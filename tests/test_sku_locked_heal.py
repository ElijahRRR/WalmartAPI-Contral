"""SKU_LOCKED 自愈链回归:退役提交、冷却防重、清列重上、失败不自动重试。

旧实证(legacy_survey.md:1667):SKU 绑死旧 UPC,不先 RETIRE 直接换 UPC
重发同一 SKU 也失败——所以链路必须是 RETIRE → 24h 冷却 → 清列 → 新行重上。
"""

from workflows import sku_locked_heal as heal


class _Conn:
    """假连接:记录 SQL,喂 _SQL_OPEN 的返回。"""

    def __init__(self, state=()):
        self.sqls: list = []
        self._state = list(state)

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))

    def executemany(self, sql, rows):
        self.sqls.append((sql, list(rows)))

    def fetchall(self):
        return self._state

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _row(rownum, store="T1", asin=None, list_result="SKU_LOCKED"):
    return {"rownum": rownum, "store": store,
            "asin": asin or f"B0LOCK{rownum:04d}",
            "list_result": list_result, "feed_id": "F0", "listed": "Yes"}


def _patch_common(monkeypatch, rows, state, conn=None):
    conn = conn or _Conn(state)
    monkeypatch.setattr(heal.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(heal.db, "pg_conn", lambda: conn)
    monkeypatch.setattr(heal.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    return conn


def test_dry_run_reports_without_touching_anything(monkeypatch):
    rows = [_row(2), _row(3)]
    conn = _patch_common(monkeypatch, rows, state=[])
    monkeypatch.setattr(heal.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("dry-run 不许提交")))
    out = heal.run({"execute": False})
    assert "SKU_LOCKED 2 行" in out
    assert "将退役 2 个" in out
    # dry-run 只读 _SQL_OPEN,不写冷却表
    assert all("INSERT" not in sql for sql, _ in conn.sqls)


def test_retire_submits_and_starts_cooldown(monkeypatch):
    rows = [_row(2), _row(3)]
    conn = _patch_common(monkeypatch, rows, state=[])
    submitted = {}
    monkeypatch.setattr(
        heal.feeds, "submit_feed",
        lambda store, ft, skus, workflow: (
            submitted.update({"ft": ft, "skus": list(skus), "wf": workflow}),
            [{"outcome": "submitted", "feed_id": "FR1", "count": len(skus)}],
        )[1])
    recorded = []
    monkeypatch.setattr(heal.product_events, "record_many",
                        lambda c, evs: (recorded.extend(evs), len(evs))[1])
    out = heal.run({"execute": True})
    assert submitted["ft"] == "RETIRE_ITEM" and submitted["wf"] == "sku_locked_heal"
    assert submitted["skus"] == ["B0LOCK0002", "B0LOCK0003"]
    # 冷却记录落库 + retire_submitted 入病历(source=sku_locked_heal)
    inserts = [rows_ for sql, rows_ in conn.sqls if "INSERT INTO listing.retire_cooldown" in sql]
    assert inserts and [r[1] for r in inserts[0]] == ["B0LOCK0002", "B0LOCK0003"]
    assert {e["event"] for e in recorded} == {heal.product_events.RETIRE_SUBMITTED}
    assert all(e["source"] == "sku_locked_heal" for e in recorded)
    assert "退役提交 2 条" in out


def test_open_cooldown_not_resubmitted_and_failed_needs_human(monkeypatch):
    """冷却在途不重复退役;此前回执失败的点名人工,不自动重试。"""
    rows = [_row(2, asin="B0COOLING"), _row(3, asin="B0FAILED")]
    state = [("T1", "B0COOLING", "FR1", None, "pending", False),
             ("T1", "B0FAILED", "FR2", None, "failed", False)]
    _patch_common(monkeypatch, rows, state)
    monkeypatch.setattr(heal.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("在途/失败都不该再提交")))
    out = heal.run({"execute": True})
    assert "冷却中 1" in out
    assert "人工处置" in out and "B0FAILED" in out


def test_ripe_success_clears_row_failed_marks(monkeypatch):
    """冷却期满:回执成功清列重上;回执失败标 failed;未到继续等。"""
    rows = [_row(2, asin="B0DONE"), _row(3, asin="B0BAD"),
            _row(4, asin="B0WAIT")]
    state = [("T1", "B0DONE", "FR1", None, "pending", True),
             ("T1", "B0BAD", "FR2", None, "pending", True),
             ("T1", "B0WAIT", "FR3", None, "pending", True)]
    conn = _patch_common(monkeypatch, rows, state)
    receipts = {"FR1": {"B0DONE": ("success", "")},
                "FR2": {"B0BAD": ("failed", "ERR_X")},
                "FR3": {}}
    monkeypatch.setattr(heal.feed_track, "item_results",
                        lambda fid: receipts[fid])
    monkeypatch.setattr(heal.feed_track, "poll_feed",
                        lambda store, fid: (None, None))
    cleared = []
    monkeypatch.setattr(heal.listing_sheet, "clear_for_relist",
                        lambda rownums, execute: (cleared.extend(rownums),
                                                  len(rownums))[1])
    monkeypatch.setattr(heal.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("在途冷却不该再提交")))
    out = heal.run({"execute": True})
    assert cleared == [2]                       # 只有回执成功的行被清列
    closes = [args for sql, args in conn.sqls if "UPDATE listing.retire_cooldown" in sql]
    assert ("cleared", "T1", "B0DONE") in closes
    assert ("failed", "T1", "B0BAD") in closes
    assert "清列重上 1 行" in out and "回执未到 1 条" in out and "回执失败 1 条" in out
