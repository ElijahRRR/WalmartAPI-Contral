"""沃尔玛 feed 域:全项目唯一 feed 通道(蓝图 §5 定稿,矩阵 #10/#11/#12/#16/#17)。

  submit_feed(store, feed_type, entries, workflow) -> [切片结果 dict]
  get_feed_status(store, feed_id) -> 汇总 dict
  iter_feed_items(store, feed_id) -> 逐 SKU 明细生成器(50/页自动翻)
  find_recent_feed(store, feed_type, items_received) -> 反查三态

旧系统之乱(本文件的存在理由):6 套 header schema 散落、DELETE_ITEM 3 处
裸 httpx 提交、防重语义七零八落。收口后:
- header schema 按 feedType 分发,版本字符串唯一出处 registry.FEED_SPEC_VERSIONS;
- 切片按「条数+字节」双约束(DELETE_ITEM 官方 400KB,定稿 350KB+2500 条);
- **三层防重**(安全铁律):①提交前先写 ops.feed_log(status=pending),同
  (feed_type, store, payload_key) 已存在即拒绝重复提交;②POST 网络异常后
  find_recent_feed 反查三态,FOUND 收编、NOT_FOUND(30s 双确认)按同一方法
  补交一次、UNKNOWN 保持 pending 留给启动对账;③工作流启动时对账 pending/
  submitted 行(query_pending 供工作流用)。
- 写操作永不跨方法兜底(CLAUDE.md):补交只用同一 feedType 同一载荷。

本层只做接口适配:哪些 SKU 该删该停是 services/workflows 的事。
"""

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote

from api import _client
from registry import db, resources

logger = logging.getLogger("api.feeds")

# 终态与 SKU 级状态映射(官方枚举 2026-08-05 核验;不存在 COMPLETE)
FEED_TERMINAL = {"PROCESSED", "ERROR"}
FEED_STATUSES = {"RECEIVED", "INPROGRESS", "PROCESSED", "ERROR"}
_SKU_SUCCESS = {"SUCCESS"}
_SKU_FAILED = {"DATA_ERROR", "SYSTEM_ERROR", "TIMEOUT_ERROR"}
_SKU_INPROGRESS = {"INPROGRESS"}

_DETAIL_PAGE = 50          # includeDetails 明细页大小(官方两页矛盾 50 vs 1000,保守)
_PAGE_SLEEP = 0.2
_RECHECK_SLEEP = 30        # 反查 NOT_FOUND 的二次确认间隔(防索引滞后)

# 切片双约束:(最大条数, 最大字节)。RETIRE_ITEM 官方无现值,按 DELETE 同档保守
_SLICE_LIMITS = {
    "DELETE_ITEM": (2500, 350_000),
    "RETIRE_ITEM": (1000, 350_000),
    "MP_MAINTENANCE": (1000, 24_000_000),
}


def _sanitize(v):
    """数值一律 round ≤2 位小数(Walmart 拒收 >2 位,蓝图 §5.1 sanitize 兜底)。"""
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, dict):
        return {k: _sanitize(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_sanitize(x) for x in v]
    return v


def build_payload(feed_type: str, entries: list) -> dict:
    """输入:feedType + 条目(DELETE/RETIRE 传 SKU 字符串列表,MAINTENANCE 传
    MPItem dict 列表)→ 输出:完整 feed 载荷(header schema 按类型分发)。

    header 用旧系统实测值而非官方 sample(蓝图 §5.1:官方 sample 不可信)。
    """
    ver = resources.FEED_SPEC_VERSIONS.get(feed_type)
    if feed_type == "DELETE_ITEM":
        return {"ItemFeedHeader": {"businessUnit": "WALMART_US", "locale": "en",
                                   "version": ver},
                "Item": [{"Deletable": {"sku": str(s)}} for s in entries]}
    if feed_type == "RETIRE_ITEM":
        # feedDate 必须真 UTC(旧系统本地时间硬拼 Z 后缀是坑,已修)
        feed_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return {"RetireItemHeader": {"feedDate": feed_date, "version": ver},
                "RetireItem": [{"sku": str(s)} for s in entries]}
    if feed_type == "MP_MAINTENANCE":
        return {"MPItemFeedHeader": {"businessUnit": "WALMART_US", "locale": "en",
                                     "version": ver},
                "MPItem": [_sanitize(e) for e in entries]}
    raise ValueError(f"feedType 未在 api/feeds.py 收录: {feed_type}"
                     f"(蓝图收录规则:预留端点只登记不实现)")


def _slices(feed_type: str, entries: list) -> list[list]:
    """按条数+字节双约束切片(字节按逐条序列化长度估算,含分隔符余量)。"""
    max_items, max_bytes = _SLICE_LIMITS[feed_type]
    base = len(json.dumps(build_payload(feed_type, []), ensure_ascii=False).encode())
    out, cur, size = [], [], base
    for e in entries:
        wrapped = build_payload(feed_type, [e])
        esz = (len(json.dumps(wrapped, ensure_ascii=False).encode()) - base) + 2
        if cur and (len(cur) >= max_items or size + esz > max_bytes):
            out.append(cur)
            cur, size = [], base
        cur.append(e)
        size += esz
    if cur:
        out.append(cur)
    return out


def payload_key(feed_type: str, entries: list) -> str:
    """输入:feedType + 条目 → 输出:内容指纹(防重键;条目顺序无关)。"""
    canon = sorted(json.dumps(e, ensure_ascii=False, sort_keys=True, default=str)
                   if isinstance(e, dict) else str(e) for e in entries)
    return hashlib.sha256("\x1f".join([feed_type, *canon]).encode()).hexdigest()[:32]


# ── ops.feed_log(三层防重的第①层)──────────────────────────────────────────

def _log_claim(workflow: str, store_name: str, feed_type: str, key: str):
    """输入:防重四元组 → 输出:(log_id, None) 抢占成功 / (None, 既有行 dict)。

    既有行 status='failed'(已确认未达)允许重占回 pending——failed 是唯一
    可安全重试的终态;pending/submitted/done 一律拒绝重复提交。
    """
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.feed_log (workflow, store, feed_type, payload_key, status) "
            "VALUES (%s, %s, %s, %s, 'pending') "
            "ON CONFLICT (feed_type, store, payload_key) DO NOTHING RETURNING id",
            (workflow, store_name, feed_type, key))
        row = cur.fetchone()
        if row:
            return row[0], None
        cur.execute("SELECT id, status, feed_id FROM ops.feed_log "
                    "WHERE feed_type = %s AND store = %s AND payload_key = %s",
                    (feed_type, store_name, key))
        prev = cur.fetchone()
        if prev and prev[1] == "failed":
            cur.execute("UPDATE ops.feed_log SET status = 'pending', "
                        "updated_at = now() WHERE id = %s", (prev[0],))
            logger.info("feed 防重:failed 行重占为 pending(%s %s)",
                        store_name, feed_type)
            return prev[0], None
    return None, {"id": prev[0], "status": prev[1], "feed_id": prev[2]}


def _log_update(log_id, status: str, feed_id: str | None = None) -> None:
    with db.pg_conn() as conn:
        conn.execute(
            "UPDATE ops.feed_log SET status = %s, "
            "feed_id = COALESCE(%s, feed_id), updated_at = now() WHERE id = %s",
            (status, feed_id, log_id))


def query_pending(store_name: str | None = None) -> list[dict]:
    """输入:可选店铺 → 输出:pending/submitted 的 feed_log 行(启动对账用)。"""
    sql = ("SELECT id, workflow, store, feed_type, payload_key, feed_id, status, "
           "created_at FROM ops.feed_log WHERE status IN ('pending', 'submitted')")
    args: tuple = ()
    if store_name:
        sql += " AND store = %s"
        args = (store_name,)
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def mark_feed_done(feed_id: str, ok: bool) -> None:
    """输入:feed_id + 终态是否成功 → 输出:无(feed_log 行落终态)。"""
    with db.pg_conn() as conn:
        conn.execute("UPDATE ops.feed_log SET status = %s, updated_at = now() "
                     "WHERE feed_id = %s", ("done" if ok else "failed", feed_id))


def _chunk_skus(feed_type: str, chunk: list) -> list[str]:
    if feed_type == "MP_MAINTENANCE":
        # MPItem 的 sku 可能在顶层或嵌在 Orderable 里(反补载荷是后者)
        return [str(e.get("sku") or (e.get("Orderable") or {}).get("sku") or "")
                for e in chunk]
    return [str(s) for s in chunk]


def _items_record(feed_id: str, workflow: str, store_name: str,
                  feed_type: str, skus: list[str]) -> None:
    """提交成功即落 SKU 级台账(ops.feed_items,status=submitted);
    终态由 services/feed_track 轮询回写。SKU 级状态权威在库,飞书只是投影。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ops.feed_items (feed_id, sku, workflow, store, feed_type, "
            "status) VALUES (%s, %s, %s, %s, %s, 'submitted') "
            "ON CONFLICT (feed_id, sku) DO NOTHING",
            [(feed_id, s, workflow, store_name, feed_type) for s in skus if s])


# ── 提交(唯一入口)────────────────────────────────────────────────────────────

def submit_feed(store: dict, feed_type: str, entries: list, *,
                workflow: str = "") -> list[dict]:
    """输入:店铺 + feedType + 条目 → 输出:逐切片结果
    [{"feed_id", "count", "outcome": submitted/dedup/unknown/failed}]。

    写操作零自动重试(CLAUDE.md:换方法重试=重复提交制造机);网络异常走
    反查三态,只有**确认未达**才按同一载荷补交一次。
    """
    if feed_type not in _SLICE_LIMITS:
        raise ValueError(f"feedType 未收录: {feed_type}")
    results = []
    for chunk in _slices(feed_type, entries):
        key = payload_key(feed_type, chunk)
        log_id, prev = _log_claim(workflow, store["name"], feed_type, key)
        if log_id is None:
            logger.warning("feed 防重命中:%s %s 同载荷已存在(status=%s feed_id=%s),"
                           "拒绝重复提交", store["name"], feed_type,
                           prev["status"], prev["feed_id"])
            results.append({"feed_id": prev["feed_id"], "count": len(chunk),
                            "outcome": "dedup"})
            continue
        results.append(_submit_one(store, feed_type, chunk, log_id, workflow))
    return results


def _post(store: dict, feed_type: str, payload: dict):
    _client.rate_acquire(f"feeds.post.{feed_type}", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"],
                              store["proxy"])
    return _client.safe_post_ex(
        f"{_client.base_url()}/v3/feeds", token, store["client_id"],
        store["proxy"], json_body=payload, params={"feedType": feed_type},
        timeout=120)


def _submit_one(store: dict, feed_type: str, chunk: list, log_id,
                workflow: str = "") -> dict:
    payload = build_payload(feed_type, chunk)
    skus = _chunk_skus(feed_type, chunk)

    def _ok(feed_id: str) -> dict:
        _log_update(log_id, "submitted", feed_id)
        _items_record(feed_id, workflow, store["name"], feed_type, skus)
        return {"feed_id": feed_id, "count": len(chunk), "outcome": "submitted"}

    status, _, data = _post(store, feed_type, payload)
    n = len(chunk)

    if status == 200 and data and data.get("feedId"):
        logger.info("feed 提交成功:%s %s %d 条 feedId=%s",
                    store["name"], feed_type, n, data["feedId"])
        return _ok(data["feedId"])

    if status is not None:
        # 服务端明确拒绝(4xx/5xx):没提交上,落 failed,绝不自动换姿势重试
        _log_update(log_id, "failed")
        logger.error("feed 提交被拒:%s %s HTTP %s 响应=%s",
                     store["name"], feed_type, status, str(data)[:300])
        return {"feed_id": None, "count": n, "outcome": "failed"}

    # 网络异常(status=None):不知道到没到 → 反查三态
    verdict, feed = find_recent_feed(store, feed_type, n)
    if verdict == "FOUND":
        logger.warning("feed 网络异常但反查已达:%s %s feedId=%s(收编,不补交)",
                       store["name"], feed_type, feed["feedId"])
        return _ok(feed["feedId"])
    if verdict == "NOT_FOUND":
        logger.warning("feed 网络异常且双确认未达:%s %s,按同一载荷补交一次",
                       store["name"], feed_type)
        status2, _, data2 = _post(store, feed_type, payload)
        if status2 == 200 and data2 and data2.get("feedId"):
            return _ok(data2["feedId"])
        _log_update(log_id, "failed")
        return {"feed_id": None, "count": n, "outcome": "failed"}
    # UNKNOWN:保持 pending,留给启动对账(query_pending),人不在环时宁停不重
    logger.error("feed 网络异常且反查不确定:%s %s 保持 pending 待启动对账",
                 store["name"], feed_type)
    return {"feed_id": None, "count": n, "outcome": "unknown"}


# ── 反查三态(蓝图 §5.2 ②)──────────────────────────────────────────────────

def find_recent_feed(store: dict, feed_type: str, items_received: int,
                     window_minutes: int = 30) -> tuple[str, dict | None]:
    """输入:店铺 + feedType + 精确条数 → 输出:(FOUND/NOT_FOUND/UNKNOWN, feed)。

    按 (feedType, itemsReceived 精确数, feedDate 时间窗) 匹配"刚才那笔";
    NOT_FOUND 需 30s 后二次确认(防沃尔玛索引滞后)。查询自身失败 → UNKNOWN。
    """
    def _probe():
        _client.rate_acquire("feeds.get", store["client_id"])
        token = _client.get_token(store["client_id"], store["client_secret"],
                                  store["proxy"])
        status, _, data = _client.safe_get_ex(
            f"{_client.base_url()}/v3/feeds", token, store["client_id"],
            store["proxy"], params={"feedType": feed_type, "limit": 50},
            max_retries=2)
        if status != 200 or not isinstance(data, dict):
            return None
        feeds = ((data.get("results") or {}).get("feed")
                 or data.get("feed") or [])
        cutoff = time.time() * 1000 - window_minutes * 60_000
        for f in feeds:
            fd = f.get("feedDate")
            fd_ms = fd if isinstance(fd, (int, float)) else None
            if (f.get("itemsReceived") == items_received
                    and (fd_ms is None or fd_ms >= cutoff)):
                return f
        return {}

    first = _probe()
    if first is None:
        return "UNKNOWN", None
    if first:
        return "FOUND", first
    time.sleep(_RECHECK_SLEEP)
    second = _probe()
    if second is None:
        return "UNKNOWN", None
    if second:
        return "FOUND", second
    return "NOT_FOUND", None


# ── 状态轮询 ──────────────────────────────────────────────────────────────────

def _feed_url(feed_id: str) -> str:
    # feedId 含 '@',必须整体转义(蓝图 §5.3 实证)
    return f"{_client.base_url()}/v3/feeds/{quote(feed_id, safe='')}"


def get_feed_status(store: dict, feed_id: str) -> dict:
    """输入:店铺 + feed_id → 输出:汇总 dict(feedStatus/itemsReceived/…)。

    未知 feedStatus 告警而非静默"处理中"(防官方加值导致行永久卡死,C1 实证)。
    """
    _client.rate_acquire("feeds.get", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"],
                              store["proxy"])
    status, _, data = _client.safe_get_ex(
        _feed_url(feed_id), token, store["client_id"], store["proxy"],
        params={"limit": 0}, max_retries=2)
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"feed 状态查询失败 HTTP {status}(feedId={feed_id})")
    fs = data.get("feedStatus")
    if fs not in FEED_STATUSES:
        logger.warning("未知 feedStatus=%r(feedId=%s),官方枚举可能已扩,请核对",
                       fs, feed_id)
    return data


def iter_feed_items(store: dict, feed_id: str):
    """输入:店铺 + feed_id → 输出:逐 SKU 明细生成器(itemDetails,50/页自动翻)。"""
    offset = 0
    token = _client.get_token(store["client_id"], store["client_secret"],
                              store["proxy"])
    while True:
        _client.rate_acquire("feeds.get", store["client_id"])
        status, _, data = _client.safe_get_ex(
            _feed_url(feed_id), token, store["client_id"], store["proxy"],
            params={"includeDetails": "true", "limit": _DETAIL_PAGE,
                    "offset": offset},
            max_retries=2)
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"feed 明细查询失败 HTTP {status}(feedId={feed_id})")
        items = ((data.get("itemDetails") or {}).get("itemIngestionStatus")
                 or [])
        yield from items
        total = data.get("itemsReceived") or 0
        offset += len(items)
        # 双轨终止:累计条数够了,或本页不足一整页
        if offset >= total or len(items) < _DETAIL_PAGE:
            return
        time.sleep(_PAGE_SLEEP)


def sku_outcome(ingestion_status: str) -> str:
    """输入:SKU 级 ingestionStatus → 输出:success/failed/processing/unknown。

    官方五值(含旧系统遗漏的 INPROGRESS);未知值告警返回 unknown,不装成功也不装失败。
    """
    s = (ingestion_status or "").upper()
    if s in _SKU_SUCCESS:
        return "success"
    if s in _SKU_FAILED:
        return "failed"
    if s in _SKU_INPROGRESS:
        return "processing"
    logger.warning("未知 SKU ingestionStatus=%r,官方枚举可能已扩", ingestion_status)
    return "unknown"
