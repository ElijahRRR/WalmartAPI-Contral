"""L2 硬规则/软证据 + 理由映射向量(spec_vectors B6/B7;旧仓零测试)。

ctx 用 SimpleNamespace 鸭子拼装;mega/nrtl/nice 走真实 refdata yaml
(loader 即被测面之一);R5 恒关(ctx.uspto=None)。
"""

from types import SimpleNamespace

import pytest

from services import audit_l2, audit_reason
from services.audit_models import AuditOutcome, L1Info, ProductInfo, RuleHit


def _ctx(pt_meta=None, pt_spec=None, ac=None):
    mapping, default = audit_l2.load_nice_mapping()
    small, whole = audit_l2.load_nrtl_keywords()
    return SimpleNamespace(
        pt_meta=pt_meta or {}, pt_spec=pt_spec or {},
        ac_automaton=ac, mega=audit_l2.load_mega_categories(),
        nrtl_small=small, nrtl_whole=whole,
        nice_mapping=mapping, nice_default=default, uspto=None)


def _p(**kw):
    kw.setdefault("asin", "B000TEST00")
    return ProductInfo(**kw)


def _l1(pt=None, cat=None, excluded=None):
    return L1Info(walmart_product_type=pt, walmart_category=cat,
                  excluded_category_reason=excluded)


def _codes(res):
    return [h.rule_code for h in res.hits]


# ── R0 中国卖家八大类硬禁 ────────────────────────────────────────────────────

def test_r0_electronics_hit():
    res = audit_l2.evaluate(_p(), _l1(pt="Widgets", cat="Electronics"), _ctx())
    h = [x for x in res.hits if x.rule_code == "zh_seller_mega_cat_forbidden"][0]
    assert h.penalty == -100
    assert h.detail["walmart_policy"] == "Electronics & RF"
    assert h.detail["source"] == "用户指定: 中国搬运卖家全类目硬禁"
    assert res.verdict == "reject"


def test_r0_case_sensitive_and_guard():
    assert "zh_seller_mega_cat_forbidden" not in _codes(
        audit_l2.evaluate(_p(), _l1(pt="Widgets", cat="electronics"), _ctx()))
    assert "zh_seller_mega_cat_forbidden" not in _codes(
        audit_l2.evaluate(_p(), _l1(pt="W", cat="Electronics",
                                    excluded="3C禁售"), _ctx()))


@pytest.mark.parametrize("cat,policy", [
    ("Vehicles", "Auto & Motor Vehicles"), ("Automotive", "Auto & Motor Vehicles"),
    ("Fashion", "Textiles & Apparel"), ("Food & Beverage", "Food Products"),
    ("Health & Personal Care", "Drugs & Paraphernalia"),
    ("Beauty", "Cosmetic Products"), ("Baby", "Baby Products"),
])
def test_r0_all_keys(cat, policy):
    res = audit_l2.evaluate(_p(), _l1(pt="Widgets", cat=cat), _ctx())
    h = [x for x in res.hits if x.rule_code == "zh_seller_mega_cat_forbidden"][0]
    assert h.detail["walmart_policy"] == policy


# ── R1 双白名单闸 ────────────────────────────────────────────────────────────

def _meta(access, zh, cat="Home"):
    return {"Widgets": {"walmart_category": cat, "walmart_ptg": None,
                        "access_state": access, "zh_can_do": zh,
                        "requirements": "", "notes": ""}}


def test_r1_access_blocked():
    res = audit_l2.evaluate(_p(), _l1(pt="Widgets"),
                            _ctx(pt_meta=_meta("需Walmart审批", "是")))
    h = [x for x in res.hits if x.rule_code == "cat_access_blocked"][0]
    assert h.penalty == -100
    assert h.detail["walmart_policy"] == "Restricted/Illegal"
    assert h.detail["rule"] == "access_state 不在白名单 {普通商品, 附条件允许}"


def test_r1_zh_blocked_and_prefix_pass():
    res = audit_l2.evaluate(
        _p(), _l1(pt="Widgets"),
        _ctx(pt_meta=_meta("普通商品", "否（上架记录回测,BIZ-CN触发X次）")))
    assert "cat_zh_blocked" in _codes(res)
    res2 = audit_l2.evaluate(
        _p(), _l1(pt="Widgets"),
        _ctx(pt_meta=_meta("附条件允许", "需评估 (要合规投入)")))
    assert "cat_zh_blocked" not in _codes(res2)
    assert "cat_access_blocked" not in _codes(res2)


def test_r1_both_bad_reports_access_only():
    res = audit_l2.evaluate(_p(), _l1(pt="Widgets"),
                            _ctx(pt_meta=_meta("禁售", "否")))
    assert "cat_access_blocked" in _codes(res)
    assert "cat_zh_blocked" not in _codes(res)


def test_r1_empty_access_shows_placeholder():
    res = audit_l2.evaluate(_p(), _l1(pt="Widgets"),
                            _ctx(pt_meta=_meta("", "是")))
    h = [x for x in res.hits if x.rule_code == "cat_access_blocked"][0]
    assert h.detail["access_state"] == "(空)"


def test_r1_pt_not_in_meta_silently_passes():
    res = audit_l2.evaluate(_p(), _l1(pt="UnknownPT"), _ctx())
    assert "cat_access_blocked" not in _codes(res)
    assert res.verdict == "pass"


# ── R2 禁售大类(真实 yaml)───────────────────────────────────────────────────

def test_r2_word_boundary_plural():
    """B6-11/12:'Bras' 命中 bra(可选复数 s),'Brackets' 不命中。"""
    ok = audit_l2.evaluate(_p(), _l1(pt="Sports Bras", cat=None),
                           _ctx(pt_meta={"Sports Bras": {
                               "walmart_category": None, "access_state": "普通商品",
                               "zh_can_do": "是", "requirements": "", "notes": "",
                               "walmart_ptg": None}}))
    assert "forbidden_mega_cat" in _codes(ok)
    no = audit_l2.evaluate(_p(), _l1(pt="Shelf Brackets"),
                           _ctx(pt_meta={"Shelf Brackets": {
                               "walmart_category": None, "access_state": "普通商品",
                               "zh_can_do": "是", "requirements": "", "notes": "",
                               "walmart_ptg": None}}))
    assert "forbidden_mega_cat" not in _codes(no)


def test_r2_detail_shape():
    res = audit_l2.evaluate(_p(), _l1(pt="E-Cigarette Kits", cat=None),
                            _ctx(pt_meta={"E-Cigarette Kits": {
                                "walmart_category": None, "access_state": "普通商品",
                                "zh_can_do": "是", "requirements": "", "notes": "",
                                "walmart_ptg": None}}))
    hits = [x for x in res.hits if x.rule_code == "forbidden_mega_cat"]
    assert hits and hits[0].detail["matched_by"] == "pt_contains"
    assert hits[0].penalty == -100


def test_r2_skips_unknown_pt():
    res = audit_l2.evaluate(_p(), _l1(pt="(unknown)"), _ctx())
    assert "forbidden_mega_cat" not in _codes(res)


# ── R3 认证四分支 ────────────────────────────────────────────────────────────

def _spec(hard=False, real=None, soft=False, softf=None):
    return {"Widgets": {"has_real_cert": hard, "real_cert_fields": real or [],
                        "has_soft_cert": soft, "soft_cert_fields": softf or []}}


def test_r3a_hard_keyword():
    res = audit_l2.evaluate(_p(), _l1(pt="Widgets"),
                            _ctx(pt_meta=_meta("普通商品", "是"),
                                 pt_spec=_spec()))
    assert "cat_requires_cert_hard" not in _codes(res)
    meta = _meta("普通商品", "是")
    meta["Widgets"]["requirements"] = "需 UL 认证与测试报告"
    res2 = audit_l2.evaluate(_p(), _l1(pt="Widgets"),
                             _ctx(pt_meta=meta, pt_spec=_spec()))
    h = [x for x in res2.hits if x.rule_code == "cat_requires_cert_hard"][0]
    assert h.penalty == -100
    assert "UL 认证" in h.detail["matched_hard_kws"]
    assert h.detail["source"] == "walmart_pt_meta.requirements"


def test_r3c_soft_only_zero_penalty():
    meta = _meta("普通商品", "是")
    meta["Widgets"]["requirements"] = "ISO 9001 质量体系"
    res = audit_l2.evaluate(_p(), _l1(pt="Widgets"),
                            _ctx(pt_meta=meta, pt_spec=_spec()))
    h = [x for x in res.hits if x.rule_code == "cat_requires_cert_soft"][0]
    assert h.penalty == 0 and res.verdict == "pass"


def test_r3b_small_part_vs_whole_unit():
    """B6-20/21:has_real_cert + nrtl 分流(真实 yaml:Switch=小件)。"""
    small = audit_l2.evaluate(
        _p(), _l1(pt="Toggle Switches"),
        _ctx(pt_meta={"Toggle Switches": {"walmart_category": None,
                      "access_state": "普通商品", "zh_can_do": "是",
                      "requirements": "", "notes": "", "walmart_ptg": None}},
             pt_spec={"Toggle Switches": {"has_real_cert": True,
                      "real_cert_fields": ["nrtl_information"],
                      "has_soft_cert": False, "soft_cert_fields": []}}))
    codes = _codes(small)
    assert "cat_requires_cert_small_part" in codes
    whole = audit_l2.evaluate(
        _p(), _l1(pt="Space Heaters"),
        _ctx(pt_meta={"Space Heaters": {"walmart_category": None,
                      "access_state": "普通商品", "zh_can_do": "是",
                      "requirements": "", "notes": "", "walmart_ptg": None}},
             pt_spec={"Space Heaters": {"has_real_cert": True,
                      "real_cert_fields": ["nrtl_information"],
                      "has_soft_cert": False, "soft_cert_fields": []}}))
    h = [x for x in whole.hits if x.rule_code == "cat_requires_cert_hard"][0]
    assert h.detail["source"] == "walmart_pt_spec.has_real_cert + nrtl_classifier"


def test_r3d_spec_soft():
    res = audit_l2.evaluate(
        _p(), _l1(pt="Widgets"),
        _ctx(pt_meta=_meta("普通商品", "是"),
             pt_spec=_spec(soft=True, softf=["smallPartsWarnings"])))
    h = [x for x in res.hits if x.rule_code == "cat_requires_cert_soft"][0]
    assert h.detail["source"] == "walmart_pt_spec.has_soft_cert"


# ── R4 品牌黑名单扫描(小自动机)──────────────────────────────────────────────

def _ac(words):
    import ahocorasick
    a = ahocorasick.Automaton()
    for w in words:
        a.add_word(w, w)
    a.make_automaton()
    return a


def test_r4_hit_and_boundary_and_own_brand():
    ac = _ac(["fender", "ninja foodi"])
    res = audit_l2.evaluate(_p(title="Ninja Foodi grill basket"),
                            _l1(pt=None), _ctx(ac=ac))
    h = [x for x in res.hits if x.rule_code == "title_desc_blacklist"][0]
    assert h.penalty == 0
    assert h.detail["matches"][0]["brand"] == "ninja foodi"
    assert h.detail["matches"][0]["matched_phrase"] == "Ninja Foodi"
    # 词边界:Fenderish 不命中
    assert "title_desc_blacklist" not in _codes(
        audit_l2.evaluate(_p(title="Fenderish style"), _l1(), _ctx(ac=ac)))
    # 自品牌豁免
    assert "title_desc_blacklist" not in _codes(
        audit_l2.evaluate(_p(title="Fender strap", brand="Fender"),
                          _l1(), _ctx(ac=ac)))


def test_r4_none_automaton_skips():
    res = audit_l2.evaluate(_p(title="Fender strap"), _l1(), _ctx(ac=None))
    assert "title_desc_blacklist" not in _codes(res)


# ── R7 促销宣称 ──────────────────────────────────────────────────────────────

def test_r7_strong_and_softonly_and_allcaps():
    res = audit_l2.evaluate(_p(title="The #1 Best Seller Steel Frame"),
                            _l1(), _ctx())
    h = [x for x in res.hits if x.rule_code == "content_promotional"][0]
    assert h.penalty == 0 and h.detail["walmart_policy"] == "Content Standards"
    # B6-32 soft-only 不产 hit
    assert "content_promotional" not in _codes(
        audit_l2.evaluate(_p(title="high quality bolt"), _l1(), _ctx()))
    # B6-33 全大写连跑;B6-34 噪声 token 不凑数
    assert "content_promotional" in _codes(
        audit_l2.evaluate(_p(title="HEAVY DUTY STEEL FRAME"), _l1(), _ctx()))
    assert "content_promotional" not in _codes(
        audit_l2.evaluate(_p(title="USB LED HDMI"), _l1(), _ctx()))


# ── R8 敏感内容 ──────────────────────────────────────────────────────────────

def test_r8_subtypes():
    res = audit_l2.evaluate(_p(title="Juneteenth party banner"), _l1(), _ctx())
    h = [x for x in res.hits if x.rule_code == "walmart_strict_sensitive"][0]
    assert "cultural_day" in h.detail["subtypes"]
    assert h.detail["walmart_policy"] == "Offensive Content"
    res2 = audit_l2.evaluate(_p(title="Mickey Mouse sticker set"), _l1(), _ctx())
    h2 = [x for x in res2.hits if x.rule_code == "walmart_strict_sensitive"][0]
    assert "cartoon_ip_character" in h2.detail["subtypes"]
    assert h2.penalty == 0


# ── evaluate 打分/阈值/下界 ──────────────────────────────────────────────────

def test_evaluate_soft_only_passes_with_hits():
    """B6-40:全 0 分软证据 → score 100 pass,但 hits 落账。"""
    res = audit_l2.evaluate(_p(title="Juneteenth HEAVY DUTY STEEL FRAME #1 best"),
                            _l1(), _ctx())
    assert res.score_final == 100 and res.verdict == "pass"
    assert len(res.hits) >= 2


def test_evaluate_stacking_and_floor():
    """B6-41:硬规则叠加 -100 不去重;R0+R1 = -100×2。"""
    res = audit_l2.evaluate(
        _p(), _l1(pt="Widgets", cat="Electronics"),
        _ctx(pt_meta={"Widgets": {"walmart_category": "Electronics",
                                  "walmart_ptg": None, "access_state": "禁售",
                                  "zh_can_do": "否", "requirements": "",
                                  "notes": ""}}))
    assert res.score_final == -100      # 100 - 100(R0) - 100(R1)
    assert res.verdict == "reject"


def test_evaluate_l1_hits_only_add_score():
    """B6-39:L1 hits 只加分不进 L2 hits 列表。"""
    l1 = _l1(pt=None)
    l1.hits = [RuleHit(stage="L1", rule_code="excluded_category", penalty=-100)]
    res = audit_l2.evaluate(_p(), l1, _ctx())
    assert res.score_final == 0 and res.verdict == "reject"
    assert "excluded_category" not in _codes(res)


# ── B7 理由映射 ──────────────────────────────────────────────────────────────

def _outcome(verdict="reject", hits=(), l1=None):
    o = AuditOutcome(asin="B0", verdict=verdict, score_final=0,
                     stage_stopped_at="L2", l1=l1 or _l1())
    o.l2 = SimpleNamespace(hits=list(hits))
    return o


def test_b7_pass_returns_none():
    assert audit_reason.compute_final_reason(_outcome(verdict="pass")) is None


def test_b7_hard_rule_first():
    hits = [RuleHit("L0", "phase0_forbidden_category", -100,
                    {"walmart_policy": "Intellectual Property"})]
    o = _outcome(hits=hits)
    assert audit_reason.compute_final_reason(o) == "Intellectual Property"


def test_b7_brand_blacklist_step10():
    o = _outcome(hits=[RuleHit("L0", "phase0_brand_blacklist", -100, {})])
    assert audit_reason.compute_final_reason(o) == "Intellectual Property"


def test_b7_cert_with_category_buckets():
    # pt 必须避开 _pt_to_policy 十组关键词(如 "Cribs" 会先被第 8 步 baby 组
    # 吃掉返回 Baby Products,轮不到第 9 步 cert 分桶)——顺序即语义
    o = _outcome(hits=[RuleHit("L2", "cat_requires_cert_hard", -100, {})],
                 l1=_l1(pt="Playmats", cat="Baby"))
    assert audit_reason.compute_final_reason(o) == "Children's Products"
    o2 = _outcome(hits=[RuleHit("L2", "cat_requires_cert_soft", 0, {})],
                  l1=_l1(pt="W", cat="Home"))
    assert audit_reason.compute_final_reason(o2) == "General-Use Products"


def test_b7_pt_to_policy_step8():
    o = _outcome(hits=[RuleHit("L2", "cat_zh_blocked", -100, {})],
                 l1=_l1(pt="Pepper Spray Holsters"))
    # cat_zh_blocked 的 walmart_policy="Restricted/Illegal" 走步 1.5 先返回
    # ——detail 为空时才轮到 _pt_to_policy;这里 detail 空,验证第 8 步
    assert audit_reason.compute_final_reason(o) == "Military & Law Enforcement"


def test_b7_fallback_general_use():
    o = _outcome(hits=[RuleHit("L2", "cat_zh_blocked", -100, {})])
    assert audit_reason.compute_final_reason(o) == "General-Use Products"


def test_b7_normalize_l3_cat():
    assert audit_reason._normalize_l3_cat("brand_misuse") == "Intellectual Property"
    assert audit_reason._normalize_l3_cat("None") is None
    assert audit_reason._normalize_l3_cat("some new policy") == "Some New Policy"


# ── 人话理由(所有者 2026-08-16:「General-Use Products 这是什么意思」)──────

def test_human_reason_leads_with_the_rule_not_the_policy():
    """⚠ `General-Use Products` 是 37 政策映射第 4g 步的**兜底**(以上全不中)。

    它落在一把锤子、一个土豆压泥器上时,人只会一头雾水 —— 那不是原因,
    是"没能归到具体哪条政策"。真正的原因在命中的规则里,而 hit.detail 里
    本来就写着中文 note,此前一个字都没露给人看。
    """
    h = audit_reason.human_reason(
        [("cat_requires_cert_hard",
          {"walmart_pt": "Hammers",
           "note": "飞书维护的合规要求 (含实验室证书/官方注册号), 搬运模式做不了",
           "matched_hard_kws": ["ASTM F2413", "CPC"]})],
        "General-Use Products")
    assert h.startswith("**该类目要求认证**")     # 人话在最前
    assert "搬运模式做不了" in h                  # 规则作者当场写下的"为什么"
    assert "ASTM F2413" in h                      # 命中的是哪条要求
    assert h.endswith("[政策:General-Use Products]")   # 平台口径留着,但不占头


def test_human_reason_keeps_the_policy_even_with_no_hits():
    """一条规则都没记到时也不能只留一个政策名 —— 要明说是"没记录"。"""
    assert audit_reason.human_reason([], "Intellectual Property") \
        == "未记录命中规则[政策:Intellectual Property]"
    assert audit_reason.human_reason([], None) == "未记录命中规则"


def test_human_reason_drops_process_only_hits():
    """`pt_dict_fallback` 之流是过程留痕不是拒绝原因,单独出现时不当理由显示。"""
    out = audit_reason.human_reason(
        [("pt_dict_fallback", {}), ("phase0_brand_blacklist", {"brand": "Nike"})],
        "Intellectual Property")
    assert "字典回落" not in out and "品牌黑名单(命中:Nike)" in out


def test_explain_hit_falls_back_to_the_rule_code_it_does_not_know():
    """新加规则忘了登记中文名时,露出规则码总好过露出空白。"""
    assert audit_reason.explain_hit("brand_new_rule", {}) == "brand_new_rule"
    assert audit_reason.explain_hit("brand_new_rule", {"note": "因为 X"}) \
        == "brand_new_rule(因为 X)"
