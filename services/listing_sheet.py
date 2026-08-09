"""上架表(registry.LISTING_SHEET)读写积木(list_new 与 feed_poll 共用)。

列契约(21 列 A~U,所有者建表 2026-08-07):
  A=ASIN B=店铺 C=walmart上架标题 D=walmart_product_type E=审核结果 F=理由
  G=审核日期 H=amz价格 I=库存 J=walmart价格 K=是否上架 L=上架feedid
  M=上架日期 N=未上架理由 O=上架结果 P=上架失败理由 Q=feed查询日期
  R=真实walmart标题 S=真实walmart_product_type T=真实UPC U=UPC是否一致

列权责(旧系统纪律,跨界写就是 bug):A/B/D/E/F/G 人工域;list_new 写
C/H/I/J(数据回显)与 K/L/M/N(提交结果);回执反哺器只写 O/P/Q;
L3 状态跟踪写 R~U。K 三态语义:Yes(已提交)/Unknown(结局不确定,
也算已上架不重复提交)/空或 No(待上架);O=SKU_LOCKED 永久跳过
(L3 自愈链处理)。

回执分类(旧 reconcile 实证,"四集合+优先级"):
  优先级 SKU_LOCKED > 真SUCCESS(无码) > ASYNC(审核中假错误,绝不当失败
  重发) > 失败;错误码尾部可能带 \\t 必须 strip(旧全 miss 事故)。
"""

import logging
from datetime import datetime

from api import feishu
from registry import db, resources
from services import feed_track, kpi, upc_pool

logger = logging.getLogger("services.listing_sheet")

_COLS = 21          # A~U
PENDING_O = ("", "处理中", "ASYNC_PENDING")   # O 列这些值反哺器继续跟


def read_rows() -> list[dict]:
    """输入:无 → 输出:表内全部数据行(键=registry columns,含 rownum)。"""
    sheet = resources.LISTING_SHEET
    total = feishu.sheet_row_count(sheet)
    if total < 2:
        return []
    values = feishu.sheet_values(sheet, f"A2:U{total}")
    rows = []
    for i, raw in enumerate(values):
        cells = [(str(c).strip() if c is not None else "") for c in raw] \
            + [""] * _COLS
        d = dict(zip(resources.LISTING_SHEET.columns, cells[:_COLS]))
        if d["asin"] or d["store"]:
            d["rownum"] = i + 2
            rows.append(d)
    return rows


def write_submit_cols(updates: list[tuple[int, list]], execute: bool = True) -> int:
    """输入:[(行号, [C,H,I,J] + [K,L,M,N] 八值)] → 输出:写入行数。

    list_new 专用:一次写 C:D 之外的两段?列不连续,拆两个 range:
    C{r}(标题)与 H{r}:N{r}(H amz价 I 库存 J walmart价 K L M N)。
    """
    if not updates:
        return 0
    if not execute:
        for rownum, vals in updates[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 C+H:N=%s", rownum, vals)
        if len(updates) > 20:
            logger.info("[DRY-RUN] …另有 %d 行省略", len(updates) - 20)
        return 0
    ranges = []
    for r, vals in updates:
        title, rest = vals[0], vals[1:]
        ranges.append((f"C{r}:C{r}", [[title]]))
        ranges.append((f"H{r}:N{r}", [rest]))
    feishu.sheet_write_ranges(resources.LISTING_SHEET, ranges)
    return len(updates)


def write_reason(rownum: int, reason: str, execute: bool = True) -> None:
    """输入:行号 + 未上架理由 → 输出:无(只写 N 列)。"""
    if not execute:
        logger.info("[DRY-RUN] 将回写 第%d行 N=%s", rownum, reason)
        return
    feishu.sheet_write_ranges(resources.LISTING_SHEET,
                              [(f"N{rownum}:N{rownum}", [[reason]])])


def classify_receipt(status: str, error_code: str) -> tuple[str, str]:
    """输入:feed_items 的 (status, error_code) → 输出:(O 上架结果, P 失败理由)。

    四集合+优先级(旧 reconcile 实证):SKU_LOCKED > 真SUCCESS > ASYNC >
    失败;SUCCESS 可以同时带 ingestionErrors——必须先看码再看状态。
    """
    code = (error_code or "").strip()       # 尾部 \t 实证
    if code == resources.WALMART_ERR_SKU_LOCKED:
        return "SKU_LOCKED", code           # L3:RETIRE→24h→新 UPC 重上
    if code in resources.WALMART_ERR_ASYNC_REVIEW:
        return "ASYNC_PENDING", ""          # 审核中假错误,绝不当失败重发
    if status == "success":
        return ("SUCCESS", "") if not code else ("SUCCESS_WITH_WARNING", code)
    if status == "failed":
        return "FAILED", code
    if status == "missing":
        return "MISSING", "终态明细查无此 SKU"
    return "处理中", ""


def _mark_upc_conflicts(asins: list[str]) -> int:
    """输入:撞库的 ASIN 列表 → 输出:标记数。

    ERR_EXT_DATA_0101119:该 UPC 号在沃尔玛目录里已被占用——**UPC 永久弃用**
    (旧 upc_pool 实证),重上时领新号。按 sku 反查池中的 UPC。

    ⚠ 所有者澄清 2026-08-09:撞库**只说明这个 UPC 号被占了**,与"我们的产品
    是否已在沃尔玛上架"无关(UPC 被他人用掉是常态)。连撞多次只是运气差,
    照常领新号重试,不得据此推断该走跟卖。
    """
    if not asins:
        return 0
    n = 0
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT upc, sku FROM catalog.upc_pool "
                        "WHERE sku = ANY(%s) AND status <> 'conflict'",
                        (list(set(asins)),))
            found = cur.fetchall()
        for upc, sku in found:
            upc_pool.mark_conflict(conn, upc, sku)
            n += 1
    missing = len(set(asins)) - n
    if missing:
        logger.warning("UPC 撞库 %d 个在池中找不到对应 UPC(无法标冲突)", missing)
    return n


def sync_from_ledger() -> str | None:
    """feed_poll 反哺器:L 有 feedid 且 O 在途的行,按台账落 O/P/Q。"""
    try:
        resources.LISTING_SHEET.require()
    except LookupError as e:
        # 未登记时**说出来**:静默返 None 会让 feed_poll 什么都不打印,
        # 看起来像"回写过了但飞书没变"(所有者 2026-08-09 实遇)
        return f"上架表:表未登记,跳过回写({e})"
    rows = read_rows()
    pollable = [r for r in rows if r["feed_id"] and r["list_result"] in PENDING_O]
    if not pollable:
        return None
    today = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d")
    updates, cache = [], {}
    descs: dict[str, dict[str, str]] = {}
    codes: dict[str, dict[str, set]] = {}
    conflicts: list[tuple[str, str]] = []       # (asin, 全部码) → 正交标 UPC 池
    for r in pollable:
        fid = r["feed_id"]
        if fid not in cache:
            cache[fid] = feed_track.item_results(fid)
            descs[fid] = feed_track.item_errors(fid)
            codes[fid] = feed_track.item_codes(fid)
        st = cache[fid].get(r["asin"])      # 上架 sku=asin 约定
        if st is None or st[0] == "submitted":
            continue
        o, p = classify_receipt(st[0], st[1])
        # P 列写「码 + 人话」:光有 EXT_DATA_ERROR_507165… 这种数字码没法修
        desc = descs.get(fid, {}).get(r["asin"])
        if p and desc:
            p = f"{p} | {desc}"[:900]
        if o in ("处理中",) or (o == r["list_result"] and o != "ASYNC_PENDING"):
            continue
        # UPC 撞库**正交处置**(旧 reconcile 实证:与主分类独立,多错并存也要标)
        if resources.WALMART_ERR_UPC_CONFLICT in codes.get(fid, {}).get(
                r["asin"], set()):
            conflicts.append(r["asin"])
        updates.append((f"O{r['rownum']}:Q{r['rownum']}", [[o, p, today]]))
    n_conflict = _mark_upc_conflicts(conflicts)
    if not updates:
        line = f"上架表:在途 {len(pollable)} 行,台账尚无新终态"
        return line + (f";UPC 撞库标记 {n_conflict}" if n_conflict else "")
    n = feishu.sheet_write_ranges(resources.LISTING_SHEET, updates)
    line = f"上架表回填 {n} 行(在途 {len(pollable)})"
    if n_conflict:
        line += f";⚠ UPC 撞库 {n_conflict} 个已标冲突(永久弃用,重上会领新号)"
    return line
