"""维护意图 provider(maintenance 工作流的可插拔数据源)。

意图 dict 统一形态(管道契约,provider 只管产出):
  {"store": 店铺名, "sku": SKU, "kind": "title"|"price"|"inventory"|"delete",
   "old": 旧值, "new": 新值}
  title 意图额外携带 product_type / product_id(UPC)两键(feed 载荷必需;
  按「三缺一跳过」旧防线过滤后再产出)。
清零是 inventory 的 new=0 特例,由 zero_intents 产出。
delete 是唯一的**不可逆**类(采集永久偏移 / 商品不存在),由 delete_intents
产出,old/new = 在线/删除,并带 reason 与 label(维护记录 C 列)。

**驱动方式的变化(2026-08-09,采集接入后)**:旧系统读飞书「在线产品总表」
的运营决策列(是否更新价格+新价 / 是否更新库存+新库存 / amz标题+相似度);
新系统的在线产品总表是程序写的投影,没有那些列——改为**从产品中心自动算**:
  amz 最新观测(catalog.latest_snapshot / products.slow)
    × 沃尔玛在线现值(catalog.walmart_items)
    × 定价规则(限额表倍率,services/pricing)
  → 差异超阈值才产出意图。人工要临时改某个 SKU 走 product_clear 同款的
  驱动表(尚未建;需要时再说),自动链不承担一次性人工指令。

路由铁律(所有者定稿 2026-08-07):意图产出必须 JOIN catalog.listing_sources
按出身路由——amz 快照驱动的意图只作用于 source_type='amz' 的行;
"源数据查不到"绝不可对 match/unknown 行推导出清库存/删除等破坏动作
(旧系统按 SKU 格式排除的补丁废止,以登记簿为准)。

路由阈值与标题载荷结构逐字移植旧系统(erpAPI 沃尔玛商品维护,实证勿改)。
"""

import logging
import os

from services import mp_mapper, order_audit, pricing, store_limits

logger = logging.getLogger("services.maintenance_intents")

# 单店该类型条数 ≤ 阈值走同步 PUT(结果当场已知),超过走 feed;
# 标题无同步接口永远 feed(旧 SYNC_THRESHOLDS 原值)
SYNC_THRESHOLDS = {"price": 5, "inventory": 10}

# M 列占位符:命中则跳过标题维护(否则 Walmart 退回;旧系统原值)
TITLE_PLACEHOLDERS = {"[商品不存在]"}

# 改价触发阈值:差额绝对值 ≥ 1 分且相对变化 ≥ 该比例才提交
# (亚马逊价格日内小幅抖动很常见,逐分钱跟会把 feed 配额烧光)
PRICE_MIN_DELTA = 0.01
PRICE_MIN_RATIO = 0.01          # 1%

# 单轮每类意图上限(防某天采集侧大面积变动 → 一次几万条 feed)
MAX_INTENTS_PER_KIND = 5000

# 删除类专属:批次数门槛与单店单轮上限
MIN_OFFSET_BATCHES = 1          # 出现一次即删(所有者:偏移了就不会恢复)
DELETE_PER_STORE = 300          # 限额表「下架限制」缺该店时的退路(会告警)
LONG_OOS_DAYS = 15              # 连续这么多天没有库存 → 删除(所有者定稿)

_SQL_ZERO = """
SELECT store, sku, avail_qty FROM catalog.walmart_items
WHERE store = ANY(%s) AND missing_since IS NULL AND avail_qty > 0
"""
# avail_qty > 0 是显式条件:旧系统 `None != 0` 也触发清零是坑(库存未知的行
# 被盲清)——新规矩:未知库存不动,只清确知有货的行。

# amz 侧最新观测 × 沃尔玛在线现值。三个 provider 共用一条取数(一次 JOIN,
# 各自挑字段),避免同一张大表扫三遍。
#   · 只取 source_type='amz'(路由铁律)
#   · 只取在架行(missing_since IS NULL)
#   · zip_verify='mismatch' 的观测不参与(请求邮编未生效,价格不属于该分组)
#   · stockzero 店整店排除(它们归 zero_intents,不能被自动同步顶回去)
_SQL_AMZ_JOIN = """
SELECT w.store, w.sku, w.product_name, w.product_type, w.upc,
       w.price AS wm_price, w.avail_qty,
       s.price AS amz_price, s.stock_count, s.delivery_days,
       p.slow, s.fulfillment, s.shipping,
       -- 处置三信号(所有者定稿 2026-08-16):采集结局 / 亚马逊在架状态 /
       -- 契约的 fast.stock_state。三者取自**同一条最新快照**,不能各查各的
       -- ——分开查会出现"按昨天的 outcome 配今天的库存"这种错配。
       s.outcome, s.stock_status, s.stock_state
FROM catalog.walmart_items w
JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz'
JOIN catalog.products p ON p.marketplace = 'US' AND p.asin = w.sku
LEFT JOIN LATERAL (
    -- 配送方式(FBA/FBM)决定用哪套定价区间。采集契约的 fast 段没把它列成
    -- 一等字段,但 raw 是"裁剪后的原样载荷",is_fba 就在里面(采集侧
    -- worker/parser._parse_fulfillment 读 buybox 的 Ships from 行)。
    SELECT price, stock_count, delivery_days, shipping, outcome, stock_state,
           raw ->> 'is_fba' AS fulfillment,
           -- 亚马逊在架状态原文(采集侧存 raw,契约未列为一等字段)
           raw ->> 'stock_status' AS stock_status
    FROM catalog.latest_snapshot l
    WHERE l.marketplace = 'US' AND l.asin = w.sku
      AND coalesce(l.scrape_params ->> 'zip_verify', '') <> 'mismatch'
    ORDER BY l.scraped_at DESC LIMIT 1
) s ON true
WHERE w.missing_since IS NULL
  AND w.published_status = 'PUBLISHED'
  AND NOT (w.store = ANY(%s))
"""


# 采集永久偏移(variant_offset)的在线行 → 删除意图。
# · 同样守路由铁律:只删 source_type='amz' 的行(破坏动作绝不碰 match/unknown)
# · 店铺 ACTIVE 才删(无 KPI 记录 fail-open,与其他闸同口径)
# · snapshots.outcome 在补列之前的历史行是 NULL,那些都是成功采集,按 ok 处理
#   ——否则老 SKU 会因为"查不到 ok 快照"被误判该删
_SQL_VARIANT_OFFSET = """
WITH vo AS (
    SELECT asin,
           count(DISTINCT batch_name) AS batches,
           min(recorded_at) AS first_seen,
           max(recorded_at) AS last_seen
    FROM ops.scrape_failures
    WHERE error_type = 'variant_offset'
    GROUP BY asin
), latest_status AS (
    SELECT DISTINCT ON (store) store, store_status
    FROM ops.store_kpi_daily ORDER BY store, data_date DESC
)
SELECT w.store, w.sku, vo.batches, vo.first_seen, vo.last_seen
FROM vo
JOIN catalog.walmart_items w ON w.sku = vo.asin
JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz'
LEFT JOIN latest_status s ON s.store = w.store
WHERE w.missing_since IS NULL
  AND w.published_status = 'PUBLISHED'
  AND (s.store_status IS NULL OR upper(s.store_status) = 'ACTIVE')
  AND vo.batches >= %(min_batches)s
  AND NOT EXISTS (
        SELECT 1 FROM catalog.snapshots sn
        WHERE sn.asin = vo.asin
          AND COALESCE(sn.outcome, 'ok') = 'ok'
          AND sn.scraped_at > vo.last_seen)
ORDER BY w.store, w.sku
"""


# 连续 N 天没有库存 → 删除(所有者定稿 2026-08-09)。
# 三道判据,缺一个都会误删:
#   1. 窗口内**没有任何一条"有货"观测**(stock_count>0 或 stock_state='in_stock')
#   2. **至少有一条明确的缺货观测**——防"这 15 天页面一直采不全、全是 unknown"
#      被读成"一直缺货"(采不到 ≠ 没货,这是删除,不是清零,不能含糊)
#   3. 窗口**两端都有观测**(最早一条在窗口起点 36 小时内、最新一条在近 3 天内)
#      ——防"15 天前采过一次、昨天采过一次,中间断 13 天"被当成连续观测
# 只看 outcome=ok 的观测(降级采集的 fast 段基本是空的,拿它判缺货是冤案)。
_SQL_LONG_OOS = """
WITH win AS (
    SELECT asin,
           min(scraped_at) AS first_seen,
           max(scraped_at) AS last_seen,
           count(*) AS obs,
           count(*) FILTER (WHERE stock_count > 0
                               OR stock_state = 'in_stock') AS in_stock_obs,
           count(*) FILTER (WHERE stock_state = 'out_of_stock'
                               OR stock_count = 0) AS oos_obs
    FROM catalog.snapshots
    WHERE scraped_at > now() - make_interval(days => %(days)s)
      AND COALESCE(outcome, 'ok') = 'ok'
    GROUP BY asin
    HAVING count(*) FILTER (WHERE stock_count > 0
                               OR stock_state = 'in_stock') = 0
       AND count(*) FILTER (WHERE stock_state = 'out_of_stock'
                               OR stock_count = 0) > 0
       AND min(scraped_at) <= now() - make_interval(days => %(days)s)
                            + interval '36 hours'
       AND max(scraped_at) >= now() - interval '3 days'
), latest_status AS (
    SELECT DISTINCT ON (store) store, store_status
    FROM ops.store_kpi_daily ORDER BY store, data_date DESC
)
SELECT w.store, w.sku, win.obs, win.first_seen, win.last_seen
FROM win
JOIN catalog.walmart_items w ON w.sku = win.asin
JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz'
LEFT JOIN latest_status s ON s.store = w.store
WHERE w.missing_since IS NULL
  AND w.published_status = 'PUBLISHED'
  AND (s.store_status IS NULL OR upper(s.store_status) = 'ACTIVE')
ORDER BY w.store, w.sku
"""


# ── 处置判据(所有者定稿 2026-08-16 走进生产)────────────────────────────────
# **全项目唯一一处**决定"这个在线商品该拿它怎么办"。四个 provider 都问它,
# 不各判各的 —— 分开写迟早飘成"删除链按 A 判、库存链按 B 判"而两边都不报错。

TITLE_SIM_FLOOR = 0.70      # 相似度低于此 → 删除;不低于 → 改标题

def processed_title(slow) -> str:
    """输入:products.slow → 输出:过完上架文案处理的标题(去品牌/去符号/截 199)。

    ⚠ **相似度必须拿它去比,不能比亚马逊原始标题**:`force_amazon_copy` 会去掉
    品牌名,我们的沃尔玛标题天生就是"亚马逊标题减品牌"。拿原始标题比,品牌一长
    (`SuperMegaBrandName Cup` → `Cup`)相似度就掉到 24%,一个完全正常的商品
    会被判成"不是同一个东西"而删除。两边过同一套处理,剩下的差异才是真差异。
    """
    if not isinstance(slow, dict):
        return ""
    t = slow.get("title")
    if not t or str(t).strip() in TITLE_PLACEHOLDERS:
        return ""
    return mp_mapper.force_amazon_copy(
        {}, {"title": t, "brand": slow.get("brand"), "attrs": slow}
    ).get("productName") or ""


# 无货三档的映射搬去 `order_audit.stock_block`(维护链与审核链的唯一出处);
# 本模块已经 import order_audit,方向不变。

# 动作优先级:删除 > 库存 > 标题。一个 SKU 一轮只出一个动作 ——
# 既建议删除又建议改标题的话,执行件会先花配额改一个马上要删的商品。
_ACTION_RANK = {"delete": 0, "inventory": 1, "title": 2}


def classify(*, outcome=None, stock_status=None, stock_state=None,
             title_similarity=None, over_lead=False, lead_note="") -> tuple:
    """输入:一行在线商品的处置信号 → 输出:(动作, 原因码, 原因文案);无动作返回 (None,'','')。

    所有者定稿的判据,逐条对应他给的伪代码:

      outcome == 'not_found'                     → 删除(ASIN 已从亚马逊下架)
      stock_status == 'Currently unavailable'    → 库存 0(在架但不可售,拿不到价格库存)
      stock_status == 'No Featured Offer'        → 库存 0(无 Buy Box)
      stock_state == 'out_of_stock'              → 库存 0(普通缺货)
      配送超本店上限                              → 库存 0
      标题相似度 < 0.70                           → 删除
      标题相似度 ≥ 0.70 且标题有差异              → 改标题

    ⚠ 顺序即优先级,**删除压过一切**:一个 SKU 一轮只出一个动作,否则执行件
    会先花配额去改一个马上要删的商品(批次 E 踩过同款坑:同一 SKU 既建议反补
    又建议删除,先花配额救活再花配额删掉)。

    ⚠ 相似度 None(有一边根本没标题)**不算不匹配**:算不出来 ≠ 不像。
    走到这一步说明标题缺失,那是采集问题,不该拿它当删除依据。
    """
    if str(outcome or "").strip().lower() == "not_found":
        return ("delete", "not_found", "亚马逊已下架(采集 not_found)")
    if title_similarity is not None and title_similarity < TITLE_SIM_FLOOR:
        return ("delete", "title_mismatch",
                f"标题相似度 {title_similarity:.0%} < {TITLE_SIM_FLOOR:.0%}")
    # 无货三档的判据是**审核链共用的**(order_audit.stock_block 是唯一出处):
    # 维护链据此把库存写 0,审核链据此报「无货」而不是「采集缺字段」。
    # 各写一份的下场是同一个商品两条链说两种话,而且两边都不报错。
    hit = order_audit.stock_block(stock_status, stock_state)
    if hit:
        return ("inventory", hit[0], hit[1])
    if over_lead:
        return ("inventory", "lead_days", lead_note or "配送时长超本店上限")
    return (None, "", "")


def pick_one(actions: list[tuple]) -> tuple:
    """输入:同一 SKU 的多个 (动作,原因码,原因文案) → 输出:优先级最高的那个。

    删除 > 库存 > 标题。空列表返回 (None,'','')。
    """
    real = [a for a in actions if a and a[0]]
    if not real:
        return (None, "", "")
    return min(real, key=lambda a: _ACTION_RANK.get(a[0], 9))


def _rows(conn, stockzero_stores: list[str]) -> list[dict]:
    """输入:连接 + stockzero 名单 → 输出:在线商品行(**dict,不是元组**)。

    ⚠ 2026-08-16 从元组改成 dict:SQL 加了 outcome/stock_status/stock_state 三列,
    四个 provider 的位置解包**全部要改**,漏一处轻则 ValueError、重则静默取错
    字段(元组长度对得上、字段错位时不报错)。按名字取之后,加列只改 SQL。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_AMZ_JOIN, (list(stockzero_stores or []),))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


_DEDUPE_SCOPE = "maintenance:submitted"
SUPPRESS_HOURS = 20     # 同 (店铺,SKU,类型,新值) 多久内不重复提交


def _suppress_key(it: dict) -> str:
    return f"{it['store']}|{it['sku']}|{it['kind']}|{it.get('new')}"


def drop_recent(conn, intents: list[dict],
                hours: int = SUPPRESS_HOURS) -> tuple[list[dict], int]:
    """输入:连接 + 意图 → 输出:(去掉近期已提交过的, 被压掉的条数)。

    **为什么必须有这道闸**(2026-08-09 生产实证:208 条 ERR_EXT_DATA_0101198
    "stale update request"):provider 比的是 amz 值 vs `catalog.walmart_items`
    的**上次扫店快照**。提交成功后本地快照不变(要等 catalog_sync 再扫一遍),
    下一轮同样的差异又算出来 → 重发一模一样的载荷 → 烧配额且被沃尔玛判重。

    键含**新值**:值真变了(amz 又调价了)照样能提交,压的只是"同一件事
    再做一遍"。窗口 20 小时(维护日跑一轮,留 4 小时余量)。
    """
    if not intents:
        return intents, 0
    keys = [_suppress_key(i) for i in intents]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT key FROM ops.dedupe WHERE scope = %s AND key = ANY(%s)"
            " AND created_at > now() - make_interval(hours => %s)",
            (_DEDUPE_SCOPE, keys, int(hours)))
        recent = {r[0] for r in cur.fetchall()}
    if not recent:
        return intents, 0
    kept = [i for i, k in zip(intents, keys) if k not in recent]
    logger.info("近 %d 小时内已提交过的意图压掉 %d 条(防 stale update)",
                hours, len(intents) - len(kept))
    return kept, len(intents) - len(kept)


def record_submitted(conn, intents: list[dict]) -> int:
    """输入:连接 + 已提交的意图 → 输出:记账条数(供 drop_recent 抑制)。"""
    if not intents:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ops.dedupe (scope, key, meta) VALUES (%s,%s,%s::jsonb)"
            " ON CONFLICT (scope, key) DO UPDATE SET created_at = now()",
            [(_DEDUPE_SCOPE, _suppress_key(i), None) for i in intents])
    return len(intents)


def _cap(intents: list[dict], kind: str) -> list[dict]:
    """输入:意图列表 → 输出:截到单轮上限(超出只告警,下轮继续)。"""
    if len(intents) <= MAX_INTENTS_PER_KIND:
        return intents
    logger.warning("%s 意图 %d 条超单轮上限 %d,本轮只提交前 %d 条(其余下轮)",
                   kind, len(intents), MAX_INTENTS_PER_KIND,
                   MAX_INTENTS_PER_KIND)
    return intents[:MAX_INTENTS_PER_KIND]


def zero_intents(conn, stockzero_stores: list[str]) -> list[dict]:
    """输入:连接 + stockzero 店名单 → 输出:整店清零意图(库存 → 0)。"""
    if not stockzero_stores:
        return []
    with conn.cursor() as cur:
        cur.execute(_SQL_ZERO, (list(stockzero_stores),))
        rows = cur.fetchall()
    return [{"store": s, "sku": k, "kind": "inventory", "old": q, "new": 0,
             "code": "stockzero", "reason": "整店清零(限额表「库存特殊要求」=0)"}
            for s, k, q in rows]


# 跟卖品铺货量:跟卖无 amz 侧库存可跟,固定保守值(与 AMZ_IN_STOCK_QTY
# 同源口径,所有者拍板 2026-08-12 终值 10)
MATCH_INVENTORY_QTY = int(os.environ.get("MATCH_INVENTORY_QTY", "10"))

# 跟卖品在架且库存为 0/未知 → 铺货。不要求 published_status='PUBLISHED':
# 0 库存本身就可能导致 UNPUBLISHED(reason=Inventory),要求已发布会死锁
# (没库存→不发布→不给库存)。RETIRED/ARCHIVED 不碰。
_SQL_MATCH_INV = """
SELECT w.store, w.sku, w.avail_qty
FROM catalog.walmart_items w
JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'match'
WHERE w.missing_since IS NULL
  AND coalesce(upper(w.lifecycle_status), 'ACTIVE') = 'ACTIVE'
  AND coalesce(w.avail_qty, 0) = 0
  AND NOT (w.store = ANY(%s))
"""


def match_inventory_intents(conn, stockzero_stores: list[str] | None = None
                            ) -> list[dict]:
    """输入:连接 + stockzero 名单 → 输出:跟卖品铺货意图(0/未知 → 保守值)。

    补结构洞(所有者批复 2026-08-12):跟卖 offer 建成即 0 库存不可售,
    而 amz 驱动的 inventory_intents 按路由铁律永远排除 source_type='match'
    ——旧系统同病(inventory_push 因 --no-poll 从未真跑)。本 provider 是
    跟卖库存的**唯一**实现路径:新 offer 进目录(catalog_sync)后自动铺货,
    stockzero 店解除后也自动回补(修清零/回补不对称)。
    ⚠ 手动把个别跟卖品清零会被本 provider 回填——单品停售走停用/删除流程,
    整店停售走 stockzero,清零不是停售手段。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_MATCH_INV, (list(stockzero_stores or []),))
        rows = cur.fetchall()
    return _cap([{"store": s, "sku": k, "kind": "inventory",
                  "old": q, "new": MATCH_INVENTORY_QTY,
                  "code": "match_restock",
                  "reason": f"跟卖品铺货(库存 0/未知 → {MATCH_INVENTORY_QTY})"}
                 for s, k, q in rows], "inventory")


def price_intents(conn, multipliers: dict[str, dict],
                  stockzero_stores: list[str] | None = None) -> list[dict]:
    """输入:连接 + {店铺: 限额表倍率行} + stockzero 名单 → 输出:改价意图。

    新价 = services.pricing.walmart_price(**落地价(amz 现价 + 运费)** × 该店
    对应区间倍率),
    与上架用的是**同一套定价规则**(避免上架价与维护价两套口径)。
    区间按**配送方式**分两套(FBA 0-30/30-75、FBM 15-80/80-1000),配送方式
    取 latest_snapshot 的 raw.is_fba(采集侧 parser 读 buybox 的 Ships from);
    **未知则不改价**——猜错一档就是拿错倍率改线上价。
    出界按 300% 兜底(所有者定稿 2026-08-09);只有**区间内倍率未配置**
    才不产出(不是改成 0,是不动)。
    差异需同时满足 PRICE_MIN_DELTA 与 PRICE_MIN_RATIO 才提交。
    """
    out = []
    skipped_no_rule = skipped_no_channel = skipped_no_shipping = 0
    for r in _rows(conn, stockzero_stores):
        store, sku = r["store"], r["sku"]
        wm_price, amz_price = r["wm_price"], r["amz_price"]
        fulfillment, shipping = r["fulfillment"], r["shipping"]
        # 删除类压过改价:一个 SKU 一轮只出一个动作,否则先花配额改一个
        # 马上要删的商品(pick_one 的同款理由)。两条删除判据都要判到。
        if classify(outcome=r["outcome"],
                    title_similarity=order_audit.title_similarity(
                        r["product_name"], processed_title(r["slow"]))
                    )[0] == "delete":
            continue
        if amz_price is None or wm_price is None:
            continue                    # 缺任一侧现值:没有可比基准,不动
        channel = str(fulfillment or "").strip().upper()
        if channel not in pricing.PRICE_BANDS:
            # 配送方式未知 → **不改价**(所有者 2026-08-09:这是必须要获取的
            # 信息)。FBA/FBM 两套区间边界不同,猜错一档就是拿错倍率改线上价,
            # 比不改危险得多。
            skipped_no_channel += 1
            continue
        if shipping is None:
            # 运费没采到 ⇒ 落地价算不出来。与"配送方式未知不改价"同一个道理:
            # 当 0 定出来的价偏低,越贵的运费亏得越多,而两侧都不报错
            skipped_no_shipping += 1
            continue
        new_price = pricing.walmart_price(channel, amz_price,
                                          multipliers.get(store, {}), shipping)
        if new_price is None:
            skipped_no_rule += 1
            continue
        old = float(wm_price)
        delta = abs(new_price - old)
        if delta < PRICE_MIN_DELTA or (old > 0 and delta / old < PRICE_MIN_RATIO):
            continue
        out.append({"store": store, "sku": sku, "kind": "price",
                    "old": old, "new": new_price, "code": "price_sync",
                    "reason": f"{channel} 落地价 × 区间倍率 → "
                              f"{old:.2f}→{new_price:.2f}"})
    if skipped_no_rule:
        logger.info("改价:%d 行因该区间倍率未配置跳过(不动,非改 0)",
                    skipped_no_rule)
    if skipped_no_channel:
        logger.warning("改价:%d 行配送方式(FBA/FBM)未知,本轮不改价"
                       "——采集侧 raw.is_fba 缺失或该 ASIN 尚未重采",
                       skipped_no_channel)
    if skipped_no_shipping:
        logger.warning("改价:%d 行运费未采到(采集侧 N/A),落地价算不出来,"
                       "本轮不改价——**绝不当免运费处理**", skipped_no_shipping)
    return _cap(out, "price")


def inventory_intents(conn, stockzero_stores: list[str] | None = None
                      ) -> list[dict]:
    """输入:连接 + stockzero 名单 → 输出:改库存意图。

    库存决策(所有者定稿 2026-08-09):
      · stock_count 有值 → 同步该值(0 就是 0)
      · **stock_count 为 NULL(没采到)→ 也写 0**。采不到就不卖,是运营口径;
        库里 NULL 与 0 仍然分得清(catalog.snapshots 原样存),只在决策这一层
        把"不知道"当成"别卖"。
      · 配送超上限 → 写 0。上限取限额表**本店**「配送时长限制」
        (`lead_limit`,与分配链共用同一列同一常量);该店没填就回落全局
        `MAX_LEAD_DAYS`(**7 天**,所有者两次收紧 12 →08-09→ 8 →08-15→ 7)。
        ⚠ 与上架侧口径不同:上架侧超限是**不上架**,这里已经在架了只能压库存
    ⚠ 血量提醒:采集服务中断一整轮会让大批行的 stock_count 变 NULL,
    按本规则即全线清零。单轮上限 MAX_INTENTS_PER_KIND 是唯一刹车,
    真跑前务必看 dry-run 的清零条数。
    """
    from services import amz_source
    lead_caps = store_limits.lead_day_caps()
    out = []
    for r in _rows(conn, stockzero_stores):
        store, sku, avail_qty = r["store"], r["sku"], r["avail_qty"]
        stock_count = r["stock_count"]
        if stock_count is None:
            stock_count = 0             # 没采到 → 不卖(所有者定稿)
        cap = store_limits.cap_for(lead_caps, store, amz_source.MAX_LEAD_DAYS)
        over = store_limits.over_lead_cap(r["delivery_days"], cap)
        # 处置判据集中在 classify():四条清零判据(不可售/无 BuyBox/缺货/超时)
        # 各自带原因码,飞书「原因」列靠它才分得清 —— 表里四条长得一模一样
        act, code, why = classify(
            outcome=r["outcome"], stock_status=r["stock_status"],
            stock_state=r["stock_state"], over_lead=over,
            # ⚠ 相似度也要给:不给的话"标题不匹配 → 删除"这条在本 provider 看不见,
            # 于是该删的行照样产一条库存意图,执行件先花配额清零再花配额删
            # (2026-08-16 演练实见 B0MISMATCH 10 → 7)
            title_similarity=order_audit.title_similarity(
                r["product_name"], processed_title(r["slow"])),
            lead_note=f"配送 {r['delivery_days']} 天 > 本店上限 {cap} 天")
        if act == "delete":
            continue        # 删除类归 delete_intents,这里不抢(一 SKU 一动作)
        new_qty = 0 if act == "inventory" else int(stock_count)
        if avail_qty is not None and int(avail_qty) == new_qty:
            continue
        out.append({"store": store, "sku": sku, "kind": "inventory",
                    "old": avail_qty, "new": new_qty,
                    "code": code, "reason": why})
    return _cap(out, "inventory")


def title_intents(conn, stockzero_stores: list[str] | None = None
                  ) -> list[dict]:
    """输入:连接 + stockzero 名单 → 输出:标题维护意图。

    新标题 = 亚马逊标题过**与上架同一套文案处理**(force_amazon_copy:
    去品牌名、去项目符号、折叠空白、截 199)——两处口径必须一致,
    否则上架写一个标题、维护又改成另一个,自己跟自己打架。

    旧防线原样保留:占位符跳过;**productType / UPC / 标题三缺一跳过**
    (缺任一沃尔玛必退回)。标题相同则不产出。
    """
    out = []
    skipped_incomplete = skipped_mismatch = 0
    for r in _rows(conn, stockzero_stores):
        store, sku = r["store"], r["sku"]
        product_name, product_type, upc = r["product_name"], r["product_type"], r["upc"]
        slow = r["slow"]
        amz_title = ((slow or {}).get("title") if isinstance(slow, dict)
                     else None)
        if not amz_title or str(amz_title).strip() in TITLE_PLACEHOLDERS:
            continue
        # 70% 闸(所有者定稿 2026-08-16):相似度过低说明**采到的可能不是同一个
        # 商品**,那时把亚马逊标题抄过去是把错的抄进线上 —— 交给删除链处置。
        # 相似度算不出来(有一边没标题)不算不匹配,照旧走改标题。
        sim = order_audit.title_similarity(product_name,
                                           processed_title(slow))
        if sim is not None and sim < TITLE_SIM_FLOOR:
            skipped_mismatch += 1
            continue
        new_title = processed_title(slow)    # 与上架同款处理(去品牌/截 199)
        if not new_title or new_title == (product_name or ""):
            continue
        if not product_type or not upc:
            skipped_incomplete += 1     # 三缺一跳过(旧防线)
            continue
        out.append({"store": store, "sku": sku, "kind": "title",
                    "old": product_name, "new": new_title,
                    "product_type": product_type, "product_id": upc,
                    "code": "title_sync",
                    "reason": f"相似度 {sim:.0%},同步亚马逊标题" if sim is not None
                              else "同步亚马逊标题(相似度算不出)"})
    if skipped_incomplete:
        logger.info("标题:%d 行缺 productType/UPC 跳过(三缺一防线)",
                    skipped_incomplete)
    if skipped_mismatch:
        logger.info("标题:%d 行相似度 < %.0f%% 不改标题(交给删除链)",
                    skipped_mismatch, TITLE_SIM_FLOOR * 100)
    return _cap(out, "title")


def delete_intents(conn, stockzero_stores: list[str] | None = None,
                   caps: dict[str, int] | None = None,
                   min_batches: int = MIN_OFFSET_BATCHES,
                   oos_days: int = LONG_OOS_DAYS) -> list[dict]:
    """输入:连接(+单店上限表/批次数门槛/无货天数)→ 输出:删除意图(kind='delete')。

    三个原因(所有者定稿 2026-08-09),都是"这个产品已经不值得留在架上了":

    variant_offset —— 亚马逊把 /dp/<ASIN> 返回成兄弟变体页面,parser 比对
      页面 ASIN 不一致后**拒绝写入**,采集侧列为**不自动重试** ⇒ 价格/库存
      **永远拿不到新数据**。门槛 min_batches 默认 1(所有者:偏移了就不会
      恢复,不设观察期);唯一防呆:最后一次偏移之后若有 outcome=ok 快照
      则移出名单——真能采就不该删。

    商品不存在 —— amz 标题是占位符(TITLE_PLACEHOLDERS)。旧系统在这里只是
      跳过标题维护,所有者 2026-08-09 改为**删除该产品**:亚马逊页面都没了,
      留在沃尔玛就是个卖不出去的死链。

    连续无货 N 天 —— 窗口内一条"有货"观测都没有(判据三条见 _SQL_LONG_OOS)。
      库存 provider 早就把它清零了,清零后还这么久不回货 = 这个货源没了。
      ⚠ 采集接线于 2026-08-08,历史攒够 15 天之前这条恒返空,不是坏了。

    单店单轮上限取限额表「下架限制」(caps,与 product_clear 同一列同一口径),
    店铺不在表内退 DELETE_PER_STORE 并告警。
    """
    caps = caps or {}
    seen, out, per_store = set(), [], {}

    def _take(store, sku, code, why="", extra=None):
        """code = 机器码(飞书「原因」列的分组依据 / 建议行 category);
        why = 人读原因文案。2026-08-16 前两者是同一个字符串,「原因」列加进来
        之后必须分开:`title_mismatch` 分得了组但读不出"低到什么程度"。"""
        if (store, sku) in seen:
            return          # 两个原因都命中只删一次
        cap = int(caps.get(store, DELETE_PER_STORE))
        per_store[store] = per_store.get(store, 0) + 1
        if per_store[store] > cap:
            return          # 超单店上限的留到下轮(下面统一告警)
        seen.add((store, sku))
        out.append({"store": store, "sku": sku, "kind": "delete",
                    "old": "在线", "new": "删除",
                    "code": code, "reason": why or code,
                    "label": f"删除({code})", **(extra or {})})

    with conn.cursor() as cur:
        cur.execute(_SQL_VARIANT_OFFSET, {"min_batches": int(min_batches)})
        for store, sku, batches, first_seen, last_seen in cur.fetchall():
            _take(store, sku, "variant_offset",
                  "采集永久偏移(拿不到新数据)",
                  {"batches": batches, "first_seen": first_seen,
                   "last_seen": last_seen})

    for r in _rows(conn, stockzero_stores):
        store, sku = r["store"], r["sku"]
        slow = r["slow"]
        title = ((slow or {}).get("title") if isinstance(slow, dict) else None)
        if str(title or "").strip() in TITLE_PLACEHOLDERS and title:
            _take(store, sku, "商品不存在", "亚马逊标题是占位符,页面已不存在")
            continue
        # 所有者定稿 2026-08-16 新增两条删除判据(judgement 在 classify):
        #   outcome == 'not_found'  → ASIN 已从亚马逊下架
        #   标题相似度 < 70%         → 采到的可能不是同一个商品,抄标题会抄错
        act, code, why = classify(
            outcome=r["outcome"],
            title_similarity=order_audit.title_similarity(
                r["product_name"], processed_title(slow)))
        if act == "delete":
            _take(store, sku, code, why)

    with conn.cursor() as cur:
        cur.execute(_SQL_LONG_OOS, {"days": int(oos_days)})
        rows = cur.fetchall()
    for store, sku, obs, first_seen, last_seen in rows:
        _take(store, sku, f"连续无货{oos_days}天",
              f"{oos_days} 天窗口内 {obs} 次观测无一有货,货源已断",
              {"obs": obs, "first_seen": first_seen, "last_seen": last_seen})
    if not rows:
        # 采集历史不足窗口长度时这条恒空——说出来,免得被读成"没有长期缺货的"
        logger.info("连续无货 %d 天:本轮 0 个候选(采集历史不足 %d 天时属正常)",
                    oos_days, oos_days)

    over = {s: n - int(caps.get(s, DELETE_PER_STORE))
            for s, n in per_store.items()
            if n > int(caps.get(s, DELETE_PER_STORE))}
    if over:
        logger.warning("删除超单店上限,本轮留下:%s", over)
    return _cap(out, "delete")


def collect_all(conn, stockzero: list[str], oos_days: int = 0) -> list[dict]:
    """输入:连接 + stockzero 名单(+无货天数)→ 输出:本轮全部维护意图。

    ⚠ **住在 services 而不是 workflow 里**:2026-08-16 拆成
    maintenance_scan(建议)+ maintenance(执行)之后,只有扫描件调它;但铁律
    禁止 workflow 互相 import,而 dry-run 口径必须与真跑一致 —— 收在这里,
    两边看到的是同一份意图。

    stockzero 店整店排除在三个自动 provider 之外 —— 它们归 zero_intents,
    否则"跟随 amz 库存"会把刚清零的货又顶回去(两条规则打架)。
    """
    mults = store_limits.price_multipliers()
    deletes = delete_intents(conn, stockzero, store_limits.retire_caps(),
                             oos_days=oos_days or LONG_OOS_DAYS)
    doomed = {(d["store"], d["sku"]) for d in deletes}
    intents = list(deletes)
    for it in (title_intents(conn, stockzero)
               + price_intents(conn, mults, stockzero)
               + inventory_intents(conn, stockzero)
               # 跟卖品铺货(所有者批复 2026-08-12):amz 三 provider 按路由
               # 铁律只碰 source_type='amz',跟卖品的库存唯一由它负责
               + match_inventory_intents(conn, stockzero)
               + zero_intents(conn, stockzero)):
        # 将被删除的行不再改价/改库存/改标题:它们的 amz 数据本来就是陈旧的
        # (采不到才要删),再跟一轮既烧配额又是拿错数据改线上
        if (it["store"], it["sku"]) not in doomed:
            intents.append(it)
    # 近期已提交过同一件事的压掉:提交成功后本地快照要等 catalog_sync 才更新,
    # 不压就会一轮轮重发同样的载荷(生产实证 208 条 stale update)
    intents, _n = drop_recent(conn, intents)
    return intents


# ── 意图 ⇄ 建议行(ops.dispositions)的互转 ────────────────────────────────────
# maintenance_scan 用 to_disposition() 落建议,maintenance 用 from_disposition()
# 还原成意图。**两个方向必须写在一起**:分散在两个 workflow 里,加一个键时
# 只改了写侧,读侧就静默丢那个键 —— 最典型的是 title 的 product_type/product_id,
# 丢了之后 build_title_item 组出 None,feed 被沃尔玛整批退回。
#
# 列位对应(建议行 → 飞书维护记录):
#   action   = kind      → 「建议」列(执行件另写「动作」列)
#   category = code      → 「原因」列的分组依据
#   reason   = 人读原因   → 「原因」列正文
#   detail   = 其余全部   → 旧值/新值/标题载荷/删除证据
_DETAIL_KEYS = ("old", "new", "label", "product_type", "product_id",
                "batches", "first_seen", "last_seen", "obs")

#: kind → 「建议」列的中文标签。**全项目唯一出处。**
#: 2026-08-17 起它不再只是显示文案,而是 maintenance_scan 与 maintenance 之间
#: 的**连接键**:扫描件按 (店铺, SKU, 建议) append 半行,执行件按同一个键找回
#: 那一行就地补齐动作/旧值/新值/feedid。两个 workflow 各存一份副本的话,改一处
#: 忘一处的表现是**执行件找不到扫描件写的行,于是每条都另起一行**——表里一条
#: 建议对两行、飞书行数翻倍,而两边都不报错。
#: 铁律 1 禁止 workflow 互相 import,所以这份唯一副本住在 services。
KIND_LABEL = {"delete": "删除", "title": "标题",
              "price": "价格", "inventory": "库存"}


def to_disposition(it: dict) -> dict:
    """输入:维护意图 → 输出:ops.dispositions 建议行(services.dispositions 收)。"""
    return {
        "store": it["store"], "sku": it["sku"], "source": "maint",
        "action": it["kind"], "category": it.get("code") or "",
        "reason": it.get("reason") or "",
        "detail": {k: it[k] for k in _DETAIL_KEYS if k in it},
    }


def from_disposition(row: dict) -> dict:
    """输入:ops.dispositions 建议行 → 输出:维护意图(带 disposition_id)。"""
    detail = row.get("detail") or {}
    it = {"disposition_id": row["id"], "store": row["store"], "sku": row["sku"],
          "kind": row["action"], "code": row.get("category") or "",
          "reason": row.get("reason") or ""}
    it.update({k: detail[k] for k in _DETAIL_KEYS if k in detail})
    return it


def build_title_item(sku: str, product_type: str, product_id: str,
                     title: str) -> dict:
    """输入:sku/productType/UPC/新标题 → 输出:MP_MAINTENANCE 标题维护 MPItem。

    结构旧系统实证:Visible 直接以 productType 名作命名空间(中间没有
    productCategory 层);Orderable 与 Visible 是并列顶级对象,不是 MPProduct。
    """
    return {"Orderable": {"sku": str(sku),
                          "productIdentifiers": {"productId": str(product_id),
                                                 "productIdType": "UPC"}},
            "Visible": {str(product_type): {"productName": str(title)}}}
