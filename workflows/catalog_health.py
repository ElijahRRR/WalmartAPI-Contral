"""catalog_health — 产品库数据完备度体检(纯 SQL 只读;随时可跑)。

用法:
  python cli.py catalog_health

所有者定稿 2026-08-14 的实施顺序第一步:**先查哪些产品没有亚马逊数据**
——不知道分母就谈不上优化分子。报告四类:

  A 采集完备度:标题 / 类目路径 / 类目 ID / 图片 各有多少(空壳行单列);
  B 类目锚:有 ID 且能在映射表直出 PT 的比例(ID 缺口 = 待挖掘面);
  C PT 与类目的交叉:有 PT 有类目(挖掘燃料)/ 有类目无 PT(待判定)/
    有 PT 无类目(反哺不了映射)/ 两者皆无(先补采集);
  D 审核结论分布。

只读不写,不调 LLM。
"""

import logging

from registry import db

DANGEROUS = False

logger = logging.getLogger("workflows.catalog_health")

_SQL = """
WITH p AS (
    SELECT
        title IS NOT NULL AND btrim(title) <> ''            AS has_title,
        amazon_category IS NOT NULL
            AND btrim(amazon_category) <> ''                AS has_path,
        browse_node_id IS NOT NULL                          AS has_node,
        image_url IS NOT NULL                               AS has_img,
        walmart_pt IS NOT NULL AND walmart_pt <> 'unknown'  AS has_pt,
        audit_status
    FROM catalog.products WHERE marketplace = 'US'
)
SELECT count(*)                                               AS total,
       count(*) FILTER (WHERE has_title)                      AS n_title,
       count(*) FILTER (WHERE has_path)                       AS n_path,
       count(*) FILTER (WHERE has_node)                       AS n_node,
       count(*) FILTER (WHERE has_img)                        AS n_img,
       count(*) FILTER (WHERE NOT has_title AND NOT has_path) AS n_stub,
       count(*) FILTER (WHERE has_pt AND has_path)            AS n_pt_path,
       count(*) FILTER (WHERE has_path AND NOT has_pt)        AS n_path_only,
       count(*) FILTER (WHERE has_pt AND NOT has_path)        AS n_pt_only,
       count(*) FILTER (WHERE NOT has_pt AND NOT has_path)    AS n_neither,
       count(*) FILTER (WHERE audit_status = 'approved')      AS n_approved,
       count(*) FILTER (WHERE audit_status = 'rejected')      AS n_rejected,
       count(*) FILTER (WHERE audit_status = 'pending')       AS n_pending,
       count(*) FILTER (WHERE audit_status IS NULL)           AS n_unaudited
FROM p
"""
# ↑ 别名一律 ASCII:PG 把未加引号的标识符折成小写,'有类目ID' 会变
#   '有类目id'(中文不折、ASCII 折),按原文取键必 KeyError(生产实测
#   2026-08-14)。展示中文名在 Python 侧给

# 类目锚命中率:产品侧 node 去重后,有多少能在映射表高置信行直出 PT
_NODE_SQL = """
WITH n AS (
    SELECT DISTINCT browse_node_id AS node FROM catalog.products
    WHERE marketplace = 'US' AND browse_node_id IS NOT NULL
)
SELECT count(*) AS 去重node,
       count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM audit.walmart_category_map m
           WHERE m.browse_node_id = n.node AND m.confidence = '高')) AS 映射有
FROM n
"""

# 快照侧还能补多少 ID(node_backfill 未跑或增量新到)
_SNAP_SQL = """
SELECT count(DISTINCT s.asin)
FROM catalog.snapshots s
JOIN catalog.products p ON p.marketplace = s.marketplace AND p.asin = s.asin
WHERE s.marketplace = 'US' AND s.outcome = 'ok'
  AND s.raw ->> 'category_ids' IS NOT NULL
  AND btrim(s.raw ->> 'category_ids') <> ''
  AND p.browse_node_id IS NULL
"""


def run(params: dict) -> str:
    """输入:params(无参)→ 输出:四类完备度报告。"""
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL)
            cols = [d[0] for d in cur.description]
            r = dict(zip(cols, cur.fetchone()))
            cur.execute(_NODE_SQL)
            nodes, node_hit = cur.fetchone()
            cur.execute(_SNAP_SQL)
            (snap_fillable,) = cur.fetchone()

    n = r["total"] or 1

    def pct(x):
        return f"{x / n * 100:.1f}%"

    lines = [
        f"catalog_health(catalog.products US {r['total']} 行)",
        f"A 采集完备度:标题 {r['n_title']}({pct(r['n_title'])})/ "
        f"类目路径 {r['n_path']}({pct(r['n_path'])})/ "
        f"类目 ID {r['n_node']}({pct(r['n_node'])})/ 图 {r['n_img']}",
        f"  空壳行(无标题无类目){r['n_stub']}"
        f"——pt_backfill 占位 + 采集降级,不进审核候选",
        f"B 类目锚:产品侧去重 node {nodes},映射表能直出 PT {node_hit}"
        + (f"({node_hit / nodes * 100:.1f}%);缺口 {nodes - node_hit} 个 node"
           f"——挖掘/LLM 的真实工作面" if nodes else ""),
        f"  快照里还能补 ID 的产品 {snap_fillable}"
        + ("(跑 node_backfill -p apply=1)" if snap_fillable else ""),
        f"C PT×类目交叉:有PT有类目 {r['n_pt_path']}(挖掘燃料)/ "
        f"有类目无PT {r['n_path_only']}(待判定)/ "
        f"有PT无类目 {r['n_pt_only']}(反哺不了映射)/ "
        f"两者皆无 {r['n_neither']}(先补采集)",
        f"D 审核结论:过 {r['n_approved']} / 拒 {r['n_rejected']} / "
        f"待定 {r['n_pending']} / 未审 {r['n_unaudited']}",
    ]
    return "\n".join(lines)
