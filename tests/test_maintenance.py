"""maintenance 回归:stockzero 名单/清零意图/路由/dry-run 零提交/维护记录/反哺器。"""

import contextlib
import copy
import pathlib
import re
from datetime import date as _date

import pytest

from api import feeds, feishu, inventory as inv_api, prices
from registry import resources
from registry.resources import Spreadsheet
from services import feed_track, maint_sheet, \
    maintenance_intents as mi, store_limits, store_targets as st
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

    @property
    def description(self):
        """psycopg 的 cursor.description。按**最后一条 SQL** 给列名。

        `stuck_executing` 等函数走 `dict(zip(cols, row))` 取值,没有它就 AttributeError。
        给固定列名而不是 None:拿位置解包的假桩会让"SQL 加了一列而读侧漏改"
        这类错在测试里看不出来。
        """
        class _D:
            def __init__(self, name):
                self.name = name
        if "FROM ops.dispositions" in getattr(self, "_last", ""):
            return [_D(n) for n in ("store", "action", "n", "oldest")]
        return []

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
    # 列序 = _SQL_ZERO 的 SELECT:(store, sku, avail_qty, node_qty)
    conn = _Conn(rows=[("T1", "S1", 5, None), ("T1", "S2", 12, None),
                       ("T1", "S3", None, None)])
    out = mi.zero_intents(conn, ["T1"])
    # 未知库存不动(旧系统 None != 0 盲清是坑):S3 不出现
    assert [(i["sku"], i["kind"], i["old"], i["new"], i["code"]) for i in out] == [
        ("S1", "inventory", 5, 0, "stockzero"),
        ("S2", "inventory", 12, 0, "stockzero")]
    assert all("ship_node" not in i for i in out)    # 未配置店不带这个键
    assert mi.zero_intents(conn, []) == []           # 无 stockzero 店零查询


def test_zero_intents_clears_the_managed_node_not_the_total():
    """⚠ 多仓下清的是**受管仓**(所有者定稿:自动链只管自建自发货仓)。

    按合计选行、只清一个节点的话,合计永远 > 0 ⇒ 每轮重选重清,而"这家店
    停售"从未真达成 —— 摘要还一直显示"清零 N 条"。故障清单里唯一后果是
    钱在漏的那条。
    """
    # S1:受管仓 4 件(合计 9,别的节点 5 件不归自动链管)→ 清 4
    # S2:受管仓 0 件(合计 5)→ 已经停售,不动
    # S3:受管仓明细还没扫到 → **不回落合计**,本轮跳过
    conn = _Conn(rows=[("T1", "S1", 9, 4), ("T1", "S2", 5, 0),
                       ("T1", "S3", 7, None)])
    out = mi.zero_intents(conn, ["T1"], managed={"T1": "FC9"})
    assert [(i["sku"], i["old"], i["new"], i["ship_node"]) for i in out] == [
        ("S1", 4, 0, "FC9")]


# amz 侧 × 沃尔玛侧联表的一行(顺序 = _SQL_AMZ_JOIN 的 SELECT 列)
# ⚠ 默认的 name 与 slow.title 必须是**同一个商品**(处理后相似度高):
# 2026-08-16 起 price/inventory 两个 provider 都会判"标题相似度 < 70% → 该删",
# 该删的行不再产改价/清零意图。默认值若是随手写的两个不相干字符串,
# 全部用例会集体空转而看起来只是"没有意图"。
class _DelConn(_Conn):
    """删除链假连接:偏移件与「连续无货/渠道不符」两条 SQL **列数不同**。

    ⚠ 2026-08-25 之前两条都返 5 列,测试就用一份 rows 喂两边 —— 那是巧合不是
    契约。渠道维(2026-08-25)与二手维(2026-09-14)加进来之后 LONG_OOS 返 8 列,
    共用一份 rows 会当场 ValueError
    (这次就是这么发现的),而列数万一又撞上就会**静默错位**。按 SQL 分开给。
    """

    def __init__(self, offset=None, oos=None):
        super().__init__()
        self.offset, self.oos = offset or [], oos or []

    def fetchall(self):
        return self.offset if "variant_offset" in self._last else self.oos


def _row(store="T1", sku="B0A", name="Steel Cup", pt="Cups", upc="012345678905",
         wm_price=20.0, avail_qty=10, amz_price=10.0, stock_count=7,
         delivery_days=3, slow=None, fulfillment="FBM", shipping=0.0,
         outcome="ok", stock_status="In Stock", stock_state="in_stock",
         offer_condition="N/A"):
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
            "stock_state": stock_state,
            # 缺省 N/A = **未知,不是全新**(采集契约:全新品的 buybox 不写品相)。
            # 判据「未采到不算二手」正是靠这个缺省值在全部既有用例里被钉住
            "offer_condition": offer_condition}


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
    out = {i["sku"]: i["new"] for i in mi.price_intents(rows, _MULTS)}
    assert out == {"B0CHANGE": 40.0, "B0OUTBAND": 15000.0}


def test_price_intents_skip_when_fulfillment_unknown(monkeypatch):
    """FBA/FBM 决定用哪套区间;未知**不猜**(所有者 2026-08-09:必须获取)。"""
    rows = [
        _row(sku="B0FBM", wm_price=20.0, amz_price=15.0, fulfillment="FBM"),
        _row(sku="B0FBA", wm_price=20.0, amz_price=15.0, fulfillment="FBA"),
        _row(sku="B0UNK", wm_price=20.0, amz_price=15.0, fulfillment=None),
        _row(sku="B0JUNK", wm_price=20.0, amz_price=15.0, fulfillment="???"),
    ]
    mults = {"T1": {"fbm_range1": "200%", "fba_range1": "300%"}}
    out = {i["sku"]: i["new"] for i in mi.price_intents(rows, mults)}
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
    out = {i["sku"]: i["new"] for i in mi.inventory_intents(rows)}
    assert out == {"B0SYNC": 7, "B0OOS": 0, "B0UNKNOWN": 0, "B0LEAD9": 0,
                   "B0LEAD8": 0, "B0LEAD7": 50}


def test_inventory_threshold_applies_to_every_store_and_cap_to_its_store():
    """门槛决定卖不卖,最大库存决定卖多少(所有者定稿 2026-09-25):
    亚马逊 <5 写 0,**所有店**(「库存维护也需要门槛5」—— 此前 1~4 件照写原数);
    过了门槛写 min(亚马逊库存, 本店最大库存 N),没设 N 照原数。清零判据
    (缺货/渠道/货期…)照旧排在前面。换算与上架同一个函数 `store_limits.stock_for`。"""
    caps = {"T_CAP3": 3, "T_CAP20": 20}
    rows = [
        _row(store="T_FREE", sku="B0FOUR", avail_qty=4, stock_count=4),
        _row(store="T_FREE", sku="B0FIVE", avail_qty=9, stock_count=5),
        _row(store="T_CAP3", sku="B0BIG", avail_qty=50, stock_count=50),
        _row(store="T_CAP3", sku="B0AT3", avail_qty=3, stock_count=99),  # 已是 3
        _row(store="T_CAP3", sku="B0FOUR3", avail_qty=3, stock_count=4),
        _row(store="T_CAP20", sku="B0TWELVE", avail_qty=20, stock_count=12),
        _row(store="T_CAP20", sku="B0NULL", avail_qty=20, stock_count=None),
        _row(store="T_CAP3", sku="B0OOS", avail_qty=3, stock_count=50,
             stock_state="out_of_stock"),
    ]
    got = {i["sku"]: (i["new"], i["code"])
           for i in mi.inventory_intents(rows, stock_caps=caps)}
    assert got == {
        "B0FOUR": (0, store_limits.QTY_BELOW_MIN),     # 没设 N 的店也卡门槛
        "B0FIVE": (5, ""),                             # 没设 N:原样跟随
        "B0BIG": (3, store_limits.QTY_CAPPED),         # 50 → 3
        "B0FOUR3": (0, store_limits.QTY_BELOW_MIN),    # 4 件:过不了门槛
        "B0TWELVE": (12, ""),                          # 12 < 20:写 12,不是 0
        "B0NULL": (0, store_limits.QTY_NO_COUNT),
        "B0OOS": (0, "out_of_stock"),                  # 清零判据在前
    }
    reasons = {i["sku"]: i["reason"]
               for i in mi.inventory_intents(rows, stock_caps=caps)}
    assert reasons["B0BIG"] == "按本店最大库存 3 写(亚马逊 50)"
    assert reasons["B0FOUR"] == "亚马逊库存 4 低于门槛 5"
    # 不传上限 = 不限(直接调本函数的排查不会静默拿到一份飞书读)
    assert {i["sku"]: i["new"] for i in mi.inventory_intents(rows)}["B0AT3"] == 99


def test_collect_all_reads_store_caps_once_for_the_amz_inventory_provider(
        monkeypatch):
    """「最大库存」只给跟随亚马逊的库存 provider;跟卖铺货与 stockzero 不受管
    (所有者定稿 2026-09-25「跟卖不管」)。"""
    conn = _Conn()
    reads, seen = [], {}
    monkeypatch.setattr(mi.store_limits, "stock_caps",
                        lambda: reads.append(1) or {"T1": 3})
    monkeypatch.setattr(mi.store_limits, "price_multipliers", lambda: {})
    monkeypatch.setattr(st, "store_channels", lambda: {})
    monkeypatch.setattr(mi, "_rows", lambda *a, **k: [])
    monkeypatch.setattr(mi, "delete_intents", lambda *a, **k: [])
    monkeypatch.setattr(mi, "title_intents", lambda rows: [])
    monkeypatch.setattr(mi, "price_intents", lambda rows, m: [])

    def inv(rows, mn=None, store_channels=None, stock_caps=None):
        seen["caps"] = stock_caps
        return []

    monkeypatch.setattr(mi, "inventory_intents", inv)
    monkeypatch.setattr(mi, "zero_intents", lambda c, sz, mn=None, only=None: [])
    monkeypatch.setattr(mi, "match_inventory_intents",
                        lambda c, sz, mn=None, only=None: [])
    monkeypatch.setattr(mi, "drop_recent", lambda c, i: (i, 0))
    mi.collect_all(conn, [])
    assert reads == [1] and seen["caps"] == {"T1": 3}


def test_channel_mismatch_zeroes_the_inventory(monkeypatch):
    """本店 FBA、货变成 FBM ⇒ 这家店卖不了 ⇒ 库存写 0(所有者定稿 2026-08-25)。

    **清零不删除**:清零可逆(渠道翻回来自动回补),删除不可逆。真下架由删除链
    的「渠道不符 N 天」窗口收尾 —— 与"缺货清零 → 连续无货 N 天才删"同一条阶梯。
    """
    rows = [
        _row(sku="B0FBM", avail_qty=9, stock_count=50, fulfillment="FBM"),
        _row(sku="B0FBA", avail_qty=9, stock_count=50, fulfillment="FBA"),
    ]
    out = {i["sku"]: (i["new"], i["code"])
           for i in mi.inventory_intents(rows, store_channels={"T1": "FBA"})}
    # 本店只做 FBA:FBM 的货清零并带自己的原因码,FBA 的货照常同步(无原因码)
    assert out == {"B0FBM": (0, "channel_mismatch"), "B0FBA": (50, "")}


def test_channel_unknown_or_unmarked_store_never_zeroes(monkeypatch):
    """两个方向都不许猜(写反了都不报错,而且是"无辜商品被清零再删掉")。

    · 店**没标**「配送限制」→ 什么渠道都能卖(所有者:「没标就都能上」);
    · 产品渠道**采不到 / 采出第三种值** → 不算不符(那说明采集侧 is_fba 坏了,
      要修的是采集,不是把货清零)。
    """
    rows = [_row(sku="B0UNK", avail_qty=9, stock_count=50, fulfillment=None),
            _row(sku="B0JUNK", avail_qty=9, stock_count=50, fulfillment="N/A"),
            _row(sku="B0FBM", avail_qty=9, stock_count=50, fulfillment="FBM")]
    # ① 店限定 FBA:未知与第三种值照常同步 50,只有确定的 FBM 清零
    got = {i["sku"]: i["new"] for i in mi.inventory_intents(
        rows, store_channels={"T1": "FBA"})}
    assert got == {"B0UNK": 50, "B0JUNK": 50, "B0FBM": 0}
    # ② 店没标:一行都不因渠道清零(不传 = 不限制,与传空字典同义)
    assert {i["sku"]: i["new"] for i in mi.inventory_intents(rows)} \
        == {"B0UNK": 50, "B0JUNK": 50, "B0FBM": 50}


def test_channel_mismatch_outranks_out_of_stock_as_a_reason(monkeypatch):
    """两条都命中时动作一样(清零),原因码要报**渠道不符**。

    「缺货」是暂时的(等回货),「渠道不符」是结构性的(回了货也卖不了)。
    排在缺货后面就会被盖住,而缺货那一栏天天几百条。
    """
    act, code, _ = mi.classify(stock_state="out_of_stock", channel_bad=True)
    assert (act, code) == ("inventory", "channel_mismatch")
    # 删除仍然压过一切:一个 SKU 一轮只出一个动作
    assert mi.classify(outcome="not_found", channel_bad=True)[0] == "delete"


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
    out = mi.title_intents(rows)
    assert [i["sku"] for i in out] == ["B0NEW"]
    assert out[0]["new"] == "Steel Cup"          # 与上架同一套文案处理(去品牌)
    assert out[0]["product_type"] == "Cups" and out[0]["product_id"]


def test_amz_join_honors_routing_and_stockzero():
    # 路由铁律:只作用于 source_type='amz';stockzero 店整店排除(归 zero_intents)
    assert "source_type = 'amz'" in mi._SQL_AMZ_JOIN
    assert "NOT (w.store = ANY(%(stores)s::text[]))" in mi._SQL_AMZ_JOIN
    assert "missing_since IS NULL" in mi._SQL_AMZ_JOIN
    assert "zip_verify" in mi._SQL_AMZ_JOIN


def test_amz_join_reads_the_identity_key_not_the_raw_sku():
    """三个 amz provider 共用的取数按**身份键**接 products 与 latest_snapshot。

    切码之后把 ASIN 列直接跟裸 SKU 比会**静默**匹配不上任何一行 —— 不报错,
    只是维护链对新码永久失明(不改价、不清零)。唯一写法见 conventions §九。
    ls 已经是 INNER JOIN 且限 source_type='amz',顺序在前可直接引用;用
    coalesce 而不是裸 ls.source_key,是因为 register 允许 source_key 缺省,
    那些行今天靠回落裸 sku 命中。
    """
    q = mi._SQL_AMZ_JOIN
    assert "p.asin = coalesce(ls.source_key, w.sku)" in q       # 0a-12
    assert "l.asin = coalesce(ls.source_key, w.sku)" in q       # 0a-13
    assert "p.asin = w.sku" not in q and "l.asin = w.sku" not in q


def test_variant_offset_joins_scrape_failures_through_the_registry():
    """变体偏移的删除面按身份键接 ops.scrape_failures(0a-14)。

    驱动表换成 walmart_items 是为了让 ls 先就位;ls 本来就是 INNER JOIN
    (未登记行今天已被排除),存量 amz 行 source_key = sku ⇒ 同一个集合。
    """
    q = mi._SQL_VARIANT_OFFSET
    assert "JOIN vo ON vo.asin = coalesce(ls.source_key, w.sku)" in q
    assert "w.sku = vo.asin" not in q
    assert "FROM catalog.walmart_items w" in q


def test_long_oos_live_cte_carries_the_identity_key():
    """catalog.snapshots 按 ASIN 存,live CTE 必须把身份键带出来才接得上 obs。

    带不出来 ⇒ 连续缺货删除对新码失明。输出的仍是真 SKU:意图行照旧按
    (store, sku) 走,GROUP BY 里多带一列不改分组粒度(同一 (store,sku) 只有
    一个身份键)。
    """
    q = mi._SQL_LONG_OOS
    assert "coalesce(ls.source_key, w.sku) AS asin" in q
    assert "JOIN obs o ON o.asin = live.asin" in q
    assert "o.asin = live.sku" not in q
    assert "GROUP BY live.store, live.sku, live.asin, live.want" in q
    # 最终输出仍是 (store, sku, …):身份键只用来接快照,不外泄
    assert ("SELECT store, sku, obs, first_seen, last_seen, wrong_ch_obs, "
            "want, used_obs" in q)


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

    conn = _DelConn(offset=[("T1", "B0A", 1, None, None),
                            ("T1", "B0B", 2, None, None)])
    out = mi.delete_intents(conn, [])
    # code = 机器码(分组用),reason = 人读文案 —— 2026-08-16 起分开两列
    assert [(i["store"], i["sku"], i["kind"], i["old"], i["new"], i["code"])
            for i in out] == [
        ("T1", "B0A", "delete", "在线", "删除", "variant_offset"),
        ("T1", "B0B", "delete", "在线", "删除", "variant_offset")]
    assert all(i["reason"] and i["reason"] != i["code"] for i in out)

    # ⚠ 2026-08-24 归一:扫描件**不再截**单店上限,如实报待办。上限只在
    # 执行件领取时施加一次(dispositions.cap_destructive)——此前两条链各截
    # 一次同一张限额表,每店最多 N 条实际变成了最多 2N。
    rows3 = [("T1", "B0A", 1, None, None), ("T1", "B0B", 1, None, None),
             ("T2", "B0C", 1, None, None)]
    assert len(mi.delete_intents(_DelConn(offset=rows3), [])) == 3
    assert not hasattr(mi, "DELETE_PER_STORE")   # 唯一出处搬去 dispositions


def test_delete_intents_also_take_title_placeholder(monkeypatch):
    """占位符[商品不存在]:旧系统只是跳过标题,所有者 2026-08-09 改为删除。"""
    rows = [
        _row(sku="B0GONE", slow={"title": "[商品不存在]"}),
        _row(sku="B0OK", name="正常标题", slow={"title": "正常标题"}),
        _row(sku="B0DUP", slow={"title": "[商品不存在]"}),
    ]
    # B0DUP 同时是偏移件:两个原因命中只删一次
    out = mi.delete_intents(
        _DelConn(offset=[("T1", "B0DUP", 1, None, None)]), rows)
    got = {i["sku"]: i["code"] for i in out}
    assert got == {"B0DUP": "variant_offset", "B0GONE": "商品不存在"}
    assert [i["label"] for i in out if i["sku"] == "B0GONE"] == ["删除(商品不存在)"]


def test_long_oos_delete_sql_guards():
    """连续无货 N 天 → 删除。三道判据缺一个都会误删(所有者定稿 2026-08-09)。"""
    q = mi._SQL_LONG_OOS
    # 1. 窗口内一条"本店卖得了"的观测都没有
    assert "stock_count > 0" in q and "stock_state = 'in_stock'" in q
    assert "sellable_obs = 0" in q
    # 2. 至少一条**明确**卖不了的观测(缺货 或 确定是另一个渠道)
    #    ——防"15 天全是 unknown(采不全)"被当成缺货
    assert "stock_state = 'out_of_stock'" in q
    assert "oos_obs + wrong_ch_obs + used_obs > 0" in q
    # 3. 窗口两端都有观测——防"两头各采一次、中间断 13 天"被当连续
    assert "interval '36 hours'" in q and "last_seen >= now()" in q
    # 降级采集的 fast 段基本是空的,拿它判缺货是冤案
    assert "COALESCE(sn.outcome, 'ok') = 'ok'" in q
    # 破坏动作守路由铁律 + 在架 + 店铺 ACTIVE
    assert "ls.source_type = 'amz'" in q
    assert "published_status = 'PUBLISHED'" in q
    assert "upper(st.store_status) = 'ACTIVE'" in q
    assert mi.LONG_OOS_DAYS == 15


def test_long_oos_window_is_evaluated_per_channel():
    """「该渠道下库存连续不足 N 天,下架」(所有者定稿 2026-08-25)。

    三个方向写反了都不报错,逐条钉住:
      · 渠道来自快照 raw.is_fba,**且必须 coalesce 成二值** —— 三值逻辑下
        `NOT (… AND NULL)` 是 NULL,FILTER 当不命中,于是"渠道采不到"的观测
        会从"挡住删除"翻成"不挡",正好反了;
      · 店没标(want='')时两个渠道条件恒假 ⇒ 判据逐字退回旧口径;
      · 确定是另一个渠道的"有货"观测,既不算卖得了、又算一条明确证据。
    """
    q = mi._SQL_LONG_OOS
    assert "raw ->> 'is_fba'" in q
    assert "coalesce(upper(btrim(sn.raw ->> 'is_fba')), '')" in q
    assert "coalesce(sn.stock_count > 0" in q and "false) AS has_stock" in q
    # 店铺渠道要求从**参数**进来(硬编码进 SQL 就等于代码里写死配置)
    assert "%(ch_stores)s::text[]" in q and "%(ch_wants)s::text[]" in q
    assert "coalesce(req.want, '') AS want" in q
    # 没标的店:want='' ⇒ 这两处条件恒假 ⇒ sellable_obs 退回 in_stock_obs
    assert q.count("live.want <> ''") == 2
    assert "o.channel <> live.want" in q


def test_long_oos_intents_carry_reason(monkeypatch):
    out = mi.delete_intents(
        _DelConn(oos=[("T1", "B0DEAD", 15, None, None, 0, "", 0)]), [],
        oos_days=15)
    assert [(i["sku"], i["code"], i["label"]) for i in out] == [
        ("B0DEAD", "连续无货15天", "删除(连续无货15天)")]
    # 「原因」列要读得出"低到什么程度",机器码读不出来
    assert out[0]["reason"] == "15 天窗口内 15 次观测无一有货,货源已断"


def test_wrong_channel_delete_has_its_own_reason_code(monkeypatch):
    """渠道不符走到头 ≠ 货源断了:原因码分开,删除预览按码分组才说得清。"""
    out = mi.delete_intents(
        _DelConn(oos=[("T1", "B0WRONG", 15, None, None, 12, "FBA", 0),
                      ("T1", "B0DEAD", 15, None, None, 0, "FBA", 0)]), [],
        oos_days=15)
    assert {i["sku"]: i["code"] for i in out} == {
        "B0WRONG": "渠道不符15天", "B0DEAD": "连续无货15天"}
    why = {i["sku"]: i["reason"] for i in out}
    assert "本店渠道(FBA)" in why["B0WRONG"] and "12 次确认为另一渠道" in why["B0WRONG"]


# ══════════════════════════════════════════════════════════════════════════════
#  二手/翻新 offer(所有者定稿 2026-09-14:「对于二手商品,库存设置为 0,
#  然后沿用 15 天无货就删除的逻辑」;采集侧字段 offer_condition,commit 9c70ce0)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("value, want", [
    ("Used - Like New", True), ("Used - Very Good", True),
    ("Used - Good", True), ("Used - Acceptable", True),
    ("Open Box", True), ("Collectible", True),
    ("Renewed", True), ("Refurbished", True),
    ("Used - Fair", True),          # 契约里还没有的新档位:反着写才认得
    ("N/A", False), ("n/a", False), ("  N/A  ", False),
    ("", False), (None, False),
    ("New", False),                 # 契约说不会出现;真出现按字面读,不反判
])
def test_used_offer_reads_the_contract_backwards(value, want):
    """判据**反着写**:除 N/A / 空 / New 之外都算非全新。

    两条理由都在采集契约里:取值域全是非全新的档位;而**全新品也是 N/A 而不是
    New**(全新 offer 的 buybox 根本不写品相)。所以 N/A = 未知 ≠ 全新。
    反着写才对新增档位免疫 —— 正着列白名单会静默漏掉 `Used - Fair` 这种,
    而漏掉的方向是"当成全新继续卖"。
    """
    assert mi.used_offer(value) is want


def test_unknown_condition_never_counts_as_used():
    """**方向题**:未采到一律不算二手(与 is_fba、定制品闸同向)。

    反过来写的后果不是少清几条,是无辜商品先被清零、再被 15 天窗口删掉,
    而删除不可逆。采集侧这个字段 2026-09-14 才上线且自称未经真实页面验证,
    DOM 变了就会整批返回 N/A —— 那时这条判据是唯一的护栏。
    """
    assert mi.classify(offer_condition=None)[0] is None
    assert mi.classify(offer_condition="N/A")[0] is None


def test_used_offer_zeroes_and_outranks_channel_and_oos():
    """二手 → 清零,且**排在渠道与无货三档之前**(顺序即优先级)。

    三条都命中时动作一样(清零),差别只在原因码,而处置完全不同:二手是
    "这个 offer 我们根本不该卖",渠道是"这家店做不了这个渠道",缺货是"等回货"。
    排在后面就会被天天几百条的缺货盖住。
    """
    act, code, why = mi.classify(offer_condition="Used - Good",
                                 channel_bad=True, channel_note="ch",
                                 stock_state="out_of_stock")
    assert (act, code) == ("inventory", "used_offer")
    assert "Used - Good" in why          # 原因列要读得出是哪一档,机器码读不出
    # 删除仍压过一切:该删的行不该先花配额去清零
    assert mi.classify(outcome="not_found",
                       offer_condition="Renewed")[0] == "delete"


def test_inventory_provider_zeroes_used_offers():
    out = mi.inventory_intents([_row(sku="B0USED", offer_condition="Open Box",
                                     avail_qty=9, stock_count=9)], {})
    assert [(i["sku"], i["new"], i["code"]) for i in out] == [
        ("B0USED", 0, "used_offer")]
    # 对照组:品相未知的行照常按亚马逊库存走,**不被误伤清零**
    (same,) = mi.inventory_intents(
        [_row(sku="B0NEW", offer_condition="N/A", avail_qty=0, stock_count=9)], {})
    assert (same["sku"], same["new"], same["code"]) == ("B0NEW", 9, "")


def test_price_provider_refuses_to_reprice_a_used_offer():
    """二手**不改价** —— 这是 offer_condition 这个字段存在的头号理由。

    采集契约原话:二手 offer 的 current_price 是**二手价**,与全新价不可混用
    (实例 B0G449YVHD 采到 $20.70,卖家 Amazon Resale)。改价链的落地价正是
    amz 现价 + 运费,拿二手价 × 全新倍率改线上价,算得出来、看着也正常、
    没有任何一侧报错 —— 与"配送方式未知就不改价"同一条纪律。

    ⚠ 这条必须单独钉:price_intents 只在 classify 判 **delete** 时才跳过,
    而二手返回的是 `inventory` —— 只看 delete 拦不住它(本次接线前的真实状态)。
    """
    used = _row(sku="B0USED", offer_condition="Used - Very Good",
                wm_price=40.0, amz_price=10.0)
    assert mi.price_intents([used], _MULTS) == []
    # 对照组:同样的价,品相未知 ⇒ 照常改价(证明拦住它的是品相,不是别的)
    ok = _row(sku="B0OK", offer_condition="N/A", wm_price=40.0, amz_price=10.0)
    assert [i["sku"] for i in mi.price_intents([ok], _MULTS)] == ["B0OK"]


def test_long_oos_window_counts_used_as_unsellable_evidence():
    """15 天窗口把二手接成**第三类观测**,与渠道那一维同一种改法。

    最容易漏、且漏了不报错的一处:二手 offer 通常是**有货**的,不从
    `sellable_obs` 里剔掉的话每条观测都算"卖得了",窗口永远熬不满 ⇒
    清零了却永远删不掉。
    """
    q = mi._SQL_LONG_OOS
    assert "WHERE o.has_stock AND NOT o.is_used" in q    # 二手不算卖得了
    assert "count(*) FILTER (WHERE o.is_used) AS used_obs" in q
    assert "oos_obs + wrong_ch_obs + used_obs > 0" in q  # 算一条明确的卖不了证据
    # 采不到 ⇒ NULL ⇒ coalesce 成 false ⇒ 算"卖得了",挡住删除(方向题)
    assert "false) AS is_used" in q


def test_long_oos_used_predicate_is_same_source_as_the_python_one():
    """SQL 谓词与 `used_offer()` **同源**:两处各写一份就是"清零按 A 判、
    删除按 B 判",而且两边都不报错。键名同理,唯一出处在 registry。"""
    q = mi._SQL_LONG_OOS
    assert f"raw ->> '{resources.AMZ_OFFER_CONDITION_KEY}'" in q
    assert f"NOT IN {mi._NOT_USED_CONDITIONS}" in q
    # 取数那条 SQL 也走同一个键(否则 classify 永远收到 None)
    assert f"raw ->> '{resources.AMZ_OFFER_CONDITION_KEY}' AS offer_condition" \
        in mi._SQL_AMZ_JOIN
    # 键名不许在 services/workflows 里写字面量(改名时会漏改,且闸恒放行)
    for path in ("services/maintenance_intents.py", "workflows/maintenance_scan.py"):
        src = pathlib.Path(path).read_text(encoding="utf-8")
        # 拦的是 **SQL 里的字面量键名**(单引号那种):改键名时漏改一处,
        # 闸恒放行且不报错。行 dict 的 `r["offer_condition"]` 是列名不是键名,
        # 不在此列 —— 列名由本仓的 SQL 自己起,与采集侧改不改名无关。
        assert "'offer_condition'" not in src, path


def test_used_delete_has_its_own_reason_code_and_outranks_channel():
    """二手走到头 ≠ 货源断了 ≠ 渠道不符:三个原因码分开,删除预览按码分组
    才说得清该找谁。优先序与 classify 一致(二手 > 渠道 > 缺货)。"""
    out = mi.delete_intents(
        _DelConn(oos=[("T1", "B0USED", 15, None, None, 0, "", 9),
                      ("T1", "B0BOTH", 15, None, None, 4, "FBA", 9),
                      ("T1", "B0WRONG", 15, None, None, 12, "FBA", 0),
                      ("T1", "B0DEAD", 15, None, None, 0, "", 0)]), [],
        oos_days=15)
    assert {i["sku"]: i["code"] for i in out} == {
        "B0USED": "二手15天", "B0BOTH": "二手15天",
        "B0WRONG": "渠道不符15天", "B0DEAD": "连续无货15天"}
    why = {i["sku"]: i["reason"] for i in out}
    assert "9 次确认为二手/翻新 offer" in why["B0USED"]


def test_scan_summary_names_the_used_rows():
    """摘要单列一行:补救动作与其余四档都不同(等 buybox 换回全新,或这个品
    根本不该继续跟卖),而且它会顺着 15 天窗口走到不可逆的删除。"""
    from workflows import maintenance_scan as ms
    zeroing = [{"store": "T1", "sku": "A", "code": "used_offer", "reason": "二手"},
               {"store": "T1", "sku": "B", "code": "used_offer", "reason": "二手"},
               {"store": "T2", "sku": "C", "code": "used_offer", "reason": "二手"},
               {"store": "T2", "sku": "D", "code": "out_of_stock", "reason": "缺货"}]
    lines = ms._used_lines(zeroing)
    assert "二手/翻新清零 3 行" in lines[0]
    assert "T1×2" in lines[0] and "T2×1" in lines[0]
    assert "15 天" in lines[-1]
    assert ms._used_lines([zeroing[3]]) == []      # 没有二手就不占版面


def test_delete_intents_actually_binds_the_store_channels(monkeypatch):
    """渠道闸最容易的死法:参数没绑,SQL 里 want 恒空,闸**永远不触发且不报错**。"""
    conn = _DelConn()
    mi.delete_intents(conn, [], store_channels={"T1": "FBA", "T2": "FBM"})
    args = [a for sql, a in conn.sqls if "ch_stores" in sql][0]
    assert args["ch_stores"] == ["T1", "T2"]
    # 两个数组按位对齐 —— 错位就是把 T1 的渠道要求安到 T2 头上
    assert dict(zip(args["ch_stores"], args["ch_wants"])) == {"T1": "FBA",
                                                              "T2": "FBM"}


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
    monkeypatch.setattr(mi, "delete_intents",
                        lambda c, rows, oos_days=0, store_channels=None,
                        only=None: [
                            {"store": "T1", "sku": "B0A", "kind": "delete",
                             "old": "在线", "new": "删除",
                             "code": "variant_offset"}])
    monkeypatch.setattr(mi.store_limits, "retire_caps", lambda: {})
    monkeypatch.setattr(mi.store_limits, "price_multipliers", lambda: {})
    monkeypatch.setattr(st, "store_channels", lambda: {"T1": "FBA"})
    monkeypatch.setattr(mi, "title_intents", lambda rows: [])
    monkeypatch.setattr(mi, "price_intents", lambda rows, m: [
        {"store": "T1", "sku": "B0A", "kind": "price", "old": 9.9, "new": 11.0},
        {"store": "T1", "sku": "B0B", "kind": "price", "old": 9.9, "new": 11.0}])
    monkeypatch.setattr(mi, "inventory_intents",
                        lambda rows, mn=None, store_channels=None,
                        stock_caps=None: [
                            {"store": "T1", "sku": "B0A", "kind": "inventory",
                             "old": 5, "new": 0}])
    monkeypatch.setattr(mi, "zero_intents",
                        lambda c, sz, mn=None, only=None: [])
    monkeypatch.setattr(mi, "match_inventory_intents",
                        lambda c, sz, mn=None, only=None: [])
    monkeypatch.setattr(mi, "drop_recent", lambda c, i: (i, 0))
    out, capped = mi.collect_all(conn, [])
    assert [(i["sku"], i["kind"]) for i in out] == [("B0A", "delete"),
                                                    ("B0B", "price")]
    assert capped == []          # 没超单店上限就一个组都不该进截断报告


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


# ── 单店单轮上限(cap_per_store,2026-08-26 按店化)──────────────────────────
# 08-25 生产实证的病根:全局 5000/类闸把 7 家店的**合法**倍率调整截成随机零头
# (SQL 无 ORDER BY,[:5000] 取的是物理序),截断只进日志,飞书摘要看起来一切
# 正常。按店化后配额才对得上沃尔玛的计法(全部按店),截谁也不再随机。


def _price_intent(store, sku, old, new):
    return {"store": store, "sku": sku, "kind": "price", "old": old, "new": new}


def test_cap_is_per_store_not_global():
    """一家店超限只截这一家;别家一个不少;全局总量允许超旧 5000 闸。"""
    cap = mi.MAX_INTENTS_PER_STORE["price"]
    big = [_price_intent("A", f"S{i}", 10.0, 12.0) for i in range(cap + 100)]
    small = [_price_intent("B", f"T{i}", 10.0, 15.0) for i in range(300)]
    out, report = mi.cap_per_store(big + small)
    n = {}
    for i in out:
        n[i["store"]] = n.get(i["store"], 0) + 1
    assert n == {"A": cap, "B": 300}
    assert len(out) > 5000                    # 全局旧闸已废
    assert len(report) == 1
    r = report[0]
    assert (r["store"], r["kind"], r["total"], r["kept"]) == \
        ("A", "price", cap + 100, cap)
    # 落榜行的键要给全:扫描件靠它把落榜行留在 withdraw keep 里
    assert len(r["deferred_keys"]) == 100
    assert all(k[0] == "A" and k[2] == "price" for k in r["deferred_keys"])


def test_price_truncation_keeps_the_biggest_mispricing(monkeypatch):
    """截谁不是随机的:偏差比例大的先走 —— 错得越离谱的价越该当天纠。"""
    monkeypatch.setitem(mi.MAX_INTENTS_PER_STORE, "price", 2)
    out, report = mi.cap_per_store([
        _price_intent("A", "S1", 10.0, 10.2),     # +2%
        _price_intent("A", "S2", 10.0, 15.0),     # +50%
        _price_intent("A", "S3", 10.0, 8.0),      # -20%(跌也按幅度算)
    ])
    assert {i["sku"] for i in out} == {"S2", "S3"}
    assert report[0]["total"] == 3 and report[0]["kept"] == 2
    assert report[0]["deferred_keys"] == [("A", "S1", "price")]


def test_title_truncation_keeps_mismatch_sync_first(monkeypatch):
    """标题也有截断优先级:停闸期低相似度同步(抄错标题嫌疑最大)先走;
    第二键 SKU —— 产出序是无 ORDER BY 的物理序,会随 VACUUM 漂移,
    截断名单必须跨轮可预期(08-25 "随机截"的一半病根)。"""
    monkeypatch.setitem(mi.MAX_INTENTS_PER_STORE, "title", 2)
    out, _ = mi.cap_per_store([
        {"store": "A", "sku": "T3", "kind": "title", "old": "a", "new": "b",
         "code": "title_sync"},
        {"store": "A", "sku": "T2", "kind": "title", "old": "a", "new": "b",
         "code": "title_mismatch_sync"},
        {"store": "A", "sku": "T1", "kind": "title", "old": "a", "new": "b",
         "code": "title_sync"},
    ])
    assert [i["sku"] for i in out] == ["T2", "T1"]


def test_inventory_truncation_keeps_zeroing_first(monkeypatch):
    """清零(new=0)优先于补货:「别卖错」压过「上量」;同档保持产出序。"""
    monkeypatch.setitem(mi.MAX_INTENTS_PER_STORE, "inventory", 2)
    out, _ = mi.cap_per_store([
        {"store": "A", "sku": "R1", "kind": "inventory", "old": 0, "new": 10},
        {"store": "A", "sku": "Z1", "kind": "inventory", "old": 5, "new": 0},
        {"store": "A", "sku": "Z2", "kind": "inventory", "old": 3, "new": 0},
    ])
    assert [i["sku"] for i in out] == ["Z1", "Z2"]


def test_delete_is_never_capped_at_scan():
    """破坏类数量闸唯一在执行件(2026-08-24 归一);扫描件如实报待办。"""
    dels = [{"store": "A", "sku": f"D{i}", "kind": "delete",
             "old": "在线", "new": "删除"} for i in range(9000)]
    out, report = mi.cap_per_store(dels)
    assert len(out) == 9000 and report == []


def test_store_caps_align_with_feed_quota_and_slice():
    """上限 =(按店速率桶 − 1)× 单 feed 切片条数,三处必须钉在一起。

    **−1 是补交余量**:api/feeds 的"双确认未达 → 同一载荷补交一次"每次
    多烧一个桶名额,切片数吃满桶时一次补交就会在令牌桶上睡到窗口滑出
    (≈1 小时),整条链抱着 flock 陪等。
    **窗口那一维也要钉**:把 (8,3600) 改成 (8,86400) 条数断言照样过,
    而单轮吞吐掉到 1/24、"×小时"的六处文档全部变假话。
    谁要改桶/切片/上限表任何一处,先改到全部同时成立。
    """
    from api import _client, feeds
    for kind, bucket, slice_key in (
            ("price", "feeds.post.price", "price"),
            ("inventory", "feeds.post.inventory", "inventory"),
            ("title", "feeds.post.MP_MAINTENANCE", "MP_MAINTENANCE")):
        n, window = _client._RATE_BUCKETS[bucket]
        assert window == 3600.0, bucket       # 单轮 = 一小时桶
        assert mi.MAX_INTENTS_PER_STORE[kind] == \
            (n - 1) * feeds._SLICE_LIMITS[slice_key][0], kind


# ── 扫描件(maintenance_scan)────────────────────────────────────────────────

def _scan_wire(monkeypatch, intents, sz=("T1",), capped=(), absent=()):
    from workflows import maintenance_scan as ms
    calls = {"suggest": [], "withdraw": [], "collect": []}
    monkeypatch.setattr(ms.store_limits, "stockzero_stores", lambda: list(sz))
    # 默认没有店配「维护仓库」= 现状;要钉受管仓的用例自己覆盖这一项
    monkeypatch.setattr(ms.store_limits, "managed_nodes",
                        lambda conn=None, stats=None: ({}, {}))
    monkeypatch.setattr(ms.store_absence, "stale_stores",
                        lambda conn, since=None, hours=None: list(absent))
    # ⚠ 桩**故意不理会 only**(照旧返回全部意图):workflow 里的 Python 侧过滤
    # 是下推之后的双保险,这个桩正好把它钉住 —— 桩若跟着过滤,那两行就没人验了
    monkeypatch.setattr(ms.mi, "collect_all",
                        lambda conn, s, oos=0, managed=None, only=None: (
                            calls["collect"].append(only),
                            list(intents), list(capped))[1:])
    _fake_db(monkeypatch, _Conn())
    monkeypatch.setattr(ms.dispositions, "suggest_many",
                        lambda conn, rows: (calls["suggest"].extend(rows),
                                            len(rows))[1])
    monkeypatch.setattr(ms.dispositions, "withdraw_stale",
                        lambda conn, src, keep, why, store=None,
                        exclude_stores=None: (
                            calls["withdraw"].append(
                                (src, keep, store, exclude_stores)), 0)[1])
    monkeypatch.setattr(ms.dispositions, "count_open",
                        lambda conn, sources=None: len(calls["suggest"]))
    monkeypatch.setattr(ms.dispositions, "count_suppressed",
                        lambda conn, actions=None: calls.get("suppressed", 0))
    return ms, calls


def test_scan_breaks_channel_clears_down_by_store():
    """1600 行清零只报一个总数没用:渠道不符的**补救动作是逐店的**。

    规划外店(谭总系)带旗标,但**照判不豁免**(所有者定稿 2026-08-25)。旗标的
    用途是提醒:这些行永远不会进 `alloc_audit` 的渠道不符下架清单(那条链在判
    渠道之前就剔了规划外店),两份报告对不上是预期的,不是漏报。
    """
    zeroing = ([{"store": "A085朱丽霖", "sku": f"B0A{i}", "kind": "inventory",
                 "new": 0, "code": "channel_mismatch"} for i in range(3)]
               + [{"store": "谭总6", "sku": f"B0T{i}", "kind": "inventory",
                   "new": 0, "code": "channel_mismatch"} for i in range(9)]
               + [{"store": "A085朱丽霖", "sku": "B0OOS", "kind": "inventory",
                   "new": 0, "code": "out_of_stock"}])
    from workflows import maintenance_scan as ms
    got = "\n".join(ms._channel_lines(zeroing))
    # 按条数降序,规划外店带旗标;缺货那条不算进来(它的处置对象是商品不是店)
    assert "渠道不符清零 12 行" in got
    assert "谭总6×9⚑" in got and "A085朱丽霖×3" in got
    assert "A085朱丽霖×3⚑" not in got          # 在册店不许被标成规划外
    assert "规划外店,共 9 行" in got
    # 旗标记的是**定稿**(所有者 2026-08-25),不是待办 —— 不许退回"要对齐
    # 就把这一闸也对规划外店豁免"那种悬案文案:悬案会被人当成"这里还没做完"
    assert "不豁免" in got
    assert "要对齐" not in got
    # 一行渠道不符都没有时不占版面(排版规范规矩 2:只报真的发生了的)
    assert ms._channel_lines([z for z in zeroing
                              if z["code"] != "channel_mismatch"]) == []


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


def test_scan_says_the_pause_only_stops_deletion(monkeypatch):
    """停闸摘要要说清"只停删除",并把低相似度改标题的条数单独摊开。

    所有者 2026-08-20:「删除(title_mismatch)已停闸,那么就要对这批行同时
    改价改标题改库存」。改的是"可能不是同一个商品"的标题 —— 混在"标题 N"
    里等于没说,人眼闸门看不到条数就没法判断今天要不要拦。
    """
    assert mi.TITLE_MISMATCH_DELETE is False        # 停闸是当前现状
    intents = [{"store": "T1", "sku": "B0M", "kind": "title", "old": "A",
                "new": "B", "code": "title_mismatch_sync"},
               {"store": "T1", "sku": "B0N", "kind": "title", "old": "A",
                "new": "B", "code": "title_sync"}]
    ms, _calls = _scan_wire(monkeypatch, intents)
    out = ms.run({})
    assert "只停删除" in out and "照常改价/改标题/改库存" in out
    assert "低相似度改标题 1 条" in out             # 另一条是常规同步,不算


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
    src, keep, store, _excl = calls["withdraw"][0]
    assert src == "maint" and store == "T1"
    assert keep == [("T1", "A", "inventory")]       # 只保留本店本轮的
    assert [r["store"] for r in calls["suggest"]] == ["T1"]


def test_scan_preview_writes_nothing(monkeypatch):
    ms, calls = _scan_wire(monkeypatch, [
        {"store": "T1", "sku": "A", "kind": "price", "old": 9.9, "new": 11.0}])
    out = ms.run({"preview": "1"})
    assert calls["suggest"] == [] and calls["withdraw"] == []
    assert "preview" in out and "将写 1 条" in out


def test_scan_surfaces_truncation_in_summary_first_line(monkeypatch):
    """截断必须进摘要**首行**,不许只进日志或第 2 行。

    08-25 生产实证:全局闸截掉 7,766 条改价只写了一行日志 warning,飞书通知
    报的全是截断后的数,看起来一切正常。而且唯一调度路径(product_chain 链)
    对成功步骤**只发首行**(cli first_line_of)—— 截断放第 2 行等于只写日志
    (对抗校验 2026-08-26 实跑证实)。逐店明细留在后续行。
    """
    ms, _calls = _scan_wire(monkeypatch, [], capped=[
        {"store": "谭总12", "kind": "price", "total": 8000, "kept": 6000}])
    out = ms.run({"preview": "1"})
    first = out.splitlines()[0]
    assert "⚠ 截断 1 组共 2000 条顺延" in first
    assert "谭总12" in out and "8000→6000" in out


def test_scan_first_line_carries_zeroing_count(monkeypatch):
    """清零规模也必须在首行:全局刹车废除后它是整店误清零的唯一人眼防线,
    而链通知只发成功步骤的首行 —— 放 _preview_lines 里等于没人看见。"""
    ms, _calls = _scan_wire(monkeypatch, [
        {"store": "T1", "sku": "A", "kind": "inventory", "old": 3, "new": 0,
         "code": "out_of_stock", "reason": "亚马逊缺货"},
        {"store": "T1", "sku": "B", "kind": "inventory", "old": 0, "new": 7,
         "code": "match_restock", "reason": "跟卖铺货"}])
    out = ms.run({"preview": "1"})
    assert "清零 1" in out.splitlines()[0]     # 补货那条不算清零


def test_scan_surfaces_store_cap_in_first_line_and_preview(monkeypatch):
    """「最大库存」封顶的条数要进**首行**(链通知只发首行),预览里按店摊开 ——
    非 0 的改库存在别处只有逐店总数,不单列就看不出上限生效了多少。"""
    ms, _calls = _scan_wire(monkeypatch, [
        {"store": "T1", "sku": "A", "kind": "inventory", "old": 50, "new": 3,
         "code": store_limits.QTY_CAPPED, "reason": "按本店最大库存 3 写(亚马逊 50)"},
        {"store": "T1", "sku": "B", "kind": "inventory", "old": 4, "new": 0,
         "code": store_limits.QTY_BELOW_MIN, "reason": "亚马逊库存 4 低于门槛 5"}])
    out = ms.run({"preview": "1"})
    first = out.splitlines()[0]
    assert "库存 2(清零 1,封顶 1;stockzero 店" in first
    assert "按本店最大库存封顶 1 条:T1×1" in out
    assert "清零合计 1 条,原因:below_min×1" in out
    # 没有封顶时首行一个字不变(排版规范:只报真的发生了的)
    ms, _calls = _scan_wire(monkeypatch, [
        {"store": "T1", "sku": "B", "kind": "inventory", "old": 4, "new": 0,
         "code": store_limits.QTY_BELOW_MIN}])
    assert "(清零 1;stockzero 店" in ms.run({"preview": "1"}).splitlines()[0]


def test_scan_store_filter_scopes_the_truncation_report_too(monkeypatch):
    """-p store=X 那一轮,别家的截断行不该混进本店摘要。

    ⚠ 店名别用 stockzero 名单里的:_scan_wire 缺省 sz=("T1",) 会让
    「stockzero 名单:T1」恒出现,断言 'T1' in out 就是空断言(变异实测:
    把 store 过滤整个删掉照样绿)。断言只认截断行才会出现的片段。
    """
    ms, _calls = _scan_wire(monkeypatch, [], sz=(), capped=[
        {"store": "甲店", "kind": "price", "total": 7000, "kept": 6000},
        {"store": "乙店", "kind": "price", "total": 9000, "kept": 6000}])
    out = ms.run({"preview": "1", "store": "甲店"})
    assert "7000→6000" in out and "9000→6000" not in out


def test_scan_avoids_absent_stores_and_shields_their_rows(monkeypatch):
    """缺席避让(店级重试标准③):catalog_sync 补试后仍缺席的店,本轮
    ①不产任何意图(拿陈旧现值算差异会误伤 —— 38 条 not found 的老账);
    ②首行点名(链通知只发首行);③它挂着的 suggested 行不许被撤
    (缺席 ≠ 恢复正常,withdraw 走 exclude_stores)。"""
    intents = [
        {"store": "缺席店", "sku": "A", "kind": "price", "old": 9.9, "new": 11.0},
        {"store": "正常店", "sku": "B", "kind": "price", "old": 9.9, "new": 11.0}]
    ms, calls = _scan_wire(monkeypatch, intents, absent=["缺席店"])
    out = ms.run({})
    first = out.splitlines()[0]
    assert "⚠ 缺席避让 1 店:缺席店" in first and "1 条意图不产出" in first
    assert [r["store"] for r in calls["suggest"]] == ["正常店"]   # ①
    _src, keep, _store, excl = calls["withdraw"][0]
    assert keep == [("正常店", "B", "price")]
    assert excl == ["缺席店"]                                     # ③


def test_scan_keeps_deferred_rows_out_of_withdraw(monkeypatch):
    """被配额截掉 ≠ 不再建议:落榜行的旧 suggested 行不许被 withdraw_stale
    撤成「商品自己恢复正常了」—— 错误取证,且下轮扫描又原样重建。"""
    intents = [{"store": "T1", "sku": "A", "kind": "price",
                "old": 9.9, "new": 11.0}]
    ms, calls = _scan_wire(monkeypatch, intents, capped=[
        {"store": "T1", "kind": "price", "total": 3, "kept": 1,
         "deferred_keys": [("T1", "B", "price"), ("T1", "C", "price")]}])
    ms.run({})
    _src, keep, _store, _excl = calls["withdraw"][0]
    assert ("T1", "A", "price") in keep          # 入选的照常在
    assert ("T1", "B", "price") in keep and ("T1", "C", "price") in keep


# ── 执行件(maintenance)────────────────────────────────────────────────────

def _disp(it, i):
    """意图 → claim() 会返回的建议行形态(带 id)。"""
    return {"id": 100 + i, **mi.to_disposition(it)}


def _wire(monkeypatch, intents, stores=(STORE,), absent=()):
    calls = {"put_inv": [], "put_price": [], "feeds": [], "sheet": [],
             "marked": [], "marked_by": set(), "settled": 0,
             "suppressed": 0}
    _fake_db(monkeypatch, _Conn())
    # 打 stale_stores 这一层:执行件调的 stale_or_note 是它的降级外壳,
    # 探测正常时 note 为空串(降级那条路由 store_absence 自己的用例钉)
    monkeypatch.setattr(mw.store_absence, "stale_stores",
                        lambda conn, **k: list(absent))
    # 失败店串行补试每店前有 _client.backoff(0) 抖动等待:用例只钉行为,
    # 不必真等(与 tests/test_store_retry_standard 同款)
    monkeypatch.setattr(mw.store_retry.time, "sleep", lambda s: None)
    monkeypatch.setattr(mw.dispositions, "claim",
                        lambda conn, actions=None: [_disp(it, i)
                                                    for i, it in enumerate(intents)])
    monkeypatch.setattr(mw.dispositions, "settle",
                        lambda conn: (_ for _ in ()).throw(
                            AssertionError("maintenance 不该落定破坏类"
                                           "——归 problem_product_cleanup")))
    monkeypatch.setattr(mw.dispositions, "settle_maintenance",
                        lambda conn: (calls.__setitem__("settled",
                                                        calls["settled"] + 1),
                                      {"confirmed": 0, "ineffective": 0})[1])
    monkeypatch.setattr(mw.dispositions, "expire_executing", lambda conn: 0)
    monkeypatch.setattr(mw.dispositions, "count_suppressed",
                        lambda conn, actions=None: calls["suppressed"])
    monkeypatch.setattr(mw.dispositions, "mark_executing",
                        lambda conn, ids, fid, by="": (
                            calls["marked"].append((list(ids), fid)),
                            calls["marked_by"].add(by))[0])
    monkeypatch.setattr(mi, "record_submitted", lambda conn, items: len(items))
    monkeypatch.setattr(mw.stores_svc, "load_stores",
                        lambda names=None: list(stores))
    # 默认没有店配「维护仓库」= 现状;要钉第二道闸的用例自己覆盖这一项
    monkeypatch.setattr(mw.store_limits, "maint_nodes", lambda: {})
    monkeypatch.setattr(inv_api, "put_inventory",
                        lambda store, sku, qty, node=None: (
                            calls["put_inv"].append(
                                (store["name"], sku, qty)
                                + ((node,) if node else ())),
                            (True, ""))[1])
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
    # 炸掉那条也要留痕:动作空、结果写明为什么(否则表里完全看不见)。
    # 两轮都炸也**只写一行**:补试重跑同店时首轮写过的「未执行」不再写第二遍
    t1 = [r for r in calls["sheet"] if r[0] == "T1"]
    assert len(t1) == 1 and t1[0][_c("action")] == ""
    assert t1[0][_c("result")] == "未执行(提交异常)"


def test_failed_store_is_retried_serially_once(monkeypatch):
    """店级重试标准①(所有者定稿 2026-08-26):跨店跑完之后失败店**串行补试
    一遍**。所有者原话「某个店当时有问题,最后补一次」—— 此前本文件把这句
    写在并发段的注释里,补一次的代码却没有(只 diagnose 一句「下轮重试」)。

    补试跑的是第一轮同一个 _one_store(单一落地路径):重提由 feeds 的
    payload_key 在途防重看护,首轮已发出去的片不会重复提交。
    """
    calls = _wire(monkeypatch, _zero(11))       # >10 → 走 feed
    seen = {"n": 0}

    def flaky_then_ok(store, ft, entries, *, workflow=""):
        seen["n"] += 1
        if seen["n"] == 1:
            raise ConnectionError("proxy down")
        calls["feeds"].append((store["name"], ft, len(entries)))
        return [{"feed_id": "F2", "count": len(entries), "outcome": "submitted"}]

    monkeypatch.setattr(feeds, "submit_feed", flaky_then_ok)
    out = mw.run({"execute": True})
    assert seen["n"] == 2                       # 补一次即止,不是无限重试
    assert calls["feeds"] == [("T1", "inventory", 11)]
    assert calls["marked"] == [(list(range(100, 111)), "F2")]
    assert "缺席" not in out                     # 救回来了就不点名
    # 首轮的「未执行」行既不撤也不重写,补试成功的另起一行:
    # 两行连起来正是这家店这一轮的经过
    got = [(r[_c("action")], r[_c("result")]) for r in calls["sheet"]]
    assert got.count(("", "未执行(提交异常)")) == 11
    assert got.count(("库存", "处理中")) == 11
    # 补试必须见人:摘要行每轮重置,首轮那句「⚠ 提交异常已跳过」被覆盖了,
    # 而上面那 11 行「未执行(提交异常)」还留在维护记录表上 —— 摘要不说
    # 一句,表上那几行就成了没出处的孤证(§六:兜底触发不许静默)
    assert "店级补试 1 店(串行):T1,救回 1,仍失败 0" in out


def test_put_route_second_pass_reuses_the_same_absolute_write(monkeypatch):
    """小批量走 PUT 路由,**不进 feed 台账、没有在途防重**(api/prices 与
    api/inventory 头注:「不产生 feed_id 不进 feed 台账」)—— 补试因此会把
    首轮已成功的那几条再赋值一遍。

    这仍然安全,但依据与 feed 路由**不是同一条**:两个端点都是绝对赋值
    (quantity.amount / currentPrice.amount),重设同一个值终态不变;
    CLAUDE.md「写操作永不自动兜底」禁的是*换方法*重试,补试走的还是这条路由。
    钉住两件事:① 补试不许改走 feed(换方法 = 重复提交制造机);
    ② 重发的值必须与首轮一模一样(绝对赋值,不是增量)。
    """
    calls = _wire(monkeypatch, _zero(3))         # ≤10 → 走 PUT
    seen = {"n": 0}

    def flaky(store, sku, qty, node=None):
        seen["n"] += 1
        calls["put_inv"].append((store["name"], sku, qty))
        if sku == "S2" and seen["n"] <= 3:       # 首轮最后一条炸
            raise ConnectionError("proxy down")
        return True, ""

    monkeypatch.setattr(inv_api, "put_inventory", flaky)
    out = mw.run({"execute": True})
    assert calls["put_inv"] == [("T1", "S0", 0), ("T1", "S1", 0), ("T1", "S2", 0),
                                ("T1", "S0", 0), ("T1", "S1", 0), ("T1", "S2", 0)]
    assert calls["feeds"] == []                  # ① 没有偷偷改走 feed
    assert "店级补试 1 店(串行):T1,救回 1,仍失败 0" in out
    assert "缺席" not in out


def test_store_that_fails_twice_is_named_in_the_first_line(monkeypatch):
    """标准②③:补试仍失败**不炸整轮**,但必须点名在摘要**首行** ——
    链通知对成功步骤只发首行(cli.first_line_of),写在后面等于只写进日志。
    归类词唯一出处 store_retry.diagnose;尾句是本件的处置(领取只读,
    没提交出去的建议行还是 suggested,下轮照样领得到)。"""
    calls = _wire(monkeypatch, _zero(11))
    monkeypatch.setattr(feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ConnectionError("proxy down")))
    out = mw.run({"execute": True})
    first = out.splitlines()[0]
    assert "⚠ 缺席 1 店:T1(其他)" in first
    assert "已串行补试仍失败" in first
    assert "本轮不炸链(存量建议保留,下轮重试)" in first
    assert calls["marked"] == []                # 一条都没提交出去


def test_credential_death_is_never_retried(monkeypatch):
    """凭证失效不补试(标准①):凭证死是确定性的,重试只会再死一次。
    它也不进缺席点名 —— 那是「今天没轮到」,凭证死是「去修凭证表」。"""
    from api import _client as client_api
    calls = _wire(monkeypatch, _zero(11))
    seen = {"n": 0}

    def dead(store, ft, entries, *, workflow=""):
        seen["n"] += 1
        raise client_api.StoreDeadError("T1", 401)

    monkeypatch.setattr(feeds, "submit_feed", dead)
    out = mw.run({"execute": True})
    assert seen["n"] == 1                       # 只试一次
    assert "T1:凭证失效跳过" in out and "缺席" not in out
    assert all(r[_c("result")] == "未执行(凭证失效)" for r in calls["sheet"])


def test_maintenance_no_longer_deletes_anything(monkeypatch):
    """删除的唯一出口是 problem_product_cleanup(所有者定稿 2026-08-24)。

    两个执行件都能发 DELETE_ITEM 的时候,配额、在途防重、病历口径各有一套,
    同一个 SKU 被两条链先后删两次是生产实证过的。这里钉三件事:
      ① 维护链的领取集不含 delete/retire;
      ② 真有一条删除行混进来(领取集写错),分桶直接抛,不会静默删掉;
      ③ 本文件不再产出任何 DELETE_ITEM 载荷。
    """
    import inspect

    import pytest

    from services import dispositions as ds

    assert set(mw._KIND_ORDER) == set(ds.MAINT_ACTIONS)
    assert "delete" not in mw._KIND_ORDER and "retire" not in mw._KIND_ORDER
    # ② 分桶件 2026-08-27 上移 services.dispositions:按本执行件的接线
    #   (key='kind' × _KIND_ORDER)喂一条删除行,照样宁炸不吞
    assert 'key="kind", order=_KIND_ORDER' in inspect.getsource(mw.run)
    with pytest.raises(ValueError, match="未知 kind"):
        ds.group_by_store([{"store": "T1", "sku": "B0A", "kind": "delete"}],
                          key="kind", order=mw._KIND_ORDER,
                          id_field="disposition_id")
    assert "DELETE_ITEM" not in inspect.getsource(mw._submit_kind)


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
    asked = []
    monkeypatch.setattr(
        maint_sheet.feishu, "sheet_values_rows",
        lambda sheet, c1, c2, rf, rt, **kw: (
            asked.append((c1, c2, rf, rt)),
            [(2, _sheet_row("T1", "B0A", "价格", "", "", "F1",
                            "2026-08-09", "成功"))])[1])
    appended = []
    monkeypatch.setattr(maint_sheet, "append_records",
                        lambda rows: (appended.extend(rows), len(rows))[1])
    out = maint_sheet.resync_from_ledger()
    # 存量识别走标准读通道,读的是整行宽(_span,不写死字母)的 [2, next_row) 区间
    assert asked == [(*maint_sheet._span(), 2, 2)]
    assert "补写 1 行" in out
    assert appended[0][_c("sku")] == "B0B" and appended[0][_c("action")] == "库存"
    assert appended[0][_c("feed_id")] == "F2"
    assert appended[0][_c("result")] == "失败"
    # 旧值/新值补不回来(PG 只记 SKU 级状态,不存当时的新旧值)
    assert appended[0][_c("old_value")] == "" and appended[0][_c("new_value")] == ""


def test_no_sheet_side_clock_old_rows_wait_for_the_ledger(monkeypatch):
    """表侧 3 天时钟已删(所有者 2026-09-25):feed 按落定期限收口,台账每一行都会
    在期限内拿到有依据的终态;本表只转述,不自己编「未查到」。老行台账已落「超期
    未完成」⇒ 写这个词;台账仍 submitted ⇒ 照等,水位停在它身上。"""
    from registry.resources import Spreadsheet
    monkeypatch.setattr(resources, "MAINT_SHEET",
                        Spreadsheet(name="维护记录", token="TOK", sheet_id="SID",
                                    columns=resources.MAINT_SHEET.columns))
    conn = _Conn()
    conn.cursor_value = {"next_row": 5, "unresolved_from": 2}
    _fake_db(monkeypatch, conn)
    monkeypatch.setattr(maint_sheet, "_today", lambda: _date(2026, 8, 9))
    monkeypatch.setattr(
        maint_sheet.feishu, "sheet_values_rows",
        lambda sheet, c1, c2, rf, rt, **kw: [
            (2, _sheet_row("T1", "B0OLD", "价格", "", "", "F1",
                           "2026-08-01", "处理中")),
            (3, _sheet_row("T1", "B0WAIT", "价格", "", "", "F2",
                           "2026-08-01", "处理中")),
            (4, _sheet_row("T1", "B0NEW", "价格", "", "", "F3",
                           "2026-08-09", "处理中")),
        ])
    ledger = {"F1": {"B0OLD": ("overdue", "")},
              "F2": {"B0WAIT": ("submitted", "")},
              "F3": {"B0NEW": ("success", "")}}
    monkeypatch.setattr(feed_track, "item_results", lambda fid: ledger[fid])
    monkeypatch.setattr(feed_track, "item_errors", lambda fid: {})
    written = []
    monkeypatch.setattr(maint_sheet.feishu, "sheet_write_ranges",
                        lambda sheet, ups: (written.extend(ups), len(ups))[1])
    out = maint_sheet.sync_from_ledger()
    assert "未查到" not in out and not hasattr(maint_sheet, "STALE_DAYS")
    texts = [vals[0][0] for _rng, vals in written]
    assert texts == ["超期未完成", "成功"]          # 转述台账原词,不编结论
    # 第 3 行台账仍在途:水位停在它身上(不再被 3 天时钟推过去)
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
    grid = [
        ["T1", "B0OLD", "价格", "涨价", "价格", "10", "12.5", "F1",
         "2026-07-01", "成功", ""],
        ["T1", "B0KEEP", "价格", "涨价", "价格", "10", "12.5", "F2",
         "2026-08-08", "成功", ""],
        ["T1", "B0NODATE", "价格", "", "价格", "", "", "F3", "", "成功", ""],
        # 回归钉:**新值**长得像个老日期,而**日期**列是近期的。
        # 读错列的那版会拿新值当日期,把这行当"早于保留期"删掉。
        ["T1", "B0TRAP", "标题", "标题不符", "标题", "旧标题", "2026-07-01",
         "F4", "2026-08-08", "成功", ""],
    ]
    asked = []
    monkeypatch.setattr(
        maint_sheet.feishu, "sheet_values_rows",
        lambda sheet, c1, c2, rf, rt, **kw: (
            asked.append((c1, c2, rf, rt)),
            list(enumerate(grid, start=rf)))[1])
    rewritten = []
    monkeypatch.setattr(maint_sheet.feishu, "sheet_overwrite",
                        lambda sheet, rows: (rewritten.extend(rows), len(rows))[1])
    out = maint_sheet.prune(7)
    # 裁剪的整表读也走标准读通道:整行宽(_span,不写死字母)× [2, next_row) 区间。
    # 从第 1 行读起会把表头当数据行重写进去,少读一列会静默截掉结果/报错
    assert asked == [(*maint_sheet._span(), 2, 5)]
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
    # 日期动态生成:裁剪按 RETAIN_DAYS 的墙钟判(2026-08-11 实爆:写死的日期
    # 过几天就悄悄变成在测另一条分支;表侧 3 天时钟 2026-09-25 已删)。
    today = maint_sheet._today().isoformat()
    sheet_rows = [
        _sheet_row("T1", "S1", "库存", "5", "0", "F1", today, "处理中"),
        _sheet_row("T1", "S2", "库存", "3", "0", "F1", today, "处理中"),
        _sheet_row("T1", "S3", "价格", "9", "8", "sync", today, "成功"),
        _sheet_row("T1", "S4", "库存", "7", "0", "F2", today, "处理中"),
    ]
    writes = []
    monkeypatch.setattr(feishu, "sheet_values_rows",
                        lambda s, c1, c2, rf, rt, **kw:
                        list(enumerate(sheet_rows, start=rf)))
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


def test_big_backlog_is_read_in_blocks_and_cleared_in_one_round(monkeypatch):
    """积压再大也**当轮清完**:分块读 + 行号对齐 + 水位一次推到底。

    2026-08-27 生产事故:反哺器把 [unresolved_from, next_row) 整段一把裸读,
    表长起来后撞飞书单响应 10MB 上限(90221 data exceeded)—— 读一次炸一次,
    水位一步不推,积压每轮重读、越读越大,**自己好不了**。换标准读通道
    (feishu.sheet_values_rows:行方向分块 + 90221 对半兜底)之后,一次
    feed_poll 就必须把整段扫完、全部回填、水位推到区间末尾;这里刻意跑真
    通道(只桩掉最底下的 _values_raw),分块循环本身也在射程内。

    ⚠ 顺带钉行号:飞书只裁**范围尾部**的空行,中段空行仍占位。分块之后
    每块各自被裁一次,`区间起点 + enumerate` 那种手算会从被裁的那一块起
    整段错位 —— 回填写到别人的行上,而且两边都不报错。
    """
    monkeypatch.setattr(resources, "MAINT_SHEET",
                        Spreadsheet(name="维护记录", token="TOK", sheet_id="SID",
                                    columns=resources.MAINT_SHEET.columns))
    lo, hi = 2, 50_002                  # 5 万行级积压(一天几千行,攒几天就这个量)
    conn = _Conn()
    conn.cursor_value = {"next_row": hi, "unresolved_from": lo}
    _fake_db(monkeypatch, conn)
    today = maint_sheet._today().isoformat()
    block = feishu._SHEET_READ_BLOCK_ROWS
    holes = {lo + block - 2, lo + block - 1}    # 首块最后两行是空的,会被飞书裁掉
    first_after_hole = lo + block               # 第二块的首行:错位就写到 holes 上
    rows = {r: _sheet_row("T1", f"S{r}", "库存", "5", "0", "F1", today, "处理中")
            for r in range(lo, hi) if r not in holes}

    asked = []

    def fake_raw(sheet, rng):
        head, tail = rng.split(":")
        rf, rt = int(head[1:]), int(tail[1:])
        asked.append((rf, rt))
        got = [rows.get(r, []) for r in range(rf, rt + 1)]
        while got and not got[-1]:      # 只裁范围尾部;中段空行返回空列表占位
            got.pop()
        return got

    monkeypatch.setattr(feishu, "_values_raw", fake_raw)
    writes = []
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda s, ups: (writes.extend(ups), len(ups))[1])
    # 错位那版会把第二块首行的结果写到 holes 里的空行上,所以它必须可分辨
    results = {f"S{r}": ("success", "") for r in rows}
    results[f"S{first_after_hole}"] = ("failed", "ERR_X")
    monkeypatch.setattr(feed_track, "item_results", lambda fid: results)
    monkeypatch.setattr(feed_track, "item_errors", lambda fid: {})

    out = maint_sheet.sync_from_ledger()

    # ① 整段在**这一次调用里**读完:按 4750 行/块切,块首块尾都对得上
    assert len(asked) == -(-(hi - lo) // block)
    assert asked[0] == (lo, lo + block - 1) and asked[-1][1] == hi - 1
    assert asked == sorted(asked)               # 没有回头重读,没有漏块
    # ② 行号对齐:被裁掉的两行没人写,第二块首行落在自己的行上
    _R, _E = maint_sheet._col("result"), maint_sheet._col("error")
    w = {rng: vals[0] for rng, vals in writes}
    assert w[f"{_R}{first_after_hole}:{_E}{first_after_hole}"] == ["失败", "ERR_X"]
    assert all(f"{_R}{r}:{_E}{r}" not in w for r in holes)
    assert w[f"{_R}{hi - 1}:{_E}{hi - 1}"] == ["成功", ""]     # 末块末行也回填了
    # ③ 一轮清完:积压全部回填,水位推到区间末尾,不留给下一轮
    assert len(writes) == (hi - lo) - len(holes)
    assert f"维护记录回填 {(hi - lo) - len(holes)} 行(扫描区间 {lo}~{hi - 1})" == out
    saved = [a for s, a in conn.sqls if "INSERT INTO ops.cursors" in s]
    assert saved and f'"unresolved_from": {hi}' in saved[-1][1]


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
    out = {i["sku"]: i["new"] for i in mi.price_intents(rows, _MULTS)}
    assert out == {"B0SHIP": 50.0, "B0FREE": 40.0}


def test_match_inventory_intents_fills_zero_stock():
    """跟卖品铺货(所有者批复 2026-08-12):唯一给 source_type='match' 行
    补库存的路径;stockzero 店排除(解除后自动回补,修清零/回补不对称)。"""
    # 列序 = _SQL_MATCH_INV 的 SELECT:(store, sku, avail_qty, node_qty)
    conn = _Conn(rows=[("T1", "PHUMWMT001", 0, None),
                       ("T2", "PHUMWMT002", None, None),
                       ("T3", "PHUMWMT003", 10, None)])     # 已有货 → 不铺
    out = mi.match_inventory_intents(conn, ["Z店"])
    assert [(i["store"], i["sku"], i["kind"], i["new"]) for i in out] == [
        ("T1", "PHUMWMT001", "inventory", mi.MATCH_INVENTORY_QTY),
        ("T2", "PHUMWMT002", "inventory", mi.MATCH_INVENTORY_QTY)]
    sql, args = conn.sqls[0]
    assert "source_type = 'match'" in sql       # 路由铁律:只碰跟卖出身
    assert "missing_since IS NULL" in sql       # 只补在架行
    assert args["stores"] == ["Z店"]            # stockzero 店整店排除


def test_match_inventory_looks_at_the_managed_node():
    """⚠ 多仓下"库存为 0"要按受管仓判:受管仓 0 而别的节点有货时,合计非 0
    会让**该铺的行选不出来、永远不铺**,而且完全静默(P3 假阴性)。"""
    conn = _Conn(rows=[("T1", "M1", 8, 0),      # 合计 8、受管仓 0 → 要铺
                       ("T1", "M2", 8, 3)])     # 受管仓有货 → 不铺
    out = mi.match_inventory_intents(conn, [], managed={"T1": "FC9"})
    assert [(i["sku"], i["ship_node"]) for i in out] == [("M1", "FC9")]


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

    # 末列 node_qty:受管仓那一行的现值(NULL = 未配置 / 本轮没扫到)
    conn = _SettleConn([
        (1, "price", {"new": 30.0}, 30.0, None, None, None),   # 改过来了
        (2, "price", {"new": 30.0}, 20.0, None, None, None),   # 线上还是旧价
        (3, "inventory", {"new": 0}, None, 0, None, None),     # 清零到位
        (4, "inventory", {"new": 0}, None, 10, None, None),    # 库存没动
        (5, "title", {"new": "新标题"}, None, None, "新标题", None),
        (6, "title", {"new": "新标题"}, None, None, "旧标题", None),
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


def test_settle_maintenance_judges_managed_node_by_node_qty():
    """受管仓的库存建议按**该节点**的现值判,不按全店合计(多仓批次 2)。

    ⚠ 拿 `w.avail_qty`(全节点合计)跟单仓目标比,多节点店永远判 ineffective
    ⇒ 下轮重新建议、再发一遍,循环不会自己停(多仓故障清单 P1)。
    """
    from services import dispositions as ds

    conn = _SettleConn([
        # 合计 10(另一个节点还有货),受管仓已清零 → **生效**
        (1, "inventory", {"new": 0, "ship_node": "N1"}, None, 10, None, 0),
        # 合计 0 但受管仓还是 10 → **未生效**(只看合计会判反)
        (2, "inventory", {"new": 0, "ship_node": "N1"}, None, 0, None, 10),
        # 受管仓的行,节点明细本轮没重新扫到 → 保持 executing,**不落判**
        (3, "inventory", {"new": 0, "ship_node": "N1"}, None, 0, None, None),
    ])
    assert ds.settle_maintenance(conn) == {"confirmed": 1, "ineffective": 1}
    assert conn.updates == [("confirmed", [1]), ("ineffective", [2])]
    # 新鲜度写在 JOIN 里:没重新扫到的节点明细取不出来,而不是取出旧值来判
    assert "ni.seen_at > d.executed_at" in ds._MAINT_OPEN_SQL


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


def test_two_chains_claim_disjoint_actions():
    """⚠ 两条链共用一张建议表,按**动作**分工领取(2026-08-24 改)。

    旧口径按 source 领。后果 08-19 生产实见:一条 (店铺,SKU,delete) 被维护链
    先建议(source='maint')、审核链后覆写 reason,那行仍归维护链执行 —— 表里
    写着维护链的「建议」、问题链的「原因」,谁也说不清是哪条链干的。
    source 回答"为什么建议",action 回答"该谁干",不能互相顶替。
    """
    import inspect

    from services import dispositions as ds
    from workflows import problem_product_cleanup as ppc

    assert set(ds.PROBLEM_ACTIONS) & set(ds.MAINT_ACTIONS) == set()
    assert set(ds.PROBLEM_ACTIONS) | set(ds.MAINT_ACTIONS) == set(ds.ACTIONS)
    assert "dispositions.PROBLEM_ACTIONS" in inspect.getsource(ppc.run)
    assert "dispositions.MAINT_ACTIONS" in inspect.getsource(mw.run)
    # 破坏动作只有一个出口:maintenance 不再发任何删除 feed
    assert "delete" in ds.PROBLEM_ACTIONS and "delete" not in ds.MAINT_ACTIONS
    assert "DELETE_ITEM" not in inspect.getsource(mw._submit_kind)
    # 维护链的动作若落进问题商品链的分桶,那边会直接抛(宁炸不吞)。
    # 分桶件 2026-08-27 上移 services.dispositions:按问题链的接线喂
    # (key='action' × _ACTION_ORDER),判据一字未改
    import pytest
    assert 'key="action"' in inspect.getsource(ppc.run)
    with pytest.raises(ValueError, match="未知 action"):
        ds.group_by_store([{"id": 1, "store": "T1", "sku": "S",
                            "action": "price"}],
                          key="action", order=ppc._ACTION_ORDER, id_field="id")


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


def test_stuck_executing_is_reported_by_both_scans():
    """卡在 executing 的建议必须被**两个**扫描件报出来。

    部分唯一索引挡的是 (店铺,SKU,动作),所以一条 executing 会让那个组合
    **建不出新建议**;它靠观测落定,而观测全来自 catalog_sync —— 店铺不被扫
    (凭证坏掉的店正是如此),观测就永远不来。此前这完全静默:扫描件照常报
    「建议 N 条」,看不出少了谁。

    ⚠ 尤其问题商品链:它**没有** expire_executing 那道兜底(删除/反补靠观测
    判定,粗暴时限会抢先判掉真正在途的删除),卡住就是一直卡着。
    """
    import inspect

    from services import dispositions as ds
    from workflows import maintenance_scan as ms
    from workflows import problem_scan as ps

    q = ds._STUCK_SQL
    assert "status = 'executing'" in q
    assert "executed_at < now() - make_interval(days => %(days)s::int)" in q
    assert "source = ANY(%(sources)s::text[])" in q      # 各查各的链
    for mod in (ms, ps):
        src = inspect.getsource(mod.run)
        assert "stuck_executing" in src and "stuck_note" in src, mod.__name__


def test_stuck_note_says_why_and_is_empty_when_clean():
    from services import dispositions as ds

    assert ds.stuck_note([]) == ""
    note = ds.stuck_note([{"store": "谭总10", "action": "delete", "n": 3,
                           "oldest": None}], days=3)
    assert "谭总10×3" in note
    assert "建不出新建议" in note        # 后果说清楚,不只报个数
    assert "catalog_sync" in note        # 指向成因,人知道该去查什么


def test_summary_does_not_call_executed_rows_pending(monkeypatch):
    """真跑完不许再说「待执行建议」—— 那些行此刻已经是 executing 了。

    所有者 2026-08-17 实见:通知开头 `✅ [EXECUTE] maintenance 成功`,正文却是
    「待执行建议 8257 条」,读起来像"什么都没干"。这一行在**提交之前**生成,
    dry-run 说「待执行」是对的,真跑就是谎话。

    也不能改口叫「已执行」:领取的行里有一部分会撞上单店上限/在途防重/凭证
    缺失,并没有全部提交出去 —— 那样只是换了个方向说谎。
    """
    calls = _wire(monkeypatch, _zero(2))

    dry = mw.run({"execute": False})
    assert "待执行建议 2 条" in dry

    real = mw.run({"execute": True})
    assert "待执行" not in real, "真跑完还在说「待执行」"
    assert "本轮领取建议 2 条" in real
    assert calls["put_inv"] or calls["feeds"]      # 确实提交过,不是空跑


def test_cleanup_summary_uses_the_same_wording(monkeypatch):
    """两条链同一处理 —— 各写各的措辞迟早一边改了另一边没改。"""
    import inspect

    from workflows import problem_product_cleanup as ppc
    for src in (inspect.getsource(mw.run), inspect.getsource(ppc.run)):
        assert '"待执行建议" if not execute else "本轮领取建议"' in src


def test_maintenance_goes_cross_store_concurrent_but_serial_within_a_store():
    """跨店并发,店内四类动作仍串行(所有者定稿 2026-08-17)。

    店内必须留着串行:同店 token 桶互挤,四类一起发只会互相退避。
    跨店安全:每店有自己的固定出口代理,配额与令牌桶都按 (store, endpoint) 计。
    """
    import inspect

    from services import stores as ss
    src = inspect.getsource(mw.run)
    assert "ThreadPoolExecutor" in src
    assert "stores_svc.STORE_WORKERS" in src
    # 店内串行:提交仍在 for kind in _KIND_ORDER 里逐类走
    assert "for kind in _KIND_ORDER" in src
    assert ss.STORE_WORKERS == 24


def test_summary_order_does_not_depend_on_which_store_finishes_first():
    """摘要按店名排序合并 —— 完成先后是随机的,行序不能跟着随机。

    往共享 list 上追加在 GIL 下不会坏数据,但同一轮跑两次输出不一样就没法对拍。
    """
    import inspect
    src = inspect.getsource(mw.run)
    merge = src[src.index("as_completed"):]
    assert "per_store" in merge and "for name, _ in todo" in merge


def test_title_mismatch_delete_is_paused_but_rows_stay_maintained(monkeypatch):
    """2026-08-19 所有者停闸「删除(title_mismatch)」;2026-08-20 所有者定停闸
    期口径:「删除(title_mismatch)已停闸,那么就要对这批行同时改价改标题改库存」。

    闸只关**删除**这一个出口:not_found 的删除照发;被压下来的低相似度行
    不是被冻结,而是与其余在架行同口径地改价/改标题/改库存。头一版顺手冻结
    了它们(三类维护也一并停),那是最坏的一种状态 —— 两百多条在架商品挂着
    错价错标题过夜,日志只说一句"压制 N 条"。
    恢复 = TITLE_MISMATCH_DELETE 改回 True,整段行为回到停闸前(下面钉住)。
    """
    assert mi.TITLE_MISMATCH_DELETE is False        # 停闸是当前现状
    rows = [_row(sku="B0MISMATCH", name="Walmart Title AAA", wm_price=20.0,
                 amz_price=20.0, avail_qty=10, stock_count=7,
                 slow={"title": "Totally Different ZZZ"}),
            _row(sku="B0GONE2", outcome="not_found",
                 name="X", slow={"title": "X"})]
    out = mi.delete_intents(_Conn(rows=[]), rows)
    got = {i["sku"]: i["code"] for i in out}
    assert "B0MISMATCH" not in got                  # 停闸:不出删除建议
    assert got.get("B0GONE2") == "not_found"        # 别的删除原因不受影响

    # 停闸 = 先别删,**不是**先别管:三类维护照常产出(所有者 2026-08-20)
    assert {i["sku"]: i["new"]
            for i in mi.price_intents(rows, _MULTS)} \
        .get("B0MISMATCH") == 40.0                          # 落地价 20×200%
    assert {i["sku"]: i["new"]
            for i in mi.inventory_intents(rows)} \
        .get("B0MISMATCH") == 7                             # 跟 amz 库存
    titled = {i["sku"]: i for i in mi.title_intents(rows)}
    assert titled["B0MISMATCH"]["new"] == "Totally Different ZZZ"
    # 原因码与常规同步分开:飞书「原因」列要数得出停闸期照改了多少行
    assert titled["B0MISMATCH"]["code"] == "title_mismatch_sync"

    monkeypatch.setattr(mi, "TITLE_MISMATCH_DELETE", True)
    out2 = mi.delete_intents(_Conn(rows=[]), rows)
    assert {i["sku"]: i["code"] for i in out2}.get("B0MISMATCH") \
        == "title_mismatch"                          # 恢复开关即回到旧行为
    # 恢复后这批行重新交给删除链:不改价、不改标题(停闸前的防呆)
    assert mi.classify(title_similarity=0.42)[:2] == ("delete", "title_mismatch")
    assert [i for i in mi.price_intents(rows, _MULTS)
            if i["sku"] == "B0MISMATCH"] == []
    assert [i for i in mi.title_intents(rows)
            if i["sku"] == "B0MISMATCH"] == []


# ── 双基准标题相似度(2026-08-19 所有者定稿:亚马逊标题拆两段之后)──────────

_LONG = "River Dream Waffle Shower Curtain with Liner Graphite Grey 71x74"
_SUB = "Snap-in Liner Heavy Duty Hotel Grade Mesh Top Window Standard Size"
_SLOW_SPLIT = {"title": f"{_LONG} | {_SUB}", "subtitle": _SUB, "brand": None}


def test_main_processed_title_exact_removesuffix():
    """主标题 = 精确剥掉 " | "+subtitle 尾段;不按 "|" 猜切。

    改版前老记录(subtitle 空、title 是拼好的长串)返回 "" —— 拆不出就说
    拆不出,不猜(契约明令禁止按分隔符切,正文本来就可能含 |)。
    """
    assert mi.main_processed_title(_SLOW_SPLIT).startswith("River Dream")
    assert _SUB.split()[0] not in mi.main_processed_title(_SLOW_SPLIT)
    assert mi.main_processed_title(
        {"title": f"{_LONG} | {_SUB}", "subtitle": None}) == ""   # 老记录不猜
    assert mi.main_processed_title(
        {"title": "尾巴对不上 | X", "subtitle": "Y"}) == ""       # 结尾不匹配
    assert mi.main_processed_title(None) == ""


def test_title_sim_dual_takes_the_better_basis():
    """双基准取 max:在架标题=主标题(在架的短标题存量行)时,
    单基准 ~0.6 会误判"不是同一个商品",双基准 = 1.0。"""
    wm = mi.processed_title({"title": _LONG, "brand": None})    # 短标题在架
    single = mi.order_audit.title_similarity(wm, mi.processed_title(_SLOW_SPLIT))
    assert single is not None and single < mi.TITLE_SIM_FLOOR   # 单基准会误杀
    dual = mi.title_sim_dual(wm, _SLOW_SPLIT)
    assert dual is not None and dual > 0.99                     # 双基准救回


def test_short_title_row_survives_delete_and_retitle(monkeypatch):
    """在架的短标题存量行:①删除判据(即使停闸恢复后)不再命中
    title_mismatch;②改标题 provider 不把它改回长标题(这批行当初就是长标题
    被内容审查拒了才换的短标题,改回去 = 自找再拒一遍);③库存/改价
    provider 也认双基准。

    ⚠ 造出这批行的「内容拒捞回」通道已于 2026-08-23 撤除,**但行还在线**,
    所以这三条防线一条都不能跟着撤 —— 撤了就是维护链自己把它们删掉/改坏。

    ③ 是 2026-08-20 补的:库存链此前拿**单基准**相似度问 classify,短标题行
    在它眼里 ~0.6 = 该删,于是不产库存意图;而删除链用双基准不删它 ——
    这批行既不清零也不删除,悄悄脱管而两边都不报错。四个 provider 问同一个
    判据,就必须喂同样的数。
    """
    monkeypatch.setattr(mi, "TITLE_MISMATCH_DELETE", True)      # 按停闸恢复后验
    wm = mi.processed_title({"title": _LONG, "brand": None})
    rows = [_row(sku="B0SHORT", name=wm, slow=_SLOW_SPLIT)]
    dels = mi.delete_intents(_Conn(rows=[]), rows)
    assert [d for d in dels if d["sku"] == "B0SHORT"] == []     # ① 不删
    titles = mi.title_intents(rows)
    assert [t for t in titles if t["sku"] == "B0SHORT"] == []   # ② 不改回长
    invs = mi.inventory_intents(rows)
    assert [i["new"] for i in invs if i["sku"] == "B0SHORT"] == [7]  # ③ 照常跟库存
    prices = mi.price_intents(rows, _MULTS)
    assert [p["sku"] for p in prices] == ["B0SHORT"]                 # ③ 照常改价


# ── 多仓:受管发货节点的配置与校验(批次 1)──────────────────────────────────

def _store(name="T1"):
    return {"name": name, "client_id": "cid", "client_secret": "sec",
            "proxy": None}


class _NodeConn:
    """ops.node_validations 的假连接:validated_at 按 age_hours 给(None=没认过)。"""

    def __init__(self, age_hours=None):
        self.age_hours = age_hours
        self.writes = []            # (动词, 参数):INSERT / DELETE
        self._last = ""

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self._last = sql
        verb = sql.strip().split()[0]
        if verb != "SELECT":
            self.writes.append((verb, args))

    def fetchone(self):
        from datetime import datetime, timedelta, timezone
        if "FROM ops.node_validations" in self._last and self.age_hours is not None:
            return (datetime.now(timezone.utc) - timedelta(hours=self.age_hours),)
        return None


def _node_wire(monkeypatch, age_hours=None, nodes=None, err=None):
    """受管仓校验的三件桩:记忆年龄 / shipnodes 返回(或抛)/ 补试不睡。"""
    from services import store_limits, store_retry

    conn = _NodeConn(age_hours)
    _fake_db(monkeypatch, conn)
    calls = []

    def _ship(store):
        calls.append(store["name"])
        if err is not None:
            raise err
        return nodes if nodes is not None else {}

    monkeypatch.setattr(store_limits.settings, "list_ship_nodes", _ship)
    monkeypatch.setattr(store_retry.time, "sleep", lambda s: None)
    return conn, calls


def test_resolve_node_returns_none_when_unconfigured(monkeypatch):
    """没填「维护仓库」= 现状(Virtual Node):**根本不调沃尔玛、不开连接**,零成本零变化。"""
    from services import store_limits

    monkeypatch.setattr(store_limits.settings, "list_ship_nodes",
                        lambda s: (_ for _ in ()).throw(
                            AssertionError("未配置的店不该调 shipnodes")))
    monkeypatch.setattr(store_limits.db, "pg_conn",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("未配置的店不该开连接")))
    assert store_limits.resolve_node(_store(), {}) is None
    assert store_limits.resolve_node(_store(), {"别的店": "123"}) is None


def test_resolve_node_accepts_a_known_fc_id_and_remembers_it(monkeypatch):
    """没认过 → 真调 shipnodes;认识 → 记一行(下次保鲜期内不再调)。"""
    from services import store_limits

    conn, calls = _node_wire(monkeypatch, None,
                             nodes={"91539778610008065": {"nodeType": "PHYSICAL"}})
    assert store_limits.resolve_node(
        _store(), {"T1": "91539778610008065"}) == "91539778610008065"
    assert calls == ["T1"]
    assert [w[0] for w in conn.writes] == ["INSERT"]
    assert conn.writes[0][1][:2] == ("T1", "91539778610008065")


def test_resolve_node_trusts_fresh_memory_without_calling_walmart(monkeypatch):
    """多仓 §3 原句「校验结果缓存一天」:保鲜期内**一次沃尔玛调用都不发**。

    这是根治的核心:常态店不再有那次"经代理、零重试、决定整店路由"的远程读。
    """
    from services import store_limits

    conn, calls = _node_wire(monkeypatch, age_hours=1,
                             err=AssertionError("保鲜期内不该调 shipnodes"))
    assert store_limits.resolve_node(_store(), {"T1": "999"}) == "999"
    assert calls == [] and conn.writes == []


def test_resolve_node_fails_closed_on_unknown_id(monkeypatch):
    """⚠ 填错不回落 Virtual Node —— 那等于把新仓的货写到旧节点,而且不报错。

    宁可这店今天不动:抛 NodeUnknownError(配置错),调用方整店跳过并告警;
    记忆一并抹掉 —— 沃尔玛的**明确否定**是记忆唯一的失效条件。
    """
    from services import store_limits

    conn, _calls = _node_wire(monkeypatch, age_hours=30, nodes={"111": {}})
    with pytest.raises(store_limits.NodeUnknownError) as e:
        store_limits.resolve_node(_store(), {"T1": "999"})
    assert "999" in str(e.value) and "整店跳过" in str(e.value)
    assert isinstance(e.value, store_limits.NodeConfigError)   # 老调用方照接
    assert [w[0] for w in conn.writes] == ["DELETE"]


def test_resolve_node_uses_stale_memory_when_list_unreadable(monkeypatch):
    """读不到 ≠ 不认识(2026-09-19 根因):值没变、昨天刚认过,今天代理抖一下不改判。

    此前这一条是"接口失败也算认不出",一次零重试的远程读就把整店从受管仓
    改判成默认节点(09-17 三家店 SSL EOF,235 条库存写到旧节点)。
    """
    from services import store_limits

    conn, calls = _node_wire(monkeypatch, age_hours=30,
                             err=RuntimeError("shipnodes 查询失败,沃尔玛返回 None"))
    node, how = store_limits._resolve(_store(), {"T1": "999"}, conn)
    assert (node, how) == ("999", "memory_stale")      # 兜底要计数,how 就是计数键
    assert calls == ["T1"] and conn.writes == []       # 试过、没成、记忆不动


def test_resolve_node_fails_closed_when_unreadable_and_no_memory(monkeypatch):
    """从没认过(新配置第一天)且读不到:仍然宁可不动 —— 但抛的是「读不到」,可补试。"""
    from api import _client
    from services import store_limits

    cause = _client.StoreProxyError("client_id=abc…",
                                    ConnectionError("Malformed reply"))
    _node_wire(monkeypatch, None, err=cause)
    with pytest.raises(store_limits.NodeUnreachableError) as e:
        store_limits.resolve_node(_store(), {"T1": "999"})
    assert e.value.__cause__ is cause                 # 归类/补试判据看原始异常
    assert "没有可沿用的校验记忆" in str(e.value)

    # 记忆超上限也不沿用:一家店 API 整月不通,别的链早就天天喊了
    _node_wire(monkeypatch, age_hours=24 * 31, err=cause)
    with pytest.raises(store_limits.NodeUnreachableError) as e:
        store_limits.resolve_node(_store(), {"T1": "999"})
    assert "记忆已超 30 天" in str(e.value)


# ── 多仓:维护链切受管仓(批次 2)──────────────────────────────────────────

def test_managed_nodes_splits_effective_from_skipped(monkeypatch):
    """校验通过的进字典、失败的进跳过名单 —— **两个都要有**。

    只读不校验 = 填错一个字符就静默写到别的节点;只校验不汇总 = 摘要报不出
    "今天几家店生效",配置生不生效没人看得见(计划 §6 第 6 条)。
    """
    from services import store_limits

    monkeypatch.setattr(store_limits, "maint_nodes",
                        lambda: {"T1": "111", "T2": "999", "T3": "222"})
    _node_wire(monkeypatch, None)
    monkeypatch.setattr(store_limits.settings, "list_ship_nodes",
                        lambda s: {"T1": {"111": {}}, "T2": {"333": {}}}[s["name"]])
    stats: dict = {}
    ok, skipped = store_limits.managed_nodes(
        [_store("T1"), _store("T2")], stats=stats)   # T3 填了但不在可调用店铺列表里
    assert ok == {"T1": "111"}
    assert set(skipped) == {"T2", "T3"}
    assert "不在可调用店铺列表里" in skipped["T3"]
    note = store_limits.managed_note(ok, skipped, stats)
    assert "T1=111" in note and "整店跳过 2 家" in note
    # 归类词跟着店名:填错改表、不可调用查凭证表 —— 只报店名等于让人翻日志
    assert "T2(FC ID 不在列表)" in note and "T3(不可调用)" in note
    assert "接口校验 1 家" in note and "沿用旧记忆" not in note


def test_managed_nodes_retries_unreachable_stores_once_and_names_the_cause(monkeypatch):
    """「读不到」走店维失败标准①:串行补试一遍(凭证死不补),归类词进摘要。

    此前 managed_nodes 是全仓唯一不走这套标准的按店远程调用 —— 一次抖动就
    整店改判,而摘要只说「校验失败」,人分不清该改表还是该等。
    """
    from api import _client
    from services import store_limits

    monkeypatch.setattr(store_limits, "maint_nodes",
                        lambda: {"T1": "111", "T2": "222", "T3": "333"})
    conn, calls = _node_wire(monkeypatch, None)
    flaky = {"T1": 1}          # T1 首轮抖一次、补试成功;T2 一直代理坏;T3 凭证死

    def _ship(store):
        n = store["name"]
        calls.append(n)
        if n == "T1" and flaky["T1"]:
            flaky["T1"] -= 1
            raise _client.StoreProxyError("client_id=a…", ConnectionError("Malformed reply"))
        if n == "T2":
            raise _client.StoreProxyError("client_id=b…",
                                          ConnectionError("Invalid username/password"))
        if n == "T3":
            raise _client.StoreDeadError("client_id=c…", 400)
        return {"111": {}}

    monkeypatch.setattr(store_limits.settings, "list_ship_nodes", _ship)
    stats: dict = {}
    ok, skipped = store_limits.managed_nodes(
        [_store("T1"), _store("T2"), _store("T3")], stats=stats)
    assert ok == {"T1": "111"}
    assert calls == ["T1", "T2", "T3", "T1", "T2"]    # 补试串行;凭证死不补
    assert set(skipped) == {"T2", "T3"}
    assert stats["words"] == {"T2": "代理无效", "T3": "凭证失效"}
    assert stats["retried"] == 3 and stats["fresh"] == 1
    note = store_limits.managed_note(ok, skipped, stats)
    assert "T2(代理无效)" in note and "T3(凭证失效)" in note and "补试 3 家" in note
    assert [w[0] for w in conn.writes] == ["INSERT"]  # 只有真认过的才落记忆


def test_managed_nodes_counts_stale_memory_fallback_in_the_note(monkeypatch):
    """兜底(接口读不到沿用旧记忆)触发必须见人(conventions §六 三要件)。"""
    from services import store_limits

    monkeypatch.setattr(store_limits, "maint_nodes", lambda: {"T1": "111"})
    _node_wire(monkeypatch, age_hours=48,
               err=RuntimeError("shipnodes 查询失败,沃尔玛返回 503: x"))
    stats: dict = {}
    ok, skipped = store_limits.managed_nodes([_store("T1")], stats=stats)
    assert ok == {"T1": "111"} and skipped == {}
    assert stats["memory_stale"] == 1 and stats["retried"] == 0
    assert "⚠ 接口读不到沿用旧记忆 1 家" in store_limits.managed_note(ok, skipped, stats)


def test_managed_nodes_logs_the_uncallable_branch_in_fleet_mode(monkeypatch, caplog):
    """2026-09-06 谭总22/23/24 走的就是这条无日志分支 —— 事后只能靠摘要里的店名猜。"""
    import logging

    from services import store_limits

    monkeypatch.setattr(store_limits, "maint_nodes", lambda: {"T9": "111"})
    _fake_db(monkeypatch, _NodeConn())
    from services import stores as stores_mod
    monkeypatch.setattr(stores_mod, "load_stores", lambda names=None: [])
    with caplog.at_level(logging.WARNING, logger="services.store_limits"):
        ok, skipped = store_limits.managed_nodes()
    assert skipped == {"T9": "不在可调用店铺列表里"}
    assert any("T9" in r.message and "不在可调用店铺列表里" in r.message
               for r in caplog.records)


def test_ship_nodes_empty_parse_raises_and_is_not_cached(monkeypatch):
    """200 但没解析出节点:抛而不是返回空 —— 返回空会被判成「不在列表(认识的:(空))」

    (瞬时故障伪装成配置错),而且空结果进 lru 缓存,整个进程再也不重打接口。
    """
    from api import _client, settings as settings_api

    settings_api._cached_ship_nodes.cache_clear()
    monkeypatch.setattr(_client, "rate_acquire", lambda b, c: 0.0)
    monkeypatch.setattr(_client, "get_token", lambda *a: "tok")
    monkeypatch.setattr(_client, "safe_get_ex",
                        lambda *a, **k: (200, {}, None))   # 空体 / 非 JSON
    with pytest.raises(RuntimeError, match="没解析出任何节点"):
        settings_api.list_ship_nodes(_store())
    assert settings_api._cached_ship_nodes.cache_info().currsize == 0
    # 非 200 的文案要让 store_retry.diagnose 认得出(沃尔玛NNN / 网络未达)
    from services import store_retry
    monkeypatch.setattr(_client, "safe_get_ex", lambda *a, **k: (None, {}, None))
    with pytest.raises(RuntimeError) as e:
        settings_api.list_ship_nodes(_store("T2"))
    assert store_retry.diagnose(e.value) == "网络未达"
    monkeypatch.setattr(_client, "safe_get_ex", lambda *a, **k: (503, {}, None))
    with pytest.raises(RuntimeError) as e:
        settings_api.list_ship_nodes(_store("T3"))
    assert store_retry.diagnose(e.value) == "沃尔玛503"


def test_managed_nodes_costs_nothing_when_unconfigured(monkeypatch):
    """一家店都没配 = 一次沃尔玛调用都不发生(现状零成本、零行为变化)。"""
    from services import store_limits

    monkeypatch.setattr(store_limits, "maint_nodes", lambda: {})
    monkeypatch.setattr(store_limits.settings, "list_ship_nodes",
                        lambda s: (_ for _ in ()).throw(
                            AssertionError("没配置就不该调 shipnodes")))
    assert store_limits.managed_nodes() == ({}, {})
    assert store_limits.managed_note({}, {}) == ""      # 摘要里也不占一行


def test_ship_node_survives_the_disposition_round_trip():
    """⚠ `ship_node` 必须在 _DETAIL_KEYS 里:它决定执行件走哪条写通道。

    漏登记的表现是扫描件判对了受管仓、执行件却写到默认节点,**两边都不报错**。
    """
    it = {"store": "T1", "sku": "B0A", "kind": "inventory", "old": 10,
          "new": 0, "code": "stockzero", "reason": "整店清零",
          "ship_node": "91539778610008065"}
    back = mi.from_disposition({"id": 7, **mi.to_disposition(it)})
    assert back["ship_node"] == "91539778610008065"
    # 未配置店不带这个键 —— 建议行与改造前逐字节一致
    plain = mi.to_disposition({k: v for k, v in it.items() if k != "ship_node"})
    assert "ship_node" not in plain["detail"]


def test_managed_store_routes_both_write_channels(monkeypatch):
    """受管仓的店:小批量走分节点 PUT,大批量走 MP_INVENTORY v1.5。

    ⚠ 大批量若照旧走 v1.4 `inventory` feed,载荷里**根本没有节点字段** ——
    发出去就是写到官方无定义的"默认节点",而且回执一切正常。
    """
    small = [dict(i, ship_node="N1") for i in _zero(2)]
    calls = _wire(monkeypatch, small)
    mw.run({"execute": True})
    assert calls["put_inv"] == [("T1", "S0", 0, "N1"), ("T1", "S1", 0, "N1")]

    big = [dict(i, ship_node="N1")
           for i in _zero(mi.SYNC_THRESHOLDS["inventory"] + 1)]
    calls = _wire(monkeypatch, big)
    mw.run({"execute": True})
    assert [(ft, n) for _, ft, n in calls["feeds"]] == [("MP_INVENTORY", 11)]

    # 未配置店逐字节维持现状:v1.4 `inventory`
    calls = _wire(monkeypatch, _zero(mi.SYNC_THRESHOLDS["inventory"] + 1))
    mw.run({"execute": True})
    assert [(ft, n) for _, ft, n in calls["feeds"]] == [("inventory", 11)]


def test_mixed_node_batch_fails_loudly(monkeypatch):
    """一店一个受管仓:同批混着带/不带节点 = 配置本轮中途变了,**响亮失败**。

    挑着发一半的后果是另一半写到默认节点,而摘要显示"提交成功 N 条"。
    """
    items = _zero(mi.SYNC_THRESHOLDS["inventory"] + 1)
    for it in items[:-1]:
        it["ship_node"] = "N1"
    calls = _wire(monkeypatch, items)
    out = mw.run({"execute": True})
    assert calls["feeds"] == []                  # 一片都没发
    assert "提交异常已跳过" in out


def test_scan_reports_which_stores_the_managed_node_took_effect_for(monkeypatch):
    """配置生效与否必须天天见人(计划 §6 第 6 条)。"""
    ms, _calls = _scan_wire(monkeypatch, _zero(1))
    monkeypatch.setattr(ms.store_limits, "managed_nodes",
                        lambda conn=None, stats=None: (
                            {"T1": "111"}, {"T2": "认不出"}))
    out = ms.run({"preview": "1"})
    assert "T1=111" in out and "整店跳过 1 家" in out


# ── 多仓:上架链(批次 3)────────────────────────────────────────────────────

def test_listing_fc_prefers_the_managed_node(monkeypatch):
    """上架仓:配置了「维护仓库」的店填那个 FC ID,没配的店才用 Partner ID。

    官方口径:建了自建仓就填该仓的 shipNode,"没建过 FC、走 Default Node
    (Virtual)时才用 Virtual Node ID(它等于 Partner ID)"。
    """
    from services import store_limits

    monkeypatch.setattr(store_limits.settings, "get_partner_id",
                        lambda s: "PARTNER")
    assert store_limits.listing_fc(_store("T1"), {"T1": "111"}) == "111"
    assert store_limits.listing_fc(_store("T2"), {"T1": "111"}) == "PARTNER"


def test_match_restock_treats_a_missing_node_row_as_empty_not_as_skip():
    """配置店缺节点行 = 受管仓里没货 = **该铺**(2026-08-30 实测定案)。

    ⚠ 曾经在这里判"跳过"(以为节点行要先做 SKU×FC 关联才出现),那会造成
    死锁:永远不写 → 永远没有行 → 永远跳过。实测证明节点行是**第一次写库存
    时创建的**(谭总12 B008LUW4CI:写入 Success,读回立刻多出该节点)。
    """
    managed = {"T1": "N_NEW"}
    # (store, sku, avail_qty, node_qty):受管仓无行 ⇒ 现值 0 ⇒ 铺货
    conn = _Conn(rows=[("T1", "M0A", 0, None)])
    out = mi.match_inventory_intents(conn, [], managed)
    assert [(i["sku"], i["new"], i["ship_node"]) for i in out] == [
        ("M0A", mi.MATCH_INVENTORY_QTY, "N_NEW")]

    # 受管仓已有货 ⇒ 不铺(哪怕合计是别的节点凑出来的,也只看受管仓)
    conn = _Conn(rows=[("T1", "M0A", 99, 7)])
    assert mi.match_inventory_intents(conn, [], managed) == []

    # 未配置店维持旧口径:合计 0/未知都算要铺(不带 ship_node)
    conn = _Conn(rows=[("T2", "M0B", None, None)])
    out = mi.match_inventory_intents(conn, [], managed)
    assert [(i["sku"], "ship_node" in i) for i in out] == [("M0B", False)]


# ── 维护记录追加:起点先验空(2026-08-27 找空行归一到 blacklist_sheet 后补钉)──

def test_append_records_starts_at_the_first_truly_empty_row(monkeypatch):
    """水位漂了也不许覆盖已有流水:起点必须**先验空**再写。

    这是 append_records 唯一的防覆盖手段(流水账只追加、程序是唯一写入方),
    而 2026-08-27 把找空行换成 services/blacklist_sheet.next_empty 之后,这一跳
    跨了模块 —— 传错东西(比如传 `.require()` 的产物而不是登记条目)在生产上
    才炸。两边共用 api.feishu 同一个模块对象,所以这里 patch 一次就盖住两边。
    """
    monkeypatch.setattr(resources, "MAINT_SHEET",
                        Spreadsheet(name="维护记录", token="TOK", sheet_id="SID",
                                    columns=resources.MAINT_SHEET.columns))
    conn = _Conn()
    conn.cursor_value = {"next_row": 5, "unresolved_from": 2}
    _fake_db(monkeypatch, conn)
    monkeypatch.setattr(maint_sheet.feishu, "sheet_row_count", lambda s: 100)
    # 水位说第 5 行,可 5/6 两行还有流水(裁剪后行号整体上移就是这样)⇒ 真空行在 7
    # (next_empty 扫的是 A 单列的固定小范围,走 sheet_values_small,不是大范围读通道)
    monkeypatch.setattr(maint_sheet.feishu, "sheet_values_small",
                        lambda sheet, rng: [["x"], ["y"]])
    ensured, written = [], []
    monkeypatch.setattr(maint_sheet.feishu, "sheet_ensure_rows",
                        lambda sheet, n: ensured.append(n))
    monkeypatch.setattr(maint_sheet.feishu, "sheet_write_ranges",
                        lambda sheet, ups: written.extend(ups))
    row = maint_sheet.build_row("T1", "B0A", "改价", "", "price", "F1",
                                "2026-08-27", "处理中")

    assert maint_sheet.append_records([row]) == 1
    assert written[0][0].startswith("A7:")      # 5/6 有数据 → 从 7 起写,不覆盖
    assert ensured == [8]                       # 网格不够先扩行(next_empty 的返回契约)
    saved = [a for sql, a in conn.sqls if "ops.cursors" in sql][-1]
    assert '"next_row": 8' in saved[1]          # 水位落到真正写完的下一行


def test_append_records_delegates_the_scan_to_the_shared_next_empty(monkeypatch):
    """找空行**只有一条实现**(blacklist_sheet.next_empty),且传进去的是
    登记条目本身 —— next_empty 内部还要拿它去 sheet_row_count/sheet_values_small,
    传 `.require()` 的产物会当场坏掉。谁哪天又在本文件抄回一份,这条会红。
    """
    sheet_entry = Spreadsheet(name="维护记录", token="TOK", sheet_id="SID",
                              columns=resources.MAINT_SHEET.columns)
    monkeypatch.setattr(resources, "MAINT_SHEET", sheet_entry)
    conn = _Conn()
    conn.cursor_value = {"next_row": 9, "unresolved_from": 2}
    _fake_db(monkeypatch, conn)
    seen = []
    monkeypatch.setattr(maint_sheet.blacklist_sheet, "next_empty",
                        lambda sheet, start: (seen.append((sheet, start)), 9)[1])
    monkeypatch.setattr(maint_sheet.feishu, "sheet_ensure_rows", lambda s, n: None)
    monkeypatch.setattr(maint_sheet.feishu, "sheet_write_ranges", lambda s, ups: None)
    row = maint_sheet.build_row("T1", "B0A", "改价", "", "price", "F1",
                                "2026-08-27", "处理中")

    maint_sheet.append_records([row])
    assert seen == [(sheet_entry, 9)]


# ── 店铺事件账本(运营类:每店每轮一条,按动作类分桶)──────────────────────

def _capture_rounds(monkeypatch):
    got: list = []
    monkeypatch.setattr(mw.store_events, "record_round",
                        lambda conn, source, event, per_store:
                        (got.append((source, event, dict(per_store))),
                         len(per_store))[1])
    return got


def test_execute_records_one_round_event_bucketed_by_kind(monkeypatch):
    """PUT 路由记 submitted/failed 两档(它不进 feed 台账,没有 dedup/unknown
    —— 强行凑四档会让账本看起来两条路由同构)。"""
    _wire(monkeypatch, _zero(2))
    got = _capture_rounds(monkeypatch)
    mw.run({"execute": True})
    assert len(got) == 1
    source, event, per_store = got[0]
    assert (source, event) == ("maintenance", mw.store_events.MAINT_ROUND)
    assert per_store == {"T1": {"inventory": {"submitted": 2, "failed": 0}}}


def test_dry_run_records_no_round_event(monkeypatch):
    _wire(monkeypatch, _zero(2))
    got = _capture_rounds(monkeypatch)
    mw.run({"execute": False})
    assert got == []


# 旧节点清零(node_clear)的用例在 tests/test_node_clear.py

def test_suppress_key_separates_nodes_but_leaves_legacy_keys_byte_identical():
    """⚠ 受管仓意图的防重键要带 node;未配置店的键**一个字节都不许变**。

    带 node:昨天写 Virtual、今天写受管仓,店铺/SKU/类型/新值全一样 ——
    不区分就会被压掉,受管仓收不到(搬仓当天 = 两节点都 0 = 不可售)。
    不动旧键:加个空段会让 ops.dedupe 存量键全部失配 ⇒ 全店一轮重发,
    正是这道闸当初要防的 208 条 stale update(2026-08-09 生产实证)。
    """
    base = {"store": "T1", "sku": "B0A", "kind": "inventory", "new": 7}
    assert mi._suppress_key(base) == "T1|B0A|inventory|7"          # 逐字节不变
    assert mi._suppress_key({**base, "ship_node": "N1"}) == "T1|B0A|inventory|7|N1"
    # 同一件事写不同节点 = 两件事
    assert mi._suppress_key({**base, "ship_node": "N1"}) != \
        mi._suppress_key({**base, "ship_node": "N2"})


# ── 一次取数 + 单店下推(2026-09-03 性能定案;**零行为变化**是硬要求)──────────
# 背景(所有者生产实测):maintenance_scan 全量十几分钟,`-p store=` 一样慢。
# 两条根因都与判据无关 —— ① 同一条昂贵查询被四个 provider 各跑一遍(其中三次
# 参数完全相同,pg_stat_activity 实见三条一模一样的查询并发跑 2 分半);
# ② `-p store=` 只在 Python 里过滤,数据库照旧全库扫。
# 下面这几条用例钉的全是"只是快,产出一个字不变"。


class _ScanConn(_Conn):
    """collect_all 的假连接:六条 SQL 各给一份行,并**真的按 %(only)s 过滤**。

    ⚠ 必须真过滤:只有这样才验得出"下推之后的意图集合 == 不下推 + Python 过滤"。
    桩若忽略 only,这条用例就退化成"两次全量跑出来一样",什么都没证明。
    """

    def __init__(self, amz=(), offset=(), oos=(), match=(), zero=()):
        super().__init__()
        self.amz, self.offset, self.oos = list(amz), list(offset), list(oos)
        self.match, self.zero = list(match), list(zero)
        self.n_amz = 0
        self._args: dict = {}

    def execute(self, sql, args=None):
        super().execute(sql, args)
        # drop_recent 走的是位置参数(另一张表,不按店取行)——只收具名的
        self._args = dict(args) if isinstance(args, dict) else {}

    def _only(self, rows, key):
        only = self._args.get("only")
        return [r for r in rows if only is None or key(r) == only]

    @property
    def description(self):
        class _D:
            def __init__(self, name):
                self.name = name
        if "FROM catalog.snapshots l" in self._last and self.amz:
            return [_D(k) for k in self.amz[0]]
        return []

    def fetchall(self):
        q = self._last
        if "FROM catalog.snapshots l" in q:              # _SQL_AMZ_JOIN
            self.n_amz += 1
            cols = list(self.amz[0]) if self.amz else []
            return [tuple(r[c] for c in cols)
                    for r in self._only(self.amz, lambda r: r["store"])]
        if "ops.scrape_failures" in q:                  # _SQL_VARIANT_OFFSET
            return self._only(self.offset, lambda r: r[0])
        if "WITH req AS" in q:                          # _SQL_LONG_OOS
            return self._only(self.oos, lambda r: r[0])
        if "source_type = 'match'" in q:                # _SQL_MATCH_INV
            return self._only(self.match, lambda r: r[0])
        if "WHERE w.store = ANY(%(stores)s::text[])" in q:      # _SQL_ZERO
            return self._only(self.zero, lambda r: r[0])
        return []                                       # ops.dedupe(drop_recent)


def _collect_conn():
    """两家店 T1/T2 各出一条改价/一条清零/一条偏移删除/一条长期无货删除。"""
    return _ScanConn(
        amz=[_row(store="T1", sku="B0P1", wm_price=20.0, amz_price=20.0),
             _row(store="T2", sku="B0P2", wm_price=20.0, amz_price=20.0),
             _row(store="T1", sku="B0I1", avail_qty=5, stock_count=0),
             _row(store="T2", sku="B0I2", avail_qty=5, stock_count=0)],
        offset=[("T1", "B0V1", 1, None, None), ("T2", "B0V2", 1, None, None)],
        oos=[("T1", "B0O1", 15, None, None, 0, "", 0),
             ("T2", "B0O2", 15, None, None, 0, "", 0)],
        match=[("T1", "M1", 0, None), ("T2", "M2", 0, None)],
        zero=[("Z1", "S1", 5, None)])


def _collect_wire(monkeypatch):
    m = {"fbm_range1": "200%", "fbm_range2": "200%"}
    monkeypatch.setattr(mi.store_limits, "price_multipliers",
                        lambda: {"T1": m, "T2": m})
    monkeypatch.setattr(mi.store_limits, "lead_day_caps", lambda: {})
    monkeypatch.setattr(st, "store_channels", lambda: {})


def test_four_providers_share_one_fetch_and_never_touch_the_rows(monkeypatch):
    """① 共享一份行的产出 == 各自查一份;② provider 不就地改行。

    ② 是这次改造唯一的静默风险:四个 provider 从前各拿一份自己的 dict,
    共享之后谁写一句 `r[...] = ...` 都会串到后面的 provider 头上,而且不报错。
    """
    monkeypatch.setattr(mi.store_limits, "lead_day_caps", lambda: {})
    rows = [_row(sku="B0P", wm_price=20.0, amz_price=20.0),
            _row(sku="B0T", name="Steel Cup 500ml"),
            _row(sku="B0I", avail_qty=5, stock_count=0),
            _row(sku="B0D", outcome="not_found", name="X", slow={"title": "X"})]
    before = copy.deepcopy(rows)
    shared = (mi.delete_intents(_DelConn(), rows) + mi.title_intents(rows)
              + mi.price_intents(rows, _MULTS) + mi.inventory_intents(rows))
    apart = (mi.delete_intents(_DelConn(), copy.deepcopy(rows))
             + mi.title_intents(copy.deepcopy(rows))
             + mi.price_intents(copy.deepcopy(rows), _MULTS)
             + mi.inventory_intents(copy.deepcopy(rows)))
    assert shared == apart and shared          # 逐行相同,且真有产出
    assert rows == before                      # 一个字节都没被就地改过


def test_collect_all_queries_the_amz_join_exactly_once(monkeypatch):
    """从前四个 provider 各查一次(生产实见三条一模一样的查询并发跑 2 分半)。"""
    _collect_wire(monkeypatch)
    conn = _collect_conn()
    intents, _capped = mi.collect_all(conn, ["Z1"])
    assert conn.n_amz == 1
    assert {i["kind"] for i in intents} >= {"delete", "price", "inventory"}


def test_providers_never_fetch_their_own_rows():
    """取数唯一入口是 collect_all(§六 单一实现路径)。

    provider 自己再查一次的表现不是报错,而是"改价按 A 行、删除按 B 行"
    (两次查询之间目录可能已被 catalog_sync 改写)—— 而且两边都不报错。
    """
    import inspect
    src = inspect.getsource(mi)
    assert len(re.findall(r"\b_rows\(", src)) == 2       # 定义处 + collect_all


def test_store_filter_pushdown_changes_nothing_but_the_scan(monkeypatch):
    """② `only` 下推后的意图集合 == 不下推 + Python 过滤(逐行相同)。

    ⚠ 这里的"逐行"含顺序,是因为桩按固定顺序发行。**真库里行序本来就不稳定**
    (_SQL_AMZ_JOIN 无 ORDER BY;PG 16 实测同一连接连查两次序都不同),所以
    下推前后的**顺序**不保证一致 —— 保证一致的是意图集合与截断报告,后者靠
    _TRUNC_PRIORITY 的全序键(第二键是 SKU)与顺序无关。
    """
    _collect_wire(monkeypatch)
    full, cap_full = mi.collect_all(_collect_conn(), ["Z1"])
    one, cap_one = mi.collect_all(_collect_conn(), ["Z1"], only="T1")
    assert one == [i for i in full if i["store"] == "T1"]
    assert cap_one == [c for c in cap_full if c["store"] == "T1"]
    # 真有东西被滤掉(否则这条断言是空转):四类意图 T1 侧都在
    assert {i["kind"] for i in one} >= {"delete", "price", "inventory"}
    assert any(i["store"] != "T1" for i in full)


def test_every_per_store_sql_actually_binds_only(monkeypatch):
    """闸最容易的死法:某条 SQL 加了条件却没绑参数 —— 单店照旧全库扫且不报错。"""
    _collect_wire(monkeypatch)
    conn = _collect_conn()
    mi.collect_all(conn, ["Z1"], only="T1")
    bound = [a for sql, a in conn.sqls if mi._ONLY_STORE in sql]
    assert len(bound) == 5                      # 五条按店取行的 SQL 一条不漏
    assert all(a.get("only") == "T1" for a in bound)
    conn2 = _collect_conn()
    mi.collect_all(conn2, ["Z1"])
    assert all(a.get("only") is None
               for sql, a in conn2.sqls if mi._ONLY_STORE in sql)


def test_only_is_a_no_op_when_no_store_is_given():
    """③ only=None 时行为与下推前等价 —— `NULL::text IS NULL` 恒真。

    所以整段条件退化成 `AND true`,五条 SQL 的行集合与改造前逐行相同。
    形状钉死在这里:写成 `w.store = coalesce(%(only)s, w.store)` 之类的变体
    在 store 为 NULL 的行上语义就不一样了(本表 store NOT NULL,但别留这个坑)。
    """
    assert mi._ONLY_STORE.strip() == \
        "AND (%(only)s::text IS NULL OR w.store = %(only)s)"
    for q in (mi._SQL_ZERO, mi._SQL_AMZ_JOIN, mi._SQL_VARIANT_OFFSET,
              mi._SQL_LONG_OOS, mi._SQL_MATCH_INV):
        assert q.count(mi._ONLY_STORE) == 1
        # ⚠ psycopg3 不许位置与具名参数混用:这五条只准有具名参数
        assert not re.search(r"(?<!%)%s", q)


def test_scan_pushes_the_store_filter_and_keeps_the_python_belt(monkeypatch):
    """workflow 侧:`only` 传进 collect_all,而 Python 那两行过滤**保留**。

    保留的理由是不对称的:漏了下推只是慢;漏了过滤则是别的店的意图混进单店
    这一轮,再顺着 withdraw_stale 的 store=only 造出错误取证,两边都不报错。
    (本用例的 collect_all 桩故意不理会 only,正好把那两行钉住。)
    """
    intents = [{"store": "T1", "sku": "B0A", "kind": "inventory",
                "old": 5, "new": 0},
               {"store": "T2", "sku": "B0B", "kind": "inventory",
                "old": 5, "new": 0}]
    ms, calls = _scan_wire(monkeypatch, intents)
    out = ms.run({"store": "T1", "preview": "1"})
    assert calls["collect"] == ["T1"]           # 下推了
    assert "T2" not in out and "B0B" not in out  # 双保险仍在


# ── 多仓:校验失败整店跳过要真的跳过(2026-09-19 生产缺陷)────────────────────

def test_scan_drops_every_intent_of_a_store_whose_node_failed_validation(monkeypatch):
    """「校验失败整店跳过」此前只是摘要里一句话,意图一条没少。

    managed_nodes() 把校验失败的店放进 skipped、不进 managed,于是 collect_all
    给它算出的库存意图**不带 ship_node**,执行件拿到就走 legacy 通道写到默认
    节点 —— 谭总23 2026-09-17 一天 235 条全部执行,受管仓值没变(ineffective)、
    旧节点被写上货,双节点有货由此而来。整店剔除、首行点名、护住存量行。
    """
    intents = _zero(2) + [{"store": "T2", "sku": "X1", "kind": "inventory",
                           "old": 3, "new": 0, "code": "out_of_stock",
                           "reason": "亚马逊缺货", "ship_node": "222"}]
    capped = [{"store": "T1", "kind": "inventory", "total": 9, "kept": 8,
               "deferred_keys": [("T1", "S9", "inventory")]}]
    ms, calls = _scan_wire(monkeypatch, intents, capped=capped)

    def _mn(conn=None, stats=None):
        stats.update(words={"T1": "代理波动"}, memory=1, fresh=0,
                     memory_stale=0, retried=1, gate_note="")
        return {"T2": "222"}, {"T1": "读不到"}

    monkeypatch.setattr(ms.store_limits, "managed_nodes", _mn)
    out = ms.run({})
    first = out.splitlines()[0]
    # 只有首行能到飞书:剔了多少条必须写在首行,且不回落默认节点;归类词跟着店名
    # (「代理波动」等下轮、「FC ID 不在列表」改表 —— 只报店名人还得翻日志)
    assert ("受管仓校验失败整店跳过 1 店:T1(代理波动)(2 条意图不产出,"
            "不回落默认节点") in first
    assert "记忆沿用 1 家" in out and "补试 1 家" in out
    assert "维护意图 1 条" in first
    assert "截断" not in first                   # 跳过店的截断顺延一并剔掉
    assert [(r["store"], r["sku"]) for r in calls["suggest"]] == [("T2", "X1")]
    # 跳过 ≠ 恢复正常:该店挂着的 suggested 不许被撤成「商品自己恢复正常了」
    _src, keep, _store, excluded = calls["withdraw"][0]
    assert "T1" in excluded
    assert ("T1", "S9", "inventory") not in keep


def test_executor_holds_nodeless_inventory_intents_of_a_configured_store(monkeypatch):
    """第二道闸:填了「维护仓库」的店,库存建议不带节点就**不执行**。

    来源只可能是修复前的存量行 / 扫描与执行之间改了配置 / provider 漏节点;
    三种都不许走 legacy 写默认节点。留在 suggested 原地、表上一行「未执行」;
    价格与节点无关照常;同店带节点的库存建议照常。
    """
    intents = _zero(2) + [
        {"store": "T1", "sku": "P1", "kind": "price", "old": 9.0, "new": 8.0,
         "code": "price_sync", "reason": "跟价"},
        {"store": "T1", "sku": "S9", "kind": "inventory", "old": 1, "new": 0,
         "code": "out_of_stock", "reason": "亚马逊缺货", "ship_node": "N1"}]
    calls = _wire(monkeypatch, intents)
    monkeypatch.setattr(mw.store_limits, "maint_nodes", lambda: {"T1": "N1"})
    out = mw.run({"execute": True})
    assert calls["put_inv"] == [("T1", "S9", 0, "N1")]     # 带节点的照常
    assert calls["put_price"] == [("T1", "P1", 8.0)]        # 价格与节点无关
    assert calls["feeds"] == []
    assert "受管仓建议缺节点扣下 2 条(不写默认节点):T1×2" in out
    held = [r for r in calls["sheet"] if mw.NODELESS_HELD in r]
    assert len(held) == 2 and all("S0" in r or "S1" in r for r in held)
    # 扣下的行没转 executing(留在 suggested 等扫描件重算)
    marked = {i for ids, _ in calls["marked"] for i in ids}
    assert marked == {102, 103}                      # _disp 的 id 从 100 起

    # 全被扣下:没有可提交的,但扣下的行照样进表
    calls = _wire(monkeypatch, _zero(2))
    monkeypatch.setattr(mw.store_limits, "maint_nodes", lambda: {"T1": "N1"})
    out = mw.run({"execute": True})
    assert calls["put_inv"] == [] and calls["feeds"] == []
    assert "没有待执行的维护建议" in out and "扣下 2 条" in out
    assert sum(1 for r in calls["sheet"] if mw.NODELESS_HELD in r) == 2

    # dry-run 也要说出来(人眼闸门看的就是它)
    calls = _wire(monkeypatch, _zero(2))
    monkeypatch.setattr(mw.store_limits, "maint_nodes", lambda: {"T1": "N1"})
    out = mw.run({"execute": False})
    assert "扣下 2 条" in out and calls["sheet"] == []

    # 未配置店逐字节维持现状:不带节点就是正常的 legacy 路由
    calls = _wire(monkeypatch, _zero(2))
    monkeypatch.setattr(mw.store_limits, "maint_nodes", lambda: {"T9": "N1"})
    out = mw.run({"execute": True})
    assert calls["put_inv"] == [("T1", "S0", 0), ("T1", "S1", 0)]
    assert "扣下" not in out


def test_executor_node_gate_fails_open_but_loudly_when_config_unreadable(monkeypatch):
    """配置读不到:不扣、只喊(与本件缺席避让同方向;扫描件刚按同一张表剔过)。"""
    calls = _wire(monkeypatch, _zero(2))

    def boom():
        raise RuntimeError("feishu 503")

    monkeypatch.setattr(mw.store_limits, "maint_nodes", boom)
    out = mw.run({"execute": True})
    assert calls["put_inv"] == [("T1", "S0", 0), ("T1", "S1", 0)]
    assert "受管仓配置读不到(feishu 503)" in out
