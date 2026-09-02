"""order_asin_normalize — 订单行 SKU→ASIN 清洗(可反复跑,只补 NULL)。

用法:
  python cli.py db_init                          # ★ 首次必跑:应用 asin 列的迁移
  python cli.py order_asin_normalize             # 预览:形态分布 + 各桶样本,零写入
  python cli.py order_asin_normalize -p apply=1  # 补填 orders.order_lines.asin

这是 docs/allocation_plan.md §十 的 **A1.5 批次,A2 分配引擎的硬前置**:
产品分最强的信号是"这个产品/这个品牌卖过且卖得动",而 `order_lines` 只有
沃尔玛侧的订货号 sku,没有源头 asin —— 没这一列,销量的层级回退
(ASIN → 品牌 → 大类 → 全局)前两级是空的,只剩最粗的大类基线。

清洗路径与 `sku_normalize`(事件账本那份)**逐字同源**,规则唯一出处
`services/sku_asin`,这里不重复实现:
  ⓪ 登记簿 `catalog.listing_sources` 按 (店, sku) 反查 source_key(切码后唯一
     通路);②的倒查分两级:先 (店, item_id),查不到再按 item_id 全局查一次
     (**后者是既有行为,保住跨店补齐的覆盖面**);
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
切码后登记簿是主路、形态提取只是存量兜底 —— 两者都空才留 NULL。

**它是扫尾不是主路**(与 `sku_normalize` 同一分工):`order_sync` 与
`order_history_import` **落库当场**就把 asin 填好(前者在唯一写入口
`order_lines.upsert_order_lines` 里经登记簿反查,后者只导存量形态),
所以新进的行不会是空的。本工作流只负责两件事 ——
  ① **存量补洗**(加列之前入库的历史行);
  ② **纯数字 item_id 形态**:那一跳要查 `catalog.walmart_items`,
     写入路径上做不了(逐行查库),只能事后扫
     (倒查分两级:先 (店, item_id),再按 item_id 全局兜底一次)。
⚠ 正因为同步侧对纯数字形态算不出 asin,`upsert_order_lines` 给这一列配了
`COALESCE(EXCLUDED.asin, t.asin)` 守卫 —— 否则每轮同步都会把本工作流
填好的值冲回 NULL,那一列永远填不满。

**幂等**:`WHERE asin IS NULL`,重复跑只补新增行;规则扩充后重跑会把
上一轮解析不了的再捞一遍。
"""

import logging

from registry import db
from services import sku_asin

DANGEROUS = False

logger = logging.getLogger("workflows.order_asin_normalize")

_BATCH = 10000

_DISTINCT_SQL = """
SELECT DISTINCT store, sku FROM orders.order_lines
WHERE asin IS NULL AND sku IS NOT NULL AND btrim(sku) <> ''
"""

# ⚠ `IS NOT DISTINCT FROM` 与事件账本那份逐字同写法(两条清洗器有对拍测试):
# 订单行的 store 理论上非空,非空时它与 `=` 等价,脏数据上更安全。带 store 是
# 因为同一串 sku 在两家店切码后指向不同产品,不带就会把 A 店的 asin 写到 B 店。
_FILL_SQL = """
UPDATE orders.order_lines o SET asin = m.asin
FROM (SELECT unnest(%s::text[]) AS store, unnest(%s::text[]) AS sku,
             unnest(%s::text[]) AS asin) m
WHERE o.sku = m.sku AND o.store IS NOT DISTINCT FROM m.store AND o.asin IS NULL
"""

_COVERAGE_SQL = """
SELECT count(*) AS total,
       count(*) FILTER (WHERE asin IS NOT NULL) AS filled
FROM orders.order_lines
"""


def run(params: dict) -> str:
    """输入:params(apply)→ 输出:清洗或预览摘要。"""
    apply = str(params.get("apply", "")).lower() in {"1", "true", "yes"}
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(_DISTINCT_SQL)
            except Exception as e:            # noqa: BLE001 只翻译这一种,其余照抛
                # 本工作流是 asin 列的第一个消费方,所以"迁移还没应用"这件事
                # 一定先从这里炸。原始 UndefinedColumn 栈对着列名说话,
                # 不告诉人下一步该干什么 —— 而下一步只有一条命令
                if "asin" not in str(e) or "does not exist" not in str(e):
                    raise
                conn.rollback()               # 事务已 aborted,不回滚 commit 会再炸
                return ("⛔ `orders.order_lines.asin` 列还不存在 —— "
                        "schema.sql 的迁移块没应用到这个库。\n"
                        "   先跑:python cli.py db_init(幂等,只补缺的表与列)\n"
                        "   再跑本工作流。")
            pairs = [(s, k) for s, k in cur.fetchall()]
        if not pairs:
            return "订单 ASIN 清洗:无待洗行(asin 全已填)"

        mapping, buckets = sku_asin.resolve_pairs(conn, pairs)
        shape = ",".join(f"{k}×{v}" for k, v in sorted(buckets.items()))
        samples = sku_asin.samples(pairs, buckets)
        # 「个 sku」是 sku 级数字(与改前逐字可比),组合数并列报出
        n_sku = len({k for _s, k in pairs})
        n_sku_ok = len({k for _s, k in mapping})
        rate = len(mapping) / len(pairs) if pairs else 0.0
        head = (f"待洗 {n_sku} 个不同 sku / {len(pairs)} 个 (店,sku) 组合,"
                f"形态 {shape};可解析 {n_sku_ok} 个 sku、"
                f"{len(mapping)} 个组合({rate:.1%})")

        if not apply:
            return (f"🧪 订单 ASIN 清洗预览:{head}"
                    + "".join(f";{k} 样本 {v}" for k, v in samples.items())
                    + "\n   解析不了的**留 NULL 不猜**(消费方按 asin IS NOT NULL 过滤);"
                    + "\n   加 -p apply=1 补填 orders.order_lines.asin")

        done = 0
        # store 可能是 None,排序键必须自己给 or "";三个平行数组从同一个
        # chunk 摊出来,错位就是把别人的 asin 填进去(而且不报错)
        keys = sorted(mapping, key=lambda p: (p[0] or "", p[1]))
        with conn.cursor() as cur:
            for i in range(0, len(keys), _BATCH):
                chunk = keys[i:i + _BATCH]
                cur.execute(_FILL_SQL, ([s for s, _ in chunk],
                                        [k for _, k in chunk],
                                        [mapping[p] for p in chunk]))
                done += cur.rowcount or 0
                logger.info("订单 ASIN 清洗:已补 %d 行(%d/%d 个 sku)",
                            done, min(i + _BATCH, len(keys)), len(keys))
            cur.execute(_COVERAGE_SQL)
            total, filled = cur.fetchone()

        unresolved = n_sku - n_sku_ok
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
