"""风险追溯引擎:回答「这个品牌(或这批 ASIN)历史上到过哪些店,哪些还在架」。

TRO / 钓鱼订单的第一件事是**波及展开** —— 一家店中招,同品牌的货往往散在
另外几家店里,而"散在哪儿"没有单一真相表:上过架又删掉的只剩事件账本,
删干净的只剩占用台账的 released 行。所以本模块**四证据源取并集**,
宁可多追出一家店让人去看,也不能漏掉一家还在挂货的店:

  ① catalog.walmart_items    在架/曾在架的截面(唯一能判"还在架"的源)
  ② catalog.listing_sources  上架时登记的出身(source_type='amz' 时 key=ASIN)
  ③ catalog.product_events   一生的病历(提交上架/下架/删除都留痕,行删了也在)
  ④ catalog.claims           占用台账,**含 released 行** —— 货和记录都清干净之后,
                             它是"这个品牌当初属于谁"的唯一答案(schema.sql claims 表注)

## still_listed 的三个条件(少一个都会误判)

  a. `missing_since IS NULL`         —— 最近一轮全量扫描仍见到它;
  b. `coalesce(upper(lifecycle_status),'ACTIVE') = 'ACTIVE'` —— ⚠ **退市品的
     missing_since 也是 NULL**(catalog_sync 显式扫一轮 RETIRED,它没缺席,
     只是退市了;services/alloc_survey._SQL_ONLINE 同款判法与教训);
  c. **店还在册** —— 已从凭证表删掉的死店,它的 walmart_items 行永久冻结为
     "在架"(docs/allocation_plan.md §9.4)。这一条本模块**不自己查**:
     在册集合要调飞书凭证表,services 层不该每次追溯都去敲外部接口。
     由调用方(workflow 层)传 `registered=stores.registered_names()` 进来;
     不传就只算 a+b,并在返回里把原始判定留在 `listed_in_items` 里,
     谁也不会以为自己拿到的是终判。

## 品牌归一只在 Python 做(本模块的硬纪律)

仓里**并存三套不一致的品牌归一**,这是事实不是笔误:
  · services/brand_key.normalize —— 小写 + 内部空白压单空格(占用键/黑名单键)
  · services/blacklist           —— SQL 侧 `lower(btrim(brand))`(不压内部空白)
  · services/risk_gate           —— `casefold()`(连大小写折叠算法都不同)
它们各自对着各自的下游,谁也不能单方面改。追溯要跨这几张表,若在 SQL 里
再写第四套归一,就是第四个各漂各的版本;而且 `lower(btrim(brand))` 上没有
表达式索引,写在 WHERE 里等于对 128 万行 products 做函数扫。

所以:**归一一律调 `services/brand_key`(唯一出处),SQL 侧一个归一函数都不写。**
Python 端把品牌原文展开成若干**候选原文串**,SQL 用 `= ANY(...)` 等值比
(先例:services/audit_l2 的 R5 商标反查 `brand_upper = ANY(%s)`)。
候选与真值对不上时后果是"少追出几个 ASIN",由四证据源里的另外三源兜住。

纯查询模块:一行都不写库。
"""

import logging

from services import brand_key, stores

logger = logging.getLogger("services.risk_trace")

# 证据标签(输出里 evidence 的取值;顺序即四证据源的查询顺序)
EV_ITEM = "在架表"          # catalog.walmart_items
EV_SOURCE = "来源登记"      # catalog.listing_sources
EV_EVENT = "事件账本"       # catalog.product_events
EV_CLAIM_PRODUCT = "占用-产品"   # catalog.claims kind='product'
EV_CLAIM_BRAND = "占用-品牌"     # catalog.claims kind='brand'


def brand_candidates(brand_raw) -> list[str]:
    """输入:品牌原文 → 输出:SQL 侧 `lower(btrim(...))` 可能等于的候选串(去重保序)。

    两形:① 原文小写去两端空白(数据库里存的多半就是这个样子);
    ② brand_key.normalize 的压空格形 —— **输入**自带多余空白时靠它命中库里的
    干净值。两形相同就只留一个,绝大多数品牌走的都是这条路。

    ⚠ 反过来不成立:库里存的是脏值(内部双空格)而输入干净时,等值比不上。
    这是"SQL 侧不做归一"的代价(头注),不是 bug —— 漏掉的那几个 ASIN
    由另外三个证据源兜住,而修它的唯一办法是在 SQL 里再写第四套归一。
    """
    raw = str(brand_raw or "")
    out: list[str] = []
    for c in (raw.strip().lower(), brand_key.normalize(raw)):
        if c and c not in out:
            out.append(c)
    return out


# ⚠ 不带 ORDER BY 之外的任何归一函数:brand 列上没有表达式索引,products 约
# 128 万行,这里是**按候选串等值比**的顺序扫(TRO/钓鱼触发频率极低,可接受)。
# 若哪天变成高频调用,再建 `products ((lower(btrim(brand))))` 表达式索引 ——
# 现在不建:128 万行的表达式索引要占空间、拖每一次 products 写入,为一年
# 几次的查询不值(话头记在 docs/backlog.md §十一,别顺手就建)。
_BRAND_ASINS_SQL = """
SELECT asin FROM catalog.products
WHERE marketplace = 'US'
  AND lower(btrim(brand)) = ANY(%(cands)s::text[])
UNION
SELECT asin FROM catalog.products
WHERE marketplace = 'US'
  AND lower(btrim(slow->>'manufacturer')) = ANY(%(cands)s::text[])
  AND (coalesce(btrim(brand), '') = ''
       OR lower(btrim(brand)) = ANY(%(placeholders)s::text[]))
ORDER BY 1
"""


def asins_of_brand(conn, brand_raw) -> list[str]:
    """输入:连接 + 品牌原文 → 输出:该品牌名下的 ASIN(升序;占位符品牌返回空)。

    manufacturer 兜底腿只在 **brand 为空或占位符**时算数(与 risk_gate 双字段
    实证同源:亚马逊大量商品 brand=Generic,真品牌在 manufacturer);brand 有
    真值时不许拿 manufacturer 再捞一遍 —— 代工厂给几十个品牌代工,那是把
    不相干的产品拉进同一次波及展开。

    ⚠ 占位符品牌(OEM / Generic / 无品牌 …)**直接返回空**并打 warning:
    按 "OEM" 展开波及 = 一次成千上万个不相干产品的大面积误标,方向与
    services/brand_key 的保守取舍一致(宁可漏,不可乱)。调用方要区分
    "占位符拒绝"与"真的一个都没有",先自己调 `brand_key.is_placeholder`,
    或改用 `stores_of_brand`(它用 bkey=None 明说这一情形)。
    """
    if brand_key.is_placeholder(brand_raw):
        logger.warning("risk_trace:品牌 %r 是占位符,拒绝展开(会大面积误标)",
                       brand_raw)
        return []
    cands = brand_candidates(brand_raw)
    with conn.cursor() as cur:
        # 占位符表从 brand_key 传进 SQL,不在 SQL 里另抄一份(唯一出处纪律)
        cur.execute(_BRAND_ASINS_SQL,
                    {"cands": cands, "placeholders": sorted(brand_key.PLACEHOLDERS)})
        return [r[0] for r in cur.fetchall()]


# ① 在架表:命中 walmart_items_sku_idx。still_listed 的 a+b 两条件在 SQL 里
# 逐店 bool_or —— 同一店可能既有在架的又有已缺席的,只要还有一件在架就是在架。
_ITEMS_SQL = """
SELECT store,
       bool_or(missing_since IS NULL
               AND coalesce(upper(lifecycle_status), 'ACTIVE') = 'ACTIVE'),
       array_agg(DISTINCT sku),
       min(created_at), max(last_seen_at)
FROM catalog.walmart_items
WHERE sku = ANY(%(asins)s::text[])
GROUP BY store
"""

# ② 来源登记:source_type='amz' 时 source_key 就是 ASIN(schema.sql 表注)。
# 反查 source_key 原本全表扫,listing_sources_key_idx 是为这条查询建的。
_SOURCES_SQL = """
SELECT store, array_agg(DISTINCT source_key), min(created_at), max(created_at)
FROM catalog.listing_sources
WHERE source_type = 'amz' AND source_key = ANY(%(asins)s::text[])
GROUP BY store
"""

# ③ 事件账本:身份键表达式必须与 product_events_identity_idx **逐字一致**
# (schema.sql:`((coalesce(asin, sku)), occurred_at DESC)`)。⚠ 写成
# `asin = ANY(...) OR sku = ANY(...)` 语义相同但用不上索引,几百万行全表扫 ——
# audit_listing_conflicts 视图就是这么把生产查询挂死的(schema.sql 表注)。
_EVENTS_SQL = """
SELECT store, array_agg(DISTINCT coalesce(asin, sku)),
       min(occurred_at), max(occurred_at)
FROM catalog.product_events
WHERE coalesce(asin, sku) = ANY(%(asins)s::text[]) AND store IS NOT NULL
GROUP BY store
"""

# ④ 占用台账:**不带 status 过滤**。released 行不是垃圾,它是"这个品牌当初
# 属于谁"的唯一答案(claims 表注);按 status='active' 过滤会让"已释放的
# 历史归属"整个消失,而波及展开要的恰恰是历史。
# 两个常量而不是一个带 `claim_key = NULL` 的空转腿:显式路由(CLAUDE.md
# "严禁隐式降级"),也让人一眼看出有没有走品牌腿。
_CLAIMS_SQL = """
SELECT store, kind, array_agg(DISTINCT claim_key),
       min(claimed_at), max(coalesce(released_at, claimed_at))
FROM catalog.claims
WHERE kind = 'product' AND claim_key = ANY(%(asins)s::text[])
GROUP BY store, kind
"""

_CLAIMS_WITH_BRAND_SQL = """
SELECT store, kind, array_agg(DISTINCT claim_key),
       min(claimed_at), max(coalesce(released_at, claimed_at))
FROM catalog.claims
WHERE (kind = 'product' AND claim_key = ANY(%(asins)s::text[]))
   OR (kind = 'brand' AND claim_key = %(bkey)s::text)
GROUP BY store, kind
"""


def _merge(acc: dict, store, label: str, ids, first_at, last_at,
           listed=None) -> None:
    """输入:累加器 + 一行证据 → 输出:无(就地并入)。时间取跨源 min/max。"""
    if not store:
        return
    rec = acc.setdefault(store, {"evidence": [], "asins": set(),
                                 "listed_in_items": False,
                                 "first_at": None, "last_at": None})
    if label not in rec["evidence"]:
        rec["evidence"].append(label)
    rec["asins"].update(i for i in (ids or ()) if i)
    if listed:
        rec["listed_in_items"] = True
    if first_at and (rec["first_at"] is None or first_at < rec["first_at"]):
        rec["first_at"] = first_at
    if last_at and (rec["last_at"] is None or last_at > rec["last_at"]):
        rec["last_at"] = last_at


def stores_of_asins(conn, asins, brand_key_norm=None,
                    registered: set[str] | None = None) -> dict[str, dict]:
    """输入:连接 + ASIN 列表(+品牌归一键 +在册店集合)→ 输出:{店名: 追溯记录}。

    记录形状:`{still_listed, listed_in_items, registered, evidence, asins,
    first_at, last_at}`。四证据源取并集,evidence 按查询顺序列出命中的源。

    `brand_key_norm` 给的是 `services/brand_key` 的归一键,只用于 claims 的
    品牌腿(kind='brand' 的 claim_key 就是这个键);为 None 时那条腿不查。
    `registered` 见模块头注 still_listed 第三条 —— 不传只算 a+b。

    店名顺序用 `services/stores.sort_key`,**不用 SQL ORDER BY**:PG 的
    collation 在主排序级把中文整个忽略,同一条 SQL 换台机器结果都不同。
    """
    asins = sorted({str(a).strip() for a in (asins or ()) if str(a or "").strip()})
    acc: dict[str, dict] = {}
    with conn.cursor() as cur:
        if asins:
            cur.execute(_ITEMS_SQL, {"asins": asins})
            for store, listed, skus, first_at, last_at in cur.fetchall():
                _merge(acc, store, EV_ITEM, skus, first_at, last_at, listed=listed)
            cur.execute(_SOURCES_SQL, {"asins": asins})
            for store, keys, first_at, last_at in cur.fetchall():
                _merge(acc, store, EV_SOURCE, keys, first_at, last_at)
            cur.execute(_EVENTS_SQL, {"asins": asins})
            for store, ids, first_at, last_at in cur.fetchall():
                _merge(acc, store, EV_EVENT, ids, first_at, last_at)
        if brand_key_norm:
            cur.execute(_CLAIMS_WITH_BRAND_SQL,
                        {"asins": asins, "bkey": brand_key_norm})
        elif asins:
            cur.execute(_CLAIMS_SQL, {"asins": asins})
        if asins or brand_key_norm:
            for store, kind, keys, first_at, last_at in cur.fetchall():
                label = EV_CLAIM_BRAND if kind == "brand" else EV_CLAIM_PRODUCT
                # 品牌占用的 claim_key 是品牌键不是 ASIN,不并进 asins 里
                _merge(acc, store, label,
                       () if kind == "brand" else keys, first_at, last_at)

    out: dict[str, dict] = {}
    for store in sorted(acc, key=stores.sort_key):
        rec = acc[store]
        in_reg = None if registered is None else (store in registered)
        out[store] = {
            "still_listed": rec["listed_in_items"] if in_reg is None
                            else (rec["listed_in_items"] and in_reg),
            "listed_in_items": rec["listed_in_items"],   # 未过在册的原始判定
            "registered": in_reg,                        # None = 没校验过
            "evidence": list(rec["evidence"]),
            "asins": sorted(rec["asins"]),
            "first_at": rec["first_at"], "last_at": rec["last_at"],
        }
    return out


def stores_of_brand(conn, brand_raw, manufacturer=None,
                    registered: set[str] | None = None):
    """输入:连接 + 品牌原文(+制造商 +在册店集合)→ 输出:(品牌键, ASIN 列表, {店: 记录})。

    品牌键为 None = **这个品牌不参与展开**(占位符,或 brand/manufacturer
    都是占位符),此时 ASIN 与店都是空 —— 调用方据此区分"拒绝展开"与
    "查了但一家都没有",两者的处置完全不同(前者要人工给个真品牌)。
    """
    bkey = brand_key.brand_key(brand_raw, manufacturer)
    if bkey is None:
        logger.warning("risk_trace:brand=%r manufacturer=%r 都是占位符,不展开",
                       brand_raw, manufacturer)
        return None, [], {}
    # 用**产出 bkey 的那个字段原文**去查 products:brand 是占位符时 bkey 来自
    # manufacturer,再拿 brand 原文查就是拿 "Generic" 去查
    raw = brand_raw if not brand_key.is_placeholder(brand_raw) else manufacturer
    asins = asins_of_brand(conn, raw)
    return bkey, asins, stores_of_asins(conn, asins, bkey, registered=registered)
