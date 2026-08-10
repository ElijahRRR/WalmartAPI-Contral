"""增量泵的游标纪律 + 借锁就地摄取。

游标推进错一步就是**静默丢一段数据**:两侧都不报错,只是产品中心少了一批
记录,而增量流永不回头(游标只前进)。所以这里逐条钉死,而不是"跑通就行"。
"""

import fcntl

import pytest

from services import product_ingest as ingest
from services import runlock


class FakeScraper:
    """按脚本吐页的假采集器;记录每次请求的游标。"""

    RetentionGapError = type("RetentionGapError", (RuntimeError,), {})
    ExportAuthError = type("ExportAuthError", (RuntimeError,), {})

    def __init__(self, pages):
        self.pages = list(pages)
        self.asked = []

    def export_incremental(self, cursor, limit):
        self.asked.append(cursor)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


class FakeDB:
    """假连接:游标存内存,ingest_batch 被打桩掉,所以不碰真表。"""

    def __init__(self):
        self.cursor_value = 0
        self.saved = []

    def pg_conn(self):
        db = self

        class _Ctx:
            def __enter__(self): return db
            def __exit__(self, *a): return False
        return _Ctx()


@pytest.fixture
def wired(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(ingest, "load_cursor", lambda conn: conn.cursor_value)

    def save(conn, v):
        conn.cursor_value = v
        conn.saved.append(v)
    monkeypatch.setattr(ingest, "save_cursor", save)
    monkeypatch.setattr(ingest, "ingest_batch",
                        lambda conn, recs: {"snapshots": len(recs), "dup": 0,
                                            "products": len(recs),
                                            "skipped_outcome": 0,
                                            "incomplete": 0, "invalid": 0})
    return db


def test_pump_walks_pages_and_follows_next_cursor(wired):
    """游标只认响应给的 next_cursor —— 不自己算 max(seq) 或 cursor+count。"""
    sc = FakeScraper([([{"a": 1}], 10, True),
                      ([{"a": 2}, {"a": 3}], 25, False)])
    res = ingest.pump(sc, wired)
    assert res["ok"] and res["pages"] == 2 and res["fetched"] == 3
    assert res["cursor_from"] == 0 and res["cursor_to"] == 25
    assert sc.asked == [0, 10]          # 第二页从上一页给的游标接着拉
    assert wired.saved == [10, 25]


def test_pump_empty_page_does_not_advance(wired):
    """空页不推进 —— 这是唯一不丢数据的方向(契约边界语义)。

    若这里改成"推进到 next_cursor",而采集侧空页恰好回了一个更大的值,
    中间那段记录就永远不会被拉一次,两侧都不报错。
    """
    sc = FakeScraper([([], 0, False)])
    res = ingest.pump(sc, wired)
    assert res["ok"] and res["fetched"] == 0
    assert res["cursor_to"] == 0 and wired.saved == []


def test_pump_stops_when_records_empty_even_if_has_more(wired):
    """空页 + has_more=True 也要停:再拉一次还是空页,只会空转。"""
    sc = FakeScraper([([], 7, True)])
    res = ingest.pump(sc, wired)
    assert res["pages"] == 1


def test_pump_retention_gap_stops_and_keeps_cursor(wired):
    """409 保留期缺口:停在原地 + 报错,**绝不当普通错误往前跳**。

    往前跳 = 一路跳过被裁掉的区间,静默丢数据。重置只能由人工在全量对账后
    显式 -p cursor=N 做。
    """
    sc = FakeScraper([([{"a": 1}], 5, True),
                      FakeScraper.RetentionGapError("cursor_below_retention")])
    res = ingest.pump(sc, wired)
    assert res["ok"] is False
    assert "保留期缺口" in res["error"] and "未推进" in res["error"]
    assert res["cursor_to"] == 5            # 停在成功那一页落下的位置
    assert wired.cursor_value == 5
    assert "⛔" in ingest.pump_summary(res)


def test_pump_auth_error_stops_without_advancing(wired):
    sc = FakeScraper([FakeScraper.ExportAuthError("401")])
    res = ingest.pump(sc, wired)
    assert res["ok"] is False and res["cursor_to"] == 0 and wired.saved == []


def test_pump_respects_max_pages(wired):
    sc = FakeScraper([([{"a": 1}], 1, True), ([{"a": 2}], 2, True),
                      ([{"a": 3}], 3, True)])
    res = ingest.pump(sc, wired, max_pages=2)
    assert res["pages"] == 2 and res["cursor_to"] == 2


def test_pump_explicit_cursor_overrides_stored(wired):
    """-p cursor=N 全量对账后重置:必须从给的值起,不读库里的。"""
    wired.cursor_value = 999
    sc = FakeScraper([([{"a": 1}], 3, False)])
    res = ingest.pump(sc, wired, cursor=0)
    assert sc.asked == [0] and res["cursor_from"] == 0


def test_pump_summary_surfaces_non_ok_outcomes(wired):
    sc = FakeScraper([([{"a": 1}], 1, False)])
    res = ingest.pump(sc, wired)
    res["totals"].update(skipped_outcome=3, outcome_blocked=2, outcome_stale=1,
                         incomplete=1, invalid=2)
    line = ingest.pump_summary(res)
    assert "blocked×2" in line and "stale×1" in line
    assert "completeness_ok=false 1" in line and "丢弃 2" in line


# ── 借锁 ──────────────────────────────────────────────────────────────────────

def test_runlock_is_exclusive(tmp_path, monkeypatch):
    """同名锁只能有一个持有者 —— order_audit 就地摄取时靠它挡住
    并发推游标(后写的会盖掉先写的,中间那段永远不会再被拉一次)。"""
    monkeypatch.setattr(runlock.paths, "locks_dir", lambda: tmp_path)
    fh = runlock.acquire("product_ingest")
    assert fh is not None
    try:
        with runlock.hold("product_ingest") as got:
            assert got is False           # 已被占用:调用方按"这轮跳过"处理
        assert runlock.hold("别的锁").__enter__() is True   # 不同名互不影响
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
    with runlock.hold("product_ingest") as got:
        assert got is True                # 释放后拿得到
