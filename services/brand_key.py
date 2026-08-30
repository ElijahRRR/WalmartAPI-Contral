"""品牌归一化(分配占用的唯一出处)。

为什么单独一个模块:品牌是**排他占用的键**(一个品牌只能属于一家店),
键算错的两种后果不对称——
  - 该合的没合(同品牌算成两个键)→ 同品牌散落多店,排他失效;
  - 不该合的合了(占位符当品牌)→ "OEM"/"Generic" 这类词把成千上万个
    互不相干的产品锁进同一家店,一次误锁没有自动释放(§二铁律)。
所以本模块选**保守方向**:占位符一律判为"无品牌",宁可逐 ASIN 分散,
也不制造大面积误锁。

算法与噪声表的来源(两处都是既有实证,不自创):
  - 归一化 = `" ".join(raw.lower().split())`,与 `services/audit_phase0.
    _normalize_brand` 逐字同款——黑名单键就是这么规整的,占用键与黑名单
    键必须同算法,否则"品牌被拉黑"和"品牌被占用"对不上同一个字符串;
  - 占位符表 = phase0 白名单 **∪ mp_mapper._BRAND_NOISE ∪ {generic, oem,
    various}**(mp_mapper 独有 `unknown`——两表各漏对方几个词,占用键取并集
    才安全:漏一个词就是一次大面积误锁,多一个词只是少占一个品牌)。
    ⚠ **本表与 phase0 白名单已于 2026-08-23 分家,不再是"逐字同款"**:
    所有者把 generic / oem / various 从 phase0 撤下交还黑名单裁决(飞书登记
    了 GENERIC 却拦不住),但**这三个词在本表必须留着** —— 两张表方向相反,
    那边多留一个词只是少拦一个黑名单品牌,本表少留一个词就是"Generic"变成
    排他占用键、成千上万个无关产品锁进一家店,且占用没有自动释放。
    `tests/test_alloc_audit` 用子集断言钉住方向:phase0 ⊆ 本表,反向不成立。

品牌缺失时的兜底顺序(与风控闸 `risk_gate.check` 双字段实证同源):
brand → manufacturer;两者都是占位符 = 真·无品牌,不占品牌只占产品。
"""

PLACEHOLDERS = frozenset({
    # 17 项与 services/audit_phase0._NON_BRAND_PLACEHOLDERS 同源
    "n/a", "na", "n.a.", "n.a",
    "none", "null", "nil",
    "unbranded", "no brand", "no brand name", "no name",
    "不详", "无品牌", "无",
    "-", "--", "---",
    # phase0 已于 2026-08-23 撤下这三项(交还品牌黑名单裁决),**本表保留**:
    # 撤了就是把 Generic/OEM 当成排他占用键用,一次误锁没有自动释放
    "generic", "oem", "various",
    # mp_mapper._BRAND_NOISE 独有(2026-08-15 测试发现两表互有缺项)
    "unknown",
})


def normalize(raw) -> str:
    """输入:品牌原文 → 输出:归一键(小写 + 内部空白压单空格 + 两端去空白)。

    不去标点、不做 unicode 折叠:"L'Oréal" → "l'oréal"(与黑名单侧同算法)。
    """
    return " ".join(str(raw or "").lower().split())


def is_placeholder(raw) -> bool:
    """输入:品牌原文 → 输出:是否"没标品牌"的占位符(空串也算)。"""
    return normalize(raw) in PLACEHOLDERS or not normalize(raw)


def brand_key(brand, manufacturer=None) -> str | None:
    """输入:brand(+ manufacturer 兜底)→ 输出:占用键;真·无品牌返回 None。

    None 的语义是"这个产品不参与品牌排他",调用方按逐 ASIN 处理——
    **不要**把 None 当成一个叫 None 的品牌去占用。
    """
    for v in (brand, manufacturer):
        if not is_placeholder(v):
            return normalize(v)
    return None
