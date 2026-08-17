"""returns_sync — 售后单同步入订单中心(plan #2,替代旧 售后订单同步)。

用法:
  python cli.py returns_sync                     # 全部启用店铺,近 45 天(增量)
  python cli.py returns_sync -p store=A085朱丽霖  # 单店
  python cli.py returns_sync -p days=90          # 建库首拉用 90 天
  python cli.py returns_sync -p workers=8

每店:GET /v3/returns(创建时间窗,Start/End 成对——实证只传 start 返 400)
→ 展开 returnOrderLine → upsert orders.return_lines(主键 RMA×订单行)。
窗口全量重拉:售后状态(INITIATED→DELIVERED→CLOSED/退款)在创建后持续变化,
按创建时间增量会漏老单状态迁移;45 天窗口 + 幂等 upsert 覆盖(所有者定稿:
售后正常 2~4 周走到终态,二次售后是新 RMA 新记录;建库首拉才需要 90 天)。
旧系统"整表覆盖残留旧行"缺陷在 PG 按键 upsert 下天然不存在(plan #2 备注)。
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import httpx

from api import _client
from api import returns as returns_api
from registry import db
from services import order_center
from services import order_lines as ol
from services import stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.returns_sync")


def _sync_one_store(store: dict, created_start: str) -> dict:
    name = store["name"]
    rows: list[dict] = []
    n_orders = 0
    for return_order in returns_api.iter_returns(store, created_start=created_start):
        n_orders += 1
        rows.extend(ol.flatten_return_lines(name, return_order))
    with db.pg_conn() as conn:
        # 烂账治理:订单不在库(早于建库拉单窗口)的售后行不入库
        rows, dropped = ol.drop_unlinked(conn, rows)
        written = ol.upsert_return_lines(conn, rows)
    if dropped:
        logger.info("店铺 %s:%d 条售后行订单不在库,未入库", name, dropped)
    return {"store": name, "returns": n_orders, "lines": written,
            "dropped": dropped}


def run(params: dict) -> str:
    """输入:params(可选 store/days/workers)→ 输出:各店售后同步统计摘要。"""
    names = [params["store"]] if params.get("store") else None
    store_list = stores_svc.load_stores(names)
    if not store_list:
        return f"店铺凭证未找到:{params.get('store') or '(全部)'}"
    days = int(params.get("days", 45))
    workers = int(params.get("workers", 8))
    created_start = (datetime.now(timezone.utc) - timedelta(days=days)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")

    results, dead, failed = [], [], []
    with ThreadPoolExecutor(max_workers=min(workers, len(store_list))) as pool:
        futs = {pool.submit(_sync_one_store, s, created_start): s["name"]
                for s in store_list}
        for f in as_completed(futs):
            name = futs[f]
            try:
                results.append(f.result())
            except (_client.StoreDeadError, httpx.ProxyError) as e:
                logger.error("店铺 %s 凭证/代理失效跳过: %s", name, e)
                dead.append(name)
            except Exception as e:
                logger.exception("店铺 %s 售后同步失败: %s", name, e)
                failed.append(f"{name}({e})")

    total = sum(r["lines"] for r in results)
    total_dropped = sum(r.get("dropped", 0) for r in results)
    lines = [f"returns_sync:{len(results)}/{len(store_list)} 店完成"
             f"(窗口 {days} 天),售后行入库 {total}"]
    if total_dropped:
        lines[0] += f",订单不在库丢弃 {total_dropped}"
    if dead:
        lines.append(f"凭证失效跳过:{','.join(dead)}")
    if failed:
        lines.append(f"失败:{'; '.join(failed)}")
    if not results:
        # ⚠ 零店完成不许报成功(2026-08-17 补,与 catalog_sync 同款闸)。
        # 「凭证失效跳过」按设计不进 failed —— 一家店坏了不该拖垮整轮;但
        # **全部**店都被跳过意味着这一轮什么都没同步。同日 api/_client 把换
        # token 阶段的 **400** 也归成 StoreDeadError(沃尔玛凭证被拒回的是 400),
        # 于是"请求形状被改坏"这类全店故障从"整轮报错"变成了"整轮跳过" ——
        # 没有这道闸就是报成功。本链**每小时**跑,静默陈旧起来最难发现。
        lines.append("⚠ 零店完成 —— 本轮没有同步任何售后数据")
        raise RuntimeError("\n".join(lines))
    # 跑完就写飞书(同 order_sync;投影失败只告警,售后行已落 PG)
    lines.append(order_center.push_after(
        order_center.BY_WORKFLOW["returns_sync"], days=max(days, 90)))
    return "\n".join(lines)
