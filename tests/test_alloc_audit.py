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
        self.extra_items: list = []
        self.status = [("A085", "ACTIVE"), ("A107", ""), ("DEAD1", "ACTIVE")]
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
            self._rows = list(ITEMS) + list(self.extra_items)
        elif "store_kpi_daily" in sql:
            self._rows = list(self.status)
        elif "GROUP BY store ORDER BY n DESC" in sql:      # A8 订单店名对账
            self._rows = [("A085", 30, 30), ("旧名店", 5, 5)]
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
    # 六节都在(报告以"要你做的事"开头——它是给人动手用的,不是给人读的)
    for tag in ("▍要你做的事", "▍数据体检", "▍在线商品", "▍冲突",
                "▍店铺", "▍明细"):
        assert tag in out, tag
    assert out.index("▍要你做的事") < out.index("▍数据体检")
    # 评分/评论探针为 0 → 必须点名删权重(禁止 or 0)
    assert "评分 ✗" in out and "v1 权重要删掉这两项" in out
    # DEAD1 不在册:其行被排除出冲突,且被点名
    assert "不在册店冻结 1 家(DEAD1)" in out
    # A107 状态为空串 → fail-open 视同 ACTIVE,不进非 ACTIVE 清单
    assert "非 ACTIVE" not in out
    # 订单里的"旧名店"不在凭证表 → 点名(它的销量进不了店×类目维度)
    assert "订单店名对不上 1 家 5 行" in out and "旧名店 5" in out


def test_report_columns_align_by_display_width():
    """列宽按**显示宽度**补,不按字符数——中文占 2 格,按字符补列就是歪的。

    这正是所有者说"看不明白"的那一半原因:数字列对不齐,一屏数字糊成一片。
    """
    assert wf._width("同品牌") == 6 and wf._width("同 ASIN") == 7
    lines = wf._grid([("同品牌", "1 组", "依据:x"),
                      ("同 ASIN", "12 组", "依据:y")])
    # 判据:两行"依据"之前的**显示宽度**相等(字符数则不等——正是老写法的错)
    assert len({wf._width(ln.split("依据")[0]) for ln in lines}) == 1
    assert len({len(ln.split("依据")[0]) for ln in lines}) == 2
    assert wf._grid([]) == []                      # 空表不产出空行


def test_run_degrades_when_pt_dict_fails(monkeypatch):
    """PT 字典是对拍探针,炸了要降级成一行提示,不能拖垮整份存量审计。"""
    cur = _FakeCur(fail_pt_dict=True)
    conn = _wire(monkeypatch, cur)
    out = wf.run({})
    assert "PT 字典" in out and "对拍跳过" in out
    assert conn.rolled_back is True          # 事务 aborted 必须先回滚
    assert "▍冲突" in out and "▍店铺" in out


def test_run_marks_channel_skip_instead_of_reporting_zero(monkeypatch, tmp_path):
    """-p channel=0 时必须明说跳过——打印"不符 0 件"读起来像全店合规。

    而且这一轮**不能写**下架清单 csv:offenders 必空,照写会把上一轮跑出来的
    清单覆盖成空表,所有者照着空表下架 = 什么都不下,还看不出是被本轮清掉的。
    """
    cur = _FakeCur()
    _wire(monkeypatch, cur, reports=tmp_path)
    stale = tmp_path / "alloc_渠道不符下架清单.csv"
    stale.write_text("上一轮的清单", encoding="utf-8-sig")
    out = wf.run({"channel": "0"})
    assert "下架渠道不符  本轮没查" in out and "**跳过**" in out
    assert "0 件" not in out
    assert stale.read_text(encoding="utf-8-sig") == "上一轮的清单"
    assert "alloc_渠道不符下架清单.csv" not in out.split("▍明细")[-1]


def test_run_marks_channel_skip_when_limits_table_unreadable(monkeypatch):
    """限额表读不到时同理:没有配送限制可对拍,不能拿 0 冒充"全合规"。"""
    cur = _FakeCur()
    _wire(monkeypatch, cur)

    def _boom():
        raise RuntimeError("飞书 500")
    monkeypatch.setattr(wf.store_targets, "load_targets", _boom)
    out = wf.run({})
    assert "填类目三列    本轮没查" in out and "下架渠道不符  本轮没查" in out
    assert "限额表读不到" in out


def test_run_exports_five_lists(monkeypatch, tmp_path):
    """五份明细要真落盘,且内容是"照着能做"的逐行明细。

    控制台只留计数,所以 csv 是唯一能照着动手的东西——少一份就等于那件事
    没法做。
    """
    cur = _FakeCur()
    _wire(monkeypatch, cur, reports=tmp_path)
    out = wf.run({})

    names = sorted(p.name for p in tmp_path.glob("*.csv"))
    assert set(names) == {"alloc_类目建议.csv", "alloc_渠道不符下架清单.csv",
                          "alloc_店铺总览.csv", "alloc_同ASIN冲突处置.csv",
                          "alloc_同品牌冲突处置.csv"}
    for name in names:                       # 每份都在控制台列了名
        assert name in out

    # 类目建议:A085 在线 Fashion×2 → 建议 Fashion;表格已填 Fashion → 一致
    c1 = (tmp_path / "alloc_类目建议.csv").read_text(encoding="utf-8-sig")
    assert "A085" in c1 and "一致" in c1
    assert "A107" in c1 and "未填" in c1

    # 同 ASIN:A085 卖过 120 元、A107 零销量 → 留 A085,A107 那行判下架
    c3 = (tmp_path / "alloc_同ASIN冲突处置.csv").read_text(encoding="utf-8-sig")
    lines = [ln for ln in c3.splitlines() if "B0AAAA0001" in ln]
    assert any(ln.startswith("B0AAAA0001,A085") and ln.endswith("保留")
               for ln in lines)
    assert any(",A107," in ln and ln.endswith("下架") for ln in lines)


def test_store_overview_csv_carries_the_per_store_detail(monkeypatch, tmp_path):
    """控制台只报计数,逐店明细全在这份 csv——包括规划外/不在册的点名。"""
    cur = _FakeCur()
    cur.extra_items = [("谭总4", "B0CCCC0003", "Knives", "PUBLISHED")]
    _wire(monkeypatch, cur, registered={"A085", "A107", "谭总4"},
          reports=tmp_path)          # DEAD1 故意不在册
    wf.run({})
    text = (tmp_path / "alloc_店铺总览.csv").read_text(encoding="utf-8-sig")
    rows = {ln.split(",")[0]: ln for ln in text.splitlines()[1:]}
    assert rows["谭总4"].split(",")[1] == "否(规划外)"
    assert rows["DEAD1"].split(",")[1] == "否(不在册)"
    assert rows["A085"].split(",")[1] == "是"
    assert "Fashion" in rows["A085"]          # 准入类目/大类分布都在行里


def test_run_export_off(monkeypatch, tmp_path):
    cur = _FakeCur()
    _wire(monkeypatch, cur, reports=tmp_path)
    out = wf.run({"export": "0"})
    assert "未落明细 csv" in out
    assert list(tmp_path.glob("*.csv")) == []


def test_run_flags_missing_order_history(monkeypatch, tmp_path):
    """订单没导入时销量全零,冲突清单只能打平——必须明说,不能装作判过了。

    csv 每行都写着"判定依据",不说破的话看起来就像真按销量判过了。
    """
    cur = _FakeCur()
    cur._no_sales = True
    _wire(monkeypatch, cur, reports=tmp_path)
    out = wf.run({})
    assert "订单历史还没导入" in out and "打平" in out
    assert "✓ 全部有销量/类目依据" not in out   # 不能同时说"判得了"


def test_run_warns_when_credential_table_unreadable(monkeypatch):
    """凭证表读不到时不能静默不排除冻结行——那正是冲突数被灌水的场景。"""
    cur = _FakeCur()
    _wire(monkeypatch, cur)

    def _boom():
        raise RuntimeError("飞书 500")
    monkeypatch.setattr(wf.stores_svc, "registered_names", _boom)
    out = wf.run({})
    assert "凭证表读不到" in out and "幻影店一个没排" in out
    assert "只可参考" in out


def test_sales_sql_excludes_cancelled_not_missing_raw_key():
    """销量口径按 #35 的落库事实校准:历史行没有 raw,按 raw 过滤等于没写。

    历史导入行 sale_status 一律 'Delivered',API 行才有真实状态 ——
    所以排除条件只能落在 sale_status 上。
    """
    assert "sale_status" in sv._SQL_SALES and "Cancelled" in sv._SQL_SALES
    assert "统计状态" not in sv._SQL_SALES      # 该列根本没被导入


# ── 规划范围外的店(所有者定稿 2026-08-15:店名含「谭总」)────────────────

def test_excluded_stores_are_matched_by_substring(monkeypatch):
    assert sv.is_excluded("谭总4") and sv.is_excluded("X谭总Y")
    assert not sv.is_excluded("A085朱丽霖")
    monkeypatch.setenv("ALLOC_EXCLUDE_STORES", "测试店,foo")
    assert sv.is_excluded("测试店1") and sv.is_excluded("FOObar") is False
    assert sv.is_excluded("foo仓")
    monkeypatch.setenv("ALLOC_EXCLUDE_STORES", "")
    assert not sv.is_excluded("谭总4")        # 显式置空 = 谁都不排除


def test_run_drops_out_of_scope_stores_from_conflicts(monkeypatch, tmp_path):
    """范围外店的在线行不进冲突:别的店可以与它们重复上同一品牌/产品。"""
    cur = _FakeCur()
    cur.extra_items = [("谭总4", "B0AAAA0001", "Socks", "PUBLISHED")]
    _wire(monkeypatch, cur, registered={"A085", "A107", "谭总4"}, reports=tmp_path)
    out = wf.run({})
    assert "规划外 1 家 1 行(谭总4)" in out
    assert "销量仍计入" in out          # 排除的是归属不是数据
    # 同 ASIN 仍只算 A085/A107 两家,谭总4 那条不参与
    c3 = (tmp_path / "alloc_同ASIN冲突处置.csv").read_text(encoding="utf-8-sig")
    assert "谭总4" not in c3


def test_category_admission_is_a_hard_gate_before_the_ladder():
    """类目先定、冲突后判:店不准入该大类,货就必须离开它,与销量无关。"""
    rows = [
        {"store": "A085", "sku": "S1", "asin": "B0A", "brand_key": "acme",
         "category": "Home", "published": True, "pt": "Socks", "pt_source": None},
        {"store": "A107", "sku": "S2", "asin": "B0A", "brand_key": "acme",
         "category": "Home", "published": True, "pt": "Socks", "pt_source": None},
    ]
    # A107 卖得更好,但它的准入类目里没有 Home → 仍然判给 A085
    sales = {("A107", "S2"): (9, 999.0)}
    cfg = {"A085": {"categories": ["Home"]}, "A107": {"categories": ["Office"]}}
    key, keep, _st, detail, level = sv.resolve_conflicts(
        rows, sales, "asin", sv.store_metrics(rows, sales), cfg)[0]
    assert keep == "A085" and level == sv.BY_CATEGORY
    assert [d[7] for d in detail] == ["保留", "下架"]


def test_category_gate_skipped_when_nobody_admits():
    """两家都不准入时不作数——那说明这批货哪家都不该有,交给人看,
    不能因为"都不合规"就随便留一家。"""
    rows = [
        {"store": "A", "sku": "S1", "asin": "B0A", "brand_key": None,
         "category": "Home", "published": True, "pt": None, "pt_source": None},
        {"store": "B", "sku": "S2", "asin": "B0A", "brand_key": None,
         "category": "Home", "published": True, "pt": None, "pt_source": None},
    ]
    cfg = {"A": {"categories": ["Office"]}, "B": {"categories": ["Office"]}}
    (key, keep, _st, _d, level) = sv.resolve_conflicts(
        rows, {}, "asin", ({}, {}), cfg)[0]
    assert level != sv.BY_CATEGORY          # 退回阶梯,不是类目说了算


# ── 非 ACTIVE = 当全新店;参不参与分配另有开关(所有者定稿 2026-08-15 晚)──

def test_accepts_allocation_keeps_zero_and_unfilled_apart():
    """0 = 所有者按下"不接货";None = 还没填。压成两态会让没填的店永远分不到货。"""
    assert store_targets.accepts_allocation({"max_online": 0}) is False
    assert store_targets.accepts_allocation({"max_online": 0.0}) is False
    assert store_targets.accepts_allocation({"max_online": 500}) is True
    assert store_targets.accepts_allocation({"max_online": None}) is None
    assert store_targets.accepts_allocation(None) is None
    assert store_targets.accepts_allocation({}) is None


def test_opted_out_store_is_not_nagged_for_the_other_columns():
    """填了 0 的店不接货,配送限制/目标三列填不填都无所谓,别再点名它。"""
    cfg = {"关店": {"max_online": 0.0, "channel": None, "gmv": None,
                    "orders": None, "categories": []},
           "在营": {"max_online": None, "channel": None, "gmv": None,
                    "orders": None, "categories": []}}
    miss = store_targets.missing_config(cfg, ["关店", "在营"])
    assert "关店" not in miss
    assert set(miss["在营"]) == {"配送限制", "单店最大在线数", "目标销售额", "目标订单"}


def test_run_still_counts_a_suspended_store_in_conflicts(monkeypatch, tmp_path):
    """SUSPENDED 店的在线行**照常进冲突** —— 「暂停不释放、占用保持」(§六.2)。

    2026-08-15 晚一度被实现成"当作全新店、不进冲突",所有者当即纠正。
    放它出冲突等于宣布它手上的货没人占:别店可以照同一个品牌上架,而占用
    没有自动释放,店恢复之后拿不回来。停用只是暂时不给它分新货,那件事的
    开关是「单店最大在线数」填 0(与占用归属无关)。
    """
    cur = _FakeCur()
    cur.extra_items = [("停用店", "B0AAAA0001", "Socks", "PUBLISHED")]
    cur.status = [("A085", "ACTIVE"), ("A107", ""), ("DEAD1", "ACTIVE"),
                  ("停用店", "SUSPENDED")]
    _wire(monkeypatch, cur, registered={"A085", "A107", "停用店"},
          reports=tmp_path)
    out = wf.run({})
    c3 = (tmp_path / "alloc_同ASIN冲突处置.csv").read_text(encoding="utf-8-sig")
    assert "停用店" in c3                       # 它参与争 B0AAAA0001 的归属
    assert "占用按设计保持不动" in out
    # 「占用内」= 是:停用不改变占用归属
    ov = (tmp_path / "alloc_店铺总览.csv").read_text(encoding="utf-8-sig")
    row = next(ln for ln in ov.splitlines() if ln.startswith("停用店,"))
    assert row.split(",")[1] == "是"


def test_run_reports_which_stores_opted_out_of_allocation(monkeypatch, tmp_path):
    """「单店最大在线数」填 0 的店要点名回读——所有者才能核对表格改对了没。"""
    cur = _FakeCur()
    _wire(monkeypatch, cur, reports=tmp_path, cfg={
        "A085": {"gmv": 100.0, "orders": 3.0, "max_online": 0.0,
                 "channel": "FBA", "channel_raw": "fba",
                 "categories": ["Fashion"]},
        "A107": {"gmv": None, "orders": None, "max_online": 500.0,
                 "channel": None, "channel_raw": "", "categories": []}})
    out = wf.run({})
    assert "不参与分配  1 家" in out and "A085" in out
    ov = (tmp_path / "alloc_店铺总览.csv").read_text(encoding="utf-8-sig")
    rows = {ln.split(",")[0]: ln.split(",") for ln in ov.splitlines()[1:]}
    assert rows["A085"][2] == "否(填了0)" and rows["A085"][3] == "0"
    assert rows["A107"][2] == "是" and rows["A107"][3] == "500"
    assert rows["DEAD1"][2] == "(未填)" and rows["DEAD1"][3] == ""
