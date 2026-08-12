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


def test_judge_not_found_rejects_directly():
    """outcome=not_found = 亚马逊页面 404,货源没了——比照 TERMINAL 直接
    建议拒绝,不再白烧一天重采配额(所有者拍板 2026-08-12)。"""
    res = rules.judge(LINE, _snap(outcome="not_found"), SUPPLIERS, set())
    assert res.status == rules.REJECT
    assert "not_found" in res.note and "建议拒绝" in res.note
    assert not res.rescrape                 # 不排重采


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


def test_judge_title_mismatch_never_yields_a_final_verdict():
    """商品一致性存疑 ⇒ 结论**一定是待人工**,绝不给通过/建议拒绝。

    商品都可能不是同一个,拿它算出来的结论不作数 —— 这条不变量不变。
    """
    res = rules.judge(LINE, _snap(amz_price=999, amz_title="Different Thing"),
                      SUPPLIERS, set())
    assert res.status == rules.MANUAL
    assert "商品可能不一致" in res.note


def test_judge_title_mismatch_still_assigns_supplier_and_cost():
    """标题不符的行**也要把采购方/限价/成本算出来填上**(所有者定稿 2026-08-10)。

    人工复核时得看到"如果这真是同一个商品,该找谁采、成本多少、过不过限价";
    只给一句"可能不一致"什么都判断不了。原先在标题这道就 return 了,
    飞书上采购方、成本全是空的。
    """
    res = rules.judge(LINE, _snap(amz_price=40, shipping=0,
                                  amz_title="Different Thing"),
                      SUPPLIERS, set())
    assert res.status == rules.MANUAL
    assert res.detail["supplier"] == "甲"          # 档位照选
    assert res.detail["price_cap"] == 75.0         # 限价照算
    assert res.detail["cost"] == 43.2              # 40 × 1 × 1.0 × 1.08
    assert res.detail["landed_price"] == 40.0
    assert "供参考" in res.note                     # 但明确标注不作数


def test_judge_title_missing_also_keeps_computing():
    """标题取不到与标题不符同一处理:降级为待人工,后面照算。"""
    res = rules.judge(LINE, _snap(amz_price=40, shipping=0, amz_title=None),
                      SUPPLIERS, set())
    assert res.status == rules.MANUAL and "标题取不到" in res.note
    assert res.detail["supplier"] == "甲" and res.detail["cost"] == 43.2


def test_judge_title_mismatch_keeps_rescrape_flag():
    """降级不能把重采标记吃掉:运费没采到仍要进待采清单。"""
    res = rules.judge(LINE, _snap(shipping=None, amz_title="Different Thing"),
                      SUPPLIERS, set())
    assert res.status == rules.MANUAL and res.rescrape is True


# ══════════════════════════════════════════════════════════════════════════════
#  波次编排(2026-08-10 定稿:一批混邮编,只有同一 ASIN 的多邮编才拆批)
# ══════════════════════════════════════════════════════════════════════════════

def test_plan_waves_mixes_zips_in_one_batch():
    """不同 ASIN 的不同邮编同批 —— 采集侧 items[].zip_code 是一等能力
    (邮编三档:逐 ASIN > 批次级 > 服务端默认)。

    这条守的是一次真实的过度收紧:曾按"一个邮编一个批次"改过,结果 134 行
    订单推出 127 个批次(收件邮编几乎两两不同),而那个收紧的两条理由
    (截图会串、按批次取数分不出邮编)查证后都不成立。
    """
    waves = rules.plan_waves([("B1", "10001"), ("B2", "90210")], set())
    assert waves == [[("B1", "10001"), ("B2", "90210")]]


def test_plan_waves_splits_same_asin_across_batches():
    """同一 ASIN 的多个邮编必须拆批:采集侧 tasks 是 UNIQUE(batch_id, asin),
    同批两个邮编回 400 conflicting_zip_for_asin(明确拒绝,不静默取第一个)。
    但**同一轮内全部推完**,不跨轮等待。"""
    waves = rules.plan_waves([("B1", "10001"), ("B1", "90210"),
                              ("B2", "10001")], set())
    assert len(waves) == 2
    assert waves[0] == [("B1", "10001"), ("B2", "10001")]
    assert waves[1] == [("B1", "90210")]
    # 每一波内 ASIN 不重复 —— 这正是采集侧那条 400 的触发条件
    for w in waves:
        assert len({a for a, _ in w}) == len(w)


def test_plan_waves_three_zips_same_asin_makes_three_waves():
    waves = rules.plan_waves(
        [("B1", "10001"), ("B1", "20002"), ("B1", "30003")], set())
    assert [len(w) for w in waves] == [1, 1, 1]


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
    """分波必须可复现,否则排障时"上轮到底推了什么"说不清。"""
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

    # 采集器一律不真连:记录每次提交的 (批次名, [(asin, 邮编)], 是否要截图)
    calls["batches"] = []

    def fake_submit(name, items, *, needs_screenshot=False):
        items = list(items)
        calls["batches"].append((name, items, needs_screenshot))
        return {"batch_id": "b1", "inserted": len(items),
                "per_asin_zip_count": len(items), "invalid_zip_rows": 0}
    monkeypatch.setattr(wf.scraper, "submit_json", fake_submit)

    # 轮询默认开(2026-08-10 起),但 run() 的用例关心的是判定与推送,不是等待。
    # 这里把等待与就地摄取打成空转:不打的话每个 run() 用例都要去连采集器,
    # 还要真 sleep —— 一次改默认值让整套用例慢了 10 秒就是这么来的。
    # 专门测等待/摄取的用例在下面直接调 _wait_for_batches / _ingest_now。
    calls["waited"] = []
    calls["ingested"] = 0

    def fake_wait(names, timeout_min):
        calls["waited"].append(list(names))
        return f"等采集:{len(names)} 批全部落定(假)", 0
    monkeypatch.setattr(wf, "_wait_for_batches", fake_wait)

    def fake_ingest():
        calls["ingested"] += 1
        return "就地增量摄取:(假)"
    monkeypatch.setattr(wf, "_ingest_now", fake_ingest)

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
    name, items, shot = calls["batches"][0]
    assert items == [("B0TEST0001", "10001")]      # zip+4 收敛后再推
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

def _reap_conn(pending=(("B0A", "10001"), ("B0B", "10001")),
               *, reapable=True, caught_up=True):
    """在途批次 + 可认账批次两段分开喂。

    `reapable=False` = 这批还没落定(或刚落定),不在可认账集合里;
    `caught_up=False` = 已落定但 product_ingest 还没跑过 —— 数据可能正在
    增量流里,**这时认账失败就是冤枉**(2026-08-10 实测 127 个组合全军覆没)。
    """
    return FakeConn({
        "ORDER BY b.submitted_at":
            (["batch_name", "batch_id", "asin_count"],
             [("wm-audit-10001-x", "7", 2)]),
        "ingest_caught_up":
            (["batch_name", "batch_id", "ingest_caught_up"],
             [("wm-audit-10001-x", "7", caught_up)] if reapable else []),
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
    conn = _reap_conn(reapable=False)
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: {"stats": {"open": 2}, "screenshots": {"open": 0}})
    monkeypatch.setattr(wf.batches, "pull_failures",
                        lambda n, bid: pytest.fail("在途批次不该去拉失败明细"))
    assert wf._reap_batches(conn) == (0, 0, [])
    assert [a[3] for a in calls.get("recorded", [])] == ["running"]
    assert not [a for s, a in conn.executed
                if isinstance(a, dict) and "reason" in a]


def test_reap_batches_settles_even_with_screenshots_open(wired, monkeypatch):
    """任务采完、截图还没截完 → **照样算落定**(2026-08-10 实测后改)。

    原先拿 screenshots.open 当落定条件,结果一个 variant_offset 失败的任务
    之后它那张图永远不会被截出来,shots_open 永久停在 >0,批次永远落不定
    —— `-p wait=1` 明明数据都采完了却干等满 20 分钟。

    截图是佐证材料,"从不阻断审核结论",更不该阻断数据侧的落定判断。
    早点认落定反而让图更快贴上:_settled_batches 只对已落定的批次去拿清单。
    """
    wf, _ = wired
    conn = _reap_conn(reapable=False)
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: {"stats": {"open": 0, "done": 2},
                                   "screenshots": {"open": 2, "done": 0}})
    settled, failed_pairs, _notes = wf._reap_batches(conn)
    assert (settled, failed_pairs) == (1, 0)   # 落定了,但认账仍等摄取水位线


def test_reap_batches_marks_vanished_batch_failed(wired, monkeypatch):
    """采集侧查不到这个批次了 → 台账落 failed,组合交给兜底超时收尾。"""
    wf, _ = wired
    finished = []
    conn = _reap_conn(reapable=False)
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
    conn = _reap_conn(reapable=False)
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: (_ for _ in ()).throw(RuntimeError("502")))
    monkeypatch.setattr(wf.batches, "finish",
                        lambda *a, **k: pytest.fail("查不动就别落定"))
    assert wf._reap_batches(conn) == (0, 0, [])


def test_reap_batches_waits_for_ingest_before_blaming(wired, monkeypatch):
    """批次已落定但 product_ingest 还没跑过 → **一个组合都不许判失败**。

    2026-08-10 实测的原样重现:127 个组合被判「批次已采完但无快照」,
    紧接着一次 product_ingest 就把这 127 条原样摄了进来——采集全成功,
    是我们问早了。批次 completed 只说明采集侧干完了,数据还在增量导出流里。

    代价不止台账写错:failed 不挡重推,下一轮会把这些组合再采一遍,
    每小时白烧一轮配额,而两侧都不报错。
    """
    wf, _ = wired
    conn = _reap_conn(reapable=True, caught_up=False)
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: {"stats": {"open": 0, "done": 2},
                                   "screenshots": {"open": 0, "done": 2}})
    monkeypatch.setattr(wf.batches, "pull_failures",
                        lambda n, bid: pytest.fail("摄取没追上就别去问失败原因"))
    settled, failed_pairs, notes = wf._reap_batches(conn)
    assert failed_pairs == 0
    assert any("等摄取" in n for n in notes)     # 摘要要能看出在等什么
    assert not [a for s_, a in conn.executed
                if isinstance(a, dict) and "reason" in a]


def test_reap_batches_blames_once_ingest_caught_up(wired, monkeypatch):
    """摄取水位线越过落定时刻后仍无快照 → 这才是真失败,带真实原因认账。"""
    wf, _ = wired
    conn = _reap_conn(reapable=True, caught_up=True)
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: {"stats": {"open": 0, "done": 2},
                                   "screenshots": {"open": 0, "done": 2}})
    monkeypatch.setattr(wf.batches, "pull_failures",
                        lambda n, bid: ("captcha×2", {"B0A": "captcha"}))
    _settled, failed_pairs, _notes = wf._reap_batches(conn)
    assert failed_pairs == 2
    reasons = [a["reason"] for s_, a in conn.executed
               if isinstance(a, dict) and "reason" in a]
    assert "采集失败:captcha" in reasons          # 有明细的按明细
    assert any("摄取已追上" in r for r in reasons)  # 没明细的说清是问过之后才判的



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
    note, sent = wf._push_scrape(conn, [("B0A", "10001"), ("B0B", "90210")], {})
    assert "推采集:2 个 ASIN×邮编" in note
    # 两个不同 ASIN 的不同邮编进同一批(不再一个邮编一个批次)
    assert len(calls["batches"]) == 1
    name, items, shot = calls["batches"][0]
    assert sorted(items) == [("B0A", "10001"), ("B0B", "90210")]
    assert shot is True
    assert sent == [name]     # 等待名单就是本轮真推出去的批次
    recorded = {a[0]: a for a in calls["recorded"]}
    assert set(recorded) == {name}
    assert all(a[1] == "b1" and a[3] == "pushed" for a in recorded.values())


def test_push_scrape_splits_same_asin_into_waves(wired):
    """同一 ASIN 的两个邮编 → 两个批次(否则采集侧 400)。"""
    wf, calls = wired
    wf._push_scrape(FakeConn({}), [("B0A", "10001"), ("B0A", "90210")], {})
    assert len(calls["batches"]) == 2
    assert {z for _, items, _ in calls["batches"] for _, z in items} == {
        "10001", "90210"}
    names = [n for n, _, _ in calls["batches"]]
    assert len(set(names)) == 2       # 批次名必须互不相同,否则第二波撞 409


def test_push_scrape_warns_when_zips_not_all_accepted(wired, monkeypatch, caplog):
    """采集侧只认了一部分逐 ASIN 邮编 ⇒ 没认的按**服务端默认邮编**采,
    拿回来的价格是别的地区的,而两侧都不报错。必须喊出来。"""
    wf, _ = wired
    monkeypatch.setattr(wf.scraper, "submit_json",
                        lambda *a, **k: {"batch_id": "b1", "inserted": 2,
                                         "per_asin_zip_count": 1,
                                         "invalid_zip_rows": 1})
    with caplog.at_level("WARNING"):
        wf._push_scrape(FakeConn({}), [("B0A", "10001"), ("B0B", "90210")], {})
    assert "只认了 1/2" in caplog.text


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
    note, sent = wf._push_scrape(conn, [("B0A", "10001")], {})
    assert "失败 1" in note
    assert sent == []        # 推失败的批次不进等待名单
    assert any("state = 'failed'" in s for s, _ in conn.executed)
    assert calls["recorded"][-1][3] == "failed"


# ══════════════════════════════════════════════════════════════════════════════
#  wait=1:等采完 → 就地摄取 → 重判(一条命令出结论)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def nosleep(monkeypatch):
    from workflows import order_audit as wf
    monkeypatch.setattr(wf.time, "sleep", lambda s: None)
    return wf


def test_wait_returns_when_all_batches_settle(nosleep, monkeypatch):
    wf = nosleep
    seen = []

    def status(name):
        seen.append(name)
        return {"stats": {"open": 0}, "screenshots": {"open": 0}}
    monkeypatch.setattr(wf.scraper, "batch_status", status)
    note, stuck = wf._wait_for_batches(["b1", "b2"], 20)
    assert stuck == 0 and "2 批全部落定" in note
    assert set(seen) == {"b1", "b2"}


def test_wait_keeps_waiting_while_screenshots_open(nosleep, monkeypatch):
    """任务采完了但截图还在跑 → 还没落定。这轮就摄取的话,截图列会整批空。"""
    wf = nosleep
    calls = {"n": 0}

    def status(name):
        calls["n"] += 1
        open_shots = 1 if calls["n"] < 3 else 0
        return {"stats": {"open": 0}, "screenshots": {"open": open_shots}}
    monkeypatch.setattr(wf.scraper, "batch_status", status)
    note, stuck = wf._wait_for_batches(["b1"], 20)
    assert stuck == 0 and calls["n"] >= 3


def test_wait_gives_up_at_deadline_without_failing(nosleep, monkeypatch):
    """超时不是失败:已采到的照常进增量流,没采到的下轮由台账三层判据处置。"""
    wf = nosleep
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: {"stats": {"open": 5}, "screenshots": {"open": 0}})
    note, stuck = wf._wait_for_batches(["b1"], 0)      # 0 分钟 = 立刻到点
    assert stuck == 1 and "仍在跑" in note


def test_wait_stops_waiting_on_vanished_batch(nosleep, monkeypatch):
    """采集侧查不到这个批次了 → 再等也没意义,交给对账那层认账。"""
    wf = nosleep
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: (_ for _ in ()).throw(LookupError("没有")))
    note, stuck = wf._wait_for_batches(["b1"], 20)
    assert stuck == 0


def test_wait_survives_status_outage(nosleep, monkeypatch):
    """状态查询炸了不能当落定(那会让下一步的对账把整批判成没数据),
    继续等到超时为止。"""
    wf = nosleep
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: (_ for _ in ()).throw(RuntimeError("502")))
    note, stuck = wf._wait_for_batches(["b1"], 0)
    assert stuck == 1


def test_wait_with_no_batches_is_a_noop(nosleep):
    assert nosleep._wait_for_batches([], 20) == ("", 0)


def test_ingest_now_skips_when_product_ingest_holds_the_lock(monkeypatch):
    """拿不到 product_ingest 的锁 = 它正在跑:跳过,别和它抢游标。

    两个进程同推游标,后写的会盖掉先写的,中间那段记录永远不会再被拉一次
    —— 两侧都不报错,只是产品中心少了一批数据。
    """
    import contextlib
    from workflows import order_audit as wf        # 要真的 _ingest_now,不用 wired 的桩
    monkeypatch.setattr(wf.runlock, "hold",
                        lambda name: contextlib.nullcontext(False))
    monkeypatch.setattr(wf.ingest, "pump",
                        lambda *a, **k: pytest.fail("没拿到锁就不该摄取"))
    note = wf._ingest_now()
    assert "跳过" in note and "product_ingest" in note


def test_ingest_now_pumps_under_the_lock(monkeypatch):
    import contextlib
    from workflows import order_audit as wf        # 同上
    taken = []
    monkeypatch.setattr(wf.runlock, "hold",
                        lambda name: taken.append(name) or
                        contextlib.nullcontext(True))
    monkeypatch.setattr(wf.ingest, "pump",
                        lambda sc, d, **k: {"ok": True, "pages": 1, "fetched": 2,
                                            "cursor_from": 0, "cursor_to": 9,
                                            "totals": {"snapshots": 2, "dup": 0,
                                                       "products": 2,
                                                       "skipped_outcome": 0,
                                                       "incomplete": 0,
                                                       "invalid": 0},
                                            "error": None})
    note = wf._ingest_now()
    assert taken == ["product_ingest"]     # 借的是它的锁,不是自己开一把
    assert "就地增量摄取" in note and "游标 0 → 9" in note


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


# ══════════════════════════════════════════════════════════════════════════════
#  飞书数字列:落库形态 + 推送前的兜底
# ══════════════════════════════════════════════════════════════════════════════

def test_detail_keeps_numbers_as_numbers(wired):
    """Decimal 落 audit_detail 必须还是数字,**不能被 default=str 变成 "25.99"**。

    2026-08-10 生产实测:`json.dumps(default=str)` 把价格存成了字符串,回写
    飞书「亚马逊单价」数字列时 NumberFieldConvFail。落库、判定、日志全程
    正常,只有推送那一步炸,而飞书的报错里既没有行号也没有列名。
    前几轮没有行真的拿到过价格,这个字段一直是空的,直到 102 行判通过才撞上。
    """
    from decimal import Decimal
    wf, _ = wired
    conn = FakeConn({})
    res = SimpleNamespace(status="✓ 通过", note="ok",
                          detail={"amz_price": Decimal("25.99"),
                                  "scraped_at": datetime(2026, 8, 10,
                                                         tzinfo=timezone.utc)})
    wf._save(conn, [({"order_line_id": "PO1|SKU1"}, res)])
    stored = json.loads([a for s_, a in conn.executed if s_.startswith("UPDATE")][0][0][1])
    assert stored["amz_price"] == 25.99
    assert isinstance(stored["amz_price"], float)      # 不是 "25.99"
    assert isinstance(stored["scraped_at"], str)       # datetime 仍转字符串


@pytest.mark.parametrize("raw,want", [
    (25.99, 25.99),
    (7, 7),
    ("25.99", 25.99),        # 历史行的老形状:得推得出去,不能炸整批
    ("  3 ", 3.0),
    (None, None),
    ("", None),
    ("N/A", None),           # 非数字降成空,而不是让 152 行一起推不上去
    ("免费", None),
    (True, None),            # bool 是 int 的子类,别当 1 推进数字列
    (float("nan"), None),    # json.dumps 会吐 NaN —— JSON 规范里没有,飞书拒
    (float("inf"), None),
])
def test_num_coerces_or_drops(wired, raw, want):
    wf, _ = wired
    got = wf._num(raw, "亚马逊单价", "PO1|SKU1")
    assert got == want or (got is None and want is None)


# ══════════════════════════════════════════════════════════════════════════════
#  采购区间按**亚马逊落地价**(单价 + 运费)选档(所有者定稿 2026-08-10)
# ══════════════════════════════════════════════════════════════════════════════

_BANDED = rules.parse_suppliers([
    _rec(name="低档", m="FBA", lo=0, hi=50, rate=1.30),
    _rec(name="高档", m="FBA", lo=50, hi=200, rate=1.05),
], FIELDS)


def test_supplier_band_uses_landed_price_not_unit_price():
    """单价 45 + 运费 10 = 落地价 55 ⇒ 该进 [50,200] 那档,不是 [0,50]。

    按裸单价选档会挑到「低档」,于是用错汇率(1.30 而不是 1.05),
    成本算出来偏大或偏小都可能,而选档、定价、飞书全程不报错。
    """
    res = rules.judge(LINE, _snap(amz_price=45, shipping=10), _BANDED, set())
    assert res.detail["landed_price"] == 55.0
    assert res.detail["supplier"] == "高档"
    assert res.detail["rate"] == 1.05


def test_supplier_band_ignores_walmart_sale_amount():
    """区间说的是"我们要花多少钱买",跟沃尔玛卖多少钱无关。

    沃尔玛售价 999 而亚马逊落地价 30 ⇒ 仍选低档。拿销售金额选档
    等于把利润高的单一律推到高价档,选到错的汇率。
    """
    line = {**LINE, "product_amount": 999}
    res = rules.judge(line, _snap(amz_price=20, shipping=10), _BANDED, set())
    assert res.detail["landed_price"] == 30.0
    assert res.detail["supplier"] == "低档"


def test_supplier_band_boundary_is_closed_on_landed():
    """区间是**闭**区间,落地价压在端点上时两档都命中 → 按既有规则取汇率最低者。

    这里 50.0 同时落在 [0,50] 和 [50,200],1.05 < 1.30 ⇒ 高档。
    换成落地价选档并没有动这条消歧规则,只是换了拿去比的那个数。
    """
    res = rules.judge(LINE, _snap(amz_price=40, shipping=10), _BANDED, set())
    assert res.detail["landed_price"] == 50.0
    assert res.detail["supplier"] == "高档"     # 汇率最低者胜,不是"先命中者"


def test_missing_shipping_blocks_supplier_pick():
    """运费没采到 ⇒ 落地价算不出来 ⇒ **选不了档**,待人工 + 重采。

    绝不 `or 0` 拿单价顶替:那会稳定挑到偏低的一档,汇率错、成本偏小,
    本该拒的单被放行,而全程不报错(采集契约不变量 3b)。
    """
    res = rules.judge(LINE, _snap(shipping=None), _BANDED, set())
    assert res.status == rules.MANUAL and res.rescrape is True
    assert "落地价" in res.note and "运费" in res.note
    assert res.detail["landed_price"] is None
    assert "supplier" not in res.detail          # 没瞎猜一个档位出来


def test_free_shipping_is_zero_not_missing():
    """FREE → 0.0 是"确认免运费"这条真信息,落地价 = 单价,照常选档。"""
    res = rules.judge(LINE, _snap(amz_price=30, shipping=0), _BANDED, set())
    assert res.detail["landed_price"] == 30.0 and res.detail["supplier"] == "低档"


# ── 限价与采购方解耦 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("snap,why", [
    (None, "没快照"),
    ({"asin": "B0TEST0001", "zip": "10001", "amz_price": 9999, "shipping": 0,
      "stock_qty": 1, "ship_method": "FBM", "ship_days": 3, "seller": "x",
      "outcome": "ok", "amz_title": LINE["product_name"],
      "scraped_at": "2026-08-09T00:00:00Z"}, "无匹配采购方"),
])
def test_price_cap_filled_regardless_of_supplier(snap, why):
    """限价只跟沃尔玛这侧的商品金额有关(× 0.75),**跟有没有采购方无关**。

    原先限价是在采购方命中之后才算的,于是「无匹配采购方」「待采集」这些行
    在飞书上连限价列都是空的 —— 人工复核时连个参照都没有,而这个数根本不
    需要等采集。
    """
    res = rules.judge(LINE, snap, SUPPLIERS, set())
    assert res.status == rules.MANUAL, why
    assert res.detail["price_cap"] == 75.0        # 100 × 0.75


# ══════════════════════════════════════════════════════════════════════════════
#  重采无效的采集失败 → 终局结论(所有者定稿 2026-08-10)
# ══════════════════════════════════════════════════════════════════════════════

def test_unretryable_scrape_failure_is_terminal_reject():
    """variant_offset 这类重采也没用的失败 ⇒ 建议拒绝,**不再重采**。

    重定向到兄弟变体页意味着这个 ASIN 的页面根本取不到目标商品,换时段、
    换 worker 都一样。挂"待采集"是害人的:行会一直等一个永远不来的快照,
    而每轮还要为它烧一次配额,两侧都不报错 —— 典型的静默卡死。
    """
    res = rules.judge(LINE, None, SUPPLIERS, set(), scrape_fail="variant_offset")
    assert res.status == rules.REJECT
    assert res.rescrape is False
    assert "variant_offset" in res.note and "建议拒绝" in res.note
    assert "兄弟变体页" in res.note           # 把封闭集里的人话带出来
    assert res.detail["rules"]["scrape"]["retryable"] is False


@pytest.mark.parametrize("et", sorted(rules.RETRYABLE))
def test_retryable_scrape_failure_still_waits(et):
    """验证码、超时、切邮编失败这类换个时段可能就好了 ⇒ 照旧待人工 + 重采。"""
    res = rules.judge(LINE, None, SUPPLIERS, set(), scrape_fail=et)
    assert res.status == rules.MANUAL and res.rescrape is True
    assert res.detail["rules"]["scrape"]["retryable"] is True


@pytest.mark.parametrize("et", sorted(rules.TERMINAL))
def test_every_unretryable_type_is_terminal(et):
    """封闭集里所有不可重采的类型都走终局,别只照顾 variant_offset 一个。

    采集侧新增一个不可重采类型时,这条会自动覆盖到 —— 而不是等它在生产上
    挂成"待采集"堆几百行才有人发现。
    """
    res = rules.judge(LINE, None, SUPPLIERS, set(), scrape_fail=et)
    assert res.status == rules.REJECT and res.rescrape is False


def test_unknown_error_type_stays_manual():
    """兜底桶 unknown 不当终局:拿不准就别替人下终局结论。

    (services.scrape_batches.pull_failures 那边已经会为未登记类型告警,
     这里只保证判定链不擅自替它做决定。)
    """
    res = rules.judge(LINE, None, SUPPLIERS, set(), scrape_fail="unknown")
    assert res.status == rules.MANUAL and res.rescrape is True


def test_scrape_failure_ignored_once_snapshot_arrives():
    """台账里有历史失败,但这次快照回来了 ⇒ 正常判,不受旧失败影响。"""
    res = rules.judge(LINE, _snap(), SUPPLIERS, set(), scrape_fail="variant_offset")
    assert res.status == rules.PASS


# ══════════════════════════════════════════════════════════════════════════════
#  批次"落定"不是最终:server 会把失败任务推回 worker 再循环两轮
#  (所有者 2026-08-10 澄清采集侧机制)
# ══════════════════════════════════════════════════════════════════════════════

def test_settled_batch_is_repolled_while_pairs_pending(wired, monkeypatch):
    """已记 completed 的批次**照样要复查状态** —— 它可能被 server 推回重采。

    原先只查 status IN ('pushed','running'),批次一旦记成 completed 就再也不会
    被看一眼:重新入队完全看不见,而我们已经按"它结束了"去认账失败了。
    """
    wf, calls = wired
    conn = _reap_conn(reapable=False)
    asked = []
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: asked.append(n) or {"stats": {"open": 2},
                                                      "screenshots": {"open": 0}})
    wf._reap_batches(conn)
    assert asked == ["wm-audit-10001-x"]      # 查了(夹具里它有 pending 组合)
    assert [a[3] for a in calls.get("recorded", [])] == ["running"]


def test_reopened_batch_clears_the_stability_clock():
    """批次弹回在途时 record() 必须把 finished_at 清空。

    不清的话:第一次 open 归零记下的 finished_at 会让"稳住 15 分钟"提前满足,
    而那批数据其实正在重采路上,我们却已经认账失败了 —— 且 failed 不挡重推,
    下一轮还会把它们再采一遍,白烧配额。
    这条只能断言 SQL 文本:夹具喂什么假数据都盖不住少一句 CASE。
    """
    import inspect
    from services import scrape_batches as sb
    src = inspect.getsource(sb.record)
    assert "finished_at = CASE WHEN EXCLUDED.status IN ('pushed','running')" in src
    assert "THEN NULL" in src


def test_reap_waits_for_the_stability_window():
    """认账失败的 SQL 必须带"落定已稳住 N 分钟"这一条。

    同样只能断言 SQL 文本 —— 时间条件在 PG 侧求值,假连接盖不住。
    """
    from workflows import order_audit as wf
    assert "b.finished_at < now() - make_interval(mins => %(stable)s)" in wf._REAPABLE_SQL
    assert wf._AUTO_RETRY_SETTLE_MIN >= 15      # server 自动重试 2 轮 × 5 分钟


# ══════════════════════════════════════════════════════════════════════════════
#  等待循环的截图宽限(所有者定稿 2026-08-10:180 秒)
# ══════════════════════════════════════════════════════════════════════════════

class _FakeTime:
    """假时钟:sleep 只推进虚拟时间。真时钟下 180 秒宽限没法测。"""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    from workflows import order_audit as wf
    c = _FakeTime()
    monkeypatch.setattr(wf, "time", c)      # 只换 workflow 里的 time 名字
    return wf, c


def test_grace_counts_all_batches_not_just_newly_settled(clock, monkeypatch):
    """先落定批次遗留的未截图**必须还算数**。

    原先只累加"本轮新落定的":A 先落定带着 2 张图没好、B 后落定带 0 张,
    总数就算成 0 直接收工,A 那 2 张白白错过一轮。修法是给每批记当前值,
    数据一齐就把所有批次再问一遍刷新 —— 这时数字才准,也正是要用它的时候。
    """
    wf, c = clock
    polls = {"b1": 0, "b2": 0}

    def status(name):
        polls[name] += 1
        if name == "b1":
            # 立刻落定,但头两次问它时还有 2 张图没截完
            return {"stats": {"open": 0},
                    "screenshots": {"open": 2 if polls["b1"] <= 2 else 0}}
        # b2 第 2 次才落定,且没有待截的图
        return {"stats": {"open": 0 if polls["b2"] >= 2 else 1},
                "screenshots": {"open": 0}}

    monkeypatch.setattr(wf.scraper, "batch_status", status)
    note, stuck = wf._wait_for_batches(["b1", "b2"], 20)
    assert stuck == 0 and "全部落定" in note
    # 关键:b2 落定之后 b1 被重新问过 —— 没有它就会算成 0 张直接收工
    assert polls["b1"] >= 3
    assert c.t < wf._SHOT_GRACE_SEC + 60      # 图好了就早收工,没耗满宽限


def test_grace_expires_and_stops_waiting(clock, monkeypatch):
    """图一直好不了 ⇒ 宽限到点就出结论,不拖到 20 分钟兜底。

    任务失败后它那张图永远不会好(shots_open 永久 >0),这条就是防它把
    -p wait=1 拖满的。
    """
    wf, c = clock
    monkeypatch.setattr(wf.scraper, "batch_status",
                        lambda n: {"stats": {"open": 0},
                                   "screenshots": {"open": 1}})
    note, stuck = wf._wait_for_batches(["b1"], 20)
    assert stuck == 0 and "全部落定" in note
    assert wf._SHOT_GRACE_SEC <= c.t < 20 * 60      # 用满宽限,但远没到兜底


def test_grace_is_180_seconds(clock):
    """宽限时长是所有者定的数,改它必须让本用例红。"""
    wf, _ = clock
    assert wf._SHOT_GRACE_SEC == 180


def test_vanished_batch_is_not_polled_again(clock, monkeypatch):
    """采集侧查不到的批次不再问 —— 否则宽限期每轮都为它撞一次 404。"""
    wf, c = clock
    polls = []

    def status(name):
        polls.append(name)
        if name == "b1":
            raise LookupError("没有")
        return {"stats": {"open": 0}, "screenshots": {"open": 0}}

    monkeypatch.setattr(wf.scraper, "batch_status", status)
    note, stuck = wf._wait_for_batches(["b1", "b2"], 20)
    assert stuck == 0 and polls.count("b1") == 1


# ══════════════════════════════════════════════════════════════════════════════
#  轮询默认开(所有者定稿 2026-08-10)
# ══════════════════════════════════════════════════════════════════════════════

def _needs_scrape_conn():
    """一行缺快照的待审行 —— 跑到"推采集"那一步,后面才有东西可等。"""
    return FakeConn({
        "ORDER BY order_date DESC": (
            PICK_COLS, [("PO1|SKU1", "店A", "B0TEST0001", "Acme Widget",
                         1, 100, 0, "10001", "Shipped", None, None)]),
        "audit_status IS NOT NULL": (["order_line_id", "audit_status",
                                      "audit_detail"], []),
    })


def test_wait_is_on_by_default(wired, monkeypatch):
    """不加任何参数就该等采集 + 就地摄取。

    忘了加开关只会看到上一轮的结论,**而且不报错** —— 这种"忘了就静默降级"
    的默认值不该留着(所有者定稿 2026-08-10)。
    """
    wf, calls = wired
    monkeypatch.setattr(wf.db, "pg_conn", lambda: _needs_scrape_conn())
    wf.run({})
    assert calls["waited"] and calls["ingested"] == 1


@pytest.mark.parametrize("off", ["0", "false", "no", "NO", "False"])
def test_wait_can_be_turned_off(wired, monkeypatch, off):
    """挂调度时用 -p wait=0 关掉,免得一次运行占住那一小时的位置。"""
    wf, calls = wired
    monkeypatch.setattr(wf.db, "pg_conn", lambda: _needs_scrape_conn())
    wf.run({"wait": off})
    assert calls["waited"] == [] and calls["ingested"] == 0


def test_wait_skipped_when_nothing_was_pushed(wired, monkeypatch):
    """没推出去任何批次就没什么可等的,别白等一轮退避。"""
    wf, calls = wired
    monkeypatch.setattr(wf.db, "pg_conn", lambda: _needs_scrape_conn())
    wf.run({"scrape": "0"})
    assert calls["waited"] == [] and calls["ingested"] == 0
