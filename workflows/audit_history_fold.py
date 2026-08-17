"""audit_history_fold — 历史审核结论折叠进产品事件账本(一次性)。

用法:
  python cli.py audit_history_fold              # 预览:折叠体量/时间范围
  python cli.py audit_history_fold -p apply=1   # 真正入库(擦净重灌)

所有者需求 2026-08-13:病历(product_events)里要一眼看到历史审核结论。
手法沿用 cleanup_history_import 先例(48.5 万行折 23.9 万条变迁):不逐
run 平铺(204 万行进账本 = 噪声淹没病历),只投**每 ASIN 的结论变迁点**
——按时间排序,verdict 与前一条不同才落一条事件,occurred_at 带旧库
原始时间戳。

口径:
  · 源 = audit.audit_runs 中 created_at 早于本系统 product_audit 首跑
    (ops.runs 取 min(started_at),与 audit_calibrate 同一分界)的历史行
    ——新系统 runs 在真跑时已实时写事件,折叠再收会双记;
  · SHORTCUT 影子行排除(旧系统 reject 粘性产物,非真实判定);
  · pending 不折(过渡态不进病历,与实时链 event_row 同口径);
  · 事件码复用 audit_passed / audit_rejected(登记制),source 区分来源;
    detail 带 run_id/stage/reason,可追溯回 audit_runs 明细。

幂等 = 擦净重灌(cleanup_history_import 同款,"账本只追加"的同一例外:
历史折叠是旧账的投影,投影可以重投):apply 先删本 source 的全部事件再
重导,别的 source(product_audit / audit_backfill 等)一根手指都不碰。

写库是 INSERT…SELECT 单语句(折叠在 SQL 窗口函数里完成,十几万行不过
Python;事件码经 product_events 常量传参而非字面量,登记纪律不破)。
"""

import logging

from registry import db
from services import product_events

DANGEROUS = False

logger = logging.getLogger("workflows.audit_history_fold")

SOURCE = "audit_history_fold"

_CUTOFF_SQL = """
SELECT min(started_at) FROM ops.runs WHERE workflow = 'product_audit'
"""

# 折叠源(两条 SQL 共用口径;cutoff 为 NULL = 本系统从未跑过审核,
# 全部历史行都可折,coalesce 到 infinity)
_SRC = """
    SELECT asin, run_id, verdict, stage_stopped_at, l3_reason_category,
           created_at,
           lag(verdict) OVER (PARTITION BY asin
                              ORDER BY created_at, run_id) AS prev
    FROM audit.audit_runs
    WHERE verdict IN ('pass', 'reject')
      AND stage_stopped_at IS DISTINCT FROM 'SHORTCUT'
      AND created_at < coalesce(%(cutoff)s, 'infinity'::timestamptz)
"""

_PREVIEW_SQL = f"""
WITH src AS ({_SRC})
SELECT count(*)                                        AS scanned,
       count(DISTINCT asin)                            AS asins,
       count(*) FILTER (WHERE verdict IS DISTINCT FROM prev) AS folded,
       count(*) FILTER (WHERE verdict IS DISTINCT FROM prev
                          AND verdict = 'pass')        AS f_pass,
       count(*) FILTER (WHERE verdict IS DISTINCT FROM prev
                          AND verdict = 'reject')      AS f_reject,
       min(created_at)                                 AS t_min,
       max(created_at)                                 AS t_max
FROM src
"""

# sku=asin:审核域产品尚无沃尔玛订货号,实时链 event_row 同口径;
# asin 列直填(源列本就是标准 ASIN,不必等 sku_normalize 补)
_INSERT_SQL = f"""
INSERT INTO catalog.product_events
    (sku, asin, store, event, source, detail, occurred_at)
SELECT asin, asin, NULL,
       CASE verdict WHEN 'pass' THEN %(ev_pass)s ELSE %(ev_reject)s END,
       %(source)s,
       jsonb_strip_nulls(jsonb_build_object(
           'run_id', run_id,
           'stage', stage_stopped_at,
           'reason', l3_reason_category)),
       created_at
FROM ({_SRC}) src
WHERE verdict IS DISTINCT FROM prev
"""


def run(params: dict) -> str:
    """输入:params(apply=1 才写库)→ 输出:折叠体检/导入结果摘要。"""
    apply = str(params.get("apply", "")).strip() == "1"

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_CUTOFF_SQL)
            (cutoff,) = cur.fetchone()
            cur.execute(_PREVIEW_SQL, {"cutoff": cutoff})
            scanned, asins, folded, f_pass, f_reject, t_min, t_max = \
                cur.fetchone()
            cur.execute("SELECT count(*) FROM catalog.product_events "
                        "WHERE source = %s", (SOURCE,))
            (existing,) = cur.fetchone()

        lines = [
            f"audit_history_fold(分界 {cutoff.isoformat() if cutoff else '无(product_audit 从未跑过)'}):",
            f"历史 runs {scanned}(ASIN {asins})→ 折叠为变迁事件 {folded}"
            f"(过 {f_pass}/拒 {f_reject}),压缩比 {scanned / folded:.1f}:1"
            if folded else "历史 runs 0 —— 没有可折叠的行",
        ]
        if folded and t_min:
            lines.append(f"时间范围 {t_min.date()} ~ {t_max.date()}"
                         f"(occurred_at 带原始时间戳)")
        if existing:
            lines.append(f"账本内已有本 source 事件 {existing} 条"
                         f"(apply 时擦净重灌)")
        if not apply:
            lines.append("(预览:未写库;确认无误加 -p apply=1)")
            return "\n".join(lines)
        if not folded:
            return "\n".join(lines)

        with conn.cursor() as cur:
            cur.execute("DELETE FROM catalog.product_events "
                        "WHERE source = %s", (SOURCE,))
            deleted = cur.rowcount
            cur.execute(_INSERT_SQL, {
                "cutoff": cutoff,
                "ev_pass": product_events.AUDIT_PASSED,
                "ev_reject": product_events.AUDIT_REJECTED,
                "source": SOURCE,
            })
            inserted = cur.rowcount
    lines.append(f"擦净 {deleted} 条 → 重灌 {inserted} 条 ✓")
    return "\n".join(lines)
