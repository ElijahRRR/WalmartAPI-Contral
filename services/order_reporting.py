"""日报订单窗口补齐与历史两列校正;不改变 WFS 范围或其它 KPI。"""

import logging
from datetime import datetime, time, timedelta
from decimal import Decimal

from api import orders
from registry import db
from services import kpi, order_ingest

logger = logging.getLogger(__name__)


def window(data_date):
    """输入:日报日期 → 输出:该日 06:30 截止的 24 小时 UTC 窗口。"""
    return kpi.sales_window_utc(datetime.combine(
        data_date, time(kpi.WINDOW_END_HOUR, kpi.WINDOW_END_MINUTE), kpi.CN_TZ))


def pg_stats(store, start, end):
    """输入:店铺和窗口 → 输出:库内 PO 数与 PRODUCT 金额。"""
    with db.pg_conn() as conn:
        row = conn.execute(
            "SELECT count(DISTINCT po_id), coalesce(sum(product_amount), 0)"
            " FROM orders.order_lines WHERE store=%s AND order_date>=%s AND order_date<%s",
            (store, start, end)).fetchone()
    return int(row[0]), Decimal(str(row[1])).quantize(Decimal("0.01"))


def fetch_window(store, start, end):
    """输入:店铺和窗口 → 输出:(完整订单列表, PO 数, 金额);失败/截断抛错。"""
    stats = {}
    rows = list(orders.iter_orders(store, created_start=start, created_end=end, stats=stats))
    pos = [str(r.get("purchaseOrderId") or "") for r in rows]
    total = stats.get("total")
    # 重复 cursor/页数上限会正常结束生成器,必须额外校验完整性。
    if total is None or int(total) != len(rows) or len(set(pos)) != len(rows) or "" in pos:
        raise RuntimeError(f"{store['name']} 订单窗口不完整:API 总数 {total},"
                           f"实际 {len(rows)},唯一 PO {len(set(pos))}")
    amount = sum((Decimal(str(orders.order_product_sales(r))) for r in rows), Decimal(0))
    return rows, len(rows), amount.quantize(Decimal("0.01"))


def sync_window(store, start, end):
    """输入:店铺和窗口 → 输出:API 单量/金额;完整读取后用共用入口补入库。"""
    rows, count, amount = fetch_window(store, start, end)
    order_ingest.persist(store, rows)
    return count, amount


_UPDATE = """
UPDATE ops.store_kpi_daily SET orders_count=%s, sales_amount=%s, updated_at=now()
WHERE store=%s AND data_date=%s
  AND orders_count IS NOT DISTINCT FROM %s
  AND sales_amount IS NOT DISTINCT FROM %s
"""


def reconcile_history(store_list, data_date, *, dates=None, days=7, dry_run=False):
    """输入:启用店铺/日报日期/可选精确日期 → 输出:历史校正摘要;仅改订单两列。

    先比库值与历史 KPI,仅差异行调用同窗口 API;API 与补齐后的库值一致才更新。
    按旧两列值比较更新,并发变更时拒绝覆盖。失败只保留旧值并点名。
    """
    if not 1 <= days <= 45:
        raise ValueError("history_days 必须在 1~45 天之间")
    dates = sorted(set(dates if dates is not None else
                       [data_date - timedelta(days=i) for i in range(1, days + 1)]))
    if any(d >= data_date for d in dates):
        raise ValueError("历史校正日期必须早于今天")
    stores = {s["name"]: s for s in store_list}
    with db.pg_conn() as conn:
        rows = conn.execute(
            "SELECT store,data_date,orders_count,sales_amount FROM ops.store_kpi_daily"
            " WHERE store=ANY(%s) AND data_date=ANY(%s) ORDER BY data_date,store",
            (list(stores), dates)).fetchall()
    changes, failures = [], []
    for name, day, old_count, old_amount in rows:
        try:
            start, end = window(day)
            old = (old_count, old_amount)
            if pg_stats(name, start, end) == old:
                continue
            if dry_run:
                _, count, amount = fetch_window(stores[name], start, end)
            else:
                count, amount = sync_window(stores[name], start, end)
            if pg_stats(name, start, end) != (count, amount):
                raise RuntimeError("API 与库仍不一致,保留原值")
            if (count, amount) == old:
                continue
            note = f"{day} {name}:{old_count} 单/${old_amount} → {count} 单/${amount}"
            logger.info("历史订单校正%s:%s", "预览" if dry_run else "", note)
            if not dry_run:
                with db.pg_conn() as conn:
                    changed = conn.execute(_UPDATE, (count, amount, name, day,
                                                     old_count, old_amount)).rowcount
                    if changed != 1:
                        raise RuntimeError("历史行已被其它执行修改,未覆盖")
            changes.append(note)
        except Exception as e:
            logger.exception("历史订单校正失败:%s %s", day, name)
            failures.append(f"{day} {name}:{e}")
    summary = f"历史订单校正{'预览' if dry_run else ''}:检查 {len(rows)} 行,{'拟改' if dry_run else '修正'} {len(changes)} 行"
    if changes:
        summary += "\n" + "\n".join(changes)
    if failures:
        summary += "\n⚠ 未校正:" + ";".join(failures)
    return summary
