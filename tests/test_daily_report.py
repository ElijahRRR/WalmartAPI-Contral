"""daily_report 链条回归:业务规则(照搬清单)、insights 204、orders 分页模型 2、xlsx 解析。"""

import io
import time
from datetime import datetime, timezone

import httpx
import openpyxl
import pytest

from api import _client, insights, orders as orders_api
from services import kpi

STORE = {"name": "T1", "client_id": "cid_dr", "client_secret": "sec", "proxy": None}


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


# ── 业务规则(照搬清单) ────────────────────────────────────────────────────────

def test_sales_window_anchored_at_cn_0630():
    # 中国时间 08:00(UTC 00:00)跑 → 窗口 = 昨天 06:30 ~ 今天 06:30(中国时间)
    start, end = kpi.sales_window_utc(datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc))
    assert end == "2026-08-04T22:30:00Z"      # 今天 06:30 CN = 前一日 22:30 UTC
    assert start == "2026-08-03T22:30:00Z"
    # 中国时间 05:00(未过锚点)→ 锚点退回昨天 06:30
    start2, end2 = kpi.sales_window_utc(datetime(2026, 8, 4, 21, 0, tzinfo=timezone.utc))
    assert end2 == "2026-08-03T22:30:00Z"


def test_settlement_rules_non_active_payout_zero():
    st = {"paymentStatus": "HOLD", "storeFrontUrl": "https://www.walmart.com/seller/123456",
          "partnerId": "P1", "sellerInfo": {"sellerStatus": "ACTIVE"},
          "accountSummary": {"closingBalance": 500.0, "reserveToDate": -30.5,
                             "scheduledSettlementDate": "2026-08-19"},
          "transactionDetails": {"saleAggregate": {"productPrice": 800, "netComm": 80}},
          "refundDetails": {"productPrice": 20}}
    out = kpi.extract_settlement(st)
    assert out["payout"] == 0.0               # 非 ACTIVE 强制 0
    assert out["no_hold"] is False
    assert out["seller_id"] == "123456"       # storeFrontUrl 正则提取
    assert out["reserve_to_date"] == 30.5     # 取绝对值
    assert out["period_sales"] == 800 and out["commission"] == 80


def test_settlement_active_no_hold_flag():
    st = {"paymentStatus": "ACTIVE", "storeFrontUrl": "",
          "accountSummary": {"closingBalance": 100.0, "reserve": 0,
                             "scheduledSettlementDate": ""}}
    out = kpi.extract_settlement(st)
    assert out["payout"] == 100.0 and out["no_hold"] is True

    st2 = {"paymentStatus": "ACTIVE", "storeFrontUrl": "",
           "accountSummary": {"closingBalance": 100.0, "reserve": 40,
                              "scheduledSettlementDate": ""}}
    out2 = kpi.extract_settlement(st2)
    assert out2["payout"] == 60.0 and out2["no_hold"] is False


def test_prev_recon_strict_minus_14_days():
    assert kpi.prev_recon_date("2026-08-19") == "08052026"        # MMDDYYYY
    assert kpi.prev_recon_date("08/19/2026") == "08052026"
    ms = str(int(datetime(2026, 8, 19, tzinfo=timezone.utc).timestamp() * 1000))
    assert kpi.prev_recon_date(ms) == "08052026"
    assert kpi.prev_recon_date("") is None


def test_payment_summary_total_negative_to_zero():
    recs = [{"Transaction Type": "Sale", "Total Payable": 10},
            {"Transaction Type": "PaymentSummary", "Total Payable": -55.2}]
    assert kpi.payment_summary_total(iter(recs)) == 0.0
    recs2 = [{"Transaction Type": "PaymentSummary", "Total Payable": "321.5"}]
    assert kpi.payment_summary_total(iter(recs2)) == 321.5
    assert kpi.payment_summary_total(iter([])) == 0.0


def _make_xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title=title)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_problem_report_rules():
    blob = _make_xlsx({
        "Late Delivery": [
            ["Data current as of 2026-08-05", "", "", ""],
            ["Sales Order Number", "PO #", "Carrier", "Tracking Number"],
            ["108xxx1", "PO1", "USPS", "TRK1"],
            ["=SUM(A1:A2)", "", "", ""],                  # 公式行跳过
            ["", "", "", ""],                              # 空行跳过
        ],
        "Not Accountable": [
            ["Sales Order Number", "Item Name"],
            ["108xxx2", "Great Cup$$Kitchen"],             # $$ 切前半
        ],
    })
    rows = kpi.parse_problem_report("otd", blob)
    assert len(rows) == 2
    r1 = next(r for r in rows if r["sales_order_no"] == "108xxx1")
    assert r1["indicator"] == "🚚 OTD"                     # emoji 契约
    assert r1["sub_category"] == "Late Delivery"
    assert r1["accountable"] == "✅ 是"
    assert r1["carrier"] == "USPS" and r1["tracking_no"] == "TRK1"
    r2 = next(r for r in rows if r["sales_order_no"] == "108xxx2")
    assert r2["accountable"] == "⚪ 否" and r2["sub_category"] == ""
    assert r2["item"] == "Great Cup"                       # $$ 只取前半


# ── api 层 ────────────────────────────────────────────────────────────────────

def test_insights_summary_204_is_none_and_rate_priority(monkeypatch):
    def handler(request):
        if "otd" in request.url.path:
            return httpx.Response(204)
        return httpx.Response(200, json={"payload": {
            "overallRate": 9.9, "sellerAccountableRate": "1.234"}})

    _use(monkeypatch, handler)
    assert insights.performance_summary(STORE, "otd") is None       # 204=无数据
    assert insights.performance_summary(STORE, "vtr") == 1.23       # 优先 sellerAccountable


def test_orders_pagination_model2_cursor_suffix(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "nextCursor" not in str(request.url):
            return httpx.Response(200, json={"list": {
                "meta": {"totalCount": 3, "nextCursor": "?nextCursor=ABC&limit=200"},
                "elements": {"order": [
                    {"orderLines": {"orderLine": [{"charges": {"charge": [
                        {"chargeType": "PRODUCT", "chargeAmount": {"amount": 10.5}},
                        {"chargeType": "SHIPPING", "chargeAmount": {"amount": 99}}]}}]}},
                ]}}})
        return httpx.Response(200, json={"list": {
            "meta": {"totalCount": 3, "nextCursor": None},
            "elements": {"order": [{"orderLines": {"orderLine": []}}]}}})

    _use(monkeypatch, handler)
    stats = {}
    got = list(orders_api.iter_orders(STORE, created_start="2026-08-04T22:30:00Z",
                                      stats=stats))
    assert stats["total"] == 3
    assert len(got) == 2
    assert orders_api.order_product_sales(got[0]) == 10.5   # 只累加 PRODUCT 类费用
    assert "nextCursor=ABC" in calls[1]                     # cursor 即 query 串直接拼


def test_orders_404_is_empty_window(monkeypatch):
    _use(monkeypatch, lambda r: httpx.Response(404, json={}))
    stats = {}
    assert list(orders_api.iter_orders(STORE, created_start="x", stats=stats)) == []
    assert stats["total"] == 0


def test_settlement_moved_out_tells_you_where(monkeypatch):
    """对账摘出后,老命令不能静默变成"什么也没做"——要指路。

    daily_report -p phase=settlement 曾经是有效用法,拆走后若只返回"phase 只
    接受 ...",挂着旧调度的人会以为参数写错,而不是知道它搬家了(problems
    2026-08-08 摘出时就是这么处理的,这里照同一口径)。
    """
    from workflows import daily_report as dr
    out = dr.run({"phase": "settlement"})
    assert "settlement_sync" in out

    from workflows import settlement_sync as ss
    assert hasattr(ss, "run") and ss.DANGEROUS is False
