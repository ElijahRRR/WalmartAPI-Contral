"""每张 csv 都要能看出品类(所有者 2026-08-22:「现在有些有,有些没有,我看不清晰」)。

分配链一共九张 csv,凡是逐行带类目的都必须**大类(26类)与品类(五大类)并排**:
26 类是产品侧事实,品类是**闸门真正判的那一层**。只给 26 类,人没法跟飞书
限额表的准入列对照;只给品类,又看不出这个 Hardlines 是由哪几个 26 类堆出来的。

⚠ 这条测的是**表头本身**,不是某一次输出 —— 下次新加一张表漏了品类列,
   这里会红。
"""

import inspect

import pytest

from registry import resources
from workflows import alloc_audit, alloc_plan, alloc_products, claim_audit


def test_super_label_always_returns_something_printable():
    """报表列不许出现空白:空白读起来像"这一格没算",而不是"大类没采到"。"""
    assert resources.super_label("Furniture") == "Home"
    assert resources.super_label("Everything Else") == resources.SUPER_OTHER
    assert resources.super_label(None) == resources.UNKNOWN_SUPER
    assert resources.super_label("") == resources.UNKNOWN_SUPER
    assert resources.super_label("Hoem") == resources.SUPER_OTHER


def test_unknown_super_is_not_the_same_as_other():
    """⚠ 「(大类未知)」≠「其他」。

    「其他」是**业务归类**(Safety & Emergency / Everything Else),处置是
    找一家收「其他」的店;「大类未知」是**数据缺口**,处置是补一次采集。
    在表上写成同一个词,人就分不出该做哪件事。
    """
    assert resources.UNKNOWN_SUPER != resources.SUPER_OTHER


@pytest.mark.parametrize("mod", [alloc_audit, alloc_plan, alloc_products,
                                 claim_audit])
def test_every_csv_header_pairs_the_two_taxonomy_levels(mod):
    """凡是出现「大类(26类)」的表头,同一处必须也有「品类(五大类)」。"""
    src = inspect.getsource(mod)
    assert src.count("大类(26类)") <= src.count("品类(五大类)"), (
        f"{mod.__name__}:有「大类(26类)」列却没有配套的「品类(五大类)」列")


def test_no_csv_header_still_uses_the_bare_word_daileiA():
    """「大类」不许再裸用 —— 26 类还是五大类,列名必须自己说清楚。

    裸用是这次混乱的根源:同一个词在不同表里指两层东西,所有者对不上。
    """
    for mod in (alloc_audit, alloc_plan, alloc_products, claim_audit):
        src = inspect.getsource(mod)
        for bad in ('"大类"', "'大类'", '"商品大类"', '"组大类"'):
            assert bad not in src, f"{mod.__name__} 表头里还有裸的 {bad}"


def test_conflict_rows_are_named_not_positional():
    """明细行改成具名元组之后,消费方不许再按列号取。

    2026-08-22 给它插一列「大类」,三个消费方全是 `d[7] == "下架"` —— 插完
    全部错位**而且不报错**,只是把「保留/下架」读成一个数字。
    """
    from services import alloc_survey as sv
    assert sv.ConflictRow._fields == (
        "store", "sku", "asin", "category", "orders", "gmv",
        "cat_gmv", "store_gmv", "verdict")
    src = inspect.getsource(alloc_audit)
    assert "d[7]" not in src and "d[8]" not in src
