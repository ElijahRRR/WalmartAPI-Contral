"""A1.5 订单 ASIN 归一回归:四形态解析、留 NULL 不猜、幂等、只补 NULL。"""

import contextlib

from services import sku_asin
from workflows import order_asin_normalize as oan


class _Cur:
    def __init__(self, skus, item_ids=None):
        self.skus, self.item_ids = skus, item_ids or {}
        self.fills: list = []
        self._rows: list = []

    def execute(self, sql, args=None):
        if "SELECT DISTINCT sku FROM orders.order_lines" in sql:
            self._rows = [(s,) for s in self.skus]
        elif "FROM catalog.walmart_items" in sql:
            self._rows = [(k, v) for k, v in self.item_ids.items()
                          if k in set(args[0])]
        elif sql.strip().startswith("UPDATE orders.order_lines"):
            self.fills.append((list(args[0]), list(args[1])))
            self.rowcount = len(args[0])
        elif "count(*) FILTER" in sql:
            self._rows = [(100, 90)]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


def _wire(monkeypatch, cur):
    monkeypatch.setattr(oan.db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([_Conn(cur)])))
    return cur


def test_resolves_the_four_sku_forms(monkeypatch):
    """裸 ASIN / 三段式 / 纯数字倒查 / 其他 —— 规则本身在 services/sku_asin,
    这里只验证本工作流把四种形态都走对了路。"""
    cur = _wire(monkeypatch, _Cur(
        skus=["B0ABCDEFGH",                  # 裸 ASIN
              "XKJ-B0GXX75JN5-39.98",        # 三段式
              "102460018738",                # 纯数字 → 倒查
              "随便什么"],                    # 其他形态
        item_ids={"102460018738": "JTZW-B08M4D1GMT-38"}))
    mapping, buckets = oan._resolve(_Conn(cur), [s for s in cur.skus])
    assert mapping == {"B0ABCDEFGH": "B0ABCDEFGH",
                       "XKJ-B0GXX75JN5-39.98": "B0GXX75JN5",
                       "102460018738": "B08M4D1GMT"}
    assert buckets["numeric_resolved"] == 1
    assert "随便什么" not in mapping          # 提不出的不进映射


def test_unresolvable_stays_null_and_is_never_backfilled_with_raw_sku(monkeypatch):
    """提不出的**留 NULL,绝不拿 sku 原文当 asin**。

    拿原文兜底比留空更坏:三段式与纯数字直连采集库**永远查空**,
    那时它看起来像"这个产品一单没卖过"——一个静默的错误信号,
    而 NULL 至少能被 `asin IS NOT NULL` 诚实地过滤掉。
    """
    cur = _wire(monkeypatch, _Cur(skus=["随便什么", "ALSO-BAD"]))
    out = oan.run({"apply": "1"})
    assert cur.fills == []                    # 一行都不写
    assert "解析不了 2 个 sku" in out and "留 NULL 不猜" in out
    assert "不影响店×SKU 维度" in out


def test_preview_writes_nothing(monkeypatch):
    cur = _wire(monkeypatch, _Cur(skus=["B0ABCDEFGH", "坏的"]))
    out = oan.run({})
    assert cur.fills == [] and "预览" in out
    assert "可解析 1 个(50.0%)" in out
    assert "-p apply=1" in out


def test_apply_fills_only_resolved(monkeypatch):
    cur = _wire(monkeypatch, _Cur(skus=["B0ABCDEFGH", "XKJ-B0GXX75JN5-39.98",
                                        "坏的"]))
    out = oan.run({"apply": "1"})
    skus, asins = cur.fills[0]
    assert dict(zip(skus, asins)) == {"B0ABCDEFGH": "B0ABCDEFGH",
                                      "XKJ-B0GXX75JN5-39.98": "B0GXX75JN5"}
    assert "全表覆盖率 90/100(90.0%)" in out


def test_sql_only_touches_null_rows():
    """幂等的根据:两条 SQL 都带 `asin IS NULL`,重复跑不会覆盖已填的行。"""
    assert "asin IS NULL" in oan._DISTINCT_SQL
    assert "o.asin IS NULL" in oan._FILL_SQL
    # 空 sku 不进待洗集合(否则会拿空串去 classify,白占一个 other 桶)
    assert "btrim(sku) <> ''" in oan._DISTINCT_SQL


def test_rules_are_not_reimplemented_here():
    """规则唯一出处是 services/sku_asin —— 本工作流不许自己写正则。

    仓里已有 sku_normalize 走同一套路径;两处各写一份正则,
    规则扩充时漏改没人调的那份不会报错,只会制造"我改了规则"的错觉。
    """
    src = open(oan.__file__, encoding="utf-8").read()
    assert "re.compile" not in src and "import re" not in src
    assert "sku_asin.extract_asin" in src and "sku_asin.classify" in src


def test_normalize_agrees_with_the_event_ledger_cleaner():
    """与 sku_normalize(事件账本那份)对同一批 sku 必须给出同一个 asin。

    两份清洗器落在两张表上,结果不一致的话,"这个 ASIN 的销量"与
    "这个 ASIN 的事件历史"会指向不同的产品。
    """
    from workflows import sku_normalize as sn
    skus = ["B0ABCDEFGH", "XKJ-B0GXX75JN5-39.98", "A109-B08QF9XLMH-02",
            "102460018738", "随便什么"]
    ids = {"102460018738": "JTZW-B08M4D1GMT-38"}
    mine, _ = oan._resolve(_Conn(_Cur(skus, ids)), skus)
    theirs, _ = sn._resolve(_Conn(_Cur(skus, ids)), skus)
    assert mine == theirs
    assert mine["A109-B08QF9XLMH-02"] == "B08QF9XLMH"   # 曾被过严规则冤枉的那 208 个
    # 两个工作流都只经由 sku_asin,没有各自的规则副本
    assert "re.compile" not in open(sn.__file__, encoding="utf-8").read()


# ── 实时链路自己填,扫尾工作流只管补漏(所有者 2026-08-15 晚追问)──────────

def test_order_sync_fills_asin_at_write_time():
    """新进的订单行**落库当场**就有 asin,不留给后台补。

    否则每次 order_sync 之后,最新的订单——也就是最有价值的选品信号——
    在产品/品牌销量维度里全是空的,直到有人想起来重跑扫尾工作流。
    """
    from services import order_lines as ol
    assert "asin" in ol._ORDER_LINE_COLS
    rows = ol.extract_order_lines("A085", {
        "purchaseOrderId": "108906521136562",
        "customerOrderId": "C1", "orderDate": 1700000000000,
        "orderLines": {"orderLine": [{
            "lineNumber": "1",
            "item": {"sku": "XKJ-B0GXX75JN5-39.98", "productName": "T"},
            "orderLineQuantity": {"amount": "1"},
            "orderLineStatuses": {"orderLineStatus": [{"status": "Shipped"}]},
        }]}})
    assert rows[0]["asin"] == "B0GXX75JN5"


def test_resync_never_wipes_an_asin_the_sweeper_resolved():
    """⚠ 纯数字 sku 在同步侧提不出 asin(要查 walmart_items),裸写
    `EXCLUDED.asin` 会把扫尾工作流填好的值**冲回 NULL** —— 每轮同步抹一次,
    那一列永远填不满。必须 COALESCE 守着。
    """
    from services import order_lines as ol
    assert ol._ASIN_GUARD == "COALESCE(EXCLUDED.asin, t.asin)"
    # 纯数字形态在纯 Python 侧确实提不出 —— 这正是要 guard 的原因
    assert sku_asin.extract_asin("102460018738") is None


def test_history_import_also_fills_asin():
    """历史导入同样当场填,否则 15 万行全靠扫尾补一遍。"""
    from workflows import order_history_import as ohi
    assert "asin" in ohi._INSERT and "%(asin)s" in ohi._INSERT
    rows, _ = ohi.parse_rows(
        ["合并键", "统一订单日期", "店铺名称", "SKU", "商品标题", "数量", "销售额"],
        [["108906521136562B0BC6ZBD7M", "2024-03-05", "S", "B0BC6ZBD7M",
          "T", "1", "23.1"]])
    assert rows[0]["asin"] == "B0BC6ZBD7M"
