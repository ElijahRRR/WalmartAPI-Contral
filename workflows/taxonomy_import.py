"""taxonomy_import — 亚马逊官方类目树入库(一次性;可重跑)。

用法:
  python cli.py taxonomy_import -p file=~/Desktop/amazon_taxonomy_official.json
  python cli.py taxonomy_import -p file=... -p apply=1

所有者提供 2026-08-13:抓取的亚马逊官方类目树(meta + tree 嵌套结构,
32,140 节点/26,955 叶/39 个一级)。入库 audit.amazon_taxonomy 后成为
映射维护的**骨架**:
  · catmap_suggest 第 0 层"兄弟继承"——同父节点下已有映射沿树传播,免 LLM;
  · 采集脏路径对官方树校验归一(后续接);
  · node_id 与映射表 browse_node_id 对齐(比字符串等值稳)。

path 口径与 catalog.products.amazon_category 相同(' > ' 连接);同名
路径冲突(树里偶有重复节点)先到先得。幂等 = TRUNCATE 全量重灌
(官方树是整体快照,增量 upsert 会残留已消失节点)+ 空读/骤缩护栏
(risk_sync 黑名单中心同款)。
"""

import json
import logging
from pathlib import Path

from registry import db

DANGEROUS = False

logger = logging.getLogger("workflows.taxonomy_import")


def flatten_tree(tree: list) -> list[tuple]:
    """输入:嵌套 tree 列表 → 输出:(path, name, node_id, depth, parent_path,
    is_leaf) 行列表(先序;同 path 先到先得)。纯函数(便于测试)。"""
    rows: list[tuple] = []
    seen: set = set()

    def walk(node: dict, parent_path: str | None):
        name = (node.get("name") or "").strip()
        if not name:
            return
        path = f"{parent_path} > {name}" if parent_path else name
        children = node.get("children") or []
        if path not in seen:
            seen.add(path)
            rows.append((path, name, node.get("node_id"),
                         int(node.get("depth") or (path.count(" > ") + 1)),
                         parent_path, not children))
        for c in children:
            walk(c, path)

    for top in tree:
        walk(top, None)
    return rows


def run(params: dict) -> str:
    """输入:params(file=树 JSON 路径,apply=1 写库)→ 输出:导入摘要。"""
    raw_path = str(params.get("file", "")).strip()
    if not raw_path:
        raise ValueError("必填参数 file=<amazon_taxonomy_official.json 路径>")
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise ValueError(f"文件不存在:{path}")
    apply = str(params.get("apply", "")).strip() == "1"

    data = json.loads(path.read_text(encoding="utf-8"))
    meta, tree = data.get("meta") or {}, data.get("tree") or []
    rows = flatten_tree(tree)
    leaves = sum(1 for r in rows if r[5])
    lines = [f"taxonomy_import:{path.name} → 节点 {len(rows)}(叶 {leaves},"
             f"一级 {sum(1 for r in rows if r[4] is None)});"
             f"文件 meta 自称 {meta.get('total_nodes')}/{meta.get('total_leaves')}"]
    if not rows:
        return "\n".join(lines + ["没有可导入的节点"])

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit.amazon_taxonomy")
            (old_n,) = cur.fetchone()
        if old_n >= 1000 and len(rows) < old_n * 0.5:
            raise RuntimeError(f"骤缩 {old_n}→{len(rows)}(超 50%),拒绝重灌"
                               f"——先人工核实文件再跑")
        lines.append(f"库内现有 {old_n}(重灌语义:官方树整体快照)")
        if not apply:
            lines.append("(预览:未写库;确认无误加 -p apply=1)")
            return "\n".join(lines)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE audit.amazon_taxonomy")
            cur.executemany(
                "INSERT INTO audit.amazon_taxonomy "
                "(path, name, node_id, depth, parent_path, is_leaf) "
                "VALUES (%s, %s, %s, %s, %s, %s)", rows)
    lines.append(f"全量重灌 {len(rows)} 条 ✓")
    return "\n".join(lines)
