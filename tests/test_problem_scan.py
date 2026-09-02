"""problem_scan 回归:归类规则、优先级、一律删除路由、去重、建议行产出。

批次 E 拆分前这些用例住在 test_problem_product_cleanup.py —— **决策逻辑搬到
哪,钉它的测试就跟到哪**。执行侧(消费建议行 + 发 feed)的用例留在原文件。

2026-08-28 所有者定稿「非 PUBLISHED 一律删除,不再改 End Date 救商品」:
反补/Stage 豁免/反补计数的用例换成反向钉死(不许回来),路由用例改为全删。
"""

import pathlib

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


def test_scan_sql_covers_everything_not_published():
    """扫描面 = 一切非 PUBLISHED(所有者定稿 2026-08-28),两个边界一起钉:
    NULL(状态未采到)不进——删除不可逆,不拿未知赌;Stage 不再按行豁免
    (店铺闸挡非 ACTIVE 店;ACTIVE 店里的 Stage = 翻出来的老档,照删)。"""
    assert "published_status IS NOT NULL" in scan._SQL_ITEMS
    assert "published_status <> 'PUBLISHED'" in scan._SQL_ITEMS
    assert "missing_since IS NULL" in scan._SQL_ITEMS
    assert "IN ('UNPUBLISHED'" not in scan._SQL_ITEMS   # 旧白名单口径不许回来
    import inspect
    assert "is_stage_pending" not in inspect.getsource(scan.plan)


def _item(store, sku, reasons):
    return {"store": store, "sku": sku, "reasons": reasons}


def test_plan_routing_and_dedup():
    """一律删除(2026-08-28 定稿):A 类过期、Stage、其他类别全部进删除桶;
    在途/非 ACTIVE 店照旧跳过;顽固双击照旧。"""
    items = [
        _item("T1", "S_A", "end date has passed"),            # 过期 → 删除
        _item("T1", "S_B", "prohibited product policy"),      # → 删除
        _item("T1", "S_STAGE", "stage status until you go live"),  # → 删除(不再豁免)
        _item("T1", "S_FLY", "intellectual property"),        # 处置在途 → 跳过
        _item("T1", "S_NEW", "prohibited product policy"),    # 上架在途 → 跳过
        _item("T_OFF", "S_X", "prohibited product policy"),   # 非 ACTIVE 店 → 跳过
        _item("T1", "S_ZOMBIE", "prohibited product policy"),  # 删除未生效 → 双击
    ]
    # 2026-08-24 起在途计数拆两桶(跳过行为不变):处置在途 vs 上架/维护在途
    plans, n = scan.plan(items,
                         inflight={("T1", "S_FLY"), ("T1", "S_NEW")},
                         inactive={"T_OFF"},
                         stubborn={("T1", "S_ZOMBIE")},
                         inflight_disposal={("T1", "S_FLY")})
    # 顽固 SKU 停用+删除双 feed;其余(含过期与 Stage)全进删除桶
    assert {r["sku"] for r in plans["T1"]["delete"]} == \
        {"S_A", "S_B", "S_STAGE", "S_ZOMBIE"}
    assert [r["sku"] for r in plans["T1"]["retire"]] == ["S_ZOMBIE"]
    assert "relist" not in plans["T1"]          # 反补桶不存在了
    assert n["stubborn"] == 1
    assert "T_OFF" not in plans
    assert (n["inflight"], n["inactive"]) == (1, 1)
    assert n["inflight_listing"] == 1        # S_NEW:上架 feed 在途,单列一桶
    assert n["delete"] == 3                  # 双击那条不计在 delete(摘要按行重算)
    # Stage 行照常归类(J 类进病历/摘要),只是不再改变走向
    stage_row = [r for r in plans["T1"]["delete"] if r["sku"] == "S_STAGE"][0]
    assert stage_row["category"] == "J"


def test_to_dispositions_splits_double_hit():
    """顽固双击 = **两条**建议行,不是一条。它们是两个 feed、两次独立的生效
    判定,合成一行会让其中一个的落定结果覆盖另一个。"""
    plans, _ = scan.plan([_item("T1", "S_Z", "prohibited product policy")],
                         inflight=set(), inactive=set(),
                         stubborn={("T1", "S_Z")})
    rows = scan.to_dispositions(plans)
    assert sorted(r["action"] for r in rows) == ["delete", "retire"]
    assert all(r["store"] == "T1" and r["sku"] == "S_Z" for r in rows)
    assert all(r["source"] == "scan" for r in rows)


def test_to_dispositions_carries_category_and_reason():
    plans, _ = scan.plan([_item("T1", "S_B", "violates Prohibited Product Policy")],
                         inflight=set(), inactive=set())
    (row,) = scan.to_dispositions(plans)
    assert (row["action"], row["category"]) == ("delete", "B")
    assert "Prohibited" in row["reason"]
    assert row["detail"]["cat_name"] == "禁售"


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

    rows = scan._audit_rejected_rows(
        _Conn(), inflight={("T1", "S_FLY")}, inactive={"T_OFF"}, only=None)
    assert [r["sku"] for r in rows] == ["S1"]
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

    rows = scan._audit_rejected_rows(_Conn(), inflight=set(), inactive=set(),
                                     only=None)
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
        set(), set(), {}, set(), set(), set()))
    monkeypatch.setattr(scan.dispositions, "suggest_many",
                        lambda conn, rows: (_ for _ in ()).throw(
                            AssertionError("preview 不许写建议行")))
    monkeypatch.setattr(scan, "_audit_rejected_rows",
                        lambda conn, inflight, inactive, only, wfs=None: [])

    import contextlib
    from registry import db as _db
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    # 缺席避让走库里水位(store_absence),测试环境无库无飞书,置空
    monkeypatch.setattr(scan.store_absence, "stale_stores",
                        lambda conn, since=None, hours=None: [])
    out = scan.run({"preview": "1"})
    assert "preview" in out and "删除 1" in out
    assert "类别={B:1}" in out and "删除样本=[('S_B', 'B')]" in out


def test_absence_probe_failure_does_not_stop_the_scan(monkeypatch):
    """扫描件是**只读**件:缺席探测挂了按"不避让"照常出建议(fail-open),
    与破坏件 problem_product_cleanup 的 fail-closed 方向相反 —— preview 是纯
    PG 查询,不该被一次飞书抖动整个拦下。

    降级本身收在 services/store_absence.stale_or_note(四处同形,2026-08-27
    收口),这里钉的是**首行拼装**:分号由调用方补,措辞一个字都不许改。
    """
    monkeypatch.setattr(scan, "_load_state", lambda: (
        [_item("T1", "S_B", "prohibited product policy")],
        set(), set(), {}, set(), set(), set()))
    monkeypatch.setattr(scan, "_audit_rejected_rows",
                        lambda conn, inflight, inactive, only, wfs=None: [])

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
                         inflight=set(), inactive=set(),
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
         "wfs": 0}
    head = _summ(allrows, audit_rows, n, 99)
    # 删除报 2(retire 之外的全部 delete 行),不是 n['delete'] 的 1
    assert "删除 2" in head[0]
    assert "非 PUBLISHED 商品 99 行" in head[0]
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
         "wfs": 0,
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
    2026-08-28 所有者定稿推翻:非 PUBLISHED 一律删除,L 类不再例外。
    归类仍是 L(病历/摘要照记),只是走向统一成删除。"""
    items = [
        _item("T1", "S_L1", "an internal error occurred while publishing"),
        _item("T1", "S_L2", "an internal error occurred"),
    ]
    plans, n = scan.plan(items, inflight=set(), inactive=set())
    assert {r["sku"] for r in plans["T1"]["delete"]} == {"S_L1", "S_L2"}
    assert all(r["category"] == "L" for r in plans["T1"]["delete"])
    assert n["delete"] == 2


def test_k_cluster_note_fires_on_concentration():
    """「内部标记」单条无信息量,聚集才是信号(实测谭总11 一店 45 条)。"""
    items = [{"store": "T1", "sku": f"S{i}", "category": "K"}
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


# ── WFS 删不掉的闸(多仓批次 0)──────────────────────────────────────────────

def test_wfs_blocked_skus_are_skipped_not_re_deleted_every_round():
    """WFS 件删不掉 → 跳过并计数,**不再每天空发一次注定被拒的 DELETE_ITEM**。

    生产实证 11 条(L001/A152/A154/A170)连着几轮同一个 ERR_EXT_DATA_0101218。
    ⚠ 只拦破坏动作:反补走 MP_MAINTENANCE,对 WFS 件照常可用。
    """
    items = [_item("T1", "S_DEL", "prohibited product policy"),   # → 删除
             _item("T1", "S_OK", "prohibited product policy")]
    plans, n = scan.plan(items, set(), {}, set(),
                         wfs_blocked={("T1", "S_DEL")})
    assert n["wfs"] == 1
    assert [r["sku"] for r in plans["T1"]["delete"]] == ["S_OK"]


def test_wfs_gate_blocks_the_delete_and_counts_it():
    """WFS 件在「一律删除」口径下(2026-08-28 反补退役)一条都发不出去:
    跳过并报数,把"要不要转出 WFS"交回给人 —— 不跳的话每天空发一次注定
    被拒的 DELETE_ITEM(生产实见 11 条连烧多轮)。"""
    it = _item("T1", "S_EXP", "end date has passed")
    plans, n = scan.plan([it], set(), set(), wfs_blocked={("T1", "S_EXP")})
    assert n["wfs"] == 1 and n["delete"] == 0
    assert plans.get("T1", {"delete": []})["delete"] == []


def test_wfs_gate_also_blocks_stubborn_double_feed():
    """顽固件的 retire+delete 双发同样拦:delete 注定被拒,而 retire 对 WFS
    件行不行官方没有明文 —— 按本仓纪律不按推断编码,整条跳过并报数。"""
    items = [_item("T1", "S_Z", "prohibited product policy")]
    plans, n = scan.plan(items, set(), set(), stubborn={("T1", "S_Z")},
                         wfs_blocked={("T1", "S_Z")})
    assert n["wfs"] == 1 and n["stubborn"] == 0
    assert plans.get("T1", {"delete": [], "retire": []})["delete"] == []


def test_wfs_blocked_sql_reads_only_the_latest_attempt():
    """口径是**最近一次**删除回执,不是"历史上出现过就永久拉黑"。

    商品转出 WFS 之后就该能删了 —— 下一次尝试的回执会把它放出来。
    写成 EXISTS(任意一轮命中过)的话,转出 WFS 的件永远删不了,而且没人
    看得出来是被自己的闸拦着。
    """
    q = scan._SQL_WFS_BLOCKED
    assert "DISTINCT ON (store, sku)" in q
    assert "ORDER BY store, sku, submitted_at DESC" in q
    assert "feed_type = 'DELETE_ITEM'" in q
    assert scan._WFS_BLOCKED_CODE == "ERR_EXT_DATA_0101218"
