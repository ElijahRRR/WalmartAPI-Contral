"""daily_report — 沃尔玛店铺日报(替代旧 沃尔玛店铺日报/ 三脚本流水线)。

用法:
  python cli.py daily_report                       # **默认全链**:kpi(含影刀)→ board → 推送飞书
  python cli.py daily_report -p phase=kpi          # 只采集 KPI
  python cli.py daily_report -p phase=kpi -p yingdao=input  # 只产出影刀输入清单,不 spawn
  python cli.py daily_report -p phase=kpi -p yingdao=1   # 含影刀链(停旧调度后才可开)
  python cli.py daily_report -p phase=board [-p days=90]   # 只刷 KPI 看板(总览+历史)
  python cli.py daily_report -p phase=push -p push=1   # 生成日报并真正推送(飞书 webhook)
  python cli.py daily_report -p store=A085朱丽霖    # 单店(kpi 阶段)

**问题订单已摘出**(所有者定稿 2026-08-08):绩效问题订单明细(insights
report 端点,xlsx)+ 订单中心 perf_events 归 `workflows/perf_problems.py`,
由独立调度驱动;本工作流只取指标比率(insights summary 端点),不再等
那条最脆的链。push 阶段的"问题订单 TOP"仍读 PG,数据由那条流写。

**对账明细也已摘出**(所有者定稿 2026-08-10):账期是**双周**节奏,KPI 是**每日**,
绑在一起等于每天为一件十四天才变一次的事扫全店 ⇒ `workflows/settlement_sync.py`。

与旧系统的关键差异(设计决策见 docs/legacy_survey.md #daily_report 迁移建议):
- PG 是权威(ops.store_kpi_daily / ops.perf_problem_orders),飞书降级为展示层
  ——消掉旧系统「清空飞书全表再重写,中途崩溃=历史全丢」的最大风险
- KPI 看板(所有者定稿 2026-08-08):新表格两页——总览(每店最新一行全 32 列)
  + 历史(全店合一近 90 天),整表重写可随时重建;旧「店铺KPI」表 72 张
  每店分页**停更归档**(仅剩两个角色:kpi_history_import 的只读导入源、
  yingdao=1 的影刀输入 A:H——影刀 RPA 内部读旧表,切换需先改 RPA 再换 env)
- 在线商品/有库存/无库存 三列直接读 catalog.walmart_items(catalog_sync 的产出),
  不再翻页调 /v3/items——中央库复用,省 60/min 配额
- 昨日出单/销售额两列改读 orders.order_lines(所有者认可 2026-08-08):当前为
  双算对拍期——API 现拉仍是权威值,库算值只记日志差异;连续对平后摘除 API
  拉取,order_sync 成为本工作流的调度前置
- 影刀接入(-p yingdao=1;2026-08-08 接入,2026-08-15 改为文件衔接):
  写 input.json 影刀输入清单(过滤空 sellerId 行——A147 事故防线)→ spawn 影刀
  → 等 latest.json 新鲜 → 回填当日卖家名称/销售状态。
  **不再经飞书**:原「先把总览写飞书 → 影刀读飞书拿 sellerId」已删,新影刀
  应用直接读 paths.yingdao_input_file()。`-p yingdao=input` 是接入调试档:
  只产出清单不 spawn,旧调度还开着时也能安全跑。默认关:切换期仍由旧系统 8 点驱动影刀,
  **停旧 walmart-kpi-daily 之前严禁开启**(双 spawn 互抢,新鲜度校验会
  反复失败到超时);未开/超时时只读现有 latest.json(新鲜才用销售状态;
  卖家名称允许 stale 补 + 跨日延续)
- **默认全链**(所有者定稿 2026-08-16 走进生产):不带参数 = kpi(含影刀)+ 看板 +
  **真发日报**。调度里只写工作流名就是完整一轮,不必逐个 phase 拼命令 ——
  漏拼一段的后果是"日报每天不发而且报成功"。
  要空跑预览:`-p push=0`;要关影刀:`-p yingdao=0`
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from api import _client, feishu, insights, orders as orders_api, reports
from registry import db, paths, resources
from services import kpi, order_lines, store_events, stores as stores_svc, \
    yingdao

DANGEROUS = False

logger = logging.getLogger("workflows.daily_report")

# 店铺级并发(所有者定稿 2026-08-16 走进生产:6 → 16,"不然速度跟不上")。
# ⚠ 旧系统 README 曾写"店铺级并发不要调高(代理共享/全局风控)",这条被显式覆盖。
# 支撑理由:每店有**自己的固定出口代理**,店铺之间不共享出口;沃尔玛配额是
# 按 (store, endpoint) 计的,api/_client.py 的令牌桶也按这个维度限流,所以
# 加店铺并发不会挤占同一个桶。真正的共享资源只有本机 CPU/连接数。
# 若出现大面积 429 或代理超时,先降这个数再查别的。
_STORE_WORKERS = stores_svc.STORE_WORKERS   # 唯一出处在 services/stores

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


def _last_seller_names() -> dict[str, str]:
    """输入:无 → 输出:{store: 最近一次非空卖家名称},影刀缺席时跨日延续。

    卖家名称允许 stale(旧系统规则),影刀当天没给就沿用库里最近值;
    销售状态**不做**同款延续——旧值回填会把昨天的状态传染成今天(旧事故规则)。
    """
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (store) store, seller_name"
            " FROM ops.store_kpi_daily WHERE seller_name IS NOT NULL"
            " ORDER BY store, data_date DESC")
        return {s: n for s, n in cur.fetchall()}


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


def _pg_order_stats(store_name: str, win_start: str, win_end: str) -> tuple[int, float]:
    """输入:店铺 + 24h 窗口(ISO UTC)→ 输出:(订单数, 销售额),读 orders.order_lines。

    对拍影子:与 KPI 阶段 API 现拉的单量/销售额同窗口双算。口径对齐 API 侧:
    订单数 = 窗口内下单的 PO 数(meta.totalCount 的库等价 = COUNT(DISTINCT po_id)),
    销售额 = 行 PRODUCT 费用合计(order_lines.product_amount 同源)。
    """
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(DISTINCT po_id), coalesce(sum(product_amount), 0)"
            " FROM orders.order_lines"
            " WHERE store = %s AND order_date >= %s AND order_date < %s",
            (store_name, win_start, win_end))
        row = cur.fetchone()
    return int(row[0]), round(float(row[1]), 2)


def _collect_store_kpi(store: dict, data_date, win_start: str, win_end: str,
                       names: dict, statuses: dict, last_names: dict) -> dict:
    """输入:店铺 + 日期 + 24h 窗口 + 影刀两 map + 历史名称 map → 输出:一行 KPI dict。"""
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
                    reports.iter_recon_records(store, recon_date))
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

    # 订单列双算对拍(对平后此段与上面的 iter_orders 拉取一并摘除,改用库值)
    orders_diff = None
    try:
        db_orders, db_sales = _pg_order_stats(name, win_start, win_end)
        api_orders = stats.get("total", 0)
        if db_orders != api_orders or abs(db_sales - sales) > 0.01:
            orders_diff = (f"API {api_orders} 单/${sales:.2f}"
                           f" vs 库 {db_orders} 单/${db_sales:.2f}")
            logger.info("对拍[订单列] %s:%s", name, orders_diff)
    except Exception as e:
        orders_diff = f"库算失败:{e}"
        logger.warning("对拍[订单列] %s 库算失败: %s", name, e)

    sid = settle["seller_id"]
    return {
        "_orders_diff": orders_diff,
        "store": name, "data_date": data_date,
        # 影刀今日值优先(真改名能跟上),缺席才延续库里最近非空值
        "seller_name": names.get(sid) or last_names.get(name) or None,
        # 影刀实测优先;没抓(停用店本就不进清单)则按店铺状态推导「不可售」
        "sales_status": (statuses.get(sid)
                         or kpi.derived_sales_status(settle["store_status"])),
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


# -p yingdao=<值> 三态。**不认识的值一律报错**,不许当成"关"静默跑过去:
# 打错一个字就悄悄少跑半条链,而摘要里看不出区别(2026-08-14 only_pending 的教训)。
_YINGDAO_MODES = {
    "": "",                                     # 不接影刀(默认)
    "0": "", "false": "", "no": "",
    "input": "input",                           # 只产出 input.json,不 spawn
    "1": "full", "true": "full", "yes": "full",  # 全链路
}


def _yingdao_mode(raw) -> str:
    """输入:-p yingdao 原值 → 输出:''(不接)/'input'(只产清单)/'full'(全链路)。

    'input' 是接入调试档:写完清单就返回,**不 spawn**。用它可以在旧
    walmart-kpi-daily 还开着的时候安全地跑——不 spawn 就没有双 spawn 互抢,
    也就不会把旧系统那次影刀的新鲜度校验搅坏。
    """
    key = str(raw if raw is not None else "").strip().lower()
    if key not in _YINGDAO_MODES:
        raise ValueError(
            f"yingdao 只接受 input(只产清单不 spawn)/1(全链路)/0(不接),收到:{raw!r}")
    return _YINGDAO_MODES[key]


def _yingdao_refresh(rows: list[dict], data_date, do_spawn: bool = True) -> str:
    """输入:当日已入库 KPI 行 + 是否 spawn → 输出:影刀链路结果行。

    三步:①写 input.json 影刀输入清单(空 sellerId 行过滤——A147 事故:影刀打开
    /seller//cp/shopall 会崩掉整条 RPA 循环)②spawn + 等 latest.json 新鲜
    ③回填当日卖家名称/销售状态。任一步失败只降级不报错:已入库值有跨日延续兜底。

    ⚠ 输入清单只含**本轮真跑到的店**,不与任何历史合并 —— 旧的飞书投影会把
    上次的行保留下来(sheet 是长存的),于是单店跑 `-p store=X` 也会让影刀去抓
    全部 49 家。文件每轮覆盖写,"这轮抓哪些"与"这轮拉了哪些"从此是同一件事。
    """
    st = yingdao.write_input(rows)
    written = st["written"]
    skipped = ",".join(f"{k} {st[k]}" for k in
                       ("no_seller_id", "inactive", "dup_seller_id") if st[k])
    line = (f"影刀:输入清单 {written} 店(未纳入:{skipped or '无'})"
            f" → {paths.yingdao_input_file()}")
    if st["unknown_status"]:     # 空状态照常纳入,但必须在摘要里看得见
        line += f",⚠ 状态为空仍纳入 {st['unknown_status']} 店(结算解析该查)"
    if not written:
        return line + ",无可抓店铺,跳过 spawn"
    if not do_spawn:        # -p yingdao=input:接入调试用,见下方 run() 注释
        return line + ",仅产出清单(yingdao=input),未 spawn"

    trigger = datetime.now(timezone.utc)
    if not yingdao.spawn():
        return line + ",spawn 失败/未配置,沿用旧值"
    if not yingdao.wait_fresh(trigger):
        return line + ",等待超时,沿用旧值(卖家名称跨日延续兜底)"

    names, statuses = _load_frontend(fresh_within_hours=1)
    updated = 0
    with db.pg_conn() as conn, conn.cursor() as cur:
        for row in rows:
            new_name = names.get(row["seller_id"] or "")
            new_status = statuses.get(row["seller_id"] or "")
            if not (new_name or new_status):
                continue
            if new_status:
                # 影刀回填是 sales_status 的第二条写入路径,状态 diff 也得跟着
                # (只给这一列,另两列 new 为空会被 diff 跳过;同日去重兜重复)
                store_events.record_kpi_diff(conn, row["store"], data_date,
                                             {"sales_status": new_status})
            cur.execute(
                "UPDATE ops.store_kpi_daily SET"
                " seller_name = COALESCE(%s, seller_name),"
                " sales_status = COALESCE(%s, sales_status), updated_at = now()"
                " WHERE store = %s AND data_date = %s",
                (new_name, new_status, row["store"], data_date))
            updated += cur.rowcount
    return line + f",抓取新鲜,回填 {updated} 店"


def _phase_kpi(store_list: list[dict], data_date, yingdao_mode: str = "") -> str:
    win_start, win_end = kpi.sales_window_utc()
    names, statuses = _load_frontend()
    last_names = _last_seller_names()
    ok, failed, diffs, rows_ok = [], [], [], []
    alerts: list[str] = []      # high 状态事件的摘要短句(店铺被封/资金冻结)
    with ThreadPoolExecutor(max_workers=min(_STORE_WORKERS, len(store_list))) as pool:
        futs = {pool.submit(_collect_store_kpi, s, data_date, win_start, win_end,
                            names, statuses, last_names): s["name"] for s in store_list}
        for f in as_completed(futs):
            name = futs[f]
            try:
                row = f.result()
                if row.pop("_orders_diff"):
                    diffs.append(name)
                with db.pg_conn() as conn:
                    # 状态 diff 在 upsert **之前**取上一条非空观测(同一事务里
                    # 顺序其实无所谓:_PREV_SQL 按 data_date < 今天 过滤,
                    # 今天这行怎么写都进不了"上一条")
                    landed = store_events.record_kpi_diff(conn, name,
                                                          data_date, row)
                    conn.execute(_KPI_UPSERT, row)
                highs = [e for e in landed if e["severity"] == "high"]
                if highs:
                    note = ";".join(store_events.brief(e) for e in highs)
                    if store_events.tro_signature(landed):
                        note += "(封店+资金冻结同日出现,**疑似 TRO 封店**)"
                    alerts.append(note)
                rows_ok.append(row)
                ok.append(name)
            except (_client.StoreDeadError, httpx.ProxyError) as e:
                logger.error("店铺 %s 凭证/代理失效跳过: %s", name, e)
                failed.append(f"{name}(凭证)")
            except Exception as e:
                logger.exception("店铺 %s KPI 采集失败: %s", name, e)
                failed.append(name)
    line = f"KPI:{len(ok)}/{len(store_list)} 店入库(窗口 {win_start}~{win_end})"
    if alerts:
        # 高危状态迁移必须在摘要点名(店铺事件账本 2026-08-30):飞书通知是
        # 人的第一入口,"写进库等 store_watch 扫"不算第一时间
        line += "\n🚨 " + " | ".join(sorted(alerts))
    if diffs:
        line += f",订单列对拍差异 {len(diffs)} 店(详见日志):{','.join(diffs)}"
    if failed:
        line += f",失败:{','.join(failed)}"
    if yingdao_mode and rows_ok:
        try:
            line += "\n" + _yingdao_refresh(rows_ok, data_date,
                                            do_spawn=yingdao_mode == "full")
        except Exception as e:
            logger.exception("影刀链路失败(已入库值不受影响): %s", e)
            line += f"\n影刀:链路失败({e}),沿用旧值"
    return line


# 看板表头:沿用旧表真实中文列名(与 kpi._HIST_HEADER_MAP 对称,运营零学习成本)。
# ⚠ 首两列 (店铺, 日期) 与旧「店铺KPI」表的 (日期, 店铺) 相反(所有者定稿
# 2026-08-15),顺序以 resources._KPI_BOARD_COLUMNS 为准,本列表只是它的中文名。
_BOARD_HEADER = ["店铺", "日期", "卖家名称", "partnerId", "sellerId", "店铺状态",
                 "支付状态", "销售状态", "在线商品", "有库存", "无库存", "昨日出单",
                 "昨日销售额($)", "准时送达(90%)", "取消率", "有效追踪(99%)",
                 "卖家回复率(95%)", "退款率", "差评率", "退货率", "未收到",
                 "账期销售额($)", "佣金", "退款金额", "期末余额", "迄今备用金($)",
                 "回款", "回款日", "收款方", "结算周期", "无Hold", "上期回款"]


_DATE_EPOCH = datetime(1899, 12, 30).date()     # 飞书/Excel 日期序列起点


def _date_serial(v):
    """输入:date/日期字符串 → 输出:日期序列 int(配合列 formatter 显示为日期);
    解析不了原样返回字符串。"""
    if hasattr(v, "toordinal") and not isinstance(v, datetime):
        return (v - _DATE_EPOCH).days
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return (datetime.strptime(s[:10], fmt).date() - _DATE_EPOCH).days
        except ValueError:
            continue
    return s


def _board_cell(field: str, v):
    """输入:字段名 + PG 值 → 输出:看板单元格(数字列写数字、日期列写序列值,
    None→空,bool→是/否)——所有者定稿 2026-08-08:数字/日期列用对应格式。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if field in ("data_date", "payout_date"):
        return _date_serial(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    return str(v)


_STORE_COL = resources.KPI_BOARD_OVERVIEW.columns.index("store")


def _by_store(rows):
    """输入:看板行(元组序列)→ 输出:按店铺重排后的列表(Python 稳定排序)。

    稳定 = 店内保留调用方给的次序(历史页即 SQL 的日期降序),不用在这里
    再排一次日期,也就不用关心 data_date 是 date 还是 None。
    """
    return sorted(rows, key=lambda r: stores_svc.sort_key(r[_STORE_COL]))


def _board_matrix(rows) -> list[list]:
    cols = resources.KPI_BOARD_OVERVIEW.columns
    return [[_board_cell(f, v) for f, v in zip(cols, r)] for r in rows]


def _board_formats(n_rows: int) -> list[tuple[str, str]]:
    """输入:数据行数 → 输出:[(范围, formatter)]:日期两列 + 数字列。"""
    cols = resources.KPI_BOARD_OVERVIEW.columns
    end = n_rows + 1        # 数据从第 2 行起
    items = []
    for i, f in enumerate(cols):
        letter = feishu._col_letter(i + 1)
        if f in ("data_date", "payout_date"):
            items.append((f"{letter}2:{letter}{end}", "yyyy/MM/dd"))
        elif f in ("items_online", "items_in_stock", "items_out_stock",
                   "orders_count"):
            items.append((f"{letter}2:{letter}{end}", "#,##0"))
        elif f in ("sales_amount", "otd_rate", "cancel_rate", "vtr_rate",
                   "srr_rate", "refund_rate", "negative_rate", "return_rate",
                   "inr_rate", "period_sales", "commission", "refund_amount",
                   "closing_balance", "reserve_to_date", "payout",
                   "prev_payout"):
            items.append((f"{letter}2:{letter}{end}", "#,##0.00"))
    return items


def _phase_board(history_days: int) -> str:
    """输入:历史窗口天数 → 输出:结果行。PG → 新 KPI 看板两页(整表重写)。

    总览 = 每店最新一行;历史 = 全店合一近 N 天。**两页一律按店铺排序**
    (所有者定稿 2026-08-15,与「店铺」列提到最左同一件事):历史页店内再按
    日期降序,同店的近 N 天连成一段,肉眼可直接看单店趋势——旧的
    `data_date DESC, store` 是按天横切,同一家店被打散在 90 个日期块里。

    ⚠ 店铺序由 `stores_svc.sort_key` 在 Python 里定,**不用 SQL ORDER BY**:
    PG 的 collation 会忽略中文主排序权重,把「谭总1」当「1」排(见该函数注释)。
    SQL 只负责日期降序,店铺序靠 Python 稳定排序叠上去。

    旧「店铺KPI」表 72 张分页停更归档(所有者定稿 2026-08-08);影刀输入已改
    本地 input.json,本仓不再写那张表。
    """
    cols = ", ".join(resources.KPI_BOARD_OVERVIEW.columns)
    with db.pg_conn() as conn, conn.cursor() as cur:
        # DISTINCT ON 要求 ORDER BY 以 store 打头,这里只用来取"每店最新一行",
        # 不作展示序——展示序在下面重排
        cur.execute(f"SELECT DISTINCT ON (store) {cols} FROM ops.store_kpi_daily"
                    " ORDER BY store, data_date DESC")
        overview = cur.fetchall()
        cur.execute(f"SELECT {cols} FROM ops.store_kpi_daily"
                    " WHERE data_date >= current_date - %s"
                    " ORDER BY data_date DESC", (history_days,))
        history = cur.fetchall()
    overview = _by_store(overview)
    history = _by_store(history)        # 稳定排序:店内保留 SQL 的日期降序
    n1 = feishu.sheet_overwrite(resources.KPI_BOARD_OVERVIEW,
                                [_BOARD_HEADER] + _board_matrix(overview))
    n2 = feishu.sheet_overwrite(resources.KPI_BOARD_HISTORY,
                                [_BOARD_HEADER] + _board_matrix(history))
    feishu.sheet_set_formatter(resources.KPI_BOARD_OVERVIEW,
                               _board_formats(len(overview)))
    feishu.sheet_set_formatter(resources.KPI_BOARD_HISTORY,
                               _board_formats(len(history)))
    return (f"看板:总览 {n1 - 1} 店,历史 {n2 - 1} 行(近 {history_days} 天)")


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
        # 状态变化改读事件账本(2026-08-30):此前是现拼"严格比昨天"的 JOIN,
        # 只看 store_status 一列、跨空档(昨天没跑出来)会漏。检测已收口到
        # services/store_events.record_kpi_diff 一处,这里只是消费当日事件。
        cur.execute(
            "SELECT store, event, severity, detail FROM ops.store_events"
            " WHERE detail->>'data_date' = %s::text"
            "   AND event = ANY(%s::text[]) ORDER BY severity, store",
            (str(data_date), list(store_events.CLASS)))
        status_changes = [
            {"store": s, "event": e, "severity": sev, "detail": d}
            for s, e, sev, d in cur.fetchall()]

    lines = [f"📊 沃尔玛店铺日报 {data_date}",
             f"店铺 {n_stores} 家 | 24h 订单 {int(n_orders)} 单 | 销售额 ${float(n_sales):,.2f}"]
    highs = [e for e in status_changes if e["severity"] == "high"]
    others = [e for e in status_changes if e["severity"] != "high"]
    if highs:
        lines.append("🚨 高危状态迁移:" + "; ".join(
            store_events.brief(e) for e in highs))
    if others:
        lines.append("⚠ 其余状态变化:" + "; ".join(
            store_events.brief(e) for e in others))
    if top_problems:
        lines.append("🆕 今日新增问题订单 TOP:" + "; ".join(
            f"{s} {c} 条" for s, c in top_problems))
    text = "\n".join(lines)

    if do_push:
        # ⚠ 别写成"日报已推送(webhook=未配置/失败)":sent=False 时一个字节都没发出去,
        # 说"已推送"就是假话。2026-08-16 所有者实见——日志里三处都写着未配置,
        # 摘要却报成功,人只看摘要就以为发了。摘要是人眼闸门,不许自我美化。
        if feishu.notify(text):
            return "日报已推送"
        return ("⚠ 日报**未发出**:FEISHU_WEBHOOK_URL 未配置或推送被拒"
                "(填 <DATA_ROOT>/.env 即生效)。内容:\n" + text)
    logger.info("日报预览(未推送,-p push=1 真发):\n%s", text)
    return "日报仅预览(未推送):\n" + text


def run(params: dict) -> str:
    """输入:params(phase/store/push)→ 输出:各阶段结果摘要。"""
    # 默认全链(所有者定稿 2026-08-16):kpi + 影刀 + 看板 + 推送。
    # 调度里只写工作流名就是完整一轮,不必逐个 phase 拼命令。
    phase = str(params.get("phase", "all"))
    if phase not in ("all", "kpi", "board", "push", "settle_debug"):
        if phase == "problems":     # 已摘出为独立工作流(2026-08-08)
            return "问题订单已独立:改跑 python cli.py perf_problems"
        if phase == "settlement":   # 已摘出为独立工作流(2026-08-10)
            return "对账明细已独立:改跑 python cli.py settlement_sync"
        return ("phase 只接受 all/kpi/board/push/"
                f"settle_debug,收到:{phase}")
    data_date = datetime.now(kpi.CN_TZ).date()

    if phase == "settle_debug":     # 结算解析诊断:单店打点关键键全部出现位置
        if not params.get("store"):
            return "settle_debug 需要 -p store=店铺名"
        store_list = stores_svc.load_stores([params["store"]])
        if not store_list:
            return f"店铺凭证未找到:{params['store']}"
        statement = reports.payment_statement(store_list[0])
        extracted = kpi.extract_settlement(statement)
        return (kpi.settlement_debug(statement)
                + "\n当前 extract_settlement 结果:\n"
                + "\n".join(f"  {k} = {v}" for k, v in extracted.items()))

    lines = []
    if phase in ("all", "kpi"):
        names = [params["store"]] if params.get("store") else None
        store_list = stores_svc.load_stores(names)
        if not store_list:
            return f"店铺凭证未找到:{params.get('store') or '(任一)'}"
        try:
            # 默认开影刀(所有者定稿 2026-08-16):卖家名称/销售状态是日报的
            # 固定两列,默认关等于每天少两列而且不报错。要关显式 -p yingdao=0
            mode = _yingdao_mode(params.get("yingdao", "1"))
        except ValueError as e:
            return str(e)
        lines.append(_phase_kpi(store_list, data_date, mode))
    if phase in ("all", "board"):
        try:
            lines.append(_phase_board(int(params.get("days", 90))))
        except LookupError as e:
            lines.append(f"看板:未登记跳过({e})")
    if phase in ("all", "push"):
        # 默认真发(同上):调度里漏写 -p push=1 的后果是日报每天不发而且报成功
        do_push = str(params.get("push", "1")) not in ("0", "false", "no")
        lines.append(_phase_push(data_date, do_push))
    return "\n".join(lines)
