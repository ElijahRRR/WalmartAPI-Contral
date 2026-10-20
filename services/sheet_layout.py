"""电子表格「按表头名认列」的唯一实现(上架表 / 产品分配表共用)。

**列定位:按表头名,不按列字母**(所有者要求 2026-09-02:「以后再调整列顺序
也能准确写入」)。做法:读一次表头行,拿 registry 登记的 `Spreadsheet.headers`
(字段名 → 中文表头)去认列,得到 字段名 → 1-based 列号 的映射;一切 range 由
它算出来,**业务代码里一个写死的列字母都没有**(守门:
tests/test_sku_guard.py::test_listing_sheet_has_no_hardcoded_column_letters)。

表头缺失/重复 = fail-closed 直接抛 `HeaderMismatch` 拒绝读写(宁可不跑,也不写
错列);表头行里多出登记之外的列只告警(所有者随时会加自己的列);比对忽略
大小写、空白与全/半角括号(表头里多敲个空格、括号打成中文的,不该停掉一整轮)。

⚠ 这套定位是 **2026-09-02 事故**换来的:所有者改了上架表表头(C 插 SKU、审核
理由拆两列),硬编码字母那版按旧字母写了半天 —— 读出来的字段整体错位、结论
落在别人的列上,而且全程不报错。算法原本长在 services/listing_sheet 里;
2026-09-17 点名分配要第二张按表头认列的表(产品分配表),把**算法**搬到这里
一份共用 —— 每个能力只有一条实现路径(conventions §六),第二张表照抄一遍
就是双轨,表头比对的宽容口径迟早在两边漂开。

**本模块不持有缓存**:「每进程读一次表头」的缓存归各消费方自己
(`listing_sheet._LAYOUT` / `alloc_sheet._LAYOUT`),单测靠 monkeypatch 它们预置
布局、或打桩它们的 `_read_header_row`。这里只做纯函数:读表头行、认列、
算字母、拼 range。
"""

import logging

from api import feishu
from registry.resources import Spreadsheet

logger = logging.getLogger("services.sheet_layout")

#: 表头行往登记列数之外多扫几列 —— 只为发现"登记之外的新列"并告警。
#: 多出的列不算错(所有者随时会加自己的列),但要说出来,免得下一个人
#: 以为程序看得见它。
_HEADER_SCAN_SLACK = 5

#: 比对时折成半角的全角括号。所有者的表头是手敲的,「商品品类(五大类)」里的
#: 括号打成中文括号是常事,而那种差别不会让值写错列。
_FOLD_PARENS = str.maketrans({"(": "(", ")": ")"})


class HeaderMismatch(LookupError, ValueError):
    """表头行与 registry 登记对不上 —— 本轮拒绝一切读写。

    **两个基类都要**(合并 2026-09-04):SKU 改造这一侧的调用方按
    `LookupError` 捕(与 `Spreadsheet.require()` 的"表没登记"同一类失败,
    heal_unknown / sync_from_ledger 的 except 就是这么写的);审核链第三步
    的表头核验按 `ValueError` 捕。谁的 except 都不该在合并里被静默改掉。
    """


def read_header_row(sheet: Spreadsheet) -> list[str]:
    """输入:登记条目 → 输出:表头行的单元格文本(已 strip,右侧多扫几列)。"""
    s = sheet.require()
    width = len(s.headers) + _HEADER_SCAN_SLACK
    last = feishu._col_letter(width)
    got = feishu.sheet_values_small(s, f"A1:{last}1")
    raw = (got or [[]])[0] or []
    return [(str(c).strip() if c is not None else "") for c in raw]


def norm_head(s) -> str:
    """输入:表头单元格 → 输出:比对用的规范形(去掉全部空白 + casefold + 括号折半角)。

    审核链第三步定的宽容口径,合并时原样保住:运营在表头里多敲一个空格、
    把 walmart 写成 Walmart 都是常事,**为这个 fail-closed 停掉一整轮不值**;
    真正要拦的是"少一列/多一列/两列重名"这种会让值写进别人列的漂移。
    ⚠ 只在比对时规范化,报错与告警一律回显**表上的原文**,不然人对着
    规范化过的字符串找不到自己那一格。
    """
    return "".join(str(s or "").split()).casefold().translate(_FOLD_PARENS)


def resolve(sheet: Spreadsheet, cells: list[str]) -> dict[str, int]:
    """输入:登记条目 + 表头行单元格 → 输出:{字段名: 1-based 列号}。

    **fail-closed**:registry 登记的表头只要缺一个、或在表头行里出现两次,
    直接抛 `HeaderMismatch` 拒绝一切读写 —— 宁可这一轮不跑,也不能把标题写进
    SKU 列(2026-09-02 重排之前那套硬编码字母,插一列就是全体静默错位)。
    表头行里多出登记之外的列**只告警**:所有者随时会加自己的列,那不是错。
    比对忽略大小写、空白与全/半角括号(`norm_head`):那种差别不会让值写错列。
    """
    want = sheet.headers
    if not want:
        raise LookupError(f"「{sheet.name}」未登记 headers(字段名→中文表头):"
                          "按表头名定位列是前提,先补 registry")
    seen: dict[str, list[int]] = {}       # 规范形 → 1-based 列号们
    raw_of: dict[str, str] = {}           # 规范形 → 表上原文(报错时回显)
    for i, text in enumerate(cells, 1):
        key = norm_head(text)
        if key:
            seen.setdefault(key, []).append(i)
            raw_of.setdefault(key, text)
    known = {norm_head(h) for h in want.values()}
    missing = [h for h in want.values() if norm_head(h) not in seen]
    dupes = [h for h in want.values() if len(seen.get(norm_head(h), ())) > 1]
    extra = [raw_of[k] for k in seen if k not in known]
    if extra:
        logger.warning("「%s」表头有登记之外的列 %s —— 程序看不见它们"
                       "(要接线先登记 registry 该表的 headers)", sheet.name, extra)
    if missing or dupes:
        logger.warning("「%s」表头对不上登记:缺失 %s;重复 %s",
                       sheet.name, missing, dupes)
        # 点名到列:缺的那几个说不出位置(压根没有),重复的把撞在一起的
        # 列字母一并报出来 —— 光说"重复"人得自己一列列数过去。
        where = {h: "/".join(feishu._col_letter(i) for i in seen[norm_head(h)])
                 for h in dupes}
        raise HeaderMismatch(
            f"「{sheet.name}」表头与 registry 登记对不上(缺失 {missing};"
            f"重复 {where or dupes})——本轮**拒绝一切读写**:列认不准就会把值"
            f"写进别人的列,而且不报错。表头行实际读到的是 {cells};"
            f"请核对飞书表头行或 registry 里「{sheet.name}」的 headers")
    return {f: seen[norm_head(h)][0] for f, h in want.items()}


def letters(index: dict[str, int]) -> dict[str, str]:
    """输入:{字段名: 列号} → 输出:{字段名: 列字母}。"""
    return {f: feishu._col_letter(i) for f, i in index.items()}


def ranges(sheet: Spreadsheet, index: dict[str, int], row_from: int,
           fields: list[str], rows_vals: list[list]) -> list[tuple[str, list[list]]]:
    """输入:登记条目 + 列布局 + 起始行号 + 字段序列 + 每行的等长值序列
    → 输出:[(A1范围, 值矩阵)]。

    **列字母在这里出生,别处一律不许写字母**。列号相邻的字段粘成一段
    (少一个飞书请求位),不相邻就拆多段 —— 所有者在中间插一列,同一批
    写入会自动从一段变两段,值一个都不会落到隔壁列。
    """
    unknown = [f for f in fields if f not in index]
    if unknown:
        raise LookupError(f"「{sheet.name}」没有这些字段:{unknown}(先登记 registry)")
    segs: list[list[int]] = []          # 每段 = fields 里的下标序列
    for pos, f in enumerate(fields):
        if segs and index[f] == index[fields[segs[-1][-1]]] + 1:
            segs[-1].append(pos)
        else:
            segs.append([pos])
    row_to = row_from + len(rows_vals) - 1
    out = []
    for seg in segs:
        a = feishu._col_letter(index[fields[seg[0]]])
        b = feishu._col_letter(index[fields[seg[-1]]])
        out.append((f"{a}{row_from}:{b}{row_to}",
                    [[vals[pos] for pos in seg] for vals in rows_vals]))
    return out


def rows_ranges(sheet: Spreadsheet, index: dict[str, int],
                rows: list[tuple[int, list[str], list]]
                ) -> list[tuple[str, list[list]]]:
    """输入:登记条目 + 列布局 + [(行号, 该行字段序列, 等长值序列)]
    → 输出:**按段位次分组**的 [(A1范围, 值矩阵)]:先是每行的第 1 段,再是每行的第 2 段……

    **为什么要分组**(所有者 2026-10-20 实遇:产品分配表"不是批量写入的"):
    一行的字段在表上不相邻时会拆成几段(产品分配表 = 店铺 A 一段 + C:Q 一段)。
    逐行展开就成了 A2、C2:Q2、A3、C3:Q3……交错排列,而写通道的粘段
    (`api/feishu._coalesce`)只合并**紧挨着的、列区间相同的连续行** ——
    交错时相邻两段列永远不同,一段都粘不起来:每行占 2 段、每请求 100 段,
    2000 行要发 40 个请求(每个间隔 0.3s)。按位次分组之后同列的段首尾相接,
    通道把它们粘成 2 段,1 个请求写完。

    **段的先后由调用方的字段顺序决定**:一行的段按「段里第一个字段在 `fields`
    里的位置」排(`ranges` 本来就这么产出),分组后**第 k 段整列写完才轮到
    第 k+1 段**。写入跨多个请求时,中途失败只会留下"前面的段写了、后面的没写",
    所以**哪一列是"这行已处理"的标记,就把它的字段放在最后**(产品分配表的
    「店铺」);哪一列必须先于别的落地,就放在最前(上架表的 SKU 先于
    「是否上架/feedid」,否则会出现「已提交但无码」的行)。

    同一位次里保持输入顺序(稳定),所以同一格在一次调用里被同一组字段写两次时,
    后写的仍然在后 —— 与逐行展开的覆盖语义一致。一条输入拆出的各段列互不重叠,
    换序不改变它的值。⚠ 例外只有一种:同一行在一次调用里出现多次、而且**字段组
    不同**,不同位次的段才可能落在同一格,这时以位次靠后的那段为准。本仓的调用方
    每行只写一次,没有这种用法;以后要这么用,先把同一行的几次写入合成一条。
    """
    groups: list[list[tuple[str, list[list]]]] = []
    for rownum, fields, vals in rows:
        for pos, seg in enumerate(ranges(sheet, index, rownum, fields,
                                         [list(vals)])):
            while len(groups) <= pos:
                groups.append([])
            groups[pos].append(seg)
    return [seg for g in groups for seg in g]


def row_ranges(sheet: Spreadsheet, index: dict[str, int],
               updates: list[tuple[int, list]],
               fields: list[str]) -> list[tuple[str, list[list]]]:
    """输入:登记条目 + 列布局 + [(行号, 等长值序列)] + 字段序列
    → 输出:按段位次分组的 [(A1范围, 值矩阵)](每行同一组字段;见 `rows_ranges`)。"""
    return rows_ranges(sheet, index, [(r, fields, vals) for r, vals in updates])
