"""catalog_sync 链条回归:items 分页模型1、inventories 分页模型4、合并落库、飞书投影。"""

import contextlib
import time

import httpx
import pytest

from api import _client, feishu, inventory as inv_api, items
from services import walmart_catalog

STORE = {"name": "T1", "client_id": "cid_cs", "client_secret": "sec", "proxy": None}


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


# ── GET /v3/items 分页模型 1 ──────────────────────────────────────────────────

def test_list_items_cursor_anchored_offset_paging(monkeypatch):
    calls = []

    def handler(request):
        p = request.url.params
        calls.append((p.get("nextCursor"), int(p.get("offset"))))
        offset = int(p.get("offset"))
        page = [{"sku": f"S{i}"} for i in range(offset, min(offset + 2, 5))]
        return httpx.Response(200, json={
            "ItemResponse": page, "totalItems": 5, "nextCursor": "REAL_CURSOR"})

    _use(monkeypatch, handler)
    got, truncated = items.list_items(STORE, lifecycle_status="ACTIVE", limit=2)
    assert [i["sku"] for i in got] == ["S0", "S1", "S2", "S3", "S4"]
    assert truncated is False
    # 首页 '*',拿到真 cursor 后全程不变;翻页靠 offset
    assert calls[0] == ("*", 0)
    assert all(c == "REAL_CURSOR" for c, _ in calls[1:])
    assert [o for _, o in calls] == [0, 2, 4]


def test_list_items_truncates_at_max_offset(monkeypatch):
    def handler(request):
        offset = int(request.url.params.get("offset"))
        return httpx.Response(200, json={
            "ItemResponse": [{"sku": f"S{offset + i}"} for i in range(2)],
            "totalItems": 100, "nextCursor": "C"})

    _use(monkeypatch, handler)
    got, truncated = items.list_items(STORE, max_offset=4, limit=2)
    assert truncated is True and len(got) == 4


def test_list_items_cursor_expiry_resets_and_retries(monkeypatch):
    state = {"expired_once": False, "calls": 0}

    def handler(request):
        p = request.url.params
        state["calls"] += 1
        if p.get("nextCursor") == "C1" and not state["expired_once"]:
            state["expired_once"] = True
            return httpx.Response(400, json={"error": "cursor expired"})
        cursor = "C2" if state["expired_once"] else "C1"
        offset = int(p.get("offset"))
        page = [{"sku": f"S{offset}"}] if offset < 2 else []
        return httpx.Response(200, json={
            "ItemResponse": page, "totalItems": 2, "nextCursor": cursor})

    _use(monkeypatch, handler)
    got, _ = items.list_items(STORE, limit=1)
    assert [i["sku"] for i in got] == ["S0", "S1"]   # 整轮重来后拿全


def test_list_items_404_means_empty_round(monkeypatch):
    # 生产实证:某状态组合零商品时官方返回 404 而非空列表,必须按空轮处理
    _use(monkeypatch, lambda r: httpx.Response(404, json={}))
    got, truncated = items.list_items(STORE, published_status="UNPUBLISHED")
    assert got == [] and truncated is False


def test_list_inventories_404_means_empty(monkeypatch):
    _use(monkeypatch, lambda r: httpx.Response(404, json={}))
    assert inv_api.list_inventories(STORE) == {}


def test_iter_all_items_five_rounds_dedupe(monkeypatch):
    seen_rounds = []

    def handler(request):
        p = request.url.params
        seen_rounds.append((p.get("lifecycleStatus"), p.get("publishedStatus")))
        if p.get("lifecycleStatus") == "RETIRED":
            page = [{"sku": "DUP"}, {"sku": "R1"}]     # DUP 与 ACTIVE 轮重复
        elif p.get("publishedStatus") == "PUBLISHED":
            page = [{"sku": "DUP"}, {"sku": "A1"}]
        else:
            page = []
        return httpx.Response(200, json={
            "ItemResponse": page, "totalItems": len(page), "nextCursor": "C"})

    _use(monkeypatch, handler)
    stats = {}
    got = list(items.iter_all_items(STORE, stats))
    assert [i["sku"] for i in got] == ["DUP", "A1", "R1"]   # 跨轮去重
    assert stats["total"] == 3 and stats["truncated"] is False
    assert len({r for r in seen_rounds}) == 5               # 5 轮组合都扫了


def test_iter_all_items_fast_mode_two_rounds(monkeypatch):
    seen_params = []

    def handler(request):
        p = request.url.params
        seen_params.append((p.get("lifecycleStatus"), p.get("publishedStatus")))
        page = [{"sku": "X"}] if p.get("lifecycleStatus") is None else [{"sku": "R"}]
        return httpx.Response(200, json={
            "ItemResponse": page, "totalItems": 1, "nextCursor": "C"})

    _use(monkeypatch, handler)
    stats = {}
    got = list(items.iter_all_items(STORE, stats, mode="fast"))
    assert [i["sku"] for i in got] == ["X", "R"]
    assert seen_params == [(None, None), ("RETIRED", None)]   # 无参轮 + RETIRED 补充轮
    assert stats["rounds"] == {"ALL/ALL": 1, "RETIRED/ALL": 1}


def test_get_item_404_is_none_and_store_dead_raises(monkeypatch):
    _use(monkeypatch, lambda r: httpx.Response(404, json={}))
    assert items.get_item(STORE, "NOPE") is None

    _client._close_all_clients()   # 连接池按代理缓存 transport,换 handler 前必须清
    _use(monkeypatch, lambda r: httpx.Response(401, json={}))
    with pytest.raises(_client.StoreDeadError):
        items.get_item(STORE, "ANY")


def test_summarize_item_reasons_dict_and_list():
    s = items.summarize_item({"sku": "A", "unpublishedReasons": {"reason": ["r1", "r2"]},
                              "price": {"amount": 5, "currency": "USD"}})
    assert s["unpublished_reasons"] == "r1; r2"
    s2 = items.summarize_item({"sku": "B", "unpublished_reasons": ["x"]})
    assert s2["unpublished_reasons"] == "x"


def test_summarize_item_variant_group():
    s = items.summarize_item({"sku": "A", "variantGroupId": "VG1",
                              "variantGroupInfo": {"isPrimary": True,
                                                   "groupingAttributes": [{"name": "color"}]}})
    assert s["variant_group_id"] == "VG1"
    assert '"isPrimary": true' in s["variant_group_info"]
    s2 = items.summarize_item({"sku": "B"})
    assert s2["variant_group_id"] is None and s2["variant_group_info"] is None


# ── GET /v3/inventories 分页模型 4 ───────────────────────────────────────────

def test_list_inventories_terminates_on_cursor_not_page_length(monkeypatch):
    def handler(request):
        if request.url.path == "/v3/inventory":
            return httpx.Response(200, json={"sku": request.url.params["sku"],
                                             "quantity": {"amount": 9}})
        cursor = request.url.params.get("nextCursor")
        if not cursor:
            # 第一页只有 1 条(< limit)但仍有下页——历史 bug 场景
            return httpx.Response(200, json={
                "elements": {"inventories": [{"sku": "A", "nodes": [
                    {"availToSellQty": {"amount": 3}}, {"availToSellQty": {"amount": 2}}]}]},
                "meta": {"nextCursor": "PAGE2"}})
        return httpx.Response(200, json={
            "elements": {"inventories": [{"sku": "B", "quantity": {"amount": 7}}]},
            "meta": {"nextCursor": None}})

    _use(monkeypatch, handler)
    inv = inv_api.list_inventories(STORE, expected_skus={"A", "B", "C"})
    assert inv["A"] == 5          # 多 node 求和
    assert inv["B"] == 7
    assert inv["C"] == 9          # bulk 漏掉 → 单查兜底


# ── services 合并与落库 ───────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, rows=None):
        self.executed = []
        self.rows = rows or []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self.rowcount = 2
        # mark_missing 改 RETURNING sku 后按 fetchall 计数
        if "RETURNING sku" in sql:
            self.rows = [("S1",), ("S2",)]
        elif "SELECT sku, published_status" in sql:
            self.rows = []

    def executemany(self, sql, seq):
        batch = list(seq)
        self.executed.append((sql, batch))
        self.rowcount = len(batch)     # 模拟 psycopg3:executemany 后 rowcount=累计影响行数

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows=None):
        self.cur = _FakeCursor(rows)

    def cursor(self):
        return self.cur


def test_merge_rows_and_upsert():
    summaries = [{"sku": "A", "wpid": None, "upc": "012", "gtin": None,
                  "product_name": "P", "shelf": None, "product_type": None,
                  "price": 9.9, "currency": "USD", "published_status": "PUBLISHED",
                  "lifecycle_status": "ACTIVE", "unpublished_reasons": ""},
                 {"sku": None}]     # 无 sku 的丢弃
    rows = walmart_catalog.merge_rows("T1", summaries, {"A": 4}, "2026-08-05")
    assert len(rows) == 1 and rows[0]["avail_qty"] == 4 and rows[0]["store"] == "T1"

    conn = _FakeConn()
    assert walmart_catalog.upsert_items(conn, rows) == 1
    assert walmart_catalog.upsert_items(conn, []) == 0
    assert walmart_catalog.mark_missing(conn, "T1", "2026-08-05") == 2


def test_projection_rows_cell_conversion():
    from datetime import datetime
    from decimal import Decimal
    conn = _FakeConn(rows=[("T1", "A", None, "00123", None, "P", None, None,
                            Decimal("9.90"), "USD", 4, "PUBLISHED", "ACTIVE", "",
                            datetime(2026, 8, 5, 1, 2, 3), None)])
    rows = walmart_catalog.projection_rows(conn)
    assert rows[0][3] == "00123"                      # 前导零保住
    assert rows[0][8] == pytest.approx(9.9)           # Decimal → float
    assert rows[0][14] == "2026-08-05 01:02:03"       # 时间格式化
    assert rows[0][2] == "" and rows[0][15] == ""     # None → 空串


def test_item_id_backfill_helpers():
    conn = _FakeConn(rows=[("SKU_A",), ("SKU_B",)])
    assert walmart_catalog.skus_missing_item_id(conn, "T1") == {"SKU_A", "SKU_B"}
    assert walmart_catalog.set_item_ids(conn, "T1", {"SKU_A": "14901706450"}) == 1
    assert walmart_catalog.set_item_ids(conn, "T1", {}) == 0


def test_report_parse_and_item_id_extraction():
    from api import reports
    import io as _io
    import zipfile as _zip
    csv_bytes = ("SKU,Product Name,Item Page URL\n"
                 "B0AAA,Cup,https://www.walmart.com/ip/Steel-Cup/14901706450\n"
                 "B0BBB,Lid,\n").encode("utf-8-sig")
    rows = reports.parse_report_csv(csv_bytes)
    assert reports.report_row_sku(rows[0]) == "B0AAA"
    assert reports.extract_item_id(rows[0]) == "14901706450"   # 从 /ip/ URL 提取
    assert reports.extract_item_id(rows[1]) is None

    # 显式 Item ID 列优先
    rows2 = reports.parse_report_csv(b"SKU,ITEM_ID\nB0CCC,592648041\n")
    assert reports.extract_item_id(rows2[0]) == "592648041"

    # zip 包装的 CSV
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w") as zf:
        zf.writestr("report.csv", csv_bytes)
    assert reports.parse_report_csv(buf.getvalue())[0]["SKU"] == "B0AAA"


def test_upsert_resets_item_id_on_reappearance():
    # 缺席后复现的行 item_id 必须重置(下架重上可能换 ID),正常在售的行保留
    assert "missing_since IS NOT NULL" in walmart_catalog._UPSERT_SQL
    assert "THEN NULL ELSE catalog.walmart_items.item_id" in walmart_catalog._UPSERT_SQL


def test_upsert_preserves_avail_qty_when_absent():
    # 库存失败/skip_inventory 时 EXCLUDED.avail_qty 为 NULL,必须保留旧值而非清空
    assert "COALESCE(EXCLUDED.avail_qty, catalog.walmart_items.avail_qty)" \
        in walmart_catalog._UPSERT_SQL


def test_mark_missing_clears_status_columns():
    # 缺席行 published/lifecycle 清空(所有者定稿 2026-08-07):旧观测不再展示
    assert "published_status = NULL" in walmart_catalog._MARK_MISSING_SQL
    assert "lifecycle_status = NULL" in walmart_catalog._MARK_MISSING_SQL


def test_projection_excludes_missing_rows():
    # 飞书「在线产品总表」只写在架商品,缺席行不进表
    assert "WHERE missing_since IS NULL" in walmart_catalog._PROJECTION_SQL


def test_projection_columns_match_registry():
    from registry import resources
    select_part = walmart_catalog._PROJECTION_SQL.split("FROM")[0]
    n_sql = select_part.replace("SELECT", "").count(",") + 1
    assert n_sql == len(resources.ONLINE_PRODUCTS_SHEET.columns)


# ── 飞书电子表格 ──────────────────────────────────────────────────────────────

def test_col_letter():
    assert feishu._col_letter(1) == "A"
    assert feishu._col_letter(16) == "P"
    assert feishu._col_letter(26) == "Z"
    assert feishu._col_letter(27) == "AA"


def test_sheet_ensure_rows_chunks_at_5000(monkeypatch):
    # dimension_range 单次上限 5000(90204 实证):扩 12794 行须分 5000/5000/2794 三次
    from registry.resources import Spreadsheet
    sheet = Spreadsheet(name="测试表", token="TOK", sheet_id="SID", columns=("a",))
    adds = []

    def fake_call(method, path, *, json_body=None, params=None, timeout=60):
        if path.endswith("/sheets/query"):
            return {"sheets": [{"sheet_id": "SID", "grid_properties": {"row_count": 1}}]}
        adds.append(json_body["dimension"]["length"])
        return {}

    monkeypatch.setattr(feishu, "_call", fake_call)
    assert feishu.sheet_ensure_rows(sheet, 12795) == 12794
    assert adds == [5000, 5000, 2794]


def test_sheet_overwrite_blocks_and_trims(monkeypatch):
    from registry.resources import Spreadsheet
    sheet = Spreadsheet(name="测试表", token="TOK", sheet_id="SID", columns=("a", "b"))
    calls = []
    grid_rows = {"n": 9000}

    def fake_call(method, path, *, json_body=None, params=None, timeout=60):
        calls.append((method, path, json_body))
        if path.endswith("/sheets/query"):
            return {"sheets": [{"sheet_id": "SID",
                                "grid_properties": {"row_count": grid_rows["n"]}}]}
        return {}

    monkeypatch.setattr(feishu, "_call", fake_call)
    rows = [["h1", "h2"]] + [[i, i] for i in range(4999)]   # 5000 行 → 2 块
    assert feishu.sheet_overwrite(sheet, rows) == 5000

    writes = [c for c in calls if "values_batch_update" in c[1]]
    assert len(writes) == 2
    assert writes[0][2]["valueRanges"][0]["range"] == "SID!A1:B4000"
    assert writes[1][2]["valueRanges"][0]["range"] == "SID!A4001:B5000"
    deletes = [c for c in calls if c[0] == "DELETE"]        # 9000 网格 - 5000 数据 → 删尾
    assert len(deletes) == 1
    dim = deletes[0][2]["dimension"]
    assert dim["startIndex"] == 5001 and dim["endIndex"] == 9000


def test_projection_skipped_when_sheet_unregistered(monkeypatch):
    monkeypatch.delenv("FEISHU_ONLINE_SHEET_TOKEN", raising=False)
    monkeypatch.delenv("FEISHU_ONLINE_SHEET_ID", raising=False)
    from workflows import catalog_sync
    out = catalog_sync._write_projection()
    assert "跳过" in out and "在线产品总表" in out


# ── 一家店坏掉 vs 全部店坏掉:两者的成败判定必须相反 ──────────────────────

def _stub_stores(monkeypatch, names):
    from workflows import catalog_sync
    monkeypatch.setattr(catalog_sync.stores_svc, "load_stores",
                        lambda filter_names=None: [
                            {"name": n, "client_id": f"c{n}", "client_secret": "s",
                             "proxy": "http://p:1"} for n in names])
    return catalog_sync


def _ok_result(name):
    return {"store": name, "written": 10, "missing": 0, "item_ids": 0,
            "truncated": False, "inv_failed": False}


def test_partial_dead_store_still_succeeds(monkeypatch):
    """41/42 店完成、一家凭证坏掉 ⇒ **成功**(所有者 2026-08-17:程序是跑成功的)。

    一家店的凭证坏掉不该拖垮整轮——目录数据已经入库、飞书已经重写。它以
    「凭证失效跳过」出现在成功通知里,每天可见,不会烂在那没人管。
    """
    import contextlib
    catalog_sync = _stub_stores(monkeypatch, ["好店", "坏店"])

    def one(store, *a, **kw):
        if store["name"] == "坏店":
            raise catalog_sync._client.StoreDeadError(store["name"], 400)
        return _ok_result(store["name"])

    monkeypatch.setattr(catalog_sync, "_sync_one_store", one)
    monkeypatch.setattr(catalog_sync.db, "pg_conn",
                        lambda *a, **kw: contextlib.nullcontext(object()))
    monkeypatch.setattr(catalog_sync.product_events, "verify_deletions",
                        lambda conn: (0, 0))

    summary = catalog_sync.run({"skip_feishu": "1"})      # 不碰飞书
    assert "1/2 店完成" in summary
    assert "凭证失效跳过:坏店" in summary


def test_zero_stores_completed_is_failure_not_success(monkeypatch):
    """全部店都被跳过 ⇒ **失败**。

    「凭证失效跳过」按设计不进 failed,于是"全部店都跳过"曾经一路走到
    `return` 报成功——那一轮什么都没同步,而通知是绿的。这也是把换 token 的
    400 归为死店之后必须堵上的口子:万一请求形状被改坏,表现正是全部店一起
    dead,不能让它静默报成功。
    """
    catalog_sync = _stub_stores(monkeypatch, ["A1", "A2"])

    def dead(store, *a, **kw):
        raise catalog_sync._client.StoreDeadError(store["name"], 400)

    monkeypatch.setattr(catalog_sync, "_sync_one_store", dead)
    with pytest.raises(RuntimeError) as ei:
        catalog_sync.run({"skip_feishu": "1"})
    assert "零店完成" in str(ei.value)
    assert "0/2 店完成" in str(ei.value)


def test_failed_store_gets_one_serial_second_pass(monkeypatch):
    """店级重试标准①(所有者定稿 2026-08-26):失败店跑完别人后串行补试一遍,
    救回的照常入账 —— 且补试跑的是**同一个** _sync_one_store(单一落地路径)。"""
    import contextlib
    catalog_sync = _stub_stores(monkeypatch, ["好店", "抖店"])
    calls = []

    def one(store, *a, **kw):
        calls.append(store["name"])
        if store["name"] == "抖店" and calls.count("抖店") == 1:
            raise OSError("proxy hiccup")           # 第一轮抖,补试即好
        return _ok_result(store["name"])

    monkeypatch.setattr(catalog_sync, "_sync_one_store", one)
    monkeypatch.setattr(catalog_sync.db, "pg_conn",
                        lambda *a, **kw: contextlib.nullcontext(object()))
    monkeypatch.setattr(catalog_sync.product_events, "verify_deletions",
                        lambda conn: (0, 0))
    summary = catalog_sync.run({"skip_feishu": "1"})
    assert "2/2 店完成" in summary
    assert "⚠ 缺席" not in summary       # 救回了就不点名(「缺席标记 N 行」是另一回事)
    assert calls.count("抖店") == 2                  # 首轮 + 补试各一次,不多试


def test_still_failed_store_is_absent_in_first_line_not_a_raise(monkeypatch):
    """标准②:补试仍失败 ⇒ **不炸整轮**(08-26 事故:两家店 SOCKS 报错放倒
    八步链)。缺席店在摘要**首行**点名并带归类词 —— 链通知只发成功步骤的
    第一行,放后面等于只写日志。"""
    import contextlib
    catalog_sync = _stub_stores(monkeypatch, ["好店", "断店"])
    from socksio.exceptions import ProtocolError

    def one(store, *a, **kw):
        if store["name"] == "断店":
            raise ProtocolError("Malformed reply")   # 事故同款,补试也不好
        return _ok_result(store["name"])

    monkeypatch.setattr(catalog_sync, "_sync_one_store", one)
    monkeypatch.setattr(catalog_sync.db, "pg_conn",
                        lambda *a, **kw: contextlib.nullcontext(object()))
    monkeypatch.setattr(catalog_sync.product_events, "verify_deletions",
                        lambda conn: (0, 0))
    summary = catalog_sync.run({"skip_feishu": "1"})     # 不抛 = 不炸链
    first = summary.splitlines()[0]
    assert "1/2 店完成" in first
    assert "⚠ 缺席 1 店:断店(代理波动)" in first     # 归类进首行,人知道去找代理商
