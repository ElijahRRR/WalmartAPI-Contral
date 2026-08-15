"""A0.5 存量审计回归:富化/冲突/画像/渠道对拍四个纯函数 + 品牌占用键口径。

含 2026-08-15 对抗式审查后补的护栏:SQL 列名字面量、排序可复现、
白名单判渠道、未归类不参与类目计数。
"""

from collections import Counter

import pytest

from services import brand_key as bk
from services import store_targets
from services import alloc_survey as sv
from workflows import alloc_audit as wf

# (store, sku, product_type, published_status)
ITEMS = [
    ("A085", "B0AAAA0001", "Socks", "PUBLISHED"),
    ("A107", "B0AAAA0001", "Socks", "PUBLISHED"),        # 同 ASIN 跨店
    ("A085", "A109-B0BBBB0002-02", "Hats", "PUBLISHED"),  # 三段式 sku
    ("A107", "B0CCCC0003", "Knives", "UNPUBLISHED"),      # 同品牌(Acme)跨店
    ("A085", "998877665544", None, "PUBLISHED"),          # numeric:提不出 asin
    ("DEAD1", "B0DDDD0004", "Socks", "PUBLISHED"),        # 已不在凭证表的店
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
    return sv.enrich(ITEMS, META, PT2CAT)


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


def test_placeholder_list_is_superset_of_both_upstreams():
    """占用键的噪声表必须 ⊇ 两个上游表(任一新增词都要先红)。

    漏一个词 = 一次大面积误锁(该词名下所有产品被锁进一家店,且占用无自动
    释放);多一个词只是少占一个品牌。方向明确,取并集。
    """
    from services.audit_phase0 import _NON_BRAND_PLACEHOLDERS as P0
    from services.mp_mapper import _BRAND_NOISE
    assert P0 <= bk.PLACEHOLDERS
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
    """在线 PT 缺失时用产品审核 PT 兜底(主路/兜底两跳,设计稿 §十二.10)。

    两条来源必须分开计数:兜底那部分可能是 LLM 推断的 PT,开新类目时
    不能当实证用(§十二.14⑥)。
    """
    rows, st = sv.enrich([("A085", "B0AAAA0001", None, "PUBLISHED")],
                         META, PT2CAT)
    assert rows[0]["category"] == "Fashion"
    assert rows[0]["cat_source"] == "product"
    assert st["cat_from_product"] == 1 and st["cat_from_item"] == 0


def test_enrich_marks_published(enriched):
    rows, _ = enriched
    by_sku = {r["sku"]: r for r in rows}
    assert by_sku["B0AAAA0001"]["published"] is True
    assert by_sku["B0CCCC0003"]["published"] is False


def test_enrich_counts_unrecognized_channel():
    """FBA/FBM 之外的渠道值单列计数——它是采集侧解析坏了的信号,不是货不对。"""
    meta = {"B0AAAA0001": {"brand": "X", "manufacturer": None,
                           "walmart_pt": "Socks", "pt_source": None,
                           "fulfillment": "海外仓"}}
    rows, st = sv.enrich([("A085", "B0AAAA0001", "Socks", "PUBLISHED")],
                         meta, PT2CAT)
    assert st["channel_weird"] == 1 and rows[0]["channel"] == "海外仓"


def test_enrich_counts_products_without_brand(enriched):
    _, st = enriched
    assert st["no_brand"] == 1          # B0DDDD0004 两字段皆占位符


# ── 冲突与画像 ─────────────────────────────────────────────────────────

def test_cross_store_asin_and_brand(enriched):
    rows, _ = enriched
    a1 = sv.cross_store(rows, "asin")
    assert [k for k, _ in a1] == ["B0AAAA0001"]
    assert a1[0][1] == {"A085": 1, "A107": 1}
    # acme 在 A085(B0AAAA0001)与 A107(两条)——归一化后同键才看得出来
    brands = dict(sv.cross_store(rows, "brand_key"))
    assert set(brands["acme"]) == {"A085", "A107"}


def test_cross_store_ignores_none_keys():
    rows = [{"store": "A", "asin": None, "brand_key": None},
            {"store": "B", "asin": None, "brand_key": None}]
    assert sv.cross_store(rows, "asin") == []
    assert sv.cross_store(rows, "brand_key") == []


def test_cross_store_ordering_is_reproducible():
    """三级排序(店铺数→件数→键名)决定所有者看到哪 sample 条,必须可复现。"""
    rows = (
        [{"store": s, "asin": "B0THREE", "brand_key": None} for s in "ABC"]
        + [{"store": "A", "asin": "B0BIG", "brand_key": None}] * 5
        + [{"store": "B", "asin": "B0BIG", "brand_key": None}] * 4
        + [{"store": "A", "asin": "B0SMALL", "brand_key": None},
           {"store": "B", "asin": "B0SMALL", "brand_key": None}]
        # 与 B0SMALL 店铺数、件数全同 → 只能按键名定序
        + [{"store": "A", "asin": "B0ALPHA", "brand_key": None},
           {"store": "B", "asin": "B0ALPHA", "brand_key": None}]
    )
    assert [k for k, _ in sv.cross_store(rows, "asin")] == [
        "B0THREE",      # 3 店
        "B0BIG",        # 2 店 9 件
        "B0ALPHA",      # 2 店 2 件,键名靠前
        "B0SMALL",
    ]


def test_store_profiles_counts_categories_and_channels(enriched):
    rows, _ = enriched
    prof = sv.store_profiles(rows)
    assert prof["A085"]["n"] == 3
    assert prof["A085"]["published"] == 3
    assert prof["A107"]["published"] == 1                  # 一条 UNPUBLISHED
    assert prof["A085"]["categories"]["Fashion"] == 2
    assert prof["A085"]["categories"][sv.UNCLASSIFIED] == 1  # numeric sku 那行
    assert prof["A107"]["channels"] == Counter({"FBA": 1, "FBM": 1})


def test_real_cats_excludes_unclassified(enriched):
    """未归类不是一个大类:它进了计数,"超 2 类"的判定与排序就都偏了。"""
    rows, _ = enriched
    prof = sv.store_profiles(rows)
    assert sv.real_cats(prof["A085"]) == ["Fashion"]
    assert len(prof["A085"]["categories"]) == 2            # 含未归类占位


def test_channel_mismatch_only_for_configured_stores(enriched):
    rows, _ = enriched
    prof = sv.store_profiles(rows)
    cfg = {"A107": {"channel": "FBA"}, "A085": {"channel": None}}
    mism = sv.channel_mismatch(prof, cfg)
    assert [m[0] for m in mism] == ["A107"]        # A085 未填限制,不对拍
    assert mism[0][1] == "FBA" and mism[0][2] == 1  # 一件 FBM 不符


def test_channel_mismatch_does_not_blame_unknown_channel():
    """渠道没采到 ≠ 货不对:不能把无辜商品混进下架清单。"""
    prof = {"S": {"n": 2, "categories": Counter(),
                  "channels": Counter({"FBA": 1, sv.UNKNOWN_CHANNEL: 1})}}
    assert sv.channel_mismatch(prof, {"S": {"channel": "FBA"}}) == []


def test_channel_mismatch_is_whitelist_not_blacklist():
    """认不出的第三种值同样不算不符——那是采集解析坏了,不是货不对。"""
    prof = {"S": {"n": 3, "categories": Counter(),
                  "channels": Counter({"FBA": 1, "海外仓": 5, "FBM": 2})}}
    out = sv.channel_mismatch(prof, {"S": {"channel": "FBA"}})
    assert out[0][2] == 2                  # 只有 FBM 那 2 件算不符,海外仓不算


def test_channel_mismatch_sort_has_tiebreak():
    """件数相同的店按店名定序,否则两次跑的样例清单不同。"""
    prof = {s: {"n": 1, "categories": Counter(), "channels": Counter({"FBM": 1})}
            for s in ("S2", "S1", "S3")}
    cfg = {s: {"channel": "FBA"} for s in prof}
    assert [m[0] for m in sv.channel_mismatch(prof, cfg)] == ["S1", "S2", "S3"]


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


# ── SQL 字面量护栏(反推表的列名错误单测抓不到,只有生产跑才炸)────────────

def test_pt_meta_column_name_in_sql():
    """audit.walmart_pt_meta 的主键列是 walmart_product_type。

    2026-08-15 实测教训:写成 m.product_type 会 UndefinedColumn,而它是
    run() 的第三条 SQL —— 整份报告一行都出不来(P1/P2 已查的结果一并丢)。
    """
    assert "m.walmart_product_type" in sv._SQL_PT_DICT
    assert "m.product_type" not in sv._SQL_PT_DICT


def test_online_query_is_ordered_and_carries_published():
    """固定 ORDER BY:样例截断要可复现;带 published_status:下架判定只看已发布。"""
    assert "ORDER BY store, sku" in sv._SQL_ONLINE
    assert "published_status" in sv._SQL_ONLINE


def test_pool_query_gates_on_category_lookup():
    """P1 末层必须过大类关——查不到大类的产品过不了「一店两大类」硬闸。"""
    assert "risk_product_types" in sv._SQL_POOL and "with_cat" in sv._SQL_POOL


# ── run() 端到端冒烟(报告拼装路径此前零覆盖:未定义变量只有生产跑才炸)──

class _FakeCur:
    """按 SQL 特征串返回预设结果;description 供 dict(zip(...)) 用。"""

    def __init__(self, fail_pt_dict=False):
        self.fail_pt_dict = fail_pt_dict
        self._no_sales = False
        self.description = []
        self._rows: list = []

    def execute(self, sql, args=None):
        def desc(*names):
            self.description = [(n,) for n in names]

        if "AS total" in sql:
            desc("total", "approved", "with_title", "with_pt", "pt_evid",
                 "with_cat", "with_brand")
            self._rows = [(1_280_000, 178_000, 170_000, 168_000, 90_000,
                           160_000, 120_000)]
        elif "n_rating" in sql:
            desc("n", "n_rating", "n_review", "n_fba")
            self._rows = [(50_000, 0, 0, 49_000)]
        elif "n_risk" in sql:
            if self.fail_pt_dict:
                raise RuntimeError("column m.product_type does not exist")
            desc("n_risk", "n_risk_cat", "n_meta", "only_risk", "only_meta",
                 "cat_diff")
            self._rows = [(7008, 7000, 7033, 12, 37, 5)]
        elif "GROUP BY 1 ORDER BY 2 DESC" in sql:
            self._rows = [("Fashion", 900), ("Home", 800)]
        elif "product_type, btrim" in sql:
            self._rows = [("Socks", "Fashion"), ("Hats", "Fashion"),
                          ("Knives", "Home")]
        elif "FROM catalog.walmart_items" in sql:
            self._rows = list(ITEMS)
        elif "store_kpi_daily" in sql:
            self._rows = [("A085", "ACTIVE"), ("A107", ""), ("DEAD1", "ACTIVE")]
        elif "FROM orders.order_lines" in sql:
            # A085 的那件卖过,A107 的同款没卖过 → 冲突留 A085
            self._rows = [] if self._no_sales else [("A085", "B0AAAA0001", 3, 120.0)]
        elif "FROM catalog.products p" in sql:
            self._rows = [(a, m["brand"], m["manufacturer"], m["walmart_pt"],
                           m["pt_source"], m["fulfillment"])
                          for a, m in META.items()]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.rolled_back = False

    def cursor(self):
        return self._cur

    def rollback(self):
        self.rolled_back = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire(monkeypatch, cur, *, registered=None, stores=None, cfg=None,
          reports=None):
    import contextlib
    conn = _FakeConn(cur)
    monkeypatch.setattr(wf.db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([conn])))
    if reports is not None:
        monkeypatch.setattr(wf.paths, "reports_dir", lambda: reports)
    monkeypatch.setattr(wf.stores_svc, "registered_names",
                        lambda: registered if registered is not None else {"A085", "A107"})
    monkeypatch.setattr(wf.stores_svc, "load_stores",
                        lambda: [{"name": n} for n in (stores if stores is not None
                                                       else ["A085", "A107"])])
    monkeypatch.setattr(wf.store_targets, "load_targets",
                        lambda: cfg if cfg is not None else {
                            "A085": {"gmv": 100.0, "orders": 3.0,
                                     "max_online": 500.0, "channel": "FBA",
                                     "channel_raw": "fba",
                                     "categories": ["Fashion"]},
                            "A107": {"gmv": None, "orders": None,
                                     "max_online": None, "channel": None,
                                     "channel_raw": "", "categories": []}})
    return conn


def test_run_end_to_end_report(monkeypatch):
    cur = _FakeCur()
    _wire(monkeypatch, cur)
    out = wf.run({})
    # 七节都在
    for tag in ("P1 候选池", "P2 打分信号", "P3 PT 字典对拍", "P4 品牌占用键",
                "A0 在线行", "A1 同 ASIN", "A2 同品牌", "A3 每店大类",
                "A4 不在册店冻结行", "A5 渠道对拍", "A6 店铺配置", "A7 店铺状态"):
        assert tag in out, tag
    # 评分/评论探针为 0 → 必须点名删权重(禁止 or 0)
    assert "禁止 or 0" in out
    # DEAD1 不在册:其行被排除出冲突,且在 A4 被点名
    assert "已排除不在册店的冻结行 1 行 / 1 家店" in out
    assert "DEAD1×1" in out
    # A7:A107 状态为空串 → fail-open 视同 ACTIVE,不进非 ACTIVE 清单
    assert "非 ACTIVE 的 0 家" in out


def test_run_degrades_when_pt_dict_fails(monkeypatch):
    """P3 是对拍探针,炸了要降级成一行提示,不能拖垮整份存量审计。"""
    cur = _FakeCur(fail_pt_dict=True)
    conn = _wire(monkeypatch, cur)
    out = wf.run({})
    assert "P3 PT 字典对拍:跳过" in out
    assert conn.rolled_back is True          # 事务 aborted 必须先回滚
    assert "A1 同 ASIN" in out and "A7 店铺状态" in out


def test_run_marks_channel_skip_instead_of_reporting_zero(monkeypatch):
    """-p channel=0 时 A5 必须明说跳过——打印"不符 0 家"读起来像全店合规。"""
    cur = _FakeCur()
    _wire(monkeypatch, cur)
    out = wf.run({"channel": "0"})
    assert "A5 渠道对拍:**跳过**" in out


def test_run_exports_four_lists(monkeypatch, tmp_path):
    """C 段四份处置清单要真落盘,且内容是"照着能做"的逐行明细。"""
    cur = _FakeCur()
    _wire(monkeypatch, cur, reports=tmp_path)
    out = wf.run({})

    names = sorted(p.name for p in tmp_path.glob("*.csv"))
    assert names == ["alloc_同ASIN冲突处置.csv", "alloc_同品牌冲突处置.csv",
                     "alloc_渠道不符下架清单.csv", "alloc_类目建议.csv"]
    assert "C1 类目建议" in out and "C2 渠道不符" in out
    assert "C3 同 ASIN 跨店" in out and "C4 同品牌跨店" in out

    # C1:A085 在线 Fashion×2 → 建议 Fashion;表格已填 Fashion → 一致
    c1 = (tmp_path / "alloc_类目建议.csv").read_text(encoding="utf-8-sig")
    assert "A085" in c1 and "一致" in c1
    assert "A107" in c1 and "未填" in c1

    # C3:A085 卖过 120 元、A107 零销量 → 留 A085,A107 那行判下架
    c3 = (tmp_path / "alloc_同ASIN冲突处置.csv").read_text(encoding="utf-8-sig")
    lines = [ln for ln in c3.splitlines() if "B0AAAA0001" in ln]
    assert any(ln.startswith("B0AAAA0001,A085") and ln.endswith("保留")
               for ln in lines)
    assert any(",A107," in ln and ln.endswith("下架") for ln in lines)


def test_run_export_off(monkeypatch, tmp_path):
    cur = _FakeCur()
    _wire(monkeypatch, cur, reports=tmp_path)
    out = wf.run({"export": "0"})
    assert "C 处置清单:跳过" in out
    assert list(tmp_path.glob("*.csv")) == []


def test_run_flags_missing_order_history(monkeypatch, tmp_path):
    """订单没导入时销量全零,冲突清单只能打平——必须明说,不能装作判过了。"""
    cur = _FakeCur()
    cur._no_sales = True
    _wire(monkeypatch, cur, reports=tmp_path)
    out = wf.run({})
    assert "订单历史还没导入" in out


def test_run_warns_when_credential_table_unreadable(monkeypatch):
    """凭证表读不到时不能静默不排除冻结行——那正是 A1 被灌水的场景。"""
    cur = _FakeCur()
    _wire(monkeypatch, cur)

    def _boom():
        raise RuntimeError("飞书 500")
    monkeypatch.setattr(wf.stores_svc, "registered_names", _boom)
    out = wf.run({})
    assert "本轮未排除已不在册店的冻结行" in out
    assert "A4 不在册店冻结行:跳过" in out
