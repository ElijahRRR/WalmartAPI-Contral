"""L3 语义审核测试(services/audit_l3.py;批次 C 移植钉住测试)。

离线:不打网,chat_json 一律 monkeypatch;DB 走 FakeConn 假游标。

重点钉住三类东西:
  1. **提示词字节稳定** —— S1/S3 字面量 sha256、system prompt 两次构造相同、
     两个不同产品的 messages[0] 相同(前缀缓存命中率的生死线);
  2. **已知缺陷照迁** —— R7/R8 不进 prompt、cert requirements 键名 bug 套话、
     原产国行恒 (空)、is_real_brand 严格 `is True` 强制翻拒;
  3. **失败四路 → pending 且不写 llm_cache**。
"""

import hashlib

import pytest

from registry import paths, resources
from services import audit_l3
from services.audit_models import L1Info, L2Result, ProductInfo, RuleHit

# ---------------------------------------------------------------- 假件

POLICY_ROWS = [
    (1, "Alcohol", "酒精", "禁售", "蒸馏设备•酒类", "红色高风险", "别碰"),
    (2, "Animals", "动物", "", "活体动物", "黄色", "备注不该出现"),
    (3, "Intellectual Property", "知识产权", "限制", "仿冒品", "红线", "IP 备注"),
    (4, "Offensive Content", "冒犯内容", "禁售", "", "绿色", "无禁品字段"),
]
POLICY_COLS = ["id", "category_en", "category_zh", "overall_status",
               "prohibited_items", "zh_seller_risk", "zh_seller_notes"]
# 库序(ORDER BY category_en 由 PG 给出),故意不是 Python sorted 序
CATEGORY_ROWS = [("Alcohol",), ("Animals",), ("Pet Products",),
                 ("PFAS Chemicals",), ("Intellectual Property",),
                 ("Offensive Content",), ("Children's Products",)]
KNOWN = frozenset(c for (c,) in CATEGORY_ROWS)


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
    yield
    audit_l3.reset_prompt_cache()


# ---------------------------------------------------------------- 提示词字节稳定


def test_s1_s3_literals_byte_pinned():
    """S1/S3 字节钉死 —— 提示词是判定面,不许无声漂移。

    S3 仍逐字节等于旧仓 l3_llm.py:432-438(除计数占位符)。
    S1 原本也是逐字节移植(旧仓 :295-431,sha256 859aced1…,5233 字符),
    此后**有意改过两次**,两次都是所有者定的:

      · 2026-08-21:补入判定维度 6「整机电器 / NRTL 认证」—— L2 里按 PT 名猜
        整机/小件的分类器同日下线,而"这个产品是不是整机电器"必须有人接,
        不能留真空期(见 test_audit_l2_reason 里
        `test_the_whole_appliance_call_moved_to_the_l3_prompt`);
      · 2026-09-02(§十.7):S1/S3 各一处硬写的「37 条」换成 `{N}` 占位符,
        由 `build_system_prompt` 按实时政策条数渲染。**除这一个 token 外
        两段一个字节都没动** —— 下一条测试拿"填回 37"逐字节证明。
    """
    assert hashlib.sha256(audit_l3._S1.encode()).hexdigest() == (
        "05a955e4331012ee564fd2ffd96c38f35813b560b536eaf19fcca26043e98412")
    assert hashlib.sha256(audit_l3._S3.encode()).hexdigest() == (
        "52edf07f5c85262fe03e827f9e6d4fcabdbf8fe622fa648e90d674ec69551d68")
    assert len(audit_l3._S1) == 6028
    assert audit_l3._S1.endswith("# 候选 reason_category (verdict=reject 时必选其一)\n")
    assert audit_l3._S3.startswith("\n\n# 沃尔玛 {N} 条 Prohibited Products Policy 全清单")


def test_only_the_policy_count_token_moved_in_2026_09_02():
    """⚠ 动态化只许动**计数 token**:把 `{N}` 填回 `37`,两段必须逐字节回到旧值。

    这条测试是那次改动的边界证明 —— 提示词是判定面,"顺手改一句措辞"不会报错,
    只会让判定悄悄漂;有了它,任何夹带都会在这里露出来。
    """
    assert audit_l3._COUNT_SLOT == "{N}"
    assert hashlib.sha256(
        audit_l3._fill_count(audit_l3._S1, 37).encode()).hexdigest() == (
        "8248ff0c2c2ac2960625a6a14348a578e00efbc89410d390f45f4cee156dc1a3")
    assert hashlib.sha256(
        audit_l3._fill_count(audit_l3._S3, 37).encode()).hexdigest() == (
        "1564f179581b34ad5785caba6daa1b461e0624129c55cbec19e47c5025d2d409")


def test_the_policy_count_follows_the_table_not_a_literal():
    """⚠ 提示词自称的条数 = 紧随其后的 S4 全清单行数,不是写死的「37 条」。

    写死的后果不报错:政策表补了武器族之后,提示词还在说"沃尔玛 37 条全清单",
    而下面列着 42 条 —— LLM 拿那个数当"我应该看到多少条"的锚。
    """
    cats = [c for (c,) in CATEGORY_ROWS]
    rows = [dict(zip(POLICY_COLS, r)) for r in POLICY_ROWS]
    p = audit_l3.build_system_prompt(cats, rows)
    assert f"沃尔玛 {len(rows)} 条 Prohibited Products Policy 全清单" in p
    assert p.count(f"沃尔玛 {len(rows)} 条") == 2       # S1 一处 + S3 一处
    assert "{N}" not in p and "沃尔玛 37 条" not in p
    # 多一行政策,两处数字一起跟着走
    more = rows + [dict(zip(POLICY_COLS,
                            (99, "Firearms", "枪支", "禁售", "枪", "红", "")))]
    assert f"沃尔玛 {len(more)} 条" in audit_l3.build_system_prompt(cats, more)


def test_system_prompt_二次构造字节相同且进程内只查一次库():
    cats = [c for (c,) in CATEGORY_ROWS]
    rows = [dict(zip(POLICY_COLS, r)) for r in POLICY_ROWS]
    a = audit_l3.build_system_prompt(cats, rows)
    b = audit_l3.build_system_prompt(cats, rows)
    assert a == b and hash(a) == hash(b)

    conn = FakeConn()
    p1 = audit_l3.system_prompt(conn)
    p2 = audit_l3.system_prompt(conn)
    assert p1 is p2 and p1 == a
    assert len([s for s in conn.sql_log if "walmart_prohibited_policy" in s]) == 2


def test_system_prompt_四段顺序():
    p = audit_l3.system_prompt(FakeConn())
    i1 = p.index("你是沃尔玛 Marketplace 合规审核 AI")
    i2 = p.index("  - Alcohol\n")
    i3 = p.index(f"# 沃尔玛 {len(POLICY_ROWS)} 条 Prohibited Products Policy 全清单")
    i4 = p.index("## 1. Alcohol (酒精)")
    assert i1 < i2 < i3 < i4


def test_S2_保库序并固定两项收尾():
    """ORDER BY category_en 的库序不可用 Python sorted 覆盖(PFAS/Pet 两序相反)。"""
    block = audit_l3.format_reason_categories([c for (c,) in CATEGORY_ROWS])
    lines = block.split("\n")
    assert lines == [f"  - {c}" for (c,) in CATEGORY_ROWS] + \
        ["  - brand_misuse", "  - none"]
    assert lines != sorted(lines)


def test_S4_截断常量_50_30_240_80_与备注红闸():
    rows = [{"id": 9, "category_en": "X", "category_zh": "叉",
             "overall_status": "S" * 80, "zh_seller_risk": "红" + "R" * 80,
             "prohibited_items": "P" * 300, "zh_seller_notes": "N" * 200}]
    out = audit_l3.format_full_policy_block(rows)
    assert "状态: " + "S" * 50 + " | 中国卖家:" + "红" + "R" * 29 in out
    assert "禁: " + "P" * 240 + "\n" in out
    assert out.split("备注: ")[1] == "N" * 80
    # 风险非红 → 备注整行不出;空字段整行不出;条目之间空行分隔
    rows2 = [{"id": 1, "category_en": "A", "category_zh": "a",
              "overall_status": "", "zh_seller_risk": "绿",
              "prohibited_items": "", "zh_seller_notes": "N"},
             {"id": 2, "category_en": "B", "category_zh": "b",
              "overall_status": "ok", "zh_seller_risk": "",
              "prohibited_items": "x•y\nz", "zh_seller_notes": ""}]
    out2 = audit_l3.format_full_policy_block(rows2)
    assert out2 == "## 1. A (a)\n\n## 2. B (b)\n状态: ok\n禁: x/y z"


# ---------------------------------------------------------------- 政策路由


def test_路由_always_include_恒在最前且截断挤不掉():
    cats = audit_l3.route_policy_hints("Baby", "Baby Bodysuit")
    assert cats[:2] == ["Intellectual Property", "Offensive Content"]
    assert len(cats) == 5
    # category 路由(等值)+ PT 子串路由都在,去重保序
    assert cats == ["Intellectual Property", "Offensive Content",
                    "Baby Products", "Children's Products", "PFAS Chemicals"]


def test_路由_等值查表不是子串_而PT是裸子串():
    assert audit_l3.route_policy_hints("Home Improvement Tools", None) == \
        ["Intellectual Property", "Offensive Content"]
    assert audit_l3.route_policy_hints("home improvement", None) == \
        ["Intellectual Property", "Offensive Content", "Home Goods",
         "Hazardous Items"]
    # PT 子串:'blade' 在 'Bladeless Fans' 里也算命中(裸子串,照迁)
    assert "Military & Law Enforcement" in \
        audit_l3.route_policy_hints(None, "Bladeless Fans")
    # 多组同时命中,按表顺序追加
    assert audit_l3.route_policy_hints(None, "baby knife toddler") == \
        ["Intellectual Property", "Offensive Content",
         "Military & Law Enforcement", "Baby Products", "Children's Products"]


def test_路由_先截断后按库内政策过滤():
    got = audit_l3.route_policy_hints("baby", None, known=KNOWN)
    # 截断到 5 条后,库里没有的 Recalled Products 本就在第 6 位;
    # PFAS Chemicals 在库里 → 保留
    assert got == ["Intellectual Property", "Offensive Content",
                   "Children's Products", "PFAS Chemicals"]
    assert audit_l3.route_policy_hints("toys", None, known=frozenset()) == []


def _route_names() -> set[str]:
    """路由两张表里出现过的全部政策名(去重)。"""
    names = set(audit_l3._ALWAYS_INCLUDE)
    for v in audit_l3._CATEGORY_ROUTES.values():
        names.update(v)
    for _kws, v in audit_l3._PT_KEYWORD_ROUTES:
        names.update(v)
    return names


# 政策表的两种形态:改名后(官方 42 名)/ 改名前那 7 行(存量缩写名)
_OFFICIAL = frozenset(
    f.read_text(encoding="utf-8").split("\n", 1)[0][2:].strip()
    for f in sorted(paths.policy_pages_dir("en").glob("*.md")))
_LEGACY = frozenset(resources.POLICY_LEGACY_NAMES)

# 路由表里**本来就对不上政策表**的两条(2026-09-02 实测):官方没有这两个类别名,
# 归一化也打不平 —— `Pet Products` 官方叫 `Pet Foods, Supplements, Medicines and
# Other Products`,`Jewelry/Precious Metals` 是旧仓自造的斜杠写法。它们记 warning
# 计数,改法随「L3 输出规范化」一起定(docs/audit_pipeline.md §6 点名)。
# 2026-09-02 首跑 dry-run 后这两条也进了 POLICY_LEGACY_NAMES(官方名各是一长串),
# 路由表里**不再有**对不上的条目 —— 空元组是有意的,别删这个守门。
_DEAD_ROUTES: tuple[str, ...] = ()


def test_路由表的政策名改名前后都对得上政策表():
    """⚠ 路由表写死的是**旧缩写名**(`Electronics & RF` 一族),政策表 2026-09-02
    改成官方拼写。旧写法 `c in known` 会**静默丢掉 7 条**:提示词照旧漂亮,
    L3 少拿到 7 类政策提示,判据悄悄变窄,而没有任何东西会红。

    现在两级都走 `policy_names.resolve`,同一份路由表在两种表形态下都活。
    """
    from services import policy_names
    names = _route_names()
    assert len(names) == 29
    dead = sorted(n for n in names if policy_names.resolve(n, _OFFICIAL) is None)
    assert dead == list(_DEAD_ROUTES)          # 映射表补齐后一条不剩
    # 那 7 个旧缩写名:改名后解析到官方名,改名前(表里还是旧名)解析到旧名
    for legacy, official in resources.POLICY_LEGACY_NAMES.items():
        if legacy not in names:
            continue
        assert policy_names.resolve(legacy, _OFFICIAL) == official, legacy
        assert policy_names.resolve(legacy, _LEGACY) == legacy, legacy
    assert len({n for n in names} & set(resources.POLICY_LEGACY_NAMES)) == 9


def test_路由_返回表内原拼写而不是路由表写的名字():
    """命中回**表里那一个串** —— 下游(user prompt 的 routed_cats)与 S2 候选块
    必须是同一套拼写,否则 LLM 看到的候选与提示对不上。"""
    got = audit_l3.route_policy_hints("electronics", None, known=_OFFICIAL)
    assert got == ["Intellectual Property", "Offensive Content",
                   "Electronics and Radio Frequency Devices",
                   "Hazardous Items", "Digital Goods"]
    # 改名落地前(表里还是缩写名)照样命中,回的是缩写名
    old_table = _LEGACY | {"Intellectual Property", "Offensive Content",
                           "Hazardous Items", "Digital Goods"}
    assert audit_l3.route_policy_hints("electronics", None, known=old_table) == \
        ["Intellectual Property", "Offensive Content", "Electronics & RF",
         "Hazardous Items", "Digital Goods"]


def test_路由_解析不到的条目记warning与计数(caplog):
    """⚠ 兜底三要件:解析不到 = 少给一条政策提示,**必须记日志计数**
    (debug 不算 —— 没人看得见的信号等于不存在)。同一个名字一轮只警告一次,
    78 万行不该被刷屏,但计数照旧逐次累加。"""
    audit_l3.reset_stats()
    assert audit_l3.STATS["route_unresolved"] == 0
    with caplog.at_level("WARNING", logger="services.audit_l3"):
        # 表里缺 Pet 那一行(模拟官方删类/表未同步)⇒ 路由条目解析不到
        table = _OFFICIAL - {"Pet Foods, Supplements, Medicines and Other Products"}
        for _ in range(3):
            got = audit_l3.route_policy_hints("animals", "pet food",
                                              known=table)
    assert "Pet Products" not in got and "Animals" in got
    assert audit_l3.STATS["route_unresolved"] == 3          # 逐次计数
    assert audit_l3.STATS["route_unresolved:Pet Products"] == 3
    assert audit_l3.unresolved_route_names() == ["Pet Products"]
    hits = [r for r in caplog.records if "Pet Products" in r.getMessage()]
    assert len(hits) == 1                                   # 只警告一次
    audit_l3.reset_stats()
    assert audit_l3.unresolved_route_names() == []


# ---------------------------------------------------------------- L2 软证据


def _r4_hit(n=12):
    return RuleHit(stage="L2", rule_code="title_desc_blacklist", penalty=0,
                   detail={"matches": [{"brand": f"nike{i}",
                                        "matched_phrase": f"Nike{i}"}
                                       for i in range(n)], "count": n})


def test_L2摘要_R4前10去重保序_R5小写():
    l2 = _l2([_r4_hit(),
              RuleHit(stage="L2", rule_code="trademark_live", penalty=0,
                      detail={"matched_marks": ["ACME", "acme", "ZETA"]})])
    txt, brands = audit_l3.summarize_l2_for_l3(l2)
    assert "共12个, 前10" in txt and "nike0(原文:Nike0)" in txt
    assert "nike10" not in txt                      # matches[:10]
    assert "* USPTO LIVE 商标(R5, 前10): ACME, acme, ZETA" in txt
    assert brands == [f"nike{i}" for i in range(10)]  # 品牌总数也截 10
    # R5 单独出现时小写入表
    _, b2 = audit_l3.summarize_l2_for_l3(_l2([
        RuleHit(stage="L2", rule_code="trademark_live", penalty=0,
                detail={"matched_marks": ["ACME", "acme"]})]))
    assert b2 == ["acme"]


def test_L2摘要_R7R8证据现在真的进prompt了():
    """2026-08-20 修(原 L3-1 缺陷):R7/R8 此前没有分支,L2 在 detail 里写着
    "L3 LLM 需判断宣称词是否有事实依据",而 L3 一个字都收不到 ——
    承诺了没送到,比不承诺更糟(L3 只能自己从原文重看一遍)。"""
    l2 = _l2([
        RuleHit(stage="L2", rule_code="content_promotional", penalty=0,
                detail={"strong_phrases": ["best seller"], "allcaps_runs": [],
                        "soft_phrases": [], "soft_only": False}),
        RuleHit(stage="L2", rule_code="walmart_strict_sensitive", penalty=0,
                detail={"subtypes": ["adult"], "matched_phrases": ["sexy"]}),
    ])
    txt, _ = audit_l3.summarize_l2_for_l3(l2)
    assert "促销宣称(R7, 含无据宣称/全大写滥用): best seller" in txt
    assert "敏感/严格合规(R8, adult): sexy" in txt


def test_L2摘要_R7只命中软词时标明份量():
    """软证据也要送到,但要让 LLM 看出它只是空洞形容词,不是无据宣称。"""
    l2 = _l2([RuleHit(stage="L2", rule_code="content_promotional", penalty=0,
                      detail={"strong_phrases": [], "allcaps_runs": [],
                              "soft_phrases": ["high quality"],
                              "soft_only": True})])
    txt, _ = audit_l3.summarize_l2_for_l3(l2)
    assert "促销宣称(R7, 仅空洞形容词): high quality" in txt


def test_L2摘要_cert分支取真实键名():
    """2026-08-20 修(原 L3-2 缺陷):分支取的是 detail['requirements'],
    而这个键在三种 cert hit 里**一个都不存在**(真实键是 meta_requirements /
    hard_cert_fields / soft_cert_fields),前两档永远退化成一句固定套话。"""
    small = RuleHit(stage="L2", rule_code="cat_requires_cert_small_part",
                    penalty=0, detail={"hard_cert_fields": ["UL"],
                                       "note": "电气小件/配件, 部分可填 No 上架, 需下游人工审核"})
    soft_meta = RuleHit(stage="L2", rule_code="cat_requires_cert_soft", penalty=0,
                        detail={"meta_requirements": "ASTM F963 测试报告",
                                "matched_soft_kws": ["astm"],
                                "note": "软合规 (SDS/ASTM/ISO/RoHS/Prop65/警告标签/测试报告), 可提供资料或填披露"})
    soft_spec = RuleHit(stage="L2", rule_code="cat_requires_cert_soft", penalty=0,
                        detail={"soft_cert_fields": ["prop65Warning"], "note": "n"})
    txt, _ = audit_l3.summarize_l2_for_l3(_l2([small, soft_meta, soft_spec]))
    lines = txt.split("\n")
    assert lines[0] == "* 类目需证书(cat_requires_cert_small_part): ['UL']"
    assert "ASTM F963 测试报告" in txt          # meta_requirements 现在送得到
    assert lines[2] == "* 类目需证书(cat_requires_cert_soft): ['prop65Warning']"


def test_L2摘要_三条死分支不迁():
    l2 = _l2([RuleHit(stage="L2", rule_code="brand_blacklist", penalty=-100,
                      detail={"brand": "nike"}),
              RuleHit(stage="L2", rule_code="forbidden_mega_cat", penalty=-100,
                      detail={"cat": "x"}),
              RuleHit(stage="L2", rule_code="cat_zh_blocked", penalty=-100,
                      detail={"pt": "x"})])
    assert audit_l3.summarize_l2_for_l3(l2) == ("(L2 无命中)", [])


# ---------------------------------------------------------------- user prompt


def test_user_prompt_截断_bullets5_desc600_notes200():
    p = _product(bullet_points=[f"b{i}" for i in range(9)],
                 long_description=" " + "x" * 700)
    out = audit_l3.build_user_prompt(p, _l1(), _l2(), pt_notes="N" * 250,
                                     routed_cats=[])
    assert "  - b4\n" in out and "  - b5" not in out
    assert "x" * 600 + "...(已截断)" in out and "x" * 601 not in out
    assert "飞书人工标注 (pt_meta.notes): " + "N" * 200 + "\n" in out
    assert "N" * 201 not in out


def test_user_prompt_已知缺陷_原产国行恒空():
    """已知缺陷照迁(spec §3.4 / 合同 L3-9):行保留,值恒 (空)。"""
    out = audit_l3.build_user_prompt(_product(), _l1(), _l2())
    assert "\n原产国: (空)\n" in out


def test_user_prompt_空态与hint行():
    out = audit_l3.build_user_prompt(
        _product(bullet_points=[], long_description="", brand=""),
        _l1(walmart_product_type=None, pt_confidence=None, pt_source=None,
            walmart_category=None), _l2())
    assert out.startswith("# 产品信息\n")            # hint_line 为空则整行不出
    assert "五点描述:\n  (无)\n" in out
    assert "长描述:\n(空)\n" in out
    assert "品牌字段(商家填报): (空)" in out
    assert "沃尔玛 PT: (空) (置信 None, 源 None)" in out
    assert "(L2 无命中)" in out
    assert "  (无 R4/R5 命中, 跳过品牌真伪判定)" in out
    out2 = audit_l3.build_user_prompt(_product(), _l1(), _l2(),
                                      routed_cats=["A", "B", "C", "D", "E", "F"])
    assert out2.startswith("# 政策路由提示 (本产品 PT/category 最相关的政策, "
                           "优先核对): A, B, C, D, E\n\n# 产品信息")


# ---------------------------------------------------------------- 解析


ALLOWED = audit_l3.valid_reason_categories(KNOWN)


def test_白名单是全集与路由无关():
    assert "children's products" in ALLOWED and "brand_misuse" in ALLOWED \
        and "none" in ALLOWED
    assert "intellectual property" in ALLOWED


def test_解析_pass归一无hit():
    r = audit_l3.parse_l3_reply({"verdict": "PASS ", "reason_category": "Alcohol",
                                 "reason_text": "x", "llm_confidence": "HIGH"},
                                ALLOWED)
    assert (r.verdict, r.reason_category, r.llm_confidence) == \
        ("pass", "none", "high")
    assert r.hits == []


def test_解析_reject产一条hit_五键定序_penalty0():
    r = audit_l3.parse_l3_reply(
        {"verdict": "reject", "reason_category": "Children's Products",
         "reason_text": "儿童用品需 CPC", "llm_confidence": "bogus",
         "signals": {"has_trademark_symbol": True},
         "blacklist_brand_verdict": [{"brand": "nike", "is_real_brand": False}]},
        ALLOWED)
    assert r.verdict == "reject" and r.reason_category == "children's products"
    assert r.llm_confidence == "medium"
    (hit,) = r.hits
    assert (hit.stage, hit.rule_code, hit.penalty) == \
        ("L3", "llm_children_s_products", 0)
    assert list(hit.detail) == ["reason_category", "reason_text",
                                "llm_confidence", "signals",
                                "blacklist_brand_verdict"]


def test_解析_none类目走llm_reject_与legacy映射():
    r = audit_l3.parse_l3_reply({"verdict": "reject", "reason_category": "none"},
                                ALLOWED)
    assert r.hits[0].rule_code == "llm_reject"
    r2 = audit_l3.parse_l3_reply({"verdict": "reject",
                                  "reason_category": "ip_infringement"}, ALLOWED)
    assert r2.reason_category == "intellectual property"
    assert r2.hits[0].rule_code == "llm_intellectual_property"
    r3 = audit_l3.parse_l3_reply({"verdict": "reject",
                                  "reason_category": "needs_cert"}, ALLOWED)
    assert r3.reason_category == "none" and r3.hits[0].rule_code == "llm_reject"
    # 不在白名单也不在 legacy → reject 时降级 IP,pass 时降级 none
    r4 = audit_l3.parse_l3_reply({"verdict": "reject",
                                  "reason_category": "怪东西"}, ALLOWED)
    assert r4.reason_category == "intellectual property"
    r5 = audit_l3.parse_l3_reply({"verdict": "pass",
                                  "reason_category": "怪东西"}, ALLOWED)
    assert r5.reason_category == "none"


def test_解析_reason_text截断500():
    r = audit_l3.parse_l3_reply({"verdict": "reject", "reason_category": "none",
                                 "reason_text": " " + "字" * 700}, ALLOWED)
    assert len(r.reason_text) == 500
    r2 = audit_l3.parse_l3_reply({"verdict": "reject", "reason_category": "none",
                                  "reason_text": "   "}, ALLOWED)
    assert r2.reason_text is None


def test_解析_is_real_brand强制翻拒_严格is_True():
    """合同 L3-7 照迁:LLM 自述 pass 也翻 reject;字符串 'true' 不算(已知缺陷)。"""
    r = audit_l3.parse_l3_reply(
        {"verdict": "pass", "reason_category": "none",
         "blacklist_brand_verdict": [{"brand": "top", "is_real_brand": False},
                                     {"brand": "Dyson", "is_real_brand": True}]},
        ALLOWED)
    assert r.verdict == "reject"
    assert r.reason_category == "intellectual property"
    assert r.reason_text == "[Trademark] 未授权引用品牌名 Dyson"
    assert r.hits[0].rule_code == "llm_intellectual_property"
    # 严格 is True:字符串 "true" / 1 都不翻
    for v in ("true", 1, "True"):
        r2 = audit_l3.parse_l3_reply(
            {"verdict": "pass", "reason_category": "none",
             "blacklist_brand_verdict": [{"brand": "x", "is_real_brand": v}]},
            ALLOWED)
        assert r2.verdict == "pass", v
    # 非 list 的 blacklist_brand_verdict 归一成 []
    r3 = audit_l3.parse_l3_reply({"verdict": "pass",
                                  "blacklist_brand_verdict": "nope"}, ALLOWED)
    assert r3.blacklist_brand_verdict == [] and r3.verdict == "pass"


def test_解析_非法verdict与坏JSON全部pending():
    for raw in ({}, {"_raw": "not json"}, {"verdict": "unknown"},
                {"verdict": "pending"}):
        r = audit_l3.parse_l3_reply(raw, ALLOWED)
        assert r.verdict == "pending", raw
        assert r.reason_category == "none" and r.llm_confidence == "low"
        (hit,) = r.hits
        assert hit.rule_code == "llm_bad_json" and hit.penalty == 0
    assert audit_l3.parse_l3_reply({"_raw": "x" * 999}, ALLOWED).hits[0] \
        .detail["raw"][:4] == "{'_r"
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
    ctx = _ctx(pt_meta={"Sneakers": {"notes": "⚠️T&S抽查48h内"}})
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
    assert "飞书人工标注 (pt_meta.notes): ⚠️T&S抽查48h内" in \
        calls[0]["messages"][1]["content"]
    assert "# 政策路由提示" in calls[1]["messages"][1]["content"]


def test_judge_命中缓存不调LLM(monkeypatch):
    calls = _patch_llm(monkeypatch, reply={"verdict": "pass"}, calls=[])
    conn = FakeConn()
    ctx = _ctx()
    audit_l3.judge_l3(_product(), _l1(), _l2(), ctx, conn)
    assert len(calls) == 1 and len(conn.put_log) == 1
    conn2 = FakeConn(cache={conn.put_log[0]: {"verdict": "reject",
                                              "reason_category": "Alcohol"}})
    r = audit_l3.judge_l3(_product(), _l1(), _l2(), ctx, conn2)
    assert len(calls) == 1                      # 未再调 LLM
    assert r.verdict == "reject" and r.reason_category == "alcohol"
    assert conn2.put_log == []                  # 命中不重复写


@pytest.mark.parametrize("exc,rule_code", [
    (RuntimeError("LLM 调用连续 3 次失败:timeout"), "llm_chain_exhausted"),
    (ValueError("LLM 请求被拒 HTTP 400"), "llm_error"),
])
def test_judge_失败_pending_且不写缓存(monkeypatch, exc, rule_code):
    _patch_llm(monkeypatch, exc=exc)
    conn = FakeConn()
    r = audit_l3.judge_l3(_product(), _l1(), _l2(), _ctx(), conn)
    assert r.verdict == "pending" and r.llm_confidence == "low"
    (hit,) = r.hits
    assert (hit.stage, hit.rule_code, hit.penalty) == ("L3", rule_code, 0)
    assert hit.detail["error"] == str(exc)[:500]
    assert conn.put_log == []


def test_judge_错误摘要截断500(monkeypatch):
    _patch_llm(monkeypatch, exc=RuntimeError("e" * 900))
    r = audit_l3.judge_l3(_product(), _l1(), _l2(), _ctx(), FakeConn())
    assert len(r.hits[0].detail["error"]) == 500
    assert len(r.raw["error"]) == 500
    assert r.reason_text == "LLM 全链路故障, 待人工复核"


@pytest.mark.parametrize("reply", [{}, {"_raw": "??"}, {"verdict": "maybe"}])
def test_judge_坏输出_pending_且不写缓存(monkeypatch, reply):
    _patch_llm(monkeypatch, reply=reply)
    conn = FakeConn()
    r = audit_l3.judge_l3(_product(), _l1(), _l2(), _ctx(), conn)
    assert r.verdict == "pending"
    assert r.hits[0].rule_code == "llm_bad_json"
    assert conn.put_log == []                   # pending 绝不写缓存


def test_judge_reject写缓存且分数不由L3动(monkeypatch):
    _patch_llm(monkeypatch, reply={"verdict": "reject",
                                   "reason_category": "Offensive Content",
                                   "reason_text": "冒犯图案"})
    conn = FakeConn()
    r = audit_l3.judge_l3(_product(), _l1(), _l2(), _ctx(), conn)
    assert r.verdict == "reject" and len(conn.put_log) == 1
    assert all(h.penalty == 0 for h in r.hits)
