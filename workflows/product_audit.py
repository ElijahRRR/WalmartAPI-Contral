"""product_audit — 产品审核主流程(批次 C:全链含 LLM 层;危险,默认 dry-run)。

用法:
  python cli.py product_audit                       # dry-run:判定 + 落 runs/hits,不写结论
  python cli.py product_audit --execute             # 真跑:另写 products 五列 + 审核事件
  python cli.py product_audit -p limit=2000
  python cli.py product_audit -p asins=B0A,B0B --execute   # 指定 ASIN(无视现有结论强审)
  python cli.py product_audit -p mode=backfill --execute   # 补刷:只审无结论,历史结论直接采用
  python cli.py product_audit -p r5=on                     # 开 USPTO 商标反查(默认关)
  python cli.py product_audit -p l3=off                    # 关 L3 语义层(省 LLM 配额)
  python cli.py product_audit -p l4=on                     # 开 L4 视觉(默认关,批复 #2)

链路(批次 C 全链):领 catalog.products 待审行 → Phase0 四件套 →
L1(实证→报错实证→哨兵→映射表→候选+rerank)→ L2 硬规则 → [L3 语义 →
L4 视觉] → 37 政策理由映射 → 落 audit.audit_runs/audit_hits;--execute 才写
products.audit_* 五列与审核事件。**只落库不投影飞书**(并跑期纪律,
E/D 列投影在批次 D 切换日开闸)。

dry-run 语义(计划 B4 定稿):判定照跑、runs/hits 照落,但不碰 products
五列、不发事件、不投影。⚠ 批次 C 起 dry-run **同样产生真实 LLM 调用与费用**
(L1 rerank / L3;L4 需显式 l4=on)——验收抽样时用 limit 控制成本。

补刷(mode=backfill,批复 #5"只补刷"):候选限 audit_status IS NULL;先查
audit.audit_runs 历史结论(谓词必须 stage_stopped_at IS DISTINCT FROM
'SHORTCUT'——204 万存量里有短路影子行;排序键 (verdict='reject') DESC
实现旧 reject 粘性),有历史者**直接采用**写五列+事件(detail 带
referenced_run_id,不写新 run——方案 A,不制造影子行),无历史者进正常判定。

pending 两来源(reason 区分):L1=类目解不出(候选/rerank 均无解);
L3=LLM 故障(10.2 单链:重试尽→pending 绝不默认放行)。均按每日退避重试。
无标题产品跳过不审(采集降级,不够格判定;amz_source:103 先例)并计数。
seller 闸依赖 snapshots.buybox->>'buybox_seller_id'(契约外字段,可能恒缺)
——缺失计数在摘要亮出,恒缺说明卖家闸未生效,需向采集侧提契约扩展。

R5(USPTO)默认关:spec_l2 §5.6f——brand_nice_class 覆盖率仅 ~2.6 万/1400 万,
先离线抽样出数据再决定常开;开时全程复用一个只读连接。
"""

import logging

from registry import db, resources
from services import audit_reason, audit_rules, audit_store, product_events

DANGEROUS = True

logger = logging.getLogger("workflows.product_audit")

_CANDIDATE_SQL = """
SELECT p.asin,
       p.title,
       p.brand,
       p.walmart_pt,
       p.pt_source,
       p.browse_node_id,
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
WHERE p.marketplace = %(marketplace)s AND ({where}){recent_guard}
  AND p.title IS NOT NULL AND p.title <> ''
ORDER BY p.audited_at NULLS FIRST, p.updated_at
LIMIT %(limit)s
"""
# ↑ title 过滤挡两类:采集降级空标题行,以及 pt_backfill 的占位行(只有
#   asin+walmart_pt)。占位行若进候选,循环级跳过会让同一批空壳行每轮
#   霸占 LIMIT 名额 → 真候选饿死。注:asins= 点名的空壳行也被过滤,
#   会体现在"库中命中 N"的缺口提示里(空壳行没有可审内容,过滤是对的)

# dry-run 复烧护栏(评审 P1-1):dry-run 不动 audited_at,同一批候选会被
# 连续 dry-run 反复领走——L1 rerank/L4 不缓存,每轮全额重付 LLM 费用。
# runs 是 dry-run 也落的,拿它做 24h 排除;asins= 强审除外(点名就要审)
_RECENT_RUN_GUARD = """
 AND NOT EXISTS (
    SELECT 1 FROM audit.audit_runs r
    WHERE r.asin = p.asin AND r.created_at > now() - interval '24 hours')"""
# 排序契约:从未审过的(audited_at NULL)永远先于重试的 pending——
# 否则 pending 存量 ≥ limit 时新入库产品会被饿死(评审 P1-3)

# 历史结论(补刷用):SHORTCUT 排除 + reject 粘性排序键——
# (verdict='reject') DESC 把旧 history_shortcut 的"reject 查询先跑"压成一个
# 排序键,语义等价(spec_shortcut §3.4C),别当成可随手删的排序
_HISTORY_SQL = """
SELECT DISTINCT ON (asin) asin, run_id, verdict, score_final,
       walmart_product_type, l3_reason_category, stage_stopped_at, created_at,
       pt_source
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
    pt_source = CASE WHEN %(pt)s IS NULL THEN pt_source ELSE %(pt_source)s END,
    audited_at = now(), audit_version = %(version)s
WHERE marketplace = %(marketplace)s AND asin = %(asin)s
"""


_KNOWN_PARAMS = {"execute", "asins", "limit", "mode", "r5", "force_rerun",
                 "l3", "l4", "workers"}


def _pick_where(params: dict) -> tuple[str, dict]:
    unknown = set(params) - _KNOWN_PARAMS
    if unknown:
        # 静默吞参数 = "全量重审跑完了"的假象(评审 P1-4),宁炸不吞
        raise ValueError(f"未识别参数 {sorted(unknown)}(可用:{sorted(_KNOWN_PARAMS)})")
    asins = [a.strip() for a in str(params.get("asins", "")).split(",")
             if a.strip()]
    if asins:
        # 指定 ASIN = 无视现有结论强审(与旧仓 force_rerun 不同:这里没有
        # 运行时短路可绕,绕的是 audit_status 候选谓词)
        return "p.asin = ANY(%(asins)s)", {"asins": asins}
    fr = str(params.get("force_rerun", "")).strip()
    if fr:
        # 按版本批量重审(B7):audit_version 不等于目标版本的全部重审,
        # 含已 approved/rejected 的存量
        return "p.audit_version IS DISTINCT FROM %(force_rerun)s", \
            {"force_rerun": fr}
    if str(params.get("mode", "")).strip() == "backfill":
        return "p.audit_status IS NULL", {}
    # 默认:新品 + pending 重试(退避 1 天:批次 B 的 pending 多为 PT 解不出,
    # 每小时重判只会无界追加 audit_runs,评审 P1-3)
    return ("(p.audit_status IS NULL OR (p.audit_status = 'pending' "
            "AND (p.audited_at IS NULL OR p.audited_at < now() - interval '1 day')))"), {}


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
    for (asin, run_id, verdict, _score, pt, reason_cat, stage, created,
         src) in rows:
        adopted.add(asin)
        if not execute:
            continue
        status = "approved" if verdict == "pass" else "rejected"
        if verdict == "reject":
            # 存量大头是 L0/L2 拒,l3_reason_category 本就 NULL——不留空
            # (rejected 说不出理由 = 排查断线),也不迁旧'history_shortcut'字面量
            reason = reason_cat or f"历史结论(阶段 {stage or '未知'},理由未留存)"
        else:
            reason = None
        conn.execute(_ADOPT_SQL, {
            "status": status,
            "reason": reason,
            "pt": (pt if pt and not pt.startswith("(") else None),
            # 旧结论的 PT 来源照搬 runs 记录;非实证一律记 audit_llm
            # (来历不明的 PT 不当实证——它会被 catmap_mine 投票放大)
            "pt_source": ("walmart_confirmed"
                          if src in ("walmart_confirmed",
                                     "historical_confirmed")
                          else "audit_llm"),
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
    # L3 默认开(旧仓 run_l3 默认 True);L4 默认关(批复 #2,显式 l4=on)
    run_l3 = str(params.get("l3", "")).strip().lower() != "off"
    run_l4 = str(params.get("l4", "")).strip().lower() == "on"
    # 判定并发(旧仓 10 worker 常驻先例):worker 只做判定(LLM+只读+幂等
    # 缓存写,各自 autocommit 连接),落库仍归主线程单连接(savepoint 语义
    # 不变)。r5=on 强制 1(uspto 单连接不可跨线程)
    workers = max(1, min(int(params.get("workers", 4)), 16))
    if r5_on:
        workers = 1
    where, extra = _pick_where(params)
    if "asins" in extra:
        # 指定 ASIN 时 limit 不许截断(评审 I-6:传 600 只审 500 且无提示)
        limit = max(limit, len(extra["asins"]))

    import contextlib
    uspto_cm = db.uspto_conn() if r5_on else contextlib.nullcontext()
    with db.pg_conn() as conn, uspto_cm as uspto:
        ctx = audit_rules.load_context(conn, uspto=uspto)
        query_params = {"marketplace": "US", "limit": limit, **extra}
        # 复烧护栏只在 dry-run 生效:execute 写 audited_at 天然推进;
        # dry-run 后紧跟的 --execute 也不能被自己刚落的 runs 拦掉
        guard = (_RECENT_RUN_GUARD
                 if (not execute and "asins" not in extra) else "")
        with conn.cursor() as cur:
            cur.execute(_CANDIDATE_SQL.format(where=where,
                                              recent_guard=guard),
                        query_params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        adopted_n, adopted = (0, set())
        if backfill:
            adopted_n, adopted = _adopt_history(
                conn, [r["asin"] for r in rows], execute)

        counts = {"pass": 0, "reject": 0, "pending": 0}
        no_title = seller_missing = policy_unknown = 0
        stage_stats = {"L3_ran": 0, "L3_reject": 0, "L3_pending": 0,
                       "L4_ran": 0, "L4_reject": 0}
        l4_fail: dict = {}           # rule_code → 次数(评审 P1-2:层死≠层净)
        audit_rules.audit_l1_llm.reset_stats()   # 本轮 rerank 计数从零起
        events = []
        row_errors, consec_errors = 0, 0
        todo = []
        for row in rows:
            if row["asin"] in adopted:
                continue
            if not row.get("title"):
                no_title += 1        # 采集降级无标题:不够格判定,跳过不写结论
                continue
            if not row.get("seller_id"):
                seller_missing += 1  # 卖家闸未生效面(契约外字段,摘要必亮)
            todo.append((row["asin"], audit_rules.product_info_from_row(row)))

        # 判定并发:worker 各领一条 autocommit 连接跑 audit_one(LLM 秒级,
        # 是墙钟大头);结果按完成序回主线程,落库/计数全在主线程单连接上
        # (savepoint 语义与串行版完全一致)。连错 ≥5 = 系统性故障,炸停
        import queue as _queue
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from contextlib import ExitStack

        with ExitStack() as stack:
            pool: _queue.SimpleQueue = _queue.SimpleQueue()
            for _ in range(workers):
                pool.put(stack.enter_context(db.pg_conn(autocommit=True)))

            def _judge(product):
                c = pool.get()
                try:
                    return audit_rules.audit_one(product, ctx, c,
                                                 run_l3=run_l3, run_l4=run_l4)
                finally:
                    pool.put(c)

            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_judge, p): asin for asin, p in todo}
                for fut in as_completed(futs):
                    asin = futs[fut]
                    try:
                        outcome = fut.result()
                        # 每行 savepoint(评审 P2):一行落库报错不炸整批
                        # 已付费的 runs/hits
                        with conn.transaction():
                            run_id = audit_store.persist_run(conn, outcome)
                            if execute:
                                audit_store.write_conclusion(conn, outcome)
                                ev = audit_store.event_row(outcome, run_id)
                                if ev:
                                    events.append(ev)
                    except Exception as e:  # noqa: BLE001 —— 单行隔离,计数亮出
                        row_errors += 1
                        consec_errors += 1
                        logger.error("单行审核失败 asin=%s:%s", asin, e)
                        if consec_errors >= 5:
                            for f in futs:
                                f.cancel()
                            raise RuntimeError(
                                f"连续 {consec_errors} 行失败(共 {row_errors}),"
                                f"疑似系统性故障,停批。最后错误:{e}") from e
                        continue
                    consec_errors = 0
                    if outcome.l3 is not None:
                        stage_stats["L3_ran"] += 1
                        if outcome.l3.verdict == "reject":
                            stage_stats["L3_reject"] += 1
                        elif outcome.l3.verdict == "pending":
                            stage_stats["L3_pending"] += 1
                    if outcome.l4 is not None:
                        stage_stats["L4_ran"] += 1
                        if outcome.l4.verdict == "reject":
                            stage_stats["L4_reject"] += 1
                        for h in outcome.l4.hits:
                            if h.penalty == 0 and h.rule_code.startswith("l4_"):
                                l4_fail[h.rule_code] = \
                                    l4_fail.get(h.rule_code, 0) + 1
                    counts[outcome.verdict] += 1
                    if (outcome.verdict == "reject" and ctx.known_policies
                            and not audit_reason.known_policies_check(
                                outcome.final_reason_category,
                                ctx.known_policies)):
                        policy_unknown += 1
        if execute and events:
            product_events.record_many(conn, events)

        # pending 可见性(一致性审查 3.5):只报总量——audited_at 是"审核动作
        # 时刻"不是"进入 pending 时刻",拿它算龄期两种来源口径相反(评审 P1-3/
        # I-3);诚实的龄期需要 pending_since 列,批次 C 随 L1 一并定
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM catalog.products "
                        "WHERE marketplace = 'US' AND audit_status = 'pending'")
            (pending_total,) = cur.fetchone()

    judged = sum(counts.values())
    lines = [f"product_audit({resources.AUDIT_RULES_VERSION}"
             f"{',补刷' if backfill else ''}{',R5开' if r5_on else ''}"
             f"{',L3关' if not run_l3 else ''}{',L4开' if run_l4 else ''}):"
             f"候选 {len(rows)},判定 {judged}"
             f"(过 {counts['pass']}/拒 {counts['reject']}/待定 {counts['pending']})"]
    l1s = audit_rules.audit_l1_llm.STATS
    if l1s.get("llm_called", 0) or l1s.get("no_candidate", 0):
        lines.append(f"L1 rerank:调用 {l1s['llm_called']}"
                     f"(失败 {l1s.get('llm_failed', 0)}/坏 JSON {l1s.get('bad_json', 0)}),"
                     f"unknown→待定 {l1s.get('unknown', 0)},"
                     f"字典回落 {l1s.get('dict_fallback', 0)},"
                     f"无候选→待定 {l1s.get('no_candidate', 0)},"
                     f"低置信采纳 {l1s.get('conf_low', 0)}")
        # 候选路归因:哪一路把最终 PT 送进来的(新加的祖先/字典两路是否有用)
        picked = {k[len("picked_"):]: v for k, v in l1s.items()
                  if k.startswith("picked_") and v}
        if picked:
            lines.append("  选中候选来自:" + " / ".join(
                f"{k} {v}" for k, v in sorted(picked.items(),
                                              key=lambda kv: -kv[1])))
        opened = {k: v for k, v in l1s.items()
                  if k.startswith("open_") and v}
        if opened:
            lines.append("  零参考两阶段:" + " / ".join(
                f"{k} {v}" for k, v in sorted(opened.items())))
    l1_blocked = (l1s.get("seed_excluded_direct", 0)
                  + l1s.get("llm_excluded", 0) + l1s.get("seed_excluded", 0)
                  + l1s.get("publication_forbidden", 0))
    if l1_blocked:
        lines.append(f"L1 硬拦:直出级 seed {l1s.get('seed_excluded_direct', 0)}"
                     f" / rerank 级 excluded {l1s.get('llm_excluded', 0)}"
                     f"(seed 补位 {l1s.get('seed_excluded', 0)})"
                     f" / 出版物 {l1s.get('publication_forbidden', 0)}")
    if stage_stats["L3_ran"]:
        lines.append(f"L3 语义:判 {stage_stats['L3_ran']}"
                     f"(拒 {stage_stats['L3_reject']}/"
                     f"LLM 故障待定 {stage_stats['L3_pending']})")
    if stage_stats["L4_ran"]:
        lines.append(f"L4 视觉:判 {stage_stats['L4_ran']}"
                     f"(拒 {stage_stats['L4_reject']})")
    if l4_fail:
        # 层死与层净必须长得不一样(评审 P1-2):故障回落 pass 逐码亮出
        detail = ", ".join(f"{k}×{v}" for k, v in sorted(l4_fail.items()))
        lines.append(f"⚠ L4 故障回落 pass:{detail}"
                     f"(全故障=层未生效,先查 ARK_API_KEY/取图)")
    if row_errors:
        lines.append(f"⚠ 单行失败跳过 {row_errors}(savepoint 隔离,详见日志)")
    if "asins" in extra and len(rows) < len(extra["asins"]):
        lines.append(f"⚠ 指定 ASIN {len(extra['asins'])} 个,库中命中 {len(rows)}"
                     f"——缺的 {len(extra['asins']) - len(rows)} 个不在 catalog.products")
    if adopted_n:
        lines.append(f"历史结论采用 {adopted_n}(不写新 run,detail 指回原 run_id)")
    if no_title:
        lines.append(f"无标题跳过 {no_title}(采集降级,不够格判定)")
    if seller_missing:
        lines.append(f"⚠ 卖家字段缺失 {seller_missing}/{judged}"
                     f"(buybox_seller_id 契约外字段;恒缺=卖家闸未生效,需契约扩展)")
    if policy_unknown:
        lines.append(f"⚠ 理由映射落 37 政策外 {policy_unknown} 条(详见日志,只记不改判)")
    if r5_on and getattr(ctx, "uspto_failures", 0):
        lines.append(f"⚠ R5 查询失败 {ctx.uspto_failures} 次"
                     f"{'(≥5 已自动关停本轮 R5)' if ctx.uspto is None else ''}")
    lines.append(f"全库 pending 存量 {pending_total}")
    if not execute:
        lines.append("(dry-run:runs/hits 已落,products 五列与事件未写)")
    return "\n".join(lines)
