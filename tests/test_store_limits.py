"""按店配送时长上限(限额表「配送时长限制」列)回归。所有者定稿 2026-08-16。"""

from services import store_limits as sl


def test_unknown_lead_days_is_never_over_cap():
    """⚠ 没采到(None)不算超限 —— `or 0` 会把"未知"读成"当天达",方向反了。

    读反的后果两侧都不报错:上架侧会把一批未知货期的商品照常上架,
    维护侧则会把它们全部清零。
    """
    assert sl.over_lead_cap(None, 8) is False
    assert sl.over_lead_cap(8, 8) is False      # 等于上限不算超
    assert sl.over_lead_cap(9, 8) is True
    assert sl.over_lead_cap(0, 8) is False


def test_cap_falls_back_per_store():
    caps = {"A085": 3}
    assert sl.cap_for(caps, "A085", 8) == 3
    assert sl.cap_for(caps, "没配的店", 8) == 8      # 查不到回落默认
    assert sl.cap_for({}, "A085", 8) == 8            # 表整个读不到也回落
    assert sl.cap_for(caps, None, 8) == 8


def test_zero_or_blank_is_treated_as_not_configured(monkeypatch):
    """「填了 0」与「没填」都视同没配 —— 上限 0 天 = 这条链整店停摆,
    不像人的本意;真要停某店有 店铺状态 与 stockzero 两条显式路径。"""
    from api import feishu
    from registry import resources
    monkeypatch.setattr(feishu, "list_records", lambda t, field_names=None: [
        {"fields": {"店铺": "A", "配送时长限制": "5"}},
        {"fields": {"店铺": "B", "配送时长限制": "0"}},
        {"fields": {"店铺": "C", "配送时长限制": ""}},
        {"fields": {"店铺": "D", "配送时长限制": "垃圾"}},
        {"fields": {"店铺": "", "配送时长限制": "9"}},      # 无店铺名
    ])
    monkeypatch.setattr(feishu, "_plain_text",
                        lambda v: "" if v is None else str(v))
    # ⚠ **一列一个常量**:2026-08-16 合并时分配链与上下架链各建过一个指向
    # 同一列的常量,已合并为 lead_limit。两个常量的后果是表头一改名只坏一半,
    # 另一半静默读空(而"读空"在这条链上表现为"全店回落默认上限",不报错)
    assert resources.RETIRE_LIMITS.fields.lead_limit == "配送时长限制"
    assert not hasattr(resources.RETIRE_LIMITS.fields, "max_lead_days")
    assert sl.lead_day_caps() == {"A": 5}


def test_table_unregistered_degrades_to_empty(monkeypatch):
    """表没登记不许把整条链拖垮:返回空字典,调用方全店回落默认上限。"""
    from api import feishu
    def _boom(t, field_names=None):
        raise LookupError("未登记")
    monkeypatch.setattr(feishu, "list_records", _boom)
    assert sl.lead_day_caps() == {}


def test_listing_and_maintenance_use_the_same_predicate():
    """两个消费方共用 over_lead_cap/cap_for —— 各写各的迟早飘成
    "上架时按 8 天拦、维护时按 12 天清零"这种自相矛盾。"""
    import inspect

    from services import maintenance_intents
    from workflows import list_new
    for mod in (list_new, maintenance_intents):
        src = inspect.getsource(mod)
        assert "store_limits.over_lead_cap" in src, mod.__name__
        assert "store_limits.cap_for" in src, mod.__name__
