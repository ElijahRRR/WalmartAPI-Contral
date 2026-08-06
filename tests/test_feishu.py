"""api/feishu.py 行为回归:token 缓存与失效重试 / 瞬时退避 / 批量切块 / 分页 / 错误模型。"""

import time
from types import SimpleNamespace

import httpx
import pytest

from api import feishu
from registry.resources import Bitable

TABLE = Bitable(name="测试表", app_token="appX", table_id="tblY",
                fields=SimpleNamespace(a="字段A"))


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setenv("WALMART_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_test")
    feishu._token_cache.clear()
    monkeypatch.setattr(time, "sleep", lambda s: None)  # 退避不真等
    yield
    if feishu._client is not None:
        feishu._client.close()
    feishu._client = None
    feishu._token_cache.clear()


def _use_handler(handler):
    feishu._client = httpx.Client(transport=httpx.MockTransport(handler))


def _token_ok(n=1):
    return httpx.Response(200, json={"code": 0, "tenant_access_token": f"tt{n}",
                                     "expire": 7200})


def test_transient_code_retries_then_succeeds():
    seq = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return _token_ok()
        seq["n"] += 1
        if seq["n"] == 1:
            return httpx.Response(200, json={"code": 90235, "msg": "data not ready"})
        return httpx.Response(200, json={"code": 0, "data": {
            "records": [{"record_id": "rec1"}]}})

    _use_handler(handler)
    ids = feishu.batch_create(TABLE, [{"字段A": 1}])
    assert ids == ["rec1"]
    assert seq["n"] == 2


def test_transient_text_fallback_track():
    # int code 不在瞬时集合,但错误文本命中子串轨 → 仍应重试
    seq = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return _token_ok()
        seq["n"] += 1
        if seq["n"] == 1:
            return httpx.Response(200, json={"code": 424242, "msg": "Request Timeout"})
        return httpx.Response(200, json={"code": 0, "data": {"records": []}})

    _use_handler(handler)
    feishu.batch_update(TABLE, [{"record_id": "r", "fields": {"字段A": 2}}])
    assert seq["n"] == 2


def test_token_invalid_refreshes_and_retries():
    state = {"token": 0, "biz": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            state["token"] += 1
            return _token_ok(state["token"])
        state["biz"] += 1
        if request.headers["Authorization"] == "Bearer tt1":
            return httpx.Response(200, json={"code": 99991663, "msg": "token invalid"})
        return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})

    _use_handler(handler)
    assert feishu.list_records(TABLE) == []
    assert state["token"] == 2 and state["biz"] == 2


def test_non_transient_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return _token_ok()
        return httpx.Response(200, json={"code": 1254045, "msg": "FieldNameNotFound"})

    _use_handler(handler)
    with pytest.raises(feishu.FeishuError) as ei:
        feishu.list_records(TABLE)
    assert ei.value.code == 1254045


def test_batch_create_chunks_at_500():
    batches = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return _token_ok()
        import json
        records = json.loads(request.content)["records"]
        batches.append(len(records))
        return httpx.Response(200, json={"code": 0, "data": {
            "records": [{"record_id": f"r{i}"} for i in range(len(records))]}})

    _use_handler(handler)
    ids = feishu.batch_create(TABLE, [{"字段A": i} for i in range(1200)])
    assert batches == [500, 500, 200]
    assert len(ids) == 1200


def test_list_records_paginates():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return _token_ok()
        calls.append(dict(request.url.params))
        if "page_token" not in request.url.params:
            return httpx.Response(200, json={"code": 0, "data": {
                "items": [{"record_id": "r1", "fields": {"字段A": 1}}],
                "has_more": True, "page_token": "PT2"}})
        return httpx.Response(200, json={"code": 0, "data": {
            "items": [{"record_id": "r2", "fields": {"字段A": 2}}],
            "has_more": False}})

    _use_handler(handler)
    recs = feishu.list_records(TABLE)
    assert [r["record_id"] for r in recs] == ["r1", "r2"]
    assert calls[1]["page_token"] == "PT2"


def test_http_client_ignores_env_proxy(monkeypatch):
    # 生产 Mac 挂着 Clash:HTTP(S)_PROXY 环境变量绝不能劫持飞书请求
    # (旧系统 2026-05-07 事故:本机代理拒连 → 14,610 个单元格写回失败)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7897")
    feishu._client = None  # 强制走 _http() 真实构造路径
    client = feishu._http()
    assert client.trust_env is False
    assert client._mounts == {}  # 无任何代理挂载


def test_unregistered_table_rejected():
    empty = Bitable(name="未登记", app_token="", table_id="",
                    fields=SimpleNamespace())
    with pytest.raises(LookupError):
        feishu.list_records(empty)


# ── sync_by_key(展示投影键对齐同步)────────────────────────────────────────


def test_sync_by_key_create_update_delete_and_dedupe():
    import json as _json
    calls: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "tenant_access_token" in path:
            return _token_ok()
        body = _json.loads(request.content or b"{}")
        if path.endswith("/records/search"):
            calls["search_body"] = body
            return httpx.Response(200, json={"code": 0, "data": {"items": [
                # 文本字段的分段结构也要能读出键
                {"record_id": "rA", "fields": {"字段A": [{"text": "k1", "type": "text"}]}},
                {"record_id": "rB", "fields": {"字段A": "k2"}},
                {"record_id": "rB2", "fields": {"字段A": "k2"}},
                {"record_id": "rC", "fields": {"字段A": "gone"}},
            ], "has_more": False}})
        if path.endswith("/batch_create"):
            calls["create"] = body["records"]
            return httpx.Response(200, json={"code": 0, "data": {
                "records": [{"record_id": "new1"}]}})
        if path.endswith("/batch_update"):
            calls["update"] = body["records"]
            return httpx.Response(200, json={"code": 0, "data": {
                "records": body["records"]}})
        if path.endswith("/batch_delete"):
            calls["delete"] = body["records"]
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(f"意外请求 {path}")

    _use_handler(handler)
    c, u, d = feishu.sync_by_key(TABLE, "字段A", {
        "k1": {"字段A": "k1", "x": 1},
        "k2": {"字段A": "k2", "x": None},   # None 显式送 null 清空
        "k3": {"字段A": "k3"},
    })
    assert (c, u, d) == (1, 2, 2)           # 建 k3;改 k1/k2;删 重复rB2 + 消失rC
    assert calls["search_body"]["field_names"] == ["字段A"]
    assert calls["create"] == [{"fields": {"字段A": "k3"}}]
    assert {r["record_id"] for r in calls["update"]} == {"rA", "rB"}
    upd_k2 = next(r for r in calls["update"] if r["record_id"] == "rB")
    assert upd_k2["fields"]["x"] is None    # null 必须保留在载荷里
    assert set(calls["delete"]) == {"rB2", "rC"}


def test_sync_by_key_wrong_table_guard():
    # 表里有记录但全都读不出键字段 → 疑似 table_id 错登记,必须拒绝而非清空对方
    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return _token_ok()
        assert request.url.path.endswith("/records/search")
        return httpx.Response(200, json={"code": 0, "data": {"items": [
            {"record_id": "x1", "fields": {"别的字段": "v"}},
            {"record_id": "x2", "fields": {"别的字段": "w"}},
        ], "has_more": False}})

    _use_handler(handler)
    with pytest.raises(feishu.FeishuError, match="疑似 table_id 登记错表"):
        feishu.sync_by_key(TABLE, "字段A", {"k": {"字段A": "k"}})


def test_sync_by_key_no_delete_mode():
    # delete_stale=False:消失键/重复键/无键行一律不删(人工域共存表)
    import json as _json
    calls: dict = {"delete": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "tenant_access_token" in path:
            return _token_ok()
        body = _json.loads(request.content or b"{}")
        if path.endswith("/records/search"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [
                {"record_id": "rA", "fields": {"字段A": "k1"}},
                {"record_id": "rA2", "fields": {"字段A": "k1"}},
                {"record_id": "rGone", "fields": {"字段A": "gone"}},
            ], "has_more": False}})
        if path.endswith("/batch_update"):
            return httpx.Response(200, json={"code": 0, "data": {
                "records": body["records"]}})
        if path.endswith("/batch_delete"):
            calls["delete"].extend(body["records"])
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(f"意外请求 {path}")

    _use_handler(handler)
    c, u, d = feishu.sync_by_key(TABLE, "字段A", {"k1": {"字段A": "k1"}},
                                 delete_stale=False)
    assert (c, u, d) == (0, 1, 0)
    assert calls["delete"] == []


def test_ensure_keys_creates_missing_only():
    import json as _json
    calls: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "tenant_access_token" in path:
            return _token_ok()
        body = _json.loads(request.content or b"{}")
        if path.endswith("/records/search"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [
                {"record_id": "r1", "fields": {"字段A": [{"text": "k1", "type": "text"}]}},
            ], "has_more": False}})
        if path.endswith("/batch_create"):
            calls["create"] = body["records"]
            return httpx.Response(200, json={"code": 0, "data": {
                "records": [{"record_id": "n1"}, {"record_id": "n2"}]}})
        raise AssertionError(f"意外请求 {path}(ensure_keys 不许更新/删除)")

    _use_handler(handler)
    n = feishu.ensure_keys(TABLE, "字段A", {"k1", "k2", "k3", ""})
    assert n == 2
    # 只写键字段,且已有的 k1 与空键不建
    assert calls["create"] == [{"fields": {"字段A": "k2"}},
                               {"fields": {"字段A": "k3"}}]
