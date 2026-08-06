"""order_center_push 回归:单元值转换、六表行成形、键补齐、未登记跳过、单表失败聚合。"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from api import feishu
from registry import resources
from workflows import order_center_push as ocp

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

    def fake_sync(table, key_field, desired, *, delete_stale=True):
        captured[table.name] = {"key_field": key_field, "desired": desired,
                                "delete_stale": delete_stale}
        return (len(desired), 0, 0)

    monkeypatch.setattr(feishu, "sync_by_key", fake_sync)
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

    line, keys = ocp._push_sales(90)
    assert "1 行" in line and keys == {"ol_abc"}
    cap = captured["订单中心-销售订单"]
    assert cap["key_field"] == F_SALES.key == "order_line_id"
    assert cap["delete_stale"] is False     # 主订单表有关联,任何表不删行
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

    ocp._push_returns(90)
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

    ocp._push_perf()
    desired = captured["订单中心-绩效订单"]["desired"]
    assert set(desired) == {"PO1|otd", "PO2|自创指标"}
    d1 = desired["PO1|otd"]
    assert d1[F_PERF.metric] == "🚚 OTD"          # emoji 展示名契约
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

    ocp._push_settlement(90)
    d = captured["订单中心-对账明细"]["desired"]["ol_s"]
    assert d[F_SETTLE.settle_status] == "已退款"
    assert d[F_SETTLE.period] == "07292026"
    assert d[F_SETTLE.commission_rate] == 15.0
    assert d[F_SETTLE.settle_date] == int(
        datetime(2026, 7, 29, tzinfo=timezone.utc).timestamp() * 1000)


def test_push_keys_creates_only(monkeypatch):
    calls = []

    def fake_ensure(table, key_field, keys):
        calls.append((table.name, key_field, set(keys)))
        return 2

    monkeypatch.setattr(feishu, "ensure_keys", fake_ensure)
    lines = ocp._push_keys({"ol_a", "ol_b"})
    assert len(lines) == 2 and all("键补齐 2 行" in x for x in lines)
    assert calls[0][1] == "order_line_id" and calls[0][2] == {"ol_a", "ol_b"}
    names = {c[0] for c in calls}
    assert names == {"订单中心-主订单表", "订单中心-采购信息"}


def test_run_skips_unregistered_tables(monkeypatch):
    # 默认环境未登记 app_token/table_id → 六表全部走"跳过(未登记)",不报错
    monkeypatch.setattr(ocp, "_fetch", lambda sql, args: [])
    out = ocp.run({})
    assert out.count("跳过(未登记)") == 6


def test_run_rejects_unknown_table_param():
    assert "table 只接受" in ocp.run({"table": "nope"})


def test_run_partial_failure_raises_after_all_tables(monkeypatch):
    monkeypatch.setattr(ocp, "_fetch", lambda sql, args: [])
    seen = []

    def fake_sync(table, key_field, desired, *, delete_stale=True):
        seen.append(table.name)
        if table is resources.ORDER_SALES:
            raise feishu.FeishuError(123, "boom")
        return (0, 0, 0)

    monkeypatch.setattr(feishu, "sync_by_key", fake_sync)
    monkeypatch.setattr(feishu, "ensure_keys", lambda t, k, keys: 0)
    with pytest.raises(RuntimeError, match="部分表同步失败"):
        ocp.run({})
    # 销售表失败不挡后面的表(售后/绩效/对账仍同步)
    assert len(seen) == 4
