"""services/feed_effect 回归:feed 明细的实际结果(生效 / 未生效)。

所有者 2026-09-25 定稿:feed 结果与实际结果分开;实际结果只有生效 / 未生效,
「feed 显示成功、观测未生效」给人看,不挂任何自动化。
"""

import contextlib
import os
import socket
from datetime import datetime, timedelta, timezone

import pytest

from services import feed_effect as fe
from services import feed_track

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _row(feed_type="price", **kw):
    """一行候选(_CANDIDATES_SQL 的列),缺省 = 期限已过 1 天、店刚扫过、SKU 刚观测到。"""
    limit = timedelta(minutes=feed_track.FEED_DEADLINE_MINUTES[feed_type])
    sub = NOW - limit - timedelta(days=1)
    due = sub + limit
    base = {"feed_id": "F1", "sku": "S1", "store": "T1", "feed_type": feed_type,
            "workflow": "maintenance", "feed_status": "success",
            "submitted_at": sub, "due_at": due, "scanned_at": NOW,
            "seen": True, "missing_since": None, "lifecycle_status": "ACTIVE",
            "last_seen_at": NOW, "price": 12.99, "avail_qty": 5,
            "product_name": "T", "gone": False, "action": None, "want": None,
            "ship_node": None, "node_qty": None}
    base.update(kw)
    return base


# ── 判词(纯函数)──────────────────────────────────────────────────────────────

def test_price_uses_the_maintenance_value_check():
    """改价复用维护链落定的同一个现值比对(maint_effective),不另写第二份。"""
    ok = fe.verdict(_row(action="price", want="12.99"))
    assert ok[0] == fe.EFFECTIVE and ok[1] == "12.99" and "现值比对" in ok[2]
    bad = fe.verdict(_row(action="price", want="10.00"))
    assert bad[0] == fe.NOT_EFFECTIVE


def test_not_effective_needs_an_observation_after_the_deadline():
    """未生效只认期限之后的观测:期限内没变不算没生效。"""
    r = _row(action="price", want="10.00")
    r["last_seen_at"] = r["due_at"] - timedelta(minutes=1)
    assert fe.verdict(r) is None


def test_inventory_on_a_managed_node_compares_that_node():
    """受管仓:看**该节点**的现值;节点期限后还没扫到 ⇒ 判不了(拿合计比就是错比)。"""
    r = _row("inventory", action="inventory", want="3", ship_node="FC1",
             node_qty=3, avail_qty=9)
    assert fe.verdict(r)[0] == fe.EFFECTIVE
    assert fe.verdict(dict(r, node_qty=None)) is None


def test_title_compares_the_product_name():
    r = _row("MP_MAINTENANCE", action="title", want="New T", product_name="New T")
    assert fe.verdict(r)[:2] == (fe.EFFECTIVE, "New T")


def test_listing_is_effective_once_the_sku_shows_up():
    r = _row("MP_ITEM", workflow="list_new")
    assert fe.verdict(r)[:2] == (fe.EFFECTIVE, "在架")
    gone = _row("MP_ITEM", seen=False, last_seen_at=None)
    assert fe.verdict(gone)[:2] == (fe.NOT_EFFECTIVE, "缺席")
    # 期限后该店还没扫过:缺席说明不了任何事
    stale = dict(gone, scanned_at=gone["due_at"] - timedelta(hours=1))
    assert fe.verdict(stale) is None


def test_retire_and_delete():
    assert fe.verdict(_row("RETIRE_ITEM", lifecycle_status="RETIRED"))[0] == fe.EFFECTIVE
    assert fe.verdict(_row("RETIRE_ITEM"))[0] == fe.NOT_EFFECTIVE
    assert fe.verdict(_row("RETIRE_ITEM", seen=False)) is None      # 从没见过
    assert fe.verdict(_row("DELETE_ITEM", gone=True))[0] == fe.EFFECTIVE
    still = _row("DELETE_ITEM")
    assert fe.verdict(still)[0] == fe.NOT_EFFECTIVE
    early = dict(still, last_seen_at=still["due_at"] - timedelta(hours=1))
    assert fe.verdict(early) is None                                # 72 小时内不判未生效


def test_the_delete_gone_rule_is_the_one_delete_verification_uses():
    """"已经不在了"只有一份口径:删除核验与实际结果共用 product_events.GONE_SQL。"""
    from services import product_events
    assert product_events.GONE_SQL in product_events._VERIFY_SQL
    assert product_events.GONE_SQL in fe._CANDIDATES_SQL


def test_delete_verification_grace_is_the_delete_deadline(monkeypatch):
    """删除核验的"未生效"宽限对齐删除落定期限(官方最长 72 小时),不再各说各的。"""
    from services import product_events

    class _C:
        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, args=None):
            self.args = args

        def fetchall(self):
            return []

    c = _C()
    monkeypatch.setattr(product_events, "record_many", lambda conn, evs: 0)
    product_events.verify_deletions(c)
    assert c.args == (72,)


# ── judge():计数与落库 ─────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, rows):
        self.rows, self.sqls = rows, []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))

    def executemany(self, sql, rows):
        self.sqls.append((sql, list(rows)))

    def fetchall(self):
        return [tuple(r[c] for c in fe._COLS) for r in self.rows]


def test_judge_counts_and_writes_each_verdict_once():
    rows = [_row(action="price", want="12.99", sku="OK"),
            _row(action="price", want="10.00", sku="BAD"),
            _row(action="price", want="10.00", sku="BAD2", feed_status="overdue"),
            _row(action=None, sku="NOTARGET"),                      # 目标值不在库里
            _row("DELETE_ITEM", sku="WAIT",
                 last_seen_at=NOW - timedelta(days=2))]              # 期限后还没观测
    conn = _Conn(rows)
    out = fe.judge(conn)
    assert out == {"effective": 1, "not_effective": 2, "review": 1,
                   "waiting": 1, "no_target": 1}
    sql, args = conn.sqls[0]
    assert args["days"] == fe.LOOKBACK_DAYS and "failed" not in args["judgeable"]
    ins, params = conn.sqls[-1]
    assert "ON CONFLICT (feed_id, sku) DO NOTHING" in ins           # 判一次,不回头改
    assert sorted(p["sku"] for p in params) == ["BAD", "BAD2", "OK"]
    bad = next(p for p in params if p["sku"] == "BAD")
    assert (bad["effect"], bad["want"], bad["observed"]) == ("not_effective", "10.00", "12.99")


def test_judge_only_reads_the_feed_ledger_and_writes_its_own_table():
    """两本账互不写对方:本模块不改 ops.feed_items,只写 ops.feed_effects。"""
    import inspect
    src = inspect.getsource(fe)
    assert "UPDATE ops.feed_items" not in src and "INSERT INTO ops.feed_items" not in src
    assert "INSERT INTO ops.feed_effects" in src


# ── 真库 ─────────────────────────────────────────────────────────────────────────
# ⚠ 测试夹具地址(非标准端口 55432,不可能连到生产库);整场事务最后一律回滚。

_DSN = os.environ.get(
    "WALMART_TEST_PG_DSN", "host=127.0.0.1 port=55432 user=postgres dbname=walmart_data")


def _pg_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 55432), timeout=1):
            return True
    except OSError:
        return False


needs_pg = pytest.mark.skipif(not _pg_up(), reason="沙箱 PG 127.0.0.1:55432 未启动")
_ST = "FE_SANDBOX_T1"


@pytest.fixture
def pg(monkeypatch):
    monkeypatch.setenv("WALMART_PG_DSN", _DSN)
    from registry import db
    with db.pg_conn() as conn:
        try:
            yield conn
        finally:
            conn.rollback()


def _item(cur, fid, sku, ft, status, hours_ago, wf="maintenance"):
    cur.execute("INSERT INTO ops.feed_items (feed_id, sku, workflow, store, feed_type,"
                " status, submitted_at) VALUES (%s, %s, %s, %s, %s, %s,"
                " now() - make_interval(hours => %s))",
                (fid, sku, wf, _ST, ft, status, hours_ago))


def _seen(cur, sku, price=None, lifecycle="ACTIVE", missing=False):
    cur.execute("INSERT INTO catalog.walmart_items (store, sku, price, lifecycle_status,"
                " last_seen_at, missing_since) VALUES (%s, %s, %s, %s, now(),"
                " CASE WHEN %s THEN now() END)", (_ST, sku, price, lifecycle, missing))


@needs_pg
def test_effects_on_a_real_database(pg):
    """真库一轮:判得出的各落一行;失败回执不判;被覆盖的旧明细不判;期限未到不判;
    第二轮一行都不重复(判一次不回头改);复核清单只出「成功 ∧ 未生效」与"没给结论"。"""
    with pg.cursor() as cur:
        _item(cur, "FE_P1", "P_OK", "price", "success", 30)
        _item(cur, "FE_P2", "P_BAD", "price", "success", 30)
        _item(cur, "FE_P3", "P_FAIL", "price", "failed", 30)          # 沃尔玛拒了:不判
        _item(cur, "FE_OLD", "P_SUP", "price", "success", 50)         # 被下一条覆盖
        _item(cur, "FE_NEW", "P_SUP", "price", "success", 30)
        _item(cur, "FE_D1", "D_STILL", "DELETE_ITEM", "success", 100, "product_clear")
        _item(cur, "FE_D2", "D_YOUNG", "DELETE_ITEM", "success", 10, "product_clear")
        _item(cur, "FE_L1", "L_NEW", "MP_ITEM", "overdue", 30, "list_new")
        # 同 (店, SKU, 动作) 只许一条未落定(dispositions_open_uidx):被覆盖那次已落定
        for fid, sku, new, st in (("FE_P1", "P_OK", "9.99", "executing"),
                                  ("FE_P2", "P_BAD", "8.00", "executing"),
                                  ("FE_OLD", "P_SUP", "7.00", "confirmed"),
                                  ("FE_NEW", "P_SUP", "6.00", "executing")):
            cur.execute("INSERT INTO ops.dispositions (store, sku, source, action,"
                        " status, feed_id, detail) VALUES (%s, %s, 'maint', 'price',"
                        " %s, %s, jsonb_build_object('new', %s::text))",
                        (_ST, sku, st, fid, new))
        _seen(cur, "P_OK", 9.99)
        _seen(cur, "P_BAD", 9.99)
        _seen(cur, "P_SUP", 6.00)
        _seen(cur, "D_STILL")
        _seen(cur, "D_YOUNG")
        _seen(cur, "L_NEW")
    out = fe.judge(pg, store=_ST)
    with pg.cursor() as cur:
        cur.execute("SELECT feed_id, effect, feed_status, want, observed"
                    " FROM ops.feed_effects WHERE store = %s ORDER BY feed_id", (_ST,))
        got = {r[0]: r[1:] for r in cur.fetchall()}
    assert got == {
        "FE_D1": ("not_effective", "success", None, "仍在架"),
        "FE_L1": ("effective", "overdue", None, "在架"),
        "FE_NEW": ("effective", "success", "6.00", "6.00"),
        "FE_P1": ("effective", "success", "9.99", "9.99"),
        "FE_P2": ("not_effective", "success", "8.00", "9.99"),
    }                                   # FE_P3 失败不判,FE_OLD 被覆盖,FE_D2 未到 72h
    assert out["review"] == 2 and out["not_effective"] == 2
    assert fe.judge(pg, store=_ST)[fe.EFFECTIVE] == 0        # 第二轮:一行都不重判
    review = fe.review_rows(pg, days=7, store=_ST)
    assert sorted(r["feed_id"] for r in review) == ["FE_D1", "FE_P2"]


def test_review_list_formatting(monkeypatch):
    """复核清单:首行两档计数、按 (店, 动作) 分组、每条带目标 / 观测 / 观测时刻 / feed 码。"""
    from workflows import feed_poll
    seen = datetime(2026, 9, 25, 6, 5, tzinfo=timezone.utc)
    rows = [{"store": "T1", "feed_type": "price", "workflow": "maintenance",
             "feed_id": "F_PRICE_1", "sku": "S1", "feed_status": "success",
             "want": "8.00", "observed": "9.99", "observed_at": seen,
             "basis": "x", "judged_at": seen},
            {"store": "T2", "feed_type": "DELETE_ITEM", "workflow": "product_clear",
             "feed_id": "F_DEL_1", "sku": "S2", "feed_status": "overdue",
             "want": None, "observed": "仍在架", "observed_at": None,
             "basis": "x", "judged_at": seen}]
    monkeypatch.setattr(feed_poll.db, "pg_conn",
                        lambda *a, **k: contextlib.nullcontext(object()))
    monkeypatch.setattr(feed_poll.feed_effect, "review_rows",
                        lambda conn, days, store: rows)
    out = feed_poll.run({"review": "1", "execute": True})
    lines = out.splitlines()
    assert lines[0].startswith("实际结果复核清单(近 7 天判定):feed 成功但未生效 1 条;"
                               "沃尔玛没给结论且未生效 1 条")
    assert "T1 改价(maintenance)×1:" in out
    assert "S1 目标 8.00 → 观测 9.99(观测于 09-25 06:05,成功,feed F_PRICE_1)" in out
    assert "S2 观测 仍在架(观测于 ?,超期未完成,feed F_DEL_1)" in out
    monkeypatch.setattr(feed_poll.feed_effect, "review_rows",
                        lambda conn, days, store: [])
    assert "没有" in feed_poll.run({"review": "1", "execute": True})


def test_catalog_sync_effect_step_is_isolated_and_dry_run_rolls_back(monkeypatch):
    """附属步骤:炸了只报一行不拖垮同步;空跑同事务 rollback(判一次不回头改)。"""
    from workflows import catalog_sync

    class _C:
        rolled = False

        def rollback(self):
            _C.rolled = True

    monkeypatch.setattr(catalog_sync.db, "pg_conn",
                        lambda *a, **k: contextlib.nullcontext(_C()))
    monkeypatch.setattr(catalog_sync.feed_effect, "judge", lambda conn, store=None: {
        "effective": 3, "not_effective": 2, "review": 1, "waiting": 4, "no_target": 0})
    line = catalog_sync._judge_effects(None, dry_run=True)
    assert _C.rolled and line.startswith("实际结果(空跑未落库):将判 生效 3,未生效 2")
    assert "feed 成功但未生效 1 条" in line and "feed_poll -p review=1" in line

    def _boom(conn, store=None):
        raise RuntimeError("relation ops.feed_effects does not exist")

    monkeypatch.setattr(catalog_sync.feed_effect, "judge", _boom)
    assert catalog_sync._judge_effects(None, dry_run=False).startswith(
        "⚠ 实际结果判定失败(不影响目录同步,下轮再判)")
