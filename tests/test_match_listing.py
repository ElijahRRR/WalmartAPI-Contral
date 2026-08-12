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


def test_build_match_item_five_fields_per_verified_sample():
    # 2026-08-07 对拍定稿:五字段,price/ShippingWeight 裸 number,
    # condition 缺省补 New,productIdentifiers 模板缺失时用预检结果兜底
    spec_raw = {"itemSpecPayload": {"MPItem": [{"Item": {
        "productIdentifiers": {"productId": "06432341052907",
                               "productIdType": "GTIN"}}}]}}
    item = match_feed.build_match_item(spec_raw, "PHUMWMT202608070001",
                                       "14.759", "0.4")
    assert item == {"sku": "PHUMWMT202608070001",
                    "condition": "New",
                    "productIdentifiers": {"productId": "06432341052907",
                                           "productIdType": "GTIN"},
                    "ShippingWeight": 0.4, "price": 14.76}
    # SPEC 无模板时 productIdentifiers 兜底
    bare = match_feed.build_match_item(None, "S", 1, 1,
                                       product_id="00012345678905",
                                       product_id_type="GTIN")
    assert bare["productIdentifiers"]["productId"] == "00012345678905"


def test_sku_autogen_format_and_serial_resume():
    assert match_feed.make_sku("20260807", 1) == "PHUMWMT202608070001"
    assert match_feed.make_sku("20260807", 123) == "PHUMWMT202608070123"

    class _Conn:
        def __init__(self, val):
            self.val = val

        def cursor(self):
            return self

        def execute(self, sql, args=None):
            assert "MP_ITEM_MATCH" in sql

        def fetchone(self):
            return (self.val,)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    assert match_feed.next_serial_start(_Conn("0007"), "20260807") == 8
    assert match_feed.next_serial_start(_Conn(None), "20260807") == 1


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
    monkeypatch.setattr(ml.match_feed, "next_serial_start",
                        lambda conn, d: 1)
    # 三道闸数据(2026-08-12 接入):默认全空=全放行,gate 专测单独喂
    monkeypatch.setattr(ml.risk_gate, "load_gate",
                        lambda conn: {"banned_pts": set(), "brands": set()})
    monkeypatch.setattr(ml.blacklist, "load_banned_asins", lambda conn: {})
    monkeypatch.setattr(ml.product_events, "risky_asins", lambda conn: {})
    monkeypatch.setattr(ml.listing_sources, "risky_source_keys",
                        lambda conn, st: set())
    calls["sources"] = []
    monkeypatch.setattr(ml.listing_sources, "register",
                        lambda conn, rows: (calls["sources"].extend(rows),
                                            len(rows))[1])

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
    # B 列留空 → 自动按旧格式续号(PHUMWMT+YYYYMMDD+0001)
    assert by_row[2][0].startswith("PHUMWMT") and by_row[2][0].endswith("0001")
    assert by_row[3][4] == "码无效"
    assert by_row[4][4] == "需完整建品"
    assert by_row[5][4] == "店铺不识别"
    assert 6 not in by_row                          # 终态行不动
    ev = [e for e in calls["events"] if e["event"] == "match_submitted"]
    assert len(ev) == 1 and ev[0]["detail"]["feed_id"] == "F_M"
    assert "跟卖提交 1" in out
    # 来源登记簿:跟卖品登记出身(amz 驱动的自动流程按此路由,不误伤)
    assert calls["sources"] == [{
        "store": "T1", "sku": by_row[2][0], "source_type": "match",
        "source_key": "00012345678905", "workflow": "match_listing"}]


def test_listing_sources_register_first_wins():
    from services import listing_sources

    class _Conn:
        def __init__(self):
            self.sqls = []

        def cursor(self):
            return self

        def executemany(self, sql, rows):
            self.sqls.append((sql, list(rows)))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    conn = _Conn()
    n = listing_sources.register(conn, [
        {"store": "T1", "sku": "S1", "source_type": "match",
         "source_key": "G1", "workflow": "match_listing"}])
    assert n == 1
    sql, rows = conn.sqls[0]
    assert "ON CONFLICT (store, sku) DO NOTHING" in sql   # 首次登记优先
    assert rows[0] == ("T1", "S1", "match", "G1", "match_listing")
    assert listing_sources.register(conn, []) == 0


def test_manual_sku_takes_priority(monkeypatch):
    # B 列人工填号优先(旧系统习惯),不占自动序号
    row_manual = _row(2, "012345678905")
    row_manual["sku"] = "MY-OWN-001"
    row_auto = _row(3, "012345678912")
    spec2 = dict(_SPEC_OK, product_id="00012345678912")
    calls = _wire(monkeypatch, [row_manual, row_auto],
                  {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK,
                   "012345678912": spec2, "00012345678912": spec2})
    ml.run({"execute": True})
    by_row = {r: vals for r, vals in calls["writes"]}
    assert by_row[2][0] == "MY-OWN-001"
    assert by_row[3][0].endswith("0001")            # 自动号从 1 起,人工行不占号


def test_gate_reason_three_gates():
    """跟卖三道闸纯函数:风控 > 黑名单 > 防呆;字段缺失跳过该道闸。"""
    gate = {"banned_pts": {"BannedPT"}, "brands": {"badbrand"}}
    banned = {"B0BANNED01": ("E", "沃尔玛-知产")}
    risky = {"B0RISKY001": (2, 0, 0, None)}
    keys = {"00012345678905"}
    g = ml._gate_reason
    spec = lambda **kw: {"feed_type": "MP_ITEM_MATCH", "product_id": None,
                         "product_type": None, "brand": None, "asin": None,
                         **kw}
    assert g(spec(product_type="BannedPT"), gate, {}, {}, set()) \
        == "风控拦截:禁售类目:BannedPT"
    assert g(spec(brand="BadBrand"), gate, {}, {}, set()) \
        == "风控拦截:黑名单品牌:BadBrand"
    assert g(spec(asin="B0BANNED01"), gate, banned, {}, set()) \
        == "ASIN黑名单:沃尔玛-知产(E类)"
    assert "防呆:该ASIN有删除史" in g(spec(asin="B0RISKY001"),
                                       gate, {}, risky, set())
    # 同 GTIN 旧跟卖 offer 被删过:换新编号重跟也拦(经 listing_sources 接回)
    assert g(spec(product_id="00012345678905"), gate, {}, {}, keys) \
        == "防呆:该GTIN的旧跟卖offer有删除史"
    # 交叉不出 ASIN/品牌 → 跳过对应闸,放行
    assert g(spec(), gate, banned, risky, keys - {"00012345678905"}) is None


def test_gated_row_terminal_not_submitted(monkeypatch):
    """命中闸的行写 F 终态、不提交、不烧序号;下轮不再重复预检。"""
    rows = [_row(2, "012345678905"),       # 干净行 → 提交
            _row(3, "012345678912")]       # SPEC 交叉出黑名单 ASIN → 拦
    spec_bad = dict(_SPEC_OK, product_id="00012345678912", asin="B0BANNED01")
    calls = _wire(monkeypatch, rows,
                  {"012345678905": _SPEC_OK, "00012345678905": _SPEC_OK,
                   "012345678912": spec_bad, "00012345678912": spec_bad})
    monkeypatch.setattr(ml.blacklist, "load_banned_asins",
                        lambda conn: {"B0BANNED01": ("E", "沃尔玛-知产")})
    out = ml.run({"execute": True})
    assert calls["feeds"] == [("T1", "MP_ITEM_MATCH", 1)]
    by_row = {r: vals for r, vals in calls["writes"]}
    assert by_row[3][4].startswith("ASIN黑名单:沃尔玛-知产")   # F 列终态
    assert by_row[3][7] == ""                                  # 无 feedid
    assert by_row[2][0].endswith("0001")       # 被拦的行不占自动序号
    assert "ASIN黑名单" in out and "跟卖提交 1" in out


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
    monkeypatch.setattr(feed_track, "item_errors", lambda fid: {})

    out = match_sheet.sync_from_ledger()
    w = {rng: vals[0] for rng, vals in writes}
    assert w["B2:K2"][8] == "成功" and w["B2:K2"][9] != ""      # J 结果 K 时间
    assert w["B3:K3"][8] == "失败:ERR_M"
    assert "B4:K4" not in w                                     # F2 未落定不动
    assert "回填 2 行" in out
