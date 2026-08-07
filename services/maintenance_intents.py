"""维护意图 provider(maintenance 工作流的可插拔数据源)。

意图 dict 统一形态(管道契约,provider 只管产出):
  {"store": 店铺名, "sku": SKU, "kind": "title"|"price"|"inventory",
   "old": 旧值, "new": 新值}
  title 意图额外携带 product_type / product_id(UPC)两键(feed 载荷必需;
  provider 做实时按「三缺一跳过」旧防线过滤后再产出)。
清零是 inventory 的 new=0 特例,由 zero_intents 产出。

路由铁律(所有者定稿 2026-08-07,provider 做实时必须遵守):意图产出必须
JOIN catalog.listing_sources 按出身路由——amz 快照驱动的意图只作用于
source_type='amz' 的行;"源数据查不到"绝不可对 match/unknown 行推导出
清库存/删除等破坏动作(旧系统按 SKU 格式排除的补丁废止,以登记簿为准)。

Provider 面(2026-08-07 预留接口定稿,采集侧改造中):
  zero_intents()       ✅ 做实:限额表「库存特殊要求」=0 的 stockzero 店整店清零,
                       源=catalog.walmart_items,不依赖采集
  price_intents()      ⏳ 预留:catalog.latest_snapshot(amz 快变)× walmart_items
                       × 定价规则(规则届时与所有者定稿;涨跌幅闸位也留在此)
  inventory_intents()  ⏳ 预留:amz stock_state → 沃尔玛库存
  title_intents()      ⏳ 预留:处理后 amz 标题(采集+LLM 链路)
采集接入后只填预留函数体(SQL+规则),workflow 管道零改动。

路由阈值与标题载荷结构逐字移植旧系统(erpAPI 沃尔玛商品维护,实证勿改)。
"""

import logging

logger = logging.getLogger("services.maintenance_intents")

# 单店该类型条数 ≤ 阈值走同步 PUT(结果当场已知),超过走 feed;
# 标题无同步接口永远 feed(旧 SYNC_THRESHOLDS 原值)
SYNC_THRESHOLDS = {"price": 5, "inventory": 10}

# M 列占位符:命中则跳过标题维护(否则 Walmart 退回;旧系统原值)
TITLE_PLACEHOLDERS = {"[商品不存在]"}

_SQL_ZERO = """
SELECT store, sku, avail_qty FROM catalog.walmart_items
WHERE store = ANY(%s) AND missing_since IS NULL AND avail_qty > 0
"""
# avail_qty > 0 是显式条件:旧系统 `None != 0` 也触发清零是坑(库存未知的行
# 被盲清)——新规矩:未知库存不动,只清确知有货的行。


def zero_intents(conn, stockzero_stores: list[str]) -> list[dict]:
    """输入:连接 + stockzero 店名单 → 输出:整店清零意图(库存 → 0)。"""
    if not stockzero_stores:
        return []
    with conn.cursor() as cur:
        cur.execute(_SQL_ZERO, (list(stockzero_stores),))
        rows = cur.fetchall()
    return [{"store": s, "sku": k, "kind": "inventory", "old": q, "new": 0}
            for s, k, q in rows]


def price_intents(conn) -> list[dict]:
    """⏳ 预留(采集侧改造中):amz 最新快照 × 定价规则 → 改价意图。

    接入点:catalog.latest_snapshot(scraper_migration_brief 契约 v1)
    JOIN catalog.walmart_items(sku=asin)+ 限额表 fba/FBM 区间定价规则。
    做实时同步补涨跌幅闸(所有者 2026-08-07:暂不需要,闸位留此)。
    """
    logger.info("price_intents:采集源未接(采集服务改造中),本轮无改价意图")
    return []


def inventory_intents(conn) -> list[dict]:
    """⏳ 预留(采集侧改造中):amz stock_state → 沃尔玛库存意图。"""
    logger.info("inventory_intents:采集源未接(采集服务改造中),本轮无改库存意图")
    return []


def title_intents(conn) -> list[dict]:
    """⏳ 预留(采集侧改造中):处理后 amz 标题 → 标题维护意图。

    产出时须过 TITLE_PLACEHOLDERS 与「productType/upc/title 三缺一跳过」
    (旧系统防线,做实时移植)。
    """
    logger.info("title_intents:采集源未接(采集服务改造中),本轮无标题意图")
    return []


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
