"""处置建议路由器契约(所有者定稿 2026-08-24)。

钉的是"建议合并 → 谁来执行"这一段的四条规矩:
  ① 动作优先级是**全项目唯一出处**,序由所有者定;
  ② 执行件按**动作**领取,不按来源(旧口径按来源领,08-19 生产实见错位);
  ③ 单店破坏类上限**只施加一次**,在执行件领取时(此前两条链各截一次 ⇒ 2N);
  ④ 转态必须落 executed_by —— 合并之后"最终是谁干的"要在库里有答案。
"""

import pathlib
import re

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
    """删除 > 停用 > 反补 > 库存 > 标题 > 价格(所有者定稿 2026-08-24)。

    这条序此前只活在 maintenance_intents._ACTION_RANK 的**一轮内存**里
    (删除 > 库存 > 标题,三个动作),看不见另一条链挂在库里的建议 —— 跨链
    重复删两次就是这么来的。提升作用域之后它必须覆盖全部六个动作。
    """
    assert ds.ACTION_ORDER == ("delete", "retire", "relist",
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
