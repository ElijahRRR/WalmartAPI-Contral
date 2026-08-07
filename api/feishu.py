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


def sync_by_key(table: Bitable, key_field: str, desired: dict[str, dict],
                *, delete_stale: bool = True,
                hash_field: str | None = None) -> tuple[int, int, int]:
    """输入:表 + 去重键字段名 + {键: fields dict} → 输出:(新建, 更新, 删除) 计数。

    投影同步(PG 权威):键不存在则建,存在则**只覆盖 fields 里给出的字段**
    (desired 里为 None 的字段显式送 null 清空,不能省略——省略=保留飞书旧值;
    因此人工/关联字段只要不出现在 fields 里就绝不会被碰)。

    hash_field(变更检测,万行级表的写放大治理):写行时同时存载荷指纹;
    下一轮把指纹随键读回,指纹一致的行跳过不写——写请求量从"窗口行数"
    降到"真实变化行数"。副作用要知情:人工改动程序列后,只要 PG 侧没变化,
    该行不会被重写纠正(指纹仍一致)——程序列本就不该手改。

    delete_stale=True:飞书多出的键删除,重复/无键行清理——仅限程序独占展示表。
    delete_stale=False:任何行都不删(键消失只是停止刷新);用于与人工列/
    关联字段共存的表(删行会断关联、丢人工数据),重复/无键行仅告警。

    防错登记守卫:表里已有记录但没有任何一行能读出键字段 → 大概率 table_id
    填错(指向了别的表),拒绝执行而不是把人家的表写坏。
    """
    field_names = [key_field] + ([hash_field] if hash_field else [])
    existing = list_records(table, field_names=field_names)
    by_key: dict[str, str] = {}
    old_hash: dict[str, str] = {}
    dupes: list[str] = []
    for rec in existing:
        k = _plain_text(rec["fields"].get(key_field)).strip()
        if not k or k in by_key:
            dupes.append(rec["record_id"])
        else:
            by_key[k] = rec["record_id"]
            if hash_field:
                old_hash[k] = _plain_text(rec["fields"].get(hash_field)).strip()
    if existing and not by_key:
        raise FeishuError(None,
                          f"表「{table.name}」现有 {len(existing)} 行均无「{key_field}」字段值,"
                          f"疑似 table_id 登记错表,拒绝同步(会写坏对方数据)")
    if dupes:
        logger.warning("表「%s」发现 %d 行重复/无键记录%s", table.name, len(dupes),
                       ",将删除" if delete_stale else "(不删,请人工核查)")

    if hash_field:
        for f in desired.values():
            f[hash_field] = _row_hash(f, hash_field)

    creates = [f for k, f in desired.items() if k not in by_key]
    updates = [{"record_id": by_key[k], "fields": f}
               for k, f in desired.items()
               if k in by_key and (not hash_field
                                   or old_hash.get(k) != f[hash_field])]
    unchanged = len(desired) - len(creates) - len(updates)
    deletes = (dupes + [rid for k, rid in by_key.items() if k not in desired]) \
        if delete_stale else []
    if creates:
        batch_create(table, creates)
    if updates:
        batch_update(table, updates)
    if deletes:
        batch_delete(table, deletes)
    logger.info("表「%s」同步:新建 %d,更新 %d,删除 %d%s",
                table.name, len(creates), len(updates), len(deletes),
                f",指纹一致跳过 {unchanged}" if hash_field else "")
    return len(creates), len(updates), len(deletes)


def ensure_keys(table: Bitable, key_field: str, keys: set[str]) -> int:
    """输入:表 + 键字段名 + 应存在的键集合 → 输出:本次补建的行数。

    人工域/枢纽表(如主订单表、采购表)的键补齐:缺键的建一行且**只写键字段**,
    既有行永不更新、永不删除——其余列全部归人工与关联字段所有。
    """
    existing = {_plain_text(r["fields"].get(key_field)).strip()
                for r in list_records(table, field_names=[key_field])}
    missing = sorted(k for k in keys if k and k not in existing)
    if missing:
        batch_create(table, [{key_field: k} for k in missing])
    logger.info("表「%s」键补齐:新建 %d 行(已有 %d)",
                table.name, len(missing), len(existing))
    return len(missing)


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


def sheet_write_ranges(sheet: Spreadsheet, updates: list[tuple[str, list[list]]]) -> int:
    """输入:登记条目 + [(A1范围, 值矩阵)] → 输出:写入的范围数。

    定点回写(如逐行写 E{r}:G{r} 三列),与 sheet_overwrite 的整表重写互补;
    按 100 范围/批切块 values_batch_update,批间节流。
    """
    s = sheet.require()
    n = 0
    for i in range(0, len(updates), 100):
        chunk = updates[i:i + 100]
        _call("POST",
              f"/open-apis/sheets/v2/spreadsheets/{_sheet_token(s)}/values_batch_update",
              json_body={"valueRanges": [
                  {"range": f"{s.sheet_id}!{rng}", "values": vals}
                  for rng, vals in chunk]})
        n += len(chunk)
        if i + 100 < len(updates):
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
