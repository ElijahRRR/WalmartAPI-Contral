"""审核 ⇄ 上架表闭环回归(所有者定稿 2026-08-16)。

定稿两句话:
  ① 审核「直接读取上架表的 A、E 列(为空就审核),然后回填 C、D、E、F、G」;
  ② 上架「以数据库的数据为准 —— 要上架就肯定要过审核,读取速度也更快」。

这两句话合起来是一条**单向环**:表 A 列进审核 → 结论落 PG → 投影回表 C~G(给人看)
→ 上架读 **PG**(不读投影)。本文件钉住的是这条环上四个"错了也不报错"的接缝:

  · E 列写的值必须是 `list_new` 之外的人能看懂的中文态,而 **PG 里是英文态** ——
    两套词表错配的表现是"审过了但一行也上不去",没有任何报错;
  · 库里没结论的行 **E 列必须留空**(不是写 pending)—— 写了人就以为审过了,
    而且它下一轮不会被重领;
  · 上架的审核闸读 PG:表 E 列被人手改成 pass **不该**让它上架;
  · 类目同样以库为准:表 D 列被手改成另一个 PT,不该按手改的那个上架
    (那等于绕过审核换类目)。
"""

import datetime as dt

import pytest

from registry import resources
from services import listing_sheet
from workflows import list_new as ln
from workflows import product_audit as pa
from tests.test_list_new import _sheet_row


# ── ① 领任务:A 有值且 E 为空 ─────────────────────────────────────────────

def test_audit_targets_takes_blank_e_only(monkeypatch):
    rows = [
        _sheet_row(2, audit_result=""),            # 待审
        _sheet_row(3, audit_result="   "),         # 空白也算空(表格里常见)
        _sheet_row(4, audit_result="pass"),        # 审过了
        _sheet_row(5, audit_result="reject"),      # 审过了(判拒也是结论)
        _sheet_row(6, asin="", audit_result=""),   # 没 ASIN:不是待审行
    ]
    monkeypatch.setattr(listing_sheet, "read_rows", lambda: rows)
    got = listing_sheet.audit_targets()
    assert [r["rownum"] for r in got] == [2, 3]
    assert got[0]["asin"] and got[0]["store"] == "T1"


def test_audit_result_cn_matches_what_list_new_reads():
    """E 列词表与 PG 词表是两套 —— 这条钉住它们的对应关系。

    `AUDIT_RESULT_CN` 把 PG 的 audit_status 翻成表里的写法;
    `list_new.AUDIT_OK` 判的是 **PG 的写法**。两边一起改才对。
    """
    assert listing_sheet.AUDIT_RESULT_CN[ln.AUDIT_OK] == "pass"
    assert set(listing_sheet.AUDIT_RESULT_CN) == {"approved", "rejected",
                                                  "pending"}


# ── ② 回填:C/D/E/F/G,库里没结论的留空 ───────────────────────────────────

class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.sql = sql

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self._rows)


def test_project_to_sheet_writes_cg_and_leaves_absent_blank(monkeypatch):
    at = dt.datetime(2026, 8, 16, 9, 30)
    monkeypatch.setattr(pa.db, "pg_conn", lambda: _Conn([
        ("B0OK", "沃标题", "Cups", "approved", "", at),
        ("B0NO", "沃标题2", "Mugs", "rejected", "品牌命中黑名单", at),
        # B0NONE 在库里查不到 → 不出现在结果集
    ]))
    writes = []
    monkeypatch.setattr(listing_sheet, "write_audit_cols",
                        lambda ups, execute: (writes.extend(ups), len(ups))[1])
    out = pa._project_to_sheet([
        {"rownum": 2, "asin": "B0OK"},
        {"rownum": 3, "asin": "B0NO"},
        {"rownum": 4, "asin": "B0NONE"},
        {"rownum": 5, "asin": "B0OK"},        # 同 ASIN 多行(不同店铺)都要写
    ], True)
    by_row = dict(writes)
    assert set(by_row) == {2, 3, 5}                 # 4 行留空,一格没动
    assert by_row[2] == ["沃标题", "Cups", "pass", "", "2026-08-16"]
    assert by_row[3][2] == "reject" and by_row[3][3] == "品牌命中黑名单"
    assert by_row[5] == by_row[2]
    assert "回填 3 行" in out
    assert "1 行库里没有结论" in out and "E 列留空" in out


def test_project_failure_only_warns(monkeypatch):
    """结论已经在 PG 了,飞书写挂不该把整轮审核判失败(与订单中心同款纪律)。"""
    def _boom():
        raise RuntimeError("飞书 5xx")
    monkeypatch.setattr(pa.db, "pg_conn", _boom)
    out = pa._project_to_sheet([{"rownum": 2, "asin": "B0OK"}], True)
    assert "回填失败" in out and "飞书 5xx" in out
    assert "from_sheet=1" in out            # 告诉人怎么补写


def test_write_audit_cols_stays_inside_cg(monkeypatch):
    """⚠ 只准动 C~G。越界写会覆盖 list_new 的 H~N 与反哺器的 O~Q。"""
    sent = []
    monkeypatch.setattr(listing_sheet.feishu, "sheet_write_ranges",
                        lambda s, ups: (sent.extend(ups), len(ups))[1])
    n = listing_sheet.write_audit_cols([(7, ["T", "Cups", "pass", "", "d"])])
    assert n == 1 and sent[0][0] == "C7:G7"
    assert sent[0][1] == [["T", "Cups", "pass", "", "d"]]
    # dry-run 一格不写
    sent.clear()
    assert listing_sheet.write_audit_cols([(7, ["T"] * 5)], execute=False) == 0
    assert sent == []


def test_column_shuffle_is_caught_before_anything_runs(monkeypatch):
    """⚠ 表头再被调一次而 registry 没跟着改 —— 必须在动库之前炸。

    不炸的话:审核照样跑完、照样回填,只是全都判"库里没有这个 ASIN"。
    表现是"这批产品怎么全都没采集",查半天查不到根因(2026-08-16 所有者
    刚把 A/B 对调过一次,这类事会再发生)。
    """
    monkeypatch.setattr(listing_sheet, "audit_targets", lambda: [
        {"rownum": 2, "asin": "梦琪专营店", "store": "B0REAL0001"}])
    with pytest.raises(ValueError, match="不像 ASIN"):
        pa._claim_from_sheet(500)


def test_already_audited_asins_are_not_re_judged(monkeypatch):
    """⚠ E 列为空 ≠ 库里没结论 —— 已有结论的**直接回填,不重审**。

    所有者纠正 2026-08-16:「按我们的运行逻辑,不是应该直接从库里读取结果吗」。
    当成强审的后果:每轮把已审过的几万个 ASIN 重判一遍,钱白花、慢得离谱,
    而且哪里都看不出不对(结论还是那个结论)。
    """
    where, extra = pa._pick_where({"asins": "B0A,B0B", "from_sheet": "1"})
    assert "p.asin = ANY(%(asins)s)" in where
    assert pa._DEFAULT_CANDIDATE in where          # 叠了默认候选谓词
    assert extra["asins"] == ["B0A", "B0B"]
    # 而人手点名的 asins=(不带 from_sheet)仍是强审:点名就是要重判
    forced, _ = pa._pick_where({"asins": "B0A"})
    assert forced == "p.asin = ANY(%(asins)s)"
    assert pa._DEFAULT_CANDIDATE not in forced


def test_backlog_breakdown_reaches_the_summary_not_just_the_log(monkeypatch):
    """三个数就是"为什么这轮还在审"的全部答案,必须进摘要(飞书通知的正文)。

    生产实证 2026-08-16:28498 个 ASIN / limit=500,当时摘要只说"待审 28498",
    看不出其中多少是已审过的、多少压根不在库里。
    """
    monkeypatch.setattr(listing_sheet, "audit_targets", lambda: [
        {"rownum": i, "asin": f"B0ASIN{i:04d}", "store": "T1"}
        for i in range(2, 12)])                       # 10 个 ASIN
    monkeypatch.setattr(pa.db, "pg_conn", lambda: _Conn([
        ("approved", 3), ("rejected", 1), ("pending", 2), ("未审", 2)]))
    rows, asins, head = pa._claim_from_sheet(3)
    # ⚠ 不截断:整批交给候选谓词,LIMIT 只限制**真要判的**那部分
    assert len(rows) == 10 and len(asins) == 10
    assert "E 列为空 10 个 ASIN" in head[0]
    assert "已有结论 4" in head[1] and "不重审" in head[1]
    assert "待审 4" in head[1]
    assert "不在库 2" in head[1]                      # 10 - (3+1+2+2)
    assert "还剩 1 个" in head[2] and "limit=3" in head[2]   # 待审 4 > limit 3
    # 待审没超 limit 时不该无中生有地报警
    assert len(pa._claim_from_sheet(500)[2]) == 2


def test_from_sheet_reuses_the_asins_path(monkeypatch):
    """from_sheet 只是换了个领任务的地方 —— 判定引擎仍只有一条实现。"""
    monkeypatch.setattr(listing_sheet, "audit_targets", lambda: [
        {"rownum": 2, "asin": "B0B", "store": "T1"},
        {"rownum": 3, "asin": "B0A", "store": "T1"},
        {"rownum": 4, "asin": "B0A", "store": "T2"},     # 同 ASIN 去重
    ])
    where, extra = pa._pick_where({"asins": "B0A,B0B"})
    assert extra["asins"] == ["B0A", "B0B"]
    # 一行待审都没有时直说,而且说明重审入口(清空 E 列)
    monkeypatch.setattr(listing_sheet, "audit_targets", lambda: [])
    out = pa.run({"from_sheet": "1"})
    assert "没有待审行" in out and "清空" in out


# ── ③④ 上架闸:读 PG,不读表 ─────────────────────────────────────────────

def _stub_gates(monkeypatch, rows):
    monkeypatch.setattr(ln.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(ln, "_load_gate_state", lambda: (
        set(), {}, set(), {}, set(),
        {"banned_pts": set(), "brands": set()}, {}, {}))
    monkeypatch.setattr(ln, "_load_quota", lambda: {})
    monkeypatch.setattr(ln, "_load_multipliers", lambda: {})
    monkeypatch.setattr(ln.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    monkeypatch.setattr(ln.pt_spec, "load_pt", lambda pt: {"properties": {}})


def test_sheet_e_column_cannot_smuggle_a_row_past_the_audit_gate(monkeypatch):
    """表 E 列写着 pass、库里没结论 ⇒ **不上架**,而且必须在摘要里点名。

    这是改成读库之后最危险的一种沉默:表里明明几百行,一行也不上,
    却没有任何一处说"因为没审核"。
    """
    rows = [_sheet_row(2, asin="B0AUDITED", audit_result="pass"),
            _sheet_row(3, asin="B0FAKEOK", audit_result="pass"),
            _sheet_row(4, asin="B0REJECT", audit_result="pass")]
    _stub_gates(monkeypatch, rows)
    monkeypatch.setattr(ln, "load_verdicts", lambda a: {
        "B0AUDITED": ("approved", "Cups"),
        "B0REJECT": ("rejected", "Cups"),
        # B0FAKEOK 库里没有 —— 表里写着 pass 也不算
    })
    fetched = {}
    monkeypatch.setattr(ln.amz_source, "fetch_products",
                        lambda asins: (fetched.setdefault("a", asins), {})[1])
    out = ln.run({"execute": False})
    assert "待上架 1" in out
    assert fetched["a"] == ["B0AUDITED"]
    assert "未审核 1 行" in out and "审核判拒 1 行" in out
    assert "product_audit -p from_sheet=1" in out       # 说清下一步怎么做


def test_product_type_also_comes_from_pg(monkeypatch):
    """D 列被手改成别的 PT 也没用:按库里那个走(否则等于绕过审核换类目)。"""
    rows = [_sheet_row(2, asin="B0PT", product_type="手改的PT")]
    _stub_gates(monkeypatch, rows)
    monkeypatch.setattr(ln, "load_verdicts",
                        lambda a: {"B0PT": ("approved", "库里的PT")})
    seen = []
    monkeypatch.setattr(ln.pt_spec, "load_pt",
                        lambda pt: (seen.append(pt), {"properties": {}})[1])
    monkeypatch.setattr(ln.amz_source, "fetch_products", lambda a: {})
    ln.run({"execute": False})
    assert "库里的PT" in seen and "手改的PT" not in seen


def test_pt_falls_back_to_sheet_when_pg_has_none():
    """库里没 PT(老数据)才退回表里的值 —— 不能因此把 PT 抹成空。"""
    r = _sheet_row(2, product_type="表里的PT")
    assert ln._with_pt(r, {r["asin"]: ("approved", None)})["product_type"] \
        == "表里的PT"
    assert ln._with_pt(r, {})["product_type"] == "表里的PT"
    assert ln._with_pt(r, {r["asin"]: ("approved", "库PT")})["product_type"] \
        == "库PT"


def test_columns_contract():
    """列序的唯一出处是这条元组 —— 错一位,整套回填静默写到隔壁列去。

    A/B 已经被所有者对调过一次(2026-08-16:原 A=ASIN B=店铺 → 现 A=店铺 B=ASIN)。
    读取全程按字段名,所以那次对调只动了这条元组;**但写入用的是字母 range**,
    所以 C~G 这一段一旦位移,`write_audit_cols` 会把审核结论写进别人的列里
    —— 而且不报错。
    """
    cols = resources.LISTING_SHEET.columns
    assert cols[0] == "store" and cols[1] == "asin"      # A/B 对调后的现状
    assert cols[2:7] == ("list_title", "product_type", "audit_result",
                         "audit_reason", "audit_date")   # C~G 审核域
    assert len(cols) == 21                               # A~U
