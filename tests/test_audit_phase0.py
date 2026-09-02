"""Phase0 四件套判定向量(spec_vectors B1~B5/B9;旧仓判定层零测试,全部新钉)。

向量出处:批次 B 移植规格(四路取证),每条对应旧仓实现行号——断言的是
"与旧仓逐字节同判",不是"理想行为"。已知缺陷(B2-5 漏拦/B2-6 口径不一/
全角不折叠)按现状钉死,修复须先过所有者。
"""

from types import SimpleNamespace

import pytest

from registry import resources
from services import audit_phase0
from services.audit_models import ProductInfo
from services.audit_stopwords import is_stopword


def _ctx(sellers=(), asins=(), cats=(), brands=None, tops=(), nodes=(),
         known=frozenset()):
    """cats=归一化完整路径(飞书历史行);tops=顶级名;nodes=子树根 node_id。

    2026-08-20 起类目闸判据全在库里(catalog.amazon_cat_blacklist),
    这里按 services.category_blacklist 的装配形态直接喂。
    """
    from services import category_blacklist as cb
    rules = cb.CatRules(
        nodes={str(n): {"match_value": str(n), "category_path": f"子树{n}",
                        "category_zh": "", "reason": "测试子树",
                        "walmart_policy": "Intellectual Property"} for n in nodes},
        tops={cb._norm_top(t): {"match_value": t, "category_path": t,
                                "category_zh": "", "reason": f"{t} 禁售",
                                "walmart_policy": "Intellectual Property"}
              for t in tops},
        paths={c: {"match_value": c, "category_path": c, "category_zh": "",
                   "reason": "", "walmart_policy": ""} for c in cats})
    return SimpleNamespace(
        phase0_sellers=frozenset(sellers),
        phase0_asins=frozenset(asins),
        cat_rules=rules,
        brand_blacklist=dict(brands or {}),
        known_policies=known,     # 类目黑名单行的 walmart_policy 按它对表
    )


# 迁进 DB 之前硬编码在 audit_phase0 里的四个顶级(现为 SEED_RULES 的一部分)
_LEGACY_TOPS = ("Books", "Kindle Store", "Clothing, Shoes & Jewelry", "Automotive")


def _p(**kw):
    kw.setdefault("asin", "B000TEST00")
    return ProductInfo(**kw)


# ── B1 品牌精准黑名单 ─────────────────────────────────────────────────────────

BRANDS = {"ikea": "IKEA", "ernie ball": "Ernie Ball", "n/a": "N/A",
          "无": "无", "nexgrill": "Nexgrill",
          # 所有者 2026-08-23 在飞书品牌总表登记的是大写 GENERIC;
          # 键侧一律小写(audit_rules._brand_map 规整),原文回显保留大写
          "generic": "GENERIC", "oem": "OEM"}


@pytest.mark.parametrize("brand,blocked", [
    ("IKEA", True),            # B1-1
    ("ikea", True),            # B1-2 大小写不敏感
    ("  Ernie   Ball ", True), # B1-3 空白压一
    ("N/A", False),            # B1-4 占位符优先于黑名单(未撤下的 17 项照旧)
    ("n / a", False),          # B1-5 变体不识别(内部空格保留)
    ("", False), ("   ", False),   # B1-7
    ("无", False),             # B1-8 中文占位符
    ("IKEA Furniture", False), # B1-9 等值非前缀
    ("Nexgrill", True),        # B1-10 yaml additional_hard_brands 同池
    # B1-11/12/13(2026-08-23 所有者撤下 generic/oem/various):登记了就拦。
    # 大小写两侧都规整,飞书写 GENERIC / 产品写 Generic 落同一个键
    ("Generic", True), ("GENERIC", True), ("  oem ", True),
    # B1-14 撤下 ≠ 无条件拦:黑名单里没登记的照旧放行(查表落空)
    ("various", False),
])
def test_brand_blacklist_vectors(brand, blocked):
    r = audit_phase0.check(_p(brand=brand), _ctx(brands=BRANDS))
    assert r.blocked is blocked
    if blocked:
        h = r.hits[0]
        assert h.rule_code == "phase0_brand_blacklist" and h.penalty == -100
        assert h.detail["source"] == "blacklist_center"
        assert h.detail["match_type"] == "exact"


def test_placeholder_whitelist_no_longer_shields_generic_oem_various():
    """撤下的三项(所有者定稿 2026-08-23)不再挡在黑名单前面。

    起因:飞书品牌总表登记了 GENERIC,审核却对 brand=Generic 的产品照发
    pass —— _check_brand 在查黑名单**之前**先过白名单就短路返回了。
    白名单原本的假设是"占位符出现在黑名单里 = 飞书录错了",而所有者是
    **故意**登记的,代码分不出这两种情况,故按所有者口径交还黑名单裁决。

    ⚠ 同时钉住**不能顺手同步改**的那一半:services/brand_key.PLACEHOLDERS
    必须仍含这三个词。两张表方向相反 —— 这边多留一个词只是少拦一个黑名单
    品牌,占用键那边少留一个词就是 "Generic" 变成排他占用键,把成千上万个
    无关产品锁进同一家店,而占用没有自动释放。
    """
    from services import brand_key as bk
    gone = {"generic", "oem", "various"}
    assert not (gone & audit_phase0._NON_BRAND_PLACEHOLDERS)
    assert len(audit_phase0._NON_BRAND_PLACEHOLDERS) == 17
    assert gone <= bk.PLACEHOLDERS              # 占用键那边一个都不许少
    assert bk.brand_key("Generic", "OEM") is None    # 仍是真·无品牌,不占用


def test_brand_detail_keeps_raw_value():
    """detail.brand = 原值未 strip;matched_brand 回显表内原文(B1-1/B1-3)。"""
    r = audit_phase0.check(_p(brand="  Ernie   Ball "), _ctx(brands=BRANDS))
    assert r.hits[0].detail["brand"] == "  Ernie   Ball "
    assert r.hits[0].detail["matched_brand"] == "Ernie Ball"
    assert r.matched_brand == "Ernie Ball"


# ── B2 Amazon 顶级类目禁售 ───────────────────────────────────────────────────

@pytest.mark.parametrize("path,blocked,top", [
    ("Books > Literature", True, "Books"),                       # B2-1
    ("Books", True, "Books"),                                    # B2-2
    ("  books  ", True, "Books"),                                # B2-3 兜底改写规范 key
    ("Clothing Shoes & Jewelry > Men", True,
     "Clothing, Shoes & Jewelry"),                               # B2-4 缺逗号兜底
    ("Clothing,Shoes & Jewelry > Men", True,
     "Clothing, Shoes & Jewelry"),          # B2-5 旧的逗号漏拦已随 DB 化修掉
    ("Books->Kindle", False, None),                              # B2-6 只切 '>' 口径钉死
    ("Automotive Parts & Accessories", False, None),             # B2-7
    ("", False, None),                                           # B2-8
    ("Video Games > Xbox", False, None),                         # B2-9
])
def test_forbidden_category_vectors(path, blocked, top):
    r = audit_phase0.check(_p(amazon_category_path=path),
                           _ctx(tops=_LEGACY_TOPS))
    assert r.blocked is blocked
    if blocked:
        h = r.hits[0]
        assert h.rule_code == "phase0_forbidden_category" and h.penalty == -100
        assert h.detail["full_path"] == path
        assert h.detail["matched_by"] == "top_name"


def test_forbidden_category_policy_books():
    """黑名单行自带的 `walmart_policy` 是**行原值**;类别按它对政策表解析。

    ⚠ 2026-09-02 B1 §二:能解析到表 → 用那条政策;解析不到(空 / 旧拼写 /
    自造名)→ `内部黑名单`(内部决策不挂政策名,别再兜底编一个)。
    """
    known = frozenset({"Intellectual Property"})
    r = audit_phase0.check(_p(amazon_category_path="Books"),
                           _ctx(tops=_LEGACY_TOPS, known=known))
    assert r.hits[0].detail["walmart_policy"] == "Intellectual Property"
    assert r.hits[0].detail["category"] == "Intellectual Property"
    # 没有政策表可对(或对不上)⇒ 落内部黑名单
    r2 = audit_phase0.check(_p(amazon_category_path="Books"),
                            _ctx(tops=_LEGACY_TOPS))
    assert r2.hits[0].detail["category"] == resources.AUDIT_CAT_INTERNAL_BLACKLIST


# ── B2b 摘掉批次 B 新增的 4 个大类(2026-08-17 裁决 A)──────────────────────

# 所有者验收上架时的真实产品:B0BWMVQHVJ,一包牛皮纸礼品袋,被判
# 「药品/膳食补充剂 restricted」。判据只是路径**第一段**是 Health & Household
_GIFT_BAG_PATH = ("Health & Household > Stationery & Gift Wrapping Supplies"
                  " > Gift Wrapping Supplies > Gift Bags")


@pytest.mark.parametrize("path", [
    _GIFT_BAG_PATH,                                    # 礼品袋(真实误杀)
    "Health & Household > Household Supplies > Paper & Plastic",
    "Beauty & Personal Care > Tools & Accessories > Bags & Cases",
    "Health & Personal Care > Household Supplies",
    "Grocery & Gourmet Food > Beverages > Coffee",
])
def test_removed_tops_no_longer_block(path):
    """⚠ 这 4 个大类是**筐**不是**品**,Phase0 只看第一段 = 整筐一起拒。

    药品/补剂改由 L2 两道更精准的闸判(R2 按 Walmart PT 名词表、R0 按 Walmart
    类目)。摘掉是**去重**不是放开——详见 services/category_blacklist 头注
    与 docs/audit_migration_plan.md 九节补批复。
    """
    assert audit_phase0.check(_p(amazon_category_path=path),
                              _ctx(tops=_LEGACY_TOPS)).blocked is False


def test_category_rules_live_in_db_not_in_code():
    """⚠ 类目判据**不许再回到代码里**(所有者定稿 2026-08-20:
    「代码里面的类目可以拿到数据库里来,还可以减轻代码的臃肿」)。

    判定件零硬编码类目:ctx.cat_rules 为空 = 类目闸整条不生效(而不是
    "退回某个内置清单")——那样才能保证"改表就改了行为",不会出现
    "库里删了但代码里还拦着"这种查不出来的鬼。
    """
    import inspect
    from services import category_blacklist as cb
    assert not hasattr(audit_phase0, "FORBIDDEN_AMAZON_TOPS")
    src = inspect.getsource(audit_phase0)
    assert "Kindle Store" not in src and "Grocery & Gourmet Food" not in src
    # 空规则 = 不拦(判定件不自带兜底清单)
    empty = SimpleNamespace(phase0_sellers=frozenset(), phase0_asins=frozenset(),
                            cat_rules=cb.CatRules(),
                            brand_blacklist={})
    assert audit_phase0.check(_p(amazon_category_path="Books"), empty).blocked is False
    # 判定件也不许自带种子清单(2026-08-20 所有者:一切以回传的标注文档为准)
    assert not hasattr(cb, "SEED_RULES")


# ── B3 商标符号 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,blocked,brand_phrase", [
    ("Stearns & Foster® Mattress", True, "Stearns & Foster"),   # B3-1
    ("Sealy Posturepedic ™ Pillow", True, "Sealy Posturepedic"),  # B3-2
    ("Buy Stearns & Foster® Now", True, "Buy Stearns & Foster"),  # B3-3 贪婪吞前词
    ("The Product ® Set", False, None),                          # B3-4 stop prefix
])
def test_trademark_symbol_vectors(title, blocked, brand_phrase):
    r = audit_phase0.check(_p(title=title), _ctx())
    assert r.blocked is blocked
    if blocked:
        h = r.hits[0]
        assert h.rule_code == "phase0_trademark_symbol"
        assert h.detail["matched_brand"] == brand_phrase
        # 规则自报类别(2026-09-02 B1 §二):键从 walmart_policy 改名 category,
        # 语义从"顺手记一个政策名"变成"这条规则判的就是这个类别"
        assert h.detail["category"] == resources.AUDIT_IP_POLICY


def test_trademark_no_own_brand_exemption():
    """B3-5:无自品牌豁免——brand 字段同名照拦。"""
    r = audit_phase0.check(_p(brand="LEGO", title="LEGO® Bricks"), _ctx())
    assert r.blocked and r.hits[0].rule_code == "phase0_trademark_symbol"


def test_trademark_dedup_case_insensitive():
    """B3-6:title 与 bullets 短语相同仅大小写不同 → 按 lower 去重只记 1 条。

    注意贪婪捕获:字符类含 \\s(连换行),前文任何大写开头的连续段都会被
    吞进短语——去重场景必须用类外字符(逗号/%)隔断,否则是两个不同短语。
    """
    r = audit_phase0.check(
        _p(title="Fender® Guitar, FENDER® strap"), _ctx())
    assert r.blocked and r.hits[0].detail["count"] == 1


def test_trademark_truncation_bounds():
    """B3-7/B3-8:desc 只扫前 1000 字符;bullets 只扫前 5 条。"""
    r = audit_phase0.check(
        _p(long_description=" " * 1500 + "Nike®"), _ctx())
    assert not r.blocked
    r2 = audit_phase0.check(
        _p(bullet_points=["a", "b", "c", "d", "e", "Nike® original"]), _ctx())
    assert not r2.blocked


# ── B4 归一化 + 飞书三闸 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,norm", [
    ("Clothing, Shoes & Jewelry > Men > Shoes",
     "Clothing,Shoes&Jewelry->Men->Shoes"),                  # B4-1
    ("VideoGames->XboxOne->Games", "VideoGames->XboxOne->Games"),  # B4-2 幂等
    ("  Home & Kitchen  >  Cups  ", "Home&Kitchen->Cups"),   # B4-3
    ("Toys / Games / Blocks", "Toys->Games->Blocks"),        # B4-4 '/' 也是分隔
    ("A >> B", "A->B"),                                      # B4-5 空段丢弃
    ("Home　&　Kitchen", "Home&Kitchen"),            # B4-6 全角空格被 \s 吃
    ("Home ＞ Kitchen", "Home＞Kitchen"),            # B4-7 全角 ＞ 不是分隔符
    ("", ""), (None, ""),                                    # B4-8
])
def test_normalize_amazon_category(raw, norm):
    assert audit_phase0.normalize_amazon_category(raw) == norm


def test_lark_seller_asin_cat_priority():
    """B4-10~13:seller → asin → cat 短路;detail 结构逐键。"""
    ctx = _ctx(sellers={"A327G0VM18EU0N"}, asins={"B0BLACK0001"},
               cats={"VideoGames->Xbox"})
    r = audit_phase0.check(
        _p(asin="B0BLACK0001", seller_id="A327G0VM18EU0N"), ctx)
    assert r.hits[0].rule_code == "phase0_lark_blacklist_seller"   # B4-11 短路
    assert len(r.hits) == 1
    assert r.hits[0].detail["source"] == "blacklist_center"
    assert r.hits[0].detail["seller_name"] is None                 # 空串→None

    r2 = audit_phase0.check(_p(asin="B0BLACK0001"), ctx)
    assert r2.hits[0].rule_code == "phase0_lark_blacklist_asin"

    r3 = audit_phase0.check(
        _p(amazon_category_path="Video Games > Xbox"), ctx)
    assert r3.hits[0].rule_code == "phase0_lark_blacklist_amazon_cat"
    assert r3.hits[0].detail["amazon_category_path"] == "Video Games > Xbox"
    assert r3.hits[0].detail["matched_by"] == "path_exact"
    assert r3.matched_category == "VideoGames->Xbox"


def test_每条硬拒规则自报类别():
    """⚠ 类别**只许两种来源、零推断**:规则在 detail 里自报,或 L3 结构化输出。

    这条钉的是 §二 那张表在 L0 的四行:卖家 / ASIN 黑名单 = 内部黑名单
    (我们自己的决策,不对应任何沃尔玛政策 —— 所有者 2026-08-18 实遇
    「ASIN 在黑名单中心 [政策:General-Use Products]」那种自相矛盾的话);
    品牌黑名单 / 商标符号 / 专利自述 = Intellectual Property。
    漏了自报的后果不报错:`compute_final_reason` 查不到就落 NULL。
    """
    internal = resources.AUDIT_CAT_INTERNAL_BLACKLIST
    ctx = _ctx(sellers={"S1"}, asins={"B0BLACK0001"}, brands=BRANDS)
    assert audit_phase0.check(_p(seller_id="S1"), ctx).hits[0] \
        .detail["category"] == internal
    assert audit_phase0.check(_p(asin="B0BLACK0001"), ctx).hits[0] \
        .detail["category"] == internal
    assert audit_phase0.check(_p(brand="IKEA"), ctx).hits[0] \
        .detail["category"] == resources.AUDIT_IP_POLICY
    assert audit_phase0.check(_p(title="LEGO® Bricks"), ctx).hits[0] \
        .detail["category"] == resources.AUDIT_IP_POLICY
    assert audit_phase0.check(_p(title="Patented Shelf"), ctx).hits[0] \
        .detail["category"] == resources.AUDIT_IP_POLICY


def test_lark_blank_seller_skipped():
    """B4-14:纯空白 seller 只跳过本项,不影响后续闸。"""
    ctx = _ctx(sellers={"X"}, asins={"B0BLACK0001"})
    r = audit_phase0.check(_p(asin="B0BLACK0001", seller_id="  "), ctx)
    assert r.hits[0].rule_code == "phase0_lark_blacklist_asin"


def test_lark_category_case_sensitive():
    """B4-9:类目比较大小写敏感(归一化不 lower)。"""
    ctx = _ctx(cats={"Home&Kitchen"})
    assert not audit_phase0.check(
        _p(amazon_category_path="home & kitchen"), ctx).blocked


# ── B5 编排顺序 ──────────────────────────────────────────────────────────────

def test_phase0_ordering_lark_beats_category():
    ctx = _ctx(asins={"B0BLACK0001"})
    r = audit_phase0.check(
        _p(asin="B0BLACK0001", amazon_category_path="Books"), ctx)
    assert len(r.hits) == 1
    assert r.hits[0].rule_code == "phase0_lark_blacklist_asin"


def test_phase0_ordering_category_beats_trademark():
    r = audit_phase0.check(
        _p(amazon_category_path="Books", title="Nike® Book"),
        _ctx(tops=_LEGACY_TOPS))
    assert r.hits[0].rule_code == "phase0_forbidden_category"


def test_phase0_all_clear():
    r = audit_phase0.check(_p(title="Plain widget"), _ctx())
    assert r.blocked is False and r.hits == []


# ── B9 停用词闸 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("token,expect", [
    ("", True), (None, True), ("  ", True),      # B9-1
    ("top", True),                               # B9-2 长度<4
    ("premium", True),                           # B9-3 STOPWORDS_COMMON
    ("360", True),                               # B9-4 纯数字
    ("4x4", True),                               # B9-5
    ("Fender", False),                           # B9-6
    ("ninja foodi", False),                      # B9-7 多词有显著词
    ("top best", True),                          # B9-8 多词全停用
    ("Nike.", False),                            # B9-9 去尾标点后 4 字符
    ("NIKE", False),                             # B9-10
])
def test_is_stopword_vectors(token, expect):
    assert is_stopword(token) is expect


# ── 专利声明硬拦(规则 2.5,2026-08-19 所有者定稿新增)────────────────────────

@pytest.mark.parametrize("title,blocked", [
    ("Yociyoga 4-Tier Closet Organizer (Patent Protection)"
     "(Patent No. 30022416)", True),                        # 实证原型
    ("Patented Ergonomic Design Pillow", True),
    ("Storage Rack, Patent Pending", True),
    ("Covered by US Patents 9,876,543", True),
    ("Women's Patent Leather Handbag", False),              # 漆皮=材质,豁免
    ("Patent-Leather Loafers", False),                      # 连字符变体也豁免
    ("Plain Storage Shelf Black", False),
])
def test_patent_claim_vectors(title, blocked):
    r = audit_phase0.check(_p(title=title), _ctx())
    assert r.blocked is blocked, title
    if blocked:
        h = r.hits[0]
        assert h.rule_code == "phase0_patent_claim" and h.penalty == -100
        assert h.detail["category"] == resources.AUDIT_IP_POLICY


def test_patent_scans_bullets_and_desc_same_window_as_trademark():
    """与商标规则同一扫描面:bullets 前 5 条、desc 前 1000 字符。"""
    r = audit_phase0.check(
        _p(title="Shelf", bullet_points=["sturdy", "patented latch design"]),
        _ctx())
    assert r.blocked and r.hits[0].rule_code == "phase0_patent_claim"
    # 第 6 条 bullet 之外的 patent 不扫(窗口截断,与商标规则同款)
    r2 = audit_phase0.check(
        _p(title="Shelf", bullet_points=["a", "b", "c", "d", "e",
                                         "patented design"]),
        _ctx())
    assert r2.blocked is False
