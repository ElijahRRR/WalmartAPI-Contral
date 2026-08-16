"""分配方案回归。**这是全项目唯一会大批量落不可逆占用的工作流**,
所以每条都盯着"会把货分错、或者把占用落错"的具体路径。
"""

import pytest

from services import claims
from workflows import alloc_plan as wf


def _c(asin, brand, score, cat="家居", ch="FBA", pt="pt1"):
    return {"asin": asin, "brand": brand, "manufacturer": None, "pt": pt,
            "category": cat, "channel": ch, "score": score, "base": score,
            "bonus": 0.0, "penalty": 0.0, "why": "", "missing": [],
            "sales": None, "rating": "4.5", "reviews": "10", "lead": 3}


def _grp(key, brand, items, store=None):
    g = {"key": key, "brand": brand, "score": max(i["score"] for i in items),
         "size": len(items), "category": items[0]["category"],
         "channel": items[0]["channel"], "items": items}
    if store:
        g["store"] = store
    return g


# ── 落占用 ────────────────────────────────────────────────────────────

def test_free_flow_claims_both_the_brand_and_every_asin():
    items = [_c("B0AAAA0001", "acme", 90.0), _c("B0AAAA0002", "acme", 80.0)]
    rows = wf._to_claim([{"group": _grp("acme", "acme", items), "store": "A",
                          "layer": 1, "tier": 1}], [])
    kinds = [(r["kind"], r["claim_key"], r["store"]) for r in rows]
    assert (claims.BRAND, "acme", "A") in kinds
    assert (claims.PRODUCT, "B0AAAA0001", "A") in kinds
    assert (claims.PRODUCT, "B0AAAA0002", "A") in kinds
    assert len(rows) == 3


def test_directed_flow_does_not_re_claim_the_brand():
    """⚠ 定向流的品牌占用**已经存在** —— 它就是因为被占才走定向流的。

    重复落不会出错(ON CONFLICT DO NOTHING),但会让"落库 N 条"对不上
    人能数出来的东西,而这个数是所有者唯一的核对手段。
    """
    items = [_c("B0BBBB0001", "zeta", 70.0)]
    rows = wf._to_claim([], [_grp("zeta", "zeta", items, store="A085")])
    assert [(r["kind"], r["claim_key"], r["store"]) for r in rows] == [
        (claims.PRODUCT, "B0BBBB0001", "A085")]


def test_unbranded_group_claims_only_the_asin():
    """无品牌的组不占品牌 —— 拿 "(无品牌):B0..." 这个合成键去占用,
    等于凭空造一个叫这个名字的品牌把别人挡住。"""
    items = [_c("B0CCCC0001", None, 60.0)]
    g = _grp("(无品牌):B0CCCC0001", None, items)
    rows = wf._to_claim([{"group": g, "store": "A", "layer": 1, "tier": 1}], [])
    assert [r["kind"] for r in rows] == [claims.PRODUCT]


def test_claim_snapshot_carries_the_walmart_pt():
    """每条占用记下当时的 PT —— 回答"当初按什么分的"只能靠它(§7.8)。"""
    items = [_c("B0AAAA0001", "acme", 90.0, pt="Socks")]
    rows = wf._to_claim([{"group": _grp("acme", "acme", items), "store": "A",
                          "layer": 1, "tier": 1}], [])
    prod = [r for r in rows if r["kind"] == claims.PRODUCT][0]
    assert prod["walmart_pt"] == "Socks"


# ── 批量与切口 ────────────────────────────────────────────────────────

def test_headroom_leaves_room_for_gate_failures():
    """候选切口必须**大于**批量。

    切到刚好够数就没有腾挪余地:轮到某家店时,它类目/渠道能接的货已被前面
    挑光了。实测 1.0× 少发 7.5% 且顶层比值两家越界。
    """
    assert wf.HEADROOM > 1.0


# ── 配额 ──────────────────────────────────────────────────────────────

def test_quota_weights_match_the_owner_ruling():
    """所有者拍板:把货给离目标最远的店 ⇒ 缺口主导(§7.4g #5)。"""
    import inspect
    src = inspect.getsource(wf.run)
    assert "W_GAP, W_ROOM, W_EFF = 0.6, 0.25, 0.15" in src


def test_pending_delist_is_deduped_across_the_two_gates(monkeypatch):
    """⚠ 一行同时踩类目与渠道两道闸,也只空出**一个**货位。

    不去重会把 room 高估一倍,配额跟着虚高,分下去的货塞不进店里。
    """
    rows = [{"store": "A", "sku": "S1", "asin": "B0AAAA0001", "brand_key": "b",
             "category": "Home", "channel": "FBM", "published": True}]
    cfg = {"A": {"categories": ["Fashion"], "channel": "FBA"}}
    monkeypatch.setattr(wf.sv, "_fetch_meta", lambda *a, **k: {})
    monkeypatch.setattr(wf.sv, "enrich", lambda *a, **k: (rows, {}))

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cur()

    # 两道闸都踩了(Home 不准入 + FBM 不符),但只该记 1
    assert wf.sv.offends_category(rows[0], cfg)
    assert wf.sv.offends_channel(rows[0], cfg)
    assert wf._pending_delist(_Conn(), cfg, {"A"}) == {"A": 1}


# ── 方案表 ────────────────────────────────────────────────────────────

def test_plan_csv_lists_every_product_not_every_group(tmp_path, monkeypatch):
    """方案表逐**产品**一行:一张牌是一个品牌组,但你要照着上架的是产品。

    只写组的话,一个 40 件的组在表上就是一行,没人知道具体上哪 40 个。
    """
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    items = [_c("B0AAAA0001", "acme", 90.0), _c("B0AAAA0002", "acme", 80.0)]
    p = wf._write_plan([{"group": _grp("acme", "acme", items), "store": "A",
                         "layer": 1, "tier": 1}], [], [], [])
    body = open(p, encoding="utf-8-sig").read().splitlines()
    assert len(body) == 3                      # 表头 + 2 个产品
    assert "B0AAAA0001" in body[1] and "B0AAAA0002" in body[2]


def test_plan_csv_also_carries_the_ones_that_did_not_make_it(tmp_path, monkeypatch):
    """未发出与定向流淘汰**必须进同一张表**。

    分开放的话,所有者看到的是"分了 3000 件",而不知道有多少货因为
    没人开这个大类而卡住 —— 那才是他能动手改的东西。
    """
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    g = _grp("zeta", "zeta", [_c("B0BBBB0001", "zeta", 50.0)])
    p = wf._write_plan([], [], [{"group": g, "reason": wf.ae.NO_GATE}],
                       [(dict(g, store="A085"), "占用店容量不足")])
    text = open(p, encoding="utf-8-sig").read()
    assert "未发出" in text and "定向流淘汰" in text and "占用店容量不足" in text


def test_dangerous_and_defaults_to_dry_run():
    assert wf.DANGEROUS is True


# ── 端到端:run() 的接线 ──────────────────────────────────────────────

class _Cur:
    def __init__(self, online): self.online, self._r = online, []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, args=None):
        self._r = [(s, n) for s, n in self.online.items()] if "count(*)" in sql else []
    def fetchall(self): return list(self._r)


class _Conn:
    def __init__(self, online): self.online = online
    def cursor(self): return _Cur(self.online)


def _wire(monkeypatch, pool, held_brand=None, held_prod=None, claimed=None):
    online = {"A": 100, "B": 100}
    monkeypatch.setattr(wf.store_targets, "load_targets", lambda: {
        "A": {"categories": ["家居"], "channel": "FBA", "max_online": 5000,
              "gmv": 400.0, "orders": 5.0},
        "B": {"categories": ["家居"], "channel": "FBA", "max_online": 5000,
              "gmv": 400.0, "orders": 5.0}})
    monkeypatch.setattr(wf.stores_svc, "registered_names", lambda: {"A", "B"})
    monkeypatch.setattr(wf.product_pool, "load",
                        lambda conn, win: {"pool": [None] * len(pool), "sales": {},
                                           "refund": {}, "risk": {}, "risk_err": None})
    monkeypatch.setattr(wf.product_pool, "score_all", lambda data: (pool, {}))
    monkeypatch.setattr(wf.store_perf, "load", lambda conn, win: {
        s: dict(rec_days=90, active_days=90, avg_online=100, orders=90,
                gross=9000.0, refund=0.0, hist_rows=0, net=9000.0)
        for s in ("A", "B")})
    monkeypatch.setattr(wf.claims, "load_active",
                        lambda conn, kind: (held_brand or {}) if kind == wf.claims.BRAND
                        else (held_prod or {}))
    monkeypatch.setattr(wf, "_pending_delist", lambda *a, **k: {})
    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda *a, **k: __import__("contextlib").nullcontext(_Conn(online)))
    if claimed is not None:
        monkeypatch.setattr(wf.claims, "claim_many",
                            lambda conn, rows: (claimed.extend(rows), [])[1] and (0, [])
                            or (len(rows), []))


def test_run_dry_run_writes_nothing_and_reports_the_funnel(monkeypatch, tmp_path):
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i}", 90.0 - i) for i in range(20)]
    landed = []
    _wire(monkeypatch, pool, claimed=landed)
    out = wf.run({"batch": 10, "execute": False})
    assert "dry-run:未落任何占用" in out and landed == []
    assert "候选漏斗" in out and "发牌结果" in out
    assert (tmp_path / "alloc_分配方案.csv").exists()


def test_run_execute_lands_claims(monkeypatch, tmp_path):
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i}", 90.0 - i) for i in range(20)]
    landed = []
    _wire(monkeypatch, pool, claimed=landed)
    out = wf.run({"batch": 10, "execute": True})
    assert "已落占用" in out and landed
    # 每组一个品牌 + 一个 ASIN;店只有 A/B 两家
    assert {r["store"] for r in landed} <= {"A", "B"}
    assert {r["kind"] for r in landed} == {wf.claims.BRAND, wf.claims.PRODUCT}


def test_run_refuses_when_no_store_can_take_goods(monkeypatch):
    """一家店都接不了货时**明说**,不要出一张空方案表让人以为没货可分。"""
    monkeypatch.setattr(wf.store_targets, "load_targets", lambda: {
        "A": {"categories": [], "channel": "FBA", "max_online": 0}})
    monkeypatch.setattr(wf.stores_svc, "registered_names", lambda: {"A"})
    monkeypatch.setattr(wf.product_pool, "load", lambda conn, win: {
        "pool": [], "sales": {}, "refund": {}, "risk": {}, "risk_err": None})
    monkeypatch.setattr(wf.product_pool, "score_all", lambda data: ([], {}))
    monkeypatch.setattr(wf.store_perf, "load", lambda conn, win: {})
    monkeypatch.setattr(wf.claims, "load_active", lambda conn, kind: {})
    monkeypatch.setattr(wf, "_pending_delist", lambda *a, **k: {})
    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda *a, **k: __import__("contextlib").nullcontext(_Conn({})))
    assert "没有一家店可以接货" in wf.run({})


def test_quota_is_an_integer_not_a_float(monkeypatch, tmp_path):
    """配额落成 float 会一路带到报告和 csv 里(「配额 10.0」)。

    成因是 `-(-a // b)` 这个整数向上取整的写法对 float 是地板除 —— 看起来
    对,类型悄悄变了。
    """
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i}", 90.0 - i) for i in range(20)]
    _wire(monkeypatch, pool, claimed=[])
    seen = {}
    real = wf.ae.deal
    monkeypatch.setattr(wf.ae, "deal",
                        lambda g, s, **k: (seen.update(s), real(g, s, **k))[1])
    wf.run({"batch": 10, "execute": False})
    assert seen and all(isinstance(v["quota"], int) for v in seen.values())


def test_funnel_does_not_divide_group_counts_by_product_counts(monkeypatch, tmp_path):
    """⚠ 漏斗里组数与产品数是两个单位,混在一列会骗人。

    60 个产品组成 20 个组时,把 20÷60 报成 33.3% 读起来像丢了三分之二的货 ——
    其实一件没丢(每组 3 件)。
    """
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    # 分数全在淘汰线以上,好让漏斗只剩"组队"这一处收窄
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i // 3}", 95.0 - i * 0.7) for i in range(60)]
    _wire(monkeypatch, pool, claimed=[])
    out = wf.run({"batch": 20, "execute": False})
    line = [x for x in out.splitlines() if "组队后·自由流" in x][0]
    assert "100.0%" in line and " 60 " in line   # 货位 60/60(**不是** 20/60=33%)
    assert line.rstrip().endswith("20")          # 组数单列一栏


# ── 定向流:两条会炸的纪律 ────────────────────────────────────────────

def _wire_directed(monkeypatch, pool, held_brand, room):
    monkeypatch.setattr(wf.store_targets, "load_targets", lambda: {
        "A": {"categories": ["家居"], "channel": "FBA", "max_online": room,
              "gmv": 400.0, "orders": 5.0}})
    monkeypatch.setattr(wf.stores_svc, "registered_names", lambda: {"A"})
    monkeypatch.setattr(wf.product_pool, "load", lambda conn, win: {
        "pool": [None] * len(pool), "sales": {}, "refund": {}, "risk": {},
        "risk_err": None})
    monkeypatch.setattr(wf.product_pool, "score_all", lambda data: (pool, {}))
    monkeypatch.setattr(wf.store_perf, "load", lambda conn, win: {
        "A": dict(rec_days=90, active_days=90, avg_online=100, orders=90,
                  gross=9000.0, refund=0.0, hist_rows=0, net=9000.0)})
    monkeypatch.setattr(wf.claims, "load_active",
                        lambda conn, kind: held_brand if kind == wf.claims.BRAND else {})
    monkeypatch.setattr(wf, "_pending_delist", lambda *a, **k: {})
    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda *a, **k: __import__("contextlib").nullcontext(
                            _Conn({"A": 0})))


def test_directed_flow_capacity_is_cumulative_not_per_group(monkeypatch, tmp_path):
    """⚠ 生产实测 2026-08-16:A142 剩余容量 1,918,定向流塞了 8,384。

    成因是逐组判 `size <= room` —— 十几个组各自都"塞得下",加起来撑爆四倍。
    容量必须**累计**。
    """
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    # 10 个已占品牌,各 5 件 = 50 件,而店里只剩 20 个货位
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i // 5}", 80.0) for i in range(50)]
    held = {f"brand{i}": "A" for i in range(10)}
    _wire_directed(monkeypatch, pool, held, room=20)
    landed = []
    monkeypatch.setattr(wf.claims, "claim_many",
                        lambda conn, rows: (landed.extend(rows), (len(rows), []))[1])
    wf.run({"batch": 10_000, "execute": True})
    assert len(landed) <= 20, "定向流突破了剩余容量"


def test_directed_flow_is_capped_by_the_batch(monkeypatch, tmp_path):
    """⚠ 定向流也吃批量。不受批量约束的话,写 batch=3000 会落 4 万条占用 ——
    而占用撤不回(生产实测 2026-08-16:批量 3,000,定向流 36,894 个货位)。
    """
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i // 5}", 80.0) for i in range(500)]
    held = {f"brand{i}": "A" for i in range(100)}
    _wire_directed(monkeypatch, pool, held, room=100_000)
    out = wf.run({"batch": 30, "execute": False})
    assert "排队等下一批" in out
    landed = []
    monkeypatch.setattr(wf.claims, "claim_many",
                        lambda conn, rows: (landed.extend(rows), (len(rows), []))[1])
    wf.run({"batch": 30, "execute": True})
    prods = [r for r in landed if r["kind"] == wf.claims.PRODUCT]
    assert len(prods) <= 30, f"定向流突破了批量:{len(prods)}"


def test_waiting_and_rejected_directed_groups_are_counted_apart(monkeypatch, tmp_path):
    """「本批额度用完」与「去不了占用店」处置完全不同:前者加大 batch 就能发,
    后者要所有者去改配置或释放品牌。混在一起报会让人去改不该改的东西。"""
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i}", 80.0) for i in range(10)]
    # 一半品牌归 A(能收家居),一半归一家不存在于本批的店
    held = {f"brand{i}": ("A" if i % 2 else "不接货店") for i in range(10)}
    _wire_directed(monkeypatch, pool, held, room=100_000)
    out = wf.run({"batch": 2, "execute": False})
    assert "排队等下一批" in out and "去不了" in out


def test_directed_flow_keeps_the_items_the_claiming_store_can_take(monkeypatch, tmp_path):
    """⚠ 定向流按**件**筛,不整组淘汰。

    所有者 2026-08-16 追问"定向流淘汰是什么意思"时发现的:组大类取的是件数
    多数派,所以一个品牌 60% 厨房 / 40% 家居,组大类 = 厨房 → 只做家居的
    占用店整组拒收,**连里面那 40% 本来能上架的家居商品一起**。
    去向店已被品牌占用固定死,不存在"该给谁"的竞争,按件筛是良定义的。
    """
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    items = ([_c(f"B0KIT{i:05d}", "acme", 80.0, cat="厨房") for i in range(6)]
             + [_c(f"B0HOM{i:05d}", "acme", 70.0, cat="家居") for i in range(4)])
    _wire_directed(monkeypatch, items, {"acme": "A"}, room=100_000)
    landed = []
    monkeypatch.setattr(wf.claims, "claim_many",
                        lambda conn, rows: (landed.extend(rows), (len(rows), []))[1])
    out = wf.run({"batch": 100, "execute": True})
    assert "按件筛掉 6 件" in out
    keys = {r["claim_key"] for r in landed}
    assert keys == {f"B0HOM{i:05d}" for i in range(4)}     # 4 件家居照常发


def test_fit_to_store_returns_none_when_nothing_survives():
    """一件都留不下才算真淘汰 —— 返回 None,由调用方归进"去不了"那一类。"""
    grp = {"key": "acme", "brand": "acme", "score": 80.0, "size": 2,
           "category": "厨房", "channel": "FBA", "store": "A",
           "items": [_c("B0AAAA0001", "acme", 80.0, cat="厨房"),
                     _c("B0AAAA0002", "acme", 70.0, cat="厨房")]}
    assert wf._fit_to_store(grp, {"categories": ["家居"]}) == (None, 2)


def test_fit_to_store_recomputes_score_and_category_after_trimming():
    """剪完要重算组分与组大类,否则方案表上写的是被剪掉那批的属性。"""
    grp = {"key": "acme", "brand": "acme", "score": 95.0, "size": 3,
           "category": "厨房", "channel": "FBA", "store": "A",
           "items": [_c("B0AAAA0001", "acme", 95.0, cat="厨房"),
                     _c("B0AAAA0002", "acme", 60.0, cat="家居"),
                     _c("B0AAAA0003", "acme", 50.0, cat="家居")]}
    kept, trimmed = wf._fit_to_store(grp, {"categories": ["家居"]})
    assert trimmed == 1 and kept["size"] == 2
    assert kept["score"] == 60.0 and kept["category"] == "家居"


def test_store_with_no_category_limit_takes_everything():
    """三列全空 = 不限制(store_targets.allowed 的口径),一件都不剪。"""
    grp = {"key": "acme", "brand": "acme", "score": 80.0, "size": 2,
           "category": "厨房", "channel": "FBA", "store": "A",
           "items": [_c("B0AAAA0001", "acme", 80.0, cat="厨房"),
                     _c("B0AAAA0002", "acme", 70.0, cat="家居")]}
    assert wf._fit_to_store(grp, {"categories": []}) == (grp, 0)
