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

⚠ **`catalog.asin_blacklist.category` 一个字不动,判定链的行为一点没变。**
L0 的 ASIN 闸读的仍是 `category`,新码**只是账**。让新码改变拦截行为
(把按 PT_WRONG/GATED 拉黑的行放出来)是**另一次裁决** —— 黑名单的既定语义
是「一次入选、永久禁止」,批量放行是破坏性动作,不在本工作流里顺手做。

## 原文从哪儿来(四级优先,**全文优先于样本**)

所有者说得对:原文是有的。按这个顺序找,记进 `taxonomy_src`:
  1. `records` —— `audit.walmart_error_records.raw_reason`(**全文**,NOT NULL;
     同一 asin 多条取 `report_date` 最新的那条);
  2. `events`  —— `catalog.product_events.detail->>'reasons'`(病历,最新一条);
  3. `items`   —— `catalog.walmart_items.unpublished_reasons`(当前值,按
     黑名单行的 `src_sku` 精确对 —— 那列存的就是沃尔玛侧订货号原文);
  4. `self`    —— 本表 `reason` 列(⚠ **截 200 字符的样本**,判据串可能被切掉);
  5. 都没有 → `taxonomy_src='none'`、`taxonomy_code` 留 **NULL**(不猜)。

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
from services import error_taxonomy, problem_products

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
    taxonomy_src = %(src)s
WHERE asin = %(asin)s
"""

# 原文三级外源。⚠ 都是 DISTINCT ON 取**最新一条**:同一 asin 多条报错时,
# 拿最近那次的原文判 —— 与黑名单"当轮类别"的口径一致(旧类别翻动频繁,
# 「曾经命中过」不作数,见 services/blacklist 头注)。
_SQL_SRC_RECORDS = """
SELECT DISTINCT ON (asin) asin, raw_reason
FROM audit.walmart_error_records
WHERE asin = ANY(%(asins)s) AND coalesce(raw_reason, '') <> ''
ORDER BY asin, report_date DESC NULLS LAST, id DESC
"""
_SQL_SRC_EVENTS = """
SELECT DISTINCT ON (coalesce(asin, sku)) coalesce(asin, sku) AS k,
       detail->>'reasons' AS reasons
FROM catalog.product_events
WHERE coalesce(asin, sku) = ANY(%(asins)s)
  AND coalesce(detail->>'reasons', '') <> ''
ORDER BY coalesce(asin, sku), occurred_at DESC
"""
_SQL_SRC_ITEMS = """
SELECT DISTINCT ON (sku) sku, unpublished_reasons
FROM catalog.walmart_items
WHERE sku = ANY(%(skus)s) AND coalesce(unpublished_reasons, '') <> ''
ORDER BY sku, updated_at DESC
"""


def _parse(params: dict) -> tuple[str, int, bool, bool, int]:
    """输入:params → 输出:(scope, chunk, force, execute, limit)。

    ⚠ 未识别参数**宁炸不吞**(与 product_audit 同款纪律):打错的参数名被静默
    吞掉,人会以为"按我说的跑完了",而实际跑的是缺省口径,摘要还长得一模一样。
    """
    unknown = set(params) - _KNOWN_PARAMS - _CLI_INJECTED
    if unknown:
        raise ValueError(f"未识别参数 {sorted(unknown)}(可用:{sorted(_KNOWN_PARAMS)})")
    scope = str(params.get("scope", "all")).strip().lower()
    if scope not in ("all", "records", "blacklist"):
        raise ValueError(f"scope 只能是 all / records / blacklist,给的是 {scope!r}")
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

    四级优先,**全文优先于样本**(见模块头);都没有给 `("", "none")`。
    纯函数,零 I/O —— 优先序是判据的一部分,拿假数据就能测。
    """
    text = records.get(asin)
    if text:
        return text, "records"
    text = events.get(asin)
    if text:
        return text, "events"
    if src_sku:
        text = items.get(src_sku)
        if text:
            return text, "items"
    own = (own_reason or "").strip()
    if own:
        return own, "self"
    return "", "none"


def _load_policy_names(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(_SQL_POLICY)
        return [r[0] for r in cur.fetchall()]


def _sources(conn, asins: list[str], skus: list[str]) -> tuple[dict, dict, dict]:
    """输入:一批 asin/sku → 输出:三张外源映射(查不到的表只告警不阻断)。"""
    out: list[dict] = []
    for sql, args, key in ((_SQL_SRC_RECORDS, {"asins": asins}, "asins"),
                           (_SQL_SRC_EVENTS, {"asins": asins}, "asins"),
                           (_SQL_SRC_ITEMS, {"skus": skus}, "skus")):
        if not args[key]:
            out.append({})
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                out.append({k: v for k, v in cur.fetchall() if v})
        except Exception as e:                                  # noqa: BLE001
            logger.warning("外源读不到(本级跳过):%s… / %s",
                           " ".join(sql.split())[:50], e)
            conn.rollback()
            out.append({})
    return out[0], out[1], out[2]


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


def _blacklist_pass(conn, ver: str, chunk: int, force: bool, limit: int,
                    execute: bool, policy_names) -> list[str]:
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
                        "term": term, "ver": ver, "src": src})
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
        out.append("  入选旧码(category) → 新码,前 15:")
        out += [f"    {n:>7}  {old} → {code}"
                for (old, code), n in matrix.most_common(15)]
    n_sus = sum(suspect.values())
    if n_sus:
        out += ["",
                f"  ⚠ **依据在新码下站不住的黑名单行:{n_sus} 条** ——"
                f"旧码算它们永久禁售,新码认出病根另在别处:",
                *[f"       {n:>7}  {old} → {code}"
                  for (old, code), n in suspect.most_common(15)],
                "  ⚠ **本工作流没有放行任何一条**:`category` 一个字没动,"
                "L0 的 ASIN 闸读的仍是它。",
                "     黑名单是「一次入选、永久禁止」的既定语义,批量放行是"
                "破坏性动作 —— 要不要放、怎么放是所有者的另一次裁决。"]
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
        if scope in ("all", "records"):
            body += [""] + _records_pass(conn, ver, chunk, force, limit,
                                         execute, policy_names)
        if scope in ("all", "blacklist"):
            body += [""] + _blacklist_pass(conn, ver, chunk, force, limit,
                                           execute, policy_names)
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
                 "⚠ **老列与判定行为一个字没动** —— 换轨与放行是另一次裁决。"]
    return "\n".join(head + body)
