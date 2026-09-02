"""沃尔玛在线商品目录的合并与落库积木(catalog_sync 用,未来 listing/daily_cleanup 复用)。

职责:items 摘要 + 库存合并成行 → upsert catalog.walmart_items → 标记本轮缺席行。
连接一律由调用方传入(registry/db.py 的 pg_conn),本模块不自行连库,便于测试注入。
"""

import logging

from services import listing_sources, product_events

logger = logging.getLogger("services.walmart_catalog")

_UPSERT_SQL = """
INSERT INTO catalog.walmart_items
    (store, sku, wpid, upc, gtin, product_name, shelf, product_type,
     variant_group_id, variant_group_info,
     price, currency, avail_qty, node_count, published_status, lifecycle_status,
     unpublished_reasons, last_seen_at, missing_since, updated_at)
VALUES (%(store)s, %(sku)s, %(wpid)s, %(upc)s, %(gtin)s, %(product_name)s,
        %(shelf)s, %(product_type)s,
        %(variant_group_id)s, %(variant_group_info)s::jsonb,
        %(price)s, %(currency)s, %(avail_qty)s, %(node_count)s,
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
    node_count = COALESCE(EXCLUDED.node_count, catalog.walmart_items.node_count),
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
SET missing_since = %(run_at)s,
    -- 缺席行状态清空(所有者定稿 2026-08-07):published/lifecycle 是"它还在架时"
    -- 的旧观测,商品已从目录消失后保留会误导;复现时 upsert 重新写入新状态
    published_status = NULL, lifecycle_status = NULL,
    updated_at = now()
WHERE store = %(store)s AND last_seen_at < %(run_at)s AND missing_since IS NULL
RETURNING sku
"""


def merge_rows(store_name: str, item_summaries: list[dict],
               inventory: dict[str, dict[str, int]], seen_at) -> list[dict]:
    """输入:店铺名 + summarize_item 列表 + {sku:{节点:数量}} + 本轮时间
    → 输出:待 upsert 行列表。

    ⚠ 入参从 `{sku: 合计}` 改成 `{sku: {节点: 数量}}`(2026-08-24,多仓批次 0):
    合计与节点数都从这一份算,**不能让调用方各算各的** —— 那正是"一条判据散在
    多处"的老病(改了其中一处,另外几处不报错、只是悄悄按旧规矩办事)。
    读不到库存的 SKU 在字典里根本不出现,两列都落 None 走 COALESCE 保旧值。
    """
    rows = []
    for s in item_summaries:
        if not s.get("sku"):
            continue
        nodes = inventory.get(s["sku"])
        rows.append({**s, "store": store_name, "seen_at": seen_at,
                     "avail_qty": sum(nodes.values()) if nodes else None,
                     "node_count": len(nodes) if nodes else None})
    return rows


_NODE_UPSERT_SQL = """
INSERT INTO catalog.item_node_inventory (store, sku, ship_node, avail_qty, seen_at)
VALUES (%(store)s, %(sku)s, %(ship_node)s, %(avail_qty)s, %(seen_at)s)
ON CONFLICT (store, sku, ship_node) DO UPDATE SET
    avail_qty = EXCLUDED.avail_qty, seen_at = EXCLUDED.seen_at
"""


def upsert_node_inventory(conn, store_name: str,
                          inventory: dict[str, dict[str, int]], seen_at) -> int:
    """输入:连接 + 店铺 + {sku:{节点:数量}} + 本轮时间 → 输出:写入行数。

    `walmart_items.avail_qty`(合计)的明细面,给维护链的"受管仓现值"用。
    ⚠ **本轮没扫到的行不删**(见 refdata/schema.sql 的表头注释):沃尔玛分页
    漏 SKU 是常态,删了下轮又建,中间那轮维护链会读成"该节点没货"而重推库存。
    过期与否由 `seen_at` 说了算,不由"在不在表里"说了算。
    """
    payload = [{"store": store_name, "sku": sku, "ship_node": node,
                "avail_qty": qty, "seen_at": seen_at}
               for sku, nodes in inventory.items()
               for node, qty in nodes.items()]
    if not payload:
        return 0
    with conn.cursor() as cur:
        cur.executemany(_NODE_UPSERT_SQL, payload)
    return len(payload)


def drop_node_rows(conn, store_name: str, sku: str) -> int:
    """输入:连接 + 店 + SKU → 输出:删除的分节点库存行数(**只给改码定案用**)。

    catalog.item_node_inventory 的**唯一删除出口**(SKU 改造批次 3,O11)。
    与 upsert_node_inventory 头注的「本轮没扫到的行不删」不冲突:那条讲的是
    沃尔玛分页漏 SKU(行还在、只是这轮没扫到),这里讲的是**我们自己把这个
    SKU 改没了** —— 改码定案后旧码在沃尔玛侧已不存在,留着就是一条永不更新的
    幽灵节点库存,而维护链的受管仓判据会照读不误。

    调用方只有 workflows/sku_migrate 的 confirmed 分支,与 settle_replacement
    同一事务;别处要删这张表的行 = 先想清楚是不是又在按"没扫到"删。
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM catalog.item_node_inventory "
                    "WHERE store = %(store)s AND sku = %(sku)s",
                    {"store": store_name, "sku": sku})
        return cur.rowcount


def upsert_items(conn, rows: list[dict]) -> int:
    """输入:连接 + merge_rows 产出的行 → 输出:写入行数(upsert,重复执行幂等)。

    顺手写产品事件账本:先取旧状态,对比出 上架/重现/状态变化 事件
    (services/product_events.diff_catalog),与 upsert 同事务落账。

    在途改码的 {新码: 旧码} 也在**同一轮观测**里取(SKU 改造批次 3,O2):
    diff_catalog 是纯函数(自述"便于测试")、自己不查库,替换关系只能由调用方
    喂进去;而这里已经在同一事务里取过旧状态,顺手多取一张小表是最省的接法,
    也保证"新码第一次出现"与"事件落账"用的是同一份替换关系。
    改码前 replacement_map 恒返回空字典 ⇒ 事件列表逐行不变。
    """
    if not rows:
        return 0
    store = rows[0]["store"]
    with conn.cursor() as cur:
        cur.execute("SELECT sku, published_status, missing_since "
                    "FROM catalog.walmart_items WHERE store = %s", (store,))
        old = {sku: (st, miss) for sku, st, miss in cur.fetchall()}
        replaced = listing_sources.replacement_map(conn, store)
        cur.executemany(_UPSERT_SQL, rows)
    product_events.record_many(
        conn, product_events.diff_catalog(old, rows, store, replaced=replaced))
    return len(rows)


def mark_missing(conn, store_name: str, run_at) -> int:
    """输入:连接 + 店铺 + 本轮时间 → 输出:本轮全量扫描未见而被标记缺席的行数。

    只标记不删除;已标记过的(missing_since 非空)不重复刷新,保留首次缺席时间。

    ⚠ **在途改码的旧码:照常标 missing_since,但不记 item_missing**
    (SKU 改造批次 3,O3)。它的消失是**我们自己造成的**(SkuUpdate 生效后旧码
    必然从目录消失),不是平台下架,不进病历:照记的话 ① product_risk 的
    unexplained_missing(「我们没提交过删/停 + 消失过」)会对每一个被改码的品
    置真,list_new 每轮对着一大批行报"疑似平台强制下架";② missing_times 灌水,
    风险档案失真。
    **抑制的是事件不是观测**:missing_since 必须照写 —— sku_migrate 判 confirmed
    的"旧码缺席"证据就取自它,抑制掉标记会让定案永远等不到。
    返回值仍是被标缺席的**总行数**(契约不变,workflows/catalog_sync 的调用方
    一字不改);被抑制的条数只进日志。
    """
    with conn.cursor() as cur:
        cur.execute(_MARK_MISSING_SQL, {"store": store_name, "run_at": run_at})
        gone = [r[0] for r in (cur.fetchall() or [])]
    replaced = listing_sources.replaced_skus(conn, store_name) if gone else set()
    hushed = [sku for sku in gone if sku in replaced]
    if hushed:
        logger.info("%s:%d 个缺席行是在途改码的旧码,不记 item_missing"
                    "(missing_since 照标,它正是定案证据):%s",
                    store_name, len(hushed), ",".join(hushed[:10]))
    product_events.record_many(conn, [
        {"sku": sku, "store": store_name, "event": product_events.ITEM_MISSING,
         "source": "catalog_sync"} for sku in gone if sku not in replaced])
    return len(gone)


_PROJECTION_SQL = """
SELECT w.store, w.sku, w.item_id, w.upc, w.gtin, w.product_name, w.shelf,
       w.product_type, w.variant_group_id, w.variant_group_info::text,
       w.price, w.currency, w.avail_qty, w.published_status, w.lifecycle_status,
       w.unpublished_reasons, ls.source_key
FROM catalog.walmart_items w
LEFT JOIN catalog.listing_sources ls ON ls.store = w.store AND ls.sku = w.sku
WHERE w.missing_since IS NULL
ORDER BY w.store, w.sku
"""  # 列序与 registry.resources.ONLINE_PRODUCTS_SHEET.columns 一一对应,改必同步
# 最后一列 source_key 来自登记簿 LEFT JOIN(不限 source_type:amz=ASIN、
# match=匹配 GTIN),未登记行为空;LEFT JOIN 是硬要求 —— 未登记的在架行照样
# 要进表。**不加 abandoned_at 条件**:投影是展示,已弃码的在架僵尸行更要看得见
# (wpid 不投影:用户明确不需要;PG 仍保留该列供 API 场景用)
# 缺席行不投影、last_seen_at/missing_since 两列不投影(所有者定稿 2026-08-07):
# 飞书表只展示在架商品;追踪与历史在 PG(两列仍是缺席标记/删除核验的依据)+ 事件账本


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
