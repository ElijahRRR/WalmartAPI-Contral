"""sources_backfill 回归:盲区统计口径、格式路由、幂等写入契约。"""

import contextlib

from workflows import sources_backfill as sb


def _wire(monkeypatch, gap):
    class _Cur:
        def execute(self, sql, args=None):
            self._sql = sql

        def fetchall(self):
            return gap

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    monkeypatch.setattr(sb.db, "pg_conn",
                        contextlib.contextmanager(lambda **kw: iter([_Conn()])))


def test_dry_run_reports_blind_spot_without_writing(monkeypatch):
    _wire(monkeypatch, [("T1", "B0AAAAAAA1"), ("T1", "MANUAL-001")])
    wrote = []
    monkeypatch.setattr(sb.listing_sources, "register",
                        lambda conn, rows: wrote.extend(rows) or len(rows))
    out = sb.run({})
    assert "盲区" in out and "2 行" in out
    assert "amz)1" in out and "unknown,不自动维护)1" in out
    assert "MANUAL-001" in out                      # unknown 样本可见
    assert wrote == []                              # dry-run 一行不写


def test_execute_routes_by_sku_format(monkeypatch):
    """像 ASIN → amz(source_key=sku,与 list_new 登记同构);其余 → unknown
    (source_key 空,不参与自动破坏动作);workflow 统一标 backfill。"""
    _wire(monkeypatch, [("T1", "B0AAAAAAA1"), ("T2", "MANUAL-001")])
    wrote = []
    monkeypatch.setattr(sb.listing_sources, "register",
                        lambda conn, rows: wrote.extend(rows) or len(rows))
    out = sb.run({"execute": True})
    assert "已回填 2 行" in out and "maintenance_scan" in out
    assert wrote[0] == {"store": "T1", "sku": "B0AAAAAAA1",
                        "source_type": "amz", "source_key": "B0AAAAAAA1",
                        "workflow": "backfill"}
    assert wrote[1]["source_type"] == "unknown"
    assert wrote[1]["source_key"] is None
