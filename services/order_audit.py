"""订单审核积木(order_audit workflow 的全部业务判断,纯函数,不碰 IO)。

所有者定稿 2026-08-09,与旧系统(沃尔玛订单审核/)的口径差异已在此处收敛:

| 项 | 旧口径 | 新口径 |
|---|---|---|
| 钓鱼检测 | 黑名单地址(双向 substring)+ 黑名单邮编 | **只匹配邮编**,地址一整套不迁 |
| 限价 | 商品金额 × 0.75,成本用写死汇率 6.8 | 限价公式不变;汇率改**按采购方取** |
| 成本 | 单价×数量×汇率(+运费另计) | (单价×数量×采购方汇率 + 运费) × 1.08 |
| 配送时长 | ≥12 天建议拒绝 | **≥9 天**建议拒绝 |
| 商品一致性 | 标题相似度 0.9 | 阈值不变;相似度数值另出一列给人看 |
| 输出 | 单列 AN 文本 | 审核状态(结论)+ 脚本审核(明细)两列 |

审核顺序即优先级(见 judge):钓鱼 → **采不了的**(邮编/SKU 不合法)→
采集完整性 → **商品一致性** → 配送时长 → 采购方 → 限价。
任何一道给不出确定答案,结论都是"待人工",**绝不当作通过**。
一致性排在配送时长与限价之前:采到的若不是同一个商品,拿它算出来的限价
和货期全无意义,不该产出确定结论。

**闭环要求**:每条分支都得回一个三值结论 + 一句人能读的原因,并显式标出
`rescrape`(重采能不能解决)。"采不了的"必须与"待采集"用**不同文案**——
否则那些行会永远挂着等一个不会到来的快照,而摘要里的待采数还不含它们。

null-0 铁律:采集侧 stock_count / delivery_days 的 NULL 表示"没采到",
0 表示"确实是 0"。本模块只在值 is not None 时判定,None 一律走待人工——
把没采到当成 0 天送达会放行一批本该拒绝的单。
"""

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# 落地价只此一处定义(services/pricing):审核选采购方档位、maintenance 改价、
# list_new 上架定价,三处都吃「单价 + 运费」。各写各的迟早漂 —— 一处漏运费
# 就是选错档 / 定价偏低,而全程不报错。
from services.pricing import landed_price
from services.scrape_batches import ERROR_TYPES, RETRY_LATER, RETRYABLE, TERMINAL

# ⚠ **订单链的"终局"比产品库的宽一档**(2026-08-14 拆 RETRY_LATER 时定):
# 判据是**决策窗口**,不是错误类型的物理性质。
#   · 产品库补采的时间尺度是**周**——variant_offset / not_found 成因非确定性
#     (A/B 分桶、地域缓存、下架后重新上架),隔两周重提批次有意义 ⇒ 归 RETRY_LATER。
#   · 订单链的时间尺度是**小时**——一张单等着发货,冷却两周再给答案等于没答案。
#     在这个窗口内它们就是终局:采集侧对 variant_offset 自己永远不再试
#     (cap=1,force=true 也不越过),not_found 页面此刻确实取不到货源。
# 所以订单链取并集。合并不是"顺手统一口径",拆开也不是——这两个集合服务于
# 两个不同的问题,谁改都得先问"决策窗口多长"。
_TERMINAL_FOR_ORDER = TERMINAL | RETRY_LATER

logger = logging.getLogger("services.order_audit")

# ── 业务常量(所有者定稿 2026-08-09;改这里等于改钱,须所有者确认)──────────────
PROFIT_SAFETY_RATIO = 0.75   # 限价 = 沃尔玛商品金额 × 该比例
TAX_RATE = 1.08              # 采购成本税费系数(乘在汇率换算之后)
DELIVERY_DAYS_MAX = 9        # 配送时长 ≥ 该天数即建议拒绝
TITLE_SIMILARITY_MIN = 0.9   # 标题相似度低于此值 = 疑似采错商品,转待人工

# ── 结论文案(下游人工筛选与重采正则都依赖它,改字符串前先查引用)──────────────
PASS = "✓ 通过"
REJECT = "建议拒绝"
MANUAL = "待人工"

# 钓鱼标记不可覆盖(旧系统 审核决策.是钓鱼标记):一旦某行结论含此二字,
# 后续轮次不再改写该行,复审须人工清空审核状态。
PHISHING_MARK = "钓鱼"

# 合法 ASIN 形态(与采集侧 common/core/idents.ASIN_RE 同口径):B + 9 位大写字母数字。
# 订单行的 SKU 里混着旧系统留下的自定义编码,采集侧建任务时就把它们丢掉,
# 推了也是白推——这类行必须给出**与"待采集"不同的结论**,否则会永远挂着等
# 一个不会到来的快照。
ASIN_RE = re.compile(r"^B[0-9A-Z]{9}$")


# ══════════════════════════════════════════════════════════════════════════════
#  无货三档(采到了页面,但没有可买的报价)
# ══════════════════════════════════════════════════════════════════════════════

#: stock_status(在 snapshots.raw 顶层,契约未列为一等字段)→ (原因码, 页面原文)
#: ⚠ 第二项是**采集侧页面原文**,不是给人读的句子:维护链已经把它写进生产的
#: 「维护记录」原因列了,改字面等于改线上数据。**共用的是原因码,措辞归各链**
#: (审核链的句子在 STOCK_BLOCK_SAY)。
_STOCK_STATUS_BLOCK = {
    "currently unavailable": ("unavailable", "Currently unavailable"),
    "no featured offer": ("no_buybox", "No Featured Offer"),
}

#: 原因码 → **审核链**的人话。审核结论是给运营看的一句话,不能只甩个英文原文。
STOCK_BLOCK_SAY = {
    "unavailable": "亚马逊在架但不可售(Currently unavailable)",
    "no_buybox": "亚马逊无 Buy Box(No Featured Offer)",
    "out_of_stock": "亚马逊缺货(out_of_stock)",
}


def stock_block(stock_status=None, stock_state=None) -> tuple | None:
    """输入:两个库存信号 → 输出:(原因码, 人话)表示"没有可买的报价";正常返回 None。

    **全项目唯一出处**,维护链与审核链共用(`maintenance_intents.classify` 调它)。
    三档都是"页面采到了、但此刻没有能买的东西",与 `outcome == 'not_found'`
    (页面本身没了)是两回事——后者货源永久消失,前者可能过几小时就回来。

    两条链拿它做的事不同,但**判据必须同一份**:
      维护链 → 库存写 0(在架却买不到,不能让顾客下单)
      审核链 → 待人工 + 重采(此刻算不出限价,但不是采集出了错)
    各写一份的下场是"维护把它清零了,审核还在报『采集缺字段』",同一个商品
    两条链说两种话,而且两边都不报错。

    ⚠ 顺序即优先级:stock_status 比 stock_state 具体(前者是页面原文,后者是
    采集侧归一化出来的三值),先看具体的。
    """
    hit = _STOCK_STATUS_BLOCK.get(str(stock_status or "").strip().lower())
    if hit:
        return hit
    if str(stock_state or "").strip().lower() == "out_of_stock":
        return ("out_of_stock", "亚马逊缺货")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  邮编标准化与钓鱼检测
# ══════════════════════════════════════════════════════════════════════════════

def norm_zip(v) -> str:
    """输入:任意邮编写法 → 输出:5 位标准邮编(取不出则空串)。

    zip+4 必须识别为前 5 位:'60606-6771' → '60606'(旧系统实证,通用去标点
    标准化会把它变成 606066771 从而漏掉黑名单)。全角/空格/引号一并清掉。
    """
    if v is None:
        return ""
    digits = re.sub(r"\D", "", str(v))
    return digits[:5] if len(digits) >= 5 else ""


def zip_blacklist(rows: list[list]) -> set[str]:
    """输入:黑名单邮编表原始行(A 列邮编,无表头)→ 输出:标准化邮编集合。"""
    out = {norm_zip(r[0]) for r in rows if r}
    out.discard("")
    return out


def is_phishing(postal_code, blacklist: set[str]) -> bool:
    """输入:订单收件邮编 + 黑名单集合 → 输出:是否命中钓鱼邮编。"""
    z = norm_zip(postal_code)
    return bool(z) and z in blacklist


# ══════════════════════════════════════════════════════════════════════════════
#  采购方匹配
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Supplier:
    """一行采购方配置:配送方式 + 单价区间 + 汇率。"""

    name: str
    ship_method: str      # FBA / FBM(大写归一)
    band_from: float
    band_to: float
    rate: float


def _num(v, default=None):
    """输入:飞书单元格值 → 输出:float;空/非数返回 default。"""
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.\-]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return default


def _text(v) -> str:
    """输入:飞书单元格值(可能是分段/数组结构)→ 输出:去空白纯文本。"""
    if isinstance(v, list):
        return "".join(_text(x) for x in v)
    if isinstance(v, dict):
        return str(v.get("text", "")).strip()
    return "" if v is None else str(v).strip()


def _enabled(v) -> bool:
    """输入:「是否启用」单元格 → 输出:是否启用(复选框 True / 文本 是|启用|Y|1)。"""
    if isinstance(v, bool):
        return v
    s = _text(v).casefold()
    return s in {"是", "启用", "y", "yes", "true", "1", "√", "✓"}


def parse_suppliers(records: list[dict], fields) -> list[Supplier]:
    """输入:采购方表记录 + 字段常量 → 输出:启用且区间合法的 Supplier 列表。

    跳过(并告警)的行:未启用、采购方名为空、汇率缺失、区间起止取不出数。
    区间缺一端按开区间处理(起空=0,止空=正无穷)——运营常只填一端表示"以上/以下"。
    """
    out, skipped = [], 0
    for rec in records:
        f = rec.get("fields", rec)
        if not _enabled(f.get(fields.enabled)):
            continue
        name = _text(f.get(fields.supplier))
        rate = _num(f.get(fields.rate))
        if not name or rate is None:
            skipped += 1
            continue
        lo = _num(f.get(fields.band_from), 0.0)
        hi = _num(f.get(fields.band_to), float("inf"))
        if lo is None or hi is None or hi < lo:
            skipped += 1
            continue
        out.append(Supplier(name=name,
                            ship_method=_text(f.get(fields.ship_method)).upper(),
                            band_from=lo, band_to=hi, rate=rate))
    if skipped:
        logger.warning("采购方表跳过 %d 行(名称/汇率缺失或区间非法),已启用 %d 行",
                       skipped, len(out))
    return out


def pick_supplier(suppliers: list[Supplier], ship_method, landed_price):
    """输入:采购方列表 + 亚马逊配送方式 + **亚马逊落地价** → 命中的 Supplier(无则 None)。

    命中条件:配送方式相同(大小写无关)且**落地价**落在 [区间起, 区间止]
    闭区间内。多个候选取**汇率最低**者(旧系统 采购方匹配.py:80-87 语义,
    逐字保留:汇率越低采购成本越低,对限价最有利)。

    ⚠ 落地价 = **亚马逊单价 + 亚马逊运费**(所有者定稿 2026-08-10),三点澄清:
    - **不是沃尔玛那边的销售金额**——区间说的是"我们要花多少钱买",
      跟卖多少钱无关;
    - **不是单看单价**——25.99 + 12 运费的东西该落进 30+ 那一档,按 25.99
      去匹配会挑到错的采购方,进而用错汇率,成本算小、本该拒的单被放行;
    - 与 `walmart_price` 的定价口径同源(那边也是「单价 + 运费」再乘倍率),
      两处必须一致,否则同一件商品选档和定价各说各话。

    运费没采到(None)时调用方就不该走到这儿:落地价算不出来 ⇒ 匹配不了,
    绝不 `or 0` 拿单价顶替(那会稳定挑到偏低的一档)。
    """
    m = _text(ship_method).upper()
    price = _num(landed_price)
    if not m or price is None:
        return None
    hit = [s for s in suppliers
           if s.ship_method == m and s.band_from <= price <= s.band_to]
    return min(hit, key=lambda s: s.rate) if hit else None


# ══════════════════════════════════════════════════════════════════════════════
#  商品一致性(标题相似度)
# ══════════════════════════════════════════════════════════════════════════════

def _norm_title(v) -> str:
    """输入:标题 → 输出:比对用归一化文本(小写、去标点、折叠空白)。"""
    s = _text(v).casefold()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def title_similarity(walmart_title, amazon_title):
    """输入:沃尔玛商品名 + 亚马逊标题 → 输出:相似度 0~1(任一为空返回 None)。

    None 与 0.0 是两回事:None = 有一边根本没有标题(算不了),
    0.0 = 两个标题都在、但完全不像。前者走待人工,后者是明确的不一致。
    """
    a, b = _norm_title(walmart_title), _norm_title(amazon_title)
    if not a or not b:
        return None
    return round(SequenceMatcher(None, a, b).ratio(), 4)


# ══════════════════════════════════════════════════════════════════════════════
#  限价
# ══════════════════════════════════════════════════════════════════════════════

def price_cap(product_amount):
    """输入:沃尔玛行商品金额 → 输出:限价 = 金额 × 0.75(取不出金额返回 None)。"""
    amt = _num(product_amount)
    return None if amt is None else round(amt * PROFIT_SAFETY_RATIO, 2)


def purchase_cost(unit_price, qty, rate, shipping):
    """输入:亚马逊单价/数量/采购方汇率/运费 → 输出:含税采购成本(算不出返回 None)。

    公式(所有者定稿 2026-08-09):(单价 × 数量 × 汇率 + 运费) × 1.08。
    注意运费**不乘汇率**——按所有者给定的写法原样实现。

    ⚠ **运费 None 时返回 None,绝不当 0**(采集契约不变量 3b,与 stock_count
    同一条):采集侧 `FREE → 0.0` 是"确认免运费"这条真信息,`N/A → null` 是
    "这次没采到"。把没采到当 0,成本照样算得出来、看着也正常,只是**偏小**,
    于是本该拒的单被放行,而两侧都不会报错。宁可转待人工。
    """
    p, q, r, s = _num(unit_price), _num(qty), _num(rate), _num(shipping)
    if p is None or q is None or r is None or s is None:
        return None
    return round((p * q * r + s) * TAX_RATE, 2)


def price_ok(cost, cap) -> bool:
    """输入:采购成本 + 限价 → 输出:是否价格通过(成本 ≤ 限价)。"""
    return cost is not None and cap is not None and cost <= cap


# ══════════════════════════════════════════════════════════════════════════════
#  审核决策链
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AuditResult:
    """一行的审核产物:两列结论 + 供飞书展示与落库的明细。"""

    status: str                 # PASS / REJECT / MANUAL
    note: str                   # 「脚本审核」列文本(明细原因)
    detail: dict                # 落 orders.order_lines.audit_detail
    # 重采能不能解决它。True 只给"数据本身不可用"的四种:没快照 / outcome≠ok /
    # 缺配送方式或配送时长 / 运费没采到。**不给**无匹配采购方(配置问题)、
    # 标题不符(重采还是同一个商品)、商品金额缺失(沃尔玛侧的事)——
    # 对这些重采只是白烧采集配额,行还是卡在待人工。
    rescrape: bool = False

    @property
    def is_phishing(self) -> bool:
        return PHISHING_MARK in self.note


def judge(line: dict, snap: dict | None, suppliers: list[Supplier],
          blacklist: set[str], scrape_fail: str | None = None) -> AuditResult:
    """输入:订单行 + 该行邮编下的亚马逊快照 + 采购方配置 + 邮编黑名单
    (+ 该 (ASIN,邮编) 上次采集的失败 error_type)
    → 输出:AuditResult(审核状态 + 脚本审核明细 + 落库 detail)。

    line 用 orders.order_lines 的列名;snap 用 services.order_audit.from_snapshot
    产出的统一形状(见该函数)——采集器换端点只改那一处,本函数不动。
    """
    detail: dict = {"rules": {}}

    # 限价只跟沃尔玛这一侧的商品金额有关(× 0.75),**跟采不采得到、
    # 有没有匹配到采购方都无关**。所以一进门就算出来落进 detail:
    # 「无匹配采购方」那些行原先连限价列都是空的,人工复核时连个参照都没有。
    detail["price_cap"] = price_cap(line.get("product_amount"))

    # ① 钓鱼:命中即终局,不看采集、不算限价(采都不用采,省一次请求)
    if is_phishing(line.get("postal_code"), blacklist):
        z = norm_zip(line.get("postal_code"))
        detail["rules"]["phishing"] = {"hit": True, "zip": z}
        return AuditResult(REJECT, f"{PHISHING_MARK}邮编:{z}", detail)
    detail["rules"]["phishing"] = {"hit": False}

    # ② 采不了的两种:说"待采集"就是骗人,得让人一眼看出要人工处理。
    #    (不标 rescrape:推了也白推,而且行会一直挂着"待采集"等一个永远不来的
    #     快照——这正是"静默卡死"的典型形状)
    zip5 = norm_zip(line.get("postal_code"))
    if not zip5:
        return AuditResult(MANUAL, "收件邮编缺失或不足 5 位,无法按邮编采集,"
                                   "需人工核对地址", detail)
    asin = str(line.get("sku") or "").strip().upper()
    if not ASIN_RE.fullmatch(asin):
        return AuditResult(MANUAL, f"SKU「{asin or '空'}」不是 ASIN 形态,"
                                   f"采集器不受理,需人工核对", detail)

    # ③ 采集完整性:没采到 / 采到但缺关键字段 → 待人工,绝不当通过
    if not snap:
        # 上次采集是**在本单的决策窗口内重采也拿不到**的那类失败
        # (parse_error / server_reject / variant_offset …)⇒ 给终局结论。
        # 所有者定稿 2026-08-10:直接建议拒绝,后续不再重采。
        # 挂"待采集"才是害人的——行会一直等一个永远不来的快照,而每轮
        # 还要为它烧一次配额,两侧都不报错(典型的静默卡死)。
        # 措辞 2026-08-14 订正:variant_offset 不是"永远拿不到"(成因非确定性,
        # 隔冷却期重提批次有意义,见 _TERMINAL_FOR_ORDER 头注),但在一张单
        # 等着发货的窗口内它就是拿不到——终局结论对订单链依然成立。
        if scrape_fail in _TERMINAL_FOR_ORDER:
            detail["rules"]["scrape"] = {"error_type": scrape_fail,
                                         "retryable": False}
            return AuditResult(REJECT,
                               f"采集失败且重采无效({scrape_fail}:"
                               f"{ERROR_TYPES.get(scrape_fail, '未登记类型')}),"
                               f"拿不到亚马逊数据,建议拒绝", detail)
        if scrape_fail:
            detail["rules"]["scrape"] = {"error_type": scrape_fail,
                                         "retryable": True}
        return AuditResult(MANUAL, "待采集:该 ASIN 在本单邮编下无亚马逊快照",
                           detail, rescrape=True)
    detail.update({k: snap.get(k) for k in
                   ("asin", "zip", "amz_price", "shipping", "shipping_raw",
                    "stock_qty", "ship_method", "ship_days", "seller",
                    "amz_title", "scraped_at")})
    if snap.get("outcome") and snap["outcome"] != "ok":
        # 契约的 outcome 是**粗粒度**的(所有者 2026-08-10 澄清):一条
        # `parse_failed` 底下可能是 network / timeout / parse_error 任意一种,
        # 真实原因只有 `/api/batches/{id}/failures` 的 error_type 说得准 ——
        # 台账已把它落在 ops.audit_scrape.error_type,这里直接用。
        # 拿 outcome 当原因去决定"要不要再采",等于把可重试的和不可重试的
        # 混成一桶:前者放弃得太早,后者每轮白烧一次配额。
        detail["rules"]["scrape"] = {"outcome": snap["outcome"],
                                     "error_type": scrape_fail}
        if scrape_fail in _TERMINAL_FOR_ORDER:
            return AuditResult(REJECT,
                               f"采集未成功({snap['outcome']} → 实际 {scrape_fail}:"
                               f"{ERROR_TYPES[scrape_fail]}),重采无效,建议拒绝",
                               detail)
        if snap["outcome"] == "not_found":
            # 所有者拍板 2026-08-12:页面 404 = 产品已从亚马逊下架,货源没了;
            # 此前白烧一天重采配额再堆待人工。审核只出建议,人工仍是最后一道。
            # 2026-08-14 订正措辞(不改行为):404 在**产品库**那侧归 RETRY_LATER
            # ——下架后重新上架是常事,采集侧注释也明写"下一轮定时采集自动纠正"。
            # 但订单链等不了那一轮:此刻没货源就是没货源,终局结论依旧成立。
            return AuditResult(REJECT,
                               "采集未成功(not_found:亚马逊页面不存在/已下架,"
                               "货源缺失),重采无效,建议拒绝", detail)
        why = (f"{snap['outcome']} → 实际 {scrape_fail}" if scrape_fail
               else snap["outcome"])
        return AuditResult(MANUAL, f"采集未成功({why}),已排重采",
                           detail, rescrape=True)

    missing = [n for n, v in (("亚马逊单价", snap.get("amz_price")),
                              ("配送方式", snap.get("ship_method")),
                              ("配送时长", snap.get("ship_days")))
               if v is None or v == ""]
    if missing:
        # ⚠ 先分清**为什么**缺:采到了页面、但此刻没有可买的报价(不可售 /
        # 无 Buy Box / 缺货)时,单价与货期本来就不存在——那不是采集出了错。
        # 报「采集缺字段」会把人指向采集器,而采集器什么都没做错;人去查一遍
        # 才发现商品页好好的,只是 Currently unavailable(所有者 2026-08-17 实遇)。
        # 判据与维护链同一份(order_audit.stock_block),那边据此把库存写 0。
        blocked = stock_block(snap.get("stock_status"), snap.get("stock_state"))
        if blocked:
            detail["rules"]["stock"] = {"code": blocked[0],
                                        "stock_status": snap.get("stock_status"),
                                        "stock_state": snap.get("stock_state")}
            return AuditResult(
                MANUAL, f"{STOCK_BLOCK_SAY.get(blocked[0], blocked[1])},"
                        f"拿不到单价与货期(缺:{'、'.join(missing)}),已排重采",
                detail, rescrape=True)
        return AuditResult(MANUAL, "采集缺字段:" + "、".join(missing) + ",已排重采",
                           detail, rescrape=True)

    # ④ 商品一致性:采到的得是同一个商品,否则后面拿它算的限价全无意义,
    #    所以排在配送时长与限价**之前**——垃圾输入不该产出确定结论
    sim = title_similarity(line.get("product_name"), snap.get("amz_title"))
    detail["title_similarity"] = sim
    detail["rules"]["title"] = {"similarity": sim, "min": TITLE_SIMILARITY_MIN}
    if sim is None:
        held = "标题取不到(沃尔玛或亚马逊一侧为空)"
    elif sim < TITLE_SIMILARITY_MIN:
        held = f"商品可能不一致:标题相似度 {sim} < {TITLE_SIMILARITY_MIN}"
    else:
        held = None

    def out(status: str, note: str, **kw) -> AuditResult:
        """商品一致性存疑时把结论**降级为待人工**,但后面的判定照跑完。

        所有者定稿 2026-08-10:标题不符的行也要分配采购方 —— 人工复核时
        得看到"如果这真是同一个商品,该找谁采、成本多少、过不过限价",
        否则只有一句"可能不一致",什么都判断不了。

        降级而不是直接 return,是为了同时守住原来那条不变量:**垃圾输入
        不该产出确定结论**。商品都可能不是同一个,拿它算出来的"通过"或
        "建议拒绝"都不作数,所以状态一律回到待人工,原因里两条都写上。
        """
        if held and status != MANUAL:
            return AuditResult(MANUAL, f"{held},待人工(供参考:{note})",
                               detail, **kw)
        if held:
            return AuditResult(MANUAL, f"{held};{note}", detail, **kw)
        return AuditResult(status, note, detail, **kw)

    # ⑤ 配送时长闸(≥9 天建议拒绝)
    days = _num(snap.get("ship_days"))
    detail["rules"]["delivery"] = {"days": days, "max": DELIVERY_DAYS_MAX}
    if days is not None and days >= DELIVERY_DAYS_MAX:
        return out(REJECT, f"配送时长 {int(days)} 天 ≥ {DELIVERY_DAYS_MAX} 天")

    # ⑥ 落地价 = 亚马逊单价 + 亚马逊运费。**采购区间按它匹配**,不是按单价,
    #    更不是按沃尔玛的销售金额(所有者定稿 2026-08-10)。
    #    运费没采到就停在这儿:落地价算不出来 ⇒ 选不了档也算不出成本。
    #    绝不 `or 0` —— 那会稳定挑到偏低的一档 + 成本偏小,本该拒的单被放行,
    #    而改价、下单、飞书全程不报错。
    landed = landed_price(snap.get("amz_price"), snap.get("shipping"))
    detail["landed_price"] = landed
    if landed is None:
        return out(MANUAL, "运费没采到,亚马逊落地价算不出来"
                           "(选不了采购方档位),已排重采", rescrape=True)

    # ⑦ 采购方匹配(无命中 → 待人工:没有汇率就算不出成本,不能猜)
    sup = pick_supplier(suppliers, snap.get("ship_method"), landed)
    if sup is None:
        detail["rules"]["supplier"] = {"hit": False, "landed": landed}
        return out(MANUAL,
                   f"无匹配采购方({_text(snap.get('ship_method')).upper()} / "
                   f"落地价 {landed} = 单价 {snap.get('amz_price')}"
                   f" + 运费 {snap.get('shipping')}),待人工")
    detail["supplier"] = sup.name
    detail["rate"] = sup.rate
    detail["rules"]["supplier"] = {"hit": True, "name": sup.name, "rate": sup.rate,
                                   "landed": landed}

    # ⑧ 限价(cap 一进门就算过了,这里只算成本)
    cap = detail["price_cap"]
    cost = purchase_cost(snap.get("amz_price"), line.get("qty"), sup.rate,
                         snap.get("shipping"))
    detail["cost"] = cost
    detail["rules"]["price"] = {"cap": cap, "cost": cost}
    if cap is None or cost is None:
        return out(MANUAL, "商品金额或采购成本算不出,待人工")
    if not price_ok(cost, cap):
        return out(REJECT, f"限价不通过:成本 {cost} > 限价 {cap}")

    return out(PASS, f"成本 {cost} ≤ 限价 {cap};采购方 {sup.name}")


# ══════════════════════════════════════════════════════════════════════════════
#  按邮编采集的波次编排
# ══════════════════════════════════════════════════════════════════════════════

def plan_waves(pairs, blocked) -> list[list[tuple[str, str]]]:
    """输入:待采 [(asin, 邮编)] + 不可推的 {(asin, 邮编)} → 输出:波次列表。

    **一批可以混不同 ASIN 的不同邮编;只有同一 ASIN 的多个邮编才必须拆批。**
    (所有者定稿 2026-08-10;中途我按"一个邮编一个批次"收紧过,理由查证有误,
    已纠正回来——见下方两条实证。)

    采集侧 `POST /api/batches` 的邮编是**三档独立**的(server/api/batches.py
    文档串):`items[].zip_code`(这一个 ASIN)> 顶层 `zip_code`(这一批)>
    服务端默认。所以逐 ASIN 带邮编是接口一等能力,不是变通。

    唯一的硬约束来自库结构:`tasks` 上有 `UNIQUE(batch_id, asin)`,一个批次里
    一个 ASIN 只能有一个邮编。同批给同一 ASIN 两个不同邮编 ⇒
    `400 conflicting_zip_for_asin`(采集侧明确拒绝,不静默取第一个)。
    故按"同一 ASIN 的第 k 个邮编进第 k 波"分批,每波内 ASIN 不重复。

    ⚠ 两条曾被我当成"必须一个邮编一个批次"的理由,都不成立:
    - **截图不会串**:批次内一个 ASIN 只可能有一个邮编(上面那条 400 保证),
      所以 `(批次名, asin)` 本来就唯一定位一个 (ASIN,邮编),落盘的
      `<批次名>/<asin>.png` 天然不冲突,批次名不必带邮编。
    - **取数不受分批影响**:本项目取数只走 `/api/export/incremental` 按
      `scrape_params.zipcode` 分组,与批次怎么切无关。(按批次取数的
      `/api/results?batch_id=` 确实分不出邮编,但我们根本不用它。)

    blocked 里的 pair 跳过:在途的(等它落定)+ 重试窗口已耗尽的(再推白烧配额)。
    排序固定,保证分波可复现——排障时"上轮到底推了什么"说得清。
    """
    waves: list[list[tuple[str, str]]] = []
    seen: dict[str, int] = {}           # asin → 已占用几个波次
    for asin, zip5 in sorted(set(pairs)):
        if not asin or not zip5 or (asin, zip5) in blocked:
            continue
        i = seen.get(asin, 0)
        seen[asin] = i + 1
        while len(waves) <= i:
            waves.append([])
        waves[i].append((asin, zip5))
    return waves


# ══════════════════════════════════════════════════════════════════════════════
#  采集接入缝(采集契约字段变了,只改这一个函数)
# ══════════════════════════════════════════════════════════════════════════════

def from_snapshot(row: dict | None) -> dict | None:
    """输入:catalog.latest_snapshot 一行 → 输出:审核用统一形状(无行返回 None)。

    这是全项目**唯一**把采集快照翻译成审核输入的地方。采集契约字段位置变了
    只改本函数,judge/限价/采购方匹配全部不动。

    字段来历(契约 v1 + §5.1,逐个有据,别按名字猜):
    - 邮编:`scrape_params.zipcode`(**不是 zip/zip_code**)。
    - **`zip_verify == "mismatch"` 的记录直接判废**(契约:不应进入该邮编的
      价格序列)——采集侧切邮编失败时拿到的是默认地区价格,拿它审单等于
      拿错地区的价格算限价。返回 zip="" 使其匹配不上任何订单行。
    - 配送方式:`raw->>'is_fba'`(采集侧 parser 读 buybox 的 Ships from 行;
      契约 fast 段未列为一等字段,但 raw 未裁剪它)。与 maintenance/list_new
      取的是同一处,三处口径必须一致。
    - 卖家:`buybox.buybox_seller`(product_ingest 只把 buybox_* 三个键塞进
      buybox jsonb)。
    - 运费:`fast.shipping`(采集侧 2026-08-10 追加,落到 snapshots.shipping 列)。
      **None 不是 0**:FREE→0.0 是"确认免运费",N/A→None 是"这次没采到"。
    - 截图:**不在快照里**。取图走 api/scraper.fetch_screenshot(批次名, ASIN),
      抓手是 ops.audit_scrape 台账里记的 batch_name。
    """
    if not row:
        return None
    bb = row.get("buybox") or {}
    if not isinstance(bb, dict):
        bb = {}
    params = row.get("scrape_params") or {}
    if not isinstance(params, dict):
        params = {}
    raw = row.get("raw") or {}
    if not isinstance(raw, dict):
        raw = {}

    zip5 = norm_zip(params.get("zipcode"))
    if str(params.get("zip_verify") or "").strip().lower() == "mismatch":
        logger.warning("ASIN %s 快照 zip_verify=mismatch(请求 %s / 实到 %s),"
                       "判废不参与审核", row.get("asin"), zip5,
                       params.get("zip_observed"))
        zip5 = ""

    return {
        "asin": row.get("asin"),
        "zip": zip5,
        "amz_price": row.get("price"),
        "shipping": row.get("shipping"),
        "shipping_raw": row.get("shipping_raw"),
        "stock_qty": row.get("stock_count"),
        "ship_method": (str(raw.get("is_fba")).strip().upper()
                        if raw.get("is_fba") is not None else None),
        "ship_days": row.get("delivery_days"),
        "seller": bb.get("buybox_seller"),
        "amz_title": (row.get("title") or "").strip() or None,
        "outcome": row.get("outcome"),
        # 无货三档的两个信号(与维护链同源):stock_status 在 raw 顶层(契约未列
        # 为一等字段),stock_state 是 snapshots 的一等列。缺了它们,"页面采到了
        # 但没有可买的报价"会被后面的缺字段分支报成「采集缺字段」——
        # 那句话把人指向采集器,而采集器什么都没做错。
        "stock_status": raw.get("stock_status"),
        "stock_state": row.get("stock_state"),
        "scraped_at": row.get("scraped_at"),
    }
