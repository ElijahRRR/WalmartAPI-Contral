"""weight_fix:按名单把已上架商品的发货重量改成固定值(临时工具,所有者 2026-10-16)。

钉住四件事:载荷键集合在官方 MP_MAINTENANCE 规范原件(摘录)之内、只改重量;
名单只认「店铺」「SKU」两列;查不到 / 没 UPC / 不可调用的店不发且点名;
dry-run 一条不发,真跑走 feed 台账(workflow=weight_fix)。
"""

import contextlib
import csv
import json
from pathlib import Path

import pytest

from api import feeds
from workflows import weight_fix as wf

_EXCERPT = (Path(__file__).resolve().parent.parent / "refdata" / "specs"
            / "MP_MAINTENANCE_5.0.20260703-18_22_27-api.orderable_excerpt.json")


def _spec():
    return json.loads(_EXCERPT.read_text(encoding="utf-8"))


def _store(name):
    return {"name": name, "client_id": "cid", "client_secret": "sec", "proxy": None}


# ── 载荷对照官方规范原件 ─────────────────────────────────────────────────────

def test_item_keys_are_inside_the_official_maintenance_orderable():
    """MPItem 只有 Orderable;Orderable 键集合 ⊆ 原件 20 个属性、含全部必填;
    productIdentifiers 两键齐全且类型在原件枚举里。"""
    spec = _spec()
    item = wf.build_item("B000OMIW04", "012345678905", 11)
    assert set(item) <= set(spec["MPItem_item"]["properties"])
    orderable = item["Orderable"]
    allowed = set(spec["Orderable"]["property_names"])
    assert spec["Orderable"]["additionalProperties"] is False
    assert set(orderable) <= allowed, set(orderable) - allowed
    assert set(spec["Orderable"]["required"]) <= set(orderable)
    pi_spec = spec["Orderable"]["properties"]["productIdentifiers"]
    assert set(orderable["productIdentifiers"]) == set(pi_spec["required"])
    assert orderable["productIdentifiers"]["productIdType"] in \
        pi_spec["properties"]["productIdType"]["enum"]


def test_only_shipping_weight_is_changed():
    """除了必填的 sku / productIdentifiers,只带 ShippingWeight:价格、库存、标题都不碰。"""
    orderable = wf.build_item("S1", "012345678905", 11)["Orderable"]
    assert set(orderable) == {"sku", "productIdentifiers", "ShippingWeight"}
    sw = _spec()["Orderable"]["properties"]["ShippingWeight"]
    assert sw["type"] == "number" and "lbs" in sw["title"]
    assert isinstance(orderable["ShippingWeight"], float)
    assert orderable["ShippingWeight"] == 11.0
    assert "Visible" not in wf.build_item("S1", "012345678905", 11)


def test_payload_through_the_standard_feed_builder_keeps_the_weight():
    """走 api/feeds 唯一的载荷构造:header 三字段,条目原样(数值 ≤2 位小数)。"""
    payload = feeds.build_payload("MP_MAINTENANCE",
                                  [wf.build_item("S1", "012345678905", 11)])
    assert set(payload["MPItemFeedHeader"]) == {"businessUnit", "locale", "version"}
    assert payload["MPItem"][0]["Orderable"]["ShippingWeight"] == 11.0


def test_weight_param():
    assert wf._weight(None) == wf.DEFAULT_WEIGHT_LBS == 11.0
    assert wf._weight("11") == 11.0
    assert wf._weight("7.126") == 7.13
    for bad in ("0", "-3", "abc"):
        with pytest.raises(ValueError):
            wf._weight(bad)


# ── 名单读取 ────────────────────────────────────────────────────────────────

_HEADER = ["店铺", "SKU", "Item ID", "商品名称", "运输重量", "重量单位", "获取状态",
           "目录发布状态", "目录生命周期"]


def _xlsx(tmp_path, rows, header=_HEADER):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    p = tmp_path / "list.xlsx"
    wb.save(p)
    return str(p)


def test_read_list_xlsx_takes_only_store_and_sku(tmp_path):
    path = _xlsx(tmp_path, [
        ["谭总2", "B000OMIW04", 20924668493, "Hydrant Wrench", 1814, "LB"],
        ["谭总2", "B09CCQW76C", 20897865103, "Ladder", 13200, "LB"],
        ["谭总2", "B000OMIW04", 1, "重复行", 1, "LB"],      # 同 (店铺, SKU) 只发一次
        ["A109黄威威", 123456, 1, "数字 SKU", 20, "LB"],      # Excel 存成数字的 SKU
        ["", "B0X", 1, "缺店铺", 20, "LB"],
        [None, None, None, None, None, None],               # 表尾空行不算数
    ])
    by_store, stat = wf.read_list(path)
    assert by_store == {"谭总2": ["B000OMIW04", "B09CCQW76C"],
                        "A109黄威威": ["123456"]}
    assert stat == {"rows": 5, "blank": 1, "dup": 1}


def test_read_list_csv_and_english_headers(tmp_path):
    p = tmp_path / "list.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["store", "sku", "weight"])
        w.writerow(["谭总2", " B0CVXQGFZK ", "438"])
    by_store, _ = wf.read_list(str(p))
    assert by_store == {"谭总2": ["B0CVXQGFZK"]}


def test_read_list_missing_column_raises(tmp_path):
    path = _xlsx(tmp_path, [["谭总2", "B0X"]], header=["店铺", "Item ID"])
    with pytest.raises(ValueError, match="店铺.*SKU"):
        wf.read_list(path)


# ── run:查库、跳过、提交 ────────────────────────────────────────────────────

class _Cur:
    def __init__(self, catalog, seen):
        self._catalog, self._seen, self._rows = catalog, seen, []

    def execute(self, sql, params):
        self._seen.append(dict(params))
        cat = self._catalog.get(params["store"], {})
        self._rows = [(s, cat[s]) for s in params["skus"] if s in cat]

    def fetchall(self):
        return list(self._rows)


def _wire(monkeypatch, callable_stores, catalog, outcome="submitted"):
    """接好可调用店铺、目录(店 → {sku: upc})与 feed 提交;返回 (查询记录, 提交记录)。"""
    monkeypatch.setattr(wf.stores_svc, "load_stores",
                        lambda filter_names=None: [
                            _store(n) for n in callable_stores
                            if filter_names is None or n in filter_names])
    seen: list = []

    @contextlib.contextmanager
    def pg_conn():
        class _Conn:
            @contextlib.contextmanager
            def cursor(self):
                yield _Cur(catalog, seen)
        yield _Conn()

    monkeypatch.setattr(wf.db, "pg_conn", pg_conn)
    sent: list = []

    seq = list(outcome) if isinstance(outcome, list) else None

    def submit_feed(store, feed_type, entries, *, workflow="", defer_settle=False):
        sent.append((store["name"], feed_type, list(entries), workflow))
        o = seq.pop(0) if seq is not None else outcome
        extra = {}
        if o == "not_sent":             # submit_feed 对取 token / 代理失败的真实形状
            o, extra = "failed", {"retryable": True}
        return [{"feed_id": None if o == "failed" else f"F{len(sent)}",
                 "count": len(entries), "outcome": o, **extra}]

    monkeypatch.setattr(wf.feeds, "submit_feed", submit_feed)
    return seen, sent


def _list(monkeypatch, by_store):
    monkeypatch.setattr(wf, "read_list", lambda path, sheet="": (
        {k: list(v) for k, v in by_store.items()},
        {"rows": sum(len(v) for v in by_store.values()), "blank": 0, "dup": 0}))


def test_real_run_sends_one_maintenance_feed_per_store(monkeypatch):
    _list(monkeypatch, {"谭总2": ["A", "B", "GONE", "NOUPC"]})
    seen, sent = _wire(monkeypatch, ["谭总2"],
                       {"谭总2": {"A": "012345678905", "B": "0098765432109",
                                 "NOUPC": None}})
    out = wf.run({"file": "x.xlsx"})
    assert seen == [{"store": "谭总2", "skus": ["A", "B", "GONE", "NOUPC"]}]
    assert sent == [("谭总2", "MP_MAINTENANCE",
                     [wf.build_item("A", "012345678905", 11.0),
                      wf.build_item("B", "0098765432109", 11.0)],
                     "weight_fix")]
    first = out.splitlines()[0]
    assert first.startswith("改发货重量 → 11 磅:1 店,要改 2 条,已提交 2 条")
    assert "目录里没有或已缺席 1" in first and "没 UPC 1" in first
    assert "feed:F1" in out


def test_sql_only_reads_items_still_in_the_catalog():
    assert "missing_since IS NULL" in wf._SQL_ITEMS
    assert "%(store)s::text" in wf._SQL_ITEMS and "%(skus)s::text[]" in wf._SQL_ITEMS


def test_dry_run_sends_nothing(monkeypatch):
    _list(monkeypatch, {"谭总2": ["A"]})
    _, sent = _wire(monkeypatch, ["谭总2"], {"谭总2": {"A": "012345678905"}})
    out = wf.run({"file": "x.xlsx", "dry_run": True})
    assert sent == []
    assert out.startswith("[DRY-RUN] 改发货重量 → 11 磅:1 店,要改 1 条")
    assert "一条都没发" in out


def test_limit_and_store_filter_for_a_trial_run(monkeypatch):
    """试水:-p store=X -p limit=1 只发一条,截下的点名;别的店不碰。"""
    _list(monkeypatch, {"谭总2": ["A", "B", "C"], "谭总4": ["D"]})
    _, sent = _wire(monkeypatch, ["谭总2", "谭总4"],
                    {"谭总2": {"A": "1", "B": "2", "C": "3"}, "谭总4": {"D": "4"}})
    out = wf.run({"file": "x.xlsx", "store": "谭总2", "limit": "1"})
    assert [(s, [e["Orderable"]["sku"] for e in es]) for s, _, es, _ in sent] == \
        [("谭总2", ["A"])]
    assert "limit 截下 2" in out.splitlines()[0]


def test_custom_weight(monkeypatch):
    _list(monkeypatch, {"谭总2": ["A"]})
    _, sent = _wire(monkeypatch, ["谭总2"], {"谭总2": {"A": "1"}})
    out = wf.run({"file": "x.xlsx", "weight": "9.5"})
    assert sent[0][2][0]["Orderable"]["ShippingWeight"] == 9.5
    assert out.startswith("改发货重量 → 9.5 磅")


def test_uncallable_store_is_named_not_sent(monkeypatch):
    _list(monkeypatch, {"谭总2": ["A"], "停用店": ["X", "Y"]})
    _, sent = _wire(monkeypatch, ["谭总2"], {"谭总2": {"A": "1"}})
    out = wf.run({"file": "x.xlsx"})
    assert [s for s, *_ in sent] == ["谭总2"]
    assert "整店跳过 1 店 2 条:停用店" in out.splitlines()[0]


def test_dedup_and_unknown_outcomes_are_named(monkeypatch):
    _list(monkeypatch, {"谭总2": ["A"]})
    _wire(monkeypatch, ["谭总2"], {"谭总2": {"A": "1"}}, outcome="dedup")
    assert "防重拦下 1" in wf.run({"file": "x.xlsx"}).splitlines()[0]
    _wire(monkeypatch, ["谭总2"], {"谭总2": {"A": "1"}}, outcome="deferred")
    assert "结局不确定 1" in wf.run({"file": "x.xlsx"}).splitlines()[0]


def test_missing_file_param():
    assert wf.run({}).startswith("⚠ 缺名单")


def test_store_filter_with_no_rows(monkeypatch):
    _list(monkeypatch, {"谭总2": ["A"]})
    _wire(monkeypatch, ["谭总2"], {})
    assert wf.run({"file": "x.xlsx", "store": "谭总9"}) == "⚠ 名单里没有店铺 谭总9 的行"


def test_not_scheduled_and_dangerous():
    """临时工具:不进调度表;缺省即真跑(DANGEROUS)。"""
    from registry import schedule
    assert wf.DANGEROUS is True
    assert "weight_fix" not in Path(schedule.__file__).read_text(encoding="utf-8")


# ── 请求没发出(取 token / 代理阶段失败):补试一遍,照实点名 ───────────────────

def test_not_sent_is_retried_once_and_named_not_called_rejected(monkeypatch):
    """2026-10-16 生产实见(A085朱丽霖,代理 TLS 握手 EOF):请求根本没发出,
    摘要不能写成「被拒」;按店维标准串行补试一遍,仍失败就点名并说明可直接重跑。"""
    _list(monkeypatch, {"谭总2": ["A"]})
    _, sent = _wire(monkeypatch, ["谭总2"], {"谭总2": {"A": "1"}},
                    outcome=["not_sent", "not_sent"])
    out = wf.run({"file": "x.xlsx"})
    assert len(sent) == 2                                    # 第一轮 + 串行补试一次
    first = out.splitlines()[0]
    assert "失败 1 店(已串行补试一遍):谭总2(" in first
    assert "1 条请求没发出:取 token / 代理阶段失败" in first
    assert "被拒" not in out and "拒收" not in out


def test_not_sent_then_ok_on_retry(monkeypatch):
    _list(monkeypatch, {"谭总2": ["A"]})
    _, sent = _wire(monkeypatch, ["谭总2"], {"谭总2": {"A": "1"}},
                    outcome=["not_sent", "submitted"])
    out = wf.run({"file": "x.xlsx"})
    assert len(sent) == 2
    first = out.splitlines()[0]
    assert "已提交 1 条" in first and "失败" not in first


def test_walmart_rejection_is_not_retried(monkeypatch):
    """沃尔玛明确拒收(4xx)不补试:换姿势重试 = 重复提交制造机,原样再发也没用。"""
    _list(monkeypatch, {"谭总2": ["A"]})
    _, sent = _wire(monkeypatch, ["谭总2"], {"谭总2": {"A": "1"}}, outcome="failed")
    out = wf.run({"file": "x.xlsx"})
    assert len(sent) == 1
    assert "沃尔玛拒收 1" in out.splitlines()[0]
