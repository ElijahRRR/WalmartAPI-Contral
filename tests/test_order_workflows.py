"""订单域工作流回归:order_sync / returns_sync 全链路(mock 沃尔玛 + 假 PG)、
绩效事件构建、账期挑选。"""

import contextlib
import time

import httpx
import pytest

from api import _client
from services import order_lines as ol

STORE = {"name": "T1", "client_id": "cid_wf", "client_secret": "sec", "proxy": None}


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


class _FakeCursor:
    def __init__(self, store):
        self.store = store

    def executemany(self, sql, rows):
        self.store.append(("many", sql, list(rows)))

    def execute(self, sql, args=None):
        self.store.append(("one", sql, args))

    def fetchall(self):
        return []

    rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCursor(self.store)

    def execute(self, sql, args=None):
        self.store.append(("one", sql, args))


def _fake_pg(monkeypatch, calls: list):
    from registry import db

    @contextlib.contextmanager
    def fake_conn():
        yield _FakeConn(calls)
    monkeypatch.setattr(db, "pg_conn", fake_conn)


def _fake_stores(monkeypatch, module):
    monkeypatch.setattr(module.stores_svc, "load_stores", lambda names=None: [STORE])


# ── order_sync 全链路 ────────────────────────────────────────────────────────

def test_order_sync_end_to_end(monkeypatch):
    from workflows import order_sync

    def handler(request):
        assert request.url.path == "/v3/orders"
        assert "createdStartDate" in str(request.url)
        return httpx.Response(200, json={"list": {
            "elements": {"order": [{
                "purchaseOrderId": "PO1", "orderDate": 1754300000000,
                "orderLines": {"orderLine": [
                    {"lineNumber": "1", "item": {"sku": "A"},
                     "orderLineQuantity": {"amount": "1"}},
                    {"lineNumber": "2", "item": {"sku": "B"},
                     "orderLineQuantity": {"amount": "1"}}]}}]},
            "meta": {"totalCount": 1}}})

    _use(monkeypatch, handler)
    calls: list = []
    _fake_pg(monkeypatch, calls)
    _fake_stores(monkeypatch, order_sync)

    out = order_sync.run({"days": "7"})
    assert "1/1 店完成" in out and "订单行入库 2" in out
    kind, sql, rows = calls[0]
    assert kind == "many" and "orders.order_lines" in sql
    assert {r["sku"] for r in rows} == {"A", "B"}
    # 审核列绝不在 upsert 列内(重拉不得冲掉审核结论)
    assert "audit_status" not in sql


def test_returns_sync_end_to_end(monkeypatch):
    from workflows import returns_sync

    def handler(request):
        return httpx.Response(200, json={"returnOrders": [{
            "returnOrderId": "RMA1",
            "returnOrderLines": [{"purchaseOrderId": "PO1",
                                  "purchaseOrderLineNumber": 1,
                                  "item": {"sku": "A"}, "status": "INITIATED"}]}],
            "meta": {}})

    _use(monkeypatch, handler)
    calls: list = []
    _fake_pg(monkeypatch, calls)
    _fake_stores(monkeypatch, returns_sync)

    out = returns_sync.run({})
    assert "1/1 店完成" in out and "售后行入库 1" in out
    kind, sql, rows = calls[0]
    assert "orders.return_lines" in sql
    assert rows[0]["return_status"] == "INITIATED"


# ── 绩效事件构建与账期挑选 ────────────────────────────────────────────────────

def test_perf_rows_from_problems():
    rows = [
        {"po_no": "PO1", "sku": "B0X", "accountable": "✅ 是", "raw": "{}"},
        {"po_no": "PO1", "sku": "", "accountable": "⚪ 否", "raw": "{}"},
        {"po_no": "", "sales_order_no": "SO9", "accountable": "✅ 是", "raw": "{}"},
    ]
    out, skipped = ol.perf_rows_from_problems("T1", "otd", rows, "2026-08-06")
    assert skipped == 1                       # 无 PO 不建键
    assert len(out) == 2
    assert out[0] == {"store": "T1", "po_id": "PO1", "metric": "otd",
                      "period": "2026-08-06", "sku": "B0X", "accountable": True,
                      # v3:带 SKU 的事件写入时直接建键,订单不在库里也成立
                      "order_line_id": ol.make_order_line_id("PO1", "B0X"),
                      "status": "违规", "detail": "{}"}
    assert out[1]["accountable"] is False and out[1]["status"] == "不计入"
    assert out[1]["sku"] is None and out[1]["order_line_id"] is None


def test_perf_rows_line_no_resolves_sku_via_lookup():
    # returns/INR 版式:带 Order Line # 无 SKU(2026-08-06 实证)→ 反查订单行建键
    rows = [
        {"po_no": "PO5", "sku": "", "line_no": "2.0", "accountable": "✅ 是", "raw": "{}"},
        {"po_no": "PO6", "sku": "", "line_no": "1", "accountable": "✅ 是", "raw": "{}"},
    ]
    out, _ = ol.perf_rows_from_problems("T1", "returns", rows, "2026-08-06",
                                        sku_lookup={("PO5", "2"): "B0Z9"})
    assert out[0]["sku"] == "B0Z9"            # 行号归一后命中反查
    assert out[0]["order_line_id"] == ol.make_order_line_id("PO5", "B0Z9")
    assert out[1]["sku"] is None and out[1]["order_line_id"] is None  # 查不到不硬造


def test_pick_new_periods_sorts_across_years():
    available = ["12212025", "01042026", "07142026", "06302026"]
    todo = ol.pick_new_periods(available, have={"07142026"}, limit=2)
    # 跨年排序按 YYYY+MMDD:12212025 < 01042026 < 06302026;取最近 2 期
    assert todo == ["01042026", "06302026"]
