"""影刀(ShadowBot)客户端本地任务接口适配(仅 macOS 生产机;客户端 1.22.x)。

为什么有这个模块(2026-10-31):影刀 1.22.x 删掉了 `shadowbot:Run?robot-uuid=`
协议 —— 客户端照收照记日志,但什么也不做,于是日报那边"指令发出去了、
latest.json 却一直不更新"。官方给的替代是客户端自带的本地 REST 接口
(官方 `shadowbot-cli` 的底层;1.22.20 安装包没带 CLI 可执行文件,所以本仓
直连接口)。**接口没有官方公开文档**,下面的契约全部读自官方公开下载的
1.22.20 安装包代码,两路独立复核,出处与待现场核实项见 docs/plan.md 2026-10-31。

api 层只做"把接口调对":找端口与令牌、鉴权头、超时、信封解析、按传输层
故障分型。**提交成没成、要不要等、算哪一类失败**,判在 services/yingdao。

  health()                        -> {instanceId, pid, port}    免鉴权
  version()                       -> {appVersion, ...}          免鉴权
  auth_current()                  -> {username, gradeName, ...} 未登录 = code 4011
  state()                         -> {mode, loggedIn, task:{...}, ...}
  list_apps(app_type, page)       -> {items:[{appId, ...}], pages, ...}
  submit_task(app_id, app_type)   -> {accepted, taskId}         异步:入队即返回
  get_task(task_id)               -> {status, error, sourceKind, ...}

接线(每一条都来自安装包代码):
  · 端口与令牌在 instance.json(registry.paths.yingdao_instance_file),
    **客户端每次启动都换**,所以每次调用前重读,绝不缓存。
  · 只连 127.0.0.1,请求头 `Authorization: Bearer <token>`(逐字节比对);
    health / version 两个端点免鉴权。⚠ 令牌**不许进日志、不许进异常文本**。
  · ⚠ 绝不能带 `Origin` 头(一律 403),httpx 默认不发,别手动加。
  · ⚠ `trust_env=False`:本机环境变量里的代理一概不走 —— 本地接口经代理
    转一道要么连不上、要么把令牌送到代理那里。
  · 统一信封 {code, message, requestId, timestamp, data}:成功 code=0;
    **业务错误(未登录、模式不对、任务被拒)也是 HTTP 200**,必须看 code。

传输层分型(services 据此判"能不能重提",别合并):
  YingdaoUnreachable → 连接没建立(客户端没起来 / instance.json 缺失或是旧的),
                       请求**肯定没送达**,稍后重试安全
  YingdaoNoResponse  → 连上了但没拿到完整回应(读超时 / 连接中途断),
                       对 POST 来说**结果不明**:任务可能已经建好在跑
  YingdaoApiError    → 拿到了错误信封(HTTP 非 200 或 code≠0),带原文
  YingdaoProtocolError → 发现文件的协议版本或地址不是验证过的那一套
                       (客户端升级换了接口),停下来报,不猜
"""

import json
import logging

import httpx

from registry import paths

logger = logging.getLogger("api.yingdao")

PROTOCOL_VERSION = "1"                       # instance.json 的 protocolVersion
HOST = "127.0.0.1"                           # 服务端只监听这个地址
VERIFIED_VERSIONS = ("1.22.18", "1.22.20")   # 契约读自这两版安装包代码
APP_TYPES = ("subscribed", "developed")      # 能运行的两种;developed-activity 不能跑

# 读超时 30s:服务端把请求转给渲染进程只等 10s(超时回 4091),
# 自身请求超时 65s;连接超时短,连不上就是客户端没起来,不必干等
_TIMEOUT = httpx.Timeout(30.0, connect=3.0)


class YingdaoUnreachable(RuntimeError):
    """连不上本地接口:客户端没起来,或发现文件缺失/是旧文件。请求没送达。"""


class YingdaoNoResponse(RuntimeError):
    """请求可能已送达但没拿到完整回应。对提交任务来说 = 结果不明,不许重提。"""


class YingdaoProtocolError(RuntimeError):
    """发现文件的协议版本或地址与验证过的不一致(客户端升级换了接口)。"""


class YingdaoApiError(RuntimeError):
    """接口回了错误信封。http_status / code / message 原样保留供 services 分类。"""

    def __init__(self, http_status: int, code, message: str):
        super().__init__(f"影刀接口 HTTP {http_status} code={code}:{message}")
        self.http_status = http_status
        self.code = code
        self.message = message or ""


def _instance() -> dict:
    """输入:无 → 输出:当前发现文件内容(含令牌,**只在本模块内用**)。"""
    path = paths.yingdao_instance_file()
    try:
        inst = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise YingdaoUnreachable(
            f"没有发现文件 {path}(客户端没起来,或版本低于 1.22 没有本地接口)"
        ) from None
    except (OSError, ValueError) as e:
        # 客户端是原子写(tmp + rename),读到半截基本不会发生;读不了按没起来处理
        raise YingdaoUnreachable(f"发现文件读不了 {path}:{type(e).__name__}") from None
    if not isinstance(inst, dict):
        raise YingdaoUnreachable(f"发现文件格式不对 {path}")
    if str(inst.get("protocolVersion")) != PROTOCOL_VERSION:
        raise YingdaoProtocolError(
            f"发现文件协议版本 {inst.get('protocolVersion')!r},本仓只验证过 "
            f"{PROTOCOL_VERSION!r} —— 影刀升级换了接口,先跑 yingdao_probe 看清再改")
    if inst.get("hostname") != HOST:
        # 令牌是明文发出去的,地址不是本机就不发
        raise YingdaoProtocolError(f"发现文件地址 {inst.get('hostname')!r} 不是 {HOST}")
    if not isinstance(inst.get("port"), int) or not inst.get("token"):
        raise YingdaoUnreachable(f"发现文件缺端口或令牌 {path}")
    return inst


def _call(inst: dict, method: str, path: str, *, auth: bool = True,
          params: dict | None = None, body: dict | None = None,
          request_id: str | None = None):
    """输入:发现文件 + 方法/路径(+查询/请求体)→ 输出:信封里的 data。"""
    headers = {}
    if auth:
        headers["Authorization"] = f"Bearer {inst['token']}"
    if request_id:
        headers["X-Request-Id"] = request_id[:128]      # 原样回填到 requestId,便于对日志
    url = f"http://{HOST}:{inst['port']}{path}"
    try:
        with httpx.Client(trust_env=False, timeout=_TIMEOUT) as c:
            # json= 会带上 Content-Type: application/json(POST 必须有,否则 415)
            r = c.request(method, url, params=params, json=body, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise YingdaoUnreachable(f"连不上本地接口 {HOST}:{inst['port']}"
                                 f"({type(e).__name__})") from None
    except httpx.TransportError as e:
        raise YingdaoNoResponse(f"{method} {path} 没拿到完整回应"
                                f"({type(e).__name__})") from None
    try:
        env = r.json()
    except ValueError:
        raise YingdaoApiError(r.status_code, None, "响应不是 JSON(端口上不是影刀?)") from None
    if not isinstance(env, dict):
        raise YingdaoApiError(r.status_code, None, "响应不是信封格式")
    if r.status_code != 200 or env.get("code") != 0:
        raise YingdaoApiError(r.status_code, env.get("code"), str(env.get("message") or ""))
    return env.get("data")


def health() -> dict:
    """输入:无 → 输出:{instanceId, pid, port}(免鉴权;核对发现文件是不是活的那份)。

    客户端崩溃 / kill -9 / 自己 app.exit 后发现文件会残留(只有正常退出才删),
    端口上可能已是别的东西 —— 5000 是它优先选的端口,macOS 的隔空播放接收器
    也在 5000 上。两种都按"连不上"报,调用方才会去拉起客户端:
      · 回的不是信封(code 为空:HTML、空体、别家 JSON)= 端口上不是影刀。
        真影刀的 health 由主进程直接回、免鉴权、不经渲染进程,永远是信封;
      · 是信封但 instanceId 与文件对不上 = 别的影刀实例,文件是旧的。
    """
    inst = _instance()
    try:
        data = _call(inst, "GET", "/api/v1/system/health", auth=False) or {}
    except YingdaoApiError as e:
        if e.code is None:
            raise YingdaoUnreachable(f"端口 {inst['port']} 上不是影刀(HTTP {e.http_status},"
                                     "发现文件是旧的)") from None
        raise
    if not isinstance(data, dict) or data.get("instanceId") != inst.get("instanceId"):
        raise YingdaoUnreachable("发现文件与正在监听的实例对不上(旧文件残留)")
    return {"instanceId": data.get("instanceId"), "pid": data.get("pid"),
            "port": inst["port"]}


def version() -> dict:
    """输入:无 → 输出:{appVersion, compatibleCli}(免鉴权)。"""
    return _call(_instance(), "GET", "/api/v1/system/version", auth=False) or {}


def auth_current() -> dict:
    """输入:无 → 输出:当前登录账号 {userId, username, grade, gradeName, ...}。

    未登录抛 YingdaoApiError(code 4011)。
    """
    return _call(_instance(), "GET", "/api/v1/auth/current") or {}


def state() -> dict:
    """输入:无 → 输出:{mode, loggedIn, task:{running, queueLength, queueState}, ...}。

    ⚠ 渲染进程没回应(还在启动 / 10 秒超时)时**照样 code 0**,返回一份
    占位结果:多出 `studioWindowState` 键、队列假报空闲。怎么认在 services。
    """
    return _call(_instance(), "GET", "/api/v1/system/state") or {}


def list_apps(app_type: str, page: int = 1, page_size: int = 200) -> dict:
    """输入:应用类型 + 页码 → 输出:{page, pages, total, items:[{appId, ...}]}。"""
    return _call(_instance(), "GET", "/api/v1/apps",
                 params={"appType": app_type, "page": page,
                         "pageSize": page_size}) or {}


def submit_task(app_id: str, app_type: str, request_id: str | None = None) -> dict:
    """输入:应用 UUID + 类型(+请求号)→ 输出:{accepted, taskId}。

    **异步**:入队就返回,不等跑完;跟踪用 get_task。不传 inputs —— 本仓
    的机器人读 input.json,不吃接口入参。
    ⚠ 抛 YingdaoNoResponse 时任务可能已建好在跑,调用方**不许重提**。
    """
    return _call(_instance(), "POST", "/api/v1/tasks",
                 body={"appId": app_id, "appType": app_type},
                 request_id=request_id) or {}


def get_task(task_id: str) -> dict:
    """输入:任务号 → 输出:{taskId, appId, status, sourceKind, error, ...}。

    status ∈ create / running / completed / failed / canceled(后三个是终态)。
    客户端中途重启后,正在跑的那个任务会查不到(code 4041)。
    """
    return _call(_instance(), "GET", f"/api/v1/tasks/{task_id}") or {}
