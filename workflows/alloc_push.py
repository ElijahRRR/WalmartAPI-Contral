"""alloc_push — 把已落占用的货追加进飞书上架表(A/B 两列)。**危险:缺省即真跑,空跑用 `--dry-run`。**

用法:
  python cli.py alloc_push --dry-run        # 预览:要追加多少行、被什么挡下
  python cli.py alloc_push                  # 真写
  python cli.py alloc_push -p limit=500     # 只推前 N 条(想小步试一次)

## 它补的是哪条缝

分配链跑完 `alloc_plan`,结果落在 `catalog.claims`(台账)与两张 csv(给人看)。
**但货进不了上架表** —— 上架链的驱动表是飞书上架表,不是 claims。此前这一步
只能人手把方案表往表里粘,§11.3 #1 记了很久的"写入器仍待建"就是它。

    alloc_plan(落占用)→ **alloc_push(本条)** → product_audit(审)→ list_new(上架)

## 列权责:只写 A/B(§9.2,所有者 2026-08-16 定稿)

只写 店铺/ASIN 两列,**「审核结果」留空即「待审」**,审核链下一轮自动领走。
⚠ **绝不许顺手写「审核结果」`pass`** —— 那是伪造审核结论,而且伪造也没用:
上架闸读的是 `catalog.products`,只会骗到人眼。

## 推哪些:三道筛,少一道都会出事

占用台账里躺着的**远不止待上架的货**,直接全推是灾难性的:

1. **在营店**(`stores.enabled_names()`)—— 停用店的占用还在台账里(占用没有
   自动释放,那是有意的),推进去等于给一家不做了的店派活;
2. **该店还没在架**(`walmart_items` 里没有它的活行)—— `alloc_backfill` 是
   **拿在架商品倒推出来的占用**,台账里绝大多数条目的货**早就在架上卖着**。
   不筛这一条,首跑会把几万条已上架的产品当成待办灌进上架表;
3. **上架表里还没有**(去重)—— 由 `listing_sheet.append_assignments` 整读
   A/B 判定。同一个 ASIN 重复派工,运营会做两遍。

三道剩下的才是"**已经定了要上、还没上、也还没派工**"的那批。
"""

import logging

from registry import db
from services import claims, listing_sheet, stores as stores_svc

DANGEROUS = True

logger = logging.getLogger("workflows.alloc_push")

# 该店此刻在架的 (店, ASIN)。判"在架"与 alloc_survey._SQL_ONLINE 同口径:
# 排 RETIRED —— 退市行不算活货位,它对应的占用是待重上的,该派工。
# ⚠ ASIN 走身份键(登记簿 amz 键优先,模式提取兜存量),唯一规则出处
# services/sku_asin.pick_asin:裸提取在切码后会让"已在架"集合恒空 ⇒ 已在架
# 的品被重新派工、重复上架(本工作流 DANGEROUS=True,直接写飞书上架表)。
# `ls.abandoned_at IS NULL` 在批次 2 之前**恒真**(全库该列为 NULL,而且这是
# LEFT JOIN,未登记行同样是 NULL),提前落地是为了让写侧切换只改一处。
_SQL_ONLINE = """
SELECT w.store, w.sku, ls.source_key
FROM catalog.walmart_items w
LEFT JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz'
WHERE w.missing_since IS NULL
  AND coalesce(upper(w.lifecycle_status), 'ACTIVE') = 'ACTIVE'
  AND ls.abandoned_at IS NULL
"""


def run(params: dict) -> str:
    """输入:params(limit/execute)→ 输出:推送摘要。"""
    execute = bool(params.get("execute"))
    limit = int(params.get("limit", 0)) or None

    try:
        live = stores_svc.enabled_names()
    except Exception as e:                          # noqa: BLE001
        return (f"⛔ 凭证表读不到({e}):分不清在营店与停用店,拒绝推送 —— "
                f"给一家不做了的店派活,运营会照着上架")

    from services import sku_asin
    with db.pg_conn() as conn:
        held = claims.load_active(conn, claims.PRODUCT)
        with conn.cursor() as cur:
            cur.execute(_SQL_ONLINE)
            online = {(s, sku_asin.pick_asin(k, sku))
                      for s, sku, k in cur.fetchall()}
    online = {(s, a) for s, a in online if a}

    rows, off, listed = [], 0, 0
    for asin, store in sorted(held.items()):
        if store not in live:
            off += 1
            continue
        if (store, asin) in online:
            listed += 1
            continue
        rows.append((store, asin))
    rows.sort()
    total = len(rows)
    if limit:
        rows = rows[:limit]

    n, start = listing_sheet.append_assignments(rows, execute=execute)
    dup = len(rows) - n if rows else 0

    head = [f"占用台账 {len(held):,} 条 → 待派工 {n:,} 行"]
    body = [f"  已在架 {listed:,}(回填出来的占用,货早就在卖 —— **不是**待办)",
            f"  停用店 {off:,}(占用保持不释放,但不给它派活)",
            f"  上架表里已有 {dup:,}(去重;同一个 ASIN 重复派工运营会做两遍)"]
    if limit and total > limit:
        body.append(f"  ⚠ `-p limit={limit:,}` 只取了前 {limit:,} 条,"
                    f"**还剩 {total - limit:,} 条没推** —— 这是安全阀不是模型,"
                    f"确认无误后去掉它再跑一次")
    if not n:
        return "\n".join([f"✓ 没有要派工的行({head[0]})"] + body)

    if not execute:
        return "\n".join(
            [f"🧪 将追加 {n:,} 行到上架表,从第 {start} 行起(只写 A 店铺 / B ASIN)"]
            + body
            + ["  「审核结果」留空 = 待审,审核链下一轮 `product_audit -p from_sheet=1` 领走。",
               "  ⚠ **本工作流绝不写「审核结果」** —— 那是伪造审核结论。",
               "  确认后去掉 --dry-run 重跑"])

    logger.warning("alloc_push 已追加上架表 %d 行,起始行 %d", n, start)
    return "\n".join(
        [f"✅ 已追加 {n:,} 行到上架表(第 {start} 行起,只写 A/B)"] + body
        + ["  下一步:`python cli.py product_audit -p from_sheet=1` 审这批"])
