"""problem_scan 回归:归类规则、优先级、反补路由、四层去重、建议行产出。

批次 E 拆分前这些用例住在 test_problem_product_cleanup.py —— **决策逻辑搬到
哪,钉它的测试就跟到哪**。执行侧(消费建议行 + 发 feed)的用例留在原文件。
"""

import pytest

from services import problem_products as pp
from workflows import problem_scan as scan


def test_categorize_rules_and_priority():
    assert pp.categorize("The End Date has passed for this item") == ("A", "过期")
    assert pp.categorize("violates Prohibited Product Policy") == ("B", "禁售")
    # 同文本命中 C 与 A → 严重性顺序 C 先(具体归类优先,A 过期最后)
    both = "restricts certain brands ... end date has passed"
    assert pp.categorize(both)[0] == "C"
    assert pp.categorize("Intellectual Property complaint") == ("E", "知产")
    assert pp.categorize("完全无关的文本") == ("Z", "其他")
    assert pp.categorize(None) == ("Z", "其他")
    assert pp.is_stage_pending("in Stage status until you go live.")
    assert not pp.is_stage_pending("end date has passed")


def test_pick_product_id_and_relist_item():
    assert pp.pick_product_id("123456789012", "") == ("00123456789012", "GTIN")
    assert pp.pick_product_id("", "12345678") == ("000012345678", "UPC")
    assert pp.pick_product_id("1234567", "1234") is None      # 都不足 8 位
    item = pp.build_relist_item("SKU1", "123456789012", "")
    assert item["Orderable"]["sku"] == "SKU1"
    assert item["Orderable"]["endDate"] == pp.NEW_END_DATE
    assert item["Orderable"]["productIdentifiers"]["productIdType"] == "GTIN"
    assert pp.build_relist_item("S", "", "") is None


def _item(store, sku, reasons, gtin="123456789012"):
    return {"store": store, "sku": sku, "gtin": gtin, "upc": "", "reasons": reasons}


def test_plan_routing_and_dedup():
    """⚠ 逐字迁自拆分前的 test_problem_product_cleanup —— 断言一个字没改。
    批次 E 的验收标准就是"重排不改行为",这条用例是那把锁。"""
    items = [
        _item("T1", "S_A", "end date has passed"),            # → 反补
        _item("T1", "S_A2", "end date has passed", gtin=""),  # A 无 productId → 删除
        _item("T1", "S_AMAX", "end date has passed"),         # 反补满 2 次 → 删除
        _item("T1", "S_B", "prohibited product policy"),      # → 删除
        _item("T1", "S_STAGE", "stage status until you go live"),   # 排除
        _item("T1", "S_FLY", "intellectual property"),        # 在途 → 跳过
        _item("T_OFF", "S_X", "prohibited product policy"),   # 非 ACTIVE 店 → 跳过
        _item("T1", "S_ZOMBIE", "prohibited product policy"),  # 删除未生效 → 双击
    ]
    plans, n = scan.plan(items,
                         inflight={("T1", "S_FLY")},
                         attempts={("T1", "S_AMAX"): 2},
                         inactive={"T_OFF"},
                         stubborn={("T1", "S_ZOMBIE")})
    assert [r["sku"] for r in plans["T1"]["relist"]] == ["S_A"]
    # 顽固 SKU 停用+删除双 feed
    assert {r["sku"] for r in plans["T1"]["delete"]} == \
        {"S_A2", "S_AMAX", "S_B", "S_ZOMBIE"}
    assert [r["sku"] for r in plans["T1"]["retire"]] == ["S_ZOMBIE"]
    assert n["stubborn"] == 1
    assert "T_OFF" not in plans
    assert (n["stage"], n["inflight"], n["inactive"]) == (1, 1, 1)
    assert n["fallback"] == 1 and n["relist"] == 1 and n["delete"] == 3


def test_to_dispositions_splits_double_hit():
    """顽固双击 = **两条**建议行,不是一条。它们是两个 feed、两次独立的生效
    判定,合成一行会让其中一个的落定结果覆盖另一个。"""
    plans, _ = scan.plan([_item("T1", "S_Z", "prohibited product policy")],
                         inflight=set(), attempts={}, inactive=set(),
                         stubborn={("T1", "S_Z")})
    rows = scan.to_dispositions(plans)
    assert sorted(r["action"] for r in rows) == ["delete", "retire"]
    assert all(r["store"] == "T1" and r["sku"] == "S_Z" for r in rows)
    assert all(r["source"] == "scan" for r in rows)
    # gtin/upc 随建议行带走:执行件不回头查库,免得决策与执行看到的数据不一致
    assert rows[0]["detail"]["gtin"] == "123456789012"


def test_to_dispositions_carries_category_and_reason():
    plans, _ = scan.plan([_item("T1", "S_B", "violates Prohibited Product Policy")],
                         inflight=set(), attempts={}, inactive=set())
    (row,) = scan.to_dispositions(plans)
    assert (row["action"], row["category"]) == ("delete", "B")
    assert "Prohibited" in row["reason"]
    assert row["detail"]["cat_name"] == "禁售"


def test_stubborn_sql_binds_to_listing_generation():
    # 顽固标记绑定当前上架代际:最新事件是(重)上架 → 旧核验失效不再顽固
    assert "item_appeared" in scan._SQL_STUBBORN
    assert "item_reappeared" in scan._SQL_STUBBORN


def test_attempts_sql_counts_both_sources():
    """⚠ 拆分后必须同时认两个 source。历史反补事件的 source 是
    'problem_product_cleanup',漏掉它会让 30 天窗口内的历史计数归零,
    "反补满 2 次转删"重新从 0 数起 ⇒ 商品被无限反补。"""
    assert "source = ANY(%s)" in scan._SQL_ATTEMPTS
    assert "problem_product_cleanup" in scan._ATTEMPT_SOURCES
    assert "problem_scan" in scan._ATTEMPT_SOURCES


def test_inflight_sql_blocks_unobserved_success():
    # 在途/待观测拦截:feed 落定 success 但 catalog_sync 未重新观测
    # (resolved_at > last_seen_at)必须继续拦——否则落定后、扫店前重跑
    # 会把同一批 SKU 全量重发(2026-08-07 生产实证)
    assert "f.status = 'submitted'" in scan._SQL_INFLIGHT
    assert "f.resolved_at > w.last_seen_at" in scan._SQL_INFLIGHT
    assert "JOIN catalog.walmart_items" in scan._SQL_INFLIGHT


def test_audit_rejected_reads_the_view_not_its_own_join():
    """判据只有一处:catalog.audit_listing_conflicts 视图。
    这里原本抄了一份等价 JOIN,两份实现迟早漂 —— 口径要改只改视图那一处。
    视图同时是 audit_passed/audit_rejected 事件的第一个消费方
    (在此之前那 119 万条事件零读者)。"""
    assert "audit_listing_conflicts" in scan._SQL_AUDIT_REJECTED
    assert "rejected_still_listed" in scan._SQL_AUDIT_REJECTED
    # 不能自己再拼一份:出现这些说明又抄回来了
    assert "JOIN" not in scan._SQL_AUDIT_REJECTED.upper()
    assert "published_status" not in scan._SQL_AUDIT_REJECTED


def test_audit_rejected_respects_the_same_gates(monkeypatch):
    """审核来源与 scan 来源共用同一套闸:非 ACTIVE 店与在途都不建议。"""
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): pass
        def fetchall(self):
            # 视图的列序:store, sku, asin, audit_reason, rejected_after_listing
            return [("T1", "S1", "B01", "知产", True),
                    ("T_OFF", "S2", "B02", "禁售", False),
                    ("T1", "S_FLY", "B03", "禁售", False)]

    class _Conn:
        def cursor(self): return _Cur()

    rows = scan._audit_rejected_rows(_Conn(), inflight={("T1", "S_FLY")},
                                     inactive={"T_OFF"}, only=None)
    assert [r["sku"] for r in rows] == ["S1"]
    assert rows[0]["source"] == "audit" and rows[0]["action"] == "delete"
    assert rows[0]["asin"] == "B01" and "知产" in rows[0]["reason"]
    # 先上架后被判拒的标记随建议行带走:它是审核链漏拦的线索,
    # 与"该不该删"是两个问题,所以只进 detail 不改 action
    assert rows[0]["detail"]["rejected_after_listing"] is True
    assert rows[0]["action"] == "delete"


def test_preview_writes_nothing(monkeypatch):
    """preview=1 只打印:一条建议行都不许落(与危险工作流的 dry-run 同精神)。"""
    monkeypatch.setattr(scan, "_load_state", lambda: (
        [_item("T1", "S_B", "prohibited product policy")],
        set(), {}, {}, set(), set()))
    monkeypatch.setattr(scan.dispositions, "suggest_many",
                        lambda conn, rows: (_ for _ in ()).throw(
                            AssertionError("preview 不许写建议行")))
    monkeypatch.setattr(scan, "_audit_rejected_rows",
                        lambda conn, inflight, inactive, only: [])

    import contextlib
    from registry import db as _db
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    out = scan.run({"preview": "1"})
    assert "preview" in out and "删除 1" in out
    assert "类别={B:1}" in out and "删除样本=[('S_B', 'B')]" in out


def test_scan_never_submits_feeds():
    """扫描件的核心承诺:够不着 feed 提交入口。

    查的是**导入**不是源码文本 —— 文本里出现 "submit_feed" 完全正常
    (docstring 要解释防重口径在谁那儿),够不够得着它取决于有没有把
    api.feeds 拿进来。
    """
    import ast
    import inspect
    assert not hasattr(scan, "feeds"), "problem_scan 不该导入 api.feeds"
    tree = ast.parse(inspect.getsource(scan))
    imported = {n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module}
    imported |= {a.name for n in ast.walk(tree)
                 if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith("api") for m in imported), \
        f"扫描件只读库不碰外部接口,却导入了 {imported}"
    assert scan.DANGEROUS is False


@pytest.mark.parametrize("bad", [{"action": "nope"}, {"source": "nope"}])
def test_suggest_many_rejects_unknown_enums(bad):
    """拼错一个字符串会静默落一批永远没人领的建议行(执行件按 action 分桶,
    不认识的桶不会被消费)——宁炸不吞。"""
    from services import dispositions
    row = {"store": "T1", "sku": "S1", "action": "delete", **bad}
    with pytest.raises(ValueError, match="未知"):
        dispositions.suggest_many(object(), [row])


def test_conflicts_view_join_matches_its_index():
    """⚠ 生产事故的锁(2026-08-14):audit_listing_conflicts 的 LATERAL 用
    `coalesce(asin, sku)` 关联 product_events,而表达式索引必须与它**逐字一致**
    才会被用上。首版没有这个索引 ⇒ 对外层每行做一次几百万行全表扫描,查挂死。
    改任何一边都要同步改另一边,这条用例就是提醒。"""
    import pathlib as _p
    sql = _p.Path("refdata/schema.sql").read_text()
    view = sql[sql.index("CREATE VIEW catalog.audit_listing_conflicts"):]
    view = view[:view.index(") e ON true;")]
    assert "coalesce(ev.asin, ev.sku) = lr.asin" in view
    assert "((coalesce(asin, sku)), occurred_at DESC)" in sql, "身份键索引没了"
    # 外层过滤不许再引用 LATERAL 产出(那会让 PG 一行都剪不掉)
    tail = sql[sql.index("FROM live_rejected lr"):sql.index(") e ON true;")]
    assert "WHERE" not in tail.split("LEFT JOIN LATERAL")[0]
