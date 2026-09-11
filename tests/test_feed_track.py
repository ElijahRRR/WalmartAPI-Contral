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
    assert ("success", None, None, "F1", "A") in rows
    assert ("failed", "ERR_9", None, "F1", "B") in rows
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
    assert ("success", None, None, "F1", "A") in rows          # 台账不受白名单影响


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
    for status, code, desc, fid, _sku in rows:
        assert (status, code, fid) == ("failed",
                                       "EXT_DATA_ERROR_50575703577001", "F1")
        assert "item setup limit of 5000" in desc      # 码本身不含任何信息
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
    assert out == {"S1": ("success", ""), "S2": ("failed", "ERR_9")}
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


def test_poll_all_summary_and_pending_alarm(monkeypatch, caplog):
    import logging as _logging
    monkeypatch.setattr(feeds, "query_pending", lambda: [
        {"status": "submitted", "feed_id": "F1", "store": "T1",
         "feed_type": "DELETE_ITEM", "workflow": "", "created_at": "t"},
        {"status": "submitted", "feed_id": "F2", "store": "T_GONE",
         "feed_type": "DELETE_ITEM", "workflow": "", "created_at": "t"},
        {"status": "pending", "feed_id": None, "store": "T1",
         "feed_type": "RETIRE_ITEM", "created_at": "t"},
    ])
    monkeypatch.setattr(feed_track, "poll_feed",
                        lambda store, fid: ({"feedStatus": "PROCESSED"},
                                            {"A": ("success", "")}))
    with caplog.at_level(_logging.WARNING, logger="services.feed_track"):
        out = feed_track.poll_all({"T1": STORE})
    assert "落定 1" in out and "凭证缺失跳过 1" in out
    assert "pending 待人工核对 1" in out
    # 逐 feed 明细:店铺 + 业务动作名 + feed_id + 结果
    assert "T1 删除(-) F1:已落定 PROCESSED,成功 1,失败 0" in out
    assert "T_GONE 删除(-) F2:店铺凭证缺失,跳过" in out
    assert any("提交结局不确定" in m for m in caplog.messages)
    # ⚠ pending 的明细必须进**摘要**(发去飞书的那一份),不能只在日志里:
    # 只报个数,人看到之后无从下手;而 pending 行永不老化,数字只增不减,
    # 几轮之后这行警告就成了背景噪音(2026-08-16 feed 闭环审计)
    assert "系统不会自动补交" in out
    assert "T1 RETIRE_ITEM(-) 提交于 t" in out


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
    """「状态→中文」四份拷贝的并集,六个键一个都不能少。

    processing/unknown 只有 clear_sheet 那份带,而 product_clear 是
    `RESULT_TEXT[outcome]` **直接下标**取(不是 .get)——少一键就是 KeyError,
    停用/删除表的整轮回写当场炸。missing 同理来自 poll_feed 的"台账里有、
    终态明细里查无"。
    """
    assert feed_track.RESULT_TEXT == {
        "success": "成功", "failed": "失败", "missing": "未查到",
        "submitted": "处理中", "processing": "处理中", "unknown": "处理中"}


def test_text_of_maps_status_and_never_fakes_a_verdict():
    """未登记状态按未落定报「处理中」:不装成功也不装失败,下轮再看。"""
    t = feed_track.text_of
    assert t("success") == "成功"
    assert t("failed") == "失败"
    assert t("missing") == "未查到"
    assert t("submitted") == t("processing") == t("unknown") == "处理中"
    assert t("OFFICIAL_NEW_ENUM") == "处理中"
    assert t("") == "处理中"


def test_text_of_appends_error_only_on_the_failed_bucket():
    """跟卖表现行形状「失败:{码 | 人话}」;成功/未查到后面不挂报错。"""
    t = feed_track.text_of
    want = "EXT_ERR_1 | [color] required"
    assert t("failed", want) == f"失败:{want}"
    assert t("failed", "") == "失败"
    assert t("success", want) == "成功"
    assert t("missing", want) == "未查到"
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

    def fake_poll(store, fid):
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
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f: (
        {"feedStatus": "PROCESSED"},
        {"A": ("success", ""), "B": ("failed", "E1"), "C": ("processing", "")}))
    out = feed_track.poll_all({"T1": STORE})
    assert "已落定" not in out
    assert "落定 0,仍处理中 1" in out
    assert "T1 上架(list_new) F1:PROCESSED 已终态,但 1 个 SKU 未落定" in out


def test_residue_says_out_loud_when_it_is_an_unrecognised_enum(monkeypatch):
    """残留是 unknown 时摘要要点破:枚举可能已扩,光等是等不来的。

    `sku_outcome` 对没见过的 ingestionStatus 返回 unknown 并告警 —— 那条告警
    只在日志里,而摘要是发去飞书的那一份。不说,人只看得到"还有 3 个没落定",
    以为沃尔玛慢,实际是码表该补了。
    """
    monkeypatch.setattr(feeds, "query_pending", lambda: [_inflight(fid="F1")])
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f: (
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
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f: (
        {"feedStatus": "INPROGRESS", "itemsReceived": 15, "itemsSucceeded": 12,
         "itemsFailed": 2}, None))
    out = feed_track.poll_all({"T1": STORE})
    assert "F1" not in out and "F2" not in out          # 明细不再逐条复读
    assert "⏳ 长期在途 2(最久 36.0h)" in out            # 首行带结论(规矩 1)
    assert "T1 维护(卡 36h)、T1 分仓库存(卡 5h)" in out   # 点得出是哪几个
    assert "docs/feed_closure_audit.md" in out          # 自带处置(规矩 3)
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
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f: (
        {"feedStatus": "INPROGRESS", "itemsReceived": 10, "itemsSucceeded": 3,
         "itemsFailed": 1}, None))
    out = feed_track.poll_all({"T1": STORE})
    assert "T1 删除(-) F1:INPROGRESS,已收 10,成功 3,失败 1,待处理 6" in out
    assert "长期在途" not in out


def test_an_unknowable_age_counts_as_fresh(monkeypatch):
    """年龄拿不到(updated_at 缺)一律当新鲜:宁可多播一行,不可少播一行。"""
    monkeypatch.setattr(feeds, "query_pending", lambda: [_inflight(fid="F1")])
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f: (
        {"feedStatus": "INPROGRESS"}, None))
    out = feed_track.poll_all({"T1": STORE})
    assert "T1 删除(-) F1:INPROGRESS" in out and "长期在途" not in out


def test_a_feed_that_finally_settles_prints_even_after_days(monkeypatch):
    """落定永远出明细行,哪怕它在途了四天:那是**新信息**,而且下一轮这个
    feed 就出队了,只播这一次 —— 折叠折的是"还会再播 47 遍"的那些。"""
    monkeypatch.setattr(feeds, "query_pending",
                        lambda: [_inflight(fid="F1", age_h=99.0)])
    monkeypatch.setattr(feed_track, "poll_feed", lambda s, f: (
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
