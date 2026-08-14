"""审核接线层测试(评审 I-8 补钉:P0/P1 发现全在这些零测试文件里)。

覆盖:resolve_pt 的 pt_meta 闸与优先级、audit_store 词表/桩值、
_pick_where 四态与参数白名单、黑名单中心镜像空读/骤缩护栏、
ASIN 历史导入解析、行适配。
"""

import json
from types import SimpleNamespace

import pytest

from services import audit_rules, audit_store
from services.audit_models import AuditOutcome, L1Info, ProductInfo
from workflows import product_audit
from workflows.asin_blacklist_import import parse_asin_lines
from workflows.risk_sync import _sync_column_blacklist


def _ctx(**kw):
    base = dict(phase0_sellers=frozenset(), phase0_asins=frozenset(),
                phase0_cats=frozenset(), brand_blacklist={},
                pt_meta={}, pt_spec={}, ac_automaton=None, mega=[],
                nrtl_small=[], nrtl_whole=[], nice_mapping={},
                nice_default=[], uspto=None)
    base.update(kw)
    return audit_rules.AuditContext(**base)


META = {"GoodPT": {"walmart_category": "Home", "walmart_ptg": None,
                   "access_state": "普通商品", "zh_can_do": "是",
                   "requirements": "", "notes": ""}}


# ── resolve_pt:pt_meta 闸与两级优先级(评审 P0-1)────────────────────────────

def test_resolve_pt_confirmed_wins_and_fills_category():
    ctx = _ctx(pt_meta=META, walmart_confirmed={"B0A": "GoodPT"},
               catmap={"Cat > Path": "GoodPT"})
    l1 = audit_rules.resolve_pt(ProductInfo(asin="B0A"), ctx)
    assert l1.walmart_product_type == "GoodPT"
    assert l1.pt_source == "walmart_confirmed"
    assert l1.walmart_category == "Home"


def test_resolve_pt_dead_pt_falls_to_pending_not_pass():
    """废弃 PT(不在 pt_meta)绝不直出——四条硬规则会集体失明产出假 pass。"""
    ctx = _ctx(pt_meta=META, walmart_confirmed={"B0B": "Office Chairs"})
    l1 = audit_rules.resolve_pt(ProductInfo(asin="B0B"), ctx)
    assert l1.walmart_product_type is None and l1.pt_source is None


def test_resolve_pt_catmap_strips_and_misses():
    ctx = _ctx(pt_meta=META, catmap={"Cat > Path": "GoodPT"})
    l1 = audit_rules.resolve_pt(
        ProductInfo(asin="B0C", amazon_category_path="  Cat > Path  "), ctx)
    assert l1.walmart_product_type == "GoodPT" and l1.pt_source == "map_direct"
    miss = audit_rules.resolve_pt(
        ProductInfo(asin="B0D", amazon_category_path="Other > Path"), ctx)
    assert miss.walmart_product_type is None


def test_audit_one_pending_when_pt_unresolved():
    ctx = _ctx(pt_meta=META)
    out = audit_rules.audit_one(ProductInfo(asin="B0E", title="widget"), ctx)
    assert out.verdict == "pending" and out.stage_stopped_at == "L1"


def test_audit_one_phase0_blocked_stub():
    ctx = _ctx(pt_meta=META, phase0_asins=frozenset({"B0F"}))
    out = audit_rules.audit_one(ProductInfo(asin="B0F", title="w"), ctx)
    assert out.verdict == "reject" and out.stage_stopped_at == "L0"
    assert out.score_final == 0
    assert out.l1.walmart_product_type == "(phase0_blocked)"


# ── audit_store:词表映射与桩值(spec_shortcut §4)─────────────────────────────

def test_real_pt_excludes_stub():
    o = AuditOutcome(asin="B0", verdict="reject", score_final=0,
                     stage_stopped_at="L0",
                     l1=L1Info(walmart_product_type="(phase0_blocked)"))
    assert audit_store.real_pt(o) is None


def test_event_row_mapping():
    def _o(v):
        return AuditOutcome(asin="B0", verdict=v, score_final=100,
                            stage_stopped_at=None,
                            l1=L1Info(walmart_product_type="GoodPT"))
    assert audit_store.event_row(_o("pass"), 1)["event"] == "audit_passed"
    assert audit_store.event_row(_o("reject"), 1)["event"] == "audit_rejected"
    assert audit_store.event_row(_o("pending"), 1) is None   # 过渡态不进病历


def test_write_conclusion_status_words(monkeypatch):
    captured = {}

    class _Conn:
        def execute(self, sql, params):
            captured.update(params)
    o = AuditOutcome(asin="B0", verdict="pass", score_final=100,
                     stage_stopped_at=None,
                     l1=L1Info(walmart_product_type="GoodPT"))
    audit_store.write_conclusion(_Conn(), o)
    assert captured["status"] == "approved"       # 两套词表显式映射
    assert captured["walmart_pt"] == "GoodPT"
    assert captured["reason"] is None


# ── _pick_where 四态与参数白名单(评审 P1-4/I-6)──────────────────────────────

def test_pick_where_four_states():
    w, e = product_audit._pick_where({})
    assert "IS NULL OR" in w and "interval '1 day'" in w   # pending 退避
    w, e = product_audit._pick_where({"asins": "B0A, B0B"})
    assert e["asins"] == ["B0A", "B0B"]
    w, e = product_audit._pick_where({"force_rerun": "b.2026-08-13.1"})
    assert "audit_version IS DISTINCT FROM" in w
    w, _ = product_audit._pick_where({"mode": "backfill"})
    assert w == "p.audit_status IS NULL"


def test_pick_where_rejects_unknown_params():
    """静默吞参数 = '全量重审跑完了'的假象,宁炸不吞。"""
    with pytest.raises(ValueError, match="未识别参数"):
        product_audit._pick_where({"force_rurn": "x"})     # 手滑拼错


# ── _sync_column_blacklist 护栏(评审 P0-2;黑名单中心定稿 2026-08-13)────────

class _BLCur:
    def __init__(self, conn):
        self._c = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._c.sql.append(sql)

    def executemany(self, sql, rows):
        self._c.sql.append(sql)
        self._c.inserted.extend(rows)

    def fetchone(self):
        return (self._c.old_n,)


class _BLConn:
    def __init__(self, old_n=0):
        self.old_n = old_n
        self.sql = []
        self.inserted = []

    def cursor(self):
        return _BLCur(self)


_SELLER_SHEET = SimpleNamespace(name="黑名单卖家店铺ID", columns=("seller_id",))
_CAT_SHEET = SimpleNamespace(name="黑名单亚马逊类目", columns=("category",))


def test_sync_blacklist_empty_read_never_truncates():
    conn = _BLConn(old_n=1314)
    msg = _sync_column_blacklist(conn, _SELLER_SHEET,
                                 "catalog.seller_blacklist", [], False)
    assert "不重灌" in msg
    assert not any("TRUNCATE" in s for s in conn.sql)


def test_sync_blacklist_shrink_guard():
    """骤缩超 50% 拒绝重灌——接口异常与运营删行是两回事。"""
    conn = _BLConn(old_n=1314)
    rows = [{"seller_id": "S1"}]
    with pytest.raises(RuntimeError, match="骤缩"):
        _sync_column_blacklist(conn, _SELLER_SHEET,
                               "catalog.seller_blacklist", rows, False)
    assert not any("TRUNCATE" in s for s in conn.sql)


def test_sync_blacklist_seller_refill_dedupes():
    conn = _BLConn(old_n=2)
    rows = [{"seller_id": "S1"}, {"seller_id": "S2"},
            {"seller_id": "S1"}, {"seller_id": ""}]
    msg = _sync_column_blacklist(conn, _SELLER_SHEET,
                                 "catalog.seller_blacklist", rows, False)
    assert "全量重灌 2 条" in msg
    assert sum("TRUNCATE" in s for s in conn.sql) == 1
    assert conn.inserted == [("S1",), ("S2",)]


def test_sync_blacklist_cat_refill_normalizes():
    """类目存归一化值(与查询侧共用 normalize_amazon_category),原文留档。"""
    conn = _BLConn(old_n=1)
    rows = [{"category": "Toys > Games"},
            {"category": "Toys>Games"},        # 归一化后与上一行同键 → 去重
            {"category": ""}]
    msg = _sync_column_blacklist(conn, _CAT_SHEET,
                                 "catalog.amazon_cat_blacklist", rows, True)
    assert "全量重灌 1 条" in msg
    assert conn.inserted == [("Toys->Games", "Toys > Games")]  # 首见原文为准


# ── parse_asin_lines(历史继承 ASIN 导入)────────────────────────────────────

def test_parse_asin_lines_dedupe_and_nonstd():
    text = "B0ABCDEFGH\n\nB0ABCDEFGH\n  B1ABCDEFGH \nB0BTXNF10MX\n\n"
    asins, dups, nonstd = parse_asin_lines(text)
    assert asins == ["B0ABCDEFGH", "B1ABCDEFGH", "B0BTXNF10MX"]
    assert dups == 1
    assert nonstd == 1          # 11 位:照灌不丢行,单独计数


def test_parse_asin_lines_keeps_raw_case():
    """键以原文为准,不做 upper——与黑名单中心既有键口径一致。"""
    asins, _, nonstd = parse_asin_lines("b0abcdefgh\n")
    assert asins == ["b0abcdefgh"] and nonstd == 1   # 小写不匹配标准式


# ── 批次 C 接线:L1 三级扩展 / L3 / L4 流转(orchestrator.py:378-398 口径)────

_META_C = {**META,
           "Books": {"walmart_category": "Media", "walmart_ptg": None,
                     "access_state": "普通商品", "zh_can_do": "是",
                     "requirements": "", "notes": ""}}


def test_resolve_pt_known_pt_second_source():
    """①b 产品行已知 PT(pt_backfill 回填的历史实证;所有者定稿:PT 长在
    产品主档不查边表):在架实证优先,行 PT 次之,废弃 PT 过闸。"""
    ctx = _ctx(pt_meta=META)
    l1 = audit_rules.resolve_pt(ProductInfo(asin="B0X", known_pt="GoodPT"), ctx)
    assert l1.walmart_product_type == "GoodPT"
    assert l1.pt_source == "historical_confirmed"
    both = _ctx(pt_meta=META, walmart_confirmed={"B0X": "GoodPT"})
    assert audit_rules.resolve_pt(
        ProductInfo(asin="B0X", known_pt="GoodPT"),
        both).pt_source == "walmart_confirmed"
    dead = audit_rules.resolve_pt(
        ProductInfo(asin="B0Y", known_pt="DeadPT"), ctx)
    assert dead.walmart_product_type is None      # 行 PT 不在 pt_meta → 不采


def test_resolve_pt_sentinel_hard_reject_after_evidence():
    """Layer 0 哨兵:旧仓字面量三件(unknown/低/none)+ -100 hit;
    实证命中时哨兵不生效(批复 #10 实证最优先,与旧仓'哨兵最前'有意不同)。"""
    ctx = _ctx(pt_meta=META, unmapped_paths=frozenset({"Dead > Path"}))
    p = ProductInfo(asin="B0S", title="w", amazon_category_path="Dead > Path")
    l1 = audit_rules.resolve_pt(p, ctx)
    assert l1.walmart_product_type == "unknown"
    assert l1.hits[0].rule_code == "unmapped_amazon_path"
    assert l1.hits[0].penalty == -100
    out = audit_rules.audit_one(p, ctx)
    assert out.verdict == "reject" and out.score_final == 0
    # 实证在场 → 哨兵让位
    ev = _ctx(pt_meta=META, unmapped_paths=frozenset({"Dead > Path"}),
              walmart_confirmed={"B0S": "GoodPT"})
    assert audit_rules.resolve_pt(p, ev).walmart_product_type == "GoodPT"


def test_resolve_pt_publication_ban_covers_direct_levels():
    """合同 L1-6:出版物硬禁盖全三级(批次 B 漏迁归还)——实证直出 Books 也拦。"""
    ctx = _ctx(pt_meta=_META_C, walmart_confirmed={"B0P": "Books"})
    p = ProductInfo(asin="B0P", title="novel")
    l1 = audit_rules.resolve_pt(p, ctx)
    assert any(h.rule_code == "publication_pt_forbidden" for h in l1.hits)
    out = audit_rules.audit_one(p, ctx)
    assert out.verdict == "reject"
    assert out.final_reason_category == "Intellectual Property"


def _l3_result(verdict, **kw):
    from services.audit_l3 import L3Result
    return L3Result(verdict=verdict, **kw)


def test_audit_one_l3_flow(monkeypatch):
    """L3 流转逐字迁 orchestrator.py:378-389:l2 pass 才进;reject/pending
    改判 + stage='L3',分数保留 L2 值(L3 不动分)。"""
    from services import audit_l3
    ctx = _ctx(pt_meta=META, walmart_confirmed={"B0A": "GoodPT"})
    p = ProductInfo(asin="B0A", title="widget")
    calls = []

    def fake_judge(product, l1, l2, c, conn):
        calls.append(product.asin)
        return _l3_result("reject", reason_category="offensive content")
    monkeypatch.setattr(audit_l3, "judge_l3", fake_judge)
    out = audit_rules.audit_one(p, ctx, conn=object())
    assert calls == ["B0A"]
    assert out.verdict == "reject" and out.stage_stopped_at == "L3"
    assert out.score_final == 100          # L3 不动分:reject 而分数保持
    assert out.l3.verdict == "reject"

    monkeypatch.setattr(audit_l3, "judge_l3",
                        lambda *a: _l3_result("pending"))
    out2 = audit_rules.audit_one(p, ctx, conn=object())
    assert out2.verdict == "pending" and out2.stage_stopped_at == "L3"
    assert out2.score_final == 100         # 合同 L3-8:L3 pending 保留 L2 分

    out3 = audit_rules.audit_one(p, ctx, conn=object(), run_l3=False)
    assert out3.l3 is None and out3.verdict == "pass"

    out4 = audit_rules.audit_one(p, ctx)   # conn=None:批次 B 形态,零 LLM
    assert out4.l3 is None and out4.verdict == "pass"


def test_audit_one_l2_reject_skips_l3(monkeypatch):
    from services import audit_l3
    monkeypatch.setattr(audit_l3, "judge_l3",
                        lambda *a: pytest.fail("L2 reject 不得进 L3"))
    ctx = _ctx(pt_meta=_META_C, walmart_confirmed={"B0P": "Books"})
    out = audit_rules.audit_one(ProductInfo(asin="B0P", title="n"), ctx,
                                conn=object())
    assert out.verdict == "reject" and out.l3 is None


def test_audit_one_l4_flow(monkeypatch):
    """L4 流转(orchestrator.py:392-398):仅 outcome pass 且 l4 开;只认 reject。"""
    from services import audit_l3, audit_l4
    from services.audit_l4 import L4Result
    ctx = _ctx(pt_meta=META, walmart_confirmed={"B0A": "GoodPT"})
    p = ProductInfo(asin="B0A", title="widget")
    monkeypatch.setattr(audit_l3, "judge_l3", lambda *a: _l3_result("pass"))
    monkeypatch.setattr(audit_l4, "judge_l4",
                        lambda *a, **k: L4Result(verdict="reject"))
    out = audit_rules.audit_one(p, ctx, conn=object(), run_l4=True)
    assert out.verdict == "reject" and out.stage_stopped_at == "L4"
    # 默认关(批复 #2)
    out2 = audit_rules.audit_one(p, ctx, conn=object())
    assert out2.l4 is None and out2.verdict == "pass"
    # L3 已拒 → L4 不跑
    monkeypatch.setattr(audit_l3, "judge_l3", lambda *a: _l3_result("reject"))
    monkeypatch.setattr(audit_l4, "judge_l4",
                        lambda *a, **k: pytest.fail("非 pass 不得进 L4"))
    out3 = audit_rules.audit_one(p, ctx, conn=object(), run_l4=True)
    assert out3.stage_stopped_at == "L3"


def test_audit_one_rerank_wiring(monkeypatch):
    """PT 前两级解不出且有 conn → 走候选+rerank;rerank None → pending。"""
    from services import audit_l1_llm
    ctx = _ctx(pt_meta=META)
    p = ProductInfo(asin="B0R", title="widget", amazon_category_path="X > Y")
    monkeypatch.setattr(audit_l1_llm, "candidates", lambda conn, pr: [
        {"walmart_product_type": "GoodPT", "confidence": "高"}])
    monkeypatch.setattr(
        audit_l1_llm, "rerank",
        lambda pr, cands, ptd, **k: audit_rules.L1Info(
            walmart_product_type="GoodPT", pt_confidence="高",
            pt_source="map_verified"))
    out = audit_rules.audit_one(p, ctx, conn=object(), run_l3=False)
    assert out.verdict == "pass"
    assert out.l1.walmart_category == "Home"   # 接线补 walmart_category
    monkeypatch.setattr(audit_l1_llm, "rerank", lambda *a, **k: None)
    out2 = audit_rules.audit_one(p, ctx, conn=object(), run_l3=False)
    assert out2.verdict == "pending" and out2.stage_stopped_at == "L1"


def test_persist_run_l3_l4_columns():
    """audit_runs 的 l3/l4 槽位:未跑 'skip'/NULL/'[]',跑了写实际值。"""
    from services.audit_l4 import L4Result
    captured = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            captured.append(params)

        def executemany(self, sql, rows):
            pass

        def fetchone(self):
            return (7,)

    class _Conn:
        def cursor(self):
            return _Cur()

    base = dict(asin="B0", verdict="pass", score_final=100,
                stage_stopped_at=None,
                l1=L1Info(walmart_product_type="GoodPT"))
    audit_store.persist_run(_Conn(), AuditOutcome(**base))
    assert captured[0][7:] == ("skip", None, None, "skip", "[]")
    o = AuditOutcome(**{**base, "verdict": "reject", "stage_stopped_at": "L3"})
    o.l3 = _l3_result("reject", reason_category="offensive content",
                      reason_text="bad")
    o.l4 = L4Result(verdict="pass", image_issues=[{"image_index": 1}])
    audit_store.persist_run(_Conn(), o)
    assert captured[1][7:11] == ("reject", "offensive content", "bad", "pass")
    assert json.loads(captured[1][11]) == [{"image_index": 1}]


def test_write_conclusion_pending_reason_by_stage():
    """两种 pending 来源 reason 分开:L1=类目解不出,L3=LLM 故障(旧仓字面量)。"""
    captured = {}

    class _Conn:
        def execute(self, sql, params):
            captured.update(params)
    o = AuditOutcome(asin="B0", verdict="pending", score_final=None,
                     stage_stopped_at="L1", l1=L1Info())
    audit_store.write_conclusion(_Conn(), o)
    assert "待类目判定" in captured["reason"]
    o2 = AuditOutcome(asin="B0", verdict="pending", score_final=100,
                      stage_stopped_at="L3",
                      l1=L1Info(walmart_product_type="GoodPT"))
    audit_store.write_conclusion(_Conn(), o2)
    assert captured["reason"] == "LLM 全链路故障, 待人工复核"


def test_real_pt_excludes_unknown():
    o = AuditOutcome(asin="B0", verdict="reject", score_final=0,
                     stage_stopped_at="L2",
                     l1=L1Info(walmart_product_type="unknown"))
    assert audit_store.real_pt(o) is None


def test_pick_where_accepts_l3_l4_params():
    w, _ = product_audit._pick_where({"l3": "off", "l4": "on", "workers": "8"})
    assert "IS NULL OR" in w      # 白名单收编,不炸


def test_pt_backfill_fold_rows():
    """历史实证折叠:extract_asin 归一、同 ASIN 多行取时间新者、None ts 最旧;
    naive/aware 时间戳跨源可比(2026-08-13 生产 TypeError 回归钉)。"""
    from datetime import datetime, timezone
    from workflows.pt_backfill import _UPSERT_SQL, fold_rows
    t1, t2 = datetime(2026, 5, 1), datetime(2026, 6, 1)
    aware = datetime(2026, 7, 1, tzinfo=timezone.utc)   # 报错日报侧 timestamptz
    rows = [("XKJ-B0ABCDEFGH-39.98", "OldPT", t1),
            ("B0ABCDEFGH", "NewPT", t2),           # 同 ASIN 更新的一条胜
            ("B0ABCDEFGH", "AwarePT", aware),      # aware 归一后可比且更新
            ("B1ABCDEFGH", "TsPT", None),          # None ts 视为最旧
            ("B1ABCDEFGH", "NewerPT", t1),
            ("102460026733", "AnyPT", t1)]          # 纯数字提不出 ASIN → 剔
    folded, no_asin = fold_rows(rows)
    assert folded["B0ABCDEFGH"][0] == "AwarePT"
    assert folded["B0ABCDEFGH"][1].tzinfo is None       # 归一为 naive
    assert folded["B1ABCDEFGH"] == ("NewerPT", t1)
    assert no_asin == 1
    # 只填空语义钉死:回填绝不覆盖审核结论/既有值
    assert "WHERE catalog.products.walmart_pt IS NULL" in _UPSERT_SQL


def test_rerank_exit_pt_meta_gate(monkeypatch):
    """评审 P0:rerank 出口过 pt_meta 闸——spec-only PT 直出会让 L2 四闸失明
    产假 pass,还把 meta 表没有的 PT 写进身份层。防御后转 pending。"""
    from services import audit_l1_llm
    ctx = _ctx(pt_meta=META, pt_spec={"SpecOnlyPT": {}})
    p = ProductInfo(asin="B0Z", title="widget", amazon_category_path="X > Y")
    seen = {}
    monkeypatch.setattr(audit_l1_llm, "candidates", lambda conn, pr: [
        {"walmart_product_type": "SpecOnlyPT", "confidence": "高"}])

    def fake_rerank(pr, cands, ptd, **k):
        seen["dict"] = set(ptd)
        return audit_rules.L1Info(walmart_product_type="SpecOnlyPT",
                                  pt_confidence="高", pt_source="map_verified")
    monkeypatch.setattr(audit_l1_llm, "rerank", fake_rerank)
    out = audit_rules.audit_one(p, ctx, conn=object(), run_l3=False)
    assert seen["dict"] == {"GoodPT"}          # 字典收窄为 pt_meta
    assert out.verdict == "pending" and out.stage_stopped_at == "L1"


def test_reason_mapper_l4_medium_falls_to_general_use():
    """已知缺陷照迁钉住(评审 P2-4):reason 步(3)只认 confidence=='high',
    aggressive 模式下仅由 offensive medium 触发的 L4 reject 落不到 L4 分支,
    一路兜到 General-Use Products。改它=行为变更,须双跑出数据后由所有者批。"""
    from services.audit_l4 import L4Result
    o = AuditOutcome(asin="B0", verdict="reject", score_final=100,
                     stage_stopped_at="L4",
                     l1=L1Info(walmart_product_type="GoodPT"))
    o.l4 = L4Result(verdict="reject", image_issues=[
        {"image_index": 0, "issue": "offensive gesture",
         "confidence": "medium"}])
    from services.audit_reason import compute_final_reason
    assert compute_final_reason(o, ProductInfo(asin="B0", title="t")) \
        == "General-Use Products"


def test_resolve_pt_browse_node_first():
    """②a browse_node_id 直查(所有者定稿 2026-08-14:名称会漂 ID 不会)。
    映射表 ID 覆盖率实测 100%(15,987/15,987),故 ID 在场时优先于路径。"""
    ctx = _ctx(pt_meta=META, node_map={"14083111": "GoodPT"},
               catmap={"Some > Path": "GoodPT"})
    l1 = audit_rules.resolve_pt(
        ProductInfo(asin="B0N", browse_node_id="14083111",
                    amazon_category_path="漂移的 > 名称 > 谁也对不上"), ctx)
    assert l1.walmart_product_type == "GoodPT" and l1.pt_source == "map_node"
    # 实证仍优先于 ID(批复 #10)
    ev = _ctx(pt_meta=META, node_map={"14083111": "GoodPT"},
              walmart_confirmed={"B0N": "GoodPT"})
    assert audit_rules.resolve_pt(
        ProductInfo(asin="B0N", browse_node_id="14083111"),
        ev).pt_source == "walmart_confirmed"
    # ID 不在映射表 → 落回字符串路径(无 ID 老行同此)
    fallback = _ctx(pt_meta=META, node_map={"999": "GoodPT"},
                    catmap={"Some > Path": "GoodPT"})
    assert audit_rules.resolve_pt(
        ProductInfo(asin="B0N", browse_node_id="14083111",
                    amazon_category_path="Some > Path"),
        fallback).pt_source == "map_direct"


def test_ingest_category_nodes():
    """契约 v1 追加 slow.category_id_chain:逗号串/数组两形态,末段=最细类目。"""
    from services.product_ingest import category_nodes, product_params
    assert category_nodes({"category_id_chain": "2972638011,553788,14083111"}) \
        == ("2972638011,553788,14083111", "14083111")
    assert category_nodes({"category_id_chain": [228013, "551238", " 553220 "]}) \
        == ("228013,551238,553220", "553220")
    assert category_nodes({}) == (None, None)           # 缺失 → 退回字符串路径
    assert category_nodes({"category_id_chain": " , "}) == (None, None)
    row = product_params({"asin": "B0A", "slow": {
        "title": "t", "category_path": ["A", "B"],
        "category_id_chain": "1,2,3"}})
    assert row["browse_node_id"] == "3"
    assert row["browse_node_chain"] == "1,2,3"
    assert product_params({"asin": "B0A", "slow": {}})["browse_node_id"] is None


def test_resolve_pt_path_alias_folding():
    """②级查表前折别名(catmap_align):中间层名漂移的路径也能命中映射;
    别名表空 → 退化回纯精确匹配(零行为变化)。"""
    drift = ("Home & Kitchen > Home Décor Products > Picture Frames")
    canon = ("Home & Kitchen > Home Décor > Picture Frames")
    ctx = _ctx(pt_meta=META, catmap={canon: "GoodPT"},
               path_alias={drift: canon})
    l1 = audit_rules.resolve_pt(
        ProductInfo(asin="B0F", amazon_category_path=drift), ctx)
    assert l1.walmart_product_type == "GoodPT" and l1.pt_source == "map_direct"
    # 无别名表:同一产品解不出(证明命中确实来自折叠)
    bare = _ctx(pt_meta=META, catmap={canon: "GoodPT"})
    assert audit_rules.resolve_pt(
        ProductInfo(asin="B0F", amazon_category_path=drift),
        bare).walmart_product_type is None
    # 精确命中优先:别名不得改写已能直接命中的路径
    both = _ctx(pt_meta=META, catmap={canon: "GoodPT", drift: "GoodPT"},
                path_alias={drift: "Other > Path"})
    assert audit_rules.resolve_pt(
        ProductInfo(asin="B0F", amazon_category_path=drift),
        both).walmart_product_type == "GoodPT"


def test_resolve_pt_alias_folds_into_sentinel():
    """别名折到哨兵路径 → 照样 -100 硬拒(折叠只改查得到查不到)。"""
    drift, canon = "A > B Products > Leaf", "A > B > Leaf"
    ctx = _ctx(pt_meta=META, unmapped_paths=frozenset({canon}),
               path_alias={drift: canon})
    l1 = audit_rules.resolve_pt(
        ProductInfo(asin="B0G", amazon_category_path=drift), ctx)
    assert l1.hits[0].rule_code == "unmapped_amazon_path"
    assert l1.hits[0].detail["amazon_path"] == drift   # detail 记原文不记折后


def test_catmap_mine_classify_path():
    """挖掘分桶(所有者三段式 2026-08-13):多产品同 PT=可信;少量支持=待核;
    分流=人工;与旧映射相左=冲突只报;单证不立;与旧映射一致=无事。"""
    from workflows.catmap_mine import classify_path
    assert classify_path({"A": 6}, None) == ("mined_trusted", "A", 6)
    assert classify_path({"A": 3}, None) == ("mined_review", "A", 3)
    assert classify_path({"A": 9, "B": 2}, None) == ("mined_mixed", "A", 9)
    assert classify_path({"A": 1}, None) is None            # 单证不立
    assert classify_path({"A": 6}, "A") is None             # 与旧映射一致
    assert classify_path({"A": 6}, "B") == ("map_conflict", "A", 6)
    assert classify_path({"A": 3}, "B") is None             # 冲突也要够票
    assert classify_path({"A": 6, "C": 1}, "B") is None     # 冲突须自身无分流
    assert classify_path({}, None) is None
    assert classify_path({"A": 5}, None, min_support=8) == ("mined_review", "A", 5)


def test_catmap_sibling_verdict_and_parent():
    """兄弟继承:恰一 PT 且 ≥2 兄弟支持才继承;任何分流(含哨兵)不传播;
    父路径从面包屑字符串推导(外部 zgbs 树词汇表对不上,已撤——所有者
    实测 2026-08-13)。"""
    from workflows.catmap_suggest import path_parent, sibling_verdict
    assert path_parent("A > B > C") == "A > B"
    assert path_parent("A") is None
    assert sibling_verdict([("GoodPT", 3)]) == "GoodPT"
    assert sibling_verdict([("GoodPT", 1)]) is None            # 单证不立
    assert sibling_verdict([("GoodPT", 5), ("OtherPT", 1)]) is None   # 分流
    assert sibling_verdict([("GoodPT", 5),
                            ("无对应Walmart PT", 2)]) is None   # 哨兵一票否决
    assert sibling_verdict([]) is None


def test_catmap_suggestion_from_l1():
    """建议三态:ok(挑出 PT)/ excluded(-100 hit,PT 仍留档)/ unknown。"""
    from workflows.catmap_suggest import suggestion_from_l1
    ok = audit_rules.L1Info(walmart_product_type="GoodPT", pt_confidence="高")
    assert suggestion_from_l1(ok) == ("GoodPT", "高", "ok")
    exc = audit_rules.L1Info(walmart_product_type="unknown", pt_confidence="低")
    exc.hits.append(audit_rules.RuleHit(stage="L1", rule_code="excluded_category",
                                        penalty=-100, detail={}))
    assert suggestion_from_l1(exc) == ("unknown", "低", "excluded")
    assert suggestion_from_l1(None) == (None, None, "unknown")


def test_candidate_sql_recent_guard_shape():
    """评审 P1:dry-run 复烧护栏——同批候选 24h 内有 runs 即让位(仅 dry-run)。"""
    sql = product_audit._CANDIDATE_SQL.format(
        where="x", recent_guard=product_audit._RECENT_RUN_GUARD)
    assert "NOT EXISTS" in sql and "interval '24 hours'" in sql
    plain = product_audit._CANDIDATE_SQL.format(where="x", recent_guard="")
    assert "NOT EXISTS" not in plain


# ── 行适配 ───────────────────────────────────────────────────────────────────

def test_product_info_from_row_bullet_shapes():
    base = {"asin": "B0A", "title": "t", "brand": None,
            "long_description": None, "amazon_category_path": None,
            "seller_id": None, "seller_name": None}
    p1 = audit_rules.product_info_from_row({**base, "bullet_points": ["a", "b"]})
    assert p1.bullet_points == ["a", "b"]
    p2 = audit_rules.product_info_from_row(
        {**base, "bullet_points": '["x", "y"]'})       # jsonb 以串到达
    assert p2.bullet_points == ["x", "y"]
    p3 = audit_rules.product_info_from_row(
        {**base, "bullet_points": "line1\nline2"})     # 换行分隔兼容
    assert p3.bullet_points == ["line1", "line2"]
    p4 = audit_rules.product_info_from_row({**base, "bullet_points": None})
    assert p4.bullet_points == [] and p4.brand == ""


# ── audit_calibrate:旧中间判决口径(spec_vectors §4.3)───────────────────────

def test_old_intermediate_mapping():
    from workflows.audit_calibrate import old_intermediate
    assert old_intermediate("L0", "reject") == "reject"
    assert old_intermediate("L2", "reject") == "reject"
    assert old_intermediate("L3", "reject") == "pass"   # LLM 层拦的,批次 B 没有
    assert old_intermediate("L4", "reject") == "pass"
    assert old_intermediate(None, "pass") == "pass"


def test_calibrate_default_since_from_ops_runs():
    """切点不写死(UTC+8 生产机上 16:00Z 硬编码把当天新侧整批切没,实测事故):
    取 ops.runs 里 product_audit 的 min(started_at),没跑过 → None。"""
    from datetime import datetime, timezone
    from workflows.audit_calibrate import _default_since

    class _Cur:
        def __init__(self, val):
            self._val = val

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            assert "product_audit" in sql

        def fetchone(self):
            return (self._val,)

    class _Conn:
        def __init__(self, val):
            self._val = val

        def cursor(self):
            return _Cur(self._val)

    t = datetime(2026, 8, 13, 8, 6, 0, tzinfo=timezone.utc)
    assert _default_since(_Conn(t)) == t.isoformat()
    assert _default_since(_Conn(None)) is None


# ── audit_history_fold:折叠口径钉子(SQL 在库内执行,钉不变量)────────────────

def test_history_fold_sql_invariants():
    from workflows import audit_history_fold as f
    for sql in (f._PREVIEW_SQL, f._INSERT_SQL):
        assert "IS DISTINCT FROM 'SHORTCUT'" in sql      # 影子行必须排除
        assert "verdict IN ('pass', 'reject')" in sql    # pending 不进病历
        assert "created_at < coalesce(%(cutoff)s" in sql  # 新系统 runs 不双记
    assert "verdict IS DISTINCT FROM prev" in f._INSERT_SQL  # 只投变迁点
    assert "occurred_at" in f._INSERT_SQL                # 带原始时间戳
    # 擦净重灌只许删自己 source 的行(账本只追加的唯一例外,范围必须钉死)
    assert f.SOURCE == "audit_history_fold"
