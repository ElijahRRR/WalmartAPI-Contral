"""L2 硬规则/软证据 + 理由映射向量(spec_vectors B6/B7;旧仓零测试)。

ctx 用 SimpleNamespace 鸭子拼装;nice 走真实 refdata yaml(loader 即被测面之一);
R5 恒关(ctx.uspto=None)。
⚠ 2026-08-21 起 R3 只看飞书 requirements —— `pt_spec` / `nrtl_*` 三个 ctx 字段
连同 NRTL 整机/小件分类器一起下线,所以 `_ctx` 不再接 `pt_spec`。
⚠ 2026-09-02 B1:理由映射收敛为**查表**(规则自报 `category` / L3 的 policy),
归一化那一族(`_normalize_l3_cat` / `_L3_NORMALIZE` / `_pt_to_policy`)整体退役
—— 政策名归一化只剩 `services/policy_names` 一处实现,测试见 test_policy_names。
"""

from types import SimpleNamespace

import pytest

from registry import resources
from services import audit_l2, audit_reason
from services.audit_models import (AuditOutcome, L1Info, Phase0Result,
                                   ProductInfo, RuleHit)


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
    # 规则自报类别(2026-09-02 B1 §二):白名单拦下 = **类目准入**。
    # ⚠ 原先写死的 `walmart_policy="Restricted/Illegal"` 是猜的(白名单拦下
    #   与那条禁售政策没有关系),B1 整个删掉 —— 别再写回来
    assert h.detail["category"] == resources.AUDIT_CAT_ACCESS
    assert "walmart_policy" not in h.detail
    assert h.detail["rule"] == "access_state 不在白名单 {普通商品, 附条件允许}"


def test_r1_zh_blocked_and_prefix_pass():
    res = audit_l2.evaluate(
        _p(), _l1(pt="Widgets"),
        _ctx(pt_meta=_meta("普通商品", "否（上架记录回测,BIZ-CN触发X次）")))
    assert "cat_zh_blocked" in _codes(res)
    h = [x for x in res.hits if x.rule_code == "cat_zh_blocked"][0]
    assert h.detail["category"] == resources.AUDIT_CAT_ACCESS   # §二,同上
    assert "walmart_policy" not in h.detail
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
    # 2026-09-02 B1 重写 S1 后,这一维住在「本 PT 的沃尔玛准入要求怎么判」那节:
    # 先判这个**具体产品**要不要那张证(NRTL 挂牌是它的典型形态),拿不准 pass
    assert "NRTL" in audit_l3._S1
    assert "先判**这个具体产品**要不要这张证" in audit_l3._S1
    assert "拿不准不拒" in audit_l3._S1           # 默认放行,不许再连坐整类
    assert "按类目名连坐整类" in audit_l3._S1


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


# ── B1 理由映射:查表,零兜底(2026-09-02,规格 §3.5)───────────────────────

def _outcome(verdict="reject", hits=(), l1=None):
    o = AuditOutcome(asin="B0", verdict=verdict, score_final=0,
                     stage_stopped_at="L2", l1=l1 or _l1())
    o.l2 = SimpleNamespace(hits=list(hits))
    return o


def test_reason_非reject恒None():
    assert audit_reason.compute_final_reason(_outcome(verdict="pass")) is None
    assert audit_reason.compute_final_reason(_outcome(verdict="pending")) is None
    assert audit_reason.compute_final_reason(None) is None


def test_reason_取第一条自报category的hit():
    """顺序 = all_hits 的 phase0 → l1 → l2(首个命中即出)。"""
    o = _outcome(hits=[RuleHit("L2", "cat_access_blocked", -100,
                               {"category": "类目准入"})])
    assert audit_reason.compute_final_reason(o) == "类目准入"
    # 多条自报时按 all_hits 顺序取第一条(L0 在 L2 之前)
    o2 = _outcome(hits=[RuleHit("L2", "cat_access_blocked", -100,
                                {"category": "类目准入"})])
    o2.phase0 = Phase0Result(blocked=True, hits=[
        RuleHit("L0", "phase0_brand_blacklist", -100,
                {"category": "Intellectual Property"})])
    assert audit_reason.compute_final_reason(o2) == "Intellectual Property"


def test_reason_规则没自报就轮到L3的policy():
    o = _outcome(hits=[RuleHit("L2", "some_soft_hit", 0, {})])
    o.l3 = SimpleNamespace(verdict="reject", hits=[],
                           policy="Drugs and Drug Paraphernalia")
    assert audit_reason.compute_final_reason(o) == "Drugs and Drug Paraphernalia"
    # L3 判 pass/pending 时它的 policy 不算数(pass 的 policy 恒 'none')
    o.l3 = SimpleNamespace(verdict="pass", hits=[], policy="none")
    audit_reason.reset_stats()
    assert audit_reason.compute_final_reason(o) is None


def test_reason_查不到不兜底_落None加计数加warning(caplog):
    """⚠ B1 删掉的九步兜底在这里立碑:曾经"以上全不中"会返回
    `General-Use Products` —— 一把螺丝刀、一个土豆压泥器都挂着它,人只会
    一头雾水(所有者 2026-08-16 实遇)。现在:None + 计数 + warning。

    ⚠ 2026-09-03 C 批之后这条计数是**纯 bug 信号**(L4 关闭时应恒为 0):
    曾经占着它的两条已知缺口都消化了 —— R3 硬拒整条删除(本 PT 的
    requirements 随产品进 L3),R10 迁进 L0 并自报 `Product claims`。
    用例里那个 `cat_requires_cert_hard` 现在只是"某条忘了自报的硬拒规则"的
    替身(存量 hits 里还有这个码)。
    """
    audit_reason.reset_stats()
    o = _outcome(hits=[RuleHit("L2", "cat_requires_cert_hard", -100,
                               {"walmart_policy": "Electronics & RF"})])
    with caplog.at_level("WARNING", logger="services.audit_reason"):
        for _ in range(3):                 # 同一组规则码来三次
            assert audit_reason.compute_final_reason(o) is None
    assert audit_reason.STATS["reason_missing"] == 3          # 计数逐次累加
    # ⚠ 但 warning **一轮只打一次**:已知缺口每轮能拒成千上万条,逐条打会把
    #   日志淹掉,而信息量只有第一条(与 audit_l3 那两个计数同款口径)
    assert len([r for r in caplog.records
                if "判拒但没有类别" in r.getMessage()]) == 1
    assert audit_reason.STATS["reason_missing:cat_requires_cert_hard"] == 3
    # ⚠ 旧的 `walmart_policy` 键**不再被读**:类别只认自报的 `category`
    assert audit_reason.compute_final_reason(
        _outcome(hits=[RuleHit("L0", "phase0_forbidden_category", -100,
                               {"walmart_policy": "Intellectual Property"})])) is None


def test_reason_九步推断全部退役():
    """守门:别照着旧文档把 PT 关键词猜测 / cert 分桶 / 归一化写回来。"""
    for gone in ("_pt_to_policy", "_L3_NORMALIZE", "_normalize_l3_cat",
                 "known_policies_check", "human_reason"):
        assert not hasattr(audit_reason, gone), gone
    # 类别键是唯一入口
    assert audit_reason.CATEGORY_KEY == "category"


def test_reason_L4判拒也不猜类别():
    """旧步 3 按 issue 文本猜 Offensive/IP —— 那是关键词推断,B1 删。

    L4 默认关;它判拒而没有类别时走"零兜底"那一路(计数 + NULL)。
    """
    from services.audit_l4 import L4Result
    audit_reason.reset_stats()
    o = _outcome()
    o.l4 = L4Result(verdict="reject", image_issues=[
        {"image_index": 0, "issue": "offensive gesture", "confidence": "high"}])
    assert audit_reason.compute_final_reason(o) is None
    assert audit_reason.STATS["reason_missing"] == 1


def test_reason_计数线程安全且可清零():
    audit_reason.reset_stats()
    assert audit_reason.STATS["reason_missing"] == 0
    audit_reason.bump("reason_missing")
    assert audit_reason.STATS["reason_missing"] == 1
    audit_reason.reset_stats()
    assert audit_reason.STATS["reason_missing"] == 0


# ── 人话理由(给人看的那一面)────────────────────────────────────────────────

def test_explain_hit_leads_with_the_rule_and_the_cell_it_read():
    """真正的原因在命中的规则里,而 hit.detail 里本来就写着中文 note。"""
    h = audit_reason.explain_hit(
        "cat_requires_cert_hard",
        {"walmart_pt": "Hammers",
         "note": "飞书维护的合规要求 (含实验室证书/官方注册号), 搬运模式做不了",
         "matched_hard_kws": ["ASTM F2413", "CPC"]})
    assert h.startswith("**该类目要求认证**")     # 人话在最前
    assert "搬运模式做不了" in h                  # 规则作者当场写下的"为什么"
    assert "ASTM F2413" in h                      # 命中的是哪条要求


def test_explain_hit_不再把自报类别当命中值():
    """⚠ `category` 现在是**规则自报的类别**(§二),不是"命中的那个值"。

    留在 hit_val 键表里的后果:专利自述那条会渲染成
    「文案自述专利保护(…;命中:Intellectual Property)」—— 把类别名说成
    命中的原文,人照着去搜根本搜不到。
    """
    out = audit_reason.explain_hit("phase0_patent_claim",
                                   {"category": "Intellectual Property",
                                    "note": "文案自述专利保护"})
    assert out == "文案自述专利保护(文案自述专利保护)"
    assert "命中:" not in out


def test_explain_hits_取代human_reason_不带政策尾巴():
    """类别已单列(products.audit_reason / 上架表 G 列),这一句只说规则。"""
    out = audit_reason.explain_hits(
        [("pt_dict_fallback", {}), ("phase0_brand_blacklist", {"brand": "Nike"})])
    assert out == "品牌黑名单(命中:Nike)"        # 过程留痕不当理由
    assert "[政策:" not in out
    assert audit_reason.explain_hits([]) == "未记录命中规则"
    # 最多三条,顺序即入参顺序
    many = audit_reason.explain_hits(
        [("phase0_brand_blacklist", {"brand": f"B{i}"}) for i in range(5)])
    assert many.count(";") == 2


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
