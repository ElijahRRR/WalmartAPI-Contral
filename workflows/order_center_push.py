"""order_center_push — 订单中心投影到飞书「订单中心V1」应用(六表)。

用法:
  python cli.py order_center_push                  # 全部表(销售/售后窗口 90 天)
  python cli.py order_center_push -p table=sales   # 只推一张(sales/returns/perf/settlement/keys)
  python cli.py order_center_push -p days=180      # 自定窗口(销售按下单时间,售后按发起时间,
                                                   #   对账按最近入账日;绩效表全量,自身有界)

设计(PG 权威,飞书是人机界面;对齐用户既有六表结构,2026-08-06):
- 程序写四张:销售 ← order_lines;售后 ← return_lines;绩效 ← perf_event_spans
  (事件级,统计周期=首末拉取日);对账 ← settlement_by_line 跨账期合并
  (金额=合计,佣金率/优惠/账期=最近账期值;逐账期明细留在 PG)。
- 键补齐两张:主订单表、采购信息只建缺失的 order_line_id 行(只写键列),
  其余列全归人工与关联字段,永不更新。
- **任何表都不删行**(sync_by_key delete_stale=False):主订单表是永久枢纽,
  行间有关联字段,删行断链;滑出窗口的行只是停止刷新。
- 只覆盖 registry 登记的程序字段;人工列/采集列/关联字段不出现在载荷里,
  绝不会被碰。售后表键 = RMA号|order_line_id(需在表中补「唯一键」字段)。
- 表格未在 .env 登记(FEISHU_ORDER_APP_TOKEN + 各 table_id)时逐表跳过并提示;
  字段契约与建表要求见 docs/feishu_tables.md。
"""

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from api import feishu
from registry import db, resources
from services import kpi

DANGEROUS = False

logger = logging.getLogger("workflows.order_center_push")

_SALES_SQL = """
SELECT order_line_id, store, po_id, line_number, sku, product_name, qty,
       sale_status, audit_status, status_date, order_date, est_ship_date,
       est_delivery_date, product_amount, shipping_amount, cancel_reason,
       refund_amount, refund_comments, carrier, tracking_no, tracking_url,
       ship_name, phone, address1, address2, city, state, postal_code,
       country, updated_at
FROM orders.order_lines
WHERE order_date >= now() - make_interval(days => %s)
"""

_RETURNS_SQL = """
SELECT r.return_order_id, r.order_line_id, r.store, r.po_id, r.line_number,
       r.customer_order_id, r.sku, r.return_status, r.refund_status,
       r.return_method, r.refund_mode, r.is_keep_it, r.refund_total,
       r.return_reason, r.return_comment, r.return_by, r.return_created,
       r.last_modified, r.customer_name, r.customer_email, r.qty,
       r.refunded_qty, r.carrier, r.tracking_no, l.order_date
FROM orders.return_lines r
LEFT JOIN orders.order_lines l USING (order_line_id)
WHERE r.return_created >= now() - make_interval(days => %s)
"""

# 绩效表全量:行数按 (po, metric) 事件去重,增长缓慢,自身有界。
# 只推挂上订单行的事件(所有者决策 2026-08-06):挂不上的 = PO 早于拉单窗口的
# 老单,报表每天仍会重报它们(滚动窗口),不能删库只能推送侧过滤;
# 滚出统计窗口后自然消失。PG 保留全量供统计。
# 最近一期原始行(detail/status)经 LATERAL 取;period 为 ISO 日期串,字符序即时间序
_PERF_SQL = """
SELECT s.store, s.po_id, s.metric, s.order_line_id, s.first_period,
       s.last_period, s.periods_seen, s.ever_accountable, s.still_active,
       l.order_date, le.detail, le.status, le.last_seen_at
FROM orders.perf_event_spans s
LEFT JOIN orders.order_lines l ON l.order_line_id = s.order_line_id
LEFT JOIN LATERAL (
    SELECT e.detail, e.status, e.last_seen_at FROM orders.perf_events e
    WHERE e.po_id = s.po_id AND e.metric = s.metric
    ORDER BY e.period DESC LIMIT 1) le ON true
WHERE s.order_line_id IS NOT NULL
"""

# 账期 MMDDYYYY:最近账期按 YYYY+MMDD 重排后取最大
_SETTLE_SQL = """
SELECT b.order_line_id, b.store, b.po_id, b.line_number, b.net_amount,
       b.product_amount, b.commission_amount, b.settle_status,
       b.last_settle_date, l.order_date,
       lp.period, lp.commission_rate, lp.original_commission,
       lp.commission_saving, lp.incentive, lp.updated_at
FROM orders.settlement_by_line b
LEFT JOIN orders.order_lines l USING (order_line_id)
LEFT JOIN LATERAL (
    SELECT s2.period, s2.commission_rate, s2.original_commission,
           s2.commission_saving, s2.incentive, s2.updated_at
    FROM orders.settlement_lines s2
    WHERE s2.order_line_id = b.order_line_id
    ORDER BY substr(s2.period, 5, 4) || substr(s2.period, 1, 4) DESC
    LIMIT 1) lp ON true
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


def _detail_desc(detail, limit: int = 300) -> str | None:
    """输入:perf_events.detail(报表原始行 dict)→ 输出:人读摘要 "k:v; k:v"。"""
    if not isinstance(detail, dict):
        return None
    parts = [f"{k}:{str(v).strip()[:40]}" for k, v in detail.items()
             if str(v or "").strip()]
    return "; ".join(parts)[:limit] or None


def _fetch(sql: str, args: tuple) -> list[dict]:
    """输入:SQL + 参数 → 输出:行 dict 列表(列名 → 值)。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _push_sales(days: int) -> tuple[str, set[str]]:
    """返回 (结果行, 本窗口键集合)——键集合供主订单表/采购信息补齐。"""
    t = resources.ORDER_SALES
    f = t.fields
    desired = {}
    for r in _fetch(_SALES_SQL, (days,)):
        desired[r["order_line_id"]] = {
            f.key: r["order_line_id"], f.order_date: _cell(r["order_date"]),
            f.store: r["store"], f.po_id: r["po_id"],
            f.line_number: r["line_number"], f.sku: r["sku"],
            f.product_name: r["product_name"], f.qty: _cell(r["qty"]),
            f.sale_status: r["sale_status"], f.audit_status: r["audit_status"],
            f.status_date: _cell(r["status_date"]),
            f.est_ship_date: _cell(r["est_ship_date"]),
            f.est_delivery_date: _cell(r["est_delivery_date"]),
            f.product_amount: _cell(r["product_amount"]),
            f.shipping_amount: _cell(r["shipping_amount"]),
            f.cancel_reason: r["cancel_reason"],
            f.refund_amount: _cell(r["refund_amount"]),
            f.refund_comments: r["refund_comments"],
            f.carrier: r["carrier"], f.tracking_no: r["tracking_no"],
            f.tracking_url: r["tracking_url"], f.ship_name: r["ship_name"],
            f.phone: r["phone"], f.address1: r["address1"],
            f.address2: r["address2"], f.city: r["city"], f.state: r["state"],
            f.postal_code: r["postal_code"], f.country: r["country"],
            f.pulled_at: _cell(r["updated_at"]),
        }
    c, u, d = feishu.sync_by_key(t, f.key, desired, delete_stale=False)
    return (f"销售订单:{len(desired)} 行(窗口 {days} 天),新建 {c} 更新 {u}",
            set(desired))


def _push_returns(days: int) -> str:
    t = resources.ORDER_RETURNS
    f = t.fields
    desired = {}
    for r in _fetch(_RETURNS_SQL, (days,)):
        key = f"{r['return_order_id']}|{r['order_line_id']}"
        desired[key] = {
            f.key: key, f.order_line_id: r["order_line_id"],
            f.order_date: _cell(r["order_date"]), f.store: r["store"],
            f.rma: r["return_order_id"],
            f.customer_order_id: r["customer_order_id"],
            f.po_id: r["po_id"], f.line_number: r["line_number"],
            f.sku: r["sku"], f.return_status: r["return_status"],
            f.refund_status: r["refund_status"],
            f.return_method: r["return_method"],
            f.refund_mode: r["refund_mode"],
            f.refund_total: _cell(r["refund_total"]),
            f.return_reason: r["return_reason"],
            f.return_comment: r["return_comment"],
            f.return_by: _cell(r["return_by"]),
            f.return_created: _cell(r["return_created"]),
            f.last_modified: _cell(r["last_modified"]),
            f.customer_name: r["customer_name"],
            f.customer_email: r["customer_email"],
            f.qty: _cell(r["qty"]), f.refunded_qty: _cell(r["refunded_qty"]),
            f.carrier: r["carrier"], f.tracking_no: r["tracking_no"],
            f.is_keep_it: r["is_keep_it"],
        }
    c, u, d = feishu.sync_by_key(t, f.key, desired, delete_stale=False)
    return f"售后订单:{len(desired)} 行(窗口 {days} 天),新建 {c} 更新 {u}"


def _push_perf() -> str:
    t = resources.ORDER_PERF
    f = t.fields
    desired = {}
    for r in _fetch(_PERF_SQL, ()):
        key = f"{r['po_id']}|{r['metric']}"
        span = (r["first_period"] if r["first_period"] == r["last_period"]
                else f"{r['first_period']} ~ {r['last_period']}")
        desired[key] = {
            f.key: key, f.order_line_id: r["order_line_id"],
            f.order_date: _cell(r["order_date"]), f.store: r["store"],
            f.po_id: r["po_id"],
            # 指标推 emoji 展示名(日报同款契约),未知指标原样
            f.metric: kpi.METRIC_LABELS.get(r["metric"], r["metric"]),
            f.accountable: r["ever_accountable"],
            f.description: _detail_desc(r["detail"]),
            # 仍出现在最近一期报表 = 仍拖当前绩效分;消失 = 滚出官方统计窗口
            f.status: "影响中" if r["still_active"] else "已滚出窗口",
            f.period_span: f"{span}(共 {r['periods_seen']} 期)",
            f.detail: (json.dumps(r["detail"], ensure_ascii=False)[:1000]
                       if r["detail"] is not None else None),
            f.pulled_at: _cell(r["last_seen_at"]),
        }
    c, u, d = feishu.sync_by_key(t, f.key, desired, delete_stale=False)
    return f"绩效订单:{len(desired)} 行(全量),新建 {c} 更新 {u}"


def _push_settlement(days: int) -> str:
    t = resources.ORDER_SETTLE
    f = t.fields
    desired = {}
    for r in _fetch(_SETTLE_SQL, (days,)):
        desired[r["order_line_id"]] = {
            f.key: r["order_line_id"], f.order_date: _cell(r["order_date"]),
            f.store: r["store"], f.po_id: r["po_id"],
            f.line_number: r["line_number"],
            f.settle_status: r["settle_status"],
            f.net_amount: _cell(r["net_amount"]),
            f.product_amount: _cell(r["product_amount"]),
            f.commission_amount: _cell(r["commission_amount"]),
            # 佣金率/优惠/账期为最近账期值,金额为跨账期合计
            f.commission_rate: _cell(r["commission_rate"]),
            f.original_commission: _cell(r["original_commission"]),
            f.commission_saving: _cell(r["commission_saving"]),
            f.incentive: r["incentive"], f.period: r["period"],
            f.settle_date: _cell(r["last_settle_date"]),
            f.pulled_at: _cell(r["updated_at"]),
        }
    c, u, d = feishu.sync_by_key(t, f.key, desired, delete_stale=False)
    return f"对账明细:{len(desired)} 行(窗口 {days} 天),新建 {c} 更新 {u}"


def _push_keys(sales_keys: set[str]) -> list[str]:
    """主订单表/采购信息:补齐销售窗口内缺失的 order_line_id 行(只写键列)。"""
    lines = []
    for t in (resources.ORDER_MAIN, resources.ORDER_PURCHASE):
        try:
            n = feishu.ensure_keys(t, t.fields.key, sales_keys)
            lines.append(f"{t.name}:键补齐 {n} 行")
        except LookupError as e:
            lines.append(f"{t.name}:跳过(未登记)— {e}")
    return lines


_TABLES = ("sales", "returns", "perf", "settlement", "keys")


def run(params: dict) -> str:
    """输入:params(可选 table/days)→ 输出:各表同步结果摘要。"""
    only = str(params.get("table", "")).strip()
    if only and only not in _TABLES:
        return f"table 只接受 {'/'.join(_TABLES)},收到:{only}"
    days = int(params.get("days", 90))

    lines, failed = [], []
    sales_keys: set[str] = set()
    todo = (only,) if only else _TABLES
    for name in todo:
        try:
            if name == "sales":
                line, sales_keys = _push_sales(days)
                lines.append(line)
            elif name == "returns":
                lines.append(_push_returns(days))
            elif name == "perf":
                lines.append(_push_perf())
            elif name == "settlement":
                lines.append(_push_settlement(days))
            elif name == "keys":
                # 单独跑 keys 时销售未拉,现查窗口键集合
                if not sales_keys:
                    sales_keys = {r["order_line_id"]
                                  for r in _fetch(_SALES_SQL, (days,))}
                lines.extend(_push_keys(sales_keys))
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
