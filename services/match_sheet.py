"""跟卖表(registry.MATCH_SHEET)读写积木(match_listing 与 feed_poll 共用)。

列契约(A~K,列序即契约,所有者建表 2026-08-07,单路飞书读替代旧 xlsx):
  A=UPC B=SKU C=售价 D=重量 E=店铺 F=跟卖状态 G=匹配GTIN
  H=上架时间 I=feedId J=feed结果 K=feed查询时间
  运营填 A/C/D/E;脚本填 B/F/G/H/I/J;J/K 由 feed_poll 反哺器按台账回填。

行状态机(**待处理是包含式白名单**,唯一实现在 workflows/match_listing.run
的 todo 筛选;白名单之外的 F 值一律隐式终态):
  F 空 + I 空 + A 有 UPC → 待处理(SPEC 预检 → 可跟卖才提交)
  F=可跟卖 + I 空       → 预检过但上轮未提交成功,重新排队
  F 以「预检失败」开头 + I 空 → **也是待处理**,每轮自动重新预检
                          (2026-08-12 旧仓对照纠正:那多半是 SPEC 接口网络
                           抖动,当终态会把行永久停摆。本行状态机此前把它
                           列进终态档,与活的代码相反 —— 2026-08-27 照实改)
  F∈{需完整建品/目录无/店铺不识别/…} → 终态跳过
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
    # 上界随表长增长 ⇒ 走唯一标准读通道(行方向分块 + 90221 对半兜底);
    # 行号取通道返回的 rownum,不再 i+2 手算:飞书只裁**范围尾部**的空行
    # (中段空行仍占位),所以块尾一空那块就少返几行,而下一块的行号照旧从
    # 块首起算 —— 按返回序号手算会把后面每一块整体上移
    pairs = feishu.sheet_values_rows(sheet, "A", "K", 2, total)
    rows = []
    for rownum, raw in pairs:
        cells = [(str(c).strip() if c is not None else "") for c in raw] \
            + [""] * _WRITE_COLS
        (upc, sku, price, weight, store, status, gtin,
         list_time, feed_id, feed_result, check_time) = cells[:_WRITE_COLS]
        if not (upc or sku):
            continue
        rows.append({"rownum": rownum, "upc": upc, "sku": sku, "price": price,
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
    except LookupError as e:
        # 未登记时**说出来**:静默返 None 会让 feed_poll 什么都不打印,
        # 看起来像"回写过了但飞书没变"(所有者 2026-08-09 实遇)
        return f"跟卖表:表未登记,跳过回写({e})"
    rows = read_rows()
    pollable = [r for r in rows if r["feed_id"] and r["feed_result"] in PENDING]
    if not pollable:
        return None
    now = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d %H:%M")
    updates, cache, descs = [], {}, {}
    for r in pollable:
        fid = r["feed_id"]
        if fid not in cache:
            cache[fid] = feed_track.item_results(fid)
            descs[fid] = feed_track.item_errors(fid)
        st = cache[fid].get(r["sku"])
        if st is None or st[0] == "submitted":
            continue
        why = feed_track.merge_error(st[1], descs.get(fid, {}).get(r["sku"]))
        # 状态→中文的唯一出处在 feed_track;报错拼进「失败:{why}」是本表特有
        # 的形状(J 列一格既放结果又放报错,不像维护表另有报错列)
        text = feed_track.text_of(st[0], why)
        if text in PENDING:
            continue
        r["feed_result"], r["check_time"] = text, now
        updates.append((r["rownum"], row_vals(r)))
    if not updates:
        return f"跟卖表:在途 {len(pollable)} 行,台账尚无新终态"
    n = write_rows(updates)
    return f"跟卖表回填 {n} 行(在途 {len(pollable)})"
