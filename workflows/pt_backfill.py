"""pt_backfill — 历史实证 PT 回填产品主档(可重跑)。

用法:
  python cli.py pt_backfill              # 预览:体量/已有行/占位行
  python cli.py pt_backfill -p apply=1   # 真正写库(只填空,不覆盖)

所有者定稿 2026-08-13:PT 是产品属性,直接长在 catalog.products.walmart_pt
列上,**不另建证据边表**(原 deleted_items_pt 方案废弃)。产品表一 ASIN
一行是主档:历史上知道 PT 的产品没在库就先占位一行,以后采集摄取时
product_ingest 的 COALESCE upsert 自动填充其余列(walmart_pt 不在 ingest
列清单里,不会被采集覆盖)。

数据源(两份历史实证,按时间戳每 ASIN 取最新):
  · walmart_cleanup.error_items(41.7 万行后台删除快照,product_type =
    沃尔玛认定 PT;registry.db.legacy_cleanup_conn 只读)
  · audit.walmart_error_records(9.7 万行报错日报,walmart_pt;'default' 剔)

写入语义:**只填空**——`ON CONFLICT ... WHERE walmart_pt IS NULL`,已有值
(审核结论写的或上一轮回填的)绝不覆盖;可随时重跑吸收旧库新增行。
sku 经 services/sku_asin.extract_asin 归一,提不出 ASIN 的行剔除计数。
**不过 pt_meta 闸**:闸的唯一出处在消费端(resolve_pt 判定时校验),
导入端过闸会让"pt_meta 后来补了该 PT"的行永远进不来。

消费方:audit_rules.resolve_pt ①b 级直接读产品行的 walmart_pt
(pt_source='historical_confirmed');占位行(title 为空)不进审核候选
(product_audit 候选 SQL 排除,防止空壳行饿死队列)。
"""

import logging

from registry import db
from services.sku_asin import extract_asin

DANGEROUS = False

logger = logging.getLogger("workflows.pt_backfill")

_LEGACY_SQL = """
SELECT DISTINCT ON (sku) sku, product_type, run_ts
FROM error_items
WHERE product_type IS NOT NULL AND product_type <> ''
  AND product_type <> 'default'
ORDER BY sku, run_ts DESC
"""

_ERROR_RECORDS_SQL = """
SELECT DISTINCT ON (asin) asin, walmart_pt, recorded_at
FROM audit.walmart_error_records
WHERE asin IS NOT NULL AND asin <> ''
  AND walmart_pt IS NOT NULL AND walmart_pt <> 'default'
ORDER BY asin, recorded_at DESC NULLS LAST
"""

# 一条语句同时覆盖"占位新行"与"已有行填空":walmart_pt 已有值的行 WHERE
# 不满足 → 跳过(审核结论优先,回填绝不覆盖)
_UPSERT_SQL = """
INSERT INTO catalog.products (marketplace, asin, walmart_pt)
VALUES ('US', %s, %s)
ON CONFLICT (marketplace, asin) DO UPDATE
SET walmart_pt = EXCLUDED.walmart_pt, updated_at = now()
WHERE catalog.products.walmart_pt IS NULL
"""


def fold_rows(rows) -> tuple[dict, int]:
    """输入:(sku或asin, pt, ts) 行 → 输出:({asin: (pt, ts)}, 提不出 ASIN 数)。

    纯函数(便于测试)。键经 extract_asin 归一;同 ASIN 多行 → ts 新者胜
    (ts 为 None 视为最旧)。
    """
    out: dict = {}
    no_asin = 0
    for key, pt, ts in rows:
        asin = extract_asin(key)
        if not asin:
            no_asin += 1
            continue
        cur = out.get(asin)
        if cur is None or (ts is not None and (cur[1] is None or ts > cur[1])):
            out[asin] = (pt, ts)
    return out, no_asin


def run(params: dict) -> str:
    """输入:params(apply=1 才写库)→ 输出:回填体检/结果摘要。"""
    apply = str(params.get("apply", "")).strip() == "1"

    with db.legacy_cleanup_conn() as legacy:
        with legacy.cursor() as cur:
            cur.execute(_LEGACY_SQL)
            legacy_rows = cur.fetchall()

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_ERROR_RECORDS_SQL)
            err_rows = cur.fetchall()
        folded, no_asin = fold_rows(list(legacy_rows) + list(err_rows))
        lines = [f"pt_backfill:删除历史 {len(legacy_rows)} + 报错日报 "
                 f"{len(err_rows)} → 归一后 ASIN {len(folded)}"
                 f"(提不出 ASIN 剔除 {no_asin})"]
        if not folded:
            return "\n".join(lines + ["没有可回填的行"])

        asins = list(folded.keys())
        with conn.cursor() as cur:
            cur.execute("SELECT count(*),"
                        " count(*) FILTER (WHERE walmart_pt IS NOT NULL) "
                        "FROM catalog.products "
                        "WHERE marketplace = 'US' AND asin = ANY(%s)",
                        (asins,))
            in_lib, has_pt = cur.fetchone()
        stubs = len(folded) - in_lib
        lines.append(f"库内已有 {in_lib}(其中 {has_pt} 已有 PT 不覆盖,"
                     f"{in_lib - has_pt} 将填空)+ 占位新行 {stubs}"
                     f"(title 空,不进审核候选,待采集填充)")
        if not apply:
            lines.append("(预览:未写库;确认无误加 -p apply=1)")
            return "\n".join(lines)

        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, [
                (asin, pt) for asin, (pt, _ts) in folded.items()])
    lines.append(f"回填完成 ✓(填空 {in_lib - has_pt} + 占位 {stubs};"
                 f"下一轮 product_audit ①b 级直接受益)")
    return "\n".join(lines)
