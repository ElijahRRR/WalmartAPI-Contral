"""catmap_prune 回归:清「改过了但旧行没清」的死映射。

2026-08-17 实证:所有者映射表 150 行死映射,**150 行全是这种** ——
同一 Amazon 路径已经有一条有效 PT,死行只是当年修正时没清
(catmap_fix 的惯例是插新行不删旧行,旧映射当历史证据留着)。

它们不是无害的脏数据:装配 L1 catmap 时同路径两个 DISTINCT PT 判两义 →
连有效那条一起丢,白丢 105 条本可直出的路径。
"""

import pytest

from workflows import catmap_prune as cp


class _Cur:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "SELECT d.amazon_category" in sql:
            self._out = self.store["rows"]
            return
        self.store["ops"].append(("DELETE" if sql.strip().startswith("DELETE")
                                  else "UPDATE", params))
        self.rowcount = 1

    def fetchall(self):
        return self._out


class _Conn:
    def __init__(self, rows):
        self.store = {"rows": rows, "ops": []}
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self.store)

    def commit(self):
        self.committed = True


_ROWS = [
    ("Home > Lighting > Book Lights", "Novelty Lights", "高",
     "阅读灯→Novelty Lights", "Novelty Lighting"),
    ("Home > Lighting > Disco Ball Lamps", "Novelty Lights", "高",
     "迪斯科球→Novelty Lights", "Novelty Lighting"),
]


def test_dry_run_lists_and_changes_nothing(monkeypatch):
    conn = _Conn(_ROWS)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True, "dry_run": True})
    assert "2 行 / 1 个死 PT / 2 条路径" in out
    assert "Novelty Lights" in out and "Novelty Lighting" in out
    assert "一行未改" in out
    assert conn.store["ops"] == [] and not conn.committed


def test_default_downgrades_and_leaves_a_trail(monkeypatch):
    """⚠ 缺省不删:旧映射是历史证据,删了就查不出"当初为什么这么映"。

    降级后 confidence='低' 不再进高置信通道,备注写清为什么降、正确答案是谁。
    """
    conn = _Conn(_ROWS)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True})
    kinds = {k for k, _ in conn.store["ops"]}
    assert kinds == {"UPDATE"}
    tag = conn.store["ops"][0][1]["tag"]
    assert "不在 walmart_pt_meta" in tag and "Novelty Lighting" in tag
    assert "已降级 2 行" in out and conn.committed
    # 清完要提醒重审,否则受影响的产品还卡在 pending
    assert "mode=pending" in out


def test_delete_requires_an_explicit_flag(monkeypatch):
    conn = _Conn(_ROWS)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True, "delete": "1"})
    assert {k for k, _ in conn.store["ops"]} == {"DELETE"}
    assert "已删除 2 行" in out


def test_nothing_to_do_is_said_plainly(monkeypatch):
    monkeypatch.setattr(cp.db, "pg_conn", lambda: _Conn([]))
    assert "没有" in cp.run({"execute": True})


def test_summary_explains_why_these_rows_are_not_harmless(monkeypatch):
    """不解释的话,人会觉得"反正字典闸会拦,留着无所谓"。"""
    monkeypatch.setattr(cp.db, "pg_conn", lambda: _Conn(_ROWS))
    out = cp.run({"execute": True, "dry_run": True})
    assert "两义" in out and "一起丢掉" in out


def test_already_pruned_rows_are_not_touched_again(monkeypatch):
    """⚠ 幂等:降过的不再降。

    所有者 2026-08-17 连跑了两次,同 150 行被处理两遍 —— 降级本身幂等
    (还是 '低'),但**备注会被重复追加**,而且摘要一直报"已降级 150 行",
    看着像每天都在发现新问题。判据 = 备注里已有 [catmap_prune: 标记。
    """
    sql_seen = []

    class _C(_Cur):
        def execute(self, sql, params=None):
            sql_seen.append(sql)
            super().execute(sql, params)

    conn = _Conn(_ROWS)
    monkeypatch.setattr(conn, "cursor", lambda: _C(conn.store))
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    cp.run({"execute": True, "dry_run": True})
    pick = next(s for s in sql_seen if "SELECT d.amazon_category" in s)
    assert "NOT LIKE" in pick and "catmap_prune" in pick


def test_the_trail_tag_matches_what_the_filter_looks_for(monkeypatch):
    """⚠ 留痕的标记与幂等判据必须是同一串 —— 对不上就等于没有幂等。"""
    conn = _Conn(_ROWS)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    cp.run({"execute": True})
    tag = conn.store["ops"][0][1]["tag"]
    assert "[catmap_prune:" in tag          # 与 _SQL_PICK 的 NOT LIKE 同串
