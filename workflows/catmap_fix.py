"""catmap_fix — 按 node 定点修正映射表(人工圈定;危险:缺省即真跑,空跑用 --dry-run)。

用法:
  python cli.py catmap_fix -p nodes=2563847011,14008381 --dry-run   # 预览改动
  python cli.py catmap_fix -p nodes=2563847011,14008381             # 真改
  python cli.py catmap_fix -p nodes=all_conflicts               # 全部冲突行(慎)

用途:`catmap_mine` 报出的 `map_conflict`(实证 PT 与旧映射相左)只报不改,
本工作流把**人工圈定的那几条**落到映射表。所有者复核 2026-08-14:70 条
冲突里多数是实证更精确(Suncatchers→Sun Catchers、Camping Stoves→
Camping Stoves、Night Lights 等),少数语义变粗需人眼定夺
(Document Cameras→Webcams、Doll Making→Craft Eyes 等)。

⚠ 实证 PT 的语义:它来自**历史上真提交成功过的商品**(含后来被删的),
证明的是"沃尔玛接受过这个类目",不等于"语义最准确"。看着变粗的往往是
沃尔玛没有更细 PT——对上架成功率而言"接受过 > 语义正确"。

改法(保留证据,不毁旧行):
  1. 旧高置信行 confidence 降为 '低'、notes 追加降级原因(不删——旧映射
     是历史证据,删了就查不出"当初为什么这么映");
  2. 插入新行(代表路径 + 实证 PT + browse_node_id,match_type=
     'mined_conflict_fix',confidence='高');
  3. 建议表对应行标 'fixed'。
**没有批量模式的默认值**:nodes 必填,要么逐个列,要么显式 all_conflicts。
"""

import logging

from registry import db

DANGEROUS = True

logger = logging.getLogger("workflows.catmap_fix")


# 建议表自带 browse_node_id(catmap_mine node 键模式写入),直接按 ID 连接
_PICK_BY_NODE_SQL = """
SELECT s.browse_node_id, m.walmart_product_type,
       s.amazon_category, s.suggested_pt, s.support_count, s.product_count
FROM audit.category_map_suggestions s
JOIN audit.walmart_category_map m
  ON m.browse_node_id = s.browse_node_id AND m.confidence = '高'
WHERE s.status = 'map_conflict' AND s.browse_node_id = ANY(%(nodes)s)
"""

_ALL_CONFLICT_NODES_SQL = """
SELECT DISTINCT browse_node_id FROM audit.category_map_suggestions
WHERE status = 'map_conflict' AND browse_node_id IS NOT NULL
"""

_DEMOTE_SQL = """
UPDATE audit.walmart_category_map
SET confidence = '低',
    notes = concat_ws(' | ', notes,
        'catmap_fix 2026 降级:实证 PT ' || %(new_pt)s || ' 票数占优')
WHERE browse_node_id = %(node)s AND confidence = '高'
  AND walmart_product_type <> %(new_pt)s
"""

_INSERT_SQL = """
INSERT INTO audit.walmart_category_map
    (amazon_category, walmart_product_type, browse_node_id, confidence,
     match_type)
VALUES (%(path)s, %(new_pt)s, %(node)s, '高', 'mined_conflict_fix')
ON CONFLICT (amazon_category, walmart_product_type) DO UPDATE
SET confidence = '高', browse_node_id = EXCLUDED.browse_node_id,
    match_type = 'mined_conflict_fix'
"""

_MARK_SQL = """
UPDATE audit.category_map_suggestions SET status = 'fixed'
WHERE amazon_category = %s AND status = 'map_conflict'
"""


def run(params: dict) -> str:
    """输入:params(nodes=逗号列表或 all_conflicts;execute)→ 输出:改动摘要。"""
    raw = str(params.get("nodes", "")).strip()
    if not raw:
        raise ValueError("必填参数 nodes=<node_id 逗号列表> 或 nodes=all_conflicts"
                         "(定点修正,没有隐式批量)")
    execute = bool(params.get("execute"))

    with db.pg_conn() as conn:
        if raw == "all_conflicts":
            with conn.cursor() as cur:
                cur.execute(_ALL_CONFLICT_NODES_SQL)
                nodes = [r[0] for r in cur.fetchall()]
        else:
            nodes = [n.strip() for n in raw.split(",") if n.strip()]
        if not nodes:
            return "catmap_fix:没有可修正的 node"

        with conn.cursor() as cur:
            cur.execute(_PICK_BY_NODE_SQL, {"nodes": nodes})
            rows = cur.fetchall()
        if not rows:
            return (f"catmap_fix:圈定 {len(nodes)} 个 node,"
                    f"但在冲突清单里找不到对应行(先跑 catmap_mine)")

        seen: dict = {}
        for node, old_pt, sug_path, new_pt, sup, tot in rows:
            if node in seen or old_pt == new_pt:
                continue
            seen[node] = (sug_path, old_pt, new_pt, sup, tot)

        lines = [f"catmap_fix:圈定 {len(nodes)} node → 待改 {len(seen)} 条"]
        for node, (path, old_pt, new_pt, sup, tot) in list(seen.items())[:30]:
            lines.append(f"  node {node}|{old_pt} → {new_pt}"
                         f"(实证 {sup}/{tot})|{path[:50]}")
        if len(seen) > 30:
            lines.append(f"  …余 {len(seen) - 30} 条")
        if not execute:
            lines.append("(dry-run:未改库;确认无误后**去掉 --dry-run** 重跑)")
            return "\n".join(lines)

        changed = 0
        for node, (path, _old, new_pt, _s, _t) in seen.items():
            with conn.cursor() as cur:
                cur.execute(_DEMOTE_SQL, {"node": node, "new_pt": new_pt})
                cur.execute(_INSERT_SQL, {"path": path, "new_pt": new_pt,
                                          "node": node})
                cur.execute(_MARK_SQL, (path,))
            changed += 1
    lines.append(f"修正完成 {changed} 条 ✓(旧行降级为'低'并留痕,不删除;"
                 f"下一轮 product_audit ②a 级按新映射直出)")
    return "\n".join(lines)
