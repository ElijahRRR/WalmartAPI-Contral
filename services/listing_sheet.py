"""上架表(registry.LISTING_SHEET)读写积木(list_new 与 feed_poll 共用)。

列契约(21 列 A~U,所有者建表 2026-08-07):
  A=店铺 B=ASIN C=walmart上架标题 D=walmart_product_type E=审核结果 F=理由
  G=审核日期 H=amz价格 I=库存 J=walmart价格 K=是否上架 L=上架feedid
  M=上架日期 N=未上架理由 O=上架结果 P=上架失败理由 Q=feed查询日期
  R=真实walmart标题 S=真实walmart_product_type T=真实UPC U=UPC是否一致

⚠ **A/B 于 2026-08-16 被所有者对调**(原 A=ASIN B=店铺,现 A=店铺 B=ASIN)。
代码里没有一处按字母取 ASIN —— 列序的唯一出处是 `resources.LISTING_SHEET.columns`,
`read_rows()` 按它 zip;写入侧一律显式 range。表头再动只改 registry 那一条元组。

列权责(旧系统纪律,跨界写就是 bug):**A/B 人工域**(运营填店铺与 ASIN);
**C/D/E/F/G 审核域**(`product_audit -p from_sheet=1` 写,2026-08-16 所有者定稿
「审核直接读取上架表的 ASIN 与审核结果两列(结果为空就审核),然后回填 C、D、E、F、G」——
此前 D/E/F/G 记在人工域,那是并跑期旧审核链在填,现在由新审核链接管);
list_new 写 C/H/I/J(数据回显)与 K/L/M/N(提交结果);回执反哺器只写 O/P/Q;
L3 状态跟踪写 R~U。**唯一例外**:heal_unknown 自愈反哺器对 K=Unknown
的行可写 K~Q(所有者批复 2026-08-12——Unknown 是 list_new 自己写的
中间态,自愈是同一职责的收尾,不算跨界)。K 三态语义:Yes(已提交)/Unknown(结局不确定,
也算已上架不重复提交)/空或 No(待上架);O=SKU_LOCKED 由 sku_locked_heal
自愈链处理(RETIRE→24h→清列重上新 UPC;所有者纠正 2026-08-12:不是
永久跳过——但旧实证不先退役直接换 UPC 重发也失败,legacy_survey.md:1667)。

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


def write_data_cols(updates: list[tuple[int, list]], execute: bool = True) -> int:
    """输入:[(行号, [C 标题, H amz价, I 库存, J walmart价])] → 输出:写入行数。

    淘汰行数据回显(2026-08-12 旧仓对照接线):旧系统对拉到过数据的淘汰行
    也写标题与价库,运营在表上直接看到"为什么这行没上"的数字;
    只动 C 与 H:J,不碰 K~N(提交结果域)。
    """
    if not updates:
        return 0
    if not execute:
        return 0
    ranges = []
    for r, vals in updates:
        ranges.append((f"C{r}:C{r}", [[vals[0]]]))
        ranges.append((f"H{r}:J{r}", [vals[1:4]]))
    feishu.sheet_write_ranges(resources.LISTING_SHEET, ranges)
    return len(updates)


# 审核结论 → 上架表 E 列的取值。
# ⚠ **必须是 "pass"**:`list_new` 的领任务闸判的是
# `r["audit_result"].lower() == "pass"`。写 "approved" 那行就永远不会被上架领走,
# 而且不报错 —— 表面上"审过了",实际上再也上不去。
AUDIT_RESULT_CN = {"approved": "pass", "rejected": "reject",
                   "pending": "pending"}


def audit_targets() -> list[dict]:
    """输入:无 → 输出:待审行 [{rownum, asin, store}](ASIN 有值且**审核结果为空**)。

    所有者定稿 2026-08-16:「审核直接读取上架表的 ASIN 与审核结果两列
    (结果为空就审核)」。

    ⚠ **清空 E 列 ≠ 重审**(2026-08-17 更正:原注释写的"这是唯一的重审入口"
    是错的,与同日定稿的"from_sheet 非强审"直接打架)。清空只是让这行重新被
    **领取**;判不判由库里的 `audit_status` 说了算,已有结论的零 LLM 原样投影
    回来 —— 净效果是那格被填回同一个结论,看起来"重审了一遍",其实一次判定
    都没发生。表是**展示面**,不是判定入口(批复 #4 二次批复)。
    真重审走 CLI:`product_audit -p asins=<逗号分隔>`(点名强审)或
    `-p rerule=<规则码>`(改了某条规则后定点翻案)。

    ⚠ 按**字段名**取,不按列字母 —— A/B 已经被对调过一次(2026-08-16),
    再调一次也只改 `resources.LISTING_SHEET.columns` 那一条元组。
    """
    return [{"rownum": r["rownum"], "asin": r["asin"], "store": r.get("store")}
            for r in read_rows()
            if r.get("asin") and not str(r.get("audit_result") or "").strip()]


def write_audit_cols(updates: list[tuple[int, list]], execute: bool = True) -> int:
    """输入:[(行号, [C 标题, D PT, E 结果, F 理由, G 日期])] → 输出:写入行数。

    C~G 连续一段,一行一个 range。⚠ 只动这五列 —— H 之后是 list_new 与
    反哺器的域,跨界写就是 bug(见模块头注的列权责)。
    """
    if not updates:
        return 0
    if not execute:
        for rownum, vals in updates[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 C:G=%s", rownum, vals)
        if len(updates) > 20:
            logger.info("[DRY-RUN] …另有 %d 行省略", len(updates) - 20)
        return 0
    feishu.sheet_write_ranges(resources.LISTING_SHEET, [
        (f"C{r}:G{r}", [[("" if v is None else str(v)) for v in vals]])
        for r, vals in updates])
    return len(updates)


def write_reason(rownum: int, reason: str, execute: bool = True) -> None:
    """输入:行号 + 未上架理由 → 输出:无(只写 N 列)。"""
    if not execute:
        logger.info("[DRY-RUN] 将回写 第%d行 N=%s", rownum, reason)
        return
    feishu.sheet_write_ranges(resources.LISTING_SHEET,
                              [(f"N{rownum}:N{rownum}", [[reason]])])


def clear_for_relist(rownums: list[int], execute: bool = True) -> int:
    """输入:行号列表 → 输出:清列行数(K~M 与 O~Q 清空,N 写自愈标记)。

    sku_locked_heal 专用:RETIRE 回执成功 + 24h 冷却后把行恢复成"新行",
    下一轮 list_new 按正常闸门链领**新 UPC** 重上。N 列留一句人话,
    运营看得出这行为什么 K/L 突然空了。
    """
    if not rownums:
        return 0
    if not execute:
        logger.info("[DRY-RUN] 将清列重上 %d 行:%s", len(rownums), rownums[:20])
        return 0
    mark = "SKU_LOCKED已退役,冷却完毕待重上(自愈链)"
    ranges = []
    for r in rownums:
        ranges.append((f"K{r}:Q{r}", [["", "", "", mark, "", "", ""]]))
    feishu.sheet_write_ranges(resources.LISTING_SHEET, ranges)
    return len(rownums)


def classify_receipt(status: str, error_code: str) -> tuple[str, str]:
    """输入:feed_items 的 (status, error_code) → 输出:(O 上架结果, P 失败理由)。

    四集合+优先级(旧 reconcile 实证):SKU_LOCKED > 真SUCCESS > ASYNC >
    失败;SUCCESS 可以同时带 ingestionErrors——必须先看码再看状态。
    """
    code = (error_code or "").strip()       # 尾部 \t 实证
    if code == resources.WALMART_ERR_SKU_LOCKED:
        return "SKU_LOCKED", code           # sku_locked_heal:RETIRE→24h→重上
    if code in resources.WALMART_ERR_ASYNC_REVIEW:
        return "ASYNC_PENDING", ""          # 审核中假错误,绝不当失败重发
    if code in resources.WALMART_ERR_PROHIBITED:
        # 政策违禁(旧 O 列第五类,2026-08-12 抢救接线):永远不能上架,
        # 不进 FAILED 重试通道——重发也永远是拒,白烧 UPC 与配额
        return "PROHIBITED", code
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


# Unknown 自愈的两个判定源(所有者批复 2026-08-12,替代旧 sync_status_track
# 的 K 自愈半边;"反查真实状态"半边已由 catalog_sync 承接):
#   源① feed 回执台账:unknown 行多半没拿到 feedId(网络断在提交半途),但
#     ops.feed_log 的 pending 防重与后续轮询可能已把同 (店铺,SKU) 的提交落成
#     终态——按 (store, sku) 反查 MP_ITEM 最新台账行
#   源② 沃尔玛目录:catalog.walmart_items 在架(catalog_sync 每日全量;
#     product_events 的 list_submitted→item_appeared 时间线与之同源)
# 三层防误写(2026-06-09 全表误写事故语义,按新形态映射):
#   · 目录读空 → 源② 整体停用本轮(等价旧"空索引硬中止")
#   · **"查无"永不产生负向写**——K=No 只允许来自源① 的 feed 终态 FAILED,
#     目录里查不到只保持 Unknown(旧"单店 80% 熔断"与"48h 审核窗豁免"
#     防的就是负向误写,新形态下负向写根本不走目录源,天然满足)
_SQL_HEAL_RECEIPT = """
SELECT DISTINCT ON (f.store, f.sku) f.store, f.sku, f.feed_id, f.status,
       f.error_code, f.error_desc
FROM ops.feed_items f
JOIN unnest(%s::text[], %s::text[]) AS t(store, sku)
  ON f.store = t.store AND f.sku = t.sku
WHERE f.feed_type = 'MP_ITEM'
ORDER BY f.store, f.sku, f.submitted_at DESC
"""
_SQL_HEAL_ONLINE = """
SELECT w.store, w.sku
FROM catalog.walmart_items w
JOIN unnest(%s::text[], %s::text[]) AS t(store, sku)
  ON w.store = t.store AND w.sku = t.sku
WHERE w.missing_since IS NULL
"""


def heal_unknown() -> str | None:
    """feed_poll 反哺器:K=Unknown 的行按 feed 台账 + 沃尔玛目录自愈。

    Unknown 是"提交结局不确定"的防重态(不重复提交防双上架),但没人收尾
    就永久卡死 + UPC 永久占用(claimed 不释放)。收尾三条路:
      台账终态 success/审核中 → K=Yes L=feedid O/P/Q 回填,UPC 标已用
      台账终态 failed        → K=No O=FAILED(进 list_new 限次重试通道),
                               UPC 回收(rejected 路径)
      目录在架(无台账终态)→ K=Yes(L 保持原样),UPC 标已用
    其余保持 Unknown 等下一轮(摘要里报数,长期不愈人工看)。
    """
    try:
        resources.LISTING_SHEET.require()
    except LookupError as e:
        return f"上架表自愈:表未登记,跳过({e})"
    rows = read_rows()
    unknown = [r for r in rows if r["listed"].strip().lower() == "unknown"]
    if not unknown:
        return None
    today = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d")
    stores = [r["store"] for r in unknown]
    skus = [r["asin"] for r in unknown]
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_HEAL_RECEIPT, (stores, skus))
        receipts = {(s, k): (fid, st, code, desc)
                    for s, k, fid, st, code, desc in cur.fetchall()}
        cur.execute("SELECT 1 FROM catalog.walmart_items LIMIT 1")
        catalog_ok = cur.fetchone() is not None
        online: set[tuple[str, str]] = set()
        if catalog_ok:
            cur.execute(_SQL_HEAL_ONLINE, (stores, skus))
            online = set(cur.fetchall())
        cur.execute("SELECT store, asin, upc FROM catalog.upc_pool "
                    "WHERE status = 'claimed' AND asin = ANY(%s)",
                    (list(set(skus)),))
        claimed = {(s, a): u for s, a, u in cur.fetchall()}

    ranges = []
    n_yes = n_no = n_locked = n_stay = 0
    upc_used: list[tuple[str, str]] = []
    upc_release: list[str] = []
    for r in unknown:
        key, rn = (r["store"], r["asin"]), r["rownum"]
        rec = receipts.get(key)
        if rec and rec[1] in ("success", "failed", "missing"):
            fid, st, code, desc = rec
            o, p = classify_receipt(st, code)
            if p and desc:
                p = f"{p} | {desc}"[:900]
            if o == "SKU_LOCKED":
                # 只落 O,K 保持 Unknown:行交给 sku_locked_heal 自愈链
                ranges.append((f"O{rn}:Q{rn}", [[o, p, today]]))
                n_locked += 1
                continue
            if o == "FAILED":
                ranges.append((f"K{rn}:Q{rn}", [[
                    "No", "", r["list_date"],
                    "自愈:feed回执FAILED,重新排队", o, p, today]]))
                if key in claimed:
                    upc_release.append(claimed[key])
                n_no += 1
                continue
            if o == "PROHIBITED":
                # 政策违禁:K=No 但 O=PROHIBITED 让 list_new 永不再领
                ranges.append((f"K{rn}:Q{rn}", [[
                    "No", "", r["list_date"],
                    "自愈:政策违禁,永不重试", o, p, today]]))
                if key in claimed:
                    upc_release.append(claimed[key])
                n_no += 1
                continue
            # SUCCESS / SUCCESS_WITH_WARNING / ASYNC_PENDING / MISSING?
            # MISSING(feed 终态但明细查无此 SKU)= 高置信未达:按旧
            # RolledBack 语义当"没提交过"——K=No 且 O 留 MISSING 供追查
            if o == "MISSING":
                ranges.append((f"K{rn}:Q{rn}", [[
                    "No", "", r["list_date"],
                    "自愈:feed终态但明细无此SKU,按未达重排", o, p, today]]))
                if key in claimed:
                    upc_release.append(claimed[key])
                n_no += 1
                continue
            ranges.append((f"K{rn}:Q{rn}", [[
                "Yes", fid, r["list_date"] or today,
                f"自愈:feed回执{o}", o, p, today]]))
            if key in claimed:
                upc_used.append((claimed[key], r["asin"]))
            n_yes += 1
            continue
        if key in online:
            ranges.append((f"K{rn}:N{rn}", [[
                "Yes", r["feed_id"], r["list_date"] or today,
                "自愈:沃尔玛目录在线(catalog_sync)"]]))
            if key in claimed:
                upc_used.append((claimed[key], r["asin"]))
            n_yes += 1
            continue
        n_stay += 1

    if upc_used or upc_release:
        with db.pg_conn() as conn:
            if upc_used:
                upc_pool.mark_used(conn, upc_used)
            if upc_release:
                upc_pool.release(conn, upc_release, "rejected")
    if not ranges:
        note = "" if catalog_ok else "(⚠ 目录为空,在线判定本轮停用)"
        return f"上架表自愈:Unknown {len(unknown)} 行暂无可判定依据{note}"
    feishu.sheet_write_ranges(resources.LISTING_SHEET, ranges)
    line = (f"上架表自愈:Unknown {len(unknown)} 行 → 确认在线 {n_yes},"
            f"确认失败重排 {n_no}")
    if n_locked:
        line += f",SKU_LOCKED 移交自愈链 {n_locked}"
    if n_stay:
        line += f",继续观察 {n_stay}"
    if not catalog_ok:
        line += "(⚠ 目录为空,在线判定本轮停用)"
    return line


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
