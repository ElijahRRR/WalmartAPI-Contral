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


# ── 新码漏登记的报警桶(2026-09-02:切码后「非零即报警」当场作废)──────────

_OPAQUE = "AK7QM2X9RT4W"


def test_opaque_code_without_registry_row_is_alarmed(monkeypatch):
    """不透明码只能由 sku_codec.mint 在同一事务里发+登记 —— 在架却查不到
    登记行 = 「谁上架谁登记」被绕过了(或 mint 写库回滚过)。这是本工作流
    唯一的真报警,不分桶就会被旧格式存量的噪声淹掉。"""
    _wire(monkeypatch, [("T1", "MANUAL-001"), ("T1", _OPAQUE)])
    monkeypatch.setattr(sb.listing_sources, "register", lambda conn, rows: len(rows))
    lines = sb.run({}).split("\n")
    assert "⚠ 疑似新码漏登记 1 行" in lines[1] and _OPAQUE in lines[1]
    assert "旧格式存量(登记 unknown,不自动维护)1" in lines[0]
    assert _OPAQUE not in lines[0]


def test_dry_run_alarm_keeps_the_dry_run_banner_first(monkeypatch):
    """告警行必须 insert(1,…) 不是 insert(0,…):本工作流常驻 product_chain,
    链通知只取首行 —— 顶掉 🧪 抬头会让一次空跑的告警以真跑的面目进飞书。"""
    _wire(monkeypatch, [("T1", _OPAQUE)])
    monkeypatch.setattr(sb.listing_sources, "register", lambda conn, rows: len(rows))
    lines = sb.run({}).split("\n")
    assert lines[0].startswith("🧪 [DRY-RUN] ")
    assert lines[1].startswith("🧪 [DRY-RUN] ⚠")


def test_summary_is_byte_identical_when_no_opaque_codes(monkeypatch):
    """守门零行为变化:今天的生产库里不该有不透明码 ⇒ 摘要一个字不变。"""
    _wire(monkeypatch, [("T1", "B0AAAAAAA1"), ("T1", "MANUAL-001")])
    monkeypatch.setattr(sb.listing_sources, "register", lambda conn, rows: len(rows))
    assert "⚠ 疑似新码漏登记" not in sb.run({})


def test_opaque_codes_are_still_registered_as_unknown(monkeypatch):
    """报警 ≠ 不登记:orphan 码的真实来源查不出来,而 unknown 的语义就是
    「不参与任何自动破坏动作,等人工归类」—— 写路由一字不改。"""
    _wire(monkeypatch, [("T1", _OPAQUE)])
    wrote = []
    monkeypatch.setattr(sb.listing_sources, "register",
                        lambda conn, rows: wrote.extend(rows) or len(rows))
    sb.run({"execute": True})
    assert wrote[0]["source_type"] == "unknown" and wrote[0]["source_key"] is None
