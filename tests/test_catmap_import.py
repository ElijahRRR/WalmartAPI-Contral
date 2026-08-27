"""catmap_import 回归:飞书映射明细补回库(与 export 反向)。

2026-08-17 生产事故的收尾:`audit.walmart_category_map` 是当年从
mapping_detail_v5.5.xlsx 一次导入的 15,770 行快照,而飞书那张表此后一直在被
维护(挖掘成果 + 人工修冲突),涨到 17,592 行,**库从没往回同步过**。
首版 catmap_export 把库整表重写到飞书,净删 1,847 行(1,695 行指向有效 PT)。

本文件钉三样:表头错位必须炸(不是静默错列)、缺省只增不改(别把 catmap_fix
的修复回滚掉)、dry-run 一行不写。
"""

import pytest

from registry import resources
from workflows import catmap_import as ci

_HDR = list(resources.CATMAP_SHEET_HEADER)
_ROW1 = ["Home Improvement", "Hand Tools", "Hammers", "Hammers",
         "Tools > Hand Tools > Hammers", "123", "1", "高", "a2w_agent",
         "", "v5.5"]
_ROW2 = ["Home", "Decor", "Bird Feeders",
         "Feeders", "Patio > Birding > Feeders", "456", "2", "高",
         "mined_products", "挖掘补的", ""]


class _Cur:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "SELECT amazon_category" in sql:
            self._out = list(self.store["have"])
            return
        key = (params["path"], params["pt"])
        if key in self.store["have"] and "DO NOTHING" in sql:
            self.rowcount = 0
            return
        self.store["have"].add(key)
        self.store["written"].append(params)
        self.rowcount = 1

    def fetchall(self):
        return self._out


class _Conn:
    def __init__(self, have):
        self.store = {"have": set(have), "written": []}
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self.store)

    def commit(self):
        self.committed = True


@pytest.fixture
def sheet(monkeypatch):
    def _set(rows):
        monkeypatch.setattr(ci.feishu, "sheet_row_count", lambda s: len(rows) + 1)
        monkeypatch.setattr(ci.feishu, "sheet_values_rows",
                            lambda s, c1, c2, r1, r2, **kw:
                            list(enumerate([_HDR] + rows, r1)))
    return _set


def test_header_mismatch_stops_instead_of_misreading(sheet, monkeypatch):
    """⚠ 按位置读一张被人改过列的表 = 整表错位,而且一个错都不报。

    飞书表是人在维护的,插一列/改个名随时发生 —— 宁炸不吞。
    """
    monkeypatch.setattr(ci.feishu, "sheet_row_count", lambda s: 2)
    monkeypatch.setattr(ci.feishu, "sheet_values_rows",
                        lambda s, c1, c2, r1, r2, **kw:
                        list(enumerate([["乱七八糟"] + _HDR[1:], _ROW1], r1)))
    with pytest.raises(ValueError, match="表头与登记不符"):
        ci.run({"execute": True})


def test_dry_run_reports_the_gap_and_writes_nothing(sheet, monkeypatch):
    sheet([_ROW1, _ROW2])
    conn = _Conn([("Tools > Hand Tools > Hammers", "Hammers")])
    monkeypatch.setattr(ci.db, "pg_conn", lambda: conn)
    out = ci.run({"execute": True, "dry_run": True})
    assert "飞书有库里没有 1 行" in out
    assert "Bird Feeders(1)" in out       # 缺的是哪个 PT
    assert "一行未写" in out
    assert conn.store["written"] == [] and not conn.committed


def test_default_is_insert_only(sheet, monkeypatch):
    """⚠ 缺省只增不改:库里已有的行可能被 catmap_fix/align 修过,

    拿飞书的旧值盖回去 = 把修复回滚掉,而且看不出来。
    """
    sheet([_ROW1, _ROW2])
    conn = _Conn([("Tools > Hand Tools > Hammers", "Hammers")])
    monkeypatch.setattr(ci.db, "pg_conn", lambda: conn)
    out = ci.run({"execute": True})
    assert [w["pt"] for w in conn.store["written"]] == ["Bird Feeders"]
    assert "只增不改" in out and "库里已有的一个字不动" in out
    assert conn.committed
    # 数字列要转成 int,不能把 '2' 塞进 integer 列
    assert conn.store["written"][0]["rank"] == 2
    assert conn.store["written"][0]["node"] == "456"
    # 空串必须变 None,别把 '' 写进库当成"有值"
    assert conn.store["written"][0]["batch"] is None


def test_update_mode_touches_every_row(sheet, monkeypatch):
    sheet([_ROW1, _ROW2])
    conn = _Conn([("Tools > Hand Tools > Hammers", "Hammers")])
    monkeypatch.setattr(ci.db, "pg_conn", lambda: conn)
    out = ci.run({"execute": True, "update": "1"})
    assert len(conn.store["written"]) == 2      # 已有的那行也被覆盖
    assert "飞书值盖库值" in out


def test_db_having_more_rows_is_reported_but_never_deleted(sheet, monkeypatch):
    """本工作流不删任何东西 —— 但两边不一致要说出来。"""
    sheet([_ROW1])
    conn = _Conn([("Tools > Hand Tools > Hammers", "Hammers"),
                  ("别的路径", "别的PT"), ("再一条", "再一个PT")])
    monkeypatch.setattr(ci.db, "pg_conn", lambda: conn)
    out = ci.run({"execute": True, "dry_run": True})
    assert "库里另有 2 行飞书没有" in out and "不删任何东西" in out


def test_empty_sheet_never_becomes_an_empty_import(sheet, monkeypatch):
    monkeypatch.setattr(ci.feishu, "sheet_row_count", lambda s: 0)
    assert "一行都没读到" in ci.run({"execute": True})
