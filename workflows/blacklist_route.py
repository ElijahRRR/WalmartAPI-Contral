"""blacklist_route — 存量 ASIN 黑名单按**新码裁决**重新路由(删掉不该拉黑的行)。

用法(**手动跑,不进调度**;缺省即真跑,空跑用 --dry-run):
  python cli.py blacklist_route --dry-run       # 只报将删/将留,一行不动
  python cli.py blacklist_route                 # 真删(先自动落备份)
  python cli.py blacklist_route -p limit=500    # 分批删,先放一小批看效果

为什么要有这条(所有者 2026-09-03 裁决):`error_reclass` 已把存量按新 16 码
判过并落进 `taxonomy_*` 列,但**判定行为一点没变** —— L0 的 ASIN 闸读的仍是
入选时那个旧码。这条按所有者逐码定的集合(裁决表 `docs/error_taxonomy.md`
§十二)把**不该拉黑的行删掉**,让拦截行为与新标准对齐。

## 裁决(唯一出处 `services/error_taxonomy`)

**留下(继续永久拉黑)**:
  · `PERMANENT_CODES` 七个:PROHIBITED_FINAL / POLICY / IP / BRAND / RECALL /
    FLAGGED / GATED;
  · `OTHER` 里的两个显式词条:`business decision` / `trust & safety`
    (⚠ `currently under review` **不算** —— 审查中是自愈态);
  · **`taxonomy_code` 为 NULL 的**(四处都找不到原文,判不出来)——
    所有者定的是**拉黑**:查不出理由就继续禁,不因为查不出而放行。

**删掉**:其余全部(PT_WRONG / CONTENT / INFO / PRICE / SYSTEM / STAGE /
EXPIRED / SPECIAL,以及 `OTHER` 的其他词条)。主体是 **PT_WRONG** ——
沃尔玛原话是「要重新上架请把 product type 选对」,那是修法不是禁令。

## 三道闸

1. **只路由回填过的行**:`taxonomy_version = <当前 ERROR_TAXONOMY_VERSION>`。
   没回填过的行拿不出新码,按陈旧信息删就是瞎删 —— 它们只进摘要点名,
   提示先跑 `error_reclass`;
2. **删前自动落备份**:被删的整行写 `<DATA_ROOT>/backups/blacklist_route_
   <时间戳>.jsonl`。所有者选的是"直接删行",备份不改变这一点,只是把
   溯源留在库外 —— 44,111 行的不可逆操作,值这一个文件;
3. `--dry-run` 一行不动,报的就是真跑会删什么。

⚠ **删完飞书还没同步**:黑名单投影是 `blacklist_push` 整表重写,不跑它的话
飞书那张表仍是旧的。摘要末尾会提醒。
"""

import json
import logging
import pathlib
from collections import Counter
from datetime import datetime, timezone

from registry import db, paths, resources
from services import blacklist, error_taxonomy

DANGEROUS = True        # 删行,不可逆;`--dry-run` 由 cli 接管

logger = logging.getLogger("workflows.blacklist_route")

_KNOWN_PARAMS = {"limit", "chunk"}
_CLI_INJECTED = {"execute", "dry_run", "store"}

#: 「行级留下」谓词 —— 与 `error_taxonomy.is_permanent` 同一份裁决,只是搬进
#: SQL(十万行逐行往返太慢)。三段依次是:七个永久码 / `OTHER` 的两个显式词条 /
#: 判不出来的行(所有者定:查不出理由就继续禁)。
#: ⚠ 改这三段 = 改业务口径,必须先过所有者,并同步 `is_permanent`。
#:
#: ⚠⚠ **这只是「行级记录」,不是判定**(所有者 2026-09-04:「其中被拉黑的那个
#: 作为最高优先级,其他的都是作为记录」)。`taxonomy_code` 只是**那一行入选时
#: 那条原文**的归类;该不该拉黑要看这个 asin 的**全部历史**。
#: 生产实证:上一轮按行级码删的 42,113 条里,**3,201 条按产品级判定该留**
#: (POLICY 2,703 / FLAGGED 429 / GATED 45 / IP 9 / PROHIBITED_FINAL 9 /
#: BRAND 6)—— 那些品历史上被真禁售拒过,只是入选那一行记的是可修复类。
#: 所以下面再加一道:**产品级说该拉黑的,一律不删**。
_KEEP = """(
    taxonomy_code = ANY(%(perm)s)
    OR (taxonomy_code = 'OTHER'
        AND lower(btrim(coalesce(taxonomy_term, ''))) = ANY(%(terms)s))
    OR taxonomy_code IS NULL
)"""

# 只看回填过的行(闸 1);没回填过的单独计数点名
_SQL_STALE = """
SELECT count(*) FROM catalog.asin_blacklist
WHERE taxonomy_version IS DISTINCT FROM %(ver)s
"""
_SQL_PLAN = """
SELECT taxonomy_code, taxonomy_term, (asin = ANY(%(spare)s)) AS spared,
       count(*) AS n
FROM catalog.asin_blacklist
WHERE taxonomy_version = %(ver)s
GROUP BY 1, 2, 3
"""
_SQL_DOOMED = """
SELECT asin, category, source, reason, src_store, biz_cn, src_sku,
       created_at, taxonomy_code, taxonomy_policy, taxonomy_term,
       taxonomy_src, taxonomy_version
FROM catalog.asin_blacklist
WHERE taxonomy_version = %(ver)s AND NOT """ + _KEEP + """
  -- ⚠ 产品级判定说该拉黑的一律不删(所有者:被拉黑的那个最高优先级)。
  --   ⚠ 这一条**必须放进 SQL**,不能取出来再在 Python 里过滤:循环是
  --     「取一批 → 删一批 → 再取」,过滤掉不删的行会被**反复取到**,
  --     那就是 §13.8 那个死循环的同款陷阱。
  AND asin <> ALL(%(spare)s)
ORDER BY asin
LIMIT %(chunk)s
"""
_SQL_DELETE = """
DELETE FROM catalog.asin_blacklist
WHERE asin = ANY(%(asins)s)
"""


def product_level_keep(conn) -> set:
    """输入:连接 → 输出:按**产品历史**判定该永久拉黑的 asin 集合(这些一律不删)。

    ⚠ 判据与回填**同一份**(`blacklist._judge_events`)。而那一份**不做判定,
    只做查询** —— 判定在 `problem_scan` 写事件那一刻就发生过,历史事件由
    `error_reclass -p scope=events` 回填成新码(所有者 2026-09-04:
    「产品级的记录已经有产品事件在做了」)。这里唯一的规则是取哪一条:
    **够格拉黑的那条最高优先级**。
    """
    return {r["asin"] for r in blacklist._judge_events(conn)}


def _parse(params: dict) -> tuple[int, int, bool]:
    """输入:params → 输出:(limit, chunk, execute)。未识别参数宁炸不吞。"""
    unknown = set(params) - _KNOWN_PARAMS - _CLI_INJECTED
    if unknown:
        raise ValueError(f"未识别参数 {sorted(unknown)}(可用:{sorted(_KNOWN_PARAMS)})")
    raw = str(params.get("limit", "")).strip()
    limit = int(raw) if raw else 0          # 0 = 不限量
    if raw and limit <= 0:
        raise ValueError(f"limit 要正整数,给的是 {raw!r};**不给就是不限量**")
    chunk = int(params.get("chunk", 5_000))
    if chunk <= 0:
        raise ValueError(f"chunk 要正整数,给的是 {chunk}")
    return limit, chunk, bool(params.get("execute"))


def keeps(code: str | None, term: str | None) -> bool:
    """输入:新码 + 显式词条 → 输出:这一行留不留(与 `_KEEP` 那段 SQL 同义)。

    纯函数,给守门测试用:**SQL 与 Python 两处必须同义**,漂了就是
    "报告说要删 A,实际删了 B",而两边看着都正常。
    """
    if code is None:                 # 判不出来 → 所有者定:继续禁
        return True
    return error_taxonomy.is_permanent(code, term)


def plan_lines(rows, ver: str, stale: int) -> tuple[list[str], Counter, Counter]:
    """输入:(code, term, spared, n) 行 → 输出:(摘要行, 将删分布, 将留分布)。纯拼装。

    三分,不是两分:
      · **行级就永久** → 留;
      · 行级不永久,但**产品级判定说该拉黑**(`spared`)→ 留 ——
        所有者 2026-09-04:「被拉黑的那个作为最高优先级,其他的都是作为记录」;
      · 两个都不永久 → 删。
    """
    doomed: Counter = Counter()
    kept: Counter = Counter()
    spared_c: Counter = Counter()
    for code, term, spared, n in rows:
        key = code or "(判不出)"
        if keeps(code, term):
            kept[key] += int(n)
        elif spared:
            spared_c[key] += int(n)        # 行级说删、产品级救回来
        else:
            doomed[key] += int(n)
    out = [f"存量黑名单按新码路由(码表 {ver};裁决表 docs/error_taxonomy.md §十二)",
           f"  **将删** {sum(doomed.values()):,} 条 / "
           f"**留下** {sum(kept.values()) + sum(spared_c.values()):,} 条"]
    if spared_c:
        out += [f"  ⚠ 其中 **{sum(spared_c.values()):,} 条是产品级判定救下的** ——"
                f"行级记录说可修复,但这个 asin 的**历史里有够格拉黑的那条**"
                f"(所有者:被拉黑的那个最高优先级):",
                *[f"    {n:>7}  行级 {c}" for c, n in spared_c.most_common(8)]]
    if doomed:
        out.append("  将删,按新码:")
        out += [f"    {n:>7}  {c}" for c, n in doomed.most_common()]
    if kept:
        out.append("  留下,按新码:")
        out += [f"    {n:>7}  {c}" for c, n in kept.most_common()]
    if stale:
        out.append(f"  ⚠ **另有 {stale:,} 条还没按当前码表回填**(taxonomy_version "
                   f"对不上),本轮**一条都不动** —— 先跑 `python cli.py "
                   f"error_reclass` 再来")
    return out, doomed, kept


def _backup_path():
    """输出:本轮备份文件路径。**整轮只算一次**(2026-09-04 实遇的坑)。

    ⚠ 原来时间戳是在 `_dump` 里算的,而 `_dump` 每批调一次、戳到秒 ——
    42,113 条分 9 批跑完只花了 1 秒多,于是备份**裂成了两个文件**
    (…042420Z 装前 30,000 条,…042421Z 装后 12,113 条),而摘要只报最后
    那一个。真要回滚的人照摘要去找,会少掉三万行**而且不会发现**。
    删数万行不可逆,备份是唯一的网 —— 一轮一个文件,路径在 run() 里定死。
    """
    paths.backups_dir().mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return paths.backups_dir() / f"blacklist_route_{stamp}.jsonl"


def _dump(rows, cols, path) -> str:
    """输入:待删行 + 本轮备份路径 → 输出:该路径(jsonl,一行一条,追加)。"""
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(dict(zip(cols, (
                v.isoformat() if hasattr(v, "isoformat") else v for v in r))),
                ensure_ascii=False) + "\n")
    return str(path)


_COLS = ("asin", "category", "source", "reason", "src_store", "biz_cn",
         "src_sku", "created_at", "taxonomy_code", "taxonomy_policy",
         "taxonomy_term", "taxonomy_src", "taxonomy_version")


def run(params: dict) -> str:
    """输入:params(limit/chunk)→ 输出:路由摘要。"""
    limit, chunk, execute = _parse(params)
    ver = resources.ERROR_TAXONOMY_VERSION
    args = {"ver": ver, "perm": sorted(error_taxonomy.PERMANENT_CODES),
            "terms": sorted(error_taxonomy.PERMANENT_UNLISTED_TERMS)}
    with db.pg_conn() as conn:
        # ⚠ 产品级判定**先算**,计划与实删共用这一份 —— 两处各算各的 =
        #   计划说删 3 万、实际删 4 万,而两边看着都正常。
        args["spare"] = sorted(product_level_keep(conn))
        logger.info("产品级判定该拉黑 %d 个 asin(这些一律不删)", len(args["spare"]))
        with conn.cursor() as cur:
            try:
                cur.execute(_SQL_STALE, {"ver": ver})
                stale = cur.fetchone()[0]
                cur.execute(_SQL_PLAN, {"ver": ver, "spare": args["spare"]})
                plan = cur.fetchall()
            except Exception as e:                          # noqa: BLE001
                # ⚠ 缺列不该甩一屏 traceback(2026-09-03 实遇:`taxonomy_term`
                #   没建就跑,人看到的是 psycopg.UndefinedColumn)。缺的是
                #   **前置步骤**,说清做什么比说清哪一行炸了有用得多。
                conn.rollback()
                if "does not exist" not in str(e):
                    raise
                raise RuntimeError(
                    f"读不到 `catalog.asin_blacklist` 的新码列({e})。"
                    "前置两步没做完 —— 依次跑:\n"
                    "  python cli.py db_init                    # 建 taxonomy_* 四列\n"
                    "  python cli.py error_reclass -p force=1   # 回填(force 是"
                    "必须的:版本号已盖章时增量谓词会把全部行排除,判 0 条)\n"
                    "然后再回来跑 blacklist_route。") from None
        out, doomed, _kept = plan_lines(plan, ver, stale)
        if not execute:
            return "\n".join(out + [
                "", "🧪 --dry-run:**一行都没删**;上面报的就是真跑会删什么"])
        if not sum(doomed.values()):
            return "\n".join(out + ["", "没有要删的行,库已经与新标准对齐。"])

        deleted = 0
        backup = str(_backup_path())      # 整轮一个文件,不是每批一个
        while True:
            take = chunk if not limit else min(chunk, limit - deleted)
            if take <= 0:
                break
            with conn.cursor() as cur:
                cur.execute(_SQL_DOOMED, {**args, "chunk": take})
                rows = cur.fetchall()
            if not rows:
                break
            _dump(rows, _COLS, pathlib.Path(backup))   # 闸 2:删前先落备份
            with conn.cursor() as cur:
                cur.execute(_SQL_DELETE, {"asins": [r[0] for r in rows]})
            conn.commit()
            deleted += len(rows)
            logger.warning("已删 %d 条(本批 %d);备份 %s", deleted, len(rows), backup)
    return "\n".join(out + [
        "", f"**已删 {deleted:,} 条**;整行备份落在 {backup}",
        "⚠ **飞书那张表还是旧的** —— 黑名单投影是 `blacklist_push` 整表重写,"
        "跑一遍它才同步。",
        "⚠ 拦截行为**从此变了**:这批 ASIN 不再被 L0 的 ASIN 闸与上架拦截挡住。"
        "建议跟一轮 `python cli.py audit_replay -p tag=<标签>` 看正例误伤水位。"])
