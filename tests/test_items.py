"""api/items.py 与限速桶回归:三端点适配、SPEC 双位置提取、双命名兜底、未登记桶拒绝。"""

import time

import httpx
import pytest

from api import _client, items

STORE = {"name": "T1", "client_id": "cid_test", "client_secret": "sec", "proxy": None}


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


def test_search_walmart_default(monkeypatch):
    def handler(request):
        assert request.url.path == "/v3/items/walmart/search"
        assert request.url.params["upc"] == "036000291452"
        return httpx.Response(200, json={"items": [
            {"itemId": 123, "title": "T", "brand": "B", "price": {"amount": 9.99, "currency": "USD"},
             "properties": {"num_reviews": 7, "categories": ["Home", "Cups"]}}]})

    _use(monkeypatch, handler)
    hits = items.search_walmart(STORE, upc="036000291452")
    s = items.summarize_search_item(hits[0])
    assert s["item_id"] == 123 and s["price"] == 9.99
    assert s["reviews"] == 7                      # snake_case 兜底命中
    assert s["categories"] == "Home > Cups"


def test_search_walmart_requires_exactly_one_param():
    with pytest.raises(ValueError):
        items.search_walmart(STORE)
    with pytest.raises(ValueError):
        items.search_walmart(STORE, query="a", upc="1")


def test_spec_match_extracts_from_item_position(monkeypatch):
    def handler(request):
        assert request.url.params["responseFormat"] == "SPEC"
        return httpx.Response(200, json={"items": [{
            "feedType": "MP_ITEM_MATCH", "productType": "Cups",
            "itemSpecPayload": {"MPItem": [{
                "Item": {"productIdentifiers": {"productId": "00036000291452",
                                                "productIdType": "GTIN"}},
                "Visible": {"Cups": {"productName": "Great Cup"}}}]},
            "externalProductIdentifier": [
                {"externalProductIdType": "ASIN", "externalProductId": "B0AAAAAAAA"}]}]})

    _use(monkeypatch, handler)
    spec = items.search_walmart_spec(STORE, gtin="00036000291452")
    assert spec["feed_type"] == "MP_ITEM_MATCH"
    assert spec["product_id"] == "00036000291452" and spec["product_id_type"] == "GTIN"
    assert spec["asin"] == "B0AAAAAAAA" and spec["title"] == "Great Cup"


def test_spec_mp_item_extracts_from_orderable_position(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"items": [{
            "feedType": "MP_ITEM",
            "itemSpecPayload": {"MPItem": [{
                "Orderable": {"productIdentifiers": {"productId": "036000291452",
                                                     "productIdType": "UPC"}}}]}}]})

    _use(monkeypatch, handler)
    spec = items.search_walmart_spec(STORE, upc="036000291452")
    assert spec["feed_type"] == "MP_ITEM" and spec["product_id_type"] == "UPC"


def test_spec_empty_means_not_in_catalog(monkeypatch):
    _use(monkeypatch, lambda r: httpx.Response(200, json={"items": []}))
    spec = items.search_walmart_spec(STORE, asin="B0AAAAAAAA")
    assert spec["feed_type"] is None and spec["raw"] is None


def test_catalog_search_rejects_item_id_and_reads_payload(monkeypatch):
    with pytest.raises(ValueError):
        items.catalog_search(STORE, "itemId", "123")

    def handler(request):
        import json
        assert json.loads(request.content) == {"query": {"field": "sku", "value": "SKU1"}}
        return httpx.Response(200, json={"payload": [{"itemId": 1, "productName": "P"}]})

    _use(monkeypatch, handler)
    hits = items.catalog_search(STORE, "sku", "SKU1")
    assert hits[0]["productName"] == "P"


def test_rate_bucket_unregistered_rejected():
    with pytest.raises(KeyError):
        _client.rate_acquire("no.such.bucket", "cid")


def test_rate_bucket_blocks_when_full(monkeypatch):
    _client._RATE_BUCKETS["test.tiny"] = (2, 60.0)
    slept = []

    def fake_sleep(s):
        # 模拟等待期间最早一次调用滑出窗口(真实场景是时间流逝,这里直接清一个名额)
        slept.append(s)
        with _client._rate_lock:
            _client._rate_state[("cidX", "test.tiny")].popleft()

    monkeypatch.setattr(time, "sleep", fake_sleep)
    try:
        assert _client.rate_acquire("test.tiny", "cidX") == 0.0
        assert _client.rate_acquire("test.tiny", "cidX") == 0.0
        waited = _client.rate_acquire("test.tiny", "cidX")  # 第三次:窗口满,须先等待
        assert slept and slept[0] > 0
        assert waited == slept[0]
    finally:
        _client._RATE_BUCKETS.pop("test.tiny", None)


def test_workflow_classify_and_candidates():
    from workflows import product_query as pq
    assert pq._classify("B0ABCD1234") == "asin"
    assert pq._classify("036000291452") == "code"
    assert pq._classify("stainless cup") == "keyword"
    # 11 位(丢前导 0)→ 先补 12 位 upc 再补 14 位 gtin
    assert pq._code_candidates("36000291452") == [
        ("upc", "036000291452"), ("gtin", "00036000291452")]
    assert pq._code_candidates("036000291452")[0] == ("upc", "036000291452")
