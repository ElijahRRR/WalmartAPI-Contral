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


# ⚠ 本函数当前**无仓内调用方**,但它是 HTTP 原语(与 batch_create/batch_update
# 同级),不是策略——2026-08-14 死代码盘点时唯一的调用方 sync_by_key 被删,
# 它本身保留:哪天要删行时不必重写一遍分批/节流/错误模型。
# 真要删行前先想清楚:订单中心六表的纪律是**任何表都不删行**(主订单表是
# 永久枢纽,行间有关联字段,删行断链),见 workflows/order_center_push.py 头注。
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


def list_fields(table: Bitable) -> list[dict]:
    """输入:Bitable → 输出:字段元数据列表 [{field_name, type, ui_type}](自动翻页)。

    type 为飞书字段类型码(1文本/2数字/3单选/4多选/5日期/7复选框/15超链接…),
    调用方据此做写入值的类型适配。
    """
    t = table.require()
    out: list[dict] = []
    page_token = None
    while True:
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
    import json as _json
    payload = _json.dumps({k: v for k, v in fields.items() if k != hash_field},
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
        if code in _TOKEN_INVALID_CODES:
            _tenant_token(force_refresh=True)
            continue
        if _is_transient(code, msg) and attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_BACKOFF[attempt])
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
#  切块/节流参数照抄旧系统实测:4000 行/块(20 列约 4MB,5000 行撞 90227)、块间 0.3s
# ══════════════════════════════════════════════════════════════════════════════

_SHEET_WRITE_BLOCK_ROWS = 4000
_SHEET_CELL_MAX_CHARS = 20000   # 飞书单元格上限 5 万,留余量(超长必是脏数据)
_SHEET_WRITE_THROTTLE_SECS = 0.3
_SHEET_DIMENSION_MAX = 5000     # dimension_range 增/删单次上限(90204 实证 2026-08-05)


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
    current = sheet_row_count(s)
    if current >= need_rows:
        return 0
    add = need_rows - current
    remaining = add
    while remaining > 0:      # 单次最多 5000 行(90204),分块扩
        step = min(remaining, _SHEET_DIMENSION_MAX)
        _call("POST", f"/open-apis/sheets/v2/spreadsheets/{_sheet_token(s)}/dimension_range",
              json_body={"dimension": {"sheetId": s.sheet_id,
                                       "majorDimension": "ROWS", "length": step}})
        remaining -= step
        if remaining > 0:
            time.sleep(_SHEET_WRITE_THROTTLE_SECS)
    logger.info("电子表格「%s」扩行 %d(%d → %d)", s.name, add, current, need_rows)
    return add


def sheet_values(sheet: Spreadsheet, a1_range: str) -> list[list]:
    """输入:登记条目 + A1 范围(如 'A2:G500',不带 sheet 前缀)→ 输出:值矩阵。

    单元格统一 ToString 渲染(公式取结果,数字转文本),调用方拿到的都是 str/None。
    """
    s = sheet.require()
    data = _call("GET",
                 f"/open-apis/sheets/v2/spreadsheets/{_sheet_token(s)}/values/"
                 f"{s.sheet_id}!{a1_range}",
                 params={"valueRenderOption": "ToString"})
    return ((data.get("valueRange") or {}).get("values")) or []


_CTRL_CHARS = {c: None for c in range(32) if c not in (9, 10, 13)}


def _scrub(v):
    """输入:单元格值 → 输出:飞书能收的字符串(控制字符剔除,超长截断)。

    采集来的标题/描述偶尔带 \\x00 一类控制字符,飞书直接返 90202
    validate RangeVal fail——报错里不会告诉你是哪个单元格。这里剔掉,
    **剔到了就记日志**(静默清洗 = 下次同样的坑没人知道)。
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


def _split_rows(rng: str, values: list[list]) -> list[tuple[str, list[list]]]:
    """输入:A1 范围 + 值矩阵 → 输出:按 _SHEET_WRITE_BLOCK_ROWS 切开的子范围。

    单个范围裹着上千行会被飞书拒(90202);sheet_overwrite 早就按块切,
    这条定点回写路径此前漏了(所有者 2026-08-09 实遇:1000+ 行维护流水
    一次写,整轮 failed)。范围形如 A11:I1037,只切行不动列。
    """
    m = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", rng.strip().upper())
    if not m or len(values) <= _SHEET_WRITE_BLOCK_ROWS:
        return [(rng, values)]
    c1, r1, c2, _r2 = m.group(1), int(m.group(2)), m.group(3), m.group(4)
    out = []
    for i in range(0, len(values), _SHEET_WRITE_BLOCK_ROWS):
        block = values[i:i + _SHEET_WRITE_BLOCK_ROWS]
        out.append((f"{c1}{r1 + i}:{c2}{r1 + i + len(block) - 1}", block))
    return out


def _coalesce(updates: list[tuple[str, list[list]]]
              ) -> list[tuple[str, list[list]]]:
    """输入:[(A1范围, 值矩阵)] → 输出:相邻同列的合成一段(**保持原序**)。

    定点回写的调用方几乎都是"一行一个 range"(`C{r}:G{r}`),而整表重写走的是
    "一段 4000 行"。同一个接口,两条路径差了三个数量级:
      · 一行一 range → 100 行/请求 + 0.3s 节流 ⇒ 28000 行要 280 个请求、~2 分钟
      · 合成段     → 4000 行/range、100 range/请求 ⇒ 同样 28000 行 1 个请求
    这就是所有者 2026-08-16 问的「写飞书的速度怎么各处都不一样,有的 4000
    有的几十甚至逐行」—— 差别不在接口,在调用方给的形状。**在这里补齐**:
    调用方照旧一行一个 range,api 层负责把连号的粘起来(分批是 api 层职责)。

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


def sheet_write_ranges(sheet: Spreadsheet, updates: list[tuple[str, list[list]]]) -> int:
    """输入:登记条目 + [(A1范围, 值矩阵)] → 输出:**写入的行数**。

    定点回写(如逐行写 E{r}:G{r} 三列),与 sheet_overwrite 的整表重写互补。
    三步:**连号的先粘成段**(否则一行一个请求位,几万行要跑几分钟)→
    按行切开过大的段 → 按 100 范围/批 values_batch_update,批间节流。

    ⚠ 返回的是行数不是范围数。粘段之前两者恰好相等(调用方全是一行一 range),
    所有调用方也都当行数在用(「回填 N 行」);粘段之后必须显式数行,
    否则那些摘要会一夜之间从「回填 200 行」变成「回填 1 行」。
    """
    s = sheet.require()
    split: list[tuple[str, list[list]]] = []
    for rng, vals in _coalesce(updates):
        split += _split_rows(rng, [[_scrub(c) for c in row] for row in vals])
    n = 0
    for i in range(0, len(split), 100):
        chunk = split[i:i + 100]
        try:
            _call("POST",
                  f"/open-apis/sheets/v2/spreadsheets/{_sheet_token(s)}/values_batch_update",
                  json_body={"valueRanges": [
                      {"range": f"{s.sheet_id}!{rng}", "values": vals}
                      for rng, vals in chunk]})
        except FeishuError as e:
            # 报错带上范围:飞书只说 validate RangeVal fail,不说哪一块
            raise FeishuError(e.code, f"{e}(范围 {chunk[0][0]}~{chunk[-1][0]},"
                                      f"{sum(len(v) for _r, v in chunk)} 行)") from None
        n += sum(len(v) for _r, v in chunk)
        if i + 100 < len(split):
            time.sleep(_SHEET_WRITE_THROTTLE_SECS)
    return n


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
        _call("POST", f"/open-apis/sheets/v2/spreadsheets/{_sheet_token(s)}/values_batch_update",
              json_body={"valueRanges": [{"range": rng, "values": block}]})
        written += len(block)
        logger.info("电子表格「%s」写入 %d/%d 行", s.name, written, len(rows))
        if i + _SHEET_WRITE_BLOCK_ROWS < len(rows):
            time.sleep(_SHEET_WRITE_THROTTLE_SECS)

    surplus = sheet_row_count(s) - len(rows)
    trimmed = surplus
    while surplus > 0:        # 从尾部分块删,单次 ≤5000(与扩行同限制)
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
    """
    s = sheet.require()
    if not items:
        return 0
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
