"""listing L2a 回归:UPC 池状态机/领号语义/注入校验 + 定价区间。"""

import contextlib

import pytest

from api import feishu
from registry import resources
from registry.resources import Spreadsheet
from services import pricing, upc_pool
from workflows import upc_sync


class _Conn:
    def __init__(self, fetch=None):
        self.sqls = []
        self.fetch = fetch or []
        self.rowcount = 1

    def cursor(self):
        return self

    def execute(self, sql, args=None):
        self.sqls.append((sql, args))
        self._last = sql

    def executemany(self, sql, rows):
        self.sqls.append((sql, list(rows)))

    def fetchall(self):
        if "FOR UPDATE SKIP LOCKED" in self._last:
            return self.fetch
        if "GROUP BY status" in self._last:
            return [("", 5), ("used", 2)]
        return self.fetch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── UPC 池 ───────────────────────────────────────────────────────────────────

def test_normalize_and_prefix_whitelist():
    assert upc_pool.normalize(" 12345678 ") == "000012345678"   # zfill 补前导零
    assert upc_pool.normalize("842565531441") == "842565531441"
    assert upc_pool.is_safe_prefix("012345678905")
    assert upc_pool.is_safe_prefix("842565531441")
    for bad in "2345":      # GS1 特殊用途段,沃尔玛拒收
        assert not upc_pool.is_safe_prefix(bad + "12345678901")


def test_sync_rows_flags_bad_prefix(caplog):
    import logging as _logging
    conn = _Conn()
    with caplog.at_level(_logging.WARNING, logger="services.upc_pool"):
        ok, bad = upc_pool.sync_rows(conn, [("842565531441", "2026-08-07"),
                                            ("212345678901", "2026-08-07"),
                                            ("", "")])
    assert (ok, bad) == (1, 1)
    sql, rows = conn.sqls[0]
    assert "ON CONFLICT (upc) DO NOTHING" in sql            # 幂等,不覆盖状态
    assert ("212345678901", "bad_prefix", "2026-08-07") in rows
    assert any("非法前缀" in m for m in caplog.messages)


def test_claim_uses_skip_locked_and_pads_none(caplog):
    import logging as _logging
    conn = _Conn(fetch=[("000000000017",)])
    with caplog.at_level(_logging.WARNING, logger="services.upc_pool"):
        got = upc_pool.claim(conn, [{"store": "T1", "asin": "B0X"},
                                    {"store": "T1", "asin": "B0Y"}])
    assert got == ["000000000017", None]                     # 池不足补 None
    sel_sql = conn.sqls[0][0]
    assert "FOR UPDATE SKIP LOCKED" in sel_sql and "status = ''" in sel_sql
    upd_sql, upd_rows = conn.sqls[1]
    assert "status = 'claimed'" in upd_sql
    assert upd_rows == [("T1", "B0X", "000000000017")]       # 只更新领到的
    assert any("余量不足" in m for m in caplog.messages)


def test_release_only_three_reasons():
    conn = _Conn()
    assert upc_pool.release(conn, ["u1"], "not_found") == 1
    sql, _ = conn.sqls[0]
    assert "AND status = 'claimed'" in sql                   # 只回收已领态
    with pytest.raises(AssertionError):                      # Unknown 永不回收
        upc_pool.release(conn, ["u1"], "unknown")


def test_upc_sync_workflow_projection(monkeypatch):
    monkeypatch.setattr(resources, "UPC_SHEET",
                        Spreadsheet(name="UPC池", token="TOK", sheet_id="SID",
                                    columns=resources.UPC_SHEET.columns))
    sheet_rows = [["842565531441", "2026-08-07", "", "", "", ""],
                  ["212345678901", "2026-08-07", "", "", "", ""]]
    writes = []
    monkeypatch.setattr(feishu, "sheet_row_count", lambda s: 3)
    monkeypatch.setattr(feishu, "sheet_values", lambda s, rng: sheet_rows)
    monkeypatch.setattr(feishu, "sheet_write_ranges",
                        lambda s, ups: (writes.extend(ups), len(ups))[1])
    from datetime import datetime
    info = {"842565531441": ("used", "T1", "SKU9", datetime(2026, 8, 7)),
            "212345678901": ("bad_prefix", None, None, None)}
    monkeypatch.setattr(upc_pool, "sync_rows", lambda conn, rows: (1, 1))
    monkeypatch.setattr(upc_pool, "lookup", lambda conn, upcs: info)
    monkeypatch.setattr(upc_pool, "pool_stats",
                        lambda conn: {"": 5, "used": 1, "bad_prefix": 1})
    from registry import db as _db
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))

    out = upc_sync.run({})
    w = {rng: vals[0] for rng, vals in writes}
    assert w["C2:F2"] == ["已用", "T1", "SKU9", "2026-08-07"]
    assert w["C3:F3"] == ["非法前缀", "", "", ""]
    assert "新入库 1" in out and "非法前缀 1" in out and "未用 5" in out


# ── 定价 ─────────────────────────────────────────────────────────────────────

def test_price_bands_overlap_prefers_lower():
    # 所有者定稿:FBA 0-30/30-75,FBM 15-80/80-1000;边界/重叠向下兼容
    assert pricing.pick_band("FBA", 30) == "fba_range1"      # 30 用低区间
    assert pricing.pick_band("FBA", 30.01) == "fba_range2"
    assert pricing.pick_band("FBA", 76) is None              # 出界(走默认倍率)
    assert pricing.pick_band("FBM", 80) == "fbm_range1"
    assert pricing.pick_band("FBM", 14) is None
    assert pricing.pick_band("FBM", 999) == "fbm_range2"


def test_parse_multiplier_handles_percent_display(caplog):
    # 2355 行全店误淘汰事故:飞书返回格式化显示值 '275%',必须显式处理
    import logging as _logging
    assert pricing.parse_multiplier("275%") == pytest.approx(2.75)
    assert pricing.parse_multiplier("2.75") == pytest.approx(2.75)
    assert pricing.parse_multiplier(2.75) == pytest.approx(2.75)
    assert pricing.parse_multiplier("") is None
    with caplog.at_level(_logging.WARNING, logger="services.pricing"):
        assert pricing.parse_multiplier("abc") is None       # 失败告警不静默
    assert any("倍率解析失败" in m for m in caplog.messages)


def test_walmart_price_end_to_end():
    mults = {"fba_range1": "275%", "fba_range2": 2.2, "fbm_range1": ""}
    assert pricing.walmart_price("FBA", 10, mults, 0) == 27.5
    assert pricing.walmart_price("FBA", 30, mults, 0) == 82.5  # 边界走低区间倍率
    assert pricing.walmart_price("FBA", 50, mults, 0) == 110.0
    assert pricing.walmart_price("FBM", 20, mults, 0) is None  # 倍率未配置 → 不动
    assert pricing.walmart_price("FBA", "bad", mults, 0) is None


def test_walmart_price_includes_shipping():
    """定价输入是**落地价 = 单价 + 运费**(所有者定稿 2026-08-10)。

    漏掉运费 = 按比成本低的数去乘倍率,越贵的运费亏得越多。
    """
    mults = {"fba_range1": "200%", "fba_range2": "200%"}
    assert pricing.landed_price(10, 2.5) == 12.5
    assert pricing.walmart_price("FBA", 10, mults, 2.5) == 25.0   # (10+2.5)×2
    # 运费把落地价顶进了下一档:28 + 5 = 33 → fba_range2
    assert pricing.pick_band("FBA", 28) == "fba_range1"
    assert pricing.pick_band("FBA", 33) == "fba_range2"


def test_walmart_price_refuses_missing_shipping():
    """运费没采到 ⇒ 落地价算不出来 ⇒ 不定价。**绝不当免运费**——
    当 0 定出来的价偏低,看着还挺正常,两侧都不报错。"""
    mults = {"fba_range1": "275%"}
    assert pricing.landed_price(10, None) is None
    assert pricing.walmart_price("FBA", 10, mults, None) is None
    # 0 是"确认免运费"这条真信息,照常定价
    assert pricing.walmart_price("FBA", 10, mults, 0.0) == 27.5


def test_out_of_band_falls_back_to_default_multiplier():
    """所有者定稿 2026-08-09:价格出界按 300% 定价,不再淘汰。"""
    mults = {"fba_range1": "275%", "fbm_range1": "200%"}
    assert pricing.OUT_OF_BAND_MULTIPLIER == 3.0
    assert pricing.walmart_price("FBA", 200, mults, 0) == 600.0  # FBA 上界 75 外
    assert pricing.walmart_price("FBM", 10, mults, 0) == 30.0     # FBM 下界 15 外
    assert pricing.walmart_price("FBM", 2000, mults, 0) == 6000.0
    # 出界不查表:该店一个倍率都没配也照样出价
    assert pricing.walmart_price("FBA", 200, {}, 0) == 600.0
    # 在区间内但倍率没配 → 仍返 None(配置缺失不该拿默认值蒙混)
    assert pricing.walmart_price("FBA", 10, {}, 0) is None


# ── 上架先注入 UPC(所有者定稿 2026-08-16)+ feed 闭环审计的两处修复 ──────────

def test_upc_injection_lives_in_services_not_in_upc_sync():
    """`list_new` 与 `upc_sync` 共用同一段注入代码(铁律 1:不许互相 import)。

    各写一份迟早飘成"上架看到的池"与"upc_sync 看到的池"不是同一个。
    """
    import inspect

    from services import upc_pool
    from workflows import list_new, upc_sync
    assert "def sync_from_sheet(" in inspect.getsource(upc_pool)
    for m in (list_new, upc_sync):
        src = inspect.getsource(m)
        assert "upc_pool.sync_from_sheet" in src
        assert "from workflows" not in src and "import workflows" not in src


def test_list_new_injects_upc_before_the_gate_chain(monkeypatch):
    """注入必须排在闸门链之前 —— 领号是第 ⑦ 道闸,刚贴进表格的号这轮就要能用。"""
    import inspect

    from workflows import list_new
    src = inspect.getsource(list_new.run)
    assert src.index("_sync_upc(") < src.index("_load_gate_state()")


def test_list_new_upc_injection_failure_never_blocks_listing(monkeypatch):
    """飞书挂了不该把整条上架链拖下水 —— 但必须**说出来**。"""
    from services import upc_pool
    from workflows import list_new

    def boom(conn):
        raise RuntimeError("飞书 502")

    monkeypatch.setattr(upc_pool, "sync_from_sheet", boom)
    import contextlib

    from registry import db

    @contextlib.contextmanager
    def _open():
        yield object()

    monkeypatch.setattr(db, "pg_conn", _open)
    lines = []
    list_new._sync_upc(True, lines)         # 不抛
    assert "UPC 注入失败" in lines[0] and "飞书 502" in lines[0]
    # dry-run 不写库,但要点明 no_upc 可能偏多(否则两次跑对不上账)
    lines2 = []
    list_new._sync_upc(False, lines2)
    assert "dry-run 跳过 UPC 注入" in lines2[0] and "no_upc" in lines2[0]


def test_dedup_never_writes_a_product_event():
    """⚠ dedup 挂旧 feed_id、这一轮什么都没提交 —— 记了就是幽灵事件。

    2026-08-16 feed 闭环审计:`product_clear` 与 `sku_locked_heal` 曾把产品事件
    写在 `outcome in ("submitted","dedup")` 的同一分支里,而
    `catalog.product_risk` 的 delete_times/retire_times 直接数它 ——
    "这个 SKU 被删过几次"于是不再是事实。其余四个提交点一直是对的。

    检法:每个 `record_many(` 往上找最近的 `res["outcome"]` 判断,
    那一行里不许出现 dedup。
    """
    import inspect

    from workflows import (list_new, maintenance, match_listing,
                           problem_product_cleanup, product_clear,
                           sku_locked_heal)
    for m in (list_new, match_listing, maintenance, problem_product_cleanup,
              product_clear, sku_locked_heal):
        lines = inspect.getsource(m).splitlines()
        for i, ln in enumerate(lines):
            if "product_events.record_many(" not in ln:
                continue
            gate = next((lines[j] for j in range(i, -1, -1)
                         if 'res["outcome"]' in lines[j]), None)
            if gate is None:
                continue        # 不在 feed 提交分支里(如 catalog_sync 类写法)
            assert "dedup" not in gate, (
                f"{m.__name__} 第 {i + 1} 行:产品事件写在了含 dedup 的分支里"
                f" —— {gate.strip()!r}")
