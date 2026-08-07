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
        _item("T_OFF", "S_X", "prohibited product policy"),   # 非 ACTIVE 店 → 跳过
        _item("T1", "S_ZOMBIE", "prohibited product policy"),  # 删除未生效 → 双击
    ]
    plans, n = ppc.plan(items,
                        inflight={("T1", "S_FLY")},
                        attempts={("T1", "S_AMAX"): 2},
                        inactive={"T_OFF"},
                        stubborn={("T1", "S_ZOMBIE")})
    assert [r["sku"] for r in plans["T1"]["relist"]] == ["S_A"]
    # 顽固 SKU 停用+删除双 feed
    assert {r["sku"] for r in plans["T1"]["delete"]} == \
        {"S_A2", "S_AMAX", "S_B", "S_ZOMBIE"}
    assert [r["sku"] for r in plans["T1"]["retire"]] == ["S_ZOMBIE"]
    assert n["stubborn"] == 1
    assert "T_OFF" not in plans
    assert (n["stage"], n["inflight"], n["inactive"]) == (1, 1, 1)
    assert n["fallback"] == 1 and n["relist"] == 1 and n["delete"] == 3


def test_execute_records_events_per_slice(monkeypatch):
    # 多切片提交:事件账本必须滑窗对位([:count] 写法会把第一片重复记到 FB)
    import contextlib
    from registry import db as _db
    events = []
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    monkeypatch.setattr(ppc.product_events, "record_many",
                        lambda conn, rows: (events.extend(rows), len(rows))[1])
    monkeypatch.setattr(ppc.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    items = [_item("T1", f"S{i}", "prohibited product policy") for i in range(4)]
    monkeypatch.setattr(ppc, "_load_state",
                        lambda: (items, set(), {}, {}, set(), set()))
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda store, ft, entries, *, workflow="": [
                            {"feed_id": "FA", "count": 2, "outcome": "submitted"},
                            {"feed_id": "FB", "count": 2, "outcome": "submitted"}])

    ppc.run({"execute": True})
    sub = [(e["sku"], e["detail"]["feed_id"]) for e in events
           if e["event"] == "delete_submitted"]
    assert sub == [("S0", "FA"), ("S1", "FA"), ("S2", "FB"), ("S3", "FB")]


def test_execute_skips_recording_on_dedup(monkeypatch):
    # dedup=在途防重命中,什么都没提交:不许落 *_submitted 幽灵事件
    # (反补计数被灌水会导致少一次真实反补就转永久删除),摘要单列跳过数
    import contextlib
    from registry import db as _db
    events = []
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    monkeypatch.setattr(ppc.product_events, "record_many",
                        lambda conn, rows: (events.extend(rows), len(rows))[1])
    monkeypatch.setattr(ppc.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    monkeypatch.setattr(ppc, "_load_state", lambda: (
        [_item("T1", "S0", "prohibited product policy")],
        set(), {}, {}, set(), set()))
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda store, ft, entries, *, workflow="": [
                            {"feed_id": "F_OLD", "count": 1, "outcome": "dedup"}])
    out = ppc.run({"execute": True})
    assert not [e for e in events if e["event"] == "delete_submitted"]
    assert "删除提交 0" in out and "在途防重跳过 1" in out


def test_execute_reports_rejected_and_unknown(monkeypatch):
    # 提交被拒/结局不确定必须在摘要中可见(危险工作流不许静默假成功)
    import contextlib
    from registry import db as _db
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    monkeypatch.setattr(ppc.product_events, "record_many",
                        lambda conn, rows: len(rows))
    monkeypatch.setattr(ppc.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    items = [_item("T1", f"S{i}", "prohibited product policy") for i in range(2)]
    monkeypatch.setattr(ppc, "_load_state",
                        lambda: (items, set(), {}, {}, set(), set()))
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda store, ft, entries, *, workflow="": [
                            {"feed_id": None, "count": 1, "outcome": "failed"},
                            {"feed_id": None, "count": 1, "outcome": "unknown"}])
    out = ppc.run({"execute": True})
    assert "提交失败 1" in out and "结局不确定留 pending 1" in out
    assert "删除提交 0" in out
    assert "二轮重试" not in out          # 4xx 被拒/unknown 不算网络波动


def test_execute_isolates_store_failures(monkeypatch):
    # 单店代理断线不炸整轮(2026-08-07 生产实证):异常店跳过,其余店照常
    import contextlib
    from registry import db as _db
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    monkeypatch.setattr(ppc.product_events, "record_many",
                        lambda conn, rows: len(rows))
    monkeypatch.setattr(ppc.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}, {"name": "T2"}])
    items = [_item("T1", "S1", "prohibited product policy"),
             _item("T2", "S2", "prohibited product policy")]
    monkeypatch.setattr(ppc, "_load_state",
                        lambda: (items, set(), {}, {}, set(), set()))
    submitted = []

    def flaky(store, ft, entries, *, workflow=""):
        if store["name"] == "T1":
            raise ConnectionError("proxy TLS EOF")
        submitted.append((store["name"], list(entries)))
        return [{"feed_id": "F_OK", "count": len(entries),
                 "outcome": "submitted"}]

    monkeypatch.setattr(ppc.feeds, "submit_feed", flaky)
    out = ppc.run({"execute": True})
    assert submitted == [("T2", ["S2"])]                     # T2 不被重复提交
    assert "⚠ T1:提交异常" in out and "T2:删除提交 1" in out
    assert "二轮重试 1 店:T1" in out and "⚠ T1:二轮仍失败" in out


def test_second_round_retry_after_network_failure(monkeypatch):
    # 二轮重试成功路径:第一轮 token 阶段确定未达(retryable)→
    # 全部店跑完后整店重提,事件只落一次、挂二轮的真实 feed_id
    import contextlib
    from registry import db as _db
    events = []
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([None])))
    monkeypatch.setattr(ppc.product_events, "record_many",
                        lambda conn, rows: (events.extend(rows), len(rows))[1])
    monkeypatch.setattr(ppc.stores_svc, "load_stores",
                        lambda names=None: [{"name": "T1"}])
    monkeypatch.setattr(ppc, "_load_state", lambda: (
        [_item("T1", "S1", "prohibited product policy")],
        set(), {}, {}, set(), set()))
    calls = {"n": 0}

    def flaky_then_ok(store, ft, entries, *, workflow=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"feed_id": None, "count": 1, "outcome": "failed",
                     "retryable": True}]
        return [{"feed_id": "F_RETRY", "count": 1, "outcome": "submitted"}]

    monkeypatch.setattr(ppc.feeds, "submit_feed", flaky_then_ok)
    out = ppc.run({"execute": True})
    sub = [e for e in events if e["event"] == "delete_submitted"]
    assert len(sub) == 1 and sub[0]["detail"]["feed_id"] == "F_RETRY"
    assert "二轮重试 1 店:T1" in out and "二轮仍失败" not in out


def test_stubborn_sql_binds_to_listing_generation():
    # 顽固标记绑定当前上架代际:最新事件是(重)上架 → 旧核验失效不再顽固
    assert "item_appeared" in ppc._SQL_STUBBORN
    assert "item_reappeared" in ppc._SQL_STUBBORN


def test_inflight_sql_blocks_unobserved_success():
    # 在途/待观测拦截:feed 落定 success 但 catalog_sync 未重新观测
    # (resolved_at > last_seen_at)必须继续拦——否则落定后、扫店前重跑
    # 会把同一批 SKU 全量重发(2026-08-07 生产实证)
    assert "f.status = 'submitted'" in ppc._SQL_INFLIGHT
    assert "f.resolved_at > w.last_seen_at" in ppc._SQL_INFLIGHT
    assert "JOIN catalog.walmart_items" in ppc._SQL_INFLIGHT


def test_dry_run_zero_submissions(monkeypatch):
    monkeypatch.setattr(ppc, "_load_state", lambda: (
        [_item("T1", "S_B", "prohibited product policy")],
        set(), {}, {}, set(), set()))
    monkeypatch.setattr(ppc.feeds, "submit_feed",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("dry-run 不许提交")))
    out = ppc.run({"execute": False})
    assert "DRY-RUN" in out and "删除 1" in out
    assert "类别={B:1}" in out and "删除样本=[('S_B', 'B')]" in out
