"""采集批次台账积木(product_refresh 全量重推 与 order_audit 按邮编采集共用)。

原先这几个函数长在 workflows/product_refresh.py 里,order_audit 接批次生命周期
时需要同一套 —— 工作流之间不准互相 import(铁律 1),抄第二份则两边语义迟早漂,
所以提到这里。

批次生命周期的判据(采集侧 2026-08-10 实测确认):

    completed  ⇔  tasks.open == 0  AND  screenshots.open == 0

盯 `open`(既不是 done 也不是 failed 的数量)就够。**failed 算终态** ——
一张永远截不出来的图不会把批次卡死(实测 1 done + 1 failed → completed)。

⚠ 批次 completed **不等于**我们库里有数据:数据还要经增量导出 → product_ingest
才落到 catalog.snapshots。所以批次状态只用来判断"还要不要等"与"失败原因是什么",
"这条数据到没到"必须另看快照是否真出现。
"""

import logging
from datetime import datetime, timezone

from api import scraper
from registry import db

logger = logging.getLogger("services.scrape_batches")

# 采集侧 error_type 封闭集(2026-08-10 实测确认,11 类 + 1 兜底)。
# 登记在这里是为了让"新出现的类型"能被一眼看出来——采集侧加了新类型而消费侧
# 不知道时,它会落进 unknown 而不是被静默当成普通失败。
ERROR_TYPES = {
    "network": "网络请求失败(连接错误/DNS/连接重置)",
    "timeout": "请求超时",
    "blocked": "被 Amazon 判定异常流量拦截(403/503,非验证码)",
    "captcha": "遇到验证码页",
    "parse_error": "页面拿到了但解析不出预期字段",
    "zip_switch_failed": "切换配送邮编失败",
    "zip_not_effective": "邮编多次重发仍未生效",
    "variant_offset": "重定向到兄弟变体页,不是目标 ASIN",
    "session_not_ready": "worker 本地 session 迟迟未就绪",
    "discover_failed": "卖家店铺发现阶段失败",
    "server_reject": "server 二次校验判定结果不合法",
    "unknown": "兜底",
}

# 重试有意义的类型:换个时段/换个 worker 可能就好了。
# 其余(variant_offset / parse_error / server_reject 等)重试多少次都一样,
# 由调用方决定是放弃还是转人工。
RETRYABLE = {"network", "timeout", "blocked", "captcha",
             "zip_switch_failed", "zip_not_effective", "session_not_ready"}

_SQL_FAILURE = """
INSERT INTO ops.scrape_failures (batch_name, asin, status, error_type,
    error_detail, retry_count, occurred_at)
VALUES (%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (batch_name, asin) DO UPDATE SET
    status = EXCLUDED.status, error_type = EXCLUDED.error_type,
    error_detail = EXCLUDED.error_detail, retry_count = EXCLUDED.retry_count,
    occurred_at = EXCLUDED.occurred_at, recorded_at = now()
"""


def record(batch_name: str, batch_id, n: int, status: str,
           note: str = "") -> None:
    """输入:批次名 + batch_id + ASIN 数 + 状态 → 输出:无(写 ops.scrape_batches)。"""
    with db.pg_conn() as conn:
        conn.execute(
            "INSERT INTO ops.scrape_batches (batch_name, batch_id, asin_count,"
            " status, note) VALUES (%s,%s,%s,%s,%s)"
            " ON CONFLICT (batch_name) DO UPDATE SET"
            " batch_id = COALESCE(EXCLUDED.batch_id, ops.scrape_batches.batch_id),"
            " status = EXCLUDED.status, note = EXCLUDED.note",
            (batch_name, str(batch_id) if batch_id else None, n, status,
             note or None))


def finish(batch_name: str, status: str, done, failed, note: str = "") -> None:
    """输入:批次名 + 终态 + done/failed 计数 → 输出:无(落定 ops.scrape_batches)。"""
    with db.pg_conn() as conn:
        conn.execute(
            "UPDATE ops.scrape_batches SET status = %s, done = %s,"
            " failed = %s, finished_at = now(), note = COALESCE(%s, note)"
            " WHERE batch_name = %s",
            (status, done, failed, note or None, batch_name))


def ts_utc(v):
    """输入:采集侧 updated_at → 输出:带时区的 datetime(或 None)。

    采集侧存的是 **UTC 裸串** `'YYYY-MM-DD HH:MM:SS'`(无时区标记)。
    直接塞进 timestamptz 会按会话时区解释——本机 CN_TZ 下整整差 8 小时,
    而且不会报错。所以这里显式补 UTC。
    """
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        logger.warning("采集失败明细时间无法解析(按空处理): %r", v)
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def pull_failures(batch_name: str, batch_id) -> tuple[str, dict]:
    """输入:批次名 + **batch_id**(不是名字)→ 输出:(摘要, {asin: error_type})。

    **拉失败明细是批次落定时的标准动作**(与 feed 报错同款口径):
    "这个 ASIN 为什么没有新数据" 是遇到数据缺口时第一个要问的问题,
    而增量流里根本不会出现这些 ASIN——它们压根没产出记录。

    返回的 {asin: error_type} 让调用方能把真实原因写进自己的台账,
    而不是一律记成"超时未见快照"——验证码(换时段可重试)和 404
    (该去删链接了)的处置完全不同。
    """
    if not batch_id:
        return "失败明细:该批次没记下 batch_id,查不了", {}
    try:
        rows = scraper.batch_failures(batch_id)
    except Exception as e:
        logger.warning("批次 %s 失败明细拉取失败:%s", batch_name, e)
        return f"失败明细:拉取失败({e})", {}
    if not rows:
        return "失败明细:无失败任务", {}
    dist: dict[str, int] = {}
    by_asin: dict[str, str] = {}
    params = []
    for r in rows:
        et = str(r.get("error_type") or "unknown")
        if et not in ERROR_TYPES:
            logger.warning("采集侧出现未登记的 error_type=%r(批次 %s)——"
                           "封闭集该更新了,见 services/scrape_batches.ERROR_TYPES",
                           et, batch_name)
        dist[et] = dist.get(et, 0) + 1
        asin = r.get("asin")
        if asin:
            by_asin[str(asin)] = et
        params.append((batch_name, asin, r.get("status"), et,
                       (r.get("error_detail") or None), r.get("retry_count"),
                       ts_utc(r.get("updated_at"))))
    params = [p for p in params if p[1]]        # 无 asin 的行没有落库价值
    if not params:
        return (f"失败明细:{len(rows)} 行均无 asin,未落库(采集侧数据异常)",
                {})
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.executemany(_SQL_FAILURE, params)
    top = ",".join(f"{k}×{v}" for k, v in
                   sorted(dist.items(), key=lambda kv: -kv[1])[:5])
    return f"失败明细:{len(params)} 个 ASIN 已落库({top})", by_asin


def is_settled(status_body: dict) -> bool:
    """输入:batch_status 响应体 → 输出:批次是否已落定(采不出新东西了)。

    判据 = `tasks.open == 0 AND screenshots.open == 0`(采集侧
    get_batch_completion_status 同一份口径,两个后端一致)。
    没有 open 字段的旧响应体退回看顶层 status——**未知一律当"还在跑"**,
    宁可多等一轮也不要把在途批次误判成落定后重推(重推 = 重复烧配额)。
    """
    stats = status_body.get("stats") or {}
    shots = status_body.get("screenshots") or {}
    if "open" in stats:
        return int(stats.get("open") or 0) == 0 and int(shots.get("open") or 0) == 0
    return str(status_body.get("status") or "").lower() in ("completed", "failed")
