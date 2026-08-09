"""product_refresh — 在线产品全量重推采集(维护链的数据新鲜度源头)。

用法:
  python cli.py product_refresh                 # dry-run:列出将推多少个 ASIN
  python cli.py product_refresh --execute       # 真推(建批次 + 落台账)
  python cli.py product_refresh -p wait=1       # 真推后阻塞等采完(默认不等)
  python cli.py product_refresh -p check=1      # 只查在途批次状态(不推新的)

批次落定(采完/超时)时**顺手拉失败明细落 ops.scrape_failures**:验证码/超时/
封禁这类"根本没采到"的 ASIN 在增量流里压根不出现(没产出记录就没有可导出
的行),不主动拉就永远答不上"这个 ASIN 为什么没有新数据"。
降级采到的(outcome≠ok)则在 catalog.snapshots,两者互补,见 docs/db_schema.md。

**这是旧工作流第 2 步的等价物**(所有者澄清 2026-08-09:旧维护三步 =
获取在线产品 → 推送采集拿最新 amz 数据并自动计算 → 读决策并提交)。
没有这一步,latest_snapshot 会越来越陈旧,而 maintenance 的三个 provider
照样会拿着陈旧数据算出价格和库存去提交——**比任何字段问题都危险**。

口径(所有者定稿 2026-08-09):
  · 每次改价前**全量重推**在线产品(PUBLISHED + 店铺 ACTIVE),不做优先级
  · 采集器吞吐 2000~3000/分钟(不切邮编、不截图)
  · **一次性提交一个批次**(上传上限 50MB,几十万 ASIN 也只有几 MB)
  · **必须确认推上去了才开始计时**;超时 1 小时
  · 批次命名:取回按批次比整库查快得多

批次语义(v4 实证,与 v3 不同):批次名撞名返 **409 且绝不静默合并**,
响应体带既有 batch_id ⇒ 本端点**可以安全重试**,200 恒等于新建批次,
inserted 无歧义。所以不需要 v3 那套毫秒精度躲合并的把戏。
"""

import logging
from datetime import datetime, timedelta, timezone

from api import scraper
from registry import db
from services import kpi

DANGEROUS = True        # 会给采集器压十几万个任务,默认 dry-run

logger = logging.getLogger("workflows.product_refresh")

# **默认一次性全量提交一个批次**(所有者质疑 2026-08-09,复核后改):
# 采集侧上传上限 50MB,txt 每个 ASIN 约 11 字节 —— 27722 个才 ~300KB(0.6%),
# 百万级也只有 ~11MB。分批不省任何资源,反而把"按批次取回"拆成多次聚合、
# 台账多行、查进度要合并。BATCH_SIZE 因此只是**安全阀**(远超当前规模),
# 不是常规切分:真到那个量级再考虑分批的可观测性。
BATCH_SIZE = 200000
TIMEOUT_HOURS = 1           # 推上去后多久没采完算超时(所有者定稿)

# 在线且店铺 ACTIVE 的 ASIN。同一 ASIN 多店只推一次(采集结果按 ASIN 共享)。
# 店铺状态取 ops.store_kpi_daily 每店最新一行;无 KPI 记录的店视为 ACTIVE
# (与 list_new 闸门同口径,fail-open)。
_SQL_TARGETS = """
WITH latest_status AS (
    SELECT DISTINCT ON (store) store, store_status
    FROM ops.store_kpi_daily ORDER BY store, data_date DESC
)
SELECT DISTINCT w.sku
FROM catalog.walmart_items w
LEFT JOIN latest_status s ON s.store = w.store
WHERE w.missing_since IS NULL
  AND w.published_status = 'PUBLISHED'
  AND (s.store_status IS NULL OR upper(s.store_status) = 'ACTIVE')
ORDER BY w.sku
"""

_SQL_OPEN = """
SELECT batch_name, batch_id, asin_count, status, submitted_at
FROM ops.scrape_batches
WHERE status IN ('pushed', 'running')
ORDER BY submitted_at
"""


def _targets() -> list[str]:
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_TARGETS)
        return [r[0] for r in cur.fetchall()]


def _record(batch_name: str, batch_id, n: int, status: str,
            note: str = "") -> None:
    with db.pg_conn() as conn:
        conn.execute(
            "INSERT INTO ops.scrape_batches (batch_name, batch_id, asin_count,"
            " status, note) VALUES (%s,%s,%s,%s,%s)"
            " ON CONFLICT (batch_name) DO UPDATE SET"
            " batch_id = COALESCE(EXCLUDED.batch_id, ops.scrape_batches.batch_id),"
            " status = EXCLUDED.status, note = EXCLUDED.note",
            (batch_name, str(batch_id) if batch_id else None, n, status,
             note or None))


def _finish(batch_name: str, status: str, done, failed, note: str = "") -> None:
    with db.pg_conn() as conn:
        conn.execute(
            "UPDATE ops.scrape_batches SET status = %s, done = %s,"
            " failed = %s, finished_at = now(), note = COALESCE(%s, note)"
            " WHERE batch_name = %s",
            (status, done, failed, note or None, batch_name))


_SQL_FAILURE = """
INSERT INTO ops.scrape_failures (batch_name, asin, status, error_type,
    error_detail, retry_count, occurred_at)
VALUES (%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (batch_name, asin) DO UPDATE SET
    status = EXCLUDED.status, error_type = EXCLUDED.error_type,
    error_detail = EXCLUDED.error_detail, retry_count = EXCLUDED.retry_count,
    occurred_at = EXCLUDED.occurred_at, recorded_at = now()
"""


def _ts_utc(v):
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


def _pull_failures(batch_name: str, batch_id) -> str:
    """输入:批次名 + batch_id → 输出:失败原因分布摘要(顺便落 ops.scrape_failures)。

    **拉失败明细是批次落定时的标准动作**(与 feed 报错同款口径):
    "这个 ASIN 为什么没有新数据" 是维护链遇到数据缺口时第一个要问的问题,
    而增量流里根本不会出现这些 ASIN——它们压根没产出记录。
    """
    if not batch_id:
        return "失败明细:该批次没记下 batch_id,查不了"
    try:
        rows = scraper.batch_failures(batch_id)
    except Exception as e:
        logger.warning("批次 %s 失败明细拉取失败:%s", batch_name, e)
        return f"失败明细:拉取失败({e})"
    if not rows:
        return "失败明细:无失败任务"
    dist: dict[str, int] = {}
    params = []
    for r in rows:
        et = str(r.get("error_type") or "unknown")
        dist[et] = dist.get(et, 0) + 1
        params.append((batch_name, r.get("asin"), r.get("status"), et,
                       (r.get("error_detail") or None), r.get("retry_count"),
                       _ts_utc(r.get("updated_at"))))
    params = [p for p in params if p[1]]        # 无 asin 的行没有落库价值
    if not params:
        return f"失败明细:{len(rows)} 行均无 asin,未落库(采集侧数据异常)"
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.executemany(_SQL_FAILURE, params)
    top = ",".join(f"{k}×{v}" for k, v in
                   sorted(dist.items(), key=lambda kv: -kv[1])[:5])
    return f"失败明细:{len(params)} 个 ASIN 已落库({top})"


def _check_open() -> list[str]:
    """输入:无 → 输出:在途批次的状态行(顺便按采集侧结果落定/标超时)。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_OPEN)
        rows = cur.fetchall()
    if not rows:
        return ["无在途采集批次"]
    out = []
    deadline = timedelta(hours=TIMEOUT_HOURS)
    for name, bid, n, status, submitted in rows:
        try:
            st = scraper.batch_status(name)
        except LookupError:
            _finish(name, "failed", None, None, "采集侧查无此批次")
            out.append(f"  {name}:⚠ 采集侧查无此批次(已标 failed)")
            continue
        except Exception as e:
            out.append(f"  {name}:状态查询失败 {e}(保持在途,下轮再查)")
            continue
        # 台账没记下 batch_id 时(老行/推送时响应异常)从状态响应补:
        # 失败明细端点只认 batch_id,缺了就查不了
        bid = bid or st.get("batch_id")
        stats = st.get("stats") or {}
        done, failed = stats.get("done") or 0, stats.get("failed") or 0
        total = stats.get("total") or n
        age = datetime.now(timezone.utc) - submitted.astimezone(timezone.utc)
        if str(st.get("status")) in ("completed", "failed") or done + failed >= total:
            _finish(name, "completed", done, failed)
            out.append(f"  {name}:✅ 采完 {done}/{total}(失败 {failed})"
                       f",耗时 {age.total_seconds() / 60:.0f} 分钟")
            out.append(f"      {_pull_failures(name, bid)}")
        elif age > deadline:
            # 超时不代表数据没用:已采到的照常进增量流,只是这批不再等
            _finish(name, "timeout", done, failed,
                    f"超 {TIMEOUT_HOURS} 小时未采完")
            out.append(f"  {name}:⏰ 超时({done}/{total}),已标 timeout;"
                       f"已采到的部分照常进增量流")
            # 超时批同样拉:此刻已判失败的那些,原因照样有价值
            out.append(f"      {_pull_failures(name, bid)}")
        else:
            _record(name, None, n, "running")
            out.append(f"  {name}:采集中 {done}/{total}"
                       f"(已 {age.total_seconds() / 60:.0f} 分钟)")
    return out


def run(params: dict) -> str:
    """输入:params(execute/check/wait)→ 输出:推送与批次状态摘要。"""
    if params.get("check"):
        return "\n".join(["在途采集批次:"] + _check_open())

    asins = _targets()
    if not asins:
        return "无在线产品可推(catalog_sync 是否跑过?)"

    batches = [asins[i:i + BATCH_SIZE]
               for i in range(0, len(asins), BATCH_SIZE)]
    est_min = len(asins) / 2500        # 所有者口径:2000~3000/分钟
    if not params.get("execute"):
        split = "一个批次" if len(batches) == 1 else f"{len(batches)} 个批次"
        return (f"🧪 [DRY-RUN] 将全量重推 {len(asins)} 个在线 ASIN"
                f"(PUBLISHED + 店铺 ACTIVE,跨店去重),{split};"
                f"按 2500/分钟估算约 {est_min:.0f} 分钟采完\n"
                + "\n".join(["在途批次:"] + _check_open()))

    stamp = datetime.now(kpi.CN_TZ).strftime("%Y%m%d-%H%M%S")
    pushed, lines = 0, []
    for i, chunk in enumerate(batches, 1):
        # 单批(常态)不带序号:批次名就是取回的抓手,越简单越好
        name = (f"wm-refresh-{stamp}" if len(batches) == 1
                else f"wm-refresh-{stamp}-{i:02d}")
        try:
            res = scraper.submit_batch(name, chunk)
            # 200 恒等于新建批次:拿到 batch_id 才算"确认推上去了",此刻起计时
            _record(name, res.get("batch_id"), len(chunk), "pushed",
                    f"inserted={res.get('inserted')}")
            pushed += len(chunk)
            lines.append(f"  {name}:推送 {len(chunk)} 个"
                         f"(inserted={res.get('inserted')})")
        except scraper.BatchExistsError as e:
            # 撞名 = 上一次其实推成功了(v4 绝不静默合并):接着用既有批次
            _record(name, e.batch_id, len(chunk), "pushed", "撞名沿用既有批次")
            pushed += len(chunk)
            lines.append(f"  {name}:已存在,沿用既有批次 {e.batch_id}")
        except Exception as e:
            _record(name, None, len(chunk), "failed", str(e)[:200])
            lines.append(f"  {name}:❌ 推送失败 {e}")
            logger.exception("批次 %s 推送失败", name)

    head = (f"全量重推:{pushed}/{len(asins)} 个 ASIN 已确认推上"
            f"({len(batches)} 个批次),预计约 {est_min:.0f} 分钟采完;"
            f"超时阈值 {TIMEOUT_HOURS} 小时")
    tail = ["", "查进度:python cli.py product_refresh -p check=1",
            "采完后拉数据:python cli.py product_ingest"]
    return "\n".join([head] + lines + tail)
