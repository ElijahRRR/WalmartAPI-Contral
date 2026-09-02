"""下单时间「观测→定稿」的真库回归(所有者定稿 2026-09-02:下单时间不应该被修改)。

单测桩只能验 SQL 文本;状态机(首见候选 / 连续两轮一致才定稿 / 定稿锁死 /
未定稿连续两轮同一异值改判 / 拒写不抹旧值 / 观测记账不碰 updated_at)的真值
只有真 PostgreSQL 能证。这里 initdb 一个一次性集群到临时目录,跑真
refdata/schema.sql,再按轮次调用 services.order_lines 的真 SQL。
没有 initdb 的机器自动跳过;root 环境经 setpriv 降权为 nobody(PG 拒绝 root)。
"""

import glob
import os
import shutil
import socket
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from registry import db
from services import order_lines as ol

STORE = "T1"
SKU = "B0TEST0001"
X_MS = 1788034103634            # 2026-08-29T20:08:23.634Z(事故里的真值)
Y_MS = 1788293543157            # 2026-09-01T20:12:23.157Z(事故里写错的值)
STATUS_MS = 1788141735000       # 行状态时间,晚于 X 早于 Y


def _pg_bindir():
    p = shutil.which("initdb")
    if p:
        return os.path.dirname(p)
    cands = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"), reverse=True)
    return os.path.dirname(cands[0]) if cands else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def pg_dsn():
    bindir = _pg_bindir()
    if not bindir:
        pytest.skip("本机没有 initdb,跳过真库回归")
    prefix: list[str] = []
    if os.geteuid() == 0:
        if not shutil.which("setpriv"):
            pytest.skip("root 且没有 setpriv,无法降权起 PG")
        prefix = ["setpriv", "--reuid=nobody", "--regid=nogroup", "--clear-groups"]
    d = tempfile.mkdtemp(prefix="wm-pgtest-")
    if prefix:
        import grp
        import pwd
        os.chown(d, pwd.getpwnam("nobody").pw_uid, grp.getgrnam("nogroup").gr_gid)
    os.chmod(d, 0o700)
    env = dict(os.environ, HOME=d, LC_ALL="C")
    port = _free_port()

    def run(*args):
        return subprocess.run([*prefix, *args], env=env, check=True,
                              capture_output=True, text=True)
    try:
        run(f"{bindir}/initdb", "-D", f"{d}/data", "-A", "trust", "-U", "wtest",
            "--no-locale", "-E", "UTF8")
        run(f"{bindir}/pg_ctl", "-D", f"{d}/data", "-w", "-l", f"{d}/pg.log",
            "-o", f"-k {d} -h '' -p {port} -c fsync=off", "start")
    except subprocess.CalledProcessError as e:      # 环境问题不算代码失败
        shutil.rmtree(d, ignore_errors=True)
        pytest.skip(f"一次性 PG 起不来:{e.stderr[-300:]}")
    try:
        yield f"host={d} port={port} user=wtest dbname=postgres"
    finally:
        subprocess.run([*prefix, f"{bindir}/pg_ctl", "-D", f"{d}/data", "-m", "immediate",
                        "stop"], env=env, capture_output=True)
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def pg(pg_dsn, monkeypatch):
    """真库:DSN 只走 registry.db(铁律),schema.sql 每模块跑一次,每测清表。"""
    monkeypatch.setattr(db, "pg_dsn", lambda: pg_dsn)
    monkeypatch.delenv("READONLY_DB_PASSWORD", raising=False)
    if not getattr(pg, "_schema_done", False):
        from workflows import db_init
        db_init.run({})
        pg._schema_done = True
    with db.pg_conn() as conn:
        conn.execute("TRUNCATE orders.order_lines")
    return db


def _order(po: str, order_ms, status_ms=STATUS_MS) -> dict:
    return {"purchaseOrderId": po, "customerOrderId": f"C{po}", "orderDate": order_ms,
            "shippingInfo": {"estimatedDeliveryDate": 1789153200000},
            "orderLines": {"orderLine": [{
                "lineNumber": "1", "item": {"sku": SKU, "productName": "x"},
                "orderLineQuantity": {"amount": "1"}, "statusDate": status_ms,
                "orderLineStatuses": {"orderLineStatus": [{"status": "Shipped"}]}}]}}


def _sync(pg, po: str, order_ms, *, repair=False) -> list[dict]:
    """模拟 order_sync._persist 的一轮:先比对(取证)再 upsert;返回冲突分类。"""
    rows = ol.extract_order_lines(STORE, _order(po, order_ms))
    with pg.pg_conn() as conn:
        found = ol.order_date_conflicts(conn, rows)
        ol.upsert_order_lines(conn, rows, repair_order_date=repair)
    return found


def _state(pg, po: str) -> tuple:
    with pg.pg_conn() as conn:
        return conn.execute(
            "SELECT order_date, order_date_seen, order_date_confirmed, updated_at,"
            " order_meta->>'orderDate', source FROM orders.order_lines WHERE po_id = %s",
            (po,)).fetchone()


def _dt(ms) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def test_first_sight_is_candidate_and_second_identical_read_confirms(pg):
    assert _sync(pg, "P1", X_MS) == []
    od, seen, confirmed, upd1, meta_raw, source = _state(pg, "P1")
    assert (od, seen, confirmed) == (_dt(X_MS), _dt(X_MS), False)   # 候选 + 本轮观测,未定稿
    assert meta_raw == str(X_MS) and source is None       # 首见信封落 order_meta 取证
    assert _sync(pg, "P1", X_MS) == []
    od, seen, confirmed, upd2, *_ = _state(pg, "P1")
    assert (od, seen, confirmed) == (_dt(X_MS), _dt(X_MS), True)
    assert upd2 == upd1        # 观测记账不碰 updated_at:业务行没变就不许触发飞书重推


def test_confirmed_value_is_locked_and_conflict_is_classified(pg):
    _sync(pg, "P2", X_MS)
    _sync(pg, "P2", X_MS)
    got = _sync(pg, "P2", Y_MS)
    assert [c["kind"] for c in got] == ["冲突"] and got[0]["db"] == _dt(X_MS)
    od, seen, confirmed, *_ = _state(pg, "P2")
    assert (od, confirmed) == (_dt(X_MS), True)
    assert seen == _dt(Y_MS)   # 异值照样记账(证据),但不动定稿值
    assert _sync(pg, "P2", Y_MS)[0]["kind"] == "冲突"     # 再来一次还是锁死


def test_transient_glitch_before_confirmation_is_ignored(pg):
    _sync(pg, "P3", X_MS)
    assert [c["kind"] for c in _sync(pg, "P3", Y_MS)] == ["待定"]
    od, seen, confirmed, *_ = _state(pg, "P3")
    assert (od, seen, confirmed) == (_dt(X_MS), _dt(Y_MS), False)
    assert _sync(pg, "P3", X_MS) == []
    od, seen, confirmed, *_ = _state(pg, "P3")
    assert (od, seen, confirmed) == (_dt(X_MS), _dt(X_MS), False)   # 上轮观测是 Y,还不能定
    _sync(pg, "P3", X_MS)
    assert _state(pg, "P3")[2] is True


def test_wrong_first_sight_self_heals_after_two_consistent_reads(pg):
    """对账明细那类"首次写入就错"的行:不用修复模式,两轮正确值后自动改判并定稿。"""
    _sync(pg, "P4", Y_MS)
    assert [c["kind"] for c in _sync(pg, "P4", X_MS)] == ["待定"]
    assert _state(pg, "P4")[0] == _dt(Y_MS)
    assert [c["kind"] for c in _sync(pg, "P4", X_MS)] == ["改判"]
    od, seen, confirmed, *_ = _state(pg, "P4")
    assert (od, seen, confirmed) == (_dt(X_MS), _dt(X_MS), True)


def test_pre_existing_unconfirmed_rows_heal_the_same_way(pg):
    """上线前已写坏的库行(三列取默认值)= 未定稿:两轮正确值后自愈。"""
    with pg.pg_conn() as conn:
        conn.execute("INSERT INTO orders.order_lines (order_line_id, store, po_id, line_number,"
                     " sku, order_date) VALUES (%s, %s, %s, '1', %s, %s)",
                     (ol.make_order_line_id("P5", SKU), STORE, "P5", SKU, _dt(Y_MS)))
    _sync(pg, "P5", X_MS)
    _sync(pg, "P5", X_MS)
    od, seen, confirmed, *_ = _state(pg, "P5")
    assert (od, confirmed) == (_dt(X_MS), True)


def test_history_import_midnight_row_yields_to_api_after_two_reads(pg):
    midnight = datetime(2026, 8, 29, tzinfo=timezone.utc)
    with pg.pg_conn() as conn:
        conn.execute("INSERT INTO orders.order_lines (order_line_id, store, po_id, line_number,"
                     " sku, order_date, source) VALUES (%s, %s, %s, '', %s, %s, %s)",
                     (ol.make_order_line_id("P6", SKU), STORE, "P6", SKU, midnight,
                      ol.HISTORY_SOURCE))
    _sync(pg, "P6", X_MS)
    od, _s, _c, _u, _m, source = _state(pg, "P6")
    assert od == midnight and source is None      # 真行到了:历史标记摘掉,下单时间先保留
    _sync(pg, "P6", X_MS)
    assert _state(pg, "P6")[0] == _dt(X_MS)


def test_future_order_date_is_rejected_and_never_wipes_a_value(pg):
    future = int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp() * 1000)
    _sync(pg, "P7", future)
    od, seen, confirmed, *_ = _state(pg, "P7")
    assert (od, seen, confirmed) == (None, None, False)
    _sync(pg, "P7", X_MS)                 # 首次可信值:填上,仍未定稿
    assert _state(pg, "P7")[:3] == (_dt(X_MS), _dt(X_MS), False)
    _sync(pg, "P7", future)               # 又来未来日期:不抹已有值,不动上轮观测
    assert _state(pg, "P7")[:3] == (_dt(X_MS), _dt(X_MS), False)
    _sync(pg, "P7", X_MS)
    assert _state(pg, "P7")[:3] == (_dt(X_MS), _dt(X_MS), True)


def test_persistent_new_value_switches_after_two_reads_then_locks(pg):
    """沃尔玛若连续两轮都给另一个值,信它并定稿——两轮一致是唯一判据,不偏袒先来者。"""
    _sync(pg, "P8", X_MS)
    _sync(pg, "P8", Y_MS)
    assert [c["kind"] for c in _sync(pg, "P8", Y_MS)] == ["改判"]
    assert _state(pg, "P8")[:3] == (_dt(Y_MS), _dt(Y_MS), True)
    _sync(pg, "P8", X_MS)
    assert _state(pg, "P8")[0] == _dt(Y_MS)   # 定稿后 X 也改不回来(要走修复模式)


def test_repair_mode_overrides_a_confirmed_value(pg):
    _sync(pg, "P9", Y_MS)
    _sync(pg, "P9", Y_MS)
    assert _state(pg, "P9")[:3] == (_dt(Y_MS), _dt(Y_MS), True)
    assert [c["kind"] for c in _sync(pg, "P9", X_MS, repair=True)] == ["冲突"]
    od, _seen, confirmed, *_ = _state(pg, "P9")
    assert (od, confirmed) == (_dt(X_MS), True)


def test_order_meta_is_written_once_and_business_updates_still_flow(pg):
    _sync(pg, "P10", X_MS)
    upd1, meta1 = _state(pg, "P10")[3], _state(pg, "P10")[4]
    rows = ol.extract_order_lines(STORE, _order("P10", X_MS))
    rows[0]["sale_status"] = "Delivered"          # 业务字段真变了
    with pg.pg_conn() as conn:
        ol.upsert_order_lines(conn, rows)
    with pg.pg_conn() as conn:
        status, upd2, meta2 = conn.execute(
            "SELECT sale_status, updated_at, order_meta->>'orderDate' FROM orders.order_lines"
            " WHERE po_id = 'P10'").fetchone()
    assert status == "Delivered" and upd2 > upd1
    assert meta2 == meta1 == str(X_MS)            # 信封只在插入时写
