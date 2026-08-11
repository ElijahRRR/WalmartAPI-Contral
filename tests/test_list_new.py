"""listing L2d 后半回归:回执四集合、上架表反哺器、list_new 闸门链。"""

import contextlib

from api import feishu
from registry import resources
from registry.resources import Spreadsheet
from services import feed_track, listing_sheet
from workflows import list_new as ln


def test_classify_receipt_priority():
    c = listing_sheet.classify_receipt
    # SKU_LOCKED 优先于一切(即使 status=success)
    assert c("success", "ERR_EXT_DATA_0101211\t")[0] == "SKU_LOCKED"  # 尾部 \t 实证
    # 异步审核假错误绝不当失败(即使 status=failed)
    assert c("failed", "EXT_DATA_ERROR_56026862530206")[0] == "ASYNC_PENDING"
    assert c("success", "")[0] == "SUCCESS"
    assert c("success", "SOME_WARN") == ("SUCCESS_WITH_WARNING", "SOME_WARN")
    assert c("failed", "ERR_X") == ("FAILED", "ERR_X")
    assert c("missing", "")[0] == "MISSING"
    assert c("submitted", "")[0] == "处理中"


def _sheet_row(rownum, **kw):
    d = dict(zip(resources.LISTING_SHEET.columns, [""] * 21))
    d.update({"rownum": rownum, "asin": f"B0ASIN{rownum:04d}", "store": "T1",
              "product_type": "Cups", "audit_result": "pass"})
    d.update(kw)
    return d


def test_listing_reflector_writes_opq(monkeypatch):
    monkeypatch.setattr(resources, "LISTING_SHEET",
                        Spreadsheet(name="上架表", token="TOK", sheet_id="SID",
                                    columns=resources.LISTING_SHEET.columns))
    rows = [_sheet_row(2, feed_id="F1", listed="Yes", list_result="处理中"),
            _sheet_row(3, feed_id="F1", listed="Yes", list_result="ASYNC_PENDING")]
    monkeypatch.setattr(listing_sheet, "read_rows", lambda: rows)
    writes = []
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda s, ups: (writes.extend(ups), len(ups))[1])
    ledger = {"F1": {rows[0]["asin"]: ("success", ""),
                     rows[1]["asin"]: ("success", "")}}
    monkeypatch.setattr(feed_track, "item_results", lambda fid: ledger[fid])
    monkeypatch.setattr(feed_track, "item_errors", lambda fid: {})
    monkeypatch.setattr(feed_track, "item_codes", lambda fid: {})
    out = listing_sheet.sync_from_ledger()
    w = {rng: vals[0] for rng, vals in writes}
    assert w["O2:Q2"][0] == "SUCCESS"
    assert w["O3:Q3"][0] == "SUCCESS"        # ASYNC 转正
    assert "回填 2 行" in out


def test_list_new_dry_run_gate_chain(monkeypatch):
    rows = [
        _sheet_row(2),                                    # 走到"待数据源"
        _sheet_row(3, product_type="BannedPT"),           # 风控拦截
        _sheet_row(4, asin="B0LISTED01"),                 # 全局去重
        _sheet_row(5, asin="B0RISKY001"),                 # 防呆
        _sheet_row(6, product_type="NoSpecPT"),           # PT 无 spec
        _sheet_row(7, store="T_OFF"),                     # 非 ACTIVE 店
        _sheet_row(8, listed="Yes"),                      # 已上架不领任务
        _sheet_row(9, list_result="SKU_LOCKED"),          # 永久跳过
    ]
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "_load_gate_state", lambda: (
        {"T_OFF"}, {}, {"B0LISTED01"}, {"B0RISKY001": (2, 1, 3, None)},
        {"banned_pts": {"BannedPT"}, "brands": set()}))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln, "_load_multipliers", lambda: {})
    monkeypatch.setattr(ln.stores_svc, "load_stores", lambda names=None: [
        {"name": "T1"}, {"name": "T_OFF"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt",
                        lambda pt: None if pt == "NoSpecPT" else {"properties": {}})
    fetched = {}
    monkeypatch.setattr(ln.amz_source, "fetch_products",
                        lambda asins: (fetched.setdefault("asins", asins), {})[1])
    monkeypatch.setattr(ln.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("dry-run 不许提交")))

    out = ln.run({"execute": False})
    assert "待上架 6" in out          # 8 行中 K=Yes 与 SKU_LOCKED 不领任务
    assert "非ACTIVE店 1" in out and "风控拦截 1" in out
    assert "去重 1" in out and "防呆 1" in out and "PT无spec 1" in out
    assert "待数据源 1" in out
    assert fetched["asins"] == [rows[0]["asin"]]   # 只有过全闸的行才拉数据


def test_risk_reason_carries_evidence():
    """防呆理由带证据(计数/最近移除时间),不再只写"有删除史"四个字。"""
    from datetime import datetime
    r = ln._risk_reason(2, 1, 3, datetime(2026, 7, 30))
    assert "提交删除2次" in r and "删除未生效1次" in r
    assert "历史上架3次" in r and "最近移除2026-07-30" in r
    # 空缺列不硬凑:没有未生效/上架史/时间就不出现对应片段
    assert ln._risk_reason(1, 0, 0, None) == "防呆:该ASIN有删除史(提交删除1次)"


def test_error_desc_joined_into_p_column(monkeypatch):
    """P 列写「码 | 人话」——数字错误码本身不含任何可修的信息。"""
    monkeypatch.setattr(resources, "LISTING_SHEET",
                        Spreadsheet(name="上架表", token="TOK", sheet_id="SID",
                                    columns=resources.LISTING_SHEET.columns))
    rows = [_sheet_row(2, feed_id="F9", listed="Yes", list_result="处理中")]
    monkeypatch.setattr(listing_sheet, "read_rows", lambda: rows)
    writes = []
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda s, ups: (writes.extend(ups), len(ups))[1])
    monkeypatch.setattr(feed_track, "item_results",
                        lambda fid: {rows[0]["asin"]: ("failed", "EXT_DATA_ERROR_1")})
    monkeypatch.setattr(feed_track, "item_errors",
                        lambda fid: {rows[0]["asin"]: "[price] must be > 0"})
    monkeypatch.setattr(feed_track, "item_codes", lambda fid: {})
    listing_sheet.sync_from_ledger()
    o, p, _ = writes[0][1][0]
    assert o == "FAILED"
    assert p == "EXT_DATA_ERROR_1 | [price] must be > 0"


def test_feed_track_error_text_shape():
    from services.feed_track import error_text
    assert error_text([{"field": "price", "description": "bad"}]) == "[price] bad"
    assert error_text([{"description": "x"}, {"description": "y"}]) == "x; y"
    assert error_text([{"code": "C1"}]) == ""       # 没描述就是空,不塞码
    assert error_text([]) == ""


def test_upc_conflict_marked_orthogonally(monkeypatch):
    """ERR_EXT_DATA_0101119:UPC 全站撞库 → 池标冲突永久弃用(与主分类正交)。"""
    monkeypatch.setattr(resources, "LISTING_SHEET",
                        Spreadsheet(name="上架表", token="TOK", sheet_id="SID",
                                    columns=resources.LISTING_SHEET.columns))
    rows = [_sheet_row(2, feed_id="F1", listed="Yes", list_result="处理中")]
    asin = rows[0]["asin"]
    monkeypatch.setattr(listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(feishu, "sheet_write_ranges", lambda s, ups: len(ups))
    monkeypatch.setattr(feed_track, "item_results",
                        lambda fid: {asin: ("failed", "ERR_EXT_DATA_0101119")})
    monkeypatch.setattr(feed_track, "item_errors", lambda fid: {asin: "[QARTH] 已存在"})
    # 一个 SKU 可能多码并存:必须看全集,不能只看 error_code 里的第一个
    monkeypatch.setattr(feed_track, "item_codes",
                        lambda fid: {asin: {"EXT_DATA_ERROR_9", "ERR_EXT_DATA_0101119"}})
    marked = []
    monkeypatch.setattr(listing_sheet, "_mark_upc_conflicts",
                        lambda a: (marked.extend(a), len(a))[1])
    out = listing_sheet.sync_from_ledger()
    assert marked == [asin]
    assert "UPC 撞库 1 个已标冲突" in out


def test_failed_rows_requeue_until_cap(monkeypatch):
    """FAILED 行要重新排队(UPC 撞库领新号即可修);超上限则停手。"""
    rows = [
        _sheet_row(2, asin="B0RETRY01", list_result="FAILED",
                   feed_id="F1", listed="Yes"),          # 试过 1 次 → 重试
        _sheet_row(3, asin="B0CAPPED01", list_result="FAILED",
                   feed_id="F2", listed="Yes"),          # 试过 3 次 → 停手
        _sheet_row(4, asin="B0LOCKED01", list_result="SKU_LOCKED",
                   feed_id="F3", listed="Yes"),          # 永不重试
        _sheet_row(5, asin="B0ASYNC001", list_result="ASYNC_PENDING",
                   feed_id="F4", listed="Yes"),          # 不是失败
    ]

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def execute(self, sql, args):
            # 假游标抓不到 SQL 语法错(2026-08-09 踩过:psycopg3 不支持
            # `(a,b) IN %s`),至少钉住参数形状是两个等长数组
            assert "IN %s" not in sql, "psycopg3 不支持元组序列 IN"
            assert isinstance(args, tuple) and len(args) == 2
            assert isinstance(args[0], list) and len(args[0]) == len(args[1])
            self.args = args

        def fetchall(self):
            return [("T1", "B0RETRY01", 1), ("T1", "B0CAPPED01", 3)]

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()

    monkeypatch.setattr(ln.db, "pg_conn", lambda: _Conn())
    retry, exhausted = ln._retry_rows(rows)
    assert [r["asin"] for r in retry] == ["B0RETRY01"]
    assert exhausted == [("T1", "B0CAPPED01")]
    # 重新排队的行要看起来像新行(主链才会走领 UPC → 提交)
    assert retry[0]["feed_id"] == "" and retry[0]["list_result"] == ""
    assert ln._retry_rows([_sheet_row(2)]) == ([], [])


def test_list_new_skips_when_shipping_missing(monkeypatch):
    """运费没采到 ⇒ 落地价算不出来 ⇒ 不上架(所有者定稿 2026-08-10)。

    与"配送方式未知不定价"同一个道理:当 0 定出来的价偏低,越贵的运费亏得
    越多,而上架成功、价格看着也正常,两侧都不会报错。
    """
    rows = [_sheet_row(rownum=2, store="T1", asin="B0HASSHIP"),
            _sheet_row(rownum=3, store="T1", asin="B0NOSHIP")]
    base = {"title": "T", "price": 20.0, "stock": 50,
            "stock_state": "in_stock", "lead_days": 2, "channel": "FBM"}
    products = {
        "B0HASSHIP": {**base, "asin": "B0HASSHIP", "shipping": 3.0},
        "B0NOSHIP": {**base, "asin": "B0NOSHIP", "shipping": None},
    }
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "_load_gate_state", lambda: (
        set(), {}, set(), {}, {"banned_pts": set(), "brands": set()}))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln, "_load_multipliers",
                        lambda: {"T1": {"fbm_range1": "200%"}})
    monkeypatch.setattr(ln.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: products)

    out = ln.run({"execute": False})
    assert "运费未采到" in out
    assert "共 1 行将进入" in out        # 只有带运费那行进得去
