"""alloc_plan 限定池预览(-p pool_csv= / -p quota_csv=,2026-11-19 标准选品流程)。"""

import contextlib

import pytest

from services import alloc_sheet
from tests.test_alloc_plan import _Conn, _c
from workflows import alloc_plan as wf


def _wire(monkeypatch, tmp_path, pool, seen):
    monkeypatch.setattr(wf.report_csv.paths, "reports_dir", lambda: tmp_path)
    monkeypatch.setattr(wf.store_targets, "load_targets", lambda: {
        s: {"categories": ["Home"], "channel": "FBA", "max_online": 5000,
            "gmv": 400.0, "orders": 5.0} for s in ("A", "B")} | {
        "Z": {"categories": ["Home"], "channel": "FBA", "max_online": 0,
              "gmv": 400.0, "orders": 5.0}})
    monkeypatch.setattr(wf.stores_svc, "enabled_names", lambda: {"A", "B", "Z"})

    def _load(conn, win, asins=None):
        seen["asins"] = asins
        rows = [c for c in pool if asins is None or c["asin"] in asins]
        seen["scored"] = rows
        return {"pool": [(c["asin"],) for c in rows], "sales": {}, "refund": {},
                "risk": {}, "risk_err": None}
    monkeypatch.setattr(wf.product_pool, "load", _load)
    monkeypatch.setattr(wf.product_pool, "score_all", lambda data: (seen["scored"], {}))
    monkeypatch.setattr(wf.store_perf, "load", lambda conn, win: {
        s: dict(rec_days=90, active_days=90, avg_online=100, orders=90,
                gross=9000.0, refund=0.0, hist_rows=0, net=9000.0) for s in ("A", "B")})
    monkeypatch.setattr(wf.claims, "load_active", lambda conn, kind: {})
    monkeypatch.setattr(wf, "_pending_delist", lambda *a, **k: {})
    monkeypatch.setattr(wf, "_why_absent", lambda conn, asins: {
        a: (seen.get("why", "审核未过(rejected)"), None) for a in asins})
    monkeypatch.setattr(wf.claims, "claim_many",
                        lambda conn, rows: seen.setdefault("claimed", []).extend(rows))
    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda *a, **k: contextlib.nullcontext(_Conn({"A": 100, "B": 100})))


def _csv(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_pool_csv_limits_pool_drops_cutoff_and_reports_absent(monkeypatch, tmp_path):
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i}", 20.0) for i in range(6)]
    seen = {}
    _wire(monkeypatch, tmp_path, pool, seen)
    p = _csv(tmp_path, "pool.csv", "ASIN\nB0AAAA0001\nb0aaaa0002\nB0AAAA0001\nB0MISS0001\n")
    out = wf.run({"pool_csv": p, "execute": False})
    assert seen["asins"] == ["B0AAAA0001", "B0AAAA0002", "B0MISS0001"]
    # 分数 20 < 全库淘汰线 40,限定池缺省不设线 ⇒ 照样进牌堆
    assert "≥ 淘汰线 0" in out and "限定池清单" in out
    assert "限定池里不在候选池:审核未过(rejected) 1" in out
    assert "dry-run:未落任何占用" in out and "claimed" not in seen


@pytest.mark.parametrize("why", ["未审核", "不在库(产品库没有这个 ASIN)"])
def test_pool_csv_refuses_when_pool_not_scraped_or_audited(monkeypatch, tmp_path, why):
    """没采 / 没审的品不是淘汰品:照出预览 = 跳过审核把它们静默丢掉。"""
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i}", 90.0) for i in range(3)]
    seen = {"why": why}
    _wire(monkeypatch, tmp_path, pool, seen)
    p = _csv(tmp_path, "pool.csv", "ASIN\nB0AAAA0001\nB0MISS0002\nB0MISS0001\n")
    out = wf.run({"pool_csv": p, "execute": False})
    assert out.startswith("⛔") and "2 个 ASIN 还没走完采集/审核" in out
    assert f"{why} 2" in out and "B0MISS0001,B0MISS0002" in out
    assert not (tmp_path / "alloc_分配方案.csv").exists() and "claimed" not in seen


def test_quota_csv_overrides_quota_and_room(monkeypatch, tmp_path):
    pool = [_c(f"B0AAAA{i:04d}", f"brand{i}", 90.0 - i) for i in range(30)]
    seen = {}
    _wire(monkeypatch, tmp_path, pool, seen)
    q = _csv(tmp_path, "quota.csv", "店铺,配额\nA,3\nB,0\nZ,0\n")
    out = wf.run({"quota_csv": q, "execute": False})
    assert "本轮可分 3 个货位" in out and "quota_csv 给定配额" in out
    rows = (tmp_path / "alloc_分配方案.csv").read_text(encoding="utf-8-sig")
    assert ",B," not in rows


def test_quota_stores_hand_built_stores(monkeypatch):
    stores = {"A": {"quota": 9, "room": 9}, "B": {"quota": 9, "room": 9}}
    out, basis, total = wf._override_quota(stores, {"A": 4, "B": 0})
    assert out == {"A": {"quota": 4, "room": 4}} and total == 4 and set(basis) == {"A"}
    with pytest.raises(ValueError, match="不在分配范围"):
        wf._override_quota(stores, {"Z": 5})


@pytest.mark.parametrize("extra", [{"execute": True}, {"batch": "10", "execute": False},
                                   {"from_sheet": "1", "execute": False}])
def test_preview_params_refuse_execute_batch_and_sheet(monkeypatch, tmp_path, extra):
    seen = {}
    _wire(monkeypatch, tmp_path, [], seen)
    p = _csv(tmp_path, "pool.csv", "ASIN\nB0AAAA0001\n")
    with pytest.raises(ValueError):
        wf.run({"pool_csv": p, **extra})
    assert "claimed" not in seen and "asins" not in seen


def test_preview_params_rejected_alongside_from_csv(monkeypatch, tmp_path):
    p = _csv(tmp_path, "plan.csv", "店铺,ASIN\nA,B0AAAA0001\n")
    with pytest.raises(ValueError, match="不与"):
        wf.run({"from_csv": p, "pool_csv": p, "execute": False})


def test_empty_preview_value_refuses(monkeypatch, tmp_path):
    seen = {}
    _wire(monkeypatch, tmp_path, [], seen)
    with pytest.raises(ValueError, match="空值"):
        wf.run({"pool_csv": " ", "execute": False})


def test_cutoff_still_refused_on_full_pool(monkeypatch, tmp_path):
    seen = {}
    _wire(monkeypatch, tmp_path, [], seen)
    with pytest.raises(ValueError, match="cutoff"):
        wf.run({"cutoff": "30", "execute": False})


def test_read_csv_helpers_validate(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("ASIN\nnot-asin\n", encoding="utf-8")
    with pytest.raises(ValueError, match="第 2 行"):
        alloc_sheet.read_asin_list(p)
    p.write_text("店铺,配额\nA,1.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="配额无效"):
        alloc_sheet.read_store_quota(p)
    p.write_text("店铺,配额\nA,1\nA,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重复店铺"):
        alloc_sheet.read_store_quota(p)
    p.write_text("店铺 , 配额\nA,12\n,\n", encoding="utf-8")
    assert alloc_sheet.read_store_quota(p) == {"A": 12}
