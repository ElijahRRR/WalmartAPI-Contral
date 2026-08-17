"""catmap_promote — 把 LLM 建议(catmap_suggest 的产物)升级进映射表。

用法:
  python cli.py catmap_promote --dry-run          # 看会写什么 ← 先跑这个
  python cli.py catmap_promote                    # 写成**中置信**(只进候选,不直出)
  python cli.py catmap_promote -p as=高 -p paths=路径A,路径B   # 逐条确认后直出
  python cli.py catmap_promote -p limit=500       # 一次多写一些

`catmap_suggest` 的建议一直是**死数据零消费**(它自己的头注写着"升级通道等
所有者定编辑权威再建")。编辑权威已于 2026-08-17 定下来:**PG 是权威,
飞书「映射明细」是镜子**(catmap_export 库→飞书;catmap_import 只是那次
覆盖事故的补救路径)。所以升级通道就落在这里,直写 PG。

⚠ **缺省写「中」不写「高」,这是刻意的。**
L1 的 ②级直出闸硬筛 `confidence='高'`:
  高  → 直接当答案用,不再问 LLM;
  中/低 → 只进候选,仍由 LLM 在候选里挑。
而 LLM 建议**确实会错**,还错得很自信 —— 所有者 2026-08-17 首批 50 条里就有
`Building Toys > Building Sets → Advent Calendars(高)`、
`Xbox 360 > Accessories → Other Bicycle Parts(中)`。写成高 = 这些路径的产品
从此直出到错类目,上架到错类目比不上架更麻烦。
写成中已经是巨大改善:那些路径现在是**零候选**(L1 只能两阶段从头开放判定,
所有者实测 200 个里 196 个走这条),给个候选让 LLM 挑,准确率与成本都好得多。

要某几条直出,`-p as=高` **必须配 `-p paths=`** 点名 —— 不给点名就写高,
等于把"人工确认"这一步跳过了。

只收 `status='ok'` 的建议;`excluded`(LLM 判禁售)/`unknown`(候选都不匹配)/
`no_candidate`(召回全空)/`llm_failed` 一律不动 —— 前两者是**结论**不是缺口,
后两者要人工或补数据。
"""

import logging

from registry import db

DANGEROUS = True        # 写映射表 = 影响判定,按纪律先 dry-run

logger = logging.getLogger("workflows.catmap_promote")

# 只挑 ok 且映射表里还没有这条路径的(已有的不覆盖:那可能是人工修过的)
_PICK = """
SELECT s.amazon_category, s.suggested_pt, s.confidence, s.product_count,
       s.browse_node_id, s.sample_title
FROM audit.category_map_suggestions s
JOIN audit.walmart_pt_meta m ON m.walmart_product_type = s.suggested_pt
WHERE s.status = 'ok' AND s.suggested_pt IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM audit.walmart_category_map c
                  WHERE btrim(c.amazon_category) = btrim(s.amazon_category))
  AND ({path_filter})
ORDER BY s.product_count DESC NULLS LAST
LIMIT %(limit)s
"""
# ↑ JOIN pt_meta 不是多余的:建议的 PT 必须在字典里,否则写进去又是一条死映射
#   (2026-08-17 刚清完 150 条那种)

_INSERT = """
INSERT INTO audit.walmart_category_map
  (amazon_category, walmart_product_type, browse_node_id, confidence,
   match_type, notes, source_batch)
VALUES (%(path)s, %(pt)s, %(node)s, %(conf)s, 'llm_suggest',
        %(notes)s, 'catmap_promote')
ON CONFLICT (amazon_category, walmart_product_type) DO NOTHING
"""

_MARK = """
UPDATE audit.category_map_suggestions SET status = 'promoted'
WHERE amazon_category = %s
"""


def run(params: dict) -> str:
    execute = bool(params.get("execute")) and not params.get("dry_run")
    limit = int(params.get("limit", 500))
    conf = str(params.get("as", "中")).strip() or "中"
    paths = [p.strip() for p in str(params.get("paths", "")).split(",")
             if p.strip()]

    if conf not in ("高", "中", "低"):
        raise ValueError(f"as 只能是 高/中/低,给的是 {conf!r}")
    if conf == "高" and not paths:
        # 不点名就写高 = 把"人工确认"这一步跳过了。宁炸不吞
        raise ValueError(
            "as=高 必须配 -p paths=路径1,路径2 点名 —— 高置信会让 L1 ②级"
            "**直出**不再问 LLM,而 LLM 建议确实会错(实测首批 50 条里就有 "
            "Building Sets→Advent Calendars 这种)。逐条确认过的才写高")

    flt = "s.amazon_category = ANY(%(paths)s)" if paths else "TRUE"
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_PICK.format(path_filter=flt),
                        {"limit": limit, "paths": paths})
            rows = cur.fetchall()
        if not rows:
            return ("没有可升级的建议(status='ok'、PT 在字典里、且映射表里"
                    "还没有这条路径)。先跑 `python cli.py catmap_suggest`")

        covered = sum(r[3] or 0 for r in rows)
        lines = [f"{'' if execute else '🧪 [DRY-RUN] '}"
                 f"可升级建议 {len(rows)} 条路径,覆盖 {covered} 件产品 "
                 f"→ 写成**{conf}**置信"]
        lines.append(
            "  高 = L1 ②级直出(不再问 LLM);中/低 = 只进候选,仍由 LLM 在候选里挑"
            if conf == "高" else
            "  中/低只进候选,仍由 LLM 挑 —— 但那些路径现在是**零候选**"
            "(只能两阶段从头开放判定),给候选是巨大改善")
        for path, pt, c, n, _node, _t in rows[:15]:
            lines.append(f"    {n or 0:>6} 件|{path[:64]} → **{pt}**(建议 {c})")
        if len(rows) > 15:
            lines.append(f"    …另有 {len(rows) - 15} 条")

        if not execute:
            lines += ["", "(dry-run:一行未写;去掉 --dry-run 才落库)"]
            return "\n".join(lines)

        n_ins = 0
        with conn.cursor() as cur:
            for path, pt, c, cnt, node, title in rows:
                cur.execute(_INSERT, {
                    "path": path, "pt": pt, "node": node, "conf": conf,
                    # 留痕:谁写的、依据是什么、当时 LLM 给的置信是多少 ——
                    # 以后有人翻到这行要能看出"这不是人定的,是机器建议升上来的"
                    "notes": f"[catmap_promote:LLM 建议升级,原建议置信 {c},"
                             f"样例「{(title or '')[:40]}」]",
                })
                n_ins += cur.rowcount
                cur.execute(_MARK, (path,))
        conn.commit()
    lines.append(f"  已写映射表 {n_ins} 条({conf}),建议表对应行标 promoted")
    lines.append("  下一步:`python cli.py product_audit -p mode=pending` "
                 "让卡住的产品用上新映射")
    return "\n".join(lines)
