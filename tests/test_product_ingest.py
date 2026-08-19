"""采集增量摄取回归:契约状态码分流 + 两层落库三条硬规则 + provider 读库。"""

import json

import httpx
import pytest

from api import scraper
from services import amz_source, product_ingest as ingest
from tests.test_list_new import _sheet_row, fake_verdicts


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


# ── api/scraper.export_batch_records:批次端点状态码分流(契约 §4.11)────────

def test_batch_export_ok_carries_meta(monkeypatch):
    """200:records/next_cursor/has_more 同增量端点,meta 透传 coverage 等。"""
    _patch_http(monkeypatch, [(200, {
        "contract_version": 1, "records": [{"asin": "B0A"}],
        "next_cursor": 34567, "has_more": False,
        "batch": {"id": 12, "name": "wm-audit-x", "status": "completed"},
        "coverage": {"asin_total": 500, "asin_with_event": 498},
        "retention_min_cursor": 676, "max_cursor": 34567})])
    records, nxt, more, meta = scraper.export_batch_records("wm-audit-x")
    assert records == [{"asin": "B0A"}] and nxt == 34567 and more is False
    assert meta["coverage"] == {"asin_total": 500, "asin_with_event": 498}
    assert meta["batch"]["status"] == "completed"


def test_batch_export_404_batch_not_found_is_terminal(monkeypatch):
    """本端点的 404 唯一含义:批次名不存在 → LookupError,不重试。"""
    calls = _patch_http(monkeypatch,
                        [(404, {"error": "batch_not_found",
                                "detail": "批次不存在: wm-audit-x"})])
    with pytest.raises(LookupError, match="批次不存在"):
        scraper.export_batch_records("wm-audit-x")
    assert calls["n"] == 1


def test_batch_export_alien_404_is_not_batch_gone(monkeypatch):
    """长得不像 batch_not_found 的 404(采集侧还是旧版本、端点没挂)→
    按瞬时故障重试报错,**绝不当「批次没了」**——那会让调用方把一批
    要得回来的数据判成要不回来。"""
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    _patch_http(monkeypatch, [(404, {"detail": "Not Found"})])
    with pytest.raises(RuntimeError, match="未部署"):
        scraper.export_batch_records("wm-audit-x", max_retries=2)


def test_batch_export_terminal_codes_do_not_retry(monkeypatch):
    calls = _patch_http(monkeypatch, [(401, {"error": "invalid_export_token"})])
    with pytest.raises(scraper.ExportAuthError):
        scraper.export_batch_records("wm-audit-x")
    assert calls["n"] == 1

    calls = _patch_http(monkeypatch, [(422, {"error": "invalid_parameter"})])
    with pytest.raises(ValueError):
        scraper.export_batch_records("wm-audit-x")
    assert calls["n"] == 1

    monkeypatch.setenv("SCRAPER_BASE_URL", "http://127.0.0.1:8899")
    with pytest.raises(ValueError):
        scraper.export_batch_records("wm-audit-x", limit=1001)
    with pytest.raises(ValueError):
        scraper.export_batch_records("")


def test_batch_export_503_retries_then_succeeds(monkeypatch):
    """503(事件流暂不可用)是瞬时故障:退避重试,好了就正常返回。"""
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    calls = _patch_http(monkeypatch, [
        (503, {"error": "event_stream_unavailable"}),
        (200, {"records": [], "next_cursor": 0, "has_more": False})])
    records, _nxt, more, _meta = scraper.export_batch_records("wm-audit-x")
    assert records == [] and more is False and calls["n"] == 2


# ── services/product_ingest:三条硬规则 ──────────────────────────────────

class _FakeCur:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def fetchone(self):
        # _PRODUCT_SQL 的 RETURNING (xmax=0):假实现按"首见 ASIN=新插入"模拟
        return (self._last_inserted,)

    def executemany(self, sql, rows):
        # product_ingested 事件批量写(product_events.record_many)
        self.store.setdefault("events", []).extend(rows)

    def execute(self, sql, params=None):
        if "catalog.snapshots" in sql:
            sid = params["source_id"]
            self.rowcount = 0 if sid in self.store["snap"] else 1
            self.store["snap"].add(sid)
        else:
            seen = {p["asin"] for p in self.store["prod"]}
            self._last_inserted = params["asin"] not in seen
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
        # asin, title, brand, category, image, slow, price, state, count,
        # days, raw, fulfillment, shipping
        ("B0A", "标题A", "BrandA", "Home > Tools", "https://x/1.jpg",
         {"images": ["https://x/2.jpg", "https://x/1.jpg"],
          "bullet_points": ["a"]},                      # 身份层 slow 全量段
         19.99, "in_stock", 37, 8, None, "FBA", 0.0),
        ("B0B", "标题B", None, None, None, None, 8.0, "out_of_stock", 0,
         None, None, "fbm", 2.5),
        ("B0C", "标题C", None, None, None, None, 8.0, "in_stock", None,
         None, None, None, None),
        ("B0D", None, None, None, None, None, 8.0, "in_stock", 5, 3, None,
         None, None),
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
    # 保序去重(2026-08-12 旧仓对照:主图=亚马逊原序第一张)
    assert set(out["B0A"]["images"]) == {"https://x/1.jpg", "https://x/2.jpg"}
    assert out["B0A"]["attrs"]["bullet_points"] == ["a"]   # 来自 products.slow
    assert out["B0B"]["stock"] == 0                   # 确实缺货,不是 None
    assert out["B0C"]["stock"] is None                # 没采到,不是 0
    assert out["B0C"]["stock_state"] == "in_stock"    # 状态给调用方兜底判断
    assert out["B0A"]["price"] == 19.99
    # 配送方式来自 raw.is_fba,归一化大写;采不到就是 None(调用方不许猜)
    assert out["B0A"]["channel"] == "FBA" and out["B0B"]["channel"] == "FBM"
    assert out["B0C"]["channel"] is None
    assert amz_source.fetch_products([]) == {}


def test_list_new_stock_three_way(monkeypatch):
    """list_new 的三态处理:真值走闸 / 没采到+有货按常量 / 其余不上架。"""
    from workflows import list_new as ln

    rows = [_sheet_row(2, asin="B0REAL"), _sheet_row(3, asin="B0LOW"),
            _sheet_row(4, asin="B0ZERO"), _sheet_row(5, asin="B0NULLOK"),
            _sheet_row(6, asin="B0NULLUNK"), _sheet_row(7, asin="B0SLOW")]
    products = {
        "B0REAL":    {"asin": "B0REAL", "title": "T", "price": 20.0,
                      "stock": 37, "stock_state": "in_stock", "lead_days": 8,
                      "channel": "FBM", "shipping": 0.0},
        "B0LOW":     {"asin": "B0LOW", "title": "T", "price": 20.0,
                      "stock": 3, "stock_state": "in_stock", "lead_days": 2,
                      "channel": "FBM", "shipping": 0.0},
        "B0ZERO":    {"asin": "B0ZERO", "title": "T", "price": 20.0,
                      "stock": 0, "stock_state": "out_of_stock", "lead_days": 2,
                      "channel": "FBM", "shipping": 0.0},
        "B0NULLOK":  {"asin": "B0NULLOK", "title": "T", "price": 20.0,
                      "stock": None, "stock_state": "in_stock",
                      "lead_days": None, "channel": "FBA", "shipping": 0.0},
        "B0NULLUNK": {"asin": "B0NULLUNK", "title": "T", "price": 20.0,
                      "stock": None, "stock_state": "unknown",
                      "lead_days": None, "channel": "FBM", "shipping": 0.0},
        "B0SLOW":    {"asin": "B0SLOW", "title": "T", "price": 20.0,
                      "stock": 50, "stock_state": "in_stock", "lead_days": 30,
                      "channel": "FBM", "shipping": 0.0},
    }
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: fake_verdicts(rows))
    monkeypatch.setattr(ln, "_load_gate_state", lambda: (
        set(), {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {}))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln, "_load_multipliers", lambda: {})
    monkeypatch.setattr(ln.stores_svc, "load_stores", lambda names=None: [{"name": "T1"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: products)
    monkeypatch.setattr(ln.pricing, "walmart_price",
                        lambda ch, price, m, ship: 99.0)

    monkeypatch.setattr(ln.store_limits, "lead_day_caps", lambda: {})
    out = ln.run({"execute": False})
    # 真值 37 过闸;3 <5 拦;0 拦(确实缺货);None+in_stock 按常量铺货;
    # None+unknown 拦(不知道有没有货);
    # ⚠ 50 但 30 天 → **不上架**(所有者定稿 2026-08-16 走进生产改口径:
    #   此前是"上架但库存写 0";不上架就不占 UPC、不占配额,比上一个卖不动的更省)
    assert "数据过滤 3" in out
    assert f"库存数未采到按 {amz_source.IN_STOCK_QTY} 铺货 1 行" in out
    assert "库存不足:3" in out and "库存不足:0" in out
    assert "库存未知(状态 unknown)" in out
    # ⚠ 2026-08-16 合并后**两条改动叠加**:全局上限 8 → 7(main),
    # 且超限从"上架但清零"改成"不上架"(走进生产批次二)。于是 8 天那行
    # 也被拦了 —— 超时 2 行,只剩 1 行进入提交
    assert "配送超时 2" in out                       # 闸门行里单独计数
    assert "配送 8 天 > 本店上限 7 天" in out         # 8 天那行:全局收紧后也超
    assert "配送 30 天 > 本店上限 7 天" in out        # 理由写清是哪个上限
    assert "库存 0 待提交" not in out                # 不再有"上架但清零"这一档
    assert "共 1 行将进入" in out


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


def test_slow_segment_stored_whole():
    """slow 段全量留存:契约的 raw 是裁剪过的,卖点/重量只在 slow 里。"""
    p = ingest.product_params(_rec(slow={
        "title": "T", "brand": "B", "images": ["u1"],
        "bullet_points": ["卖点一", "卖点二"],
        "weight": {"package": 3.5, "item": 3.0},
        "variant": {"parent_asin": "B0PARENT"}}))
    stored = json.loads(p["slow"])
    assert stored["bullet_points"] == ["卖点一", "卖点二"]
    assert stored["weight"]["package"] == 3.5
    assert stored["variant"]["parent_asin"] == "B0PARENT"
    assert ingest.product_params(_rec(slow={}))["slow"] is None


# ── 采集推送(product_refresh)────────────────────────────────────────────

def test_submit_batch_txt_and_conflict(monkeypatch):
    """v4 语义:200=新建(inserted 无歧义);409=上次已成功,带既有 batch_id。"""
    sent = {}

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        sent["url"] = url
        sent["body"] = files["file"][1].decode()
        sent["data"] = data
        return httpx.Response(200, json={"batch_id": "B1", "inserted": 3},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("SCRAPER_BASE_URL", "http://127.0.0.1:8899")
    out = scraper.submit_batch("wm-refresh-1", ["B0A", "B0B", "B0C"])
    assert out["batch_id"] == "B1" and out["inserted"] == 3
    assert sent["body"] == "B0A\nB0B\nB0C"          # txt 每行一个
    assert sent["data"]["needs_screenshot"] == "false"   # 不截图=最高吞吐
    assert "zip_code" not in sent["data"]                # 不切邮编

    def conflict(url, files=None, data=None, headers=None, timeout=None):
        return httpx.Response(409, json={"detail": {"batch_id": "OLD",
                                                    "batch_name": "wm-refresh-1"}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", conflict)
    with pytest.raises(scraper.BatchExistsError) as ei:
        scraper.submit_batch("wm-refresh-1", ["B0A"])
    assert ei.value.batch_id == "OLD"       # 拿既有批次接着轮询,不重复建批

    with pytest.raises(ValueError):
        scraper.submit_batch("x", [])


def test_snapshot_carries_outcome_and_completeness():
    """采集结局随观测落库:没有它,'这个 ASIN 为什么没数据'查不了。"""
    s = ingest.snapshot_params(_rec(outcome="blocked", completeness_ok=False))
    assert s["outcome"] == "blocked" and s["completeness_ok"] is False
    assert ingest.snapshot_params(_rec())["outcome"] == "ok"
    # 缺字段的老记录按 ok(契约默认),不能落成 NULL 与"没采过"混淆
    assert ingest.snapshot_params(_rec(outcome=None))["outcome"] == "ok"
    for col in ("outcome", "completeness_ok"):
        assert col in ingest._SNAPSHOT_SQL


def test_ingest_reports_outcome_distribution():
    conn = _FakeConn()
    counts = ingest.ingest_batch(conn, [
        _rec(source_id="s1", outcome="blocked"),
        _rec(source_id="s2", outcome="blocked"),
        _rec(source_id="s3", outcome="not_found"),
    ])
    assert counts["outcome_blocked"] == 2 and counts["outcome_not_found"] == 1


def test_batch_failures_pulls_detail_not_just_a_count(monkeypatch):
    """失败明细按 batch_id 拉(按批次名的旧接口只给最近 200 条,v4 已废)。"""
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"], seen["params"] = url, params
        return httpx.Response(200, json={
            "batch_id": 7, "count": 1,
            "failed_tasks": [{"asin": "B0A", "status": "failed",
                              "error_type": "captcha", "error_detail": "boom",
                              "retry_count": 3,
                              "updated_at": "2026-08-09 02:00:00"}]},
            request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setenv("SCRAPER_BASE_URL", "http://127.0.0.1:8899")
    rows = scraper.batch_failures("7", limit=999999)
    assert rows[0]["error_type"] == "captcha"
    assert seen["url"].endswith("/api/batches/7/failures")
    assert seen["params"]["limit"] == scraper.FAILURES_LIMIT_MAX   # 上限夹住

    _patch_http(monkeypatch, [(404, {"detail": "批次不存在"})])
    with pytest.raises(LookupError):
        scraper.batch_failures(7)
    with pytest.raises(ValueError):
        scraper.batch_failures(None)        # 没记下 batch_id 就查不了,别瞎猜


def test_pull_failures_lands_rows_with_utc_time(monkeypatch):
    from datetime import timezone
    from services import scrape_batches as sb

    saved = []

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def executemany(self, sql, params): saved.extend(params)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _C()

    monkeypatch.setattr(sb.db, "pg_conn", lambda: _Conn())
    monkeypatch.setattr(sb.scraper, "batch_failures", lambda bid: [
        {"asin": "B0A", "status": "failed", "error_type": "captcha",
         "error_detail": "d", "retry_count": 3,
         "updated_at": "2026-08-09 02:00:00"},
        {"asin": "B0B", "status": "failed", "error_type": "captcha",
         "error_detail": None, "retry_count": 1, "updated_at": None},
        {"asin": None, "error_type": "timeout"},        # 无 asin:落库没价值
    ])
    line, by_asin = sb.pull_failures("wm-refresh-x", "7")
    assert "2 个 ASIN 已落库" in line and "captcha×2" in line
    assert [p[1] for p in saved] == ["B0A", "B0B"]
    # {asin: error_type} 是给调用方写自己台账用的:验证码(可重试)和
    # variant_offset(重试也一样)的处置完全不同,不能一律记成"超时"
    assert by_asin == {"B0A": "captcha", "B0B": "captcha"}
    # 采集侧给的是 UTC 裸串:必须显式补 UTC,否则按会话时区解释会差 8 小时
    assert saved[0][6].tzinfo is not None
    assert saved[0][6].astimezone(timezone.utc).hour == 2
    assert saved[1][6] is None

    # 拉不到不该毁掉整轮状态检查(批次已落定,失败明细是附加信息)
    def boom(bid):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(sb.scraper, "batch_failures", boom)
    assert "拉取失败" in sb.pull_failures("wm-refresh-x", "7")[0]
    assert "查不了" in sb.pull_failures("wm-refresh-x", None)[0]


def test_pull_failures_flags_unregistered_error_type(monkeypatch, caplog):
    """采集侧加了新 error_type 而本侧封闭集没更新 → 必须告警。
    静默当普通失败的话,一整类新故障就永远没人知道。"""
    from services import scrape_batches as sb

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def executemany(self, sql, params): pass

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _C()

    monkeypatch.setattr(sb.db, "pg_conn", lambda: _Conn())
    monkeypatch.setattr(sb.scraper, "batch_failures", lambda bid: [
        {"asin": "B0A", "error_type": "brand_new_failure"}])
    with caplog.at_level("WARNING"):
        _, by_asin = sb.pull_failures("wm-refresh-x", "7")
    assert by_asin == {"B0A": "brand_new_failure"}
    assert "未登记的 error_type" in caplog.text


@pytest.mark.parametrize("body,settled", [
    ({"stats": {"open": 0}, "screenshots": {"open": 0}}, True),
    # 截图没截完**不再算在途**:任务失败(如 variant_offset)之后它那张图
        # 永远不会好,shots_open 永久停在 >0 ⇒ 批次永远落不定,-p wait=1
        # 干等满 20 分钟(所有者 2026-08-10 实测)。截图从不阻断结论,
        # 更不该阻断数据侧的落定判断。
        ({"stats": {"open": 0}, "screenshots": {"open": 3}}, True),   # 图还没截完
    ({"stats": {"open": 2}, "screenshots": {"open": 0}}, False),
    ({"stats": {"open": 0, "failed": 5}, "screenshots": {}}, True),  # 失败算终态
    ({"status": "completed"}, True),          # 无 open 字段的旧响应体
    ({"status": "running"}, False),
    ({}, False),                              # 未知一律当"还在跑",宁可多等一轮
])
def test_is_settled(body, settled):
    from services import scrape_batches as sb
    assert sb.is_settled(body) is settled


def test_refresh_targets_sql_gates():
    from workflows import product_refresh as pr
    # 只推在线 + PUBLISHED + 店铺 ACTIVE(无 KPI 记录 fail-open);跨店去重
    assert "missing_since IS NULL" in pr._SQL_TARGETS
    assert "published_status = 'PUBLISHED'" in pr._SQL_TARGETS
    assert "s.store_status IS NULL OR upper(s.store_status) = 'ACTIVE'" in pr._SQL_TARGETS
    assert "SELECT DISTINCT w.sku" in pr._SQL_TARGETS
    assert pr.TIMEOUT_HOURS == 1 and pr.DANGEROUS is True


def test_refresh_filters_non_asin_skus(monkeypatch):
    """历史遗留的非 ASIN 形态 SKU 推了也建不成任务(所有者定稿:直接过滤)。"""
    from workflows import product_refresh as pr

    skus = ["B0ABCDEFGH", "B0123456789", "WM-CUSTOM-01", "b0abcdefgh", "",
            "B0Z9Y8X7W6"]

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): pass
        def fetchall(self): return [(s,) for s in skus]

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _C()

    monkeypatch.setattr(pr.db, "pg_conn", lambda: _Conn())
    ok, dropped = pr._targets()
    # 11 位、小写、自定义编码、空串全过滤掉
    assert ok == ["B0ABCDEFGH", "B0Z9Y8X7W6"] and dropped == 4


def test_snapshot_carries_shipping_null_is_not_zero():
    """运费三态照搬采集契约:FREE→0.0 / N/A→None / $5.99→5.99。

    **None 不能折成 0**:0 是"确认免运费"这条真信息,None 是"这次没采到"。
    折了的话下游算出的落地价照样看着正常,只是偏小,两侧都不报错
    (契约不变量 3b,与 stock_count 同一条)。
    """
    free = ingest.snapshot_params(_rec(fast={"shipping": 0.0,
                                             "shipping_raw": "FREE"}))
    assert free["shipping"] == 0.0 and free["shipping_raw"] == "FREE"

    missing = ingest.snapshot_params(_rec(fast={"shipping": None,
                                                "shipping_raw": None}))
    assert missing["shipping"] is None          # 绝不是 0

    paid = ingest.snapshot_params(_rec(fast={"shipping": 5.99,
                                             "shipping_raw": "$5.99"}))
    assert paid["shipping"] == 5.99 and paid["shipping_raw"] == "$5.99"

    # 老记录没有这两个键:按未采到,不是 0
    assert ingest.snapshot_params(_rec(fast={"price": 1}))["shipping"] is None
    for col in ("shipping", "shipping_raw"):
        assert col in ingest._SNAPSHOT_SQL
