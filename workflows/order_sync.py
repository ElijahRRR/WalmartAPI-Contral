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

并发形态(2026-08-13,蓝图 §6.3 async 变体落地):跨店并发由
api/orders.fetch_orders_bulk 承担(asyncio 藏在 api 层内部,默认 12 店
同时拉,网络与入库在线程池重叠);本文件只提供持久化回调,保持同步世界。

审核列(audit_status/audit_detail/audited_at)不在本工作流的 upsert 列内,
重拉不会冲掉审核结论——审核规则与采集对接后由 order_audit 补全。
"""

import logging
from datetime import datetime, timedelta, timezone

from api import _client, orders as orders_api
from registry import db
from services import order_center, store_retry
from services import order_lines as ol
from services import stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.order_sync")


def _persist(store: dict, orders: list[dict]) -> int:
    """输入:店铺 + 该店全部订单 → 输出:入库行数(fetch_orders_bulk 回调)。"""
    rows: list[dict] = []
    for order in orders:
        rows.extend(ol.extract_order_lines(store["name"], order))
    with db.pg_conn() as conn:
        return ol.upsert_order_lines(conn, rows)


def run(params: dict) -> str:
    """输入:params(可选 store/days/workers)→ 输出:各店拉取统计摘要。"""
    names = [params["store"]] if params.get("store") else None
    store_list = stores_svc.load_stores(names)
    if not store_list:
        return f"店铺凭证未找到:{params.get('store') or '(全部)'}"
    days = int(params.get("days", 45))
    workers = int(params.get("workers", stores_svc.STORE_WORKERS))
    created_start = (datetime.now(timezone.utc) - timedelta(days=days)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")

    results, dead, failed = orders_api.fetch_orders_bulk(
        store_list, created_start=created_start, concurrency=workers,
        handler=_persist)

    # 标准②(所有者定稿 2026-08-26,补齐:此前本文件只做隔离+分诊,同链
    # returns_sync 是全套):失败店跑完别人后串行补试一遍。单一落地路径 ——
    # 补试重调**同一个** fetch_orders_bulk(单店入参),店间串行由一次一店成立
    absent: list[tuple[str, str]] = []
    gate_note = ""
    if failed:
        by_name = {s["name"]: s for s in store_list}

        def _retry_one(store):
            r2, dead2, failed2 = orders_api.fetch_orders_bulk(
                [store], created_start=created_start, concurrency=1,
                handler=_persist)
            if dead2:   # 补试中才暴露的凭证死:还原成异常交标准件归类
                raise _client.StoreDeadError(store["name"], "401/403(补试)")
            if failed2:
                raise failed2[0][1]
            return r2[0]

        recovered, still, gate_note = store_retry.serial_second_pass(
            [(by_name[n], e) for n, e in failed], _retry_one,
            total_stores=len(store_list))
        results.extend(r for _s, r in recovered)
        for s, e in still:
            cls = store_retry.diagnose(e)
            if cls == "凭证失效":
                dead.append(s["name"])
            else:
                absent.append((s["name"], cls))

    total_lines = sum(r["lines"] for r in results)
    # 标准③:缺席店点名进摘要**首行**(链通知只发成功步骤的首行);
    # 订单窗口全量重拉 + 幂等 upsert,缺席店下轮整点自然补上,无需水位避让
    lines = [f"order_sync:{len(results)}/{len(store_list)} 店完成"
             f"(窗口 {days} 天),订单行入库 {total_lines}"
             + (f";⚠ 缺席 {len(absent)} 店:"
                + ",".join(f"{n}({c})" for n, c in absent)
                + ("——超补试规模闸未补试(疑似系统性故障)" if gate_note
                   else "——已串行补试仍失败")
                + ",本轮不炸链(下轮整点自然重拉)"
                if absent else "")]
    if gate_note:
        lines.append(gate_note)
    if dead:
        lines.append(f"凭证失效跳过:{','.join(dead)}")
    if not results:
        # ⚠ 零店完成不许报成功(2026-08-26 补齐,与 returns_sync 同款闸;
        # 此前同一条 order_chain 里两步不对称):全部店都没拉到 = 凭证表
        # 整体出问题或请求形状被改坏,报成功没人会来看,订单数据静默停更
        lines.append("⚠ 零店完成 —— 本轮没有同步任何订单数据")
        raise RuntimeError("\n".join(lines))
    # 跑完就写飞书(所有者定稿 2026-08-16:已对接飞书表的执行完就写,
    # 不做成单独的)。**永不因投影失败而失败** —— 订单已经落 PG 了
    lines.append(order_center.push_after(
        order_center.BY_WORKFLOW["order_sync"], days=max(days, 90)))
    return "\n".join(lines)
