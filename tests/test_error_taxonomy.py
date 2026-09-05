"""报错归类引擎(services/error_taxonomy)的语料回归与守门(2026-09-01 建)。

方案定稿 `docs/error_taxonomy.md`;本文件是它的**机器可验形式**,三段:

  ① 语料逐行断言 —— `tests/fixtures/reason_corpus.jsonl`(77 行)与
     `feed_error_corpus.jsonl`(20 行),原文全部取自生产实查(前 70 行
     2026-08-31 全量实查;#71-#77 是 2026-09-01 首轮对照报告的 unknown 清单
     补收,`provenance:"prod-2026-09-01-report"`,标 `truncated` 的是报告展示
     截断、判据只用可见段),**一行不许跳**。语料是验收标准:引擎迁就语料,
     不是语料迁就引擎。
  ② ~~旧行为快照~~ —— 2026-09-04 随旧引擎一并删除(所有者:「旧码不需要留」);
     曾经是:同一批 reason 语料跑 `problem_products.categorize()`,
     把它**现在**的输出冻死在这里。第二步换轨时 diff 一目了然:哪些条从
     A/J/Z 翻成了真问题,是有账可查的,不是"看起来变好了"。
     ⚠ 快照是**现状**不是**期望**:里面 Z 一片、Stage 归"特殊"、
     "offensive content standards" 被判成内容问题,都是旧引擎的真实病灶,
     照抄不改(改了就不叫快照了)。
  ③ 守门 —— 码表/主码序/别名表/规范化边角,以及"对照报告只读"这条红线。
"""

import ast
import json
import pathlib
import re

import pytest

from registry import paths, resources
from services import error_taxonomy as et
from services import policy_names
from services import problem_products

_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> list[dict]:
    text = (_FIXTURES / name).read_text(encoding="utf-8")
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


REASONS = _load("reason_corpus.jsonl")
FEED_ERRORS = _load("feed_error_corpus.jsonl")

# 政策表 category_en 对照清单 —— **目标态**(2026-09-02 定稿 §十.7:官方政策
# 类别名 = 全链唯一键)。`policy_sync` 真跑后生产表就长这样:官方 44 名(42 禁售 + 2 内容族),
# 逐字取自 `refdata/policy_pages/en/*.md` 的头注 H1(下面 `test_known_policies_
# are_verbatim_official_names` 守门,漂了当场红)。
#
# ⚠ 写死而不是现读 refdata:这份清单是**断言的真值**,现读等于拿被测数据当答案。
#   守门测试负责证明两者一致。
# ⚠ 拼写细节别"顺手修":`Children’s` 是**弯撇号**、`Product claims` /
#   `Resold products` 官方就是小写、Tobacco 那条**没有牛津逗号**、Jewelry 那条
#   带 `(Covered Goods)` 后缀 —— 官方怎么写就怎么抄。
KNOWN_POLICIES = (
    "Alcohol", "Animals", "Art", "Artifacts and Antiquities",
    "Auto and Motor Vehicles", "Autographs and Collectibles", "Baby Products",
    "Children’s Products", "Cosmetic Products", "Dietary Supplements",
    "Digital Goods", "Drugs and Drug Paraphernalia",
    "Electronics and Radio Frequency Devices", "Food Products",
    "Funeral Products", "General-Use Products", "Hazardous Items",
    "Home Goods", "Intellectual Property",
    "Jewelry, Watches, Precious Gemstones, Currency, Coins and Precious "
    "Metals (Covered Goods)",
    "Medical Devices", "Medical Foods",
    "Military and Law Enforcement Products", "Native American Products",
    "Offensive Content", "Pet Foods, Supplements, Medicines and Other Products",
    "PFAS Chemicals", "Plants and Seeds", "Product claims", "Recalled Products",
    "Resold products", "Restricted/Illegal Products",
    "Ride-Ons and Micromobility Devices", "Software", "Stamps and Tickets",
    "Textiles and Apparel", "Tobacco, E-Cigarettes and Vaping Products",
    "Air Powered Guns, BB Guns, Toy Guns and Imitation Firearms", "Firearms",
    "Firearm Accessories", "Firearm Ammunition",
    "Knives and Other Melee Weapons",
    # 内容族两页(2026-09-02,A 批):不是禁售类别,是「content policy」/
    # 「authenticity claims」两类下架原因所指页面;同表同枚举
    "Content standards: Overview", "Product details policy",
)

# 2026-09-02 改名落地**之前**生产表里那一族旧缩写名(历史事实,不是当前状态)。
# 只用于证明"这类语义缩写今天 join 不上、该进政策表缺口清单"—— 别拿它当真值。
LEGACY_ABBREVIATIONS = ("Drugs & Paraphernalia", "Electronics & RF",
                        "Military & Law Enforcement", "Pet Products",
                        "Restricted/Illegal", "Ride-Ons & Micromobility")


# ══════════════════════════════════════════════════════════════════════════════
#  ① 语料逐行
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("row", REASONS,
                         ids=[f"L{i}" for i in range(1, len(REASONS) + 1)])
def test_reason_corpus_row(row):
    """下架原因语料逐行:主码 / 政策名 / 子类 / AI 标记 / 逐原子码全核。"""
    atoms = et.split_reasons(row["text"])
    res = et.classify_reasons(atoms, KNOWN_POLICIES)
    note = row.get("note", "")

    assert res.code == row["expect_code"], f"{note}\n原文:{row['text'][:200]}"
    assert res.name == resources.ERROR_CATEGORY_CODES[res.code]
    assert res.policy_name == row.get("expect_policy"), note
    # 没写 expect_sub 的行,子类必须是 None —— 别在没子类的地方凭空拆出一个
    assert res.policy_sub == row.get("expect_sub"), note
    assert res.via_ai is bool(row.get("expect_via_ai")), note

    codes = [c for c, _p in res.atom_codes]
    assert codes == row.get("atoms_expect", [row["expect_code"]]), note

    if row["expect_code"] == "OTHER":
        # 语料里的 OTHER 全是**显式清单**(business decision / trust & safety):
        # 进 unlisted、不进 unknown 告警(方案 §3.3 序 16)
        assert res.unlisted, "显式杂项应记进 unlisted"
        assert res.unknown == (), "显式清单不该进 unknown 告警"
    else:
        assert res.unknown == (), f"不该有未识别原子:{res.unknown}"


@pytest.mark.parametrize("row", FEED_ERRORS,
                         ids=[f"F{i}" for i in range(1, len(FEED_ERRORS) + 1)])
def test_feed_error_corpus_row(row):
    """feed 报错语料逐行:通道 / 码 / 政策名全核(通道判定次序见方案 §3.6)。"""
    res = et.classify_feed_error(row["code"], row["field"],
                                 row["description"], row["status"])
    note = row.get("note", "")
    assert res.channel == row["expect_channel"], f"{note}\n{row['description'][:160]}"
    assert res.code == row.get("expect_code"), note
    assert res.policy_name == row.get("expect_policy"), note
    # 原样带回:operational 通道靠这两格说话(已结构化,不强行进 16 码表)
    assert (res.err_code, res.field) == (row["code"], row["field"])


def test_the_whole_corpus_is_covered_not_a_subset():
    """夹具是验收标准 —— 行数少了说明有人删了语料(只许读不许改)。"""
    assert len(REASONS) == 78 and len(FEED_ERRORS) == 20


# ══════════════════════════════════════════════════════════════════════════════
#  ② 旧行为快照(冻结现状,第二步换轨时 diff 用)
# ══════════════════════════════════════════════════════════════════════════════
#
# 生成方式:对 reason 语料逐行实跑 `problem_products.categorize()`,输出**人工
# 逐条核对**过旧 _RULES/_SEVERITY_ORDER 后写死(方案 §八.5)。其中 6 行还与
# 语料 note 里记的生产计数互证(#63 归 A、#64/#66/#70 归 J、#67 归 D、#68 归 A)。
#
# 这张表里最该被看见的四类现状:
#   · Z 其他 12 行 —— 禁售不可申诉(#4)、品牌未授权(#7/#23)、无价/无物流(#37/#40)、
#     图片指引(#53)、内容质量(#54)、显式杂项(#50/#51)旧引擎**一条都判不出**;
#   · #34 判 I 内容 —— "do not meet **offensive** content standards" 被裸判据
#     `content standards` 抢走,病根是政策拒却记成内容问题(方案 §3.3 序 9 的反例);
#   · #2/#60/#64/#65/#66/#70 判 J 特殊 —— Stage 与 Preorder 挤在一个码里,
#     中性码盖住了同记录里的 PROHIBITED_FINAL / POLICY / PT_WRONG;
#   · #63/#68 判 A 过期 —— 同上,过期盖住了终局禁售与品牌未授权。
# ⚠ 2026-09-04:「旧引擎输出快照」那 77 条参数化断言,连同
# `test_snapshot_covers_every_corpus_line` / `test_the_two_engines_really_do_disagree`
# **一并删除** —— 所有者定「删除旧码,我们已经迁移到新码,旧码不需要留」,
# `problem_products.categorize()` 已删,给不存在的函数留测试是自欺。
# 语料本身**一条没动**(77 行仍在 `reason_corpus.jsonl`),新引擎的逐行断言
# 在上面的 `test_reason_corpus_row` —— 判据的守门只剩这一处,正是要的。


# ══════════════════════════════════════════════════════════════════════════════
#  ③ 守门
# ══════════════════════════════════════════════════════════════════════════════


def test_rules_cover_every_code():
    """16 码每一个都得有判据 —— 有码无判据 = 那一类永远归不出来。"""
    assert {r.code for r in et.RULES} == set(resources.ERROR_CATEGORY_CODES)


def test_rule_order_is_the_table_order():
    """表序即优先级:RULES 必须按 order 严格递增排列,首命中即码。"""
    orders = [r.order for r in et.RULES]
    assert orders == sorted(orders) == list(range(len(et.RULES)))


def test_severity_matches_the_code_table():
    """主码序与码表**同一个集合**,不多不少不重复。"""
    assert set(resources.ERROR_CATEGORY_SEVERITY) == set(resources.ERROR_CATEGORY_CODES)
    assert len(resources.ERROR_CATEGORY_SEVERITY) == len(resources.ERROR_CATEGORY_CODES)


def test_severity_puts_the_neutral_codes_last():
    """非中性永远压过中性 —— STAGE/EXPIRED 排在最后两位,是这套码表的立命之本。"""
    assert resources.ERROR_CATEGORY_SEVERITY[-2:] == ("STAGE", "EXPIRED")
    assert resources.ERROR_CATEGORY_SEVERITY[0] == "PROHIBITED_FINAL"


def test_known_policies_are_verbatim_official_names():
    """⚠ 上面那份清单是断言的**真值**:它与 refdata 头注 H1 差一个字符,
    下面所有 join 断言就都在拿一个官方并不存在的拼写当标准答案。"""
    heads = tuple(f.read_text(encoding="utf-8").split("\n", 1)[0][2:].strip()
                  for f in sorted(paths.policy_pages_dir("en").glob("*.md")))
    assert len(heads) == 44
    assert set(KNOWN_POLICIES) == set(heads)
    assert len(set(KNOWN_POLICIES)) == 44


def test_the_legacy_alias_table_is_retired():
    """⚠ 2026-09-03 C 批:`POLICY_ALIASES` / `alias_gaps` 连同
    `registry.resources.POLICY_LEGACY_NAMES` 一起删除。

    它是改名过渡期的桥:报错正文里的政策名一直是官方全称,而政策表存量行是
    旧仓搬迁时的缩写名。2026-09-02 `policy_sync` 真跑把表内名全改成官方拼写
    之后,**直接键就命中**,桥的两头连的是同一个地方。
    留着的代价不是多几行代码,是多一条"对不上就翻译一下再试"的暗道 ——
    而报错正文里出现表里没有的政策名,本来就该进「政策表缺口」让人看见。
    """
    for gone in ("POLICY_ALIASES", "alias_gaps"):
        assert not hasattr(et, gone), gone
    assert not hasattr(resources, "POLICY_LEGACY_NAMES")
    from workflows import error_reclass_report as wf
    assert not hasattr(wf, "_alias_notes")


def test_semantic_abbreviations_now_land_in_the_policy_gap_list():
    """⚠ 别名退役后,语义缩写 join 不上 —— **那是正确答案**。

    这些串今天只会出现在两种地方:改名前的历史报告,或政策表被人改回旧拼写。
    前者与今天无关,后者正该在「政策表缺口」清单上显形,而不是被一张历史
    映射表悄悄接住(归一化打得平的纯词形差 `&`↔`and` 不在此列,见下)。
    """
    for legacy in LEGACY_ABBREVIATIONS:
        assert et.policy_join(legacy, KNOWN_POLICIES) is None, legacy
    # 纯词形差照旧打得平(那一级没动)
    assert et.policy_join("Auto & Motor Vehicles", KNOWN_POLICIES) == \
        "Auto and Motor Vehicles"
    assert et.policy_join("Textiles & Apparel", KNOWN_POLICIES) == \
        "Textiles and Apparel"


def test_policy_join_never_rewrites_the_extracted_name():
    """join 归 join,policy_name 归 policy_name:抽出什么保留什么(语料 #26 钉死)。"""
    assert et.policy_join("Auto and Motor Vehicles", KNOWN_POLICIES) == \
        "Auto and Motor Vehicles"
    assert et.policy_join("Food Products", KNOWN_POLICIES) == "Food Products"
    # 武器族 2026-09-02 起在表里了(补齐官方 42 类),照实说有
    assert et.policy_join("Knives and other Melee Weapons", KNOWN_POLICIES) == \
        "Knives and Other Melee Weapons"
    assert et.policy_join("Firearm Accessories", KNOWN_POLICIES) == \
        "Firearm Accessories"
    # 表里没有的照实说没有 —— 不许在别名表里做语义合并把它塞给别的政策
    assert et.policy_join("Weapons", KNOWN_POLICIES) is None
    assert et.policy_join(None, KNOWN_POLICIES) is None
    assert et.policy_join("Food Products", ()) is None
    row = next(r for r in REASONS if r.get("expect_policy") == "Auto and Motor Vehicles")
    res = et.classify_reasons(et.split_reasons(row["text"]), KNOWN_POLICIES)
    assert res.policy_name == "Auto and Motor Vehicles"


# 旧手写 `_norm_key`(折叠空白 + casefold + 弯引号归直)在语料上的实测命中数,
# 2026-09-02 归并前跑出来的。新实现**只许升不许降**:归一化放宽的理由就是它。
_BASELINE_OFFICIAL_JOINS = 16


def test_widening_the_join_key_never_loses_ground_on_the_corpus():
    """⚠ `_norm_key` 2026-09-02 放宽到 `policy_names.norm_category`(同一份实现)。

    放宽是判定面之外的事(`policy_join` 只喂报告,`policy_name` 一律保留原文),
    但"放宽"这种改动天然可疑:它可能在补上一处缺口的同时悄悄丢掉另一处。
    所以这条守门量的是**语料里 19 个政策名在官方表上的命中数**,与旧手写实现
    的实测值(16/19)比:只许升不许降,且改名落地后应当**一条不剩** ——
    `Plants & Seeds`(& vs and)、牛津逗号 Tobacco、不带 `(Covered Goods)` 的
    Jewelry 这三种报错写法,正是归并前白白进"政策表缺口"清单的那些。

    ⚠ 2026-09-03 C 批删掉了这条用例的另一半(改名落地**前**那张 30 行近似表
    上的命中数):那一半量的是旧名别名表这座桥,而桥与它的两头
    (`POLICY_ALIASES` / `POLICY_LEGACY_NAMES`)已经拆了。今天的生产表就是
    官方拼写,再钉一个"改名前也能对上"只会把退役的东西又焊回来。
    """
    wanted = sorted({r["expect_policy"] for r in REASONS if r.get("expect_policy")})
    assert len(wanted) == 19
    official = [v for v in wanted if et.policy_join(v, KNOWN_POLICIES)]
    assert len(official) >= _BASELINE_OFFICIAL_JOINS
    # 改名落地后一条都不该剩(剩下的会进对照报告的「政策表缺口」清单)
    assert sorted(set(wanted) - set(official)) == []


def test_the_join_key_still_refuses_to_merge_two_different_policies():
    """⚠ 放宽的边界:词形可以削,语义不许合 —— 44 个官方名两两不撞
    (`policy_names` 那份实现自带守门,这里从**报告侧**再证一次:
    任意两个官方名之间不许 join 到对方)。"""
    keys = {et._norm_key(n) for n in KNOWN_POLICIES}
    assert len(keys) == len(KNOWN_POLICIES)
    for name in KNOWN_POLICIES:
        assert et.policy_join(name, KNOWN_POLICIES) == name
    # 缩写差照旧不许自己合并(那要人裁决,见 policy_sync 的「疑似改名对」)
    assert et._norm_key("Electronics & RF") != \
        et._norm_key("Electronics and Radio Frequency Devices")


def test_no_rule_uses_the_bare_content_standards_needle():
    """⚠ 裸 `content standards` 会把政策拒抢成内容问题(语料 #34 的 sex toys 条)。

    判据必须带主语("does not meet **our** content standards" / "content
    **quality** standards")。这条守门是为了让后来加判据的人当场撞墙。
    """
    for rule in et.RULES:
        assert "content standards" not in rule.needles


def test_the_content_policy_needle_never_steals_a_policy_rejection():
    """⚠ `content policy`(序 9)排在政策词根(序 15)前面 —— 靠的是连续子串。

    2026-09-01 轮次二补收 `content policy` 时当场核过的风险:政策原文里
    "Offensive Content" 后面永远紧跟句号或逗号,再另起 "Walmart's policy
    prohibits…",两词拼不出连续的 `content policy`。这条守门把它钉死 ——
    以后谁把判据放宽成 `content` + `polic` 的松匹配,整批政策拒会当场翻码。
    """
    hits = [i for i, row in enumerate(REASONS, 1)
            if any("content policy" in et.normalize_atom(a).fold
                   for a in et.split_reasons(row["text"]))]
    assert hits == [71], f"除 #71 外还有行含 content policy:{hits}"
    assert REASONS[70]["expect_code"] == "CONTENT"
    for row in REASONS:
        if row["expect_code"] == "POLICY":
            for atom in et.split_reasons(row["text"]):
                assert "content policy" not in et.normalize_atom(atom).fold
    for row in FEED_ERRORS:
        if row.get("expect_code") == "POLICY":
            assert "content policy" not in et.normalize_atom(
                row["description"]).fold


def test_normalize_unescapes_double_encoded_entities():
    """&amp;amp; 双重转义:循环到不动点(生产加拿大版原文实见)。"""
    assert et.normalize_atom("Prohibited &amp;amp; restricted").text == \
        "Prohibited & restricted"
    assert et.normalize_atom("a &amp;amp;amp; b").text == "a & b"


def test_normalize_strips_html_tags_without_gluing_words():
    """<p> 包裹整句是生产常态;`Items</p><p>page` 不许被粘成一个词。"""
    assert et.normalize_atom(
        "<p>This item is unpublished due to Prohibited Product policy.</p>"
    ).text == "This item is unpublished due to Prohibited Product policy."
    assert et.normalize_atom("…Unpublished Items</p><p>&nbsp;page.</p>").text == \
        "…Unpublished Items page."


def test_normalize_strips_link_markup_including_the_broken_one():
    """链接标记保留链文本丢 url;残缺标记(`- )` 结尾那批)尽量剥,剥不净不报错。"""
    assert et.normalize_atom("see ||guide@@@https://x.com/a|| now").text == \
        "see guide now"
    # 残缺:无闭合 ||,url 段只吃一个 token —— 不许把标记后面的整句话一起吞了
    assert et.normalize_atom(
        "…violation of ||Walmart Marketplace’s Intellectual Property policy"
        "@@@https://x.com/a- )").text == \
        "…violation of Walmart Marketplace’s Intellectual Property policy )"
    assert et.normalize_atom(
        "||A@@@https://x.com/a This item is prohibited and not eligible for appeal."
    ).fold.endswith("not eligible for appeal.")
    # 生产实见只有两个 @ 的写法(语料 #54),分隔符不许死写 @@@
    assert et.normalize_atom("visit ||Listing Quality Dashboard@@https://x/b|| ok"
                             ).text == "visit Listing Quality Dashboard ok"


def test_rules_do_not_anchor_on_the_first_letter():
    """缺首字母 typo(生产 29 条 `his item…`)必须与完整句同码。"""
    full = "This item was unpublished due to brand restrictions."
    typo = "his item was unpublished due to brand restrictions."
    assert et.classify_atom(full).code == et.classify_atom(typo).code == "BRAND"


def test_normalize_gives_both_copies():
    """两个副本:casefold 供句式匹配,原大小写供政策名抽取。"""
    norm = et.normalize_atom("  Prohibited   Product   Policy: Home Goods  ")
    assert norm.text == "Prohibited Product Policy: Home Goods"
    assert norm.fold == norm.text.casefold()


def test_split_reasons_splits_on_the_join_used_by_api_items():
    assert et.split_reasons("a; b") == ["a", "b"]
    assert et.split_reasons("") == [] and et.split_reasons(None) == []
    assert et.split_reasons("only one") == ["only one"]


def test_split_reasons_never_splits_an_ai_message():
    """AI 文案自身含分号 —— 整行短路,否则一句话被劈成两条假原子。"""
    text = ("Your item listing does not meet our content standards because the "
            "title is ambiguous; and the size is inconsistent. This message has "
            "been generated by AI and may be inaccurate.")
    assert et.split_reasons(text) == [text]
    res = et.classify_reasons(et.split_reasons(text))
    assert (res.code, res.via_ai) == ("CONTENT", True)


def test_extract_policy_requires_the_colon():
    """无冒号 = 无候选,不猜(两个生产反例:类别在标记外 / 结构反置)。"""
    assert et.extract_policy(
        "…violating Walmart's Marketplace *Prohibited Product Policy* for Toys."
    ) == (None, None)
    assert et.extract_policy(
        "…prohibited due to Walmart's Marketplace Children's Products "
        "Prohibited Products Policy."
    ) == (None, None)
    assert et.extract_policy("Prohibited Products Policy: Home Goods.") == \
        ("Home Goods", None)


def test_extract_policy_keeps_commas_that_belong_to_the_name():
    """政策名自己带逗号(语料 #32/#45),续句才截 —— 见 _split_main_sub 头注。"""
    assert et.extract_policy(
        "Prohibited Product Policy: Tobacco, E-Cigarettes, and Vaping Products."
    ).name == "Tobacco, E-Cigarettes, and Vaping Products"
    assert et.extract_policy(
        "Prohibited Product Policy: Hazardous Items, please review your items."
    ).name == "Hazardous Items"
    assert et.extract_policy(
        "Prohibited Product Policy: Offensive Content, Halloween Items."
    ) == ("Offensive Content", "Halloween Items")


def test_extract_policy_splits_the_politics_subcategory():
    """子类词形家族补 Politics(方案 §3.4.3,2026-09-01 轮次二)。

    ⚠ 本条用的是 **synthetic 拟造串**:语料里没有 Politics 的生产全文,
    所以它只配待在单元测试段,**不许写进 fixtures**(夹具全部是生产原文)。
    钉的是拆分行为本身:家族词命中 → 逗号前是主名、逗号后进 policy_sub;
    没命中家族又不是小写续句的逗号,照旧整串留给主名(#32/#45 那类)。
    """
    assert et.extract_policy(                       # synthetic
        "Prohibited Product Policy: Offensive Content, Politics."
    ) == ("Offensive Content", "Politics")
    res = et.classify_reasons(et.split_reasons(     # synthetic
        "This item was unpublished for violating Walmart's Marketplace "
        "Prohibited Products Policy: Offensive Content, Politics."))
    assert (res.code, res.policy_name, res.policy_sub) == \
        ("POLICY", "Offensive Content", "Politics")


def test_classify_reasons_on_nothing_does_not_explode():
    res = et.classify_reasons([])
    assert (res.code, res.atom_codes, res.unknown) == ("OTHER", (), ())


def test_unknown_atoms_come_back_verbatim_for_the_alarm_list():
    """未识别原子**原文全文**带回:调用方要照着原文补判据,截断了就补不了。"""
    text = "Some brand new sentence Walmart just invented, with detail."
    res = et.classify_reasons(et.split_reasons(text))
    assert res.code == "OTHER" and res.unknown == (text,) and res.unlisted == ()


def test_feed_status_gate_is_hard():
    """SUCCESS 回执可以带 ingestionErrors(带警告的成功)—— field 锚不许判成政策拒。"""
    desc = ("This item has been unpublished for violating Walmart's Marketplace "
            "*Prohibited Product Policy: Offensive Content*.")
    assert et.classify_feed_error("X", "Defects Platform", desc, "failed").channel \
        == "policy"
    assert et.classify_feed_error("X", "Defects Platform", desc, "success").channel \
        == "operational"
    assert et.classify_feed_error("X", "Defects Platform", desc, None).channel \
        == "operational"


def test_feed_content_channel_keeps_the_code_anchor():
    """生产明细的 description 常被截断,AI 尾句可能不在截断内 —— code 锚必须留着。"""
    code = sorted(resources.WALMART_ERR_CONTENT)[0]
    res = et.classify_feed_error(code, "gtin", "内容被截断了看不到尾句", "failed")
    assert (res.channel, res.code, res.via_ai) == ("content_ai", "CONTENT", True)


def test_feed_policy_channel_falls_back_to_other_when_nothing_matches():
    """政策族里判不出码的正文归 OTHER —— 报告会把它们列出来补判据,不许静默。"""
    res = et.classify_feed_error("X", "RNA", "something nobody has a rule for",
                                 "failed")
    assert (res.channel, res.code) == ("policy", "OTHER")


# ── 纯函数纪律与只读红线 ──────────────────────────────────────────────────────

def test_the_engine_stays_a_pure_function_module():
    """零 DB、零环境变量、不碰 api/workflows(方案 §四;铁律 1 的方向也一样)。

    看的是 **import 语句**(docstring 里提消费方是应该的),外加两个取环境变量
    的写法 —— 政策字典由调用方注入,引擎自己一个外部输入都不许有。
    """
    src = pathlib.Path(et.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for mod in sorted(imported):
        assert not mod.startswith(("api", "workflows", "registry.db", "psycopg",
                                   "os")), f"引擎里不该 import {mod}"
    assert "os.environ" not in src and "getenv" not in src


def test_the_reclass_report_workflow_is_read_only():
    """对照报告只 SELECT:第一步不许碰任何行为(方案 §〇 红线)。"""
    from workflows import error_reclass_report as wf

    assert wf.DANGEROUS is False
    src = pathlib.Path(wf.__file__).read_text(encoding="utf-8")
    sql = " ".join(src.split()).upper()
    for banned in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "TRUNCATE"):
        assert banned not in sql, f"只读工作流里出现了 {banned!r}"
    # 连接只走 registry/db(铁律:数据库连接唯一入口)
    assert "from registry import db" in src or re.search(r"\bdb\.pg_conn\b", src)
    assert "psycopg.connect" not in src


def test_taxonomy_version_is_registered():
    """码表/判据变更时手动递增 —— 版本串是报告上唯一能对上"哪一版判的"的东西。"""
    assert re.fullmatch(r"t\.\d{4}-\d{2}-\d{2}\.\d+",
                        resources.ERROR_TAXONOMY_VERSION)


def test_extract_policy_keeps_the_longest_official_name_whole():
    """官方最长政策名 89 字符:旧正则上限 80 把它截成 "…(Cover";候选两端剥标点又把
    配对的尾 `)` 剥掉 —— 两处都让珠宝政策 join 不上(2026-09-02 真跑改名后首份报告
    14 条,§十一)。孤 `)`(残缺标记留下的)照旧剥。"""
    name = ("Jewelry, Watches, Precious Gemstones, Currency, Coins and Precious "
            "Metals (Covered Goods)")
    assert et.extract_policy(                       # synthetic
        f"Prohibited Products Policy: {name}. For more details, create a case."
    ) == (name, None)
    assert et.policy_join(name, [name]) == name
    assert et.extract_policy("Prohibited Products Policy: Alcohol).") == \
        ("Alcohol", None)                            # synthetic


def test_the_report_no_longer_carries_the_alias_health_line():
    """报告头的「别名表健康」两行随别名表一起退役(2026-09-03 C 批)。

    2026-09-02 真跑改名后的首份报告曾把 9 条已退役别名印成「静默失效」告警,
    误导读数的人去核对政策表命名 —— 现在那张表没了,那两行也就没有了。
    政策名对不上照旧在「政策表缺口」清单里显形,那才是它该待的地方。
    """
    import inspect

    from workflows import error_reclass_report as wf

    src = inspect.getsource(wf)
    assert "_alias_notes" not in src and "别名表" not in src
    assert "政策表缺口" in src


# ── 第四面:已经拉黑的那批 ASIN(所有者 2026-09-03 问的那件事)──────────────

def test_黑名单面_把无原文与站不住的行分开报():
    """⚠ 所有者原问:「禁售占了 4 万多个产品……这些产品的具体报错我们重新按新规
    归类了吗?」答案是**没有**(新引擎一个生产写入路径都没接),而这一面把账摆出来。

    三件事必须同时说清,少一件就会被误读成"可以批量翻案了":
      ① `reason` 为空的行**无法重判** —— 历史导入那批本来就不带原文,
         它多半是大头,不亮出来会让分母显得很小;
      ② 有原文的也只是**截 200 字符的样本**,判据串可能被切掉 ⇒ 给的是**下限**;
      ③ 新码认为"不是商品违禁"的行要单独点名,但**不等于授权翻案** ——
         黑名单是「一次入选、永久禁止」的既定语义,改不改是所有者的裁决。
    """
    from workflows import error_reclass_report as wf

    rows = [
        ("B", "prohibited product. Walmart's Prohibited Products Policy: Alcohol.", 120),
        # 旧码算永久禁售,新码认出病根是我方类目选错
        ("B", "may be a prohibited product. Please make sure the appropriate "
              "product type selected for this item.", 30),
        # 旧码 F(限类)也进永久黑名单,新码是 GATED:没资质 ≠ 商品违禁
        ("F", "This is a restricted category that requires pre-approval.", 9),
        ("LEGACY", "", 4100),          # 历史导入:无原文,重判不了
    ]
    txt = "\n".join(wf.blacklist_section(rows, ["Alcohol"], 20, True))
    assert "catalog.asin_blacklist" in txt
    assert "4100 条" in txt and "无法重判" in txt          # ①
    assert "下限" in txt and "200 字符" in txt              # ②
    assert "站不住的黑名单行:39 条" in txt                  # ③ = 30 + 9
    assert "不是自动翻案的授权" in txt
    assert "B → PT_WRONG" in txt and "F → GATED" in txt
    # 真·商品违禁那两条不许被点名
    assert "B → POLICY  ←" not in txt


def test_黑名单面_全是真违禁时不报那一段():
    """一条站不住的都没有 → 不出「站不住」那一段(别给读的人加噪声,
    也别让人以为报告坏了)。"""
    from workflows import error_reclass_report as wf

    txt = "\n".join(wf.blacklist_section(
        [("B", "Prohibited Products Policy: Alcohol.", 5)], ["Alcohol"], 20, True))
    assert "站不住的黑名单行" not in txt        # 那一段整段不出
    assert "不是自动翻案的授权" not in txt
    assert "B → POLICY" in txt


def test_换轨已落地_入选路径吃的是新码():
    """⚠ 2026-09-03 **换轨**:这条从"钉住现状"翻面成"钉住换轨结果"。

    换轨前它钉的是「新引擎只被报告与评估消费,入选路径仍吃 A-L 码」;
    所有者裁决后,入选那条路(problem_scan / feed_track → blacklist.record_asins)
    改吃新 16 码,`PERMANENT` 换成他逐码定的七个(裁决表 §十二)。

    修的是一个具体缺陷:旧 B(禁售)一个桶里混着 PT_WRONG —— 沃尔玛原话是
    「要重新上架请把 product type 选对」,是修法不是禁令,却被永久拉黑
    (存量实测 40,825 条)。**PT_WRONG 绝不许再回到 PERMANENT 里。**
    """
    from services import blacklist
    assert blacklist.PERMANENT == set(et.PERMANENT_CODES)
    assert "PT_WRONG" not in blacklist.PERMANENT
    assert blacklist.BRAND_CATEGORIES == {"BRAND", "IP"}
    # 旧引擎**已删**(2026-09-04 所有者定「旧码不需要留」):全仓归类只有
    # `error_taxonomy.classify_reasons` 一条路。`_RULES` 保留但只是「旧码 →
    # 中文名」的查表,给读历史数据的两处用 —— 读历史 ≠ 判据路径。
    from services import problem_products
    assert not hasattr(problem_products, "categorize")
    assert problem_products._RULES["B"][0] == "禁售"      # 查表还在


def test_回填不看事件里那个码_拿原文重判():
    """⚠ 所有者 2026-09-04:「删除旧码,我们已经迁移到新码,旧码不需要留。
    把口径做统一」。过渡桥(旧 A-L 码也算永久)已删 —— 它本身就是**把旧引擎
    的错重新引进来的通道**:旧码 `B` 是混装桶,生产实测 40,827 条 `PT_WRONG`
    混在里面,只有 3,512 条是真 `POLICY`。

    2026-09-04 查出的两条岔路(方向相反,都静默):
      · `backfill_from_events` 认新旧两套 ⇒ 把 blacklist_route 刚删的
        ~41,600 条灌回来,摘要显示「历史回填 +41,600」看着像正常干活;
      · `rebuild_asin_blacklist` 只认新码 ⇒ 擦净重灌**七万变几十**,同样不报错。
    现在两条都拿事件原文过 `classify_reasons`,`is_permanent` 定去留。
    """
    import inspect
    from services import blacklist
    # 旧码通道整个删干净
    for gone in ("backfill_codes", "brand_backfill_codes",
                 "_LEGACY_PERMANENT", "_LEGACY_BRAND_CATEGORIES",
                 "_label_case", "_BACKFILL_ASIN_SQL"):
        assert not hasattr(blacklist, gone), gone
    src = inspect.getsource(blacklist)
    assert '"B": "POLICY"' not in src        # 旧码字面量不许再出现
    # 两条路径必须都走同一个判据
    for fn in (blacklist.backfill_from_events, blacklist.rebuild_asin_blacklist,
               blacklist.backfill_counts):
        assert "_judge_events" in inspect.getsource(fn), fn.__name__
    judge = inspect.getsource(blacklist._judge_events)
    assert "worst_verdict" in judge
    assert "先判再擦" in inspect.getsource(blacklist.rebuild_asin_blacklist)


def test_不是商品违禁那一集只有一份():
    """⚠ 双轨禁止:报账的与回填的必须读同一份常量。

    工作流之间不许 import,所以口径住在 `services/error_taxonomy`;
    哪天有人在某个工作流里又抄一份字面量,这条会红。
    """
    from workflows import error_reclass, error_reclass_report
    assert error_reclass.NOT_A_PRODUCT_BAN is et.NOT_A_PRODUCT_BAN
    assert error_reclass_report._NOT_A_PRODUCT_BAN is et.NOT_A_PRODUCT_BAN
    # FLAGGED / OTHER 故意不在里面:不能反过来断言"不是违禁"
    assert "FLAGGED" not in et.NOT_A_PRODUCT_BAN
    assert "OTHER" not in et.NOT_A_PRODUCT_BAN
    # 每个码都得是码表里真有的
    for code in et.NOT_A_PRODUCT_BAN:
        assert code in resources.ERROR_CATEGORY_CODES, code


def test_取原文只有一处实现_四级优先():
    """⚠ 2026-09-04 生产实证:**判据统一 ≠ 口径统一**。同一段文本判成什么是
    确定的,但不同路径拿到的「那段文本」不一样,于是同一个 ASIN 判出相反的码:

      · walmart_items 全文,带句尾「To republish this item please make sure you
        have the appropriate product type selected.」→ PT_WRONG(修法不是禁令)
      · product_events 的 reason,句尾那句**不在**(`||…@@@…` 格式)→ POLICY

    后果:**3,037 个品**被 blacklist_route 正确删掉、又要被回填错误加回来,
    而两边摘要都显示正常。所以取原文与归类一样,只准有一处实现。
    """
    import inspect
    from services import error_source, blacklist
    from workflows import error_reclass
    # 两个消费方都转调 services/error_source,自己不再实现
    assert "error_source.pick" in inspect.getsource(error_reclass.pick_source)
    assert "error_source.fetch" in inspect.getsource(error_reclass._sources)
    # ⚠ `_judge_events` 2026-09-04 起**不走 error_source** —— 所有者定的判据是
    #   「看产品历史,够格拉黑的那条最高优先级」,原文直接从 product_events 取全部,
    #   不再是「取一条再四级补全」。四级优先仍服务 error_reclass 的存量复核。
    # SQL 只在 services 里出生
    src = inspect.getsource(error_reclass)
    for gone in ("_SQL_SRC_RECORDS", "_SQL_SRC_ITEMS", "raw_reason\nFROM"):
        assert gone not in src, gone
    # 优先序本身是判据:全文压过样本,四处都没有 → 不猜
    assert error_source.pick("A", "样本", "S", {"A": "全文"}, {"A": "事"},
                             {"S": "件"}) == ("全文", "records")
    assert error_source.pick("A", "样本", "S", {}, {"A": "事"},
                             {"S": "件"}) == ("事", "events")
    assert error_source.pick("A", "样本", "S", {}, {}, {"S": "件"}) == ("件", "items")
    assert error_source.pick("A", "样本", None, {}, {}, {}) == ("样本", "self")
    assert error_source.pick("A", "  ", None, {}, {}, {}) == ("", "none")


def test_同一个ASIN两条源判出相反的码_这就是那3037条():
    """把生产实测的两段原文钉成回归用例 —— 它们是 `services/error_source`
    存在的全部理由。以后谁把取原文那一步简化掉,这条会红。"""
    full = ("This item has been unpublished for violating Walmart's Marketplace "
            "*Prohibited Product Policy*.  To republish this item please make "
            "sure you have the appropriate product type selected for this item.")
    partial = ("This item has been unpublished for violating Walmart's Marketplace "
               "||Prohibited Product Policy@@@https://marketplacelearn.walmart.com"
               "/guides/Prohibited-products")
    assert et.classify_reasons(et.split_reasons(full)).code == "PT_WRONG"
    assert et.classify_reasons(et.split_reasons(partial)).code == "POLICY"
    # 一个该放、一个会被永久拉黑 —— 取错原文的代价就是这个
    assert et.is_permanent("POLICY", None) is True
    assert et.is_permanent("PT_WRONG", None) is False


def test_items那一级要按asin也索引一份_否则sku对不上就查不中():
    """⚠ 2026-09-04 生产实测:回填与路由的 2,261 条冲突里 **2,194 条(97%)**
    出在这一处 —— 同一个 ASIN 在多店有**多个 sku**,`_judge_events` 拿的是
    `product_events.sku`、`error_reclass` 拿的是 `asin_blacklist.src_sku`,
    两个对不上 ⇒ items 那一级查不中 ⇒ 退回残缺的事件 reason ⇒ 判成 POLICY
    而不是 PT_WRONG,于是把被正确释放的品又加回来。

    ⚠ 开关必须**显式**:它是一次全表扫,分批调用的消费方(`error_reclass` 有
    精确的 src_sku)不该付这个代价。
    """
    import inspect
    from services import error_source, blacklist
    from workflows import error_reclass
    sig = inspect.signature(error_source.fetch)
    assert sig.parameters["items_by_asin"].default is False   # 缺省不付代价
    assert "extract_asin" in inspect.getsource(error_source.items_by_asin_map)
    # ⚠ 消费方只剩 `error_reclass`(存量复核):那 14,474 条 self(残文)**有**
    #   src_sku,但那个 sku 在 walmart_items 里已经不在了(下架删除),照样查不中,
    #   所以要按 asin 兜底。它分批跑,故在**循环外**查一次、跨批复用。
    #   (`_judge_events` 已改成直接读产品历史,不再需要这一档。)
    #   ⚠ 2026-09-04 起这一次全表扫挪到 `run()`:事件遍与黑名单遍**共用同一份**
    #     (scope=all 时各扫一遍是白付两次代价)。
    run_src = inspect.getsource(error_reclass.run)
    assert "error_source.items_by_asin_map(conn)" in run_src
    for fn in (error_reclass._events_pass, error_reclass._blacklist_pass):
        pass_src = inspect.getsource(fn)
        assert "items_by_asin_map" not in pass_src, fn.__name__   # 不许每批扫
        # 按 sku 命中的优先(调用方给的 sku 更精确)
        assert "{**by_asin, **it}" in pass_src, fn.__name__
    assert "{**items_by_asin_map(conn), **items}" in inspect.getsource(error_source.fetch)


def test_多条事件取够格拉黑的那条_不是取最新():
    """⚠ 所有者 2026-09-04 定稿:「一个产品的报错可能存在多次,**其中被拉黑的
    那个作为最高优先级**,其他的都是作为记录」。

    这**推翻了**此前那条「最新类别命中才算,『曾命中过』不作数」——
    旧写法 `DISTINCT ON (asin) … ORDER BY occurred_at DESC` 只看最新一条,
    于是一个品上个月被判 POLICY(该永久拉黑)、这个月记录是 EXPIRED(过期),
    就**把历史上那条禁令忘了**,与「一次入选、永久禁止」的语义相反。

    ⚠ 且**只读码,不重判原文**(所有者:「产品级的记录已经有产品事件在做了」)——
    判定在 problem_scan 写事件那一刻发生过一次。
    """
    from services import blacklist
    # 够格拉黑的那条说了算 —— **不管它在不在最后**
    assert blacklist.worst_verdict(
        [["EXPIRED", None], ["POLICY", None], ["PT_WRONG", None]]) == ("POLICY", None)
    assert blacklist.worst_verdict(
        [["POLICY", None], ["EXPIRED", None]]) == ("POLICY", None)
    # 一条都不够格 → None(调用方据此不拉黑)
    assert blacklist.worst_verdict([["EXPIRED", None], ["PT_WRONG", None]]) is None
    # OTHER 是混装桶:只有两个显式词条算永久 —— 所以事件里必须存 term
    assert blacklist.worst_verdict([["OTHER", "business decision"]]) \
        == ("OTHER", "business decision")
    assert blacklist.worst_verdict([["OTHER", "currently under review"]]) is None
    # 空 / 缺项都不炸
    assert blacklist.worst_verdict([]) is None
    assert blacklist.worst_verdict(None) is None
    assert blacklist.worst_verdict([["POLICY"]]) == ("POLICY", None)


def test_pick的items那一级_sku与asin两个都要试():
    """⚠ 2026-09-04 实遇的**第二个** bug:`fetch(items_by_asin=True)` 按 asin
    补了索引,而 `pick` 只查 `items.get(src_sku)` —— **索引加了、查法没改**,
    补进来的 asin 键永远查不到,冲突数纹丝不动(2,261 → 2,263)。

    sku 更精确排前面;sku 失效(下架删除)或对不上时按 asin 兜底。
    """
    from services import error_source as es
    # sku 命中 → 用 sku 那份
    assert es.pick("B0X", None, "S-1", {}, {}, {"S-1": "按sku", "B0X": "按asin"}) \
        == ("按sku", "items")
    # sku 查不中 → 退到 asin,**而不是**掉到 self
    assert es.pick("B0X", "残文", "S-9", {}, {}, {"B0X": "按asin"}) \
        == ("按asin", "items")
    # 手上压根没有 sku 时也要试 asin
    assert es.pick("B0X", "残文", None, {}, {}, {"B0X": "按asin"}) \
        == ("按asin", "items")
    # 两个都没有才轮到自己那份残文
    assert es.pick("B0X", "残文", "S-9", {}, {}, {}) == ("残文", "self")


def test_复核出结论就把category改成新码_LEGACY与判不出的不动():
    """⚠ 所有者 2026-09-04:「旧 A-L 码入选然后按新码复核过,那么现在库里保留的
    应该就只有新码,没有旧码残留……不要做双轨,没有意义,以新规则统一」。

    两条不动:① `LEGACY`(历史继承,所有者说「保留原样没有问题」);
             ② 判不出的(code 为 NULL)—— 没结论就没有可写的东西。
    ⚠ **拦截行为仍然一个字没变**:上架闸拦的是「这个 asin 在不在表里」,
    `category` 只进提示文字;飞书「来源」列也不变(source_label 经 _NAMES
    把新码映射回旧中文标签)。
    """
    from workflows import error_reclass as wf
    from services import blacklist
    sql = wf._SQL_BL_SET
    assert "category = CASE WHEN category = 'LEGACY' OR %(code)s::text IS NULL" in sql
    assert "THEN category ELSE %(code)s::text END" in sql
    assert "source   = CASE WHEN category = 'LEGACY'" in sql   # 来源标签跟着走
    # ⚠ 2026-09-04 实遇:`IS NULL` 位的转型不能省 —— psycopg 把每个 %(code)s
    #   展开成独立占位符,那个位置没有列可以推类型,PG 报
    #   AmbiguousParameter: could not determine data type of parameter $1。
    #   赋值位由列推得出来,判断位推不出来。
    for frag in ("%(code)s::text IS NULL", "ELSE %(code)s::text",
                 "ELSE %(source)s::text"):
        assert frag in sql, frag
    assert "%(code)s IS NULL" not in sql        # 不许有裸的(会炸)
    # 飞书那一列的文字确实不变
    assert blacklist.source_label("POLICY") == "沃尔玛-禁售"
    assert blacklist.source_label("BRAND") == "沃尔玛-品牌"


def test_产品事件是产品级记录_下游只读码():
    """⚠ 所有者 2026-09-04:「产品级的记录已经有产品事件在做了。我们改了新归类,
    **产品事件跟随更新了吗?**」—— 换轨(2026-09-03)只改了写入侧,历史事件的
    `detail.category` 还是旧 A-L 码,全仓没有任何改它的代码。

    这才是根:此前是在**读的时候**一遍遍重判原文来绕开旧码,而正确的做法是
    **让账本本身是对的** —— 判定只在 `problem_scan` 发生一次,其余全是查询。
    """
    import inspect
    from services import blacklist
    from workflows import error_reclass
    # ① 有一条回填事件码的路(scope=events),且盖版本号能断点续跑
    assert "problem_categorized" in error_reclass._SQL_EV_PICK
    assert "detail->>'taxonomy_version' IS DISTINCT FROM %(ver)s" in \
        error_reclass._SQL_EV_PICK
    assert "jsonb_build_object('category'" in error_reclass._SQL_EV_SET
    assert "'taxonomy_term'" in error_reclass._SQL_EV_SET   # OTHER 判永久要它
    assert error_reclass._parse({"scope": "events"})[0] == "events"
    # ② 下游只读码,不再重判原文
    judge = inspect.getsource(blacklist._judge_events)
    assert "classify_reasons" not in judge and "worst_verdict" in judge
    sql = blacklist._HISTORY_SQL
    assert "detail->>'category'" in sql and "detail->>'taxonomy_term'" in sql
    assert "GROUP BY 1" in sql and "coalesce(asin, sku) AS asin" in sql
    assert "DISTINCT ON" not in sql        # 不许退回「只取最新一条」


def test_restore只接回被截掉的那段_不换成别的文本():
    """⚠ `error_source.restore` 的两条判据,2026-09-04 事故换来的(§17):

    ① 候选必须**以 own 为前缀** —— 是前缀就是同一段文本被切之前的样子;不是
       前缀就是**另一次报错**,拿它判这一格等于串账;
    ② `own` 必须够到 `SAMPLE_LEN`(200)—— 短于它的那份根本没被我们切过,
       此时"更长的候选"是另一段更长的文本(比如后来又追加了一条理由),
       接上去就等于拿后来的状态改写历史那一格。
    """
    from services import error_source as es
    full = ("This item has been unpublished for violating Walmart's Marketplace "
            "*Prohibited Product Policy*. Please review the policy documentation in "
            "the Seller Help Center for the complete list of restricted categories. "
            "To republish this item please make sure you have the appropriate "
            "product type selected for this item.")
    sample = full[:es.SAMPLE_LEN]
    assert len(sample) == 200
    # ① 是延长 → 接回来,并记下从哪一级还原的
    assert es.restore(sample, (("records", None), ("items", full))) == (full, "items")
    assert es.restore(sample, (("records", full), ("items", None))) == (full, "records")
    # ① 更长但**不是**延长 → 不能用
    assert es.restore(sample, (("items", "另一次报错" * 200),)) == (sample, "self")
    # ② own 没够到 200(没被切过)→ 一律不还原
    short = "Item is prohibited."
    assert es.restore(short, (("items", short + " Extra reason appended later."),)) \
        == (short, "self")
    # 候选与 own 一样长(就是同一份)→ 无事可做
    assert es.restore(sample, (("items", sample),)) == (sample, "self")
    # 空 / 缺项都不炸
    assert es.restore(None, (("items", full),)) == ("", "self")
    assert es.restore(sample, None) == (sample, "self")
    # 这就是那次事故的两个结果:全文 PT_WRONG(可放)/ 残文 POLICY(永久禁)
    assert et.classify_reasons(et.split_reasons(full)).code == "PT_WRONG"
    assert et.classify_reasons(et.split_reasons(sample)).code == "POLICY"


def test_对照报告要覆盖全文语料_且把残片分出去():
    """⚠ 所有者 2026-09-04:「没归类到的报错原文……如果它本来就是不规范的那种,
    那就不入库也可以」。要照这句话做判断,清单里就**只能有真的原文**。

    三个缺口(都在这次补上):
      ① `audit.walmart_error_records.raw_reason` 是 NOT NULL 的**全文**,却
         完全不在报告里 —— 而事件回填第三轮实测它是最大的还原来源(47,956 条);
      ② 事件那一面只读 `detail->>'reasons'`(复数),而写入方写的是
         `'reason'`(单数)⇒ 这一面长期近乎空转,摘要照样显示正常;
      ③ `asin_blacklist.reason` 是 200 字样本,它归不出类可能只是**我们自己
         切坏的**,混进"未识别"清单会让人误判成"沃尔玛写得不规范"。
    """
    import inspect
    from workflows import error_reclass_report as wf
    from services import error_source
    # ① 全文语料这一面在
    assert "audit.walmart_error_records" in wf._SQL_RECORDS
    assert "raw_reason" in wf._SQL_RECORDS
    src = inspect.getsource(wf.run)
    assert '"all", "records"' in src
    assert "walmart_error_records(raw_reason 全文)" in src
    # ② 键名两种都读
    assert "coalesce(detail->>'reasons', detail->>'reason')" in wf._SQL_EVENTS
    # ③ 残片单独一栏,不进"未识别"清单
    t = wf._Tally("t")
    cut = "x" * error_source.SAMPLE_LEN            # 正好 200 = 我们切的
    t.add(cut, 3, ())
    assert t.unknown == {} and sum(t.unknown_cut.values()) == 3
    t2 = wf._Tally("t2")
    t2.add("x" * (error_source.SAMPLE_LEN + 1), 5, ())   # 201 字 = 真原文
    assert sum(t2.unknown.values()) == 5 and t2.unknown_cut == {}
    # 报告文本里要把这层区别说清楚,不能只在代码里
    lines = "\n".join(wf._fmt_lists(t, 20, True))
    assert "残片" in lines and "不是沃尔玛写得不规范" in lines


def test_黑名单标签序按所有者给的_不是字典序():
    """⚠ 所有者 2026-09-04:「严重程度按这个:品牌 → 知产 → 禁售 → 不可申诉
    → 召回 → …」。

    一个 ASIN 被报错多次、多次都够格永久拉黑时,黑名单那一行只写得下一个理由、
    飞书「来源」列也只显示一个词。**原先取的是"第一个够格的"** —— 而那个数组
    来自 `array_agg(DISTINCT …)`,PG 的 DISTINCT 聚合会**排序**,于是取到的是
    **字典序**最靠前的(`BRAND` < `FLAGGED` < `GATED` < `IP` < `POLICY` <
    `PROHIBITED_FINAL` < `RECALL`),不是最严重的。

    ⚠ 只影响标签,**拉不拉黑不受影响**(任一够格即拉黑)。
    """
    from registry import resources
    from services import blacklist as bl
    order = resources.BLACKLIST_LABEL_ORDER
    # 所有者给定的头五个,逐字钉住
    assert order[:5] == ("BRAND", "IP", "POLICY", "PROHIBITED_FINAL", "RECALL")
    # 覆盖面 = 够格永久拉黑的全部(七个永久码 + OTHER 的显式词条),不多不少
    assert set(order) == set(et.PERMANENT_CODES) | {"OTHER"}
    assert len(order) == len(set(order))
    # 序真的起作用:字典序会选 BRAND/FLAGGED,严重度序选所有者要的那个
    assert bl.worst_verdict([["PROHIBITED_FINAL", None], ["BRAND", None]]) \
        == ("BRAND", None)                       # 所有者:品牌排第一
    assert bl.worst_verdict([["RECALL", None], ["IP", None], ["POLICY", None]]) \
        == ("IP", None)                          # 知产 > 禁售 > 召回
    assert bl.worst_verdict([["FLAGGED", None], ["POLICY", None]]) \
        == ("POLICY", None)                      # 字典序会选 FLAGGED,这里不许
    # OTHER 排最后:混装桶不该盖过有名有姓的码
    assert bl.worst_verdict([["OTHER", "business decision"], ["GATED", None]]) \
        == ("GATED", None)
    # 一个都不够格 → None(不拉黑),与序无关
    assert bl.worst_verdict([["EXPIRED", None], ["PT_WRONG", None]]) is None


def test_两个序是两个问题_不许互相套():
    """⚠ `BLACKLIST_LABEL_ORDER` 与 `ERROR_CATEGORY_SEVERITY` **故意不一样**,
    这不是双轨 —— 它们回答的是两个问题:

      · `ERROR_CATEGORY_SEVERITY` —— **一条报错原文**里同时写了几个问题,主码取哪个;
      · `BLACKLIST_LABEL_ORDER`   —— **一个产品的多条历史报错**都够格拉黑,标签取哪个。

    2026-09-04 实测:把所有者给黑名单的那个序套到主码序上,78 条语料里会变
    **1 条** —— 正是语料 #69 的 `PT_WRONG → POLICY`,等于推翻「类目选错是修法
    不是禁令」的裁决(那 4 万条误拉黑的病根)。这条测试把那一行钉死。
    """
    from registry import resources
    # 主码序里 PT_WRONG **必须**排在 POLICY 前面 —— 这一条就是那 4 万条的护栏
    sev = resources.ERROR_CATEGORY_SEVERITY
    assert sev.index("PT_WRONG") < sev.index("POLICY")
    # 两个序不是同一个对象、成员也不同(后者只收够格永久拉黑的)
    assert set(resources.BLACKLIST_LABEL_ORDER) < set(sev)
    # 语料 #69 那条两句话的原文,主码必须仍是 PT_WRONG(可放)
    row = REASONS[68]
    assert "appropriate product type selected" in row["text"]
    assert row["expect_code"] == "PT_WRONG"
    assert et.classify_reasons(et.split_reasons(row["text"])).code == "PT_WRONG"
    assert et.is_permanent("PT_WRONG", None) is False
