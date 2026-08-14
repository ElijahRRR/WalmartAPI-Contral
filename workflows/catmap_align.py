"""catmap_align — 类目路径别名对齐(纯 SQL+纯函数,零 LLM;可反复跑)。

用法:
  python cli.py catmap_align              # 预览:真缺口 vs 假缺口(别名)分布
  python cli.py catmap_align -p apply=1   # 别名落库(审核②级即刻多命中)

所有者发现 2026-08-13:Amazon 的 URL slug / 商品面包屑 / Best Sellers 导航
三套名称不完全一致,**中间层节点名有别名漂移**(叶子与顶级通常一致):
  产品侧 `… > Home Décor Products > … > Wall & Tabletop Frames`
  映射表 `… > Home Décor          > … > Wall & Tabletop Frames`
按完整路径精确等值 ⇒ 已映射的路径被误判成缺口。本工作流把"精确匹配不到"
的产品路径,按 services/catpath 的三闸对齐(叶子相等 + 顶级相等 + 段集
重叠唯一最佳)折回映射表里的等价路径,落 audit.category_path_alias。

**顶级闸挡得住跨大类串类,挡不住同一大类内的不同子树**(所有者首跑
实测:`Home Décor Accents > … > Decorative Signs & Plaques` 被 0.60 分
折到 `Seasonal Décor > Wreath Hangers > …`)。故收口靠两条,而不是单纯
调阈值(合法浅路径 `… > Vases`、`… > Party Packs` 只有 0.67,一刀切会误伤):

  1. **实证背书**:缺口路径下的产品带实证 walmart_pt(pt_backfill 回填),
     与候选 canonical 路径的映射 PT 一比即知真假——**PT 一致 = verified**
     (任何分数都收,数据说话);**PT 相左 = pt_conflict**(高分也不折);
  2. 无实证可查时才看分数:≥ min_score(默认 0.75,两条真实漂移实测
     0.80/0.83)才折;0.5~0.75 落 low_score 报告,交人工。

产出六态(只有 verified / aligned 落库):
  verified    实证 PT 与 canonical 映射 PT 一致 → 最硬的证据,直接折
  aligned     无实证可查、分数 ≥ min_score → 折
  low_score   无实证、分数不足 → 报告待人工(不折)
  pt_conflict 实证 PT 与 canonical 相左 → **不折**,报告(疑似串子树)
  ambiguous   多个候选并列 → 交人工(**不猜**)
  no_match    叶子/顶级对不上 → 真缺口,归 catmap_mine / catmap_suggest

幂等:ON CONFLICT 覆盖(阈值调整或映射表新增后重跑即刷新)。别名只是
**查询侧折叠**,不改写映射表一行——映射表编辑权威(飞书 vs PG)未定,
别名表天然不涉及那个决定。
"""

import logging

from registry import db
from services import catpath

DANGEROUS = False

logger = logging.getLogger("workflows.catmap_align")

# 产品侧待对齐路径(精确匹配不到映射表的;按产品数降序,大缺口优先)
_GAP_SQL = """
SELECT btrim(p.amazon_category) AS path, count(*) AS n
FROM catalog.products p
WHERE p.marketplace = 'US'
  AND p.amazon_category IS NOT NULL AND btrim(p.amazon_category) <> ''
  AND NOT EXISTS (SELECT 1 FROM audit.walmart_category_map m
                  WHERE btrim(m.amazon_category) = btrim(p.amazon_category))
GROUP BY 1
ORDER BY n DESC
"""

# 映射表侧候选路径 + 其 PT(高置信;哨兵行也要——'无对应Walmart PT'
# 是有效结论。同路径多 PT 的取 NULL:PT 不唯一时无从做实证背书)
_CANON_SQL = """
SELECT btrim(amazon_category),
       CASE WHEN count(DISTINCT walmart_product_type) = 1
            THEN min(walmart_product_type) END AS pt
FROM audit.walmart_category_map
WHERE confidence = '高' AND btrim(amazon_category) <> ''
GROUP BY 1
"""

# 缺口路径的产品实证 PT 分布(pt_backfill 回填 + 审核结论;过 pt_meta 闸)
_EVIDENCE_SQL = """
SELECT btrim(p.amazon_category), p.walmart_pt, count(*)
FROM catalog.products p
JOIN audit.walmart_pt_meta m ON m.walmart_product_type = p.walmart_pt
WHERE p.marketplace = 'US'
  AND p.amazon_category IS NOT NULL AND btrim(p.amazon_category) <> ''
  AND p.walmart_pt IS NOT NULL AND p.walmart_pt <> 'unknown'
GROUP BY 1, 2
"""

_UPSERT_SQL = """
INSERT INTO audit.category_path_alias (path, canonical_path, score)
VALUES (%s, %s, %s)
ON CONFLICT (path) DO UPDATE
SET canonical_path = EXCLUDED.canonical_path,
    score          = EXCLUDED.score,
    aligned_at     = now()
"""


MIN_EVIDENCE = 2         # 实证背书需要的最少产品数(单证不立)


def consensus_pt(dist: dict, min_n: int = MIN_EVIDENCE) -> str | None:
    """输入:{pt: 产品数} → 输出:共识 PT(单一 PT 且票数达标)或 None。

    纯函数。有分流(多个 PT)就没有共识——那种路径本身就该交人工,
    不配给别名做背书。
    """
    if len(dist) != 1:
        return None
    pt, n = next(iter(dist.items()))
    return pt if n >= min_n else None


def decide_alias(tier: str, evidence_pt: str | None,
                 canonical_pt: str | None) -> str:
    """输入:结构信任层 + 实证共识 PT + canonical 映射 PT → 输出:落库状态。

    纯函数。两条判据,实证优先于结构(数据 > 字符串形态):

      实证 PT 与 canonical 一致 → **verified**(任何结构层都收)。别名的
        目的不是证明两条路径语义等价,而是**拿到正确的 PT**——产品实证
        说的 PT 与折过去拿到的 PT 相同,结果就是对的;
      实证 PT 与 canonical 相左 → **pt_conflict**(结构再像也拒)。所有者
        首跑实测拦下 `Team Sports > Soccer > …` → `… > Lacrosse > …`;
      无实证可查 → 只信 strong 层(仅上层一段改名,叶子的父归属未变);
        medium(父节点也变)与 weak(差两段以上)一律交人工。
    """
    if evidence_pt and canonical_pt:
        return "verified" if evidence_pt == canonical_pt else "pt_conflict"
    return "aligned" if tier == "strong" else "needs_review"


def build_leaf_index(canonical_paths) -> dict:
    """输入:映射表路径集合 → 输出:{归一叶子: [路径,…]}(纯函数,便于测试)。

    对齐的第一闸是"叶子相等",故按叶子建倒排索引——1.5 万条映射表对
    7 千条缺口路径,不建索引就是亿级串比较。
    """
    idx: dict = {}
    for p in canonical_paths:
        key = catpath.leaf_key(p)
        if key:
            idx.setdefault(key, []).append(p)
    return idx


_STATUSES = ("verified", "aligned", "needs_review", "pt_conflict",
             "ambiguous", "no_match")
_FOLDABLE = ("verified", "aligned")


def run(params: dict) -> str:
    """输入:params(apply=1 写库)→ 输出:分布摘要。"""
    apply = str(params.get("apply", "")).strip() == "1"

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_CANON_SQL)
            canon_pt = dict(cur.fetchall())
            cur.execute(_EVIDENCE_SQL)
            votes: dict = {}
            for path, pt, n in cur.fetchall():
                votes.setdefault(path, {})[pt] = n
            cur.execute(_GAP_SQL)
            gaps = cur.fetchall()
        idx = build_leaf_index(canon_pt.keys())

        rows = []
        samples: dict = {s: [] for s in _STATUSES}
        stat = {s: 0 for s in _STATUSES}
        prod = {s: 0 for s in _STATUSES}
        tiers = {"strong": 0, "medium": 0, "weak": 0}
        for path, n in gaps:
            cands = idx.get(catpath.leaf_key(path) or "", [])
            best, score, status = catpath.align_path(path, cands)
            tier = ""
            if status == "aligned":     # 再过结构层 + 实证背书两道
                tier = catpath.align_tier(catpath.segments(path),
                                          catpath.segments(best))
                tiers[tier] += 1
                status = decide_alias(tier, consensus_pt(votes.get(path, {})),
                                      canon_pt.get(best))
            stat[status] += 1
            prod[status] += n
            if status in _FOLDABLE:
                rows.append((path, best, round(score, 3)))
            if best and len(samples[status]) < 5:
                samples[status].append(
                    f"  {n:>5} 件|{path[:58]}…\n"
                    f"        ↳ {best[:58]}…({tier},差异分 {score:.2f})")

        foldable = sum(stat[s] for s in _FOLDABLE)
        lines = [
            f"catmap_align:精确匹配缺口 {len(gaps)} 条路径 →",
            f"  可折 {foldable} 条 / {prod['verified'] + prod['aligned']} 件"
            f"(实证 PT 背书 {stat['verified']} + 结构 strong 层 "
            f"{stat['aligned']})",
            f"  待人工 {stat['needs_review'] + stat['pt_conflict'] + stat['ambiguous']}"
            f" 条 / {prod['needs_review'] + prod['pt_conflict'] + prod['ambiguous']}"
            f" 件(无实证且结构存疑 {stat['needs_review']} / **PT 相左 "
            f"{stat['pt_conflict']}** / 并列歧义 {stat['ambiguous']})",
            f"  真缺口 {stat['no_match']} 条 / {prod['no_match']} 件"
            f"(归 catmap_mine / catmap_suggest)",
            f"  结构层分布:strong(仅上层改名){tiers['strong']} / "
            f"medium(父节点也变){tiers['medium']} / "
            f"weak(差两段以上){tiers['weak']}",
        ]
        for tag, title in (("verified", "实证 PT 背书样例(数据说话,已折)"),
                           ("aligned", "结构 strong 层样例(仅上层一段改名,已折)"),
                           ("pt_conflict", "⚠ PT 相左(疑似串子树,已拒折)"),
                           ("needs_review", "待人工(无实证 + 结构存疑)")):
            if samples[tag]:
                lines.append(f"{title}:")
                lines += samples[tag]
        if not apply:
            lines.append("(预览:未写库;确认无误加 -p apply=1)")
            return "\n".join(lines)
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, rows)
    lines.append(f"别名落库 {len(rows)} 条 ✓(下一轮 product_audit ②级"
                 f"经别名折叠命中,rerank 与 pending 同步下降)")
    return "\n".join(lines)
