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
    # 内容标准拒(2026-08-19):文案是亚马逊原文,原样重发必然同拒 → 不重试
    assert c("failed", "EXT_DATA_ERROR_07705958490105")[0] == "CONTENT_REJECTED"
    assert c("success", "")[0] == "SUCCESS"
    assert c("success", "SOME_WARN") == ("SUCCESS_WITH_WARNING", "SOME_WARN")
    assert c("failed", "ERR_X") == ("FAILED", "ERR_X")
    assert c("missing", "")[0] == "MISSING"
    assert c("submitted", "")[0] == "处理中"


# 宽度从 registry 取:zip 在最短处截断,写死数字的话加了列的那天夹具行里
# 根本不会出现新键(row_sku 之类走 .get 的地方就静默看不到它)。
def _sheet_row(rownum, **kw):
    d = dict(zip(resources.LISTING_SHEET.columns,
                 [""] * len(resources.LISTING_SHEET.columns)))
    d.update({"rownum": rownum, "asin": f"B0ASIN{rownum:04d}", "store": "T1",
              "product_type": "Cups", "audit_result": "pass"})
    d.update(kw)
    return d


# 上架表 E 列 → catalog.products.audit_status 的对照(services.listing_sheet
# .AUDIT_RESULT_CN 的反向)。2026-08-16 起审核闸**读库不读表**,夹具沿用
# E 列写法只是为了让每行"该不该过审"仍然一眼可见。
_AUDIT_DB = {"pass": "approved", "reject": "rejected", "pending": "pending"}


def fake_verdicts(rows):
    """输入:上架表行 → 输出:假 catalog.products 的 {asin: (结论, PT)}。

    E 列为空 = 库里查不到那个 ASIN(键直接不存在),这正是"没审核过"的形状。
    """
    return {r["asin"]: (_AUDIT_DB[r["audit_result"]], r["product_type"] or None)
            for r in rows if r["asin"] and r["audit_result"]}


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
    # 回执三列 = 上架结果/报错/feed查询日期,range 由 layout 按表头名算
    assert w["Q2:S2"][0] == "SUCCESS"
    assert w["Q3:S3"][0] == "SUCCESS"        # ASYNC 转正
    assert "回填 2 行" in out


# ── 上架表 SKU 列(所有者 2026-09-02 表头重排,批次 1)───────────────────────

def test_row_sku_falls_back_to_the_asin_byte_for_byte():
    """SKU 列为空的存量行:`row_sku` 逐字节 = B 列 ASIN(与旧 sku=asin 约定同)。

    批次 1 只加列不写值,所以全表都走这条回落腿 —— 它一旦不等价,回执找行、
    退役载荷、mark_used 全部换了键,而且不报错。
    """
    asin = "B0GXX75JN5"
    assert listing_sheet.row_sku({"asin": asin}) == asin
    assert listing_sheet.row_sku({"sku": "", "asin": asin}) == asin
    assert listing_sheet.row_sku({"sku": "   ", "asin": asin}) == asin  # 空白=空
    assert listing_sheet.row_sku({}) == ""            # 窄读的行连键都没有
    # 有值就用它(批次 2 起的形态)
    assert listing_sheet.row_sku({"sku": "A0X1Y2Z3W4V5", "asin": asin}) \
        == "A0X1Y2Z3W4V5"


def test_write_submit_cols_ninth_value_writes_the_sku_column(monkeypatch):
    """八值 = 改造前同形;第 9 值 = 与 是否上架/feedid 同一次写出的 SKU。

    SKU 与提交结果必须同一次落地:分两次写、中间崩掉就留下「已提交但无码」
    的行,而回执反哺器正是靠 SKU 找行,那一行永远回填不上。
    """
    sent = []
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda s, ups: (sent.extend(ups), len(ups))[1])
    eight = ["标题", "9.9", 3, "19.9", "Yes", "F1", "2026-09-02", ""]
    listing_sheet.write_submit_cols([(2, eight)])
    assert [r for r, _ in sent] == ["D2:D2", "J2:P2"]      # 标题 + 提交七列
    assert sent[1][1] == [eight[1:]]
    sent.clear()
    listing_sheet.write_submit_cols([(3, eight + ["A0X1Y2Z3W4V5"])])
    # SKU 与标题在今天的表里相邻 ⇒ 粘成一段,值仍各归各位
    assert [r for r, _ in sent] == ["C3:D3", "J3:P3"]
    assert sent[0][1] == [["A0X1Y2Z3W4V5", "标题"]]
    sent.clear()
    listing_sheet.write_submit_cols([(4, eight + [""])])   # 空第 9 值不写
    assert [r for r, _ in sent] == ["D4:D4", "J4:P4"]


def test_clear_for_relist_never_clears_the_sku_column(monkeypatch):
    """清列恢复成"新行"时**不碰 SKU**:清列不是弃码点(弃码只有一个出口)。

    清掉 SKU 会让回执与退役都找不回这一行;而码的寿命由登记簿的
    abandoned_at 说了算(sku_plan §5.3)。
    """
    sent = []
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda s, ups: (sent.extend(ups), len(ups))[1])
    n = listing_sheet.clear_for_relist([2, 3])
    lay = listing_sheet.layout()
    assert n == 2 and [r for r, _ in sent] == ["M2:S2", "M3:S3"]
    assert all(not r.startswith(lay["sku"]) for r, _ in sent)
    assert sent[0][1] == [["", "", "", "SKU_LOCKED已退役,冷却完毕待重上(自愈链)",
                           "", "", ""]]


def test_read_rows_maps_cells_by_header_position(monkeypatch):
    """读取按**物理列号**回填字段名:所有者挪了列顺序也对得上。"""
    monkeypatch.setattr(feishu, "sheet_row_count", lambda s: 2)
    # 表头顺序打乱:SKU 挪到末列
    order = [c for c in resources.LISTING_SHEET.columns if c != "sku"] + ["sku"]
    monkeypatch.setattr(listing_sheet, "_LAYOUT",
                        {f: i for i, f in enumerate(order, 1)})
    cells = [""] * 21
    cells[0], cells[1], cells[20] = "T1", "B0AAAA0001", "A0X1Y2Z3W4V5"
    monkeypatch.setattr(feishu, "sheet_values_rows",
                        lambda s, c1, c2, rf, rt, **kw: [(2, cells)])
    got = listing_sheet.read_rows()
    assert got[0]["store"] == "T1" and got[0]["asin"] == "B0AAAA0001"
    assert got[0]["sku"] == "A0X1Y2Z3W4V5"
    assert listing_sheet.row_sku(got[0]) == "A0X1Y2Z3W4V5"


def test_receipt_lookup_uses_the_sku_column_when_present(monkeypatch):
    """回执找行按 `row_sku`:SKU 有值的行按真码找,存量行仍按 ASIN 找。"""
    monkeypatch.setattr(resources, "LISTING_SHEET",
                        Spreadsheet(name="上架表", token="TOK", sheet_id="SID",
                                    columns=resources.LISTING_SHEET.columns,
                                    headers=resources.LISTING_SHEET.headers))
    rows = [_sheet_row(2, feed_id="F1", listed="Yes", list_result="处理中",
                       sku="A0X1Y2Z3W4V5"),
            _sheet_row(3, feed_id="F1", listed="Yes", list_result="处理中")]
    monkeypatch.setattr(listing_sheet, "read_rows", lambda: rows)
    writes = []
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda s, ups: (writes.extend(ups), len(ups))[1])
    ledger = {"F1": {"A0X1Y2Z3W4V5": ("success", ""),
                     rows[1]["asin"]: ("success", "")}}
    monkeypatch.setattr(feed_track, "item_results", lambda fid: ledger[fid])
    monkeypatch.setattr(feed_track, "item_errors", lambda fid: {})
    monkeypatch.setattr(feed_track, "item_codes", lambda fid: {})
    out = listing_sheet.sync_from_ledger()
    assert {rng for rng, _ in writes} == {"Q2:S2", "Q3:S3"}   # 两行都回填上了
    assert "回填 2 行" in out


def test_heal_unknown_splits_the_ledger_key_from_the_upc_pool_key(monkeypatch):
    """自愈两个键各查各的表:台账/目录按 row_sku,UPC 池按 (店, ASIN)。

    批次 2 一通电两者就分叉,不拆键要么自愈永远查不到台账(行永久卡 Unknown、
    UPC 永久占用),要么拿真码去查 UPC 池查不到号。
    """
    monkeypatch.setattr(resources, "LISTING_SHEET",
                        Spreadsheet(name="上架表", token="TOK", sheet_id="SID",
                                    columns=resources.LISTING_SHEET.columns,
                                    headers=resources.LISTING_SHEET.headers))
    row = _sheet_row(2, listed="Unknown", sku="A0X1Y2Z3W4V5")
    monkeypatch.setattr(listing_sheet, "read_rows", lambda: [row])
    seen = {}

    class _C:
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def execute(self, sql, args=None):
            self.sql = sql
            if "feed_items" in sql:
                seen["receipt"] = args
            elif "walmart_items w" in sql:
                seen["online"] = args
            elif "upc_pool" in sql:
                seen["pool"] = args

        def fetchone(self): return (1,)

        def fetchall(self):
            if "feed_items" in self.sql:
                return [("T1", "A0X1Y2Z3W4V5", "F9", "success", "", "")]
            if "upc_pool" in self.sql:
                return [("T1", row["asin"], "0011")]
            return []

    monkeypatch.setattr(listing_sheet.db, "pg_conn",
                        lambda: contextlib.nullcontext(_C()))
    monkeypatch.setattr(feishu, "sheet_write_ranges", lambda s, ups: len(ups))
    used = []
    monkeypatch.setattr(listing_sheet.upc_pool, "mark_used",
                        lambda c, pairs: used.extend(pairs))
    listing_sheet.heal_unknown()
    assert seen["receipt"][1] == ["A0X1Y2Z3W4V5"]      # 台账按真码
    assert seen["online"][1] == ["A0X1Y2Z3W4V5"]       # 目录按真码
    assert seen["pool"] == ([row["asin"]],)            # UPC 池按 ASIN
    assert used == [("0011", "A0X1Y2Z3W4V5")]          # 池的 sku 列存真码


def test_list_new_dry_run_gate_chain(monkeypatch):
    rows = [
        _sheet_row(2),                                    # 走到"待数据源"
        _sheet_row(3, product_type="BannedPT"),           # 风控拦截
        _sheet_row(4, asin="B0LISTED01"),                 # 本店已在架(本店去重)
        _sheet_row(5, asin="B0RISKY001"),                 # 有删除史:不拦(口径)
        _sheet_row(6, product_type="NoSpecPT"),           # PT 无 spec
        _sheet_row(7, store="T_OFF"),                     # 非 ACTIVE 店
        _sheet_row(8, listed="Yes"),                      # 已上架不领任务
        _sheet_row(9, list_result="SKU_LOCKED"),          # sku_locked_heal 处理
        _sheet_row(10, asin="B0BANNED01"),                # ASIN 黑名单
    ]
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        {"T_OFF"}, {}, {("T1", "B0LISTED01")},
        {"B0BANNED01": ("E", "沃尔玛-知产")},
        {"B0ASIN0002"},                # 不明消失史:放行但报警(第 2 行)
        {"banned_pts": {"BannedPT"}, "brands": set()},
        {}, {},                        # 占用台账为空 = 占用闸恒放行
        {}, set()))                    # 冷却/代际两道码闸也恒放行
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln.store_limits, "price_multipliers", lambda: {})
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
    assert "待上架 7" in out    # 9 行中 K=Yes 不领,SKU_LOCKED 归自愈链不归本链
    # 2026-08-17 起闸门行**只报真拦到的**(零值不打印,排版规范规矩 2),
    # 标签也加了空格更好读 —— 这里跟着改,顺便断言那些 0 确实不出现
    assert "非 ACTIVE 店 1" in out and "风控拦截 1" in out
    assert "配送超时 0" not in out and "黑名单 0" not in out
    assert "本店已在架 1" in out and "PT 无 spec 1" in out
    assert "黑名单 1" in out and "待数据源 2" in out
    # 黑名单理由带来源与类别(收集侧建好,拦截侧在此接通)
    assert any("ASIN黑名单:沃尔玛-知产(E类)" in why for _, why in
               [(0, w) for w in out.splitlines()])
    # 防呆=黑名单,不看删除史(所有者口径 2026-08-12):有删除史的
    # B0RISKY001 不拦,照常走到"待数据源"拉数据
    assert fetched["asins"] == [rows[0]["asin"], "B0RISKY001"]
    # 不明消失史(疑似平台下架)只提示不拦截:该行照样走到"待数据源",
    # 但摘要必须报警亮出来(所有者口径 2026-08-12)
    assert "不明原因消失" in out and "B0ASIN0002" in out


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
    assert marked == [("T1", asin)]     # 反查键 = (店铺, ASIN),不是 SKU
    assert "UPC 撞库 1 个已标冲突" in out


def test_failed_rows_requeue_until_cap(monkeypatch):
    """FAILED 行要重新排队(UPC 撞库领新号即可修);超上限则停手。

    SKU_LOCKED 不进本通道:旧实证不先 RETIRE 换 UPC 重发也失败,
    归 sku_locked_heal 自愈链(RETIRE→24h→清列后以新行身份回来)。
    """
    rows = [
        _sheet_row(2, asin="B0RETRY01", list_result="FAILED",
                   feed_id="F1", listed="Yes"),          # 试过 1 次 → 重试
        _sheet_row(3, asin="B0CAPPED01", list_result="FAILED",
                   feed_id="F2", listed="Yes"),          # 试过 3 次 → 停手
        _sheet_row(4, asin="B0LOCKED01", list_result="SKU_LOCKED",
                   feed_id="F3", listed="Yes"),          # 归自愈链,不进重试
        _sheet_row(5, asin="B0ASYNC001", list_result="ASYNC_PENDING",
                   feed_id="F4", listed="Yes"),          # 不是失败
    ]

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def execute(self, sql, args):
            # 假游标抓不到 SQL 语法错(2026-08-09 踩过:psycopg3 不支持
            # `(a,b) IN %s`),至少钉住参数形状:两个等长数组 + 事件名。
            # ⚠ 参数改成具名字典是硬要求:0a-25 的代际过滤要传事件名,而
            # psycopg3 **不许位置占位符与具名占位符混用**。
            assert "IN %s" not in sql, "psycopg3 不支持元组序列 IN"
            assert isinstance(args, dict) and set(args) == {
                "stores", "asins", "abandoned"}
            assert len(args["stores"]) == len(args["asins"])
            self.args = args

        def fetchall(self):
            return [("T1", "B0RETRY01", 1), ("T1", "B0CAPPED01", 3)]

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()

    monkeypatch.setattr(ln.db, "pg_conn", lambda: _Conn())
    retry, exhausted = ln._retry_rows(rows, fake_verdicts(rows))
    assert [r["asin"] for r in retry] == ["B0RETRY01"]
    assert exhausted == [("T1", "B0CAPPED01")]
    # 重新排队的行要看起来像新行(主链才会走领 UPC → 提交)
    assert retry[0]["feed_id"] == "" and retry[0]["list_result"] == ""
    solo = [_sheet_row(2)]
    assert ln._retry_rows(solo, fake_verdicts(solo)) == ([], [])


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
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {},
        {}, set()))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln.store_limits, "price_multipliers",
                        lambda: {"T1": {"fbm_range1": "200%"}})
    monkeypatch.setattr(ln.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: products)

    out = ln.run({"execute": False})
    assert "运费未采到" in out
    assert "共 1 行将进入" in out        # 只有带运费那行进得去


def test_lead_cap_uses_this_rows_store_not_the_last_one(monkeypatch):
    """逐店「配送时长限制」必须按**本行的店**读(2026-08-25 修的作用域泄漏)。

    d4bcaab(2026-08-17 走进生产)把全局常量换成逐店列时,`lead_cap` 那行用的是
    上面按店循环留下的 `store_name` 残值 ⇒ 每一行读到的都是**店名排序最末那家
    店**的上限。这里两家店的上限一宽一严,严的那家排在后面:泄漏一回来,
    宽松店的货就会被严格店的上限拦掉,而摘要里的"本店上限"还写得振振有词。
    """
    rows = [_sheet_row(rownum=2, store="T_A", asin="B0SLOWISH"),
            _sheet_row(rownum=3, store="T_Z", asin="B0SLOWISH2")]
    base = {"title": "T", "price": 20.0, "stock": 50, "shipping": 3.0,
            "stock_state": "in_stock", "channel": "FBM"}
    products = {"B0SLOWISH": {**base, "asin": "B0SLOWISH", "lead_days": 10},
                "B0SLOWISH2": {**base, "asin": "B0SLOWISH2", "lead_days": 10}}
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {},
        {}, set()))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln.store_limits, "price_multipliers",
                        lambda: {"T_A": {"fbm_range1": "200%"},
                                 "T_Z": {"fbm_range1": "200%"}})
    monkeypatch.setattr(ln.store_limits, "lead_day_caps",
                        lambda: {"T_A": 20, "T_Z": 3})
    monkeypatch.setattr(ln.store_targets, "store_channels", lambda: {})
    monkeypatch.setattr(ln.stores_svc, "load_stores", lambda names=None: [
        {"name": "T_A"}, {"name": "T_Z"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: products)

    out = ln.run({"execute": False})
    # T_A 上限 20 天 ⇒ 10 天的货照上;T_Z 上限 3 天 ⇒ 同样 10 天的货拦下
    assert "配送超时 1" in out
    assert "第3行:配送 10 天 > 本店上限 3 天" in out
    assert "共 1 行将进入" in out


def test_store_channel_gate(monkeypatch):
    """限额表「配送限制」标了 fba/fbm 就只上该渠道的货;**没标就都能上**
    (所有者定稿 2026-08-25)。

    ⚠ 与分配侧的未填口径**相反,而且两边都对**:分配 `alloc_engine._blocker`
    未填=不接自由流(没渠道就过不了硬闸);上架照搬那条会把没配置的店整店废掉。
    """
    rows = [_sheet_row(rownum=2, store="T_FBA", asin="B0FBAGOOD"),
            _sheet_row(rownum=3, store="T_FBA", asin="B0FBMBAD"),
            _sheet_row(rownum=4, store="T_FBA", asin="B0JUNKCH"),
            _sheet_row(rownum=5, store="T_ANY", asin="B0FBMOK")]
    base = {"title": "T", "price": 20.0, "stock": 50, "shipping": 3.0,
            "stock_state": "in_stock", "lead_days": 2}
    products = {
        "B0FBAGOOD": {**base, "asin": "B0FBAGOOD", "channel": "FBA"},
        "B0FBMBAD": {**base, "asin": "B0FBMBAD", "channel": "FBM"},
        # 第三种值:上一档"配送方式未采到"就拦了,渠道闸这里根本不该看到它
        "B0JUNKCH": {**base, "asin": "B0JUNKCH", "channel": "N/A"},
        "B0FBMOK": {**base, "asin": "B0FBMOK", "channel": "FBM"},
    }
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {},
        {}, set()))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln.store_limits, "price_multipliers", lambda: {
        "T_FBA": {"fba_range1": "300%", "fbm_range1": "200%"},
        "T_ANY": {"fba_range1": "300%", "fbm_range1": "200%"}})
    # T_FBA 标了 FBA;T_ANY 没标(不在字典里)
    monkeypatch.setattr(ln.store_targets, "store_channels",
                        lambda: {"T_FBA": "FBA"})
    monkeypatch.setattr(ln.stores_svc, "load_stores", lambda names=None: [
        {"name": "T_FBA"}, {"name": "T_ANY"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: products)

    out = ln.run({"execute": False})
    assert "渠道不符本店 1" in out                 # 只有 B0FBMBAD 被渠道闸拦
    assert "第3行:本店只做 FBA,该品是 FBM" in out
    # 没标的店照上 FBM;本店渠道相符的照上;第三种值走"未采到"那一档不重复计数
    assert "共 2 行将进入" in out
    assert "配送方式(FBA/FBM)未采到" in out


def test_silent_buckets_now_write_reasons(monkeypatch):
    """所有者定稿 2026-08-28:除「配额排队」外的静默桶都要写明 N 列原因。

    钉三路:审核判拒 / 审核未过(pending·未审)/ 店铺非 ACTIVE(整店跳过也
    逐行写)。都只写理由**不写终态**——条件解除下一轮自动续上;配额排队
    仍然故意不写(计划上架,还在队里)。"""
    rows = [_sheet_row(2, audit_result="reject"),
            _sheet_row(3, audit_result="pending"),
            _sheet_row(4, audit_result=""),                  # 库里查不到=未审
            _sheet_row(5, store="T_OFF")]                    # 非 ACTIVE 店
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        {"T_OFF"}, {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {},
        {}, set()))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln.store_limits, "price_multipliers", lambda: {})
    monkeypatch.setattr(ln.store_targets, "store_channels", lambda: {})
    monkeypatch.setattr(ln.stores_svc, "load_stores", lambda names=None: [
        {"name": "T1"}, {"name": "T_OFF"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda asins: {})

    out = ln.run({"execute": False})
    assert "第2行:审核判拒,不上架" in out
    assert "第3行:审核未过:pending(过审后自动续上)" in out
    assert "第4行:审核未过:未审(过审后自动续上)" in out
    assert "第5行:店铺非ACTIVE,整店暂停上架" in out
    assert "非 ACTIVE 店 1" in out          # 计数照旧


def test_custom_product_gate(monkeypatch):
    """定制品不上架(所有者定稿 2026-08-28)。明确标了才拦;未采到照常走后面
    的闸(fail-open,与黑名单同向:命中才拦)。"""
    rows = [_sheet_row(rownum=2, store="T1", asin="B0CUSTOM01"),
            _sheet_row(rownum=3, store="T1", asin="B0NORMAL01")]
    base = {"title": "T", "price": 20.0, "stock": 50, "shipping": 3.0,
            "stock_state": "in_stock", "lead_days": 2, "channel": "FBM"}
    products = {
        "B0CUSTOM01": {**base, "asin": "B0CUSTOM01", "is_custom": True},
        "B0NORMAL01": {**base, "asin": "B0NORMAL01", "is_custom": False},
    }
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {},
        {}, set()))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln.store_limits, "price_multipliers",
                        lambda: {"T1": {"fbm_range1": "200%"}})
    monkeypatch.setattr(ln.store_targets, "store_channels", lambda: {})
    monkeypatch.setattr(ln.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: products)

    out = ln.run({"execute": False})
    assert "定制品 1" in out
    assert "第2行:定制品不上架" in out
    assert "共 1 行将进入" in out


def test_other_stores_presence_no_longer_blocks_listing(monkeypatch):
    """取消全局去重(所有者定稿 2026-08-28)的正面钉死:该 ASIN 在**别的店**
    在架(含死档案行)不再拦本店上架 —— 2026-08-28 事件里任何店的退市档案
    都会把 ASIN 对全船队封死,这正是要拆掉的那半边。跨店纪律归占用闸。"""
    rows = [_sheet_row(rownum=2, store="T1", asin="B0FREE0001")]
    base = {"title": "T", "price": 20.0, "stock": 50, "shipping": 3.0,
            "stock_state": "in_stock", "lead_days": 2, "channel": "FBM"}
    products = {"B0FREE0001": {**base, "asin": "B0FREE0001"}}
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, {("A109", "B0FREE0001"), ("A102", "B0FREE0001")},
        {}, set(), {"banned_pts": set(), "brands": set()}, {}, {},
        {}, set()))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln.store_limits, "price_multipliers",
                        lambda: {"T1": {"fbm_range1": "200%"}})
    monkeypatch.setattr(ln.store_targets, "store_channels", lambda: {})
    monkeypatch.setattr(ln.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: products)

    out = ln.run({"execute": False})
    assert "本店已在架" not in out
    assert "共 1 行将进入" in out


def test_quota_slices_after_filters(monkeypatch):
    """配额以能成功提交的行计(所有者批复 2026-08-12):先过滤后切片。

    旧写法 srows[:allow] 先切片再过闸,被淘汰行白占名额——配额 1 时第 1 行
    被库存闸淘汰,当天就一行都上不了;新写法幸存者切片,后面的行顶上。
    """
    rows = [_sheet_row(2, asin="B0LOWSTOCK"),
            _sheet_row(3, asin="B0GOODONE1"),
            _sheet_row(4, asin="B0GOODONE2")]
    base = {"title": "T", "price": 20.0, "stock_state": "in_stock",
            "lead_days": 2, "channel": "FBM", "shipping": 3.0}
    products = {"B0LOWSTOCK": {**base, "asin": "B0LOWSTOCK", "stock": 1},
                "B0GOODONE1": {**base, "asin": "B0GOODONE1", "stock": 50},
                "B0GOODONE2": {**base, "asin": "B0GOODONE2", "stock": 50}}
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {},
        {}, set()))
    monkeypatch.setattr(ln, "_load_quota", lambda: {"T1": 1})
    monkeypatch.setattr(ln.store_limits, "price_multipliers",
                        lambda: {"T1": {"fbm_range1": "200%"}})
    monkeypatch.setattr(ln.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: products)

    out = ln.run({"execute": False})
    assert "库存不足:1" in out
    assert "共 1 行将进入" in out
    assert "B0GOODONE1" in out           # 幸存者顶上配额位(旧写法这里是 0 行)
    assert "超配额 1" in out             # 超额的是第二个幸存者,不是被淘汰行


def test_material_gate_drops_before_llm_and_quota(monkeypatch):
    """卖点/副图凑不够的行**在预备期之前**就淘汰(2026-08-22 生产实证)。

    这两项由系统从采集数据生成(LLM 一个字也插不上手),够不够在取数这一步
    就是定论。不提前拦的话它们要走到预备期才被 validate 拦成"必填缺失",
    代价是白打一次 LLM + 白占一个当天配额名额(切片在预备期之前),而素材是
    产品的固定属性 —— 天天重来天天白烧。理由也要说人话,不能只报字段名。
    """
    rows = [_sheet_row(2, asin="B0NOBULLET"),   # 卖点 1 条、描述空 → 凑不够
            _sheet_row(3, asin="B0FEWIMGS"),    # 主图 1 + 副图 1 → 少于 minItems 2
            _sheet_row(4, asin="B0GOODONE1")]   # 素材齐全
    base = {"title": "Steel Widget Pro", "price": 20.0, "stock": 50,
            "stock_state": "in_stock", "lead_days": 2, "channel": "FBM",
            "shipping": 3.0}
    rich = {"bullet_points": [f"Bullet {i} describing the widget in detail"
                              for i in range(5)], "description": "D" * 500}
    products = {
        "B0NOBULLET": {**base, "asin": "B0NOBULLET",
                       "attrs": {"bullet_points": ["only one"], "description": ""},
                       "images": [f"i{i}" for i in range(6)]},
        "B0FEWIMGS": {**base, "asin": "B0FEWIMGS", "attrs": rich,
                      "images": ["main", "s1"]},
        "B0GOODONE1": {**base, "asin": "B0GOODONE1", "attrs": rich,
                       "images": [f"i{i}" for i in range(6)]},
    }
    spec = {"required": ["keyFeatures", "productSecondaryImageURL"],
            "properties": {"keyFeatures": {"minItems": 3},
                           "productSecondaryImageURL": {"minItems": 2}}}
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {},
        {}, set()))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln.store_limits, "price_multipliers",
                        lambda: {"T1": {"fbm_range1": "200%"}})
    monkeypatch.setattr(ln.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: spec)
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: products)

    out = ln.run({"execute": False})
    assert "素材不足 2" in out            # 摘要单独一栏,不混进"数据过滤"
    assert "卖点凑不够" in out and "副图不够" in out    # 理由说人话
    assert "共 1 行将进入" in out         # 只有素材齐全那行进预备期


def test_push_scrape_daily_dedup(monkeypatch):
    """缺数据自动推采集(所有者批复 2026-08-12):日界批次名撞名即防重。"""
    calls = []
    monkeypatch.setattr(ln.scraper, "submit_batch",
                        lambda name, asins: (calls.append((name, asins)),
                                             {"inserted": len(asins)})[1])
    # 台账与插队是写库/调采集侧的副作用,这里只验证被叫到(2026-08-18
    # 同轮闭环:推完本侧在等,所以要 record + prioritize)
    booked, jumped = [], []
    monkeypatch.setattr(ln.scrape_batches, "record",
                        lambda name, bid, n, status, note="": booked.append(
                            (name, status)))
    monkeypatch.setattr(ln.scrape_batches, "prioritize",
                        lambda name, bid: (jumped.append(name), True)[1])
    note, names = ln._push_scrape(["B1", "B2"], execute=True)
    assert "已推采集" in note and calls[0][1] == ["B1", "B2"]
    assert calls[0][0].startswith("listing_gap_")
    assert names == [calls[0][0]]            # 可等待的批次名交还调用方
    assert booked == [(calls[0][0], "pushed")] and jumped == [calls[0][0]]

    def boom(name, asins):
        raise ln.scraper.BatchExistsError(7, name)
    monkeypatch.setattr(ln.scraper, "submit_batch", boom)
    note2, names2 = ln._push_scrape(["B3"], execute=True)
    assert "已推过" in note2 and len(names2) == 1   # 撞名沿用既有批次,照样可等
    note3, names3 = ln._push_scrape(["B4"], execute=False)   # dry-run 不推
    assert "DRY-RUN" in note3 and names3 == []
    assert ln._push_scrape([], True) == (None, [])


def test_heal_unknown_three_paths(monkeypatch):
    """K=Unknown 自愈(所有者批复 2026-08-12):台账终态双向 + 目录在线。"""
    monkeypatch.setattr(resources, "LISTING_SHEET",
                        Spreadsheet(name="上架表", token="TOK", sheet_id="SID",
                                    columns=resources.LISTING_SHEET.columns))
    rows = [_sheet_row(2, listed="Unknown", list_date="2026-08-10"),  # 台账 success
            _sheet_row(3, listed="Unknown"),                          # 台账 failed
            _sheet_row(4, listed="Unknown"),                          # 目录在线
            _sheet_row(5, listed="Unknown"),                          # 无依据,保持
            _sheet_row(6, listed="Yes")]                              # 不碰
    a2, a3, a4 = rows[0]["asin"], rows[1]["asin"], rows[2]["asin"]

    class _C:
        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, args=None):
            self.sql = sql

        def fetchone(self):
            return (1,)                          # 目录非空,源② 启用

        def fetchall(self):
            if "ops.feed_items" in self.sql:
                return [("T1", a2, "F1", "success", "", None),
                        ("T1", a3, "F2", "failed", "ERR_X", "字段坏了")]
            if "walmart_items" in self.sql:
                return [("T1", a4)]
            if "upc_pool" in self.sql:
                return [("T1", a2, "0011"), ("T1", a3, "0022")]
            return []

    conn = _C()
    monkeypatch.setattr(listing_sheet.db, "pg_conn",
                        lambda: contextlib.nullcontext(conn))
    monkeypatch.setattr(listing_sheet, "read_rows", lambda: rows)
    writes = []
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda s, ups: (writes.extend(ups), len(ups))[1])
    used, released = [], []
    monkeypatch.setattr(listing_sheet.upc_pool, "mark_used",
                        lambda c, pairs: used.extend(pairs))
    monkeypatch.setattr(listing_sheet.upc_pool, "release",
                        lambda c, upcs, reason: released.extend(upcs))

    out = listing_sheet.heal_unknown()
    w = {rng: vals[0] for rng, vals in writes}
    # 提交结果四列 + 回执三列 = 今天的 M:S(range 由 layout 算,不是硬编码)
    assert w["M2:S2"][0] == "Yes" and w["M2:S2"][1] == "F1"
    assert w["M2:S2"][2] == "2026-08-10"          # 原上架日期不被覆盖
    assert w["M3:S3"][0] == "No" and w["M3:S3"][4] == "FAILED"
    assert "ERR_X | 字段坏了" in w["M3:S3"][5]     # 报错列码+人话
    assert w["M4:P4"][0] == "Yes"                 # 目录在线只写提交结果四列
    assert "M5:S5" not in w and "M5:P5" not in w  # 无依据绝不负向写
    assert "M6:S6" not in w and "M6:P6" not in w  # 非 Unknown 不碰
    assert used == [("0011", a2)] and released == ["0022"]
    assert "确认在线 2" in out and "确认失败重排 1" in out and "继续观察 1" in out


def test_prohibited_receipt_never_requeues():
    """政策违禁(旧 O 列第五类,2026-08-12 接线):三违禁码 → O=PROHIBITED,
    不进 FAILED 重试通道——重发也永远是拒。"""
    c = listing_sheet.classify_receipt
    assert c("failed", "EXT_DATA_ERROR_71666506605865") == \
        ("PROHIBITED", "EXT_DATA_ERROR_71666506605865")
    assert c("failed", "EXT_DATA_ERROR_61020366035308")[0] == "PROHIBITED"
    # SKU_LOCKED/ASYNC 优先级不受影响
    assert c("failed", "ERR_EXT_DATA_0101211")[0] == "SKU_LOCKED"


def test_fresh_filter_excludes_prohibited(monkeypatch):
    rows = [_sheet_row(2, list_result="PROHIBITED", listed="No"),
            _sheet_row(3)]
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {},
        {}, set()))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln.store_limits, "price_multipliers", lambda: {})
    monkeypatch.setattr(ln.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: {})
    out = ln.run({"execute": False})
    assert "待上架 1" in out          # PROHIBITED 行不领任务


def test_claim_gates_block_other_stores_only(monkeypatch):
    """占用闸:别店占着就拦,自家占着放行,真·无品牌不受品牌排他管。

    与快照闸的分工:快照闸看"现在在不在架",占用闸看"归谁"——商品下架后
    快照闸失守而占用闸仍在,这正是所有者要的"店没停运就不许别店碰"。
    """
    rows = [
        _sheet_row(2, asin="B0OWNED001"),                  # 产品被别店占
        _sheet_row(3, asin="B0MINE0001"),                  # 自家占着 → 放行
        _sheet_row(4, asin="B0BRANDED1"),                  # 品牌被别店占
        _sheet_row(5, asin="B0NOBRAND1"),                  # 无品牌 → 不受品牌闸管
    ]
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()},
        {"B0OWNED001": "OTHER", "B0MINE0001": "T1"},        # 产品占用
        {"acme": "OTHER"},                                  # 品牌占用
        {}, set()))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln.store_limits, "price_multipliers", lambda: {})
    monkeypatch.setattr(ln.stores_svc, "load_stores", lambda names=None: [{"name": "T1"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    base = {"title": "T", "price": 20.0, "stock": 50, "shipping": 0.0,
            "stock_state": "in_stock", "lead_days": 2, "channel": "FBM"}
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda asins: {
        "B0MINE0001": {**base, "asin": "B0MINE0001", "brand": "beta"},
        "B0BRANDED1": {**base, "asin": "B0BRANDED1", "brand": "Acme"},
        "B0NOBRAND1": {**base, "asin": "B0NOBRAND1", "brand": "Generic"},
    })
    monkeypatch.setattr(ln.store_limits, "price_multipliers",
                        lambda: {"T1": {"fbm_range1": "200%"}})
    out = ln.run({"execute": False})

    assert "产品占用:已属于 OTHER" in out          # 别店占的产品被拦
    assert "品牌占用:acme 已属于 OTHER" in out     # 别店占的品牌被拦
    # 自家占的产品、以及无品牌(Generic 是占位符,不参与品牌排他)的,
    # 都要走到待提交——占用闸只拦"别的店占着"的
    assert "T1 B0MINE0001 定价" in out
    assert "T1 B0NOBRAND1 定价" in out
    assert "共 2 行将进入" in out


def test_listed_pairs_cover_every_store_for_self_dedup(monkeypatch):
    """去重集合 = (店铺, SKU) 对,**全部店都进**(含规划外店)。

    2026-08-28 所有者定稿「取消全局去重」:集合只回答"这家店自己有没有这个
    ASIN"(自己拦自己防重复上架),不再承载跨店互拦 —— 于是 2026-08-15
    「规划外店不拦别人」不再需要在这里把它们的行剔掉。
    """
    seen = {}

    class _Cur:
        def execute(self, sql, args=None):
            self.sql = sql
            seen["last"] = sql

        def fetchall(self):
            if "walmart_items" in seen["last"]:
                return [("谭总4", "B0TANZONG1"), ("A085", "B0MINE0001")]
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    import contextlib
    monkeypatch.setattr(ln.db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([_Conn()])))
    monkeypatch.setattr(ln.blacklist, "load_banned_asins", lambda c: {})
    monkeypatch.setattr(ln.risk_gate, "load_gate",
                        lambda c: {"banned_pts": set(), "brands": set()})
    monkeypatch.setattr(ln.claims, "load_active", lambda c, k: {})

    pairs = ln._load_gate_state().listed_pairs
    assert ("A085", "B0MINE0001") in pairs
    assert ("谭总4", "B0TANZONG1") in pairs   # 规划外店也进:自己拦自己
    # 集合是对,不是裸 ASIN:别的店在架不构成任何拦截依据
    assert "B0MINE0001" not in pairs


# ── 去重闸的身份口径(0a-24)────────────────────────────────────────────

def test_dedup_gate_reads_the_registry_key():
    """闸的第二列是**身份键**(coalesce(ls.source_key, w.sku)),不是 SKU 串。

    这是切换后最贵的一条:失效不报错,后果是同店同 ASIN 反复上架 ——
    烧 UPC、烧 MP_ITEM 配额,而摘要一切正常。闸判那头拿的是 r["asin"],
    两边必须同一个口径。
    """
    q = ln._SQL_LISTED_ASINS
    assert "coalesce(ls.source_key, w.sku)" in q
    assert "ls.source_type = 'amz'" in q
    assert "SELECT DISTINCT w.store" in q


def test_dedup_gate_has_no_lifecycle_condition():
    """**不加** lifecycle 条件 —— 别照抄 alloc_push 的排 RETIRED 写法。

    RETIRED 行只要码未弃就得拦:退市档案不由 list_new 复活(2026-08-28 定稿;
    plan.md 记着那次 7,342 行批量复活事故)。
    """
    assert "lifecycle" not in ln._SQL_LISTED_ASINS


def test_dedup_gate_still_blocks_unregistered_rows():
    """必须 **LEFT** JOIN 登记簿:未登记的在架行也要拦。

    写成 INNER JOIN 的话,两次回填之间新出现的行会静默漏闸 —— 而漏闸的表现
    就是重复上架。
    """
    q = ln._SQL_LISTED_ASINS
    assert "LEFT JOIN catalog.listing_sources ls" in q


def test_dedup_gate_ignores_non_amz_registry_rows():
    """闸**只按 amz 身份键**去重(ON 条件带 source_type='amz')。

    这是与 synthesis 规则 4 字面写法(不带 source_type)的一处**有意偏差**:
    match 行的码寿命由 match_listing 自己的通道管,后果是「已弃码的 match
    僵尸行仍会挡新码」,对只处理 amz 行的 list_new 无实害。写在这里是为了让
    契约与实现的差有东西钉着,而不是靠谁记得。
    """
    q = ln._SQL_LISTED_ASINS
    assert "ls.sku = w.sku AND ls.source_type = 'amz'" in q


def test_dedup_gate_lets_abandoned_codes_through():
    """`ls.abandoned_at IS NULL`:码已弃 = 沃尔玛侧无物可撞,该放行。

    批次 2 之前它**恒真**(全库该列 NULL,而且是 LEFT JOIN,未登记行同样
    是 NULL)⇒ 拦截集合逐个不变;提前落地是为了让写侧切换只改一处。
    """
    assert "ls.abandoned_at IS NULL" in ln._SQL_LISTED_ASINS


# ── 重试上限的代际口径(0a-25)──────────────────────────────────────────

def test_retry_cap_is_still_three():
    """上限 3 次不变(旧 retry_state 永久淘汰名单的等价物)。"""
    assert ln.MAX_LIST_ATTEMPTS == 3


def test_attempts_are_counted_per_store_and_identity_key():
    """计数键是 (店铺, **身份键**),不是 (店铺, SKU)。

    按裸 SKU 数的话每次新码 count 恒 0 ⇒ FAILED 无限重试(烧 UPC、烧配额,
    不报错)。这也是"码复用到退役"那条定稿的理由之一。
    """
    q = ln._SQL_ATTEMPTS
    assert "coalesce(ls.source_key, f.sku) = t.asin" in q
    assert "GROUP BY t.store, t.asin" in q
    assert "SELECT t.store, t.asin, count(*)" in q
    assert "f.feed_type = 'MP_ITEM'" in q


def test_attempts_fall_back_to_cross_code_counting_without_an_abandon_event():
    """没有弃码事件 ⇒ LATERAL 返 NULL ⇒ 谓词恒真 ⇒ 退化成今天的跨码累计。

    这就是本批"零行为变化"在这一处的落点:全库此刻没有任何 sku_abandoned
    事件(abandon 零接线),所以代际过滤一行都不筛。
    """
    assert "g.since IS NULL OR f.submitted_at > g.since" in ln._SQL_ATTEMPTS


def test_attempts_only_count_after_the_last_abandon_event():
    """有弃码事件 ⇒ 只数**最近一次**弃码之后的提交(换了码就重新给三次)。"""
    q = ln._SQL_ATTEMPTS
    assert "max(occurred_at) AS since" in q
    assert "FROM catalog.product_events e" in q
    assert "e.store = t.store" in q


def test_attempts_generation_filter_reads_the_event_detail_not_the_asin_column():
    """代际过滤读 abandon 自己写进 detail 的 source_key,**不读 asin 列**。

    product_events.asin 要到批次 0b 才经登记簿反查;在「0a 已合、0b 未合」的
    窗口里 abandon 写出的事件 asin 恒为 NULL、sku 是不透明码 —— 按 asin 认
    弃码事件会永不命中。改读 detail 之后本批不依赖 0b 的落地时序。
    事件名走 product_events 常量,不写字面量(registry / 常量唯一出处纪律)。
    """
    q = ln._SQL_ATTEMPTS
    assert "e.detail ->> 'source_key' = t.asin" in q
    assert "coalesce(e.asin, e.sku)" not in q
    assert "%(abandoned)s" in q
    assert ln.product_events.SKU_ABANDONED == "sku_abandoned"


def test_submit_jitter_desynchronizes_the_starts(monkeypatch):
    """起跑抖动:去同步,**不降并发**(所有者定稿 2026-08-26)。

    三段式重排(#51)之后提交期只剩「领号(毫秒)→ 回填(微秒)→ POST」,
    24 个线程几乎在同一毫秒发起。抖动消掉的就是这一下 —— TLS 握手、首包、
    PG 领号、连接池建连不再全撞在一个瞬间。

    ⚠ 它**不减少峰值**:上传要几十秒,几百毫秒错不开任何两家店。用例因此只
    断言"起跑时刻确实散开了",不断言并发下降 —— 断错了会把这个廉价手段
    当成万灵药,下次 5xx 又来时没人知道该往哪查。
    """
    import time as _t

    rows = [_sheet_row(i + 2, store=f"T{i}", asin=f"B0JIT{i:05d}")
            for i in range(4)]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    _wire_execute_env(monkeypatch, rows, products)
    starts, lock = [], __import__("threading").Lock()

    def fake_submit(store, feed_type, items, workflow=None, defer_settle=False):
        with lock:
            starts.append(_t.monotonic())
        yield {"outcome": "submitted", "feed_id": "F-J", "count": len(items)}

    monkeypatch.setattr(ln.feeds, "submit_feed", fake_submit)
    out = ln.run({"execute": True, "submit_jitter_ms": 300})

    assert len(starts) == 4
    # 四家店的起跑时刻必须真的散开(同一毫秒发起的话跨度会是微秒级)
    assert max(starts) - min(starts) > 0.01, starts
    assert "起跑抖动 0~300ms" in out


def test_submit_jitter_off_says_so_out_loud(monkeypatch):
    """关掉抖动要在摘要里喊出来 —— 缺省是开的,关着跑而摘要不提,
    等于把"今晚为什么又 5xx"的线索藏起来。"""
    rows = [_sheet_row(2, store="T1", asin="B0NOJIT001")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    _wire_execute_env(monkeypatch, rows, products)
    out = ln.run({"execute": True, "submit_jitter_ms": 0})
    assert "起跑抖动**已关**" in out


def test_adaptive_gate_steps_down_the_ladder_and_never_back_up():
    """遇 5xx 按 24 → 16 → 12 → 8 → 4 降档,**到底不再降、也永不回升**
    (所有者定稿 2026-08-26:「动态降并发,而不是直接打死」)。

    只降不升是有意的:升回去要先判断"拥堵过去了",而一轮就几分钟,判据必然
    是猜的 —— 猜错就是在拥堵没散时又冲一次,把刚退下来的让步白费。下一轮
    进程重开,阶梯自然回到顶格。**降到 0 才是"打死"**,所以 4 是保底通道。
    """
    g = ln._AdaptiveGate(24)
    assert g.limit == 24
    for want in (16, 12, 8, 4):
        g.step_down("5xx")
        assert g.limit == want
    g.step_down("又一个 5xx")
    assert g.limit == 4, "到底之后还往下降就是把当轮剩下的店全废了"
    # 轨迹要留:摘要得报出降到哪一档、首因是谁
    assert [n for n, _ in g.steps] == [16, 12, 8, 4]
    assert g.steps[0][1] == "5xx"


def test_adaptive_gate_ladder_skips_rungs_above_the_start():
    """顶格本来就低于某几档时,那几档要跳过 —— 否则"降档"会把并发**调高**。

    (`STORE_WORKERS` 是全项目共用常量,别处改小过它这条就会踩上。)
    """
    g = ln._AdaptiveGate(10)          # 顶格 10:16 这一档必须跳过
    g.step_down("5xx")
    assert g.limit == 8
    g.step_down("5xx")
    assert g.limit == 4


def test_deferred_rows_write_no_terminal_state_until_the_settle_round(monkeypatch):
    """不确定的片子**当轮不写终态**,整轮跑完才结算(所有者定稿 2026-08-26)。

    盯三件事:
      ① 第一轮不回收 UPC、不写表 —— 写了就没得救了;
      ② 第二轮真的跑了,而且用的是**同一条落地路径**(_apply_submit_result);
      ③ 并发闸因为这次不确定降了一档 —— 第二轮不该再按顶格冲。
    """
    rows = [_sheet_row(2, store="T1", asin="B0DEFER001"),
            _sheet_row(3, store="T2", asin="B0DEFER002")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    seen = _wire_execute_env(monkeypatch, rows, products)

    def fake_submit(store, feed_type, items, workflow=None, defer_settle=False):
        assert defer_settle, "上架链必须走延后结算"
        yield {"outcome": "deferred", "feed_id": None, "count": len(items),
               "_settle": {"store": store["name"]}}

    settled = []

    def fake_settle(store, settle):
        settled.append(store["name"])
        return {"outcome": "submitted", "feed_id": "F-LATE", "count": 1}

    monkeypatch.setattr(ln.feeds, "submit_feed", fake_submit)
    monkeypatch.setattr(ln.feeds, "settle_deferred", fake_settle)
    written = []
    monkeypatch.setattr(ln.listing_sheet, "write_submit_cols",
                        lambda u: (written.extend(u), len(u))[1])

    out = ln.run({"execute": True})

    # ① 第一轮:一个 UPC 都没回收(回收了就等于判了未达)
    assert seen["released"] == []
    # ② 第二轮真跑了,两家店都结算了
    assert sorted(settled) == ["T1", "T2"]
    assert "⏸ 延后结算:2 片" in out and "✅ 补上 2 条" in out
    # 表只在结算之后写,且写的是成功态(K=Yes)
    assert [u[1][4] for u in written] == ["Yes", "Yes"]
    # ③ 降档发生了,而且摘要报得出来
    assert "提交并发降档" in out


def test_submit_loop_is_cross_store_concurrent(monkeypatch):
    """跨店并发提交:并发确实发生、摘要仍按店名排序、提交期三个计数不丢。

    盯三件事,少一件这次改造就白做:
      ① **真并发**:submit_feed 里记在飞的店数,峰值必须 >1(不然只是把
         for 循环包了一层线程池,慢照旧)。
      ② **顺序确定**:让 T5 最快、T1 最慢,摘要行序仍须 T1…T5 —— 完成先后
         是随机的,输出跟着随机就没法拿两轮跑对拍。
      ③ **计数不丢**:no_upc 从共享 dict 改成各店局部 dict 再合并,合并漏
         一店就少算,而少算**不报错**;invalid 自 2026-08-18 起在预备期
         (_prep_rows 跨店并发)统计,钉住"预备期/提交期"两行摘要都在。
    """
    import threading
    import time

    stores = [f"T{i}" for i in range(1, 6)]
    rows, rn = [], 1
    for s in stores:
        for tag in ("NOUPC", "BAD", "OK"):
            rn += 1
            rows.append(_sheet_row(rn, store=s, asin=f"B0{s}{tag}"))
    base = {"title": "标题够长的十个字以上", "price": 20.0, "stock": 50,
            "stock_state": "in_stock", "lead_days": 2, "channel": "FBM",
            "shipping": 3.0}
    products = {r["asin"]: {**base, "asin": r["asin"]} for r in rows}

    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {},
        {}, set()))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln.store_limits, "price_multipliers",
                        lambda: {s: {"fbm_range1": "200%"} for s in stores})
    monkeypatch.setattr(ln.stores_svc, "load_stores",
                        lambda names=None: [{"name": s} for s in stores])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.pt_spec, "orderable_spec", lambda: {})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: products)
    monkeypatch.setattr(ln, "_push_scrape", lambda want, execute: (None, []))
    monkeypatch.setattr(ln, "_sync_upc", lambda e, l: None)
    monkeypatch.setattr(ln, "_writeback_upc", lambda e, l: None)
    # _prep_rows 的连接池会带 autocommit=True 开连接,桩要收 kwargs
    monkeypatch.setattr(ln.db, "pg_conn",
                        contextlib.contextmanager(
                            lambda **kw: iter([object()])))
    monkeypatch.setattr(ln.settings_api, "get_partner_id", lambda s: "P")

    # UPC:每店第一行(…NOUPC)领不到号 → no_upc;其余给号
    monkeypatch.setattr(ln.upc_pool, "claim", lambda c, wants: [
        None if w["asin"].endswith("NOUPC") else "0" + w["asin"] for w in wants])
    monkeypatch.setattr(ln.upc_pool, "release", lambda *a, **k: 0)
    monkeypatch.setattr(ln.upc_pool, "mark_used", lambda *a, **k: 0)
    # 抽码桩(码尾保留 ASIN 后 6 位,好让下面的 BAD 判据还认得出行)
    monkeypatch.setattr(
        ln.sku_codec, "mint",
        lambda conn, store, st_, key, *, workflow:
            "A" + key[-6:].upper().rjust(11, "X"))
    monkeypatch.setattr(ln, "_map_llm",
                        lambda c, pt, spec, p, stats=None: ({"productName": p["title"]}, {}))
    monkeypatch.setattr(ln.mp_mapper, "build_orderable", lambda *a, **k: {})
    monkeypatch.setattr(ln.mp_mapper, "assemble_mp_item",
                        lambda o, pt, v: {"pt": pt})
    # spec 一致化:每店第二行(…BAD)必填缺失 → invalid
    monkeypatch.setattr(ln.mp_conform, "conform", lambda *a, **k: (
        a[2], a[3], [], ["brand"] if str(k.get("sku", "")).endswith("BAD") else []))
    monkeypatch.setattr(ln.listing_sources, "register", lambda *a, **k: None)
    monkeypatch.setattr(ln.product_events, "record_many", lambda *a, **k: None)
    monkeypatch.setattr(ln.listing_sheet, "write_reasons", lambda *a, **k: 0)
    monkeypatch.setattr(ln.listing_sheet, "write_data_cols", lambda *a, **k: 0)
    monkeypatch.setattr(ln.listing_sheet, "write_submit_cols", lambda u: len(u))

    lock = threading.Lock()
    state = {"inflight": 0, "peak": 0}

    def fake_submit(store, feed_type, items, workflow=None, defer_settle=False):
        with lock:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        # T1 最慢、T5 最快:完成顺序与店名顺序**相反**
        time.sleep(0.02 * (6 - int(store["name"][1:])))
        with lock:
            state["inflight"] -= 1
        yield {"outcome": "submitted", "feed_id": f"F-{store['name']}",
               "count": len(items)}

    monkeypatch.setattr(ln.feeds, "submit_feed", fake_submit)

    # 起跑抖动关掉:本用例盯的是"池子是真线程池不是 for 循环包了层壳",
    # 与抖动**正交**(抖动去的是同一毫秒起跑,不改并发)。抖动另有用例钉着
    out = ln.run({"execute": True, "submit_jitter_ms": 0})

    assert state["peak"] > 1, f"没有真并发,峰值在飞 {state['peak']} 家"
    hit = [ln_ for ln_ in out.splitlines() if "提交 1 条" in ln_]
    assert [l.split(":")[0].strip() for l in hit] == stores, hit
    # 必填缺失在预备期拦下(不领号),UPC 池不足在提交期领号时才知道
    prep = [l for l in out.splitlines() if l.startswith("预备期")]
    assert prep and "通过 10/15" in prep[0] and "必填缺失 5" in prep[0], prep
    assert "提交期:UPC池不足 5" in out


def _wire_execute_env(monkeypatch, rows, products):
    """真跑路径的标准桩(闸门全放行、外设全假);返回可观测容器。

    2026-08-18 三段式重排(预备期→领号期→提交期)的三个验收点共用这套底座:
    prep 失败不领号 / 占位号回填成真号 / 同轮闭环救回。
    """
    stores = sorted({r["store"] for r in rows})
    seen = {"claim_wants": [], "released": [], "orderable_upcs": [],
            "assembled_upcs": [], "submitted": [], "minted": [],
            "orderable_skus": [], "conform_skus": [], "submit_vals": [],
            "registered": [], "events": [], "marked_used": []}
    # 抽码桩:返回**可预测**的码(不是随机),否则断言只写得出"不等于 ASIN",
    # 写不出"载荷里那个就是登记簿里那个"。签名跟着 sku_codec.mint 的定死签名
    # 走(workflow 是关键字参):漏了它真代码调用会 TypeError 而桩不会,
    # 那种差异只在生产暴露
    def fake_mint(conn, store, source_type, source_key, *, workflow):
        seen["minted"].append((store, source_type, source_key, workflow))
        # 码尾保留 ASIN 的后 6 位,只是为了让夹具还能分得清哪行是哪行
        # (…BAD 行仍要在预备期被 conform 拦下);真码是纯随机的
        return "A" + source_key[-6:].upper().rjust(11, "X")
    monkeypatch.setattr(ln.sku_codec, "mint", fake_mint)
    # 起跑抖动在单元测试里一律关掉(缺省 0~800ms × 每店会让多店用例真睡几秒)。
    # 关的是**常量默认值**;要测抖动的用例自己传 submit_jitter_ms
    monkeypatch.setattr(ln, "SUBMIT_JITTER_MS", 0)
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {},
        {}, set()))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln.store_limits, "price_multipliers",
                        lambda: {s: {"fbm_range1": "200%"} for s in stores})
    monkeypatch.setattr(ln.stores_svc, "load_stores",
                        lambda names=None: [{"name": s} for s in stores])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.pt_spec, "orderable_spec", lambda: {})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: products)
    # 2026-08-19 起全部候选先推采集刷新:底座默认"推了但没有可等的批次"
    # (不等不摄取),要测同轮闭环的用例自己覆盖
    seen["scrape_pushed"] = []
    monkeypatch.setattr(ln, "_push_scrape",
                        lambda want, execute: (
                            seen["scrape_pushed"].extend(want),
                            (f"  候选 {len(want)} 个 ASIN 已推采集刷新",
                             []))[1])
    monkeypatch.setattr(ln, "_sync_upc", lambda e, l: None)
    monkeypatch.setattr(ln, "_writeback_upc", lambda e, l: None)
    monkeypatch.setattr(ln.db, "pg_conn",
                        contextlib.contextmanager(
                            lambda **kw: iter([object()])))
    monkeypatch.setattr(ln.settings_api, "get_partner_id", lambda s: "P")

    def claim(c, wants):
        seen["claim_wants"].extend(wants)
        return ["19999" + w["asin"][-7:] for w in wants]
    monkeypatch.setattr(ln.upc_pool, "claim", claim)
    monkeypatch.setattr(ln.upc_pool, "release",
                        lambda c, upcs, reason: seen["released"].extend(upcs))
    monkeypatch.setattr(ln.upc_pool, "mark_used",
                        lambda c, pairs: seen["marked_used"].extend(pairs))
    monkeypatch.setattr(ln, "_map_llm",
                        lambda c, pt, spec, p, stats=None: ({"productName": p["title"]}, {}))
    seen["orderable_fcs"] = []
    monkeypatch.setattr(
        ln.mp_mapper, "build_orderable",
        lambda sku, upc, price, qty, partner, **k: (
            seen["orderable_upcs"].append(str(upc)),
            seen["orderable_skus"].append(str(sku)),
            seen["orderable_fcs"].append(str(partner)),
            {"sku": str(sku),
             "productIdentifiers": {"productId": str(upc),
                                    "productIdType": "UPC"}})[3])
    # 每店第二类行(…BAD)必填缺失 → 预备期本地拦下(批次 2 起 conform 收到的
    # 是不透明码,靠 fake_mint 保留的 ASIN 码尾仍分得出来)
    def _conform(*a, **k):
        seen["conform_skus"].append(str(k.get("sku", "")))
        bad = str(k.get("sku", "")).endswith("BAD")
        return a[2], a[3], [], ["brand"] if bad else []
    monkeypatch.setattr(ln.mp_conform, "conform", _conform)
    monkeypatch.setattr(
        ln.mp_mapper, "assemble_mp_item",
        lambda o, pt, v: (seen["assembled_upcs"].append(
            o["productIdentifiers"]["productId"]), {"pt": pt})[1])
    # register 保留桩是为了断言它**零调用**(登记已在 mint 里做完,双轨禁止)
    monkeypatch.setattr(ln.listing_sources, "register",
                        lambda c, rows_: seen["registered"].extend(rows_))
    monkeypatch.setattr(ln.product_events, "record_many",
                        lambda c, rows_: seen["events"].extend(rows_))
    monkeypatch.setattr(ln.listing_sheet, "write_reasons", lambda *a, **k: 0)
    monkeypatch.setattr(ln.listing_sheet, "write_data_cols", lambda *a, **k: 0)
    monkeypatch.setattr(ln.listing_sheet, "write_submit_cols",
                        lambda u: (seen["submit_vals"].extend(u), len(u))[1])

    def fake_submit(store, feed_type, items, workflow=None, defer_settle=False):
        seen["submitted"].append((store["name"], len(items)))
        yield {"outcome": "submitted", "feed_id": "F-1", "count": len(items)}
    monkeypatch.setattr(ln.feeds, "submit_feed", fake_submit)
    return seen


_PRODUCT_OK = {"title": "标题够长的十个字以上", "price": 20.0, "stock": 50,
               "stock_state": "in_stock", "lead_days": 2, "channel": "FBM",
               "shipping": 3.0}


def test_prep_fail_does_not_claim_upc(monkeypatch):
    """预备期失败的行**根本不领号**(2026-08-18 重排的核心收益)。

    旧序是"领了再 release":池紧张时号先被注定失败的行占走,水位来回抖。
    新序里 BAD 行在 _prep_rows 就被拦下,claim 的 wants 里不许出现它,
    release 一次都不该发生(没领过,无号可回收)。
    """
    rows = [_sheet_row(2, asin="B0AAAAAOK1"),
            _sheet_row(3, asin="B0AAAAABAD")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    seen = _wire_execute_env(monkeypatch, rows, products)
    out = ln.run({"execute": True})
    assert [w["asin"] for w in seen["claim_wants"]] == ["B0AAAAAOK1"]
    assert seen["released"] == []
    assert "必填缺失 1" in out and "通过 1/2" in out


def test_placeholder_upc_backfilled_with_real(monkeypatch):
    """占位号只进预备期,发出去的载荷必须是真号。

    build_orderable 在预备期只见占位号(_UPC_PLACEHOLDER);领号期把真号
    回填进 productIdentifiers;assemble 时载荷里若还是 000000000000,
    等于把占位号提交给沃尔玛 —— 这是本次重排最不能出的错。
    """
    rows = [_sheet_row(2, asin="B0AAAAAOK1")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    seen = _wire_execute_env(monkeypatch, rows, products)
    ln.run({"execute": True})
    assert seen["orderable_upcs"] == [ln._UPC_PLACEHOLDER]
    assert seen["assembled_upcs"] == ["19999AAAAOK1"]
    assert ln._UPC_PLACEHOLDER not in seen["assembled_upcs"]
    assert seen["submitted"] == [("T1", 1)]


def test_multi_slice_results_line_up_with_their_own_rows(monkeypatch):
    """submit_feed 只回 count 不回条目,对位走 api/feeds.iter_result_slices
    —— 错一位就是整批结局落到别人行上,而且**不报错**。"""
    rows = [_sheet_row(rn, store="T1", asin=f"B0SLICE{rn:03d}")
            for rn in (2, 3, 4)]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    seen = _wire_execute_env(monkeypatch, rows, products)
    written = []
    monkeypatch.setattr(ln.listing_sheet, "write_submit_cols",
                        lambda u: (written.extend(u), len(u))[1])

    def sliced(store, feed_type, items, workflow=None, defer_settle=False):
        yield {"outcome": "submitted", "feed_id": "F-A", "count": 1}
        yield {"outcome": "failed", "feed_id": None, "count": 2}
    monkeypatch.setattr(ln.feeds, "submit_feed", sliced)

    ln.run({"execute": True})
    by_row = dict(written)
    assert by_row[2][4] == "Yes" and by_row[2][5] == "F-A"   # 第一片 = 第 2 行
    assert by_row[3][4] == "No" and by_row[3][7] == "提交被拒"
    assert by_row[4][4] == "No" and by_row[4][7] == "提交被拒"
    # 被拒那片的号全数回收,成功那片一个都不回收
    assert len(seen["released"]) == 2


def test_failed_store_gets_one_serial_second_pass(monkeypatch):
    """店级重试标准①(所有者定稿 2026-08-26):失败店跑完别人后串行补试一遍,
    救回的照常入账 —— 补试跑的是**同一个** _one_store(单一落地路径)。"""
    from socksio.exceptions import ProtocolError
    rows = [_sheet_row(2, store="T1", asin="B0GOOD0001"),
            _sheet_row(3, store="T2", asin="B0SHAKY001")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    _wire_execute_env(monkeypatch, rows, products)
    monkeypatch.setattr(ln.store_retry.time, "sleep", lambda s: None)
    tries = []

    def flaky(store, feed_type, items, workflow=None, defer_settle=False):
        tries.append(store["name"])
        if store["name"] == "T2" and tries.count("T2") == 1:
            raise ProtocolError("Malformed reply")   # 08-26 事故同款,补试即好
        yield {"outcome": "submitted", "feed_id": f"F-{store['name']}",
               "count": len(items)}
    monkeypatch.setattr(ln.feeds, "submit_feed", flaky)

    out = ln.run({"execute": True})
    assert tries.count("T2") == 2          # 首轮 + 补试各一次,不多试
    assert "⚠ 缺席" not in out              # 救回了就不点名
    assert "T2:提交 1 条" in out


def test_second_pass_resubmits_a_byte_identical_payload(monkeypatch):
    """补试是对**写接口**再发一次,安全性全押在「载荷一字不差」上:同载荷 ⇒
    同 payload_key ⇒ api/feeds 的在途防重(pending/submitted)把真重复挡回
    dedup,不会双上架。本用例钉 list_new 这一侧 —— 两次尝试交给 submit_feed
    的条目必须完全相同:不重领占位号、不把变体后缀叠成第二遍。
    (领号侧「同 (店,ASIN) 原号复用」是 services/upc_pool.claim 的契约,
    由它自己的用例钉;这里的桩按该契约回同一个号。)"""
    import copy

    from socksio.exceptions import ProtocolError
    rows = [_sheet_row(2, store="T2", asin="B0SHAKY001")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    _wire_execute_env(monkeypatch, rows, products)
    monkeypatch.setattr(ln.store_retry.time, "sleep", lambda s: None)
    # 底座的 assemble 桩把载荷压成 {"pt": …}(它只观测 UPC),这里换成真形状
    monkeypatch.setattr(ln.mp_mapper, "assemble_mp_item",
                        lambda o, pt, v: {"Orderable": o, "Visible": {pt: v}})
    sent = []

    def flaky(store, feed_type, items, workflow=None, defer_settle=False):
        sent.append(copy.deepcopy(items))       # 快照:之后被就地改也照得出来
        if len(sent) == 1:
            raise ProtocolError("Malformed reply")
        yield {"outcome": "submitted", "feed_id": "F-1", "count": len(items)}
    monkeypatch.setattr(ln.feeds, "submit_feed", flaky)

    ln.run({"execute": True})
    assert len(sent) == 2
    assert sent[0] == sent[1], (sent[0], sent[1])
    # 批次 2:两次发的是**同一个码**(抽码在 _prep_rows,补试不二次抽码);
    # 同码 ⇒ 同 payload_key ⇒ 在途防重把真重复挡回 dedup,不会双上架
    assert sent[0][0]["Orderable"]["sku"] == sent[1][0]["Orderable"]["sku"]
    assert sent[0][0]["Orderable"]["sku"] != rows[0]["asin"]


def test_still_failed_store_is_absent_in_first_line_and_keeps_its_half_work(
        monkeypatch):
    """标准②:补试仍失败 ⇒ **不炸整轮**,缺席店带归类词点名在摘要**首行**
    (链通知只发成功步骤的首行,写在后面等于只写进日志)。

    同时盯**信息零丢失**:逐店那行「(归类词:异常),下轮重试」照旧,
    领号阶段已经攒好的 no_upc 计数与 N 列理由不许随异常一起蒸发。
    """
    from socksio.exceptions import ProtocolError
    rows = [_sheet_row(2, store="T1", asin="B0GOOD0001"),
            _sheet_row(3, store="T2", asin="B0NOUPC001"),
            _sheet_row(4, store="T2", asin="B0DOWN0001")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    _wire_execute_env(monkeypatch, rows, products)
    monkeypatch.setattr(ln.store_retry.time, "sleep", lambda s: None)
    reasons = []
    monkeypatch.setattr(ln.listing_sheet, "write_reasons",
                        lambda rs, *a, **k: (reasons.extend(rs), 0)[1])
    monkeypatch.setattr(ln.upc_pool, "claim", lambda c, wants: [
        None if w["asin"].startswith("B0NOUPC") else "19999" + w["asin"][-7:]
        for w in wants])

    def down(store, feed_type, items, workflow=None, defer_settle=False):
        if store["name"] == "T2":
            raise ProtocolError("Malformed reply")   # 补试也不好
        yield {"outcome": "submitted", "feed_id": "F-1", "count": len(items)}
    monkeypatch.setattr(ln.feeds, "submit_feed", down)

    out = ln.run({"execute": True})          # 不抛 = 不炸链
    first = out.splitlines()[0]
    assert "⚠ 缺席 1 店:T2(代理波动)" in first
    assert "已串行补试仍失败" in first and "未上架行下轮重试" in first
    # 逐店那行的字样一字不改
    assert any(row.strip().startswith("⚠ T2:上架异常已跳过(代理波动:")
               and row.endswith(",下轮重试") for row in out.splitlines()), out
    # 半成品照原样入账:领不到号的行照旧计数、照旧写 N 列
    assert "提交期:UPC池不足 1" in out
    assert (3, "UPC池余量不足") in reasons


def test_same_round_scrape_refreshes_all_candidates(monkeypatch):
    """同轮闭环(2026-08-19 升级:上架必须用当天最新数据):**全部候选**先推
    采集刷新(不只缺数据的)→ 等窗口 → 按批摄取 → 才取数定价提交。

    wait_settled/_ingest_batches 打桩(它们各自有自己的测试),这里只验接线:
    推的是全候选、次序对(推→等→摄取→才取数)、只拉自己那批、摘要说人话。
    """
    rows = [_sheet_row(2, asin="B0AAAAAOK1"), _sheet_row(3, asin="B0AAAAAGAP")]
    full = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    calls = {"order": [], "pushed": []}
    seen = _wire_execute_env(monkeypatch, rows, full)

    def fetch(asins):
        calls["order"].append("fetch")
        return {a: full[a] for a in asins}
    monkeypatch.setattr(ln.amz_source, "fetch_products", fetch)
    monkeypatch.setattr(
        ln, "_push_scrape",
        lambda want, execute: (calls["order"].append("push"),
                               calls["pushed"].extend(want),
                               (f"  候选 {len(want)} 个 ASIN 已推采集刷新",
                                ["listing_gap_test"]))[2])
    monkeypatch.setattr(
        ln.scrape_batches, "wait_settled",
        lambda names, t: (calls["order"].append("wait"), ("1/1 落定", 0))[1])
    monkeypatch.setattr(
        ln, "_ingest_batches",
        lambda names: (calls["order"].append("ingest"),
                       calls.setdefault("ingested_names", []).append(list(names)),
                       "按批摄取:新增 2")[2])
    out = ln.run({"execute": True})
    # 推的是**全部候选**,而且推→等→摄取都发生在取数之前
    assert calls["order"] == ["push", "wait", "ingest", "fetch"]
    assert sorted(calls["pushed"]) == ["B0AAAAAGAP", "B0AAAAAOK1"]
    assert calls["ingested_names"] == [["listing_gap_test"]]  # 只拉自己那批
    assert "同轮闭环" in out
    assert sorted(w["asin"] for w in seen["claim_wants"]) == [
        "B0AAAAAGAP", "B0AAAAAOK1"]          # 刷新后本轮就领号提交
    # gap_wait=0 = 只推不等(不打 wait/ingest,直接用库中现值)
    calls["order"].clear()
    calls["pushed"].clear()
    out2 = ln.run({"execute": True, "gap_wait": 0})
    assert calls["order"] == ["push", "fetch"] and "同轮闭环" not in out2


def test_absent_rows_skip_without_terminal_state(monkeypatch):
    """刷新之后库里仍没有的行:不写终态、次日续上,摘要点名"仍缺"。"""
    rows = [_sheet_row(2, asin="B0AAAAAOK1"), _sheet_row(3, asin="B0AAAAAGAP")]
    have = {"B0AAAAAOK1": {**_PRODUCT_OK, "asin": "B0AAAAAOK1"}}
    seen = _wire_execute_env(monkeypatch, rows, have)
    out = ln.run({"execute": True})
    assert sorted(seen["scrape_pushed"]) == ["B0AAAAAGAP", "B0AAAAAOK1"]
    assert "仍缺 1 个 ASIN 无数据" in out
    assert [w["asin"] for w in seen["claim_wants"]] == ["B0AAAAAOK1"]


def test_map_llm_three_level_fetch(monkeypatch):
    """取数三级(所有者定稿 2026-08-18):一级 hash → 二级 (asin,pt) 复用
    (硬条件签名 + 标题规格验证)→ 才真调 LLM。盯三件事:复用零 LLM 且
    回写新键;验证不过重打;一级命中不重复 put。"""
    calls = {"llm": 0, "put": []}
    monkeypatch.setattr(ln.pt_spec, "orderable_spec", lambda: {})
    monkeypatch.setattr(ln.mp_mapper, "build_llm_messages",
                        lambda *a, **k: [{"role": "user", "content": "m"}])
    monkeypatch.setattr(ln.mp_mapper, "reuse_sig", lambda *a, **k: "SIG1")
    monkeypatch.setattr(ln.mp_mapper, "split_llm_output",
                        lambda raw: (raw.get("visible") or {},
                                     raw.get("orderable") or {}))
    monkeypatch.setattr(ln.mp_mapper, "finalize_visible",
                        lambda pt, rv, spec, **k: rv)
    monkeypatch.setattr(ln.llm_cache, "get", lambda c, k: None)
    monkeypatch.setattr(ln.llm_cache, "put",
                        lambda c, k, r, **meta: calls["put"].append(meta))
    old = {"visible": {"material": "Paper", "count": "48"}, "orderable": {}}
    monkeypatch.setattr(ln.llm_cache, "find_reusable",
                        lambda c, a, p, s: (old, "Gift Bags 48 Pcs Brown"))

    def chat(messages, **kw):
        calls["llm"] += 1
        # ⚠ purpose 必须传:不传就全落进 "default" 桶,摘要里和别的默认调用
        # 混成一坨,换模型时看不出"上架这一段花了多少"(2026-08-21 建的用途)
        assert kw.get("purpose") == "listing_attrs"
        return {"visible": {"material": "Paper"}, "orderable": {}}
    monkeypatch.setattr(ln.llm, "chat_json", chat)

    stats: dict = {}
    # 标题只是措辞变了(数字/规格词都在)→ 二级复用,零 LLM,回写新键
    prod = {"asin": "B0X", "title": "Brown Gift Bags, 48 Pcs, New",
            "attrs": {}}
    v, _o = ln._map_llm(object(), "Cups", {}, prod, stats=stats)
    assert v == {"material": "Paper", "count": "48"}
    assert calls["llm"] == 0 and stats == {"reuse": 1}
    assert calls["put"][-1]["asin"] == "B0X"      # 下轮直接走一级

    # 件数 48→100:数字集合变了 → 验证不过,重打 LLM
    prod2 = {"asin": "B0X", "title": "Brown Gift Bags, 100 Pcs", "attrs": {}}
    ln._map_llm(object(), "Cups", {}, prod2, stats=stats)
    assert calls["llm"] == 1
    assert stats["reuse_miss"] == 1 and stats["llm"] == 1

    # 一级命中:不调 LLM 也不重复 put
    monkeypatch.setattr(ln.llm_cache, "get", lambda c, k: old)
    n_put = len(calls["put"])
    ln._map_llm(object(), "Cups", {}, prod, stats=stats)
    assert calls["llm"] == 1 and len(calls["put"]) == n_put
    assert stats["cache"] == 1


def test_write_reasons_is_one_batched_call(monkeypatch):
    """理由回写必须批量(2026-08-19 所有者实遇):此前逐行一格一请求,
    几百行淘汰理由 = 提交前白耗几分钟。现在 N 行收敛为一次
    sheet_write_ranges(切块归飞书层的双预算)。"""
    from api import feishu
    from services import listing_sheet as ls
    calls = []
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda t, ranges: (calls.append(ranges), len(ranges))[1])
    n = ls.write_reasons([(2, "理由A"), (5, "理由B"), (9, "理由C")])
    assert n == 3 and len(calls) == 1               # 三行一次请求
    assert calls[0][0] == ("P2:P2", [["理由A"]])   # 未上架理由列(layout 算)
    assert ls.write_reasons([], True) == 0 and len(calls) == 1
    assert ls.write_reasons([(3, "x")], execute=False) == 0   # dry-run 不写
    assert len(calls) == 1


def test_main_title_three_shapes():
    """主标题拆分(amz_source.main_title):精确 removesuffix,不按 | 猜。"""
    from services import amz_source as az
    p = {"title": "Main Part | Tail Part", "attrs": {"subtitle": "Tail Part"}}
    assert az.main_title(p) == "Main Part"
    assert az.main_title({"title": "Short Only", "attrs": {}}) == "Short Only"
    # 改版前老记录:subtitle 空但 title 是拼好的长串 → 不猜,None
    assert az.main_title({"title": "A | B", "attrs": {"subtitle": None}}) is None
    assert az.main_title({"title": "尾巴不匹配 | X",
                          "attrs": {"subtitle": "Y"}}) is None
    assert az.main_title({"title": "", "attrs": {"subtitle": "Y"}}) is None


def test_out_of_scope_store_skips_claim_gates(monkeypatch):
    """规划外店(谭总系)上架**不受产品/品牌占用管**(所有者定稿 2026-08-15
    「既不占用、也不拦别人」;2026-08-19 生产实证补全这个方向)。
    同一现状下规划内的店照旧被占用闸拦,豁免不外溢。
    2026-08-28 取消全局去重后,「别店在架」本身不再拦任何人 —— T1 被拦
    只因占用闸(跨店纪律由 claims 台账独自承担)。
    """
    rows = [_sheet_row(2, store="谭总4", asin="B0AAAAAOK1"),
            _sheet_row(3, store="T1", asin="B0AAAAAOK1")]
    products = {"B0AAAAAOK1": {**_PRODUCT_OK, "asin": "B0AAAAAOK1",
                               "brand": "SomeBrand"}}
    seen = _wire_execute_env(monkeypatch, rows, products)
    # 该 ASIN 已在**别店**在架(不构成拦截)+ 产品/品牌都被别店占用(拦 T1)
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, {("A085", "B0AAAAAOK1")}, {}, set(),
        {"banned_pts": set(), "brands": set()},
        {"B0AAAAAOK1": "A085"},
        {ln.brand_key.brand_key("SomeBrand", None): "A085"},
        {}, set()))
    monkeypatch.setattr(ln.stores_svc, "load_stores",
                        lambda names=None: [{"name": "谭总4"}, {"name": "T1"}])
    monkeypatch.setattr(ln.store_limits, "price_multipliers",
                        lambda: {s: {"fbm_range1": "200%"}
                                 for s in ("谭总4", "T1")})
    out = ln.run({"execute": True})
    # T1 被**占用闸**拦在领号之前(claimed 计数历来不进闸门行,理由走 N 列),
    # 谭总4 豁免占用照常提交 —— 两个可观测面共同证明拦截发生在占用闸:
    assert [w["asin"] for w in seen["claim_wants"]] == ["B0AAAAAOK1"]  # 只有谭总
    assert seen["submitted"] == [("谭总4", 1)]
    assert "本店已在架" not in out              # 别店在架不再是拦截理由


def test_out_of_scope_holders_do_not_block_others(monkeypatch):
    """占用台账里持有人是规划外店的行,加载时剔除——它们不占任何产品/品牌。"""
    import contextlib

    class _Cur:
        def execute(self, sql, args=None):
            self.sql = sql

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    monkeypatch.setattr(ln.db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([_Conn()])))
    monkeypatch.setattr(ln.blacklist, "load_banned_asins", lambda c: {})
    monkeypatch.setattr(ln.risk_gate, "load_gate",
                        lambda c: {"banned_pts": set(), "brands": set()})
    monkeypatch.setattr(
        ln.claims, "load_active",
        lambda c, kind: ({"B0TANZHELD1": "谭总4", "B0NORMAL01": "A085"}
                         if kind == ln.claims.PRODUCT
                         else {"somebrand": "谭总4"}))
    gs = ln._load_gate_state()
    assert gs.owned_asin == {"B0NORMAL01": "A085"}   # 谭总持有的不拦别人
    assert gs.owned_brand == {}


# ── LLM 花费上报(所有者 2026-08-21:「上架我也希望可以输出花了多少钱」)────

def test_listing_llm_purpose_is_registered_not_falling_into_default():
    """上架出参有自己的用途桶 —— 落进 "default" 就和别的默认调用混成一坨。

    摘要按**用途**分行,那是换模型时唯一有用的维度:要回答"上架这一段值不值得
    换个更便宜的模型",就得先能把它单独看见。
    """
    from registry import resources
    assert "listing_attrs" in resources.LLM_PURPOSE_ENV


def test_cost_is_reported_at_every_exit_including_dry_run():
    """⚠ 每一个 return 之前都要报,**dry-run 也不例外**。

    `-p check_spec=1` 的 spec 预检是**真调 LLM** 的(那行提示自己写着
    "会真调 LLM,但不领 UPC 不提交")。漏报就是白花钱不留痕 —— 而 dry-run
    恰恰是人最容易以为"不花钱"的那条路。
    """
    import inspect
    src = inspect.getsource(ln.run)
    exits = [i for i, line in enumerate(src.splitlines())
             if line.strip() == 'return "\\n".join(lines)']
    assert exits, "run() 的出口形状变了,这条守卫要跟着改"
    body = src.splitlines()
    for i in exits:
        # 往回看几行,必须有花费上报(中间可能隔着 append 一两行)
        window = "\n".join(body[max(0, i - 4):i])
        assert "_llm_cost_lines" in window, f"第 {i} 行的 return 没报花费"


def test_no_llm_call_means_no_noise_line():
    """一次都没调 LLM 时不许多打一行 —— 摘要里每一行都得是有信息量的。"""
    from api import llm as _llm
    saved = dict(_llm.USAGE_STATS)
    _llm.USAGE_STATS.clear()
    try:
        assert ln._llm_cost_lines(100) == []
    finally:
        _llm.USAGE_STATS.update(saved)


def test_denominator_is_rows_that_entered_prep_not_rows_that_succeeded():
    """每千条的分母是**进过预备期**的行,不是提交成功的行。

    出参失败 / 必填缺失的行钱照花,拿成功数当分母会把单价算低 —— 而这个数
    正是用来推整批预算的。
    """
    import inspect
    src = inspect.getsource(ln.run)
    assert "_llm_cost_lines(len(prep_in))" in src
    assert "_llm_cost_lines(len(prep_ok))" not in src


# ── 多仓:上架仓(批次 3)────────────────────────────────────────────────────

def test_listing_uses_the_managed_node_as_fulfillment_center(monkeypatch):
    """配置了「维护仓库」的店:MP_ITEM 的 fulfillmentCenterID = 那个 FC ID。

    ⚠ 恒填 Partner ID 是**退化写法**:对已建实体仓的店铺是错的 —— 货上到
    Virtual Node,而运费模板挂在新仓上,回执一切正常。
    """
    rows = [_sheet_row(2, asin="B0AAAAAOK1")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    seen = _wire_execute_env(monkeypatch, rows, products)
    monkeypatch.setattr(ln.store_limits, "managed_nodes",
                        lambda stores=None: ({"T1": "91539778610008065"}, {}))
    ln.run({"execute": True})
    assert seen["orderable_fcs"] == ["91539778610008065"]


def test_listing_skips_the_store_when_the_node_fails_validation(monkeypatch):
    """校验失败**整店跳过,不回落 Virtual Node**。

    回落等于把本该进新仓的货上到旧节点,而且全程不报错 —— 比"这店今天没上架"
    坏得多(与 services/store_limits.resolve_node 同一条口径)。
    """
    rows = [_sheet_row(2, asin="B0AAAAAOK1")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    seen = _wire_execute_env(monkeypatch, rows, products)
    monkeypatch.setattr(ln.store_limits, "managed_nodes",
                        lambda stores=None: ({}, {"T1": "填的 999 认不出"}))
    out = ln.run({"execute": True})
    assert seen["claim_wants"] == [] and seen["submitted"] == []
    assert "整店跳过" in out and "不回落 Virtual Node" in out


def test_missing_limits_table_is_logged_not_silent(monkeypatch, caplog):
    """⚠ 同一张限额表,本链与分配链的降级方向**相反**,所以降级必须留痕。

    这里读不到 ⇒ 默认不限、照常上架(旧语义,故意不改:改成硬拒会在飞书抖动
    时停掉生产上架线);分配链读不到 ⇒ 硬拒。最坏组合是**上架侧放开、分配侧
    关停** —— 货照上,却没人在决定该上什么。

    原来是静默 `return {}`:运行摘要上完全看不出今天的上架是"按限额跑的"
    还是"限额没读到、全店不限"。
    """
    import logging as _lg
    monkeypatch.setattr(ln.feishu, "list_records",
                        lambda *a, **k: (_ for _ in ()).throw(LookupError("未登记")))
    with caplog.at_level(_lg.WARNING, logger="workflows.list_new"):
        assert ln._load_quota() == {}
    # getMessage() 才是格式化后的整句(message 是模板,args 还没代入)
    assert any("所有店按不限量上架" in r.getMessage() for r in caplog.records)


# ── 店铺事件账本(运营类:每店每轮一条)────────────────────────────────────

def _capture_rounds(monkeypatch):
    """输入:monkeypatch → 输出:收 record_round 调用的列表。

    盯 `record_round` 而不是 `record_round_safe`:后者把一切异常吞掉(那是
    它的职责),用它当探针的话"根本没调"与"调了但炸了"分不开。
    """
    got: list = []
    monkeypatch.setattr(ln.store_events, "record_round",
                        lambda conn, source, event, per_store:
                        (got.append((source, event, dict(per_store))),
                         len(per_store))[1])
    return got


def test_execute_records_one_round_event_per_store(monkeypatch):
    """真跑:每店一条,detail 是这一轮的计数(不复制任何流水)。"""
    rows = [_sheet_row(2, store="T1", asin="B0AAAAAOK1"),
            _sheet_row(3, store="T1", asin="B0AAAAABAD"),   # 必填缺失(预备期)
            _sheet_row(4, store="T2", asin="B0BBBBBOK1")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    _wire_execute_env(monkeypatch, rows, products)
    got = _capture_rounds(monkeypatch)
    ln.run({"execute": True})
    assert len(got) == 1
    source, event, per_store = got[0]
    assert (source, event) == ("list_new", ln.store_events.LIST_ROUND)
    assert sorted(per_store) == ["T1", "T2"]
    assert per_store["T1"]["submitted"] == 1
    assert per_store["T1"]["invalid"] == 1      # 预备期拦下的按店记
    assert per_store["T2"]["submitted"] == 1 and "invalid" not in per_store["T2"]
    assert per_store["T1"]["failed"] == 0 and per_store["T1"]["unknown"] == 0


def test_dry_run_records_no_round_event(monkeypatch):
    """★ 空跑一条都不许记:dry-run 什么都没提交,记了就是一整轮假流水。"""
    rows = [_sheet_row(2, store="T1", asin="B0AAAAAOK1")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    _wire_execute_env(monkeypatch, rows, products)
    got = _capture_rounds(monkeypatch)
    ln.run({"execute": False})
    assert got == []


# ── SKU 改造批次 2:写侧切换(mint / 载荷 / 回写 / 两道码闸 / limit)───────────
#
# 本批是整个 SKU 改造里唯一有行为变化的一批。下面的用例分四组盯四件事:
#   ① 码在**预备期**抽、每行一次、挂 r["_sku"](挪进 _one_store = 双上架);
#   ② 载荷 / mark_used / 事件 / 上架表 SKU 列写的都是那个码,不是 ASIN;
#   ③ dry-run 一次都不抽码(写库函数不设 dry_run 开关,靠调用方分路);
#   ④ 两道新闸(代际上限 / 退役冷却)与试点 limit 的命中、放行与顺序。


def _wire_dry_env(monkeypatch, rows, *, listed=(), cooling=None, over_gen=()):
    """dry-run 路径的标准桩:闸门数据面可注入,**mint 一被调到就当场炸**。

    "空跑不写库"这条红线在 list_new 里靠位置保证(`if not execute:` 早于
    `_prep_rows` return),但位置是会被挪的 —— 所以这里把 mint 桩成抛断言,
    哪天有人把抽码挪到闸门段,红的是这一整组用例而不是生产。
    """
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    stores = sorted({r["store"] for r in rows})
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, set(listed), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {},
        dict(cooling or {}), set(over_gen)))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln.store_limits, "price_multipliers",
                        lambda: {s: {"fbm_range1": "200%"} for s in stores})
    monkeypatch.setattr(ln.stores_svc, "load_stores",
                        lambda names=None: [{"name": s} for s in stores])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.pt_spec, "orderable_spec", lambda: {})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: products)
    monkeypatch.setattr(ln, "_push_scrape", lambda want, execute: (None, []))
    monkeypatch.setattr(ln, "_sync_upc", lambda execute, lines: None)

    def _no(*a, **k):
        raise AssertionError("dry-run 不许写库/不许提交")
    monkeypatch.setattr(ln.sku_codec, "mint", _no)
    monkeypatch.setattr(ln.upc_pool, "claim", _no)
    monkeypatch.setattr(ln.listing_sources, "register", _no)
    monkeypatch.setattr(ln.feeds, "submit_feed", _no)
    return products


def test_mint_happens_in_prep_not_in_one_store(monkeypatch):
    """★ 抽码在 `_prep_rows`,**不在** `_one_store`:串行补试重跑的是后者。

    抽码若在 _one_store 里,补试会抽出第二个码 ⇒ 载荷不再一字不差 ⇒
    api/feeds.payload_key 的在途防重不命中 ⇒ 首轮已发出去的那片被真的再发
    一次 = 双上架,而且全程不报错。这里钉的就是"补试没有二次抽码"。
    """
    from socksio.exceptions import ProtocolError
    rows = [_sheet_row(2, store="T1", asin="B0GOOD0001"),
            _sheet_row(3, store="T2", asin="B0SHAKY001")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    seen = _wire_execute_env(monkeypatch, rows, products)
    monkeypatch.setattr(ln.store_retry.time, "sleep", lambda s: None)
    tries = []

    def flaky(store, feed_type, items, workflow=None, defer_settle=False):
        tries.append(store["name"])
        if store["name"] == "T2" and tries.count("T2") == 1:
            raise ProtocolError("Malformed reply")
        yield {"outcome": "submitted", "feed_id": "F-1", "count": len(items)}
    monkeypatch.setattr(ln.feeds, "submit_feed", flaky)

    ln.run({"execute": True})
    assert tries.count("T2") == 2                  # _one_store 真跑了两遍
    assert len(seen["minted"]) == len(rows)        # 而码只抽了行数次
    assert [m[3] for m in seen["minted"]] == ["list_new"] * len(rows)
    assert [m[1] for m in seen["minted"]] == \
        [ln.listing_sources.SOURCE_AMZ] * len(rows)


def test_code_is_committed_before_any_feed_call(monkeypatch):
    """★ 全部抽码都早于第一次 submit_feed(防重状态先落库再调接口)。

    码在任何一次外部调用之前就已经 commit,进程半路死掉重跑拿到的是同一个码
    (mint 的复用语义),不会每轮换码白烧一个 UPC。
    """
    rows = [_sheet_row(2, store="T1", asin="B0AAAAAOK1"),
            _sheet_row(3, store="T2", asin="B0BBBBBOK2")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    _wire_execute_env(monkeypatch, rows, products)
    trace = []
    real_mint = ln.sku_codec.mint

    def traced_mint(*a, **k):
        trace.append("mint")
        return real_mint(*a, **k)
    monkeypatch.setattr(ln.sku_codec, "mint", traced_mint)

    def traced_submit(store, feed_type, items, workflow=None,
                      defer_settle=False):
        trace.append("submit")
        yield {"outcome": "submitted", "feed_id": "F-1", "count": len(items)}
    monkeypatch.setattr(ln.feeds, "submit_feed", traced_submit)

    ln.run({"execute": True})
    assert trace.count("mint") == 2 and trace.count("submit") == 2
    assert trace[:2] == ["mint", "mint"], trace   # 抽码全在提交之前


def test_duplicate_rows_reusing_one_code_are_counted_out_loud(
        monkeypatch, caplog):
    """同 (店, ASIN) 在上架表贴了两行 ⇒ mint 复用同一个码,**必须数出来**。

    两个随机码相同不像两个 ASIN 相同那样扎眼,不数出来没人看得见。
    """
    rows = [_sheet_row(2, store="T1", asin="B0SAMEASIN"),
            _sheet_row(3, store="T1", asin="B0SAMEASIN")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    _wire_execute_env(monkeypatch, rows, products)
    with caplog.at_level("INFO", logger="workflows.list_new"):
        ln.run({"execute": True})
    assert "本轮 2 行只用了 1 个码" in caplog.text


def test_payload_sku_is_the_minted_code_not_the_asin(monkeypatch):
    """★ 发给沃尔玛的 Orderable.sku 是登记簿里那个码,不是 ASIN。

    同一个值也进 mp_conform 的 sku=(单品占位 variantGroupId):只改一处会出现
    「Orderable.sku 是新码、variantGroupId 还是 ASIN」的半身像,而
    variantGroupId 也是发出去的,等于把 ASIN 从后门递出去。
    """
    rows = [_sheet_row(2, store="T1", asin="B0AAAAAOK1")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    seen = _wire_execute_env(monkeypatch, rows, products)
    ln.run({"execute": True})
    code = "A" + "B0AAAAAOK1"[-6:].rjust(11, "X")
    assert seen["orderable_skus"] == [code]        # 载荷主键
    assert seen["conform_skus"] == [code]          # 单品 variantGroupId 同源
    assert code != rows[0]["asin"]


def test_mark_used_and_events_carry_the_code_not_the_asin(monkeypatch):
    """upc_pool.sku 与 product_events.sku 写的都是真发出去的码;

    事件 detail 另带一份 asin —— 不透明码在 product_events.asin 列里提不出来,
    而这里 ASIN 本来就在手边。upc_pool 的复用键 (store, asin) 不受影响。
    """
    rows = [_sheet_row(2, store="T1", asin="B0AAAAAOK1")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    seen = _wire_execute_env(monkeypatch, rows, products)
    ln.run({"execute": True})
    code = "A" + "B0AAAAAOK1"[-6:].rjust(11, "X")
    assert [sku for _upc, sku in seen["marked_used"]] == [code]
    assert [w["asin"] for w in seen["claim_wants"]] == ["B0AAAAAOK1"]
    ev = seen["events"][0]
    assert ev["sku"] == code and ev["event"] == ln.product_events.LIST_SUBMITTED
    assert ev["detail"]["asin"] == "B0AAAAAOK1"


def test_registration_happens_at_mint_not_after_submit(monkeypatch):
    """★ 提交成功后**不再** listing_sources.register:登记已在 mint 里做完。

    留着 register 就是同一能力两条实现路径(双轨禁止),而且会让下一个人以为
    登记发生在提交之后,进而把 mint 挪到提交后去 —— 那正是双上架的入口。
    """
    rows = [_sheet_row(2, store="T1", asin="B0AAAAAOK1")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    seen = _wire_execute_env(monkeypatch, rows, products)
    ln.run({"execute": True})
    assert seen["registered"] == []
    assert len(seen["minted"]) == 1
    # 源码级(AST,不看注释与文档串):list_new 里一个 register 调用都不许剩
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(ln))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "register"
             and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "listing_sources"]
    assert calls == []


def test_submit_writes_the_code_into_the_sku_column(monkeypatch):
    """三种结局都把码作为第 9 值写进 SKU 列。

    SKU 列的语义是「这一行当前持有的码」而不是「已成功上架的码」:
    failed / unknown 的行下一轮用同一个码重来,运营要能从表上看到那个串;
    三分支同写也让回写只有一条路径,没有"哪些结局写码"这种要记的规则。
    """
    rows = [_sheet_row(2, store="TOK", asin="B0AAAAAOK1"),
            _sheet_row(3, store="TNO", asin="B0BBBBBNO1"),
            _sheet_row(4, store="TUN", asin="B0CCCCCUN1")]
    products = {r["asin"]: {**_PRODUCT_OK, "asin": r["asin"]} for r in rows}
    seen = _wire_execute_env(monkeypatch, rows, products)
    outcome = {"TOK": "submitted", "TNO": "failed", "TUN": "unknown"}

    def by_store(store, feed_type, items, workflow=None, defer_settle=False):
        yield {"outcome": outcome[store["name"]], "feed_id": "F-1",
               "count": len(items)}
    monkeypatch.setattr(ln.feeds, "submit_feed", by_store)

    ln.run({"execute": True})
    got = {rn: vals for rn, vals in seen["submit_vals"]}
    assert sorted(got) == [2, 3, 4]
    for rownum, asin in ((2, "B0AAAAAOK1"), (3, "B0BBBBBNO1"),
                         (4, "B0CCCCCUN1")):
        vals = got[rownum]
        assert len(vals) == 9, vals
        assert vals[8] == "A" + asin[-6:].rjust(11, "X")
    assert [got[2][4], got[3][4], got[4][4]] == ["Yes", "No", "Unknown"]


def test_dry_run_uses_a_placeholder_code_and_writes_nothing(monkeypatch):
    """★ 空跑一次都不抽码、不领号、不登记(桩里这三个一被调到就抛)。"""
    rows = [_sheet_row(2, store="T1", asin="B0AAAAAOK1")]
    _wire_dry_env(monkeypatch, rows)
    out = ln.run({"execute": False})
    assert "[DRY-RUN] 共 1 行将进入" in out


def test_spec_precheck_payload_uses_the_placeholder_code(monkeypatch):
    """check_spec 预检用 `sku_codec.DRYRUN_PLACEHOLDER`,不 mint。

    占位码含 `0`(不在字母表里)⇒ is_opaque 恒 False,形态上就不可能与任何一个
    真 mint 出来的码相撞;抬头行说破它是占位的,免得被当成真发出去的串。
    """
    rows = [_sheet_row(2, store="T1", asin="B0AAAAAOK1")]
    _wire_dry_env(monkeypatch, rows)
    monkeypatch.setattr(ln.db, "pg_conn",
                        contextlib.contextmanager(
                            lambda **kw: iter([object()])))
    monkeypatch.setattr(ln, "_map_llm",
                        lambda c, pt, spec, p, stats=None:
                            ({"productName": p["title"]}, {}))
    monkeypatch.setattr(ln, "_variant_plan", lambda *a, **k: None)
    skus = []
    monkeypatch.setattr(ln.mp_mapper, "build_orderable",
                        lambda sku, upc, *a, **k: (skus.append(str(sku)), {})[1])

    def _conform(*a, **k):
        skus.append("conform:" + str(k.get("sku", "")))
        return a[2], a[3], [], []
    monkeypatch.setattr(ln.mp_conform, "conform", _conform)

    out = ln.run({"execute": False, "check_spec": "1"})
    ph = ln.sku_codec.DRYRUN_PLACEHOLDER
    assert skus == [ph, "conform:" + ph], skus
    assert not ln.sku_codec.is_opaque(ph)          # 形态上就不是真码
    assert f"sku 用占位码 {ph}" in out


def test_gate_state_fields_are_appended_not_inserted():
    """两个新字段**追加在末尾**:_GateState 是按位置构造的,插中间全部错位,

    而错位不报错(集合与字典长得都一样)。
    """
    assert ln._GateState._fields[:8] == (
        "inactive", "today_used", "listed_pairs", "banned", "unexplained",
        "gate", "owned_asin", "owned_brand")
    assert ln._GateState._fields[8:] == ("cooling", "over_gen")


def test_cooldown_sql_scopes_the_registry_join_to_amz():
    """冷却键按 (店, **ASIN**) 建,且登记簿 JOIN 限定 amz。

    不限 amz 的话跟卖行的 source_key 是 GTIN,冷却键按 GTIN 建、与闸判用的
    r["asin"] 永远对不上 ⇒ 跟卖品的冷却恒不生效且不报错。
    事件码走常量:回执码是 {kind}_feed_{status} 派生的,写字面量的话
    _FEED_KIND 一改取值,这条 SQL 会静默返回空集。
    """
    q = ln._SQL_RETIRE_COOLDOWN
    assert "ls.source_type = 'amz'" in q
    assert "coalesce(ls.source_key, e.sku)" in q
    assert "LEFT JOIN catalog.listing_sources ls" in q
    assert "'retire_feed_success'" not in q and "%(event)s" in q
    assert ln.product_events.RETIRE_FEED_SUCCESS == "retire_feed_success"
    assert ln.product_events.RETIRE_FEED_SUCCESS in ln.product_events.EVENTS
    # 代际计数按 source_key 分组:按 sku 分组每行恒 1,闸永不命中
    g = ln._SQL_ABANDONED_GEN
    assert "GROUP BY 1, 2" in g and "HAVING count(*) >= %(cap)s" in g
    assert "abandoned_at IS NOT NULL" in g


def test_cooldown_and_generation_thresholds_come_from_sku_codec():
    """两个阈值的唯一出处是 services/sku_codec,list_new 只引用常量名。"""
    assert ln.sku_codec.RETIRE_COOLDOWN_HOURS == 24
    assert ln.sku_codec.MAX_SKU_GENERATIONS == 3
    import inspect
    src = inspect.getsource(ln._load_gate_state)
    assert "sku_codec.RETIRE_COOLDOWN_HOURS" in src
    assert "sku_codec.MAX_SKU_GENERATIONS" in src


def test_generation_cap_stops_the_code_churn_loop(monkeypatch):
    """★ 换码次数达上限的 (店, ASIN) 不再自动重上,写 N 理由点名待人工。

    堵的是「弃码 → 新码 → 再弃码」这个闭环:每转一圈白烧一个 UPC 与一个
    MP_ITEM 配额名额,而且重试上限/在途防重/原号复用三条护栏跟着码重新计数。
    """
    rows = [_sheet_row(2, store="T1", asin="B0CHURN001"),
            _sheet_row(3, store="T1", asin="B0AAAAAOK1")]
    _wire_dry_env(monkeypatch, rows, over_gen={("T1", "B0CHURN001")})
    out = ln.run({"execute": False})
    assert "第2行:换码次数达上限,待人工" in out
    assert "换码达上限 1" in out
    assert "[DRY-RUN] 共 1 行将进入" in out      # 另一行照常放行


def test_retire_cooldown_gate_holds_the_row_and_names_it(monkeypatch):
    """★ 刚退役成功的 (店, ASIN) 冷却期内不重上;冷却期满(不在快照里)放行。

    命中只写 N 理由**不写终态** —— 冷却期满下一轮自动续上,与既有闸门同语义。
    """
    from datetime import datetime
    rows = [_sheet_row(2, store="T1", asin="B0COOL0001")]
    _wire_dry_env(monkeypatch, rows,
                  cooling={("T1", "B0COOL0001"): datetime(2026, 9, 2)})
    out = ln.run({"execute": False})
    assert "第2行:退役冷却中" in out and "退役冷却中 1" in out
    assert "将进入 领UPC→LLM→提交" not in out
    # 冷却期满:_SQL_RETIRE_COOLDOWN 的时间窗把它筛掉 ⇒ 快照里没有 ⇒ 放行
    _wire_dry_env(monkeypatch, rows)
    out2 = ln.run({"execute": False})
    assert "退役冷却中" not in out2
    assert "[DRY-RUN] 共 1 行将进入" in out2


def test_new_gates_sit_between_dedup_and_claims(monkeypatch):
    """顺序即语义:一行同时命中在架/代际/冷却,N 列写的是「本店已在架」。

    已在架的行压根不是"再上架",不该走到这两道闸;两道之间代际在前,因为它
    是要人介入的终局判断,冷却只是等一等。
    """
    rows = [_sheet_row(2, store="T1", asin="B0ALL3HIT1")]
    from datetime import datetime
    _wire_dry_env(monkeypatch, rows, listed={("T1", "B0ALL3HIT1")},
                  over_gen={("T1", "B0ALL3HIT1")},
                  cooling={("T1", "B0ALL3HIT1"): datetime(2026, 9, 2)})
    out = ln.run({"execute": False})
    assert "第2行:本店已在架:同店重复上架拦截" in out
    assert "换码次数达上限" not in out and "退役冷却中" not in out
    # 去掉在架事实之后,先命中的是代际(要人做的那条),不是冷却
    _wire_dry_env(monkeypatch, rows, over_gen={("T1", "B0ALL3HIT1")},
                  cooling={("T1", "B0ALL3HIT1"): datetime(2026, 9, 2)})
    out2 = ln.run({"execute": False})
    assert "第2行:换码次数达上限,待人工" in out2
    assert "退役冷却中" not in out2


def test_limit_truncates_after_the_gates_not_before(monkeypatch):
    """★ `-p limit=` 在**全部闸门与数据过滤之后**切:被淘汰行不占名额。

    切在前面的话 -p limit=1 可能一行都上不了(名额被注定淘汰的那行占了),
    人会以为功能坏了。与配额切片同一条纪律。
    """
    rows = [_sheet_row(2, store="T1", asin="B0LISTED01"),   # 去重闸拦掉
            _sheet_row(3, store="T1", asin="B0AAAAAOK1"),
            _sheet_row(4, store="T1", asin="B0BBBBBOK2")]
    _wire_dry_env(monkeypatch, rows, listed={("T1", "B0LISTED01")})
    out = ln.run({"execute": False, "limit": 1})
    assert "[DRY-RUN] T1 B0AAAAAOK1" in out       # 名额给了幸存者
    assert "B0BBBBBOK2 定价" not in out
    assert "[DRY-RUN] 共 1 行将进入" in out


def test_limit_says_how_many_it_left_behind(monkeypatch):
    """截断必须说出留了多少行给下一轮(不写 N 理由不写终态)。"""
    rows = [_sheet_row(2, store="T1", asin="B0AAAAAOK1"),
            _sheet_row(3, store="T1", asin="B0BBBBBOK2"),
            _sheet_row(4, store="T1", asin="B0CCCCCOK3")]
    _wire_dry_env(monkeypatch, rows)
    out = ln.run({"execute": False, "limit": 1})
    assert "人工上限 -p limit=1:本轮只做前 1 行,其余 2 行留到下一轮" in out


def test_limit_absent_means_no_truncation(monkeypatch):
    """不传 limit ⇒ 与今天逐字一致:一行都不截,摘要里没有那句话。"""
    rows = [_sheet_row(2, store="T1", asin="B0AAAAAOK1"),
            _sheet_row(3, store="T1", asin="B0BBBBBOK2")]
    _wire_dry_env(monkeypatch, rows)
    out = ln.run({"execute": False})
    assert "[DRY-RUN] 共 2 行将进入" in out
    assert "人工上限" not in out
    # -p limit=0 与不传等价(`or None`):0 不是"一行都不做"的开关
    out0 = ln.run({"execute": False, "limit": 0})
    assert "[DRY-RUN] 共 2 行将进入" in out0 and "人工上限" not in out0
