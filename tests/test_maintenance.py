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

    @contextlib.contextmanager
    def _open():            # 可重复进入:一轮维护会开好几次连接
        yield conn

    monkeypatch.setattr(db, "pg_conn", _open)


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

    def executemany(self, sql, seq):
        self.sqls.append((sql, list(seq)))
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
         delivery_days=3, slow=None, fulfillment="FBM"):
    return (store, sku, name, pt, upc, wm_price, avail_qty,
            amz_price, stock_count, delivery_days,
            slow if slow is not None else {"title": "Amz 标题", "brand": None},
            fulfillment)


_MULTS = {"T1": {"fbm_range1": "200%", "fbm_range2": "200%"}}


def test_price_intents_threshold_and_no_rule(monkeypatch):
    # _MULTS 只配了 FBM 两段;FBM 区间 15-80 / 80-1000,倍率 200%
    rows = [
        _row(sku="B0CHANGE", wm_price=20.0, amz_price=20.0),   # 新价 40 → 改
        _row(sku="B0SAME", wm_price=40.0, amz_price=20.0),     # 新价 40 → 不动
        _row(sku="B0TINY", wm_price=40.10, amz_price=20.0),    # 差 0.25% → 不动
        _row(sku="B0NOAMZ", amz_price=None),                   # 缺 amz 现价 → 不动
        # 出界:所有者定稿 2026-08-09 改为按 300% 定价(此前是不动)
        _row(sku="B0OUTBAND", wm_price=20.0, amz_price=5000.0),
        # 在区间内但该渠道倍率没配 → 仍不动(配置缺失不拿默认值蒙混)
        _row(sku="B0NORULE", wm_price=20.0, amz_price=10.0, fulfillment="FBA"),
    ]
    monkeypatch.setattr(mi, "_rows", lambda conn, sz: rows)
    out = {i["sku"]: i["new"] for i in mi.price_intents(_Conn(), _MULTS, [])}
    assert out == {"B0CHANGE": 40.0, "B0OUTBAND": 15000.0}


def test_price_intents_skip_when_fulfillment_unknown(monkeypatch):
    """FBA/FBM 决定用哪套区间;未知**不猜**(所有者 2026-08-09:必须获取)。"""
    rows = [
        _row(sku="B0FBM", wm_price=20.0, amz_price=15.0, fulfillment="FBM"),
        _row(sku="B0FBA", wm_price=20.0, amz_price=15.0, fulfillment="FBA"),
        _row(sku="B0UNK", wm_price=20.0, amz_price=15.0, fulfillment=None),
        _row(sku="B0JUNK", wm_price=20.0, amz_price=15.0, fulfillment="???"),
    ]
    monkeypatch.setattr(mi, "_rows", lambda conn, sz: rows)
    mults = {"T1": {"fbm_range1": "200%", "fba_range1": "300%"}}
    out = {i["sku"]: i["new"] for i in mi.price_intents(_Conn(), mults, [])}
    # 同一个 15 美金:FBM 落区间1(×2=30),FBA 落区间1(×3=45)——区间不同套
    assert out == {"B0FBM": 30.0, "B0FBA": 45.0}
    # 配送方式来自 latest_snapshot 的 raw.is_fba
    assert "raw ->> 'is_fba'" in mi._SQL_AMZ_JOIN


def test_inventory_intents_unknown_stock_goes_zero(monkeypatch):
    """所有者定稿 2026-08-09:没采到也写 0(采不到就不卖);货期闸收紧到 8 天。"""
    rows = [
        _row(sku="B0SYNC", avail_qty=10, stock_count=7),        # 7≠10 → 改
        _row(sku="B0SAME", avail_qty=7, stock_count=7),         # 相同 → 不动
        _row(sku="B0OOS", avail_qty=5, stock_count=0),          # 确实缺货 → 改 0
        _row(sku="B0UNKNOWN", avail_qty=5, stock_count=None),   # 没采到 → **也 0**
        _row(sku="B0ZEROED", avail_qty=0, stock_count=None),    # 已经是 0 → 不动
        _row(sku="B0LEAD9", avail_qty=9, stock_count=50,
             delivery_days=9),                                  # 9>8 → 清零
        _row(sku="B0LEAD8", avail_qty=9, stock_count=50,
             delivery_days=8),                                  # 8 不超 → 同步 50
    ]
    monkeypatch.setattr(mi, "_rows", lambda conn, sz: rows)
    out = {i["sku"]: i["new"] for i in mi.inventory_intents(_Conn(), [])}
    assert out == {"B0SYNC": 7, "B0OOS": 0, "B0UNKNOWN": 0, "B0LEAD9": 0,
                   "B0LEAD8": 50}


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


def test_variant_offset_intents_gates_and_store_cap(monkeypatch):
    """采集永久偏移 → 删除意图。门槛 1(所有者:偏移了就不会恢复)。"""
    q = mi._SQL_VARIANT_OFFSET
    assert "error_type = 'variant_offset'" in q
    assert "count(DISTINCT batch_name)" in q      # 门槛按批次数,不是失败行数
    assert "vo.batches >= %(min_batches)s" in q
    # 后来又采到了就不该删;历史行 outcome 为 NULL 时按 ok(否则老 SKU 被误判)
    assert "COALESCE(sn.outcome, 'ok') = 'ok'" in q
    assert "sn.scraped_at > vo.last_seen" in q
    # 破坏动作守路由铁律:只删 amz 出身的行
    assert "ls.source_type = 'amz'" in q
    assert "published_status = 'PUBLISHED'" in q and "missing_since IS NULL" in q
    assert mi.MIN_OFFSET_BATCHES == 1

    monkeypatch.setattr(mi, "_rows", lambda conn, sz: [])
    conn = _Conn(rows=[("T1", "B0A", 1, None, None),
                       ("T1", "B0B", 2, None, None)])
    out = mi.delete_intents(conn)
    assert [(i["store"], i["sku"], i["kind"], i["old"], i["new"], i["reason"])
            for i in out] == [
        ("T1", "B0A", "delete", "在线", "删除", "variant_offset"),
        ("T1", "B0B", "delete", "在线", "删除", "variant_offset")]

    # 单店上限取限额表「下架限制」;不在表内退 DELETE_PER_STORE(所有者问来源)
    rows3 = [("T1", "B0A", 1, None, None), ("T1", "B0B", 1, None, None),
             ("T2", "B0C", 1, None, None)]
    assert len(mi.delete_intents(_Conn(rows=rows3), caps={"T1": 1})) == 2
    monkeypatch.setattr(mi, "DELETE_PER_STORE", 1)
    assert len(mi.delete_intents(_Conn(rows=rows3))) == 2    # 每店各留 1


def test_delete_intents_also_take_title_placeholder(monkeypatch):
    """占位符[商品不存在]:旧系统只是跳过标题,所有者 2026-08-09 改为删除。"""
    monkeypatch.setattr(mi, "_rows", lambda conn, sz: [
        _row(sku="B0GONE", slow={"title": "[商品不存在]"}),
        _row(sku="B0OK", slow={"title": "正常标题"}),
        _row(sku="B0DUP", slow={"title": "[商品不存在]"}),
    ])
    # B0DUP 同时是偏移件:两个原因命中只删一次
    out = mi.delete_intents(_Conn(rows=[("T1", "B0DUP", 1, None, None)]))
    got = {i["sku"]: i["reason"] for i in out}
    assert got == {"B0DUP": "variant_offset", "B0GONE": "商品不存在"}
    assert [i["label"] for i in out if i["sku"] == "B0GONE"] == ["删除(商品不存在)"]


def test_long_oos_delete_sql_guards():
    """连续无货 N 天 → 删除。三道判据缺一个都会误删(所有者定稿 2026-08-09)。"""
    q = mi._SQL_LONG_OOS
    # 1. 窗口内一条"有货"观测都没有
    assert "stock_count > 0" in q and "stock_state = 'in_stock'" in q
    # 2. 至少一条明确缺货观测——防"15 天全是 unknown(采不全)"被当成缺货
    assert "stock_state = 'out_of_stock'" in q
    # 3. 窗口两端都有观测——防"两头各采一次、中间断 13 天"被当连续
    assert "interval '36 hours'" in q and "max(scraped_at) >= now()" in q
    # 降级采集的 fast 段基本是空的,拿它判缺货是冤案
    assert "COALESCE(outcome, 'ok') = 'ok'" in q
    # 破坏动作守路由铁律 + 在架 + 店铺 ACTIVE
    assert "ls.source_type = 'amz'" in q
    assert "published_status = 'PUBLISHED'" in q
    assert "upper(s.store_status) = 'ACTIVE'" in q
    assert mi.LONG_OOS_DAYS == 15


def test_long_oos_intents_carry_reason(monkeypatch):
    monkeypatch.setattr(mi, "_rows", lambda conn, sz: [])

    class _TwoQueries(_Conn):
        """第一条 SQL(偏移)无结果,第二条(连续无货)返回一行。"""

        def fetchall(self):
            return [] if "variant_offset" in self._last else [
                ("T1", "B0DEAD", 15, None, None)]

    out = mi.delete_intents(_TwoQueries(), oos_days=15)
    assert [(i["sku"], i["reason"], i["label"]) for i in out] == [
        ("B0DEAD", "连续无货15天", "删除(连续无货15天)")]


def test_drop_recent_suppresses_same_intent_within_window(monkeypatch):
    """208 条 stale update 的解药:同 (店铺,SKU,类型,新值) 20 小时内不重发。"""
    intents = [
        {"store": "T1", "sku": "B0A", "kind": "price", "new": 30.0},
        {"store": "T1", "sku": "B0B", "kind": "price", "new": 31.0},
    ]

    class _Recent(_Conn):
        def fetchall(self):
            return [("T1|B0A|price|30.0",)]

    kept, n = mi.drop_recent(_Recent(), intents)
    assert n == 1 and [i["sku"] for i in kept] == ["B0B"]
    # 值真变了照样能提交(键含新值,压的只是"同一件事再做一遍")
    kept2, n2 = mi.drop_recent(_Recent(), [
        {"store": "T1", "sku": "B0A", "kind": "price", "new": 33.0}])
    assert n2 == 0 and len(kept2) == 1
    assert mi.drop_recent(_Conn(), []) == ([], 0)


def test_record_submitted_keys_include_new_value():
    conn = _Conn()
    n = mi.record_submitted(conn, [
        {"store": "T1", "sku": "B0A", "kind": "inventory", "new": 0}])
    assert n == 1
    sql, params = conn.sqls[-1]
    assert "ops.dedupe" in sql and params[0][1] == "T1|B0A|inventory|0"


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
    monkeypatch.setattr(mw, "collect_intents",
                        lambda conn, sz, oos=0: list(intents))
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


def test_delete_kind_routes_to_delete_item_and_lands_events(monkeypatch):
    """删除走 DELETE_ITEM;只有 submitted 落病历(dedup 记了是幽灵事件)。"""
    intents = [{"store": "T1", "sku": s, "kind": "delete", "old": "在线",
                "new": "删除", "reason": "variant_offset",
                "label": "删除(variant_offset)", "batches": 1,
                "first_seen": None, "last_seen": None}
               for s in ("B0A", "B0B", "B0C")]
    calls = _wire(monkeypatch, intents)
    events = []
    monkeypatch.setattr(mw, "_record_deletes",
                        lambda store, rows, fid: events.append(
                            (store, [r["sku"] for r in rows], fid)))
    monkeypatch.setattr(feeds, "submit_feed",
                        lambda store, ft, entries, workflow="": [
                            {"feed_id": "F1", "count": 2, "outcome": "submitted"},
                            {"feed_id": "OLD", "count": 1, "outcome": "dedup"}])
    out = mw.run({"execute": True})
    assert events == [("T1", ["B0A", "B0B"], "F1")]
    assert "删除 feed 提交 2" in out
    # 维护记录三行都出(dedup 挂旧 feedid 照样能被反哺器落定)
    assert [r[1] for r in calls["sheet"]] == ["B0A", "B0B", "B0C"]
    assert all(r[2] == "删除(variant_offset)" and r[7] == "处理中"
               for r in calls["sheet"])


def test_dry_run_lists_delete_names_separately(monkeypatch):
    """删除不可逆:名单必须看得见,且与其余三类分开说。"""
    intents = ([{"store": "T1", "sku": "B0A", "kind": "delete", "old": "在线",
                 "new": "删除", "batches": 1}] + _zero(2))
    _wire(monkeypatch, intents)
    out = mw.run({"execute": False})
    assert "删除 1" in out and "永久删除 1 行" in out and "B0A" in out


def test_doomed_skus_dropped_from_other_kinds(monkeypatch):
    """将被删除的行不再改价/改库存:它们的 amz 数据本来就是陈旧的。"""
    conn = _Conn()
    monkeypatch.setattr(mi, "delete_intents", lambda c, sz, caps, oos_days=0: [
        {"store": "T1", "sku": "B0A", "kind": "delete", "old": "在线",
         "new": "删除", "reason": "variant_offset"}])
    monkeypatch.setattr(mw, "_load_delete_caps", lambda: {})
    monkeypatch.setattr(mi, "title_intents", lambda c, sz: [])
    monkeypatch.setattr(mi, "price_intents", lambda c, m, sz: [
        {"store": "T1", "sku": "B0A", "kind": "price", "old": 9.9, "new": 11.0},
        {"store": "T1", "sku": "B0B", "kind": "price", "old": 9.9, "new": 11.0}])
    monkeypatch.setattr(mi, "inventory_intents", lambda c, sz: [
        {"store": "T1", "sku": "B0A", "kind": "inventory", "old": 5, "new": 0}])
    monkeypatch.setattr(mi, "zero_intents", lambda c, sz: [])
    monkeypatch.setattr(mw, "_load_multipliers", lambda: {})
    out = mw.collect_intents(conn, [])
    assert [(i["sku"], i["kind"]) for i in out] == [("B0A", "delete"),
                                                    ("B0B", "price")]


def test_resync_sheet_backfills_only_missing_rows(monkeypatch):
    """提交成功但写表炸了之后的恢复路径:按 (feedid, sku) 只补缺的行。"""
    from registry.resources import Spreadsheet
    monkeypatch.setattr(resources, "MAINT_SHEET",
                        Spreadsheet(name="维护记录", token="TOK",
                                    sheet_id="SID",
                                    columns=resources.MAINT_SHEET.columns))
    import datetime as _dt
    when = _dt.datetime(2026, 8, 9, 12, 0)
    conn = _Conn(rows=[("T1", "B0A", "price", "F1", "success", None, None, when),
                       ("T1", "B0B", "inventory", "F2", "failed", "E1", "没库存",
                        when)])
    conn.cursor_value = {"next_row": 3, "unresolved_from": 3}
    _fake_db(monkeypatch, conn)
    # 表里已有 (F1,B0A) 那一行,只该补 B0B
    monkeypatch.setattr(maint_sheet.feishu, "sheet_values",
                        lambda sheet, rng: [["T1", "B0A", "价格", "", "",
                                             "F1", "2026-08-09", "成功", ""]])
    appended = []
    monkeypatch.setattr(maint_sheet, "append_records",
                        lambda rows: (appended.extend(rows), len(rows))[1])
    out = maint_sheet.resync_from_ledger()
    assert "补写 1 行" in out
    assert appended[0][1] == "B0B" and appended[0][2] == "库存"
    assert appended[0][5] == "F2" and appended[0][7] == "失败"
    # 旧值/新值补不回来(PG 只记 SKU 级状态,不存当时的新旧值)
    assert appended[0][3] == "" and appended[0][4] == ""


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
