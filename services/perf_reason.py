"""绩效问题订单的归因:把报表原始行翻成一句「为什么这单被判违规」(唯一出处)。

背景(所有者 2026-09-03 提出):订单中心「绩效订单」表的**问题描述**与**明细**
是同一份东西 —— 问题描述把报表整行拍平成 `列名:值; 列名:值`,明细是同一行的
JSON。人看到的只有一堆单号和金额,看不出原因。本模块给出原因本身:
VTR 是「承运商无扫描」还是「追踪号无效」,取消是「缺货」还是「买家主动取消」。

归因优先级(**报表自身 > 官方分类 > 订单库补全 > 指标通用语**,逐级降级):
  ① 行内原因列:报表若带 reason/defect/issue/exception 一类列,取其值;
  ② sheet 分类(sub_category):沃尔玛把 report 按缺陷桶分 sheet,桶名即原因;
  ③ 订单库补全:report 端点不给原因时,同一单的 `cancellationReason`(orders)
     与 `returnReason`(returns)已经在库里 —— 另一个端点答得上的问题,
     不要留空(所有者 2026-09-03:「如果请求的数据不全,也可以补全一下」);
  ④ 指标通用语:什么都没有时也要说人话(「追踪未通过校验」),
     **绝不回退成拍平原始行** —— 那正是本模块要消掉的东西。

桶名中文对照 `_BUCKETS` 的词表出处是**沃尔玛官方 Seller performance standards**
(marketplacelearn 2026-09-03 核对),不是自造;report 的 xlsx 列名/sheet 名
官方从不公开(developer.walmart.com 只说「下载参与计算的订单」),所以:
**对不上的桶名原样透传 + 计数告警**,由 perf_problems 摘要点名、人眼校准后
再进词表 —— 静默归到「其他」就是又一次「解析 0 行」。
"""

import logging
import re

logger = logging.getLogger("services.perf_reason")

# ── 表头/桶名归一:小写、非字母数字压成单空格 ────────────────────────────────
# "PO #"→"po"、"GMV loss"→"gmv loss"、"No Carrier Scan"→"no carrier scan"


def _key(s) -> str:
    # \W 在 py3 是 unicode 语义:中日韩桶名不会被压成空串(压空 = 静默丢线索)
    return re.sub(r"[\W_]+", " ", str(s or "").lower()).strip()


# ── ① 行内原因列的表头关键词 ────────────────────────────────────────────────
# ⚠ 只收真·原因列:商品类目列("Category")、商品状态列("Item Condition")
# 都不是原因,收进来会把 "PLUMBING" 当成违规原因写进问题描述。
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
    ((), ("address not serviceable",), "误标地址不可送达"),
    ((), ("customer requested", "customer request"), "买家主动取消"),
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
    # 只表明"算不算卖家责任"、不含原因的桶名(官方两 sheet 版式)
    ((), ("seller accountable", "accountable"), ""),   # 空 = 无信息,继续降级
    ((), ("not accountable", "non seller accountable"), ""),
)

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
_MONEY, _TS = "money", "ts"
_EV_CANCEL = ((("gmv loss", "gmv"), "损失 {}", _MONEY),
              (("cancellation timestamp", "cancel date"), "取消于 {}", _TS))
_EV_SHIP = ((("carrier",), "承运商 {}", ""),
            (("tracking",), "单号 {}", ""))
_EVIDENCE: dict[str, tuple[tuple[tuple[str, ...], str, str], ...]] = {
    "cancellations": _EV_CANCEL,
    "vtr": _EV_SHIP + ((("ship date", "shipped"), "发货 {}", _TS),),
    "otd": ((("days late", "delay days"), "迟 {} 天", ""),
            (("expected delivery", "promised delivery", "promise date"),
             "承诺 {}", _TS),
            (("actual delivery", "delivered date", "delivery date"),
             "实际 {}", _TS)) + _EV_SHIP,
    "itemNotReceived": _EV_SHIP + ((("delivery status", "status"), "状态 {}", ""),),
    "returns": ((("return reason",), "退货原因 {}", ""),
                (("refund amount", "refund"), "退款 {}", _MONEY),
                (("return date", "return created"), "退货于 {}", _TS)),
    "refunds": ((("refund amount", "refund"), "退款 {}", _MONEY),
                (("refund date",), "退款于 {}", _TS)),
    "negativeFeedback": ((("rating", "star"), "评分 {}", ""),
                         (("comment", "review", "feedback"), "评论 {}", "")),
    "srr": ((("response time", "first response"), "响应 {}", ""),
            (("inquiry", "conversation", "topic"), "咨询 {}", "")),
}
_EV_LIMIT = 3           # 佐证最多 3 条:问题描述是给人扫一眼的,不是第二份明细
_EV_VALUE_MAX = 40      # 单条佐证值上限

_TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})")


def _fmt(value: str, fmt: str) -> str:
    """输入:原始值 + 格式名 → 输出:窄化后的展示值(金额补 $、时间只留月日时分)。"""
    v = str(value or "").strip()
    if fmt == _MONEY:
        return "$" + v.lstrip("$ ") if v else v
    if fmt == _TS:
        m = _TS_RE.search(v)
        if m:
            out = f"{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}"
            return out + " UTC" if v.rstrip().upper().endswith("UTC") else out
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
    if not text:
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
    """
    cells = _cells(detail)
    unknown = ""
    for raw in (_pick(cells, _REASON_HEADERS, skip_time=True), sub_category):
        zh, miss = _bucket_zh(metric, raw)
        if zh:
            return zh, ""
        unknown = unknown or miss
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
        val = _pick(cells, needles)
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
