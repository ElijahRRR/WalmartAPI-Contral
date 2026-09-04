"""blacklist_push 投影:推送范围 + 追加定位 + 分块水位。

方向只有 PG → 飞书。最重的两条纪律:镜像行绝不回推(靠 src_sku 指纹),
水位每块落(崩了重推不丢行)。
"""

from datetime import date

import pytest

from registry.resources import Spreadsheet
from services import blacklist_sheet as sheets
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
        if start == 1:
            return []          # 表头行回读为空 → 回落 registry 登记列名
        return [[c] for c in cells[start - 2:]]
    monkeypatch.setattr(wf.feishu, "sheet_values_small", values)
    monkeypatch.setattr(wf.feishu, "sheet_write_ranges",
                        lambda s, ups: calls["writes"].append((which(s), ups)) or 1)
    calls["overwritten"] = {}
    monkeypatch.setattr(wf.feishu, "sheet_overwrite",
                        lambda s, body: calls["overwritten"].__setitem__(which(s), body) or len(body))

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


def test_push_rewrites_whole_sheet_from_db(wired):
    """整表重写:不管飞书里原来是什么,按库里全量覆盖(所有者定稿 2026-08-17)。

    换掉的是"按 pushed_at 水位只追加"那套 —— 增量要先线性扫列 A 找空行
    (5.7 万行的表每轮 285 个 GET,比写还贵),表被手工删过几行水位就永久
    对不上,崩在半路还留重复行。整表重写没有水位,天然幂等。
    """
    calls, cells = wired
    cells["asin"] = ["B0OLD1", "B0OLD2", "B0OLD3"]        # 表里原有的,全不管
    calls["asin_all"] = [("B0NEW", "沃尔玛-禁售", "2026-08-11")]
    calls["channel_rows"] = [("Nike", "沃尔玛-品牌", "2026-08-11", "B0A")]
    out = wf.run({"allow_shrink": "1"})       # 3 行 → 1 行,本用例不测护栏
    assert "整表重写 1 行" in out
    body = [r for r in calls["overwritten"]["asin"]]
    assert body[0][0] == "asin"               # 表头行保留(回读不到用登记列名)
    assert body[1] == ["B0NEW", "沃尔玛-禁售", "2026-08-11"]
    assert len(body) == 2                     # 表头 + 1 行,旧的三行没了
    # 水位改成整表打:不再按块传 key
    assert ("asin", "ALL") in calls["marked"]


def test_push_brand_rows_carry_sku_provenance(wired):
    """品牌表四列:品牌/来源/入库日期/SKU(溯源)。空溯源写空串不写 None。"""
    calls, _ = wired
    calls["channel_rows"] = [("Nike", "沃尔玛-品牌", "2026-08-11", None)]
    wf.run({"allow_shrink": "1"})
    body = calls["overwritten"]["brand"]
    assert body[1] == ["Nike", "沃尔玛-品牌", "2026-08-11", ""]


def test_push_stops_dead_when_db_returns_far_fewer_rows(wired):
    """骤缩护栏:库里只查出一小半就停手,一格不写。

    catmap_export 2026-08-17 的生产事故:整表重写把飞书 17592 行写成 15770 行,
    净丢 1847 行,当时没有任何东西拦住。PG 是权威没错,但"库这次只查出
    一小半"(查询写错/连接中断拿到半截)和"本来就该少"长得一模一样。
    """
    calls, cells = wired
    cells["asin"] = [f"B{i:04d}" for i in range(1000)]
    calls["asin_all"] = [("B0001", "沃尔玛-禁售", "d")]      # 1000 → 1
    calls["channel_rows"] = []
    out = wf.run({})
    assert "已停手,一格未写" in out and "allow_shrink" in out
    assert "asin" not in calls["overwritten"], "护栏没拦住,表被覆盖了"
    # 一张表停手不该拖垮另一张
    assert "brand" in calls["overwritten"]


def test_shrink_guard_can_be_overridden(wired):
    calls, cells = wired
    cells["asin"] = [f"B{i:04d}" for i in range(1000)]
    calls["asin_all"] = [("B0001", "沃尔玛-禁售", "d")]
    wf.run({"allow_shrink": "1"})
    assert "asin" in calls["overwritten"]


def test_brand_projection_reads_channel_table_only():
    """beyKyi 的数据源是渠道表 brand_err_hits,**绝不**碰总清单镜像表
    brand_blacklist(历史上两次走错:只推镜像表自产行→总表已有的品牌
    永远进不了渠道;整表全推→总清单被复制进渠道表)。"""
    for sql in (sheets.BRAND_STATS, sheets.CHANNEL_ALL,
                sheets.CHANNEL_MARK_ALL):
        assert "brand_err_hits" in sql
        assert "brand_blacklist" not in sql


# ── 历史回填 ──────────────────────────────────────────────────────────────────

def test_回填的来源标签只有一处出生():
    """来源标签由 `source_label()` 一处产出(原来回填 SQL 里另写了一份 CASE)。
    两处各写一份迟早漂,漂了 = 同一类别的历史行和实时行在飞书来源列长得不一样。"""
    import inspect
    from services import blacklist as bl
    assert "source_label(code)" in inspect.getsource(bl._judge_events)
    assert not hasattr(bl, "_label_case")      # 那份 CASE 已随过渡桥删掉


def test_backfill_selects_latest_category_only():
    """入选按**最新**事件(DISTINCT ON + occurred_at DESC)——历史里类别
    翻动频繁,"曾命中过"作数会把短暂误判的商品永久拉黑。身份 =
    coalesce(asin, sku):清洗出的标准码优先、订货号原文兜底(2026-08-11
    实证 sku≠asin,多店订货号须归并到产品级)。SQL 文本钉死。"""
    from services import blacklist as bl
    assert "DISTINCT ON (coalesce(asin, sku))" in bl._LATEST_CTE
    assert "ORDER BY coalesce(asin, sku), occurred_at DESC" in bl._LATEST_CTE
    assert "ON CONFLICT (asin) DO NOTHING" in bl._INSERT_ASIN_SQL


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
    """⚠ 预览的「够格永久拉黑 N 个」必须与真写**同一条判据**(都走
    `_judge_events`)。两处各算各的 = 预览说 3 万、真写写 7 万,而两边看着都正常。

    同时钉住所有者 2026-09-04 定的判据:**一个 asin 的多条历史报错里,
    够格拉黑的那条最高优先级**,不是取最新。
    """
    wrote = []

    class _Cur:
        def __init__(self): self._q = ""
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=None):
            self._q = sql
            if "INSERT" in sql:
                wrote.append(sql)
        def executemany(self, sql, rows):
            wrote.append(sql)
        def fetchone(self): return (3, 20)          # (brand_cand, total)
        def fetchall(self):
            if "FROM catalog.asin_blacklist" in self._q:
                return [("B0DDD",)]                 # 已在表里 ⇒ 不算新增
            assert "GROUP BY 1" in self._q, self._q[:80]   # 产品事件那条
            # (asin, [[code, term], …], sku, store, reason, latest)
            return [
                # ⚠ B0AAA 的**最新**事件是「过期」,而历史上有一条真禁售 ——
                #    旧口径(取最新)会漏掉它,新口径必须拉黑。
                ("B0AAA", [["EXPIRED", None], ["POLICY", None]],
                 "SKU-A", "s1", "prohibited product policy", None),
                ("B0CCC", [["IP", None]], "SKU-C", "s1", "IP complaint", None),
                # 全部不够格 ⇒ 不拉黑(其余报错只作记录)
                ("B0BBB", [["PT_WRONG", None]], "SKU-B", "s1", "pt wrong", None),
                ("B0DDD", [["POLICY", None]], "SKU-D", "s1", "policy", None),
            ]

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
    monkeypatch.setattr(wf.db, "pg_conn", lambda: _Conn())
    out = wf.run({"backfill": "1"})
    # B0AAA(历史里那条禁售)+ B0CCC + B0DDD = 3;B0BBB 的 PT_WRONG 判出去
    assert "该永久拉黑 3 个" in out
    # B0DDD 已在表里 ⇒ 真跑只新增 2 条
    assert "真跑只会新增 2 条" in out and "回填只加不减" in out
    assert "apply=1" in out
    # ⚠ 摘要必须点明「产品级判定 ≠ 行级记录」,以及 blacklist_route 判据没跟上
    assert "产品级判定" in out and "行级记录" in out
    assert "blacklist_route" in out and "判据没跟上" in out
    assert wrote == []


# ── 品牌渠道重建(一次性:清错版内容 + 从时间线重灌)──────────────────────────

def test_rebuild_brand_preview_does_not_write(wired, monkeypatch):
    """预览只报数:总表认领腿 + 时间线推导腿 + beyKyi 现有行数,零写入。"""
    calls, cells = wired
    cells["brand"] = ["a", "b", "c"]
    monkeypatch.setattr(wf.blacklist, "channel_counts",
                        lambda conn: {"with_brand": 2573, "no_brand": 129,
                                      "brands": 1800, "master": 2609})
    out = wf.run({"rebuild_brand": "1"})
    assert "沃尔玛来源品牌 2609 个" in out and "可解析品牌 1800 个" in out
    assert "现有 3 行" in out and "≤4409" in out and "apply=1" in out
    assert calls["writes"] == [] and calls["marked"] == []


def test_channel_seed_claims_only_walmart_sourced_master_rows():
    """认领腿 SQL 钉死:只认来源「沃尔玛…」的总表镜像行(旧后台收集的
    历史渠道),DO NOTHING 不覆盖;条件放宽 = 把别的渠道也复制进来,
    又回到②号错版。"""
    from services import blacklist as bl
    assert "LIKE '沃尔玛%%'" in bl._CHANNEL_SEED_SQL
    assert "ON CONFLICT (brand_key) DO NOTHING" in bl._CHANNEL_SEED_SQL
    assert "INSERT INTO catalog.brand_err_hits" in bl._CHANNEL_SEED_SQL


def test_rebuild_brand_apply_overwrites_and_marks_all(wired, monkeypatch):
    """apply:认领+推导重灌 → beyKyi 整表重写(表头 + 全量数据行,错版残留
    由 sheet_overwrite 的尾部裁剪清掉)→ 全表打水位。"""
    calls, cells = wired
    overwritten = []
    monkeypatch.setattr(wf.blacklist, "channel_counts",
                        lambda conn: {"with_brand": 3, "no_brand": 1,
                                      "brands": 2, "master": 0})
    monkeypatch.setattr(wf.blacklist, "rebuild_brand_channel",
                        lambda conn: {"wiped": 42064, "seeded": 0, "derived": 2})
    monkeypatch.setattr(wf.feishu, "sheet_overwrite",
                        lambda s, rows: overwritten.append(rows) or len(rows))
    calls["channel_rows"] = [("Nike", "沃尔玛-品牌", "2026-04-20", "B0A"),
                             ("Sony", "沃尔玛-知产", "2026-05-01", None)]
    out = wf.run({"rebuild_brand": "1", "apply": "1"})
    assert "时间线推导 2 个" in out and "整表重写 2 行" in out
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


def test_chunking_is_delegated_to_the_api_layer():
    """整表重写之后,切块完全归 api 层(sheet_overwrite),本工作流不再自带块大小。

    ⚠ 旧值 4000 是实测天花板(真硬限是单请求载荷约 4MB,20 列×5000 行撞 90227);
    2026-08-27 起换成官方限额 ×95% 的登记表值 _SHEET_WRITE_MAX_ROWS=4750,
    并另加一条字节预算管住载荷 —— 行数不再替字节数背锅。谁想调大先读登记表。
    """
    from api.feishu import _SHEET_WRITE_BYTE_BUDGET, _SHEET_WRITE_MAX_ROWS
    assert _SHEET_WRITE_MAX_ROWS == 4750        # 官方 5000 行 × 95%
    assert _SHEET_WRITE_BYTE_BUDGET == 9_000_000
    assert not hasattr(wf, "_BLOCK"), "又自带了一份块大小,两处必漂"
    assert not hasattr(wf, "_append"), "增量追加应随水位一起退役"


def test_next_empty_scans_in_big_blocks(wired, monkeypatch):
    """找空行是 O(表已填行数) 的 GET,块太小会把整轮拖成分钟级。

    所有者 2026-08-17 实见日志里全是
    `GET .../values/mPwUBu!A9202:A9401`(200 行一段),还以为"一次只写 200 行"
    —— 那不是写,是这里在逐段**读**。ASIN 表已填 5.7 万行 ⇒ 285 个 GET、
    每个约 0.3 秒 ≈ 一分半;而真正的写只有 3 个请求(4000 行/块)。
    """
    calls, cells = wired
    cells["asin"] = ["A%d" % i for i in range(12000)]      # 已填 12000 行
    ranges = []
    real = wf.feishu.sheet_values_small

    def spy(sheet, rng):
        ranges.append(rng)
        return real(sheet, rng)
    monkeypatch.setattr(wf.feishu, "sheet_values_small", spy)
    monkeypatch.setattr(wf.feishu, "sheet_row_count", lambda s: 20000)

    row = sheets.next_empty(wf.resources.ASIN_BLACKLIST_SHEET)
    assert row == 12002                                    # 表头 + 12000 行
    # 200 行一段要 60 个请求;5000 一段只要 3 个
    assert len(ranges) <= 4, f"扫了 {len(ranges)} 个请求,块太小:{ranges[:3]}"
    assert sheets._SCAN_BLOCK >= 5000


def test_shrink_guard_measures_filled_rows_not_grid_size(wired):
    """护栏比的是**已填行数**,不是网格大小 —— 否则每轮都会误停手。

    飞书网格被 ensure_rows 撑大后只增不减:6.7 万行的表若网格 7 万,
    拿 `sheet_row_count` 当"现有行数"会算成"缩了 2365 行"从而每轮停手,
    护栏反倒成了故障源。
    """
    import inspect
    src = inspect.getsource(sheets.rewrite_sheet)
    guard = src[src.index("if not allow_shrink"):]
    # ⚠ 必须**剥掉注释**再比:这一段的注释里正好写着 sheet_row_count(解释为什么
    # 不用它),连注释一起 grep 会被自己的说明绊倒(CLAUDE.md 记的那种假阳性)
    code = "\n".join(ln.split("#")[0] for ln in guard.splitlines())
    assert "next_empty" in code
    assert "sheet_row_count" not in code, "又拿网格大小当现有行数了"

    # 行为验证:网格 2000 而只填了 1000 行,库里给 995 行(缩 0.5%)应放行
    calls, cells = wired
    cells["asin"] = [f"B{i:04d}" for i in range(1000)]
    calls["asin_all"] = [(f"B{i:04d}", "沃尔玛-禁售", "d") for i in range(995)]
    calls["channel_rows"] = []
    out = wf.run({})
    assert "asin" in calls["overwritten"], f"网格 2000 把它误判成骤缩了:{out}"


# ── problem_scan 跑完立刻投影(所有者定稿 2026-08-17)────────────────────────

def test_problem_scan_projects_right_after_it_collects():
    """「让 problem_scan 完成后立马推飞书」。

    钉两件事,第二件是这次唯一真会静默出错的地方:

    ① 投影代码在 `services.blacklist_sheet`,`problem_scan` 调 service 而不是
       import `blacklist_push` 工作流(铁律 1);与 order_center 那次拆分同款。
    ② ⚠ **投影必须在 `with db.pg_conn()` 块之外**。投影是另开一条连接查全表,
       放在扫描那个事务里面调的话它**看不到刚写的那几行** —— 表现是
       "黑名单收集 +5,可表格一行没多",而且不报任何错。
    """
    import inspect
    from workflows import problem_scan as ps
    src = inspect.getsource(ps.run)
    assert "_push_sheets()" in src
    # 缩进说明位置:调用点必须与 `with db.pg_conn()` 同级或更外(4 空格),
    # 落到 8 空格就是在 with 块里面
    line = next(ln for ln in src.splitlines() if "_push_sheets()" in ln)
    assert len(line) - len(line.lstrip()) <= 8, "投影被塞进事务里了"
    assert src.index("with db.pg_conn()") < src.index("_push_sheets()")
    # 收集本身仍然只写 PG —— 一个飞书调用都不许有
    collect = inspect.getsource(ps._collect_blacklists)
    assert "feishu" not in collect and "push_after" not in collect
    # 铁律 1:不许 import 工作流
    whole = inspect.getsource(ps)
    assert "import blacklist_push" not in whole


def test_scan_side_projection_never_raises():
    """投影挂了不该把一轮扫描记成 failed:黑名单已落 PG、**闸门已经生效**
    (上架与审核读 PG,从不读飞书表)。但必须说出来 —— 否则表现成
    "跑成功了可表格没动"(所有者 2026-08-16 在日报上实遇过这种)。"""
    import services.blacklist_sheet as bs

    def _boom(**kw):
        raise RuntimeError("飞书 5xx")
    orig, bs.push_all = bs.push_all, _boom
    try:
        out = bs.push_after()
    finally:
        bs.push_all = orig
    assert out.startswith("⚠") and "飞书 5xx" in out
    assert "闸门已经生效" in out          # 别让人以为拦截也没生效
    assert "cli.py blacklist_push" in out  # 告诉人怎么补推


def test_shrink_is_isolated_per_table(monkeypatch):
    """一张表被骤缩护栏停手,不该拖垮另一张(它们是两份独立投影)。"""
    import services.blacklist_sheet as bs
    seen = []

    def _rw(sheet, all_sql, mark_sql, *, allow_shrink=False):
        seen.append(sheet.name)
        if all_sql is bs.ASIN_ALL:
            raise bs.Shrink("「ASIN 黑名单」**已停手,一格未写**")
        return 7
    monkeypatch.setattr(bs, "rewrite_sheet", _rw)
    lines = bs.push_all()
    assert len(seen) == 2                      # 第一张停手,第二张照写
    assert lines[0].startswith("⛔")
    assert "整表重写 7 行" in lines[1]


def test_reason_存全文_不许再截200():
    """⚠ 所有者 2026-09-04 问「为什么要截断字符样本?」—— 考古结论是**没有理由**:
    全仓找不到任何依据,最合理的解释是这一列当初只用来「人看一眼知道为什么被
    拉黑」,对显示来说 200 字符够,那时它不是判据。

    换轨后它成了 `error_reclass` 四级优先里**最大的一档**证据源(36,868 条),
    而沃尔玛的判据串恰好在**句尾**(「…To republish this item please make sure
    you have the appropriate product type selected.」)—— 200 字符精确砍掉它,
    于是那批品被判成永久拉黑,真相是 PT_WRONG。**40,827 是低估的。**

    不截的依据:列是 `text` 无限制;飞书那侧自己有 20,000/40,000 两道闸
    (官方上限 50,000),跟 200 差两个数量级。**截断属于展示层,不属于存储层。**
    """
    import inspect
    from services import blacklist as bl, cleanup_history
    # 只查**写入侧**的函数体(模块头注里会引用 `[:200]` 讲这段考古,那是文档)
    for fn in (bl.record_asins, bl._judge_events, cleanup_history.timeline):
        src = inspect.getsource(fn)
        assert "[:200]" not in src, fn.__name__
        assert "left(reason, 200)" not in src, fn.__name__
    # 头注要留着考古结论,不然下一个人又会"顺手截一下"
    assert "截断属于展示层,不属于存储层" in inspect.getsource(bl)
    # 展示侧那道闸还在(它才是该管长度的地方)
    from api import feishu
    assert feishu._SHEET_CELL_MAX_CHARS >= 20000
