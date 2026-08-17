"""catmap_promote 回归:LLM 建议升级进映射表的那道通道。

`catmap_suggest` 的建议此前是**死数据零消费**(升级通道等编辑权威定)。
权威 2026-08-17 定下:PG 是权威、飞书是镜子,通道就落在这里。

本文件钉住三条安全线,每条都对应一种"错了也不报错":
  ① 缺省写「中」不写「高」—— 高会让 L1 ②级**直出**,而 LLM 建议确实会错
     (实测首批 50 条里就有 Building Sets→Advent Calendars(高));
  ② as=高 必须点名路径,不点名就写高 = 跳过人工确认这一步;
  ③ 建议的 PT 必须在字典里,否则写进去又是一条死映射(刚清完 150 条那种)。
"""

import pytest

from workflows import catmap_promote as cp


class _Cur:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "FROM audit.category_map_suggestions s" in sql:
            self.store["sql"] = sql
            self._out = self.store["rows"]
            return
        self.store["ops"].append((sql.split()[0], params))
        self.rowcount = 1

    def fetchall(self):
        return self._out


class _Conn:
    def __init__(self, rows):
        self.store = {"rows": rows, "ops": [], "sql": ""}
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
    ("Patio > Outdoor Power Tools > Replacement Parts", "Lawn Mower Parts",
     "高", 2817, "111", "Mower blade"),
    ("Toys & Games > Building Toys > Building Sets", "Advent Calendars",
     "高", 1399, "222", "LEGO set"),
]


def test_default_writes_medium_not_high(monkeypatch):
    """⚠ 缺省写「中」:高会让 L1 ②级直出不再问 LLM,而建议确实会错。

    实测首批里 `Building Sets → Advent Calendars` 就标着「高」——
    写成高的话那条路径的产品从此直出到错类目,上架到错类目比不上架更麻烦。
    """
    conn = _Conn(_ROWS)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True})
    confs = {p["conf"] for k, p in conn.store["ops"] if k == "INSERT"}
    assert confs == {"中"}
    assert "写成**中**置信" in out and "零候选" in out
    assert conn.committed


def test_high_requires_named_paths(monkeypatch):
    """不点名就写高 = 把人工确认那一步跳过了。宁炸不吞。"""
    monkeypatch.setattr(cp.db, "pg_conn", lambda: _Conn(_ROWS))
    with pytest.raises(ValueError, match="必须配"):
        cp.run({"execute": True, "as": "高"})


def test_high_with_named_paths_is_allowed(monkeypatch):
    conn = _Conn(_ROWS[:1])
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True, "as": "高",
                  "paths": "Patio > Outdoor Power Tools > Replacement Parts"})
    assert {p["conf"] for k, p in conn.store["ops"] if k == "INSERT"} == {"高"}
    assert "直出" in out


def test_bad_confidence_value_is_rejected(monkeypatch):
    monkeypatch.setattr(cp.db, "pg_conn", lambda: _Conn(_ROWS))
    with pytest.raises(ValueError, match="只能是"):
        cp.run({"execute": True, "as": "high"})


def test_pick_requires_the_pt_to_exist_in_the_dictionary(monkeypatch):
    """⚠ 建议的 PT 不在字典里就写进去 = 又造一条死映射(刚清完 150 条那种)。"""
    conn = _Conn(_ROWS)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    cp.run({"execute": True, "dry_run": True})
    assert "JOIN audit.walmart_pt_meta" in conn.store["sql"]
    # 已有映射的路径不覆盖 —— 那可能是人工修过的
    assert "NOT EXISTS" in conn.store["sql"]


def test_dry_run_writes_nothing(monkeypatch):
    conn = _Conn(_ROWS)
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    out = cp.run({"execute": True, "dry_run": True})
    assert "一行未写" in out
    assert conn.store["ops"] == [] and not conn.committed


def test_promoted_rows_carry_a_trail(monkeypatch):
    """以后有人翻到这行,要能看出"这不是人定的,是机器建议升上来的"。"""
    conn = _Conn(_ROWS[:1])
    monkeypatch.setattr(cp.db, "pg_conn", lambda: conn)
    cp.run({"execute": True})
    ins = next(p for k, p in conn.store["ops"] if k == "INSERT")
    assert "[catmap_promote:" in ins["notes"] and "原建议置信 高" in ins["notes"]
    # 建议表要标 promoted,否则下轮又被捞出来
    assert any(k == "UPDATE" for k, _ in conn.store["ops"])


def test_nothing_to_promote_points_at_suggest(monkeypatch):
    monkeypatch.setattr(cp.db, "pg_conn", lambda: _Conn([]))
    assert "catmap_suggest" in cp.run({"execute": True})
