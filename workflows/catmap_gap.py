"""catmap_gap — 类目映射缺口清单(树×产品×映射三方对账;纯 SQL 只读)。

用法:
  python cli.py catmap_gap              # Top 30 缺口(按产品数)
  python cli.py catmap_gap -p limit=200
  python cli.py catmap_gap -p only=unmapped_with_products   # 只看某一类

三方数据源(所有者定稿 2026-08-14,全部以 browse_node_id 为键):
  audit.amazon_taxonomy       亚马逊类目全集(3.2 万 node,规范名/父子/叶子)
  catalog.products            我们实际有货的类目(browse_node_id + 产品数)
  audit.walmart_category_map  已有映射(1.57 万 node)

四类缺口(按处置去向分,报告分列):
  A 有产品·无映射·有实证 PT  → catmap_mine 能挖(零 LLM,先跑它)
  B 有产品·无映射·无实证 PT  → catmap_suggest(LLM)或人工
  C 有产品·无映射·树里也没有 → 树没抓全 / 采集侧新类目(告知所有者补抓)
  D 无产品·无映射            → 亚马逊有但我们没货,**不必处理**(别浪费 LLM)
排序一律按产品数降序:先补覆盖面大的,收益最大。
"""

import logging

from registry import db

DANGEROUS = False

logger = logging.getLogger("workflows.catmap_gap")

_SUMMARY_SQL = """
WITH prod AS (
    SELECT browse_node_id AS node, count(*) AS n,
           count(*) FILTER (WHERE walmart_pt IS NOT NULL
                              AND walmart_pt <> 'unknown') AS n_pt
    FROM catalog.products
    WHERE marketplace = 'US' AND browse_node_id IS NOT NULL
    GROUP BY 1
), mapped AS (
    SELECT DISTINCT browse_node_id AS node
    FROM audit.walmart_category_map
    WHERE confidence = '高' AND browse_node_id IS NOT NULL
), tax AS (
    SELECT node_id AS node FROM audit.amazon_taxonomy
)
SELECT
  (SELECT count(*) FROM tax)                                        AS tax_nodes,
  (SELECT count(*) FROM prod)                                       AS prod_nodes,
  (SELECT count(*) FROM mapped)                                     AS map_nodes,
  (SELECT count(*) FROM prod WHERE node IN (SELECT node FROM mapped)) AS prod_mapped,
  (SELECT count(*) FROM prod p WHERE p.node NOT IN (SELECT node FROM mapped)
     AND p.n_pt > 0)                                                AS gap_a,
  (SELECT coalesce(sum(n), 0) FROM prod p
     WHERE p.node NOT IN (SELECT node FROM mapped) AND p.n_pt > 0)   AS gap_a_prod,
  (SELECT count(*) FROM prod p WHERE p.node NOT IN (SELECT node FROM mapped)
     AND p.n_pt = 0)                                                AS gap_b,
  (SELECT coalesce(sum(n), 0) FROM prod p
     WHERE p.node NOT IN (SELECT node FROM mapped) AND p.n_pt = 0)   AS gap_b_prod,
  (SELECT count(*) FROM prod p WHERE p.node NOT IN (SELECT node FROM tax)) AS gap_c,
  (SELECT count(*) FROM tax t WHERE t.node NOT IN (SELECT node FROM prod)
     AND t.node NOT IN (SELECT node FROM mapped))                   AS gap_d
"""

_LIST_SQL = """
WITH prod AS (
    SELECT browse_node_id AS node, count(*) AS n,
           count(*) FILTER (WHERE walmart_pt IS NOT NULL
                              AND walmart_pt <> 'unknown') AS n_pt
    FROM catalog.products
    WHERE marketplace = 'US' AND browse_node_id IS NOT NULL
    GROUP BY 1
)
SELECT p.node, p.n, p.n_pt,
       t.name, t.path, t.is_leaf,
       (t.node_id IS NULL) AS not_in_tree
FROM prod p
LEFT JOIN audit.amazon_taxonomy t ON t.node_id = p.node
WHERE NOT EXISTS (SELECT 1 FROM audit.walmart_category_map m
                  WHERE m.browse_node_id = p.node AND m.confidence = '高')
  AND ({filter})
ORDER BY p.n DESC
LIMIT %(limit)s
"""

_FILTERS = {
    "all": "true",
    "unmapped_with_pt": "p.n_pt > 0",                       # A:能挖
    "unmapped_no_pt": "p.n_pt = 0",                         # B:要 LLM/人工
    "not_in_tree": "t.node_id IS NULL",                     # C:树没抓全
}


def run(params: dict) -> str:
    """输入:params(limit / only=筛选类)→ 输出:缺口分类统计 + Top 清单。"""
    limit = int(params.get("limit", 30))
    only = str(params.get("only", "all")).strip()
    if only not in _FILTERS:
        raise ValueError(f"only 可选:{sorted(_FILTERS)}")

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SUMMARY_SQL)
            cols = [d[0] for d in cur.description]
            s = dict(zip(cols, cur.fetchone()))
            cur.execute(_LIST_SQL.format(filter=_FILTERS[only]),
                        {"limit": limit})
            rows = cur.fetchall()

    if not s["tax_nodes"]:
        return ("catmap_gap:audit.amazon_taxonomy 为空——先跑 "
                "taxonomy_import -p file=<对账版类目树> -p apply=1")
    pm = s["prod_mapped"]
    lines = [
        f"catmap_gap(类目树 {s['tax_nodes']} node / 产品侧 {s['prod_nodes']} "
        f"node / 映射表 {s['map_nodes']} node)",
        f"覆盖:产品侧已映射 {pm}"
        + (f"({pm / s['prod_nodes'] * 100:.1f}%)" if s["prod_nodes"] else ""),
        f"A 有产品·无映射·**有实证 PT** {s['gap_a']} node / {s['gap_a_prod']} 件"
        f" → catmap_mine 能挖(零 LLM,先跑它)",
        f"B 有产品·无映射·无实证 PT {s['gap_b']} node / {s['gap_b_prod']} 件"
        f" → catmap_suggest(LLM)或人工",
        f"C 有产品·**树里也没有** {s['gap_c']} node → 树没抓全或采集侧新类目",
        f"D 无产品·无映射 {s['gap_d']} node → 亚马逊有但我们没货,**不必处理**",
        f"—— Top {len(rows)}(only={only},按产品数降序)——",
    ]
    for node, n, n_pt, name, path, is_leaf, not_in_tree in rows:
        tag = ("树外" if not_in_tree else ("叶" if is_leaf else "非叶"))
        label = (path or name or "(树里无此 node)")
        lines.append(f"  {n:>6} 件(带 PT {n_pt:>5})|node {node}|{tag}|"
                     f"{label[:70]}")
    return "\n".join(lines)
