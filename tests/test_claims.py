"""占用台账回归:排他/幂等/释放边界 + 回填判定(留销量大的店)。"""

import contextlib

import pytest

from services import alloc_survey as sv
from services import claims
from workflows import alloc_backfill as bf
from workflows import store_release as rel


class _Claims:
    """内存版 claims 表:实现部分唯一索引语义(同 kind+key 只许一条 active)。"""

    def __init__(self, seed=(), online=7):
        self.rows = [dict(kind=k, claim_key=v, store=s, status="active")
                     for k, v, s in seed]
        self.online = online
        self._last: list = []
        self.sql: list = []

    # --- cursor 协议 ---
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.sql.append(sql)
        if "INSERT INTO catalog.claims" in sql:
            a = args
            hit = self._active(a["kind"], a["claim_key"])
            if hit:
                self._last = []                     # ON CONFLICT DO NOTHING
            else:
                self.rows.append({**a, "status": "active"})
                self._last = [(len(self.rows),)]
        elif "SELECT claim_key, store" in sql and "= ANY(" in sql:
            kind, keys = args
            self._last = [(r["claim_key"], r["store"]) for r in self.rows
                          if r["status"] == "active" and r["kind"] == kind
                          and r["claim_key"] in keys]
        elif "SELECT claim_key, store" in sql:
            (kind,) = args
            self._last = [(r["claim_key"], r["store"]) for r in self.rows
                          if r["status"] == "active" and r["kind"] == kind]
        elif "UPDATE catalog.claims" in sql:
            self._last = []
            for r in self._match(args):
                r["status"] = "released"
                r["released_reason"] = args["reason"]
                self._last.append((r["kind"], r["claim_key"], r["store"]))
        elif "SELECT kind, claim_key, store FROM catalog.claims" in sql:
            self._last = [(r["kind"], r["claim_key"], r["store"])
                          for r in self._match(args)]
        elif "count(*) FROM catalog.walmart_items" in sql:
            self._last = [(self.online,)]
        else:
            self._last = []

    def _active(self, kind, key):
        return [r for r in self.rows if r["status"] == "active"
                and r["kind"] == kind and r["claim_key"] == key]

    def _match(self, a):
        return [r for r in self.rows if r["status"] == "active"
                and (a.get("store") is None or r["store"] == a["store"])
                and (a.get("kind") is None or r["kind"] == a["kind"])
                and (a.get("key") is None or r["claim_key"] == a["key"])]

    def fetchone(self):
        return self._last[0] if self._last else None

    def fetchall(self):
        return list(self._last)

    rowcount = 0


# ── services/claims ────────────────────────────────────────────────────

def test_claim_is_exclusive_and_reports_owner():
    c = _Claims()
    assert claims.try_claim(c, claims.BRAND, "acme", "A085", "t") is None
    # 第二家店来占同一品牌:不抛错,告诉调用方现任是谁(调用方顺延次优店)
    assert claims.try_claim(c, claims.BRAND, "acme", "A107", "t") == "A085"


def test_claim_is_idempotent_for_same_store():
    c = _Claims()
    claims.try_claim(c, claims.PRODUCT, "B0X", "A085", "t")
    assert claims.try_claim(c, claims.PRODUCT, "B0X", "A085", "t") == "A085"
    ok, conflicts = claims.claim_many(
        c, [{"kind": claims.PRODUCT, "claim_key": "B0X", "store": "A085"}])
    assert (ok, conflicts) == (1, [])          # 同店重复 = 成功,不是冲突


def test_claim_many_separates_ok_and_conflicts():
    c = _Claims(seed=[("brand", "acme", "A085")])
    ok, conflicts = claims.claim_many(c, [
        {"kind": "brand", "claim_key": "acme", "store": "A107"},
        {"kind": "brand", "claim_key": "beta", "store": "A107"},
    ])
    assert ok == 1
    assert conflicts == [("brand", "acme", "A107", "A085")]


def test_kind_and_empty_key_are_rejected():
    c = _Claims()
    with pytest.raises(ValueError):
        claims.try_claim(c, "shop", "x", "A085", "t")
    with pytest.raises(ValueError):
        claims.try_claim(c, claims.BRAND, "", "A085", "t")


def test_release_requires_a_scope():
    """三个条件全空会清空整个台账——那是事故不是功能。"""
    c = _Claims()
    with pytest.raises(ValueError):
        claims.release(c, reason="x")
    with pytest.raises(ValueError):
        claims.release(c, reason="x", kind=claims.BRAND)


def test_release_by_store_frees_all_kinds():
    c = _Claims(seed=[("brand", "acme", "A085"), ("product", "B0X", "A085"),
                      ("brand", "beta", "A107")])
    freed = claims.release(c, reason="terminated", store="A085")
    assert sorted(f[1] for f in freed) == ["B0X", "acme"]
    assert claims.load_active(c, "brand") == {"beta": "A107"}
    # released 行保留(回答"当初属于谁"只能靠它)
    assert any(r["status"] == "released" for r in c.rows)


# ── workflows/store_release ────────────────────────────────────────────

def _wire_rel(monkeypatch, conn):
    monkeypatch.setattr(rel.db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([conn])))


def test_store_release_requires_exactly_one_target(monkeypatch):
    assert rel.run({}).startswith("⛔ 三选一")
    assert rel.run({"store": "A", "brand": "b"}).startswith("⛔ 三选一")


def test_store_release_dry_run_lists_without_changing(monkeypatch):
    c = _Claims(seed=[("brand", "acme", "A085"), ("product", "B0X", "A085")])
    _wire_rel(monkeypatch, c)
    out = rel.run({"store": "A085"})
    assert "🧪 将释放" in out and "active 占用 2 条" in out
    assert "确认后加 --execute" in out
    assert all(r["status"] == "active" for r in c.rows)      # 没动


def test_store_release_execute_frees_and_marks_offline(monkeypatch):
    c = _Claims(seed=[("brand", "acme", "A085")])
    _wire_rel(monkeypatch, c)
    out = rel.run({"store": "A085", "execute": True})
    assert "✅ 已释放" in out
    assert c.rows[0]["status"] == "released"
    assert any("UPDATE catalog.walmart_items" in s for s in c.sql)


def test_store_release_can_skip_offline_marking(monkeypatch):
    c = _Claims(seed=[("brand", "acme", "A085")])
    _wire_rel(monkeypatch, c)
    rel.run({"store": "A085", "execute": True, "mark_offline": "0"})
    assert not any("UPDATE catalog.walmart_items" in s for s in c.sql)


def test_store_release_normalizes_brand_key(monkeypatch):
    """品牌键必须与占用时同一套归一算法,否则大小写差一点就释放不到。"""
    c = _Claims(seed=[("brand", "karl home", "A085")])
    _wire_rel(monkeypatch, c)
    out = rel.run({"brand": "  Karl   Home ", "execute": True})
    assert "✅ 已释放" in out and c.rows[0]["status"] == "released"


def test_store_release_refuses_placeholder_brand(monkeypatch):
    c = _Claims()
    _wire_rel(monkeypatch, c)
    assert "占位符" in rel.run({"brand": "Generic"})


def test_store_release_by_asin_does_not_touch_snapshot(monkeypatch):
    c = _Claims(seed=[("product", "B0X", "A085")])
    _wire_rel(monkeypatch, c)
    rel.run({"asin": "b0x", "execute": True})
    # ASIN 大小写归一后能命中;点名释放不碰在线快照(店还在正常经营)
    assert c.rows[0]["status"] == "released"
    assert not any("UPDATE catalog.walmart_items" in s for s in c.sql)


# ── workflows/alloc_backfill 的判定 ────────────────────────────────────

ROWS = [
    {"store": "A085", "sku": "S1", "asin": "B0A", "brand_key": "acme",
     "published": True, "pt": "Socks", "pt_source": "walmart_confirmed"},
    {"store": "A107", "sku": "S2", "asin": "B0A", "brand_key": "acme",
     "published": True, "pt": "Socks", "pt_source": None},
    {"store": "A107", "sku": "S3", "asin": "B0B", "brand_key": "beta",
     "published": True, "pt": "Hats", "pt_source": None},
]


def test_backfill_keeps_the_higher_selling_store():
    sales = {("A107", "S2"): (5, 500.0), ("A085", "S1"): (1, 10.0)}
    owner, skipped = bf._pick(ROWS, sales, "asin", include_ties=False)
    assert owner["B0A"] == "A107"          # 销量大的赢,不是先来的赢
    assert owner["B0B"] == "A107"          # 无冲突的键直接归它唯一那家
    assert skipped == 0


def test_backfill_skips_zero_sales_ties_by_default():
    """两边都没卖过时机器判不出谁该留——默认不落,等人看清单。"""
    owner, skipped = bf._pick(ROWS, {}, "asin", include_ties=False)
    assert "B0A" not in owner and skipped == 1
    assert owner["B0B"] == "A107"          # 无冲突的不受影响

    owner2, skipped2 = bf._pick(ROWS, {}, "asin", include_ties=True)
    assert "B0A" in owner2 and skipped2 == 0


def test_resolve_conflicts_marks_ties_and_is_deterministic():
    res = sv.resolve_conflicts(ROWS, {}, "asin")
    assert len(res) == 1
    key, keep, _stat, detail, tie = res[0]
    assert key == "B0A" and tie is True
    assert keep == "A085"                          # 全打平按店名,但已标 tie
    assert [d[5] for d in detail] == ["保留", "下架"]


def test_resolve_conflicts_sums_sales_per_store():
    sales = {("A085", "S1"): (2, 30.0), ("A107", "S2"): (1, 99.0)}
    (key, keep, stat, detail, tie) = sv.resolve_conflicts(ROWS, sales, "asin")[0]
    assert keep == "A107" and tie is False
    assert dict((d[0], d[4]) for d in detail) == {"A085": 30.0, "A107": 99.0}
