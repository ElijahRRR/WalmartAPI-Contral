"""产品分配表(registry.ALLOC_SHEET)读写积木 —— 点名分配的驱动表。

`alloc_plan -p from_sheet=1`(口径 #19,所有者定稿 2026-09-17)只对这张表
ASIN 列点名的品做分配:大的限制(四道闸 / 容量 / 配额 / 品牌排他)一样不少,
只是候选池不再是全库,而是表里那几十个 ASIN。

列权责(跨界写就是 bug):**ASIN 人工域**(所有者只填这一列);其余 16 列
全部机器域,分配跑完一次写满 —— 店铺 / 品牌 / 产品分 / 罚分 / 罚分原因 /
流别 / 未分配原因 / 商品品类(五大类) / 商品大类(26类) / 评分 / 评论数 /
配送方式 / 配送天数 / 落地价 / 窗口销售额(毛额) / 是否在线。
**「店铺」已填的行 = 上一轮处理过,重跑跳过**:不读它的 ASIN,也不碰它的
任何一格(所有者定稿:「表里已经填了店铺的行,重跑时跳过」)。

列定位按表头名(services/sheet_layout,与上架表同一份算法):所有者挪列
顺序代码一行不改;表头缺列/重名 fail-closed 拒绝读写。**源码里没有列字母**
(守门:tests/test_sku_guard.py::test_listing_sheet_has_no_hardcoded_column_letters)。
"""

import logging

from api import feishu
from registry import resources
from services import sheet_layout

logger = logging.getLogger("services.alloc_sheet")

#: 进程内列布局缓存:字段名 → 1-based 列号。None = 还没读过表头行。
_LAYOUT: dict[str, int] | None = None


def reset_layout_cache() -> None:
    """输入:无 → 输出:无。清空进程内列布局缓存,下次用时重读表头行。"""
    global _LAYOUT
    _LAYOUT = None


def _read_header_row() -> list[str]:
    """输入:无 → 输出:产品分配表表头行的单元格文本(已 strip)。"""
    return sheet_layout.read_header_row(resources.ALLOC_SHEET)


def _index_map() -> dict[str, int]:
    """输入:无 → 输出:{字段名: 1-based 列号}(每进程读一次表头行认列,fail-closed)。"""
    global _LAYOUT
    if _LAYOUT is None:
        _LAYOUT = sheet_layout.resolve(resources.ALLOC_SHEET, _read_header_row())
    return _LAYOUT


def layout() -> dict[str, str]:
    """输入:无 → 输出:{字段名: 列字母}(按表头名认列)。"""
    return sheet_layout.letters(_index_map())


def script_fields() -> tuple[str, ...]:
    """输入:无 → 输出:机器域字段序(登记的全部列去掉 ASIN 这一人工列)。

    从 registry 派生而不是另抄一份:所有者再加一列,登记处加一行,这里自动跟上。
    """
    return tuple(c for c in resources.ALLOC_SHEET.columns if c != "asin")


def read_targets() -> list[dict]:
    """输入:无 → 输出:[{rownum, asin, asin_raw, store}](ASIN 非空的行)。

    **窄读**:只读到 店铺/ASIN 两列里靠右的那一列(列号由表头名算),不拉全宽
    —— 与上架表 `audit_targets` 同款纪律,读取响应体官方上限 10MB。
    `asin` 已 strip+upper(与登记簿身份键同口径:`sku_asin.pick_asin` 也是先归一
    再过形态闸);`asin_raw` 留表上原文给报错回显。**形态闸不在这里判**
    (`sku_asin.is_standard_asin` 归工作流:它要把不合形的行写进「未分配原因」,
    而不是在读表这一步丢掉)。`store` 非空的行原样返回,跳不跳由工作流决定。
    """
    sheet = resources.ALLOC_SHEET
    total = feishu.sheet_row_count(sheet)
    if total < 2:
        return []
    idx = _index_map()          # 表头一动就停(fail-closed),不错位着跑
    width = max(idx["asin"], idx["store"])
    pairs = feishu.sheet_values_rows(sheet, "A", feishu._col_letter(width),
                                     2, total)
    rows = []
    for rownum, raw in pairs:
        cells = [(str(c).strip() if c is not None else "") for c in raw] \
            + [""] * width
        asin_raw, store = cells[idx["asin"] - 1], cells[idx["store"] - 1]
        if not asin_raw:
            continue
        rows.append({"rownum": rownum, "asin": asin_raw.upper(),
                     "asin_raw": asin_raw, "store": store})
    return rows


def write_rows(updates: list[tuple[int, dict]], execute: bool = True) -> int:
    """输入:[(行号, {机器域字段: 值})] → 输出:写入行数。**永不写 ASIN 列。**

    值按字段名给,不按位置 —— 16 个值靠位置对,少给一个后面的整体前移一格
    写进别人的列,而且不报错(上架表 2026-09-02 那次错位就是这么发生的)。
    字段缺一个、多一个都直接抛错;None 写空串。今天「店铺」与「品牌…是否在线」
    中间隔着 ASIN 列,`sheet_layout.ranges` 会自动拆成两段;所有者挪列后
    段数会变,值不会串位。
    """
    if not updates:
        return 0
    fields = list(script_fields())
    want = set(fields)
    bad = [(r, sorted(want ^ set(vals))) for r, vals in updates
           if set(vals) != want]
    if bad:
        raise ValueError(
            f"write_rows 要的是机器域全部 {len(fields)} 个字段,这些行给的对不上"
            f"(行号, 差异字段):{bad[:10]} —— 少一个值就会整体错位写进别人的列")
    if not execute:
        for rownum, vals in updates[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 %s", rownum, vals)
        if len(updates) > 20:
            logger.info("[DRY-RUN] …另有 %d 行省略", len(updates) - 20)
        return 0
    feishu.sheet_write_ranges(resources.ALLOC_SHEET, sheet_layout.row_ranges(
        resources.ALLOC_SHEET, _index_map(),
        [(r, [("" if vals[f] is None else str(vals[f])) for f in fields])
         for r, vals in updates],
        fields))
    return len(updates)
