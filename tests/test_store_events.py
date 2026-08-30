"""店铺事件账本回归:diff 三铁规、按方向定级、fail-loud、同日去重、TRO 组合。"""

import pathlib
import re

import pytest

from services import store_events as se


def _ev(prev, new, store="T1", day="2026-08-30"):
    return se.kpi_status_events(store, day, prev, new)


def test_first_observation_is_not_a_change():
    """首次观测不算变化:新店第一天就是 SUSPENDED,事件流里不该有东西。"""
    assert _ev({}, {"store_status": "SUSPENDED"}) == []
    assert _ev({"store_status": None}, {"store_status": "SUSPENDED"}) == []


def test_empty_cell_is_unknown_not_a_state():
    """空=没抓到,不是状态(影刀列 30 天 570 行空):两侧任一为空都不产事件。

    尤其 prev 有值 + new 为空 —— 不是"变没了",是这轮没抓到,照旧口径
    「不新鲜宁可留空」,diff 必须跳过,否则每次影刀失联都误报一轮迁移。
    """
    assert _ev({"sales_status": "可售"}, {"sales_status": ""}) == []
    assert _ev({"sales_status": "可售"}, {"sales_status": "  "}) == []
    assert _ev({"sales_status": ""}, {"sales_status": "可售"}) == []


def test_no_event_when_value_unchanged():
    assert _ev({"store_status": "ACTIVE"}, {"store_status": "ACTIVE"}) == []
    # btrim 后相同也不算变化(表里常见尾部空格)
    assert _ev({"store_status": "ACTIVE "}, {"store_status": " ACTIVE"}) == []


def test_severity_by_direction():
    """同一事件码两个方向级别不同 —— 这是 severity 必须落行的原因。

    分级依据 2026-08-30 生产数据(90 天迁移分布),对照表在模块头注。
    """
    def one(col, old, new):
        evs = _ev({col: old}, {col: new})
        assert len(evs) == 1, (col, old, new)
        return evs[0]

    # high:封店 / 资金冻结 / 终局
    assert one("store_status", "ACTIVE", "SUSPENDED")["severity"] == "high"
    assert one("payment_status", "ACTIVE", "INACTIVE")["severity"] == "high"
    assert one("store_status", "ACTIVE", "TERMINATED")["severity"] == "high"
    assert one("store_status", "SUSPENDED", "TERMINATED")["severity"] == "high"
    # info:恢复方向,入账不推送(时间线要能看到"被封→3 天后恢复"整个来回)
    assert one("store_status", "SUSPENDED", "ACTIVE")["severity"] == "info"
    assert one("payment_status", "INACTIVE", "ACTIVE")["severity"] == "info"
    assert one("sales_status", "不可售", "可售")["severity"] == "info"
    # mid:影刀列跌落(新鲜度差不进 high)与未知值之间的迁移
    assert one("sales_status", "可售", "不可售")["severity"] == "mid"
    assert one("payment_status", "INACTIVE", "ON_HOLD")["severity"] == "mid"


def test_three_columns_diff_independently():
    evs = _ev({"store_status": "ACTIVE", "payment_status": "ACTIVE",
               "sales_status": "可售"},
              {"store_status": "SUSPENDED", "payment_status": "INACTIVE",
               "sales_status": "不可售"})
    assert {e["event"] for e in evs} == {
        se.STORE_STATUS_CHANGED, se.PAYMENT_STATUS_CHANGED,
        se.SALES_STATUS_CHANGED}
    # detail 只记 {old,new,data_date},绝不复制 KPI 数值(两处存必漂)
    for e in evs:
        assert set(e["detail"]) == {"old", "new", "data_date"}


def test_tro_signature_needs_both_high_legs():
    """封店 + 资金冻结**同日同店**才是疑似 TRO;单腿不算,恢复方向更不算。"""
    both = _ev({"store_status": "ACTIVE", "payment_status": "ACTIVE"},
               {"store_status": "SUSPENDED", "payment_status": "INACTIVE"})
    assert se.tro_signature(both)
    only_store = _ev({"store_status": "ACTIVE"}, {"store_status": "SUSPENDED"})
    assert not se.tro_signature(only_store)
    recovery = _ev({"store_status": "SUSPENDED", "payment_status": "INACTIVE"},
                   {"store_status": "ACTIVE", "payment_status": "ACTIVE"})
    assert not se.tro_signature(recovery)


def test_record_many_rejects_unregistered_code_and_bad_severity():
    """未登记码 / 非法 severity 宁炸不吞 —— 写错的行是永远没人查的分叉。"""
    with pytest.raises(ValueError, match="未登记"):
        se.record_many(object(), [{"store": "T1", "event": "typo_event",
                                   "severity": "high", "source": "x"}])
    with pytest.raises(ValueError, match="severity"):
        se.record_many(object(), [{"store": "T1",
                                   "event": se.STORE_STATUS_CHANGED,
                                   "severity": "URGENT", "source": "x"}])


def test_every_event_code_has_a_class():
    """码登记时必须同时归类(risk/governance/ops)—— EVENTS 就是 CLASS 的键集,
    这里钉住任何人以后加码却忘归类会立刻红。"""
    assert se.EVENTS == frozenset(se.CLASS)
    assert set(se.CLASS.values()) <= {"risk", "governance", "ops"}


def test_every_sql_param_is_cast_in_store_events():
    """与 test_problem_scan 同款 lint 护栏(dispositions 生产实炸三次的教训)。"""
    src = pathlib.Path("services/store_events.py").read_text()
    bad = []
    for m in re.finditer(r'^(_\w*SQL)\s*=\s*"""(.*?)"""', src, re.S | re.M):
        for pm in re.finditer(r"%\((\w+)\)s(?!\s*::)", m.group(2)):
            bad.append(f"{m.group(1)}.{pm.group(1)}")
    assert not bad, "这些 SQL 参数没写显式 ::类型:" + ", ".join(bad)


class _Cur:
    """记录 execute 的假游标:rowcount 可编程,模拟同日去重的命中/未命中。"""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, args=None):
        self.conn.sqls.append((sql, args))
        if "INSERT INTO ops.store_events" in sql:
            key = (args["store"], args["event"], args["old"], args["new"])
            if key in self.conn.existing:
                self.rowcount = 0          # NOT EXISTS 拦下:同日同迁移已落过
            else:
                self.conn.existing.add(key)
                self.rowcount = 1
        else:
            self.rowcount = 1

    def fetchone(self):
        return self.conn.prev_row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, prev_row):
        self.prev_row = prev_row
        self.sqls: list = []
        self.existing: set = set()

    def cursor(self):
        return _Cur(self)


class _RoundCur:
    """收 executemany 的假游标(运营类走批量写,一轮一次)。"""

    def __init__(self, conn):
        self.conn = conn

    def executemany(self, sql, seq):
        self.conn.batches.append((sql, list(seq)))

    def execute(self, sql, args=None):
        raise AssertionError("运营类只准走 executemany 批量写")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RoundConn:
    def __init__(self):
        self.batches: list = []

    def cursor(self):
        return _RoundCur(self)


def test_record_round_skips_stores_with_nothing_to_report():
    """★ 计数全 0 的店不落行 —— 五条链每轮都遍历一批店,大半是"没它的货"。

    不滤掉的话账本每天多几十条全零行,风险与治理两类会被淹到查不出来。
    """
    conn = _RoundConn()
    n = se.record_round(conn, "list_new", se.LIST_ROUND, {
        "T1": {"submitted": 3, "no_upc": 0},
        "T2": {"submitted": 0, "no_upc": 0, "failed": 0},   # 没活干,不是事件
        "T3": {"submitted": 0, "failed": 2},
    })
    assert n == 2
    rows = conn.batches[0][1]
    assert [r[0] for r in rows] == ["T1", "T3"]      # 按店名定序,T2 不落
    assert {r[1] for r in rows} == {se.LIST_ROUND}
    assert {r[2] for r in rows} == {"info"}          # 运营类一律 info
    assert len(conn.batches) == 1, "多店必须一次 executemany,不是逐店 execute"


def test_record_round_counts_nested_buckets_and_flags():
    """维护/清理链按动作类分桶(计数嵌在第二层);match 的异常店没有计数,
    只有一个 True —— 那也是"这一轮发生了什么",照落。"""
    conn = _RoundConn()
    n = se.record_round(conn, "maintenance", se.MAINT_ROUND, {
        "T1": {"price": {"submitted": 0, "failed": 0}},          # 全零:不落
        "T2": {"price": {"submitted": 0}, "title": {"dedup": 2}},
        "T3": {"exception": True},
        "T4": {"retried": False, "delete": {"submitted": 0}},    # 全假:不落
    })
    assert n == 2
    assert [r[0] for r in conn.batches[0][1]] == ["T2", "T3"]


def test_record_round_lands_nothing_for_an_empty_round():
    conn = _RoundConn()
    assert se.record_round(conn, "list_new", se.LIST_ROUND, {}) == 0
    assert conn.batches == []


def test_ops_round_codes_are_all_classed_ops():
    for code in (se.LIST_ROUND, se.MAINT_ROUND, se.CLEANUP_ROUND,
                 se.CLEAR_ROUND, se.MATCH_ROUND):
        assert se.CLASS[code] == "ops"


def test_record_round_safe_never_takes_the_chain_down(monkeypatch):
    """★ 记账失败只告警 —— 货已经提交出去了,账本缺一轮可以补,整轮炸掉不行。

    但**失败必须见人**:摘要里要有一行(兜底静默常态化 = 主路径已坏没人知道)。
    """
    import contextlib

    from registry import db
    monkeypatch.setattr(db, "pg_conn",
                        contextlib.contextmanager(lambda **kw: iter([object()])))
    lines: list[str] = []
    assert se.record_round_safe("list_new", se.LIST_ROUND,
                                {"T1": {"submitted": 1}}, lines) == 0
    assert lines and "账本本轮没记上" in lines[0]


def test_record_kpi_diff_dedups_same_day_rerun():
    """daily_report 一天多轮(影刀下午补刷):同一迁移只落一次;
    当天内 A→B→A 的来回因 old/new 不同仍各落各的。"""
    conn = _Conn(prev_row=("ACTIVE", "ACTIVE", None))
    new = {"store_status": "SUSPENDED", "payment_status": "ACTIVE"}
    first = se.record_kpi_diff(conn, "T1", "2026-08-30", new)
    assert [e["event"] for e in first] == [se.STORE_STATUS_CHANGED]
    # 第二轮同样的迁移:NOT EXISTS 拦下,返回空(摘要不会重复报警)
    assert se.record_kpi_diff(conn, "T1", "2026-08-30", new) == []
    # 当天又弹回 ACTIVE(old/new 反过来)→ 新事件,照落
    conn.prev_row = ("SUSPENDED", "ACTIVE", None)
    back = se.record_kpi_diff(conn, "T1", "2026-08-30",
                              {"store_status": "ACTIVE"})
    assert [e["severity"] for e in back] == ["info"]
