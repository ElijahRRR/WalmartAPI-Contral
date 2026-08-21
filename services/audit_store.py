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
# 第三种 pending(2026-08-20):PT 解出来了,但类目准入白名单里查不到这一行
# ⇒ 判不了。与 L1 那种"根本没解出 PT"是两回事,重试口径也不同:
# 这种要等 walmart_pt_meta 补行(pt_spec_sync),重刷一百遍也不会自己好。
_PENDING_REASON_L2 = "PT 不在类目准入明细,判不了(待补 walmart_pt_meta)"

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
    -- 实证行不许被推断覆盖(与 product_audit._ADOPT_SQL 同一条不变量)
    walmart_pt    = CASE WHEN pt_source = 'walmart_confirmed'
                              AND %(pt_source)s <> 'walmart_confirmed'
                         THEN walmart_pt
                         ELSE COALESCE(%(walmart_pt)s, walmart_pt) END,
    pt_source     = CASE WHEN pt_source = 'walmart_confirmed'
                              AND %(pt_source)s <> 'walmart_confirmed'
                         THEN pt_source
                         WHEN %(walmart_pt)s IS NULL THEN pt_source
                         ELSE %(pt_source)s END,
    audited_at    = now(),
    audit_version = %(version)s
WHERE marketplace = %(marketplace)s AND asin = %(asin)s
"""


def _run_params(outcome: AuditOutcome) -> tuple:
    """输入:判定结果 → 输出:_RUN_SQL 的参数元组。

    单条与批量两条路径共用**同一份**取值,不许各拼各的:runs 有 12 列,
    两处各写一份的话加一列漏改一处 = 静默写错列(元组长度对得上时不报错)。
    """
    # l3/l4 槽位口径逐字迁 orchestrator.py:36-58:未跑 = 'skip'/NULL/'[]'
    l3, l4 = outcome.l3, outcome.l4
    return (
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
    )


def _hit_params(run_id: int, outcome: AuditOutcome) -> list[tuple]:
    """输入:run_id + 判定结果 → 输出:_HIT_SQL 的参数列表。"""
    return [(run_id, h.stage, h.rule_code, h.penalty,
             json.dumps(h.detail or {}, ensure_ascii=False, default=str))
            for h in outcome.all_hits]


def persist_runs(conn, outcomes: list) -> list[int]:
    """输入:连接 + 一批判定结果 → 输出:与入参**同序**的 run_id 列表。

    批量版:runs 一次 executemany(returning)、hits 再一次 executemany。
    逐行版每行要走一个 savepoint + 一次 INSERT + N 次 hits INSERT,在单连接的
    主线程上排队;判定并发调到 128 之后,那一段就是新的瓶颈(所有者定稿
    2026-08-17:「审核默认设置为 128,并且做批量落库」)。

    ⚠ **调用方必须保留逐行兜底**:批量一炸整批回滚,而里面多半只有一行是脏的
    (已付费的 LLM 结果不能陪葬)。product_audit 的做法是 except 后对这一批
    改走 persist_run 逐行落,坏行单独计数 —— 好路径拿批量的速度,坏路径保留
    「一行落库报错不炸整批」那条评审结论。

    ⚠ 顺序是承重的:`returning=True` 的结果集与入参一一对应,调用方按下标把
    run_id 配回 outcome(要写 product_events 的事件行)。配错 = 事件挂到别的
    ASIN 上,而且两边都不报错。
    """
    if not outcomes:
        return []
    with conn.cursor() as cur:
        run_ids: list[int] = []
        cur.executemany(_RUN_SQL, [_run_params(o) for o in outcomes],
                        returning=True)
        while True:
            run_ids.append(cur.fetchone()[0])
            if not cur.nextset():
                break
        if len(run_ids) != len(outcomes):
            raise RuntimeError(
                f"批量落 runs 返回 {len(run_ids)} 个 id,入参 {len(outcomes)} 条 "
                f"—— 顺序对不上就没法把 run_id 配回 outcome,停手")
        hits = [p for rid, o in zip(run_ids, outcomes)
                for p in _hit_params(rid, o)]
        if hits:
            cur.executemany(_HIT_SQL, hits)
    return run_ids


def persist_run(conn, outcome: AuditOutcome) -> int:
    """输入:连接 + 判定结果 → 输出:run_id(runs 一行 + hits 逐条)。

    逐行版,现在只在批量落库出错后当兜底用(见 persist_runs)。
    """
    with conn.cursor() as cur:
        cur.execute(_RUN_SQL, _run_params(outcome))
        run_id = cur.fetchone()[0]
        hits = _hit_params(run_id, outcome)
        if hits:
            cur.executemany(_HIT_SQL, hits)
    return run_id


# L1 各级来源 → 产品主档的两分道(所有者定稿 2026-08-14):只有沃尔玛真
# 接受过的才算实证,其余(含 LLM 推断、映射表推出来的)一律记 audit_llm。
# 映射本身也是人/挖掘给的推断,不是沃尔玛的回执——**别把它当实证再喂回挖掘**,
# 否则 A 推出 B、B 又去证明 A。
_CONFIRMED_SOURCES = frozenset({"walmart_confirmed", "historical_confirmed"})


def pt_provenance(outcome: AuditOutcome) -> str | None:
    """输入:判定结果 → 输出:PT 来源标记('walmart_confirmed'/'audit_llm')。"""
    if real_pt(outcome) is None:
        return None
    return ("walmart_confirmed"
            if outcome.l1.pt_source in _CONFIRMED_SOURCES else "audit_llm")


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
        # 三种 pending 来源分开留痕(重试口径不同):
        #   L1 = 类目根本解不出(候选/rerank 无解)—— 隔天重试有意义
        #   L2 = PT 解出来了但不在准入明细 —— 要等明细补行,重刷无用
        #   L3 = LLM 故障 —— 重试有意义
        reason = {"L3": _PENDING_REASON_L3,
                  "L2": _PENDING_REASON_L2}.get(outcome.stage_stopped_at,
                                                _PENDING_REASON)
    else:
        reason = None
    conn.execute(_PRODUCT_SQL, {
        "status": status, "reason": reason, "walmart_pt": real_pt(outcome),
        "pt_source": pt_provenance(outcome),
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


_LATEST_HITS_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (asin) asin, run_id, l3_reason_text
    FROM audit.audit_runs
    WHERE asin = ANY(%s) AND verdict = 'reject'
      -- SHORTCUT 影子行不算(与 product_audit._HISTORY_SQL 同一条纪律):
      -- 它们是旧仓历史采用的痕迹,没有自己的命中,选中它 = 理由永远查不出
      AND stage_stopped_at IS DISTINCT FROM 'SHORTCUT'
    ORDER BY asin, created_at DESC
)
SELECT l.asin, l.l3_reason_text, h.rule_code, h.detail, h.penalty
FROM latest l
LEFT JOIN audit.audit_hits h ON h.run_id = l.run_id
ORDER BY l.asin, h.penalty NULLS LAST, h.hit_id
"""


def reject_reasons(conn, asins: list[str]) -> dict[str, list[tuple[str, dict]]]:
    """输入:连接 + ASIN 列表 → 输出:{asin: [(规则码, detail), …]}(只判拒的)。

    取每个 ASIN **最近一次判拒**那轮的全部命中,按扣分从重到轻排 ——
    人要看的是"最重的那条为什么",不是第一条。
    L3 有自由文本理由时塞成一条伪命中(rule_code='l3_reason'),
    它往往是判拒里最像人话的一句。
    """
    if not asins:
        return {}
    out: dict[str, list[tuple[str, dict]]] = {}
    seen_l3: set[str] = set()
    with conn.cursor() as cur:
        cur.execute(_LATEST_HITS_SQL, (sorted(set(asins)),))
        for asin, l3_text, code, detail, _pen in cur.fetchall():
            rows = out.setdefault(asin, [])
            if l3_text and asin not in seen_l3:
                seen_l3.add(asin)
                rows.append(("l3_reason", {"note": l3_text}))
            if code:                       # LEFT JOIN:没有命中时 code 为 NULL
                rows.append((code, detail or {}))
    return out
