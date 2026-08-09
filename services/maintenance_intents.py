"""维护意图 provider(maintenance 工作流的可插拔数据源)。

意图 dict 统一形态(管道契约,provider 只管产出):
  {"store": 店铺名, "sku": SKU, "kind": "title"|"price"|"inventory"|"delete",
   "old": 旧值, "new": 新值}
  title 意图额外携带 product_type / product_id(UPC)两键(feed 载荷必需;
  按「三缺一跳过」旧防线过滤后再产出)。
清零是 inventory 的 new=0 特例,由 zero_intents 产出。
delete 是唯一的**不可逆**类(variant_offset:采集永久偏移,数据再也拿不到),
由 variant_offset_intents 产出,old/new = 在线/删除。

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

from services import mp_mapper, pricing

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
       p.slow
FROM catalog.walmart_items w
JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz'
JOIN catalog.products p ON p.marketplace = 'US' AND p.asin = w.sku
LEFT JOIN LATERAL (
    SELECT price, stock_count, delivery_days
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


def _rows(conn, stockzero_stores: list[str]) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(_SQL_AMZ_JOIN, (list(stockzero_stores or []),))
        return cur.fetchall()


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
    return [{"store": s, "sku": k, "kind": "inventory", "old": q, "new": 0}
            for s, k, q in rows]


def price_intents(conn, multipliers: dict[str, dict],
                  stockzero_stores: list[str] | None = None) -> list[dict]:
    """输入:连接 + {店铺: 限额表倍率行} + stockzero 名单 → 输出:改价意图。

    新价 = services.pricing.walmart_price(amz 现价 × 该店对应区间倍率),
    与上架用的是**同一套定价规则**(避免上架价与维护价两套口径)。
    出界/倍率未配置 → 不产出(不是改成 0,是不动)。
    差异需同时满足 PRICE_MIN_DELTA 与 PRICE_MIN_RATIO 才提交。
    """
    out = []
    skipped_no_rule = 0
    for (store, sku, _name, _pt, _upc, wm_price, _qty,
         amz_price, _sc, _dd, _slow) in _rows(conn, stockzero_stores):
        if amz_price is None or wm_price is None:
            continue                    # 缺任一侧现值:没有可比基准,不动
        # channel 采集侧未产出,一律按 FBM 区间(与 list_new 同口径)
        new_price = pricing.walmart_price("FBM", amz_price,
                                          multipliers.get(store, {}))
        if new_price is None:
            skipped_no_rule += 1
            continue
        old = float(wm_price)
        delta = abs(new_price - old)
        if delta < PRICE_MIN_DELTA or (old > 0 and delta / old < PRICE_MIN_RATIO):
            continue
        out.append({"store": store, "sku": sku, "kind": "price",
                    "old": old, "new": new_price})
    if skipped_no_rule:
        logger.info("改价:%d 行因定价出界/倍率未配置跳过(不动,非改 0)",
                    skipped_no_rule)
    return _cap(out, "price")


def inventory_intents(conn, stockzero_stores: list[str] | None = None
                      ) -> list[dict]:
    """输入:连接 + stockzero 名单 → 输出:改库存意图。

    库存决策(所有者定稿 2026-08-09):
      · stock_count 有值 → 同步该值(0 就是 0)
      · **stock_count 为 NULL(没采到)→ 也写 0**。采不到就不卖,是运营口径;
        库里 NULL 与 0 仍然分得清(catalog.snapshots 原样存),只在决策这一层
        把"不知道"当成"别卖"。
      · 配送 > MAX_LEAD_DAYS(8 天,所有者 2026-08-09 从 12 改)→ 写 0
    ⚠ 血量提醒:采集服务中断一整轮会让大批行的 stock_count 变 NULL,
    按本规则即全线清零。单轮上限 MAX_INTENTS_PER_KIND 是唯一刹车,
    真跑前务必看 dry-run 的清零条数。
    """
    from services import amz_source
    out = []
    for (store, sku, _name, _pt, _upc, _wp, avail_qty,
         _ap, stock_count, delivery_days, _slow) in _rows(conn, stockzero_stores):
        if stock_count is None:
            stock_count = 0             # 没采到 → 不卖(所有者定稿)
        new_qty = 0 if (delivery_days is not None
                        and delivery_days > amz_source.MAX_LEAD_DAYS) \
            else int(stock_count)
        if avail_qty is not None and int(avail_qty) == new_qty:
            continue
        out.append({"store": store, "sku": sku, "kind": "inventory",
                    "old": avail_qty, "new": new_qty})
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
    skipped_incomplete = 0
    for (store, sku, product_name, product_type, upc, _wp, _qty,
         _ap, _sc, _dd, slow) in _rows(conn, stockzero_stores):
        amz_title = ((slow or {}).get("title") if isinstance(slow, dict)
                     else None)
        if not amz_title or str(amz_title).strip() in TITLE_PLACEHOLDERS:
            continue
        # 与上架同款处理(brand 从 slow 取,和 force_amazon_copy 的入参一致)
        new_title = mp_mapper.force_amazon_copy(
            {}, {"title": amz_title, "brand": (slow or {}).get("brand"),
                 "attrs": slow or {}}).get("productName") or ""
        if not new_title or new_title == (product_name or ""):
            continue
        if not product_type or not upc:
            skipped_incomplete += 1     # 三缺一跳过(旧防线)
            continue
        out.append({"store": store, "sku": sku, "kind": "title",
                    "old": product_name, "new": new_title,
                    "product_type": product_type, "product_id": upc})
    if skipped_incomplete:
        logger.info("标题:%d 行缺 productType/UPC 跳过(三缺一防线)",
                    skipped_incomplete)
    return _cap(out, "title")


def delete_intents(conn, stockzero_stores: list[str] | None = None,
                   caps: dict[str, int] | None = None,
                   min_batches: int = MIN_OFFSET_BATCHES) -> list[dict]:
    """输入:连接(+单店上限表/批次数门槛)→ 输出:删除意图(kind='delete')。

    两个原因(所有者定稿 2026-08-09),都是"这个产品在亚马逊侧已经没法维护了":

    variant_offset —— 亚马逊把 /dp/<ASIN> 返回成兄弟变体页面,parser 比对
      页面 ASIN 不一致后**拒绝写入**,采集侧列为**不自动重试** ⇒ 价格/库存
      **永远拿不到新数据**。门槛 min_batches 默认 1(所有者:偏移了就不会
      恢复,不设观察期);唯一防呆:最后一次偏移之后若有 outcome=ok 快照
      则移出名单——真能采就不该删。

    商品不存在 —— amz 标题是占位符(TITLE_PLACEHOLDERS)。旧系统在这里只是
      跳过标题维护,所有者 2026-08-09 改为**删除该产品**:亚马逊页面都没了,
      留在沃尔玛就是个卖不出去的死链。

    单店单轮上限取限额表「下架限制」(caps,与 product_clear 同一列同一口径),
    店铺不在表内退 DELETE_PER_STORE 并告警。
    """
    caps = caps or {}
    seen, out, per_store = set(), [], {}

    def _take(store, sku, reason, extra=None):
        if (store, sku) in seen:
            return          # 两个原因都命中只删一次
        cap = int(caps.get(store, DELETE_PER_STORE))
        per_store[store] = per_store.get(store, 0) + 1
        if per_store[store] > cap:
            return          # 超单店上限的留到下轮(下面统一告警)
        seen.add((store, sku))
        out.append({"store": store, "sku": sku, "kind": "delete",
                    "old": "在线", "new": "删除", "reason": reason,
                    "label": f"删除({reason})", **(extra or {})})

    with conn.cursor() as cur:
        cur.execute(_SQL_VARIANT_OFFSET, {"min_batches": int(min_batches)})
        for store, sku, batches, first_seen, last_seen in cur.fetchall():
            _take(store, sku, "variant_offset",
                  {"batches": batches, "first_seen": first_seen,
                   "last_seen": last_seen})

    for (store, sku, _name, _pt, _upc, _wp, _qty,
         _ap, _sc, _dd, slow) in _rows(conn, stockzero_stores):
        title = ((slow or {}).get("title") if isinstance(slow, dict) else None)
        if str(title or "").strip() in TITLE_PLACEHOLDERS and title:
            _take(store, sku, "商品不存在")

    over = {s: n - int(caps.get(s, DELETE_PER_STORE))
            for s, n in per_store.items()
            if n > int(caps.get(s, DELETE_PER_STORE))}
    if over:
        logger.warning("删除超单店上限,本轮留下:%s", over)
    return _cap(out, "delete")


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
