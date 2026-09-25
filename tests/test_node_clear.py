"""node_clear:受管仓以外的节点库存清零(搬仓收尾,一次性)。

2026-09-25 所有者定稿:不调接口读库存、不逐条写 —— 从库里的分仓库存
(catalog.item_node_inventory)查出要清的「SKU × 旧仓」,按「店 × 旧仓」分批用
分仓库存 feed(MP_INVENTORY)写 0。判断条件(所有者定稿):**维护仓以外、有货就清**,
不看维护仓里有没有这个 SKU(「只清已接管」那道闸同日去掉)。保留:受管仓必须
校验过、维护仓永不清、节点身份未知不碰。
"""

import contextlib

from workflows import node_clear as nc


def _store(name="T1"):
    return {"name": name, "client_id": "cid", "client_secret": "sec",
            "proxy": None}


class _Cur:
    def __init__(self, rows_by_store, seen, boom):
        self._rows_by_store, self._seen, self._boom = rows_by_store, seen, boom
        self._rows = []

    def execute(self, sql, params):
        if self._boom:
            raise self._boom
        self._seen.append((sql, dict(params)))
        self._rows = self._rows_by_store.get(params["store"], [])

    def fetchall(self):
        return list(self._rows)


def _wire(monkeypatch, stores, managed, skipped=None, words=None, rows=None,
          outcome="submitted", boom=None):
    """接好店铺表、受管仓校验结果、库里的分仓库存与 feed 提交;返回 (查询记录, 提交记录)。"""
    monkeypatch.setattr(nc.stores_svc, "load_stores",
                        lambda filter_names=None: [
                            s for s in stores
                            if filter_names is None or s["name"] in filter_names])

    def managed_nodes(stores=None, conn=None, stats=None):
        if stats is not None:
            stats["words"] = dict(words or {})
        return dict(managed), dict(skipped or {})

    monkeypatch.setattr(nc.store_limits, "managed_nodes", managed_nodes)
    seen: list = []

    @contextlib.contextmanager
    def pg_conn():
        class _Conn:
            @contextlib.contextmanager
            def cursor(self):
                yield _Cur(rows or {}, seen, boom)
        yield _Conn()

    monkeypatch.setattr(nc.db, "pg_conn", pg_conn)
    sent: list = []

    def submit_feed(store, feed_type, entries, *, workflow="", defer_settle=False):
        sent.append((store["name"], feed_type, list(entries), workflow))
        o = outcome(entries) if callable(outcome) else outcome
        return [{"feed_id": f"F{len(sent)}", "count": len(entries), "outcome": o}]

    monkeypatch.setattr(nc.feeds, "submit_feed", submit_feed)
    return seen, sent


def test_criterion_old_node_with_stock_is_cleared_regardless_of_managed_row():
    """维护仓以外、有货就清(所有者定稿 2026-09-25),维护仓里有没有记录都一样;
    只有同步时没给 shipNode 的(空串)没有节点可写,不碰。"""
    targets, per_node, unknown = nc.plan([
        ("A", "OLD", 999),      # 维护仓有记录
        ("C", "OLD", 50),       # 维护仓没有记录 —— 也清(那道「只清已接管」的闸已去掉)
        ("D", "", 5),           # 同步时没给 shipNode → 不碰
    ])
    assert targets == {"OLD": ["A", "C"]}
    assert per_node == {"OLD": (2, 1049)}
    assert unknown == 1


def test_reads_the_db_not_the_inventory_api():
    """所有者定稿:库里就有分仓库存,不再单独调接口读库存、也不逐条写。"""
    assert not hasattr(nc, "inv_api")
    sql = nc._SQL_OLD_NODE_STOCK
    assert "catalog.item_node_inventory" in sql
    assert "missing_since IS NULL" in sql                # 只清目录里还在的码
    assert "avail_qty > 0" in sql                        # 旧仓有货
    assert "n.ship_node <> %(managed)s::text" in sql     # 维护仓本身永不清
    assert "EXISTS" not in sql                           # 不看维护仓有没有记录


def test_one_mp_inventory_batch_per_old_node(monkeypatch):
    """按「店 × 旧仓」分批:同一个 SKU 在两个旧仓都有货时,同一个 feed 里不能重复。"""
    seen, sent = _wire(monkeypatch, [_store("T1")], {"T1": "N_NEW"}, rows={
        "T1": [("A", "OLD1", 5), ("B", "OLD1", 2), ("C", "OLD1", 50),
               ("A", "OLD2", 7)]})
    out = nc.run({"store": "T1"})
    assert seen[0][1] == {"store": "T1", "managed": "N_NEW"}
    assert sent == [
        ("T1", "MP_INVENTORY", [{"sku": "A", "qty": 0, "ship_node": "OLD1"},
                                {"sku": "B", "qty": 0, "ship_node": "OLD1"},
                                {"sku": "C", "qty": 0, "ship_node": "OLD1"}],
         "node_clear"),
        ("T1", "MP_INVENTORY", [{"sku": "A", "qty": 0, "ship_node": "OLD2"}],
         "node_clear"),
    ]
    first = out.splitlines()[0]
    assert first.startswith("节点清零(受管仓以外):1 店,待清 4 条 SKU×旧仓 共 64 件")
    assert "已提交 4 条,结果由 feed_poll 回写" in first
    assert "旧仓 OLD1 3 个/57 件" in out and "旧仓 OLD2 1 个/7 件" in out
    assert "本轮不清" not in out
    assert "feed:F1,F2" in out


def test_dry_run_sends_nothing(monkeypatch):
    _, sent = _wire(monkeypatch, [_store("T1")], {"T1": "N_NEW"}, rows={
        "T1": [("A", "OLD", 999), ("B", "", 3)]})
    out = nc.run({"store": "T1", "dry_run": True})
    assert sent == []
    assert out.startswith("[DRY-RUN] 节点清零(受管仓以外):1 店,待清 1 条")
    assert "一条都没发" in out
    assert "节点身份未知" in out and "1 份" in out


def test_dedup_failed_and_unknown_outcomes_are_named(monkeypatch):
    """同一批还在处理中 → 防重拦下(不重复提交);结局不确定 → 交 feed_poll 对账,
    不要手工补发;被拒 → 点名。三种都要进摘要,不能只报"已提交"。"""
    outcomes = {"OLD1": "dedup", "OLD2": "unknown", "OLD3": "failed"}
    _wire(monkeypatch, [_store("T1")], {"T1": "N_NEW"}, rows={
        "T1": [("A", "OLD1", 5), ("B", "OLD2", 5), ("C", "OLD3", 5)]},
        outcome=lambda entries: outcomes[entries[0]["ship_node"]])
    out = nc.run({"store": "T1"})
    first = out.splitlines()[0]
    assert "已提交 0 条" in first and "防重拦下 1" in first
    assert "⚠ 被拒 1" in first and "⚠ 结局不确定 1" in first
    assert "pending 对账会接手" in out


def test_unconfigured_store_is_refused(monkeypatch):
    seen, sent = _wire(monkeypatch, [_store("T1")], {})
    out = nc.run({"store": "T1"})
    assert "没配「维护仓库」" in out
    assert seen == [] and sent == []


def test_validation_failure_skips_the_store(monkeypatch):
    """受管仓校验不过(填错/读不到)⇒ 不清:判据建在错编号上 = 把真仓当旧仓清空。"""
    seen, sent = _wire(monkeypatch, [_store("T1")], {},
                       skipped={"T1": "T1:「维护仓库」填的 N_TYPO 不在该店发货节点列表里"})
    out = nc.run({"store": "T1"})
    assert "受管仓校验失败" in out and "N_TYPO" in out
    assert seen == [] and sent == []


def test_fleet_mode_covers_only_validated_managed_stores(monkeypatch):
    """不传 store:只处理填了「维护仓库」且校验通过的店;没配的不碰,校验失败的首行点名。"""
    seen, sent = _wire(
        monkeypatch, [_store("T1"), _store("T2"), _store("T3")],
        {"T1": "N1"}, skipped={"T3": "读不到"}, words={"T3": "代理波动"},
        rows={"T1": [("A", "OLD", 6)], "T2": [("B", "OLD", 9)]})
    out = nc.run({})
    assert [p["store"] for _, p in seen] == ["T1"]     # T2 没配、T3 校验失败:都不查
    assert [s for s, *_ in sent] == ["T1"]
    first = out.splitlines()[0]
    assert "1 店" in first and "受管仓校验失败整店跳过 1 店:T3(代理波动)" in first


def test_fleet_mode_with_nothing_configured_says_so(monkeypatch):
    seen, _ = _wire(monkeypatch, [_store("T1")], {})
    out = nc.run({})
    assert "没有填了「维护仓库」且校验通过的店" in out and seen == []


def test_store_failure_is_retried_once_then_named(monkeypatch):
    """店级失败按店维标准:串行补试一遍,仍失败首行点名归类词(重跑即补)。"""
    monkeypatch.setattr(nc.store_retry.time, "sleep", lambda s: None)
    calls = []

    class _Boom(RuntimeError):
        def __init__(self):
            calls.append(1)
            super().__init__("GET /v3/inventories 返回 500(店铺 T1): {}")

    _, sent = _wire(monkeypatch, [_store("T1")], {"T1": "N_NEW"})

    @contextlib.contextmanager
    def pg_conn():
        raise _Boom()
        yield  # pragma: no cover

    monkeypatch.setattr(nc.db, "pg_conn", pg_conn)
    out = nc.run({"store": "T1"})
    assert len(calls) == 2                                # 首轮 + 串行补试一次
    assert sent == []
    assert "失败 1 店:T1(沃尔玛500)" in out.splitlines()[0]
