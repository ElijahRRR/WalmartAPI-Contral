"""brand_scrape:防循环三道闸 + 推送形态(不设邮编不截图)+ 先摄取再入账。"""

import contextlib

import pytest

from workflows import brand_scrape as wf


@pytest.fixture
def wired(monkeypatch):
    calls = {"submitted": [], "marked": [], "collected": [], "pumped": 0,
             "status": {"status": "completed",
                        "stats": {"total": 2, "done": 2, "failed": 0}}}
    state = {"items": [], "attempted": set()}

    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(wf.blacklist, "missing_brand_items",
                        lambda conn: list(state["items"]))
    monkeypatch.setattr(wf.blacklist, "scrape_attempted",
                        lambda conn, asins: state["attempted"] & set(asins))
    monkeypatch.setattr(wf.blacklist, "mark_scrape_attempted",
                        lambda conn, asins, batch:
                        calls["marked"].append((sorted(asins), batch)))
    monkeypatch.setattr(wf.blacklist, "collect_brands",
                        lambda conn, items:
                        calls["collected"].append([it["asin"] for it in items])
                        or {"brand_new": 1, "brand_known": 0,
                            "no_brand": len(items) - 1 if items else 0,
                            "skipped": 0})

    def submit(name, asins, **kw):
        calls["submitted"].append((name, list(asins), kw))
        return {"batch_id": 7, "inserted": len(asins)}
    monkeypatch.setattr(wf.scraper, "submit_batch", submit)
    monkeypatch.setattr(wf.scraper, "batch_status", lambda n: calls["status"])
    monkeypatch.setattr(wf, "_pump", lambda: "摄取:0 条")
    monkeypatch.setattr(wf.time, "sleep", lambda s: None)
    return calls, state


def _it(asin, sku=None):
    return {"asin": asin, "sku": sku or asin, "store": "店A",
            "category": "C", "reasons": "brand issue"}


def test_preview_pushes_nothing(wired):
    calls, state = wired
    state["items"] = [_it("B0AAAAAAA1"), _it("102460018738")]
    out = wf.run({})
    assert "候选 2 个" in out and "标准 ASIN 1 个" in out and "非标准过滤 1 个" in out
    assert calls["submitted"] == [] and calls["marked"] == []


def test_apply_filters_nonstandard_and_attempted(wired):
    """闸①非标准不推(采不到=无限循环);闸②推过的不再推;
    推送形态:不带 zip_code、不带 needs_screenshot(最快形态)。"""
    calls, state = wired
    state["items"] = [_it("B0AAAAAAA1"), _it("B0BBBBBBB2"),
                      _it("102460018738"), _it("JTZW-D01027HVK3W-38")]
    state["attempted"] = {"B0BBBBBBB2"}
    out = wf.run({"apply": "1"})
    name, asins, kw = calls["submitted"][0]
    assert asins == ["B0AAAAAAA1"]          # 非标准 ×2 + 已尝试 ×1 全过滤
    assert kw == {}                          # 不设邮编不截图
    assert calls["marked"][0] == (["B0AAAAAAA1"], name)
    assert "入账" in out


def test_apply_collects_previous_round_first(wired):
    """上一轮推过的 ASIN:本轮先摄取再入账(到货的品牌落袋),不重推。"""
    calls, state = wired
    state["items"] = [_it("B0BBBBBBB2")]
    state["attempted"] = {"B0BBBBBBB2"}
    out = wf.run({"apply": "1"})
    assert calls["submitted"] == []                       # 没有新推
    assert calls["collected"] == [["B0BBBBBBB2"]]          # 但入账跑了
    assert "无可推候选" in out


def test_batch_exists_is_success_not_failure(wired, monkeypatch):
    """撞名 409 = 上次其实推成功了:沿用既有批次继续等落定,照样记台账。"""
    calls, state = wired
    state["items"] = [_it("B0AAAAAAA1")]

    def boom(name, asins, **kw):
        raise wf.scraper.BatchExistsError(9, "brandfix-old")
    monkeypatch.setattr(wf.scraper, "submit_batch", boom)
    out = wf.run({"apply": "1", "wait": "0"})
    assert "批次已存在" in out
    assert calls["marked"][0] == (["B0AAAAAAA1"], "brandfix-old")
