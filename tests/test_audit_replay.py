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
        if sql.lstrip().startswith("SELECT asin, expected_verdict"):
            self._rows = list(c.tag_rows)          # 同 tag 既有样本
        elif sql.lstrip().startswith("SELECT DISTINCT sku FROM catalog"):
            self._rows = [(k,) for k in c.rejected]   # 曾被拒的 sku(asin 级判据)
        elif "published_status = 'PUBLISHED'" in sql:
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
    def __init__(self, neg=(), pos=(), products=None, old=None, item_ids=None,
                 tag_rows=(), rejected=()):
        self.neg, self.pos = list(neg), list(pos)
        self.products = products or {}
        self.old = old or {}
        self.item_ids = item_ids or {}
        self.tag_rows = list(tag_rows)
        self.rejected = list(rejected)
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
        self.sql.append("COMMIT")      # 顺序留痕:判定前必须已经提交过


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


def test_positives_exclude_the_whole_negative_pool_not_just_the_picked():
    """⚠ 排除面是**整个反例池**,不是抽中的那 600 条。

    只排抽中的:池里另外几千个"沃尔玛拒过"的 asin 照样能以正例身份进样本,
    新链判拒它们反而被算成"误伤" —— 直接污染所有者唯一的底线指标。
    """
    conn = _Conn(pos=[("B0PLAIN001",), ("B0PLAIN002",)],
                 products={"B0PLAIN001": "t", "B0PLAIN002": "t"})
    got, st = ar._positives(conn, ar._parse_params({"pos": "10"}),
                            {"B0PLAIN001"}, set())
    assert got == ["B0PLAIN002"] and st["dup_neg"] == 1
    # 接线:run() 传的是**池**而不是 picked
    src = _source()
    assert "{s.asin for s in neg_pool}" in src
    assert "rejected_asins(conn)" in src


def test_a_positive_sku_whose_asin_was_rejected_in_another_store_is_dropped():
    """⚠ `_POS_SQL` 的 NOT EXISTS 是 **sku 级**,而身份是 **asin 级**。

    同一个产品:A 店订货号 `XKJ-B0GXX75JN5-39.98` 在架、B 店订货号
    `YP-B0GXX75JN5-88.00` 被沃尔玛拒过 —— 两条行的 sku 根本不相等,SQL 自己
    比不出来。比不出来的后果不是报错:那个品会以正例身份进样本,新链判拒它
    反而被算成误伤。
    """
    conn = _Conn(pos=[("XKJ-B0GXX75JN5-39.98",), ("AB-B0CLEAN0001-9.9",)],
                 products={"B0GXX75JN5": "t", "B0CLEAN0001": "t"},
                 rejected=["YP-B0GXX75JN5-88.00"])
    rejected = ar.rejected_asins(conn)
    assert rejected == {"B0GXX75JN5"}          # 走 sku_asin,不是裸等值
    got, st = ar._positives(conn, ar._parse_params({"pos": "10"}),
                            set(), rejected)
    assert got == ["B0CLEAN0001"] and st["ever_rejected"] == 1
    # 判据面不许封顶(抽样面才可以):漏一行就是把被拒的品当成好品去算误伤率
    assert "LIMIT" not in ar._REJECTED_SKU_SQL


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
    assert audit_rules.PRODUCT_ROW_COLUMNS in product_audit._candidate_sql(
        "x", "", None)


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
    # 类别准确率给两个分母:判拒的那些里 1/2,端到端 1/4
    # (只报端到端会把"没判拒"的窟窿算进类别账上,反之则把召回的窟窿藏起来)
    assert "判拒的带类别反例里 1/2 = 50.0%" in text
    assert "端到端(判拒且类别对 ÷ 全部带类别反例 4 条)1/4 = 25.0%" in text
    assert "新链 1/3 = 33.3%" in text and "旧链 2/3 = 66.7%" in text
    assert "底线达标" in text                      # 新 ≤ 旧
    assert "pending(判不了,不是判过了):1/7" in text
    assert "llm_bad_json" in text                  # pending 按来源分层
    assert "混淆表" in text and "Offensive Content" in text
    # 混淆表只画**判拒的**那些:没判拒的行画进去全是「→ (无类别)」,
    # 那说的是召回不是类别
    assert "→  得到 (无类别)" not in text
    assert "high" in text and "low" in text        # confidence 分层
    assert "预估成本 ≈ $0.12" in text


def test_report_把内部黑名单命中与判据召回分开报():
    """⚠ 2026-09-03 首测暴露的读数陷阱,这条钉住它别被"简化"掉。

    反例取自沃尔玛拒过的品,而**拒了就拉黑是既有流程** ⇒ 反例大量早就躺在
    `catalog` 黑名单三表里,L0 认出 ASIN 当场硬拒、类别自报 `内部黑名单`,
    判据一步都没走。总召回因此天然虚高(首测 78/114=68.4% 看着不错),
    而"政策判得对不对"一个字都没回答(拆开看**判据召回 0/36**)。

    所以召回和类别准确率都必须给第二个数:扣掉黑名单命中之后的那个。
    """
    memo = resources.AUDIT_CAT_INTERNAL_BLACKLIST
    rows = [
        # 三条反例被黑名单拦下(记忆,不是判据)
        _row(asin="B01", got_category=memo, stage_stopped_at="L0"),
        _row(asin="B02", got_category=memo, stage_stopped_at="L0"),
        _row(asin="B03", got_category=memo, stage_stopped_at="L0"),
        # 一条真的走到判据并判拒且类别对
        _row(asin="B04"),
        # 一条走到判据却放行(判据的窟窿)
        _row(asin="B05", got_verdict="pass", got_category=None),
    ]
    text = "\n".join(ar.report(rows, _META)[0])
    assert "▍反例召回(沃尔玛拒了,我们也拒):4/5" in text     # 总召回:含黑名单
    assert "**判据召回**" in text
    assert "1/2" in text                                       # 扣掉黑名单后
    assert "要看判据行不行,只能看这个数" in text
    # 类别准确率也要给扣掉之后的第二个数
    assert "真正由判据给出类别的" in text
    # 一条黑名单命中都没有时不硬凑这两行(别给读的人加噪声)
    clean = "\n".join(ar.report([_row(asin="B04"), _row(asin="B05",
                                 got_verdict="pass", got_category=None)],
                                _META)[0])
    assert "**判据召回**" not in clean
    assert "真正由判据给出类别的" not in clean


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
        conn.sql.append("JUDGE")        # 顺序留痕(提交必须在这之前)
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
    # 两次提交:抽样/打标读完一次(判定前撒手,见下面那条),落库后一次
    assert len(conn.written) == 4 and conn.commits == 2
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


def test_an_empty_sample_says_which_funnel_ate_everything(monkeypatch, wired):
    """一条样本都没抽到时,空报告只会让人以为"跑过了" —— 两条漏斗必须打出来
    (库是空的 / sku 提不出 asin / 产品行没采回来,下一步完全不同)。"""
    conn, calls = wired
    conn.neg, conn.pos = [], []
    out = ar.run({"execute": True})
    assert "一条样本都没抽到" in out and "反例漏斗" in out and "正例漏斗" in out
    assert calls == [] and conn.written == []


# ══════════════════════════════════════════════════════════════════════════════
#  ⑧ 复核修订(2026-09-02 对抗复核 ACCEPT-WITH-FIXES)
# ══════════════════════════════════════════════════════════════════════════════

def test_old_chain_baseline_excludes_rows_the_new_chain_wrote():
    """⚠ 命门:`audit_runs` 里没有版本痕迹时,`mode=stale` 一跑,每个 asin 的
    "最近一次 run"就是**新链自己刚写的那一行** —— 回放于是拿新链跟新链比,
    误伤率、一致率全部漂亮,而没有任何东西会红。

    所以基线 = 最近一次 `audit_version IS DISTINCT FROM <当前版本>` 的行
    (NULL = 存量老行,算旧链)。
    """
    assert "audit_version IS DISTINCT FROM %(current)s" in ar._OLD_SQL
    assert "IS DISTINCT FROM 'SHORTCUT'" in ar._OLD_SQL      # 影子行照旧排除
    assert "ORDER BY asin, created_at DESC" in ar._OLD_SQL

    seen = {}

    class _C(_Conn):
        def cursor(self, *a, **k):
            outer = self

            class _X(_Cur):
                def execute(self, sql, args=None):
                    if "FROM audit.audit_runs" in sql:
                        seen.update(args)
                        # 假库照谓词过滤:当前版本的那一行不该被选中
                        self._rows = [("B0A", "pass", None)]
                        return
                    super().execute(sql, args)
            return _X(outer)

    conn = _C()
    got = ar._old_runs(conn, ["B0A"])
    assert got == {"B0A": ("pass", None)}
    assert seen["current"] == resources.AUDIT_RULES_VERSION   # 谓词真的带了值
    # 落库侧把版本盖上了,基线才排得掉(services/audit_store 唯一出处)
    from services import audit_store
    assert "audit_version" in audit_store._RUN_SQL


def test_report_header_states_the_baseline_rule():
    """读数的人必须知道"旧链"是怎么取的 —— 否则 mode=stale 跑过之后的报告
    看起来和之前一模一样,而基线已经换成了新链自己。"""
    text = "\n".join(ar.report([_row()], _META)[0])
    assert "旧链基线" in text and "audit_version" in text
    assert resources.AUDIT_RULES_VERSION in text


def test_bottom_line_is_judged_on_the_shared_subset():
    """⚠ 两个分母不能比大小(400/40 场景):

    新链在**全部 400 条**正例上误伤 20(5%),旧链只在**其中 40 条**上有结论、
    误伤 5(12.5%)—— 首版会得出"新链更好、底线达标"。可在那共同的 40 条上
    新链误伤 12(30%),真相是**更差**。底线只判在同一批产品上。
    """
    rows = []
    for i in range(400):
        has_old = i < 40
        # 共同子集里新链误伤 12;子集外再误伤 8(全库合计 20)
        new_rej = (i < 12) or (100 <= i < 108)
        rows.append(_row(asin=f"B0P{i:05d}", source="pos", stratum="正例",
                         expected_verdict="pass", expected_category=None,
                         got_verdict="reject" if new_rej else "pass",
                         got_category="Alcohol" if new_rej else None,
                         old_verdict=("reject" if i < 5 else "pass")
                         if has_old else None,
                         confidence="medium", reason=""))
    text = "\n".join(ar.report(rows, _META)[0])
    assert "共同子集" in text and "底线判这里" in text
    assert "新链 12/40 = 30.0%" in text and "旧链 5/40 = 12.5%" in text
    assert "新链误伤高于旧链" in text and "底线达标" not in text
    # 全库水位另行单列(它不是对照)
    assert "全部正例上的新链误伤" in text and "20/400 = 5.0%" in text


def test_bottom_line_says_so_when_there_is_nothing_to_compare_with():
    """正例一条旧链结论都没有时,不许默认"达标"——那是没比过,不是比赢了。"""
    rows = [_row(asin="B0P1", source="pos", stratum="正例",
                 expected_verdict="pass", expected_category=None,
                 got_verdict="reject", got_category="Alcohol",
                 old_verdict=None, confidence="low", reason="")]
    text = "\n".join(ar.report(rows, _META)[0])
    assert "一条旧链结论都没有" in text and "底线达标" not in text


def test_content_family_names_are_guarded_against_the_policy_table():
    """内容族两页与 `AUDIT_IP_POLICY` 一样是**代码里写死的拼写**:政策表改了
    而常量没跟上,表现是"内容族那一类的类别准确率一夜归零",没有任何东西会红。
    所以走同一道装配期守门,对不上直接 RuntimeError。"""
    from services import audit_rules as ru

    names = {n for n, _ in ru.RULE_POLICIES}
    assert set(resources.AUDIT_CONTENT_POLICIES) <= names
    assert resources.AUDIT_IP_POLICY in names
    # 2026-09-03 C 批:L0 的 Made in USA 也自报一个写死的政策名,同一道闸
    assert resources.AUDIT_PRODUCT_CLAIMS_POLICY in names
    ok = frozenset({resources.AUDIT_IP_POLICY,
                    resources.AUDIT_PRODUCT_CLAIMS_POLICY,
                    *resources.AUDIT_CONTENT_POLICIES})
    ru.check_rule_policies(ok)                       # 对得上:不抛
    # 43 那一页在表里被改成了别的拼写 → 装配即炸,并点名该改哪个常量
    drifted = frozenset({resources.AUDIT_IP_POLICY,
                         resources.AUDIT_PRODUCT_CLAIMS_POLICY,
                         "Content standards overview (renamed)",
                         resources.AUDIT_CONTENT_POLICIES[1]})
    with pytest.raises(RuntimeError, match="AUDIT_CONTENT_POLICIES"):
        ru.check_rule_policies(drifted)
    # 空集合 = 没有表可对(离线/测试路径),不是"对不上"
    ru.check_rule_policies(frozenset())


def test_content_expected_category_uses_the_table_spelling():
    """期望类别取**表内原拼写**(judge 侧落库的就是表内拼写);表里没有内容族
    两页时不进集,不拿常量硬凑一个。"""
    res = _res("This item has content issues.")
    # 表里是另一种大小写写法 → 期望值跟表走
    got = ar.label(res, ["Content Standards: OVERVIEW", "Alcohol"])
    assert got == ("Content Standards: OVERVIEW", "内容族")
    assert ar.label(res, ["Alcohol"]) is None        # 表里没有 → 不进集
    # 互认按归一化键比,拼写差不算"类别判错"
    assert ar.category_ok("Content Standards: OVERVIEW",
                          "Product details policy")


def test_same_tag_replays_the_stored_asins_instead_of_resampling(wired):
    """⚠ 同 seed ≠ 同样本:候选面 `catalog.walmart_items` 每天被 catalog_sync
    重写,`md5(sku || seed) LIMIT` 的窗口跟着天天变 —— 隔天"同 seed 对比"
    已经不是同一批产品了,而两份报告长得一模一样。

    所以样本身份是 **run_tag**:该 tag 已有行就重放那一批。
    """
    conn, calls = wired
    conn.tag_rows = [("B0OLD00001", "reject", "Alcohol"),
                     ("B0OLD00002", "pass", None)]
    conn.products.update({"B0OLD00001": "t", "B0OLD00002": "t"})
    out = ar.run({"tag": "T7", "execute": True})
    assert sorted(calls) == ["B0OLD00001", "B0OLD00002"]     # 只判既有那批
    assert not any("md5" in s for s in conn.sql), "重放不许再抽样"
    one = [w for w in conn.written if w["asin"] == "B0OLD00001"][0]
    assert one["expected_verdict"] == "reject"               # 期望值原样取回
    assert one["expected_category"] == "Alcohol"
    assert "未重新抽样" in out and "重放既有样本" in out


def test_a_fresh_tag_still_samples(wired):
    """新 tag 才抽样(否则第一次跑什么都拿不到)。"""
    conn, calls = wired
    conn.tag_rows = []
    out = ar.run({"tag": "全新的tag", "execute": True})
    assert len(calls) == 4 and any("md5" in s for s in conn.sql)
    assert "之后同 tag 再跑会**重放这一批**" in out


def test_the_sampling_transaction_is_committed_before_judging_starts(wired):
    """⚠ 判定要跑几十分钟(几百条 × 几秒 LLM)。抽样那几条查询的事务若一直
    挂着,这条连接就是几十分钟的 idle in transaction:它按住一个老快照,
    vacuum 清不掉这期间的死行,而回放期间生产链正往同几张表写。"""
    conn, calls = wired
    ar.run({"execute": True})
    assert "COMMIT" in conn.sql and "JUDGE" in conn.sql
    assert conn.sql.index("COMMIT") < conn.sql.index("JUDGE")
    # 落库之后还要再提交一次(不然结果吊在事务里等 with 退出)
    assert conn.commits >= 2


def test_limit_per_category_zero_is_an_error_not_the_default():
    """`opts.cap or default_cap(...)` 会把 0 和"没传"当成同一件事 ——
    人以为这轮不封顶,实际按 neg/类别数 封了,而摘要长得一模一样。"""
    with pytest.raises(ValueError, match="limit_per_category"):
        ar._parse_params({"limit_per_category": "0"})
    with pytest.raises(ValueError, match="不能为负"):
        ar._parse_params({"limit_per_category": "-3"})
    assert ar._parse_params({}).cap is None                  # 没传 = 现算
    assert ar._parse_params({"limit_per_category": "7"}).cap == 7
