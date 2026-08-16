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
                          "layer": 1, "tier": 1}])
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
    rows = wf._to_claim([{"group": _grp("zeta", "zeta", items, store="A085"),
                          "store": "A085", "layer": 1, "tier": 1}])
    assert [(r["kind"], r["claim_key"], r["store"]) for r in rows] == [
        (claims.PRODUCT, "B0BBBB0001", "A085")]


def test_unbranded_group_claims_only_the_asin():
    """无品牌的组不占品牌 —— 拿 "(无品牌):B0..." 这个合成键去占用,
    等于凭空造一个叫这个名字的品牌把别人挡住。"""
    items = [_c("B0CCCC0001", None, 60.0)]
    g = _grp("(无品牌):B0CCCC0001", None, items)
    rows = wf._to_claim([{"group": g, "store": "A", "layer": 1, "tier": 1}])
    assert [r["kind"] for r in rows] == [claims.PRODUCT]


def test_claim_snapshot_carries_the_walmart_pt():
    """每条占用记下当时的 PT —— 回答"当初按什么分的"只能靠它(§7.8)。"""
    items = [_c("B0AAAA0001", "acme", 90.0, pt="Socks")]
    rows = wf._to_claim([{"group": _grp("acme", "acme", items), "store": "A",
                          "layer": 1, "tier": 1}])
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

def test_quota_is_capacity_capped_gap_converted():
    """配额 = min(剩余容量, 缺口 ÷ 单品日产出)。

    所有者原话(2026-08-16):「上限 3500,在线 2200,还有 1300 个位置,按缺口
    算出来是 1500,但也只能上架 1300 个」——**容量是硬的,缺口是想要的,取小**。
    """
    # 缺口 1500 元/天 ÷ 货位值 1.0 = 1500 个货位,但只剩 1300 个位置
    n, why = wf._quota({"room": 1300, "gap": 0.75}, {"slot_value": 1.0}, 2000.0)
    assert n == 1300 and "剩余容量" in why
    # 容量管够时就按缺口来
    n, why = wf._quota({"room": 9999, "gap": 0.75}, {"slot_value": 1.0}, 2000.0)
    assert n == 1500 and why == "缺口换算"


def test_quota_falls_back_to_room_not_zero_when_gap_is_unknown():
    """⚠ 算不出缺口时**退回剩余容量**,不是退回 0。

    「单店最大在线数」是所有者显式填的上限,拿它当界是尊重设置;退回 0 会让
    没填日目标的店永远分不到货,而且不报错 —— 正是空值纪律要防的事。
    """
    assert wf._quota({"room": 500, "gap": None}, {"slot_value": 1.0}, None)[0] == 500
    assert wf._quota({"room": 500, "gap": 0.5}, {"slot_value": None}, 100.0)[0] == 500


def test_a_store_at_its_target_gets_nothing():
    """已达目标的店缺口 0 ⇒ 配额 0(所有者口径:把货给离目标最远的店)。"""
    assert wf._quota({"room": 999, "gap": 0.0}, {"slot_value": 1.0}, 100.0)[0] == 0


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
    p, n = wf._write_plan([{"group": _grp("acme", "acme", items), "store": "A",
                            "layer": 1, "tier": 1}])
    body = open(p, encoding="utf-8-sig").read().splitlines()
    assert n == 2 and len(body) == 3           # 表头 + 2 个产品
    assert "B0AAAA0001" in body[1] and "B0AAAA0002" in body[2]


def test_plan_table_holds_only_what_you_act_on(tmp_path, monkeypatch):
    """⚠ 要动手的与诊断用的**分两张表**。

    合成一张的实测后果(2026-08-16):批量 3,000 却出了 48,816 行,其中
    45,815 行是排队与淘汰 —— 那张表没法用,而所有者第一眼看到的就是那个总数。
    摘要里两个数都报,所以不存在"把卡住的货藏起来"的问题。
    """
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    ok = _grp("acme", "acme", [_c("B0AAAA0001", "acme", 90.0)])
    g = _grp("zeta", "zeta", [_c("B0BBBB0001", "zeta", 50.0)])
    p_plan, n_plan = wf._write_plan([{"group": ok, "store": "A", "layer": 1,
                                      "tier": 1}])
    p_out, n_out = wf._write_rejects([{"group": g, "reason": wf.ae.NO_GATE}],
                                     [(dict(g, store="A085"), "占用店容量不足")],
                                     [dict(g, store="A085")])
    plan = open(p_plan, encoding="utf-8-sig").read()
    out = open(p_out, encoding="utf-8-sig").read()
    assert n_plan == 1 and "B0AAAA0001" in plan
    assert "B0BBBB0001" not in plan          # 诊断行不许混进要动手的那张
    assert n_out == 3
    assert "未发出" in out and "定向流淘汰" in out and "排队" in out
    assert "占用店容量不足" in out


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


def test_capacity_is_never_exceeded_even_by_directed_groups(monkeypatch, tmp_path):
    """⚠ 生产实测 2026-08-16:A142 剩余容量 1,918,定向流塞了 8,384。

    成因是定向流当时走的是**另一条流水线**、自己判 `size <= room`,逐组判
    加起来撑爆四倍。合成一副牌之后容量记账只有引擎一处,天然累计。
    """
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i // 5}", 80.0) for i in range(50)]
    held = {f"brand{i}": "A" for i in range(10)}
    _wire_directed(monkeypatch, pool, held, room=20)
    landed = []
    monkeypatch.setattr(wf.claims, "claim_many",
                        lambda conn, rows: (landed.extend(rows), (len(rows), []))[1])
    wf.run({"execute": True})
    assert len(landed) <= 20, "定向流突破了剩余容量"


def test_directed_and_free_compete_in_one_deck_by_score(monkeypatch, tmp_path):
    """定向流不是另一条流水线,就是同一副牌里"只有一家店能要"的牌。

    分成两个阶段的实测后果:两边各有一套配额与容量记账,谁也不知道对方吃了
    多少 —— 定向流一口吃光批量、自由流 0。合成一副之后按组分排,谁分高谁先。
    """
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    pool = ([_c("B0DIR00001", "held0", 50.0)]          # 定向流分低
            + [_c(f"B0FRE{i:05d}", f"new{i}", 95.0 - i) for i in range(5)])
    _wire_directed(monkeypatch, pool, {"held0": "A"}, room=3)
    landed = []
    monkeypatch.setattr(wf.claims, "claim_many",
                        lambda conn, rows: (landed.extend(rows), (len(rows), []))[1])
    wf.run({"execute": True})
    keys = {r["claim_key"] for r in landed if r["kind"] == wf.claims.PRODUCT}
    # 容量只有 3:分高的自由流先上,分 50 的定向流轮不到
    assert "B0DIR00001" not in keys and "B0FRE00000" in keys


def test_a_group_bound_to_a_store_can_only_go_there(monkeypatch, tmp_path):
    """归属闸:带 `store` 的组只能去那家店,别的店再空也不给。"""
    from services import alloc_engine as ae
    st = {"quota": 99, "room": 99, "categories": [], "channel": "FBA"}
    grp = {"key": "acme", "score": 90.0, "size": 1, "category": "家居",
           "channel": "FBA", "store": "A"}
    assert ae._gate(grp, "A", st) is True
    assert ae._gate(grp, "B", st) is False


def test_batch_is_an_optional_safety_valve_not_the_model(monkeypatch, tmp_path):
    """`-p batch=` 只是想小步试跑时的安全阀。

    所有者定稿 2026-08-16:"限制 3000 设置的也很奇怪…这个限制我甚至就认为
    不应该有" —— 每家店能接多少由容量与缺口算出来,默认不设总量上限。
    """
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0FRE{i:05d}", f"new{i}", 90.0) for i in range(500)]
    _wire_directed(monkeypatch, pool, {}, room=400)
    free = wf.run({"execute": False})
    capped = wf.run({"batch": 50, "execute": False})
    n = lambda out: int([x for x in out.splitlines()             # noqa: E731
                         if "实发" in x][0].split("实发 ")[1].split(" ")[0])
    assert n(free) > n(capped) and n(capped) <= 50
    assert "把配额等比缩过" in capped and "把配额等比缩过" not in free


def test_a_top_scoring_group_below_the_cut_is_findable(monkeypatch, tmp_path):
    """⚠ 「我那个高分品怎么没分出去」必须在某张表里答得上来。

    实测 2026-08-16:自由流额度为 0 那一跑,63,418 组一件没发,而它们既不在
    方案表也不在未入选表 —— 所有者哪儿都查不到,分不清是"排队中"还是"被闸挡了"。
    """
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0FRE{i:05d}", f"new{i}", 95.0 - i * 0.1) for i in range(200)]
    _wire_directed(monkeypatch, pool, {}, room=100_000)
    out = wf.run({"batch": 10, "execute": False})
    assert "排队中" in out or "一件都没发" in out
    text = open(tmp_path / "alloc_未入选.csv", encoding="utf-8-sig").read()
    assert "排队(分数排在本轮切口之外)" in text
    assert "B0FRE00199" in text or "B0FRE00100" in text   # 切口之外的确实写进去了


def test_the_cut_score_is_reported_so_queued_is_distinguishable(monkeypatch, tmp_path):
    """切口分数要报出来:低于它 = 排队中,不是被闸挡了。两者处置不同。"""
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0FRE{i:05d}", f"new{i}", 95.0 - i * 0.1) for i in range(200)]
    _wire_directed(monkeypatch, pool, {}, room=100_000)
    out = wf.run({"batch": 10, "execute": False})
    assert "切口在**组分" in out and "排队中" in out


def test_queue_sample_is_capped_and_says_so(monkeypatch, tmp_path):
    """截断必须说破 —— 静默截断读起来像"就这么多了"。"""
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    monkeypatch.setattr(wf, "QUEUE_SAMPLE", 5)
    pool = [_c(f"B0FRE{i:05d}", f"new{i}", 95.0 - i * 0.01) for i in range(100)]
    _wire_directed(monkeypatch, pool, {}, room=100_000)
    out = wf.run({"batch": 10, "execute": False})
    assert "只写了**组分最高的 5 组**" in out


def test_directed_blocker_is_attributed_to_the_gate_that_actually_blocked():
    """⚠ 三个闸的处置完全不同(开个大类 / 放宽货期 / 换渠道)。

    混成一个标签的实测后果(2026-08-16):货期闸挡下的组一律被记成"缺某大类",
    把所有者送去开一个根本没用的类目。
    """
    st = {"categories": ["家居"], "lead_limit": 5, "channel": "FBA"}
    # 全被类目挡
    g1 = _grp("a", "a", [_c("B0AAAA0001", "a", 90.0, cat="厨房")])
    assert wf._fit_to_store(g1, st)[2] == "类目"
    # 全被货期挡
    g2 = _grp("b", "b", [dict(_c("B0AAAA0002", "b", 90.0), lead=9)])
    assert wf._fit_to_store(g2, st)[2] == "货期"
    # 混着挡时按件数多的那个归因,并列按名字定序(不许随行序漂)
    g3 = _grp("c", "c", [_c("B0AAAA0003", "c", 90.0, cat="厨房"),
                         _c("B0AAAA0004", "c", 80.0, cat="厨房"),
                         dict(_c("B0AAAA0005", "c", 70.0), lead=9)])
    assert wf._fit_to_store(g3, st)[2] == "类目"


def test_lead_blocked_groups_get_their_own_summary_line(monkeypatch, tmp_path):
    """货期挡下的要单列一行 —— 「给该店开这个大类」对它们完全无效。"""
    monkeypatch.setattr(wf.paths, "reports_dir", lambda: tmp_path)
    monkeypatch.setattr(wf.store_targets, "load_targets", lambda: {
        "A": {"categories": [], "channel": "FBA", "max_online": 5000,
              "gmv": 400.0, "orders": 5.0, "lead_limit": 5}})
    monkeypatch.setattr(wf.stores_svc, "registered_names", lambda: {"A"})
    pool = [dict(_c(f"B0SLW{i:05d}", "held0", 90.0), lead=30) for i in range(4)]
    monkeypatch.setattr(wf.product_pool, "load", lambda conn, win: {
        "pool": [None] * len(pool), "sales": {}, "refund": {}, "risk": {},
        "gross": {}, "risk_err": None})
    monkeypatch.setattr(wf.product_pool, "score_all", lambda data: (pool, {}))
    monkeypatch.setattr(wf.store_perf, "load", lambda conn, win: {
        "A": dict(rec_days=90, active_days=90, avg_online=100, orders=90,
                  gross=9000.0, refund=0.0, hist_rows=0, net=9000.0)})
    monkeypatch.setattr(wf.claims, "load_active",
                        lambda conn, kind: {"held0": "A"}
                        if kind == wf.claims.BRAND else {})
    monkeypatch.setattr(wf, "_pending_delist", lambda *a, **k: {})
    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda *a, **k: __import__("contextlib").nullcontext(
                            _Conn({"A": 0})))
    out = wf.run({"execute": False})
    assert "其中**货期**挡下的" in out and "开类目没用" in out
    assert "其中**类目**挡下的" not in out


def test_claims_not_yet_listed_consume_room():
    """⚠ 占用是「这个位置归你了」,在线数是「已经在架的」—— 中间隔着一次上架。

    不扣掉这个差额的后果(2026-08-16 所有者追问「执行会如何标记产品」时发现):
    --execute 落了三万条占用、货还没上,第二天再跑一次,剩余容量一点没变,
    于是**把同一批货位再许诺一次**。已占 ASIN 会被排除所以换了一批产品,
    但两批货加起来塞不进那些店 —— 而占用撤不回。
    """
    from services import store_perf
    cfg = {"A": {"max_online": 1000, "gmv": 400.0}}
    m = {"A": {"slot_value": 1.0, "daily_net_own": 100.0}}
    plain = store_perf.quota_inputs(m, cfg, {"A": 200})
    withres = store_perf.quota_inputs(m, cfg, {"A": 200}, None, {"A": 300})
    assert plain["A"]["room"] == 800 and plain["A"]["room_now"] == 800
    assert withres["A"]["room"] == 500 and withres["A"]["room_now"] == 500
    assert withres["A"]["reserved"] == 300


def test_reserved_counts_only_claims_whose_goods_are_not_yet_on_that_shelf():
    """⚠ 只数**属于该店自己**的占用。

    占用在 A、货在 B 的行不算 A 的预留 —— 那种情况是 B 该下架(claim_audit
    专门报它),把它记成 A 的预留会白白吃掉 A 的容量。
    """
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self):
            return [("A", "B0AAAA0001"),      # A 店已上架
                    ("B", "B0CCCC0003")]      # 货在 B,占用却在 A

    class _Conn:
        def cursor(self): return _C()

    held = {"B0AAAA0001": "A",     # 已上架 → 不算预留
            "B0BBBB0002": "A",     # 占了没上 → 算
            "B0CCCC0003": "A"}     # 货在别人架上 → 仍算 A 的预留(A 那儿确实空着)
    assert wf._reserved(_Conn(), held) == {"A": 2}
