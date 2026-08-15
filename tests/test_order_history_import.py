"""order_history_import 回归:表头映射、合并键切分、口径计数、只补不覆盖。"""

import contextlib
from datetime import datetime, timezone

from services import order_lines
from workflows import order_history_import as ohi

_HEADER = ["合并键", "统计状态", "统一订单日期", "店铺名称", "SKU", "商品标题",
           "数量", "销售额USD", "采购成本CNY", "利润估算CNY", "退款原因"]


def _row(key, date, store, sku, title="T", qty="1", amt="23.1"):
    return [key, "有效销售", date, store, sku, title, qty, amt, "121", "20.3", ""]


def test_header_map_covers_required_and_ignores_cost_columns():
    mapping, unmapped = ohi.map_header(_HEADER)
    by_field = {f: i for i, f in mapping.items()}
    assert by_field == {"merge_key": 0, "order_date": 2, "store": 3, "sku": 4,
                        "product_name": 5, "qty": 6, "product_amount": 7}
    # 采购成本/利润/退款原因/统计状态**不导**(所有者定稿:只要销售)
    assert set(unmapped) == {"统计状态", "采购成本CNY", "利润估算CNY", "退款原因"}


def test_split_po_uses_suffix_not_fixed_width():
    """⚠ 生产文件里有 PO 与 SKU 之间夹 8 个空格的行(15 万行中 1 行)。

    按"前 15 位"切会切出带空格的 PO,进而算出与 order_sync 不同的
    order_line_id —— 同一张单在库里变成两行,而且不报错。
    """
    assert ohi.split_po("108906521136562B0BC6ZBD7M", "B0BC6ZBD7M") == "108906521136562"
    assert ohi.split_po("108909730063166        B08M4D1GMT",
                        "B08M4D1GMT") == "108909730063166"
    assert ohi.split_po("108906521136562XXX", "B0BC6ZBD7M") is None   # 不以 SKU 结尾
    assert ohi.split_po("B0BC6ZBD7M", "B0BC6ZBD7M") is None           # 切完只剩空
    assert ohi.split_po("", "X") is None and ohi.split_po("X", "") is None


def test_order_line_id_matches_order_sync():
    """身份必须与 API 侧同算法 —— 否则将来 order_sync 拉到同一张单会插出第二行。"""
    rows, _ = ohi.parse_rows(
        _HEADER, [_row("108906521136562B0BC6ZBD7M", "2024-03-05 00:00:00",
                       "1杨宜凡", "B0BC6ZBD7M")])
    assert rows[0]["order_line_id"] == order_lines.make_order_line_id(
        "108906521136562", "B0BC6ZBD7M")


def test_date_becomes_utc_midnight_not_cn_midnight():
    """按 UTC 零点入库:CN 是 UTC+8,UTC 零点 +8h 仍是同一日历日,于是这个日期在
    UTC 和 CN 两种时区下读出来都是同一天;换成 CN 零点会在 UTC 下退成前一天。"""
    rows, _ = ohi.parse_rows(
        _HEADER, [_row("1" * 15 + "SKU1", datetime(2024, 3, 5), "S", "SKU1")])
    od = rows[0]["order_date"]
    assert od == datetime(2024, 3, 5, tzinfo=timezone.utc)
    assert od.astimezone(timezone.utc).strftime("%Y-%m-%d") == "2024-03-05"


def test_每类跳过单独计数():
    """静默丢行会让"导入了 12 万条"看起来正常,而少的那 3 万条永远没人发现。"""
    rows, stat = ohi.parse_rows(_HEADER, [
        _row("1" * 15 + "A", "2026-03-01", "S", "A"),
        _row("坏键", "2026-03-01", "S", "ZZZ"),                     # bad_key
        _row("2" * 15 + "B", "不是日期", "S", "B"),                  # bad_date
        _row("3" * 15 + "C", "2026-03-01", "S", "C", qty="abc"),    # bad_num
        _row("1" * 15 + "A", "2026-03-01", "S", "A"),               # dup_in_file
        [None, None, None, None, None, None, None, None],           # 空行不计
    ])
    assert len(rows) == 1
    assert stat == {"total": 5, "bad_key": 1, "bad_date": 1, "bad_num": 1,
                    "before_since": 0, "dup_in_file": 1}


def test_since_filters_by_date():
    rows, stat = ohi.parse_rows(_HEADER, [
        _row("1" * 15 + "A", "2024-03-05", "S", "A"),
        _row("2" * 15 + "B", "2026-03-05", "S", "B"),
    ], since="2026-02-15")
    assert [r["sku"] for r in rows] == ["B"] and stat["before_since"] == 1


def _wire(monkeypatch, existing=(), rows=None):
    """DB 换假的:_existing 返回给定集合,记录 executemany 的批次。"""
    seen = {"batches": []}

    class _Cur:
        def execute(self, sql, params=None):
            self._ids = params["ids"]

        def fetchall(self):
            return [(i,) for i in self._ids if i in set(existing)]

        def executemany(self, sql, chunk):
            seen["batches"].append(list(chunk))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    monkeypatch.setattr(ohi.db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([_Conn()])))
    monkeypatch.setattr(ohi, "_read_sheet",
                        lambda path, sheet: (_HEADER, iter(rows or [])))
    return seen


def test_dry_run_writes_nothing_and_reports_overlap(monkeypatch):
    hit = order_lines.make_order_line_id("1" * 15, "A")
    seen = _wire(monkeypatch, existing=[hit], rows=[
        _row("1" * 15 + "A", "2026-03-01", "S", "A"),
        _row("2" * 15 + "B", "2026-03-02", "S", "B"),
    ])
    out = ohi.run({"file": "x.xlsx"})
    assert "DRY-RUN" in out and seen["batches"] == []
    assert "库中已有 1" in out and "待新增 1" in out


def test_apply_only_inserts_new_rows(monkeypatch):
    """只补不覆盖:库里已有的行连 INSERT 都不发(ON CONFLICT DO NOTHING 是第二道)。"""
    hit = order_lines.make_order_line_id("1" * 15, "A")
    seen = _wire(monkeypatch, existing=[hit], rows=[
        _row("1" * 15 + "A", "2026-03-01", "S", "A"),
        _row("2" * 15 + "B", "2026-03-02", "S", "B"),
    ])
    out = ohi.run({"file": "x.xlsx", "apply": "1"})
    sent = [r for b in seen["batches"] for r in b]
    assert [r["sku"] for r in sent] == ["B"]
    assert sent[0]["source"] == order_lines.HISTORY_SOURCE
    assert "已写入 1 行" in out


def test_fixed_columns_are_in_the_sql():
    """销售状态一律 Delivered、行号填空串(NOT NULL)——所有者定稿的两个固定值。"""
    assert "'Delivered'" in ohi._INSERT and "''" in ohi._INSERT
    assert "ON CONFLICT DO NOTHING" in ohi._INSERT
    # 不带冲突目标:主键与 UNIQUE(po_id, sku) 两条约束都要覆盖
    assert "ON CONFLICT (" not in ohi._INSERT


def test_missing_header_refuses_to_guess(monkeypatch):
    _wire(monkeypatch, rows=[])
    monkeypatch.setattr(ohi, "_read_sheet",
                        lambda path, sheet: (["随便", "什么"], iter([])))
    out = ohi.run({"file": "x.xlsx", "apply": "1"})
    assert "表头缺字段" in out


def test_history_rows_excluded_from_feishu_push():
    """历史残缺行不推飞书;且过滤必须用 IS DISTINCT FROM。

    `source <> '历史数据'` 对 NULL 求值为 NULL —— 而绝大多数行的 source 是
    NULL,那样写会把**全部 API 行**一起过滤掉,飞书表直接变空。
    """
    from workflows import order_center_push as ocp
    assert "source IS DISTINCT FROM %s" in ocp._SALES_SQL
    assert "source <> " not in ocp._SALES_SQL
    # source 留在覆盖列里:API 拉到真行后标记自动摘掉,该行回到推送流
    assert "source" in order_lines._ORDER_LINE_COLS
