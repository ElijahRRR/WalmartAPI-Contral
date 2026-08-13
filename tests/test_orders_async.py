"""api/orders async 变体(蓝图 §6.3)回归:多店并发拉取的同步门面。"""

import httpx

from api import _client, orders as orders_api


def _seam(monkeypatch, handler):
    """与 test_order_workflows._use 同款测试缝:MockTransport 双栈,
    async 路径必须走同一个桩(绕出去真打沃尔玛=事故)。"""
    def full(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/token":
            return httpx.Response(200, json={"access_token": "tok",
                                             "expires_in": 900})
        return handler(request)
    monkeypatch.setattr(_client, "_build_transport",
                        lambda proxy: httpx.MockTransport(full))


def _store(name):
    return {"name": name, "client_id": f"cid-{name}",
            "client_secret": "s", "proxy": None}


def _page(orders, next_cursor=None, total=None):
    meta = {}
    if total is not None:
        meta["totalCount"] = total
    if next_cursor:
        meta["nextCursor"] = next_cursor
    return httpx.Response(200, json={"list": {
        "elements": {"order": orders}, "meta": meta}})


def test_bulk_two_stores_with_pagination(monkeypatch):
    """分页模型 2:nextCursor 是带 '?' 的完整 query 串,第二页 URL=base+串。"""
    seen: dict[str, list[str]] = {}

    def handler(req: httpx.Request):
        cid = req.headers["WM_CONSUMER.ID"]
        seen.setdefault(cid, []).append(str(req.url))
        if cid == "cid-T1":
            if "cursor=p2" in str(req.url):
                return _page([{"purchaseOrderId": "PO2"}])
            return _page([{"purchaseOrderId": "PO1"}],
                         next_cursor="?cursor=p2", total=2)
        return _page([{"purchaseOrderId": "PO9"}], total=1)

    _seam(monkeypatch, handler)
    persisted: dict[str, int] = {}

    def persist(store, orders):
        persisted[store["name"]] = len(orders)
        return len(orders) * 10

    results, dead, failed = orders_api.fetch_orders_bulk(
        [_store("T1"), _store("T2")], created_start="2026-08-01T00:00:00Z",
        handler=persist)
    assert not dead and not failed
    by = {r["store"]: r for r in results}
    assert by["T1"]["orders"] == 2 and by["T1"]["lines"] == 20
    assert by["T2"]["orders"] == 1 and by["T2"]["lines"] == 10
    assert persisted == {"T1": 2, "T2": 1}
    # 第二页 URL 直接拼 cursor 串,不重复带 createdStartDate 参数
    p2 = seen["cid-T1"][1]
    assert p2.endswith("/v3/orders?cursor=p2")


def test_bulk_404_means_zero_orders(monkeypatch):
    _seam(monkeypatch, lambda req: httpx.Response(404, json={}))
    results, dead, failed = orders_api.fetch_orders_bulk(
        [_store("T1")], created_start="2026-08-01T00:00:00Z",
        handler=lambda s, o: len(o))
    assert not dead and not failed
    assert results == [{"store": "T1", "orders": 0, "lines": 0}]


def test_bulk_dead_store_isolated(monkeypatch):
    """401 店进 dead 列,不拖垮其它店(单店隔离)。"""
    def handler(req: httpx.Request):
        if req.headers["WM_CONSUMER.ID"] == "cid-BAD":
            return httpx.Response(401, json={})
        return _page([{"purchaseOrderId": "PO1"}], total=1)

    _seam(monkeypatch, handler)
    results, dead, failed = orders_api.fetch_orders_bulk(
        [_store("BAD"), _store("OK")], created_start="2026-08-01T00:00:00Z")
    assert dead == ["BAD"] and not failed
    assert [r["store"] for r in results] == ["OK"]
    assert results[0]["lines"] is None      # 无 handler 不落库


def test_bulk_429_retries_then_succeeds(monkeypatch):
    hits = {"n": 0}

    def handler(req: httpx.Request):
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={})
        return _page([{"purchaseOrderId": "PO1"}], total=1)

    _seam(monkeypatch, handler)
    results, dead, failed = orders_api.fetch_orders_bulk(
        [_store("T1")], created_start="2026-08-01T00:00:00Z")
    assert not dead and not failed
    assert results[0]["orders"] == 1 and hits["n"] == 2
