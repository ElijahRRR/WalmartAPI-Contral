"""产品分(0~100)与硬闸:分配引擎 A2 的产品侧。

设计见 docs/allocation_plan.md §7.6 / §7.4c。两件事分清:

**硬闸**(过不了就淘汰,与分数无关):落地价算不出、库存不足、黑名单、风控 PT。
**产品分**三段:

    score = 口碑基础分(0~60) + 销量加分(0~40) − 罚分(配送/退货/黑历史)

**销量加分 = 件数与销售额合成**(销售额权重更高:店铺目标是日 GMV,
100 件 × $5 与 10 件 × $100 对补缺口的价值完全不同);窗口默认**近一年**
(`SALES_WINDOW_DAYS`),与店铺经营水平那个 90 天窗口是两码事。

四条纪律,每条都对应一个**生产实测踩到过**的错:

1. **没有口碑数据 ⇒ 口碑段记 0 分**(所有者定稿 2026-08-15 晚)。
   旧实现做五信号加权平均 + 权重摊回,于是"只有配送时效有值"的品独占
   100% 权重、而 `lead<=8` 恒等满分 ⇒ **直接 100 分**;98.4% 的品配送都
   ≤8 天,等于给所有缺口碑数据的品白送满分。
   **为什么这里记 0 不违反「缺失 ≠ 0」**:落地价硬闸用的 price/shipping
   与评分**来自同一条快照** ⇒ 能走到打分这步的品一定有快照 ⇒ 快照里没有
   评分,意思是**这个商品确实没有评价**,那是真实观测不是数据缺口。
   没人评过的品本来就该排在有 800 条好评的品后面;
2. **销量只加分不减分**(口径 #8)。旧实现让销量参与加权(权重 .35),
   卖过 3 件的品反而比没订单史的低 17.6 分 —— 而销量覆盖率只有 **1.0%**,
   等于把**唯一有正面证据的那批品系统性压分**。现在它是纯加分项;
3. **缺失 ≠ 0,禁 `or 0`**。加分项没数据就 +0,罚分项没数据就 −0;
   口碑缺一项时权重摊回另一项。编数据两个方向都是错的;
4. **三段各自可查**(口径 #9 透明打分):`score()` 返回 base/bonus/penalty
   与逐信号 parts,方案表照印 —— 分数说不清来源,人就没法推翻它。

归一曲线全是 v1 启发式,**进配置、首批 dry-run 对着方案表砍**(§7.4g 同款)。
"""

import logging
import math

from services import amz_source

logger = logging.getLogger("services.product_score")

#: 低于这个分不参与分配(§7.6 初值,配置化)。
#: **它是兜底扫烂品的线,不是排序工具** —— 真正的闸是**容量**(14 家店合计
#: 剩余约 2.8 万个货位 vs 十几万候选),引擎按分数排序取到配额为止。
#:
#: ⚠ **这条线的严格程度会被口碑满分带着走**(2026-08-16 实测):
#:   · 口碑满分 75 的标尺下,40 只筛掉 7.5%(92.5% 的可判分品在线上);
#:   · 上调销量权重把口碑压到 60 之后,同样一个 40 变成筛掉 **56%**,
#:     中位数 38.4 直接掉到线下,牌堆缓冲从 40% 腰斩到 20%、切口分只剩 43.0。
#: 标尺变了这条线就得跟着挪 —— 所有者定稿 2026-08-16:**40 → 35**。
#: 改 `BASE_MAX` / `SALES_BONUS_MAX` 时**必须回头看这一行**。
CUTOFF = 35.0

# ══ 三段式打分(2026-08-15 生产实测后重构,按设计稿 §7.6 自己的措辞)══
#
# 原实现把五个信号做成一个加权平均,结果**违反了口径 #8「销量只加分不减分」**:
# 同样的品,卖过 3 件的比没有订单史的低 17.6 分(销量 0.35 的权重把它从
# "不计入"拉进了"计入且分低")。而销量覆盖率实测只有 **1.0%** ——
# 那意味着我们**唯一有正面证据的 3,897 个品反而被系统性压分**。
#
# 改成三段,每段语义单一,也与设计稿的原话对上:
#   基础分 = 口碑(评分 + 评论数),0~BASE_MAX
#   加分   = 卖过且卖得动(**纯加分,没数据就是 0,永远不会因此变低**)
#   罚分   = 配送慢 / 退货高 / 黑历史(§7.6 原话:"超 MAX_LEAD_DAYS 是**减分项**")

#: 基础分(口碑)的权重,和为 1。缺失的不计入,权重摊回另一项
WEIGHTS: dict[str, float] = {"rating": 0.6, "reviews": 0.4}
LABELS = {"rating": "评分", "reviews": "评论数"}

#: 口碑满分,留 SALES_BONUS_MAX 给销量加分 ⇒ 总分仍是 0~100。
#: ⚠ 2026-08-16 所有者:「销量和销售额的权重我感觉有点不够,应该上调一些」
#: ⇒ 75/25 改成 60/40。**代价要知道**:销量覆盖率实测只有 1.0%,所以这一改
#: 主要是把那 1% 往上顶,同时把 99% 没订单史的品的上限从 75 压到 60 ——
#: 有销售证据的品会大面积挤进顶层。这正是所有者要的,但顶层的品类构成会变。
BASE_MAX = 60.0
SALES_BONUS_MAX = 40.0      #: 卖过且卖得动的加分上限(件数 + 销售额)
#: 销量加分内部怎么分。**销售额权重更高**:店铺目标是日 GMV(§7.4a),
#: 100 件 × $5 与 10 件 × $100 对"补缺口"的价值完全不同,只数件数会把
#: 低单价走量品排到高客单品前面 —— 而缺口是按钱算的。
SALES_SPLIT: dict[str, float] = {"units": 0.375, "gross": 0.625}
LEAD_PENALTY_MAX = 15.0     #: 配送慢的罚分上限
REFUND_PENALTY_MAX = 20.0   #: 退货高的罚分上限
RISK_PENALTY_MAX = 15.0     #: 黑历史罚分上限(硬拦截归黑名单三表,这里只减分)
#: 「不明原因消失」要**连续发生这么多次**才扣分(所有者定稿 2026-08-15 晚)。
#: 判据来自 `catalog.product_risk`:消失过且我们从没提交过删/停 = 疑似平台
#: 强制下架。但 `mark_missing` 的判据是"本轮全量扫没扫到",**一次坏扫描
#: (整店凭证失效、代理挂掉)能把一批好品冤枉成消失** —— offset 截断那种
#: 已被 catalog_sync 的单查补漏兜住,这类兜不住。
#: 消失 1~2 次更像扫描抖动;≥3 次基本是真被平台反复下架。
UNEXPLAINED_MISSING_MIN = 3

#: 报告里按这个顺序印各段
SEGMENTS = ("口碑", "销量加分", "配送罚分", "退货罚分", "黑历史罚分")

#: 产品侧销量信号的默认窗口(天)。⚠ 与**店铺经营水平**的窗口是两码事:
#: 那边要"这家店现在什么水平"(近 90 天),这边要"这个品到底卖没卖过" ——
#: 窗口越短、覆盖率越低(90 天实测只有 1.0% 的品有订单史)。
#: 所有者定稿 2026-08-16:「看销售额,默认统计近一年的」。
SALES_WINDOW_DAYS = 365

# ── 归一曲线(全部 v1 启发式,配置化)────────────────────────────────────
_RATING_FLOOR, _RATING_CEIL = 3.0, 4.8     # 3.0 以下记 0,4.8 以上记满
_REVIEWS_FULL = 1000                        # 评论数对数标度的满分点
_SALES_FULL = 50                            # 窗口内销量(件)对数标度满分点
_GROSS_FULL = 2000                          # 窗口内销售额(毛额)对数标度满分点
#: ≤ 这个天数不扣分。**直接引用 amz_source 的唯一出处,不许在这里写字面量**
#: —— 它是所有者 2026-08-09 从 12 改成 8 的值,list_new 与 maintenance 都跟着它;
#: 这里抄一份 8 的话,哪天所有者再改,上架链跟着变而打分不变,而且不会报错。
_LEAD_FREE = amz_source.MAX_LEAD_DAYS
#: ⚠ **这个 30 是实现时定的占位值,不是所有者给的**(2026-08-15)。
#: 语义:配送慢到多少天算"扣满"。8~30 之间线性扣。要改就改这里,
#: 影响面只有产品分的罚分曲线,不碰上架/维护链。
_LEAD_DEAD = 30
_REFUND_DEAD = 0.30                         # 退货率 ≥ 30% 扣满


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


def _log_scale(v: float, full: float) -> float:
    """输入:非负值 + 满分点 → 输出:0~1 的对数标度。

    对数而非线性:0→10 条评论的信息量远大于 990→1000 条,线性标度会让
    爆款把所有普通品压成 0 分。
    """
    if v is None or v <= 0:
        return 0.0
    return _clamp01(math.log10(1 + v) / math.log10(1 + full))


def norm_rating(v) -> float | None:
    """输入:评分原值(可能是 text)→ 输出:0~1,**采不到返回 None**。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not 0 < f <= 5:                      # 越界 = 解析错了,当没采到
        return None
    return _clamp01((f - _RATING_FLOOR) / (_RATING_CEIL - _RATING_FLOOR))


def norm_reviews(v) -> float | None:
    """输入:评论数原值 → 输出:0~1,**采不到返回 None**。

    ⚠ 0 条评论与"没采到"必须分开:前者是真值(新品,记 0 分),
    后者不计入。所以只有解析失败才返回 None。
    """
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return _log_scale(n, _REVIEWS_FULL) if n >= 0 else None


def norm_sales(units, gross=None) -> float | None:
    """输入:窗口内销量(件)+ 销售额(毛额)→ 输出:0~1;**都没有返回 None**。

    纪律 2:没卖过只是没信息,不是缺点。一个从没上过架的品与一个上了架
    卖不动的品在 order_lines 里长得一样 —— 分不出来就都当"没信息"。

    ★ 两个信号按 `SALES_SPLIT` 合成,**销售额权重更高**(所有者 2026-08-16):
    店铺目标是日 GMV,100 件 × $5 与 10 件 × $100 对补缺口的价值完全不同。
    ⚠ 只有一项有值时,**权重摊回另一项**(与口碑缺项的处理相反,是有意的):
    口碑那边"没有评分"是真实观测(有快照就一定有这一栏),而这里"有件数
    没金额"只可能是历史行的金额列缺失 —— 那是数据缺口,不是"卖了 0 元"。
    """
    parts = {"units": (_log_scale(units, _SALES_FULL) if units is not None
                       else None),
             "gross": (_log_scale(gross, _GROSS_FULL) if gross is not None
                       else None)}
    live = {k: v for k, v in parts.items() if v is not None}
    if not live:
        return None
    w = sum(SALES_SPLIT[k] for k in live)
    return sum(SALES_SPLIT[k] * v for k, v in live.items()) / w


def lead_penalty(days) -> tuple[float, str]:
    """输入:配送天数 → 输出:(罚分, 原因)。**采不到 = 0 罚分**(NULL 不当超时)。

    设计稿 §7.6 原话:"超 MAX_LEAD_DAYS 是**减分项**不是闸"。做成正向信号是
    错的 —— 实测 98.4% 的品都 ≤8 天拿满分,那一项等于白送 18% 的有效权重、
    零区分度,还把权重从真正有信息的口碑那里抢走了。
    """
    if days is None:
        return 0.0, ""
    try:
        d = float(days)
    except (TypeError, ValueError):
        return 0.0, ""
    if d <= _LEAD_FREE:
        return 0.0, ""
    ratio = _clamp01((d - _LEAD_FREE) / (_LEAD_DEAD - _LEAD_FREE))
    return LEAD_PENALTY_MAX * ratio, f"配送{int(d)}天"


def refund_penalty(rate) -> tuple[float, str]:
    """输入:退货率(0~1)→ 输出:(罚分, 原因)。**算不出 = 0 罚分**。

    ⚠ 历史期算不出退货(order_history_import 只导销售六列),实测覆盖率仅
    0.9%。算不出就不罚 —— 把"没数据"当成"零退货"和当成"高退货"都是编数据;
    不罚至少不冤枉人,而真有退货数据的那 0.9% 该罚照罚。
    """
    if rate is None:
        return 0.0, ""
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return 0.0, ""
    if r <= 0:
        return 0.0, ""
    return REFUND_PENALTY_MAX * _clamp01(r / _REFUND_DEAD), f"退货{r:.0%}"


def risk_penalty(risk: dict | None) -> tuple[float, str]:
    """输入:product_risk 行 → 输出:(罚分, 原因)。硬拦截不在这里(归黑名单)。

    ⚠ 「不明原因消失」要 `missing_times >= UNEXPLAINED_MISSING_MIN` 才算数 ——
    见那个常量的注释:一两次更像扫描抖动,把它当平台下架会冤枉好品。
    调用方**必须把 `missing_times` 一起查出来**,漏了这一列就等于永不扣分
    (0 >= 3 恒假),而且不会报错。
    """
    if not risk:
        return 0.0, ""
    why, pen = [], 0.0
    if risk.get("delete_times"):
        pen += 5.0 * min(2, int(risk["delete_times"]))
        why.append(f"删过{risk['delete_times']}次")
    # ⚠ 只在**反复**消失时才扣:一两次更可能是扫描抖动,不是平台下架
    n_missing = int(risk.get("missing_times") or 0)
    if risk.get("unexplained_missing") and n_missing >= UNEXPLAINED_MISSING_MIN:
        pen += 8.0
        why.append(f"不明原因消失过{n_missing}次")
    if risk.get("audit_reject_times"):
        pen += 4.0
        why.append(f"审核拒过{risk['audit_reject_times']}次")
    return min(RISK_PENALTY_MAX, pen), "、".join(why)


def score(signals: dict, risk: dict | None = None) -> dict:
    """输入:{rating/reviews/sales/lead/refund: 原始值} (+ product_risk 行)
    → 输出:{score, base, bonus, penalty, parts, missing, why}。

    **三段,每段语义单一**(2026-08-15 生产实测后重构,见文件头那段):

        score = 口碑基础分(0~60) + 销量加分(0~40) − 罚分(配送/退货/黑历史)

    · **口碑**:评分 + 评论数加权;**缺的那项按 0 分算**(所有者定稿 08-15 晚)
      —— 见下面那段:走到打分这步的品一定有快照,"没有评分"是真实观测;
    · **销量加分**:纯加分 —— 没订单史就是 +0,**永远不会因为"卖得少"而比
      "没数据"更低**(口径 #8「只加分不减分」)。旧的加权平均实现违反了这条:
      卖过 3 件的品比没数据的低 17.6 分,而销量覆盖率只有 1%,等于把我们
      **唯一有正面证据的那批品系统性压分**;
    · **罚分**:只有坏消息才扣分,没数据一律不扣 —— 那些是真数据缺口
      (退货只有 API 期算得出),编一个数出来两个方向都是错的。
    """
    norms = {"rating": norm_rating(signals.get("rating")),
             "reviews": norm_reviews(signals.get("reviews"))}
    missing = sorted(k for k, v in norms.items() if v is None)
    sales_n = norm_sales(signals.get("sales"), signals.get("gross"))
    if sales_n is None:
        missing.append("sales")

    # **口碑缺项按 0 分算,不摊回、不排除**(所有者定稿 2026-08-15 晚)。
    # 这跟全仓「缺失 ≠ 0」的纪律不冲突,因为这里的"缺"不是数据缺口:
    # 落地价硬闸用的 price/shipping **也来自同一条快照** ⇒ 能走到打分这步的品
    # 一定有快照 ⇒ 快照里没有评分,意思是**这个商品确实没有评价**,
    # 那是一个真实观测,不是我们没采到。没人买过没人评过的品,本来就该排在
    # 有 800 条好评的品后面。
    # ⚠ 这条推理依赖「打分前必先过 gate()」。哪天有人绕过硬闸直接调 score(),
    #   这个前提就断了 —— 所以 missing 照样如实返回,报告要印出来。
    parts = {k: (signals.get(k), v if v is not None else 0.0, WEIGHTS[k])
             for k, v in norms.items()}
    base = BASE_MAX * sum(WEIGHTS[k] * (v or 0.0) for k, v in norms.items())
    bonus = SALES_BONUS_MAX * (sales_n or 0.0)

    pens, whys = [], []
    for fn, arg in ((lead_penalty, signals.get("lead")),
                    (refund_penalty, signals.get("refund"))):
        pen, why = fn(arg)
        pens.append(pen)
        if why:
            whys.append(why)
    rpen, rwhy = risk_penalty(risk)
    pens.append(rpen)
    if rwhy:
        whys.append(rwhy)
    penalty = sum(pens)
    return {"score": max(0.0, min(100.0, base + bonus - penalty)),
            "base": base, "bonus": bonus, "penalty": penalty,
            "parts": parts, "missing": sorted(missing), "why": "、".join(whys)}


def gate(row: dict, min_stock: int, in_stock_qty: int) -> str | None:
    """输入:产品行 + 库存阈值 → 输出:淘汰原因;过闸返回 None。

    **硬闸与分数分开**:这里过不了的根本不参与打分(§7.6)。口径与 list_new
    同源 —— 落地价 = 单价 + 运费,任一 NULL 就定不了价,不是"便宜一点"。
    """
    if row.get("price") is None or row.get("shipping") is None:
        return "落地价算不出(price/shipping 有 NULL)"
    stock = row.get("stock")
    if stock is None and str(row.get("stock_state") or "") == "in_stock":
        stock = in_stock_qty            # 有货但没采到数量:保守量(绝不覆盖 0)
    if stock is None:
        return "库存未知"
    if stock < min_stock:
        return f"库存不足({stock} < {min_stock})"
    return None
