"""pt_census 回归:四源对账,分清"准入漏收"与"当年瞎猜"。

所有者 2026-08-17 的两个假设都要能被这份对账证伪或证实:
  ① 「这不是沃尔玛类目(以前做映射表时瞎猜的)」 → 判定 `瞎猜`
  ② 「沃尔玛类目从 spec 里拿出来时不全」       → 判定 `准入漏了`
两者处置完全相反(改映射 vs 补准入明细),混在一起就没法照着做。

权威性顺序是硬约束:**官方 MP_ITEM spec 是终审**,准入明细只是飞书侧维护的
一份镜像,它有没有收不代表这个 PT 存不存在。
"""

import pytest

from workflows import pt_census as pc


class _Cur:
    def __init__(self, data):
        self._data, self._out = data, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        for key in sorted(self._data, key=len, reverse=True):
            if key in sql:
                self._out = self._data[key]
                return
        self._out = []

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
    "FROM audit.walmart_pt_meta": [
        ("Hammers", "Home Improvement", "Hand Tools", "普通商品", "是"),
        ("Retired PT", "Home", "Old", "普通商品", "是"),      # spec 里没有
    ],
    "FROM audit.walmart_pt_spec": [("Hammers",)],
    "FROM audit.walmart_category_map": [
        ("Hammers", 3), ("Wall Clocks", 155), ("Lonely PT", 0),
    ],
}


@pytest.fixture
def _sources(monkeypatch):
    monkeypatch.setattr(pc.db, "pg_conn", lambda: _Conn(_DATA))
    # 官方 spec:有 Hammers 与 Lonely PT,**没有** Wall Clocks / Retired PT
    monkeypatch.setattr(pc.pt_spec, "known_pts",
                        lambda: {"Hammers", "Lonely PT"})


def test_the_two_hypotheses_land_in_different_buckets(_sources):
    rows, counts = pc._census()
    by = {r["walmart_product_type"]: r["判定"] for r in rows}
    # ① 映射在用,而 spec/准入/模板三处都没有 → 当年瞎猜
    assert by["Wall Clocks"] == "瞎猜"
    # ② 官方 spec 有、准入明细没收 → 准入漏了(R1/R3 对它静默放行)
    assert by["Lonely PT"] == "准入漏了"
    # ③ 准入明细收了但 spec 没有 → 多半是沃尔玛下架了
    assert by["Retired PT"] == "已废弃?"
    assert by["Hammers"] == "ok"
    assert counts["瞎猜"] == 1 and counts["准入漏了"] == 1


def test_summary_says_what_to_do_for_each_bucket(_sources, monkeypatch, tmp_path):
    monkeypatch.setattr(pc.paths, "reports_dir", lambda: tmp_path)
    out = pc.run({})
    # 两类的处置完全相反,摘要必须分开说
    assert "准入明细漏收 1 个真 PT" in out and "补进飞书" in out
    assert "1 个 PT 三处都不存在" in out and "填了等于没填" in out
    assert "无对应Walmart PT" in out          # 三选一里的第二条
    # 映射条数排序:谁影响面大先说
    assert out.index("Wall Clocks") > out.index("Lonely PT")


def test_csv_carries_every_source_column(_sources, monkeypatch, tmp_path):
    monkeypatch.setattr(pc.paths, "reports_dir", lambda: tmp_path)
    pc.run({})
    text = (tmp_path / "pt_census.csv").read_text(encoding="utf-8-sig")
    head = text.splitlines()[0]
    for col in ("在spec", "在准入明细", "在上传模板", "映射条数", "判定"):
        assert col in head
    # 每一源都要能单独看出来,不能只给一个结论
    line = next(l for l in text.splitlines() if l.startswith("Wall Clocks"))
    assert ",瞎猜,,,,155," in line          # spec/准入/模板三个都空


def test_missing_spec_dir_is_not_a_silent_pass(monkeypatch):
    """⚠ spec 没就位 = 这份对账缺最权威那一列,绝不能当成"跑过了"。"""
    monkeypatch.setattr(pc.pt_spec, "known_pts",
                        lambda: (_ for _ in ()).throw(
                            FileNotFoundError("_pt_index.json 不存在")))
    with pytest.raises(RuntimeError, match="缺最权威那一源"):
        pc.run({})


def test_only_filter(_sources, monkeypatch, tmp_path):
    monkeypatch.setattr(pc.paths, "reports_dir", lambda: tmp_path)
    pc.run({"only": "瞎猜"})
    text = (tmp_path / "pt_census.csv").read_text(encoding="utf-8-sig")
    assert "Wall Clocks" in text and "Hammers" not in text
