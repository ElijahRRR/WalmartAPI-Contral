"""alloc_push:把已落占用的货追加进飞书上架表(A/B 两列)。

⚠ 这条工作流会往**运营天天在看的表**里追加待办。写多了人要一行行删,
写重了运营会把同一个货上两遍。所以每条测试盯的都是"什么不该被推进去"。
"""

import contextlib

import pytest

from services import claims, listing_sheet
from workflows import alloc_push as wf


class _Cur:
    def __init__(self, online):
        self._online = online
        self._rows = []

    def execute(self, sql, args=None):
        self._rows = list(self._online)

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, online):
        self._online = online

    def cursor(self):
        return _Cur(self._online)


def _wire(monkeypatch, held, live, online=(), sheet_rows=(), appended=None):
    monkeypatch.setattr(wf.stores_svc, "enabled_names", lambda: set(live))
    monkeypatch.setattr(wf.claims, "load_active", lambda conn, kind: dict(held))
    monkeypatch.setattr(wf.db, "pg_conn",
                        lambda *a, **k: contextlib.nullcontext(_Conn(online)))
    monkeypatch.setattr(listing_sheet, "read_rows",
                        lambda upto=None: list(sheet_rows))
    # LISTING_SHEET 是 frozen dataclass,不能给它打属性补丁 —— 换一份填好
    # token/sheet_id 的副本,`require()` 才不会抛"尚未登记"
    import dataclasses
    monkeypatch.setattr(
        listing_sheet.resources, "LISTING_SHEET",
        dataclasses.replace(listing_sheet.resources.LISTING_SHEET,
                            token="tk", sheet_id="sid"))
    monkeypatch.setattr(listing_sheet.feishu, "sheet_write_ranges",
                        lambda sheet, ranges:
                        None if appended is None else appended.extend(ranges))
    monkeypatch.setattr(listing_sheet.feishu, "sheet_ensure_rows",
                        lambda sheet, n: n)


# ── 三道筛,少一道都会出事 ────────────────────────────────────────────

def test_products_already_online_are_not_pushed(monkeypatch):
    """★ 这一条最要紧。

    `alloc_backfill` 是**拿在架商品倒推出来的占用** —— 台账里绝大多数条目的货
    早就在架上卖着。不筛这一条,首跑会把几万条已上架的产品当成待办灌进上架表。
    """
    _wire(monkeypatch,
          held={"B0ONLINE01": "A085", "B0TODO0001": "A085"},
          live={"A085"},
          # 真实 SKU 形态:前缀-ASIN-序号(services/sku_asin._WRAPPED);
          # 第三列是登记簿 amz 键(未登记 ⇒ None ⇒ 回落模式提取)
          online=[("A085", "WM-B0ONLINE01-1", None)])
    out = wf.run({"execute": False})
    assert "将追加 1 行" in out and "已在架 1" in out


def test_online_set_reads_the_registry_key(monkeypatch):
    """「已在架」按**身份键**算:登记簿优先,模式提取只兜存量(0a-21)。

    不透明码经模式提取必返 None ⇒ 集合恒空 ⇒ **已在架的品被重新派工**,而本
    工作流 DANGEROUS=True、直接写运营天天看的上架表。
    """
    _wire(monkeypatch,
          held={"B0ONLINE01": "A085", "B0TODO0001": "A085"},
          live={"A085"},
          online=[("A085", "AZZZZ234567", "B0ONLINE01")])
    out = wf.run({"execute": False})
    assert "将追加 1 行" in out and "已在架 1" in out


def test_online_set_still_excludes_retired(monkeypatch):
    """反向钉死:0a **不做**决策 C 的第二步(去掉 lifecycle 过滤)。

    去掉它在存量数据上就立刻生效(catalog_sync 显式扫一轮 RETIRED,那批行
    missing_since 为 NULL),是真行为变化,随批次 2 的弃码点接线一起上。
    批次 2 对齐口径时再翻转这条断言。
    """
    assert "coalesce(upper(w.lifecycle_status), 'ACTIVE') = 'ACTIVE'" \
        in wf._SQL_ONLINE


def test_abandoned_predicate_is_inert_while_no_code_is_abandoned(monkeypatch):
    """`ls.abandoned_at IS NULL` 在批次 2 之前恒真:全库该列 NULL + LEFT JOIN。

    提前落地是为了让写侧切换只改一处;它现在**不筛掉任何一行**。
    """
    assert "ls.abandoned_at IS NULL" in wf._SQL_ONLINE
    _wire(monkeypatch,
          held={"B0ONLINE01": "A085"},
          live={"A085"},
          online=[("A085", "B0ONLINE01", "B0ONLINE01")])   # abandoned_at 为 NULL
    out = wf.run({"execute": False})
    assert "已在架 1" in out


def test_disabled_stores_get_no_work(monkeypatch):
    """停用店的占用**保持不释放**(那是有意的),但不能给它派活。"""
    _wire(monkeypatch, held={"B0A": "A085", "B0B": "Z001已停用"},
          live={"A085"})
    out = wf.run({"execute": False})
    assert "将追加 1 行" in out and "停用店 1" in out


def test_asins_already_in_the_sheet_are_deduped(monkeypatch):
    """同一个 ASIN 重复派工,运营会把它上两遍。"""
    _wire(monkeypatch, held={"B0A": "A085", "B0B": "A085"}, live={"A085"},
          sheet_rows=[{"store": "A085", "asin": "B0A", "rownum": 2}])
    out = wf.run({"execute": False})
    assert "将追加 1 行" in out and "上架表里已有 1" in out


def test_a_credential_read_failure_refuses_instead_of_degrading(monkeypatch):
    """⚠ 分不清在营/停用时**不许降级**:给一家不做了的店派活,运营会照着上架。"""
    def boom():
        raise RuntimeError("飞书超时")
    monkeypatch.setattr(wf.stores_svc, "enabled_names", boom)
    out = wf.run({"execute": False})
    assert out.startswith("⛔") and "拒绝推送" in out


# ── 只写 A/B ──────────────────────────────────────────────────────────

def test_only_columns_a_and_b_are_written(monkeypatch):
    """§9.2 的列权责:分配器只写 A/B,E 留空即「待审」。

    ⚠ 顺手写 E=pass 就是**伪造审核结论**。而且伪造也没用 —— 上架闸读的是
    `catalog.products`,只会骗到人眼。
    """
    got = []
    _wire(monkeypatch, held={"B0A": "A085"}, live={"A085"}, appended=got)
    wf.run({"execute": True})
    assert len(got) == 1
    rng, vals = got[0]
    assert rng.startswith("A2:B2"), rng
    assert vals == [["A085", "B0A"]]


def test_the_workflow_never_touches_the_audit_column():
    """连源码里都不许出现写 E 列的 range —— 这条比行为测试更难绕过。"""
    import inspect
    src = inspect.getsource(wf) + inspect.getsource(
        listing_sheet.append_assignments)
    for bad in ('"E', "'E", "E{", ":E"):
        assert f"{bad}" not in src.replace("E 列", "").replace("E=", ""), bad


# ── 追加位置 ──────────────────────────────────────────────────────────

def test_append_starts_after_the_last_non_empty_row(monkeypatch):
    """从**表里算**下一空行,不存水位 —— 上架表是运营在手工编辑的。

    存水位的两个坏处:有人删了几十行 ⇒ 水位停在表尾之外,追加会在中间留一片
    空行;水位与表一旦不一致,谁也说不清该信哪个。
    """
    got = []
    _wire(monkeypatch, held={"B0NEW": "A085"}, live={"A085"},
          sheet_rows=[{"store": "A085", "asin": "B0OLD", "rownum": 77}],
          appended=got)
    wf.run({"execute": True})
    assert got[0][0].startswith("A78:B78")


def test_an_empty_sheet_starts_at_row_two(monkeypatch):
    got = []
    _wire(monkeypatch, held={"B0A": "A085"}, live={"A085"}, appended=got)
    wf.run({"execute": True})
    assert got[0][0].startswith("A2:B2")


def test_a_partial_failure_self_heals_on_rerun(monkeypatch):
    """★ 不记账也不会重复写:重跑整读一遍表,已写进去的自然被去重掉。

    这正是"从表里算位置"换来的 —— `maint_sheet` 那种水位模式必须在失败时
    小心地落一半水位,这里不用。
    """
    got = []
    _wire(monkeypatch, held={"B0A": "A085", "B0B": "A085"}, live={"A085"},
          sheet_rows=[{"store": "A085", "asin": "B0A", "rownum": 2}],
          appended=got)
    wf.run({"execute": True})
    assert [v for _, vals in got for v in vals] == [["A085", "B0B"]]


# ── 安全阀 ────────────────────────────────────────────────────────────

def test_limit_says_how_many_it_left_behind(monkeypatch):
    """截断必须说破 —— 静默截断读起来像"就这么多了"。"""
    _wire(monkeypatch, held={f"B0{i:04d}": "A085" for i in range(10)},
          live={"A085"})
    out = wf.run({"execute": False, "limit": 3})
    assert "将追加 3 行" in out and "还剩 7 条没推" in out


def test_dry_run_writes_nothing(monkeypatch):
    got = []
    _wire(monkeypatch, held={"B0A": "A085"}, live={"A085"}, appended=got)
    out = wf.run({"execute": False})
    assert got == [] and out.startswith("🧪")


def test_dangerous_is_declared():
    """会往运营天天在看的表里追加待办 —— 缺省即真跑,得让 cli 知道它危险。"""
    assert wf.DANGEROUS is True
