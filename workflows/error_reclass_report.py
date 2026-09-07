"""error_reclass_report — 报错归类新旧对照(只读排查,不写任何库表)。

用法:
  python cli.py error_reclass_report              # 全量对照,摘要打印 + 全文落盘
  python cli.py error_reclass_report -p limit=40  # 各清单在摘要里多列几行
  python cli.py error_reclass_report -p scope=feed # 只跑 feed 侧
  python cli.py error_reclass_report -p scope=blacklist  # 只跑已拉黑的那批 ASIN
       (scope ∈ all / items / records / events / feed / blacklist)

为什么要有这条:看同一批**生产数据**在现行归类引擎
(`services/error_taxonomy.py`)下判成什么 —— 主码怎么分布、unknown 还剩多少条
没判据、抽出来的政策名有多少 join 不上政策表、已经产生后果的黑名单行按新码
站不站得住。

⚠ 2026-09-04 换轨完成后**退役了「旧码 → 新码迁移矩阵」那一面**:它的使命是
「所有者过完这份账才换轨」,换轨已经做完(存量按新码回填 97,002 + 73,918 条,
`blacklist_route` 按裁决删了 42,113 条),旧引擎 `problem_products.categorize()`
随之一起删。旧码从此只在**读历史数据**时出现(黑名单表 `category` 列存的是
入选那一刻的码),不再有任何判据读它。

本工作流只 SELECT,不改任何行为;跑完给所有者一份可核对的账,过了才开第二步
(处置接线与换轨)。**手动跑,不进调度**。
"""

import logging
from collections import Counter

from registry import db, paths, resources
from services import (error_source, error_taxonomy, problem_products,
                      product_events)

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

# ⚠ 键名两种都要读:写入方 `problem_scan` / `cleanup_history` 写的是
# `detail->>'reason'`(**单数**),这里原先只读 `'reasons'`(复数)—— 于是这一面
# 长期近乎空转,而摘要照样显示正常(2026-09-04 查出)。
_SQL_EVENTS = """
SELECT coalesce(detail->>'reasons', detail->>'reason') AS reasons, count(*) AS n
FROM catalog.product_events
WHERE event = %s
  AND coalesce(detail->>'reasons', detail->>'reason', '') <> ''
GROUP BY 1
"""

# ⚠ status 不在 ops.feed_item_errors 上(那张表只有 type/code/field/description),
# SKU 级终态的权威是 ops.feed_items.status —— 方案 §3.6 那道
# 「status=='failed' 才认 field 锚」的硬闸靠这个 JOIN 供数。
# 关联不上的(老行/明细先到台账后到)status 给 NULL,按非 failed 处理:
# 判不准就不判成政策拒,与"带警告的成功"同一方向的保守。
# ⚠ **全文语料**(2026-09-04 补):`raw_reason` 是 NOT NULL 的全文,所有者
# 2026-09-03 说的"报错原文是有的"就是这一列。此前这一面**完全不在报告里** ——
# 而事件回填第三轮实测它是最大的一份还原来源(47,956 条)。
# 要回答「哪些原文归不出类」,该看的正是这种没被截过的语料:
# `asin_blacklist.reason` 是 200 字样本,归不出类可能只是**我们自己切坏的**,
# 不是沃尔玛写得不规范。
_SQL_RECORDS = """
SELECT raw_reason AS reasons, count(*) AS n
FROM audit.walmart_error_records
WHERE coalesce(raw_reason, '') <> ''
GROUP BY 1
"""

_SQL_FEED = """
SELECT e.code, e.field, e.description, fi.status, count(*) AS n
FROM ops.feed_item_errors e
LEFT JOIN ops.feed_items fi ON fi.feed_id = e.feed_id AND fi.sku = e.sku
GROUP BY 1, 2, 3, 4
"""

# ⚠ 第四面:**已经永久拉黑的那批 ASIN**(所有者 2026-09-03 问:「禁售占了 4 万多个
# 产品,这些产品的具体报错我们重新按新规归类了吗?」)。前三面看的是**报错文本**
# 会怎么改判,这一面看的是**已经产生后果的那批行**里有多少是按旧码拉黑的。
# `category` 存的是入选那一刻的旧码(B/C/E/F/G/K 六类永久码,或历史导入的
# `LEGACY`),`ON CONFLICT DO NOTHING` ⇒ **先到先得、永不回头改**,库里没有
# 任何"按新码重判过"的痕迹。
# ⚠ `reason` 是入选时截 200 字符的**样本**,不是全文:截断可能把判据串切掉
#   (如 `appropriate product type selected` 在 200 字符外)⇒ 本面给的是**下限**。
_SQL_BLACKLIST = """
SELECT category, coalesce(reason, '') AS reason, count(*) AS n
FROM catalog.asin_blacklist
GROUP BY 1, 2
"""

#: 口径唯一出处在 `services/error_taxonomy`(本报告与回填工作流 error_reclass
#: 读同一份;工作流之间不许 import,各写一份就是双轨)。
_NOT_A_PRODUCT_BAN = error_taxonomy.NOT_A_PRODUCT_BAN

_SQL_POLICY = ("SELECT category_en FROM audit.walmart_prohibited_policy "
               "WHERE category_en IS NOT NULL ORDER BY category_en")

_REPORT_FILE = "error_reclass_report.txt"
_UNKNOWN_TARGET = 50        # 方案 §七.2 的验收线:unknown 全文清单 <50 条


def _pct(a: int, b: int) -> str:
    """输入:分子 + 分母 → 输出:`a/b = x%`;分母 0 给 `a/0 = —`(不编数)。"""
    return f"{a}/{b} = {100.0 * a / b:.1f}%" if b else f"{a}/0 = —"


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
        self.new_codes: Counter = Counter()
        self.unknown: Counter = Counter()   # 原子原文 → 条数(**全文**里归不出的)
        #: 源文本正好 SAMPLE_LEN 字符 ⇒ 是**我们自己 `[:200]` 切出来的残片**,
        #: 归不出类不能算"沃尔玛写得不规范"。所有者 2026-09-04:「没归类到的报错
        #: 原文……如果它本来就是不规范的那种,那就不入库也可以」—— 要照这句话做
        #: 判断,前提是清单里**只有真的原文**,残片必须分出去。
        self.unknown_cut: Counter = Counter()
        self.unlisted: Counter = Counter()
        self.policy_hit: Counter = Counter()    # 政策名 → 条数(join 得上的)
        self.policy_gap: Counter = Counter()    # 政策名 → 条数(join 不上的)
        self.via_ai = 0

    def add(self, text: str, n: int, policy_names) -> None:
        atoms = error_taxonomy.split_reasons(text)
        res = error_taxonomy.classify_reasons(atoms, policy_names)
        self.rows += 1
        self.records += n
        self.new_codes[res.code] += n
        bucket = (self.unknown_cut if len(text or "") == error_source.SAMPLE_LEN
                  else self.unknown)
        for atom in res.unknown:
            bucket[atom] += n
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


def _fmt_codes(t: _Tally, limit: int) -> list[str]:
    """输入:对照账 → 输出:主码分布文本行。

    ⚠ 2026-09-04 退役了「旧码 → 新码迁移矩阵」这一面:它的使命是「所有者过完
    这份账才换轨」(README),**换轨已经完成**,唯一的消费者
    `problem_products.categorize()` 随之一起删。旧码从此只在**读历史数据**时
    出现(`_RULES` 的码→中文名,给 `cleanup_history` 与下面的黑名单面用),
    不再有任何判据读它。
    """
    out = [f"▍{t.label}:{t.records} 条(不同文本 {t.rows} 种)",
           "  主码分布:"]
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
    if t.unknown_cut:
        out.append(f"  ⚠ 另有 {len(t.unknown_cut)} 种 / "
                   f"{sum(t.unknown_cut.values())} 条来自**正好 "
                   f"{error_source.SAMPLE_LEN} 字符的残片**(我们自己 `[:200]` "
                   f"切的,不是沃尔玛写得不规范)—— **不列进上面那张清单**,"
                   f"判「要不要补判据 / 要不要入库」时不能拿它当据。")
        take = (t.unknown_cut.most_common() if full
                else t.unknown_cut.most_common(min(limit, 5)))
        for atom, n in take:
            out.append(f"    (残){n:>4}  {atom if full else atom[:150]}")
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


def blacklist_section(rows, policy_names, limit: int, full: bool) -> list[str]:
    """输入:(旧码, reason 样本, 条数) 行 → 输出:黑名单面的对照文本行。

    纯拼装 + 归类,零 I/O(拿假数据就能测)。回答的是所有者那一问:
    **已经永久拉黑的那批 ASIN,按新码还站得住吗?**
    """
    total = sum(int(n) for _c, _r, n in rows)
    no_text = sum(int(n) for _c, r, n in rows if not (r or "").strip())
    by_cat: Counter = Counter()
    matrix: Counter = Counter()
    suspect: Counter = Counter()        # (旧码, 新码) → 条数;新码不是商品违禁
    judged = 0
    for cat, reason, n in rows:
        n = int(n)
        by_cat[cat or "(空)"] += n
        text = (reason or "").strip()
        if not text:
            continue
        judged += n
        res = error_taxonomy.classify_reasons(
            error_taxonomy.split_reasons(text), policy_names)
        matrix[(cat, res.code)] += n
        if res.code in _NOT_A_PRODUCT_BAN:
            suspect[(cat, res.code)] += n

    out = [f"▍catalog.asin_blacklist(**已经产生后果的那批行**):{total} 条",
           "  ⚠ 这一面与前三面问的不是同一件事:前三面看**报错文本**会怎么改判,",
           "     这一面看**已经按旧码永久拉黑**的 ASIN 里,有多少依据在新码下站不住。",
           f"  入选时的旧码分布(`category`,入选那一刻写死、`ON CONFLICT DO "
           f"NOTHING` 永不回头改):"]
    for cat, n in by_cat.most_common(limit):
        name = problem_products._RULES.get(cat, ("",))[0] if cat in \
            problem_products._RULES else ("历史导入" if cat == "LEGACY" else "")
        out.append(f"    {n:>7}  {cat}{(' ' + name) if name else ''}")
    out += [f"  **没有报错样本、无法重判** {no_text} 条"
            f"({_pct(no_text, total)};`reason` 为空 —— 历史导入那批本来就不带原文)",
            f"  可重判(有 `reason` 样本){judged} 条 —— ⚠ 样本**截 200 字符**,"
            f"判据串可能被切掉 ⇒ 下面的数是**下限**,不是精确值"]
    if matrix:
        out.append("  旧码 → 新主码(条数):")
        for (cat, code), n in matrix.most_common(limit):
            flag = "  ← 新码认为**不是商品违禁**" if code in _NOT_A_PRODUCT_BAN else ""
            out.append(f"    {n:>7}  {cat} → {code}{flag}")
    n_suspect = sum(suspect.values())
    if n_suspect:
        out += ["",
                f"  ⚠ **依据在新码下站不住的黑名单行:{n_suspect} 条**"
                f"({_pct(n_suspect, judged)} of 可重判)——",
                "     旧码把它们算作永久禁售,新码认出病根另在别处"
                "(类目选错 / 要资质 / 文案图片 / 信息缺失 …)。",
                "     ⚠ **这不是自动翻案的授权**:黑名单是「一次入选、永久禁止」"
                "的既定语义,",
                "        改不改、怎么改是所有者的裁决;本报告只把账摆出来。"]
        for (cat, code), n in suspect.most_common(limit):
            out.append(f"       {n:>7}  {cat} → {code}")
    if full and no_text:
        out.append(f"  (无原文那 {no_text} 条要重判,只能回源头文本:"
                   f"`catalog.walmart_items.unpublished_reasons` / "
                   f"`catalog.product_events` —— 已删除的品可能已经没有了)")
    return out


def run(params: dict) -> str:
    limit = int(params.get("limit", 20))
    scope = str(params.get("scope", "all")).strip().lower()
    dry_run = bool(params.get("dry_run"))

    with db.pg_conn() as conn:
        policy_names = _load_policy_names(conn)
        with conn.cursor() as cur:
            items = _fetch(cur, _SQL_ITEMS) if scope in ("all", "items") else []
            recs = (_fetch(cur, _SQL_RECORDS)
                    if scope in ("all", "records") else [])
            events = (_fetch(cur, _SQL_EVENTS, (product_events.STATUS_CHANGED,))
                      if scope in ("all", "events") else [])
            feed = _fetch(cur, _SQL_FEED) if scope in ("all", "feed") else []
            blist = (_fetch(cur, _SQL_BLACKLIST)
                     if scope in ("all", "blacklist") else [])

    head = [f"报错归类对照(码表版本 {resources.ERROR_TAXONOMY_VERSION})",
            f"政策表 audit.walmart_prohibited_policy:{len(policy_names)} 条 "
            f"category_en"]
    if not policy_names:
        head.append("⚠ 政策表读不到 —— 本轮政策名一律算「缺口」,别据此下结论")

    body: list[str] = []
    summary: list[str] = list(head)
    for label, rows in (("catalog.walmart_items(下架且有原因)", items),
                        ("audit.walmart_error_records(raw_reason 全文)", recs),
                        ("catalog.product_events(status_changed)", events)):
        if not rows:
            continue
        tally = _Tally(label)
        for text, n in rows:
            tally.add(text, int(n), policy_names)
        summary += [""] + _fmt_codes(tally, limit) + _fmt_lists(tally, limit, False)
        body += [""] + _fmt_codes(tally, limit) + _fmt_lists(tally, limit, True)
    if feed:
        summary += [""] + _feed_section(feed, policy_names, limit, False)
        body += [""] + _feed_section(feed, policy_names, limit, True)
    if blist:
        summary += [""] + blacklist_section(blist, policy_names, limit, False)
        body += [""] + blacklist_section(blist, policy_names, limit, True)
    if not (items or recs or events or feed or blist):
        return "\n".join(head + ["", "五张表都没有可对照的行(库是空的?先跑 catalog_sync)"])

    if dry_run:
        summary.append("")
        summary.append("🧪 --dry-run:全文报告未落盘(本工作流只读,落盘是它唯一的写)")
        return "\n".join(summary)
    paths.reports_dir().mkdir(parents=True, exist_ok=True)
    path = paths.reports_dir() / _REPORT_FILE
    path.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    summary += ["", f"▍全文报告(含全部 unknown 原文与缺口清单)→ {path}"]
    return "\n".join(summary)
