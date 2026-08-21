"""分配引擎依赖的 registry 登记项回归(飞书改表头 = 这里先红)。"""

from registry import resources


def test_store_target_and_channel_fields_registered():
    """限额表四列:目标三列(日目标/容量上限)+ 配送限制(一店一渠道权威)。"""
    f = resources.RETIRE_LIMITS.fields
    assert f.target_gmv_daily == "目标销售额"
    assert f.target_orders_daily == "目标订单"
    assert f.max_online == "单店最大在线数"
    assert f.channel_limit == "配送限制"


# ── 沃尔玛五大品类(所有者 2026-08-21 拍板)────────────────────────────

def test_super_categories_cover_every_category_the_pool_actually_has():
    """库里出现过的 Walmart Category,除刻意「不归」的两个外都要有归属。

    漏一个的后果是静默的:`super_category` 返回 None,那条货会被当成
    "归不到品类"处理 —— 和 Safety & Emergency 走同一条路,却没人知道
    它其实只是漏填了。
    """
    seen = {  # 2026-08-21 生产库 + 准入明细里出现过的全部 26 个值
        "Home", "Home Improvement", "Furniture", "Arts & Crafts",
        "Garden & Patio", "Office", "Sporting Goods", "Household",
        "Occasion & Seasonal", "Business & Industrial", "Animals",
        "Musical Instruments", "Safety & Emergency", "Fashion", "Vehicles",
        "Media", "Health & Personal Care", "Electronics", "Beauty",
        "Photography", "Everything Else", "Toys", "Food & Beverage", "Baby",
        "Sports & Outdoors", "Automotive",
    }
    unmapped = {c for c in seen if resources.super_category(c) is None}
    assert unmapped == {"Safety & Emergency", "Everything Else"}


def test_the_four_categories_the_owner_ruled_on_by_name():
    """所有者 2026-08-21 逐条点名回的四个,钉死防漂。"""
    assert resources.super_category("Musical Instruments") == "ETS"
    assert resources.super_category("Business & Industrial") == "Hardlines"
    # 「不归」是一条口径不是漏填:这两类只能分给**没有确定类目**的店,
    # 正好落在 store_targets.allowed 已有的「归不到大类的,受限店拒收」上
    assert resources.super_category("Safety & Emergency") is None
    assert resources.super_category("Everything Else") is None


def test_super_category_values_are_the_five_from_the_official_deck():
    """映射的值域只能是官方那五个 —— 多冒出一个就是有人顺手自创了品类。"""
    vals = set(resources._SUPER_CATEGORY_OF.values())
    assert vals == set(resources.WALMART_SUPER_CATEGORIES)


def test_unknown_and_blank_categories_never_fall_back_to_a_default():
    """None 不许兜成某个默认品类 —— 那会把"谁都能收"变成"某家专收",正好反了。"""
    for junk in (None, "", "   ", "Nonesuch Category"):
        assert resources.super_category(junk) is None
