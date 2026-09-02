"""订单域地基回归:行标识同构、三源归一化、returns 翻页实证语义、recon CSV、upsert。"""

import csv
import hashlib
import io
import time
import zipfile

import httpx
import pytest

from api import _client, reports, returns as returns_api
from services import order_lines as ol

STORE = {"name": "T1", "client_id": "cid_ord", "client_secret": "sec", "proxy": None}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setenv("WALMART_DATA_ROOT", str(tmp_path))
    _client._token_cache.clear()
    _client._rate_state.clear()
    for c in _client._client_pool.values():
        c.close()
    _client._client_pool.clear()
    monkeypatch.setattr(time, "sleep", lambda s: None)
    yield
    for c in _client._client_pool.values():
        c.close()
    _client._client_pool.clear()
    _client._token_cache.clear()
    _client._rate_state.clear()


def _use(monkeypatch, handler):
    def full_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})
        return handler(request)
    monkeypatch.setattr(_client, "_build_transport",
                        lambda proxy: httpx.MockTransport(full_handler))


# ── 行标识 v3:(PO, SKU),店铺/行号不参与身份 ─────────────────────────────────

def test_order_line_id_v3_semantics():
    # v3 定稿:身份 = (PO, SKU)——同一 PO 内同一 SKU 必合并为一行(所有者实证);
    # 店铺不参与(我方标签,改名不得作废行标识);行号只存列做展示
    def reference(po, sku):
        raw = f"{po}\x1f{sku}"
        return "ol_" + hashlib.sha256(raw.encode()).hexdigest()[:24]

    assert ol.make_order_line_id("108888888888", "B0X1") == reference("108888888888", "B0X1")
    # 仅去首尾空白归一;SKU 大小写敏感,不动
    assert ol.make_order_line_id("PO1", " B0X1 ") == ol.make_order_line_id("PO1", "B0X1")
    assert ol.make_order_line_id("PO1", "b0x1") != ol.make_order_line_id("PO1", "B0X1")
    assert ol.make_order_line_id("PO1", "A") != ol.make_order_line_id("PO1", "B")
    assert ol.norm_line("2.0") == "2"          # 行号归一仍在(展示列用)


# ── 源1:订单展开 ─────────────────────────────────────────────────────────────

_ORDER = {
    "purchaseOrderId": "108000000001", "customerOrderId": "200000001",
    "orderDate": 1754300000000,
    "shippingInfo": {"phone": "555", "postalAddress": {
        "name": "N", "address1": "A1", "city": "C", "state": "TX",
        "postalCode": "75001", "country": "USA"}},
    "orderLines": {"orderLine": [{
        "lineNumber": "1", "statusDate": 1785722223000,
        "item": {"sku": "B0AAAA1111", "productName": "P"},
        "orderLineQuantity": {"amount": "2"},
        "orderLineStatuses": {"orderLineStatus": {
            "status": "Shipped", "statusSetDate": 1754310000000,
            "trackingInfo": {"carrierName": {"carrier": "USPS"},
                             "trackingURL": "https://www.walmart.com/tracking?tracking_id=9400",
                             "trackingNumber": "9400", "shipDateTime": 1754305000000}}},
        "fulfillment": {"pickUpDateTime": 1754400000000},
        "charges": {"charge": [
            {"chargeType": "PRODUCT", "chargeAmount": {"amount": 19.99}},
            {"chargeType": "SHIPPING", "chargeAmount": {"amount": 5.0}}]},
        "refunds": {"refundLines": {"refundLine": {
            "refundComment": "broken",
            "refundCharges": {"refundCharge": {
                "charge": {"chargeAmount": {"amount": -19.99}}}}}}},
    }]},
}


def test_extract_order_lines_full_parse():
    rows = ol.extract_order_lines("T1", _ORDER)
    assert len(rows) == 1
    r = rows[0]
    assert r["order_line_id"] == ol.make_order_line_id("108000000001", "B0AAAA1111")
    assert r["line_number"] == "1"             # 行号仍存列做展示
    assert r["qty"] == 2 and r["sale_status"] == "Shipped"
    assert r["product_amount"] == 19.99 and r["shipping_amount"] == 5.0
    assert r["refund_amount"] == -19.99 and r["refund_comments"] == "broken"
    assert r["carrier"] == "USPS"
    assert r["tracking_url"] == "https://www.walmart.com/tracking?tracking_id=9400"  # 官方优先
    assert r["status_date"] is not None      # 实证:statusDate 在 orderLine 层
    assert ol.tracking_url("USPS", "9400").startswith("https://tools.usps.com")  # 自拼回退仍在
    # 已发货订单无 estimated*:est_ship 回退 shipDateTime,est_delivery 回退 pickUpDateTime
    assert r["est_ship_date"] is not None and r["est_delivery_date"] is not None
    assert r["postal_code"] == "75001"


# ── 源2:售后展开(新结构 + 老格式兼容)───────────────────────────────────────

def test_flatten_return_lines_new_structure():
    order = {
        "returnOrderId": "RMA1", "customerOrderId": "200000001",
        "customerName": {"firstName": "Jo", "lastName": "Doe"},
        "refundMode": "POST_DELIVERY",
        "refundValue": {"totalAmount": 25.5},
        "returnCreationDate": "2026-08-01T00:00:00Z",
        "returnLineGroups": [{"labels": [{"carrierInfoList": [
            {"carrierName": "FedEx", "trackingNo": "777"}]}]}],
        "returnOrderLines": [{
            "purchaseOrderId": "108000000001", "purchaseOrderLineNumber": 1,
            "item": {"sku": "B0AAAA1111"}, "status": "INITIATED",
            "currentRefundStatus": "NOT_REFUNDED", "returnMethod": "KEEP_ITEM",
            "isKeepIt": True, "returnReason": "damaged",
            "quantity": {"amount": 1}}],
    }
    rows = ol.flatten_return_lines("T1", order)
    r = rows[0]
    # 身份用 item.sku(售后行必有),与销售行同 (PO,SKU) → 同一 order_line_id
    assert r["order_line_id"] == ol.make_order_line_id("108000000001", "B0AAAA1111")
    assert r["line_number"] == "1"
    assert r["return_status"] == "INITIATED" and r["is_keep_it"] is True
    assert r["carrier"] == "FedEx" and r["tracking_no"] == "777"
    assert r["refund_total"] == 25.5 and r["customer_name"] == "Jo Doe"


def test_flatten_return_lines_legacy_structure():
    order = {"returnOrderId": "RMA2",
             "returnLines": {"returnLine": [{
                 "purchaseOrderId": "PO9", "purchaseOrderLineNumber": "2",
                 "item": {"sku": "X"}, "status": "CLOSED"}]}}
    rows = ol.flatten_return_lines("T1", order)
    assert rows[0]["return_status"] == "CLOSED"
    assert rows[0]["line_number"] == "2"


# ── api/returns 翻页实证语义 ──────────────────────────────────────────────────

def test_iter_returns_paginates_by_url_appended_cursor(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "offset=200" in str(request.url):
            return httpx.Response(200, json={
                "returnOrders": [{"returnOrderId": "R2"}], "meta": {}})
        # 首页必须同时带成对时间参数
        assert "returnCreationStartDate" in str(request.url)
        assert "returnCreationEndDate" in str(request.url)
        return httpx.Response(200, json={
            "returnOrders": [{"returnOrderId": "R1"}],
            "meta": {"nextCursor": "?limit=200&offset=200"}})

    _use(monkeypatch, handler)
    got = list(returns_api.iter_returns(STORE, created_start="2026-08-01T00:00:00Z"))
    assert [g["returnOrderId"] for g in got] == ["R1", "R2"]
    assert calls[1].endswith("/v3/returns?limit=200&offset=200")   # cursor 整段拼 URL


def test_iter_returns_stops_on_duplicate_cursor_and_404(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "returnOrders": [{"returnOrderId": "R"}],
            "meta": {"nextCursor": "?offset=0"}})   # 永远同一个 cursor

    _use(monkeypatch, handler)
    got = list(returns_api.iter_returns(STORE, created_start="2026-08-01T00:00:00Z"))
    assert len(got) == 2    # 首页 + 拼接页各一条,第三次因 cursor 重复终止

    _client._close_all_clients()
    _use(monkeypatch, lambda r: httpx.Response(404, json={}))
    assert list(returns_api.iter_returns(STORE, created_start="2026-08-01T00:00:00Z")) == []


# ── recon CSV 主路径 ─────────────────────────────────────────────────────────

def _zip_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("recon.csv", buf.getvalue())
    return zbuf.getvalue()


def test_iter_recon_records_reads_zip_csv(monkeypatch):
    body = _zip_csv([
        {"Transaction Type": "PaymentSummary", "Total Payable": "123.45",
         "Purchase Order #": "", "Purchase Order line #": "", "Amount": "",
         "Amount Type": ""},
        {"Transaction Type": "Sale", "Total Payable": "",
         "Purchase Order #": "PO1", "Purchase Order line #": "1",
         "Amount": "94.43", "Amount Type": "Product Price"}])

    def handler(request):
        assert request.headers["accept"] == "application/octet-stream"
        assert request.url.params["reportVersion"] == "v1"
        return httpx.Response(200, content=body)

    _use(monkeypatch, handler)
    rows = list(reports.iter_recon_records(STORE, "07142026"))
    assert rows[0]["Transaction Type"] == "PaymentSummary"
    assert rows[1]["Purchase Order #"] == "PO1"


def test_iter_recon_records_rejects_non_zip(monkeypatch):
    _use(monkeypatch, lambda r: httpx.Response(200, content=b'{"error":"x"}'))
    with pytest.raises(RuntimeError, match="ZIP"):
        list(reports.iter_recon_records(STORE, "07142026"))


# ── 源3:对账聚合(汇总行/浮点相消/入账状态)──────────────────────────────────

def test_aggregate_settlement_skips_summary_and_rounds():
    rows = [
        {"Purchase Order #": "", "Purchase Order line #": "",       # 汇总行必须跳过
         "Amount": "9999", "Amount Type": ""},
        {"Purchase Order #": "PO1", "Purchase Order line #": "1",
         "Partner Item Id": "B0X1",                                 # CSV 自带 SKU 列
         "Amount": "52.68", "Amount Type": "Product Price"},
        {"Purchase Order #": "PO1", "Purchase Order line #": "1",   # 全额退款相消
         "Amount": "-52.68", "Amount Type": "Product Price"},
        {"Purchase Order #": "PO1", "Purchase Order line #": "1",
         "Amount": "-7.9", "Amount Type": "Commission on Product",
         "Commission Rate": "15.0"},
        {"Purchase Order #": "PO1", "Purchase Order line #": "1",
         "Amount": "7.9", "Amount Type": "Commission on Product"},
    ]
    recs, skipped = ol.aggregate_settlement_lines("T1", rows, "07142026")
    assert len(recs) == 1 and skipped == 0
    r = recs[0]
    assert r["order_line_id"] == ol.make_order_line_id("PO1", "B0X1")  # SKU 建键
    assert r["net_amount"] == 0.0          # round6 吸掉 4.44e-16 类浮点残渣
    assert r["gross_amount"] > 0
    assert r["commission_rate"] == 15.0
    assert ol.settle_status(r["net_amount"], r["gross_amount"]) == "已退款"
    assert ol.settle_status(10, 10) == "已入账"
    assert ol.settle_status(-1, 1) == "已冲销"
    assert ol.settle_status(0, 0) == "待入账"
    assert str(r["settle_date"]) == "2026-07-14"


def test_aggregate_settlement_sku_lookup_fallback_and_skip():
    # CSV 无 SKU 列:① sku_lookup 按 (po, 行号) 反查命中 → 建键;② 反查也没有 → 跳过计数
    rows = [
        {"Purchase Order #": "PO1", "Purchase Order line #": "1",
         "Amount": "10", "Amount Type": "Product Price"},
        {"Purchase Order #": "PO2", "Purchase Order line #": "3",
         "Amount": "20", "Amount Type": "Product Price"},
    ]
    recs, skipped = ol.aggregate_settlement_lines(
        "T1", rows, "07142026", sku_lookup={("PO1", "1"): "B0Y2"})
    assert len(recs) == 1 and skipped == 1
    assert recs[0]["order_line_id"] == ol.make_order_line_id("PO1", "B0Y2")
    assert recs[0]["line_number"] == "1"


# ── upsert SQL 形态 ──────────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, reg=()):
        self.calls, self.rowcount = [], 3
        self.reg = list(reg)

    def executemany(self, sql, rows):
        self.calls.append((sql, list(rows)))

    def execute(self, sql, args=None):
        self.calls.append((sql, args))

    def fetchall(self):
        # 批次 0b:upsert_order_lines 落库前先查一次登记簿(_fill_asins)。
        # 没有这个方法的话新增的那条 SELECT 会当场 AttributeError
        return list(self.reg)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, reg=()):
        self.cur = _FakeCursor(reg)

    def cursor(self):
        return self.cur

    def _many(self):
        """输入:无 → 输出:第一条 executemany 的 (sql, rows)(跳过登记簿 SELECT)。"""
        return next(c for c in self.cur.calls if isinstance(c[1], list))


def test_upserts_build_conflict_sql():
    conn = _FakeConn()
    n = ol.upsert_order_lines(conn, ol.extract_order_lines("T1", _ORDER))
    assert n == 1
    sql, rows = conn._many()
    assert "ON CONFLICT (order_line_id)" in sql and "updated_at = now()" in sql
    assert rows[0]["po_id"] == "108000000001"

    conn2 = _FakeConn()
    ol.upsert_perf_events(conn2, [{"store": "T1", "po_id": "PO1",
                                   "metric": "otd", "period": "2026-08-01",
                                   "detail": {"k": "v"}}])
    sql2, rows2 = conn2.cur.calls[0]
    assert "ON CONFLICT (po_id, metric, period)" in sql2
    assert "first_seen_at" not in sql2.split("DO UPDATE")[1]   # 首见时间不被覆盖
    assert isinstance(rows2[0]["detail"], str)                 # dict 已序列化


def test_extract_order_lines_no_longer_computes_asin():
    """asin 不在纯函数里算 —— 它要查登记簿(要连接),统一由唯一写入口
    upsert_order_lines 落库当场补。留一份 extract 时点的算法 = 两条实现路径。"""
    rows = ol.extract_order_lines("T1", _ORDER)
    assert rows and "asin" not in rows[0]


def test_upsert_fills_asin_from_the_registry():
    """登记簿有 amz 行 ⇒ 键取 source_key(切码后这是唯一通路:不透明码对
    形态提取恒返 None,不接这一跳 order_lines.asin 就恒 NULL,产品分的
    销量/退货率维度整个退出,而且不报错)。"""
    rows = ol.extract_order_lines("T1", _ORDER)
    sku = rows[0]["sku"]
    conn = _FakeConn(reg=[("T1", sku, "B0REGISTER")])
    ol.upsert_order_lines(conn, rows)
    _, written = conn._many()
    assert written[0]["asin"] == "B0REGISTER"


def test_upsert_leaves_asin_null_when_unresolvable():
    """两条腿都提不出就留 None,**绝不写 sku 原文**:订单链的口径是
    「提不出留 NULL」(所有者 2026-08-15),原文兜底在采集库永远查空,
    看起来像"这个产品一单没卖过"——一个静默的错误信号。"""
    conn = _FakeConn()
    rows = [{"order_line_id": "L1", "store": "T1", "sku": "102460018738"}]
    ol.upsert_order_lines(conn, rows)
    _, written = conn._many()
    assert written[0]["asin"] is None


def test_upsert_asin_guard_still_coalesces():
    """纯数字 item_id 形态是本批次唯一没被登记簿补掉的口子(那一跳要查
    walmart_items),由 order_asin_normalize 扫尾 —— 这条守卫**不能拆**,
    拆了每轮同步都会把扫尾填好的值冲回 NULL。"""
    assert ol._ASIN_GUARD == "COALESCE(EXCLUDED.asin, t.asin)"
    conn = _FakeConn()
    ol.upsert_order_lines(conn, ol.extract_order_lines("T1", _ORDER))
    sql, _ = conn._many()
    assert "asin = COALESCE(EXCLUDED.asin, t.asin)" in sql


def test_fill_asins_is_one_query_per_batch():
    """一批一条 SELECT(order_sync 是每店一批):退化成逐行往返的话,
    cleanup/同步这类万级批量会把连接打满,而功能看起来完全正常。"""
    conn = _FakeConn()
    rows = [{"order_line_id": f"L{i}", "store": "T1", "sku": f"B0ABCDEF{i:02d}"}
            for i in range(50)]
    assert ol._fill_asins(conn, rows) == 50
    assert len(conn.cur.calls) == 1
    assert ol._fill_asins(conn, []) == 0        # 空批不开游标
    assert len(conn.cur.calls) == 1


def test_upsert_skips_write_when_nothing_changed():
    """内容没变就整行不写——updated_at 才能表示"这行什么时候变的"。

    原先是无条件 `DO UPDATE SET ..., updated_at = now()`,而 order_sync 每轮
    全量重拉 45 天窗口 ⇒ 窗口内每行的 updated_at 都被刷新 ⇒ order_center_push
    的指纹(含「拉取时间」)必变 ⇒ 每轮把窗口内全部行重推飞书。
    2026-08-10 生产实证:7100 行更新 3122,正是 45 天窗口的行数。
    """
    conn = _FakeConn()
    ol.upsert_order_lines(conn, ol.extract_order_lines("T1", _ORDER))
    sql, _ = conn._many()
    body = sql.split("DO UPDATE")[1]
    assert "IS DISTINCT FROM" in body, "没有变更检测 = 每轮全窗口重写"
    # 变更检测比的是**生效后的值**,不是 EXCLUDED——否则被 guard 挡下的
    # 假变化照样会让整行重写、updated_at 空跳
    where = body.split("WHERE")[1]
    assert "CASE WHEN" in where and "t.phone" in where


def test_upsert_phone_all_zero_never_overwrites_real_number():
    """沃尔玛常态把电话打码成全 0(实证 84%),原样覆盖会把真电话冲掉且找不回。

    旧系统的「电话全 0 保护」,legacy_survey 明列必须照搬。
    反向不设防:真电话覆盖全 0 是正常修复。
    """
    conn = _FakeConn()
    ol.upsert_order_lines(conn, ol.extract_order_lines("T1", _ORDER))
    sql, _ = conn._many()
    guard = [seg for seg in sql.split(",") if "phone =" in seg]
    assert guard, "phone 列必须走保护表达式而不是裸 EXCLUDED"
    assert "^0*$" in sql and "THEN t.phone" in sql
    # 其余列不受影响,仍是直接覆盖
    assert "ship_name = EXCLUDED.ship_name" in sql


def test_backfill_perf_line_ids_two_stage_sku_first():
    conn = _FakeConn()
    total = ol.backfill_perf_line_ids(conn)
    assert total == 6                       # FakeCursor rowcount=3 × 两段
    sku_sql = conn.cur.calls[0][0]
    fallback_sql = conn.cur.calls[1][0]
    # 第一段:按 (po_id, sku) 等值连接(v3 下该组合在 order_lines 唯一,无需 HAVING),
    # 店铺不参与(PO 全局唯一)
    assert "p.sku = l.sku" in sku_sql and "HAVING" not in sku_sql
    assert "p.store" not in sku_sql and "p.store" not in fallback_sql
    # 第二段:无 SKU 事件退"该 PO 仅一行"规则;两段都只回填 NULL 行
    assert "HAVING count(*) = 1" in fallback_sql
    assert "p.sku" not in fallback_sql.split("WHERE")[1]
    assert all("order_line_id IS NULL" in s for s in (sku_sql, fallback_sql))


def test_extract_warns_on_duplicate_sku_in_one_po(caplog):
    # 身份前提"同 PO 同 SKU 必合并"被打破时必须告警(兜底不许静默)
    import logging as _logging
    order = {"purchaseOrderId": "PO7", "orderLines": {"orderLine": [
        {"lineNumber": "1", "item": {"sku": "S"}, "orderLineQuantity": {"amount": "1"}},
        {"lineNumber": "2", "item": {"sku": "S"}, "orderLineQuantity": {"amount": "1"}},
    ]}}
    with caplog.at_level(_logging.WARNING, logger="services.order_lines"):
        rows = ol.extract_order_lines("T1", order)
    assert rows[0]["order_line_id"] == rows[1]["order_line_id"]
    assert any("身份撞键" in m for m in caplog.messages)


def test_perf_upsert_skips_write_when_nothing_changed():
    """绩效事件同款:内容没变就别刷 last_seen_at,否则每轮把窗口内全部行重推飞书。

    与 test_upsert_skips_write_when_nothing_changed 是**同一个病**,但当时只修了
    销售那一半。2026-08-17 所有者实见:`飞书投影:绩效订单:1634 行(全量),
    新建 0 更新 1093 跳过 541` —— 1093 正是 still_active 的那批,业务字段一个都
    没变,只因为绩效报表是滚动的、同一批单每轮再出现一次,
    `last_seen_at = now()` 就被无条件刷新,而它作为「拉取时间」参与飞书指纹。

    自我循环:因为我们跑了时间戳才变,因为时间戳变了才推。
    """
    conn = _FakeConn()
    ol.upsert_perf_events(conn, [{"store": "T1", "po_id": "PO1",
                                  "metric": "otd", "period": "2026-08-01",
                                  "detail": {"k": "v"}}])
    body = conn.cur.calls[0][0].split("DO UPDATE")[1]
    assert "IS DISTINCT FROM" in body, "没有变更检测 = 每轮全窗口刷时间戳"
    where = body.split("WHERE")[1]
    # ⚠ 比的必须是**生效后的值**(带 COALESCE),不是 EXCLUDED:
    # order_line_id/sku 的 COALESCE 会把"新值为 NULL"挡成不变,拿 EXCLUDED 比
    # 则把这种假变化当真变化,整行照写、时间戳照跳,等于闸没装
    assert "COALESCE(EXCLUDED.order_line_id" in where
    assert "COALESCE(EXCLUDED.sku" in where
    # 裸 EXCLUDED.order_line_id / EXCLUDED.sku 不许出现在比较里
    import re
    assert not re.search(r"(?<!COALESCE\()EXCLUDED\.order_line_id", where)
    assert not re.search(r"(?<!COALESCE\()EXCLUDED\.sku", where)


def test_perf_last_seen_at_has_no_judgement_reader():
    """`last_seen_at` 少刷新不影响任何结论 —— still_active 看的是 period。

    这条钉的是**改动的前提**:哪天有人拿 last_seen_at 当判据(比如"多久没见到
    就算滚出窗口"),上面那道闸就会静默改变它的语义。届时本用例转红。
    """
    import pathlib
    ddl = pathlib.Path("refdata/schema.sql").read_text(encoding="utf-8")
    span = ddl.split("CREATE OR REPLACE VIEW orders.perf_event_spans")[1]
    span = span.split(";")[0]
    assert "still_active" in span
    assert "max(e.period) = m.latest_period" in span
    assert "last_seen_at" not in span, "still_active 开始依赖 last_seen_at 了"


# ── 订单双算体检(SKU 改造批次 3 地基,X1;判据在 orders.v_order_line_dupes)──

class _ViewConn:
    """只回放视图行的假连接(体检函数是纯只读薄壳)。"""

    def __init__(self, rows):
        self.rows, self.sqls = rows, []

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_duplicate_po_lines_flags_two_skus_under_one_po_line():
    """同 (store, po_id, line_number) 出现两个 order_line_id = 同一笔销售算两次。

    order_line_id = sha256(PO + SKU):改码后若沃尔玛对改码之前的 PO 返回新码,
    那一行会被当成新行插入而旧行不删 —— 销量、产品分、日报、对账全受影响,
    **而且不报错**。官方对"改码后旧 PO 返回哪个码"一个字都没有,只能靠体检。
    """
    conn = _ViewConn([("T1", "PO1", "1", 2, ["AN3WC0DE2345", "B0ABCDEFGH"])])
    got = ol.duplicate_po_lines(conn)
    assert got == [{"store": "T1", "po_id": "PO1", "line_number": "1", "n": 2,
                    "skus": ["AN3WC0DE2345", "B0ABCDEFGH"]}]
    assert conn.sqls[0][1] == {"days": 120}          # 默认回看窗口
    assert ol.duplicate_po_lines(_ViewConn([]), days=None)[:] == []
    assert conn.sqls[0][1] != {"days": None}


def test_duplicate_po_lines_is_empty_on_healthy_data():
    """健康数据下必须是空列表:这是改码前要存档的基线,改码后必须一致。"""
    assert ol.duplicate_po_lines(_ViewConn([])) == []


def test_duplicate_po_lines_reads_the_view_not_a_local_group_by():
    """判据只有一处出生(refdata/schema.sql 的 orders.v_order_line_dupes)。

    在这里重写一遍 GROUP BY/HAVING 就是同一条体检两份实现、口径还可能不同 ——
    两边数字对不上时没有判据说该信哪个,而这条体检正是发现"改码后销量双算"的
    唯一手段。窗口(days)是消费方的事,判据不是。
    """
    import pathlib
    conn = _ViewConn([])
    ol.duplicate_po_lines(conn)
    sql = conn.sqls[0][0]
    assert "orders.v_order_line_dupes" in sql
    assert "GROUP BY" not in sql.upper() and "HAVING" not in sql.upper()
    ddl = pathlib.Path("refdata/schema.sql").read_text(encoding="utf-8")
    view = ddl.split("CREATE OR REPLACE VIEW orders.v_order_line_dupes")[1]
    view = view.split(";")[0]
    assert "count(DISTINCT order_line_id)" in view      # 不是 count(*)
    assert "HAVING count(DISTINCT order_line_id) > 1" in view
    assert "orders.order_lines" not in sql              # 薄壳只读视图,不碰明细表
    assert "first_order_date" in sql                    # 窗口用视图给的那一列
