"""上架定价积木(listing L2;区间数值为所有者定稿 2026-08-07)。

规则:amz 价格落在哪个区间,乘该区间在限额表(registry.RETIRE_LIMITS,
按店铺分行)里的倍率得沃尔玛价;**区间重叠部分向下兼容**(边界/重叠取
低区间,如 30 美金用 FBA 0-30 的倍数)。价格不落任何区间 → 不上架
(返回 None,调用方按"价格出界"淘汰)。

区间定稿(表格中不可见,所有者口述定稿,勿改):
  FBA:区间1 = 0~30,区间2 = 30~75
  FBM:区间1 = 15~80,区间2 = 80~1000
"""

import logging

logger = logging.getLogger("services.pricing")

# (下界, 上界, 限额表字段名);顺序即优先级——重叠/边界向下兼容取先命中的低区间
PRICE_BANDS = {
    "FBA": [(0, 30, "fba_range1"), (30, 75, "fba_range2")],
    "FBM": [(15, 80, "fbm_range1"), (80, 1000, "fbm_range2")],
}


def parse_multiplier(v) -> float | None:
    """输入:限额表倍率单元格值('275%'/'2.75'/2.75)→ 输出:倍数或 None。

    旧系统事故教训(2355 行全店误淘汰):飞书返回的是**格式化显示值**,
    '275%' 直接 float() 抛异常被静默跳过 → 全店判无倍率。此处显式处理
    百分号;解析失败返回 None 并告警,绝不静默。
    """
    s = str(v if v is not None else "").strip()
    if not s:
        return None
    try:
        if s.endswith("%"):
            return float(s[:-1]) / 100
        return float(s)
    except ValueError:
        logger.warning("倍率解析失败(原值=%r),该店该区间视为未配置", v)
        return None


def pick_band(channel: str, amz_price: float) -> str | None:
    """输入:渠道 FBA/FBM + amz 价格 → 输出:限额表字段名或 None(出界)。"""
    for low, high, field in PRICE_BANDS.get(channel, []):
        if low <= amz_price <= high:
            return field
    return None


def walmart_price(channel: str, amz_price, multipliers: dict) -> float | None:
    """输入:渠道 + amz 价格 + {字段名: 倍率原值}(该店限额表行)
    → 输出:沃尔玛价(round2)或 None(出界/倍率未配置)。"""
    try:
        p = float(amz_price)
    except (TypeError, ValueError):
        return None
    field = pick_band(channel, p)
    if field is None:
        return None
    mult = parse_multiplier(multipliers.get(field))
    if mult is None or mult <= 0:
        return None
    return round(p * mult, 2)
