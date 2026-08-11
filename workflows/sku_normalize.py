"""sku_normalize — 事件账本 SKU→ASIN 清洗(可反复跑,只补 NULL)。

用法:
  python cli.py sku_normalize             # 预览:形态分布 + 各桶样本,零写入
  python cli.py sku_normalize -p apply=1  # 补填 product_events.asin

背景(所有者定稿 2026-08-11):沃尔玛侧 sku 是订货号,与源头 asin 不保证
相等(三段式 JTZW-D01027HVK3W-38、纯数字 item id 实证)。历史 24 万条时间线
事件入库时没有 asin 列,拿原文直连采集库导致品牌全查空——本工作流一次性
补洗存量;实时链路已由 record_many 自动清洗,以后只在**规则扩充后**偶尔
重跑(WHERE asin IS NULL,天然幂等)。

清洗路径(规则唯一出处 services/sku_asin,这里不重复实现):
  ① 裸 ASIN / 三段式 → 模式提取;
  ② 纯数字 = walmart item id → 倒查 catalog.walmart_items(item_id → 订货号
    → 再走 ①);查不到保持 NULL;
  ③ 其他形态 → 保持 NULL 并在预览报样本,人认了再扩规则。

清洗完成后黑名单两侧要重建(按标准 asin 归并 + 表格重写):
  python cli.py blacklist_push -p rebuild_asin=1 -p apply=1
  python cli.py blacklist_push -p rebuild_brand=1 -p apply=1
"""

import logging
from collections import Counter

from registry import db
from services import sku_asin

DANGEROUS = False

logger = logging.getLogger("workflows.sku_normalize")

_DISTINCT_SQL = """
SELECT DISTINCT sku FROM catalog.product_events WHERE asin IS NULL
"""

_ITEMID_SQL = """
SELECT DISTINCT item_id, sku FROM catalog.walmart_items
WHERE item_id = ANY(%s)
"""

_FILL_SQL = """
UPDATE catalog.product_events e SET asin = m.asin
FROM (SELECT unnest(%s::text[]) AS sku, unnest(%s::text[]) AS asin) m
WHERE e.sku = m.sku AND e.asin IS NULL
"""


def _resolve(conn, skus: list[str]) -> tuple[dict, dict]:
    """输入:连接 + 待洗 sku 列表 → 输出:({sku: asin}, 形态计数)。
    模式提取 + 纯数字倒查 item id 两跳;解析不了的不进映射。"""
    mapping: dict = {}
    buckets: Counter = Counter()
    numeric: list = []
    for s in skus:
        kind = sku_asin.classify(s)
        buckets[kind] += 1
        a = sku_asin.extract_asin(s)
        if a:
            mapping[s] = a
        elif kind == "numeric":
            numeric.append(s)
    if numeric:
        with conn.cursor() as cur:
            cur.execute(_ITEMID_SQL, (numeric,))
            hits = dict(cur.fetchall())     # item_id → 沃尔玛订货号
        for s in numeric:
            a = sku_asin.extract_asin(hits.get(s))
            if a:
                mapping[s] = a
                buckets["numeric_resolved"] += 1
    return mapping, dict(buckets)


def run(params: dict) -> str:
    """输入:params(apply)→ 输出:清洗或预览摘要。"""
    apply = str(params.get("apply", "")).lower() in {"1", "true", "yes"}
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_DISTINCT_SQL)
            skus = [r[0] for r in cur.fetchall()]
        if not skus:
            return "SKU 清洗:事件账本无待洗行(asin 全已填)"
        mapping, buckets = _resolve(conn, skus)

        samples = {k: [s for s in skus if sku_asin.classify(s) == k][:5]
                   for k in ("numeric", "other") if buckets.get(k)}
        shape = ",".join(f"{k}×{v}" for k, v in sorted(buckets.items()))
        if not apply:
            return (f"SKU 清洗预览:待洗 {len(skus)} 个不同 sku,形态 {shape};"
                    f"可解析 {len(mapping)} 个"
                    + "".join(f";{k} 样本 {v}" for k, v in samples.items())
                    + ";加 -p apply=1 补填 product_events.asin,"
                      "完事后按模块头两条 rebuild 命令重建黑名单")

        n = 0
        if mapping:
            with conn.cursor() as cur:
                cur.execute(_FILL_SQL,
                            (list(mapping), [mapping[s] for s in mapping]))
                n = cur.rowcount or 0
        unresolved = len(skus) - len(mapping)
        if unresolved:
            logger.warning("SKU 清洗:%d 个 sku 解析不了(形态 %s,样本 %s),"
                           "保持 NULL 等规则扩充", unresolved, shape, samples)
        return (f"SKU 清洗:{len(mapping)}/{len(skus)} 个 sku 解析成功,"
                f"补填事件 {n} 行;解析不了 {unresolved} 个(保持原文兜底)"
                + "".join(f";{k} 样本 {v}" for k, v in samples.items())
                + ";接着跑 rebuild_asin / rebuild_brand 重建黑名单")
