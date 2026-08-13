"""沃尔玛 Orders 域接口(蓝图矩阵 #23)。

  iter_orders()        订单生成器,分页模型 2(蓝图 §4):meta.nextCursor 返回带
                       '?' 的完整 query 串,直接拼在 /v3/orders 后;单店内必须串行翻页。
  fetch_orders_bulk()  多店并发拉取(蓝图 §6.3 async 变体,2026-08-13 落地):
                       同步门面,内部 asyncio + httpx.AsyncClient——单店内翻页
                       仍串行(cursor 语义),**店与店之间并发**;async 只藏在
                       本文件内部,调用方保持同步世界(不另起体系)。
                       持久化经 handler 回调注入(api 层不 import services)。

stats dict(可选传入)填充 {"total": 首页 meta.totalCount}——总单数以它为准,
不要数生成器条数(翻页中新单进来会导致两者不一致)。
"""

import asyncio
import logging

import httpx

from api import _client

logger = logging.getLogger("api.orders")


def iter_orders(store: dict, *, created_start: str | None = None,
                created_end: str | None = None,
                last_modified_start: str | None = None,
                limit: int = 200, stats: dict | None = None):
    """输入:店铺 + 时间过滤(ISO8601 字符串)→ 输出:订单 dict 生成器。"""
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    params: dict = {"limit": limit}
    if created_start:
        params["createdStartDate"] = created_start
    if created_end:
        params["createdEndDate"] = created_end
    if last_modified_start:
        params["lastModifiedStartDate"] = last_modified_start

    url = f"{_client.base_url()}/v3/orders"
    cursor_suffix: str | None = None
    first_page = True
    while True:
        _client.rate_acquire("orders.list", store["client_id"])
        if cursor_suffix:
            # 分页模型 2:nextCursor 就是完整 query 串(含 '?'),直接拼 URL,不再带 params
            status, _, data = _client.safe_get_ex(
                url + cursor_suffix, token, store["client_id"], store["proxy"], max_retries=3)
        else:
            status, _, data = _client.safe_get_ex(
                url, token, store["client_id"], store["proxy"], params=params, max_retries=3)
        if status in (401, 403):
            raise _client.StoreDeadError(store["name"], status)
        if status == 404:       # 窗口内零订单(与 items 家族同款语义)
            if first_page and stats is not None:
                stats["total"] = 0
            return
        if status != 200 or data is None:
            raise RuntimeError(f"GET /v3/orders 返回 {status}(店铺 {store['name']})")

        lst = (data or {}).get("list") or {}
        meta = lst.get("meta") or {}
        if first_page and stats is not None:
            stats["total"] = int(meta.get("totalCount") or 0)
        first_page = False
        for order in (lst.get("elements") or {}).get("order") or []:
            yield order
        cursor_suffix = meta.get("nextCursor")
        if not cursor_suffix:
            return


# ── async 变体(蓝图 §6.3;只服务多店订单拉取,勿在别处复用)──────────────────

def _build_async_client(proxy: str | None) -> httpx.AsyncClient:
    """每店固定出口代理铁律在此落地。

    测试缝与同步世界共用:先问 _client._build_transport——测试把它换成
    httpx.MockTransport(双栈,Async 也认)时直接沿用,**保证 async 路径
    永远走同一个桩,不会绕出去真打沃尔玛**;生产返回的是同步 HTTPTransport,
    弃用之(未开连接,零成本),换 Async 同参版本。
    """
    t = _client._build_transport(proxy)
    if not isinstance(t, httpx.MockTransport):
        t = httpx.AsyncHTTPTransport(
            proxy=proxy, retries=2, http2=_client._HTTP2,
            limits=httpx.Limits(max_keepalive_connections=20,
                                max_connections=50))
    return httpx.AsyncClient(transport=t,
                             timeout=httpx.Timeout(30.0, connect=15.0))


async def _get_async(client: httpx.AsyncClient, url: str, store: dict,
                     token: str, *, params: dict | None = None,
                     max_retries: int = 3) -> tuple[int | None, dict, dict | None]:
    """输入:AsyncClient + URL + 店铺/token → 输出:(status, headers, data)。

    镜像 _client._request_ex 的 GET 语义(那是同步世界的唯一路径,本函数是
    蓝图批准的 async 孪生,重试口径必须与它一致):
      · 429:按 Retry-After / X-Next-Replenishment-Time 退避
      · 5xx / 网络异常:指数退避(1s→2s→4s,网络另有 transport retries=2)
      · 401:清 token 缓存重新换一次(独立于 max_retries,最多 1 次)
    """
    attempt, refreshed_401 = 0, False
    while True:
        try:
            resp = await client.get(
                url, params=params,
                headers=_client.make_headers(token, store["client_id"]))
        except httpx.HTTPError as e:
            logger.warning("GET %s 网络失败(店铺 %s): %s", url, store["name"], e)
            if attempt < max_retries:
                attempt += 1
                await asyncio.sleep(min(2 ** attempt, 10))
                continue
            return None, {}, None
        status = resp.status_code
        headers = {k.lower(): v for k, v in resp.headers.items()}
        if status == 401 and not refreshed_401:
            refreshed_401 = True
            _client._token_cache.pop(store["client_id"], None)
            token = await asyncio.to_thread(
                _client.get_token, store["client_id"],
                store["client_secret"], store["proxy"])
            continue
        if status == 429 and attempt < max_retries:
            attempt += 1
            await asyncio.sleep(_client._parse_retry_after(headers))
            continue
        if 500 <= status < 600 and attempt < max_retries:
            attempt += 1
            await asyncio.sleep(min(2 ** attempt, 10))
            continue
        data = None
        if 200 <= status < 300:
            try:
                data = resp.json()
            except ValueError:
                data = None
        return status, headers, data


async def _fetch_store(store: dict, sem: asyncio.Semaphore, *,
                       created_start, created_end, last_modified_start,
                       limit, handler) -> dict:
    """输入:单店 → 输出:{"store", "orders", "lines"}。翻页串行(cursor 语义)。"""
    async with sem:
        token = await asyncio.to_thread(
            _client.get_token, store["client_id"], store["client_secret"],
            store["proxy"])
        params: dict = {"limit": limit}
        if created_start:
            params["createdStartDate"] = created_start
        if created_end:
            params["createdEndDate"] = created_end
        if last_modified_start:
            params["lastModifiedStartDate"] = last_modified_start
        url = f"{_client.base_url()}/v3/orders"
        orders: list[dict] = []
        total = 0
        cursor_suffix: str | None = None
        first_page = True
        client = _build_async_client(store["proxy"])
        try:
            while True:
                # orders.list = 进程内高频桶(3000/min/店),to_thread 防它
                # 罕见节流时睡死事件循环
                await asyncio.to_thread(_client.rate_acquire, "orders.list",
                                        store["client_id"])
                if cursor_suffix:   # 分页模型 2:nextCursor 是完整 query 串
                    status, _, data = await _get_async(
                        client, url + cursor_suffix, store, token)
                else:
                    status, _, data = await _get_async(
                        client, url, store, token, params=params)
                if status in (401, 403):
                    raise _client.StoreDeadError(store["name"], status)
                if status == 404:       # 窗口内零订单(与 iter_orders 同义)
                    break
                if status != 200 or data is None:
                    raise RuntimeError(
                        f"GET /v3/orders 返回 {status}(店铺 {store['name']})")
                lst = (data or {}).get("list") or {}
                meta = lst.get("meta") or {}
                if first_page:
                    total = int(meta.get("totalCount") or 0)
                    first_page = False
                orders.extend((lst.get("elements") or {}).get("order") or [])
                cursor_suffix = meta.get("nextCursor")
                if not cursor_suffix:
                    break
        finally:
            await client.aclose()
        lines = None
        if handler is not None:     # 持久化回调注入(依赖铁律:api 不碰 services)
            lines = await asyncio.to_thread(handler, store, orders)
        return {"store": store["name"], "orders": total, "lines": lines}


def fetch_orders_bulk(stores: list[dict], *, created_start: str | None = None,
                      created_end: str | None = None,
                      last_modified_start: str | None = None,
                      limit: int = 200, concurrency: int = 12,
                      handler=None) -> tuple[list[dict], list[str], list[str]]:
    """输入:店铺列表 + 时间窗(+ 每店持久化回调)→ 输出:(结果, 凭证死店, 失败店)。

    同步门面:内部 asyncio.run 跑 30+ 店并发(蓝图 §6.3),调用方无感。
    handler(store, orders)->int 在线程池执行(网络与入库重叠),返回值记入
    结果的 "lines";单店异常不拖垮整轮——凭证/代理死店与其它失败分列。
    """
    async def _run():
        sem = asyncio.Semaphore(max(1, min(concurrency, len(stores))))
        tasks = [_fetch_store(s, sem, created_start=created_start,
                              created_end=created_end,
                              last_modified_start=last_modified_start,
                              limit=limit, handler=handler) for s in stores]
        return await asyncio.gather(*tasks, return_exceptions=True)

    results, dead, failed = [], [], []
    for store, outcome in zip(stores, asyncio.run(_run())):
        if isinstance(outcome, _client.StoreDeadError):
            logger.error("店铺 %s 凭证/代理失效跳过: %s", store["name"], outcome)
            dead.append(store["name"])
        elif isinstance(outcome, BaseException):
            logger.error("店铺 %s 订单拉取失败: %s", store["name"], outcome)
            failed.append(f"{store['name']}({outcome})")
        else:
            results.append(outcome)
    return results, dead, failed


def order_product_sales(order: dict) -> float:
    """输入:单个订单 dict → 输出:该订单 PRODUCT 类费用合计(销售额口径,旧系统同款)。"""
    total = 0.0
    lines = ((order.get("orderLines") or {}).get("orderLine")) or []
    for line in lines:
        charges = ((line.get("charges") or {}).get("charge")) or []
        for c in charges:
            if c.get("chargeType") == "PRODUCT":
                amt = (c.get("chargeAmount") or {}).get("amount")
                total += float(amt or 0)
    return round(total, 2)
