"""产品分回归:三段式(口碑 + 销量加分 − 罚分)。

每条都对应一个会把好品判死、或把没信息的品捧上天的实例。
两条最贵的教训(2026-08-15 生产实测抓到)各有一条钉死的测试:
  · 只有配送时效的品拿 100 分(权重摊回被推到极端);
  · 卖过 3 件的品比没订单史的低 17.6 分(违反「销量只加分不减分」)。
"""

import pytest

from services import product_score as ps


# ══ 教训一:没有口碑信息的品不许拿高分 ═══════════════════════════════════

def test_a_product_with_only_delivery_data_scores_zero_not_a_hundred():
    """**没评分、没评论 ⇒ 口碑段记 0 分**,不是 100 分。

    旧实现把五个信号做加权平均 + 权重摊回,于是"只有配送时效有值"的品
    独占 100% 权重、`lead<=8` 又恒等于满分 ⇒ **直接 100 分**。
    而 98.4% 的品配送都 ≤8 天 —— 等于给所有缺口碑数据的品白送满分,
    P75/P90 全被它们顶上去(生产实测 2026-08-15)。
    """
    r = ps.score({"lead": 3})
    assert r["score"] == 0.0 and r["base"] == 0.0
    assert r["missing"] == ["rating", "reviews", "sales"]   # 缺了什么照样如实报
    # 配送快也救不了:它现在只是罚分项,永远不加分
    assert ps.lead_penalty(3) == (0.0, "")
    assert ps.lead_penalty(None) == (0.0, "")


def test_missing_reputation_is_a_real_observation_not_a_data_gap():
    """**为什么这里记 0 不违反全仓的「缺失 ≠ 0」纪律** —— 前提要写死。

    落地价硬闸用的 price/shipping 与评分**来自同一条快照**:没快照的品在
    落地价那一关就被拦了 ⇒ 能走到打分这步的品一定有快照 ⇒ 快照里没有评分,
    意思是**这个商品确实没有评价**,那是真实观测,不是我们没采到。
    """
    from services import amz_source
    # 没快照 ⇒ price 为 None ⇒ 硬闸拦下,根本走不到 score()
    assert ps.gate({"price": None, "shipping": None, "stock": 99},
                   amz_source.MIN_INVENTORY, amz_source.IN_STOCK_QTY)
    # 有快照、有价、没评价 ⇒ 过闸,口碑记 0
    assert ps.gate({"price": 9.9, "shipping": 0.0, "stock": 99, "lead": 3},
                   amz_source.MIN_INVENTORY, amz_source.IN_STOCK_QTY) is None
    assert ps.score({})["base"] == 0.0


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

def test_missing_review_count_costs_exactly_its_own_weight():
    """缺评论数 = 丢掉评论数那 40% 的分,**不多不少**,也不摊给评分。

    "没评论"是真实观测(见上一条),所以按 0 分算;但它不该连累评分那一项。
    """
    both = ps.score({"rating": 4.8, "reviews": 1000})
    only = ps.score({"rating": 4.8})
    assert both["score"] == ps.BASE_MAX                      # 两项满分 = 75
    assert only["score"] == ps.BASE_MAX * ps.WEIGHTS["rating"]   # 只剩评分那 60%
    assert "reviews" in only["missing"]                      # 缺了什么照样如实报


def test_zero_reviews_and_absent_reviews_score_the_same_but_report_differently():
    """两者**分数相同**(都记 0 分,所有者定稿:相关分数扣除即可),
    但 `missing` 仍如实区分 —— 报告里要看得出哪些是真没评论、哪些是字段缺。

    分数上不区分是业务判断(没评论就是没口碑);可追溯性上区分是纪律 ——
    哪天覆盖率掉下去,得能从 missing 里看出是采集出了问题。
    """
    real_zero = ps.score({"rating": 4.8, "reviews": 0})
    absent = ps.score({"rating": 4.8})
    assert real_zero["score"] == absent["score"]      # 分数一样
    assert "reviews" not in real_zero["missing"]      # 但来源分得清
    assert "reviews" in absent["missing"]


def test_unparseable_rating_counts_as_not_taken():
    """评分是 text 型,解析失败按"没采到",**禁止 or 0**。"""
    for bad in (None, "", "N/A", "abc", 0, 9.9, -1):
        assert ps.norm_rating(bad) is None


def test_no_signals_at_all_scores_zero_and_falls_below_the_cutoff():
    """什么都没有 ⇒ 0 分,被淘汰线扫掉 —— 不需要单独一个「信息不足」桶。

    所有者定稿 2026-08-15 晚:「没有评分和评论的,相关分数扣除即可」。
    """
    r = ps.score({})
    assert r["score"] == 0.0 and r["base"] == 0.0
    assert r["score"] < ps.CUTOFF


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
                                "missing_times": 4, "audit_reject_times": 3})
    assert pen == ps.RISK_PENALTY_MAX
    assert "删过5次" in why and "不明原因消失过4次" in why
    assert ps.risk_penalty(None) == (0.0, "") and ps.risk_penalty({}) == (0.0, "")


def test_unexplained_missing_needs_three_strikes(monkeypatch):
    """消失 1~2 次**不扣分**(所有者定稿 2026-08-15 晚)。

    `mark_missing` 的判据是"本轮全量扫没扫到" —— 整店凭证失效或代理挂掉时,
    一次坏扫描能把一批好品冤枉成"平台强制下架"。offset 截断那种已被
    catalog_sync 的单查补漏兜住,这类兜不住,所以要求**反复发生**。
    """
    def _pen(n):
        return ps.risk_penalty({"unexplained_missing": n > 0,
                                "missing_times": n})[0]
    assert _pen(1) == 0.0 and _pen(2) == 0.0        # 抖动,不扣
    assert _pen(ps.UNEXPLAINED_MISSING_MIN) == 8.0  # 够次数才扣
    assert _pen(9) == 8.0                           # 扣的额度不随次数涨


def test_missing_times_must_be_selected_by_callers():
    """调用方漏查 `missing_times` ⇒ 该项**永不扣分且不报错**(0 >= 3 恒假)。

    所以查询里必须有这一列 —— 这种"少一列就静默失效"的依赖,
    只能靠测试钉住。
    """
    from workflows import alloc_products as wf
    assert "missing_times" in wf._SQL_RISK
    # 只有 unexplained_missing 没有 missing_times 时,如实不扣(而不是猜)
    assert ps.risk_penalty({"unexplained_missing": True}) == (0.0, "")


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
    assert ps.gate({"price": 9.9, "shipping": 0.0, "stock": 10, "lead": 3},
                   5, 10) is None


def test_stock_gate_never_overrides_a_confirmed_zero():
    """有货但没采到数量 → 用保守量;**stock=0 是确实缺货,不许被覆盖**。"""
    assert ps.gate({"price": 1.0, "shipping": 0.0, "stock": None,
                    "stock_state": "in_stock", "lead": 3}, 5, 10) is None
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


def test_lead_threshold_follows_amz_source_not_a_local_literal():
    """配送阈值**只能有一个出处**(CLAUDE.md 铁律)。

    8 天是所有者 2026-08-09 从 12 改的值,`list_new` 与 `maintenance` 都跟着
    `amz_source.MAX_LEAD_DAYS`。在打分这里抄一份字面量 8 的话,哪天所有者
    再改,上架链跟着变而打分不变 —— **而且不会报错**。
    """
    from services import amz_source
    assert ps._LEAD_FREE is amz_source.MAX_LEAD_DAYS
    src = open(ps.__file__, encoding="utf-8").read()
    assert "_LEAD_FREE = amz_source.MAX_LEAD_DAYS" in src


def test_lead_penalty_curve_between_the_two_thresholds():
    """8 天以内不扣,30 天扣满,中间线性 —— 30 是实现占位值,不是所有者给的。"""
    assert ps.lead_penalty(ps._LEAD_FREE)[0] == 0.0
    assert ps.lead_penalty(ps._LEAD_DEAD)[0] == ps.LEAD_PENALTY_MAX
    assert ps.lead_penalty(ps._LEAD_DEAD * 2)[0] == ps.LEAD_PENALTY_MAX   # 封顶
    mid = (ps._LEAD_FREE + ps._LEAD_DEAD) / 2
    assert abs(ps.lead_penalty(mid)[0] - ps.LEAD_PENALTY_MAX / 2) < 1e-9


# ── 销量权重上调 + 销售额进分(所有者 2026-08-16)──────────────────────

def test_sales_bonus_combines_units_and_revenue_with_revenue_weighted_higher():
    """所有者:「销量和销售额的权重我感觉有点不够,应该上调一些」。

    销售额权重更高是因为**店铺目标是日 GMV**(§7.4a):100 件 × $5 与
    10 件 × $100 对补缺口的价值完全不同,只数件数会把低单价走量品排到
    高客单品前面 —— 而缺口是按钱算的。
    """
    assert ps.BASE_MAX + ps.SALES_BONUS_MAX == 100.0
    assert ps.SALES_BONUS_MAX > 25.0                   # 上调过
    assert ps.SALES_SPLIT["gross"] > ps.SALES_SPLIT["units"]
    # 同样件数、销售额高的分更高
    lo = ps.score({"sales": 10, "gross": 100.0, "rating": "4.5", "reviews": "50"})
    hi = ps.score({"sales": 10, "gross": 2000.0, "rating": "4.5", "reviews": "50"})
    assert hi["bonus"] > lo["bonus"]


def test_sales_signal_missing_entirely_still_adds_nothing_and_subtracts_nothing():
    """口径 #8 不变:没订单史 = +0,**永远不会因此比别人低**。"""
    none = ps.score({"rating": "4.5", "reviews": "50"})
    some = ps.score({"sales": 3, "gross": 30.0, "rating": "4.5", "reviews": "50"})
    assert none["bonus"] == 0.0
    assert some["score"] >= none["score"]


def test_one_sales_signal_missing_falls_back_to_the_other():
    """⚠ 有件数没金额只可能是**历史行的金额列缺失**,是数据缺口不是"卖了 0 元"。

    与口碑缺项按 0 算相反 —— 那边有快照就一定有那一栏,这边不是。
    """
    both = ps.norm_sales(10, 200.0)
    only_units = ps.norm_sales(10, None)
    only_gross = ps.norm_sales(None, 200.0)
    assert 0 < only_units <= 1 and 0 < only_gross <= 1
    assert ps.norm_sales(None, None) is None
    # 权重摊回:只有件数时就是件数那一项的归一值本身
    assert only_units == pytest.approx(ps._log_scale(10, ps._SALES_FULL))


def test_product_sales_window_defaults_to_a_year():
    """产品侧窗口 ≠ 店铺侧窗口。

    合成一个的话:90 天则产品侧覆盖率只有 1.0%(信号形同虚设);365 天则
    店铺的缺口与货位值被一年前的经营状况稀释。
    """
    assert ps.SALES_WINDOW_DAYS == 365


def test_cutoff_is_calibrated_against_the_reputation_ceiling():
    """⚠ 淘汰线的严格程度**被口碑满分带着走**,改权重时必须回头看它。

    2026-08-16 实测:口碑满分 75 时,40 只筛掉 7.5%;把口碑压到 60 之后,
    同一个 40 变成筛掉 56%。**这条不钉死某个数**,它钉的是这条线在标尺上的
    位置 —— 让"改了满分却忘了挪线"当场变红。

    判据用两个参照品,两个方向各堵一头(只堵一头会漏另一头):
      · **差品必须在线下**。都在线上说明这条线什么也没扫到 —— 它就白设了
        (口碑满分 75 那次实测:40 只筛掉 7.5%,92.5% 的可判分品都在线上);
      · **好品必须在线上**。好品掉到线下说明这条线已经不是"扫烂品",是在砍
        正经货 —— `BASE_MAX` 被调小而线没跟着下来时就是这个样子。

    ⚠ 中间那档(4.2 分 / 50 评 = 37.7)**故意不判**:所有者 2026-08-16 明知
    40 高过它、还是把线从 35 抬回 40(「反正现在产品用不完」)。货多得用不完
    时只在好的一半里挑是合理的经营选择,不该被回归拦下。
    """
    bad = ps.score({"rating": "3.5", "reviews": "5"})["score"]       # 实测 16.2
    good = ps.score({"rating": "4.7", "reviews": "500"})["score"]    # 实测 55.6
    assert bad < ps.CUTOFF, (
        f"「差口碑」({bad:.1f})都在淘汰线 {ps.CUTOFF} 之上 —— 这条线什么也没"
        f"扫到,多半是 BASE_MAX 调大了而线没跟着抬")
    assert ps.CUTOFF < good, (
        f"淘汰线 {ps.CUTOFF} 已经砍到「好口碑」({good:.1f})头上 —— 它该扫烂品,"
        f"不是砍正经货;多半是 BASE_MAX 调小了而线没跟着降")


def test_the_hard_gate_judges_the_product_alone_never_a_store_condition():
    """⚠ `gate()` 只判产品自身:定不了价 / 没库存。**逐店的条件不许写进来。**

    2026-08-21 一度把「配送 >7 天」写在这里,当天撤回:货期是限额表
    「配送时长限制」一店一填的,写死一个全局数两头错 —— 有店填 10 就白白丢掉
    它能卖的货,全店都填 5 又让 6~7 天的货空占池子。落点是
    `alloc_plan._pool_reach`(取参与分配的店的并集)。
    """
    ok = {"price": 9.9, "shipping": 0.0, "stock": 99}
    for lead in (0, 3, 7, 12, 99, None):
        assert ps.gate({**ok, "lead": lead}, 5, 10) is None
    # 免罚线仍与上架链共用同一常量(打分归打分,闸归闸)
    from services import amz_source
    assert ps._LEAD_FREE == amz_source.MAX_LEAD_DAYS
