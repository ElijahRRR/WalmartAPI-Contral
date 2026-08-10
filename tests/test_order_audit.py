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

LINE = {"order_line_id": "PO1|SKU1", "sku": "B0TEST0001", "qty": 1,
        "product_amount": 100, "postal_code": "10001",
        "product_name": "Acme Widget Pro 12 inch Blue"}


def _snap(**kw):
    base = {"asin": "B0TEST0001", "zip": "10001", "amz_price": 50, "shipping": 0,
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
#  按邮编分批(2026-08-10 定稿:一个邮编一个批次,批次名带邮编)
# ══════════════════════════════════════════════════════════════════════════════

def test_plan_zip_batches_groups_by_zip():
    """一个邮编一个批次——批次名是取数与取图唯一的邮编隔离键。"""
    plan = rules.plan_zip_batches(
        [("B1", "10001"), ("B2", "90210"), ("B2", "10001")], set())
    assert plan == {"10001": ["B1", "B2"], "90210": ["B2"]}


def test_plan_zip_batches_same_asin_lands_in_different_batches():
    """同一 ASIN 的两个邮编分到两个批次:混在一批里采集侧只存一行,
    两个邮编会拿到完全相同的数据且不报错(而且截图也只会有一张)。"""
    plan = rules.plan_zip_batches([("B1", "10001"), ("B1", "90210")], set())
    assert plan == {"10001": ["B1"], "90210": ["B1"]}
    # 任何一批内 ASIN 都不重复 —— 采集侧 UNIQUE(batch_id, asin) 的触发条件
    for asins in plan.values():
        assert len(set(asins)) == len(asins)


def test_plan_zip_batches_skips_blocked():
    """在途/重试窗口耗尽的组合直接跳过,不占批次。"""
    plan = rules.plan_zip_batches([("B1", "10001"), ("B1", "90210")],
                                  {("B1", "10001")})
    assert plan == {"90210": ["B1"]}


def test_plan_zip_batches_all_blocked_gives_nothing():
    assert rules.plan_zip_batches([("B1", "10001")], {("B1", "10001")}) == {}


def test_plan_zip_batches_drops_incomplete_pairs():
    assert rules.plan_zip_batches([("B1", ""), ("", "10001")], set()) == {}


def test_plan_zip_batches_is_deterministic():
    """分批必须可复现,否则排障时"上轮到底推了什么"说不清。"""
    pairs = [("B2", "90210"), ("B1", "90210"), ("B1", "10001")]
    assert rules.plan_zip_batches(pairs, set()) == rules.plan_zip_batches(
        list(reversed(pairs)), set())


# ══════════════════════════════════════════════════════════════════════════════
#  采集接入缝
# ══════════════════════════════════════════════════════════════════════════════

def test_from_snapshot_uses_contract_fields():
    """字段位置按契约 v1:邮编 scrape_params.zipcode、配送方式 raw.is_fba、
    卖家 buybox.buybox_seller —— 按名字猜会全取空。"""
    snap = rules.from_snapshot({
        "asin": "B0TEST0001", "price": 12.5, "stock_count": 3, "delivery_days": 4,
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
        "asin": "B0TEST0001", "price": 12.5, "buybox": {}, "raw": {},
        "scrape_params": {"zipcode": "10001", "zip_verify": "mismatch",
                          "zip_observed": "94105"}})
    assert snap["zip"] == ""                 # 匹配不上任何订单行 → 视同没采到


def test_from_snapshot_tolerates_missing_buybox():
    snap = rules.from_snapshot({"asin": "B0TEST0001", "price": 1, "buybox": None,
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

    def fake_submit(name, asins, *, zip_code, needs_screenshot=False):
        calls["batches"].append((name, list(asins), zip_code, needs_screenshot))
        return {"batch_id": "b1", "inserted": len(asins)}
    monkeypatch.setattr(wf.scraper, "submit_json", fake_submit)

    # 批次台账走 services.scrape_batches(自己开连接),测试里不落库
    monkeypatch.setattr(wf.batches, "record",
                        lambda *a, **k: calls.setdefault("recorded", []).append(a))
    monkeypatch.setattr(wf.batches, "finish", lambda *a, **k: None)
    # 截图清单默认"这批还没有图":要测取图的用例各自覆盖
    monkeypatch.setattr(wf.scraper, "screenshot_list", lambda name: [])

    # registry 未登记也能跑:require() 直接返回自身
    for res in (wf.resources.ZIP_BLACKLIST_SHEET, wf.resources.SUPPLIER_TABLE,
                wf.resources.ORDER_SALES_AUDIT):
        monkeypatch.setattr(type(res), "require", lambda self: self, raising=False)
    return wf, calls


PICK_COLS = ["order_line_id", "store", "sku", "product_name", "qty",
             "product_amount", "shipping_amount", "postal_code",
             "sale_status", "audit_status", "audit_detail"]


def test_run_end_to_end_pass(wired, monkeypatch):
    wf, calls = wired
    conn = FakeConn({
        "ORDER BY order_date DESC": (      # 只有待审查询有,推送查询没有

            PICK_COLS, [("PO1|SKU1", "店A", "B0TEST0001", "Acme Widget Pro 12 inch Blue",
                         1, 100, 0, "10001", "Shipped", None, None)]),
        "FROM catalog.latest_snapshot": (
            ["asin", "price", "stock_count", "delivery_days", "shipping",
             "shipping_raw", "buybox", "scrape_params", "raw", "outcome",
             "scraped_at", "title"],
            [("B0TEST0001", 50, 5, 3, 0.0, "FREE", {"buybox_seller": "Acme"},
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
    """已标钓鱼的行任何轮次都不再改写(旧系统不可覆盖语义)。

    ⚠ 标记在 **audit_detail.note**,不在 audit_status——status 只会是
    「✓ 通过/建议拒绝/待人工」三值之一,钓鱼行就是「建议拒绝」。本用例
    喂的是**真实形状**:之前那版喂了个 judge 永远不会产出的 status 值,
    于是这道闸整个失效了却一路绿灯。
    """
    wf, calls = wired
    conn = FakeConn({
        "ORDER BY order_date DESC": (
            PICK_COLS, [
                # 普通拒绝行:recheck 时照样重判
                ("PO1|SKU1", "店A", "B0TEST0001", "Acme Widget", 1, 100, 0,
                 "10001", "Shipped", rules.REJECT,
                 {"note": "限价不通过:成本 90 > 限价 75"}),
                # 钓鱼行:status 同样是「建议拒绝」,区别只在 note
                ("PO2|SKU2", "店A", "B0TEST0002", "Acme Widget", 1, 100, 0,
                 "10001", "Shipped", rules.REJECT,
                 {"note": f"{rules.PHISHING_MARK}邮编:10001"}),
            ]),
        "audit_status IS NOT NULL": (["order_line_id", "audit_status",
                                      "audit_detail"], []),
    })
    monkeypatch.setattr(wf.db, "pg_conn", lambda: conn)
    summary = wf.run({"recheck": "1"})
    assert "待审 1 行" in summary          # 钓鱼那行被剔除,只剩一行进判定


def test_marked_phishing_reads_note_not_status():
    """直接钉住判据本身:光看 status 是看不出钓鱼的。"""
    from workflows import order_audit as wf
    phishing = {"audit_status": rules.REJECT,
                "audit_detail": {"note": f"{rules.PHISHING_MARK}邮编:10001"}}
    normal = {"audit_status": rules.REJECT,
              "audit_detail": {"note": "限价不通过:成本 90 > 限价 75"}}
    assert wf._marked_phishing(phishing) is True
    assert wf._marked_phishing(normal) is False
    # detail 以 JSON 串形式回来(psycopg 配置不同可能如此)也要认得出
    assert wf._marked_phishing(
        {"audit_status": rules.REJECT,
         "audit_detail": json.dumps(phishing["audit_detail"],
                                    ensure_ascii=False)}) is True
    # 脏 detail 不能让整轮炸掉
    assert wf._marked_phishing({"audit_detail": "{坏 JSON"}) is False


def test_run_no_snapshot_reports_pending_scrape(wired, monkeypatch):
    wf, _ = wired
    conn = FakeConn({
        "ORDER BY order_date DESC": (      # 只有待审查询有,推送查询没有

            PICK_COLS, [("PO1|SKU1", "店A", "B0TEST0001", "Acme Widget Pro 12 inch Blue",
                         1, 100, 0, "10001", "Shipped", None, None)]),
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
            PICK_COLS, [("PO1|SKU1", "店A", "B0TEST0001", "Acme Widget",
                         1, 100, 0, "10001-2222", "Shipped", None, None)]),
        "audit_status IS NOT NULL": (["order_line_id", "audit_status",
                                      "audit_detail"], []),
    })
    monkeypatch.setattr(wf.db, "pg_conn", lambda: conn)
    summary = wf.run({})

    assert len(calls["batches"]) == 1
    name, asins, zip_code, shot = calls["batches"][0]
    assert asins == ["B0TEST0001"]
    assert zip_code == "10001"          # zip+4 收敛后再推
    assert "10001" in name              # 批次名带邮编:取数与取图的隔离键
    assert shot is True                 # 审核要截图做佐证
    assert "推采集:1 个" in summary

    # 提交前必须已经写过 pending(顺序反了就会"推上去了但库里没记录")
    sqls = [s for s, _ in conn.executed]
    pending_at = next(i for i, s in enumerate(sqls)
                      if "INSERT INTO ops.audit_scrape" in s)
    marked = conn.executed[pending_at][1]
    assert marked[0]["asin"] == "B0TEST0001" and marked[0]["zip"] == "10001"
    assert marked[0]["batch"] == name
    # 台账对账必须发生在写 pending 之前(重启安全的前提)
    assert any("state = 'done'" in s for s in sqls[:pending_at])


def test_run_scrape_can_be_disabled(wired, monkeypatch):
    wf, calls = wired
    conn = FakeConn({
        "ORDER BY order_date DESC": (
            PICK_COLS, [("PO1|SKU1", "店A", "B0TEST0001", "Acme Widget",
                         1, 100, 0, "10001", "Shipped", None, None)]),
        "audit_status IS NOT NULL": (["order_line_id", "audit_status",
                                      "audit_detail"], []),
    })
    monkeypatch.setattr(wf.db, "pg_conn", lambda: conn)
    wf.run({"scrape": "0"})
    assert calls["batches"] == []


def test_screenshot_not_ready_writes_nothing_and_leaves_no_tombstone(wired,
                                                                     monkeypatch):
    """清单里还没 done:本轮不写这一列,也不记墓碑——下轮还要再来取。
    连图都不该去取(那张图根本不存在,取只会撞 404)。"""
    wf, _ = wired
    conn = FakeConn({})
    calls, fetched = [], []
    monkeypatch.setattr(wf.scraper, "fetch_screenshot",
                        lambda b, a: fetched.append((b, a)) or b"x")
    monkeypatch.setattr(wf, "_remember", lambda *a: calls.append(a))
    for status in ("pending", "running", ""):
        assert wf._screenshot_token(conn, "wm-audit-10001-x", "B0TEST0001",
                                    status) is None
    assert calls == [] and fetched == []


def test_screenshot_unknown_status_is_retried_not_buried(wired, monkeypatch):
    """采集侧冒出没见过的状态 → 当"还没好"(下轮再问),**不记墓碑**。
    反过来把未知当终态,一次改名就永久放弃一批本来能拿到的图。"""
    wf, _ = wired
    calls = []
    monkeypatch.setattr(wf, "_remember", lambda *a: calls.append(a))
    assert wf._screenshot_token(FakeConn({}), "wm-audit-10001-x", "B0TEST0001",
                               "queued_v2") is None
    assert calls == []


def test_screenshot_failed_status_writes_tombstone(wired, monkeypatch):
    """终态失败:记墓碑,以后不再为这张图发请求。"""
    wf, _ = wired
    remembered = {}
    monkeypatch.setattr(wf, "_remember",
                        lambda c, k, m: remembered.update({k: m}))
    assert wf._screenshot_token(FakeConn({}), "wm-audit-10001-x",
                               "B0TEST0001", "failed") is None
    assert remembered["wm-audit-10001-x|B0TEST0001"]["gone"] is True


def test_screenshot_gone_on_fetch_writes_tombstone(wired, monkeypatch):
    """清单说 done 但取图返 404/410(图被清理了):同样记墓碑。"""
    wf, _ = wired
    remembered = {}
    monkeypatch.setattr(wf.scraper, "fetch_screenshot",
                        lambda b, a: (_ for _ in ()).throw(
                            wf.scraper.ScreenshotGone("captcha")))
    monkeypatch.setattr(wf, "_remember",
                        lambda c, k, m: remembered.update({k: m}))
    assert wf._screenshot_token(FakeConn({}), "wm-audit-10001-x",
                               "B0TEST0001", "done") is None
    assert remembered["wm-audit-10001-x|B0TEST0001"]["gone"] is True


def test_screenshot_uploads_and_dedupes(wired, monkeypatch):
    """done:上传换 file_token 并记账;已记账的不再上传(上传接口不幂等)。"""
    wf, _ = wired
    uploads = []
    monkeypatch.setattr(wf.scraper, "fetch_screenshot", lambda b, a: b"\x89PNG")
    monkeypatch.setattr(wf.feishu, "upload_media",
                        lambda t, name, content, mime="image/jpeg":
                        uploads.append((name, mime)) or "ft_1")
    monkeypatch.setattr(wf, "_remember", lambda *a: None)
    conn = FakeConn({})
    assert wf._screenshot_token(conn, "wm-audit-10001-x", "B0TEST0001",
                               "done") == "ft_1"
    assert uploads == [("B0TEST0001.png", "image/png")]

    # 账上已有 → 直接复用,不再取图也不再上传
    hit = FakeConn({"FROM ops.dedupe": (["file_token", "gone"],
                                        [("ft_cached", None)])})
    assert wf._screenshot_token(hit, "wm-audit-10001-x", "B0TEST0001",
                               "done") == "ft_cached"
    assert len(uploads) == 1


def test_shot_index_keys_by_batch_and_asin(wired, monkeypatch):
    """按批次一次拿清单(不逐 ASIN 试探),键含批次名——同一 ASIN 的两个邮编
    批次各有各的图,少了批次名就会互相顶掉。"""
    wf, _ = wired
    asked = []

    def fake_list(name):
        asked.append(name)
        return [{"asin": "b0test0001", "status": "DONE"},
                {"asin": "B0TEST0002", "status": "pending"},
                {"asin": "", "status": "done"}]        # 无 asin:丢掉
    monkeypatch.setattr(wf.scraper, "screenshot_list", fake_list)
    idx = wf._shot_index({"wm-audit-10001-x", "wm-audit-90210-x", None})
    assert asked == ["wm-audit-10001-x", "wm-audit-90210-x"]
    assert idx[("wm-audit-10001-x", "B0TEST0001")] == "done"    # 大小写归一
    assert idx[("wm-audit-90210-x", "B0TEST0002")] == "pending"
    assert len(idx) == 4


def test_settled_batches_skips_inflight(wired, monkeypatch):
    """在途批次不去问截图:一轮上百个按邮编的批次,少了这道过滤就是每轮
    多发上百个必然空手而归的清单请求。"""
    wf, _ = wired
    conn = FakeConn({"FROM ops.scrape_batches": (["batch_name"],
                                                 [("wm-audit-10001-x",)])})
    assert wf._settled_batches(conn, {"wm-audit-10001-x",
                                      "wm-audit-90210-y", None}) == {
        "wm-audit-10001-x"}
    sql, args = conn.executed[0]
    assert "NOT IN ('pushed', 'running')" in sql
    assert None not in args["names"]
    # 一个批次名都没有时不该白发一次查询
    empty = FakeConn({})
    assert wf._settled_batches(empty, {None}) == set()
    assert empty.executed == []


def test_shot_index_survives_scraper_outage(wired, monkeypatch):
    """清单查不到只当"这批本轮没有图"——截图是佐证材料,永不阻断审核结论。"""
    wf, _ = wired
    monkeypatch.setattr(wf.scraper, "screenshot_list",
                        lambda n: (_ for _ in ()).throw(RuntimeError("502")))
    assert wf._shot_index({"wm-audit-10001-x"}) == {}


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
    old = ("B0TEST0001", 10, 1, 1, 0.0, "FREE", {}, {"zipcode": "10001",
           "parse_engine": "lxml"}, {"is_fba": "FBA"}, "ok",
           datetime(2026, 8, 9, 1, 0, tzinfo=timezone.utc), "T")
    new = ("B0TEST0001", 99, 1, 1, 0.0, "FREE", {}, {"zipcode": "10001",
           "parse_engine": "selectolax"}, {"is_fba": "FBA"}, "ok",
           datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc), "T")
    for order in ([old, new], [new, old]):        # 两种返回顺序结果必须一致
        conn = FakeConn({"FROM catalog.latest_snapshot": (cols, order)})
        snaps = wf._snapshots(conn, [{"sku": "B0TEST0001"}])
        assert snaps[("B0TEST0001", "10001")]["amz_price"] == 99


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


# ══════════════════════════════════════════════════════════════════════════════
#  批次生命周期(完成度判据 = tasks.open == 0 且 screenshots.open == 0)
# ══════════════════════════════════════════════════════════════════════════════

def _reap_conn(pending=(("B0A", "10001"), ("B0B", "10001"))):
    return FakeConn({
        "FROM ops.scrape_batches": (["batch_name", "batch_id", "asin_count"],
                                    [("wm-audit-10001-x", "7", 2)]),
        "state = 'pending' AND batch_name": (["asin", "zip"], list(pending)),
    })


def test_reap_batches_blames_the_real_reason(wired, monkeypatch):
    """批次采完了、这些组合却没快照 ⇒ 真失败,原因去 /failures 拿真值。

    一律记"超时未见快照"是不行的:验证码(换时段可重试)和 variant_offset
    (重试多少次都一样,该去人工看)的处置完全不同。
    """
    wf, _ = wired
    conn = _reap_conn()
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: {"stats": {"open": 0, "done": 1, "failed": 1},
                                   "screenshots": {"open": 0, "done": 1}})
    monkeypatch.setattr(wf.batches, "pull_failures",
                        lambda n, bid: ("失败明细:1 个 ASIN 已落库(captcha×1)",
                                        {"B0A": "captcha"}))
    reaped, failed_pairs, notes = wf._reap_batches(conn)
    assert (reaped, failed_pairs) == (1, 2)
    reasons = [a["reason"] for s, a in conn.executed
               if isinstance(a, dict) and "reason" in a]
    assert "采集失败:captcha" in reasons          # 有真实原因就写真实原因
    assert any("无快照" in r for r in reasons)     # 没原因的也得有个交代
    assert notes and "captcha" in notes[0]


def test_reap_batches_leaves_inflight_alone(wired, monkeypatch):
    """批次还在跑(open > 0)→ 不判失败、不重推,只更新台账状态。
    盲超时重推 = 采集侧正干着我们又推一遍,白烧一批配额。"""
    wf, calls = wired
    conn = _reap_conn()
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: {"stats": {"open": 2}, "screenshots": {"open": 0}})
    monkeypatch.setattr(wf.batches, "pull_failures",
                        lambda n, bid: pytest.fail("在途批次不该去拉失败明细"))
    assert wf._reap_batches(conn) == (0, 0, [])
    assert [a[3] for a in calls.get("recorded", [])] == ["running"]
    assert not [a for s, a in conn.executed
                if isinstance(a, dict) and "reason" in a]


def test_reap_batches_waits_for_screenshots(wired, monkeypatch):
    """任务采完了但截图还没截完 → 批次未落定。截图也是这批的产出,
    这时认账失败会把一批本来马上就有图的组合判死。"""
    wf, _ = wired
    conn = _reap_conn()
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: {"stats": {"open": 0, "done": 2},
                                   "screenshots": {"open": 2, "done": 0}})
    assert wf._reap_batches(conn) == (0, 0, [])


def test_reap_batches_marks_vanished_batch_failed(wired, monkeypatch):
    """采集侧查不到这个批次了 → 台账落 failed,组合交给兜底超时收尾。"""
    wf, _ = wired
    finished = []
    conn = _reap_conn()
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: (_ for _ in ()).throw(LookupError("没有")))
    monkeypatch.setattr(wf.batches, "finish",
                        lambda *a, **k: finished.append(a))
    reaped, failed_pairs, notes = wf._reap_batches(conn)
    assert (reaped, failed_pairs) == (0, 0)
    assert finished[0][1] == "failed"
    assert "查不到" in notes[0]


def test_reap_batches_survives_status_outage(wired, monkeypatch):
    """状态查询炸了 → 保持在途,下轮再查。**绝不当成落定去认账失败**。"""
    wf, _ = wired
    conn = _reap_conn()
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: (_ for _ in ()).throw(RuntimeError("502")))
    monkeypatch.setattr(wf.batches, "finish",
                        lambda *a, **k: pytest.fail("查不动就别落定"))
    assert wf._reap_batches(conn) == (0, 0, [])


def test_timeout_backstop_spares_inflight_batches():
    """兜底超时只打在**批次已不在途**的组合上——SQL 文本直接断言。

    夹具喂什么假数据都盖不住 SQL 本身:少了这个 NOT EXISTS,20 分钟一到
    就会把采集侧正在跑的批次里的组合全判失败并重推一遍。
    """
    from workflows import order_audit as wf
    assert "NOT EXISTS" in wf._TIMEOUT_SQL
    assert "ops.scrape_batches" in wf._TIMEOUT_SQL
    assert "('pushed', 'running')" in wf._TIMEOUT_SQL


def test_audit_batches_are_prefix_isolated():
    """两条工作流共用 ops.scrape_batches,查在途必须按前缀圈自己的。
    否则 product_refresh 的 1 小时超时口径会把订单审核的批次标成 timeout。"""
    from workflows import order_audit as wf, product_refresh as pr
    assert wf._BATCH_PREFIX != pr.BATCH_PREFIX
    assert "batch_name LIKE" in wf._OPEN_BATCHES_SQL
    assert "batch_name LIKE" in pr._SQL_OPEN


def test_push_scrape_records_batch_id_for_failures(wired, monkeypatch):
    """推送响应里的 batch_id 必须当场记进台账:`/failures` 只认 id 不认名字,
    漏记了就永远答不上"这个 ASIN 为什么没采到"。"""
    wf, calls = wired
    conn = FakeConn({})
    note = wf._push_scrape(conn, [("B0A", "10001"), ("B0B", "90210")], {})
    assert "2 个 ASIN×邮编,2 个邮编批次" in note
    # 一个邮编一个批次,批次名带邮编
    names = [n for n, _, _, _ in calls["batches"]]
    assert sorted(z for _, _, z, _ in calls["batches"]) == ["10001", "90210"]
    assert all(z in n for n, _, z, _ in calls["batches"])
    recorded = {a[0]: a for a in calls["recorded"]}
    assert set(recorded) == set(names)
    assert all(a[1] == "b1" and a[3] == "pushed" for a in recorded.values())


def test_push_scrape_marks_pending_before_calling(wired, monkeypatch):
    """先落 pending 再调接口(CLAUDE.md 铁律):反过来网络一断就成了
    "推上去了但库里没记录",下轮重复推同一批。"""
    wf, _ = wired
    conn = FakeConn({})
    seen = []
    monkeypatch.setattr(wf.scraper, "submit_json",
                        lambda *a, **k: seen.append(len(conn.executed))
                        or {"batch_id": "b1", "inserted": 1})
    wf._push_scrape(conn, [("B0A", "10001")], {})
    assert seen[0] > 0                    # 调接口时台账里已经写过东西了
    sql, params = conn.executed[0]
    assert "INSERT INTO ops.audit_scrape" in sql and params[0]["asin"] == "B0A"


def test_push_scrape_failure_settles_the_pairs(wired, monkeypatch):
    """推送失败 → 这些组合当场判 failed,不能永远挂在 pending 上等超时。"""
    wf, calls = wired
    conn = FakeConn({})
    monkeypatch.setattr(wf.scraper, "submit_json",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("炸")))
    note = wf._push_scrape(conn, [("B0A", "10001")], {})
    assert "失败 1" in note
    assert any("state = 'failed'" in s for s, _ in conn.executed)
    assert calls["recorded"][-1][3] == "failed"


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


# ══════════════════════════════════════════════════════════════════════════════
#  闭环:每种情况都要有处置,不能"说在等采集"却永远不采
# ══════════════════════════════════════════════════════════════════════════════

def test_judge_unusable_zip_is_not_called_pending_scrape():
    """邮编取不出来 ⇒ 按邮编采集根本发不出去。

    这类行若沿用"待采集"文案,就会永远挂在那儿等一个不会到来的快照,
    而摘要里的待采数又不含它 —— 静默卡死的典型形状。
    """
    for bad in (None, "", "123", "abc"):
        res = rules.judge(dict(LINE, postal_code=bad), None, SUPPLIERS, set())
        assert res.status == rules.MANUAL
        assert res.rescrape is False, "推了也发不出去,别进待采清单"
        assert "邮编" in res.note and "待采集" not in res.note


def test_judge_non_asin_sku_is_not_called_pending_scrape():
    """SKU 不是 ASIN 形态 ⇒ 采集侧建任务时就丢弃,推了也白推。"""
    res = rules.judge(dict(LINE, sku="OLD-CUSTOM-001"), None, SUPPLIERS, set())
    assert res.status == rules.MANUAL
    assert res.rescrape is False
    assert "ASIN 形态" in res.note and "待采集" not in res.note


def test_judge_every_branch_returns_a_verdict():
    """穷举:各种残缺输入下,judge 必须给出三值之一,不能返回 None 或抛异常。"""
    cases = [
        ({}, None),                                        # 空行 + 无快照
        (LINE, None),
        (LINE, _snap(outcome="blocked")),
        (LINE, _snap(amz_price=None)),
        (LINE, _snap(ship_method=None)),
        (LINE, _snap(ship_days=None)),
        (LINE, _snap(shipping=None)),
        (LINE, _snap(amz_title=None)),
        (LINE, _snap(ship_method="FBM")),                  # 无匹配采购方
        (dict(LINE, product_amount=None), _snap()),        # 限价算不出
        (dict(LINE, qty=None), _snap()),                   # 成本算不出
        (dict(LINE, product_name=None), _snap()),          # 沃尔玛标题空
    ]
    for line, snap in cases:
        res = rules.judge(line, snap, SUPPLIERS, set())
        assert res.status in (rules.PASS, rules.REJECT, rules.MANUAL)
        assert res.note, "每种情况都得给出人能读的原因"
        assert isinstance(res.detail, dict)
