"""order_center_push 回归:单元值转换、六表行成形、键补齐、未登记跳过、单表失败聚合。"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from api import feishu
from registry import resources
from services import order_center as ocp
from workflows import order_center_push as ocw

F_SALES = resources.ORDER_SALES.fields
F_PERF = resources.ORDER_PERF.fields
F_SETTLE = resources.ORDER_SETTLE.fields


def test_cell_conversions():
    dt = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    assert ocp._cell(dt) == int(dt.timestamp() * 1000)
    assert ocp._cell(date(2026, 8, 1)) == int(
        datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert ocp._cell(Decimal("1.50")) == 1.5
    assert ocp._cell(None) is None
    assert ocp._cell(True) is True          # bool 不能被当数字转
    assert ocp._cell("x") == "x"
    assert ocp._cell(3) == 3


def test_detail_desc():
    assert ocp._detail_desc({"PO #": "P1", "空": "", "Reason": "late"}) \
        == "PO #:P1; Reason:late"
    assert ocp._detail_desc(None) is None
    assert ocp._detail_desc({}) is None


def _capture_sync(monkeypatch):
    captured = {}

    def fake_sync(table, desired, reconcile=False):
        captured[table.name] = {"key_field": table.fields.key,
                                "desired": desired, "reconcile": reconcile}
        return (len(desired), 0, 0)

    monkeypatch.setattr(ocp, "_sync_stateful", fake_sync)
    return captured


_SALES_ROW = {
    "order_line_id": "ol_abc", "store": "T1", "po_id": "PO1",
    "line_number": "1", "sku": "SKU1", "product_name": "商品", "qty": 2,
    "sale_status": "Shipped", "audit_status": None,
    "status_date": datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc),
    "order_date": datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc),
    "est_ship_date": None, "est_delivery_date": None,
    "product_amount": Decimal("19.99"), "shipping_amount": Decimal("0"),
    "cancel_reason": None, "refund_amount": None, "refund_comments": None,
    "carrier": "USPS", "tracking_no": "9400", "tracking_url": "https://t",
    "ship_name": "N", "phone": "1", "address1": "a1", "address2": None,
    "city": "c", "state": "s", "postal_code": "z", "country": "US",
    "updated_at": datetime(2026, 8, 6, 1, 0, tzinfo=timezone.utc),
}


def test_push_sales_row_shape_and_no_delete(monkeypatch):
    captured = _capture_sync(monkeypatch)
    monkeypatch.setattr(ocp, "_fetch", lambda sql, args: [_SALES_ROW])

    line, keys = ocp.push_sales(90)
    assert "1 行" in line and keys == {"ol_abc"}
    cap = captured["订单中心-销售订单"]
    assert cap["key_field"] == F_SALES.key == "order_line_id"
    d = cap["desired"]["ol_abc"]
    assert d[F_SALES.po_id] == "PO1" and F_SALES.po_id == "采购订单号"
    assert d[F_SALES.order_date] == int(_SALES_ROW["order_date"].timestamp() * 1000)
    assert d[F_SALES.product_amount] == 19.99
    assert d[F_SALES.pulled_at] == int(_SALES_ROW["updated_at"].timestamp() * 1000)
    # None 字段必须保留在载荷里(省略=飞书保留旧值,送 null 才是清空)
    assert F_SALES.cancel_reason in d and d[F_SALES.cancel_reason] is None
    # 人工/采集列(脚本审核、亚马逊单价、主订单表…)绝不出现在载荷里
    assert "脚本审核" not in d and "亚马逊单价" not in d and "主订单表" not in d


def test_push_returns_key_is_rma_plus_line(monkeypatch):
    captured = _capture_sync(monkeypatch)
    row = {c: None for c in (
        "customer_order_id", "line_number", "sku", "return_status",
        "refund_status", "return_method", "refund_mode", "is_keep_it",
        "refund_total", "return_reason", "return_comment", "return_by",
        "return_created", "last_modified", "customer_name", "customer_email",
        "qty", "refunded_qty", "carrier", "tracking_no", "order_date")}
    row.update({"return_order_id": "RMA1", "order_line_id": "ol_x",
                "store": "T1", "po_id": "PO1"})
    monkeypatch.setattr(ocp, "_fetch", lambda sql, args: [row])

    ocp.push_returns(90)
    cap = captured["订单中心-售后订单"]
    assert cap["key_field"] == "唯一键"
    assert set(cap["desired"]) == {"RMA1|ol_x"}
    assert cap["desired"]["RMA1|ol_x"]["order_line_id"] == "ol_x"


def test_push_perf_shaping(monkeypatch):
    captured = _capture_sync(monkeypatch)
    rows = [
        {"store": "T1", "po_id": "PO1", "metric": "otd", "order_line_id": "ol_1",
         "first_period": "2026-08-01", "last_period": "2026-08-06",
         "periods_seen": 3, "ever_accountable": True, "still_active": True,
         "order_date": None, "detail": {"PO #": "PO1", "Late": "yes"},
         "status": "违规",
         "last_seen_at": datetime(2026, 8, 6, tzinfo=timezone.utc)},
        {"store": "T1", "po_id": "PO2", "metric": "自创指标", "order_line_id": None,
         "first_period": "2026-07-01", "last_period": "2026-07-01",
         "periods_seen": 1, "ever_accountable": False, "still_active": False,
         "order_date": None, "detail": None, "status": None,
         "last_seen_at": None},
    ]
    monkeypatch.setattr(ocp, "_fetch", lambda sql, args: rows)

    ocp.push_perf()
    desired = captured["订单中心-绩效订单"]["desired"]
    assert set(desired) == {"PO1|otd", "PO2|自创指标"}
    d1 = desired["PO1|otd"]
    assert d1[F_PERF.metric] == "OTD"    # 单选预设无 emoji(v1 init_bitable 实证)
    assert d1[F_PERF.status] == "影响中"
    assert d1[F_PERF.period_span] == "2026-08-01 ~ 2026-08-06(共 3 期)"
    assert "PO #:PO1" in d1[F_PERF.description]
    d2 = desired["PO2|自创指标"]
    assert d2[F_PERF.metric] == "自创指标"          # 未知指标原样
    assert d2[F_PERF.status] == "已滚出窗口"
    assert d2[F_PERF.period_span] == "2026-07-01(共 1 期)"
    assert d2[F_PERF.detail] is None


def test_push_settlement_latest_period_fields(monkeypatch):
    captured = _capture_sync(monkeypatch)
    row = {
        "order_line_id": "ol_s", "store": "T1", "po_id": "PO1",
        "line_number": "1", "net_amount": Decimal("0"),
        "product_amount": Decimal("52.68"), "commission_amount": Decimal("-7.9"),
        "settle_status": "已退款", "last_settle_date": date(2026, 7, 29),
        "order_date": None, "period": "07292026",
        "commission_rate": Decimal("15"), "original_commission": Decimal("-7.9"),
        "commission_saving": Decimal("0"), "incentive": None,
        "updated_at": datetime(2026, 8, 6, tzinfo=timezone.utc),
    }
    monkeypatch.setattr(ocp, "_fetch", lambda sql, args: [row])

    ocp.push_settlement(90)
    d = captured["订单中心-对账明细"]["desired"]["ol_s"]
    assert d[F_SETTLE.settle_status] == "已退款"
    assert d[F_SETTLE.period] == "07292026"
    assert d[F_SETTLE.commission_rate] == 15.0
    assert d[F_SETTLE.settle_date] == int(
        datetime(2026, 7, 29, tzinfo=timezone.utc).timestamp() * 1000)


def test_push_keys_creates_only_missing(monkeypatch):
    import dataclasses
    monkeypatch.setattr(resources, "ORDER_MAIN", dataclasses.replace(
        resources.ORDER_MAIN, app_token="appT", table_id="tblMain"))
    monkeypatch.setattr(resources, "ORDER_PURCHASE", dataclasses.replace(
        resources.ORDER_PURCHASE, app_token="appT", table_id="tblPur"))
    created, saved = [], []
    monkeypatch.setattr(ocp, "_load_state",
                        lambda t: {"ol_a": ("r1", None)})
    monkeypatch.setattr(ocp, "_save_state",
                        lambda t, entries: saved.append((t.name, entries)))
    monkeypatch.setattr(feishu, "batch_create",
                        lambda t, fl: (created.append((t.name, fl)),
                                       [f"n{i}" for i in range(len(fl))])[1])
    lines = ocp.push_keys({"ol_a", "ol_b"})
    # 状态里已有 ol_a → 只建 ol_b;record_id 回存本地状态
    assert len(lines) == 2 and all("键补齐 1 行" in x for x in lines)
    assert created[0][1] == [{"order_line_id": "ol_b"}]
    assert saved[0][1] == [("ol_b", "n0", None)]
    assert {c[0] for c in created} == {"订单中心-主订单表", "订单中心-采购信息"}


def test_run_skips_unregistered_tables(monkeypatch):
    # 默认环境未登记 app_token/table_id → 六表全部走"跳过(未登记)",不报错
    monkeypatch.setattr(ocp, "_fetch", lambda sql, args: [])
    out = ocw.run({})
    assert out.count("跳过(未登记)") == 6


def test_run_rejects_unknown_table_param():
    assert "table 只接受" in ocw.run({"table": "nope"})


def test_run_partial_failure_raises_after_all_tables(monkeypatch):
    monkeypatch.setattr(ocp, "_fetch", lambda sql, args: [])
    seen = []

    def fake_sync(table, desired, reconcile=False):
        seen.append(table.name)
        if table is resources.ORDER_SALES:
            raise feishu.FeishuError(123, "boom")
        return (0, 0, 0)

    monkeypatch.setattr(ocp, "_sync_stateful", fake_sync)
    monkeypatch.setattr(ocp, "push_keys",
                        lambda keys, reconcile=False: ["keys ok"])
    # ⚠ 手动补推入口**失败必须记 failed**;业务链顺手写的那次相反(push_after
    # 只告警)—— 数据已经在 PG,不该让飞书拖累一轮数据拉取的成败判定
    with pytest.raises(RuntimeError, match="部分表同步失败"):
        ocw.run({})
    # 销售表失败不挡后面的表(售后/绩效/对账仍同步)
    assert len(seen) == 4


# ── 字段类型自适应(2026-08-06 首跑实证:整批原子,一列类型错=全表推不上)──────


def test_conv_per_field_type():
    assert ocp._conv(2, "07292026") == 7292026.0     # 数字列收字符串:能转就转
    assert ocp._conv(2, "abc") is None               # 转不动置空,不炸整批
    assert ocp._conv(2, True) == 1
    assert ocp._conv(5, 1754300000000) == 1754300000000
    assert ocp._conv(5, "2026-08-01") is None        # 日期列只收毫秒
    assert ocp._conv(7, True) is True and ocp._conv(7, "yes") is None
    assert ocp._conv(15, "https://t") == {"link": "https://t", "text": "https://t"}
    assert ocp._conv(3, True) == "是" and ocp._conv(3, False) == "否"
    assert ocp._conv(1, 123) == "123"
    assert ocp._conv(4, "a") == ["a"]
    assert ocp._conv(9999, {"x": 1}) == {"x": 1}     # 未知类型原样试写
    for t in (1, 2, 5, 7, 15):
        assert ocp._conv(t, None) is None            # null 清空语义全类型保留


def test_adapt_rows_types_and_guards(monkeypatch):
    from registry import resources as res
    t = res.ORDER_PERF
    f = t.fields
    created = []
    monkeypatch.setattr(feishu, "create_field",
                        lambda table, name, ftype=1: created.append(name))
    monkeypatch.setattr(feishu, "list_fields", lambda table: [
        {"field_name": f.key, "type": 1},
        {"field_name": f.metric, "type": 3},          # 单选
        {"field_name": f.accountable, "type": 3},     # 单选收 bool → 是/否
        {"field_name": f.pulled_at, "type": 5},
        {"field_name": f.detail, "type": 20},         # 公式列 → 整列跳过
    ])
    desired = {"k1": {f.key: "k1", f.metric: "🚚 OTD", f.accountable: True,
                      f.pulled_at: 1754300000000, f.detail: "{}",
                      "不存在的列": "x"}}
    out = ocp._adapt_rows(t, desired)
    row = out["k1"]
    assert row[f.accountable] == "是"
    assert row[f.pulled_at] == 1754300000000
    assert f.detail not in row and "不存在的列" not in row
    assert created == [ocp._HASH_FIELD]      # 指纹列缺失时自动创建


def test_adapt_rows_missing_key_field_raises(monkeypatch):
    from registry import resources as res
    monkeypatch.setattr(feishu, "list_fields",
                        lambda table: [{"field_name": "别的列", "type": 1}])
    with pytest.raises(feishu.FeishuError, match="缺少键字段"):
        ocp._adapt_rows(res.ORDER_RETURNS, {"k": {"唯一键": "k"}})


# ── 本地状态驱动同步(日常零拉表)────────────────────────────────────────────


def _state_env(monkeypatch, state: dict):
    """搭 _sync_stateful 的假环境:内存状态 + 捕获的飞书写调用。"""
    calls = {"create": None, "update": None, "saved": [], "dropped": []}
    monkeypatch.setattr(ocp, "_adapt_rows",
                        lambda t, d: {k: dict(v) for k, v in d.items()})
    monkeypatch.setattr(ocp, "_load_state", lambda t: dict(state))
    monkeypatch.setattr(ocp, "_save_state",
                        lambda t, entries: calls["saved"].append(entries))
    monkeypatch.setattr(ocp, "_drop_state",
                        lambda t: calls["dropped"].append(t.name))
    monkeypatch.setattr(feishu, "batch_create",
                        lambda t, fl: (calls.__setitem__("create", fl),
                                       [f"n{i}" for i in range(len(fl))])[1])
    monkeypatch.setattr(feishu, "batch_update",
                        lambda t, ups: (calls.__setitem__("update", ups),
                                        len(ups))[1])
    # 日常路径绝不许拉表
    monkeypatch.setattr(feishu, "list_records",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("日常同步不应拉飞书表")))
    return calls


def test_sync_stateful_local_diff_no_pull(monkeypatch):
    t = resources.ORDER_PERF
    same = {"perf_key": "k1", "x": 1}
    h_same = feishu._row_hash(same, ocp._HASH_FIELD)
    calls = _state_env(monkeypatch, {
        "k1": ("r1", h_same),      # 指纹一致 → 跳过
        "k2": ("r2", "stale"),     # 指纹不同 → 按 record_id 更新
    })
    c, u, s = ocp._sync_stateful(t, {
        "k1": dict(same),
        "k2": {"perf_key": "k2", "x": 9},
        "k3": {"perf_key": "k3"},  # 状态没有 → 新建
    })
    assert (c, u, s) == (1, 1, 1)
    assert calls["update"][0]["record_id"] == "r2"
    assert calls["create"][0]["perf_key"] == "k3"
    # 新建行的 record_id 与指纹都回存本地状态
    assert calls["saved"][0][0][0] == "k3" and calls["saved"][0][0][1] == "n0"


def test_sync_stateful_bootstraps_when_state_empty(monkeypatch):
    t = resources.ORDER_PERF
    calls = _state_env(monkeypatch, {})
    payload = {"perf_key": "k1", "x": 1}
    h = feishu._row_hash(payload, ocp._HASH_FIELD)
    monkeypatch.setattr(feishu, "list_records", lambda table, field_names: [
        {"record_id": "rA", "fields": {"perf_key": "k1",
                                       ocp._HASH_FIELD: h}}])
    c, u, s = ocp._sync_stateful(t, {"k1": dict(payload)})
    # 首轮:拉表重建映射 → k1 已存在且指纹一致 → 零写入
    assert (c, u, s) == (0, 0, 1)
    assert calls["create"] is None and calls["update"] is None


def test_bootstrap_rejects_wrong_table_id(monkeypatch):
    """登记错表守卫(2026-08-14 从已删除的 feishu.sync_by_key 移植过来)。

    表里明明有行、却一行都读不出键字段 ⇒ 大概率 .env 里 table_id 填错、指向了
    别人的表。此时状态是空的,下一步会把窗口内全部行当"缺失"新建——**把人家的
    表写坏且不可逆**。必须炸,不能静默当成空表。
    """
    t = resources.ORDER_PERF
    calls = _state_env(monkeypatch, {})
    monkeypatch.setattr(feishu, "list_records", lambda table, field_names: [
        {"record_id": "x1", "fields": {"别的字段": "v"}},
        {"record_id": "x2", "fields": {"别的字段": "w"}}])
    with pytest.raises(feishu.FeishuError, match="疑似 table_id 登记错表"):
        ocp._sync_stateful(t, {"k1": {"perf_key": "k1"}})
    assert calls["create"] is None and calls["update"] is None   # 一个字都没写


def test_bootstrap_empty_table_is_not_an_error(monkeypatch):
    """守卫的边界:表**本身就是空的**(0 行)是正常首轮,不能误判成登记错表。"""
    t = resources.ORDER_PERF
    _state_env(monkeypatch, {})
    monkeypatch.setattr(feishu, "list_records", lambda table, field_names: [])
    c, u, s = ocp._sync_stateful(t, {"k1": {"perf_key": "k1"}})
    assert (c, u, s) == (1, 0, 0)            # 照常建行


def test_sync_stateful_write_failure_drops_state(monkeypatch):
    t = resources.ORDER_PERF
    calls = _state_env(monkeypatch, {"k1": ("r1", "stale")})

    def boom(*a, **k):
        raise feishu.FeishuError(500, "down")
    monkeypatch.setattr(feishu, "batch_update", boom)
    with pytest.raises(feishu.FeishuError):
        ocp._sync_stateful(t, {"k1": {"perf_key": "k1"}})
    assert calls["dropped"] == [t.name]      # 清状态,下轮全量对账自愈


# ── 拆家:各链跑完自己写飞书(工作项 B,所有者定稿 2026-08-16)────────────────

def test_each_chain_pushes_its_own_table_and_nobody_else_s():
    """「已对接飞书表的,执行完就写,不要做成单独的」——四条链各写各的。

    映射收在 services 一处(BY_WORKFLOW),不在各工作流里抄字符串:
    加一张表时漏改的那条链会**静默不写**,而且看不出来。
    """
    import inspect

    from workflows import order_sync, perf_problems, returns_sync, settlement_sync
    assert set(ocp.BY_WORKFLOW) == {"order_sync", "returns_sync",
                                    "perf_problems", "settlement_sync"}
    # 五张表恰好被瓜分干净,不重不漏
    covered = [t for v in ocp.BY_WORKFLOW.values() for t in v]
    assert sorted(covered) == sorted(ocp.TABLES)
    assert len(covered) == len(set(covered))
    for m, key in ((order_sync, "order_sync"), (returns_sync, "returns_sync"),
                   (perf_problems, "perf_problems"),
                   (settlement_sync, "settlement_sync")):
        src = inspect.getsource(m)
        assert f'BY_WORKFLOW["{key}"]' in src, m.__name__
        assert "push_after(" in src, m.__name__


def test_push_after_never_fails_the_data_pull(monkeypatch):
    """⚠ 数据已经落 PG 了,飞书写失败不该让那一轮数据拉取记成 failed。

    但**必须说出来** —— 否则表现成"跑成功了可表格没动"(所有者 2026-08-16
    在日报上实遇过这种:日志三处写着未配置,摘要却报成功)。
    """
    def boom(names, days=90, reconcile=False):
        raise RuntimeError("飞书 502")

    monkeypatch.setattr(ocp, "push_tables", boom)
    out = ocp.push_after(("sales",))
    assert "⚠ 飞书投影失败" in out and "飞书 502" in out
    assert "数据已在 PG" in out and "order_center_push" in out    # 补推指路

    monkeypatch.setattr(ocp, "push_tables",
                        lambda names, days=90, reconcile=False: (["sales:ok"],
                                                                 ["sales"]))
    out2 = ocp.push_after(("sales",))
    assert "⚠ 失败 1 张" in out2 and "-p table=sales" in out2


def test_keys_branch_passes_both_sql_params(monkeypatch):
    """⚠ 搬家前 keys 分支只给 _SALES_SQL 传了 (days,),而它有**两个**占位符。

    `order_center_push -p table=keys` 一跑就是参数个数不匹配 —— 拆函数时
    顺手修的,用例钉住别再退回去。
    """
    seen = []
    monkeypatch.setattr(ocp, "_fetch",
                        lambda sql, args: (seen.append(args), [])[1])
    monkeypatch.setattr(ocp, "push_keys", lambda keys, reconcile=False: ["ok"])
    ocp.push_tables(("keys",), days=30)
    assert len(seen) == 1 and len(seen[0]) == 2 and seen[0][0] == 30
    assert ocp._SALES_SQL.count("%s") == 2
