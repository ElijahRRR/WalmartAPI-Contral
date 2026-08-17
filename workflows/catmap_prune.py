"""catmap_prune — 清掉「改过了但旧行没清」的死映射(降级留痕,不删行)。

用法:
  python cli.py catmap_prune --dry-run    # 只列清单 ← 先跑这个
  python cli.py catmap_prune              # 降级为低置信 + 备注留痕
  python cli.py catmap_prune -p delete=1  # 真删行(需要显式要求)

**只处理一种行**:PT 不在 `walmart_pt_meta`(字典外,永远不会被采纳),
**而同一条 Amazon 路径上已经有另一行的 PT 是有效的**。也就是说:当年的修正
做过了,正确答案就在隔壁,这行只是没清。生产实测 2026-08-17:所有者映射表里
150 行死映射,150 行全是这种,真孤儿 0 个。

⚠ 它们**不是无害的脏数据**。装配 L1 的 catmap/node_map 时,同一路径出现两个
DISTINCT PT 会被判成"两义"而整条丢弃 —— **连那条有效的一起没了**。
生产实测因此白丢 105 条本可直出的路径(如 `… > Calipers → Automotive Brakes`
被 `Disc Brake Calipers` 挤掉),表现是那些产品 L1 解不出判 pending,
而映射表里明明写着答案。装配侧已修(先过 pt_meta 闸再计票),
但映射表本身的脏数据还在,清掉它才算修完。

**缺省降级不删**(沿用 catmap_fix 的纪律:旧映射是历史证据,删了就查不出
"当初为什么这么映")。降级后 confidence='低' 不再进高置信通道,备注追加原因。
真要删加 `-p delete=1`。
"""

import logging

from registry import db

DANGEROUS = True        # 改映射表,按纪律先 dry-run 人眼确认

logger = logging.getLogger("workflows.catmap_prune")

# 与 pt_census 的 _SQL_SUPERSEDED 同一口径,这里要逐行(带主键)而不是汇总
_SQL_PICK = """
SELECT d.amazon_category, d.walmart_product_type, d.confidence, d.notes,
       min(g.walmart_product_type) AS good_pt
FROM audit.walmart_category_map d
LEFT JOIN audit.walmart_pt_meta dm
       ON dm.walmart_product_type = d.walmart_product_type
JOIN audit.walmart_category_map g
       ON g.amazon_category = d.amazon_category
      AND g.walmart_product_type <> d.walmart_product_type
JOIN audit.walmart_pt_meta gm
       ON gm.walmart_product_type = g.walmart_product_type
WHERE dm.walmart_product_type IS NULL
  AND d.walmart_product_type NOT IN ('无对应Walmart PT', '-', '')
GROUP BY d.amazon_category, d.walmart_product_type, d.confidence, d.notes
ORDER BY d.walmart_product_type, d.amazon_category
"""

_DOWNGRADE = """
UPDATE audit.walmart_category_map
SET confidence = '低',
    notes = coalesce(notes, '') || %(tag)s,
    synced_at = now()
WHERE amazon_category = %(path)s AND walmart_product_type = %(pt)s
"""

_DELETE = """
DELETE FROM audit.walmart_category_map
WHERE amazon_category = %(path)s AND walmart_product_type = %(pt)s
"""


def run(params: dict) -> str:
    execute = bool(params.get("execute")) and not params.get("dry_run")
    delete = bool(params.get("delete"))

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_PICK)
            rows = cur.fetchall()
        if not rows:
            return "映射表里没有「改过了但旧行没清」的行 ✅"

        pts = sorted({r[1] for r in rows})
        paths = len({r[0] for r in rows})
        mode = "删除" if delete else "降级为低置信(不删,留历史证据)"
        lines = [f"{'' if execute else '🧪 [DRY-RUN] '}"
                 f"「改过了但旧行没清」的死映射:{len(rows)} 行 / "
                 f"{len(pts)} 个死 PT / {paths} 条路径",
                 f"  处置方式:{mode}",
                 f"  ⚠ 清掉它们同时修好两件事:①映射表本身干净;"
                 f"②同路径不再出现两个 DISTINCT PT —— 装配 L1 catmap 时"
                 f"两义会把**那条有效的一起丢掉**(装配侧已修,数据侧靠这条)"]
        for path, pt, conf, _n, good in rows[:20]:
            lines.append(f"    {pt}({conf}) → 同路径已有 **{good}**")
            lines.append(f"        {path[:90]}")
        if len(rows) > 20:
            lines.append(f"    …另有 {len(rows) - 20} 行")

        if not execute:
            lines += ["", "(dry-run:一行未改;去掉 --dry-run 才落库)"]
            return "\n".join(lines)

        n = 0
        with conn.cursor() as cur:
            for path, pt, _c, _n2, good in rows:
                if delete:
                    cur.execute(_DELETE, {"path": path, "pt": pt})
                else:
                    cur.execute(_DOWNGRADE, {
                        "path": path, "pt": pt,
                        # 留痕写清"为什么降"与"正确答案是谁",下次有人翻到能看懂
                        "tag": f" [catmap_prune:PT 不在 walmart_pt_meta,"
                               f"同路径已有有效 PT {good},降级]"})
                n += cur.rowcount
        conn.commit()
    lines.append(f"  已{'删除' if delete else '降级'} {n} 行")
    lines.append("  下一步:受影响的产品要重审才会用上恢复的映射 —— "
                 "`python cli.py product_audit -p mode=pending`")
    return "\n".join(lines)
