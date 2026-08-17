"""catmap_export — 把类目映射明细投影到飞书(给人看的一面镜子)。

用法:
  python cli.py catmap_export --dry-run          # 只统计,不写飞书 ← 先跑这个
  python cli.py catmap_export                    # 整表重写
  python cli.py catmap_export -p pt=Hammers      # 只导某个 PT(排查用,仍整表重写)
  python cli.py catmap_export -p node_only=1     # 只导有 browse_node_id 的行

所有者定稿 2026-08-17:「以前的审核系统是从这里拿的,我们现在直接当映射查看使用」。

⚠ **方向是库 → 飞书,不是反过来。** 权威在 `audit.walmart_category_map`
(catmap_mine/suggest/fix/align 那条链维护它),这张表只是镜子。
在表里改一个格子不会影响任何判定,下一次导出就被覆盖 —— 要改映射走那条链。

列序即所有者手上那份的表头(`resources.CATMAP_SHEET_HEADER`),一个字都不许动:
他那边的筛选/公式按列位置写死,列一挪就全废,而且不报错。

整表重写(sheet_overwrite):行数变少时尾部多余行会被删掉 —— 映射被 catmap_fix
删掉的行必须跟着从表里消失,残留一行就是"看着还有映射,实际没有"。
"""

import logging

from api import feishu
from registry import db, resources

DANGEROUS = False       # 只写飞书镜子,不碰沃尔玛也不改库

logger = logging.getLogger("workflows.catmap_export")

# 列序 = registry.CATMAP_SHEET.columns,别在这里另排一遍
_SQL = """
SELECT m.walmart_product_type,
       t.walmart_category,
       t.walmart_ptg,
       m.amazon_leaf,
       m.amazon_category,
       m.browse_node_id,
       m.rank_in_pt,
       m.confidence,
       m.match_type,
       m.notes,
       m.source_batch,
       -- PT 在不在 pt_meta 里 —— 映射指向一个字典外的 PT 就是**死映射**:
       -- 审核 resolve_pt 末尾那道防御会把它作废判 pending,等于这行白填
       (t.walmart_product_type IS NOT NULL) AS pt_alive
FROM audit.walmart_category_map m
LEFT JOIN audit.walmart_pt_meta t
       ON t.walmart_product_type = m.walmart_product_type
WHERE (%(pt)s = '' OR m.walmart_product_type = %(pt)s)
  AND (NOT %(node_only)s OR m.browse_node_id IS NOT NULL)
ORDER BY t.walmart_category NULLS LAST, m.walmart_product_type,
         m.rank_in_pt NULLS LAST, m.amazon_category
"""


def _cell(v) -> str:
    return "" if v is None else str(v)


def run(params: dict) -> str:
    execute = bool(params.get("execute")) and not params.get("dry_run")
    pt = str(params.get("pt", "")).strip()
    node_only = bool(params.get("node_only"))

    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL, {"pt": pt, "node_only": node_only})
        rows = cur.fetchall()
    if not rows:
        return f"类目映射:没有匹配的行(pt={pt!r} node_only={node_only})"

    dead = [r for r in rows if not r[-1]]
    body = [[_cell(v) for v in (r[1], r[2], r[0], *r[3:11])] for r in rows]
    #        ↑ 表头是 Category/PTG/PT,SQL 取的是 PT 打头,这里换回表头的序

    lines = [f"{'' if execute else '🧪 [DRY-RUN] '}类目映射明细 {len(rows)} 行"
             f"{f'(只 PT={pt})' if pt else ''}"
             f"{'(只带 node_id 的)' if node_only else ''}"]
    pts = len({r[0] for r in rows})
    with_node = sum(1 for r in rows if r[5])
    lines.append(f"  覆盖 {pts} 个 PT;带 browse_node_id 的 {with_node} 行"
                 f"(不带的只能按路径字符串匹配,类目改名就失效)")
    if dead:
        # ⚠ 这才是所有者要找的东西:映射指向字典外 PT = 这行映射白填。
        # 旧系统的 L1 拿它当答案直出,新系统会把它作废判 pending —— 两边
        # 表现完全不同,而表面上映射"是有的"
        bad_pts = sorted({r[0] for r in dead})
        lines.append(
            f"  ⚠ **{len(dead)} 行映射指向 walmart_pt_meta 里没有的 PT**"
            f"({len(bad_pts)} 个 PT)—— 这些映射是死的:审核解出这个 PT 之后"
            f"会被字典闸作废判 pending,等于没映射")
        lines.append("     " + "、".join(bad_pts[:12])
                     + (f" …另 {len(bad_pts) - 12} 个" if len(bad_pts) > 12
                        else ""))

    if not execute:
        lines += ["", "(dry-run:一格未写;去掉 --dry-run 才推飞书)"]
        return "\n".join(lines)

    n = feishu.sheet_overwrite(resources.CATMAP_SHEET,
                               [list(resources.CATMAP_SHEET_HEADER)] + body)
    lines.append(f"  已整表重写飞书「{resources.CATMAP_SHEET.name}」{n} 行"
                 f"(含表头;行数变少时尾部多余行已删)")
    lines.append("  ⚠ 这张表是**镜子**:在里面改格子不影响任何判定,"
                 "下次导出即被覆盖 —— 改映射走 catmap_fix/suggest/mine 那条链")
    return "\n".join(lines)
