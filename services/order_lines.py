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

from services import sku_asin

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


def _order_date_of(order: dict, store_name: str, po: str) -> datetime | None:
    """输入:订单对象 + 店铺/PO(报错用)→ 输出:下单时间;未来日期拒写返 None 并告警。"""
    od = _ts(order.get("orderDate"))
    if od is None:
        return None
    limit = datetime.now(timezone.utc).timestamp() + _ORDER_DATE_TOLERANCE_SECS
    if od.timestamp() > limit:
        logger.warning("店铺 %s PO %s 的 orderDate=%s 晚于当前时刻,不是下单时间,拒写",
                       store_name, po, od.isoformat())
        return None
    return od


def extract_order_lines(store_name: str, order: dict) -> list[dict]:
    """输入:店铺名 + 单个 Walmart 订单 → 输出:order_lines 行 dict 列表。

    踩坑(实证):已发货订单不返回 estimated* 字段,est_ship 回退
    trackingInfo.shipDateTime,est_delivery 回退 fulfillment.pickUpDateTime。
    """
    # `or ""`而不是 get 默认值:信封里 "purchaseOrderId": null 时 dict.get 的默认值
    # 不生效,str(None) 会造出字面量 'None' 当 PO,让所有缺 PO 的订单跨店塌进同一行
    # (2026-09-02 对抗审查实证)。没有 PO 的订单没有身份,跳过并告警
    po = str(order.get("purchaseOrderId") or "")
    if not po:
        logger.warning("店铺 %s 一张订单没有 purchaseOrderId(customerOrderId=%s),"
                       "无身份不入库", store_name, order.get("customerOrderId"))
        return []
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
            # A1.5:落库当场清洗,不留给后台补(2026-08-15)。规则唯一出处
            # services/sku_asin;纯数字 item_id 形态这里提不出(要查库),
            # 由 order_asin_normalize 扫尾——所以下面的 upsert 用 COALESCE 守着
            "asin": sku_asin.extract_asin(sku),
            "product_name": str((ol.get("item") or {}).get("productName") or ""),
            "qty": int(_num((ol.get("orderLineQuantity") or {}).get("amount"), 1) or 1),
            "sale_status": st.get("status", ""),
            # 实证(2026-08-06 生产 raw):时间戳叫 statusDate 且在 orderLine 层,
            # PLAN 文档写的 orderLineStatus.statusSetDate 线上不存在,留作回退
            "status_date": _ts(ol.get("statusDate") or st.get("statusSetDate")),
            "order_date": _order_date_of(order, store_name, po),
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
    env = _order_envelope(order, rows)
    for r in rows:
        r["_envelope"] = env            # 下划线键不是列:_upsert 按列名取值,自动忽略
        if r["order_date"] is None and order.get("orderDate") not in (None, ""):
            r["_order_date_rejected"] = True
        elif _later_than_status(r["order_date"], r["status_date"]):
            # 下单时间不可能晚于本行的状态时间(状态变更发生在下单之后)。
            # 只标记不拒写:拒写会让整单从所有按下单时间取数的口子里消失
            r["_order_date_suspect"] = True
            logger.warning("店铺 %s PO %s SKU %s 下单时间 %s 晚于状态时间 %s,存疑;信封 %s",
                           store_name, po, r["sku"], r["order_date"].isoformat(),
                           r["status_date"].isoformat(), json.dumps(env, ensure_ascii=False))
    return rows


def _order_envelope(order: dict, rows: list[dict]) -> dict:
    """输入:订单对象 + 已展开行 → 输出:订单级信封摘要(取证:落 order_meta / 进冲突日志)。"""
    si = order.get("shippingInfo") or {}
    return {"orderDate": order.get("orderDate"),
            "customerOrderId": order.get("customerOrderId"),
            "orderType": order.get("orderType"),
            "estimatedShipDate": si.get("estimatedShipDate"),
            "estimatedDeliveryDate": si.get("estimatedDeliveryDate"),
            "lines": [{"line": r["line_number"], "sku": r["sku"], "status": r["sale_status"],
                       "statusDate": r["status_date"].isoformat() if r["status_date"] else None}
                      for r in rows],
            "seenAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _later_than_status(od, sd) -> bool:
    """输入:下单时间 + 状态时间 → 输出:下单时间是否晚于状态时间超过余量。

    沃尔玛的 statusDate 本身会回垃圾值(生产实见 0001-01-01 / 1970-01-01 /
    2026-01-01 00:00),拿它当参照会把整批行误标存疑,早于 2020 的一律不参照。
    """
    if not od or not sd or sd.year < 2020:
        return False
    return (od - sd).total_seconds() > _ORDER_DATE_TOLERANCE_SECS


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
# 算不出就别覆盖:order_sync 拿纯数字 sku 提不出 asin,而扫尾工作流查库能填出来
_ASIN_GUARD = "COALESCE(EXCLUDED.asin, t.asin)"

_PHONE_GUARD = ("CASE WHEN coalesce(EXCLUDED.phone, '') ~ '^0*$' "
                "AND coalesce(t.phone, '') !~ '^0*$' "
                "THEN t.phone ELSE EXCLUDED.phone END")

# 下单时间是**事实字段**(所有者定稿 2026-09-02:"下单时间不应该被修改")。
# 事故:09-02 13:20 一轮 order_sync 把三家店若干行的 order_date 写成别的订单的
# 下单时间(毫秒精度、与本单任何时间戳都不等),同一行的 raw/status_date 却是
# 本单自己的;另有一行(对账明细 PO 129123046265401)**首次写入**就是未来日期。
# 事后多轮同步/异步对拍,API 返回全部正确稳定。本仓从解析到 upsert 全程按名
# 取值,不存在串位通道 ⇒ 沃尔玛返回的 orderDate **单次读取不可信**。
# 所以拉取这一步不再相信任何一次读取,下单时间按"观测→定稿"两段走:
#   ① 首见只是候选:写入但不定稿(order_date_confirmed=false);
#   ② **连续两轮拉取给同一个值才定稿**,定稿后锁死不再改(写一次);
#   ③ 未定稿时若连续两轮出现同一个**不同**的值,改判为它(首见就错的自愈通道);
#   ④ 未来日期不是下单时间,拒写(留 NULL,后续轮次补上);晚于本行状态时间的
#      记"存疑"告警但不拒(避免误伤把全部新单藏起来);
#   ⑤ 每一次不一致都逐条记日志并进摘要首行,不静默;首见信封摘要落 order_meta 取证。
# 修复已定稿的错行走 repair_order_date 显式模式。表达式里 t = 目标表别名,
# 与 _ORDER_DATE_STATE_SQL 配对(先 upsert 定值,再记录本轮观测)。
_ORDER_DATE_GUARD = (
    "CASE WHEN t.order_date_confirmed THEN t.order_date"
    " WHEN EXCLUDED.order_date IS NULL THEN t.order_date"
    " WHEN t.order_date IS NULL THEN EXCLUDED.order_date"
    " WHEN EXCLUDED.order_date = t.order_date THEN t.order_date"
    " WHEN EXCLUDED.order_date = t.order_date_seen THEN EXCLUDED.order_date"
    " ELSE t.order_date END")

# 本轮观测记账(与 _ORDER_DATE_GUARD 同一事务内、在 upsert **之后**执行):
# order_date_seen 永远是"最近一轮观测到的值";定稿条件 = 本轮值 == 生效值 ==
# 上轮观测值(连续两轮一致)。本轮拒写(NULL)不动上轮观测。不碰 updated_at:
# 观测记账不是业务行变化,不许触发飞书重推。
_ORDER_DATE_STATE_SQL = (
    "UPDATE orders.order_lines t SET"
    " order_date_confirmed = CASE"
    "   WHEN t.order_date_confirmed THEN true"
    "   WHEN %(observed)s::timestamptz IS NULL THEN false"
    "   WHEN t.order_date = %(observed)s::timestamptz"
    "    AND t.order_date_seen = %(observed)s::timestamptz THEN true"
    "   ELSE false END,"
    " order_date_seen = COALESCE(%(observed)s::timestamptz, t.order_date_seen)"
    " WHERE t.order_line_id = %(id)s")

# 时间余量:晚于当前时刻超过它的 orderDate 拒写;晚于本行状态时间超过它的记存疑
_ORDER_DATE_TOLERANCE_SECS = 3600


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
    "postal_code", "country", "raw",
    # ⚠ source 留在覆盖列里是**有意的**:order_sync 的行不带这个键 → 写 NULL,
    # 于是 API 一拉到真行,历史标记自动摘掉,该行回到飞书推送流。
    # 把它挪进 skip_update 会让残缺行永远被当历史行排除在外。
    "source",
    # ⚠ asin 必须配 _ASIN_GUARD:order_sync 对纯数字 sku 算不出 asin(要查
    # walmart_items),裸写 EXCLUDED.asin 会把 order_asin_normalize 扫尾填好的
    # 值**冲回 NULL**——每轮同步抹一次,那一列永远填不满。COALESCE 保留旧值。
    "asin",
    # 下单时间定稿状态三列:只在插入时写(skip_update),更新走 _ORDER_DATE_STATE_SQL
    "order_date_seen", "order_date_confirmed", "order_meta"]
_ORDER_DATE_STATE_COLS = ("order_date_seen", "order_date_confirmed", "order_meta")

HISTORY_SOURCE = "历史数据"      # order_history_import 写;push 侧按它排除

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


_ORDER_DATE_EXISTING_SQL = ("SELECT order_line_id, po_id, sku, order_date,"
                            " order_date_seen, order_date_confirmed"
                            " FROM orders.order_lines"
                            " WHERE order_line_id = ANY(%(ids)s::text[])")


def order_date_conflicts(conn, rows: list[dict]) -> list[dict]:
    """输入:连接 + 待写行 → 输出:[{kind, po, sku, db, api}] 库里已有下单时间且与 API 不一致的行。

    在 upsert **之前**按库里现状分三类(kind),逐条记 warning 并交调用方进摘要——
    错值来源未明,这是抓证据的口子,不许静默;信封摘要一并进日志:
      冲突  已定稿,库值保留(写一次)
      改判  未定稿且 API 值与上轮观测一致 ⇒ 本轮 upsert 会改成 API 值(连续两轮)
      待定  未定稿且 API 值是新出现的 ⇒ 库值保留,等下一轮再看
    只比两边都非空的:库空 = 首见/此前拒写,API 空 = 本轮未来日期已被拒写。
    """
    api_by_id = {r["order_line_id"]: r for r in rows if r.get("order_date") is not None}
    if not api_by_id:
        return []
    with conn.cursor() as cur:
        cur.execute(_ORDER_DATE_EXISTING_SQL, {"ids": list(api_by_id)})
        existing = cur.fetchall()
    out: list[dict] = []
    for lid, po, sku, db_od, seen, confirmed in existing:
        row = api_by_id[lid]
        api_od = row["order_date"]
        if db_od is None or db_od == api_od:
            continue
        if confirmed:
            kind, verdict = "冲突", "已定稿,库值保留"
        elif seen is not None and seen == api_od:
            kind, verdict = "改判", "连续两轮一致,改为 API 值"
        else:
            kind, verdict = "待定", "库值保留,等下一轮"
        out.append({"kind": kind, "po": po, "sku": sku, "db": db_od, "api": api_od})
        logger.warning("PO %s SKU %s 下单时间%s:库 %s / API %s —— %s;信封 %s",
                       po, sku, kind, db_od.isoformat(), api_od.isoformat(), verdict,
                       json.dumps(row.get("_envelope"), ensure_ascii=False))
    return out


def upsert_order_lines(conn, rows: list[dict], *,
                       repair_order_date: bool = False) -> int:
    """输入:连接 + extract_order_lines 产出 → 输出:写入行数。审核列不在此覆盖。

    order_date 走观测→定稿两段(_ORDER_DATE_GUARD + _ORDER_DATE_STATE_SQL,同一
    事务):首见写入不定稿,连续两轮一致才定稿,定稿后锁死。repair_order_date=True
    是显式修复模式,本次调用允许 API 值直接覆盖库值(调用方须先看清冲突清单)。
    """
    rows = _dedupe_order_lines(rows)
    for r in rows:
        if isinstance(r.get("raw"), (dict, list)):
            r["raw"] = json.dumps(r["raw"], ensure_ascii=False, default=str)
        env = r.pop("_envelope", None)
        # 三列只在**插入**时生效(skip_update):首见未定稿、留信封取证;
        # order_date_seen 随后由 _record_order_date_observations 记成本轮观测值
        r["order_date_seen"] = None
        r["order_date_confirmed"] = False
        r["order_meta"] = json.dumps(env, ensure_ascii=False, default=str) if env else None
    guards = {"phone": _PHONE_GUARD, "asin": _ASIN_GUARD,
              "order_date": _ORDER_DATE_GUARD}
    if repair_order_date:
        guards.pop("order_date")
    n = _upsert(conn, "orders.order_lines", _ORDER_LINE_COLS,
                ["order_line_id"], rows, skip_update=_ORDER_DATE_STATE_COLS,
                guards=guards)
    _record_order_date_observations(conn, rows)
    return n


def _record_order_date_observations(conn, rows: list[dict]) -> None:
    """输入:连接 + 本轮已 upsert 的行 → 输出:无(逐行记录本轮 orderDate 观测并判定定稿)。"""
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(_ORDER_DATE_STATE_SQL,
                        [{"id": r["order_line_id"], "observed": r.get("order_date")}
                         for r in rows])


def _dedupe_order_lines(rows: list[dict]) -> list[dict]:
    """输入:一批待写行 → 输出:按 order_line_id 去重后的行(**先到者胜**),重复即告警。

    extract_order_lines 内的撞键告警只看得见同一张订单内部;两张信封给出同一
    (PO, SKU) 时,此前 executemany 按顺序静默后写覆盖先写,返回值还是塌缩前的
    行数,摘要看不出少了几行(2026-09-02 对抗审查实证)。先到者胜与写一次守卫
    同向:库里首见的下单时间是权威。
    """
    seen: dict[str, dict] = {}
    for r in rows:
        lid = r["order_line_id"]
        first = seen.get(lid)
        if first is None:
            seen[lid] = r
            continue
        logger.warning("同批内 PO %s SKU %s 出现两个订单对象(下单时间 %s vs %s),"
                       "后到的整行丢弃,请核查沃尔玛返回", r.get("po_id"), r.get("sku"),
                       first.get("order_date"), r.get("order_date"))
    return list(seen.values())


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
    → 输出:**送进来的行数**(不是实际写入数,与 upsert_lines 同口径)。
    同键重现只在内容真变了时才刷新 last_seen_at,保留 first_seen_at。

    ⚠ **内容没变就整行不写**(`WHERE ... IS DISTINCT FROM`)——与 upsert_lines
    的第 1 条同一个病、同一个药,2026-08-17 所有者实见后补上:
    绩效报表是**滚动**的,同一批问题订单每轮都会再出现一次。原先无条件
    `last_seen_at = now()` ⇒ 窗口内每一行的时间戳都被刷新,哪怕一个字段都没变;
    而 order_center 把 last_seen_at 当「拉取时间」写进飞书载荷、载荷又参与指纹
    ⇒ **指纹必变 ⇒ 每轮重推窗口内全部行**(实证:1634 行里更新 1093,正是
    still_active 的那批;剩下 541 行已滚出报表窗口,时间戳不动,指纹一致跳过)。
    这是自我循环:因为我们跑了时间戳才变,因为时间戳变了才推。

    ⚠ 变更检测比的是**生效后的值**(带 COALESCE)而不是 EXCLUDED:
    `order_line_id`/`sku` 的 COALESCE 会把"新值为 NULL"挡成不变,拿 EXCLUDED 比
    则会把这种假变化当成真变化,整行照写、时间戳照跳,等于闸没装。

    ⚠ `last_seen_at` **没有任何判据依赖它**(2026-08-17 逐个核过读者):
    `perf_event_spans.still_active` 看的是 `max(period) = latest_period`,不是它。
    唯一读者就是飞书投影那一列。所以少刷新它不影响任何结论。
    """
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
        WHERE (orders.perf_events.order_line_id, orders.perf_events.sku,
               orders.perf_events.accountable, orders.perf_events.status,
               orders.perf_events.detail)
          IS DISTINCT FROM
              (COALESCE(EXCLUDED.order_line_id, orders.perf_events.order_line_id),
               COALESCE(EXCLUDED.sku, orders.perf_events.sku),
               EXCLUDED.accountable, EXCLUDED.status, EXCLUDED.detail)
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
