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
    assert rows[0][0] == "S1" and '"feed_id"' in rows[0][6]
    assert rows[0][1] is None          # 'S1' 提不出标准码 → asin 存 NULL
    assert rows[1][1] is None and rows[1][5] is None      # store/detail 可空
    assert pe.record_many(conn, []) == 0                  # 空集不发 SQL


def test_receipt_in_ledger_whitelist():
    # 入账定稿(所有者 2026-08-07):生死类恒进;maintenance 仅反补来源进;
    # 改价/改库存/改标题/清库存不进(店铺维度操作,流水在 ops.feed_items)
    assert pe.receipt_in_ledger("delete", "product_clear")
    assert pe.receipt_in_ledger("retire", None)
    assert pe.receipt_in_ledger("maintenance", "problem_product_cleanup")
    assert not pe.receipt_in_ledger("maintenance", "maintenance")   # 标题/到期日期
    assert not pe.receipt_in_ledger("price_and_promotion", "price_sync")
    assert not pe.receipt_in_ledger("inventory", "maintenance")     # 清库存


def test_diff_catalog_transitions():
    old = {"A": ("PUBLISHED", None),          # 状态将变化
           "B": (None, "2026-08-01"),         # 曾缺席将重现(缺席行状态列已清空)
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
    # 复现只记 reappeared,不叠记 status_changed(old=None 是噪音)
    assert ("B", "status_changed") not in by
    changed = next(e for e in evs if e["event"] == "status_changed")
    assert changed["detail"] == {"old": "PUBLISHED", "new": "UNPUBLISHED",
                                 "reasons": "价格问题"}


def test_delete_verify_grace_is_46h():
    # 所有者定稿 2026-08-29(48h → 46h):观测一天一轮(13:00 开链)而回执
    # 落账必然晚于开链,48h 会让「未生效」判决结构性滑到第 3 轮(等效 ~72h);
    # 46h 给次次日观测留 ~2h 漂移余量,回执当天 ~15:00-15:30 前落账的第 2 轮
    # 即判。改这个数 = 改「后台处理上界」假设,先过所有者再同步这里。
    assert pe.DELETE_VERIFY_GRACE_HOURS == 46
    conn = _Conn()
    pe.verify_deletions(conn)
    _sql, args = conn.sqls[0]
    assert args == (46,)            # 缺省值真的接线到 SQL 参数,不是死在签名里


def test_verify_deletions_verdicts(caplog):
    import logging as _logging
    conn = _Conn(fetch=[("T1", "S_GONE", "gone"),
                        ("T1", "S_STILL", "still"),
                        ("T1", "S_WAIT", "wait")])
    with caplog.at_level(_logging.WARNING, logger="services.product_events"):
        gone, still = pe.verify_deletions(conn)
    assert (gone, still) == (1, 1)
    ins_sql, rows = conn.sqls[-1]
    events = {(r[0], r[3]) for r in rows}
    assert ("S_GONE", "delete_verified") in events
    assert ("S_STILL", "delete_not_effective") in events
    assert not any(r[0] == "S_WAIT" for r in rows)        # 未到期不落判
    assert any("仍在架" in m for m in caplog.messages)
