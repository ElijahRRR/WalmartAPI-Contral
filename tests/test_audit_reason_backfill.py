"""存量「理由未留存」批量刷新(所有者定稿 2026-08-19)。"""

from workflows import audit_reason_backfill as wf


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.conn.executed.append((sql, args))
        if "FROM catalog.products" in sql:
            self._rows = self.conn.pages.pop(0) if self.conn.pages else []
        else:
            self._rows = []

    def executemany(self, sql, rows):
        self.conn.updated.extend(rows)
        assert "audit_reason LIKE" in sql       # 守卫谓词:不覆盖新结论
        assert "audit_status = 'rejected'" in sql

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, pages):
        self.pages, self.executed, self.updated = pages, [], []

    def cursor(self):
        return _Cur(self)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, pages, reasons):
    conn = _Conn(pages)
    import contextlib
    monkeypatch.setattr(wf.db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([conn])))
    monkeypatch.setattr(wf.audit_store, "reject_reasons",
                        lambda c, asins: reasons)
    return conn


def test_backfill_rewrites_from_old_hits(monkeypatch):
    """占位句 → 旧命中人话;孤儿 run 原句保留;分布进摘要。"""
    pages = [[("B0AAAAAAA1", "历史结论(阶段 L0,理由未留存)"),
              ("B0AAAAAAA2", "历史结论(阶段 L2,理由未留存)"),
              ("B0AAAAAAA3", "历史结论(阶段 未知,理由未留存)")]]
    reasons = {
        "B0AAAAAAA1": [("phase0_brand_blacklist", {"matched_brand": "IKEA"})],
        "B0AAAAAAA2": [("l3_reason", {"note": "标题堆砌品牌词,疑似仿品"})],
        # A3:孤儿(hits 也没有)
    }
    conn = _wire(monkeypatch, pages, reasons)
    out = wf.run({"execute": True})
    by = {u["asin"]: u["reason"] for u in conn.updated}
    assert "品牌黑名单" in by["B0AAAAAAA1"]
    assert by["B0AAAAAAA1"].startswith("历史结论(阶段 L0):")
    assert by["B0AAAAAAA2"] == "历史结论(阶段 L2):标题堆砌品牌词,疑似仿品"
    assert "B0AAAAAAA3" not in by                 # 孤儿不动
    assert "改写 2 行" in out and "孤儿" in out and "1 行" in out
    assert "phase0_brand_blacklist" in out        # 分布可见(挑误伤类型用)
    assert "l3_reason" in out


def test_backfill_dry_run_writes_nothing(monkeypatch):
    pages = [[("B0AAAAAAA1", "历史结论(阶段 L0,理由未留存)")]]
    conn = _wire(monkeypatch, pages, {
        "B0AAAAAAA1": [("excluded_category", {"reason": "服饰禁售"})]})
    out = wf.run({})
    assert conn.updated == []
    assert "[DRY-RUN]" in out and "将改写 1 行" in out


def test_backfill_reason_is_capped(monkeypatch):
    """detail 里的长文本不许把理由列变成档案柜。"""
    pages = [[("B0AAAAAAA1", "历史结论(阶段 L3,理由未留存)")]]
    conn = _wire(monkeypatch, pages, {
        "B0AAAAAAA1": [("l3_reason", {"note": "x" * 900})]})
    wf.run({"execute": True})
    assert len(conn.updated[0]["reason"]) < 400
