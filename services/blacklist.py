"""黑名单收集积木(problem_product_cleanup 尾段用)——自产回路的落库端。

旧系统语义逐字保留(legacy_survey:1435 / blacklist_sync.py:18-21):

  ASIN 黑名单只收 **PERMANENT**(永久产品级禁止),其余(可修复/临时/平台类)
  明确排除——进了会误杀重上架拦截。所有者 2026-08-11 再次拍板:飞书表来源列的
  13 类词表只是**格式约定**,不是入选范围。

  ⚠ **2026-09-03 换轨**:入选码从旧 A-L 的 {B,C,E,F,G,K} 换成新 16 码里
  所有者逐码裁决过的七个 + `OTHER` 的两个显式词条(`services/error_taxonomy.
  PERMANENT_CODES` / `is_permanent`,裁决表 `docs/error_taxonomy.md` §十二)。
  修的是一个具体缺陷:旧 B(禁售)桶里混着 `PT_WRONG` —— 沃尔玛原话是
  「要重新上架请把 product type 选对」,是修法不是禁令,却被永久拉黑
  (存量实测 40,825 条)。

  入选按**当轮类别**判(cleanup 跑的就是今天的问题清单,当轮=最新)。
  历史数据实证类别翻动频繁(48.5 万行折叠出 23.9 万次变迁),
  「曾经命中过 B」不能作数——那会把短暂误判过的商品永久拉黑。

  品牌收集只看 `BRAND`(品牌未授权)/ `IP`(知识产权)两类(换轨前是 C/E);品牌名从 **catalog.products.brand**
  取(所有者定稿 2026-08-11:从采集库读,不再走旧系统的 DMIT 逐个采);
  **去重按品牌**,SKU 只是溯源列。已处理 ASIN 记 ops.dedupe
  ('cleanup:brand_asin',历史 2,609 个已导入)——品牌还没采到的 ASIN
  **不标已处理**,等产品中心补上品牌后自然重试。

  品牌落**两张表**(所有者厘清 2026-08-11):
    catalog.brand_err_hits   渠道表(beyKyi 投影源):完整记录沃尔玛后台
                             拿到过哪些品牌,渠道内按品牌去重,**不与总
                             清单去重**——挤在 brand_blacklist 里按品牌
                             冲突,总表已有的品牌就永远进不了渠道(第一版
                             的建模缺陷,当天修正)。brand_new/known 按
                             渠道表算。
    catalog.brand_blacklist  总清单镜像 + 否决闸:自产品牌 DO NOTHING
                             补进闸门,立刻挡住重上架;总表真值不覆盖。

  BIZ-CN 独立成维度(legacy_survey:2077:唯一明确标注中国卖家专属禁售的
  错误码,不能被 C 品牌类的关键词匹配吸收)——两张表都带 biz_cn 布尔列。

写入方向:`problem_scan` → PG(本文件)→ 飞书投影(`blacklist_push` 工作流,
**整表重写**;按 pushed_at 水位追加那套 2026-08-17 已废除)。
PG 权威,飞书只是人机界面。

⚠ **两条时间线不一样,别混**(所有者 2026-08-17 问「黑名单出来了会立刻推送到
飞书表格吗」):

  · **否决闸立刻生效** —— 本文件写完 `brand_blacklist` / `asin_blacklist` 那一刻,
    上架(`list_new` / `match_listing`)与审核(Phase0)就拦得住了。它们读的是
    **PG**,从不读飞书表。
  · **表格要等 `blacklist_push`** —— 那是另一条链(调度里的 `blacklist` 任务)。
    所以"扫出来"和"表格里看得见"之间隔着一条链、两个小时。想立刻看见就手动跑
    `python cli.py blacklist_push`。
"""

import json
import logging
from collections import Counter

from registry import resources
from services import error_source, error_taxonomy
from services.sku_asin import extract_asin

logger = logging.getLogger("services.blacklist")

# 永久产品级禁止(入选集合)。**2026-09-03 换轨**:从旧 A-L 的
# {B,C,E,F,G,K} 换成新 16 码里所有者逐码裁决过的那七个(裁决表
# `docs/error_taxonomy.md` §十二),口径唯一出处在 `services/error_taxonomy`。
# 换轨修的是一个具体缺陷:旧 B(禁售)一个桶里混着 `PT_WRONG` —— 沃尔玛原话是
# 「要重新上架请把 product type 选对」,那是修法不是禁令,却被永久拉黑
# (存量实测 40,825 条,§11.6)。新码把它摘了出去,真禁售那几种照旧拉黑。
# 改这个集合 = 改业务口径,必须先过所有者。
#: ⚠ **`reason` 存全文,不许截断**(2026-09-04 所有者问「为什么要截断字符样本?」
#: 之后考古的结论)。原来三处写入都做 `[:200]`,而**全仓找不到任何依据** ——
#: 最合理的解释是:这一列当初的用途是「人看一眼知道为什么被拉黑」,对**显示**
#: 来说 200 字符足够,那时它不是判据。
#:
#: 它变成问题是因为换轨之后 `error_reclass` 的四级优先把它当成了证据源,而且是
#: **最大的一档**(36,868 条只有它)。而沃尔玛那句判据串恰好在**句尾** ——
#: 「…violating Prohibited Product Policy. **To republish this item please make
#: sure you have the appropriate product type selected.**」—— 200 字符精确地
#: 砍掉它,于是那批品被判成 `POLICY` 永久拉黑,而真相是 `PT_WRONG`(修法不是禁令)。
#: **40,827 这个数是低估的。**
#:
#: 不截的依据:① 列是 `text`,无长度限制;② 飞书那侧**已经有自己的截断**
#: (`api/feishu._scrub` 20,000 字符脏数据闸 + 40,000 硬闸,官方上限 50,000),
#: 跟 200 差两个数量级。**截断属于展示层,不属于存储层** —— 存储侧截了,
#: 展示侧那道就白设了,而判据侧永久失去证据。
#: ⚠ 存量**救不回来**(截掉的字没了),只能从 `audit.walmart_error_records.raw_reason`
#: 重新取 —— 那正是 §14.7 说的「原文来源统一」。

PERMANENT = frozenset(error_taxonomy.PERMANENT_CODES)

# 品牌收集的触发类别:品牌未授权 / 知识产权(旧 C/E 的新码等价物)
BRAND_CATEGORIES = frozenset({"BRAND", "IP"})

BRAND_ASIN_SCOPE = "cleanup:brand_asin"     # ops.dedupe:已做过品牌收集的 ASIN

# 新码 → 飞书「来源」列的类名。⚠ **故意沿用旧 A-L 那套中文词**
# (禁售/品牌/知产/限类/审查):来源列是飞书表的格式约定,运营按这几个词筛,
# 换轨换的是**底层判据**,不该顺手改掉人眼看的那一列。
# POLICY / PROHIBITED_FINAL / RECALL 三个新码都从旧 B 拆出来,故同归「禁售」
# (旧 G 药品也是政策的一类,一并并进去)。
_NAMES = {"POLICY": "禁售", "PROHIBITED_FINAL": "禁售", "RECALL": "禁售",
          "BRAND": "品牌", "IP": "知产", "GATED": "限类", "FLAGGED": "审查",
          "OTHER": "禁售"}


def is_biz_cn(reason_text) -> bool:
    """输入:报错原文 → 输出:是否 BIZ-CN(中国卖家专属禁售)。"""
    t = (reason_text or "").lower()
    return "biz-cn" in t or "reference code biz" in t


def source_label(code: str) -> str:
    """输入:类别码 → 输出:飞书来源列格式「沃尔玛-〈类名〉」。"""
    return f"沃尔玛-{_NAMES.get(code, code)}"


_ASIN_SQL = """
INSERT INTO catalog.asin_blacklist
    (asin, category, source, reason, src_store, biz_cn, src_sku)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (asin) DO NOTHING
"""


def record_asins(conn, items: list[dict]) -> int:
    """输入:连接 + 当轮已归类 item(store/sku/category/reasons)
    → 输出:新入选数。永久禁止 = 一次入选,已在名单的不更新(DO NOTHING)。

    黑名单键 = 清洗后的标准 asin(sku_asin 规则;提不出用订货号原文兜底,
    宁可键不标准也不丢行),订货号原文存 src_sku 溯源。"""
    added = 0
    with conn.cursor() as cur:
        for it in items:
            code = it.get("category")
            # ⚠ 走 `is_permanent` 而不是 `code in PERMANENT`:`OTHER` 是混装桶
            # (显式杂项 + 兜底),所有者只让 business decision / trust & safety
            # 两个词条算永久拉黑,判据在引擎里,这儿不重新匹配一遍。
            if not error_taxonomy.is_permanent(code, it.get("unlisted_term")):
                continue
            asin = extract_asin(it["sku"]) or it["sku"]
            cur.execute(_ASIN_SQL, (
                asin, code, source_label(code),
                (it.get("reasons") or "") or None,      # 全文,别截(见头注)
                it.get("store"), is_biz_cn(it.get("reasons")), it["sku"]))
            added += cur.rowcount or 0
    return added


_PROCESSED_SQL = "SELECT key FROM ops.dedupe WHERE scope = %s AND key = ANY(%s)"

_BRAND_OF_SQL = """
SELECT asin, brand FROM catalog.products
WHERE marketplace = 'US' AND asin = ANY(%s)
  AND coalesce(btrim(brand), '') <> ''
"""

_CHANNEL_SQL = """
INSERT INTO catalog.brand_err_hits
    (brand_key, brand, source, added_date, src_sku, src_store, biz_cn)
VALUES (%s, %s, %s, CURRENT_DATE::text, %s, %s, %s)
ON CONFLICT (brand_key) DO NOTHING
"""

_GATE_SQL = """
INSERT INTO catalog.brand_blacklist (brand_key, brand, source, added_date)
VALUES (%s, %s, %s, CURRENT_DATE::text)
ON CONFLICT (brand_key) DO NOTHING
"""

_MARK_SQL = """
INSERT INTO ops.dedupe (scope, key, meta)
VALUES (%s, %s, %s::jsonb) ON CONFLICT DO NOTHING
"""


def collect_brands(conn, items: list[dict]) -> dict:
    """输入:连接 + 当轮已归类 item → 输出:统计 dict。

    C/E 类 → 查品牌 → 入渠道表 brand_err_hits(brand_new/known 按渠道算)
    + DO NOTHING 补进否决闸 brand_blacklist(总表真值不覆盖)→ 标 ASIN
    已处理。**品牌缺失的 ASIN 不标已处理**:产品中心还没这行或 brand 为空,
    等 product_ingest 补上后下一轮自然重试,标了就永远漏了。
    """
    stats = {"brand_new": 0, "brand_known": 0, "no_brand": 0, "skipped": 0}
    cands = {it["sku"]: it for it in items
             if it.get("category") in BRAND_CATEGORIES}
    if not cands:
        return stats
    with conn.cursor() as cur:
        cur.execute(_PROCESSED_SQL, (BRAND_ASIN_SCOPE, list(cands)))
        done = {r[0] for r in cur.fetchall()}
        stats["skipped"] = len(done)
        todo = {a: it for a, it in cands.items() if a not in done}
        if not todo:
            return stats
        # 采集库按**清洗后的标准 asin** 查(订货号原文直接查必然全空——
        # 2026-08-11 生产实证:2,702 个 C/E 品牌 0 命中,教训在此)
        asin_of = {sku: (extract_asin(sku) or sku) for sku in todo}
        cur.execute(_BRAND_OF_SQL, (list(set(asin_of.values())),))
        brand_of = {a: b for a, b in cur.fetchall()}
        for sku, it in todo.items():
            brand = (brand_of.get(asin_of[sku]) or "").strip()
            if not brand:
                stats["no_brand"] += 1      # 不标已处理:等品牌到位重试
                continue
            label = source_label(it["category"])
            # 溯源列存**清洗后的标准 asin**(表格 D 列表头就叫 ASIN;
            # 提不出标准码才落订货号原文)
            cur.execute(_CHANNEL_SQL, (
                brand.casefold(), brand, label,
                asin_of[sku], it.get("store"), is_biz_cn(it.get("reasons"))))
            if cur.rowcount:
                stats["brand_new"] += 1
            else:
                stats["brand_known"] += 1
            cur.execute(_GATE_SQL, (brand.casefold(), brand, label))
            cur.execute(_MARK_SQL, (BRAND_ASIN_SCOPE, sku,
                                    json.dumps({"brand": brand.casefold()},
                                               ensure_ascii=False)))
    return stats


# ── 上架拦截消费方(2026-08-12 接入:黑名单建好不接消费方 = 白建)─────────────

_GATE_ASINS_SQL = "SELECT asin, category, source FROM catalog.asin_blacklist"


def load_banned_asins(conn) -> dict:
    """输入:连接 → 输出:{asin: (category, source)}。

    list_new / match_listing 上架前逐行查的拦截集合(每轮加载一次,
    逐行零查询——与 risk_gate.load_gate 同款用法)。键是清洗后的标准
    asin(重灌后个别 numeric 键是订货号原文兜底,拦不着也不误拦)。"""
    with conn.cursor() as cur:
        cur.execute(_GATE_ASINS_SQL)
        return {a: (c, s) for a, c, s in cur.fetchall()}


# ── 历史回填(blacklist_push -p backfill=1 / rebuild_brand=1 用,一次性)────────
#
# ⚠ **判据(所有者 2026-09-04 定稿)**:
#   「一个产品的报错可能存在多次,**其中被拉黑的那个作为最高优先级**,
#    其他的都是作为记录」。
#
# 这**推翻了**此前那条「最新类别命中才算,『曾命中过』不作数」——
# 旧写法 `DISTINCT ON (asin) … ORDER BY occurred_at DESC` 只看最新一条,于是
# 一个品上个月被判 `POLICY`(该永久拉黑)、这个月的记录是 `EXPIRED`(过期),
# 就**把历史上那条禁令忘了**。那与黑名单「一次入选、永久禁止」的语义相反。
#
# 现在:拿这个 asin 的**全部历史报错原文**逐条归类,**只要有一条够格永久拉黑
# 就以它为准**;一条都不够格才算它不该拉黑(其余报错只是记录)。
#
# ⚠ 身份是 **asin**(所有者第 4 点):「产品报错 → 通过 sku 找到来源码(asin)
#   → 对相关 asin 归类处理。如果只跟着 sku 走,sku 又是由我们系统生成的,
#   后面会追不到问题产品的来源码」。所以 `coalesce(asin, sku)` 分组,
#   sku 只是提不出 asin 时的兜底键。
#
# ⚠ 键名两个都认:写入方(`problem_scan` / `cleanup_history`)写的是
#   `detail->>'reason'`(**单数**),而库里历史上还有一批写的是 `reasons`
#   (复数)。2026-09-04 查出读写不一致导致 events 那一级大面积空转 ——
#   这里 `coalesce` 两个都取,别再各写一半。

#: 「每个 asin 的最新一条」—— **只给品牌渠道用**(brand_err_hits 的语义就是
#: 「当前这个品牌还在不在问题里」)。⚠ **黑名单那条路已经不用它了**:
#: 黑名单是「一次入选、永久禁止」,判据是下面的 `_HISTORY_SQL`(全部历史里
#: 够格拉黑的那条优先),两者别混。
_LATEST_CTE = """
WITH latest AS (
    SELECT DISTINCT ON (coalesce(asin, sku)) coalesce(asin, sku) AS asin,
           sku, store, occurred_at,
           detail->>'category' AS cat,
           coalesce(detail->>'reason', detail->>'reasons') AS reason
    FROM catalog.product_events
    WHERE event = 'problem_categorized'
    ORDER BY coalesce(asin, sku), occurred_at DESC)
"""

_HISTORY_SQL = """
SELECT coalesce(asin, sku) AS asin,
       array_agg(DISTINCT ARRAY[detail->>'category',
                                detail->>'taxonomy_term']) AS codes,
       (array_agg(sku   ORDER BY occurred_at DESC))[1] AS sku,
       (array_agg(store ORDER BY occurred_at DESC))[1] AS store,
       (array_agg(coalesce(detail->>'reason', detail->>'reasons')
                  ORDER BY occurred_at DESC))[1] AS reason,
       max(occurred_at) AS latest
FROM catalog.product_events
WHERE event = 'problem_categorized'
  AND coalesce(detail->>'category', '') <> ''
GROUP BY 1
"""


#: 预览用的两个数:品牌渠道候选 + 时间线总量(去留由 `_judge_events` 算)。
_BACKFILL_COUNT_SQL = """
SELECT count(*) FILTER (WHERE cat = ANY(%(brandcats)s)) AS brand_cand,
       count(*) AS total
FROM (SELECT DISTINCT ON (coalesce(asin, sku)) coalesce(asin, sku),
             detail->>'category' AS cat
      FROM catalog.product_events
      WHERE event = 'problem_categorized'
      ORDER BY coalesce(asin, sku), occurred_at DESC) t
"""


#: 够格拉黑的码之间谁当标签 —— 序在 registry(铁律 3),这儿只查表。
_LABEL_RANK = {c: i for i, c in enumerate(resources.BLACKLIST_LABEL_ORDER)}


def worst_verdict(codes):
    """输入:一个 asin 的**全部事件码** `[[code, term], …]` → 输出:够格永久拉黑的
    那个 `(code, term)`;一个都不够格给 None。

    ⚠ **够格拉黑的那条最高优先级**(所有者 2026-09-04),其余只是记录。
    ⚠ 多条都够格时按 `resources.BLACKLIST_LABEL_ORDER` 取(所有者 2026-09-04:
      「严重程度按这个:品牌 → 知产 → 禁售 → 不可申诉 → 召回 → …」)。
      **原先是「取第一个够格的」,而那个数组来自 `array_agg(DISTINCT …)`**——
      PG 的 DISTINCT 聚合会排序,所以取到的是**字典序**最靠前的那个
      (`BRAND` < `FLAGGED` < … < `RECALL`),不是最严重的。
      ⚠ 只影响写进 `category` 与飞书「来源」列的**标签**,拉不拉黑不受影响
      (任一够格即拉黑)。
    ⚠ 只**读码**,不重判原文 —— 判定在 `problem_scan` 写事件时发生过一次,
      历史事件由 `error_reclass -p scope=events` 回填成新码。
      所有者原话:「产品级的记录已经有产品事件在做了」。
    纯函数,拿假数据就能测。
    """
    hits = []
    for pair in codes or []:
        code = pair[0] if pair else None
        term = pair[1] if pair and len(pair) > 1 else None
        if error_taxonomy.is_permanent(code, term):
            hits.append((code, term))
    if not hits:
        return None
    # 没登记的码排最后(判不准就别让它当标签),同名次按出现序稳定
    return min(hits, key=lambda p: _LABEL_RANK.get(p[0], len(_LABEL_RANK)))


def _judge_events(conn) -> list[dict]:
    """输入:连接 → 输出:按**产品事件**判定该永久拉黑的行。

    ⚠ 这里**不做判定,只做查询** —— 判定在 `problem_scan` 写事件那一刻发生过
    (历史事件由 `error_reclass -p scope=events` 回填成新码)。
    所有者 2026-09-04:「产品级的记录已经有产品事件在做了」。
    唯一的规则是取哪一条:**够格拉黑的那条最高优先级**(`worst_verdict`)。
    """
    with conn.cursor() as cur:
        cur.execute(_HISTORY_SQL)
        rows = cur.fetchall()
    out = []
    for asin, codes, sku, store, reason, latest in rows:
        got = worst_verdict(codes)
        if got is None:
            continue
        code, _term = got
        low = (reason or "").lower()
        out.append({
            "asin": asin, "cat": code, "sku": sku, "store": store,
            "source": source_label(code),
            "reason": reason, "created_at": latest,      # 全文,别截
            "biz_cn": ("biz-cn" in low or "reference code biz" in low)})
    return out


_INSERT_ASIN_SQL = """
INSERT INTO catalog.asin_blacklist
    (asin, category, source, reason, src_store, biz_cn, src_sku, created_at)
VALUES (%(asin)s, %(cat)s, %(source)s, %(reason)s, %(store)s, %(biz_cn)s,
        %(sku)s, %(created_at)s)
ON CONFLICT (asin) DO NOTHING
"""

# ASIN 黑名单重建(rebuild_asin):SKU 清洗后表内键还是订货号原文/多店重复,
# 按标准 asin 重灌(created_at=报错发生时刻,表格日期列因此有意义)。
#
# ⚠ **只删得掉重灌的那些行**(所有者 2026-09-04:「那 10,335 行没有产品事件
#   背书的历史导入需要保留」)。原先是裸 `DELETE FROM catalog.asin_blacklist`
#   —— 而重灌的数据源只有产品事件时间线,**时间线里没有的行删了就再也回不来**:
#   `asin_blacklist_import` 那批一次性导入的历史 ASIN(`LEGACY`)压根没有事件。
#   实测 32,716 行里只有 22,381 行有事件背书,裸删会静默丢掉 10,335 行,
#   而摘要只会说「擦净 32,716 行 → 重灌 24,163 行」,看着像正常。
#
# 判据两条腿都要(与 `_judge_events` 的身份键口径一致):
#   · `coalesce(e.asin, e.sku) = b.asin`  —— 已经按标准 asin 落键的行;
#   · `e.sku = b.src_sku`                 —— 键还是订货号原文的行(重建正是
#     为了给它们换键,只按第一条会漏掉它们、于是换键失败还不报错)。
_ASIN_WIPE_SQL = """
DELETE FROM catalog.asin_blacklist b
WHERE EXISTS (SELECT 1 FROM catalog.product_events e
              WHERE e.event = 'problem_categorized'
                AND (coalesce(e.asin, e.sku) = b.asin
                     OR (b.src_sku IS NOT NULL AND e.sku = b.src_sku)))
"""

#: 重建**碰不到**的行数(没有产品事件背书 ⇒ 重灌不出来 ⇒ 一律保留)。
#: 预览要报这个数:原先报的是**飞书表格行数**,而删的是 PG —— 报的不是要删的那个。
_ASIN_KEEP_COUNT_SQL = """
SELECT count(*) FROM catalog.asin_blacklist b
WHERE NOT EXISTS (SELECT 1 FROM catalog.product_events e
                  WHERE e.event = 'problem_categorized'
                    AND (coalesce(e.asin, e.sku) = b.asin
                         OR (b.src_sku IS NOT NULL AND e.sku = b.src_sku)))
"""

def backfill_counts(conn) -> dict:
    """输入:连接 → 输出:回填预览计数(不写任何东西)。

    ⚠ 预览与真写**必须同一条判据**(都走 `_judge_events`)—— 两处各算各的,
    预览说 3 万、真写写 7 万,而两边看着都正常。
    """
    with conn.cursor() as cur:
        cur.execute(_BACKFILL_COUNT_SQL, {"brandcats": sorted(BRAND_CATEGORIES)})
        brand_cand, total = cur.fetchone()
    keep = _judge_events(conn)
    # ⚠ 「够格永久」≠「真跑会加多少」:INSERT 是 ON CONFLICT DO NOTHING,
    #   已经在表里的一条都不动。预览只报前者,人会以为要写 2.6 万行,
    #   而实际可能只新增几百 —— 或者反过来,把 blacklist_route 刚删的品加回来
    #   却看不出来。**apply 之前真正要看的是「新增」这个数。**
    with conn.cursor() as cur:
        cur.execute("SELECT asin FROM catalog.asin_blacklist")
        have = {a for (a,) in cur.fetchall()}
    fresh = [r for r in keep if r["asin"] not in have]
    with conn.cursor() as cur:
        cur.execute(_ASIN_KEEP_COUNT_SQL)
        untouched = cur.fetchone()[0]
    return {"permanent": len(keep), "brand_cand": brand_cand, "total": total,
            "in_table": len(have), "fresh": len(fresh),
            # 重建碰不到的行(没有事件背书,重灌不出来 ⇒ 保留)
            "untouched": untouched, "fresh_codes": Counter(r["cat"] for r in fresh)}


def backfill_from_events(conn) -> dict:
    """输入:连接 → 输出:ASIN 回填统计。
    品牌渠道的历史重建走 rebuild_brand_channel(单独命令,含清表重灌)。"""
    rows = _judge_events(conn)
    if not rows:
        return {"asin_new": 0}
    with conn.cursor() as cur:
        cur.executemany(_INSERT_ASIN_SQL, rows)
        return {"asin_new": cur.rowcount or 0}


def rebuild_asin_blacklist(conn) -> dict:
    """输入:连接 → 输出:{wiped, inserted, untouched}。按标准 asin 重灌。

    黑名单是时间线的投影,投影可以重投 —— 但**只重投得出来的那一部分**:
    没有产品事件背书的行(`asin_blacklist_import` 那批历史导入)重灌不出来,
    所以一条都不碰(所有者 2026-09-04 定:「需要保留」)。见 `_ASIN_WIPE_SQL`。
    """
    rows = _judge_events(conn)          # ⚠ 先判再擦:判炸了不能留下空表
    with conn.cursor() as cur:
        cur.execute(_ASIN_KEEP_COUNT_SQL)
        untouched = cur.fetchone()[0]
        cur.execute(_ASIN_WIPE_SQL)
        wiped = cur.rowcount or 0
        if rows:
            cur.executemany(_INSERT_ASIN_SQL, rows)
        return {"wiped": wiped, "inserted": len(rows), "untouched": untouched}


# ── 品牌渠道重建(blacklist_push -p rebuild_brand=1,一次性)───────────────────
#
# 两条腿,先认领后推导,同键 DO NOTHING(认领的历史日期优先):
#
# ① **从总表认领**:旧系统后台收集的品牌当年写进旧「禁止品牌收集」表,
#   来源列固定「沃尔玛-品牌限制/沃尔玛-侵权/沃尔玛后台」(legacy_survey:1360),
#   经所有者归拢进总表、risk_sync 镜像进 brand_blacklist——来源以「沃尔玛」
#   开头的镜像行**就是**历史沃尔玛渠道,零成本直接认领。不这么做的话只能
#   为 2,701 个历史 ASIN 补采(采集侧保留期早裁掉了,2026-08-11 实证
#   catalog.products 只命中 1 个)。
# ② **从时间线推导**:C/E 最新类 ASIN × catalog.products.brand,每品牌取
#   最早报错日当 added_date。绕过 ops.dedupe——那本账管"这个 ASIN 别重复
#   采集",不管"渠道该不该记账"。

_CHANNEL_WIPE_SQL = "DELETE FROM catalog.brand_err_hits"

_CHANNEL_SEED_SQL = """
INSERT INTO catalog.brand_err_hits (brand_key, brand, source, added_date, src_sku)
SELECT brand_key, brand, source, added_date, src_sku
FROM catalog.brand_blacklist
WHERE source LIKE '沃尔玛%%'
ON CONFLICT (brand_key) DO NOTHING
"""

_MASTER_WM_COUNT_SQL = """
SELECT count(*) FROM catalog.brand_blacklist WHERE source LIKE '沃尔玛%%'
"""

_CHANNEL_COUNT_SQL = _LATEST_CTE + """
SELECT count(*) FILTER (WHERE coalesce(btrim(p.brand), '') <> ''),
       count(*) FILTER (WHERE coalesce(btrim(p.brand), '') = ''),
       count(DISTINCT lower(btrim(p.brand)))
           FILTER (WHERE coalesce(btrim(p.brand), '') <> '')
FROM latest l
LEFT JOIN catalog.products p ON p.marketplace = 'US' AND p.asin = l.asin
WHERE l.cat = ANY(%(brandcats)s)
"""

_CHANNEL_REBUILD_SQL = _LATEST_CTE + """
INSERT INTO catalog.brand_err_hits
    (brand_key, brand, source, added_date, src_sku, src_store, biz_cn)
SELECT DISTINCT ON (lower(btrim(p.brand)))
       lower(btrim(p.brand)), btrim(p.brand),
       '沃尔玛-' || CASE l.cat WHEN 'C' THEN '品牌' WHEN 'E' THEN '知产' END,
       l.occurred_at::date::text, l.asin, l.store,
       (lower(coalesce(l.reason, '')) LIKE '%%biz-cn%%'
        OR lower(coalesce(l.reason, '')) LIKE '%%reference code biz%%')
FROM latest l
JOIN catalog.products p ON p.marketplace = 'US' AND p.asin = l.asin
WHERE l.cat = ANY(%(brandcats)s) AND coalesce(btrim(p.brand), '') <> ''
ORDER BY lower(btrim(p.brand)), l.occurred_at
ON CONFLICT (brand_key) DO NOTHING
"""


def channel_counts(conn) -> dict:
    """输入:连接 → 输出:渠道重建预览计数(不写任何东西)。"""
    with conn.cursor() as cur:
        cur.execute(_CHANNEL_COUNT_SQL, {"brandcats": sorted(BRAND_CATEGORIES)})
        with_brand, no_brand, brands = cur.fetchone()
        cur.execute(_MASTER_WM_COUNT_SQL)
        master = cur.fetchone()[0]
    return {"with_brand": with_brand, "no_brand": no_brand,
            "brands": brands, "master": master}


# ── 缺品牌候选(brand_scrape 工作流用:推采集补品牌)──────────────────────────

BRAND_SCRAPE_SCOPE = "cleanup:brand_scrape"     # ops.dedupe:已推过采集的 ASIN

_UNCOLLECTED_SQL = _LATEST_CTE + """
SELECT l.asin, l.sku, l.store, l.cat, l.reason,
       (p.asin IS NOT NULL) AS has_brand
FROM latest l
LEFT JOIN catalog.products p
    ON p.marketplace = 'US' AND p.asin = l.asin
   AND coalesce(btrim(p.brand), '') <> ''
WHERE l.cat = ANY(%(brandcats)s)
  AND NOT EXISTS (SELECT 1 FROM ops.dedupe d
                  WHERE d.scope = %(done_scope)s AND d.key = l.sku)
ORDER BY l.occurred_at
"""


def uncollected_brand_items(conn) -> list[dict]:
    """输入:连接 → 输出:未做过品牌收集的 C/E 候选
    [{asin, sku, store, category, reasons, has_brand}]。

    **含 has_brand=True 的行**(采集库已有品牌、只差入账)——2026-08-11
    实测教训:清单只查"仍缺品牌的"会把摄取刚补到货的 99 个永远漏掉入账,
    它们既不在缺口清单里、又没人收集,两侧都不报错。"""
    with conn.cursor() as cur:
        cur.execute(_UNCOLLECTED_SQL,
                    {"brandcats": sorted(BRAND_CATEGORIES),
                     "done_scope": BRAND_ASIN_SCOPE})
        return [{"asin": a, "sku": sku, "store": store,
                 "category": cat, "reasons": reason, "has_brand": bool(hb)}
                for a, sku, store, cat, reason, hb in cur.fetchall()]


def scrape_attempted(conn, asins: list[str]) -> set:
    """输入:连接 + ASIN 列表 → 输出:已推过采集的子集(防无限循环的账)。"""
    if not asins:
        return set()
    with conn.cursor() as cur:
        cur.execute(_PROCESSED_SQL, (BRAND_SCRAPE_SCOPE, asins))
        return {r[0] for r in cur.fetchall()}


def mark_scrape_attempted(conn, asins: list[str], batch_name: str) -> None:
    """输入:连接 + 本次推送的 ASIN + 批次名 → 输出:无(记台账)。"""
    with conn.cursor() as cur:
        for a in asins:
            cur.execute(_MARK_SQL, (BRAND_SCRAPE_SCOPE, a,
                                    json.dumps({"batch": batch_name},
                                               ensure_ascii=False)))


def rebuild_brand_channel(conn) -> dict:
    """输入:连接 → 输出:{wiped, seeded, derived}。擦净重灌(渠道表是
    历史的投影,投影可以重投);先认领(真历史日期)后推导(补漏)。"""
    with conn.cursor() as cur:
        cur.execute(_CHANNEL_WIPE_SQL)
        wiped = cur.rowcount or 0
        cur.execute(_CHANNEL_SEED_SQL)
        seeded = cur.rowcount or 0
        cur.execute(_CHANNEL_REBUILD_SQL, {"brandcats": sorted(BRAND_CATEGORIES)})
        return {"wiped": wiped, "seeded": seeded, "derived": cur.rowcount or 0}
