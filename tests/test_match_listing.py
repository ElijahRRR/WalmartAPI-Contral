"""match_listing 回归:预检候选/Item 构造/行状态机/dry-run 零提交/反哺器。"""

import contextlib

from api import feeds, feishu, items as items_api
from registry import resources
from registry.resources import Spreadsheet
from services import feed_track, match_feed, match_sheet
from workflows import match_listing as ml

STORE = {"name": "T1", "client_id": "c", "client_secret": "s", "proxy": None}


def test_spec_candidates_by_length():
    # 12 位走 upc,13-14 位走 gtin;短码 zfill;退化码/超长判无效
    assert match_feed.spec_candidates("012345678905")[0] == ("upc", "012345678905")
    assert match_feed.spec_candidates("12345678")[0] == ("upc", "000012345678")
    assert match_feed.spec_candidates("4006381333931")[0] == \
        ("gtin", "04006381333931")
    assert match_feed.spec_candidates("00000000000000") == []   # 退化码
    assert match_feed.spec_candidates("") == []
    assert match_feed.spec_candidates("123456789012345") == []  # >14 位


def test_build_match_item_overlays_spec_template():
    spec_raw = {"itemSpecPayload": {"MPItem": [{"Item": {
        "productIdentifiers": {"productId": "00012345678905",
                               "productIdType": "GTIN"},
        "condition": "New"}}]}}
    item = match_feed.build_match_item(spec_raw, "SKU1", "19.999", "1.5")
    assert item["productIdentifiers"]["productId"] == "00012345678905"
    assert item["condition"] == "New"              # SPEC 模板字段保留
    assert item["sku"] == "SKU1" and item["price"] == 20.0
    assert item["ShippingWeight"] == 1.5
    # SPEC 无模板时仍能构造(最小载荷)
    bare = match_feed.build_match_item(None, "S", 1, 1)
    assert bare["sku"] == "S"


def _wire(monkeypatch, sheet_rows, spec_results, stores=(STORE,)):
    calls = {"feeds": [], "writes": [], "events": []}
    monkeypatch.setattr(match_sheet, "read_rows", lambda: [dict(r) for r in sheet_rows])
    monkeypatch.setattr(match_sheet, "write_rows",
                        lambda ups, execute=True: (calls["writes"].extend(ups),
                                                   len(ups))[1] if execute else 0)
    monkeypatch.setattr(ml.stores_svc, "load_stores",
                        lambda names=None: list(stores))
    from registry import db as _db
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    monkeypatch.setattr(ml.product_events, "record_many",
                        lambda conn, rows: (calls["events"].extend(rows),
                                            len(rows))[1])
    monkeypatch.setattr(items_api, "search_walmart_spec",
                        lambda store, **kw: spec_results[next(iter(kw.values()))])

    def fake_submit(store, ft, entries, *, workflow=""):
        calls["feeds"].append((store["name"], ft, len(entries)))
        return [{"feed_id": "F_M", "count": len(entries),
                 "outcome": "submitted"}]

    monkeypatch.setattr(feeds, "submit_feed", fake_submit)
    return calls


def _row(rownum, upc, store="T1", status="", feed_id=""):
    return {"rownum": rownum, "upc": upc, "sku": "", "price": "9.99",
            "weight": "1.2", "store": store, "status": status, "gtin": "",
            "list_time": "", "feed_id": feed_id, "feed_result": "",
            "check_time": ""}


_SPEC_OK = {"feed_type": "MP_ITEM_MATCH", "product_id": "00012345678905",
            "product_id_type": "GTIN", "product_type": "Cups",
            "title": "Cup", "asin": None,
            "raw": {"itemSpecPayload": {"MPItem": [{"Item": {
                "productIdentifiers": {"productId": "00012345678905"}}}]}}}
_SPEC_BUILD = {"feed_type": "MP_ITEM", "product_id": None,
               "product_id_type": None, "product_type": None,
               "title": None, "asin": None, "raw": None}


def test_dry_run_prechecks_but_submits_nothing(monkeypatch):
    calls = _wire(monkeypatch, [_row(2, "012345678905")],
                  {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK})
    out = ml.run({"execute": False})
    assert calls["feeds"] == [] and calls["writes"] == []
    assert "可跟卖 1 行" in out and "Item 载荷(对拍用)" in out


def test_execute_routes_and_terminal_states(monkeypatch):
    rows = [_row(2, "012345678905"),               # 可跟卖 → 提交
            _row(3, "111111111111"),               # 退化码 → 码无效
            _row(4, "012345678929"),               # MP_ITEM → 需完整建品
            _row(5, "012345678905", store="T9"),   # 店铺不识别
            _row(6, "012345678936", status="目录无")]   # 终态不再处理
    calls = _wire(monkeypatch, rows,
                  {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK,
                   "012345678929": _SPEC_BUILD, "00012345678929": _SPEC_BUILD})
    out = ml.run({"execute": True})
    assert calls["feeds"] == [("T1", "MP_ITEM_MATCH", 1)]
    by_row = {r: vals for r, vals in calls["writes"]}
    assert by_row[2][7] == "F_M" and by_row[2][8] == "处理中"   # I=feedId J=处理中
    assert by_row[2][0] == "00012345678905"        # B=SKU(暂定=productId)
    assert by_row[3][4] == "码无效"
    assert by_row[4][4] == "需完整建品"
    assert by_row[5][4] == "店铺不识别"
    assert 6 not in by_row                          # 终态行不动
    ev = [e for e in calls["events"] if e["event"] == "match_submitted"]
    assert len(ev) == 1 and ev[0]["detail"]["feed_id"] == "F_M"
    assert "跟卖提交 1" in out


def test_match_sheet_sync_from_ledger(monkeypatch):
    monkeypatch.setattr(resources, "MATCH_SHEET",
                        Spreadsheet(name="跟卖表", token="TOK", sheet_id="SID",
                                    columns=resources.MATCH_SHEET.columns))
    sheet_rows = [
        ["012", "SKU_A", "9.99", "1", "T1", "可跟卖", "G1",
         "2026-08-07", "F1", "处理中", ""],
        ["013", "SKU_B", "9.99", "1", "T1", "可跟卖", "G2",
         "2026-08-07", "F1", "处理中", ""],
        ["014", "SKU_C", "9.99", "1", "T1", "可跟卖", "G3",
         "2026-08-07", "F2", "处理中", ""],
    ]
    writes = []
    monkeypatch.setattr(feishu, "sheet_row_count", lambda s: len(sheet_rows) + 1)
    monkeypatch.setattr(feishu, "sheet_values", lambda s, rng: sheet_rows)
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda s, ups: (writes.extend(ups), len(ups))[1])
    ledger = {"F1": {"SKU_A": ("success", ""), "SKU_B": ("failed", "ERR_M")},
              "F2": {"SKU_C": ("submitted", "")}}
    monkeypatch.setattr(feed_track, "item_results", lambda fid: ledger[fid])

    out = match_sheet.sync_from_ledger()
    w = {rng: vals[0] for rng, vals in writes}
    assert w["B2:K2"][8] == "成功" and w["B2:K2"][9] != ""      # J 结果 K 时间
    assert w["B3:K3"][8] == "失败:ERR_M"
    assert "B4:K4" not in w                                     # F2 未落定不动
    assert "回填 2 行" in out
