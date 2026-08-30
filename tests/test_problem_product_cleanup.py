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
    from services import store_retry as _sr
    seen = {"events": [], "marked": [], "marked_by": set(), "sheet": [],
            "settled": settled or
            {"confirmed": 0, "ineffective": 0}}
    # 二轮补试走 store_retry,每店前有 _client.backoff(0) 抖动等待:
    # 用例只钉行为,不必真等(与 tests/test_store_retry_standard 同款)
    monkeypatch.setattr(_sr.time, "sleep", lambda s: None)
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    monkeypatch.setattr(ppc.dispositions, "claim",
                        lambda conn, actions=None: list(rows))
    monkeypatch.setattr(ppc.dispositions, "settle",
                        lambda conn: seen["settled"])
    monkeypatch.setattr(ppc.dispositions, "mark_executing",
                        lambda conn, ids, feed_id, by="": (
                            seen["marked"].append((tuple(ids), feed_id)),
                            seen["marked_by"].add(by), len(ids))[2])
    monkeypatch.setattr(ppc.product_events, "record_many",
                        lambda conn, rs: (seen["events"].extend(rs), len(rs))[1])
    monkeypatch.setattr(ppc, "_retire_caps", lambda: {})
    # 缺席避让与按日配额记账都走库,测试环境无库:置空(缺席=无、当日已放行=0)。
    # 打 stale_stores 这一层:执行件调的 stale_or_note 是它的降级外壳,
    # 探测正常时 note 为空串 —— fail-closed 那条路专门有用例走
    monkeypatch.setattr(ppc.store_absence, "stale_stores",
                        lambda conn, since=None, lag_hours=None: [])
    monkeypatch.setattr(ppc.dispositions, "destructive_executed_today",
                        lambda conn, hours=20: {})
    monkeypatch.setattr(ppc.maint_sheet, "append_records",
                        lambda rows: (seen["sheet"].extend(rows), len(rows))[1])
    monkeypatch.setattr(ppc.stores_svc, "load_stores",
                        lambda names=None: [{"name": s} for s in stores])
    return seen


def test_run_buckets_rows_by_action(monkeypatch):
    """分桶件 2026-08-27 上移 services.dispositions —— 这里钉**接线**:
    本工作流按 action × _ACTION_ORDER 分桶,两个动作各进各的桶
    (relist 2026-08-28 退役,见 test_legacy_relist_rows_blow_up_loudly)。
    (算法与「未知动作即抛」的单元用例在 tests/test_dispositions_router.py)"""
    _wire(monkeypatch, [_row("T1", "S1"), _row("T1", "S2", "retire"),
                        _row("T2", "S3", "retire")], stores=("T1", "T2"))
    out = ppc.run({"execute": False})
    assert "删除 1,顽固停用 2" in out
    assert "T1:删除 1,顽固停用 1" in out
    assert "T2:删除 0,顽固停用 1" in out


def test_run_rejects_unknown_action(monkeypatch):
    """建议表里出现不认识的动作 → 宁炸不吞,**生产路径上照样抛**。静默丢弃会让
    那些行永远挂 suggested,而部分唯一索引又挡着新建议,该 SKU 从此再也处理不了。

    报错里的 `id=` 取的必须是本表的 `id` 列(接线传 id_field="id"):点错列会
    报出 `id=None`,拿着这条报错回表里根本找不到是哪一行。"""
    _wire(monkeypatch, [_row("T1", "S1", "nope", rid=77)])
    with pytest.raises(ValueError, match=r"未知 action='nope'.*id=77"):
        ppc.run({"execute": False})


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
    assert "二轮重试 1 店(串行):T1" in out and "⚠ T1:二轮仍失败" in out


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
    assert "二轮重试 1 店(串行):T1" in out and "二轮仍失败" not in out


def test_legacy_relist_rows_blow_up_loudly(monkeypatch):
    """反补 2026-08-28 退役后的两道防线,钉死第二道:

    ① claim 不再领 relist(dispositions.PROBLEM_ACTIONS 收窄,存量 suggested
       由扫描件 withdraw_stale 撤);
    ② 万一一条 relist 行绕过①漏进来(排查用 actions=None 全领之类),分桶件
       按「未知动作即抛」宁炸不吞 —— 绝不能静默把它当维护 feed 发出去。"""
    assert "relist" not in ppc._ACTION_FEED
    assert ppc._ACTION_ORDER == ("retire", "delete")
    _wire(monkeypatch, [_row("T1", "S1", "relist", rid=9)])
    with pytest.raises(ValueError, match=r"未知 action='relist'.*id=9"):
        ppc.run({"execute": False})


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


# ── 破坏动作的唯一出口(所有者定稿 2026-08-24)────────────────────────────

def test_maint_sourced_delete_is_executed_here(monkeypatch):
    """维护链建议的删除也归本工作流执行 —— 按**动作**领取,不按来源。

    此前 maintenance 也能发 DELETE_ITEM:两个出口意味着配额、在途防重、
    病历口径各有一套,同一个 SKU 被两条链先后删两次是生产实证过的。
    """
    row = _row("T1", "B0A", action="delete")
    row["source"] = "maint"                 # 维护链建的,审核链没参与
    seen = _wire(monkeypatch, [row])
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda store, ft, entries, workflow="": [
                            {"feed_id": "F1", "count": len(entries),
                             "outcome": "submitted"}])
    out = ppc.run({"execute": True})
    assert "删除提交 1" in out
    assert seen["marked"] == [((row["id"],), "F1")]
    # 转态必须落执行者:合并之后"最终是谁干的"不能靠 source 反推
    assert seen["marked_by"] == {"problem_product_cleanup"}


def test_per_store_cap_is_applied_once_here_and_is_visible(monkeypatch):
    """单店「下架限制」只在这里截一次,削掉多少必须见人。

    此前两条扫描件各按同一张限额表截一次 ⇒ 每店实际可删 2N。截断静默的话,
    摘要读起来就是"今天就这么多",而其实还压着一批。
    (2026-08-28 起限额暂停,这里打回 False 测机械还在——同 cap 用例。)
    """
    rows = [_row("T1", f"S{i}", action="delete", rid=i) for i in range(5)]
    seen = _wire(monkeypatch, rows)
    monkeypatch.setattr(ppc, "RETIRE_CAP_PAUSED", False)
    monkeypatch.setattr(ppc, "_retire_caps", lambda: {"T1": 2})
    sent = []
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda store, ft, entries, workflow="": (
                            sent.append(list(entries)),
                            [{"feed_id": "F1", "count": len(entries),
                              "outcome": "submitted"}])[1])
    out = ppc.run({"execute": True})
    assert sent == [["S0", "S1"]]           # 定序取件:留下的不是随机一批
    assert "⚠ 超单店「下架限制」留到下轮:T1×3" in out
    assert "没有丢弃" in out
    assert seen["marked"] == [((0, 1), "F1")]


def test_cap_caps_destructive_and_reports_leftover(monkeypatch):
    """单店「下架限制」封顶破坏类(delete/retire),超额留到下轮且必须报出来
    (静默截断读起来就是"全做完了")。
    ⚠ 2026-08-28 起限额**暂停**(RETIRE_CAP_PAUSED=True),这里显式打回
    False 测的是**机械还在**:停用不等于拆除,恢复只需改常量。"""
    rows = [_row("T1", f"D{i}", action="delete", rid=i) for i in range(3)]
    _wire(monkeypatch, rows)
    monkeypatch.setattr(ppc, "RETIRE_CAP_PAUSED", False)
    monkeypatch.setattr(ppc, "_retire_caps", lambda: {"T1": 1})
    sent = []
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda store, ft, entries, workflow="": (
                            sent.append((ft, len(entries))),
                            [{"feed_id": "F1", "count": len(entries),
                              "outcome": "submitted"}])[1])
    out = ppc.run({"execute": True})
    assert ("DELETE_ITEM", 1) in sent and len(sent) == 1
    assert "T1×2" in out


def test_cap_pause_lets_everything_through_and_shouts(monkeypatch):
    """限额暂停(所有者定稿 2026-08-28「暂时关闭这个限制」,08-28 档案清理波):

    ① 钉住现状:开关就是 True(恢复时改回 False 并同步改这条——先例
       title_mismatch 停闸,常量停闸、用例钉状态);
    ② 不截断:限额表值再小也全量出闸,限额表与按日记账**根本不读**
       (监-桩在这两处埋了雷,读了就炸);
    ③ 摘要**首行**点名停用中——静默的闸没人记得它关着。
    缺席避让 fail-closed 与在途防重不归这个开关管,各有用例。"""
    assert ppc.RETIRE_CAP_PAUSED is True
    rows = [_row("T1", f"D{i}", action="delete", rid=i) for i in range(3)]
    _wire(monkeypatch, rows)
    monkeypatch.setattr(ppc, "_retire_caps",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("停闸期间不该读限额表")))
    monkeypatch.setattr(ppc.dispositions, "destructive_executed_today",
                        lambda conn, hours=20: (_ for _ in ()).throw(
                            AssertionError("停闸期间不该按日记账")))
    sent = []
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda store, ft, entries, workflow="": (
                            sent.append((ft, len(entries))),
                            [{"feed_id": "F1", "count": len(entries),
                              "outcome": "submitted"}])[1])
    out = ppc.run({"execute": True})
    assert ("DELETE_ITEM", 3) in sent          # 3 条全出闸,没有截断
    assert "留到下轮" not in out
    first = out.splitlines()[0]
    assert "「下架限制」停用中" in first and "RETIRE_CAP_PAUSED" in first


# ── 维护记录表(2026-08-24:删除归口到本工作流之后必须接上)──────────────

def _cell(name):
    from registry import resources
    return resources.MAINT_SHEET.columns.index(name)


def test_delete_flow_lands_in_the_maintenance_sheet(monkeypatch):
    """删除流水必须进维护记录表 —— 那是所有者每天看的面板。

    删除此前由 maintenance 执行并写表。归口到本工作流之后不接上写表,面板上
    就再也看不见删除了,而且**完全静默**:两边都不报错,只是那张表少了一类。
    """
    rows = [_row("T1", "S1", action="delete", rid=1),
            _row("T1", "S2", action="delete", rid=2)]
    seen = _wire(monkeypatch, rows)
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda store, ft, entries, workflow="": [
                            {"feed_id": "F1", "count": 1, "outcome": "submitted"},
                            {"feed_id": "OLD", "count": 1, "outcome": "dedup"}])
    out = ppc.run({"execute": True})
    assert "维护记录追加 2 行" in out
    got = seen["sheet"]
    assert [r[_cell("sku")] for r in got] == ["S1", "S2"]
    # 「建议」恒是扫描件定的;「动作」是真做了什么 —— 在途防重写「跳过」,
    # 写成"删除"会让表格看起来做了两次删除
    assert [r[_cell("suggestion")] for r in got] == ["删除", "删除"]
    assert [r[_cell("action")] for r in got] == ["删除", "跳过"]
    assert [r[_cell("result")] for r in got] == ["处理中", "在途防重"]
    assert [r[_cell("feed_id")] for r in got] == ["F1", "OLD"]
    # 旧值/新值是维护三类用的,破坏动作没有值的变化
    assert all(r[_cell("old_value")] == "" and r[_cell("new_value")] == ""
               for r in got)


def test_unexecuted_rows_still_get_a_sheet_line(monkeypatch):
    """领到了却没执行的也要写表(与 maintenance 同一纪律)。

    不写的话它在飞书完全不可见,看起来像"扫描件没建议它",而它其实每天都在
    建议、每天都没做成。
    """
    seen = _wire(monkeypatch, [_row("T1", "S1", action="delete")],
                 stores=())            # 凭证缺失
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("凭证缺失不该提交")))
    ppc.run({"execute": True})
    assert [(r[_cell("sku")], r[_cell("action")], r[_cell("result")])
            for r in seen["sheet"]] == [("S1", "", "未执行(凭证缺失)")]


def test_dry_run_writes_no_sheet_rows(monkeypatch):
    """dry-run 一行都不许写:写表也是写操作,而且会推进水位。"""
    seen = _wire(monkeypatch, [_row("T1", "S1")])
    monkeypatch.setattr(ppc.feeds, "submit_feed", lambda *a, **k: [])
    ppc.run({"execute": False})
    assert seen["sheet"] == []


def test_only_maintenance_prunes_the_shared_sheet():
    """裁剪只由一处做:同一张表两处裁会各按各的水位重复读全段。"""
    import inspect
    assert "prune_after=False" in inspect.getsource(ppc.run)
    from workflows import maintenance as mw
    assert "prune_after" not in inspect.getsource(mw._write_sheet)


def test_second_round_goes_through_the_standard_serial_pass():
    """二轮重试必须串行(店级重试标准 2026-08-26):第一轮已经证明这批店/
    代理在抖,补试没有理由再齐射一遍。对抗校验实测过改回并发全量照绿,
    这里按调用点钉住。

    2026-08-27 起钉的是**标准件**:此前本文件自造 `_round(workers=1)`,
    与 store_retry 是「同语义、不同实现」—— 标准件后来加的规模闸
    (max(3, 总数//5) 判系统性故障就不补试)因此漏在了外面。
    """
    import inspect
    src = inspect.getsource(ppc.run)
    assert "store_retry.serial_second_pass(" in src
    assert "total_stores=len(first)" in src     # 规模闸要拿得到总店数
    assert "workers=1" not in src               # 自造的那份不许回来


def test_absence_probe_failure_stops_every_destructive_action(monkeypatch):
    """缺席探测失败 → **fail-closed**(2026-08-27 改;此前是 fail-open「不避让」)。

    本工作流是破坏动作的唯一出口,而缺席探测经 enabled_names 走飞书:一次
    飞书抖动就能让「按隔夜观测发 DELETE_ITEM」这条路重新打开(同一次抖动
    还会一起打开上游 problem_scan 的同款闸,没有第二重兜底)。
    conventions §六:兜底是补偿外部世界的缺陷,不是补偿自己的不确定 ——
    拿不准就不删,建议留在 suggested,下轮重新定夺(延后一轮无损)。
    """
    seen = _wire(monkeypatch, [_row("T1", "S1"), _row("T1", "S2")])
    monkeypatch.setattr(ppc.store_absence, "stale_stores",
                        lambda conn, **k: (_ for _ in ()).throw(
                            RuntimeError("飞书 502")))
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("探测失败不许发任何 feed")))
    out = ppc.run({"execute": True})
    first = out.splitlines()[0]         # 链通知只发首行,点名必须在这一行
    assert "⚠ 缺席探测失败,本轮破坏动作全停(fail-closed)" in first
    assert "2 条建议留在 suggested 原地" in first
    assert seen["marked"] == [] and seen["events"] == [] and seen["sheet"] == []


# ── 店铺事件账本(运营类:每店每轮一条)────────────────────────────────────

def _capture_rounds(monkeypatch):
    got: list = []
    monkeypatch.setattr(ppc.store_events, "record_round",
                        lambda conn, source, event, per_store:
                        (got.append((source, event, dict(per_store))),
                         len(per_store))[1])
    return got


def test_the_two_rounds_are_summed_into_one_event(monkeypatch):
    """★ 二轮重试的店只记**一条**,两轮计数相加。

    分两条记的话,账本上那家店看起来这一轮删了两遍 —— 而二轮重提的那批
    第一轮其实已经发出去了(被在途防重挡回 dedup),不是又删了一次。
    """
    _wire(monkeypatch, [_row("T1", "S1", rid=7)])
    calls = {"n": 0}

    def flaky_then_dedup(store, ft, entries, *, workflow=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"feed_id": None, "count": 1, "outcome": "failed",
                     "retryable": True}]
        return [{"feed_id": "F_OLD", "count": 1, "outcome": "dedup"}]

    monkeypatch.setattr(ppc.feeds, "submit_feed", flaky_then_dedup)
    got = _capture_rounds(monkeypatch)
    ppc.run({"execute": True})
    assert len(got) == 1
    per_store = got[0][2]
    assert list(per_store) == ["T1"]
    assert per_store["T1"]["delete"] == {"submitted": 0, "dedup": 1,
                                         "failed": 1, "unknown": 0}
    assert per_store["T1"]["retried"] is True
    assert got[0][1] == ppc.store_events.CLEANUP_ROUND


def test_dry_run_records_no_round_event(monkeypatch):
    _wire(monkeypatch, [_row("T1", "S1")])
    got = _capture_rounds(monkeypatch)
    ppc.run({"execute": False})
    assert got == []
