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
    # (has_pt, 总数, 从没审过, 停L0, 停L1);数字取所有者 2026-08-17 的实测
    "worth": [(True, 113496, 0, 6231, 1),
              (False, 84414, 61, 70714, 66)],
}


def test_products_with_evidence_pt_are_not_counted_as_benefit(monkeypatch):
    """⚠ 有实证 PT 的产品**不靠映射表** —— 把它们算进"值得补"是误导。

    首版按 L0 一刀切,把 A 类 11.3 万件算成"95% 值得补",而实测
    `停在 L1 解不出 1 件` 就已经说明它们根本不缺类目:resolve_pt 的实证级
    (walmart_items / 产品主档)直接直出,映射表是第 ② 级,轮不到。
    补映射对这批自身无用,价值只在同路径将来的新产品。
    """
    monkeypatch.setattr(cg.db, "pg_conn", lambda: _Conn(_DATA))
    out = cg.run({})
    assert "它们不靠映射表" in out and "轮不到" in out
    assert "价值只在同路径**将来的新产品**" in out
    # 收益只算无实证那一档的非 L0 部分
    assert "真正值得补的约 13700 件" in out
    assert "不是收益" in out          # 明说别拿 ① 的数字当排期理由


def test_the_two_sections_do_not_pretend_to_share_an_axis(monkeypatch):
    """⚠ 上面 A/B 按 node 分,这里按产品分 —— 数字对不上是**正常的**。

    两处都叫 A/B 会让人以为该对得上(180166 vs 113496),然后怀疑工具算错。
    """
    monkeypatch.setattr(cg.db, "pg_conn", lambda: _Conn(_DATA))
    out = cg.run({})
    assert "与上面按 node 分的 A/B 不同轴" in out
    assert "① 产品自己有实证 PT" in out and "② 产品自己无实证 PT" in out


def test_empty_taxonomy_stops_early(monkeypatch):
    d = dict(_DATA, summary=(0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(cg.db, "pg_conn", lambda: _Conn(d))
    assert "taxonomy_import" in cg.run({})


def test_bad_only_is_rejected(monkeypatch):
    monkeypatch.setattr(cg.db, "pg_conn", lambda: _Conn(_DATA))
    with pytest.raises(ValueError, match="only 可选"):
        cg.run({"only": "nope"})
