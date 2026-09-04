"""政策名归一化(services/policy_names)—— 仓内唯一那一份的守门。

这个模块自己不写库、不判定,但它是**三条链的共同判据**:policy_sync 拿它对行、
audit 侧拿它把 L3/规则给的政策名对回表内拼写、error_taxonomy 拿它 join 报错
正文。所以它错了不会有人报错,只会:

  · 归一化**太松** → 两条真不同的政策被当成同一条(policy_sync 那边是覆盖正文);
  · 归一化**太紧** → 类别/join 静默落空(判据悄悄变窄,报告照旧漂亮)。

两条守门就是这两个方向:44 个官方名两两不撞(太松会红)、实证词形差全部打平
(太紧会红)。
⚠ 2026-09-03 C 批删掉了第 4 级 `to_official`(旧缩写名认领):改名已落地,
表内就是官方拼写;语义缩写从此**解析不到就是解析不到**(见文件末尾那条守门)。
"""

import inspect

import pytest

from registry import paths, resources
from services import policy_names as pn

# 官方 44 名 = `refdata/policy_pages/en/*.md` 的头注 H1(权威判据源,逐字取)
OFFICIAL = tuple(f.read_text(encoding="utf-8").split("\n", 1)[0][2:].strip()
                 for f in sorted(paths.policy_pages_dir("en").glob("*.md")))
# 2026-09-02 改名落地**之前**生产表里那一族旧缩写名(历史事实,不是当前状态):
# 留着它们是为了钉"这类语义缩写今天解析不到"—— 它们已经不在任何代码里。
LEGACY_ABBREVIATIONS = (
    ("Drugs & Paraphernalia", "Drugs and Drug Paraphernalia"),
    ("Electronics & RF", "Electronics and Radio Frequency Devices"),
    ("Military & Law Enforcement", "Military and Law Enforcement Products"),
    ("Ride-Ons & Micromobility", "Ride-Ons and Micromobility Devices"),
    ("Pet Products", "Pet Foods, Supplements, Medicines and Other Products"),
    ("Restricted/Illegal", "Restricted/Illegal Products"),
)


# ── 归一化:词形可以削,语义不许合 ─────────────────────────────────────────

def test_the_official_names_never_collide_after_normalization():
    """⚠ 44 个官方名两两不撞 —— 这是归一化敢削词形的**前提**。

    撞了的后果不报错:policy_sync 会把两个官方页认成表里同一行(A 政策的正文
    写进 B 行),audit 侧会把两条政策的理由认成一条。
    """
    assert len(OFFICIAL) == 44
    keys = [pn.norm_category(c) for c in OFFICIAL]
    assert len(set(keys)) == len(keys), \
        [k for k in keys if keys.count(k) > 1]
    assert all(keys), "有官方名归一化成空串"


@pytest.mark.parametrize("a,b", [
    ("Cosmetic Products", "Cosmetics Products"),          # 词尾单复数
    ("Plants and Seeds", "Plants & Seeds"),               # &↔and
    ("Tobacco, E-Cigarettes and Vaping Products",         # 牛津逗号
     "Tobacco, E-Cigarettes, and Vaping Products"),
    ("Knives and Other Melee Weapons", "Knives and other Melee Weapons"),
    ("Jewelry, Watches, Precious Gemstones, Currency, Coins and Precious "
     "Metals (Covered Goods)",
     "Jewelry, Watches, Precious Gemstones, Currency, Coins and Precious Metals"),
    ("Children’s Products", "Children's Products"),       # 弯撇号 ↔ 直撇号
    ("Children's Products", "Childrens Products"),
])
def test_the_word_form_gaps_all_fold_together(a, b):
    """§〇 逐条实证的词形差:削不平就是几行白白新增 / 几条路由白白落空。"""
    assert pn.norm_category(a) == pn.norm_category(b)


def test_normalization_still_refuses_to_merge_abbreviations():
    """⚠ 缩写差是**语义合并**,归一化一个字都不许沾 —— 那要人裁决
    (policy_sync 报告的「疑似改名对」),不是词形规则。"""
    for legacy, official in LEGACY_ABBREVIATIONS:
        assert pn.norm_category(legacy) != pn.norm_category(official), legacy


# ── 旧名翻译:已退役 ──────────────────────────────────────────────────────

def test_the_legacy_name_translation_is_retired():
    """⚠ 2026-09-03 C 批:`to_official` 与它查的
    `registry.resources.POLICY_LEGACY_NAMES` 一起删除。

    它是 2026-09-02 那一次改名的**过渡桥**:表内还是旧缩写名时,写死官方名的
    代码靠它对得上。改名真跑落地后,桥的两头连的是同一个地方 —— 留着 = 一份
    永远不会再被验证的历史映射,还多给一条"归一化认不出就翻译一下再试"的暗道。
    """
    assert not hasattr(pn, "to_official")
    assert not hasattr(resources, "POLICY_LEGACY_NAMES")
    from services import error_taxonomy
    assert not hasattr(error_taxonomy, "POLICY_ALIASES")
    assert not hasattr(error_taxonomy, "alias_gaps")
    assert pn.__all__ == ["norm_category", "resolve"]


# ── resolve:三级,认不出就说不认识 ───────────────────────────────────────

def test_semantic_abbreviations_no_longer_resolve_and_that_is_the_answer():
    """⚠ C 批之后 `resolve` 只有三级(精确 / casefold / 词形键)。

    语义缩写(`Electronics & RF` ↔ `Electronics and Radio Frequency Devices`)
    **解析不到就是解析不到** —— 那是正确答案:它要人来裁决是改名还是新增,
    不是代码替它选一个(选错 = 把 A 政策的结论挂到 B 政策名下,没人会红)。
    纯词形差(`&`↔`and`)照旧打得平,那一级没动。
    """
    for legacy, official in LEGACY_ABBREVIATIONS:
        assert pn.resolve(legacy, OFFICIAL) is None, legacy
        assert official in OFFICIAL, official
        assert pn.resolve(official, OFFICIAL) == official
    # 纯词形差不受影响:这两条 2026-09-02 之前也在旧名表里,靠的却是词形那一级
    assert pn.resolve("Auto & Motor Vehicles", OFFICIAL) == \
        "Auto and Motor Vehicles"
    assert pn.resolve("Textiles & Apparel", OFFICIAL) == "Textiles and Apparel"


def test_resolve_returns_the_spelling_that_is_in_the_table():
    """命中回的是**表里那一个原串**,不是入参、也不是归一化键。"""
    assert pn.resolve("children's products", OFFICIAL) == "Children’s Products"
    assert pn.resolve("PLANTS & SEEDS", OFFICIAL) == "Plants and Seeds"
    assert pn.resolve("  food products  ", OFFICIAL) == "Food Products"
    assert pn.resolve("Cosmetics Products", OFFICIAL) == "Cosmetic Products"


def test_resolve_says_no_instead_of_inventing_a_name():
    """认不出来给 None —— 编一个表里没有的名字,下游一路都对不上还不报错。"""
    assert pn.resolve("Weapons", OFFICIAL) is None
    assert pn.resolve("Pet Supplies (old)", OFFICIAL) is None   # 映射表里也没有
    assert pn.resolve("Jewelry/Gems", OFFICIAL) is None
    assert pn.resolve(None, OFFICIAL) is None
    assert pn.resolve("Alcohol", ()) is None
    assert pn.resolve("Alcohol", None) is None


def test_resolve_is_deterministic_when_the_table_has_near_duplicates():
    """⚠ 表是反推表:真出现只差大小写/词形的两行时,答案必须**定序** ——
    不定序的漂移不会报错,只会让同一条产品在两次进程里挂到不同的政策名。"""
    table = frozenset({"Alcohol", "ALCOHOL", "alcohol"})
    got = {pn.resolve("Alcohols", table) for _ in range(20)}      # 词形对上三行
    assert len(got) == 1
    assert got.pop() == sorted(table)[0]
    # 精确同名永远优先于同名不同壳
    assert pn.resolve("ALCOHOL", table) == "ALCOHOL"


# ── 分层纪律 ──────────────────────────────────────────────────────────────

def test_the_module_only_imports_re():
    """⚠ 铁律 1:services 不许 import workflows。归一化搬到这里,正是为了让
    audit 侧用得上它 —— 反过来 import 回 workflows 就把依赖方向倒过来了。
    (旧名表退役后连 registry 都不需要了:这个模块现在是纯字符串规则。)"""
    src = inspect.getsource(pn)
    # 只看**行首**的 import 语句(docstring 正文里会提到这条纪律本身)
    imports = [ln.rstrip() for ln in src.splitlines()
               if ln.startswith(("import ", "from "))]
    assert imports == ["import re"], imports
