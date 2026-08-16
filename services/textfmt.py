"""控制台文本对齐(报告类 workflow 共用)。

为什么是 services:`alloc_audit` 与 `alloc_stores` 都要按显示宽度对齐,
而工作流之间不准互相 import(铁律 1)——共用的东西只能沉到这里。

**核心一句**:`f"{s:<8}"` 按**字符数**补,而中日韩字符在终端占 2 格。
「同品牌」(3 字)与「同 ASIN」(7 字)补到同样字符数,显示宽度差 3 格,
列就是歪的 —— 满屏数字对不齐,人就读不下去(所有者 2026-08-15 反馈
"太难看了,我都看不明白",一半原因在这)。
"""

import unicodedata


def width(s: str) -> int:
    """输入:文本 → 输出:终端显示宽度(中日韩字符按 2 格算)。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, w: int, align: str = "<") -> str:
    """输入:文本 + 目标显示宽度(+ 对齐方向)→ 输出:补齐后的文本。

    `align='<'` 左对齐补右侧,`'>'` 右对齐补左侧。宽度不够就原样返回
    (**不截断**:截断会把店名切成看不出是谁,比列歪了更糟)。
    """
    gap = " " * max(0, w - width(s))
    return (s + gap) if align == "<" else (gap + s)


def grid(rows: list[tuple], indent: str = "  ", sep: str = "  ") -> list[str]:
    """输入:等长元组行 → 输出:各列按显示宽度左对齐的文本行(末列不补尾空格)。

    列宽不写死,按本次实际内容算 —— 加一列不用回来调常数。
    """
    if not rows:
        return []
    cols = max(len(r) for r in rows)
    cells = [[str(c) for c in r] + [""] * (cols - len(r)) for r in rows]
    w = [max(width(r[i]) for r in cells) for i in range(cols)]
    out = []
    for r in cells:
        line = sep.join([pad(c, w[i]) for i, c in enumerate(r[:-1])] + [r[-1]])
        out.append((indent + line).rstrip())
    return out


def table(header: list, rows: list[list], indent: str = "  ",
          align: str = "") -> list[str]:
    """输入:表头 + 数据行(+ 每列对齐方向串,如 "<>>>")→ 输出:对齐的表格行。

    与 `grid` 的区别:这里**每列独立指定左/右对齐**(数字列右对齐才好比大小),
    且表头与数据一起参与列宽计算。align 短于列数时,其余列默认右对齐。
    """
    all_rows = [[str(c) for c in header]] + [[str(c) for c in r] for r in rows]
    cols = max(len(r) for r in all_rows)
    all_rows = [r + [""] * (cols - len(r)) for r in all_rows]
    w = [max(width(r[i]) for r in all_rows) for i in range(cols)]
    dirs = [(align[i] if i < len(align) else ">") for i in range(cols)]
    return [(indent + "  ".join(pad(c, w[i], dirs[i])
                                for i, c in enumerate(r))).rstrip()
            for r in all_rows]
