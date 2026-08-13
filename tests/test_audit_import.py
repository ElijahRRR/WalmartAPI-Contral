"""audit_import 的契约与 run() 级纪律测试(批次 A)。

搬迁工具的危险面:静默丢数据、静默少搬表、dry-run 不预告破坏动作、
行数错位不回滚——全部用假连接钉到 run() 级(仓内先例 test_cleanup_history)。
"""

import contextlib

import pytest

from workflows import audit_import as ai

# ── 假连接:按 SQL 形状应答,copy 可禁用/记录 ─────────────────────────────────


class _FakeCur:
    def __init__(self, conn):
        self._c = conn
        self._rows = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._rows = self._c._answer(sql, params)

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows

    def copy(self, sql):
        return self._c._copy(sql)


class FakeDB:
    """tables = {表名: {"cols": [(列名, 类型)...], "count": int 或 [依次弹出]}}"""

    def __init__(self, tables, forbid_copy=False):
        self.tables = tables
        self.forbid_copy = forbid_copy
        self.copies: list[str] = []
        self.sql: list[str] = []        # conn.execute 直呼(TRUNCATE/setval)
        self.isolation_level = None
        self.read_only = False

    def cursor(self):
        return _FakeCur(self)

    def execute(self, sql, params=None):
        self.sql.append(sql)
        cur = _FakeCur(self)
        cur._rows = [(1,)]              # setval 返回非 NULL
        return cur

    @staticmethod
    def _tbl(qualified):
        return qualified.split(".", 1)[1]

    def _answer(self, sql, params):
        if "to_regclass(%s) IS NOT NULL" in sql:
            return [(self._tbl(params[0]) in self.tables,)]
        if "pg_attribute" in sql:
            t = self.tables.get(self._tbl(params[0]))
            return list(t["cols"]) if t else []
        if sql.startswith("SELECT count(*)"):
            c = self.tables[self._tbl(sql.split("FROM ", 1)[1].strip())]["count"]
            return [(c.pop(0) if isinstance(c, list) else c,)]
        raise AssertionError(f"意外 SQL:{sql}")

    def _copy(self, sql):
        if self.forbid_copy:
            raise AssertionError("dry-run 不许 COPY:" + sql)
        self.copies.append(sql)
        conn = self

        class _C:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def __iter__(s):
                return iter([b"x\n"])

            def write(s, chunk):
                pass
        return _C()


@contextlib.contextmanager
def _cm(obj):
    yield obj


def _wire(monkeypatch, src, tgt):
    monkeypatch.setattr(ai.db, "legacy_audit_conn", lambda: _cm(src))
    monkeypatch.setattr(ai.db, "pg_conn", lambda: _cm(tgt))


_COLS = [("brand", "text"), ("raw", "jsonb")]


# ── 契约 ─────────────────────────────────────────────────────────────────────

def test_tables_contract():
    """13 张表、runs 在 hits 之前(外键)、不搬清单里的表绝不在列。"""
    assert len(ai.TABLES) == 13
    assert ai.TABLES.index("audit_runs") < ai.TABLES.index("audit_hits")
    for banned in ("products", "llm_cache", "sync_runs",
                   "llm_usage", "llm_route_events"):
        assert banned not in ai.TABLES


def test_fk_pair_selected_together():
    assert ai._pick_tables({"table": "audit_hits"}) == ["audit_runs", "audit_hits"]
    assert ai._pick_tables({"table": "audit_runs"}) == ["audit_runs", "audit_hits"]
    assert ai._pick_tables({}) == list(ai.TABLES)


def test_identity_tables_registered():
    assert set(ai._IDENTITY) == {"audit_runs", "audit_hits", "walmart_error_records"}


def test_replace_rejects_unknown_value():
    """replace=maybe 静默当 false = 用户以为会重灌实际没灌,必须报错。"""
    with pytest.raises(ValueError, match="无法识别"):
        ai._parse_replace({"replace": "maybe"})
    assert ai._parse_replace({"replace": "TRUE"}) is True
    assert ai._parse_replace({}) is False


# ── diff_columns 纯函数 ──────────────────────────────────────────────────────

def test_diff_columns_identical():
    fatal, warn, common = ai.diff_columns(_COLS, _COLS)
    assert not fatal and not warn and common == ["brand", "raw"]


def test_diff_columns_source_only_is_fatal():
    fatal, _, common = ai.diff_columns(
        [("a", "text"), ("extra", "text")], [("a", "text")])
    assert len(fatal) == 1 and "extra" in fatal[0]
    assert common == ["a"]


def test_diff_columns_precision_mismatch_is_fatal():
    """format_type 口径下精度也参与比对——numeric(5,2) ≠ numeric(12,6)。"""
    fatal, _, common = ai.diff_columns(
        [("p", "numeric(5,2)")], [("p", "numeric(12,6)")])
    assert len(fatal) == 1 and "类型不一致" in fatal[0]
    assert common == []


def test_diff_columns_target_only_is_warning():
    fatal, warn, common = ai.diff_columns(
        [("a", "text")], [("a", "text"), ("synced_at", "timestamp with time zone")])
    assert not fatal and len(warn) == 1 and common == ["a"]


# ── run() 级纪律 ─────────────────────────────────────────────────────────────

def test_dry_run_writes_nothing(monkeypatch):
    """dry-run 一行不写:COPY 禁用、conn.execute 零调用,报告仍完整。"""
    src = FakeDB({"blacklist_brands": {"cols": _COLS, "count": 7}}, forbid_copy=True)
    tgt = FakeDB({"blacklist_brands": {"cols": _COLS, "count": 0}}, forbid_copy=True)
    _wire(monkeypatch, src, tgt)
    out = ai.run({"table": "blacklist_brands"})
    assert "dry-run" in out and "导入 7 行" in out
    assert tgt.sql == [] and tgt.copies == []


def test_dry_run_announces_truncate(monkeypatch):
    """replace=yes 的 dry-run 必须预告 TRUNCATE——最危险的动作不许缺席。"""
    src = FakeDB({"blacklist_brands": {"cols": _COLS, "count": 7}}, forbid_copy=True)
    tgt = FakeDB({"blacklist_brands": {"cols": _COLS, "count": 5}}, forbid_copy=True)
    _wire(monkeypatch, src, tgt)
    out = ai.run({"table": "blacklist_brands", "replace": "yes"})
    assert "TRUNCATE 5 行" in out and "重灌 7 行" in out
    assert tgt.sql == []                # 预告归预告,一行不写


def test_missing_source_table_is_fatal(monkeypatch):
    """旧库缺清单里的表 = 致命:dry-run 标 ✗,execute 拒绝——绝不静默少搬。"""
    src = FakeDB({})
    tgt = FakeDB({"blacklist_brands": {"cols": _COLS, "count": 0}})
    _wire(monkeypatch, src, tgt)
    out = ai.run({"table": "blacklist_brands"})
    assert "✗" in out and "旧库无此表" in out
    with pytest.raises(RuntimeError, match="致命"):
        ai.run({"table": "blacklist_brands", "execute": True})


def test_execute_blocked_on_fatal_columns(monkeypatch):
    src = FakeDB({"blacklist_brands":
                  {"cols": [("brand", "text"), ("extra", "text")], "count": 7}})
    tgt = FakeDB({"blacklist_brands": {"cols": [("brand", "text")], "count": 0}})
    _wire(monkeypatch, src, tgt)
    with pytest.raises(RuntimeError, match="致命"):
        ai.run({"table": "blacklist_brands", "execute": True})


def test_execute_rowcount_mismatch_raises(monkeypatch):
    """拷完行数对不上必须炸(整轮回滚),绝不带着缺数据报成功。"""
    src = FakeDB({"blacklist_brands": {"cols": _COLS, "count": 7}})
    tgt = FakeDB({"blacklist_brands": {"cols": _COLS, "count": [5, 5]}})  # 拷后仍 5
    _wire(monkeypatch, src, tgt)
    with pytest.raises(RuntimeError, match="行数不符"):
        ai.run({"table": "blacklist_brands", "execute": True, "replace": "yes"})
    assert any("TRUNCATE" in s for s in tgt.sql)


def test_execute_success_with_identity_setval(monkeypatch):
    """成功路径:体检行 + 执行行都在摘要;identity 表拷后 setval 续接。"""
    cols = [("id", "bigint"), ("asin", "text")]
    src = FakeDB({"walmart_error_records": {"cols": cols, "count": 7}})
    tgt = FakeDB({"walmart_error_records": {"cols": cols, "count": [0, 7]}})
    _wire(monkeypatch, src, tgt)
    out = ai.run({"table": "walmart_error_records", "execute": True})
    assert "7 行 ✓" in out and "体检" in out
    assert any("setval" in s for s in tgt.sql)
    assert len(tgt.copies) == 1
