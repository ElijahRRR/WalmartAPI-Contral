"""SKU → ASIN 清洗规则(**唯一出处**,Python 单实现,批量清洗也走这里)。

沃尔玛侧 sku 是订货号,与产品源头侧 asin **不保证相等**(所有者给样
2026-08-11,推翻了"sku=asin"的全局约定;跟卖行早已是已知例外):

  JTZW-D01027HVK3W-38     「前缀-源头码-价格」三段式,中段即源头码
  XKJ-B0GXX75JN5-39.98      (价格可带小数;源头码 8~14 位、字母开头)
  YP-B09TDMGVRW-188.88
  B0ABCDEFGH              裸 ASIN(10 位含字母)
  102460018738            纯数字 = walmart item id,模式提不出源头码,
                          须倒查 catalog.walmart_items(item_id → 订货号
                          → 再提取);查不到就保持原文

原则:提得出就提,提不出**保留原文并可报告**——绝不猜。规则不全是常态
(所有者:偶尔清洗一次),新形态先进「其他」桶报出来,人认了再扩规则。
"""

import re

# 裸 ASIN:10 位大写字母数字、至少含一个字母(纯数字 10 位是 item id 不是 ASIN)
_PLAIN = re.compile(r"^(?=.*[A-Z])[A-Z0-9]{10}$")
# 三段式:前缀(字母开头 ≤8 位,**可含数字**——2026-08-11 生产实证
# A109-B08QF9XLMH-02 这类 208 个被纯字母版规则冤枉)- 源头码(字母开头
# 8~14 位)- 价格(可小数,可前导零)
_WRAPPED = re.compile(r"^[A-Z][A-Z0-9]{0,7}-([A-Z][A-Z0-9]{7,13})-\d+(?:\.\d+)?$")
_NUMERIC = re.compile(r"^\d{6,}$")


def extract_asin(sku) -> str | None:
    """输入:沃尔玛侧 sku → 输出:源头 asin(提不出返 None,调用方决定兜底)。"""
    s = str(sku or "").strip().upper()
    if _PLAIN.fullmatch(s):
        return s
    m = _WRAPPED.fullmatch(s)
    return m.group(1) if m else None


def is_standard_asin(v) -> bool:
    """输入:候选码 → 输出:是否标准 ASIN(10 位含字母)。推送采集前的
    过滤闸:非标准码(纯数字 item id / 11 位源头码 / 原文兜底)推去采集
    只会永远采不到 → 永远缺品牌 → 永远再推,无限循环。"""
    return bool(_PLAIN.fullmatch(str(v or "").strip().upper()))


def classify(sku) -> str:
    """输入:sku → 输出:形态桶 'asin'/'wrapped'/'numeric'/'other'(清洗预览用)。"""
    s = str(sku or "").strip().upper()
    if _PLAIN.fullmatch(s):
        return "asin"
    if _WRAPPED.fullmatch(s):
        return "wrapped"
    if _NUMERIC.fullmatch(s):
        return "numeric"
    return "other"
