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
  - 占位符表 = phase0 的 20 项 **∪ mp_mapper._BRAND_NOISE**(后者独有
    `unknown` 一词——两表各漏对方几个词,占用键取并集才安全:漏一个词
    就是一次大面积误锁,多一个词只是少占一个品牌)。

品牌缺失时的兜底顺序(与风控闸 `risk_gate.check` 双字段实证同源):
brand → manufacturer;两者都是占位符 = 真·无品牌,不占品牌只占产品。
"""

PLACEHOLDERS = frozenset({
    # 20 项逐字取自 services/audit_phase0._NON_BRAND_PLACEHOLDERS
    "n/a", "na", "n.a.", "n.a",
    "none", "null", "nil",
    "unbranded", "no brand", "no brand name", "no name",
    "generic", "oem", "various",
    "不详", "无品牌", "无",
    "-", "--", "---",
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
