"""feed 生命周期追踪积木(所有 feed 操作共用的轮询机制)。

分工(2026-08-06 定稿):
  api/feeds     负责 提交 + feed_log/feed_items 的**落台账**(提交时)
  本模块        负责 轮询 submitted feed → SKU 级终态回写 ops.feed_items
                + feed_log 落 done/failed + pending 行对账告警
  workflows     feed_poll 薄壳全局轮询;各业务工作流(daily_retire 等)
                用 poll_feed 拿 {sku: 结果} 去刷各自的飞书投影列

SKU 级状态权威在 ops.feed_items;停用/删除/设置到期日期/未来的上架、改价、
改库存、改标题 feed 全走这一套,不许各工作流自造轮询。
"""

import logging

from api import feeds
from registry import db

logger = logging.getLogger("services.feed_track")

# 提交超过此小时数仍 pending(提交结局不确定)→ 告警升级(人工核对后清理)
_PENDING_ALARM_HOURS = 6


def poll_feed(store: dict, feed_id: str) -> dict | None:
    """输入:店铺 + feed_id → 输出:终态时 {sku: (outcome, error_code)},未终态 None。

    终态时同步:ops.feed_items 逐 SKU 落 success/failed(+错误码),
    台账里有而明细里查无的 SKU 落 missing;feed_log 落 done/failed。
    outcome 取值:success / failed / processing / unknown(api/feeds.sku_outcome)。
    """
    head = feeds.get_feed_status(store, feed_id)
    if head.get("feedStatus") not in feeds.FEED_TERMINAL:
        return None

    results: dict[str, tuple[str, str]] = {}
    for item in feeds.iter_feed_items(store, feed_id):
        sku = str(item.get("sku") or "")
        if not sku:
            continue
        code = ""
        errs = (item.get("ingestionErrors") or {}).get("ingestionError") or []
        if errs:
            code = str(errs[0].get("code") or errs[0].get("type") or "")
        results[sku] = (feeds.sku_outcome(item.get("ingestionStatus")), code)

    _STATUS = {"success": "success", "failed": "failed",
               "processing": "submitted", "unknown": "submitted"}
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            "UPDATE ops.feed_items SET status = %s, error_code = %s, "
            "resolved_at = now() WHERE feed_id = %s AND sku = %s",
            [(_STATUS[o], code or None, feed_id, sku)
             for sku, (o, code) in results.items()])
        # 台账里有、终态明细里查无 → missing(不装成功也不装失败)
        cur.execute(
            "UPDATE ops.feed_items SET status = 'missing', resolved_at = now() "
            "WHERE feed_id = %s AND status = 'submitted' AND NOT (sku = ANY(%s))",
            (feed_id, list(results) or [""]))
        n_missing = cur.rowcount
    if n_missing:
        logger.warning("feed %s:%d 个 SKU 在终态明细中查无,已标 missing",
                       feed_id, n_missing)
    feeds.mark_feed_done(feed_id, head.get("feedStatus") == "PROCESSED")
    return results


def poll_all(stores_by_name: dict) -> str:
    """输入:{店铺名: store dict} → 输出:全局轮询摘要。

    扫 feed_log 全部 submitted 行逐一轮询;pending 行(提交结局不确定,
    仅网络 UNKNOWN 结局产生)只告警不自动补交——写操作宁停不重。
    """
    rows = feeds.query_pending()
    submitted = [r for r in rows if r["status"] == "submitted" and r["feed_id"]]
    pendings = [r for r in rows if r["status"] == "pending"]

    done = still = skipped = 0
    for r in submitted:
        store = stores_by_name.get(r["store"])
        if store is None:
            skipped += 1
            continue
        try:
            results = poll_feed(store, r["feed_id"])
        except Exception as e:
            logger.warning("feed %s 轮询失败(下轮再试): %s", r["feed_id"], e)
            still += 1
            continue
        if results is None:
            still += 1
        else:
            done += 1

    if pendings:
        logger.warning("feed_log 有 %d 条 pending(提交结局不确定),"
                       "请人工核对后处理:%s", len(pendings),
                       [(p["store"], p["feed_type"], str(p["created_at"]))
                        for p in pendings[:10]])
    line = (f"feed 轮询:{len(submitted)} 个在途,落定 {done},仍处理中 {still}")
    if skipped:
        line += f",店铺凭证缺失跳过 {skipped}"
    if pendings:
        line += f";⚠ pending 待人工核对 {len(pendings)}"
    return line


def item_results(feed_id: str) -> dict[str, tuple[str, str]]:
    """输入:feed_id → 输出:{sku: (status, error_code)}(读 ops.feed_items 台账)。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT sku, status, error_code FROM ops.feed_items "
                    "WHERE feed_id = %s", (feed_id,))
        return {sku: (status, code or "") for sku, status, code in cur.fetchall()}
