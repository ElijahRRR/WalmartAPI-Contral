"""error_reclass — 报错记录按**新码表**重新归类并回填(存量换标准的第一步)。

用法(**手动跑,不进调度**;缺省即真跑,空跑用 --dry-run):
  python cli.py error_reclass --dry-run                 # 只统计,不写一行
  python cli.py error_reclass                           # 真跑:两张表都回填
  python cli.py error_reclass -p scope=records          # 只回填报错记录表
  python cli.py error_reclass -p scope=blacklist        # 只回填黑名单
  python cli.py error_reclass -p force=1                # 连已盖章的一起重判
  python cli.py error_reclass -p chunk=5000

为什么要有这条(所有者 2026-09-03:「报错原文应该是有的,我们要重新对这些
记录按新标准进行重新定义并归类。先把这一步做了,**标准不统一,审核误差很大**」):
新码表 `services/error_taxonomy`(16 码)2026-09-01 就写好了,但在此之前
**只有报告与评估读它**,生产落库跑的仍是 `problem_products.categorize` 的
A-L 单字母码。于是同一批产品在库里挂着旧码、在报告里是新码,两套并存 ——
这就是"标准不统一"。本工作流把**存量记录**按新码判一遍并写进库。

## 写什么、不写什么(全部写面)

写 **两张表的新增列**,老列一个字不动:
  · `audit.walmart_error_records`:`taxonomy_code` / `taxonomy_policy` /
    `taxonomy_version`(老列 `error_code` char(1) 是旧 A-L 码,**保留**——
    删了就没法对照"当初按什么拉的黑");
  · `catalog.asin_blacklist`:同上三列 + `taxonomy_src`(原文从哪儿找到的)。

⚠ **`catalog.asin_blacklist.category` 复核出结论就改写成新码**(2026-09-04
所有者裁决:「不要做双轨,没有意义,以新规则统一」)。`LEGACY` 与判不出的
两类不动。**拦截行为仍然一点没变** —— 上架闸拦的判据是「这个 asin 在不在
表里」,`category` 只进提示文字;飞书「来源」列也不变(`source_label` 经
`_NAMES` 把新码映射回旧中文标签)。
L0 的 ASIN 闸读的仍是 `category`,新码**只是账**。让新码改变拦截行为
(把按 PT_WRONG/GATED 拉黑的行放出来)是**另一次裁决** —— 黑名单的既定语义
是「一次入选、永久禁止」,批量放行是破坏性动作,不在本工作流里顺手做。

## 原文从哪儿来(唯一取用口 `services/error_source`,两遍分工不同)

所有者说得对:原文是有的。但**取法按「这一行有没有自己那一刻的原文」分两种**:

· **黑名单行 / 报错记录**没有自己那一刻的原文(`reason` 只是入选时抄的样本),
  走 `error_source.pick`,四级优先记进 `taxonomy_src`:
    1. `records` —— `audit.walmart_error_records.raw_reason`(**全文**,NOT NULL;
       同一 asin 多条取 `report_date` 最新的那条);
    2. `events`  —— `catalog.product_events.detail->>'reasons'`(病历,最新一条);
    3. `items`   —— `catalog.walmart_items.unpublished_reasons`(当前值,先按
       `src_sku` 精确对,再按 asin 兜底);
    4. `self`    —— 本表 `reason` 列(⚠ **截 200 字符的样本**,判据串可能被切掉);
    5. 都没有 → `taxonomy_src='none'`、`taxonomy_code` 留 **NULL**(不猜)。

· **产品事件**有自己那一刻的原文(它是时间线上的一格),走
  `error_source.restore`:候选必须**以事件自己那份为前缀**,只把被 `[:200]`
  切掉的那段接回去,不换成别的时间点的文本。再加一道棘轮:**已经是新码的行,
  原文还原不了就一个字不动**。⚠ 这两条是 2026-09-04 事故换来的(§17):
  第一版拿事件的 200 字残文重判,把 `problem_scan` 当初用全文判对的 2,595 条
  `PT_WRONG` 改成了 `POLICY` —— **回填的风险不是判得糙,是把判对的行改错**。

## 幂等与增量

两条判据**各司其职,缺一不可**(2026-09-03 踩过:把两件事当成一件):

1. **断点续跑** —— `taxonomy_version IS DISTINCT FROM <当前
   ERROR_TAXONOMY_VERSION>`:判过的行盖上版本号即退出候选集,跑一半中断
   直接重跑不重复劳动;码表递增后再跑就是全量重判。
   `-p force=1` **有意关掉这一条**,无视版本号重判全部;
2. **翻页** —— 键集游标 `key > after`(`_pages`),**任何模式下都必须在**。

⚠ 别把第 1 条当成分页:`force` 时它被 `true OR …` 短路,`ORDER BY key
LIMIT n` 每轮都返回同一批,盖章也排不掉 —— 循环永不终止(实遇:进度打到
**5,574 万**,表里只有 97,002 行),而第一批之后的数据一条都没碰。
"""

import logging
from collections import Counter

from registry import db, resources
from services import (blacklist, error_source, error_taxonomy,
                      problem_products)

DANGEROUS = False       # 只写自己库的新增列,不碰沃尔玛接口

logger = logging.getLogger("workflows.error_reclass")

_KNOWN_PARAMS = {"scope", "chunk", "force", "limit"}
_CLI_INJECTED = {"execute", "dry_run", "store"}

#: 口径唯一出处在 `services/error_taxonomy`(报账的 error_reclass_report 与
#: 回填的本工作流读同一份;工作流之间不许 import,各写一份就是双轨)。
NOT_A_PRODUCT_BAN = error_taxonomy.NOT_A_PRODUCT_BAN

_SQL_POLICY = ("SELECT category_en FROM audit.walmart_prohibited_policy "
               "WHERE category_en IS NOT NULL ORDER BY category_en")

# ── 报错记录表 ────────────────────────────────────────────────────────────
_SQL_REC_PICK = """
SELECT id, raw_reason, error_code
FROM audit.walmart_error_records
WHERE ({force} OR taxonomy_version IS DISTINCT FROM %(ver)s)
  AND coalesce(raw_reason, '') <> ''
  AND (%(after)s::bigint IS NULL OR id > %(after)s::bigint)
ORDER BY id
LIMIT %(chunk)s
"""
_SQL_REC_SET = """
UPDATE audit.walmart_error_records
SET taxonomy_code = %(code)s, taxonomy_policy = %(policy)s,
    taxonomy_term = %(term)s, taxonomy_version = %(ver)s
WHERE id = %(id)s
"""

# ── 黑名单表 ──────────────────────────────────────────────────────────────
_SQL_BL_PICK = """
SELECT asin, category, reason, src_sku
FROM catalog.asin_blacklist
WHERE ({force} OR taxonomy_version IS DISTINCT FROM %(ver)s)
  AND (%(after)s::text IS NULL OR asin > %(after)s::text)
ORDER BY asin
LIMIT %(chunk)s
"""
_SQL_BL_SET = """
UPDATE catalog.asin_blacklist
SET taxonomy_code = %(code)s, taxonomy_policy = %(policy)s,
    taxonomy_term = %(term)s, taxonomy_version = %(ver)s,
    taxonomy_src = %(src)s,
    -- ⚠ 2026-09-04 所有者裁决:「旧 A-L 码入选然后按新码复核过,那么现在库里
    --   保留的应该就只有新码,没有旧码残留……不要做双轨,没有意义,以新规则统一」。
    --   所以复核出结论的行,`category` 同步改写成新码。
    --   两条不动:① `LEGACY`(历史继承,所有者说「保留原样没有问题」);
    --            ② 判不出的(`code` 为 NULL)—— 没结论就没有可写的东西。
    -- ⚠ `%(code)s::text` 的转型不能省:psycopg 把每个 `%(code)s` 展开成**独立
    --   占位符**,`$1 IS NULL` 那个没有列可以推类型,PG 直接
    --   `AmbiguousParameter: could not determine data type of parameter $1`
    --   (2026-09-04 实遇)。赋值位那几个由列推得出来,`IS NULL` 位推不出来。
    -- ⚠ CASE 里读到的 `category` 是**更新前**的值(PG 的 UPDATE 语义:所有 SET
    --   表达式都看旧行)—— 正是要的:拿旧码判豁免,写新码。
    category = CASE WHEN category = 'LEGACY' OR %(code)s::text IS NULL
                    THEN category ELSE %(code)s::text END,
    source   = CASE WHEN category = 'LEGACY' OR %(code)s::text IS NULL
                    THEN source ELSE %(source)s::text END
WHERE asin = %(asin)s
"""

# 原文三级外源。⚠ 都是 DISTINCT ON 取**最新一条**:同一 asin 多条报错时,
# 拿最近那次的原文判 —— 与黑名单"当轮类别"的口径一致(旧类别翻动频繁,
# 「曾经命中过」不作数,见 services/blacklist 头注)。


def _parse(params: dict) -> tuple[str, int, bool, bool, int]:
    """输入:params → 输出:(scope, chunk, force, execute, limit)。

    ⚠ 未识别参数**宁炸不吞**(与 product_audit 同款纪律):打错的参数名被静默
    吞掉,人会以为"按我说的跑完了",而实际跑的是缺省口径,摘要还长得一模一样。
    """
    unknown = set(params) - _KNOWN_PARAMS - _CLI_INJECTED
    if unknown:
        raise ValueError(f"未识别参数 {sorted(unknown)}(可用:{sorted(_KNOWN_PARAMS)})")
    scope = str(params.get("scope", "all")).strip().lower()
    if scope not in ("all", "records", "blacklist", "events"):
        raise ValueError(
            f"scope 只能是 all / records / blacklist / events,给的是 {scope!r}")
    chunk = int(params.get("chunk", 10_000))
    if chunk <= 0:
        raise ValueError(f"chunk 要正整数,给的是 {chunk}")
    raw_limit = str(params.get("limit", "")).strip()
    limit = int(raw_limit) if raw_limit else 0      # 0 = 不限量
    # ⚠ DANGEROUS=False 时 cli 恒给 execute=True,`--dry-run` 必须自己认
    #   (漏了这一句 `--dry-run` 会直接把存量刷了,而且报成功)
    execute = bool(params.get("execute")) and not params.get("dry_run")
    return scope, chunk, bool(params.get("force")), execute, limit


def classify(text: str, policy_names) -> tuple[str, str | None, str | None]:
    """输入:报错原文 + 政策表名 → 输出:(新主码, 政策名或 None, 显式词条或 None)。

    **政策名是与主码正交的第二维**(所有者 2026-09-03 问「沃尔玛的报错是带禁售
    政策类别的,这里面怎么没有体现」):沃尔玛的原文常写成
    `Prohibited Products Policy: Alcohol.`,引擎把 `Alcohol` 抽出来,再对
    `audit.walmart_prohibited_policy.category_en` join —— **那正是审核链
    `catalog.products.audit_reason` 用的同一张枚举**,所以两边可以直接对照。
    ⚠ 只有**部分**报错带类别名(通用政策拒就一句"违反禁售政策",没有类别);
    抽不出来是常态,不是缺口。

    政策名**必须 join 得上政策表才写**:猜出来的名字会一路进报表与申诉口径,
    而没有任何东西会红(与 audit_l3 的 `policy` 解析同一条纪律)。
    第三个返回值是**赢下主码那个原子**命中的显式词条(序 16),`OTHER` 该不该
    拉黑靠它判(所有者只认 business decision / trust & safety)。
    """
    res = error_taxonomy.classify_reasons(
        error_taxonomy.split_reasons(text), policy_names)
    return (res.code,
            error_taxonomy.policy_join(res.policy_name, policy_names),
            res.unlisted_term)


def pick_source(asin: str, own_reason: str | None, src_sku: str | None,
                records: dict, events: dict, items: dict) -> tuple[str, str]:
    """输入:一行黑名单 + 三张外源表的映射 → 输出:(原文, 来源标签)。

    ⚠ 2026-09-04 起实现搬到 `services/error_source.pick` —— 取原文与归类
    (`services/error_taxonomy`)一样,只准有一处实现。这里只留个转调:
    `blacklist` 的回填走的是同一份,不然同一个品在两条路上判出相反的码
    (实测 3,037 个,见该模块头注)。
    """
    return error_source.pick(asin, own_reason, src_sku, records, events, items)


def _load_policy_names(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(_SQL_POLICY)
        return [r[0] for r in cur.fetchall()]


def _sources(conn, asins: list[str], skus: list[str]) -> tuple[dict, dict, dict]:
    """输入:一批 asin/sku → 输出:三张外源映射(唯一实现在 services/error_source)。"""
    return error_source.fetch(conn, asins, skus)


def _pages(conn, sql: str, ver: str, chunk: int, limit: int, execute: bool):
    """输入:带键集游标的 PICK SQL → 逐批产出行(第 0 列必须是主键,且已 ORDER BY 它)。

    ⚠ **翻页只能靠键集游标 `key > after`,不能靠「版本号已盖章」当排除条件。**
    2026-09-03 实遇:`-p force=1` 时候选谓词被 `true OR …` **短路**掉,
    `ORDER BY key LIMIT n` 每轮返回的都是**同一批**,UPDATE 盖了章也排不掉 ——
    循环永不终止,进度打到 **5,574 万**(表里只有 97,002 行),而第一批之后的
    数据**一条都没碰过**。看着像在干活,其实原地转圈。

    两条判据各司其职,缺一不可:
      · `taxonomy_version IS DISTINCT FROM ver` —— 断点续跑(跑一半中断不重复劳动),
        `force` 时被有意关掉;
      · `key > after` —— **翻页**,任何模式下都必须在,与 `force` 无关。
    """
    after, done = None, 0
    while True:
        take = chunk if not limit else min(chunk, limit - done)
        if take <= 0:
            return
        with conn.cursor() as cur:
            cur.execute(sql, {"ver": ver, "chunk": take, "after": after})
            rows = cur.fetchall()
        if not rows:
            return
        yield rows
        done += len(rows)
        after = rows[-1][0]          # 键集游标 = 本批最后一行的主键
        if not execute:
            return                   # 空跑不盖版本号 ⇒ 只取一批看形态


def _records_pass(conn, ver: str, chunk: int, force: bool, limit: int,
                  execute: bool, policy_names) -> list[str]:
    """回填 audit.walmart_error_records。返回摘要行。"""
    matrix: Counter = Counter()
    new_codes: Counter = Counter()
    policies: Counter = Counter()      # 政策类别名(与主码正交的第二维)
    done = 0
    sql = _SQL_REC_PICK.format(force="true" if force else "false")
    for rows in _pages(conn, sql, ver, chunk, limit, execute):
        ups = []
        for rid, raw, old in rows:
            code, policy, term = classify(raw, policy_names)
            matrix[(old or "(空)", code)] += 1
            new_codes[code] += 1
            if policy:
                policies[policy] += 1
            ups.append({"id": rid, "code": code, "policy": policy,
                        "term": term, "ver": ver})
        if execute:
            with conn.cursor() as cur:
                cur.executemany(_SQL_REC_SET, ups)
            conn.commit()
        done += len(rows)
        logger.info("报错记录回填进度 %d(本批 %d)", done, len(rows))
    out = [f"▍audit.walmart_error_records:判了 {done} 条"]
    out += [f"    {n:>7}  {code}" for code, n in new_codes.most_common(15)]
    if policies:
        out += ["", f"  **政策类别名**(与主码正交的第二维;沃尔玛原文里写成 "
                f"`Prohibited Products Policy: <类别>`,已 join 政策表 "
                f"⇒ 与审核链 `catalog.products.audit_reason` **同一张枚举**,"
                f"可直接对照):抽出且 join 上 {sum(policies.values())} 条,"
                f"{len(policies)} 个类别,前 15:"]
        out += [f"    {n:>7}  {name}" for name, n in policies.most_common(15)]
        out.append("  ⚠ 只有**部分**报错带类别名(通用政策拒就一句「违反禁售"
                   "政策」,没有类别)—— 抽不出是常态,不是缺口。")
    if matrix:
        out.append("  旧码(error_code) → 新码,前 15:")
        out += [f"    {n:>7}  {old} → {code}"
                for (old, code), n in matrix.most_common(15)]
    return out


# ── 产品事件(catalog.product_events)────────────────────────────────────────
#
# ⚠ 所有者 2026-09-04:「产品级的记录已经有产品事件在做了。我们改了新归类,
#   **产品事件跟随更新了吗?**」—— 没有。换轨(2026-09-03)只改了写入侧,
#   `problem_scan` 从此写新码,而**历史事件的 `detail.category` 还是旧 A-L 码**。
#
# 这才是问题的根:此前我在**读的时候**一遍遍重判原文来绕开旧码,而正确的做法是
# **让账本本身是对的** —— 判定只在 `problem_scan` 发生一次,其余全是查询。
_SQL_EV_PICK = """
SELECT id, coalesce(detail->>'reason', detail->>'reasons') AS text,
       detail->>'category' AS cat, sku, coalesce(asin, sku) AS akey
FROM catalog.product_events
WHERE event = 'problem_categorized'
  AND coalesce(detail->>'reason', detail->>'reasons', '') <> ''
  AND ({force} OR detail->>'taxonomy_version' IS DISTINCT FROM %(ver)s)
  AND (%(after)s::bigint IS NULL OR id > %(after)s::bigint)
ORDER BY id
LIMIT %(chunk)s
"""
#: 改码:`name` 跟着走(它是给人看的中文标签,码变了名不变就成了两套说法),
#: `taxonomy_src` 记原文是从哪儿**还原**来的,版本号盖章(断点续跑靠它)。
_SQL_EV_SET = """
UPDATE catalog.product_events
SET detail = detail
    || jsonb_build_object('category', %(code)s::text,
                          'name', %(name)s::text,
                          -- OTHER 是混装桶,没有词条判不了永久(is_permanent)
                          'taxonomy_term', %(term)s::text,
                          'taxonomy_src', %(src)s::text,
                          'taxonomy_version', %(ver)s::text)
WHERE id = %(id)s
"""
#: 不改码,只盖章。**这条 SQL 就是那道棘轮**:够不到重判门槛的行走这里,
#: 于是既不会被改坏,也不会每轮重新排队(见 `_events_pass` 头注)。
_SQL_EV_KEEP = """
UPDATE catalog.product_events
SET detail = detail || jsonb_build_object('taxonomy_src', 'keep',
                                          'taxonomy_version', %(ver)s::text)
WHERE id = %(id)s
"""


def _events_pass(conn, ver: str, chunk: int, force: bool, limit: int,
                 execute: bool, policy_names, by_asin: dict) -> list[str]:
    """回填 catalog.product_events 的 detail.category。返回摘要行。

    ## 一条棘轮:**已经是新码的行,只有原文被还原时才重判,否则一个字不动**

    2026-09-04 生产事故(docs/error_taxonomy.md §17):本遍第一版拿事件自己的
    `detail.reason` 重判,而 `problem_scan` 到当天为止写事件时是
    `(it["reasons"] or "")[:200]` —— **判用全文、存留残文**。于是回填把 2,595 条
    当初判对的 `PT_WRONG`(可放)改成了 `POLICY`(永久禁),摘要还显示
    「码变了 239,313 条」一切正常。**回填的下限不是判得糙,是把判对的行改错。**

    所以这一遍先还原原文(`error_source.restore`:候选必须以事件自己那份为
    前缀 ⇒ 只接回被切掉的那段,不换成别的时间点的文本),再按这条判:
      · 还原成功 → 拿全文重判并写(这也正是**修那批行**的路径:当初那段全文
        还在 `walmart_error_records` / `walmart_items` / `ops.dispositions`
        三处之一 —— 最后那条是 `problem_scan` 当轮写的全文副本,回填从没碰过);
      · 没还原、但 `detail.category` **已经是新码** → 不动
        (`problem_scan` 用的文本只会比我们手上这份更全,重判只会更差);
      · 没还原、码还是旧 A-L(或空)→ 判残文写进去,这才是真正的"下限"。
    """
    matrix: Counter = Counter()
    new_codes: Counter = Counter()
    src_stat: Counter = Counter()
    done = changed = kept = 0
    sql = _SQL_EV_PICK.format(force="true" if force else "false")
    for rows in _pages(conn, sql, ver, chunk, limit, execute):
        akeys = [r[4] for r in rows if r[4]]
        skus = [r[3] for r in rows if r[3]]
        # ⚠ events 那一级对本遍**没有意义**:要判的就是事件,拿同一 asin
        #   **另一条**事件的文本判这一条就是串账 —— 所以丢掉,只用两条外源。
        rec, _ev, it = _sources(conn, akeys, skus)
        it = {**by_asin, **it}          # 按 sku 命中的优先
        # 第四条外源:`ops.dispositions.reason` 是 problem_scan 当轮的**全文**,
        # 而回填从没碰过那张表 —— 被残文覆盖的事件,全文只剩这一份副本了
        # (生产实测:确认救不回来的 349 条里 335 条在这儿对得上)。
        disp = error_source.dispositions_map(conn, skus)
        sets, keeps = [], []
        for ev_id, own, cat, sku, akey in rows:
            cands = [("records", rec.get(akey)),
                     ("items", it.get(sku) or it.get(akey))]
            cands += [("dispositions", t) for t in disp.get(sku, ())]
            full, src = error_source.restore(own, cands)
            src_stat[src] += 1
            if src == "self" and cat in resources.ERROR_CATEGORY_CODES:
                keeps.append({"id": ev_id, "ver": ver})     # 棘轮:不动
                kept += 1
                continue
            code, _policy, term = classify(full, policy_names)
            new_codes[code] += 1
            if cat != code:
                matrix[(cat or "(空)", code)] += 1
                changed += 1
            sets.append({"id": ev_id, "code": code, "ver": ver, "term": term,
                         "src": src,
                         "name": resources.ERROR_CATEGORY_CODES.get(code, code)})
        if execute:
            with conn.cursor() as cur:
                if sets:
                    cur.executemany(_SQL_EV_SET, sets)
                if keeps:
                    cur.executemany(_SQL_EV_KEEP, keeps)
            conn.commit()
        done += len(rows)
        logger.info("产品事件回填进度 %d(本批 %d:重判 %d / 不动 %d)",
                    done, len(rows), len(sets), len(keeps))
    # ⚠ 第一句必须是「判了 N 条」:`run()` 靠这个串认出"判了 0 条 = 已经判过",
    #   换了措辞那句提示就永远不出现(2026-09-03 那个坑的近亲)。
    out = [f"▍catalog.product_events(problem_categorized):判了 {done} 条 —— "
           f"重判 {done - kept} 条(其中**码变了** {changed} 条),"
           f"**已是新码且原文还原不了、原样不动** {kept} 条"]
    if src_stat:
        out.append("  原文还原来源(候选须以事件自己那份为前缀,只接回被 200 "
                   "截掉的那段):"
                   + "  ".join(f"{k}={v}" for k, v in src_stat.most_common()))
    out += [f"    {n:>7}  {code}" for code, n in new_codes.most_common(10)]
    if matrix:
        out.append("  码变了的,旧 → 新,前 10:")
        out += [f"    {n:>7}  {old} → {code}"
                for (old, code), n in matrix.most_common(10)]
    out += ["  ⚠ 事件是**产品级记录**:改完之后下游(黑名单该不该拉黑)"
            "直接读这个码,不必再拿原文重判 —— 判定只在 problem_scan 发生一次。",
            "  ⚠ 棘轮(§17):**已经是新码的行,只有原文被还原时才重判**。"
            "回填的风险不是判得糙,是把判对的行改错。"]
    return out


def _blacklist_pass(conn, ver: str, chunk: int, force: bool, limit: int,
                    execute: bool, policy_names, by_asin: dict) -> list[str]:
    """回填 catalog.asin_blacklist。返回摘要行。"""
    src_stat: Counter = Counter()
    matrix: Counter = Counter()
    new_codes: Counter = Counter()
    policies: Counter = Counter()      # 政策类别名(与主码正交的第二维)
    suspect: Counter = Counter()
    done = 0
    sql = _SQL_BL_PICK.format(force="true" if force else "false")
    for rows in _pages(conn, sql, ver, chunk, limit, execute):
        asins = [r[0] for r in rows]
        skus = [r[3] for r in rows if r[3]]
        rec, ev, it = _sources(conn, asins, skus)
        it = {**by_asin, **it}          # 按 sku 命中的优先
        ups = []
        for asin, cat, reason, src_sku in rows:
            text, src = pick_source(asin, reason, src_sku, rec, ev, it)
            src_stat[src] += 1
            if text:
                code, policy, term = classify(text, policy_names)
                new_codes[code] += 1
                matrix[(cat or "(空)", code)] += 1
                if policy:
                    policies[policy] += 1
                if code in NOT_A_PRODUCT_BAN:
                    suspect[(cat or "(空)", code)] += 1
            else:
                code = policy = term = None     # 找不到原文就不猜
            ups.append({"asin": asin, "code": code, "policy": policy,
                        "term": term, "ver": ver, "src": src,
                        # 码名统一了,来源标签跟着走(飞书「来源」列的文字不变:
                        # `_NAMES` 把新码映射回旧中文标签)
                        "source": blacklist.source_label(code) if code else None})
        if execute:
            with conn.cursor() as cur:
                cur.executemany(_SQL_BL_SET, ups)
            conn.commit()
        done += len(rows)
        logger.info("黑名单回填进度 %d(本批 %d)", done, len(rows))
    out = [f"▍catalog.asin_blacklist:判了 {done} 条",
           "  原文来源(全文优先于样本):"
           + "  ".join(f"{k}={v}" for k, v in src_stat.most_common())]
    if src_stat.get("self"):
        out.append(f"  ⚠ 其中 {src_stat['self']} 条只找到本表 200 字符**样本** ——"
                   f"判据串可能被切掉,这部分是下限")
    if src_stat.get("none"):
        out.append(f"  ⚠ {src_stat['none']} 条**四处都没有原文** ——"
                   f"taxonomy_code 留 NULL,不猜")
    out += [f"    {n:>7}  {code}" for code, n in new_codes.most_common(15)]
    if policies:
        out += ["", f"  **政策类别名**(与主码正交的第二维;沃尔玛原文里写成 "
                f"`Prohibited Products Policy: <类别>`,已 join 政策表 "
                f"⇒ 与审核链 `catalog.products.audit_reason` **同一张枚举**,"
                f"可直接对照):抽出且 join 上 {sum(policies.values())} 条,"
                f"{len(policies)} 个类别,前 15:"]
        out += [f"    {n:>7}  {name}" for name, n in policies.most_common(15)]
        out.append("  ⚠ 只有**部分**报错带类别名(通用政策拒就一句「违反禁售"
                   "政策」,没有类别)—— 抽不出是常态,不是缺口。")
    if matrix:
        # ⚠ `category` 统一到新码之后(2026-09-04),这张矩阵**大部分是对角线**
        #   (`FLAGGED → FLAGGED`),信息量归零 —— 只报**真的换了码**的那些,
        #   剩下的报个总数。留着全量对角线只会让人以为"还在迁移中"。
        # ⚠ `LEGACY` 要单列:它判得出新码,但 `_SQL_BL_SET` **有意不改写**它的
        #   `category`(所有者:「历史继承保留原样没有问题」)。混进"变了"那一栏
        #   就是**摘要说变了、库里没变** —— 这条工作流最不该出的那种错。
        legacy = Counter({k[1]: v for k, v in matrix.items() if k[0] == "LEGACY"})
        moved = Counter({k: v for k, v in matrix.items()
                         if k[0] != k[1] and k[0] != "LEGACY"})
        same = sum(v for k, v in matrix.items() if k[0] == k[1])
        if moved:
            out.append(f"  入选码**变了**的(其余 {same:,} 条码没变):")
            out += [f"    {n:>7}  {old} → {code}"
                    for (old, code), n in moved.most_common(15)]
        else:
            out.append(f"  入选码**全部没变**({same:,} 条)—— 已统一到新码")
        if legacy:
            out += [f"  `LEGACY` 历史继承 {sum(legacy.values()):,} 条:判出了新码"
                    f"(写进 `taxonomy_code` 列),但 **`category` 按裁决保持 "
                    f"`LEGACY` 不改写** —— 所以这些**不算「码变了」**:",
                    "    " + "  ".join(f"{c}×{n}"
                                       for c, n in legacy.most_common(10))]
    # ⚠ 「站不住」= 新码属于 `NOT_A_PRODUCT_BAN`(病根不是产品本身违禁)。
    #   它与**去留**是两个正交的问题:去留看 `is_permanent`(所有者裁决的七码
    #   + OTHER 两词条)。两张表都含 `GATED` —— 品类准入拿不到,产品本身不违禁
    #   (所以"站不住"),但我们照样卖不了(所以裁决"继续禁")。
    #   ⚠ 2026-09-04 实遇:统一之前这一段写「旧码算它们永久禁售,新码认出病根
    #     另在别处」,统一之后左右同码(`GATED → GATED`),那句话自相矛盾;
    #     而 2,006 条 GATED 被叫"站不住"更是误导 —— 它们是**留下**的。
    #   所以现在按**会不会被 blacklist_route 删**分两栏报,那才是人要的答案。
    doomed = Counter({c: n for (_o, c), n in suspect.items()
                      if not error_taxonomy.is_permanent(c, None)})
    kept_sus = Counter({c: n for (_o, c), n in suspect.items()
                        if error_taxonomy.is_permanent(c, None)})
    if doomed or kept_sus:
        out.append("")
        if doomed:
            out += [f"  ⚠ **病根不是产品违禁、且按裁决会被放行的:"
                    f"{sum(doomed.values()):,} 条**(下次 blacklist_route 会删):",
                    *[f"       {n:>7}  {c}" for c, n in doomed.most_common(10)]]
        if kept_sus:
            out += [f"  病根不是产品违禁、但**按裁决仍留**的:"
                    f"{sum(kept_sus.values()):,} 条 —— "
                    + "  ".join(f"{c}×{n}" for c, n in kept_sus.most_common(5)),
                    "     (如 GATED:品类准入拿不到,产品本身不违禁,但我们照样卖不了)"]
        out += ["  ⚠ **本工作流没有放行任何一条**:只写码与来源标签,"
                "L0 的 ASIN 闸拦的是「这个 asin 在不在表里」。",
                "     放行是破坏性动作,归 blacklist_route(所有者另一次裁决)。"]
    return out


def run(params: dict) -> str:
    """输入:params(scope/chunk/force/limit)→ 输出:回填摘要。"""
    scope, chunk, force, execute, limit = _parse(params)
    ver = resources.ERROR_TAXONOMY_VERSION
    pred = ("**force:无视版本号,全部重判**" if force else
            "taxonomy_version IS DISTINCT FROM 当前版本(断点续跑)")
    head = [f"报错记录按新码重新归类(码表版本 {ver};"
            f"旧码 = problem_products 的 A-L / error_code)",
            f"候选谓词:{pred}"]
    body: list[str] = []
    with db.pg_conn() as conn:
        policy_names = _load_policy_names(conn)
        if not policy_names:
            head.append("⚠ 政策表读不到 —— 本轮 taxonomy_policy 一律留空,"
                        "别据此下结论")
        # ⚠ 按 asin 索引的 items 那一份**全程只查一次**(全表扫,每批扫一遍太贵;
        #   事件遍与黑名单遍共用同一份)。2026-09-04 实证:上一轮 `self=14,475`
        #   条拿的是 200 字符残文 —— 它们**有** sku,但那个 sku 在 `walmart_items`
        #   里可能已经不在了(下架删除),于是 items 那一级查不中、退回残文。
        #   我原先判断「有精确的 src_sku 就不需要按 asin 兜底」是**错的**:
        #   有 sku ≠ 那个 sku 还查得到。
        by_asin: dict = {}
        if scope in ("all", "events", "blacklist"):
            by_asin = error_source.items_by_asin_map(conn)
            logger.info("items 按 asin 索引 %d 条(给 sku 已失效的行兜底)",
                        len(by_asin))
        if scope in ("all", "records"):
            body += [""] + _records_pass(conn, ver, chunk, force, limit,
                                         execute, policy_names)
        if scope in ("all", "events"):
            # ⚠ 事件先回填:它是**产品级记录**,黑名单那一遍的判定要读它
            body += [""] + _events_pass(conn, ver, chunk, force, limit,
                                        execute, policy_names, by_asin)
        if scope in ("all", "blacklist"):
            body += [""] + _blacklist_pass(conn, ver, chunk, force, limit,
                                           execute, policy_names, by_asin)
    # ⚠ 判了 0 条**必须说清为什么**(2026-09-03 实遇):版本号已盖章时增量谓词
    #   把全部行排除,摘要只剩一行"判了 0 条",看着像"没数据",实际是"已经判过"。
    #   而这时若刚加了新列(如 taxonomy_term),那一列会**全是 NULL** 而没有
    #   任何提示 —— 下游按它做判断就会静默走错。
    if not force and "判了 0 条" in "\n".join(body):
        body += ["", f"⚠ **判了 0 条 = 已经按当前码表({ver})判过了**,不是没数据:"
                 f"候选谓词把盖过章的行排除了(那正是它的作用,天然分页)。",
                 "   要重判(比如**刚加了新列**、或改了判据但没提码表版本):"
                 "`python cli.py error_reclass -p force=1`"]
    if not execute:
        body += ["", "🧪 --dry-run:**一行都没写**;且空跑不盖版本号 ⇒ "
                 "候选集恒定,上面报的就是真跑第一批会判成什么",
                 "   (空跑只取一批看形态,真跑才会一批批走完)"]
    else:
        body += ["", f"新码已写进两表的 taxonomy_* 列(版本 {ver})。"
                 "⚠ **判定行为一个字没动**(拦的是「在不在表里」);`category`\n   已按新码统一(LEGACY 与判不出的两类不动),放行仍归 blacklist_route。"]
    return "\n".join(head + body)
