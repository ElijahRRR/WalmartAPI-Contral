"""audit_calibrate — 双跑校准报告(批次 B 验收闸门;只读,非危险)。

用法:
  python cli.py audit_calibrate            # 切点自动 = product_audit 首跑时刻
  python cli.py audit_calibrate -p since=2026-08-13T08:00:00Z   # 显式覆盖

口径(spec_vectors §4.3,零 LLM 批次的唯一公平比法):
  新侧 = since 之后本系统写的 audit_runs(每 ASIN 取最新一条);
  旧侧 = since 之前的历史 runs(SHORTCUT 影子行排除 + reject 粘性),
  但**只比旧系统 L0+L2 的中间判决**,不比旧最终 verdict——旧最终含 L3/L4
  两层 LLM,批次 B 没有,直接比必然大面积"漏拦"假差异:
    旧 stage_stopped_at ∈ ('L0','L2') → 旧中间判决 = reject
    其余(NULL 全过 / L3 / L4 拒)     → 旧中间判决 = pass(L0+L2 层放行)

报告四桶(不报一个总数——总数掩盖方向):
  一致 / 新拦旧放 / 旧拦新放 / 新 pending(PT 解不出,不计入一致率分母);
  分歧桶按 rule_code 下钻 top10,直接对应移植规格向量表定位语义漂移。
分母明细显式打印(spec §4.1 分母陷阱:被剔除的必须可见)。
"""

import logging

from registry import db

DANGEROUS = False

logger = logging.getLogger("workflows.audit_calibrate")

_PAIR_SQL = """
WITH new_runs AS (
    SELECT DISTINCT ON (asin) asin, run_id, verdict, stage_stopped_at,
           walmart_product_type, l3_verdict
    FROM audit.audit_runs
    WHERE created_at >= %(since)s
    ORDER BY asin, created_at DESC
), old_runs AS (
    SELECT DISTINCT ON (asin) asin, run_id, verdict, stage_stopped_at,
           walmart_product_type, l3_verdict
    FROM audit.audit_runs
    WHERE created_at < %(since)s
      AND verdict IN ('pass', 'reject')
      AND stage_stopped_at IS DISTINCT FROM 'SHORTCUT'
    ORDER BY asin, (verdict = 'reject') DESC, created_at DESC
)
SELECT n.asin, n.run_id AS new_run, n.verdict AS new_verdict,
       n.stage_stopped_at AS new_stage,
       n.walmart_product_type AS new_pt, n.l3_verdict AS new_l3,
       o.run_id AS old_run, o.verdict AS old_verdict,
       o.stage_stopped_at AS old_stage,
       o.walmart_product_type AS old_pt, o.l3_verdict AS old_l3
FROM new_runs n LEFT JOIN old_runs o USING (asin)
"""

_STUB_PTS = {"", "unknown", "(phase0_blocked)"}   # PT 一致率不比占位值

_HITS_SQL = """
SELECT run_id, array_agg(DISTINCT rule_code) FROM audit.audit_hits
WHERE run_id = ANY(%s) GROUP BY run_id
"""


def _default_since(conn) -> str | None:
    """输入:连接 → 输出:新旧分界 = 本系统 product_audit 首跑时刻(ISO 串)。

    不写死时间(首版硬编码 16:00Z 在 UTC+8 生产机上把当天新侧整批切没了,
    2026-08-13 实测事故):cli 每次运行都落 ops.runs,product_audit 的
    min(started_at) 必然早于它写的第一条 audit_runs、晚于批次 A 搬入的全部
    历史行(搬入保留旧库原始 created_at),天然就是无歧义分界。
    从未跑过 → None(报告直接说明,不瞎比)。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT min(started_at) FROM ops.runs "
                    "WHERE workflow = 'product_audit'")
        (t,) = cur.fetchone()
    return t.isoformat() if t else None


def old_intermediate(stage: str | None, verdict: str) -> str:
    """输入:run 的 stage_stopped_at + verdict → 输出:L0+L2 硬规则层中间判决。

    纯函数(便于测试)。L3/L4 拒 = 硬规则层放行后被 LLM 层拦,算 pass;
    NULL = 全流程通过。批次 C 起**两侧对称套用**(新侧也有 L3 了:新 L3 拒
    若按最终判决计,会把"旧 L3 拒/新 L3 拒"的一致案例误记成新拦旧放)。
    """
    if verdict == "reject" and stage in ("L0", "L2"):
        return "reject"
    return "pass"


def _top_rules(conn, run_ids: list[int], cap: int = 10) -> list[str]:
    if not run_ids:
        return []
    freq: dict = {}
    with conn.cursor() as cur:
        cur.execute(_HITS_SQL, (run_ids,))
        for _rid, codes in cur.fetchall():
            for c in codes:
                freq[c] = freq.get(c, 0) + 1
    return [f"{c}×{n}" for c, n in
            sorted(freq.items(), key=lambda kv: -kv[1])[:cap]]


def run(params: dict) -> str:
    """输入:params(可选 since=ISO 时间)→ 输出:四桶一致率报告。"""
    since = str(params.get("since", "")).strip() or None

    with db.pg_conn() as conn:
        if since is None:
            since = _default_since(conn)
            if since is None:
                return ("audit_calibrate:ops.runs 里没有 product_audit 运行"
                        "记录,新侧为空无从校准——先跑 product_audit")
        with conn.cursor() as cur:
            cur.execute(_PAIR_SQL, {"since": since})
            rows = cur.fetchall()

        agree = new_only = old_only = 0
        new_pending, no_old = 0, 0
        pt_pairs = pt_agree = 0
        l3_pairs = l3_agree = l3_new_only = l3_old_only = 0
        bucket_new_only, bucket_old_only = [], []
        for (_asin, new_run, new_v, new_stage, new_pt, new_l3,
             old_run, old_v, old_stage, old_pt, old_l3) in rows:
            if new_v == "pending" and new_stage != "L3":
                new_pending += 1     # PT 解不出(L1);L3 pending=硬层已放行,照比
                continue
            if old_run is None:
                no_old += 1          # 旧系统没审过,无从比对(分母外,必须亮出)
                continue
            # 硬规则层:两侧对称取 L0+L2 中间判决(批次 C 口径)
            new_mid = old_intermediate(new_stage, new_v)
            old_mid = old_intermediate(old_stage, old_v)
            if new_mid == old_mid:
                agree += 1
            elif new_mid == "reject":
                new_only += 1
                bucket_new_only.append(new_run)
            else:
                old_only += 1
                bucket_old_only.append(old_run)
            # L1 层:PT 字符串等值(占位值不计入;计划 C 验收"L1 结论一致率")
            if (new_pt and old_pt and new_pt not in _STUB_PTS
                    and old_pt not in _STUB_PTS):
                pt_pairs += 1
                pt_agree += int(new_pt == old_pt)
            # L3 层:两侧都真跑到 L3 的才可比
            if new_l3 in ("pass", "reject") and old_l3 in ("pass", "reject"):
                l3_pairs += 1
                if new_l3 == old_l3:
                    l3_agree += 1
                elif new_l3 == "reject":
                    l3_new_only += 1
                else:
                    l3_old_only += 1

        top_new = _top_rules(conn, bucket_new_only)
        top_old = _top_rules(conn, bucket_old_only)

    compared = agree + new_only + old_only
    rate = f"{agree / compared * 100:.1f}%" if compared else "n/a"
    lines = [
        f"audit_calibrate(切点 {since}):新侧 runs {len(rows)}",
        f"分母明细:可比 {compared} / 新 pending(L1 类目解不出) {new_pending}"
        f"(不计入) / 旧系统未审过 {no_old}(不计入)",
        f"硬规则层一致率:{rate}(一致 {agree};两侧对称按 L0+L2 中间判决)",
        f"分歧|新拦旧放 {new_only}" + (f":{'; '.join(top_new)}" if top_new else ""),
        f"分歧|旧拦新放 {old_only}" + (f":{'; '.join(top_old)}" if top_old else ""),
    ]
    if pt_pairs:
        lines.append(f"L1 PT 一致率:{pt_agree / pt_pairs * 100:.1f}%"
                     f"({pt_agree}/{pt_pairs},两侧均真 PT 才计)")
    if l3_pairs:
        lines.append(f"L3 一致率:{l3_agree / l3_pairs * 100:.1f}%"
                     f"({l3_agree}/{l3_pairs};新拦旧放 {l3_new_only}/"
                     f"旧拦新放 {l3_old_only};两侧均跑到 L3 才计)")
    if old_only:
        lines.append("(旧拦新放需逐条核:大概率是 PT 来源不同——旧 L1 LLM 判的"
                     " PT vs 新实证/映射 PT——先看 rule_code 是不是 R0/R1/R2/R3)")
    return "\n".join(lines)
