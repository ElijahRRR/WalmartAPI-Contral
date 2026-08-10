"""order_audit 单测:规则引擎逐条钉死 + 工作流串接(全程不连飞书/PG/采集器)。

规则常量是钱的判定(限价 0.75、税费 1.08、配送时长 9 天),这里的断言
就是它们的回归护栏——改常量必然红,红了就得所有者确认。
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from services import order_audit as rules


# ══════════════════════════════════════════════════════════════════════════════
#  邮编标准化与钓鱼检测
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,want", [
    ("60606", "60606"),
    ("60606-6771", "60606"),      # zip+4 必须收敛到前 5 位
    (" 60606 ", "60606"),
    ("606066771", "60606"),
    (60606, "60606"),
    ("1234", ""),                 # 不足 5 位:取不出,不参与匹配
    (None, ""),
    ("", ""),
])
def test_norm_zip(raw, want):
    assert rules.norm_zip(raw) == want


def test_zip_blacklist_and_hit():
    bl = rules.zip_blacklist([["60606"], ["  90210-1234 "], [""], []])
    assert bl == {"60606", "90210"}
    assert rules.is_phishing("60606-9999", bl) is True
    assert rules.is_phishing("90210", bl) is True
    assert rules.is_phishing("10001", bl) is False
    assert rules.is_phishing(None, bl) is False


# ══════════════════════════════════════════════════════════════════════════════
#  采购方匹配
# ══════════════════════════════════════════════════════════════════════════════

FIELDS = SimpleNamespace(supplier="采购方", ship_method="配送方式",
                         band_from="价格区间起", band_to="价格区间止",
                         rate="汇率", enabled="是否启用")


def _rec(**kw):
    return {"fields": {FIELDS.supplier: kw.get("name", "甲"),
                       FIELDS.ship_method: kw.get("m", "FBA"),
                       FIELDS.band_from: kw.get("lo", 0),
                       FIELDS.band_to: kw.get("hi", 100),
                       FIELDS.rate: kw.get("rate", 1.0),
                       FIELDS.enabled: kw.get("on", "是")}}


def test_parse_suppliers_filters():
    recs = [
        _rec(name="甲", on="是"),
        _rec(name="乙", on="否"),          # 未启用
        _rec(name="", on=True),            # 无名
        _rec(name="丁", rate=None),        # 无汇率
        _rec(name="戊", lo=200, hi=100),   # 区间非法
    ]
    out = rules.parse_suppliers(recs, FIELDS)
    assert [s.name for s in out] == ["甲"]


def test_parse_suppliers_open_band():
    """区间只填一端 = 以上/以下,不能整行丢掉。"""
    out = rules.parse_suppliers([_rec(name="甲", lo=50, hi="")], FIELDS)
    assert out[0].band_from == 50 and out[0].band_to == float("inf")
    assert rules.pick_supplier(out, "FBA", 9999).name == "甲"


def test_parse_suppliers_checkbox_enabled():
    """「是否启用」是复选框时值是 bool,不是文本。"""
    assert rules.parse_suppliers([_rec(on=True)], FIELDS)
    assert not rules.parse_suppliers([_rec(on=False)], FIELDS)


def test_pick_supplier_by_method_and_band():
    sup = rules.parse_suppliers([
        _rec(name="甲", m="FBA", lo=0, hi=50, rate=1.10),
        _rec(name="乙", m="FBM", lo=0, hi=50, rate=1.01),
        _rec(name="丙", m="FBA", lo=50, hi=100, rate=1.20),
    ], FIELDS)
    assert rules.pick_supplier(sup, "fba", 30).name == "甲"      # 大小写无关
    assert rules.pick_supplier(sup, "FBM", 30).name == "乙"
    assert rules.pick_supplier(sup, "FBA", 50).name == "甲"      # 闭区间,边界归先命中者
    assert rules.pick_supplier(sup, "FBA", 80).name == "丙"
    assert rules.pick_supplier(sup, "FBA", 500) is None
    assert rules.pick_supplier(sup, "", 30) is None


def test_pick_supplier_lowest_rate_wins():
    """多候选取最低汇率(旧系统语义,逐字保留)。"""
    sup = rules.parse_suppliers([
        _rec(name="贵", m="FBA", lo=0, hi=100, rate=1.30),
        _rec(name="便宜", m="FBA", lo=0, hi=100, rate=1.05),
    ], FIELDS)
    assert rules.pick_supplier(sup, "FBA", 30).name == "便宜"


# ══════════════════════════════════════════════════════════════════════════════
#  限价
# ══════════════════════════════════════════════════════════════════════════════

def test_price_cap():
    assert rules.price_cap(100) == 75.0
    assert rules.price_cap("39.99") == 29.99
    assert rules.price_cap(None) is None


def test_purchase_cost_formula():
    """(单价 × 数量 × 汇率 + 运费) × 1.08;运费不乘汇率。"""
    assert rules.purchase_cost(10, 2, 1.0, 5) == round((10 * 2 * 1.0 + 5) * 1.08, 2)
    assert rules.purchase_cost(10, 2, 1.0, 0) == 21.6       # 0 = 确认免运费
    assert rules.purchase_cost(None, 2, 1.0, 0) is None
    assert rules.purchase_cost(10, 2, None, 0) is None


def test_purchase_cost_refuses_missing_shipping():
    """运费没采到(None)≠ 免运费(0):算不出成本,绝不当 0 蒙混过关。

    当 0 的话成本照样算得出来、看着正常,只是偏小 —— 本该拒的单被放行,
    而两侧都不会报错。这是采集契约不变量 3b 在本项目的落点。
    """
    assert rules.purchase_cost(10, 2, 1.0, None) is None


def test_price_ok_boundary():
    assert rules.price_ok(75.0, 75.0) is True               # 相等算通过
    assert rules.price_ok(75.01, 75.0) is False
    assert rules.price_ok(None, 75.0) is False


# ══════════════════════════════════════════════════════════════════════════════
#  决策链
# ══════════════════════════════════════════════════════════════════════════════

LINE = {"order_line_id": "PO1|SKU1", "sku": "B001", "qty": 1,
        "product_amount": 100, "postal_code": "10001",
        "product_name": "Acme Widget Pro 12 inch Blue"}


def _snap(**kw):
    base = {"asin": "B001", "zip": "10001", "amz_price": 50, "shipping": 0,
            "stock_qty": 10, "ship_method": "FBA", "ship_days": 3,
            "seller": "Amazon.com", "screenshot_url": None, "outcome": "ok",
            "amz_title": "Acme Widget Pro 12 inch Blue",
            "scraped_at": "2026-08-09T00:00:00Z"}
    base.update(kw)
    return base


SUPPLIERS = rules.parse_suppliers([_rec(name="甲", m="FBA", lo=0, hi=1000,
                                        rate=1.0)], FIELDS)


def test_judge_pass():
    res = rules.judge(LINE, _snap(), SUPPLIERS, set())
    assert res.status == rules.PASS
    assert res.detail["price_cap"] == 75.0
    assert res.detail["cost"] == 54.0        # 50×1×1.0×1.08
    assert res.detail["supplier"] == "甲"


def test_judge_phishing_wins_over_everything():
    """钓鱼是终局:不看采集、不算限价,连快照都不需要。"""
    res = rules.judge(LINE, None, SUPPLIERS, {"10001"})
    assert res.status == rules.REJECT
    assert rules.PHISHING_MARK in res.note
    assert res.is_phishing is True
    assert res.detail["rules"]["phishing"]["hit"] is True


def test_judge_no_snapshot_is_manual_not_pass():
    res = rules.judge(LINE, None, SUPPLIERS, set())
    assert res.status == rules.MANUAL
    assert "待采集" in res.note


def test_judge_bad_outcome_is_manual():
    res = rules.judge(LINE, _snap(outcome="blocked"), SUPPLIERS, set())
    assert res.status == rules.MANUAL
    assert "blocked" in res.note


def test_judge_missing_fields_is_manual():
    res = rules.judge(LINE, _snap(ship_days=None), SUPPLIERS, set())
    assert res.status == rules.MANUAL
    assert "配送时长" in res.note


def test_judge_delivery_days_gate():
    """≥9 天建议拒绝;8 天放行(闸口值改动必须让本用例红)。"""
    assert rules.judge(LINE, _snap(ship_days=9), SUPPLIERS, set()).status == rules.REJECT
    assert rules.judge(LINE, _snap(ship_days=8), SUPPLIERS, set()).status == rules.PASS


def test_judge_delivery_zero_days_is_not_missing():
    """0 天是"确实是 0",不能当没采到(null-0 铁律)。"""
    assert rules.judge(LINE, _snap(ship_days=0), SUPPLIERS, set()).status == rules.PASS


def test_judge_no_supplier_is_manual():
    res = rules.judge(LINE, _snap(ship_method="FBM"), SUPPLIERS, set())
    assert res.status == rules.MANUAL
    assert "无匹配采购方" in res.note


def test_judge_price_cap_reject():
    # 单价 80 → 成本 86.4 > 限价 75
    res = rules.judge(LINE, _snap(amz_price=80), SUPPLIERS, set())
    assert res.status == rules.REJECT
    assert "限价不通过" in res.note
    assert res.detail["cost"] == 86.4


def test_judge_shipping_counts_into_cost():
    """运费进成本:含运费后越线即拒绝。"""
    assert rules.judge(LINE, _snap(amz_price=69), SUPPLIERS, set()).status == rules.PASS
    res = rules.judge(LINE, _snap(amz_price=69, shipping=5), SUPPLIERS, set())
    assert res.status == rules.REJECT


def test_judge_qty_multiplies_cost():
    """同一单价,数量翻倍即越线——数量必须进成本(限价不随数量放大)。"""
    assert rules.judge(LINE, _snap(amz_price=40), SUPPLIERS, set()).status == rules.PASS
    line = dict(LINE, qty=2)
    assert rules.judge(line, _snap(amz_price=40), SUPPLIERS, set()).status == rules.REJECT


# ══════════════════════════════════════════════════════════════════════════════
#  商品一致性(标题相似度)
# ══════════════════════════════════════════════════════════════════════════════

def test_title_similarity_normalizes():
    """大小写/标点/多余空白不该拉低相似度。"""
    assert rules.title_similarity("Acme Widget, 12-inch (Blue)",
                                  "acme  widget 12 inch blue") == 1.0


def test_title_similarity_none_when_missing():
    """None(算不了)与 0.0(都在但完全不像)是两回事。"""
    assert rules.title_similarity("", "Acme") is None
    assert rules.title_similarity("Acme", None) is None
    assert rules.title_similarity("aaaa", "zzzz") == 0.0


def test_judge_title_mismatch_is_manual():
    """标题差太远 = 疑似采错商品,拿它算的限价无意义 → 待人工。"""
    res = rules.judge(LINE, _snap(amz_title="Completely Different Thing"),
                      SUPPLIERS, set())
    assert res.status == rules.MANUAL
    assert "标题相似度" in res.note
    assert res.detail["title_similarity"] < rules.TITLE_SIMILARITY_MIN


def test_judge_title_similarity_recorded_on_pass():
    """通过的行也要留下相似度数值——「标题相似度」列要展示它。"""
    res = rules.judge(LINE, _snap(), SUPPLIERS, set())
    assert res.status == rules.PASS
    assert res.detail["title_similarity"] == 1.0


def test_judge_title_missing_is_manual():
    res = rules.judge(LINE, _snap(amz_title=None), SUPPLIERS, set())
    assert res.status == rules.MANUAL
    assert "标题取不到" in res.note


def test_judge_title_checked_before_price():
    """一致性排在限价前:标题不符时不该先蹦出限价结论。"""
    res = rules.judge(LINE, _snap(amz_price=999, amz_title="Different Thing"),
                      SUPPLIERS, set())
    assert res.status == rules.MANUAL and "限价" not in res.note


# ══════════════════════════════════════════════════════════════════════════════
#  按邮编采集的波次编排(2026-08-10 放宽:不再一个邮编一个批次)
# ══════════════════════════════════════════════════════════════════════════════

def test_plan_waves_mixes_zips_in_one_batch():
    """不同 ASIN 的不同邮编可以同批——采集侧切邮编性能已优化。"""
    waves = rules.plan_waves([("B1", "10001"), ("B2", "90210")], set())
    assert waves == [[("B1", "10001"), ("B2", "90210")]]


def test_plan_waves_splits_same_asin_across_batches():
    """同一 ASIN 的多个邮编必须拆批(采集侧 UNIQUE(batch_id, asin) 会 400),
    但**同一轮内全部推完**,不再跨轮等待。"""
    waves = rules.plan_waves([("B1", "10001"), ("B1", "90210"),
                              ("B2", "10001")], set())
    assert len(waves) == 2
    assert waves[0] == [("B1", "10001"), ("B2", "10001")]
    assert waves[1] == [("B1", "90210")]
    # 每一波内 ASIN 不重复 —— 这正是采集侧那条 400 的触发条件
    for w in waves:
        assert len({a for a, _ in w}) == len(w)


def test_plan_waves_skips_blocked():
    """在途/重试窗口耗尽的组合直接跳过,不占波次。"""
    waves = rules.plan_waves([("B1", "10001"), ("B1", "90210")],
                             {("B1", "10001")})
    assert waves == [[("B1", "90210")]]


def test_plan_waves_all_blocked_gives_nothing():
    assert rules.plan_waves([("B1", "10001")], {("B1", "10001")}) == []


def test_plan_waves_drops_incomplete_pairs():
    assert rules.plan_waves([("B1", ""), ("", "10001")], set()) == []


def test_plan_waves_is_deterministic():
    """波次划分必须可复现,否则排障时"上轮到底推了什么"说不清。"""
    pairs = [("B2", "90210"), ("B1", "90210"), ("B1", "10001")]
    assert rules.plan_waves(pairs, set()) == rules.plan_waves(
        list(reversed(pairs)), set())


# ══════════════════════════════════════════════════════════════════════════════
#  采集接入缝
# ══════════════════════════════════════════════════════════════════════════════

def test_from_snapshot_uses_contract_fields():
    """字段位置按契约 v1:邮编 scrape_params.zipcode、配送方式 raw.is_fba、
    卖家 buybox.buybox_seller —— 按名字猜会全取空。"""
    snap = rules.from_snapshot({
        "asin": "B001", "price": 12.5, "stock_count": 3, "delivery_days": 4,
        "buybox": {"buybox_seller": "Acme"}, "shipping": 2.5,
        "shipping_raw": "$2.50", "raw": {"is_fba": "FBA"},
        "scrape_params": {"zipcode": "10001-2222"}, "outcome": "ok",
        "title": " Acme Widget ", "scraped_at": "2026-08-09T00:00:00Z"})
    assert snap["zip"] == "10001"            # 快照侧邮编也走同一标准化
    assert snap["ship_method"] == "FBA"
    assert snap["seller"] == "Acme"
    assert snap["shipping"] == 2.5
    assert snap["shipping_raw"] == "$2.50"
    assert snap["amz_title"] == "Acme Widget"


def test_from_snapshot_discards_zip_mismatch():
    """zip_verify=mismatch:切邮编失败拿回的是默认地区价格,必须判废。"""
    snap = rules.from_snapshot({
        "asin": "B001", "price": 12.5, "buybox": {}, "raw": {},
        "scrape_params": {"zipcode": "10001", "zip_verify": "mismatch",
                          "zip_observed": "94105"}})
    assert snap["zip"] == ""                 # 匹配不上任何订单行 → 视同没采到


def test_from_snapshot_tolerates_missing_buybox():
    snap = rules.from_snapshot({"asin": "B001", "price": 1, "buybox": None,
                                "scrape_params": None})
    assert snap["ship_method"] is None and snap["zip"] == ""


def test_from_snapshot_none():
    assert rules.from_snapshot(None) is None


# ══════════════════════════════════════════════════════════════════════════════
#  工作流串接(假 PG / 假飞书)
# ══════════════════════════════════════════════════════════════════════════════

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.conn.executed.append((sql, args))
        for pattern, (cols, rows) in self.conn.responses.items():
            if pattern in sql:
                self.description = [(c,) for c in cols]
                self._rows = rows
                return
        self.description = []      # 未登记的查询一律"查不到",与真库空结果同形
        self._rows = []

    def executemany(self, sql, seq):
        self.conn.executed.append((sql, list(seq)))

    @property
    def rowcount(self):
        return len(self._rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, responses):
        self.responses = responses
        self.executed = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def wired(monkeypatch):
    """把 order_audit 的外部依赖(飞书四个调用 + PG)全换成假的。"""
    from workflows import order_audit as wf

    calls = {"updates": None, "uploaded": []}

    monkeypatch.setattr(wf.feishu, "sheet_row_count", lambda s: 10)
    monkeypatch.setattr(wf.feishu, "sheet_values", lambda s, r: [["60606"]])
    monkeypatch.setattr(wf.feishu, "list_records",
                        lambda t: [_rec(name="甲", m="FBA", lo=0, hi=1000, rate=1.0)])

    def fake_update(table, key, desired):
        calls["updates"] = desired
        return len(desired), []
    monkeypatch.setattr(wf.feishu, "update_by_key", fake_update)

    # 采集器一律不真连:记录每次提交的 (批次名, ASIN 列表, 邮编, 是否要截图)
    calls["batches"] = []

    def fake_submit(name, items, *, needs_screenshot=False):
        calls["batches"].append((name, list(items), needs_screenshot))
        return {"batch_id": "b1", "inserted": len(items)}
    monkeypatch.setattr(wf.scraper, "submit_items", fake_submit)

    # registry 未登记也能跑:require() 直接返回自身
    for res in (wf.resources.ZIP_BLACKLIST_SHEET, wf.resources.SUPPLIER_TABLE,
                wf.resources.ORDER_SALES_AUDIT):
        monkeypatch.setattr(type(res), "require", lambda self: self, raising=False)
    return wf, calls


PICK_COLS = ["order_line_id", "store", "sku", "product_name", "qty",
             "product_amount", "shipping_amount", "postal_code",
             "sale_status", "audit_status"]


def test_run_end_to_end_pass(wired, monkeypatch):
    wf, calls = wired
    conn = FakeConn({
        "ORDER BY order_date DESC": (      # 只有待审查询有,推送查询没有

            PICK_COLS, [("PO1|SKU1", "店A", "B001", "Acme Widget Pro 12 inch Blue",
                         1, 100, 0, "10001", "Shipped", None)]),
        "FROM catalog.latest_snapshot": (
            ["asin", "price", "stock_count", "delivery_days", "shipping",
             "shipping_raw", "buybox", "scrape_params", "raw", "outcome",
             "scraped_at", "title"],
            [("B001", 50, 5, 3, 0.0, "FREE", {"buybox_seller": "Acme"},
              {"zipcode": "10001"}, {"is_fba": "FBA"}, "ok", None,
              "Acme Widget Pro 12 inch Blue")]),
        "audit_status IS NOT NULL": (
            ["order_line_id", "audit_status", "audit_detail"],
            [("PO1|SKU1", rules.PASS,
              {"note": "ok", "amz_price": 50, "supplier": "甲",
               "price_cap": 75.0})]),
    })
    monkeypatch.setattr(wf.db, "pg_conn", lambda: conn)

    summary = wf.run({})
    assert rules.PASS in summary
    assert "飞书回写 1 行" in summary
    payload = calls["updates"]["PO1|SKU1"]
    f = wf.resources.ORDER_SALES_AUDIT.fields
    assert payload[f.audit_status] == rules.PASS
    assert payload[f.supplier] == "甲"
    assert payload[f.price_cap] == 75.0
    # 「建议采购日期」属人工域,载荷里绝不能出现
    assert "建议采购日期" not in payload


def test_run_skips_phishing_marked_rows(wired, monkeypatch):
    """已标钓鱼的行任何轮次都不再改写(旧系统不可覆盖语义)。"""
    wf, calls = wired
    conn = FakeConn({
        "ORDER BY order_date DESC": (      # 只有待审查询有,推送查询没有

            PICK_COLS, [("PO1|SKU1", "店A", "B001", "Acme Widget", 1, 100, 0,
                         "10001", "Shipped", f"{rules.REJECT}"),
                        ("PO2|SKU2", "店A", "B002", "Acme Widget", 1, 100, 0,
                         "10001", "Shipped",
                         f"{rules.PHISHING_MARK}邮编:10001")]),
        "audit_status IS NOT NULL": (["order_line_id", "audit_status",
                                      "audit_detail"], []),
    })
    monkeypatch.setattr(wf.db, "pg_conn", lambda: conn)
    summary = wf.run({"recheck": "1"})
    assert "待审 1 行" in summary          # 钓鱼那行被剔除,只剩一行进判定


def test_run_no_snapshot_reports_pending_scrape(wired, monkeypatch):
    wf, _ = wired
    conn = FakeConn({
        "ORDER BY order_date DESC": (      # 只有待审查询有,推送查询没有

            PICK_COLS, [("PO1|SKU1", "店A", "B001", "Acme Widget Pro 12 inch Blue",
                         1, 100, 0, "10001", "Shipped", None)]),
        "audit_status IS NOT NULL": (["order_line_id", "audit_status",
                                      "audit_detail"], []),
    })
    monkeypatch.setattr(wf.db, "pg_conn", lambda: conn)
    summary = wf.run({})
    assert "待采集 1 行" in summary
    assert rules.MANUAL in summary


def test_run_requeues_manual_rows(wired, monkeypatch):
    """「待人工」每轮都要重判——否则采集回来了这行永远不会被再看一眼。"""
    wf, _ = wired
    conn = FakeConn({"ORDER BY order_date DESC": (PICK_COLS, [])})
    monkeypatch.setattr(wf.db, "pg_conn", lambda: conn)
    wf.run({})
    pick_sql = next(s for s, _ in conn.executed if "ORDER BY order_date DESC" in s)
    assert "audit_status IS NULL OR audit_status = %(manual)s" in pick_sql
    # 终局结论不重判(除非 recheck)
    wf.run({"recheck": "1"})
    recheck_sql = [s for s, _ in conn.executed if "ORDER BY order_date DESC" in s][-1]
    assert "audit_status IS NULL" not in recheck_sql


def test_run_pushes_scrape_for_missing_snapshot(wired, monkeypatch):
    """缺快照的行 → 按收件邮编推采集,且**先落 pending 再调接口**。"""
    wf, calls = wired
    conn = FakeConn({
        "ORDER BY order_date DESC": (
            PICK_COLS, [("PO1|SKU1", "店A", "B001", "Acme Widget", 1, 100, 0,
                         "10001-2222", "Shipped", None)]),
        "audit_status IS NOT NULL": (["order_line_id", "audit_status",
                                      "audit_detail"], []),
    })
    monkeypatch.setattr(wf.db, "pg_conn", lambda: conn)
    summary = wf.run({})

    assert len(calls["batches"]) == 1
    name, items, shot = calls["batches"][0]
    assert items == [{"asin": "B001", "zip_code": "10001"}]   # zip+4 收敛后再推
    assert shot is True                 # 审核要截图做佐证
    assert "推采集:1 个" in summary

    # 提交前必须已经写过 pending(顺序反了就会"推上去了但库里没记录")
    sqls = [s for s, _ in conn.executed]
    pending_at = next(i for i, s in enumerate(sqls)
                      if "INSERT INTO ops.audit_scrape" in s)
    marked = conn.executed[pending_at][1]
    assert marked[0]["asin"] == "B001" and marked[0]["zip"] == "10001"
    assert marked[0]["batch"] == name
    # 台账对账必须发生在写 pending 之前(重启安全的前提)
    assert any("state = 'done'" in s for s in sqls[:pending_at])


def test_run_scrape_can_be_disabled(wired, monkeypatch):
    wf, calls = wired
    conn = FakeConn({
        "ORDER BY order_date DESC": (
            PICK_COLS, [("PO1|SKU1", "店A", "B001", "Acme Widget", 1, 100, 0,
                         "10001", "Shipped", None)]),
        "audit_status IS NOT NULL": (["order_line_id", "audit_status",
                                      "audit_detail"], []),
    })
    monkeypatch.setattr(wf.db, "pg_conn", lambda: conn)
    wf.run({"scrape": "0"})
    assert calls["batches"] == []


def test_screenshot_pending_writes_nothing_and_leaves_no_tombstone(wired,
                                                                   monkeypatch):
    """409 未就绪:本轮不写这一列,也不记墓碑——下轮还要再来取。"""
    wf, _ = wired
    conn = FakeConn({})
    calls = []
    monkeypatch.setattr(wf.scraper, "fetch_screenshot",
                        lambda b, a: (_ for _ in ()).throw(
                            wf.scraper.ScreenshotPending("still working")))
    monkeypatch.setattr(wf, "_remember", lambda *a: calls.append(a))
    assert wf._screenshot_token(conn, "wm-audit-10001-x", "B001") is None
    assert calls == []


def test_screenshot_gone_writes_tombstone(wired, monkeypatch):
    """404/410 不会再有:记墓碑,以后不再为这张图发请求。"""
    wf, _ = wired
    conn = FakeConn({})
    remembered = {}
    monkeypatch.setattr(wf.scraper, "fetch_screenshot",
                        lambda b, a: (_ for _ in ()).throw(
                            wf.scraper.ScreenshotGone("captcha")))
    monkeypatch.setattr(wf, "_remember",
                        lambda c, k, m: remembered.update({k: m}))
    assert wf._screenshot_token(conn, "wm-audit-10001-x", "B001") is None
    assert remembered["wm-audit-10001-x|B001"]["gone"] is True


def test_screenshot_uploads_and_dedupes(wired, monkeypatch):
    """200:上传换 file_token 并记账;已记账的不再上传(上传接口不幂等)。"""
    wf, _ = wired
    uploads = []
    monkeypatch.setattr(wf.scraper, "fetch_screenshot", lambda b, a: b"\x89PNG")
    monkeypatch.setattr(wf.feishu, "upload_media",
                        lambda t, name, content, mime="image/jpeg":
                        uploads.append((name, mime)) or "ft_1")
    monkeypatch.setattr(wf, "_remember", lambda *a: None)
    conn = FakeConn({})
    assert wf._screenshot_token(conn, "wm-audit-10001-x", "B001") == "ft_1"
    assert uploads == [("B001.png", "image/png")]

    # 账上已有 → 直接复用,不再取图也不再上传
    hit = FakeConn({"FROM ops.dedupe": (["file_token", "gone"],
                                        [("ft_cached", None)])})
    assert wf._screenshot_token(hit, "wm-audit-10001-x", "B001") == "ft_cached"
    assert len(uploads) == 1


@pytest.mark.parametrize("snap_kw,why", [
    ({"outcome": "blocked"}, "采集失败"),
    ({"ship_days": None}, "缺配送时长"),
    ({"ship_method": None}, "缺配送方式"),
    ({"shipping": None}, "运费没采到"),
])
def test_unusable_snapshot_is_marked_for_rescrape(snap_kw, why):
    """有快照但关键信息缺失 → 标记重采(所有者定稿 2026-08-10)。

    这四种的共同点是"这条数据没法用",而重采正是解药;此前它们因为
    `snap is not None` 进不了待采清单,只能干等维护链全量重推顺带刷新。
    """
    res = rules.judge(LINE, _snap(**snap_kw), SUPPLIERS, set())
    assert res.status == rules.MANUAL
    assert res.rescrape is True, why


@pytest.mark.parametrize("snap_kw", [
    {"ship_method": "FBM"},                        # 无匹配采购方:配置问题
    {"amz_title": "Completely Different Thing"},   # 标题不符:重采还是同一个
])
def test_non_data_problems_are_not_rescraped(snap_kw):
    """重采解决不了的,别进待采清单——那只是每小时白烧一次采集配额。"""
    res = rules.judge(LINE, _snap(**snap_kw), SUPPLIERS, set())
    assert res.status == rules.MANUAL and res.rescrape is False


def test_phishing_is_never_rescraped():
    """钓鱼是终局,连采都不用采。"""
    res = rules.judge(LINE, None, SUPPLIERS, {"10001"})
    assert res.rescrape is False


def test_pass_is_never_rescraped():
    assert rules.judge(LINE, _snap(), SUPPLIERS, set()).rescrape is False


def test_snapshot_query_gates_on_freshness():
    """超 24 小时的快照视同没有——审单看的价格/库存/货期变得快。"""
    from workflows import order_audit as wf
    assert "s.scraped_at >= now() - make_interval(hours => %(fresh)s)" in wf._SNAP_SQL
    assert wf._SNAPSHOT_FRESH_HOURS == 24


def test_snapshots_picks_newest_when_params_differ(wired, monkeypatch):
    """同一 (ASIN,邮编) 有多组 scrape_params(zip_observed/parse_engine 不同)
    时必须取最新那条,不能让字典后写覆盖先写(那等于随机挑,无法复现)。"""
    wf, _ = wired
    cols = ["asin", "price", "stock_count", "delivery_days", "shipping",
            "shipping_raw", "buybox", "scrape_params", "raw", "outcome",
            "scraped_at", "title"]
    old = ("B001", 10, 1, 1, 0.0, "FREE", {}, {"zipcode": "10001",
           "parse_engine": "lxml"}, {"is_fba": "FBA"}, "ok",
           datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc), "T")
    new = ("B001", 99, 1, 1, 0.0, "FREE", {}, {"zipcode": "10001",
           "parse_engine": "selectolax"}, {"is_fba": "FBA"}, "ok",
           datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc), "T")
    for order in ([old, new], [new, old]):        # 两种返回顺序结果必须一致
        conn = FakeConn({"FROM catalog.latest_snapshot": (cols, order)})
        snaps = wf._snapshots(conn, [{"sku": "B001"}])
        assert snaps[("B001", "10001")]["amz_price"] == 99


def test_sql_selects_every_column_the_rules_read():
    """守卫:判定用到的列,取数 SQL 必须真的选出来。

    这条是补票——`product_name` 曾漏在待审查询之外,而单测的假游标按夹具
    列名喂数据,漏列一路绿灯;上线后每一行都会因"标题取不到"变待人工。
    假数据永远盖不住真 SQL,只能直接断言 SQL 文本。
    """
    from workflows import order_audit as wf
    for sql in (wf._PICK_SQL, wf._ONE_SQL):
        for col in ("order_line_id", "sku", "product_name", "qty",
                    "product_amount", "postal_code", "audit_status"):
            assert col in sql, f"待审查询漏了 {col}"
    for col in ("price", "stock_count", "delivery_days", "shipping",
                "shipping_raw", "buybox", "scrape_params", "raw",
                "outcome", "title"):
        assert col in wf._SNAP_SQL, f"快照查询漏了 {col}"


def test_run_config_missing_suppliers_refuses(wired, monkeypatch):
    """采购方表一行都没启用 → 直接失败,不出结论(不拿旧配置算钱)。"""
    wf, _ = wired
    monkeypatch.setattr(wf.feishu, "list_records", lambda t: [])
    with pytest.raises(RuntimeError, match="采购方"):
        wf.run({})


def test_save_writes_detail_json(wired, monkeypatch):
    wf, _ = wired
    conn = FakeConn({})
    res = rules.judge(LINE, _snap(), SUPPLIERS, set())
    assert wf._save(conn, [(LINE, res)]) == 1
    sql, payload = conn.executed[-1]
    assert "UPDATE orders.order_lines" in sql
    status, detail_json, key = payload[0]
    assert status == rules.PASS and key == "PO1|SKU1"
    assert json.loads(detail_json)["note"] == res.note
