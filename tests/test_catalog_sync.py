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
    seen_paths = []

    def handler(request):
        seen_paths.append(request.url.path)
        if request.url.path.startswith("/v3/inventories/"):
            # 单品兜底:与 bulk 同族端点,响应同样带 nodes(多仓批次 0 改)
            return httpx.Response(200, json={
                "sku": request.url.path.rsplit("/", 1)[-1],
                "nodes": [{"shipNode": "N1", "availToSellQty": {"amount": 9}}]})
        cursor = request.url.params.get("nextCursor")
        if not cursor:
            # 第一页只有 1 条(< limit)但仍有下页——历史 bug 场景
            return httpx.Response(200, json={
                "elements": {"inventories": [{"sku": "A", "nodes": [
                    {"shipNode": "N1", "availToSellQty": {"amount": 3}},
                    {"shipNode": "N2", "availToSellQty": {"amount": 2}}]}]},
                "meta": {"nextCursor": "PAGE2"}})
        return httpx.Response(200, json={
            "elements": {"inventories": [{"sku": "B", "quantity": {"amount": 7}}]},
            "meta": {"nextCursor": None}})

    _use(monkeypatch, handler)
    inv = inv_api.list_inventories(STORE, expected_skus={"A", "B", "C"})
    assert inv["A"] == 5          # 多 node 求和
    assert inv["B"] == 7
    assert inv["C"] == 9          # bulk 漏掉 → 单查兜底
    # ⚠ 兜底走的是 /v3/inventories/{sku},**不是** legacy /v3/inventory?sku=:
    # 后者不带 shipNode 时返回"默认节点"而非合计,与 bulk 不同语义,多节点店
    # 会让 avail_qty 这一列混进两种口径且无从分辨(多仓批次 0 修)
    assert "/v3/inventories/C" in seen_paths
    assert "/v3/inventory" not in seen_paths


def test_list_inventory_nodes_keeps_node_identity(monkeypatch):
    """节点身份保留在键上——多仓探测靠它数"这个 SKU 铺在几个仓"。"""
    def handler(request):
        return httpx.Response(200, json={
            "elements": {"inventories": [
                {"sku": "A", "nodes": [
                    {"shipNode": "N1", "availToSellQty": {"amount": 3}},
                    {"shipNode": "N2", "availToSellQty": 2}]},      # 裸值也收
                {"sku": "B", "quantity": {"amount": 7}}]},
            "meta": {"nextCursor": None}})

    _use(monkeypatch, handler)
    nodes = inv_api.list_inventory_nodes(STORE)
    assert nodes["A"] == {"N1": 3, "N2": 2}
    assert nodes["B"] == {"": 7}        # 空串 = 节点身份未知(legacy 扁平响应)
    # 合计包装与改造前逐字节同口径
    assert inv_api.list_inventories(STORE) == {"A": 5, "B": 7}


def test_unrecognised_inventory_shape_is_none_not_zero(monkeypatch):
    """⚠ nodes 在而无一带 availToSellQty(官方文档样例给的就是这种 PUT 风格)
    → 判"读不到"返回 None,**绝不当 0**。

    当 0 的后果:该 SKU 的 avail_qty 被刷成 0 → 维护链认为线上没货 → 把 amz
    库存整店重推一遍。返回 None 则落不进结果,COALESCE 保留上一轮值。
    """
    def handler(request):
        return httpx.Response(200, json={
            "elements": {"inventories": [
                {"sku": "A", "nodes": [{"shipNode": "N1", "status": "Success"}]},
                {"sku": "B", "nodes": [{"shipNode": "N1",
                                        "availToSellQty": {"amount": 0}}]}]},
            "meta": {"nextCursor": None}})

    _use(monkeypatch, handler)
    inv = inv_api.list_inventories(STORE)
    assert "A" not in inv               # 形状认不出 → 不落结果
    assert inv["B"] == 0                # 真的是 0 → 照落(与上面区分开)


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
        elif "catalog.listing_sources" in sql and "catalog.walmart_items w" not in sql:
            # record_many 的登记簿反查(批次 0b):这个夹具里没有登记行,
            # 不清空的话上一条 RETURNING 的行会被当成登记簿结果喂回去。
            # ⚠ 排除投影 SQL —— 它也 JOIN 登记簿,但要的是构造函数给的夹具行
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
    rows = walmart_catalog.merge_rows("T1", summaries, {"A": {"N1": 4}},
                                      "2026-08-05")
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
                            datetime(2026, 8, 5, 1, 2, 3), None, "B0AAAAAAA1")])
    rows = walmart_catalog.projection_rows(conn)
    assert rows[0][3] == "00123"                      # 前导零保住
    assert rows[0][8] == pytest.approx(9.9)           # Decimal → float
    assert rows[0][14] == "2026-08-05 01:02:03"       # 时间格式化
    assert rows[0][2] == "" and rows[0][15] == ""     # None → 空串
    assert rows[0][16] == "B0AAAAAAA1"                # 末列 source_key(来源码)

    # 未登记的在架行:LEFT JOIN 取空,_cell 转空串(不是漏行、也不是 None)
    conn2 = _FakeConn(rows=[("T1", "A", None, None, None, None, None, None,
                             None, None, None, None, None, None, None, None,
                             None)])
    assert walmart_catalog.projection_rows(conn2)[0][16] == ""


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
    assert "WHERE w.missing_since IS NULL" in walmart_catalog._PROJECTION_SQL


def test_projection_left_joins_the_registry():
    """钉的是:末列 source_key 走 LEFT JOIN 登记簿,且不带 abandoned_at 条件。

    改成 INNER JOIN,未登记的在架行会静默从飞书表里消失(表变短不报错);
    加 abandoned_at 条件,已弃码却还在架的僵尸行会看不见 —— 那正是要看的行。
    """
    sql = walmart_catalog._PROJECTION_SQL
    assert "LEFT JOIN catalog.listing_sources ls" in sql
    assert "ls.store = w.store AND ls.sku = w.sku" in sql
    assert "abandoned_at" not in sql
    # 不限 source_type:amz=ASIN、match=匹配 GTIN,展示都要
    assert "source_type" not in sql


def test_online_sheet_last_column_is_source_key():
    """钉的是:来源码只准**追加在末尾**(电子表格按 range 坐标写,插中间全体错位)。"""
    from registry import resources
    assert resources.ONLINE_PRODUCTS_SHEET.columns[-1] == "source_key"
    assert len(resources.ONLINE_PRODUCTS_SHEET.columns) == 17     # A~Q


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


def test_sheet_ensure_rows_chunks_at_dimension_max(monkeypatch):
    # dimension_range 单次上限:官方 5000 行(90204 实证 2026-08-05 也是这条),
    # 本仓按 95% 红线取 _SHEET_DIMENSION_MAX=4750 → 扩 12794 行分 4750/4750/3294 三次
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
    assert adds == [4750, 4750, 3294] == [feishu._SHEET_DIMENSION_MAX,
                                          feishu._SHEET_DIMENSION_MAX,
                                          12794 - 2 * feishu._SHEET_DIMENSION_MAX]


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

    # 块大小 = _SHEET_WRITE_MAX_ROWS(官方 5000 行 ×95% = 4750),整表重写与定点
    # 回写走同一套预算切批(唯一写通道),不再各有各的块大小
    writes = [c for c in calls if "values_batch_update" in c[1]]
    assert len(writes) == 2
    assert writes[0][2]["valueRanges"][0]["range"] == "SID!A1:B4750"
    assert writes[1][2]["valueRanges"][0]["range"] == "SID!A4751:B5000"
    # ⚠ 整表重写**不 scrub**:KPI 看板靠写数字型日期序列值 + formatter 才显示成
    # 日期,把数字 str 化会让格式化当场失效
    assert writes[0][2]["valueRanges"][0]["values"][1] == [0, 0]
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
                        lambda conn: (0, 0, []))

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


# ── 多仓探测(批次 0)─────────────────────────────────────────────────────────

def test_merge_rows_derives_total_and_node_count_from_one_source():
    """合计与节点数**同源**:两列都从 {sku:{节点:数量}} 那一份算。

    各算各的就是"一条判据散在多处"的老病 —— 改了其中一处,另外几处不报错、
    只是悄悄按旧规矩办事。读不到库存的 SKU 两列都是 None(走 COALESCE 保旧值,
    不刷成 0)。
    """
    summaries = [{"sku": "A"}, {"sku": "B"}, {"sku": "C"}]
    rows = walmart_catalog.merge_rows(
        "T1", summaries, {"A": {"N1": 3, "N2": 2}, "B": {"": 7}}, "2026-08-24")
    got = {r["sku"]: (r["avail_qty"], r["node_count"]) for r in rows}
    assert got["A"] == (5, 2)       # 两个节点 → 合计 5,node_count 2
    assert got["B"] == (7, 1)
    assert got["C"] == (None, None)  # 本轮读不到 → 两列都 None


def test_upsert_preserves_node_count_when_absent():
    """本轮没拿到库存时 node_count 保留上一轮值(与 avail_qty 同款 COALESCE)。"""
    sql = walmart_catalog._UPSERT_SQL
    assert "node_count = COALESCE(EXCLUDED.node_count," in sql
    assert "catalog.walmart_items.node_count)" in sql


# ── 批次 1:配置 + 分节点落库 ────────────────────────────────────────────────

def test_node_inventory_upsert_keeps_rows_not_seen_this_round():
    """本轮没扫到的节点行**不删**:沃尔玛分页漏 SKU 是常态,删了下轮又建,
    中间那一轮维护链会读成"该节点没货"而把库存重推一遍。过期与否看 seen_at。
    """
    sql = walmart_catalog._NODE_UPSERT_SQL
    assert "ON CONFLICT (store, sku, ship_node) DO UPDATE" in sql
    assert "DELETE" not in sql.upper()


def test_node_inventory_payload_flattens_every_node():
    seen = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def executemany(self, sql, rows): seen.extend(rows)

    class _Conn:
        def cursor(self): return _Cur()

    n = walmart_catalog.upsert_node_inventory(
        _Conn(), "T1", {"A": {"N1": 3, "N2": 2}, "B": {"": 7}}, "2026-08-24")
    assert n == 3
    assert {(r["sku"], r["ship_node"], r["avail_qty"]) for r in seen} == {
        ("A", "N1", 3), ("A", "N2", 2), ("B", "", 7)}


# ── 分节点写(批次 2)─────────────────────────────────────────────────────────

def test_put_inventory_without_node_stays_on_legacy(monkeypatch):
    """未配置「维护仓库」的店逐字节维持现状:legacy PUT /v3/inventory。"""
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return httpx.Response(200, json={"sku": "A"})

    _use(monkeypatch, handler)
    assert inv_api.put_inventory(STORE, "A", 7) == (True, "")
    assert seen["path"] == "/v3/inventory"


def test_put_inventory_with_node_uses_body_and_input_qty(monkeypatch):
    """带节点走 PUT /v3/inventories/{sku}:shipNode 在 **body**,数量字段
    名是 **inputQty**(读侧 availToSellQty / feed 侧 quantity,三套并存)。"""
    import json as _json
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"sku": "A", "nodes": [
            {"shipNode": "N1", "status": "SUCCESS"}]})

    _use(monkeypatch, handler)
    assert inv_api.put_inventory(STORE, "A", 7, "N1") == (True, "")
    assert seen["path"] == "/v3/inventories/A"
    assert seen["body"] == {"inventories": {"nodes": [
        {"shipNode": "N1", "inputQty": {"unit": "EACH", "amount": 7}}]}}


def test_put_inventory_node_parses_partial_success(monkeypatch):
    """⚠ **HTTP 200 不代表写进去了**:每个 nodes[] 各带自己的 status。

    只看 HTTP 码的话,写失败会被当成写成功 —— settle 随后判 ineffective、
    下一轮重发,而"为什么没生效"没有任何线索。
    """
    def fail(request):
        return httpx.Response(200, json={"sku": "A", "nodes": [
            {"shipNode": "N1", "status": "FAILURE",
             "errors": [{"code": "ERR_X", "description": "node not eligible"}]}]})

    _use(monkeypatch, fail)
    ok, why = inv_api.put_inventory(STORE, "A", 7, "N1")
    assert ok is False and "ERR_X" in why

    # 目标节点根本没出现在响应里:同样判失败(宁可多重发一轮)
    def other(request):
        return httpx.Response(200, json={"sku": "A", "nodes": [
            {"shipNode": "N2", "status": "SUCCESS"}]})

    _use(monkeypatch, other)
    assert inv_api.put_inventory(STORE, "A", 7, "N1")[0] is False

    # 形状认不出(字段改名/文档与实测不符)也判失败,绝不当成功
    _use(monkeypatch, lambda r: httpx.Response(200, json={"status": "OK"}))
    assert inv_api.put_inventory(STORE, "A", 7, "N1")[0] is False


def test_inventories_cursor_death_restarts_the_whole_sweep(monkeypatch):
    """翻页中途 400 = 游标作废(2026-08-30 生产实证:断连后重试同游标即 400)。

    与 items 域同款自愈:整轮重来一次,result 从头攒。第二次还 400 就真抛
    —— 无限重扫比失败更糟(一轮全店翻页不便宜)。
    """
    state = {"deaths": 0, "sweeps": 0}

    def handler(request):
        cur = request.url.params.get("nextCursor")
        if cur is None:
            state["sweeps"] += 1
            return httpx.Response(200, json={
                "elements": {"inventories": [
                    {"sku": "A", "nodes": [
                        {"shipNode": "N1", "availToSellQty": {"amount": 3}}]}]},
                "meta": {"nextCursor": "C1"}})
        if state["deaths"] == 0:
            state["deaths"] += 1
            return httpx.Response(400, json={"error": "cursor expired"})
        return httpx.Response(200, json={
            "elements": {"inventories": [
                {"sku": "B", "nodes": [
                    {"shipNode": "N1", "availToSellQty": {"amount": 5}}]}]},
            "meta": {}})

    _use(monkeypatch, handler)
    got = inv_api.list_inventory_nodes(STORE)
    assert got == {"A": {"N1": 3}, "B": {"N1": 5}}      # 重扫后数据完整
    assert state["sweeps"] == 2                          # 确实整轮重来了一次

    def _reuse(handler):
        # 连接池按 proxy 缓存 transport,同一用例内换 handler 必须先清池
        for c in _client._client_pool.values():
            c.close()
        _client._client_pool.clear()
        _use(monkeypatch, handler)

    # 第一页(无游标)就 400 不是游标问题,照旧响亮失败
    _reuse(lambda r: httpx.Response(400, json={}))
    with pytest.raises(RuntimeError, match="返回 400"):
        inv_api.list_inventory_nodes(STORE)

    # 两轮都死:抛 _CursorExpired 会漏出去吗?不 —— 第二轮的异常原样上抛,
    # 调用方(catalog_sync 单店 try)按同步失败处理,不无限重扫
    state2 = {"n": 0}

    def always_dead(request):
        if request.url.params.get("nextCursor") is None:
            return httpx.Response(200, json={
                "elements": {"inventories": []}, "meta": {"nextCursor": "C1"}})
        state2["n"] += 1
        return httpx.Response(400, json={})

    _reuse(always_dead)
    with pytest.raises(inv_api._CursorExpired):
        inv_api.list_inventory_nodes(STORE)
    assert state2["n"] == 2                              # 恰好两轮,没有第三轮
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
                        lambda conn: (0, 0, []))
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
                        lambda conn: (0, 0, []))
    summary = catalog_sync.run({"skip_feishu": "1"})     # 不抛 = 不炸链
    first = summary.splitlines()[0]
    assert "1/2 店完成" in first
    assert "⚠ 缺席 1 店:断店(代理波动)" in first     # 归类进首行,人知道去找代理商


def test_sync_one_store_pulls_inventory_bulk_only(monkeypatch):
    """工作流**不许**把扫描 SKU 集传给 list_inventories(所有者定稿 2026-08-28
    撤线,推翻自己 08-26 的「拍板接上」)。

    撤线依据是接上后的**第一次生产触发**(08-28,A109):目录 6,976 − bulk
    3,511 = 3,465 个"漏",逐个单查**全 404**,一店多烧 43 分钟。404 = 「库存
    台账没有这一行」——退市/Stage 死档案永远不会有,部分**真在线**商品同样
    没有,单查问不出新信息。"bulk 没给"≠"翻页漏了",蓝图 #22 的假设被生产
    证伪。bulk 真漏的行由 upsert 的 COALESCE 沿用上一轮值兜着。
    ⚠ api 层 list_inventories 的 expected_skus **能力保留**(上面那条 api 级
    用例继续钉它),撤的只是本调用方的接线 —— 别顺手删掉 api 能力。"""
    import contextlib
    from datetime import datetime, timezone
    from workflows import catalog_sync

    monkeypatch.setattr(catalog_sync.items, "iter_all_items",
                        lambda store, stats, mode: iter(
                            [{"sku": "A"}, {"sku": "B"}, {"sku": None}]))
    seen: dict = {"called": False}

    def fake_inv(store, expected_skus=None):
        seen["called"] = True
        seen["skus"] = expected_skus
        return {"A": {"N1": 3}}

    # 多仓批次 1 后工作流取节点明细版(list_inventory_nodes);
    # 撤线口径不变:同样不许把扫描集传进去
    monkeypatch.setattr(catalog_sync.inv_api, "list_inventory_nodes", fake_inv)
    monkeypatch.setattr(catalog_sync.walmart_catalog, "upsert_node_inventory",
                        lambda conn, name, inv, run_at: 0)
    monkeypatch.setattr(catalog_sync.db, "pg_conn",
                        lambda *a, **kw: contextlib.nullcontext(object()))
    monkeypatch.setattr(catalog_sync.walmart_catalog, "merge_rows",
                        lambda *a, **kw: [])
    monkeypatch.setattr(catalog_sync.walmart_catalog, "upsert_items",
                        lambda conn, rows: 0)
    monkeypatch.setattr(catalog_sync.walmart_catalog, "mark_missing",
                        lambda conn, name, run_at: 0)

    r = catalog_sync._sync_one_store(STORE, datetime.now(timezone.utc),
                                     False, "fast", False)
    assert seen["called"] and seen["skus"] is None, \
        "接线又被接回来了:扫描集不许进库存单查(2026-08-28 定稿)"
    assert r["inv"] == 1


def test_put_node_never_reads_a_nodeless_echo_as_success(monkeypatch):
    """⚠ 响应里 node 不带 shipNode ⇒ **判失败**,不许当成目标节点成功。

    2026-08-30 生产实测:写入报 (True,'') 而读回毫无变化 —— 当时 `_node_result`
    把"没带 shipNode"也算命中(为兼容不回显节点的响应),于是真成功与假成功
    长得一模一样,无从分辨。宁可多重发一轮。
    """
    _use(monkeypatch, lambda r: httpx.Response(200, json={
        "sku": "A", "nodes": [{"status": "SUCCESS"}]}))          # 无 shipNode
    ok, why = inv_api.put_inventory(STORE, "A", 7, "N1")
    assert ok is False and "没有目标节点 N1" in why
    assert "原始响应" in why          # 形状对不上时要把实物带出来给人看


def test_multi_node_warning_splits_configured_from_unconfigured(monkeypatch):
    """⚠ 多节点告警的措辞按**该店配没配「维护仓库」**分两种(2026-08-31)。

    批次 2 之后配置店的维护链已按受管仓写,再对它喊"仍按单仓写、库存会漂"
    是**过时告警** —— 它正好出现在搬仓当天的摘要里,读起来像"改造没生效",
    把人引向反面。未配置店那句必须原样保留:那才是真的会漂。
    """
    import contextlib
    catalog_sync = _stub_stores(monkeypatch, ["谭总12", "没配的店"])
    monkeypatch.setattr(catalog_sync.store_limits, "maint_nodes",
                        lambda: {"谭总12": "N_NEW"})
    monkeypatch.setattr(catalog_sync, "_sync_one_store",
                        lambda store, *a, **kw: {**_ok_result(store["name"]),
                                                 "multi_node": 3568})
    monkeypatch.setattr(catalog_sync.db, "pg_conn",
                        lambda *a, **kw: contextlib.nullcontext(object()))
    monkeypatch.setattr(catalog_sync.product_events, "verify_deletions",
                        lambda conn: (0, 0, []))
    out = catalog_sync.run({"skip_feishu": "1"})

    assert "谭总12=N_NEW" in out and "自动链不碰" in out   # 配置店:已按受管仓维护
    assert "node_clear" in out                            # 指向旧节点收尾工具
    assert "没配的店" in out and "会漂" in out             # 未配置店:原样告警
    # 配置店不许再被扣上"会漂"的帽子
    warn = [l for l in out.splitlines() if "会漂" in l][0]
    assert "谭总12" not in warn
