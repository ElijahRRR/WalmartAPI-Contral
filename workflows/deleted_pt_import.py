"""deleted_pt_import — 删除历史实证 PT 导入(walmart_cleanup → 中心库;可重跑)。

用法:
  python cli.py deleted_pt_import              # 预览:体量/可归一率/字典命中率
  python cli.py deleted_pt_import -p apply=1   # 真正入库(upsert)

所有者提议 2026-08-13:旧问题商品清理库 `walmart_cleanup.error_items`
(41.7 万行后台删除快照)带 `product_type` 列——那是沃尔玛后台对已上架
商品认定的 PT,是报错日报(9.7 万行/9 天)之外量级更大的第二实证源。
本工作流把它折叠成 每 ASIN 最新一条 灌进 `audit.deleted_items_pt`,
供 L1 第①b 级(audit_l1_llm.error_confirmed_map)与报错日报按时间戳
合并消费,零 LLM 成本降低 rerank 触达率与 pending 率。

口径:
  · sku 经 services/sku_asin.extract_asin 归一(三段式订货号提得出 ASIN;
    提不出的行不进——PT 键在审核域是 ASIN);
  · product_type 空/'default' 剔除;每 ASIN 取 run_ts 最新一条;
  · **不过 pt_meta 闸**——闸的唯一出处在消费端装配期(error_confirmed_map),
    导入端过闸会让"pt_meta 后来补了该 PT"的行永远进不来;
  · 幂等 = upsert(ON CONFLICT 按 run_ts 新者胜),旧库还在长可以随时重跑。

旧库连接走 registry.db.legacy_cleanup_conn(只读)。
"""

import logging

from registry import db
from services.sku_asin import extract_asin

DANGEROUS = False

logger = logging.getLogger("workflows.deleted_pt_import")

_SRC_SQL = """
SELECT DISTINCT ON (sku) sku, product_type, run_ts
FROM error_items
WHERE product_type IS NOT NULL AND product_type <> ''
  AND product_type <> 'default'
ORDER BY sku, run_ts DESC
"""

_UPSERT_SQL = """
INSERT INTO audit.deleted_items_pt (asin, product_type, run_ts)
VALUES (%s, %s, %s)
ON CONFLICT (asin) DO UPDATE
SET product_type = EXCLUDED.product_type,
    run_ts       = EXCLUDED.run_ts,
    imported_at  = now()
WHERE EXCLUDED.run_ts > audit.deleted_items_pt.run_ts
"""


def fold_rows(rows) -> tuple[dict, int]:
    """输入:(sku, product_type, run_ts) 行 → 输出:({asin: (pt, run_ts)}, 提不出数)。

    纯函数(便于测试)。同一 ASIN 多个 SKU(多店铺)→ run_ts 新者胜。
    """
    out: dict = {}
    no_asin = 0
    for sku, pt, ts in rows:
        asin = extract_asin(sku)
        if not asin:
            no_asin += 1
            continue
        cur = out.get(asin)
        if cur is None or ts > cur[1]:
            out[asin] = (pt, ts)
    return out, no_asin


def run(params: dict) -> str:
    """输入:params(apply=1 才写库)→ 输出:导入体检/结果摘要。"""
    apply = str(params.get("apply", "")).strip() == "1"

    with db.legacy_cleanup_conn() as legacy:
        with legacy.cursor() as cur:
            cur.execute(_SRC_SQL)
            rows = cur.fetchall()
    folded, no_asin = fold_rows(rows)

    lines = [f"deleted_pt_import:旧库带真 PT 的 SKU {len(rows)} 个 → "
             f"归一后 ASIN {len(folded)}(sku 提不出 ASIN 剔除 {no_asin})"]
    if not folded:
        return "\n".join(lines + ["源侧没有可导入的行"])

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit.deleted_items_pt")
            (existing,) = cur.fetchone()
        lines.append(f"库内已有 {existing}(upsert:run_ts 新者胜,可重跑)")
        if not apply:
            lines.append("(预览:未写库;确认无误加 -p apply=1)")
            return "\n".join(lines)
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, [
                (asin, pt, ts) for asin, (pt, ts) in folded.items()])
            cur.execute("SELECT count(*) FROM audit.deleted_items_pt")
            (after,) = cur.fetchone()
    lines.append(f"导入完成:库内 {existing} → {after} 行 ✓"
                 f"(下一轮 product_audit 装配期自动生效)")
    return "\n".join(lines)
