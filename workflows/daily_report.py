"""daily_report — 沃尔玛店铺日报(替代旧 沃尔玛店铺日报/ 三脚本流水线)。

用法:
  python cli.py daily_report                       # 全部阶段(kpi → problems),不推送
  python cli.py daily_report -p phase=kpi          # 只采集 KPI
  python cli.py daily_report -p phase=problems     # 只拉问题订单
  python cli.py daily_report -p phase=push -p push=1   # 生成日报并真正推送(飞书 webhook)
  python cli.py daily_report -p store=A085朱丽霖    # 单店(kpi/problems 阶段)

与旧系统的关键差异(设计决策见 docs/legacy_survey.md #daily_report 迁移建议):
- PG 是权威(ops.store_kpi_daily / ops.perf_problem_orders),飞书降级为展示层
  ——消掉旧系统「清空飞书全表再重写,中途崩溃=历史全丢」的最大风险
- 在线商品/有库存/无库存 三列直接读 catalog.walmart_items(catalog_sync 的产出),
  不再翻页调 /v3/items——中央库复用,省 60/min 配额
- 影刀 RPA 本期不 spawn:只读旧系统产出的 latest.json(新鲜才用销售状态;
  卖家名称允许 stale 补——旧系统反直觉规则,原样保留);切换期由旧系统 8 点驱动影刀
- 问题订单 xlsx 的逐指标列映射旧代码不可得,首版按表头关键词归一并保留原始行
  (raw 列),对拍后校准
- push 阶段默认只打印预览;-p push=1 才真发(并跑期不打扰运营)
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import httpx

from api import _client, feishu, insights, orders as orders_api, reports
from registry import db, paths
from services import kpi, stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.daily_report")

_STORE_WORKERS = 6      # 旧系统 README:店铺级并发不要调高(代理共享/全局风控)

_KPI_UPSERT = """
INSERT INTO ops.store_kpi_daily (
    store, data_date, seller_name, partner_id, seller_id, store_status,
    payment_status, sales_status, items_online, items_in_stock, items_out_stock,
    orders_count, sales_amount, otd_rate, cancel_rate, vtr_rate, srr_rate,
    refund_rate, negative_rate, return_rate, inr_rate, period_sales, commission,
    refund_amount, closing_balance, reserve_to_date, payout, payout_date,
    payment_processor, settle_cycle, no_hold, prev_payout, updated_at)
VALUES (%(store)s, %(data_date)s, %(seller_name)s, %(partner_id)s, %(seller_id)s,
        %(store_status)s, %(payment_status)s, %(sales_status)s, %(items_online)s,
        %(items_in_stock)s, %(items_out_stock)s, %(orders_count)s, %(sales_amount)s,
        %(otd_rate)s, %(cancel_rate)s, %(vtr_rate)s, %(srr_rate)s, %(refund_rate)s,
        %(negative_rate)s, %(return_rate)s, %(inr_rate)s, %(period_sales)s,
        %(commission)s, %(refund_amount)s, %(closing_balance)s, %(reserve_to_date)s,
        %(payout)s, %(payout_date)s, %(payment_processor)s, %(settle_cycle)s,
        %(no_hold)s, %(prev_payout)s, now())
ON CONFLICT (store, data_date) DO UPDATE SET
    -- 影刀两列:本轮为空不覆盖旧值(旧系统 A-H 保护语义的 PG 等价)
    seller_name = COALESCE(EXCLUDED.seller_name, ops.store_kpi_daily.seller_name),
    sales_status = COALESCE(EXCLUDED.sales_status, ops.store_kpi_daily.sales_status),
    partner_id = EXCLUDED.partner_id, seller_id = EXCLUDED.seller_id,
    store_status = EXCLUDED.store_status, payment_status = EXCLUDED.payment_status,
    items_online = EXCLUDED.items_online, items_in_stock = EXCLUDED.items_in_stock,
    items_out_stock = EXCLUDED.items_out_stock, orders_count = EXCLUDED.orders_count,
    sales_amount = EXCLUDED.sales_amount, otd_rate = EXCLUDED.otd_rate,
    cancel_rate = EXCLUDED.cancel_rate, vtr_rate = EXCLUDED.vtr_rate,
    srr_rate = EXCLUDED.srr_rate, refund_rate = EXCLUDED.refund_rate,
    negative_rate = EXCLUDED.negative_rate, return_rate = EXCLUDED.return_rate,
    inr_rate = EXCLUDED.inr_rate, period_sales = EXCLUDED.period_sales,
    commission = EXCLUDED.commission, refund_amount = EXCLUDED.refund_amount,
    closing_balance = EXCLUDED.closing_balance,
    reserve_to_date = EXCLUDED.reserve_to_date, payout = EXCLUDED.payout,
    payout_date = EXCLUDED.payout_date,
    payment_processor = EXCLUDED.payment_processor,
    settle_cycle = EXCLUDED.settle_cycle, no_hold = EXCLUDED.no_hold,
    prev_payout = EXCLUDED.prev_payout, updated_at = now()
"""

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


def _load_frontend(fresh_within_hours: int = 24) -> tuple[dict, dict]:
    """输入:新鲜度阈值 → 输出:(卖家名称 map, 销售状态 map),键为 sellerId。

    读影刀产出的 latest.json(路径经 registry)。卖家名称允许 stale;
    销售状态只在数据新鲜时用——旧值回填会把昨天的状态传染成今天(旧事故规则)。
    """
    path = paths.frontend_scrape_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.info("影刀 latest.json 不可读(%s),前台两列留空", path)
        return {}, {}
    names, statuses = {}, {}
    fresh = False
    try:
        scraped = datetime.fromisoformat(str(data.get("scraped_at")))
        age_h = (datetime.now(timezone.utc) - scraped.astimezone(timezone.utc)).total_seconds() / 3600
        fresh = age_h <= fresh_within_hours
    except (TypeError, ValueError):
        pass
    for sid, info in (data.get("stores") or {}).items():
        if info.get("seller_name"):
            names[str(sid)] = info["seller_name"]
        if fresh and info.get("sales_status"):
            statuses[str(sid)] = info["sales_status"]
    if not fresh:
        logger.info("影刀数据不新鲜(scraped_at=%s),销售状态留空(卖家名称仍可用)",
                    data.get("scraped_at"))
    return names, statuses


def _pg_item_counts(store_name: str) -> tuple[int, int, int]:
    """输入:店铺 → 输出:(在线商品, 有库存, 无库存),读 catalog.walmart_items。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FILTER (WHERE published_status='PUBLISHED'),"
            "       count(*) FILTER (WHERE published_status='PUBLISHED' AND avail_qty > 0),"
            "       count(*) FILTER (WHERE published_status='PUBLISHED' AND"
            "                        (avail_qty IS NULL OR avail_qty = 0))"
            " FROM catalog.walmart_items WHERE store=%s AND missing_since IS NULL",
            (store_name,))
        row = cur.fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def _collect_store_kpi(store: dict, data_date, win_start: str, win_end: str,
                       names: dict, statuses: dict) -> dict:
    """输入:店铺 + 日期 + 24h 窗口 + 影刀两 map → 输出:一行 KPI dict。"""
    name = store["name"]

    # 8 项绩效并发(端点桶互相独立,同店同端点才是 1/min)
    rates: dict[str, float | None] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(insights.performance_summary, store, m): m
                for m in insights.METRICS}
        for f in as_completed(futs):
            m = futs[f]
            try:
                rates[m] = f.result()
            except Exception as e:
                logger.warning("店铺 %s 绩效 %s 拉取失败: %s", name, m, e)
                rates[m] = None

    settle = kpi.extract_settlement(reports.payment_statement(store))

    prev_payout = 0.0
    recon_date = kpi.prev_recon_date(settle["payout_date"])
    if recon_date:
        try:
            if recon_date in reports.available_recon_dates(store):
                prev_payout = kpi.payment_summary_total(
                    reports.iter_recon_records(store, recon_date, page_size=5))
        except Exception as e:
            logger.warning("店铺 %s 上期回款查询失败(按 0 计): %s", name, e)

    stats: dict = {}
    sales = 0.0
    try:
        for order in orders_api.iter_orders(store, created_start=win_start,
                                            created_end=win_end, stats=stats):
            sales += orders_api.order_product_sales(order)
    except Exception as e:
        logger.warning("店铺 %s 订单窗口拉取失败(单量/销售额置 0): %s", name, e)
        stats["total"] = 0

    online, in_stock, out_stock = _pg_item_counts(name)
    sid = settle["seller_id"]
    return {
        "store": name, "data_date": data_date,
        "seller_name": names.get(sid) or None,
        "sales_status": statuses.get(sid) or None,
        "partner_id": settle["partner_id"], "seller_id": sid,
        "store_status": settle["store_status"],
        "payment_status": settle["payment_status"],
        "items_online": online, "items_in_stock": in_stock,
        "items_out_stock": out_stock,
        "orders_count": stats.get("total", 0), "sales_amount": round(sales, 2),
        "otd_rate": rates.get("otd"), "cancel_rate": rates.get("cancellations"),
        "vtr_rate": rates.get("vtr"), "srr_rate": rates.get("srr"),
        "refund_rate": rates.get("refunds"),
        "negative_rate": rates.get("negativeFeedback"),
        "return_rate": rates.get("returns"), "inr_rate": rates.get("itemNotReceived"),
        "period_sales": settle["period_sales"], "commission": settle["commission"],
        "refund_amount": settle["refund_amount"],
        "closing_balance": settle["closing_balance"],
        "reserve_to_date": settle["reserve_to_date"], "payout": settle["payout"],
        "payout_date": settle["payout_date"],
        "payment_processor": settle["payment_processor"],
        "settle_cycle": settle["settle_cycle"], "no_hold": settle["no_hold"],
        "prev_payout": prev_payout,
    }


def _phase_kpi(store_list: list[dict], data_date) -> str:
    win_start, win_end = kpi.sales_window_utc()
    names, statuses = _load_frontend()
    ok, failed = [], []
    with ThreadPoolExecutor(max_workers=min(_STORE_WORKERS, len(store_list))) as pool:
        futs = {pool.submit(_collect_store_kpi, s, data_date, win_start, win_end,
                            names, statuses): s["name"] for s in store_list}
        for f in as_completed(futs):
            name = futs[f]
            try:
                row = f.result()
                with db.pg_conn() as conn:
                    conn.execute(_KPI_UPSERT, row)
                ok.append(name)
            except (_client.StoreDeadError, httpx.ProxyError) as e:
                logger.error("店铺 %s 凭证/代理失效跳过: %s", name, e)
                failed.append(f"{name}(凭证)")
            except Exception as e:
                logger.exception("店铺 %s KPI 采集失败: %s", name, e)
                failed.append(name)
    line = f"KPI:{len(ok)}/{len(store_list)} 店入库(窗口 {win_start}~{win_end})"
    if failed:
        line += f",失败:{','.join(failed)}"
    return line


def _phase_problems(store_list: list[dict], data_date) -> str:
    total_new, total_rows, failed = 0, 0, []

    def _one_store(store: dict) -> tuple[int, int]:
        rows_all = []
        for m in insights.METRICS:
            try:
                blob = insights.performance_report(store, m)
            except Exception as e:
                logger.warning("店铺 %s %s report 拉取失败: %s", store["name"], m, e)
                continue
            if blob:
                rows_all.extend(kpi.parse_problem_report(m, blob))
        inserted = 0
        if rows_all:
            with db.pg_conn() as conn, conn.cursor() as cur:
                for r in rows_all:
                    cur.execute(_PROBLEM_INSERT,
                                {**r, "store": store["name"],
                                 "first_seen_date": data_date})
                    inserted += cur.rowcount
        return len(rows_all), inserted

    with ThreadPoolExecutor(max_workers=min(_STORE_WORKERS, len(store_list))) as pool:
        futs = {pool.submit(_one_store, s): s["name"] for s in store_list}
        for f in as_completed(futs):
            name = futs[f]
            try:
                parsed, inserted = f.result()
                total_rows += parsed
                total_new += inserted
            except (_client.StoreDeadError, httpx.ProxyError) as e:
                logger.error("店铺 %s 凭证/代理失效跳过: %s", name, e)
                failed.append(f"{name}(凭证)")
            except Exception as e:
                logger.exception("店铺 %s 问题订单失败: %s", name, e)
                failed.append(name)
    line = (f"问题订单:{len(store_list) - len(failed)}/{len(store_list)} 店,"
            f"解析 {total_rows} 行,新增 {total_new} 条(其余为历史已存在)")
    if failed:
        line += f",失败:{','.join(failed)}"
    return line


def _phase_push(data_date, do_push: bool) -> str:
    """输入:日期 + 是否真发 → 输出:结果行。读 PG 生成日报 markdown。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), coalesce(sum(orders_count),0), coalesce(sum(sales_amount),0)"
            " FROM ops.store_kpi_daily WHERE data_date=%s", (data_date,))
        n_stores, n_orders, n_sales = cur.fetchone()
        cur.execute(
            "SELECT store, count(*) FROM ops.perf_problem_orders "
            "WHERE first_seen_date=%s GROUP BY store ORDER BY count(*) DESC LIMIT 5",
            (data_date,))
        top_problems = cur.fetchall()
        cur.execute(
            "SELECT t.store, t.store_status, y.store_status"
            " FROM ops.store_kpi_daily t JOIN ops.store_kpi_daily y"
            "   ON y.store = t.store AND y.data_date = t.data_date - 1"
            " WHERE t.data_date=%s AND t.store_status IS DISTINCT FROM y.store_status",
            (data_date,))
        status_changes = cur.fetchall()

    lines = [f"📊 沃尔玛店铺日报 {data_date}",
             f"店铺 {n_stores} 家 | 24h 订单 {int(n_orders)} 单 | 销售额 ${float(n_sales):,.2f}"]
    if status_changes:
        lines.append("⚠ 店铺状态变化:" + "; ".join(
            f"{s}: {old or '?'}→{new or '?'}" for s, new, old in status_changes))
    if top_problems:
        lines.append("🆕 今日新增问题订单 TOP:" + "; ".join(
            f"{s} {c} 条" for s, c in top_problems))
    text = "\n".join(lines)

    if do_push:
        sent = feishu.notify(text)
        return f"日报已推送(webhook={'成功' if sent else '未配置/失败'})"
    logger.info("日报预览(未推送,-p push=1 真发):\n%s", text)
    return "日报仅预览(未推送):\n" + text


def run(params: dict) -> str:
    """输入:params(phase/store/push)→ 输出:各阶段结果摘要。"""
    phase = str(params.get("phase", "all"))
    if phase not in ("all", "kpi", "problems", "push"):
        return f"phase 只接受 all/kpi/problems/push,收到:{phase}"
    data_date = datetime.now(kpi.CN_TZ).date()

    lines = []
    if phase in ("all", "kpi", "problems"):
        names = [params["store"]] if params.get("store") else None
        store_list = stores_svc.load_stores(names)
        if not store_list:
            return f"店铺凭证未找到:{params.get('store') or '(任一)'}"
        if phase in ("all", "kpi"):
            lines.append(_phase_kpi(store_list, data_date))
        if phase in ("all", "problems"):
            lines.append(_phase_problems(store_list, data_date))
    if phase == "push":
        do_push = str(params.get("push", "")) in ("1", "true", "yes")
        lines.append(_phase_push(data_date, do_push))
    return "\n".join(lines)
