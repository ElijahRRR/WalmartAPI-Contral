"""order_center_cleanup — 建库一次性清理:删除对不上订单的烂账行(危险:缺省即真跑,空跑用 --dry-run)。

背景(所有者决策 2026-08-06):首次建库只拉近 90 天订单,更早订单的售后/绩效/
对账行永远无法匹配销售行,留着就是烂账。入库侧过滤(returns_sync /
daily_report problems·settlement 已内置)负责堵住每日回流;本工作流负责
清掉过滤上线**之前**已入库的存量烂账。正常只在建库期跑一次。

建库标准序列(单店验证同款,去掉 -p store= 即全店):
  1. python cli.py order_sync   -p days=90
  2. python cli.py returns_sync -p days=90
  3. python cli.py daily_report -p phase=problems
     python cli.py daily_report -p phase=settlement -p periods=99
  4. python cli.py order_center_cleanup             # dry-run 只报数量
     python cli.py order_center_cleanup   # 确认后真删(先加 --dry-run 看清单)
  5. python cli.py order_center_push
     (若清理前已推过飞书:先清空六表数据行,再 push -p reconcile=1)

清理规则:
  - return_lines / settlement_lines:order_line_id 不在 orders.order_lines;
  - perf_events:po_id 不在 orders.order_lines(与入库过滤同口径——
    无 SKU 行只要 PO 在库就保留,给单行订单回填留机会);
  - 被整期删空的对账账期记入 recon_done 台账,防止被当缺失账期重拉。
"""

import logging

from registry import db
from services import order_lines as ol

DANGEROUS = True

logger = logging.getLogger("workflows.order_center_cleanup")

_RETURNS_WHERE = ("FROM orders.return_lines r WHERE NOT EXISTS "
                  "(SELECT 1 FROM orders.order_lines l "
                  "WHERE l.order_line_id = r.order_line_id)")
_PERF_WHERE = ("FROM orders.perf_events p WHERE NOT EXISTS "
               "(SELECT 1 FROM orders.order_lines l WHERE l.po_id = p.po_id)")
_SETTLE_WHERE = ("FROM orders.settlement_lines s WHERE NOT EXISTS "
                 "(SELECT 1 FROM orders.order_lines l "
                 "WHERE l.order_line_id = s.order_line_id)")


def run(params: dict) -> str:
    """输入:params(execute 由 cli 注入)→ 输出:各表烂账统计/删除结果。"""
    execute = bool(params.get("execute"))
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) {_RETURNS_WHERE}")
        n_ret = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) {_PERF_WHERE}")
        n_perf = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) {_SETTLE_WHERE}")
        n_settle = cur.fetchone()[0]
        # 将被整期删空的账期(全部行都对不上)→ 需记台账防重拉
        cur.execute(
            "SELECT s.store, s.period FROM orders.settlement_lines s "
            "GROUP BY s.store, s.period "
            "HAVING bool_and(NOT EXISTS (SELECT 1 FROM orders.order_lines l "
            "WHERE l.order_line_id = s.order_line_id))")
        vanish = cur.fetchall()

        head = (f"烂账体检:售后 {n_ret} 行,绩效 {n_perf} 行,对账 {n_settle} 行"
                f"(其中 {len(vanish)} 个账期将被整期清空,已安排记台账)")
        if not execute:
            return ("🧪 [DRY-RUN] " + head +
                    "\n确认无误后去掉 --dry-run 重跑真删;删除后若飞书已推过数据:"
                    "清空六表数据行 → order_center_push -p reconcile=1 重推")

        cur.execute(f"DELETE {_RETURNS_WHERE}")
        d_ret = cur.rowcount
        cur.execute(f"DELETE {_PERF_WHERE}")
        d_perf = cur.rowcount
        cur.execute(f"DELETE {_SETTLE_WHERE}")
        d_settle = cur.rowcount
        by_store: dict[str, list[str]] = {}
        for store, period in vanish:
            by_store.setdefault(store, []).append(period)
        for store, periods in by_store.items():
            ol.mark_recon_done(conn, store, periods)

    logger.info("烂账清理:售后 %d,绩效 %d,对账 %d,台账账期 %d",
                d_ret, d_perf, d_settle, len(vanish))
    return (f"烂账清理完成:删除 售后 {d_ret} 行,绩效 {d_perf} 行,"
            f"对账 {d_settle} 行;整期清空的 {len(vanish)} 个账期已记台账。"
            f"\n后续:若飞书已推过数据,清空六表数据行后 "
            f"order_center_push -p reconcile=1 重推")
