"""类目建议与不符清单:**判据在品类层,证据在 26 类层**(所有者 2026-08-22)。

这两处此前是全链唯一还按 26 类出结果的地方,而闸门早已判品类层。后果不是
难看:所有者照着 26 类建议填「Home」+「Furniture」,以为开了两类,闸看见的
是同一个 Home —— 他做了一件自以为有效、实际什么都没变的事。

`suggest_categories` 在改之前**一条测试都没有**,这就是它悄悄跟闸门脱节的原因。
"""

from collections import Counter

from registry import resources
from services import alloc_survey as sv


def _prof(**cats):
    return {"categories": Counter(cats), "n": sum(cats.values()), "published": 0}


def _one(prof, cfg=None):
    out = sv.suggest_categories(prof, cfg or {})
    assert len(out) == 1
    return out[0]


# ── 建议出在品类层 ────────────────────────────────────────────────────

def test_suggestion_is_a_super_category_not_a_walmart_category():
    """一家店卖 Furniture 218 + Home 96 + Office 40:26 类口径会建议
    「Furniture|Home」(其实是同一个品类),品类口径建议「Home|Hardlines」。"""
    r = _one({"A": _prof(Furniture=218, Home=96, Office=40)})
    assert r["suggest"] == ["Home", "Hardlines"]
    assert all(v in resources.SUPER_BUCKETS for v in r["suggest"])


def test_super_counts_are_summed_across_the_walmart_categories_under_them():
    """品类件数是它底下 26 类的**和** —— 不是取最大的那个。

    不求和的话:Home 96 单独一项会输给 Office 40 + Sporting Goods 30 = 70?
    不会 —— 但 Furniture 60 + Home 50 = Home 110 会输给 Office 100,而
    真相是这家店 Home 做得更多。取最大等于按最大的那个 26 类排名次。
    """
    r = _one({"A": _prof(Furniture=60, Home=50, Office=100)})
    assert r["by_super"][0] == ("Home", 110)
    assert r["suggest"][0] == "Home"


def test_walmart_categories_are_kept_as_evidence():
    """26 类不作废:只说"建议 Hardlines"而不说它由哪几个 26 类堆出来,
    所有者没法判断这个建议合不合理。"""
    r = _one({"A": _prof(**{"Home Improvement": 218, "Garden & Patio": 96})})
    assert r["suggest"] == ["Hardlines"]
    assert r["by_category"] == [("Home Improvement", 218), ("Garden & Patio", 96)]


def test_unmapped_categories_are_suggested_as_other():
    """归不到五品类的货 → 建议填「其他」,而不是消失。

    消失的后果:一家专卖 Everything Else 的店会被建议成"没有类目",
    而它明明有一个确切的准入口径。
    """
    r = _one({"A": _prof(**{"Everything Else": 300, "Safety & Emergency": 50})})
    assert r["suggest"] == [resources.SUPER_OTHER]


def test_unclassified_never_becomes_a_suggestion():
    """「(未归类)」是数据缺口,不是可填的类目 —— 不许进建议。"""
    prof = {"A": {"categories": Counter({sv.UNCLASSIFIED: 900, "Home": 10}),
                  "n": 910, "published": 0}}
    r = _one(prof)
    assert r["suggest"] == ["Home"]
    assert all(c != sv.UNCLASSIFIED for c, _ in r["by_super"])


def test_top_is_two_by_default():
    r = _one({"A": _prof(Home=50, Office=40, Toys=30, Beauty=20)})
    assert len(r["suggest"]) == 2 and r["suggest"] == ["Home", "Hardlines"]


# ── 对拍 ──────────────────────────────────────────────────────────────

def test_filled_is_compared_after_folding_to_super():
    """⚠ 对拍必须在品类层比。拿 26 类原文去比品类建议,**永远不一致** ——
    那一栏于是全是红的,所有者学会忽略它,真正不一致的店也就看不见了。"""
    r = _one({"A": _prof(Furniture=218, Home=96)},
             {"A": {"categories": ["Furniture"]}})
    assert r["filled"] == ["Furniture"]
    assert r["filled_super"] == ["Home"]
    assert r["suggest"] == ["Home"]          # 折完两边一致


def test_two_filled_values_that_collapse_to_one_super_are_visible():
    """填了两格、其实只开了一个品类 —— 这正是 26 类建议会诱导所有者做的事。
    `filled` 与 `filled_super` 并排放,才看得出「填了 2 个 = 开了 1 个」。"""
    r = _one({"A": _prof(Home=10)}, {"A": {"categories": ["Home", "Furniture"]}})
    assert len(r["filled"]) == 2 and r["filled_super"] == ["Home"]


# ── 认不出的填写值 ────────────────────────────────────────────────────

def test_unrecognised_filled_values_are_named():
    """`super_bucket` 把认不出的值折进「其他」而不是丢掉(丢掉会静默废掉
    一家店)。代价是拼写错变成"只收其他",所以**必须点名**,否则就是
    静默改准入。"""
    r = _one({"A": _prof(Home=10)},
             {"A": {"categories": ["Home", "Hoem", "家居"]}})
    assert r["unknown"] == ["Hoem", "家居"]
    assert resources.SUPER_OTHER in r["filled_super"]


def test_the_two_unmapped_walmart_categories_are_not_flagged_as_unknown():
    """Safety & Emergency / Everything Else 是**合法的** Walmart Category,
    只是不归五品类。把它们点名成"认不出"会让那一栏出现两个其实填对的值,
    人就学会忽略整栏了。"""
    r = _one({"A": _prof(Home=10)},
             {"A": {"categories": ["Safety & Emergency", "Everything Else"]}})
    assert r["unknown"] == []
    assert r["filled_super"] == [resources.SUPER_OTHER]


# ── 「碎不碎」按品类数 ────────────────────────────────────────────────

def test_fragmentation_is_counted_in_super_categories():
    """按 26 类数,47 家有货店里 41 家"超 2 大类";而它们绝大多数只是
    Home + Hardlines 两个品类。报 26 类等于给所有者一份他无从下手的名单。"""
    p = _prof(Home=10, Furniture=10, **{"Arts & Crafts": 10,
                                        "Home Improvement": 10,
                                        "Garden & Patio": 10})
    assert len(sv.real_cats(p)) == 5
    assert sv.real_supers(p) == ["Home", "Hardlines"]


def test_real_supers_drops_unclassified_but_keeps_other():
    p = {"categories": Counter({sv.UNCLASSIFIED: 5, "Everything Else": 3,
                                "Home": 2})}
    assert sv.real_supers(p) == [resources.SUPER_OTHER, "Home"]
