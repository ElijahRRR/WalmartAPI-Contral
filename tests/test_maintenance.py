"""maintenance 回归:stockzero 名单/清零意图/路由/dry-run 零提交/维护记录/反哺器。"""

import contextlib
from datetime import date as _date

from api import feeds, feishu, inventory as inv_api, prices
from registry import resources
from registry.resources import Spreadsheet
from services import feed_track, maint_sheet, \
    maintenance_intents as mi, store_limits
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
    assert [(i["sku"], i["kind"], i["old"], i["new"], i["code"]) for i in out] == [
        ("S1", "inventory", 5, 0, "stockzero"),
        ("S2", "inventory", 12, 0, "stockzero")]
    # 显式条件:未知库存不动(旧系统 None != 0 盲清是坑)
    assert "avail_qty > 0" in mi._SQL_ZERO
    assert mi.zero_intents(conn, []) == []          # 无 stockzero 店零查询


# amz 侧 × 沃尔玛侧联表的一行(顺序 = _SQL_AMZ_JOIN 的 SELECT 列)
# ⚠ 默认的 name 与 slow.title 必须是**同一个商品**(处理后相似度高):
# 2026-08-16 起 price/inventory 两个 provider 都会判"标题相似度 < 70% → 该删",
# 该删的行不再产改价/清零意图。默认值若是随手写的两个不相干字符串,
# 全部用例会集体空转而看起来只是"没有意图"。
def _row(store="T1", sku="B0A", name="Steel Cup", pt="Cups", upc="012345678905",
         wm_price=20.0, avail_qty=10, amz_price=10.0, stock_count=7,
         delivery_days=3, slow=None, fulfillment="FBM", shipping=0.0,
         outcome="ok", stock_status="In Stock", stock_state="in_stock"):
    """一行在线商品夹具(**dict,与 _rows 的真实产出同形**)。

    ⚠ 2026-08-16 从元组改成 dict:SQL 加了 outcome/stock_status/stock_state 三列,
    元组夹具会让四个 provider 的位置解包全部错位 —— 而元组长度对得上时**不报错**,
    只是字段错位。按名字取之后,加列只改这里的默认值。
    """
    return {"store": store, "sku": sku, "product_name": name,
            "product_type": pt, "upc": upc, "wm_price": wm_price,
            "avail_qty": avail_qty, "amz_price": amz_price,
            "stock_count": stock_count, "delivery_days": delivery_days,
            "slow": slow if slow is not None
                    else {"title": "ACME Steel Cup", "brand": "ACME"},
            "fulfillment": fulfillment, "shipping": shipping,
            "outcome": outcome, "stock_status": stock_status,
            "stock_state": stock_state}


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
    """所有者定稿 2026-08-09:没采到也写 0(采不到就不卖);货期闸 2026-08-15 再收紧到 7 天。"""
    rows = [
        _row(sku="B0SYNC", avail_qty=10, stock_count=7),        # 7≠10 → 改
        _row(sku="B0SAME", avail_qty=7, stock_count=7),         # 相同 → 不动
        _row(sku="B0OOS", avail_qty=5, stock_count=0),          # 确实缺货 → 改 0
        _row(sku="B0UNKNOWN", avail_qty=5, stock_count=None),   # 没采到 → **也 0**
        _row(sku="B0ZEROED", avail_qty=0, stock_count=None),    # 已经是 0 → 不动
        _row(sku="B0LEAD9", avail_qty=9, stock_count=50,
             delivery_days=9),                                  # 9>7 → 清零
        # ⚠ 8 天这一档在 2026-08-15 阈值从 8 收到 7 之后**由"同步"翻成"清零"**
        _row(sku="B0LEAD8", avail_qty=9, stock_count=50,
             delivery_days=8),                                  # 8>7 → 清零
        _row(sku="B0LEAD7", avail_qty=9, stock_count=50,
             delivery_days=7),                                  # 7 不超 → 同步 50
    ]
    monkeypatch.setattr(mi, "_rows", lambda conn, sz: rows)
    out = {i["sku"]: i["new"] for i in mi.inventory_intents(_Conn(), [])}
    assert out == {"B0SYNC": 7, "B0OOS": 0, "B0UNKNOWN": 0, "B0LEAD9": 0,
                   "B0LEAD8": 0, "B0LEAD7": 50}


def test_title_intents_reuses_listing_copy_rules(monkeypatch):
    rows = [
        # 处理后 "Steel Cup" vs 现值 "Steel Cup 500ml":相似度 82% ≥ 70% → 改标题
        _row(sku="B0NEW", name="Steel Cup 500ml",
             slow={"title": "ACME Steel Cup", "brand": "ACME"}),
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
    # code = 机器码(分组用),reason = 人读文案 —— 2026-08-16 起分开两列
    assert [(i["store"], i["sku"], i["kind"], i["old"], i["new"], i["code"])
            for i in out] == [
        ("T1", "B0A", "delete", "在线", "删除", "variant_offset"),
        ("T1", "B0B", "delete", "在线", "删除", "variant_offset")]
    assert all(i["reason"] and i["reason"] != i["code"] for i in out)

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
        _row(sku="B0OK", name="正常标题", slow={"title": "正常标题"}),
        _row(sku="B0DUP", slow={"title": "[商品不存在]"}),
    ])
    # B0DUP 同时是偏移件:两个原因命中只删一次
    out = mi.delete_intents(_Conn(rows=[("T1", "B0DUP", 1, None, None)]))
    got = {i["sku"]: i["code"] for i in out}
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
    assert [(i["sku"], i["code"], i["label"]) for i in out] == [
        ("B0DEAD", "连续无货15天", "删除(连续无货15天)")]
    # 「原因」列要读得出"低到什么程度",机器码读不出来
    assert out[0]["reason"] == "15 天窗口内 15 次观测无一有货,货源已断"


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


def test_stockzero_survives_integer_zero(monkeypatch):
    """限额表读取搬到 services/store_limits 之后,0-falsy 陷阱的用例跟着搬。"""
    monkeypatch.setattr(feishu, "list_records", lambda t, field_names=None: [
        {"fields": {"店铺": "T1", "库存特殊要求": 0}},
        {"fields": {"店铺": "T2", "库存特殊要求": "0"}},
        {"fields": {"店铺": "T3", "库存特殊要求": ""}},
        {"fields": {"店铺": "T4", "库存特殊要求": "5"}},
    ])
    assert store_limits.stockzero_stores() == ["T1", "T2"]


def test_collect_all_lives_in_services_not_in_a_workflow():
    """扫描件与执行件不许互相 import(铁律 1),取意图这段必须住在 services。

    dry-run 口径与真跑口径必须是同一份代码 —— 各写一份迟早飘。
    """
    import inspect

    from workflows import maintenance_scan as ms
    assert "def collect_all(" in inspect.getsource(mi)
    src = inspect.getsource(ms) + inspect.getsource(mw)
    assert "import workflows" not in src and "from workflows" not in src


def test_doomed_skus_dropped_from_other_kinds(monkeypatch):
    """将被删除的行不再改价/改库存:它们的 amz 数据本来就是陈旧的。"""
    conn = _Conn()
    monkeypatch.setattr(mi, "delete_intents", lambda c, sz, caps, oos_days=0: [
        {"store": "T1", "sku": "B0A", "kind": "delete", "old": "在线",
         "new": "删除", "code": "variant_offset"}])
    monkeypatch.setattr(mi.store_limits, "retire_caps", lambda: {})
    monkeypatch.setattr(mi.store_limits, "price_multipliers", lambda: {})
    monkeypatch.setattr(mi, "title_intents", lambda c, sz: [])
    monkeypatch.setattr(mi, "price_intents", lambda c, m, sz: [
        {"store": "T1", "sku": "B0A", "kind": "price", "old": 9.9, "new": 11.0},
        {"store": "T1", "sku": "B0B", "kind": "price", "old": 9.9, "new": 11.0}])
    monkeypatch.setattr(mi, "inventory_intents", lambda c, sz: [
        {"store": "T1", "sku": "B0A", "kind": "inventory", "old": 5, "new": 0}])
    monkeypatch.setattr(mi, "zero_intents", lambda c, sz: [])
    monkeypatch.setattr(mi, "match_inventory_intents", lambda c, sz: [])
    monkeypatch.setattr(mi, "drop_recent", lambda c, i: (i, 0))
    out = mi.collect_all(conn, [])
    assert [(i["sku"], i["kind"]) for i in out] == [("B0A", "delete"),
                                                    ("B0B", "price")]


def test_intent_disposition_roundtrip_keeps_the_title_payload():
    """⚠ 标题载荷(productType/UPC)必须走完 to→from 一圈还在。

    丢了它 build_title_item 会组出 None,整批 MP_MAINTENANCE 被沃尔玛退回,
    而两侧都不报错 —— 所以互转写在同一个模块里,并由本用例钉住。
    """
    it = {"store": "T1", "sku": "B0A", "kind": "title", "old": "旧", "new": "新",
          "product_type": "Furniture", "product_id": "012345678905",
          "code": "title_sync", "reason": "相似度 80%,同步亚马逊标题"}
    row = mi.to_disposition(it)
    assert row["source"] == "maint" and row["action"] == "title"
    assert row["category"] == "title_sync"       # 建议行 category = 原因码
    back = mi.from_disposition({"id": 7, **row})
    assert back["disposition_id"] == 7
    for k in ("store", "sku", "kind", "old", "new", "product_type",
              "product_id", "code", "reason"):
        assert back[k] == it[k], k


def test_delete_detail_datetimes_survive_json():
    """删除建议带 first_seen/last_seen(datetime)。不给 json default 会整轮抛。"""
    import json
    from datetime import datetime as _dt

    row = mi.to_disposition({
        "store": "T1", "sku": "B0A", "kind": "delete", "old": "在线",
        "new": "删除", "code": "variant_offset", "reason": "采集永久偏移",
        "batches": 2, "first_seen": _dt(2026, 8, 1), "last_seen": _dt(2026, 8, 9)})
    assert json.dumps(row["detail"], default=str)     # 不抛即可
    import inspect

    from services import dispositions as ds
    assert "default=str" in inspect.getsource(ds.suggest_many)


# ── 扫描件(maintenance_scan)────────────────────────────────────────────────

def _scan_wire(monkeypatch, intents, sz=("T1",)):
    from workflows import maintenance_scan as ms
    calls = {"suggest": [], "withdraw": []}
    monkeypatch.setattr(ms.store_limits, "stockzero_stores", lambda: list(sz))
    monkeypatch.setattr(ms.mi, "collect_all",
                        lambda conn, s, oos=0: list(intents))
    _fake_db(monkeypatch, _Conn())
    monkeypatch.setattr(ms.dispositions, "suggest_many",
                        lambda conn, rows: (calls["suggest"].extend(rows),
                                            len(rows))[1])
    monkeypatch.setattr(ms.dispositions, "withdraw_stale",
                        lambda conn, src, keep, why, store=None: (
                            calls["withdraw"].append((src, keep, store)), 0)[1])
    monkeypatch.setattr(ms.dispositions, "count_open",
                        lambda conn, sources=None: len(calls["suggest"]))
    return ms, calls


def test_scan_lists_delete_names_separately(monkeypatch):
    """删除不可逆:名单必须看得见,且与其余三类分开说(拆分后归扫描件)。"""
    intents = ([{"store": "T1", "sku": "B0A", "kind": "delete", "old": "在线",
                 "new": "删除", "code": "variant_offset"}]
               + [{"store": "T1", "sku": f"S{i}", "kind": "inventory",
                   "old": i + 1, "new": 0, "code": "out_of_stock"}
                  for i in range(2)])
    ms, calls = _scan_wire(monkeypatch, intents)
    out = ms.run({})
    assert "删除 1" in out and "建议永久删除 1 行" in out and "B0A" in out
    # 清零四条判据在表里长得一样,摘要必须按原因码摊开
    assert "清零合计 2 条,原因:out_of_stock×2" in out
    assert [r["action"] for r in calls["suggest"]] == ["delete", "inventory",
                                                       "inventory"]


def test_scan_writes_no_feed_and_is_not_dangerous(monkeypatch):
    ms, calls = _scan_wire(monkeypatch, [])
    assert ms.DANGEROUS is False
    submitted = []
    monkeypatch.setattr(feeds, "submit_feed",
                        lambda *a, **k: submitted.append(a) or [])
    ms.run({})
    assert submitted == []


def test_scan_withdraw_is_scoped_to_the_store_it_scanned(monkeypatch):
    """⚠ 批次 E 的坑:单店扫描不限范围会清空其余全部店铺的待执行建议。"""
    intents = [{"store": "T1", "sku": "A", "kind": "inventory", "old": 1,
                "new": 0, "code": "out_of_stock"},
               {"store": "T2", "sku": "B", "kind": "inventory", "old": 1,
                "new": 0, "code": "out_of_stock"}]
    ms, calls = _scan_wire(monkeypatch, intents)
    ms.run({"store": "T1"})
    src, keep, store = calls["withdraw"][0]
    assert src == "maint" and store == "T1"
    assert keep == [("T1", "A", "inventory")]       # 只保留本店本轮的
    assert [r["store"] for r in calls["suggest"]] == ["T1"]


def test_scan_preview_writes_nothing(monkeypatch):
    ms, calls = _scan_wire(monkeypatch, [
        {"store": "T1", "sku": "A", "kind": "price", "old": 9.9, "new": 11.0}])
    out = ms.run({"preview": "1"})
    assert calls["suggest"] == [] and calls["withdraw"] == []
    assert "preview" in out and "将写 1 条" in out


# ── 执行件(maintenance)────────────────────────────────────────────────────

def _disp(it, i):
    """意图 → claim() 会返回的建议行形态(带 id)。"""
    return {"id": 100 + i, **mi.to_disposition(it)}


def _wire(monkeypatch, intents, stores=(STORE,)):
    calls = {"put_inv": [], "put_price": [], "feeds": [], "sheet": [],
             "marked": [], "settled": 0}
    _fake_db(monkeypatch, _Conn())
    monkeypatch.setattr(mw.dispositions, "claim",
                        lambda conn, sources=None: [_disp(it, i)
                                                    for i, it in enumerate(intents)])
    monkeypatch.setattr(mw.dispositions, "settle",
                        lambda conn: (calls.__setitem__("settled",
                                                        calls["settled"] + 1),
                                      {"confirmed": 0, "ineffective": 0})[1])
    monkeypatch.setattr(mw.dispositions, "settle_maintenance",
                        lambda conn: {"confirmed": 0, "ineffective": 0})
    monkeypatch.setattr(mw.dispositions, "expire_executing", lambda conn: 0)
    monkeypatch.setattr(mw.dispositions, "mark_executing",
                        lambda conn, ids, fid: calls["marked"].append(
                            (list(ids), fid)))
    monkeypatch.setattr(mi, "record_submitted", lambda conn, items: len(items))
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
    monkeypatch.setattr(maint_sheet, "prune", lambda *a: "裁剪:0")
    return calls


def _zero(n):
    return [{"store": "T1", "sku": f"S{i}", "kind": "inventory",
             "old": i + 1, "new": 0, "code": "out_of_stock",
             "reason": "亚马逊缺货"} for i in range(n)]


def test_executor_makes_no_decisions():
    """执行件不许再自己算意图 —— 决策全在 maintenance_scan。"""
    import inspect

    # 只看代码,不看头注 —— 头注要指路"判据在 classify()",那不是决策代码
    src = inspect.getsource(mw).split('"""', 2)[-1]
    for gone in ("collect_intents", "_load_stockzero", "_load_multipliers",
                 "_load_delete_caps", "_intents(", "classify("):
        assert gone not in src, f"执行件里还留着决策代码:{gone}"


def test_dry_run_shows_route_and_submits_nothing(monkeypatch):
    calls = _wire(monkeypatch, _zero(3))
    out = mw.run({"execute": False})
    assert calls["put_inv"] == [] and calls["feeds"] == [] and calls["sheet"] == []
    assert calls["settled"] == 0                # dry-run 不落定
    assert "DRY-RUN" in out and "库存 3" in out and "路由 PUT" in out
    assert "'1→0'" in out                       # 逐 SKU 旧值→新值样本


def test_small_batch_routes_to_put_and_records_sync(monkeypatch):
    calls = _wire(monkeypatch, _zero(2))
    out = mw.run({"execute": True})
    assert calls["put_inv"] == [("T1", "S0", 0), ("T1", "S1", 0)]
    assert calls["feeds"] == []
    assert calls["sheet"][0][_c("feed_id")] == "sync"
    assert calls["sheet"][0][_c("result")] == "成功"
    assert calls["sheet"][0][_c("reason")] == "亚马逊缺货"
    assert "同步 PUT 2,成功 2" in out
    assert calls["marked"] == [([100], "sync"), ([101], "sync")]


def test_large_batch_routes_to_feed(monkeypatch):
    calls = _wire(monkeypatch, _zero(11))       # >10 → inventory feed
    mw.run({"execute": True})
    assert calls["put_inv"] == []
    assert calls["feeds"] == [("T1", "inventory", 11)]
    assert all(r[_c("feed_id")] == "F_inventory" and r[_c("result")] == "处理中"
               for r in calls["sheet"])
    assert calls["marked"] == [(list(range(100, 111)), "F_inventory")]


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
    # 炸掉那条也要留痕:动作空、结果写明为什么(否则表里完全看不见)
    t1 = [r for r in calls["sheet"] if r[0] == "T1"]
    assert len(t1) == 1 and t1[0][_c("action")] == ""
    assert t1[0][_c("result")] == "未执行(提交异常)"


def test_delete_kind_routes_to_delete_item_and_lands_events(monkeypatch):
    """删除走 DELETE_ITEM;只有 submitted 落病历(dedup 记了是幽灵事件)。"""
    intents = [{"store": "T1", "sku": s, "kind": "delete", "old": "在线",
                "new": "删除", "code": "variant_offset",
                "reason": "采集永久偏移(拿不到新数据)",
                "label": "删除(variant_offset)", "batches": 1}
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
    # 建议列恒是 scan 定的;动作列前两条是"删除",第三条在途防重是"跳过"
    assert all(r[_c("suggestion")] == "删除(variant_offset)"
               for r in calls["sheet"])
    # 动作列只说做了什么(删除/跳过),原因码归「建议」与「原因」两列
    assert [r[_c("action")] for r in calls["sheet"]] == ["删除", "删除", "跳过"]
    assert calls["sheet"][2][_c("result")] == "在途防重"
    # 只有 submitted 才转 executing:dedup 什么都没提交
    assert calls["marked"] == [([100, 101], "F1")]


def test_unexecuted_suggestions_still_get_a_row(monkeypatch):
    """凭证缺失的店:建议照样写表,动作留空 —— 否则这些行在飞书完全不可见。"""
    calls = _wire(monkeypatch, _zero(2), stores=())
    out = mw.run({"execute": True})
    assert "凭证缺失" in out
    assert len(calls["sheet"]) == 2
    assert all(r[_c("action")] == "" and r[_c("result")] == "未执行(凭证缺失)"
               for r in calls["sheet"])


def test_no_suggestions_points_at_the_scanner(monkeypatch):
    _wire(monkeypatch, [])
    out = mw.run({"execute": True})
    assert "maintenance_scan" in out and "顺序是硬约束" in out


def test_settle_runs_before_claim_and_only_when_executing(monkeypatch):
    calls = _wire(monkeypatch, _zero(1))
    mw.run({"execute": True})
    assert calls["settled"] == 1
    calls2 = _wire(monkeypatch, _zero(1))
    mw.run({"execute": False})
    assert calls2["settled"] == 0



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
                        lambda sheet, rng: [
                            _sheet_row("T1", "B0A", "价格", "", "", "F1",
                                       "2026-08-09", "成功")])
    appended = []
    monkeypatch.setattr(maint_sheet, "append_records",
                        lambda rows: (appended.extend(rows), len(rows))[1])
    out = maint_sheet.resync_from_ledger()
    assert "补写 1 行" in out
    assert appended[0][_c("sku")] == "B0B" and appended[0][_c("action")] == "库存"
    assert appended[0][_c("feed_id")] == "F2"
    assert appended[0][_c("result")] == "失败"
    # 旧值/新值补不回来(PG 只记 SKU 级状态,不存当时的新旧值)
    assert appended[0][_c("old_value")] == "" and appended[0][_c("new_value")] == ""


def test_stale_rows_stop_pinning_the_cursor(monkeypatch):
    """超 3 天未落定判「未查到」并推进水位:一行悬着会让每轮重读整段。"""
    from registry.resources import Spreadsheet
    monkeypatch.setattr(resources, "MAINT_SHEET",
                        Spreadsheet(name="维护记录", token="TOK", sheet_id="SID",
                                    columns=resources.MAINT_SHEET.columns))
    conn = _Conn()
    conn.cursor_value = {"next_row": 4, "unresolved_from": 2}
    _fake_db(monkeypatch, conn)
    monkeypatch.setattr(maint_sheet, "_today", lambda: _date(2026, 8, 9))
    monkeypatch.setattr(maint_sheet.feishu, "sheet_values", lambda sheet, rng: [
        _sheet_row("T1", "B0OLD", "价格", "", "", "F1", "2026-08-01", "处理中"),
        _sheet_row("T1", "B0NEW", "价格", "", "", "F2", "2026-08-09", "处理中"),
    ])
    monkeypatch.setattr(feed_track, "item_results", lambda fid: {"B0NEW": ("submitted", "")})
    monkeypatch.setattr(feed_track, "item_errors", lambda fid: {})
    written = []
    monkeypatch.setattr(maint_sheet.feishu, "sheet_write_ranges",
                        lambda sheet, ups: (written.extend(ups), len(ups))[1])
    out = maint_sheet.sync_from_ledger()
    assert "超 3 天判未查到 1 行" in out
    assert written[0][1][0][0] == "未查到"
    # 老行放行后水位推进到第 3 行(新行仍未落定,停在它身上)
    saved = [a for sql, a in conn.sqls if "ops.cursors" in sql][-1]
    assert '"unresolved_from": 3' in saved[1]


def test_prune_keeps_recent_days_only(monkeypatch):
    """飞书只留近 7 天(一天几千行);历史在 PG,不在表里。"""
    from registry.resources import Spreadsheet
    monkeypatch.setattr(resources, "MAINT_SHEET",
                        Spreadsheet(name="维护记录", token="TOK", sheet_id="SID",
                                    columns=resources.MAINT_SHEET.columns))
    conn = _Conn()
    conn.cursor_value = {"next_row": 6, "unresolved_from": 6}   # 4 个数据行
    _fake_db(monkeypatch, conn)
    monkeypatch.setattr(maint_sheet, "_today", lambda: _date(2026, 8, 9))
    # ⚠ 行必须按**真实的 11 列**给(店铺 SKU 建议 原因 动作 旧值 新值 feedid
    #   日期 结果 报错)。此前这里喂的是 9 列、日期在下标 6 —— 正好和 prune 里
    #   写死的 cells[6] 对齐,于是测试替 bug 背了书。
    monkeypatch.setattr(maint_sheet.feishu, "sheet_values", lambda sheet, rng: [
        ["T1", "B0OLD", "价格", "涨价", "价格", "10", "12.5", "F1",
         "2026-07-01", "成功", ""],
        ["T1", "B0KEEP", "价格", "涨价", "价格", "10", "12.5", "F2",
         "2026-08-08", "成功", ""],
        ["T1", "B0NODATE", "价格", "", "价格", "", "", "F3", "", "成功", ""],
        # 回归钉:**新值**长得像个老日期,而**日期**列是近期的。
        # 读错列的那版会拿新值当日期,把这行当"早于保留期"删掉。
        ["T1", "B0TRAP", "标题", "标题不符", "标题", "旧标题", "2026-07-01",
         "F4", "2026-08-08", "成功", ""],
    ])
    rewritten = []
    monkeypatch.setattr(maint_sheet.feishu, "sheet_overwrite",
                        lambda sheet, rows: (rewritten.extend(rows), len(rows))[1])
    out = maint_sheet.prune(7)
    assert "删 1 行" in out and "留 3 行" in out
    assert [r[1] for r in rewritten] == ["SKU", "B0KEEP", "B0NODATE", "B0TRAP"]
    # 整表重写不许把行截短:结果/报错(J/K)是最后两列,截到 9 列就没了
    ncol = len(resources.MAINT_SHEET.columns)
    assert all(len(r) == ncol for r in rewritten), "重写把行截短了"
    # 表头必须与 registry 列序一一对应,否则裁剪一次表头就和数据错位
    assert tuple(rewritten[0]) == maint_sheet._header()
    assert rewritten[0][maint_sheet._idx("op_date")] == "日期"
    assert rewritten[0][maint_sheet._idx("suggestion")] == "建议"
    # 行号整体上移 → 水位必须重置,否则反哺器扫到错行
    saved = [a for sql, a in conn.sqls if "ops.cursors" in sql][-1]
    assert '"next_row": 5' in saved[1] and '"unresolved_from": 2' in saved[1]


# ── 维护记录反哺器 ────────────────────────────────────────────────────────────

def _c(name: str) -> int:
    """列名 → 下标。**按名字取,不写死数字** —— 2026-08-16 从 9 列加到 11 列时,
    写死下标的断言全线转红(那是好事:它们确实在测错的列);改成按名字取之后,
    下次加列测试不用动。"""
    return resources.MAINT_SHEET.columns.index(name)


def _sheet_row(store, sku, action, old, new, feed_id, date, result, err="",
               suggestion=None, reason=""):
    """按 registry 列序拼一行维护记录夹具(11 列)。"""
    vals = {"store": store, "sku": sku, "suggestion": suggestion or action,
            "reason": reason, "action": action, "old_value": old,
            "new_value": new, "feed_id": feed_id, "op_date": date,
            "result": result, "error": err}
    return [vals[c] for c in resources.MAINT_SHEET.columns]

def test_maint_sheet_sync_from_ledger(monkeypatch):
    monkeypatch.setattr(resources, "MAINT_SHEET",
                        Spreadsheet(name="维护记录", token="TOK", sheet_id="SID",
                                    columns=resources.MAINT_SHEET.columns))
    conn = _Conn()
    conn.cursor_value = {"next_row": 6, "unresolved_from": 2}
    _fake_db(monkeypatch, conn)
    # 日期必须动态生成:反哺器有"超 STALE_DAYS 天判未查到"的墙钟规则,
    # 写死日期的夹具会在三天后悄悄变成在测另一条分支(2026-08-11 实爆:
    # 写死的 08-07 过期,四行全走了未查到,断言在测根本不该触发的兜底)。
    today = maint_sheet._today().isoformat()
    sheet_rows = [
        _sheet_row("T1", "S1", "库存", "5", "0", "F1", today, "处理中"),
        _sheet_row("T1", "S2", "库存", "3", "0", "F1", today, "处理中"),
        _sheet_row("T1", "S3", "价格", "9", "8", "sync", today, "成功"),
        _sheet_row("T1", "S4", "库存", "7", "0", "F2", today, "处理中"),
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
    _R, _E = maint_sheet._col("result"), maint_sheet._col("error")
    assert w[f"{_R}2:{_E}2"] == ["成功", ""]
    assert w[f"{_R}3:{_E}3"] == ["失败", "ERR_P"]
    assert "H5:I5" not in w                     # F2 未落定不动
    assert "回填 2 行" in out
    # 水位推进到第一个未落定行(第 5 行的 F2):unresolved_from=5
    saved = [a for s, a in conn.sqls if "INSERT INTO ops.cursors" in s]
    assert saved and '"unresolved_from": 5' in saved[-1][1]


def test_price_intents_include_shipping_and_skip_when_missing(monkeypatch):
    """定价输入是落地价(单价 + 运费);运费没采到一律不改价。

    漏运费 = 按比成本低的数乘倍率,越贵的运费亏得越多;当 0 更糟——
    价照样定得出来、看着正常,两侧都不报错。
    """
    rows = [
        # 落地价 20+5=25 → ×200% = 50(漏运费的话会算成 40)
        _row(sku="B0SHIP", wm_price=20.0, amz_price=20.0, shipping=5.0),
        # 确认免运费:照常定价
        _row(sku="B0FREE", wm_price=20.0, amz_price=20.0, shipping=0.0),
        # 运费没采到:不改价
        _row(sku="B0NOSHIP", wm_price=20.0, amz_price=20.0, shipping=None),
    ]
    monkeypatch.setattr(mi, "_rows", lambda conn, sz: rows)
    out = {i["sku"]: i["new"] for i in mi.price_intents(_Conn(), _MULTS, [])}
    assert out == {"B0SHIP": 50.0, "B0FREE": 40.0}


def test_match_inventory_intents_fills_zero_stock():
    """跟卖品铺货(所有者批复 2026-08-12):唯一给 source_type='match' 行
    补库存的路径;stockzero 店排除(解除后自动回补,修清零/回补不对称)。"""
    conn = _Conn(rows=[("T1", "PHUMWMT001", 0), ("T2", "PHUMWMT002", None)])
    out = mi.match_inventory_intents(conn, ["Z店"])
    assert [(i["store"], i["sku"], i["kind"], i["new"]) for i in out] == [
        ("T1", "PHUMWMT001", "inventory", mi.MATCH_INVENTORY_QTY),
        ("T2", "PHUMWMT002", "inventory", mi.MATCH_INVENTORY_QTY)]
    sql, args = conn.sqls[0]
    assert "source_type = 'match'" in sql       # 路由铁律:只碰跟卖出身
    assert "missing_since IS NULL" in sql       # 只补在架行
    assert args == (["Z店"],)                   # stockzero 店整店排除


def test_no_hardcoded_column_letters_left():
    """列字母一律从 registry.MAINT_SHEET.columns 推,不许再写死 A/H/I。

    2026-08-16 从 9 列加到 11 列时,硬编码那版会把「动作」写进「建议」列、
    把回执写进「新值」列 —— **整表错位且不报错**。这条守的是下次再加列。
    """
    import inspect
    import re

    src = inspect.getsource(maint_sheet)
    # 允许同列范围(f"A{row}:A{end}" 是扫 A 列找下一个空行,与列数无关);
    # 禁的是**跨列**写死,那种才会随列数变化而错位
    bad = [m for m in re.findall(r'f"([A-Z])\{[^}]+\}:([A-Z])\{', src)
           if m[0] != m[1]]
    assert not bad, f"仍有写死列字母的跨列范围:{bad}"
    assert maint_sheet._span() == ("A", "K")           # 11 列
    assert maint_sheet._col("result") == "J"
    assert maint_sheet._col("error") == "K"


def test_row_builder_is_the_only_place_that_shapes_a_row():
    """维护记录只有一处造行 —— 散在各分支手拼元组的话,加列时漏改一处就错位。"""
    import inspect

    from workflows import maintenance as wf
    src = inspect.getsource(wf)
    assert "def _record(" in src
    # 各分支一律走 _record(),不再出现 records.append((name, ...
    assert "records.append((name," not in src
    row = wf._record("T1", {"sku": "S1", "kind": "inventory", "old": 5,
                            "new": 0, "reason": "Currently unavailable"},
                     "库存", "F1", "2026-08-16", "处理中", "")
    assert len(row) == len(resources.MAINT_SHEET.columns)
    assert row[_c("suggestion")] == "库存" and row[_c("action")] == "库存"
    assert row[_c("reason")] == "Currently unavailable"
    # 「建议」来自 scan(label 优先),「动作」是执行件真做了什么 —— 两者可分歧
    skipped = wf._record("T1", {"sku": "S1", "kind": "delete",
                                "label": "删除(not_found)"},
                         "跳过", "OLD", "2026-08-16", "在途防重", "")
    assert skipped[_c("suggestion")] == "删除(not_found)"
    assert skipped[_c("action")] == "跳过"


# ── 建议行落定(维护链专属)──────────────────────────────────────────────────

class _SettleConn(_Conn):
    """settle_maintenance 的假连接:第一条 SQL 取待判行,后面两条是 UPDATE。"""

    def __init__(self, rows):
        super().__init__(rows)
        self.updates = []

    def execute(self, sql, args=None):
        super().execute(sql, args)
        if "UPDATE ops.dispositions" in sql:
            self.updates.append((args["status"], sorted(args["ids"])))


def test_settle_maintenance_splits_by_whether_the_value_actually_changed():
    """维护三类没有核验事件,判据就是"重新观测后线上值是不是我们要的值"。"""
    from services import dispositions as ds

    conn = _SettleConn([
        (1, "price", {"new": 30.0}, 30.0, None, None),       # 改过来了
        (2, "price", {"new": 30.0}, 20.0, None, None),       # 线上还是旧价
        (3, "inventory", {"new": 0}, None, 0, None),         # 清零到位
        (4, "inventory", {"new": 0}, None, 10, None),        # 库存没动
        (5, "title", {"new": "新标题"}, None, None, "新标题"),
        (6, "title", {"new": "新标题"}, None, None, "旧标题"),
    ])
    assert ds.settle_maintenance(conn) == {"confirmed": 3, "ineffective": 3}
    assert conn.updates == [("confirmed", [1, 3, 5]), ("ineffective", [2, 4, 6])]
    # 只判**已被 catalog_sync 重新观测过**的行 —— 拿提交前的旧快照判,
    # 会把每一条都判成"没生效"
    assert "w.last_seen_at > d.executed_at" in ds._MAINT_OPEN_SQL
    assert "d.status = 'executing'" in ds._MAINT_OPEN_SQL


def test_settle_maintenance_never_guesses_effective():
    """⚠ 值转不动一律判未生效:判错成生效 = 这条建议销案、商品永远停在错的值上。"""
    from services import dispositions as ds

    assert ds.maint_effective("price", 30.0, None, None, None) is False
    assert ds.maint_effective("inventory", 0, None, None, None) is False
    assert ds.maint_effective("title", "新", None, None, None) is False
    assert ds.maint_effective("title", "新", None, None, "新") is True
    # 浮点回读:30.0 与 30.00 是同一个价
    assert ds.maint_effective("price", 30.0, 30.00, None, None) is True


def test_expire_executing_unblocks_the_partial_unique_index():
    """executing 行永远不落定 = 那个 SKU 的那类维护永久停摆,而且完全静默。"""
    from services import dispositions as ds

    q = ds._EXPIRE_SQL
    assert "status = 'executing'" in q
    # 只放行本链动作:删除类有自己的 48h 宽限 + 观测判定,不该被时限抢先判掉
    assert "action = ANY(%(actions)s::text[])" in q
    assert "executed_at < now() - make_interval(days => %(days)s::int)" in q
    assert ds.MAINT_ACTIONS == ("title", "price", "inventory")
    assert "delete" not in ds.MAINT_ACTIONS


def test_two_chains_claim_disjoint_sources():
    """⚠ 两条链共用一张建议表:不限来源就会领到对方的建议行。"""
    import inspect

    from services import dispositions as ds
    from workflows import problem_product_cleanup as ppc

    assert set(ds.PROBLEM_SOURCES) & set(ds.MAINT_SOURCES) == set()
    assert "dispositions.PROBLEM_SOURCES" in inspect.getsource(ppc.run)
    assert "dispositions.MAINT_SOURCES" in inspect.getsource(mw.run)
    # 维护链的动作若落进问题商品链的分桶,那边会直接抛(宁炸不吞)
    import pytest
    with pytest.raises(ValueError):
        ppc.group_by_store([{"id": 1, "store": "T1", "sku": "S", "action": "price"}])


# ── product_refresh 的 wait(工作项 A;产品线一条链跑完的前提)────────────────

def test_product_refresh_actually_reads_the_wait_param():
    """⚠ 2026-08-16 之前:用法行写着 -p wait=1,run() 里从头到尾没读过它。

    传了等于没传 —— 后果不是报错,是**静默降级**:推完立刻返回,链里下一步
    product_ingest 摄回来的还是上一轮的数据,而摘要看起来一切正常。
    """
    import inspect

    from workflows import product_refresh as pr
    src = inspect.getsource(pr.run)
    assert 'params.get("wait")' in src
    assert "wait_settled(" in src


def test_wait_settled_polls_until_settled_and_reports_timeout(monkeypatch):
    from services import scrape_batches as sb

    # ⚠ 队列**不能 pop 空**:IndexError 是 LookupError 的子类,会被
    # wait_settled 的 `except LookupError` 当成"采集侧查无此批次"吃掉
    # (写这条用例时实际踩到,断言从 1 变成 0)。最后一个值重复返回。
    seen = []
    states = {"b1": [False, True], "b2": [False]}

    def fake_status(name):
        seen.append(name)
        q = states[name]
        v = q.pop(0) if len(q) > 1 else q[0]
        return {"stats": {"open": 0 if v else 3}}

    monkeypatch.setattr(sb.scraper, "batch_status", fake_status)
    monkeypatch.setattr(sb, "logger", sb.logger)
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)

    line, unsettled = sb.wait_settled(["b1", "b2"], timeout_min=0.01)
    assert unsettled == 1                       # b2 没落定
    assert "已落定 1" in line and "仍未采完 1" in line


def test_wait_settled_gives_up_on_batches_the_scraper_lost(monkeypatch):
    """采集侧查不到了:别再问,交给 check_open 那层认账(不算"未采完")。"""
    from services import scrape_batches as sb

    monkeypatch.setattr(sb.scraper, "batch_status",
                        lambda n: (_ for _ in ()).throw(LookupError("no such batch")))
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    line, unsettled = sb.wait_settled(["gone"], timeout_min=1)
    assert unsettled == 0 and "采集侧查无 1" in line


def test_wait_settled_is_not_a_duplicate_of_order_audit_wait():
    """两个等待函数**能力不同**,不是重复实现 —— 订单审核那条还要等截图。

    合成一个再加开关,只会让两边的超时语义互相拖累(本仓"同一目的多种方法"
    那条:能力不同就写两个显式函数)。
    """
    import inspect

    from services import scrape_batches as sb
    from workflows import order_audit
    assert "screenshots" not in inspect.getsource(sb.wait_settled)
    assert "shots" in inspect.getsource(order_audit._wait_for_batches)
