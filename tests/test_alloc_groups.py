"""品牌组组装回归。三个决定都会**静默**把货分错地方,所以逐条钉死。"""

from services import alloc_groups as ag


def _c(asin, brand, score, cat="家居", ch="FBA", manuf=None):
    return {"asin": asin, "brand": brand, "manufacturer": manuf,
            "score": score, "category": cat, "channel": ch}


def test_group_score_is_the_max_not_the_median():
    """一个爆款带一堆平庸品的品牌,整体不该掉下去 —— 那正是最该抢的品牌。"""
    g = ag.build([_c("B1", "acme", 95.0), _c("B2", "acme", 40.0),
                  _c("B3", "acme", 38.0)])["free"]
    assert len(g) == 1 and g[0]["score"] == 95.0 and g[0]["size"] == 3


def test_minority_channel_items_are_dropped_from_the_group():
    """⚠ 店铺一店一渠道,而品牌必须整组去一家店。

    混渠道品牌里不合该店渠道的那部分**上不了架**。留在组里会让 size 虚高 ——
    配额被吃掉,货却上不去,而且方案表上看不出来。
    """
    r = ag.build([_c("B1", "acme", 90.0, ch="FBA"), _c("B2", "acme", 88.0, ch="FBA"),
                  _c("B3", "acme", 99.0, ch="FBM")])
    g = r["free"][0]
    assert g["channel"] == "FBA" and g["size"] == 2
    assert [x["asin"] for x in g["items"]] == ["B1", "B2"]
    # 组分要按**留下的**算:99 分那个是 FBM,它上不了这家店的架
    assert g["score"] == 90.0
    assert r["dropped"]["渠道少数派(随品牌走不了)"] == 1


def test_group_with_no_usable_channel_is_dropped_whole():
    """渠道认不出就整组不发 —— **不猜**,猜错等于把 FBM 的货分给 FBA 店。"""
    r = ag.build([_c("B1", "acme", 90.0, ch=None), _c("B2", "acme", 80.0, ch=None)])
    assert r["free"] == [] and r["dropped"]["渠道未知(整组)"] == 2


def test_category_ties_are_broken_deterministically():
    """并列时按大类名定序。

    `Counter.most_common()` 并列时按插入序 —— 那取决于上游 SQL 的行序,
    等于给分配塞了个看不见的随机源,而占用撤不回。
    """
    a = ag.build([_c("B1", "acme", 9.0, cat="厨房"), _c("B2", "acme", 8.0, cat="家居")])
    b = ag.build([_c("B2", "acme", 8.0, cat="家居"), _c("B1", "acme", 9.0, cat="厨房")])
    assert a["free"][0]["category"] == b["free"][0]["category"]


def test_unbranded_products_each_become_their_own_group():
    """无品牌的品**每个 ASIN 自成一组**。

    合成一坨的话,它们会整体去一家店,而彼此之间毫无关系 —— 品牌排他管不着
    它们,却被当成一个品牌对待了。
    """
    r = ag.build([_c("B1", None, 90.0), _c("B2", "", 80.0), _c("B3", "n/a", 70.0)])
    assert len(r["free"]) == 3
    assert all(g["size"] == 1 and g["brand"] is None for g in r["free"])


def test_claimed_brands_go_to_the_directed_flow_with_their_store():
    """品牌已被占用 → 定向流,只能去占用店(§7.3),不进自由流的分层。"""
    r = ag.build([_c("B1", "acme", 90.0), _c("B2", "zeta", 80.0)],
                 claimed_brands={"acme": "A085"})
    assert [g["key"] for g in r["free"]] == ["zeta"]
    assert len(r["directed"]) == 1 and r["directed"][0]["store"] == "A085"


def test_manufacturer_is_the_brand_fallback():
    """品牌键的算法只有 brand_key 一处 —— 这里不许另写一遍。"""
    r = ag.build([_c("B1", None, 90.0, manuf="Acme Inc"),
                  _c("B2", "acme inc", 80.0)])
    assert len(r["free"]) == 1 and r["free"][0]["size"] == 2


def test_groups_are_emitted_in_a_stable_order():
    """同样的输入必须给同样的输出 —— dry-run 看到的就是 --execute 会做的。"""
    cs = [_c("B1", "zeta", 90.0), _c("B2", "acme", 80.0), _c("B3", "mid", 85.0)]
    assert ([g["key"] for g in ag.build(cs)["free"]]
            == [g["key"] for g in ag.build(list(reversed(cs)))["free"]])
