"""catmap_mine — 从产品实证数据挖类目映射(纯 SQL 零 LLM;可反复跑)。

用法:
  python cli.py catmap_mine                    # 按类目 ID 挖掘(默认,推荐)
  python cli.py catmap_mine -p key=path        # 按类目路径挖掘(无 ID 老行)
  python cli.py catmap_mine -p min_support=8   # 收紧票数门槛(默认 5)
  python cli.py catmap_mine -p min_dominance=0.9  # 收紧优势度(默认 0.8)
  python cli.py catmap_mine -p promote=1       # 把 mined_trusted 升级进映射表

**键 = browse_node_id(所有者定稿 2026-08-14)**:类目名会漂 ID 不会,按
ID 投票天然绕开三套名称不一致;挖出来的映射自带 ID,补进映射表正好填上
ID 缺口(实测产品侧 15,538 个 node 只有 61.5% 在映射表里)。`key=path`
是无 ID 老行的兜底口径(与 2026-08-13 首版一致)。

所有者定稿 2026-08-13 的三段式映射维护顺序,本工作流是第一段:
  ① **数据挖掘(本工作流)**:products 里既有亚马逊类目路径、又有实证
    walmart_pt(pt_backfill 回填 9.5 万 + 审核结论)的产品,按路径分组投票
    ——多个产品指向同一 PT = 可信映射;
  ② 少量支持(2~4 个产品)或有分流的 → 人工核对清单;
  ③ 零数据路径 → catmap_suggest(LLM)/ 人工,从无到有。

分桶(classify_path 纯函数):
  mined_trusted  不在映射表 + 单一 PT + 支持数 ≥ min_support(默认 5)
  mined_review   不在映射表 + 单一 PT + 支持数 2~(min_support-1)
  mined_mixed    不在映射表 + 多 PT 分流(distribution 附全分布供人工)
  map_conflict   已在映射表但高置信映射 PT ≠ 数据共识 PT(只报不改——
                 这是对旧映射表的实证体检,冲突行最值得人看)
  支持数 1 的路径不入桶(单证不立,留给 ③ 段)

产品侧 PT 先过 pt_meta 闸(废 PT 不参与投票);写入
audit.category_map_suggestions(与 catmap_suggest 同一张复核面)。
promote=1:仅 mined_trusted 且仍不在映射表的行,以 confidence='高'、
match_type='mined_products' 插入 walmart_category_map(ON CONFLICT 跳过)
——②级直出立即对同路径全部产品生效。⚠ 映射表编辑权威(飞书 vs PG)
所有者尚未定稿;若定飞书,已升级行需同步补录进「映射明细」表。
"""

import json
import logging

from registry import db

DANGEROUS = False

logger = logging.getLogger("workflows.catmap_mine")

# 键 × PT 投票(pt_meta 闸内联:废 PT 不参与;'unknown' 防御性剔除)。
# {key} = p.browse_node_id(默认)或 btrim(p.amazon_category)
_MINE_SQL = """
SELECT {key} AS k, p.walmart_pt, count(*) AS n
FROM catalog.products p
JOIN audit.walmart_pt_meta m ON m.walmart_product_type = p.walmart_pt
WHERE p.marketplace = 'US'
  AND {key} IS NOT NULL AND btrim({key}) <> ''
  AND p.walmart_pt IS NOT NULL AND p.walmart_pt <> 'unknown'
GROUP BY 1, 2
"""

# 每个 node 的代表路径(产品数最多的那条;升级进映射表时当 amazon_category
# 主键用——映射表 PK 是 (amazon_category, walmart_product_type))
_REP_PATH_SQL = """
SELECT DISTINCT ON (browse_node_id) browse_node_id, btrim(amazon_category)
FROM (
    SELECT browse_node_id, amazon_category, count(*) AS n
    FROM catalog.products
    WHERE marketplace = 'US' AND browse_node_id IS NOT NULL
      AND amazon_category IS NOT NULL AND btrim(amazon_category) <> ''
    GROUP BY 1, 2
) t ORDER BY browse_node_id, n DESC
"""

_IN_MAP_NODE_SQL = """
SELECT browse_node_id,
       CASE WHEN count(DISTINCT walmart_product_type) = 1
            THEN min(walmart_product_type) END
FROM audit.walmart_category_map
WHERE confidence = '高' AND browse_node_id IS NOT NULL
  AND btrim(browse_node_id) <> ''
GROUP BY 1
"""

_IN_MAP_PATH_SQL = """
SELECT DISTINCT ON (btrim(amazon_category))
       btrim(amazon_category), walmart_product_type
FROM audit.walmart_category_map
WHERE confidence = '高'
ORDER BY btrim(amazon_category)
"""

_UPSERT_SQL = """
INSERT INTO audit.category_map_suggestions
    (amazon_category, suggested_pt, confidence, status,
     product_count, support_count, pt_distribution, browse_node_id)
VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
ON CONFLICT (amazon_category) DO UPDATE
SET suggested_pt = EXCLUDED.suggested_pt,
    confidence   = EXCLUDED.confidence,
    status       = EXCLUDED.status,
    product_count = EXCLUDED.product_count,
    support_count = EXCLUDED.support_count,
    pt_distribution = EXCLUDED.pt_distribution,
    browse_node_id = COALESCE(EXCLUDED.browse_node_id,
                              audit.category_map_suggestions.browse_node_id),
    created_at   = now()
"""

# 升级:node 键挖出来的行**带 browse_node_id 一起写**——正是映射表 ID
# 缺口的填补物;amazon_category 用代表路径(PK 需要)。ON CONFLICT 跳过
_PROMOTE_NODE_SQL = """
INSERT INTO audit.walmart_category_map
    (amazon_category, walmart_product_type, browse_node_id, confidence,
     match_type)
VALUES (%s, %s, %s, '高', 'mined_products')
ON CONFLICT (amazon_category, walmart_product_type) DO NOTHING
"""


MIN_DOMINANCE = 0.8   # 首选 PT 的占比门槛(首跑实测:要求 100% 一致 → 1321 分流)


def classify_path(dist: dict, in_map_pt: str | None,
                  min_support: int = 5,
                  min_dominance: float = MIN_DOMINANCE
                  ) -> tuple[str, str, int] | None:
    """输入:{pt: 支持数} + 该键映射表现值 → 输出:(status, pt, support) 或 None。

    纯函数。**判据是优势度不是全票**(首跑实测修正 2026-08-14):回填的 PT
    来自删除历史+报错日报,同一类目下历史被挂过几个不同 PT 很正常(不同
    店铺挂法/早期挂错),要求 100% 一致会把 1,321 个类目全打成"分流";
    真实信号是"压倒性多数指向同一 PT",少数派是噪声。
      dominance = 首选 PT 支持数 / 该键总票数
      dominance ≥ min_dominance 且 支持数 ≥ min_support → mined_trusted
      dominance 达标但支持 2~(min_support-1)            → mined_review
      dominance 不达标(真分流)                          → mined_mixed(人工)
    支持数 1 → None(单证不立);已在映射表且共识与之一致 → None(无事可报)。
    """
    if not dist:
        return None
    total = sum(dist.values())
    top_pt, top_n = max(dist.items(), key=lambda kv: kv[1])
    dominance = top_n / total if total else 0.0
    if in_map_pt is not None:
        if (top_pt != in_map_pt and top_n >= min_support
                and dominance >= min_dominance):
            return ("map_conflict", top_pt, top_n)   # 实证压倒性却与旧映射相左
        return None
    if dominance < min_dominance:
        return ("mined_mixed", top_pt, top_n)
    if top_n >= min_support:
        return ("mined_trusted", top_pt, top_n)
    if top_n >= 2:
        return ("mined_review", top_pt, top_n)
    return None


def run(params: dict) -> str:
    """输入:params(key=node|path / min_support / promote=1)→ 输出:分桶摘要。"""
    min_support = int(params.get("min_support", 5))
    min_dominance = float(params.get("min_dominance", MIN_DOMINANCE))
    promote = str(params.get("promote", "")).strip() == "1"
    by_node = str(params.get("key", "node")).strip().lower() != "path"
    key_sql = "p.browse_node_id" if by_node else "btrim(p.amazon_category)"

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_MINE_SQL.format(key=key_sql))
            votes: dict = {}
            for k, pt, n in cur.fetchall():
                votes.setdefault(k, {})[pt] = n
            rep_path: dict = {}
            if by_node:
                cur.execute(_IN_MAP_NODE_SQL)
                in_map = {k: v for k, v in cur.fetchall() if k}
                cur.execute(_REP_PATH_SQL)
                rep_path = dict(cur.fetchall())
            else:
                cur.execute(_IN_MAP_PATH_SQL)
                in_map = dict(cur.fetchall())
                # 别名路径视同已在映射表(catmap_align:中间层名漂移的假缺口),
                # 否则会被当新映射挖出来、还可能与 canonical 行冲突刷屏
                cur.execute("SELECT path, canonical_path "
                            "FROM audit.category_path_alias")
                for alias_path, canonical in cur.fetchall():
                    if canonical in in_map:
                        in_map.setdefault(alias_path, in_map[canonical])

        counts = {"mined_trusted": 0, "mined_review": 0,
                  "mined_mixed": 0, "map_conflict": 0}
        # 分流桶按优势度分档:让所有者拿数据挑阈值,而不是拍脑袋
        dom_bands = {"0.7~0.8": 0, "0.6~0.7": 0, "0.5~0.6": 0, "<0.5": 0}
        samples: dict = {"mined_trusted": [], "map_conflict": [],
                         "mined_mixed": []}
        rows, promote_rows = [], []
        for k, dist in votes.items():
            got = classify_path(dist, in_map.get(k), min_support,
                                min_dominance)
            if got is None:
                continue
            status, pt, support = got
            counts[status] += 1
            total_votes = sum(dist.values())
            dom = support / total_votes if total_votes else 0
            if status == "mined_mixed":
                band = ("0.7~0.8" if dom >= 0.7 else "0.6~0.7" if dom >= 0.6
                        else "0.5~0.6" if dom >= 0.5 else "<0.5")
                dom_bands[band] += 1
            if status in samples and len(samples[status]) < 5:
                extra = (f" ←旧映射 {in_map.get(k)}"
                         if status == "map_conflict" else "")
                samples[status].append(
                    f"  node {k}|{pt}(票 {support}/{total_votes},"
                    f"优势 {dom:.0%}){extra}")
            # 建议表主键是 amazon_category:node 键模式下用代表路径当展示键,
            # 没有代表路径的 node 用 'node:<id>' 兜底(不会与真路径撞)
            label = (rep_path.get(k) or f"node:{k}") if by_node else k
            rows.append((label, pt, "高" if status == "mined_trusted" else None,
                         status, sum(dist.values()), support,
                         json.dumps(dist, ensure_ascii=False),
                         k if by_node else None))
            if by_node and status == "mined_trusted" and rep_path.get(k):
                promote_rows.append((rep_path[k], pt, k))
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, rows)

        kind = "类目 ID" if by_node else "类目路径"
        lines = [f"catmap_mine(键={kind},票数≥{min_support},优势≥{min_dominance:.0%}):"
                 f"{kind} {len(votes)} 个(有实证 PT 投票)→ "
                 f"可信 {counts['mined_trusted']} / 待核(少量支持)"
                 f" {counts['mined_review']} / 分流 {counts['mined_mixed']} / "
                 f"与旧映射冲突 {counts['map_conflict']}",
                 "复核 SQL:SELECT browse_node_id, suggested_pt, "
                 "support_count, product_count, amazon_category "
                 "FROM audit.category_map_suggestions "
                 "WHERE status='map_conflict' ORDER BY support_count DESC;"]
        if counts["mined_mixed"]:
            lines.append("分流桶按优势度分档(降门槛能救回多少):"
                         + " / ".join(f"{b} {n}" for b, n in dom_bands.items()))
        for tag, title in (("mined_trusted", "可信样例(将升级)"),
                           ("map_conflict", "⚠ 与旧映射冲突(只报不改)"),
                           ("mined_mixed", "分流样例(人工看)")):
            if samples.get(tag):
                lines.append(f"{title}:")
                lines += samples[tag]
        if not promote:
            if counts["mined_trusted"]:
                lines.append(f"(可信 {counts['mined_trusted']} 条未升级;"
                             f"确认后 -p promote=1 写进映射表)")
            return "\n".join(lines)
        if not by_node:
            return "\n".join(lines + [
                "⚠ key=path 模式不支持 promote(升级行必须带 browse_node_id"
                "——那才是映射表的 ID 缺口填补物);去掉 key=path 重跑"])
        with conn.cursor() as cur:
            cur.executemany(_PROMOTE_NODE_SQL, promote_rows)
        lines.append(f"升级完成:{len(promote_rows)} 条 mined_trusted → "
                     f"walmart_category_map(带 browse_node_id,confidence=高,"
                     f"match_type=mined_products)✓ 审核 ②a 级即刻生效")
    return "\n".join(lines)
