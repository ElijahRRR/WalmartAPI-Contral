"""services/feed_effect 回归:feed 明细的实际结果(生效 / 未生效)。

所有者 2026-09-25 定稿:feed 结果与实际结果分开;实际结果只有生效 / 未生效,
「feed 显示成功、观测未生效」给人看,不挂任何自动化。
2026-09-26 改口径:只在期限后的第一次观测判、观测前同一参数又改过不判、库存不判。
"""

import contextlib
import json
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
            "submitted_at": sub, "due_at": due, "scanned_at": NOW, "overridden": False,
            "seen": True, "missing_since": None, "lifecycle_status": "ACTIVE",
            "last_seen_at": NOW, "price": 12.99, "product_name": "T", "gone": False,
            "action": None, "want": None}
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


def test_inventory_is_not_judged():
    """所有者 2026-09-26:「库存不观测结果」—— 订单随时在扣库存,快照对不上目标说明不了
    是 feed 没生效还是卖掉了。两种库存 feed 都不进候选,库存动作的目标值也不取。"""
    assert "inventory" not in fe._JUDGED_FEEDS and "MP_INVENTORY" not in fe._JUDGED_FEEDS
    assert "inventory" not in fe._VALUE_ACTIONS
    assert fe.verdict(_row("inventory", action="inventory", want="3")) is None
    assert all("inventory" not in g and "MP_INVENTORY" not in g
               for g in fe._SAME_PARAM.values())


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


def test_same_param_groups():
    """「观测前同一参数又改过就不判」的对照表:整条重写商品的上架 / 跟卖 / 改码算动过价格、
    标题与在不在架;单品 PUT 只有改价会盖掉 feed 的判定。"""
    assert set(fe._SAME_PARAM["price"]) == {"price", "PRICE_AND_PROMOTION", "MP_ITEM",
                                            "MP_ITEM_MATCH"}
    assert set(fe._SAME_PARAM["MP_MAINTENANCE"]) == {"MP_MAINTENANCE", "MP_ITEM",
                                                     "MP_ITEM_MATCH"}
    for ft in ("MP_ITEM", "MP_ITEM_MATCH", "RETIRE_ITEM", "DELETE_ITEM"):
        assert set(fe._SAME_PARAM[ft]) == {"MP_ITEM", "MP_ITEM_MATCH", "RETIRE_ITEM",
                                           "DELETE_ITEM"}
    assert set(fe._SAME_PARAM) == set(fe._JUDGED_FEEDS)             # 每种判的 feed 都在表里
    assert fe._PUT_PRICE_FEEDS == ("price", "PRICE_AND_PROMOTION")


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


# ── judge():窗口、计数与落库(假连接)──────────────────────────────────────────

class _Conn:
    """按 SQL 分派的假连接:观测水位 / 观测台账 / 候选 / 落库 / 写台账。"""

    def __init__(self, observed, cursor=None, rows=()):
        self.observed, self.cursor_value, self.rows = observed, cursor, list(rows)
        self.sqls: list = []
        self._last = ""

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))
        self._last = sql

    def executemany(self, sql, rows):
        self.sqls.append((sql, list(rows)))

    def fetchone(self):
        if self._last == fe._CURSOR_GET_SQL:
            return None if self.cursor_value is None else (self.cursor_value,)
        return None

    def fetchall(self):
        if self._last == fe._OBSERVED_SQL:
            return list(self.observed)
        if self._last == fe._CANDIDATES_SQL:
            return [tuple(r[c] for c in fe._COLS) for r in self.rows]
        return []

    def saved(self):
        """最后写进观测台账的值(没写过就是 None)。"""
        got = [a for s, a in self.sqls if s == fe._CURSOR_PUT_SQL]
        return json.loads(got[-1][1]) if got else None

    def ran_candidates(self):
        return [a for s, a in self.sqls if s == fe._CANDIDATES_SQL]


def test_first_observation_of_a_store_only_records_the_baseline():
    """某店第一次出现:只把这一次观测记成基线,一条都不判(不补判上线前的存量)。"""
    conn = _Conn(observed=[("T1", NOW)], cursor=None)
    out = fe.judge(conn)
    assert out["baseline"] == 1 and out[fe.EFFECTIVE] == out[fe.NOT_EFFECTIVE] == 0
    assert conn.ran_candidates() == []                          # 没有窗口就不查候选
    assert conn.saved() == {"T1": NOW.isoformat()}
    timeout, _ = conn.sqls[0]
    assert timeout == f"SET LOCAL statement_timeout = {fe.STATEMENT_TIMEOUT_S * 1000}"


def test_a_store_whose_observation_did_not_move_is_not_judged():
    """本轮没扫成的店水位停在上一轮 ⇒ 窗口是空的,不判;台账也不往回写。"""
    prev = NOW - timedelta(hours=18)
    conn = _Conn(observed=[("T1", prev)], cursor={"T1": prev.isoformat(),
                                                  "T2": prev.isoformat()})
    out = fe.judge(conn)
    assert conn.ran_candidates() == [] and not any(out.values())
    assert conn.saved() == {"T1": prev.isoformat(), "T2": prev.isoformat()}


def test_judge_counts_and_writes_each_verdict_once():
    """窗口 = (上一次观测, 这一次观测];盖掉的、错过的、没目标值的各自计数,判出来的各落一行。"""
    prev = NOW - timedelta(days=1)
    rows = [_row(action="price", want="12.99", sku="OK"),
            _row(action="price", want="10.00", sku="BAD"),
            _row(action="price", want="10.00", sku="BAD2", feed_status="overdue"),
            _row(action=None, sku="NOTARGET"),                      # 目标值不在库里
            _row(action="price", want="10.00", sku="OVER", overridden=True),
            _row("DELETE_ITEM", sku="MISSED",
                 last_seen_at=NOW - timedelta(days=4))]              # 这一轮没观测到
    conn = _Conn(observed=[("T1", NOW)], cursor={"T1": prev.isoformat()}, rows=rows)
    out = fe.judge(conn)
    assert out == {"effective": 1, "not_effective": 2, "review": 1, "overridden": 1,
                   "missed": 1, "no_target": 1, "baseline": 0}
    (args,) = conn.ran_candidates()
    assert (args["stores"], args["prev_at"], args["this_at"]) == (["T1"], [prev], [NOW])
    assert args["since"] == prev - timedelta(minutes=max(
        feed_track.FEED_DEADLINE_MINUTES.values()))               # 最早窗口 − 最长期限
    assert "failed" not in args["judgeable"]
    assert args["value_feeds"] == list(fe._VALUE_FEEDS)
    assert args["maint_actions"] == ["price", "title"]
    assert set(zip(args["same_ft"], args["same_other"])) == {
        (ft, o) for ft, g in fe._SAME_PARAM.items() for o in g}
    ins, params = next((s, a) for s, a in conn.sqls if s == fe._INSERT_SQL)
    assert "ON CONFLICT (feed_id, sku) DO NOTHING" in ins           # 判一次,不回头改
    assert sorted(p["sku"] for p in params) == ["BAD", "BAD2", "OK"]
    bad = next(p for p in params if p["sku"] == "BAD")
    assert (bad["effect"], bad["want"], bad["observed"]) == ("not_effective", "10.00", "12.99")
    assert conn.saved() == {"T1": NOW.isoformat()}                  # 下一轮从这里接着判


def test_candidates_sql_judges_only_the_first_observation_after_the_deadline():
    """口径钉在 SQL 上:落定期限落在 (上一次观测, 这一次观测] 才是候选;观测前同一参数又改过
    (后续 feed / 单品 PUT 改价)要标出来;不再有「回看 N 天」。"""
    q = " ".join(fe._CANDIDATES_SQL.split())
    assert "(%(deadlines)s::jsonb ->> f.feed_type)::int) > w.prev_at" in q
    assert "(%(deadlines)s::jsonb ->> f.feed_type)::int) <= w.this_at" in q
    assert "WHERE n.submitted_at > d.submitted_at AND n.submitted_at <= d.this_at" in q
    assert "x.feed_id = 'sync' AND x.action = 'price'" in q
    assert "p.executed_at > d.submitted_at AND p.executed_at <= d.this_at" in q
    assert "(o.feed_id IS NOT NULL) AS overridden" in q
    assert not hasattr(fe, "LOOKBACK_DAYS") and "days =>" not in q


def test_targets_are_joined_in_one_batch_not_probed_per_candidate():
    """2026-09-26 生产事故:目标值是逐候选 LATERAL `ORDER BY id DESC LIMIT 1` 去捞的,
    ops.dispositions 的 feed_id 上没有索引,1.88 万个候选各把 65.8 万行扫一遍,日报链卡
    44 分钟。目标值只许整批 join(disp CTE),且只给值比对类候选取;索引必须在 schema 里。
    后续提交与单品 PUT 也先按 since 截在窗口期内,走各自的时间索引。"""
    import pathlib
    sql = " ".join(fe._CANDIDATES_SQL.split())
    assert "LATERAL" not in sql and "LIMIT" not in sql
    assert ("SELECT DISTINCT ON (x.feed_id, x.sku) x.feed_id, x.sku, x.action, x.detail"
            " FROM due d JOIN ops.dispositions x") in sql
    assert "WHERE d.feed_type = ANY(%(value_feeds)s::text[])" in sql
    assert sql.count("> %(since)s::timestamptz") == 3             # 候选 / 后续 feed / PUT
    schema = (pathlib.Path(__file__).resolve().parent.parent
              / "refdata" / "schema.sql").read_text(encoding="utf-8")
    assert ("CREATE INDEX IF NOT EXISTS dispositions_feed_sku_idx\n"
            "    ON ops.dispositions (feed_id, sku) WHERE feed_id IS NOT NULL;") in schema
    assert "CREATE INDEX IF NOT EXISTS dispositions_executed_at_idx" in schema


def test_judge_only_reads_the_feed_ledger_and_writes_its_own_table():
    """两本账互不写对方:本模块不改 ops.feed_items,只写 ops.feed_effects(+自己的观测台账)。"""
    import inspect
    src = inspect.getsource(fe)
    assert "UPDATE ops.feed_items" not in src and "INSERT INTO ops.feed_items" not in src
    assert "INSERT INTO ops.feed_effects" in src
    assert "UPDATE ops.dispositions" not in src


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


def _price_disp(cur, sku, fid, new, status, hours_ago=None):
    cur.execute("INSERT INTO ops.dispositions (store, sku, source, action, status, feed_id,"
                " executed_at, detail) VALUES (%s, %s, 'maint', 'price', %s, %s,"
                " now() - make_interval(hours => %s), jsonb_build_object('new', %s::text))",
                (_ST, sku, status, fid, hours_ago or 0, new))


def _set_prev(cur, hours_ago):
    """观测台账里本店的"上一次观测"= now() − hours_ago(与事务内 now() 同一时钟)。"""
    cur.execute("SELECT now() - make_interval(hours => %s)", (hours_ago,))
    at = cur.fetchone()[0]
    cur.execute(fe._CURSOR_PUT_SQL, (fe.CURSOR, json.dumps({_ST: at.isoformat()})))


def _effects(cur):
    cur.execute("SELECT feed_id, effect, feed_status, want, observed"
                " FROM ops.feed_effects WHERE store = %s ORDER BY feed_id", (_ST,))
    return {r[0]: r[1:] for r in cur.fetchall()}


@needs_pg
def test_effects_on_a_real_database(pg):
    """真库一轮(上一次观测 = 30 小时前,这一次 = 现在):
    判得出的各落一行;失败回执不判;观测前被同参数后续 feed / 单品 PUT 盖掉的不判;库存不判;
    期限落在窗口外的(还没到期 / 上一轮就该判)不判;第二轮窗口为空一行都不重判;
    复核清单只出「成功 ∧ 未生效」。"""
    with pg.cursor() as cur:
        _set_prev(cur, 30)
        _item(cur, "FE_P1", "P_OK", "price", "success", 20)
        _item(cur, "FE_P2", "P_BAD", "price", "success", 20)
        _item(cur, "FE_P3", "P_FAIL", "price", "failed", 20)          # 沃尔玛拒了:不判
        _item(cur, "FE_OLD", "P_SUP", "price", "success", 25)         # 被下一条覆盖
        _item(cur, "FE_NEW", "P_SUP", "price", "success", 22)
        _item(cur, "FE_PUT", "P_PUT", "price", "success", 20)         # 被单品 PUT 覆盖
        _item(cur, "FE_I1", "I_1", "inventory", "success", 20)        # 库存:不判
        _item(cur, "FE_D1", "D_STILL", "DELETE_ITEM", "success", 100, "product_clear")
        _item(cur, "FE_D2", "D_YOUNG", "DELETE_ITEM", "success", 10, "product_clear")
        _item(cur, "FE_D3", "D_EARLY", "DELETE_ITEM", "success", 110, "product_clear")
        _item(cur, "FE_L1", "L_NEW", "MP_ITEM", "overdue", 30, "list_new")
        # 同 (店, SKU, 动作) 只许一条未落定(dispositions_open_uidx):盖掉的那条已落定
        _price_disp(cur, "P_OK", "FE_P1", "9.99", "executing", 20)
        _price_disp(cur, "P_BAD", "FE_P2", "8.00", "executing", 20)
        _price_disp(cur, "P_SUP", "FE_OLD", "7.00", "confirmed", 25)
        _price_disp(cur, "P_SUP", "FE_NEW", "6.00", "executing", 22)
        _price_disp(cur, "P_PUT", "FE_PUT", "5.00", "confirmed", 20)
        _price_disp(cur, "P_PUT", "sync", "4.00", "executing", 10)    # 之后又走了一次 PUT
        for sku, price in (("P_OK", 9.99), ("P_BAD", 9.99), ("P_SUP", 6.00),
                           ("P_PUT", 4.00), ("I_1", None), ("D_STILL", None),
                           ("D_YOUNG", None), ("D_EARLY", None), ("L_NEW", None)):
            _seen(cur, sku, price)
    out = fe.judge(pg, store=_ST)
    with pg.cursor() as cur:
        got = _effects(cur)
    assert got == {
        "FE_D1": ("not_effective", "success", None, "仍在架"),
        "FE_L1": ("effective", "overdue", None, "在架"),
        "FE_NEW": ("effective", "success", "6.00", "6.00"),
        "FE_P1": ("effective", "success", "9.99", "9.99"),
        "FE_P2": ("not_effective", "success", "8.00", "9.99"),
    }   # FE_P3 失败不判;FE_OLD / FE_PUT 被盖;FE_I1 库存;FE_D2 未到期;FE_D3 上一轮就该判
    assert (out["review"], out["not_effective"], out["overridden"], out["baseline"]) == (
        2, 2, 2, 0)
    again = fe.judge(pg, store=_ST)                     # 第二轮:窗口为空,一行都不重判
    assert not any(again.values())
    review = fe.review_rows(pg, days=7, store=_ST)
    assert sorted(r["feed_id"] for r in review) == ["FE_D1", "FE_P2"]


@needs_pg
def test_a_new_store_is_baselined_on_a_real_database(pg):
    """台账里没有这家店:这一轮只记基线,一条都不判;下一轮才从基线往后判。"""
    with pg.cursor() as cur:
        cur.execute("DELETE FROM ops.cursors WHERE name = %s", (fe.CURSOR,))
        _item(cur, "FE_B1", "B_OK", "DELETE_ITEM", "success", 100, "product_clear")
        _seen(cur, "B_OK")
    out = fe.judge(pg, store=_ST)
    assert out["baseline"] == 1 and out[fe.NOT_EFFECTIVE] == 0
    with pg.cursor() as cur:
        assert _effects(cur) == {}
        cur.execute(fe._CURSOR_GET_SQL, (fe.CURSOR,))
        assert list(cur.fetchone()[0]) == [_ST]


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
    """附属步骤:炸了只报一行不拖垮同步;空跑同事务 rollback(判决与观测台账一起回滚)。"""
    from workflows import catalog_sync

    class _C:
        rolled = False

        def rollback(self):
            _C.rolled = True

    monkeypatch.setattr(catalog_sync.db, "pg_conn",
                        lambda *a, **k: contextlib.nullcontext(_C()))
    monkeypatch.setattr(catalog_sync.feed_effect, "judge", lambda conn, store=None: {
        "effective": 3, "not_effective": 2, "review": 1, "overridden": 4, "missed": 5,
        "no_target": 0, "baseline": 0})
    line = catalog_sync._judge_effects(None, dry_run=True)
    assert _C.rolled and line.startswith("实际结果(空跑未落库):将判 生效 3,未生效 2")
    assert "feed 成功但未生效 1 条" in line and "feed_poll -p review=1" in line
    assert "观测前同一参数又改过、不判 4" in line
    assert "期限后这一轮没观测到、不再补判 5" in line
    monkeypatch.setattr(catalog_sync.feed_effect, "judge", lambda conn, store=None: {
        "effective": 0, "not_effective": 0, "review": 0, "overridden": 0, "missed": 0,
        "no_target": 0, "baseline": 7})
    assert "首次记观测基线 7 店(下一轮起判)" in catalog_sync._judge_effects(None, False)

    def _boom(conn, store=None):
        raise RuntimeError("relation ops.feed_effects does not exist")

    monkeypatch.setattr(catalog_sync.feed_effect, "judge", _boom)
    assert catalog_sync._judge_effects(None, dry_run=False).startswith(
        "⚠ 实际结果判定失败(不影响目录同步,下轮再判)")
