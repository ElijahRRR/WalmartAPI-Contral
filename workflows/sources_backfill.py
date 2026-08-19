"""sources_backfill — 在架商品来源登记簿补齐(格式回填;幂等可重跑)。

用法:
  python cli.py sources_backfill --dry-run     # 盲区统计:在架未登记多少/格式分类/样本
  python cli.py sources_backfill               # 真跑:写 catalog.listing_sources

背景(2026-08-19 所有者实遇):product_chain 拉回了在架商品,但维护链
(services/maintenance_intents 三个 provider)按路由铁律只认
catalog.listing_sources 里 source_type='amz' 的行——没登记来源的在架商品
一律不维护(防误伤设计,行为正确;问题是该有的登记行没有)。
登记簿的日常写入是「谁上架谁登记」(list_new=amz / match_listing=match),
**旧系统上的存量、以及旧系统调度停掉之前仍在产出的行,没有任何补给线**
——只能靠本工作流按 SKU 格式回填(db_schema.md listing_sources 节
「存量按 SKU 格式一次性回填」的落地件)。旧系统调度停干净后跑一次即封口,
之后不需要常驻;多跑无害(幂等)。

规则(与设计定稿一致,一条不加):
  · sku 形如 ASIN(B+9 位大写字母数字)→ source_type='amz',source_key=sku
    (sku=asin 约定,与 list_new 登记的行完全同构);
  · 其余 → 'unknown':**不参与任何自动破坏动作**(路由铁律),等人工归类;
  · ON CONFLICT DO NOTHING:绝不覆盖 list_new / match_listing 已登记的行;
  · workflow 列写 'backfill'(格式回填的统一记号,出身可审计)。

⚠ 回填成 amz 的行从此被 amz 快照驱动的**改价/清库存/删除**管到——盲区变
辖区,破坏面第一次打开。纪律:真跑后先 `python cli.py maintenance_scan
--dry-run` 看意图量(尤其「建议永久删除」那一段),人眼确认再放行 13:00
的 product_chain。
"""

import logging
import re

from registry import db
from services import listing_sources

DANGEROUS = False   # 只写登记簿一张表(DO NOTHING 幂等),不发 feed 不动状态

logger = logging.getLogger("workflows.sources_backfill")

# 合法 ASIN 形态(与采集侧 common/core/idents.ASIN_RE 同口径;
# scrape_missing/_ASIN_RE 同款本地拷贝——三处口径一致,改要一起改)
_ASIN_RE = re.compile(r"^B[0-9A-Z]{9}$")

_SQL_GAP = """
SELECT w.store, w.sku FROM catalog.walmart_items w
WHERE w.missing_since IS NULL
  AND NOT EXISTS (SELECT 1 FROM catalog.listing_sources ls
                  WHERE ls.store = w.store AND ls.sku = w.sku)
ORDER BY w.store, w.sku
"""


def run(params: dict) -> str:
    """输入:params(execute)→ 输出:盲区统计(dry-run)或回填摘要。"""
    execute = bool(params.get("execute"))
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_GAP)
            gap = cur.fetchall()
        amz = [(s, k) for s, k in gap if _ASIN_RE.fullmatch(k or "")]
        other = [(s, k) for s, k in gap if not _ASIN_RE.fullmatch(k or "")]
        by_store: dict[str, int] = {}
        for s, _k in gap:
            by_store[s] = by_store.get(s, 0) + 1
        mode = "" if execute else "🧪 [DRY-RUN] "
        lines = [f"{mode}在架未登记来源(维护链盲区):{len(gap)} 行 —— "
                 f"格式像 ASIN(登记 amz){len(amz)},"
                 f"其余(登记 unknown,不自动维护){len(other)}"]
        if gap:
            top = sorted(by_store.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
            lines.append("  分店:" + ",".join(f"{s}×{n}" for s, n in top)
                         + (" …" if len(by_store) > 8 else ""))
        if other:
            lines.append(f"  unknown 样本:{[k for _s, k in other[:8]]}")
        if not execute:
            if gap:
                lines.append("  真跑将写 catalog.listing_sources(DO NOTHING "
                             "不覆盖已登记行);回填后先 maintenance_scan "
                             "--dry-run 看破坏面再放行调度")
            return "\n".join(lines)
        n = listing_sources.register(conn, [
            {"store": s, "sku": k,
             "source_type": (listing_sources.SOURCE_AMZ
                             if _ASIN_RE.fullmatch(k or "")
                             else listing_sources.SOURCE_UNKNOWN),
             "source_key": k if _ASIN_RE.fullmatch(k or "") else None,
             "workflow": "backfill"}
            for s, k in gap])
        lines.append(f"已回填 {n} 行;⚠ amz 行自此被自动维护管到 —— 先 "
                     f"`python cli.py maintenance_scan --dry-run` 看意图量"
                     f"(尤其删除段)再放行 product_chain")
    return "\n".join(lines)
