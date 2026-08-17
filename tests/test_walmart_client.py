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


@pytest.mark.parametrize("status", [400, 401, 403])
def test_token_4xx_is_store_dead_not_raw_http_error(monkeypatch, status):
    """换 token 被拒 ⇒ StoreDeadError(调用方"跳过全店"),不是裸 HTTPStatusError。

    2026-08-17 生产实见:沃尔玛在凭证被拒时回 **400**(不是 401),于是它以
    httpx 原生异常冒进 workflow 的泛化 except ⇒ 一家店凭证坏掉判整轮失败;
    而同样坏掉、只是回 401 的店走的是"跳过、整轮照常成功"。同一个现实原因
    两种相反结果,差别只在沃尔玛回哪个码。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "invalid_client"})

    _use_transport(monkeypatch, handler)
    with pytest.raises(_client.StoreDeadError) as ei:
        _client.get_token("cid-abcdef-tail", "sec", "socks5://x:1")
    assert ei.value.status == status
    # 报错里带 client_id 前缀便于定位,但不带整串
    assert "cid-ab" in str(ei.value) and "cid-abcdef-tail" not in str(ei.value)


def test_token_5xx_still_raises_http_error(monkeypatch):
    """5xx 不是凭证问题,不能当死店跳过(跳过 = 那店当轮静默不同步)。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    _use_transport(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        _client.get_token("cid", "sec", "socks5://x:1")


def test_store_dead_error_call_sites_have_two_args():
    """`StoreDeadError(...)` 必须传 (店铺, 状态码) 两个参数。

    api/reports.py 与 api/returns.py 曾各有一处只传一个 f-string:真触发时抛的
    是 `TypeError: missing 1 required positional argument`,既不会被
    `except StoreDeadError` 接住(于是整轮判失败,而不是跳过这家店),消息里
    也没有店名和状态码。一个参数的构造在语法上完全合法,只有真跑到那一行才
    炸——这种缺陷靠用例钉住比靠人眼靠谱。
    """
    import ast
    import pathlib

    bad = []
    for path in pathlib.Path("api").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "StoreDeadError" and len(node.args) + len(node.keywords) != 2:
                bad.append(f"{path}:{node.lineno} 传了 {len(node.args)} 个参数")
    assert not bad, "StoreDeadError 参数个数不对:\n" + "\n".join(bad)
