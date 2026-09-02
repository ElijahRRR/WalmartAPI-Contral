"""上架表(registry.LISTING_SHEET)读写积木(list_new 与 feed_poll 共用)。

列契约(21 列 A~U;所有者建表 2026-08-07,2026-09-02 改表头):
  A=店铺 B=ASIN C=SKU D=walmart上架标题 E=walmart_product_type F=审核结果
  G=类别 H=具体内容 I=审核日期 J=amz价格 K=库存 L=walmart价格 M=是否上架
  N=上架feedid O=上架日期 P=未上架理由 Q=上架结果 R=报错 S=feed查询日期
  T=登记日期 U=查询编码

⚠ **A/B 于 2026-08-16 被所有者对调**(原 A=ASIN B=店铺,现 A=店铺 B=ASIN);
**2026-09-02 所有者再改表头**(第三步输出规范化):C 插入 SKU,「审核理由」拆成
G=类别 + H=具体内容,尾部 真实标题/真实PT/真实UPC/UPC匹配 四列换成 T/U(运营域)。
代码里没有一处按字母取 ASIN —— 列序的唯一出处是 `resources.LISTING_SHEET.columns`,
`read_rows()` 按它 zip;写入侧一律显式 range(表头一动,本文件里的字母得跟着挪,
`tests/test_audit_sheet_loop.py` 钉着审核域的区间)。

列权责(旧系统纪律,跨界写就是 bug):**A/B/C 人工域**(运营填店铺、ASIN、SKU);
**D~I 审核域**(`product_audit -p from_sheet=1` 写:D 标题 E PT F 结果 G 类别
H 具体内容 I 日期;2026-08-16 所有者定稿「审核直接读取上架表的 ASIN 与审核结果两列
(结果为空就审核),然后回填」;**H 还有一条单列通道** `write_audit_notes` ——
审不了的行(库里没数据)在 F 留空的前提下只写 H 说明原因,见那个函数的头注);
list_new 写 D/J/K/L(数据回显)与 M/N/O/P(提交结果);回执反哺器只写 Q/R/S;
T/U 运营域,脚本不写。**唯一例外**:heal_unknown 自愈反哺器对 M=Unknown
的行可写 M~S(所有者批复 2026-08-12——Unknown 是 list_new 自己写的
中间态,自愈是同一职责的收尾,不算跨界)。M 三态语义:Yes(已提交)/Unknown(结局不确定,
也算已上架不重复提交)/空或 No(待上架);Q=SKU_LOCKED 由 sku_locked_heal
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

_COLS = len(resources.LISTING_SHEET.columns)   # 21,A~U
_APPEND_BLOCK = 500 # 单次写飞书的行数上限(一次裹上千行会被 90202 拒;
                    # 与 maint_sheet 同值,那边是实遇被拒后定的)
PENDING_O = ("", "处理中", "ASYNC_PENDING")   # 「上架结果」列这些值反哺器继续跟


# 列字母**从 registry.LISTING_SHEET.columns 的下标推导**,不再硬编码(与 maint_sheet
# 同一套路,所有者定稿 2026-09-02「统一为按表头名定位写表」):表头再动,只改
# registry 那条元组 + 下面的 _HEADER_NAMES,本文件一个字母都不用碰。
# 2026-09-02 实证:所有者改了表头(C 插 SKU、理由拆两列),硬编码那版按旧字母
# 写了半天 —— 读出来的字段整体错位、结论落在别人的列上,而且全程不报错。
def _col(name: str) -> str:
    return feishu._col_letter(resources.LISTING_SHEET.columns.index(name) + 1)


def _rng(first: str, last: str, row: int) -> str:
    """输入:首/末字段名 + 行号 → 输出:同一行的 A1 区间,如 'D7:I7'。"""
    return f"{_col(first)}{row}:{_col(last)}{row}"


# 与 registry 列序一一对应的飞书表头文字(所有者 2026-09-02 给的表头原文)。
# ⚠ 与 `resources.LISTING_SHEET.columns` 同增同减:少一条 _header() 就 KeyError。
_HEADER_NAMES = {
    "store": "店铺", "asin": "ASIN", "sku": "SKU",
    "list_title": "walmart上架标题", "product_type": "walmart_product_type",
    "audit_result": "审核结果", "audit_category": "类别",
    "audit_detail": "具体内容", "audit_date": "审核日期",
    "amz_price": "amz价格", "stock": "库存", "walmart_price": "walmart价格",
    "listed": "是否上架", "feed_id": "上架feedid", "list_date": "上架日期",
    "not_listed_reason": "未上架理由", "list_result": "上架结果",
    "list_fail_reason": "报错", "feed_check_date": "feed查询日期",
    "register_date": "登记日期", "query_code": "查询编码",
}


def _header() -> tuple:
    """输入:无 → 输出:与 registry 列序一一对应的中文表头。"""
    return tuple(_HEADER_NAMES[c] for c in resources.LISTING_SHEET.columns)


def _norm_head(s) -> str:
    return "".join(str(s or "").split()).casefold()


def verify_header() -> None:
    """输入:无 → 输出:无;第 1 行表头与 registry 对不上就 ValueError,点名哪一列。

    表头是运营的领地,随时会再动(2026-08-16 对调 A/B、2026-09-02 插 SKU 拆理由);
    按位读、按名推字母写,两边都以 registry 为准 —— 所以读写之前先对一遍第 1 行,
    对不上就停,而不是错位着跑完还不报错。比对忽略大小写与空白。
    """
    sheet = resources.LISTING_SHEET
    got = feishu.sheet_values_small(sheet, f"A1:{feishu._col_letter(_COLS)}1")
    row = [_norm_head(c) for c in (got[0] if got else [])] + [""] * _COLS
    want = _header()
    bad = [f"{feishu._col_letter(i + 1)} 应为「{w}」实为「{row[i] or '(空)'}」"
           for i, w in enumerate(want) if row[i] != _norm_head(w)]
    if bad:
        raise ValueError(
            "上架表表头与 registry.resources.LISTING_SHEET.columns 对不上,停止读写"
            "(改了表头就先改 registry 的列序与 listing_sheet._HEADER_NAMES):"
            + ";".join(bad))


def read_rows(upto: str | None = None) -> list[dict]:
    """输入:可选「只读到某字段」→ 输出:表内数据行(键=registry columns,含 rownum)。

    `upto` 给字段名(如 'audit_result')时只读 A 到该字段那一列——飞书单次
    读取响应体官方上限 10MB(90221 data exceeded),行列数本身不设限;
    21 列全量一把读在表长大后必炸(2026-08-19 生产实证,audit_sheet 当场
    炸在这里)。只要前几列的调用方(audit_targets)别拉全宽;要全宽的
    (list_new / 自愈 / 反哺器)靠行方向分块 + 90221 对半兜底
    (api 层 feishu.sheet_values_rows)。
    """
    sheet = resources.LISTING_SHEET
    verify_header()                      # 表头一动就停,不错位着跑
    total = feishu.sheet_row_count(sheet)
    if total < 2:
        return []
    cols = resources.LISTING_SHEET.columns
    width = (cols.index(upto) + 1) if upto else _COLS
    pairs = feishu.sheet_values_rows(sheet, "A", feishu._col_letter(width),
                                     2, total)
    rows = []
    for rownum, raw in pairs:
        cells = [(str(c).strip() if c is not None else "") for c in raw] \
            + [""] * width
        d = dict(zip(cols[:width], cells[:width]))
        if d["asin"] or d["store"]:
            d["rownum"] = rownum
            rows.append(d)
    return rows


def append_assignments(pairs: list[tuple], execute: bool = True) -> tuple[int, int]:
    """输入:[(店铺, ASIN)] → 输出:(写入行数, 起始行号)。**只写 A/B 两列。**

    分配器把定好的货追加进上架表,E 列留空即「待审」—— 审核链下一轮
    (`product_audit -p from_sheet=1`)自动领走。这是 §9.2 定的列权责:
    **A/B 人工域的机器化**,分配器写完这两列就完事。
    ⚠ **绝不许顺手写 E 列 `pass`**:那是伪造审核结论。而且伪造也没用 ——
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
            logger.info("[DRY-RUN] 将追加 第%d行 A=%s B=%s", start, st, a)
        if len(todo) > 20:
            logger.info("[DRY-RUN] …另有 %d 行省略", len(todo) - 20)
        return len(todo), start

    feishu.sheet_ensure_rows(sheet, start + len(todo))
    written = 0
    for i in range(0, len(todo), _APPEND_BLOCK):
        block = todo[i:i + _APPEND_BLOCK]
        row0 = start + i
        try:
            feishu.sheet_write_ranges(sheet, [
                (f"{_col('store')}{row0}:{_col('asin')}{row0 + len(block) - 1}",
                 [[st, a] for st, a in block])])
        except Exception:
            logger.error("上架表追加到第 %d 行时失败,已写 %d 行 —— 重跑即可,"
                         "已写的会被去重跳过", row0, written)
            raise
        written += len(block)
    return written, start


def write_submit_cols(updates: list[tuple[int, list]], execute: bool = True) -> int:
    """输入:[(行号, [D,J,K,L] + [M,N,O,P] 八值)] → 输出:写入行数。

    list_new 专用:标题与数据/提交域不连续,拆两个 range:
    D{r}(标题)与 J{r}:P{r}(J amz价 K 库存 L walmart价 M 是否上架 N feedid O 上架日期 P 未上架理由)。
    """
    if not updates:
        return 0
    if not execute:
        for rownum, vals in updates[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 D+J:P=%s", rownum, vals)
        if len(updates) > 20:
            logger.info("[DRY-RUN] …另有 %d 行省略", len(updates) - 20)
        return 0
    ranges = []
    for r, vals in updates:
        title, rest = vals[0], vals[1:]
        ranges.append((_rng("list_title", "list_title", r), [[title]]))
        ranges.append((_rng("amz_price", "not_listed_reason", r), [rest]))
    feishu.sheet_write_ranges(resources.LISTING_SHEET, ranges)
    return len(updates)


def write_data_cols(updates: list[tuple[int, list]], execute: bool = True) -> int:
    """输入:[(行号, [D 标题, J amz价, K 库存, L walmart价])] → 输出:写入行数。

    淘汰行数据回显(2026-08-12 旧仓对照接线):旧系统对拉到过数据的淘汰行
    也写标题与价库,运营在表上直接看到"为什么这行没上"的数字;
    只动 D 与 J:L,不碰 M~P(提交结果域)。
    """
    if not updates:
        return 0
    if not execute:
        return 0
    ranges = []
    for r, vals in updates:
        ranges.append((_rng("list_title", "list_title", r), [[vals[0]]]))
        ranges.append((_rng("amz_price", "walmart_price", r), [vals[1:4]]))
    feishu.sheet_write_ranges(resources.LISTING_SHEET, ranges)
    return len(updates)


# 审核结论 → 上架表 F 列(审核结果)的取值。
# ⚠ **必须是 "pass"**:`list_new` 的领任务闸判的是
# `r["audit_result"].lower() == "pass"`。写 "approved" 那行就永远不会被上架领走,
# 而且不报错 —— 表面上"审过了",实际上再也上不去。
AUDIT_RESULT_CN = {"approved": "pass", "rejected": "reject",
                   "pending": "pending"}


def audit_targets() -> list[dict]:
    """输入:无 → 输出:待审行 [{rownum, asin, store}](ASIN 有值且**审核结果为空**)。

    所有者定稿 2026-08-16:「审核直接读取上架表的 ASIN 与审核结果两列
    (结果为空就审核)」。

    ⚠ **清空 F 列 ≠ 重审**(2026-08-17 更正:原注释写的"这是唯一的重审入口"
    是错的,与同日定稿的"from_sheet 非强审"直接打架)。清空只是让这行重新被
    **领取**;判不判由库里的 `audit_status` 说了算,已有结论的零 LLM 原样投影
    回来 —— 净效果是那格被填回同一个结论,看起来"重审了一遍",其实一次判定
    都没发生。表是**展示面**,不是判定入口(批复 #4 二次批复)。
    真重审走 CLI:`product_audit -p asins=<逗号分隔>`(点名强审)或
    `-p rerule=<规则码>`(改了某条规则后定点翻案)。

    ⚠ **`pending` 也算待审**(2026-08-17 修一处静默搁浅):`_project_to_sheet`
    会把 pending 结论照实写进 F 列,而"F 有值"原本就等于"这行不用再领" ——
    净效果是 **L1 解不出类目 / L3 LLM 故障的那批,写进 E 那一刻就永久退出了
    上架表通道**,库里的一天退避重判照跑、结论也在更新,但表上那一格永远停在
    `pending`,谁也不会再看它一眼,而且全程不报错(与 rerule 首版同一种搁浅)。
    pending 是**中间态不是结论**,所以它跟空一样要被反复领回来,直到落定。
    `list_new` 的闸判 `== "pass"`,重新领取不会有误上架风险。

    ⚠ 按**字段名**取,不按列字母 —— A/B 已经被对调过一次(2026-08-16),
    再调一次也只改 `resources.LISTING_SHEET.columns` 那一条元组。
    """
    # 只读 A..F 六列(store/asin/SKU/标题/PT/审核结果):领任务用不着 G 之后的
    # 理由/回显长文本,少读 3/4 的字节,离 10MB 上限远得多
    return [{"rownum": r["rownum"], "asin": r["asin"], "store": r.get("store")}
            for r in read_rows(upto="audit_result")
            if r.get("asin")
            and str(r.get("audit_result") or "").strip().lower()
            in ("", "pending")]


def write_audit_cols(updates: list[tuple[int, list]], execute: bool = True) -> int:
    """输入:[(行号, [D 标题, E PT, F 结果, G 类别, H 具体内容, I 日期])] → 输出:写入行数。

    D~I 连续一段,一行一个 range。⚠ 只动这六列 —— J 之后是 list_new 与
    反哺器的域,跨界写就是 bug(见模块头注的列权责)。
    """
    if not updates:
        return 0
    if not execute:
        for rownum, vals in updates[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 D:I=%s", rownum, vals)
        if len(updates) > 20:
            logger.info("[DRY-RUN] …另有 %d 行省略", len(updates) - 20)
        return 0
    feishu.sheet_write_ranges(resources.LISTING_SHEET, [
        (_rng("list_title", "audit_date", r),
         [[("" if v is None else str(v)) for v in vals]])
        for r, vals in updates])
    return len(updates)


def write_audit_notes(updates: list[tuple[int, str]],
                      execute: bool = True) -> int:
    """输入:[(行号, 一句人话)] → 输出:写入行数。**只写 H 列(具体内容)**。

    给"审核轮到它了、但判不了"的行用(所有者定稿 2026-08-17:「不能因为没有
    产品就静默失败……需要把理由记录到表格中」)。典型是这行的 ASIN 压根没采集
    过 —— 库里没有它,审核引擎无从下手。

    ⚠ **绝不碰 F 列**,这是本函数存在的全部理由。F 一有值这行就不再被
    `audit_targets` 领走(`pending` 除外),往里写个"未采集"就等于**这行从此
    退出审核通道**:采集回来了也不会有人再审它,而表面上"表里写着原因呢"。
    F 留空 + H 写原因 = 运营看得见为什么卡着,而下一轮照样重新领取。

    也不碰 D/E/G/I:那四列是有结论时 `write_audit_cols` 的域,没结论时本来就空,
    顺手写进去(哪怕写空串)就是跨界写。
    """
    if not updates:
        return 0
    if not execute:
        for rownum, note in updates[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 H=%s", rownum, note)
        if len(updates) > 20:
            logger.info("[DRY-RUN] …另有 %d 行省略", len(updates) - 20)
        return 0
    feishu.sheet_write_ranges(resources.LISTING_SHEET, [
        (_rng("audit_detail", "audit_detail", r), [[note]]) for r, note in updates])
    return len(updates)


def write_reasons(items: list[tuple[int, str]], execute: bool = True) -> int:
    """输入:[(行号, 理由)] → 输出:写入行数(P 列「未上架理由」一次批量提交)。

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
            logger.info("[DRY-RUN] 将回写 第%d行 P=%s", rownum, reason)
        return 0
    feishu.sheet_write_ranges(
        resources.LISTING_SHEET,
        [(_rng("not_listed_reason", "not_listed_reason", rn), [[reason]])
         for rn, reason in items])
    return len(items)


def clear_for_relist(rownums: list[int], execute: bool = True) -> int:
    """输入:行号列表 → 输出:清列行数(M~O 与 Q~S 清空,P 写自愈标记)。

    sku_locked_heal 专用:RETIRE 回执成功 + 24h 冷却后把行恢复成"新行",
    下一轮 list_new 按正常闸门链领**新 UPC** 重上。P 列留一句人话,
    运营看得出这行为什么 M/N 突然空了。
    """
    if not rownums:
        return 0
    if not execute:
        logger.info("[DRY-RUN] 将清列重上 %d 行:%s", len(rownums), rownums[:20])
        return 0
    mark = "SKU_LOCKED已退役,冷却完毕待重上(自愈链)"
    ranges = []
    for r in rownums:
        ranges.append((_rng("listed", "feed_check_date", r),
                       [["", "", "", mark, "", "", ""]]))
    feishu.sheet_write_ranges(resources.LISTING_SHEET, ranges)
    return len(rownums)


def classify_receipt(status: str, error_code: str) -> tuple[str, str]:
    """输入:feed_items 的 (status, error_code) → 输出:(Q 上架结果, R 报错)。

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
    """输入:无(直接读上架表 + feed 台账 + 目录) → 输出:摘要行,无 Unknown 行时 None。

    feed_poll 反哺器:K=Unknown 的行按 feed 台账 + 沃尔玛目录自愈。

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
                # 只落 Q,M 保持 Unknown:行交给 sku_locked_heal 自愈链
                ranges.append((_rng("list_result", "feed_check_date", rn),
                               [[o, p, today]]))
                n_locked += 1
                continue
            if o == "FAILED":
                ranges.append((_rng("listed", "feed_check_date", rn), [[
                    "No", "", r["list_date"],
                    "自愈:feed回执FAILED,重新排队", o, p, today]]))
                if key in claimed:
                    upc_release.append(claimed[key])
                n_no += 1
                continue
            if o == "PROHIBITED":
                # 政策违禁:M=No 但 Q=PROHIBITED 让 list_new 永不再领
                ranges.append((_rng("listed", "feed_check_date", rn), [[
                    "No", "", r["list_date"],
                    "自愈:政策违禁,永不重试", o, p, today]]))
                if key in claimed:
                    upc_release.append(claimed[key])
                n_no += 1
                continue
            # SUCCESS / SUCCESS_WITH_WARNING / ASYNC_PENDING / MISSING?
            # MISSING(feed 终态但明细查无此 SKU)= 高置信未达:按旧
            # RolledBack 语义当"没提交过"——M=No 且 Q 留 MISSING 供追查
            if o == "MISSING":
                ranges.append((_rng("listed", "feed_check_date", rn), [[
                    "No", "", r["list_date"],
                    "自愈:feed终态但明细无此SKU,按未达重排", o, p, today]]))
                if key in claimed:
                    upc_release.append(claimed[key])
                n_no += 1
                continue
            ranges.append((_rng("listed", "feed_check_date", rn), [[
                "Yes", fid, r["list_date"] or today,
                f"自愈:feed回执{o}", o, p, today]]))
            if key in claimed:
                upc_used.append((claimed[key], r["asin"]))
            n_yes += 1
            continue
        if key in online:
            ranges.append((_rng("listed", "not_listed_reason", rn), [[
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
    """输入:无(直接读上架表 + feed 台账) → 输出:摘要行,无在途行时 None。

    feed_poll 反哺器:L 有 feedid 且 O 在途的行,按台账落 O/P/Q。
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
        updates.append((_rng("list_result", "feed_check_date", r["rownum"]),
                        [[o, p, today]]))
    n_conflict = _mark_upc_conflicts(conflicts)
    if not updates:
        line = f"上架表:在途 {len(pollable)} 行,台账尚无新终态"
        return line + (f";UPC 撞库标记 {n_conflict}" if n_conflict else "")
    n = feishu.sheet_write_ranges(resources.LISTING_SHEET, updates)
    line = f"上架表回填 {n} 行(在途 {len(pollable)})"
    if n_conflict:
        line += f";⚠ UPC 撞库 {n_conflict} 个已标冲突(永久弃用,重上会领新号)"
    return line
