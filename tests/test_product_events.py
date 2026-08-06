"""产品事件账本回归:落账 SQL、状态迁移对比、删除观测核验。"""

from services import product_events as pe


class _Conn:
    def __init__(self, fetch=()):
        self.sqls: list = []
        self._fetch = list(fetch)

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))

    def executemany(self, sql, rows):
        self.sqls.append((sql, list(rows)))

    def fetchall(self):
        return self._fetch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_record_many_serializes_detail():
    conn = _Conn()
    n = pe.record_many(conn, [
        {"sku": "S1", "store": "T1", "event": "delete_submitted",
         "source": "product_clear", "detail": {"feed_id": "F1"}},
        {"sku": "S2", "event": "item_missing", "source": "catalog_sync"},
    ])
    assert n == 2
    sql, rows = conn.sqls[0]
    assert "INSERT INTO catalog.product_events" in sql
    assert rows[0][0] == "S1" and '"feed_id"' in rows[0][5]
    assert rows[1][1] is None and rows[1][5] is None      # store/detail 可空
    assert pe.record_many(conn, []) == 0                  # 空集不发 SQL


def test_diff_catalog_transitions():
    old = {"A": ("PUBLISHED", None),          # 状态将变化
           "B": ("PUBLISHED", "2026-08-01"),  # 曾缺席将重现
           "C": ("PUBLISHED", None)}          # 无变化
    new_rows = [
        {"sku": "A", "published_status": "UNPUBLISHED",
         "unpublished_reasons": "价格问题"},
        {"sku": "B", "published_status": "PUBLISHED"},
        {"sku": "C", "published_status": "PUBLISHED"},
        {"sku": "D", "published_status": "PUBLISHED"},    # 新出现
    ]
    evs = pe.diff_catalog(old, new_rows, "T1")
    by = {(e["sku"], e["event"]) for e in evs}
    assert ("A", "status_changed") in by
    assert ("B", "item_reappeared") in by
    assert ("D", "item_appeared") in by
    assert not any(e["sku"] == "C" for e in evs)          # 无变化零事件
    changed = next(e for e in evs if e["event"] == "status_changed")
    assert changed["detail"] == {"old": "PUBLISHED", "new": "UNPUBLISHED",
                                 "reasons": "价格问题"}


def test_verify_deletions_verdicts(caplog):
    import logging as _logging
    conn = _Conn(fetch=[("T1", "S_GONE", "gone"),
                        ("T1", "S_STILL", "still"),
                        ("T1", "S_WAIT", "wait")])
    with caplog.at_level(_logging.WARNING, logger="services.product_events"):
        gone, still = pe.verify_deletions(conn)
    assert (gone, still) == (1, 1)
    ins_sql, rows = conn.sqls[-1]
    events = {(r[0], r[2]) for r in rows}
    assert ("S_GONE", "delete_verified") in events
    assert ("S_STILL", "delete_not_effective") in events
    assert not any(r[0] == "S_WAIT" for r in rows)        # 未到期不落判
    assert any("仍在架" in m for m in caplog.messages)
