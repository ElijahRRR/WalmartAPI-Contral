"""维护记录工作表(registry.MAINT_SHEET)读写积木(maintenance / variant_offset_cleanup
与 feed_poll 共用)。

列契约(A~I,列序即契约,所有者建表 2026-08-07):
  A=店铺 B=SKU C=动作 D=旧值 E=新值 F=feedid G=日期 H=结果 I=报错

流水账语义(区别于 clear_sheet 的运营驱动表):只追加不改行,程序是唯一写入方。
  提交时 append:feed 路径 F=真 feedid、H=处理中;PUT 同步路径 F="sync"、
  H=成功/失败 当场落定。
  C 列取值:标题/价格/库存(maintenance)、删除(variant_offset)
  ——删除类走同一张表同一个反哺器,不另建表(所有者问 2026-08-09)。
  feed 路径结果由 feed_poll 反哺器(sync_from_ledger)按 ops.feed_items 回填 H/I。

水位(ops.cursors,name='maint_sheet'):{"next_row": 下一空行, "unresolved_from":
最早未落定行}。表会长年累积(多维表格 5 万行上限装不下才用电子表格),
反哺器只扫 [unresolved_from, next_row) 区间,不做全表读。
"""

import json
import logging

from api import feishu
from registry import db, resources
from services import feed_track

logger = logging.getLogger("services.maint_sheet")

_SYNC_MARK = "sync"     # PUT 同步路径的 F 列伪标记(旧系统 'sync:200' 语义的收敛)
_PENDING = ("", "处理中")
_CURSOR = "maint_sheet"


def _load_cursor(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM ops.cursors WHERE name = %s", (_CURSOR,))
        row = cur.fetchone()
    return dict(row[0]) if row else {"next_row": 2, "unresolved_from": 2}


def _save_cursor(conn, value: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.cursors (name, value) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = now()",
            (_CURSOR, json.dumps(value)))


def _find_next_empty(start: int) -> int:
    """输入:候选起始行 → 输出:确认为空的首行(防水位漂移覆盖已有数据)。"""
    grid = feishu.sheet_row_count(resources.MAINT_SHEET)
    row = start
    while row <= grid:
        end = min(row + 49, grid)
        vals = feishu.sheet_values(resources.MAINT_SHEET, f"A{row}:A{end}")
        got = [(str(c[0]).strip() if c and c[0] is not None else "")
               for c in (vals + [[None]] * (end - row + 1))[:end - row + 1]]
        for i, v in enumerate(got):
            if not v:
                return row + i
        row = end + 1
    return row      # 网格已满,append_records 会先扩行


def append_records(rows: list[tuple]) -> int:
    """输入:[(店铺, sku, 动作, 旧值, 新值, feedid, 日期, 结果, 报错)]
    → 输出:写入行数。只追加;水位存 ops.cursors,起点先验空防覆盖。"""
    if not rows:
        return 0
    sheet = resources.MAINT_SHEET.require()
    with db.pg_conn() as conn:
        cur_state = _load_cursor(conn)
    start = _find_next_empty(int(cur_state.get("next_row", 2)))
    feishu.sheet_ensure_rows(sheet, start + len(rows))
    feishu.sheet_write_ranges(sheet, [
        (f"A{start}:I{start + len(rows) - 1}",
         [[str(c) if c is not None else "" for c in r] for r in rows])])
    cur_state["next_row"] = start + len(rows)
    cur_state.setdefault("unresolved_from", 2)
    with db.pg_conn() as conn:
        _save_cursor(conn, cur_state)
    return len(rows)


def sync_from_ledger() -> str | None:
    """输入:无 → 输出:回写摘要一行;表未配置或无未落定行返回 None。

    feed_poll 反哺器:扫 [unresolved_from, next_row) 区间内 F=真 feedid 且
    H 空/处理中的行,按 ops.feed_items 台账落 H(结果)/I(报错);
    已全落定的前缀推进水位。纯读库,零沃尔玛调用。
    """
    try:
        resources.MAINT_SHEET.require()
    except LookupError:
        return None
    with db.pg_conn() as conn:
        cur_state = _load_cursor(conn)
    lo, hi = int(cur_state.get("unresolved_from", 2)), int(cur_state.get("next_row", 2))
    if lo >= hi:
        return None
    values = feishu.sheet_values(resources.MAINT_SHEET, f"A{lo}:I{hi - 1}")
    updates, cache, descs = [], {}, {}
    new_lo, prefix_done = lo, True
    for i, raw in enumerate(values):
        cells = [(str(c).strip() if c is not None else "") for c in raw] + [""] * 9
        sku, fid, result = cells[1], cells[5], cells[7]
        rownum = lo + i
        pending = fid and fid != _SYNC_MARK and result in _PENDING
        if not pending:
            if prefix_done:
                new_lo = rownum + 1
            continue
        if fid not in cache:
            cache[fid] = feed_track.item_results(fid)
            descs[fid] = feed_track.item_errors(fid)
        st = cache[fid].get(sku)
        if st is None:
            # 台账查无此 (feed, sku):不该发生(程序是唯一写入方),
            # 告警后视为已处理,不许它永久卡住水位
            logger.warning("维护记录第 %d 行 feed=%s sku=%s 台账查无,跳过",
                           rownum, fid, sku)
            if prefix_done:
                new_lo = rownum + 1
            continue
        if st[0] == "submitted":
            prefix_done = False         # feed 未落定,水位停在这里
            continue
        text = {"success": "成功", "failed": "失败",
                "missing": "未查到"}.get(st[0], "处理中")
        # 报错列写「码 | 人话」(改价/改库存/改标题/清库存共用这一列)
        err = feed_track.merge_error(
            st[1], descs.get(fid, {}).get(sku)) if text == "失败" else ""
        updates.append((f"H{rownum}:I{rownum}", [[text, err]]))
        if prefix_done:
            new_lo = rownum + 1
    n = feishu.sheet_write_ranges(resources.MAINT_SHEET, updates) if updates else 0
    if new_lo != lo:
        cur_state["unresolved_from"] = new_lo
        with db.pg_conn() as conn:
            _save_cursor(conn, cur_state)
    if not updates:
        return f"维护记录:未落定 {hi - lo} 行,台账尚无新终态" if hi > lo else None
    return f"维护记录回填 {n} 行(扫描区间 {lo}~{hi - 1})"
