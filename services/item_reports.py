"""ITEM 报表 → item_id 的判据与台账积木(item_id_sync 的业务节奏;api/reports 只管把接口调对)。

输入→输出一览:
  expected_header()                 无 → 实测表头列表(refdata/specs/item_report_header.txt)
  check_header(header)              报表表头 → (拦截原因, 漂移提示);三个关键列缺一即拦
  map_item_ids(rows)                报表行 → ({sku: item_id}, 计数):Item ID 列与 URL 尾段互校
  plan_updates(current, mapping)    在架现值 × 报表映射 → ({sku: 要写的 item_id}, 计数)
  coverage_note(counters)           计数 → 「疑似不全」提示或空串
  date_span(rows, column)           报表行 + 日期列名 → 最早/最晚/按年计数(探针判断数据范围按哪列筛)
  reconcile_breakdown(catalog, rows) 在架行画像 + 报表行 → 两边差集按状态/入库年分组(探针对账,不猜)
  wait_ready(store, request_id, …)  轮询到 READY / ERROR / TIMEOUT(先睡后查,列表生成器找到即停)
  台账 ops.report_requests:open_request / expire_stale / record_pending / mark_*

三条纪律(所有者定稿 2026-09-07):
  · **不复用**后台(Seller Center)或 Scheduler 生成的报表 —— 台账只认本仓自己
    POST 出去的 requestId;崩溃/超时后接着等的是自己那一份。
  · **报表为准**:库里已有 item_id 与报表不同,按报表改(计入 overwritten,摘要点名)。
  · **全量靠对账不靠参数**:请求体不传行过滤器,「拿全没有」用报表 SKU 集合 ×
    catalog_sync 扫回来的在架集合来证明(两边独立);覆盖率低于阈值在首行点名
    「疑似不全」,当轮照填已匹配的行,明天再拿一份。
  · **数据范围带近 DATA_RANGE_DAYS 天**(所有者 2026-09-07 22:xx 决定):不带日期的
    ITEM 报表只回 1 行(C021 探针,在架 1490 行;后台不设时间同样只显示很少),
    官方参数 dataStartTime/dataEndTime 放 body(格式要带毫秒,见 data_window),上限
    730 天。范围按哪个日期列筛
    官方没写 —— 覆盖率就是检验:老品掉出窗口会体现为「疑似不全」,那时把天数放到 730。

轮询节奏(官方 On-request Reports 页:生成典型 15–45 分钟;单查 20/hour;列表官方表
写 200/min 但生产实见是小时级桶 —— 2026-09-07 连打 4 次即 429、下枚令牌 142 秒后,
代码与单查共用 18/hour 的 reports.query 桶):先睡 poll_secs(5 分钟)再查,用**列表
生成器**找自己的 requestId、找到即停(不带日期参数,见 wait_ready);列表里找不到时
每第 STATUS_FALLBACK_EVERY 次才动用一次单查兜底。60 分钟 12 次 + 兜底 ≤2 次 < 18。
"""

import logging
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from api import reports
from registry import paths

logger = logging.getLogger("services.item_reports")

REPORT_TYPE = "ITEM"
REPORT_VERSION = "v6"          # 所有者贴的后台导出含 Product Condition(v6 新增);探针核对表头
REQUIRED_COLUMNS = ("SKU", "Item ID", "Item Page URL")

DEFAULT_WAIT_MIN = 60          # 官方典型 15–45 分钟;超过就把 requestId 留在台账下轮接着等
DEFAULT_POLL_SECS = 300        # 列表与单查共用 18/hour 桶:五分钟一问,60 分钟 12 次 + 兜底 ≤2 次 < 18
STATUS_FALLBACK_EVERY = 5      # 列表找不到 requestId 时,每第 N 次轮询才用一次 20/hour 单查

DATA_RANGE_DAYS = 365          # 报表数据范围:近一年(所有者定;官方上限 730 天;-p data_days= 可覆盖)

COVERAGE_WARN_RATIO = 0.95     # 报表匹配到的在架行 / 在架行 低于它 ⇒ 首行点名「疑似不全」
COVERAGE_MIN_ROWS = 20         # 在架行太少时比例没意义,不报

IN_FLIGHT_STATUSES = ("pending", "submitted", "ready")
RETENTION_DAYS = 30            # 官方:请求与报表保留 30 天
ORPHAN_PENDING_MIN = 15        # pending 无 requestId 超过它 = POST 前后崩溃留下的孤行


# ── 数据范围 ────────────────────────────────────────────────────────────────

def data_window(days: int = DATA_RANGE_DAYS, now: datetime | None = None) -> tuple[str, str]:
    """输入:天数(+ 可注入的当前时刻)→ 输出:(dataStartTime, dataEndTime),`YYYY-MM-DDTHH:mm:ss.000Z`(UTC)。

    结束 = 现在,开始 = 现在 - days;days 夹在 1..730(官方上限两年)。
    ⚠ 格式带毫秒:官方参考页写 `YYYY-MM-DDTHH:mm:ssZ`,照它传沃尔玛回 400
    「Date parse exception - Text '2025-09-07T14:52:02Z' could not be parsed at index 19」
    (2026-09-07 22:52 C021 实证,第 19 位就是 Z 的位置,解析器要 `.SSS`);
    ITEM_PERFORMANCE 指南的 cURL 示例用的正是 `2024-08-10T20:11:24.000Z`。
    """
    days = max(1, min(int(days), 730))
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return start.strftime(fmt), end.strftime(fmt)


# ── 日期列分布(探针)────────────────────────────────────────────────────────

DATE_COLUMNS = ("Item Creation Date", "Item Last Updated")
_DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                 "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y")


def _parse_date(raw: str) -> datetime | None:
    s = str(raw or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def date_span(rows: list[dict], column: str) -> dict:
    """输入:报表行 + 日期列名(模糊匹配)→ 输出:{min, max, parsed, unparsed, by_year, sample}。

    探针用:dataStartTime/dataEndTime 按哪个日期列筛官方没写 —— 哪一列的最早值贴着
    dataStartTime,就是按哪列筛;by_year 顺便给出老品分布,决定要不要把范围放到 730 天
    或分段多拿。列名找不到给 parsed=0、sample=None。
    """
    key = next((k for k in (rows[0].keys() if rows else []) if _norm(k) == _norm(column)), None)
    out = {"min": None, "max": None, "parsed": 0, "unparsed": 0, "by_year": {}, "sample": None}
    if key is None:
        return out
    lo = hi = None
    for r in rows:
        raw = r.get(key)
        if out["sample"] is None and raw:
            out["sample"] = str(raw)
        dt = _parse_date(raw)
        if dt is None:
            out["unparsed"] += 1
            continue
        out["parsed"] += 1
        out["by_year"][dt.year] = out["by_year"].get(dt.year, 0) + 1
        lo = dt if lo is None or dt < lo else lo
        hi = dt if hi is None or dt > hi else hi
    if lo is not None:
        out["min"], out["max"] = lo.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d")
        out["by_year"] = dict(sorted(out["by_year"].items()))
    return out


# ── 对账明细(探针)──────────────────────────────────────────────────────────

_RECON_SAMPLE = 8


def reconcile_breakdown(catalog_rows: list[dict], report_rows: list[dict]) -> dict:
    """输入:在架行画像(walmart_catalog.in_catalog_profile)+ 报表行 → 输出:对账明细 dict。

    matched_by_status {"lifecycle/published": n}:报表覆盖到的在架行是什么;
    unmatched_by_status 同款分组 + unmatched_sample [sku…]:在架却不在报表里的行是什么;
    extra_by_status {"报表 Lifecycle/Publish": n}、extra_sample:报表有、在架名单没有的行。
    (不按 created_at 分年:那是本库首次入库时间不是沃尔玛上架时间,全是 2026 没信息量。)
    所有者 2026-09-07:覆盖率缺口是老品掉出数据范围、还是 catalog_sync 名单里的僵尸 /
    RETIRED 存档(08-28 起 GET /v3/items 会把删除后的存档也列出来),拿两边名单对一下
    就知道,不猜 —— 这一步就是「全量靠对账不靠参数」的对账本身。
    """
    report_by_sku: dict[str, dict] = {}
    for r in report_rows:
        sku = reports.report_row_sku(r)
        if sku:
            report_by_sku.setdefault(sku, r)
    catalog_skus = {r["sku"] for r in catalog_rows}
    matched = [r for r in catalog_rows if r["sku"] in report_by_sku]
    unmatched = [r for r in catalog_rows if r["sku"] not in report_by_sku]
    extra = [r for sku, r in report_by_sku.items() if sku not in catalog_skus]

    def by_status(rows):
        return dict(Counter(f"{r.get('lifecycle_status') or '?'}/{r.get('published_status') or '?'}"
                            for r in rows).most_common())

    return {
        "matched": len(matched),
        "matched_by_status": by_status(matched),
        "unmatched": len(unmatched),
        "unmatched_by_status": by_status(unmatched),
        "unmatched_sample": [r["sku"] for r in unmatched[:_RECON_SAMPLE]],
        "extra": len(extra),
        "extra_by_status": dict(Counter(
            f"{r.get('Lifecycle Status') or '?'}/{r.get('Publish Status') or '?'}" for r in extra
        ).most_common()),
        "extra_sample": [reports.report_row_sku(r) for r in extra[:_RECON_SAMPLE]],
    }


# ── 表头守门 ────────────────────────────────────────────────────────────────

def _norm(col: str) -> str:
    return str(col or "").strip().lower().replace("_", " ")


def expected_header() -> list[str]:
    """输入:无 → 输出:实测表头(specs 原件,一列一行;空行忽略)。"""
    text = paths.item_report_header_file().read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def check_header(header: list[str]) -> tuple[str, str]:
    """输入:报表表头 → 输出:(拦截原因, 漂移提示)。

    三个关键列(SKU / Item ID / Item Page URL)缺一即拦 —— 拦下来当轮不写库、点名;
    其余列的增减只报不拦(沃尔玛加列是常态,拦了就是"新增一列导致 item_id 停摆")。
    """
    have = {_norm(c) for c in header}
    missing = [c for c in REQUIRED_COLUMNS if _norm(c) not in have]
    if missing:
        return (f"报表表头缺关键列 {missing}(实际 {len(header)} 列,前几列:"
                f"{header[:6]});不写库,更新解析或 specs 原件后再跑", "")
    exp = expected_header()
    exp_norm = {_norm(c) for c in exp}
    extra = [c for c in header if _norm(c) not in exp_norm]
    gone = [c for c in exp if _norm(c) not in have]
    if not extra and not gone:
        return "", ""
    return "", (f"表头与 specs 原件漂移:新增 {extra} / 消失 {gone}"
                f"(三个关键列都在,照常解析;核对后更新 "
                f"refdata/specs/item_report_header.txt)")


# ── 报表 → 映射 ──────────────────────────────────────────────────────────────

def map_item_ids(rows: list[dict]) -> tuple[dict[str, str], dict]:
    """输入:报表行 → 输出:({sku: item_id}, 计数)。纯函数,可测。

    · 「Item ID」列与「Item Page URL」尾段都有且**不一致** → 该行不写(url_mismatch);
    · 两者都空 → no_id(多半是还没 published 的新品,下轮再来);
    · 同一 SKU 在报表里出现多次且 item_id 不同 → 整个 SKU 不写(dup_conflict);
    · 没有 SKU 的行 → no_sku。
    """
    mapping: dict[str, str] = {}
    conflicts: set[str] = set()
    n = {"total": len(rows), "no_sku": 0, "no_id": 0,
         "url_mismatch": 0, "dup_conflict": 0}
    for row in rows:
        sku = reports.report_row_sku(row)
        if not sku:
            n["no_sku"] += 1
            continue
        col = reports.item_id_from_column(row)
        url = reports.item_id_from_url(row)
        if col and url and col != url:
            n["url_mismatch"] += 1
            continue
        iid = col or url
        if not iid:
            n["no_id"] += 1
            continue
        if sku in mapping and mapping[sku] != iid:
            conflicts.add(sku)
            continue
        mapping[sku] = iid
    for sku in conflicts:
        mapping.pop(sku, None)
    n["dup_conflict"] = len(conflicts)
    return mapping, n


def plan_updates(current: dict[str, str | None],
                 mapping: dict[str, str]) -> tuple[dict[str, str], dict]:
    """输入:{在架 sku: 现 item_id 或 None} × {报表 sku: item_id} → 输出:(要写的 {sku: item_id}, 计数)。

    matched   报表 ∩ 在架
    filled    现值 NULL → 写
    overwritten 现值 ≠ 报表 → 写(报表为准,所有者定稿 2026-09-07)
    unchanged 现值 = 报表
    unmatched 在架且 NULL、报表里没有(还没 published / 报表不全)
    extra     报表里有、在架里没有(已缺席/退役档,不写)
    """
    updates: dict[str, str] = {}
    n = {"catalog": len(current), "matched": 0, "filled": 0,
         "overwritten": 0, "unchanged": 0, "unmatched": 0, "extra": 0}
    for sku, iid in mapping.items():
        if sku not in current:
            n["extra"] += 1
            continue
        n["matched"] += 1
        cur = current[sku]
        if cur is None:
            n["filled"] += 1
            updates[sku] = iid
        elif str(cur) != iid:
            n["overwritten"] += 1
            updates[sku] = iid
        else:
            n["unchanged"] += 1
    n["unmatched"] = sum(1 for s, v in current.items() if v is None and s not in mapping)
    return updates, n


def coverage_note(counters: dict) -> str:
    """输入:plan_updates 的计数 → 输出:覆盖率不达标的提示(达标或样本太小给空串)。"""
    cat, matched = counters.get("catalog", 0), counters.get("matched", 0)
    if cat < COVERAGE_MIN_ROWS:
        return ""
    ratio = matched / cat
    if ratio >= COVERAGE_WARN_RATIO:
        return ""
    return (f"⚠ 疑似不全:报表只覆盖在架行 {matched}/{cat}({ratio:.0%}),"
            f"本轮只填已匹配的,明天再拿一份")


# ── 等报表就绪 ───────────────────────────────────────────────────────────────

def wait_ready(store: dict, request_id: str, *, wait_min: int = DEFAULT_WAIT_MIN,
               poll_secs: int = DEFAULT_POLL_SECS, since: str | None = None,
               list_fn=None, status_fn=None, sleep=time.sleep,
               clock=time.monotonic) -> tuple[str, int]:
    """输入:店铺 + requestId(+ 等待上限/间隔)→ 输出:("READY"|"ERROR"|"TIMEOUT", 轮询次数)。

    先睡后查(报表至少要几分钟);每轮用列表**生成器**按 requestId 找自己那一份,
    找到即 break、后面的页不再请求(列表与单查共用 18/hour 桶,一次轮询通常一枚令牌);
    列表里找不到(分页/索引滞后)时每第 STATUS_FALLBACK_EVERY 轮用一次单查兜底。
    超时不是失败:requestId 还在台账,下轮接着等(官方保留 30 天)。
    ⚠ `since` 缺省 **不传**(2026-09-07 生产实见:按官方参考页格式
    `YYYY-MM-DDTHH:mm:ssZ` 传 requestSubmissionStartDate 也回 400;列表只有 30 天、
    每店每天一份,不筛也就几十条,按 requestId 匹配足够)。
    """
    list_fn = list_fn or reports.iter_report_requests
    status_fn = status_fn or reports.get_report_request
    deadline = clock() + wait_min * 60
    polls = 0
    while True:
        sleep(poll_secs)
        polls += 1
        found, scanned = None, 0
        for r in list_fn(store, REPORT_TYPE, since=since):
            scanned += 1
            if str(r.get("requestId") or "") == request_id:
                found = r
                break                           # 生成器:后面的页不再请求
        if found is None and polls % STATUS_FALLBACK_EVERY == 0:
            found = status_fn(store, request_id)
        state = str((found or {}).get("requestStatus") or "").upper()
        logger.info("店铺 %s 轮询 #%d:requestId %s %s(列表扫了 %d 条)",
                    store.get("name"), polls, request_id, state or "未见", scanned)
        if state in reports.REPORT_STATUS_TERMINAL:
            return state, polls
        if clock() >= deadline:
            return "TIMEOUT", polls


# ── 台账 ops.report_requests ─────────────────────────────────────────────────

_SQL_OPEN = """
SELECT id, request_id, status, submitted_at, created_at
FROM ops.report_requests
WHERE store = %s AND report_type = %s
  AND status = ANY(%s::text[])
  AND created_at > now() - make_interval(days => %s)
ORDER BY created_at DESC LIMIT 1
"""


def open_request(conn, store: str, report_type: str = REPORT_TYPE) -> dict | None:
    """输入:连接 + 店铺 → 输出:本店最近一条未落定台账行(dict)或 None。"""
    with conn.cursor() as cur:
        cur.execute(_SQL_OPEN, (store, report_type, list(IN_FLIGHT_STATUSES),
                                RETENTION_DAYS))
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "request_id": row[1], "status": row[2],
            "submitted_at": row[3], "created_at": row[4]}


def expire_stale(conn, store: str, report_type: str = REPORT_TYPE) -> int:
    """输入:连接 + 店铺 → 输出:本店转成 error 的陈旧行数(孤儿 pending / 超 30 天)。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.report_requests SET status = 'error', updated_at = now(), "
            "note = %s WHERE store = %s AND report_type = %s AND status = 'pending' "
            "AND request_id IS NULL AND created_at < now() - make_interval(mins => %s)",
            (f"orphan: pending 无 requestId 超 {ORPHAN_PENDING_MIN} 分钟(POST 前后崩溃)",
             store, report_type, ORPHAN_PENDING_MIN))
        n = cur.rowcount or 0
        cur.execute(
            "UPDATE ops.report_requests SET status = 'error', updated_at = now(), "
            "note = %s WHERE store = %s AND report_type = %s "
            "AND status = ANY(%s::text[]) "
            "AND created_at < now() - make_interval(days => %s)",
            (f"expired: 超 {RETENTION_DAYS} 天,沃尔玛已不保留", store, report_type,
             list(IN_FLIGHT_STATUSES), RETENTION_DAYS))
        n += cur.rowcount or 0
    return max(n, 0)


def record_pending(conn, store: str, report_type: str, report_version: str,
                   workflow: str, note: str = "") -> int:
    """输入:连接 + 店铺 + 报表类型/版本 + 发起工作流(+ 备注:请求形状,如数据范围)
    → 输出:新台账行 id(status=pending)。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.report_requests (store, report_type, report_version, "
            "status, workflow, note) VALUES (%s, %s, %s, 'pending', %s, %s) RETURNING id",
            (store, report_type, report_version, workflow, note[:500] or None))
        return cur.fetchone()[0]


def mark_submitted(conn, row_id: int, request_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE ops.report_requests SET request_id = %s, status = 'submitted', "
                    "submitted_at = now(), updated_at = now() WHERE id = %s",
                    (request_id, row_id))


def mark_ready(conn, row_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE ops.report_requests SET status = 'ready', ready_at = now(), "
                    "updated_at = now() WHERE id = %s", (row_id,))


def mark_downloaded(conn, row_id: int, rows_total: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE ops.report_requests SET downloaded_at = now(), rows_total = %s, "
                    "updated_at = now() WHERE id = %s", (rows_total, row_id))


def mark_applied(conn, row_id: int, counters: dict, note: str = "") -> None:
    """输入:连接 + 台账行 + map/plan 计数 → 输出:无(status=applied,终态)。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.report_requests SET status = 'applied', applied_at = now(), "
            "rows_matched = %s, rows_filled = %s, rows_overwritten = %s, "
            "rows_unmatched = %s, rows_no_id = %s, "
            "note = concat_ws('; ', nullif(note, ''), nullif(%s, '')), updated_at = now() "
            "WHERE id = %s",
            (counters.get("matched", 0), counters.get("filled", 0),
             counters.get("overwritten", 0), counters.get("unmatched", 0),
             counters.get("no_id", 0), note or "", row_id))


def mark_error(conn, row_id: int, note: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE ops.report_requests SET status = 'error', note = %s, "
                    "updated_at = now() WHERE id = %s", (note[:500], row_id))
