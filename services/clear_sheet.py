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

# SKU 台账/轮询状态 → G 列文案(submitted/processing/unknown 均未落定)。
# **唯一出处在 feed_track**(2026-08-27 归一:此前四份拷贝 —— 本处 /
# maint_sheet 两份 / match_sheet)。这里保留同名再导出,因为
# workflows/product_clear:81 是 `RESULT_TEXT[outcome]` **直接下标**取
# (不是 .get):少一键就是 KeyError,改名会当场炸那条链。
RESULT_TEXT = feed_track.RESULT_TEXT


def read_rows() -> list[dict]:
    """输入:无 → 输出:表内全部数据行(含 1 基行号,表头在第 1 行)。"""
    sheet = resources.RETIRE_SHEET
    total = feishu.sheet_row_count(sheet)
    if total < 2:
        return []
    # 上界随表长增长 ⇒ 走唯一标准读通道(行方向分块 + 90221 对半兜底);
    # 行号取通道返回的 rownum,不再 i+2 手算:飞书只裁**范围尾部**的空行
    # (中段空行仍占位),所以块尾一空那块就少返几行,而下一块的行号照旧从
    # 块首起算 —— 按返回序号手算会把后面每一块整体上移
    pairs = feishu.sheet_values_rows(sheet, "A", "H", 2, total)
    rows = []
    for rownum, raw in pairs:
        cells = [(str(c).strip() if c is not None else "") for c in raw] + [""] * 8
        store, sku, action, reason, feed_id, op_date, result, error = cells[:8]
        if not (store or sku):
            continue
        rows.append({"rownum": rownum, "store": store, "sku": sku,
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
    except LookupError as e:
        # 未登记时**说出来**:静默返 None 会让 feed_poll 什么都不打印,
        # 看起来像"回写过了但飞书没变"(所有者 2026-08-09 实遇)
        return f"停用/删除表:表未登记,跳过回写({e})"
    rows = read_rows()
    pollable = [r for r in rows if r["feed_id"] and r["result"] in POLLABLE]
    if not pollable:
        return None
    updates, cache, descs = [], {}, {}
    for r in pollable:
        fid = r["feed_id"]
        if fid not in cache:
            cache[fid] = feed_track.item_results(fid)
            descs[fid] = feed_track.item_errors(fid)
        st = cache[fid].get(r["sku"])
        if st is None:
            continue        # 台账查无此 (feed, sku):不是本系统提交的,不动
        result = feed_track.text_of(st[0])
        # 报错列写「码 | 人话」:数字码本身不含可修的信息
        code = feed_track.merge_error(
            st[1], descs.get(fid, {}).get(r["sku"])) if result == "失败" else ""
        if result in POLLABLE:
            continue        # feed 未落定,下轮再看
        if result != r["result"] or code != r["error"]:
            updates.append((r["rownum"], fid, r["op_date"], result, code))
    if not updates:
        return f"停用/删除表:在途 {len(pollable)} 行,台账尚无新终态"
    n = writeback(updates)
    return f"停用/删除表回写 {n} 行(在途 {len(pollable)})"
