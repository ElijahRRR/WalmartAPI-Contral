"""store_watch 回归:扫描面、推送-标记的因果、seed/dry-run、TRO 组合、滞留计数。

钉的全是"预警看起来在跑,其实没人被叫醒"那一类故障:
推送失败却把事件标掉(永久埋掉)、dry-run 悄悄写库、明细渲染成 `None→None`、
窗口外滞留没人报、调度条目被改掉。没有一条会在测试之外被发现。
"""

import contextlib
import os
import pathlib
import re
import socket

import pytest

from registry import schedule
from services import store_events as se
from workflows import store_watch as sw


# ── 扫描面:等值 severity + 局部索引 + 行锁 ──────────────────────────────────

def test_scan_sql_uses_equality_on_severity_and_skips_locked():
    """⚠ lint 式护栏,不是风格洁癖。

    局部索引 store_events_unnotified_idx 是 `(severity, occurred_at DESC)
    WHERE notified_at IS NULL`,**首列 severity**:等值才能把它当前缀用上并
    顺着第二列拿到有序结果。改成 `= ANY(...)` / `IN (...)` / `severity <> 'info'`
    都会退化成扫整个局部索引再排序 —— 而且**不报错**,账本小的时候一样快。
    SKIP LOCKED 是 flock 之外的第二道保险(换个入口就绕过 flock 了)。
    """
    sql = se._SCAN_SQL
    assert "severity = %(sev)s::text" in sql
    assert "= ANY" not in sql and " IN (" not in sql
    assert "notified_at IS NULL" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    # 标记语句必须再带一次 notified_at IS NULL:只标"我这轮真推出去的"
    assert "notified_at IS NULL" in se._MARK_SQL


def test_every_sql_param_is_cast_in_store_events_scan():
    """与 test_store_events 同款 lint 护栏(dispositions 生产实炸三次的教训)。"""
    src = pathlib.Path("services/store_events.py").read_text()
    bad = []
    for m in re.finditer(r'^(_\w*SQL)\s*=\s*"""(.*?)"""', src, re.S | re.M):
        for pm in re.finditer(r"%\((\w+)\)s(?!\s*::)", m.group(2)):
            bad.append(f"{m.group(1)}.{pm.group(1)}")
    assert not bad, "这些 SQL 参数没写显式 ::类型:" + ", ".join(bad)


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self.rowcount = 0

    def execute(self, sql, args=None):
        self.conn.calls.append((sql, args))
        if "FROM ops.store_events" in sql and "count(*)" in sql:
            self._rows = [self.conn.counts]
            self.description = [("a",), ("b",)]
        elif sql.strip().startswith("SELECT id"):
            self.description = [(c,) for c in ("id", "store", "event",
                                               "severity", "source", "detail",
                                               "occurred_at")]
            self._rows = self.conn.scan_rows
        else:                                    # UPDATE ... notified_at
            self.rowcount = len(args["ids"])
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, scan_rows=(), counts=(0, 0)):
        self.scan_rows, self.counts, self.calls = list(scan_rows), counts, []

    def cursor(self):
        return _Cur(self)


def test_mark_notified_makes_no_query_for_an_empty_list():
    conn = _Conn()
    assert se.mark_notified(conn, []) == 0
    assert conn.calls == []


def test_scan_returns_dicts_keyed_by_column_name():
    conn = _Conn(scan_rows=[(1, "A085", se.STORE_STATUS_CHANGED, "high",
                             "daily_report", {"old": "ACTIVE"}, "t0")])
    rows = se.scan_unnotified(conn, "high", 48, 50)
    assert rows[0]["id"] == 1 and rows[0]["store"] == "A085"
    assert conn.calls[0][1] == {"sev": "high", "hours": 48, "limit": 50}


# ── TRO 组合:同店同日两条腿 ────────────────────────────────────────────────

_NEXT_ID = [0]


def _row(store, event, severity="high", day="2026-08-30", **detail):
    """状态三码自动补上 old/new —— 它们的 detail 在生产里一定有这两个键。"""
    d = {"data_date": day}
    if event in (se.STORE_STATUS_CHANGED, se.PAYMENT_STATUS_CHANGED,
                 se.SALES_STATUS_CHANGED):
        d.update(old="ACTIVE", new="SUSPENDED")
    d.update(detail)
    _NEXT_ID[0] += 1
    return {"id": _NEXT_ID[0], "store": store, "event": event,
            "severity": severity, "source": "daily_report", "detail": d,
            "occurred_at": day}


def test_tro_stores_needs_both_legs_same_store_same_day():
    """跨店凑、跨日凑都不算 —— A 店周一被封 + B 店周三冻结合起来什么也不是。"""
    same = [_row("A085", se.STORE_STATUS_CHANGED),
            _row("A085", se.PAYMENT_STATUS_CHANGED)]
    assert se.tro_stores(same) == ["A085"]
    cross_store = [_row("A085", se.STORE_STATUS_CHANGED),
                   _row("谭总9", se.PAYMENT_STATUS_CHANGED)]
    assert se.tro_stores(cross_store) == []
    cross_day = [_row("A085", se.STORE_STATUS_CHANGED, day="2026-08-29"),
                 _row("A085", se.PAYMENT_STATUS_CHANGED, day="2026-08-30")]
    assert se.tro_stores(cross_day) == []


def test_tro_stores_ignores_global_rows():
    """store 为空的全局行(TRO 品牌源头)不属于任何一家店,不参与分组。"""
    rows = [_row(None, se.TRO_BRAND_HIT), _row(None, se.STORE_STATUS_CHANGED)]
    assert se.tro_stores(rows) == []


# ── brief:全事件码都得说人话,一条也不许 None→None ──────────────────────────

def _all_families():
    """每一族给一条真实形状的 detail(与各接线点逐字对齐)。"""
    return [
        {"store": "A085", "event": se.STORE_STATUS_CHANGED, "severity": "high",
         "detail": {"old": "ACTIVE", "new": "SUSPENDED",
                    "data_date": "2026-08-30"}},
        # TRO 源头:store=NULL,detail 里没有 old/new
        {"store": None, "event": se.TRO_BRAND_HIT, "severity": "high",
         "detail": {"brand": "ACME", "source": "r4", "first_asin": "B0AAA",
                    "judged": True}},
        {"store": "谭总9", "event": se.TRO_BRAND_EXPOSURE, "severity": "mid",
         "detail": {"brand": "ACME", "evidence": ["item"], "still_listed": True,
                    "asins": ["B0AAA"], "asin_total": 3}},
        {"store": "A085", "event": se.PHISHING_ORDER, "severity": "high",
         "detail": {"order_line_id": "L1", "po_id": "PO9", "sku": "B0AAA",
                    "asin": "B0AAA", "zip": "99999"}},
        {"store": "谭总9", "event": se.PHISHING_BRAND_EXPOSURE, "severity": "mid",
         "detail": {"order_line_id": "L1", "brand": "ACME",
                    "origin_store": "A085", "evidence": ["item"],
                    "still_listed": False, "asins": []}},
        {"store": "A085", "event": se.STORE_LIMITS_CHANGED, "severity": "high",
         "detail": {"field": "单店最大在线数", "old": "3000", "new": "0"}},
        {"store": "A085", "event": se.STORE_ENABLED_CHANGED, "severity": "high",
         "detail": {"old": True, "new": False}},
        {"store": None, "event": se.STORE_SCOPE_CHANGED, "severity": "mid",
         "detail": {"old": [], "new": ["谭总"]}},
        # 限额表表结构:store=NULL,detail 里只有列名(未登记列的值绝不入账)
        {"store": None, "event": se.STORE_LIMITS_COLUMNS_CHANGED,
         "severity": "info", "detail": {"added": ["备注"], "removed": []}},
        {"store": "A085", "event": se.STORE_DEREGISTERED, "severity": "high",
         "detail": {"enabled": True}},
        {"store": "A085", "event": se.CLAIM_RELEASED, "severity": "high",
         "detail": {"brand": 12, "product": 0, "sample": ["brand:acme"],
                    "scope": "store", "reason": "dead"}},
        {"store": "A085", "event": se.LIST_ROUND, "severity": "info",
         "detail": {"listed": 3}},
    ]


def test_brief_never_renders_none_to_none():
    """⚠ 早先那版兜底对 TRO/钓鱼/治理三族输出 `? tro_brand_hit None→None`:
    通知照发、字数照占,人一个字都读不出来。全事件码都得说人话。"""
    for e in _all_families():
        got = se.brief(e)
        assert "None→None" not in got, (e["event"], got)
        assert "None" not in got, (e["event"], got)
        assert got.strip() and not got.startswith(" ")


def test_brief_covers_every_registered_event_code():
    """码登记时渲染要跟着登记 —— 漏了的会退到干瘪的码名,这条把它挡在提交期。"""
    missing = [c for c in sorted(se.EVENTS) if not se._brief_body(c, {})]
    assert not missing, f"这些事件码没有 brief 渲染:{missing}"


def test_brief_keeps_the_three_status_lines_unchanged():
    """日报的状态节读的是同一个 brief,形态逐字不动。"""
    assert se.brief(_all_families()[0]) == "A085 店铺 ACTIVE→SUSPENDED"


def test_brief_labels_global_rows_instead_of_a_question_mark():
    assert se.brief(_all_families()[1]).startswith("全局 TRO 品牌「ACME」")
    assert se.brief(_all_families()[7]).startswith("全局 规划外名单")
    assert se.brief(_all_families()[8]) == "全局 限额表未登记列变化:新增 备注"


# ── 一轮:推送 / 标记的因果 ─────────────────────────────────────────────────

@pytest.fixture
def wired(monkeypatch):
    """把 store_watch 的四个外沿(库/治理/扫描/飞书)换成可编程夹具。"""
    state = {"marked": [], "sent": [], "notify_ok": True, "rows": [],
             "counts": (0, 0), "gov": ([], None), "gov_called": 0}

    @contextlib.contextmanager
    def _conn():
        yield object()

    def _check(conn):
        state["gov_called"] += 1
        return state["gov"]

    monkeypatch.setattr(sw.db, "pg_conn", _conn)
    monkeypatch.setattr(sw.store_config, "check_and_record", _check)
    monkeypatch.setattr(sw.store_events, "scan_unnotified",
                        lambda c, s, h, l: list(state["rows"]))
    monkeypatch.setattr(sw.store_events, "unnotified_counts",
                        lambda c, s, h: state["counts"])
    monkeypatch.setattr(sw.store_events, "mark_notified",
                        lambda c, ids: state["marked"].extend(ids) or len(ids))

    def _notify(text):
        state["sent"].append(text)
        return state["notify_ok"]

    monkeypatch.setattr(sw.feishu, "notify", _notify)
    return state


_EXEC = {"execute": True, "dry_run": False}


def test_nothing_to_push_says_so_with_the_window(wired):
    out = sw.run(dict(_EXEC))
    assert out.splitlines()[0] == "店铺预警:无待推送高危(窗口 48h)"
    assert wired["sent"] == [] and wired["marked"] == []


def test_push_then_mark_only_when_feishu_really_sent(wired):
    wired["rows"] = [_row("A085", se.STORE_STATUS_CHANGED)]
    wired["rows"][0]["id"] = 7
    wired["counts"] = (1, 0)
    out = sw.run(dict(_EXEC))
    assert out.startswith("🚨 店铺预警:1 店 1 条高危")
    assert "已推送并标记 1 条" in out
    assert wired["marked"] == [7]
    assert wired["sent"][0].splitlines()[0].startswith("🚨 店铺预警:")


def test_failed_push_marks_nothing_and_the_summary_says_so(wired):
    """⚠ 标了就等于把这些事件永久埋掉:账本只追加,没有第二条补给线。
    摘要是人眼闸门,不许自我美化(daily_report._phase_push 同款纪律)。"""
    wired["notify_ok"] = False
    wired["rows"] = [_row("A085", se.STORE_STATUS_CHANGED)]
    wired["counts"] = (1, 0)
    out = sw.run(dict(_EXEC))
    assert wired["marked"] == []
    assert out.startswith("⚠ 店铺预警:")
    assert "**未发出**" in out and "下轮重试" in out
    assert "已推送" not in out


def test_seed_marks_without_pushing(wired):
    """首次上线把存量吞掉:不推送直接标记(否则第一条消息是几个月的历史)。"""
    wired["rows"] = [_row("A085", se.STORE_STATUS_CHANGED)]
    wired["rows"][0]["id"] = 11
    wired["counts"] = (1, 0)
    out = sw.run(dict(_EXEC, seed="1"))
    assert wired["sent"] == [] and wired["marked"] == [11]
    assert "seed 标记 1 条" in out and "不推送" in out


def test_dry_run_lists_but_touches_nothing(wired):
    """⚠ DANGEROUS=False ⇒ cli 把 execute 恒置真,`--dry-run` 得自己认 ——
    只看 execute 的话这个开关对本工作流完全无效,而且不报错。"""
    wired["rows"] = [_row("A085", se.STORE_STATUS_CHANGED)]
    wired["counts"] = (1, 0)
    out = sw.run({"execute": True, "dry_run": True})
    assert wired["sent"] == [] and wired["marked"] == []
    assert wired["gov_called"] == 0          # 治理比对会写库,dry-run 整段跳过
    assert out.startswith("🧪 [DRY-RUN] 店铺预警:")
    assert "未推送未标记" in out
    assert "A085 店铺 ACTIVE→SUSPENDED" in out    # 但明细照列(这就是 dry-run 的用处)


def test_tro_combination_reaches_the_push_text(wired):
    wired["rows"] = [_row("A085", se.STORE_STATUS_CHANGED),
                     _row("A085", se.PAYMENT_STATUS_CHANGED)]
    wired["counts"] = (2, 0)
    out = sw.run(dict(_EXEC))
    assert "(1 店疑似 TRO 封店)" in out.splitlines()[0]
    assert "🚨 疑似 TRO 封店:A085" in wired["sent"][0]


def test_global_rows_are_counted_separately_from_stores(wired):
    """全局行不属于任何一家店:不点出来的话"0 店 1 条"看着像 bug。

    ⚠ 而且**不能算进店铺那个数**(2026-08-30 首次 dry-run 实见):算进去就是
    "1 店 3 条高危,另全局 1 条" —— 3 里已经含着那 1 条,人拿这两个数去库里
    对永远对不上,而且不报错。
    """
    wired["rows"] = [_row(None, se.TRO_BRAND_HIT, brand="ACME",
                          first_asin="B0AAA", judged=True),
                     _row("A085", se.STORE_STATUS_CHANGED)]
    wired["counts"] = (2, 0)
    out = sw.run(dict(_EXEC))
    assert "1 店 1 条高危,全局 1 条" in out.splitlines()[0]


def test_over_limit_says_how_many_are_still_queued(wired):
    wired["rows"] = [_row("A085", se.STORE_STATUS_CHANGED)]
    wired["counts"] = (9, 0)          # 窗口内 9 条,本轮只取到 1 条
    out = sw.run(dict(_EXEC, limit="1"))
    assert "⚠ 还有 8 条待推(本轮 limit=1)" in out


def test_stale_backlog_outside_the_window_is_reported(wired):
    """⚠ 时间窗挡的是首轮历史洪水,代价是超窗的高危永远推不出去。
    恒 0 不打;>0 就是有漏网的,人该去看为什么。"""
    wired["counts"] = (0, 4)
    out = sw.run(dict(_EXEC))
    assert "⚠ 窗口外滞留 4 条未推送高危(早于 48h" in out
    wired["counts"] = (0, 0)
    assert "窗口外滞留" not in sw.run(dict(_EXEC))


def test_governance_events_and_warning_join_the_summary(wired):
    wired["gov"] = ([{"store": "A085", "event": se.STORE_LIMITS_CHANGED,
                      "severity": "high",
                      "detail": {"field": "类目1", "old": "Home", "new": ""}}],
                    None)
    out = sw.run(dict(_EXEC))
    assert "治理快照:1 条变更(其中 high 1 条,本轮随高危一起推)" in out
    assert "A085 类目1「Home」→「」" in out
    wired["gov"] = ([], "⚠ 治理配置本轮没比对(飞书读不到:boom)")
    assert "飞书读不到:boom" in sw.run(dict(_EXEC))


def test_bad_params_are_rejected_loudly(wired):
    """写错的参数若被静默当成缺省,人会以为自己改的窗口生效了。"""
    assert sw.run(dict(_EXEC, severity="URGENT")).startswith("severity 只接受")
    with pytest.raises(ValueError, match="hours"):
        sw.run(dict(_EXEC, hours="48h"))
    with pytest.raises(ValueError, match="limit"):
        sw.run(dict(_EXEC, limit="0"))


# ── 调度条目 ────────────────────────────────────────────────────────────────

def test_the_schedule_entry_is_pinned():
    """每小时 :45、批 1、launchd —— 改任何一项都得先撞上这条。

    :45 避开每小时另外三个峰(:00/:30 feed 轮询、:20 订单链、:50 摄取泵):
    锁是按工作流名的,撞在同一分钟不会互相拿锁,但会一起挤代理与 PG 连接。
    """
    job = next(j for j in schedule.JOBS if j["label"] == "store_watch")
    assert job["workflows"] == ["store_watch"]
    assert (job["hour"], job["minute"]) == (None, 45)     # hour=None ⇒ 每小时
    assert job["batch"] == 1 and job["runner"] == "launchd"
    busy = {j["minute"] for j in schedule.jobs_for("launchd")
            if isinstance(j["minute"], int) and j["label"] != "store_watch"}
    assert 45 not in busy
    # 调度里绝不许带 seed:seed 是"标记但不推送",挂上就是每小时静默吞事件
    assert not any(p.startswith("seed") for p in job["params"])


def test_the_workflow_is_not_dangerous_but_says_why():
    assert sw.DANGEROUS is False
    assert "notified_at" in sw.__doc__ and "重跑" in sw.__doc__


# ── 视图里的事件码字面量 ────────────────────────────────────────────────────

def test_schema_round_codes_match_the_constants():
    """⚠ v_store_profile 里五个 round 码是**唯一一处字面量副本**(视图是 SQL,
    取不到 Python 常量)。漏改的表现是那五列永远为空,不报错。"""
    sql = pathlib.Path("refdata/schema.sql").read_text()
    body = sql.split("CREATE OR REPLACE VIEW ops.v_store_profile")[1]
    for code in (se.LIST_ROUND, se.MAINT_ROUND, se.CLEANUP_ROUND,
                 se.CLEAR_ROUND, se.MATCH_ROUND):
        assert f"ev.event = '{code}'" in body, code


def test_both_views_are_create_or_replace():
    """db_init 幂等:DROP+CREATE 的视图不许被依赖,自己也不该是那种
    (PG 不允许 DROP 一个还有依赖者的视图 = 第二次跑就报错)。"""
    sql = pathlib.Path("refdata/schema.sql").read_text()
    for v in ("ops.v_store_timeline", "ops.v_store_profile"):
        assert f"CREATE OR REPLACE VIEW {v} AS" in sql
        assert f"DROP VIEW IF EXISTS {v}" not in sql


# ── 沙箱 PG 集成 ────────────────────────────────────────────────────────────
#
# ⚠ 地址是**测试夹具**(生产走 registry/db.pg_dsn() 的 unix socket);非标准
# 端口 55432 正是为了不可能连到生产库。整场事务最后回滚。
_PG_HOST, _PG_PORT = "127.0.0.1", 55432
_DSN = os.environ.get(
    "WALMART_TEST_PG_DSN",
    f"host={_PG_HOST} port={_PG_PORT} user=postgres dbname=walmart_data")


def _pg_up() -> bool:
    try:
        with socket.create_connection((_PG_HOST, _PG_PORT), timeout=1):
            return True
    except OSError:
        return False


needs_pg = pytest.mark.skipif(not _pg_up(),
                              reason=f"沙箱 PG {_PG_HOST}:{_PG_PORT} 未启动")


@pytest.fixture
def pg(monkeypatch):
    """真库连接;store_watch 复用**同一条**连接,整场结束回滚(不留残留)。"""
    monkeypatch.setenv("WALMART_PG_DSN", _DSN)
    from registry import db
    with db.pg_conn() as conn:
        @contextlib.contextmanager
        def _same():
            yield conn

        monkeypatch.setattr(sw.db, "pg_conn", _same)
        monkeypatch.setattr(sw.store_config, "check_and_record",
                            lambda c: ([], None))
        # ⚠ 扫描面是**全表**的(按 severity + 时间窗,不按店)—— 库里若已有
        # 别人留下的未推送高危,本用例的计数断言会被它们带偏。事务内先把存量
        # 全标掉(整场回滚,不影响库),让这一轮只看得见自己种的行。
        with conn.cursor() as cur:
            cur.execute("UPDATE ops.store_events SET notified_at = now()"
                        " WHERE notified_at IS NULL")
        try:
            yield conn
        finally:
            conn.rollback()


def _seed_events(conn, store="ZQX预警店"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.store_events (store, event, severity, source,"
            " detail, occurred_at) VALUES"
            " (%s, %s, 'high', 'daily_report', %s::jsonb, now()),"
            " (%s, %s, 'high', 'daily_report', %s::jsonb, now()),"
            " (%s, %s, 'info', 'list_new', '{\"listed\": 3}'::jsonb, now()),"
            " (%s, %s, 'high', 'daily_report', %s::jsonb,"
            "  now() - interval '90 days')",
            (store, se.STORE_STATUS_CHANGED,
             '{"old":"ACTIVE","new":"SUSPENDED","data_date":"2026-08-30"}',
             store, se.PAYMENT_STATUS_CHANGED,
             '{"old":"ACTIVE","new":"INACTIVE","data_date":"2026-08-30"}',
             store, se.LIST_ROUND,
             store, se.STORE_STATUS_CHANGED,
             '{"old":"ACTIVE","new":"SUSPENDED","data_date":"2026-05-01"}'))


@needs_pg
def test_pg_one_real_round_pushes_marks_and_leaves_the_stale_one(pg,
                                                                monkeypatch):
    """真库上跑一整轮:窗口内两条推出去并标掉,90 天前那条留在滞留计数里。"""
    _seed_events(pg)
    sent = []
    monkeypatch.setattr(sw.feishu, "notify",
                        lambda t: bool(sent.append(t)) or True)
    out = sw.run({"execute": True, "dry_run": False})

    assert "🚨 店铺预警:" in out and "已推送并标记 2 条" in out
    assert "(1 店疑似 TRO 封店)" in out          # 封店 + 冻结同日
    assert "⚠ 窗口外滞留 1 条未推送高危" in out
    assert "ZQX预警店 店铺 ACTIVE→SUSPENDED" in sent[0]
    with pg.cursor() as cur:
        # ⚠ 同一条 INSERT 里的 now() 三行完全相同,按时间排是不确定序 ——
        # 按 (级别, 是否在窗口内, 是否已标) 的多重集断言,不依赖行序
        cur.execute("SELECT severity,"
                    " occurred_at > now() - interval '1 day',"
                    " notified_at IS NOT NULL"
                    " FROM ops.store_events WHERE store = %s::text",
                    ("ZQX预警店",))
        got = sorted(cur.fetchall())
    # 只标 high 且在窗口内的两条;info 与 90 天前那条一律不动
    assert got == sorted([("high", True, True), ("high", True, True),
                          ("high", False, False), ("info", True, False)])

    # 同一轮再跑一次:已标的不再重推(索引条件 notified_at IS NULL)
    sent.clear()
    again = sw.run({"execute": True, "dry_run": False})
    assert sent == [] and again.startswith("店铺预警:无待推送高危")


@needs_pg
def test_pg_failed_push_leaves_every_row_unnotified(pg, monkeypatch):
    _seed_events(pg, store="ZQX未发店")
    monkeypatch.setattr(sw.feishu, "notify", lambda t: False)
    out = sw.run({"execute": True, "dry_run": False})
    assert "**未发出**" in out
    with pg.cursor() as cur:
        cur.execute("SELECT count(*) FROM ops.store_events"
                    " WHERE store = %s::text AND notified_at IS NOT NULL",
                    ("ZQX未发店",))
        assert cur.fetchone()[0] == 0


@needs_pg
def test_pg_both_archive_views_answer(pg):
    """两个视图真建在库上并各查一次(口径:档案的店铺全集来自 KPI 表)。"""
    _seed_events(pg, store="ZQX档案店")
    with pg.cursor() as cur:
        cur.execute("INSERT INTO ops.store_kpi_daily (store, data_date,"
                    " store_status, items_online) VALUES"
                    " (%s, '2026-08-29', 'ACTIVE', 10),"
                    " (%s, '2026-08-30', 'SUSPENDED', 12)",
                    ("ZQX档案店", "ZQX档案店"))
        cur.execute("SELECT 级别, 事件, 旧值, 新值, 数据日期"
                    " FROM ops.v_store_timeline WHERE 店铺 = %s::text",
                    ("ZQX档案店",))
        tl = cur.fetchall()
        assert len(tl) == 4
        # 状态类:三个键摊平;运营类:detail 里没有 old/new,摊平列为空但
        # `明细` 列照旧给整个 jsonb(踩坑注 ①)
        assert ("high", se.STORE_STATUS_CHANGED, "ACTIVE", "SUSPENDED",
                "2026-08-30") in tl
        assert ("info", se.LIST_ROUND, None, None, None) in tl

        cur.execute("SELECT 数据日期, 店铺状态, 在线商品, 高危累计, 待推高危,"
                    " 最近高危事件, 最近上架轮 IS NOT NULL"
                    " FROM ops.v_store_profile WHERE 店铺 = %s::text",
                    ("ZQX档案店",))
        row = cur.fetchone()
    assert str(row[0]) == "2026-08-30"       # DISTINCT ON 取每店最新一行
    assert (row[1], row[2]) == ("SUSPENDED", 12)
    assert row[3] == 3 and row[4] == 3       # 三条 high(含 90 天前那条)全未推
    assert row[5] == se.STORE_STATUS_CHANGED
    assert row[6] is True                    # 五个 round 码各自的最近时间
