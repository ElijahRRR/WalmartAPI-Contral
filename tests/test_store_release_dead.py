"""批量清死店(`store_release -p dead=1`)。

⚠ 这条命令会把一批店的在架行**整店标缺席**。missing_since 一打上,在线表
投影 / list_new 去重闸 / maintenance 三处同时按「已下架」办事,而要恢复只能
靠 catalog_sync 重新扫到它 —— 死店根本不会被扫。所以每条测试盯的都是
"什么情况下它**不该**动手"。
"""

import contextlib

import pytest

from services import claims
from workflows import store_release as wf


class _Cur:
    def __init__(self, online):
        self._online, self.rowcount, self._rows = online, 0, []
        self.marked = []
        self.events: list = []          # ops.store_events 的落行(治理类)

    def executemany(self, sql, seq):
        if "INSERT INTO ops.store_events" in sql:
            self.events.extend(seq)

    def execute(self, sql, args=None):
        if "GROUP BY store" in sql:
            self._rows = list(self._online.items())
        elif "SET missing_since" in sql:
            store = args[0]
            self.marked.append(store)
            self.rowcount = self._online.get(store, 0)
        else:
            self._rows = [(self._online.get(args[0], 0),)]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, online):
        self._cur = _Cur(online)

    def cursor(self):
        return self._cur


def _wire(monkeypatch, online, registered, held=None, freed=None):
    conn = _Conn(online)
    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda *a, **k: contextlib.nullcontext(conn))
    import services.stores as stores_svc
    monkeypatch.setattr(stores_svc, "enabled_names", lambda: set(registered))
    monkeypatch.setattr(wf.claims, "preview_release",
                        lambda c, **kw: (held or {}).get(kw.get("store"), []))
    monkeypatch.setattr(wf.claims, "release",
                        lambda c, **kw: (freed if freed is not None else []) or
                        (held or {}).get(kw.get("store"), []))
    return conn


# ── 三道拒跑闸 ────────────────────────────────────────────────────────

def test_refuses_when_the_credential_sheet_cannot_be_read():
    """⚠ 拿不到真值时**不许降级**,更不许当成"全死了"。"""
    import services.stores as stores_svc

    def boom():
        raise RuntimeError("飞书超时")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(stores_svc, "enabled_names", boom)
        out = wf.run({"dead": "1", "execute": False})
    assert out.startswith("⛔") and "凭证表读不到" in out


def test_refuses_when_the_credential_sheet_comes_back_empty(monkeypatch):
    _wire(monkeypatch, {"A085": 10}, set())
    out = wf.run({"dead": "1", "execute": False})
    assert out.startswith("⛔") and "一家店都没读到" in out


def test_refuses_on_a_near_name_collision(monkeypatch):
    """库里「A085 朱丽霖」(多一个空格)与在册「A085朱丽霖」——
    这是**名字漂了**,不是店没了。自动对齐等于替所有者决定两个字符串是同一家店。
    """
    _wire(monkeypatch, {"A085 朱丽霖": 900, "B999死店": 5}, {"A085朱丽霖"})
    out = wf.run({"dead": "1", "execute": False})
    assert out.startswith("⛔") and "名字漂了" in out
    assert "A085 朱丽霖" in out and "A085朱丽霖" in out


def test_case_only_difference_also_trips_the_collision_guard(monkeypatch):
    _wire(monkeypatch, {"h006詹松涛": 12}, {"H006詹松涛"})
    out = wf.run({"dead": "1", "execute": False})
    assert out.startswith("⛔")


def test_refuses_when_nothing_would_be_kept(monkeypatch):
    """一家都不保留 —— 店名格式整体对不上的可能性,远大于所有店同时终止。"""
    _wire(monkeypatch, {"店甲": 100, "店乙": 50}, {"完全不同的店"})
    out = wf.run({"dead": "1", "execute": False})
    assert out.startswith("⛔") and "没有一家" in out


# ── 判据 ──────────────────────────────────────────────────────────────

def test_planning_excluded_stores_are_not_dead(monkeypatch):
    """⚠ **规划外 ≠ 不在营。** 「谭总」那些不参与分配,但货是真在卖的 ——
    扫掉就是把在售商品凭空标成下架。判定只看在不在凭证表。
    """
    _wire(monkeypatch, {"谭总2": 300, "A085朱丽霖": 100, "Z001已终止": 7},
          {"谭总2", "A085朱丽霖"})
    out = wf.run({"dead": "1", "execute": False})
    assert "谭总2" in out.split("保留的")[1]        # 在保留那一侧
    assert "Z001已终止(7)" in out


def test_registered_stores_with_no_online_rows_are_a_no_op(monkeypatch):
    _wire(monkeypatch, {"A085": 10}, {"A085", "A102", "A107"})
    out = wf.run({"dead": "1", "execute": False})
    assert "没有需要清理的店" in out and "在册共 3 家" in out


def test_both_rosters_are_printed_in_full_never_truncated(monkeypatch):
    """这条命令唯一的人工控制点就是这两张名单。截断 = 让人在看不全的情况下
    按确认。"""
    online = {f"D{i:03d}死": 1 for i in range(30)}
    online["A085"] = 5
    _wire(monkeypatch, online, {"A085"})
    out = wf.run({"dead": "1", "execute": False})
    for i in range(30):
        assert f"D{i:03d}死" in out


# ── 真跑 ──────────────────────────────────────────────────────────────

def test_execute_marks_only_the_dead_stores(monkeypatch):
    conn = _wire(monkeypatch, {"A085": 100, "Z001": 7, "Z002": 3},
                 {"A085"}, held={"Z001": [(claims.BRAND, "acme", "Z001")]})
    out = wf.run({"dead": "1", "execute": True})
    assert conn._cur.marked == ["Z001", "Z002"]
    assert "已清理 2 家" in out and "标缺席 10 行" in out


def test_mark_offline_zero_releases_claims_but_leaves_the_snapshot(monkeypatch):
    conn = _wire(monkeypatch, {"A085": 100, "Z001": 7}, {"A085"},
                 held={"Z001": [(claims.BRAND, "acme", "Z001")]})
    out = wf.run({"dead": "1", "execute": True, "mark_offline": "0"})
    assert conn._cur.marked == []
    assert "标缺席 0 行" in out


def test_dry_run_touches_nothing(monkeypatch):
    conn = _wire(monkeypatch, {"A085": 100, "Z001": 7}, {"A085"})
    out = wf.run({"dead": "1", "execute": False})
    assert conn._cur.marked == [] and out.startswith("🧪")
    assert "确认后去掉 --dry-run 重跑" in out


def test_dead_takes_precedence_over_the_four_way_choice(monkeypatch):
    """`-p dead=1` 不需要也不该再给 store/brand/asin —— 别让它掉进
    "四选一"那条校验里报错。"""
    _wire(monkeypatch, {"A085": 1}, {"A085"})
    assert "没有需要清理的店" in wf.run({"dead": "1", "execute": False})
