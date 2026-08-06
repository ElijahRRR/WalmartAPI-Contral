"""problem_product_cleanup 回归:归类规则、优先级、反补路由、四层去重、dry-run。"""

from services import problem_products as pp
from workflows import problem_product_cleanup as ppc


def test_categorize_rules_and_priority():
    assert pp.categorize("The End Date has passed for this item") == ("A", "过期")
    assert pp.categorize("violates Prohibited Product Policy") == ("B", "禁售")
    # 同文本命中 C 与 A → 严重性顺序 C 先(具体归类优先,A 过期最后)
    both = "restricts certain brands ... end date has passed"
    assert pp.categorize(both)[0] == "C"
    assert pp.categorize("Intellectual Property complaint") == ("E", "知产")
    assert pp.categorize("完全无关的文本") == ("Z", "其他")
    assert pp.categorize(None) == ("Z", "其他")
    assert pp.is_stage_pending("in Stage status until you go live.")
    assert not pp.is_stage_pending("end date has passed")


def test_pick_product_id_and_relist_item():
    assert pp.pick_product_id("123456789012", "") == ("00123456789012", "GTIN")
    assert pp.pick_product_id("", "12345678") == ("000012345678", "UPC")
    assert pp.pick_product_id("1234567", "1234") is None      # 都不足 8 位
    item = pp.build_relist_item("SKU1", "123456789012", "")
    assert item["Orderable"]["sku"] == "SKU1"
    assert item["Orderable"]["endDate"] == pp.NEW_END_DATE
    assert item["Orderable"]["productIdentifiers"]["productIdType"] == "GTIN"
    assert pp.build_relist_item("S", "", "") is None


def _item(store, sku, reasons, gtin="123456789012"):
    return {"store": store, "sku": sku, "gtin": gtin, "upc": "", "reasons": reasons}


def test_plan_routing_and_dedup():
    items = [
        _item("T1", "S_A", "end date has passed"),            # → 反补
        _item("T1", "S_A2", "end date has passed", gtin=""),  # A 无 productId → 删除
        _item("T1", "S_AMAX", "end date has passed"),         # 反补满 2 次 → 删除
        _item("T1", "S_B", "prohibited product policy"),      # → 删除
        _item("T1", "S_STAGE", "stage status until you go live"),   # 排除
        _item("T1", "S_FLY", "intellectual property"),        # 在途 → 跳过
        _item("T1", "S_RECENT", "intellectual property"),     # 近 7 天已删 → 跳过
        _item("T_OFF", "S_X", "prohibited product policy"),   # 非 ACTIVE 店 → 跳过
        _item("T1", "S_ZOMBIE", "prohibited product policy"),  # 删除未生效 → 双击
    ]
    plans, n = ppc.plan(items,
                        inflight={("T1", "S_FLY")},
                        recent_del={("T1", "S_RECENT"), ("T1", "S_ZOMBIE")},
                        attempts={("T1", "S_AMAX"): 2},
                        inactive={"T_OFF"},
                        stubborn={("T1", "S_ZOMBIE")})
    assert [r["sku"] for r in plans["T1"]["relist"]] == ["S_A"]
    # 顽固 SKU 绕过 7 天防重窗,停用+删除双 feed
    assert {r["sku"] for r in plans["T1"]["delete"]} == \
        {"S_A2", "S_AMAX", "S_B", "S_ZOMBIE"}
    assert [r["sku"] for r in plans["T1"]["retire"]] == ["S_ZOMBIE"]
    assert n["stubborn"] == 1
    assert "T_OFF" not in plans
    assert (n["stage"], n["inflight"], n["recent"], n["inactive"]) == (1, 1, 1, 1)
    assert n["fallback"] == 1 and n["relist"] == 1 and n["delete"] == 3


def test_dry_run_zero_submissions(monkeypatch):
    monkeypatch.setattr(ppc, "_load_state", lambda: (
        [_item("T1", "S_B", "prohibited product policy")],
        set(), set(), {}, {}, set(), set()))
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("dry-run 不许提交")))
    out = ppc.run({"execute": False})
    assert "DRY-RUN" in out and "删除 1" in out and "('S_B', 'B')" in out
