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
    assert "action = ANY(%(actions)s::text[])" in seen["sql"]
    assert "source" not in seen["sql"].split("ORDER BY")[0].replace(
        "d.source", "").replace("source,", "")     # WHERE 里不再筛来源
    assert seen["params"]["actions"] == list(ds.PROBLEM_ACTIONS)
    # 取件顺序 = (店铺, 动作优先级, 建议时间):执行期按这个顺序截单店上限,
    # 所以优先级高的那些总是先保住。按 action 文本排序会变成字母序(delete
    # 恰好在 relist 前面纯属巧合,retire 就排到 relist 后面去了)
    assert "array_position(%(rank)s::text[], action)" in seen["sql"]
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
