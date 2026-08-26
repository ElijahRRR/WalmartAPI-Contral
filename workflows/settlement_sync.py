"""settlement_sync — 沃尔玛对账明细(从 daily_report 摘出,独立成流)。

用法:
  python cli.py settlement_sync                    # 全店,单店最多补 6 个账期
  python cli.py settlement_sync -p store=A085朱丽霖 # 单店
  python cli.py settlement_sync -p periods=99      # 首次建库可放开账期上限

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
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

from api import _client, reports
from registry import db
from services import order_center
from services import order_lines, store_retry, stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.settlement_sync")

_STORE_WORKERS = stores_svc.STORE_WORKERS   # 唯一出处在 services/stores


def _sync(store_list: list[dict], periods_limit: int) -> str:
    """输入:店铺列表 + 单店账期上限 → 输出:结果摘要(一行)。

    按店拉缺失账期(关账快照不可变,已入库不重拉)→ orders.settlement_lines。
    """
    total_periods, total_lines, total_no_sku, failed = 0, 0, 0, []

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
        todo = order_lines.pick_new_periods(
            reports.available_recon_dates(store), have, periods_limit)
        written = no_sku = unlinked = 0
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
                order_lines.mark_recon_done(conn, name, [period])
        if unlinked:
            logger.info("店铺 %s:%d 组对账行订单不在库,未入库", name, unlinked)
        return len(todo), written, no_sku

    with ThreadPoolExecutor(max_workers=min(_STORE_WORKERS, len(store_list))) as pool:
        futs = {pool.submit(_one_store, s): s["name"] for s in store_list}
        for f in as_completed(futs):
            name = futs[f]
            try:
                periods, written, no_sku = f.result()
                total_periods += periods
                total_lines += written
                total_no_sku += no_sku
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
            f"新账期 {total_periods} 个,入库 {total_lines} 行")
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
    out = _sync(store_list, int(params.get("periods", 6)))
    # 跑完就写飞书。窗口给宽:对账按最近入账日筛,账期是双周发布的,
    # 90 天才盖得住"上上个账期的行今天才补齐"这种情况
    return out + "\n" + order_center.push_after(
        order_center.BY_WORKFLOW["settlement_sync"], days=180)
