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
