"""error_reclass_report — 报错归类新旧对照(只读排查,不写任何库表)。

用法:
  python cli.py error_reclass_report              # 全量对照,摘要打印 + 全文落盘
  python cli.py error_reclass_report -p limit=40  # 各清单在摘要里多列几行
  python cli.py error_reclass_report -p scope=feed # 只跑 feed 侧(items/events/feed)

为什么要有这条(方案 `docs/error_taxonomy.md` §五):新归类引擎
`services/error_taxonomy.py` 已经写好,但**生产判定路径一个字都还没动** ——
现在跑的仍是 `services/problem_products.categorize()` 那套 A-L 单字母码。
换轨之前所有者要看见的是同一批生产数据在新旧两套下的**并排结果**:
旧码怎么迁到新码、多少条从中性码(过期/未上线)里翻出真问题、unknown 还剩
多少条没判据、抽出来的政策名有多少 join 不上政策表。

本工作流只 SELECT,不改任何行为;跑完给所有者一份可核对的账,过了才开第二步
(处置接线与换轨)。**手动跑,不进调度**。
"""

import logging
from collections import Counter

from registry import db, paths, resources
from services import error_taxonomy, problem_products, product_events

DANGEROUS = False       # 纯 SELECT(唯一的写是报告文件落盘)

logger = logging.getLogger("workflows.error_reclass_report")

# ⚠ 三张表一律 **GROUP BY 原文** 取"文本 + 条数",不是逐行拉回来:
# 下架原因是**高度重复**的模板串(生产 9,024 条过期用的是同一句话),
# 分组后几万行塌成几百条不同文本,内存与耗时都下来了,而报告要的每一样
# (迁移矩阵计数、unknown 全文、政策名命中率)都能按条数加权算出来。
_SQL_ITEMS = """
SELECT unpublished_reasons AS reasons, count(*) AS n
FROM catalog.walmart_items
WHERE published_status IS DISTINCT FROM 'PUBLISHED'
  AND coalesce(unpublished_reasons, '') <> ''
GROUP BY 1
"""
# 含 missing_since 非空的行(方案 §五.1):缺席行的原因是历史病历,
# 正是"被中性码盖住的真问题"最集中的地方,不能按在架口径滤掉。

_SQL_EVENTS = """
SELECT detail->>'reasons' AS reasons, count(*) AS n
FROM catalog.product_events
WHERE event = %s AND coalesce(detail->>'reasons', '') <> ''
GROUP BY 1
"""

# ⚠ status 不在 ops.feed_item_errors 上(那张表只有 type/code/field/description),
# SKU 级终态的权威是 ops.feed_items.status —— 方案 §3.6 那道
# 「status=='failed' 才认 field 锚」的硬闸靠这个 JOIN 供数。
# 关联不上的(老行/明细先到台账后到)status 给 NULL,按非 failed 处理:
# 判不准就不判成政策拒,与"带警告的成功"同一方向的保守。
_SQL_FEED = """
SELECT e.code, e.field, e.description, fi.status, count(*) AS n
FROM ops.feed_item_errors e
LEFT JOIN ops.feed_items fi ON fi.feed_id = e.feed_id AND fi.sku = e.sku
GROUP BY 1, 2, 3, 4
"""

_SQL_POLICY = ("SELECT category_en FROM audit.walmart_prohibited_policy "
               "WHERE category_en IS NOT NULL ORDER BY category_en")

_REPORT_FILE = "error_reclass_report.txt"
_UNKNOWN_TARGET = 50        # 方案 §七.2 的验收线:unknown 全文清单 <50 条


def _fetch(cur, sql, args=None) -> list[tuple]:
    """输入:游标 + SQL → 输出:行列表;查不到表只告警不阻断(表可能还没建)。

    ⚠ 失败后必须 rollback:PG 里一条语句报错整个事务进 aborted 态,
    不回滚的话后面每一查都 InFailedSqlTransaction —— 一张表缺失会连累整份报告。
    """
    try:
        if args:
            cur.execute(sql, args)
        else:
            cur.execute(sql)
        return cur.fetchall()
    except Exception as e:                                  # noqa: BLE001
        logger.warning("读不到数据(本段跳过):%s… / %s",
                       " ".join(sql.split())[:60], e)
        cur.connection.rollback()
        return []


class _Tally:
    """一个数据面的对照账:旧码 × 新码矩阵 + 新码分布 + unknown/政策名清单。"""

    def __init__(self, label: str):
        self.label = label
        self.rows = 0                       # 不同文本数
        self.records = 0                    # 加权条数
        self.matrix: Counter = Counter()    # (旧码, 新主码) → 条数
        self.new_codes: Counter = Counter()
        self.old_codes: Counter = Counter()
        self.old_names: dict[str, str] = {}     # 旧码 → 旧中文名(categorize 给的)
        self.unknown: Counter = Counter()   # 原子原文 → 条数
        self.unlisted: Counter = Counter()
        self.policy_hit: Counter = Counter()    # 政策名 → 条数(join 得上的)
        self.policy_gap: Counter = Counter()    # 政策名 → 条数(join 不上的)
        self.via_ai = 0

    def add(self, text: str, n: int, policy_names) -> None:
        atoms = error_taxonomy.split_reasons(text)
        res = error_taxonomy.classify_reasons(atoms, policy_names)
        old_code, old_name = problem_products.categorize(text)
        self.rows += 1
        self.records += n
        self.matrix[(old_code, res.code)] += n
        self.new_codes[res.code] += n
        self.old_codes[old_code] += n
        self.old_names[old_code] = old_name
        for atom in res.unknown:
            self.unknown[atom] += n
        for term, hits in res.unlisted:
            self.unlisted[term] += n * hits
        if res.via_ai:
            self.via_ai += n
        # 政策名按**原子**统计:一条记录可能带多个政策名(多原子组合)
        for _code, name in res.atom_codes:
            if not name:
                continue
            if error_taxonomy.policy_join(name, policy_names):
                self.policy_hit[name] += n
            else:
                self.policy_gap[name] += n


def _fmt_matrix(t: _Tally, limit: int) -> list[str]:
    """输入:对照账 → 输出:迁移矩阵文本行(旧码逐个摊开去向)。"""
    out = [f"▍{t.label}:{t.records} 条(不同文本 {t.rows} 种)",
           "  旧码 → 新主码(条数;旧码按量降序):"]
    for old, n_old in t.old_codes.most_common():
        dests = sorted(((new, n) for (o, new), n in t.matrix.items() if o == old),
                       key=lambda kv: -kv[1])
        head = f"    {old} {t.old_names.get(old, ''):<4} {n_old:>7} →  "
        out.append(head + "  ".join(f"{new}×{n}" for new, n in dests[:limit]))
    out.append("  新主码分布:")
    for code, n in t.new_codes.most_common():
        out.append(f"    {code:<17} {resources.ERROR_CATEGORY_CODES[code]:<8}"
                   f" {n:>7}")
    if t.via_ai:
        out.append(f"  其中 AI 内容审查(via_ai)          {t.via_ai:>7}")
    return out


def _fmt_lists(t: _Tally, limit: int, full: bool) -> list[str]:
    """输入:对照账 → 输出:unknown / 政策表缺口 / 显式杂项三张清单。"""
    out = []
    n_unknown = sum(t.unknown.values())
    flag = "" if len(t.unknown) < _UNKNOWN_TARGET else \
        f"  ⚠ 超过 {_UNKNOWN_TARGET} 条 —— **判据有漏**,照原文补 §3.3 判据表"
    out.append(f"  未识别(OTHER 兜底)文本 {len(t.unknown)} 种 / {n_unknown} 条"
               f"{flag}")
    take = t.unknown.most_common() if full else t.unknown.most_common(limit)
    for atom, n in take:
        out.append(f"    {n:>6}  {atom if full else atom[:150]}")
    if t.unlisted:
        out.append("  显式杂项清单(不算 unknown):"
                   + "  ".join(f"{k}×{v}" for k, v in t.unlisted.most_common()))
    hit, gap = sum(t.policy_hit.values()), sum(t.policy_gap.values())
    if hit or gap:
        rate = 100.0 * hit / (hit + gap)
        out.append(f"  政策名:抽出 {hit + gap} 条,join 上政策表 {hit} 条"
                   f"({rate:.1f}%)")
        take = t.policy_gap.most_common() if full else t.policy_gap.most_common(limit)
        if take:
            out.append("  政策表缺口(抽到类别名但 audit.walmart_prohibited_policy "
                       "里没有对应行):")
            out += [f"    {n:>6}  {name}" for name, n in take]
    return out


def _feed_section(rows: list[tuple], policy_names, limit: int,
                  full: bool) -> list[str]:
    """输入:feed 报错分组行 → 输出:四通道分类 + 政策族明细 + 未知 field 榜。"""
    chan: Counter = Counter()
    codes: Counter = Counter()
    pol_hit, pol_gap, pol_none = Counter(), Counter(), 0
    fields: Counter = Counter()
    unknown_policy: Counter = Counter()
    total = 0
    for code, field, desc, status, n in rows:
        res = error_taxonomy.classify_feed_error(code, field, desc, status)
        total += n
        chan[res.channel] += n
        if res.channel == "policy":
            codes[res.code] += n
            if res.code == "OTHER":
                unknown_policy[(desc or "")] += n
            if not res.policy_name:
                pol_none += n
            elif error_taxonomy.policy_join(res.policy_name, policy_names):
                pol_hit[res.policy_name] += n
            else:
                pol_gap[res.policy_name] += n
        elif res.channel == "operational":
            fields[field or "(空)"] += n
    out = [f"▍feed 报错(ops.feed_item_errors):{total} 条"
           f"(不同 code/field/文本 {len(rows)} 种)",
           "  通道分布:" + "  ".join(f"{k}×{v}" for k, v in chan.most_common())]
    if codes:
        out.append("  政策族(field ∈ "
                   + "/".join(sorted(resources.WALMART_ERR_FIELD_POLICY))
                   + " 且 status=failed)逐条归类:"
                   + "  ".join(f"{k}×{v}" for k, v in codes.most_common()))
        named = sum(pol_hit.values()) + sum(pol_gap.values())
        if named + pol_none:
            out.append(f"  政策名命中率:抽出 {named} 条 / 共 {named + pol_none} 条"
                       f"({100.0 * named / (named + pol_none):.1f}%);"
                       f"其中 join 上政策表 {sum(pol_hit.values())} 条")
        if pol_gap:
            out.append("  政策表缺口:")
            take = pol_gap.most_common() if full else pol_gap.most_common(limit)
            out += [f"    {n:>6}  {name}" for name, n in take]
    if unknown_policy:
        out.append(f"  ⚠ 政策族里判不出码的正文 {len(unknown_policy)} 种"
                   f"(序 1-15 全不命中,照原文补判据):")
        take = unknown_policy.most_common() if full else unknown_policy.most_common(limit)
        out += [f"    {n:>6}  {d if full else d[:150]}" for d, n in take]
    if fields:
        out.append(f"  operational 通道 field 榜(前 {limit};已结构化,"
                   f"本步不进 16 码表):")
        out += [f"    {n:>7}  {f}" for f, n in fields.most_common(limit)]
    return out


def _load_policy_names(conn) -> list[str]:
    """输入:连接 → 输出:政策表 category_en 列表(读不到给空列表,不阻断)。"""
    with conn.cursor() as cur:
        rows = _fetch(cur, _SQL_POLICY)
    return [str(r[0]).strip() for r in rows if r[0] and str(r[0]).strip()]


def run(params: dict) -> str:
    limit = int(params.get("limit", 20))
    scope = str(params.get("scope", "all")).strip().lower()
    dry_run = bool(params.get("dry_run"))

    with db.pg_conn() as conn:
        policy_names = _load_policy_names(conn)
        with conn.cursor() as cur:
            items = _fetch(cur, _SQL_ITEMS) if scope in ("all", "items") else []
            events = (_fetch(cur, _SQL_EVENTS, (product_events.STATUS_CHANGED,))
                      if scope in ("all", "events") else [])
            feed = _fetch(cur, _SQL_FEED) if scope in ("all", "feed") else []

    head = [f"报错归类新旧对照(码表版本 {resources.ERROR_TAXONOMY_VERSION};"
            f"旧引擎 = problem_products.categorize 的 A-L 码)",
            f"政策表 audit.walmart_prohibited_policy:{len(policy_names)} 条 "
            f"category_en"]
    if not policy_names:
        head.append("⚠ 政策表读不到 —— 本轮政策名一律算「缺口」,别据此下结论")

    body: list[str] = []
    summary: list[str] = list(head)
    for label, rows in (("catalog.walmart_items(下架且有原因)", items),
                        ("catalog.product_events(status_changed)", events)):
        if not rows:
            continue
        tally = _Tally(label)
        for text, n in rows:
            tally.add(text, int(n), policy_names)
        summary += [""] + _fmt_matrix(tally, limit) + _fmt_lists(tally, limit, False)
        body += [""] + _fmt_matrix(tally, limit) + _fmt_lists(tally, limit, True)
    if feed:
        summary += [""] + _feed_section(feed, policy_names, limit, False)
        body += [""] + _feed_section(feed, policy_names, limit, True)
    if not (items or events or feed):
        return "\n".join(head + ["", "三张表都没有可对照的行(库是空的?先跑 catalog_sync)"])

    if dry_run:
        summary.append("")
        summary.append("🧪 --dry-run:全文报告未落盘(本工作流只读,落盘是它唯一的写)")
        return "\n".join(summary)
    paths.reports_dir().mkdir(parents=True, exist_ok=True)
    path = paths.reports_dir() / _REPORT_FILE
    path.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    summary += ["", f"▍全文报告(含全部 unknown 原文与缺口清单)→ {path}"]
    return "\n".join(summary)
