"""店级重试标准(所有者定稿 2026-08-26)四步的回归。

背景:2026-08-26 13:00 两家店 SOCKS 代理报 "Malformed reply"
(socksio.ProtocolError,直接继承 Exception、httpx 不映射),落进
catalog_sync 的泛化异常桶 → raise → product_chain 八步全停。
标准:①失败店跑完别人再串行补试一遍;②仍失败不炸整轮、首行点名缺席;
③下游按目录水位避让缺席店;④链尾逐店重跑完整链一次,再失败即止。
"""

import httpx
import pytest

from api import _client
from services import store_absence, store_retry


# ── ⑤ 异常分类:SOCKS 报错必须被代理桶接住 ────────────────────────────────────

def test_store_proxy_error_is_caught_by_existing_proxy_branches():
    """StoreProxyError 子类化 httpx.ProxyError 是刻意的:全仓
    `except (StoreDeadError, httpx.ProxyError)` 分支零改动天然接住它。"""
    e = _client.StoreProxyError("client_id=abc123…", ValueError("x"))
    assert isinstance(e, httpx.ProxyError)
    assert isinstance(e, httpx.TransportError)      # 网络桶(剔池+重试)也接得住
    assert isinstance(e, _client.PROXY_ERRORS)


def test_socksio_errors_are_registered_in_the_buckets():
    """socksio 不在 httpx 异常树上,必须单列进网络桶与分类元组 ——
    漏一处就是 08-26 事故的回归(SOCKS 报错落泛化桶炸整轮)。"""
    from socksio.exceptions import ProtocolError, SOCKSError
    assert issubclass(SOCKSError, Exception)
    assert not issubclass(ProtocolError, httpx.HTTPError)   # 事故根因本身
    assert any(issubclass(ProtocolError, t) for t in _client.SOCKS_ERRORS)
    assert isinstance(ProtocolError("Malformed reply"), _client.PROXY_ERRORS)
    assert any(issubclass(ProtocolError, t) for t in _client._NET_ERRORS)


def test_get_token_wraps_transport_errors_as_store_proxy_error(monkeypatch):
    """换 token 是每店第一跳,27 个调用点曾对传输层裸奔 —— 现在 SOCKS/传输
    异常一律收口成 StoreProxyError,并顺手剔除连接池里的坏连接。"""
    from socksio.exceptions import ProtocolError

    class _Boom:
        def post(self, *a, **k):
            raise ProtocolError("Malformed reply")

    invalidated = []
    monkeypatch.setattr(_client, "_get_client", lambda proxy: _Boom())
    monkeypatch.setattr(_client, "_invalidate_client",
                        lambda proxy: invalidated.append(proxy))
    monkeypatch.setattr(_client, "_token_cache", {})
    with pytest.raises(_client.StoreProxyError) as ei:
        _client.get_token("cid_test", "secret", "socks5://x")
    assert "Malformed reply" in str(ei.value)
    assert invalidated == ["socks5://x"]            # 坏连接不留给下一家店
    # 老式分类分支(五处生产现存写法)不改一字就能接住
    try:
        raise ei.value
    except (_client.StoreDeadError, httpx.ProxyError):
        pass


def test_backoff_single_source_with_jitter():
    """退避阶梯唯一出处在 _client(#91 引入于 feeds,上提共用);feeds 只留别名
    —— 双轨禁止,谁再写第二张阶梯表这条测试拦不住,但别名断裂拦得住。"""
    from api import feeds
    assert feeds._backoff is _client.backoff
    assert feeds._BACKOFF_LADDER is _client.BACKOFF_LADDER
    assert _client.BACKOFF_LADDER == (2, 4, 8, 16, 32)
    vals = {_client.backoff(0) for _ in range(40)}
    assert all(1.0 <= v <= 2.0 for v in vals) and len(vals) > 1   # 带抖动


# ── ① 串行补试 ────────────────────────────────────────────────────────────────

def _no_sleep(monkeypatch):
    import services.store_retry as sr
    monkeypatch.setattr(sr.time, "sleep", lambda s: None)


def test_serial_second_pass_retries_once_and_splits(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def attempt(store):
        calls.append(store["name"])
        if store["name"] == "救得回":
            return {"ok": store["name"]}
        raise TimeoutError("still down")

    recovered, still, gate = store_retry.serial_second_pass(
        [({"name": "救得回"}, OSError("first")),
         ({"name": "救不回"}, OSError("first"))], attempt)
    assert calls == ["救得回", "救不回"]            # 串行、各一次
    assert [(s["name"], r) for s, r in recovered] == [("救得回", {"ok": "救得回"})]
    assert [(s["name"], type(e).__name__) for s, e in still] == \
        [("救不回", "TimeoutError")]
    assert gate == ""


def test_serial_second_pass_scale_gate_stops_systemic_failures(monkeypatch):
    """失败店数超过 max(3, 总数//5) = 系统性故障:一家都不补,点名止损 ——
    串行补试只会把故障时长按店数放大,每小时的链会拖过整点丢下一轮。"""
    _no_sleep(monkeypatch)
    attempted = []
    fails = [({"name": f"店{i}"}, OSError("x")) for i in range(4)]
    recovered, still, gate = store_retry.serial_second_pass(
        fails, lambda s: attempted.append(s), total_stores=10)
    assert attempted == [] and recovered == []      # 4 > max(3, 10//5=2) → 全停
    assert len(still) == 4 and "系统性故障" in gate
    # 不给总数 → 闸不生效(闸是止损优化,不是正确性前提)
    _r, _s, gate2 = store_retry.serial_second_pass(
        [({"name": "A"}, OSError("x"))], lambda s: {"ok": 1})
    assert gate2 == ""


def test_serial_second_pass_never_retries_dead_credentials(monkeypatch):
    """凭证死是确定性的:重试只会再死一次,一次都不试(ppc 二轮同款判据)。"""
    _no_sleep(monkeypatch)
    attempted = []
    dead = _client.StoreDeadError("T1", 400)
    recovered, still, _gate = store_retry.serial_second_pass(
        [({"name": "T1"}, dead)], lambda s: attempted.append(s))
    assert attempted == [] and recovered == []
    assert still == [({"name": "T1"}, dead)]


def test_classify_names_the_actual_culprit():
    """所有者要求(2026-08-26):出问题一眼看到是凭证失效、代理无效、
    代理波动还是沃尔玛侧 —— 六档词表,每档指一条不同的处置路。"""
    from socksio.exceptions import ProtocolError
    assert store_retry.diagnose(_client.StoreDeadError("T", 401)) == "凭证失效"
    # 代理服务器明确拒认证 = 配置错,去修凭证表的代理账号密码
    assert store_retry.diagnose(httpx.ProxyError("Invalid username/password")) \
        == "代理无效"
    # SOCKS 层其他故障 = 波动,找代理商;补试/重赛常能自愈(08-26 事故同款)
    assert store_retry.diagnose(ProtocolError("Malformed reply")) == "代理波动"
    assert store_retry.diagnose(
        _client.StoreProxyError("T", ProtocolError("Malformed reply"))) \
        == "代理波动"
    # 沃尔玛端点回了状态码(api 层 "返回 {status}" 格式):与本地无关
    assert store_retry.diagnose(RuntimeError(
        "GET /v3/items 返回 500(店铺 X): {}")) == "沃尔玛500"
    assert store_retry.diagnose(RuntimeError(
        "GET /v3/items 返回 429(店铺 X): {}")) == "沃尔玛429"
    # api 层重试耗尽后状态码是 None:网络/代理链路未达
    assert store_retry.diagnose(RuntimeError(
        "GET /v3/items 返回 None(店铺 X): None")) == "网络未达"
    assert store_retry.diagnose(ValueError("boom")) == "其他"


def test_classify_message_patterns_match_the_api_layer():
    """「沃尔玛NNN/网络未达」靠匹配 api 层自家的 "返回 {status}" 报错格式 ——
    这条钉住两端:api 层改文案而没同步 classify 时这里会红。"""
    import pathlib
    import re as _re
    api_src = "".join(p.read_text(encoding="utf-8")
                      for p in pathlib.Path("api").glob("*.py"))
    assert _re.search(r'RuntimeError\(f"[^"]*返回 \{status\}', api_src), \
        "api 层的状态码报错格式变了,同步 services/store_retry.diagnose"


# ── ③ 缺席判据(目录水位派生,不建新表、不靠调度顺序)─────────────────────────

class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        assert "max(last_seen_at)" in sql

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)


def test_stale_stores_is_fleet_relative_and_enabled_only(monkeypatch):
    """判据 = 落后**船队最新水位**超过 LAG_HOURS,不是绝对小时窗 ——
    绝对窗两头都错:早晨手动 dry-run 会把全船队误判缺席(>20h),
    而放宽到 >24h 又接不住今天刚失败的事故店(24.4h)。"""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    import services.stores as stores_svc
    monkeypatch.setattr(stores_svc, "enabled_names",
                        lambda: ["新鲜店", "缺席店", "停用外的另一家"])
    conn = _Conn([("新鲜店", now),                           # 船队最新水位
                  ("缺席店", now - timedelta(hours=24)),     # 落后 24h > 4h
                  ("停用店", now - timedelta(days=9)),       # 不在营:不进名单
                  ("停用外的另一家", None)])                  # 水位空:查不出,不误报
    assert store_absence.stale_stores(conn) == ["缺席店"]
    # since 显式锚点(cli 链尾重赛用):水位没跨过链起点 = 本轮没同步成
    assert store_absence.stale_stores(conn, since=now - timedelta(hours=2)) \
        == ["缺席店"]
    assert store_absence.stale_stores(
        conn, since=now - timedelta(days=30)) == []


def test_stale_stores_morning_dry_run_is_not_a_false_alarm(monkeypatch):
    """次日早晨手动 dry-run:全船队水位都停在昨天 13:0x,彼此滞后 ~0 ——
    谁都不缺席(那是"该不该扫"的调度纪律问题,不是缺席)。"""
    from datetime import datetime, timedelta, timezone
    yesterday = datetime.now(timezone.utc) - timedelta(hours=21)
    import services.stores as stores_svc
    monkeypatch.setattr(stores_svc, "enabled_names", lambda: ["A", "B"])
    conn = _Conn([("A", yesterday), ("B", yesterday + timedelta(minutes=40))])
    assert store_absence.stale_stores(conn) == []


# ── ④ 链尾重赛(cli._replay_absent)─────────────────────────────────────────────

def _replay_wire(monkeypatch, absent, statuses, chronic=(), still=(),
                 per_step=None):
    """statuses: {(store, step): status};缺省 success。返回 (lines, 调用序)。"""
    import contextlib
    import types

    import cli
    from registry import db as _db
    ran = []
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    from services import store_absence as sa
    monkeypatch.setattr(sa, "split_stale",
                        lambda conn, since: (list(absent), list(chronic)))
    monkeypatch.setattr(sa, "stale_stores",           # 重赛后的水位复核
                        lambda conn, since=None, lag_hours=None: list(still))

    def fake_run_step(name, module, params, dry_run, operator, logs_dir):
        ran.append((params.get("store"), name))
        st = statuses.get((params.get("store"), name), "success")
        # 形状 = _run_step 真实产物:横幅行 + 摘要(折叠必须取摘要首行)
        return st, f"{name} {st}\n{name}:{st} 摘要首行\n第二行明细"
    monkeypatch.setattr(cli, "_run_step", fake_run_step)
    mods = {
        "catalog_sync": types.SimpleNamespace(SUPPORTS_STORE=True),
        "sources_backfill": types.SimpleNamespace(),        # 全局步:重赛跳过
        "maintenance_scan": types.SimpleNamespace(SUPPORTS_STORE=True),
        "maintenance": types.SimpleNamespace(SUPPORTS_STORE=True),
    }
    steps = list(mods)
    lines = cli._replay_absent(steps, mods,
                               per_step if per_step is not None
                               else {n: {"execute": True} for n in steps},
                               False, "test", None, since=None)
    return lines, ran


def test_replay_runs_store_scoped_steps_once_per_absent_store(monkeypatch):
    lines, ran = _replay_wire(monkeypatch, ["店A"], {})
    # 全局步骤(sources_backfill)跳过;支持 store 的按链序逐店跑
    assert ran == [("店A", "catalog_sync"), ("店A", "maintenance_scan"),
                   ("店A", "maintenance")]
    assert any("sources_backfill" in ln and "跳过" in ln for ln in lines)
    assert any(ln.startswith("✅ 店A:救回") for ln in lines)
    # 每步压的是**摘要首行**(不是「名 成功」横幅):重赛跑的是 DANGEROUS
    # 步骤,发了多少 feed 不能只剩一个 ✅,更不能只剩横幅
    assert any("maintenance:success 摘要首行" in ln for ln in lines)
    assert not any(ln.strip().startswith("· maintenance success") for ln in lines)


def test_replay_stops_that_store_on_first_failure_and_says_so(monkeypatch):
    """某步失败即终止该店重赛(上游语义与主链一致),**再失败即止**,不循环;
    失败步骤摘要铺开(人要能直接看出该修凭证还是找代理商)。"""
    lines, ran = _replay_wire(monkeypatch, ["店A", "店B"],
                              {("店A", "catalog_sync"): "failed"})
    assert ("店A", "maintenance_scan") not in ran      # 店A 卡住即止
    assert ran.count(("店A", "catalog_sync")) == 1     # 只试一次,不循环
    assert [c for c in ran if c[0] == "店B"] == [      # 店B 不受店A牵连
        ("店B", "catalog_sync"), ("店B", "maintenance_scan"),
        ("店B", "maintenance")]
    stuck = next(ln for ln in lines if ln.startswith("❌ 店A:仍缺席"))
    assert "catalog_sync" in stuck and "第二行明细" in stuck   # 失败全文铺开


def test_replay_is_a_noop_without_absent_stores(monkeypatch):
    lines, ran = _replay_wire(monkeypatch, [], {})
    assert lines == [] and ran == []


def test_replay_refuses_store_scoped_chains(monkeypatch):
    """⚠ blocker 回归:主链带了 store= 范围参数时,水位判据看的是全船 ——
    重赛会把"没被本次范围覆盖"误判成"缺席",对其余全部店真跑破坏步骤。"""
    per_step = {"catalog_sync": {"execute": True, "store": "A085"},
                "sources_backfill": {}, "maintenance_scan": {},
                "maintenance": {}}
    lines, ran = _replay_wire(monkeypatch, ["店B", "店C"], {},
                              per_step=per_step)
    assert ran == []                                   # 一步都不许跑
    assert any("store= 范围参数" in ln for ln in lines)


def test_replay_skips_chronic_and_gates_on_scale(monkeypatch):
    """长期缺席(>72h)只点名不重赛;今日缺席超规模闸判系统性故障止损。"""
    lines, ran = _replay_wire(monkeypatch, [], {}, chronic=["死店"])
    assert ran == [] and any("长期缺席" in ln and "死店" in ln for ln in lines)
    many = [f"店{i}" for i in range(6)]                # 6 > REPLAY_MAX_STORES=5
    lines2, ran2 = _replay_wire(monkeypatch, many, {})
    assert ran2 == [] and any("系统性故障" in ln for ln in lines2)


def test_replay_demotes_green_steps_when_watermark_did_not_advance(monkeypatch):
    """步骤全绿 ≠ 救回:重赛后水位仍没跨过链起点的(在线 0 商品店等),
    照实说仍缺席,不发假 ✅。"""
    lines, _ran = _replay_wire(monkeypatch, ["店A"], {}, still=["店A"])
    assert any("水位未推进" in ln for ln in lines)
    assert not any(ln.startswith("✅ 店A") for ln in lines)


def test_replay_says_unverified_when_recheck_itself_fails(monkeypatch):
    """水位复核探测失败 ≠ 都救回了(2026-08-26 审计实见:裸 except 落空 still
    → 每家都发 ✅):按「未核实」报,不发假 ✅,也不炸重赛结果。"""
    import contextlib
    import types

    import cli
    from registry import db as _db
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    from services import store_absence as sa
    monkeypatch.setattr(sa, "split_stale", lambda conn, since: (["店A"], []))

    def boom(conn, since=None, lag_hours=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(sa, "stale_stores", boom)
    monkeypatch.setattr(cli, "_run_step",
                        lambda name, module, params, dry_run, operator,
                        logs_dir: ("success", f"{name} 成功\n{name}:摘要首行"))
    mods = {"catalog_sync": types.SimpleNamespace(SUPPORTS_STORE=True)}
    lines = cli._replay_absent(list(mods), mods, {"catalog_sync": {}},
                               False, "test", None, since=None)
    assert any("水位复核失败" in ln and "未核实" in ln for ln in lines)
    assert not any(ln.startswith("✅ 店A:救回") for ln in lines)


def test_main_wires_replay_after_a_successful_catalog_chain(monkeypatch,
                                                            tmp_path):
    """④ 在 main() 里的接线回归:对抗校验变异实测曾证明整段删掉全量测试照样
    绿。钉三件事:主链全成 + 含 catalog_sync 才重赛;锚点 = 链起点(不是
    None,传错会退化成船队相对口径);结果拼进那条唯一的链通知。"""
    import cli
    monkeypatch.setenv("WALMART_DATA_ROOT", str(tmp_path))
    calls = {"replay": None, "notify": []}
    monkeypatch.setattr(
        cli, "_run_step",
        lambda name, module, params, dry_run, operator, logs_dir:
        ("success", f"{name} 成功\n首行{name}"))

    def fake_replay(steps, modules, per_step, dry_run, operator, logs_dir,
                    since):
        calls["replay"] = (tuple(steps), since)
        return ["—— 重赛桩 ——"]
    monkeypatch.setattr(cli, "_replay_absent", fake_replay)
    monkeypatch.setattr(cli, "_notify",
                        lambda text: calls["notify"].append(text))
    assert cli.main(["catalog_sync", "maintenance_scan"]) == 0
    steps, since = calls["replay"]
    assert steps == ("catalog_sync", "maintenance_scan")
    assert since is not None
    assert "—— 重赛桩 ——" in calls["notify"][0]


def test_main_skips_replay_when_chain_failed_or_lacks_catalog_sync(
        monkeypatch, tmp_path):
    """主链没跑完 = 半成品数据,不重赛;链里没有 catalog_sync = 水位无锚,
    不重赛(order_chain 等链与本机制无关)。"""
    import cli
    monkeypatch.setenv("WALMART_DATA_ROOT", str(tmp_path))
    called = []
    monkeypatch.setattr(cli, "_replay_absent",
                        lambda *a, **k: called.append(1) or [])
    monkeypatch.setattr(cli, "_notify", lambda text: None)
    monkeypatch.setattr(cli, "_run_step",
                        lambda name, *a, **k: ("failed", f"{name} 失败"))
    assert cli.main(["catalog_sync", "maintenance_scan"]) == 1
    monkeypatch.setattr(cli, "_run_step",
                        lambda name, *a, **k: ("success", f"{name} 成功"))
    cli.main(["maintenance_scan", "maintenance"])
    assert called == []


# ── ② 落库面:withdraw_stale 的缺席排除口(缺席 ≠ 恢复正常)─────────────────────

def test_executors_hold_absent_store_rows(monkeypatch):
    """③ 补全回归(对抗校验):扫描件避让 + withdraw 护行之后,执行件不避让
    的话,缺席店的存量 suggested 会在同一轮链里照样被领走提交 —— 隔夜现值
    不配拿来改线上/开破坏 feed。缺席店的行留在 suggested 原地。"""
    import tests.test_maintenance as tm
    intents = [
        {"store": "缺席店", "sku": "A", "kind": "inventory", "old": 5, "new": 0,
         "code": "out_of_stock", "reason": "x"},
        {"store": tm.STORE["name"], "sku": "B", "kind": "inventory",
         "old": 5, "new": 0, "code": "out_of_stock", "reason": "x"}]
    calls = tm._wire(monkeypatch, intents, absent=("缺席店",))
    out = tm.mw.run({"execute": True})
    assert "缺席避让:1 条" in out
    assert all(sku == "B" for _s, sku, _q in calls["put_inv"])   # 缺席店没执行


def test_cap_destructive_counts_todays_already_executed():
    """「下架限制」是按天的语义:当日已放行的先扣掉,链尾重赛/人工重跑
    不把上限翻倍(2026-08-24 归一消灭「按来源翻倍」,别让「按轮次翻倍」回来)。"""
    from services import dispositions as ds
    rows = [{"store": "A", "action": "delete"} for _ in range(5)]
    kept, over = ds.cap_destructive(rows, {"A": 4}, 300,
                                    executed_today={"A": 3})
    assert len(kept) == 1 and over == {"A": 4}       # 4-3=1 个名额
    kept2, _ = ds.cap_destructive(rows, {"A": 4}, 300, executed_today={"A": 9})
    assert kept2 == []                               # 超支不给负数名额


def test_maint_settle_has_a_grace_period():
    """链尾重赛把「提交→重新观测」从 16~24h 压到十几分钟:没有宽限期的话,
    主链刚提交的 feed 会被重赛的观测判成「未生效」销案(把"太早看"谎报成
    "沃尔玛没执行")。"""
    from services import dispositions as ds
    assert "make_interval(hours => %(grace)s::int)" in ds._MAINT_OPEN_SQL
    assert ds.MAINT_SETTLE_GRACE_HOURS >= 1


def test_order_sync_full_standard_and_zero_gate(monkeypatch):
    """order_sync 走全套标准(2026-08-26 审计补齐:此前只做隔离+分诊,同链
    returns_sync 是全套):失败店**串行补试**重调同一个 fetch_orders_bulk
    (单店入参),仍失败以「⚠ 缺席」+ 归类词点名进摘要**首行**;补试救回的
    照常入账;零店完成必须失败。"""
    import pytest as _pytest

    from workflows import order_sync as osw
    monkeypatch.setattr(osw.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}, {"name": "T2"}])
    monkeypatch.setattr(osw.order_center, "push_after",
                        lambda spec, days=90: "投影桩")
    from socksio.exceptions import ProtocolError
    calls = []

    def fake_bulk(stores, **k):
        calls.append([s["name"] for s in stores])
        if len(stores) > 1:                     # 首轮:T2 断
            return ([{"store": "T1", "lines": 3}], [],
                    [("T2", ProtocolError("Malformed reply"))])
        return ([], [], [(stores[0]["name"],    # 补试:同店再断
                          ProtocolError("Malformed reply"))])

    monkeypatch.setattr(osw.orders_api, "fetch_orders_bulk", fake_bulk)
    out = osw.run({})
    assert calls == [["T1", "T2"], ["T2"]]      # 补试 = 单店重调同一个函数
    first = out.splitlines()[0]                 # 标准③:缺席点名在首行
    assert "⚠ 缺席 1 店:T2(代理波动)" in first
    assert "已串行补试仍失败" in first

    calls.clear()                               # 补试救回:照常入账,无缺席

    def fake_bulk_recover(stores, **k):
        calls.append([s["name"] for s in stores])
        if len(stores) > 1:
            return ([{"store": "T1", "lines": 3}], [],
                    [("T2", ProtocolError("Malformed reply"))])
        return ([{"store": "T2", "lines": 7}], [], [])

    monkeypatch.setattr(osw.orders_api, "fetch_orders_bulk", fake_bulk_recover)
    out = osw.run({})
    assert "2/2 店完成" in out and "订单行入库 10" in out
    assert "缺席" not in out

    monkeypatch.setattr(osw.orders_api, "fetch_orders_bulk",
                        lambda stores, **k: ([], ["T1", "T2"], []))
    with _pytest.raises(RuntimeError, match="零店完成"):
        osw.run({})


def test_workflows_that_use_store_retry_import_it():
    """调 store_retry.* 的 workflow 必须真的 import 它 —— 2026-08-26 审计实见
    三个文件(daily_report/perf_problems/settlement_sync)复制了分诊块却漏了
    import:店一失败 except 体先 NameError,单店隔离变成一店放倒整轮。
    用 AST 判引用(文本 grep 会被注释里的字样骗过 —— order_sync 当初就是
    这么漏网的)。"""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "workflows"
    offenders = []
    for py in sorted(root.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        uses = any(isinstance(n, ast.Attribute)
                   and isinstance(n.value, ast.Name)
                   and n.value.id == "store_retry" for n in ast.walk(tree))
        if not uses:
            continue
        imported = any(
            isinstance(n, ast.ImportFrom) and n.module == "services"
            and any(a.name == "store_retry" for a in n.names)
            for n in ast.walk(tree))
        if not imported:
            offenders.append(py.name)
    assert not offenders, f"调 store_retry 却没 import:{offenders}"


def test_withdraw_sql_carries_the_exclude_stores_clause():
    """缺席店的行不在 keep 里(本轮避让了),不排除会被撤成
    「商品自己恢复正常了」—— 两条 SQL 分支都必须带排除口。"""
    import inspect

    from services import dispositions as ds
    assert "NOT (d.store = ANY(%(exclude)s::text[]))" in ds._WITHDRAW_SQL
    src = inspect.getsource(ds.withdraw_stale)
    assert src.count("NOT (d.store = ANY(%(exclude)s::text[]))") >= 1  # 空 keep 分支
    assert "exclude_stores" in src
