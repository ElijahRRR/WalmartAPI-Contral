"""catmap_prune — 清掉映射表里指向"不存在的 PT"的行(缺省按官方 spec 判、真删)。

用法:
  python cli.py catmap_prune --dry-run          # 只列清单 ← 先跑这个
  python cli.py catmap_prune                    # **删掉**不在 spec 里的那些行
  python cli.py catmap_prune -p keep=1          # 不删,只降级为低置信 + 备注留痕
  python cli.py catmap_prune -p by=meta         # 改用准入明细当判据(旧口径)
  python cli.py catmap_prune -p sentinel=drop   # 连「无对应Walmart PT」哨兵行一起清

**判据 = 官方 MP_ITEM spec**(`<DATA_ROOT>/specs/MP_ITEM/<版本>/_pt_index.json`)。
所有者定稿 2026-08-17:「不在 spec 模版里的需要清理掉,准入漏了的需要补」。

⚠ 为什么不是 `walmart_pt_meta`(准入明细):那是飞书侧维护的一份镜像,
它**有没有收**不代表这个 PT 存不存在。两个方向都会判错:
  · spec 有·准入无(实测 9 个,全是汽配)→ 是**真 PT**,该补准入明细,**不该删映射**;
  · 准入有·spec 无(实测 66 个)→ 沃尔玛已经没有这个 PT,映射指过去是死的,**该删**。
按准入明细判会漏掉后面那 66 个 —— 所有者 2026-08-17 正是在投影表里
看到这批还在(它们没有 Walmart Category / PTG,一眼就是空的)。

⚠ **缺省真删**(所有者定稿:「有问题的直接清理掉不好吗」)。此前缺省是降级留证,
但降级的行**仍然留在映射表里**,导出到飞书还是那一堆空 Category/PTG 的行 ——
"清理"没有发生。要留证据用 `-p keep=1`。

清掉它们修的是两件事:①映射表本身干净;②同一路径不再出现两个 DISTINCT PT。
装配 L1 catmap 时两义会把**那条有效的一起丢掉** —— 生产实测因此白丢 105 条
本可直出的路径(装配侧已修:先过闸再计票;数据侧靠这条)。
"""

import logging

from registry import db
from services import pt_spec

DANGEROUS = True        # 改映射表,按纪律先 dry-run 人眼确认

logger = logging.getLogger("workflows.catmap_prune")

# 映射表里全部在用的 PT + 每个 PT 涉及多少行、同路径有没有有效兄弟。
# 判据集合在 Python 侧算(spec 是文件不是表,JOIN 不到)
# ⚠ **不在 SQL 里过滤任何东西**(2026-08-17:首版把哨兵行与 '-' 行写进
# WHERE 排除掉,于是摘要报 306 行,而所有者在投影表里数到 846 行"没有
# Walmart Category/PTG"的 —— 差的那五百多正是被我静默排除的两类)。
# 全取回来,在 Python 侧分桶并**逐桶报数**,该不该动由参数定。
_SQL_ALL = """
SELECT d.amazon_category, d.walmart_product_type, d.confidence, d.notes,
       coalesce(min(g.walmart_product_type), '') AS sibling
FROM audit.walmart_category_map d
LEFT JOIN audit.walmart_category_map g
       ON g.amazon_category = d.amazon_category
      AND g.walmart_product_type <> d.walmart_product_type
GROUP BY d.amazon_category, d.walmart_product_type, d.confidence, d.notes
ORDER BY d.walmart_product_type, d.amazon_category
"""

# 哨兵:映射表里明确标注"这条 Amazon 路径没有对应的沃尔玛 PT"。
# **不是垃圾,是信息**:`resolve_pt` 的 ⓪ 级读它,命中时打一条 penalty=0 的
# `unmapped_amazon_path` 留痕并把这条信息带进 LLM 提示词(所有者定稿
# 2026-08-14:标注不再判死,只留痕)。删了 = 那点参考信息没了,LLM 少一个线索。
# 在投影表里它表现为"没有 Walmart Category/PTG",看着像空行,其实不是。
SENTINEL = "无对应Walmart PT"
# 真垃圾:PT 是 '-' 或空 —— 既不是 PT 也不是标注,纯脏数据
_JUNK = ("-", "")

_SQL_META_PTS = "SELECT walmart_product_type FROM audit.walmart_pt_meta"

_DOWNGRADE = """
UPDATE audit.walmart_category_map
SET confidence = '低',
    notes = coalesce(notes, '') || %(tag)s,
    synced_at = now()
WHERE amazon_category = %(path)s AND walmart_product_type = %(pt)s
"""

_DELETE = """
DELETE FROM audit.walmart_category_map
WHERE amazon_category = %(path)s AND walmart_product_type = %(pt)s
"""


def run(params: dict) -> str:
    execute = bool(params.get("execute")) and not params.get("dry_run")
    keep = bool(params.get("keep"))          # keep=1 → 降级不删
    # 哨兵行缺省保留(它是信息不是垃圾,见 SENTINEL 注释)
    drop_sentinel = str(params.get("sentinel", "")).strip() == "drop"
    by = str(params.get("by", "spec")).strip()
    if by not in ("spec", "meta"):
        raise ValueError(f"by 只能是 spec/meta,给的是 {by!r}")

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            if by == "spec":
                try:
                    alive = pt_spec.known_pts()
                except FileNotFoundError as e:
                    # spec 没就位就停手 —— 拿一个空集合当"真相"会把整张表删光
                    raise RuntimeError(
                        f"官方 spec 未就位,判据取不到,已停手(绝不拿空集合当"
                        f"真相删数据):{e};临时可用 -p by=meta 走准入明细口径")
            else:
                cur.execute(_SQL_META_PTS)
                alive = {r[0] for r in cur.fetchall()}
            if not alive:
                raise RuntimeError("判据集合为空,已停手 —— 再跑下去会删光整张表")
            cur.execute(_SQL_ALL)
            all_rows = cur.fetchall()

        # 三桶,逐桶报数 —— 所有者数到的 846 行就是这三桶之和
        junk = [r for r in all_rows if (r[1] or "").strip() in _JUNK]
        senti = [r for r in all_rows if r[1] == SENTINEL]
        dead = [r for r in all_rows
                if r[1] not in alive and r[1] != SENTINEL
                and (r[1] or "").strip() not in _JUNK]
        src = ("官方 MP_ITEM spec" if by == "spec" else "准入明细 walmart_pt_meta")

        rows = list(dead) + list(junk)          # 垃圾行永远清
        if drop_sentinel:
            rows += senti
        if not rows:
            return f"映射表里没有指向「不在{src}」的 PT 的行 ✅"

        pts = sorted({r[1] for r in rows})
        paths = len({r[0] for r in rows})
        mode = "降级为低置信(保留行)" if keep else "**删除**"
        orphan = [r for r in rows if not r[4]]
        lines = [
            f"{'' if execute else '🧪 [DRY-RUN] '}映射表 {len(all_rows)} 行,"
            f"其中「投影到飞书后没有 Walmart Category/PTG」的共 "
            f"{len(dead) + len(senti) + len(junk)} 行,分三类:",
            f"  ① 死 PT(不在{src}) {len(dead):>5} 行 / {len({r[1] for r in dead})} 个 PT"
            f" —— **清**",
            f"  ② 哨兵「{SENTINEL}」 {len(senti):>5} 行 —— "
            + ("**本次一并清**(-p sentinel=drop)" if drop_sentinel else
               "**默认保留**:它不是垃圾是信息,resolve_pt ⓪ 级读它、"
               "把「这条路径没有对应 PT」带进 LLM 提示词当线索"
               "(所有者定稿 2026-08-14:标注不再判死,只留痕)。"
               "要一起清加 `-p sentinel=drop`"),
            f"  ③ 垃圾(PT 是 '-' 或空) {len(junk):>5} 行 —— **清**(既不是 PT 也不是标注)",
            "",
            f"本次处置 {len(rows)} 行 / {len(pts)} 个 PT / {paths} 条路径;"
            f"判据 {len(alive)} 个真实 PT;方式:{mode}"]
        if orphan:
            lines.append(
                f"  ⚠ 其中 {len(orphan)} 行**同路径没有别的映射** —— 清掉后"
                f"这些路径成为缺口,由 catmap_mine(实证挖掘,零 LLM)或 "
                f"catmap_suggest(LLM)重建;先跑 catmap_gap 看清单")
        for path, pt, conf, _n, sib in rows[:20]:
            tail = f" → 同路径另有 {sib}" if sib else " → **同路径没有别的映射**"
            lines.append(f"    {pt}({conf}){tail}")
            lines.append(f"        {path[:90]}")
        if len(rows) > 20:
            lines.append(f"    …另有 {len(rows) - 20} 行(共 {len(pts)} 个 PT)")

        if not execute:
            lines += ["", "(dry-run:一行未改;去掉 --dry-run 才落库)"]
            return "\n".join(lines)

        n = 0
        with conn.cursor() as cur:
            for path, pt, _c, _n2, sib in rows:
                if keep:
                    cur.execute(_DOWNGRADE, {
                        "path": path, "pt": pt,
                        "tag": f" [catmap_prune:PT 不在{src},"
                               f"{('同路径另有 ' + sib) if sib else '同路径无替代'},降级]"})
                else:
                    cur.execute(_DELETE, {"path": path, "pt": pt})
                n += cur.rowcount
        conn.commit()
    lines.append(f"  已{'降级' if keep else '删除'} {n} 行")
    lines.append("  下一步:①`python cli.py catmap_gap` 看清掉之后的缺口;"
                 "②`python cli.py product_audit -p mode=pending` 让卡住的产品重审")
    return "\n".join(lines)
