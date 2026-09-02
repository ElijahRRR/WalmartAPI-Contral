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
        if "SELECT DISTINCT store, sku FROM orders.order_lines" in sql:
            self._rows = [(None, s) for s in self.skus]
        elif "catalog.listing_sources" in sql:
            self._rows = []                  # 未登记 ⇒ 回落形态提取
        elif "FROM catalog.walmart_items" in sql:
            self._rows = [(k, v) for k, v in self.item_ids.items()
                          if k in set(args[-1])]
        elif sql.strip().startswith("UPDATE orders.order_lines"):
            self.fills.append((list(args[0]), list(args[1]), list(args[2])))
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
    pairs = [(None, s) for s in cur.skus]
    mapping, buckets = oan.sku_asin.resolve_pairs(_Conn(cur), pairs)
    assert mapping == {(None, "B0ABCDEFGH"): "B0ABCDEFGH",
                       (None, "XKJ-B0GXX75JN5-39.98"): "B0GXX75JN5",
                       (None, "102460018738"): "B08M4D1GMT"}
    assert buckets["numeric_resolved"] == 1
    assert (None, "随便什么") not in mapping   # 提不出的不进映射


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
    assert "可解析 1 个 sku、1 个组合(50.0%)" in out
    assert "-p apply=1" in out


def test_apply_fills_only_resolved(monkeypatch):
    cur = _wire(monkeypatch, _Cur(skus=["B0ABCDEFGH", "XKJ-B0GXX75JN5-39.98",
                                        "坏的"]))
    out = oan.run({"apply": "1"})
    stores, skus, asins = cur.fills[0]
    assert len(stores) == len(skus) == len(asins)     # 三数组逐位对齐
    assert dict(zip(skus, asins)) == {"B0ABCDEFGH": "B0ABCDEFGH",
                                      "XKJ-B0GXX75JN5-39.98": "B0GXX75JN5"}
    assert "全表覆盖率 90/100(90.0%)" in out


def test_sql_only_touches_null_rows():
    """幂等的根据:两条 SQL 都带 `asin IS NULL`,重复跑不会覆盖已填的行。"""
    assert "asin IS NULL" in oan._DISTINCT_SQL
    assert "o.asin IS NULL" in oan._FILL_SQL
    # 空 sku 不进待洗集合(否则会拿空串去 classify,白占一个 other 桶)
    assert "btrim(sku) <> ''" in oan._DISTINCT_SQL
    # 带 store 定位(2026-09-02):同一串 sku 在两家店切码后指向不同产品;
    # IS NOT DISTINCT FROM 与事件账本那份逐字同写法(两条清洗器有对拍测试)
    assert "DISTINCT store, sku" in oan._DISTINCT_SQL
    assert "IS NOT DISTINCT FROM" in oan._FILL_SQL


def test_rules_are_not_reimplemented_here():
    """规则唯一出处是 services/sku_asin —— 本工作流不许自己写正则。

    仓里已有 sku_normalize 走同一套路径;两处各写一份正则,
    规则扩充时漏改没人调的那份不会报错,只会制造"我改了规则"的错觉。
    """
    src = open(oan.__file__, encoding="utf-8").read()
    # 射程是**代码**不是模块 docstring:docstring 里写清"这一跳查的是
    # catalog.listing_sources"正是我们要的文档(与 test_sku_guard 的
    # abandoned_at 那条同一条纪律),扫那个词本身没有意义
    body = src.split('"""', 2)[2]
    assert "re.compile" not in src and "import re" not in src
    # 连倒查那一跳(item_id → 订货号)与形态样本也在 services(2026-08-27 收编):
    # 本工作流只剩 _DISTINCT_SQL / _FILL_SQL 这两条"打哪张表"的真差异
    assert "sku_asin.resolve_pairs" in src and "sku_asin.samples" in src
    assert "_ITEMID_SQL" not in src        # 倒查那一跳的 SQL 也不许再有第二份
    # 登记簿那一跳(2026-09-02 新加的规则)同样只准住在 services:
    # 在这里写一条 JOIN listing_sources 就是第二份规则,漏改一份不报错
    assert "listing_sources" not in body
    assert "resolve_many" not in body      # 工作流只准用批量入口 resolve_pairs


def test_normalize_agrees_with_the_event_ledger_cleaner():
    """与 sku_normalize(事件账本那份)对同一批 sku 必须给出同一个 asin。

    两份清洗器落在两张表上,结果不一致的话,"这个 ASIN 的销量"与
    "这个 ASIN 的事件历史"会指向不同的产品。
    """
    from workflows import sku_normalize as sn
    skus = ["B0ABCDEFGH", "XKJ-B0GXX75JN5-39.98", "A109-B08QF9XLMH-02",
            "102460018738", "随便什么"]
    ids = {"102460018738": "JTZW-B08M4D1GMT-38"}
    pairs = [(None, s) for s in skus]
    mine, _ = oan.sku_asin.resolve_pairs(_Conn(_Cur(skus, ids)), pairs)
    theirs, _ = sn.sku_asin.resolve_pairs(_Conn(_Cur(skus, ids)), pairs)
    assert mine == theirs
    assert mine[(None, "A109-B08QF9XLMH-02")] == "B08QF9XLMH"   # 曾被冤枉的 208 个
    # 两个工作流走的是**同一个函数对象**(2026-08-27 起连倒查那一跳也在
    # services):这才是"不会飘"的根据,各写一份再比对只能比中当天那一次
    assert oan.sku_asin.resolve_pairs is sn.sku_asin.resolve_pairs
    # 两个工作流都只经由 sku_asin,没有各自的规则副本
    assert "re.compile" not in open(sn.__file__, encoding="utf-8").read()


# ── 实时链路自己填,扫尾工作流只管补漏(所有者 2026-08-15 晚追问)──────────

def test_order_sync_fills_asin_at_write_time():
    """新进的订单行**落库当场**就有 asin,不留给后台补。

    否则每次 order_sync 之后,最新的订单——也就是最有价值的选品信号——
    在产品/品牌销量维度里全是空的,直到有人想起来重跑扫尾工作流。
    ⚠ 2026-09-02 起这一跳挪到了**唯一写入口** upsert_order_lines(它要查
    登记簿,得有连接);extract_order_lines 仍是纯函数,守的语义没变。
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

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args=None): self.rows = []
        def executemany(self, sql, seq): self.written = list(seq)
        def fetchall(self): return []

    class _Conn2:
        def __init__(self): self.cur = _C()
        def cursor(self): return self.cur

    conn = _Conn2()
    ol.upsert_order_lines(conn, rows)
    assert conn.cur.written[0]["asin"] == "B0GXX75JN5"


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


def test_missing_column_says_run_db_init_not_a_traceback(monkeypatch):
    """本工作流是 asin 列的第一个消费方,"迁移没应用"一定先从这里炸。

    原始 UndefinedColumn 栈对着列名说话,不告诉人下一步干什么 ——
    而下一步只有一条命令(db_init 幂等)。2026-08-15 生产实测踩到。
    """
    class _Boom(_Cur):
        def execute(self, sql, args=None):
            if "SELECT DISTINCT store, sku" in sql:
                raise RuntimeError('column "asin" does not exist\nLINE 3: ...')
            super().execute(sql, args)

    class _C(_Conn):
        def __init__(self, cur):
            super().__init__(cur)
            self.rolled_back = False

        def rollback(self):
            self.rolled_back = True

    cur = _Boom(skus=[])
    conn = _C(cur)
    monkeypatch.setattr(oan.db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([conn])))
    out = oan.run({})
    assert "db_init" in out and "asin` 列还不存在" in out
    assert conn.rolled_back is True        # 事务 aborted 不回滚,commit 时会再炸


def test_other_db_errors_are_not_swallowed(monkeypatch):
    """只翻译"列不存在"这一种,别的错照抛 —— 把所有 DB 错都翻成
    "去跑 db_init" 会把真故障藏起来。"""
    import pytest

    class _Boom(_Cur):
        def execute(self, sql, args=None):
            raise RuntimeError("connection refused")

    def _gen():                       # 真生成器:iter([...]) 接不住往外抛的异常
        yield _Conn(_Boom([]))
    monkeypatch.setattr(oan.db, "pg_conn", contextlib.contextmanager(_gen))
    with pytest.raises(RuntimeError, match="connection refused"):
        oan.run({})
