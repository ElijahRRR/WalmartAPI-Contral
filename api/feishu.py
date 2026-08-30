"""飞书开放平台客户端:多维表格(bitable)读写 + 群机器人 webhook 通知。

多维表格是本系统的人机界面(docs/feishu_tables.md);旧仓库 lark_io 只支持电子表格,
bitable 为全新实现,但重试/退避参数照抄旧系统的实测值(docs/legacy_survey.md);
切块参数 2026-08-27 起改由下面的「限额登记表」按官方限制 ×95% 出:

  - tenant_access_token:线程安全双检缓存,提前 300s 刷新,有效期兜底 7200s
  - 瞬时错误双轨判定:int code {90235, 90217, 50502, 99991400}
    + 小写子串兜底("data not ready" / "too many request" / "timeout")——
    新接口是否暴露 int code 未经全面验证,子串轨不可删
  - 退避 1/2/4/8 秒,最多 4 次;限流(99991400/90217)优先按官方
    `x-ogw-ratelimit-reset` 响应头精确等待,无头才退回阶梯
  - token 失效码 99991663/99991664:清缓存换新 token 重试(与瞬时码分开处理)
  - 批量写按下面「限额登记表」切块(整批全成功或全失败),同表串行(锁)+ 批间节流

错误模型(统一,修掉旧系统 cli/http 两后端不一致的陷阱):
  非瞬时业务错误一律抛 FeishuError(附 code/msg),绝不静默返回错误 envelope。

限额规矩(所有者 2026-08-27 定稿):所有限额常量集中在本文件顶部的「限额登记表」,
取值 = 官方限制 × 95%;读写各只有一条通道(读 sheet_values_rows /
写 sheet_write_ranges),别的写法一律不新开。

字段名规则:本层收发的 fields dict 由调用方用 registry 的字段常量构造,
本层不出现任何具体表的字段名。
"""

import json
import logging
import re
import threading
import time
from collections import defaultdict

import httpx

from registry import resources
from registry.resources import Bitable, Spreadsheet

logger = logging.getLogger("api.feishu")

# ══════════════════════════════════════════════════════════════════════════════
#  限额登记表(所有者 2026-08-27 定稿)——**全部**限额常量只在这里出生
#
#  三条规矩,新增常量照抄:
#   ① 本仓取值 = 官方限制 × 95%,向下取整(官方 5000 行 → 本仓 4750 行)。
#      官方另给了更严的自荐值时取更严者(单元格:硬上限 50000×95%=47500,
#      但官方自荐 40000 → 取 40000)。
#   ② 每条常量行内注释三件套:**官方**原值 | 官方 URL | 核对日期 2026-08-27。
#   ③ 官方没给数字的用工程预算值,并明写「工程值,非官方」——不许假装有出处。
#
#  官方原句逐条对照(含「官方未说明」项)存档在 refdata/feishu_limits.tsv,
#  与本表出自同一次调研(2026-08-27)、同一批 URL,改一处就同步另一处。
#
#  ⚠ 已知的**唯一例外**:list_fields 的 page_size=100 仍是就地字面量 ——
#  2026-08-27 那轮调研只覆盖了「电子表格读/写、频控与配额、多维表格记录」四组,
#  没查「列出字段」端点,写不出官方出处就不进表(规矩③:不许假装有出处)。
#  该处已就地注明,补到官方原句后按规矩迁进来。
# ══════════════════════════════════════════════════════════════════════════════

# ── 电子表格 v2 写(values / values_batch_update / values_append)─────────────
_SHEET_WRITE_MAX_ROWS = 4750          # 官方 5000 行 | https://open.feishu.cn/document/server-docs/docs/sheets-v3/data-operation/write-data-to-multiple-ranges?lang=zh-CN | 核对 2026-08-27
#     官方原句「单次写入数据不得超过 5000 行、100列。」四个 values 写接口口径一致。
_SHEET_WRITE_MAX_COLS = 95            # 官方 100 列 | https://open.feishu.cn/document/server-docs/docs/sheets-v3/data-operation/write-data-to-multiple-ranges?lang=zh-CN | 核对 2026-08-27
#     同一句原文的另一半;5000×100 是「与」不是「或」。列超限是**结构错误**:
#     分批只切得出行、切不出列,所以直接抛,不假装还能救。
_SHEET_CELL_HARD_MAX_CHARS = 40000    # 官方硬上限 50000、官方自荐 40000,取严 | https://open.feishu.cn/document/server-docs/docs/sheets-v3/data-operation/write-data-to-multiple-ranges?lang=zh-CN | 核对 2026-08-27
#     官方原句「每个单元格不超过 50,000 字符，由于服务端会增加控制字符，因此推荐
#     每个单元格不超过 40,000 字符。」(sheets-faq 页另写 45,000,同站两页互斥,取小)
#     ⚠ 与业务层 _SHEET_CELL_MAX_CHARS(20000 截断)**两层分工**,见那条注释。
_SHEET_WRITE_BYTE_BUDGET = 9_000_000  # 工程值,非官方(官方写侧无字节上限,只有 90227 码)| https://open.feishu.cn/document/server-docs/docs/sheets-v3/data-operation/write-data-to-multiple-ranges?lang=zh-CN | 核对 2026-08-27
#     调研结论逐字:「【请求体字节上限 10MB】官方未说明……超量时只会拿到 90227
#     TooLargeRequest，官方不给阈值。」网上流传的写侧 10MB 是把**读**侧的
#     「该接口返回数据的最大限制为 10 MB。」张冠李戴。取读侧 10MB 的 ~86% 作预算,
#     余量补偿 json.dumps 估算与服务端控制字符的误差。
_SHEET_RANGES_PER_REQUEST = 100       # 工程值,非官方(官方未说明 valueRanges 数组长度上限)| https://open.feishu.cn/document/server-docs/docs/sheets-v3/data-operation/write-data-to-multiple-ranges?lang=zh-CN | 核对 2026-08-27
#     调研结论逐字:「【values_batch_update 的 range 段数(valueRanges 数组长度)
#     上限】官方未说明。」沿用现行 100(生产久经),不因为查不到就放开。
_SHEET_DIMENSION_MAX = 4750           # 官方 5000 行/列 | https://open.feishu.cn/document/server-docs/docs/sheets-v3/sheet-rowcol/add-rows-or-columns?lang=zh-CN | 核对 2026-08-27
#     官方原句「单次调用该接口，最多支持增加 5000 行或列。」删行同额:
#     「单次调用该接口，最多支持删除 5000 行或列。」(.../-delete-rows-or-columns)
#     ⚠ 90204 实证 2026-08-05:超量时飞书报的是 90204,不是超限码,别照码去猜阈值。
_SHEET_WRITE_THROTTLE_SECS = 0.3      # 官方 100 次/秒 + 「单个文档只能串行调用」 | https://open.feishu.cn/document/server-docs/docs/sheets-v3/overview?lang=zh-CN | 核对 2026-08-27
#     官方原句「向多个范围写入数据 | 单租户单应用100次/秒；单个文档只能串行调用」——
#     后半句就是 _sheet_locks 那把同表串行锁的官方背书(并发不是慢,是被明令禁止)。
#     0.3s 照抄旧系统实测值,远低于官方 100 次/秒,不动。

# ── 电子表格 v2 读 ──────────────────────────────────────────────────────────
_SHEET_READ_BLOCK_ROWS = 4750         # 工程值,非官方(官方读侧无行数限制)| https://open.feishu.cn/document/server-docs/docs/sheets-v3/data-operation/reading-a-single-range?lang=zh-CN | 核对 2026-08-27
#     官方读侧唯一硬数字是原句「该接口返回数据的最大限制为 10 MB。」,按行分块 +
#     90221 对半兜底管住它;块粒度与写侧对称取 4750,不另立数字。

# ── 多维表格 v1(bitable)────────────────────────────────────────────────────
_BITABLE_BATCH_CREATE_MAX = 950       # 官方 1,000 条/次 | https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/batch_create?lang=zh-CN | 核对 2026-08-27
#     官方原句「在多维表格数据表中新增多条记录，单次调用最多新增 1,000 条记录。」
_BITABLE_BATCH_UPDATE_MAX = 950       # 官方 1,000 条/次 | https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/batch_update?lang=zh-CN | 核对 2026-08-27
#     官方原句「更新数据表中的多条记录，单次调用最多更新 1,000 条记录。」
_BITABLE_BATCH_DELETE_MAX = 475       # 官方 500 条/次(**与增/改的 1000 不同**)| https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/batch_delete?lang=zh-CN | 核对 2026-08-27
#     官方原句「- 单次调用中最多删除 500 条记录。」删比写严一半,别照 create 外推。
_BITABLE_PAGE_SIZE = 475              # 官方 500 行/页 | https://open.feishu.cn/document/docs/bitable-v1/app-table-record/search?lang=zh-CN | 核对 2026-08-27
#     官方原句「该接口用于查询数据表中的现有记录，单次最多查询 500 行记录，支持分页获取。」
_WRITE_THROTTLE_SECS = 0.15           # 官方 50 次/秒 + 「同一个数据表(table) 不支持并发调用写接口」 | https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/batch_create?lang=zh-CN | 核对 2026-08-27
#     官方原句(错误码 1254291)「同一个数据表(table) 不支持并发调用写接口」——
#     _table_locks 那把同表串行锁的官方背书。0.15s 照抄旧系统实测(写 QPS≈10),不动。

# ── 频控与配额(全域)────────────────────────────────────────────────────────
_RATELIMIT_RESET_HEADER = "x-ogw-ratelimit-reset"  # 官方指定的退避依据头 | https://open.feishu.cn/document/server-docs/api-call-guide/frequency-control?lang=zh-CN | 核对 2026-08-27
#     官方原句「x-ogw-ratelimit-reset: 52 //恢复 limit 周期，单位：秒」,以及
#     「使用该响应头延迟请求是解除限频的最好方法。1. 等待 x-ogw-ratelimit-reset
#     中指定的秒数。2. 重试请求。」⚠ 官方**没有** Retry-After,按那个头写读不到值。
#     ⚠ 旧版 OpenAPI(本仓 sheets/v2 就是)限流时 HTTP 码是 400 不是 429,
#     判据只能认 code=99991400,不能认状态码。
_RATELIMIT_RESET_CAP_SECS = 60        # 工程值,非官方(官方未说明该头的取值范围/上界)| https://open.feishu.cn/document/server-docs/api-call-guide/frequency-control?lang=zh-CN | 核对 2026-08-27
_QUOTA_EXHAUSTED_CODE = 99991403      # 官方:月度 API 调用量耗尽,**不是频控** | https://open.feishu.cn/document/server-docs/api-call-guide/generic-error-code?lang=zh-CN | 核对 2026-08-27
#     官方原句「99991403 | This month's API call quota has been exceeded |
#     本月 API 调用次数已达上限，请联系企业管理员升级飞书版本。」配额按自然月 1 号
#     刷新,退避多久都不会好——所以它**不进**可重试集合,见 _QUOTA_EXHAUSTED_HINT。

_TRANSIENT_CODES = {90235, 90217, 50502, 99991400}
_RATELIMIT_CODES = {99991400, 90217}   # 频控码:优先读 reset 头精确等待
_TOKEN_INVALID_CODES = {99991663, 99991664}
_TRANSIENT_TEXTS = ("data not ready", "too many request", "timeout")
_BACKOFF = (1, 2, 4, 8)
_MAX_ATTEMPTS = 4
_EARLY_REFRESH_SECS = 300
_QUOTA_EXHAUSTED_HINT = "月度 API 配额耗尽,不可重试,升级版本或等下月 1 号"

_token_cache: dict = {}  # {"token": str, "expires_at": float}
_token_lock = threading.Lock()
_table_locks: dict = defaultdict(threading.Lock)  # (app_token, table_id) → 串行写锁
#: 电子表格 token → 串行写锁(与 _table_locks 同一条理由,晚一步补上)。
#:
#: 为什么需要:各 sheet_* 写函数内部都靠 `time.sleep(_SHEET_WRITE_THROTTLE_SECS)`
#: 压 QPS,而那个节流**只在一次调用内部生效**。跨店线程池(STORE_WORKERS=24)
#: 让 24 个线程各自进入写函数时,每个线程都以为自己是唯一写者,节流被整体绕过,
#: 飞书一侧看到的是 24 倍瞬时写入 → 90227/限流。
#: 用 RLock 是因为 sheet_overwrite 内部还会调 sheet_ensure_rows(同一把锁)。
#:
#: ⚠⚠ **不能写成 `defaultdict(threading.RLock)`**(2026-08-17 实验反证过)。
#: `threading.RLock` 是 **Python 函数**(`threading.py` 里那个挑 _CRLock/_PyRLock
#: 的包装),所以 `defaultdict.__missing__` 调它时会执行 Python 字节码 ⇒
#: eval breaker 可能触发 ⇒ 线程切换 ⇒ 两个线程各造一把 RLock,只有一把落进
#: 字典,**落空的那个线程拿着一把没人认的锁**。实测 64 线程同时首次取同一个
#: key,拿到 2 个不同的锁对象。而这正是本锁存在的那个场景(24 个店铺线程同时
#: 第一次写同一张表),故障表现恰好就是它要防的那件事:一阵 90227 限流。
#: `dict.setdefault` 是 C 实现、原子,多造出来的 RLock 直接被 GC —— 实测同一
#: key 恒返回同一把。
#: (对照:`_table_locks` 用的 `threading.Lock` 是 `_thread.allocate_lock`,
#:  **C 工厂**,不执行 Python 字节码,所以那处 defaultdict 是安全的,别一起改。)
_sheet_locks: dict = {}


def _sheet_lock(token: str) -> threading.RLock:
    """输入:电子表格 token → 输出:该表的串行写锁(同 token 恒为同一把)。"""
    return _sheet_locks.setdefault(token, threading.RLock())


_client: httpx.Client | None = None
_client_lock = threading.Lock()


class FeishuError(Exception):
    """飞书业务错误(code != 0 且非瞬时)。属性:code, msg。"""

    def __init__(self, code: int | None, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"feishu code={code}: {msg}")


def _http() -> httpx.Client:
    """进程内单例 httpx.Client;测试替换 feishu._client。

    trust_env=False 必须保留:飞书调用不走任何代理——既不走沃尔玛店铺代理,
    也不准被 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 环境变量劫持(生产 Mac 挂着
    Clash,旧系统 2026-05-07 事故即本机代理拒连导致 14,610 个单元格写回失败)。
    """
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.Client(trust_env=False,
                                   timeout=httpx.Timeout(30.0, connect=15.0))
        return _client


def _is_transient(code: int | None, msg: str) -> bool:
    """双轨判定:int code 命中,或错误文本(小写)命中子串。两条轨都必须保留。"""
    if code in _TRANSIENT_CODES:
        return True
    low = (msg or "").lower()
    return any(t in low for t in _TRANSIENT_TEXTS)


def _check_quota(code: int | None, msg: str) -> None:
    """输入:envelope 的 code/msg → 输出:无(命中月度配额耗尽即抛,不返回)。

    99991403 **不是频控**,是自然月调用量配额打满(官方:「本月 API 调用次数已达
    上限，请联系企业管理员升级飞书版本。」),下月 1 号才刷新——退避、换 token、
    重试都不会好,继续重试只是把剩下的额度也烧掉。所以在两条重试轨(_is_transient
    的 int 轨与子串轨)**之前**先手,明说不可重试。
    """
    if code == _QUOTA_EXHAUSTED_CODE:
        raise FeishuError(code, f"{msg}({_QUOTA_EXHAUSTED_HINT})")


def _ratelimit_wait(code: int | None, resp, attempt: int) -> float:
    """输入:错误码 + 响应 + 第几次重试 → 输出:该等的秒数。

    官方给的解法是读 `x-ogw-ratelimit-reset`(单位:秒)按它等,原句
    「使用该响应头延迟请求是解除限频的最好方法」;头缺失/不是正整数时(官方
    未承诺一定带,也未说明取值范围)退回本仓现行 1/2/4/8 阶梯,不新开第二套。
    上限 _RATELIMIT_RESET_CAP_SECS 是工程值:等一分钟还不如让本轮失败、
    交给调度下一轮,不能让一个工作流吊死在一个头上。
    """
    ladder = _BACKOFF[attempt]
    if code not in _RATELIMIT_CODES:
        return ladder
    try:
        secs = int(str((resp.headers or {}).get(_RATELIMIT_RESET_HEADER, "")).strip())
    except (TypeError, ValueError):
        return ladder
    if secs <= 0:
        return ladder
    wait = min(secs, _RATELIMIT_RESET_CAP_SECS)
    logger.warning("飞书限流 code=%s,按官方 %s=%ds 等待 %ds(第 %d/%d 次)",
                   code, _RATELIMIT_RESET_HEADER, secs, wait,
                   attempt + 1, _MAX_ATTEMPTS)
    return wait


def _tenant_token(force_refresh: bool = False) -> str:
    """输入:force_refresh → 输出:有效 tenant_access_token(缓存,提前 300s 刷新)。"""
    if not force_refresh:
        cached = _token_cache.get("t")
        if cached and time.time() < cached["expires_at"]:
            return cached["token"]
    with _token_lock:
        cached = _token_cache.get("t")
        if not force_refresh and cached and time.time() < cached["expires_at"]:
            return cached["token"]
        resp = _http().post(
            f"{resources.feishu_base_url()}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": resources.feishu_app_id(),
                  "app_secret": resources.feishu_app_secret()},
        )
        resp.raise_for_status()
        env = resp.json()
        if env.get("code") != 0:
            raise FeishuError(env.get("code"), env.get("msg", "tenant_access_token failed"))
        expire = int(env.get("expire", 7200))
        _token_cache["t"] = {
            "token": env["tenant_access_token"],
            "expires_at": time.time() + expire - _EARLY_REFRESH_SECS,
        }
        return _token_cache["t"]["token"]


def _call(method: str, path: str, *, json_body=None, params=None, timeout=60) -> dict:
    """输入:HTTP 方法 + open-apis 路径 → 输出:envelope 的 data 字段(dict)。

    自动:带 tenant token、瞬时错误退避重试(1/2/4/8s,最多 4 次)、
    token 失效换新重试。非瞬时业务错误抛 FeishuError,网络异常按瞬时处理。
    """
    url = f"{resources.feishu_base_url()}{path}"
    last_err: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = _http().request(
                method, url,
                headers={"Authorization": f"Bearer {_tenant_token()}"},
                json=json_body, params=params, timeout=timeout,
            )
            env = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            last_err = e
            logger.warning("飞书 %s %s 网络/解析异常(第 %d/%d 次): %s",
                           method, path, attempt + 1, _MAX_ATTEMPTS, e)
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF[attempt])
                continue
            raise FeishuError(None, f"network error after {_MAX_ATTEMPTS} attempts: {e}") from e

        code = env.get("code")
        if code == 0:
            return env.get("data") or {}
        msg = env.get("msg", "")
        _check_quota(code, msg)
        if code in _TOKEN_INVALID_CODES:
            logger.warning("飞书 token 失效(code=%s),刷新后重试 %s", code, path)
            _tenant_token(force_refresh=True)
            continue
        if _is_transient(code, msg) and attempt < _MAX_ATTEMPTS - 1:
            wait = _ratelimit_wait(code, resp, attempt)
            logger.warning("飞书瞬时错误 code=%s msg=%s,%gs 后重试(第 %d/%d 次)%s",
                           code, msg, wait, attempt + 1, _MAX_ATTEMPTS, path)
            time.sleep(wait)
            continue
        raise FeishuError(code, msg)
    raise FeishuError(None, f"exhausted retries: {last_err}")


# ══════════════════════════════════════════════════════════════════════════════
#  多维表格
# ══════════════════════════════════════════════════════════════════════════════


def _records_path(table: Bitable, op: str) -> str:
    t = table.require()
    return f"/open-apis/bitable/v1/apps/{t.app_token}/tables/{t.table_id}/records{op}"


def list_records(table: Bitable, *, filter_: dict | None = None,
                 field_names: list[str] | None = None,
                 page_size: int = _BITABLE_PAGE_SIZE) -> list[dict]:
    """输入:Bitable(+可选服务端过滤条件/字段裁剪)→ 输出:记录列表 [{record_id, fields}]。

    走 records/search,服务端过滤 + 自动翻页;不要整表拉回自己扫。
    filter_ 形如 {"conjunction":"and","conditions":[{"field_name":...,"operator":"is","value":[...]}]},
    field_name 用 registry 字段常量构造。
    """
    body: dict = {}
    if filter_:
        body["filter"] = filter_
    if field_names:
        body["field_names"] = field_names
    out: list[dict] = []
    page_token = None
    while True:
        params = {"page_size": min(page_size, _BITABLE_PAGE_SIZE)}
        if page_token:
            params["page_token"] = page_token
        data = _call("POST", _records_path(table, "/search"), json_body=body, params=params)
        for item in data.get("items") or []:
            out.append({"record_id": item.get("record_id"), "fields": item.get("fields") or {}})
        if not data.get("has_more"):
            return out
        page_token = data.get("page_token")


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def batch_create(table: Bitable, fields_list: list[dict]) -> list[str]:
    """输入:fields dict 列表 → 输出:新建 record_id 列表(顺序对应)。

    自动按登记表 _BITABLE_BATCH_CREATE_MAX 切块;同表串行 + 批间节流。
    单批失败即抛 FeishuError,此时之前的批已写入飞书(飞书无跨批事务)——
    调用方按返回长度判断写到哪。
    """
    t = table.require()
    ids: list[str] = []
    with _table_locks[(t.app_token, t.table_id)]:
        for i, chunk in enumerate(_chunks(fields_list, _BITABLE_BATCH_CREATE_MAX)):
            if i:
                time.sleep(_WRITE_THROTTLE_SECS)
            data = _call("POST", _records_path(table, "/batch_create"),
                         json_body={"records": [{"fields": f} for f in chunk]})
            ids.extend(r.get("record_id") for r in data.get("records") or [])
            logger.info("飞书 batch_create %s:第 %d 批 %d 条", t.name, i + 1, len(chunk))
    return ids


def batch_update(table: Bitable, updates: list[dict]) -> int:
    """输入:[{record_id, fields}] 列表 → 输出:成功更新的记录数。

    按 record_id 更新(多维表格的"同步"语义靠去重键字段对齐后走这里)。
    切块/串行/节流/失败语义同 batch_create。
    """
    t = table.require()
    n = 0
    with _table_locks[(t.app_token, t.table_id)]:
        for i, chunk in enumerate(_chunks(updates, _BITABLE_BATCH_UPDATE_MAX)):
            if i:
                time.sleep(_WRITE_THROTTLE_SECS)
            data = _call("POST", _records_path(table, "/batch_update"),
                         json_body={"records": chunk})
            n += len(data.get("records") or [])
            logger.info("飞书 batch_update %s:第 %d 批 %d 条", t.name, i + 1, len(chunk))
    return n


# ⚠ 本函数当前**无仓内调用方**,但它是 HTTP 原语(与 batch_create/batch_update
# 同级),不是策略——2026-08-14 死代码盘点时唯一的调用方 sync_by_key 被删,
# 它本身保留:哪天要删行时不必重写一遍分批/节流/错误模型。
# 真要删行前先想清楚:订单中心六表的纪律是**任何表都不删行**(主订单表是
# 永久枢纽,行间有关联字段,删行断链),见 workflows/order_center_push.py 头注。
def batch_delete(table: Bitable, record_ids: list[str]) -> int:
    """输入:record_id 列表 → 输出:删除数。仅限展示类表重建;登记类表永不删人写的行。

    ⚠ 切块用 _BITABLE_BATCH_DELETE_MAX(官方删是 500 条/次,只有增/改的一半),
    别照 batch_create 外推。
    """
    t = table.require()
    n = 0
    with _table_locks[(t.app_token, t.table_id)]:
        for i, chunk in enumerate(_chunks(record_ids, _BITABLE_BATCH_DELETE_MAX)):
            if i:
                time.sleep(_WRITE_THROTTLE_SECS)
            _call("POST", _records_path(table, "/batch_delete"),
                  json_body={"records": chunk})
            n += len(chunk)
            logger.info("飞书 batch_delete %s:第 %d 批 %d 条", t.name, i + 1, len(chunk))
    return n


def list_fields(table: Bitable) -> list[dict]:
    """输入:Bitable → 输出:字段元数据列表 [{field_name, type, ui_type}](自动翻页)。

    type 为飞书字段类型码(1文本/2数字/3单选/4多选/5日期/7复选框/15超链接…),
    调用方据此做写入值的类型适配。
    """
    t = table.require()
    out: list[dict] = []
    page_token = None
    while True:
        # ⚠ 未进顶部「限额登记表」的唯一一处限额字面量:2026-08-27 那轮官方调研
        # 没覆盖「列出字段」端点(refdata/feishu_limits.tsv 里没有这一行),
        # 写不出官方原值/URL 就不硬塞进登记表冒充有出处。沿用现行 100 不动;
        # 下一轮补到官方原句后,按登记表规矩迁进去并删掉这段注释。
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        data = _call("GET", f"/open-apis/bitable/v1/apps/{t.app_token}"
                            f"/tables/{t.table_id}/fields", params=params)
        for item in data.get("items") or []:
            out.append({"field_name": item.get("field_name"),
                        "type": item.get("type"),
                        "ui_type": item.get("ui_type")})
        if not data.get("has_more"):
            return out
        page_token = data.get("page_token")


def create_field(table: Bitable, field_name: str, ftype: int = 1) -> None:
    """输入:表 + 字段名 + 类型码(默认文本)→ 输出:无。

    调用方自行确保字段不存在(重名会被飞书拒绝)。用于程序自建辅助列
    (如同步指纹),业务列一律由用户在 UI 建。
    """
    t = table.require()
    _call("POST", f"/open-apis/bitable/v1/apps/{t.app_token}"
                  f"/tables/{t.table_id}/fields",
          json_body={"field_name": field_name, "type": ftype})
    logger.info("表「%s」新建字段「%s」(type=%d)", t.name, field_name, ftype)


def _plain_text(v) -> str:
    """输入:records/search 返回的字段值 → 输出:纯文本(文本字段可能是分段结构)。"""
    if isinstance(v, list):
        return "".join(str(seg.get("text", "")) if isinstance(seg, dict) else str(seg)
                       for seg in v)
    if v is None:
        return ""
    return str(v)


def _row_hash(fields: dict, hash_field: str) -> str:
    """输入:一行载荷 → 输出:内容指纹(排除指纹列自身,键序无关)。"""
    import hashlib
    payload = json.dumps({k: v for k, v in fields.items() if k != hash_field},
                         ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]



def update_by_key(table: Bitable, key_field: str,
                  desired: dict[str, dict]) -> tuple[int, list[str]]:
    """输入:表 + 键字段名 + {键: fields dict} → 输出:(更新行数, 表中不存在的键)。

    **只更新,不新建、不删除**。用于"给别人已建好的行补几列"的场景
    (order_audit 往销售订单表写审核列):建行是 order_center_push 的职责,
    这里若也建行会造出只有审核列、没有订单本体的半截行。
    键不在表里不是错误——调用方通常在下一轮(等对方建完行)自然补上,
    返回缺键清单供调用方计数与告警。

    只覆盖 fields 里给出的列(省略的列保留飞书旧值),人工列绝不会被碰。
    """
    existing: dict[str, str] = {}
    for rec in list_records(table, field_names=[key_field]):
        k = _plain_text(rec["fields"].get(key_field)).strip()
        if k and k not in existing:
            existing[k] = rec["record_id"]
    updates = [{"record_id": existing[k], "fields": f}
               for k, f in desired.items() if k in existing]
    missing = sorted(k for k in desired if k not in existing)
    if updates:
        batch_update(table, updates)
    logger.info("表「%s」定向更新:%d 行%s", table.name, len(updates),
                f",{len(missing)} 个键不在表中(待建行方补齐)" if missing else "")
    return len(updates), missing



def _call_multipart(path: str, *, data: dict, files: dict, timeout=120) -> dict:
    """输入:open-apis 路径 + 表单字段 + 文件 → 输出:envelope 的 data(dict)。

    _call 的 multipart 孪生(上传接口不收 JSON):同一套 token 注入、瞬时退避、
    token 失效换新。files 的值形如 (文件名, bytes, MIME)。
    """
    url = f"{resources.feishu_base_url()}{path}"
    last_err: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = _http().post(
                url, headers={"Authorization": f"Bearer {_tenant_token()}"},
                data=data, files=files, timeout=timeout,
            )
            env = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            last_err = e
            logger.warning("飞书上传 %s 网络/解析异常(第 %d/%d 次): %s",
                           path, attempt + 1, _MAX_ATTEMPTS, e)
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF[attempt])
                continue
            raise FeishuError(None, f"upload network error: {e}") from e
        code = env.get("code")
        if code == 0:
            return env.get("data") or {}
        msg = env.get("msg", "")
        _check_quota(code, msg)            # 月度配额与 _call 同一条判据,不另开一套
        if code in _TOKEN_INVALID_CODES:
            _tenant_token(force_refresh=True)
            continue
        if _is_transient(code, msg) and attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_ratelimit_wait(code, resp, attempt))
            continue
        raise FeishuError(code, msg)
    raise FeishuError(None, f"upload exhausted retries: {last_err}")


def upload_media(table: Bitable, file_name: str, content: bytes,
                 *, mime: str = "image/jpeg") -> str:
    """输入:目标多维表格 + 文件名 + 字节内容 → 输出:file_token(填附件字段用)。

    附件字段的值形如 [{"file_token": "..."}];file_token 与 app_token 绑定
    (parent_type=bitable_image),不能跨表复用。上传本身不幂等——同一张图
    上传两次得两个 token,调用方须自行防重(order_audit 用内容哈希查 ops.dedupe)。
    """
    t = table.require()
    data = _call_multipart(
        "/open-apis/drive/v1/medias/upload_all",
        data={"file_name": file_name, "parent_type": "bitable_image",
              "parent_node": t.app_token, "size": str(len(content))},
        files={"file": (file_name, content, mime)},
    )
    token = data.get("file_token")
    if not token:
        raise FeishuError(None, f"上传「{file_name}」未返回 file_token")
    return token


# ══════════════════════════════════════════════════════════════════════════════
#  电子表格(sheets)——仅用于行数超 bitable 套餐上限的大表(如在线产品总表)
#  限额常量全在顶部「限额登记表」;这里只留**业务语义**的那一条(见下)。
#  旧系统实测的 4000 行/块(20 列约 4MB,5000 行撞 90227)已被官方 95% 红线
#  (_SHEET_WRITE_MAX_ROWS=4750 + _SHEET_WRITE_BYTE_BUDGET 字节预算)取代:
#  行与字节两条预算任一先到即封批,不再靠一个凑出来的行数替字节数背锅。
# ══════════════════════════════════════════════════════════════════════════════

#: 业务层脏数据闸,**不是限额**(限额是 _SHEET_CELL_HARD_MAX_CHARS=40000)。
#: 两层分工别混:
#:   · 这一条(20000,超了**截断 + 告警**):采集来的标题/描述超两万字必是脏数据,
#:     截掉照写,不因为一行脏数据把整轮写入炸掉;
#:   · _SHEET_CELL_HARD_MAX_CHARS(40000,超了**直接抛**):通道硬闸,对着官方
#:     自荐值定;它的岗位在**不清洗的 sheet_overwrite 路径**——那里超长本会被
#:     飞书 90222/90227 整批拒,本地先抛是净收益。
#: 顺序(总控裁决 2026-08-27):清洗路径 sheet_write_ranges **先截断后硬闸**——
#: 脏数据截断+告警、轮次照走是既有能力,不许被硬闸炸掉;硬闸在该路上对字符长度
#: 天然不触发,只剩列数闸。全文口径同 _scrub/_check_shape 两处 docstring 与
#: docs/conventions.md §八。
_SHEET_CELL_MAX_CHARS = 20000


def _col_letter(n: int) -> str:
    """输入:列数(1-based)→ 输出:列字母(1→A,26→Z,27→AA)。"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


_wiki_token_cache: dict[str, str] = {}


def _sheet_token(s: Spreadsheet) -> str:
    """输入:登记条目 → 输出:真实 spreadsheet_token。

    wiki 承载的表格(feishu.cn/wiki/ 链接)登记的是知识库节点 token,
    须经 wiki API 解析成 obj_token 才能调 sheets 接口;进程内缓存。
    """
    if not getattr(s, "wiki", False):
        return s.token
    if s.token not in _wiki_token_cache:
        data = _call("GET", "/open-apis/wiki/v2/spaces/get_node",
                     params={"token": s.token})
        obj = ((data.get("node") or {}).get("obj_token")) or ""
        if not obj:
            raise FeishuError(None, f"wiki 节点解析失败(「{s.name}」token={s.token}):"
                                    f"确认链接是知识库表格且应用有权限")
        _wiki_token_cache[s.token] = obj
    return _wiki_token_cache[s.token]


def sheet_list(sheet: Spreadsheet) -> list[tuple[str, str]]:
    """输入:电子表格登记条目(用其 token)→ 输出:[(sheet_id, title)] 全部子表。

    枚举 workbook 内所有 sheet(kpi_history_import 用它发现每店历史页;
    店铺 sheet 的 sheet_id 运行时才可知,不进 registry)。
    """
    s = sheet  # 不 require():只需 token,sheet_id 允许为空
    if not s.token:
        raise LookupError(f"电子表格「{s.name}」尚未登记 token")
    data = _call("GET", f"/open-apis/sheets/v3/spreadsheets/{_sheet_token(s)}/sheets/query")
    return [(m.get("sheet_id") or "", m.get("title") or "")
            for m in data.get("sheets") or []]


def sheet_row_count(sheet: Spreadsheet) -> int:
    """输入:电子表格登记条目 → 输出:网格总行数(grid_properties.row_count)。"""
    s = sheet.require()
    data = _call("GET", f"/open-apis/sheets/v3/spreadsheets/{_sheet_token(s)}/sheets/query")
    for meta in data.get("sheets") or []:
        if meta.get("sheet_id") == s.sheet_id:
            return int((meta.get("grid_properties") or {}).get("row_count") or 0)
    raise FeishuError(None, f"电子表格「{s.name}」中找不到 sheet_id={s.sheet_id}")


def sheet_ensure_rows(sheet: Spreadsheet, need_rows: int) -> int:
    """输入:登记条目 + 需要的总行数 → 输出:本次扩充的行数(网格不足时 add-dimension)。"""
    s = sheet.require()
    with _sheet_lock(s.token):
        current = sheet_row_count(s)
        if current >= need_rows:
            return 0
        add = need_rows - current
        remaining = add
        while remaining > 0:      # 单次上限见登记表 _SHEET_DIMENSION_MAX,分块扩
            step = min(remaining, _SHEET_DIMENSION_MAX)
            _call("POST", f"/open-apis/sheets/v2/spreadsheets/{_sheet_token(s)}/dimension_range",
                  json_body={"dimension": {"sheetId": s.sheet_id,
                                           "majorDimension": "ROWS", "length": step}})
            remaining -= step
            if remaining > 0:
                time.sleep(_SHEET_WRITE_THROTTLE_SECS)
        logger.info("电子表格「%s」扩行 %d(%d → %d)", s.name, add, current, need_rows)
        return add


def _values_raw(sheet: Spreadsheet, a1_range: str) -> list[list]:
    """输入:登记条目 + A1 范围(如 'A2:G500',不带 sheet 前缀)→ 输出:值矩阵。

    **私有裸调用**:一次 GET values/:range,没有任何分块与兜底。只准两个人用——
    标准读通道 sheet_values_rows(它负责分块与 90221 对半)与薄壳
    sheet_values_small(已知小范围)。外面直接用它 = 绕开唯一读通道。

    单元格统一 ToString 渲染(公式取结果,数字转文本),调用方拿到的都是 str/None。
    ⚠ 单次读取响应体官方上限 10MB(原句「该接口返回数据的最大限制为 10 MB。」,
    超出返 90221 data exceeded);行列数官方不设限,所以大范围只能按行分块。
    """
    s = sheet.require()
    data = _call("GET",
                 f"/open-apis/sheets/v2/spreadsheets/{_sheet_token(s)}/values/"
                 f"{s.sheet_id}!{a1_range}",
                 params={"valueRenderOption": "ToString"})
    return ((data.get("valueRange") or {}).get("values")) or []


def sheet_values_small(sheet: Spreadsheet, a1_range: str) -> list[list]:
    """输入:登记条目 + **已知小范围** A1(表头/单行/固定几行)→ 输出:值矩阵。

    ⚠ **无界范围禁用**:范围随表长增长的读取(A2:U{总行数} 这类)一律走
    sheet_values_rows —— 它按行分块 + 90221 对半兜底,是本仓唯一的大范围读通道。
    这里是薄壳,不分块、不兜底:给 `A1:K1`、`A2:F2` 这种一眼看得出上界的场景用,
    省掉一次没必要的分块循环。范围写成字面量,别拼变量上界。
    """
    return _values_raw(sheet, a1_range)


def sheet_values_rows(sheet: Spreadsheet, first_col: str, last_col: str,
                      row_from: int, row_to: int, *,
                      block_rows: int = _SHEET_READ_BLOCK_ROWS
                      ) -> list[tuple[int, list]]:
    """输入:列区间 + 行区间 → 输出:[(行号, 该行值列表)](行方向分块拼接)。

    **唯一标准读通道**:范围上界会随表长增长的读取一律走这里,不要自己拼
    A2:U{总行数} 去调裸读。块粒度取登记表 _SHEET_READ_BLOCK_ROWS(4750,与写侧
    对称;读侧官方无行数限制,10MB 响应上限靠分块 + 90221 兜底管)。

    2026-08-19 生产实证:上架表 21 列全量一把读,表长大后撞官方单响应
    10MB 上限(90221 data exceeded)。读取本身没有行列数限制,所以按行
    分块;单块仍超(块内长文本堆积)则**对半再切**,每次触发都记日志
    (兜底静默常态化 = 主路径已坏没人知道)。

    行号按块首偏移计算:飞书只裁掉**范围尾部**的空行(中段空行仍占位),
    块内 enumerate + 块首行号恒对得上,调用方拿到的 rownum 可直接回写。
    """
    out: list[tuple[int, list]] = []

    def _read(rf: int, rt: int) -> None:
        try:
            vals = _values_raw(sheet, f"{first_col}{rf}:{last_col}{rt}")
        except FeishuError as e:
            if e.code == 90221 and rt > rf:
                mid = (rf + rt) // 2
                logger.warning("读取 %s%d:%s%d 超 10MB(90221),对半重读",
                               first_col, rf, last_col, rt)
                _read(rf, mid)
                _read(mid + 1, rt)
                return
            raise
        for i, row in enumerate(vals):
            out.append((rf + i, row))

    start = row_from
    while start <= row_to:
        _read(start, min(start + block_rows - 1, row_to))
        start += block_rows
    return out


_CTRL_CHARS = {c: None for c in range(32) if c not in (9, 10, 13)}


def _scrub(v):
    """输入:单元格值 → 输出:飞书能收的字符串(控制字符剔除,超长截断)。

    采集来的标题/描述偶尔带 \\x00 一类控制字符,飞书直接返 90202
    validate RangeVal fail——报错里不会告诉你是哪个单元格。这里剔掉,
    **剔到了就记日志**(静默清洗 = 下次同样的坑没人知道)。

    ⚠ 这里的 20000 是**业务脏数据闸**,不是限额;通道硬闸
    (_SHEET_CELL_HARD_MAX_CHARS=40000)在 _check_shape 里。清洗路径
    (sheet_write_ranges)**先截断后硬闸**(总控裁决 2026-08-27:脏数据
    截断+告警、轮次照走是既有能力,不许被硬闸炸掉),故硬闸在该路上对
    字符长度天然不触发,只剩列数闸;40000 硬闸的真正岗位在不清洗的
    sheet_overwrite 路径。
    """
    s = "" if v is None else str(v)
    if len(s) > _SHEET_CELL_MAX_CHARS:
        logger.warning("单元格超 %d 字符已截断(原长 %d)",
                       _SHEET_CELL_MAX_CHARS, len(s))
        s = s[:_SHEET_CELL_MAX_CHARS]
    if any(ord(c) < 32 and c not in "\t\n\r" for c in s):
        logger.warning("单元格含控制字符,已剔除:%r", s[:80])
        s = s.translate(_CTRL_CHARS)
    return s


def _check_shape(rng: str, values: list[list]) -> None:
    """输入:A1 范围 + 值矩阵 → 输出:无(结构超限即抛 ValueError)。

    两条**分批救不了**的官方硬限,写通道入口一次性拦掉:
      · 列数 > _SHEET_WRITE_MAX_COLS(官方 100 列 ×95%):切批只切得出行,
        列宽是调用方给的形状,只能当成调用方的 bug 抛回去;
      · 单元格 > _SHEET_CELL_HARD_MAX_CHARS(官方自荐 40000):超到这个量级的
        「单元格」根本不是表格数据,截断只会把 bug 藏进飞书。
    抛 ValueError 而不是 FeishuError:错在本仓调用方,不在飞书。
    """
    width = max((len(r) for r in values), default=0)
    if width > _SHEET_WRITE_MAX_COLS:
        raise ValueError(
            f"写飞书范围 {rng} 有 {width} 列,超官方单次写入列上限"
            f"(95% 红线 {_SHEET_WRITE_MAX_COLS} 列):列超限分批救不了,请裁列")
    for i, row in enumerate(values):
        for j, cell in enumerate(row):
            n = len(cell) if isinstance(cell, str) else len("" if cell is None
                                                            else str(cell))
            if n > _SHEET_CELL_HARD_MAX_CHARS:
                raise ValueError(
                    f"写飞书范围 {rng} 第 {i + 1} 行第 {j + 1} 列单元格 {n} 字符,"
                    f"超通道硬闸 {_SHEET_CELL_HARD_MAX_CHARS}(官方自荐上限):"
                    f"这不是表格数据,上游先修")


def _est_bytes(rng: str, values: list[list]) -> int:
    """输入:一段 (A1范围, 值矩阵) → 输出:它进请求体的估算字节数。

    按 UTF-8 实际字节算(ensure_ascii=False 后 encode),不是字符数:中文一字
    三字节,按字符数算会低估三倍,而低估正是 90227 那一侧。估的是单段,批的
    预算 = 各段之和 + 外层 {"valueRanges":[…]} 的壳(壳几十字节,忽略不计)。
    """
    return len(json.dumps({"range": rng, "values": values},
                          ensure_ascii=False, default=str).encode("utf-8"))


def _subrange(rng: str, offset: int, count: int) -> str | None:
    """输入:A1 范围 + 行偏移 + 行数 → 输出:子范围(形状看不懂返 None)。"""
    m = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", rng.strip().upper())
    if not m or count <= 0:
        return None
    c1, r1, c2 = m.group(1), int(m.group(2)), m.group(3)
    return f"{c1}{r1 + offset}:{c2}{r1 + offset + count - 1}"


def _split_rows(rng: str, values: list[list]) -> list[tuple[str, list[list]]]:
    """输入:A1 范围 + 值矩阵 → 输出:行/字节两条预算都不超的子范围列表。

    单个范围裹着上千行会被飞书拒(90202);sheet_overwrite 早就按块切,
    这条定点回写路径此前漏了(所有者 2026-08-09 实遇:1000+ 行维护流水
    一次写,整轮 failed)。范围形如 A11:I1037,只切行不动列。

    两条预算**任一先到即封段**:
      · 行数 _SHEET_WRITE_MAX_ROWS(官方 5000 行 ×95%);
      · 估算字节 _SHEET_WRITE_BYTE_BUDGET(工程值,官方写侧无字节上限)。
    字节这条必须在**切段**这一步就管,不能只在批间累加时管:粘段之后一整段
    可能就是几万行,批间累加时它是一个不可分的整体,再判也切不动了
    (2026-08-18 audit_sheet 那次就是长文本行把单请求撑爆的)。
    单行自身就超字节预算时它独占一段——切不动就如实发出去,不假装能救。
    ⚠ 范围形状看不懂就原样放过(不猜),与 _coalesce 同一条纪律。
    """
    m = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", rng.strip().upper())
    if not m:
        return [(rng, values)]
    c1, r1, c2 = m.group(1), int(m.group(2)), m.group(3)
    overhead = _est_bytes(rng, [])
    cuts: list[tuple[int, int]] = []          # [(起, 止+1)]
    start = n_rows = 0
    n_bytes = overhead
    for i, row in enumerate(values):
        size = len(json.dumps(row, ensure_ascii=False,
                              default=str).encode("utf-8")) + 1
        if n_rows and (n_rows + 1 > _SHEET_WRITE_MAX_ROWS
                       or n_bytes + size > _SHEET_WRITE_BYTE_BUDGET):
            cuts.append((start, i))
            start, n_rows, n_bytes = i, 0, overhead
        n_rows += 1
        n_bytes += size
    cuts.append((start, len(values)))
    if len(cuts) == 1:
        # 一刀没切:**原样返回调用方给的 rng**。它可能声明得比数据宽
        # (官方允许「range 所指定的范围需要大于等于写入的数据所占用的范围」),
        # 那是调用方的形状,没切就不改——同 _coalesce 的「看不懂就不猜」。
        return [(rng, values)]
    return [(f"{c1}{r1 + s}:{c2}{r1 + e - 1}", values[s:e]) for s, e in cuts]


def _coalesce(updates: list[tuple[str, list[list]]]
              ) -> list[tuple[str, list[list]]]:
    """输入:[(A1范围, 值矩阵)] → 输出:相邻同列的合成一段(**保持原序**)。

    定点回写的调用方几乎都是"一行一个 range"(`C{r}:G{r}`),而整表重写走的是
    "一大段"。同一个接口,两条路径差了三个数量级:
      · 一行一 range → 100 行/请求 + 0.3s 节流 ⇒ 28000 行要 280 个请求、~2 分钟
      · 合成段     → 一段占满行预算 ⇒ 同样 28000 行只要个位数请求
    这就是所有者 2026-08-16 问的「写飞书的速度怎么各处都不一样,有的 4000
    有的几十甚至逐行」—— 差别不在接口,在调用方给的形状。**在这里补齐**:
    调用方照旧一行一个 range,api 层负责把连号的粘起来(分批是 api 层职责)。
    ⚠ 粘完的段**不能**再 100 段合一个请求:2026-08-18 audit_sheet 回填
    28,498 行 × C:G,8 段(当时块大小 4000)进同一请求被飞书 90227
    (request too large)整批拒。每请求的行/字节双预算见 sheet_write_ranges。

    只合并**紧邻的下一行**且列区间完全相同的:不排序、不去重、不跨空行,
    所以"同一行被写两次"的先后覆盖语义与逐行写时逐字一致。
    """
    out: list[tuple[str, list[list]]] = []
    prev: tuple[str, int, int, str] | None = None   # (首列, 起行, 末行, 末列)
    for rng, vals in updates:
        m = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", rng.strip().upper())
        if not m or len(vals) != int(m.group(4)) - int(m.group(2)) + 1:
            out.append((rng, vals))       # 形状看不懂就原样放过,不猜
            prev = None
            continue
        c1, r1, c2, r2 = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
        if prev and prev[0] == c1 and prev[3] == c2 and prev[2] + 1 == r1:
            out[-1] = (f"{c1}{prev[1]}:{c2}{r2}", out[-1][1] + vals)
            prev = (c1, prev[1], r2, c2)
        else:
            out.append((f"{c1}{r1}:{c2}{r2}", list(vals)))
            prev = (c1, r1, r2, c2)
    return out


_oversize_count = [0]


def _oversize_retries(inc: int = 0) -> int:
    """输入:增量 → 输出:90227 对半兜底的累计触发次数(进程内计数)。

    §六 要件二「触发必须记日志计数」:光记日志不计数,查的时候分不清是偶发
    一次还是每轮都在兜(兜底静默常态化 = 主路径已坏没人知道)。
    """
    _oversize_count[0] += inc
    return _oversize_count[0]


def _post_batch(s: Spreadsheet, chunk: list[tuple[str, list[list]]]) -> None:
    """输入:登记条目 + 一批 [(A1范围, 值矩阵)] → 输出:无(发一次 values_batch_update)。

    只管发,不管切也不管报错措辞:切批预算与 90227 兜底都在 _sheet_put,
    报错补范围也在那儿(一处出口,免得两边各补一遍还补得不一样)。
    """
    _call("POST",
          f"/open-apis/sheets/v2/spreadsheets/{_sheet_token(s)}/values_batch_update",
          json_body={"valueRanges": [
              {"range": f"{s.sheet_id}!{rng}", "values": vals}
              for rng, vals in chunk]})


def _with_range(e: FeishuError, chunk: list[tuple[str, list[list]]]) -> FeishuError:
    """输入:飞书错误 + 出错的那一批 → 输出:补上范围的同码错误。

    飞书只说 validate RangeVal fail / request too large,不说是哪一块;
    几万行回填时不带范围等于没报错。
    """
    return FeishuError(e.code, f"{e}(范围 {chunk[0][0]}~{chunk[-1][0]},"
                               f"{sum(len(v) for _r, v in chunk)} 行)")


def _halve_batch(chunk: list[tuple[str, list[list]]]
                 ) -> tuple[list, list] | None:
    """输入:一批 [(A1范围, 值矩阵)] → 输出:行数大致对半的两批(切不动返 None)。

    按**行**对半,不是按段对半:一批可能就是一个几千行的大段,按段切等于没切。
    段内切要重算 A1 子范围,形状看不懂(_subrange 返 None)就判切不动。
    """
    total = sum(len(v) for _r, v in chunk)
    if total < 2:
        return None
    half = total // 2
    first: list[tuple[str, list[list]]] = []
    second: list[tuple[str, list[list]]] = []
    acc = 0
    for rng, vals in chunk:
        if acc >= half:
            second.append((rng, vals))
            continue
        room = half - acc
        if len(vals) <= room:
            first.append((rng, vals))
            acc += len(vals)
            continue
        a, b = _subrange(rng, 0, room), _subrange(rng, room, len(vals) - room)
        if not a or not b:
            return None
        first.append((a, vals[:room]))
        second.append((b, vals[room:]))
        acc += room
    if not first or not second:
        return None
    return first, second


def _sheet_put(s: Spreadsheet, segments: list[tuple[str, list[list]]], *,
               total_rows: int | None = None) -> int:
    """输入:登记条目 + 已切好行的段列表 → 输出:写入行数。**唯一写通道的底座**。

    调用方须已持 _sheet_lock(s.token)——官方原句「单个文档只能串行调用」。

    预算切批(所有者 2026-08-27 定稿):逐段累加,批内**总行数**满
    _SHEET_WRITE_MAX_ROWS 或**估算字节**满 _SHEET_WRITE_BYTE_BUDGET 或段数满
    _SHEET_RANGES_PER_REQUEST,任一先到即**封批提交**,剩余继续循环——
    「当轮写完不留下一轮」(所有者原话):本次调用把 segments 全部写完才返回,
    不把余量攒到下一轮。行与字节两条一起管,是因为官方只给了行列数,字节数
    官方未说明、只有 90227 反馈;单靠行数会被长文本行整批打穿
    (2026-08-18 audit_sheet 就是这么被拒的)。

    90227 兜底(§六 三要件:同函数内 / 触发记日志计数 / 条件明确非 catch-all):
    预算算错时对该批**对半重切一次**再发,并 logger.warning 计数;两半里再有
    一半失败即抛。它是最后一根保险丝,不是主防线——主防线是上面的预算。
    """
    n = 0
    cur: list[tuple[str, list[list]]] = []
    cur_rows = 0
    cur_bytes = 0

    def _flush() -> None:
        nonlocal n, cur, cur_rows, cur_bytes
        if not cur:
            return
        try:
            _post_batch(s, cur)
        except FeishuError as e:
            halves = _halve_batch(cur) if e.code == 90227 else None
            if halves is None:
                raise _with_range(e, cur) from None
            logger.warning("写「%s」%d 行 %d 字节仍被 90227 拒(预算失算第 %d 次),"
                           "对半重切一次再发", s.name, cur_rows, cur_bytes,
                           _oversize_retries(1))
            for half in halves:
                try:
                    _post_batch(s, half)   # 再失败即抛:兜底只兜一层
                except FeishuError as again:
                    raise _with_range(again, half) from None
        n += cur_rows
        if total_rows is not None:
            logger.info("电子表格「%s」写入 %d/%d 行", s.name, n, total_rows)
        cur, cur_rows, cur_bytes = [], 0, 0

    for rng, vals in segments:
        size = _est_bytes(rng, vals)
        if cur and (len(cur) >= _SHEET_RANGES_PER_REQUEST
                    or cur_rows + len(vals) > _SHEET_WRITE_MAX_ROWS
                    or cur_bytes + size > _SHEET_WRITE_BYTE_BUDGET):
            _flush()
            time.sleep(_SHEET_WRITE_THROTTLE_SECS)
        cur.append((rng, vals))
        cur_rows += len(vals)
        cur_bytes += size
    _flush()
    return n


def sheet_write_ranges(sheet: Spreadsheet, updates: list[tuple[str, list[list]]]) -> int:
    """输入:登记条目 + [(A1范围, 值矩阵)] → 输出:**写入的行数**。

    **唯一写通道**:定点回写(如逐行写 E{r}:G{r} 三列)与整表重写
    (sheet_overwrite)最后都落到这套预算切批,别的写法一律不新开。
    四步:结构硬闸(列/单元格,分批救不了的先抛)→ **连号的先粘成段**
    (否则一行一个请求位,几万行要跑几分钟)→ 按行切开过大的段 →
    行/字节/段数三条预算切批提交(见 _sheet_put),批间节流 + 同表串行锁。
    逐行小段的老路径不受影响:100 个一行段合计 100 行,远在行预算之内。

    ⚠ 返回的是行数不是范围数。粘段之前两者恰好相等(调用方全是一行一 range),
    所有调用方也都当行数在用(「回填 N 行」);粘段之后必须显式数行,
    否则那些摘要会一夜之间从「回填 200 行」变成「回填 1 行」。
    """
    s = sheet.require()
    split: list[tuple[str, list[list]]] = []
    for rng, vals in _coalesce(updates):
        # 顺序是刻意的:**先 _scrub 后硬闸**(总控裁决 2026-08-27)。清洗路径的
        # 既有能力是「超长脏数据截断+告警,轮次照走」——硬闸放在截断前,一条
        # 4 万字符的脏报错就能炸掉整轮回写(缺失既有能力,违反红线)。截断后
        # 硬闸只剩查列数>95 的结构错误;40000 字符硬闸的真正岗位在不清洗的
        # sheet_overwrite(那里超长会被飞书 90222/90227 整批拒,本地先抛=净收益)
        scrubbed = [[_scrub(c) for c in row] for row in vals]
        _check_shape(rng, scrubbed)
        split += _split_rows(rng, scrubbed)
    with _sheet_lock(s.token):
        return _sheet_put(s, split)


def sheet_overwrite(sheet: Spreadsheet, rows: list[list]) -> int:
    """输入:登记条目 + 全部数据行(含表头行)→ 输出:写入行数。整表重写语义。

    走与 sheet_write_ranges 同一套预算切批(_sheet_put);写完后删除网格中多余的
    尾部行——修掉旧系统"本次行数变少时残留旧行"的已知缺陷(plan.md #2 同款问题)。

    ⚠ 这条路**不 _scrub**:KPI 看板靠写入日期序列值(数字)配 sheet_set_formatter
    才显示成日期,scrub 会把数字变成字符串,格式化当场失效。脏数据清洗是采集侧
    (定点回写)的职责,整表重写的行是本仓自己从库里拼的。
    """
    s = sheet.require()
    if not rows:
        return 0
    n_cols = max(len(r) for r in rows)
    last_col = _col_letter(n_cols)
    rng = f"A1:{last_col}{len(rows)}"
    _check_shape(rng, rows)
    # 锁把「扩行 → 整表写 → 删尾部残留」三步裹成一段:中间插进来一个别的写者,
    # 删尾部那一步会按**自己**算的 len(rows) 去删,把人家刚写的行删掉。
    with _sheet_lock(s.token):
        sheet_ensure_rows(s, len(rows))

        written = _sheet_put(s, _split_rows(rng, rows), total_rows=len(rows))

        surplus = sheet_row_count(s) - len(rows)
        trimmed = surplus
        while surplus > 0:        # 从尾部分块删,单次 ≤_SHEET_DIMENSION_MAX(与扩行同限制)
            step = min(surplus, _SHEET_DIMENSION_MAX)
            _call("DELETE", f"/open-apis/sheets/v2/spreadsheets/{_sheet_token(s)}/dimension_range",
                  json_body={"dimension": {"sheetId": s.sheet_id, "majorDimension": "ROWS",
                                           "startIndex": len(rows) + surplus - step + 1,
                                           "endIndex": len(rows) + surplus}})
            surplus -= step
            if surplus > 0:
                time.sleep(_SHEET_WRITE_THROTTLE_SECS)
        if trimmed > 0:
            logger.info("电子表格「%s」删除尾部残留 %d 行", s.name, trimmed)
    return written


# ══════════════════════════════════════════════════════════════════════════════
#  通知
# ══════════════════════════════════════════════════════════════════════════════


def sheet_set_formatter(sheet: Spreadsheet, items: list[tuple[str, str]]) -> int:
    """输入:登记条目 + [(A1范围, formatter)](如 ('A2:A500','yyyy/MM/dd'))→ 输出:范围数。

    设置单元格数字/日期显示格式(styles_batch_update)。日期列须配合写入
    日期序列值(1899-12-30 起算天数)才会显示为日期。

    ⚠ 这条**不走值写通道的预算切批**:它发的是样式不是值,官方限额也是另一套
    (单次 5000 行×100 列;带边框时单次单元格数还骤降到 30,000)。本仓的调用方
    只传个位数个范围,远在任何一档之下,故不另立切批;官方原句存档见
    refdata/feishu_limits.tsv「单次设置样式范围上限 / 设置边框样式时…」两行。
    真要批量刷样式了再按那两行的数字 ×95% 加进登记表,别拿值写的行预算凑合。
    """
    s = sheet.require()
    if not items:
        return 0
    with _sheet_lock(s.token):
        _call("PUT",
              f"/open-apis/sheets/v2/spreadsheets/{_sheet_token(s)}/styles_batch_update",
              json_body={"data": [
                  {"ranges": [f"{s.sheet_id}!{rng}"], "style": {"formatter": fmt}}
                  for rng, fmt in items]})
    return len(items)


_MOBILE_RE = re.compile(r"^\d{11}$")
_open_id_cache: dict[str, str] = {}      # 手机号/邮箱 → open_id(进程内)


def _receive_type(who: str) -> str:
    """输入:收件人标识 → 输出:飞书 receive_id_type。纯函数,可测。

    前缀判型逐字沿用旧系统(legacy_survey:1818,notify.py:137):
    `ou_` → open_id、`oc_` → chat_id。另补邮箱与手机号两种人好记的写法。
    """
    w = str(who or "").strip()
    if w.startswith("ou_"):
        return "open_id"
    if w.startswith("oc_"):
        return "chat_id"
    if w.startswith("on_"):
        return "union_id"
    if "@" in w:
        return "email"
    if _MOBILE_RE.match(w):
        return "mobile"          # ⚠ 飞书**没有**这一档,要先换 open_id
    return "user_id"


def resolve_open_id(who: str) -> str | None:
    """输入:手机号或邮箱 → 输出:open_id(查不到返 None)。进程内缓存。

    ⚠ **手机号不能直接当 receive_id** —— 飞书 `im/v1/messages` 的
    receive_id_type 只有 open_id / user_id / union_id / email / chat_id 五档,
    没有"手机号"。所以先走通讯录换 ID。

    这一步要应用有 **`contact:user.id:readonly`** 权限。没有权限时飞书返
    99991672 之类的错,本函数只告警返 None —— 调用方会退到 webhook 或只记日志,
    不会因为"通知发不出去"把工作流拖垮。
    """
    key = str(who or "").strip()
    if not key:
        return None
    if key in _open_id_cache:
        return _open_id_cache[key]
    field = "mobiles" if _MOBILE_RE.match(key) else "emails"
    try:
        data = _call("POST", "/open-apis/contact/v3/users/batch_get_id",
                     params={"user_id_type": "open_id"}, json_body={field: [key]})
    except Exception as e:                                      # noqa: BLE001
        logger.warning("手机号/邮箱换 open_id 失败(应用是否有 "
                       "contact:user.id:readonly 权限?):%s", e)
        return None
    for u in (data.get("user_list") or []):
        oid = str(u.get("user_id") or u.get("open_id") or "").strip()
        if oid:
            _open_id_cache[key] = oid
            return oid
    logger.warning("通讯录里查不到 %s 对应的用户(号码是否为飞书账号?)", key)
    return None


def _notify_via_app(text: str) -> bool:
    """输入:通知文本 → 输出:是否发出。用**应用身份**直接发给人或群。

    旧系统一直是这么发的(legacy_survey:649/1818:`lark-cli im +messages-send
    --as bot`,收件人 open_id 硬编码在 summary.py:38)——本仓改回同一条路,
    群机器人 webhook 退为备用。
    """
    who = resources.feishu_notify_to()
    if not who:
        return False
    rtype = _receive_type(who)
    if rtype == "mobile":
        who = resolve_open_id(who)
        if not who:
            return False
        rtype = "open_id"
    # ⚠ content 必须是**字符串化的 JSON**,不是嵌套对象 ——
    # 传对象飞书直接拒(这是这个接口最常见的错法)
    _call("POST", "/open-apis/im/v1/messages",
          params={"receive_id_type": rtype},
          json_body={"receive_id": who, "msg_type": "text",
                     "content": json.dumps({"text": text}, ensure_ascii=False)},
          timeout=15)
    return True


def notify(text: str) -> bool:
    """输入:通知文本 → 输出:是否真正发出。**绝不抛异常**(通知失败不能拖垮工作流)。

    三条路依次退,任何一条成了就返回 True:
      ① 应用身份直发(FEISHU_NOTIFY_TO,支持 open_id/chat_id/邮箱/手机号)
      ② 群机器人 webhook(FEISHU_WEBHOOK_URL)
      ③ 都没配 → 只记日志,返回 False

    留两条而不是一刀切换,是为了切换期不把通知打断:应用权限还没批下来时
    webhook 照样能发,反过来也一样。
    """
    try:
        if _notify_via_app(text):
            return True
    except Exception as e:                                      # noqa: BLE001
        logger.warning("飞书应用通知失败(退到 webhook):%s", e)

    url = resources.feishu_webhook_url()
    if not url:
        # ⚠ 只记**首行**。整段抄一遍的话,cli 传进来的是完整摘要,而它此刻
        # 已经打到终端了 —— 再吐一份就是同一屏文字出现两次(2026-08-16 所有者
        # 反馈"所有的命令都是这样子的")。全文进不进日志由 cli 决定,
        # 通知这一层只负责说"没发出去"。
        logger.info("FEISHU_NOTIFY_TO 与 FEISHU_WEBHOOK_URL 均未配置,"
                    "通知未发出:%s",
                    text.strip().splitlines()[0] if text.strip() else "(空)")
        return False
    try:
        resp = _http().post(url, json={"msg_type": "text", "content": {"text": text}}, timeout=15)
        env = resp.json()
        ok = env.get("code", env.get("StatusCode", -1)) == 0
        if not ok:
            logger.warning("飞书 webhook 通知被拒:%s", env)
        return ok
    except Exception as e:
        logger.warning("飞书 webhook 通知失败:%s", e)
        return False
