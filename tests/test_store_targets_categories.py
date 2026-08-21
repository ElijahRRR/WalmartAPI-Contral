"""店铺准入大类:三列取值 + "空=不限制" 判定(所有者口径 2026-08-15)。"""

from services import store_targets as st


def _cfg(cats):
    return {"categories": cats}


def test_allowed_empty_means_unrestricted():
    """三列都空 = 不限制类目——这条正反都像对的,只准在这一处判。"""
    for row in (_cfg([]), _cfg(None), {}, None):
        assert st.allowed(row, "Home") is True
        assert st.allowed(row, None) is True          # 归不到大类也放行


def test_allowed_filled_admits_only_the_super_categories_listed():
    """填了就只准入填的那几个 —— 但**判定在五大品类那一层**(所有者拍 Q1)。

    ⚠ 这是一次**有意的放宽**,不是 bug:店填「Office」= 它做 Hardlines,
    于是同属 Hardlines 的 Sporting Goods 也收得了。改的理由见 `allowed`
    的 docstring(按 26 类判会锁死全池 24.2% 的货)。
    别的品类照旧拒 —— 放宽的是"同品类内",不是"什么都收"。
    """
    row = _cfg(["Home", "Office"])                    # Home / Hardlines
    assert st.allowed(row, "Home") is True
    assert st.allowed(row, "Office") is True
    assert st.allowed(row, "Sporting Goods") is True  # 同属 Hardlines
    assert st.allowed(row, "Furniture") is True       # 同属 Home
    assert st.allowed(row, "Animals") is False        # FCHW,没填
    assert st.allowed(row, "Toys") is False           # ETS,没填


def test_a_store_that_filled_only_unmapped_categories_admits_nothing():
    """⚠ 填了类目、但填的全是「不归」的那两类 → 折完是空集。

    **不许因为空集就当成"没填 = 不限制"** —— 那是把最严的店误读成最松的店,
    一批本该只去无类目店的货会全灌给它,而占用撤不回。
    """
    row = _cfg(["Safety & Emergency", "Everything Else"])
    assert st.super_categories_of(row) == set()
    assert st.allowed(row, "Home") is False
    assert st.allowed(row, "Safety & Emergency") is False   # 它自己也不收


def test_unmapped_goods_go_only_to_stores_with_no_category_set():
    """「不归」的两类只能去**没有确定类目**的店(所有者 2026-08-21 原话)。"""
    for cat in ("Safety & Emergency", "Everything Else"):
        assert st.allowed(_cfg([]), cat) is True        # 没填类目的店:收
        assert st.allowed(_cfg(["Home"]), cat) is False  # 填了的:一律拒


def test_allowed_restricted_store_rejects_unclassified():
    """受限店遇到归不到大类的产品:拒收(宁可不分也不错分)。"""
    assert st.allowed(_cfg(["Home"]), None) is False
    assert st.allowed(_cfg(["Home"]), "") is False


def test_load_targets_reads_three_category_columns(monkeypatch):
    from registry import resources
    f = resources.RETIRE_LIMITS.fields
    recs = [{"fields": {f.store: "A085", f.category1: "Home",
                        f.category2: " Office ", f.category3: "Home",
                        f.max_online: "500", f.channel_limit: "fba"}},
            {"fields": {f.store: "A107"}}]
    monkeypatch.setattr(st.feishu, "list_records", lambda *a, **k: recs)
    # Bitable 是 frozen dataclass:用 replace 造一个"已登记"的副本,
    # 不 reload 模块——reload 会把测试用的假 token 永久留在进程里污染后续用例
    import dataclasses
    from types import SimpleNamespace
    registered = dataclasses.replace(resources.RETIRE_LIMITS,
                                     app_token="x", table_id="y")
    monkeypatch.setattr(st, "resources",
                        SimpleNamespace(RETIRE_LIMITS=registered))
    out = st.load_targets()
    # 去重 + 去空白,顺序保留
    assert out["A085"]["categories"] == ["Home", "Office"]
    assert out["A107"]["categories"] == []
    assert out["A085"]["max_online"] == 500.0
