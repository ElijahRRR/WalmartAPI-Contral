"""SKU → ASIN 清洗规则(**唯一出处**,Python 单实现,批量清洗也走这里)。

⚠ 2026-09-02(SKU 改造批次 0a)起,身份有**两条腿**:登记簿 catalog.listing_sources
里 source_type='amz' 行的 source_key 是权威身份键,模式提取(extract_asin)只为存量
兜底。两条腿的优先级与形态闸收在本模块的 `pick_asin` 里(**唯一出处**),批量反查
是 `resolve_many` / 单条壳 `resolve`,三个入口的分工见文件末尾那一段。

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


# ── 登记簿那一跳(SKU 改造批次 0a,2026-09-02)────────────────────────────────
# 三个入口的分工(**别在旁边另起第四条**):
#   · pick_asin(source_key, sku)  纯函数,一对一;五个调用点共用的优先级规则,
#     谁也别再各写一遍 `key or extract_asin(sku)`。
#   · resolve_many(conn, pairs)   services 内部的**有界批量反查**(几十~几百对)。
#   · resolve(conn, store, sku)   单条薄壳,内部就是 resolve_many。
# ⚠ **全表级取数一律在 SQL 里 LEFT JOIN 登记簿再取 coalesce(ls.source_key, w.sku)**,
#   不要拿十万对 (store, sku) 去 unnest —— 那是把一次 JOIN 换成一次巨型数组传参。
# ⚠ 批次 0b 若要给清洗工作流做 resolve_pairs(含纯数字倒查那一跳),必须**建在
#   resolve_many 之上**,不许在旁边另起一条批量入口(conventions §六单路径)。

#: 登记簿只按 (store, sku) 反查 amz 身份键。**不按 abandoned_at 过滤**:旧码
#: 带着订单/售后回来时必须还查得到(消费方契约,sku_plan §5.3)。
_REG_SQL = """
SELECT store, sku, source_key FROM catalog.listing_sources
JOIN unnest(%s::text[], %s::text[]) AS t(s, k) ON store = t.s AND sku = t.k
WHERE source_type = 'amz' AND source_key IS NOT NULL
"""


def pick_asin(source_key, sku) -> str | None:
    """输入:登记簿 amz 行的 source_key(可空)+ sku → 输出:ASIN(登记簿优先但
    要过形态闸,两条腿都提不出返 None)。

    **登记簿只是优先级,不是免检通道**:source_key 是运营在上架表里手填、由
    list_new 原样落库的(读表只 strip 不 upper),裸取会把一个小写 ASIN 变成
    下游集合里的垃圾键 —— 而真 ASIN 仍然缺席,于是"已在架"被判成"不在架",
    已上架的品被重新派工。故这条腿与 extract_asin 同口径:先归一(strip+upper)
    再过裸 ASIN 形态闸,不过就落回模式提取。
    """
    k = str(source_key or "").strip().upper()
    if k and is_standard_asin(k):
        return k
    return extract_asin(sku)


def resolve_many(conn, pairs) -> dict:
    """输入:连接 + [(store, sku)] → 输出:{(store, sku): asin}(有界批量反查)。

    一条 SQL 取登记簿键,逐对过 `pick_asin`;**提不出的不进映射**(与
    resolve_skus 同纪律:调用方留 NULL,绝不猜)。纯读,不改任何行。
    """
    want = [(str(st), str(sk)) for st, sk in pairs]
    if not want:
        return {}
    with conn.cursor() as cur:
        cur.execute(_REG_SQL, ([p[0] for p in want], [p[1] for p in want]))
        keys = {(st, sk): key for st, sk, key in cur.fetchall()}
    out = {}
    for pair in want:
        a = pick_asin(keys.get(pair), pair[1])
        if a:
            out[pair] = a
    return out


def resolve(conn, store, sku) -> str | None:
    """输入:连接 + 店铺 + sku → 输出:ASIN(提不出返 None)。resolve_many 的单条壳。"""
    return resolve_many(conn, [(store, sku)]).get((str(store), str(sku)))
