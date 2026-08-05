"""api/_client.py 移植行为回归:token 缓存 / 401 自愈 / 429 退避 / retry-after 解析。

旧仓库对 walmart_client 零测试覆盖,行为规格只存在于代码注释;
本文件把移植规格固化下来,后续任何改动跑 pytest 即可回归。
"""

import time

import httpx
import pytest

from api import _client


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setenv("WALMART_DATA_ROOT", str(tmp_path))
    _client._token_cache.clear()
    for c in _client._client_pool.values():
        c.close()
    _client._client_pool.clear()
    yield
    for c in _client._client_pool.values():
        c.close()
    _client._client_pool.clear()
    _client._token_cache.clear()


def _use_transport(monkeypatch, handler):
    monkeypatch.setattr(_client, "_build_transport",
                        lambda proxy: httpx.MockTransport(handler))


def test_token_cached_within_ttl(monkeypatch):
    calls = {"token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/token"
        calls["token"] += 1
        return httpx.Response(200, json={"access_token": f"tok{calls['token']}",
                                         "expires_in": 900})

    _use_transport(monkeypatch, handler)
    t1 = _client.get_token("cid", "sec", "socks5://x:1")
    t2 = _client.get_token("cid", "sec", "socks5://x:1")
    assert t1 == t2 == "tok1"
    assert calls["token"] == 1


def test_401_self_heal_refreshes_token_once(monkeypatch):
    state = {"token": 0, "gets": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/token":
            state["token"] += 1
            return httpx.Response(200, json={"access_token": f"tok{state['token']}",
                                             "expires_in": 900})
        state["gets"].append(request.headers["WM_SEC.ACCESS_TOKEN"])
        if state["gets"][-1] == "tok1":
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"ok": 1})

    _use_transport(monkeypatch, handler)
    token = _client.get_token("cid", "sec", None)
    status, _, data = _client.safe_get_ex("https://marketplace.walmartapis.com/v3/x",
                                          token, "cid", None)
    assert status == 200 and data == {"ok": 1}
    assert state["token"] == 2          # 401 后只换了一次 token
    assert state["gets"] == ["tok1", "tok2"]


def test_429_backoff_honors_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    seq = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seq["n"] += 1
        if seq["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={})
        return httpx.Response(200, json={"ok": 1})

    _use_transport(monkeypatch, handler)
    status, _, data = _client.safe_get_ex("https://h/x", "t", "cid", None, max_retries=1)
    assert status == 200 and data == {"ok": 1}
    assert sleeps == [7.0]


def test_5xx_exponential_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    seq = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seq["n"] += 1
        return httpx.Response(503 if seq["n"] <= 2 else 200, json={"ok": seq["n"]})

    _use_transport(monkeypatch, handler)
    status, _, _ = _client.safe_get_ex("https://h/x", "t", "cid", None, max_retries=3)
    assert status == 200
    assert sleeps == [1, 2]  # min(2**attempt, 10)


def test_non_2xx_returns_status_without_retry(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    _use_transport(monkeypatch, handler)
    status, headers, data = _client.safe_get_ex("https://h/x", "t", "cid", None, max_retries=3)
    assert status == 404 and data is None
    assert isinstance(headers, dict)


def test_parse_retry_after_priority():
    assert _client._parse_retry_after({"retry-after": "3"}) == 3.0
    assert _client._parse_retry_after({"retry-after": "9999"}) == 300.0  # 上限
    # X-Next-Replenishment-Time epoch 毫秒
    future_ms = (time.time() + 42) * 1000
    wait = _client._parse_retry_after({"x-next-replenishment-time": str(future_ms)})
    assert 40 < wait < 43
    assert _client._parse_retry_after({}) == 60.0  # 兜底
