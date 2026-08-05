"""沃尔玛 Items 域接口(函数面与配额定稿见 docs/api_blueprint.md §3/§7)。

本文件当前实现 product_query 工作流所需的三个端点(蓝图矩阵 #5/#6):
  search_walmart()       GET /v3/items/walmart/search(DEFAULT)  全站目录搜索
  search_walmart_spec()  同端点 responseFormat=SPEC             跟卖路由/标识互转
  catalog_search()       POST /v3/items/catalog/search          本店目录精确查询

其余函数(list_items / iter_all_items / get_item / count_items / get_spec)
按蓝图 §7 签名预留,随对应工作流迁移时实现——不自创签名(CLAUDE.md api 层收录规则)。

响应字段坑(旧系统实测,api 层统一兜底):
- 线上返回 camelCase,本地 OpenAPI 规格是 snake_case(numReviews/num_reviews 等)→ _pick 双键
- SPEC 的 productIdentifiers 位置随 feedType 变:MP_ITEM_MATCH 在 MPItem[0].Item,
  MP_ITEM 在 MPItem[0].Orderable
- 这些是公开目录只读端点,与店铺无关,任一有效店铺 token 均可查
"""

import logging

from api import _client

logger = logging.getLogger("api.items")


def _pick(d: dict, *keys, default=None):
    """camelCase / snake_case 双键取值:依次尝试 keys,取第一个存在的。"""
    for k in keys:
        if k in d:
            return d[k]
    return default


def search_walmart(store: dict, *, query: str | None = None,
                   upc: str | None = None, gtin: str | None = None) -> list[dict]:
    """输入:店铺 + query/upc/gtin 三选一 → 输出:全站命中商品列表(≤40 条,原始 dict)。

    GET /v3/items/walmart/search(DEFAULT 格式)。只返回 published 商品;
    响应无 UPC/GTIN 字段;空列表 = 全站未占用/未命中(官方语义)。
    """
    if sum(v is not None for v in (query, upc, gtin)) != 1:
        raise ValueError("query/upc/gtin 必须恰好传一个")
    params = {}
    if query is not None:
        params["query"] = query
    if upc is not None:
        params["upc"] = upc
    if gtin is not None:
        params["gtin"] = gtin

    _client.rate_acquire("items.walmart_search", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/items/walmart/search",
        token, store["client_id"], store["proxy"], params=params, max_retries=3)
    if status != 200:
        raise RuntimeError(f"walmart/search 返回 {status}: {data}")
    return (data or {}).get("items") or []


def search_walmart_spec(store: dict, *, upc: str | None = None,
                        gtin: str | None = None, asin: str | None = None) -> dict:
    """输入:店铺 + upc/gtin/asin 三选一 → 输出:跟卖路由结果 dict。

    GET /v3/items/walmart/search?responseFormat=SPEC(官方另有 1000/day 限额)。
    返回:
      {"feed_type": "MP_ITEM_MATCH"(已在售可跟卖) | "MP_ITEM"(未在售需完整建品) | None(目录无),
       "product_id": 规范化 GTIN/UPC 或 None, "product_id_type": ..., "product_type": ...,
       "title": ..., "asin": 交叉出的 ASIN 或 None, "raw": 原始 item 或 None}
    硬约束(实测):SPEC 不能带 query 参数;asin 参数仅 SPEC 格式支持。
    """
    if sum(v is not None for v in (upc, gtin, asin)) != 1:
        raise ValueError("upc/gtin/asin 必须恰好传一个")
    params = {"responseFormat": "SPEC"}
    if upc is not None:
        params["upc"] = upc
    if gtin is not None:
        params["gtin"] = gtin
    if asin is not None:
        params["asin"] = asin

    _client.rate_acquire("items.walmart_search", store["client_id"])
    _client.rate_acquire("items.walmart_search_spec", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/items/walmart/search",
        token, store["client_id"], store["proxy"], params=params, max_retries=3)
    if status != 200:
        raise RuntimeError(f"walmart/search SPEC 返回 {status}: {data}")

    items = (data or {}).get("items") or []
    if not items:
        return {"feed_type": None, "product_id": None, "product_id_type": None,
                "product_type": None, "title": None, "asin": None, "raw": None}
    item = items[0]
    feed_type = item.get("feedType")
    mp0 = ((item.get("itemSpecPayload") or {}).get("MPItem") or [{}])[0]
    # productIdentifiers 位置随 feedType 变(MP_ITEM_MATCH→Item;MP_ITEM→Orderable)
    ids = ((mp0.get("Item") or {}).get("productIdentifiers")
           or (mp0.get("Orderable") or {}).get("productIdentifiers") or {})
    ext_asin = None
    for ext in item.get("externalProductIdentifier") or []:
        if ext.get("externalProductIdType") == "ASIN":
            ext_asin = ext.get("externalProductId")
    title = None
    for pt_attrs in (mp0.get("Visible") or {}).values():
        title = pt_attrs.get("productName") or title
    return {"feed_type": feed_type,
            "product_id": ids.get("productId"),
            "product_id_type": ids.get("productIdType"),
            "product_type": item.get("productType") or item.get("specProductType"),
            "title": title, "asin": ext_asin, "raw": item}


def catalog_search(store: dict, field: str, value: str) -> list[dict]:
    """输入:店铺 + 查询字段名 + 值 → 输出:本店目录命中列表(payload 原始 dict)。

    POST /v3/items/catalog/search(只搜本店铺自有目录,查不到别家)。
    field ∈ sku/upc/gtin/productName/wpid/isbn/ean;
    ⚠ 实测 field=itemId 连自有商品都 404,自有目录用 sku 查(旧系统实证)。
    查询型 POST 幂等,开自动重试是安全的。
    """
    if field == "itemId":
        raise ValueError("catalog_search 不支持 field=itemId(实测 404),自有目录请用 sku")

    _client.rate_acquire("items.catalog_search", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    status, _, data = _client.safe_post_ex(
        f"{_client.base_url()}/v3/items/catalog/search",
        token, store["client_id"], store["proxy"],
        json_body={"query": {"field": field, "value": value}}, max_retries=3)
    if status != 200:
        raise RuntimeError(f"catalog/search 返回 {status}: {data}")
    return (data or {}).get("payload") or []


def summarize_search_item(item: dict) -> dict:
    """输入:walmart/search DEFAULT 的单个 item → 输出:扁平摘要 dict(处理双命名兜底)。"""
    price = item.get("price") or {}
    props = item.get("properties") or {}
    return {
        "item_id": item.get("itemId"),
        "title": item.get("title"),
        "brand": item.get("brand"),
        "product_type": item.get("productType"),
        "price": price.get("amount"),
        "currency": price.get("currency"),
        "rating": item.get("customerRating"),
        "reviews": _pick(props, "numReviews", "num_reviews"),
        "marketplace": item.get("isMarketPlaceItem"),
        "variants": _pick(props, "variantItemsNum", "variant_items_num"),
        "categories": " > ".join(props.get("categories") or []) or None,
    }
