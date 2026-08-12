"""audit_sync — 审核结论回流:审核系统库 → catalog.products 审核五列。

用法:
  python cli.py audit_sync                 # 全量对齐(幂等,可反复跑)
  python cli.py audit_sync -p limit=100    # 小批冒烟

背景(docs/allocation_plan.md §十一.3,2026-08-12 拍板):
  审核系统(walmart-audit-system)是独立 PG 库 walmart_audit,无 JSON API,
  现行下游对接方式就是直连库。本工作流每轮取**每 ASIN 最新一条** audit_runs
  (DISTINCT ON … ORDER BY run_id DESC,与审核仓库自身回填脚本同款写法),
  写进 catalog.products 的 audit_status / audit_reason / walmart_pt /
  audited_at / audit_version 五列(audit_version = 审核 run_id,可溯源)。

三条取数语义(审核系统实证,改前先读):
  1. stage_stopped_at='SHORTCUT' 的行是"历史短路"——verdict/PT 复制自
     历史真实审核,**可直接用**,不需要排除;
  2. verdict 域是 pass / reject / pending(原样落库,与上架表 E 列口径一致;
     db_schema.md 的域注释已同步改);
  3. pass 有 45 天 TTL,到期重审可能翻案 ⇒ 本工作流**每轮全量对齐**,
     不做增量游标;"内容没变就不写"由 audit_version 比对保证(写放大纪律)。

products 缺行的 ASIN 只计数报告不落库(身份层的行由 product_ingest 建;
候选池 ASIN 推 v4 重采落身份行后,下一轮 audit_sync 自然补上)。
入库/审核**事件**仍按 backlog 第三节保持休眠,本工作流不写 product_events。
"""

import logging

from registry import db

DANGEROUS = False

logger = logging.getLogger("workflows.audit_sync")

_BATCH = 2000

# 每 ASIN 最新一条 + 该 run 的扣分规则码(reason 的兜底来源;
# SHORTCUT 行无 hits,codes 为空,reason 退回 l3 字段或留空)
_PULL_SQL = """
SELECT DISTINCT ON (r.asin)
       r.asin, r.verdict, r.walmart_product_type, r.run_id, r.created_at,
       r.l3_reason_category, r.l3_reason_text, r.stage_stopped_at, h.codes
FROM audit_runs r
LEFT JOIN LATERAL (
    SELECT string_agg(DISTINCT rule_code, ',') AS codes
    FROM audit_hits WHERE run_id = r.run_id AND penalty < 0
) h ON true
ORDER BY r.asin, r.run_id DESC
"""

_COLS = ("asin", "verdict", "pt", "run_id", "created_at",
         "l3_cat", "l3_text", "stage", "codes")

# marketplace 恒 'US':审核系统只做美国站(其 products 表无站点维度)
_UPDATE_SQL = """
UPDATE catalog.products SET
    audit_status  = %(verdict)s,
    audit_reason  = %(reason)s,
    walmart_pt    = %(pt)s,
    audited_at    = %(audited_at)s,
    audit_version = %(ver)s,
    updated_at    = now()
WHERE marketplace = 'US' AND asin = %(asin)s
  AND audit_version IS DISTINCT FROM %(ver)s
"""


def _reason(row: dict) -> str | None:
    """输入:审核行 → 输出:落 audit_reason 的人话摘要(pass 恒 None)。"""
    if row["verdict"] == "pass":
        return None
    parts = []
    if row["l3_cat"]:
        parts.append(row["l3_cat"] + (f": {row['l3_text']}" if row["l3_text"] else ""))
    elif row["codes"]:
        parts.append(f"rules: {row['codes']}")
    if row["stage"]:
        parts.append(f"stage={row['stage']}")
    return "; ".join(parts) or None


def _to_update(row: dict) -> dict:
    return {
        "asin": row["asin"],
        "verdict": row["verdict"],
        "reason": _reason(row),
        "pt": row["pt"],
        "audited_at": row["created_at"],
        "ver": str(row["run_id"]),
    }


def _pull(audit, limit=None):
    """输入:审核库连接(+可选 limit)→ 逐行 yield dict(服务端游标流式)。"""
    sql = _PULL_SQL + (f" LIMIT {int(limit)}" if limit else "")
    with audit.cursor(name="audit_pull") as cur:
        cur.itersize = _BATCH
        cur.execute(sql)
        for r in cur:
            yield dict(zip(_COLS, r))


def _flush(conn, batch: list[dict]) -> int:
    if not batch:
        return 0
    with conn.cursor() as cur:
        cur.executemany(_UPDATE_SQL, batch)
        return max(cur.rowcount, 0)


def run(params: dict) -> str:
    """输入:params(limit 可选)→ 输出:对齐摘要(审核行数/更新数/缺行数)。"""
    limit = params.get("limit")
    counts = {"pass": 0, "reject": 0, "pending": 0, "other": 0}
    missing: list[str] = []
    updated = 0
    seen = 0

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            # 全量 asin 集合驻内存:百万行量级 ≈ 数十 MB,生产 Mac 可承受;
            # 真到撑不住的规模再换临时表 JOIN,先不预支复杂度
            cur.execute("SELECT asin FROM catalog.products WHERE marketplace = 'US'")
            known = {r[0] for r in cur.fetchall()}

        batch: list[dict] = []
        with db.audit_conn() as audit:
            for row in _pull(audit, limit):
                seen += 1
                counts[row["verdict"] if row["verdict"] in counts else "other"] += 1
                if row["asin"] not in known:
                    missing.append(row["asin"])
                    continue
                batch.append(_to_update(row))
                if len(batch) >= _BATCH:
                    updated += _flush(conn, batch)
                    batch = []
        updated += _flush(conn, batch)

    lines = [f"审核结论 {seen} 个 ASIN(pass {counts['pass']} / reject "
             f"{counts['reject']} / pending {counts['pending']}"
             + (f" / 其他 {counts['other']}" if counts["other"] else "") + ")",
             f"五列更新 {updated} 行(其余已是最新,audit_version 比对跳过)"]
    if missing:
        lines.append(f"产品库缺行 {len(missing)} 个 ASIN 未回流(身份行由采集摄取建,"
                     f"重采落库后下轮自动补上;样例 {', '.join(missing[:5])})")
    return ";".join(lines)
