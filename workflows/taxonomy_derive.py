"""taxonomy_derive — 从产品自有数据反推**中间层类目节点**(零采集;可重跑)。

用法:
  python cli.py taxonomy_derive                 # 预览:对齐率 + 与已知树对拍
  python cli.py taxonomy_derive -p apply=1      # 补进 audit.amazon_taxonomy
  python cli.py taxonomy_derive -p min_agree=0.95   # 收紧对拍闸(默认 0.9)

**为什么需要**:所有者的对账版类目树只发了叶子层(+新增 node),中间层
非叶节点没随文件下发 ⇒ 库里走不了父链,"叶子没映射就退一级找祖先映射"
这种兜底做不了。

**为什么不用重抓**:中间层的 ID 和名字我们**手里已经有**,只是分在两列:
  `products.browse_node_chain` = 根→叶的 **ID 链**(node_backfill 从快照回填)
  `products.amazon_category`   = 同一条路径的**名称串**(面包屑)
两者按位置对齐(**右对齐**,叶子必然是两边最后一段)即可还原每一层的
node_id / 名称 / 父节点 / 完整路径。覆盖面 = 我们有货的类目——正是唯一
需要走父链的那部分。

**先验后落**(2026-08-13 教训:接数据前先验能不能对上):预览强制报
  ① 长度差分布(ID 链与面包屑段数不等 = 面包屑掉段或多了促销层);
  ② **与已知树对拍**:反推名 vs 树里官方名的一致率,叶位/中间位**分开报**
     ——中间位一致率才是这条路能不能信的判据;
一致率低于 min_agree 直接**拒绝 apply**,不给"先写进去再说"的口子。

写入口径:`source='derived_products'`,**只补树里没有的 node**
(ON CONFLICT DO NOTHING —— 文件行永远压过反推行);taxonomy_import
重灌时只删文件段的行,反推行留着(见该文件 _DELETE_FILE_SQL)。
"""

import logging
from collections import Counter

from registry import db
from registry.resources import TAXONOMY_SOURCE_DERIVED as DERIVED_SOURCE
from services.catpath import norm_seg, segments

DANGEROUS = False

logger = logging.getLogger("workflows.taxonomy_derive")

MIN_AGREE = 0.9      # 中间位名称与已知树的一致率下限

# (ID 链, 面包屑) 组合 × 产品数:同组合只算一次,票按产品数加权
_PAIRS_SQL = """
SELECT btrim(browse_node_chain), btrim(amazon_category), count(*)
FROM catalog.products
WHERE marketplace = 'US'
  AND browse_node_chain IS NOT NULL AND btrim(browse_node_chain) <> ''
  AND amazon_category IS NOT NULL AND btrim(amazon_category) <> ''
GROUP BY 1, 2
"""

_TREE_SQL = "SELECT node_id, name, is_leaf FROM audit.amazon_taxonomy"

_INSERT_SQL = """
INSERT INTO audit.amazon_taxonomy
    (node_id, name, path, depth, parent_node_id, is_leaf, root_name,
     product_samples, source)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '""" + DERIVED_SOURCE + """')
ON CONFLICT (node_id) DO NOTHING
"""


def derive_nodes(pairs) -> tuple[dict, dict]:
    """输入:[(ID 链, 面包屑, 产品数)] → 输出:({node: 反推事实}, 计数)。

    纯函数。**右对齐**取两边共有的后 k 段(叶子必然对齐);长度不等时
    多出来的头部段不采信(可能是面包屑带了促销层,也可能是链带了根 node),
    但链上多出来的那一段仍可当最顶层的父 ID 用。
    """
    acc: dict = {}
    stat = Counter()
    for chain, path, n in pairs:
        ids = [s.strip() for s in (chain or "").split(",") if s.strip()]
        segs = segments(path)
        if not ids or not segs:
            stat["空"] += n
            continue
        delta = len(ids) - len(segs)
        stat[f"链-段={delta:+d}"] += n
        k = min(len(ids), len(segs))
        off_id, off_seg = len(ids) - k, len(segs) - k
        for j in range(k):
            node, name = ids[off_id + j], segs[off_seg + j]
            if j > 0:
                parent = ids[off_id + j - 1]
            else:
                parent = ids[off_id - 1] if off_id > 0 else None
            e = acc.setdefault(node, {"name": Counter(), "parent": Counter(),
                                      "path": Counter(), "root": Counter(),
                                      "depth": Counter(), "leaf": 0,
                                      "mid": 0, "prod": 0})
            e["name"][name] += n
            e["parent"][parent or ""] += n
            e["path"][" > ".join(segs[: off_seg + j + 1])] += n
            e["root"][segs[0]] += n
            e["depth"][off_id + j + 1] += n
            e["prod"] += n
            if j == k - 1:
                e["leaf"] += n
            else:
                e["mid"] += n
    return acc, dict(stat)


def resolve(node: str, e: dict) -> tuple:
    """输入:node 与其投票 → 输出:入库行(名/路径/深度/父/是否叶/根/产品数)。

    每一维独立取多数票:同一 node 在不同产品的面包屑里可能名字漂移
    (Home Décor vs Home Décor Products),多数票即规范名。
    """
    top = lambda c: c.most_common(1)[0][0]           # noqa: E731
    parent = top(e["parent"]) or None
    return (node, top(e["name"]), top(e["path"]), top(e["depth"]), parent,
            e["leaf"] >= e["mid"], top(e["root"]), e["prod"])


def run(params: dict) -> str:
    """输入:params(apply=1 写库 / min_agree)→ 输出:对拍体检 + 补入摘要。"""
    apply = str(params.get("apply", "")).strip() == "1"
    min_agree = float(params.get("min_agree", MIN_AGREE))

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_PAIRS_SQL)
            acc, stat = derive_nodes(cur.fetchall())
            cur.execute(_TREE_SQL)
            tree = {n: (nm, leaf) for n, nm, leaf in cur.fetchall()}

    if not acc:
        return ("taxonomy_derive:products 里没有 (ID 链 × 面包屑) 成对数据"
                "——先跑 node_backfill")

    rows = [resolve(n, e) for n, e in acc.items()]
    # 与已知树对拍:叶位/中间位分开——中间位才是这条路的判据
    agree = {"叶位": [0, 0], "中间位": [0, 0]}
    bad_samples = []
    for node, name, _p, _d, _pa, is_leaf, _r, _n in rows:
        known = tree.get(node)
        if not known:
            continue
        bucket = "叶位" if known[1] else "中间位"
        agree[bucket][1] += 1
        if norm_seg(name) == norm_seg(known[0]):
            agree[bucket][0] += 1
        elif bucket == "中间位" and len(bad_samples) < 5:
            bad_samples.append(f"  node {node}|反推 {name}|树 {known[0]}")
    mid_ok, mid_n = agree["中间位"]
    mid_rate = mid_ok / mid_n if mid_n else 0.0

    new_rows = [r for r in rows if r[0] not in tree]
    new_rows.sort(key=lambda r: r[7], reverse=True)
    new_mid = sum(1 for r in new_rows if not r[5])

    lines = [
        f"taxonomy_derive:反推 node {len(rows)}(树里已有 {len(rows) - len(new_rows)}"
        f" / 可补 {len(new_rows)},其中中间层 {new_mid})",
        "① 链×面包屑长度差(按产品数;0 = 严丝合缝):"
        + " / ".join(f"{k} {v}" for k, v in sorted(stat.items())),
        f"② 与已知树对拍(名称归一后相等):"
        f"叶位 {agree['叶位'][0]}/{agree['叶位'][1]}"
        + (f"({agree['叶位'][0] / agree['叶位'][1]:.1%})" if agree['叶位'][1] else "")
        + f",中间位 {mid_ok}/{mid_n}"
        + (f"({mid_rate:.1%})" if mid_n else "(无样本)"),
    ]
    if bad_samples:
        lines.append("中间位不一致样例(名称漂移 or 对齐串位):")
        lines += bad_samples
    for node, name, path, _d, _pa, _leaf, _r, n in new_rows[:15]:
        lines.append(f"  {n:>7} 件|node {node}|{(path or name)[:70]}")
    if len(new_rows) > 15:
        lines.append(f"  …余 {len(new_rows) - 15} 个")

    if not apply:
        lines.append("(预览:未写库;确认无误加 -p apply=1)")
        return "\n".join(lines)
    if mid_n and mid_rate < min_agree:
        raise RuntimeError(
            f"中间位对拍一致率 {mid_rate:.1%} < {min_agree:.0%},拒绝写入"
            f"——ID 链与面包屑没对齐好,写进去会污染类目树"
            f"(要放宽先看不一致样例,确认是名称漂移而非串位)")
    if not new_rows:
        return "\n".join(lines + ["无新增 node,库内已是最新"])
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(_INSERT_SQL, new_rows)
    lines.append(f"补入完成 {len(new_rows)} 个 node ✓"
                 f"(source={DERIVED_SOURCE};文件行永远压过反推行,"
                 f"taxonomy_import 重灌不会冲掉它们)")
    return "\n".join(lines)
