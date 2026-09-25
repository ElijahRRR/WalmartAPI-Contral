"""node_clear:受管仓以外的节点库存清零(搬仓收尾,一次性)。

2026-09-25 所有者定稿:「api 应该可以拿到仓库编号,如果有设置维护仓的,运行
脚本时直接把非维护仓的库存清零」—— 旧仓编号从 GET /v3/inventories 的
`nodes[].shipNode` 自动发现,不再要求人工 `-p node=`。保留的四道闸:
受管仓必须校验过、拒绝清受管仓、只清受管仓已接管的 SKU、节点身份未知不碰。
"""

from workflows import node_clear as nc


def _store(name="T1"):
    return {"name": name, "client_id": "cid", "client_secret": "sec",
            "proxy": None}


def _wire(monkeypatch, stores, managed, skipped=None, words=None, nodes=None):
    """把店铺表、受管仓校验结果与库存读数接好;返回写入记录列表。"""
    monkeypatch.setattr(nc.stores_svc, "load_stores",
                        lambda filter_names=None: [
                            s for s in stores
                            if filter_names is None or s["name"] in filter_names])

    def managed_nodes(stores=None, conn=None, stats=None):
        if stats is not None:
            stats["words"] = dict(words or {})
        return dict(managed), dict(skipped or {})

    monkeypatch.setattr(nc.store_limits, "managed_nodes", managed_nodes)
    read = []

    def list_nodes(store):
        read.append(store["name"])
        return (nodes or {}).get(store["name"], {})

    monkeypatch.setattr(nc.inv_api, "list_inventory_nodes", list_nodes)
    wrote = []

    def put(store, sku, qty, node=None):
        wrote.append((store["name"], sku, qty, node))
        return True, ""

    monkeypatch.setattr(nc.inv_api, "put_inventory", put)
    return read, wrote


def test_old_nodes_are_discovered_from_the_api_not_typed_in(monkeypatch):
    """不传 node:凡不是受管仓的节点、有货就清 —— 一个 SKU 挂几个旧仓就清几个。"""
    read, wrote = _wire(monkeypatch, [_store("T1")], {"T1": "N_NEW"}, nodes={
        "T1": {"A": {"N_NEW": 3, "N_OLD1": 5, "N_OLD2": 7},
               "B": {"N_NEW": 0, "N_OLD1": 2},     # 受管仓有行(0 也算接管)
               "C": {"N_NEW": 9}}})                # 旧仓没货 → 不在名单里
    out = nc.run({"store": "T1"})
    assert sorted(wrote) == [("T1", "A", 0, "N_OLD1"), ("T1", "A", 0, "N_OLD2"),
                             ("T1", "B", 0, "N_OLD1")]
    assert all(node != "N_NEW" for _, _, _, node in wrote)     # 受管仓一格不碰
    assert "N_OLD1 2 个/7 件" in out and "N_OLD2 1 个/7 件" in out
    assert "清零成功 3/3" in out
    assert out.splitlines()[0].startswith("节点清零(受管仓以外):1 店,待清 3 条")


def test_node_param_narrows_to_one_old_node(monkeypatch):
    _, wrote = _wire(monkeypatch, [_store("T1")], {"T1": "N_NEW"}, nodes={
        "T1": {"A": {"N_NEW": 3, "N_OLD1": 5, "N_OLD2": 7}}})
    nc.run({"store": "T1", "node": "N_OLD2"})
    assert wrote == [("T1", "A", 0, "N_OLD2")]


def test_unknown_node_identity_is_never_written(monkeypatch):
    """⚠ 接口没给 shipNode(键为空串或 `?序号`)的数量**不碰**:不带节点只能
    走旧接口写默认节点,可能正是受管仓 —— 只点名。"""
    _, wrote = _wire(monkeypatch, [_store("T1")], {"T1": "N_NEW"}, nodes={
        "T1": {"A": {"N_NEW": 3, "?0": 4, "": 2, "N_OLD": 1}}})
    out = nc.run({"store": "T1"})
    assert wrote == [("T1", "A", 0, "N_OLD")]
    assert "节点身份未知" in out and "2 份" in out


def test_refuses_to_clear_the_managed_node(monkeypatch):
    """⚠ 拒绝清受管仓:自动链每轮都在维护它,清了下一轮就写回来。"""
    read, wrote = _wire(monkeypatch, [_store("T1")], {"T1": "N_NEW"})
    out = nc.run({"store": "T1", "node": "N_NEW"})
    assert "拒绝执行" in out and "受管仓" in out and "stockzero" in out
    assert read == [] and wrote == []                 # 拒绝之前不去读库存


def test_dry_run_writes_nothing_and_lists_the_targets(monkeypatch):
    """--dry-run 一件都不写,但要报出规模与最大的几条(人眼闸门)。"""
    _, wrote = _wire(monkeypatch, [_store("T1")], {"T1": "N_NEW"}, nodes={
        "T1": {"B0A": {"N_OLD": 999, "N_NEW": 3},
               "B0B": {"N_OLD": 5},              # 受管仓没有行 ⇒ 未接管 ⇒ 跳过
               "B0C": {"N_NEW": 7},
               "B0D": {"N_OLD": 0}}})            # 旧节点是 0 → 不用清
    out = nc.run({"store": "T1", "dry_run": True})
    assert wrote == []
    assert "N_OLD 2 个/1004 件" in out
    assert "尚未接管** 1 个" in out and "B0B" in out
    assert "待清 1 条(SKU×节点),合计 999 件" in out
    assert out.startswith("[DRY-RUN] 节点清零") and "一件都没写" in out


def test_only_clears_what_the_managed_node_took_over(monkeypatch):
    """⚠ 只清**受管仓已接管**的 SKU(谭总12 搬仓实见:一把清完 = 断售)。"""
    _, wrote = _wire(monkeypatch, [_store("T1")], {"T1": "N_NEW"}, nodes={
        "T1": {"TAKEN": {"N_OLD": 999, "N_NEW": 3},
               "UNTAKEN": {"N_OLD": 50}}})
    out = nc.run({"store": "T1"})
    assert wrote == [("T1", "TAKEN", 0, "N_OLD")]     # UNTAKEN 一个字节都没碰
    assert "尚未接管** 1 个" in out and "UNTAKEN" in out
    assert "sources_backfill" in out                 # 给出补救路径,不只是拒绝

    wrote.clear()
    nc.run({"store": "T1", "include_untaken": "1"})
    assert sorted(s for _, s, _, _ in wrote) == ["TAKEN", "UNTAKEN"]


def test_names_the_failures(monkeypatch):
    """失败必须点名:写 0 是幂等的,重跑即补;静默的话那批货还在旧节点上卖。"""
    _wire(monkeypatch, [_store("T1")], {"T1": "N_NEW"}, nodes={
        "T1": {"B0A": {"N_OLD": 9, "N_NEW": 1}, "B0B": {"N_OLD": 8, "N_NEW": 2}}})
    seen = []

    def put(store, sku, qty, node=None):
        seen.append((sku, qty, node))
        return (False, "节点 N_OLD status=FAILURE: 库存台账没有这一行") \
            if sku == "B0B" else (True, "")

    monkeypatch.setattr(nc.inv_api, "put_inventory", put)
    out = nc.run({"store": "T1"})
    assert seen == [("B0A", 0, "N_OLD"), ("B0B", 0, "N_OLD")]  # 数量降序,都带节点
    assert "清零成功 1/2" in out
    assert "⚠ 失败 1 条" in out and "B0B@N_OLD" in out and "FAILURE" in out
    assert "⚠ 失败 1" in out.splitlines()[0]


def test_unconfigured_store_is_refused_unless_explicit(monkeypatch):
    """没配「维护仓库」⇒ 判不出哪个是旧仓、接管与否 ⇒ **拒绝**;显式给旧仓并
    include_untaken=1 才整节点清空(判不准就判活)。"""
    read, wrote = _wire(monkeypatch, [_store("T1")], {}, nodes={
        "T1": {"B0A": {"N_OLD": 9, "N_OTHER": 4}}})
    for params in ({"store": "T1"}, {"store": "T1", "node": "N_OLD"}):
        out = nc.run(params)
        assert "拒绝执行" in out and "include_untaken=1" in out
    assert read == [] and wrote == []
    nc.run({"store": "T1", "node": "N_OLD", "include_untaken": "1"})
    assert wrote == [("T1", "B0A", 0, "N_OLD")]       # 只清点名的那个节点


def test_validation_failure_skips_the_store_without_reading(monkeypatch):
    """受管仓校验不过(填错/读不到)⇒ 不清:"受管仓以外全清"建在错编号上 = 清空真仓。"""
    read, wrote = _wire(monkeypatch, [_store("T1")], {},
                        skipped={"T1": "T1:「维护仓库」填的 N_TYPO 不在该店发货节点列表里"})
    out = nc.run({"store": "T1"})
    assert "受管仓校验失败" in out and "N_TYPO" in out
    assert read == [] and wrote == []


def test_fleet_mode_covers_only_validated_managed_stores(monkeypatch):
    """不传 store:只处理填了「维护仓库」且校验通过的店;没配的店不碰,校验失败的首行点名。"""
    read, wrote = _wire(
        monkeypatch, [_store("T1"), _store("T2"), _store("T3")],
        {"T1": "N1"}, skipped={"T3": "读不到"}, words={"T3": "代理波动"},
        nodes={"T1": {"A": {"N1": 1, "OLD": 6}},
               "T2": {"B": {"OLD": 9}}})
    out = nc.run({})
    assert read == ["T1"]                               # T2 没配、T3 校验失败:都不读
    assert wrote == [("T1", "A", 0, "OLD")]
    first = out.splitlines()[0]
    assert "1 店" in first and "受管仓校验失败整店跳过 1 店:T3(代理波动)" in first


def test_fleet_mode_with_nothing_configured_says_so(monkeypatch):
    read, _ = _wire(monkeypatch, [_store("T1")], {})
    out = nc.run({})
    assert "没有填了「维护仓库」且校验通过的店" in out and read == []


def test_read_failure_is_retried_once_then_named(monkeypatch):
    """读库存失败按店维标准:串行补试一遍,仍失败首行点名归类词(写 0 幂等,重跑即补)。"""
    monkeypatch.setattr(nc.store_retry.time, "sleep", lambda s: None)
    _, wrote = _wire(monkeypatch, [_store("T1")], {"T1": "N_NEW"})
    calls = []

    def boom(store):
        calls.append(store["name"])
        raise RuntimeError("GET /v3/inventories 返回 500(店铺 T1): {}")

    monkeypatch.setattr(nc.inv_api, "list_inventory_nodes", boom)
    out = nc.run({"store": "T1"})
    assert calls == ["T1", "T1"]                        # 首轮 + 串行补试一次
    assert wrote == []
    assert "读库存失败 1 店:T1(沃尔玛500)" in out.splitlines()[0]


def test_plan_is_pure_and_orders_by_quantity():
    targets, per_node, untaken, unknown = nc.plan(
        {"A": {"M": 1, "X": 3}, "B": {"M": 1, "X": 8, "Y": 2}, "C": {"X": 5}},
        "M")
    assert targets == [("B", "X", 8), ("A", "X", 3), ("B", "Y", 2)]
    assert per_node == {"X": (3, 16), "Y": (1, 2)}
    assert untaken == {"C": 5} and unknown == 0
