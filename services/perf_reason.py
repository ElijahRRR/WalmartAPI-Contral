"""绩效问题订单的归因:把报表原始行翻成一句「为什么这单被判违规」(唯一出处)。

背景(所有者 2026-09-03 提出):订单中心「绩效订单」表的**问题描述**与**明细**
是同一份东西 —— 问题描述把报表整行拍平成 `列名:值; 列名:值`,明细是同一行的
JSON。人看到的只有一堆单号和金额,看不出原因。本模块给出原因本身:
VTR 是「承运商无扫描」还是「追踪号无效」,取消是「缺货」还是「买家主动取消」。

归因优先级(逐级降级,上一级说不出才往下走):
  ① 报表自身的两处线索,**都用**:
     - sheet 桶名(sub_category):沃尔玛把这单记在**哪一类缺陷**上,
       即「为什么算我头上」;
     - 行内原因列:这单**具体发生了什么**。列名按指标写死(见 `_REASON_COL`),
       泛化匹配会抓错列 —— 实测 INR 的 `Return reason description` 被别的
       带 "reason" 的列抢走过。
     两者都有且不同就一起说:`错发商品(与描述/图片不符)`。
  ② 订单库补全:report 端点不给原因时,同一单的 `cancellationReason`(orders)
     与 `returnReason`(returns)已经在库里 —— 另一个端点答得上的问题,
     不要留空(所有者 2026-09-03:「如果请求的数据不全,也可以补全一下」);
  ③ 指标通用语:什么都没有时也要说人话(「追踪未通过校验」),
     **绝不回退成拍平原始行** —— 那正是本模块要消掉的东西。

桶名中文对照 `_BUCKETS` 两个来源,都不是自造:
  - **沃尔玛官方 Seller performance standards**(marketplacelearn 2026-09-03 核对);
  - **2026-09-03 生产报表实测**(M001 全 8 张 + 库内存量行):官方标准页没列、
     但报表在用的桶,如 Lost / Incorrect item / Defective / Damaged /
     Item Missing / Seller Issued Refund / Change_Mind * / Miscellaneous,
     以及 sheet 写「Address **is** not serviceable」这种与文档不同的措辞。
report 的 xlsx 列名/sheet 名官方从不公开(developer.walmart.com 只说「下载参与
计算的订单」),所以:**对不上的桶名原样透传 + 计数告警**,由 kpi 按 sheet 汇总
点名、人眼校准后再进词表 —— 静默归到「其他」就是又一次「解析 0 行」。
回归语料在 tests/test_perf_reason.py 的 PRODUCTION,官方改版式先在那里红。
"""

import logging
import re

logger = logging.getLogger("services.perf_reason")

# ── 表头/桶名归一:小写、非字母数字压成单空格 ────────────────────────────────
# "PO #"→"po"、"GMV loss"→"gmv loss"、"No Carrier Scan"→"no carrier scan"


def _key(s) -> str:
    # \W 在 py3 是 unicode 语义:中日韩桶名不会被压成空串(压空 = 静默丢线索)
    return re.sub(r"[\W_]+", " ", str(s or "").lower()).strip()


# ── ① 行内原因列 ────────────────────────────────────────────────────────────
# **按指标写死真实列名**(2026-09-03 生产报表实测,M001 全 8 张 + 库内存量):
# 一张报表可能有多列名字里带 "reason",泛化匹配会撞上错的那列 —— 实测把 INR 的
# 「Return reason description: Seller Issued Refund」抓成了别的列的 Miscellaneous。
# 指标专属列在前,泛化关键词只做新版式的兜底。
# ⚠ 只收真·原因列:商品类目("Category")、商品状态("Item Condition")不是原因,
# 收进来会把 "PLUMBING" 当成违规原因写进问题描述。
_REASON_COL = {
    "otd": ("late delivery reason", "delivery reason"),
    "returns": ("return reason description", "return reason"),
    "itemNotReceived": ("return reason description", "return reason"),
    "refunds": ("refund reason",),
    "cancellations": ("cancellation reason", "cancel reason"),
    "negativeFeedback": ("feedback reason", "feedback type"),
    "srr": ("inquiry type", "contact reason"),
}
_REASON_HEADERS = ("reason", "defect", "issue", "exception", "root cause",
                   "cancelled by", "canceled by", "disposition", "violation",
                   "error type", "failure")

# ── ② 官方缺陷桶 → 中文。(适用指标, 桶名别名, 中文);指标为空 = 通用 ──────
# 匹配用子串,同一轮内长别名优先(“invalid tracking url”先于“invalid tracking”)
_BUCKETS: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    # 取消率:官方 seller accountable 四类 + non-seller 两类
    ((), ("out of stock", "oos", "inventory issue"), "缺货取消"),
    ((), ("pricing error",), "定价错误取消"),
    ((), ("ship window expired", "shipping window expired"), "超发货窗口取消"),
    # 官方文档写 "address not serviceable",生产报表 sheet 是
    # "Address is not serviceable" —— 用两边都含的片段匹配
    ((), ("not serviceable",), "误标地址不可送达"),
    ((), ("customer requested", "customer request"), "买家主动取消"),
    ((), ("customer cancellation",), "买家取消"),
    ((), ("customer fraud",), "买家欺诈取消"),
    ((), ("seller cancel", "cancelled by seller", "canceled by seller"),
     "卖家取消"),
    # OTD
    ((), ("late hand over to carrier", "late handover to carrier"), "晚交运"),
    ((), ("late shipment", "late ship"), "发货晚"),
    ((), ("carrier delay",), "承运商延误"),
    ((), ("ship location mismatch",), "发货地不符"),
    ((), ("carrier method mismatch",), "配送方式不符"),
    ((), ("edd later than promised",
          "carrier estimated delivery date edd later than promised"),
     "承运商预计送达晚于承诺"),
    ((), ("carrier exception",), "承运商异常"),
    ((), ("weather delay",), "天气延误"),
    ((), ("late delivery", "delivered late"), "送达晚"),
    ((), ("undelivered", "never delivered"), "未送达"),
    # VTR(官方 seller accountable 五类)
    ((), ("no carrier scan", "no scan", "not scanned", "missing scan"),
     "承运商无扫描"),
    ((), ("invalid tracking url",), "追踪链接无效"),
    ((), ("invalid tracking id", "invalid tracking number",
          "invalid tracking"), "追踪号无效"),
    ((), ("misleading tracking",), "追踪信息不实"),
    ((), ("non integrated carrier", "unsupported carrier",
          "carrier not integrated"), "承运商未对接"),
    ((), ("no tracking", "missing tracking", "tracking not provided"),
     "未提供追踪号"),
    # SRR:官方按咨询类型分桶。⚠ 必须限定指标 —— "cancellation" 在取消率报表里
    # 是指标名不是咨询类型,不限定就会把取消率的行说成「咨询:取消」
    (("srr",), ("track order", "order tracking"), "咨询:查订单"),
    (("srr",), ("cancellation", "cancel"), "咨询:取消"),
    (("srr",), ("returns and refunds", "return refund"), "咨询:退货退款"),
    (("srr",), ("item issue", "product issue"), "咨询:商品问题"),
    (("srr",), ("feedback",), "咨询:评价"),
    # ── 以下为 2026-09-03 生产报表实测桶名(官方标准页没列,报表里真在用)──
    # 退款率 / 退货率 / 未收到 三张共用一套商品与包裹侧的原因词
    ((), ("incorrect item", "wrong item"), "错发商品"),
    ((), ("item arrived damaged",), "到货破损"),
    ((), ("damaged",), "商品破损"),
    ((), ("defective",), "商品有瑕疵"),
    ((), ("not as described pictured",), "与描述/图片不符"),
    ((), ("item not as described", "not as described"), "与描述不符"),
    ((), ("item size or comfort",), "尺寸或舒适度不合"),
    ((), ("lost after delivery",), "妥投后丢失"),
    ((), ("lost",), "包裹丢失"),
    ((), ("item missing", "missing item"), "商品缺失"),
    ((), ("arrived late",), "到货晚"),
    ((), ("seller issued refund",), "卖家已主动退款"),
    ((), ("no longer wanted",), "买家不想要了"),
    ((), ("change mind lower price",), "买家改主意:别处更便宜"),
    ((), ("change mind no longer wanted",), "买家改主意:不想要了"),
    ((), ("bought somewhere else",), "已在别处买到"),
    ((), ("miscellaneous",), "其他原因"),   # 见 _LOW_INFO_ZH:不给桶名当括号补语
    # 只表明"算不算卖家责任"、或只重复指标名、不含原因的桶名 —— 空 = 无信息,
    # 继续往下降级(别把「Negative Feedback」当成差评的原因写进问题描述)
    ((), ("seller accountable", "accountable"), ""),
    ((), ("not accountable", "non seller accountable"), ""),
    ((), ("negative feedback",), ""),
)

# 明确表示"没填/不适用"的取值:必须**整串相等**才算(子串匹配会误伤,
# 例如 "n a" 会撞进一堆正常英文里)。命中即当无信息,继续降级
_NO_INFO_EXACT = frozenset(("n a", "na", "none", "null", "not applicable",
                            "unknown", "other", "others"))

# 译出来也等于没说的原因:能当主原因(总比一片空白强),但不许挂在桶名后面
# 当补语 —— 「错发商品(其他原因)」这个括号一个字的信息量都没有
_LOW_INFO_ZH = frozenset(("其他原因",))

# ③ 订单库补全:哪个指标查哪个字段(值同样过 _BUCKETS 对照,对不上原样透传)
_CTX_FIELD = {"cancellations": "cancel_reason", "returns": "return_reason",
              "refunds": "return_reason", "itemNotReceived": "return_reason"}

# ④ 指标通用语:所有线索都没有时的兜底,分「计入绩效/不计入」两版
_GENERIC = {
    "otd": ("未按承诺时间送达", "送达超时但沃尔玛判非卖家责任"),
    "vtr": ("追踪未通过校验(报表未给细分原因)", "追踪异常但沃尔玛判非卖家责任"),
    "cancellations": ("订单被取消(报表未给原因)", "订单被取消,沃尔玛判非卖家责任"),
    "returns": ("买家退货", "买家退货,沃尔玛判非卖家责任"),
    "refunds": ("发生退款", "发生退款,沃尔玛判非卖家责任"),
    "negativeFeedback": ("买家差评", "买家差评,沃尔玛判非卖家责任"),
    "itemNotReceived": ("买家报告未收到货", "买家报告未收到货,沃尔玛判非卖家责任"),
    "srr": ("未在时限内回复买家咨询", "买家咨询未回复,沃尔玛判非卖家责任"),
}

# ── 佐证列:(表头关键词, 模板, 格式)。每指标最多取 3 条,顺序即优先级 ───────
# 列名按 2026-09-03 生产报表实测写(M001 全 8 张):
#   OTD  Delivered late by (Days) / Carrier / Shipping speed / Expected delivery
#        date / Actual delivery timestamp / Tracking number / Late Delivery Reason
#   取消 GMV loss / Category / Item ID / Item Condition / Order date /
#        Cancellation timestamp
#   退款 GMV loss / Category / Item ID / Item Condition / Order date / Refund Date
#   退货 Quantity returned / Return reason description / Sales Order # / PO # /
#        Order Line # / GMV loss        未收到 同上,数量列是 Quantity not received
# ⚠ **不许用会撞上时间列的宽关键词**:`refund` 会命中 `Refund Date`(实测写出
# 「退款 $2026-09-01」),`delivery date` 是 `Expected delivery date` 的子串
# (实测「实际送达」抓成了「承诺送达」)。非时间类佐证一律跳过时间列(见 describe)。
_MONEY, _TS = "money", "ts"
_EV_GMV = (("gmv loss", "gmv"), "损失 {}", _MONEY)
_EV_CARRIER = (("carrier",), "承运商 {}", "")
_EV_TRACK = (("tracking",), "单号 {}", "")
_EVIDENCE: dict[str, tuple[tuple[tuple[str, ...], str, str], ...]] = {
    "cancellations": (_EV_GMV,
                      (("cancellation timestamp", "cancellation date"),
                       "取消于 {}", _TS)),
    "vtr": (_EV_CARRIER, _EV_TRACK, (("ship date", "shipped"), "发货 {}", _TS)),
    "otd": ((("delivered late by", "days late"), "迟 {} 天", ""),
            _EV_CARRIER, _EV_TRACK),
    "itemNotReceived": (_EV_GMV, (("quantity not received",), "未收 {} 件", ""),
                        _EV_CARRIER),
    "returns": (_EV_GMV, (("quantity returned",), "退 {} 件", ""),
                (("return date", "return created"), "退货于 {}", _TS)),
    "refunds": (_EV_GMV, (("refund date",), "退款于 {}", _TS)),
    "negativeFeedback": ((("rating", "star"), "评分 {}", ""),
                         (("comment", "review"), "评论 {}", ""), _EV_GMV),
    "srr": ((("response time", "first response"), "响应 {}", ""),
            (("inquiry", "conversation", "topic"), "咨询 {}", "")),
}
_EV_LIMIT = 3           # 佐证最多 3 条:问题描述是给人扫一眼的,不是第二份明细
_EV_VALUE_MAX = 40      # 单条佐证值上限

_TS_RE = re.compile(r"\d{4}-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?")


def _fmt(value: str, fmt: str) -> str:
    """输入:原始值 + 格式名 → 输出:窄化后的展示值(金额补 $、时间只留月日时分)。"""
    v = str(value or "").strip()
    if fmt == _MONEY:
        return "$" + v.lstrip("$ ") if v else v
    if fmt == _TS:
        m = _TS_RE.search(v)
        if m:
            out = f"{m.group(1)}-{m.group(2)}"          # 纯日期列只到月日
            if m.group(3):
                out += f" {m.group(3)}:{m.group(4)}"
                if v.rstrip().upper().endswith("UTC"):
                    out += " UTC"
            return out
        return v[:_EV_VALUE_MAX]
    return v[:_EV_VALUE_MAX]


def _cells(detail) -> dict[str, str]:
    """输入:报表原始行(dict/其它)→ 输出:{归一表头: 非空值}。"""
    if not isinstance(detail, dict):
        return {}
    return {_key(k): str(v).strip() for k, v in detail.items()
            if str(v or "").strip()}


# 原因列关键词会误伤时间列("Issue date"/"Exception timestamp"):日期不是原因
_TIME_WORDS = ("date", "timestamp", "time")


def _pick(cells: dict[str, str], needles: tuple[str, ...],
          skip_time: bool = False) -> str:
    """输入:归一表头表 + 关键词(+ 是否跳过时间列)→ 输出:首个命中列的值。"""
    for needle in needles:
        for k, v in cells.items():
            if needle in k and not (skip_time
                                    and any(w in k for w in _TIME_WORDS)):
                return v
    return ""


def _bucket_zh(metric: str, raw: str) -> tuple[str, str]:
    """输入:指标 + 桶名原文 → 输出:(中文原因, 未归类桶名);两者互斥,都空=无信息。

    先按指标专属词条匹配(SRR 的「Cancellation」是咨询类型,不是取消率),
    再按通用词条;命中"只说责任归属"的空词条 = 有桶名但不含原因,当无信息处理。
    """
    text = _key(raw)
    if not text or text in _NO_INFO_EXACT:
        return "", ""
    for scoped in (True, False):
        hits: list[tuple[str, str]] = []
        for metrics, aliases, zh in _BUCKETS:
            if bool(metrics) != scoped or (scoped and metric not in metrics):
                continue
            hits.extend((alias, zh) for alias in aliases)
        for alias, zh in sorted(hits, key=lambda x: -len(x[0])):
            if alias in text:
                return zh, ""
    return "", str(raw).strip()


def classify(metric: str, sub_category, detail, accountable=None,
             ctx: dict | None = None) -> tuple[str, str]:
    """输入:指标键 + sheet 分类 + 报表原始行 + 是否计入绩效 + 订单库补全
    → 输出:(中文原因, 未归类桶名原文);未归类时原文照样进原因,不吞。

    accountable 传 None 表示未知(按计入绩效处理);ctx 见模块头 ③。
    未归类只在**它真的被写进原因**时才回报 —— 已经翻译出中文的行不该报警。
    """
    cells = _cells(detail)
    sheet_zh, sheet_miss = _bucket_zh(metric, sub_category)
    row_zh, row_miss = _bucket_zh(
        metric, _pick(cells, _REASON_COL.get(metric, ()) + _REASON_HEADERS,
                      skip_time=True))
    # sheet 桶名 = 沃尔玛把这单记在哪一类缺陷上(「为什么算我头上」),
    # 行内原因列 = 这单具体发生了什么(实测两者可以不同:sheet「Incorrect item」
    # 而买家填「Not as described/pictured」)。都有就都说,别丢掉任何一头
    if sheet_zh and row_zh and row_zh != sheet_zh and row_zh not in _LOW_INFO_ZH:
        return f"{sheet_zh}({row_zh})", ""
    if sheet_zh or row_zh:
        return sheet_zh or row_zh, ""
    unknown = sheet_miss or row_miss
    field = _CTX_FIELD.get(metric)
    if field and ctx and str(ctx.get(field) or "").strip():
        zh, miss = _bucket_zh(metric, ctx[field])
        # 补全值来自另一个端点,注明出处:人排查时才知道该去订单还是去报表核对
        src = "订单接口" if field == "cancel_reason" else "售后接口"
        return f"{zh or miss}({src})", unknown
    if metric == "vtr" and not _pick(cells, ("tracking",)) \
            and not str((ctx or {}).get("tracking_no") or "").strip():
        return "未提供追踪号", unknown          # 库里也没单号 = 根本没交追踪
    if unknown:
        return unknown, unknown                 # 桶名原样透传,等人校准词表
    generic = _GENERIC.get(metric)
    if not generic:
        return "", ""                           # 未知指标:交回调用方兜底
    return (generic[1] if accountable is False else generic[0]), ""


def describe(metric: str, sub_category, detail, accountable=None,
             ctx: dict | None = None, limit: int = 300,
             unknown_seen: dict | None = None) -> str | None:
    """输入:指标键 + sheet 分类 + 报表原始行(+ 是否计入绩效 + 订单库补全)
    → 输出:问题描述「原因 · 佐证 / 佐证」;未知指标且无线索返回 None。

    这是「问题描述」这一列的唯一生成口 —— ops.perf_problem_orders.description
    与飞书绩效表的问题描述都走它,两处口径一致。
    传 `unknown_seen` 时把未收录的桶名计进去(**本函数自己不打日志**:
    报表一个 sheet 上千行,逐行告警会把真正的告警冲掉),由调用方按张汇总告警。
    """
    reason, unknown = classify(metric, sub_category, detail, accountable, ctx)
    if unknown and unknown_seen is not None:
        unknown_seen[unknown] = unknown_seen.get(unknown, 0) + 1
    cells = _cells(detail)
    parts = []
    for needles, tpl, fmt in _EVIDENCE.get(metric, ()):
        val = _pick(cells, needles, skip_time=(fmt != _TS))
        if not val and "carrier" in needles:
            val = str((ctx or {}).get("carrier") or "")
        if not val and "tracking" in needles:
            val = str((ctx or {}).get("tracking_no") or "")
        if val and val[:_EV_VALUE_MAX] not in reason:
            parts.append(tpl.format(_fmt(val, fmt)))   # 已经在原因里的不再重复
        if len(parts) >= _EV_LIMIT:
            break
    if not reason:
        return " / ".join(parts)[:limit] or None
    return (reason + (" · " + " / ".join(parts) if parts else ""))[:limit]
