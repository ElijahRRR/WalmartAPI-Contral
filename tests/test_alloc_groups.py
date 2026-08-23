"""品牌组组装回归。三个决定都会**静默**把货分错地方,所以逐条钉死。"""

from services import alloc_groups as ag


def _c(asin, brand, score, cat="家居", ch="FBA", manuf=None):
    return {"asin": asin, "brand": brand, "manufacturer": manuf,
            "score": score, "category": cat, "channel": ch}


def test_group_score_is_weighted_average_plus_a_slice_of_the_best():
    """组分 = 0.7×均分 + 0.3×最高(所有者 2026-08-22 拍 Q1)。

    ⚠ **这条改过,理由跟着切口一起变了。**原来是纯取最高分,道理是"一个爆款
    带一堆平庸品的品牌整体不该掉下去,那正是最该抢的品牌" —— 那个道理成立的
    **前提是切口在组这一层做**。切口挪到产品层之后,平庸品在切口那一步就挡住
    了,进片的本来就只有好品;而纯取最高分反而会把"一个 95 分带一堆 40 分"的
    组整体拉到前排(所有者原话:"你会把大量排名靠后的产品拉到前面来")。
    保留 0.3 的最高分权重:有爆款的品牌仍然优先,只是不再一俊遮百丑。
    """
    g = ag.build([_c("B1", "acme", 95.0), _c("B2", "acme", 40.0),
                  _c("B3", "acme", 38.0)])["free"]
    assert len(g) == 1 and g[0]["size"] == 3
    # 0.7×(95+40+38)/3 + 0.3×95 = 40.37 + 28.5
    assert round(g[0]["score"], 2) == 68.87
    assert round(ag.group_score([{"score": 50}] * 4), 2) == 50.0   # 同分 ⇒ 就是它


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
    assert round(g["score"], 1) == round(0.7 * 89.0 + 0.3 * 90.0, 1) == 89.3
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


def test_group_lead_is_the_slowest_item():
    """组必须整组去一家店,所以货期取**最长** —— 那才保证"这家店收得了每一件"。"""
    g = ag.build([_c("B1", "acme", 90.0), _c("B2", "acme", 80.0)],)["free"][0]
    assert g["lead"] is None                 # 夹具默认没有 lead
    g2 = ag.build([dict(_c("B1", "z", 90.0), lead=3),
                   dict(_c("B2", "z", 80.0), lead=9)])["free"][0]
    assert g2["lead"] == 9


def test_one_unknown_lead_makes_the_whole_group_unknown():
    """任一件采不到就整组未知 —— 受限店会拒收,宁可不分也不错分。"""
    g = ag.build([dict(_c("B1", "z", 90.0), lead=3),
                  dict(_c("B2", "z", 80.0), lead=None)])["free"][0]
    assert g["lead"] is None
