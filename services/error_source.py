"""报错原文的**唯一取用口**:四级优先,全文优先于样本。

⚠ 为什么要有这个模块(2026-09-04 生产实证):**判据统一 ≠ 口径统一**。
同一段文本判成什么是确定的,但**不同路径拿到的「那段文本」不一样**,于是同一个
品在两条路上判出相反的码。实测同一个 ASIN:

  · `walmart_items` 全文 —— 「…violating Walmart's Marketplace *Prohibited
    Product Policy*.  **To republish this item please make sure you have the
    appropriate product type selected for this item.**」→ `PT_WRONG`(修法不是禁令)
  · `product_events` 的 reason —— 「…violating Walmart's Marketplace
    ||Prohibited Product Policy@@@https://…」(**句尾那句判据串不在**)→ `POLICY`

后果:**3,037 个品**被 `blacklist_route` 正确删掉,又要被 `blacklist_push
-p backfill=1 -p apply=1` 错误地加回来 —— 而两边的摘要都显示正常。

所以取原文与归类(`services/error_taxonomy`)一样,只准有一处实现。谁要判一条
报错,先来这里拿原文,别自己 SELECT。

## 四级优先(顺序本身是判据的一部分)

`records`(全文)→ `events` → `items` → 调用方自己手上那份 → `none`。
⚠ 最后那一级常是**截断过的样本**,判据串可能被切掉(§14.8:`asin_blacklist.reason`
曾经截 200,而沃尔玛的判据串恰好在句尾)—— 所以它排最后,且四处都没有时
**留 NULL 不猜**。
"""

import logging

from services.sku_asin import extract_asin

logger = logging.getLogger("services.error_source")

#: 三条外源都取**每个键最新的那一条**:同一 asin 多条报错时拿最近那次的原文
#: —— 与黑名单「当轮类别」的口径一致(旧类别翻动频繁,「曾经命中过」不作数)。
SRC_RECORDS = """
SELECT DISTINCT ON (asin) asin, raw_reason
FROM audit.walmart_error_records
WHERE asin = ANY(%(asins)s) AND coalesce(raw_reason, '') <> ''
ORDER BY asin, report_date DESC NULLS LAST, id DESC
"""
SRC_EVENTS = """
SELECT DISTINCT ON (coalesce(asin, sku)) coalesce(asin, sku) AS k,
       detail->>'reasons' AS reasons
FROM catalog.product_events
WHERE coalesce(asin, sku) = ANY(%(asins)s)
  AND coalesce(detail->>'reasons', '') <> ''
ORDER BY coalesce(asin, sku), occurred_at DESC
"""
SRC_ITEMS = """
SELECT DISTINCT ON (sku) sku, unpublished_reasons
FROM catalog.walmart_items
WHERE sku = ANY(%(skus)s) AND coalesce(unpublished_reasons, '') <> ''
ORDER BY sku, updated_at DESC
"""

#: ⚠ 同一个 ASIN 在多店有**多个 sku**,调用方手上那个未必是当初入选的那个 ——
#: 生产实测 2026-09-04:回填与路由的 2,261 条冲突里 **2,194 条(97%)** 出在这里
#: (`_judge_events` 拿的是 `product_events.sku`,而 `error_reclass` 拿的是
#: `asin_blacklist.src_sku`,两个对不上 ⇒ items 那一级查不中 ⇒ 退回残缺的事件
#: reason ⇒ 判成 POLICY 而不是 PT_WRONG)。
#: 手上没有可靠 sku 的调用方传 `items_by_asin=True`:扫一遍有下架原因的行,
#: 按 `sku_asin` 规则折成 asin 再索引一份。**是全表扫**,所以要显式开 ——
#: 分批调用的消费方(`error_reclass` 有精确的 `src_sku`)不该付这个代价。
SRC_ITEMS_ANY = """
SELECT sku, unpublished_reasons FROM catalog.walmart_items
WHERE coalesce(unpublished_reasons, '') <> ''
"""


def fetch(conn, asins: list[str], skus: list[str], *,
          items_by_asin: bool = False) -> tuple[dict, dict, dict]:
    """输入:一批 asin/sku → 输出:(records, events, items) 三张映射。

    `items_by_asin=True` 时 `items` **同时按 asin 索引**(见 `SRC_ITEMS_ANY`
    头注:手上的 sku 不可靠时唯一查得中的办法;代价是一次全表扫)。

    查不到的那一级**只告警不阻断**:少一级原文是判得糙一点,而整条工作流炸掉
    是一条都判不了。
    """
    out: list[dict] = []
    for sql, args, key in ((SRC_RECORDS, {"asins": asins}, "asins"),
                           (SRC_EVENTS, {"asins": asins}, "asins"),
                           (SRC_ITEMS, {"skus": skus}, "skus")):
        if not args[key]:
            out.append({})
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                out.append({k: v for k, v in cur.fetchall() if v})
        except Exception as e:                                  # noqa: BLE001
            logger.warning("外源读不到(本级跳过):%s… / %s",
                           " ".join(sql.split())[:50], e)
            conn.rollback()
            out.append({})
    records, events, items = out
    if items_by_asin and asins:
        want = set(asins)
        try:
            with conn.cursor() as cur:
                cur.execute(SRC_ITEMS_ANY)
                for sku, text in cur.fetchall():
                    if not text:
                        continue
                    a = extract_asin(sku)
                    # 不覆盖按 sku 命中的那份(调用方给的 sku 更精确)
                    if a and a in want and a not in items:
                        items[a] = text
        except Exception as e:                                  # noqa: BLE001
            logger.warning("按 asin 补 items 那一级失败(跳过):%s", e)
            conn.rollback()
    return records, events, items


def pick(asin: str, own_reason: str | None, src_sku: str | None,
         records: dict, events: dict, items: dict) -> tuple[str, str]:
    """输入:一行 + 三张外源映射 → 输出:(原文, 来源标签)。

    四级优先,**全文优先于样本**;都没有给 `("", "none")` —— 调用方据此留 NULL,
    **不猜**。纯函数,零 I/O:优先序是判据的一部分,拿假数据就能测。
    """
    text = records.get(asin)
    if text:
        return text, "records"
    text = events.get(asin)
    if text:
        return text, "events"
    if src_sku:
        text = items.get(src_sku)
        if text:
            return text, "items"
    own = (own_reason or "").strip()
    if own:
        return own, "self"
    return "", "none"
