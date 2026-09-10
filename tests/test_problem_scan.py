"""problem_scan 回归:归类规则、优先级、一律删除路由、去重、建议行产出。

批次 E 拆分前这些用例住在 test_problem_product_cleanup.py —— **决策逻辑搬到
哪,钉它的测试就跟到哪**。执行侧(消费建议行 + 发 feed)的用例留在原文件。

2026-08-28 所有者定稿「非 PUBLISHED 一律删除,不再改 End Date 救商品」:
反补/Stage 豁免/反补计数的用例换成反向钉死(不许回来),路由用例改为全删。
"""

import os
import pathlib
import socket

import pytest

from registry import resources
from services import feed_track
from services import problem_products as pp
from workflows import problem_scan as scan


# ⚠ 2026-09-04:`test_categorize_rules_and_priority` 随 `problem_products.categorize()`
# 一起删 —— 所有者定「删除旧码」。problem_scan 的归类现在走
# `error_taxonomy.classify_reasons`(守门在 tests/test_error_taxonomy.py 的语料断言)。


def test_relist_machinery_is_retired_for_good():
    """反向钉死(2026-08-28 所有者定稿):反补机制退役,不许被接回来。

    「end date has passed」本身就是沃尔玛给退市商品打的标记(批量退市 =
    Site End Date 设为过去),把它当可修复故障反补 = 对退市档案走官方复活
    通道 —— 2026-08-28 沃尔玛把全账号档案翻回响应集那天,这条旧规则差点
    把上万条死档案批量救活。"""
    import inspect

    from services import dispositions as ds
    for name in ("build_relist_item", "pick_product_id", "is_stage_pending",
                 "NEW_END_DATE", "MAX_ATTEMPTS", "ATTEMPT_RESET_DAYS"):
        assert not hasattr(pp, name), f"problem_products.{name} 又被接回来了"
    assert not hasattr(scan, "drop_conflicting_relists")
    assert not hasattr(scan, "_SQL_ATTEMPTS")
    assert "relist" not in ds.PROBLEM_ACTIONS and "relist" not in ds.ACTIONS
    assert "relist" not in inspect.getsource(scan.plan)


def test_scan_surface_is_every_present_row():
    """扫描面 = 一切未缺席的行(所有者定稿 2026-09-10:「扫描面不再限制,按分类
    结果处置,所有状态的产品都需要扫描」)。状态列不再是筛选条件:
    2026-08-28 的「非 PUBLISHED」与 2026-09-06 的「RETIRED 全豁免」两条同日退役;
    只剩两条**操作层**边界(缺席行、在途改码的旧码)。"""
    q = scan._SQL_ITEMS
    assert "published_status <>" not in q and "published_status IS NOT NULL" not in q
    assert "lifecycle_status" not in q                       # RETIRED 豁免退役
    assert "IN ('UNPUBLISHED'" not in q                      # 旧白名单口径不许回来
    assert "missing_since IS NULL" in q
    assert "ls.replaced_by IS NOT NULL" in q
    import inspect
    assert "is_stage_pending" not in inspect.getsource(scan.plan)


def _item(store, sku, reasons):
    return {"store": store, "sku": sku, "reasons": reasons}


def test_plan_routing_and_dedup():
    """按原子归类处置(2026-09-10 定稿):单独的 End Date 过期 / Stage 不删,
    其余进删除桶;在途照旧跳过;非 ACTIVE 店照常建议;顽固双击照旧。"""
    items = [
        _item("T1", "S_A", "end date has passed"),            # 仅可恢复原子 → 留
        _item("T1", "S_B", "prohibited product policy"),      # → 删除
        _item("T1", "S_STAGE", "stage status until you go live"),  # 仅可恢复原子 → 留
        _item("T1", "S_FLY", "intellectual property"),        # 处置在途 → 跳过
        _item("T1", "S_NEW", "prohibited product policy"),    # 上架在途 → 跳过
        _item("T_OFF", "S_X", "prohibited product policy"),   # 非 ACTIVE 店 → 照常删(2026-09-10)
        _item("T1", "S_ZOMBIE", "prohibited product policy"),  # 删除未生效 → 双击
    ]
    # 2026-08-24 起在途计数拆两桶(跳过行为不变):处置在途 vs 上架/维护在途
    plans, n = scan.plan(items,
                         inflight={("T1", "S_FLY"), ("T1", "S_NEW")},
                         stubborn={("T1", "S_ZOMBIE")},
                         inflight_disposal={("T1", "S_FLY")})
    # 顽固 SKU 停用+删除双 feed;过期与 Stage 留下,其余进删除桶
    assert {r["sku"] for r in plans["T1"]["delete"]} == {"S_B", "S_ZOMBIE"}
    assert [r["sku"] for r in plans["T1"]["retire"]] == ["S_ZOMBIE"]
    assert "relist" not in plans["T1"]          # 反补桶不存在了
    assert n["stubborn"] == 1
    # 非 ACTIVE 店不再整店跳过(所有者 2026-09-10:「非 ACTIVE 店也需要在扫描范围内」)
    assert [r["sku"] for r in plans["T_OFF"]["delete"]] == ["S_X"]
    assert "inactive" not in n
    assert n["inflight"] == 1
    assert n["inflight_listing"] == 1        # S_NEW:上架 feed 在途,单列一桶
    assert n["delete"] == 2                  # S_B + S_X;双击那条不计在 delete(摘要按行重算)
    assert n["recoverable"] == 2             # S_A / S_STAGE
    # 留下的行照常归类(进病历/摘要),走向由 recoverable 标出
    for it in items:
        if it["sku"] in ("S_A", "S_STAGE"):
            assert it["recoverable"] is True and it["category"] in ("EXPIRED", "STAGE")
    assert items[1]["recoverable"] is False


def test_to_dispositions_splits_double_hit():
    """顽固双击 = **两条**建议行,不是一条。它们是两个 feed、两次独立的生效
    判定,合成一行会让其中一个的落定结果覆盖另一个。"""
    plans, _ = scan.plan([_item("T1", "S_Z", "prohibited product policy")],
                         inflight=set(),
                         stubborn={("T1", "S_Z")})
    rows = scan.to_dispositions(plans)
    assert sorted(r["action"] for r in rows) == ["delete", "retire"]
    assert all(r["store"] == "T1" and r["sku"] == "S_Z" for r in rows)
    assert all(r["source"] == "scan" for r in rows)


def test_to_dispositions_carries_category_and_reason():
    plans, _ = scan.plan([_item("T1", "S_B", "violates Prohibited Product Policy")],
                         inflight=set())
    (row,) = scan.to_dispositions(plans)
    assert (row["action"], row["category"]) == ("delete", "POLICY")
    assert "Prohibited" in row["reason"]
    # cat_name 现在是新码表的中文名(ERROR_CATEGORY_CODES);飞书「来源」列
    # 那一栏仍走 blacklist.source_label,故意留在旧词表上(禁售/品牌/知产…)
    assert row["detail"]["cat_name"] == "违反禁售政策"


def test_stubborn_sql_binds_to_listing_generation():
    # 顽固标记绑定当前上架代际:最新事件是(重)上架 → 旧核验失效不再顽固
    assert "item_appeared" in scan._SQL_STUBBORN
    assert "item_reappeared" in scan._SQL_STUBBORN


def test_disposal_feeds_no_longer_include_maintenance():
    """反补退役后本链不再发 MP_MAINTENANCE:在途的 MP_MAINTENANCE 都是维护链
    的字段操作,必须按「上架/维护在途」分档报,不能算处置在途。"""
    assert scan._DISPOSAL_FEEDS == ("DELETE_ITEM", "RETIRE_ITEM")


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
    """审核来源与 scan 来源共用同一套闸:在途不建议;非 ACTIVE 店照常建议
    (所有者 2026-09-10:「非 ACTIVE 店也需要在扫描范围内」,此前整店跳过)。"""
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

    rows = scan._audit_rejected_rows(
        _Conn(), inflight={("T1", "S_FLY")}, only=None)
    assert [r["sku"] for r in rows] == ["S1", "S2"]      # S2 在非 ACTIVE 店,照建议
    assert rows[0]["source"] == "audit" and rows[0]["action"] == "delete"
    assert rows[0]["asin"] == "B01" and "知产" in rows[0]["reason"]
    # 先上架后被判拒的标记随建议行带走:它是审核链漏拦的线索,
    # 与"该不该删"是两个问题,所以只进 detail 不改 action
    assert rows[0]["detail"]["rejected_after_listing"] is True
    assert rows[0]["action"] == "delete"


def test_audit_scan_no_longer_caps_but_stays_ordered():
    """单店删除上限**搬去执行件**(2026-08-24 归一),扫描件如实报待办。

    此前两条扫描件各按同一张限额表「下架限制」截一次 —— 每店最多 N 条实际
    变成了最多 2N。现在只有 problem_product_cleanup 领取时截一次
    (dispositions.cap_destructive)。

    扫描件仍按 (店铺, SKU) 定序:执行期按这个顺序取件,不定序的话每轮留下的
    是随机一批,削了几天也说不清削到哪儿了。
    """
    import inspect

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): pass
        def fetchall(self):
            return ([("T1", f"S{i:03d}", f"B{i:03d}", "禁售", False)
                     for i in (4, 0, 2, 1, 3)]
                    + [("T2", f"K{i:03d}", f"C{i:03d}", "知产", False)
                       for i in range(3)])

    class _Conn:
        def cursor(self): return _Cur()

    rows = scan._audit_rejected_rows(_Conn(), inflight=set(), only=None)
    assert len(rows) == 8                       # 一条都不截
    assert [r["sku"] for r in rows if r["store"] == "T1"] == [
        "S000", "S001", "S002", "S003", "S004"]
    # 上限的唯一出处已不在本文件(散在多处 = 改了一处另一处静默按旧规矩办)
    src = inspect.getsource(scan)
    assert "_AUDIT_DELETE_PER_STORE" not in src and "retire_caps" not in src


def test_preview_writes_nothing(monkeypatch):
    """preview=1 只打印:一条建议行都不许落(与危险工作流的 dry-run 同精神)。"""
    monkeypatch.setattr(scan, "_load_state", lambda: (
        [_item("T1", "S_B", "prohibited product policy")],
        set(), set(), {}, set(), set(), set(), set()))
    monkeypatch.setattr(scan.dispositions, "suggest_many",
                        lambda conn, rows: (_ for _ in ()).throw(
                            AssertionError("preview 不许写建议行")))
    monkeypatch.setattr(scan, "_audit_rejected_rows",
                        lambda conn, inflight, inactive, only, gone=None, perm=None: [])

    import contextlib
    from registry import db as _db
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    # 缺席避让走库里水位(store_absence),测试环境无库无飞书,置空
    monkeypatch.setattr(scan.store_absence, "stale_stores",
                        lambda conn, since=None, hours=None: [])
    out = scan.run({"preview": "1"})
    assert "preview" in out and "删除 1" in out
    assert "类别={POLICY:1}" in out and "删除样本=[('S_B', 'POLICY')]" in out


def test_absence_probe_failure_does_not_stop_the_scan(monkeypatch):
    """扫描件是**只读**件:缺席探测挂了按"不避让"照常出建议(fail-open),
    与破坏件 problem_product_cleanup 的 fail-closed 方向相反 —— preview 是纯
    PG 查询,不该被一次飞书抖动整个拦下。

    降级本身收在 services/store_absence.stale_or_note(四处同形,2026-08-27
    收口),这里钉的是**首行拼装**:分号由调用方补,措辞一个字都不许改。
    """
    monkeypatch.setattr(scan, "_load_state", lambda: (
        [_item("T1", "S_B", "prohibited product policy")],
        set(), set(), {}, set(), set(), set(), set()))
    monkeypatch.setattr(scan, "_audit_rejected_rows",
                        lambda conn, inflight, inactive, only, gone=None, perm=None: [])

    import contextlib
    from registry import db as _db
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))

    def _boom(conn, since=None, hours=None):
        raise RuntimeError("飞书抖了一下")
    monkeypatch.setattr(scan.store_absence, "stale_stores", _boom)
    out = scan.run({"preview": "1"})
    assert ";⚠ 缺席探测失败(RuntimeError),本轮不避让" in out.splitlines()[0]
    assert "删除 1" in out       # 不避让 = 一条候选都没被挡掉,本轮照常出建议


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


def test_conflicts_view_reads_identity_through_the_registry():
    """身份键收口(SKU 改造批次 0a):SKU 不再恒等 ASIN,amz 行的身份在登记簿。

    视图失效 = problem_scan 的「审核来源」建议归零,而且不报错 —— 它只是
    再也匹配不上任何一行。**source_type='amz' 不许省**:match 行的 source_key
    是匹配 GTIN,拿它去撞 products.asin 语义上就是错的。
    """
    import pathlib as _p
    sql = _p.Path("refdata/schema.sql").read_text()
    view = sql[sql.index("CREATE VIEW catalog.audit_listing_conflicts"):]
    view = view[:view.index(") e ON true;")]
    assert "ls.source_type = 'amz'" in view
    assert "p.asin = coalesce(ls.source_key, w.sku)" in view
    assert "p.asin = w.sku" not in view              # 硬等号已灭


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


def test_scan_and_audit_can_only_agree_on_delete():
    """反补退役后「救活 vs 删除」的矛盾在源头就不存在了:scan 与 audit 对
    同一 SKU 只可能都建议删除,由部分唯一索引合并(2026-08-14 那类矛盾剔除
    段随之删除)。这里钉的是 scan 侧产出的动作面。"""
    plans, _ = scan.plan([_item("T1", "S_A", "end date has passed"),
                          _item("T1", "S_Z", "prohibited product policy")],
                         inflight=set(),
                         stubborn={("T1", "S_Z")})
    actions = {r["action"] for r in scan.to_dispositions(plans)}
    assert actions <= {"delete", "retire"}


def test_withdraw_only_touches_own_source_and_suggested():
    """撤销只动**本来源**且仍是 suggested 的行:扫描件那一轮不该碰审核来源的
    建议(两个来源各跑各的闸);executing 更不能碰——feed 已经提交出去了,
    撤销无意义,它的归宿是 settle() 按观测判决。

    ⚠ 本用例只能断言 SQL **文本**,断不到 PG 的类型检查。首版这条 SQL 写成
    `(store, sku, action) <> ALL(...::text[][])`(record 比二维数组,类型不匹配,
    一跑就炸),而当时的同款断言全绿 —— **SQL 子串断言的盲区,记在这里**。
    真正能发现这类错的只有生产 dry-run。"""
    from services import dispositions
    sql = dispositions._WITHDRAW_SQL
    assert "d.status = 'suggested'" in sql        # 只动 suggested
    # 只动**本来源那一格**(多来源支撑,2026-08-24):整行撤会把另一条链还在
    # 支撑的建议一起干掉,而它撤不掉自己那一格 —— 08-19 那类合并行的病根
    assert "sources = d.sources - %(source)s::text" in sql
    assert "jsonb_exists(d.sources, %(source)s::text)" in sql
    assert "?" not in sql              # `?` 在若干驱动里会被当占位符
    assert "'executing'" not in sql
    # 三个平行数组 + 多参数 unnest:别退回 record <> ALL(二维数组) 那种写法
    assert "unnest(%(stores)s::text[], %(skus)s::text[]," in sql
    assert "<> ALL" not in sql


def test_withdraw_passes_three_parallel_arrays(monkeypatch):
    """参数必须是**三个平行数组**且逐位对齐 —— 错位会撤错行(撤掉本轮仍在
    建议的、留下本轮已不建议的),而两边行数一样,不会报错。"""
    from services import dispositions
    seen = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): seen.update(params or {})
        def fetchall(self):
            # 合并行(另一条链还在支撑)只是少了一格,不算"已撤销"
            return [(1, "withdrawn"), (2, "withdrawn"), (3, "suggested")]

    class _Conn:
        def cursor(self): return _Cur()

    n = dispositions.withdraw_stale(
        _Conn(), "scan", [("T1", "S1", "delete"), ("T2", "S2", "relist")], "x")
    assert n == 2
    assert seen["stores"] == ["T1", "T2"]
    assert seen["skus"] == ["S1", "S2"]
    assert seen["actions"] == ["delete", "relist"]


def test_withdraw_scoped_to_scanned_store():
    """⚠ `-p store=X` 只扫一个店,那一轮的 keep 里只有该店的行 —— 撤销不限
    范围就会把**其余全部店铺**的待执行建议一次清空。扫了哪个范围就只能撤
    哪个范围。"""
    from services import dispositions
    # ::text 不是装饰:少了它 PG 报 "could not determine data type of
    # parameter"(参数只出现在 IS NULL 与一次比较里,推不出类型)——生产实炸过
    assert "(%(store)s::text IS NULL OR d.store = %(store)s::text)" \
        in dispositions._WITHDRAW_SQL
    seen = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): seen.update(params or {})
        def fetchall(self): return []
        rowcount = 0

    class _Conn:
        def cursor(self): return _Cur()

    dispositions.withdraw_stale(_Conn(), "scan", [("T1", "S1", "delete")],
                                "x", store="T1")
    assert seen["store"] == "T1"
    # 全量扫传 None = 不限范围(此时 keep 覆盖全库,撤销才安全)
    dispositions.withdraw_stale(_Conn(), "scan", [("T1", "S1", "delete")], "x")
    assert seen["store"] is None


def test_withdraw_empty_keep_also_respects_store():
    """本轮一条都不建议时走的是另一条 SQL —— 那条同样必须带范围闸,
    否则单店扫描扫出零建议会清空全库(最坏的组合)。"""
    from services import dispositions
    seen = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            seen["sql"] = sql
            seen.update(params or {})
        def fetchall(self): return [("withdrawn",), ("suggested",)]

    class _Conn:
        def cursor(self): return _Cur()

    n = dispositions.withdraw_stale(_Conn(), "scan", [], "x", store="T9")
    flat = " ".join(seen["sql"].split())
    assert "(%(store)s::text IS NULL OR d.store = %(store)s::text)" in flat
    assert seen["store"] == "T9"
    assert n == 1           # 只数真撤掉的,合并行少一格不算


def test_every_sql_param_is_cast():
    """⚠ **lint 式护栏,不是风格洁癖。**

    services/dispositions 的 SQL 因为 PG 推不出参数类型,在生产上连炸三次,
    而每次 pytest 都是全绿:
      ① `record <> ALL(text[][])`                类型不匹配
      ② `%(store)s IS NULL OR store = %(store)s` IS NULL 不提供类型信息
      ③ `jsonb_build_object('k', %(why)s)`       该函数收 any,无从推断
    本仓的 SQL 用例只断言**文本子串**,PG 的类型推断根本跑不到 —— "下次小心"
    不是能执行的结论,"每个参数都带 ::类型"才是,而且能被这条用例机械检查。

    只扫模块级 SQL 常量(注释里的反例不算)。
    """
    import re
    src = pathlib.Path("services/dispositions.py").read_text()
    bad = []
    for m in re.finditer(r'^(_\w*SQL)\s*=\s*"""(.*?)"""', src, re.S | re.M):
        for pm in re.finditer(r"%\((\w+)\)s(?!\s*::)", m.group(2)):
            bad.append(f"{m.group(1)}.{pm.group(1)}")
    assert not bad, ("这些 SQL 参数没写显式 ::类型,PG 可能推不出来(生产实炸三次):"
                     + ", ".join(bad))



def test_summarize_counts_the_rows_that_actually_land():
    """⚠ 摘要必须报**真正会落库**的数(2026-08-14 生产实遇)。

    钉一条老坑:按建议行统计,不按 plan() 的桶 —— n['delete'] 不含顽固双击
    那批(那支 continue 前没有 n['delete'] += 1),照它报会少一大截。
    """
    allrows = [
        {"store": "T1", "sku": "S1", "action": "delete", "category": "B",
         "source": "scan"},
        {"store": "T1", "sku": "S2", "action": "delete", "category": None,
         "source": "audit"},
        {"store": "T1", "sku": "S3", "action": "retire", "category": "B",
         "source": "scan"},
    ]
    audit_rows = [allrows[1]]
    n = {"inflight": 2, "inflight_listing": 0, "inactive": 3, "delete": 1,
         "gone": 0, "permanent": 0}
    head = _summ(allrows, audit_rows, n, 99)
    # 删除报 2(retire 之外的全部 delete 行),不是 n['delete'] 的 1
    assert "删除 2" in head[0]
    assert "扫描 99 行" in head[0]          # 2026-09-10 起扫描面是目录全量
    assert "顽固停用 1" in head[0]
    assert "其中审核判拒 1" in head[0]
    # 分店明细按建议行重建,audit 来源没有 category → 显示 '-'
    t1 = [l for l in head if l.startswith("  T1")][0]
    assert "B:1" in t1 and "-:1" in t1


def _summ(*a):
    return scan._summarize(*a)


def test_count_open_is_not_the_write_count():
    """suggest_many 报"写了多少次",count_open 报"库里有多少条" —— 两者会差,
    差额是被唯一索引合并的条数(本轮实测 519 次写入 → 库里 470 条)。
    执行件领走的是后者,摘要要报的也是后者。

    2026-08-16 加了 sources 过滤(维护链共用同一张建议表):**必须限本链来源**,
    否则摘要把维护链的待执行也算进来,又和执行件领到的数对不上。"""
    import inspect

    from services import dispositions
    src = inspect.getsource(dispositions.count_open)
    assert "SELECT count(*) FROM ops.dispositions WHERE status = %(st)s::text" in src
    assert "source = ANY(%(sources)s::text[])" in src
    # 调用方必须传 sources —— 不传就退化成"全表计数"
    scan_src = inspect.getsource(scan.run)
    assert "sources=dispositions.PROBLEM_SOURCES" in scan_src


def test_summarize_dedupes_like_the_unique_index():
    """⚠ 摘要必须按 (店铺,SKU,动作) 去重(2026-08-14 第二次修)。

    同一个 SKU 被 scan 与 audit 双双建议删除时,建议行数组里是两条,但落库被
    部分唯一索引合成一条 —— 不去重就会报 489 而执行件只领到 440,两个摘要
    对不上账,分店明细里还会看到同一个 SKU 出现两次(生产实遇:
    A121许家蕴 的 B094F29JB6 在删除样本里出现两遍)。

    去重口径与 upsert 一致:**后写的赢**(executemany 按序,audit 排在 scan
    之后,所以 category 被 audit 的 None 覆盖)。摘要如实显示,不美化。
    """
    same = ("T1", "SDUP", "delete")
    allrows = [
        {"store": same[0], "sku": same[1], "action": same[2],
         "category": "A", "source": "scan"},
        {"store": same[0], "sku": same[1], "action": same[2],
         "category": None, "source": "audit"},
        {"store": "T1", "sku": "SOLO", "action": "delete",
         "category": "B", "source": "scan"},
    ]
    n = {"fallback": 0, "stage": 0, "inflight": 0, "inflight_listing": 0,
         "gone": 0, "permanent": 0,
         "inactive": 0}
    head = scan._summarize(allrows, [allrows[1]], n, 3)
    assert "删除 2" in head[0]          # 不是 3
    assert "其中审核判拒 1" in head[0]
    t1 = [l for l in head if l.startswith("  T1")][0]
    assert t1.count("SDUP") == 1        # 分店明细里也只出现一次
    assert "-:1" in t1 and "B:1" in t1  # 后写的赢 ⇒ SDUP 的 category 是 None


# ── L 类 + K 聚集 + 政策名缺口(2026-08-24 审核反哺批;L 类 2026-08-28 改删)──

def test_l_system_error_now_deletes_like_everything_else():
    """L 类(internal error)2026-08-24 曾走反补(沃尔玛原话 Resubmit);
    2026-08-28 所有者定稿推翻;2026-09-10「按原子归类」口径下 SYSTEM 不在
    可恢复码里(只有 EXPIRED/STAGE),仍然删除。归类仍是 SYSTEM(病历/摘要照记)。"""
    items = [
        _item("T1", "S_L1", "an internal error occurred while publishing"),
        _item("T1", "S_L2", "an internal error occurred"),
    ]
    plans, n = scan.plan(items, inflight=set())
    assert {r["sku"] for r in plans["T1"]["delete"]} == {"S_L1", "S_L2"}
    assert all(r["category"] == "SYSTEM" for r in plans["T1"]["delete"])
    assert n["delete"] == 2


def test_k_cluster_note_fires_on_concentration():
    """「内部标记」单条无信息量,聚集才是信号(实测谭总11 一店 45 条)。"""
    items = [{"store": "T1", "sku": f"S{i}", "category": "FLAGGED"}
             for i in range(scan._K_CLUSTER_WARN)]
    note = scan._k_cluster_note(items)
    assert "T1" in note and "风险" in note
    assert scan._k_cluster_note(items[:3]) == ""     # 没到阈值不吵


def test_policy_gap_note_reports_unknown_policy_names():
    """沃尔玛点名了政策而政策表没有 ⇒ L3 的 S4 块看不见它,注定漏。
    政策表无同步器(audit_import 一次性),缺口靠这里天天报。"""
    class _Cur:
        def execute(self, sql): pass
        def fetchall(self):
            return [("Children's Products",), ("Hazardous Items",)]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn2:
        def cursor(self): return _Cur()

    items = [
        {"store": "T1", "sku": "A",
         "reasons": "violating Walmart's Prohibited Product Policy on "
                    "Made in USA claims. Items with..."},
        {"store": "T1", "sku": "B",
         "reasons": "||Children's Products Prohibited Products Policy@@@x"},
        {"store": "T1", "sku": "C",
         "reasons": "violating Walmart's Marketplace *Prohibited Product "
                    "Policy*."},
    ]
    note = scan._policy_gap_note(_Conn2(), items)
    assert "Made in USA claims" in note        # 未收录 → 报
    assert "Children's Products" not in note   # 已收录 → 不报
    # 裸政策名(没点名)不制造噪音
    class _CurAll(_Cur):
        def fetchall(self):
            return [("Children's Products",), ("Made in USA claims",)]
    class _ConnAll(_Conn2):
        def cursor(self): return _CurAll()
    assert scan._policy_gap_note(_ConnAll(), items) == ""


# ── 破坏类回执的两道闸:死档 / 永久拒(2026-09-09 由 WFS 那一道泛化)─────────

def test_dead_listing_skus_are_skipped_not_re_deleted_every_round():
    """死档 SKU 跳过并计数,**不再每天空发一次注定失败的 DELETE_ITEM**。

    2026-09-09 全船队实证:约 800 条 delete/retire 卡在 executing,回执里
    ~450 条「Incoming Itemid does not exist in Matching」、~200 条「This SKU has
    been deleted/retired」、数百条 QARTH「No matching record found」——
    沃尔玛侧这些 SKU 早就不在了,破坏动作的目的已达成,再发只是烧配额。
    """
    items = [_item("T1", "S_DEL", "prohibited product policy"),
             _item("T1", "S_OK", "prohibited product policy")]
    plans, n = scan.plan(items, set(), set(),
                         gone_blocked={("T1", "S_DEL")})
    assert n["gone"] == 1 and n["permanent"] == 0
    assert [r["sku"] for r in plans["T1"]["delete"]] == ["S_OK"]


def test_permanent_refusal_skus_are_skipped_and_counted_separately():
    """永久拒(WFS 不许删 / RETIRE 的 ERR_PDI_0004)与死档**分开计数**。

    两件事的下一步不同:死档是"事情已经成了,不用再管";永久拒是"要人去
    Seller Center 处理(转出 WFS)"。合成一个数就等于把待办混进已完成。
    ⚠ 拦掉不等于改判 retire:RETIRE_ITEM 对 WFS 件行不行官方没有明文,
    按本仓纪律不许按推断编码 —— 只跳过并响亮报数。
    (2026-09-10 起单独的「End Date 过期」按可恢复留、到不了回执闸,
    夹具用政策原因。)
    """
    it = _item("T1", "S_POL", "prohibited product policy")
    plans, n = scan.plan([it], set(), set(), perm_blocked={("T1", "S_POL")})
    assert n["permanent"] == 1 and n["gone"] == 0 and n["delete"] == 0
    assert plans.get("T1", {"delete": []})["delete"] == []


def test_both_receipt_gates_also_block_the_stubborn_double_feed():
    """顽固件的 retire+delete 双发同样拦(两桶都拦)。

    死档:delete 与 retire 都会拿到同一句「这个 SKU 不在了」;
    永久拒:delete 注定被拒,而 retire 对 WFS 件行不行官方没有明文。
    两种情形都整条跳过并报数,不按推断编码。
    """
    for bucket, key in (("gone_blocked", "gone"),
                        ("perm_blocked", "permanent")):
        items = [_item("T1", "S_Z", "prohibited product policy")]
        plans, n = scan.plan(items, set(), stubborn={("T1", "S_Z")},
                             **{bucket: {("T1", "S_Z")}})
        assert n[key] == 1 and n["stubborn"] == 0, bucket
        assert plans.get("T1", {"delete": [], "retire": []})["delete"] == []
        assert plans.get("T1", {"delete": [], "retire": []})["retire"] == []


def test_the_named_samples_match_the_counts_in_the_headline():
    """摘要两处的数必须对得上:总览那行的 `n['gone']`/`n['permanent']` 与
    明细里点名的条数,排除顺序都是「在途 → 死档 → 永久拒」(店铺状态不设闸)。

    对不齐的老坑(2026-08-14 生产实遇):两个数都"看起来对",人拿其中一个去
    对账才发现少了一截,而两边都不报错。
    """
    items = [_item("T1", "S_GONE", "x"), _item("T1", "S_PERM", "x"),
             _item("T1", "S_INFLIGHT", "x"), _item("T2", "S_DEAD_STORE", "x")]
    inflight = {("T1", "S_INFLIGHT")}
    gone = {("T1", "S_GONE"), ("T1", "S_INFLIGHT"), ("T2", "S_DEAD_STORE")}
    perm = {("T1", "S_PERM")}
    _plans, n = scan.plan(items, inflight, set(), inflight, gone, perm)
    # 在途的**先被在途闸拦走**,不算进这两桶;非 ACTIVE 店的行照常走到回执闸
    # (2026-09-10 店铺状态不设闸),S_DEAD_STORE 算进死档桶
    assert (n["gone"], n["permanent"], n["inflight"]) == (2, 1, 1)


def test_receipt_blocked_sql_reads_only_the_latest_attempt_of_both_feeds():
    """口径三条,一条都不能少(SQL 的唯一出处已在 services/feed_track):

    ① **最近一次**尝试,不是"历史上出现过就永久拉黑" —— 商品转出 WFS 之后
       就该能删了,下一次尝试的回执会把它放出来;写成 EXISTS 的话它永远删不了
       而且没人看得出是被自己的闸拦着;
    ② **DELETE_ITEM 与 RETIRE_ITEM 都算**:只看删不看停,顽固件的 retire
       那一半会继续每天空烧配额;
    ③ **不限定 status**:死档码 EXT_DATA_ERROR_60745664660159(QARTH)是
       `status=success` 带回来的,只收 failed/missing 就永远收不到那几百条。
    """
    q = feed_track._RECEIPT_BLOCKED_SQL
    assert "DISTINCT ON (store, sku)" in q
    assert "ORDER BY store, sku, submitted_at DESC" in q
    assert "f.feed_type = ANY(%(feeds)s::text[])" in q
    assert feed_track.DESTRUCTIVE_FEED_TYPES == ("DELETE_ITEM", "RETIRE_ITEM")
    assert "error_code = ANY(%(codes)s::text[])" in q
    assert "status IN ('failed', 'missing')" not in q      # 旧口径不许回来


def test_the_error_codes_live_only_in_the_registry():
    """码集的**唯一出处**是 registry(铁律 3:业务代码禁止散落错误码字面量)。

    原来 `problem_scan._WFS_BLOCKED_CODE` 就是一个散落的字面量;泛化成七个码
    之后再散落一次,后果是改一处另一处静默按旧清单办 —— 闸看起来还在,
    实际漏掉了新登记的码,而且不报错。
    """
    assert not hasattr(scan, "_WFS_BLOCKED_CODE")
    assert not hasattr(scan, "_SQL_WFS_BLOCKED")
    # 两集合不相交:同一条回执的同一个码不可能既是"已经不在了"又是"不许删"
    assert not (resources.WALMART_ERR_ITEM_GONE
                & resources.WALMART_ERR_DESTRUCTIVE_PERMANENT)
    codes = (resources.WALMART_ERR_ITEM_GONE
             | resources.WALMART_ERR_DESTRUCTIVE_PERMANENT)
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for d in ("services", "workflows", "registry", "api"):
        for f in sorted((root / d).rglob("*.py")):
            if "__pycache__" in f.parts or f.name == "resources.py":
                continue
            text = f.read_text(encoding="utf-8")
            offenders += [f"{f.relative_to(root)}:{c}"
                          for c in codes if c in text]
    assert not offenders, ("错误码字面量散落到业务代码里了,改引用 "
                           "registry.resources 的两个 frozenset:" + str(offenders))


# ══════════════════════════════════════════════════════════════════════════════
#  在途改码:扫描面排除(O4)+ 四段历史判据沿改码链继承一跳(O5/O6)
#
#  这一节是本批次**唯一能造成不可逆损失**的路径的回归:改码生效有 15 分钟到
#  4 小时的窗口(官方),窗口内旧码可能被观测成非 PUBLISHED 且未缺席,正好落进
#  扫描面被建议 DELETE_ITEM —— 一次**成功**的改码被自己的自动链当场永久删掉。
#  四段历史判据则相反:新码在 product_events / ops.feed_items 里一条历史都没有,
#  不继承就同时失明(顽固加压丢失、归类每次当第一次见、WFS 拦截失效每天空烧
#  DELETE_ITEM 配额、在途防重拦不住)。
# ══════════════════════════════════════════════════════════════════════════════

def test_items_sql_column_order_is_unchanged():
    """四列的**位置顺序**是契约:_load_state 按位置解包成
    store/sku/reasons/published_status。

    加表别名时把列序动了,不报错 —— 只是从此每一行的 sku 里装着
    unpublished_reasons,归类全错、建议全错。
    """
    head = scan._SQL_ITEMS.strip().splitlines()[0]
    assert head == "SELECT w.store, w.sku, w.unpublished_reasons, w.published_status"
    src = pathlib.Path("workflows/problem_scan.py").read_text(encoding="utf-8")
    assert '("store", "sku", "reasons", "published_status")' in src


def test_replaced_rows_are_excluded_by_a_not_exists_not_a_join():
    """排除写成 NOT EXISTS 而不是 JOIN:扫描面的行数不许被登记簿改变。

    JOIN 登记簿会顺手把**未登记**的在架问题行也一起丢掉(存量有一批),
    那是静默缩小扫描面 —— 与本条要挡的事完全无关。
    """
    q = scan._SQL_ITEMS
    assert "NOT EXISTS (SELECT 1 FROM catalog.listing_sources ls" in q
    assert "ls.replaced_by IS NOT NULL" in q
    assert "JOIN catalog.listing_sources" not in q
    # 改码前 replaced_by 全库为 NULL ⇒ NOT EXISTS 恒真,既有的缺席条件一字未动
    # (状态条件 2026-09-10 退役,见 test_scan_surface_is_every_present_row)
    assert "w.missing_since IS NULL" in q


def test_three_history_sqls_and_inflight_go_through_sku_aliases_only():
    """四段一律经 catalog.sku_aliases 取别名 —— 判据只能有一处出生。

    各自现写一遍登记簿指针的 JOIN 就是四份会各自漂移的实现,而漂了不报错:
    只是某一处从此看不见历史(守门 test_sku_guard 那条从源码层面钉同一件事)。
    """
    for name, mod in (("_SQL_STUBBORN", scan), ("_SQL_LAST_CAT", scan),
                      ("_RECEIPT_BLOCKED_SQL", feed_track),
                      ("_SQL_INFLIGHT", scan)):
        q = getattr(mod, name)
        assert "catalog.sku_aliases a" in q, name
        assert q.count("UNION ALL") == 1, name          # 只继承一跳
        assert "f.sku = a.alias_sku" in q or "e.sku = a.alias_sku" in q, name
        assert "listing_sources" not in q, name         # 只准经视图


def test_history_sql_placeholders_are_named_not_positional():
    """UNION 之后同一个值要用两次 ⇒ `%s` 位置参数给不了两遍(照抄会 ProgrammingError)。

    两个消费点的实参必须同步改成 dict —— 只改 SQL 不改调用点,是本批次最容易
    漏的一处,而它当场炸(这条测试只是让它在 pytest 里炸,不在生产里炸)。
    """
    assert "%(ev)s" in scan._SQL_LAST_CAT and "%s" not in scan._SQL_LAST_CAT
    assert "%(codes)s" in feed_track._RECEIPT_BLOCKED_SQL
    assert "%s" not in feed_track._RECEIPT_BLOCKED_SQL
    src = pathlib.Path("workflows/problem_scan.py").read_text(encoding="utf-8")
    assert '_SQL_LAST_CAT, {"ev": product_events.PROBLEM_CATEGORIZED}' in src
    assert "feed_track.receipt_blocked(" in src


# ── 沙箱 PG 集成:四段 SQL 的真实语义 ────────────────────────────────────────
#
# ⚠ 地址是**测试夹具**,不是生产资源(生产走 registry/db.pg_dsn())。固定在
# 非标准端口 55432 上正是为了不可能连到生产库;造的数据全在一个最后回滚的
# 事务里,不留残渣。文本断言证明不了"UNION ALL 加空集结果不变"这类事,只有
# 真库能。
_PG_HOST, _PG_PORT = "127.0.0.1", 55432
_DSN = os.environ.get(
    "WALMART_TEST_PG_DSN",
    f"host={_PG_HOST} port={_PG_PORT} user=postgres dbname=walmart_data")


def _pg_up() -> bool:
    try:
        with socket.create_connection((_PG_HOST, _PG_PORT), timeout=1):
            return True
    except OSError:
        return False


needs_pg = pytest.mark.skipif(not _pg_up(),
                              reason=f"沙箱 PG {_PG_HOST}:{_PG_PORT} 未启动")

_STORE = "PSCAN_T1"
#: 夹具用的永久拒码(WFS 件不许删)。取自 registry —— 测试里也不写字面量,
#: 否则码表一改,夹具与生产代码就对不上,而用例照绿。
_PERM_CODE = sorted(resources.WALMART_ERR_DESTRUCTIVE_PERMANENT)[0]
_OLD, _NEW = "B0PSCANOLD1", "APSCAN234567"      # 旧码 = 裸 ASIN 形态;新码 = 不透明码


@pytest.fixture
def pg(monkeypatch):
    """输入:无 → 输出:沙箱 PG 连接(整场事务**最后一律回滚**)。

    连接只准走 registry/db(工程规范:禁止自行 psycopg.connect)。
    """
    monkeypatch.setenv("WALMART_PG_DSN", _DSN)
    from registry import db
    with db.pg_conn() as conn:
        try:
            yield conn
        finally:
            conn.rollback()


def _seed_pair(conn, *, replaced: bool):
    """输入:连接 + 是否已建立改码指针 → 输出:无。

    造一对新旧码:旧码在架(改码窗口内被观测成 UNPUBLISHED)、新码在架,
    历史(顽固/归类/WFS/在途)**全挂在旧码名下**。
    replaced=False 时两行毫无关系,正是"改码前"的对照组。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO catalog.walmart_items "
            "(store, sku, published_status, missing_since, last_seen_at) VALUES "
            "(%s, %s, 'UNPUBLISHED', NULL, now() - interval '1 hour'),"
            "(%s, %s, 'PUBLISHED',   NULL, now() - interval '1 hour')",
            (_STORE, _OLD, _STORE, _NEW))
        cur.execute(
            "INSERT INTO catalog.listing_sources (store, sku, source_type,"
            " source_key, workflow, replaces, replaced_by) VALUES "
            "(%s, %s, 'amz', %s, 'test', NULL, %s),"
            "(%s, %s, 'amz', %s, 'test', %s, NULL)",
            (_STORE, _OLD, _OLD, _NEW if replaced else None,
             _STORE, _NEW, _OLD, _OLD if replaced else None))
        cur.execute(
            "INSERT INTO catalog.product_events (sku, store, event, source,"
            " detail, occurred_at) VALUES "
            "(%s, %s, 'delete_not_effective', 'test', NULL, now() - interval '2 days'),"
            "(%s, %s, 'problem_categorized', 'test', '{\"category\": \"L\"}'::jsonb,"
            " now() - interval '2 days')",
            (_OLD, _STORE, _OLD, _STORE))
        cur.execute(
            "INSERT INTO ops.feed_items (feed_id, sku, workflow, store, feed_type,"
            " status, error_code, submitted_at) VALUES "
            "('F_WFS', %s, 'problem_product_cleanup', %s, 'DELETE_ITEM',"
            " 'failed', %s, now() - interval '3 days'),"
            "('F_INF', %s, 'list_new', %s, 'MP_ITEM', 'submitted', NULL, now())",
            (_OLD, _STORE, _PERM_CODE, _OLD, _STORE))


def _read(conn):
    """输入:连接 → 输出:四段判据对 (店, 新码) 的结论 + 扫描面里的 SKU 集合。

    第四段(原「WFS 拦截」)2026-09-09 泛化成 `feed_track.receipt_blocked`,
    这里读的是**永久拒**那一桶(夹具造的正是一条 WFS 回执)。
    """
    from services import product_events
    with conn.cursor() as cur:
        cur.execute(scan._SQL_ITEMS)
        surface = {sku for st, sku, *_ in cur.fetchall() if st == _STORE}
        cur.execute(scan._SQL_STUBBORN)
        stubborn = {(st, k) for st, k, ev in cur.fetchall()
                    if ev == 'delete_not_effective'}
        cur.execute(scan._SQL_LAST_CAT, {"ev": product_events.PROBLEM_CATEGORIZED})
        last_cat = {(s, k): c for s, k, c in cur.fetchall()}
        perm = feed_track.receipt_blocked(
            conn, resources.WALMART_ERR_DESTRUCTIVE_PERMANENT)
        cur.execute(scan._SQL_INFLIGHT, {"disposal": list(scan._DISPOSAL_FEEDS)})
        inflight = {(st, k): d for st, k, d in cur.fetchall()}
    return surface, stubborn, last_cat, perm, inflight


@needs_pg
def test_rows_being_replaced_are_out_of_the_scan_surface(pg):
    """在途改码的旧码**不进扫描面**(O4)—— 本批次唯一的不可逆损失路径。

    改码窗口内旧码被观测成 UNPUBLISHED 且 missing_since 仍为 NULL,三条既有
    条件全部命中;不排除的话当轮就建议 DELETE_ITEM,而 DELETE 不可逆:我们
    **正在**改的那个 item 会被自己的自动链删掉。
    """
    _seed_pair(pg, replaced=True)
    surface, *_ = _read(pg)
    assert _OLD not in surface
    # 新码在架且 PUBLISHED:2026-09-10 起扫描面不按状态筛,它**在**扫描面里
    # (无原因 ⇒ plan() 分进 clean 桶,不是候选);排除的只是旧码,不是整对
    assert _NEW in surface


@needs_pg
def test_ordinary_unpublished_rows_still_enter_the_scan_surface(pg):
    """对照组:没有改码指针的同一行照进扫描面(排除的判据只有 replaced_by)。"""
    _seed_pair(pg, replaced=False)
    surface, *_ = _read(pg)
    assert _OLD in surface


@needs_pg
def test_stubborn_marker_follows_the_replacement_chain(pg):
    """顽固代际继承一跳:旧码的 delete_not_effective 要算在新码头上(O5)。

    不继承 = 新码是一张白纸 ⇒ 顽固件的双 feed 加压静默丢失,回到"每天删一次
    删不掉"的循环(注释记着这条已实证的故障模式)。
    """
    _seed_pair(pg, replaced=True)
    _, stubborn, _, _, _ = _read(pg)
    assert (_STORE, _NEW) in stubborn
    assert (_STORE, _OLD) in stubborn          # 旧码自己的结论不受影响


@needs_pg
def test_last_category_follows_the_replacement_chain(pg):
    """问题归类的最近类别继承一跳:不继承的话每一轮都当第一次见,重复记事件。"""
    _seed_pair(pg, replaced=True)
    _, _, last_cat, _, _ = _read(pg)
    assert last_cat[(_STORE, _NEW)] == "L"
    assert last_cat[(_STORE, _OLD)] == "L"


@needs_pg
def test_receipt_block_follows_the_replacement_chain(pg):
    """回执闸继承一跳:不继承就每天重建议、重发、同一个错、白烧
    DELETE_ITEM 的 6/hour 桶(生产实见 11 条 WFS 件)。"""
    _seed_pair(pg, replaced=True)
    _, _, _, perm, _ = _read(pg)
    assert (_STORE, _NEW) in perm


@needs_pg
def test_inflight_follows_the_replacement_chain(pg):
    """在途防重继承一跳(O6):旧码上没落定的上架/维护/处置 feed 指着的是
    **同一个 item**,新码不该被当成"从没提交过任何东西"。

    在途口径**有意不分 feed 类型**(QARTH 复审期内追发 DELETE_ITEM 属于过早),
    所以继承过来的 MP_ITEM 也算在途,只是 disposal=False(分档报数不受影响)。
    """
    _seed_pair(pg, replaced=True)
    *_, inflight = _read(pg)
    assert inflight.get((_STORE, _NEW)) is False    # 在途,但不是处置类
    assert inflight.get((_STORE, _OLD)) is False


@needs_pg
def test_four_history_sqls_are_unchanged_when_sku_aliases_is_empty(pg):
    """**零行为变化的正面证据**:sku_aliases 为空集时四段结论与改码前逐行相同。

    UNION ALL 加空集 = 原结果集;LEFT JOIN 空视图 = 全落 NULL。文本断言证明
    不了这件事,只有真库能。
    """
    _seed_pair(pg, replaced=False)
    with pg.cursor() as cur:
        cur.execute("SELECT count(*) FROM catalog.sku_aliases")
        assert cur.fetchone()[0] == 0              # 改码前恒空集
    surface, stubborn, last_cat, perm, inflight = _read(pg)
    assert surface == {_OLD}
    assert stubborn == {(_STORE, _OLD)}
    assert last_cat == {(_STORE, _OLD): "L"}
    assert perm == {(_STORE, _OLD)}
    assert inflight == {(_STORE, _OLD): False}     # 新码一条都没继承到


@needs_pg
def test_item_appeared_on_the_new_code_would_clear_the_generation(pg):
    """反向钉住 O1:**假如**新码那边记了 item_appeared,顽固标记当场丢失。

    这正是 diff_catalog 必须抑制它的理由 —— 顽固判定取的是"最新事件",
    一条比 delete_not_effective 更晚的 item_appeared 就把上一代的加压清掉了。
    """
    _seed_pair(pg, replaced=True)
    _, stubborn, _, _, _ = _read(pg)
    assert (_STORE, _NEW) in stubborn              # 抑制生效时:代际保住
    with pg.cursor() as cur:
        cur.execute("INSERT INTO catalog.product_events (sku, store, event,"
                    " source, occurred_at) VALUES (%s, %s, 'item_appeared',"
                    " 'test', now())", (_NEW, _STORE))
    _, stubborn2, _, _, _ = _read(pg)
    assert (_STORE, _NEW) not in stubborn2         # 假代际一记:加压静默丢失


@needs_pg
def test_load_state_runs_every_sql_and_never_surfaces_a_replaced_row(pg,
                                                                    monkeypatch):
    """整段 `_load_state` 打在真库上跑一遍:五条 SQL + 两次回执闸查询 +
    **实参形状**都对。

    O5 把两条 SQL 的占位符从 `%s` 改成命名式,消费点的实参必须同步改成 dict ——
    只改 SQL 不改调用点会 ProgrammingError,而纯文本断言看不出来。这条用例让它
    在 pytest 里炸,不在 13:00 那一轮生产里炸。
    """
    import contextlib
    _seed_pair(pg, replaced=True)
    monkeypatch.setattr(scan.db, "pg_conn",
                        lambda *a, **kw: contextlib.nullcontext(pg))
    (items, inflight, inflight_disposal, last_cat,
     inactive, stubborn, gone_blocked, perm_blocked) = scan._load_state()
    ours = [i for i in items if i["store"] == _STORE]
    # 旧码不在扫描面;新码 PUBLISHED 也在(状态不再筛),四列按位置解包
    assert [(i["sku"], i["published_status"]) for i in ours] == [(_NEW, "PUBLISHED")]
    assert (_STORE, _NEW) in stubborn                   # 顽固代际继承到位
    assert last_cat[(_STORE, _NEW)] == "L"
    assert (_STORE, _NEW) in perm_blocked      # 夹具造的是一条 WFS 回执
    assert (_STORE, _NEW) not in gone_blocked  # 两桶不相交,码只落一边
    assert (_STORE, _NEW) in inflight
    assert (_STORE, _NEW) not in inflight_disposal      # 继承来的是上架 feed


# ══════════════════════════════════════════════════════════════════════════════
#  按原子归类处置 + 次要原子落库(所有者定稿 2026-09-10)
# ══════════════════════════════════════════════════════════════════════════════

def test_recoverable_only_rows_are_kept_but_still_categorized():
    """原子集合 ⊆ {EXPIRED, STAGE} 的行不删:单独过期、单独 Stage、两者相加都留。
    留下的行照常归类(进病历),cat_sig 是原子码集合签名。"""
    items = [
        _item("T1", "S_EXP", "This item is unpublished because the End Date has passed."),
        _item("T1", "S_STG", "Item is in stage status until you go live."),
        _item("T1", "S_BOTH", "the End Date has passed.; stage status until you go live"),
    ]
    plans, n = scan.plan(items, inflight=set())
    assert plans == {} and n["delete"] == 0 and n["recoverable"] == 3
    assert [it["cat_sig"] for it in items] == ["EXPIRED", "STAGE", "EXPIRED,STAGE"]
    assert all(it["recoverable"] for it in items)
    assert [a["code"] for a in items[2]["atoms"]] == ["EXPIRED", "STAGE"]


def test_compound_with_a_non_recoverable_atom_deletes():
    """「End Date 过期; 禁售政策」:主码 POLICY,原子集合含非可恢复原子 → 删。
    这就是 B08HJ382VJ 08-29 那条原文的形状。"""
    text = ("This item is unpublished because the End Date has passed.; "
            "This item has been unpublished for violating Walmart's Marketplace "
            "Prohibited Product Policy: Plants & Seeds")
    it = _item("T1", "S_MIX", text)
    plans, n = scan.plan([it], inflight=set())
    assert [r["sku"] for r in plans["T1"]["delete"]] == ["S_MIX"]
    assert it["category"] == "POLICY" and it["recoverable"] is False
    assert it["cat_sig"] == "EXPIRED,POLICY"
    assert [(a["code"], a["policy_name"]) for a in it["atoms"]] == \
        [("EXPIRED", None), ("POLICY", "Plants & Seeds")]
    assert it["atoms"][0]["text"].startswith("This item is unpublished because the End Date")


def test_rows_without_reasons_are_not_candidates_whatever_the_status():
    """无原因 = 无判据:PUBLISHED 行的常态;非 PUBLISHED 而无原因的也不删
    (判不准就判活)。这一档在在途闸之前分流,在途计数只数问题行。"""
    items = [
        {"store": "T1", "sku": "S_LIVE", "reasons": "", "published_status": "PUBLISHED"},
        {"store": "T1", "sku": "S_NULL", "reasons": None, "published_status": "UNPUBLISHED"},
        {"store": "T_OFF", "sku": "S_OFF", "reasons": "  ", "published_status": "STAGE"},
    ]
    plans, n = scan.plan(items, inflight={("T1", "S_LIVE")})
    assert plans == {}
    assert n["clean"] == 3 and n["delete"] == 0
    assert n["inflight"] == n["inflight_listing"] == 0
    assert all("category" not in it for it in items)      # 没归类 ⇒ 不记事件


def test_published_row_with_a_policy_reason_follows_the_same_rule():
    """状态不再是判据:PUBLISHED 行若带非可恢复原子,与 UNPUBLISHED 行同样删。
    (walmart_catalog 每轮整行覆盖 unpublished_reasons,在售行带原因几乎不可能;
    真出现了就是沃尔玛说它有问题,按原文处置。)"""
    it = {"store": "T1", "sku": "S_PUB", "published_status": "PUBLISHED",
          "reasons": "violates Prohibited Product Policy"}
    plans, n = scan.plan([it], inflight=set())
    assert [r["sku"] for r in plans["T1"]["delete"]] == ["S_PUB"]


def test_unknown_atoms_still_delete_but_are_reported():
    """未识别原子照删(所有者:「其他的都删除」),但必须进摘要告警 ——
    classify_reasons 的契约是"unknown 引擎不吞,调用方必须告警"。"""
    it = _item("T1", "S_UNK", "Some brand-new Walmart wording nobody has seen")
    plans, n = scan.plan([it], inflight=set())
    assert [r["sku"] for r in plans["T1"]["delete"]] == ["S_UNK"]
    assert n["unknown"] == 1 and it["category"] == "OTHER"
    note = scan._unknown_note([it])
    assert "未识别原子 1 条" in note and "brand-new Walmart wording" in note
    assert scan._unknown_note([_item("T1", "S", "end date has passed")]) == ""


def test_inactive_store_note_names_stores_but_never_gates():
    """店铺状态不设闸(所有者 2026-09-10):plan() 与审核行都不再读非 ACTIVE 集合;
    run() 只拿它给最终建议行按店点名,让人眼闸门看得见。"""
    import inspect
    assert "inactive" not in inspect.signature(scan.plan).parameters
    assert "inactive" not in inspect.signature(scan._audit_rejected_rows).parameters
    rows = [{"store": "T_OFF", "sku": "A", "action": "delete"},
            {"store": "T_OFF", "sku": "B", "action": "delete"},
            {"store": "T1", "sku": "C", "action": "delete"}]
    note = scan._inactive_note(rows, {"T_OFF"})
    assert "非 ACTIVE 店照常建议" in note and "T_OFF×2" in note and "T1" not in note
    assert scan._inactive_note(rows, set()) == ""


def test_recoverable_note_counts_per_store_and_kind():
    items = [_item("T1", "A", "end date has passed"),
             _item("T1", "B", "stage status until you go live"),
             _item("T2", "C", "end date has passed"),
             _item("T2", "D", "prohibited product policy")]
    scan.plan(items, inflight=set())
    note = scan._recoverable_note(items)
    assert "T1×2{EXPIRED:1,STAGE:1}" in note and "T2×1{EXPIRED:1}" in note
    assert scan._recoverable_note([items[3]]) == ""


def test_dispositions_carry_atoms():
    """建议行 detail.atoms 与事件同款:逐原子 (码/政策名/原文)。"""
    plans, _ = scan.plan([_item("T1", "S", "the End Date has passed.; "
                                "violates Prohibited Product Policy: Hazardous Items")],
                         inflight=set())
    (row,) = scan.to_dispositions(plans)
    assert row["category"] == "POLICY"
    assert [a["code"] for a in row["detail"]["atoms"]] == ["EXPIRED", "POLICY"]
    assert row["detail"]["atoms"][1]["policy_name"] == "Hazardous Items"
    assert row["detail"]["cat_name"] == "违反禁售政策"


def test_categorized_event_fires_on_atom_set_change_not_main_code(monkeypatch):
    """归类事件的判据从主码换成原子码集合:主码不变、多了一个原子也记;
    存量事件没有 atoms 时 _SQL_LAST_CAT 退回主码,单原子行的签名与主码相同。"""
    from services import product_events
    captured: list[list[dict]] = []
    monkeypatch.setattr(product_events, "record_many",
                        lambda conn, rows: captured.append(rows) or len(rows))
    items = [
        _item("T1", "S_SAME", "prohibited product policy"),                   # POLICY == POLICY
        _item("T1", "S_GREW", "end date has passed; prohibited product policy"),  # POLICY → EXPIRED,POLICY
        _item("T1", "S_KEPT", "end date has passed"),                         # 留下的行也记
    ]
    scan.plan(items, inflight=set())
    last_cat = {("T1", "S_SAME"): "POLICY", ("T1", "S_GREW"): "POLICY"}
    n = scan._record_categories(object(), items, last_cat)
    assert n == 2
    (rows,) = captured
    assert [r["sku"] for r in rows] == ["S_GREW", "S_KEPT"]
    grew = rows[0]["detail"]
    assert grew["category"] == "POLICY"
    assert [a["code"] for a in grew["atoms"]] == ["EXPIRED", "POLICY"]
    assert grew["recoverable"] is False
    assert rows[1]["detail"]["recoverable"] is True
    assert all(r["event"] == product_events.PROBLEM_CATEGORIZED for r in rows)


def test_last_cat_sql_signs_by_atoms_with_category_fallback():
    """_SQL_LAST_CAT 与 cat_sig 同一口径:原子码去重、字典序、逗号拼;
    没有 atoms 的存量事件退回 detail.category;atoms 不是数组时不炸。"""
    q = scan._SQL_LAST_CAT
    assert q.count("jsonb_array_elements(") == 2               # 两个 UNION 分支同款
    assert q.count("string_agg(DISTINCT x->>'code', ',' ORDER BY x->>'code')") == 2
    assert q.count("jsonb_typeof(e.detail->'atoms') = 'array'") == 2
    assert q.count("e.detail->>'category'") == 2
    # Python 侧签名生成器与 SQL 同一口径:去重 + 排序 + 逗号
    it = _item("T1", "S", "prohibited product policy; end date has passed; prohibited product policy")
    scan.plan([it], inflight=set())
    assert it["cat_sig"] == "EXPIRED,POLICY"


def test_recoverable_codes_are_the_single_source():
    """可恢复码只在 error_taxonomy 出生;problem_scan 不自带一份。"""
    from services import error_taxonomy as et
    assert et.RECOVERABLE_CODES == ("EXPIRED", "STAGE")
    src = pathlib.Path("workflows/problem_scan.py").read_text(encoding="utf-8")
    assert "is_recoverable_only" in src
    assert '"EXPIRED"' not in src and "'EXPIRED'" not in src


# ══════════════════════════════════════════════════════════════════════════════
#  报错原文:账本存全文(报错归类换轨,PR #109)
# ══════════════════════════════════════════════════════════════════════════════

def test_归类事件存全文_不许再截200():
    """⚠ 2026-09-04:这本账是**产品历史**,而所有者定的判据是「看产品历史,
    够格拉黑的那条最高优先级」—— 截到 200 字符正好把沃尔玛写在**句尾**的判据串
    砍掉(「…To republish this item please make sure you have the appropriate
    product type selected.」),于是 PT_WRONG 被判成 POLICY、**可修复的品被
    永久拉黑**。生产实证:事件里的原文判 POLICY,而 walmart_items 全文判 PT_WRONG。

    截断属于展示层,不属于账本。
    """
    import inspect
    from workflows import problem_scan
    src = inspect.getsource(problem_scan)
    assert '(it["reasons"] or "")[:200]' not in src
    assert '"reason": it["reasons"] or None' in src


def test_审核冲突视图的仍在架必须是真在卖():
    """⚠ 2026-09-06 实见 B0FHPSYT8N:审核链说「审核判拒仍在架」、问题链说
    「已因 End Date 过期下架」,两条理由拼在同一行互相矛盾。病根是视图里
    「在架」只判 `missing_since IS NULL`(目录里还见得到),不判 published。
    所有者 2026-09-07:「改」—— 已下架的归问题扫描链(一律删除),审核链不重复建议。
    覆盖面不变:问题链本来就删全部未 published 的行。"""
    import pathlib as _p
    sql = _p.Path(__file__).resolve().parents[1] / "refdata" / "schema.sql"
    body = sql.read_text(encoding="utf-8")
    view = body.split("CREATE VIEW catalog.audit_listing_conflicts AS", 1)[1]
    head = view.split("SELECT lr.store", 1)[0]          # 只看 CTE 那段
    assert "w.missing_since IS NULL" in head
    assert "w.published_status = 'PUBLISHED'" in head
    assert "p.audit_status = 'rejected'" in head
