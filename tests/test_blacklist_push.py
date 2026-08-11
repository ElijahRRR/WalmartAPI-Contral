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
    monkeypatch.setattr(wf.feishu, "sheet_list",
                        lambda s: [("mPwUBu", "黑名单ASIN"), ("beyKyi", "品牌收集")])

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
            self._sql = sql
            if "ORDER BY added_date" in sql:            # 渠道全量(rebuild 用)
                self._rows = calls.get("channel_rows", [])
            elif "ORDER BY created_at, asin" in sql:    # ASIN 全量(rebuild 用)
                self._rows = calls.get("asin_all", [])
            elif "asin_blacklist" in sql:
                self._rows = calls.get("asin_pending", [])
            else:
                self._rows = calls.get("brand_pending", [])
        def fetchall(self): return self._rows
        def fetchone(self):
            key = "asin_stats" if "asin_blacklist" in self._sql else "brand_stats"
            return calls.get(key, (0, 0))

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
        def execute(self, sql, args):
            calls["marked"].append(("asin" if "asin_blacklist" in sql else "brand",
                                    list(args[0]) if args else "ALL"))
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


def test_push_marks_watermark_per_block(wired, monkeypatch):
    """每块写成功当场打 pushed_at——8500 行 3 块就打 3 次,攒最后一起提交
    的话中途崩 = 全部重推。"""
    calls, _ = wired
    monkeypatch.setattr(wf.time, "sleep", lambda s: None)
    calls["asin_pending"] = [(f"B{i:04d}", "沃尔玛-禁售", "2026-08-11")
                             for i in range(8500)]
    out = wf.run({})
    assert "ASIN 表 +8500 行(3 块)" in out
    asin_marks = [k for w, k in calls["marked"] if w == "asin"]
    assert [len(k) for k in asin_marks] == [4000, 4000, 500]
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


def test_brand_projection_reads_channel_table_only():
    """beyKyi 的数据源是渠道表 brand_err_hits,**绝不**碰总清单镜像表
    brand_blacklist(历史上两次走错:只推镜像表自产行→总表已有的品牌
    永远进不了渠道;整表全推→总清单被复制进渠道表)。SQL 文本钉死,
    探针对账口径与推送范围同表。"""
    for sql in (wf._BRAND_PENDING, wf._BRAND_STATS, wf._BRAND_MARK,
                wf._CHANNEL_ALL, wf._CHANNEL_MARK_ALL):
        assert "brand_err_hits" in sql
        assert "brand_blacklist" not in sql
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
    翻动频繁,"曾命中过"作数会把短暂误判的商品永久拉黑。身份 =
    coalesce(asin, sku):清洗出的标准码优先、订货号原文兜底(2026-08-11
    实证 sku≠asin,多店订货号须归并到产品级)。SQL 文本钉死。"""
    from services import blacklist as bl
    assert "DISTINCT ON (coalesce(asin, sku))" in bl._LATEST_CTE
    assert "ORDER BY coalesce(asin, sku), occurred_at DESC" in bl._LATEST_CTE
    assert "ON CONFLICT (asin) DO NOTHING" in bl._BACKFILL_ASIN_SQL


def test_rebuild_asin_apply_overwrites_and_marks_all(wired, monkeypatch):
    """rebuild_asin:擦净按标准 asin 重灌 → ASIN 表整表重写 → 全表打水位。"""
    calls, _ = wired
    overwritten = []
    monkeypatch.setattr(wf.blacklist, "backfill_counts",
                        lambda conn: {"total": 3, "permanent": 2, "brand_cand": 1})
    monkeypatch.setattr(wf.blacklist, "rebuild_asin_blacklist",
                        lambda conn: {"wiped": 56821, "inserted": 2})
    monkeypatch.setattr(wf.feishu, "sheet_overwrite",
                        lambda s, rows: overwritten.append(rows) or len(rows))
    calls["asin_all"] = [("B0GXX75JN5", "沃尔玛-知产", "2026-04-20"),
                        ("D01027HVK3W", "沃尔玛-禁售", "2026-05-01")]
    out = wf.run({"rebuild_asin": "1", "apply": "1"})
    assert "重灌 2 行" in out and "整表重写 2 行" in out
    rows = overwritten[0]
    assert len(rows) == 3               # 表头 + 2 数据行
    assert rows[1] == ["B0GXX75JN5", "沃尔玛-知产", "2026-04-20"]
    assert ("asin", "ALL") in calls["marked"]


def test_rebuild_asin_preview_does_not_write(wired, monkeypatch):
    calls, cells = wired
    cells["asin"] = ["x"] * 4
    monkeypatch.setattr(wf.blacklist, "backfill_counts",
                        lambda conn: {"total": 50000, "permanent": 48000,
                                      "brand_cand": 2702})
    out = wf.run({"rebuild_asin": "1"})
    assert "现有 4 行" in out and "整表重写为 48000 行" in out and "apply=1" in out
    assert calls["writes"] == [] and calls["marked"] == []


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


# ── 品牌渠道重建(一次性:清错版内容 + 从时间线重灌)──────────────────────────

def test_rebuild_brand_preview_does_not_write(wired, monkeypatch):
    """预览只报数:渠道规模 + beyKyi 现有行数 + 将重写成多少行,零写入。"""
    calls, cells = wired
    cells["brand"] = ["a", "b", "c"]
    monkeypatch.setattr(wf.blacklist, "channel_counts",
                        lambda conn: {"with_brand": 2573, "no_brand": 129,
                                      "brands": 1800})
    out = wf.run({"rebuild_brand": "1"})
    assert "可解析品牌 1800 个" in out and "现有 3 行" in out
    assert "整表重写为 1800 行" in out and "apply=1" in out
    assert calls["writes"] == [] and calls["marked"] == []


def test_rebuild_brand_apply_overwrites_and_marks_all(wired, monkeypatch):
    """apply:渠道表重灌 → beyKyi 整表重写(表头 + 全量数据行,错版残留
    由 sheet_overwrite 的尾部裁剪清掉)→ 全表打水位。"""
    calls, cells = wired
    overwritten = []
    monkeypatch.setattr(wf.blacklist, "channel_counts",
                        lambda conn: {"with_brand": 3, "no_brand": 1, "brands": 2})
    monkeypatch.setattr(wf.blacklist, "rebuild_brand_channel",
                        lambda conn: {"wiped": 42064, "brands": 2})
    monkeypatch.setattr(wf.feishu, "sheet_overwrite",
                        lambda s, rows: overwritten.append(rows) or len(rows))
    calls["channel_rows"] = [("Nike", "沃尔玛-品牌", "2026-04-20", "B0A"),
                             ("Sony", "沃尔玛-知产", "2026-05-01", None)]
    out = wf.run({"rebuild_brand": "1", "apply": "1"})
    assert "重灌 2 个品牌" in out and "整表重写 2 行" in out
    rows = overwritten[0]
    assert len(rows) == 3               # 表头 + 2 数据行
    assert rows[1] == ["Nike", "沃尔玛-品牌", "2026-04-20", "B0A"]
    assert rows[2] == ["Sony", "沃尔玛-知产", "2026-05-01", ""]   # 溯源空串
    assert ("brand", "ALL") in calls["marked"]      # 全表打水位


# ── 只读探针(换表格 / "写了看不见"时的第一诊断)──────────────────────────────

def test_probe_reads_back_and_never_writes(wired):
    """探针回读 API 真值(已填行数 + 行 2 内容 + 水位对账),绝不写表、
    绝不打水位——它就是用来在不敢推之前看清现场的。"""
    calls, cells = wired
    cells["asin"] = [f"B{i:04d}" for i in range(500)]
    calls["asin_stats"] = (500, 56821)
    out = wf.run({"probe": "1"})
    assert "已填 500 行" in out and "已推 500 / 自产共 56821" in out
    assert "行 2 回读" in out
    assert calls["writes"] == [] and calls["marked"] == [] and calls["ensured"] == []


def test_probe_flags_missing_sheet_id(wired, monkeypatch):
    """env 指向的文档里没有登记的 sheet_id(换表格后最典型的接线错)——
    必须点名报出来,而不是等推送时写进错的表。"""
    monkeypatch.setattr(wf.feishu, "sheet_list", lambda s: [("zzz", "别的表")])
    out = wf.run({"probe": "1"})
    assert "找不到 sheet_id=mPwUBu" in out and "找不到 sheet_id=beyKyi" in out


def test_probe_warns_on_watermark_mismatch(wired):
    """表行数≠已推水位要亮牌(崩溃重推的重复行 / 表被手动动过),
    但只是提示不拦路——重复行人眼可辨可删。"""
    calls, cells = wired
    cells["asin"] = ["B0001", "B0002", "B0003"]
    calls["asin_stats"] = (2, 10)
    out = wf.run({"probe": "1"})
    assert "表行数≠已推水位" in out


def test_block_size_stays_within_feishu_limit():
    """块大小与 api 层同源:sheet_write_ranges 内部按 4000 行自动切
    (_SHEET_WRITE_BLOCK_ROWS,在线产品总表 13 万行实证;真硬限是单请求
    载荷 ~4MB,20 列×5000 行撞 90227)。_BLOCK 超过 4000 只是白切——
    水位块会被 api 层拆成多个请求,"每块成功即打水位"的粒度就虚了。"""
    from api.feishu import _SHEET_WRITE_BLOCK_ROWS
    assert wf._BLOCK <= _SHEET_WRITE_BLOCK_ROWS == 4000
