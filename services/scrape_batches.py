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
# 采集侧对这些走 cap=3(MAX_RETRIES)+ 最多 2 轮自动重试(间隔 5 分钟),
# 总尝试上限约 9 次 —— **一个批次收敛得多慢,由它们决定**,
# 这也是本侧等待兜底取 20 分钟的由来。
RETRYABLE = {"network", "timeout", "blocked", "captcha",
             "zip_switch_failed", "zip_not_effective", "session_not_ready"}

# 重采一百次也是同一个结果的类型 —— 采集侧 NO_AUTO_RETRY_ERROR_TYPES 的镜像
# (2026-08-10 所有者实测口径):失败上限 cap=1,**首次上报即终态**,
# 自动重试排除,手动 POST /{name}/retry 跳过,连 ?force=true 也不越过。
# 因为它们描述的是产品/页面层的稳定事实(如 variant_offset:Amazon 把请求
# 重定向到了兄弟变体页),不是瞬时抖动。
#
# ⚠ 所以这类**任务反而是最快到终态的**,`tasks.open` 立刻减一,不拖慢批次。
# 本侧据此给终局结论(建议拒绝)而不是永远挂"待采集"——挂着的话行会等一个
# 永远不来的快照,每轮还为它烧一次配额,两侧都不报错。
#
# `unknown` **故意不在此列**:它是兜底桶,拿不准就别替人下终局结论,
# 照旧转人工。采集侧新增类型时 pull_failures 会告警,人来决定归哪边。
TERMINAL = set(ERROR_TYPES) - RETRYABLE - {"unknown"}

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
            " status = EXCLUDED.status, note = EXCLUDED.note,"
            # 重新回到在途 ⇒ 清掉 finished_at,让"稳定多久"的表从零重新计。
            # 采集侧会把失败任务从 server 推回 worker 再循环两轮,tasks.open
            # 归零**不代表最终**(所有者 2026-08-10 澄清)。不清的话:第一次
            # 归零时记下的 finished_at 会让稳定窗口提前满足,而那批数据其实
            # 正在重采路上,我们却已经认账失败了。
            " finished_at = CASE WHEN EXCLUDED.status IN ('pushed','running')"
            "                    THEN NULL ELSE ops.scrape_batches.finished_at END",
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
    """输入:batch_status 响应体 → 输出:**数据**是否已落定(采不出新东西了)。

    判据 = `tasks.open == 0`(`open` = 既不是 done 也不是 failed 的数量;
    failed 算终态,所以一个采不出来的 ASIN 不会把批次卡死)。

    ⚠ **不看 screenshots.open**(所有者 2026-08-10 实测后改):一个任务失败
    (如 `variant_offset`)之后它那张图永远不会被截出来,`screenshots.open`
    就永久停在 >0,批次于是**永远落不定**——实测表现为 `-p wait=1` 明明数据
    都采完了却干等满 20 分钟。
    截图是佐证材料,本来就"从不阻断审核结论",更不该阻断数据侧的落定判断。
    早点认落定反而让截图更快贴上:`_settled_batches` 只对已落定的批次去拿
    清单,图后来好了下一轮照样补得上。

    没有 open 字段的旧响应体退回看顶层 status——**未知一律当"还在跑"**,
    宁可多等一轮也不要把在途批次误判成落定后重推(重推 = 重复烧配额)。
    """
    stats = status_body.get("stats") or {}
    if "open" in stats:
        return int(stats.get("open") or 0) == 0
    return str(status_body.get("status") or "").lower() in ("completed", "failed")


def shots_open(status_body: dict) -> int:
    """输入:batch_status 响应体 → 输出:还有几张图没截完(拿不到则 0)。

    只用来决定"要不要再多等一小会儿把图等出来",**不参与落定判断**——
    见 is_settled 里那条:任务失败后它那张图永远不会好,拿它当闸会永久卡住。
    """
    try:
        return int((status_body.get("screenshots") or {}).get("open") or 0)
    except (TypeError, ValueError):
        return 0
