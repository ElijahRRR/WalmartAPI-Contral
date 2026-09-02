"""政策名归一化与旧名翻译(services/policy_names)—— 仓内唯一那一份的守门。

这个模块自己不写库、不判定,但它是**四条链的共同判据**:policy_sync 拿它对行、
audit_l3 拿它把路由表对到政策表、audit_reason 拿它把 L3/规则给的政策名对回表内
拼写、error_taxonomy 拿它 join 报错正文。所以它错了不会有人报错,只会:

  · 归一化**太松** → 两条真不同的政策被当成同一条(policy_sync 那边是覆盖正文);
  · 归一化**太紧** → 路由条目/理由映射静默落空(判据悄悄变窄,提示词照旧漂亮)。

两条守门就是这两个方向:42 个官方名两两不撞(太松会红)、7 条旧缩写名改名前后
都 resolve 得到(太紧会红)。
"""

import inspect

import pytest

from registry import paths, resources
from services import policy_names as pn

# 官方 42 名 = `refdata/policy_pages/en/*.md` 的头注 H1(权威判据源,逐字取)
OFFICIAL = tuple(f.read_text(encoding="utf-8").split("\n", 1)[0][2:].strip()
                 for f in sorted(paths.policy_pages_dir("en").glob("*.md")))
# 改名落地前的生产表里,那 7 行长这样(所有者逐条裁决过的旧缩写名)
LEGACY = tuple(resources.POLICY_LEGACY_NAMES)


# ── 归一化:词形可以削,语义不许合 ─────────────────────────────────────────

def test_the_official_names_never_collide_after_normalization():
    """⚠ 42 个官方名两两不撞 —— 这是归一化敢削词形的**前提**。

    撞了的后果不报错:policy_sync 会把两个官方页认成表里同一行(A 政策的正文
    写进 B 行),audit 侧会把两条政策的理由认成一条。
    """
    assert len(OFFICIAL) == 42
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
    """⚠ 缩写差是**语义合并**,归一化一个字都不许沾 —— 认领它们的是
    `to_official`(所有者裁决过的落纸),不是词形规则。"""
    for legacy, official in resources.POLICY_LEGACY_NAMES.items():
        if pn.norm_category(legacy) == pn.norm_category(official):
            continue        # `&`↔`and` 那两条本来就是纯词形差,不算语义合并
        assert pn.norm_category(legacy) != pn.norm_category(official), legacy
    assert pn.norm_category("Drugs and Drug Paraphernalia") != \
        pn.norm_category("Drugs & Paraphernalia")
    assert pn.norm_category("Electronics and Radio Frequency Devices") != \
        pn.norm_category("Electronics & RF")


# ── 旧名翻译 ──────────────────────────────────────────────────────────────

def test_to_official_translates_only_the_adjudicated_literals():
    for legacy, official in resources.POLICY_LEGACY_NAMES.items():
        assert pn.to_official(legacy) == official
        assert official in OFFICIAL, f"{legacy} 的目标值不在 42 份转录件里"
    # 没登记的原样回(调用方要的是"翻译一下再试",不是"这是不是旧名")
    assert pn.to_official("Food Products") == "Food Products"
    assert pn.to_official("  Alcohol  ") == "Alcohol"
    assert pn.to_official(None) is None
    assert pn.to_official("") is None


# ── resolve:改名前后都要认得 ──────────────────────────────────────────────

def test_the_seven_legacy_names_resolve_across_the_rename():
    """⚠ 本模块存在的理由就是这一条:写死旧缩写名的地方(audit_l3 路由表、
    audit_l2 推出来的 walmart_policy)**不必**跟着改名批改字面量,也不会静默失效。

    改名后表里是官方名 → 解析到官方名;改名落地前表里还是旧名 → 解析到旧名。
    同一份代码,两种表形态都活。
    """
    for legacy, official in resources.POLICY_LEGACY_NAMES.items():
        assert pn.resolve(legacy, OFFICIAL) == official, legacy
        assert pn.resolve(legacy, LEGACY) == legacy, legacy
        # 官方名在官方表里当然也认得(改名后 L3 答出的就是它)
        assert pn.resolve(official, OFFICIAL) == official


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

def test_the_module_only_imports_re_and_registry():
    """⚠ 铁律 1:services 不许 import workflows。归一化搬到这里,正是为了让
    audit 侧用得上它 —— 反过来 import 回 workflows 就把依赖方向倒过来了。"""
    src = inspect.getsource(pn)
    # 只看**行首**的 import 语句(docstring 正文里会提到这条纪律本身)
    imports = [ln.rstrip() for ln in src.splitlines()
               if ln.startswith(("import ", "from "))]
    assert imports == ["import re", "from registry import resources"], imports
