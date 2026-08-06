"""services/feed_track 回归:统一轮询积木——终态落台账、missing 判定、全局摘要。"""

import contextlib

import pytest

from api import feeds
from services import feed_track


class _Conn:
    def __init__(self):
        self.sqls: list = []

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))
        self._last = sql

    def executemany(self, sql, rows):
        self.sqls.append((sql, list(rows)))

    rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_db(monkeypatch, conn):
    from registry import db
    monkeypatch.setattr(db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([conn])))


STORE = {"name": "T1", "client_id": "c", "client_secret": "s", "proxy": None}


def test_poll_feed_terminal_writes_ledger(monkeypatch):
    conn = _Conn()
    _fake_db(monkeypatch, conn)
    done = []
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda store, fid: {"feedStatus": "PROCESSED"})
    monkeypatch.setattr(feeds, "iter_feed_items", lambda store, fid: iter([
        {"sku": "A", "ingestionStatus": "SUCCESS"},
        {"sku": "B", "ingestionStatus": "DATA_ERROR",
         "ingestionErrors": {"ingestionError": [{"code": "ERR_9"}]}},
    ]))
    monkeypatch.setattr(feeds, "mark_feed_done",
                        lambda fid, ok: done.append((fid, ok)))

    out = feed_track.poll_feed(STORE, "F1")
    assert out == {"A": ("success", ""), "B": ("failed", "ERR_9")}
    many_sql, rows = conn.sqls[0]
    assert "UPDATE ops.feed_items" in many_sql
    assert ("success", None, "F1", "A") in rows
    assert ("failed", "ERR_9", "F1", "B") in rows
    miss_sql, args = conn.sqls[1]
    assert "'missing'" in miss_sql and args[1] == ["A", "B"]   # 查无的标 missing
    assert done == [("F1", True)]


def test_poll_feed_not_terminal_returns_none(monkeypatch):
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda store, fid: {"feedStatus": "INPROGRESS"})
    assert feed_track.poll_feed(STORE, "F1") is None


def test_poll_all_summary_and_pending_alarm(monkeypatch, caplog):
    import logging as _logging
    monkeypatch.setattr(feeds, "query_pending", lambda store_name=None: [
        {"status": "submitted", "feed_id": "F1", "store": "T1",
         "feed_type": "DELETE_ITEM", "created_at": "t"},
        {"status": "submitted", "feed_id": "F2", "store": "T_GONE",
         "feed_type": "DELETE_ITEM", "created_at": "t"},
        {"status": "pending", "feed_id": None, "store": "T1",
         "feed_type": "RETIRE_ITEM", "created_at": "t"},
    ])
    monkeypatch.setattr(feed_track, "poll_feed", lambda store, fid: {"A": ("success", "")})
    with caplog.at_level(_logging.WARNING, logger="services.feed_track"):
        out = feed_track.poll_all({"T1": STORE})
    assert "落定 1" in out and "凭证缺失跳过 1" in out
    assert "pending 待人工核对 1" in out
    assert any("提交结局不确定" in m for m in caplog.messages)
