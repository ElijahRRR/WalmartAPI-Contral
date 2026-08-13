"""catmap_suggest — 类目映射缺口批量出建议(LLM,按路径去重;可反复跑)。

用法:
  python cli.py catmap_suggest                 # 跑缺口 Top 50 路径(默认)
  python cli.py catmap_suggest -p limit=200    # 一次多跑一些
  python cli.py catmap_suggest -p refresh=1    # 已有建议的路径也重跑

背景(所有者发起 2026-08-13):映射表是批次 A 搬来的死水(旧 a2w 工具在
erp-core,未迁),缺口实测 7,512 条路径覆盖 55 万产品。审核链对同一缺口
路径的每个产品都要单独 rerank——本工作流反过来**按路径出建议,一路径只
调一次 LLM**,同路径产品共享答案。

流程:聚合 catalog.products 中映射表缺席的 amazon_category(按产品数
降序取 Top N,已有建议的跳过)→ 每路径取一个代表产品(有标题的最新行)
→ 复用 audit_l1_llm.candidates + rerank → 建议落
audit.category_map_suggestions 并打印。

**建议是死数据,零消费**:审核链只读 walmart_category_map。人工确认后
的升级通道等所有者定编辑权威(飞书「映射明细」表 vs PG 直改)再建——
确认行进了映射表,②级直出立即对全体同路径产品生效。

status 语义:ok=LLM 从候选挑出了 PT;unknown=LLM 判候选都不匹配;
excluded=LLM 判禁售品类;no_candidate=五路召回全空(这些路径最需要
人工);llm_failed=调用失败(重跑吸收)。
"""

import logging

from registry import db
from services import audit_l1_llm, audit_rules

DANGEROUS = False

logger = logging.getLogger("workflows.catmap_suggest")

_GAP_SQL = """
SELECT p.amazon_category, count(*) AS n,
       max(p.asin) FILTER (WHERE p.title IS NOT NULL AND p.title <> '') AS sample_asin
FROM catalog.products p
WHERE p.marketplace = 'US'
  AND p.amazon_category IS NOT NULL AND p.amazon_category <> ''
  AND NOT EXISTS (SELECT 1 FROM audit.walmart_category_map m
                  WHERE btrim(m.amazon_category) = btrim(p.amazon_category))
  AND ({skip_suggested})
GROUP BY p.amazon_category
HAVING max(p.asin) FILTER (WHERE p.title IS NOT NULL AND p.title <> '') IS NOT NULL
ORDER BY n DESC
LIMIT %(limit)s
"""

_SKIP_SUGGESTED = ("NOT EXISTS (SELECT 1 FROM audit.category_map_suggestions s "
                   "WHERE s.amazon_category = p.amazon_category)")

_SAMPLE_SQL = """
SELECT asin, title, brand, amazon_category AS amazon_category_path,
       slow -> 'bullet_points' AS bullet_points,
       coalesce(slow ->> 'description', slow ->> 'long_description',
                slow ->> 'product_description') AS long_description
FROM catalog.products
WHERE marketplace = 'US' AND asin = %s
"""

_UPSERT_SQL = """
INSERT INTO audit.category_map_suggestions
    (amazon_category, suggested_pt, confidence, status,
     sample_asin, sample_title, product_count)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (amazon_category) DO UPDATE
SET suggested_pt = EXCLUDED.suggested_pt,
    confidence   = EXCLUDED.confidence,
    status       = EXCLUDED.status,
    sample_asin  = EXCLUDED.sample_asin,
    sample_title = EXCLUDED.sample_title,
    product_count = EXCLUDED.product_count,
    created_at   = now()
"""


def suggestion_from_l1(l1) -> tuple[str | None, str | None, str]:
    """输入:rerank 返回(L1Info 或 None)→ 输出:(pt, confidence, status)。

    纯函数(便于测试)。excluded 命中(带 -100 hit)→ status='excluded'
    且 pt 仍记录(便于人工看它想挑什么);unknown/解不出 → 无 pt。
    """
    if l1 is None:
        return None, None, "unknown"
    if any(h.penalty < 0 for h in l1.hits):
        return l1.walmart_product_type, l1.pt_confidence, "excluded"
    return l1.walmart_product_type, l1.pt_confidence, "ok"


def run(params: dict) -> str:
    """输入:params(limit=路径数,refresh=1 重跑已有)→ 输出:建议批次摘要。"""
    limit = int(params.get("limit", 50))
    refresh = str(params.get("refresh", "")).strip() == "1"

    with db.pg_conn() as conn:
        skip = "true" if refresh else _SKIP_SUGGESTED
        with conn.cursor() as cur:
            cur.execute(_GAP_SQL.format(skip_suggested=skip),
                        {"limit": limit})
            gaps = cur.fetchall()
        if not gaps:
            return "catmap_suggest:没有待建议的缺口路径(全部已有建议;" \
                   "refresh=1 可重跑)"

        pt_dict = None
        counts = {"ok": 0, "unknown": 0, "excluded": 0,
                  "no_candidate": 0, "llm_failed": 0}
        lines = []
        for path, n, sample_asin in gaps:
            with conn.cursor() as cur:
                cur.execute(_SAMPLE_SQL, (sample_asin,))
                cols = [d[0] for d in cur.description]
                row = dict(zip(cols, cur.fetchone()))
            product = audit_rules.product_info_from_row(row)
            cands = audit_l1_llm.candidates(conn, product)
            if not cands:
                pt, conf, status = None, None, "no_candidate"
            else:
                if pt_dict is None:   # 首次才装 pt_meta(只要键,轻装配)
                    with conn.cursor() as cur:
                        cur.execute("SELECT walmart_product_type "
                                    "FROM audit.walmart_pt_meta")
                        pt_dict = {r[0] for r in cur.fetchall()}
                try:
                    l1 = audit_l1_llm.rerank(product, cands, pt_dict)
                except Exception as e:  # noqa: BLE001 —— 单路径隔离,计数亮出
                    logger.warning("建议失败 path=%s:%s", path[:80], e)
                    pt, conf, status = None, None, "llm_failed"
                else:
                    pt, conf, status = suggestion_from_l1(l1)
            counts[status] += 1
            with conn.cursor() as cur:
                cur.execute(_UPSERT_SQL, (path, pt, conf, status,
                                          row["asin"],
                                          (row.get("title") or "")[:200], n))
            if status == "ok":
                lines.append(f"  {n:>5} 件|{path[:70]} → {pt}({conf})")

    head = [f"catmap_suggest:本轮 {len(gaps)} 条路径 → "
            f"建议 {counts['ok']} / unknown {counts['unknown']} / "
            f"禁售 {counts['excluded']} / 无候选 {counts['no_candidate']} / "
            f"失败 {counts['llm_failed']}",
            "复核 SQL:SELECT * FROM audit.category_map_suggestions "
            "ORDER BY product_count DESC;",
            "确认后的升级通道待定(飞书「映射明细」vs PG 直写,所有者定)"]
    return "\n".join(head + lines[:20]
                     + ([f"  …共 {counts['ok']} 条建议,余见复核 SQL"]
                        if counts["ok"] > 20 else []))
