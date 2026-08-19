"""audit_reason_backfill — 存量「理由未留存」批量刷成旧 run 的真实命中(一次性)。

用法:
  python cli.py audit_reason_backfill --dry-run     # 只统计与分布,不写库
  python cli.py audit_reason_backfill               # 真刷
  python cli.py audit_reason_backfill -p chunk=20000

背景(所有者定稿 2026-08-19「需要批量刷存量」):mode=backfill 采用历史结论
时,runs 行的 l3_reason_category 为空就写了占位句「历史结论(阶段 X,理由
未留存)」——而 **audit.audit_hits 里躺着当年真实的命中**。_adopt_history
已改为现场反查(PR #59),本工作流负责把**已写进库的存量**同口径刷一遍。

产出双重(所有者的下一步是「看哪些拒绝类型可能是误伤的」):
  ① catalog.products.audit_reason 改写为「历史结论(阶段 X):<旧命中人话>」;
  ② 摘要给出**旧命中 rule_code 的完整分布**——这就是挑「可能误伤类型」的
     输入(如 excluded_category / title_desc_blacklist 是旧词规则,误伤高发;
     phase0_lark_blacklist_* 是人工拉黑,可信度高)。

幂等与安全:
  · 只动 audit_status='rejected' 且 audit_reason 仍是占位句的行,UPDATE 带
    同样的守卫谓词 —— 跑一半中断可直接重跑,不会覆盖任何新结论;
  · 反查用 services.audit_store.reject_reasons(最近一次判拒 run 的全部命中,
    SHORTCUT 影子行已排除,最重的排最前;L3 自由文本当最佳人话);
  · 连 hits 都没有的孤儿 run 保持原句不动,计数报出。
"""

import collections
import logging
import re

from registry import db
from services import audit_reason, audit_store

DANGEROUS = False       # 只写 catalog.products.audit_reason 一列,不碰沃尔玛

logger = logging.getLogger("workflows.audit_reason_backfill")

_PLACEHOLDER_LIKE = "历史结论(阶段%理由未留存)"
_STAGE_RE = re.compile(r"历史结论\(阶段 (.+?),理由未留存\)")
_REASON_MAX = 300       # 人话截断:desc 类 detail 偶有长文本,理由列不是档案柜

_SQL_CANDIDATES = """
SELECT asin, audit_reason FROM catalog.products
WHERE marketplace = 'US' AND audit_status = 'rejected'
  AND audit_reason LIKE %(like)s
  AND asin > %(last)s
ORDER BY asin
LIMIT %(chunk)s
"""

# 守卫谓词与候选查询一致:中断重跑/并发审核落新结论都不会被覆盖
_SQL_UPDATE = """
UPDATE catalog.products SET audit_reason = %(reason)s
WHERE marketplace = 'US' AND asin = %(asin)s
  AND audit_status = 'rejected' AND audit_reason LIKE %(like)s
"""


def _compose(old_reason: str, hits: list[tuple[str, dict]]) -> tuple[str, str]:
    """输入:占位句 + 旧命中列表 → 输出:(新理由, 记分布用的规则码)。"""
    m = _STAGE_RE.search(old_reason or "")
    stage = m.group(1) if m else "未知"
    code, detail = hits[0]
    text = (str(detail.get("note") or "").strip() if code == "l3_reason"
            else audit_reason.explain_hit(code, detail))
    text = text or code
    if len(text) > _REASON_MAX:
        text = text[:_REASON_MAX] + "…"
    return f"历史结论(阶段 {stage}):{text}", code


def run(params: dict) -> str:
    """输入:params(execute/chunk)→ 输出:改写统计 + 旧命中规则码全分布。"""
    execute = bool(params.get("execute"))
    chunk = int(params.get("chunk", 10_000))
    scanned = rewritten = orphan = 0
    dist: collections.Counter = collections.Counter()
    last = ""

    with db.pg_conn() as conn:
        while True:
            with conn.cursor() as cur:
                cur.execute(_SQL_CANDIDATES, {"like": _PLACEHOLDER_LIKE,
                                              "last": last, "chunk": chunk})
                rows = cur.fetchall()
            if not rows:
                break
            scanned += len(rows)
            asins = [a for a, _ in rows]
            reasons = audit_store.reject_reasons(conn, asins)
            updates = []
            for asin, old in rows:
                hits = reasons.get(asin)
                if not hits:
                    orphan += 1          # 孤儿 run(连 hits 都没有):原句不动
                    continue
                new_reason, code = _compose(old, hits)
                dist[code] += 1
                updates.append({"reason": new_reason, "asin": asin,
                                "like": _PLACEHOLDER_LIKE})
            if execute and updates:
                with conn.cursor() as cur:
                    cur.executemany(_SQL_UPDATE, updates)
                conn.commit()
            rewritten += len(updates)
            last = asins[-1]
            logger.info("已扫 %d(改写 %d / 孤儿 %d),游标至 %s",
                        scanned, rewritten, orphan, last)

    head = (f"{'' if execute else '[DRY-RUN] '}存量理由刷新:占位句 {scanned} 行,"
            f"{'改写' if execute else '将改写'} {rewritten} 行,"
            f"孤儿(hits 也没有,原句保留){orphan} 行")
    if not dist:
        return head
    lines = [head, "旧命中规则码分布(挑「可能误伤类型」用,按量降序):"]
    for code, n in dist.most_common():
        lines.append(f"  {n:>7}  {code}"
                     f"({audit_reason.explain_hit(code, {})})")
    return "\n".join(lines)
