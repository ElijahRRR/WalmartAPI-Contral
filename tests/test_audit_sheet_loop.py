"""审核 ⇄ 上架表闭环回归(所有者定稿 2026-08-16)。

定稿两句话:
  ① 审核「直接读取上架表的 A、E 列(为空就审核),然后回填 C、D、E、F、G」;
  ② 上架「以数据库的数据为准 —— 要上架就肯定要过审核,读取速度也更快」。

这两句话合起来是一条**单向环**:表 A 列进审核 → 结论落 PG → 投影回表 C~G(给人看)
→ 上架读 **PG**(不读投影)。本文件钉住的是这条环上"错了也不报错"的接缝:

  · E 列写的值必须是 `list_new` 之外的人能看懂的中文态,而 **PG 里是英文态** ——
    两套词表错配的表现是"审过了但一行也上不去",没有任何报错;
  · 库里没结论的行 **E 列必须留空**(不是写 pending)—— 写了人就以为审过了,
    而且它下一轮不会被重领;
  · 上架的审核闸读 PG:表 E 列被人手改成 pass **不该**让它上架;
  · 类目同样以库为准:表 D 列被手改成另一个 PT,不该按手改的那个上架
    (那等于绕过审核换类目)。

2026-08-17 补两条,都是同一种病(**这行从此没人再管,而且不报错**):
  · **E=pending 也要被重新领取** —— pending 是中间态,写进 E 就退出通道了;
  · **库里没数据的行要推去采集、原因写 F 而不是 E** —— 原先摘要只说
    "先跑 product_ingest",而那是死路(ingest 只摄采回来的东西)。
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


def test_pending_in_e_keeps_getting_reclaimed(monkeypatch):
    """⚠ pending 是**中间态**,写进 E 之后必须还能被领回来(2026-08-17 修)。

    不领的话:L1 解不出类目 / L3 LLM 故障那批,`_project_to_sheet` 把 pending
    写进 E 的那一刻就永久退出了上架表通道 —— 库里的一天退避重判照跑、结论也
    在更新,而表上那一格永远停在 `pending`,谁也不会再看它一眼,全程不报错。
    (与 rerule 首版同一种搁浅:候选谓词把自己刚写的东西当成了"已处理"。)
    """
    rows = [_sheet_row(2, audit_result="pending"),
            _sheet_row(3, audit_result="Pending"),      # 大小写不该决定命运
            _sheet_row(4, audit_result="pass"),
            _sheet_row(5, audit_result="reject")]
    monkeypatch.setattr(listing_sheet, "read_rows", lambda: rows)
    assert [r["rownum"] for r in listing_sheet.audit_targets()] == [2, 3]
    # 而 pass 重新被领 = 已上架的行被反复重审,那是另一头的错
    assert ln.AUDIT_OK == "approved"                    # 上架闸判 PG 英文态
    assert listing_sheet.AUDIT_RESULT_CN["approved"] == "pass"


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
    """按 SQL 分流的假游标:结论查 catalog.products,理由查 audit_runs/hits。"""

    def __init__(self, rows, hits=()):
        self._rows, self._hits, self._out = rows, hits, ()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.sql = sql
        self._out = self._hits if "audit.audit_runs" in sql else self._rows

    def fetchall(self):
        return self._out


class _Conn:
    def __init__(self, rows, hits=()):
        self._rows, self._hits = rows, hits

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self._rows, self._hits)


def test_project_to_sheet_writes_cg_and_leaves_absent_blank(monkeypatch):
    at = dt.datetime(2026, 8, 16, 9, 30)
    monkeypatch.setattr(pa.db, "pg_conn", lambda: _Conn(
        [("B0OK", "沃标题", "Cups", "approved", "", at),
         ("B0NO", "沃标题2", "Mugs", "rejected", "General-Use Products", at)],
        # B0NONE 在库里查不到 → 不出现在结果集
        [("B0NO", None, "phase0_brand_blacklist", {"brand": "Nike"}, -100)]))
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
    # F 列要说人话,而不是只甩一个 37 政策类目名(政策留在方括号里)
    assert by_row[3][2] == "reject"
    assert by_row[3][3] == "品牌黑名单(命中:Nike) [政策:General-Use Products]"
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




# ── ⑤ 缺数据**同轮**补采闭环(所有者定稿 2026-08-17)────────────────────────
#
# 「产品审核不能等下一次,要轮询等采完拿数据审核,下一次运行是第二天,时间很长,
#   并且不审核,后面的上架也做不了」。
#
# 五步:报上一批 → 推 → **等** → **就地摄取** → 复查并写理由。
# 这一段钉的都是"错了也不报错"的:
#   · 整段必须跑在**候选查询之前**(跑在后面 = 采回来的这一轮判不到,退化成跨轮);
#   · 等完必须**就地摄取**(批次 completed ≠ 库里有数据,中间隔着增量导出);
#   · 摄取后必须**重查库**再写理由(拿推送前那份写 = 刚采回来的被误报成未采集);
#   · E 列一个字不许动(动了这行就退出审核通道);
#   · 每一种失败都要出现在摘要里。


class _GapCur:
    """假游标:_SQL_GAP 每次调用返回队列里的下一份"库里现状"。"""

    def __init__(self, queue):
        self._q, self._out = queue, ()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self._out = self._q.pop(0) if self._q else []

    def fetchall(self):
        return self._out


class _GapConn:
    def __init__(self, queue):
        self._q = queue

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _GapCur(self._q)


_ROWS = [{"rownum": 2, "asin": "B0HAVE0001"},
         {"rownum": 3, "asin": "B0GONE0001"},     # 库里压根没有
         {"rownum": 4, "asin": "B0THIN0001"},     # 有行但没标题
         {"rownum": 5, "asin": "B0GONE0001"}]     # 同 ASIN 多店铺:两行都写
_WANT = ["B0GONE0001", "B0HAVE0001", "B0THIN0001"]

# 推送前:只有 B0HAVE 完整,B0THIN 无标题,B0GONE 不在库
_BEFORE = [("B0HAVE0001", False), ("B0THIN0001", True)]
# 采回来之后:两个都补齐了
_AFTER_ALL = [("B0HAVE0001", False), ("B0THIN0001", False),
              ("B0GONE0001", False)]
# 采回来之后:只补上了 B0THIN,B0GONE 采集失败
_AFTER_PART = [("B0HAVE0001", False), ("B0THIN0001", False)]


def _stub(monkeypatch, states, *, pushed=None, notes=None, calls=None,
          open_lines=None, failures=None, settle=("等采集:全部落定", 0)):
    """states = _SQL_GAP 依次返回的库里现状(推送前一份、摄取后一份)。"""
    trace = calls if calls is not None else []
    # ⚠ 队列必须**跨连接共享**:_find_gap 每次开一条新连接,每条各拿一份
    #   完整副本的话第二次复查又读到"推送前"那一份 —— 那正是这段要防的 bug
    shared = [list(x) for x in states]
    monkeypatch.setattr(pa.db, "pg_conn", lambda: _GapConn(shared))
    monkeypatch.setattr(pa.scrape_batches, "check_open",
                        lambda p, h: open_lines or ["无在途采集批次"])
    monkeypatch.setattr(pa.scrape_batches, "record", lambda *a, **k: None)
    monkeypatch.setattr(pa.scraper, "submit_batch", lambda name, asins: (
        trace.append("push"),
        (pushed if pushed is None else pushed.append((name, list(asins)))),
        {"batch_id": "bid1", "inserted": len(asins)})[2])
    monkeypatch.setattr(pa.scrape_batches, "prioritize", lambda n, b: (
        trace.append("prioritize"), True)[1])
    monkeypatch.setattr(pa.scrape_batches, "wait_settled", lambda names, m: (
        trace.append(f"wait{m}"), settle)[1])
    monkeypatch.setattr(pa, "_ingest_now",
                        lambda: (trace.append("ingest"), "就地摄取:入库 2 行")[1])
    monkeypatch.setattr(pa.scrape_batches, "pull_failures",
                        lambda n, b: ("失败明细", failures or {}))
    monkeypatch.setattr(pa.listing_sheet, "write_audit_notes",
                        lambda ups, execute: (
                            (notes if notes is None else notes.extend(ups)),
                            len(ups) if execute else 0)[1])
    return trace


def test_gap_is_pushed_waited_ingested_then_audited_this_round(monkeypatch):
    """闭环全走通:推 → 等 → 摄取 → 补回来的**本轮就审**。

    顺序是这条的全部内容:少了 `wait` 就是只推不等(退化成跨轮);
    少了 `ingest` 就是等了半天照样"库里没有" —— 批次 completed 只说明采集侧
    干完了,数据还在增量流里(services/scrape_batches 头注写着)。
    """
    pushed, notes = [], []
    calls = _stub(monkeypatch, [_BEFORE, _AFTER_ALL],
                  pushed=pushed, notes=notes, calls=[])
    out = "\n".join(pa._close_gap(_WANT, _ROWS, True, 20))
    assert calls == ["push", "prioritize", "wait20", "ingest"]
    assert pushed[0][0].startswith("audit_gap_")
    assert pushed[0][1] == ["B0GONE0001", "B0THIN0001"]   # 有数据的那个不重采
    assert "审不了 2 个 ASIN" in out
    assert "补采回来 2 个" in out and "本轮就审" in out
    assert notes == []                       # 一个都不缺了,F 列不写
    assert "仍缺" not in out


def test_still_missing_gets_the_real_scraper_error_in_the_sheet(monkeypatch):
    """⚠ 理由要**真的是理由**:采集侧的 error_type 才说得出"验证码"和"页面
    解析不出"的区别,而这两者运营该做的事完全不同。统一写"未采集"等于
    把十一种成因压成一句废话。"""
    notes = []
    _stub(monkeypatch, [_BEFORE, _AFTER_PART], pushed=[], notes=notes,
          failures={"B0GONE0001": "captcha"})
    out = "\n".join(pa._close_gap(_WANT, _ROWS, True, 20))
    assert "补采回来 1 个" in out and "仍缺 1 个" in out
    by_row = dict(notes)
    assert set(by_row) == {3, 5}              # 补上的第 4 行不写;同 ASIN 两行都写
    assert "captcha" in by_row[3] and "验证码" in by_row[3]
    assert by_row[5] == by_row[3]
    assert "E 列留空" in out


def test_reasons_are_recomputed_after_ingest_not_before(monkeypatch):
    """⚠ 写理由前必须**重查库**。拿推送前那份 gap 去写的话,刚刚采回来的那些
    会被写成"未采集" —— 而它们其实这一轮就要被判掉,表格于是自相矛盾。"""
    notes = []
    _stub(monkeypatch, [_BEFORE, _AFTER_ALL], pushed=[], notes=notes)
    pa._close_gap(_WANT, _ROWS, True, 20)
    assert notes == []          # 复查后一个都不缺 ⇒ 一行原因都不该写


def test_gap_wait_zero_pushes_without_waiting(monkeypatch):
    """gap_wait=0 = 只推不等(采集侧病了时的退路),而且必须明说这一轮不等。"""
    calls = _stub(monkeypatch, [_BEFORE, _BEFORE], pushed=[], notes=[],
                  calls=[])
    out = "\n".join(pa._close_gap(_WANT, _ROWS, True, 0))
    assert calls == ["push", "prioritize"]   # 没 wait 也没 ingest
    assert "只推不等" in out and "下轮审" in out


def test_wait_timeout_is_not_a_failure_but_is_reported(monkeypatch):
    """超时不是失败:已采到的照常摄进来。但**必须说出来**,否则表现是
    "每天都有一批莫名其妙审不了"。"""
    calls = _stub(monkeypatch, [_BEFORE, _AFTER_PART], pushed=[], notes=[],
                  calls=[], settle=("等采集:0/1 批落定,**1 批超 20 分钟仍在跑**", 1))
    out = "\n".join(pa._close_gap(_WANT, _ROWS, True, 20))
    assert calls == ["push", "prioritize", "wait20", "ingest"]    # 超时也要摄取已采到的
    assert "超时仍在跑" in out and "退回下轮审" in out


def test_scraper_outage_still_records_the_reason(monkeypatch):
    """采集侧挂了不该拖垮审核(库里已有数据的照常判),但必须说出来。"""
    notes = []
    _stub(monkeypatch, [_BEFORE, _BEFORE], notes=notes)

    def _boom(name, asins):
        raise RuntimeError("采集服务 502")
    monkeypatch.setattr(pa.scraper, "submit_batch", _boom)
    out = "\n".join(pa._close_gap(_WANT, _ROWS, True, 20))
    assert "推送失败" in out and "采集服务 502" in out
    assert "一个批次都没推成" in out
    assert dict(notes)[3]                    # 理由照写


def test_ingest_borrows_product_ingest_lock(monkeypatch):
    """⚠ 借 product_ingest 的活就得借它的锁:两个进程同推增量游标,
    后写的盖掉先写的,中间那段记录永远不会再被拉一次 —— 两侧都不报错。"""
    import inspect
    src = inspect.getsource(pa._ingest_now)
    assert "runlock.hold(product_ingest.CURSOR_NAME)" in src
    assert "if not got" in src               # 拿不到锁 = 跳过,不是失败
    held = []
    monkeypatch.setattr(pa.runlock, "hold",
                        lambda n: (held.append(n), _NullCtx(False))[1])
    assert "product_ingest 正在跑" in pa._ingest_now()
    assert held == ["product_ingest"]


class _NullCtx:
    def __init__(self, got):
        self._got = got

    def __enter__(self):
        return self._got

    def __exit__(self, *a):
        return False


def test_dry_run_pushes_nothing_and_writes_nothing(monkeypatch):
    pushed, notes = [], []
    calls = _stub(monkeypatch, [_BEFORE], pushed=pushed, notes=notes, calls=[])
    out = "\n".join(pa._close_gap(_WANT, _ROWS, False, 20))
    assert calls == [] and pushed == []
    assert "真跑时会推采集批次" in out and "dry-run 一格未写" in out
    assert "这一轮就审掉" in out             # 说清真跑会做到哪一步
    assert notes                             # 算出来了,只是没写


def test_ledger_is_settled_at_the_end_not_reported_a_day_late(monkeypatch):
    """⚠ 台账落定必须在**最后**(所有者 2026-08-17:「我都已经是当时轮询了,
    不应该还存在上一批吧」—— 对,首版把 check_open 放开头是个真 bug)。

    `wait_settled` **故意不碰台账**(台账落定归 check_open)。放开头 ⇒ 每轮都把
    自己这批留在 `pushed` 上,而开头那次看到的正是**昨天自己没关的那笔** ——
    于是天天报一条"上一批 ✅ 采完",看着像有积压,其实只是台账迟了一天关。
    放最后:推的、等的、关的是同一批,当轮闭合。
    """
    _stub(monkeypatch, [_BEFORE, _AFTER_ALL], pushed=[], notes=[],
          open_lines=["  audit_gap_20260817:✅ 采完 2/2(失败 0)"])
    out = "\n".join(pa._close_gap(_WANT, _ROWS, True, 20))
    assert out.index("已推采集") < out.index("补采批次台账")
    assert "上一批" not in out


def test_ledger_is_settled_even_when_nothing_is_missing(monkeypatch):
    """缺口为空 ≠ 没有遗留:上一轮超时 / gap_wait=0 / 中途被打断的那批还挂着。

    只在有缺口时才关台账的话,那笔遗留会一直挂到下一次刚好有缺口 —— 而
    "在途批次挂着没人管"正是 ops.scrape_batches 那张表要防的事。
    """
    calls = _stub(monkeypatch, [[("B0HAVE0001", False)]], pushed=[], notes=[],
                  calls=[],
                  open_lines=["  audit_gap_20260816:⏰ 超时(3/10)"])
    out = "\n".join(pa._close_gap(["B0HAVE0001"], [_ROWS[0]], True, 20))
    assert calls == []                       # 没缺口:不推、不等、不摄取
    assert "补采批次台账" in out and "超时" in out


def test_dry_run_does_not_touch_the_ledger(monkeypatch):
    """查在途会把批次标 completed/timeout —— 那是写台账,dry-run 不许碰。"""
    seen = []
    _stub(monkeypatch, [_BEFORE], pushed=[], notes=[])
    monkeypatch.setattr(pa.scrape_batches, "check_open",
                        lambda p, h: (seen.append(p), ["x"])[1])
    out = "\n".join(pa._close_gap(_WANT, _ROWS, False, 20))
    assert seen == [] and "补采批次台账" not in out


def test_no_gap_no_noise(monkeypatch):
    """一个都不缺时不推、不等、也不产生空节 —— 空节本身就是噪声。"""
    calls = _stub(monkeypatch, [[("B0HAVE0001", False)]], pushed=[], notes=[],
                  calls=[])
    assert pa._close_gap(["B0HAVE0001"], [_ROWS[0]], True, 20) == []
    assert calls == []


def test_gap_closure_runs_before_the_candidate_query(monkeypatch):
    """⚠ 这条是本段的命门。补采跑在判定**之后**的话,采回来的产品要等下一轮
    (= 第二天)才审、再等一轮才上架 —— 每引进一批新品白等一天,而且不报错。
    (首版就是那样,所有者当场否掉:「后面的上架也做不了」。)
    """
    import inspect
    src = inspect.getsource(pa.run)
    assert "sheet_head += _close_gap(sheet_want, sheet_rows, execute" in src
    # 必须早于候选查询那一行
    assert src.index("_close_gap(") < src.index("_CANDIDATE_SQL.format")
    # 交的是整批 sheet_want,不是被 limit 截过的候选 —— 交候选的话超出 limit 的
    # 缺数据行永远排不上,也就永远不会被推去采集
    assert "_close_gap(rows" not in src and "_close_gap(todo" not in src
    assert "gap_wait" in src and "gap_wait" in pa._KNOWN_PARAMS
    # 而投影必须在补采之后(否则这一轮补采回来、刚判出结论的行写不进表格)
    assert src.index("_close_gap(") < src.index("_project_to_sheet(")


def test_gap_batch_jumps_the_queue_and_says_so_when_it_cannot(monkeypatch):
    """⚠ 不插队 ⇒ 那 20 分钟几乎注定等不到,同轮闭环写了但从不生效。

    时间账:审核 18:10 起跑,而 product_refresh 13:00 推的十几万个任务这时很
    可能还在排。表现会是"每天都超时",看着像采集侧慢 —— 所以这条不是优化,
    是这个功能能不能成立的前提。插队失败只告警,但**必须写进摘要**。
    """
    calls = _stub(monkeypatch, [_BEFORE, _AFTER_ALL], pushed=[], notes=[],
                  calls=[])
    out = "\n".join(pa._close_gap(_WANT, _ROWS, True, 20))
    assert "prioritize" in calls
    assert "插队没成功" not in out
    # 插不上时说清后果,不静默
    monkeypatch.setattr(pa.scrape_batches, "prioritize", lambda n, b: False)
    _stub2 = _stub(monkeypatch, [_BEFORE, _AFTER_ALL], pushed=[], notes=[])
    monkeypatch.setattr(pa.scrape_batches, "prioritize", lambda n, b: False)
    out2 = "\n".join(pa._close_gap(_WANT, _ROWS, True, 20))
    assert "插队没成功" in out2 and "可能等不到" in out2
