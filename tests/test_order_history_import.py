"""订单历史导入回归:合并键拆 PO + 同(PO,SKU)合并 + 采购域字段只进 raw。"""

import contextlib
from datetime import datetime

import openpyxl

from services import order_lines as ol
from workflows import order_history_import as wf

_HEAD = ["合并键", "统计状态", "统一订单日期", "店铺名称", "SKU", "商品标题",
         "数量", "销售额USD", "采购成本CNY", "利润估算CNY", "退款原因"]

_ROWS = [
    ["108906521136562B0BC6ZBD7M", "有效销售", "2024-03-05", "1杨宜凡", "B0BC6ZBD7M",
     "Vinegar Two Pack", 1, "$23.10", "¥121.00", "¥20.37", ""],
    # 同 (PO, SKU) 第二行 → 必合并(qty/金额累加,身份规则 v3 定稿)
    ["108906521136562B0BC6ZBD7M", "有效销售", "2024-03-05", "1杨宜凡", "B0BC6ZBD7M",
     "Vinegar Two Pack", 2, "$46.20", "¥220.00", "¥62.74", ""],
    ["108906625394570B00I9P242U", "退款", datetime(2024, 3, 6), "3肖炜", "B00I9P242U",
     "Tamarind Paste", 1, "$7.25", "¥41.14", "-¥3.23", "质量问题"],
    # 合并键与 SKU 对不上 → 坏行,不硬猜
    ["XXXB0MISMATCH", "有效销售", "2024-03-07", "1杨宜凡", "B0BC6ZBD7M",
     "t", 1, "$1", "", "", ""],
]


class _Conn:
    def __init__(self):
        self.batches: list = []
        self._rc = 0

    def cursor(self):
        return self

    def executemany(self, sql, rows):
        self.batches.append((sql, list(rows)))
        self._rc = len(rows)

    @property
    def rowcount(self):
        return self._rc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _xlsx(tmp_path, rows, head=_HEAD):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(head)
    for r in rows:
        ws.append(r)
    p = tmp_path / "orders.xlsx"
    wb.save(p)
    return str(p)


def test_preview_stats(tmp_path):
    s = wf.run({"file": _xlsx(tmp_path, _ROWS)})
    assert "读 4 行" in s and "可解析 3" in s and "坏行 1" in s
    assert "2024-03-05 ~ 2024-03-06" in s
    assert "1杨宜凡×2" in s and "3肖炜×1" in s
    assert "合并 1 行" in s and "预览完毕" in s


def test_apply_builds_rows(tmp_path, monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(wf.db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([conn])))
    s = wf.run({"file": _xlsx(tmp_path, _ROWS), "apply": "1"})

    rows = [r for _, b in conn.batches for r in b]
    assert len(rows) == 2
    merged = next(r for r in rows if r["sku"] == "B0BC6ZBD7M")
    assert merged["po_id"] == "108906521136562"
    assert merged["order_line_id"] == ol.make_order_line_id(
        "108906521136562", "B0BC6ZBD7M")
    assert merged["qty"] == 3
    assert abs(merged["product_amount"] - 69.30) < 1e-6
    refund = next(r for r in rows if r["sku"] == "B00I9P242U")
    assert refund["refund_comments"] == "质量问题"
    assert '"统计状态": "退款"' in refund["raw"]       # 采购域口径只进 raw
    assert '"采购成本CNY": "¥41.14"' in refund["raw"]
    assert refund["order_date"].tzinfo is not None
    assert "ON CONFLICT DO NOTHING" in conn.batches[0][0]
    assert "入库 2 行" in s


def test_missing_required_header_refuses(tmp_path):
    s = wf.run({"file": _xlsx(tmp_path, [], head=["合并键", "SKU"])})
    assert s.startswith("⛔ 缺必需列")
