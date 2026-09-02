"""api/orders async 变体(蓝图 §6.3)回归:多店并发拉取的同步门面。"""

import httpx
import pytest

from api import _client, orders as orders_api


@pytest.fixture(autouse=True)
def _clean_client_state():
    """换 token 走的是 _client 的同步连接池(按代理维度缓存 transport),
    上一个用例装的 MockTransport 会留在池里,本用例的桩根本接不到 /v3/token。"""
    _client._token_cache.clear()
    _client._rate_state.clear()
    for c in _client._client_pool.values():
        c.close()
    _client._client_pool.clear()
    yield
    for c in _client._client_pool.values():
        c.close()
    _client._client_pool.clear()
    _client._token_cache.clear()
    _client._rate_state.clear()


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


# ── 2026-09-02 下单时间事故后补的三道口子 ──────────────────────────────────────

async def _nosleep(_s):
    return None


def test_bulk_stops_on_repeated_cursor_and_flags_overcount(monkeypatch, caplog):
    """同 cursor 重复 = 服务端未推进,立即停(照抄 api/returns 三道闸);
    实拉对象数 > totalCount 是翻页重放/对象重复的指纹,必须响亮。"""
    calls = []

    def handler(req: httpx.Request):
        calls.append(str(req.url))
        return _page([{"purchaseOrderId": "PO1"}], next_cursor="?cursor=same", total=1)

    _seam(monkeypatch, handler)
    got = {}
    with caplog.at_level("WARNING"):
        results, dead, failed = orders_api.fetch_orders_bulk(
            [_store("T1")], created_start="2026-08-01T00:00:00Z",
            handler=lambda s, orders: got.setdefault("n", len(orders)))
    assert len(calls) == 2 and not failed          # 第 2 页 cursor 重复即停
    assert got["n"] == 2                            # api 层不去重,交 services 先到者胜
    assert "nextCursor 重复" in caplog.text
    assert "> 服务端 totalCount 1" in caplog.text


def test_async_socks_error_is_retried_not_fatal(monkeypatch):
    """SOCKS 层异常不在 httpx 异常树上(08-26 事故根因):异步路径此前只接
    httpx.HTTPError,一次 Malformed reply 直接穿出去。现在与同步路径同口径退避重试。"""
    from socksio.exceptions import ProtocolError
    state = {"n": 0}

    def handler(req: httpx.Request):
        state["n"] += 1
        if state["n"] == 1:
            raise ProtocolError("Malformed reply")
        return _page([{"purchaseOrderId": "PO1"}], total=1)

    _seam(monkeypatch, handler)
    monkeypatch.setattr(orders_api.asyncio, "sleep", _nosleep)
    results, dead, failed = orders_api.fetch_orders_bulk(
        [_store("T1")], created_start="2026-08-01T00:00:00Z",
        handler=lambda s, orders: len(orders))
    assert not failed and not dead and results[0]["lines"] == 1


def test_async_refreshed_token_is_reused_on_next_page(monkeypatch):
    """401 自愈刷新的 token 此前只活在 _get_async 的形参里,第 2 页起整店拿死 token
    翻页 → 被误判凭证失效。现在每页从缓存取新 token。"""
    _client._token_cache.clear()
    tokens = {"n": 0}
    seen_tokens = []

    def full(req: httpx.Request):
        if req.url.path == "/v3/token":
            tokens["n"] += 1
            return httpx.Response(200, json={"access_token": f"tok-{tokens['n']}",
                                             "expires_in": 900})
        seen_tokens.append(req.headers.get("WM_SEC.ACCESS_TOKEN"))
        if "cursor=p2" in str(req.url):
            return _page([{"purchaseOrderId": "PO2"}])
        if seen_tokens.count("tok-1") == 1 and len(seen_tokens) == 1:
            return httpx.Response(401, json={})        # 首页第一次:token 死了
        return _page([{"purchaseOrderId": "PO1"}], next_cursor="?cursor=p2", total=2)

    monkeypatch.setattr(_client, "_build_transport",
                        lambda proxy: httpx.MockTransport(full))
    results, dead, failed = orders_api.fetch_orders_bulk(
        [_store("T1")], created_start="2026-08-01T00:00:00Z",
        handler=lambda s, orders: len(orders))
    assert not dead and not failed and results[0]["lines"] == 2
    assert seen_tokens == ["tok-1", "tok-2", "tok-2"]   # 刷新后第 2 页用的是新 token


def test_correlation_id_echo_mismatch_is_logged(monkeypatch, caplog):
    """沃尔玛回显的 WM_QOS.CORRELATION_ID 与请求不符 = 响应错配的唯一现场证据,
    必须告警(先不拒收);回显一致或没回显都安静。"""
    mode = {"echo": "same"}

    def handler(req: httpx.Request):
        sent = req.headers.get("WM_QOS.CORRELATION_ID")
        hdr = {}
        if mode["echo"] == "same":
            hdr["WM_QOS.CORRELATION_ID"] = sent
        elif mode["echo"] == "other":
            hdr["WM_QOS.CORRELATION_ID"] = "00000000-0000-0000-0000-000000000000"
        r = _page([{"purchaseOrderId": "PO1"}], total=1)
        r.headers.update(hdr)
        return r

    _seam(monkeypatch, handler)
    for echo, expect_warn in (("same", False), ("none", False), ("other", True)):
        mode["echo"] = echo
        caplog.clear()
        with caplog.at_level("WARNING"):
            results, dead, failed = orders_api.fetch_orders_bulk(
                [_store("T1")], created_start="2026-08-01T00:00:00Z",
                handler=lambda s, orders: len(orders))
        assert not dead and not failed and results[0]["lines"] == 1
        assert ("响应相关 ID 与请求不符" in caplog.text) is expect_warn, echo
