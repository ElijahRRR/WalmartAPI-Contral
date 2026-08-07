"""upc_sync — UPC 池注入同步与投影回写(listing L2a;只读沃尔玛零调用,非危险)。

用法:
  python cli.py upc_sync            # 表格新号入库 + 状态列回写 + 池余量摘要

流程:读「UPC池」表(A=UPC B=放入日期,运营注入域)→ 新号入库
(catalog.upc_pool,首位白名单校验,非法前缀标注永不分配)→ 按 PG 权威
回写 C=状态 D=店铺 E=SKU F=上架日期(仅差异行)→ 摘要报池余量。

调度建议:每日一次 + 上架主链(L2d)跑前顺带;领号/用号/回收由主链
在提交事务内调 services/upc_pool,本工作流只管注入与展示。
"""

import logging

from api import feishu
from registry import db, resources
from services import upc_pool

DANGEROUS = False

logger = logging.getLogger("workflows.upc_sync")


def run(params: dict) -> str:
    """输入:params(无参)→ 输出:注入与回写摘要。"""
    sheet = resources.UPC_SHEET
    try:
        sheet.require()
    except LookupError as e:
        return f"UPC池表未登记:{e}"

    total = feishu.sheet_row_count(sheet)
    values = feishu.sheet_values(sheet, f"A2:F{total}") if total >= 2 else []
    rows = []
    for i, raw in enumerate(values):
        cells = [(str(c).strip() if c is not None else "") for c in raw] + [""] * 6
        if cells[0]:
            rows.append({"rownum": i + 2, "upc_raw": cells[0],
                         "put_date": cells[1], "shown": cells[2:6]})
    if not rows:
        return "UPC池表无数据行"

    with db.pg_conn() as conn:
        n_new, n_bad = upc_pool.sync_rows(
            conn, [(r["upc_raw"], r["put_date"]) for r in rows])
        info = upc_pool.lookup(conn, [upc_pool.normalize(r["upc_raw"])
                                      for r in rows])
        stats = upc_pool.pool_stats(conn)

    updates = []
    for r in rows:
        st = info.get(upc_pool.normalize(r["upc_raw"]))
        if st is None:
            continue
        status, store, sku, used_at = st
        vals = [upc_pool.STATUS_CN.get(status, status), store or "", sku or "",
                used_at.strftime("%Y-%m-%d") if used_at else ""]
        if vals != r["shown"]:
            updates.append((f"C{r['rownum']}:F{r['rownum']}", [vals]))
    written = feishu.sheet_write_ranges(sheet, updates) if updates else 0

    return (f"UPC池:表内 {len(rows)} 行,新入库 {n_new}"
            + (f",非法前缀 {n_bad}(已标注永不分配)" if n_bad else "")
            + f",状态回写 {written} 行;"
            f"池余量:未用 {stats.get('', 0)},已领 {stats.get('claimed', 0)},"
            f"已用 {stats.get('used', 0)},冲突 {stats.get('conflict', 0)}")
