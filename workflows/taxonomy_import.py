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

⚠ 2026-08-14 更正:文件**本来就带全量树**(`nodes` 段 32,147 行),是解析器
只认三个写死的段名把它漏了,当时还以为"文件只发了叶子"。现在改为**按内容
认段**:顶层任何 list、行里带 `browse_node_id` 就收,段名陌生照样解析并在
预览里标出来。教训入库:写死段名 = 上游多给的东西静默丢掉。

⚠ browse tree 是 **DAG 不是树**(所有者定稿 2026-08-14):同一个 node 可以
挂在多个父下、有多条完整路径。所以落两张表——
  `audit.amazon_taxonomy`   节点级属性,按 node_id 一行(路径列=代表路径);
  `audit.amazon_node_paths` 路径关系,键是 (node, parent, 完整路径) 三元组,
                            **一条都不去重掉**,否则父链回退会退到错的祖先。

幂等 = 全量重灌(树是整体快照)+ 空读/骤缩护栏。**重灌只删文件来源的行**,
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

# 节点级属性的取值优先段(先到先得);**不在这个名单里的段照收不误**,
# 只是排在后面——2026-08-14 教训:写死段名 = 文件多给的东西静默丢掉
# (`nodes` 段 32,147 行的全量树就是这么被漏掉的,当时还以为是文件缺)。
_SECTIONS = ("leaves", "verified_added_paths", "unverified_new_nodes")
_IGNORED_KEYS = ("meta",)      # 已知的非数据段


def data_sections(data: dict) -> list[str]:
    """输入:整份 JSON → 输出:按优先序排列的**数据段名**(带 node 的 list)。

    判据是内容不是段名:顶层任何 list、其首个 dict 行带 `browse_node_id`,
    就是数据段。已知段排前(节点级属性先到先得),陌生段按出现顺序排后。
    """
    known = [s for s in _SECTIONS if isinstance(data.get(s), list)]
    extra = []
    for key, val in data.items():
        if key in _IGNORED_KEYS or key in known or not isinstance(val, list):
            continue
        head = next((r for r in val if isinstance(r, dict)), None)
        if head and str(head.get(_K_NODE) or "").strip():
            extra.append(key)
    return known + extra


def survey_file(data: dict) -> list[str]:
    """输入:整份 JSON → 输出:文件构造体检行(段名 × 行数 × 是否解析)。

    **不解析的段必须报出来**:2026-08-14 所有者问"中间层是文件缺还是没读到",
    当时答不上——因为解析器只认三个段名,别的段静默丢弃。现在预览先把
    文件的顶层结构原样摊开,再对上 meta 里的自报行数,差多少一眼可见。
    """
    sections = data_sections(data)
    out, parsed = [], 0
    for key, val in data.items():
        if key in _IGNORED_KEYS:
            continue
        n = len(val) if isinstance(val, (list, dict)) else 1
        if key in sections:
            parsed += n
            tag = "解析" if key in _SECTIONS else "解析·段名陌生但带 node"
            out.append(f"  {key}: {n} 行({tag})")
        else:
            out.append(f"  {key}: {n} 行 ⚠ **未解析**"
                       f"(不是带 browse_node_id 的行清单)")
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


def parse_rows(data: dict) -> tuple[list[tuple], list[tuple], dict]:
    """输入:对账版 JSON → 输出:(节点行, 路径行, 计数)。纯函数(便于测试)。

    **两种行分开产出**(所有者定稿 2026-08-14:browse tree 是 DAG):
      节点行 = 节点级属性(名/是否叶子),按 node_id 去重,先到先得
               (leaves 段在前,权威);路径列存的是**代表路径**;
      路径行 = (node, parent, 完整路径) 三元组,**一个都不去重掉**——
               同一 node 挂在多个父下就是多行,按 ID 去重会丢掉多路径关系,
               父链回退就会退到错误的祖先。

    父节点 ID 形如 'L1_amazon-devices' 的是根级占位:节点行存 NULL,
    路径行存 ''(PK 不收 NULL)。
    """
    node_rows: list[tuple] = []
    path_rows: list[tuple] = []
    seen: set = set()
    seen_path: set = set()
    sections = data_sections(data)
    stat = {s: 0 for s in sections}
    stat["skipped"] = 0
    stat["paths"] = 0
    for sec in sections:
        for r in data.get(sec) or []:
            if not isinstance(r, dict):
                stat["skipped"] += 1
                continue
            node = str(r.get(_K_NODE) or "").strip()
            name = str(r.get(_K_NAME) or "").strip()
            if not node or not name:
                stat["skipped"] += 1
                continue
            parent = str(r.get(_K_PARENT) or "").strip()
            if not parent.isdigit():      # 'L1_xxx' 根级占位
                parent = ""
            path = str(r.get(_K_PATH) or "").strip()
            depth = _int(r.get(_K_DEPTH))
            root = str(r.get(_K_ROOT) or "").strip() or None
            if path:                       # 无路径的行不进路径表(没关系可存)
                key = (node, parent, path)
                if key not in seen_path:
                    seen_path.add(key)
                    path_rows.append((node, parent, path, depth, root, sec))
                    stat["paths"] += 1
            if node in seen:
                continue
            seen.add(node)
            node_rows.append((
                node, name, path or None, depth, parent or None,
                str(r.get(_K_LEAF) or "").strip() == "是", root,
                _int(r.get(_K_SAMPLES)), sec))
            stat[sec] += 1
    return node_rows, path_rows, stat


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

# 路径关系:三元组主键,同一 node 多个挂载点各存一行(DAG 不是树)
_INSERT_PATH_SQL = """
INSERT INTO audit.amazon_node_paths
    (node_id, parent_node_id, full_path, depth, root_name, source)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (node_id, parent_node_id, full_path) DO UPDATE
SET depth = EXCLUDED.depth, root_name = EXCLUDED.root_name,
    source = EXCLUDED.source, imported_at = now()
"""

# 重灌只清文件来源的行(段名可能随文件变,所以按"不是反推层"取反)
_DELETE_FILE_SQL = "DELETE FROM {t} WHERE source IS DISTINCT FROM %s"

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
    rows, path_rows, stat = parse_rows(data)
    if not rows:
        return f"taxonomy_import:{path.name} 没有可导入的 node"
    sections = data_sections(data)

    nodes = [r[0] for r in rows]
    multi = len(path_rows) - len({p[0] for p in path_rows})   # 多路径 node 的额外行
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_CROSS_SQL, {"nodes": nodes})
            prod_hit, prod_total, map_hit, map_total = cur.fetchone()
            cur.execute("SELECT count(*) FROM audit.amazon_taxonomy "
                        "WHERE source IS DISTINCT FROM %s",
                        (TAXONOMY_SOURCE_DERIVED,))
            (old_n,) = cur.fetchone()
            cur.execute("SELECT count(*) FROM audit.amazon_taxonomy "
                        "WHERE source = %s", (TAXONOMY_SOURCE_DERIVED,))
            (derived_n,) = cur.fetchone()

        lines = [
            f"taxonomy_import:{path.name} → node {len(rows)}"
            f"(叶 {sum(1 for r in rows if r[5])};"
            + " / ".join(f"{s} {stat[s]}" for s in sections)
            + (f";跳过 {stat['skipped']}" if stat["skipped"] else "") + ")",
            f"路径关系 {stat['paths']} 条(三元组 node+parent+完整路径;"
            f"其中 {multi} 条是多路径 node 的额外挂载点——按 ID 去重就会丢掉"
            f"它们,父链回退会退错祖先)",
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
            # 只删文件来源的行:taxonomy_derive 补的是我们自有数据反推的,
            # 换一份文件不该把它清空(文件行仍压过反推行——见 ON CONFLICT)
            cur.execute(_DELETE_FILE_SQL.format(t="audit.amazon_taxonomy"),
                        (TAXONOMY_SOURCE_DERIVED,))
            cur.executemany(_INSERT_SQL, rows)
            cur.execute(_DELETE_FILE_SQL.format(t="audit.amazon_node_paths"),
                        (TAXONOMY_SOURCE_DERIVED,))
            cur.executemany(_INSERT_PATH_SQL, path_rows)
    lines.append(f"全量重灌 node {len(rows)} + 路径 {len(path_rows)} ✓"
                 + (f"(反推补层 {derived_n} 条保留;同 node 已被文件行覆盖)"
                    if derived_n else ""))
    return "\n".join(lines)
