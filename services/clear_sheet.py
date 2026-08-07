"""停用/删除表(registry.RETIRE_SHEET)读写积木(product_clear 与 feed_poll 共用)。

列契约(A~H,列序即契约):
  A=store  B=sku  C=停用/删除  D=操作原因 | E=feedid  F=操作日期  G=结果  H=报错
  A~D 运营填,E~H 程序写(G 只放 成功/失败/处理中/未查到,报错码单独进 H)。

结果回写两条路共用本模块(所有者定稿 2026-08-07:结果落定交给轮询,
不依赖"记得再跑一次 product_clear"):
  product_clear --execute  现场轮询沃尔玛终态(feed_track.poll_feed)后回写
  feed_poll                全局轮询落台账后,从 ops.feed_items 反哺表格
                           (纯读库零沃尔玛调用)——谁先跑到谁回写,幂等
"""

import logging

from api import feishu
from registry import resources
from services import feed_track

logger = logging.getLogger("services.clear_sheet")

# G 列这些值才算在途(需要轮询/反哺);失败/未查到不自动重试
POLLABLE = ("", "处理中")
_WRITE_START = "E"

# SKU 台账/轮询状态 → G 列文案(submitted/processing/unknown 均未落定)
RESULT_TEXT = {"success": "成功", "failed": "失败", "missing": "未查到",
               "submitted": "处理中", "processing": "处理中", "unknown": "处理中"}


def read_rows() -> list[dict]:
    """输入:无 → 输出:表内全部数据行(含 1 基行号,表头在第 1 行)。"""
    sheet = resources.RETIRE_SHEET
    total = feishu.sheet_row_count(sheet)
    if total < 2:
        return []
    values = feishu.sheet_values(sheet, f"A2:H{total}")
    rows = []
    for i, raw in enumerate(values):
        cells = [(str(c).strip() if c is not None else "") for c in raw] + [""] * 8
        store, sku, action, reason, feed_id, op_date, result, error = cells[:8]
        if not (store or sku):
            continue
        rows.append({"rownum": i + 2, "store": store, "sku": sku,
                     "action": action, "reason": reason, "feed_id": feed_id,
                     "op_date": op_date, "result": result, "error": error})
    return rows


def writeback(updates: list[tuple[int, str, str, str, str]],
              execute: bool = True) -> int:
    """输入:[(行号, feedid, 日期, 结果, 报错)] → 输出:写入行数(dry-run 只打印)。"""
    if not updates:
        return 0
    if not execute:
        for rownum, fid, dt, res, err in updates[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 E=%s F=%s G=%s H=%s",
                        rownum, fid, dt, res, err)
        if len(updates) > 20:
            logger.info("[DRY-RUN] …另有 %d 行回写省略", len(updates) - 20)
        return 0
    return feishu.sheet_write_ranges(
        resources.RETIRE_SHEET,
        [(f"{_WRITE_START}{r}:H{r}", [[fid, dt, res, err]])
         for r, fid, dt, res, err in updates])


def sync_from_ledger() -> str | None:
    """输入:无 → 输出:回写摘要一行;表未配置或无在途行返回 None。

    feed_poll 用:在途行(E 有 feedid、G 空/处理中)按 ops.feed_items 台账
    落 G/H——台账已由全局轮询落定,这里纯读库,不调沃尔玛。
    """
    try:
        resources.RETIRE_SHEET.require()
    except LookupError:
        return None
    rows = read_rows()
    pollable = [r for r in rows if r["feed_id"] and r["result"] in POLLABLE]
    if not pollable:
        return None
    updates, cache = [], {}
    for r in pollable:
        fid = r["feed_id"]
        if fid not in cache:
            cache[fid] = feed_track.item_results(fid)
        st = cache[fid].get(r["sku"])
        if st is None:
            continue        # 台账查无此 (feed, sku):不是本系统提交的,不动
        result = RESULT_TEXT.get(st[0], "处理中")
        code = st[1] if result == "失败" else ""
        if result in POLLABLE:
            continue        # feed 未落定,下轮再看
        if result != r["result"] or code != r["error"]:
            updates.append((r["rownum"], fid, r["op_date"], result, code))
    if not updates:
        return f"停用/删除表:在途 {len(pollable)} 行,台账尚无新终态"
    n = writeback(updates)
    return f"停用/删除表回写 {n} 行(在途 {len(pollable)})"
