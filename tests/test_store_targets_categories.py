"""店铺准入大类:三列取值 + "空=不限制" 判定(所有者口径 2026-08-15)。"""

from services import store_targets as st


def _cfg(cats):
    return {"categories": cats}


def test_allowed_empty_means_unrestricted():
    """三列都空 = 不限制类目——这条正反都像对的,只准在这一处判。"""
    for row in (_cfg([]), _cfg(None), {}, None):
        assert st.allowed(row, "Home") is True
        assert st.allowed(row, None) is True          # 归不到大类也放行


def test_allowed_filled_admits_only_listed():
    row = _cfg(["Home", "Office"])
    assert st.allowed(row, "Home") is True
    assert st.allowed(row, "Office") is True
    assert st.allowed(row, "Sporting Goods") is False


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
