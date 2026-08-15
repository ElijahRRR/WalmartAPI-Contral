"""order_history_import — 两年订单 excel(采购分配合并总表)→ orders.order_lines(一次性)。

用法:
  python cli.py order_history_import -p file=/path/采购分配合并总表.xlsx            # 预览
  python cli.py order_history_import -p file=/path/采购分配合并总表.xlsx -p apply=1 # 入库

数据源(Q3 拍板 2026-08-12;表头样例所有者当日提供,精确匹配不做模糊猜列):
  合并键 | 统计状态 | 统一订单日期 | 店铺名称 | SKU | 商品标题 | 数量 |
  销售额USD | 采购成本CNY | 利润估算CNY | 退款原因

四条口径(改前先读):
  1. **没有独立 PO 列**:合并键 = PO号 + SKU 首尾拼接(实证
     108906521136562B0BC6ZBD7M)。PO = 合并键去掉 SKU 后缀;对不上/非纯数字
     计坏行报告,不硬猜。行标识仍走 services/order_lines.make_order_line_id
     (po+sku),与 order_sync 同源 ⇒ 与 API 45 天窗口重叠的单**天然同 ID**。
  2. **只插不改**:INSERT … ON CONFLICT DO NOTHING。API 拉的行权威,历史行
     永不覆盖;重复执行幂等。
  3. 统计状态/采购成本CNY/利润估算CNY 是采购域口径,不映射 order_lines
     业务列,**原样进 raw**(消费方按 raw->>'统计状态'='有效销售' 过滤);
     退款原因 → refund_comments;销售额USD($ 前缀)→ product_amount;
     sale_status 留 NULL(沃尔玛状态域,别拿记账状态污染)。
  4. 店铺名称原样入库("1杨宜凡" 旧命名可能与现凭证表店名不一致)——预览
     列出全部店名分布,与现店名对不上时由所有者定改名映射,导入器不猜。

时区:统一订单日期是日期(无时刻),按业务时区 Asia/Shanghai 的 0 点落
timestamptz(与日配额"北京 0 点重置"同一口径)。
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from registry import db
from services import order_lines as ol

DANGEROUS = False

logger = logging.getLogger("workflows.order_history_import")

_BATCH = 2000
_CN_TZ = timezone(timedelta(hours=8))

_REQUIRED = ("合并键", "SKU", "店铺名称", "统一订单日期", "数量", "销售额USD")

_INSERT_SQL = """
INSERT INTO orders.order_lines
    (order_line_id, store, po_id, line_number, sku, product_name, qty,
     order_date, product_amount, refund_comments, raw)
VALUES (%(order_line_id)s, %(store)s, %(po_id)s, '1', %(sku)s,
        %(product_name)s, %(qty)s, %(order_date)s, %(product_amount)s,
        %(refund_comments)s, %(raw)s::jsonb)
ON CONFLICT DO NOTHING
"""


def _cell(v) -> str:
    return str(v).strip() if v is not None else ""


def _money(v) -> float | None:
    """输入:'$23.10' / '¥121.00' / 数字 → 输出:float;解析失败 None(禁止 or 0)。"""
    if isinstance(v, (int, float)):
        return float(v)
    s = _cell(v).replace("$", "").replace("¥", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _date(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=_CN_TZ)
    s = _cell(v)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_CN_TZ)
        except ValueError:
            continue
    return None


def _split_po(merge_key: str, sku: str) -> str | None:
    """输入:合并键 + SKU → 输出:PO 号;拼接对不上或非纯数字返回 None。"""
    if not merge_key or not sku or not merge_key.endswith(sku):
        return None
    po = merge_key[: -len(sku)]
    return po if po.isdigit() and len(po) >= 10 else None


def _parse(headers: dict, row: tuple) -> tuple[dict | None, str | None]:
    """输入:表头索引 + 行 → 输出:(入库行, None) 或 (None, 坏行原因)。"""
    def g(name):
        i = headers.get(name)
        return row[i] if i is not None and i < len(row) else None

    sku = ol.norm_sku(_cell(g("SKU")))
    po = _split_po(_cell(g("合并键")), sku)
    if not po:
        return None, f"合并键/SKU 对不上:{_cell(g('合并键'))!r} vs {sku!r}"
    store = _cell(g("店铺名称"))
    if not store:
        return None, f"店铺名称为空(PO {po})"
    order_date = _date(g("统一订单日期"))
    if order_date is None:
        return None, f"订单日期解析失败:{_cell(g('统一订单日期'))!r}(PO {po})"
    qty = _money(g("数量"))
    raw = {name: _cell(g(name)) for name in headers}
    return {
        "order_line_id": ol.make_order_line_id(po, sku),
        "store": store,
        "po_id": po,
        "sku": sku,
        "product_name": _cell(g("商品标题")) or None,
        "qty": int(qty) if qty is not None else None,
        "order_date": order_date,
        "product_amount": _money(g("销售额USD")),
        "refund_comments": _cell(g("退款原因")) or None,
        "raw": json.dumps(raw, ensure_ascii=False),
    }, None


_HEADER_SCAN_ROWS = 30      # 表头前的说明/标题行:实测这份表首行是「订单数据合并概览」


def _locate_header(wb, sheet_name=None):
    """输入:workbook(+可选 sheet 名)→ 输出:(数据行迭代器, 表头映射, 位置串, 探测摘要)。

    **不假设表头在第 1 行、也不假设数据在第 1 个 sheet**:实测这份表首行是
    合并标题「订单数据合并概览」,而 openpyxl 的 read_only 迭代器只能前进,
    所以逐行扫到第一行"必需列全在"的即认表头,迭代器自然停在数据首行。

    找不到时把扫过的 sheet 与前几行原样回报——猜不出就把现场交出来,
    比抛一句"表头变了"有用。
    """
    sheets = [wb[sheet_name]] if sheet_name else list(wb.worksheets)
    probe: list[str] = []
    for ws in sheets:
        rows_iter = ws.iter_rows(values_only=True)
        seen: list[str] = []
        for i, row in enumerate(rows_iter):
            if i >= _HEADER_SCAN_ROWS:
                break
            cells = {_cell(h): j for j, h in enumerate(row or ()) if _cell(h)}
            if all(h in cells for h in _REQUIRED):
                return rows_iter, cells, f"{ws.title} 第 {i + 1} 行", probe
            if cells and len(seen) < 3:
                seen.append("|".join(list(cells)[:6]))
        probe.append(f"[{ws.title}] " + " ⏎ ".join(seen or ["(空)"]))
    return None, None, None, "; ".join(probe)


def run(params: dict) -> str:
    """输入:params(file 必填,apply/sheet 可选)→ 输出:预览或导入摘要。"""
    apply = str(params.get("apply", "")).lower() in {"1", "true", "yes"}
    path = str(params.get("file", "")).strip()
    if not path:
        return "⛔ 缺 -p file=<采购分配合并总表.xlsx 路径>"
    f = Path(path)
    if not f.is_file():
        return f"⛔ 文件不存在:{path}"

    from openpyxl import load_workbook
    wb = load_workbook(f, read_only=True, data_only=True)
    rows_iter, headers, where, probe = _locate_header(wb, params.get("sheet"))
    if headers is None:
        return ("⛔ 找不到表头行(必需列 " + "/".join(_REQUIRED) + ")。"
                f"扫过的 sheet 与前几行:{probe}"
                "——确认文件对不对;表头真变了先校准本文件 _REQUIRED/映射再跑")

    total = ok = inserted = 0
    bad: list[str] = []
    merged: dict[str, dict] = {}          # order_line_id → 行(同 PO 同 SKU 必合并)
    merge_hits = 0
    stores: dict[str, int] = {}
    dmin = dmax = None

    for row in rows_iter:
        if row is None or all(v is None or _cell(v) == "" for v in row):
            continue
        total += 1
        parsed, err = _parse(headers, row)
        if err:
            if len(bad) < 5:
                bad.append(err)
            continue
        ok += 1
        stores[parsed["store"]] = stores.get(parsed["store"], 0) + 1
        d = parsed["order_date"]
        dmin = d if dmin is None or d < dmin else dmin
        dmax = d if dmax is None or d > dmax else dmax
        key = parsed["order_line_id"]
        if key in merged:                  # 身份规则:同 (PO, SKU) 合并,qty/金额累加
            merge_hits += 1
            prev = merged[key]
            if parsed["qty"] is not None:
                prev["qty"] = (prev["qty"] or 0) + parsed["qty"]
            if parsed["product_amount"] is not None:
                prev["product_amount"] = (prev["product_amount"] or 0.0) + parsed["product_amount"]
        else:
            merged[key] = parsed
    wb.close()

    bad_total = total - ok
    lines = [f"表头定位于 {where}",
             f"读 {total} 行,可解析 {ok},坏行 {bad_total}"
             + (f"(样例:{' / '.join(bad)})" if bad else ""),
             f"日期范围 {dmin:%Y-%m-%d} ~ {dmax:%Y-%m-%d}" if dmin else "日期范围:无",
             f"店铺 {len(stores)} 个:" + ", ".join(
                 f"{s}×{n}" for s, n in sorted(stores.items(), key=lambda x: -x[1])[:15])]
    if merge_hits:
        lines.append(f"同 (PO,SKU) 合并 {merge_hits} 行(qty/金额累加)")

    if not apply:
        lines.append("⚠ 店铺名与现凭证表对不上时先定改名映射再 apply(本导入器不猜)")
        lines.append("预览完毕;-p apply=1 入库(ON CONFLICT DO NOTHING,"
                     "与 API 同源行标识,45 天窗口重叠单天然去重)")
        return ";".join(lines)

    rows = list(merged.values())
    with db.pg_conn() as conn:
        for i in range(0, len(rows), _BATCH):
            with conn.cursor() as cur:
                cur.executemany(_INSERT_SQL, rows[i:i + _BATCH])
                inserted += max(cur.rowcount, 0)
    lines.append(f"入库 {inserted} 行(已存在跳过 {len(rows) - inserted})")
    return ";".join(lines)
