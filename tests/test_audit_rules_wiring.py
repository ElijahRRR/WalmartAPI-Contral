"""审核接线层测试(评审 I-8 补钉:P0/P1 发现全在这些零测试文件里)。

覆盖:resolve_pt 的 pt_meta 闸与优先级、audit_store 词表/桩值、
_pick_where 四态与参数白名单、黑名单中心镜像空读/骤缩护栏、
ASIN 历史导入解析、行适配。
"""

import json
from types import SimpleNamespace

import pytest

from registry import resources
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
    w, _ = product_audit._pick_where({"mode": "pending"})
    # 待定专刷:只圈 pending,且**不带 1 天退避**(改完判定要立刻验证)
    assert w == "p.audit_status = 'pending'" and "interval" not in w


def test_pick_where_rerule_targets_only_the_rejected_backlog():
    """改一条规则后要动的只有被它拒过的那批,不是全库(2026-08-17 裁决 A)。

    此前唯一的批量通道是 `force_rerun=<版本>`,而版本一递增库里没有一条是新
    版本 ⇒ **全量**十几万条重审,为了几千条误杀烧掉全库的 LLM 钱。
    """
    w, e = product_audit._pick_where({"rerule": "phase0_forbidden_category"})
    assert e["rerule"] == "phase0_forbidden_category"
    assert e["rerule_ver"] == resources.AUDIT_RULES_VERSION
    assert "p.audit_status = 'rejected'" in w
    assert "p.audit_version IS DISTINCT FROM %(rerule_ver)s" in w
    assert "EXISTS" in w and "h.rule_code = %(rerule)s" in w


def test_rerule_anchors_on_products_not_on_the_latest_run():
    """⚠ 锚"最近一轮 audit_runs"会让 dry-run **吃掉**自己要救的产品。

    首版就是那样写的,所有者第一次 dry-run 当场炸出来:dry-run **也落
    runs/hits**,于是那批的"最近一轮"变成本次 dry-run 的结果 —— 被救回来的
    45 条新一轮判 pass、也不再带这条 hit,直接掉出候选集;而 dry-run 不写
    products 五列,它们的 audit_status 还是 rejected。净效果:验证一次就永久
    搁浅,任何通道都不会再捞,而且全程不报错。

    所以谓词必须锚在 `catalog.products`(dry-run 碰不到的那几列)。
    """
    w, _ = product_audit._pick_where({"rerule": "x"})
    assert "DISTINCT ON" not in w          # 不认"最近一轮"
    assert "r.verdict" not in w and "l.verdict" not in w
    # 判过的靠版本号退出候选集 —— 这同时是天然分页(limit 撞满就再跑一轮)
    assert "audit_version IS DISTINCT FROM" in w


def test_pick_where_rejects_unknown_params():
    """静默吞参数 = '全量重审跑完了'的假象,宁炸不吞。"""
    with pytest.raises(ValueError, match="未识别参数"):
        product_audit._pick_where({"force_rurn": "x"})     # 手滑拼错


def test_pick_where_lets_cli_injected_keys_through():
    """⚠ cli 自己塞的键不是人敲的参数,白名单必须放行。

    生产实证 2026-08-16:`--dry-run` 上线当天,cli 开始往 params 里塞
    `dry_run`,这条白名单把它当成手滑拼错**直接抛异常** ——
    `product_audit --dry-run` 完全起不来。每加一个 cli 级开关都会重演,
    所以 `_CLI_INJECTED` 与 cli 那侧要一起改。
    """
    for k in product_audit._CLI_INJECTED:
        product_audit._pick_where({k: True})               # 不抛就算过
    assert {"execute", "dry_run"} <= product_audit._CLI_INJECTED


def test_pick_where_rejects_unknown_mode():
    """mode 拼错静默落回默认 = 以为在补刷、实际在跑默认候选,同样宁炸不吞。"""
    with pytest.raises(ValueError, match="未识别 mode"):
        product_audit._pick_where({"mode": "backfil"})     # 手滑拼错


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
    l1 = audit_rules.resolve_pt(
        ProductInfo(asin="B0X", known_pt="GoodPT",
                    known_pt_source="walmart_confirmed"), ctx)
    assert l1.walmart_product_type == "GoodPT"
    assert l1.pt_source == "historical_confirmed"
    both = _ctx(pt_meta=META, walmart_confirmed={"B0X": "GoodPT"})
    assert audit_rules.resolve_pt(
        ProductInfo(asin="B0X", known_pt="GoodPT"),
        both).pt_source == "walmart_confirmed"
    dead = audit_rules.resolve_pt(
        ProductInfo(asin="B0Y", known_pt="DeadPT"), ctx)
    assert dead.walmart_product_type is None      # 行 PT 不在 pt_meta → 不采


def test_sentinel_no_longer_rejects_and_does_not_block_llm():
    """所有者定稿 2026-08-14:映射表标注"无对应Walmart PT"**不再判死**。

    那条标注是当年没数据时人工打的,不代表今天判不出来——判不出来才该
    pending,不该拒。改为 0 分留痕,继续走候选+LLM。
    """
    ctx = _ctx(pt_meta=META, unmapped_paths=frozenset({"Dead > Path"}))
    p = ProductInfo(asin="B0S", title="w", amazon_category_path="Dead > Path")
    l1 = audit_rules.resolve_pt(p, ctx)
    assert l1.walmart_product_type is None            # 不再写 'unknown' 桩值
    assert l1.hits[0].rule_code == "unmapped_amazon_path"
    assert l1.hits[0].penalty == 0                    # 只留痕
    assert not audit_rules._blocked(l1)                # 不算判死 → 放行进第三级
    # 无 conn(离线)⇒ 第三级跑不了 ⇒ pending,**绝不是 reject**
    out = audit_rules.audit_one(p, ctx)
    assert out.verdict == "pending" and out.stage_stopped_at == "L1"
    # 实证在场 → 连留痕都不打(哨兵只在解不出 PT 时才谈)
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
        audit_l1_llm, "rerank_ex",
        lambda pr, cands, ptd, **k: (audit_rules.L1Info(
            walmart_product_type="GoodPT", pt_confidence="高",
            pt_source="map_verified"), "ok"))
    out = audit_rules.audit_one(p, ctx, conn=object(), run_l3=False)
    assert out.verdict == "pass"
    assert out.l1.walmart_category == "Home"   # 接线补 walmart_category
    # llm_failed 不重试(换候选面治不了链路故障),直接 pending
    monkeypatch.setattr(audit_l1_llm, "rerank_ex",
                        lambda *a, **k: (None, "llm_failed"))
    out2 = audit_rules.audit_one(p, ctx, conn=object(), run_l3=False)
    assert out2.verdict == "pending" and out2.stage_stopped_at == "L1"


def test_audit_one_unknown_retries_open_candidates(monkeypatch):
    """所有者定稿:七路候选都被 LLM 否掉(unknown)→ 换开放候选面再判一次。"""
    from services import audit_l1_llm
    audit_l1_llm.reset_stats()
    ctx = _ctx(pt_meta=META)
    p = ProductInfo(asin="B0U", title="widget", amazon_category_path="X > Y")
    monkeypatch.setattr(audit_l1_llm, "candidates", lambda conn, pr: [
        {"walmart_product_type": "OtherPT", "confidence": "低"}])
    monkeypatch.setattr(audit_l1_llm, "open_candidates", lambda conn, pr, **k: [
        {"walmart_product_type": "GoodPT", "confidence": "高"}])
    calls: list[list] = []

    def fake_rerank_ex(pr, cands, ptd, **k):
        calls.append(cands)
        if len(calls) == 1:
            return None, "unknown"          # 七路候选:LLM 全否
        return audit_rules.L1Info(walmart_product_type="GoodPT",
                                  pt_confidence="高",
                                  pt_source="map_verified"), "ok"
    monkeypatch.setattr(audit_l1_llm, "rerank_ex", fake_rerank_ex)
    out = audit_rules.audit_one(p, ctx, conn=object(), run_l3=False)
    assert out.verdict == "pass" and out.l1.walmart_product_type == "GoodPT"
    assert len(calls) == 2                  # 第二次判的是开放候选面
    assert calls[1][0]["walmart_product_type"] == "GoodPT"
    assert audit_l1_llm.STATS["unknown_retry_called"] == 1
    assert audit_l1_llm.STATS["unknown_retry_saved"] == 1


def test_audit_one_unknown_retry_still_unknown(monkeypatch):
    """二次机会也解不出 → 照旧 pending,绝不默认放行(10.2)。"""
    from services import audit_l1_llm
    audit_l1_llm.reset_stats()
    ctx = _ctx(pt_meta=META)
    p = ProductInfo(asin="B0V", title="widget", amazon_category_path="X > Y")
    monkeypatch.setattr(audit_l1_llm, "candidates", lambda conn, pr: [
        {"walmart_product_type": "OtherPT", "confidence": "低"}])
    monkeypatch.setattr(audit_l1_llm, "open_candidates", lambda conn, pr, **k: [
        {"walmart_product_type": "GoodPT", "confidence": "高"}])
    monkeypatch.setattr(audit_l1_llm, "rerank_ex",
                        lambda *a, **k: (None, "unknown"))
    out = audit_rules.audit_one(p, ctx, conn=object(), run_l3=False)
    assert out.verdict == "pending" and out.stage_stopped_at == "L1"
    assert audit_l1_llm.STATS["unknown_retry_called"] == 1
    assert audit_l1_llm.STATS["unknown_retry_saved"] == 0


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
                                  pt_confidence="高",
                                  pt_source="map_verified"), "ok"
    monkeypatch.setattr(audit_l1_llm, "rerank_ex", fake_rerank)
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
    # 现役键名:raw.category_ids(采集器源码核实——ID 未进 slow,在 raw 幸存)
    assert category_nodes({}, {"category_ids": "2972638011,553788,14083111"}) \
        == ("2972638011,553788,14083111", "14083111")
    # 前向兼容:采集侧若把它提进 slow(契约追加)也认
    assert category_nodes({"category_id_chain": [228013, "551238", " 553220 "]}) \
        == ("228013,551238,553220", "553220")
    # 采集侧空值哨兵 "N/A" 不当 ID(root_category_id 缺省即此值)
    assert category_nodes({}, {"category_ids": "N/A"}) == (None, None)
    assert category_nodes({}, {"category_ids": "1055398,N/A"}) \
        == ("1055398", "1055398")
    assert category_nodes({}, {}) == (None, None)        # 缺失 → 退回字符串路径
    assert category_nodes({"category_id_chain": " , "}) == (None, None)
    row = product_params({"asin": "B0A", "slow": {"title": "t"},
                          "raw": {"category_ids": "1,2,3"}})
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
    """别名折到哨兵路径 → 照样只留痕不判死(折叠只改查得到查不到)。"""
    drift, canon = "A > B Products > Leaf", "A > B > Leaf"
    ctx = _ctx(pt_meta=META, unmapped_paths=frozenset({canon}),
               path_alias={drift: canon})
    l1 = audit_rules.resolve_pt(
        ProductInfo(asin="B0G", amazon_category_path=drift), ctx)
    assert l1.hits[0].rule_code == "unmapped_amazon_path"
    assert l1.hits[0].penalty == 0 and not audit_rules._blocked(l1)
    assert l1.hits[0].detail["amazon_path"] == drift   # detail 记原文不记折后


def test_catalog_health_aliases_ascii_and_used():
    """PG 把未加引号标识符折小写(中文不折、ASCII 折):'有类目ID' → '有类目id',
    按原文取键必 KeyError(生产实测 2026-08-14)。别名一律 ASCII,且
    run() 用到的键必须都在 SQL 别名里。"""
    import re
    from workflows import catalog_health as ch
    aliases = set(re.findall(r"\bAS ([a-zA-Z_][a-zA-Z0-9_]*)\b", ch._SQL))
    assert aliases, "SQL 里应有 AS 别名"
    assert all(a.islower() or "_" in a for a in aliases)   # 无大小写歧义
    src = open(ch.__file__, encoding="utf-8").read()
    used = set(re.findall(r"r\['([a-z_0-9]+)'\]", src))
    assert used and used <= aliases, f"取了 SQL 里没有的键:{used - aliases}"


def test_catmap_mine_classify_path():
    """挖掘分桶(所有者三段式 2026-08-13):多产品同 PT=可信;少量支持=待核;
    分流=人工;与旧映射相左=冲突只报;单证不立;与旧映射一致=无事。"""
    from workflows.catmap_mine import classify_path
    assert classify_path({"A": 6}, None) == ("mined_trusted", "A", 6)
    assert classify_path({"A": 3}, None) == ("mined_review", "A", 3)
    assert classify_path({"A": 1}, None) is None            # 单证不立
    assert classify_path({"A": 6}, "A") is None             # 与旧映射一致
    assert classify_path({"A": 6}, "B") == ("map_conflict", "A", 6)
    assert classify_path({"A": 3}, "B") is None             # 冲突也要够票
    assert classify_path({}, None) is None
    assert classify_path({"A": 5}, None, min_support=8) == ("mined_review", "A", 5)
    # 优势度而非全票(首跑实测:要求 100% 一致 → 1321 个类目全打成分流)
    assert classify_path({"A": 9, "B": 1}, None) == ("mined_trusted", "A", 9)
    assert classify_path({"A": 6, "B": 4}, None) == ("mined_mixed", "A", 6)
    assert classify_path({"A": 9, "B": 1}, None,
                         min_dominance=0.95) == ("mined_mixed", "A", 9)
    # 冲突同样看优势度:少数派噪声不该掩盖"旧映射与实证相左"
    assert classify_path({"A": 9, "B": 1}, "B") == ("map_conflict", "A", 9)
    assert classify_path({"A": 6, "C": 4}, "B") is None     # 自身分流不算冲突


def test_catmap_map_ambiguous_never_written():
    """⚠ 潜伏 bug 的锁:映射表**自己**挂着多条 PT 不同的高置信行时,
    ②级直出对该 key 已经失明(闸要求恰好一个高置信 PT)。原实现把这种 key
    的 in_map 值取成 NULL,调用方当成"没映射过" ⇒ 会被当新映射挖出来、
    promote 时再插第三条。现在单列一桶,只报不写。"""
    from workflows.catmap_mine import _TIER_BY_STATUS, classify_path
    got = classify_path({"A": 9}, None, in_map_ambiguous=True)
    assert got == ("map_ambiguous", "A", 9)
    assert "map_ambiguous" not in _TIER_BY_STATUS   # 无档位 ⇒ 不进 promote_rows


def test_catmap_tier_by_status():
    """分桶 → 置信档:高才直出,中/低只进候选交 LLM(所有者定稿 2026-08-14)。"""
    from workflows.catmap_mine import _TIER_BY_STATUS
    assert _TIER_BY_STATUS["mined_trusted"] == "高"
    assert _TIER_BY_STATUS["mined_review"] == "中"    # 有真实 ASIN,票不多
    assert _TIER_BY_STATUS["map_conflict"] == "中"    # 两条都留,LLM 挑
    assert _TIER_BY_STATUS["mined_mixed"] == "低"     # 首选 PT 是多数派非共识


def test_catmap_promotions_only_go_up():
    """升档自动、降档不自动。证据可能只是**暂时**变薄(pt_source 回填没跑完、
    本轮只挖了子集),据此自动降档会让 ②级直出对整个类目静默失效;高置信行
    也可能是人工定的,机器不该拿一轮统计推翻人的判断。"""
    from workflows.catmap_mine import plan_promotions
    rows = [
        ("A > B", "PT1", "n1", "高", "mined_trusted"),   # 新增
        ("C > D", "PT2", "n2", "高", "mined_trusted"),   # 中 → 高:升档
        ("E > F", "PT3", "n3", "中", "mined_review"),    # 已是高:跳过(不降)
        ("G > H", "PT4", "n4", "中", "mined_review"),    # 同档:跳过(幂等)
    ]
    existing = {("C > D", "PT2"): "中", ("E > F", "PT3"): "高",
                ("G > H", "PT4"): "中"}
    planned, stat = plan_promotions(rows, existing)
    assert stat == {"新增": 1, "升档": 1, "跳过(档位未提升)": 2}
    assert [(r[0], r[3]) for r in planned] == [("A > B", "高"), ("C > D", "高")]
    # 血统标记保持存量值:改名只会把同一来源的行分成两批,没有任何好处
    assert planned[0][4] == "mined_products"
    # 重跑幂等:把上一轮的结果当现状,应该一条都不写
    again, stat2 = plan_promotions(rows, {**existing, ("A > B", "PT1"): "高",
                                          ("C > D", "PT2"): "高"})
    assert again == [] and stat2["跳过(档位未提升)"] == 4


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


class _CountCur:
    def __init__(self, n):
        self.n = n
        self.sql = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        return (self.n,)


class _CountConn:
    def __init__(self, n):
        self.cur = _CountCur(n)

    def cursor(self):
        return self.cur


def test_rerule_head_says_how_many_are_left():
    """摘要只说"候选 500"时看不出**是刚好 500 还是撞了 limit**。

    所有者 2026-08-17 首轮 dry-run 实遇:limit 缺省就是 500,而误杀规模正是他
    决定要不要真跑的唯一依据。与 `_claim_from_sheet` 同款纪律。
    """
    conn = _CountConn(3200)
    head = product_audit._rerule_head(conn, "phase0_forbidden_category",
                                      "p.audit_status = 'rejected'",
                                      {"rerule": "x", "rerule_ver": "v"}, 500)
    assert "共 3200 个" in head[0]
    assert "只判 500 个,还剩 2700 个" in head[1]
    # 计数与取候选必须同一 where,否则"还剩多少"是另一件事的数
    assert "p.audit_status = 'rejected'" in conn.cur.sql
    assert "LIMIT" not in conn.cur.sql

    # 撞不到上限时不该出现"还剩"那行(它会让人以为没跑完)
    assert len(product_audit._rerule_head(
        _CountConn(12), "r", "w", {}, 500)) == 1
    # 一个都没有:多半是规则码拼错,得说出来而不是静静报"候选 0"
    assert "拼错" in product_audit._rerule_head(
        _CountConn(0), "r", "w", {}, 500)[1]


def test_is_forced_exempts_rerule_but_not_from_sheet():
    """强审才豁免复烧护栏;`from_sheet` 塞的 asins 不算强审(2026-08-16 纠正)。

    `rerule` 必须豁免:它翻的全是 24 小时内刚被拒的行,吃了护栏 dry-run 会稳定
    报"0 候选",真跑却翻出几千条 —— dry-run 说的话必须是真跑要做的事。
    """
    assert product_audit._is_forced({"asins": "B0A"}, {"asins": ["B0A"]})
    assert not product_audit._is_forced(
        {"from_sheet": 1, "asins": "B0A"}, {"asins": ["B0A"]})
    assert product_audit._is_forced({"rerule": "phase0_forbidden_category"}, {})
    assert not product_audit._is_forced({}, {})
    assert not product_audit._is_forced({"rerule": "  "}, {})   # 空串不算


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


def test_pt_provenance_splits_evidence_from_inference():
    """PT 来源两分道(所有者定稿 2026-08-14):只有沃尔玛真接受过的算实证。

    映射直查/LLM rerank 都是推断——把推断当实证写回产品行,catmap_mine
    会拿它投票挖进映射表,一次猜错永久固化(A 推出 B、B 又去证明 A)。
    """
    def _o(src):
        return AuditOutcome(asin="B0", verdict="pass", score_final=100,
                            stage_stopped_at=None,
                            l1=L1Info(walmart_product_type="GoodPT",
                                      pt_source=src))
    assert audit_store.pt_provenance(_o("walmart_confirmed")) == "walmart_confirmed"
    assert audit_store.pt_provenance(_o("historical_confirmed")) == "walmart_confirmed"
    for inferred in ("map_node", "map_direct", "llm", None):
        assert audit_store.pt_provenance(_o(inferred)) == "audit_llm"
    # 没有真 PT 可写 → 不动 pt_source(桩值/unknown)
    stub = AuditOutcome(asin="B0", verdict="reject", score_final=0,
                        stage_stopped_at="L0",
                        l1=L1Info(walmart_product_type="(phase0_blocked)"))
    assert audit_store.pt_provenance(stub) is None


def test_known_pt_splits_evidence_from_cached_inference():
    """①b 级按 pt_source 分道:沃尔玛回执才算实证,上一轮 LLM 结论只是缓存。

    不分道 = "LLM 猜一个 → 下轮以高置信实证复述",猜错会被自己反复确认,
    而且从 runs 里看不出来(所有者 2026-08-14 追问)。
    """
    ctx = _ctx(pt_meta=META)
    ev = audit_rules.resolve_pt(
        ProductInfo(asin="B0K", known_pt="GoodPT",
                    known_pt_source="walmart_confirmed"), ctx)
    assert ev.pt_source == "historical_confirmed" and ev.pt_confidence == "高"
    for src in ("audit_llm", None, ""):
        cached = audit_rules.resolve_pt(
            ProductInfo(asin="B0K", known_pt="GoodPT", known_pt_source=src), ctx)
        assert cached.walmart_product_type == "GoodPT"   # 仍直出(不重付 LLM)
        assert cached.pt_source == "audit_cached" and cached.pt_confidence == "中"
    # 缓存推断写回时仍记 audit_llm——不会因为"存过一轮"就升格成实证
    out = AuditOutcome(asin="B0K", verdict="pass", score_final=100,
                       stage_stopped_at=None,
                       l1=L1Info(walmart_product_type="GoodPT",
                                 pt_source="audit_cached"))
    assert audit_store.pt_provenance(out) == "audit_llm"


def test_adopt_only_requires_backfill_and_skips_judging():
    """adopt_only:只采用历史结论、零 LLM(所有者 2026-08-14:86 万可采用
    vs 33 万要真判,混在一起跑等于为了采用顺带付 33 万次 LLM)。"""
    with pytest.raises(ValueError, match="只在 mode=backfill"):
        product_audit.run({"adopt_only": "1"})
    assert "adopt_only" in product_audit._KNOWN_PARAMS


def test_adopt_history_batches_updates():
    """采用走 executemany:86 万条逐行往返要几十分钟。"""
    import inspect
    src = inspect.getsource(product_audit._adopt_history)
    assert "adopt_rows.append" in src and "executemany(_ADOPT_SQL" in src
    assert "conn.execute(_ADOPT_SQL" not in src      # 逐行版必须已移除


def test_adopt_only_narrows_candidates_to_rows_with_history():
    """只采用模式必须只挑有历史的行:否则没历史的那批每轮都排在前面重复捞
    (生产实测采用率 122k→25k 一路塌)。"""
    import inspect
    src = inspect.getsource(product_audit.run)
    assert "_HAS_HISTORY_SQL" in src
    has = product_audit._HAS_HISTORY_SQL
    assert "audit.audit_runs" in has and "r.asin = p.asin" in has
    assert "IS DISTINCT FROM 'SHORTCUT'" in has     # 影子行不算历史结论


def test_worker_cap_warns_instead_of_silently_clamping(caplog):
    """超上限必须告警:静默钳制 = 拿着错的数做并发决策(生产实测:所有者
    用 workers=32 测吞吐,实际跑 16 而输出只字未提)。"""
    import logging
    import inspect
    src = inspect.getsource(product_audit.run)
    assert "_MAX_WORKERS" in src and "超上限,实际用" in src
    assert product_audit._MAX_WORKERS >= 32     # I/O 密集,远超核数是正常的


def test_llm_retry_stats_are_counted():
    """退避不能静默:撞限流只表现为变慢,不计数就看不出来。"""
    from api import llm
    llm.reset_retry_stats()
    assert llm.RETRY_STATS == {"http_429": 0, "http_5xx": 0, "other": 0}
    llm._bump_retry("http_429")
    assert llm.RETRY_STATS["http_429"] == 1


def test_adopt_never_overwrites_walmart_confirmed_pt():
    """生产事故 2026-08-14:采用历史结论把 pt_backfill 回填的 9 万条沃尔玛
    回执实证覆盖成旧系统推断,来源一并降级,挖掘燃料 16.8 万腰斩到 7.7 万。
    采用的是我们自己旧系统的推断,压不过沃尔玛回执。"""
    sql = product_audit._ADOPT_SQL
    assert "WHEN pt_source = 'walmart_confirmed' THEN walmart_pt" in sql
    assert "WHEN pt_source = 'walmart_confirmed' THEN pt_source" in sql
    # 审核结论回写同一条不变量
    assert "pt_source = 'walmart_confirmed'" in audit_store._PRODUCT_SQL


def test_pt_backfill_evidence_overwrites_inference():
    """实证优先于推断:回执可以覆盖 audit_llm 行(也是上面那次事故的修复通道)。"""
    from workflows import pt_backfill
    sql = pt_backfill._UPSERT_SQL
    assert "pt_source = 'walmart_confirmed'" in sql
    assert "pt_source IS DISTINCT FROM 'walmart_confirmed'" in sql


def test_run_commits_in_segments_with_progress():
    """生产事故 2026-08-14:34 万行判在同一个未提交事务里 —— 外部查不到任何
    进度、Ctrl-C 全部回滚(半小时 LLM 费用打水漂)、长事务还挡住 vacuum。"""
    import inspect
    src = inspect.getsource(product_audit.run)
    assert "_COMMIT_EVERY" in src and "conn.commit()" in src
    assert "进度 %d/%d" in src                     # 日志可见,不必查库
    assert 0 < product_audit._COMMIT_EVERY <= 2000  # 段太大就退化回老问题


def test_retry_summary_only_calls_429_ratelimit():
    """只有 http_429 才叫撞限流(生产实测:19 次 other 被说成"已撞限流",
    把所有者引向降并发 —— 而网络抖动降并发毫无用处)。"""
    import inspect
    src = inspect.getsource(product_audit.run)
    assert 'retries.get("http_429")' in src
    assert "降并发解决不了" in src


# ── 批量落库(所有者定稿 2026-08-17:并发 128 + 批量落库)──────────────────

def test_batch_and_single_persist_share_one_param_builder():
    """runs 有 12 列,单条与批量各拼一份的话,加一列漏改一处 = 静默写错列
    (元组长度对得上时不报错)。两条路径必须共用 _run_params/_hit_params。"""
    import inspect

    from services import audit_store as st
    for fn in (st.persist_run, st.persist_runs):
        src = inspect.getsource(fn)
        assert "_run_params" in src, fn.__name__
    assert "_hit_params" in inspect.getsource(st.persist_run)
    assert "_hit_params" in inspect.getsource(st.persist_runs)


def test_persist_runs_refuses_when_ids_do_not_line_up():
    """returning 的结果集必须与入参一一对应 —— 配错 = 事件挂到别的 ASIN 上,
    而且两边都不报错。数目对不上就停手。"""
    import pytest

    from services import audit_store as st

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def executemany(self, sql, seq, returning=False): self._n = 0
        def fetchone(self): self._n += 1; return (self._n,)
        def nextset(self): return False        # 只回 1 个 id,入参却有 3 条

    class _Conn:
        def cursor(self): return _Cur()

    import types
    o = types.SimpleNamespace(
        asin="B0A", score_final=1, verdict="pass", stage_stopped_at="L2",
        l1=types.SimpleNamespace(walmart_product_type="Cups",
                                 pt_confidence=0.9, pt_source="x"),
        l3=None, l4=None, all_hits=[])
    with pytest.raises(RuntimeError, match="顺序对不上"):
        st.persist_runs(_Conn(), [o, o, o])


def test_audit_defaults_are_128_and_batched():
    """默认 128(此前默认 4 而上限 64——不显式传 -p workers= 就只跑 4,
    "上限 64"看着高其实从没生效过)。"""
    import inspect

    from workflows import product_audit as pa
    assert pa._DEFAULT_WORKERS == 128 and pa._MAX_WORKERS >= 128
    assert 'params.get("workers", _DEFAULT_WORKERS)' in inspect.getsource(pa.run)

    src = inspect.getsource(pa.run)
    # 批量落库 + 失败退回逐行(已付费的 LLM 结果不能因为同批一行脏就陪葬)
    assert "persist_runs" in src and "persist_run(" in src
    # 分段提交前必须先冲刷缓冲,否则"此刻之前判的都已持久"是谎话
    assert src.index("_flush(force=True)") < src.index("conn.commit()")
