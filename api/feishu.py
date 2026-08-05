"""飞书开放平台客户端:多维表格(bitable)读写 + 群机器人 webhook 通知。

多维表格是本系统的人机界面(docs/feishu_tables.md);旧仓库 lark_io 只支持电子表格,
bitable 为全新实现,但重试/退避/切块参数照抄旧系统的实测值(docs/legacy_survey.md):

  - tenant_access_token:线程安全双检缓存,提前 300s 刷新,有效期兜底 7200s
  - 瞬时错误双轨判定:int code {90235, 90217, 50502, 99991400}
    + 小写子串兜底("data not ready" / "too many request" / "timeout")——
    新接口是否暴露 int code 未经全面验证,子串轨不可删
  - 退避 1/2/4/8 秒,最多 4 次
  - token 失效码 99991663/99991664:清缓存换新 token 重试(与瞬时码分开处理)
  - 批量写 ≤500 条/次(整批全成功或全失败),同表串行(锁)+ 批间节流(写 QPS≈10)

错误模型(统一,修掉旧系统 cli/http 两后端不一致的陷阱):
  非瞬时业务错误一律抛 FeishuError(附 code/msg),绝不静默返回错误 envelope。

字段名规则:本层收发的 fields dict 由调用方用 registry 的字段常量构造,
本层不出现任何具体表的字段名。
"""

import logging
import threading
import time
from collections import defaultdict

import httpx

from registry import resources
from registry.resources import Bitable, Spreadsheet

logger = logging.getLogger("api.feishu")

_TRANSIENT_CODES = {90235, 90217, 50502, 99991400}
_TOKEN_INVALID_CODES = {99991663, 99991664}
_TRANSIENT_TEXTS = ("data not ready", "too many request", "timeout")
_BACKOFF = (1, 2, 4, 8)
_MAX_ATTEMPTS = 4
_EARLY_REFRESH_SECS = 300
_BATCH_LIMIT = 500          # 飞书批量增/改/删上限(整批原子)
_WRITE_THROTTLE_SECS = 0.15  # 同表批间节流,压在写 QPS≈10 之下

_token_cache: dict = {}  # {"token": str, "expires_at": float}
_token_lock = threading.Lock()
_table_locks: dict = defaultdict(threading.Lock)  # (app_token, table_id) → 串行写锁
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
        if code in _TOKEN_INVALID_CODES:
            logger.warning("飞书 token 失效(code=%s),刷新后重试 %s", code, path)
            _tenant_token(force_refresh=True)
            continue
        if _is_transient(code, msg) and attempt < _MAX_ATTEMPTS - 1:
            logger.warning("飞书瞬时错误 code=%s msg=%s,%ds 后重试(第 %d/%d 次)%s",
                           code, msg, _BACKOFF[attempt], attempt + 1, _MAX_ATTEMPTS, path)
            time.sleep(_BACKOFF[attempt])
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
                 page_size: int = 500) -> list[dict]:
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
        params = {"page_size": min(page_size, 500)}
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

    自动按 500/批切块;同表串行 + 批间节流。单批失败即抛 FeishuError,
    此时之前的批已写入飞书(飞书无跨批事务)——调用方按返回长度判断写到哪。
    """
    t = table.require()
    ids: list[str] = []
    with _table_locks[(t.app_token, t.table_id)]:
        for i, chunk in enumerate(_chunks(fields_list, _BATCH_LIMIT)):
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
        for i, chunk in enumerate(_chunks(updates, _BATCH_LIMIT)):
            if i:
                time.sleep(_WRITE_THROTTLE_SECS)
            data = _call("POST", _records_path(table, "/batch_update"),
                         json_body={"records": chunk})
            n += len(data.get("records") or [])
            logger.info("飞书 batch_update %s:第 %d 批 %d 条", t.name, i + 1, len(chunk))
    return n


def batch_delete(table: Bitable, record_ids: list[str]) -> int:
    """输入:record_id 列表 → 输出:删除数。仅限展示类表重建;登记类表永不删人写的行。"""
    t = table.require()
    n = 0
    with _table_locks[(t.app_token, t.table_id)]:
        for i, chunk in enumerate(_chunks(record_ids, _BATCH_LIMIT)):
            if i:
                time.sleep(_WRITE_THROTTLE_SECS)
            _call("POST", _records_path(table, "/batch_delete"),
                  json_body={"records": chunk})
            n += len(chunk)
            logger.info("飞书 batch_delete %s:第 %d 批 %d 条", t.name, i + 1, len(chunk))
    return n


# ══════════════════════════════════════════════════════════════════════════════
#  电子表格(sheets)——仅用于行数超 bitable 套餐上限的大表(如在线产品总表)
#  切块/节流参数照抄旧系统实测:4000 行/块(20 列约 4MB,5000 行撞 90227)、块间 0.3s
# ══════════════════════════════════════════════════════════════════════════════

_SHEET_WRITE_BLOCK_ROWS = 4000
_SHEET_WRITE_THROTTLE_SECS = 0.3
_SHEET_DIMENSION_MAX = 5000     # dimension_range 增/删单次上限(90204 实证 2026-08-05)


def _col_letter(n: int) -> str:
    """输入:列数(1-based)→ 输出:列字母(1→A,26→Z,27→AA)。"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def sheet_row_count(sheet: Spreadsheet) -> int:
    """输入:电子表格登记条目 → 输出:网格总行数(grid_properties.row_count)。"""
    s = sheet.require()
    data = _call("GET", f"/open-apis/sheets/v3/spreadsheets/{s.token}/sheets/query")
    for meta in data.get("sheets") or []:
        if meta.get("sheet_id") == s.sheet_id:
            return int((meta.get("grid_properties") or {}).get("row_count") or 0)
    raise FeishuError(None, f"电子表格「{s.name}」中找不到 sheet_id={s.sheet_id}")


def sheet_ensure_rows(sheet: Spreadsheet, need_rows: int) -> int:
    """输入:登记条目 + 需要的总行数 → 输出:本次扩充的行数(网格不足时 add-dimension)。"""
    s = sheet.require()
    current = sheet_row_count(s)
    if current >= need_rows:
        return 0
    add = need_rows - current
    remaining = add
    while remaining > 0:      # 单次最多 5000 行(90204),分块扩
        step = min(remaining, _SHEET_DIMENSION_MAX)
        _call("POST", f"/open-apis/sheets/v2/spreadsheets/{s.token}/dimension_range",
              json_body={"dimension": {"sheetId": s.sheet_id,
                                       "majorDimension": "ROWS", "length": step}})
        remaining -= step
        if remaining > 0:
            time.sleep(_SHEET_WRITE_THROTTLE_SECS)
    logger.info("电子表格「%s」扩行 %d(%d → %d)", s.name, add, current, need_rows)
    return add


def sheet_overwrite(sheet: Spreadsheet, rows: list[list]) -> int:
    """输入:登记条目 + 全部数据行(含表头行)→ 输出:写入行数。整表重写语义。

    分块 values_batch_update;写完后删除网格中多余的尾部行——
    修掉旧系统"本次行数变少时残留旧行"的已知缺陷(plan.md #2 同款问题)。
    """
    s = sheet.require()
    if not rows:
        return 0
    n_cols = max(len(r) for r in rows)
    last_col = _col_letter(n_cols)
    sheet_ensure_rows(s, len(rows))

    written = 0
    for i in range(0, len(rows), _SHEET_WRITE_BLOCK_ROWS):
        block = rows[i:i + _SHEET_WRITE_BLOCK_ROWS]
        rng = f"{s.sheet_id}!A{i + 1}:{last_col}{i + len(block)}"
        _call("POST", f"/open-apis/sheets/v2/spreadsheets/{s.token}/values_batch_update",
              json_body={"valueRanges": [{"range": rng, "values": block}]})
        written += len(block)
        logger.info("电子表格「%s」写入 %d/%d 行", s.name, written, len(rows))
        if i + _SHEET_WRITE_BLOCK_ROWS < len(rows):
            time.sleep(_SHEET_WRITE_THROTTLE_SECS)

    surplus = sheet_row_count(s) - len(rows)
    trimmed = surplus
    while surplus > 0:        # 从尾部分块删,单次 ≤5000(与扩行同限制)
        step = min(surplus, _SHEET_DIMENSION_MAX)
        _call("DELETE", f"/open-apis/sheets/v2/spreadsheets/{s.token}/dimension_range",
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


def notify(text: str) -> bool:
    """输入:通知文本 → 输出:是否真正发出。

    通过群机器人 webhook(registry:FEISHU_WEBHOOK_URL)发文本消息;
    未配置时降级为仅记日志并返回 False。绝不抛异常——通知失败不能拖垮工作流。
    """
    url = resources.feishu_webhook_url()
    if not url:
        logger.info("FEISHU_WEBHOOK_URL 未配置,通知仅记日志:%s", text)
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
