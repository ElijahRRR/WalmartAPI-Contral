"""order_center_push — 订单中心四表投影到飞书(销售/售后/绩效/对账)。

用法:
  python cli.py order_center_push                  # 四表全推(销售/售后窗口 90 天)
  python cli.py order_center_push -p table=sales   # 只推一张(sales/returns/perf/settlement)
  python cli.py order_center_push -p days=180      # 自定窗口(销售按下单时间,售后按发起时间,
                                                   #   对账按最近入账日;绩效表全量,自身有界)

设计(与 catalog_sync 同一哲学:PG 权威,飞书是人看的投影,可随时重建):
- 同步语义 = api/feishu.sync_by_key 键对齐增量:新键建、旧键整行覆盖更新、消失键删。
  不用旧系统「清空全表再重写」——中途崩溃只是少更新几行,不会丢整表展示。
- 四表数据源:销售 ← orders.order_center 视图(单行汇齐售后/绩效/入账);
  售后 ← return_lines;绩效 ← perf_event_spans(still_active=仍在拖当前绩效分);
  对账 ← settlement_by_line 跨账期合并(运营要看的是「这单钱到没到」,
  逐账期明细留在 PG,按期核数走数据库查询)。
- 表格未在 .env 登记(FEISHU_ORDER_APP_TOKEN + 各 table_id)时逐表跳过并在摘要提示;
  建表字段规格见 docs/feishu_tables.md。
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from api import feishu
from registry import db, resources
from services import kpi

DANGEROUS = False

logger = logging.getLogger("workflows.order_center_push")

_SALES_SQL = """
SELECT order_line_id, store, po_id, line_number, customer_order_id, sku,
       product_name, qty, sale_status, status_date, order_date,
       product_amount, shipping_amount, cancel_reason, refund_amount,
       carrier, tracking_no, tracking_url,
       return_status, refund_status, return_total,
       perf_metrics, settled_net, settle_status, audit_status
FROM orders.order_center
WHERE order_date >= now() - make_interval(days => %s)
"""

_RETURNS_SQL = """
SELECT r.return_order_id, r.order_line_id, r.store, r.po_id, r.sku,
       l.product_name, r.return_status, r.refund_status, r.return_method,
       r.is_keep_it, r.refund_total, r.return_reason, r.qty, r.refunded_qty,
       r.carrier, r.tracking_no, r.return_created, r.return_by
FROM orders.return_lines r
LEFT JOIN orders.order_lines l USING (order_line_id)
WHERE r.return_created >= now() - make_interval(days => %s)
"""

# 绩效表全量:行数按 (po, metric) 去重,增长缓慢,自身有界。
# sku 优先取回填后的订单行,未回填(老 PO)退回报表原始行里的 sku
_PERF_SQL = """
SELECT s.store, s.po_id, s.metric, l.sku AS line_sku, e.sku AS event_sku,
       l.product_name, s.first_period, s.last_period, s.periods_seen,
       s.ever_accountable, s.still_active
FROM orders.perf_event_spans s
LEFT JOIN orders.order_lines l ON l.order_line_id = s.order_line_id
LEFT JOIN LATERAL (
    SELECT sku FROM orders.perf_events e
    WHERE e.po_id = s.po_id AND e.metric = s.metric
      AND e.sku IS NOT NULL AND e.sku <> ''
    LIMIT 1) e ON true
"""

_SETTLE_SQL = """
SELECT b.order_line_id, b.store, b.po_id, b.line_number, l.sku,
       l.product_name, b.net_amount, b.gross_amount, b.product_amount,
       b.commission_amount, b.periods, b.last_settle_date, b.settle_status
FROM orders.settlement_by_line b
LEFT JOIN orders.order_lines l USING (order_line_id)
WHERE b.last_settle_date IS NULL
   OR b.last_settle_date >= current_date - %s
"""


def _cell(v):
    """输入:PG 单元值 → 输出:bitable 可写值(日期→毫秒,Decimal→float,None 原样)。

    None 必须保留并显式写入(sync_by_key 更新语义:省略字段=保留飞书旧值,
    送 null 才是清空)。
    """
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, datetime):
        return int(v.timestamp() * 1000)
    if isinstance(v, date):
        return int(datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
                   .timestamp() * 1000)
    if isinstance(v, Decimal):
        return float(v)
    return v


def _fetch(sql: str, args: tuple) -> list[dict]:
    """输入:SQL + 参数 → 输出:行 dict 列表(列名 → 值)。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _push_sales(days: int) -> str:
    t = resources.ORDER_SALES
    f = t.fields
    desired = {}
    for r in _fetch(_SALES_SQL, (days,)):
        desired[r["order_line_id"]] = {
            f.key: r["order_line_id"], f.store: r["store"], f.po_id: r["po_id"],
            f.line_number: r["line_number"],
            f.customer_order_id: r["customer_order_id"],
            f.sku: r["sku"], f.product_name: r["product_name"],
            f.qty: _cell(r["qty"]), f.sale_status: r["sale_status"],
            f.status_date: _cell(r["status_date"]),
            f.order_date: _cell(r["order_date"]),
            f.product_amount: _cell(r["product_amount"]),
            f.shipping_amount: _cell(r["shipping_amount"]),
            f.cancel_reason: r["cancel_reason"],
            f.refund_amount: _cell(r["refund_amount"]),
            f.carrier: r["carrier"], f.tracking_no: r["tracking_no"],
            f.tracking_url: r["tracking_url"],
            f.return_status: r["return_status"],
            f.refund_status: r["refund_status"],
            f.return_total: _cell(r["return_total"]),
            f.perf_metrics: r["perf_metrics"],
            f.settled_net: _cell(r["settled_net"]),
            f.settle_status: r["settle_status"],
            f.audit_status: r["audit_status"],
        }
    c, u, d = feishu.sync_by_key(t, f.key, desired)
    return f"销售订单:{len(desired)} 行(窗口 {days} 天),新建 {c} 更新 {u} 删除 {d}"


def _push_returns(days: int) -> str:
    t = resources.ORDER_RETURNS
    f = t.fields
    desired = {}
    for r in _fetch(_RETURNS_SQL, (days,)):
        key = f"{r['return_order_id']}|{r['order_line_id']}"
        desired[key] = {
            f.key: key, f.rma: r["return_order_id"],
            f.order_line_id: r["order_line_id"], f.store: r["store"],
            f.po_id: r["po_id"], f.sku: r["sku"],
            f.product_name: r["product_name"],
            f.return_status: r["return_status"],
            f.refund_status: r["refund_status"],
            f.return_method: r["return_method"],
            f.is_keep_it: r["is_keep_it"],
            f.refund_total: _cell(r["refund_total"]),
            f.return_reason: r["return_reason"],
            f.qty: _cell(r["qty"]), f.refunded_qty: _cell(r["refunded_qty"]),
            f.carrier: r["carrier"], f.tracking_no: r["tracking_no"],
            f.return_created: _cell(r["return_created"]),
            f.return_by: _cell(r["return_by"]),
        }
    c, u, d = feishu.sync_by_key(t, f.key, desired)
    return f"售后订单:{len(desired)} 行(窗口 {days} 天),新建 {c} 更新 {u} 删除 {d}"


def _push_perf() -> str:
    t = resources.ORDER_PERF
    f = t.fields
    desired = {}
    for r in _fetch(_PERF_SQL, ()):
        key = f"{r['po_id']}|{r['metric']}"
        desired[key] = {
            f.key: key, f.store: r["store"], f.po_id: r["po_id"],
            # 指标推 emoji 展示名(日报同款契约),未知指标原样
            f.metric: kpi.METRIC_LABELS.get(r["metric"], r["metric"]),
            f.sku: r["line_sku"] or r["event_sku"],
            f.product_name: r["product_name"],
            f.first_period: r["first_period"], f.last_period: r["last_period"],
            f.periods_seen: _cell(r["periods_seen"]),
            f.ever_accountable: r["ever_accountable"],
            f.still_active: r["still_active"],
        }
    c, u, d = feishu.sync_by_key(t, f.key, desired)
    return f"绩效订单:{len(desired)} 行(全量),新建 {c} 更新 {u} 删除 {d}"


def _push_settlement(days: int) -> str:
    t = resources.ORDER_SETTLE
    f = t.fields
    desired = {}
    for r in _fetch(_SETTLE_SQL, (days,)):
        desired[r["order_line_id"]] = {
            f.key: r["order_line_id"], f.store: r["store"], f.po_id: r["po_id"],
            f.line_number: r["line_number"], f.sku: r["sku"],
            f.product_name: r["product_name"],
            f.net_amount: _cell(r["net_amount"]),
            f.gross_amount: _cell(r["gross_amount"]),
            f.product_amount: _cell(r["product_amount"]),
            f.commission_amount: _cell(r["commission_amount"]),
            f.periods: _cell(r["periods"]),
            f.last_settle_date: _cell(r["last_settle_date"]),
            f.settle_status: r["settle_status"],
        }
    c, u, d = feishu.sync_by_key(t, f.key, desired)
    return f"对账明细:{len(desired)} 行(窗口 {days} 天),新建 {c} 更新 {u} 删除 {d}"


_TABLES = ("sales", "returns", "perf", "settlement")


def run(params: dict) -> str:
    """输入:params(可选 table/days)→ 输出:各表同步结果摘要。"""
    only = str(params.get("table", "")).strip()
    if only and only not in _TABLES:
        return f"table 只接受 {'/'.join(_TABLES)},收到:{only}"
    days = int(params.get("days", 90))

    jobs = {
        "sales": lambda: _push_sales(days),
        "returns": lambda: _push_returns(days),
        "perf": _push_perf,
        "settlement": lambda: _push_settlement(days),
    }
    lines, failed = [], []
    for name in (only,) if only else _TABLES:
        try:
            lines.append(jobs[name]())
        except LookupError as e:
            lines.append(f"{name}:跳过(未登记)— {e}")
        except feishu.FeishuError as e:
            # 单表失败不挡其余表;最后统一抛错让本次运行记为 failed
            logger.exception("表 %s 同步失败: %s", name, e)
            lines.append(f"{name}:失败 — {e}")
            failed.append(name)
    if failed:
        raise RuntimeError("部分表同步失败:" + ";".join(lines))
    return "\n".join(lines)
