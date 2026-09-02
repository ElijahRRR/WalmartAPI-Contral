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
    # 第一行带 store=T1 ⇒ INSERT 之前多一条登记簿 SELECT(批次 0b)
    sql, rows = conn.sqls[-1]
    assert "INSERT INTO catalog.product_events" in sql
    assert rows[0][0] == "S1" and '"feed_id"' in rows[0][6]
    assert rows[0][1] is None          # 'S1' 提不出标准码 → asin 存 NULL
    assert rows[1][1] is None and rows[1][5] is None      # store/detail 可空
    assert pe.record_many(conn, []) == 0                  # 空集不发 SQL


def test_record_many_resolves_asin_via_registry_when_store_present():
    """带 store 的行走登记簿:切码后 sku 是 12 位随机码,形态提取恒返 None ⇒
    事件账本的身份退化成按码分叉,product_risk 四个视图的 coalesce(asin, sku)
    全部失效,时间线断成一段一段(而且不报错)。"""
    conn = _Conn(fetch=[("T1", "AK7QM2X9RT4W", "B0ABCDEFGH")])
    pe.record_many(conn, [{"sku": "AK7QM2X9RT4W", "store": "T1",
                           "event": "item_missing", "source": "catalog_sync"}])
    reg_sql, _ = conn.sqls[0]
    assert "catalog.listing_sources" in reg_sql
    _, rows = conn.sqls[-1]
    assert rows[0][1] == "B0ABCDEFGH"


def test_record_many_falls_back_to_shape_for_platform_events():
    """store 为空的**平台级事件**没有店维度可查(登记簿主键是 (store, sku)),
    保持形态提取 —— 它们的 sku 本来就是 asin,extract_asin 恒等返回。
    这批行**一条 SELECT 都不该发**。"""
    conn = _Conn()
    pe.record_many(conn, [{"sku": "XKJ-B0GXX75JN5-39.98",
                           "event": "item_missing", "source": "catalog_sync"}])
    assert len(conn.sqls) == 1                      # 只有那条 INSERT
    _, rows = conn.sqls[-1]
    assert rows[0][1] == "B0GXX75JN5"


def test_record_many_issues_one_lookup_per_call():
    """一次调用一条批量 SELECT:最坏调用方 cleanup_history_import 每批 1 万行
    且**带 store**,逐行往返会把历史导入拖成天级。"""
    conn = _Conn()
    pe.record_many(conn, [{"sku": f"B0AAAAAA{i:02d}", "store": "T1",
                           "event": "item_missing", "source": "catalog_sync"}
                          for i in range(200)])
    assert len(conn.sqls) == 2                      # 一条 SELECT + 一条 INSERT


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


def test_verify_deletions_verdicts(caplog):
    import logging as _logging
    conn = _Conn(fetch=[("T1", "S_GONE", "gone"),
                        ("T1", "S_STILL", "still"),
                        ("T1", "S_WAIT", "wait")])
    with caplog.at_level(_logging.WARNING, logger="services.product_events"):
        gone, still, gone_pairs = pe.verify_deletions(conn)
    assert (gone, still) == (1, 1)
    # 第三元 = 判定为 gone 的 (店, SKU),与写进账本的 delete_verified 行一一
    # 对应 —— catalog_sync 拿它去弃码(弃码点 1)。still 与 wait 都不在里面。
    assert gone_pairs == [("T1", "S_GONE")]
    ins_sql, rows = conn.sqls[-1]
    events = {(r[0], r[3]) for r in rows}
    assert ("S_GONE", "delete_verified") in events
    assert ("S_STILL", "delete_not_effective") in events
    assert not any(r[0] == "S_WAIT" for r in rows)        # 未到期不落判
    assert any("仍在架" in m for m in caplog.messages)


def test_product_risk_view_exposes_sku_replaced_columns():
    """改码维度进 product_risk(SKU 改造批次 3 地基,S4)。

    所有者要能答"这个 ASIN 在这家店用过哪些码、为什么换"。不加这两列,
    sku_replaced 就是写了没人看 —— 与 2026-08-14 audit_passed/audit_rejected
    「零读者」是同一个坑。身份键仍是 coalesce(asin, sku)(新旧码经登记簿都解析到
    同一个 ASIN),所以改码天然落在同一条时间线上,不必改身份键。
    """
    import pathlib
    ddl = pathlib.Path("refdata/schema.sql").read_text(encoding="utf-8")
    view = ddl.split("CREATE VIEW catalog.product_risk AS")[1]
    view = view.split(";")[0]
    assert "count(*) FILTER (WHERE event = 'sku_replaced')" in view
    assert "AS sku_replaced_times" in view
    assert "max(occurred_at) FILTER (WHERE event = 'sku_replaced')" in view
    assert "AS last_sku_replaced_at" in view
    assert "coalesce(asin, sku) AS asin" in view       # 身份键没被动过
    assert "sku_replaced" in pe.EVENTS                 # 事件码早已登记(批次 2)
