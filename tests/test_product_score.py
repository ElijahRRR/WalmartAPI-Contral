"""产品分回归:缺失不当 0、权重摊回、销量只加分、硬闸与分数分开。

每条都对应一个会把好品判死、或把坏品放行的实例。
"""

from services import product_score as ps


# ── 缺失 ≠ 0(口径 #8 的核心)──────────────────────────────────────────

def test_missing_signal_is_not_zero_it_is_excluded():
    """没采到的信号**不计入**,权重摊回其余 —— 给 0 分等于惩罚"没采全的品"。

    两个品评分都是 4.8:A 的评论数采到了 1000(满分),B 没采到。
    B 不该因为"我们没采到它的评论数"而比 A 低分。
    """
    a = ps.score({"rating": 4.8, "reviews": 1000})
    b = ps.score({"rating": 4.8})
    assert a["score"] == b["score"] == 100.0
    assert b["missing"] == ["lead", "refund", "reviews", "sales"]
    # 摊回之后 rating 独占 100% 权重
    assert b["parts"]["rating"][2] == 1.0


def test_zero_reviews_is_a_real_value_not_missing():
    """0 条评论是真值(新品),记 0 分;"没采到"才不计入 —— 两者必须分开。"""
    real_zero = ps.score({"rating": 4.8, "reviews": 0})
    not_taken = ps.score({"rating": 4.8, "reviews": None})
    assert "reviews" in real_zero["parts"]          # 参与计分
    assert "reviews" in not_taken["missing"]        # 不参与
    assert real_zero["score"] < not_taken["score"]


def test_unparseable_rating_counts_as_not_taken():
    """评分是 text 型,解析失败按"没采到"处理,**禁止 or 0**。"""
    for bad in (None, "", "N/A", "abc", 0, 9.9, -1):
        assert ps.norm_rating(bad) is None


def test_all_signals_missing_scores_none_not_zero():
    """信号全缺 ⇒ 分数 None,不是 0。

    那是"这个品我们一无所知",该进「信息不足」桶让人看;
    判 0 分会把它和"确实很差的品"混成一堆,再也分不出来。
    """
    r = ps.score({})
    assert r["score"] is None and r["parts"] == {}
    assert r["penalty_why"] == "信号全缺"


# ── 销量只加分不减分(口径 #8)──────────────────────────────────────────

def test_no_sales_history_does_not_lower_the_score():
    """没卖过只是没信息。一个从没上过架的品与一个上架卖不动的品,
    在 order_lines 里长得一样 —— 分不出来就都当"没信息"。"""
    sold = ps.score({"rating": 4.0, "sales": 50})
    never = ps.score({"rating": 4.0, "sales": None})
    assert sold["score"] > never["score"]        # 卖过的加分
    assert never["score"] == ps.score({"rating": 4.0})["score"]   # 没卖过不扣分
    assert ps.norm_sales(None) is None
    assert ps.norm_sales(0) == 0.0               # 明确的 0 件仍是有效观测


def test_sales_uses_log_scale_so_one_hit_does_not_flatten_everyone():
    """对数标度:0→10 件的信息量远大于 990→1000 件。

    线性标度会让一个爆款把所有普通品压成 0 分。
    """
    assert ps.norm_sales(1) > 0
    assert ps.norm_sales(10) - ps.norm_sales(1) > ps.norm_sales(50) - ps.norm_sales(40)


# ── 退货率:历史期算不出 ⇒ 必须传 None ─────────────────────────────────

def test_refund_none_is_not_zero_refund():
    """把"没数据"当"零退货"会给历史期的品白送满分,而 API 期的品因为有
    真实退货被扣分 —— 两边不是同一个口径(§7.4e)。"""
    assert ps.norm_refund(None) is None          # 算不出
    assert ps.norm_refund(0.0) == 1.0            # 确认零退货 = 满分
    assert ps.norm_refund(0.15) == 0.5
    assert ps.norm_refund(0.30) == 0.0
    assert ps.norm_refund(0.99) == 0.0           # 不会变负


def test_lead_days_null_is_not_treated_as_slow():
    """配送天数没采到 ≠ 超时(§四:NULL 不当超时)。"""
    assert ps.norm_lead(None) is None
    assert ps.norm_lead(8) == 1.0                # 阈值内满分
    assert ps.norm_lead(3) == 1.0
    assert 0 < ps.norm_lead(20) < 1
    assert ps.norm_lead(30) == 0.0


# ── 黑历史罚分(硬拦截归黑名单,这里只减分)────────────────────────────

def test_risk_penalty_is_capped_and_explains_itself():
    pen, why = ps.risk_penalty({"delete_times": 5, "unexplained_missing": True,
                                "audit_reject_times": 3})
    assert pen == ps.RISK_PENALTY_MAX            # 封顶,不会把分数打到负
    assert "删过5次" in why and "不明原因消失过" in why
    assert ps.risk_penalty(None) == (0.0, "")
    assert ps.risk_penalty({}) == (0.0, "")


def test_penalty_never_pushes_score_below_zero():
    r = ps.score({"rating": 3.0}, risk={"unexplained_missing": True,
                                        "delete_times": 9})
    assert r["score"] == 0.0                     # 不是负数


# ── 硬闸与分数分开(§7.6)───────────────────────────────────────────────

def test_landed_price_gate_is_a_gate_not_a_deduction():
    """落地价 = 单价 + 运费,任一 NULL 就**定不了价**,不是"便宜一点"。
    口径与 list_new 同源。"""
    assert ps.gate({"price": None, "shipping": 0.0}, 5, 10)
    assert ps.gate({"price": 9.9, "shipping": None}, 5, 10)
    assert ps.gate({"price": 9.9, "shipping": 0.0, "stock": 10}, 5, 10) is None


def test_stock_gate_never_overrides_a_confirmed_zero():
    """有货但没采到数量 → 用保守量;**stock=0 是确实缺货,不许被覆盖**
    (amz_source 那条注释的同款纪律)。"""
    # NULL + in_stock → 用保守量 10,过闸
    assert ps.gate({"price": 1.0, "shipping": 0.0, "stock": None,
                    "stock_state": "in_stock"}, 5, 10) is None
    # 确认为 0 → 淘汰,保守量不该来救它
    why = ps.gate({"price": 1.0, "shipping": 0.0, "stock": 0,
                   "stock_state": "in_stock"}, 5, 10)
    assert why and "库存不足" in why
    # NULL 且没说有货 → 未知,不猜
    assert "库存未知" in ps.gate({"price": 1.0, "shipping": 0.0,
                                  "stock": None}, 5, 10)


def test_parts_expose_the_arithmetic_for_the_plan_sheet():
    """逐信号得分要能摊开:原值 / 归一值 / 实际权重 —— 分数说不清来源,
    人就没法推翻它(口径 #9 透明打分)。"""
    r = ps.score({"rating": 4.8, "sales": 50})
    assert set(r["parts"]) == {"rating", "sales"}
    raw, norm, w = r["parts"]["rating"]
    assert raw == 4.8 and norm == 1.0
    assert abs(sum(p[2] for p in r["parts"].values()) - 1.0) < 1e-9
    # 分数确实等于逐项加权和
    assert abs(r["score"] - 100 * sum(n * w for _, n, w in r["parts"].values())) < 1e-9


def test_weights_sum_to_one_and_all_have_labels():
    assert abs(sum(ps.WEIGHTS.values()) - 1.0) < 1e-9
    assert set(ps.WEIGHTS) == set(ps.LABELS)
