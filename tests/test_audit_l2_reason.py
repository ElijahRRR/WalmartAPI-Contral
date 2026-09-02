"""L2 硬规则/软证据 + 理由映射向量(spec_vectors B6/B7;旧仓零测试)。

ctx 用 SimpleNamespace 鸭子拼装;nice 走真实 refdata yaml(loader 即被测面之一);
R5 恒关(ctx.uspto=None)。
⚠ 2026-08-21 起 R3 只看飞书 requirements —— `pt_spec` / `nrtl_*` 三个 ctx 字段
连同 NRTL 整机/小件分类器一起下线,所以 `_ctx` 不再接 `pt_spec`。
"""

from types import SimpleNamespace

import pytest

from registry import paths, resources
from services import audit_l2, audit_reason
from services.audit_models import AuditOutcome, L1Info, ProductInfo, RuleHit

# 政策表 category_en 的两种形态:改名前(存量缩写名)/ 改名后(官方 42 名)。
# `_normalize_l3_cat` 的合同是"表里叫什么就回什么",两种形态都要成立。
_LEGACY_TABLE = tuple(resources.POLICY_LEGACY_NAMES)
_OFFICIAL_TABLE = tuple(
    f.read_text(encoding="utf-8").split("\n", 1)[0][2:].strip()
    for f in sorted(paths.policy_pages_dir("en").glob("*.md")))


def _ctx(pt_meta=None, ac=None):
    mapping, default = audit_l2.load_nice_mapping()
    return SimpleNamespace(
        pt_meta=pt_meta or {}, ac_automaton=ac,
        nice_mapping=mapping, nice_default=default, uspto=None)


def _p(**kw):
    kw.setdefault("asin", "B000TEST00")
    return ProductInfo(**kw)


def _l1(pt=None, cat=None, dead=False):
    """dead=True 模拟"上游已判死"(带一条 -100 的 L1 hit),取代已删的
    excluded_category_reason 字段。"""
    l1 = L1Info(walmart_product_type=pt, walmart_category=cat)
    if dead:
        l1.hits.append(RuleHit(stage="L1", rule_code="publication_pt_forbidden",
                               penalty=-100, detail={}))
    return l1


def _ok_meta(pt="Widgets", cat=None):
    """一个能过 R1 白名单的 pt_meta,给只想测软规则的用例用。"""
    return {pt: {"walmart_category": cat, "walmart_ptg": None,
                 "access_state": "普通商品", "zh_can_do": "是",
                 "requirements": "", "notes": ""}}


def _soft(title="", **kw):
    """跑一遍 evaluate,PT 走白名单直通 —— 只留软规则的命中。"""
    return audit_l2.evaluate(_p(title=title, **kw), _l1(pt="Widgets"),
                             _ctx(pt_meta=_ok_meta()))


def _codes(res):
    return [h.rule_code for h in res.hits]


# ── R1 双白名单闸 ────────────────────────────────────────────────────────────

def _meta(access, zh, cat="Home", req=""):
    return {"Widgets": {"walmart_category": cat, "walmart_ptg": None,
                        "access_state": access, "zh_can_do": zh,
                        "requirements": req, "notes": ""}}


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


def test_r1_pt_not_in_meta_is_pending_not_pass():
    """2026-08-20 P0:"查不到这个 PT" 曾经等于 "这个 PT 没问题",100 分放行。

    白名单是唯一的类目判据,判不了必须转待人工。
    ⚠ 这是**防御网**:当前接线下 resolve_pt 有同款 pt_meta 闸,走不到这里。
    钉住它是因为"查不到就放行"这个默认值本身不能存在 —— 上游闸一松就是
    静默满分放行且不报错。
    """
    res = audit_l2.evaluate(_p(), _l1(pt="UnknownPT"), _ctx())
    assert "cat_gate_pt_not_in_meta" in _codes(res)
    assert res.score_final == 100 and res.verdict == "pending"   # 不扣分:没有证据
    assert res.pending_reason


@pytest.mark.parametrize("pt", [None, "", "unknown", "(unknown)"])
def test_r1_unknown_pt_is_pending_not_pass(pt):
    res = audit_l2.evaluate(_p(), _l1(pt=pt), _ctx())
    assert "cat_gate_pt_unknown" in _codes(res) and res.verdict == "pending"


def test_r1_pending_never_downgrades_a_real_reject():
    """上游已判死(出版物硬禁)时 R1 整条不参与 —— 确定的拒不许变成待定。"""
    res = audit_l2.evaluate(_p(), _l1(pt=None, dead=True), _ctx())
    assert "cat_gate_pt_unknown" not in _codes(res)
    assert res.pending_reason is None and res.verdict == "reject"


# ── R3 认证四分支 ────────────────────────────────────────────────────────────

def test_r3a_hard_keyword():
    res = audit_l2.evaluate(_p(), _l1(pt="Widgets"),
                            _ctx(pt_meta=_meta("普通商品", "是")))
    assert "cat_requires_cert_hard" not in _codes(res)
    meta = _meta("普通商品", "是")
    meta["Widgets"]["requirements"] = "需 UL 认证与测试报告"
    res2 = audit_l2.evaluate(_p(), _l1(pt="Widgets"),
                             _ctx(pt_meta=meta))
    h = [x for x in res2.hits if x.rule_code == "cat_requires_cert_hard"][0]
    assert h.penalty == -100
    assert "UL 认证" in h.detail["matched_hard_kws"]
    assert h.detail["source"] == "walmart_pt_meta.requirements"


def test_r3c_soft_only_zero_penalty():
    meta = _meta("普通商品", "是")
    meta["Widgets"]["requirements"] = "ISO 9001 质量体系"
    res = audit_l2.evaluate(_p(), _l1(pt="Widgets"),
                            _ctx(pt_meta=meta))
    h = [x for x in res.hits if x.rule_code == "cat_requires_cert_soft"][0]
    assert h.penalty == 0 and res.verdict == "pass"


def test_r3_no_longer_reads_the_spec_snapshot_at_all():
    """⚠ 2026-08-21 收敛:R3 **只看飞书 requirements**,spec 那条链整条下线。

    所有者原话:「代码只判定确定性的,这种很明显不确定,应该交给 LLM 看这个产品
    是不是整机电器,而不是让代码从类目看是不是整机。所以,旧的死快照不要了,
    死代码也不要了,以飞书源为准,以后我们只更新这个」。

    生产实见的那条:一张**实木咖啡桌**被判「整机电器, 必须 NRTL 认证, 搬运做不了」
    —— 因为 `Coffee Tables` 的官方 spec 里带着 `has_nrtl_listing_certification`
    (那是给带 USB 口的电动桌准备的字段),而分类器拿 PT 名里有没有
    `parts`/`accessor` 裸子串猜整机/小件,咖啡桌两个词都不含 ⇒ 保守判整机。

    现在:飞书「必需认证」为空 ⇒ **一条 cert hit 都不出**,交给 L3 看产品本身。
    """
    res = audit_l2.evaluate(
        _p(title="FABATO 31.5'' Lift Top Coffee Table Wood Center Table"),
        _l1(pt="Coffee Tables"),
        _ctx(pt_meta={"Coffee Tables": {"walmart_category": "Furniture",
                      "access_state": "普通商品", "zh_can_do": "是",
                      "requirements": "", "notes": "", "walmart_ptg": None}}))
    assert not [c for c in _codes(res) if c.startswith("cat_requires_cert")]
    assert res.score_final == 100 and res.verdict == "pass"
    # 分类器与词表加载器都不该还在
    assert not hasattr(audit_l2, "_classify_nrtl_pt")
    assert not hasattr(audit_l2, "load_nrtl_keywords")


def test_the_whole_appliance_call_moved_to_the_l3_prompt():
    """删之前必须先补 —— 「整机电器」这一维现在住在 L3 提示词里(不留真空期)。

    这是所有者自己定过的纪律(2026-08-20 删 R0/R2 时):「关键是先补白名单、
    再删黑名单,中间不能有真空期」。
    """
    from services import audit_l3
    assert "整机电器" in audit_l3._S1 and "NRTL" in audit_l3._S1
    assert "只能看产品本身" in audit_l3._S1
    assert "拿不准一律 pass" in audit_l3._S1      # 默认放行,不许再连坐整类


def test_r3_hard_keywords_need_word_boundaries():
    """2026-08-20 P0:关键词曾是裸子串,`"ul" in "fda regulation"` 为真 ——
    任何 requirements 里写了 regulation 的类目都被打成「UL 认证」并 -100 硬拒。
    拒得理直气壮,理由却是从别的词里抠出来的两个字母。"""
    trap = _meta("普通商品", "是")
    trap["Widgets"]["requirements"] = "遵守 FDA regulation 的一般标签要求"
    res = audit_l2.evaluate(_p(), _l1(pt="Widgets"),
                            _ctx(pt_meta=trap))
    codes = _codes(res)
    assert "cat_requires_cert_hard" in codes          # fda 本身是真命中
    h = [x for x in res.hits if x.rule_code == "cat_requires_cert_hard"][0]
    assert "UL 认证" not in h.detail["matched_hard_kws"]   # 但 UL 不是
    # 其余三个同款陷阱:整条 requirements 不该产生任何硬认证
    for req in ("poison control 信息", "platform 使用说明", "idea 阶段无要求"):
        m = _meta("普通商品", "是")
        m["Widgets"]["requirements"] = req
        r = audit_l2.evaluate(_p(), _l1(pt="Widgets"),
                              _ctx(pt_meta=m))
        assert "cat_requires_cert_hard" not in _codes(r), req
    # 真写了这些认证,照样拦
    for req, label in (("需 UL 认证", "UL 认证"), ("ISO/DEA 管控物质", "DEA 管控"),
                       ("需 ATF 许可", "ATF 管控")):
        m = _meta("普通商品", "是")
        m["Widgets"]["requirements"] = req
        r = audit_l2.evaluate(_p(), _l1(pt="Widgets"),
                              _ctx(pt_meta=m))
        hh = [x for x in r.hits if x.rule_code == "cat_requires_cert_hard"][0]
        assert label in hh.detail["matched_hard_kws"], req


def test_r3_chinese_keywords_still_substring():
    """中文没有词边界,`\\b` 夹在两个汉字之间永不成立 —— 混排关键词维持子串。"""
    m = _meta("普通商品", "是")
    m["Widgets"]["requirements"] = "须完成 FDA 食品设施注册并指定美国代理人"
    r = audit_l2.evaluate(_p(), _l1(pt="Widgets"), _ctx(pt_meta=m))
    h = [x for x in r.hits if x.rule_code == "cat_requires_cert_hard"][0]
    assert "FDA 食品设施" in h.detail["matched_hard_kws"]


def test_infer_policy_medical_no_longer_lands_on_cosmetics():
    """2026-08-20 P1:第 9 步在**中文 label 拼接串**上找拉丁 'medical',
    永不成立 —— 医疗器械类目一律错落成 Cosmetic Products。"""
    assert audit_l2._infer_walmart_policy("", ["FDA 510(k)"], "Widgets") \
        == "Medical Devices"
    # 同一支里本来就靠中文命中的两条不受影响
    assert audit_l2._infer_walmart_policy("", ["FDA 食品设施"], "W") == "Food Products"
    assert audit_l2._infer_walmart_policy("", ["FDA 药品"], "W") \
        == "Drugs & Paraphernalia"


def test_soft_evidence_also_comes_only_from_feishu_now():
    """软合规同理:spec 那条软分支一并下线,软证据只从飞书「必需认证」出。"""
    res = audit_l2.evaluate(
        _p(), _l1(pt="Widgets"),
        _ctx(pt_meta=_meta("普通商品", "是", req="ASTM 测试报告")))
    h = [x for x in res.hits if x.rule_code == "cat_requires_cert_soft"][0]
    assert h.detail["source"] == "walmart_pt_meta.requirements (软合规)"
    assert h.penalty == 0


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


def test_r4_chinese_neighbours_are_boundaries():
    """2026-08-20:`c.isalnum()` 对汉字返回 True,于是「耐克运动鞋」里的
    黑名单词「耐克」左右都被判成词内字符 —— **中文品牌一个都拦不住**,
    而且不报错。中日韩不写分词空格,紧邻即边界。"""
    ac = _ac(["耐克", "小米"])
    res = audit_l2.evaluate(_p(title="耐克运动鞋 男款"), _l1(pt="Widgets"),
                            _ctx(pt_meta=_ok_meta(), ac=ac))
    h = [x for x in res.hits if x.rule_code == "title_desc_blacklist"][0]
    assert h.detail["matches"][0]["brand"] == "耐克"
    # 拉丁词边界不受影响(带音标字母仍算词内字符,不切出假前缀)
    ac2 = _ac(["caf"])
    assert "title_desc_blacklist" not in _codes(
        audit_l2.evaluate(_p(title="Café table"), _l1(pt="Widgets"),
                          _ctx(pt_meta=_ok_meta(), ac=ac2)))


def test_r4_none_automaton_skips():
    res = audit_l2.evaluate(_p(title="Fender strap"), _l1(), _ctx(ac=None))
    assert "title_desc_blacklist" not in _codes(res)


# ── R7 促销宣称 ──────────────────────────────────────────────────────────────

def test_r7_strong_and_softonly_and_allcaps():
    res = _soft(title="The #1 Best Seller Steel Frame")
    h = [x for x in res.hits if x.rule_code == "content_promotional"][0]
    assert h.penalty == 0 and h.detail["walmart_policy"] == "Content Standards"
    assert h.detail["soft_only"] is False
    # B6-33 全大写连跑;B6-34 噪声 token 不凑数
    assert "content_promotional" in _codes(_soft(title="HEAVY DUTY STEEL FRAME"))
    assert "content_promotional" not in _codes(_soft(title="USB LED HDMI"))


def test_r7_soft_only_now_keeps_the_evidence():
    """2026-08-20 修:只命中空洞形容词时,此前整条 hit 丢掉 ——
    detail 里写着"L3 LLM 需判断",而 L3 根本收不到。现在照样落账,
    penalty 仍是 0(不影响任何判定),用 soft_only 标出份量。"""
    res = _soft(title="high quality bolt")
    h = [x for x in res.hits if x.rule_code == "content_promotional"][0]
    assert h.penalty == 0 and h.detail["soft_only"] is True
    assert h.detail["soft_phrases"] and not h.detail["strong_phrases"]
    assert res.verdict == "pass"        # 证据留痕,不改结论


def test_r7_noise_tokens_are_case_normalized():
    """噪声表里的 "RoHS" 此前永远匹配不上(比较用 t.upper()),等于没写。"""
    assert "ROHS" in audit_l2._ALLCAPS_NOISE_TOKENS
    assert "content_promotional" not in _codes(_soft(title="ROHS USB LED"))


# ── R8 敏感内容 ──────────────────────────────────────────────────────────────

def test_r8_subtypes():
    res = _soft(title="Juneteenth party banner")
    h = [x for x in res.hits if x.rule_code == "walmart_strict_sensitive"][0]
    assert "cultural_day" in h.detail["subtypes"]
    assert h.detail["walmart_policy"] == "Offensive Content"
    res2 = _soft(title="Mickey Mouse sticker set")
    h2 = [x for x in res2.hits if x.rule_code == "walmart_strict_sensitive"][0]
    assert "cartoon_ip_character" in h2.detail["subtypes"]
    assert h2.penalty == 0


# ── evaluate 打分/阈值/下界 ──────────────────────────────────────────────────

def test_evaluate_soft_only_passes_with_hits():
    """B6-40:全 0 分软证据 → score 100 pass,但 hits 落账。"""
    res = _soft(title="Juneteenth HEAVY DUTY STEEL FRAME #1 best")
    assert res.score_final == 100 and res.verdict == "pass"
    assert len(res.hits) >= 2


def test_evaluate_stacking_and_floor():
    """B6-41:硬规则叠加 -100 不去重;R0/R2 删除后只剩 R1+R3 两条会扣分。"""
    res = audit_l2.evaluate(
        _p(), _l1(pt="Widgets", cat="Electronics"),
        _ctx(pt_meta={"Widgets": {"walmart_category": "Electronics",
                                  "walmart_ptg": None, "access_state": "禁售",
                                  "zh_can_do": "否",
                                  "requirements": "需 UL 认证", "notes": ""}}))
    assert res.score_final == -100      # 100 - 100(R1) - 100(R3a)
    assert res.verdict == "reject"
    assert _codes(res) == ["cat_access_blocked", "cat_requires_cert_hard"]


def test_evaluate_l1_hits_only_add_score():
    """B6-39:L1 hits 只加分不进 L2 hits 列表。"""
    l1 = _l1(pt=None)
    l1.hits = [RuleHit(stage="L1", rule_code="publication_pt_forbidden",
                       penalty=-100)]
    res = audit_l2.evaluate(_p(), l1, _ctx())
    assert res.score_final == 0 and res.verdict == "reject"
    assert "publication_pt_forbidden" not in _codes(res)


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


def test_b7_internal_blacklist_maps_to_none_not_general_use():
    """⚠ 2026-08-18 所有者实遇:B0F2ZS3M31(床头柜)命中 ASIN 黑名单被拒,
    F 列却写「ASIN 在黑名单中心 [政策:General-Use Products]」—— 黑名单中心
    是内部决策,不对应任何 Walmart 政策,此前无分支一路漏到 4g 兜底。
    三码(ASIN / 卖家 / 亚马逊类目)都必须映 None;品牌黑名单仍归 IP 不动。"""
    for code in ("phase0_lark_blacklist_asin", "phase0_lark_blacklist_seller",
                 "phase0_lark_blacklist_amazon_cat"):
        o = _outcome(hits=[RuleHit("L0", code, -100,
                                   {"asin": "B0F2ZS3M31",
                                    "source": "blacklist_center"})])
        assert audit_reason.compute_final_reason(o) is None, code
    # 政策 None ⇒ F 列只剩人话,没有 [政策:…] 尾巴
    h = audit_reason.human_reason(
        [("phase0_lark_blacklist_asin", {"asin": "B0F2ZS3M31"})], None)
    assert "[政策:" not in h
    assert "黑名单中心" in h


def test_b7_fallback_general_use():
    o = _outcome(hits=[RuleHit("L2", "cat_zh_blocked", -100, {})])
    assert audit_reason.compute_final_reason(o) == "General-Use Products"


def test_b7_normalize_l3_cat():
    assert audit_reason._normalize_l3_cat("brand_misuse") == "Intellectual Property"
    assert audit_reason._normalize_l3_cat("None") is None
    assert audit_reason._normalize_l3_cat("some new policy") == "Some New Policy"


def test_b7_normalize_l3_cat_follows_the_table_not_a_frozen_map():
    """⚠ 2026-09-02(§十.7):政策名归一化**随表**,不再写死一份缩写映射。

    写死的后果不报错:政策表改名成官方拼写后,L3 答出的官方名会被旧映射改写回
    一个**表里已经不存在**的缩写名 —— `audit_reason` / 政策表 / L3 的
    reason_category 白名单三处从此对不上,而三处都不会红。
    """
    n = audit_reason._normalize_l3_cat
    # 改名前:表里是缩写名,就回缩写名
    assert n("drugs & paraphernalia", _LEGACY_TABLE) == "Drugs & Paraphernalia"
    assert n("electronics & rf", _LEGACY_TABLE) == "Electronics & RF"
    # 改名后:表里是官方名,就回官方名(同一个函数、同一份代码)
    assert n("drugs and drug paraphernalia", _OFFICIAL_TABLE) == \
        "Drugs and Drug Paraphernalia"
    assert n("knives and OTHER melee weapons", _OFFICIAL_TABLE) == \
        "Knives and Other Melee Weapons"
    # 大小写无关(L3 是 LLM,大小写靠不住)
    assert n("FOOD PRODUCTS", _OFFICIAL_TABLE) == "Food Products"
    assert n("  Food Products  ", _OFFICIAL_TABLE) == "Food Products"


def test_b7_normalize_l3_cat_keeps_the_non_policy_pseudo_categories():
    """伪类目不是政策表里的行(brand_misuse 是 L3 提示词维度 4 的固定标签,
    content standards 一族是旧标签兼容)—— 表怎么改名都影响不到,只能写死。"""
    n = audit_reason._normalize_l3_cat
    for cat in ("brand_misuse", "brand misuse"):
        assert n(cat, _OFFICIAL_TABLE) == "Intellectual Property"
    for cat in ("content standards", "content_standards", "promotional content",
                "promotional", "content policy"):
        assert n(cat, _OFFICIAL_TABLE) == "Content Standards"
    # 政策名条目**全部**已删:表没传进来时不许再凭一张旧表把缩写名变出来
    assert "drugs & paraphernalia" not in audit_reason._L3_NORMALIZE
    assert not [k for k in audit_reason._L3_NORMALIZE
                if k not in ("brand_misuse", "brand misuse", "content standards",
                             "content_standards", "promotional content",
                             "promotional", "content policy")]


def test_b7_normalize_l3_cat_still_falls_back_to_title():
    """认不出来的照旧 `.title()` 回退 —— 保持旧行为,不扩大改动面。

    (它会把 `PFAS Chemicals` 变 `Pfas Chemicals`:那是照迁的已知缺陷,由
    `known_policies_check` 记日志计数,不改判定。)
    """
    n = audit_reason._normalize_l3_cat
    assert n("some new policy", _OFFICIAL_TABLE) == "Some New Policy"
    assert n("pfas chemicals") == "Pfas Chemicals"       # 没传表 ⇒ 旧行为
    assert n("pfas chemicals", _OFFICIAL_TABLE) == "PFAS Chemicals"   # 传了就随表


def test_b7_every_official_policy_name_normalizes_back_to_itself():
    """⚠ 守门:42 个官方名(refdata 头注 H1)必须**逐个**能归一化回自身。

    这是"旧脚本跟随 refdata"的机器可验形式:L3 照着提示词答出表里的类别名,
    这一层就必须原样交回去。任何一个被 `.title()` 改了形(`PFAS Chemicals` /
    `Children’s Products` / `Product claims` 那几个大小写特殊的),就是
    `audit_reason` 与政策表之间的一道静默错位。
    """
    assert len(_OFFICIAL_TABLE) == 44
    bad = [c for c in _OFFICIAL_TABLE
           if audit_reason._normalize_l3_cat(c, _OFFICIAL_TABLE) != c]
    assert bad == [], bad
    # 大小写乱掉的答案也要认回来(LLM 不保证大小写)
    bad2 = [c for c in _OFFICIAL_TABLE
            if audit_reason._normalize_l3_cat(c.upper(), _OFFICIAL_TABLE) != c]
    assert bad2 == [], bad2


def test_b7_l3_reason_category_goes_through_the_live_table():
    """步 2(L3 语义判)必须吃到实时集合 —— 否则改名后 L3 的答案会被改写。"""
    o = _outcome()
    o.l3 = SimpleNamespace(verdict="reject", hits=[],
                           reason_category="drugs and drug paraphernalia")
    assert audit_reason.compute_final_reason(o, None, _OFFICIAL_TABLE) == \
        "Drugs and Drug Paraphernalia"
    # 不传表 = 旧行为(.title() 回退),不炸
    assert audit_reason.compute_final_reason(o) == "Drugs And Drug Paraphernalia"


def test_b7_step15_resolves_the_l2_abbreviations_against_the_live_table():
    """⚠ `audit_l2._infer_walmart_policy` 返回的是**旧缩写名**(旧仓搬迁那批),
    它经规则 detail 的 `walmart_policy` 进理由映射,出口只有步 1.5 一个。

    政策表 2026-09-02 改成官方拼写后不解析的后果:每一条 cert 拒的
    `final_reason_category` 都落在政策表之外 —— 只多几条 warning 计数,判定
    不变,但 F 列写的政策名沃尔玛那边查无此类(申诉时报错名字)。
    """
    seen = set()
    for _kws, policy in audit_l2._PT_KEYWORD_TO_POLICY:
        seen.add(policy)
    for policy in audit_l2._CATEGORY_TO_POLICY.values():
        seen.add(policy)
    # 这批常量里确实还写着旧缩写名(本批**不动它们**,只在出口解析)
    assert seen & set(resources.POLICY_LEGACY_NAMES)

    for legacy, official in resources.POLICY_LEGACY_NAMES.items():
        o = _outcome(hits=[RuleHit("L2", "cat_requires_cert_hard", -100,
                                   {"walmart_policy": legacy})])
        assert audit_reason.compute_final_reason(
            o, None, _OFFICIAL_TABLE) == official, legacy
        # 改名落地前(表里还是缩写名)照旧回缩写名 —— 同一份代码两种表形态都活
        assert audit_reason.compute_final_reason(
            o, None, _LEGACY_TABLE) == legacy, legacy
        # 不传表 = 旧行为(原样返回),不炸
        assert audit_reason.compute_final_reason(o) == legacy


def test_b7_step15_keeps_a_name_it_cannot_resolve_instead_of_inventing_one():
    """解析不到的**原样返回**:编一个表里没有的名字,下游一路对不上还不报错。

    落在表外由调用方既有的 `known_policies_check` 记 warning 计数(不改判定)。
    """
    o = _outcome(hits=[RuleHit("L2", "cat_requires_cert_hard", -100,
                               {"walmart_policy": "Ghost Policy (old)"})])
    assert audit_reason.compute_final_reason(o, None, _OFFICIAL_TABLE) == \
        "Ghost Policy (old)"
    assert not audit_reason.known_policies_check("Ghost Policy (old)",
                                                 frozenset(_OFFICIAL_TABLE))


def test_b7_the_real_l2_inference_still_returns_the_frozen_constants():
    """本批**不逐条改** audit_l2 的常量(那是「L3 输出规范化」那一步的事)——
    这条钉住"没顺手改",免得下次有人看见两处都对表就以为常量已经换了。"""
    assert audit_l2._infer_walmart_policy("electronics", [], "Widget") == \
        "Electronics & RF"
    assert audit_l2._infer_walmart_policy("vehicles", [], "Widget") == \
        "Auto & Motor Vehicles"
    assert audit_l2._infer_walmart_policy(None, [], "vape pen") == \
        "Tobacco & Vaping"


# ── 人话理由(所有者 2026-08-16:「General-Use Products 这是什么意思」)──────

def test_human_reason_leads_with_the_rule_not_the_policy():
    """⚠ `General-Use Products` 是政策理由映射第 4g 步的**兜底**(以上全不中)。

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


def _meta_for(pt):
    return {pt: {"walmart_category": None, "access_state": "普通商品",
                 "zh_can_do": "是", "requirements": "", "notes": "",
                 "walmart_ptg": None}}


def test_r0_and_r2_are_gone_whitelist_is_the_only_category_judge():
    """2026-08-20 所有者定稿:类目能不能做只由 R1 白名单说了算。

    R0(代码里 8 个 walmart_category 硬禁)与 R2(yaml 18 条禁售大类)已删。
    这条用例钉的是**没有第二份平行清单**:白名单说行的类目,不许再被
    别处的黑名单拦下来;想加回黑名单,先在这里红。
    """
    assert not hasattr(audit_l2, "load_mega_categories")
    assert not hasattr(audit_l2, "_rule_forbidden_mega_cat")
    assert not hasattr(audit_l2, "_rule_zh_seller_mega_category_forbidden")
    # 曾被 R0 按 walmart_category 硬禁、被 R2 按 PT 词表硬禁的类目,
    # 只要白名单放行就一路 pass
    for pt, cat in (("Digital Cameras", "Electronics"), ("Wine", "Food & Beverage"),
                    ("Sweatpants", "Fashion"), ("Medical Thermometers",
                                                "Health & Personal Care")):
        res = audit_l2.evaluate(_p(), _l1(pt=pt, cat=cat),
                                _ctx(pt_meta=_ok_meta(pt, cat)))
        assert res.verdict == "pass", pt
        assert _codes(res) == [], pt


# ── R10 Made in USA 声明(2026-08-24,漏判反哺第一条硬规则)──────────────────

def _p10(title="", bullets=None, desc=""):
    from services.audit_models import ProductInfo
    return ProductInfo(asin="B0T", title=title,
                       bullet_points=bullets or [], long_description=desc)


def test_r10_made_in_usa_claim_hard_rejects():
    """字面声明即铁证:FTC 要求卖家实证,搬运文案永远实证不了。

    生产实证下架原因 "Prohibited Product Policy on Made in USA claims"。
    与"儿童品不进 L2"方向相反且都对:声明在文本里就是证据,儿童品要靠理解。
    """
    from services import audit_l2

    hits = audit_l2._rule_made_in_usa(_p10("Steel Bracket Made in USA"))
    assert len(hits) == 1 and hits[0].penalty == -100
    assert hits[0].rule_code == "made_in_usa_claim"
    # 长描述里的声明也要拦(R7/R8 只扫前 3 条五点,这条是硬拒,全文都扫)
    assert audit_l2._rule_made_in_usa(
        _p10("Table", desc="Proudly manufactured in the United States"))
    assert audit_l2._rule_made_in_usa(_p10("USA-Made leather belt"))
    assert audit_l2._rule_made_in_usa(_p10("American made blanket"))


def test_r10_word_boundary_and_negation_do_not_fire():
    """词边界防误伤 + 否定式排除 + 非声明语境不命中。"""
    from services import audit_l2

    assert not audit_l2._rule_made_in_usa(
        _p10("Jerusalem artichoke, thousand pieces"))
    assert not audit_l2._rule_made_in_usa(
        _p10("Made in China, not made in USA"))
    assert not audit_l2._rule_made_in_usa(_p10("Trip to USA travel guide"))
    assert not audit_l2._rule_made_in_usa(_p10(""))


def test_r10_is_wired_into_evaluate():
    """规则必须真挂进 evaluate 的规则列表 —— 单测函数绿而没接线是静默漏判。"""
    import inspect

    from services import audit_l2

    assert "_rule_made_in_usa" in inspect.getsource(audit_l2.evaluate)
