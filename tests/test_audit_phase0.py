"""Phase0 四件套判定向量(spec_vectors B1~B5/B9;旧仓判定层零测试,全部新钉)。

向量出处:批次 B 移植规格(四路取证),每条对应旧仓实现行号——断言的是
"与旧仓逐字节同判",不是"理想行为"。已知缺陷(B2-5 漏拦/B2-6 口径不一/
全角不折叠)按现状钉死,修复须先过所有者。
"""

from types import SimpleNamespace

import pytest

from services import audit_phase0
from services.audit_models import ProductInfo
from services.audit_stopwords import is_stopword


def _ctx(sellers=(), asins=(), cats=(), brands=None):
    return SimpleNamespace(
        phase0_sellers=frozenset(sellers),
        phase0_asins=frozenset(asins),
        phase0_cats=frozenset(cats),
        brand_blacklist=dict(brands or {}),
    )


def _p(**kw):
    kw.setdefault("asin", "B000TEST00")
    return ProductInfo(**kw)


# ── B1 品牌精准黑名单 ─────────────────────────────────────────────────────────

BRANDS = {"ikea": "IKEA", "ernie ball": "Ernie Ball", "n/a": "N/A",
          "无": "无", "nexgrill": "Nexgrill"}


@pytest.mark.parametrize("brand,blocked", [
    ("IKEA", True),            # B1-1
    ("ikea", True),            # B1-2 大小写不敏感
    ("  Ernie   Ball ", True), # B1-3 空白压一
    ("N/A", False),            # B1-4 占位符优先于黑名单
    ("n / a", False),          # B1-5 变体不识别(内部空格保留)
    ("", False), ("   ", False),   # B1-7
    ("无", False),             # B1-8 中文占位符
    ("IKEA Furniture", False), # B1-9 等值非前缀
    ("Nexgrill", True),        # B1-10 yaml additional_hard_brands 同池
])
def test_brand_blacklist_vectors(brand, blocked):
    r = audit_phase0.check(_p(brand=brand), _ctx(brands=BRANDS))
    assert r.blocked is blocked
    if blocked:
        h = r.hits[0]
        assert h.rule_code == "phase0_brand_blacklist" and h.penalty == -100
        assert h.detail["source"] == "blacklist_center"
        assert h.detail["match_type"] == "exact"


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
    ("Clothing,Shoes & Jewelry > Men", False, None),             # B2-5 已知漏拦钉死
    ("Books->Kindle", False, None),                              # B2-6 只切 '>' 口径钉死
    ("Automotive Parts & Accessories", False, None),             # B2-7
    ("", False, None),                                           # B2-8
    ("Video Games > Xbox", False, None),                         # B2-9
])
def test_forbidden_category_vectors(path, blocked, top):
    r = audit_phase0.check(_p(amazon_category_path=path), _ctx())
    assert r.blocked is blocked
    if blocked:
        h = r.hits[0]
        assert h.rule_code == "phase0_forbidden_category" and h.penalty == -100
        assert h.detail["amazon_top_category"] == top
        assert h.detail["full_path"] == path
        assert h.detail["match_type"] == "exact"   # 兜底命中也写 exact(照迁)
        assert r.matched_category == top


def test_forbidden_category_policy_books():
    r = audit_phase0.check(_p(amazon_category_path="Books"), _ctx())
    assert r.hits[0].detail["walmart_policy"] == "Intellectual Property"


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
        assert h.detail["walmart_policy"] == "Intellectual Property"


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
    assert r3.hits[0].detail["normalized"] == "VideoGames->Xbox"
    assert r3.matched_category == "VideoGames->Xbox"


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
        _p(amazon_category_path="Books", title="Nike® Book"), _ctx())
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
