"""点名分配(`alloc_plan -p from_sheet=1`,口径 #19,所有者定稿 2026-09-17)回归。

全库分配的回归在 tests/test_alloc_plan.py,这里只钉点名模式**多出来**的东西:
候选池只取点名的 ASIN(在 SQL 里筛)、逐行结局与回写、发牌序按缺口、
参数闸(打错字不许退化成全库真跑)、先落库再回写表。
"""

import contextlib

import pytest

from services import product_pool
from workflows import alloc_plan as wf

#: 一行池行 = product_pool._SQL_POOL 的 13 列(顺序必须逐字对齐)。
def _raw(asin, brand, cat="Home", price=10.0, shipping=2.0, stock=50, lead=3,
         ful="FBA", rating="4.5", reviews="100", manuf=None):
    return (asin, brand, manuf, "pt1", cat, price, shipping, stock, "in_stock",
            lead, rating, reviews, ful)


def _row(rownum, asin, store=""):
    return {"rownum": rownum, "asin": asin.upper(), "asin_raw": asin, "store": store}


#: 两家店同配置;经营水平故意反着来:A 卖得好(缺口 25%)、B 卖得差(缺口 75%)。
#: 字母序 A 在前,缺口序 B 在前 —— 两种发牌序在这张桌上会给出不同答案。
CFG = {"A": {"categories": ["Home"], "channel": "FBA", "max_online": 5000,
             "gmv": 400.0, "orders": 5.0},
       "B": {"categories": ["Home"], "channel": "FBA", "max_online": 5000,
             "gmv": 400.0, "orders": 5.0}}
PERF = {"A": dict(rec_days=90, active_days=90, avg_online=100, orders=90,
                  gross=27000.0, refund=0.0, hist_rows=0, net=27000.0),
        "B": dict(rec_days=90, active_days=90, avg_online=100, orders=90,
                  gross=9000.0, refund=0.0, hist_rows=0, net=9000.0)}


class _Cur:
    """按 SQL 特征分发的假游标:在线数 / 在架 SKU / 池外 ASIN 三问各答各的。"""

    def __init__(self, world):
        self.w, self._r = world, []

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, args=None):
        if "count(*)" in sql:
            self._r = list(self.w["online"].items())
        elif "listing_sources" in sql:
            self._r = list(self.w["listed"])
        elif "audit_status" in sql:
            want = set((args or {}).get("asins") or [])
            self.w["absent_asked"] = sorted(want)
            self._r = [r for r in self.w["absent"] if r[0] in want]
        else:
            self._r = []

    def executemany(self, sql, seq): pass       # ops.store_events 落行

    def fetchall(self): return list(self._r)


class _Conn:
    def __init__(self, world): self.w = world

    def cursor(self): return _Cur(self.w)


def _wire(monkeypatch, tmp_path, *, rows, raw, held_brand=None, held_prod=None,
          listed=(), absent=(), online=None, gross=None, conflicts=None):
    """输入:点名表行 + 池行 + 世界状态 → 输出:{written, landed, asked, world} 捕获器。"""
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    monkeypatch.setattr(wf.store_targets, "load_targets",
                        lambda: {k: dict(v) for k, v in CFG.items()})
    monkeypatch.setattr(wf.stores_svc, "enabled_names", lambda: {"A", "B"})
    monkeypatch.setattr(wf.alloc_sheet, "read_targets", lambda: [dict(r) for r in rows])
    cap = {"written": [], "landed": [], "asked": {}}
    monkeypatch.setattr(
        wf.alloc_sheet, "write_rows",
        lambda ups, execute=True: (cap["written"].extend(ups), len(ups))[1]
        if execute else 0)

    def _load(conn, win, asins=None):
        cap["asked"]["asins"] = asins
        return {"pool": [r for r in raw if asins is None or r[0] in asins],
                "sales": {}, "gross": dict(gross or {}), "refund": {},
                "risk": {}, "risk_err": None}
    monkeypatch.setattr(wf.product_pool, "load", _load)
    monkeypatch.setattr(wf.store_perf, "load", lambda conn, win: PERF)
    monkeypatch.setattr(wf.claims, "load_active",
                        lambda conn, kind: dict(held_brand or {})
                        if kind == wf.claims.BRAND else dict(held_prod or {}))
    monkeypatch.setattr(wf, "_pending_delist", lambda *a, **k: {})
    world = {"online": online or {"A": 100, "B": 100}, "listed": list(listed),
             "absent": list(absent)}
    cap["world"] = world
    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda *a, **k: contextlib.nullcontext(_Conn(world)))
    monkeypatch.setattr(
        wf.claims, "claim_many",
        lambda conn, rs: (cap["landed"].extend(rs),
                          (len(rs), list(conflicts or []), list(rs)))[1])
    return cap


def _by_row(cap) -> dict:
    return {rn: vals for rn, vals in cap["written"]}


# ── 参数闸:打错字不许退化成全库真跑 ─────────────────────────────────

def test_unknown_params_are_refused_before_any_work(monkeypatch):
    """`-p fromsheet=1`(少个下划线)静默吞掉 = 全库分配并真落几千条占用。"""
    monkeypatch.setattr(wf.store_targets, "load_targets",
                        lambda: pytest.fail("参数都不认识还往下跑"))
    with pytest.raises(ValueError, match="fromsheet"):
        wf.run({"fromsheet": "1", "execute": True})
    # cli 自己塞进来的键放行(product_audit 2026-08-16 dry_run 上线当天炸过)
    assert wf._CLI_INJECTED == {"execute", "dry_run"}


def test_cutoff_needs_from_sheet_and_batch_is_refused_in_sheet_mode(monkeypatch):
    monkeypatch.setattr(wf.store_targets, "load_targets",
                        lambda: pytest.fail("参数组合不对还往下跑"))
    with pytest.raises(ValueError, match="cutoff"):
        wf.run({"cutoff": "30", "execute": False})
    with pytest.raises(ValueError, match="batch"):
        wf.run({"from_sheet": "1", "batch": "50", "execute": False})


# ── 积木:池口白名单 / 逐 ASIN 淘汰原因 / 建店开关 / 在架店 / 池外原因 ──

class _SqlCur:
    def __init__(self): self.calls = []

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, args=None): self.calls.append((sql, args))

    def fetchall(self): return []


def test_pool_load_filters_in_sql_only_when_asins_are_named():
    cur = _SqlCur()
    conn = type("C", (), {"cursor": lambda self: cur, "rollback": lambda self: None})()
    win = {"as_of": "2026-09-17", "days": 365}
    product_pool.load(conn, win)
    sql, args = cur.calls[0]
    assert sql == product_pool._SQL_POOL and args is None     # 缺省路径逐字不变
    product_pool.load(conn, win, asins=["B0AAAA0001", "B0BBBB0002"])
    sql, args = cur.calls[4]
    assert sql.endswith(product_pool._POOL_ASIN_FILTER)
    assert "ANY(%(asins)s)" in sql and args == {"asins": ["B0AAAA0001", "B0BBBB0002"]}


def test_score_all_reports_each_gated_asin_when_asked():
    """计数那份只留归类名(「库存不足」),逐 ASIN 那份要**完整原因**(带数字)。"""
    data = {"pool": [_raw("B0AAAA0001", "acme", stock=2),
                     _raw("B0BBBB0002", "acme", price=None),
                     _raw("B0CCCC0003", "acme")],
            "sales": {}, "gross": {}, "refund": {}, "risk": {}, "risk_err": None}
    why = {}
    scored, gated = product_pool.score_all(data, gated_by_asin=why)
    assert [c["asin"] for c in scored] == ["B0CCCC0003"]
    assert gated == {"库存不足": 1, "落地价算不出": 1}
    assert why["B0AAAA0001"].startswith("库存不足(2 <") and "落地价" in why["B0BBBB0002"]
    # 不要收集器时行为一字不变
    assert product_pool.score_all(data)[1] == gated


def test_build_stores_by_gap_orders_by_need_and_flattens_tiers():
    """甲1(所有者定稿 2026-09-17):点名模式 fit = 缺口比例、梯队拍平;全库模式不动。"""
    online = {"A": 100, "B": 0}                       # B 是空店
    stores, *_ = wf._build_stores(PERF, CFG, {"A", "B"}, online, {}, 90, None,
                                  by_gap=True)
    assert stores["A"]["fit"] == pytest.approx(0.25)
    assert stores["B"]["fit"] == pytest.approx(0.75)
    assert {v["tier"] for v in stores.values()} == {1}
    stores, *_ = wf._build_stores(PERF, CFG, {"A", "B"}, online, {}, 90, None)
    assert {v["fit"] for v in stores.values()} == {0.0}
    assert (stores["A"]["tier"], stores["B"]["tier"]) == (1, 2)


def test_listed_where_lists_every_planning_store_the_asin_is_online_in():
    rows = [("A085", "B0AAAA0001", "B0AAAA0001"), ("B012", "B0AAAA0001", None),
            ("谭总3", "B0BBBB0002", "B0BBBB0002")]         # 规划外店不算
    conn = _Conn({"online": {}, "listed": rows, "absent": []})
    assert wf._listed_where(conn, {"A085", "B012", "谭总3"}) == {
        "B0AAAA0001": ["A085", "B012"]}
    assert wf._listed_asins(conn, {"A085", "B012", "谭总3"}) == {"B0AAAA0001"}


def test_why_absent_names_the_pool_condition_that_blocked_the_asin():
    absent = [("B0AAAA0001", "acme", None, True, "pt1", "Home"),
              ("B0BBBB0002", "zeta", "rejected", True, "pt1", "Home"),
              ("B0CCCC0003", "mid", "approved", False, "pt1", "Home"),
              ("B0DDDD0004", "mid", "approved", True, "unknown", None),
              ("B0EEEE0005", "mid", "approved", True, "pt9", "")]
    conn = _Conn({"online": {}, "listed": [], "absent": absent})
    got = wf._why_absent(conn, ["B0AAAA0001", "B0BBBB0002", "B0CCCC0003",
                                "B0DDDD0004", "B0EEEE0005", "B0FFFF0006"])
    assert got["B0AAAA0001"] == ("未审核", "acme")
    assert got["B0BBBB0002"][0].startswith("审核未过")
    assert "无标题" in got["B0CCCC0003"][0]
    assert "PT 未映射" in got["B0DDDD0004"][0]
    assert "大类" in got["B0EEEE0005"][0]
    assert got["B0FFFF0006"] == ("不在库(产品库没有这个 ASIN)", None)
    assert wf._why_absent(conn, []) == {}


def test_sheet_row_covers_exactly_the_machine_fields():
    from services import alloc_sheet
    vals = wf._sheet_row({"flow": "自由流", "store": "A"},
                         {"brand": "Acme", "category": "Home", "price": 10.0,
                          "shipping": 2.5, "score": 66.66, "penalty": 0.0,
                          "gross": None, "lead": 3})
    assert set(vals) == set(alloc_sheet.script_fields())
    assert (vals["store"], vals["brand"], vals["score"], vals["landed_price"],
            vals["gross"], vals["online"], vals["lead"]) == (
        "A", "Acme", 66.7, 12.5, "", "否", 3)
    # 池外的品:拿不到的列留空,不写 0
    empty = wf._sheet_row({"flow": "未分配", "why": "不在库"}, {"brand": None})
    assert empty["score"] == "" and empty["super_category"] == "" and empty["landed_price"] == ""


# ── 端到端:只取点名的、逐行结局、先落库再回写 ──────────────────────

def test_sheet_mode_reads_only_the_named_asins_and_skips_rows_with_a_store(
        monkeypatch, tmp_path):
    rows = [_row(2, "b0aaaa0001"), _row(3, "B0BBBB0002", store="A"), _row(4, "B0AAAA0001")]
    cap = _wire(monkeypatch, tmp_path, rows=rows,
                raw=[_raw("B0AAAA0001", "acme"), _raw("B0BBBB0002", "zeta")])
    out = wf.run({"from_sheet": "1", "cutoff": "0", "execute": True})
    # 池口白名单 = 待处理行的 ASIN(去重、归一);已有店铺的行连读都不读
    assert cap["asked"]["asins"] == ["B0AAAA0001"]
    assert sorted(rn for rn, _ in cap["written"]) == [2, 4]
    assert "已有店铺 1 行(重跑跳过)" in out and "本轮处理 2 行 / 1 个 ASIN" in out
    assert (tmp_path / "alloc_点名分配.csv").exists()


def test_sheet_mode_writes_one_outcome_per_row(monkeypatch, tmp_path):
    """六种结局各一行:自由流 / 定向回占用店 / 已在架 / 不在库 / 硬闸 / 格式不对。"""
    rows = [_row(2, "B0FREE0001"), _row(3, "B0HELD0002"), _row(4, "B0LIST0003"),
            _row(5, "B0NONE0004"), _row(6, "B0GATE0005"), _row(7, "怪东西")]
    raw = [_raw("B0FREE0001", "acme"), _raw("B0HELD0002", "zeta"),
           _raw("B0LIST0003", "mid"), _raw("B0GATE0005", "mid", stock=1)]
    cap = _wire(monkeypatch, tmp_path, rows=rows, raw=raw,
                held_brand={"zeta": "A"},
                listed=[("A", "B0LIST0003", "B0LIST0003"), ("B", "B0LIST0003", None)],
                absent=[("B0NONE0004", "Ghost", None, True, "pt1", "Home")],
                gross={"B0FREE0001": 123.456})
    out = wf.run({"from_sheet": "1", "cutoff": "0", "execute": True})
    w = _by_row(cap)
    assert set(w) == {2, 3, 4, 5, 6, 7}
    assert (w[2]["flow"], w[2]["store"], w[2]["online"]) == ("自由流", "B", "否")
    assert w[2]["brand"] == "acme" and w[2]["gross"] == 123.46
    assert (w[3]["flow"], w[3]["store"]) == ("定向流", "A")           # 品牌被 A 占 → 回 A
    assert (w[4]["flow"], w[4]["store"], w[4]["online"]) == ("未分配", "A、B", "是")
    assert "已在架" in w[4]["unassigned_why"]
    assert (w[5]["flow"], w[5]["brand"], w[5]["unassigned_why"]) == ("未分配", "Ghost", "未审核")
    assert w[6]["flow"] == "未分配" and w[6]["unassigned_why"].startswith("库存不足(1 <")
    assert w[6]["brand"] == "mid" and w[6]["score"] == ""      # 池里但没分:数据列有、分数列空
    assert w[7]["flow"] == "未分配" and "怪东西" in w[7]["unassigned_why"]
    assert {v["flow"] for v in w.values()} <= {"自由流", "定向流", "未分配"}
    # 占用只落自由流与定向流的品;source 记点名;定向流不重占品牌
    landed = cap["landed"]
    assert {r["source"] for r in landed} == {"alloc_plan_sheet"}
    assert {(r["kind"], r["claim_key"], r["store"]) for r in landed} == {
        (wf.claims.BRAND, "acme", "B"), (wf.claims.PRODUCT, "B0FREE0001", "B"),
        (wf.claims.PRODUCT, "B0HELD0002", "A")}
    assert "自由流 1 件 · 定向流 1 件 · 未分配 4 件" in out
    assert "已落占用 3 条" in out and "已回写 6 行" in out
    assert cap["world"]["absent_asked"] == ["B0NONE0004"]     # 只问池外的那一个


def test_sheet_mode_deals_to_the_store_with_the_bigger_gap_first(monkeypatch, tmp_path):
    """甲1:一张牌 → 缺口 75% 的 B,不是字母序在前的 A(全库模式第一圈会给 A)。"""
    cap = _wire(monkeypatch, tmp_path, rows=[_row(2, "B0FREE0001")],
                raw=[_raw("B0FREE0001", "acme")])
    wf.run({"from_sheet": "1", "cutoff": "0", "execute": True})
    assert _by_row(cap)[2]["store"] == "B"
    seen = {}
    real = wf.ae.deal
    monkeypatch.setattr(wf.ae, "deal", lambda g, s, **k: (seen.update(s), real(g, s, **k))[1])
    wf.run({"from_sheet": "1", "cutoff": "0", "execute": False})
    assert seen["B"]["fit"] > seen["A"]["fit"] and {v["tier"] for v in seen.values()} == {1}


def test_sheet_mode_dry_run_writes_the_csv_but_neither_claims_nor_the_sheet(
        monkeypatch, tmp_path):
    cap = _wire(monkeypatch, tmp_path, rows=[_row(2, "B0FREE0001")],
                raw=[_raw("B0FREE0001", "acme")])
    out = wf.run({"from_sheet": "1", "cutoff": "0", "execute": False})
    assert cap["landed"] == [] and cap["written"] == []
    assert "未回写产品分配表" in out and "将落品牌 1 + 产品 1 条" in out
    body = open(tmp_path / "alloc_点名分配.csv", encoding="utf-8-sig").read().splitlines()
    assert body[0].split(",")[:3] == ["表行号", "店铺", "ASIN"] and len(body) == 2
    assert body[1].startswith("2,B,B0FREE0001,acme,")


def test_sheet_mode_has_no_cutoff_by_default_and_only_adds_one_on_request(
        monkeypatch, tmp_path):
    """所有者定稿 2026-09-23:点名缺省不设淘汰线,`-p cutoff=` 才临时加一条。

    夹具是 4.8 分、零评论、没卖过的品:口碑 36 分、销量加分 0,在全库那条 40 的线
    下面 —— 正是所有者问的「我选的产品可能评论很少」那种。点名的品是他亲手挑的,
    缺的是**我们这边的证据**(评论少、没在我们店卖过),不是品差。
    """
    cap = _wire(monkeypatch, tmp_path, rows=[_row(2, "B0FREE0001")],
                raw=[_raw("B0FREE0001", "acme", rating="4.8", reviews="0")])
    out = wf.run({"from_sheet": "1", "execute": True})
    row = _by_row(cap)[2]
    assert row["flow"] == "自由流" and row["store"] == "B" and cap["landed"]
    assert 0 < float(row["score"]) < wf.ps.CUTOFF            # 分照算照写,只管顺序
    assert "淘汰线 不设(点名缺省" in out
    # 要筛就自己加线:加了就按线淘汰,摘要说清这条线是临时加的
    cap["landed"].clear()
    out = wf.run({"from_sheet": "1", "cutoff": "99", "execute": True})
    row = _by_row(cap)[2]
    assert row["flow"] == "未分配" and "低于淘汰线 99" in row["unassigned_why"]
    assert cap["landed"] == [] and "淘汰线 99(-p cutoff= 临时加的线)" in out
    # 全库那条线一字不动
    assert wf.ps.CUTOFF == 40.0


def test_sheet_write_failure_is_shouted_first_and_claims_stay(monkeypatch, tmp_path):
    """先落库再回写表(防重纪律):表写炸了占用已在,首行点名,重跑补写不重占。"""
    cap = _wire(monkeypatch, tmp_path, rows=[_row(2, "B0FREE0001")],
                raw=[_raw("B0FREE0001", "acme")])
    monkeypatch.setattr(wf.alloc_sheet, "write_rows",
                        lambda ups, execute=True: (_ for _ in ()).throw(RuntimeError("90227 boom")))
    out = wf.run({"from_sheet": "1", "cutoff": "0", "execute": True})
    assert out.splitlines()[0].startswith("⚠ 产品分配表回写失败(90227 boom)")
    assert len(cap["landed"]) == 2 and not out.startswith("⛔")


def test_a_claim_conflict_turns_that_row_back_to_unassigned(monkeypatch, tmp_path):
    """取数到落库之间被别家抢了:表上不能写"分给 B"而台账归 X。"""
    cap = _wire(monkeypatch, tmp_path, rows=[_row(2, "B0FREE0001")],
                raw=[_raw("B0FREE0001", "acme")],
                conflicts=[(wf.claims.PRODUCT, "B0FREE0001", "B", "X")])
    out = wf.run({"from_sheet": "1", "cutoff": "0", "execute": True})
    row = _by_row(cap)[2]
    assert row["flow"] == "未分配" and "已被 X 占" in row["unassigned_why"]
    assert "冲突 1 条" in out


def test_no_pending_rows_is_a_plain_summary_not_a_refusal(monkeypatch, tmp_path):
    cap = _wire(monkeypatch, tmp_path, rows=[_row(2, "B0FREE0001", store="A")], raw=[])
    monkeypatch.setattr(wf.db, "pg_conn", lambda *a, **k: pytest.fail("没活还连库"))
    out = wf.run({"from_sheet": "1", "execute": True})
    assert "没有待分配的 ASIN" in out and not out.startswith("⛔")
    assert cap["written"] == [] and cap["landed"] == []


def test_full_mode_is_untouched_by_the_sheet_switch(monkeypatch, tmp_path):
    """不给 from_sheet 就是原来的全库分配:不读表、fit 恒 0、source 仍 alloc_plan。"""
    from tests.test_alloc_plan import _c, _wire as _wire_full
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    monkeypatch.setattr(wf.alloc_sheet, "read_targets", lambda: pytest.fail("全库模式读了点名表"))
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i}", 90.0 - i) for i in range(5)]
    landed = []
    _wire_full(monkeypatch, pool, claimed=landed)
    seen = {}
    real = wf.ae.deal
    monkeypatch.setattr(wf.ae, "deal", lambda g, s, **k: (seen.update(s), real(g, s, **k))[1])
    out = wf.run({"execute": True})
    assert "已落占用" in out and {r["source"] for r in landed} == {"alloc_plan"}
    assert {v["fit"] for v in seen.values()} == {0.0}
