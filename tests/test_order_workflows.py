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
        self._last = ("", None)

    def executemany(self, sql, rows):
        self.store.append(("many", sql, list(rows)))

    def execute(self, sql, args=None):
        self.store.append(("one", sql, args))
        self._last = (sql, args)

    def fetchall(self):
        sql, args = self._last
        # 烂账过滤的在库查询:假装全部在库(过滤行为有专门单测)
        if "= ANY(" in sql and args:
            return [(x,) for x in args[0]]
        return []

    def fetchone(self):
        return None

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
    # calls[0] 现在是烂账过滤的在库查询;取 upsert 那条
    kind, sql, rows = next(c for c in calls if c[0] == "many")
    assert "orders.return_lines" in sql
    assert rows[0]["return_status"] == "INITIATED"


def test_returns_sync_all_stores_dead_is_not_success(monkeypatch):
    """⚠ 全部店凭证失效时**不许报成功**(2026-08-17 补,与 catalog_sync 同款闸)。

    「凭证失效跳过」按设计不进 failed —— 一家店坏了不该拖垮整轮。但同日
    `api/_client` 把换 token 阶段的 **400** 也归成 StoreDeadError(沃尔玛凭证
    被拒回的是 400,不是 401),于是"请求形状被改坏"这类**全店**故障从
    "整轮报错"变成了"整轮跳过"。没有这道闸,摘要只剩一句"0/N 店完成"而通知
    照报 ✅。本链每小时跑,静默陈旧起来最难发现。
    """
    from workflows import returns_sync

    def handler(request):
        # 换 token 就回 400 —— 生产实见的凭证被拒形态
        return httpx.Response(400, json={"error": "invalid_client"})
    monkeypatch.setattr(_client, "_build_transport",
                        lambda proxy: httpx.MockTransport(handler))
    _fake_pg(monkeypatch, [])
    _fake_stores(monkeypatch, returns_sync)
    with pytest.raises(RuntimeError, match="零店完成"):
        returns_sync.run({})


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


# ── 烂账治理:入库过滤 + 台账 + 一次性清理 ────────────────────────────────────


class _QueryConn:
    """可编程假连接:按 SQL 特征返回预置结果。"""

    def __init__(self, have_ids=(), have_pos=(), fetchone_seq=(), vanish=()):
        self.have_ids, self.have_pos = list(have_ids), list(have_pos)
        self.fetchone_seq = list(fetchone_seq)
        self.vanish = list(vanish)
        self.sqls: list = []

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))
        self._last = sql

    def fetchall(self):
        if "order_line_id = ANY" in self._last:
            return [(x,) for x in self.have_ids]
        if "po_id = ANY" in self._last:
            return [(x,) for x in self.have_pos]
        if "GROUP BY" in self._last:
            return self.vanish
        return []

    def fetchone(self):
        return self.fetchone_seq.pop(0) if self.fetchone_seq else None

    rowcount = 7

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_drop_unlinked_filters_by_order_line_id():
    conn = _QueryConn(have_ids=["ol_in"])
    rows = [{"order_line_id": "ol_in", "x": 1},
            {"order_line_id": "ol_out", "x": 2}]
    kept, dropped = ol.drop_unlinked(conn, rows)
    assert [r["order_line_id"] for r in kept] == ["ol_in"] and dropped == 1


def test_drop_unlinked_perf_filters_by_po_keeps_null_sku_rows():
    conn = _QueryConn(have_pos=["PO_IN"])
    rows = [{"po_id": "PO_IN", "order_line_id": None},   # 无 SKU 但 PO 在库 → 保留
            {"po_id": "PO_OUT", "order_line_id": "ol_x"}]
    kept, dropped = ol.drop_unlinked_perf(conn, rows)
    assert [r["po_id"] for r in kept] == ["PO_IN"] and dropped == 1


def test_recon_done_ledger_roundtrip():
    conn = _QueryConn(fetchone_seq=[(["01012026"],)])
    ol.mark_recon_done(conn, "T1", ["02022026"])
    sql, args = conn.sqls[-1]
    assert "ops.cursors" in sql and "ON CONFLICT (name)" in sql
    assert args[0] == "recon_done:T1"
    assert '"01012026"' in args[1] and '"02022026"' in args[1]  # 旧账期并入
    assert ol.mark_recon_done(conn, "T1", []) is None           # 空集不写


def test_cleanup_dry_run_counts_only(monkeypatch):
    from workflows import order_center_cleanup as occ
    conn = _QueryConn(fetchone_seq=[(3,), (5,), (9,)],
                      vanish=[("T1", "05062025")])
    _fake = contextlib.contextmanager(lambda: iter([conn]))
    monkeypatch.setattr(occ.db, "pg_conn", _fake)

    out = occ.run({"execute": False})
    assert "DRY-RUN" in out and "售后 3" in out and "绩效 5" in out and "对账 9" in out
    assert not any(s.startswith("DELETE") for s, _ in conn.sqls)


def test_cleanup_execute_deletes_and_marks_ledger(monkeypatch):
    from workflows import order_center_cleanup as occ
    conn = _QueryConn(fetchone_seq=[(3,), (5,), (9,)],
                      vanish=[("T1", "05062025")])
    _fake = contextlib.contextmanager(lambda: iter([conn]))
    monkeypatch.setattr(occ.db, "pg_conn", _fake)

    out = occ.run({"execute": True})
    deletes = [s for s, _ in conn.sqls if s.startswith("DELETE")]
    assert len(deletes) == 3
    assert any("recon_done:T1" in str(a) for _, a in conn.sqls if a)
    assert "清理完成" in out
