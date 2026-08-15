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
- 问题订单去重键 = (Sales Order #, 指标, 子分类, 物流单号, 商品) 五字段。
  **本层不实现它** —— 去重由 workflows/perf_problems.py 的
  `ON CONFLICT (sales_order_no, indicator, sub_category, tracking_no, item)`
  唯一约束保证。曾有一份 Python 版 `dedup_key()` 与之并存且零调用
  (2026-08-14 删):同一语义两份实现,改去重键时漏改没人调的那份不会报错,
  只会制造"我改了去重逻辑"的错觉。
"""

import io
import json
import logging
import re
import urllib.parse
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


def _find_all(node, key, path="$"):
    """输入:JSON 树 + 键名 → 输出:[(路径, 值)] 全部出现位置(诊断/多值择优用)。"""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}"
            if k == key:
                out.append((p, v))
            out.extend(_find_all(v, key, p))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(_find_all(v, key, f"{path}[{i}]"))
    return out


# 结算响应里要盯的键(settle_debug 全量打点;回款字段以旧表反推修正时看这里)
_SETTLE_DEBUG_KEYS = (
    "storeFrontUrl", "scheduledSettlementAmount", "totalPayable",
    "payableAmount", "closingBalance", "reserve", "reserveToDate",
    "holdAmount", "scheduledSettlementDate", "paymentStatus", "partnerId",
    "sellerStatus", "paymentProcessor", "settleCycle")


def settlement_debug(statement: dict) -> str:
    """输入:payment/statement 原始响应 → 输出:关键键全部出现位置的报告。

    诊断"递归找键命中错节点"类问题:同名键多次出现时,extract_settlement
    的深度优先第一个可能是空值/非本期——本报告把每个键的所有 (路径, 值)
    列出来,对着旧表值即可反推正确路径。
    """
    lines = [f"响应顶层: {type(statement).__name__}"
             + (f" keys={sorted(statement.keys())}" if isinstance(statement, dict) else "")]
    for key in _SETTLE_DEBUG_KEYS:
        hits = _find_all(statement, key)
        if not hits:
            lines.append(f"  {key}: (无)")
        else:
            for p, v in hits[:6]:
                sv = str(v)
                lines.append(f"  {key} @ {p} = {sv[:120]}")
            if len(hits) > 6:
                lines.append(f"  {key}: …共 {len(hits)} 处")
    return "\n".join(lines)


def extract_settlement(statement: dict) -> dict:
    """输入:payment/statement 原始响应 → 输出:结算相关 KPI 字段 dict。

    2026-08-08 旧代码实证定稿(fetch_walmart_performance.fetch_payment_statement,
    弃用此前的字段猜测链):结构 = 顶层 partnerId + payload.{sellerInfo,
    accountSummary, transactionDetails.{saleAggregate,refundDetails}};
    sellerId = sellerInfo.storeFrontUrl **先 URL 解码**再正则 /seller/(\\d+)
    (URL 是百分号编码的,不解码永远匹配不上——2026-08-08 全店空白事故);
    **本期回款 = closingBalance − reserve − holdAmount**;
    paymentStatus 在 sellerInfo 里。递归查找仅作路径兜底。
    """
    payload = (statement.get("payload") if isinstance(statement, dict) else None) \
        or _find_key(statement, "payload") or {}
    seller_info = payload.get("sellerInfo") or _find_key(statement, "sellerInfo") or {}
    acct = payload.get("accountSummary") or _find_key(statement, "accountSummary") or {}
    td = payload.get("transactionDetails") \
        or _find_key(statement, "transactionDetails") or {}
    sale_agg = td.get("saleAggregate") or {}
    refund = td.get("refundDetails") or {}
    if not acct:
        logger.warning("payment/statement 找不到 accountSummary,顶层键:%s",
                       sorted(statement.keys()) if isinstance(statement, dict) else type(statement))

    url = urllib.parse.unquote(str(seller_info.get("storeFrontUrl") or ""))
    m = _SELLER_ID_RE.search(url)
    if not m:       # 路径兜底:扫全部 storeFrontUrl(同样先解码)
        for _, u in _find_all(statement, "storeFrontUrl"):
            m = _SELLER_ID_RE.search(urllib.parse.unquote(str(u or "")))
            if m:
                break

    closing = _num(acct.get("closingBalance"))
    reserve = _num(acct.get("reserve"))
    hold = _num(acct.get("holdAmount"))
    payout = closing - reserve - hold
    reserve_to_date = abs(_num(acct.get("reserveToDate")))

    payment_status = str(seller_info.get("paymentStatus")
                         or _find_key(statement, "paymentStatus") or "")
    is_active = payment_status.upper() == "ACTIVE"
    if not is_active or payout < 0:     # 非 ACTIVE 实际不会打款;负值归 0
        payout = 0.0
    no_hold = bool(is_active and payout >= closing)

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
        "payout_date": str(acct.get("scheduledSettlementDate") or ""),
        "payment_processor": str(acct.get("paymentProcessor") or ""),
        "settle_cycle": str(acct.get("settleCycle") or ""),
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
    (("order line",), "line_no"),   # returns/INR 版式带 Order Line # 无 SKU(2026-08-06 实证)
    (("order date", "order placed"), "order_date"),
    (("sku",), "sku"),          # 放 item 前:列名"Item SKU"应归 sku 而非商品名
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
    sheet_stats: list = []
    wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    for ws in wb.worksheets:
        # 生产实证(2026-08-06):沃尔玛生成的 xlsx 把 dimension 声明成单格,
        # read_only 模式按声明只读出 1 行 → 必须 reset_dimensions 重扫真实行
        if hasattr(ws, "reset_dimensions"):
            ws.reset_dimensions()
        data = [[("" if c is None else str(c)) for c in r]
                for r in ws.iter_rows(values_only=True)]
        sheet_stats.append((ws.title, len(data),
                            [c for c in (data[0] if data else [])[:6] if c]))
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

        data_rows = kept = 0
        for raw in data[start + 1:]:
            if not any(c.strip() for c in raw):
                continue
            if any(c.strip().startswith("=") for c in raw):
                continue                                # Excel SUM 公式行
            data_rows += 1
            rec = dict(zip(header, raw))
            row = {"indicator": label, "sub_category": sub_category,
                   "accountable": accountable,
                   "sales_order_no": "", "po_no": "", "line_no": "",
                   "order_date": "",
                   "sku": "", "item": "", "carrier": "", "tracking_no": "",
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
                kept += 1
        # 单号列一个都没匹配上 = _HEADER_MAP 关键词与真实表头不符,
        # 打出真实表头供校准——静默丢行就是"解析 0 行"事故的成因
        if data_rows and not kept:
            logger.warning("报表 %s sheet '%s':%d 行数据全被丢弃(未匹配到单号列),"
                           "真实表头=%s,首行样本=%s",
                           metric, ws.title, data_rows, header,
                           data[start + 1][:8] if len(data) > start + 1 else [])
    # 报表自述:每个 sheet 的(标题, 总行数, 首行前几格)——0 行产出时据此判断
    # 是"报表本来就空"还是"版式假设错了"(2026-08-06 生产校准期,稳定后可降 debug)
    logger.info("报表 %s 解析:%d 行产出,sheets=%s", metric, len(rows), sheet_stats)
    return rows


# ── 旧「店铺KPI」历史 sheet → ops.store_kpi_daily(kpi_history_import 用)────
# 表头关键词 → 目标字段,**顺序即优先级**(更具体的在前:上期回款 > 回款日期 >
# 回款;退款率 > 退款金额)。真实表头对不上的列进 unmapped 报告,人眼校准。
_HIST_HEADER_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 具体在前,泛化在后:「上期回款」>「回款日期」>「回款」;「退款率」>「退款」;
    # 「店铺状态」>「店铺」;「本期销售」>「销售额」;「回款日期」必须先于「日期」
    ("prev_payout", ("上期回款",)),
    ("payout_date", ("回款日期", "回款日")),
    ("refund_rate", ("退款率",)),
    ("store_status", ("店铺状态",)),
    ("period_sales", ("本期销售", "账期销售")),
    ("srr_rate", ("卖家回复", "srr")),      # 先于 seller_name:「卖家回复率」含「卖家」
    ("data_date", ("日期",)),
    ("store", ("店铺名", "店铺")),
    ("seller_name", ("卖家名称", "卖家")),
    ("partner_id", ("partner",)),
    ("seller_id", ("seller",)),
    ("payment_status", ("支付状态",)),
    ("sales_status", ("销售状态",)),
    ("items_online", ("在线商品", "在线")),
    ("items_in_stock", ("有库存",)),
    ("items_out_stock", ("无库存",)),
    ("orders_count", ("昨日出单", "出单")),
    ("sales_amount", ("昨日销售额", "销售额")),
    ("otd_rate", ("准时送达", "otd")),
    ("cancel_rate", ("取消",)),
    ("vtr_rate", ("有效追踪", "vtr")),
    ("negative_rate", ("差评",)),
    ("return_rate", ("退货",)),
    ("inr_rate", ("未收到", "inr")),
    ("commission", ("佣金",)),
    ("refund_amount", ("退款",)),
    ("closing_balance", ("期末余额", "余额")),
    ("reserve_to_date", ("备用金", "预留")),
    ("payout", ("回款",)),
    ("payment_processor", ("收款方", "支付处理", "processor")),
    ("settle_cycle", ("结算周期",)),
    ("no_hold", ("hold", "押款")),
)

_HIST_INT_FIELDS = {"items_online", "items_in_stock", "items_out_stock",
                    "orders_count"}
_HIST_NUM_FIELDS = {"sales_amount", "otd_rate", "cancel_rate", "vtr_rate",
                    "srr_rate", "refund_rate", "negative_rate", "return_rate",
                    "inr_rate", "period_sales", "commission", "refund_amount",
                    "closing_balance", "reserve_to_date", "payout", "prev_payout"}

_HIST_FIELDS = tuple(f for f, _ in _HIST_HEADER_MAP if f not in ("data_date", "store"))


def map_history_header(header: list) -> tuple[dict[int, str], list[str]]:
    """输入:表头行 → 输出:({列下标: 字段名}, 未映射表头列表)。

    关键词包含匹配(casefold),每个字段只认第一个命中的列(重复表头进 unmapped)。
    """
    mapping: dict[int, str] = {}
    taken: set[str] = set()
    unmapped: list[str] = []
    for i, h in enumerate(header):
        text = str(h or "").strip().casefold()
        if not text:
            continue
        for field, kws in _HIST_HEADER_MAP:
            if field not in taken and any(k in text for k in kws):
                mapping[i] = field
                taken.add(field)
                break
        else:
            unmapped.append(str(h).strip())
    return mapping, unmapped


def _hist_date(v) -> str | None:
    """输入:单元格值 → 输出:'YYYY-MM-DD' 或 None(解析失败)。"""
    s = str(v or "").strip().replace("/", "-").replace(".", "-")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    except ValueError:
        return None


def _hist_value(field: str, v):
    """输入:字段名 + 单元格值 → 输出:入库值(空串→None,$,%千分位清理)。"""
    s = str(v if v is not None else "").strip()
    if not s:
        return None
    if field == "no_hold":
        if s in ("是", "true", "True", "TRUE", "Yes", "✓", "√", "不押款"):
            return True     # 旧表该列实际取值:"不押款" / 空
        if s in ("否", "false", "False", "FALSE", "No", "×"):
            return False
        return None
    if field in _HIST_INT_FIELDS or field in _HIST_NUM_FIELDS:
        cleaned = s.replace("$", "").replace(",", "").replace("%", "").strip()
        try:
            num = float(cleaned)
        except ValueError:
            return None
        return int(num) if field in _HIST_INT_FIELDS else num
    return s


def parse_history_rows(store: str, header: list, rows: list[list]
                       ) -> tuple[list[dict], int]:
    """输入:店铺名(=sheet 标题,权威)+ 表头 + 数据行 → 输出:(入库行列表, 跳过数)。

    店铺一律取 sheet 标题(店铺列若有只作参考不用);日期解析失败的行跳过计数。
    """
    mapping, _ = map_history_header(header)
    out, skipped = [], 0
    for raw in rows:
        rec: dict = {f: None for f in _HIST_FIELDS}
        rec["store"] = store
        rec["data_date"] = None
        for i, field in mapping.items():
            v = raw[i] if i < len(raw) else None
            if field == "data_date":
                rec["data_date"] = _hist_date(v)
            elif field == "store":
                continue
            else:
                rec[field] = _hist_value(field, v)
        if not rec["data_date"]:
            skipped += 1
            continue
        out.append(rec)
    return out, skipped

