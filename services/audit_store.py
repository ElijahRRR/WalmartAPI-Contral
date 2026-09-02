"""审核结论落库积木(audit_runs/audit_hits 明细 + catalog.products 六列 + 事件行)。

列映射逐字对齐旧仓 orchestrator._persist_run(spec_shortcut §4):
  score_start 硬编码 100;无 L3/L4 时 l3_verdict/l4_verdict 写字符串 'skip'
  (不是 NULL)、l4_issues 写 '[]';detail JSON 一律 ensure_ascii=False
  (否则中文与历史 204 万行长得不一样,文本检索会漂)。
verdict → audit_status 两套词表必须显式映射:pass→approved / reject→rejected /
pending→pending(catalog.products.audit_status 值域,schema.sql:22)。
final_reason_category 无 runs 列(旧仓同,动态算的)——落点是
catalog.products.audit_reason。

**三段输出的落点**(2026-09-02 B1,`docs/audit_step3_spec.md` §3.4):

| 段 | audit_runs | catalog.products | product_events |
|---|---|---|---|
| 判定结果 | `verdict` / `l3_verdict` | `audit_status` | 事件码 |
| 类别 | `l3_reason_category`(列名不改,语义 = 类别枚举) | `audit_reason` | `detail.reason` |
| 具体内容 | `l3_reason_text`(列名不改,语义 = 具体内容) | **新列 `audit_detail`** | `detail.detail` |

两条列名不改是有意的:`audit_runs` 有百万级存量行,改名要连历史一起迁,
而语义收窄(类别只装枚举)在**新写入**的行上就成立;老行仍是旧语义,
`audit_why` / 报表按 `audit_version` 分辨(留档口径见 docs/db_schema.md)。
"""

import json

from registry import resources
from services import audit_reason, product_events
from services.audit_models import AuditOutcome

_VERDICT_TO_STATUS = {"pass": "approved", "reject": "rejected",
                      "pending": "pending"}

_PENDING_REASON = "待类目判定(候选/rerank 均解不出,每日退避重试)"
_PENDING_REASON_L3 = "LLM 全链路故障, 待人工复核"   # 旧仓字面量(l3_llm.py F1)
# 第三种 pending(2026-08-20):PT 解出来了,但类目准入白名单里查不到这一行
# ⇒ 判不了。与 L1 那种"根本没解出 PT"是两回事,重试口径也不同:
# 这种要等 walmart_pt_meta 补行(pt_spec_sync),重刷一百遍也不会自己好。
_PENDING_REASON_L2 = "PT 不在类目准入明细,判不了(待补 walmart_pt_meta)"

# ⚠ `audit_version` 是 2026-09-02 B2 补的列(`refdata/schema.sql`):这张表原本
# 没有任何"这一行是哪一版判据判的"的痕迹,于是**回放评估分不清新旧链** ——
# `mode=stale` 一跑,每个 asin 的"最近一次 run"就变成了新链自己的结论,
# 再拿它当"旧链基线"就是自己跟自己比,而且数字看着完全正常。
# 存量 204 万行这一列是 NULL = 旧链(回放按 `IS DISTINCT FROM 当前版本` 取基线)。
_RUN_SQL = """
INSERT INTO audit.audit_runs
  (asin, walmart_product_type, pt_confidence, pt_source,
   score_start, score_final, verdict, stage_stopped_at,
   l3_verdict, l3_reason_category, l3_reason_text, l4_verdict, l4_issues,
   audit_version)
VALUES (%s, %s, %s, %s, 100, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
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
    audit_detail  = %(detail)s,
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
        l3.policy if l3 else None,          # 列名不改,语义 = 类别枚举
        l3.detail if l3 else None,          # 列名不改,语义 = 具体内容
        l4.verdict if l4 else "skip",
        json.dumps(l4.image_issues, ensure_ascii=False, default=str)
        if l4 else "[]",
        resources.AUDIT_RULES_VERSION,   # 这一行是哪一版判据判的(回放分新旧链靠它)
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


def conclusion_detail(outcome: AuditOutcome) -> str | None:
    """输入:判定结果 → 输出:`catalog.products.audit_detail` 那一格(具体内容)。

    三段输出的第三段(规格 §3.4),来源**确定**、按 verdict 分道:

      · reject + L3 判的  → `l3.detail`(LLM 给的中文一句:原文片段 + 条款要点);
        **LLM 没给 detail 时不留空**,退成一句确定的话
        `违反「<类别>」(LLM 未引用原文片段)` —— 空着的后果是飞书 H 列走
        老行兜底渲染,把 `llm_alcohol` 这种规则码原样打给运营看(`_RULE_CN`
        里没有 llm_* 条目,也不该有:那是随政策名生成的);
      · reject + 规则判的 → 判死那条 hit 的 `explain_hit`(它本来就是"具体内容"
        形态,如 `商标符号(命中:XYZ®)`)。取的是 all_hits 里**第一条扣分的**
        —— 硬拒是短路的,那一条就是判死它的那条;
      · pending           → 三句固定句之一(**按停在哪一层分**,重试口径不同:
        L1 类目解不出隔天重试有意义 / L2 要等 walmart_pt_meta 补行,重刷无用 /
        L3 LLM 故障重试有意义)。此时类别列是 NULL —— 待定不是结论;
      · pass              → None(两列都空)。
    """
    if outcome.verdict == "pending":
        return {"L3": _PENDING_REASON_L3,
                "L2": _PENDING_REASON_L2}.get(outcome.stage_stopped_at,
                                              _PENDING_REASON)
    if outcome.verdict != "reject":
        return None
    l3 = outcome.l3
    if l3 is not None and getattr(l3, "verdict", None) == "reject":
        detail = (getattr(l3, "detail", None) or "").strip()
        if detail:
            return detail
        # 判拒必须说得出一句话:LLM 漏了 detail(或只回了空白)时,
        # 用它自己给的类别拼一句确定的 —— 不许把这一格留空
        policy = (getattr(l3, "policy", None) or "").strip()
        return (f"违反「{policy}」(LLM 未引用原文片段)" if policy
                else "L3 判拒但未给出具体内容(待人工复核)")
    for h in outcome.all_hits:
        if h.penalty < 0:
            return audit_reason.explain_hit(h.rule_code, h.detail)
    return None


def write_conclusion(conn, outcome: AuditOutcome,
                     marketplace: str = "US") -> None:
    """输入:连接 + 判定结果 → 输出:无(写 catalog.products 审核六列)。

    三段分列(2026-09-02 B1,规格 §3.4):`audit_status` 判定结果 /
    `audit_reason` **类别**(reject 才有,pass 与 pending 一律 NULL)/
    新列 `audit_detail` 具体内容。⚠ pending 的那三句固定句从 `audit_reason`
    挪到了 `audit_detail` —— 类别列从此只装类别枚举,不再混着中文句子。
    """
    status = _VERDICT_TO_STATUS[outcome.verdict]
    # 类别只有 reject 才有:pass 没有类别,pending 是"这一轮判不了"(判不了
    # 不是判过了,给它挂一个类别等于替它把话说死)
    reason = outcome.final_reason_category if outcome.verdict == "reject" else None
    conn.execute(_PRODUCT_SQL, {
        "status": status, "reason": reason,
        "detail": conclusion_detail(outcome), "walmart_pt": real_pt(outcome),
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
        # ⚠ 键名 `reason` **不改**:audit_history_fold 与存量 204 万行事件
        # 都按它读(语义 = 类别)。具体内容是 2026-09-02 B1 新加的 `detail` 键
        detail = {"reason": outcome.final_reason_category,
                  "detail": conclusion_detail(outcome),
                  "rule_codes": sorted({h.rule_code for h in outcome.all_hits
                                        if h.penalty < 0}) or
                                sorted({h.rule_code for h in outcome.all_hits}),
                  "audit_version": resources.AUDIT_RULES_VERSION,
                  "run_id": run_id}
    else:
        return None      # pending 是过渡态不是生死节点,不进病历
    return {"sku": outcome.asin, "event": code, "source": source,
            "detail": detail}


# ── TRO 品牌命中(2026-08-30 接线;纯函数,零 DB)────────────────────────────

#: L3 只对 R4/R5 命中词的**前 10 个**给判定(services/audit_l3.MAX_BRANDS),
#: 第 11 个之后的词永远拿不到 is_real_brand —— 那是 unjudged 的第三种成因。
_L3_BRAND_CAP_NOTE = "L3 未给该词判定(多半在前 10 词截断之外)"


def _r4_brands(outcome: AuditOutcome) -> set:
    """输入:判定结果 → 输出:L2 R4 命中的黑名单词集合(r4 键形,天然 strip+lower)。

    R4 的 `detail.matches[].brand` 装的就是自动机的键(audit_l2
    `_rule_title_desc_blacklist`:命中值即键),与 ctx.r4_source 同型;
    这里仍再 strip+lower 一道,免得哪天 detail 形状变了静默漏。
    """
    out: set = set()
    l2 = outcome.l2
    for h in (l2.hits if l2 is not None else ()):
        if h.rule_code != "title_desc_blacklist":
            continue
        for m in (h.detail or {}).get("matches") or ():
            b = str((m or {}).get("brand") or "").strip().lower()
            if b:
                out.add(b)
    return out


def tro_hits(outcome: AuditOutcome, r4_source: dict, tro_prefix: str) -> dict:
    """输入:判定结果 + R4 键→来源原文 + TRO 来源前缀 → 输出:
    `{confirmed, unjudged, sources, reason}`(四步口径,顺序不能反)。

    ① **A = L2 R4 命中词**(黑名单自动机认出来的词,已过词边界与自品牌豁免);
    ② **B = A ∩ TRO 来源词** —— 黑名单里两万余个 TRO 品牌只是名单,命中了才
       算事;B 空则整件事到此为止(绝大多数产品走的就是这条);
    ③ **C = L3 判 `is_real_brand is True` 的词**,strip+lower 归一后
       **必须与 A 取交集**:L3 回传的 brand 是 LLM 复述的字符串,而且那份
       verdict 里混着 R5(USPTO 商标)的词 —— 不取交集就会把 R5 的词当成
       R4 的 TRO 命中报上去。严格 `is True`(与 audit_l3 强制翻拒同一口径,
       字符串 "true" 不算);
    ④ confirmed = B∩C(真品牌,可展开波及);unjudged = B **减去拿到过判定的
       词**(不是 B-C):被 L3 明确判 `is_real_brand=false` 的是通用英文词,
       那是"判过了、不是品牌",既不报也不展开;剩下的三种情形才叫拿不到判定 ——
       outcome.l3 is None(L2 就判死了,压根没跑 L3)/ l3.verdict=='pending'
       (LLM 故障,verdict 列表是空的)/ 命中词落在 L3 前 10 词截断之外。
       三种分不清时统一算 unjudged,把当轮能确定的那句写进 `reason`。

    ⚠ `brand_verdicts` 读的是 **L3Result 的属性**,不是 reject hit 的 detail:
    L3 判 pass 时 hits 是空的,而品牌判定列表照样在属性上(见
    services/audit_l3.parse_l3_reply → L3Result(brand_verdicts=...))——
    "L3 看过这个词、认为它不是真品牌"恰恰是 pass 那一路才有的信息。
    (2026-09-02 B1 字段随输出三段化改名 `blacklist_brand_verdict` → `brand_verdicts`,
    口径一字未变。)
    """
    a = _r4_brands(outcome)
    if not a:
        return {"confirmed": [], "unjudged": [], "sources": {}, "reason": None}
    b = {k for k in a
         if str(r4_source.get(k) or "").strip().lower().startswith(tro_prefix)}
    if not b:
        return {"confirmed": [], "unjudged": [], "sources": {}, "reason": None}

    l3 = outcome.l3
    judged: set = set()          # L3 给过判定的词(真伪都算"判过")
    real: set = set()            # 其中 is_real_brand is True 的
    for v in (getattr(l3, "brand_verdicts", None) or ()):
        if not isinstance(v, dict):
            continue
        brand = str(v.get("brand") or "").strip().lower()
        if not brand or brand not in a:      # ⚠ 与 A 取交集:剔掉 R5 混入的词
            continue
        judged.add(brand)
        if v.get("is_real_brand") is True:
            real.add(brand)

    unjudged = b - judged
    if not unjudged:
        reason = None
    elif l3 is None:
        reason = "L2 判死,未跑 L3(无品牌判定)"
    elif getattr(l3, "verdict", None) == "pending":
        reason = "L3 LLM 故障(pending),本轮无品牌判定"
    else:
        reason = _L3_BRAND_CAP_NOTE
    return {
        "confirmed": sorted(b & real),
        "unjudged": sorted(unjudged),
        "sources": {k: r4_source.get(k) for k in sorted(b)},
        "reason": reason,
    }


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
