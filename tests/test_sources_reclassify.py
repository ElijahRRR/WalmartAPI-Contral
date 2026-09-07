"""sources_reclassify 回归:导出清单的口径、读回的 fail loud、两道保险与跳过点名。

这条工作流做的事只有一件,而它不可逆的部分全在同一个地方:**把商品交还自动链**
—— 改完的行第一次被 amz 快照驱动的改价/清库存/删除管到。所以这里钉三类东西:
① guess 一档永不自动应用(不自动应用要落在**文件形状**上,不是口头纪律);
② `-p apply=1` 之前一行都不许写;③ 每一条没改的行都要点名(默默丢行是这类
导入最难查的故障:摘要里只是少了几行,而人无从知道少的是哪几行)。
"""

import contextlib

import pytest

from services import listing_sources as ls
from workflows import sources_reclassify as wf

_WRAPPED = "CMSQ-B0CLCX3Q1Z-169.99"      # 所有者给样:应为 B0CLCX3Q1Z(规则提得出)
_SUFFIXED = "B0822D9QQKS59"              # 所有者给样:应为 B0822D9QQK(**只能猜**)
_NUMERIC = "102460018738"                # 纯数字 item id:提不出
_OTHER = "MANUAL-001"                    # 人工号:提不出


class _Cur:
    """三条 SQL 的假游标:待归类清单 / 现状反查 / 归类 UPDATE。"""

    def __init__(self, conn):
        self.conn = conn
        self.rows: list = []
        self.rowcount = 0

    def execute(self, sql, args=None):
        if "LEFT JOIN catalog.walmart_items" in sql:          # pending_reclassify
            unknown = args[0]
            self.rows = [
                (s, k, t, key, self.conn.items.get((s, k), (None, None, None))[0],
                 self.conn.items.get((s, k), (None, None, None))[1],
                 self.conn.items.get((s, k), (None, None, None))[2],
                 (s, k) in self.conn.items)
                for (s, k), (t, key) in sorted(self.conn.reg.items())
                if t == unknown or key is None]
        elif sql.strip().upper().startswith("UPDATE"):         # reclassify
            new_type, new_key, workflow, store, sku, overwrite, unknown = args
            t, key = self.conn.reg.get((store, sku), (None, None))
            if t is not None and (overwrite or t == unknown or not key):
                self.conn.reg[(store, sku)] = (new_type, new_key)
                self.conn.wrote.append((store, sku, new_type, new_key, workflow))
                self.rowcount = 1
            else:
                self.rowcount = 0
        else:                                                  # _CURRENT_SQL
            stores, skus = args
            want = set(zip(stores, skus))
            self.rows = [(s, k, t, key) for (s, k), (t, key)
                         in sorted(self.conn.reg.items()) if (s, k) in want]

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, conn):
    """假连接要能**把异常抛出去**(fail loud 那几条断言全靠它),所以是真的
    生成器,不是 iter([conn]) —— 后者 __exit__ 时没有 throw,缺列那条会变成
    一个看不懂的 AttributeError。"""
    @contextlib.contextmanager
    def _fake(**kw):
        yield conn
    monkeypatch.setattr(wf.db, "pg_conn", _fake)


class _Conn:
    def __init__(self, reg, items=None):
        self.reg = dict(reg)          # {(店, sku): (source_type, source_key)}
        self.items = items or {}      # {(店, sku): (商品名, published_status, missing_since)}
        self.wrote: list = []

    def cursor(self):
        return _Cur(self)


@pytest.fixture
def conn(monkeypatch):
    c = _Conn(
        {("T1", _WRAPPED): ("unknown", None),
         ("T1", _SUFFIXED): ("unknown", None),
         ("T2", _NUMERIC): ("unknown", None),
         ("T2", _OTHER): ("unknown", None),
         ("T3", "B0AAAAAAA1"): ("amz", "B0AAAAAAA1")},      # 已归类,不在清单里
        {("T1", _WRAPPED): ("落地灯", "PUBLISHED", None),
         ("T2", _NUMERIC): ("台灯", "UNPUBLISHED", "2026-08-01")})
    _wire(monkeypatch, c)
    return c


@pytest.fixture(autouse=True)
def reports(monkeypatch, tmp_path):
    """落盘走 services/report_csv,报告目录在积木那侧(与 alloc_* 同款写法)。"""
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    return tmp_path


# ── 导出侧 ────────────────────────────────────────────────────────────────────

def test_export_lists_the_blind_spot_and_buckets_it(conn, reports):
    out = wf.run({})
    assert "待归类(自动链看不见)4 行" in out
    assert "规则提得出 1" in out and "只能猜 1" in out and "提不出 2" in out
    # 「标准 ASIN + 尾巴」在形态桶里就是「其他」——形态桶答的是"长什么样",
    # 提议依据答的是"能不能提出来",两个口径不重合(所以摘要两行都要有)
    assert "三段式×1" in out and "纯数字(item id)×1" in out and "其他×2" in out
    assert conn.wrote == []                       # 导出一行都不写库


def test_export_prefills_only_what_the_rules_can_extract(conn, reports):
    """⚠ **不自动应用要落在文件形状上**:guess 那一档「确认来源码」留空,
    人不动它读回时自然跳过 —— 而不是靠一句"请注意不要直接采用"。"""
    wf.run({})
    body = (reports / wf._DEFAULT_FILE).read_text(encoding="utf-8-sig").splitlines()
    assert body[0].split(",") == wf._HEADER
    rows = {ln.split(",")[1]: ln.split(",") for ln in body[1:]}
    # 第 8 列 =「确认来源类型」:与来源码**同步预填**,填了码却空着类型就会
    # 被按默认算成 amz —— 那正是 2026-09-03 要消灭的静默默认
    assert rows[_WRAPPED][4:8] == ["B0CLCX3Q1Z", "wrapped", "B0CLCX3Q1Z", "amz"]
    assert rows[_SUFFIXED][4:8] == ["B0822D9QQK", "guess", "", ""]  # 提议给了,确认留空
    assert rows[_NUMERIC][4:8] == ["", "", "", ""]
    assert rows[_WRAPPED][9] == "PUBLISHED"
    assert rows[_NUMERIC][9] == "已缺席"
    assert rows[_OTHER][9] == "在架表无此行"       # 登记簿有行、在架表没有是正常的


def test_export_honours_an_explicit_out_path(conn, tmp_path):
    target = tmp_path / "子目录" / "我的清单.csv"
    out = wf.run({"out": str(target)})
    assert target.is_file() and str(target) in out


def test_export_says_nothing_to_do_when_the_blind_spot_is_empty(monkeypatch):
    c = _Conn({("T3", "B0AAAAAAA1"): ("amz", "B0AAAAAAA1")})
    _wire(monkeypatch, c)
    assert "0 行" in wf.run({}) and "无事可做" in wf.run({})


# ── 读回侧 ────────────────────────────────────────────────────────────────────

def _csv(tmp_path, header, *rows) -> str:
    p = tmp_path / "in.csv"
    p.write_text("\n".join([",".join(header)] + [",".join(r) for r in rows]),
                 encoding="utf-8-sig")
    return str(p)


def test_missing_column_fails_loud(conn, tmp_path):
    """缺列必须炸:少一列的后果是整批行被静默当成"没填",摘要显示改了 0 行,
    而人以为清单没生效、再导一遍还是 0 行。"""
    path = _csv(tmp_path, ["店铺", "SKU"], ["T1", _WRAPPED])
    with pytest.raises(ValueError, match="缺列"):
        wf.run({"file": path})


def test_columns_are_read_by_name_not_by_position(conn, tmp_path):
    """所有者会用 Excel 拖列、加批注列 —— 列位一变就写错行,而且不报错。"""
    path = _csv(tmp_path, ["备注", "确认来源码", "SKU", "店铺"],
                ["随便写", "B0CLCX3Q1Z", _WRAPPED, "T1"])
    out = wf.run({"file": path, "apply": "1"})
    assert conn.wrote == [("T1", _WRAPPED, "amz", "B0CLCX3Q1Z", "sources_reclassify")]
    assert "已归类 1 行" in out


def test_dry_run_counts_the_visibility_flip_without_writing(conn, tmp_path):
    """预览必须说清**多少行从「自动链看不见」变成「自动链管得到」**,
    并把下一步的 maintenance_scan 纪律照抄给人(与 sources_backfill 同口径)。"""
    path = _csv(tmp_path, wf._NEED_COLS, ["T1", _WRAPPED, "B0CLCX3Q1Z"],
                ["T2", _OTHER, "B0OTHERKEY"])
    out = wf.run({"file": path})
    assert "本次将改 2 行" in out and "2 行归成 amz" in out
    assert "自动链看不见" in out and "自动链管得到" in out
    assert "maintenance_scan --dry-run" in out
    assert conn.wrote == []


def test_apply_is_still_blocked_by_dry_run(conn, tmp_path):
    """`-p apply=1 --dry-run` 组合:cli 对 DANGEROUS=False 恒给 execute=True,
    dry_run 是单独透传的那一路 —— 漏认它本工作流的 --dry-run 会照写不误。"""
    path = _csv(tmp_path, wf._NEED_COLS, ["T1", _WRAPPED, "B0CLCX3Q1Z"])
    out = wf.run({"file": path, "apply": "1", "dry_run": True, "execute": True})
    assert "未写库" in out and conn.wrote == []


def test_blank_confirm_column_is_simply_not_imported(conn, tmp_path):
    """留空 = 人没认这一行(guess 那批的默认状态),既不改也不报错。"""
    path = _csv(tmp_path, wf._NEED_COLS, ["T1", _SUFFIXED, ""],
                ["T1", _WRAPPED, "B0CLCX3Q1Z"])
    out = wf.run({"file": path, "apply": "1"})
    assert "读入 2 行,填了「确认来源码」1 行" in out
    assert [w[1] for w in conn.wrote] == [_WRAPPED]


# ── 拒收与跳过(每一条都要点名)────────────────────────────────────────────────

def test_a_non_asin_key_is_rejected_and_named(conn, tmp_path):
    """灌垃圾键比不归类危险得多:键错了下游一声不吭(采不到 → 判成源头没了
    → 清库存/删除),所以形态闸在写入之前,而且拒收要点名。"""
    path = _csv(tmp_path, wf._NEED_COLS, ["T2", _NUMERIC, "1024600187"])
    out = wf.run({"file": path, "apply": "1"})
    assert "已归类 0 行" in out
    assert "不合 amz 的形态" in out and "标准 ASIN" in out and _NUMERIC in out
    assert conn.wrote == []


# ── 来源类型(所有者 2026-09-03:"有些并不是 amz 产品,会影响后面的事情吗")──

_TYPED = (wf.COL_STORE, wf.COL_SKU, wf.COL_CONFIRM, wf.COL_CONFIRM_TYPE)


def test_non_amz_rows_are_written_with_their_own_type(conn, tmp_path):
    """归类不等于归成 amz。写死 amz 的后果是:一个 1688/自建的品只要填了个
    形态合法的 ASIN,价格/标题/库存就跟着那个亚马逊页面走,断货窗口一到还会
    被建议**永久删除** —— 全程不报错。类型必须逐行透传到 UPDATE。"""
    path = _csv(tmp_path, _TYPED,
                ["T1", _WRAPPED, "B0CLCX3Q1Z", "amz"],
                ["T2", _OTHER, "MWCS26052501", "1688"])
    out = wf.run({"file": path, "apply": "1"})
    assert [(w[1], w[2], w[3]) for w in conn.wrote] == [
        (_WRAPPED, "amz", "B0CLCX3Q1Z"), (_OTHER, "1688", "MWCS26052501")]
    # 摘要必须把"其中几行归成 amz"单独说清 —— 只有那几行的破坏面被打开
    assert "已归类 2 行" in out and "1 行归成 amz" in out


def test_key_gate_is_per_type_not_one_ruler_for_all():
    """四种出身的"键"不是同一种东西:amz 是 ASIN、match 是 GTIN、
    1688/self 是货源侧货号。用 amz 那把尺子量 1688 = 把它整批冤枉拒收。"""
    ok = ls.source_key_ok
    assert ok("amz", "B0822D9QQK") and not ok("amz", "MWCS26052501")
    assert ok("1688", "MWCS26052501") and ok("self", "M0000004")
    assert ok("match", "0193575043586") and not ok("match", "B0822D9QQK")
    assert not ok("1688", "") and not ok("self", "有 空格")


def test_unknown_type_is_refused_not_written(conn, tmp_path):
    """归类的定义就是"离开 unknown"。允许写回 unknown = 给自己开一条把已归类
    行打回盲区的路,而且是从一个人工清单上打回去的。"""
    path = _csv(tmp_path, _TYPED, ["T1", _WRAPPED, "B0CLCX3Q1Z", "unknown"])
    out = wf.run({"file": path, "apply": "1"})
    assert "来源类型不认识" in out and conn.wrote == []


def test_missing_type_column_defaults_to_amz_but_says_so(conn, tmp_path):
    """旧版清单(没有类型列)仍然能用 —— 硬性要求会让所有者已经填好的一份
    文件整个作废。但默认成 amz 这件事**必须在摘要里喊出来**,不许静默。"""
    path = _csv(tmp_path, wf._NEED_COLS, ["T1", _WRAPPED, "B0CLCX3Q1Z"])
    out = wf.run({"file": path, "apply": "1"})
    assert "全部按 amz 算" in out
    assert [(w[1], w[2]) for w in conn.wrote] == [(_WRAPPED, "amz")]


def test_type_breakdown_is_reported_before_apply(conn, tmp_path):
    """按下 apply 之前,人要看得见"这一批里有多少行会被交给破坏面"。"""
    path = _csv(tmp_path, _TYPED,
                ["T1", _WRAPPED, "B0CLCX3Q1Z", "amz"],
                ["T2", _OTHER, "MWCS26052501", "1688"])
    out = wf.run({"file": path})
    assert "amz 1 行" in out and "1688 1 行" in out
    assert "1 行归成 amz" in out and "1 行只是拿到身份" in out
    assert conn.wrote == []


def test_an_already_classified_row_needs_overwrite(conn, tmp_path):
    path = _csv(tmp_path, wf._NEED_COLS, ["T3", "B0AAAAAAA1", "B0BBBBBBB2"])
    out = wf.run({"file": path, "apply": "1"})
    assert "已归类,未传 overwrite 不覆盖" in out and conn.wrote == []
    assert conn.reg[("T3", "B0AAAAAAA1")] == ("amz", "B0AAAAAAA1")

    out2 = wf.run({"file": path, "apply": "1", "overwrite": "1"})
    assert "已归类 1 行" in out2 and "overwrite=1" in out2
    assert conn.reg[("T3", "B0AAAAAAA1")] == ("amz", "B0BBBBBBB2")


def test_rerunning_the_same_sheet_is_idempotent_and_says_so(conn, tmp_path):
    path = _csv(tmp_path, wf._NEED_COLS, ["T1", _WRAPPED, "B0CLCX3Q1Z"])
    wf.run({"file": path, "apply": "1"})
    out = wf.run({"file": path, "apply": "1"})
    assert "已归类 0 行" in out and "已经是这个值(幂等重跑)" in out


def test_a_row_absent_from_the_registry_is_named_not_inserted(conn, tmp_path):
    """归类只 UPDATE,永不 INSERT(登记只有 register 与 mint 两个出口)。"""
    path = _csv(tmp_path, wf._NEED_COLS, ["T9", "B0NOSUCHR1", "B0CLCX3Q1Z"])
    out = wf.run({"file": path, "apply": "1"})
    assert "登记簿里没有这一行" in out and conn.wrote == []


def test_duplicate_rows_in_the_sheet_are_named(conn, tmp_path):
    path = _csv(tmp_path, wf._NEED_COLS, ["T1", _WRAPPED, "B0CLCX3Q1Z"],
                ["T1", _WRAPPED, "B0DIFFEREN1"])
    out = wf.run({"file": path, "apply": "1"})
    assert "清单内重复,只认第一条" in out
    assert conn.reg[("T1", _WRAPPED)] == ("amz", "B0CLCX3Q1Z")


# ── 积木层:计划与写入同一份判据 ──────────────────────────────────────────────

def test_plan_and_write_share_one_verdict(conn, tmp_path):
    """预览报的行数与真跑改出来的行数必须同口径 —— reclassify 内部就是先跑
    plan_reclassify(判据一处出生,不许工作流另算一遍)。"""
    rows = [{"store": "T1", "sku": _WRAPPED, "source_key": "B0CLCX3Q1Z"},
            {"store": "T3", "sku": "B0AAAAAAA1", "source_key": "B0BBBBBBB2"}]
    todo, skipped = ls.plan_reclassify(conn, rows)
    changed, skipped2 = ls.reclassify(conn, rows)
    assert len(todo) == changed == 1
    assert set(skipped) == set(skipped2)
