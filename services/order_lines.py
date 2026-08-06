"""订单域行级积木:行标识生成 + 三源(orders/returns/recon)归一化 + PG 写入。

行标识与旧仓库「订单中心v1」完全同构(哈希输入逐字节一致,两系统 ID 可互查):
    order_line_id = 'ol_' + sha256(store + '\\x1f' + po + '\\x1f' + line)[:24]

解析逻辑逐条移植自 订单中心v1 的 sync_sales / sync_aftersales / sync_finance
(2026-08 生产实测),踩坑注释随代码保留。供 order_audit / returns_sync /
绩效同步 / 结算同步四条工作流复用。
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("services.order_lines")


# ── 行标识 ────────────────────────────────────────────────────────────────────

def make_order_line_id(store: str, po: str, line) -> str:
    """输入:店铺名 + PO 号 + 行号 → 输出:稳定行标识(与订单中心v1 同构)。"""
    raw = f"{store}\x1f{po}\x1f{norm_line(line)}"
    return "ol_" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def norm_line(line) -> str:
    """输入:行号(int/str/float 形态不一)→ 输出:规范字符串('1',与哈希输入一致)。"""
    try:
        return str(int(float(line)))
    except (TypeError, ValueError):
        return str(line or "").strip()


def _ts(v) -> datetime | None:
    """输入:毫秒时间戳/ISO 字符串 → 输出:datetime(UTC)或 None。"""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc)
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromtimestamp(float(s) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            return None


def _num(v, default=None):
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _aslist(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


# ── 源 1:GET /v3/orders → order_lines 行 ─────────────────────────────────────

def _parse_charges(charges_raw) -> tuple[float, float]:
    """输入:orderLine.charges → 输出:(商品金额, 运费金额)。"""
    product, shipping = 0.0, 0.0
    for ch in _aslist((charges_raw or {}).get("charge")):
        amt = _num((ch.get("chargeAmount") or {}).get("amount"), 0.0)
        if ch.get("chargeType") == "PRODUCT":
            product += amt
        elif ch.get("chargeType") == "SHIPPING":
            shipping += amt
    return product, shipping


def _parse_refund(order_line: dict) -> tuple[float, str]:
    """输入:orderLine → 输出:(行内退款金额, 退款备注)。结构缺失一律 (0, '')。"""
    try:
        lines = _aslist(((order_line.get("refunds") or {})
                         .get("refundLines") or {}).get("refundLine"))
        if not lines:
            return 0.0, ""
        rl = lines[0]
        charges = _aslist((rl.get("refundCharges") or {}).get("refundCharge"))
        total = sum(_num(((c.get("charge") or {}).get("chargeAmount") or {}).get("amount"), 0.0)
                    for c in charges)
        return total, rl.get("refundComment", "") or ""
    except Exception:
        return 0.0, ""


def tracking_url(carrier: str, tracking: str) -> str | None:
    """输入:承运商 + 单号 → 输出:跟踪链接(UPS/FedEx/USPS 专链,其余 17track)。"""
    if not tracking:
        return None
    c = (carrier or "").lower()
    if "ups" in c:
        return f"https://www.ups.com/track?tracknum={tracking}"
    if "fedex" in c:
        return f"https://www.fedex.com/fedextrack/?trknbr={tracking}"
    if "usps" in c:
        return f"https://tools.usps.com/go/TrackConfirmAction?qtc_tLabels1={tracking}"
    return f"https://t.17track.net/en#nums={tracking}"


def extract_order_lines(store_name: str, order: dict) -> list[dict]:
    """输入:店铺名 + 单个 Walmart 订单 → 输出:order_lines 行 dict 列表。

    踩坑(实证):已发货订单不返回 estimated* 字段,est_ship 回退
    trackingInfo.shipDateTime,est_delivery 回退 fulfillment.pickUpDateTime。
    """
    po = str(order.get("purchaseOrderId", ""))
    addr = (order.get("shippingInfo") or {}).get("postalAddress") or {}
    phone = (order.get("shippingInfo") or {}).get("phone") or ""

    rows = []
    for ol in _aslist((order.get("orderLines") or {}).get("orderLine")):
        line_no = ol.get("lineNumber")
        statuses = _aslist((ol.get("orderLineStatuses") or {}).get("orderLineStatus"))
        st = statuses[0] if statuses else {}
        ti = st.get("trackingInfo") or {}
        carrier_obj = ti.get("carrierName") or {}
        carrier = str(carrier_obj.get("carrier") or carrier_obj.get("otherCarrier") or "")
        tracking = str(ti.get("trackingNumber") or "")
        fulfil = ol.get("fulfillment") or {}
        product_amt, ship_amt = _parse_charges(ol.get("charges"))
        refund_amt, refund_note = _parse_refund(ol)

        rows.append({
            "order_line_id": make_order_line_id(store_name, po, line_no),
            "store": store_name, "po_id": po, "line_number": norm_line(line_no),
            "customer_order_id": str(order.get("customerOrderId") or ""),
            "sku": str((ol.get("item") or {}).get("sku") or ""),
            "product_name": str((ol.get("item") or {}).get("productName") or ""),
            "qty": int(_num((ol.get("orderLineQuantity") or {}).get("amount"), 1) or 1),
            "sale_status": st.get("status", ""),
            "status_date": _ts(st.get("statusSetDate")),
            "order_date": _ts(order.get("orderDate")),
            "est_ship_date": _ts(fulfil.get("estimatedShipDate") or ti.get("shipDateTime")),
            "est_delivery_date": _ts(fulfil.get("estimatedDeliveryDate")
                                     or fulfil.get("pickUpDateTime")),
            "product_amount": product_amt, "shipping_amount": ship_amt,
            "cancel_reason": str(ol.get("cancellationReason") or ""),
            "refund_amount": refund_amt, "refund_comments": refund_note,
            "carrier": carrier, "tracking_no": tracking,
            "tracking_url": tracking_url(carrier, tracking),
            "ship_name": str(addr.get("name") or ""), "phone": str(phone),
            "address1": str(addr.get("address1") or ""),
            "address2": str(addr.get("address2") or ""),
            "city": str(addr.get("city") or ""), "state": str(addr.get("state") or ""),
            "postal_code": str(addr.get("postalCode") or ""),
            "country": str(addr.get("country") or ""),
            "raw": json.dumps(ol, ensure_ascii=False, default=str),
        })
    return rows


# ── 源 2:GET /v3/returns → return_lines 行 ───────────────────────────────────

def _customer_name(v) -> str:
    if isinstance(v, dict):
        first = (v.get("firstName") or "").strip()
        last = (v.get("lastName") or "").strip()
        return (f"{first} {last}").strip() or str(v.get("fullName") or "")
    return str(v or "")


def flatten_return_lines(store_name: str, return_order: dict) -> list[dict]:
    """输入:店铺名 + 单个 returnOrder → 输出:return_lines 行 dict 列表。

    实测结构(2026-08):status/refundStatus/returnMethod 在 returnOrderLines[i]
    行级而非顶层;物流在 returnLineGroups[].labels[].carrierInfoList[];
    兼容老格式 returnLines.returnLine[]。
    """
    o = return_order
    rma = str(o.get("returnOrderId") or "")
    refund_obj = o.get("refundValue") or o.get("totalRefundAmount") or {}
    refund_total = _num(refund_obj.get("totalAmount") or refund_obj.get("currencyAmount"))

    carrier, tracking_no = "", ""
    groups = o.get("returnLineGroups") or []
    if groups:
        labels = groups[0].get("labels") or []
        if labels:
            cil = labels[0].get("carrierInfoList") or []
            if cil:
                carrier = cil[0].get("carrierName", "") or ""
                tracking_no = cil[0].get("trackingNo") or cil[0].get("trackingNumber", "") or ""

    lines = o.get("returnOrderLines") or []
    if not lines:                       # 老格式兼容
        wrapper = o.get("returnLines") or {}
        lines = wrapper.get("returnLine") or [] if isinstance(wrapper, dict) else wrapper

    rows = []
    for line in lines:
        po = str(line.get("purchaseOrderId") or "")
        line_no = line.get("purchaseOrderLineNumber") or line.get("salesOrderLineNumber", "")
        qty_obj = line.get("quantity") or {}
        refunded = line.get("refundedQty")
        if isinstance(refunded, dict):
            refunded = refunded.get("amount") or refunded.get("measurementValue")
        rows.append({
            "return_order_id": rma,
            "order_line_id": make_order_line_id(store_name, po, line_no),
            "store": store_name, "po_id": po, "line_number": norm_line(line_no),
            "customer_order_id": str(o.get("customerOrderId") or ""),
            "sku": str((line.get("item") or {}).get("sku") or ""),
            "return_status": line.get("status", ""),
            "refund_status": line.get("currentRefundStatus", ""),
            "return_method": line.get("returnMethod", ""),
            "refund_mode": o.get("refundMode", ""),
            "is_keep_it": bool(line.get("isKeepIt")),
            "refund_total": refund_total,
            "return_reason": line.get("returnReason", "") or "",
            "return_comment": line.get("returnComment") or line.get("returnDescription", "") or "",
            "return_by": _ts(o.get("returnByDate")),
            "return_created": _ts(o.get("returnCreationDate") or o.get("returnOrderDate")),
            "last_modified": _ts(o.get("lastModifiedDate")),
            "customer_name": _customer_name(o.get("customerName")),
            "customer_email": str(o.get("customerEmailId") or ""),
            "qty": int(_num(qty_obj.get("amount") or qty_obj.get("measurementValue"), 0) or 0) or None,
            "refunded_qty": int(_num(refunded, 0) or 0) if refunded is not None else None,
            "carrier": carrier, "tracking_no": tracking_no,
            "raw": json.dumps(line, ensure_ascii=False, default=str),
        })
    return rows


# ── 源 3:Recon CSV → settlement_lines 行 ─────────────────────────────────────

def aggregate_settlement_lines(store_name: str, rows: list[dict], period: str) -> list[dict]:
    """输入:店铺名 + 某账期全部 Recon 行 + 账期(MMDDYYYY)→ 输出:按行聚合的记录列表。

    实证规则(订单中心v1):
    - 汇总行 PO/行号为空,必须跳过(否则聚出 sha256(店铺+空+空) 的垃圾行);
    - 金额必须 round6:Sale/Refund 相消的浮点和是 4.44e-16 而非 0,会误判入账状态;
    - gross(绝对值和)用于区分"净0=全额退款"与"净0=无金额"。
    """
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        po = str(row.get("Purchase Order #") or "").strip()
        line = str(row.get("Purchase Order line #") or "").strip()
        if not po or not line:
            continue
        groups.setdefault((po, line), []).append(row)

    try:
        settle_date = datetime.strptime(period, "%m%d%Y").date()
    except (ValueError, TypeError):
        logger.warning("账期无法解析: %r", period)
        settle_date = None

    records = []
    for (po, line), group in groups.items():
        def _sum(pred):
            return round(sum(_num(r.get("Amount"), 0.0) for r in group if pred(r)), 6)
        first_of = {}
        for key, col in (("commission_rate", "Commission Rate"),
                         ("original_commission", "Original Commission"),
                         ("commission_saving", "Commission Saving")):
            first_of[key] = next((_num(r.get(col)) for r in group
                                  if _num(r.get(col)) is not None), None)
        incentive = next((str(r.get("Commission Incentive Program")).strip()
                          for r in group
                          if str(r.get("Commission Incentive Program") or "").strip()), None)
        records.append({
            "order_line_id": make_order_line_id(store_name, po, line),
            "period": period,
            "store": store_name, "po_id": po, "line_number": norm_line(line),
            "net_amount": _sum(lambda r: True),
            "gross_amount": round(sum(abs(_num(r.get("Amount"), 0.0)) for r in group), 6),
            "product_amount": _sum(lambda r: (r.get("Amount Type") or "") == "Product Price"),
            "commission_amount": _sum(
                lambda r: (r.get("Amount Type") or "") == "Commission on Product"),
            **first_of,
            "incentive": incentive,
            "settle_date": settle_date,
            "raw": None,    # 明细行量大,默认不存原文;需要时单独开
        })
    return records


def settle_status(net: float, gross: float) -> str:
    """输入:净额 + 交易额(绝对值和)→ 输出:入账状态(与 settlement_by_line 视图同规则)。"""
    if net > 0:
        return "已入账"
    if net < 0:
        return "已冲销"
    return "已退款" if gross > 0 else "待入账"


# ── PG 写入(upsert,全部幂等)──────────────────────────────────────────────────

def _upsert(conn, table: str, cols: list[str], key_cols: list[str], rows: list[dict],
            skip_update: tuple = ()) -> int:
    if not rows:
        return 0
    collist = ", ".join(cols)
    placeholders = ", ".join(f"%({c})s" for c in cols)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols
                        if c not in key_cols and c not in skip_update)
    sql = (f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) "
           f"ON CONFLICT ({', '.join(key_cols)}) DO UPDATE SET {updates}, updated_at = now()")
    with conn.cursor() as cur:
        cur.executemany(sql, [{c: r.get(c) for c in cols} for r in rows])
    return len(rows)


_ORDER_LINE_COLS = [
    "order_line_id", "store", "po_id", "line_number", "customer_order_id",
    "sku", "product_name", "qty", "sale_status", "status_date", "order_date",
    "est_ship_date", "est_delivery_date", "product_amount", "shipping_amount",
    "cancel_reason", "refund_amount", "refund_comments", "carrier", "tracking_no",
    "tracking_url", "ship_name", "phone", "address1", "address2", "city", "state",
    "postal_code", "country", "raw"]

_RETURN_LINE_COLS = [
    "return_order_id", "order_line_id", "store", "po_id", "line_number",
    "customer_order_id", "sku", "return_status", "refund_status", "return_method",
    "refund_mode", "is_keep_it", "refund_total", "return_reason", "return_comment",
    "return_by", "return_created", "last_modified", "customer_name", "customer_email",
    "qty", "refunded_qty", "carrier", "tracking_no", "raw"]

_SETTLEMENT_COLS = [
    "order_line_id", "period", "store", "po_id", "line_number", "net_amount",
    "gross_amount", "product_amount", "commission_amount", "commission_rate",
    "original_commission", "commission_saving", "incentive", "settle_date", "raw"]


def upsert_order_lines(conn, rows: list[dict]) -> int:
    """输入:连接 + extract_order_lines 产出 → 输出:写入行数。审核列不在此覆盖。"""
    for r in rows:
        if isinstance(r.get("raw"), (dict, list)):
            r["raw"] = json.dumps(r["raw"], ensure_ascii=False, default=str)
    return _upsert(conn, "orders.order_lines", _ORDER_LINE_COLS,
                   ["order_line_id"], rows)


def upsert_return_lines(conn, rows: list[dict]) -> int:
    """输入:连接 + flatten_return_lines 产出 → 输出:写入行数。"""
    return _upsert(conn, "orders.return_lines", _RETURN_LINE_COLS,
                   ["return_order_id", "order_line_id"], rows)


def upsert_settlement_lines(conn, rows: list[dict]) -> int:
    """输入:连接 + aggregate_settlement_lines 产出 → 输出:写入行数。"""
    return _upsert(conn, "orders.settlement_lines", _SETTLEMENT_COLS,
                   ["order_line_id", "period"], rows)


def upsert_perf_events(conn, rows: list[dict]) -> int:
    """输入:连接 + 绩效事件行(store/po_id/metric/period/sku/accountable/status/detail)
    → 输出:写入行数。同键重现只刷新 last_seen_at,保留 first_seen_at。"""
    if not rows:
        return 0
    for r in rows:
        if isinstance(r.get("detail"), (dict, list)):
            r["detail"] = json.dumps(r["detail"], ensure_ascii=False, default=str)
    sql = """
        INSERT INTO orders.perf_events
            (store, po_id, metric, period, order_line_id, sku, accountable, status, detail)
        VALUES (%(store)s, %(po_id)s, %(metric)s, %(period)s, %(order_line_id)s,
                %(sku)s, %(accountable)s, %(status)s, %(detail)s::jsonb)
        ON CONFLICT (store, po_id, metric, period) DO UPDATE SET
            order_line_id = COALESCE(EXCLUDED.order_line_id, orders.perf_events.order_line_id),
            sku = COALESCE(EXCLUDED.sku, orders.perf_events.sku),
            accountable = EXCLUDED.accountable, status = EXCLUDED.status,
            detail = EXCLUDED.detail, last_seen_at = now()
    """
    defaults = {"order_line_id": None, "sku": None, "accountable": None,
                "status": None, "detail": None}
    with conn.cursor() as cur:
        cur.executemany(sql, [{**defaults, **r} for r in rows])
    return len(rows)


def backfill_perf_line_ids(conn) -> int:
    """输入:连接 → 输出:回填总行数。

    绩效报表无行号但多数带商品列(实证),两段式回填,只在无歧义时落子:
    ① 事件带 SKU → 按 (store, po_id, sku) 匹配,该 SKU 在此单恰好一行才回填
       (多行订单也能关联上——这是 SKU 相对行号真正的价值点);
    ② 事件无 SKU → 该 PO 在 order_lines 恰好一行时回填。
    两段都对不上的保持 NULL,宁缺毋错。
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE orders.perf_events p
            SET order_line_id = l.order_line_id
            FROM (SELECT store, po_id, sku, min(order_line_id) AS order_line_id
                  FROM orders.order_lines GROUP BY store, po_id, sku
                  HAVING count(*) = 1) l
            WHERE p.order_line_id IS NULL AND p.sku IS NOT NULL AND p.sku <> ''
              AND p.store = l.store AND p.po_id = l.po_id AND p.sku = l.sku
        """)
        by_sku = cur.rowcount
        cur.execute("""
            UPDATE orders.perf_events p
            SET order_line_id = l.order_line_id
            FROM (SELECT store, po_id, min(order_line_id) AS order_line_id
                  FROM orders.order_lines GROUP BY store, po_id HAVING count(*) = 1) l
            WHERE p.order_line_id IS NULL
              AND p.store = l.store AND p.po_id = l.po_id
        """)
        return by_sku + cur.rowcount
