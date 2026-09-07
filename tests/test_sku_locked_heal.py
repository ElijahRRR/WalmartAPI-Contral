"""SKU_LOCKED 自愈链回归:退役提交、冷却防重、清列重上、失败不自动重试。

旧实证(legacy_survey.md:1667):SKU 绑死旧 UPC,不先 RETIRE 直接换 UPC
重发同一 SKU 也失败——所以链路必须是 RETIRE → 24h 冷却 → 清列 → 新行重上。
"""

import pytest

from workflows import sku_locked_heal as heal


class _Conn:
    """假连接:记录 SQL,喂 _SQL_OPEN 的返回。"""

    def __init__(self, state=()):
        self.sqls: list = []
        self._state = list(state)
        self.rowcount = 1        # upc_pool.burn 读 rowcount

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))

    def executemany(self, sql, rows):
        self.sqls.append((sql, list(rows)))

    def fetchall(self):
        return self._state

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# 默认 SKU 列为空 = 存量行形态(全链回落 B 列 ASIN);传 sku= 即模拟
# 批次 2 之后「C 列是真码」的行。
def _row(rownum, store="T1", asin=None, list_result="SKU_LOCKED", sku=""):
    return {"rownum": rownum, "store": store,
            "asin": asin or f"B0LOCK{rownum:04d}", "sku": sku,
            "list_result": list_result, "feed_id": "F0", "listed": "Yes"}


def _patch_common(monkeypatch, rows, state, conn=None):
    conn = conn or _Conn(state)
    monkeypatch.setattr(heal.listing_sheet, "read_rows", lambda: rows)
    monkeypatch.setattr(heal.db, "pg_conn", lambda: conn)
    monkeypatch.setattr(heal.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    return conn


def test_dry_run_reports_without_touching_anything(monkeypatch):
    rows = [_row(2), _row(3)]
    conn = _patch_common(monkeypatch, rows, state=[])
    monkeypatch.setattr(heal.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("dry-run 不许提交")))
    out = heal.run({"execute": False})
    assert "SKU_LOCKED 2 行" in out
    assert "将退役 2 个" in out
    # dry-run 只读 _SQL_OPEN,不写冷却表
    assert all("INSERT" not in sql for sql, _ in conn.sqls)


def test_retire_submits_and_starts_cooldown(monkeypatch):
    rows = [_row(2), _row(3)]
    conn = _patch_common(monkeypatch, rows, state=[])
    submitted = {}
    monkeypatch.setattr(
        heal.feeds, "submit_feed",
        lambda store, ft, skus, workflow: (
            submitted.update({"ft": ft, "skus": list(skus), "wf": workflow}),
            [{"outcome": "submitted", "feed_id": "FR1", "count": len(skus)}],
        )[1])
    recorded = []
    monkeypatch.setattr(heal.product_events, "record_many",
                        lambda c, evs: (recorded.extend(evs), len(evs))[1])
    out = heal.run({"execute": True})
    assert submitted["ft"] == "RETIRE_ITEM" and submitted["wf"] == "sku_locked_heal"
    assert submitted["skus"] == ["B0LOCK0002", "B0LOCK0003"]
    # 冷却记录落库 + retire_submitted 入病历(source=sku_locked_heal)
    inserts = [rows_ for sql, rows_ in conn.sqls if "INSERT INTO listing.retire_cooldown" in sql]
    assert inserts and [r[1] for r in inserts[0]] == ["B0LOCK0002", "B0LOCK0003"]
    assert {e["event"] for e in recorded} == {heal.product_events.RETIRE_SUBMITTED}
    assert all(e["source"] == "sku_locked_heal" for e in recorded)
    assert "退役提交 2 条" in out


def test_multi_slice_results_line_up_with_their_own_rows(monkeypatch):
    """submit_feed 只回 count 不回条目,对位走 api/feeds.iter_result_slices
    —— 错一位就是别人的 SKU 挂着这一片的 feed_id 起算冷却,而且**不报错**。

    ⚠ 中间那片必须也是 submitted 且带**另一个** feed_id:只有落地的片子才
    验得出游标。首片起点恒为 0,尾片若是 failed 又不碰行 —— 两头都验不出
    错位,「切片对位」这条断言会变成摆设。
    """
    rows = [_row(2, asin="B0SLICE001"), _row(3, asin="B0SLICE002"),
            _row(4, asin="B0SLICE003"), _row(5, asin="B0SLICE004")]
    conn = _patch_common(monkeypatch, rows, state=[])
    monkeypatch.setattr(
        heal.feeds, "submit_feed",
        lambda store, ft, skus, workflow: [
            {"outcome": "submitted", "feed_id": "FR_A", "count": 1},
            {"outcome": "submitted", "feed_id": "FR_B", "count": 2},
            {"outcome": "failed", "feed_id": None, "count": 1}])
    recorded = []
    monkeypatch.setattr(heal.product_events, "record_many",
                        lambda c, evs: (recorded.extend(evs), len(evs))[1])
    out = heal.run({"execute": True})
    inserts = [r for sql, rows_ in conn.sqls
               if "INSERT INTO listing.retire_cooldown" in sql for r in rows_]
    assert inserts == [("T1", "B0SLICE001", "FR_A"),
                       ("T1", "B0SLICE002", "FR_B"),
                       ("T1", "B0SLICE003", "FR_B")]    # 各片挂各片的 feed_id
    assert [e["sku"] for e in recorded] == ["B0SLICE001", "B0SLICE002",
                                            "B0SLICE003"]   # 被拒那片不入病历
    assert "退役提交 3 条" in out and "一批 1 条提交被拒" in out


def test_open_cooldown_not_resubmitted_and_failed_needs_human(monkeypatch):
    """冷却在途不重复退役;此前回执失败的点名人工,不自动重试。"""
    rows = [_row(2, asin="B0COOLING"), _row(3, asin="B0FAILED")]
    state = [("T1", "B0COOLING", "FR1", None, "pending", False),
             ("T1", "B0FAILED", "FR2", None, "failed", False)]
    _patch_common(monkeypatch, rows, state)
    monkeypatch.setattr(heal.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("在途/失败都不该再提交")))
    out = heal.run({"execute": True})
    assert "冷却中 1" in out
    assert "人工处置" in out and "B0FAILED" in out


def test_ripe_success_abandons_the_code_and_burns_the_upc(monkeypatch):
    """冷却期满:回执成功 ⇒ **弃码**(reason=sku_locked)+ 清列重上;
    回执失败标 failed 不弃码;未到继续等。

    弃码点 2 是四点中唯一绑回执的一个(锁死的 SKU 可能从未进过 walmart_items,
    没有观测可等)。烧号在 abandon 内部:它按登记簿把码翻回 ASIN 再烧 ——
    裸烧的话第二元是冷却表里的 SKU 码,切码后与 upc_pool 的 (店, ASIN) 领号键
    匹配恒空、**静默失效**,下一轮 claim 把旧号原样复用回来必撞 0101119。
    """
    rows = [_row(2, asin="B0DONE"), _row(3, asin="B0BAD"),
            _row(4, asin="B0WAIT")]
    state = [("T1", "B0DONE", "FR1", None, "pending", True),
             ("T1", "B0BAD", "FR2", None, "pending", True),
             ("T1", "B0WAIT", "FR3", None, "pending", True)]
    conn = _patch_common(monkeypatch, rows, state)
    receipts = {"FR1": {"B0DONE": ("success", "")},
                "FR2": {"B0BAD": ("failed", "ERR_X")},
                "FR3": {}}
    monkeypatch.setattr(heal.feed_track, "item_results",
                        lambda fid: receipts[fid])
    monkeypatch.setattr(heal.feed_track, "poll_feed",
                        lambda store, fid: (None, None))
    cleared = []
    monkeypatch.setattr(heal.listing_sheet, "clear_for_relist",
                        lambda rownums, execute: (cleared.extend(rownums),
                                                  len(rownums))[1])
    monkeypatch.setattr(heal.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("在途冷却不该再提交")))
    abandoned = []
    monkeypatch.setattr(heal.sku_codec, "abandon",
                        lambda c, store, sku, reason: (
                            abandoned.append((store, sku, reason)), True)[1])
    out = heal.run({"execute": True})
    assert cleared == [2]                       # 只有回执成功的行被清列
    closes = [args for sql, args in conn.sqls if "UPDATE listing.retire_cooldown" in sql]
    assert ("cleared", "T1", "B0DONE") in closes
    assert ("failed", "T1", "B0BAD") in closes
    # 弃码只发生在回执成功那一行,原因 = sku_locked(四个弃码点各有各的词)
    assert abandoned == [("T1", "B0DONE", heal.sku_codec.ABANDON_SKU_LOCKED)]
    assert "弃码 1 个" in out and "burned_lock" in out
    assert "清列重上 1 行" in out and "回执未到 1 条" in out and "回执失败 1 条" in out


def test_failed_receipt_never_abandons(monkeypatch):
    """回执失败:标 failed 点名人工,**零弃码零烧号、不清列**。

    写操作永不自动兜底 —— RETIRE 没成功就说明沃尔玛侧那条记录还在、还绑着
    我们的 UPC,弃了码下一轮就是拿新码新号去上一个还活着的 item。
    """
    rows = [_row(2, asin="B0BAD")]
    conn = _patch_common(monkeypatch, rows,
                         state=[("T1", "B0BAD", "FR2", None, "pending", True)])
    monkeypatch.setattr(heal.feed_track, "item_results",
                        lambda fid: {"B0BAD": ("failed", "ERR_X")})
    monkeypatch.setattr(heal.feed_track, "poll_feed",
                        lambda store, fid: (None, None))
    monkeypatch.setattr(heal.listing_sheet, "clear_for_relist",
                        lambda rownums, execute: pytest.fail("失败回执不许清列")
                        if rownums else 0)
    monkeypatch.setattr(heal.sku_codec, "abandon",
                        lambda *a, **k: pytest.fail("失败回执不许弃码"))
    monkeypatch.setattr(heal.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("冷却中不该再提交")))
    out = heal.run({"execute": True})
    assert "回执失败 1 条" in out and "弃码" not in out
    assert ("failed", "T1", "B0BAD") in [
        args for sql, args in conn.sqls if "UPDATE listing.retire_cooldown" in sql]


def test_cooldown_hours_comes_from_the_codec_constant(monkeypatch):
    """不传 -p cooldown_hours 时用的是 `sku_codec.RETIRE_COOLDOWN_HOURS`。

    此前这个数长在 params 默认值里;list_new 的退役冷却闸泛化出第二个消费方
    之后,两处各写一个 24 就是两份真相,一漂就没人说得清冷却到底几小时。
    """
    conn = _patch_common(monkeypatch, [_row(2)], state=[])
    monkeypatch.setattr(heal.feeds, "submit_feed",
                        lambda *a, **k: [{"outcome": "submitted",
                                          "feed_id": "FR1", "count": 1}])
    monkeypatch.setattr(heal.product_events, "record_many", lambda c, evs: len(evs))
    heal.run({"execute": True})
    opens = [args for sql, args in conn.sqls if "retire_cooldown WHERE status" in sql]
    assert opens == [(heal.sku_codec.RETIRE_COOLDOWN_HOURS,)]
    heal.run({"execute": True, "cooldown_hours": "6"})      # 显式传仍然生效
    assert (6,) in [args for sql, args in conn.sqls
                    if "retire_cooldown WHERE status" in sql]


# ── (店铺, SKU) 五个键同源(2026-09 SKU 改造批次 1)────────────────────────

def test_retire_uses_the_sku_column_when_present(monkeypatch):
    """SKU 列有值的行:RETIRE 载荷 / 冷却表 / 病历三处**都**发真码,不是 ASIN。

    sku_plan §3.4 点名最危险的一条:退役发的是 ASIN 就退不到,而沃尔玛只会
    回一个"查无此 SKU",链路照常往下走,行永远卡在 SKU_LOCKED。
    """
    rows = [_row(2, asin="B0LEGACY01"), _row(3, asin="B0NEWCODE1",
                                             sku="A0X1Y2Z3W4V5")]
    conn = _patch_common(monkeypatch, rows, state=[])
    submitted = {}
    monkeypatch.setattr(
        heal.feeds, "submit_feed",
        lambda store, ft, skus, workflow: (
            submitted.update({"skus": list(skus)}),
            [{"outcome": "submitted", "feed_id": "FR1", "count": len(skus)}],
        )[1])
    recorded = []
    monkeypatch.setattr(heal.product_events, "record_many",
                        lambda c, evs: (recorded.extend(evs), len(evs))[1])
    heal.run({"execute": True})
    # 存量行仍是 ASIN(逐字节等价),新码行是真码 —— 三处同源
    assert submitted["skus"] == ["B0LEGACY01", "A0X1Y2Z3W4V5"]
    inserts = [r for sql, rows_ in conn.sqls
               if "INSERT INTO listing.retire_cooldown" in sql for r in rows_]
    assert [r[1] for r in inserts] == ["B0LEGACY01", "A0X1Y2Z3W4V5"]
    assert [e["sku"] for e in recorded] == ["B0LEGACY01", "A0X1Y2Z3W4V5"]


def test_cooldown_keys_and_row_lookup_share_one_source(monkeypatch):
    """冷却表里存的键 = 回表找行用的键 = `row_sku`。

    两侧不同源的后果:在途冷却拦不住(同一 SKU 每轮重复提交 RETIRE_ITEM,
    直接烧配额),或冷却期满找不到行 → 走"只关冷却不清列"降级路径 → 行永久
    卡在 SKU_LOCKED、UPC 永久占用,日志只有一条 info,没人看得见。
    """
    rows = [_row(2, asin="B0NEWCODE1", sku="A0X1Y2Z3W4V5"),
            _row(3, asin="B0COOLING1", sku="A0AAAABBBBCC")]
    state = [("T1", "A0X1Y2Z3W4V5", "FR1", None, "pending", True),
             ("T1", "A0AAAABBBBCC", "FR2", None, "pending", False)]
    conn = _patch_common(monkeypatch, rows, state)
    monkeypatch.setattr(heal.feed_track, "item_results",
                        lambda fid: {"A0X1Y2Z3W4V5": ("success", "")})
    monkeypatch.setattr(heal.feed_track, "poll_feed",
                        lambda store, fid: (None, None))
    cleared = []
    monkeypatch.setattr(heal.listing_sheet, "clear_for_relist",
                        lambda rownums, execute: (cleared.extend(rownums),
                                                  len(rownums))[1])
    abandoned = []
    monkeypatch.setattr(heal.sku_codec, "abandon",
                        lambda c, store, sku, reason: (
                            abandoned.append((store, sku)), True)[1])
    monkeypatch.setattr(heal.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("两行都在冷却中,不该再提交")))
    out = heal.run({"execute": True})
    assert cleared == [2]                    # 按真码找回了自己那一行
    # 弃码键 = 冷却表里那个键(同源);ASIN 那一跳在 abandon 内部走登记簿
    assert abandoned == [("T1", "A0X1Y2Z3W4V5")]
    assert "冷却中 2" in out
