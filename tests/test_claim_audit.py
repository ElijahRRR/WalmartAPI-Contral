"""占用对账回归。这张表的价值全在"报什么 / 不报什么"上,两边都要钉死。

多报一条 = 诱人去释放一个该保持的占用(释放后品牌被下一轮分给别店,不可逆);
少报一条 = 一个当初就不该产生的占用永远躺在台账里,而没有任何地方会说。
"""

from services import claims
from workflows import claim_audit as ca


def _row(store, sku, asin, brand, cat="Fashion", ch="FBA", pub=True):
    return {"store": store, "sku": sku, "asin": asin, "brand_key": brand,
            "category": cat, "channel": ch, "published": pub,
            "pt": "x", "pt_source": None}


CFG = {"A": {"categories": ["Fashion"], "channel": "FBA"}}


def _run(monkeypatch, rows, held):
    """把 IO 全替掉,只跑判定逻辑 —— 这条工作流的全部风险都在判定上。"""
    monkeypatch.setattr(ca.sv, "_fetch_meta", lambda *a, **k: {})
    monkeypatch.setattr(ca.sv, "enrich", lambda *a, **k: (rows, {}))
    monkeypatch.setattr(ca.stores_svc, "registered_names", lambda: {"A"})
    monkeypatch.setattr(ca.store_targets, "load_targets", lambda: CFG)
    monkeypatch.setattr(ca.claims, "load_active",
                        lambda conn, kind: held.get(kind, {}))
    monkeypatch.setattr(ca.sku_asin, "extract_asin", lambda s: s)

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cur()

    class _Ctx:
        def __enter__(self): return _Conn()
        def __exit__(self, *a): return False

    monkeypatch.setattr(ca.db, "pg_conn", lambda *a, **k: _Ctx())
    return ca.run({"export": "0"})


def test_reports_a_claim_whose_online_rows_all_fail_the_gates(monkeypatch):
    """在架行全踩闸 = 当初就不该占,必须报出来。

    2026-08-16 的实例:回填时渠道闸还没补,渠道不符的品牌被永久锁死了。
    """
    rows = [_row("A", "S1", "B0AAAA0001", "错渠道牌", ch="FBM")]
    out = _run(monkeypatch, rows,
               {claims.BRAND: {"错渠道牌": "A"}, claims.PRODUCT: {}})
    assert "错渠道牌" in out and "渠道不符" in out
    assert "1 条占用当初就不该产生" in out


def test_delisted_product_is_never_reported(monkeypatch):
    """⚠ **商品下架不释放占用**(§六铁律)。

    该店已经没有这个键的在架行 —— 那正是占用该保持的样子。报出来会诱人
    去释放,而释放之后品牌被下一轮分给别的店,那一步不可逆。
    """
    out = _run(monkeypatch, [],          # 一行在架的都没有
               {claims.BRAND: {"下架了的牌": "A"}, claims.PRODUCT: {}})
    assert "下架了的牌" not in out
    assert "✅ 没有该释放的占用" in out


def test_healthy_claim_is_not_reported(monkeypatch):
    rows = [_row("A", "S1", "B0AAAA0001", "好牌")]
    out = _run(monkeypatch, rows,
               {claims.BRAND: {"好牌": "A"}, claims.PRODUCT: {"B0AAAA0001": "A"}})
    assert "✅ 没有该释放的占用" in out


def test_partially_offending_claim_is_not_reported(monkeypatch):
    """只要**还有一行**站得住,占用就站得住 —— 别因为品牌里有个别违规品就释放它。

    释放会把整个品牌从这家店拿走,而它手上还有合规的在卖。
    """
    rows = [_row("A", "S1", "B0AAAA0001", "混合牌"),                 # 合规
            _row("A", "S2", "B0AAAA0002", "混合牌", cat="Home")]     # 类目不符
    out = _run(monkeypatch, rows,
               {claims.BRAND: {"混合牌": "A"}, claims.PRODUCT: {}})
    assert "混合牌" not in out


def test_unpublished_rows_do_not_resurrect_a_claim(monkeypatch):
    """未发布行不占货位,所以也不能拿它当"还有在架行"的证据。

    拿它当证据的话,一个只剩未发布行的违规占用会被归进"不判",永远查不出来。
    """
    rows = [_row("A", "S1", "B0AAAA0001", "错渠道牌", ch="FBM", pub=False)]
    out = _run(monkeypatch, rows,
               {claims.BRAND: {"错渠道牌": "A"}, claims.PRODUCT: {}})
    assert "✅ 没有该释放的占用" in out       # 归入"不判",不诱导释放


def test_claim_held_by_the_wrong_store_is_judged_against_that_store(monkeypatch):
    """判的是**占用店自己**的在架行,不是这个键在别处的情况。

    写成"这个键在任何店有合规行就算站得住"的话,一个被错误的店占走的品牌
    会因为正确的店也在卖它而被判成健康 —— 恰好把最该修的那条藏起来。
    """
    monkeypatch.setattr(ca.stores_svc, "registered_names", lambda: {"A", "B"})
    monkeypatch.setattr(ca.store_targets, "load_targets",
                        lambda: {"A": {"categories": ["Fashion"], "channel": "FBA"},
                                 "B": {"categories": ["Home"], "channel": "FBA"}})
    rows = [_row("A", "S1", "B0AAAA0001", "牌", cat="Home"),   # A 不准入 Home
            _row("B", "S2", "B0AAAA0002", "牌", cat="Home")]   # B 准入
    out = _run(monkeypatch, rows, {claims.BRAND: {"牌": "A"}, claims.PRODUCT: {}})
    assert "1 条占用当初就不该产生" in out
