"""回放评估 `workflows/audit_replay` 回归(2026-09-02 B2,规格 §3.8/§3.9)。

这条工作流的价值全在"它说的数能不能信",所以钉的是**四类会静默骗人的接缝**:

  1. **只读守门** —— 它是评估不是生产。真写了 `catalog.products` /
     `audit_runs` / `audit_hits` 的话,一次"评估"就把几百条结论盖成了回放结果,
     而且看起来一切正常(SQL 动词扫描 + 依赖扫描,双保险);
  2. **抽样恒定** —— 同 seed 必须恒同一批样本:这条工作流唯一的用法是
     "改判据前后拿同一批样本对比",样本漂了两次报告就没有可比性,而漂了不报错;
  3. **打标零推断** —— 沃尔玛裁决 → 期望值的映射就是规格 §3.8 那张表;
     `sku → asin` 只准走 `services/sku_asin`(裸 `sku = asin` 在生产上是
     "永远查空"而不是报错);
  4. **报告与成本** —— 报告拿假数据也要拼得出来(几百条真样本没法在单测里跑),
     成本预估的算术要有人钉(它是 dry-run 的**唯一**产出)。

离线:不打网、不连库、零 LLM —— 判定链一律 monkeypatch,DB 走假连接。
"""

import re

import pytest

from registry import paths, resources
from services import audit_rules, error_taxonomy
from services.audit_models import (AuditOutcome, L1Info, L2Result,
                                   Phase0Result, RuleHit)
from workflows import audit_replay as ar

POLICIES = ["Alcohol", "Intellectual Property", "Offensive Content",
            "Content standards: Overview", "Product details policy"]


def _res(text: str):
    return error_taxonomy.classify_reasons(
        error_taxonomy.split_reasons(text), POLICIES)


# ══════════════════════════════════════════════════════════════════════════════
#  ① 只读守门(SQL 动词 + 依赖)
# ══════════════════════════════════════════════════════════════════════════════

_WRITE_RE = re.compile(
    r"(?i)\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([A-Za-z_][\w.]*)")


def _source() -> str:
    import inspect
    return inspect.getsource(ar)


def test_the_only_table_it_writes_is_its_own():
    """⚠ 回放**只写自己的表**。

    写了结论表的后果不是报错,是一次"评估"把几百条产品的 `audit_status` /
    `audit_reason` 盖成了回放结果 —— 而回放的样本恰恰是"沃尔玛已经拒过的品"
    与"在架在售的品",两头都是最不该被乱改的。
    """
    targets = {t.lower() for t in _WRITE_RE.findall(_source())}
    targets.discard("set")                  # `ON CONFLICT … DO UPDATE SET`
    assert targets == {"audit.replay_results"}, f"多出来的写目标:{targets}"
    for forbidden in ("catalog.products", "audit.audit_runs",
                      "audit.audit_hits", "catalog.product_events"):
        assert f"INSERT INTO {forbidden}" not in _source()
        assert f"UPDATE {forbidden}" not in _source()


def test_it_does_not_even_import_the_writing_channels():
    """SQL 扫描挡的是自己写的语句;这条挡的是**借别人的手写** ——
    `audit_store.write_conclusion` / `product_events` / 飞书投影
    随便哪一个被 import 进来,上面那条正则就看不见了。"""
    src = _source()
    for mod in ("product_events", "listing_sheet", "feishu", "audit_l4"):
        assert f"import {mod}" not in src and f", {mod}" not in src, mod
    # audit_store 只准用来渲染"具体内容"那一格,不准落库
    assert "audit_store.conclusion_detail(" in src
    assert "write_conclusion" not in src and "persist_run" not in src
    assert "run_l4=False" in src            # L4 默认关(批复 #2)


def test_the_docstring_discloses_the_llm_cache_write():
    """判定链自己会写 `catalog.llm_cache`(L3 判完缓存出参)。

    那是缓存不是结论,而且与生产共用一份(回放付过的钱随后真重审直接命中)
    —— 但头注**必须说出来**:一条自称"只写两处"的工作流,漏说第三处写面
    就是在骗读它的人。
    """
    assert "llm_cache" in (ar.__doc__ or "")
    assert ar.DANGEROUS is False


def test_dry_run_semantics_are_documented_and_parsed():
    """`--dry-run` 是 cli 塞进来的键(2026-08-16 那次上线当天炸过一次)。"""
    assert {"execute", "dry_run"} <= ar._CLI_INJECTED
    assert ar._parse_params({"dry_run": True}).dry_run is True
    with pytest.raises(ValueError, match="未识别参数"):
        ar._parse_params({"negs": 10})              # 手滑拼错:宁炸不吞
    with pytest.raises(ValueError, match="neg 要整数"):
        ar._parse_params({"neg": "六百"})
    with pytest.raises(ValueError, match="不能为负"):
        ar._parse_params({"pos": "-1"})
    with pytest.raises(ValueError, match="不能同时为 0"):
        ar._parse_params({"neg": "0", "pos": "0"})


def test_defaults_come_from_the_owner_ruling_and_registry():
    """反例 600 / 正例 400 是所有者定稿 §六.5;并发口径与 product_audit 同源。"""
    o = ar._parse_params({})
    assert (o.neg, o.pos) == (600, 400)
    assert o.workers == resources.AUDIT_WORKERS_DEFAULT
    assert o.tag.endswith(resources.AUDIT_RULES_VERSION)    # 缺省 tag 带判据版本
    assert ar._parse_params({"workers": "9999"}).workers == \
        resources.AUDIT_WORKERS_MAX


# ══════════════════════════════════════════════════════════════════════════════
#  ② 打标:沃尔玛裁决 → 期望值(规格 §3.8 那张表,一条推断都没有)
# ══════════════════════════════════════════════════════════════════════════════

def test_label_policy_maps_to_the_table_spelling():
    """POLICY → 抽出的政策名 join 政策表得到的**表内原拼写**。"""
    got = ar.label(_res("This is a prohibited product under our policy. "
                        "Prohibited Products Policy: Alcohol."), POLICIES)
    assert got == ("Alcohol", "Alcohol")


def test_label_policy_that_does_not_join_is_dropped():
    """join 不上就**不进集**:没有可比的类别名,硬凑一个等于给自己送分。

    抽不出名的通用政策拒同样不进(生产 3,363 条,那是常态不是缺口)。
    """
    assert ar.label(_res("This is a prohibited product under our policy. "
                         "Prohibited Products Policy: Fireworks."), POLICIES) is None
    assert ar.label(_res("This is a prohibited product under our policy."),
                    POLICIES) is None


def test_label_ip_and_content_and_verdict_only_codes():
    ip = ar.label(_res("Removed for an intellectual property claim."), POLICIES)
    assert ip == (resources.AUDIT_IP_POLICY, resources.AUDIT_IP_POLICY)
    # 内容族:期望值记规范名(索引页),分层键单独一个桶
    content = ar.label(_res("This item has content issues."), POLICIES)
    assert content == (resources.AUDIT_CONTENT_POLICIES[0], "内容族")
    # 只比判定的两档:期望类别 None
    assert ar.label(_res("Removed due to brand restrictions."),
                    POLICIES) == (None, "BRAND")
    assert ar.label(_res("Prohibited and not eligible for appeal."),
                    POLICIES) == (None, "PROHIBITED_FINAL")


def test_label_keeps_pt_wrong_and_gated_out_of_the_set():
    """PT_WRONG 是 L1 的题(类目选错),GATED 的沃尔玛语义与我方"能不能做"
    不对齐 —— 规格 §3.8 明确不进本集。混进来会把回放的召回率算低。"""
    assert ar.label(_res("Ensure the appropriate product type selected."),
                    POLICIES) is None
    assert ar.label(_res("This category requires pre-approval to sell."),
                    POLICIES) is None
    assert ar.label(_res("The end date has passed."), POLICIES) is None
    assert set(ar._NEG_CODES) == {"POLICY", "IP", "CONTENT", "BRAND",
                                  "PROHIBITED_FINAL"}


def test_category_ok_is_exact_except_the_content_family():
    """类别对不对是**枚举精确等值**,唯一的松口是内容族两名互认
    (43 索引页 / 44 明细页,沃尔玛报错正文不区分)。"""
    assert ar.category_ok("Alcohol", "Alcohol")
    assert not ar.category_ok("Alcohol", "alcohol")          # 大小写不算对
    assert not ar.category_ok("Alcohol", None)
    a, b = resources.AUDIT_CONTENT_POLICIES
    assert ar.category_ok(a, b) and ar.category_ok(b, a)
    assert not ar.category_ok(a, "Alcohol")
    assert ar.category_ok(None, "任何值")                    # 只比判定


# ══════════════════════════════════════════════════════════════════════════════
#  ③ 抽样:同 seed 恒定 + 每类封顶
# ══════════════════════════════════════════════════════════════════════════════

def _samples(n_per, cats=("A", "B", "C")):
    return [ar.Sample(asin=f"B{c}{i:04d}", expected_verdict="reject",
                      expected_category=c, stratum=c, source="neg")
            for c in cats for i in range(n_per)]


def test_sampling_is_deterministic_per_seed():
    """同 seed 恒同一批 —— 这条工作流唯一的用法是"改判据前后同样本对比"。"""
    rows = _samples(50)
    a = [s.asin for s in ar.stratify(rows, 30, 10, 42)]
    b = [s.asin for s in ar.stratify(rows, 30, 10, 42)]
    assert a == b and a == sorted(a)
    c = [s.asin for s in ar.stratify(rows, 30, 10, 7)]
    assert c != a, "换 seed 就该换一批(否则 seed 是摆设)"


def test_sampling_caps_each_category_and_then_the_total():
    """封顶是为了让**各类别都判得到**:量最大的那一两类不封顶会吃满整个样本。"""
    rows = _samples(50, cats=("A", "B", "C", "D"))
    picked = ar.stratify(rows, 40, 10, 42)
    from collections import Counter
    per = Counter(s.stratum for s in picked)
    assert set(per) == {"A", "B", "C", "D"} and all(v == 10 for v in per.values())
    # 总目标更小时再裁一次(仍然定序、仍然恒定)
    small = ar.stratify(rows, 12, 10, 42)
    assert len(small) == 12
    assert [s.asin for s in small] == [s.asin for s in ar.stratify(rows, 12, 10, 42)]
    # 稀有类别有多少给多少,不会被封顶补齐
    thin = ar.stratify(_samples(3, cats=("A",)) + _samples(20, cats=("B",)),
                       100, 10, 42)
    # neg=0 / pos=0 = 这一半样本整个不要(不是"不限量")
    assert ar.stratify(rows, 0, 10, 42) == [] and ar.sample_asins(["B0A"], 0, 1) == []
    assert Counter(s.stratum for s in thin) == {"B": 10, "A": 3}


def test_one_stratum_growing_does_not_move_the_other_strata():
    """⚠ 每层一个独立种子:共用一个 RNG 时,某一层候选量变了(库里多了几条
    下架记录)会让**后面所有层**整体错位,改判据前后就没法对比了。"""
    base = _samples(20, cats=("A", "B"))
    grown = base + [ar.Sample(asin=f"BA9{i:03d}", expected_verdict="reject",
                              expected_category="A", stratum="A", source="neg")
                    for i in range(30)]
    pick_b = {s.asin for s in ar.stratify(base, 999, 5, 42) if s.stratum == "B"}
    pick_b2 = {s.asin for s in ar.stratify(grown, 999, 5, 42) if s.stratum == "B"}
    assert pick_b == pick_b2


def test_positive_sampling_and_default_cap():
    pool = [f"B0POS{i:04d}" for i in range(100)]
    a = ar.sample_asins(pool, 10, 42)
    assert a == ar.sample_asins(pool, 10, 42) == sorted(a) and len(a) == 10
    assert ar.sample_asins(pool, 500, 42) == sorted(pool)      # 不够就全给
    assert ar.default_cap(600, 30) == 20
    assert ar.default_cap(600, 300) == ar._MIN_CAP             # 下限兜住


# ══════════════════════════════════════════════════════════════════════════════
#  ④ sku → asin 只走 services/sku_asin(裸等值在生产上是"永远查空")
# ══════════════════════════════════════════════════════════════════════════════

class _Cur:
    """按 SQL 分流的假游标(未预期的 SQL 直接炸,不静默)。"""

    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, args=None):
        self.conn.sql.append(sql)
        c = self.conn
        if "published_status = 'PUBLISHED'" in sql:
            self._rows = list(c.pos)        # ⚠ 先判:正例 SQL 的 NOT EXISTS
        elif "unpublished_reasons, '') <> ''" in sql and "md5" in sql:
            self._rows = list(c.neg)        #    里同样带着下架原因那一句
        elif sql.strip().startswith("SELECT asin FROM catalog.products"):
            want = set(args["asins"])
            self._rows = [(a,) for a in sorted(want & set(c.products))]
        elif "SELECT DISTINCT item_id, sku" in sql:
            self._rows = [(k, v) for k, v in sorted(c.item_ids.items())]
        elif sql.lstrip().startswith("SELECT p.asin"):
            want = set(args["asins"])
            cols = ["asin", "title", "brand", "walmart_pt", "pt_source",
                    "browse_node_id", "browse_node_chain",
                    "amazon_category_path", "bullet_points",
                    "long_description", "seller_id", "seller_name"]
            self.description = [(x,) for x in cols]
            self._rows = [(a, c.products[a], "", None, None, "", "", "",
                           None, "", "", "")
                          for a in sorted(want & set(c.products))]
        elif "FROM audit.audit_runs" in sql:
            self._rows = [(a, v, cat) for a, (v, cat) in sorted(c.old.items())]
        elif sql.lstrip().startswith("INSERT INTO audit.replay_results"):
            c.written.append(args)
        else:
            raise AssertionError(f"未预期 SQL:{' '.join(sql.split())[:90]}")

    def executemany(self, sql, rows):
        for r in rows:
            self.execute(sql, r)

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, neg=(), pos=(), products=None, old=None, item_ids=None):
        self.neg, self.pos = list(neg), list(pos)
        self.products = products or {}
        self.old = old or {}
        self.item_ids = item_ids or {}
        self.sql: list = []
        self.written: list = []
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, *a, **k):
        return _Cur(self)

    def commit(self):
        self.commits += 1


_REASON_IP = "Removed for an intellectual property claim."


def test_sku_is_mapped_through_sku_asin_never_by_bare_equality():
    """⚠ 生产 SKU 形如 `XKJ-B0GXX75JN5-39.98` / `102460018738`(纯数字 item id),
    `sku = asin` 是 2026-08-11 被所有者推翻的旧约定。裸等值在这里不会报错 ——
    它会让整个反例集变成"库里查不到产品行",回放报告直接空转。
    """
    conn = _Conn(
        neg=[("XKJ-B0GXX75JN5-39.98", _REASON_IP),   # 三段式:中段即源头码
             ("B0PLAIN001", _REASON_IP),            # 裸 ASIN
             ("102460018738", _REASON_IP)],          # 纯数字:倒查 item_id
        products={"B0GXX75JN5": "t1", "B0PLAIN001": "t2", "B0FROMID01": "t3"},
        item_ids={"102460018738": "AB-B0FROMID01-12.00"})
    rows, st = ar._negatives(conn, ar._parse_params({"neg": "10"}), POLICIES)
    assert sorted(s.asin for s in rows) == ["B0FROMID01", "B0GXX75JN5",
                                            "B0PLAIN001"]
    assert all(s.expected_verdict == "reject" for s in rows)
    assert st["coded"] == 3 and st["no_asin"] == 0
    # 规则出处只有一处:SQL 里没有任何 sku↔asin 的裸等值
    src = _source()
    assert "sku_asin.resolve_skus(" in src
    assert "w.sku = p.asin" not in src and "sku = asin" not in src


def test_negatives_drop_rows_without_a_product_row():
    """库里没有产品行(或无标题=采集降级)的进不了样本:没有可判的正文。"""
    conn = _Conn(neg=[("B0PLAIN001", _REASON_IP), ("B0MISSING1", _REASON_IP)],
                 products={"B0PLAIN001": "t"})
    rows, st = ar._negatives(conn, ar._parse_params({"neg": "10"}), POLICIES)
    assert [s.asin for s in rows] == ["B0PLAIN001"] and st["no_product"] == 1


def test_negatives_count_the_funnel_by_reason_not_by_lump():
    """漏斗要分档记:抽不出政策名(常态)与 join 不上表(政策表缺口)是两件事,
    混成一个数会让人以为政策表烂了。"""
    conn = _Conn(neg=[("B0A0000001", "This is a prohibited product under our "
                                     "policy."),
                      ("B0A0000002", "This is a prohibited product under our "
                                     "policy. Prohibited Products Policy: "
                                     "Fireworks."),
                      ("B0A0000003", "Ensure the appropriate product type "
                                     "selected.")],
                 products={})
    _, st = ar._negatives(conn, ar._parse_params({"neg": "10"}), POLICIES)
    assert st["policy_noname"] == 1 and st["policy_unjoined"] == 1
    assert st["off_set"] == 1 and st["coded"] == 0


def test_positives_exclude_asins_already_taken_by_negatives():
    """同一个 asin 可能既有在架 sku 又有被拒 sku(不同店/不同订货号)——
    两边都收就是拿同一个产品既当正例又当反例。"""
    conn = _Conn(pos=[("B0PLAIN001",), ("B0PLAIN002",)],
                 products={"B0PLAIN001": "t", "B0PLAIN002": "t"})
    got, st = ar._positives(conn, ar._parse_params({"pos": "10"}),
                            exclude={"B0PLAIN001"})
    assert got == ["B0PLAIN002"] and st["dup_neg"] == 1


def test_the_sampling_sql_is_seeded_in_the_database_not_in_python():
    """抽样封顶必须在**库里**:几十万行带长文本全拉回来是 2026-08-21 那次
    OOM 的同款走法。`md5(sku || seed)` 保证同 seed 恒定。"""
    for sql in (ar._NEG_SQL, ar._POS_SQL):
        assert "md5(t.sku || %(seed)s)" in sql and "LIMIT %(pool)s" in sql
    assert "NOT EXISTS" in ar._POS_SQL      # 别的店给过下架原因的不算正例
    assert ar._pool_size(600, ar._NEG_POOL_FACTOR) <= ar._POOL_MAX


def test_product_rows_have_the_same_shape_as_production_candidates():
    """⚠ 回放喂进去的产品正文必须与生产**同一份**:少一个 `seller_id`
    就等于卖家闸在回放里恒不命中,而两边的结论看着都正常。"""
    assert audit_rules.PRODUCT_ROW_COLUMNS in ar._PRODUCT_SQL
    assert audit_rules.PRODUCT_ROW_FROM in ar._PRODUCT_SQL
    from workflows import product_audit
    assert audit_rules.PRODUCT_ROW_COLUMNS in product_audit._CANDIDATE_SQL


# ══════════════════════════════════════════════════════════════════════════════
#  ⑤ 成本预估(dry-run 的唯一产出)
# ══════════════════════════════════════════════════════════════════════════════

def test_cost_estimate_arithmetic():
    """口径(规格 §3.9):**首条前缀未命中**(所以要串行预热),其余命中;
    每条另加 user 段(未命中价)与输出。"""
    model, tier = "deepseek-v4-flash", "offpeak"
    hit, miss, out = resources.LLM_PRICING[model][tier]
    chars = 35_000                       # 折算成 10,000 token
    est, prefix = ar.estimate_cost(3, chars, model, tier)
    assert prefix == 10_000
    want = ((prefix + ar._USER_TOKENS) * miss + ar._OUT_TOKENS * out
            + prefix * 2 * hit + ar._USER_TOKENS * 2 * miss
            + ar._OUT_TOKENS * 2 * out) / 1_000_000
    assert est == pytest.approx(want)
    # 只有一条时没有"其余"
    one, _ = ar.estimate_cost(1, chars, model, tier)
    assert one == pytest.approx(((prefix + ar._USER_TOKENS) * miss
                                 + ar._OUT_TOKENS * out) / 1_000_000)
    assert ar.estimate_cost(0, chars, model, tier)[0] == 0.0
    # 峰值时段更贵(谷时段减半是排班的全部理由)
    peak, _ = ar.estimate_cost(3, chars, model, "peak")
    assert peak > est


def test_unpriced_model_is_not_faked_as_zero():
    """按 0 计价会产出一个看着像钱、其实是编的数字(llm_cost 同一条纪律)。"""
    est, prefix = ar.estimate_cost(10, 35_000, "某个没登记的模型", "offpeak")
    assert est is None and prefix == 10_000


# ══════════════════════════════════════════════════════════════════════════════
#  ⑥ 报告拼装(拿假数据钉格式与算术)
# ══════════════════════════════════════════════════════════════════════════════

def _row(**kw):
    base = dict(asin="B0AAAAAAA1", expected_verdict="reject",
                expected_category="Alcohol", got_verdict="reject",
                got_category="Alcohol", got_detail="酒精饮品",
                stage_stopped_at="L3", old_verdict="reject",
                old_category="Alcohol", confidence="high", stratum="Alcohol",
                source="neg", reason="Prohibited Products Policy: Alcohol",
                pending_kind=None, error=None)
    base.update(kw)
    return base


_META = {"tag": "t1", "seed": 42, "cap": 10, "elapsed": 12.0,
         "neg_stats": {"scanned": 100, "pool_cap": 200, "coded": 40,
                       "no_asin": 3, "no_product": 5, "policy_unjoined": 2,
                       "policy_noname": 7},
         "pos_stats": {"scanned": 50, "pool_cap": 60, "no_asin": 1,
                       "no_product": 2},
         "cost_lines": ["预估成本 ≈ $0.12"]}


def test_report_states_the_known_limits_verbatim():
    """报告头必须逐字带上规格 §3.8 的三条局限:少了它们,读数的人会把
    "沃尔玛裁决"当金标、把绝对准确率当结论。"""
    summary, full = ar.report([_row()], _META)
    text = "\n".join(summary)
    assert "已知局限" in text
    assert "产品正文只有**当前值**" in text
    assert "政策版本与今天不同" in text
    assert "参照不是金标" in text
    assert "横向比" in text
    assert text.startswith("回放评估 audit_replay(run_tag=t1")
    assert resources.AUDIT_RULES_VERSION in text
    assert full[:len(summary)] == summary        # 全文含摘要,不另起一套


def test_report_metrics_add_up():
    rows = [
        # 反例 4:两条判对(其中一条类别错)、一条放行、一条待定
        _row(asin="B01"),
        _row(asin="B02", got_category="Offensive Content"),
        _row(asin="B03", got_verdict="pass", got_category=None,
             stage_stopped_at=None, confidence="low"),
        _row(asin="B04", got_verdict="pending", got_category=None,
             confidence=None, pending_kind="llm_bad_json"),
        # 正例 3:新链误伤 1;旧链误伤 2
        _row(asin="B05", source="pos", stratum="正例", expected_verdict="pass",
             expected_category=None, got_verdict="pass", got_category=None,
             old_verdict="reject", confidence="high"),
        _row(asin="B06", source="pos", stratum="正例", expected_verdict="pass",
             expected_category=None, got_verdict="reject",
             got_category="Alcohol", old_verdict="reject", confidence="low"),
        _row(asin="B07", source="pos", stratum="正例", expected_verdict="pass",
             expected_category=None, got_verdict="pass", got_category=None,
             old_verdict="pass", confidence="medium"),
    ]
    text = "\n".join(ar.report(rows, _META)[0])
    assert "反例召回(沃尔玛拒了,我们也拒):2/4 = 50.0%" in text
    assert "类别准确率(带类别反例 4 条" in text and "1/4 = 25.0%" in text
    assert "新链 1/3 = 33.3%" in text and "旧链 2/3 = 66.7%" in text
    assert "底线达标" in text                      # 新 ≤ 旧
    assert "pending(判不了,不是判过了):1/7" in text
    assert "llm_bad_json" in text                  # pending 按来源分层
    assert "混淆表" in text and "Offensive Content" in text
    assert "high" in text and "low" in text        # confidence 分层
    assert "预估成本 ≈ $0.12" in text


def test_report_flags_when_the_new_chain_hurts_more_than_the_old_one():
    """所有者定稿 §六.5 的底线:**正例误伤率不高于旧链**。
    不达标必须在报告里说"先修判据再回放,别开 mode=stale"。"""
    rows = [_row(asin="B0%d" % i, source="pos", stratum="正例",
                 expected_verdict="pass", expected_category=None,
                 got_verdict="reject" if i < 2 else "pass",
                 got_category=None, old_verdict="pass", confidence="medium")
            for i in range(4)]
    text = "\n".join(ar.report(rows, _META)[0])
    assert "新链误伤高于旧链" in text and "别开 mode=stale" in text


def test_full_report_lists_every_mismatch_but_the_summary_does_not():
    """逐条清单几百行,进摘要就等于把飞书通知刷爆;但**全文必须有** ——
    "哪一条判错了、沃尔玛当时说了什么"是改提示词的唯一入口。"""
    rows = [_row(asin="B0BAD00001", got_verdict="pass", got_category=None,
                 reason="Prohibited Products Policy: Alcohol")]
    summary, full = ar.report(rows, _META)
    assert "判定不符逐条" not in "\n".join(summary)
    body = "\n".join(full)
    assert "判定不符逐条" in body and "B0BAD00001" in body
    assert "沃尔玛:Prohibited Products Policy: Alcohol" in body


def test_pending_kind_splits_the_three_sources():
    """三种 pending 的处置完全不同(补映射 / 补 pt_meta / 改提示词)。"""
    l1 = L1Info(walmart_product_type=None)
    assert ar.pending_kind(AuditOutcome(asin="B0", verdict="pending",
                                        score_final=None,
                                        stage_stopped_at="L1", l1=l1)) \
        == "L1 类目解不出"
    assert ar.pending_kind(AuditOutcome(asin="B0", verdict="pending",
                                        score_final=100,
                                        stage_stopped_at="L2", l1=l1)) \
        == "L2 类目准入判不了"
    from services.audit_l3 import L3Result
    l3 = L3Result(verdict="pending", hits=[
        RuleHit(stage="L3", rule_code="llm_bad_policy", penalty=0)])
    assert ar.pending_kind(AuditOutcome(asin="B0", verdict="pending",
                                        score_final=100,
                                        stage_stopped_at="L3", l1=l1,
                                        l3=l3)) == "llm_bad_policy"


# ══════════════════════════════════════════════════════════════════════════════
#  ⑦ 端到端(判定链打桩):dry-run 零调用零落库 / 真跑写自己的表
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def wired(monkeypatch):
    """把一条完整回放接起来:库是假的、ctx 是假的、判定是桩。"""
    conn = _Conn(
        neg=[("B0NEG00001", _REASON_IP), ("B0NEG00002", _REASON_IP)],
        pos=[("B0POS00001",), ("B0POS00002",)],
        products={"B0NEG00001": "t1", "B0NEG00002": "t2",
                  "B0POS00001": "t3", "B0POS00002": "t4"},
        old={"B0NEG00001": ("reject", "Intellectual Property"),
             "B0POS00001": ("pass", None)})
    monkeypatch.setattr(ar.db, "pg_conn", lambda *a, **k: conn)
    monkeypatch.setattr(ar.db_guard, "cap_workers", lambda w: (2, ""))
    monkeypatch.setattr(ar.audit_rules, "load_context",
                        lambda c, **k: _FakeCtx())
    monkeypatch.setattr(ar.audit_l3, "system_prompt", lambda c: "x" * 35_000)
    calls: list = []

    def _fake_audit_one(product, ctx, c, **kw):
        calls.append(product.asin)
        return _outcome(product.asin)

    monkeypatch.setattr(ar.audit_rules, "audit_one", _fake_audit_one)
    return conn, calls


class _FakeCtx:
    known_policies = frozenset(POLICIES)
    pt_meta: dict = {}


def _outcome(asin: str) -> AuditOutcome:
    from services.audit_l3 import L3Result
    reject = asin.startswith("B0NEG")
    l3 = L3Result(verdict="reject" if reject else "pass",
                  policy=resources.AUDIT_IP_POLICY if reject else "none",
                  detail="未授权引用品牌名" if reject else None,
                  confidence="high")
    oc = AuditOutcome(asin=asin, verdict="reject" if reject else "pass",
                      score_final=100,
                      stage_stopped_at="L3" if reject else None,
                      l1=L1Info(walmart_product_type="Sneakers"),
                      phase0=Phase0Result(), l2=L2Result(score_final=100),
                      l3=l3)
    if reject:
        oc.final_reason_category = resources.AUDIT_IP_POLICY
    return oc


def test_dry_run_calls_no_llm_and_writes_nothing(wired):
    """`--dry-run` = 抽样 + 规模 + 预估成本。判定一次都不跑、库一行都不写、
    报告文件也不落 —— 它的用途是"这轮要花多少钱",不是"先跑一半"。"""
    conn, calls = wired
    out = ar.run({"neg": "5", "pos": "5", "dry_run": True, "execute": True})
    assert calls == [] and conn.written == [] and conn.commits == 0
    assert not any("INSERT" in s for s in conn.sql)
    assert "🧪" in out and "零 LLM" in out and "预估成本" in out
    assert "反例 2 / 正例 2" in out
    assert not (paths.reports_dir() / "audit_replay.txt").exists()


def test_real_run_writes_only_replay_results_and_the_report(wired):
    """真跑:每条样本都过一遍判定链,结果落自己的表 + 报告文件。"""
    conn, calls = wired
    out = ar.run({"neg": "5", "pos": "5", "tag": "T9", "execute": True})
    assert sorted(calls) == ["B0NEG00001", "B0NEG00002",
                            "B0POS00001", "B0POS00002"]
    assert len(conn.written) == 4 and conn.commits == 1
    one = [w for w in conn.written if w["asin"] == "B0NEG00001"][0]
    assert one["run_tag"] == "T9"
    assert one["expected_verdict"] == "reject"
    assert one["expected_category"] == resources.AUDIT_IP_POLICY
    assert one["got_verdict"] == "reject"
    assert one["got_category"] == resources.AUDIT_IP_POLICY
    assert one["got_detail"] == "未授权引用品牌名"      # 走 audit_store 同一份渲染
    assert one["confidence"] == "high"
    assert one["old_verdict"] == "reject"               # 旧链最近一次
    pos = [w for w in conn.written if w["asin"] == "B0POS00001"][0]
    assert pos["expected_verdict"] == "pass" and pos["expected_category"] is None
    # 报告落盘 + 摘要里点名"结论表没碰"
    text = paths.audit_replay_report().read_text(encoding="utf-8")
    assert "回放评估 audit_replay(run_tag=T9" in text and "已知局限" in text
    assert "反例召回" in out and "结论表一个字都没碰" in out
    assert "落库 audit.replay_results 4 行" in out


def test_real_run_survives_a_single_row_blowing_up(wired, monkeypatch):
    """一条判炸了记下来继续:整轮停掉的话前面几百条已付费的 LLM 结果一起白付。
    失败行照样落库(`got_verdict` 空)—— "这条判不出来"也是回放结果。"""
    conn, calls = wired

    def _boom(product, ctx, c, **kw):
        if product.asin == "B0NEG00001":
            raise RuntimeError("判定炸了")
        return _outcome(product.asin)

    monkeypatch.setattr(ar.audit_rules, "audit_one", _boom)
    out = ar.run({"neg": "5", "pos": "5", "execute": True})
    bad = [w for w in conn.written if w["asin"] == "B0NEG00001"][0]
    assert bad["got_verdict"] is None and len(conn.written) == 4
    assert "判定失败 1 条" in paths.audit_replay_report().read_text(
        encoding="utf-8")
    assert "落库 audit.replay_results 4 行" in out


def test_empty_policy_table_stops_the_round_instead_of_faking_accuracy(
        monkeypatch, wired):
    """政策表读不到 → 期望类别全靠它 join,空表跑出来的类别准确率是假的。"""
    class _Empty:
        known_policies = frozenset()
        pt_meta: dict = {}

    monkeypatch.setattr(ar.audit_rules, "load_context", lambda c, **k: _Empty())
    with pytest.raises(RuntimeError, match="policy_sync"):
        ar.run({"execute": True})
