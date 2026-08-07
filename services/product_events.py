"""产品事件账本积木(catalog.product_events):SKU(=ASIN)一生的病历。

事件码(唯一出处,新增先登记在此):
  item_appeared          catalog_sync 观测到新上架(店铺目录里首次/重新出现为新行)
  item_reappeared        曾标缺席(missing_since)后又被扫到
  item_missing           本轮全量扫未见(被删/被平台移除的观测事实)
  status_changed         published_status 变化(detail 含 old/new/官方原因)
  delete_submitted       product_clear 提交 DELETE_ITEM(detail 含 feed_id/操作原因)
  retire_submitted       product_clear 提交 RETIRE_ITEM
  {delete|retire|maintenance}_feed_{success|failed}
                         feed 终态的逐 SKU 回执(feed_track 写;success 是
                         沃尔玛的一面之词,删除以观测核验为准)
  delete_verified        观测核验:回执成功且商品确实从目录消失
  delete_not_effective   观测核验:回执成功但宽限期后商品仍在架(真实案例,
                         所有者实证)——告警,人工处置

原则:只追加永不改;回执与观测分开记,互相印证;上架防呆查 product_risk 视图。

入账边界(所有者定稿 2026-08-07):病历只记**产品生死**(删除/停用/反补)
与观测事实。标题/价格/库存维护(含清库存)一律不进——清库存是店铺维度的
运营操作,本系统不设店铺维度病历;此类操作的流水在 ops.feed_log/feed_items,
现状在 catalog.walmart_items 快照,若引发平台下架等后果,由 catalog_sync
的 status_changed 观测自动入账(动作不记,生死后果必记)。
"""

import json
import logging

logger = logging.getLogger("services.product_events")

_FEED_KIND = {"DELETE_ITEM": "delete", "RETIRE_ITEM": "retire",
              "MP_MAINTENANCE": "maintenance"}

# 回执入账白名单:生死类恒记;MP_MAINTENANCE 是通用部分更新 feed,
# 只有反补来源(登记制,未来 listing 反补在此登记)才记,
# 标题/到期日期等常规维护走同一 feedType 但不入病历
_RECEIPT_KINDS = {"delete", "retire"}
_MAINT_LEDGER_WORKFLOWS = {"problem_product_cleanup"}


def feed_kind(feed_type: str) -> str:
    return _FEED_KIND.get(feed_type, feed_type.lower())


def receipt_in_ledger(kind: str, workflow: str | None) -> bool:
    """输入:feed 业务类别 + 提交来源工作流 → 输出:该回执是否进病历。

    维护事件入账定稿(所有者 2026-08-07):改价/改库存/改标题/清库存的
    回执不进 product_events(流水已在 ops.feed_items);delete/retire 恒进;
    maintenance 仅反补来源进(反补计数依赖其提交/回执链)。
    """
    if kind in _RECEIPT_KINDS:
        return True
    return kind == "maintenance" and (workflow or "") in _MAINT_LEDGER_WORKFLOWS


def record_many(conn, rows: list[dict]) -> int:
    """输入:连接 + 事件行 [{sku, store, event, source, error_code?, detail?}]
    → 输出:写入数。detail 自动 JSON 序列化。"""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO catalog.product_events "
            "(sku, store, event, source, error_code, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
            [(r["sku"], r.get("store"), r["event"], r["source"],
              r.get("error_code"),
              json.dumps(r["detail"], ensure_ascii=False, default=str)
              if r.get("detail") is not None else None)
             for r in rows])
    return len(rows)


def diff_catalog(old: dict, new_rows: list[dict], store: str,
                 source: str = "catalog_sync") -> list[dict]:
    """输入:旧状态 {sku: (published_status, missing_since)} + 本轮行 + 店铺
    → 输出:状态迁移事件列表(纯函数,便于测试)。

    - 旧表没有 → item_appeared
    - 旧表标缺席又出现 → item_reappeared
    - published_status 变化 → status_changed(detail 含官方 unpublished 原因,
      这就是"平台把它下架了、为什么"的观测记录)
    """
    events = []
    for r in new_rows:
        sku = r.get("sku")
        if not sku:
            continue
        prev = old.get(sku)
        new_st = r.get("published_status")
        if prev is None:
            events.append({"sku": sku, "store": store, "event": "item_appeared",
                           "source": source,
                           "detail": {"published_status": new_st}})
            continue
        prev_st, prev_missing = prev
        if prev_missing is not None:
            # 复现只记 reappeared(detail 已含新状态);缺席行状态列已被清空,
            # 再比对必然"变化",叠记 status_changed(old=None)是噪音
            events.append({"sku": sku, "store": store,
                           "event": "item_reappeared", "source": source,
                           "detail": {"published_status": new_st}})
            continue
        if new_st != prev_st:
            events.append({"sku": sku, "store": store,
                           "event": "status_changed", "source": source,
                           "detail": {"old": prev_st, "new": new_st,
                                      "reasons": r.get("unpublished_reasons")}})
    return events


_VERIFY_SQL = """
WITH last_ok AS (
    SELECT DISTINCT ON (store, sku) store, sku, occurred_at
    FROM catalog.product_events
    WHERE event = 'delete_feed_success'
    ORDER BY store, sku, occurred_at DESC),
open_ok AS (
    SELECT l.* FROM last_ok l
    WHERE NOT EXISTS (
        SELECT 1 FROM catalog.product_events v
        WHERE v.store = l.store AND v.sku = l.sku
          AND v.event IN ('delete_verified', 'delete_not_effective')
          AND v.occurred_at >= l.occurred_at))
SELECT o.store, o.sku,
       CASE WHEN w.sku IS NULL OR w.missing_since IS NOT NULL
                 OR w.lifecycle_status = 'RETIRED' THEN 'gone'
            WHEN w.last_seen_at > o.occurred_at + make_interval(hours => %s)
                 THEN 'still'
            ELSE 'wait' END AS verdict
FROM open_ok o
LEFT JOIN catalog.walmart_items w ON w.store = o.store AND w.sku = o.sku
"""


def verify_deletions(conn, grace_hours: int = 48) -> tuple[int, int]:
    """输入:连接 + 宽限小时数 → 输出:(核验生效数, 未生效数)。

    删除核验(不信回执,信观测):delete_feed_success 之后,
    - 商品从目录消失/标缺席/RETIRED → delete_verified;
    - 宽限期后 catalog_sync 仍扫到它在架 → delete_not_effective + 告警
      (回执说成了但后台没删,所有者实证的真实故障模式);
    - 还没等到下一轮扫描 → 保持待核验,不落判。
    """
    with conn.cursor() as cur:
        cur.execute(_VERIFY_SQL, (grace_hours,))
        rows = cur.fetchall()
    events = []
    gone = still = 0
    for store, sku, verdict in rows:
        if verdict == "gone":
            gone += 1
            events.append({"sku": sku, "store": store, "event": "delete_verified",
                           "source": "catalog_sync"})
        elif verdict == "still":
            still += 1
            events.append({"sku": sku, "store": store,
                           "event": "delete_not_effective",
                           "source": "catalog_sync"})
    if still:
        logger.warning("删除核验:%d 个 SKU 回执成功但仍在架(delete_not_effective),"
                       "样本=%s,请人工处置",
                       still, [(s, k) for s, k, v in rows if v == "still"][:5])
    record_many(conn, events)
    return gone, still
