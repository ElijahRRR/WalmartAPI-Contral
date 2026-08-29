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
from collections import Counter

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


# ── 批量清洗:模式提取 + 纯数字倒查两跳(两个清洗工作流共用)──────────────
# 纯数字形态那一跳:item_id → 沃尔玛订货号 → 再走 extract_asin。
# ⚠ 这条 SQL 与 numeric_resolved 的记账此前在 order_asin_normalize 与
# sku_normalize 各写一份(**字节相同**),而规则本身早就只在本模块 ——
# 缺的正是"倒查那一跳"没跟着沉下来,于是两份拷贝各自演化(2026-08-27 收编)。
_ITEMID_SQL = """
SELECT DISTINCT item_id, sku FROM catalog.walmart_items
WHERE item_id = ANY(%s)
"""


def resolve_skus(conn, skus: list[str]) -> tuple[dict, dict]:
    """输入:连接 + 待洗 sku 列表 → 输出:({sku: asin}, 形态计数)。

    模式提取 + 纯数字倒查 item id 两跳;**解析不了的不进映射**(调用方留 NULL,
    绝不猜)。形态计数按 `classify` 的四个桶记,倒查成功的另记
    `numeric_resolved` 一档 —— 它同时也进 `numeric` 桶,两者不是互斥关系
    (摘要按"待洗形态分布 + 其中倒查救回多少"读)。

    纯读一条 SQL,不改任何行;真正的 UPDATE 在各工作流自己的 `_FILL_SQL`
    (打哪张表是两条链唯一的真差异)。
    """
    mapping: dict = {}
    buckets: Counter = Counter()
    numeric: list = []
    for s in skus:
        kind = classify(s)
        buckets[kind] += 1
        a = extract_asin(s)
        if a:
            mapping[s] = a
        elif kind == "numeric":
            numeric.append(s)
    if numeric:
        with conn.cursor() as cur:
            cur.execute(_ITEMID_SQL, (numeric,))
            hits = dict(cur.fetchall())     # item_id → 沃尔玛订货号
        for s in numeric:
            a = extract_asin(hits.get(s))
            if a:
                mapping[s] = a
                buckets["numeric_resolved"] += 1
    return mapping, dict(buckets)


def samples(skus: list[str], buckets: dict) -> dict:
    """输入:待洗 sku + 形态计数 → 输出:{形态: 前 5 个样本}(只给没解析出的桶)。

    只报 numeric/other 两个桶:asin/wrapped 是提得出的,不需要人认。
    "规则不全是常态"——新形态先进「其他」桶带样本报出来,人认了再扩规则。
    """
    return {k: [s for s in skus if classify(s) == k][:5]
            for k in ("numeric", "other") if buckets.get(k)}
