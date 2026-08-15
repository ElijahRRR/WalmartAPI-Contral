"""order_asin_normalize — 订单行 SKU→ASIN 清洗(可反复跑,只补 NULL)。

用法:
  python cli.py order_asin_normalize             # 预览:形态分布 + 各桶样本,零写入
  python cli.py order_asin_normalize -p apply=1  # 补填 orders.order_lines.asin

这是 docs/allocation_plan.md §十 的 **A1.5 批次,A2 分配引擎的硬前置**:
产品分最强的信号是"这个产品/这个品牌卖过且卖得动",而 `order_lines` 只有
沃尔玛侧的订货号 sku,没有源头 asin —— 没这一列,销量的层级回退
(ASIN → 品牌 → 大类 → 全局)前两级是空的,只剩最粗的大类基线。

清洗路径与 `sku_normalize`(事件账本那份)**逐字同源**,规则唯一出处
`services/sku_asin`,这里不重复实现:
  ① 裸 ASIN / 三段式「前缀-源头码-价格」→ 模式提取;
  ② 纯数字 = walmart item id → 倒查 `catalog.walmart_items`(item_id → 订货号
     → 再走 ①);查不到保持 NULL;
  ③ 其他形态 → 保持 NULL,预览里报样本,人认了再扩规则。

**提不出的留 NULL,不猜也不拿 sku 原文兜底**(所有者定稿 2026-08-15:
「就按 sku=asin 走,绝大部分都能拿到数据,少量拿不到也没关系」)。
拿原文兜底会更糟:三段式与纯数字两种形态直连采集库**永远查空**,
而那时它看起来像是"这个产品一单没卖过",是个静默的错误信号。
所以消费方一律按 `asin IS NOT NULL` 过滤,解析不了的那批**退出销量维度**
但**不影响店×SKU 维度**(那一层本来就用 sku,不需要 asin)。

**幂等**:`WHERE asin IS NULL`,重复跑只补新增行;规则扩充后重跑会把
上一轮解析不了的再捞一遍。
"""

import logging
from collections import Counter

from registry import db
from services import sku_asin

DANGEROUS = False

logger = logging.getLogger("workflows.order_asin_normalize")

_BATCH = 10000

_DISTINCT_SQL = """
SELECT DISTINCT sku FROM orders.order_lines
WHERE asin IS NULL AND sku IS NOT NULL AND btrim(sku) <> ''
"""

# item_id → 订货号:纯数字形态那一跳(与 sku_normalize 同一张表同一口径)
_ITEMID_SQL = """
SELECT DISTINCT item_id, sku FROM catalog.walmart_items
WHERE item_id = ANY(%s)
"""

_FILL_SQL = """
UPDATE orders.order_lines o SET asin = m.asin
FROM (SELECT unnest(%s::text[]) AS sku, unnest(%s::text[]) AS asin) m
WHERE o.sku = m.sku AND o.asin IS NULL
"""

_COVERAGE_SQL = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE asin IS NOT NULL) AS filled
FROM orders.order_lines
"""


def _resolve(conn, skus: list[str]) -> tuple[dict, dict]:
    """输入:连接 + 待洗 sku 列表 → 输出:({sku: asin}, 形态计数)。

    模式提取 + 纯数字倒查 item id 两跳;解析不了的**不进映射**(留 NULL)。
    与 `sku_normalize._resolve` 同一套路径 —— 两处都只调 services/sku_asin,
    规则本身不在这里实现,所以不存在"两份规则飘掉"的问题。
    """
    mapping: dict = {}
    buckets: Counter = Counter()
    numeric: list = []
    for s in skus:
        buckets[sku_asin.classify(s)] += 1
        a = sku_asin.extract_asin(s)
        if a:
            mapping[s] = a
        elif sku_asin.classify(s) == "numeric":
            numeric.append(s)
    if numeric:
        with conn.cursor() as cur:
            cur.execute(_ITEMID_SQL, (numeric,))
            hits = dict(cur.fetchall())        # item_id → 沃尔玛订货号
        for s in numeric:
            a = sku_asin.extract_asin(hits.get(s))
            if a:
                mapping[s] = a
                buckets["numeric_resolved"] += 1
    return mapping, dict(buckets)


def _samples(skus: list[str], buckets: dict) -> dict:
    """输入:待洗 sku + 形态计数 → 输出:{形态: 前 5 个样本}(只给没解析出的桶)。"""
    return {k: [s for s in skus if sku_asin.classify(s) == k][:5]
            for k in ("numeric", "other") if buckets.get(k)}


def run(params: dict) -> str:
    """输入:params(apply)→ 输出:清洗或预览摘要。"""
    apply = str(params.get("apply", "")).lower() in {"1", "true", "yes"}
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_DISTINCT_SQL)
            skus = [r[0] for r in cur.fetchall()]
        if not skus:
            return "订单 ASIN 清洗:无待洗行(asin 全已填)"

        mapping, buckets = _resolve(conn, skus)
        shape = ",".join(f"{k}×{v}" for k, v in sorted(buckets.items()))
        samples = _samples(skus, buckets)
        rate = len(mapping) / len(skus) if skus else 0.0
        head = (f"待洗 {len(skus)} 个不同 sku,形态 {shape};"
                f"可解析 {len(mapping)} 个({rate:.1%})")

        if not apply:
            return (f"🧪 订单 ASIN 清洗预览:{head}"
                    + "".join(f";{k} 样本 {v}" for k, v in samples.items())
                    + "\n   解析不了的**留 NULL 不猜**(消费方按 asin IS NOT NULL 过滤);"
                    + "\n   加 -p apply=1 补填 orders.order_lines.asin")

        done = 0
        keys = sorted(mapping)
        with conn.cursor() as cur:
            for i in range(0, len(keys), _BATCH):
                chunk = keys[i:i + _BATCH]
                cur.execute(_FILL_SQL, (chunk, [mapping[s] for s in chunk]))
                done += cur.rowcount or 0
                logger.info("订单 ASIN 清洗:已补 %d 行(%d/%d 个 sku)",
                            done, min(i + _BATCH, len(keys)), len(keys))
            cur.execute(_COVERAGE_SQL)
            total, filled = cur.fetchone()

        unresolved = len(skus) - len(mapping)
        if unresolved:
            logger.warning("订单 ASIN 清洗:%d 个 sku 解析不了(形态 %s,样本 %s),"
                           "保持 NULL 等规则扩充", unresolved, shape, samples)
        out = [f"✅ 订单 ASIN 清洗:{head};补填 {done} 行"]
        cov = f"{filled / total:.1%}" if total else "—"
        out.append(f"   全表覆盖率 {filled}/{total}({cov})")
        if unresolved:
            out.append(f"   解析不了 {unresolved} 个 sku,留 NULL 不猜"
                       f"(**不影响店×SKU 维度**,那一层本来就用 sku)"
                       + "".join(f";{k} 样本 {v}" for k, v in samples.items()))
        return "\n".join(out)
