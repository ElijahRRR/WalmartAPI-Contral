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


# ── 行标识 v2:(PO, 行号),店铺不参与身份 ─────────────────────────────────────

def test_order_line_id_v2_semantics():
    # v2 定稿:身份 = (PO, 行号),店铺不参与——店铺是我方标签,改名不得作废行标识
    def reference(po, line):
        raw = f"{po}\x1f{line}"
        return "ol_" + hashlib.sha256(raw.encode()).hexdigest()[:24]

    assert ol.make_order_line_id("108888888888", 1) == reference("108888888888", "1")
    # int/str/float 形态归一到同一 ID(orders 给 int,returns 给 str,CSV 给 '1')
    assert (ol.make_order_line_id("PO1", 2)
            == ol.make_order_line_id("PO1", "2")
            == ol.make_order_line_id("PO1", "2.0"))
    assert ol.make_order_line_id("PO1", 1) != ol.make_order_line_id("PO1", 2)


# ── 源1:订单展开 ─────────────────────────────────────────────────────────────

_ORDER = {
    "purchaseOrderId": "108000000001", "customerOrderId": "200000001",
    "orderDate": 1754300000000,
    "shippingInfo": {"phone": "555", "postalAddress": {
        "name": "N", "address1": "A1", "city": "C", "state": "TX",
        "postalCode": "75001", "country": "USA"}},
    "orderLines": {"orderLine": [{
        "lineNumber": "1",
        "item": {"sku": "B0AAAA1111", "productName": "P"},
        "orderLineQuantity": {"amount": "2"},
        "orderLineStatuses": {"orderLineStatus": {
            "status": "Shipped", "statusSetDate": 1754310000000,
            "trackingInfo": {"carrierName": {"carrier": "USPS"},
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
    assert r["order_line_id"] == ol.make_order_line_id("108000000001", "1")
    assert r["qty"] == 2 and r["sale_status"] == "Shipped"
    assert r["product_amount"] == 19.99 and r["shipping_amount"] == 5.0
    assert r["refund_amount"] == -19.99 and r["refund_comments"] == "broken"
    assert r["carrier"] == "USPS" and "usps.com" in r["tracking_url"]
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
    assert r["order_line_id"] == ol.make_order_line_id("108000000001", "1")
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
         "Amount": "52.68", "Amount Type": "Product Price"},
        {"Purchase Order #": "PO1", "Purchase Order line #": "1",   # 全额退款相消
         "Amount": "-52.68", "Amount Type": "Product Price"},
        {"Purchase Order #": "PO1", "Purchase Order line #": "1",
         "Amount": "-7.9", "Amount Type": "Commission on Product",
         "Commission Rate": "15.0"},
        {"Purchase Order #": "PO1", "Purchase Order line #": "1",
         "Amount": "7.9", "Amount Type": "Commission on Product"},
    ]
    recs = ol.aggregate_settlement_lines("T1", rows, "07142026")
    assert len(recs) == 1
    r = recs[0]
    assert r["net_amount"] == 0.0          # round6 吸掉 4.44e-16 类浮点残渣
    assert r["gross_amount"] > 0
    assert r["commission_rate"] == 15.0
    assert ol.settle_status(r["net_amount"], r["gross_amount"]) == "已退款"
    assert ol.settle_status(10, 10) == "已入账"
    assert ol.settle_status(-1, 1) == "已冲销"
    assert ol.settle_status(0, 0) == "待入账"
    assert str(r["settle_date"]) == "2026-07-14"


# ── upsert SQL 形态 ──────────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self):
        self.calls, self.rowcount = [], 3

    def executemany(self, sql, rows):
        self.calls.append((sql, list(rows)))

    def execute(self, sql, args=None):
        self.calls.append((sql, args))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self):
        self.cur = _FakeCursor()

    def cursor(self):
        return self.cur


def test_upserts_build_conflict_sql():
    conn = _FakeConn()
    n = ol.upsert_order_lines(conn, ol.extract_order_lines("T1", _ORDER))
    assert n == 1
    sql, rows = conn.cur.calls[0]
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


def test_backfill_perf_line_ids_two_stage_sku_first():
    conn = _FakeConn()
    total = ol.backfill_perf_line_ids(conn)
    assert total == 6                       # FakeCursor rowcount=3 × 两段
    sku_sql = conn.cur.calls[0][0]
    fallback_sql = conn.cur.calls[1][0]
    # 第一段:按 (po_id, sku) 无歧义匹配,店铺不参与(PO 全局唯一)
    assert "p.sku = l.sku" in sku_sql and "HAVING count(*) = 1" in sku_sql
    assert "p.store" not in sku_sql and "p.store" not in fallback_sql
    # 第二段:无 SKU 事件退"该 PO 仅一行"规则;两段都只回填 NULL 行
    assert "p.sku" not in fallback_sql.split("WHERE")[1]
    assert all("order_line_id IS NULL" in s for s in (sku_sql, fallback_sql))
