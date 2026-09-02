"""报错归类引擎(services/error_taxonomy)的语料回归与守门(2026-09-01 建)。

方案定稿 `docs/error_taxonomy.md`;本文件是它的**机器可验形式**,三段:

  ① 语料逐行断言 —— `tests/fixtures/reason_corpus.jsonl`(77 行)与
     `feed_error_corpus.jsonl`(20 行),原文全部取自生产实查(前 70 行
     2026-08-31 全量实查;#71-#77 是 2026-09-01 首轮对照报告的 unknown 清单
     补收,`provenance:"prod-2026-09-01-report"`,标 `truncated` 的是报告展示
     截断、判据只用可见段),**一行不许跳**。语料是验收标准:引擎迁就语料,
     不是语料迁就引擎。
  ② 旧行为快照 —— 同一批 reason 语料跑现行 `problem_products.categorize()`,
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
# 类别名 = 全链唯一键)。`policy_sync` 真跑后生产表就长这样:官方 42 名,
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
)

# 过渡态:`policy_sync` 改名落地**之前**的生产表长这样(存量缩写名那一族)。
# 只用于证明"改名前后 join 都不断"—— 别拿它当真值。
LEGACY_POLICIES = tuple(resources.POLICY_LEGACY_NAMES)


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
    assert len(REASONS) == 77 and len(FEED_ERRORS) == 20


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
_OLD_SNAPSHOT = {
    1: ("A", "过期"), 2: ("J", "特殊"), 3: ("B", "禁售"), 4: ("Z", "其他"),
    5: ("D", "价格"), 6: ("B", "禁售"), 7: ("Z", "其他"), 8: ("B", "禁售"),
    9: ("Z", "其他"), 10: ("E", "知产"), 11: ("F", "限类"), 12: ("E", "知产"),
    13: ("H", "信息"), 14: ("B", "禁售"), 15: ("B", "禁售"), 16: ("F", "限类"),
    17: ("B", "禁售"), 18: ("Z", "其他"), 19: ("B", "禁售"), 20: ("B", "禁售"),
    21: ("B", "禁售"), 22: ("B", "禁售"), 23: ("Z", "其他"), 24: ("H", "信息"),
    25: ("B", "禁售"), 26: ("B", "禁售"), 27: ("B", "禁售"), 28: ("E", "知产"),
    29: ("G", "药品"), 30: ("F", "限类"), 31: ("B", "禁售"), 32: ("B", "禁售"),
    33: ("B", "禁售"), 34: ("I", "内容"), 35: ("B", "禁售"), 36: ("F", "限类"),
    37: ("Z", "其他"), 38: ("Z", "其他"), 39: ("B", "禁售"), 40: ("Z", "其他"),
    41: ("B", "禁售"), 42: ("B", "禁售"), 43: ("B", "禁售"), 44: ("B", "禁售"),
    45: ("B", "禁售"), 46: ("B", "禁售"), 47: ("K", "审查"), 48: ("L", "系统"),
    49: ("E", "知产"), 50: ("Z", "其他"), 51: ("Z", "其他"), 52: ("B", "禁售"),
    53: ("Z", "其他"), 54: ("Z", "其他"), 55: ("B", "禁售"), 56: ("B", "禁售"),
    57: ("B", "禁售"), 58: ("I", "内容"), 59: ("I", "内容"), 60: ("J", "特殊"),
    61: ("D", "价格"), 62: ("B", "禁售"), 63: ("A", "过期"), 64: ("J", "特殊"),
    65: ("J", "特殊"), 66: ("J", "特殊"), 67: ("D", "价格"), 68: ("A", "过期"),
    69: ("B", "禁售"), 70: ("J", "特殊"),
    # #71-#77:2026-09-01 首轮对照报告补收的 7 种文本(轮次二)。这一批的看点
    # 与前 70 行相反 —— **旧引擎判得出、新引擎当时漏了**(在架面 unknown 316 条
    # 就是它们),补完判据后新旧同指一处;只有 #77 两边都落杂项(旧 Z / 新
    # OTHER 显式清单)。⚠ #77 旧码是 **Z 其他**不是 K:旧 K 的判据是
    # `flagged by our internal team`(problem_products._RULES),与"审查中"无关。
    71: ("I", "内容"), 72: ("C", "品牌"), 73: ("I", "内容"), 74: ("C", "品牌"),
    75: ("I", "内容"), 76: ("H", "信息"), 77: ("Z", "其他"),
}


@pytest.mark.parametrize("lineno", sorted(_OLD_SNAPSHOT),
                         ids=[f"L{i}" for i in sorted(_OLD_SNAPSHOT)])
def test_old_engine_snapshot(lineno):
    """现行生产归类器的输出快照 —— 第一步不改它,变了就是有人动了生产判定。"""
    text = REASONS[lineno - 1]["text"]
    assert problem_products.categorize(text) == _OLD_SNAPSHOT[lineno]


def test_snapshot_covers_every_corpus_line():
    assert sorted(_OLD_SNAPSHOT) == list(range(1, len(REASONS) + 1))


def test_the_two_engines_really_do_disagree():
    """快照的意义在于差异:新引擎不是把旧码改了个名字。

    钉住方案要所有者看见的那一类:中性码(过期/未上线)盖住的真问题必须翻出来。
    """
    flipped = 0
    for i, row in enumerate(REASONS, 1):
        old = _OLD_SNAPSHOT[i][0]
        new = et.classify_reasons(et.split_reasons(row["text"])).code
        if old in ("A", "J", "Z") and new not in ("EXPIRED", "STAGE", "OTHER"):
            flipped += 1
    assert flipped >= 10, f"只翻出 {flipped} 条,对照报告就没什么可看的了"


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
    assert len(heads) == 42
    assert set(KNOWN_POLICIES) == set(heads)
    assert len(set(KNOWN_POLICIES)) == 42


def test_the_alias_table_is_derived_not_hand_written():
    """⚠ 别名表是从 `registry.resources.POLICY_LEGACY_NAMES` **派生**的(仓内唯一一份
    旧名↔官方名映射)。手抄第二份的后果不报错:所有者往映射表里追加一条,
    这边不知道,那条别名就静默不存在。"""
    assert et.POLICY_ALIASES, "别名表空了说明有人删光了词形差映射"
    assert set(et.POLICY_ALIASES.values()) == set(resources.POLICY_LEGACY_NAMES)
    # 键过的是**归一化那一份实现**(services/policy_names),不是这里手抄的公式 ——
    # 抄一份进测试,归一化改了测试还绿,那就等于没守门
    assert et.POLICY_ALIASES == {policy_names.norm_category(v): k
                                 for k, v in resources.POLICY_LEGACY_NAMES.items()}
    assert len(et.POLICY_ALIASES) == len(resources.POLICY_LEGACY_NAMES)  # 键不撞


def test_alias_gaps_go_from_empty_to_mostly_gone_when_the_rename_lands():
    """别名的目标值(表内旧名)指不到表 = 那条别名失效 —— 但**有两种读法**。

    改名前(过渡态)11 条必须条条指得到,指不到就是映射表写错了;
    改名后(目标态)大部分指不到 —— 那不是故障,是这张别名表功成身退的信号
    (此时直接键已命中),该做的是随第三步 L3 批把它整体删掉。

    ⚠ 改名后**不是 11 条全指不到,是 9 条**:`Auto & Motor Vehicles` 与
    `Textiles & Apparel` 两条旧名与官方名只差 `&`↔`and`,而 `_norm_key`
    2026-09-02 起就是 `policy_names.norm_category`(那四条词形规则里正好有它)
    —— 归一化后旧名与官方名同键,于是"指得到表",别名本身也已多余。
    剩下 9 条是**真的语义缩写**(`Electronics & RF` ↔ `Electronics and Radio
    Frequency Devices` 那种;2026-09-02 首跑又补了 Jewelry/Pet/Restricted/
    Biodegradable 四条),归一化永远打不平,只能靠映射表。
    """
    assert et.alias_gaps(LEGACY_POLICIES) == ()
    assert et.alias_gaps(KNOWN_POLICIES) == (
        "Biodegradable Plastic", "Drugs & Paraphernalia", "Electronics & RF",
        "Jewelry/Precious Metals", "Military & Law Enforcement", "Pet Products",
        "Restricted/Illegal", "Ride-Ons & Micromobility", "Tobacco & Vaping")
    # 掉出清单的那两条是"归一化已经够用",不是别名丢了
    still_mapped = set(resources.POLICY_LEGACY_NAMES) - set(
        et.alias_gaps(KNOWN_POLICIES))
    assert still_mapped == {"Auto & Motor Vehicles", "Textiles & Apparel"}
    for legacy in still_mapped:
        official = resources.POLICY_LEGACY_NAMES[legacy]
        assert et.policy_join(official, KNOWN_POLICIES) == official


def test_policy_join_uses_aliases_but_never_rewrites_the_extracted_name():
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


def test_the_derived_alias_still_joins_while_the_table_is_still_abbreviated():
    """⚠ 过渡态守门:生产表**还没改名**时(存量缩写名),报错正文里的官方全称
    经派生别名照旧对得上 —— 别名表要活到改名落地那一刻,不是提前退休。"""
    for legacy, official in resources.POLICY_LEGACY_NAMES.items():
        assert et.policy_join(official, LEGACY_POLICIES) == legacy, official


# 改名**落地之前**的生产表(37 行)在仓内的唯一近似:`services/audit_l3` 政策
# 路由表 + 旧 `audit_reason._L3_NORMALIZE` 的目标值,30 条,逐字取自本文件
# 2026-09-01 版的 KNOWN_POLICIES。留着它是为了证明"放宽归一化不是拿今天换明天"
# —— 两种形态下 join 都不能比旧手写实现差。**不是真值**,别拿它做别的断言。
_TODAY_TABLE = (
    "Alcohol", "Animals", "Art", "Auto & Motor Vehicles", "Baby Products",
    "Children's Products", "Content Standards", "Cosmetic Products",
    "Dietary Supplements", "Digital Goods", "Drugs & Paraphernalia",
    "Electronics & RF", "Food Products", "General-Use Products",
    "Hazardous Items", "Home Goods", "Intellectual Property",
    "Jewelry/Precious Metals", "Medical Devices", "Medical Foods",
    "Military & Law Enforcement", "Offensive Content", "PFAS Chemicals",
    "Pet Products", "Plants & Seeds", "Recalled Products",
    "Ride-Ons & Micromobility", "Software", "Textiles & Apparel",
    "Tobacco & Vaping",
)

# 旧手写 `_norm_key`(折叠空白 + casefold + 弯引号归直)在语料上的实测命中数,
# 2026-09-02 归并前跑出来的。新实现**只许升不许降**:归一化放宽的理由就是它。
_BASELINE_JOINS = {"today": 15, "official": 16}


def test_widening_the_join_key_never_loses_ground_on_the_corpus():
    """⚠ `_norm_key` 2026-09-02 放宽到 `policy_names.norm_category`(同一份实现)。

    放宽是判定面之外的事(`policy_join` 只喂报告,`policy_name` 一律保留原文),
    但"放宽"这种改动天然可疑:它可能在补上一处缺口的同时悄悄丢掉另一处。
    所以这条守门量的是**两种表形态下的命中数**,与旧手写实现的实测值比:

      · 「今天的表」= 改名落地前的 30 行近似(仓内唯一的那份);
      · 「官方 42 名」= `policy_sync` 真跑后的目标态。

    旧实现 15/19 与 16/19;新实现两边都必须 ≥,且改名后应当**一条不剩**——
    `Plants & Seeds`(& vs and)、牛津逗号 Tobacco、不带 `(Covered Goods)` 的
    Jewelry 这三种报错写法,正是归并前白白进"政策表缺口"清单的那些。
    """
    wanted = sorted({r["expect_policy"] for r in REASONS if r.get("expect_policy")})
    assert len(wanted) == 19
    today = [v for v in wanted if et.policy_join(v, _TODAY_TABLE)]
    official = [v for v in wanted if et.policy_join(v, KNOWN_POLICIES)]
    assert len(today) >= _BASELINE_JOINS["today"], sorted(set(wanted) - set(today))
    assert len(official) >= _BASELINE_JOINS["official"]
    # 改名落地后一条都不该剩(剩下的会进对照报告的「政策表缺口」清单)
    assert sorted(set(wanted) - set(official)) == []
    # 改名前仍差两条 —— 武器族**表里真的没有**,是政策表的缺口,不是 join 的锅;
    # 改名批补齐武器族之后自动消失(Jewelry 那条 2026-09-02 进映射表后已能 join)
    assert sorted(set(wanted) - set(today)) == [
        "Firearm Accessories",
        "Knives and other Melee Weapons",
    ]


def test_the_join_key_still_refuses_to_merge_two_different_policies():
    """⚠ 放宽的边界:词形可以削,语义不许合 —— 42 个官方名两两不撞
    (`policy_names` 那份实现自带守门,这里从**报告侧**再证一次:
    任意两个官方名之间不许 join 到对方)。"""
    keys = {et._norm_key(n) for n in KNOWN_POLICIES}
    assert len(keys) == len(KNOWN_POLICIES)
    for name in KNOWN_POLICIES:
        assert et.policy_join(name, KNOWN_POLICIES) == name
    # 缩写差照旧不许自己合并(那是 POLICY_LEGACY_NAMES 的活)
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
