"""上架表(registry.LISTING_SHEET)读写积木(list_new 与 feed_poll 共用)。

**列定位:按表头名,不按列字母**(所有者要求 2026-09-02:「以后再调整列顺序
也能准确写入」)。`layout()` 每进程读一次表头行,拿 registry 登记的
`LISTING_SHEET.headers`(字段名 → 中文表头)去认列,得到 字段名 → 列字母 的映射;
本文件所有读写的 range 都由它算出来,**源码里一个写死的列字母都没有**
(守门:tests/test_sku_guard.py::test_listing_sheet_has_no_hardcoded_column_letters)。
表头缺失/重复 = fail-closed 直接抛错拒绝读写(宁可不跑,也不写错列);
表头行里多出登记之外的列只告警(所有者随时会加自己的列)。

列契约(21 列,所有者表头重排 2026-09-02;字母是**今天的**位置,不是契约):
  A=店铺 B=ASIN C=SKU D=walmart上架标题 E=walmart_product_type F=审核结果
  G=类别 H=具体内容 I=审核日期 J=amz价格 K=库存 L=walmart价格 M=是否上架
  N=上架feedid O=上架日期 P=未上架理由 Q=上架结果 R=报错 S=feed查询日期
  T=登记日期 U=查询编码

⚠ **T 登记日期 / U 查询编码是人工填写列,本模块永不写**(读全宽时顺带读到,
没有任何判定消费它们)。⚠ **G 类别列本模块也不写**:审核链的「类别」由
另一条 PR 接线,本文件只写「具体内容」(audit_reason)。

列权责(旧系统纪律,跨界写就是 bug):**店铺/ASIN 人工域**(运营填);
**标题/PT/审核结果/类别/具体内容/审核日期 审核域**(`product_audit -p from_sheet=1`
写,2026-08-16 所有者定稿「审核直接读取上架表的 ASIN 与审核结果两列(结果为空
就审核),然后回填标题、PT、审核结果、理由、审核日期」;**具体内容还有一条单列
通道** `write_audit_notes` —— 审不了的行(库里没数据)在审核结果留空的前提下
只写具体内容说明原因,见那个函数的头注);
list_new 写 标题/amz价/库存/walmart价(数据回显)与 是否上架/feedid/上架日期/
未上架理由(提交结果),提交时**同一次**把 SKU 写进 C 列;回执反哺器只写
上架结果/报错/feed查询日期。**唯一例外**:heal_unknown 自愈反哺器对
是否上架=Unknown 的行可写 是否上架~feed查询日期(所有者批复 2026-08-12——
Unknown 是 list_new 自己写的中间态,自愈是同一职责的收尾,不算跨界)。
是否上架 三态语义:Yes(已提交)/Unknown(结局不确定,也算已上架不重复提交)/
空或 No(待上架);上架结果=SKU_LOCKED 由 sku_locked_heal
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

_APPEND_BLOCK = 500 # 单次写飞书的行数上限(一次裹上千行会被 90202 拒;
                    # 与 maint_sheet 同值,那边是实遇被拒后定的)
PENDING_O = ("", "处理中", "ASYNC_PENDING")   # 上架结果列这些值反哺器继续跟
#: 表头行往登记列数之外多扫几列 —— 只为发现"登记之外的新列"并告警。
#: 多出的列不算错(所有者随时会加自己的列),但要说出来,免得下一个人
#: 以为程序看得见它。
_HEADER_SCAN_SLACK = 5

#: 进程内列布局缓存:字段名 → **1-based 列号**。None = 还没读过表头行。
#: 每进程读一次(表头不会在一轮跑里被改);测试与"表头刚改完"用
#: `reset_layout_cache()` 清掉重读。
_LAYOUT: dict[str, int] | None = None


def reset_layout_cache() -> None:
    """输入:无 → 输出:无。清空进程内列布局缓存,下次用时重读表头行。

    给两种场景:① 单测(每个用例互不影响);② 所有者刚改完表头、想让
    长驻进程重认一次列。正常一轮跑不需要调 —— 表头在一轮里不会变。
    """
    global _LAYOUT
    _LAYOUT = None


def _read_header_row() -> list[str]:
    """输入:无 → 输出:表头行的单元格文本(已 strip,右侧多扫几列)。"""
    sheet = resources.LISTING_SHEET.require()
    width = len(sheet.headers) + _HEADER_SCAN_SLACK
    last = feishu._col_letter(width)
    got = feishu.sheet_values_small(sheet, f"A1:{last}1")
    raw = (got or [[]])[0] or []
    return [(str(c).strip() if c is not None else "") for c in raw]


def _index_map() -> dict[str, int]:
    """输入:无 → 输出:{字段名: 1-based 列号}(每进程读一次表头行认列)。

    **fail-closed**:registry 登记的表头只要缺一个、或在表头行里出现两次,
    直接抛 LookupError 拒绝一切读写 —— 宁可这一轮不跑,也不能把标题写进
    SKU 列(2026-09-02 重排之前那套硬编码字母,插一列就是全体静默错位)。
    表头行里多出登记之外的列**只告警**:所有者随时会加自己的列,那不是错。
    """
    global _LAYOUT
    if _LAYOUT is not None:
        return _LAYOUT
    want = resources.LISTING_SHEET.headers
    if not want:
        raise LookupError("上架表未登记 headers(字段名→中文表头):"
                          "按表头名定位列是本模块的前提,先补 registry")
    cells = _read_header_row()
    seen: dict[str, list[int]] = {}
    for i, text in enumerate(cells, 1):
        if text:
            seen.setdefault(text, []).append(i)
    missing = [h for h in want.values() if h not in seen]
    dupes = [h for h in want.values() if len(seen.get(h, ())) > 1]
    extra = [t for t in seen if t not in set(want.values())]
    if extra:
        logger.warning("上架表表头有登记之外的列 %s —— 程序看不见它们"
                       "(要接线先登记 registry.LISTING_SHEET.headers)", extra)
    if missing or dupes:
        logger.warning("上架表表头对不上登记:缺失 %s;重复 %s", missing, dupes)
        raise LookupError(
            f"上架表表头与 registry 登记对不上(缺失 {missing};重复 {dupes})——"
            f"本轮**拒绝一切读写**:列认不准就会把值写进别人的列,而且不报错。"
            f"请核对飞书表头行或 registry.LISTING_SHEET.headers")
    _LAYOUT = {f: seen[h][0] for f, h in want.items()}
    return _LAYOUT


def layout() -> dict[str, str]:
    """输入:无 → 输出:{字段名: 列字母}(按表头名认列,每进程读一次表头行)。

    **本文件全部 range 的唯一来源**。所有者再挪列顺序,代码一行不改;
    表头缺失/重复则抛错拒绝读写(见 `_index_map` 的 fail-closed 口径)。
    """
    return {f: feishu._col_letter(i) for f, i in _index_map().items()}


def _ranges(row_from: int, fields: list[str],
            rows_vals: list[list]) -> list[tuple[str, list[list]]]:
    """输入:起始行号 + 字段序列 + 每行的等长值序列 → 输出:[(A1范围, 值矩阵)]。

    **列字母在这里出生,别处一律不许写字母**。列号相邻的字段粘成一段
    (少一个飞书请求位),不相邻就拆多段 —— 所有者在中间插一列,同一批
    写入会自动从一段变两段,值一个都不会落到隔壁列。
    """
    idx = _index_map()
    unknown = [f for f in fields if f not in idx]
    if unknown:
        raise LookupError(f"上架表没有这些字段:{unknown}(先登记 registry)")
    segs: list[list[int]] = []          # 每段 = fields 里的下标序列
    for pos, f in enumerate(fields):
        if segs and idx[f] == idx[fields[segs[-1][-1]]] + 1:
            segs[-1].append(pos)
        else:
            segs.append([pos])
    row_to = row_from + len(rows_vals) - 1
    out = []
    for seg in segs:
        a = feishu._col_letter(idx[fields[seg[0]]])
        b = feishu._col_letter(idx[fields[seg[-1]]])
        out.append((f"{a}{row_from}:{b}{row_to}",
                    [[vals[pos] for pos in seg] for vals in rows_vals]))
    return out


def _row_ranges(updates: list[tuple[int, list]],
                fields: list[str]) -> list[tuple[str, list[list]]]:
    """输入:[(行号, 等长值序列)] + 字段序列 → 输出:逐行展开的 [(A1范围, 值矩阵)]。"""
    out: list[tuple[str, list[list]]] = []
    for rownum, vals in updates:
        out += _ranges(rownum, fields, [list(vals)])
    return out


def read_rows(upto: str | None = None) -> list[dict]:
    """输入:可选「只读到某字段」→ 输出:表内数据行(键=registry columns,含 rownum)。

    `upto` 给字段名(如 'audit_result')时只读 A 到**该字段所在的那一列**
    (列号由 `layout()` 按表头名算,不是按 columns 数位置)——飞书单次
    读取响应体官方上限 10MB(90221 data exceeded),行列数本身不设限;
    21 列全量一把读在表长大后必炸(2026-08-19 生产实证,audit_sheet 当场
    炸在这里)。只要前几列的调用方(audit_targets)别拉全宽;要全宽的
    (list_new / 自愈 / 反哺器)靠行方向分块 + 90221 对半兜底
    (api 层 feishu.sheet_values_rows)。

    ⚠ 取值**按物理列号**回填字段名(不是 `zip(columns, 单元格)`):所有者
    挪了列顺序也照样对得上;登记之外的列读进来就丢掉(没有字段名可挂)。
    """
    sheet = resources.LISTING_SHEET
    total = feishu.sheet_row_count(sheet)
    if total < 2:
        return []
    idx = _index_map()
    width = idx[upto] if upto else max(idx.values())
    by_pos = {i: f for f, i in idx.items() if i <= width}
    pairs = feishu.sheet_values_rows(sheet, "A", feishu._col_letter(width),
                                     2, total)
    rows = []
    for rownum, raw in pairs:
        cells = [(str(c).strip() if c is not None else "") for c in raw] \
            + [""] * width
        d = {f: cells[i - 1] for i, f in by_pos.items()}
        if d["asin"] or d["store"]:
            d["rownum"] = rownum
            rows.append(d)
    return rows


def row_sku(r: dict) -> str:
    """输入:上架表一行 → 输出:该行的沃尔玛 SKU(SKU 列;为空回落 ASIN 列)。

    **全仓「这一行的 SKU 是什么」的唯一出处**,任何工作流不得自己再写一份
    `r["sku"] or r["asin"]`:回执找行、Unknown 自愈、退役载荷、冷却键、
    mark_used 五处问的是同一个问题,散着写五份表达式,批次 2 通电时漏改
    任何一份都是**静默失效**(找不到行 ⇒ 上架结果永不回填;退役发错码 ⇒ 退不到)。

    存量行 SKU 列为空 ⇒ 回落 ASIN,与「上架 sku=asin」的历史约定逐字相同;
    批次 2 起 SKU 列写真码,这个函数一行不改就自动切过去。
    窄读(`read_rows(upto=...)`)的行里没有 sku 键,所以用 `.get`。
    """
    return (str(r.get("sku") or "").strip()
            or str(r.get("asin") or "").strip())


def append_assignments(pairs: list[tuple], execute: bool = True) -> tuple[int, int]:
    """输入:[(店铺, ASIN)] → 输出:(写入行数, 起始行号)。**只写 店铺/ASIN 两列。**

    分配器把定好的货追加进上架表,「审核结果」留空即「待审」—— 审核链下一轮
    (`product_audit -p from_sheet=1`)自动领走。这是 §9.2 定的列权责:
    **A/B 人工域的机器化**,分配器写完这两列就完事。
    ⚠ **绝不许顺手写 审核结果 列 `pass`**:那是伪造审核结论。而且伪造也没用 ——
    上架闸读的是 `catalog.products`,只会骗到人眼。

    ★ **不用 `ops.cursors` 水位,直接从表里算下一空行** —— 这里有意偏离
    `maint_sheet.append_records` 的成例,理由是两张表的所有权不同:
    维护记录表**程序是唯一写入方**,水位不会被别人动;上架表是**运营在手工
    编辑**的,插行删行随时发生。存了水位反而危险:
      · 有人删了几十行 ⇒ 水位停在表尾之外,追加会在中间留一片空行;
      · 水位与表实际状态一旦不一致,谁也说不清该信哪个。
    而这里本来就要**整读一遍 A/B 做去重**(同一个 ASIN 不能重复派工),
    那一次读顺手就给出了"最后一个非空行" —— 一次读同时解决位置与去重,
    还不用维护第二份状态。
    ★ 附带的好处:**中途失败自愈**。写了三块挂在第四块时不用记账 ——
    下次跑重新读表,已写进去的三块自然被去重掉,只补剩下的。
    """
    if not pairs:
        return 0, 0
    sheet = resources.LISTING_SHEET.require()
    have = read_rows(upto="asin")
    seen = {r["asin"] for r in have if r["asin"]}
    todo = [(st, a) for st, a in pairs if a not in seen]
    if not todo:
        return 0, 0
    start = (max((r["rownum"] for r in have), default=1)) + 1

    if not execute:
        for st, a in todo[:20]:
            logger.info("[DRY-RUN] 将追加 第%d行 店铺=%s ASIN=%s", start, st, a)
        if len(todo) > 20:
            logger.info("[DRY-RUN] …另有 %d 行省略", len(todo) - 20)
        return len(todo), start

    feishu.sheet_ensure_rows(sheet, start + len(todo))
    written = 0
    for i in range(0, len(todo), _APPEND_BLOCK):
        block = todo[i:i + _APPEND_BLOCK]
        row0 = start + i
        try:
            feishu.sheet_write_ranges(sheet, _ranges(
                row0, ["store", "asin"], [[st, a] for st, a in block]))
        except Exception:
            logger.error("上架表追加到第 %d 行时失败,已写 %d 行 —— 重跑即可,"
                         "已写的会被去重跳过", row0, written)
            raise
        written += len(block)
    return written, start


#: write_submit_cols 一行八值的字段序(**值的顺序是调用方契约,不是列序**;
#: 落到哪几列由 layout 按表头名算)。
_SUBMIT_FIELDS = ["list_title", "amz_price", "stock", "walmart_price",
                  "listed", "feed_id", "list_date", "not_listed_reason"]
#: 提交结果四列(list_new 写、自愈可改写)。
_LISTED_FIELDS = ["listed", "feed_id", "list_date", "not_listed_reason"]
#: 回执三列(反哺器的专属域)。
_RECEIPT_FIELDS = ["list_result", "list_fail_reason", "feed_check_date"]
#: 清列/自愈一次写满的七列 = 提交结果四列 + 回执三列。
#: **不含 SKU**(清列不是弃码点)、不含人工域与审核域。
_RECEIPT_CLEAR_FIELDS = _LISTED_FIELDS + _RECEIPT_FIELDS


def write_submit_cols(updates: list[tuple[int, list]], execute: bool = True) -> int:
    """输入:[(行号, 八值;可选第 9 值 = 该行 SKU)] → 输出:写入行数。

    八值依次是 标题 / amz价 / 库存 / walmart价 / 是否上架 / feedid / 上架日期 /
    未上架理由(`_SUBMIT_FIELDS`)。落到哪几列由 `layout()` 按表头名算,相邻
    的粘成一段、不相邻自动拆段 —— 今天是「标题」一段 +「amz价~未上架理由」
    一段,所有者挪列后段数会变,值不会串位。

    可选第 9 值 = SKU:**必须与 是否上架/feedid/上架日期 同一次调用写出** ——
    分两次写,中间崩掉就留下「已提交但无码」的行,而回执反哺器正是靠 SKU 找行
    (row_sku),那一行的 上架结果/报错/feed查询日期 就永不回填。空值不写
    (不产生空段),批次 1 的调用方仍传八值,写出的 range 与改造前同形。
    """
    if not updates:
        return 0
    if not execute:
        for rownum, vals in updates[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 提交列=%s", rownum, vals)
        if len(updates) > 20:
            logger.info("[DRY-RUN] …另有 %d 行省略", len(updates) - 20)
        return 0
    ranges = []
    for r, vals in updates:
        sku = str(vals[8]).strip() if len(vals) > 8 and vals[8] else ""
        fields, row = list(_SUBMIT_FIELDS), list(vals[:8])
        if sku:
            fields, row = ["sku"] + fields, [sku] + row
        ranges += _ranges(r, fields, [row])
    feishu.sheet_write_ranges(resources.LISTING_SHEET, ranges)
    return len(updates)


def write_sku_col(updates: list[tuple[int, str]], execute: bool = True) -> int:
    """输入:[(行号, SKU)] → 输出:写入行数。**只写 SKU 列。**

    补写/改写单独一格 SKU 用(批次 1 没有调用方:提交时的 SKU 走
    `write_submit_cols` 的第 9 值,与 是否上架/feedid 同一次落地)。
    列字母仍由 `layout()` 算,SKU 列挪到哪都写得对。
    """
    if not updates:
        return 0
    if not execute:
        for rownum, sku in updates[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 SKU=%s", rownum, sku)
        return 0
    feishu.sheet_write_ranges(
        resources.LISTING_SHEET,
        _row_ranges([(r, [sku or ""]) for r, sku in updates], ["sku"]))
    return len(updates)


def write_data_cols(updates: list[tuple[int, list]], execute: bool = True) -> int:
    """输入:[(行号, [标题, amz价, 库存, walmart价])] → 输出:写入行数。

    淘汰行数据回显(2026-08-12 旧仓对照接线):旧系统对拉到过数据的淘汰行
    也写标题与价库,运营在表上直接看到"为什么这行没上"的数字;
    只动这四列,不碰提交结果域(是否上架/feedid/上架日期/未上架理由)。
    """
    if not updates:
        return 0
    if not execute:
        return 0
    feishu.sheet_write_ranges(resources.LISTING_SHEET, _row_ranges(
        updates, ["list_title", "amz_price", "stock", "walmart_price"]))
    return len(updates)


# 审核结论 → 上架表「审核结果」列的取值。
# ⚠ **必须是 "pass"**:`list_new` 的领任务闸判的是
# `r["audit_result"].lower() == "pass"`。写 "approved" 那行就永远不会被上架领走,
# 而且不报错 —— 表面上"审过了",实际上再也上不去。
AUDIT_RESULT_CN = {"approved": "pass", "rejected": "reject",
                   "pending": "pending"}


def audit_targets() -> list[dict]:
    """输入:无 → 输出:待审行 [{rownum, asin, store}](ASIN 有值且**审核结果为空**)。

    所有者定稿 2026-08-16:「审核直接读取上架表的 ASIN 与审核结果两列
    (结果为空就审核)」。

    ⚠ **清空「审核结果」≠ 重审**(2026-08-17 更正:原注释写的"这是唯一的重审入口"
    是错的,与同日定稿的"from_sheet 非强审"直接打架)。清空只是让这行重新被
    **领取**;判不判由库里的 `audit_status` 说了算,已有结论的零 LLM 原样投影
    回来 —— 净效果是那格被填回同一个结论,看起来"重审了一遍",其实一次判定
    都没发生。表是**展示面**,不是判定入口(批复 #4 二次批复)。
    真重审走 CLI:`product_audit -p asins=<逗号分隔>`(点名强审)或
    `-p rerule=<规则码>`(改了某条规则后定点翻案)。

    ⚠ **`pending` 也算待审**(2026-08-17 修一处静默搁浅):`_project_to_sheet`
    会把 pending 结论照实写进「审核结果」,而"它有值"原本就等于"这行不用再领" ——
    净效果是 **L1 解不出类目 / L3 LLM 故障的那批,写进 E 那一刻就永久退出了
    上架表通道**,库里的一天退避重判照跑、结论也在更新,但表上那一格永远停在
    `pending`,谁也不会再看它一眼,而且全程不报错(与 rerule 首版同一种搁浅)。
    pending 是**中间态不是结论**,所以它跟空一样要被反复领回来,直到落定。
    `list_new` 的闸判 `== "pass"`,重新领取不会有误上架风险。

    ⚠ 按**字段名**取,不按列字母 —— A/B 已经被对调过一次(2026-08-16),
    再调一次也只改 `resources.LISTING_SHEET.columns` 那一条元组。
    """
    # 只读 A..E 五列(store/asin/标题/PT/审核结果):领任务用不着 F 之后的
    # 理由/回显长文本,少读 3/4 的字节,离 10MB 上限远得多
    return [{"rownum": r["rownum"], "asin": r["asin"], "store": r.get("store")}
            for r in read_rows(upto="audit_result")
            if r.get("asin")
            and str(r.get("audit_result") or "").strip().lower()
            in ("", "pending")]


def write_audit_cols(updates: list[tuple[int, list]], execute: bool = True) -> int:
    """输入:[(行号, [标题, PT, 审核结果, 具体内容, 审核日期])] → 输出:写入行数。

    ⚠ 只动这五列。**中间隔着「类别」列,本函数不写它** —— 类别(37 政策类目)
    由另一条 PR 接线,所以 `_row_ranges` 会自动拆成两段(今天是 D:F 与 H:I),
    夹在中间的类别一格都不碰。是否拆段、拆几段全由 `layout()` 说了算。
    再往后是 list_new 与反哺器的域,跨界写就是 bug(见模块头注的列权责)。
    """
    if not updates:
        return 0
    if not execute:
        for rownum, vals in updates[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 审核列=%s", rownum, vals)
        if len(updates) > 20:
            logger.info("[DRY-RUN] …另有 %d 行省略", len(updates) - 20)
        return 0
    feishu.sheet_write_ranges(resources.LISTING_SHEET, _row_ranges(
        [(r, [("" if v is None else str(v)) for v in vals])
         for r, vals in updates],
        ["list_title", "product_type", "audit_result", "audit_reason",
         "audit_date"]))
    return len(updates)


def write_audit_notes(updates: list[tuple[int, str]],
                      execute: bool = True) -> int:
    """输入:[(行号, 一句人话)] → 输出:写入行数。**只写「具体内容」列**。

    给"审核轮到它了、但判不了"的行用(所有者定稿 2026-08-17:「不能因为没有
    产品就静默失败……需要把理由记录到表格中」)。典型是这行的 ASIN 压根没采集
    过 —— 库里没有它,审核引擎无从下手。

    ⚠ **绝不碰「审核结果」列**,这是本函数存在的全部理由。它一有值这行就不再被
    `audit_targets` 领走(`pending` 除外),往里写个"未采集"就等于**这行从此
    退出审核通道**:采集回来了也不会有人再审它,而表面上"表里写着原因呢"。
    审核结果留空 + 具体内容写原因 = 运营看得见为什么卡着,下一轮照样重新领取。

    也不碰 标题/PT/类别/审核日期:那几列是有结论时 `write_audit_cols`(与类别
    那条 PR)的域,没结论时本来就空,顺手写进去(哪怕写空串)就是跨界写。
    """
    if not updates:
        return 0
    if not execute:
        for rownum, note in updates[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 具体内容=%s", rownum, note)
        if len(updates) > 20:
            logger.info("[DRY-RUN] …另有 %d 行省略", len(updates) - 20)
        return 0
    feishu.sheet_write_ranges(resources.LISTING_SHEET, _row_ranges(
        [(r, [note]) for r, note in updates], ["audit_reason"]))
    return len(updates)


def write_reasons(items: list[tuple[int, str]], execute: bool = True) -> int:
    """输入:[(行号, 理由)] → 输出:写入行数(「未上架理由」列一次批量提交)。

    切块交给 feishu.sheet_write_ranges(段数/行数/字节三条预算任一先到即封批,
    当轮写完不留下一轮)——几百行理由从几百个请求收敛到几个。具体数字**不在
    这里复述**:唯一出处是 api/feishu 顶部的限额登记表(官方值 ×95%),
    此处抄一份就是第二个出处,旧版那句"≤4000 行/请求"正是这么漂掉的。

    ⚠ 别退回逐行写:一行一个飞书请求 ~0.7s,几百行淘汰理由 = 提交前先白耗
    几分钟(2026-08-19 所有者实遇)。只有一行要写就传 `[(行号, 理由)]`。
    """
    if not items:
        return 0
    if not execute:
        for rownum, reason in items[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 未上架理由=%s", rownum, reason)
        return 0
    feishu.sheet_write_ranges(
        resources.LISTING_SHEET,
        _row_ranges([(rn, [reason]) for rn, reason in items],
                    ["not_listed_reason"]))
    return len(items)


def clear_for_relist(rownums: list[int], execute: bool = True) -> int:
    """输入:行号列表 → 输出:清列行数(提交结果与回执列清空,未上架理由写自愈标记)。

    sku_locked_heal 专用:RETIRE 回执成功 + 24h 冷却后把行恢复成"新行",
    下一轮 list_new 按正常闸门链领**新 UPC** 重上。未上架理由留一句人话,
    运营看得出这行为什么 是否上架/feedid 突然空了。

    ⚠ **SKU 列不清**(也不在下面的字段表里):码的寿命由登记簿
    catalog.listing_sources 的 abandoned_at 说了算(sku_plan §5.3 四个弃码点),
    不由清列决定 —— 清列不是弃码点。清掉 SKU 会让回执与退役都找不回这一行。
    同理不碰 人工域(店铺/ASIN/登记日期/查询编码)与 审核域。
    """
    if not rownums:
        return 0
    if not execute:
        logger.info("[DRY-RUN] 将清列重上 %d 行:%s", len(rownums), rownums[:20])
        return 0
    mark = "SKU_LOCKED已退役,冷却完毕待重上(自愈链)"
    feishu.sheet_write_ranges(resources.LISTING_SHEET, _row_ranges(
        [(r, ["", "", "", mark, "", "", ""]) for r in rownums],
        _RECEIPT_CLEAR_FIELDS))
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
    if code in resources.WALMART_ERR_CONTENT:
        # 内容标准拒(2026-08-19):文案图片取自亚马逊原文,原样重发必然
        # 同拒,还触发/延长 QARTH 审查。不自动重试;人工改文案后清 O 列
        # 可重回通道(语义与 PROHIBITED 的"永不"有别)
        return "CONTENT_REJECTED", code
    if status == "success":
        return ("SUCCESS", "") if not code else ("SUCCESS_WITH_WARNING", code)
    if status == "failed":
        return "FAILED", code
    if status == "missing":
        return "MISSING", "终态明细查无此 SKU"
    return "处理中", ""


def _mark_upc_conflicts(pairs: list[tuple[str, str]]) -> int:
    """输入:撞库的 [(店铺, ASIN)] → 输出:标记数。

    ERR_EXT_DATA_0101119:该 UPC 号在沃尔玛目录里已被占用——**UPC 永久弃用**
    (旧 upc_pool 实证),重上时领新号。

    ⚠ 反查键是 **(店铺, ASIN)**,不是 `upc_pool.sku`(2026-09 SKU 改造修):
    ① `sku` 列批次 2 起存真码,按它反查会**静默归零** —— 撞库的号永不标
       conflict,claim 一轮轮把同一个坏号领回来(sku_plan §3.4);
    ② 领号键本来就是 (店, ASIN)(services/upc_pool.claim),不受切码影响;
    ③ 顺带修掉跨店误伤:旧写法不带店维度,A 店某 ASIN 撞库会把 B 店同 ASIN
       名下的号一起烧掉(同 ASIN 多店在架是常态,本仓 2026-08-28 起「跨店
       不互拦」)。
    missing 按**没找到号的 (店,ASIN) 对**数,不再拿"标记数"去减对数
    (一对多个号时旧算法会算出负数)。

    ⚠ 所有者澄清 2026-08-09:撞库**只说明这个 UPC 号被占了**,与"我们的产品
    是否已在沃尔玛上架"无关(UPC 被他人用掉是常态)。连撞多次只是运气差,
    照常领新号重试,不得据此推断该走跟卖。
    """
    if not pairs:
        return 0
    want = sorted(set(pairs))
    n = 0
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.upc, p.asin, p.store FROM catalog.upc_pool p "
                "JOIN unnest(%s::text[], %s::text[]) AS t(s, a) "
                "  ON p.store = t.s AND p.asin = t.a "
                "WHERE p.status <> 'conflict'",
                ([s for s, _ in want], [a for _, a in want]))
            found = cur.fetchall()
        for upc, asin, _store in found:
            upc_pool.mark_conflict(conn, upc, asin)
            n += 1
    missing = len(want) - len({(store, asin) for _u, asin, store in found})
    if missing:
        logger.warning("UPC 撞库 %d 个 (店铺,ASIN) 在池中找不到对应 UPC"
                       "(无法标冲突)", missing)
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
    """输入:无(直接读上架表 + feed 台账 + 目录) → 输出:摘要行,无 Unknown 行时 None。

    feed_poll 反哺器:是否上架=Unknown 的行按 feed 台账 + 沃尔玛目录自愈。

    Unknown 是"提交结局不确定"的防重态(不重复提交防双上架),但没人收尾
    就永久卡死 + UPC 永久占用(claimed 不释放)。收尾三条路:
      台账终态 success/审核中 → 是否上架=Yes + feedid + 回执三列回填,UPC 标已用
      台账终态 failed        → 是否上架=No,上架结果=FAILED(进 list_new 限次
                               重试通道),UPC 回收(rejected 路径)
      目录在架(无台账终态)→ 是否上架=Yes(feedid 保持原样),UPC 标已用
    其余保持 Unknown 等下一轮(摘要里报数,长期不愈人工看)。

    ⚠ **两个键各查各的表**:台账(ops.feed_items.sku)与目录
    (catalog.walmart_items.sku)存的是**沃尔玛侧真 SKU**,按 `row_sku(r)` 查;
    UPC 池的领号键是 (店, ASIN),按 `r["asin"]` 查。批次 2 一通电两者就分叉,
    不拆键的话要么自愈永远查不到台账(行永久卡 Unknown、UPC 永久占用),
    要么拿真码去查 UPC 池查不到号。SKU 列为空的存量行两者同值。
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
    skus = [row_sku(r) for r in unknown]        # 台账/目录按真 SKU 找
    asins = [r["asin"] for r in unknown]        # UPC 池按领号键 (店, ASIN) 找
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
                    (list(set(asins)),))
        claimed = {(s, a): u for s, a, u in cur.fetchall()}

    ranges = []
    n_yes = n_no = n_locked = n_stay = 0
    upc_used: list[tuple[str, str]] = []
    upc_release: list[str] = []
    for r in unknown:
        sku, rn = row_sku(r), r["rownum"]
        key = (r["store"], sku)             # 台账/目录键
        pkey = (r["store"], r["asin"])      # UPC 池领号键
        rec = receipts.get(key)
        if rec and rec[1] in ("success", "failed", "missing"):
            fid, st, code, desc = rec
            o, p = classify_receipt(st, code)
            if p and desc:
                p = f"{p} | {desc}"[:900]
            if o == "SKU_LOCKED":
                # 只落回执三列,是否上架保持 Unknown:行交给 sku_locked_heal 自愈链
                ranges += _ranges(rn, _RECEIPT_FIELDS, [[o, p, today]])
                n_locked += 1
                continue
            if o == "FAILED":
                ranges += _ranges(rn, _RECEIPT_CLEAR_FIELDS, [[
                    "No", "", r["list_date"],
                    "自愈:feed回执FAILED,重新排队", o, p, today]])
                if pkey in claimed:
                    upc_release.append(claimed[pkey])
                n_no += 1
                continue
            if o == "PROHIBITED":
                # 政策违禁:是否上架=No 但 上架结果=PROHIBITED 让 list_new 永不再领
                ranges += _ranges(rn, _RECEIPT_CLEAR_FIELDS, [[
                    "No", "", r["list_date"],
                    "自愈:政策违禁,永不重试", o, p, today]])
                if pkey in claimed:
                    upc_release.append(claimed[pkey])
                n_no += 1
                continue
            # SUCCESS / SUCCESS_WITH_WARNING / ASYNC_PENDING / MISSING?
            # MISSING(feed 终态但明细查无此 SKU)= 高置信未达:按旧
            # RolledBack 语义当"没提交过"——是否上架=No 且 上架结果留 MISSING 供追查
            if o == "MISSING":
                ranges += _ranges(rn, _RECEIPT_CLEAR_FIELDS, [[
                    "No", "", r["list_date"],
                    "自愈:feed终态但明细无此SKU,按未达重排", o, p, today]])
                if pkey in claimed:
                    upc_release.append(claimed[pkey])
                n_no += 1
                continue
            ranges += _ranges(rn, _RECEIPT_CLEAR_FIELDS, [[
                "Yes", fid, r["list_date"] or today,
                f"自愈:feed回执{o}", o, p, today]])
            if pkey in claimed:
                upc_used.append((claimed[pkey], sku))
            n_yes += 1
            continue
        if key in online:
            ranges += _ranges(rn, _LISTED_FIELDS, [[
                "Yes", r["feed_id"], r["list_date"] or today,
                "自愈:沃尔玛目录在线(catalog_sync)"]])
            if pkey in claimed:
                upc_used.append((claimed[pkey], sku))
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
    """输入:无(直接读上架表 + feed 台账) → 输出:摘要行,无在途行时 None。

    feed_poll 反哺器:有 feedid 且 上架结果 在途的行,按台账落回执三列。

    ⚠ 台账三个 dict(item_results / item_errors / item_codes)的键是
    **ops.feed_items.sku = 沃尔玛侧真 SKU**,所以按 `row_sku(r)` 取;拿 ASIN 去
    get,批次 2 起必返 None ⇒ 每行都 continue ⇒ 回执永不回填,而摘要显示正常
    (sku_plan §3.4 列的第一危险形态)。SKU 列为空的存量行回落 ASIN,与改造前同。
    """
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
    conflicts: list[tuple[str, str]] = []       # [(店铺, ASIN)] → 正交标 UPC 池
    for r in pollable:
        fid = r["feed_id"]
        if fid not in cache:
            cache[fid] = feed_track.item_results(fid)
            descs[fid] = feed_track.item_errors(fid)
            codes[fid] = feed_track.item_codes(fid)
        sku = row_sku(r)                   # 台账键 = 沃尔玛侧真 SKU
        st = cache[fid].get(sku)
        if st is None or st[0] == "submitted":
            continue
        o, p = classify_receipt(st[0], st[1])
        # 报错列写「码 + 人话」:光有 EXT_DATA_ERROR_507165… 这种数字码没法修
        desc = descs.get(fid, {}).get(sku)
        if p and desc:
            p = f"{p} | {desc}"[:900]
        if o in ("处理中",) or (o == r["list_result"] and o != "ASYNC_PENDING"):
            continue
        # UPC 撞库**正交处置**(旧 reconcile 实证:与主分类独立,多错并存也要标)
        if resources.WALMART_ERR_UPC_CONFLICT in codes.get(fid, {}).get(
                sku, set()):
            conflicts.append((r["store"], r["asin"]))
        updates += _ranges(r["rownum"], _RECEIPT_FIELDS, [[o, p, today]])
    n_conflict = _mark_upc_conflicts(conflicts)
    if not updates:
        line = f"上架表:在途 {len(pollable)} 行,台账尚无新终态"
        return line + (f";UPC 撞库标记 {n_conflict}" if n_conflict else "")
    n = feishu.sheet_write_ranges(resources.LISTING_SHEET, updates)
    line = f"上架表回填 {n} 行(在途 {len(pollable)})"
    if n_conflict:
        line += f";⚠ UPC 撞库 {n_conflict} 个已标冲突(永久弃用,重上会领新号)"
    return line
