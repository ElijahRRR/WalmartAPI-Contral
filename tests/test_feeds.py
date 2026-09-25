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


def _status_updates(logdb):
    """改 feed_log 状态的 UPDATE(不含发送标记 post_started_at 那一条)。"""
    return [(s, a) for s, a in _updates(logdb) if "SET status" in s]


def _posting_marks(logdb):
    """请求真正发出前落的发送标记(pending 对账用:为空 = 确定没发出)。"""
    return [(s, a) for s, a in _updates(logdb) if "post_started_at = now()" in s]


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

    # MP_INVENTORY v1.5(多仓批次 2 翻案:此前是"登记不实现"的显式 raise)。
    # ⚠ key **小写**、每 SKU 带 shipNodes[]、数量字段名 `quantity` —— 三处
    # 与 v1.4 都不同,任一处套错都是整批退回
    mpi = feeds.build_payload("MP_INVENTORY",
                              [{"sku": "S", "qty": 7, "ship_node": "12345"}])
    assert set(mpi.keys()) == {"inventoryHeader", "inventory"}   # 小写,非 1.4
    assert mpi["inventoryHeader"]["version"] == "1.5"
    assert mpi["inventory"][0]["shipNodes"] == [
        {"shipNode": "12345", "quantity": {"unit": "EACH", "amount": 7}}]
    # 缺节点必须**响亮失败**:悄悄发出去就是写到官方无定义的"默认节点"
    with pytest.raises(ValueError, match="缺 ship_node"):
        feeds.build_payload("MP_INVENTORY", [{"sku": "S", "qty": 7}])


def test_build_payload_price_and_inventory_schemas():
    # PriceFeed v1.7:无外层包装(加 PriceFeed 包装→ERROR,旧实证);金额 round2
    p = feeds.build_payload("price", [{"sku": "A", "price": 19.999}])
    assert set(p.keys()) == {"PriceHeader", "Price"}          # 无 PriceFeed 外壳
    assert p["PriceHeader"]["version"] == "1.7"
    assert p["Price"][0]["pricing"][0]["currentPrice"]["amount"] == 20.0
    assert p["Price"][0]["pricing"][0]["currentPriceType"] == "BASE"

    # InventoryFeed v1.4:Inventory 首字母必须大写(小写→ERR_EXT_DATA_0503009)
    inv = feeds.build_payload("inventory", [{"sku": "B", "qty": 7}])
    assert set(inv.keys()) == {"InventoryHeader", "Inventory"}
    assert inv["InventoryHeader"]["version"] == "1.4"
    assert inv["Inventory"][0] == {"sku": "B",
                                   "quantity": {"unit": "EACH", "amount": 7}}


def test_price_inventory_chunk_skus_and_slices():
    entries = [{"sku": f"S{i}", "qty": 0} for i in range(4001)]
    assert len(feeds._slices("inventory", entries)) == 2       # 4000/片
    assert feeds._chunk_skus("price", [{"sku": "X", "price": 1}]) == ["X"]
    assert feeds._chunk_skus("inventory", [{"sku": "Y", "qty": 0}]) == ["Y"]
    # 2026-09-07 生产实见(谭总12):MP_INVENTORY 漏在 dict 类型表外 ⇒ 台账 sku 列
    # 落的是 str(dict),维护记录反哺永远「台账查无」。每种 dict 条目的 feedType 都要在表里。
    assert feeds._chunk_skus("MP_INVENTORY",
                             [{"sku": "Z", "qty": 3, "ship_node": "N1"}]) == ["Z"]
    for ft in ("price", "inventory", "MP_INVENTORY", "MP_MAINTENANCE",
               "MP_ITEM", "MP_ITEM_MATCH"):
        assert not feeds._chunk_skus(ft, [{"sku": "Q"}])[0].startswith("{"), ft


def test_put_price_and_put_inventory(monkeypatch):
    from api import inventory as inv_api, prices
    seen = {}

    def handler(request):
        seen[request.url.path] = json.loads(request.content)
        if request.url.path == "/v3/inventory":
            assert request.url.params["sku"] == "S1"
        return httpx.Response(200, json={"ok": True})

    _use(monkeypatch, handler)
    ok, why = prices.put_price(STORE, "S1", 19.999)
    assert ok and why == ""
    assert seen["/v3/price"]["pricing"][0]["currentPrice"]["amount"] == 20.0
    ok2, _why = inv_api.put_inventory(STORE, "S1", 3)
    assert ok2 and seen["/v3/inventory"]["quantity"] == {"unit": "EACH",
                                                         "amount": 3}

    _client._close_all_clients()
    _use(monkeypatch, lambda r: httpx.Response(400, json={"error": "bad"}))
    ok3, why3 = prices.put_price(STORE, "S1", 5)
    assert not ok3 and "HTTP 400" in why3


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


def test_iter_result_slices_walks_the_cursor_without_off_by_one():
    """submit_feed 只回 count 不回条目:对位全靠游标,错一位就是整批结局
    落到别人行上,而且**不报错**(6 个工作流各手写过一遍这段游标)。"""
    entries = [f"S{i}" for i in range(10)]
    results = [{"count": 3, "outcome": "submitted"},
               {"count": 1, "outcome": "dedup"},
               {"count": 6, "outcome": "failed"}]
    got = list(feeds.iter_result_slices(results, entries))

    assert [r for r, _ in got] == results               # 结果原样带出
    assert [len(b) for _, b in got] == [3, 1, 6]        # 逐片长度 = count
    assert [e for _, b in got for e in b] == entries    # 切片总和 = entries
    assert got[0][1] == ["S0", "S1", "S2"]
    assert got[1][1] == ["S3"]                          # 下一片从上一片之后接上
    assert got[2][1][0] == "S4" and got[2][1][-1] == "S9"
    assert list(feeds.iter_result_slices([], entries)) == []
    assert list(feeds.iter_result_slices([{"count": 0}], entries)) == \
        [({"count": 0}, [])]                            # 空片不吃条目


def test_iter_result_slices_reproduces_real_slicing(monkeypatch):
    """与真实切片对拍:片数、逐片长度、拼回原序都要还原 _slices 的分法。"""
    monkeypatch.setitem(feeds._SLICE_LIMITS, "DELETE_ITEM", (7, 350_000))
    entries = [f"S{i}" for i in range(23)]
    chunks = feeds._slices("DELETE_ITEM", entries)
    results = [{"count": len(c), "outcome": "submitted"} for c in chunks]

    assert [len(c) for c in chunks] == [7, 7, 7, 2]     # 末片不满也要对得上
    assert [b for _, b in feeds.iter_result_slices(results, entries)] == chunks


def test_iter_result_slices_takes_any_parallel_list():
    """entries 不必是提交的载荷,只要同序等长——6 个工作流传的都是自己的
    业务行(飞书行号 / (行, 载荷) 对 / 台账行)。"""
    rows = [{"rownum": i} for i in range(5)]
    results = [{"count": 2}, {"count": 3}]
    assert [b for _, b in feeds.iter_result_slices(results, rows)] == [
        rows[:2], rows[2:]]


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
    sql, args = _status_updates(logdb)[0]
    assert "'submitted'" in sql or args[0] == "submitted"
    assert len(_posting_marks(logdb)) == 1           # 发之前落了发送标记


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
    # 收口依据:沃尔玛拒收 + HTTP 码(光一个 failed 分不出"没发出"与"被拒")
    (_sql, args), = _status_updates(logdb)
    assert args[2].startswith("沃尔玛拒收 HTTP 400")


def test_submit_token_failure_is_definite_failed(monkeypatch):
    # token/代理阶段断线(2026-08-07 生产实证 SSL EOF):请求未发出=确定未达
    # → failed 可重占,不走反查三态,不向上抛炸整轮
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)

    def handler(request):
        if request.url.path == "/v3/token":
            raise httpx.ConnectError("SSL: UNEXPECTED_EOF_WHILE_READING")
        raise AssertionError("token 失败后不应发出任何 feed 请求")

    monkeypatch.setattr(_client, "_build_transport",
                        lambda proxy: httpx.MockTransport(handler))
    out = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t")
    assert out[0] == {"feed_id": None, "count": 1, "outcome": "failed",
                      "retryable": True}
    assert any(a and a[0] == "failed" for _, a in _updates(logdb))
    assert _posting_marks(logdb) == []           # 请求没发出 ⇒ 发送标记为空
    (_sql, args), = _status_updates(logdb)
    assert args[2].startswith("未发出")


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
    assert _status_updates(logdb) == []          # 保持 pending,留给轮询的 pending 对账
    assert len(_posting_marks(logdb)) == 1       # 发过了 ⇒ 对账器按发送时刻去反查


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
    _client.rate_acquire("feeds.post.MP_INVENTORY", "cid_bucket_test")
    with pytest.raises(KeyError, match="限速桶未登记"):
        _client.rate_acquire("feeds.post.SHIPPING_OVERRIDES", "cid_bucket_test")


def test_submit_5xx_found_adopts_instead_of_terminal_fail(monkeypatch):
    """5xx ≠ 4xx(2026-08-19 官方核验 + 生产实证 Akamai 'Internal Server
    Error - Read' = 边缘从源站读响应失败):请求可能已达、feed 可能已建成。
    终态拒会把已达的 feed 弄丢台账 → 必须反查三态,查到就收编。"""
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)
    calls = {"post": 0}
    now_ms = int(time.time() * 1000)

    def handler(request):
        if request.method == "POST":
            calls["post"] += 1
            return httpx.Response(500, text="<HTML><HEAD>\n<TITLE>Internal "
                                            "Server Error</TITLE></HEAD>")
        return httpx.Response(200, json={"results": {"feed": [
            {"feedId": "F_5XX", "itemsReceived": 1, "feedDate": now_ms}]}})

    _use(monkeypatch, handler)
    out = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t")
    assert out[0] == {"feed_id": "F_5XX", "count": 1, "outcome": "submitted"}
    assert calls["post"] == 1                    # 反查已达 → 收编,不补交


def test_submit_5xx_notfound_resubmits_once(monkeypatch):
    """5xx 且双确认未达 → 同一载荷补交一次(官方口径 retry with backoff;
    反查的 30s 双确认就是退避)。4xx 的"绝不重试"语义不变(上面老测试钉着)。"""
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)
    calls = {"post": 0}

    def handler(request):
        if request.method == "POST":
            calls["post"] += 1
            if calls["post"] == 1:
                return httpx.Response(500, text="<HTML>edge error</HTML>")
            return httpx.Response(200, json={"feedId": "F_RETRY"})
        return httpx.Response(200, json={"results": {"feed": []}})

    _use(monkeypatch, handler)
    out = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t")
    assert out[0]["outcome"] == "submitted" and out[0]["feed_id"] == "F_RETRY"
    assert calls["post"] == 2


# ── 延后结算(所有者定稿 2026-08-26:重试等整轮跑完)──────────────────────────

def test_defer_settle_hands_back_a_replayable_handle_and_writes_nothing(monkeypatch):
    """`defer_settle=True` 遇 5xx:**当场什么终态都不写**,只交回重放句柄。

    这是整件事的地基 —— 当轮写了终态(判 failed 回收 UPC / 判 unknown 写
    K=Unknown)之后,第二轮就没得救了。
    """
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)
    calls = {"post": 0, "get": 0}

    def handler(request):
        if request.method == "POST":
            calls["post"] += 1
            return httpx.Response(500, text="Internal Server Error - Read")
        calls["get"] += 1
        return httpx.Response(200, json={"results": {"feed": []}})

    _use(monkeypatch, handler)
    out = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t",
                            defer_settle=True)
    assert out[0]["outcome"] == "deferred" and out[0]["feed_id"] is None
    # 当场既不反查也不补交:反查留到第二轮,那时索引才追得上
    assert calls == {"post": 1, "get": 0}
    # feed_log 停在 pending —— 一条改状态的 UPDATE 都不许有(只有发送标记)
    assert _status_updates(logdb) == []
    assert len(_posting_marks(logdb)) == 1
    # 句柄够重放:载荷由 chunk 确定性重建,不扛几 MB 的 dict 过整轮
    h = out[0]["_settle"]
    assert h["chunk"] == ["A"] and h["feed_type"] == "DELETE_ITEM"
    assert h["log_id"] == 1 and h["workflow"] == "t"


def test_settle_found_adopts_without_resubmitting(monkeypatch):
    """第二轮反查到了 ⇒ 收编,**一次补交都不发**。

    生产实证 2026-08-24:那晚 4 家店就是这么救回来的 —— feed 其实建成了,
    只是响应丢在边缘。拖过第一轮之后沃尔玛的索引也追上了,FOUND 的概率
    比当场反查高得多,这正是"等整轮跑完"的头号收益。
    """
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)
    calls = {"post": 0}
    now_ms = int(time.time() * 1000)

    def handler(request):
        if request.method == "POST":
            calls["post"] += 1
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"results": {"feed": [
            {"feedId": "F_LATE", "itemsReceived": 1, "feedDate": now_ms}]}})

    _use(monkeypatch, handler)
    h = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t",
                          defer_settle=True)[0]["_settle"]
    res = feeds.settle_deferred(STORE, h)
    assert res == {"feed_id": "F_LATE", "count": 1, "outcome": "submitted"}
    assert calls["post"] == 1          # 只有第一轮那一次,第二轮零补交


def test_settle_probes_before_every_resubmit(monkeypatch):
    """**每次补交之前都必须重新反查,一次都不许省** —— 防双上架的命门。

    省掉的话,第二次补交就可能撞上第一次其实已经落地的 feed。反查本身不贵
    (feeds.get 是 3000/min 的大桶),而每省一次都在赌一次双上架。
    """
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)
    seq: list = []

    def handler(request):
        seq.append(request.method)
        if request.method == "POST":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"results": {"feed": []}})

    _use(monkeypatch, handler)
    h = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t",
                          defer_settle=True)[0]["_settle"]
    feeds.settle_deferred(STORE, h)
    # 第一轮那次 POST 之后,序列必须是 GET,GET(双确认) → POST → GET,GET → POST …
    # 断言:**任意一次 POST 之前都紧邻着 GET**(除了第一轮那次开场的)
    for i, m in enumerate(seq):
        if m == "POST" and i > 0:
            assert seq[i - 1] == "GET", f"第 {i} 次 POST 前面不是反查:{seq}"
    assert seq.count("POST") == 1 + feeds.SETTLE_ATTEMPTS


def test_settle_unknown_never_resubmits(monkeypatch):
    """反查自己就查不动(UNKNOWN)⇒ **绝不补交**,保持 pending 交 feed_poll 对账。

    宁停不重:查不动时补交,等于在"可能已经上架了"的情况下再上一次。
    """
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)
    calls = {"post": 0}

    def handler(request):
        if request.method == "POST":
            calls["post"] += 1
            return httpx.Response(500, text="boom")
        return httpx.Response(503, text="feeds 也挂了")   # 反查失败 ⇒ UNKNOWN

    _use(monkeypatch, handler)
    h = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t",
                          defer_settle=True)[0]["_settle"]
    res = feeds.settle_deferred(STORE, h)
    assert res["outcome"] == "unknown" and res["feed_id"] is None
    assert calls["post"] == 1                    # 只有第一轮那次,第二轮零补交
    # UNKNOWN 不许把 feed_log 推向终态(pending 才是对账器的入口)
    assert _status_updates(logdb) == []


def test_settle_4xx_stops_immediately(monkeypatch):
    """补交撞 4xx ⇒ 载荷/权限问题,补多少次都是同一个拒 —— 立刻收手判 failed。"""
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)
    calls = {"post": 0}

    def handler(request):
        if request.method == "POST":
            calls["post"] += 1
            return httpx.Response(500 if calls["post"] == 1 else 400,
                                  json={"error": "bad payload"})
        return httpx.Response(200, json={"results": {"feed": []}})

    _use(monkeypatch, handler)
    h = feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t",
                          defer_settle=True)[0]["_settle"]
    res = feeds.settle_deferred(STORE, h)
    assert res["outcome"] == "failed"
    assert calls["post"] == 2            # 第一轮 + 第二轮第一次,不再往下试


def test_backoff_follows_the_official_ladder_with_jitter():
    """退避走**官方阶梯 + 抖动**(2026-08-26 核验 developer.walmart.com)。

    官方对 500 的原文是「Retry with **jitter**」,阶梯示例 2/4/8/16/32。
    抖动不是我们的发挥:没有它,一批同时失败的店会在同一秒**再次同时**
    打过去,把第一次的洪峰原样复制一遍 —— 退避只平移了洪峰,没摊平它。
    """
    assert feeds._BACKOFF_LADDER == (2, 4, 8, 16, 32)
    for i, base in enumerate(feeds._BACKOFF_LADDER):
        vals = {feeds._backoff(i) for _ in range(40)}
        assert all(base * 0.5 <= v <= base for v in vals), (i, sorted(vals)[:3])
        assert len(vals) > 1, f"第 {i} 档没有抖动:{vals}"
    # 超出阶梯长度取最后一档,不越界
    assert 16 <= feeds._backoff(99) <= 32


# ── 改码载荷经过 api 层时的两条隐含契约(通道 MP_ITEM_MATCH,2026-09-06 定案)──

def _migrate_item():
    """改码载荷 = 跟卖链的同一块积木(services/match_feed.build_match_item)。"""
    from services import match_feed
    return match_feed.build_match_item(
        None, "AN3WC0DE2345", 29.99, 0.82,
        product_id="00121678236703", product_id_type="GTIN")


def test_chunk_skus_takes_the_new_code_from_a_migrate_payload():
    """改码载荷的 sku 是**新码**,台账因此按新码落账 —— sku_migrate 的回执反查与
    feed_poll 的反哺都按新码找行,**这是有意的**。

    有人把 _chunk_skus 改成取旧码的话,回执永远查不到那一行,而且不报错。
    """
    item = _migrate_item()
    assert feeds._chunk_skus("MP_ITEM_MATCH", [item]) == ["AN3WC0DE2345"]
    assert feeds._chunk_skus("MP_MAINTENANCE", [item]) == ["AN3WC0DE2345"]


def test_match_payload_wraps_migrate_items_unchanged():
    """api 层只包信封不碰内容(铁律 2):改码 Item 原样进 MPItem[{Item:…}]。

    **2026-09-07 升 v5**(v4.2 同日退役):header 换成与 MP_ITEM 同款的
    businessUnit 制**三字段封闭**,依据是官方规范原件 refdata/specs/ 那一份
    (`MPItemFeedHeader` required 三个 + additionalProperties=false)。v4.2 的
    `{processMode: REPLACE, subset, sellingChannel}` 在 v5 里是三个未知字段 ——
    REPLACE 语义没变(同 GTIN + 新 SKU 原地换码,2026-09-06 所有者实测),它只是
    不再由 header 里的一个开关表达。逐字对规范的守门在 tests/test_match_spec_v5.py。
    """
    item = _migrate_item()
    p = feeds.build_payload("MP_ITEM_MATCH", [item])
    assert p["MPItem"] == [{"Item": item}]
    assert p["MPItemFeedHeader"] == {
        "businessUnit": "WALMART_US", "locale": "en",
        "version": resources.FEED_SPEC_VERSIONS["MP_ITEM_MATCH"]}
    assert "processMode" not in json.dumps(p)   # v4.2 的 header 三件套已退役
    assert p["MPItem"][0]["Item"]["productIdentifiers"] == {
        "productIdType": "GTIN", "productId": "00121678236703"}
    assert "SkuUpdate" not in json.dumps(p)     # 通道不需要这个开关字段
    # 切片限额已登记,本批不新增 feedType
    assert feeds._SLICE_LIMITS["MP_ITEM_MATCH"] == (1000, 24_000_000)
    assert feeds._SLICE_LIMITS["MP_MAINTENANCE"] == (1000, 24_000_000)


# ── pending 对账(所有者 2026-09-25 批):认领记条数与 SKU、反查按发送时刻、SKU 集合核对 ──

def test_claim_records_the_slice_count_and_skus(monkeypatch):
    """事后反查按**条数**匹配、按 **SKU 集合**核对、收编后按 SKU 列表补落台账 ——
    这两样只能在认领那一刻记下来。"""
    logdb = _LogDB(claim=True)
    _fake_db(monkeypatch, logdb)
    _use(monkeypatch, lambda r: httpx.Response(200, json={"feedId": "F1"}))
    feeds.submit_feed(STORE, "DELETE_ITEM", ["A", "B"], workflow="t")
    sql, args = next((s, a) for s, a in logdb.sqls if "INSERT INTO ops.feed_log" in s)
    assert "item_count, skus" in sql and args[-2:] == (2, ["A", "B"])


def test_reclaim_clears_the_previous_attempts_facts(monkeypatch):
    """重占终态行:发送标记 / 对账计数 / 收口依据是上一笔的事实,一并清空。"""
    logdb = _LogDB(claim=False, prev=(9, "failed", None))
    _fake_db(monkeypatch, logdb)
    _use(monkeypatch, lambda r: httpx.Response(200, json={"feedId": "F2"}))
    feeds.submit_feed(STORE, "DELETE_ITEM", ["A"], workflow="t")
    sql, args = next((s, a) for s, a in _updates(logdb) if "'pending'" in s)
    for frag in ("post_started_at = NULL", "recon_count = 0", "close_basis = NULL",
                 "item_count = %s", "skus = %s"):
        assert frag in sql
    assert args[1:3] == (1, ["A"])


def _list_and_details(pages: list[list[dict]], details: dict[str, list[str]],
                      seen: dict):
    from urllib.parse import unquote

    def handler(request):
        path = request.url.path
        if path == "/v3/feeds":
            off = int(request.url.params.get("offset", "0"))
            seen.setdefault("offsets", []).append(off)
            page = pages[off // 50] if off // 50 < len(pages) else []
            return httpx.Response(200, json={"results": {"feed": page}})
        fid = unquote(path.rsplit("/", 1)[1])
        skus = details.get(fid, [])
        return httpx.Response(200, json={"itemsReceived": len(skus), "itemDetails": {
            "itemIngestionStatus": [{"sku": s, "ingestionStatus": "SUCCESS"}
                                    for s in skus]}})
    return handler


def test_reconcile_lookup_pages_and_only_adopts_a_matching_sku_set(monkeypatch):
    """事后对账:按发送时刻开窗、往后翻页;同条数的候选必须过 SKU 集合核对 ——
    官方列表参数里没有 feedType,返回的 feedType 写法也核实不了,只按条数认会把
    同店同条数的别类 feed 收编过来。"""
    from datetime import datetime, timedelta, timezone
    _fake_db(monkeypatch, _LogDB(claim=True))
    since = datetime.now(timezone.utc) - timedelta(hours=3)
    t_ms = int(since.timestamp() * 1000) + 60_000
    pages = [[{"feedId": f"X{i}", "itemsReceived": 99, "feedDate": t_ms}
              for i in range(50)],
             [{"feedId": "F_OTHER", "itemsReceived": 2, "feedDate": t_ms},
              {"feedId": "F_LATE", "itemsReceived": 2,
               "feedDate": t_ms + 3 * 3600_000},                 # 窗口外
              {"feedId": "F_OURS", "itemsReceived": 2, "feedDate": t_ms}]]
    seen: dict = {}
    _use(monkeypatch, _list_and_details(
        pages, {"F_OTHER": ["X", "Y"], "F_LATE": ["A", "B"], "F_OURS": ["A", "B"]},
        seen))
    v, f = feeds.find_recent_feed(STORE, "DELETE_ITEM", 2, since=since,
                                  expect_skus=["B", "A"], recheck=False)
    assert (v, f["feedId"]) == ("FOUND", "F_OURS")
    assert seen["offsets"] == [0, 50]                     # 翻了第二页


def test_reconcile_lookup_waits_when_the_candidate_cannot_be_verified_yet(monkeypatch):
    """候选的明细还读不到(刚受理)⇒ 这一轮核不了,UNKNOWN,下轮再核 —— 不当没找到。"""
    from datetime import datetime, timedelta, timezone
    _fake_db(monkeypatch, _LogDB(claim=True))
    since = datetime.now(timezone.utc) - timedelta(minutes=40)
    t_ms = int(since.timestamp() * 1000)
    _use(monkeypatch, _list_and_details(
        [[{"feedId": "F_NEW", "itemsReceived": 2, "feedDate": t_ms}]], {}, {}))
    assert feeds.find_recent_feed(STORE, "DELETE_ITEM", 2, since=since,
                                  expect_skus=["A", "B"], recheck=False)[0] == "UNKNOWN"


def test_reconcile_lookup_without_a_match_is_not_found_without_the_30s_recheck(
        monkeypatch):
    from datetime import datetime, timedelta, timezone
    _fake_db(monkeypatch, _LogDB(claim=True))
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    seen: dict = {}
    _use(monkeypatch, _list_and_details([[]], {}, seen))
    since = datetime.now(timezone.utc) - timedelta(hours=2)
    assert feeds.find_recent_feed(STORE, "DELETE_ITEM", 2, since=since,
                                  expect_skus=["A", "B"], recheck=False) == ("NOT_FOUND", None)
    assert slept == [] and seen["offsets"] == [0]


def test_reconcile_lookup_excludes_feeds_recorded_under_any_feed_type(monkeypatch):
    """同店**别类** feed 已记账(同一批 SKU 的改库存 / 先停用后删除)⇒ 不是"刚才那笔"。
    此前只排除同类 feedId,别类的条数与 SKU 集合都对得上,会被错收编。"""
    from datetime import datetime, timedelta, timezone

    class _DB(_LogDB):
        def fetchall(self):
            if "SELECT feed_id FROM ops.feed_log" in self._last:
                return [("F_INVENTORY",)]
            return []
    logdb = _DB(claim=True)
    _fake_db(monkeypatch, logdb)
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    t_ms = int(since.timestamp() * 1000)
    _use(monkeypatch, _list_and_details(
        [[{"feedId": "F_INVENTORY", "itemsReceived": 2, "feedDate": t_ms}]],
        {"F_INVENTORY": ["A", "B"]}, {}))
    assert feeds.find_recent_feed(STORE, "price", 2, since=since, expect_skus=["A", "B"],
                                  recheck=False) == ("NOT_FOUND", None)
    sql, args = next((q, a) for q, a in logdb.sqls if "SELECT feed_id FROM ops.feed_log" in q)
    assert "feed_type" not in sql and args == (STORE["name"],)   # 按店排除,不分类


def test_reconcile_lookup_two_matching_candidates_is_undecided(monkeypatch):
    """两条候选条数与 SKU 集合都对得上(都没记账)⇒ 分不清是哪一笔,不收编,候选带回。"""
    from datetime import datetime, timedelta, timezone
    _fake_db(monkeypatch, _LogDB(claim=True))
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    t_ms = int(since.timestamp() * 1000)
    _use(monkeypatch, _list_and_details(
        [[{"feedId": "F1", "itemsReceived": 2, "feedDate": t_ms},
          {"feedId": "F2", "itemsReceived": 2, "feedDate": t_ms + 60_000}]],
        {"F1": ["A", "B"], "F2": ["B", "A"]}, {}))
    assert feeds.find_recent_feed(STORE, "price", 2, since=since, expect_skus=["A", "B"],
                                  recheck=False) == (
        "UNKNOWN", {"matched": ["F1", "F2"], "unverified": []})


def test_reconcile_lookup_partial_details_are_undecided_not_rejected(monkeypatch):
    """候选明细只露出本片一部分(还在处理 / 沃尔玛漏条)⇒ 核不了,不当"不是这一笔"
    (否则到期就把其实到了的 feed 判成提交未确认);有它在,另一条对得上的也先不收编。"""
    from datetime import datetime, timedelta, timezone
    _fake_db(monkeypatch, _LogDB(claim=True))
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    t_ms = int(since.timestamp() * 1000)
    page = [{"feedId": "F_PART", "itemsReceived": 2, "feedDate": t_ms}]
    details = {"F_PART": ["A"]}
    _use(monkeypatch, _list_and_details([page], details, {}))
    assert feeds.find_recent_feed(STORE, "price", 2, since=since, expect_skus=["A", "B"],
                                  recheck=False) == (
        "UNKNOWN", {"matched": [], "unverified": ["F_PART"]})
    # 同一个假接口(连接池会复用第一次的 transport),原地加一条完全对得上的
    page.append({"feedId": "F_FULL", "itemsReceived": 2, "feedDate": t_ms})
    details["F_FULL"] = ["A", "B"]
    assert feeds.find_recent_feed(STORE, "price", 2, since=since, expect_skus=["A", "B"],
                                  recheck=False) == (
        "UNKNOWN", {"matched": ["F_FULL"], "unverified": ["F_PART"]})


class _Returning(_LogDB):
    """UPDATE … RETURNING 取回一行(行仍是 pending 时)。"""

    def __init__(self, hit=True):
        super().__init__()
        self.hit = hit

    def fetchone(self):
        if "RETURNING id" in self._last:
            return (7,) if self.hit else None
        return super().fetchone()


def test_adopt_pending_lands_both_ledgers_in_one_transaction(monkeypatch):
    from datetime import datetime, timezone
    db_ = _Returning()
    _fake_db(monkeypatch, db_)
    posted = datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc)
    row = {"id": 7, "workflow": "t", "store": "T1", "feed_type": "DELETE_ITEM",
           "skus": ["A", "B"]}
    assert feeds.adopt_pending(row, "F_OURS", posted) is True
    upd, args = next((s, a) for s, a in db_.sqls if "SET status = 'submitted'" in s)
    assert "AND status = 'pending'" in upd and args == ("F_OURS", posted, 7)
    ins, rows = next((s, a) for s, a in db_.sqls if "INSERT INTO ops.feed_items" in s)
    assert rows == [("F_OURS", "A", "t", "T1", "DELETE_ITEM", posted),
                    ("F_OURS", "B", "t", "T1", "DELETE_ITEM", posted)]
    miss = _Returning(hit=False)
    _fake_db(monkeypatch, miss)
    assert feeds.adopt_pending(row, "F_OURS", posted) is False       # 已不是 pending
    assert not any("feed_items" in s for s, _ in miss.sqls)


def test_close_pending_only_touches_pending_rows(monkeypatch):
    db_ = _Returning()
    _fake_db(monkeypatch, db_)
    assert feeds.close_pending(7, "提交未确认:…") is True
    sql, args = db_.sqls[-1]
    assert "status = 'failed'" in sql and "AND status = 'pending'" in sql
    assert args == ("提交未确认:…", 7)
