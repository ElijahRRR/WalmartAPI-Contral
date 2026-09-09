"""处置建议路由器契约(所有者定稿 2026-08-24)。

钉的是"建议合并 → 谁来执行"这一段的四条规矩:
  ① 动作优先级是**全项目唯一出处**,序由所有者定;
  ② 执行件按**动作**领取,不按来源(旧口径按来源领,08-19 生产实见错位);
  ③ 单店破坏类上限**只施加一次**,在执行件领取时(此前两条链各截一次 ⇒ 2N);
  ④ 转态必须落 executed_by —— 合并之后"最终是谁干的"要在库里有答案。
"""

import inspect
import pathlib
import re

import pytest

from registry import resources
from services import dispositions as ds


class _Cur:
    def __init__(self, seen, rows=()):
        self.seen, self.rows = seen, list(rows)
        self.description = [type("C", (), {"name": n})()
                            for n in ("id", "store", "sku", "asin", "source",
                                      "action", "category", "reason", "detail")]

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        self.seen["sql"] = sql
        self.seen["params"] = params
        return self

    def fetchall(self): return self.rows

    def fetchone(self): return self.rows[0] if self.rows else None

    @property
    def rowcount(self): return len(self.rows)


class _Conn:
    def __init__(self, seen, rows=()):
        self._seen, self._rows = seen, rows

    def cursor(self): return _Cur(self._seen, self._rows)


def test_action_rank_is_the_single_source_and_matches_the_owner_sequence():
    """删除 > 停用 > 库存 > 标题 > 价格(2026-08-24 定序;2026-08-28 反补退役)。

    这条序此前只活在 maintenance_intents._ACTION_RANK 的**一轮内存**里
    (删除 > 库存 > 标题,三个动作),看不见另一条链挂在库里的建议 —— 跨链
    重复删两次就是这么来的。提升作用域之后它必须覆盖全部在役动作。
    relist 2026-08-28 所有者定稿退役(非 PUBLISHED 一律删,不再救),
    **不许回到任何领取集**:存量行只走 withdraw/settle 收尾。
    """
    assert "relist" not in ds.ACTIONS and "relist" not in ds.PROBLEM_ACTIONS
    assert ds.ACTION_ORDER == ("delete", "retire",
                               "inventory", "title", "price")
    assert set(ds.ACTION_RANK) == set(ds.ACTIONS)     # 一个都不能漏
    assert len(set(ds.ACTION_RANK.values())) == len(ds.ACTIONS)   # 不许并列
    # 破坏组必须排在维护组全部动作之前:先删掉就不必再为它烧改价/改库存配额
    worst_destructive = max(ds.ACTION_RANK[a] for a in ds.DESTRUCTIVE_ACTIONS)
    assert worst_destructive < min(ds.ACTION_RANK[a] for a in ds.MAINT_ACTIONS)


def test_claim_filters_by_action_and_orders_by_rank():
    """⚠ 按动作领,不按来源领。

    旧口径按 source 领,后果 08-19 生产实见:一条 (店铺,SKU,delete) 被维护链
    先建议(source='maint')、审核链后覆写 reason,那行仍归维护链执行 ——
    表里写着维护链的「建议」、问题链的「原因」,谁也说不清是哪条链干的。
    """
    seen = {}
    ds.claim(_Conn(seen), ds.PROBLEM_ACTIONS)
    assert "d.action = ANY(%(actions)s::text[])" in seen["sql"]
    where = seen["sql"].split("ORDER BY")[0].split("WHERE")[1]
    assert "source" not in where                   # WHERE 里不再筛来源
    assert seen["params"]["actions"] == list(ds.PROBLEM_ACTIONS)
    # 取件顺序 = (店铺, 动作优先级, 建议时间):执行期按这个顺序截单店上限,
    # 所以优先级高的那些总是先保住。按 action 文本排序会变成字母序(delete
    # 恰好在 relist 前面纯属巧合,retire 就排到 relist 后面去了)
    assert "array_position(%(rank)s::text[], d.action)" in seen["sql"]
    assert seen["params"]["rank"] == list(ds.ACTION_ORDER)


def test_cap_destructive_is_the_only_per_store_brake():
    """单店上限只在执行件领取时施加一次(2026-08-24 归一)。

    此前 maintenance_intents 与 problem_scan 各按同一张限额表「下架限制」
    截一次 —— 每店最多 N 条实际变成了最多 2N。
    """
    rows = ([{"store": "T1", "sku": f"S{i}", "action": "delete"}
             for i in range(5)]
            + [{"store": "T1", "sku": "S9", "action": "retire"}]
            + [{"store": "T1", "sku": "M1", "action": "title"}]
            + [{"store": "T2", "sku": "K1", "action": "delete"}])
    kept, over = ds.cap_destructive(rows, {"T1": 2}, 300)
    # T1 破坏类只留 2 条;维护类不烧下架配额,一条不截
    assert [(r["store"], r["sku"]) for r in kept] == [
        ("T1", "S0"), ("T1", "S1"), ("T1", "M1"), ("T2", "K1")]
    assert over == {"T1": 4}          # 削掉的必须报出来,不是静默丢弃
    # 缺该店时退缺省值,而不是"不限"——fail-closed 是这道闸唯一的方向
    kept2, over2 = ds.cap_destructive(rows, {}, 1)
    assert over2 == {"T1": 5, "T2": 0} or over2 == {"T1": 5}
    assert len([r for r in kept2 if r["store"] == "T1"
                and r["action"] in ds.DESTRUCTIVE_ACTIONS]) == 1


def test_destructive_per_store_default_has_exactly_one_home():
    """300 这个缺省值不许再散出第二份(散在多处 = 改一处另一处静默按旧规矩办)。"""
    assert ds.DESTRUCTIVE_PER_STORE == 300
    hits = []
    for f in list(pathlib.Path("services").glob("*.py")) + \
            list(pathlib.Path("workflows").glob("*.py")):
        src = f.read_text()
        if re.search(r"^\s*\w*(?:DELETE|RETIRE|DESTRUCTIVE)_PER_STORE\s*=",
                     src, re.M):
            hits.append(f.name)
    assert hits == ["dispositions.py"], hits


def test_mark_executing_records_who_did_it():
    """合并之后"最终是谁干的"必须在库里有答案,不能靠 source 反推。"""
    seen = {}
    ds.mark_executing(_Conn(seen, rows=[(1,)]), [1, 2], "FEED1",
                      by="problem_product_cleanup")
    assert "executed_by = %(by)s::text" in seen["sql"]
    assert seen["params"]["by"] == "problem_product_cleanup"
    assert seen["params"]["feed_id"] == "FEED1"
    # 落 executed_by 与转 executing 是同一条 UPDATE:分开写就会有"转了态但
    # 不知道谁转的"的行,而且只在其中一条失败时出现,极难复现
    assert "status = 'executing'" in seen["sql"]


# ── 合并:一条建议,多个支撑来源(所有者定稿 2026-08-24)────────────────────

def test_destructive_group_is_not_merged_per_sku():
    """⚠ 所有者提的"破坏组一 SKU 一条"**没有采纳**,理由记在这里。

    problem_scan 对**顽固 SKU**(上一轮 delete 观测到没生效)同时建议 retire 与
    delete —— 停用+删除双 feed 齐发,"能删的删,删不掉的至少停用"。合成一条会
    让其中一个的落定结果覆盖另一个,而两边都不报错:to_dispositions 照样产两条
    (它是纯函数),只有落库那一刻悄悄少一条。
    所以唯一索引仍是 (店铺, SKU, 动作);"破坏组存在即压制维护组"改由 claim()
    实现 —— 索引本来也管不了跨行的条件。
    """
    ddl = " ".join(pathlib.Path("refdata/schema.sql").read_text().split())
    assert ("CREATE UNIQUE INDEX IF NOT EXISTS dispositions_open_uidx "
            "ON ops.dispositions (store, sku, action) "
            "WHERE status IN ('suggested', 'executing')") in ddl
    assert "dispositions_open_destructive_uidx" not in ddl
    flat = " ".join(ds._UPSERT_SQL.split())
    assert ("ON CONFLICT (store, sku, action) "
            "WHERE status IN ('suggested', 'executing')") in flat


def test_merge_keeps_every_source_reason_and_never_overwrites():
    """08-19 那行的病根:后写方覆盖 reason,一行显示 A 链的建议、B 链的原因。

    合并之后两条理由都要看得见,而且**单来源时逐字不变** —— 维护记录表的
    「原因」列不该因为这次改造而变样。
    """
    flat = " ".join(ds._UPSERT_SQL.split())
    assert "sources = ops.dispositions.sources || EXCLUDED.sources" in flat
    # detail 是**合并**不是替换:替换会把维护链删除行的 label/旧值/新值 洗掉
    # (08-19 那行「建议」只剩光秃秃"删除"、旧值新值全空就是这么来的)
    assert "detail = ops.dispositions.detail || EXCLUDED.detail" in flat
    assert "reason = EXCLUDED.reason" not in flat
    assert "category = EXCLUDED.category" not in flat

    single = ds._merge_view({"reason": "标题相似度 62% < 70%", "category": "tm",
                             "sources": {"maint": {"reason": "标题相似度 62% < 70%",
                                                   "code": "tm", "at": "1"}}})
    assert single["reason"] == "标题相似度 62% < 70%"      # 逐字不变
    assert single["merged_from"] == ("maint",)

    merged = ds._merge_view({
        "reason": "写进列里的是首个来源的", "category": None,
        "sources": {"audit": {"reason": "审核判拒仍在架:知产", "code": None,
                              "at": "2026-08-19T13:20:00"},
                    "maint": {"reason": "标题相似度 62% < 70%", "code": "tm",
                              "at": "2026-08-19T13:05:00"}}})
    # 按写入时间排序:先建议的排前面,读起来就是这条建议的来历
    assert merged["reason"] == "维护:标题相似度 62% < 70% | 审核:审核判拒仍在架:知产"
    assert merged["category"] == "tm"        # 原因码取最早那个非空的
    assert merged["merged_from"] == ("audit", "maint")


def test_suppression_lives_in_claim_not_in_the_schedule():
    """破坏组压制维护组:**与两个扫描件谁先跑无关**。

    调度上把 maintenance_scan 排在 problem_scan 前面只是让人读着顺,压制本身
    在 claim() 里按库里所有未落定的破坏类建议判 —— 顺序改了结果也不变。
    本仓吃过"顺序即语义"的亏,不再让调度表承载判据。
    """
    seen = {}
    ds.claim(_Conn(seen), ds.MAINT_ACTIONS)
    flat = " ".join(seen["sql"].split())
    assert ("d.action = ANY(%(destructive)s::text[]) OR NOT EXISTS" in flat)
    assert "x.status IN ('suggested', 'executing')" in flat   # 在途的也算
    assert seen["params"]["destructive"] == list(ds.DESTRUCTIVE_ACTIONS)
    # 被压制的行**留在 suggested**:删除若最终没生效,它们还在,不用等重算
    assert "withdrawn" not in flat and "UPDATE" not in flat

    seen2 = {}
    ds.count_suppressed(_Conn(seen2, rows=[(7,)]), ds.MAINT_ACTIONS)
    flat2 = " ".join(seen2["sql"].split())
    assert "NOT (d.action = ANY(%(destructive)s::text[]))" in flat2
    assert "count(*)" in flat2


def test_withdraw_only_removes_its_own_source_slot():
    """撤销只删自己那一格,全空才 withdrawn。

    旧写法按标量 source 整行撤 —— 合并行只记得住一个来源,另一条链既撤不掉
    它、也不知道自己那条理由还成不成立。
    """
    flat = " ".join(ds._WITHDRAW_SQL.split())
    assert "sources = d.sources - %(source)s::text" in flat
    assert ("status = CASE WHEN (d.sources - %(source)s::text) = '{}'::jsonb "
            "THEN 'withdrawn' ELSE d.status END") in flat
    # keep 比对用**该来源自己记的动作**:破坏组撞车时行上的 action 可能已升格
    assert ("k.action = COALESCE(d.sources -> %(source)s::text ->> 'action', "
            "d.action)") in flat
    # 存量行(sources 还是 '{}')按标量 source 兜底,否则永远撤不掉且不报错
    assert "d.sources = '{}'::jsonb AND d.source = %(source)s::text" in flat


def test_every_module_level_name_the_module_uses_is_actually_defined():
    """⚠ lint 式护栏。**本轮改造自己踩过一次。**

    重构时把 `_SETTLE_DELETE_SQL` / `_SETTLE_RELIST_SQL` 连着一段旧 SQL 一起
    删掉了,而 `settle()` 还在引用它们 —— 全套用例照样绿,因为每个调用点都把
    `settle` monkeypatch 掉了。真跑一次才会 NameError,而那是在生产上的
    problem_product_cleanup 里,发生在"上一轮落定"这一步。

    这条用例不测行为,只测**模块里用到的全局名都还在**(Python 直到执行到
    那一行才解析全局名,所以少一个常量在导入期是完全静默的)。
    """
    import ast
    import builtins

    src = pathlib.Path("services/dispositions.py").read_text()
    tree = ast.parse(src)
    defined = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    for node in tree.body:                      # 模块级定义
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                defined.add(a.asname or a.name)

    missing = set()
    for fn in [n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        local = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
        if fn.args.vararg:
            local.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            local.add(fn.args.kwarg.arg)
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                local.add(n.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    local.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, ast.comprehension):
                for t in ast.walk(n.target):
                    if isinstance(t, ast.Name):
                        local.add(t.id)
            elif isinstance(n, ast.Lambda):
                local |= {a.arg for a in n.args.args}
                local |= {a.arg for a in n.args.kwonlyargs}
        for n in ast.walk(fn):
            if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                    and n.id not in local and n.id not in defined):
                missing.add(f"{fn.name} 用到未定义的 {n.id}")
    assert not missing, sorted(missing)


# ── 分桶积木 group_by_store(2026-08-27 从两个执行件上移)──────────────────

def test_group_by_store_buckets_by_action_in_the_given_order():
    """两个执行件各传自己的键与顺序 —— 同一份 claim() 数据的两种分桶。"""
    rows = [{"store": "A085", "action": "delete", "id": 1},
            {"store": "A085", "action": "relist", "id": 2},
            {"store": "A107", "action": "retire", "id": 3}]
    got = ds.group_by_store(rows, key="action",
                            order=("relist", "retire", "delete"), id_field="id")
    # 桶序即发 feed 的顺序(先救活再删),空桶也要在
    assert list(got["A085"]) == ["relist", "retire", "delete"]
    assert [r["id"] for r in got["A085"]["delete"]] == [1]
    assert [r["id"] for r in got["A085"]["relist"]] == [2]
    assert got["A085"]["retire"] == []
    assert [r["id"] for r in got["A107"]["retire"]] == [3]

    # maintenance 那一侧:键叫 kind,顺序是维护三类,id 在 disposition_id 上
    intents = [{"store": "A085", "kind": "price", "disposition_id": 9}]
    got2 = ds.group_by_store(intents, key="kind", order=ds.MAINT_ACTIONS,
                             id_field="disposition_id")
    assert list(got2["A085"]) == list(ds.MAINT_ACTIONS)
    assert got2["A085"]["price"][0]["disposition_id"] == 9


def test_group_by_store_raises_on_unknown_action():
    """**宁炸不吞**(conventions §三 的安全闸):抽取时不许改成静默丢弃。

    静默丢掉的话,那条建议每轮都会被领走又消失,claim() 的取件数与实际提交数
    长期对不上而两边都不报错 —— 破坏动作走的正是这条路。
    """
    rows = [{"store": "A085", "action": "nuke", "id": 7}]
    with pytest.raises(ValueError) as ei:
        ds.group_by_store(rows, key="action",
                          order=("relist", "retire", "delete"), id_field="id")
    msg = str(ei.value)
    assert "未知 action='nuke'" in msg and "建议行 id=7" in msg



# ── 改码要的两个积木(SKU 改造批次 3,O9/O10)────────────────────────────────
#
# 处置状态机的判据与写入必须出生在**所有权模块**里:工作流里现写一句
# `UPDATE ops.dispositions SET sku = …` 就是第二条处置写路径 —— 它绕过状态机、
# 撞得上 dispositions_open_uidx、还漏掉 asin 列。

class _ScriptedCur:
    """按 SQL 关键字回放不同结果的假游标(rekey 一次 with 里跑两条语句)。"""

    def __init__(self, log, taken=(), moved=0):
        self.log, self.taken, self.moved = log, list(taken), moved
        self.rowcount = 0

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        self.rowcount = self.moved if sql.strip().startswith("UPDATE") else 0
        return self

    def fetchall(self):
        return [(a,) for a in self.taken]

    def fetchone(self):
        return (len(self.taken),)


class _ScriptedConn:
    def __init__(self, taken=(), moved=0):
        self.log: list = []
        self._cur = _ScriptedCur(self.log, taken, moved)

    def cursor(self): return self._cur


def test_open_executing_count_only_counts_executing_rows_of_that_store():
    """改码前置闸的读侧:该店有没有在途处置,只问 0 还是非 0。

    executing 意味着某条 feed 已提交、正等观测判决;改码会把它等的那个
    (店, SKU) 键换掉 —— 判决从此永远等不到,行卡在 executing 里堵住同
    (店,SKU,动作) 的一切新建议(部分唯一索引挡着)。
    """
    conn = _ScriptedConn(taken=("delete", "retire"))
    assert ds.open_executing_count(conn, "T1") == 2
    sql, params = conn.log[0]
    assert "count(*)" in sql and "ops.dispositions" in sql
    assert "status = 'executing'" in sql
    assert "store = %(store)s::text" in sql          # 别店的 executing 不算数
    assert params == {"store": "T1"}
    assert "UPDATE" not in sql.upper()               # 只读


def test_rekey_open_moves_suggested_and_maintenance_executing_rows():
    """迁的面 = **全部 suggested + 维护组的 executing**(2026-09-08 扩面)。

    原口径「executing 一律不碰」的前提是"新码与旧码是两个不同的 item";
    MP_ITEM_MATCH 的改码是**同一个 item 上原地换码**(同 wpid,sku_plan §9.12),
    所以维护三类的 executing 搬过去不是换判决对象,而是把账挪到唯一还比得了的
    那一行(A171罗尹鸿 691466:改价早就生效,拿旧码那一行比永远比不了)。
    破坏组的 executing 仍旧不迁 —— 状态条件必须写成"suggested,或 executing 且
    动作在维护组里",一句 `status <> 'settled'` 会把破坏组一起卷进来且不报错。
    asin 列跟着补(coalesce 只填不覆盖);迁过的行 detail 留 rekeyed_from。
    """
    conn = _ScriptedConn(taken=(), moved=3)
    moved, taken = ds.rekey_open(conn, "T1", "B0OLD00001", "AN3WC0DE2345",
                                 asin="B0OLD00001")
    assert (moved, taken) == (3, [])
    upd_sql, upd_params = conn.log[1]
    assert "status = 'suggested'" in upd_sql
    assert "status = 'executing' AND action = ANY(%(maint)s::text[])" in upd_sql
    assert upd_params["maint"] == list(ds.MAINT_ACTIONS)     # 名单不在这儿手打
    assert "asin = coalesce(asin, %(asin)s::text)" in upd_sql
    assert upd_params["asin"] == "B0OLD00001"
    assert upd_params["taken"] == []
    # 迁过的行要留痕:回头查"这条 executing 当初打在哪个码上"必须有答案
    assert "'rekeyed_from', %(old_sku)s::text" in upd_sql
    assert "'rekeyed_at', now()" in upd_sql
    # ⚠ executed_at 一个字不改:宽限期照旧从原提交时刻算
    assert "executed_at" not in upd_sql


def test_rekey_open_never_moves_destructive_executing_rows():
    """反向钉死:破坏组(delete/retire)的 executing **不许**被搬。

    它们等的是 `product_events` 的 delete_verified(「这个 SKU 不见了」),而改码
    之后旧码正好消失 —— 搬到新码上就是拿另一个身份去等一个已经被污染的判决。
    这条与「维护组一并搬」是同一个 SQL 里的两半,写错一半不会报错。
    """
    sql = ds._REKEY_SQL
    for a in ds.DESTRUCTIVE_ACTIONS:
        assert f"'{a}'" not in sql, a                 # 名单里一个破坏动作都没有
    assert "%(maint)s::text[]" in sql                 # executing 那一半只认维护组
    assert "settled_at IS NULL" in sql                # 已落定的病历行一根手指不碰
    assert "DELETE" not in sql.upper()                # 撞车的行不删


def test_rekey_open_skips_and_reports_action_collisions(caplog):
    """新码名下已有同动作的未落定建议 ⇒ 那些动作**不迁、不删、不合并**,点名人工。

    dispositions_open_uidx 是 (store, sku, action) WHERE status IN
    ('suggested','executing'),撞上直接抛;而两条同动作的建议合成一条会让其中
    一个的落定结果覆盖另一个(schema.sql 的索引注释明写这条设计)。
    判不准就判活 —— 返回 action 列表让调用方点名。
    """
    import logging
    conn = _ScriptedConn(taken=("delete", "delete", "retire"), moved=1)
    with caplog.at_level(logging.WARNING, logger="services.dispositions"):
        moved, taken = ds.rekey_open(conn, "T1", "B0OLD00001", "AN3WC0DE2345")
    assert (moved, taken) == (1, ["delete", "retire"])     # 去重且定序
    sel_sql, sel_params = conn.log[0]
    assert "status IN ('suggested', 'executing')" in sel_sql   # 在途也算占位
    assert sel_params == {"store": "T1", "new_sku": "AN3WC0DE2345"}
    upd_sql, upd_params = conn.log[1]
    assert "action <> ALL(%(taken)s::text[])" in upd_sql
    assert upd_params["taken"] == ["delete", "retire"]
    assert any("人工" in m for m in caplog.messages)


def test_only_one_rekey_function_exists():
    """一个能力一条实现路径(§六):改名之后**不许**留着旧名当别名。

    留一个 `rekey_suggested = rekey_open` 的别名不会报错,但半年后两个名字会各自
    长出调用方,而"executing 到底搬不搬"这件事就有了两份口径。
    """
    assert not hasattr(ds, "rekey_suggested")
    assert len([n for n in dir(ds) if n.startswith("rekey")]) == 1


class _MaintSettleConn:
    """settle_maintenance 的假连接:第一条 SQL 取待判行,后面是 UPDATE。"""

    def __init__(self, rows):
        self.rows, self.updates, self.sqls = list(rows), [], []

    def cursor(self):
        return self

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, args=None):
        self.sqls.append(sql)
        if "UPDATE ops.dispositions" in sql:
            self.updates.append((args["status"], sorted(args["ids"])))
        return self

    def fetchall(self):
        return list(self.rows)


def test_a_rekeyed_executing_row_settles_on_the_new_codes_value():
    """迁过去之后**不加任何新的落定代码**:`settle_maintenance` 的现有规则自然收尾。

    A171罗尹鸿 691466(price,15.71→16.78,2026-09-07 14:00 executed):迁到新码
    AJ5K52FK5SME 之后,`_MAINT_OPEN_SQL` JOIN 到的是**新码那一行**(观测 16.78,
    `last_seen_at > executed_at + 2h`),按现值比 want ⇒ confirmed。
    不迁的话它 JOIN 的是旧码 —— 那一行已经缺席,取不出来,一路挂到
    `expire_executing` 3 天超期才放行,期间每次改码定案还要把它点名一遍。
    """
    conn = _MaintSettleConn([
        (691466, "price", {"new": 16.78, "rekeyed_from": "B0OLDA171X"},
         16.78, None, None, None),                      # 新码现值 = 提交的值
    ])
    assert ds.settle_maintenance(conn) == {"confirmed": 1, "ineffective": 0}
    assert conn.updates == [("confirmed", [691466])]
    # 落定判据仍是那两条,一个字没加:重新观测过 + 过了宽限期
    assert "w.last_seen_at > d.executed_at" in ds._MAINT_OPEN_SQL
    assert "%(grace)s::int" in ds._MAINT_OPEN_SQL


# ── 沙箱 PG 集成:迁键真的绕开了那条部分唯一索引 ─────────────────────────────
#
# ⚠ 地址是**测试夹具**,不是生产资源(生产走 registry/db.pg_dsn());固定在非
# 标准端口 55432 上正是为了不可能连到生产库,造的数据全在最后回滚的事务里。
# 假连接证明不了"UPDATE 会不会撞 dispositions_open_uidx" —— 那正是本条要挡的
# 事故(原稿在工作流里裸写 UPDATE,撞上直接抛,而且没有分支)。
import os
import socket

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

_DSTORE, _DOLD, _DNEW = "DISP_T1", "B0DISPOLD01", "ADISP1234567"


@pytest.fixture
def pg(monkeypatch):
    """输入:无 → 输出:沙箱 PG 连接(整场事务**最后一律回滚**)。"""
    monkeypatch.setenv("WALMART_PG_DSN", _DSN)
    from registry import db
    with db.pg_conn() as conn:
        try:
            yield conn
        finally:
            conn.rollback()


def _row(conn, sku, action, status, asin=None):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ops.dispositions (store, sku, asin, source,"
                    " action, status) VALUES (%s, %s, %s, 'scan', %s, %s)",
                    (_DSTORE, sku, asin, action, status))


def _state(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT sku, action, status, asin FROM ops.dispositions "
                    "WHERE store = %s ORDER BY sku, action", (_DSTORE,))
        return cur.fetchall()


@needs_pg
def test_rekey_open_survives_the_open_unique_index(pg):
    """撞车的动作**不迁**,不撞的照迁 —— 全程不抛 UniqueViolation。

    dispositions_open_uidx 是 (store, sku, action) WHERE status IN
    ('suggested','executing')。裸 UPDATE 撞上就抛,而抛在改码定案的事务里
    = 整笔定案回滚,pending 行原地不动、下一轮再撞一次。
    """
    _row(pg, _DOLD, "delete", "suggested")
    _row(pg, _DOLD, "retire", "suggested")
    _row(pg, _DNEW, "delete", "suggested")          # 新码名下已占了 delete
    moved, taken = ds.rekey_open(pg, _DSTORE, _DOLD, _DNEW,
                                 asin="B0DISPOLD01")
    assert (moved, taken) == (1, ["delete"])
    assert _state(pg) == [
        (_DNEW, "delete", "suggested", None),        # 新码原有的那条,原样不动
        (_DNEW, "retire", "suggested", "B0DISPOLD01"),   # 迁过来的,asin 补上
        (_DOLD, "delete", "suggested", None)]        # 撞车的:不迁、不删,等人工


@needs_pg
def test_rekey_open_moves_maintenance_executing_but_not_destructive(pg):
    """真库上分两组:维护组的 executing **搬**,破坏组的 executing 与已落定行**不搬**。"""
    _row(pg, _DOLD, "delete", "executing")      # 破坏组在途:不搬
    _row(pg, _DOLD, "retire", "confirmed")      # 已落定的病历:不搬
    _row(pg, _DOLD, "price", "executing")       # 维护组在途:搬
    moved, taken = ds.rekey_open(pg, _DSTORE, _DOLD, _DNEW, asin="B0DISPOLD01")
    assert (moved, taken) == (1, [])
    assert _state(pg) == [(_DNEW, "price", "executing", "B0DISPOLD01"),
                          (_DOLD, "delete", "executing", None),
                          (_DOLD, "retire", "confirmed", None)]
    with pg.cursor() as cur:
        cur.execute("SELECT detail ->> 'rekeyed_from', detail ->> 'rekeyed_at' "
                    "IS NOT NULL, executed_at FROM ops.dispositions "
                    "WHERE store = %s AND sku = %s", (_DSTORE, _DNEW))
        frm, stamped, _executed = cur.fetchone()
        assert (frm, stamped) == (_DOLD, True)   # 留痕:当初打在哪个码上


@needs_pg
def test_open_executing_count_is_scoped_to_the_store_and_the_status(pg):
    """前置闸只数**本店**的 executing:别店卡着不该拦住这家店改码。"""
    _row(pg, _DOLD, "delete", "executing")
    _row(pg, _DOLD, "retire", "suggested")
    with pg.cursor() as cur:
        cur.execute("INSERT INTO ops.dispositions (store, sku, source, action,"
                    " status) VALUES ('DISP_T2', %s, 'scan', 'delete',"
                    " 'executing')", (_DOLD,))
    assert ds.open_executing_count(pg, _DSTORE) == 1
    assert ds.open_executing_count(pg, "DISP_T2") == 1
    assert ds.open_executing_count(pg, "DISP_NOBODY") == 0


# ══════════════════════════════════════════════════════════════════════════════
#  回执落定(2026-09-09):破坏类处置的第二、三种落定来源
#
#  病根:破坏类**只有观测一条落定路径**,而观测流的起点是 delete_feed_success
#  —— 失败的回执一条都进不去。全船队约 800 条 delete/retire 因此停在 executing
#  数周,部分唯一索引挡住同 SKU 再建议 ⇒ 永不重删,改码链也把它们剔出候选面。
# ══════════════════════════════════════════════════════════════════════════════

class _SettleCur:
    """假游标:按执行顺序返回三条 settle SQL 的结果(观测两条 → 回执一条)。"""

    def __init__(self, receipt_rows):
        self.receipt_rows, self.sqls, self._rows = receipt_rows, [], []

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        # 回执那条是**唯一带实参**的(两条观测 SQL 一个参数都不带)——
        # 别拿 'settled_by' 当判据:观测那两条的 jsonb_build_object 里也有它
        self.sqls.append((sql, params))
        self._rows = list(self.receipt_rows) if params else []

    def fetchall(self): return self._rows


class _SettleConn:
    def __init__(self, receipt_rows=()):
        self.cur = _SettleCur(receipt_rows)

    def cursor(self): return self.cur


def _settle_params(conn):
    """回执那条 SQL 的实参(它是唯一带 %(gone)s 的一条)。"""
    return next(pr for sql, pr in conn.cur.sqls if pr and "gone" in pr)


def test_settle_runs_the_observed_verdicts_before_the_receipt_one():
    """**顺序是语义**:两条观测 SQL 先跑,回执那条只收观测收不到的。

    反过来跑的话,一条回执 failed 但商品其实已经消失的行会被记成 ineffective
    (下轮重新建议、重发一次注定失败的 feed),而观测本来能给出 confirmed。
    """
    conn = _SettleConn()
    ds.settle(conn)
    sqls = [sql for sql, _pr in conn.cur.sqls]
    assert len(sqls) == 3
    assert sqls[0] is ds._SETTLE_DELETE_SQL and sqls[1] is ds._SETTLE_RELIST_SQL
    assert sqls[2] is ds._SETTLE_RECEIPT_SQL
    # 判据(哪些码算"已经不在了")从 registry 传进来,本模块不认识任何具体的码
    assert _settle_params(conn)["gone"] == sorted(resources.WALMART_ERR_ITEM_GONE)
    assert _settle_params(conn)["actions"] == list(ds.DESTRUCTIVE_ACTIONS)


def test_receipt_settle_counts_both_buckets_on_top_of_the_totals():
    """confirmed / ineffective 是**总数**(含回执来源),两个 receipt_* 是其中
    回执判的那部分 —— 摘要两行都要报,只报总数就看不出是谁判的。"""
    out = ds.settle(_SettleConn([("confirmed", "receipt_gone"),
                                 ("confirmed", "receipt_gone"),
                                 ("ineffective", "receipt_failed")]))
    assert out == {"confirmed": 2, "ineffective": 1,
                   "receipt_gone": 2, "receipt_failed": 1}


def test_receipt_settle_sql_shape_covers_the_four_receipt_cases():
    """四种回执各归各的(SQL 文本层面钉住判据的形状):

      · 码 ∈ GONE(**不论 status**)⇒ confirmed / receipt_gone
        —— QARTH 那个死档码是 status=success 带回来的,只认 failed 就收不到;
      · failed / missing ⇒ ineffective / receipt_failed;
      · submitted ⇒ 不动(还没轮询到,归 feed_poll);
      · success 不带死档码 ⇒ 不动(归观测核验的 48h 宽限)。
    """
    q = ds._SETTLE_RECEIPT_SQL
    assert ds._RECEIPT_SETTLING == ("failed", "missing")     # success 不在里面
    assert "r.status = ANY(%(settling)s::text[])" in q
    assert "OR r.error_code = ANY(%(gone)s::text[])" in q    # status 之外的另一支
    assert "d.status = 'executing'" in q and "d.feed_id IS NOT NULL" in q
    assert "'receipt_gone'" in q and "'receipt_failed'" in q
    # detail 三件套:回执状态 / 码 / 描述(截 300),查账时不用再回 feed_items 翻
    for key in ("'receipt_status'", "'error_code'", "'error_desc'"):
        assert key in q
    assert "left(r.error_desc, 300)" in q
    # ⚠ 不许写成 `FROM LATERAL (… WHERE fi.feed_id = d.feed_id …)`:PG **不允许**
    # UPDATE 的 FROM 里的 LATERAL 反向引用目标表,实测报
    # `invalid reference to FROM-clause entry for table "d"` —— 而本仓的 SQL
    # 用例只断言文本子串,不连库就发现不了(与"每个参数带 cast"同一条教训)。
    assert "LATERAL" not in q
    # 回执行按**主键** (feed_id, sku) 取:一个 (feed, SKU) 天生只有一行,套一层
    # `DISTINCT ON … ORDER BY submitted_at DESC` 只是每轮把几百万行的流水表
    # 整个排一遍(结果一模一样)。下面那条真库用例证明语义等价。
    assert "FROM ops.feed_items r" in q and "DISTINCT ON" not in q
    assert "r.feed_id = d.feed_id AND r.sku = d.sku" in q


def test_receipt_settle_writes_nothing_outside_the_disposition_table():
    """**只动处置账**:不弃码、不改 walmart_items、不记 product_events。

    弃码点仍是四个(conventions §九),`receipt_gone` 是处置账的收尾、不是身份层
    的结论 —— 目录里死档行本身的根治归 docs/backlog.md §十三。
    """
    q = ds._SETTLE_RECEIPT_SQL
    assert "catalog.walmart_items" not in q and "product_events" not in q
    assert "listing_sources" not in q
    src = inspect.getsource(ds.settle)
    for forbidden in ("sku_codec", "abandon", "record_many"):
        assert forbidden not in src


@needs_pg
def test_receipt_settle_on_a_real_database(pg):
    """真库一轮:五种回执各归各的。文本断言证明不了 UPDATE…FROM 的连接语义。"""
    def _feed(feed_id, sku, status, code=None, desc=None):
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO ops.feed_items (feed_id, sku, workflow, store,"
                " feed_type, status, error_code, error_desc)"
                " VALUES (%s, %s, 'problem_product_cleanup', %s, 'DELETE_ITEM',"
                " %s, %s, %s)", (feed_id, sku, _DSTORE, status, code, desc))

    def _exec_row(sku, feed_id, action="delete"):
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO ops.dispositions (store, sku, source, action,"
                " status, feed_id, executed_at) VALUES (%s, %s, 'scan', %s,"
                " 'executing', %s, now() - interval '2 days')",
                (_DSTORE, sku, action, feed_id))

    gone_code = sorted(resources.WALMART_ERR_ITEM_GONE)[0]
    perm_code = sorted(resources.WALMART_ERR_DESTRUCTIVE_PERMANENT)[0]
    cases = [
        # (sku, 回执 status, 码, 期望状态, 期望 settled_by)
        ("K_GONE_OK", "success", gone_code, "confirmed", "receipt_gone"),
        ("K_GONE_BAD", "failed", gone_code, "confirmed", "receipt_gone"),
        ("K_PERM", "failed", perm_code, "ineffective", "receipt_failed"),
        ("K_TEMP", "failed", "EXT_DATA_ERROR_69730864580258",
         "ineffective", "receipt_failed"),
        ("K_MISSING", "missing", None, "ineffective", "receipt_failed"),
        ("K_WAIT", "submitted", None, "executing", None),
        ("K_SUCCESS", "success", None, "executing", None),
    ]
    for i, (sku, st, code, _want, _by) in enumerate(cases):
        _feed(f"F{i}", sku, st, code, "沃尔玛原文" if code else None)
        _exec_row(sku, f"F{i}")
    # 停用动作也收(顽固件的 retire 那一半):不收就继续每天空烧配额
    _feed("FR", "K_RETIRE", "failed", gone_code, None)
    _exec_row("K_RETIRE", "FR", action="retire")

    out = ds.settle(pg)
    assert out["receipt_gone"] == 3 and out["receipt_failed"] == 3
    with pg.cursor() as cur:
        cur.execute("SELECT sku, status, detail ->> 'settled_by',"
                    " detail ->> 'error_code' FROM ops.dispositions"
                    " WHERE store = %s ORDER BY sku", (_DSTORE,))
        got = {sku: (st, by, code) for sku, st, by, code in cur.fetchall()}
    for sku, _st, code, want, by in cases:
        assert got[sku][:2] == (want, by), sku
        if by:
            assert got[sku][2] == code, sku
    assert got["K_RETIRE"][:2] == ("confirmed", "receipt_gone")


@needs_pg
def test_the_observed_verdict_wins_over_the_receipt_one(pg):
    """观测判决优先:回执 failed、而 catalog_sync 已核验它确实消失了 ⇒ confirmed。

    先跑观测那两条 SQL 的**唯一**作用就在这里 —— 行被观测判掉之后就不再是
    executing,回执那条 UPDATE 自然扫不到它。
    """
    with pg.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.dispositions (store, sku, source, action, status,"
            " feed_id, executed_at) VALUES (%s, 'K_BOTH', 'scan', 'delete',"
            " 'executing', 'FB', now() - interval '2 days')", (_DSTORE,))
        cur.execute(
            "INSERT INTO ops.feed_items (feed_id, sku, workflow, store,"
            " feed_type, status, error_code) VALUES ('FB', 'K_BOTH', 'wf', %s,"
            " 'DELETE_ITEM', 'failed', 'EXT_DATA_ERROR_69730864580258')",
            (_DSTORE,))
        cur.execute(
            "INSERT INTO catalog.product_events (sku, store, event, source)"
            " VALUES ('K_BOTH', %s, 'delete_verified', 'catalog_sync')",
            (_DSTORE,))
    out = ds.settle(pg)
    assert (out["confirmed"], out["receipt_gone"], out["receipt_failed"]) \
        == (1, 0, 0)
    with pg.cursor() as cur:
        cur.execute("SELECT status, detail ->> 'settled_by' FROM"
                    " ops.dispositions WHERE store = %s", (_DSTORE,))
        assert cur.fetchone() == ("confirmed", "delete_verified")
