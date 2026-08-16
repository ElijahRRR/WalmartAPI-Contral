"""产品分(0~100)与硬闸:分配引擎 A2 的产品侧。

设计见 docs/allocation_plan.md §7.6 / §7.4c。两件事分清:

**硬闸**(过不了就淘汰,与分数无关):落地价算不出、库存不足、黑名单、风控 PT。
**产品分**(0~100,低于淘汰线不分):评分 / 评论 / 历史销量 / 配送时效 / 退货率,
再减黑历史罚分。

三条纪律,每条都对应一个会把好品判死的实例:

1. **缺失 ≠ 0,禁止 `or 0`**(口径 #8)。某个信号没采到就**不计入**,
   权重按比例**摊回其余信号** —— 给它 0 分等于惩罚"新品/没采全的品",
   而"没采到"是我们自己的数据缺口,不是商品的缺点;
2. **销量只加分不减分**:卖过且卖得动是最强信号;**没卖过只是没信息**。
   一个从没上过架的品与一个上了架卖不动的品,在 `order_lines` 里长得一样,
   分不出来 ⇒ 一律当"没信息";
3. **逐信号得分要能摊开给人看**(口径 #9 透明打分):`score()` 返回每一项的
   原始值、归一值与权重,方案表照着印 —— 分数说不清来源,人就没法推翻它。

归一曲线全是 v1 启发式,**进配置、首批 dry-run 对着方案表砍**(§7.4g 同款)。
"""

import logging
import math

logger = logging.getLogger("services.product_score")

#: 低于这个分不参与分配(§7.6 初值,配置化)
CUTOFF = 40.0

#: 信号权重(和为 1)。缺失的信号不计入,权重摊回其余 —— 见模块 docstring 纪律 1
WEIGHTS: dict[str, float] = {
    "sales": 0.35,      # 卖过且卖得动:最强选品信号
    "rating": 0.25,
    "reviews": 0.20,
    "lead": 0.10,       # 配送时效(减分项,不是闸)
    "refund": 0.10,     # 退货率(只有 API 期算得出)
}

#: 中文名(报告与方案表照印)
LABELS = {"sales": "历史销量", "rating": "评分", "reviews": "评论数",
          "lead": "配送时效", "refund": "退货率"}

#: 黑历史罚分上限(product_risk 计数列;硬拦截归黑名单三表,这里只减分)
RISK_PENALTY_MAX = 15.0

# ── 归一曲线(全部 v1 启发式,配置化)────────────────────────────────────
_RATING_FLOOR, _RATING_CEIL = 3.0, 4.8     # 3.0 以下记 0,4.8 以上记满
_REVIEWS_FULL = 1000                        # 评论数对数标度的满分点
_SALES_FULL = 50                            # 窗口内销量(件)对数标度满分点
_LEAD_FREE = 8                              # ≤ 8 天不扣分(amz_source.MAX_LEAD_DAYS)
_LEAD_DEAD = 30                             # ≥ 30 天记 0
_REFUND_DEAD = 0.30                         # 退货率 ≥ 30% 记 0


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


def norm_sales(units) -> float | None:
    """输入:窗口内销量(件)→ 输出:0~1;**没有订单史返回 None(不计入)**。

    纪律 2:没卖过只是没信息,不是缺点。一个从没上过架的品与一个上了架
    卖不动的品在 order_lines 里长得一样 —— 分不出来就都当"没信息"。
    """
    if units is None:
        return None
    return _log_scale(units, _SALES_FULL)


def norm_lead(days) -> float | None:
    """输入:配送天数 → 输出:0~1;**采不到返回 None**(NULL 不当超时)。"""
    if days is None:
        return None
    try:
        d = float(days)
    except (TypeError, ValueError):
        return None
    if d <= _LEAD_FREE:
        return 1.0
    if d >= _LEAD_DEAD:
        return 0.0
    return _clamp01((_LEAD_DEAD - d) / (_LEAD_DEAD - _LEAD_FREE))


def norm_refund(rate) -> float | None:
    """输入:退货率(0~1)→ 输出:0~1;**算不出返回 None**。

    ⚠ 历史期算不出退货(order_history_import 只导销售六列)——
    那时必须传 None,**不能传 0**:把"没数据"当成"零退货"会给历史期的品
    白送满分,而 API 期的品因为有真实退货被扣分,两边不是同一个口径。
    """
    if rate is None:
        return None
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return None
    return _clamp01(1 - r / _REFUND_DEAD)


def risk_penalty(risk: dict | None) -> tuple[float, str]:
    """输入:product_risk 行 → 输出:(罚分, 原因)。硬拦截不在这里(归黑名单)。"""
    if not risk:
        return 0.0, ""
    why, pen = [], 0.0
    if risk.get("delete_times"):
        pen += 5.0 * min(2, int(risk["delete_times"]))
        why.append(f"删过{risk['delete_times']}次")
    if risk.get("unexplained_missing"):
        pen += 8.0
        why.append("不明原因消失过")
    if risk.get("audit_reject_times"):
        pen += 4.0
        why.append(f"审核拒过{risk['audit_reject_times']}次")
    return min(RISK_PENALTY_MAX, pen), "、".join(why)


def score(signals: dict, risk: dict | None = None) -> dict:
    """输入:{sales/rating/reviews/lead/refund: 原始值} (+ product_risk 行)
    → 输出:{score, parts, missing, penalty, penalty_why}。

    `parts` 逐信号给出 (原值, 归一值, 实际权重),方案表照印 —— 分数说不清
    来源,人就没法推翻它(口径 #9 透明打分)。

    **权重摊回**:只在有值的信号间按原权重比例重新归一。全都没有 ⇒ 分数
    `None`(不是 0):那是"这个品我们一无所知",该进"信息不足"桶让人看,
    而不是判它 0 分淘汰掉。
    """
    norms = {"sales": norm_sales(signals.get("sales")),
             "rating": norm_rating(signals.get("rating")),
             "reviews": norm_reviews(signals.get("reviews")),
             "lead": norm_lead(signals.get("lead")),
             "refund": norm_refund(signals.get("refund"))}
    present = {k: v for k, v in norms.items() if v is not None}
    missing = sorted(k for k, v in norms.items() if v is None)
    if not present:
        return {"score": None, "parts": {}, "missing": missing,
                "penalty": 0.0, "penalty_why": "信号全缺"}
    total_w = sum(WEIGHTS[k] for k in present)
    parts = {k: (signals.get(k), v, WEIGHTS[k] / total_w)
             for k, v in present.items()}
    base = 100.0 * sum(v * WEIGHTS[k] for k, v in present.items()) / total_w
    pen, why = risk_penalty(risk)
    return {"score": max(0.0, base - pen), "parts": parts, "missing": missing,
            "penalty": pen, "penalty_why": why}


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
