"""catmap_import — 把飞书类目映射明细补回库(飞书 → 库,与 export 反向)。

用法:
  python cli.py catmap_import --dry-run     # 只对账:飞书有多少行库里没有 ← 先跑这个
  python cli.py catmap_import               # 补进库(**只增不改**)
  python cli.py catmap_import -p update=1   # 连已有行的置信度/备注一并覆盖

为什么需要它(2026-08-17 生产事故):`audit.walmart_category_map` 是当年从
`mapping_detail_v5.5.xlsx` 一次性导进来的 15,770 行;而飞书那张表**此后一直
在被维护**(catmap_mine 的挖掘成果、人工修的冲突),涨到 17,592 行。
库从来没往回同步过 —— 也就是说**飞书那份才是新的,库里那份是旧快照**。
首版 `catmap_export` 把库整表重写到飞书,净删 1,847 行映射(其中 1,695 行
指向有效 PT)。护栏已补进 export,而缺的那批要靠这条补回来。

⚠ 缺省**只增不改**(`ON CONFLICT DO NOTHING`):库里已有的行可能被
catmap_fix/align 改过,拿飞书的旧值盖回去等于把修复回滚。要覆盖得显式 update=1。

⚠ 表头必须与 `resources.CATMAP_SHEET_HEADER` 对得上,对不上直接停 ——
飞书表是人在维护的,列被插一列/改个名,按位置读就会整表错位而且不报错。
"""

import logging

from api import feishu
from registry import db, resources

DANGEROUS = True        # 写库(虽然只增),按纪律先 dry-run 人眼确认

logger = logging.getLogger("workflows.catmap_import")

_INSERT = """
INSERT INTO audit.walmart_category_map
  (amazon_category, walmart_product_type, amazon_leaf, browse_node_id,
   rank_in_pt, confidence, match_type, notes, source_batch)
VALUES (%(path)s, %(pt)s, %(leaf)s, %(node)s, %(rank)s, %(conf)s,
        %(mt)s, %(notes)s, %(batch)s)
ON CONFLICT (amazon_category, walmart_product_type) DO NOTHING
"""

_UPSERT = _INSERT.replace("DO NOTHING", """DO UPDATE SET
    amazon_leaf = EXCLUDED.amazon_leaf,
    browse_node_id = EXCLUDED.browse_node_id,
    rank_in_pt = EXCLUDED.rank_in_pt,
    confidence = EXCLUDED.confidence,
    match_type = EXCLUDED.match_type,
    notes = EXCLUDED.notes,
    source_batch = EXCLUDED.source_batch,
    synced_at = now()""")

_EXISTING = """
SELECT amazon_category, walmart_product_type FROM audit.walmart_category_map
"""


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def read_sheet() -> list[dict]:
    """输入:无 → 输出:飞书映射明细全部数据行(按表头校验过列序)。"""
    sheet = resources.CATMAP_SHEET
    total = feishu.sheet_row_count(sheet)
    if total < 2:
        return []
    hdr = resources.CATMAP_SHEET_HEADER
    last = chr(ord("A") + len(hdr) - 1)
    values = feishu.sheet_values(sheet, f"A1:{last}{total}")
    if not values:
        return []
    got = tuple((str(c).strip() if c else "") for c in values[0][:len(hdr)])
    if got != hdr:
        # 宁炸不吞:按位置读一张被人改过列的表 = 整表错位而且不报错
        raise ValueError(f"飞书表头与登记不符,已停手。\n  登记:{hdr}\n  实际:{got}")
    out = []
    for raw in values[1:]:
        cells = [(str(c).strip() if c is not None else "")
                 for c in raw] + [""] * len(hdr)
        d = dict(zip(resources.CATMAP_SHEET.columns, cells[:len(hdr)]))
        if d["amazon_category"] and d["walmart_product_type"]:
            out.append(d)
    return out


def run(params: dict) -> str:
    execute = bool(params.get("execute")) and not params.get("dry_run")
    update = bool(params.get("update"))
    rows = read_sheet()
    if not rows:
        return "飞书类目映射明细:一行都没读到(表空 or token 没配),已停手"

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_EXISTING)
            have = {(a, p) for a, p in cur.fetchall()}
        new = [r for r in rows
               if (r["amazon_category"], r["walmart_product_type"]) not in have]

        lines = [f"{'' if execute else '🧪 [DRY-RUN] '}飞书 {len(rows)} 行 / "
                 f"库 {len(have)} 行:**飞书有库里没有 {len(new)} 行**"]
        if len(have) > len(rows):
            # 反过来也要说:库比飞书多说明飞书那边被删过或导出过旧版本
            lines.append(f"  ⚠ 库里另有 {len(have) - len(rows)} 行飞书没有 —— "
                         f"本工作流**不删任何东西**,只是提醒你两边不一致")
        if not new:
            lines.append("  两边一致,无需补" if not update else "  无新增行")
            if not update:
                return "\n".join(lines)

        by_pt: dict[str, int] = {}
        for r in new:
            by_pt[r["walmart_product_type"]] = \
                by_pt.get(r["walmart_product_type"], 0) + 1
        top = sorted(by_pt.items(), key=lambda kv: -kv[1])[:10]
        if top:
            lines.append("  缺的按 PT 排前 10:" +
                         "、".join(f"{p}({n})" for p, n in top))
        lines.append(f"  写入语义:{'覆盖已有行(update=1)' if update else '**只增不改**'}"
                     f" —— {'飞书值盖库值' if update else '库里已有的一个字不动'}")
        if not execute:
            lines += ["", "(dry-run:一行未写;去掉 --dry-run 才落库)"]
            return "\n".join(lines)

        sql = _UPSERT if update else _INSERT
        todo = rows if update else new
        n = 0
        with conn.cursor() as cur:
            for r in todo:
                cur.execute(sql, {
                    "path": r["amazon_category"],
                    "pt": r["walmart_product_type"],
                    "leaf": r["amazon_leaf"] or None,
                    "node": r["browse_node_id"] or None,
                    "rank": _int(r["rank_in_pt"]),
                    "conf": r["confidence"] or None,
                    "mt": r["match_type"] or None,
                    "notes": r["notes"] or None,
                    "batch": r["source_batch"] or None,
                })
                n += cur.rowcount
        conn.commit()
    lines.append(f"  已写库 {n} 行")
    lines.append("  ⚠ 补完映射后,受影响的产品要重审才会用上新映射:"
                 "`product_audit -p mode=pending`(它们多半卡在 pending)")
    return "\n".join(lines)
