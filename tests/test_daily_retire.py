"""daily_retire 回归:行状态机、动作路由、上限、dry-run 零写、轮询回写。"""

import pytest

from api import feeds, feishu
from workflows import daily_retire as dr

STORE = {"name": "T1", "client_id": "cid_r", "client_secret": "s", "proxy": None}


def _env(monkeypatch, sheet_rows, *, stores=(STORE,)):
    """搭假环境:表数据 + 店铺 + 捕获的 feed 提交与表格回写。"""
    calls = {"submit": [], "write": [], "polled": [], "done": []}
    monkeypatch.setattr(feishu, "sheet_row_count", lambda s: len(sheet_rows) + 1)
    monkeypatch.setattr(feishu, "sheet_values", lambda s, rng: sheet_rows)
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda s, ups: (calls["write"].extend(ups), len(ups))[1])
    monkeypatch.setattr(dr.stores_svc, "load_stores",
                        lambda names=None: list(stores))

    def fake_submit(store, feed_type, skus, *, workflow=""):
        calls["submit"].append((store["name"], feed_type, list(skus)))
        return [{"feed_id": f"F_{feed_type}", "count": len(skus),
                 "outcome": "submitted"}]

    monkeypatch.setattr(feeds, "submit_feed", fake_submit)
    monkeypatch.setattr(feeds, "get_feed_status",
                        lambda store, fid: (calls["polled"].append(fid),
                                            {"feedStatus": "PROCESSED"})[1])
    monkeypatch.setattr(feeds, "iter_feed_items", lambda store, fid: iter([
        {"sku": "OK1", "ingestionStatus": "SUCCESS"},
        {"sku": "BAD1", "ingestionStatus": "DATA_ERROR",
         "ingestionErrors": {"ingestionError": [{"code": "ERR_X"}]}},
    ]))
    monkeypatch.setattr(feeds, "mark_feed_done",
                        lambda fid, ok: calls["done"].append((fid, ok)))
    return calls


def test_routes_actions_and_marks_invalid(monkeypatch):
    calls = _env(monkeypatch, [
        ["T1", "S1", "停用", "过期"],
        ["T1", "S2", "下架", ""],
        ["T1", "S3", "删除", "禁售"],
        ["T1", "S4", "清仓", ""],          # 动作不识别
        ["T9", "S5", "删除", ""],          # 店铺不识别
    ])
    out = dr.run({"execute": True})
    assert ("T1", "RETIRE_ITEM", ["S1", "S2"]) in calls["submit"]   # 停用+下架同路
    assert ("T1", "DELETE_ITEM", ["S3"]) in calls["submit"]
    results = {rng: vals[0][2] for rng, vals in calls["write"]}
    assert results["E5:G5"] == "动作不识别"
    assert results["E6:G6"] == "店铺不识别"
    assert results["E2:G2"] == "处理中" and "F_RETIRE_ITEM" in str(calls["write"])
    assert "回写" in out


def test_dry_run_submits_nothing_writes_nothing(monkeypatch):
    calls = _env(monkeypatch, [["T1", "S1", "删除", ""]])
    out = dr.run({"execute": False})
    assert calls["submit"] == [] and calls["write"] == [] and calls["done"] == []
    assert "DRY-RUN" in out and "待提交 1" in out


def test_per_store_daily_limit_defers_excess(monkeypatch):
    rows = [["T1", f"S{i}", "删除", ""] for i in range(5)]
    calls = _env(monkeypatch, rows)
    out = dr.run({"execute": True, "limit": "3"})
    assert len(calls["submit"]) == 1
    assert calls["submit"][0][2] == ["S0", "S1", "S2"]     # 超限 2 行延后
    assert "延后 2 行" in out


def test_poll_writes_terminal_results_and_skips_failed_rows(monkeypatch):
    calls = _env(monkeypatch, [
        ["T1", "OK1", "删除", "", "F1", "2026-08-06", "处理中"],
        ["T1", "BAD1", "删除", "", "F1", "2026-08-06", ""],
        ["T1", "GONE", "删除", "", "F1", "2026-08-06", "处理中"],
        ["T1", "X", "删除", "", "F2", "2026-08-05", "失败:ERR_X"],  # 失败不重试不轮询
    ])
    dr.run({"execute": True})
    assert calls["polled"] == ["F1"]                        # F2 不轮询
    results = {rng: vals[0][2] for rng, vals in calls["write"]}
    assert results["E2:G2"] == "成功"
    assert results["E3:G3"] == "失败:ERR_X"
    assert results["E4:G4"] == "未查到"
    assert calls["done"] == [("F1", True)]


def test_slice_rows_get_matching_feed_ids(monkeypatch):
    calls = _env(monkeypatch, [["T1", "A", "删除", ""], ["T1", "B", "删除", ""]])

    def two_slices(store, feed_type, skus, *, workflow=""):
        return [{"feed_id": "FA", "count": 1, "outcome": "submitted"},
                {"feed_id": "FB", "count": 1, "outcome": "submitted"}]
    monkeypatch.setattr(feeds, "submit_feed", two_slices)

    dr.run({"execute": True})
    w = {rng: vals[0] for rng, vals in calls["write"]}
    assert w["E2:G2"][0] == "FA" and w["E3:G3"][0] == "FB"  # 行↔切片一一对应
