"""按表头名认列的**共用算法**(services/sheet_layout)回归。

2026-09-17 点名分配要第二张按表头认列的表(产品分配表),算法从上架表积木里
搬出来共用。这里钉三件事:① 两张表都走这一份,没有第二份实现;② 宽容口径
(空白 / 大小写 / 全半角括号)只放过"不会写错列"的差别;③ 不相邻的字段拆段。
上架表自己的行为面(打乱列序 / 缺列 / 重名 / 不碰人工列)在
tests/test_sku_guard.py 那一节,没搬过来 —— 它们照旧盯着 listing_sheet 的公开函数。
"""

import inspect

import pytest

from registry.resources import Spreadsheet
from services import alloc_sheet, listing_sheet, sheet_layout


def _sheet(headers: dict, name="测试表") -> Spreadsheet:
    return Spreadsheet(name=name, token="TOK", sheet_id="SID",
                       columns=tuple(headers), headers=dict(headers))


_H = {"store": "店铺", "asin": "ASIN", "super_category": "商品品类(五大类)",
      "gross": "窗口销售额(毛额)"}


def test_both_sheets_resolve_columns_through_the_one_shared_algorithm():
    """上架表与产品分配表的 `_index_map` 都只是「缓存 + 调 sheet_layout.resolve」。

    任何一边自己再长出一套认列逻辑,就是双轨:表头比对的宽容口径迟早在两边
    漂开,而错位是静默的。
    """
    for mod in (listing_sheet, alloc_sheet):
        src = inspect.getsource(mod._index_map)
        assert "sheet_layout.resolve(" in src, mod.__name__
        assert "casefold" not in src and "split()" not in src, mod.__name__
    assert listing_sheet.HeaderMismatch is sheet_layout.HeaderMismatch


def test_fullwidth_parentheses_in_a_header_do_not_stop_the_run():
    """所有者手敲的表头把「商品品类(五大类)」打成中文括号,不该 fail-closed。

    宽容口径只放过**不会让值写错列**的差别:空白、大小写、全/半角括号。
    """
    cells = ["店铺", "asin", "商品品类(五大类)", "窗口销售额 (毛额)"]
    idx = sheet_layout.resolve(_sheet(_H), cells)
    assert idx == {"store": 1, "asin": 2, "super_category": 3, "gross": 4}


def test_missing_or_duplicated_header_names_the_sheet_and_fails_closed():
    """缺列 / 重名 → 抛 HeaderMismatch,报错里带**这张表的名字**与缺的表头原文。

    两张表共用一份算法之后,报错不点名是哪张表,人会跑去核对错的那张。
    """
    with pytest.raises(sheet_layout.HeaderMismatch, match="「产品分配表」.*窗口销售额"):
        sheet_layout.resolve(_sheet(_H, name="产品分配表"),
                             ["店铺", "ASIN", "商品品类(五大类)"])
    with pytest.raises(sheet_layout.HeaderMismatch, match="重复"):
        sheet_layout.resolve(_sheet(_H), ["店铺", "ASIN", "ASIN",
                                          "商品品类(五大类)", "窗口销售额(毛额)"])
    # 两个基类都要:两侧调用方各按一种捕
    assert issubclass(sheet_layout.HeaderMismatch, LookupError)
    assert issubclass(sheet_layout.HeaderMismatch, ValueError)


def test_extra_columns_only_warn(caplog):
    """所有者自己加的列不算错:只告警,登记的列一个不错位。"""
    import logging
    with caplog.at_level(logging.WARNING, logger="services.sheet_layout"):
        idx = sheet_layout.resolve(_sheet(_H), ["备注", "店铺", "ASIN",
                                                "商品品类(五大类)", "窗口销售额(毛额)"])
    assert idx["store"] == 2 and idx["gross"] == 5
    assert "备注" in caplog.text


def test_ranges_split_where_fields_are_not_adjacent():
    """相邻字段粘成一段,中间隔着别人的列就拆段 —— 值一个都不落到隔壁列。

    产品分配表今天就是这个形状:「店铺」在 A、ASIN(人工列)在 B、
    机器域其余列从 C 起,一行写入必须是 A 段 + C.. 段两段。
    """
    idx = {"store": 1, "asin": 2, "brand": 3, "score": 4}
    s = _sheet({"store": "店铺", "asin": "ASIN", "brand": "品牌", "score": "产品分"})
    got = sheet_layout.ranges(s, idx, 7, ["store", "brand", "score"],
                              [["A085", "acme", 88.5]])
    assert got == [("A7:A7", [["A085"]]), ("C7:D7", [["acme", 88.5]])]
    # 多行同形:一段覆盖连续行
    got = sheet_layout.ranges(s, idx, 2, ["brand", "score"],
                              [["a", 1], ["b", 2]])
    assert got == [("C2:D3", [["a", 1], ["b", 2]])]
    with pytest.raises(LookupError, match="没有这些字段"):
        sheet_layout.ranges(s, idx, 2, ["nope"], [["x"]])


def test_letters_and_header_scan_use_the_api_layers_column_letters():
    """列字母只从 api/feishu._col_letter 出(27 → AA 这种进位别再写第二遍)。"""
    assert sheet_layout.letters({"a": 1, "b": 26, "c": 27}) == {"a": "A", "b": "Z", "c": "AA"}
