"""settlement_sync — 沃尔玛对账明细(从 daily_report 摘出,独立成流)。

用法:
  python cli.py settlement_sync                    # 全店,单店最多补 6 个账期
  python cli.py settlement_sync -p store=A085朱丽霖 # 单店
  python cli.py settlement_sync -p periods=99      # 首次建库可放开账期上限
  python cli.py settlement_sync -p backfill_payouts=1 -p periods=99
                                                  # 一次性:给老账期补「累计回款」

**为什么从 daily_report 摘出**(所有者定稿 2026-08-10,与 perf_problems 同一处理):
两者节奏根本不同——KPI 是**每日**指标,对账账期是**双周**发布(实证账期序列
06/02 → 06/16 → 06/30 → 07/14 → 07/28)。绑在一起等于每天为一件十四天才变一次
的事把 48 家店全扫一遍;拆开后日报不再等这条链,对账也可以按账期节奏挂调度。

取数语义(逐条都有来历,别简化):
- **关账快照不可变**:已入库的账期永不重拉(`DISTINCT period` + recon_done 台账
  双判据——入库过滤后可能整期 0 行落库,只看 DISTINCT period 会把处理过的期
  当缺失无限重拉)。
- **v3 身份 = PO+SKU**:CSV 缺 SKU 列时按 (po, 行号) 反查订单行补 SKU。
- **烂账治理**:订单不在库(早于建库窗口)的对账行不入库,只计数。
- **累计回款**(2026-08-31,所有者:「我需要累计回款,就沃尔玛总共已经付给我
  的钱」):同一次下载**顺手**把该期 PaymentSummary 行的 Total Payable 记进
  `ops.store_settlements`,累计 = 各账期之和(daily_report 直接读)。
  ⚠ 不能拿 `settlement_lines` 求和代替:那张表按订单行聚合、且**过滤掉了
  订单不在库的行**,还不含账期级的费用/调整,加起来不是"沃尔玛付了多少"。
  ⚠ 也不能把每天的 `payout` 加起来:那是"当前待打款"的快照,同一笔钱在打款
  前天天出现,按天求和 = 同一笔重复计几十次。结算按**账期**发生,累计的
  唯一正确单位是账期。
  历史账期(本改动之前已入库的)没有这个数,用 `-p backfill_payouts=1`
  重下一遍补 —— 这是「关账快照不可变、已入库永不重拉」的**唯一例外**,
  显式开关、一次性,补完就不再触发。
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from api import _client, reports
from registry import db
from services import kpi, order_center
from services import order_lines, settlements, store_retry, stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.settlement_sync")

_STORE_WORKERS = stores_svc.STORE_WORKERS   # 唯一出处在 services/stores


def _sync(store_list: list[dict], periods_limit: int,
          backfill_payouts: bool = False) -> str:
    """输入:店铺列表 + 单店账期上限 → 输出:结果摘要(一行)。

    按店拉缺失账期(关账快照不可变,已入库不重拉)→ orders.settlement_lines。
    """
    total_periods, total_lines, total_no_sku, failed = 0, 0, 0, []
    total_payouts = 0

    def _one_store(store: dict) -> tuple[int, int, int]:
        name = store["name"]
        with db.pg_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT period FROM orders.settlement_lines "
                        "WHERE store = %s", (name,))
            have = {r[0] for r in cur.fetchall()}
            # 台账并入:入库过滤后可能整期 0 行落库,只看 DISTINCT period
            # 会把处理过的期当缺失无限重拉(services.order_lines 台账注释)
            have |= order_lines.recon_done_periods(conn, name)
            # v3 身份 = PO+SKU:CSV 缺 SKU 列时按 (po,行号) 反查订单行补 SKU
            cur.execute("SELECT po_id, line_number, sku FROM orders.order_lines "
                        "WHERE store = %s", (name,))
            sku_lookup = {(po, ln): sku for po, ln, sku in cur.fetchall() if sku}
        available = reports.available_recon_dates(store)
        todo = order_lines.pick_new_periods(available, have, periods_limit)
        if backfill_payouts:
            # 已入库但没记过 Total Payable 的老账期:重下一遍**只为补这个数**。
            # 走同一条循环(行入库是幂等 upsert、mark_recon_done 也是),
            # 不另写一条简化路径 —— 双轨的两边迟早会不一样。
            with db.pg_conn() as conn:
                have_payout = settlements.known_dates(conn, name)
            todo = todo + [d for d in available
                           if d in have and d not in have_payout]
            todo = todo[:periods_limit] if periods_limit else todo
        written = no_sku = unlinked = payouts = 0
        for period in todo:
            rows = list(reports.iter_recon_records(store, period))
            recs, skipped = order_lines.aggregate_settlement_lines(
                name, rows, period, sku_lookup)
            no_sku += skipped
            with db.pg_conn() as conn:
                # 烂账治理:订单不在库(早于建库窗口)的对账行不入库
                recs, drop = order_lines.drop_unlinked(conn, recs)
                unlinked += drop
                written += order_lines.upsert_settlement_lines(conn, recs)
                # 累计回款的唯一数据源:同一份 rows,不额外下载
                settlements.record(conn, name, period,
                                   kpi.payment_summary_total(rows))
                payouts += 1
                order_lines.mark_recon_done(conn, name, [period])
        if unlinked:
            logger.info("店铺 %s:%d 组对账行订单不在库,未入库", name, unlinked)
        return len(todo), written, no_sku, payouts

    with ThreadPoolExecutor(max_workers=min(_STORE_WORKERS, len(store_list))) as pool:
        futs = {pool.submit(_one_store, s): s["name"] for s in store_list}
        for f in as_completed(futs):
            name = futs[f]
            try:
                periods, written, no_sku, payouts = f.result()
                total_periods += periods
                total_lines += written
                total_no_sku += no_sku
                total_payouts += payouts
            except (_client.StoreDeadError, httpx.ProxyError) as e:
                # 分诊词跟 store_retry.diagnose 同口径(2026-08-26):凭证死
                # 与代理故障的处置完全不同(修凭证表 vs 找代理商),
                # get_token 收口后 SOCKS 报错到这里是 StoreProxyError(代理)
                cls = store_retry.diagnose(e)
                logger.error("店铺 %s %s失效跳过: %s", name, cls, e)
                failed.append(f"{name}({cls})")
            except Exception as e:
                logger.exception("店铺 %s 对账明细失败: %s", name, e)
                failed.append(f"{name}({store_retry.diagnose(e)})")
    line = (f"对账明细:{len(store_list) - len(failed)}/{len(store_list)} 店,"
            f"新账期 {total_periods} 个,入库 {total_lines} 行,"
            f"回款入账 {total_payouts} 期")
    if total_no_sku:
        line += f",SKU 解析失败跳过 {total_no_sku} 组"
    if failed:
        line += f",失败:{','.join(failed)}"
    return line


def run(params: dict) -> str:
    """输入:params(可选 store/periods)→ 输出:对账拉取摘要。"""
    names = [params["store"]] if params.get("store") else None
    store_list = stores_svc.load_stores(names)
    if not store_list:
        return f"店铺凭证未找到:{params.get('store') or '(任一)'}"
    out = _sync(store_list, int(params.get("periods", 6)),
                backfill_payouts=str(params.get("backfill_payouts", "")) == "1")
    # 跑完就写飞书。窗口给宽:对账按最近入账日筛,账期是双周发布的,
    # 90 天才盖得住"上上个账期的行今天才补齐"这种情况
    return out + "\n" + order_center.push_after(
        order_center.BY_WORKFLOW["settlement_sync"], days=180)
