"""分配方案回归。**这是全项目唯一会大批量落不可逆占用的工作流**,
所以每条都盯着"会把货分错、或者把占用落错"的具体路径。
"""

import pytest

from services import claims
from workflows import alloc_plan as wf


def _c(asin, brand, score, cat="Home", ch="FBA", pt="pt1"):
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

def test_headroom_is_gone_replaced_by_taking_another_round():
    """★ `HEADROOM`(切口 ×1.5)2026-08-22 删掉了,两个毛病:

    · **单位错配** —— `cut = int(total_q * HEADROOM)` 里 total_q 的单位是货位
      (产品),却拿去切**组**的列表。平均一组 k 件,牌堆实际装了约
      1.5×k×total_q 件货,比标称多出 k 倍,而报告还写着"切口 N 组(×1.5)";
    · **留余量这件事**已由"一轮不够就取下一个 N"取代(所有者 2026-08-22),
      那是按实际缺口续取,比拍一个倍数准。
    """
    assert not hasattr(wf, "HEADROOM")
    assert wf.ae.MAX_ROUNDS > 1


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
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
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
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    ok = _grp("acme", "acme", [_c("B0AAAA0001", "acme", 90.0)])
    g = _grp("zeta", "zeta", [_c("B0BBBB0001", "zeta", 50.0)])
    p_plan, n_plan = wf._write_plan([{"group": ok, "store": "A", "layer": 1,
                                      "tier": 1}])
    # ★ 排队的现在是**产品**不是组(切口挪到了产品层)
    p_out, n_out = wf._write_rejects([{"group": g, "reason": wf.ae.NO_CATEGORY}],
                                     [(dict(g, store="A085"), "占用店容量不足")],
                                     [_c("B0CCCC0001", "zeta", 40.0)])
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
        "A": {"categories": ["Home"], "channel": "FBA", "max_online": 5000,
              "gmv": 400.0, "orders": 5.0},
        "B": {"categories": ["Home"], "channel": "FBA", "max_online": 5000,
              "gmv": 400.0, "orders": 5.0}})
    monkeypatch.setattr(wf.stores_svc, "enabled_names", lambda: {"A", "B"})
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
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i}", 90.0 - i) for i in range(20)]
    landed = []
    _wire(monkeypatch, pool, claimed=landed)
    out = wf.run({"batch": 10, "execute": False})
    assert "dry-run:未落任何占用" in out and landed == []
    assert "候选漏斗" in out and "发牌结果" in out
    assert (tmp_path / "alloc_分配方案.csv").exists()


def test_run_execute_lands_claims(monkeypatch, tmp_path):
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
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
    monkeypatch.setattr(wf.stores_svc, "enabled_names", lambda: {"A"})
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
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
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
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    # 分数全在淘汰线以上,好让漏斗只剩"组队"这一处收窄
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i // 3}", 95.0 - i * 0.7) for i in range(60)]
    _wire(monkeypatch, pool, claimed=[])
    out = wf.run({"batch": 20, "execute": False})
    # ★ 重排之后进片的只有**本轮取到的**那 20 件(切口),不是全部 60 件 ——
    #   所以先核对「本轮取货」那一行,再核对组队行与它对得上
    took = [x for x in out.splitlines() if "本轮取货" in x][0]
    # ⚠ 22 而不是 20:`-p batch=` 是**逐店** ceil 等比缩的(2 家店各 11),
    #   总和会略微超过 batch —— 那是安全阀的取整,不是模型的一部分
    assert " 22 " in took, "取货量 = 各店配额之和(N),不是候选池全量 60"
    line = [x for x in out.splitlines() if "片内组队·自由流" in x][0]
    # 货位 12(每组 3 件 ⇒ 组数 8 中有 4 组只取到部分件);关键是**两个单位
    # 各占一栏**,而不是把 8÷20 报成 40% 读起来像丢了六成
    assert line.rstrip().endswith("8") and " 12 " in line


# ── 定向流:两条会炸的纪律 ────────────────────────────────────────────

def _wire_directed(monkeypatch, pool, held_brand, room):
    monkeypatch.setattr(wf.store_targets, "load_targets", lambda: {
        "A": {"categories": ["Home"], "channel": "FBA", "max_online": room,
              "gmv": 400.0, "orders": 5.0}})
    monkeypatch.setattr(wf.stores_svc, "enabled_names", lambda: {"A"})
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
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
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
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
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
    grp = {"key": "acme", "score": 90.0, "size": 1, "category": "Home",
           "lead": 3,
           "channel": "FBA", "store": "A"}
    assert ae._gate(grp, "A", st) is True
    assert ae._gate(grp, "B", st) is False


def test_batch_is_an_optional_safety_valve_not_the_model(monkeypatch, tmp_path):
    """`-p batch=` 只是想小步试跑时的安全阀。

    所有者定稿 2026-08-16:"限制 3000 设置的也很奇怪…这个限制我甚至就认为
    不应该有" —— 每家店能接多少由容量与缺口算出来,默认不设总量上限。
    """
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
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
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0FRE{i:05d}", f"new{i}", 95.0 - i * 0.1) for i in range(200)]
    _wire_directed(monkeypatch, pool, {}, room=100_000)
    out = wf.run({"batch": 10, "execute": False})
    assert "排队中" in out or "一件都没发" in out
    text = open(tmp_path / "alloc_未入选.csv", encoding="utf-8-sig").read()
    assert "排队(产品分排在本轮切口之外)" in text
    assert "B0FRE00199" in text or "B0FRE00100" in text   # 切口之外的确实写进去了


def test_the_cut_score_is_reported_so_queued_is_distinguishable(monkeypatch, tmp_path):
    """切口分数要报出来:低于它 = 排队中,不是被闸挡了。两者处置不同。"""
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0FRE{i:05d}", f"new{i}", 95.0 - i * 0.1) for i in range(200)]
    _wire_directed(monkeypatch, pool, {}, room=100_000)
    out = wf.run({"batch": 10, "execute": False})
    assert "切口在产品分" in out and "排队中" in out


def test_queue_sample_is_capped_and_says_so(monkeypatch, tmp_path):
    """截断必须说破 —— 静默截断读起来像"就这么多了"。"""
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    monkeypatch.setattr(wf, "QUEUE_SAMPLE", 5)
    pool = [_c(f"B0FRE{i:05d}", f"new{i}", 95.0 - i * 0.01) for i in range(100)]
    _wire_directed(monkeypatch, pool, {}, room=100_000)
    out = wf.run({"batch": 10, "execute": False})
    assert "只写了**产品分最高的 5 件**" in out


def test_directed_blocker_is_attributed_to_the_gate_that_actually_blocked():
    """⚠ 三个闸的处置完全不同(开个大类 / 放宽货期 / 换渠道)。

    混成一个标签的实测后果(2026-08-16):货期闸挡下的组一律被记成"缺某大类",
    把所有者送去开一个根本没用的类目。
    """
    st = {"categories": ["Home"], "lead_limit": 5, "channel": "FBA"}
    # 全被类目挡
    g1 = _grp("a", "a", [_c("B0AAAA0001", "a", 90.0, cat="Garden & Patio")])
    assert wf.ae._fit_to_store(g1, st)[2] == "类目"
    # 全被货期挡
    g2 = _grp("b", "b", [dict(_c("B0AAAA0002", "b", 90.0), lead=9)])
    assert wf.ae._fit_to_store(g2, st)[2] == "货期"
    # 混着挡时按件数多的那个归因,并列按名字定序(不许随行序漂)
    g3 = _grp("c", "c", [_c("B0AAAA0003", "c", 90.0, cat="Garden & Patio"),
                         _c("B0AAAA0004", "c", 80.0, cat="Garden & Patio"),
                         dict(_c("B0AAAA0005", "c", 70.0), lead=9)])
    assert wf.ae._fit_to_store(g3, st)[2] == "类目"


def test_lead_blocked_groups_get_their_own_summary_line(monkeypatch, tmp_path):
    """货期挡下的要单列一行 —— 「给该店开这个大类」对它们完全无效。"""
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    # ⚠ B 是**必需的**:池口按各店「配送时长限制」的并集筛,只有 A 的话 30 天的
    # 货连池子都进不去(而那正是所有者要的 —— 没人要的货不该进来)。
    # B 要**显式填 40**:2026-08-21 起未填不再是"不限"而是回落 7 天。
    # 这才是真实场景:一家店严、另一家松,严的那家要看得见"我是被货期挡的"
    monkeypatch.setattr(wf.store_targets, "load_targets", lambda: {
        "A": {"categories": [], "channel": "FBA", "max_online": 5000,
              "gmv": 400.0, "orders": 5.0, "lead_limit": 5},
        "B": {"categories": [], "channel": "FBA", "max_online": 5000,
              "gmv": 400.0, "orders": 5.0, "lead_limit": 40}})
    monkeypatch.setattr(wf.stores_svc, "enabled_names", lambda: {"A", "B"})
    pool = [dict(_c(f"B0SLW{i:05d}", "held0", 90.0), lead=30) for i in range(4)]
    monkeypatch.setattr(wf.product_pool, "load", lambda conn, win: {
        "pool": [None] * len(pool), "sales": {}, "refund": {}, "risk": {},
        "gross": {}, "risk_err": None})
    monkeypatch.setattr(wf.product_pool, "score_all", lambda data: (pool, {}))
    monkeypatch.setattr(wf.store_perf, "load", lambda conn, win: {
        s: dict(rec_days=90, active_days=90, avg_online=100, orders=90,
                gross=9000.0, refund=0.0, hist_rows=0, net=9000.0)
        for s in ("A", "B")})
    monkeypatch.setattr(wf.claims, "load_active",
                        lambda conn, kind: {"held0": "A"}
                        if kind == wf.claims.BRAND else {})
    monkeypatch.setattr(wf, "_pending_delist", lambda *a, **k: {})
    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda *a, **k: __import__("contextlib").nullcontext(
                            _Conn({"A": 0, "B": 0})))
    out = wf.run({"execute": False})
    assert "其中**货期**挡下的" in out and "开类目没用" in out
    assert "其中**类目**挡下的" not in out




# ── 占位 ≠ 上架(所有者定稿 2026-08-16)────────────────────────────────

def test_claimed_but_unlisted_products_stay_in_the_pool():
    """★ 所有者原话:「已占位和已上架是两回事…分配即占位」。

    上一轮定了、货还没上的 ASIN **照常进候选池**,只是只能回占用店。
    把它们剔掉的后果:方案表变成增量,上一轮的上架指令**从表上消失** ——
    而那恰恰是所有者要照着做的东西。
    """
    from services import alloc_groups as ag
    cands = [_c("B0AAAA0001", None, 90.0), _c("B0BBBB0002", None, 80.0)]
    r = ag.build(cands, {}, {"B0AAAA0001": "A085"})
    assert [g["key"] for g in r["free"]] == ["(无品牌):B0BBBB0002"]
    assert len(r["directed"]) == 1 and r["directed"][0]["store"] == "A085"


def test_a_claim_never_shrinks_the_stores_room():
    """⚠ **占位不消耗容量,上架才消耗**。

    两边都减就是双重扣减:那批货下一轮照样进池、重新占这些位置,而容量已经
    被它们扣过一次了 —— 店铺容量会凭空少一大截。
    """
    from services import store_perf
    cfg = {"A": {"max_online": 1000, "gmv": 400.0}}
    m = {"A": {"slot_value": 1.0, "daily_net_own": 100.0}}
    q = store_perf.quota_inputs(m, cfg, {"A": 200})
    assert q["A"]["room"] == 800 and q["A"]["room_now"] == 800


def test_pool_excludes_listed_not_claimed():
    """候选池排除的是**已在架**的;规划外店(谭总系)的在架行不算。"""
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self):
            return [("A085", "B0AAAA0001"),      # 规划内、在架 → 排除
                    ("谭总3", "B0BBBB0002"),      # 规划外 → 不排除
                    ("没在册", "B0CCCC0003")]     # 不在册 → 不排除

    class _Conn:
        def cursor(self): return _C()

    assert wf._listed_asins(_Conn(), {"A085", "谭总3"}) == {"B0AAAA0001"}


def test_out_of_band_stores_get_their_layer_histogram_printed(monkeypatch, tmp_path):
    """⚠ 「顶层越界」只说了有问题,没说问题在哪。

    实测 2026-08-16:两家店比值 0.00、自由流各 3,868 / 2,139 件(远超小样本
    门槛),报告里却查不出货来自哪几层 —— 只能手工翻方案表。层分布摊开之后,
    "全堆在末尾几层"就直接指向"这家店过不了前面几层的闸"。
    """
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    # A 收 FBA、B 收 FBM;高分的全是 FBA ⇒ B 只能从靠后的层拿货,比值必然越界
    pool = ([_c(f"B0FBA{i:05d}", f"fa{i}", 95.0 - i * 0.1, ch="FBA")
             for i in range(300)]
            + [_c(f"B0FBM{i:05d}", f"fm{i}", 60.0 - i * 0.1, ch="FBM")
               for i in range(300)])
    _wire(monkeypatch, pool, claimed=[])
    monkeypatch.setattr(wf.store_targets, "load_targets", lambda: {
        "A": {"categories": ["Home"], "channel": "FBA", "max_online": 5000,
              "gmv": 400.0, "orders": 5.0},
        "B": {"categories": ["Home"], "channel": "FBM", "max_online": 5000,
              "gmv": 400.0, "orders": 5.0}})
    out = wf.run({"execute": False})
    assert "越界店的自由流层分布" in out
    assert [x for x in out.splitlines() if "×" in x and "L" in x]


def test_conflicting_asin_and_brand_claims_are_counted_not_silently_shipped():
    """组归 A、组里却有被 B 占着的 ASIN = 两条占用互相矛盾。

    方案表不该写"把它上到 A"(落库时 claim_many 会拒掉并报冲突),所以剔掉
    并计数 —— 让人看见有多少条占用打架,而不是等发现方案表里有条指令做不到。
    """
    from services import alloc_groups as ag
    cands = [_c("B0AAAA0001", "acme", 90.0), _c("B0AAAA0002", "acme", 80.0)]
    r = ag.build(cands, {"acme": "A"}, {"B0AAAA0002": "B"})
    assert r["dropped"]["ASIN 占用与组归属矛盾"] == 1
    assert [x["asin"] for x in r["directed"][0]["items"]] == ["B0AAAA0001"]


def test_all_dashes_says_whether_the_top_layer_had_any_free_flow(
        monkeypatch, tmp_path):
    """⚠ 全表「—」有两种成因,读反了会得出相反结论。

    ① 每家自由流量都没过门槛 = 样本太小,别当信号;
    ② **顶层一件自由流都没有** ⇒ base=0 ⇒ 全体一律 None —— 这时门槛过没过
       根本不影响结果,而含义是**反堆积这一轮压根没被验证过**。

    生产实测 2026-08-16:定向流 25,420 / 自由流 3,078 那一跑,15 家全是「—」,
    其中好几家的自由流远超门槛(82杨乾良 587 件 vs 门槛 386)—— 照 ① 去读
    会以为"量小而已,没事"。
    """
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    # 高分的**每个都是独立的已占品牌** —— 牌堆顶部整整几层全是定向流,
    # 自由流只能从下面拿。合成一个品牌不行:那只是一张牌,占不满 L1
    pool = ([_c(f"B0OWNED{i:04d}", f"own{i}", 95.0 - i * 0.01) for i in range(300)]
            + [_c(f"B0FREE{i:05d}", f"new{i}", 50.0 - i * 0.01) for i in range(300)])
    _wire(monkeypatch, pool, held_brand={f"own{i}": "A" for i in range(300)},
          claimed=[])
    out = wf.run({"execute": False})
    assert "本轮全表无比值" in out and "顶层一件自由流都没有" in out
    assert "反堆积这一轮没有被验证过" in out


def test_a_full_store_is_diagnosed_as_full_not_as_missing_a_category(
        monkeypatch, tmp_path):
    """⚠ 容量闸必须压在类目/货期之上 —— 店满了的时候,类目对不对不影响结果。

    实测 2026-08-16:A154杨凯迪 剩余容量 0,报告把它记成「缺 Arts & Crafts
    161 件 / 缺 Garden & Patio 113 件」,还附一句"给该店开这个大类就能救回"
    —— 把所有者送去做一件买不到任何货位的事。而且同一家满店,类目**碰巧对上**
    的那些组走的是另一条路(进牌堆 → 容量塞不下 → "等下架腾位"),
    同一个原因两种说法。
    """
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0FULL{i:04d}", "acme", 90.0 - i, cat="Garden & Patio") for i in range(5)]
    # 品牌归 A,而 A 只做 Home 且已经装满(在线 5000 = 上限 5000)。
    # ⚠ B 必须做 Hardlines,否则这批货在**池口**就因"没有店要这个品类"出局,
    # 复现不出原 bug —— 而原 bug 恰恰发生在"货有别的店能要、只是被品牌绑在
    # 一家满店上"的时候
    _wire(monkeypatch, pool, held_brand={"acme": "A"}, claimed=[])
    monkeypatch.setattr(wf.store_targets, "load_targets", lambda: {
        "A": {"categories": ["Home"], "channel": "FBA", "max_online": 5000,
              "gmv": 400.0, "orders": 5.0},
        "B": {"categories": ["Garden & Patio"], "channel": "FBA",
              "max_online": 5000, "gmv": 400.0, "orders": 5.0}})
    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda *a, **k: __import__("contextlib").nullcontext(
                            _Conn({"A": 5000, "B": 100})))
    out = wf.run({"execute": False})
    assert "占用店容量已满" in out and "容量已满**挡下的" in out
    # 满店不许出现在类目清单里 —— 那句"开这个大类就能救回"对它是假的
    cat_lines = [x for x in out.splitlines() if "缺「" in x]
    assert not any("A 缺" in x for x in cat_lines), cat_lines


def test_no_low_scorer_can_ride_along_any_more(monkeypatch, tmp_path):
    """★ 所有者 2026-08-16 追问「产品分 38.6 和 91.4 是怎么混到一起去的」——
    2026-08-22 重排把这件事从"只报不治"变成了**结构上不可能**。

    旧版:切口切在组这一层、组分取组内最高 ⇒ 一个 95 分的爆款把同品牌三个
    42 分的货一起拉进牌堆。所有者原话:"你会把大量排名靠后的产品拉到前面来"。
    新版:先按产品分取 top-N,组只由**取到的产品**组成 ⇒ 组内每一件都在
    切口之上,不可能有搭车的。

    这条同时也是「搭车上架」那一节被撤掉的理由 —— 报一个恒为 0 的数是噪声。
    """
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    pool = ([_c("B0HIGH0001", "acme", 95.0)]
            + [_c(f"B0LOW{i:05d}", "acme", 42.0) for i in range(3)]
            + [_c(f"B0MID{i:05d}", f"mid{i}", 50.0 - i * 0.01) for i in range(60)])
    _wire_directed(monkeypatch, pool, {}, room=10)
    out = wf.run({"execute": False})
    assert "搭车上架" not in out
    # 方案表里一件 42 分的都不该有:它们排在切口之外
    plan = open(tmp_path / "alloc_分配方案.csv", encoding="utf-8-sig").read()
    assert "B0LOW" not in plan
    assert "B0HIGH0001" in plan          # 而那个 95 分的照常发出去


def test_report_gives_the_real_entry_score_not_just_the_candidate_cut(
        monkeypatch, tmp_path):
    """⚠ **入场线 = 最后发出去的那张牌的分**,不是候选切口。

    实测 2026-08-16:候选切口 42,705 组(组分 43.0),但配额在第 6,517 张牌上
    就填满了 —— 只报切口会让人以为 43 分的货都进来了,差着十几分。所有者按
    "每层 23,000 个、要选两万多个"推算分配行为时,用的就是那个错前提。
    """
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    pool = [_c(f"B0FRE{i:05d}", f"new{i}", 95.0 - i * 0.1) for i in range(500)]
    _wire_directed(monkeypatch, pool, {}, room=10)      # 只装得下 10 件
    out = wf.run({"execute": False})
    assert "实际入场线:组分" in out
    entry = float(out.split("实际入场线:组分 ")[1].split("**")[0])
    cut = float(out.split("切口在产品分 ")[1].split(" ")[0])
    assert entry > cut, "入场线必须高于候选切口 —— 配额先填满"
    assert "这不是入场线" in out


def test_unplaced_breakdown_splits_the_three_actions_apart():
    """未发出的货必须按**所有者能做的那三个动作**摊开,不能只报一个总数。

    三个动作互不相干:给某店开大类 / 放宽某店的配送时长 / 给某店配上渠道。
    2026-08-21 实测,旧摘要把三者压成一句「要它出货得先给某店开这个大类」,
    所有者据此改了一轮飞书类目配置,而实际拦路的是货期 —— 一件没救回来。
    """
    def u(reason, cat="Home", ch="FBA", lead=3, n=2):
        items = [_c(f"B0{reason[:4]}{i:06d}", "b", 50.0, cat=cat, ch=ch)
                 for i in range(n)]
        g = _grp(f"g-{reason}-{cat}-{lead}", "b", items)
        g["lead"] = lead
        return {"group": g, "reason": reason}

    lines = wf._unplaced_breakdown([
        u(wf.ae.NO_CATEGORY, cat="Furniture", n=5),
        u(wf.ae.NO_LEAD, lead=12, n=3),
        u(wf.ae.NO_LEAD, lead=None, n=7),      # 采不到 ≠ 慢,处置完全不同
        u(wf.ae.NO_CHANNEL, ch="FBM", n=4),
    ])
    body = "\n".join(lines)
    assert "Furniture 5 件" in body and "给任意一家店开这个大类" in body
    assert "12 天 3 件" in body and "开类目没用" in body
    assert "货期未知**挡下的 7 件" in body and "补采集就能救" in body
    assert "FBM 4 件" in body
    # ⚠ 未知货期不许混进"按组货期"那一行 —— 混进去就成了 "None 天 7 件"
    assert "None" not in body


def test_goods_beyond_every_store_condition_leave_at_the_funnel(monkeypatch,
                                                                tmp_path):
    """⚠ 没有任何店的条件容得下的货,在**漏斗**里出局,不在"未发出"里躺着。

    条件全从限额表读(渠道 / 配送时长 / 准入类目),**一个字面量都不写** ——
    所有者 2026-08-21:「每一个店……的限制都在表格里……再拿着条件去拿品过来
    分配」。这里三条各来一件,验的是三条都按表判、且归因分得开。

    为什么必须在漏斗出局:挂在"未发出"名下会骗人 —— 那个标签的意思是
    "配置一改就能救",而这批要的是**开一家新店/新渠道**。生产实测 43,573 件
    FBM 就是这么混进 50,063 件"卡住的货"里,把真正要动手的 6,490 件 FBA 淹掉的。
    """
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    pool = ([_c(f"B0OK00{i:04d}", f"ok{i}", 90.0 - i * 0.1) for i in range(20)]
            # 分更高,但三条各自出局:渠道没人做 / 比最宽的店还慢 / 品类没人做
            + [_c(f"B0FBM{i:05d}", f"fm{i}", 95.0 - i * 0.1, ch="FBM")
               for i in range(3)]
            + [dict(_c(f"B0SLW{i:05d}", f"sl{i}", 95.0), lead=30) for i in range(4)]
            + [_c(f"B0CAT{i:05d}", f"ct{i}", 95.0, cat="Animals")
               for i in range(5)])
    _wire(monkeypatch, pool, claimed=[])
    monkeypatch.setattr(wf.store_targets, "load_targets", lambda: {
        "A": {"categories": ["Home"], "channel": "FBA", "max_online": 5000,
              "gmv": 400.0, "orders": 5.0, "lead_limit": 7}})
    out = wf.run({"execute": False})
    assert "去掉没有店要的" in out
    assert "渠道 FBM 3" in out                    # 没人做这个渠道
    assert "配送超 7 天" in out and "4" in out    # 比最宽的店还慢
    assert "品类 FCHW 5" in out                   # 没人做这个品类
    for f in ("alloc_未入选.csv", "alloc_分配方案.csv"):
        txt = (tmp_path / f).read_text(encoding="utf-8-sig")
        for pre in ("B0FBM", "B0SLW", "B0CAT"):
            assert pre not in txt, (f, pre)       # 不许在任何一张表里占篇幅
    plan = (tmp_path / "alloc_分配方案.csv").read_text(encoding="utf-8-sig")
    assert "B0OK00" in plan


# ── 取货对账(所有者 2026-08-22 追问)──────────────────────────────────

def _wire_tight(monkeypatch, pool):
    """A 只收 Home 且容量大;B 收 Animals 但**容量只剩 5**。

    这样 Animals 的货过得了漏斗那道「至少有一家店的条件容得下」(B 收),
    却在发牌时被 B 的容量/配额挡下 —— 正是"取进来了却发不出去"的那批。
    """
    _wire(monkeypatch, pool, claimed=[])
    monkeypatch.setattr(wf.store_targets, "load_targets", lambda: {
        "A": {"categories": ["Home"], "channel": "FBA", "max_online": 5000,
              "gmv": 400.0, "orders": 5.0},
        "B": {"categories": ["Animals"], "channel": "FBA", "max_online": 105,
              "gmv": 400.0, "orders": 5.0}})


def test_the_take_ledger_accounts_for_every_product(monkeypatch, tmp_path):
    """★ 所有者:「只有 26,785 个货位为什么取了 50,457 件?」

    因为取货按**产品**取,而取进来的未必发得出去。这几笔此前分散在报告三处、
    **单位还不一样**(未发出报的是组数),人只能自己去减。

    ⚠ 这条对账一定要**能对上**。对不上就说明有一笔货在实现里凭空消失了 ——
    那种 bug 从任何单项数字上都看不出来,只有做减法才会露头。
    """
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    pool = ([_c(f"B0OK{i:06d}", f"ok{i}", 90.0 - i * 0.1) for i in range(30)]
            + [_c(f"B0NO{i:06d}", f"no{i}", 80.0 - i * 0.1, cat="Animals")
               for i in range(60)])
    _wire_tight(monkeypatch, pool)
    out = wf.run({"execute": False})
    line = [x for x in out.splitlines() if "取货去向:" in x][0]
    assert "对不上取货" not in line, line
    assert "实发" in line


def test_unplaced_is_reported_in_items_not_only_groups(monkeypatch, tmp_path):
    """未发出要跟「取货」「实发」同单位(件),否则三个数没法互相对。"""
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    pool = ([_c(f"B0OK{i:06d}", f"ok{i}", 90.0 - i * 0.1) for i in range(10)]
            + [_c(f"B0NO{i:06d}", f"no{i}", 80.0 - i * 0.1, cat="Animals")
               for i in range(60)])
    _wire_tight(monkeypatch, pool)
    out = wf.run({"execute": False})
    # ⚠ 取「未发出 …」开头那一行 —— 对账行里也提到"未发出"三个字
    line = [x for x in out.splitlines() if x.strip().startswith("未发出")][0]
    assert "组 /" in line and "件:" in line


def test_hitting_the_round_guard_is_shouted_not_swallowed(monkeypatch, tmp_path):
    """⚠ 撞到 `MAX_ROUNDS` 护栏而停 ≠ 发完了。

    静默截断在这里最危险:报告会说"未发出 N 组",读起来像"闸挡的",而真相是
    我们**根本没往下取**。所有者会去改配置,该做的却是提高护栏或缩小配额。
    """
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    # ⚠ 不能 monkeypatch `ae.MAX_ROUNDS`:它是 `deal` 的**默认参数**,def 时就
    #   绑好了,改模块常量对已定义的函数无效(改源码有效,打补丁无效)。
    #   这里包一层把 max_rounds 显式传进去,走的仍是真实渲染路径
    real = wf.ae.deal
    monkeypatch.setattr(wf.ae, "deal",
                        lambda *a, **k: real(*a, **{**k, "max_rounds": 1}))
    pool = ([_c(f"B0OK{i:06d}", f"ok{i}", 90.0 - i * 0.01) for i in range(20)]
            + [_c(f"B0NO{i:06d}", f"no{i}", 89.0 - i * 0.01, cat="Animals")
               for i in range(600)])
    _wire_tight(monkeypatch, pool)
    out = wf.run({"execute": False})
    assert "撞到轮次护栏" in out and "不是发完了" in out
