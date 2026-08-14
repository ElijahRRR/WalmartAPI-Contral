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

产出三态(报告分列,只有 aligned 落库):
  aligned    唯一最佳且分数达标 → 假缺口,别名落库,②级立即能命中
  ambiguous  多个候选并列 → 交人工(**不猜**)
  no_match   叶子/顶级对不上 → 真缺口,归 catmap_mine / catmap_suggest 处理

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

# 映射表侧候选路径(高置信;哨兵行也要——'无对应Walmart PT' 是有效结论)
_CANON_SQL = """
SELECT DISTINCT btrim(amazon_category)
FROM audit.walmart_category_map
WHERE confidence = '高' AND btrim(amazon_category) <> ''
"""

_UPSERT_SQL = """
INSERT INTO audit.category_path_alias (path, canonical_path, score)
VALUES (%s, %s, %s)
ON CONFLICT (path) DO UPDATE
SET canonical_path = EXCLUDED.canonical_path,
    score          = EXCLUDED.score,
    aligned_at     = now()
"""


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


def run(params: dict) -> str:
    """输入:params(apply=1 才写库)→ 输出:真/假缺口分布摘要。"""
    apply = str(params.get("apply", "")).strip() == "1"

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_CANON_SQL)
            canon = [r[0] for r in cur.fetchall()]
            cur.execute(_GAP_SQL)
            gaps = cur.fetchall()
        idx = build_leaf_index(canon)

        rows, samples = [], []
        stat = {"aligned": 0, "ambiguous": 0, "no_match": 0}
        prod = {"aligned": 0, "ambiguous": 0, "no_match": 0}
        for path, n in gaps:
            cands = idx.get(catpath.leaf_key(path) or "", [])
            best, score, status = catpath.align_path(path, cands)
            stat[status] += 1
            prod[status] += n
            if status == "aligned":
                rows.append((path, best, round(score, 3)))
                if len(samples) < 10:
                    samples.append(f"  {n:>5} 件|{path[:60]}…\n"
                                   f"        ↳ {best[:60]}…(分 {score:.2f})")

        lines = [
            f"catmap_align:精确匹配缺口 {len(gaps)} 条路径 → "
            f"别名可折 {stat['aligned']}(覆盖 {prod['aligned']} 件产品)/ "
            f"歧义待人工 {stat['ambiguous']}({prod['ambiguous']} 件)/ "
            f"真缺口 {stat['no_match']}({prod['no_match']} 件)",
            f"⇒ 假缺口占比 {stat['aligned'] / len(gaps) * 100:.1f}%"
            if gaps else "⇒ 无缺口",
        ]
        lines += samples
        if not apply:
            lines.append("(预览:未写库;确认无误加 -p apply=1)")
            return "\n".join(lines)
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, rows)
    lines.append(f"别名落库 {len(rows)} 条 ✓(下一轮 product_audit ②级"
                 f"经别名折叠命中,rerank 与 pending 同步下降)")
    return "\n".join(lines)
