"""taxonomy_import — 亚马逊类目树入库(ID 主键;可重跑)。

用法:
  python cli.py taxonomy_import -p file=~/Downloads/amazon_taxonomy_reconciled.json
  python cli.py taxonomy_import -p file=... -p apply=1

所有者提供 2026-08-14 的**对账版类目树**(meta / verified_added_paths /
unverified_new_nodes / leaves 四段;每行带 `browse_node_id`、`父节点ID`、
`完整路径`、`深度`、`是否叶子`、L1~L8 分层)。与 2026-08-13 撤掉的那棵
Best Sellers 名称树的**本质区别:这棵以 browse_node_id 为主键**,能与
产品侧 `products.browse_node_id`、映射表 `walmart_category_map
.browse_node_id` 三方 JOIN——名称漂移在此不成问题。

用途(按价值排序):
  1. **缺口工作面**:类目树 3.2 万 node vs 映射表 1.57 万 node,差集就是
     "亚马逊有、我们没映射"的真实清单;交叉产品数排序即优先级;
  2. **规范名**:node_id → 官方类目名/完整路径,人工复核与 LLM 提示词用
     它,不用产品侧漂移的面包屑;
  3. **祖先回退**:叶子 node 没映射时可沿 `父节点ID` 上溯找已映射祖先
     (产品侧 `browse_node_chain` 也存了同一条链,两边可互验)。

⚠ 上次教训(2026-08-13):接外部数据源前**先验 JOIN 能不能对上**。本
工作流的预览模式强制先报三方交叉命中率,对不上就别 apply。

⚠ 文件只发了**叶子层**(2026-08-14 实测:入库 28,495 vs meta 自报 32,147),
中间层非叶节点不在文件里 ⇒ 祖先回退用 `taxonomy_derive` 从我们自有的
(ID 链 × 面包屑)反推补齐。预览的「文件构造体检」会把未解析的段也报出来,
免得下次再分不清"文件没给"还是"解析器没读"。

幂等 = 全量重灌(树是整体快照)+ 空读/骤缩护栏。**重灌只删文件段的行**,
`source='derived_products'` 的反推补层留着(同 node 由文件行覆盖)。
"""

import json
import logging
from pathlib import Path

from registry import db
from registry.resources import TAXONOMY_SOURCE_DERIVED

DANGEROUS = False

logger = logging.getLogger("workflows.taxonomy_import")

# 树行的中文键(所有者对账版原样;缺任一必填键的行跳过并计数)
_K_NODE, _K_NAME, _K_PATH = "browse_node_id", "类目名", "完整路径"
_K_PARENT, _K_DEPTH, _K_LEAF = "父节点ID", "深度", "是否叶子"
_K_ROOT, _K_SAMPLES = "L1 根类目", "产品样本数"

_SECTIONS = ("leaves", "verified_added_paths", "unverified_new_nodes")
_IGNORED_KEYS = ("meta",)      # 已知的非数据段(不报警)


def survey_file(data: dict) -> list[str]:
    """输入:整份 JSON → 输出:文件构造体检行(段名 × 行数,含未解析段)。

    **不解析的段必须报出来**:2026-08-14 所有者问"中间层是文件缺还是没读到",
    当时答不上——因为解析器只认三个段名,别的段静默丢弃。现在预览先把
    文件的顶层结构原样摊开,再对上 meta 里的自报行数,差多少一眼可见。
    """
    out, parsed = [], 0
    for key, val in data.items():
        if key in _IGNORED_KEYS:
            continue
        n = len(val) if isinstance(val, (list, dict)) else 1
        if key in _SECTIONS:
            parsed += n if isinstance(val, list) else 0
            out.append(f"  {key}: {n} 行(解析)")
        else:
            out.append(f"  {key}: {n} 行 ⚠ **未解析**(解析器只认 "
                       f"{'/'.join(_SECTIONS)};若这里有节点需扩段)")
    meta = data.get("meta") or {}
    for k, v in meta.items():
        if isinstance(v, int) and v and v != parsed:
            out.append(f"  meta.{k} = {v}(文件自报;实际可解析 {parsed},"
                       f"差 {v - parsed})")
    return out


def _int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def parse_rows(data: dict) -> tuple[list[tuple], dict]:
    """输入:对账版 JSON → 输出:(入库行, 计数)。纯函数(便于测试)。

    三段全收(leaves / verified_added_paths / unverified_new_nodes),
    段名写进 source 列以便区分"已验证"与"未验证新 node";同 node_id
    先到先得(leaves 段在前,权威)。父节点 ID 形如 'L1_amazon-devices'
    的是根级占位,存 NULL。
    """
    rows: list[tuple] = []
    seen: set = set()
    stat = {s: 0 for s in _SECTIONS}
    stat["skipped"] = 0
    for sec in _SECTIONS:
        for r in data.get(sec) or []:
            node = str(r.get(_K_NODE) or "").strip()
            name = str(r.get(_K_NAME) or "").strip()
            if not node or not name or node in seen:
                if not node or not name:
                    stat["skipped"] += 1
                continue
            seen.add(node)
            parent = str(r.get(_K_PARENT) or "").strip()
            if not parent.isdigit():      # 'L1_xxx' 根级占位 → NULL
                parent = None
            rows.append((
                node, name, str(r.get(_K_PATH) or "").strip() or None,
                _int(r.get(_K_DEPTH)), parent,
                str(r.get(_K_LEAF) or "").strip() == "是",
                str(r.get(_K_ROOT) or "").strip() or None,
                _int(r.get(_K_SAMPLES)), sec))
            stat[sec] += 1
    return rows, stat


# 文件行是权威:同 node 若已有反推补层(taxonomy_derive),文件覆盖它
_INSERT_SQL = """
INSERT INTO audit.amazon_taxonomy
    (node_id, name, path, depth, parent_node_id, is_leaf, root_name,
     product_samples, source)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (node_id) DO UPDATE
SET name = EXCLUDED.name, path = EXCLUDED.path, depth = EXCLUDED.depth,
    parent_node_id = EXCLUDED.parent_node_id, is_leaf = EXCLUDED.is_leaf,
    root_name = EXCLUDED.root_name,
    product_samples = EXCLUDED.product_samples, source = EXCLUDED.source,
    imported_at = now()
"""

# 三方交叉体检(上次教训:先验 JOIN 再落库)
_CROSS_SQL = """
SELECT
  (SELECT count(DISTINCT browse_node_id) FROM catalog.products
    WHERE marketplace = 'US' AND browse_node_id = ANY(%(nodes)s))       AS prod_hit,
  (SELECT count(DISTINCT browse_node_id) FROM catalog.products
    WHERE marketplace = 'US' AND browse_node_id IS NOT NULL)            AS prod_total,
  (SELECT count(DISTINCT browse_node_id) FROM audit.walmart_category_map
    WHERE browse_node_id = ANY(%(nodes)s))                              AS map_hit,
  (SELECT count(DISTINCT browse_node_id) FROM audit.walmart_category_map
    WHERE browse_node_id IS NOT NULL)                                   AS map_total
"""


def run(params: dict) -> str:
    """输入:params(file=树 JSON,apply=1 写库)→ 输出:交叉体检 + 导入摘要。"""
    raw_path = str(params.get("file", "")).strip()
    if not raw_path:
        raise ValueError("必填参数 file=<对账版类目树 JSON 路径>")
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise ValueError(f"文件不存在:{path}")
    apply = str(params.get("apply", "")).strip() == "1"

    data = json.loads(path.read_text(encoding="utf-8"))
    rows, stat = parse_rows(data)
    if not rows:
        return f"taxonomy_import:{path.name} 没有可导入的 node"

    nodes = [r[0] for r in rows]
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_CROSS_SQL, {"nodes": nodes})
            prod_hit, prod_total, map_hit, map_total = cur.fetchone()
            cur.execute("SELECT count(*) FROM audit.amazon_taxonomy "
                        "WHERE source IS NULL OR source = ANY(%s)",
                        (list(_SECTIONS),))
            (old_n,) = cur.fetchone()
            cur.execute("SELECT count(*) FROM audit.amazon_taxonomy "
                        "WHERE source = %s", (TAXONOMY_SOURCE_DERIVED,))
            (derived_n,) = cur.fetchone()

        lines = [
            f"taxonomy_import:{path.name} → node {len(rows)}"
            f"(叶 {sum(1 for r in rows if r[5])};"
            + " / ".join(f"{s} {stat[s]}" for s in _SECTIONS)
            + (f";跳过 {stat['skipped']}" if stat["skipped"] else "") + ")",
            "⚑ 文件构造体检(段名 × 行数;未解析段会报警):",
            *survey_file(data),
            f"⚑ 三方交叉体检(先验 JOIN 再落库):",
            f"  产品侧 node {prod_total} → 树里有 {prod_hit}"
            + (f"({prod_hit / prod_total * 100:.1f}%)" if prod_total else ""),
            f"  映射表 node {map_total} → 树里有 {map_hit}"
            + (f"({map_hit / map_total * 100:.1f}%)" if map_total else ""),
            f"  树有而映射表没有的 node ≈ {len(rows) - map_hit}"
            f"(其中与产品有交集的才是真工作面,见 catmap_gap)",
        ]
        if prod_total and prod_hit / prod_total < 0.5:
            lines.append("⚠ 产品侧命中率 <50%:树与生产数据对不上,"
                         "**先别 apply**,核实文件是否完整/同源")
        lines.append(f"库内现有文件段 {old_n}(重灌语义:树是整体快照)"
                     + (f",另有反推补层 {derived_n} 条(不冲掉)"
                        if derived_n else ""))
        if not apply:
            lines.append("(预览:未写库;确认无误加 -p apply=1)")
            return "\n".join(lines)
        if old_n >= 1000 and len(rows) < old_n * 0.5:
            raise RuntimeError(f"骤缩 {old_n}→{len(rows)}(超 50%),拒绝重灌")
        with conn.cursor() as cur:
            # 只删文件段的行:taxonomy_derive 补的中间层是我们自有数据反推的,
            # 换一份文件不该把它清空(文件行仍压过反推行——见 ON CONFLICT)
            cur.execute("DELETE FROM audit.amazon_taxonomy "
                        "WHERE source IS NULL OR source = ANY(%s)",
                        (list(_SECTIONS),))
            cur.executemany(_INSERT_SQL, rows)
    lines.append(f"全量重灌 {len(rows)} 条 ✓"
                 + (f"(反推补层 {derived_n} 条保留;同 node 已被文件行覆盖)"
                    if derived_n else ""))
    return "\n".join(lines)
