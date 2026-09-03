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
        self.events: list = []      # ops.store_events 的 executemany 落行

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

    def executemany(self, sql, seq):
        self.sql.append(sql)
        if "INSERT INTO ops.store_events" in sql:
            self.events.extend(seq)

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
    ok, conflicts, landed = claims.claim_many(
        c, [{"kind": claims.PRODUCT, "claim_key": "B0X", "store": "A085"}])
    assert (ok, conflicts) == (1, [])          # 同店重复 = 成功,不是冲突
    # ★ 但**本轮没有落库**:事件账本按 landed 记,否则天天重跑天天记一条
    assert landed == []


def test_claim_many_separates_ok_and_conflicts():
    c = _Claims(seed=[("brand", "acme", "A085")])
    ok, conflicts, landed = claims.claim_many(c, [
        {"kind": "brand", "claim_key": "acme", "store": "A107"},
        {"kind": "brand", "claim_key": "beta", "store": "A107"},
    ])
    assert ok == 1
    assert conflicts == [("brand", "acme", "A107", "A085")]
    assert [r["claim_key"] for r in landed] == ["beta"]


def test_claim_created_rows_counts_only_real_inserts():
    """★ 幂等重跑零新增:同一批喂两遍,第二遍一条事件都不该产。

    `ok` 把"早就占在自己名下"也算成功(摘要口径,不动);拿它记账的话
    alloc_backfill(天生幂等,每天重跑同一批在线行)会在账本上天天多一条
    "新占 N 千条"的假事件 —— 而账本是只追加的,假事件删不掉。
    """
    c = _Claims()
    rows = [{"kind": claims.BRAND, "claim_key": "acme", "store": "A085"},
            {"kind": claims.PRODUCT, "claim_key": "B0X", "store": "A085"},
            {"kind": claims.PRODUCT, "claim_key": "B0Y", "store": "A107"}]
    ok, _c, landed = claims.claim_many(c, rows)
    evs = claims.claim_created_rows(landed, "alloc_plan")
    assert ok == 3 and len(landed) == 3
    assert [(e["store"], e["severity"]) for e in evs] == [
        ("A085", "info"), ("A107", "info")]
    assert evs[0]["detail"]["brand"] == 1 and evs[0]["detail"]["product"] == 1
    assert evs[0]["detail"]["sample"] == ["brand:acme", "product:B0X"]
    # 第二遍:ok 照样是 3(幂等成功),但真落库行为 0 ⇒ 零事件
    ok2, _c2, landed2 = claims.claim_many(c, rows)
    assert ok2 == 3 and landed2 == []
    assert claims.claim_created_rows(landed2, "alloc_plan") == []


def test_released_rows_severity_by_scope():
    """整店释放 high、点名/csv mid;标缺席行数进 detail(同一动作的另一半后果)。"""
    freed = [("brand", "acme", "A085"), ("product", "B0X", "A085")]
    whole = claims.released_rows(freed, source="store_release", scope="store",
                                 reason="清死店", marked={"A085": 120})
    assert len(whole) == 1 and whole[0]["severity"] == "high"
    assert whole[0]["detail"]["marked_offline"] == 120
    assert whole[0]["detail"]["scope"] == "store"
    named = claims.released_rows(freed, source="store_release", scope="named",
                                 reason="归属调整")
    assert named[0]["severity"] == "mid"
    assert "marked_offline" not in named[0]["detail"]
    csv_rows = claims.released_rows(freed, source="store_release", scope="csv",
                                    reason="批量")
    assert csv_rows[0]["severity"] == "mid"
    assert claims.released_rows([], source="x", scope="store", reason="r") == []


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


def test_store_release_refuses_an_empty_target(monkeypatch):
    """全空会清空整个台账。"""
    assert rel.run({}).startswith("⛔ 至少给一个")


def test_store_release_refuses_brand_and_asin_together(monkeypatch):
    assert rel.run({"brand": "b", "asin": "B0X"}).startswith("⛔ ")


def test_store_qualifies_a_named_release_instead_of_conflicting(monkeypatch):
    """★ `-p brand=X -p store=A` = 只有这个品牌**此刻确实占在 A** 才放。

    为什么要有:按 (类型, 键) 无条件释放的话,占用如果在你出清单之后换了店,
    这条命令会把**新店的好占用**一起放掉。`_run_csv` 早就按三条件释放,
    claim_audit 拼给人手跑的单条命令却没带 store —— 两条路径口径不一致。
    """
    seen = {}

    class _C:
        def cursor(self):
            raise AssertionError("限定店的点名释放不许碰在线快照")

    monkeypatch.setattr(rel.claims, "preview_release",
                        lambda c, **kw: seen.update(kw) or [])
    _wire_rel(monkeypatch, _C())
    out = rel.run({"brand": "acme", "store": "A085", "execute": False})
    assert seen == {"store": "A085", "kind": rel.claims.BRAND, "key": "acme"}
    assert "品牌 acme(限 A085)" in out


def test_a_qualified_release_never_marks_the_whole_store_offline(monkeypatch):
    """⚠ 不加这道判断的话 `-p brand=X -p store=A085` 会把 A085 **整店**标缺席
    —— 那是个别归属调整,店还在正常经营。"""
    marked = []

    class _Cur:
        rowcount = 0

        def execute(self, sql, args=None):
            marked.append(sql)

        def fetchone(self):
            return (0,)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _C:
        def cursor(self):
            return _Cur()

    monkeypatch.setattr(rel.claims, "preview_release", lambda c, **kw: [])
    monkeypatch.setattr(rel.claims, "release", lambda c, **kw: [])
    _wire_rel(monkeypatch, _C())
    rel.run({"brand": "acme", "store": "A085", "execute": True})
    assert not [q for q in marked if "missing_since" in q]


def test_store_release_csv_needs_the_claim_audit_header(monkeypatch, tmp_path):
    """认死表头,不按列号取。

    按列号取的话,以后往 csv 中间插一列,这条命令会拿着「原因」当占用键去释放,
    **而且不会报错** —— 一次误释放要人肉查回来。
    """
    p = tmp_path / "x.csv"
    p.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    assert "表头对不上" in rel.run({"from_csv": str(p)})
    assert "文件不存在" in rel.run({"from_csv": str(tmp_path / "nope.csv")})


def test_store_release_csv_scopes_every_release_to_its_store(monkeypatch, tmp_path):
    """⚠ 每行都带 store 条件:占用在出 csv 之后换了店的话,不能连新店一起放掉。

    只按 (kind, key) 放的话,一条已经被正确重新分配的占用会被这份过期 csv
    误伤,而新店那边完全无辜。
    """
    c = _Claims(seed=[("brand", "acme", "A085"), ("brand", "zeta", "新店")])
    _wire_rel(monkeypatch, c)
    p = tmp_path / "r.csv"
    p.write_text("类型,占用键,占用店\nbrand,acme,A085\nbrand,zeta,旧店\n",
                 encoding="utf-8-sig")
    out = rel.run({"from_csv": str(p), "execute": True})
    assert "实际释放 1 条" in out and "1 行没命中" in out
    left = {(r["claim_key"], r["store"]) for r in c.rows if r["status"] == "active"}
    assert left == {("zeta", "新店")}          # 换了店的那条毫发无损


def test_store_release_dry_run_lists_without_changing(monkeypatch):
    c = _Claims(seed=[("brand", "acme", "A085"), ("product", "B0X", "A085")])
    _wire_rel(monkeypatch, c)
    out = rel.run({"store": "A085"})
    assert "🧪 将释放" in out and "active 占用 2 条" in out
    # 2026-08-17:提示语跟着"缺省即真跑"改口径——dry-run 之后的下一步是
    # **去掉 --dry-run** 重跑,不是"加 --execute"(那是旧口径的空操作别名)
    assert "确认后去掉 --dry-run 重跑" in out
    assert all(r["status"] == "active" for r in c.rows)      # 没动


def test_store_release_execute_frees_and_marks_offline(monkeypatch):
    c = _Claims(seed=[("brand", "acme", "A085")])
    _wire_rel(monkeypatch, c)
    out = rel.run({"store": "A085", "execute": True})
    assert "✅ 已释放" in out
    assert c.rows[0]["status"] == "released"
    assert any("UPDATE catalog.walmart_items" in s for s in c.sql)


def test_store_release_records_a_high_governance_event_for_a_whole_store(monkeypatch):
    """★ 整店释放 = high,且**与释放同一个事务**。

    整店释放通常紧跟"这家店没了";下一轮分配就会把这些品牌发给别的店,
    那一步不可逆。台账落了而事件没落的话,事后按事件流回查这家店的一生
    会缺掉最重的那一笔。标缺席行数一起带进 detail(同一动作的另一半后果)。
    """
    import json
    c = _Claims(seed=[("brand", "acme", "A085"), ("product", "B0X", "A085")])
    _wire_rel(monkeypatch, c)
    rel.run({"store": "A085", "execute": True})
    assert len(c.events) == 1
    store, event, severity, source, detail = c.events[0]
    assert (store, event, severity) == ("A085", "claim_released", "high")
    assert source == "store_release"
    d = json.loads(detail)
    assert d["brand"] == 1 and d["product"] == 1 and d["scope"] == "store"
    assert "marked_offline" in d               # 在线快照校正的行数(假游标 0 行)
    assert "reason" in d and len(d["sample"]) == 2


def test_a_named_release_is_only_mid(monkeypatch):
    """点名释放:货还在架上、店还在正常经营 —— 是个别归属调整,不是 high。"""
    import json
    c = _Claims(seed=[("brand", "acme", "A085")])
    _wire_rel(monkeypatch, c)
    rel.run({"brand": "acme", "execute": True})
    assert len(c.events) == 1 and c.events[0][2] == "mid"
    assert json.loads(c.events[0][4])["scope"] == "named"


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
    owner, skipped = bf._pick(ROWS, sales, "asin", include_ties=False,
                              metrics=sv.store_metrics(ROWS, sales))
    assert owner["B0A"] == "A107"          # 销量大的赢,不是先来的赢
    assert owner["B0B"] == "A107"          # 无冲突的键直接归它唯一那家
    assert skipped == 0


def test_backfill_skips_only_true_ties():
    """只有连店铺整体销量都分不出才跳过;能靠店铺表现判的照落。"""
    owner, skipped = bf._pick(ROWS, {}, "asin", include_ties=False)
    assert "B0A" not in owner and skipped == 1
    assert owner["B0B"] == "A107"          # 无冲突的不受影响

    owner2, skipped2 = bf._pick(ROWS, {}, "asin", include_ties=True)
    assert "B0A" in owner2 and skipped2 == 0

    # 有店铺整体销量可依据时,不算打平,直接落
    metrics = sv.store_metrics(ROWS, {("A107", "S3"): (5, 500.0)})
    owner3, skipped3 = bf._pick(ROWS, {}, "asin", include_ties=False, metrics=metrics)
    assert owner3["B0A"] == "A107" and skipped3 == 0


def test_resolve_conflicts_falls_back_through_the_ladder():
    """两边该商品都零销量时,降级看该店该大类销量 → 该店整体销量。

    实测 96% 的同 ASIN 冲突组两边都零销量,只看商品销量等于把两千多组
    丢给人工;降级到"这家店整体卖得怎么样"是可解释的经营判断。
    """
    # A107 别处卖了 500(整体销量高),两边该商品都没卖过
    metrics = sv.store_metrics(ROWS, {("A107", "S3"): (5, 500.0)})
    key, keep, _stat, detail, level = sv.resolve_conflicts(
        ROWS, {}, "asin", metrics)[0]
    assert key == "B0A" and keep == "A107"
    assert level == "按该店整体销量"
    assert [d.verdict for d in detail] == ["下架", "保留"]


def test_resolve_conflicts_商品销量优先于店铺整体():
    """该商品自己有销量时,不看店铺整体——阶梯是有顺序的。"""
    sales = {("A085", "S1"): (2, 30.0), ("A107", "S3"): (9, 900.0)}
    metrics = sv.store_metrics(ROWS, sales)
    key, keep, _stat, _detail, level = sv.resolve_conflicts(
        ROWS, sales, "asin", metrics)[0]
    assert keep == "A085" and level == "按该商品销量"   # A107 整体高但该品没卖过


def test_resolve_conflicts_真打平才落到店名():
    """连店铺整体销量都相同才算机器判不出——那才是要人看的。"""
    key, keep, _stat, detail, level = sv.resolve_conflicts(
        ROWS, {}, "asin", ({}, {}))[0]
    assert keep == "A085" and level in (sv.LADDER[3], sv.LADDER[4])
    assert [d.verdict for d in detail] == ["保留", "下架"]


def test_resolve_conflicts_sums_sales_per_store():
    sales = {("A085", "S1"): (2, 30.0), ("A107", "S2"): (1, 99.0)}
    metrics = sv.store_metrics(ROWS, sales)
    (key, keep, stat, detail, level) = sv.resolve_conflicts(
        ROWS, sales, "asin", metrics)[0]
    assert keep == "A107" and level == "按该商品销量"
    # ⚠ 按字段名取,别按列号 —— 2026-08-22 给明细插了一列「大类」,
    #   全仓的 `d[7]` 一起错位而且不报错(把「保留/下架」读成一个数字)
    assert {d.store: d.gmv for d in detail} == {"A085": 30.0, "A107": 99.0}


# ── 非 ACTIVE 的店当全新店:回填一条占用都不给它建(定稿 2026-08-15 晚)──

class _BfCur:
    """alloc_backfill 的假游标:按 SQL 特征分派,状态可注入。"""

    def __init__(self, items, status):
        self.items, self.status, self._rows = items, status, []
        self.events: list = []

    def executemany(self, sql, seq):
        if "INSERT INTO ops.store_events" in sql:
            self.events.extend(seq)

    def execute(self, sql, args=None):
        if "product_type, btrim" in sql:
            self._rows = [("Socks", "Fashion")]
        elif "FROM catalog.walmart_items" in sql:
            self._rows = list(self.items)
        elif "store_kpi_daily" in sql:
            self._rows = list(self.status)
        elif "FROM orders.order_lines" in sql:
            self._rows = []
        elif "FROM catalog.products p" in sql:
            self._rows = [("B0AAAA0001", "Acme", None, "Socks", "walmart_confirmed",
                           None),
                          ("B0BBBB0002", "Beta", None, "Socks", "walmart_confirmed",
                           None)]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire_bf(monkeypatch, items, status, captured):
    cur = _BfCur(items, status)

    class _Conn:
        def cursor(self):
            return cur

    monkeypatch.setattr(bf.db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([_Conn()])))
    monkeypatch.setattr(bf.stores_svc, "enabled_names",
                        lambda: {"在营店", "停用店"})
    monkeypatch.setattr(bf.store_targets, "load_targets",
                        lambda: {"在营店": {"categories": [], "max_online": 500.0},
                                 "停用店": {"categories": [], "max_online": 500.0}})
    monkeypatch.setattr(
        bf.claims, "claim_many",
        lambda conn, rows: (captured.extend(rows), (len(rows), [], rows))[1])


def test_backfill_claims_for_a_suspended_store_too(monkeypatch):
    """SUSPENDED 的店**照常回填占用** —— 「暂停不释放、占用保持」(§六.2)。

    2026-08-15 晚一度被实现成"当作全新店、不给它建占用",所有者当即纠正。
    那个实现有多坏值得留一条测试钉住:停用店手上的品牌与产品会变成"没人占",
    别店一回填就抢走 —— 而占用没有自动释放,店恢复之后**拿不回来**,
    它自己还在卖的 listing 反而会撞上别店的占用进下架清单。
    停用只是暂时不给它分新货,那件事的开关是「单店最大在线数」填 0。
    """
    captured: list = []
    _wire_bf(monkeypatch, items=[("在营店", "B0AAAA0001", "Socks", "PUBLISHED"),
                                 ("停用店", "B0BBBB0002", "Socks", "PUBLISHED")],
             status=[("在营店", "ACTIVE"), ("停用店", "SUSPENDED")],
             captured=captured)
    bf.run({"execute": True})
    assert {r["store"] for r in captured} == {"在营店", "停用店"}
    assert "B0BBBB0002" in {r["claim_key"] for r in captured}
