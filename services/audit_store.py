"""审核结论落库积木(audit_runs/audit_hits 明细 + catalog.products 五列 + 事件行)。

列映射逐字对齐旧仓 orchestrator._persist_run(spec_shortcut §4):
  score_start 硬编码 100;无 L3/L4 时 l3_verdict/l4_verdict 写字符串 'skip'
  (不是 NULL)、l4_issues 写 '[]';detail JSON 一律 ensure_ascii=False
  (否则中文与历史 204 万行长得不一样,文本检索会漂)。
verdict → audit_status 两套词表必须显式映射:pass→approved / reject→rejected /
pending→pending(catalog.products.audit_status 值域,schema.sql:22)。
final_reason_category 无 runs 列(旧仓同,动态算的)——落点是
catalog.products.audit_reason。
"""

import json

from registry import resources
from services import product_events
from services.audit_models import AuditOutcome

_VERDICT_TO_STATUS = {"pass": "approved", "reject": "rejected",
                      "pending": "pending"}

_PENDING_REASON = "待类目判定(候选/rerank 均解不出,每日退避重试)"
_PENDING_REASON_L3 = "LLM 全链路故障, 待人工复核"   # 旧仓字面量(l3_llm.py F1)

_RUN_SQL = """
INSERT INTO audit.audit_runs
  (asin, walmart_product_type, pt_confidence, pt_source,
   score_start, score_final, verdict, stage_stopped_at,
   l3_verdict, l3_reason_category, l3_reason_text, l4_verdict, l4_issues)
VALUES (%s, %s, %s, %s, 100, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
RETURNING run_id
"""

_HIT_SQL = """
INSERT INTO audit.audit_hits (run_id, stage, rule_code, penalty, detail)
VALUES (%s, %s, %s, %s, %s::jsonb)
"""

_PRODUCT_SQL = """
UPDATE catalog.products
SET audit_status  = %(status)s,
    audit_reason  = %(reason)s,
    walmart_pt    = COALESCE(%(walmart_pt)s, walmart_pt),
    audited_at    = now(),
    audit_version = %(version)s
WHERE marketplace = %(marketplace)s AND asin = %(asin)s
"""


def persist_run(conn, outcome: AuditOutcome) -> int:
    """输入:连接 + 判定结果 → 输出:run_id(runs 一行 + hits 逐条)。"""
    with conn.cursor() as cur:
        # l3/l4 槽位口径逐字迁 orchestrator.py:36-58:未跑 = 'skip'/NULL/'[]'
        l3, l4 = outcome.l3, outcome.l4
        cur.execute(_RUN_SQL, (
            outcome.asin,
            outcome.l1.walmart_product_type,
            outcome.l1.pt_confidence,
            outcome.l1.pt_source,
            outcome.score_final,
            outcome.verdict,
            outcome.stage_stopped_at,
            l3.verdict if l3 else "skip",
            l3.reason_category if l3 else None,
            l3.reason_text if l3 else None,
            l4.verdict if l4 else "skip",
            json.dumps(l4.image_issues, ensure_ascii=False, default=str)
            if l4 else "[]",
        ))
        run_id = cur.fetchone()[0]
        hits = outcome.all_hits
        if hits:
            cur.executemany(_HIT_SQL, [
                (run_id, h.stage, h.rule_code, h.penalty,
                 json.dumps(h.detail or {}, ensure_ascii=False, default=str))
                for h in hits])
    return run_id


def real_pt(outcome: AuditOutcome) -> str | None:
    """输入:判定结果 → 输出:可写入 products.walmart_pt 的真实 PT(桩值不算)。"""
    pt = outcome.l1.walmart_product_type
    if not pt or pt.startswith("("):     # "(phase0_blocked)" 等桩值不进身份层
        return None
    if pt == "unknown":                  # 批次 C:哨兵/excluded 路径的旧仓字面量
        return None                      # (runs 里留档,身份层不收占位值)
    return pt


def write_conclusion(conn, outcome: AuditOutcome,
                     marketplace: str = "US") -> None:
    """输入:连接 + 判定结果 → 输出:无(写 catalog.products 五列)。"""
    status = _VERDICT_TO_STATUS[outcome.verdict]
    if outcome.verdict == "reject":
        reason = outcome.final_reason_category
    elif outcome.verdict == "pending":
        # 两种 pending 来源分开留痕:L1=类目解不出,L3=LLM 故障(重试口径不同)
        reason = (_PENDING_REASON_L3 if outcome.stage_stopped_at == "L3"
                  else _PENDING_REASON)
    else:
        reason = None
    conn.execute(_PRODUCT_SQL, {
        "status": status, "reason": reason, "walmart_pt": real_pt(outcome),
        "version": resources.AUDIT_RULES_VERSION,
        "marketplace": marketplace, "asin": outcome.asin,
    })


def event_row(outcome: AuditOutcome, run_id: int | None,
              source: str = "product_audit") -> dict | None:
    """输入:判定结果 + run_id → 输出:product_events 行(pending 不入病历)。"""
    if outcome.verdict == "pass":
        code = product_events.AUDIT_PASSED
        detail = {"walmart_pt": real_pt(outcome),
                  "audit_version": resources.AUDIT_RULES_VERSION,
                  "run_id": run_id}
    elif outcome.verdict == "reject":
        code = product_events.AUDIT_REJECTED
        detail = {"reason": outcome.final_reason_category,
                  "rule_codes": sorted({h.rule_code for h in outcome.all_hits
                                        if h.penalty < 0}) or
                                sorted({h.rule_code for h in outcome.all_hits}),
                  "audit_version": resources.AUDIT_RULES_VERSION,
                  "run_id": run_id}
    else:
        return None      # pending 是过渡态不是生死节点,不进病历
    return {"sku": outcome.asin, "event": code, "source": source,
            "detail": detail}
