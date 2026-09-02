"""audit_replay — 回放评估:拿沃尔玛自己的裁决考现在这条审核链(只读评估)。

用法(**手动跑,不进调度**;规格 `docs/audit_step3_spec.md` §3.8):
  python cli.py audit_replay --dry-run                # 只抽样 + 报规模与预估成本,零 LLM
  python cli.py audit_replay                          # 真跑:反例 600 / 正例 400
  python cli.py audit_replay -p neg=200 -p pos=100    # 小样本先看形态(便宜)
  python cli.py audit_replay -p seed=7                # 换一批样本(同 seed 恒同一批)
  python cli.py audit_replay -p tag=20260902-改提示词后 # 自定义 run_tag(同 tag 重跑覆盖)
  python cli.py audit_replay -p limit_per_category=30 # 每个期望类别的封顶

**它回答一个问题**:换了判据之后,这条链对"沃尔玛已经裁决过的商品"判得对不对,
以及**有没有比旧链更爱误伤在售品**(所有者定稿 §六.5 的底线:正例误伤率不高于旧链)。

链路:抽样 → 对每个样本调 `services.audit_rules.audit_one`(**与生产同一条链、
同一份 ctx**,不复制任何判定逻辑)→ 与两个参照对齐 → 落 `audit.replay_results`
+ 报告文件 `<DATA_ROOT>/reports/audit_replay.txt`。

三方对照:
  · **沃尔玛裁决**(参照,非金标):`catalog.walmart_items.unpublished_reasons`
    经 `services/error_taxonomy` 归类,主码 ∈ POLICY/IP/CONTENT/BRAND/
    PROHIBITED_FINAL 的算反例(期望 reject);在架在售且从没有过下架原因的算
    正例(期望 pass)。PT_WRONG / GATED **不进本集**:前者是 L1 的题(类目选错),
    后者的沃尔玛语义(要预审批)与我方"中国卖家能不能做"不对齐;
  · **旧链**:`audit.audit_runs` 每个 asin **最近一次**的 verdict 与
    l3_reason_category(历史,已落库,不重跑旧代码);
  · **新链**:本轮 `audit_one` 的输出。

⚠ **写什么、不写什么**(这条工作流的全部写面):
  · 写 `audit.replay_results`(自己的表,主键 (run_tag, asin),同 tag 覆盖);
  · 写报告文件 `paths.audit_replay_report()`;
  · **`catalog.llm_cache` 会被判定链自己写**(L3 判完把出参缓存下来)——
    那是缓存不是结论,而且与生产共用一份:回放付过的钱,随后的真重审直接命中,
    不写反而是浪费。此外**一个字都不碰** `catalog.products` / `audit.audit_runs` /
    `audit.audit_hits` / `catalog.product_events` / 飞书。
  · `--dry-run`:只抽样、报规模与**预估成本**,零 LLM 调用、零落库、不写报告文件。

⚠ **已知局限**(规格 §3.8;报告头逐字重复一遍,读数前必须知道):
  ① 产品正文只有**当前值**(`catalog.products` 就地覆盖,`snapshots.raw` 已裁大
     文本)—— 被拒之后改过 listing 的品会失真;
  ② 沃尔玛裁决时的**政策版本与今天不同**;
  ③ 沃尔玛裁决是**参照不是金标**(申诉成功、自愈态都存在)。
所以指标要横向比(改判据前后同 seed 同样本对比),不要拿绝对值当准确率。
"""

import logging
import random
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from api import llm as _llm
from registry import db, paths, resources
from services import (audit_l3, audit_pool, audit_reason, audit_rules,
                      audit_store, db_guard, error_taxonomy, llm_cost,
                      sku_asin)

DANGEROUS = False       # 只写自己的表与报告文件(判定链另写 llm_cache,见头注)

logger = logging.getLogger("workflows.audit_replay")

# ── 参数 ─────────────────────────────────────────────────────────────────────
_KNOWN_PARAMS = {"neg", "pos", "seed", "tag", "workers", "limit_per_category"}
# cli 自己塞的键(与 product_audit 同款白名单:每加一个 cli 级开关都会重演
# 2026-08-16 那次「--dry-run 让工作流起不来」)
_CLI_INJECTED = {"execute", "dry_run"}

_DEFAULT_NEG = 600      # 所有者定稿 §六.5
_DEFAULT_POS = 400
_DEFAULT_SEED = 20260902    # 固定缺省 —— 同 seed 恒同一批样本,改判据前后可比
_MIN_CAP = 10           # 每类封顶的下限(类别多的时候别把稀有类别切成 1 条)

# ── 抽样面(内存与代表性的折中)────────────────────────────────────────────
# 抽样在**库里**做:`ORDER BY md5(sku || seed)` 是伪随机且**同 seed 恒定**,
# LIMIT 一挂,进 Python 的行数就封顶了 —— 而下架原因表是几十万行带长文本,
# 全拉回来是 2026-08-21 那次 OOM 的同款走法。
# 反例池要大:一行下架原因里只有一部分是本集要的五个码(PT_WRONG / GATED /
# 过期 / 未上线都会被筛掉),再经 sku→asin、库里有没有产品行两道筛。
_NEG_POOL_FACTOR = 40
_POS_POOL_FACTOR = 5        # 正例只掉 sku→asin 与产品行两道,池小得多
_POOL_MAX = 50_000

# 进本集的主码(规格 §3.8):PT_WRONG / GATED 不进(见头注)。
# 其中 POLICY / IP / CONTENT 三档能对到具体类别,BRAND / PROHIBITED_FINAL
# 只比判定(沃尔玛这两类正文里没有可对表的政策名)—— 分道在 `label()` 里。
_NEG_CODES = ("POLICY", "IP", "CONTENT", "BRAND", "PROHIBITED_FINAL")

# ── 成本预估口径(规格 §3.9;真跑用 llm_cost.summarize 实算)────────────────
# 前缀 token 由**实际渲染出来的 system prompt** 折算,不写死:政策表一改
# 前缀就变长,写死的数字会悄悄失真(B1 实测 212,556 字符 ≈ 6.1 万 token)。
_CHARS_PER_TOKEN = 3.5
_USER_TOKENS = 2500     # user 段:规格 §3.9 的 1.5–3K,取中位
_OUT_TOKENS = 1200      # 输出:规格 §3.9 的 ≤1.5K

_WRITE_BATCH = 500

# ── SQL(全部只读,唯一的写在 _INSERT_SQL)──────────────────────────────────
#
# ⚠ 反例面**不按在架/缺席过滤**:被沃尔玛下架的品多数已经缺席(missing_since
# 非空),按在架口径滤掉的话本集会只剩刚被拒还没消失的那一小撮。
# 一个 sku 可能在多家店都有行,`DISTINCT ON` 取一行(店铺不参与身份)。
_NEG_SQL = """
SELECT sku, reasons FROM (
    SELECT DISTINCT ON (w.sku) w.sku AS sku, w.unpublished_reasons AS reasons
    FROM catalog.walmart_items w
    WHERE coalesce(w.unpublished_reasons, '') <> ''
    ORDER BY w.sku, w.store
) t
ORDER BY md5(t.sku || %(seed)s)
LIMIT %(pool)s
"""

# 正例 = 在架在售且**任何一家店都没给过下架原因**。NOT EXISTS 那一条是必须的:
# 同一个 sku 在 A 店在架、在 B 店被拒是常态,只看本行会把反例算成正例。
_POS_SQL = """
SELECT sku FROM (
    SELECT DISTINCT ON (w.sku) w.sku AS sku
    FROM catalog.walmart_items w
    WHERE w.published_status = 'PUBLISHED'
      AND w.missing_since IS NULL
      AND coalesce(w.unpublished_reasons, '') = ''
      AND NOT EXISTS (SELECT 1 FROM catalog.walmart_items x
                      WHERE x.sku = w.sku
                        AND coalesce(x.unpublished_reasons, '') <> '')
    ORDER BY w.sku, w.store
) t
ORDER BY md5(t.sku || %(seed)s)
LIMIT %(pool)s
"""

# 库里有产品行且有标题的才进样本(无标题 = 采集降级,不够格判定 ——
# 与 product_audit 的候选口径同一条)
_HAS_PRODUCT_SQL = """
SELECT asin FROM catalog.products
WHERE marketplace = %(marketplace)s AND asin = ANY(%(asins)s)
  AND title IS NOT NULL AND title <> ''
"""

# 取判定入参:**行的形状与生产候选同源**(services.audit_rules 那两个常量)
_PRODUCT_SQL = ("SELECT " + audit_rules.PRODUCT_ROW_COLUMNS + "\n"
                + audit_rules.PRODUCT_ROW_FROM + """
WHERE p.marketplace = %(marketplace)s AND p.asin = ANY(%(asins)s)
  AND p.title IS NOT NULL AND p.title <> ''
""")

# 旧链最近一次结论(历史,不重跑)。排 SHORTCUT 影子行:那是历史短路留下的,
# 不是判过一次(与 product_audit._HISTORY_SQL 同一条理由)
_OLD_SQL = """
SELECT DISTINCT ON (asin) asin, verdict, l3_reason_category
FROM audit.audit_runs
WHERE asin = ANY(%(asins)s)
  AND stage_stopped_at IS DISTINCT FROM 'SHORTCUT'
ORDER BY asin, created_at DESC
"""

_INSERT_SQL = """
INSERT INTO audit.replay_results
  (run_tag, asin, expected_verdict, expected_category, got_verdict,
   got_category, got_detail, stage_stopped_at, old_verdict, old_category,
   confidence)
VALUES (%(run_tag)s, %(asin)s, %(expected_verdict)s, %(expected_category)s,
        %(got_verdict)s, %(got_category)s, %(got_detail)s,
        %(stage_stopped_at)s, %(old_verdict)s, %(old_category)s,
        %(confidence)s)
ON CONFLICT (run_tag, asin) DO UPDATE SET
    expected_verdict = EXCLUDED.expected_verdict,
    expected_category = EXCLUDED.expected_category,
    got_verdict = EXCLUDED.got_verdict,
    got_category = EXCLUDED.got_category,
    got_detail = EXCLUDED.got_detail,
    stage_stopped_at = EXCLUDED.stage_stopped_at,
    old_verdict = EXCLUDED.old_verdict,
    old_category = EXCLUDED.old_category,
    confidence = EXCLUDED.confidence,
    created_at = now()
"""


@dataclass
class Opts:
    """一轮回放的入参(`_parse_params` 一处解析、一处校验)。"""

    neg: int
    pos: int
    seed: int
    tag: str
    workers: int
    cap: int | None             # 每类封顶;None = 按 neg / 类别数 现算
    dry_run: bool
    conn_note: str = ""         # 连接余量钳制说明(db_guard 回填)


@dataclass
class Sample:
    """一个待回放的样本(期望值来自沃尔玛裁决,不是我们的结论)。"""

    asin: str
    expected_verdict: str           # reject / pass
    expected_category: str | None   # None = 只比判定不比类别
    stratum: str                    # 分层键(类别名 / 码 / '正例')
    source: str                     # 'neg' / 'pos'
    reason: str = ""                # 沃尔玛原文摘要(报告用,不落库)


def _parse_params(params: dict) -> Opts:
    """输入:params → 输出:Opts;未识别参数/非法值一律抛(宁炸不吞)。"""
    unknown = set(params) - _KNOWN_PARAMS - _CLI_INJECTED
    if unknown:
        raise ValueError(f"未识别参数 {sorted(unknown)}"
                         f"(可用:{sorted(_KNOWN_PARAMS)})")

    def _int(key, default):
        raw = params.get(key)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            v = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{key} 要整数(收到 {raw!r})") from None
        if v < 0:
            raise ValueError(f"{key} 不能为负(收到 {v})")
        return v

    neg, pos = _int("neg", _DEFAULT_NEG), _int("pos", _DEFAULT_POS)
    if not neg and not pos:
        raise ValueError("neg 与 pos 不能同时为 0(那是一轮什么都不做的回放)")
    cap = params.get("limit_per_category")
    cap = _int("limit_per_category", None) if cap not in (None, "") else None
    workers = max(1, _int("workers", resources.AUDIT_WORKERS_DEFAULT))
    workers = min(workers, resources.AUDIT_WORKERS_MAX)
    tag = str(params.get("tag", "")).strip() or (
        f"{datetime.now().strftime('%Y%m%d')}-{resources.AUDIT_RULES_VERSION}")
    return Opts(neg=neg, pos=pos, seed=_int("seed", _DEFAULT_SEED), tag=tag,
                workers=workers, cap=cap,
                dry_run=bool(params.get("dry_run")))


# ── 打标:沃尔玛裁决 → 期望值 ────────────────────────────────────────────────

def label(res, policy_names) -> tuple[str | None, str] | None:
    """输入:`error_taxonomy.classify_reasons` 结果 + 政策表名 →
    输出:(期望类别, 分层键);不进本集给 **None**。

    映射就是规格 §3.8 那张表,一条推断都没有:
      · POLICY → 抽出的政策名 join 政策表得到的**表内原拼写**;join 不上就
        **不进集**(没有可比的类别名;硬凑一个等于给自己送分);
      · IP → `Intellectual Property`(规则侧同一个常量,装配时已对过表);
      · CONTENT → 内容族两页之一(报错正文只说"内容不合规",不说是索引页还是
        明细页)—— 期望值记规范名,判在**任一页**都算对(`category_ok`);
      · BRAND / PROHIBITED_FINAL → 期望类别 None,**只比判定**:沃尔玛这两类
        正文里没有可对表的政策名(品牌授权是账号面的事,不可申诉是终局标记)。
    """
    code = res.code
    if code not in _NEG_CODES:
        return None
    if code == "POLICY":
        name = error_taxonomy.policy_join(res.policy_name, policy_names)
        return (name, name) if name else None
    if code == "IP":
        return resources.AUDIT_IP_POLICY, resources.AUDIT_IP_POLICY
    if code == "CONTENT":
        return resources.AUDIT_CONTENT_POLICIES[0], "内容族"
    return None, code


def category_ok(expected: str | None, got: str | None) -> bool:
    """输入:期望类别 + 本次类别 → 输出:算不算对(枚举**精确等值**)。

    唯一的松口是**内容族两名互认**(`AUDIT_CONTENT_POLICIES`):43 是索引页、
    44 是明细页,沃尔玛的报错正文不区分,判在哪一页都是判对了那件事。
    期望类别为 None(BRAND / PROHIBITED_FINAL)= 只比判定,恒真。
    """
    if not expected:
        return True
    if got == expected:
        return True
    fam = set(resources.AUDIT_CONTENT_POLICIES)
    return expected in fam and (got in fam)


# ── 抽样(纯函数,给定 seed 恒定)────────────────────────────────────────────

def stratify(rows: list, want: int, cap: int, seed) -> list:
    """输入:候选样本 + 目标条数 + 每类封顶 + 种子 → 输出:抽样结果(按 asin 定序)。

    分层抽样:先按期望类别分堆,每堆**独立种子**抽到封顶,再按总目标裁一次。

    ⚠ 每层一个独立种子(`f"{seed}:{层名}"`)不是花活:共用一个 RNG 时,某一层
    的候选量变了(库里多了几条下架记录)会让**后面所有层**抽到的样本整体错位,
    改判据前后就没法拿同一批样本对比了 —— 而那正是这条工作流唯一的用法。
    """
    if want <= 0:               # `-p neg=0` = 这轮不要反例(只跑正例误伤面)
        return []
    by: dict = {}
    for r in rows:
        by.setdefault(r.stratum, []).append(r)
    picked: list = []
    for key in sorted(by):
        pool = sorted(by[key], key=lambda r: r.asin)
        rnd = random.Random(f"{seed}:{key}")
        picked.extend(rnd.sample(pool, min(cap, len(pool))))
    picked.sort(key=lambda r: r.asin)
    if len(picked) > want:
        rnd = random.Random(f"{seed}:trim")
        picked = sorted(rnd.sample(picked, want), key=lambda r: r.asin)
    return picked


def sample_asins(asins, want: int, seed) -> list:
    """输入:候选 asin + 目标条数 + 种子 → 输出:抽样结果(去重、定序)。"""
    if want <= 0:               # `-p pos=0` = 这轮不要正例(只看反例召回)
        return []
    pool = sorted(set(asins))
    if len(pool) <= want:
        return pool
    return sorted(random.Random(f"{seed}:pos").sample(pool, want))


def default_cap(neg: int, strata: int) -> int:
    """输入:反例目标数 + 期望类别数 → 输出:每类封顶(下限 `_MIN_CAP`)。

    没有封顶的话,量最大的那一两类(生产上是通用政策拒与内容问题)会把整个
    样本吃满,而回放要看的恰恰是**各类别都判得怎么样**。
    """
    return max(_MIN_CAP, neg // max(1, strata))


# ── 取数 ────────────────────────────────────────────────────────────────────

def _pool_size(want: int, factor: int) -> int:
    return min(_POOL_MAX, max(want * factor, want + 100))


def _rows(conn, sql: str, args: dict) -> list:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _with_product(conn, asins: list) -> set:
    """输入:连接 + asin 列表 → 输出:库里**有产品行且有标题**的那些。"""
    if not asins:
        return set()
    rows = _rows(conn, _HAS_PRODUCT_SQL,
                 {"marketplace": "US", "asins": sorted(set(asins))})
    return {r[0] for r in rows}


def _negatives(conn, opts: Opts, policy_names: list) -> tuple[list, dict]:
    """输入:连接 + 入参 + 政策表名 → 输出:(反例候选 Sample 列表, 漏斗计数)。

    漏斗四道,每一道都计数进报告 —— 哪一道吃掉的最多决定了下一步该修什么:
    扫描面 → 主码在本集 → sku 提得出 asin → 库里有产品行(带标题)。
    """
    pool = _pool_size(opts.neg, _NEG_POOL_FACTOR)
    raw = _rows(conn, _NEG_SQL, {"seed": str(opts.seed), "pool": pool})
    st = {"scanned": len(raw), "pool_cap": pool, "coded": 0,
          "policy_unjoined": 0, "policy_noname": 0, "off_set": 0,
          "no_asin": 0, "no_product": 0}
    keep: list = []
    for sku, text in raw:
        res = error_taxonomy.classify_reasons(
            error_taxonomy.split_reasons(text), policy_names)
        lab = label(res, policy_names)
        if lab is None:
            if res.code == "POLICY" and res.policy_name:
                # 抽到了政策名却 join 不上表 —— 这是**政策表缺口**的信号;
                st["policy_unjoined"] += 1
            elif res.code == "POLICY":
                # 通用政策拒**抽不出类别名是常态**(生产 3,363 条,
                # `extract_policy` 头注),不是缺口:没有可比的类别,不进集。
                # 与上面那档分开数,否则一眼看去像是政策表烂了
                st["policy_noname"] += 1
            else:
                st["off_set"] += 1
            continue
        st["coded"] += 1
        keep.append((sku, lab, res.code, (text or "")[:200]))
    mapping, _ = sku_asin.resolve_skus(conn, [k[0] for k in keep])
    st["no_asin"] = sum(1 for k in keep if k[0] not in mapping)
    have = _with_product(conn, [mapping[k[0]] for k in keep if k[0] in mapping])
    out: list = []
    seen: set = set()
    for sku, (cat, stratum), _code, snippet in keep:
        asin = mapping.get(sku)
        if not asin:
            continue
        if asin not in have:
            st["no_product"] += 1
            continue
        if asin in seen:            # 同一 asin 多个 sku:只留一条
            continue
        seen.add(asin)
        out.append(Sample(asin=asin, expected_verdict="reject",
                          expected_category=cat, stratum=stratum,
                          source="neg", reason=snippet))
    return out, st


def _positives(conn, opts: Opts, exclude: set) -> tuple[list, dict]:
    """输入:连接 + 入参 + 要排除的 asin → 输出:(正例候选 asin, 漏斗计数)。

    `exclude` 是反例已占的 asin:同一个 asin 可能既有在架的 sku、又有被拒的
    sku(不同店/不同订货号),两边都收就是拿同一个产品既当正例又当反例。
    """
    pool = _pool_size(opts.pos, _POS_POOL_FACTOR)
    raw = _rows(conn, _POS_SQL, {"seed": str(opts.seed), "pool": pool})
    skus = [r[0] for r in raw]
    mapping, _ = sku_asin.resolve_skus(conn, skus)
    cand = {a for a in mapping.values() if a not in exclude}
    have = _with_product(conn, sorted(cand))
    st = {"scanned": len(raw), "pool_cap": pool, "no_asin": len(skus) - len(mapping),
          "dup_neg": len({a for a in mapping.values()} & exclude),
          "no_product": len(cand - have)}
    return sorted(have), st


def _load_products(conn, asins: list) -> dict:
    """输入:连接 + asin → 输出:{asin: ProductInfo}(行的形状与生产候选同源)。"""
    if not asins:
        return {}
    with conn.cursor() as cur:
        cur.execute(_PRODUCT_SQL,
                    {"marketplace": "US", "asins": sorted(set(asins))})
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {r["asin"]: audit_rules.product_info_from_row(r) for r in rows}


def _old_runs(conn, asins: list) -> dict:
    """输入:连接 + asin → 输出:{asin: (旧判定, 旧类别)}(最近一次,历史)。"""
    if not asins:
        return {}
    rows = _rows(conn, _OLD_SQL, {"asins": sorted(set(asins))})
    return {r[0]: (r[1], r[2]) for r in rows}


# ── 判定 ────────────────────────────────────────────────────────────────────

def pending_kind(outcome) -> str:
    """输入:AuditOutcome → 输出:pending 的来源(报告按它分层)。

    三种 pending 的处置完全不同:L1 解不出类目要补映射、L2 要补
    `walmart_pt_meta`、L3 的坏 JSON / 类别对不上枚举是**提示词的问题** ——
    混成一个"pending 率"就看不出该修哪儿。
    """
    stage = outcome.stage_stopped_at
    if stage == "L3" and outcome.l3 is not None:
        hits = [h.rule_code for h in outcome.l3.hits]
        return hits[0] if hits else "llm_unknown"
    if stage == "L2":
        return "L2 类目准入判不了"
    return "L1 类目解不出"


def _judge_all(conn, ctx, samples: list, products: dict,
               workers: int) -> dict:
    """输入:连接 + ctx + 样本 + 产品 → 输出:{asin: AuditOutcome or 异常}。

    与生产同一条链:`audit_one(run_l3=True, run_l4=False)`(L4 默认关,批复 #2),
    **不落 runs/hits、不写结论** —— 判完就把 AuditOutcome 留在内存里。
    并发形态照 `product_audit`:每个 worker 一条 autocommit 连接(判定期间
    连接被握着),首条串行预热省一批前缀缓存 miss(`services/audit_pool`)。
    """
    import queue as _queue
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from contextlib import ExitStack

    todo = [(s.asin, products[s.asin]) for s in samples if s.asin in products]
    out: dict = {}
    if not todo:
        return out
    # 样本比并发还少时按样本数开池:每个 worker 独占一条 PG 连接,拿 128 条
    # 连接去判 10 个样本,吃的是别的链的连接余量(小样本试跑是常态)
    workers = max(1, min(workers, len(todo)))
    with ExitStack() as stack:
        pool: _queue.SimpleQueue = _queue.SimpleQueue()
        for _ in range(workers):
            pool.put(stack.enter_context(db.pg_conn(autocommit=True)))

        def _judge(product):
            c = pool.get()
            try:
                return audit_rules.audit_one(product, ctx, c,
                                             run_l3=True, run_l4=False)
            finally:
                pool.put(c)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs, warmed = audit_pool.submit_chunk(ex, todo, _judge, warm=True)
            logger.info("回放判定 %d 条(预热 %d),并发 %d",
                        len(todo), warmed, workers)
            done = 0
            for fut in as_completed(futs):
                asin = futs[fut]
                try:
                    out[asin] = fut.result()
                except Exception as e:              # noqa: BLE001 —— 单行隔离
                    # 回放是评估不是生产:一条判炸了记下来继续,报告里点名。
                    # 整轮停掉的话前面几百条已付费的 LLM 结果一起白付。
                    logger.error("回放单行失败 asin=%s:%s", asin, e)
                    out[asin] = e
                done += 1
                if done % 100 == 0:
                    logger.info("回放进度 %d/%d", done, len(todo))
    return out


def _result_rows(samples: list, outcomes: dict, old: dict) -> list:
    """输入:样本 + 判定结果 + 旧链结论 → 输出:报告与落库共用的行 dict 列表。"""
    rows = []
    for s in samples:
        oc = outcomes.get(s.asin)
        failed = isinstance(oc, Exception)
        l3 = getattr(oc, "l3", None) if not failed else None
        old_v, old_c = old.get(s.asin, (None, None))
        rows.append({
            "asin": s.asin,
            "expected_verdict": s.expected_verdict,
            "expected_category": s.expected_category,
            "got_verdict": (None if oc is None or failed
                            else oc.verdict),
            "got_category": (None if oc is None or failed
                             else oc.final_reason_category),
            "got_detail": (None if oc is None or failed
                           else audit_store.conclusion_detail(oc)),
            "stage_stopped_at": (None if oc is None or failed
                                 else oc.stage_stopped_at),
            "old_verdict": old_v,
            "old_category": old_c,
            "confidence": getattr(l3, "confidence", None) if l3 else None,
            # 以下三个只进报告,不落库
            "stratum": s.stratum,
            "source": s.source,
            "reason": s.reason,
            "pending_kind": (pending_kind(oc)
                             if (not failed and oc is not None
                                 and oc.verdict == "pending") else None),
            "error": str(oc)[:200] if failed else None,
        })
    return rows


def _write_results(conn, run_tag: str, rows: list) -> int:
    """输入:连接 + run_tag + 结果行 → 输出:写入行数(同 tag 覆盖)。

    **本工作流唯一的落库**。判炸的行也写(`got_verdict` 为 NULL)——
    "这条判不出来"本身就是回放的结果之一,漏掉它样本量就对不上了。
    """
    cols = ("asin", "expected_verdict", "expected_category", "got_verdict",
            "got_category", "got_detail", "stage_stopped_at", "old_verdict",
            "old_category", "confidence")
    payload = [{"run_tag": run_tag, **{c: r[c] for c in cols}} for r in rows]
    with conn.cursor() as cur:
        for i in range(0, len(payload), _WRITE_BATCH):
            cur.executemany(_INSERT_SQL, payload[i:i + _WRITE_BATCH])
    return len(payload)


# ── 成本预估(dry-run 的主要产出)────────────────────────────────────────────

def estimate_cost(n: int, prefix_chars: int, model: str,
                  tier: str) -> tuple[float | None, int]:
    """输入:条数 + 前缀字符数 + 模型 + 峰谷时段 → 输出:(预估 USD 或 None, 前缀 token)。

    口径(规格 §3.9):**首条前缀未命中**(所以要串行预热,预热的就是它),
    其余命中;每条另加 user 段(按未命中价)与输出。
    不认识的模型返回 None —— 按 0 计价会产出一个看着像钱、其实是编的数字
    (与 `services/llm_cost` 同一条纪律)。
    """
    prefix = int(round(prefix_chars / _CHARS_PER_TOKEN))
    if n <= 0:
        return 0.0, prefix
    first = llm_cost.cost_of(model, tier, {
        "cache_miss": prefix + _USER_TOKENS, "completion": _OUT_TOKENS})
    if first is None:
        return None, prefix
    rest = llm_cost.cost_of(model, tier, {
        "cache_hit": prefix * (n - 1),
        "cache_miss": _USER_TOKENS * (n - 1),
        "completion": _OUT_TOKENS * (n - 1)}) or 0.0
    return first + rest, prefix


# ── 报告(纯拼装,零 I/O —— 拿假数据就能测)──────────────────────────────────

_LIMITS = [
    "已知局限(规格 §3.8;**读数前先看**,这三条决定了指标怎么读):",
    "  ① 产品正文只有**当前值**(catalog.products 就地覆盖,snapshots.raw 已裁"
    "大文本)—— 被拒之后改过 listing 的品会失真;",
    "  ② 沃尔玛裁决时的**政策版本与今天不同**;",
    "  ③ 沃尔玛裁决是**参照不是金标**(申诉成功、自愈态都存在)。",
    "  ⇒ 指标要**横向比**(改判据前后同 seed 同样本),别拿绝对值当准确率。",
]


def _pct(a: int, b: int) -> str:
    return f"{a}/{b} = {100.0 * a / b:.1f}%" if b else f"{a}/0 = —"


def _confusion(rows: list, limit: int) -> list:
    """输入:带类别的反例行 → 输出:混淆表文本行(期望 × 得到,按量降序)。"""
    from collections import Counter
    pairs: Counter = Counter()
    for r in rows:
        pairs[(r["expected_category"], r["got_category"] or "(无类别)")] += 1
    out = [f"  混淆表(期望 × 得到,前 {limit} 行;对角线是判对的):"]
    for (exp, got), n in pairs.most_common(limit):
        mark = "  ✓" if category_ok(exp, None if got == "(无类别)" else got) \
            else "  ✗"
        out.append(f"    {n:>5}{mark}  期望 {exp}  →  得到 {got}")
    return out


def report(rows: list, meta: dict, limit: int = 15) -> tuple[list, list]:
    """输入:结果行 + 本轮元信息 → 输出:(摘要行, 全文行);纯拼装,零 I/O。"""
    neg = [r for r in rows if r["source"] == "neg"]
    pos = [r for r in rows if r["source"] == "pos"]
    labelled = [r for r in neg if r["expected_category"]]
    judged = [r for r in rows if r["got_verdict"]]
    head = [
        f"回放评估 audit_replay(run_tag={meta['tag']};判据版本 "
        f"{resources.AUDIT_RULES_VERSION};seed={meta['seed']})",
        f"样本:反例 {len(neg)}(带类别 {len(labelled)})/ 正例 {len(pos)};"
        f"每类封顶 {meta['cap']};耗时 {meta.get('elapsed', 0):.0f}s",
        *_LIMITS,
    ]

    body: list = ["", "▍样本构成(反例按期望类别,前 %d)" % limit]
    strata = Counter(r["stratum"] for r in neg)
    for k, n in strata.most_common(limit):
        body.append(f"    {n:>5}  {k}")
    if len(strata) > limit:
        body.append(f"    …另有 {len(strata) - limit} 个类别")
    for name, st in (("反例", meta.get("neg_stats") or {}),
                     ("正例", meta.get("pos_stats") or {})):
        if st:
            body.append(f"  {name}漏斗:扫描 {st.get('scanned', 0)}"
                        f"(池上限 {st.get('pool_cap', 0)})"
                        + (f" → 主码在本集 {st['coded']}" if "coded" in st else "")
                        + f" → sku 提不出 asin {st.get('no_asin', 0)}"
                        + f" / 库里无产品行或无标题 {st.get('no_product', 0)}")
    ns = meta.get("neg_stats") or {}
    if ns.get("policy_noname"):
        body.append(f"  通用政策拒**抽不出类别名** {ns['policy_noname']} 条不进本集"
                    f"(常态,不是缺口:没有可比的类别名)")
    if ns.get("policy_unjoined"):
        body.append(f"  ⚠ 抽出政策名但 join 不上政策表 {ns['policy_unjoined']} 条"
                    f"(同样不进本集;这一档是**政策表缺口**的信号,"
                    f"清单见 error_reclass_report)")

    # ① 反例召回
    rec = [r for r in neg if r["got_verdict"] == "reject"]
    body += ["", f"▍反例召回(沃尔玛拒了,我们也拒):{_pct(len(rec), len(neg))}",
             f"    其中 pass {sum(1 for r in neg if r['got_verdict'] == 'pass')}"
             f" / pending {sum(1 for r in neg if r['got_verdict'] == 'pending')}"
             f" / 判定失败 {sum(1 for r in neg if r['error'])}"]

    # ② 类别准确率 + 混淆表
    hit = [r for r in labelled
           if r["got_verdict"] == "reject"
           and category_ok(r["expected_category"], r["got_category"])]
    body += ["", f"▍类别准确率(带类别反例 {len(labelled)} 条,枚举精确等值,"
                 f"内容族两名互认):{_pct(len(hit), len(labelled))}"]
    if labelled:
        body += _confusion(labelled, limit)

    # ③ 正例误伤:新旧并排(所有者的底线)
    new_fp = [r for r in pos if r["got_verdict"] == "reject"]
    pos_old = [r for r in pos if r["old_verdict"]]
    old_fp = [r for r in pos_old if r["old_verdict"] == "reject"]
    new_rate = len(new_fp) / len(pos) if pos else 0.0
    old_rate = len(old_fp) / len(pos_old) if pos_old else 0.0
    verdict_line = "新链 ≤ 旧链 ✓(所有者定稿的底线达标)"
    if pos and pos_old and new_rate > old_rate:
        verdict_line = ("⚠ **新链误伤高于旧链** —— 所有者定稿 §六.5 的底线是"
                        "「正例误伤率不高于旧链」,不达标就先修判据再回放,"
                        "别开 mode=stale")
    body += ["", f"▍正例误伤(在架在售却被判拒):"
                 f"新链 {_pct(len(new_fp), len(pos))};"
                 f"旧链 {_pct(len(old_fp), len(pos_old))}"
                 f"(旧链分母 = 正例里有历史 run 的那些)",
             f"    {verdict_line}"]

    # ④ 新旧一致率
    both = [r for r in rows if r["got_verdict"] and r["old_verdict"]]
    same = [r for r in both if r["got_verdict"] == r["old_verdict"]]
    body += ["", f"▍新旧链一致率(两边都有结论的 {len(both)} 条):"
                 f"{_pct(len(same), len(both))}"]

    # ⑤ 按 confidence 分层的错误率
    body += ["", "▍按 L3 置信分层(错 = 判定与沃尔玛裁决不一致):"]
    conf = Counter(r["confidence"] for r in rows if r["confidence"])
    for c in ("high", "medium", "low"):
        grp = [r for r in rows if r["confidence"] == c]
        if not grp:
            continue
        bad = [r for r in grp if r["got_verdict"] != r["expected_verdict"]]
        body.append(f"    {c:<7} {len(grp):>5} 条,判定不符 {_pct(len(bad), len(grp))}")
    if not conf:
        body.append("    (本轮没有一条走到 L3 —— 全被上游层判掉了?)")

    # ⑥ pending 率与来源
    pend = [r for r in rows if r["got_verdict"] == "pending"]
    body += ["", f"▍pending(判不了,不是判过了):{_pct(len(pend), len(rows))}"]
    for k, n in Counter(r["pending_kind"] for r in pend).most_common():
        body.append(f"    {n:>5}  {k}")
    fails = [r for r in rows if r["error"]]
    if fails:
        body.append(f"  ⚠ 判定失败 {len(fails)} 条(已落库,got_verdict 为空):"
                    + "; ".join(f"{r['asin']}:{r['error'][:60]}"
                                for r in fails[:5]))

    # ⑦ 成本与耗时
    body += ["", "▍成本与耗时"]
    body += [f"  {ln}" for ln in meta.get("cost_lines", [])]
    body.append(f"  判定 {len(judged)} 条,墙钟 {meta.get('elapsed', 0):.0f}s")

    # 全文另附逐条错判清单(摘要不放:几百行)
    full = list(head) + list(body)
    wrong = [r for r in neg if r["got_verdict"] != "reject"] + \
            [r for r in pos if r["got_verdict"] == "reject"]
    if wrong:
        full += ["", f"▍判定不符逐条({len(wrong)} 条;沃尔玛原文 200 字截断)"]
        for r in wrong:
            full.append(
                f"  {r['asin']}  期望 {r['expected_verdict']}"
                f"/{r['expected_category'] or '(只比判定)'}  →  "
                f"得到 {r['got_verdict'] or '(判定失败)'}"
                f"/{r['got_category'] or '(无类别)'}  停在 "
                f"{r['stage_stopped_at'] or '-'}")
            if r["reason"]:
                full.append(f"      沃尔玛:{r['reason']}")
            if r["got_detail"]:
                full.append(f"      我们:{r['got_detail'][:200]}")
    mis = [r for r in labelled
           if r["got_verdict"] == "reject"
           and not category_ok(r["expected_category"], r["got_category"])]
    if mis:
        full += ["", f"▍判拒但类别不符逐条({len(mis)} 条)"]
        for r in mis:
            full.append(f"  {r['asin']}  期望 {r['expected_category']}  →  "
                        f"得到 {r['got_category'] or '(无类别)'}")
            if r["got_detail"]:
                full.append(f"      我们:{r['got_detail'][:200]}")
    return head + body, full


# ── 入口 ────────────────────────────────────────────────────────────────────

def run(params: dict) -> str:
    """输入:params(neg/pos/seed/tag/workers/limit_per_category)→ 输出:评估摘要。"""
    opts = _parse_params(params)
    opts.workers, opts.conn_note = db_guard.cap_workers(opts.workers)
    t0 = time.monotonic()
    with db.pg_conn() as conn:
        # **与生产同一份 ctx**(黑名单/PT 字典/政策表实时集合都在里面)
        ctx = audit_rules.load_context(conn)
        policy_names = sorted(ctx.known_policies)
        if not policy_names:
            raise RuntimeError("政策表 audit.walmart_prohibited_policy 读不到"
                               "category_en —— 回放的期望类别全靠它 join,"
                               "空表跑出来的类别准确率是假的(先跑 policy_sync)")

        neg_pool, neg_stats = _negatives(conn, opts, policy_names)
        cap = opts.cap or default_cap(
            opts.neg, len({s.stratum for s in neg_pool}))
        picked = stratify(neg_pool, opts.neg, cap, opts.seed)
        pos_pool, pos_stats = _positives(
            conn, opts, exclude={s.asin for s in picked})
        pos_asins = sample_asins(pos_pool, opts.pos, opts.seed)
        samples = picked + [Sample(asin=a, expected_verdict="pass",
                                   expected_category=None, stratum="正例",
                                   source="pos") for a in pos_asins]
        products = _load_products(conn, [s.asin for s in samples])
        samples = [s for s in samples if s.asin in products]
        if not samples:
            # 一条样本都没有:空报告只会让人以为"跑过了"。把两条漏斗打出来 ——
            # 是库里没有下架记录、还是 sku 全都提不出 asin、还是产品行没采回来,
            # 三种情况的下一步完全不同
            return ("audit_replay:**一条样本都没抽到**,本轮什么都没做。\n"
                    f"  反例漏斗:{neg_stats}\n"
                    f"  正例漏斗:{pos_stats}\n"
                    "  (catalog.walmart_items 是空的?先跑 catalog_sync;"
                    "sku 提不出 asin 看 sku_normalize;产品行缺看 product_ingest)")

        # 前缀 token 用**实际渲染出来的** system prompt 折算(政策表一改就变);
        # 顺带在主线程把提示词构建完,省得 128 个线程同时抢那把锁
        prefix_chars = len(audit_l3.system_prompt(conn))
        model = _llm.model_for(audit_l3.L3_PURPOSE)
        tier = resources.llm_price_tier(datetime.now(timezone.utc))
        est, prefix_tok = estimate_cost(len(samples), prefix_chars, model, tier)
        est_line = (f"预估成本 ≈ ${est:.4f}" if est is not None
                    else f"⚠ 模型 {model} 没有计价(registry.LLM_PRICING 补一行),"
                         f"本轮不估钱")
        cost_head = (f"{est_line}(样本 {len(samples)} 条 × [前缀 {prefix_tok} "
                     f"token 命中价 + user {_USER_TOKENS} + 出 {_OUT_TOKENS}],"
                     f"首条前缀未命中;{tier} 时段价 —— 谷时段减半,"
                     f"大批回放排北京 18:00–次日 08:00)")

        if opts.dry_run:
            lines = [
                "🧪 audit_replay --dry-run(零 LLM、零落库、不写报告文件)",
                f"run_tag={opts.tag};seed={opts.seed};并发 {opts.workers}",
                f"样本:反例 {len(picked)} / 正例 {len(pos_asins)}"
                f"(每类封顶 {cap};反例池 {neg_stats['scanned']} 行 → 合格 "
                f"{len(neg_pool)},正例池 {pos_stats['scanned']} 行 → 合格 "
                f"{len(pos_pool)})",
                cost_head,
            ]
            if opts.conn_note:
                lines.append(opts.conn_note)
            by = Counter(s.stratum for s in picked)
            lines.append("反例按期望类别:" + "  ".join(
                f"{k}×{n}" for k, n in by.most_common(12)))
            lines.append("真跑:去掉 --dry-run(会调 LLM 并落 "
                         "audit.replay_results;结论表一个字都不碰)")
            return "\n".join(lines)

        # 真跑:计数器每轮清零(跨轮累加的数字读起来像"这一轮坏了这么多")
        _llm.reset_retry_stats()
        _llm.reset_usage_stats()
        audit_l3.reset_stats()
        audit_reason.reset_stats()
        outcomes = _judge_all(conn, ctx, samples, products, opts.workers)
        old = _old_runs(conn, [s.asin for s in samples])
        rows = _result_rows(samples, outcomes, old)
        written = _write_results(conn, opts.tag, rows)
        conn.commit()

    elapsed = time.monotonic() - t0
    cost_lines = [cost_head, *llm_cost.summarize(_llm.USAGE_STATS,
                                                 items=len(rows))]
    meta = {"tag": opts.tag, "seed": opts.seed, "cap": cap,
            "elapsed": elapsed, "neg_stats": neg_stats, "pos_stats": pos_stats,
            "cost_lines": cost_lines}
    summary, full = report(rows, meta)
    paths.reports_dir().mkdir(parents=True, exist_ok=True)
    path = paths.audit_replay_report()
    path.write_text("\n".join(full) + "\n", encoding="utf-8")
    tail = [f"落库 audit.replay_results {written} 行(run_tag={opts.tag},"
            f"同 tag 重跑覆盖);**结论表一个字都没碰**",
            f"▍全文报告(含逐条错判清单)→ {path}"]
    if opts.conn_note:
        tail.insert(0, opts.conn_note)
    return "\n".join(summary + [""] + tail)
