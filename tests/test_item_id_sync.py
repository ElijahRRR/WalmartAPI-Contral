"""item_id_sync 回归(所有者定稿 2026-09-07:独立工作流补 item_id;不复用后台报表;
冲突以报表为准;05:00 调度)。

钉的全是"错了也不报错"的那类:表头改名后静默写空、两列不一致照写、库里已有值被
静默跳过、轮询打到 20/hour 的单查桶、崩溃后重建报表再吃一次创建额度、--dry-run 真建
了报表、catalog_sync 里那条旧接线又长回来。
"""

import contextlib
import inspect
import io
import pathlib
import zipfile

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
    """先睡后查;只用列表接口找自己的 requestId,不动单查兜底(两者同桶,单查纯属多花令牌)。"""
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

    def record_pending(conn, store, rt, ver, workflow, note=""):
        log["pending"] += 1
        log["pending_note"] = note
        return 7
    monkeypatch.setattr(ir, "record_pending", record_pending)
    monkeypatch.setattr(ir, "mark_submitted", lambda conn, rid, req: log["submitted"].append((rid, req)))
    monkeypatch.setattr(ir, "mark_ready", lambda conn, rid: log.__setitem__("ready", log["ready"] + 1))
    monkeypatch.setattr(ir, "mark_downloaded", lambda conn, rid, n: log["downloaded"].append((rid, n)))
    monkeypatch.setattr(ir, "mark_applied", lambda conn, rid, c, note="": log["applied"].append((rid, dict(c), note)))
    monkeypatch.setattr(ir, "mark_error", lambda conn, rid, note: log["error"].append((rid, note)))

    def create_fn(store, rt, ver, body=None, **kw):
        log["create"] += 1
        log["create_kw"] = kw
        if isinstance(create, Exception):
            raise create
        return {"requestId": "REQ-NEW", "requestStatus": "RECEIVED"}
    monkeypatch.setattr(wf.reports, "create_report_request", create_fn)
    monkeypatch.setattr(ir, "wait_ready", lambda store, rid, **kw: wait)
    monkeypatch.setattr(wf.reports, "get_download_url", lambda store, rid: ("http://dl", None))
    monkeypatch.setattr(wf.reports, "download_report", lambda url, proxy: b"blob")
    monkeypatch.setattr(wf.reports, "parse_report_csv", lambda blob: list(rows or []))
    monkeypatch.setattr(wf.walmart_catalog, "item_id_map", lambda conn, s: dict(current or {}))
    monkeypatch.setattr(wf.walmart_catalog, "in_catalog_profile",
                        lambda conn, s: [{"sku": k, "item_id": v, "lifecycle_status": "ACTIVE",
                                          "published_status": "PUBLISHED", "first_seen": None}
                                         for k, v in (current or {}).items()])
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
    # 原件留存(conftest 已把 reports_dir 指到临时目录)+ 体检 dict
    assert r["blob"]["bytes"] == 4 and r["blob"]["member"] is None
    assert pathlib.Path(r["blob"]["dump"]).read_bytes() == b"blob"
    assert any("原件 4 字节" in ln for ln in wf._probe_lines(r))


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
    assert "reports.list" not in b and "reports.status" not in b
    # 列表+单查共用:官方表列表 200/min,生产不是(2026-09-07 连打 4 次即 429、下枚令牌 142s ⇒ 小时级桶)
    assert b["reports.query"] == (18, 3600.0)
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


# ── 2026-09-07 首次生产探针暴露的两条(415 无 body;补试在创建桶里睡一小时)──────

def test_create_sends_empty_json_object_and_query_params(monkeypatch):
    """不带 body 沃尔玛回 415(要求 Content-Type: application/json):缺省发 `{}`。"""
    seen = {}
    monkeypatch.setattr(_client, "rate_try_acquire", lambda b, c: True)
    monkeypatch.setattr(_client, "get_token", lambda *a: "tok")

    def post(url, token, cid, proxy, json_body=None, params=None, timeout=30, max_retries=0):
        seen.update(json_body=json_body, params=params, max_retries=max_retries)
        return 200, {}, {"requestId": "R-1", "requestStatus": "RECEIVED"}
    monkeypatch.setattr(_client, "safe_post_ex", post)
    data = reports.create_report_request(STORE, "ITEM", "v6")
    assert data["requestId"] == "R-1"
    assert seen["json_body"] == {} and seen["max_retries"] == 0
    assert seen["params"] == {"reportType": "ITEM", "reportVersion": "v6"}


def test_create_does_not_sleep_on_a_full_bucket(monkeypatch):
    """创建桶一小时一枚:没令牌立刻 ReportQuotaError,**不**进 rate_acquire 睡等。"""
    monkeypatch.setattr(_client, "rate_try_acquire", lambda b, c: False)

    def boom(*a, **k):
        raise AssertionError("桶满时不该发 POST")
    monkeypatch.setattr(_client, "safe_post_ex", boom)
    with pytest.raises(reports.ReportQuotaError):
        reports.create_report_request(STORE, "ITEM", "v6")


@pytest.mark.parametrize("status,exc", [
    (415, reports.ReportRequestError),    # 请求形状被拒:沃尔玛照样计数(22:52 实证 400 后令牌 0)
    (400, reports.ReportRequestError),
    (429, reports.ReportQuotaError),      # 沃尔玛计了数
    (503, reports.ReportRequestError),    # 不知道有没有处理
    (None, reports.ReportRequestError),   # 网络未达
])
def test_create_never_gives_the_token_back(monkeypatch, status, exc):
    """任何结局都不还令牌:本地比沃尔玛宽,下一枚必 429(rate_release 连同前提一起撤销)。"""
    _client._rate_state.pop((STORE["client_id"], "reports.create"), None)
    monkeypatch.setattr(_client, "get_token", lambda *a: "tok")
    monkeypatch.setattr(_client, "safe_post_ex", lambda *a, **k: (status, {}, None))
    with pytest.raises(exc) as ei:
        reports.create_report_request(STORE, "ITEM", "v6")
    if exc is reports.ReportRequestError:
        assert ei.value.status == status
    assert _client.rate_try_acquire("reports.create", STORE["client_id"]) is False
    assert not hasattr(_client, "rate_release")


def test_create_401_is_store_dead(monkeypatch):
    monkeypatch.setattr(_client, "rate_try_acquire", lambda b, c: True)
    monkeypatch.setattr(_client, "get_token", lambda *a: "tok")
    monkeypatch.setattr(_client, "safe_post_ex", lambda *a, **k: (401, {}, None))
    with pytest.raises(_client.StoreDeadError):
        reports.create_report_request(STORE, "ITEM", "v6")


def test_try_acquire_in_memory_bucket():
    """有就占、没有立刻 False(不睡)。"""
    _client._rate_state.pop(("cidT", "reports.create"), None)
    assert _client.rate_try_acquire("reports.create", "cidT") is True
    assert _client.rate_try_acquire("reports.create", "cidT") is False   # 1/hour,第二枚没有
    with pytest.raises(KeyError):
        _client.rate_try_acquire("reports.NOT_REGISTERED", "cidT")


def test_one_store_deterministic_4xx_is_a_round_outcome(monkeypatch):
    """415/400 这类确定性拒绝:记台账、报 error、**不抛**(抛了就是补试再被拒一次)。"""
    log = _wire(monkeypatch, create=reports.ReportRequestError("415 媒体类型", 415))
    r = wf._one_store(STORE, 60, 120, probe=False)
    assert r["outcome"] == "error" and "415" in r["note"]
    assert log["error"][0][1].startswith("create: 415")


def test_one_store_5xx_still_goes_to_serial_retry(monkeypatch):
    log = _wire(monkeypatch, create=reports.ReportRequestError("503", 503))
    with pytest.raises(reports.ReportRequestError):
        wf._one_store(STORE, 60, 120, probe=False)
    assert log["error"][0][1].startswith("create: 503")


# ── 2026-09-07 探针第二轮:列表接口带 requestSubmissionStartDate 回 400 ────────────

def test_iter_report_requests_sends_only_report_type_by_default(monkeypatch):
    """按官方参考页格式传 requestSubmissionStartDate 也回 400:缺省只传 reportType。"""
    seen = {}
    monkeypatch.setattr(_client, "rate_acquire", lambda b, c: 0.0)
    monkeypatch.setattr(_client, "get_token", lambda *a: "tok")

    def get(url, token, cid, proxy, params=None, timeout=30, max_retries=0):
        seen["params"] = dict(params)
        return 200, {}, {"requests": [{"requestId": "R1", "requestStatus": "READY"}]}
    monkeypatch.setattr(_client, "safe_get_ex", get)
    rows = list(reports.iter_report_requests(STORE, "ITEM"))
    assert rows[0]["requestId"] == "R1"
    assert seen["params"] == {"reportType": "ITEM"}


def test_wait_ready_is_called_without_since(monkeypatch):
    seen = {}
    log = _wire(monkeypatch, rows=_ROWS, current={"A": None})

    def wait(store, rid, **kw):
        seen.update(kw)
        return "READY", 1
    monkeypatch.setattr(ir, "wait_ready", wait)
    wf._one_store(STORE, 60, 120, probe=False)
    assert "since" not in seen and log["written"] == {"A": "11"}
    assert not hasattr(ir, "since_iso")


# ── 2026-09-07 探针第三轮:翻页把 nextCursor 当参数传 ⇒ 原样回第一页、连打到 429 ────

def _list_wire(monkeypatch, pages):
    """pages:按顺序返回的响应 dict;记录每次 rate_acquire 的桶与每次 GET 的 url/params。"""
    calls = []
    monkeypatch.setattr(_client, "rate_acquire", lambda b, c: calls.append(("acquire", b)) or 0.0)
    monkeypatch.setattr(_client, "get_token", lambda *a: "tok")
    it = iter(pages)

    def get(url, token, cid, proxy, params=None, timeout=30, max_retries=0):
        calls.append(("get", url, dict(params or {})))
        return 200, {"x-current-token-count": "5", "x-next-replenishment-time": "t"}, next(it)
    monkeypatch.setattr(_client, "safe_get_ex", get)
    return calls


def test_iter_report_requests_appends_cursor_to_url_not_as_param(monkeypatch):
    """官方参考页:nextCursor 是完整 query 串,「use nextCursor value instead of query params」
    —— 直接拼 URL;当参数传会被忽略、原样回第一页(2026-09-07 生产实见)。"""
    calls = _list_wire(monkeypatch, [
        {"requests": [{"requestId": "R1"}], "nextCursor": "reportType=ITEM&page=2&limit=10"},
        {"requests": [{"requestId": "R2"}]},
    ])
    rows = list(reports.iter_report_requests(STORE, "ITEM"))
    assert [r["requestId"] for r in rows] == ["R1", "R2"]
    gets = [c for c in calls if c[0] == "get"]
    assert gets[0][2] == {"reportType": "ITEM"}
    assert gets[1][1].endswith("/v3/reports/reportRequests?reportType=ITEM&page=2&limit=10")
    assert gets[1][2] == {}                                   # 第二页不再带 params
    assert [c[1] for c in calls if c[0] == "acquire"] == ["reports.query"] * 2


def test_iter_report_requests_stops_on_repeated_cursor(monkeypatch):
    """同 cursor 重复 = 服务端未推进(实见连回三次第四次 429):立即停,不打满 max_pages。"""
    same = {"requests": [{"requestId": "R1"}], "nextCursor": "reportType=ITEM&page=2&limit=10"}
    calls = _list_wire(monkeypatch, [same] * 5)
    rows = list(reports.iter_report_requests(STORE, "ITEM", max_pages=5))
    assert len([c for c in calls if c[0] == "get"]) == 2
    assert len(rows) == 2


def test_wait_ready_stops_paging_once_request_found(monkeypatch):
    """生成器:找到自己那条就 break,后面的页不再请求(一次轮询通常只花一枚令牌)。"""
    calls = _list_wire(monkeypatch, [
        {"requests": [{"requestId": "R1", "requestStatus": "READY"}],
         "nextCursor": "reportType=ITEM&page=2&limit=10"},
        {"requests": [{"requestId": "R0", "requestStatus": "READY"}]},
    ])
    t, sleep = _clock()
    st, polls = ir.wait_ready(STORE, "R1", wait_min=60, poll_secs=300,
                              status_fn=lambda *a: {}, sleep=sleep, clock=lambda: t[0])
    assert (st, polls) == ("READY", 1)
    assert len([c for c in calls if c[0] == "get"]) == 1


def test_polling_budget_fits_the_shared_hourly_bucket():
    """60 分钟 / 5 分钟 = 12 次列表 + 每第 5 次的兜底单查 ≤ 2 次,同一 18/hour 桶装得下。"""
    limit, window = _client._RATE_BUCKETS["reports.query"]
    polls = ir.DEFAULT_WAIT_MIN * 60 // ir.DEFAULT_POLL_SECS
    assert window == 3600.0 and ir.DEFAULT_POLL_SECS == 300
    assert polls + polls // ir.STATUS_FALLBACK_EVERY <= limit


def test_reports_calls_log_walmart_quota_headers(monkeypatch, caplog):
    """报表族每次响应的 x-current-token-count / x-next-replenishment-time 进日志:真实桶只有它能证明。"""
    _list_wire(monkeypatch, [{"requests": []}])
    with caplog.at_level("INFO", logger="api.reports"):
        list(reports.iter_report_requests(STORE, "ITEM"))
    assert any("令牌 5" in r.getMessage() and "下枚 t" in r.getMessage() for r in caplog.records)


# ── 2026-09-07 探针第四轮:55 列对上、在架 1490 行却只解析出 1 行 ────────────────────

def _zip_csv(text: str, name: str = "ItemReport.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()


def test_report_blob_info_distinguishes_short_file_from_swallowed_rows():
    """体检要能分清「沃尔玛只给了 1 行」和「引号没闭合把后面全吞进一个字段」。"""
    good = _zip_csv("SKU,Item ID,Item Page URL\nA,1,u\nB,2,u\nC,3,u\n")
    info = reports.report_blob_info(good)
    assert (info["rows"], info["lines"], info["member"]) == (3, 4, "ItemReport.csv")
    assert info["members"] == [("ItemReport.csv", info["csv_bytes"])]
    assert reports.parse_report_csv(good)[2]["SKU"] == "C"

    swallowed = _zip_csv("SKU,Item ID,Name\nA,1,\"oops\nB,2,x\nC,3,y\n")
    info = reports.report_blob_info(swallowed)
    assert info["rows"] == 1 and info["lines"] == 4          # 换行 4 个却只 1 行
    assert info["longest_field"] > len("oops")                # 吞进去的都在这个字段里
    lines = wf._probe_lines({"store": "S", "request_id": "R", "header": ["SKU"], "note": "",
                             "publish": {}, "lifecycle": {}, "sample": [], "counters": {},
                             "blob": {**info, "dump": "/tmp/x.zip"}})
    assert any("引号没闭合" in ln for ln in lines)

    raw = b"SKU,Item ID\nA,1\n"                                 # 裸 CSV 也走同一条路
    assert reports.report_blob_info(raw)["members"] == [] and reports.parse_report_csv(raw)[0]["SKU"] == "A"


# ── 2026-09-07 22:05 后决定:不带日期只回 1 行 ⇒ 请求体带近一年数据范围;renew 作废在途行 ──

def test_data_window_is_official_format_and_capped_at_730_days():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 7, 14, 5, 7, 123456, tzinfo=timezone.utc)
    # 带毫秒:不带毫秒沃尔玛回 400「could not be parsed at index 19」(22:52 实证)
    assert ir.data_window(365, now) == ("2025-09-07T14:05:07.000Z", "2026-09-07T14:05:07.000Z")
    assert ir.data_window(9999, now)[0] == "2024-09-07T14:05:07.000Z"     # 夹到 730
    assert ir.DATA_RANGE_DAYS == 365


def test_create_puts_data_window_in_json_body(monkeypatch):
    """dataStartTime/dataEndTime 是 body 字段(官方 payload 回显 + ITEM_PERFORMANCE 指南示例),不进 query。"""
    seen = {}
    monkeypatch.setattr(_client, "rate_try_acquire", lambda b, c: True)
    monkeypatch.setattr(_client, "get_token", lambda *a: "tok")

    def post(url, token, cid, proxy, json_body=None, params=None, timeout=30, max_retries=0):
        seen.update(json_body=json_body, params=params)
        return 200, {}, {"requestId": "R-1", "requestStatus": "RECEIVED"}
    monkeypatch.setattr(_client, "safe_post_ex", post)
    reports.create_report_request(STORE, "ITEM", "v6", data_start="2025-09-07T00:00:00.000Z",
                                  data_end="2026-09-07T00:00:00.000Z")
    assert seen["json_body"] == {"dataStartTime": "2025-09-07T00:00:00.000Z",
                                 "dataEndTime": "2026-09-07T00:00:00.000Z"}
    assert seen["params"] == {"reportType": "ITEM", "reportVersion": "v6"}
    reports.create_report_request(STORE, "ITEM", "v6")
    assert seen["json_body"] == {}                        # 不给日期仍是 {},不是 None


def test_one_store_creates_with_window_and_records_it_in_ledger(monkeypatch):
    log = _wire(monkeypatch, rows=_ROWS, current={"A": None})
    r = wf._one_store(STORE, 60, 300, probe=False, data_days=365)
    kw = log["create_kw"]
    assert set(kw) == {"data_start", "data_end"} and kw["data_end"].endswith("Z")
    assert log["pending_note"] == f"dataStartTime={kw['data_start']} dataEndTime={kw['data_end']}"
    assert r["window"] == (kw["data_start"], kw["data_end"])


def test_renew_supersedes_in_flight_row_and_creates_again(monkeypatch):
    """改了请求形状:在途行(哪怕 ready)记 error 作废,重新 POST;不带 renew 照旧接着用。"""
    open_row = {"id": 3, "request_id": "REQ-OLD", "status": "ready",
                "submitted_at": None, "created_at": None}
    log = _wire(monkeypatch, open_row=open_row, rows=_ROWS, current={"A": None})
    r = wf._one_store(STORE, 60, 300, probe=True, renew=True)
    assert log["error"] == [(3, "superseded: renew=1,原 requestId=REQ-OLD")]
    assert log["create"] == 1 and log["pending"] == 1 and r["request_id"] == "REQ-NEW"
    assert any("数据范围" in ln for ln in wf._probe_lines(r))
    log = _wire(monkeypatch, open_row=open_row, rows=_ROWS, current={"A": None})
    wf._one_store(STORE, 60, 300, probe=True)
    assert log["create"] == 0 and log["error"] == []


# ── 2026-09-07 探针第六轮(A109):近一年范围拿到 3359 行、覆盖 49% ⇒ 要看范围按哪列筛 ──

def test_date_span_parses_mixed_formats_and_buckets_by_year():
    rows = [_row(**{"Item Creation Date": "2024-03-05T10:00:00.000Z", "Item Last Updated": "09/01/2026"}),
            _row(**{"Item Creation Date": "2025-12-31", "Item Last Updated": "2026-09-07T01:02:03Z"}),
            _row(**{"Item Creation Date": "n/a", "Item Last Updated": ""})]
    d = ir.date_span(rows, "Item Creation Date")
    assert (d["min"], d["max"], d["parsed"], d["unparsed"]) == ("2024-03-05", "2025-12-31", 2, 1)
    assert d["by_year"] == {2024: 1, 2025: 1} and d["sample"] == "2024-03-05T10:00:00.000Z"
    d = ir.date_span(rows, "item last updated")               # 列名模糊匹配
    assert (d["min"], d["max"]) == ("2026-09-01", "2026-09-07")
    assert ir.date_span(rows, "No Such Column")["parsed"] == 0
    assert ir.date_span([], "Item Creation Date")["sample"] is None


def test_probe_prints_date_spans(monkeypatch):
    rows = [dict(r, **{"Item Creation Date": "2025-11-01", "Item Last Updated": "2026-09-01"}) for r in _ROWS]
    _wire(monkeypatch, rows=rows, current={"A": None})
    r = wf._one_store(STORE, 60, 300, probe=True)
    assert r["dates"]["Item Creation Date"]["min"] == "2025-11-01"
    lines = wf._probe_lines(r)
    assert any(ln.startswith("  Item Creation Date:最早 2025-11-01") for ln in lines)
    assert any(ln.startswith("  Item Last Updated:最早 2026-09-01") for ln in lines)


# ── 所有者 2026-09-07:覆盖率缺口不猜,拿报表 SKU × catalog_sync 名单对账 ───────────

def test_reconcile_breakdown_groups_both_sides():
    from datetime import datetime
    catalog = [
        {"sku": "A", "lifecycle_status": "ACTIVE", "published_status": "PUBLISHED", "first_seen": datetime(2024, 5, 1)},
        {"sku": "B", "lifecycle_status": "RETIRED", "published_status": "UNPUBLISHED", "first_seen": datetime(2023, 1, 1)},
        {"sku": "C", "lifecycle_status": "RETIRED", "published_status": "UNPUBLISHED", "first_seen": None},
        {"sku": "D", "lifecycle_status": None, "published_status": "PUBLISHED", "first_seen": datetime(2026, 2, 2)},
    ]
    report = [_row(SKU="A", **{"Lifecycle Status": "ACTIVE", "Publish Status": "PUBLISHED"}),
              _row(SKU="Z", **{"Lifecycle Status": "ACTIVE", "Publish Status": "UNPUBLISHED"}),
              _row(SKU="Z", **{"Lifecycle Status": "ACTIVE", "Publish Status": "UNPUBLISHED"}),   # 重复 SKU 只算一次
              _row(**{"Item ID": "9"})]                                                          # 无 SKU 忽略
    rc = ir.reconcile_breakdown(catalog, report)
    assert rc["matched"] == 1 and rc["matched_by_status"] == {"ACTIVE/PUBLISHED": 1}
    assert rc["unmatched"] == 3 and rc["unmatched_sample"] == ["B", "C", "D"]
    assert rc["unmatched_by_status"] == {"RETIRED/UNPUBLISHED": 2, "?/PUBLISHED": 1}
    assert "unmatched_by_year" not in rc                     # 入库年不是上架年,不打
    assert rc["extra"] == 1 and rc["extra_by_status"] == {"ACTIVE/UNPUBLISHED": 1} and rc["extra_sample"] == ["Z"]


def test_probe_prints_reconciliation(monkeypatch):
    _wire(monkeypatch, rows=_ROWS, current={"A": None, "B": None, "X": None})
    r = wf._one_store(STORE, 60, 300, probe=True)
    assert r["recon"]["unmatched"] == 1 and r["recon"]["unmatched_sample"] == ["X"]
    assert r["recon"]["extra"] == 1 and r["recon"]["extra_sample"] == ["C"]
    lines = wf._probe_lines(r)
    assert any("对账·报表覆盖的在架行 2 行" in ln and "ACTIVE/PUBLISHED" in ln for ln in lines)
    assert any("对账·在架不在报表 1 行" in ln and "ACTIVE/PUBLISHED" in ln for ln in lines)
    assert any("对账·报表有但在架名单没有 1 行" in ln for ln in lines)
