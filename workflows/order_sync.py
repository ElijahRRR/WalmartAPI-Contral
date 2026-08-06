"""order_sync — 订单拉取入订单中心(plan #4 order_audit 的取数前半)。

用法:
  python cli.py order_sync                     # 全部启用店铺,近 45 天
  python cli.py order_sync -p store=A085朱丽霖  # 单店
  python cli.py order_sync -p days=90          # 自定窗口
  python cli.py order_sync -p workers=8        # 跨店并发

每店:GET /v3/orders(createdStartDate=now-days)→ 展开订单行 → upsert
orders.order_lines。窗口全量重拉而非游标增量:订单状态在创建后持续变化
(Created→Shipped→Delivered/Cancelled),按创建时间增量会漏老订单的状态迁移,
窗口重拉 + 幂等 upsert 天然覆盖(旧系统同样按日全刷 45 天)。

审核列(audit_status/audit_detail/audited_at)不在本工作流的 upsert 列内,
重拉不会冲掉审核结论——审核规则与采集对接后由 order_audit 补全。
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import httpx

from api import _client
from api import orders as orders_api
from registry import db
from services import order_lines as ol
from services import stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.order_sync")


def _sync_one_store(store: dict, created_start: str) -> dict:
    name = store["name"]
    rows: list[dict] = []
    stats: dict = {}
    for order in orders_api.iter_orders(store, created_start=created_start, stats=stats):
        rows.extend(ol.extract_order_lines(name, order))
    with db.pg_conn() as conn:
        written = ol.upsert_order_lines(conn, rows)
    return {"store": name, "orders": stats.get("total", 0), "lines": written}


def run(params: dict) -> str:
    """输入:params(可选 store/days/workers)→ 输出:各店拉取统计摘要。"""
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
                logger.exception("店铺 %s 订单拉取失败: %s", name, e)
                failed.append(f"{name}({e})")

    total_lines = sum(r["lines"] for r in results)
    lines = [f"order_sync:{len(results)}/{len(store_list)} 店完成"
             f"(窗口 {days} 天),订单行入库 {total_lines}"]
    if dead:
        lines.append(f"凭证失效跳过:{','.join(dead)}")
    if failed:
        lines.append(f"失败:{'; '.join(failed)}")
    return "\n".join(lines)
