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

    def fetchall(self):
        return []

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

    head, out = feed_track.poll_feed(STORE, "F1")
    assert head["feedStatus"] == "PROCESSED"
    assert out == {"A": ("success", ""), "B": ("failed", "ERR_9")}
    def _find(frag):        # 按内容找,别按位置(加语句就错位)
        return next(x for x in conn.sqls if frag in x[0])

    sel_sql, _ = _find("SELECT sku, workflow")
    assert "SELECT sku, workflow, feed_type, status" in sel_sql  # 先取更新前状态
    many_sql, rows = _find("SET status = %s")
    assert "UPDATE ops.feed_items" in many_sql
    assert ("success", None, None, "F1", "A") in rows
    assert ("failed", "ERR_9", None, "F1", "B") in rows
    miss_sql, args = _find("'missing'")
    assert "'missing'" in miss_sql and args[1] == ["A", "B"]   # 查无的标 missing
    assert done == [("F1", True)]


def test_poll_feed_keeps_feed_open_when_sku_processing(monkeypatch, caplog):
    # feed 终态但个别 SKU 仍 INPROGRESS:不 mark_feed_done,下轮重查
    # (否则该 SKU 永久卡 submitted,cleanup 在途拦截会永远跳过它)
    import logging as _logging
    conn = _Conn()
    _fake_db(monkeypatch, conn)
    done = []
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda s, f: {"feedStatus": "PROCESSED"})
    monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([
        {"sku": "A", "ingestionStatus": "SUCCESS"},
        {"sku": "B", "ingestionStatus": "INPROGRESS"}]))
    monkeypatch.setattr(feeds, "mark_feed_done", lambda fid, ok: done.append(fid))
    with caplog.at_level(_logging.WARNING, logger="services.feed_track"):
        _head, out = feed_track.poll_feed(STORE, "F1")
    assert out["B"] == ("processing", "")
    assert done == []
    assert any("仍 processing/unknown" in m for m in caplog.messages)


def test_poll_feed_maintenance_receipt_not_in_ledger_for_non_relist(monkeypatch):
    # 维护类回执不进病历(所有者定稿 2026-08-07):同为 MP_MAINTENANCE,
    # 反补来源(problem_product_cleanup)进,标题/到期日期维护来源不进;
    # feed_items 台账两者照常落定
    class _MetaConn(_Conn):
        def fetchall(self):
            if "SELECT sku, workflow, feed_type, status" in self._last:
                return [("A", "maintenance", "MP_MAINTENANCE", "submitted"),
                        ("B", "problem_product_cleanup", "MP_MAINTENANCE",
                         "submitted")]
            return []
    conn = _MetaConn()
    _fake_db(monkeypatch, conn)
    recorded = []
    monkeypatch.setattr(feed_track.product_events, "record_many",
                        lambda c, rows: (recorded.extend(rows), len(rows))[1])
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda s, f: {"feedStatus": "PROCESSED"})
    monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([
        {"sku": "A", "ingestionStatus": "SUCCESS"},
        {"sku": "B", "ingestionStatus": "SUCCESS"}]))
    monkeypatch.setattr(feeds, "mark_feed_done", lambda fid, ok: None)
    feed_track.poll_feed(STORE, "F1")
    assert [e["sku"] for e in recorded] == ["B"]        # 只有反补来源入账
    many_sql, rows = next(x for x in conn.sqls if "SET status = %s" in x[0])
    assert ("success", None, None, "F1", "A") in rows          # 台账不受白名单影响


def test_poll_feed_repoll_does_not_duplicate_events(monkeypatch):
    # 重轮询(上一轮已把 A 落定)只对本轮才落定的 SKU 记回执事件
    class _MetaConn(_Conn):
        def fetchall(self):
            if "SELECT sku, workflow, feed_type, status" in self._last:
                return [("A", "wf", "DELETE_ITEM", "success"),
                        ("B", "wf", "DELETE_ITEM", "submitted")]
            return []
    conn = _MetaConn()
    _fake_db(monkeypatch, conn)
    recorded = []
    monkeypatch.setattr(feed_track.product_events, "record_many",
                        lambda c, rows: (recorded.extend(rows), len(rows))[1])
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda s, f: {"feedStatus": "PROCESSED"})
    monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([
        {"sku": "A", "ingestionStatus": "SUCCESS"},
        {"sku": "B", "ingestionStatus": "SUCCESS"}]))
    monkeypatch.setattr(feeds, "mark_feed_done", lambda fid, ok: None)
    feed_track.poll_feed(STORE, "F1")
    assert [e["sku"] for e in recorded] == ["B"]


def test_poll_feed_not_terminal_returns_head_and_none(monkeypatch):
    monkeypatch.setattr(feeds, "get_feed_status", lambda store, fid: {
        "feedStatus": "INPROGRESS", "itemsReceived": 10, "itemsSucceeded": 3,
        "itemsFailed": 1})
    head, results = feed_track.poll_feed(STORE, "F1")
    assert results is None
    # 进度计数直接来自 feed 级 GET,零明细翻页
    assert feed_track._progress(head) == "已收 10,成功 3,失败 1,待处理 6"


def test_poll_all_summary_and_pending_alarm(monkeypatch, caplog):
    import logging as _logging
    monkeypatch.setattr(feeds, "query_pending", lambda store_name=None: [
        {"status": "submitted", "feed_id": "F1", "store": "T1",
         "feed_type": "DELETE_ITEM", "workflow": "", "created_at": "t"},
        {"status": "submitted", "feed_id": "F2", "store": "T_GONE",
         "feed_type": "DELETE_ITEM", "workflow": "", "created_at": "t"},
        {"status": "pending", "feed_id": None, "store": "T1",
         "feed_type": "RETIRE_ITEM", "created_at": "t"},
    ])
    monkeypatch.setattr(feed_track, "poll_feed",
                        lambda store, fid: ({"feedStatus": "PROCESSED"},
                                            {"A": ("success", "")}))
    with caplog.at_level(_logging.WARNING, logger="services.feed_track"):
        out = feed_track.poll_all({"T1": STORE})
    assert "落定 1" in out and "凭证缺失跳过 1" in out
    assert "pending 待人工核对 1" in out
    # 逐 feed 明细:店铺 + 业务动作名 + feed_id + 结果
    assert "T1 删除(-) F1:已落定 PROCESSED,成功 1,失败 0" in out
    assert "T_GONE 删除(-) F2:店铺凭证缺失,跳过" in out
    assert any("提交结局不确定" in m for m in caplog.messages)


def test_save_errors_rows_shape():
    """每条 ingestionError 一行,带 field/code——聚合分析的主维度。"""
    from services.feed_track import _save_errors

    captured = {}

    class _Cur:
        def executemany(self, sql, rows):
            captured["sql"] = sql
            captured["rows"] = rows

    errs = {"SKU1": [{"type": "DATA_ERROR", "code": "C1", "field": "color",
                      "description": "required"},
                     {"type": "DATA_ERROR", "code": "C2", "field": "material",
                      "description": "need JSONArray"}],
            "SKU2": []}
    n = _save_errors(_Cur(), "F1", "店A", errs,
                     {"SKU1": ("list_new", "MP_ITEM", "submitted")})
    assert n == 2
    assert "ON CONFLICT (feed_id, sku, seq) DO NOTHING" in captured["sql"]
    first = captured["rows"][0]
    assert first[:6] == ("F1", "SKU1", 0, "DATA_ERROR", "C1", "color")
    assert first[7:10] == ("MP_ITEM", "店A", "list_new")
    assert captured["rows"][1][2] == 1                  # seq 递增
    assert _save_errors(_Cur(), "F1", "店A", {"S": []}, {}) == 0
