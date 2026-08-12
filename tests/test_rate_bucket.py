"""跨进程令牌桶回归(稀缺桶落 ops.rate_events;2026-08-12)。

背景:cli 的 flock 只锁同名 workflow,不同 workflow 是不同进程——进程内
deque 在并发调度下 = N 倍配额 + 进程退出即失忆。旧 RETIRE_ITEM 零限速
事故的另一半教训("默认拒绝"是第一半)。
所有者拍板:PG 不可达时稀缺桶**直接报错停手**,不降级进程内。
"""

from datetime import datetime, timedelta, timezone

import pytest

from api import _client

# conftest 的 autouse 夹具会把模块属性 _acquire_pg 换成 _acquire_mem;
# 本文件专测 PG 路径,import 时先存原件
_REAL_ACQUIRE_PG = _client._acquire_pg


def test_persistent_partition_snapshot():
    """分档判据快照(window≥600s 或 limit≤10):哪些桶跨进程共享一目了然,
    防登记表改动时无意漂移。"""
    persistent = {b for b in _client._RATE_BUCKETS if _client._is_persistent(b)}
    # 全部 feed 提交桶必须共享——DELETE_ITEM 有三个提交来源(product_clear/
    # cleanup/maintenance),正是旧系统"3 个下架来源共享通道"的格局
    posts = {b for b in _client._RATE_BUCKETS if b.startswith("feeds.post.")}
    assert posts and posts <= persistent
    assert "prices.put" in persistent            # 80/hour
    assert "reports.request" in persistent       # 2/hour
    assert "items.walmart_search_spec" in persistent   # 1000/day 日额度
    ins = {b for b in _client._RATE_BUCKETS if b.startswith("insights.")}
    assert ins and ins <= persistent             # 1/min
    # 高频大配额桶留在进程内(分钟级窗口自然重置,429 退避兜得住)
    for b in ("orders.list", "feeds.get", "items.get", "items.walmart_search",
              "inventory.put", "settings.partnerprofile", "returns.list"):
        assert b not in persistent, b


class _Cur:
    def __init__(self, rows):
        self.rows = list(rows)
        self.sqls: list = []

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))

    def fetchone(self):
        return self.rows.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_pg_acquire_counts_inserts_and_cleans(monkeypatch):
    """窗口未满:advisory 锁 + 计数 + 占位 INSERT + 顺手清旧,同一事务。"""
    from registry import db
    cur = _Cur(rows=[(5, None, datetime.now(timezone.utc))])
    monkeypatch.setattr(db, "pg_conn", lambda: _Conn(cur))
    waited = _REAL_ACQUIRE_PG("feeds.post.DELETE_ITEM", "cidA", 6, 3600.0)
    assert waited == 0.0
    joined = " | ".join(sql for sql, _ in cur.sqls)
    assert "pg_advisory_xact_lock" in joined
    assert "INSERT INTO ops.rate_events" in joined
    assert "DELETE FROM ops.rate_events" in joined      # 自清理,不需独立清理器


def test_pg_full_window_sleeps_then_acquires(monkeypatch):
    """窗口已满:睡到最早事件滑出(事务外,不拿锁睡),下一轮拿到。"""
    from registry import db
    now = datetime.now(timezone.utc)
    conns = [
        _Conn(_Cur(rows=[(1, now - timedelta(seconds=0.06), now)])),  # 满
        _Conn(_Cur(rows=[(0, None, now)])),                            # 滑出
    ]
    monkeypatch.setattr(db, "pg_conn", lambda: conns.pop(0))
    waited = _REAL_ACQUIRE_PG("reports.request", "cidA", 1, 0.1)
    assert waited > 0                    # 真等过(0.1s 窗口,睡 ~0.09s)
    assert not conns                     # 两轮都用掉了


def test_pg_down_fails_hard(monkeypatch):
    """PG 不可达:稀缺桶直接抛错停手,绝不静默退回进程内(所有者拍板
    2026-08-12;静默降级 = 旧 RETIRE_ITEM 零限速事故换个马甲)。"""
    from registry import db

    def _boom():
        raise OSError("connection refused")

    monkeypatch.setattr(db, "pg_conn", _boom)
    with pytest.raises(OSError):
        _REAL_ACQUIRE_PG("feeds.post.MP_ITEM", "cidA", 8, 3600.0)


def test_unregistered_bucket_still_rejected():
    with pytest.raises(KeyError):
        _client.rate_acquire("feeds.post.NOT_REGISTERED", "cidA")
