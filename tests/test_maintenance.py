"""maintenance 回归:stockzero 名单/清零意图/路由/dry-run 零提交/维护记录/反哺器。"""

import contextlib

from api import feeds, feishu, inventory as inv_api, prices
from registry import resources
from registry.resources import Spreadsheet
from services import feed_track, maint_sheet, maintenance_intents as mi
from workflows import maintenance as mw

STORE = {"name": "T1", "client_id": "c", "client_secret": "s", "proxy": None}


def _fake_db(monkeypatch, conn):
    from registry import db
    monkeypatch.setattr(db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([conn])))


class _Conn:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.sqls = []
        self.cursor_value = None

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))
        self._last = sql

    def fetchall(self):
        return self.rows

    def fetchone(self):
        if "FROM ops.cursors" in self._last:
            return (self.cursor_value,) if self.cursor_value else None
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── provider 层 ───────────────────────────────────────────────────────────────

def test_zero_intents_only_positive_known_qty():
    conn = _Conn(rows=[("T1", "S1", 5), ("T1", "S2", 12)])
    out = mi.zero_intents(conn, ["T1"])
    assert out == [
        {"store": "T1", "sku": "S1", "kind": "inventory", "old": 5, "new": 0},
        {"store": "T1", "sku": "S2", "kind": "inventory", "old": 12, "new": 0}]
    # 显式条件:未知库存不动(旧系统 None != 0 盲清是坑)
    assert "avail_qty > 0" in mi._SQL_ZERO
    assert mi.zero_intents(conn, []) == []          # 无 stockzero 店零查询


# amz 侧 × 沃尔玛侧联表的一行(顺序 = _SQL_AMZ_JOIN 的 SELECT 列)
def _row(store="T1", sku="B0A", name="旧标题", pt="Cups", upc="012345678905",
         wm_price=20.0, avail_qty=10, amz_price=10.0, stock_count=7,
         delivery_days=3, slow=None):
    return (store, sku, name, pt, upc, wm_price, avail_qty,
            amz_price, stock_count, delivery_days,
            slow if slow is not None else {"title": "Amz 标题", "brand": None})


_MULTS = {"T1": {"fbm_range1": "200%", "fbm_range2": "200%"}}


def test_price_intents_threshold_and_no_rule(monkeypatch):
    rows = [
        _row(sku="B0CHANGE", wm_price=20.0, amz_price=15.0),   # 新价 30 → 改
        _row(sku="B0SAME", wm_price=20.0, amz_price=10.0),     # 新价 20 → 不动
        _row(sku="B0TINY", wm_price=20.10, amz_price=10.0),    # 差 0.5% → 不动
        _row(sku="B0NOAMZ", amz_price=None),                   # 缺 amz 现价 → 不动
        _row(sku="B0OUT", amz_price=5000.0),                   # 出界 → 不动(非改 0)
    ]
    monkeypatch.setattr(mi, "_rows", lambda conn, sz: rows)
    out = mi.price_intents(_Conn(), _MULTS, [])
    assert [i["sku"] for i in out] == ["B0CHANGE"]
    assert out[0]["old"] == 20.0 and out[0]["new"] == 30.0


def test_inventory_intents_null_is_not_zero(monkeypatch):
    rows = [
        _row(sku="B0SYNC", avail_qty=10, stock_count=7),        # 7≠10 → 改
        _row(sku="B0SAME", avail_qty=7, stock_count=7),         # 相同 → 不动
        _row(sku="B0OOS", avail_qty=5, stock_count=0),          # 确实缺货 → 改 0
        _row(sku="B0UNKNOWN", avail_qty=5, stock_count=None),   # 没采到 → **不动**
        _row(sku="B0SLOW", avail_qty=9, stock_count=50,
             delivery_days=30),                                 # 货期超限 → 清零
    ]
    monkeypatch.setattr(mi, "_rows", lambda conn, sz: rows)
    out = {i["sku"]: i["new"] for i in mi.inventory_intents(_Conn(), [])}
    assert out == {"B0SYNC": 7, "B0OOS": 0, "B0SLOW": 0}


def test_title_intents_reuses_listing_copy_rules(monkeypatch):
    rows = [
        _row(sku="B0NEW", name="旧标题",
             slow={"title": "ACME Steel Cup", "brand": "ACME"}),   # 去品牌后不同 → 改
        _row(sku="B0SAME", name="Steel Cup",
             slow={"title": "ACME Steel Cup", "brand": "ACME"}),   # 处理后相同 → 不动
        _row(sku="B0NOPT", pt="", slow={"title": "X Cup"}),        # 缺 PT → 三缺一跳过
        _row(sku="B0NOUPC", upc="", slow={"title": "X Cup"}),      # 缺 UPC → 跳过
        _row(sku="B0PLACE", slow={"title": "[商品不存在]"}),        # 占位符 → 跳过
    ]
    monkeypatch.setattr(mi, "_rows", lambda conn, sz: rows)
    out = mi.title_intents(_Conn(), [])
    assert [i["sku"] for i in out] == ["B0NEW"]
    assert out[0]["new"] == "Steel Cup"          # 与上架同一套文案处理(去品牌)
    assert out[0]["product_type"] == "Cups" and out[0]["product_id"]


def test_amz_join_honors_routing_and_stockzero():
    # 路由铁律:只作用于 source_type='amz';stockzero 店整店排除(归 zero_intents)
    assert "source_type = 'amz'" in mi._SQL_AMZ_JOIN
    assert "NOT (w.store = ANY(%s))" in mi._SQL_AMZ_JOIN
    assert "missing_since IS NULL" in mi._SQL_AMZ_JOIN
    assert "zip_verify" in mi._SQL_AMZ_JOIN


def test_build_title_item_shape():
    item = mi.build_title_item("SKU1", "Furniture", "012345678905", "新标题")
    assert item["Orderable"]["productIdentifiers"]["productIdType"] == "UPC"
    assert item["Visible"]["Furniture"]["productName"] == "新标题"
    assert "MPProduct" not in item                  # 顶级并列,不是 MPProduct


def test_load_stockzero_survives_integer_zero(monkeypatch):
    # 旧系统 str(0 or "") 的 0-falsy 陷阱:整数 0 必须仍识别为 stockzero
    monkeypatch.setattr(feishu, "list_records", lambda t, field_names=None: [
        {"fields": {"店铺": "T1", "库存特殊要求": 0}},
        {"fields": {"店铺": "T2", "库存特殊要求": "0"}},
        {"fields": {"店铺": "T3", "库存特殊要求": ""}},
        {"fields": {"店铺": "T4", "库存特殊要求": "5"}},
    ])
    assert mw._load_stockzero() == ["T1", "T2"]


# ── workflow 层 ───────────────────────────────────────────────────────────────

def _wire(monkeypatch, intents, stores=(STORE,)):
    calls = {"put_inv": [], "put_price": [], "feeds": [], "sheet": []}
    monkeypatch.setattr(mw, "_load_stockzero", lambda: ["T1"])
    monkeypatch.setattr(mw, "collect_intents", lambda conn, sz: list(intents))
    _fake_db(monkeypatch, _Conn())
    monkeypatch.setattr(mw.stores_svc, "load_stores",
                        lambda names=None: list(stores))
    monkeypatch.setattr(inv_api, "put_inventory",
                        lambda store, sku, qty: (calls["put_inv"].append(
                            (store["name"], sku, qty)), (True, ""))[1])
    monkeypatch.setattr(prices, "put_price",
                        lambda store, sku, amt: (calls["put_price"].append(
                            (store["name"], sku, amt)), (True, ""))[1])

    def fake_submit(store, ft, entries, *, workflow=""):
        calls["feeds"].append((store["name"], ft, len(entries)))
        return [{"feed_id": f"F_{ft}", "count": len(entries),
                 "outcome": "submitted"}]

    monkeypatch.setattr(feeds, "submit_feed", fake_submit)
    monkeypatch.setattr(maint_sheet, "append_records",
                        lambda rows: (calls["sheet"].extend(rows),
                                      len(rows))[1])
    return calls


def _zero(n):
    return [{"store": "T1", "sku": f"S{i}", "kind": "inventory",
             "old": i + 1, "new": 0} for i in range(n)]


def test_dry_run_shows_route_and_submits_nothing(monkeypatch):
    calls = _wire(monkeypatch, _zero(3))
    out = mw.run({"execute": False})
    assert calls["put_inv"] == [] and calls["feeds"] == [] and calls["sheet"] == []
    assert "DRY-RUN" in out and "库存 3" in out and "路由 PUT" in out
    assert "'1→0'" in out                       # 逐 SKU 旧值→新值样本


def test_small_batch_routes_to_put_and_records_sync(monkeypatch):
    calls = _wire(monkeypatch, _zero(2))
    out = mw.run({"execute": True})
    assert calls["put_inv"] == [("T1", "S0", 0), ("T1", "S1", 0)]
    assert calls["feeds"] == []
    assert calls["sheet"][0][5] == "sync" and calls["sheet"][0][7] == "成功"
    assert "同步 PUT 2,成功 2" in out


def test_large_batch_routes_to_feed(monkeypatch):
    calls = _wire(monkeypatch, _zero(11))       # >10 → inventory feed
    mw.run({"execute": True})
    assert calls["put_inv"] == []
    assert calls["feeds"] == [("T1", "inventory", 11)]
    assert all(r[5] == "F_inventory" and r[7] == "处理中"
               for r in calls["sheet"])


def test_title_always_feed_and_store_isolation(monkeypatch):
    intents = [{"store": "T1", "sku": "A", "kind": "title", "old": "旧", "new": "新",
                "product_type": "Furniture", "product_id": "012345678905"},
               {"store": "T2", "sku": "B", "kind": "inventory", "old": 3, "new": 0}]
    store2 = {"name": "T2", "client_id": "c2", "client_secret": "s", "proxy": None}
    calls = _wire(monkeypatch, intents, stores=(STORE, store2))

    def flaky(store, ft, entries, *, workflow=""):
        if store["name"] == "T1":
            raise ConnectionError("proxy down")
        calls["feeds"].append((store["name"], ft, len(entries)))
        return [{"feed_id": "F", "count": len(entries), "outcome": "submitted"}]

    monkeypatch.setattr(feeds, "submit_feed", flaky)
    out = mw.run({"execute": True})
    assert "⚠ T1:提交异常已跳过" in out          # 标题 feed 炸了只跳过 T1
    assert calls["put_inv"] == [("T2", "B", 0)]   # T2 照常(1 条走 PUT)


# ── 维护记录反哺器 ────────────────────────────────────────────────────────────

def test_maint_sheet_sync_from_ledger(monkeypatch):
    monkeypatch.setattr(resources, "MAINT_SHEET",
                        Spreadsheet(name="维护记录", token="TOK", sheet_id="SID",
                                    columns=resources.MAINT_SHEET.columns))
    conn = _Conn()
    conn.cursor_value = {"next_row": 6, "unresolved_from": 2}
    _fake_db(monkeypatch, conn)
    sheet_rows = [
        ["T1", "S1", "库存", "5", "0", "F1", "2026-08-07", "处理中", ""],
        ["T1", "S2", "库存", "3", "0", "F1", "2026-08-07", "处理中", ""],
        ["T1", "S3", "价格", "9", "8", "sync", "2026-08-07", "成功", ""],
        ["T1", "S4", "库存", "7", "0", "F2", "2026-08-07", "处理中", ""],
    ]
    writes = []
    monkeypatch.setattr(feishu, "sheet_values", lambda s, rng: sheet_rows)
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda s, ups: (writes.extend(ups), len(ups))[1])
    ledger = {"F1": {"S1": ("success", ""), "S2": ("failed", "ERR_P")},
              "F2": {"S4": ("submitted", "")}}
    monkeypatch.setattr(feed_track, "item_results", lambda fid: ledger[fid])

    out = maint_sheet.sync_from_ledger()
    w = {rng: vals[0] for rng, vals in writes}
    assert w["H2:I2"] == ["成功", ""]
    assert w["H3:I3"] == ["失败", "ERR_P"]
    assert "H5:I5" not in w                     # F2 未落定不动
    assert "回填 2 行" in out
    # 水位推进到第一个未落定行(第 5 行的 F2):unresolved_from=5
    saved = [a for s, a in conn.sqls if "INSERT INTO ops.cursors" in s]
    assert saved and '"unresolved_from": 5' in saved[-1][1]
