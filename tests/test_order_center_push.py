"""order_center_push 回归:单元值转换、四表行成形、未登记跳过、单表失败聚合。"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from api import feishu
from registry import resources
from workflows import order_center_push as ocp

F_SALES = resources.ORDER_SALES.fields
F_PERF = resources.ORDER_PERF.fields


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


def _capture_sync(monkeypatch):
    captured = {}

    def fake_sync(table, key_field, desired):
        captured[table.name] = {"key_field": key_field, "desired": desired}
        return (len(desired), 0, 0)

    monkeypatch.setattr(feishu, "sync_by_key", fake_sync)
    return captured


def test_push_sales_row_shape(monkeypatch):
    captured = _capture_sync(monkeypatch)
    dt = datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc)
    row = {
        "order_line_id": "ol_abc", "store": "T1", "po_id": "PO1",
        "line_number": "1", "customer_order_id": "CO1", "sku": "SKU1",
        "product_name": "商品", "qty": 2, "sale_status": "Shipped",
        "status_date": dt, "order_date": dt,
        "product_amount": Decimal("19.99"), "shipping_amount": Decimal("0"),
        "cancel_reason": None, "refund_amount": None,
        "carrier": "USPS", "tracking_no": "9400", "tracking_url": "https://t",
        "return_status": None, "refund_status": None, "return_total": None,
        "perf_metrics": "otd;returns", "settled_net": Decimal("12.34"),
        "settle_status": "已入账", "audit_status": None,
    }
    monkeypatch.setattr(ocp, "_fetch", lambda sql, args: [row])

    out = ocp._push_sales(90)
    assert "1 行" in out
    cap = captured["订单中心-销售订单"]
    assert cap["key_field"] == F_SALES.key
    d = cap["desired"]["ol_abc"]
    assert d[F_SALES.key] == "ol_abc"
    assert d[F_SALES.order_date] == int(dt.timestamp() * 1000)
    assert d[F_SALES.product_amount] == 19.99
    assert d[F_SALES.settled_net] == 12.34
    # None 字段必须保留在载荷里(省略=飞书保留旧值,送 null 才是清空)
    assert F_SALES.cancel_reason in d and d[F_SALES.cancel_reason] is None
    assert F_SALES.return_status in d and d[F_SALES.return_status] is None


def test_push_perf_label_and_sku_fallback(monkeypatch):
    captured = _capture_sync(monkeypatch)
    rows = [
        {"store": "T1", "po_id": "PO1", "metric": "otd", "line_sku": "S1",
         "event_sku": None, "product_name": "甲", "first_period": "2026-08-01",
         "last_period": "2026-08-06", "periods_seen": 3,
         "ever_accountable": True, "still_active": True},
        # 未回填订单行的老单:sku 退回报表原始行
        {"store": "T1", "po_id": "PO2", "metric": "自创指标", "line_sku": None,
         "event_sku": "S2", "product_name": None, "first_period": "2026-07-01",
         "last_period": "2026-07-02", "periods_seen": 1,
         "ever_accountable": False, "still_active": False},
    ]
    monkeypatch.setattr(ocp, "_fetch", lambda sql, args: rows)

    ocp._push_perf()
    desired = captured["订单中心-绩效订单"]["desired"]
    assert set(desired) == {"PO1|otd", "PO2|自创指标"}
    assert desired["PO1|otd"][F_PERF.metric] == "🚚 OTD"      # emoji 展示名契约
    assert desired["PO2|自创指标"][F_PERF.metric] == "自创指标"  # 未知指标原样
    assert desired["PO1|otd"][F_PERF.sku] == "S1"
    assert desired["PO2|自创指标"][F_PERF.sku] == "S2"
    assert desired["PO1|otd"][F_PERF.still_active] is True


def test_run_skips_unregistered_tables(monkeypatch):
    # 默认环境未登记 app_token/table_id → 四表全部走"跳过(未登记)",不报错
    monkeypatch.setattr(ocp, "_fetch", lambda sql, args: [])
    out = ocp.run({})
    assert out.count("跳过(未登记)") == 4


def test_run_rejects_unknown_table_param():
    assert "table 只接受" in ocp.run({"table": "nope"})


def test_run_partial_failure_raises_after_all_tables(monkeypatch):
    monkeypatch.setattr(ocp, "_fetch", lambda sql, args: [])
    seen = []

    def fake_sync(table, key_field, desired):
        seen.append(table.name)
        if table is resources.ORDER_SALES:
            raise feishu.FeishuError(123, "boom")
        return (0, 0, 0)

    monkeypatch.setattr(feishu, "sync_by_key", fake_sync)
    with pytest.raises(RuntimeError, match="部分表同步失败"):
        ocp.run({})
    # 销售表失败不挡后面三张表
    assert len(seen) == 4
