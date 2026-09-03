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
                        lambda fid: {"SKU1": ("failed", "EXT_ERR_1")})
    monkeypatch.setattr(feed_track, "item_errors",
                        lambda fid: {"SKU1": "[color] required"})
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
    got = []
    monkeypatch.setattr(feed_track.blacklist, "record_asins",
                        lambda c, items: (got.extend(items), len(items))[1])

    feed_track.poll_feed(STORE, "F9")
    assert len(got) == 1 and got[0]["sku"] == "B0BAD01"
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
        def _f():
            if first:                            # 每条链的头一个反哺器上会合
                gate.wait()
            with lock:
                order.append(label)
            return f"{label}:回写 1 行"
        return _f

    def _boom():
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
