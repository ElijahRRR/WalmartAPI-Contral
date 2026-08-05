"""沃尔玛 Items 域接口(函数面与配额定稿见 docs/api_blueprint.md §3/§7)。

已实现端点(按工作流迁移进度收录):
  search_walmart()       GET /v3/items/walmart/search(DEFAULT)  全站目录搜索    [product_query]
  search_walmart_spec()  同端点 responseFormat=SPEC             跟卖路由/标识互转 [product_query]
  catalog_search()       POST /v3/items/catalog/search          本店目录精确查询  [product_query]
  list_items()           GET /v3/items                          分页模型1        [catalog_sync]
  iter_all_items()       5 轮组合全量扫店(去重生成器)                            [catalog_sync]
  get_item()             GET /v3/items/{sku}                    单查补漏         [catalog_sync]

其余函数(count_items / get_spec)按蓝图 §7 签名预留,随对应工作流迁移时实现
——不自创签名(CLAUDE.md api 层收录规则)。

响应字段坑(旧系统实测,api 层统一兜底):
- 线上返回 camelCase,本地 OpenAPI 规格是 snake_case(numReviews/num_reviews 等)→ _pick 双键
- SPEC 的 productIdentifiers 位置随 feedType 变:MP_ITEM_MATCH 在 MPItem[0].Item,
  MP_ITEM 在 MPItem[0].Orderable
- 这些是公开目录只读端点,与店铺无关,任一有效店铺 token 均可查
"""

import html
import json
import logging
import re

from api import _client

logger = logging.getLogger("api.items")

_TAG_RE = re.compile(r"<[^>]+>")


def _pick(d: dict, *keys, default=None):
    """camelCase / snake_case 双键取值:依次尝试 keys,取第一个存在的。"""
    for k in keys:
        if k in d:
            return d[k]
    return default


def clean_text(s: str | None) -> str | None:
    """输入:可能含 HTML 标签的文本 → 输出:去标签+反转义后的纯文本。

    Item Search 的 title/description 实测带 <mark> 等标签(搜索词高亮)。
    """
    if not s:
        return s
    return html.unescape(_TAG_RE.sub("", s)).strip()


def fmt_shelf(shelf) -> str | None:
    """输入:catalog 的 shelf 字段(形如 '["A","B"]' 的 JSON 数组字符串)→ 输出:'A > B'。"""
    if not shelf:
        return None
    if isinstance(shelf, str):
        try:
            shelf = json.loads(shelf)
        except ValueError:
            return shelf
    if isinstance(shelf, list):
        return " > ".join(str(p) for p in shelf) or None
    return str(shelf)


# 5 轮全量扫店组合(镜像旧 sync_status_track,生产对拍过 99,197 商品):
# 前 4 轮扫 ACTIVE 下的四种发布状态,第 5 轮扫 RETIRED 全量。
_SWEEP_ROUNDS: list[tuple[str, str | None]] = [
    ("ACTIVE", "PUBLISHED"),
    ("ACTIVE", "UNPUBLISHED"),
    ("ACTIVE", "SYSTEM_PROBLEM"),
    ("ACTIVE", "STAGE"),
    ("RETIRED", None),
]


def _guard_store_dead(status: int, store: dict) -> None:
    if status in (401, 403):
        raise _client.StoreDeadError(store["name"], status)


def list_items(store: dict, *, published_status: str | None = None,
               lifecycle_status: str | None = None,
               limit: int = 1000, max_offset: int = 10000) -> tuple[list[dict], bool]:
    """输入:店铺 + 状态过滤 → 输出:(商品列表, 是否因 offset 上限被截断)。

    GET /v3/items,分页模型 1(蓝图 §4):首页 nextCursor='*' 换取真 cursor 后
    全程不变(快照会话 ID),真翻页靠 offset 递增;offset 硬上限 10000(超限 400);
    cursor 约 2 分钟过期(400 → 重置 '*' 整轮重试一次)。截断部分调用方用
    get_item() 单查补漏。
    """
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])

    def _sweep() -> tuple[list[dict], bool]:
        collected: list[dict] = []
        cursor = "*"
        offset = 0
        while True:
            params: dict = {"limit": limit, "offset": offset, "nextCursor": cursor}
            if published_status:
                params["publishedStatus"] = published_status
            if lifecycle_status:
                params["lifecycleStatus"] = lifecycle_status
            _client.rate_acquire("items.list", store["client_id"])
            status, _, data = _client.safe_get_ex(
                f"{_client.base_url()}/v3/items",
                token, store["client_id"], store["proxy"], params=params, max_retries=3)
            _guard_store_dead(status, store)
            if status == 400 and cursor != "*":
                raise _CursorExpired()          # cursor 过期,由外层整轮重来一次
            if status != 200:
                raise RuntimeError(f"GET /v3/items 返回 {status}(店铺 {store['name']}): {data}")
            page = (data or {}).get("ItemResponse") or []
            total = (data or {}).get("totalItems") or 0
            if cursor == "*":
                cursor = (data or {}).get("nextCursor") or cursor
            collected.extend(page)
            offset += len(page)
            if not page or offset >= total:
                return collected, False
            if offset >= max_offset:
                logger.warning("GET /v3/items 店铺 %s 达 offset 上限 %d(total=%d),截断待补漏",
                               store["name"], max_offset, total)
                return collected, True

    try:
        return _sweep()
    except _CursorExpired:
        logger.info("GET /v3/items cursor 过期(店铺 %s),重置整轮重试一次", store["name"])
        return _sweep()


class _CursorExpired(Exception):
    pass


def iter_all_items(store: dict, stats: dict | None = None):
    """输入:店铺(可选 stats dict 收集统计)→ 输出:生成器,按 SKU 去重逐个产出商品。

    5 轮组合全量扫店(_SWEEP_ROUNDS),跨轮按 sku 去重(以先出现的为准)。
    stats 若传入,填充 {"truncated": bool, "total": int, "rounds": {轮标识: 条数}}
    ——truncated=True 时调用方须用 get_item() 对已知 SKU 单查补漏。
    """
    seen: set[str] = set()
    truncated_any = False
    rounds_stat: dict[str, int] = {}
    for lifecycle, published in _SWEEP_ROUNDS:
        items, truncated = list_items(store, lifecycle_status=lifecycle,
                                      published_status=published)
        truncated_any = truncated_any or truncated
        key = f"{lifecycle}/{published or 'ALL'}"
        rounds_stat[key] = len(items)
        for item in items:
            sku = item.get("sku")
            if sku and sku not in seen:
                seen.add(sku)
                yield item
    if stats is not None:
        stats.update(truncated=truncated_any, total=len(seen), rounds=rounds_stat)


def get_item(store: dict, sku: str) -> dict | None:
    """输入:店铺 + SKU → 输出:单品 dict,404 返回 None(真 NOT_FOUND 语义)。

    只作补漏用,禁止用于批量拿数据(旧教训:454 SKU 单查 = 8 分钟)。
    """
    _client.rate_acquire("items.get", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    from urllib.parse import quote
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/items/{quote(str(sku), safe='')}",
        token, store["client_id"], store["proxy"], max_retries=3)
    _guard_store_dead(status, store)
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"GET /v3/items/{sku} 返回 {status}: {data}")
    payload = (data or {}).get("ItemResponse") or []
    return payload[0] if payload else None


def summarize_item(item: dict) -> dict:
    """输入:GET /v3/items 的单个 ItemResponse → 输出:扁平摘要 dict(catalog_sync 落库用)。"""
    price = item.get("price") or {}
    reasons = _pick(item, "unpublishedReasons", "unpublished_reasons") or {}
    if isinstance(reasons, dict):
        reasons = reasons.get("reason") or []
    return {
        "sku": item.get("sku"),
        "wpid": item.get("wpid"),
        "upc": item.get("upc"),
        "gtin": item.get("gtin"),
        "product_name": clean_text(item.get("productName")),
        "shelf": fmt_shelf(item.get("shelf")),
        "product_type": item.get("productType"),
        "price": price.get("amount"),
        "currency": _pick(price, "currency", "unit"),
        "published_status": item.get("publishedStatus"),
        "lifecycle_status": item.get("lifecycleStatus"),
        "unpublished_reasons": "; ".join(reasons) if isinstance(reasons, list) else str(reasons),
    }


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
    """输入:walmart/search DEFAULT 的单个 item → 输出:扁平摘要 dict。

    坑点全内置:camelCase/snake_case 双兜底、title/description 去 <mark> 标签、
    images 数组拆主图/全部图片。注意:关键词搜索结果的 price 常缺(官方行为,
    价格只在 catalog 自有目录稳定有)。
    """
    price = item.get("price") or {}
    props = item.get("properties") or {}
    imgs = [i.get("url") for i in item.get("images") or [] if i.get("url")]
    return {
        "item_id": item.get("itemId"),
        "title": clean_text(item.get("title")),
        "brand": item.get("brand"),
        "product_type": item.get("productType"),
        "categories": " > ".join(props.get("categories") or []) or None,
        "price": price.get("amount"),
        "currency": price.get("currency"),
        "rating": item.get("customerRating"),
        "reviews": _pick(props, "numReviews", "num_reviews"),
        "variants": _pick(props, "variantItemsNum", "variant_items_num"),
        "next_day_eligible": _pick(props, "nextDayEligible", "next_day_eligible"),
        "marketplace": item.get("isMarketPlaceItem"),
        "main_image": imgs[0] if imgs else None,
        "all_images": " | ".join(imgs) or None,
        "description": clean_text(item.get("description")),
    }


def summarize_catalog_item(h: dict) -> dict:
    """输入:catalog/search payload 的单个条目 → 输出:扁平摘要 dict。

    坑点内置:货币双兜底(线上 price.currency,规格写 price.unit 是错的)、
    shelf JSON 数组字符串美化为 'A > B'。
    """
    price = h.get("price") or {}
    reasons = _pick(h, "unpublishedReasons", "unpublished_reasons") or []
    return {
        "item_id": h.get("itemId") or h.get("wpid"),
        "sku": h.get("sku"),
        "wpid": h.get("wpid"),
        "upc_gtin": h.get("gtin") or h.get("upc"),
        "title": h.get("productName"),
        "brand": h.get("brand"),
        "product_type": h.get("productType"),
        "categories": fmt_shelf(h.get("shelf")),
        "price": price.get("amount"),
        "currency": _pick(price, "currency", "unit"),
        "published_status": h.get("publishedStatus"),
        "lifecycle_status": h.get("lifecycleStatus"),
        "unpublished_reasons": "; ".join(reasons) if isinstance(reasons, list) else reasons,
    }
