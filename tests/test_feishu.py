"""api/feishu.py 行为回归:token 缓存与失效重试 / 瞬时退避 / 批量切块 / 分页 / 错误模型。"""

import time
from types import SimpleNamespace

import httpx
import pytest

from api import feishu
from registry.resources import Bitable, Spreadsheet

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



def test_sheet_write_ranges_splits_big_range_and_scrubs(monkeypatch):
    """单个范围裹上千行会被飞书 90202 拒(所有者 2026-08-09 实遇);按行切。"""
    sent = []
    monkeypatch.setattr(feishu, "_call",
                        lambda m, p, **kw: sent.append(kw.get("json_body")) or {})
    sheet = Spreadsheet(name="X", token="TOK", sheet_id="SID",
                        columns=("a", "b"))
    monkeypatch.setattr(feishu, "_SHEET_WRITE_BLOCK_ROWS", 2)
    rows = [["v%d" % i, "x\x00y"] for i in range(5)]
    n = feishu.sheet_write_ranges(sheet, [("A11:B15", rows)])
    ranges = [vr["range"] for body in sent for vr in body["valueRanges"]]
    assert ranges == ["SID!A11:B12", "SID!A13:B14", "SID!A15:B15"]
    assert n == 3
    # 控制字符会让飞书整批拒收,先剔掉(剔到了记日志,不静默)
    assert sent[0]["valueRanges"][0]["values"][0][1] == "xy"
