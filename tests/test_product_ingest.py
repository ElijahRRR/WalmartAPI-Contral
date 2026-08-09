"""采集增量摄取回归:契约状态码分流 + 两层落库三条硬规则 + provider 读库。"""

import json

import httpx
import pytest

from api import scraper
from services import amz_source, product_ingest as ingest
from tests.test_list_new import _sheet_row


def _rec(**kw):
    base = {
        "source_id": "sid-1", "cursor": 1, "marketplace": "US",
        "asin": "B0TEST0001", "scraped_at": "2026-08-08T10:00:00Z",
        "scrape_params": {"zipcode": "10001", "zip_verify": "confirmed"},
        "slow": {"title": "T", "brand": "B",
                 "category_path": ["Home", "Tools"],
                 "images": ["https://x/2.jpg", "https://x/1.jpg"]},
        "fast": {"price": 19.99, "currency": "USD", "stock_state": "in_stock"},
        "slow_hash": "3471dc8c36e2d028", "outcome": "ok",
        "completeness_ok": True,
    }
    base.update(kw)
    return base


# ── api/scraper:状态码分流(每条都对应契约里的一句话)────────────────────

def _patch_http(monkeypatch, responses):
    """responses: [(status, json_or_text)] 依次返回。"""
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        status, body = responses[i]
        content = json.dumps(body) if isinstance(body, dict) else str(body)
        return httpx.Response(status, content=content,
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setenv("SCRAPER_BASE_URL", "http://127.0.0.1:8899")
    return calls


def test_export_ok_and_empty_page_does_not_advance(monkeypatch):
    _patch_http(monkeypatch, [(200, {"contract_version": 1, "records": [],
                                     "next_cursor": 77, "has_more": False})])
    records, nxt, more = scraper.export_incremental(77)
    assert records == [] and nxt == 77 and more is False   # 原样不推进


def test_export_terminal_codes_do_not_retry(monkeypatch):
    calls = _patch_http(monkeypatch, [(401, {"error": "invalid_export_token"})])
    with pytest.raises(scraper.ExportAuthError):
        scraper.export_incremental(0)
    assert calls["n"] == 1

    calls = _patch_http(monkeypatch, [(409, {"error": "cursor_below_retention"})])
    with pytest.raises(scraper.RetentionGapError):
        scraper.export_incremental(5)
    assert calls["n"] == 1

    calls = _patch_http(monkeypatch, [(422, {"error": "invalid_parameter"})])
    with pytest.raises(ValueError):
        scraper.export_incremental(0)
    assert calls["n"] == 1


def test_export_404_batch_not_found_is_not_empty_data(monkeypatch):
    """404「批次不存在」= 路由打歪,必须报错而不是被读成暂无数据。"""
    _patch_http(monkeypatch, [(404, {"detail": "批次不存在: incremental"})])
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="404"):
        scraper.export_incremental(0, max_retries=2)


def test_export_limit_guard(monkeypatch):
    monkeypatch.setenv("SCRAPER_BASE_URL", "http://127.0.0.1:8899")
    with pytest.raises(ValueError):
        scraper.export_incremental(0, limit=1001)


# ── services/product_ingest:三条硬规则 ──────────────────────────────────

class _FakeCur:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        if "catalog.snapshots" in sql:
            sid = params["source_id"]
            self.rowcount = 0 if sid in self.store["snap"] else 1
            self.store["snap"].add(sid)
        else:
            self.store["prod"].append(params)
            self.rowcount = 1


class _FakeConn:
    def __init__(self):
        self.store = {"snap": set(), "prod": []}

    def cursor(self):
        return _FakeCur(self.store)


def test_ingest_outcome_not_ok_skips_products():
    conn = _FakeConn()
    counts = ingest.ingest_batch(conn, [
        _rec(),                                              # ok → 两层
        _rec(source_id="sid-2", outcome="blocked"),           # 只进观测
        _rec(source_id="sid-3", outcome="parse_failed"),
    ])
    assert counts["snapshots"] == 3 and counts["products"] == 1
    assert counts["skipped_outcome"] == 2


def test_ingest_dedup_by_source_id():
    conn = _FakeConn()
    ingest.ingest_batch(conn, [_rec()])
    counts = ingest.ingest_batch(conn, [_rec()])      # 同 source_id 重复推送
    assert counts["snapshots"] == 0 and counts["dup"] == 1


def test_ingest_blank_values_normalized_to_none():
    """规则 2:空值一律 None,交给 SQL 的 COALESCE 保住旧值。"""
    p = ingest.product_params(_rec(slow={"title": "  ", "brand": None,
                                         "category_path": [], "images": []}))
    assert p["title"] is None and p["brand"] is None
    assert p["amazon_category"] is None and p["image_url"] is None
    # 正常值:类目拼串、首图=主图
    p2 = ingest.product_params(_rec())
    assert p2["amazon_category"] == "Home > Tools"
    assert p2["image_url"] == "https://x/2.jpg"      # 契约:images 首图即主图


def test_ingest_invalid_records_dropped():
    conn = _FakeConn()
    counts = ingest.ingest_batch(conn, [{"asin": "B0X"}, {"source_id": "s"}])
    assert counts["invalid"] == 2 and counts["snapshots"] == 0


def test_snapshot_params_shape():
    s = ingest.snapshot_params(_rec(fast={"price": 5.5, "stock_state": "unknown",
                                          "buybox_price": 6.0,
                                          "buybox_seller": "X"}))
    assert s["price"] == 5.5 and s["stock_state"] == "unknown"
    assert json.loads(s["buybox"])["buybox_seller"] == "X"
    assert json.loads(s["scrape_params"])["zipcode"] == "10001"


def test_stock_count_null_and_zero_are_different():
    """契约 3b:null=没采到,0=确实缺货。两者绝不可互相折叠。"""
    absent = ingest.snapshot_params(_rec(fast={"price": 1, "stock_state": "unknown"}))
    assert absent["stock_count"] is None and absent["delivery_days"] is None

    zero = ingest.snapshot_params(_rec(fast={"price": 1, "stock_state": "out_of_stock",
                                             "stock_count": 0, "delivery_days": 0}))
    assert zero["stock_count"] == 0 and zero["delivery_days"] == 0

    real = ingest.snapshot_params(_rec(fast={"price": 1, "stock_state": "in_stock",
                                             "stock_count": 37, "delivery_days": 8}))
    assert real["stock_count"] == 37 and real["delivery_days"] == 8
    # 脏值按"没采到"处理,绝不当 0
    dirty = ingest.snapshot_params(_rec(fast={"stock_count": "N/A"}))
    assert dirty["stock_count"] is None


# ── services/amz_source:读中心库(不直连采集器)──────────────────────────

def test_fetch_products_carries_true_values(monkeypatch):
    """provider 只搬运真值:stock/lead_days 的 None 原样透出,不 or 0。"""
    rows = [
        # asin, title, brand, category, image, price, state, count, days, raw
        ("B0A", "标题A", "BrandA", "Home > Tools", "https://x/1.jpg", 19.99,
         "in_stock", 37, 8,
         {"slow": {"images": ["https://x/2.jpg", "https://x/1.jpg"],
                   "bullet_points": ["a"]}}),
        ("B0B", "标题B", None, None, None, 8.0, "out_of_stock", 0, None, None),
        ("B0C", "标题C", None, None, None, 8.0, "in_stock", None, None, None),
        ("B0D", None, None, None, None, 8.0, "in_stock", 5, 3, None),  # 无标题不够格
    ]

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): self.sql = sql
        def fetchall(self): return rows

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _C()

    monkeypatch.setattr(amz_source.db, "pg_conn", lambda: _Conn())
    out = amz_source.fetch_products(["B0A", "B0B", "B0C", "B0D"])
    assert set(out) == {"B0A", "B0B", "B0C"}          # 无标题的被剔除
    assert out["B0A"]["stock"] == 37 and out["B0A"]["lead_days"] == 8
    assert out["B0A"]["images"] == ["https://x/1.jpg", "https://x/2.jpg"]  # 字典序
    assert out["B0A"]["attrs"]["bullet_points"] == ["a"]
    assert out["B0B"]["stock"] == 0                   # 确实缺货,不是 None
    assert out["B0C"]["stock"] is None                # 没采到,不是 0
    assert out["B0C"]["stock_state"] == "in_stock"    # 状态给调用方兜底判断
    assert out["B0A"]["price"] == 19.99
    assert amz_source.fetch_products([]) == {}


def test_list_new_stock_three_way(monkeypatch):
    """list_new 的三态处理:真值走闸 / 没采到+有货按常量 / 其余不上架。"""
    from workflows import list_new as ln

    rows = [_sheet_row(2, asin="B0REAL"), _sheet_row(3, asin="B0LOW"),
            _sheet_row(4, asin="B0ZERO"), _sheet_row(5, asin="B0NULLOK"),
            _sheet_row(6, asin="B0NULLUNK"), _sheet_row(7, asin="B0SLOW")]
    products = {
        "B0REAL":    {"asin": "B0REAL", "title": "T", "price": 20.0,
                      "stock": 37, "stock_state": "in_stock", "lead_days": 8},
        "B0LOW":     {"asin": "B0LOW", "title": "T", "price": 20.0,
                      "stock": 3, "stock_state": "in_stock", "lead_days": 2},
        "B0ZERO":    {"asin": "B0ZERO", "title": "T", "price": 20.0,
                      "stock": 0, "stock_state": "out_of_stock", "lead_days": 2},
        "B0NULLOK":  {"asin": "B0NULLOK", "title": "T", "price": 20.0,
                      "stock": None, "stock_state": "in_stock", "lead_days": None},
        "B0NULLUNK": {"asin": "B0NULLUNK", "title": "T", "price": 20.0,
                      "stock": None, "stock_state": "unknown", "lead_days": None},
        "B0SLOW":    {"asin": "B0SLOW", "title": "T", "price": 20.0,
                      "stock": 50, "stock_state": "in_stock", "lead_days": 30},
    }
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "_load_gate_state", lambda: (
        set(), {}, set(), set(), {"banned_pts": set(), "brands": set()}))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln, "_load_multipliers", lambda: {})
    monkeypatch.setattr(ln.stores_svc, "load_stores", lambda names=None: [{"name": "T1"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: products)
    monkeypatch.setattr(ln.pricing, "walmart_price", lambda ch, price, m: 99.0)

    out = ln.run({"execute": False})
    # 真值 37 过闸;3 <5 拦;0 拦(确实缺货);None+in_stock 按常量铺货;
    # None+unknown 拦(不知道有没有货);50 但 30 天 → 上架但库存 0
    assert "数据过滤 3" in out
    assert f"库存数未采到按 {amz_source.IN_STOCK_QTY} 铺货 1 行" in out
    assert "库存不足:3" in out and "库存不足:0" in out
    assert "库存未知(状态 unknown)" in out
    assert "库存 0 待提交" in out          # B0SLOW:配送超时,上架但清零
    assert "共 3 行将进入" in out


def test_partner_id_reads_nested_shape(monkeypatch):
    """partnerprofile 真实结构是 partner.partnerId(2026-08-09 生产实证);
    绝不能取成 partnerStoreId——它不是 fulfillmentCenterID。"""
    from api import settings as settings_api

    real = {"partner": {"partnerId": "10002782678",
                        "partnerDisplayName": "ZenithNode",
                        "partnerStoreId": "102763209"},
            "configurations": [{"configurationName": "ACCOUNT",
                                "configuration": {"status": "ACTIVE"}}]}
    monkeypatch.setattr(settings_api._client, "rate_acquire", lambda *a: None)
    monkeypatch.setattr(settings_api._client, "get_token", lambda *a: "tok")
    monkeypatch.setattr(settings_api._client, "base_url", lambda: "https://x")
    monkeypatch.setattr(settings_api._client, "safe_get_ex",
                        lambda *a, **k: (200, {}, real))
    settings_api._cached_partner_id.cache_clear()
    assert settings_api.get_partner_id(
        {"client_id": "c1", "client_secret": "s", "proxy": None}) == "10002782678"
