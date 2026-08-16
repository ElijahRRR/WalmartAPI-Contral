"""产品分回归:三段式(口碑 + 销量加分 − 罚分)。

每条都对应一个会把好品判死、或把没信息的品捧上天的实例。
两条最贵的教训(2026-08-15 生产实测抓到)各有一条钉死的测试:
  · 只有配送时效的品拿 100 分(权重摊回被推到极端);
  · 卖过 3 件的品比没订单史的低 17.6 分(违反「销量只加分不减分」)。
"""

from services import product_score as ps


# ══ 教训一:没有口碑信息的品不许拿高分 ═══════════════════════════════════

def test_a_product_with_only_delivery_data_is_not_a_perfect_product():
    """**没评分、没评论 ⇒ 不判分**,不是 100 分。

    旧实现把五个信号做加权平均 + 权重摊回,于是"只有配送时效有值"的品
    独占 100% 权重、`lead<=8` 又恒等于满分 ⇒ **直接 100 分**。
    而 98.4% 的品配送都 ≤8 天 —— 等于给所有缺口碑数据的品白送满分,
    P75/P90 全被它们顶上去(生产实测 2026-08-15)。
    """
    r = ps.score({"lead": 3})
    assert r["score"] is None and r["why"] == "口碑信号全缺"
    # 配送快也救不了:它现在只是罚分项,永远不加分
    assert ps.lead_penalty(3) == (0.0, "")
    assert ps.lead_penalty(None) == (0.0, "")


def test_delivery_is_a_penalty_only_never_a_bonus():
    """设计稿 §7.6 原话:"超 MAX_LEAD_DAYS 是**减分项**不是闸"。

    做成正向信号是错的 —— 98.4% 的品都拿满分,零区分度,
    还把权重从真正有信息的口碑那里抢走。
    """
    fast = ps.score({"rating": 4.5, "reviews": 100, "lead": 3})
    none = ps.score({"rating": 4.5, "reviews": 100})
    assert fast["score"] == none["score"]        # 快不加分
    slow = ps.score({"rating": 4.5, "reviews": 100, "lead": 25})
    assert slow["score"] < none["score"]         # 慢才扣分
    assert "配送25天" in slow["why"]


# ══ 教训二:销量只加分不减分(口径 #8)═══════════════════════════════════

def test_having_sales_never_lowers_the_score():
    """卖过 3 件的品**不许**比没有订单史的品低分。

    旧实现让销量参与加权平均(权重 .35),于是卖得少反而把分数拉下来:
    实测 80.4 → 62.8。而销量覆盖率只有 1.0%,等于把我们**唯一有正面证据
    的那 3,897 个品系统性压分**。
    """
    base = {"rating": 4.5, "reviews": 100, "lead": 3}
    none = ps.score(base)
    few = ps.score({**base, "sales": 3})
    many = ps.score({**base, "sales": 50})
    assert none["score"] < few["score"] < many["score"]
    assert none["bonus"] == 0.0                  # 没数据 = 不加分,不是扣分


def test_sales_bonus_is_bounded_and_sales_uses_log_scale():
    assert ps.score({"rating": 4.8, "reviews": 1000,
                     "sales": 10 ** 9})["bonus"] == ps.SALES_BONUS_MAX
    assert ps.norm_sales(None) is None           # 没订单史
    assert ps.norm_sales(0) == 0.0               # 明确的 0 件仍是有效观测
    # 对数标度:0→10 件的信息量远大于 40→50 件
    assert ps.norm_sales(10) - ps.norm_sales(1) > ps.norm_sales(50) - ps.norm_sales(40)


# ══ 缺失 ≠ 0(口径 #8)══════════════════════════════════════════════════

def test_missing_review_count_does_not_drag_the_score_down():
    """口碑两项缺一项时,权重摊回另一项 —— 给 0 分等于惩罚"我们没采全的品"。"""
    both = ps.score({"rating": 4.8, "reviews": 1000})
    only = ps.score({"rating": 4.8})
    assert both["score"] == only["score"] == ps.BASE_MAX
    assert only["parts"]["rating"][2] == 1.0     # 摊回后独占权重
    assert "reviews" in only["missing"]


def test_zero_reviews_is_a_real_value_not_missing():
    """0 条评论是真值(新品),记 0 分;"没采到"才不计入 —— 两者必须分开。"""
    real_zero = ps.score({"rating": 4.8, "reviews": 0})
    not_taken = ps.score({"rating": 4.8})
    assert "reviews" in real_zero["parts"]
    assert "reviews" in not_taken["missing"]
    assert real_zero["score"] < not_taken["score"]


def test_unparseable_rating_counts_as_not_taken():
    """评分是 text 型,解析失败按"没采到",**禁止 or 0**。"""
    for bad in (None, "", "N/A", "abc", 0, 9.9, -1):
        assert ps.norm_rating(bad) is None


def test_no_signals_at_all_scores_none_not_zero():
    """一无所知 ⇒ None,进「信息不足」桶让人看。

    判 0 分会把它和"确实很差的品"混成一堆,再也分不出来。
    """
    r = ps.score({})
    assert r["score"] is None and r["base"] is None and r["parts"] == {}


# ══ 罚分:没数据一律不扣 ════════════════════════════════════════════════

def test_refund_penalty_only_when_there_is_data():
    """历史期算不出退货(实测覆盖率 0.9%)。算不出就不罚 ——
    把"没数据"当"零退货"或"高退货"都是编数据;不罚至少不冤枉人。"""
    assert ps.refund_penalty(None) == (0.0, "")
    assert ps.refund_penalty(0.0) == (0.0, "")
    pen, why = ps.refund_penalty(0.15)
    assert pen == ps.REFUND_PENALTY_MAX / 2 and "退货15%" in why
    assert ps.refund_penalty(0.99)[0] == ps.REFUND_PENALTY_MAX   # 封顶


def test_risk_penalty_is_capped_and_explains_itself():
    pen, why = ps.risk_penalty({"delete_times": 5, "unexplained_missing": True,
                                "audit_reject_times": 3})
    assert pen == ps.RISK_PENALTY_MAX
    assert "删过5次" in why and "不明原因消失过" in why
    assert ps.risk_penalty(None) == (0.0, "") and ps.risk_penalty({}) == (0.0, "")


def test_penalty_never_pushes_score_below_zero():
    r = ps.score({"rating": 3.0}, risk={"unexplained_missing": True,
                                        "delete_times": 9})
    assert r["score"] == 0.0                     # 不是负数


def test_score_stays_within_0_100():
    top = ps.score({"rating": 5.0, "reviews": 10 ** 6, "sales": 10 ** 6})
    assert top["score"] == 100.0
    assert ps.BASE_MAX + ps.SALES_BONUS_MAX == 100.0


# ══ 硬闸与分数分开(§7.6)═══════════════════════════════════════════════

def test_landed_price_gate_is_a_gate_not_a_deduction():
    """落地价 = 单价 + 运费,任一 NULL 就**定不了价**,不是"便宜一点"。"""
    assert ps.gate({"price": None, "shipping": 0.0}, 5, 10)
    assert ps.gate({"price": 9.9, "shipping": None}, 5, 10)
    assert ps.gate({"price": 9.9, "shipping": 0.0, "stock": 10}, 5, 10) is None


def test_stock_gate_never_overrides_a_confirmed_zero():
    """有货但没采到数量 → 用保守量;**stock=0 是确实缺货,不许被覆盖**。"""
    assert ps.gate({"price": 1.0, "shipping": 0.0, "stock": None,
                    "stock_state": "in_stock"}, 5, 10) is None
    why = ps.gate({"price": 1.0, "shipping": 0.0, "stock": 0,
                   "stock_state": "in_stock"}, 5, 10)
    assert why and "库存不足" in why
    assert "库存未知" in ps.gate({"price": 1.0, "shipping": 0.0,
                                  "stock": None}, 5, 10)


# ══ 透明可查(口径 #9)══════════════════════════════════════════════════

def test_score_decomposes_into_three_segments():
    """分数说不清来源,人就没法推翻它 —— 三段各自可查,加起来等于总分。"""
    r = ps.score({"rating": 4.8, "reviews": 1000, "sales": 50, "lead": 25})
    assert abs(r["score"] - (r["base"] + r["bonus"] - r["penalty"])) < 1e-9
    raw, norm, w = r["parts"]["rating"]
    assert raw == 4.8 and norm == 1.0
    assert abs(sum(p[2] for p in r["parts"].values()) - 1.0) < 1e-9


def test_weights_sum_to_one_and_all_have_labels():
    assert abs(sum(ps.WEIGHTS.values()) - 1.0) < 1e-9
    assert set(ps.WEIGHTS) == set(ps.LABELS) == {"rating", "reviews"}
