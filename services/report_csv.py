"""报告 csv 落盘积木(**报告目录里的明细 csv 只有这一条落盘路径**)。

上移自 workflows/alloc_audit._write_csv(2026-08-27):同一段
「reports_dir().mkdir + utf-8-sig + newline='' + writer + 表头 + 行」此前在
六个工作流里各写一份(alloc_audit / alloc_products / alloc_plan 三处 /
claim_audit / pt_census 两处 / pt_spec_sync 两处)。

⚠ 两个参数是**给所有者用 Excel 直接打开**服务的,不许"顺手简化":
  · `encoding="utf-8-sig"` —— 带 BOM,否则 Excel 按本地代码页读,中文全乱码;
  · `newline=""` —— 交给 csv 模块自己写行结束符,否则 Windows 上每行之间
    多一个空行。

不在射程:DictWriter 的两处(alloc_stores / product_query)——后者还落
exports/ 且文件名带时间戳,形状不同,别硬套(2026-08-27 审计明确划界)。
"""

import csv
from pathlib import Path

from registry import paths


def write(name: str, header: list, rows: list) -> str:
    """输入:文件名 + 表头 + 行 → 输出:落盘路径(报告目录,每次覆盖)。"""
    return write_to(paths.reports_dir() / name, header, rows)


def write_to(path, header: list, rows: list) -> str:
    """输入:落盘路径(绝对/相对) + 表头 + 行 → 输出:落盘路径(父目录自动建,每次覆盖)。

    给"路径由所有者当场指定"的人工件用(`-p out=…`),**不是第二条落盘实现** ——
    `write` 就是它加一句"文件名解释成报告目录里的同名文件"。BOM 与 newline
    两个参数的理由见模块头,两条路都必须吃到,所以只能有一份写文件的代码。
    """
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8-sig") as fh:   # BOM:Excel 直开不乱码
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return str(p)
