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


def test_a_store_that_filled_other_becomes_the_store_that_takes_other():
    """★ 2026-08-22 改口径:填「其他」的店 = **专收归不到五品类的货**。

    改之前这三种填法都折成空集 ⇒ 按「填了就只准入填的那几个」判 ⇒ 谁也
    接不了,一家店被静默废掉 —— 而所有者要的建议列正是「填写 5 大类和其他」,
    照着填就会踩这个坑。所以出建议之前先让它可填。
    """
    for row in (_cfg(["其他"]), _cfg(["Safety & Emergency"]),
                _cfg(["Everything Else"])):
        assert st.super_categories_of(row) == {"其他"}
        assert st.allowed(row, "Safety & Emergency") is True
        assert st.allowed(row, "Everything Else") is True
        assert st.allowed(row, "Home") is False          # 五品类的货照旧拒


def test_other_store_still_rejects_goods_whose_category_is_unknown():
    """⚠ **空 ≠ 其他,这条不许合并。**

    「其他」是业务归类(处置:找一家收「其他」的店);大类采不到是数据缺口
    (处置:补采集)。合并会让填了「其他」的店开始收一批**我们根本不知道
    是什么**的货,而且 `category_offenders` 那条"不知道不算违规"也会跟着塌。
    """
    row = _cfg(["其他"])
    assert st.allowed(row, None) is False
    assert st.allowed(row, "") is False


def test_unmapped_goods_go_to_no_category_stores_or_to_other_stores():
    """「不归」的两类能去**没填类目**的店,也能去**明确填了「其他」**的店。

    所有者 2026-08-21 原话是「不归,可以分配给没有确定类目的店」——
    那是"可以给没填的店",不是"只能给没填的店"(2026-08-22 补开后一条)。
    """
    for cat in ("Safety & Emergency", "Everything Else"):
        assert st.allowed(_cfg([]), cat) is True          # 没填类目的店:收
        assert st.allowed(_cfg(["其他"]), cat) is True     # 明确填「其他」:收
        assert st.allowed(_cfg(["Home"]), cat) is False   # 填了别的:一律拒


def test_an_unrecognised_literal_folds_into_other_not_into_nothing():
    """表里写错字(「Hoem」)折进「其他」,而不是被丢掉变成空集。

    两种都不对,但丢掉会**静默废掉一家店**;折进「其他」只是让它收错一批货,
    而且 `alloc_audit` 会拿 `known_category_literal` 把这个值点名出来。
    这条测试同时是那条点名的理由 —— 没有点名,这就成了静默改准入。
    """
    from registry import resources
    row = _cfg(["Hoem"])
    assert st.super_categories_of(row) == {"其他"}
    assert st.allowed(row, "Home") is False
    assert resources.known_category_literal("Hoem") is False
    assert resources.known_category_literal("Home") is True
    assert resources.known_category_literal("其他") is True
    assert resources.known_category_literal("Everything Else") is True


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


# ── 配送时长:未填回落 7 天(所有者 2026-08-21 统一到上架链)──────────────

def test_unset_lead_limit_falls_back_to_seven_not_unlimited():
    """⚠ 同一列「配送时长限制」,两条链的"未填"回落方向曾经**相反**。

    上架链 `store_limits.cap_for(caps, store, MAX_LEAD_DAYS)` 未填回落 7;
    分配这边原本未填就放行一切。所有者 2026-08-21 拍板统一到 7。
    影响面不小:只要有**一家店**空着这列,`alloc_plan._pool_reach` 的并集
    就变成"不限",慢货与未知货期全池涌入 —— 而且不报错。
    """
    from services import amz_source
    unset = {"lead_limit": None}
    assert st.lead_cap_of(unset) == amz_source.MAX_LEAD_DAYS
    assert st.lead_cap_of({}) == amz_source.MAX_LEAD_DAYS
    assert st.lead_ok(unset, amz_source.MAX_LEAD_DAYS) is True
    assert st.lead_ok(unset, amz_source.MAX_LEAD_DAYS + 1) is False
    # 填了的照旧只认自己填的那个数(可松可严)
    assert st.lead_ok({"lead_limit": 3}, 5) is False
    assert st.lead_ok({"lead_limit": 30}, 20) is True


def test_unmeasured_lead_is_refused_by_every_store_including_unset_ones():
    """采不到货期 = 拒收。**现在没有"不限"的店了,所以每一家都拒。**

    拿"没采到"当"够快"是替所有者做了他没做的决定 —— 与类目那条
    「归不到大类的,受限店拒收」同一纪律。
    """
    for row in ({"lead_limit": 5}, {"lead_limit": None}, {}, None):
        assert st.lead_ok(row, None) is False
