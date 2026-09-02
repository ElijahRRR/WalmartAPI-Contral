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

_MAX_PAGES = 500        # 与 api/returns 同款翻页闸(蓝图 §8.7:cursor 家族同一契约)


def _order_list(data) -> list[dict]:
    """输入:一页响应体 → 输出:订单对象列表(单元素不包 list 的形状也归一)。"""
    v = ((data or {}).get("list") or {}).get("elements") or {}
    v = v.get("order") or []
    return v if isinstance(v, list) else [v]


def _page_should_stop(cursor, page_orders, seen_cursors: set, store: dict, page: int) -> bool:
    """输入:本页 nextCursor/订单数/已见游标集 → 输出:是否停止翻页(带告警)。

    三道闸照抄 api/returns(2026-09-02 补齐,此前只看 cursor 是否为空):
    无 cursor 或空页即止;**同 cursor 重复 = 服务端未推进,立即停**——否则同一页
    会被反复 extend 进结果,后到的对象静默覆盖先到的(下单时间事故的通道之一)。
    """
    if not cursor or not page_orders:
        return True
    if cursor in seen_cursors:
        logger.warning("GET /v3/orders nextCursor 重复,停止翻页(店铺 %s 第 %d 页)",
                       store.get("name"), page)
        return True
    seen_cursors.add(cursor)
    return False


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
    seen_cursors: set[str] = set()
    for page in range(1, _MAX_PAGES + 1):
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

        meta = ((data or {}).get("list") or {}).get("meta") or {}
        if first_page and stats is not None:
            stats["total"] = int(meta.get("totalCount") or 0)
        first_page = False
        page_orders = _order_list(data)
        yield from page_orders
        cursor_suffix = meta.get("nextCursor")
        if _page_should_stop(cursor_suffix, page_orders, seen_cursors, store, page):
            return
    logger.warning("GET /v3/orders 触达翻页上限 %d 页,可能未拉全(店铺 %s)",
                   _MAX_PAGES, store.get("name"))


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
        except (httpx.HTTPError, *_client.SOCKS_ERRORS) as e:
            # SOCKS 层异常不在 httpx 异常树上(08-26 事故根因),必须并列接住,
            # 否则一次 "Malformed reply" 直接穿出去、拿不到退避重试
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
            _check_correlation_echo(resp, store, url)
            try:
                data = resp.json()
            except ValueError:
                data = None
        return status, headers, data


def _check_correlation_echo(resp: httpx.Response, store: dict, url: str) -> None:
    """输入:响应 → 输出:无(沃尔玛回显的 WM_QOS.CORRELATION_ID 与请求不符即告警)。

    下单时间事故(2026-09-02)取证口子:每个请求都带一个新 uuid,沃尔玛在响应头
    原样回显;若哪一层(httpx/h2/代理)把别的请求的响应配给了本请求,这里是唯一
    能当场看见的地方。先只告警计数,生产确认回显稳定后再升级为拒收该页。
    """
    sent = resp.request.headers.get("WM_QOS.CORRELATION_ID")
    got = resp.headers.get("WM_QOS.CORRELATION_ID")
    if sent and got and sent != got:
        logger.warning("响应相关 ID 与请求不符(店铺 %s %s):请求 %s / 响应 %s —— 疑似响应错配",
                       store.get("name"), url, sent, got)


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
        seen_cursors: set[str] = set()
        client = _build_async_client(store["proxy"])
        try:
            for page in range(1, _MAX_PAGES + 1):
                # orders.list = 进程内高频桶(3000/min/店),to_thread 防它
                # 罕见节流时睡死事件循环
                await asyncio.to_thread(_client.rate_acquire, "orders.list",
                                        store["client_id"])
                # _get_async 的 401 自愈只刷新了它自己的形参:第 2 页起必须从
                # 缓存取新 token,否则整店拿死 token 翻页 → 被误判凭证失效
                token = (_client._token_cache.get(store["client_id"]) or {}
                         ).get("token") or token
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
                meta = ((data or {}).get("list") or {}).get("meta") or {}
                if first_page:
                    total = int(meta.get("totalCount") or 0)
                    first_page = False
                page_orders = _order_list(data)
                orders.extend(page_orders)
                cursor_suffix = meta.get("nextCursor")
                if _page_should_stop(cursor_suffix, page_orders, seen_cursors,
                                     store, page):
                    break
            else:
                logger.warning("GET /v3/orders 触达翻页上限 %d 页,可能未拉全(店铺 %s)",
                               _MAX_PAGES, store.get("name"))
        finally:
            await client.aclose()
        # 服务端 totalCount 与实拉对象数不符 = 翻页重放或对象重复的指纹
        # (2026-09-02 下单时间事故后补的口子),必须响亮
        if total and len(orders) > total:
            logger.warning("店铺 %s 拉到 %d 个订单对象 > 服务端 totalCount %d,"
                           "疑似翻页重放/对象重复", store["name"], len(orders), total)
        lines = None
        if handler is not None:     # 持久化回调注入(依赖铁律:api 不碰 services)
            lines = await asyncio.to_thread(handler, store, orders)
        return {"store": store["name"], "orders": total, "lines": lines}


def fetch_orders_bulk(stores: list[dict], *, created_start: str | None = None,
                      created_end: str | None = None,
                      last_modified_start: str | None = None,
                      limit: int = 200, concurrency: int = 12,
                      handler=None
                      ) -> tuple[list[dict], list[str], list[tuple[str, Exception]]]:
    """输入:店铺列表 + 时间窗(+ 每店持久化回调)→ 输出:(结果, 凭证死店, [(失败店, 异常)])。

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
            # (店名, 异常) 原样上交:归类词(store_retry.diagnose)由 workflow
            # 层配 —— api 不准 import services(铁律 1)
            failed.append((store["name"], outcome))
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
