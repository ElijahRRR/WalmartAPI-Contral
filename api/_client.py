"""沃尔玛 API 公共客户端(自旧仓库 walmart_client.py 移植,行为保持一致)。

提供:
  get_token()      获取 access_token(线程安全 + 进程内 900s 缓存)
  make_headers()   构造标准请求头
  base_url()       API base(registry 取值,env WALMART_BASE_URL 覆盖用于沙箱)
  safe_get()       带错误处理的 GET,返回 dict|None(简化接口)
  safe_get_ex()    带状态码/headers 的 GET,返回 (status:int|None, headers:dict, data:dict|None)
  safe_post()      带错误处理的 POST,返回 dict|None
  safe_post_ex()   带状态码/headers 的 POST
  safe_put_ex()    带状态码/headers 的 PUT

安全铁律:所有沃尔玛调用必须走本文件,每店铺固定出口代理,严禁直连——
每个店铺凭证绑定固定出口 IP,直连会导致店铺关联封号。

店铺凭证的读取不在本层(见 services/stores.py):本层不碰飞书、不碰 xlsx。

可选 opt-in:
    safe_get_ex(..., max_retries=3)  # 自动 429/5xx 退避重试

环境变量:
    WALMART_BASE_URL          自定义 API base(默认 production),用于沙箱测试
    WALMART_DEFAULT_RETRIES   safe_get 默认重试次数(默认 2);safe_post 因幂等性始终默认 0
    WALMART_HTTP2             "0" 关闭 HTTP/2(默认开启)
"""

import atexit
import base64
import logging
import os
import socket
import threading
import time
import uuid

import httpx

from registry import resources

# ⭐ 防御 SSL/SOCKS hang(旧仓库 2026-05-12 事故):httpx 的 timeout 在走 socks5 代理时
# 不可靠,实测一次 daily noon reconcile 卡在 _ssl__SSLSocket_read.poll() 2.5h 不返回。
# 这里设全局 socket 默认超时(90s)兜底——任何忘记传 timeout 或代理层 bug 都受这个保护。
# 注意这是进程级全局副作用,import 本模块即影响进程内所有 socket(含飞书调用)。
socket.setdefaulttimeout(90)

logger = logging.getLogger("api.walmart")


class StoreDeadError(Exception):
    """店铺凭证失效(401/403 经 401 自愈后仍失败)。

    调用方语义:跳过该店铺的全部剩余调用,而不是逐页重试
    (蓝图 §6.4;旧 sync_status_track 的正确做法标准化)。
    """

    def __init__(self, store_name: str, status: int):
        self.store_name = store_name
        self.status = status
        super().__init__(f"店铺 {store_name} 凭证失效(HTTP {status}),跳过全店")

# client_id → {"token": str, "expires_at": float, "secret": str, "proxy": str}
# secret + proxy 保留是为了 401 时能就地刷新 token,不需要调用方再传一次。
_token_cache: dict = {}
_token_lock = threading.Lock()

# proxy URL → 长连接 httpx.Client(按代理维度池化,复用 TCP/TLS/SOCKS 握手)
_client_pool: dict = {}
_pool_lock = threading.Lock()

# HTTP/2: 默认开启,依赖 h2 包。装了就用,没装自动回落 H1.1。
try:
    import h2  # noqa: F401

    _HAS_H2 = True
except ImportError:
    _HAS_H2 = False

_HTTP2 = _HAS_H2 and (os.environ.get("WALMART_HTTP2", "1") != "0")
if not _HAS_H2 and os.environ.get("WALMART_HTTP2", "1") != "0":
    logger.warning("h2 未安装,HTTP/2 已禁用;启用:pip install 'httpx[http2]'")

# safe_get 旧接口的默认重试次数;safe_post 因幂等性问题不用此默认值;
# ex 接口由调用方显式传 max_retries,不受此变量影响。
_DEFAULT_RETRIES = int(os.environ.get("WALMART_DEFAULT_RETRIES", "2"))


def base_url() -> str:
    """输入:无 → 输出:沃尔玛 API base URL(从 registry 取)。"""
    return resources.walmart_base_url()


# ══════════════════════════════════════════════════════════════════════════════
#  连接池
# ══════════════════════════════════════════════════════════════════════════════


def _build_transport(proxy: str | None) -> httpx.BaseTransport:
    """transport 构造单独成函数,测试用 httpx.MockTransport 替换此处。"""
    return httpx.HTTPTransport(
        proxy=proxy,
        retries=2,
        http2=_HTTP2,
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
    )


def _get_client(proxy: str | None) -> httpx.Client:
    """按代理 URL 维度复用 httpx.Client,避免每次请求重新握手。

    transport 层启用:
      - HTTP/2(多路复用;协商失败自动回落 H1.1)
      - retries=2(连接级失败:DNS / TCP / TLS 握手自动重试,便宜)
      - limits 写在 transport 上(httpx.Client 收到自定义 transport 后会忽略顶层 limits)
    """
    key = proxy or ""
    client = _client_pool.get(key)
    if client is not None and not client.is_closed:
        return client
    with _pool_lock:
        client = _client_pool.get(key)
        if client is None or client.is_closed:
            client = httpx.Client(
                transport=_build_transport(proxy),
                timeout=httpx.Timeout(30.0, connect=15.0),
            )
            _client_pool[key] = client
        return client


def _invalidate_client(proxy: str | None) -> None:
    """半死连接自愈:把对应代理的 client 从池里移除并关闭。

    场景:keep-alive 连接被代理静默 RST、SOCKS 隧道挂掉但 socket 仍存活、
    HTTP/2 共享连接进入 GOAWAY 后所有 stream 都失败。下一次 _get_client 会
    建一个全新连接,把 dead state 抛掉。
    """
    key = proxy or ""
    with _pool_lock:
        client = _client_pool.pop(key, None)
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


@atexit.register
def _close_all_clients() -> None:
    """脚本退出时关闭所有长连接,避免 ResourceWarning。"""
    for c in list(_client_pool.values()):
        try:
            c.close()
        except Exception:
            pass
    _client_pool.clear()


# ══════════════════════════════════════════════════════════════════════════════
#  per-store 令牌桶(设计定稿:docs/api_blueprint.md §3/§6)
# ══════════════════════════════════════════════════════════════════════════════

# bucket 登记表:{bucket 名: (次数上限, 窗口秒)}。取值为蓝图第 3 节"定稿"列(留官方余量)。
# 共享桶用同一个 bucket 名(如 catalog/search 与 associations 同桶)。
# 随工作流迁移逐个补充;⚠ 未登记的 bucket 一律拒绝——旧系统对未知键放行,
# RETIRE_ITEM 实际零限速跑了数月,此处反其道设计。
_RATE_BUCKETS: dict[str, tuple[int, float]] = {
    "items.walmart_search": (180, 60.0),        # GET /v3/items/walmart/search(官方 200/min,独立桶)
    "items.walmart_search_spec": (950, 86400.0),  # 同端点 SPEC 格式的每日附加额度(官方 1000/day)
    "items.catalog_search": (180, 60.0),        # POST catalog/search 与 associations 共享(官方 200/min)
    "items.list": (55, 60.0),                   # GET /v3/items 带 query 参数(官方 60/min,页间≈1.1s)
    "items.list_nofilter": (250, 60.0),         # GET /v3/items 无参数(官方 300/min)
    "items.get": (800, 60.0),                   # GET /v3/items/{sku}(官方 900/min;补漏单查 ≤8 并发)
    "inventory.list": (180, 60.0),              # GET /v3/inventories(官方 200/min,单店 cursor 强制串行)
    "inventory.get": (180, 60.0),               # GET /v3/inventory?sku=(官方未单列,按 bulk 同档保守)
    "returns.list": (46, 60.0),                 # GET /v3/returns(官方 50/min,沿用旧 1.3s 节奏)
    "reports.request": (2, 3600.0),             # POST reportRequests:配额极低(测试期 429 实证)
    "reports.poll": (55, 60.0),                 # GET reportRequests/{id} 与 downloadReport
    "reports.payment_statement": (12, 60.0),    # GET /v3/report/payment/statement(官方 15/min)
    "reports.recon": (80, 60.0),                # reconreport 两端点共用(官方 reconFile 100/min)
    "orders.list": (3000, 60.0),                # GET /v3/orders(官方 5000/min)
    # feeds 域(蓝图 §3/§6 定稿):GET 全家共享桶;POST 各 feedType 独立桶,
    # 未登记的 feedType 默认拒绝——旧系统 RETIRE_ITEM 零限速就是未知键放行漏的
    "feeds.get": (3000, 60.0),                  # GET /v3/feeds 与 /{feedId}(官方 5000/min 共享)
    "feeds.post.DELETE_ITEM": (6, 3600.0),      # 官方 10/hour,最高危留余量
    "feeds.post.RETIRE_ITEM": (6, 3600.0),      # 官方无现值(guide 已消失),按 DELETE 同档保守
    "feeds.post.MP_MAINTENANCE": (8, 3600.0),   # 官方 10/hour
}
# insights performance 类:官方 1/min/端点,summary 与 report 各自独立端点 → 逐个登记
for _m in ("otd", "cancellations", "vtr", "srr", "refunds",
           "negativeFeedback", "returns", "itemNotReceived"):
    _RATE_BUCKETS[f"insights.summary.{_m}"] = (1, 60.0)
    _RATE_BUCKETS[f"insights.report.{_m}"] = (1, 60.0)

_rate_state: dict = {}   # (client_id, bucket) → deque[单调时间戳]
_rate_lock = threading.Lock()


def rate_acquire(bucket: str, client_id: str) -> float:
    """输入:bucket 名 + 店铺 client_id → 输出:本次实际等待秒数(限流按店铺维度)。

    滑动窗口计数;窗口满时阻塞睡到最早一次调用滑出窗口。
    bucket 未登记直接抛 KeyError(默认拒绝,不放行)。
    """
    from collections import deque

    if bucket not in _RATE_BUCKETS:
        raise KeyError(f"限速桶未登记: {bucket}(先在 api/_client._RATE_BUCKETS 按蓝图定稿登记)")
    limit, window = _RATE_BUCKETS[bucket]
    waited = 0.0
    while True:
        with _rate_lock:
            q = _rate_state.setdefault((client_id, bucket), deque())
            now = time.monotonic()
            while q and now - q[0] >= window:
                q.popleft()
            if len(q) < limit:
                q.append(now)
                return waited
            sleep_for = window - (now - q[0]) + 0.01
        # 微等待(<1s)是贴着限速上限跑的正常状态,降为 DEBUG 防日志刷屏
        logger.log(logging.INFO if sleep_for >= 1.0 else logging.DEBUG,
                   "限速桶 %s(店铺 %s)已满,等待 %.1fs", bucket, client_id[:8], sleep_for)
        time.sleep(sleep_for)
        waited += sleep_for


def safe_get_raw(url, token, client_id, proxy, params=None, timeout=90,
                 max_retries=3, accept: str | None = None) -> tuple[int | None, dict, bytes | None]:
    """输入:同 safe_get_ex(可选 accept 覆盖)→ 输出:(status, headers, 原始字节)。

    与 safe_get_ex 的区别只有一个:不解析 JSON(xlsx/zip/CSV 会被强解析破坏,
    旧系统因此养出裸 httpx 直连点,蓝图 §6.2 定稿收归此处)。
    accept:部分二进制端点强制要求特定 Accept(reconFile 需
    application/octet-stream,text/csv 会 406——订单中心v1 实证)。
    429 按 Retry-After/X-Next-Replenishment-Time 退避,5xx/网络异常指数退避。
    """
    attempt = 0
    while True:
        headers_out = make_headers(token, client_id)
        if accept:
            headers_out["Accept"] = accept
        try:
            resp = _get_client(proxy).get(
                url, headers=headers_out, params=params, timeout=timeout)
        except (httpx.TransportError, httpx.ProxyError) as e:
            _invalidate_client(proxy)
            if attempt < max_retries:
                wait = min(2 ** attempt, 10)
                logger.warning("⚠ GET(raw) 网络异常 %s,%ds 后重试: %s", url, wait, e)
                time.sleep(wait)
                attempt += 1
                continue
            logger.warning("✗ GET(raw) 请求异常 %s: %s", url, e)
            return None, {}, None
        status = resp.status_code
        headers = {k.lower(): v for k, v in resp.headers.items()}
        if attempt < max_retries and (status == 429 or status >= 500):
            wait = _parse_retry_after(headers) if status == 429 else min(2 ** attempt, 10)
            logger.warning("⚠ GET(raw) %d %s,%.1fs 后重试(第 %d/%d 次)",
                           status, url, wait, attempt + 1, max_retries)
            time.sleep(wait)
            attempt += 1
            continue
        return status, headers, resp.content


def download_bytes(url: str, proxy: str | None, timeout: int = 180) -> bytes:
    """输入:URL(如报表预签名下载地址)+ 代理 → 输出:响应字节。

    走店铺固定出口代理(与所有沃尔玛流量同链路,铁律),非 2xx 抛异常。
    """
    resp = _get_client(proxy).get(url, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


# ══════════════════════════════════════════════════════════════════════════════
#  认证
# ══════════════════════════════════════════════════════════════════════════════


def get_token(client_id, client_secret, proxy):
    """
    获取 access_token,有效期内自动复用缓存,无需重复请求。
    token 提前 60 秒过期,避免边界请求失败。
    线程安全:并发调用同一 client_id 时只换一次 token。
    失败时抛出异常,由调用方决定如何处理。
    """
    cached = _token_cache.get(client_id)
    if cached and time.time() < cached["expires_at"]:
        return cached["token"]

    with _token_lock:
        # 双检:避免并发场景下重复换 token
        cached = _token_cache.get(client_id)
        if cached and time.time() < cached["expires_at"]:
            return cached["token"]

        cred = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        client = _get_client(proxy)
        resp = client.post(
            f"{base_url()}/v3/token",
            headers={
                "Authorization": f"Basic {cred}",
                "WM_SVC.NAME": "Walmart Marketplace",
                "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
                "WM_CONSUMER.ID": client_id,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        expires_in = int(data.get("expires_in", 900))
        _token_cache[client_id] = {
            "token": token,
            "expires_at": time.time() + expires_in - 60,  # 提前 60s 过期
            "secret": client_secret,  # 401 自愈:就地刷新无需调用方再传
            "proxy": proxy,
        }
        return token


# ══════════════════════════════════════════════════════════════════════════════
#  请求工具
# ══════════════════════════════════════════════════════════════════════════════


def make_headers(token, client_id):
    """构造每次 API 调用所需的标准请求头。"""
    return {
        "Authorization": f"Bearer {token}",
        "WM_SVC.NAME": "Walmart Marketplace",
        "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
        "WM_CONSUMER.ID": client_id,
        "WM_SEC.ACCESS_TOKEN": token,
        "Accept": "application/json",
    }


def _parse_retry_after(headers: dict) -> float:
    """从响应 headers 解析下次可重试的等待秒数。

    优先级:Retry-After(标准)> X-Next-Replenishment-Time(Walmart 特有)> 默认 60s
    返回值上限 300s,避免脚本卡太久。
    """
    # Retry-After: 整数秒,或 HTTP date
    ra = headers.get("retry-after")
    if ra:
        try:
            return min(float(ra), 300.0)
        except (TypeError, ValueError):
            pass

    # X-Next-Replenishment-Time: Walmart 独有,可能是 ISO8601 或 epoch ms
    nrt = headers.get("x-next-replenishment-time")
    if nrt:
        try:
            # 先试 epoch ms
            ts = float(nrt)
            if ts > 1e12:  # ms
                ts = ts / 1000.0
            wait = ts - time.time()
            if 0 < wait < 300:
                return wait
        except (TypeError, ValueError):
            # 试 ISO 8601
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(nrt.replace("Z", "+00:00"))
                wait = dt.timestamp() - time.time()
                if 0 < wait < 300:
                    return wait
            except Exception:
                pass

    return 60.0  # 兜底


def _log(msg: str, quiet: bool, level: int = logging.WARNING) -> None:
    """quiet=True 时降为 DEBUG(替代旧版的 print + quiet 组合)。"""
    logger.log(logging.DEBUG if quiet else level, msg)


def _request_ex(method, url, token, client_id, proxy, *,
                json_body=None, params=None, timeout=30, quiet=False, max_retries=0):
    """
    统一的请求实现。返回 (status, headers, data)。

    - status:      HTTP 状态码 int;网络/超时异常时为 None
    - headers:     dict(lower-case key → str);异常时为 {}
    - data:        解析后的 JSON dict;非 2xx 或解析失败时为 None

    不抛异常、不 raise_for_status,让调用方按 status 自行决定后续动作。

    自愈机制(永远生效,与 max_retries 无关):
      - 401:清空 token 缓存 → 重新换 token → 重试 1 次。处理服务端撤销 token
             或时钟漂移导致的偶发 401,避免一直拿坏 token 撞墙。
      - TransportError / ProxyError:主动 invalidate 连接池里这个代理的
             httpx.Client,下一次请求会建全新连接。处理 SOCKS 隧道半死、
             keep-alive 被静默 RST 等 dead-pool 问题。

    opt-in 重试(max_retries > 0):
      - 429:按 Retry-After / X-Next-Replenishment-Time 退避
      - 5xx:指数退避(1s → 2s → 4s)
      - 网络异常:指数退避(同时已自愈连接池)
      - 其他状态码不重试
    """
    method = method.upper()
    attempt = 0
    refreshed_401 = False
    while True:
        try:
            client = _get_client(proxy)
            resp = client.request(
                method,
                url,
                headers=make_headers(token, client_id),
                json=json_body if json_body is not None else None,
                params=params,
                timeout=timeout,
            )
        except (httpx.TransportError, httpx.ProxyError) as e:
            # 连接级故障:池里这条连接很可能已坏,主动剔除让下次建新的
            _invalidate_client(proxy)
            _log(f"✗ {method} 网络失败(已剔除连接) {url}: {e}", quiet)
            if attempt < max_retries:
                attempt += 1
                time.sleep(min(2 ** attempt, 10))
                continue
            return None, {}, None
        except Exception as e:
            # 非网络层的其他异常(程序错误、JSON 序列化失败等):不动连接池
            _log(f"✗ {method} 请求异常 {url}: {e}", quiet)
            return None, {}, None

        status = resp.status_code
        headers = {k.lower(): v for k, v in resp.headers.items()}

        # 401:token 失效,清缓存重新换一次(独立于 max_retries,最多刷新 1 次)
        if status == 401 and not refreshed_401:
            cached = _token_cache.get(client_id)
            secret = (cached or {}).get("secret")
            if secret:
                _token_cache.pop(client_id, None)
                try:
                    token = get_token(client_id, secret, (cached or {}).get("proxy") or proxy)
                    refreshed_401 = True
                    _log(f"⚠ {method} 401 → token 已刷新,重试 {url}", quiet)
                    continue
                except Exception as e:
                    _log(f"✗ {method} 401 后换 token 失败 {url}: {e}", quiet)
                    # 落回原 401 返回流程

        # 自动重试逻辑(opt-in)
        if attempt < max_retries:
            if status == 429:
                wait = _parse_retry_after(headers)
                _log(f"⚠ {method} 429 限流 {url},{wait:.1f}s 后重试 (第 {attempt+1}/{max_retries} 次)", quiet)
                time.sleep(wait)
                attempt += 1
                continue
            if 500 <= status < 600:
                wait = min(2 ** attempt, 10)
                _log(f"⚠ {method} {status} 服务端错误 {url},{wait}s 后重试 (第 {attempt+1}/{max_retries} 次)", quiet)
                time.sleep(wait)
                attempt += 1
                continue

        # 不重试,解析返回
        data = None
        if 200 <= status < 300:
            # 204 No Content / 空响应体:成功但无数据,不尝试解析 JSON
            if status == 204 or not resp.content:
                pass
            else:
                try:
                    data = resp.json()
                except Exception as e:
                    _log(f"✗ {method} {status} 但 JSON 解析失败 {url}: {e}", quiet)
        else:
            body_snip = resp.text[:200] if method in ("POST", "PUT") else ""
            msg = f"✗ {method} {status} {url}"
            if body_snip:
                msg += f": {body_snip}"
            _log(msg, quiet, level=logging.INFO)

        return status, headers, data


def safe_get_ex(url, token, client_id, proxy, params=None, timeout=30, quiet=False, max_retries=0):
    """
    GET 请求,返回 (status, headers, data)。

    - status: HTTP 状态码 int;网络/超时等异常时为 None
    - headers: dict(lower-case key → str);异常时为 {}
    - data:   解析后的 JSON dict;非 2xx 或解析失败时为 None

    不抛异常,不 raise_for_status,让调用方根据 status 自行决定重试策略
    (比如 429 时可读 headers["x-next-replenishment-time"] 精准 sleep)。

    可选 max_retries:>0 时启用自动 429/5xx 退避重试(默认 0 关闭)。
    """
    return _request_ex("GET", url, token, client_id, proxy,
                       params=params, timeout=timeout, quiet=quiet, max_retries=max_retries)


def safe_post_ex(url, token, client_id, proxy, json_body=None, params=None, timeout=30, quiet=False, max_retries=0):
    """POST 请求,返回 (status, headers, data)。语义同 safe_get_ex。"""
    return _request_ex("POST", url, token, client_id, proxy,
                       json_body=json_body, params=params, timeout=timeout,
                       quiet=quiet, max_retries=max_retries)


def safe_put_ex(url, token, client_id, proxy, json_body=None, params=None, timeout=30, quiet=False, max_retries=0):
    """PUT 请求,返回 (status, headers, data)。用于 PUT /v3/price 和 PUT /v3/inventory。"""
    return _request_ex("PUT", url, token, client_id, proxy,
                       json_body=json_body, params=params, timeout=timeout,
                       quiet=quiet, max_retries=max_retries)


def safe_get(url, token, client_id, proxy, params=None):
    """
    GET 请求,失败返回 None(简化接口)。

    默认重试 WALMART_DEFAULT_RETRIES 次(env,默认 2);想关掉用
    `WALMART_DEFAULT_RETRIES=0` 或直接调 safe_get_ex 自己控制。
    需要状态码/headers 的场景请用 safe_get_ex。
    """
    _, _, data = safe_get_ex(url, token, client_id, proxy,
                             params=params, max_retries=_DEFAULT_RETRIES)
    return data


def safe_post(url, token, client_id, proxy, json_body=None, params=None):
    """
    POST 请求,失败返回 None(简化接口)。

    ⚠ 默认 max_retries=0 — POST 非幂等,自动重试可能导致重复提交(feed 重复 /
    refund 重复 / shipping 重复)。需要重试的场景请显式调 safe_post_ex 并自
    己保证幂等(典型如查询型 POST),或按业务层反查后决定是否重试
    (feed 提交必须走 ops.feed_log 防重,见 CLAUDE.md 安全铁律)。
    需要状态码/headers 的场景请用 safe_post_ex。
    """
    _, _, data = safe_post_ex(url, token, client_id, proxy,
                              json_body=json_body, params=params)
    return data
