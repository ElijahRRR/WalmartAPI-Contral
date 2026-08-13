"""product_audit — 产品审核主流程(批次 B:零 LLM 纯规则层;危险,默认 dry-run)。

用法:
  python cli.py product_audit                       # dry-run:判定 + 落 runs/hits,不写结论
  python cli.py product_audit --execute             # 真跑:另写 products 五列 + 审核事件
  python cli.py product_audit -p limit=2000
  python cli.py product_audit -p asins=B0A,B0B --execute   # 指定 ASIN(无视现有结论强审)
  python cli.py product_audit -p mode=backfill --execute   # 补刷:只审无结论,历史结论直接采用
  python cli.py product_audit -p r5=on                     # 开 USPTO 商标反查(默认关)

链路:领 catalog.products 待审行(audit_status IS NULL / 'pending')→
Phase0 四件套 → PT 解析(实证→映射表)→ L2 硬规则 → 37 政策理由映射 →
落 audit.audit_runs/audit_hits;--execute 才写 products.audit_* 五列与
audit_passed/audit_rejected 事件。**批次 B 只落库不投影飞书**(并跑期纪律,
E/D 列投影在批次 D 切换日开闸;上架链此时仍按旧口径走,库内结论仅供校准)。

dry-run 语义(计划 B4 定稿):判定照跑、runs/hits 照落(它们是只追加的
明细账,dry-run 的判定同样是真判定),但**不碰 products 五列、不发事件、
不投影**——消费端(未来的 list_new 查库)看不到任何变化。

补刷(mode=backfill,批复 #5"只补刷"):候选限 audit_status IS NULL;先查
audit.audit_runs 历史结论(谓词必须 stage_stopped_at IS DISTINCT FROM
'SHORTCUT'——204 万存量里有短路影子行;排序键 (verdict='reject') DESC
实现旧 reject 粘性),有历史者**直接采用**写五列+事件(detail 带
referenced_run_id,不写新 run——方案 A,不制造影子行),无历史者进正常判定。

PT 解不出 → pending(reason=待类目判定;批次 C 接 L1 后自动重审)——
批次 B 的自定义保守行为,旧仓此处有 L1 LLM 保底。
无标题产品跳过不审(采集降级,不够格判定;amz_source:103 先例)并计数。
seller 闸依赖 snapshots.buybox->>'buybox_seller_id'(契约外字段,可能恒缺)
——缺失计数在摘要亮出,恒缺说明卖家闸未生效,需向采集侧提契约扩展。

R5(USPTO)默认关:spec_l2 §5.6f——brand_nice_class 覆盖率仅 ~2.6 万/1400 万,
先离线抽样出数据再决定常开;开时全程复用一个只读连接。
"""

import logging
from datetime import datetime, timezone

from registry import db, resources
from services import audit_reason, audit_rules, audit_store, product_events

DANGEROUS = True

logger = logging.getLogger("workflows.product_audit")

_CANDIDATE_SQL = """
SELECT p.asin,
       p.title,
       p.brand,
       p.amazon_category AS amazon_category_path,
       p.slow -> 'bullet_points' AS bullet_points,
       coalesce(p.slow ->> 'description',
                p.slow ->> 'long_description',
                p.slow ->> 'product_description') AS long_description,
       sn.buybox ->> 'buybox_seller_id' AS seller_id,
       sn.buybox ->> 'buybox_seller'    AS seller_name
FROM catalog.products p
LEFT JOIN LATERAL (
    SELECT s.buybox FROM catalog.snapshots s
    WHERE s.marketplace = p.marketplace AND s.asin = p.asin
      AND s.outcome = 'ok'
    ORDER BY s.scraped_at DESC LIMIT 1
) sn ON true
WHERE p.marketplace = %(marketplace)s AND ({where})
ORDER BY p.updated_at
LIMIT %(limit)s
"""

# 历史结论(补刷用):SHORTCUT 排除 + reject 粘性排序键——
# (verdict='reject') DESC 把旧 history_shortcut 的"reject 查询先跑"压成一个
# 排序键,语义等价(spec_shortcut §3.4C),别当成可随手删的排序
_HISTORY_SQL = """
SELECT DISTINCT ON (asin) asin, run_id, verdict, score_final,
       walmart_product_type, l3_reason_category, created_at
FROM audit.audit_runs
WHERE asin = ANY(%s)
  AND verdict IN ('reject', 'pass')
  AND stage_stopped_at IS DISTINCT FROM 'SHORTCUT'
ORDER BY asin, (verdict = 'reject') DESC, created_at DESC
"""

_ADOPT_SQL = """
UPDATE catalog.products
SET audit_status = %(status)s, audit_reason = %(reason)s,
    walmart_pt = COALESCE(%(pt)s, walmart_pt),
    audited_at = now(), audit_version = %(version)s
WHERE marketplace = %(marketplace)s AND asin = %(asin)s
"""


def _pick_where(params: dict) -> tuple[str, dict]:
    asins = [a.strip() for a in str(params.get("asins", "")).split(",")
             if a.strip()]
    if asins:
        # 指定 ASIN = 无视现有结论强审(force_rerun 语义:与旧仓不同,
        # 这里没有运行时短路可绕,绕的是 audit_status 候选谓词)
        return "p.asin = ANY(%(asins)s)", {"asins": asins}
    if str(params.get("mode", "")).strip() == "backfill":
        return "p.audit_status IS NULL", {}
    return "(p.audit_status IS NULL OR p.audit_status = 'pending')", {}


def _adopt_history(conn, asins: list[str], execute: bool) -> tuple[int, set]:
    """输入:候选 ASIN 列表 → 输出:(采用数, 已采用 ASIN 集)。

    方案 A(spec_shortcut §1.6,待所有者追认):历史结论直接写五列+事件,
    不写新 run。读库失败让异常冒泡整轮停——静默按"无历史"重审会把
    rejected 产品翻出来(spec_shortcut §6.1)。
    """
    if not asins:
        return 0, set()
    with conn.cursor() as cur:
        cur.execute(_HISTORY_SQL, (asins,))
        rows = cur.fetchall()
    adopted = set()
    events = []
    for asin, run_id, verdict, _score, pt, reason_cat, created in rows:
        adopted.add(asin)
        if not execute:
            continue
        status = "approved" if verdict == "pass" else "rejected"
        conn.execute(_ADOPT_SQL, {
            "status": status,
            "reason": reason_cat if verdict == "reject" else None,
            "pt": (pt if pt and not pt.startswith("(") else None),
            "version": resources.AUDIT_RULES_VERSION,
            "marketplace": "US", "asin": asin,
        })
        code = (product_events.AUDIT_PASSED if verdict == "pass"
                else product_events.AUDIT_REJECTED)
        events.append({"sku": asin, "event": code, "source": "audit_backfill",
                       "detail": {"referenced_run_id": run_id,
                                  "original_created_at":
                                      created.isoformat() if created else "",
                                  "audit_version":
                                      resources.AUDIT_RULES_VERSION}})
    if execute and events:
        product_events.record_many(conn, events)
    return len(adopted), adopted


def run(params: dict) -> str:
    """输入:params(asins/limit/mode/r5/execute)→ 输出:判定统计摘要。"""
    execute = bool(params.get("execute"))
    limit = int(params.get("limit", 500))
    backfill = str(params.get("mode", "")).strip() == "backfill"
    r5_on = str(params.get("r5", "")).strip().lower() == "on"

    import contextlib
    uspto_cm = db.uspto_conn() if r5_on else contextlib.nullcontext()
    with db.pg_conn() as conn, uspto_cm as uspto:
        ctx = audit_rules.load_context(conn, uspto=uspto)
        where, extra = _pick_where(params)
        query_params = {"marketplace": "US", "limit": limit, **extra}
        with conn.cursor() as cur:
            cur.execute(_CANDIDATE_SQL.format(where=where), query_params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        adopted_n, adopted = (0, set())
        if backfill:
            adopted_n, adopted = _adopt_history(
                conn, [r["asin"] for r in rows], execute)

        counts = {"pass": 0, "reject": 0, "pending": 0}
        no_title = seller_missing = policy_unknown = 0
        events = []
        for row in rows:
            if row["asin"] in adopted:
                continue
            if not row.get("title"):
                no_title += 1        # 采集降级无标题:不够格判定,跳过不写结论
                continue
            if not row.get("seller_id"):
                seller_missing += 1  # 卖家闸未生效面(契约外字段,摘要必亮)
            product = audit_rules.product_info_from_row(row)
            outcome = audit_rules.audit_one(product, ctx)
            counts[outcome.verdict] += 1
            if (outcome.verdict == "reject" and ctx.known_policies
                    and not audit_reason.known_policies_check(
                        outcome.final_reason_category, ctx.known_policies)):
                policy_unknown += 1
            run_id = audit_store.persist_run(conn, outcome)
            if execute:
                audit_store.write_conclusion(conn, outcome)
                ev = audit_store.event_row(outcome, run_id)
                if ev:
                    events.append(ev)
        if execute and events:
            product_events.record_many(conn, events)

        # pending 可见性(一致性审查 3.5):总量 + 最老龄期
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), min(audited_at) FROM catalog.products "
                        "WHERE marketplace = 'US' AND audit_status = 'pending'")
            pending_total, pending_oldest = cur.fetchone()

    judged = sum(counts.values())
    age = ""
    if pending_oldest:
        days = (datetime.now(timezone.utc) - pending_oldest).days
        age = f",最老 {days} 天"
    lines = [f"product_audit({resources.AUDIT_RULES_VERSION}"
             f"{',补刷' if backfill else ''}{',R5开' if r5_on else ''}):"
             f"候选 {len(rows)},判定 {judged}"
             f"(过 {counts['pass']}/拒 {counts['reject']}/待定 {counts['pending']})"]
    if adopted_n:
        lines.append(f"历史结论采用 {adopted_n}(不写新 run,detail 指回原 run_id)")
    if no_title:
        lines.append(f"无标题跳过 {no_title}(采集降级,不够格判定)")
    if seller_missing:
        lines.append(f"⚠ 卖家字段缺失 {seller_missing}/{judged}"
                     f"(buybox_seller_id 契约外字段;恒缺=卖家闸未生效,需契约扩展)")
    if policy_unknown:
        lines.append(f"⚠ 理由映射落 37 政策外 {policy_unknown} 条(详见日志,只记不改判)")
    lines.append(f"全库 pending 存量 {pending_total}{age}")
    if not execute:
        lines.append("(dry-run:runs/hits 已落,products 五列与事件未写)")
    return "\n".join(lines)
