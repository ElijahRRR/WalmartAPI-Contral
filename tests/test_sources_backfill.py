"""sources_backfill 回归:只登记不猜、累计待归类计数、幂等写入契约。

2026-09-06 所有者定稿:本工作流**不再按 SKU 的长相猜出身**(判型正则与
amz / 旧格式存量 / 新码漏登记三桶全删,schema.sql 的存量回填 INSERT 同日删除)。
在架未登记的行一律登 `unknown` + `source_key=NULL`,归类由人工件
`sources_reclassify` 做。因此本文件删掉了「按形态路由」「opaque 分桶」两类用例,
换成「一律 unknown」「累计计数每轮都报」「样本封顶 8 个」。
"""

import ast
import contextlib
from pathlib import Path

from workflows import sources_backfill as sb


def _wire(monkeypatch, gap, unknown_total=0):
    """输入:gap 行 + 登记簿现有 unknown 数 → 输出:接好的假连接(不碰真库)。"""
    class _Cur:
        def __init__(self):
            self._count = False

        def execute(self, sql, args=None):
            self._count = "count(*)" in sql

        def fetchall(self):
            return [] if self._count else gap

        def fetchone(self):
            return (unknown_total,) if self._count else None

        def executemany(self, sql, rows):
            raise AssertionError("写库必须走 listing_sources.register")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    monkeypatch.setattr(sb.db, "pg_conn",
                        contextlib.contextmanager(lambda **kw: iter([_Conn()])))


def _catch_register(monkeypatch):
    wrote: list[dict] = []
    monkeypatch.setattr(sb.listing_sources, "register",
                        lambda conn, rows: wrote.extend(rows) or len(rows))
    return wrote


def test_dry_run_previews_without_writing(monkeypatch):
    """空跑一行不写;首行报「本轮新增 N|累计 unknown 待归类 M」。"""
    _wire(monkeypatch, [("T1", "B0AAAAAAA1"), ("T1", "MANUAL-001")],
          unknown_total=7)
    wrote = _catch_register(monkeypatch)
    out = sb.run({})
    lines = out.split("\n")
    assert lines[0].startswith("🧪 [DRY-RUN] 本轮新增登记 2 行(unknown,待人工归类)")
    assert "累计 unknown 待归类 9 行" in lines[0]   # 7 现有 + 2 本轮(写完的样子)
    assert wrote == []                              # dry-run 一行不写
    assert "真跑将写 catalog.listing_sources" in out


def test_execute_registers_every_row_as_unknown(monkeypatch):
    """**只登记不猜**:像 ASIN 的串与人工号走同一条路 —— 一律
    `source_type='unknown'` + `source_key=NULL` + `workflow='backfill'`。
    出身由人经 sources_reclassify 认,机器不猜(所有者 2026-09-06)。"""
    _wire(monkeypatch, [("T1", "B0AAAAAAA1"), ("T2", "MANUAL-001"),
                        ("T2", "AK7QM2X9RT4W")], unknown_total=10)
    wrote = _catch_register(monkeypatch)
    out = sb.run({"execute": True})
    assert out.split("\n")[0].startswith("本轮新增登记 3 行(unknown,待人工归类)")
    assert "累计 unknown 待归类 10 行" in out.split("\n")[0]  # 真跑:写完再数
    assert [r["source_type"] for r in wrote] == ["unknown"] * 3
    assert all(r["source_key"] is None for r in wrote)
    assert all(r["workflow"] == "backfill" for r in wrote)
    assert wrote[0] == {"store": "T1", "sku": "B0AAAAAAA1",
                        "source_type": "unknown", "source_key": None,
                        "workflow": "backfill"}


def test_source_key_is_always_null(monkeypatch):
    """守门:登记行的 source_key 恒 NULL —— 猜出来的键会让这行第一次满足
    消费方 `source_type='amz' AND source_key IS NOT NULL` 的 JOIN,盲区变辖区
    (破坏面只许在 sources_reclassify 那一步、由人打开)。"""
    _wire(monkeypatch, [("T1", f"B0AAAAAA{i:02d}") for i in range(5)])
    wrote = _catch_register(monkeypatch)
    sb.run({"execute": True})
    assert wrote and not any(r["source_key"] for r in wrote)
    assert not any(r["source_type"] == "amz" for r in wrote)


def test_pending_total_is_reported_every_round(monkeypatch):
    """**每轮都报累计**:零新增的那一轮照样报「累计 unknown 待归类 M」——
    这正是"有新行没人归类"的信号不再登记一次就永久沉默的原因。"""
    _wire(monkeypatch, [], unknown_total=42)
    wrote = _catch_register(monkeypatch)
    out = sb.run({"execute": True})
    lines = out.split("\n")
    assert lines[0] == ("本轮新增登记 0 行(unknown,待人工归类)|"
                        "累计 unknown 待归类 42 行")
    assert wrote == []                      # 没有缺口就没有行要写
    assert "⚠" not in out                   # 零缺口不报警


def test_sample_is_capped_at_eight(monkeypatch):
    """样本最多 8 个 (店, SKU):摘要进飞书,整批几千行会把通知撑爆。"""
    gap = [("T1", f"SKU-{i:03d}") for i in range(20)]
    _wire(monkeypatch, gap)
    _catch_register(monkeypatch)
    out = sb.run({})
    sample = [ln for ln in out.split("\n") if ln.startswith("  样本:")]
    assert len(sample) == 1
    assert "SKU-007" in sample[0] and "SKU-008" not in sample[0]


def test_dry_run_alarm_keeps_the_dry_run_banner_first(monkeypatch):
    """告警行必须 insert(1,…) 不是 insert(0,…):本工作流常驻 product_chain,
    链通知只取首行 —— 顶掉 🧪 抬头会让一次空跑的告警以真跑的面目进飞书。"""
    _wire(monkeypatch, [("T1", "MANUAL-001")])
    _catch_register(monkeypatch)
    lines = sb.run({}).split("\n")
    assert lines[0].startswith("🧪 [DRY-RUN] 本轮新增登记 ")
    assert lines[1].startswith("🧪 [DRY-RUN] ⚠ 在架未登记来源 1 行")


def test_summary_and_docstring_point_at_sources_reclassify(monkeypatch):
    """人工归类的入口只有一条,摘要尾行与模块头注都得写明,否则这批
    unknown 行没人知道该怎么处理。"""
    _wire(monkeypatch, [("T1", "MANUAL-001")], unknown_total=3)
    _catch_register(monkeypatch)
    out = sb.run({"execute": True})
    assert out.split("\n")[-1].startswith("  人工归类:`python cli.py sources_reclassify`")
    assert "-p file=" in out and "apply=1" in out
    assert "sources_reclassify" in sb.__doc__


def test_no_shape_guessing_left_in_the_module():
    """守门:判型逻辑不许回来 —— 本工作流一个字都不许按 SKU 的长相猜出身。

    射程是**代码不是模块头注**(头注要讲清"谁上架谁登记"里 mint 那一跳,
    扫那句话没有意义 —— 与 tests/test_sku_guard.py 的 `_body` 同一条纪律)。
    """
    src = Path(sb.__file__).read_text(encoding="utf-8")
    doc = ast.get_docstring(ast.parse(src))
    body = src.replace(doc, "", 1) if doc else src
    assert not hasattr(sb, "_ASIN_RE")
    assert not hasattr(sb, "sku_codec"), "不许再 import 形态判据的家"
    assert "re.compile" not in body, "不许再有按 SKU 长相判型的正则"
    assert "is_opaque" not in body, "不许再调 is_opaque 之类的形态判据"
    assert "SOURCE_AMZ" not in body
