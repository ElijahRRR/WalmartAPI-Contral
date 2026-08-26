"""店级重试标准(所有者定稿 2026-08-26)四步的回归。

背景:2026-08-26 13:00 两家店 SOCKS 代理报 "Malformed reply"
(socksio.ProtocolError,直接继承 Exception、httpx 不映射),落进
catalog_sync 的泛化异常桶 → raise → product_chain 八步全停。
标准:①失败店跑完别人再串行补试一遍;②仍失败不炸整轮、首行点名缺席;
③下游按目录水位避让缺席店;④链尾逐店重跑完整链一次,再失败即止。
"""

import httpx
import pytest

from api import _client
from services import store_absence, store_retry


# ── ⑤ 异常分类:SOCKS 报错必须被代理桶接住 ────────────────────────────────────

def test_store_proxy_error_is_caught_by_existing_proxy_branches():
    """StoreProxyError 子类化 httpx.ProxyError 是刻意的:全仓
    `except (StoreDeadError, httpx.ProxyError)` 分支零改动天然接住它。"""
    e = _client.StoreProxyError("client_id=abc123…", ValueError("x"))
    assert isinstance(e, httpx.ProxyError)
    assert isinstance(e, httpx.TransportError)      # 网络桶(剔池+重试)也接得住
    assert isinstance(e, _client.PROXY_ERRORS)


def test_socksio_errors_are_registered_in_the_buckets():
    """socksio 不在 httpx 异常树上,必须单列进网络桶与分类元组 ——
    漏一处就是 08-26 事故的回归(SOCKS 报错落泛化桶炸整轮)。"""
    from socksio.exceptions import ProtocolError, SOCKSError
    assert issubclass(SOCKSError, Exception)
    assert not issubclass(ProtocolError, httpx.HTTPError)   # 事故根因本身
    assert any(issubclass(ProtocolError, t) for t in _client.SOCKS_ERRORS)
    assert isinstance(ProtocolError("Malformed reply"), _client.PROXY_ERRORS)
    assert any(issubclass(ProtocolError, t) for t in _client._NET_ERRORS)


def test_get_token_wraps_transport_errors_as_store_proxy_error(monkeypatch):
    """换 token 是每店第一跳,27 个调用点曾对传输层裸奔 —— 现在 SOCKS/传输
    异常一律收口成 StoreProxyError,并顺手剔除连接池里的坏连接。"""
    from socksio.exceptions import ProtocolError

    class _Boom:
        def post(self, *a, **k):
            raise ProtocolError("Malformed reply")

    invalidated = []
    monkeypatch.setattr(_client, "_get_client", lambda proxy: _Boom())
    monkeypatch.setattr(_client, "_invalidate_client",
                        lambda proxy: invalidated.append(proxy))
    monkeypatch.setattr(_client, "_token_cache", {})
    with pytest.raises(_client.StoreProxyError) as ei:
        _client.get_token("cid_test", "secret", "socks5://x")
    assert "Malformed reply" in str(ei.value)
    assert invalidated == ["socks5://x"]            # 坏连接不留给下一家店
    # 老式分类分支(五处生产现存写法)不改一字就能接住
    try:
        raise ei.value
    except (_client.StoreDeadError, httpx.ProxyError):
        pass


def test_backoff_single_source_with_jitter():
    """退避阶梯唯一出处在 _client(#91 引入于 feeds,上提共用);feeds 只留别名
    —— 双轨禁止,谁再写第二张阶梯表这条测试拦不住,但别名断裂拦得住。"""
    from api import feeds
    assert feeds._backoff is _client.backoff
    assert feeds._BACKOFF_LADDER is _client.BACKOFF_LADDER
    assert _client.BACKOFF_LADDER == (2, 4, 8, 16, 32)
    vals = {_client.backoff(0) for _ in range(40)}
    assert all(1.0 <= v <= 2.0 for v in vals) and len(vals) > 1   # 带抖动


# ── ① 串行补试 ────────────────────────────────────────────────────────────────

def _no_sleep(monkeypatch):
    import services.store_retry as sr
    monkeypatch.setattr(sr.time, "sleep", lambda s: None)


def test_serial_second_pass_retries_once_and_splits(monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def attempt(store):
        calls.append(store["name"])
        if store["name"] == "救得回":
            return {"ok": store["name"]}
        raise TimeoutError("still down")

    recovered, still = store_retry.serial_second_pass(
        [({"name": "救得回"}, OSError("first")),
         ({"name": "救不回"}, OSError("first"))], attempt)
    assert calls == ["救得回", "救不回"]            # 串行、各一次
    assert [(s["name"], r) for s, r in recovered] == [("救得回", {"ok": "救得回"})]
    assert [(s["name"], type(e).__name__) for s, e in still] == \
        [("救不回", "TimeoutError")]


def test_serial_second_pass_never_retries_dead_credentials(monkeypatch):
    """凭证死是确定性的:重试只会再死一次,一次都不试(ppc 二轮同款判据)。"""
    _no_sleep(monkeypatch)
    attempted = []
    dead = _client.StoreDeadError("T1", 400)
    recovered, still = store_retry.serial_second_pass(
        [({"name": "T1"}, dead)], lambda s: attempted.append(s))
    assert attempted == [] and recovered == []
    assert still == [({"name": "T1"}, dead)]


def test_classify_is_an_enumeration_not_catch_all():
    assert store_retry.classify(_client.StoreDeadError("T", 401)) == "凭证"
    assert store_retry.classify(
        _client.StoreProxyError("T", ValueError("x"))) == "代理"
    from socksio.exceptions import ProtocolError
    assert store_retry.classify(ProtocolError("Malformed reply")) == "代理"
    assert store_retry.classify(RuntimeError("GET 返回 None")) == "其他"


# ── ③ 缺席判据(目录水位派生,不建新表、不靠调度顺序)─────────────────────────

class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        assert "max(last_seen_at)" in sql

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)


def test_stale_stores_is_enabled_and_watermark_based(monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(store_absence, "STALE_HOURS", 20)
    import services.stores as stores_svc
    monkeypatch.setattr(stores_svc, "enabled_names",
                        lambda: ["新鲜店", "缺席店", "停用外的另一家"])
    conn = _Conn([("新鲜店", now - timedelta(hours=1)),
                  ("缺席店", now - timedelta(hours=25)),
                  ("停用店", now - timedelta(days=9)),      # 不在营:不进名单
                  ("停用外的另一家", None)])                 # 水位空:查不出,不误报
    assert store_absence.stale_stores(conn) == ["缺席店"]
    # since 显式锚点(cli 链尾重赛用):比 since 旧才算缺席
    assert store_absence.stale_stores(conn, since=now - timedelta(hours=2)) \
        == ["缺席店"]
    assert store_absence.stale_stores(
        conn, since=now - timedelta(days=30)) == []


# ── ④ 链尾重赛(cli._replay_absent)─────────────────────────────────────────────

def _replay_wire(monkeypatch, absent, statuses):
    """statuses: {(store, step): status};缺省 success。返回 (lines, 调用序)。"""
    import contextlib
    import types

    import cli
    from registry import db as _db
    ran = []
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    from services import store_absence as sa
    seq = iter([list(absent), []])       # 重赛前=缺席名单;重赛后=全部救回
    monkeypatch.setattr(sa, "stale_stores",
                        lambda conn, since=None, hours=None: next(seq))

    def fake_run_step(name, module, params, dry_run, operator, logs_dir):
        ran.append((params.get("store"), name))
        st = statuses.get((params.get("store"), name), "success")
        return st, f"{name} {st}"

    monkeypatch.setattr(cli, "_run_step", fake_run_step)
    mods = {
        "catalog_sync": types.SimpleNamespace(SUPPORTS_STORE=True),
        "sources_backfill": types.SimpleNamespace(),        # 全局步:重赛跳过
        "maintenance_scan": types.SimpleNamespace(SUPPORTS_STORE=True),
        "maintenance": types.SimpleNamespace(SUPPORTS_STORE=True),
    }
    steps = list(mods)
    lines = cli._replay_absent(steps, mods,
                               {n: {"execute": True} for n in steps},
                               False, "test", None, since=None)
    return lines, ran


def test_replay_runs_store_scoped_steps_once_per_absent_store(monkeypatch):
    lines, ran = _replay_wire(monkeypatch, ["店A"], {})
    # 全局步骤(sources_backfill)跳过;支持 store 的按链序逐店跑
    assert ran == [("店A", "catalog_sync"), ("店A", "maintenance_scan"),
                   ("店A", "maintenance")]
    assert any("sources_backfill" in ln and "跳过" in ln for ln in lines)
    assert any(ln.startswith("✅ 店A:救回") for ln in lines)


def test_replay_stops_that_store_on_first_failure_and_says_so(monkeypatch):
    """某步失败即终止该店重赛(上游语义与主链一致),**再失败即止**,不循环。"""
    lines, ran = _replay_wire(monkeypatch, ["店A", "店B"],
                              {("店A", "catalog_sync"): "failed"})
    assert ("店A", "maintenance_scan") not in ran      # 店A 卡住即止
    assert ran.count(("店A", "catalog_sync")) == 1     # 只试一次,不循环
    assert [c for c in ran if c[0] == "店B"] == [      # 店B 不受店A牵连
        ("店B", "catalog_sync"), ("店B", "maintenance_scan"),
        ("店B", "maintenance")]
    assert any(ln.startswith("❌ 店A:仍缺席") and "catalog_sync" in ln
               for ln in lines)


def test_replay_is_a_noop_without_absent_stores(monkeypatch):
    lines, ran = _replay_wire(monkeypatch, [], {})
    assert lines == [] and ran == []


# ── ② 落库面:withdraw_stale 的缺席排除口(缺席 ≠ 恢复正常)─────────────────────

def test_withdraw_sql_carries_the_exclude_stores_clause():
    """缺席店的行不在 keep 里(本轮避让了),不排除会被撤成
    「商品自己恢复正常了」—— 两条 SQL 分支都必须带排除口。"""
    import inspect

    from services import dispositions as ds
    assert "NOT (d.store = ANY(%(exclude)s::text[]))" in ds._WITHDRAW_SQL
    src = inspect.getsource(ds.withdraw_stale)
    assert src.count("NOT (d.store = ANY(%(exclude)s::text[]))") >= 1  # 空 keep 分支
    assert "exclude_stores" in src
