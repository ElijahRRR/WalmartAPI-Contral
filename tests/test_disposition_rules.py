"""在线商品处置判据回归(所有者定稿 2026-08-16 走进生产,逐条对应他给的伪代码)。"""

from services.maintenance_intents import TITLE_SIM_FLOOR, classify, pick_one


def test_not_found_is_delete():
    """ASIN 从亚马逊下架了 —— 拿不到标题/库存/价格,留着只会被别的 provider
    拿陈旧快照一轮轮跟。"""
    assert classify(outcome="not_found") == (
        "delete", "not_found", "亚马逊已下架(采集 not_found)")
    assert classify(outcome="NOT_FOUND")[0] == "delete"     # 大小写不敏感
    assert classify(outcome="ok")[0] is None


def test_three_zero_stock_paths_are_distinguishable():
    """三条清零判据在飞书表里长得一模一样(库存 12 → 0),
    **靠原因列才分得清是哪一条**。原因码必须各不相同。"""
    a = classify(stock_status="Currently unavailable")
    b = classify(stock_status="No Featured Offer")
    c = classify(stock_state="out_of_stock")
    d = classify(over_lead=True, lead_note="配送 30 天 > 上限 8 天")
    assert [x[0] for x in (a, b, c, d)] == ["inventory"] * 4
    assert len({x[1] for x in (a, b, c, d)}) == 4        # 四个原因码互不相同
    assert a[2] == "Currently unavailable" and b[2] == "No Featured Offer"
    assert d[2] == "配送 30 天 > 上限 8 天"


def test_title_similarity_splits_delete_from_retitle():
    """所有者定稿:< 70% 删除,≥ 70% 改标题。"""
    assert classify(title_similarity=0.42)[:2] == ("delete", "title_mismatch")
    assert "42%" in classify(title_similarity=0.42)[2]     # 原因带上真实数值
    assert classify(title_similarity=TITLE_SIM_FLOOR)[0] is None   # 边界:不删
    assert classify(title_similarity=0.85)[0] is None      # 交给 title_intents


def test_unknown_similarity_is_not_a_mismatch():
    """⚠ 相似度 None(有一边根本没标题)**不算不匹配**:算不出来 ≠ 不像。

    走到这一步说明标题缺失,那是采集问题;拿它当删除依据 = 采集抖动一次
    就删一批在架商品,而且不报错。
    """
    assert classify(title_similarity=None)[0] is None
    assert classify(title_similarity=0.0)[0] == "delete"   # 0.0 是明确的不像


def test_delete_beats_everything():
    """一个 SKU 一轮只出一个动作,删除压过一切 —— 否则执行件会先花配额
    去改/清一个马上要删的商品(批次 E 踩过同款坑)。"""
    assert classify(outcome="not_found", stock_state="out_of_stock",
                    title_similarity=0.9, over_lead=True)[0] == "delete"
    # 标题不匹配也压过清零
    assert classify(title_similarity=0.3, stock_state="out_of_stock")[0] == "delete"


def test_pick_one_ranks_delete_inventory_title():
    assert pick_one([("title", "t", ""), ("delete", "d", ""),
                     ("inventory", "i", "")])[0] == "delete"
    assert pick_one([("title", "t", ""), ("inventory", "i", "")])[0] == "inventory"
    assert pick_one([]) == (None, "", "")
    assert pick_one([(None, "", ""), (None, "", "")]) == (None, "", "")


def test_nothing_wrong_means_no_action():
    assert classify(outcome="ok", stock_status="In Stock",
                    stock_state="in_stock", title_similarity=0.95) == (None, "", "")


# ── provider 接线回归 ────────────────────────────────────────────────────────

def _row(**kw):
    base = {"store": "T1", "sku": "B0A", "product_name": "Steel Cup",
            "product_type": "Cups", "upc": "012345678905", "wm_price": 20.0,
            "avail_qty": 10, "amz_price": 10.0, "stock_count": 7,
            "delivery_days": 3, "slow": {"title": "ACME Steel Cup", "brand": "ACME"},
            "fulfillment": "FBM", "shipping": 0.0, "outcome": "ok",
            "stock_status": "In Stock", "stock_state": "in_stock"}
    base.update(kw)
    return base


def test_similarity_compares_processed_titles_not_raw():
    """⚠ 不能拿沃尔玛标题去比亚马逊**原始**标题。

    force_amazon_copy 会去掉品牌名,我们的沃尔玛标题天生就是"亚马逊标题减品牌"。
    比原始标题的话,品牌一长(SuperMegaBrandName Cup → Cup)相似度掉到 24%,
    一个完全正常的商品会被判成"不是同一个东西"而删除。
    """
    from services.maintenance_intents import processed_title
    from services.order_audit import title_similarity
    slow = {"title": "SuperMegaBrandName Cup", "brand": "SuperMegaBrandName"}
    assert processed_title(slow) == "Cup"
    assert title_similarity("Cup", slow["title"]) < 0.3      # 比原始:误判
    assert title_similarity("Cup", processed_title(slow)) == 1.0
    assert processed_title({"title": "[商品不存在]"}) == ""   # 占位符不算标题
    assert processed_title(None) == ""


def test_inventory_provider_carries_the_reason_code(monkeypatch):
    """四条清零判据必须各自带原因码传到飞书「原因」列。"""
    from services import maintenance_intents as mi
    rows = [_row(sku="B0UNAVAIL", stock_status="Currently unavailable"),
            _row(sku="B0NOBB", stock_status="No Featured Offer"),
            _row(sku="B0OOS", stock_state="out_of_stock"),
            _row(sku="B0SLOW", delivery_days=30)]
    monkeypatch.setattr(mi, "_rows", lambda conn, sz: rows)
    monkeypatch.setattr(mi.store_limits, "lead_day_caps", lambda: {})
    out = {i["sku"]: (i["code"], i["new"]) for i in mi.inventory_intents(None, [])}
    assert out["B0UNAVAIL"] == ("unavailable", 0)
    assert out["B0NOBB"] == ("no_buybox", 0)
    assert out["B0OOS"] == ("out_of_stock", 0)
    assert out["B0SLOW"][0] == "lead_days" and out["B0SLOW"][1] == 0


def test_inventory_provider_yields_to_delete(monkeypatch):
    """一个 SKU 一轮只出一个动作:该删的不在库存链里再出一条。

    ⚠ **两条删除判据都要覆盖**。2026-08-16 演练实见:只判了 outcome,
    标题不匹配那条在本 provider 看不见,于是该删的行照样产一条库存意图
    (B0MISMATCH 10 → 7),执行件先花配额清零再花配额删。
    """
    from services import maintenance_intents as mi
    monkeypatch.setattr(mi.store_limits, "lead_day_caps", lambda: {})
    for kw in ({"outcome": "not_found"},
               {"product_name": "完全不相干的商品名"}):     # 相似度 < 70%
        monkeypatch.setattr(mi, "_rows", lambda conn, sz, k=kw: [
            _row(sku="B0DEAD", stock_state="out_of_stock", **k)])
        assert mi.inventory_intents(None, []) == [], kw
        assert mi.price_intents(None, {"T1": {"fbm_range1": "200%"}}, []) == [], kw


def test_title_provider_gates_at_70_percent(monkeypatch):
    """< 70% 不改标题(交给删除链);≥ 70% 照改。"""
    from services import maintenance_intents as mi
    monkeypatch.setattr(mi, "_rows", lambda conn, sz: [
        _row(sku="B0OK", product_name="Steel Cup 500ml"),        # 82% → 改
        _row(sku="B0BAD", product_name="完全不同的东西"),          # 低 → 不改
    ])
    out = mi.title_intents(None, [])
    assert [i["sku"] for i in out] == ["B0OK"]
    assert out[0]["code"] == "title_sync" and "%" in out[0]["reason"]
