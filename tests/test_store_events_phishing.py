"""钓鱼订单入账回归(services/store_events.record_line_events + order_audit 钩子)。

钉四件事:
  ① **按订单行去重**:order_audit 每轮把窗口内的行重判一遍(`-p wait=1` 时
     同一批还判两遍),写入口必须自己幂等;一单扇 N 店时**每店一行**,
     去重键是 (码, 店, order_line_id) 三样;
  ② **三种降级只记收单店、绝不展开**:拿不准品牌就展开 = 随机标店;
  ③ **补记自愈**:_save 自己 commit、事件写在它之后,中间崩一次那条钓鱼结论
     就永远留在订单表里而账本一无所知 —— 每轮重扫窗口把漏的捞回来;
  ④ 摘要**首行**带 `🚨 钓鱼命中 N`:首行是链通知折叠的唯一出口。

沙箱 PG 集成用例在文件末尾:连不上就 skip,不让无 PG 的环境变红。
"""

import os
import socket

import pytest

from services import order_audit as rules, store_events as se
from workflows import order_audit as oa


# ── 假连接 ───────────────────────────────────────────────────────────────────

class _Cur:
    """按表名派发假结果;INSERT 记 rowcount(去重靠 conn.seen)。"""

    def __init__(self, conn):
        self.conn = conn
        self._rows = []
        self._cols = []
        self.rowcount = 0

    @property
    def description(self):
        return [(c,) for c in self._cols]

    def execute(self, sql, args=None):
        self.conn.calls.append((sql, args))
        self._rows, self._cols = [], []
        if "INSERT INTO ops.store_events" in sql:
            key = (args["event"], args["store"], args["line"])
            self.rowcount = 0 if key in self.conn.seen else 1
            self.conn.seen.add(key)
            self.conn.landed.append(args)
            return
        for table, (cols, rows) in self.conn.rows.items():
            if table in sql:
                self._cols, self._rows = cols, rows
                break

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.calls: list = []
        self.seen: set = set()
        self.landed: list = []

    def cursor(self):
        return _Cur(self)


def _line(**kw):
    row = {"order_line_id": "L1", "store": "A085", "sku": "B0PHISH0001",
           "asin": "B0PHISH0001", "po_id": "1234567890123",
           "order_date": "2026-08-29", "postal_code": "60606"}
    row.update(kw)
    return row


class _Res:
    """判定结果替身:只需要 is_phishing 与 detail 两样。"""

    def __init__(self, zip5="60606", phishing=True):
        self.detail = {"rules": {"phishing": {"hit": phishing, "zip": zip5}}}
        self.is_phishing = phishing


# ── record_line_events:去重与 fail-loud ─────────────────────────────────────

def _row(line="L1", store="A085", event=None, extra=None):
    return {"store": store, "event": event or se.PHISHING_ORDER,
            "severity": "high", "source": "order_audit",
            "detail": {"order_line_id": line, **(extra or {})}}


def test_same_order_line_rerun_lands_nothing_new():
    """每轮重判同一批行:第二遍一行都不该新增(NOT EXISTS 拦下)。"""
    conn = _Conn()
    assert len(se.record_line_events(conn, [_row()])) == 1
    assert se.record_line_events(conn, [_row()]) == []


def test_one_order_fans_out_to_two_stores_one_row_each():
    """一单扇 N 店:店不同 = 不同的事实,各落各的;再跑一遍还是零新增。"""
    conn = _Conn()
    rows = [_row(store=s, event=se.PHISHING_BRAND_EXPOSURE)
            for s in ("A085", "B012")]
    landed = se.record_line_events(conn, rows)
    assert [r["store"] for r in landed] == ["A085", "B012"]
    assert se.record_line_events(conn, rows) == []


def test_dedup_key_uses_is_not_distinct_from_for_null_store():
    """store 可能为 NULL(全局源头行);`=` 比 NULL 永远不 TRUE ⇒ 每轮重复插。"""
    assert "store IS NOT DISTINCT FROM %(store)s::text" in \
        se._INSERT_LINE_DEDUP_SQL
    conn = _Conn()
    assert len(se.record_line_events(conn, [_row(store=None)])) == 1
    assert se.record_line_events(conn, [_row(store=None)]) == []


def test_missing_order_line_id_is_fail_loud():
    """缺 order_line_id 的行会绕过去重每轮插一条,事后没法认出来 —— 宁炸不吞。"""
    bad = {"store": "A085", "event": se.PHISHING_ORDER, "severity": "high",
           "source": "order_audit", "detail": {"po_id": "x"}}
    with pytest.raises(ValueError, match="order_line_id"):
        se.record_line_events(_Conn(), [bad])


def test_unregistered_code_still_fails_loud_on_this_entrance():
    with pytest.raises(ValueError, match="未登记"):
        se.record_line_events(_Conn(), [_row(event="typo_event")])


# ── 钩子:三种降级只记收单店 ───────────────────────────────────────────────

def _record(monkeypatch, conn, cands, expand=(("B012", True),)):
    """跑一遍 _phish_record,展开结果由参数给(飞书与追溯都不真调)。"""
    monkeypatch.setattr(oa.stores_svc, "registered_names", lambda: {"A085", "B012"})
    monkeypatch.setattr(oa.risk_trace, "stores_of_brand",
                        lambda *a, **k: ("bk", ["B0PHISH0001"],
                                         {s: {"evidence": ["在架表"],
                                              "still_listed": listed,
                                              "asins": ["B0PHISH0001"]}
                                          for s, listed in expand}))
    return oa._phish_record(conn, cands, "order_audit")


def _products(rows):
    return {"catalog.products": (("asin", "brand", "manufacturer"), rows)}


def test_normal_path_records_origin_store_and_expands(monkeypatch):
    conn = _Conn(_products([("B0PHISH0001", "Zqx Phish", None)]))
    landed, n_expo = _record(monkeypatch, conn,
                             oa._phish_cands([(_line(), _Res())]))
    assert len(landed) == 1 and n_expo == 1
    d = landed[0]["detail"]
    assert landed[0]["store"] == "A085" and landed[0]["severity"] == "high"
    assert d["zip"] == "60606" and d["po_id"] == "1234567890123"
    assert d["brand"] == "Zqx Phish" and d["asin"] == "B0PHISH0001"


def test_asin_missing_records_origin_store_only(monkeypatch):
    """订单行没 ASIN 列、SKU 也解不出标准码 → 只记收单店。"""
    conn = _Conn(_products([]))
    monkeypatch.setattr(oa.risk_trace, "stores_of_brand",
                        lambda *a, **k: pytest.fail("拿不到 ASIN 不该展开"))
    landed, n_expo = oa._phish_record(
        conn, oa._phish_cands([(_line(asin=None, sku="裸订货号"), _Res())]),
        "order_audit")
    assert n_expo == 0 and landed[0]["detail"]["asin_missing"] is True
    assert "brand" not in landed[0]["detail"]


def test_brand_missing_records_origin_store_only(monkeypatch):
    """产品中心还没这个 ASIN(没采回来)→ 只记收单店,等下轮。"""
    conn = _Conn(_products([]))
    monkeypatch.setattr(oa.risk_trace, "stores_of_brand",
                        lambda *a, **k: pytest.fail("查不到产品不该展开"))
    landed, n_expo = oa._phish_record(conn,
                                      oa._phish_cands([(_line(), _Res())]),
                                      "order_audit")
    assert n_expo == 0 and landed[0]["detail"]["brand_missing"] is True


def test_placeholder_brand_records_origin_store_only(monkeypatch):
    """brand/manufacturer 都是占位符:按 "OEM" 展开 = 大面积误标。"""
    conn = _Conn(_products([("B0PHISH0001", "Generic", "OEM")]))
    monkeypatch.setattr(oa.risk_trace, "stores_of_brand",
                        lambda *a, **k: pytest.fail("占位符品牌不该展开"))
    landed, n_expo = oa._phish_record(conn,
                                      oa._phish_cands([(_line(), _Res())]),
                                      "order_audit")
    assert n_expo == 0 and landed[0]["detail"]["brand_placeholder"] is True


def test_manufacturer_is_used_when_brand_is_a_placeholder(monkeypatch):
    """brand=Generic 但 manufacturer 是真品牌 → 照展开(brand_key 同一口径)。"""
    conn = _Conn(_products([("B0PHISH0001", "Generic", "Zqx Phish")]))
    landed, n_expo = _record(monkeypatch, conn,
                             oa._phish_cands([(_line(), _Res())]))
    assert n_expo == 1 and landed[0]["detail"]["brand"] == "Zqx Phish"


def test_feishu_failure_degrades_to_unchecked_expansion(monkeypatch):
    """飞书挂了不把展开整段丢掉:标 registered_unchecked,别让人以为是终判。"""
    conn = _Conn(_products([("B0PHISH0001", "Zqx Phish", None)]))

    def _boom():
        raise RuntimeError("飞书 503")

    monkeypatch.setattr(oa.stores_svc, "registered_names", _boom)
    monkeypatch.setattr(oa.risk_trace, "stores_of_brand",
                        lambda *a, **k: ("bk", [], {"B012": {
                            "evidence": ["占用-品牌"], "still_listed": False,
                            "asins": []}}))
    _landed, n_expo = oa._phish_record(conn,
                                       oa._phish_cands([(_line(), _Res())]),
                                       "order_audit")
    assert n_expo == 1
    expo = [a for a in conn.landed
            if a["event"] == se.PHISHING_BRAND_EXPOSURE]
    assert '"registered_unchecked": true' in expo[0]["detail"]


def test_only_phishing_lines_become_candidates():
    ok = [(_line(order_line_id="L1"), _Res()),
          (_line(order_line_id="L2"), _Res(phishing=False))]
    assert [c["order_line_id"] for c in oa._phish_cands(ok)] == ["L1"]


def test_zip_comes_from_judge_not_recomputed():
    """judge 已经算好了邮编,重算就是第二套归一(改了口径两边各说各的)。"""
    cands = oa._phish_cands([(_line(postal_code="99999-1234"),
                              _Res(zip5="60606"))])
    assert cands[0]["zip"] == "60606"


def test_zip_falls_back_to_postal_code_for_legacy_rows():
    """历史导入的行没有 rules.phishing 结构 —— 回落收件邮编现算(zip+4 取前 5)。"""
    assert oa._phish_zip({}, "60606-6771") == "60606"
    assert oa._phish_zip({"rules": {}}, "60606") == "60606"


# ── 摘要:短标记进首行 ─────────────────────────────────────────────────────

def test_mark_and_note_shapes():
    landed = [_row(line=f"L{i}", store=f"S{i}",
                   extra={"po_id": f"99999{i:08d}", "zip": "60606"})
              for i in range(7)]
    assert oa._phish_mark(landed) == "🚨 钓鱼命中 7"
    note = oa._phish_note(landed, 4)
    assert note.startswith("🚨 钓鱼命中 7 行:S0/")
    assert note.count("、") == oa._PHISH_SHOW - 1 and "…" in note
    assert note.endswith(";品牌波及 4 店")
    assert "(邮编 60606)" in note
    # 零命中:一个字都不该出现(notify_fmt 规矩 2)
    assert oa._phish_mark([]) == "" and oa._phish_note([], 0) == ""


def test_note_without_exposure_has_no_tail():
    landed = [_row(extra={"po_id": "1234567890123", "zip": "60606"})]
    assert oa._phish_note(landed, 0) == "🚨 钓鱼命中 1 行:A085/67890123(邮编 60606)"


# ── 沙箱 PG 集成 ────────────────────────────────────────────────────────────
#
# ⚠ 地址是**测试夹具**(生产走 registry/db.pg_dsn() 的 unix socket);非标准
# 端口 55432 正是为了不可能连到生产库。本节造数据,整场事务最后回滚。
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

_L1, _L2 = "zqx-phish-line-1", "zqx-phish-line-2"
_PA = "B0PHISHPG01"


@pytest.fixture
def pg(monkeypatch):
    """输入:无 → 输出:沙箱 PG 连接(整场事务**最后一律回滚**)。"""
    monkeypatch.setenv("WALMART_PG_DSN", _DSN)
    from registry import db
    with db.pg_conn() as conn:
        try:
            yield conn
        finally:
            conn.rollback()


def _seed_lines(conn):
    """输入:连接 → 输出:无。两条已判钓鱼的订单行 + 一件同品牌的在架商品。"""
    detail = ('{"rules": {"phishing": {"hit": true, "zip": "60606"}},'
              ' "note": "钓鱼邮编:60606"}')
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO orders.order_lines (order_line_id, store, po_id,"
            " line_number, sku, asin, postal_code, order_date, audit_status,"
            " audit_detail) VALUES"
            " (%s, 'A085', '99900011122233', '1', %s, %s, '60606', now(),"
            "  '建议拒绝', %s::jsonb),"
            " (%s, 'B012', '99900011122244', '1', %s, %s, '60606', now(),"
            "  '建议拒绝', %s::jsonb)",
            (_L1, _PA, _PA, detail, _L2, _PA, _PA, detail))
        cur.execute(
            "INSERT INTO catalog.products (marketplace, asin, brand) "
            "VALUES ('US', %s, 'Zqx Phish Pg')", (_PA,))
        cur.execute(
            "INSERT INTO catalog.walmart_items "
            "(store, sku, missing_since, lifecycle_status, last_seen_at) "
            "VALUES ('R900', %s, NULL, NULL, now())", (_PA,))


@needs_pg
def test_pg_record_line_events_is_idempotent(pg):
    """真库上跑三遍:行数不涨,返回的"真落库行"第二遍起为空。"""
    rows = [_row(line=_L1, store=s, event=se.PHISHING_BRAND_EXPOSURE)
            for s in ("A085", "B012")]
    assert len(se.record_line_events(pg, rows)) == 2
    for _ in range(2):
        assert se.record_line_events(pg, rows) == []
    with pg.cursor() as cur:
        cur.execute("SELECT count(*) FROM ops.store_events "
                    "WHERE detail->>'order_line_id' = %s", (_L1,))
        assert cur.fetchone()[0] == 2


@needs_pg
def test_pg_selfheal_picks_up_lines_the_ledger_never_got(pg, monkeypatch):
    """账本里没有的钓鱼行被捞回来;补记之后再扫一遍就空了(幂等)。"""
    _seed_lines(pg)
    cands = oa._phish_selfheal_cands(pg, 3, [])
    got = {c["order_line_id"]: c for c in cands}
    assert {_L1, _L2} <= set(got)
    assert got[_L1]["store"] == "A085" and got[_L1]["zip"] == "60606"

    monkeypatch.setattr(oa.stores_svc, "registered_names", lambda: {"R900"})
    landed, n_expo = oa._phish_record(pg, [got[_L1], got[_L2]],
                                      "order_audit:selfheal")
    assert len(landed) == 2
    assert n_expo == 2          # 两条订单行各自扇到同一家 R900,键含订单行
    with pg.cursor() as cur:
        cur.execute(
            "SELECT store, severity, detail FROM ops.store_events "
            "WHERE event = %s AND detail->>'order_line_id' = %s",
            (se.PHISHING_ORDER, _L1))
        store, sev, detail = cur.fetchone()
    assert store == "A085" and sev == "high"
    assert detail["zip"] == "60606" and detail["brand"] == "Zqx Phish Pg"
    assert detail["po_id"] == "99900011122233"

    # 补记之后:同一条查询再也捞不到它们(NOT EXISTS 已被满足)
    left = {c["order_line_id"] for c in oa._phish_selfheal_cands(pg, 3, [])}
    assert not ({_L1, _L2} & left)


@needs_pg
def test_pg_selfheal_reads_note_not_status(pg):
    """判据是 audit_detail.note(status 是三值封闭集,里面没有"钓鱼"二字)。

    历史导入的行连 rules.phishing 结构都没有,只认 note 才捞得回来 ——
    与 `_marked_phishing` 同一读法。
    """
    with pg.cursor() as cur:
        cur.execute(
            "INSERT INTO orders.order_lines (order_line_id, store, po_id,"
            " line_number, sku, postal_code, order_date, audit_status,"
            " audit_detail) VALUES (%s, 'A085', '99900011122255', '1', %s,"
            " '60606', now(), '建议拒绝', %s::jsonb)",
            ("zqx-phish-legacy", _PA, '{"note": "钓鱼邮编:60606"}'))
    got = {c["order_line_id"]: c
           for c in oa._phish_selfheal_cands(pg, 3, [])}
    assert "zqx-phish-legacy" in got
    assert got["zqx-phish-legacy"]["zip"] == "60606"   # 回落收件邮编现算


@needs_pg
def test_pg_selfheal_respects_the_store_filter(pg):
    _seed_lines(pg)
    got = {c["order_line_id"] for c in oa._phish_selfheal_cands(pg, 3, ["B012"])}
    assert _L2 in got and _L1 not in got


@needs_pg
def test_pg_selfheal_single_line_mode_only_looks_at_that_line(pg):
    """`-p line=` 的语义是"忽略窗口":补记的扫描面得跟着收窄到那一行。

    单行模式尤其需要补记 —— 已标钓鱼的行被 `_marked_phishing` 滤出待判集合,
    判定那一路根本产不出候选。
    """
    _seed_lines(pg)
    got = {c["order_line_id"]
           for c in oa._phish_selfheal_cands(pg, 3, [], _L2)}
    assert got == {_L2}


@needs_pg
def test_pg_phishing_mark_is_the_first_thing_in_the_summary(pg, monkeypatch):
    """首行是链通知折叠的唯一出口 —— 钓鱼标记必须并进 parts[0]。"""
    parts = ["最近 3 天待审 10 行,落库 10", "结论:建议拒绝 1"]
    mark = oa._phish_mark([_row(line=_L1, extra={"po_id": "1", "zip": "6"})])
    parts[0] = f"{mark};" + parts[0]
    assert ";".join(parts).split(";")[0] == "🚨 钓鱼命中 1"
    assert rules.PHISHING_MARK in ";".join(parts).splitlines()[0]
