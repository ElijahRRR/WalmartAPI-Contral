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

# all=1:**所有** PT 不在字典的行,不要求同路径有有效兄弟。
# 所有者定稿 2026-08-17:「不存在的 pt 直接清理掉……有问题的直接清理掉不好吗」。
# 死 PT 行在任何情况下都不可能被采纳(resolve_pt 与 L1 两道字典闸),
# 留着只有两个作用:占着计票位、让人以为"映射是有的"。
_SQL_PICK_ALL = """
SELECT d.amazon_category, d.walmart_product_type, d.confidence, d.notes,
       coalesce(min(g.walmart_product_type), '(同路径也没有有效 PT)') AS good_pt
FROM audit.walmart_category_map d
LEFT JOIN audit.walmart_pt_meta dm
       ON dm.walmart_product_type = d.walmart_product_type
LEFT JOIN audit.walmart_category_map g
       ON g.amazon_category = d.amazon_category
      AND g.walmart_product_type <> d.walmart_product_type
      AND EXISTS (SELECT 1 FROM audit.walmart_pt_meta gm
                  WHERE gm.walmart_product_type = g.walmart_product_type)
WHERE dm.walmart_product_type IS NULL
  AND d.walmart_product_type NOT IN ('无对应Walmart PT', '-', '')
  AND coalesce(d.notes, '') NOT LIKE '%%[catmap_prune:%%'
GROUP BY d.amazon_category, d.walmart_product_type, d.confidence, d.notes
ORDER BY d.walmart_product_type, d.amazon_category
"""

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
  -- ⚠ 幂等:降过的不再降(所有者 2026-08-17 连跑两次,同 150 行被处理两遍,
  -- 备注被重复追加)。降级本身是幂等的(还是 '低'),但备注会越追越长,
  -- 而且摘要会一直报"已降级 150 行" —— 看着像每天都在发现新问题
  AND coalesce(d.notes, '') NOT LIKE '%%[catmap_prune:%%'
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
    # all=1:不限"同路径有有效兄弟",所有字典外 PT 的行一律处理
    # (所有者定稿 2026-08-17:「不存在的 pt 直接清理掉」)
    take_all = bool(params.get("all"))

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_PICK_ALL if take_all else _SQL_PICK)
            rows = cur.fetchall()
        what = ("字典外 PT 的映射(全部)" if take_all
                else "「改过了但旧行没清」的行")
        if not rows:
            return f"映射表里没有{what} ✅"

        pts = sorted({r[1] for r in rows})
        paths = len({r[0] for r in rows})
        mode = "删除" if delete else "降级为低置信(不删,留历史证据)"
        lines = [f"{'' if execute else '🧪 [DRY-RUN] '}"
                 f"{what}:{len(rows)} 行 / "
                 f"{len(pts)} 个死 PT / {paths} 条路径",
                 f"  处置方式:{mode}",
                 f"  ⚠ 清掉它们同时修好两件事:①映射表本身干净;"
                 f"②同路径不再出现两个 DISTINCT PT —— 装配 L1 catmap 时"
                 f"两义会把**那条有效的一起丢掉**(装配侧已修,数据侧靠这条)"]
        orphan = [r for r in rows if str(r[4]).startswith("(")]
        if orphan:
            # 这些清掉就真没映射了 —— 得让人知道会留下多少空缺口,
            # 以及缺口由谁重建(别让它悄悄变成"这些产品从此解不出类目")
            lines.append(
                f"  ⚠ 其中 {len(orphan)} 行**同路径没有有效 PT** —— "
                f"清掉后这些路径成为映射缺口,由 catmap_mine(实证挖掘,零 LLM)"
                f"或 catmap_suggest(LLM)重建;先跑 catmap_gap 看清单")
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
