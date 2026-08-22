"""冲突判定必须先看已有占用(所有者 2026-08-22)。

原话:「那张表要问有没有被占用……如果不看这个,给出的建议就是不可靠的,
**甚至完全相反的建议**」。

为什么占用不是又一级阶梯而是**先于**阶梯:占用是**决策**不是观测,没有任何
自动释放。品牌已经占给 X,报告却按销量算出"留 Y" —— 人照做以后 X 的货没了、
品牌还锁在 X,**谁也上不了**。这不是判得不准,是判反了。
"""

from services import alloc_survey as sv


def _r(store, sku, brand="acme", cat="Home", ch="FBA", pub=True):
    return {"store": store, "sku": sku, "asin": "B0" + sku, "brand_key": brand,
            "category": cat, "channel": ch, "published": pub, "pt": "pt1"}


def _one(rows, sales=None, cfg=None, held=None):
    out = sv.resolve_conflicts(rows, sales or {}, "brand_key",
                               cfg=cfg, held=held)
    return out[0] if out else None


# ── 占用压过销量阶梯 ──────────────────────────────────────────────────

def test_claim_beats_sales_even_when_the_other_store_sells_more():
    """B 卖得多,但品牌占给了 A —— 按销量判会给出**与已落决策相反**的建议。"""
    rows = [_r("A", "S1"), _r("B", "S2")]
    sales = {("B", "S2"): (99, 9999.0)}
    key, keep, _stat, detail, level = _one(rows, sales, held={"acme": "A"})
    assert keep == "A" and level == sv.BY_CLAIM
    assert dict((d[0], d[7]) for d in detail) == {"A": "保留", "B": "下架"}
    # 不看占用时正好判反
    assert _one(rows, sales)[1] == "B"


def test_claim_holder_wins_even_with_no_online_rows_of_its_own():
    """**占位 ≠ 上架**:上一轮分配定了、货还没上,占用店手上一件都没有。
    这时别家在卖的那些**全都该下架** —— 而不是"谁在卖就归谁"。"""
    rows = [_r("B", "S1"), _r("C", "S2")]
    key, keep, _s, detail, level = _one(rows, held={"acme": "A店还没上架"})
    assert keep == "A店还没上架" and level == sv.BY_CLAIM
    assert {d[7] for d in detail} == {"下架"}


def test_a_single_store_selling_someone_elses_claimed_brand_is_reported():
    """只有一家在卖 = 没有"跨店冲突",但它卖的是别人占着的品牌 ——
    不报出来这条侵占就永远看不见(claim_audit 只查占用店自己的货)。"""
    assert _one([_r("B", "S1")], held={"acme": "A"})[1] == "A"
    # 没有占用时,一家店不构成冲突
    assert _one([_r("B", "S1")]) is None


def test_claim_holder_holding_everything_is_not_a_conflict():
    """货全在占用店手上 = 正常状态,没有要下架的行,不该进清单。"""
    assert _one([_r("A", "S1"), _r("A", "S2")], held={"acme": "A"}) is None


# ── 占用站不住:不许替人改判 ──────────────────────────────────────────

def test_a_claim_that_fails_the_category_gate_is_flagged_not_reassigned():
    """⚠ 占用店的货踩了类目闸 ⇒ **占用站不住**。

    直接改判给第二名等于**绕过 `store_release`**(全系统唯一释放路径)——
    报告没有资格替人撤一条不可逆的决策。所以只标出来,归 NEEDS_HUMAN。
    """
    rows = [_r("A", "S1", cat="Toys"), _r("B", "S2", cat="Home")]
    cfg = {"A": {"categories": ["Home"]},        # A 不做 Toys
           "B": {"categories": ["Home"]}}
    key, keep, _s, detail, level = _one(rows, cfg=cfg, held={"acme": "A"})
    assert level == sv.CLAIM_STUCK
    assert keep == "A"                            # **没有**改判给 B
    assert level in sv.NEEDS_HUMAN


def test_claim_holder_that_passes_the_gate_is_plain_by_claim():
    rows = [_r("A", "S1", cat="Home"), _r("B", "S2", cat="Home")]
    cfg = {"A": {"categories": ["Home"]}, "B": {"categories": ["Home"]}}
    assert _one(rows, cfg=cfg, held={"acme": "A"})[4] == sv.BY_CLAIM


def test_claim_holder_absent_from_the_group_is_not_stuck():
    """占用店在这组里**没有货**,谈不上"它的货踩了闸" —— 那是 BY_CLAIM
    不是 CLAIM_STUCK。混成一个会把"等着上架"报成"配置坏了"。"""
    rows = [_r("B", "S1", cat="Toys")]
    cfg = {"B": {"categories": ["Home"]}}
    got = _one(rows, cfg=cfg, held={"acme": "A"})
    assert got[4] == sv.BY_CLAIM


# ── 没有占用时行为不变 ────────────────────────────────────────────────

def test_without_claims_the_ladder_is_untouched():
    """占用台账为空(还没跑过 alloc_backfill)时,一切照旧走销量阶梯。"""
    rows = [_r("A", "S1"), _r("B", "S2")]
    sales = {("B", "S2"): (5, 500.0)}
    for held in (None, {}):
        key, keep, _s, _d, level = _one(rows, sales, held=held)
        assert keep == "B" and level == sv.LADDER[0]


def test_an_unrelated_claim_does_not_leak_into_another_group():
    rows = [_r("A", "S1", brand="acme"), _r("B", "S2", brand="acme")]
    sales = {("B", "S2"): (5, 500.0)}
    assert _one(rows, sales, held={"别的品牌": "Z"})[1] == "B"


# ── 同 ASIN 走同一条路 ────────────────────────────────────────────────

def test_product_claims_apply_to_the_asin_conflict_too():
    """同一个 ASIN 在两家店 —— B 卖得多,但产品占用在 A。"""
    rows = [_r("A", "S1"), _r("B", "S1")]        # 同 SKU ⇒ 同 ASIN B0S1
    out = sv.resolve_conflicts(rows, {("B", "S1"): (9, 900.0)}, "asin",
                               held={"B0S1": "A"})
    assert [(x[0], x[1], x[4]) for x in out] == [("B0S1", "A", sv.BY_CLAIM)]
    # 不看占用时判给 B
    assert sv.resolve_conflicts(rows, {("B", "S1"): (9, 900.0)},
                                "asin")[0][1] == "B"
