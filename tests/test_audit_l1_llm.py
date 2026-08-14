"""L1 第三级(候选 SQL + DeepSeek rerank)测试 —— 全离线,不打网不连库。

钉住三类东西:
  1. **提示词字节**:system 提示词 sha256 + user 模板全文(移植合同 L1-3:
     30/top15/[:20] 三处数字不一致是已知缺陷,照迁不改——测试就是那把锁)。
  2. **候选召回口径**:五路顺序/去重/短路/leaf 无 '>' 等于整条 path/
     `raw->>'amazon_leaf'` 写法(合同 L1-2)/切词两套口径故意不同。
  3. **失败语义**:LLM 失败、坏 JSON、unknown、字典外无救 → 一律 None(pending),
     绝不 db_only 兜底;F3 字典外回落是唯一保留的兜底且必须计数。
"""

import hashlib

import pytest

from services import audit_l1_llm as l1
from services.audit_models import ProductInfo

CAND_COLS = ["amazon_category", "walmart_product_type", "confidence"]


class FakeCursor:
    """按 SQL 片段派发预置结果的假游标(script: 片段 → (列名, 行))。"""

    def __init__(self, script=None):
        self.script = script or {}
        self.executed = []
        self._cols, self._rows = [], []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        for marker, (cols, rows) in self.script.items():
            if marker in sql:
                self._cols, self._rows = cols, rows
                return
        self._cols, self._rows = [], []

    @property
    def description(self):
        return [(c,) for c in self._cols]

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


def _cand(pt, conf="高", cat="A > B"):
    return (cat, pt, conf)


def _product(**kw):
    base = dict(asin="B0TEST", title="Widget", amazon_category_path="A > B",
                bullet_points=[], long_description="")
    base.update(kw)
    return ProductInfo(**base)


@pytest.fixture(autouse=True)
def _clean_stats():
    l1.reset_stats()


# ── 1. 提示词字节钉住(合同 L1-3:不得改写/概括/顺手修数字)────────────────

def test_system_prompt_bytes_pinned():
    # 旧仓 pipelines/l1_category.py:450-540 的字符串字面量,ast 抽取后哈希
    assert hashlib.sha256(l1.L1_SYSTEM_PROMPT.encode()).hexdigest() == (
        "d56b5d6e8b4621f23aa89132f9880fad9840f2215f8f32e9827fc26aaf1d5303")
    assert len(l1.L1_SYSTEM_PROMPT) == 3435


def test_system_prompt_keeps_known_number_mismatch():
    """已知缺陷照迁:正文 30 / user 段 top 15 / 实际截断 20,三处不一致。"""
    assert "若 30 个候选里没一个合理" in l1.L1_SYSTEM_PROMPT
    assert "# 候选 PT (top 15)" in l1.build_user_prompt(_product(), [])
    # 输出 schema 允许 pt_source='excluded'(下游无任何分支消费它)——照迁
    assert '"pt_source": "map_verified" | "llm_direct" | "excluded"' in \
        l1.L1_SYSTEM_PROMPT


def test_user_prompt_template_verbatim():
    p = _product(title="Widget", amazon_category_path="A > B",
                 bullet_points=["b1", "b2", "b3", "b4"], long_description="desc")
    cands = [{"walmart_product_type": "PT1", "confidence": "高"},
             {"walmart_product_type": "PT2", "confidence": None}]
    assert l1.build_user_prompt(p, cands) == (
        "# 产品\n"
        "标题: Widget\n"
        "Amazon 类目: A > B\n"
        "五点: b1; b2; b3\n"
        "描述: desc\n"
        "\n"
        "# 候选 PT (top 15)\n"
        "  1. PT1 conf=高\n"
        "  2. PT2\n"
    )


def test_user_prompt_empty_fields_and_truncation():
    p = _product(title="T", amazon_category_path="", bullet_points=["x" * 200],
                 long_description="d" * 900)
    txt = l1.build_user_prompt(p, [])
    assert "Amazon 类目: (空)" in txt
    assert "  (无候选, 自由判)" in txt
    assert "五点: " + "x" * 120 + "\n" in txt      # 每条 bullet 截 120
    assert "描述: " + "d" * 400 + "\n" in txt      # 描述截 400
    # 候选只喂前 20 条(提示词写 top 15 —— 已知缺陷照迁)
    many = [{"walmart_product_type": f"PT{i}", "confidence": ""} for i in range(30)]
    lines = l1.build_user_prompt(p, many).splitlines()
    assert lines[-1] == "  20. PT19"


# ── 2. 候选召回(五路 SQL 逐字)───────────────────────────────────────────

def _five_route_script(**over):
    script = {
        "m.amazon_category = %s": (CAND_COLS, [_cand("ExactPT")]),
        "%s LIKE m.amazon_category": (CAND_COLS, [_cand("PrefixPT", "中")]),
        "amazon_leaf": (CAND_COLS, [_cand("LeafPT", "低")]),
        "unnest(": (CAND_COLS + ["score"], [(None, "KwPT", "高", 2)]),
        "DISTINCT ON (m.walmart_product_type)": (
            ["walmart_product_type", "confidence"], [("LiteralPT", "高")]),
    }
    # 第七路(PT 字典直搜)默认返回空:多数用例只钉映射表那五路
    script = {"coalesce(walmart_ptg, '')": (
        ["walmart_product_type", "walmart_category", "walmart_ptg", "score"],
        []), **script}
    script.update(over)
    return script


def test_candidates_five_routes_order_and_shape():
    cur = FakeCursor(_five_route_script())
    out = l1.candidates(FakeConn(cur), _product(title="Garden Hose Nozzle Sprayer"))
    # 顺序 = exact/prefix/leaf(高优先)→ title_keyword → title_literal
    assert [c["walmart_product_type"] for c in out] == [
        "ExactPT", "PrefixPT", "LeafPT", "KwPT", "LiteralPT"]
    assert [c["match_type"] for c in out] == [
        "exact", "prefix", "leaf", "title_keyword", "title_literal"]
    assert out[-1]["amazon_category"] == ""      # 第五路补空 amazon_category


def test_candidates_dedup_first_wins():
    same = _five_route_script(**{
        "%s LIKE m.amazon_category": (CAND_COLS, [_cand("ExactPT", "低")]),
        "amazon_leaf": (CAND_COLS, [_cand("ExactPT", "低")]),
    })
    out = l1.candidates(FakeConn(FakeCursor(same)), _product(title="Garden Hose"))
    pts = [c["walmart_product_type"] for c in out]
    assert pts.count("ExactPT") == 1
    assert [c for c in out if c["walmart_product_type"] == "ExactPT"][0]["confidence"] == "高"


def test_candidates_sql_pins_schema_and_raw_leaf_and_dead_columns():
    cur = FakeCursor(_five_route_script())
    l1.candidates(FakeConn(cur), _product(title="Garden Hose Nozzle"))
    sqls = [s for s, _ in cur.executed]
    # 五路映射表 + 第七路 PT 字典直搜(产品无 browse_node_id ⇒ 第六路跳过)
    assert len(sqls) == 6
    joined = "\n".join(sqls)
    # 合同 L1-2:照抄 raw->>'amazon_leaf'(不改用 amazon_leaf 列)
    assert "m.raw->>'amazon_leaf' ILIKE %s" in joined
    # 新仓 schema 前缀 + 废弃 PT 闸(INNER JOIN pt_meta)每路都在
    assert joined.count("audit.walmart_category_map") == 5
    assert joined.count("INNER JOIN audit.walmart_pt_meta") == 5
    # spec §9 N8:三列死数据不再 SELECT
    for dead in ("requires_certificate", "zh_seller_forbidden", "requirements"):
        assert dead not in joined
    # 各路 LIMIT 逐字
    for lim in ("LIMIT 3", "LIMIT 5", "LIMIT 8", "LIMIT 10"):
        assert lim in joined


def test_candidates_leaf_equals_whole_path_without_gt():
    cur = FakeCursor(_five_route_script())
    l1.candidates(FakeConn(cur), _product(amazon_category_path="Automotive"))
    leaf_params = [p for s, p in cur.executed if "amazon_leaf" in s][0]
    assert leaf_params == ("Automotive",)        # 无 '>' → leaf = 整条 path


def test_candidates_skips_path_routes_when_no_path():
    cur = FakeCursor(_five_route_script())
    l1.candidates(FakeConn(cur), _product(amazon_category_path="",
                                          title="Garden Hose Nozzle"))
    sqls = [s for s, _ in cur.executed]
    assert not any("m.amazon_category = %s" in s for s in sqls)
    assert not any("amazon_leaf" in s for s in sqls)
    assert any("unnest(" in s and "walmart_category_map" in s
               for s in sqls)                   # title 路仍走


def test_candidates_short_circuits_when_limit_reached():
    """四路的 `len(candidates) < limit` 短路条件照抄:exact 塞满就不再查后三路。"""
    full = _five_route_script(**{
        "m.amazon_category = %s": (CAND_COLS, [_cand(f"PT{i}") for i in range(15)]),
    })
    cur = FakeCursor(full)
    out = l1.candidates(FakeConn(cur), _product(title="Garden Hose Nozzle"))
    sqls = [s for s, _ in cur.executed]
    assert not any("%s LIKE m.amazon_category" in s for s in sqls)
    # 短路只管映射表那四路;第七路直搜 PT 字典与 limit 无关(它也用 unnest)
    assert not any("unnest(" in s and "walmart_category_map" in s for s in sqls)
    # 第五路 title_literal 独立于 limit,照常查
    assert any("DISTINCT ON (m.walmart_product_type)" in s for s in sqls)
    assert len(out) == 16      # 15 条 kw(截到 limit)+ 1 条 literal


def test_title_keyword_two_tokenizers_differ():
    # 第 4 路:删所有非字母数字(连词内连字符),≥4 字符,最多 6 个
    assert l1._title_keywords("Anti-Slip pack of 4 Garden Hose Nozzle Sprayer Brass Kit") == [
        "AntiSlip", "Garden", "Hose", "Nozzle", "Sprayer", "Brass"]
    # 已知冗余照迁:'set'/'the' 等 <4 的停用词本就被门槛滤掉,'pack of' 永不命中
    assert "pack of" in l1._STOP and "pack" in l1._STOP
    # 第 5 路:strip 标点(保留词内连字符)、长度 4-30、先取前 8 再过停用词
    assert l1._literal_keywords("Anti-Slip these Garden, Hose #Nozzle Brass Kit Extra Tail") == [
        "anti-slip", "garden", "hose", "nozzle", "brass", "extra", "tail"]
    assert "these" in l1._STOP_LITERAL and "these" not in l1._STOP


# ── 3. rerank 失败语义(全部 → None,调用方转 pending)──────────────────────

def _chat(payload):
    calls = []

    def fn(messages):
        calls.append(messages)
        if isinstance(payload, Exception):
            raise payload
        return payload

    fn.calls = calls
    return fn


PT_DICT = {"GoodPT", "OtherPT", "Books"}


def test_no_candidate_returns_none_without_calling_llm():
    """合同 L1-5:无候选直接 pending,不调 LLM。"""
    fn = _chat({"walmart_product_type": "GoodPT"})
    assert l1.rerank(_product(), [], PT_DICT, chat_fn=fn) is None
    assert fn.calls == []
    assert l1.STATS["no_candidate"] == 1 and l1.STATS["llm_called"] == 0


def test_llm_exception_returns_none():
    fn = _chat(RuntimeError("LLM 调用连续 3 次失败"))
    cands = [{"walmart_product_type": "GoodPT", "confidence": "高"}]
    assert l1.rerank(_product(), cands, PT_DICT, chat_fn=fn) is None
    assert l1.STATS["llm_failed"] == 1


@pytest.mark.parametrize("bad", [{}, None, "not a dict", []])
def test_bad_json_returns_none(bad):
    fn = _chat(bad)
    cands = [{"walmart_product_type": "GoodPT", "confidence": "高"}]
    assert l1.rerank(_product(), cands, PT_DICT, chat_fn=fn) is None
    assert l1.STATS["bad_json"] == 1


def test_llm_unknown_returns_none():
    fn = _chat({"walmart_product_type": "unknown", "pt_confidence": "低",
                "pt_source": "llm_direct"})
    cands = [{"walmart_product_type": "GoodPT", "confidence": "高"}]
    assert l1.rerank(_product(), cands, PT_DICT, chat_fn=fn) is None
    assert l1.STATS["unknown"] == 1


def test_dict_miss_without_usable_candidate_returns_none():
    """F4:字典外 PT 且候选全不可用 → None(旧仓在这里产 unknown 进 L2 假 pass)。"""
    fn = _chat({"walmart_product_type": "MadeUpPT"})
    cands = [{"walmart_product_type": "DeadPT", "confidence": "低"}]
    assert l1.rerank(_product(), cands, PT_DICT, chat_fn=fn) is None
    assert l1.STATS["unknown"] == 1 and l1.STATS["dict_fallback"] == 0


def test_no_db_only_fallback_and_no_cache():
    """旧仓 _classify_db_only(取候选首条当权威 PT)不迁;合同 L1-7:L1 不吃缓存。"""
    import ast
    tree = ast.parse(open(l1.__file__, encoding="utf-8").read())
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert not any("db_only" in f for f in funcs)
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
             for a in n.names}
    assert not any("llm_cache" in m for m in mods)
    # 无脑取候选首条的写法一次都不许出现(F8 事故面)
    assert "cands[0]" not in open(l1.__file__, encoding="utf-8").read()


# ── 4. rerank 正常路径与归一 ───────────────────────────────────────────────

def test_rerank_happy_path_and_request_params(monkeypatch):
    fn = _chat({"walmart_product_type": "GoodPT", "pt_confidence": "高",
                "pt_source": "map_verified", "excluded_category_reason": None,
                "reasoning": "候选契合"})
    cands = [{"walmart_product_type": "GoodPT", "confidence": "高"}]
    out = l1.rerank(_product(), cands, PT_DICT, chat_fn=fn)
    assert out.walmart_product_type == "GoodPT"
    assert (out.pt_confidence, out.pt_source) == ("高", "map_verified")
    assert out.excluded_category_reason is None and out.hits == []
    msgs = fn.calls[0]
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == l1.L1_SYSTEM_PROMPT
    assert msgs[1]["content"] == l1.build_user_prompt(_product(), cands)
    # 请求参数(spec §5):temperature 0.1(不是 api/llm 的 0.2)/ 2048 / audit_l1
    assert (l1._TEMPERATURE, l1._MAX_TOKENS, l1._PURPOSE) == (0.1, 2048, "audit_l1")


def test_default_chat_fn_uses_purpose_and_params(monkeypatch):
    seen = {}

    def fake_chat_json(messages, *, temperature, max_tokens, purpose):
        seen.update(temperature=temperature, max_tokens=max_tokens, purpose=purpose)
        return {"walmart_product_type": "GoodPT"}

    from api import llm as llm_api
    monkeypatch.setattr(llm_api, "chat_json", fake_chat_json)
    cands = [{"walmart_product_type": "GoodPT", "confidence": "高"}]
    out = l1.rerank(_product(), cands, PT_DICT)
    assert out.walmart_product_type == "GoodPT"
    assert seen == {"temperature": 0.1, "max_tokens": 2048, "purpose": "audit_l1"}


def test_confidence_and_source_normalized():
    fn = _chat({"walmart_product_type": "GoodPT", "pt_confidence": "very high",
                "pt_source": "magic"})
    cands = [{"walmart_product_type": "GoodPT", "confidence": "高"}]
    out = l1.rerank(_product(), cands, PT_DICT, chat_fn=fn)
    assert (out.pt_confidence, out.pt_source) == ("低", "llm_direct")


def test_zero_confidence_threshold_low_conf_still_adopted():
    """合同 L1-4:零阈值照迁——'低' 置信也直接采纳,只计数不改判。"""
    fn = _chat({"walmart_product_type": "GoodPT", "pt_confidence": "低"})
    cands = [{"walmart_product_type": "GoodPT", "confidence": "低"}]
    out = l1.rerank(_product(), cands, PT_DICT, chat_fn=fn)
    assert out.walmart_product_type == "GoodPT"
    assert l1.STATS["conf_low"] == 1


def test_dict_fallback_f3_takes_first_usable_candidate():
    fn = _chat({"walmart_product_type": "Office Chairs"})      # 废弃 PT
    cands = [{"walmart_product_type": "DeadPT", "confidence": "高"},
             {"walmart_product_type": "OtherPT", "confidence": "中"}]
    out = l1.rerank(_product(), cands, PT_DICT, chat_fn=fn)
    assert out.walmart_product_type == "OtherPT"          # 跳过不可用的首条
    assert (out.pt_source, out.pt_confidence) == ("map_fallback", "中")
    hit = out.hits[0]
    assert (hit.stage, hit.rule_code, hit.penalty) == ("L1", "pt_dict_fallback", 0)
    assert hit.detail == {
        "llm_output_pt": "Office Chairs", "fallback_pt": "OtherPT",
        "reason": "LLM 输出 PT 不在 Walmart 字典, 回落到候选首条",
        "source": "map_fallback"}
    assert l1.STATS["dict_fallback"] == 1                 # 兜底必须计数


# ── 5. excluded / seed / 出版物硬禁 ───────────────────────────────────────

def test_llm_excluded_produces_minus_100_hit():
    fn = _chat({"walmart_product_type": "GoodPT", "excluded_category_reason": "3C禁售",
                "reasoning": "是耳机"})
    cands = [{"walmart_product_type": "GoodPT", "confidence": "高"}]
    out = l1.rerank(_product(), cands, PT_DICT, chat_fn=fn)
    assert out.excluded_category_reason == "3C禁售"
    hit = out.hits[0]
    assert (hit.rule_code, hit.penalty) == ("excluded_category", -100)
    assert hit.detail["from_seed_yaml"] is False
    assert hit.detail["llm_reasoning"] == "是耳机"


@pytest.mark.parametrize("null_like", [None, "null", "None", "", "none"])
def test_excluded_null_like_values_normalized(null_like):
    fn = _chat({"walmart_product_type": "GoodPT",
                "excluded_category_reason": null_like})
    cands = [{"walmart_product_type": "GoodPT", "confidence": "高"}]
    out = l1.rerank(_product(), cands, PT_DICT, chat_fn=fn)
    assert out.excluded_category_reason is None and out.hits == []


def test_seed_yaml_only_fills_when_llm_silent():
    """seed 仅补位,LLM 的 excluded 优先(逐字 :967-969)。"""
    p = _product(amazon_category_path="Consumer Electronics > Headphones")
    assert l1.check_seed_excluded(p) == "3C 禁售"
    fn = _chat({"walmart_product_type": "GoodPT"})
    cands = [{"walmart_product_type": "GoodPT", "confidence": "高"}]
    out = l1.rerank(p, cands, PT_DICT, chat_fn=fn)
    assert out.excluded_category_reason == "3C 禁售"
    assert out.hits[0].detail["from_seed_yaml"] is True
    assert l1.STATS["seed_excluded"] == 1


def test_seed_walmart_pt_scope_never_sees_llm_output():
    """已知缺陷照迁(spec §3.1,合同 L1-1):seed 在 LLM 之前跑且不带 PT,
    所以 LLM 输出 'Headphones' 这类 walmart_pt scope 的 PT 时 seed 不拦。"""
    p = _product(amazon_category_path="Home > Audio", title="Earbuds")
    assert l1.check_seed_excluded(p) is None
    assert l1.check_seed_excluded(p, "Headphones") == "3C 禁售"   # 带 PT 才会拦
    fn = _chat({"walmart_product_type": "Headphones"})
    cands = [{"walmart_product_type": "Headphones", "confidence": "高"}]
    out = l1.rerank(p, cands, {"Headphones"}, chat_fn=fn)
    assert out.walmart_product_type == "Headphones"
    assert out.excluded_category_reason is None                   # 不拦 —— 照迁


def test_seed_first_hit_wins_and_scope_default():
    p = _product(amazon_category_path="Clothing, Shoes & Jewelry > Men")
    assert l1.check_seed_excluded(p) == "服饰禁售"     # Clothing 在 Shoes 之前


@pytest.mark.parametrize("pt", sorted(l1.PUBLICATION_HARD_FORBID))
def test_publication_ban_pure_function(pt):
    hit = l1.check_publication_ban(pt)
    assert (hit.stage, hit.rule_code, hit.penalty) == (
        "L1", "publication_pt_forbidden", -100)
    assert hit.detail["reason"] == "出版物类 PT 搬运无法合规 (数据验证 E 知产占比 ≥96%)"
    assert hit.detail["walmart_pt"] == pt
    # 已有 excluded 理由时不叠加(旧仓条件逐字)→ 接线三级重复调用幂等
    assert l1.check_publication_ban(pt, "3C禁售") is None
    assert l1.check_publication_ban("Planners") is None   # 误伤高的两类不含


def test_publication_ban_applies_to_rerank_output():
    fn = _chat({"walmart_product_type": "Books", "pt_confidence": "高"})
    cands = [{"walmart_product_type": "Books", "confidence": "高"}]
    out = l1.rerank(_product(), cands, PT_DICT, chat_fn=fn)
    assert out.excluded_category_reason.startswith("出版物类 PT 搬运无法合规")
    assert [h.rule_code for h in out.hits] == ["publication_pt_forbidden"]
    assert l1.STATS["publication_forbidden"] == 1


# (原第①级第二数据源 error_confirmed_map 已随所有者定稿移除:历史实证 PT
#  经 pt_backfill 直接回填 products.walmart_pt,resolve_pt ①b 读产品行——
#  测试见 test_audit_rules_wiring.test_resolve_pt_known_pt_second_source)


# ── 第六/七路 + 两阶段开放判定(所有者定稿 2026-08-14)───────────────────────

def _seven_route_script(**over):
    # 派发按 dict 顺序取首个匹配片段:第六路 SQL 里也有 "DISTINCT ON
    # (m.walmart_product_type)",必须排在第五路那条键之前才不会被它截走
    script = {"FROM audit.amazon_node_paths": (
        ["amazon_category", "walmart_product_type", "confidence", "dist"],
        [("A > B", "AncPT2", "高", 2), ("A", "AncPT1", "中", 1)])}
    script.update(_five_route_script())
    script["coalesce(walmart_ptg, '')"] = (
        ["walmart_product_type", "walmart_category", "walmart_ptg", "score"],
        [("DictPT", "Home", "Garden", 3)])
    script.update(over)
    return script


def test_ancestor_and_pt_dict_routes_append_after_map_routes():
    """六/七路只补候选池,顺序排在映射表五路之后(映射证据强于祖先/字典)。"""
    cur = FakeCursor(_seven_route_script())
    out = l1.candidates(FakeConn(cur),
                        _product(title="Garden Hose Nozzle", browse_node_id="9"))
    assert [c["match_type"] for c in out] == [
        "exact", "prefix", "leaf", "title_keyword", "title_literal",
        "ancestor_1", "ancestor_2", "pt_dict"]      # 祖先按层距近的在前


def test_ancestor_route_skipped_without_node_id():
    cur = FakeCursor(_seven_route_script())
    l1.candidates(FakeConn(cur), _product(title="Garden Hose", browse_node_id=""))
    assert not any("amazon_node_paths" in s for s, _ in cur.executed)


def test_pt_dict_words_take_leaf_then_title():
    """第七路的词:先 Amazon 叶子类目名,再补标题关键词(去重、≤8)。"""
    p = _product(title="Golf Cart Seat Cover Beige",
                 amazon_category_path="Sports > Golf > Golf Cart Accessories")
    assert l1._dict_words(p)[:3] == ["Golf", "Cart", "Accessories"]
    assert "Seat" in l1._dict_words(p)


def test_open_candidates_two_stage():
    """七路全空时的兜底:先让 LLM 选大类,再把该大类全部 PT 当候选。"""
    cur = FakeCursor({
        "SELECT DISTINCT walmart_category": (["walmart_category"],
                                             [("Home",), ("Garden",)]),
        "walmart_category = ANY": (
            ["walmart_product_type", "walmart_category", "walmart_ptg"],
            [("PTa", "Home", "G1"), ("PTb", "Home", "G2")]),
    })
    out = l1.open_candidates(FakeConn(cur), _product(title="w"),
                             chat_fn=lambda m: {"categories": ["Home"]})
    assert [c["walmart_product_type"] for c in out] == ["PTa", "PTb"]
    assert all(c["match_type"] == "open_mega" for c in out)
    # 一阶段选了清单外的大类 → 不认,返回空(绝不放大到全字典)
    assert l1.open_candidates(FakeConn(cur), _product(title="w"),
                              chat_fn=lambda m: {"categories": ["瞎编"]}) == []
    # 一阶段调用失败 → 空(调用方照旧 pending)
    def _boom(_m):
        raise RuntimeError("down")
    assert l1.open_candidates(FakeConn(cur), _product(title="w"),
                              chat_fn=_boom) == []


def test_picked_route_attribution():
    """归因计数:LLM 选中的 PT 是哪一路召回的——没有它就说不清六七两路
    到底出没出力(no_candidate 归零可能是它们的功劳,也可能本来就是 0)。"""
    l1.reset_stats()
    cands = [{"walmart_product_type": "AncPT", "confidence": "高",
              "match_type": "ancestor_2"}]
    got = l1.rerank(_product(title="w"), cands, {"AncPT"},
                    chat_fn=lambda m: {"walmart_product_type": "AncPT",
                                       "pt_confidence": "高"})
    assert got.walmart_product_type == "AncPT"
    assert l1.STATS["picked_ancestor_2"] == 1
    # LLM 挑了个不在候选里但在字典内的 PT → 单独计一类,不冒充某一路的功劳
    l1.reset_stats()
    l1.rerank(_product(title="w"), cands, {"AncPT", "OtherPT"},
              chat_fn=lambda m: {"walmart_product_type": "OtherPT"})
    assert l1.STATS["picked_off_candidates"] == 1


def test_open_stage1_puts_mega_list_in_system_for_cache():
    """大类清单必须在 system 段:它每次调用完全一样,放 user 段(产品信息之后)
    一个字节都进不了 DeepSeek 前缀缓存 —— 生产实测可缓存占比只剩 ~9%。"""
    seen = {}

    def _spy(messages):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        return {"categories": []}

    cur = FakeCursor({"SELECT DISTINCT walmart_category": (
        ["walmart_category"], [("Home",), ("Garden",)])})
    l1.open_candidates(FakeConn(cur), _product(title="w"), chat_fn=_spy)
    assert "- Home" in seen["system"] and "- Garden" in seen["system"]
    assert "Home" not in seen["user"].replace("Home & Kitchen", "")
    # system 必须比 user 长:可缓存的那半边才该是大头
    assert len(seen["system"]) > len(seen["user"]) * 0.5
