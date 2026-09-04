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

## 两个取用口,按「调用方有没有自己的原文」分

· `pick` —— 调用方**没有**自己那一刻的原文(黑名单行:`reason` 只是入选时抄的
  样本,行本身代表「这个 asin 被禁」)。四级优先,顺序本身是判据的一部分:
  `records`(全文)→ `events` → `items` → 调用方手上那份 → `none`。
  ⚠ 倒数第二级常是**截断过的样本**,判据串可能被切掉(§14.8:
  `asin_blacklist.reason` 曾经截 200,而沃尔玛的判据串恰好在句尾)—— 所以它
  排最后,且四处都没有时**留 NULL 不猜**。

· `restore` —— 调用方**有**自己那一刻的原文(产品事件:时间线上的一格)。
  这时"换一份更好的原文"是错的,唯一该做的是**把被切掉的那段接回去**:
  候选必须以自己那份为前缀。2026-09-04 我在这里翻过车(§17):事件回填拿
  事件自己的 200 字残文重判,把 `problem_scan` 当初用**全文**判对的
  2,595 条 `PT_WRONG` 改成了 `POLICY` —— 回填把判对的行改错了。
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
        items = {**items_by_asin_map(conn), **items}   # 按 sku 命中的优先
    return records, events, items


def items_by_asin_map(conn) -> dict:
    """输入:连接 → 输出:{asin: 下架原因}(扫一遍有下架原因的行,按 sku_asin 折)。

    ⚠ **是全表扫**,分批跑的调用方要在循环外调一次、跨批复用,别每批扫一遍。
    查不到就返回空:少一级原文是判得糙一点,炸掉是一条都判不了。
    """
    out: dict = {}
    try:
        with conn.cursor() as cur:
            cur.execute(SRC_ITEMS_ANY)
            for sku, text in cur.fetchall():
                if not text:
                    continue
                a = extract_asin(sku)
                if a and a not in out:
                    out[a] = text
    except Exception as e:                                      # noqa: BLE001
        logger.warning("按 asin 扫 items 失败(本级跳过):%s", e)
        conn.rollback()
    return out


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
    # ⚠ 先 sku 后 asin,**两个都要试**(2026-09-04 实遇:只按 src_sku 查时,
    #   `fetch(items_by_asin=True)` 补进来的 asin 键**永远查不到** —— 索引加了、
    #   查法没改,冲突数纹丝不动 2,261 → 2,263)。
    #   sku 更精确所以排前面;sku 失效(下架删除)或对不上时按 asin 兜底。
    if src_sku:
        text = items.get(src_sku)
        if text:
            return text, "items"
    text = items.get(asin)
    if text:
        return text, "items"
    own = (own_reason or "").strip()
    if own:
        return own, "self"
    return "", "none"


#: 两处历史截断都是 `[:200]`:`asin_blacklist.reason` 与 `problem_scan` 写事件的
#: `detail.reason`(后者到 2026-09-04 才拆掉)。**比它短的那份没被我们切过**,
#: 所以短于此长度一律不做还原 —— 见 `restore` 头注第二条。
SAMPLE_LEN = 200


def restore(own: str | None, candidates) -> tuple[str, str]:
    """输入:一段(可能被截断的)原文 + [(标签, 候选全文), …] → 输出:(原文, 来源标签)。

    **只做一件事:把被 `[:200]` 切掉的那一段接回去。** 没接上就原样返回
    `("…", "self")`。

    与 `pick` 的分工(两者都在这个模块里,是因为「取原文」只准有一处实现):
      · `pick` 服务**没有自己原文**的调用方(黑名单行:`reason` 只是入选时抄的
        样本,行本身代表「这个 asin 被禁」)—— 拿这个 asin 最新的全文判是对的;
      · `restore` 服务**有自己原文**的调用方(产品事件:它是时间线上的一格,
        自带那一刻的原文)—— 拿别的时间点的文本判它就是**串账**。

    两条判据,缺一不可(2026-09-04 生产事故,见 docs/error_taxonomy.md §17):
      1. 候选必须**以 own 为前缀**:是前缀 ⇒ 就是同一段文本被切之前的样子;
         不是前缀 ⇒ 那是**另一次报错**,不是这一条的全文,不能用;
      2. `own` 必须**够到 `SAMPLE_LEN`**:短于 200 的那份根本没被切过,
         此时"更长的候选"是另一段更长的文本(比如后来又追加了一条理由),
         接上去就等于拿后来的状态改写历史那一格。

    纯函数,零 I/O:优先序与前缀判据是判据的一部分,拿假数据就能测。
    """
    base = (own or "").rstrip()
    best, label = own or "", "self"
    if len(base) < SAMPLE_LEN:
        return best, label
    for tag, text in candidates or ():
        if text and len(text) > len(best) and text.startswith(base):
            best, label = text, tag
    return best, label
