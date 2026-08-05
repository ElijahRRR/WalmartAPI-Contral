"""店铺日报 KPI 的业务规则积木(daily_report 用)。

照搬旧系统的业务规则(docs/legacy_survey.md #daily_report「照搬」清单,别优化):
- 24h 销售窗口锚定中国时间 06:30(改锚点必须同步改调度时间)
- 上期回款严格取 scheduledSettlementDate 减 14 天那一期,不在可下载列表就填 0
- paymentStatus 非 ACTIVE 强制本期回款=0;回款<0 归 0;reserveToDate 取绝对值;
  「不押款」只在 ACTIVE 且 回款>=期末余额 时标
- sellerId 从 storeFrontUrl 正则 /seller/(\\d+) 提取
- 问题订单 xlsx:首行含 "Data current as of" 视为信息行(表头下移);
  任何单元格以 '=' 开头的行(Excel SUM 公式)整行跳过;
  sheet 名 == "Not Accountable" → 计入绩效 "⚪ 否",其余 sheet 名作为子分类
- 指标名带 emoji 前缀是隐式契约(日报的承运商分析按字符串精确匹配)
- 问题订单去重键 = (Sales Order #, 指标, 子分类, 物流单号, 商品) 五字段
"""

import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger("services.kpi")

CN_TZ = ZoneInfo("Asia/Shanghai")
WINDOW_END_HOUR, WINDOW_END_MINUTE = 6, 30      # 锚点改动必须同步改调度时间

# 指标 → (emoji 名, KPI 列名)。emoji 是下游日报匹配契约,改动会静默弄坏承运商分析。
METRIC_LABELS = {
    "otd": "🚚 OTD",
    "vtr": "🛰 VTR",
    "cancellations": "❌ 取消率",
    "refunds": "💰 退款率",
    "negativeFeedback": "⭐ 差评率",
    "returns": "📦 退货率",
    "itemNotReceived": "📭 未收到",
    "srr": "💬 SRR",
}

_SELLER_ID_RE = re.compile(r"/seller/(\d+)")


def sales_window_utc(now_utc: datetime | None = None) -> tuple[str, str]:
    """输入:当前 UTC 时间(默认 now)→ 输出:(开始, 结束) ISO8601 UTC,24h 窗口。

    锚点 = 最近一个已过去的中国时间 06:30,窗口向前推 24 小时。
    """
    now_cn = (now_utc or datetime.now(timezone.utc)).astimezone(CN_TZ)
    anchor = now_cn.replace(hour=WINDOW_END_HOUR, minute=WINDOW_END_MINUTE,
                            second=0, microsecond=0)
    if now_cn < anchor:
        anchor -= timedelta(days=1)
    start = anchor - timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (start.astimezone(timezone.utc).strftime(fmt),
            anchor.astimezone(timezone.utc).strftime(fmt))


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _find_key(node, key):
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def extract_settlement(statement: dict) -> dict:
    """输入:payment/statement 原始响应 → 输出:结算相关 KPI 字段 dict。

    ⚠对拍校准点:『本期回款』的取值字段旧代码未在摸底文档留痕,此处按
    scheduledSettlementAmount > totalPayable > (期末余额-备用金) 的优先级取,
    首轮对拍若与旧表不一致,以旧表反推字段后修正此函数。
    """
    acct = statement.get("accountSummary") or {}
    seller_info = statement.get("sellerInfo") or {}
    url = statement.get("storeFrontUrl") or ""
    m = _SELLER_ID_RE.search(url)

    closing = _num(_find_key(acct, "closingBalance"))
    reserve_to_date = abs(_num(_find_key(acct, "reserveToDate")))
    payout = None
    for key in ("scheduledSettlementAmount", "totalPayable", "payableAmount"):
        v = _find_key(statement, key)
        if v is not None:
            payout = _num(v)
            break
    if payout is None:
        payout = closing - max(_num(_find_key(acct, "reserve")), 0.0)

    payment_status = str(statement.get("paymentStatus")
                         or _find_key(statement, "paymentStatus") or "")
    is_active = payment_status.upper() == "ACTIVE"
    if not is_active or payout < 0:     # 非 ACTIVE 实际不会打款;负值归 0
        payout = 0.0
    no_hold = bool(is_active and payout >= closing)

    tx = statement.get("transactionDetails") or {}
    sale_agg = tx.get("saleAggregate") or {}
    refund = statement.get("refundDetails") or tx.get("refundDetails") or {}

    return {
        "partner_id": str(statement.get("partnerId")
                          or _find_key(statement, "partnerId") or ""),
        "seller_id": m.group(1) if m else "",
        "store_status": str(seller_info.get("sellerStatus") or ""),
        "payment_status": payment_status,
        "period_sales": _num(sale_agg.get("productPrice")),
        "commission": _num(sale_agg.get("netComm")),
        "refund_amount": _num(refund.get("productPrice")),
        "closing_balance": closing,
        "reserve_to_date": reserve_to_date,
        "payout": round(payout, 2),
        "payout_date": str(_find_key(acct, "scheduledSettlementDate") or ""),
        "payment_processor": str(_find_key(acct, "paymentProcessor") or ""),
        "settle_cycle": str(_find_key(acct, "settleCycle") or ""),
        "no_hold": no_hold,
    }


def prev_recon_date(settlement_date_raw: str) -> str | None:
    """输入:scheduledSettlementDate 原始值 → 输出:减 14 天的 MMDDYYYY,解析失败 None。

    业务规则:严格 -14 天,不许改成"找最近可用账期"(README 明令)。
    支持 epoch 毫秒 / ISO / MM/DD/YYYY 三种输入形态。
    """
    s = str(settlement_date_raw or "").strip()
    if not s:
        return None
    dt = None
    if s.isdigit() and len(s) >= 12:                 # epoch 毫秒
        dt = datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
    else:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(s[:19].split(".")[0], fmt)
                break
            except ValueError:
                continue
    if dt is None:
        logger.warning("scheduledSettlementDate 无法解析: %r", settlement_date_raw)
        return None
    return (dt - timedelta(days=14)).strftime("%m%d%Y")


def payment_summary_total(records) -> float:
    """输入:reconFileJson 记录迭代器 → 输出:PaymentSummary 行的 Total Payable(负归 0)。"""
    for rec in records:
        if str(rec.get("Transaction Type") or rec.get("transactionType") or "") == "PaymentSummary":
            total = _num(rec.get("Total Payable") or rec.get("totalPayable"))
            return max(total, 0.0)
    return 0.0


# ── 问题订单 xlsx 解析 ────────────────────────────────────────────────────────

# 表头 → 目标字段的模糊映射(旧系统按指标各写了一套精确映射,旧代码不可得;
# 此处按表头关键词归一,并把整行原文留在 raw 里供对拍校准。⚠对拍校准点)
_HEADER_MAP = (
    (("sales order",), "sales_order_no"),
    (("po #", "po#", "purchase order"), "po_no"),
    (("order date", "order placed"), "order_date"),
    (("item", "product"), "item"),
    (("carrier",), "carrier"),
    (("tracking",), "tracking_no"),
)


def _short(s: str, n: int = 30) -> str:
    """先按 '$$' 切前半(退货报告 Item name 带 $$ 分隔子分类),再截断。"""
    s = str(s or "").split("$$")[0].strip()
    return s[:n]


def parse_problem_report(metric: str, blob: bytes) -> list[dict]:
    """输入:指标名 + report xlsx 字节 → 输出:问题订单行 dict 列表(13 列语义)。"""
    import openpyxl

    label = METRIC_LABELS[metric]
    rows: list[dict] = []
    wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    for ws in wb.worksheets:
        data = [[("" if c is None else str(c)) for c in r]
                for r in ws.iter_rows(values_only=True)]
        if not data:
            continue
        start = 0
        if any("data current as of" in c.lower() for c in data[0]):
            start = 1                                   # 信息行,表头下移
        if start >= len(data):
            continue
        header = [h.strip() for h in data[start]]
        accountable = "⚪ 否" if ws.title.strip().lower() == "not accountable" else "✅ 是"
        sub_category = "" if accountable == "⚪ 否" else ws.title.strip()

        for raw in data[start + 1:]:
            if not any(c.strip() for c in raw):
                continue
            if any(c.strip().startswith("=") for c in raw):
                continue                                # Excel SUM 公式行
            rec = dict(zip(header, raw))
            row = {"indicator": label, "sub_category": sub_category,
                   "accountable": accountable,
                   "sales_order_no": "", "po_no": "", "order_date": "",
                   "item": "", "carrier": "", "tracking_no": "",
                   "description": "", "note": "",
                   "raw": json.dumps(rec, ensure_ascii=False)[:2000]}
            desc_parts = []
            for key, val in rec.items():
                k = key.lower()
                v = str(val).strip()
                if not v:
                    continue
                for needles, field in _HEADER_MAP:
                    if any(n in k for n in needles) and not row[field]:
                        row[field] = _short(v, 60) if field == "item" else v
                        break
                else:
                    desc_parts.append(f"{key}:{_short(v)}")
            row["description"] = "; ".join(desc_parts)[:300]
            if row["sales_order_no"] or row["po_no"] or row["tracking_no"]:
                rows.append(row)
    return rows


def dedup_key(row: dict) -> tuple:
    """输入:问题订单行 → 输出:五字段联合去重键(旧系统同款语义)。"""
    return (row.get("sales_order_no") or "", row.get("indicator") or "",
            row.get("sub_category") or "", row.get("tracking_no") or "",
            row.get("item") or "")
