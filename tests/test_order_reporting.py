"""迟到订单补齐、历史两列保护和不完整 API 的生产回归。"""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest

from services import order_reporting as r

STORE = {"name": "S"}


def test_report_date_is_end_anchor_not_calendar_day():
    assert r.window(date(2026, 11, 11)) == (
        "2026-11-09T22:30:00Z", "2026-11-10T22:30:00Z")


@pytest.mark.parametrize("pos,total", [(["1"], 2), (["1", "1"], 2), ([""], 1)])
def test_incomplete_or_duplicate_pages_never_reach_ingest(monkeypatch, pos, total):
    def fetch(*a, stats, **kw):
        stats["total"] = total
        return [{"purchaseOrderId": p} for p in pos]
    monkeypatch.setattr(r.orders, "iter_orders", fetch)
    monkeypatch.setattr(r.order_ingest, "persist", lambda *a: pytest.fail("不可写不完整结果"))
    with pytest.raises(RuntimeError, match="不完整"):
        r.sync_window(STORE, "start", "end")


def test_mid_page_failure_never_writes_partial_data(monkeypatch):
    def fetch(*a, stats, **kw):
        stats["total"] = 2
        yield {"purchaseOrderId": "one"}
        raise TimeoutError("第二页超时")
    monkeypatch.setattr(r.orders, "iter_orders", fetch)
    monkeypatch.setattr(r.order_ingest, "persist", lambda *a: pytest.fail("不可写部分结果"))
    with pytest.raises(TimeoutError):
        r.sync_window(STORE, "start", "end")


def test_window_uses_shared_ingest_before_returning_counts(monkeypatch):
    rows = [{"purchaseOrderId": "late"}]
    events = []
    monkeypatch.setattr(r, "fetch_window", lambda *a: (rows, 1, Decimal("60.47")))
    monkeypatch.setattr(r.order_ingest, "persist", lambda store, data: events.append((store, data)))
    assert r.sync_window(STORE, "a", "b") == (1, Decimal("60.47"))
    assert events == [(STORE, rows)]


@pytest.fixture
def history(monkeypatch):
    row = {"count": 2, "amount": Decimal("78.59"), "payout": Decimal("999.12")}
    writes = []

    class Cursor:
        rowcount = 1

        def fetchall(self):
            return [("S", date(2026, 11, 11), row["count"], row["amount"])]

    class Conn:
        def execute(self, sql, args):
            if sql.lstrip().startswith("UPDATE"):
                assert "payout" not in sql and "seller_name" not in sql
                assert "IS NOT DISTINCT FROM" in sql
                assert args[4:] == (row["count"], row["amount"])
                row.update(count=args[0], amount=args[1])
                writes.append(args)
            return Cursor()

    @contextmanager
    def conn():
        yield Conn()

    monkeypatch.setattr(r.db, "pg_conn", conn)
    monkeypatch.setattr(r, "pg_stats", lambda *a: (3, Decimal("139.06")))
    monkeypatch.setattr(r, "sync_window", lambda *a: (3, Decimal("139.06")))
    monkeypatch.setattr(r, "fetch_window", lambda *a: ([], 3, Decimal("139.06")))
    return row, writes


def test_historical_late_order_correction_is_idempotent_and_scoped(history):
    row, writes = history
    out = r.reconcile_history([STORE], date(2026, 11, 15))
    assert "修正 1 行" in out and "78.59" in out and "139.06" in out
    assert row == {"count": 3, "amount": Decimal("139.06"), "payout": Decimal("999.12")}
    assert "修正 0 行" in r.reconcile_history([STORE], date(2026, 11, 15))
    assert len(writes) == 1


def test_preview_does_not_ingest_or_update(history, monkeypatch):
    row, writes = history
    monkeypatch.setattr(r, "sync_window", lambda *a: pytest.fail("预览不可落库"))
    out = r.reconcile_history([STORE], date(2026, 11, 15), dry_run=True)
    assert "拟改 1 行" in out
    assert row["count"] == 2 and writes == []


@pytest.mark.parametrize("failure", ["api", "mismatch"])
def test_failed_verification_preserves_all_old_kpi(history, monkeypatch, failure):
    row, writes = history
    if failure == "api":
        def bad(*a):
            raise TimeoutError("API 不可用")
        monkeypatch.setattr(r, "sync_window", bad)
    else:
        monkeypatch.setattr(r, "sync_window", lambda *a: (4, Decimal("199.53")))
    out = r.reconcile_history([STORE], date(2026, 11, 15))
    assert "未校正" in out and writes == [] and row["count"] == 2


def test_current_or_future_date_is_rejected():
    with pytest.raises(ValueError):
        r.reconcile_history([STORE], date(2026, 11, 15), dates=[date(2026, 11, 15)])
