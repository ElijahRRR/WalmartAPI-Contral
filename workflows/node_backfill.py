"""node_backfill — 从已存快照回填类目 ID 锚(零重采;可重跑)。

用法:
  python cli.py node_backfill              # 预览:可回填量 / 映射命中率
  python cli.py node_backfill -p apply=1   # 写 products.browse_node_*

背景(采集器源码核实 2026-08-14):类目 ID 一直在抓也一直在传——采集侧
解析面包屑时 ID 与名字树同一处产出,只是增量导出的 `slow` 段只挑了名字树,
**ID 在 `raw` 段幸存**(`raw.category_ids`,逗号串 root→leaf)。我们的
`catalog.snapshots.raw` 原样存了这段 ⇒ **存量产品不必重采,就地提取即可**。

回填口径:每个 ASIN 取**最新一条带 category_ids 的 ok 快照**;链末段写
`products.browse_node_id`,全链写 `browse_node_chain`;**只填空不覆盖**
(已有值可能来自新一轮 ingest,更新)。回填后审核 L1 ②a 级立即用 ID
直查映射表(该表 browse_node_id 覆盖率 100%),不再受三套名称漂移影响。
"""

import logging

from registry import db
from services.product_ingest import category_nodes

DANGEROUS = False

logger = logging.getLogger("workflows.node_backfill")

# 每 ASIN 最新一条带 ID 的 ok 快照(raw.category_ids 现役键名)
_SRC_SQL = """
SELECT DISTINCT ON (s.asin) s.asin, s.raw ->> 'category_ids'
FROM catalog.snapshots s
WHERE s.marketplace = 'US' AND s.outcome = 'ok'
  AND s.raw ->> 'category_ids' IS NOT NULL
  AND btrim(s.raw ->> 'category_ids') <> ''
ORDER BY s.asin, s.scraped_at DESC
"""

_UPDATE_SQL = """
UPDATE catalog.products
SET browse_node_chain = %s, browse_node_id = %s
WHERE marketplace = 'US' AND asin = %s AND browse_node_id IS NULL
"""


def run(params: dict) -> str:
    """输入:params(apply=1 才写库)→ 输出:回填体检/结果摘要。"""
    apply = str(params.get("apply", "")).strip() == "1"

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SRC_SQL)
            rows = []
            for asin, ids in cur.fetchall():
                chain, node = category_nodes({}, {"category_ids": ids})
                if node:
                    rows.append((chain, node, asin))
            cur.execute("SELECT count(*) FROM catalog.products "
                        "WHERE marketplace = 'US' AND browse_node_id IS NOT NULL")
            (already,) = cur.fetchone()
            # 命中率体检:回填后有多少 ID 能在映射表里直接查到 PT
            nodes = list({r[1] for r in rows})
            cur.execute(
                "SELECT count(DISTINCT browse_node_id) "
                "FROM audit.walmart_category_map "
                "WHERE confidence = '高' AND browse_node_id = ANY(%s)",
                (nodes,))
            (hit_nodes,) = cur.fetchone()

        lines = [f"node_backfill:快照里带类目 ID 的 ASIN {len(rows)}"
                 f"(去重 node {len(nodes)}),products 已有 ID {already}",
                 f"其中 {hit_nodes} 个 node 在映射表高置信行里有 PT"
                 f"({hit_nodes / len(nodes) * 100:.1f}% 可直出)"
                 if nodes else "没有可回填的行"]
        if not rows:
            return "\n".join(lines)
        if not apply:
            lines.append("(预览:未写库;确认无误加 -p apply=1)")
            return "\n".join(lines)
        with conn.cursor() as cur:
            cur.executemany(_UPDATE_SQL, rows)
            filled = cur.rowcount
    lines.append(f"回填完成 ✓(只填空:实际写 {filled} 行;"
                 f"下一轮 product_audit ②a 级直接用 ID 命中)")
    return "\n".join(lines)
