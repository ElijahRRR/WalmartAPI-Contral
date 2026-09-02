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


def _sheet_row(rownum, **kw):
    d = dict(zip(resources.LISTING_SHEET.columns, [""] * 21))
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
    assert w["O2:Q2"][0] == "SUCCESS"
    assert w["O3:Q3"][0] == "SUCCESS"        # ASYNC 转正
    assert "回填 2 行" in out


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
        {}, {}))                       # 占用台账为空 = 占用闸恒放行
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
    assert marked == [asin]
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
        {"banned_pts": set(), "brands": set()}, {}, {}))
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
        {"banned_pts": set(), "brands": set()}, {}, {}))
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
        {"banned_pts": set(), "brands": set()}, {}, {}))
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
        {"banned_pts": set(), "brands": set()}, {}, {}))
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
        {"banned_pts": set(), "brands": set()}, {}, {}))
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
        {}, set(), {"banned_pts": set(), "brands": set()}, {}, {}))
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
        {"banned_pts": set(), "brands": set()}, {}, {}))
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
        {"banned_pts": set(), "brands": set()}, {}, {}))
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
    assert w["K2:Q2"][0] == "Yes" and w["K2:Q2"][1] == "F1"
    assert w["K2:Q2"][2] == "2026-08-10"          # 原上架日期不被覆盖
    assert w["K3:Q3"][0] == "No" and w["K3:Q3"][4] == "FAILED"
    assert "ERR_X | 字段坏了" in w["K3:Q3"][5]     # P 列码+人话
    assert w["K4:N4"][0] == "Yes"                 # 目录在线只写 K~N,不碰 O/P/Q
    assert "K5:Q5" not in w and "K5:N5" not in w  # 无依据绝不负向写
    assert "K6:Q6" not in w and "K6:N6" not in w  # 非 Unknown 不碰
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
        {"banned_pts": set(), "brands": set()}, {}, {}))
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
        {"acme": "OTHER"}))                                 # 品牌占用
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
        {"banned_pts": set(), "brands": set()}, {}, {}))
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
            "assembled_upcs": [], "submitted": []}
    # 起跑抖动在单元测试里一律关掉(缺省 0~800ms × 每店会让多店用例真睡几秒)。
    # 关的是**常量默认值**;要测抖动的用例自己传 submit_jitter_ms
    monkeypatch.setattr(ln, "SUBMIT_JITTER_MS", 0)
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: ln._GateState(
        set(), {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {}))
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
    monkeypatch.setattr(ln.upc_pool, "mark_used", lambda *a, **k: 0)
    monkeypatch.setattr(ln, "_map_llm",
                        lambda c, pt, spec, p, stats=None: ({"productName": p["title"]}, {}))
    seen["orderable_fcs"] = []
    monkeypatch.setattr(
        ln.mp_mapper, "build_orderable",
        lambda sku, upc, price, qty, partner, **k: (
            seen["orderable_upcs"].append(str(upc)),
            seen["orderable_fcs"].append(str(partner)),
            {"productIdentifiers": {"productId": str(upc),
                                    "productIdType": "UPC"}})[2])
    # 每店第二类行(…BAD)必填缺失 → 预备期本地拦下
    monkeypatch.setattr(ln.mp_conform, "conform", lambda *a, **k: (
        a[2], a[3], [],
        ["brand"] if str(k.get("sku", "")).endswith("BAD") else []))
    monkeypatch.setattr(
        ln.mp_mapper, "assemble_mp_item",
        lambda o, pt, v: (seen["assembled_upcs"].append(
            o["productIdentifiers"]["productId"]), {"pt": pt})[1])
    monkeypatch.setattr(ln.listing_sources, "register", lambda *a, **k: None)
    monkeypatch.setattr(ln.product_events, "record_many", lambda *a, **k: None)
    monkeypatch.setattr(ln.listing_sheet, "write_reasons", lambda *a, **k: 0)
    monkeypatch.setattr(ln.listing_sheet, "write_data_cols", lambda *a, **k: 0)
    monkeypatch.setattr(ln.listing_sheet, "write_submit_cols", lambda u: len(u))

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
    assert calls[0][0] == ("N2:N2", [["理由A"]])
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
        {ln.brand_key.brand_key("SomeBrand", None): "A085"}))
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
