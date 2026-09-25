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


def test_setup_limits_reads_the_item_ceiling_column(monkeypatch):
    """「商品上限」= 沃尔玛每店的 item setup limit(2026-09-07 A131吕灿荣 实证:
    店内现有 item 数 + 本 feed 条数 超了它,**整个 feed 被拒收**)。

    ⚠ 与「单店最大在线数」是两回事:那是我们自己给店定的经营容量(分配引擎读),
    这是沃尔玛的硬限。合成一列的表现是分配目标一改,改码就开始整批被拒。
    """
    from api import feishu
    from registry import resources
    assert resources.RETIRE_LIMITS.fields.item_setup_limit == "商品上限"
    assert resources.RETIRE_LIMITS.fields.max_online == "单店最大在线数"
    assert resources.WALMART_ITEM_SETUP_LIMIT_DEFAULT == 5000
    monkeypatch.setattr(feishu, "list_records", lambda t, field_names=None: [
        {"fields": {"店铺": "A131吕灿荣", "商品上限": "5000"}},
        {"fields": {"店铺": "A085朱丽霖", "商品上限": ""}},      # 没填 ⇒ 走缺省
    ])
    monkeypatch.setattr(feishu, "_plain_text",
                        lambda v: "" if v is None else str(v))
    caps = sl.setup_limits()
    assert caps == {"A131吕灿荣": 5000}
    # 没填 / 表整个读不到 ⇒ 调用方回落缺省(不知道 ≠ 限死)
    assert sl.cap_for(caps, "A085朱丽霖",
                      resources.WALMART_ITEM_SETUP_LIMIT_DEFAULT) == 5000


def test_setup_limits_survives_an_unregistered_table(monkeypatch):
    """列还没建 / 表没登记 ⇒ 空字典,全船队走缺省 5000(不许把改码整条链拖垮)。"""
    from api import feishu

    def _boom(t, field_names=None):
        raise LookupError("未登记")
    monkeypatch.setattr(feishu, "list_records", _boom)
    assert sl.setup_limits() == {}


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


# ── 单品最大库存(限额表「最大库存」,所有者定稿 2026-09-25)────────────────

def test_stock_for_threshold_decides_whether_cap_decides_how_many(monkeypatch):
    """门槛与最大库存**各管一件事**(所有者定稿 2026-09-25):门槛决定卖不卖
    (低于门槛:上架侧不上架、维护侧写 0),最大库存 N 决定卖多少(过了门槛写
    min(亚马逊库存, N))。下面两组正是所有者给的例子。"""
    from services import amz_source
    assert amz_source.MIN_INVENTORY == 5            # 现行门槛:全局常量 5
    f = sl.stock_for
    # 例一:门槛 5、N=3 —— 0~4 件写 0,5 件及以上写 3
    for s in range(0, 5):
        assert f(s, 3) == (0, sl.QTY_BELOW_MIN), s
    for s in (5, 6, 7, 999):
        assert f(s, 3) == (3, sl.QTY_CAPPED), s
    # 例二:门槛 3、N=5(门槛是常量,这里临时改它只为复现所有者的例子)——
    # 0~2 件写 0;3 → 3,4 → 4,5 件及以上 → 5
    monkeypatch.setattr(amz_source, "MIN_INVENTORY", 3)
    assert [f(s, 5)[0] for s in range(0, 9)] == [0, 0, 0, 3, 4, 5, 5, 5, 5]
    assert f(4, 5) == (4, "")                       # 没超 N:原样,不算封顶
    assert f(5, 5) == (5, "")                       # 恰好等于 N:同上
    assert f(6, 5) == (5, sl.QTY_CAPPED)
    monkeypatch.setattr(amz_source, "MIN_INVENTORY", 5)
    # 没设 N:过了门槛原样跟随;N 比亚马逊大时也是原数(N 只是封顶)
    assert f(4, None) == (0, sl.QTY_BELOW_MIN)
    assert f(500, None) == (500, "")
    assert f(12, 20) == (12, "")
    assert f(50, 0) == (50, "")                     # 0 = 没设(与读列口径一致)
    # 没采到数量:0
    assert f(None, None) == (0, sl.QTY_NO_COUNT)
    assert f(None, 3) == (0, sl.QTY_NO_COUNT)


def test_stock_caps_reads_the_new_column_blank_or_zero_means_unlimited(
        monkeypatch, caplog):
    """「最大库存」是新列,**不复用「库存特殊要求」**(那一格的 0 是整店停售开关)。

    留空或填 0 = 不限;填了不是数字的要**出声**(按没填处理 = 那家店的上限
    悄悄失效,而表上看着明明填了)。
    """
    from api import feishu
    from registry import resources
    f = resources.RETIRE_LIMITS.fields
    assert f.max_stock == "最大库存"
    assert f.max_stock != f.inventory_note
    monkeypatch.setattr(feishu, "list_records", lambda t, field_names=None: [
        {"fields": {"店铺": "A", "最大库存": "3"}},
        {"fields": {"店铺": "B", "最大库存": "0"}},
        {"fields": {"店铺": "C", "最大库存": ""}},
        {"fields": {"店铺": "D", "最大库存": "3件"}},
        {"fields": {"店铺": "E", "最大库存": 20}},
    ])
    monkeypatch.setattr(feishu, "_plain_text",
                        lambda v: "" if v is None else str(v))
    with caplog.at_level("WARNING", logger="services.store_limits"):
        assert sl.stock_caps() == {"A": 3, "E": 20}
    assert any("3件" in r.getMessage() and "最大库存" in r.getMessage()
               for r in caplog.records)


def test_listing_and_maintenance_share_one_stock_rule():
    """上架与维护共用 `stock_for` —— 各写一份迟早飘成"上架按 5 拦、维护按 3 写"。"""
    import inspect

    from services import maintenance_intents
    from workflows import list_new
    for mod in (list_new, maintenance_intents):
        src = inspect.getsource(mod)
        assert "store_limits.stock_for" in src, mod.__name__
    # 门槛常量只在 stock_for 里被比较,两条链都不自己比
    assert "< amz_source.MIN_INVENTORY" not in inspect.getsource(list_new)
