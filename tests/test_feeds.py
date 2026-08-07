"""api/feeds.py 回归:header 分发、切片双约束、三层防重、反查三态、明细翻页。"""

import contextlib
import json
import time

import httpx
import pytest

from api import _client, feeds
from registry import resources

STORE = {"name": "T1", "client_id": "cid_feed", "client_secret": "sec", "proxy": None}


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


class _LogDB:
    """可编程 feed_log 假库:claim 成败、既有行、记录全部 update。"""

    def __init__(self, claim=True, prev=(9, "submitted", "F_OLD")):
        self.claim, self.prev = claim, prev
        self.sqls: list = []

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))
        self._last = sql

    def executemany(self, sql, rows):
        self.sqls.append((sql, list(rows)))

    def fetchone(self):
        if "INSERT INTO ops.feed_log" in self._last:
            return (1,) if self.claim else None
        if "SELECT id, status, feed_id" in self._last:
            return self.prev
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_db(monkeypatch, logdb: _LogDB):
    from registry import db

    @contextlib.contextmanager
    def fake_conn():
        yield logdb
    monkeypatch.setattr(db, "pg_conn", fake_conn)


def _updates(logdb):
    return [(s, a) for s, a in logdb.sqls if s.strip().startswith("UPDATE")]


# ── 载荷构造与切片 ────────────────────────────────────────────────────────────

def test_build_payload_schemas():
    p = feeds.build_payload("DELETE_ITEM", ["A", "B"])
    assert p["ItemFeedHeader"]["version"] == resources.FEED_SPEC_VERSIONS["DELETE_ITEM"]
    assert p["ItemFeedHeader"]["businessUnit"] == "WALMART_US"
    assert p["Item"] == [{"Deletable": {"sku": "A"}}, {"Deletable": {"sku": "B"}}]

    r = feeds.build_payload("RETIRE_ITEM", ["X"])
    assert r["RetireItemHeader"]["version"] == "1.0"
    assert r["RetireItemHeader"]["feedDate"].endswith(".000Z")   # 真 UTC
    assert r["RetireItem"] == [{"sku": "X"}]

    m = feeds.build_payload("MP_MAINTENANCE", [{"sku": "S", "price": 1.999}])
    assert m["MPItem"][0]["price"] == 2.0                        # sanitize round2
    assert "MPItemFeedHeader" in m

    with pytest.raises(ValueError, match="未在 api/feeds.py 收录"):
        feeds.build_payload("MP_ITEM", [])


def test_slices_item_and_byte_caps(monkeypatch):
    assert len(feeds._slices("DELETE_ITEM", [f"S{i}" for i in range(2500)])) == 1
    assert len(feeds._slices("DELETE_ITEM", [f"S{i}" for i in range(2501)])) == 2
    # 字节约束:收紧上限后长 SKU 被拆片
    monkeypatch.setitem(feeds._SLICE_LIMITS, "DELETE_ITEM", (100, 300))
    chunks = feeds._slices("DELETE_ITEM", ["X" * 40 for _ in range(10)])
    assert len(chunks) > 1 and sum(len(c) for c in chunks) == 10


def test_payload_key_order_independent():
    assert feeds.payload_key("DELETE_ITEM", ["A", "B"]) == \
        feeds.payload_key("DELETE_ITEM", ["B", "A"])
    assert feeds.payload_key("DELETE_ITEM", ["A"]) != \
        feeds.payload_key("RETIRE_ITEM", ["A"])


# ── 提交:防重/成功/被拒/反查三态 ─────────────────────────────────────────────

def test_submit_dedup_refuses_resubmission(monkeypatch):
    logdb = _LogDB(claim=False)
    _fake_db(monkeypatch, logdb)
    _use(monkeypatch, lambda r: (_ for _ in ()).throw(
        AssertionError("防重命中时绝不能发 HTTP")))

    out = feeds.submit_feed(STORE, "DELETE_ITEM", ["A", "B"], workflow="t")
    assert out == [{"feed_id": "F_OLD", "count": 2, "outcome": "dedup"}]


def test_log_claim_reclaims_done_row(monkeypatch):
    # 所有者定稿:防重只拦在途(pending/submitted);done=上一笔已完结,
    # 同载荷重发是新一轮合法操作(顽固 SKU 每日重发/反补第 2 次依赖此语义)
    logdb = _LogDB(claim=False, prev=(9, "done", "F_OLD"))
    _fake_db(monkeypatch, logdb)
    _use(monkeypatch, lambda r: httpx.Response(200, json={"feedId": "F_NEW"}))
    out = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t")
    assert out[0]["outcome"] == "submitted" and out[0]["feed_id"] == "F_NEW"
    assert any("'pending'" in s for s, _ in _updates(logdb))   # done 行重占回 pending


def test_find_recent_feed_excludes_claimed_sibling(monkeypatch):
    # 同尺寸兄弟切片:反查必须排除 feed_log 已占用的 feedId,防误收编整片丢失
    class _DB(_LogDB):
        def fetchall(self):
            if "SELECT feed_id FROM ops.feed_log" in self._last:
                return [("F_SIB",)]
            return []
    logdb = _DB(claim=True)
    _fake_db(monkeypatch, logdb)
    calls = {"post": 0}
    now_ms = int(time.time() * 1000)

    def handler(request):
        if request.method == "POST":
            calls["post"] += 1
            if calls["post"] == 1:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"feedId": "F_NEW"})
        return httpx.Response(200, json={"results": {"feed": [
            {"feedId": "F_SIB", "itemsReceived": 1, "feedDate": now_ms}]}})

    _use(monkeypatch, handler)
    out = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t")
    assert out[0]["feed_id"] == "F_NEW" and calls["post"] == 2   # 排除→未达→补交


def test_submit_success_marks_submitted(monkeypatch):
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)
    seen = {}

    def handler(request):
        assert request.url.path == "/v3/feeds"
        assert request.url.params["feedType"] == "DELETE_ITEM"
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"feedId": "F1@abc"})

    _use(monkeypatch, handler)
    out = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t")
    assert out[0]["outcome"] == "submitted" and out[0]["feed_id"] == "F1@abc"
    assert seen["body"]["Item"] == [{"Deletable": {"sku": "A"}}]
    sql, args = _updates(logdb)[0]
    assert "'submitted'" in sql or args[0] == "submitted"


def test_submit_rejected_marks_failed_no_retry(monkeypatch):
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad"})

    _use(monkeypatch, handler)
    out = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t")
    assert out[0]["outcome"] == "failed"
    assert calls["n"] == 1                       # 被拒绝不自动重试
    assert any(a and a[0] == "failed" for _, a in _updates(logdb))


def test_submit_network_error_found_adopts_feed(monkeypatch):
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)
    calls = {"post": 0}
    now_ms = int(time.time() * 1000)

    def handler(request):
        if request.method == "POST":
            calls["post"] += 1
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"results": {"feed": [
            {"feedId": "F_REC", "itemsReceived": 2, "feedDate": now_ms}]}})

    _use(monkeypatch, handler)
    out = feeds.submit_feed(STORE, "DELETE_ITEM", ["A", "B"], workflow="t")
    assert out[0] == {"feed_id": "F_REC", "count": 2, "outcome": "submitted"}
    assert calls["post"] == 1                    # 反查已达 → 收编,不补交


def test_submit_network_error_notfound_resubmits_once(monkeypatch):
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)
    calls = {"post": 0}

    def handler(request):
        if request.method == "POST":
            calls["post"] += 1
            if calls["post"] == 1:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"feedId": "F_2ND"})
        return httpx.Response(200, json={"results": {"feed": []}})   # 两次都查无

    _use(monkeypatch, handler)
    out = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t")
    assert out[0]["outcome"] == "submitted" and out[0]["feed_id"] == "F_2ND"
    assert calls["post"] == 2                    # 确认未达 → 同一载荷补交一次


def test_submit_network_error_unknown_keeps_pending(monkeypatch):
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)

    def handler(request):
        if request.method == "POST":
            raise httpx.ConnectError("boom")
        return httpx.Response(500, json={})      # 反查自身失败 → UNKNOWN

    _use(monkeypatch, handler)
    out = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t")
    assert out[0]["outcome"] == "unknown"
    assert _updates(logdb) == []                 # 保持 pending,留给启动对账


# ── 状态轮询与明细 ────────────────────────────────────────────────────────────

def test_iter_feed_items_paginates_and_quotes_feed_id(monkeypatch):
    pages = []

    def handler(request):
        assert "%40" in request.url.raw_path.decode()     # '@' 必须转义
        offset = int(request.url.params["offset"])
        pages.append(offset)
        items = [{"sku": f"S{offset + i}", "ingestionStatus": "SUCCESS"}
                 for i in range(50 if offset == 0 else 10)]
        return httpx.Response(200, json={
            "itemsReceived": 60,
            "itemDetails": {"itemIngestionStatus": items}})

    _use(monkeypatch, handler)
    got = list(feeds.iter_feed_items(STORE, "F1@abc"))
    assert len(got) == 60 and pages == [0, 50]


def test_get_feed_status_warns_on_unknown_status(monkeypatch, caplog):
    import logging as _logging
    _use(monkeypatch, lambda r: httpx.Response(200, json={
        "feedStatus": "WEIRD_NEW", "itemsReceived": 1}))
    with caplog.at_level(_logging.WARNING, logger="api.feeds"):
        data = feeds.get_feed_status(STORE, "F1@abc")
    assert data["feedStatus"] == "WEIRD_NEW"
    assert any("未知 feedStatus" in m for m in caplog.messages)


def test_sku_outcome_mapping(caplog):
    assert feeds.sku_outcome("SUCCESS") == "success"
    for s in ("DATA_ERROR", "SYSTEM_ERROR", "TIMEOUT_ERROR"):
        assert feeds.sku_outcome(s) == "failed"
    assert feeds.sku_outcome("INPROGRESS") == "processing"
    assert feeds.sku_outcome("NEW_VALUE") == "unknown"   # 不装成功也不装失败


def test_feed_rate_buckets_default_deny():
    _client.rate_acquire("feeds.post.DELETE_ITEM", "cid_bucket_test")
    _client.rate_acquire("feeds.get", "cid_bucket_test")
    with pytest.raises(KeyError, match="限速桶未登记"):
        _client.rate_acquire("feeds.post.MP_ITEM", "cid_bucket_test")
