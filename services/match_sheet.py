"""跟卖表(registry.MATCH_SHEET)读写积木(match_listing 与 feed_poll 共用)。

列契约(A~K,列序即契约,所有者建表 2026-08-07,单路飞书读替代旧 xlsx):
  A=UPC B=SKU C=售价 D=重量 E=店铺 F=跟卖状态 G=匹配GTIN
  H=上架时间 I=feedId J=feed结果 K=feed查询时间
  运营填 A/C/D/E;脚本填 B/F/G/H/I/J;J/K 由 feed_poll 反哺器按台账回填。

行状态机:
  F 空 + I 空 + A 有 UPC → 待处理(SPEC 预检 → 可跟卖才提交)
  F=可跟卖 + I 空       → 预检过但上轮未提交成功,重新排队
  F∈{需完整建品/目录无/预检失败/店铺不识别/…} → 终态跳过
                          (运营核对后清空 F 列即重新排队)
  I 有 + J 空/处理中    → 在途,反哺器按 ops.feed_items 回填 J/K
"""

import logging
from datetime import datetime

from api import feishu
from registry import resources
from services import feed_track, kpi

logger = logging.getLogger("services.match_sheet")

PENDING = ("", "处理中")
_WRITE_COLS = 11    # A~K


def read_rows() -> list[dict]:
    """输入:无 → 输出:表内全部数据行(含 1 基行号,表头在第 1 行)。"""
    sheet = resources.MATCH_SHEET
    total = feishu.sheet_row_count(sheet)
    if total < 2:
        return []
    values = feishu.sheet_values(sheet, f"A2:K{total}")
    rows = []
    for i, raw in enumerate(values):
        cells = [(str(c).strip() if c is not None else "") for c in raw] \
            + [""] * _WRITE_COLS
        (upc, sku, price, weight, store, status, gtin,
         list_time, feed_id, feed_result, check_time) = cells[:_WRITE_COLS]
        if not (upc or sku):
            continue
        rows.append({"rownum": i + 2, "upc": upc, "sku": sku, "price": price,
                     "weight": weight, "store": store, "status": status,
                     "gtin": gtin, "list_time": list_time, "feed_id": feed_id,
                     "feed_result": feed_result, "check_time": check_time})
    return rows


def write_rows(updates: list[tuple[int, list]], execute: bool = True) -> int:
    """输入:[(行号, [B..K 十列值])] → 输出:写入行数(dry-run 只打印)。

    只写程序列 B~K(A=UPC 是运营域,永不覆盖)。
    """
    if not updates:
        return 0
    if not execute:
        for rownum, vals in updates[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 B:K=%s", rownum, vals)
        if len(updates) > 20:
            logger.info("[DRY-RUN] …另有 %d 行回写省略", len(updates) - 20)
        return 0
    return feishu.sheet_write_ranges(
        resources.MATCH_SHEET,
        [(f"B{r}:K{r}", [[str(v) if v is not None else "" for v in vals]])
         for r, vals in updates])


def row_vals(r: dict) -> list:
    """输入:行 dict → 输出:B~K 十列当前值(定点改字段后整段回写用)。"""
    return [r["sku"], r["price"], r["weight"], r["store"], r["status"],
            r["gtin"], r["list_time"], r["feed_id"], r["feed_result"],
            r["check_time"]]


def sync_from_ledger() -> str | None:
    """输入:无 → 输出:回写摘要一行;表未配置或无在途行返回 None。

    feed_poll 反哺器:I 有 feedId 且 J 空/处理中的行,按 ops.feed_items
    台账落 J(成功/失败:码/未查到)与 K(查询时间)。纯读库零沃尔玛调用。
    """
    try:
        resources.MATCH_SHEET.require()
    except LookupError:
        return None
    rows = read_rows()
    pollable = [r for r in rows if r["feed_id"] and r["feed_result"] in PENDING]
    if not pollable:
        return None
    now = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d %H:%M")
    updates, cache = [], {}
    for r in pollable:
        fid = r["feed_id"]
        if fid not in cache:
            cache[fid] = feed_track.item_results(fid)
        st = cache[fid].get(r["sku"])
        if st is None or st[0] == "submitted":
            continue
        text = {"success": "成功", "failed": f"失败:{st[1]}" if st[1] else "失败",
                "missing": "未查到"}.get(st[0], "处理中")
        if text in PENDING:
            continue
        r["feed_result"], r["check_time"] = text, now
        updates.append((r["rownum"], row_vals(r)))
    if not updates:
        return f"跟卖表:在途 {len(pollable)} 行,台账尚无新终态"
    n = write_rows(updates)
    return f"跟卖表回填 {n} 行(在途 {len(pollable)})"
