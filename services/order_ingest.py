"""订单落库共用入口:小时同步与日报补齐共享日期定稿和字段保护。"""

import logging

from api import orders as orders_api
from registry import db
from services import order_lines as ol

logger = logging.getLogger("services.order_ingest")


def persist(store: dict, orders: list[dict], *, anomalies: list | None = None,
             repair: frozenset = frozenset(), detail_stats: dict | None = None) -> int:
    """输入:店铺 + 该店全部订单(+异常收集器/修复 PO 集/详情计数器)→ 输出:入库行数(fetch_orders_bulk 回调)。

    下单时间(所有者定稿 2026-09-02):新单首见以**详情接口**的值落库并定稿;没被
    详情核对过的存量行每轮查详情直到定稿;详情定稿后列表再不一致只计数、不查不改。
    详情不可用时退回观测→定稿两轮机制。每类异常(疑错/改判/拒写/详情补正/详情失败/
    冲突/待定/存疑)逐条记日志并收进 anomalies(进摘要);repair 里的 PO 才允许 API 值直接覆盖。
    """
    rows: list[dict] = []
    for order in orders:
        rows.extend(ol.extract_order_lines(store["name"], order))
    found: list[dict] = []
    for r in rows:
        for mark, kind in (("_order_date_rejected", "拒写"), ("_order_date_suspect", "存疑")):
            if r.get(mark):
                found.append({"kind": kind, "po": r["po_id"], "sku": r["sku"],
                              "db": None, "api": r.get("order_date")})

    def detail(po: str):
        if detail_stats is not None:
            detail_stats["calls"] = detail_stats.get("calls", 0) + 1
        return orders_api.get_order(store, po)

    try:
        with db.pg_conn() as conn:
            found.extend(ol.screen_order_dates(conn, rows, detail=detail))
            fix = [r for r in rows if r["po_id"] in repair]
            rest = [r for r in rows if r["po_id"] not in repair]
            n = ol.upsert_order_lines(conn, rest) if rest else 0
            if fix:
                n += ol.upsert_order_lines(conn, fix, repair_order_date=True)
    except Exception as e:
        if type(e).__name__ == "UndefinedColumn":   # 表没跟上代码:说人话,别让人猜
            raise RuntimeError("orders.order_lines 缺下单时间定稿列(order_date_seen 等):"
                               "请先执行 python cli.py db_init 再跑") from e
        raise
    if anomalies is not None:
        anomalies.extend({"store": store["name"], **c} for c in found)
    return n
