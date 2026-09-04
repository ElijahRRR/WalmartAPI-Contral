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

# ⚠ 形态提取那个函数**不再直接 import**:身份只从登记簿那条路取
#   (`sku_asin.pick_asin` / `resolve_many` 内部才回落形态腿),这儿留个裸入口
#   就会有人又写第二份规则。守门的文本轨连注释里的函数名一起拦,所以这段
#   只说"形态腿"不写它的名字 —— 拦的正是"先在注释里写好再抄进代码"那一步。
from services import error_source, error_taxonomy, sku_asin, sku_codec

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
#: ⚠ **码 → 渠道来源中文** 的唯一出处(2026-09-04 补)。原先 SQL 里写死
#: `CASE l.cat WHEN 'C' … WHEN 'E' …`,2026-09-03 换轨把 cat 换成新码之后
#: 那两个 WHEN 一个都不命中 ⇒ `'沃尔玛-' || NULL` = **NULL**,渠道重建出来的
#: 每一行 source 都是空 —— 而且不报错,SQL 照常 INSERT。键与文案分两处写
#: 就是这么坏的:改了一处,另一处静默失配。
BRAND_CATEGORIES_CN = {"BRAND": "品牌", "IP": "知产"}
BRAND_CATEGORIES = frozenset(BRAND_CATEGORIES_CN)

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

    黑名单键 = 登记簿反查出的 source_key(切码后唯一通路),查不到回落形态提取,
    再不行用订货号原文兜底并**告警计数**,宁可键不标准也不丢行(口径见 D-0b-1);
    订货号原文永远存 src_sku 溯源。"""
    # ⚠ 预取的入选判据必须与下面那一行**逐字同一个** —— 用 `in PERMANENT` 预取、
    #   用 `is_permanent` 落库,`OTHER` + 显式词条那批就不在预取名单里,登记簿
    #   一次都没查就直接落进「原文兜底」,计数虚高而键还是错的。
    asin_of = sku_asin.resolve_many(
        conn, [(it.get("store"), it["sku"]) for it in items
               if error_taxonomy.is_permanent(it.get("category"),
                                              it.get("unlisted_term"))])
    fell_back: list[str] = []
    added = 0
    with conn.cursor() as cur:
        for it in items:
            code = it.get("category")
            # ⚠ 走 `is_permanent` 而不是 `code in PERMANENT`:`OTHER` 是混装桶
            # (显式杂项 + 兜底),所有者只让 business decision / trust & safety
            # 两个词条算永久拉黑,判据在引擎里,这儿不重新匹配一遍。
            if not error_taxonomy.is_permanent(code, it.get("unlisted_term")):
                continue
            asin = asin_of.get((it.get("store"), it["sku"]))
            if not asin:
                asin = it["sku"]
                fell_back.append(it["sku"])
            cur.execute(_ASIN_SQL, (
                asin, code, source_label(code),
                (it.get("reasons") or "") or None,      # 全文,别截(见头注)
                it.get("store"), is_biz_cn(it.get("reasons")), it["sku"]))
            added += cur.rowcount or 0
    if fell_back:
        logger.warning("ASIN 黑名单:%d 个 sku 登记簿与形态都解不出,按订货号原文"
                       "入键(键不标准,按 ASIN 的重上架拦截对它们不生效):%s",
                       len(fell_back), fell_back[:5])
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
    # 去重键是 sku 不是 asin:切码后 sku 全局唯一 ⇒ 同一 ASIN 在两家店各收一次
    # 品牌(渠道表/闸门表都 DO NOTHING 幂等,只多一次查库);改成 asin 会改变
    # 存量三段式行的去重粒度,不在本批次范围(见 D-0b-3)。
    # ⚠ 折叠后的 it["store"] 只是**任意一家**(同 sku 多店时后写覆盖先写),
    #   只准用于溯源列,**不得当查询键** —— 下面按「该 sku 出现过的全部店」反查。
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
        # 登记簿反查是主路,形态提取只是存量兜底;采集库按**清洗后的标准 asin**
        # 查(订货号原文直接查必然全空——2026-08-11 生产实证:2,702 个 C/E 品牌
        # 0 命中,教训在此)
        stores_of: dict[str, list] = {}
        for it in items:
            if it.get("category") in BRAND_CATEGORIES:
                stores_of.setdefault(it["sku"], []).append(it.get("store"))
        for sku in stores_of:                      # 定序:与 items 顺序无关
            stores_of[sku] = sorted(set(stores_of[sku]),
                                    key=lambda s: (s is None, s or ""))
        resolved = sku_asin.resolve_many(
            conn, [(st, sku) for sku in todo for st in stores_of.get(sku, [])])
        asin_of = {sku: next((resolved[(st, sku)] for st in stores_of.get(sku, [])
                              if (st, sku) in resolved), None) or sku
                   for sku in todo}
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

#: 身份(ident)= coalesce(登记簿 source_key, asin, sku)：登记簿是切码后的
#: 唯一通路,其次是 record_many 清洗出的标准码,再不行才用订货号原文兜底
#: (口径与 record_asins 一致,见 D-0b-1)。多个订货号(不同店同一产品)归并到
#: 同一 asin,看**产品级**全局。
#: ⚠ LEFT JOIN 且限 source_type='amz'：非 amz 行的 source_key 是 GTIN/offer_id,
#:   拿它当 ASIN 黑名单键是错的类型(与 0a 全部收口点的身份表达式同形)。
#: ⚠ 身份表达式**只在这一处出生**(`ev` 这个 CTE)：下面的 `latest`(品牌渠道)
#:   与 `_HISTORY_SQL`(黑名单)都建在它上面。两边各写一份 `coalesce(...)`,
#:   漂了就是同一个产品在两条链里是两个身份。
_EV_CTE = """
WITH ev AS (
    SELECT e.*, coalesce(ls.source_key, e.asin, e.sku) AS ident
    FROM catalog.product_events e
    LEFT JOIN catalog.listing_sources ls
           ON ls.store = e.store AND ls.sku = e.sku AND ls.source_type = 'amz'
    WHERE e.event = 'problem_categorized')"""

#: 「每个 asin 的最新一条」—— **只给品牌渠道用**(brand_err_hits 的语义就是
#: 「当前这个品牌还在不在问题里」)。⚠ **黑名单入选那条路已经不用它了**：
#: 黑名单是「一次入选、永久禁止」,判据是下面的 `_HISTORY_SQL`(全部历史里
#: 够格拉黑的那条优先),两者别混。(回填预览的 brand_cand/total/opaque
#: 三个只读计数仍然是「最新一条」语义,故也建在它上面。)
_LATEST_CTE = _EV_CTE + """,
latest AS (
    SELECT DISTINCT ON (ident) ident AS asin,
           sku, store, occurred_at,
           detail->>'category' AS cat,
           coalesce(detail->>'reason', detail->>'reasons') AS reason
    FROM ev
    ORDER BY ident, occurred_at DESC)
"""

#: 黑名单入选那条判据:一个 asin 的**全部历史报错码**聚一行(去留由
#: `worst_verdict` 算,「够格拉黑的那条最高优先级」)。
#: ⚠ 分组键是 `ident`(登记簿 source_key 优先),**不是裸 `coalesce(asin, sku)`**:
#:   切码后 sku 是我们自己生成的 12 位不透明码,拿它分组 = 同一个产品的历史被
#:   拆成一码一份,「历史里那条禁售」永远聚不到一起(所有者第 4 点:
#:   「如果只跟着 sku 走,sku 又是由我们系统生成的,后面会追不到问题产品的来源码」)。
#:   `_judge_events` 出来的 asin 就是 `blacklist_route` 的保留集,身份错 = 它删错行。
#: ⚠ 键名两个都认:写入方(`problem_scan` / `cleanup_history`)写的是
#:   `detail->>'reason'`(**单数**),库里历史上还有一批写的是 `reasons`(复数)。
#:   2026-09-04 查出读写不一致导致 events 那一级大面积空转 —— 这里 `coalesce`
#:   两个都取,别再各写一半。
_HISTORY_SQL = _EV_CTE + """
SELECT ident AS asin,
       array_agg(DISTINCT ARRAY[detail->>'category',
                                detail->>'taxonomy_term']) AS codes,
       (array_agg(sku   ORDER BY occurred_at DESC))[1] AS sku,
       (array_agg(store ORDER BY occurred_at DESC))[1] AS store,
       (array_agg(coalesce(detail->>'reason', detail->>'reasons')
                  ORDER BY occurred_at DESC))[1] AS reason,
       max(occurred_at) AS latest
FROM ev
WHERE coalesce(detail->>'category', '') <> ''
GROUP BY 1
"""


#: 预览用的三个只读数:品牌渠道候选 + 时间线总量 + 不透明码键告警
#: (该永久拉黑多少个由 `_judge_events` 算,不在这条 SQL 里)。
#: ⚠ 建在 `_LATEST_CTE` 上而不是另抄一份 `DISTINCT ON (coalesce(asin, sku))`:
#:   预览与实时链必须是同一个身份表达式,抄第二份就会漂。
# `opaque` 是**只读告警计数**:键形如 12 位不透明码 = 登记簿查不到那批,拦不住
# 任何东西(它匹配不到任何真 ASIN,也不误伤)。只计数不过滤 —— 过滤会丢行,
# 那是 D-0b-1 要拍板的口径,不混进零行为变化批次。字符类**不许手写字面量**:
# 从 services.sku_codec 的字母表常量拼进来(字母表唯一之家)。
_BACKFILL_COUNT_SQL = _LATEST_CTE + f"""
SELECT count(*) FILTER (WHERE cat = ANY(%(brandcats)s)) AS brand_cand,
       count(*) AS total,
       count(*) FILTER (WHERE asin ~ '^[{sku_codec._ALPHABET}]{{{sku_codec._LEN}}}$'
                          AND asin ~ '[A-Z]') AS opaque
FROM latest
"""


def worst_verdict(codes):
    """输入:一个 asin 的**全部事件码** `[[code, term], …]` → 输出:够格永久拉黑的
    那个 `(code, term)`;一个都不够格给 None。

    ⚠ **够格拉黑的那条最高优先级**(所有者 2026-09-04),其余只是记录。
    ⚠ 只**读码**,不重判原文 —— 判定在 `problem_scan` 写事件时发生过一次,
      历史事件由 `error_reclass -p scope=events` 回填成新码。
      所有者原话:「产品级的记录已经有产品事件在做了」。
    纯函数,拿假数据就能测。
    """
    for pair in codes or []:
        code = pair[0] if pair else None
        term = pair[1] if pair and len(pair) > 1 else None
        if error_taxonomy.is_permanent(code, term):
            return code, term
    return None


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
# 擦净按标准 asin 重灌(created_at=报错发生时刻,表格日期列因此有意义)。
_ASIN_WIPE_SQL = "DELETE FROM catalog.asin_blacklist"

def backfill_counts(conn) -> dict:
    """输入:连接 → 输出:回填预览计数(不写任何东西)。

    ⚠ 预览与真写**必须同一条判据**(都走 `_judge_events`)—— 两处各算各的,
    预览说 3 万、真写写 7 万,而两边看着都正常。
    """
    with conn.cursor() as cur:
        cur.execute(_BACKFILL_COUNT_SQL, {"brandcats": sorted(BRAND_CATEGORIES)})
        brand_cand, total, opaque = cur.fetchone()
    keep = _judge_events(conn)
    # ⚠ 「够格永久」≠「真跑会加多少」:INSERT 是 ON CONFLICT DO NOTHING,
    #   已经在表里的一条都不动。预览只报前者,人会以为要写 2.6 万行,
    #   而实际可能只新增几百 —— 或者反过来,把 blacklist_route 刚删的品加回来
    #   却看不出来。**apply 之前真正要看的是「新增」这个数。**
    with conn.cursor() as cur:
        cur.execute("SELECT asin FROM catalog.asin_blacklist")
        have = {a for (a,) in cur.fetchall()}
    fresh = [r for r in keep if r["asin"] not in have]
    # `opaque` 是 D-0b-1 那一档只读告警,原样透出(零时摘要逐字不变)。
    return {"permanent": len(keep), "brand_cand": brand_cand, "total": total,
            "opaque": opaque,
            "in_table": len(have), "fresh": len(fresh),
            "fresh_codes": Counter(r["cat"] for r in fresh)}


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
    """输入:连接 → 输出:{wiped, inserted}。擦净按标准 asin 重灌
    (黑名单是时间线的投影,投影可以重投——与品牌渠道重建同一权衡)。"""
    rows = _judge_events(conn)          # ⚠ 先判再擦:判炸了不能留下空表
    with conn.cursor() as cur:
        cur.execute(_ASIN_WIPE_SQL)
        wiped = cur.rowcount or 0
        if rows:
            cur.executemany(_INSERT_ASIN_SQL, rows)
        return {"wiped": wiped, "inserted": len(rows)}


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

#: `_CHANNEL_REBUILD_SQL` 里那段 CASE 由 `BRAND_CATEGORIES_CN` 派生 —— 手写
#: 第二份就是 2026-09-03 换轨漏改的那个坑(SQL 里还是旧码 C/E,source 全 NULL)。
#: 码是常量里的字面量、不来自外部输入,拼进 SQL 安全。
_BRAND_CN_CASE = "".join(f" WHEN '{c}' THEN '{cn}'"
                         for c, cn in sorted(BRAND_CATEGORIES_CN.items()))

_CHANNEL_REBUILD_SQL = _LATEST_CTE + """
INSERT INTO catalog.brand_err_hits
    (brand_key, brand, source, added_date, src_sku, src_store, biz_cn)
SELECT DISTINCT ON (lower(btrim(p.brand)))
       lower(btrim(p.brand)), btrim(p.brand),
       '沃尔玛-' || CASE l.cat""" + _BRAND_CN_CASE + """ END,
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
