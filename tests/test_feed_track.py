"""services/feed_track 回归:统一轮询积木——终态落台账、missing 判定、全局摘要。"""

import contextlib

import pytest

from api import feeds
from services import feed_track


class _Conn:
    def __init__(self):
        self.sqls: list = []

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))
        self._last = sql

    def executemany(self, sql, rows):
        self.sqls.append((sql, list(rows)))

    def fetchall(self):
        return []

    rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_db(monkeypatch, conn):
    from registry import db
    monkeypatch.setattr(db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([conn])))


STORE = {"name": "T1", "client_id": "c", "client_secret": "s", "proxy": None}


def test_poll_feed_terminal_writes_ledger(monkeypatch):
    conn = _Conn()
    _fake_db(monkeypatch, conn)
    done = []
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda store, fid: {"feedStatus": "PROCESSED"})
    monkeypatch.setattr(feeds, "iter_feed_items", lambda store, fid: iter([
        {"sku": "A", "ingestionStatus": "SUCCESS"},
        {"sku": "B", "ingestionStatus": "DATA_ERROR",
         "ingestionErrors": {"ingestionError": [{"code": "ERR_9"}]}},
    ]))
    monkeypatch.setattr(feeds, "mark_feed_done",
                        lambda fid, ok: done.append((fid, ok)))

    head, out = feed_track.poll_feed(STORE, "F1")
    assert head["feedStatus"] == "PROCESSED"
    assert out == {"A": ("success", ""), "B": ("failed", "ERR_9")}
    def _find(frag):        # 按内容找,别按位置(加语句就错位)
        return next(x for x in conn.sqls if frag in x[0])

    sel_sql, _ = _find("SELECT sku, workflow")
    assert "SELECT sku, workflow, feed_type, status" in sel_sql  # 先取更新前状态
    many_sql, rows = _find("SET status = %s")
    assert "UPDATE ops.feed_items" in many_sql
    # 每行带落定依据:沃尔玛原始状态 + 收口方式(汇总终态 = head)
    assert ("success", None, None, "SUCCESS", "head", "F1", "A") in rows
    assert ("failed", "ERR_9", None, "DATA_ERROR", "head", "F1", "B") in rows
    assert many_sql.rstrip().endswith("AND status = 'submitted'")   # 首次落定即定稿
    miss_sql, args = _find("'missing'")
    assert "'missing'" in miss_sql and args[1] == ["A", "B"]   # 查无的标 missing
    assert done == [("F1", True)]


def test_poll_feed_keeps_feed_open_when_sku_processing(monkeypatch, caplog):
    # feed 终态但个别 SKU 仍 INPROGRESS:不 mark_feed_done,下轮重查
    # (否则该 SKU 永久卡 submitted,cleanup 在途拦截会永远跳过它)
    import logging as _logging
    conn = _Conn()
    _fake_db(monkeypatch, conn)
    done = []
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda s, f: {"feedStatus": "PROCESSED"})
    monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([
        {"sku": "A", "ingestionStatus": "SUCCESS"},
        {"sku": "B", "ingestionStatus": "INPROGRESS"}]))
    monkeypatch.setattr(feeds, "mark_feed_done", lambda fid, ok: done.append(fid))
    with caplog.at_level(_logging.WARNING, logger="services.feed_track"):
        _head, out = feed_track.poll_feed(STORE, "F1")
    assert out["B"] == ("processing", "")
    assert done == []
    assert any("仍 processing/unknown" in m for m in caplog.messages)


def test_poll_feed_maintenance_receipt_not_in_ledger_for_non_relist(monkeypatch):
    # 维护类回执不进病历(所有者定稿 2026-08-07):同为 MP_MAINTENANCE,
    # 反补来源(problem_product_cleanup)进,标题/到期日期维护来源不进;
    # feed_items 台账两者照常落定
    class _MetaConn(_Conn):
        def fetchall(self):
            if "SELECT sku, workflow, feed_type, status" in self._last:
                return [("A", "maintenance", "MP_MAINTENANCE", "submitted"),
                        ("B", "problem_product_cleanup", "MP_MAINTENANCE",
                         "submitted")]
            return []
    conn = _MetaConn()
    _fake_db(monkeypatch, conn)
    recorded = []
    monkeypatch.setattr(feed_track.product_events, "record_many",
                        lambda c, rows: (recorded.extend(rows), len(rows))[1])
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda s, f: {"feedStatus": "PROCESSED"})
    monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([
        {"sku": "A", "ingestionStatus": "SUCCESS"},
        {"sku": "B", "ingestionStatus": "SUCCESS"}]))
    monkeypatch.setattr(feeds, "mark_feed_done", lambda fid, ok: None)
    feed_track.poll_feed(STORE, "F1")
    assert [e["sku"] for e in recorded] == ["B"]        # 只有反补来源入账
    many_sql, rows = next(x for x in conn.sqls if "SET status = %s" in x[0])
    assert ("success", None, None, "SUCCESS", "head", "F1", "A") in rows  # 台账不受白名单影响


def test_poll_feed_repoll_does_not_duplicate_events(monkeypatch):
    # 重轮询(上一轮已把 A 落定)只对本轮才落定的 SKU 记回执事件
    class _MetaConn(_Conn):
        def fetchall(self):
            if "SELECT sku, workflow, feed_type, status" in self._last:
                return [("A", "wf", "DELETE_ITEM", "success"),
                        ("B", "wf", "DELETE_ITEM", "submitted")]
            return []
    conn = _MetaConn()
    _fake_db(monkeypatch, conn)
    recorded = []
    monkeypatch.setattr(feed_track.product_events, "record_many",
                        lambda c, rows: (recorded.extend(rows), len(rows))[1])
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda s, f: {"feedStatus": "PROCESSED"})
    monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([
        {"sku": "A", "ingestionStatus": "SUCCESS"},
        {"sku": "B", "ingestionStatus": "SUCCESS"}]))
    monkeypatch.setattr(feeds, "mark_feed_done", lambda fid, ok: None)
    feed_track.poll_feed(STORE, "F1")
    assert [e["sku"] for e in recorded] == ["B"]


# ── feed 级拒收:终态 ERROR + 零明细(2026-09-07 A131吕灿荣 整店改码实证)──────

#: 沃尔玛原文(feed 级 ingestionError,itemsReceived=0)。整店 2740 条改码分三个
#: MP_ITEM_MATCH feed,三条全是这一份 —— 按「明细里查无 ⇒ missing」办的话,
#: sku_migrate 的 `_verdict` 不会当场回滚(它只认 failed),2740 条卡满 24h 观测期。
_A131_ERR = {
    "type": "DATA_ERROR",
    "code": "EXT_DATA_ERROR_50575703577001",
    "description": ("You have exceeded your item setup limit of 5000. "
                    "Please resubmit your file to ensure that the total number "
                    "of items in your catalog is below your designated limit."),
}
_A131_HEAD = {"feedStatus": "ERROR", "itemsReceived": 0, "itemsSucceeded": 0,
              "itemsFailed": 0,
              "ingestionErrors": {"ingestionError": [_A131_ERR]}}


class _LedgerConn(_Conn):
    """台账里预写了三条 SKU(提交时落的),终态明细里一条都查不到。"""

    def fetchall(self):
        if "SELECT sku, workflow, feed_type, status" in self._last:
            return [(s, "sku_migrate", "MP_ITEM_MATCH", "submitted")
                    for s in ("S1", "S2", "S3")]
        return []


def test_feed_level_error_without_details_fails_every_ledger_sku(monkeypatch):
    """整 feed 被拒 ⇒ 台账逐 SKU 落 **failed**(带 feed 级码与原文),不落 missing。

    missing 的语义是"沃尔玛没说这条的下落",而这里它说得很清楚:一条都没收。
    读成 missing 的后果是改码等 24h 观测反证、上架/维护链把"整批没进去"看成"查无"。
    """
    conn = _LedgerConn()
    _fake_db(monkeypatch, conn)
    done = []
    monkeypatch.setattr(feeds, "get_feed_status", lambda s, f: dict(_A131_HEAD))
    monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([]))
    monkeypatch.setattr(feeds, "mark_feed_done",
                        lambda fid, ok: done.append((fid, ok)))

    head, out = feed_track.poll_feed(STORE, "F1")
    assert head["feedStatus"] == "ERROR"
    assert out == {s: ("failed", "EXT_DATA_ERROR_50575703577001")
                   for s in ("S1", "S2", "S3")}

    def _find(frag):
        return next(x for x in conn.sqls if frag in x[0])

    _sql, rows = _find("SET status = %s")
    assert len(rows) == 3
    for status, code, desc, raw, by, fid, _sku in rows:
        assert (status, code, fid) == ("failed",
                                       "EXT_DATA_ERROR_50575703577001", "F1")
        assert "item setup limit of 5000" in desc      # 码本身不含任何信息
        assert (raw, by) == ("ERROR(整 feed 拒收)", "head")
    # 台账里一条都不许落 missing:三个 SKU 都在"已落定"的名单里
    _sql, args = _find("'missing'")
    assert sorted(args[1]) == ["S1", "S2", "S3"]
    # feed_log 落 failed(ok=False),不是 done
    assert done == [("F1", False)]
    # 报错明细照落(聚合看 ops.v_feed_error_stats),一 SKU 一行
    _sql, err_rows = _find("INSERT INTO ops.feed_item_errors")
    assert len(err_rows) == 3
    assert {r[4] for r in err_rows} == {"EXT_DATA_ERROR_50575703577001"}


def test_feed_level_error_without_ledger_rows_changes_nothing(monkeypatch):
    """台账里一条都没有(提交时没预写)⇒ 没有可回执的对象,行为与从前一样。"""
    conn = _Conn()
    _fake_db(monkeypatch, conn)
    monkeypatch.setattr(feeds, "get_feed_status", lambda s, f: dict(_A131_HEAD))
    monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([]))
    monkeypatch.setattr(feeds, "mark_feed_done", lambda fid, ok: None)
    assert feed_track.poll_feed(STORE, "F1")[1] == {}


def test_an_error_feed_that_does_carry_details_is_unchanged(monkeypatch):
    """**有**逐条明细的 ERROR feed 一字不变:真相在明细里,台账里多出来的
    那条(明细没提到)照旧落 missing。"""
    conn = _LedgerConn()
    _fake_db(monkeypatch, conn)
    monkeypatch.setattr(feeds, "get_feed_status", lambda s, f: dict(
        _A131_HEAD, itemsReceived=2))
    monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([
        {"sku": "S1", "ingestionStatus": "SUCCESS"},
        {"sku": "S2", "ingestionStatus": "DATA_ERROR",
         "ingestionErrors": {"ingestionError": [{"code": "ERR_9"}]}}]))
    monkeypatch.setattr(feeds, "mark_feed_done", lambda fid, ok: None)
    _head, out = feed_track.poll_feed(STORE, "F1")
    assert out == {"S1": ("success", ""), "S2": ("failed", "ERR_9"),
                   "S3": ("missing", "")}           # 台账里有、明细里查无
    _sql, args = next(x for x in conn.sqls if "'missing'" in x[0])
    assert sorted(args[1]) == ["S1", "S2"]            # S3 落 missing,不被顶掉


def test_ingestion_errors_accepts_both_official_shapes():
    """裸 list 与 {"ingestionError": [...]} 两种形态都实见过(旧仓 _first_error
    专门兼容)。只认 dict 那一种的表现是报错原文静默读空、还不报错。"""
    assert feed_track.ingestion_errors(
        {"ingestionErrors": {"ingestionError": [_A131_ERR]}}) == [_A131_ERR]
    assert feed_track.ingestion_errors(
        {"ingestionErrors": [_A131_ERR]}) == [_A131_ERR]
    assert feed_track.ingestion_errors({}) == []
    assert feed_track.ingestion_errors({"ingestionErrors": None}) == []


def test_poll_feed_not_terminal_returns_head_and_none(monkeypatch):
    monkeypatch.setattr(feeds, "get_feed_status", lambda store, fid: {
        "feedStatus": "INPROGRESS", "itemsReceived": 10, "itemsSucceeded": 3,
        "itemsFailed": 1})
    head, results = feed_track.poll_feed(STORE, "F1")
    assert results is None
    # 进度计数直接来自 feed 级 GET,零明细翻页
    assert feed_track._progress(head) == "已收 10,成功 3,失败 1,待处理 6"


def test_poll_all_summary_and_pending_reconcile(monkeypatch):
    """摘要:逐 feed 明细照旧;pending 先过对账器,首行报三档个数,明细逐行报依据。

    ⚠ pending 的明细必须进**摘要**(发去飞书的那一份),不能只在日志里(2026-08-16
    feed 闭环审计);2026-09-25 起它们不再"永不老化":每轮只读反查,到期收口。
    """
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        {"status": "submitted", "feed_id": "F1", "store": "T1",
         "feed_type": "DELETE_ITEM", "workflow": "", "created_at": "t"},
        {"status": "submitted", "feed_id": "F2", "store": "T_GONE",
         "feed_type": "DELETE_ITEM", "workflow": "", "created_at": "t"},
        {"status": "pending", "feed_id": None, "store": "T1",
         "feed_type": "RETIRE_ITEM", "workflow": "product_clear", "created_at": "t"},
    ])
    monkeypatch.setattr(feed_track, "poll_feed",
                        lambda store, fid, **_: ({"feedStatus": "PROCESSED"},
                                            {"A": ("success", "")}))
    monkeypatch.setattr(feed_track.runlock, "is_held", lambda name: False)
    out = feed_track.poll_all({"T1": STORE})
    assert "落定 1" in out and "凭证缺失跳过 1" in out
    assert "pending 对账 1:仍待 1" in out.splitlines()[0]
    assert "T1 删除(-) F1:已落定 PROCESSED,成功 1,失败 0" in out
    assert "T_GONE 删除(-) F2:店铺凭证缺失,跳过" in out
    assert "每轮只读反查,**不自动补交**" in out
    assert "T1 停用(product_clear):存量 pending(没记条数与 SKU,无法反查)" in out


def test_save_errors_rows_shape():
    """每条 ingestionError 一行,带 field/code——聚合分析的主维度。"""
    from services.feed_track import _save_errors

    captured = {}

    class _Cur:
        def executemany(self, sql, rows):
            captured["sql"] = sql
            captured["rows"] = rows

    errs = {"SKU1": [{"type": "DATA_ERROR", "code": "C1", "field": "color",
                      "description": "required"},
                     {"type": "DATA_ERROR", "code": "C2", "field": "material",
                      "description": "need JSONArray"}],
            "SKU2": []}
    n = _save_errors(_Cur(), "F1", "店A", errs,
                     {"SKU1": ("list_new", "MP_ITEM", "submitted")})
    assert n == 2
    assert "ON CONFLICT (feed_id, sku, seq) DO NOTHING" in captured["sql"]
    first = captured["rows"][0]
    assert first[:6] == ("F1", "SKU1", 0, "DATA_ERROR", "C1", "color")
    assert first[7:10] == ("MP_ITEM", "店A", "list_new")
    assert captured["rows"][1][2] == 1                  # seq 递增
    assert _save_errors(_Cur(), "F1", "店A", {"S": []}, {}) == 0


def test_merge_error_shapes():
    m = feed_track.merge_error
    assert m("C1", "坏了") == "C1 | 坏了"
    assert m("C1", "") == "C1"                 # 只有码
    assert m("", "坏了") == "坏了"              # 只有描述
    assert m(None, None) == ""
    assert len(m("C1", "x" * 2000)) == 900     # 截断


def test_result_text_is_the_union_of_the_four_sheet_copies():
    """「状态→中文」四份拷贝的并集 + 2026-09-25 期限收口的三个终态词,一个都不能少。

    processing/unknown 只有 clear_sheet 那份带,而 product_clear 是
    `RESULT_TEXT[outcome]` **直接下标**取(不是 .get)——少一键就是 KeyError,
    停用/删除表的整轮回写当场炸。missing 同理来自 poll_feed 的"台账里有、
    终态明细里查无",中文改「明细无此条」(原「未查到」与维护表 3 天超期同字不同义)。
    """
    assert feed_track.RESULT_TEXT == {
        "success": "成功", "failed": "失败", "missing": "明细无此条",
        "overdue": "超期未完成", "unrecognized": "未知状态", "unreadable": "无法查询",
        "submitted": "处理中", "processing": "处理中", "unknown": "处理中"}


def test_text_of_maps_status_and_never_fakes_a_verdict():
    """未登记状态按未落定报「处理中」:不装成功也不装失败,下轮再看。"""
    t = feed_track.text_of
    assert t("success") == "成功"
    assert t("failed") == "失败"
    assert t("missing") == "明细无此条"
    assert t("overdue") == "超期未完成" and t("unreadable") == "无法查询"
    assert t("unrecognized") == "未知状态"
    assert t("submitted") == t("processing") == t("unknown") == "处理中"
    assert t("OFFICIAL_NEW_ENUM") == "处理中"
    assert t("") == "处理中"


def test_text_of_appends_error_only_on_the_failed_bucket():
    """跟卖表现行形状「失败:{码 | 人话}」;成功/明细无此条后面不挂报错。"""
    t = feed_track.text_of
    want = "EXT_ERR_1 | [color] required"
    assert t("failed", want) == f"失败:{want}"
    assert t("failed", "") == "失败"
    assert t("success", want) == "成功"
    assert t("missing", want) == "明细无此条"
    assert t("overdue", want) == "超期未完成"
    assert t("submitted", want) == "处理中"


def test_all_reflectors_write_code_plus_desc(monkeypatch):
    """停用/删除、维护记录、跟卖三张表的报错列都要带人话(不只上架表)。"""
    from api import feishu
    from registry import resources
    from registry.resources import Spreadsheet
    from services import clear_sheet, match_sheet

    def _live(sheet):        # 登记条目是 frozen dataclass:换整个对象,别改字段
        return Spreadsheet(name=sheet.name, token="TOK", sheet_id="SID",
                           columns=sheet.columns)

    monkeypatch.setattr(feed_track, "item_results",
                        lambda fid, workflow=None: {"SKU1": ("failed", "EXT_ERR_1")})
    monkeypatch.setattr(feed_track, "item_errors",
                        lambda fid, workflow=None: {"SKU1": "[color] required"})
    want = "EXT_ERR_1 | [color] required"

    # 停用/删除表:H 报错列
    monkeypatch.setattr(clear_sheet, "read_rows", lambda: [
        {"rownum": 2, "sku": "SKU1", "feed_id": "F1", "op_date": "d",
         "result": "处理中", "error": ""}])
    captured = {}
    monkeypatch.setattr(clear_sheet, "writeback",
                        lambda ups: (captured.update(clear=ups), len(ups))[1])
    monkeypatch.setattr(resources, "RETIRE_SHEET",
                        _live(resources.RETIRE_SHEET))
    clear_sheet.sync_from_ledger()
    assert captured["clear"][0][4] == want

    # 跟卖表:J feed结果
    monkeypatch.setattr(match_sheet, "read_rows", lambda: [
        {"rownum": 2, "sku": "SKU1", "feed_id": "F1", "feed_result": "",
         "check_time": ""}])
    monkeypatch.setattr(match_sheet, "row_vals", lambda r: [r["feed_result"]])
    monkeypatch.setattr(resources, "MATCH_SHEET",
                        _live(resources.MATCH_SHEET))
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda s, ups: (captured.update(match=ups), len(ups))[1])
    match_sheet.sync_from_ledger()
    assert captured["match"][0][1][0][0] == f"失败:{want}"


def test_prohibited_receipt_flows_into_blacklist(monkeypatch):
    """上架回执违禁 → 自动入 ASIN 黑名单 B=禁售(所有者 2026-08-12:失败
    事件反哺'上架前拦截')。只收 kind=list;跟卖 sku 提不出 ASIN 不收。"""
    class _C(_Conn):
        def fetchall(self):
            if "FROM ops.feed_items" in self._last:
                return [("B0BAD01", "list_new", "MP_ITEM", "submitted"),
                        ("PHUMWMT1", "match_listing", "MP_ITEM_MATCH",
                         "submitted")]
            return []

    conn = _C()
    _fake_db(monkeypatch, conn)
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda s, f: {"feedStatus": "PROCESSED"})
    monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([
        {"sku": "B0BAD01", "ingestionStatus": "DATA_ERROR",
         "ingestionErrors": {"ingestionError": [
             {"code": "EXT_DATA_ERROR_61020366035308",
              "description": "General Prohibited Product"}]}},
        {"sku": "PHUMWMT1", "ingestionStatus": "DATA_ERROR",
         "ingestionErrors": {"ingestionError": [
             {"code": "EXT_DATA_ERROR_61020366035308"}]}},
    ]))
    monkeypatch.setattr(feeds, "mark_feed_done", lambda f, ok: None)
    got, srcs = [], []
    monkeypatch.setattr(feed_track.blacklist, "record_asins",
                        lambda c, items, src="scan":
                            (got.extend(items), srcs.append(src), len(items))[2])

    feed_track.poll_feed(STORE, "F9")
    assert len(got) == 1 and got[0]["sku"] == "B0BAD01"
    assert srcs == ["feed"]                 # 上架回执进口要自报身份(taxonomy_src)
    assert got[0]["category"] == "POLICY"   # 换轨前是旧码 B(禁售);
    # 两者都在 PERMANENT 里 ⇒ 拦截行为一字不变,变的只是码名统一到新表
    assert "61020366035308" in got[0]["reasons"]
    assert "General Prohibited" in got[0]["reasons"]


def test_poll_all_is_cross_store_concurrent_and_in_store_serial(monkeypatch):
    """跨店并发、店内串行;摘要按店铺序排,不按完成先后。

    所有者定稿 2026-08-17。盯三件事:
      ① **跨店真并发**:三家店的 poll_feed 必须能同时在飞(Barrier 会合)。
      ② **店内仍串行**:同一店的两个 feed 不许并发 —— 沃尔玛配额按
         `(store, endpoint)` 计,店内并发只会让自己排队等退避。
      ③ **顺序确定**:让 A085 最慢、谭总2 最快,摘要仍须按 sort_key 排
         (A085 → 81张三 → 谭总2)。query_pending 没有 ORDER BY,原来那份
         顺序是 PG 堆序,本来就不稳定,并发之后更要显式定序。
    """
    import threading

    stores = ["谭总2", "A085", "81张三"]
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        {"status": "submitted", "feed_id": f"F{s}{i}", "store": s,
         "feed_type": "DELETE_ITEM", "workflow": "", "created_at": "t"}
        for s in stores for i in (1, 2)])

    gate = threading.Barrier(3, timeout=5)     # 三家店会合 = 真的同时在飞
    lock = threading.Lock()
    in_store: dict[str, int] = {}
    peak_in_store = {"v": 0}

    def fake_poll(store, fid, **_):
        name = store["name"]
        with lock:
            in_store[name] = in_store.get(name, 0) + 1
            peak_in_store["v"] = max(peak_in_store["v"], in_store[name])
        if fid.endswith("1"):
            gate.wait()                        # 每店第一个 feed 上会合
        with lock:
            in_store[name] -= 1
        return {"feedStatus": "PROCESSED"}, {"A": ("success", "")}

    monkeypatch.setattr(feed_track, "poll_feed", fake_poll)
    out = feed_track.poll_all({s: {"name": s} for s in stores})

    assert gate.n_waiting == 0 and not gate.broken, "三家店没能同时在飞"
    assert peak_in_store["v"] == 1, "同一个店内并发了,配额桶会自己挤自己"
    assert "落定 6" in out
    got = [l.split(" ")[2] for l in out.splitlines() if "已落定" in l]
    # 每店两个 feed:店序按 sort_key,店内两行相邻(不与别店交织)
    assert got == ["A085", "A085", "81张三", "81张三", "谭总2", "谭总2"], got


def test_reflectors_run_concurrently_but_same_sheet_stays_serial(monkeypatch):
    """反哺器链间并发、链内串行;摘要按登记顺序拼,不按完成先后。

    所有者定稿 2026-08-17「一起并」。分链的判据是**写不写同一张表**:
    上架表与上架表自愈都走 read_rows → 算 → 回写,是读-改-写三步,并发跑
    后者会读到前者写之前的快照。`_sheet_locks` 只串得住"写"那一步。
    """
    import threading

    from workflows import feed_poll

    gate = threading.Barrier(4, timeout=5)      # 四条链会合 = 真的同时在飞
    order: list[str] = []
    lock = threading.Lock()

    def _mk(label, first=False):
        def _f(execute=True):                    # 五个反哺器统一收 execute
            if first:                            # 每条链的头一个反哺器上会合
                gate.wait()
            with lock:
                order.append(label)
            return f"{label}:回写 1 行"
        return _f

    def _boom(execute=True):
        gate.wait()          # 也要会合:秒退的话剩下三条永远凑不齐四个
        raise RuntimeError("飞书 90227")

    monkeypatch.setattr(feed_poll, "_REFLECTOR_CHAINS", [
        [("停用/删除表", _mk("停用/删除表", first=True))],
        [("维护记录", _mk("维护记录", first=True))],
        [("跟卖表", _boom)],                     # 一条链整个炸掉
        [("上架表", _mk("上架表", first=True)),
         ("上架表自愈", _mk("上架表自愈"))],
    ])

    out = feed_poll._run_reflectors()

    assert not gate.broken, "四条反哺链没能同时在飞"
    # 同一条链内严格按登记顺序:自愈永远在上架表之后
    assert order.index("上架表") < order.index("上架表自愈")
    # 摘要按登记顺序拼,不按完成先后;一条链炸了只吃掉它自己那一行
    assert [l.split(":")[0] for l in out] == [
        "停用/删除表", "维护记录", "⚠ 跟卖表回写失败", "上架表", "上架表自愈"]


def test_same_sheet_reflectors_registered_in_one_chain():
    """**同一个 services 模块出来的反哺器必须在同一条链里**(登记侧的不变量)。

    上一个用例验的是机制(链内串行、链间并发),这一个钉的是**登记**:
    每个 sheet 模块正好管一张飞书表,所以"同模块"就是"同一张表"的机械判据。
    哪天有人往 _REFLECTOR_CHAINS 追加一个 listing_sheet.xxx 却另起一条链,
    上架表就会被两个线程读-改-写,而且**不报错**,只是偶尔覆盖掉对方的回写。
    """
    from workflows import feed_poll

    seen: dict[str, int] = {}
    for i, chain in enumerate(feed_poll._REFLECTOR_CHAINS):
        for label, fn in chain:
            mod = getattr(fn, "__module__", "")
            if mod in seen and seen[mod] != i:
                raise AssertionError(
                    f"{label}({mod})与第 {seen[mod]} 条链同模块=同一张表,"
                    f"却被登记在第 {i} 条链 —— 并发读-改-写会互相覆盖")
            seen[mod] = i
    # 上架的两步是本仓当下唯一的同表组合,正向钉住它别被拆开
    listing = [c for c in feed_poll._REFLECTOR_CHAINS
               if any(f.__module__.endswith("listing_sheet") for _, f in c)]
    assert len(listing) == 1 and len(listing[0]) == 2


def test_sku_migrate_failure_never_blacklists_the_asin(monkeypatch):
    """改码被拒**不得反哺 ASIN 黑名单**(SKU 改造批次 3,O8)。

    形态 B 下改码走 MP_ITEM ⇒ kind=list ⇒ 正好命中这段反哺。一次改码被拒若碰巧
    带上违禁码,会把一个**正在正常销售**的 ASIN 永久拉黑(record_asins 是
    PERMANENT),list_new/match_listing 的黑名单闸下一轮就开始拦 —— 后果永久,
    且没有任何摘要会说是改码干的。改码失败是我们的载荷/时序问题,不是政策违禁。
    """
    class _C(_Conn):
        def fetchall(self):
            if "FROM ops.feed_items" in self._last:
                return [("B0MIG001", "sku_migrate", "MP_ITEM", "submitted"),
                        ("B0BAD02", "list_new", "MP_ITEM", "submitted")]
            return []

    conn = _C()
    _fake_db(monkeypatch, conn)
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda s, f: {"feedStatus": "PROCESSED"})
    monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([
        {"sku": sku, "ingestionStatus": "DATA_ERROR",
         "ingestionErrors": {"ingestionError": [
             {"code": "EXT_DATA_ERROR_61020366035308",
              "description": "General Prohibited Product"}]}}
        for sku in ("B0MIG001", "B0BAD02")]))
    monkeypatch.setattr(feeds, "mark_feed_done", lambda f, ok: None)
    got = []
    monkeypatch.setattr(feed_track.blacklist, "record_asins",
                        lambda c, items, src="scan": (got.extend(items), len(items))[1])

    feed_track.poll_feed(STORE, "F10")
    # 同一份回执里:改码那行被放过,上架那行照常反哺(挡的是来源,不是整段)
    assert [g["sku"] for g in got] == ["B0BAD02"]


def test_sku_migrate_receipt_writes_no_product_event(monkeypatch):
    """改码回执一条病历都不写(O7 的收口点在 feed_track 这一侧的回归)。

    与 O8 是两件事:O8 管黑名单反哺,这条管 product_events —— 形态 B 的
    kind=list 恒入账,不挡就是一串 list_feed_success 灌进「上架」时间线。
    """
    class _C(_Conn):
        def fetchall(self):
            if "FROM ops.feed_items" in self._last:
                return [("B0MIG002", "sku_migrate", "MP_ITEM", "submitted"),
                        ("B0LIST01", "list_new", "MP_ITEM", "submitted")]
            return []

    conn = _C()
    _fake_db(monkeypatch, conn)
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda s, f: {"feedStatus": "PROCESSED"})
    monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([
        {"sku": "B0MIG002", "ingestionStatus": "SUCCESS"},
        {"sku": "B0LIST01", "ingestionStatus": "SUCCESS"}]))
    monkeypatch.setattr(feeds, "mark_feed_done", lambda f, ok: None)
    written = []
    monkeypatch.setattr(feed_track.product_events, "record_many",
                        lambda c, rows: written.extend(rows) or len(rows))

    feed_track.poll_feed(STORE, "F11")
    assert [(r["sku"], r["event"]) for r in written] == \
        [("B0LIST01", "list_feed_success")]


# ══════════════════════════════════════════════════════════════════════════════
#  receipt_blocked:破坏类回执闸的**唯一一份 SQL**(2026-09-09 上移到 services)
#
#  两个消费方:problem_scan(死档 / 永久拒不再重建议)与 sku_migrate(死档不
#  改码)。各写一份的表现是两条链对"这个 SKU 还在不在"给出不同答案,而且不报错。
# ══════════════════════════════════════════════════════════════════════════════

def test_receipt_blocked_sql_shape():
    """SQL 的四个形状要件,每个都对应一种"错了不报错"的失效方式。"""
    q = feed_track._RECEIPT_BLOCKED_SQL
    # ① 最近一次尝试,不是"历史上出现过就永久拉黑"(转出 WFS 之后要能放出来)
    assert "DISTINCT ON (store, sku)" in q
    assert "ORDER BY store, sku, submitted_at DESC" in q
    # ② 先取最近一次、**再**比码集:内层就按码过滤会拿更早那次失败当结论
    assert q.index("DISTINCT ON") < q.index("t.error_code = ANY(%(codes)s::text[])")
    # ③ 删与停都算(顽固件双发的另一半不能漏)
    assert "f.feed_type = ANY(%(feeds)s::text[])" in q
    assert feed_track.DESTRUCTIVE_FEED_TYPES == ("DELETE_ITEM", "RETIRE_ITEM")
    # ④ 改码链继承一跳(新码在 feed_items 里一条历史都没有,不继承就整个失明)
    assert "catalog.sku_aliases a" in q and q.count("UNION ALL") == 1
    assert "f.sku = a.alias_sku" in q
    assert "listing_sources" not in q          # 代际继承只准经视图
    # 每个参数带显式 ::类型(本仓 SQL 因 PG 推不出参数类型连炸三次的老教训)
    import re
    assert not re.findall(r"%\((\w+)\)s(?!\s*::)", q)


def test_receipt_blocked_asks_nothing_when_the_code_set_is_empty():
    """空码集 ⇒ 一条 SQL 都不发(`= ANY('{}')` 恒假,查了也是白查)。"""
    conn = _Conn()
    assert feed_track.receipt_blocked(conn, []) == set()
    assert conn.sqls == []


def test_receipt_blocked_passes_the_codes_and_the_store_through():
    """码集由调用方从 registry 传进来,本函数**不认识任何具体的码**(铁律 3);
    store 为 None = 全船队(SQL 里那半条 `IS NULL OR` 恒真)。"""
    conn = _Conn()
    feed_track.receipt_blocked(conn, {"B", "A", "A"}, store="T1")
    sql, args = conn.sqls[0]
    assert sql is feed_track._RECEIPT_BLOCKED_SQL
    assert args["codes"] == ["A", "B"] and args["store"] == "T1"
    assert args["feeds"] == list(feed_track.DESTRUCTIVE_FEED_TYPES)


# ── 在途 feed 的摘要口径(2026-09-11)────────────────────────────────────────
# 所有者实见:飞书里这五行每 30 分钟原样再来一遍 ——
#   A085朱丽霖 改价(maintenance) 18CEF5AC…:INPROGRESS,已收 15,成功 12,失败 2,待处理 1
#   A109黄威威 上架(list_new) 18CE2095…:已落定 PROCESSED,成功 451,失败 42
#   …
# 两件事凑出来的:① 说"已落定"的那几条**其实没落定**(残留 SKU 仍 processing
# ⇒ poll_feed 不调 mark_feed_done,行留在 feed_log 里下轮重查);② 在途行永不
# 老化,于是同一段明细一天播 48 遍。下面这组钉住修法。

def _inflight(store="T1", fid="F1", ft="DELETE_ITEM", wf="", age_h=None,
              created_age_h=None):
    """在途 feed_log 行;age_h 给了就按"几小时前提交"落 updated_at。"""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return {"status": "submitted", "feed_id": fid, "store": store,
            "feed_type": ft, "workflow": wf,
            "created_at": (now - timedelta(hours=created_age_h)
                           if created_age_h is not None else "t"),
            "updated_at": (now - timedelta(hours=age_h)
                           if age_h is not None else None)}


def test_unresolved_counts_the_residue_and_splits_out_unknown():
    """(未落定数, 其中状态未知数)——**收工判据只有这一处**。

    第二个数是给摘要分因用的:processing 是沃尔玛还在跑(等就行),unknown 是
    `sku_outcome` 没认出来的枚举值(等到天荒地老也不会变,得补码表)。
    """
    assert feed_track.unresolved({}) == (0, 0)
    assert feed_track.unresolved({"A": ("success", ""), "B": ("failed", "E")}) == (0, 0)
    assert feed_track.unresolved({"A": ("processing", ""), "B": ("unknown", ""),
                                  "C": ("success", "")}) == (2, 1)


def test_terminal_feed_with_residue_never_claims_it_settled(monkeypatch):
    """feed 终态 ≠ 落定:有残留就不许说"已落定",也不许计进 `落定 N`。

    poll_feed 这时**不**调 mark_feed_done(残留 SKU 要留在在途队列里下轮重查,
    否则它们永久卡 submitted、在途拦截会永远跳过)。摘要却说"已落定 PROCESSED,
    成功 451,失败 42"的后果:行还在 feed_log 里,下一轮一字不差再播一遍、
    `落定` 每轮把同一个 feed 重数一次 —— 每句话都对,合起来是假的。
    """
    monkeypatch.setattr(feeds, "query_pending",
                        lambda: [_inflight(fid="F1", ft="MP_ITEM", wf="list_new")])
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f, **_: (
        {"feedStatus": "PROCESSED"},
        {"A": ("success", ""), "B": ("failed", "E1"), "C": ("processing", "")}))
    out = feed_track.poll_all({"T1": STORE})
    assert "已落定" not in out
    assert "落定 0,仍处理中 1" in out
    assert "T1 上架(list_new) F1:PROCESSED 已终态,但 1 个 SKU 仍在处理" in out


def test_residue_says_out_loud_when_it_is_an_unrecognised_enum(monkeypatch):
    """残留是 unknown 时摘要要点破:枚举可能已扩,光等是等不来的。

    `sku_outcome` 对没见过的 ingestionStatus 返回 unknown 并告警 —— 那条告警
    只在日志里,而摘要是发去飞书的那一份。不说,人只看得到"还有 3 个没落定",
    以为沃尔玛慢,实际是码表该补了。
    """
    monkeypatch.setattr(feeds, "query_pending", lambda: [_inflight(fid="F1")])
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f, **_: (
        {"feedStatus": "PROCESSED"},
        {"A": ("success", ""), "B": ("unknown", "")}))
    out = feed_track.poll_all({"T1": STORE})
    assert "1 个状态未知" in out and "枚举可能已扩" in out


def test_long_in_flight_feeds_fold_into_one_line_with_a_next_step(monkeypatch):
    """超过静默闸的在途 feed:折成一行点名,不再逐条复读明细。

    feed_poll 挂 0/30 分两班;卡住的 feed 一天把同一段明细原样发 48 遍,而人
    对固定文案的反应是不看 —— 真出事的那一轮跟着一起漏掉。折叠只动**排版**:
    这些行照旧每轮轮询、照旧不落定,一个业务判断都没改。
    """
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        _inflight(fid="F1", ft="MP_MAINTENANCE", wf="maintenance", age_h=36.0),
        _inflight(fid="F2", ft="MP_INVENTORY", wf="maintenance", age_h=5.0)])
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f, **_: (
        {"feedStatus": "INPROGRESS", "itemsReceived": 15, "itemsSucceeded": 12,
         "itemsFailed": 2}, None))
    out = feed_track.poll_all({"T1": STORE})
    assert "F1" not in out and "F2" not in out          # 明细不再逐条复读
    assert "⏳ 长期在途 2(最久 36.0h)" in out            # 首行带结论(规矩 1)
    assert "T1 维护(卡 36h)、T1 分仓库存(卡 5h)" in out   # 点得出是哪几个
    assert "refdata/walmart_slas.tsv" in out            # 自带处置(规矩 3):期限依据
    assert "到了落定期限即按明细强制落定" in out
    assert "feed 轮询:2 个在途,落定 0,仍处理中 2" in out


def test_a_fresh_in_flight_feed_keeps_its_own_detail_line(monkeypatch):
    """刚提交的在途 feed 照旧出明细行:人正等着它,进度是有用信息。

    ⚠ 年龄按 **updated_at**(这个 feedId 的提交时刻)算,不是 created_at ——
    `_log_claim` 重占终态行时不重置 created_at,这一行的 created_at 是 100 小时
    前那次同载荷提交留下的。拿 created_at 当年龄,刚提交的 feed 一上来就被判成
    "卡了四天"、当场从摘要里折掉。
    """
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        _inflight(fid="F1", age_h=0.5, created_age_h=100.0)])
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f, **_: (
        {"feedStatus": "INPROGRESS", "itemsReceived": 10, "itemsSucceeded": 3,
         "itemsFailed": 1}, None))
    out = feed_track.poll_all({"T1": STORE})
    assert "T1 删除(-) F1:INPROGRESS,已收 10,成功 3,失败 1,待处理 6" in out
    assert "长期在途" not in out


def test_an_unknowable_age_counts_as_fresh(monkeypatch):
    """年龄拿不到(updated_at 缺)一律当新鲜:宁可多播一行,不可少播一行。"""
    monkeypatch.setattr(feeds, "query_pending", lambda: [_inflight(fid="F1")])
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f, **_: (
        {"feedStatus": "INPROGRESS"}, None))
    out = feed_track.poll_all({"T1": STORE})
    assert "T1 删除(-) F1:INPROGRESS" in out and "长期在途" not in out


def test_a_feed_that_finally_settles_prints_even_after_days(monkeypatch):
    """落定永远出明细行,哪怕它在途了四天:那是**新信息**,而且下一轮这个
    feed 就出队了,只播这一次 —— 折叠折的是"还会再播 47 遍"的那些。"""
    monkeypatch.setattr(feeds, "query_pending",
                        lambda: [_inflight(fid="F1", age_h=99.0)])
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f, **_: (
        {"feedStatus": "PROCESSED"}, {"A": ("success", "")}))
    out = feed_track.poll_all({"T1": STORE})
    assert "T1 删除(-) F1:已落定 PROCESSED,成功 1,失败 0" in out
    assert "长期在途" not in out


def test_every_submittable_feed_type_has_a_chinese_label():
    """摘要里不许再蹦裸 feedType(2026-09-11 实见「A171罗尹鸿 MP_INVENTORY」)。

    漏登记不报错,只是运营看不出那是分仓库存 —— 守门测试比"记得去加"可靠。
    """
    from api.feeds import _SLICE_LIMITS
    missing = [ft for ft in _SLICE_LIMITS if ft not in feed_track._FEED_LABEL]
    assert not missing, f"_FEED_LABEL 漏登记 feedType: {missing}"


def test_query_pending_carries_the_submit_moment(monkeypatch):
    """query_pending 必须带 updated_at:在途年龄是拿它算的(见上一条的理由)。"""
    import contextlib

    from registry import db

    class _C(list):
        description = ()

        def cursor(self):
            return self

        def execute(self, sql, args=None):
            self.append(sql)

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    c = _C()
    monkeypatch.setattr(db, "pg_conn", contextlib.contextmanager(lambda: iter([c])))
    assert feeds.query_pending() == []
    assert "updated_at" in c[0]


# ── 通知里那串截断的码要能用(2026-09-11 所有者:「我找不到这些 feed 的完整的码了」)

class _Rows(list):
    """按 execute 的参数返回固定行集的最小假连接。"""

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        self.append((sql, args))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_rows(monkeypatch, rows):
    import contextlib

    from registry import db
    conn = _Rows(rows)
    monkeypatch.setattr(db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([conn])))
    return conn


def test_feed_id_accepts_the_truncated_code_from_the_summary(monkeypatch):
    """摘要里的码是头 18 位 + 「…」,粘过来就该能查 —— 完整码一直在台账里。

    不认前缀的表现是人拿着通知里那串去跑诊断,得到"不在台账中",而他手上
    再没有别的地方能拿到完整码(所有者 2026-09-11 实见)。
    """
    from workflows import feed_poll

    conn = _fake_rows(monkeypatch, [
        ("18CEF5AC89245FD596ABCDEF", "A085朱丽霖", "submitted", "t")])
    assert feed_poll._resolve_feed("18CEF5AC89245FD596…") == (
        "18CEF5AC89245FD596ABCDEF", "A085朱丽霖")
    sql, args = conn[0]
    assert "LIKE" in sql and args[0] == "18CEF5AC89245FD596%"   # 前缀查,省略号吃掉


def test_a_prefix_that_hits_several_feeds_never_guesses(monkeypatch):
    """前缀撞多条 ⇒ 摊开候选让人挑,**绝不**回退到"按原样查"。

    截断的码拿去问沃尔玛只会查无,而查无长得像"这个 feed 不存在" —— 人会
    以为 feed 丢了,实际是我们拿半截码去查的。
    """
    from workflows import feed_poll

    _fake_rows(monkeypatch, [("18CE2095E6975E388B11", "A109黄威威", "submitted", "t"),
                             ("18CE2095A15D51BA8622", "L001贾林红", "submitted", "t")])
    out = feed_poll._resolve_feed("18CE2095", store_hint="A109黄威威")
    assert isinstance(out, str)
    assert "匹配到 2 条" in out
    assert "18CE2095E6975E388B11" in out and "18CE2095A15D51BA8622" in out


def test_an_exact_code_wins_over_a_longer_sibling(monkeypatch):
    """一个完整码恰好是另一个码的前缀时,人打的是哪个就查哪个。"""
    from workflows import feed_poll

    _fake_rows(monkeypatch, [("18CE2095", "A109黄威威", "done", "t"),
                             ("18CE2095AA", "L001贾林红", "submitted", "t")])
    assert feed_poll._resolve_feed("18CE2095") == ("18CE2095", "A109黄威威")


def test_a_feed_outside_the_ledger_still_works_with_an_explicit_store(monkeypatch):
    """台账里没有(旧系统 / Seller Center 手发的 feed):指名店铺就按原样直查;
    不指名则明说去哪儿找码,而不是干巴巴一句"不在台账中"。"""
    from workflows import feed_poll

    _fake_rows(monkeypatch, [])
    assert feed_poll._resolve_feed("ZZZ999", store_hint="A085朱丽霖") == (
        "ZZZ999", "A085朱丽霖")
    _fake_rows(monkeypatch, [])
    out = feed_poll._resolve_feed("ZZZ999")
    assert isinstance(out, str) and "-p stuck=1" in out


def test_stuck_list_prints_full_codes_and_ready_to_paste_commands(monkeypatch):
    """`-p stuck=1`:完整 feed_id + 卡了多久 + 每条现成的诊断命令,老的在前。

    纯读 ops.feed_log,一个沃尔玛接口都不调 —— 卡住的 feed 要人工处置,
    第一步就是"到底是哪几条、码是什么"。
    """
    from datetime import datetime, timedelta, timezone

    from workflows import feed_poll

    now = datetime.now(timezone.utc)
    _fake_rows(monkeypatch, [("18CC2F2D11BA547E88FULL", 298), ("FRESH0001", 7)])
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        {"status": "submitted", "feed_id": "FRESH0001", "store": "A109黄威威",
         "feed_type": "MP_ITEM", "workflow": "list_new", "created_at": now,
         "updated_at": now - timedelta(minutes=20)},
        {"status": "submitted", "feed_id": "18CC2F2D11BA547E88FULL",
         "store": "A162朱行", "feed_type": "DELETE_ITEM",
         "workflow": "product_clear", "created_at": now,
         "updated_at": now - timedelta(hours=300)},
        {"status": "pending", "feed_id": None, "store": "A171罗尹鸿",
         "feed_type": "MP_INVENTORY", "workflow": "maintenance",
         "created_at": "2026-09-01"},
    ])
    out = feed_poll._inflight_list()
    lines = out.splitlines()
    assert "在途 feed 2 条(老的在前),其中卡超过 2h 的 1 条" in lines[0]
    assert "台账里共 305 个 SKU 卡在「处理中」" in lines[0]   # 卡住多少货
    assert "18CC2F2D11BA547E88FULL" in out                      # 完整码,不截断
    assert ("python cli.py feed_poll -p store=A162朱行 "
            "-p feed_id=18CC2F2D11BA547E88FULL" in out)          # 粘了就能跑
    assert lines[1].startswith("  ⏳ A162朱行")                   # 老的在前 + 点名
    assert "FRESH0001" in out and "⏳ A109黄威威" not in out       # 新鲜的不点 ⏳
    assert "另有 pending 1 条" in out                             # 另一个口子也带上


def test_stuck_list_and_the_summary_fold_share_one_threshold():
    """卡多久的口径只有 `feed_track.is_stuck` 一处:两处各写一个阈值的表现是
    通知里折掉了、清单里却不认为它卡住(反过来也一样)。"""
    from workflows import feed_poll

    assert feed_track.is_stuck(None) is False          # 年龄未知一律当新鲜
    assert feed_track.is_stuck(feed_track.FEED_QUIET_HOURS - 0.01) is False
    assert feed_track.is_stuck(feed_track.FEED_QUIET_HOURS) is True
    src = __import__("inspect").getsource(feed_poll._inflight_list)
    assert "feed_track.is_stuck" in src and "FEED_QUIET_HOURS" not in src.replace(
        "feed_track.FEED_QUIET_HOURS", "")     # 只准引用,不准自带一个阈值


# ── 在途卡住的三种病要分得开(2026-09-11 生产实数据:20 条在途、6162 个 SKU)

def test_verdict_judges_terminal_heads_and_never_trusts_a_non_terminal_one():
    """汇总终态:残留 / 下轮收工,当场分得开。汇总**未终态:不拿它的计数下结论**。

    2026-09-22 生产实证(所有者 09-23 核实):A131吕灿荣 改价 feed 汇总 31 小时
    停在 INPROGRESS / 成功 0 / 失败 0 / 处理中 71,明细却 71/71 SUCCESS、价格
    早已生效。此前这里按汇总计数判「沃尔玛确实还在跑(全部待处理)」,正好把
    人引向了"沃尔玛一条都没处理"的错误结论。
    """
    from workflows import feed_poll

    done_with_residue = {"feedStatus": "PROCESSED", "itemsReceived": 300,
                         "itemsSucceeded": 298, "itemsFailed": 1}
    assert "残留" in feed_poll._verdict(done_with_residue, 1)
    assert "下轮轮询即收工" in feed_poll._verdict(done_with_residue, 0)

    frozen_head = {"feedStatus": "INPROGRESS", "itemsReceived": 71,
                   "itemsSucceeded": 0, "itemsFailed": 0, "itemsProcessing": 71}
    partial_head = {"feedStatus": "INPROGRESS", "itemsReceived": 15,
                    "itemsSucceeded": 12, "itemsFailed": 2}
    for head, n_open in ((frozen_head, 71), (partial_head, 15)):
        got = feed_poll._verdict(head, n_open)
        assert "还在跑" not in got and "等就行" not in got       # 不再替汇总背书
        assert "以明细为准" in got and "上一行命令" in got        # 指到能看到真相的地方
        assert "落定期限" in got                                  # 到期读明细强制落定
    assert "落定期限 15 分钟" in feed_poll._verdict(frozen_head, 71, "price")


def test_verdict_reads_the_head_itself_not_our_own_formatted_line(monkeypatch):
    """判档吃 head **原件**,不去反解 `_progress` 拼好的那句话。

    反解自己刚拼的字符串 = 给同一份数字造第二个出处,改一处忘一处就静默错档
    (而错档的表现是"沃尔玛还在跑",人就真的等下去了)。把 `_progress` 砸了还
    照样判得对,才算真没走那条路。
    """
    from workflows import feed_poll

    def _boom(_head):
        raise AssertionError("_verdict 不该经过 _progress")

    monkeypatch.setattr(feed_track, "_progress", _boom)
    assert "残留" in feed_poll._verdict(
        {"feedStatus": "PROCESSED", "itemsReceived": 15,
         "itemsSucceeded": 12, "itemsFailed": 2}, 1)
    assert "以明细为准" in feed_poll._verdict(
        {"feedStatus": "INPROGRESS", "itemsReceived": 15,
         "itemsSucceeded": 12, "itemsFailed": 2}, 15)


def test_stuck_probe_puts_walmart_next_to_the_ledger(monkeypatch):
    """`-p probe=1`:feed 级 GET(零明细翻页)的结果与台账并排,当场分档;
    问不到的(凭证缺失 / HTTP 错)把原话摆出来,不装成"还在跑"。"""
    import contextlib
    from datetime import datetime, timedelta, timezone

    from registry import db
    from workflows import feed_poll

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        {"status": "submitted", "feed_id": "F_OLD", "store": "A085朱丽霖",
         "feed_type": "price", "workflow": "maintenance", "created_at": now,
         "updated_at": now - timedelta(hours=412.5)},
        {"status": "submitted", "feed_id": "F_DEAD", "store": "谭总9",
         "feed_type": "MP_ITEM", "workflow": "list_new", "created_at": now,
         "updated_at": now - timedelta(hours=5)},
    ])

    class _C(list):
        def cursor(self):
            return self

        def execute(self, sql, args=None):
            self.append((sql, args))

        def fetchall(self):
            return [("F_OLD", 15), ("F_DEAD", 40)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    conn = _C()
    monkeypatch.setattr(db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([conn])))

    def _head(store, fid):
        if fid == "F_DEAD":
            raise RuntimeError("feed 状态查询失败 HTTP 404")
        return {"feedStatus": "INPROGRESS", "itemsReceived": 15,
                "itemsSucceeded": 12, "itemsFailed": 2}

    monkeypatch.setattr(feeds, "get_feed_status", _head)
    out = feed_poll._inflight_list({"A085朱丽霖": {"name": "A085朱丽霖"},
                                    "谭总9": {"name": "谭总9"}})
    assert "台账里共 55 个 SKU 卡在「处理中」" in out      # 首行 = 卡住多少货
    assert "台账未落定 15" in out                          # 逐条也有
    assert "沃尔玛:INPROGRESS,已收 15,成功 12,失败 2,待处理 1" in out
    assert "以明细为准" in out                             # 汇总未终态不替它下结论
    assert "沃尔玛:查询失败(feed 状态查询失败 HTTP 404)" in out
    sql = next(s for s, _ in conn if "feed_items" in s)
    assert "status = 'submitted'" in sql                   # 未落定 = 台账仍 submitted


def test_stuck_without_probe_asks_walmart_nothing(monkeypatch):
    """不带 `-p probe=1` 的清单一个沃尔玛接口都不调 —— 凭证缺失也跑得动,
    而人要的只是"到底是哪几条、码是什么"。"""
    import contextlib
    from datetime import datetime, timezone

    from registry import db
    from workflows import feed_poll

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        {"status": "submitted", "feed_id": "F1", "store": "T1",
         "feed_type": "MP_ITEM", "workflow": "list_new",
         "created_at": now, "updated_at": now}])

    class _C(list):
        def cursor(self):
            return self

        def execute(self, sql, args=None):
            self.append(sql)

        def fetchall(self):
            return [("F1", 7)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([_C()])))

    def _boom(*a, **k):
        raise AssertionError("不带 probe 不许调沃尔玛")

    monkeypatch.setattr(feeds, "get_feed_status", _boom)
    out = feed_poll._inflight_list()
    assert "F1" in out and "台账未落定 7" in out and "沃尔玛:" not in out


# ── 落定期限(所有者 2026-09-25 定稿)──────────────────────────────────────────
# 「提交 feed,追踪 feed 直至该 feed 的最长期限……达到最长期限还没有完全落定的,
# 就查询明细来落定」「期限按官方值、不加余量」。取代 09-24 的汇总停更闸:那一类
# (A131吕灿荣 改价 feed 汇总 31 小时停在 INPROGRESS / 0 / 0 / 71、明细 71/71
# SUCCESS)现在按改价期限 15 分钟到期读明细收口。

_STALE_HEAD = {"feedStatus": "INPROGRESS", "itemsReceived": 3,
               "itemsSucceeded": 0, "itemsFailed": 0, "itemsProcessing": 3}


class _Ledger(_Conn):
    """台账里这个 feed 的行(sku, workflow, feed_type, status)按构造参数给出。"""

    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.rolled_back = False

    def fetchall(self):
        if "SELECT sku, workflow, feed_type, status" in self._last:
            return self.rows
        return []

    def rollback(self):
        self.rolled_back = True


def _feed(monkeypatch, ledger_rows, items, head=None):
    conn = _Ledger(ledger_rows)
    _fake_db(monkeypatch, conn)
    done = []
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda s, f: dict(head or _STALE_HEAD))
    monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter(items))
    monkeypatch.setattr(feeds, "mark_feed_done",
                        lambda fid, ok: done.append((fid, ok)))
    return conn, done


def _landed(conn):
    """本轮逐条落账的 (status, sku, raw_status, settled_by)。"""
    got = [x for x in conn.sqls if "SET status = %s" in x[0]]
    return sorted((r[0], r[6], r[3], r[4]) for _sql, rows in got for r in rows)


def _absent_sweep(conn):
    """明细里查无那一扫:(落成的状态, 排除名单)。"""
    sql, args = next(x for x in conn.sqls if "NOT (sku = ANY(%s))" in x[0])
    status = "missing" if "'missing'" in sql else "overdue"
    return status, sorted(args[1]), sql


def test_deadline_table_is_the_official_values_without_margin():
    """所有者 2026-09-25:「期限按官方值、不加余量……不猜测,不凭记忆回答」。
    每个值都钉住,改任何一个都要先改 refdata/walmart_slas.tsv 的官方原句。"""
    assert feed_track.FEED_DEADLINE_MINUTES == {
        "price": 15, "PRICE_AND_PROMOTION": 15,          # SLA 15 分钟
        "inventory": 240, "MP_INVENTORY": 240,           # 最长 4 小时
        "MP_ITEM": 1440, "MP_MAINTENANCE": 1440,         # 合规审核最长 24 小时
        "MP_ITEM_MATCH": 1440,                           # 美国站最长 24 小时
        "RETIRE_ITEM": 2880,                             # 最长 48 小时
        "DELETE_ITEM": 4320,                             # 最长 72 小时
    }
    assert feed_track.UNREADABLE_GRACE_HOURS == 24       # 所有者 09-25 批的宽限


def test_every_submittable_feed_type_has_a_deadline():
    """api/feeds 能发的每个 feedType 都有期限 —— 漏一个,那类 feed 就只能等汇总
    终态,回到"永不老化"的老路(摘要会点名,但不该发生)。"""
    assert set(feeds._SLICE_LIMITS) <= set(feed_track.FEED_DEADLINE_MINUTES)


def test_the_official_quotes_behind_every_deadline_are_on_file():
    """期限的依据是页面原句(2026-09-25 逐页重核),不是记忆:tsv 里必须有。"""
    import pathlib
    tsv = (pathlib.Path(__file__).resolve().parents[1] / "refdata"
           / "walmart_slas.tsv").read_text(encoding="utf-8")
    for quote in (
            "the bulk price update has an service level agreement (SLA) of 15 minutes",
            "The bulk price update Service Level Agreement (SLA) is 15 minutes.",
            "Updates can appear in as quickly as 15 minutes or may take up to four hours.",
            "A bulk item submission (create or update) takes up to four hours to process.",
            "The review may take up to 24 hours.",
            "Updates may take up to 24 hours.",
            "the catalog update itself can take up to 48 hours",
            "It may take up to 72 hours before your items are removed from your Catalog.",
            "The feedId does not exist or is not visible to your account."):
        assert quote in tsv, quote
    # 跟卖「无法导入时最长 72 小时」美国站已无:登记为不采用,不许悄悄又用回去
    assert "不采用(加拿大站口径,不适用美国店)" in tsv


def test_deadline_helpers():
    assert feed_track.deadline_hours("price") == 0.25
    assert feed_track.past_deadline("price", 0.24) is False
    assert feed_track.past_deadline("price", 0.25) is True
    assert feed_track.past_deadline("price", None) is False     # 年龄未知 ⇒ 未到期
    assert feed_track.past_deadline(None, 99.0) is False        # 不知道类型 ⇒ 未到期
    assert feed_track.past_grace("DELETE_ITEM", 95.9) is False  # 72 + 24
    assert feed_track.past_grace("DELETE_ITEM", 96.0) is True
    assert feed_track.deadline_text("price") == "15 分钟"
    assert feed_track.deadline_text("DELETE_ITEM") == "72 小时"
    with pytest.raises(KeyError):
        feed_track.deadline_hours("NO_SUCH_FEED")               # 宁炸不吞


def test_a_stale_price_feed_past_its_deadline_settles_by_the_details(monkeypatch):
    """汇总停在 INPROGRESS、改价期限(15 分钟)已过、明细全终态 ⇒ 逐 SKU 落账 +
    收口(落 done)。这就是 09-22 那 71 条:沃尔玛早判完了,只是汇总没收口。"""
    conn, done = _feed(
        monkeypatch,
        [(s, "maintenance", "price", "submitted") for s in ("A", "B", "C")],
        [{"sku": s, "ingestionStatus": "SUCCESS"} for s in ("A", "B", "C")])
    head, out = feed_track.poll_feed(STORE, "F1", age_h=31.0, feed_type="price")
    assert head["feedStatus"] == "INPROGRESS"
    assert out == {s: ("success", "") for s in ("A", "B", "C")}
    assert _landed(conn) == [("success", s, "SUCCESS", "deadline")
                             for s in ("A", "B", "C")]
    assert done == [("F1", True)]
    assert feed_track.unresolved(out) == (0, 0)


def test_at_the_deadline_everything_left_is_forced_to_a_final_word(monkeypatch):
    """到期不管汇总怎么说都读明细**强制落定**:INPROGRESS ⇒ overdue,不认识的
    状态值 ⇒ unrecognized(原值照存),汇总没收工而明细里查无 ⇒ overdue
    (raw_status 记「明细缺席」)。收口后一条在途都不留。"""
    conn, done = _feed(
        monkeypatch,
        [(s, "list_new", "MP_ITEM", "submitted") for s in "ABCDE"],
        [{"sku": "A", "ingestionStatus": "SUCCESS"},
         {"sku": "B", "ingestionStatus": "DATA_ERROR",
          "ingestionErrors": {"ingestionError": [{"code": "E1"}]}},
         {"sku": "C", "ingestionStatus": "INPROGRESS",
          "pendingStatusDescription": "This item is currently under review for "
                                      "compliance. This process may take up to 24 hours."},
         {"sku": "D", "ingestionStatus": "BRAND_NEW_ENUM"}])      # E:明细里查无
    _head, out = feed_track.poll_feed(STORE, "F1", age_h=24.0, feed_type="MP_ITEM")
    assert _landed(conn) == [
        ("failed", "B", "DATA_ERROR", "deadline"),
        ("overdue", "C", "INPROGRESS", "deadline"),
        ("success", "A", "SUCCESS", "deadline"),
        ("unrecognized", "D", "BRAND_NEW_ENUM", "deadline")]
    status, excluded, sql = _absent_sweep(conn)
    assert status == "overdue" and excluded == ["A", "B", "C", "D"]
    assert "明细缺席" in sql and "settled_by = 'deadline'" in sql
    assert out["C"] == ("overdue", "") and out["D"] == ("unrecognized", "")
    assert out["E"] == ("overdue", "")
    assert feed_track.unresolved(out) == (0, 0)
    assert done == [("F1", True)]
    # 合规审核的原话进 error_desc:到期没结论的,人看得到"为什么还在跑"
    rows = next(r for x, r in conn.sqls if "SET status = %s" in x)
    assert any(r[6] == "C" and "under review for compliance" in (r[2] or "")
               for r in rows)


def test_a_terminal_head_before_the_deadline_keeps_the_residue_open(monkeypatch):
    """汇总终态但未到期:有结论的落,明细里查无的落 missing,还在跑的**留在途**
    (官方:PROCESSED 之后单条仍可能在复核),不收口。"""
    conn, done = _feed(
        monkeypatch,
        [(s, "list_new", "MP_ITEM", "submitted") for s in "ABCD"],
        [{"sku": "A", "ingestionStatus": "SUCCESS"},
         {"sku": "B", "ingestionStatus": "INPROGRESS"},
         {"sku": "C", "ingestionStatus": "BRAND_NEW_ENUM"}],      # D:明细里查无
        head={"feedStatus": "PROCESSED"})
    _head, out = feed_track.poll_feed(STORE, "F1", age_h=3.0, feed_type="MP_ITEM")
    assert _landed(conn) == [("success", "A", "SUCCESS", "head")]
    status, excluded, _sql = _absent_sweep(conn)
    assert status == "missing" and excluded == ["A", "B", "C"]
    assert out["B"] == ("processing", "") and out["C"] == ("unknown", "")
    assert out["D"] == ("missing", "")
    assert feed_track.unresolved(out) == (2, 1)
    assert done == []


def test_a_terminal_head_past_the_deadline_still_calls_absent_rows_missing(
        monkeypatch):
    """汇总终态 + 已到期:明细里查无仍是 missing(沃尔玛收工了、没有这一条),
    只有还在跑的才落 overdue。"""
    conn, done = _feed(
        monkeypatch,
        [(s, "product_clear", "DELETE_ITEM", "submitted") for s in "AB"],
        [{"sku": "A", "ingestionStatus": "INPROGRESS"}],
        head={"feedStatus": "PROCESSED"})
    _head, out = feed_track.poll_feed(STORE, "F1", age_h=80.0,
                                      feed_type="DELETE_ITEM")
    assert _landed(conn) == [("overdue", "A", "INPROGRESS", "deadline")]
    assert _absent_sweep(conn)[0] == "missing"
    assert out == {"A": ("overdue", ""), "B": ("missing", "")}
    assert done == [("F1", True)]


def test_first_settlement_is_final_and_never_rewritten(monkeypatch):
    """落过的行不重写:只改仍 submitted 的行。重写会把 resolved_at 刷成当下,
    problem_scan 的在途闸「success 且 resolved_at > last_seen_at ⇒ 待观测」就
    一直成立(2026-09-24 实见:终态残留 feed 每轮重读,约 970 个 SKU 长期"待观测")。
    判据写在 SQL 上(`AND status = 'submitted'`),与同时轮询的业务工作流也不打架。"""
    conn, _done = _feed(
        monkeypatch,
        [("A", "maintenance", "price", "success"),
         ("B", "maintenance", "price", "submitted")],
        [{"sku": "A", "ingestionStatus": "SUCCESS"},
         {"sku": "B", "ingestionStatus": "SUCCESS"}],
        head={"feedStatus": "PROCESSED"})
    feed_track.poll_feed(STORE, "F1")
    sql = next(x for x, _ in conn.sqls if "SET status = %s" in x)
    assert sql.rstrip().endswith("AND status = 'submitted'")
    sweep = next(x for x, _ in conn.sqls if "NOT (sku = ANY(%s))" in x)
    assert "AND status = 'submitted'" in sweep


def test_repolled_residue_does_not_replay_events_or_blacklist(monkeypatch):
    """终态残留的 feed 到期前每轮重读:上一轮已落定的行不再记回执事件。"""
    conn, _done = _feed(
        monkeypatch,
        [("A", "wf", "DELETE_ITEM", "success"), ("B", "wf", "DELETE_ITEM", "submitted")],
        [{"sku": "A", "ingestionStatus": "SUCCESS"},
         {"sku": "B", "ingestionStatus": "SUCCESS"}],
        head={"feedStatus": "PROCESSED"})
    recorded = []
    monkeypatch.setattr(feed_track.product_events, "record_many",
                        lambda c, rows: (recorded.extend(rows), len(rows))[1])
    feed_track.poll_feed(STORE, "F1")
    assert [e["sku"] for e in recorded] == ["B"]


def test_a_non_terminal_head_before_the_deadline_reads_no_details(monkeypatch):
    """期限前、汇总未终态:结果 None、一页明细都不翻 —— 刚提交的 feed 本来就该等;
    业务工作流就地的即时轮询不传年龄与类型,只认汇总终态。"""
    def _boom(*a, **k):
        raise AssertionError("期限前不许翻明细")

    monkeypatch.setattr(feeds, "get_feed_status", lambda s, f: dict(_STALE_HEAD))
    monkeypatch.setattr(feeds, "iter_feed_items", _boom)
    assert feed_track.poll_feed(STORE, "F1")[1] is None
    assert feed_track.poll_feed(STORE, "F1", age_h=None, feed_type="price")[1] is None
    assert feed_track.poll_feed(STORE, "F1", age_h=0.2, feed_type="price")[1] is None
    assert feed_track.poll_feed(STORE, "F1", age_h=99.0)[1] is None   # 没给类型


def test_a_due_feed_without_ledger_rows_still_closes(monkeypatch):
    """台账里这个 feed 一行都没有、已到期:没有可落的对象,但 feed_log 照样收口
    —— 否则它永远在途、每轮复查(无依据地挂着正是这次要消灭的)。"""
    conn, done = _feed(monkeypatch, [], [{"sku": "A", "ingestionStatus": "SUCCESS"}])
    _head, out = feed_track.poll_feed(STORE, "F1", age_h=31.0, feed_type="price")
    assert out == {"A": ("success", "")}
    assert done == [("F1", True)]


def test_dry_run_computes_everything_and_rolls_the_ledger_back(monkeypatch):
    """`execute=False`:判据照算、照返回,落账同事务 rollback,feed_log 不收口。"""
    conn, done = _feed(
        monkeypatch,
        [(s, "maintenance", "price", "submitted") for s in ("A", "B")],
        [{"sku": "A", "ingestionStatus": "SUCCESS"},
         {"sku": "B", "ingestionStatus": "INPROGRESS"}])
    _head, out = feed_track.poll_feed(STORE, "F1", age_h=1.0, feed_type="price",
                                      execute=False)
    assert out == {"A": ("success", ""), "B": ("overdue", "")}
    assert conn.rolled_back is True and done == []


def test_settle_unreadable_lands_only_open_rows_and_closes_the_feed(monkeypatch):
    conn = _Ledger([])
    _fake_db(monkeypatch, conn)
    done = []
    monkeypatch.setattr(feeds, "mark_feed_done", lambda fid, ok: done.append((fid, ok)))
    n = feed_track.settle_unreadable("F1", "沃尔玛404", "feed 状态查询返回 404(feedId=F1)")
    sql, args = conn.sqls[0]
    assert "status = 'unreadable'" in sql and "AND status = 'submitted'" in sql
    assert "settled_by = 'unreadable'" in sql
    assert args == ("沃尔玛404", "feed 状态查询返回 404(feedId=F1)", "F1")
    assert n == 1 and done == [("F1", True)]
    conn2 = _Ledger([])
    _fake_db(monkeypatch, conn2)
    feed_track.settle_unreadable("F2", "代理波动", "x", execute=False)
    assert conn2.rolled_back is True and done == [("F1", True)]


def _query_error(status):
    return feeds.FeedQueryError(f"feed 状态查询返回 {status}(feedId=F1)", status)


def _poll_raising(err):
    def _poll(store, fid, **_):
        raise err
    return _poll


def test_a_404_past_the_deadline_is_unreadable_at_once(monkeypatch):
    """所有者 09-25:到期后 404 直接落「无法查询」(官方:feedId 不存在或不可见)。"""
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        _inflight(fid="F1", ft="price", wf="maintenance", age_h=0.5)])
    monkeypatch.setattr(feed_track, "poll_feed", _poll_raising(_query_error(404)))
    seen = []
    monkeypatch.setattr(feed_track, "settle_unreadable",
                        lambda fid, cls, why, execute=True: (seen.append(
                            (fid, cls, execute)), 7)[1])
    out = feed_track.poll_all({"T1": STORE})
    assert seen == [("F1", "沃尔玛404", True)]
    assert "落定 1(其中无法查询 1)" in out.splitlines()[0]
    assert "无法查询(沃尔玛404)" in out and "7 个 SKU 落「无法查询」" in out


def test_a_404_before_the_deadline_is_just_retried(monkeypatch):
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        _inflight(fid="F1", ft="MP_ITEM", age_h=1.0)])          # 期限 24 小时
    monkeypatch.setattr(feed_track, "poll_feed", _poll_raising(_query_error(404)))
    monkeypatch.setattr(feed_track, "settle_unreadable",
                        lambda *a, **k: pytest.fail("期限前不许落无法查询"))
    out = feed_track.poll_all({"T1": STORE})
    assert "查询失败" in out and "下轮再试" in out and "落定 0" in out


def test_other_read_errors_wait_out_the_grace_after_the_deadline(monkeypatch):
    """其他读取失败:期限后再宽限 24 小时,仍读不到才落「无法查询」(归类走
    store_retry.diagnose)。宽限内只说"下轮再试",并点明还要等多久。"""
    seen = []
    monkeypatch.setattr(feed_track, "settle_unreadable",
                        lambda fid, cls, why, execute=True: (seen.append(
                            (fid, cls)), 3)[1])
    monkeypatch.setattr(feed_track, "poll_feed", _poll_raising(_query_error(503)))
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        _inflight(fid="F1", ft="price", age_h=1.0)])             # 期限后、宽限内
    out = feed_track.poll_all({"T1": STORE})
    assert seen == [] and "已过落定期限 15 分钟,读不到满 24h 落「无法查询」" in out
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        _inflight(fid="F1", ft="price", age_h=24.3)])            # 15 分钟 + 24h 之后
    feed_track.poll_all({"T1": STORE})
    assert seen == [("F1", "沃尔玛503")]


def test_an_unloadable_store_goes_unreadable_only_after_the_grace(monkeypatch):
    seen = []
    monkeypatch.setattr(feed_track, "settle_unreadable",
                        lambda fid, cls, why, execute=True: (seen.append(
                            (fid, cls)), 2)[1])
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        _inflight(store="GONE", fid="F1", ft="RETIRE_ITEM", age_h=50.0)])
    out = feed_track.poll_all({"T1": STORE})
    assert seen == [] and "店铺凭证缺失跳过 1" in out
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        _inflight(store="GONE", fid="F1", ft="RETIRE_ITEM", age_h=72.0)])
    feed_track.poll_all({"T1": STORE})
    assert seen == [("F1", "店铺不可调用")]


def test_poll_all_hands_each_feed_its_age_type_and_the_dry_run_switch(monkeypatch):
    """poll_all 必须把在途年龄、feedType、execute 递给 poll_feed:少一样,期限
    收口就永远不开,或者空跑照样落账。"""
    seen = {}

    def fake_poll(store, fid, **k):
        seen[fid] = k
        return {"feedStatus": "PROCESSED"}, {"A": ("success", "")}

    monkeypatch.setattr(feeds, "query_pending", lambda: [
        _inflight(fid="F1", ft="price", age_h=31.0), _inflight(fid="F2")])
    monkeypatch.setattr(feed_track, "poll_feed", fake_poll)
    out = feed_track.poll_all({"T1": STORE}, execute=False)
    assert 30.9 < seen["F1"]["age_h"] < 31.1 and seen["F2"]["age_h"] is None
    assert seen["F1"]["feed_type"] == "price" and seen["F2"]["feed_type"] == "DELETE_ITEM"
    assert seen["F1"]["execute"] is False
    assert out.startswith("[DRY-RUN] feed 轮询:")


def test_summary_names_a_feed_closed_at_its_deadline(monkeypatch):
    """到期收口的 feed:明细行把「汇总处理中 / 明细有结论」这对矛盾原样摆出来
    (那就是沃尔玛汇总停更的证据),首行点个数 —— 例外计数,0 则整段消失。"""
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        _inflight(fid="F1", ft="price", wf="maintenance", age_h=31.0)])
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f, **_: (
        dict(_STALE_HEAD), {"A": ("success", ""), "B": ("success", ""),
                            "C": ("overdue", "")}))
    out = feed_track.poll_all({"T1": STORE})
    first = out.splitlines()[0]
    assert "落定 1(其中到期收口 1),仍处理中 0" in first
    assert ("T1 改价(maintenance) F1:到期收口(落定期限 15 分钟,沃尔玛汇总仍停在 "
            "INPROGRESS:已收 3,成功 0,失败 0,待处理 3):成功 2,失败 0,"
            "超期未完成 1") in out
    assert "长期在途" not in out                  # 落定的永远出明细行,不折叠


def test_summary_for_a_terminal_head_with_residue_before_the_deadline(monkeypatch):
    """汇总终态、未到期、还有 SKU 在跑:不许说"已落定",说清最迟什么时候强制落定。"""
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        _inflight(fid="F1", ft="MP_ITEM", wf="list_new", age_h=1.5)])
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f, **_: (
        {"feedStatus": "PROCESSED"},
        {"A": ("success", ""), "B": ("failed", "E1"), "C": ("processing", "")}))
    out = feed_track.poll_all({"T1": STORE})
    assert "已落定" not in out and "到期收口" not in out
    assert "落定 0,仍处理中 1" in out
    assert ("T1 上架(list_new) F1:PROCESSED 已终态,但 1 个 SKU 仍在处理,留在途,"
            "最迟到期(24 小时)按明细强制落定") in out


def test_no_verdict_statuses_are_one_vocabulary():
    """"沃尔玛没给结论"的三个词只有一个出处,中文面也各有一个词。"""
    assert feed_track.NO_VERDICT_STATUSES == ("overdue", "unrecognized", "unreadable")
    assert [feed_track.RESULT_TEXT[s] for s in feed_track.NO_VERDICT_STATUSES] == [
        "超期未完成", "未知状态", "无法查询"]


# ── 真库:落账 SQL 的语义(假连接只证明得了文本)──────────────────────────────────
# ⚠ 地址是**测试夹具**(非标准端口 55432,不可能连到生产库);poll_feed 自己开连接、
# 自己提交,所以本组用专用的 feed_id / 店铺名,前后各清一次。
import os as _os
import socket as _socket

_PG_DSN = _os.environ.get(
    "WALMART_TEST_PG_DSN", "host=127.0.0.1 port=55432 user=postgres dbname=walmart_data")


def _pg_up() -> bool:
    try:
        with _socket.create_connection(("127.0.0.1", 55432), timeout=1):
            return True
    except OSError:
        return False


needs_pg = pytest.mark.skipif(not _pg_up(), reason="沙箱 PG 127.0.0.1:55432 未启动")

_PG_FID, _PG_STORE = "FT_DEADLINE_SANDBOX", "FT_SANDBOX_STORE"


def _pg_reset(db):
    with db.pg_conn() as conn:
        for t in ("ops.feed_items", "ops.feed_item_errors", "ops.feed_log"):
            conn.execute(f"DELETE FROM {t} WHERE feed_id = %s", (_PG_FID,))


def _pg_seed(db, rows):
    _pg_reset(db)
    with db.pg_conn() as conn:
        for sku, st in rows:
            conn.execute(
                "INSERT INTO ops.feed_items (feed_id, sku, workflow, store, feed_type,"
                " status, resolved_at) VALUES (%s, %s, 'maintenance', %s, 'price', %s,"
                " CASE WHEN %s = 'submitted' THEN NULL"
                "      ELSE now() - interval '5 days' END)",
                (_PG_FID, sku, _PG_STORE, st, st))
        conn.execute(
            "INSERT INTO ops.feed_log (workflow, store, feed_type, payload_key,"
            " feed_id, status) VALUES ('maintenance', %s, 'price', 'k-sandbox', %s,"
            " 'submitted')", (_PG_STORE, _PG_FID))


def _pg_rows(db):
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT sku, status, raw_status, settled_by,"
                    " resolved_at > now() - interval '1 minute'"
                    " FROM ops.feed_items WHERE feed_id = %s ORDER BY sku", (_PG_FID,))
        rows = cur.fetchall()
        cur.execute("SELECT status FROM ops.feed_log WHERE feed_id = %s", (_PG_FID,))
        return rows, cur.fetchone()[0]


@needs_pg
def test_deadline_landing_on_a_real_database(monkeypatch):
    """真库一轮:改价 feed 汇总停在 INPROGRESS、已过 15 分钟期限 ⇒ 明细有结论的照落,
    还在跑的与明细里查无的落 overdue(依据分得开),**早已落定的那行一个字不动**
    (resolved_at 不刷新),feed_log 收口 done。"""
    monkeypatch.setenv("WALMART_PG_DSN", _PG_DSN)
    from registry import db
    _pg_seed(db, [("A", "submitted"), ("B", "submitted"), ("C", "submitted"),
                  ("D", "submitted"), ("E", "success")])
    try:
        monkeypatch.setattr(feeds, "get_feed_status", lambda s, f: dict(_STALE_HEAD))
        monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([
            {"sku": "A", "ingestionStatus": "SUCCESS"},
            {"sku": "B", "ingestionStatus": "DATA_ERROR",
             "ingestionErrors": {"ingestionError": [
                 {"code": "E1", "field": "price", "description": "bad price"}]}},
            {"sku": "C", "ingestionStatus": "INPROGRESS"},
            {"sku": "E", "ingestionStatus": "DATA_ERROR"}]))     # D 查无;E 早已落定
        store = dict(STORE, name=_PG_STORE)
        _head, out = feed_track.poll_feed(store, _PG_FID, age_h=1.0, feed_type="price")
        rows, log = _pg_rows(db)
        assert rows == [("A", "success", "SUCCESS", "deadline", True),
                        ("B", "failed", "DATA_ERROR", "deadline", True),
                        ("C", "overdue", "INPROGRESS", "deadline", True),
                        ("D", "overdue", "明细缺席", "deadline", True),
                        ("E", "success", None, None, False)]    # 首次落定即定稿
        assert log == "done"
        assert out["D"] == ("overdue", "") and feed_track.unresolved(out) == (0, 0)
    finally:
        _pg_reset(db)


@needs_pg
def test_dry_run_and_unreadable_on_a_real_database(monkeypatch):
    """空跑:判据照算,库里一行不变、feed_log 不收口;到期后读不到:只改仍
    submitted 的行,落 unreadable + 归类,feed_log 收口。"""
    monkeypatch.setenv("WALMART_PG_DSN", _PG_DSN)
    from registry import db
    _pg_seed(db, [("A", "submitted"), ("B", "failed")])
    try:
        monkeypatch.setattr(feeds, "get_feed_status", lambda s, f: dict(_STALE_HEAD))
        monkeypatch.setattr(feeds, "iter_feed_items", lambda s, f: iter([
            {"sku": "A", "ingestionStatus": "SUCCESS"}]))
        store = dict(STORE, name=_PG_STORE)
        _head, out = feed_track.poll_feed(store, _PG_FID, age_h=1.0,
                                          feed_type="price", execute=False)
        assert out["A"] == ("success", "")
        rows, log = _pg_rows(db)
        assert rows[0][:2] == ("A", "submitted") and log == "submitted"
        n = feed_track.settle_unreadable(_PG_FID, "沃尔玛404", "feed 状态查询返回 404")
        rows, log = _pg_rows(db)
        assert n == 1 and log == "done"
        assert rows == [("A", "unreadable", "沃尔玛404", "unreadable", True),
                        ("B", "failed", None, None, False)]
    finally:
        _pg_reset(db)


def test_a_store_filtered_run_never_judges_other_stores(monkeypatch):
    """`feed_poll -p store=X` 只加载 X:其他店的老 feed 不许因为"不在 stores_by_name
    里"被当成店铺不可调用、过了宽限就落「无法查询」。"""
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        _inflight(store="T1", fid="F1", ft="price", age_h=0.1),
        _inflight(store="OTHER", fid="F2", ft="RETIRE_ITEM", age_h=500.0)])
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f, **_: (
        {"feedStatus": "INPROGRESS"}, None))
    monkeypatch.setattr(feed_track, "settle_unreadable",
                        lambda *a, **k: pytest.fail("别的店的 feed 不许被判掉"))
    out = feed_track.poll_all({"T1": STORE}, only="T1")
    assert out.startswith("feed 轮询:1 个在途") and "F2" not in out


# ── pending 对账(所有者 2026-09-25 批:「pending 按你的建议做」)──────────────────
# pending ≠ 没提交上:POST 可能已到沃尔玛、只是没拿到 feedId。每轮只读反查,查到收编、
# 到期查不到落 failed「提交未确认」、确定没发出落 failed「未发出」,**绝不补交**。

def _pending(age_h=1.0, posted=True, item_count=2, ft="DELETE_ITEM", wf="product_clear",
             store="T1", recon=0):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    at = now - timedelta(hours=age_h)
    return {"id": 7, "status": "pending", "feed_id": None, "store": store,
            "feed_type": ft, "workflow": wf, "created_at": at, "updated_at": at,
            "item_count": item_count, "skus": ["A", "B"] if item_count else None,
            "post_started_at": at if posted else None, "recon_count": recon}


def _recon_wired(monkeypatch, verdict=("NOT_FOUND", None), held=False):
    calls = {"close": [], "adopt": [], "note": [], "lookup": []}
    monkeypatch.setattr(feeds, "close_pending",
                        lambda lid, basis: (calls["close"].append(basis), True)[1])
    monkeypatch.setattr(feeds, "adopt_pending",
                        lambda row, fid, at: (calls["adopt"].append((fid, at)), True)[1])
    monkeypatch.setattr(feeds, "note_reconcile", lambda lid: calls["note"].append(lid))

    def _lookup(store, ft, n, **kw):
        calls["lookup"].append((ft, n, kw))
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    monkeypatch.setattr(feeds, "find_recent_feed", _lookup)
    monkeypatch.setattr(feed_track.runlock, "is_held", lambda name: held)
    return calls


def test_found_is_adopted_with_the_send_time(monkeypatch):
    calls = _recon_wired(monkeypatch, verdict=("FOUND", {"feedId": "F_OURS"}))
    p = _pending(age_h=1.0)
    (rec,) = feed_track.reconcile_pending([p], {"T1": STORE})
    assert rec["state"] == "adopted" and "收编 feedId=F_OURS" in rec["detail"]
    assert calls["adopt"] == [("F_OURS", p["post_started_at"])]
    ft, n, kw = calls["lookup"][0]
    assert (ft, n) == ("DELETE_ITEM", 2)
    assert kw == {"since": p["post_started_at"], "expect_skus": ["A", "B"],
                  "recheck": False}                      # 按发送时刻、核 SKU 集合
    assert calls["note"] == [7]


def test_not_found_waits_until_the_deadline_then_closes(monkeypatch):
    calls = _recon_wired(monkeypatch)
    (rec,) = feed_track.reconcile_pending([_pending(age_h=10.0)], {"T1": STORE})
    assert rec["state"] == "open" and "落定期限 72 小时 前每轮再查" in rec["detail"]
    assert calls["close"] == []
    (rec,) = feed_track.reconcile_pending([_pending(age_h=73.0, recon=5)], {"T1": STORE})
    assert rec["state"] == "closed"
    assert calls["close"][0].startswith("提交未确认:落定期限 72 小时 内反查 6 次")


def test_an_unknown_lookup_waits_out_the_grace(monkeypatch):
    calls = _recon_wired(monkeypatch, verdict=("UNKNOWN", None))
    (rec,) = feed_track.reconcile_pending([_pending(age_h=80.0)], {"T1": STORE})
    assert rec["state"] == "open" and calls["close"] == []          # 72 + 24 之内
    (rec,) = feed_track.reconcile_pending([_pending(age_h=97.0)], {"T1": STORE})
    assert rec["state"] == "closed" and "反查一直没有结论" in calls["close"][0]


def test_an_undecided_lookup_names_its_candidates(monkeypatch):
    """候选核不清(不止一条对得上 / 明细还核对不了)⇒ 不收编;依据里点名候选。"""
    calls = _recon_wired(monkeypatch, verdict=(
        "UNKNOWN", {"matched": ["F1", "F2"], "unverified": ["F3"]}))
    (rec,) = feed_track.reconcile_pending([_pending(age_h=10.0)], {"T1": STORE})
    assert rec["state"] == "open" and calls["adopt"] == []
    assert "2 条候选条数与 SKU 集合都对得上(F1、F2),分不清是哪一笔" in rec["detail"]
    assert "另有 1 条候选明细还核对不了(F3)" in rec["detail"]
    (rec,) = feed_track.reconcile_pending([_pending(age_h=97.0)], {"T1": STORE})
    assert rec["state"] == "closed" and "分不清是哪一笔" in calls["close"][0]
    calls = _recon_wired(monkeypatch, verdict=(
        "UNKNOWN", {"matched": ["F1"], "unverified": ["F3"]}))
    (rec,) = feed_track.reconcile_pending([_pending(age_h=10.0)], {"T1": STORE})
    assert "F1 对得上,但另有 1 条候选明细还核对不了(F3),核清之前不收编" in rec["detail"]
    calls = _recon_wired(monkeypatch, verdict=("UNKNOWN", None))
    (rec,) = feed_track.reconcile_pending([_pending(age_h=10.0)], {"T1": STORE})
    assert "feed 列表读取失败" in rec["detail"]


def test_a_lookup_that_raises_is_unknown_with_its_class(monkeypatch):
    calls = _recon_wired(monkeypatch, verdict=feeds.FeedQueryError(
        "feed 状态查询返回 503(feedId=x)", 503))
    (rec,) = feed_track.reconcile_pending([_pending(age_h=97.0)], {"T1": STORE})
    assert rec["state"] == "closed" and "沃尔玛503" in calls["close"][0]


def test_never_sent_closes_only_when_the_workflow_is_not_running(monkeypatch):
    """发送标记为空 = 请求还没开始发:原工作流还在跑(可能排队等配额)就不动;
    不在跑 ⇒ 确定没发出。判不了(锁文件打不开)期限 + 宽限内也不动。"""
    calls = _recon_wired(monkeypatch, held=True)
    (rec,) = feed_track.reconcile_pending([_pending(posted=False)], {"T1": STORE})
    assert rec["state"] == "open" and "请求还没开始发送,product_clear 正在跑" in rec["detail"]
    assert calls["close"] == []
    calls = _recon_wired(monkeypatch, held=None)
    (rec,) = feed_track.reconcile_pending([_pending(posted=False)], {"T1": STORE})
    assert rec["state"] == "open" and "锁文件打不开" in rec["detail"]
    (rec,) = feed_track.reconcile_pending([_pending(age_h=97.0, posted=False)], {"T1": STORE})
    assert rec["state"] == "closed" and calls["close"][0].startswith(
        "未发出:请求从没开始发送(发送标记为空),claim 已过落定期限 72 小时")
    calls = _recon_wired(monkeypatch, held=False)
    (rec,) = feed_track.reconcile_pending([_pending(posted=False)], {"T1": STORE})
    assert rec["state"] == "closed" and calls["close"][0].startswith(
        "未发出:product_clear 已不在运行")
    assert calls["lookup"] == []                  # 没发出就不去沃尔玛那边查


def test_a_row_whose_workflow_is_still_running_is_left_alone(monkeypatch):
    """原工作流还在跑 ⇒ 这一笔可能正在它手里当场结算(30 秒复查 / list_new 的延后
    结算):不反查、不收编、不收口 —— 这时收编,它自己的反查会把收编的 feed 当
    "已记账"排除 → 判未达 → 同一载荷补交 = 重复提交。存量行同理(部署那一刻还在跑的
    旧代码建的行也没有条数)。"""
    calls = _recon_wired(monkeypatch, verdict=("FOUND", {"feedId": "F_OURS"}), held=True)
    for row in (_pending(age_h=200.0), _pending(age_h=200.0, item_count=None, posted=False)):
        (rec,) = feed_track.reconcile_pending([row], {"T1": STORE})
        assert rec["state"] == "open"
        assert "product_clear 正在跑,这一笔可能正在它手里当场结算" in rec["detail"]
    assert calls == {"close": [], "adopt": [], "note": [], "lookup": []}
    calls = _recon_wired(monkeypatch, verdict=("FOUND", {"feedId": "F_OURS"}), held=None)
    (rec,) = feed_track.reconcile_pending([_pending(age_h=10.0)], {"T1": STORE})
    assert rec["state"] == "open" and calls["lookup"] == []       # 探不了锁:宽限内不动
    (rec,) = feed_track.reconcile_pending([_pending(age_h=97.0)], {"T1": STORE})
    assert rec["state"] == "adopted"                              # 过了宽限:照常对账


def test_legacy_pending_rows_close_after_the_grace_without_a_lookup(monkeypatch):
    """09-25 之前 claim 的存量行没记条数 / SKU / 发送标记:没法反查,期限 + 宽限后收口。"""
    calls = _recon_wired(monkeypatch)
    legacy = dict(_pending(age_h=50.0, item_count=None, posted=False))
    (rec,) = feed_track.reconcile_pending([legacy], {"T1": STORE})
    assert rec["state"] == "open" and calls["close"] == []
    legacy = dict(_pending(age_h=100.0, item_count=None, posted=False))
    (rec,) = feed_track.reconcile_pending([legacy], {"T1": STORE})
    assert rec["state"] == "closed" and calls["close"][0].startswith("提交未确认:存量 pending")
    assert calls["lookup"] == []


def test_an_unloadable_store_closes_after_the_grace(monkeypatch):
    calls = _recon_wired(monkeypatch)
    (rec,) = feed_track.reconcile_pending([_pending(age_h=90.0, store="GONE")], {"T1": STORE})
    assert rec["state"] == "open" and "暂不能反查" in rec["detail"]
    (rec,) = feed_track.reconcile_pending([_pending(age_h=97.0, store="GONE")], {"T1": STORE})
    assert rec["state"] == "closed" and "店铺不可调用" in calls["close"][0]


def test_reconcile_dry_run_decides_but_writes_nothing(monkeypatch):
    calls = _recon_wired(monkeypatch, verdict=("FOUND", {"feedId": "F_OURS"}))
    (rec,) = feed_track.reconcile_pending([_pending()], {"T1": STORE}, execute=False)
    assert rec["state"] == "adopted"
    assert calls["adopt"] == [] and calls["note"] == [] and calls["close"] == []


def test_reconcile_never_submits_anything():
    """对账器只读:整个函数里不许出现任何提交入口(补交由原业务工作流按原方法做)。"""
    import inspect
    src = inspect.getsource(feed_track.reconcile_pending)
    for forbidden in ("submit_feed", "_post(", "_submit_one", "settle_deferred"):
        assert forbidden not in src


@needs_pg
def test_pending_ledger_primitives_on_a_real_database(monkeypatch):
    """真库:认领记条数与 SKU 数组 → 发送标记 → 收编(feed_log 转 submitted、feed_items
    按发送时刻落)→ 另一行落 failed 带依据 → 对账计数。"""
    monkeypatch.setenv("WALMART_PG_DSN", _PG_DSN)
    from registry import db
    key1, key2 = "k-recon-sandbox-1", "k-recon-sandbox-2"

    def _cleanup():
        with db.pg_conn() as conn:
            conn.execute("DELETE FROM ops.feed_items WHERE feed_id = 'F_RECON_SANDBOX'")
            conn.execute("DELETE FROM ops.feed_log WHERE payload_key IN (%s, %s)",
                         (key1, key2))
    _cleanup()
    try:
        lid, _ = feeds._log_claim("product_clear", _PG_STORE, "DELETE_ITEM", key1,
                                  2, ["A", "B"])
        lid2, _ = feeds._log_claim("product_clear", _PG_STORE, "DELETE_ITEM", key2,
                                   1, ["C"])
        feeds._log_posting(lid)
        feeds.note_reconcile(lid2)
        rows = {r["id"]: r for r in feeds.query_pending() if r["id"] in (lid, lid2)}
        assert rows[lid]["item_count"] == 2 and rows[lid]["skus"] == ["A", "B"]
        posted = rows[lid]["post_started_at"]
        assert posted is not None and rows[lid2]["post_started_at"] is None
        assert rows[lid2]["recon_count"] == 1
        assert feeds.adopt_pending(rows[lid], "F_RECON_SANDBOX", posted) is True
        assert feeds.adopt_pending(rows[lid], "F_RECON_SANDBOX", posted) is False
        assert feeds.close_pending(lid2, "未发出:测试") is True
        with db.pg_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, status, feed_id, updated_at = post_started_at,"
                        " close_basis FROM ops.feed_log WHERE id IN (%s, %s)"
                        " ORDER BY id", (lid, lid2))
            got = cur.fetchall()
            cur.execute("SELECT sku, status, submitted_at = %s FROM ops.feed_items"
                        " WHERE feed_id = 'F_RECON_SANDBOX' ORDER BY sku", (posted,))
            items = cur.fetchall()
        assert got == [(lid, "submitted", "F_RECON_SANDBOX", True, None),
                       (lid2, "failed", None, None, "未发出:测试")]   # 没发出:标记为空
        assert items == [("A", "submitted", True), ("B", "submitted", True)]
        # 重占 failed 行:上一笔的事实一并清空
        lid3, prev = feeds._log_claim("product_clear", _PG_STORE, "DELETE_ITEM", key2,
                                      1, ["C"])
        assert lid3 == lid2 and prev is None
        with db.pg_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, recon_count, close_basis, post_started_at"
                        " FROM ops.feed_log WHERE id = %s", (lid2,))
            assert cur.fetchone() == ("pending", 0, None, None)
    finally:
        _cleanup()
