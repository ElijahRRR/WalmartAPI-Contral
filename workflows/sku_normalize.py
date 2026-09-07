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
  ⓪ 登记簿 catalog.listing_sources 按 (店, sku) 反查 source_key(切码后唯一
    通路);②的倒查分两级:先 (店, item_id),查不到再按 item_id 全局查一次
    (**后者是既有行为,保住跨店补齐的覆盖面**);
  ① 裸 ASIN / 三段式 → 模式提取;
  ② 纯数字 = walmart item id → 倒查 catalog.walmart_items(item_id → 订货号
    → 再走 ①);查不到保持 NULL;
  ③ 其他形态 → 保持 NULL 并在预览报样本,人认了再扩规则。

清洗完成后黑名单两侧要重建(按标准 asin 归并 + 表格重写):
  python cli.py blacklist_push -p rebuild_asin=1 -p apply=1
  python cli.py blacklist_push -p rebuild_brand=1 -p apply=1
"""

import logging

from registry import db
from services import sku_asin

DANGEROUS = False

logger = logging.getLogger("workflows.sku_normalize")

_DISTINCT_SQL = """
SELECT DISTINCT store, sku FROM catalog.product_events WHERE asin IS NULL
"""

# ⚠ `IS NOT DISTINCT FROM` 不许写成 `=`:平台级事件(product_ingest /
# audit_store.event_row / product_audit 补采)的 store 是 NULL,`=` 会把这批行
# 全部漏掉**而且不报错**。带 store 只改「怎么定位行」不改「填什么值」——待洗集合
# 由 SELECT DISTINCT store, sku 枚举了每一个组合,更新到的行集合与按裸 sku 相同。
_FILL_SQL = """
UPDATE catalog.product_events e SET asin = m.asin
FROM (SELECT unnest(%s::text[]) AS store, unnest(%s::text[]) AS sku,
             unnest(%s::text[]) AS asin) m
WHERE e.sku = m.sku AND e.store IS NOT DISTINCT FROM m.store AND e.asin IS NULL
"""


def run(params: dict) -> str:
    """输入:params(apply)→ 输出:清洗或预览摘要。"""
    apply = str(params.get("apply", "")).lower() in {"1", "true", "yes"}
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_DISTINCT_SQL)
            pairs = [(s, k) for s, k in cur.fetchall()]
        if not pairs:
            return "SKU 清洗:事件账本无待洗行(asin 全已填)"
        mapping, buckets = sku_asin.resolve_pairs(conn, pairs)

        samples = sku_asin.samples(pairs, buckets)
        shape = ",".join(f"{k}×{v}" for k, v in sorted(buckets.items()))
        # 摘要里的「个 sku」一律用 sku 级数字(与改前逐字可比);(店,sku) 组合数
        # 另起一格并列报出,别把两种单位混进同一个数
        n_sku = len({k for _s, k in pairs})
        n_sku_ok = len({k for _s, k in mapping})
        if not apply:
            return (f"SKU 清洗预览:待洗 {n_sku} 个不同 sku / {len(pairs)} 个 "
                    f"(店,sku) 组合,形态 {shape};"
                    f"可解析 {n_sku_ok} 个 sku、{len(mapping)} 个组合"
                    + "".join(f";{k} 样本 {v}" for k, v in samples.items())
                    + ";加 -p apply=1 补填 product_events.asin,"
                      "完事后按模块头两条 rebuild 命令重建黑名单")

        n = 0
        if mapping:
            # 三个平行数组一旦错位,填进去的就是别人的 asin,而且不报错:先把
            # keys 固定下来再摊(store 可能是 None,排序键必须自己给 or "")
            keys = sorted(mapping, key=lambda p: (p[0] or "", p[1]))
            with conn.cursor() as cur:
                cur.execute(_FILL_SQL, ([s for s, _ in keys],
                                        [k for _, k in keys],
                                        [mapping[p] for p in keys]))
                n = cur.rowcount or 0
        unresolved = n_sku - n_sku_ok
        if unresolved:
            logger.warning("SKU 清洗:%d 个 sku 解析不了(形态 %s,样本 %s),"
                           "保持 NULL 等规则扩充", unresolved, shape, samples)
        return (f"SKU 清洗:{n_sku_ok}/{n_sku} 个 sku 解析成功,"
                f"补填事件 {n} 行;解析不了 {unresolved} 个(保持原文兜底)"
                + "".join(f";{k} 样本 {v}" for k, v in samples.items())
                + ";接着跑 rebuild_asin / rebuild_brand 重建黑名单")
