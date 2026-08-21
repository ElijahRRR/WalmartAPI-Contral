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
        if "DISTINCT ON (store, asin)" in self._last:
            return getattr(self, "reuse", [])    # claim 的原号复用查询
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
    assert "DISTINCT ON (store, asin)" in conn.sqls[0][0]    # 先查可复用旧号
    sel_sql = conn.sqls[1][0]
    assert "FOR UPDATE SKIP LOCKED" in sel_sql and "status = ''" in sel_sql
    upd_sql, upd_rows = conn.sqls[2]
    assert "status = 'claimed'" in upd_sql
    assert upd_rows == [("T1", "B0X", "000000000017")]       # 只更新领到的
    assert any("余量不足" in m for m in caplog.messages)


def test_claim_reuses_prior_upc_for_same_store_asin():
    """O=FAILED 重试不换号(2026-08-19 生产实证 ERR_EXT_DATA_0101211):
    SKU 在沃尔玛端绑死首个 UPC,换新号重发必败还白烧号。同 (店,ASIN)
    已有 claimed/used 的号必须原号复用,新号只发给真正的新行。"""
    conn = _Conn(fetch=[("000000000024",)])
    conn.reuse = [("T1", "B0OLD", "000000000017")]
    got = upc_pool.claim(conn, [{"store": "T1", "asin": "B0OLD"},
                                {"store": "T1", "asin": "B0NEW"}])
    assert got == ["000000000017", "000000000024"]           # 旧行复用,新行新号
    upd_sql, upd_rows = conn.sqls[2]
    assert upd_rows == [("T1", "B0NEW", "000000000024")]     # 复用行不再 UPDATE


def test_burn_for_retire_marks_conflict():
    """RETIRE 成功后旧号永久弃用(标 conflict):不烧的话 claim 的复用逻辑
    会把旧号还给同 (店,ASIN),"清列重上领新号"就成了空话。"""
    conn = _Conn()
    assert upc_pool.burn_for_retire(conn, [("T1", "B0X")]) == 1
    sql, _ = conn.sqls[0]
    assert "status = 'conflict'" in sql
    assert "status IN ('claimed', 'used')" in sql
    assert upc_pool.burn_for_retire(conn, []) == 0


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
    # 所有者定稿:FBA 0-30/30-1000,FBM 15-80/80-1000;边界/重叠向下兼容
    assert pricing.pick_band("FBA", 30) == "fba_range1"      # 30 用低区间
    assert pricing.pick_band("FBA", 30.01) == "fba_range2"
    # 2026-08-21 所有者把 FBA 区间2 上界从 75 抬到 1000:76 此前出界走 300%,
    # 现在吃 fba区间2 的倍率(该店没配那一格就变成不定价——见下面那条用例)
    assert pricing.pick_band("FBA", 76) == "fba_range2"
    assert pricing.pick_band("FBA", 1001) is None            # 出界(走默认倍率)
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
    assert pricing.walmart_price("FBA", 2000, mults, 0) == 6000.0  # FBA 上界 1000 外
    assert pricing.walmart_price("FBM", 10, mults, 0) == 30.0     # FBM 下界 15 外
    assert pricing.walmart_price("FBM", 2000, mults, 0) == 6000.0
    # 出界不查表:该店一个倍率都没配也照样出价
    assert pricing.walmart_price("FBA", 2000, {}, 0) == 6000.0
    # 在区间内但倍率没配 → 仍返 None(配置缺失不该拿默认值蒙混)
    assert pricing.walmart_price("FBA", 10, {}, 0) is None


def test_fba_band2_upper_bound_moved_to_1000():
    """2026-08-21 所有者:FBA 区间2 上界 75 → 1000。

    **换版的代价钉在这里**:75~1000 这一段此前出界、走 300% 兜底照样出价;
    现在它落进 fba区间2,该店那一格没填就变成**不定价**(上架不上、维护不改)。
    这是"配置缺失不拿默认值蒙混"的正确表现,但换版当天会多出一批跳过,
    别把它当成故障——去限额表把 fba区间2 填上就是了。
    """
    assert pricing.PRICE_BANDS["FBA"] == [(0, 30, "fba_range1"),
                                          (30, 1000, "fba_range2")]
    配了 = {"fba_range1": "275%", "fba_range2": "250%"}
    没配 = {"fba_range1": "275%"}
    assert pricing.walmart_price("FBA", 200, 配了, 0) == 500.0   # 200×250%
    assert pricing.walmart_price("FBA", 200, 没配, 0) is None    # ← 换版的代价
    assert pricing.walmart_price("FBA", 2000, 没配, 0) == 6000.0  # 1000 外仍兜底


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


def test_upc_writeback_runs_after_listing_not_beside_injection():
    """⚠ 回写必须在上架**之后** —— 放注入旁边回写的是上一轮的状态。

    表面看也在动,实际上你永远看不到刚刚这一轮消耗了哪些号
    (所有者定稿 2026-08-16:upc_sync 并进上架)。
    """
    import inspect

    from workflows import list_new
    src = inspect.getsource(list_new.run)
    assert src.index("_sync_upc(") < src.index("_load_gate_state()")
    assert src.index("_writeback_upc(") > src.index("_load_gate_state()")


def test_upc_writeback_is_skipped_on_dry_run_and_never_blocks(monkeypatch):
    from services import upc_pool
    from workflows import list_new

    lines = []
    list_new._writeback_upc(False, lines)        # dry-run:一个字都不写
    assert lines == []

    monkeypatch.setattr(upc_pool, "sync_from_sheet",
                        lambda conn: (_ for _ in ()).throw(RuntimeError("飞书 502")))
    import contextlib

    from registry import db

    @contextlib.contextmanager
    def _open():
        yield object()

    monkeypatch.setattr(db, "pg_conn", _open)
    list_new._writeback_upc(True, lines)         # 不抛
    assert "回写失败" in lines[0] and "feed 已提交" in lines[0]


# ── 飞书通知:应用直发(工作项 C)────────────────────────────────────────────

def test_receive_type_follows_the_legacy_prefix_rule():
    """前缀判型逐字沿用旧系统(legacy_survey:1818,notify.py:137)。"""
    from api import feishu
    assert feishu._receive_type("ou_36c5f91668c42a735e7b9d4ae74eedc1") == "open_id"
    assert feishu._receive_type("oc_abc") == "chat_id"
    assert feishu._receive_type("someone@example.com") == "email"
    # ⚠ 手机号飞书**没有**这一档,必须先换 open_id
    assert feishu._receive_type("17882211182") == "mobile"


def test_mobile_is_resolved_to_open_id_before_sending(monkeypatch):
    from api import feishu
    feishu._open_id_cache.clear()
    calls = []

    def fake_call(method, path, *, json_body=None, params=None, timeout=60):
        calls.append((path, params, json_body))
        if "batch_get_id" in path:
            return {"user_list": [{"user_id": "ou_resolved"}]}
        return {}

    monkeypatch.setattr(feishu, "_call", fake_call)
    monkeypatch.setattr(feishu.resources, "feishu_notify_to", lambda: "17882211182")
    assert feishu.notify("测试") is True
    assert "batch_get_id" in calls[0][0] and calls[0][2] == {"mobiles": ["17882211182"]}
    send_path, send_params, send_body = calls[1]
    assert send_path == "/open-apis/im/v1/messages"
    assert send_params == {"receive_id_type": "open_id"}
    assert send_body["receive_id"] == "ou_resolved"
    # ⚠ content 必须是**字符串化的 JSON**,传对象飞书直接拒
    assert isinstance(send_body["content"], str)
    import json as _json
    assert _json.loads(send_body["content"]) == {"text": "测试"}
    # 换 ID 只做一次(进程内缓存)
    feishu.notify("再来一条")
    assert sum(1 for c in calls if "batch_get_id" in c[0]) == 1


def test_notify_falls_back_to_webhook_then_to_log(monkeypatch, caplog):
    """三条路依次退:应用 → webhook → 只记日志。切换期不把通知打断。"""
    import logging as _logging

    from api import feishu
    monkeypatch.setattr(feishu.resources, "feishu_notify_to", lambda: "ou_x")
    monkeypatch.setattr(feishu, "_call",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("没权限")))
    posted = []

    class _Resp:
        def json(self):
            return {"code": 0}

    class _C:
        def post(self, url, json=None, timeout=None):
            posted.append(url)
            return _Resp()

    monkeypatch.setattr(feishu, "_http", lambda: _C())
    monkeypatch.setattr(feishu.resources, "feishu_webhook_url", lambda: "https://hook")
    assert feishu.notify("x") is True and posted == ["https://hook"]

    monkeypatch.setattr(feishu.resources, "feishu_webhook_url", lambda: None)
    with caplog.at_level(_logging.INFO, logger="api.feishu"):
        assert feishu.notify("x") is False
    assert any("均未配置" in m for m in caplog.messages)
