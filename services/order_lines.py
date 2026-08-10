"""订单域行级积木:行标识生成 + 三源(orders/returns/recon)归一化 + PG 写入。

行标识(2026-08-06 定稿,v3):
    order_line_id = 'ol_' + sha256(po + '\\x1f' + sku)[:24]

⚠ 身份两次定稿的理由都要记住:
- **店铺不参与哈希**(v2 起):PO 号是沃尔玛发的、平台全局唯一,而店铺名是
  我们自己的标签(飞书凭证表),改名/换人是真实运营事件,参与哈希会让改名
  瞬间作废全部行标识。店铺仍存列+索引,只做归属不做身份。
- **SKU 而非行号参与哈希**(v3,项目所有者定稿):沃尔玛同一订单内同一 SKU
  必合并为一行(qty 累加),(PO, SKU) 与 (PO, 行号) 同样唯一;而绩效报表只给
  PO+SKU 不给行号,用 SKU 做身份后绩效事件可**直接算出行标识**(订单不在库里
  也能建键),消掉了 v2 时代绩效关联的两段式回填缺口。行号仍存列做展示/对账。
  若"同 SKU 合并"规则被线上数据打破(同 PO 同 SKU 两行),extract 会告警——
  兜底不许静默。

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

def make_order_line_id(po: str, sku) -> str:
    """输入:PO 号 + SKU → 输出:稳定行标识(店铺/行号不参与身份,见模块 docstring)。"""
    raw = f"{po}\x1f{norm_sku(sku)}"
    return "ol_" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def norm_sku(sku) -> str:
    """输入:SKU(任意形态)→ 输出:规范字符串(仅去首尾空白;SKU 大小写敏感,不动)。"""
    return str(sku or "").strip()


def norm_line(line) -> str:
    """输入:行号(int/str/float 形态不一)→ 输出:规范字符串('1',仅作展示列)。"""
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
    seen_skus: dict[str, str] = {}
    for ol in _aslist((order.get("orderLines") or {}).get("orderLine")):
        line_no = ol.get("lineNumber")
        sku = norm_sku((ol.get("item") or {}).get("sku"))
        # 身份前提校验:同一 PO 内同一 SKU 应合并为一行(项目所有者实证规则);
        # 被打破时后一行覆盖前一行,必须告警——兜底不许静默
        if sku in seen_skus:
            logger.warning("订单 %s 内 SKU %r 出现在多行(行 %s 与 %s),"
                           "(PO,SKU) 身份撞键,后行覆盖前行,请人工核查",
                           po, sku, seen_skus[sku], line_no)
        seen_skus[sku] = str(line_no)
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
            "order_line_id": make_order_line_id(po, sku),
            "store": store_name, "po_id": po, "line_number": norm_line(line_no),
            "customer_order_id": str(order.get("customerOrderId") or ""),
            "sku": sku,
            "product_name": str((ol.get("item") or {}).get("productName") or ""),
            "qty": int(_num((ol.get("orderLineQuantity") or {}).get("amount"), 1) or 1),
            "sale_status": st.get("status", ""),
            # 实证(2026-08-06 生产 raw):时间戳叫 statusDate 且在 orderLine 层,
            # PLAN 文档写的 orderLineStatus.statusSetDate 线上不存在,留作回退
            "status_date": _ts(ol.get("statusDate") or st.get("statusSetDate")),
            "order_date": _ts(order.get("orderDate")),
            "est_ship_date": _ts(fulfil.get("estimatedShipDate") or ti.get("shipDateTime")),
            "est_delivery_date": _ts(fulfil.get("estimatedDeliveryDate")
                                     or fulfil.get("pickUpDateTime")),
            "product_amount": product_amt, "shipping_amount": ship_amt,
            "cancel_reason": str(ol.get("cancellationReason") or ""),
            "refund_amount": refund_amt, "refund_comments": refund_note,
            "carrier": carrier, "tracking_no": tracking,
            # 官方自带 trackingURL(实证),缺失才自拼(UPS/FedEx/USPS 专链,其余 17track)
            "tracking_url": ti.get("trackingURL") or tracking_url(carrier, tracking),
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
        # 行号只做展示列(售后行给的是原订单行号引用);身份用 SKU——
        # item.sku 在售后行永远存在,行号引用字段在旧数据中有过缺失记录
        line_no = line.get("purchaseOrderLineNumber") or line.get("salesOrderLineNumber", "")
        sku = norm_sku((line.get("item") or {}).get("sku"))
        qty_obj = line.get("quantity") or {}
        refunded = line.get("refundedQty")
        if isinstance(refunded, dict):
            refunded = refunded.get("amount") or refunded.get("measurementValue")
        rows.append({
            "return_order_id": rma,
            "order_line_id": make_order_line_id(po, sku),
            "store": store_name, "po_id": po, "line_number": norm_line(line_no),
            "customer_order_id": str(o.get("customerOrderId") or ""),
            "sku": sku,
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

# Recon CSV 可能的 SKU 列名(官方叫 Partner Item Id;多写几个候选防版式差异,
# 命中哪个记日志——首个非空即用)
_RECON_SKU_COLS = ("Partner Item Id", "Partner Item ID", "Partner Item id",
                   "SKU", "Sku", "Item Id", "Item ID")


def aggregate_settlement_lines(store_name: str, rows: list[dict], period: str,
                               sku_lookup: dict | None = None
                               ) -> tuple[list[dict], int]:
    """输入:店铺名 + 某账期全部 Recon 行 + 账期(MMDDYYYY)+ 可选 {(po,行号)→sku}
    → 输出:(按行聚合的记录列表, 因无法解析 SKU 而跳过的行组数)。

    实证规则(订单中心v1):
    - 汇总行 PO/行号为空,必须跳过(否则聚出 sha256(店铺+空) 的垃圾行);
    - 金额必须 round6:Sale/Refund 相消的浮点和是 4.44e-16 而非 0,会误判入账状态;
    - gross(绝对值和)用于区分"净0=全额退款"与"净0=无金额"。

    SKU 解析(v3 身份 = PO+SKU)两级,均计数记日志:
    ① CSV 自带 SKU 列(_RECON_SKU_COLS 探测);
    ② 缺列/空值时用 sku_lookup(调用方从 orders.order_lines 按 (po_id,line_number)
      预查)。两级都解析不到的行组跳过并计数——绝不硬造键。
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

    def _resolve_sku(po: str, line: str, group: list[dict]) -> str | None:
        for r in group:
            for col in _RECON_SKU_COLS:
                v = norm_sku(r.get(col))
                if v:
                    return v
        if sku_lookup:
            return sku_lookup.get((po, norm_line(line)))
        return None

    skipped = 0
    records = []
    for (po, line), group in groups.items():
        sku = _resolve_sku(po, line, group)
        if not sku:
            skipped += 1
            continue
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
            "order_line_id": make_order_line_id(po, sku),
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
    if skipped:
        logger.warning("店铺 %s 账期 %s:%d 个行组解析不到 SKU(CSV 列+订单行反查均失败),"
                       "已跳过——CSV 表头若含 SKU 列请告知列名补进 _RECON_SKU_COLS",
                       store_name, period, skipped)
    return records, skipped


def settle_status(net: float, gross: float) -> str:
    """输入:净额 + 交易额(绝对值和)→ 输出:入账状态(与 settlement_by_line 视图同规则)。"""
    if net > 0:
        return "已入账"
    if net < 0:
        return "已冲销"
    return "已退款" if gross > 0 else "待入账"


# ── PG 写入(upsert,全部幂等)──────────────────────────────────────────────────

# 沃尔玛常态性把买家电话打码成全 0(2026-08-10 实证:45 天窗口 2964/3542 = 84%)。
# **全 0 不覆盖已有的真电话**——旧系统的「电话全 0 保护」,legacy_survey 明列为
# 必须照搬的防线,此前漏了。覆盖掉就找不回来:raw 也是每次一起被覆盖的。
# 反向不设防:真电话覆盖全 0 是正常修复。
_PHONE_GUARD = ("CASE WHEN coalesce(EXCLUDED.phone, '') ~ '^0*$' "
                "AND coalesce(t.phone, '') !~ '^0*$' "
                "THEN t.phone ELSE EXCLUDED.phone END")


def _upsert(conn, table: str, cols: list[str], key_cols: list[str], rows: list[dict],
            skip_update: tuple = (), guards: dict | None = None) -> int:
    """输入:连接 + 表 + 列 + 键 + 行 → 输出:提交的行数(不等于真正改动的行数)。

    两条与"写放大"直接相关的语义,改之前先读:

    1. **内容没变就整行不写**(`WHERE ... IS DISTINCT FROM`)。
       原先是无条件 `DO UPDATE SET ..., updated_at = now()`,于是 order_sync
       每轮全量重拉 45 天窗口 ⇒ 窗口内每一行的 updated_at 都被刷新,哪怕一个
       字段都没变。而 order_center_push 把 updated_at 当「拉取时间」写进飞书
       载荷、载荷又参与指纹 ⇒ **指纹必变 ⇒ 每轮重推窗口内全部行**
       (2026-08-10 实证:7100 行里更新 3122,正是 45 天窗口的行数;
       售后表因为没有「拉取时间」列,同一轮只更新了真变化的 7 行——天然对照组)。
       改完 `updated_at` 才真正表示"这行什么时候变的"。

    2. **guards**:{列名: SQL 表达式} 给个别列换掉默认的 `EXCLUDED.列`
       (如电话全 0 保护)。表达式里 `t` 是目标表别名。
       ⚠ 变更检测用的是**生效后的值**而不是 EXCLUDED,否则被 guard 挡下的
       "假变化"照样会让整行重写、updated_at 空跳。
    """
    if not rows:
        return 0
    guards = guards or {}
    collist = ", ".join(cols)
    placeholders = ", ".join(f"%({c})s" for c in cols)
    upd_cols = [c for c in cols if c not in key_cols and c not in skip_update]
    exprs = {c: guards.get(c, f"EXCLUDED.{c}") for c in upd_cols}
    updates = ", ".join(f"{c} = {exprs[c]}" for c in upd_cols)
    changed = (f"({', '.join('t.' + c for c in upd_cols)}) IS DISTINCT FROM "
               f"({', '.join(exprs[c] for c in upd_cols)})")
    sql = (f"INSERT INTO {table} AS t ({collist}) VALUES ({placeholders}) "
           f"ON CONFLICT ({', '.join(key_cols)}) DO UPDATE SET "
           f"{updates}, updated_at = now() WHERE {changed}")
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
                   ["order_line_id"], rows, guards={"phone": _PHONE_GUARD})


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
        ON CONFLICT (po_id, metric, period) DO UPDATE SET
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


def perf_rows_from_problems(store_name: str, metric: str, rows: list[dict],
                            period: str, sku_lookup: dict | None = None
                            ) -> tuple[list[dict], int]:
    """输入:店铺+指标键+parse_problem_report 行+周期+可选 {(po,行号)→sku}
    → 输出:(perf_events 行, 无 PO 跳过数)。

    period = 拉取日(报表是当时滚动窗口的快照,按日累积即可支撑 still_active 判定);
    无 PO 号的行无法建键,跳过并计数(调用方记日志——兜底不许静默)。

    SKU 三级解析(2026-08-06 实证:returns/INR 报表版式带 Order Line # 无 SKU 列):
    ① 报表自带 SKU 列;② 报表带行号 → sku_lookup 反查订单行拿 SKU;
    ③ 都没有 → sku 留 NULL,order_line_id 由 backfill 的单行订单段兜。
    """
    out, skipped = [], 0
    for r in rows:
        po = str(r.get("po_no") or "").strip()
        if not po:
            skipped += 1
            continue
        accountable = str(r.get("accountable", "")).startswith("✅")
        sku = norm_sku(r.get("sku")) or None
        line_no = norm_line(r.get("line_no"))
        if not sku and line_no and sku_lookup:
            sku = sku_lookup.get((po, line_no)) or None
        out.append({"store": store_name, "po_id": po, "metric": metric,
                    "period": str(period),
                    "sku": sku,
                    # v3 身份 = PO+SKU:解析到 SKU 即直接建键,订单不在库里也成立
                    "order_line_id": make_order_line_id(po, sku) if sku else None,
                    "accountable": accountable,
                    "status": "违规" if accountable else "不计入",
                    "detail": r.get("raw")})
    return out, skipped


def pick_new_periods(available: list[str], have: set[str], limit: int) -> list[str]:
    """输入:可用账期(MMDDYYYY)+已入库账期集合+上限 → 输出:待拉账期(旧→新,最近 limit 个)。

    账期文件是关账快照(不可变),已入库的不重拉;MMDDYYYY 按 YYYY+MMDD 排序。
    """
    todo = [d for d in available if d not in have]
    return sorted(todo, key=lambda d: d[4:] + d[:4])[-limit:]


# ── 烂账治理(所有者决策 2026-08-06):订单不在库,关联数据不入库 ─────────────
# 建库拉单窗口(90 天)之前的老订单永远无法匹配,其售后/绩效/对账行入库即烂账;
# 且各源按滚动窗口每天重拉,只删一次会回流——必须在入库侧永久过滤。
# 丢弃数一律返回给调用方记日志,过滤不许静默。


def drop_unlinked(conn, rows: list[dict]) -> tuple[list[dict], int]:
    """输入:连接 + 含 order_line_id 的行(售后/对账)→ 输出:(订单在库的行, 丢弃数)。"""
    ids = sorted({r["order_line_id"] for r in rows if r.get("order_line_id")})
    if not ids:
        return [], len(rows)
    with conn.cursor() as cur:
        cur.execute("SELECT order_line_id FROM orders.order_lines "
                    "WHERE order_line_id = ANY(%s)", (ids,))
        have = {x for (x,) in cur.fetchall()}
    kept = [r for r in rows if r.get("order_line_id") in have]
    return kept, len(rows) - len(kept)


def drop_unlinked_perf(conn, rows: list[dict]) -> tuple[list[dict], int]:
    """输入:连接 + 绩效事件行 → 输出:(PO 在库的行, 丢弃数)。

    绩效按 PO 而非行标识过滤:无 SKU 的老版报表行 order_line_id 为 NULL,
    但只要 PO 在库,单行订单回填仍有机会,不能一刀切掉。
    """
    pos = sorted({r["po_id"] for r in rows if r.get("po_id")})
    if not pos:
        return [], len(rows)
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT po_id FROM orders.order_lines "
                    "WHERE po_id = ANY(%s)", (pos,))
        have = {x for (x,) in cur.fetchall()}
    kept = [r for r in rows if r.get("po_id") in have]
    return kept, len(rows) - len(kept)


def recon_done_periods(conn, store: str) -> set:
    """输入:连接 + 店铺 → 输出:已处理过的对账账期集合(ops.cursors 台账)。

    台账存在的原因:入库过滤后某账期可能 0 行落库,若只看 settlement_lines
    的 DISTINCT period,该期会被当"缺失账期"无限重拉。处理过就记账,不重拉。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM ops.cursors WHERE name = %s",
                    (f"recon_done:{store}",))
        row = cur.fetchone()
    return set(row[0]) if row and row[0] else set()


def mark_recon_done(conn, store: str, periods) -> None:
    """输入:连接 + 店铺 + 本次处理的账期 → 输出:无(并入台账,幂等)。"""
    periods = set(periods)
    if not periods:
        return
    done = recon_done_periods(conn, store) | periods
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ops.cursors (name, value) VALUES (%s, %s::jsonb) "
                    "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, "
                    "updated_at = now()",
                    (f"recon_done:{store}", json.dumps(sorted(done))))


def backfill_perf_line_ids(conn) -> int:
    """输入:连接 → 输出:回填总行数。

    v3 身份 = PO+SKU 后,带 SKU 的事件在写入时就直接建键(见
    perf_rows_from_problems),本函数只兜两类残留,均只在无歧义时落子:
    ① 历史遗留:order_line_id 为 NULL 但 sku 非空(v2→v3 迁移置空的老行)
       → 直接按哈希语义重算:与 orders.order_lines 按 (po_id, sku) 等值连接
       (v3 下该组合唯一);
    ② 事件无 SKU(老版报表缺商品列)→ 该 PO 在 order_lines 恰好一行时回填。
    PO 全局唯一,店铺不参与匹配;对不上的保持 NULL,宁缺毋错。
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE orders.perf_events p
            SET order_line_id = l.order_line_id
            FROM orders.order_lines l
            WHERE p.order_line_id IS NULL AND p.sku IS NOT NULL AND p.sku <> ''
              AND p.po_id = l.po_id AND p.sku = l.sku
        """)
        by_sku = cur.rowcount
        cur.execute("""
            UPDATE orders.perf_events p
            SET order_line_id = l.order_line_id
            FROM (SELECT po_id, min(order_line_id) AS order_line_id
                  FROM orders.order_lines GROUP BY po_id HAVING count(*) = 1) l
            WHERE p.order_line_id IS NULL
              AND p.po_id = l.po_id
        """)
        return by_sku + cur.rowcount
