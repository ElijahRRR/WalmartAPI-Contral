"""沃尔玛在线商品目录的合并与落库积木(catalog_sync 用,未来 listing/daily_cleanup 复用)。

职责:items 摘要 + 库存合并成行 → upsert catalog.walmart_items → 标记本轮缺席行。
连接一律由调用方传入(registry/db.py 的 pg_conn),本模块不自行连库,便于测试注入。
"""

import logging

logger = logging.getLogger("services.walmart_catalog")

_UPSERT_SQL = """
INSERT INTO catalog.walmart_items
    (store, sku, wpid, upc, gtin, product_name, shelf, product_type,
     price, currency, avail_qty, published_status, lifecycle_status,
     unpublished_reasons, last_seen_at, missing_since, updated_at)
VALUES (%(store)s, %(sku)s, %(wpid)s, %(upc)s, %(gtin)s, %(product_name)s,
        %(shelf)s, %(product_type)s, %(price)s, %(currency)s, %(avail_qty)s,
        %(published_status)s, %(lifecycle_status)s, %(unpublished_reasons)s,
        %(seen_at)s, NULL, now())
ON CONFLICT (store, sku) DO UPDATE SET
    wpid = EXCLUDED.wpid, upc = EXCLUDED.upc, gtin = EXCLUDED.gtin,
    product_name = EXCLUDED.product_name, shelf = EXCLUDED.shelf,
    product_type = EXCLUDED.product_type, price = EXCLUDED.price,
    currency = EXCLUDED.currency, avail_qty = EXCLUDED.avail_qty,
    published_status = EXCLUDED.published_status,
    lifecycle_status = EXCLUDED.lifecycle_status,
    unpublished_reasons = EXCLUDED.unpublished_reasons,
    last_seen_at = EXCLUDED.last_seen_at, missing_since = NULL, updated_at = now()
"""

_MARK_MISSING_SQL = """
UPDATE catalog.walmart_items
SET missing_since = %(run_at)s, updated_at = now()
WHERE store = %(store)s AND last_seen_at < %(run_at)s AND missing_since IS NULL
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
    """输入:连接 + merge_rows 产出的行 → 输出:写入行数(upsert,重复执行幂等)。"""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_SQL, rows)
    return len(rows)


def mark_missing(conn, store_name: str, run_at) -> int:
    """输入:连接 + 店铺 + 本轮时间 → 输出:本轮全量扫描未见而被标记缺席的行数。

    只标记不删除;已标记过的(missing_since 非空)不重复刷新,保留首次缺席时间。
    """
    with conn.cursor() as cur:
        cur.execute(_MARK_MISSING_SQL, {"store": store_name, "run_at": run_at})
        return cur.rowcount


_PROJECTION_SQL = """
SELECT store, sku, wpid, upc, gtin, product_name, shelf, product_type,
       price, currency, avail_qty, published_status, lifecycle_status,
       unpublished_reasons, last_seen_at, missing_since
FROM catalog.walmart_items ORDER BY store, sku
"""  # 列序与 registry.resources.ONLINE_PRODUCTS_SHEET.columns 一一对应,改必同步


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


def known_skus(conn, store_name: str) -> set[str]:
    """输入:连接 + 店铺 → 输出:该店铺 PG 中已知的全部 SKU 集合(offset 截断补漏候选)。"""
    with conn.cursor() as cur:
        cur.execute("SELECT sku FROM catalog.walmart_items WHERE store = %s", (store_name,))
        return {r[0] for r in cur.fetchall()}
