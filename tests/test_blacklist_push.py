"""blacklist_push 投影:推送范围 + 追加定位 + 分块水位。

方向只有 PG → 飞书。最重的两条纪律:镜像行绝不回推(靠 src_sku 指纹),
水位每块落(崩了重推不丢行)。
"""

from datetime import date

import pytest

from registry.resources import Spreadsheet
from workflows import blacklist_push as wf


@pytest.fixture
def wired(monkeypatch):
    calls = {"writes": [], "marked": [], "ensured": []}
    sheet_cells = {"asin": [], "brand": []}     # 列 A 现有内容(模拟表内已有行)

    def which(sheet):
        return "asin" if sheet.name == "黑名单ASIN" else "brand"

    monkeypatch.setattr(wf.resources, "ASIN_BLACKLIST_SHEET",
                        Spreadsheet(name="黑名单ASIN", token="T", sheet_id="mPwUBu",
                                    columns=("asin", "source", "added_date"),
                                    wiki=True))
    monkeypatch.setattr(wf.resources, "BRAND_ERR_SHEET",
                        Spreadsheet(name="黑名单品牌(后台报错集成)", token="T",
                                    sheet_id="beyKyi",
                                    columns=("brand", "source", "added_date", "sku"),
                                    wiki=True))
    monkeypatch.setattr(wf.feishu, "sheet_row_count", lambda s: 2000)
    monkeypatch.setattr(wf.feishu, "sheet_ensure_rows",
                        lambda s, n: calls["ensured"].append(n) or 0)

    def values(sheet, rng):
        # 行 2 起的列 A:表里已有 len(cells) 行旧数据
        cells = sheet_cells[which(sheet)]
        start = int(rng[1:rng.index(":")])
        return [[c] for c in cells[start - 2:]]
    monkeypatch.setattr(wf.feishu, "sheet_values", values)
    monkeypatch.setattr(wf.feishu, "sheet_write_ranges",
                        lambda s, ups: calls["writes"].append((which(s), ups)) or 1)

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=None):
            self._rows = (calls.get("asin_pending", []) if "asin_blacklist" in sql
                          else calls.get("brand_pending", []))
        def fetchall(self): return self._rows

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
        def execute(self, sql, args):
            calls["marked"].append(("asin" if "asin_blacklist" in sql else "brand",
                                    list(args[0])))
    monkeypatch.setattr(wf.db, "pg_conn", lambda: _Conn())
    return calls, sheet_cells


def test_push_appends_after_existing_rows(wired):
    """追加起点 = 列 A 首个空行——表里已有 3 行旧数据就从第 5 行写起
    (1 表头 + 3 数据),绝不覆盖。"""
    calls, cells = wired
    cells["asin"] = ["B0OLD1", "B0OLD2", "B0OLD3"]
    calls["asin_pending"] = [("B0NEW", "沃尔玛-禁售", "2026-08-11")]
    out = wf.run({})
    assert "ASIN 表 +1 行" in out
    which, ups = calls["writes"][0]
    assert which == "asin" and ups[0][0] == "A5:C5"
    assert ups[0][1] == [["B0NEW", "沃尔玛-禁售", "2026-08-11"]]


def test_push_marks_watermark_per_block(wired):
    """每块写成功当场打 pushed_at——250 行 3 块就打 3 次,攒最后一起提交
    的话中途崩 = 全部重推。"""
    calls, _ = wired
    calls["asin_pending"] = [(f"B{i:04d}", "沃尔玛-禁售", "2026-08-11")
                             for i in range(1250)]
    out = wf.run({})
    assert "ASIN 表 +1250 行(3 块)" in out
    asin_marks = [k for w, k in calls["marked"] if w == "asin"]
    assert [len(k) for k in asin_marks] == [500, 500, 250]
    assert asin_marks[0][0] == "B0000"


def test_push_brand_rows_carry_sku_provenance(wired):
    """品牌表四列:品牌/来源/入库日期/SKU(溯源)。空溯源写空串不写 None。"""
    calls, _ = wired
    calls["brand_pending"] = [("nike", "Nike", "沃尔玛-品牌", "2026-08-11", "B0A")]
    wf.run({})
    which, ups = calls["writes"][0]
    assert which == "brand"
    assert ups[0][1] == [["Nike", "沃尔玛-品牌", "2026-08-11", "B0A"]]
    marks = [k for w, k in calls["marked"] if w == "brand"]
    assert marks == [["nike"]]          # 水位按 brand_key 打


def test_push_nothing_pending(wired):
    calls, _ = wired
    assert wf.run({}) == "黑名单投影:无待推行"
    assert calls["writes"] == [] and calls["marked"] == []


def test_push_warns_at_limit(wired):
    calls, _ = wired
    calls["asin_pending"] = [(f"B{i}", "沃尔玛-禁售", "d") for i in range(5)]
    out = wf.run({"limit": "5"})
    assert "达单轮上限 5" in out


def test_brand_pending_sql_excludes_mirror_rows():
    """镜像行(src_sku IS NULL)绝不回推——只能断言 SQL 文本:
    夹具喂什么都盖不住 WHERE 少一个条件,而少了它的后果是把 risk_sync
    镜像来的 4.2 万品牌整表复制进收集表。"""
    assert "src_sku IS NOT NULL" in wf._BRAND_PENDING
    assert "pushed_at IS NULL" in wf._BRAND_PENDING
    assert "pushed_at IS NULL" in wf._ASIN_PENDING


# ── 历史回填 ──────────────────────────────────────────────────────────────────

def test_backfill_case_labels_match_source_label():
    """回填 SQL 的 CASE 标签必须与 source_label 逐码一致——两处各写一份
    迟早漂,漂了 = 同一类别历史行和实时行在飞书来源列长得不一样。"""
    from services import blacklist as bl
    for code in sorted(bl.PERMANENT):
        assert f"WHEN '{code}' THEN '{bl.source_label(code)[len('沃尔玛-'):]}'"             in bl._BACKFILL_ASIN_SQL, code


def test_backfill_selects_latest_category_only():
    """入选按**最新**类别(DISTINCT ON + occurred_at DESC)——历史里类别
    翻动频繁,"曾命中过"作数会把短暂误判的商品永久拉黑。SQL 文本钉死。"""
    from services import blacklist as bl
    assert "DISTINCT ON (sku)" in bl._LATEST_CTE
    assert "ORDER BY sku, occurred_at DESC" in bl._LATEST_CTE
    assert "ON CONFLICT (asin) DO NOTHING" in bl._BACKFILL_ASIN_SQL


def test_backfill_preview_does_not_write(wired, monkeypatch):
    from services import blacklist as bl
    wrote = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=None):
            if "INSERT" in sql:
                wrote.append(sql)
        def fetchone(self): return (10, 3, 20)
        def fetchall(self): return []

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
    monkeypatch.setattr(wf.db, "pg_conn", lambda: _Conn())
    out = wf.run({"backfill": "1"})
    assert "永久禁止 10 个" in out and "apply=1" in out
    assert wrote == []


def test_block_size_stays_within_feishu_limit():
    """单请求 ≤500 行是飞书实测硬限(90202,maint_sheet._APPEND_BLOCK 同源)。
    谁把 _BLOCK 调大都必须先过这条。"""
    from services.maint_sheet import _APPEND_BLOCK
    assert wf._BLOCK <= _APPEND_BLOCK == 500
