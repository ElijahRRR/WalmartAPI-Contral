"""problem_product_cleanup 回归(批次 E 拆分后:纯执行件)。

钉的是**提交与记账**这一半:滑窗对位、dedup 不落幽灵事件、单店隔离、二轮重试、
建议行转态、dry-run 零提交。决策侧(归类/路由/去重口径)的用例已随 plan()
搬到 tests/test_problem_scan.py —— 决策逻辑搬到哪,钉它的测试就跟到哪。
"""

import contextlib

import pytest

from workflows import problem_product_cleanup as ppc


def _row(store, sku, action="delete", rid=None, category="B", **detail):
    """一条 ops.dispositions 建议行(claim() 的返回形态)。"""
    return {"id": rid if rid is not None else abs(hash((store, sku, action))) % 10**6,
            "store": store, "sku": sku, "asin": None, "action": action,
            "category": category, "reason": "prohibited product policy",
            "detail": detail or {}}


def _wire(monkeypatch, rows, stores=("T1",), settled=None):
    """把 DB 侧全部换成假的:claim 给定建议行,settle 给定落定数,转态记账。"""
    from registry import db as _db
    seen = {"events": [], "marked": [], "settled": settled or
            {"confirmed": 0, "ineffective": 0}}
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    monkeypatch.setattr(ppc.dispositions, "claim", lambda conn: list(rows))
    monkeypatch.setattr(ppc.dispositions, "settle",
                        lambda conn: seen["settled"])
    monkeypatch.setattr(ppc.dispositions, "mark_executing",
                        lambda conn, ids, feed_id: (
                            seen["marked"].append((tuple(ids), feed_id)),
                            len(ids))[1])
    monkeypatch.setattr(ppc.product_events, "record_many",
                        lambda conn, rs: (seen["events"].extend(rs), len(rs))[1])
    monkeypatch.setattr(ppc.stores_svc, "load_stores",
                        lambda names=None: [{"name": s} for s in stores])
    return seen


def test_group_by_store_buckets_by_action():
    rows = [_row("T1", "S1"), _row("T1", "S2", "relist"),
            _row("T2", "S3", "retire")]
    got = ppc.group_by_store(rows)
    assert [r["sku"] for r in got["T1"]["delete"]] == ["S1"]
    assert [r["sku"] for r in got["T1"]["relist"]] == ["S2"]
    assert [r["sku"] for r in got["T2"]["retire"]] == ["S3"]


def test_group_by_store_rejects_unknown_action():
    """建议表里出现不认识的动作 → 宁炸不吞。静默丢弃会让那些行永远挂
    suggested,而部分唯一索引又挡着新建议,该 SKU 从此再也处理不了。"""
    with pytest.raises(ValueError, match="未知 action"):
        ppc.group_by_store([_row("T1", "S1", "nope")])


def test_execute_records_events_per_slice(monkeypatch):
    # 多切片提交:事件账本必须滑窗对位([:count] 写法会把第一片重复记到 FB)
    rows = [_row("T1", f"S{i}", rid=i) for i in range(4)]
    seen = _wire(monkeypatch, rows)
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda store, ft, entries, *, workflow="": [
                            {"feed_id": "FA", "count": 2, "outcome": "submitted"},
                            {"feed_id": "FB", "count": 2, "outcome": "submitted"}])
    ppc.run({"execute": True})
    sub = [(e["sku"], e["detail"]["feed_id"]) for e in seen["events"]
           if e["event"] == "delete_submitted"]
    assert sub == [("S0", "FA"), ("S1", "FA"), ("S2", "FB"), ("S3", "FB")]
    # 建议行转态同样滑窗对位:每片的 id 挂各自的 feed_id
    assert seen["marked"] == [((0, 1), "FA"), ((2, 3), "FB")]


def test_execute_skips_recording_on_dedup(monkeypatch):
    # dedup=在途防重命中,什么都没提交:不许落 *_submitted 幽灵事件
    # (反补计数被灌水会导致少一次真实反补就转永久删除),也不许转 executing
    # ——转了就会卡在那里等一个不存在的 feed 的判决,而部分唯一索引挡着新建议
    seen = _wire(monkeypatch, [_row("T1", "S0")])
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda store, ft, entries, *, workflow="": [
                            {"feed_id": "F_OLD", "count": 1, "outcome": "dedup"}])
    out = ppc.run({"execute": True})
    assert not [e for e in seen["events"] if e["event"] == "delete_submitted"]
    assert seen["marked"] == []
    assert "删除提交 0" in out and "在途防重跳过 1" in out


def test_execute_reports_rejected_and_unknown(monkeypatch):
    # 提交被拒/结局不确定必须在摘要中可见(危险工作流不许静默假成功)
    seen = _wire(monkeypatch, [_row("T1", "S0"), _row("T1", "S1")])
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda store, ft, entries, *, workflow="": [
                            {"feed_id": None, "count": 1, "outcome": "failed"},
                            {"feed_id": None, "count": 1, "outcome": "unknown"}])
    out = ppc.run({"execute": True})
    assert "提交失败 1" in out and "结局不确定留 pending 1" in out
    assert "删除提交 0" in out and seen["marked"] == []
    assert "二轮重试" not in out          # 4xx 被拒/unknown 不算网络波动


def test_execute_isolates_store_failures(monkeypatch):
    # 单店代理断线不炸整轮(2026-08-07 生产实证):异常店跳过,其余店照常
    _wire(monkeypatch, [_row("T1", "S1"), _row("T2", "S2")], stores=("T1", "T2"))
    submitted = []

    def flaky(store, ft, entries, *, workflow=""):
        if store["name"] == "T1":
            raise ConnectionError("proxy TLS EOF")
        submitted.append((store["name"], list(entries)))
        return [{"feed_id": "F_OK", "count": len(entries),
                 "outcome": "submitted"}]

    monkeypatch.setattr(ppc.feeds, "submit_feed", flaky)
    out = ppc.run({"execute": True})
    assert submitted == [("T2", ["S2"])]                     # T2 不被重复提交
    assert "⚠ T1:提交异常" in out and "T2:删除提交 1" in out
    assert "二轮重试 1 店:T1" in out and "⚠ T1:二轮仍失败" in out


def test_second_round_retry_after_network_failure(monkeypatch):
    # 二轮重试成功路径:第一轮 token 阶段确定未达(retryable)→
    # 全部店跑完后整店重提,事件只落一次、挂二轮的真实 feed_id
    seen = _wire(monkeypatch, [_row("T1", "S1", rid=7)])
    calls = {"n": 0}

    def flaky_then_ok(store, ft, entries, *, workflow=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"feed_id": None, "count": 1, "outcome": "failed",
                     "retryable": True}]
        return [{"feed_id": "F_RETRY", "count": 1, "outcome": "submitted"}]

    monkeypatch.setattr(ppc.feeds, "submit_feed", flaky_then_ok)
    out = ppc.run({"execute": True})
    sub = [e for e in seen["events"] if e["event"] == "delete_submitted"]
    assert len(sub) == 1 and sub[0]["detail"]["feed_id"] == "F_RETRY"
    assert seen["marked"] == [((7,), "F_RETRY")]     # 只转一次态
    assert "二轮重试 1 店:T1" in out and "二轮仍失败" not in out


def test_relist_uses_detail_not_a_second_db_read(monkeypatch):
    """反补条目的 gtin/upc 由建议行 detail 带过来,执行件不回头查库
    ——否则"决策看到的数据"与"执行用的数据"分处两个时间点,会不一致。"""
    seen = _wire(monkeypatch, [_row("T1", "S1", "relist", rid=3,
                                    gtin="123456789012", upc="")])
    sent = []
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda store, ft, entries, *, workflow="": (
                            sent.append((ft, list(entries))),
                            [{"feed_id": "FR", "count": 1,
                              "outcome": "submitted"}])[1])
    ppc.run({"execute": True})
    ft, entries = sent[0]
    assert ft == "MP_MAINTENANCE"
    assert entries[0]["Orderable"]["sku"] == "S1"
    assert [e["event"] for e in seen["events"]] == ["maintenance_submitted"]


def test_relist_without_product_id_is_reported_not_silent(monkeypatch):
    """建议行 detail 里没有 gtin/upc → 组不出条目。静默少发比什么都不做更坏:
    那些行会一直挂 suggested,而摘要里看不出少了什么。"""
    _wire(monkeypatch, [_row("T1", "S1", "relist", gtin="", upc="")])
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("组不出条目就不该提交")))
    out = ppc.run({"execute": True})
    assert "缺 gtin/upc 组不出条目" in out


def test_settle_runs_before_claim_and_is_reported(monkeypatch):
    """上一轮的落定必须先跑再领新建议 —— 顺序反了的话,本轮刚提交的行会被
    立刻拿去和上一轮的观测事件比对,判出一个毫无意义的结论。"""
    _wire(monkeypatch, [], settled={"confirmed": 3, "ineffective": 2})
    out = ppc.run({"execute": True})
    assert "生效 3" in out and "未生效 2" in out


def test_no_suggestions_points_at_the_scanner(monkeypatch):
    """没有建议行时要说清该先跑什么 —— 拆分后最容易犯的错就是只跑执行件。"""
    _wire(monkeypatch, [])
    out = ppc.run({"execute": False})
    assert "problem_scan" in out and "顺序是硬约束" in out


def test_dry_run_zero_submissions(monkeypatch):
    _wire(monkeypatch, [_row("T1", "S_B")])
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("dry-run 不许提交")))
    out = ppc.run({"execute": False})
    assert "DRY-RUN" in out and "删除 1" in out
    assert "类别={B:1}" in out and "删除样本=[('S_B', 'B')]" in out


def test_dry_run_does_not_settle(monkeypatch):
    """dry-run 连落定都不做:settle 会改建议行状态,那是写操作。"""
    seen = _wire(monkeypatch, [_row("T1", "S1")])
    monkeypatch.setattr(ppc.dispositions, "settle",
                        lambda conn: (_ for _ in ()).throw(
                            AssertionError("dry-run 不许改状态")))
    monkeypatch.setattr(ppc.feeds, "submit_feed", lambda *a, **k: [])
    ppc.run({"execute": False})
    assert seen["marked"] == []


def test_cleanup_makes_no_decisions():
    """执行件的核心承诺:不再有任何归类/路由决策入口。"""
    import inspect
    src = inspect.getsource(ppc)
    assert "categorize" not in src and "is_stage_pending" not in src
    assert "walmart_items" not in src        # 不自己查问题商品清单
