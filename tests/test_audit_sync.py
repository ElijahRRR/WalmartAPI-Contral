"""审核结论回流回归:取数映射 + 缺行计数 + 幂等更新语义 + 目标三列登记。"""

import contextlib
from datetime import datetime, timezone

from registry import resources
from workflows import audit_sync as wf

AUDIT_ROWS = [
    # asin, verdict, pt, run_id, created_at, l3_cat, l3_text, stage, codes
    ("B0PASS0001", "pass", "Vitamins & Supplements", 101,
     datetime(2026, 8, 1, tzinfo=timezone.utc), None, None, None, None),
    ("B0REJ00001", "reject", "Knives", 102,
     datetime(2026, 8, 2, tzinfo=timezone.utc),
     "Intellectual Property", "logo hit", "L3", "brand_blacklist,llm_ip"),
    ("B0MISS0001", "pending", None, 103,
     datetime(2026, 8, 3, tzinfo=timezone.utc), None, None, "L3", None),
]


class _AuditConn:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None
        self.itersize = 0

    def cursor(self, name=None):
        return self

    def execute(self, sql, args=None):
        self.sql = sql

    def __iter__(self):
        return iter(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _PgConn:
    def __init__(self, known):
        self.known = known
        self.batches: list = []
        self._rc = 0

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        pass

    def fetchall(self):
        return [(a,) for a in self.known]

    def executemany(self, sql, rows):
        self.batches.append((sql, list(rows)))
        self._rc = len(rows)

    @property
    def rowcount(self):
        return self._rc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, audit, pg):
    monkeypatch.setattr(wf.db, "audit_conn",
                        contextlib.contextmanager(lambda: iter([audit])))
    monkeypatch.setattr(wf.db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([pg])))


def test_sync_maps_five_columns_and_counts_missing(monkeypatch):
    audit = _AuditConn(AUDIT_ROWS)
    pg = _PgConn(known=["B0PASS0001", "B0REJ00001"])
    _wire(monkeypatch, audit, pg)

    summary = wf.run({})

    rows = [r for _, batch in pg.batches for r in batch]
    assert [r["asin"] for r in rows] == ["B0PASS0001", "B0REJ00001"]
    p, rej = rows
    assert p["verdict"] == "pass" and p["reason"] is None
    assert p["ver"] == "101" and p["pt"] == "Vitamins & Supplements"
    assert rej["reason"] == "Intellectual Property: logo hit; stage=L3"
    sql = pg.batches[0][0]
    assert "IS DISTINCT FROM" in sql          # 内容没变就不写(写放大纪律)
    assert "marketplace = 'US'" in sql
    assert "缺行 1" in summary and "B0MISS0001" in summary
    assert "pass 1 / reject 1 / pending 1" in summary


def test_reason_falls_back_to_hit_codes():
    row = dict(zip(wf._COLS, ("B0X", "reject", None, 1, None,
                              None, None, "L2", "excluded_category")))
    assert wf._reason(row) == "rules: excluded_category; stage=L2"


def test_pass_never_carries_reason():
    row = dict(zip(wf._COLS, ("B0X", "pass", "Socks", 1, None,
                              "Intellectual Property", "残留", "SHORTCUT", "x")))
    assert wf._reason(row) is None


def test_limit_appends_to_sql(monkeypatch):
    audit = _AuditConn([])
    pg = _PgConn(known=[])
    _wire(monkeypatch, audit, pg)
    wf.run({"limit": "50"})
    assert audit.sql.strip().endswith("LIMIT 50")


def test_registry_store_target_fields_registered():
    """Q4 拍板(2026-08-12):限额表三个目标列的字段常量必须在 registry。"""
    f = resources.RETIRE_LIMITS.fields
    assert f.target_gmv_daily == "目标销售额"
    assert f.target_orders_daily == "目标订单"
    assert f.max_online == "单店最大在线数"
