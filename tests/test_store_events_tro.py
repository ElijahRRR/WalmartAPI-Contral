"""TRO 品牌命中接线回归(services/audit_store.tro_hits + product_audit 钩子)。

钉四件事:
  ① **四步口径顺序不能反**:R4 命中 → 交 TRO 来源 → 交 L3 真品牌判定 → 分档。
     每一步都是在缩小范围,少一步就会拿一批通用英文词去标店铺;
  ② L3 那一步的两个坑:回传的 brand 是 **LLM 字符串**(大小写不定)、而且
     verdict 里**混着 R5 的词** —— 所以必须归一 + 与 R4 命中集取交集;
  ③ **先写 ops.dedupe 再写事件**(事件表只追加,重复行不可撤);
  ④ 口径守夜:装配期 TRO 前缀词恒 0 要告警(source 是自由文本且被飞书覆盖)。

沙箱 PG 集成用例在文件末尾:连不上就 skip,不让无 PG 的环境变红。
"""

import os
import socket

import pytest

from registry import resources
from services import audit_store, store_events as se
from services.audit_l3 import L3Result
from services.audit_models import AuditOutcome, L1Info, L2Result, RuleHit
from workflows import product_audit as pa

_PREFIX = resources.TRO_BRAND_SOURCE_PREFIX

# 生产实证的三种 source 写法(22,527 行「TRO品牌」+「TRO」+「tro」)
_SRC = {"dyson": "TRO品牌", "nike": "TRO", "bose": "tro",
        "top": "TRO品牌",          # 黑名单里混着的通用英文词,同样标 TRO
        "hammer": "产品清理报错扫描"}  # 非 TRO 来源:不该被这条链碰


def _outcome(r4_brands, l3=None, asin="B0TRO00001", verdict="pass"):
    """输入:R4 命中词 + 可选 L3 结果 → 输出:一条 AuditOutcome。"""
    hits = []
    if r4_brands:
        hits.append(RuleHit(
            stage="L2", rule_code="title_desc_blacklist", penalty=0,
            detail={"matches": [{"brand": b, "matched_phrase": b}
                                for b in r4_brands],
                    "count": len(r4_brands)}))
    return AuditOutcome(asin=asin, verdict=verdict, score_final=100,
                        stage_stopped_at=None, l1=L1Info(),
                        l2=L2Result(score_final=100, hits=hits), l3=l3)


def _l3(verdict="pass", brands=()):
    return L3Result(verdict=verdict, blacklist_brand_verdict=list(brands))


def _hits(outcome):
    return audit_store.tro_hits(outcome, _SRC, _PREFIX)


# ── 四步口径 ────────────────────────────────────────────────────────────────

def test_confirmed_is_the_intersection_of_all_three_sets():
    """B∩C:R4 命中 ∩ TRO 来源 ∩ L3 判真品牌。三者缺一都不算确认。"""
    res = _hits(_outcome(["dyson"], _l3(brands=[
        {"brand": "dyson", "is_real_brand": True, "evidence": "真空吸尘器品牌"}])))
    assert res["confirmed"] == ["dyson"]
    assert res["unjudged"] == []
    assert res["sources"] == {"dyson": "TRO品牌"}


def test_non_tro_source_word_is_not_our_business():
    """黑名单里绝大多数词不是 TRO 来源 —— 命中了也与这条链无关。"""
    res = _hits(_outcome(["hammer"], _l3(brands=[
        {"brand": "hammer", "is_real_brand": True}])))
    assert res == {"confirmed": [], "unjudged": [], "sources": {},
                   "reason": None}


def test_all_three_production_source_spellings_match():
    """「TRO品牌」/「TRO」/「tro」三种写法都要中 —— 前缀匹配的理由就在这。"""
    res = _hits(_outcome(["dyson", "nike", "bose"], _l3(brands=[
        {"brand": b, "is_real_brand": True} for b in ("dyson", "nike", "bose")])))
    assert res["confirmed"] == ["bose", "dyson", "nike"]


def test_l3_brand_is_an_llm_string_so_case_is_normalized():
    """L3 回传的 brand 是 LLM 复述的字符串:大小写/空白不定,归一后才能比。"""
    res = _hits(_outcome(["dyson"], _l3(brands=[
        {"brand": "  Dyson ", "is_real_brand": True}])))
    assert res["confirmed"] == ["dyson"]


def test_r5_words_in_the_verdict_are_cut_by_intersecting_with_r4():
    """⚠ blacklist_brand_verdict 里**混着 R5(USPTO 商标)的词**。

    不与 R4 命中集取交集的话,一个只在 R5 出现、恰好也在黑名单里标着 TRO 的词
    会被当成"本产品命中了 TRO 品牌"报上去 —— 而 R4 根本没在标题里见到它。
    """
    res = _hits(_outcome(["dyson"], _l3(brands=[
        {"brand": "dyson", "is_real_brand": True},
        {"brand": "nike", "is_real_brand": True}])))   # nike 只在 R5 那边
    assert res["confirmed"] == ["dyson"]
    assert res["sources"] == {"dyson": "TRO品牌"}      # nike 连 sources 都不进


def test_is_real_brand_must_be_strictly_true():
    """严格 `is True`(与 audit_l3 强制翻拒同口径):字符串 "true" / 1 都不算。"""
    for v in ("true", "True", 1, "yes"):
        res = _hits(_outcome(["dyson"], _l3(brands=[
            {"brand": "dyson", "is_real_brand": v}])))
        assert res["confirmed"] == [], v
        # 给过判定(只是不是 True)⇒ 不算"拿不到判定",不进 unjudged
        assert res["unjudged"] == [], v


def test_explicitly_judged_generic_word_is_dropped_not_unjudged():
    """L3 说「top 是通用英文词」= 判过了、不是品牌:既不报也不展开。

    这正是 unjudged 取 `B - 判过的` 而不是 `B - C` 的原因 —— 后者会把
    "已确认是通用词"和"没人判过"混成一档,前者是噪声、后者才要人看。
    """
    res = _hits(_outcome(["top"], _l3(brands=[
        {"brand": "top", "is_real_brand": False, "evidence": "常见形容词"}])))
    assert res["confirmed"] == [] and res["unjudged"] == []
    assert res["reason"] is None


# ── unjudged 的三种成因 ─────────────────────────────────────────────────────

def test_unjudged_when_l2_killed_it_before_l3_ran():
    """L2 就判死了,压根没跑 L3(outcome.l3 is None)。"""
    res = _hits(_outcome(["dyson"], l3=None, verdict="reject"))
    assert res["confirmed"] == [] and res["unjudged"] == ["dyson"]
    assert "未跑 L3" in res["reason"]


def test_unjudged_when_l3_is_pending():
    """LLM 故障 → L3 pending,verdict 列表是空的。"""
    res = _hits(_outcome(["dyson"], _l3(verdict="pending")))
    assert res["unjudged"] == ["dyson"] and "LLM 故障" in res["reason"]


def test_unjudged_when_word_falls_outside_the_top_ten_cap():
    """L3 只判前 10 个词(audit_l3.MAX_BRANDS),第 11 个永远没有判定。"""
    from services import audit_l3
    words = [f"w{i}" for i in range(audit_l3.MAX_BRANDS)] + ["dyson"]
    l3 = _l3(brands=[{"brand": w, "is_real_brand": False}
                     for w in words[:audit_l3.MAX_BRANDS]])
    res = audit_store.tro_hits(_outcome(words, l3),
                               {**_SRC, **{w: "TRO品牌" for w in words}},
                               _PREFIX)
    assert res["confirmed"] == []
    assert res["unjudged"] == ["dyson"]          # 被截在 10 词之外的那个
    assert "10 词" in res["reason"]


def test_no_r4_hit_means_no_work_at_all():
    assert _hits(_outcome([])) == {"confirmed": [], "unjudged": [],
                                   "sources": {}, "reason": None}


# ── 装配期口径守夜:TRO 前缀恒 0 要告警 ─────────────────────────────────────

def test_zero_tro_prefix_words_raises_a_warning(monkeypatch, caplog):
    """source 是自由文本、且被飞书同步整列覆盖 —— 改了写法就静默变 0,
    表现是"从此再也不报警"。恒 0 必须有人喊一声。"""
    from services import audit_rules

    def _fake(conn):
        return {}, {"hammer"}, {"hammer": "产品清理报错扫描"}

    monkeypatch.setattr(audit_rules, "_brand_map", _fake)
    monkeypatch.setattr(audit_rules, "_rows_dict",
                        lambda conn, sql, key: {})
    monkeypatch.setattr(audit_rules, "_frozen", lambda conn, sql: frozenset())
    monkeypatch.setattr(audit_rules, "_pairs", lambda conn, sql: {})
    monkeypatch.setattr(audit_rules, "_build_automaton", lambda w: None)
    monkeypatch.setattr(audit_rules.audit_l2, "load_nice_mapping",
                        lambda *a, **k: ({}, []))
    monkeypatch.setattr(audit_rules.category_blacklist, "load",
                        lambda conn: None)

    class _Cur:
        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    with caplog.at_level("WARNING"):
        ctx = audit_rules.load_context(_Conn())
    assert ctx.r4_source == {"hammer": "产品清理报错扫描"}
    assert any("TRO 前缀 0 词" in r.getMessage() for r in caplog.records)


def test_context_default_keeps_hand_built_ctx_working():
    """r4_source 带默认值:测试里手搓的 ctx 不给它也照跑(TRO 那路退化成空)。"""
    from services import audit_rules
    ctx = audit_rules.AuditContext(
        phase0_sellers=frozenset(), phase0_asins=frozenset(),
        brand_blacklist={}, pt_meta={}, ac_automaton=None,
        nice_mapping={}, nice_default=[])
    assert ctx.r4_source == {}
    assert audit_store.tro_hits(_outcome(["dyson"]), ctx.r4_source,
                                _PREFIX)["confirmed"] == []


# ── 钩子:dedupe 先于事件 ───────────────────────────────────────────────────

class _RecCur:
    """记录 execute/executemany 顺序的假游标;dedupe 首次落键、再来 rowcount=0。"""

    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0

    def execute(self, sql, args=None):
        self.conn.calls.append(("execute", sql, args))
        if "ops.dedupe" in sql:
            key = (args["scope"], args["key"])
            self.rowcount = 0 if key in self.conn.keys else 1
            self.conn.keys.add(key)
        else:
            self.rowcount = 1

    def executemany(self, sql, rows):
        self.conn.calls.append(("executemany", sql, list(rows)))

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RecConn:
    def __init__(self):
        self.calls: list = []
        self.keys: set = set()

    def cursor(self):
        return _RecCur(self)


class _Ctx:
    r4_source = _SRC


def _kinds(conn):
    """输入:假连接 → 输出:调用类型序列('dedupe' / 'event')。"""
    out = []
    for how, sql, _args in conn.calls:
        if "ops.dedupe" in sql:
            out.append("dedupe")
        elif "ops.store_events" in sql:
            out.append("event")
    return out


def test_dedupe_is_written_before_the_event(monkeypatch):
    """事件表只追加、重复行不可撤 —— 去重键必须先落库,rowcount==1 才写事件。"""
    monkeypatch.setattr(pa, "_tro_expand", lambda conn, brand, state: 3)
    conn = _RecConn()
    state = {"brands": set(), "unjudged_brands": set(),
             "new": 0, "expo": 0, "errors": 0}
    pa._tro_hook(conn, _outcome(["dyson"], _l3(brands=[
        {"brand": "dyson", "is_real_brand": True}])), _Ctx(), state)
    assert _kinds(conn) == ["dedupe", "event", "dedupe"]   # 展开的键也在事件后
    assert state == {"brands": {"dyson"}, "unjudged_brands": set(),
                     "new": 1, "expo": 3, "errors": 0}


def test_second_product_of_the_same_brand_writes_nothing(monkeypatch):
    """一个 TRO 品牌一辈子报一次:同品牌第二条产品只碰去重键,不再写事件。"""
    monkeypatch.setattr(pa, "_tro_expand", lambda conn, brand, state: 3)
    conn = _RecConn()
    state = {"brands": set(), "unjudged_brands": set(),
             "new": 0, "expo": 0, "errors": 0}
    oc = _outcome(["dyson"], _l3(brands=[{"brand": "dyson",
                                          "is_real_brand": True}]))
    pa._tro_hook(conn, oc, _Ctx(), state)
    conn.calls.clear()
    pa._tro_hook(conn, oc, _Ctx(), state)
    assert _kinds(conn) == ["dedupe", "dedupe"]      # 两个键都已被占,零事件
    assert state["new"] == 1 and state["expo"] == 3  # 数字不重复涨
    assert state["brands"] == {"dyson"}              # 命中数按品牌去重


def test_unjudged_brand_is_recorded_but_never_expanded(monkeypatch):
    """没判定就展开 = 拿一个可能是通用英文词的"品牌"去标一批店。"""
    monkeypatch.setattr(pa, "_tro_expand",
                        lambda *a, **k: pytest.fail("未判品牌不该展开"))
    conn = _RecConn()
    state = {"brands": set(), "unjudged_brands": set(),
             "new": 0, "expo": 0, "errors": 0}
    pa._tro_hook(conn, _outcome(["dyson"], l3=None), _Ctx(), state)
    assert _kinds(conn) == ["dedupe", "event"]
    row = conn.calls[1][2][0]
    assert row[0] is None and row[1] == se.TRO_BRAND_HIT and row[2] == "mid"
    assert '"judged": false' in row[4]
    assert state["unjudged_brands"] == {"dyson"} and state["expo"] == 0


def test_expansion_still_happens_after_an_earlier_unjudged_report(monkeypatch):
    """先"未判"报过、后来 L3 确认:源头事件被键挡下(一个品牌一条源头),
    **波及展开不能跟着被挡** —— 那才是要人去看的那一半。"""
    monkeypatch.setattr(pa, "_tro_expand", lambda conn, brand, state: 2)
    conn = _RecConn()
    state = {"brands": set(), "unjudged_brands": set(),
             "new": 0, "expo": 0, "errors": 0}
    pa._tro_hook(conn, _outcome(["dyson"], l3=None), _Ctx(), state)
    conn.calls.clear()
    pa._tro_hook(conn, _outcome(["dyson"], _l3(brands=[
        {"brand": "dyson", "is_real_brand": True}])), _Ctx(), state)
    assert _kinds(conn) == ["dedupe", "dedupe"]   # 源头被挡,展开键是新的
    assert state["expo"] == 2


# ── 摘要:非零才打印 ───────────────────────────────────────────────────────

def _summary_lines(**kw):
    counts = pa.Counts(
        verdicts={"pass": 1, "reject": 0, "pending": 0}, cand_n=1, todo_n=1,
        l0_untouched=0, adopted_n=0, no_title=0, seller_missing=0,
        policy_unknown=0, row_errors=0, asked_asins=0, uspto_failures=0,
        uspto_off=True, **kw)
    opts = pa.Opts(execute=True, limit=1, backfill=False, adopt_only=False,
                   r5_on=False, run_l3=True, run_l4=False, only_l0=False,
                   workers=1, conn_note="")
    stage = {"L3_ran": 1, "L3_reject": 0, "L3_pending": 0,
             "L4_ran": 0, "L4_reject": 0}
    return pa._summary(opts, counts, stage, {}, {}, 0, [])


def test_summary_prints_tro_only_when_non_zero():
    assert not [ln for ln in _summary_lines() if "TRO" in ln]
    lines = _summary_lines(tro_n=2, tro_new=1, tro_expo=5, tro_unjudged=3,
                           tro_errors=1)
    assert "🚨 TRO 品牌命中 2(首报 1,波及 5 店)" in lines
    assert any("TRO 嫌疑未判 3" in ln for ln in lines)
    assert any("TRO 接线失败 1 次" in ln for ln in lines)


# ── 沙箱 PG 集成 ────────────────────────────────────────────────────────────
#
# ⚠ 地址是**测试夹具**(生产走 registry/db.pg_dsn() 的 unix socket);非标准
# 端口 55432 正是为了不可能连到生产库。本节造数据,整场事务最后回滚。
_PG_HOST, _PG_PORT = "127.0.0.1", 55432
_DSN = os.environ.get(
    "WALMART_TEST_PG_DSN",
    f"host={_PG_HOST} port={_PG_PORT} user=postgres dbname=walmart_data")


def _pg_up() -> bool:
    try:
        with socket.create_connection((_PG_HOST, _PG_PORT), timeout=1):
            return True
    except OSError:
        return False


needs_pg = pytest.mark.skipif(not _pg_up(),
                              reason=f"沙箱 PG {_PG_HOST}:{_PG_PORT} 未启动")

_TB = "zqx tro brand"          # 与库里既有数据绝无重叠
_TA1, _TA2 = "B0TROHOOK001", "B0TROHOOK002"


@pytest.fixture
def pg(monkeypatch):
    """输入:无 → 输出:沙箱 PG 连接(整场事务**最后一律回滚**)。"""
    monkeypatch.setenv("WALMART_PG_DSN", _DSN)
    from registry import db
    with db.pg_conn() as conn:
        try:
            yield conn
        finally:
            conn.rollback()


def _seed(conn):
    """输入:连接 → 输出:无。一个 TRO 品牌两个 ASIN 散在两家店。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO catalog.products (marketplace, asin, brand) "
            "VALUES ('US', %s, %s), ('US', %s, %s)", (_TA1, _TB, _TA2, _TB))
        cur.execute(
            "INSERT INTO catalog.walmart_items "
            "(store, sku, missing_since, lifecycle_status, last_seen_at) VALUES "
            "('A085', %s, NULL, NULL, now()),"        # 在架
            "('B012', %s, now(), NULL, now())",       # 历史:已缺席
            (_TA1, _TA2))


@needs_pg
def test_pg_hook_writes_one_origin_row_and_one_row_per_store(pg, monkeypatch):
    """源头一条(store IS NULL)+ 波及逐店一条,形状与 severity 都钉住。"""
    _seed(pg)
    monkeypatch.setattr(pa.stores, "registered_names",
                        lambda: {"A085", "B012"})
    state = {"brands": set(), "unjudged_brands": set(),
             "new": 0, "expo": 0, "errors": 0}
    ctx = type("C", (), {"r4_source": {_TB: "TRO品牌"}})()
    pa._tro_hook(pg, _outcome([_TB], _l3(brands=[
        {"brand": _TB.upper(), "is_real_brand": True,
         "evidence": "真品牌"}]), asin=_TA1), ctx, state)

    with pg.cursor() as cur:
        cur.execute(
            "SELECT store, event, severity, source, detail FROM ops.store_events"
            " WHERE detail->>'brand' = %s ORDER BY store NULLS FIRST", (_TB,))
        rows = cur.fetchall()
    assert len(rows) == 3                      # 源头 1 + 波及 2
    origin = rows[0]
    assert origin[0] is None and origin[1] == se.TRO_BRAND_HIT
    assert origin[2] == "high" and origin[3] == "product_audit"
    assert origin[4]["first_asin"] == _TA1 and origin[4]["judged"] is True
    assert origin[4]["source"] == "TRO品牌"
    expo = {r[0]: r for r in rows[1:]}
    assert set(expo) == {"A085", "B012"}
    for r in expo.values():
        assert r[1] == se.TRO_BRAND_EXPOSURE and r[2] == "mid"
        assert "registered_unchecked" not in r[4]
    assert expo["A085"][4]["still_listed"] is True      # 在架
    assert expo["B012"][4]["still_listed"] is False     # 已缺席
    assert expo["A085"][4]["asins"] == [_TA1]
    assert state["new"] == 1 and state["expo"] == 2


@needs_pg
def test_pg_feishu_failure_degrades_to_unchecked_expansion(pg, monkeypatch):
    """飞书挂了不许把波及展开整段丢掉:按 registered=None 展开并逐行标未校验。"""
    _seed(pg)

    def _boom():
        raise RuntimeError("飞书 503")

    monkeypatch.setattr(pa.stores, "registered_names", _boom)
    state = {"brands": set(), "unjudged_brands": set(),
             "new": 0, "expo": 0, "errors": 0}
    ctx = type("C", (), {"r4_source": {_TB: "tro"}})()
    pa._tro_hook(pg, _outcome([_TB], _l3(brands=[
        {"brand": _TB, "is_real_brand": True}]), asin=_TA1), ctx, state)
    with pg.cursor() as cur:
        cur.execute("SELECT detail FROM ops.store_events "
                    "WHERE event = %s AND detail->>'brand' = %s",
                    (se.TRO_BRAND_EXPOSURE, _TB))
        details = [r[0] for r in cur.fetchall()]
    assert details and all(d["registered_unchecked"] is True for d in details)
    assert state["errors"] == 0          # 飞书失败不算接线失败,是有意的降级


@needs_pg
def test_pg_rerun_is_idempotent(pg, monkeypatch):
    """重跑不涨行:dedupe 两个 scope 各自挡住源头与展开。"""
    _seed(pg)
    monkeypatch.setattr(pa.stores, "registered_names", lambda: {"A085", "B012"})
    ctx = type("C", (), {"r4_source": {_TB: "TRO品牌"}})()
    oc = _outcome([_TB], _l3(brands=[{"brand": _TB, "is_real_brand": True}]),
                  asin=_TA1)
    for _ in range(3):
        state = {"brands": set(), "unjudged_brands": set(),
                 "new": 0, "expo": 0, "errors": 0}
        pa._tro_hook(pg, oc, ctx, state)
    with pg.cursor() as cur:
        cur.execute("SELECT count(*) FROM ops.store_events "
                    "WHERE detail->>'brand' = %s", (_TB,))
        assert cur.fetchone()[0] == 3        # 仍是 源头 1 + 波及 2
    assert state["new"] == 0 and state["expo"] == 0
