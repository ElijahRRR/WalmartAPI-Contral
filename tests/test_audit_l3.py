"""L3 语义审核测试(services/audit_l3.py;2026-09-02 B1:换喂官方全文 + 三段化)。

离线:不打网,chat_json 一律 monkeypatch;DB 走 FakeConn 假游标;S4 的断言打在
**真实转录件**(`refdata/policy_pages/en/*.md`)上,不编造夹具 —— 提示词是判定面,
拿假数据钉不出"喂进去的是不是官方原文"。

重点钉住四类东西:
  1. **喂的是什么** —— S4 = 44 篇官方英文全文、按 id 序、无 URL、同轮逐字节稳定;
     S2 = 库序类别 + 两条非政策类别 + none(没有 brand_misuse);
  2. **user 段的形状** —— 上游三层证据、本 PT 准入要求、描述 3000 字符;
     **没有**路由提示行、**没有**恒空的原产国行;
  3. **解析零猜测** —— 政策名对不上枚举 → pending(不降级猜 IP);pass 强制 none;
     品牌翻拒严格 `is True`;
  4. **失败四路 → pending 且不写 llm_cache**。
"""

import pytest

from registry import paths, resources
from services import audit_l3
from services.audit_models import (L1Info, L2Result, Phase0Result, ProductInfo,
                                   RuleHit)
from workflows import policy_sync as _ps

# ---------------------------------------------------------------- 假件

POLICY_ROWS = [
    (1, "Alcohol", "Prohibited:\n- Distillation apparatus\n- Wine kits"),
    (2, "Animals", "Prohibited:\n- Live animals"),
    (3, "Intellectual Property", "Prohibited:\n- Counterfeit goods"),
    (4, "Offensive Content", "Prohibited:\n- Hate symbols"),
]
POLICY_COLS = ["id", "category_en", "full_policy"]
# 库序(ORDER BY category_en 由 PG 给出),故意不是 Python sorted 序
CATEGORY_ROWS = [("Alcohol",), ("Animals",), ("Pet Products",),
                 ("PFAS Chemicals",), ("Intellectual Property",),
                 ("Offensive Content",), ("Children's Products",)]
KNOWN = frozenset(c for (c,) in CATEGORY_ROWS)

# 真实转录件(44 篇官方英文)——S4 接线的断言打在它们上面
_EN_FILES = sorted(paths.policy_pages_dir("en").glob("*.md"))
_OFFICIAL = [_ps.parse_policy_file(f) for f in _EN_FILES]
_OFFICIAL_NAMES = [r["category_en"] for r in _OFFICIAL]


def _official_rows() -> list[dict]:
    """输入:无 → 输出:与 `load_policy_rows` 同形的 44 行(id 按文件序)。"""
    return [{"id": i + 1, "category_en": r["category_en"],
             "full_policy": r["full_policy"]} for i, r in enumerate(_OFFICIAL)]


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.sql_log.append(sql)
        if "ORDER BY id" in sql:
            assert "full_policy IS NOT NULL" in sql   # 空壳行不进 S4
            self.description = [(c,) for c in POLICY_COLS]
            self._rows = list(POLICY_ROWS)
        elif "ORDER BY category_en" in sql:
            self.description = [("category_en",)]
            self._rows = list(CATEGORY_ROWS)
        elif sql.startswith("UPDATE catalog.llm_cache"):
            self._one = (self.conn.cache.get(params[0]),) if params[0] in self.conn.cache else None
        elif sql.startswith("INSERT INTO catalog.llm_cache"):
            self.conn.put_log.append(params[0])
        else:                       # 未预期 SQL:测试要炸,不要静默
            raise AssertionError(f"未预期 SQL: {sql}")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


class FakeConn:
    def __init__(self, cache=None):
        self.sql_log = []
        self.put_log = []
        self.cache = cache or {}

    def cursor(self):
        return FakeCursor(self)


def _ctx(**kw):
    from types import SimpleNamespace
    base = dict(known_policies=KNOWN, pt_meta={})
    base.update(kw)
    return SimpleNamespace(**base)


def _product(**kw):
    base = dict(asin="B0TEST0001", title="Nike Air Shoes", brand="Generic",
                bullet_points=["b1", "b2"], long_description="desc",
                amazon_category_path="Toys > Kids")
    base.update(kw)
    return ProductInfo(**base)


def _l1(**kw):
    base = dict(walmart_product_type="Sneakers", pt_confidence="高",
                pt_source="map_direct", walmart_category="Toys")
    base.update(kw)
    return L1Info(**base)


def _l2(hits=None):
    return L2Result(score_final=100, hits=hits or [])


@pytest.fixture(autouse=True)
def _reset_prompt_cache():
    audit_l3.reset_prompt_cache()
    audit_l3.reset_stats()
    yield
    audit_l3.reset_prompt_cache()
    audit_l3.reset_stats()


# ---------------------------------------------------------------- S4:官方全文


def test_S4_喂的是44篇官方英文全文而不是中文人工列():
    """⚠ B1 的核心换喂:S4 = 官方原文,不是六个中文人工列的二手转述。

    44 篇一篇不少(42 类禁售 + 内容族两页),每篇一个 `## 类别名` 标题;
    判据句子要真的在里面(抽三条互不相干的验)。
    """
    rows = _official_rows()
    parts = audit_l3.policy_parts(rows)
    block = audit_l3.format_policy_block(rows)
    assert len(_OFFICIAL_NAMES) == 44
    for name in _OFFICIAL_NAMES:
        assert f"## {name}\n" in block, name
    assert len(parts) == 44                       # 篇数按渲染出的段数,不数 "## "
    # ⚠ 官方正文自己带 `## Overview` 之类小标题:数 "\n## " 会得到 251 而不是
    #   44 —— 提示词自称的篇数会瞬间变成一个假数(这条守门就为这个)
    assert block.count("\n## ") > 200
    assert "Distiller/distillation equipment and kits" in block   # 01 Alcohol
    assert "CPC" in block                                         # 08 儿童产品
    assert "## Content standards: Overview" in block              # 43 内容族
    assert "## Product details policy" in block                   # 44 内容族


def test_S4_两条SQL的形状是契约():
    """`ORDER BY id` / `ORDER BY category_en` 都不可省 —— 顺序即前缀缓存命中率;
    `full_policy IS NOT NULL` 是"空壳不进 S4"的第一道闸(第二道在渲染层)。"""
    assert audit_l3.POLICY_ROWS_SQL == (
        "SELECT id, category_en, full_policy "
        "FROM audit.walmart_prohibited_policy "
        "WHERE full_policy IS NOT NULL ORDER BY id")
    assert audit_l3.REASON_CATEGORIES_SQL.endswith("ORDER BY category_en")
    # 中文人工列一列都不读了(B1 换喂:判据以官方英文原文为准)
    for col in ("category_zh", "overall_status", "prohibited_items",
                "zh_seller_risk", "zh_seller_notes"):
        assert col not in audit_l3.POLICY_ROWS_SQL, col


def test_S4_剥掉URL并按id序拼接():
    """喂入版由 policy_feed 渲染:URL 一条不留(对判定零贡献、徒耗 token),
    顺序 = 入参顺序(SQL 是 ORDER BY id)—— 顺序即前缀缓存命中率。"""
    rows = _official_rows()
    parts = audit_l3.policy_parts(rows)
    block = audit_l3.format_policy_block(rows)
    assert "https://" not in block and "http://" not in block
    # 每段的**首行**就是这一篇的类别名(篇内小标题不算)
    assert [p.split("\n", 1)[0] for p in parts] == \
        [f"## {r['category_en']}" for r in rows]
    # 打乱入参 ⇒ 输出跟着乱(证明这里不自作主张排序,顺序由 SQL 保证)
    shuffled = list(reversed(rows))
    assert [p.split("\n", 1)[0] for p in audit_l3.policy_parts(shuffled)] == \
        [f"## {r['category_en']}" for r in shuffled]


def test_S4_同一轮内逐字节稳定():
    """前缀缓存的硬前提:同一批政策行渲染两次必须一模一样(含空行位置)。"""
    rows = _official_rows()
    assert audit_l3.format_policy_block(rows) == audit_l3.format_policy_block(rows)
    cats = [c for (c,) in CATEGORY_ROWS]
    assert audit_l3.build_system_prompt(cats, rows) == \
        audit_l3.build_system_prompt(cats, rows)


def test_S4_没有全文的行整条跳过并计数(caplog):
    """空壳标题给 LLM 等于没给,却会让它以为"这一类我看过了"。

    ⚠ 兜底三要件:跳过 = 判据变窄,**必须记日志计数**(同名一轮只警告一次,
    计数逐次累加,进 run 摘要)。
    """
    rows = [{"id": 1, "category_en": "Alcohol", "full_policy": "Prohibited: x"},
            {"id": 2, "category_en": "Animals", "full_policy": None},
            {"id": 3, "category_en": "Art", "full_policy": "   \n\n"}]
    with caplog.at_level("WARNING", logger="services.audit_l3"):
        block = audit_l3.format_policy_block(rows)
        audit_l3.format_policy_block(rows)          # 再来一轮:计数累加
    assert "## Alcohol" in block
    assert "Animals" not in block and "Art" not in block
    assert audit_l3.STATS["policy_no_full_text"] == 4
    assert audit_l3.policies_without_full_text() == ["Animals", "Art"]
    assert len([r for r in caplog.records if "Animals" in r.getMessage()]) == 1


# ---------------------------------------------------------------- S1/S2/S3


def test_S2_库序_两条非政策类别_none_且没有brand_misuse():
    """枚举 = 政策表库序 + `内部黑名单` / `类目准入` + `none`。

    ⚠ `brand_misuse` 删了(B1):它只在提示词里存在、政策表里没有,品牌误用
    归 `Intellectual Property`(解析层的翻拒规则落地)。多一个假类别 =
    多一处对不上的口径。
    """
    block = audit_l3.format_reason_categories([c for (c,) in CATEGORY_ROWS])
    lines = block.split("\n")
    assert lines == [f"  - {c}" for (c,) in CATEGORY_ROWS] + \
        ["  - 内部黑名单", "  - 类目准入", "  - none"]
    assert lines != sorted(lines)                  # 库序不被 Python sorted 覆盖
    assert "brand_misuse" not in block
    assert list(resources.AUDIT_NONPOLICY_CATEGORIES) == ["内部黑名单", "类目准入"]


def test_篇数占位符按实时渲染篇数填而不是政策表行数():
    """提示词自称的篇数 = **S4 真正渲染出来的**篇数。

    写死或按行数算都会撒谎:没有全文的行不进 S4,说 44 篇却只给 42 篇,
    LLM 会拿那个数当"我应该看到多少篇"的锚。
    """
    assert audit_l3._COUNT_SLOT == "{N}"
    cats = [c for (c,) in CATEGORY_ROWS]
    rows = [dict(zip(POLICY_COLS, r)) for r in POLICY_ROWS]
    p = audit_l3.build_system_prompt(cats, rows)
    assert "{N}" not in p
    assert p.count(f"{len(rows)} 篇沃尔玛政策全文"
                   "(Prohibited Products Policy 各类别 + 内容标准两页)") == 2
    # 少一篇全文 ⇒ 两处数字一起跟着走
    rows2 = rows + [{"id": 9, "category_en": "Art", "full_policy": None}]
    p2 = audit_l3.build_system_prompt(cats, rows2)
    assert f"{len(rows)} 篇沃尔玛政策全文" in p2       # 仍是 4,不是 5
    assert "  - Art" not in p2                        # S2 也没多一条(它读的是 cats)


def test_S1_把B1定稿的要点都写进去了():
    """⚠ 提示词是判定面:少一句话不会报错,只会让判定悄悄漂。

    这条钉的是 §3.1 逐条定稿的**指令要点**,不是措辞:判据只认末尾原文 /
    policy 逐字抄 / detail 引原文片段且 ≤120 字 / 提到 ≠ 卖的就是 /
    准入要求先判"这个产品要不要" / 严格 JSON。
    """
    s1 = audit_l3._S1
    for must in ("训练记忆里的沃尔玛政策**一律作废**",
                 "**逐字抄**下面「候选类别」清单里的一项",
                 "≤120 字",
                 "**引用触发判定的原文片段**",
                 "**提到 ≠ 卖的就是它**",
                 "先判**这个具体产品**要不要这张证",
                 "`类目准入`",
                 "只输出严格 JSON"):
        assert must in s1, must
    # 输出示例里是新五键,旧键一个都不许留
    for key in ('"verdict"', '"policy"', '"detail"', '"brand_verdicts"',
                '"confidence"'):
        assert key in s1, key
    for gone in ("reason_category", "reason_text", "blacklist_brand_verdict",
                 "llm_confidence", "signals", "brand_misuse"):
        assert gone not in s1, gone


def test_system_prompt_四段顺序与进程内只查一次库():
    conn = FakeConn()
    p = audit_l3.system_prompt(conn)
    i1 = p.index("你是沃尔玛 Marketplace 合规审核 AI")
    i2 = p.index("  - Alcohol\n")
    i3 = p.index("\n# 4 篇沃尔玛政策全文")          # S3 分隔段的标题行
    i4 = p.index("## Alcohol\n\nProhibited:")
    assert i1 < i2 < i3 < i4

    # 进程级缓存:第二次调用一条 SQL 都不再发(两条 = 类别 + 政策各一次)
    assert audit_l3.system_prompt(conn) is p
    assert len([s for s in conn.sql_log if "walmart_prohibited_policy" in s]) == 2


def test_路由提示整体删除():
    """§3.7:两张手工「类目 → 政策」映射表连同 hint 行一起退役 ——
    换全文后 LLM 面前有全部政策,提示只会把注意力锁在 ≤5 篇上。
    这条守门防的是"有人照着旧文档把它写回来"。"""
    for gone in ("route_policy_hints", "_CATEGORY_ROUTES", "_PT_KEYWORD_ROUTES",
                 "_ALWAYS_INCLUDE", "ROUTE_MAX_POLICIES",
                 "unresolved_route_names", "summarize_l2_for_l3",
                 "format_full_policy_block", "valid_reason_categories"):
        assert not hasattr(audit_l3, gone), gone
    assert "route_unresolved" not in audit_l3.STATS


# ---------------------------------------------------------------- 上游证据通道


def _r4_hit(n=12, code="title_desc_blacklist"):
    return RuleHit(stage="L2", rule_code=code, penalty=0,
                   detail={"matches": [{"brand": f"nike{i}",
                                        "matched_phrase": f"Nike{i}"}
                                       for i in range(n)], "count": n})


def test_证据通道_跨三层且按rule_code渲染():
    """⚠ B1 的通道泛化:读 L0/L1/L2 三层的软 hit,按 rule_code 查渲染表 ——
    与它出自哪一层无关(C 批把品牌扫描迁进 L0 时不用再改这里)。"""
    p0 = Phase0Result(blocked=False, hits=[
        _r4_hit(2, code="phase0_brand_mention")])
    l1 = L1Info(hits=[RuleHit(stage="L1", rule_code="unmapped_amazon_path",
                              penalty=0, detail={"reason": "映射表曾标注"})])
    l2 = _l2([RuleHit(stage="L2", rule_code="walmart_strict_sensitive",
                      penalty=0,
                      detail={"subtypes": ["adult"], "matched_phrases": ["sexy"]})])
    txt, brands = audit_l3.summarize_evidence(p0, l1, l2)
    lines = txt.split("\n")
    assert lines[0].startswith("* 文案提到黑名单品牌(共2个, 前10): nike0")
    assert lines[1] == "* unmapped_amazon_path: reason=映射表曾标注"   # 未登记也不丢
    assert lines[2] == "* 敏感/严格合规(R8, adult): sexy"
    assert brands == ["nike0", "nike1"]
    # 三层都空 ⇒ 明说没有证据(别让 LLM 以为这一段丢了)
    assert audit_l3.summarize_evidence() == ("(上游无证据)", [])


def test_证据通道_只收软hit_硬拒不进():
    """扣了分的是硬拒 —— 那种产品根本进不了 L3,出现在证据里就是接线错了。"""
    l2 = _l2([RuleHit(stage="L2", rule_code="cat_access_blocked", penalty=-100,
                      detail={"category": "类目准入"}),
              RuleHit(stage="L2", rule_code="content_promotional", penalty=0,
                      detail={"strong_phrases": ["best seller"],
                              "allcaps_runs": [], "soft_only": False})])
    txt, _ = audit_l3.summarize_evidence(None, None, l2)
    assert txt == "* 促销宣称(R7, 含无据宣称/全大写滥用): best seller"


def test_证据通道_R4前10去重保序_R5小写():
    l2 = _l2([_r4_hit(),
              RuleHit(stage="L2", rule_code="trademark_live", penalty=0,
                      detail={"matched_marks": ["ACME", "acme", "ZETA"]})])
    txt, brands = audit_l3.summarize_evidence(None, None, l2)
    assert "共12个, 前10" in txt and "nike0(原文:Nike0)" in txt
    assert "nike10" not in txt                      # matches[:10]
    assert "* USPTO LIVE 商标(R5, 前10): ACME, acme, ZETA" in txt
    assert brands == [f"nike{i}" for i in range(10)]  # 品牌总数也截 10


def test_证据通道_cert分支取真实键名():
    """键名照**规则真实写进 detail 的那些**取(meta_requirements /
    hard_cert_fields / soft_cert_fields);取 `requirements` 那个键三种 cert
    hit 里一个都没有,前两档会永远退化成一句固定套话。"""
    small = RuleHit(stage="L2", rule_code="cat_requires_cert_small_part",
                    penalty=0, detail={"hard_cert_fields": ["UL"], "note": "n"})
    soft_meta = RuleHit(stage="L2", rule_code="cat_requires_cert_soft", penalty=0,
                        detail={"meta_requirements": "ASTM F963 测试报告",
                                "note": "软合规"})
    txt, _ = audit_l3.summarize_evidence(None, None, _l2([small, soft_meta]))
    lines = txt.split("\n")
    assert lines[0] == "* 类目需证书(cat_requires_cert_small_part): ['UL']"
    assert lines[1] == "* 类目需证书(cat_requires_cert_soft): ASTM F963 测试报告"


# ---------------------------------------------------------------- user prompt


def test_user_prompt_截断_bullets10_desc3000_notes200():
    p = _product(bullet_points=[f"b{i}" for i in range(14)],
                 long_description=" " + "x" * 3200)
    out = audit_l3.build_user_prompt(p, _l1(), _l2(), pt_notes="N" * 250)
    assert "  - b9\n" in out and "  - b10" not in out       # MAX_BULLETS=10
    assert "x" * 3000 + "...(已截断)" in out and "x" * 3001 not in out
    assert "飞书人工标注 (pt_meta.notes): " + "N" * 200 + "\n" in out
    assert "N" * 201 not in out
    assert (audit_l3.MAX_DESC_CHARS, audit_l3.MAX_BULLETS) == (3000, 10)


def test_user_prompt_删了原产国与路由提示_段名改上游证据():
    """恒空的原产国字段会诱导 LLM 把"原产国未知"当证据;路由行已退役。"""
    out = audit_l3.build_user_prompt(_product(), _l1(), _l2())
    assert out.startswith("# 产品信息\n")
    assert "原产国" not in out
    assert "政策路由提示" not in out
    assert "# L2 规则引擎命中" not in out
    assert "# 上游证据" in out
    assert "(上游无证据)" in out
    assert "  (上游无品牌命中, 跳过品牌真伪判定)" in out


def test_user_prompt_带本PT准入要求_截500_没有就整段不出():
    """R3 硬拒的替身(§3.2):类目要什么证是事实,这个产品要不要是判断。"""
    out = audit_l3.build_user_prompt(_product(), _l1(), _l2(),
                                     pt_requirements="CPC 证书 " + "R" * 600)
    assert "\n# 本 PT 的沃尔玛准入要求\n\nCPC 证书 " + "R" * 493 + "\n" in out
    assert "R" * 494 not in out
    assert audit_l3.MAX_REQ_CHARS == 500
    assert "本 PT 的沃尔玛准入要求" not in audit_l3.build_user_prompt(
        _product(), _l1(), _l2(), pt_requirements="")


def test_user_prompt_空态():
    out = audit_l3.build_user_prompt(
        _product(bullet_points=[], long_description="", brand=""),
        _l1(walmart_product_type=None, pt_confidence=None, pt_source=None,
            walmart_category=None), _l2())
    assert "五点描述:\n  (无)\n" in out
    assert "长描述:\n(空)\n" in out
    assert "品牌字段(商家填报): (空)" in out
    assert "沃尔玛 PT: (空) (置信 None, 源 None)" in out


# ---------------------------------------------------------------- 解析


ALLOWED = audit_l3.policy_enum(KNOWN)


def test_枚举是全集加两条非政策类别_none不在里面():
    assert "Children's Products" in ALLOWED and "Intellectual Property" in ALLOWED
    assert "内部黑名单" in ALLOWED and "类目准入" in ALLOWED
    assert "none" not in ALLOWED and "brand_misuse" not in ALLOWED


def test_解析_pass强制none且无hit():
    r = audit_l3.parse_l3_reply({"verdict": "PASS ", "policy": "Alcohol",
                                 "detail": "x", "confidence": "HIGH"}, ALLOWED)
    assert (r.verdict, r.policy, r.confidence) == ("pass", "none", "high")
    assert r.hits == []


def test_解析_reject产一条hit_五键定序_带提示词版本():
    r = audit_l3.parse_l3_reply(
        {"verdict": "reject", "policy": "children's products",
         "detail": "标题写 for kids 3+,儿童产品政策要求 CPC",
         "confidence": "bogus",
         "brand_verdicts": [{"brand": "nike", "is_real_brand": False}]},
        ALLOWED)
    assert r.verdict == "reject"
    assert r.policy == "Children's Products"        # 回**表内原拼写**
    assert r.confidence == "medium"                 # 非法置信度归一
    (hit,) = r.hits
    assert (hit.stage, hit.rule_code, hit.penalty) == \
        ("L3", "llm_children_s_products", 0)
    assert list(hit.detail) == ["policy", "detail", "confidence",
                                "brand_verdicts", "prompt_version"]
    assert hit.detail["prompt_version"] == resources.AUDIT_RULES_VERSION


def test_解析_政策名对不上枚举一律pending_不猜(caplog):
    """⚠ B1 删掉的降级:旧版把认不出的类别猜成 `intellectual property`。

    猜出来的类别会一路落库、进飞书 G 列、进申诉口径,而没有任何东西会红。
    现在:pending + `llm_bad_policy` 计数(成批出现 = 提示词/政策表出了问题)。
    """
    with caplog.at_level("WARNING", logger="services.audit_l3"):
        for policy in ("怪东西", "none", "", "brand_misuse"):
            r = audit_l3.parse_l3_reply({"verdict": "reject", "policy": policy,
                                         "detail": "x"}, ALLOWED)
            assert r.verdict == "pending", policy
            assert r.policy == "none" and r.confidence == "low"
            (hit,) = r.hits
            assert hit.rule_code == "llm_bad_policy" and hit.penalty == 0
            assert hit.detail["detail"] == "x"      # 原话留档,便于复盘
    assert audit_l3.STATS["llm_bad_policy"] == 4
    assert "intellectual property" not in \
        audit_l3.parse_l3_reply({"verdict": "reject", "policy": "怪东西"},
                                ALLOWED).policy


def test_解析_政策名容错到表内拼写_两条非政策类别也认():
    """大小写/词形靠 policy_names.resolve(仓内唯一一份归一化),不在这儿再写一份。"""
    for given, want in (("ALCOHOL", "Alcohol"),
                        ("  Offensive Content  ", "Offensive Content"),
                        ("childrens products", "Children's Products"),
                        ("类目准入", "类目准入"),
                        ("内部黑名单", "内部黑名单")):
        r = audit_l3.parse_l3_reply({"verdict": "reject", "policy": given},
                                    ALLOWED)
        assert (r.verdict, r.policy) == ("reject", want), given


def test_解析_detail截断500():
    r = audit_l3.parse_l3_reply({"verdict": "reject", "policy": "Alcohol",
                                 "detail": " " + "字" * 700}, ALLOWED)
    assert len(r.detail) == 500
    r2 = audit_l3.parse_l3_reply({"verdict": "reject", "policy": "Alcohol",
                                  "detail": "   "}, ALLOWED)
    assert r2.detail is None


def test_解析_is_real_brand强制翻拒_严格is_True():
    """确定性后处理:LLM 自述 pass 也翻 reject;字符串 'true' 不算(照迁口径)。"""
    r = audit_l3.parse_l3_reply(
        {"verdict": "pass", "policy": "none",
         "brand_verdicts": [{"brand": "top", "is_real_brand": False},
                            {"brand": "Dyson", "is_real_brand": True}]},
        ALLOWED)
    assert r.verdict == "reject"
    assert r.policy == "Intellectual Property" == resources.AUDIT_IP_POLICY
    assert r.detail == "未授权引用品牌名 Dyson"
    assert r.hits[0].rule_code == "llm_intellectual_property"
    for v in ("true", 1, "True"):
        r2 = audit_l3.parse_l3_reply(
            {"verdict": "pass", "policy": "none",
             "brand_verdicts": [{"brand": "x", "is_real_brand": v}]}, ALLOWED)
        assert r2.verdict == "pass", v
    r3 = audit_l3.parse_l3_reply({"verdict": "pass", "brand_verdicts": "nope"},
                                 ALLOWED)
    assert r3.brand_verdicts == [] and r3.verdict == "pass"


def test_解析_非法verdict与坏JSON全部pending():
    for raw in ({}, {"_raw": "not json"}, {"verdict": "unknown"},
                {"verdict": "pending"}):
        r = audit_l3.parse_l3_reply(raw, ALLOWED)
        assert r.verdict == "pending", raw
        assert r.policy == "none" and r.confidence == "low"
        (hit,) = r.hits
        assert hit.rule_code == "llm_bad_json" and hit.penalty == 0
    assert len(audit_l3.parse_l3_reply({"_raw": "x" * 999}, ALLOWED)
               .hits[0].detail["raw"]) == 300


# ---------------------------------------------------------------- 入口


def _patch_llm(monkeypatch, reply=None, exc=None, calls=None):
    def fake(messages, *, temperature, max_tokens, purpose):
        if calls is not None:
            calls.append({"messages": messages, "temperature": temperature,
                          "max_tokens": max_tokens, "purpose": purpose})
        if exc is not None:
            raise exc
        return reply
    monkeypatch.setattr(audit_l3._llm_api, "chat_json", fake)
    return calls


def test_judge_调用参数与两产品system一致(monkeypatch):
    calls = _patch_llm(monkeypatch, reply={"verdict": "pass"}, calls=[])
    conn = FakeConn()
    ctx = _ctx(pt_meta={"Sneakers": {"notes": "⚠️T&S抽查48h内",
                                     "requirements": "CPC + ASTM F963"}})
    r1 = audit_l3.judge_l3(_product(), _l1(), _l2(), ctx, conn)
    r2 = audit_l3.judge_l3(_product(asin="B0OTHER", title="Baby Bib"),
                           _l1(walmart_category="Baby"), _l2(), ctx, conn)
    assert r1.verdict == "pass" and r2.verdict == "pass"
    assert [c["temperature"] for c in calls] == [0.1, 0.1]
    assert [c["max_tokens"] for c in calls] == [1500, 1500]
    assert [c["purpose"] for c in calls] == ["audit_l3", "audit_l3"]
    assert [m["role"] for m in calls[0]["messages"]] == ["system", "user"]
    # 前缀缓存生死线:两个产品的 system 段逐字节相同,user 段不同
    assert calls[0]["messages"][0] == calls[1]["messages"][0]
    assert calls[0]["messages"][1] != calls[1]["messages"][1]
    user0 = calls[0]["messages"][1]["content"]
    assert "飞书人工标注 (pt_meta.notes): ⚠️T&S抽查48h内" in user0
    # 本 PT 的准入要求从 ctx.pt_meta 带进来(judge 不查库)
    assert "# 本 PT 的沃尔玛准入要求\n\nCPC + ASTM F963" in user0


def test_judge_把L0证据也送进user段(monkeypatch):
    """接线钉子:phase0 的软 hit 必须到得了提示词(C 批品牌扫描迁 L0 的前提)。"""
    calls = _patch_llm(monkeypatch, reply={"verdict": "pass"}, calls=[])
    p0 = Phase0Result(blocked=False, hits=[
        RuleHit(stage="L0", rule_code="phase0_brand_mention", penalty=0,
                detail={"matches": [{"brand": "dyson",
                                     "matched_phrase": "Dyson V6"}],
                        "count": 1})])
    audit_l3.judge_l3(_product(), _l1(), _l2(), _ctx(), FakeConn(), phase0=p0)
    user = calls[0]["messages"][1]["content"]
    assert "* 文案提到黑名单品牌(共1个, 前10): dyson(原文:Dyson V6)" in user
    assert "\n  - dyson\n" in user          # 品牌词清单出自同一通道


def test_judge_命中缓存不调LLM(monkeypatch):
    calls = _patch_llm(monkeypatch, reply={"verdict": "pass"}, calls=[])
    conn = FakeConn()
    ctx = _ctx()
    audit_l3.judge_l3(_product(), _l1(), _l2(), ctx, conn)
    assert len(calls) == 1 and len(conn.put_log) == 1
    conn2 = FakeConn(cache={conn.put_log[0]: {"verdict": "reject",
                                              "policy": "Alcohol"}})
    r = audit_l3.judge_l3(_product(), _l1(), _l2(), ctx, conn2)
    assert len(calls) == 1                      # 未再调 LLM
    assert r.verdict == "reject" and r.policy == "Alcohol"
    assert conn2.put_log == []                  # 命中不重复写


@pytest.mark.parametrize("exc,rule_code", [
    (RuntimeError("LLM 调用连续 3 次失败:timeout"), "llm_chain_exhausted"),
    (ValueError("LLM 请求被拒 HTTP 400"), "llm_error"),
])
def test_judge_失败_pending_且不写缓存(monkeypatch, exc, rule_code):
    _patch_llm(monkeypatch, exc=exc)
    conn = FakeConn()
    r = audit_l3.judge_l3(_product(), _l1(), _l2(), _ctx(), conn)
    assert r.verdict == "pending" and r.confidence == "low"
    (hit,) = r.hits
    assert (hit.stage, hit.rule_code, hit.penalty) == ("L3", rule_code, 0)
    assert hit.detail["error"] == str(exc)[:500]
    assert conn.put_log == []


def test_judge_错误摘要截断500(monkeypatch):
    _patch_llm(monkeypatch, exc=RuntimeError("e" * 900))
    r = audit_l3.judge_l3(_product(), _l1(), _l2(), _ctx(), FakeConn())
    assert len(r.hits[0].detail["error"]) == 500
    assert len(r.raw["error"]) == 500
    assert r.detail == "LLM 全链路故障, 待人工复核"


@pytest.mark.parametrize("reply", [{}, {"_raw": "??"}, {"verdict": "maybe"},
                                   {"verdict": "reject", "policy": "怪东西"}])
def test_judge_坏输出_pending_且不写缓存(monkeypatch, reply):
    _patch_llm(monkeypatch, reply=reply)
    conn = FakeConn()
    r = audit_l3.judge_l3(_product(), _l1(), _l2(), _ctx(), conn)
    assert r.verdict == "pending"
    assert conn.put_log == []                   # pending 绝不写缓存


def test_judge_reject写缓存且分数不由L3动(monkeypatch):
    _patch_llm(monkeypatch, reply={"verdict": "reject",
                                   "policy": "Offensive Content",
                                   "detail": "冒犯图案"})
    conn = FakeConn()
    r = audit_l3.judge_l3(_product(), _l1(), _l2(), _ctx(), conn)
    assert r.verdict == "reject" and len(conn.put_log) == 1
    assert all(h.penalty == 0 for h in r.hits)
