"""上架定价积木(listing L2;区间数值为所有者定稿 2026-08-07)。

规则:**落地价(亚马逊单价 + 运费)**落在哪个区间,乘该区间在限额表
(registry.RETIRE_LIMITS,按店铺分行)里的倍率得沃尔玛价;**区间重叠部分
向下兼容**(边界/重叠取低区间,如 30 美金用 FBA 0-30 的倍数)。
**价格不落任何区间 → 按 OUT_OF_BAND_MULTIPLIER(300%)定价**
(所有者定稿 2026-08-09,此前是淘汰)。

⚠ 定价输入是**落地价不是裸单价**(所有者定稿 2026-08-10):采购真正付的是
单价加运费。运费没采到(采集侧 N/A → NULL)一律**不定价**,不当 0——
当 0 定出来的价偏低,越贵的运费亏得越多,而两侧都不会报错。

区间定稿(表格中不可见,所有者口述定稿,勿改):
  FBA:区间1 = 0~30,区间2 = 30~75
  FBM:区间1 = 15~80,区间2 = 80~1000
"""

import logging

logger = logging.getLogger("services.pricing")

# 价格落在所有区间之外时的默认倍率(所有者定稿 2026-08-09:出界按 300% 定价,
# 不再淘汰)。它是**兜底口径不是配置项**——某个价格段要走别的倍率,
# 应该在限额表里给它开一段区间,而不是改这个常量。
OUT_OF_BAND_MULTIPLIER = 3.0

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


def landed_price(amz_price, shipping):
    """输入:亚马逊单价 + 运费 → 输出:落地价 = 单价 + 运费(算不出返回 None)。

    定价的输入是**落地价不是裸单价**(所有者定稿 2026-08-10)——采购真正要
    付的是单价加运费,漏掉运费等于按比成本低的数去乘倍率,越贵的运费亏得越多。
    (旧系统读的是采集侧导出的虚拟列「总价」,本来就是 price + shipping;
    新链路改吃增量导出的 fast 段后漏了这一项,此处补回。)

    ⚠ **运费 None 时返回 None,绝不当 0**(采集契约不变量 3b,与 stock_count
    同一条):`FREE → 0.0` 是"确认免运费"这条真信息,`N/A → null` 是"这次
    没采到"。当 0 的话定价照样算得出来、看着也正常,只是**偏低**,而两侧
    都不会报错——和"配送方式未知不定价"是同一个道理,宁可不改。
    """
    try:
        return float(amz_price) + float(shipping)
    except (TypeError, ValueError):
        return None


def walmart_price(channel: str, amz_price, multipliers: dict,
                  shipping) -> float | None:
    """输入:渠道 + amz 单价 + {字段名: 倍率原值}(该店限额表行)+ 运费
    → 输出:沃尔玛价(round2)或 None(倍率未配置/落地价算不出来)。

    新价 = **落地价(单价 + 运费)× 该区间倍率**;区间也按落地价选
    (运营给的区间说的是"这个货多少钱",那就是到手价,两处用同一个数才自洽)。

    **价格出界不再淘汰**(所有者定稿 2026-08-09):落在任何区间之外的一律按
    OUT_OF_BAND_MULTIPLIER(300%)定价。区间只是"运营给过明确倍率的价格段",
    出界不代表这个货不能卖。

    仍返 None 的两种:该区间在限额表里没配倍率(配置缺失,不该拿默认值蒙混)、
    落地价算不出来(单价或**运费**没采到——见 landed_price)。
    """
    p = landed_price(amz_price, shipping)
    if p is None:
        return None
    field = pick_band(channel, p)
    if field is None:
        # 出界:走默认倍率,不查表(表里本来就没有对应这段价格的列)
        return round(p * OUT_OF_BAND_MULTIPLIER, 2)
    mult = parse_multiplier(multipliers.get(field))
    if mult is None or mult <= 0:
        return None
    return round(p * mult, 2)
