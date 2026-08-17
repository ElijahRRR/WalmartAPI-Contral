"""catmap_gap 回归:缺口里**哪些值得补**。

所有者 2026-08-17 问:「咱们之前已经全量跑过一次,为什么还有这么大的缺口?
这部分是否是 L0 都过不了的,所以缺?这种缺口没必要去补」。

这个问题**可验证**,不该靠猜:缺口里的产品按最近一轮审核停在哪分档 ——
停在 L0(Phase0 黑名单/禁售大类/®™)的补映射是白补,审核第一层就短路了,
压根不查类目;停在 L1(类目解不出)的**正是缺映射害的**,补了立刻见效。
"""

import pytest

from workflows import catmap_gap as cg


class _Cur:
    def __init__(self, data):
        self._data, self._out = data, []
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        if "tax_nodes" in sql:
            self._out = [self._data["summary"]]
            self.description = [(c,) for c in (
                "tax_nodes", "prod_nodes", "map_nodes", "prod_mapped",
                "gap_a", "gap_a_prod", "gap_b", "gap_b_prod", "gap_c", "gap_d")]
        elif "GROUP BY 1" in sql and "has_pt" in sql:
            self._out = self._data["worth"]
        else:
            self._out = self._data["list"]

    def fetchone(self):
        return self._out[0]

    def fetchall(self):
        return self._out


class _Conn:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self._data)


_DATA = {
    "summary": (34669, 15618, 13474, 10272, 3595, 180166, 1751, 17744, 1, 15858),
    "list": [(111, 4934, 4540, "Golf Cart Accessories", "Sports > Golf", True,
              False)],
    # (has_pt, 总数, 从没审过, 停L0, 停L1)
    "worth": [(True, 180166, 20166, 120000, 40000),
              (False, 17744, 7744, 2000, 8000)],
}


def test_gap_is_split_by_whether_it_is_worth_mapping(monkeypatch):
    monkeypatch.setattr(cg.db, "pg_conn", lambda: _Conn(_DATA))
    out = cg.run({})
    assert "值不值得补映射" in out
    # L0 那一档要点名占比并说清为什么白补
    assert "停在 L0" in out and "**120000**" in out and "(67%" in out
    assert "补映射白补" in out and "第一层就短路" in out
    # L1 那一档相反:正是缺映射害的
    assert "停在 L1 类目解不出 40000 件" in out and "正是缺映射害的" in out
    # 给出净值,免得人自己减
    assert "真正值得补的约 60166 件" in out


def test_never_audited_is_not_counted_as_worthless(monkeypatch):
    """⚠ 「从没审过」≠「不用补」—— 混进去会把该补的说成不该补。"""
    monkeypatch.setattr(cg.db, "pg_conn", lambda: _Conn(_DATA))
    out = cg.run({})
    assert "从没审过 20166 件" in out
    assert "不等于「不用补」" in out


def test_empty_taxonomy_stops_early(monkeypatch):
    d = dict(_DATA, summary=(0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(cg.db, "pg_conn", lambda: _Conn(d))
    assert "taxonomy_import" in cg.run({})


def test_bad_only_is_rejected(monkeypatch):
    monkeypatch.setattr(cg.db, "pg_conn", lambda: _Conn(_DATA))
    with pytest.raises(ValueError, match="only 可选"):
        cg.run({"only": "nope"})
