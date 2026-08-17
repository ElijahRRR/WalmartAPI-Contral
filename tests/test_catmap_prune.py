"""catmap_prune 回归:清掉指向"不存在的 PT"的映射行。

所有者定稿 2026-08-17:「不在 spec 模版里的需要清理掉,准入漏了的需要补」。

首版有两处错,都被他在投影表里当场看出来(那批行还在,而且没有
Walmart Category / PTG,一眼就是空的):
  ① 缺省只**降级**不删 —— 降级的行仍留在映射表里,导出到飞书还是那一堆,
     "清理"根本没有发生;
  ② 判据用的是 `walmart_pt_meta`(准入明细),而准入明细只是飞书侧的镜像:
     准入有·spec 无的 66 个 PT 按它判会被当成"有效"漏掉,
     spec 有·准入无的 9 个(全汽配)则会被误删 —— 那些是真 PT,该补表不该删映射。
"""

import pytest

from workflows import catmap_prune as cp


class _Cur:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0
        self._out = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.store.setdefault("sql", []).append(sql)
        if "FROM audit.walmart_pt_meta" in sql:
            self._out = [(p,) for p in self.store["meta"]]
            return
        if "FROM audit.walmart_category_map d" in sql:
            self._out = self.store["rows"]
            return
        self.store["ops"].append(
            ("DELETE" if sql.strip().startswith("DELETE") else "UPDATE", params))
        self.rowcount = 1

    def fetchall(self):
        return self._out


class _Conn:
    def __init__(self, rows, meta=()):
        self.store = {"rows": rows, "meta": set(meta), "ops": []}
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self.store)

    def commit(self):
        self.committed = True


# (路径, PT, 置信度, 备注, 同路径的另一个 PT)
_ROWS = [
    ("Home > Lighting > Book Lights", "Novelty Lights", "低",
     " [catmap_prune:…降级]", "Novelty Lighting"),      # 上一版降过级,行还在
    ("Auto > Brakes > Calipers", "Disc Brake Calipers", "高",
     "", "Automotive Brakes"),                          # spec 有·准入无
    ("Home > Decor > Clocks", "Retired PT", "高", "", ""),   # 准入有·spec 无
    ("Tools > Hammers", "Hammers", "高", "", ""),            # 正常
]
_SPEC = {"Novelty Lighting", "Disc Brake Calipers", "Hammers",
         "Automotive Brakes"}
_META = {"Novelty Lighting", "Retired PT", "Hammers", "Automotive Brakes"}


@pytest.fixture
def _spec(monkeypatch):
    monkeypatch.setattr(cp.pt_spec, "known_pts", lambda: set(_SPEC))


def test_spec_is_the_judge_not_the_admission_sheet(_spec, monkeypatch):
    """⚠ 两个方向都会判错,所以判据必须是 spec 不是准入明细。

    · spec 有·准入无(Disc Brake Calipers)= **真 PT**,该补准入明细,不该删映射;
    · 准入有·spec 无(Retired PT)= 沃尔玛已经没这个 PT,映射是死的,该删。
    """
    conn = _Conn(_ROWS, _META)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True})
    deleted = {p["pt"] for k, p in conn.store["ops"] if k == "DELETE"}
    assert deleted == {"Novelty Lights", "Retired PT"}
    assert "Disc Brake Calipers" not in deleted     # 真 PT,留着
    assert "Hammers" not in deleted
    assert "官方 MP_ITEM spec" in out


def test_meta_mode_judges_differently(_spec, monkeypatch):
    """旧口径保留但要能看出差别:按准入明细判会漏掉 Retired PT、误删汽配那条。"""
    conn = _Conn(_ROWS, _META)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    cp.run({"execute": True, "by": "meta"})
    deleted = {p["pt"] for k, p in conn.store["ops"] if k == "DELETE"}
    assert "Retired PT" not in deleted              # 漏掉了(准入表里有)
    assert "Disc Brake Calipers" in deleted         # 误删(准入表里没有)


def test_default_actually_deletes(_spec, monkeypatch):
    """⚠ 首版缺省只降级 —— 降级的行仍留在表里,导出到飞书还是那一堆空 Category。

    所有者 2026-08-17 在投影表里当场看到这批还在:「数据库的类目映射没有
    清理干净……这些都没有 Walmart Category / PTG,应该被删掉才对」。
    """
    conn = _Conn(_ROWS, _META)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True})
    assert {k for k, _ in conn.store["ops"]} == {"DELETE"}
    assert "**删除**" in out and conn.committed


def test_already_downgraded_rows_are_still_deleted(_spec, monkeypatch):
    """⚠ 上一版降过级的行**必须还能被删** —— 它们正是所有者看到的那批。

    首版的幂等判据是"备注里有 [catmap_prune:"就跳过,于是第二次跑报
    "没有了",而行还在表里。幂等不能变成"再也清不掉"。
    """
    conn = _Conn(_ROWS, _META)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    cp.run({"execute": True})
    assert any(p["pt"] == "Novelty Lights"
               for k, p in conn.store["ops"] if k == "DELETE")


def test_keep_mode_downgrades_with_a_trail(_spec, monkeypatch):
    conn = _Conn(_ROWS, _META)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True, "keep": "1"})
    assert {k for k, _ in conn.store["ops"]} == {"UPDATE"}
    tag = conn.store["ops"][0][1]["tag"]
    assert "不在官方 MP_ITEM spec" in tag and "Novelty Lighting" in tag
    assert "保留行" in out


def test_orphan_paths_are_flagged(_spec, monkeypatch):
    """同路径没有别的映射 → 清掉就真成缺口,必须点名多少条、由谁重建。"""
    conn = _Conn(_ROWS, _META)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True, "dry_run": True})
    assert "1 行**同路径没有别的映射**" in out    # Retired PT 那条
    assert "catmap_mine" in out and "catmap_gap" in out


def test_missing_spec_never_becomes_an_empty_truth(monkeypatch):
    """⚠ spec 取不到就停手 —— 拿空集合当"真相"会把整张表删光。"""
    monkeypatch.setattr(cp.pt_spec, "known_pts",
                        lambda: (_ for _ in ()).throw(
                            FileNotFoundError("_pt_index.json 不存在")))
    monkeypatch.setattr(cp.db, "pg_conn", lambda: _Conn(_ROWS, _META))
    with pytest.raises(RuntimeError, match="已停手"):
        cp.run({"execute": True})


def test_empty_judge_set_stops_too(monkeypatch):
    monkeypatch.setattr(cp.pt_spec, "known_pts", lambda: set())
    monkeypatch.setattr(cp.db, "pg_conn", lambda: _Conn(_ROWS, _META))
    with pytest.raises(RuntimeError, match="删光整张表"):
        cp.run({"execute": True})


def test_dry_run_changes_nothing(_spec, monkeypatch):
    conn = _Conn(_ROWS, _META)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True, "dry_run": True})
    assert "一行未改" in out
    assert conn.store["ops"] == [] and not conn.committed


def test_all_clean_is_said_plainly(_spec, monkeypatch):
    conn = _Conn([("p", "Hammers", "高", "", "")], _META)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    assert "没有" in cp.run({"execute": True})


def test_bad_by_value_is_rejected(monkeypatch):
    monkeypatch.setattr(cp.db, "pg_conn", lambda: _Conn(_ROWS, _META))
    with pytest.raises(ValueError, match="只能是"):
        cp.run({"execute": True, "by": "meta2"})


_MIXED = _ROWS + [
    ("Path > A", cp.SENTINEL, "无", "", ""),
    ("Path > B", cp.SENTINEL, "无", "", ""),
    ("Path > C", "-", "低", "", ""),
    ("Path > D", "", "低", "", ""),
]


def test_every_bucket_is_reported_never_silently_excluded(_spec, monkeypatch):
    """⚠ 首版把哨兵行与 '-' 行写进 SQL 的 WHERE 排除掉,摘要报 306 行,

    而所有者在投影表里数到 846 行"没有 Walmart Category/PTG"的 ——
    差的五百多正是被静默排除的两类。静默排除 = 数字对不上,人只能怀疑工具。
    现在三桶逐桶报数,该不该动由参数定。
    """
    conn = _Conn(_MIXED, _META)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True, "dry_run": True})
    assert "分三类" in out
    assert "① 死 PT" in out and "② 哨兵" in out and "③ 垃圾" in out
    # 三桶之和必须等于人在投影表里看到的那个数
    assert f"共 {2 + 2 + 2} 行" in out       # 死 2(Novelty Lights/Retired PT)+哨兵 2+垃圾 2


def test_sentinel_is_kept_by_default_and_says_why(_spec, monkeypatch):
    """⚠ 哨兵不是垃圾是信息:resolve_pt ⓪ 级读它,把线索带进 LLM 提示词。"""
    conn = _Conn(_MIXED, _META)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True})
    touched = {p["pt"] for _k, p in conn.store["ops"]}
    assert cp.SENTINEL not in touched
    assert "默认保留" in out and "带进 LLM 提示词" in out
    assert "sentinel=drop" in out            # 想清也得告诉人怎么清


def test_sentinel_drop_is_opt_in(_spec, monkeypatch):
    conn = _Conn(_MIXED, _META)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True, "sentinel": "drop"})
    assert cp.SENTINEL in {p["pt"] for _k, p in conn.store["ops"]}
    assert "本次一并清" in out


def test_junk_rows_are_always_cleaned(_spec, monkeypatch):
    """PT 是 '-' 或空:既不是 PT 也不是标注,纯脏数据,不需要开关。"""
    conn = _Conn(_MIXED, _META)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    cp.run({"execute": True})
    touched = {p["pt"] for _k, p in conn.store["ops"]}
    assert "-" in touched and "" in touched
