"""A0.5 存量审计回归:富化/冲突/画像/渠道对拍四个纯函数 + 品牌占用键口径。"""

from collections import Counter

import pytest

from services import brand_key as bk
from services import store_targets
from workflows import alloc_audit as wf

# (store, sku, product_type)
ITEMS = [
    ("A085", "B0AAAA0001", "Socks"),
    ("A107", "B0AAAA0001", "Socks"),            # 同 ASIN 跨店
    ("A085", "A109-B0BBBB0002-02", "Hats"),     # 三段式 sku
    ("A107", "B0CCCC0003", "Knives"),           # 同品牌(Acme)跨店
    ("A085", "998877665544", None),             # numeric:提不出 asin
    ("DEAD1", "B0DDDD0004", "Socks"),           # 已不在凭证表的店
]

META = {
    "B0AAAA0001": {"brand": "Acme", "manufacturer": None,
                   "walmart_pt": "Socks", "pt_source": "walmart_confirmed",
                   "fulfillment": "FBA"},
    "B0BBBB0002": {"brand": "Generic", "manufacturer": "Beta Works",
                   "walmart_pt": "Hats", "pt_source": "audit_llm",
                   "fulfillment": "FBA"},
    "B0CCCC0003": {"brand": "acme  ", "manufacturer": None,
                   "walmart_pt": "Knives", "pt_source": "audit_llm",
                   "fulfillment": "FBM"},
    "B0DDDD0004": {"brand": "N/A", "manufacturer": "none",
                   "walmart_pt": "Socks", "pt_source": None,
                   "fulfillment": None},
}

PT2CAT = {"Socks": "Fashion", "Hats": "Fashion", "Knives": "Home"}


@pytest.fixture
def enriched():
    return wf.enrich(ITEMS, META, PT2CAT)


# ── 品牌占用键(services/brand_key)──────────────────────────────────────

def test_brand_key_normalizes_and_falls_back_to_manufacturer():
    assert bk.brand_key("Acme") == bk.brand_key("acme  ") == "acme"
    assert bk.brand_key("  L'Oréal  Paris ") == "l'oréal paris"
    # brand 是占位符 → 用 manufacturer(真品牌常在这)
    assert bk.brand_key("Generic", "Beta Works") == "beta works"


def test_brand_key_none_when_all_placeholders():
    """两者皆占位符 = 真·无品牌:不占品牌,逐 ASIN 分配。

    这条是排他性的安全边:把 'OEM' 当品牌会把成千上万无关产品锁进一家店,
    而占用没有自动释放。
    """
    for b, m in (("N/A", "none"), ("unbranded", None), ("无品牌", "-"),
                 (None, None), ("", "  ")):
        assert bk.brand_key(b, m) is None


def test_placeholder_list_is_superset_of_mapper_noise():
    """占用键的噪声表必须 ⊇ 文案去品牌那份(小表漏词=误锁)。"""
    from services.mp_mapper import _BRAND_NOISE
    assert {w for w in _BRAND_NOISE if w} <= bk.PLACEHOLDERS


# ── enrich ─────────────────────────────────────────────────────────────

def test_enrich_maps_asin_brand_and_category(enriched):
    rows, st = enriched
    by_sku = {r["sku"]: r for r in rows}
    assert by_sku["B0AAAA0001"]["asin"] == "B0AAAA0001"
    # 三段式 sku 取中段源头码(services/sku_asin 唯一规则)
    assert by_sku["A109-B0BBBB0002-02"]["asin"] == "B0BBBB0002"
    assert by_sku["A109-B0BBBB0002-02"]["brand_key"] == "beta works"
    assert by_sku["B0AAAA0001"]["category"] == "Fashion"
    assert by_sku["B0CCCC0003"]["channel"] == "FBM"


def test_enrich_counts_unresolvable_sku(enriched):
    rows, st = enriched
    assert st["no_asin"] == 1 and st["form_numeric"] == 1
    assert next(r for r in rows if r["sku"] == "998877665544")["asin"] is None
    assert st["online"] == len(ITEMS)


def test_enrich_falls_back_to_product_pt_for_category():
    """在线 PT 缺失时用产品审核 PT 兜底(主路/兜底两跳,设计稿 §十二.10)。"""
    rows, _ = wf.enrich([("A085", "B0AAAA0001", None)], META, PT2CAT)
    assert rows[0]["category"] == "Fashion"


def test_enrich_counts_products_without_brand(enriched):
    _, st = enriched
    assert st["no_brand"] == 1          # B0DDDD0004 两字段皆占位符


# ── 冲突与画像 ─────────────────────────────────────────────────────────

def test_cross_store_asin_and_brand(enriched):
    rows, _ = enriched
    a1 = wf.cross_store(rows, "asin")
    assert [k for k, _ in a1] == ["B0AAAA0001"]
    assert a1[0][1] == {"A085": 1, "A107": 1}
    # acme 在 A085(B0AAAA0001)与 A107(两条)——归一化后同键才看得出来
    brands = dict(wf.cross_store(rows, "brand_key"))
    assert set(brands["acme"]) == {"A085", "A107"}


def test_cross_store_ignores_none_keys():
    rows = [{"store": "A", "asin": None, "brand_key": None},
            {"store": "B", "asin": None, "brand_key": None}]
    assert wf.cross_store(rows, "asin") == []
    assert wf.cross_store(rows, "brand_key") == []


def test_store_profiles_counts_categories_and_channels(enriched):
    rows, _ = enriched
    prof = wf.store_profiles(rows)
    assert prof["A085"]["n"] == 3
    assert prof["A085"]["categories"]["Fashion"] == 2
    assert prof["A085"]["categories"]["(未归类)"] == 1     # numeric sku 那行
    assert prof["A107"]["channels"] == Counter({"FBA": 1, "FBM": 1})


def test_channel_mismatch_only_for_configured_stores(enriched):
    rows, _ = enriched
    prof = wf.store_profiles(rows)
    cfg = {"A107": {"channel": "FBA"}, "A085": {"channel": None}}
    mism = wf.channel_mismatch(prof, cfg)
    assert [m[0] for m in mism] == ["A107"]        # A085 未填限制,不对拍
    assert mism[0][1] == "FBA" and mism[0][2] == 1  # 一件 FBM 不符


def test_channel_mismatch_does_not_blame_unknown_channel():
    """渠道没采到 ≠ 货不对:不能把无辜商品混进下架清单。"""
    prof = {"S": {"n": 2, "categories": Counter(),
                  "channels": Counter({"FBA": 1, "(未知)": 1})}}
    assert wf.channel_mismatch(prof, {"S": {"channel": "FBA"}}) == []


# ── 店铺配置 ───────────────────────────────────────────────────────────

def test_missing_config_lists_unfilled_columns():
    cfg = {"A085": {"gmv": 100.0, "orders": 3.0, "max_online": 500.0,
                    "channel": "FBA", "channel_raw": "fba"},
           "A107": {"gmv": None, "orders": None, "max_online": None,
                    "channel": None, "channel_raw": ""}}
    miss = store_targets.missing_config(cfg, ["A085", "A107", "NEW1"])
    assert "A085" not in miss
    assert miss["A107"] == ["配送限制", "单店最大在线数", "目标销售额", "目标订单"]
    assert miss["NEW1"] == ["未在限额表登记"]


def test_missing_config_flags_unrecognized_channel_value():
    cfg = {"S": {"gmv": 1.0, "orders": 1.0, "max_online": 1.0,
                 "channel": None, "channel_raw": "海外仓"}}
    assert store_targets.missing_config(cfg, ["S"])["S"] == [
        "配送限制(填了「海外仓」认不出)"]


def test_targets_blank_is_none_not_zero():
    """未填 ≠ 目标为零:退化成 0 会让该店缺口恒为 0 而永远分不到货。"""
    assert store_targets._num("") is None
    assert store_targets._num("  ") is None
    assert store_targets._num("abc") is None
    assert store_targets._num("1,200.50") == 1200.5
    assert store_targets._num(0) == 0.0        # 真填了 0 要保留


def test_channel_value_normalization():
    assert store_targets._channel("fba") == ("FBA", "fba")
    assert store_targets._channel(" FBM ") == ("FBM", "FBM")
    assert store_targets._channel("海外仓") == (None, "海外仓")
    assert store_targets._channel("") == (None, "")
