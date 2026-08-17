"""perf_problems — 绩效问题订单明细(原 daily_report 的 problems 阶段,独立成流)。

用法:
  python cli.py perf_problems                      # 全店
  python cli.py perf_problems -p store=A085朱丽霖   # 单店

所有者定稿 2026-08-08:**从 daily_report 摘出**,由独立调度驱动——日报只要
指标数字(insights summary),不再等这条链。两者数据源不同端点:
  指标比率 = /v3/insights/performance/{指标}/summary(daily_report kpi 阶段)
  订单明细 = /v3/insights/performance/{指标}/report(本工作流,xlsx)
report 端点是全 API 最脆的一族(沃尔玛现场生成 xlsx,多店并发时边缘 520
面状抖动实证),独立后它的失败与重试不再拖慢日报。

写两处:ops.perf_problem_orders(明细,五字段唯一键、首见日期永不覆盖)
+ orders.perf_events(订单中心绩效订单表数据源,按 (po,metric,period=拉取日)
逐周期累积并回填订单行;PO 不在库的事件丢弃——建库窗口外的烂账治理)。
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import httpx

from api import _client, insights
from registry import db
from services import order_center
from services import kpi, order_lines, stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.perf_problems")

_STORE_WORKERS = 6      # 旧系统 README:店铺级并发不要调高(代理共享/全局风控)

_PROBLEM_INSERT = """
INSERT INTO ops.perf_problem_orders (
    first_seen_date, store, sales_order_no, po_no, order_date, indicator,
    sub_category, accountable, description, item, carrier, tracking_no, note, raw)
VALUES (%(first_seen_date)s, %(store)s, %(sales_order_no)s, %(po_no)s,
        %(order_date)s, %(indicator)s, %(sub_category)s, %(accountable)s,
        %(description)s, %(item)s, %(carrier)s, %(tracking_no)s, %(note)s,
        %(raw)s::jsonb)
ON CONFLICT (sales_order_no, indicator, sub_category, tracking_no, item)
DO NOTHING
"""


def _one_store(store: dict, data_date) -> tuple[int, int, int, int, int]:
    """输入:店铺 + 拉取日 → 输出:(解析行, 新增明细, 绩效事件, 无PO, 订单不在库)。"""
    by_metric: list[tuple[str, list[dict]]] = []
    for m in insights.METRICS:
        try:
            blob = insights.performance_report(store, m)
        except Exception as e:
            logger.warning("店铺 %s %s report 拉取失败: %s", store["name"], m, e)
            continue
        if blob:
            by_metric.append((m, kpi.parse_problem_report(m, blob)))

    inserted = perf_written = no_po = unlinked = 0
    rows_all = [r for _, rs in by_metric for r in rs]
    if rows_all:
        with db.pg_conn() as conn, conn.cursor() as cur:
            # returns/INR 报表带行号无 SKU(实证):预载 (po,行号)→sku 供反查建键
            cur.execute("SELECT po_id, line_number, sku FROM orders.order_lines "
                        "WHERE store = %s", (store["name"],))
            sku_lookup = {(po, ln): sku for po, ln, sku in cur.fetchall() if sku}
            for r in rows_all:
                cur.execute(_PROBLEM_INSERT,
                            {**r, "store": store["name"],
                             "first_seen_date": data_date})
                inserted += cur.rowcount
            # 订单中心:逐周期累积进 orders.perf_events(period=拉取日);
            # 烂账治理:PO 不在库(早于建库窗口)的事件不入库
            for m, rs in by_metric:
                perf_rows, skipped = order_lines.perf_rows_from_problems(
                    store["name"], m, rs, str(data_date), sku_lookup)
                perf_rows, drop = order_lines.drop_unlinked_perf(conn, perf_rows)
                unlinked += drop
                perf_written += order_lines.upsert_perf_events(conn, perf_rows)
                no_po += skipped
    if no_po:
        logger.warning("店铺 %s 有 %d 行问题订单无 PO 号,perf_events 未建键",
                       store["name"], no_po)
    if unlinked:
        logger.info("店铺 %s:%d 条绩效事件 PO 不在库,未入库", store["name"], unlinked)
    return len(rows_all), inserted, perf_written, no_po, unlinked


def run(params: dict) -> str:
    """输入:params(可选 store)→ 输出:问题订单与绩效事件摘要。"""
    names = [params["store"]] if params.get("store") else None
    store_list = stores_svc.load_stores(names)
    if not store_list:
        return f"店铺凭证未找到:{params.get('store') or '(任一)'}"
    data_date = datetime.now(kpi.CN_TZ).date()

    total_new = total_rows = total_perf = total_no_po = total_unlinked = 0
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=min(_STORE_WORKERS, len(store_list))) as pool:
        futs = {pool.submit(_one_store, s, data_date): s["name"] for s in store_list}
        for f in as_completed(futs):
            name = futs[f]
            try:
                parsed, inserted, perf_written, no_po, unlinked = f.result()
                total_rows += parsed
                total_new += inserted
                total_perf += perf_written
                total_no_po += no_po
                total_unlinked += unlinked
            except (_client.StoreDeadError, httpx.ProxyError) as e:
                logger.error("店铺 %s 凭证/代理失效跳过: %s", name, e)
                failed.append(f"{name}(凭证)")
            except Exception as e:
                logger.exception("店铺 %s 问题订单失败: %s", name, e)
                failed.append(name)

    with db.pg_conn() as conn:
        linked = order_lines.backfill_perf_line_ids(conn)
    line = (f"问题订单:{len(store_list) - len(failed)}/{len(store_list)} 店,"
            f"解析 {total_rows} 行,新增 {total_new} 条;"
            f"绩效事件 {total_perf} 条(回填订单行 {linked},无 PO 跳过 {total_no_po},"
            f"订单不在库丢弃 {total_unlinked})")
    if failed:
        line += f",失败:{','.join(failed)}"
    # 跑完就写飞书(绩效表全量,自身有界,不吃 days 窗口)
    return line + "\n" + order_center.push_after(
        order_center.BY_WORKFLOW["perf_problems"])
