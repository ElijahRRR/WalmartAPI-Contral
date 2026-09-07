"""item_id_sync 回归(所有者定稿 2026-09-07:独立工作流补 item_id;不复用后台报表;
冲突以报表为准;05:00 调度)。

钉的全是"错了也不报错"的那类:表头改名后静默写空、两列不一致照写、库里已有值被
静默跳过、轮询打到 20/hour 的单查桶、崩溃后重建报表再吃一次创建额度、--dry-run 真建
了报表、catalog_sync 里那条旧接线又长回来。
"""

import contextlib
import inspect

import pytest

from api import _client, reports
from services import item_reports as ir
from workflows import item_id_sync as wf

HEADER = ir.expected_header()


def _row(**kw) -> dict:
    row = {c: "" for c in HEADER}
    row.update(kw)
    return row


# ── 表头守门 ────────────────────────────────────────────────────────────────

def test_spec_header_is_the_owners_55_column_export():
    """specs 原件 = 所有者 2026-09-07 贴的后台导出表头:55 列,三个关键列都在。"""
    assert len(HEADER) == 55
    assert HEADER[0] == "SKU" and HEADER[1] == "Item ID" and "Item Page URL" in HEADER
    for c in ir.REQUIRED_COLUMNS:
        assert c in HEADER
    assert ir.check_header(HEADER) == ("", "")


def test_check_header_blocks_missing_key_column_but_only_notes_drift():
    err, _ = ir.check_header([c for c in HEADER if c != "Item ID"])
    assert "Item ID" in err and "不写库" in err
    err, drift = ir.check_header(HEADER + ["New Column"])
    assert err == "" and "New Column" in drift          # 加列只报不拦
    err, drift = ir.check_header([c for c in HEADER if c != "Brand"])
    assert err == "" and "Brand" in drift               # 少一个非关键列也只报


# ── 报表 → 映射 → 写库计划 ───────────────────────────────────────────────────

def test_map_item_ids_cross_checks_column_against_url():
    rows = [
        _row(SKU="A", **{"Item ID": "20865123946",
                         "Item Page URL": "http://www.walmart.com/ip/Circular-40/20865123946"}),
        _row(SKU="B", **{"Item Page URL": "http://www.walmart.com/ip/y/111"}),   # 只有 URL:兜底
        _row(SKU="C", **{"Item ID": "5", "Item Page URL": "http://www.walmart.com/ip/z/6"}),  # 不一致
        _row(SKU="D"),                                                             # 两列都空
        _row(SKU="E", **{"Item ID": "7"}),
        _row(SKU="E", **{"Item ID": "8"}),                                         # 同 SKU 两个 ID
        _row(**{"Item ID": "9"}),                                                  # 没 SKU
    ]
    mapping, n = ir.map_item_ids(rows)
    assert mapping == {"A": "20865123946", "B": "111"}
    assert n == {"total": 7, "no_sku": 1, "no_id": 1, "url_mismatch": 1, "dup_conflict": 1}


def test_plan_updates_report_wins_over_existing_value():
    """冲突以报表为准(所有者定稿):已有值不同 → 改并计 overwritten;相同不动;NULL 填。"""
    current = {"A": None, "B": "old", "C": "same", "D": None}
    updates, n = ir.plan_updates(current, {"A": "1", "B": "2", "C": "same", "Z": "9"})
    assert updates == {"A": "1", "B": "2"}
    assert n == {"catalog": 4, "matched": 3, "filled": 1, "overwritten": 1,
                 "unchanged": 1, "unmatched": 1, "extra": 1}


def test_coverage_note_thresholds():
    assert ir.coverage_note({"catalog": 100, "matched": 94}).startswith("⚠ 疑似不全")
    assert ir.coverage_note({"catalog": 100, "matched": 95}) == ""
    assert ir.coverage_note({"catalog": 10, "matched": 1}) == ""      # 样本太小不报


def test_extract_item_id_prefers_column_then_url():
    row = _row(SKU="A", **{"Item ID": "123", "Item Page URL": "http://www.walmart.com/ip/x/456"})
    assert reports.item_id_from_column(row) == "123"
    assert reports.item_id_from_url(row) == "456"
    assert reports.extract_item_id(row) == "123"
    assert reports.item_id_from_column(_row(**{"Item ID": "n/a"})) is None


# ── 轮询节奏 ────────────────────────────────────────────────────────────────

def _clock():
    t = [0.0]

    def sleep(s):
        t[0] += s

    return t, sleep


def test_wait_ready_sleeps_first_and_polls_the_list_endpoint():
    """先睡后查;只用 200/min 的列表接口找自己的 requestId,不动 20/hour 的单查。"""
    t, sleep = _clock()
    seen = {"list": 0, "status": 0}

    def list_fn(store, rt, since=None):
        seen["list"] += 1
        st = "READY" if seen["list"] >= 3 else "INPROGRESS"
        return [{"requestId": "other", "requestStatus": "READY"},
                {"requestId": "R1", "requestStatus": st}]

    def status_fn(store, rid):
        seen["status"] += 1
        return {"requestStatus": "INPROGRESS"}

    st, polls = ir.wait_ready({"name": "S"}, "R1", wait_min=60, poll_secs=120,
                              list_fn=list_fn, status_fn=status_fn,
                              sleep=sleep, clock=lambda: t[0])
    assert (st, polls) == ("READY", 3)
    assert seen == {"list": 3, "status": 0}
    assert t[0] == 360                                   # 3 × 120s,先睡后查


def test_wait_ready_status_fallback_every_fifth_poll_then_timeout():
    t, sleep = _clock()
    seen = {"status": 0}

    def status_fn(store, rid):
        seen["status"] += 1
        return {"requestStatus": "RECEIVED"}

    st, polls = ir.wait_ready({"name": "S"}, "R1", wait_min=20, poll_secs=120,
                              list_fn=lambda *a, **k: [], status_fn=status_fn,
                              sleep=sleep, clock=lambda: t[0])
    assert (st, polls) == ("TIMEOUT", 10)                # 20 分钟 / 2 分钟
    assert seen["status"] == 2                           # 第 5、10 次才用单查


def test_wait_ready_error_is_terminal():
    t, sleep = _clock()
    st, polls = ir.wait_ready(
        {"name": "S"}, "R1", wait_min=60, poll_secs=60,
        list_fn=lambda *a, **k: [{"requestId": "R1", "requestStatus": "ERROR"}],
        status_fn=lambda *a: {}, sleep=sleep, clock=lambda: t[0])
    assert (st, polls) == ("ERROR", 1)


# ── 单店流程(台账 + 接口全打桩)────────────────────────────────────────────

STORE = {"name": "S1", "client_id": "cid", "client_secret": "sec", "proxy": None}


def _wire(monkeypatch, *, open_row=None, rows=None, current=None,
          create=None, wait=("READY", 1)):
    """把 _one_store 的四类依赖全部换成记录器:台账 / 接口 / 等待 / 写库。"""
    log: dict = {"create": 0, "pending": 0, "submitted": [], "ready": 0,
                 "downloaded": [], "applied": [], "error": [], "written": {}}
    monkeypatch.setattr(wf.db, "pg_conn", lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(ir, "expire_stale", lambda conn, store, report_type=ir.REPORT_TYPE: 0)
    monkeypatch.setattr(ir, "open_request", lambda conn, store, report_type=ir.REPORT_TYPE: open_row)

    def record_pending(conn, store, rt, ver, workflow):
        log["pending"] += 1
        return 7
    monkeypatch.setattr(ir, "record_pending", record_pending)
    monkeypatch.setattr(ir, "mark_submitted", lambda conn, rid, req: log["submitted"].append((rid, req)))
    monkeypatch.setattr(ir, "mark_ready", lambda conn, rid: log.__setitem__("ready", log["ready"] + 1))
    monkeypatch.setattr(ir, "mark_downloaded", lambda conn, rid, n: log["downloaded"].append((rid, n)))
    monkeypatch.setattr(ir, "mark_applied", lambda conn, rid, c, note="": log["applied"].append((rid, dict(c), note)))
    monkeypatch.setattr(ir, "mark_error", lambda conn, rid, note: log["error"].append((rid, note)))

    def create_fn(store, rt, ver, body=None):
        log["create"] += 1
        if isinstance(create, Exception):
            raise create
        return {"requestId": "REQ-NEW", "requestStatus": "RECEIVED"}
    monkeypatch.setattr(wf.reports, "create_report_request", create_fn)
    monkeypatch.setattr(ir, "wait_ready", lambda store, rid, **kw: wait)
    monkeypatch.setattr(wf.reports, "get_download_url", lambda store, rid: ("http://dl", None))
    monkeypatch.setattr(wf.reports, "download_report", lambda url, proxy: b"blob")
    monkeypatch.setattr(wf.reports, "parse_report_csv", lambda blob: list(rows or []))
    monkeypatch.setattr(wf.walmart_catalog, "item_id_map", lambda conn, s: dict(current or {}))
    monkeypatch.setattr(wf.walmart_catalog, "set_item_ids",
                        lambda conn, s, m: log["written"].update(m) or len(m))
    return log


_ROWS = [
    _row(SKU="A", **{"Item ID": "11", "Item Page URL": "http://www.walmart.com/ip/a/11"}),
    _row(SKU="B", **{"Item ID": "22", "Item Page URL": "http://www.walmart.com/ip/b/22"}),
    _row(SKU="C", **{"Item ID": "", "Item Page URL": ""}),
]


def test_one_store_creates_waits_downloads_and_applies(monkeypatch):
    log = _wire(monkeypatch, rows=_ROWS, current={"A": None, "B": "old", "C": None, "D": None})
    r = wf._one_store(STORE, 60, 120, probe=False)
    assert r["outcome"] == "applied"
    assert log["create"] == 1 and log["pending"] == 1
    assert log["submitted"] == [(7, "REQ-NEW")] and log["ready"] == 1
    assert log["written"] == {"A": "11", "B": "22"}          # B:报表为准,改
    assert log["applied"] and log["applied"][0][1]["overwritten"] == 1
    c = r["counters"]
    # unmatched = 在架且 NULL、报表里没给出 ID 的:C(报表里无 ID)+ D(报表里没有)
    assert (c["filled"], c["overwritten"], c["unmatched"], c["no_id"]) == (1, 1, 2, 1)


def test_one_store_resumes_ledger_row_instead_of_creating_again(monkeypatch):
    """崩溃/超时后台账里有 submitted 行 ⇒ 接着等,不再 POST(创建每小时只有一次)。"""
    open_row = {"id": 3, "request_id": "REQ-OLD", "status": "submitted",
                "submitted_at": None, "created_at": None}
    log = _wire(monkeypatch, open_row=open_row, rows=_ROWS, current={"A": None})
    r = wf._one_store(STORE, 60, 120, probe=False)
    assert r["outcome"] == "applied" and r["request_id"] == "REQ-OLD"
    assert log["create"] == 0 and log["pending"] == 0
    assert log["applied"][0][0] == 3


def test_one_store_ready_row_skips_waiting(monkeypatch):
    open_row = {"id": 4, "request_id": "REQ-R", "status": "ready",
                "submitted_at": None, "created_at": None}
    log = _wire(monkeypatch, open_row=open_row, rows=_ROWS, current={"A": None},
                wait=("TIMEOUT", 99))            # 不该被调用
    r = wf._one_store(STORE, 60, 120, probe=False)
    assert r["outcome"] == "applied" and log["ready"] == 0 and log["written"] == {"A": "11"}


def test_quota_429_is_a_round_outcome_not_an_exception(monkeypatch):
    """429 = 这小时额度没了:本轮放弃、记台账、**不抛**(抛出去会进串行补试,
    补试在持久桶里睡到下一个小时)。"""
    log = _wire(monkeypatch, create=reports.ReportQuotaError("S1 限流"))
    r = wf._one_store(STORE, 60, 120, probe=False)
    assert r["outcome"] == "quota"
    assert log["error"] and log["error"][0][1].startswith("quota:")
    assert log["written"] == {} and log["applied"] == []


def test_timeout_keeps_request_in_ledger(monkeypatch):
    log = _wire(monkeypatch, rows=_ROWS, current={"A": None}, wait=("TIMEOUT", 30))
    r = wf._one_store(STORE, 60, 120, probe=False)
    assert r["outcome"] == "timeout" and "下轮接着等" in r["note"]
    assert log["submitted"] == [(7, "REQ-NEW")] and log["error"] == []   # 行留在 submitted


def test_walmart_error_marks_ledger_and_stops(monkeypatch):
    log = _wire(monkeypatch, rows=_ROWS, current={"A": None}, wait=("ERROR", 2))
    r = wf._one_store(STORE, 60, 120, probe=False)
    assert r["outcome"] == "error" and log["error"][0][1].startswith("walmart:")
    assert log["written"] == {}


def test_missing_key_column_blocks_the_write(monkeypatch):
    bad = [{k: v for k, v in r.items() if k != "Item ID"} for r in _ROWS]
    log = _wire(monkeypatch, rows=bad, current={"A": None})
    r = wf._one_store(STORE, 60, 120, probe=False)
    assert r["outcome"] == "error" and "Item ID" in r["note"]
    assert log["written"] == {} and log["error"][0][1].startswith("header:")


def test_probe_downloads_but_never_writes(monkeypatch):
    log = _wire(monkeypatch, rows=_ROWS, current={"A": None, "B": "old"})
    r = wf._one_store(STORE, 60, 120, probe=True)
    assert r["outcome"] == "probe"
    assert log["written"] == {} and log["applied"] == []
    assert r["sample"][0] == ("A", "11", "11") and r["publish"] and "header" in r
    assert log["downloaded"] == [(7, 3)]                 # 台账停在 ready + 已下载


def test_create_failure_marks_ledger_and_raises_for_serial_retry(monkeypatch):
    log = _wire(monkeypatch, create=RuntimeError("reportRequests 创建 返回 503"))
    with pytest.raises(RuntimeError):
        wf._one_store(STORE, 60, 120, probe=False)
    assert log["error"][0][1].startswith("create:")


# ── run():候选、空跑、摘要 ──────────────────────────────────────────────────

def _wire_run(monkeypatch, *, stores, gaps, attempt=None):
    monkeypatch.setattr(wf.db, "pg_conn", lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(wf.stores_svc, "load_stores",
                        lambda names=None: [s for s in stores if not names or s["name"] in names])
    monkeypatch.setattr(wf.walmart_catalog, "stores_missing_item_id", lambda conn: dict(gaps))
    monkeypatch.setattr(ir, "open_request", lambda conn, store, report_type=ir.REPORT_TYPE: None)
    ran: list[str] = []

    def fan_out(cands, fn, workers, log_label=""):
        results = []
        for s in cands:
            ran.append(s["name"])
            results.append(fn(s) if attempt is None else attempt(s))
        return results, [], [], ""
    monkeypatch.setattr(wf.store_retry, "fan_out", fan_out)

    def boom(*a, **k):
        raise AssertionError("空跑不许创建报表")
    monkeypatch.setattr(wf.reports, "create_report_request", boom)
    return ran


_STORES = [{"name": n, "client_id": n, "client_secret": "x", "proxy": None}
           for n in ("S1", "S2", "S3")]


def test_dry_run_reports_candidates_and_creates_nothing(monkeypatch):
    _wire_run(monkeypatch, stores=_STORES, gaps={"S1": 5, "S3": 2})
    out = wf.run({"execute": True, "dry_run": True})
    assert out.startswith("🧪 [DRY-RUN] item_id_sync:候选 2/3 店(缺口 7 行)")
    assert "S1:缺口 5 行" in out and "S3:缺口 2 行" in out and "S2" not in out


def test_default_candidates_are_stores_with_gaps_and_all_overrides(monkeypatch):
    def ok(s):
        return {"store": s["name"], "outcome": "applied", "note": "",
                "counters": {"total": 3, "matched": 2, "filled": 2, "overwritten": 0,
                             "unmatched": 0, "no_id": 1, "catalog": 2}}
    ran = _wire_run(monkeypatch, stores=_STORES, gaps={"S2": 1}, attempt=ok)
    out = wf.run({"execute": True})
    assert ran == ["S2"]
    assert out.startswith("item_id_sync:1/1 店补齐,填 2,改 0(报表为准),在架未匹配 0")
    ran.clear()
    wf.run({"execute": True, "all": "1"})
    assert ran == ["S1", "S2", "S3"]


def test_no_gaps_means_no_report_request(monkeypatch):
    ran = _wire_run(monkeypatch, stores=_STORES, gaps={})
    out = wf.run({"execute": True})
    assert out.startswith("item_id_sync:无缺口") and ran == []


def test_summary_first_line_carries_quota_timeout_and_errors(monkeypatch):
    outcomes = {"S1": "quota", "S2": "timeout", "S3": "error"}

    def att(s):
        return {"store": s["name"], "outcome": outcomes[s["name"]],
                "note": f"{s['name']} 的原因", "counters": {}}
    _wire_run(monkeypatch, stores=_STORES, gaps={"S1": 1, "S2": 1, "S3": 1}, attempt=att)
    out = wf.run({"execute": True})
    head = out.splitlines()[0]
    assert "0/3 店补齐" in head and "限流放弃 1 店" in head \
        and "超时待续 1 店" in head and "⚠ 报表失败 1 店" in head
    assert "S1:限流放弃" in out and "S2:超时待续" in out and "S3:报表失败" in out


def test_probe_requires_store_and_real_run():
    assert wf.run({"execute": True, "probe": "1"}).startswith("⛔")
    assert wf.run({"execute": True, "probe": "1", "store": "S1", "dry_run": True}).startswith("⛔")


# ── 桶登记 / 旧接线不许长回来 / 调度 ───────────────────────────────────────

def test_report_buckets_follow_the_official_rate_page():
    b = _client._RATE_BUCKETS
    assert b["reports.create"] == (1, 3600.0)       # 每类型每小时一次(MX/1P 页;08-05 实测 429)
    assert b["reports.list"] == (180, 60.0)         # 官方 200/min
    assert b["reports.status"] == (18, 3600.0)      # 官方 20/hour
    assert b["reports.download"] == (18, 3600.0)    # 官方 20/hour
    assert "reports.poll" not in b and "reports.request" not in b


def test_catalog_sync_no_longer_backfills_item_id():
    """双轨禁止:item_id 只从 item_id_sync 写;catalog_sync 只剩投影与复现重置。"""
    from workflows import catalog_sync
    src = inspect.getsource(catalog_sync)
    assert "_backfill_item_ids" not in src and "item_ids" not in src
    assert not hasattr(reports, "fetch_item_report")


def test_scheduled_daily_at_0500_on_gpt_runner():
    from registry import schedule
    job = next(j for j in schedule.JOBS if j["label"] == "item_id_sync")
    assert job["workflows"] == ["item_id_sync"]
    assert (job["hour"], job["minute"], job["runner"], job["batch"]) == (5, 0, "gpt", 1)
    assert not job["params"]                         # 缺省即"按缺口";首轮 all=1 手动


def test_workflow_flags():
    assert wf.DANGEROUS is False and wf.SUPPORTS_STORE is True
