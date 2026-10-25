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


# ── 多段写入按段位次分组:让写通道能把同列连续行粘成一段(2026-10-20)────────

def test_rows_ranges_groups_segments_by_position_and_keeps_input_order():
    """一行拆成两段时,输出是"每行第 1 段"全部在前、"每行第 2 段"全部在后。

    逐行交错(A2、C2:D2、A3、C3:D3)时写通道一段都粘不起来;分组后同列首尾相接。
    段的先后由字段顺序决定:这里把 store 放最后,它所在的段就最后。
    同一行在一次调用里出现两次时,后写的仍在后(稳定)—— 覆盖语义不变。
    """
    s = _sheet({"store": "店铺", "asin": "ASIN", "brand": "品牌", "score": "产品分"})
    idx = {"store": 1, "asin": 2, "brand": 3, "score": 4}
    order = ["brand", "score", "store"]
    got = sheet_layout.rows_ranges(s, idx, [(2, order, ["a", 1, "S2"]),
                                            (3, order, ["b", 2, "S3"]),
                                            (2, order, ["a2", 9, "S2b"])])
    assert [r for r, _ in got] == ["C2:D2", "C3:D3", "C2:D2", "A2:A2", "A3:A3", "A2:A2"]
    assert [v for _, v in got][2] == [["a2", 9]] and [v for _, v in got][5] == [["S2b"]]
    # row_ranges 就是"每行同一组字段"的 rows_ranges
    assert sheet_layout.row_ranges(s, idx, [(2, ["a", 1, "S2"]), (3, ["b", 2, "S3"])],
                                   order) == got[:2] + got[3:5]


def _listing_requests(monkeypatch, write):
    """输入:一次上架表写入 → 输出:真实写通道发出的每个请求里的范围列表。"""
    from registry import resources
    from registry.resources import Spreadsheet
    from api import feishu
    monkeypatch.setattr(resources, "LISTING_SHEET", Spreadsheet(
        name="上架表", token="TOK", sheet_id="SID",
        columns=resources.LISTING_SHEET.columns,
        headers=dict(resources.LISTING_SHEET.headers)))
    batches = []
    monkeypatch.setattr(feishu, "_post_batch",
                        lambda s, chunk: batches.append([r for r, _ in chunk]))
    monkeypatch.setattr(feishu.time, "sleep", lambda x: None)
    write()
    return batches


_EIGHT = ["标题", "9.9", 3, "19.9", "Yes", "F1", "2026-10-20", ""]


def test_list_new_submit_write_is_one_request_for_two_thousand_rows(monkeypatch):
    """list_new 回写提交结果:标题(D)+ amz价~feed查询日期(J:S)两段。

    提交成功的行同一次清掉回执三列(Q:S,2026-10-24),与 J:P 相邻、粘在同一段。

    改前逐行交错,2000 行 40 个请求;分组后 1 个请求(4000 行,在 4750 预算内)。
    含 SKU 的行 SKU 与标题相邻,粘成 C:D 段,与不带 SKU 的行各自成段。
    """
    rows = range(2, 2002)
    batches = _listing_requests(monkeypatch, lambda: listing_sheet.write_submit_cols(
        [(r, _EIGHT + ["A0X1Y2Z3W4V5"]) for r in rows]))
    assert batches == [["C2:D2001", "J2:S2001"]]
    batches = _listing_requests(monkeypatch, lambda: listing_sheet.write_data_cols(
        [(r, ["标题", "9.9", 3, "19.9"]) for r in rows]))
    assert batches == [["D2:D2001", "J2:L2001"]]


def test_sku_lands_before_listed_even_across_requests(monkeypatch):
    """SKU 必须先于「是否上架/feedid」落地(否则会出现「已提交但无码」的行,
    回执反哺器靠 SKU 找行,那一行永远回填不上)。

    6000 行要分好几个请求;中途失败只会丢后面的请求,所以整个发送序列里
    含 SKU 列的段必须全部排在含「是否上架」列的段前面。
    """
    rows = range(2, 6002)
    batches = _listing_requests(monkeypatch, lambda: listing_sheet.write_submit_cols(
        [(r, _EIGHT + ["A0X1Y2Z3W4V5"]) for r in rows]))
    flat = [r for b in batches for r in b]
    assert len(batches) > 1
    lay = listing_sheet.layout()
    has = lambda rng, col: rng.split(":")[0].rstrip("0123456789") <= col \
        <= rng.split(":")[1].rstrip("0123456789")
    last_sku = max(i for i, r in enumerate(flat) if has(r, lay["sku"]))
    first_listed = min(i for i, r in enumerate(flat) if has(r, lay["listed"]))
    assert last_sku < first_listed
