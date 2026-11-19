"""node_create:给店铺账户新建发货节点(2026-10-24 所有者定稿,推翻「不自动建仓」)。

钉四件事:载荷键集合对官方样例(refdata/specs 原件,不照记忆改字段)、
POST 三种结局三种信号(成功 / 确认未达 / 不明)、防重(同名不发、pending 反查、
宽限期按兵不动、dry-run 零写入)、两个新桶登记且落稀缺档。
"""

import contextlib
import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from api import _client, settings as settings_api
from services import ship_nodes, store_events as se
from workflows import node_create as nc

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SAMPLE = json.loads((_ROOT / "refdata/specs/ship_node_create_1.2_sample.json")
                     .read_text(encoding="utf-8"))

_FULL = {"name": "中山仓", "address1": "zhongshan 17hao", "city": "zhongshanshi",
         "state": "GD", "country": "CN", "postal": "528400", "tz": "CST",
         "carrier": "fedex", "services": "TWO_DAY_DELIVERY", "custom_id": "zs01",
         "workdays": "mon-fri", "cutoff": "11:30", "days_off": "2026-12-25"}
_MIN = {k: _FULL[k] for k in ship_nodes.REQUIRED}


def _keys(obj, prefix="") -> set[str]:
    """递归键路径(数组元素不分序号),拿来比"我们发的键官方样例里都有"。"""
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(f"{prefix}{k}")
            out |= _keys(v, f"{prefix}{k}.")
    elif isinstance(obj, list):
        for v in obj:
            out |= _keys(v, f"{prefix}[].")
    return out


# ── 载荷 ─────────────────────────────────────────────────────────────────────

def test_full_payload_uses_only_keys_from_the_official_sample():
    node = ship_nodes.build_node(_FULL)
    official = _keys(_SAMPLE["shipNode"][0])
    extra = _keys(node) - official
    assert not extra, f"发了官方样例里没有的键:{sorted(extra)}"
    # 必填五项全在;承运商大写;日历七天齐、工作日带截单、休息日不带
    assert node["shipNodeName"] == "中山仓" and node["status"] == "ACTIVE"
    assert node["postalAddress"] == {"addressLine1": "zhongshan 17hao", "city": "zhongshanshi",
                                     "state": "GD", "country": "CN", "postalCode": "528400"}
    assert node["shippingDetails"] == [{"twoDayShipping": [
        {"carrierMethodName": "FEDEX", "carrierMethodType": "GROUND"}]}]
    sched = node["calendarDayConfiguration"]["standardProcessingSchedule"]
    assert set(sched) == set(_SAMPLE["shipNode"][0]["calendarDayConfiguration"]
                             ["standardProcessingSchedule"])
    assert sched["monday"] == {"isWorkingDay": True, "cutOffTime": "11:30"}
    assert sched["sunday"] == {"isWorkingDay": False}
    assert node["calendarDayConfiguration"]["additionalDaysOff"] == ["2026-12-25"]


def test_minimal_payload_carries_no_invented_optional_keys_but_two_arrays_are_always_present():
    """可选项没给就不带键 —— 两个例外 distributorSupportedServices / shippingDetails:
    2026-10-24 两次真跑实证(L001贾林红 400「can't be null」×2)它们必须在,
    不提供两日达 / 不声明承运方式就发空数组,不是不带键。"""
    node = ship_nodes.build_node(_MIN)
    assert set(node) == {"shipNodeName", "status", "timeZone", "postalAddress",
                         "distributorSupportedServices", "shippingDetails"}
    assert node["distributorSupportedServices"] == []
    assert node["shippingDetails"] == []
    assert "addressLine2" not in node["postalAddress"]


def test_address2_and_services_are_normalized_when_given():
    node = ship_nodes.build_node({**_MIN, "address2": " 3-5 ", "services": "two_day_delivery",
                                  "tz": "cst"})
    assert node["postalAddress"]["addressLine2"] == "3-5"     # 官方 schema 的字段名
    assert node["distributorSupportedServices"] == ["TWO_DAY_DELIVERY"]
    assert node["timeZone"] == "CST"


@pytest.mark.parametrize("bad, word", [
    ({**_MIN, "name": ""}, "缺必填项"),
    ({**_MIN, "status": "OPEN"}, "status"),
    ({**_MIN, "workdays": "mon-fri"}, "必须同给"),
    ({**_MIN, "cutoff": "11:30"}, "必须同给"),
    ({**_MIN, "workdays": "mon-fri", "cutoff": "25:00"}, "HH:MM"),
    ({**_MIN, "workdays": "mon,xyz", "cutoff": "11:30"}, "认不出"),
    ({**_MIN, "days_off": "2026/12/25"}, "YYYY-MM-DD"),
    ({**_MIN, "tz": "Asia/Shanghai"}, "timeZone 枚举"),
    ({**_MIN, "services": "STANDARD"}, "官方允许值"),
])
def test_spec_errors_name_the_offending_field(bad, word):
    with pytest.raises(ship_nodes.SpecError, match=word):
        ship_nodes.build_node(bad)


def test_parse_workdays_accepts_ranges_lists_and_wraparound():
    assert ship_nodes.parse_workdays("mon-fri") == ["mon", "tue", "wed", "thu", "fri"]
    assert ship_nodes.parse_workdays("sat,sun,mon") == ["sat", "sun", "mon"]
    assert ship_nodes.parse_workdays("fri-mon") == ["fri", "sat", "sun", "mon"]


def test_find_existing_matches_name_loosely_and_custom_id():
    nodes = {"100": {"shipNodeName": " Zhong Shan  仓 ", "customNodeId": "zs01"},
             "200": {"shipNodeName": "Virtual", "nodeType": "VIRTUAL"}}
    assert ship_nodes.find_existing(nodes, "zhong shan 仓") == "100"
    assert ship_nodes.find_existing(nodes, "别的名", custom_id="zs01") == "100"
    assert ship_nodes.find_existing(nodes, "别的名") is None


# ── api 层:三种结局三种信号 ───────────────────────────────────────────────────

def _store(name="T1"):
    return {"name": name, "client_id": "cid-" + name, "client_secret": "sec", "proxy": None}


def _wire_api(monkeypatch, status, data, calls):
    monkeypatch.setattr(settings_api._client, "rate_acquire", lambda *a: None)
    monkeypatch.setattr(settings_api._client, "get_token", lambda *a: "tok")
    monkeypatch.setattr(settings_api._client, "base_url", lambda: "https://x")

    def post(url, token, client_id, proxy, json_body=None, params=None,
             timeout=30, max_retries=0, error_body=False):
        calls.append({"url": url, "body": json_body, "max_retries": max_retries,
                      "error_body": error_body})
        return status, {}, data

    monkeypatch.setattr(settings_api._client, "safe_post_ex", post)


def test_create_ship_node_posts_official_envelope_without_retries_and_clears_cache(monkeypatch):
    calls: list = []
    node = ship_nodes.build_node(_MIN)
    _wire_api(monkeypatch, 200, [{**node, "shipNode": "91539778610008065",
                                  "nodeType": "PHYSICAL"}], calls)
    cleared = []
    monkeypatch.setattr(settings_api._cached_ship_nodes, "cache_clear",
                        lambda: cleared.append(1))
    got = settings_api.create_ship_node(_store(), node)
    assert got["shipNode"] == "91539778610008065"
    assert calls[0]["url"].endswith("/v3/settings/shipping/shipnodes")
    assert calls[0]["max_retries"] == 0                 # POST 不幂等,不自动重试
    assert calls[0]["error_body"] is True               # 被拒原因要进摘要与台账,不能只在日志里
    assert calls[0]["body"] == {"shipNodeHeader": {"version": _SAMPLE["shipNodeHeader"]["version"]},
                                "shipNode": [node]}
    assert cleared == [1]                               # 同进程紧接着的校验要看得见新仓


@pytest.mark.parametrize("status, data, exc", [
    (400, {"errors": ["bad"]}, RuntimeError),                    # 确认未达
    (403, None, RuntimeError),
    (None, None, settings_api.ShipNodeOutcomeUnknown),           # 网络中断
    (503, None, settings_api.ShipNodeOutcomeUnknown),            # 5xx 无体:不知道到没到
    (502, {"errors": [{"description": "gateway", "category": "SYSTEM"}]},
     settings_api.ShipNodeOutcomeUnknown),                       # 5xx 系统侧:同上
    # 5xx 但沃尔玛明说是请求侧(2026-10-24 第三次真跑实见 520 HV000028):确认未达
    (520, {"errors": [{"description": "HV000028: Unexpected exception during isValid call.",
                       "severity": "ERROR", "category": "REQUEST"}]}, RuntimeError),
    (200, [], settings_api.ShipNodeOutcomeUnknown),              # 200 却读不出节点
    (200, [{"shipNode": "1"}, {"shipNode": "2"}], settings_api.ShipNodeOutcomeUnknown),
])
def test_create_ship_node_signals_rejected_vs_unknown(monkeypatch, status, data, exc):
    _wire_api(monkeypatch, status, data, [])
    with pytest.raises(exc):
        settings_api.create_ship_node(_store(), ship_nodes.build_node(_MIN))
    if exc is RuntimeError:
        # 「不明」是 RuntimeError 的子类,确认未达那边必须**不是**不明
        with pytest.raises(RuntimeError) as ei:
            settings_api.create_ship_node(_store(), ship_nodes.build_node(_MIN))
        assert not isinstance(ei.value, settings_api.ShipNodeOutcomeUnknown)


def test_rejection_message_carries_walmart_field_and_description(monkeypatch):
    """2026-10-24 首跑:摘要与台账记的是「400: None」,原因只在日志 —— 这条把原文钉进异常文案。"""
    body = {"errors": [{"code": "INVALID_REQUEST_CONTENT.INVALID_REQUEST", "field": "shipNode",
                        "description": "shipNode object has invalid data: "
                                       "distributorSupportedServices can't be null",
                        "severity": "ERROR"}]}
    _wire_api(monkeypatch, 400, body, [])
    with pytest.raises(RuntimeError, match=r"400: \[shipNode\] .*can't be null"):
        settings_api.create_ship_node(_store(), ship_nodes.build_node(_MIN))
    assert settings_api._error_text({"text": "<html>Akamai</html>"}) == "<html>Akamai</html>"
    assert settings_api._error_text(None) == "None"


def test_request_ex_returns_error_body_only_when_opted_in(monkeypatch):
    class _Resp:
        status_code, content, text = 400, b"x", '{"errors":[{"description":"bad"}]}'
        headers = {}

        def json(self):
            return {"errors": [{"description": "bad"}]}

    class _Client:
        def request(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(_client, "_get_client", lambda proxy: _Client())
    monkeypatch.setattr(_client, "make_headers", lambda t, c: {})
    st, _, data = _client._request_ex("POST", "https://x/y", "tok", "cid", None)
    assert (st, data) == (400, None)                     # 老契约不变
    st, _, data = _client._request_ex("POST", "https://x/y", "tok", "cid", None, error_body=True)
    assert (st, data) == (400, {"errors": [{"description": "bad"}]})


def test_list_carrier_methods_accepts_bare_array_and_raises_on_empty(monkeypatch):
    monkeypatch.setattr(settings_api._client, "rate_acquire", lambda *a: None)
    monkeypatch.setattr(settings_api._client, "get_token", lambda *a: "tok")
    monkeypatch.setattr(settings_api._client, "base_url", lambda: "https://x")
    rows = [{"carrierMethodName": "FEDEX", "carrierMethodType": "GROUND"}]
    monkeypatch.setattr(settings_api._client, "safe_get_ex",
                        lambda *a, **k: (200, {}, rows))
    assert settings_api.list_carrier_methods(_store()) == rows
    monkeypatch.setattr(settings_api._client, "safe_get_ex",
                        lambda *a, **k: (200, {}, {"payload": []}))
    with pytest.raises(RuntimeError):
        settings_api.list_carrier_methods(_store())


def test_two_new_buckets_are_registered_and_persistent():
    """官方都是 10/min:上限 ≤10 必须落稀缺档(跨进程共享),与 feeds.post 同理。"""
    for b in ("settings.carriers", "settings.shipnodes.create"):
        assert b in _client._RATE_BUCKETS, b
        assert _client._is_persistent(b), b


# ── 台账:反查收编 / 宽限期 ────────────────────────────────────────────────────

class _Cur:
    def __init__(self, pending):
        self.pending, self.sqls = pending, []

    def execute(self, sql, params):
        self.sqls.append((sql.strip().split()[0], dict(params)))
        self._last = sql

    def fetchall(self):
        return list(self.pending)

    def fetchone(self):
        return (7,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, pending=()):
        self.cur = _Cur(list(pending))

    def cursor(self):
        return self.cur


def test_reconcile_adopts_pending_when_same_name_is_now_listed():
    now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    conn = _Conn([(5, now - timedelta(minutes=1))])
    found, blocked, lines = ship_nodes.reconcile_pending(
        conn, "T1", "中山仓", {"9": {"shipNodeName": "中山仓"}}, now=now)
    assert found == "9" and blocked is False
    updates = [p for verb, p in conn.cur.sqls if verb == "UPDATE"]
    assert updates == [{"id": 5, "status": "created", "ship_node": "9",
                        "note": updates[0]["note"]}]


def test_reconcile_blocks_inside_grace_and_fails_after_it():
    now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    fresh = _Conn([(5, now - timedelta(minutes=2))])
    found, blocked, _ = ship_nodes.reconcile_pending(fresh, "T1", "中山仓", {}, now=now)
    assert found is None and blocked is True
    assert not [1 for verb, _ in fresh.cur.sqls if verb == "UPDATE"]
    old = _Conn([(5, now - timedelta(minutes=ship_nodes.PENDING_GRACE_MINUTES))])
    found, blocked, _ = ship_nodes.reconcile_pending(old, "T1", "中山仓", {}, now=now)
    assert found is None and blocked is False
    assert [p["status"] for verb, p in old.cur.sqls if verb == "UPDATE"] == ["failed"]


def test_settle_only_accepts_terminal_states():
    with pytest.raises(ValueError):
        ship_nodes.settle(_Conn(), 1, "pending")


# ── workflow ─────────────────────────────────────────────────────────────────

def _wire_wf(monkeypatch, *, stores, nodes_by_store=None, carriers=None,
             create=None, pending=None):
    """接好店铺表、节点列表、承运商、POST、台账与事件账本;返回观测记录。"""
    seen = {"posted": [], "opened": [], "settled": [], "events": [], "carriers_called": 0}
    monkeypatch.setattr(nc.stores_svc, "load_stores",
                        lambda filter_names=None: [s for s in stores
                                                   if filter_names is None
                                                   or s["name"] in filter_names])
    monkeypatch.setattr(nc.settings, "list_ship_nodes",
                        lambda s: dict((nodes_by_store or {}).get(s["name"], {})))

    def _carriers(s):
        seen["carriers_called"] += 1
        return list(carriers or [])

    monkeypatch.setattr(nc.settings, "list_carrier_methods", _carriers)

    def _create(s, node):
        seen["posted"].append((s["name"], node))
        r = create(s) if callable(create) else create
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(nc.settings, "create_ship_node", _create)

    @contextlib.contextmanager
    def pg_conn():
        yield object()

    monkeypatch.setattr(nc.db, "pg_conn", pg_conn)
    monkeypatch.setattr(nc.ship_nodes, "reconcile_pending",
                        lambda conn, store, name, nodes, now=None:
                        (pending or {}).get(store, (None, False, [])))

    def _open(conn, store, name, node):
        seen["opened"].append((store, name))
        return 7

    monkeypatch.setattr(nc.ship_nodes, "open_request", _open)
    monkeypatch.setattr(nc.ship_nodes, "settle",
                        lambda conn, rid, status, ship_node=None, note=None:
                        seen["settled"].append((rid, status, ship_node)))
    monkeypatch.setattr(nc.se, "record_many",
                        lambda conn, rows: seen["events"].extend(rows) or len(rows))
    monkeypatch.setattr(nc.store_retry, "serial_second_pass",
                        lambda failures, attempt, total_stores=None: ([], list(failures), ""))
    return seen


_OK = {"shipNode": "1011159562695573505", "nodeType": "PHYSICAL"}




def test_dry_run_reads_but_writes_nothing_and_lists_carriers(monkeypatch):
    seen = _wire_wf(monkeypatch, stores=[_store("T1")], create=_OK,
                    carriers=[{"carrierMethodName": "FEDEX", "carrierMethodType": "GROUND"},
                              {"carrierMethodName": "UPS", "carrierMethodType": "GROUND"}])
    out = nc.run({**_MIN, "store": "T1", "dry_run": True, "execute": False})
    assert out.startswith("[DRY-RUN] 建仓「中山仓」:1 店 → 将新建 1 家")
    assert "一个字都没发" in out
    assert "可用承运方法" in out and "FEDEX/GROUND,UPS/GROUND" in out   # 没填承运商就把能填的列出来
    assert seen["carriers_called"] == 1
    assert not seen["posted"] and not seen["opened"] and not seen["events"]


def test_existing_name_is_never_recreated(monkeypatch):
    seen = _wire_wf(monkeypatch, stores=[_store("T1")],
                    nodes_by_store={"T1": {"555": {"shipNodeName": "中山仓"}}}, create=_OK)
    out = nc.run({**_MIN, "store": "T1", "dry_run": False, "execute": True})
    assert "已存在 1" in out.splitlines()[0] and "FC 555" in out
    assert not seen["posted"] and not seen["opened"]


def test_real_run_opens_ledger_before_post_then_settles_and_records_event(monkeypatch):
    seen = _wire_wf(monkeypatch, stores=[_store("T1")], create=_OK)
    out = nc.run({**_MIN, "store": "T1", "dry_run": False, "execute": True})
    assert out.splitlines()[0] == "建仓「中山仓」:1 店 → 新建 1 家"
    assert seen["opened"] == [("T1", "中山仓")]
    assert seen["posted"][0][0] == "T1"
    assert seen["settled"] == [(7, "created", "1011159562695573505")]
    assert [e["event"] for e in seen["events"]] == [se.SHIP_NODE_CREATED]
    assert seen["events"][0]["detail"]["ship_node"] == "1011159562695573505"
    assert "维护仓库" in out and "4 小时" in out      # 两件人工后续要提醒
    assert "共用同一仓库地址" not in out              # 单店不点名


def test_rejected_post_settles_failed_and_unknown_keeps_pending(monkeypatch):
    seen = _wire_wf(monkeypatch, stores=[_store("A"), _store("B")],
                    create=lambda s: (RuntimeError("建仓被拒,沃尔玛返回 400: x")
                                      if s["name"] == "A"
                                      else settings_api.ShipNodeOutcomeUnknown("None")))
    out = nc.run({**_MIN, "store": "A,B", "dry_run": False, "execute": True})
    head = out.splitlines()[0]
    assert "⚠ 被拒 1" in head and "⚠ 结局不明 1" in head
    assert seen["settled"] == [(7, "failed", None)]      # 不明的那条不落终态
    assert not seen["events"]
    assert "共用同一仓库地址" in out                   # 多店点名关联信号


def test_unknown_carrier_skips_the_store_without_posting(monkeypatch):
    seen = _wire_wf(monkeypatch, stores=[_store("T1")], create=_OK,
                    carriers=[{"carrierMethodName": "USPS", "carrierMethodType": "GROUND"}])
    out = nc.run({**_MIN, "carrier": "FEDEX", "store": "T1", "dry_run": False, "execute": True})
    assert "承运商不认跳过 1" in out.splitlines()[0]
    assert seen["carriers_called"] == 1 and not seen["posted"] and not seen["opened"]


def test_pending_inside_grace_blocks_and_adopted_pending_counts_as_exists(monkeypatch):
    seen = _wire_wf(monkeypatch, stores=[_store("A"), _store("B")], create=_OK,
                    pending={"A": (None, True, ["    ⚠ 上次提交结局不明"]),
                             "B": ("777", False, ["    收编 777"])})
    out = nc.run({**_MIN, "store": "A,B", "dry_run": False, "execute": True})
    head = out.splitlines()[0]
    assert "结局不明按兵不动 1" in head and "已存在 1" in head
    assert not seen["posted"]


def test_missing_required_param_prints_help_and_touches_nothing(monkeypatch):
    seen = _wire_wf(monkeypatch, stores=[_store("T1")], create=_OK)
    out = nc.run({"store": "T1", "name": "x", "dry_run": False, "execute": True})
    assert out.startswith("⚠ 建仓信息缺必填项") and "address1" in out
    assert not seen["posted"]
    assert nc.run({"dry_run": True}).startswith("⚠ 缺参数:-p store=")


def test_event_code_is_registered_with_a_brief():
    assert se.CLASS[se.SHIP_NODE_CREATED] == "governance"
    assert "中山仓" in se._brief_body(se.SHIP_NODE_CREATED,
                                      {"name": "中山仓", "ship_node": "1", "city": "zs"})
