"""沃尔玛在线商品目录的合并与落库积木(catalog_sync 用,未来 listing/daily_cleanup 复用)。

职责:items 摘要 + 库存合并成行 → upsert catalog.walmart_items → 标记本轮缺席行。
连接一律由调用方传入(registry/db.py 的 pg_conn),本模块不自行连库,便于测试注入。
"""

import logging

from services import product_events

logger = logging.getLogger("services.walmart_catalog")

_UPSERT_SQL = """
INSERT INTO catalog.walmart_items
    (store, sku, wpid, upc, gtin, product_name, shelf, product_type,
     variant_group_id, variant_group_info,
     price, currency, avail_qty, published_status, lifecycle_status,
     unpublished_reasons, last_seen_at, missing_since, updated_at)
VALUES (%(store)s, %(sku)s, %(wpid)s, %(upc)s, %(gtin)s, %(product_name)s,
        %(shelf)s, %(product_type)s,
        %(variant_group_id)s, %(variant_group_info)s::jsonb,
        %(price)s, %(currency)s, %(avail_qty)s,
        %(published_status)s, %(lifecycle_status)s, %(unpublished_reasons)s,
        %(seen_at)s, NULL, now())
ON CONFLICT (store, sku) DO UPDATE SET
    wpid = EXCLUDED.wpid, upc = EXCLUDED.upc, gtin = EXCLUDED.gtin,
    product_name = EXCLUDED.product_name, shelf = EXCLUDED.shelf,
    product_type = EXCLUDED.product_type,
    variant_group_id = EXCLUDED.variant_group_id,
    variant_group_info = EXCLUDED.variant_group_info,
    price = EXCLUDED.price,
    currency = EXCLUDED.currency,
    -- 本轮没拿到库存(接口失败/skip_inventory/该 SKU 缺席响应)时保留上一轮值,不刷成 NULL
    avail_qty = COALESCE(EXCLUDED.avail_qty, catalog.walmart_items.avail_qty),
    published_status = EXCLUDED.published_status,
    lifecycle_status = EXCLUDED.lifecycle_status,
    unpublished_reasons = EXCLUDED.unpublished_reasons,
    -- 缺席后复现 = 可能经历下架重上,itemId 或已改变 → 重置触发回填重查
    item_id = CASE WHEN catalog.walmart_items.missing_since IS NOT NULL
                   THEN NULL ELSE catalog.walmart_items.item_id END,
    last_seen_at = EXCLUDED.last_seen_at, missing_since = NULL, updated_at = now()
"""

_MARK_MISSING_SQL = """
UPDATE catalog.walmart_items
SET missing_since = %(run_at)s, updated_at = now()
WHERE store = %(store)s AND last_seen_at < %(run_at)s AND missing_since IS NULL
RETURNING sku
"""


def merge_rows(store_name: str, item_summaries: list[dict],
               inventory: dict[str, int], seen_at) -> list[dict]:
    """输入:店铺名 + summarize_item 列表 + {sku:数量} + 本轮时间 → 输出:待 upsert 行列表。"""
    rows = []
    for s in item_summaries:
        if not s.get("sku"):
            continue
        rows.append({**s, "store": store_name,
                     "avail_qty": inventory.get(s["sku"]), "seen_at": seen_at})
    return rows


def upsert_items(conn, rows: list[dict]) -> int:
    """输入:连接 + merge_rows 产出的行 → 输出:写入行数(upsert,重复执行幂等)。

    顺手写产品事件账本:先取旧状态,对比出 上架/重现/状态变化 事件
    (services/product_events.diff_catalog),与 upsert 同事务落账。
    """
    if not rows:
        return 0
    store = rows[0]["store"]
    with conn.cursor() as cur:
        cur.execute("SELECT sku, published_status, missing_since "
                    "FROM catalog.walmart_items WHERE store = %s", (store,))
        old = {sku: (st, miss) for sku, st, miss in cur.fetchall()}
        cur.executemany(_UPSERT_SQL, rows)
    product_events.record_many(
        conn, product_events.diff_catalog(old, rows, store))
    return len(rows)


def mark_missing(conn, store_name: str, run_at) -> int:
    """输入:连接 + 店铺 + 本轮时间 → 输出:本轮全量扫描未见而被标记缺席的行数。

    只标记不删除;已标记过的(missing_since 非空)不重复刷新,保留首次缺席时间。
    """
    with conn.cursor() as cur:
        cur.execute(_MARK_MISSING_SQL, {"store": store_name, "run_at": run_at})
        gone = [r[0] for r in (cur.fetchall() or [])]
    product_events.record_many(conn, [
        {"sku": sku, "store": store_name, "event": "item_missing",
         "source": "catalog_sync"} for sku in gone])
    return len(gone)


_PROJECTION_SQL = """
SELECT store, sku, item_id, upc, gtin, product_name, shelf, product_type,
       variant_group_id, variant_group_info::text,
       price, currency, avail_qty, published_status, lifecycle_status,
       unpublished_reasons, last_seen_at, missing_since
FROM catalog.walmart_items ORDER BY store, sku
"""  # 列序与 registry.resources.ONLINE_PRODUCTS_SHEET.columns 一一对应,改必同步
# (wpid 不投影:用户明确不需要;PG 仍保留该列供 API 场景用)


def projection_rows(conn) -> list[list]:
    """输入:连接 → 输出:在线产品总表投影的全部数据行(不含表头,已转字符串安全值)。"""
    from decimal import Decimal

    def _cell(v):
        if v is None:
            return ""
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, (int, float)):
            return v
        return str(v)   # upc/gtin 是 text 原样传,保前导零

    with conn.cursor() as cur:
        cur.execute(_PROJECTION_SQL)
        return [[_cell(v) for v in row] for row in cur.fetchall()]


def skus_missing_item_id(conn, store_name: str) -> set[str]:
    """输入:连接 + 店铺 → 输出:item_id 为空的在售 SKU 集合(ITEM 报表回填候选,全状态)。"""
    with conn.cursor() as cur:
        cur.execute("SELECT sku FROM catalog.walmart_items "
                    "WHERE store = %s AND item_id IS NULL AND missing_since IS NULL",
                    (store_name,))
        return {r[0] for r in cur.fetchall()}


def set_item_ids(conn, store_name: str, mapping: dict[str, str]) -> int:
    """输入:连接 + 店铺 + {sku: item_id} → 输出:更新行数。"""
    if not mapping:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE catalog.walmart_items SET item_id = %(item_id)s, updated_at = now() "
            "WHERE store = %(store)s AND sku = %(sku)s",
            [{"store": store_name, "sku": k, "item_id": v} for k, v in mapping.items()])
        affected = cur.rowcount
    # 返回数据库实际更新行数;与提交数不一致说明 (store, sku) 没对上,要暴露不要吞
    return affected if affected and affected >= 0 else len(mapping)


def known_skus(conn, store_name: str) -> set[str]:
    """输入:连接 + 店铺 → 输出:该店铺 PG 中已知的全部 SKU 集合(offset 截断补漏候选)。"""
    with conn.cursor() as cur:
        cur.execute("SELECT sku FROM catalog.walmart_items WHERE store = %s", (store_name,))
        return {r[0] for r in cur.fetchall()}
