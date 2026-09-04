"""audit_replay — 回放评估:拿沃尔玛自己的裁决考现在这条审核链(只读评估)。

用法(**手动跑,不进调度**;规格 `docs/audit_step3_spec.md` §3.8):
  python cli.py audit_replay --dry-run                # 只抽样 + 报规模与预估成本,零 LLM
  python cli.py audit_replay                          # 真跑:反例 600 / 正例 400
  python cli.py audit_replay -p neg=200 -p pos=100    # 小样本先看形态(便宜)
  python cli.py audit_replay -p seed=7                # 换一批样本(新 tag 才会抽样)
  python cli.py audit_replay -p tag=改提示词前         # 首次:抽样 + 落库
  python cli.py audit_replay -p tag=改提示词前         # 再跑:**重放同一批 asin**,原地覆盖
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
  · **旧链**:`audit.audit_runs` 每个 asin 最近一次 **`audit_version` 不是当前
    `AUDIT_RULES_VERSION`** 的 verdict 与 l3_reason_category(历史,已落库,
    不重跑旧代码;NULL 也算旧链)。⚠ 那道版本谓词是命门:`mode=stale` 一跑,
    "最近一次 run"就是新链自己刚写的行,不排掉就是自己跟自己比,而且数字
    看着完全正常(`audit_version` 列 2026-09-02 B2 为此补进 audit_runs);
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
所以指标要横向比(改判据前后**同 tag** 重放同一批),不要拿绝对值当准确率。

⚠ **样本身份是 `run_tag`,不是 `seed`**:seed 只保证"同一份候选面上抽同一批",
而候选面 `catalog.walmart_items` 每天被 `catalog_sync` 重写(上下架、缺席、新店),
`ORDER BY md5(sku || seed) LIMIT` 的窗口跟着天天变 —— 隔天同 seed 已经不是同一批,
而两份报告长得一模一样。所以:**同 tag 已有行 ⇒ 重放那一批 asin(不重新抽样)**,
换一批就换 tag。
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
# ⚠ 只引函数不引模块名:本文件里 `policy_names` 是**政策表名字列表**这个局部
#   变量(与 error_taxonomy 的形参同名),模块名撞上去会同名两义
from services.policy_names import norm_category, resolve as resolve_policy

DANGEROUS = False       # 只写自己的表与报告文件(判定链另写 llm_cache,见头注)

logger = logging.getLogger("workflows.audit_replay")

# ── 参数 ─────────────────────────────────────────────────────────────────────
_KNOWN_PARAMS = {"neg", "pos", "seed", "tag", "workers", "limit_per_category",
                 "pos_days"}
# cli 自己塞的键(与 product_audit 同款白名单:每加一个 cli 级开关都会重演
# 2026-08-16 那次「--dry-run 让工作流起不来」)
_CLI_INJECTED = {"execute", "dry_run"}

_DEFAULT_NEG = 600      # 所有者定稿 §六.5
_DEFAULT_POS = 400
#: 正例最少在线天数(所有者 2026-09-04):沃尔玛的沉默要够久才算证据。
#: 180 天 ≈ 趟过两轮季度巡检;拿不到量就 `-p pos_days=N` 调,别默默放宽。
_DEFAULT_POS_DAYS = 180
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
#: 结论**由判据之外的层给出**的那些 stage(硬拒停在判据之前,或 L4 视觉改判)。
_NON_JUDGE_STAGES = ("L0", "L1", "L2", "L4")

#: 三个证据桶的中文名(报告用;`evidence_kind` 的返回值 → 这里)。
_KIND_CN = {"rules": "L0/L1/L2 规则与记忆", "brand": "L3 品牌翻拒(确定性后处理)",
            "policy": "L3 政策判据"}

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

# 正例 = **在线够久**、在架在售、且**从来没被沃尔玛报错过**。
#
# ⚠ 所有者 2026-09-04 定的口径:「拿一些在线时间长、且从未被沃尔玛报错的产品
#   当作正例,这个算比较有说服力的」。为什么这一条是判据而不是口味:
#   正例这一侧**沃尔玛没有留下任何证据** —— 它只是"没拒过"。而对一条昨天才
#   上架的 listing,"没拒过"几乎不含信息(沃尔玛根本还没看过它)。
#   **曝光时长才是把沉默变成证据的那个量**:一个品在架 N 天没被摘,说明它
#   真的趟过了沃尔玛的巡检。天数由 `-p pos_days=N` 给,缺省 180。
#
# ⚠ `created_at` 是**我们第一次同步到这行**的时间,不是沃尔玛的上架时间 ——
#   它是在线时长的**下界**(新接的店会让老 listing 看着"年轻")。下界正是
#   我们要的:宁可把老品当新品排掉,不可把新品当老品放进来。
#   跨店取 `min`:同一个 sku 在多店有行,取最早那条还活着的。
# NOT EXISTS 那一条照旧必须:同一个 sku 在 A 店在架、在 B 店被拒是常态,
# 只看本行会把反例算成正例。
_POS_SQL = """
SELECT sku, age_days FROM (
    SELECT w.sku AS sku,
           (extract(epoch FROM now() - min(w.created_at)) / 86400)::int AS age_days
    FROM catalog.walmart_items w
    WHERE w.published_status = 'PUBLISHED'
      AND w.missing_since IS NULL
      AND coalesce(w.unpublished_reasons, '') = ''
      AND NOT EXISTS (SELECT 1 FROM catalog.walmart_items x
                      WHERE x.sku = w.sku
                        AND coalesce(x.unpublished_reasons, '') <> '')
    GROUP BY w.sku
    HAVING (extract(epoch FROM now() - min(w.created_at)) / 86400)::int
           >= %(min_days)s
) t
ORDER BY md5(t.sku || %(seed)s)
LIMIT %(pool)s
"""

#: 同一口径**去掉天数闸**的总量 + **全库最老的是多少天**。
#: 天数闸砍到零时,光说"砍光了"没用 —— 要能直接读出"你要 180 天,而全库最老的
#: 才 N 天",人才知道是把闸调小还是这个判据根本不成立。
_POS_POOL_TOTAL_SQL = """
SELECT count(*), coalesce(max(age_days), 0) FROM (
    SELECT w.sku,
           (extract(epoch FROM now() - min(w.created_at)) / 86400)::int AS age_days
    FROM catalog.walmart_items w
    WHERE w.published_status = 'PUBLISHED'
      AND w.missing_since IS NULL
      AND coalesce(w.unpublished_reasons, '') = ''
      AND NOT EXISTS (SELECT 1 FROM catalog.walmart_items x
                      WHERE x.sku = w.sku
                        AND coalesce(x.unpublished_reasons, '') <> '')
    GROUP BY w.sku
) t
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

# 旧链基线 = 每个 asin **最近一次「不是当前判据版本」的** run(历史,不重跑)。
#
# ⚠ `audit_version IS DISTINCT FROM %(current)s` 这一条是命门(2026-09-02 B2
# 补的列):没有它,`product_audit -p mode=stale` 跑过之后每个 asin 的"最近
# 一次 run"就是**新链自己刚写的那一行** —— 回放于是拿新链跟新链比,误伤率
# 一致率全部漂亮,而没有任何东西会红。`NULL` 也算旧链(204 万存量行没有版本)。
# 排 SHORTCUT 影子行:那是历史短路留下的,不是判过一次(与
# product_audit._HISTORY_SQL 同一条理由)。
_OLD_SQL = """
SELECT DISTINCT ON (asin) asin, verdict, l3_reason_category
FROM audit.audit_runs
WHERE asin = ANY(%(asins)s)
  AND stage_stopped_at IS DISTINCT FROM 'SHORTCUT'
  AND audit_version IS DISTINCT FROM %(current)s
ORDER BY asin, created_at DESC
"""

# 同 tag 重放:样本直接读既有行(见 `_stored_samples` 头注)
_TAG_ROWS_SQL = """
SELECT asin, expected_verdict, expected_category
FROM audit.replay_results
WHERE run_tag = %(tag)s
ORDER BY asin
"""

# **任何一家店给过下架原因**的 sku(只取 sku 一列,不带长文本)。
# ⚠ 这里**不设上限**,而且不许设:它是正例的"干净"判据,漏一行就是把一个
# 沃尔玛拒过的品当成在架好品去算误伤率 —— 抽样面可以封顶(抽不到就是没抽到),
# 判据面不行。只取一列 text,生产是几万行的量级,内存与拉全表带正文不是一回事。
_REJECTED_SKU_SQL = """
SELECT DISTINCT sku FROM catalog.walmart_items
WHERE coalesce(unpublished_reasons, '') <> ''
"""

#: **历史**报错账本(`walmart_items.unpublished_reasons` 是就地覆盖的当前值,
#: 半年前被拒、后来改好的品在那一列上看着是干净的 —— 而它显然不该当正例)。
#: 这张表天生带 asin,不用过 sku_asin。
_EVER_FLAGGED_SQL = """
SELECT DISTINCT asin FROM audit.walmart_error_records
WHERE coalesce(asin, '') <> ''
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
    pos_days: int               # 正例最少在线天数(沃尔玛的沉默要够久才算证据)
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
    raw_cap = params.get("limit_per_category")
    cap = None
    if raw_cap not in (None, ""):
        cap = _int("limit_per_category", None)
        if cap == 0:
            # 静默落回缺省 = 人以为"这轮不封顶",实际按 neg/类别数 封了
            # (`opts.cap or default_cap(...)` 把 0 和"没传"当成同一件事)
            raise ValueError("limit_per_category 要 ≥1(每类至少留一条);"
                             "不想被封顶砍到就把 neg 调大,别传 0")
    workers = max(1, _int("workers", resources.AUDIT_WORKERS_DEFAULT))
    workers = min(workers, resources.AUDIT_WORKERS_MAX)
    tag = str(params.get("tag", "")).strip() or (
        f"{datetime.now().strftime('%Y%m%d')}-{resources.AUDIT_RULES_VERSION}")
    return Opts(neg=neg, pos=pos, seed=_int("seed", _DEFAULT_SEED), tag=tag,
                workers=workers, cap=cap,
                pos_days=_int("pos_days", _DEFAULT_POS_DAYS),
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
        明细页)—— 期望值取**表内原拼写**(`policy_names.resolve` 对
        `ctx.known_policies` 解一次,不把常量原样吐出来:判定链落库的是表内
        拼写,期望值拿常量拼写去比就会整类归零),判在**任一页**都算对
        (`category_ok`);表里没有这两页 → 不进集(没有可比的类别名);
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
        known = set(policy_names)
        for name in resources.AUDIT_CONTENT_POLICIES:
            hit = resolve_policy(name, known)
            if hit:
                return hit, "内容族"
        return None
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
    # 两名互认按**归一化键**比(`policy_names.norm_category`,全仓唯一一份):
    # 期望值来自政策表、`got` 来自判定链,两边都是表内拼写,但大小写/词形的
    # 差别不该被算成"类别判错"
    fam = {norm_category(n) for n in resources.AUDIT_CONTENT_POLICIES}
    return norm_category(expected) in fam and norm_category(got or "") in fam


def evidence_kind(row) -> str | None:
    """输入:一行结果 → 输出:这条结论**是谁给的**(rules / brand / policy);没结论给 None。

    ⚠ **不许按 `got_category` 的字符串分道** —— 2026-09-03 查出的度量失效:
    上游硬拒**自报的就是真政策名**,一律 ≠ `内部黑名单`,于是一路混进
    「判据」的分子与分母,而它们**没有一条读过那 44 篇原文**:

      · `audit_phase0` 品牌黑名单 / 商标符号 / 专利自述 → `Intellectual Property`
        (`audit_phase0.py:454 / :270 / :315`);
      · Made in USA → `Product claims`(`:375`);
      · 亚马逊类目黑名单**能 join 上政策表时,category 被改写成那条真政策名**
        (`:169-176`)—— 同一张表,一部分行进记忆桶、一部分行进判据桶,
        分界线竟是"黑名单行里有没有填对政策名";
      · L1 出版物硬禁与 L2 两条类目准入 → `类目准入`
        (`audit_l1_llm.py:129`、`audit_l2.py:179/:199`)。

    反方向也漏:`AUDIT_NONPOLICY_CATEGORIES` 被喂进了 L3 自己的候选枚举与
    白名单(`audit_l3.py:368 / :743`),**走完 44 篇全文**却答 `内部黑名单` 的行
    会被当成记忆,从分子分母里一起踢掉 ⇒ 分母偏小。

    所以分道只认两样**事实**,都早就落库(`audit.replay_results`):
      1. `stage_stopped_at` —— 停在判据之前(L0/L1/L2)或之后(L4)的,不是判据;
      2. `confidence` —— 「没走 L3 为 NULL」(`schema.sql:1811`),它是"到过判据"
         的唯一可靠标记(L3 判 pass 时 `stage_stopped_at` 是 None,不是 'L3')。

    L3 内部再分一次:**品牌翻拒是确定性后处理,不是模型的政策判断**,两者同为
    `stage_stopped_at='L3'`、同为 `Intellectual Property`,只有 detail 的固定句式
    能分 —— 常量出自 `audit_l3.BRAND_OVERRIDE_PREFIX`(那边写明了它是接口)。
    """
    if row.get("error") or not row.get("got_verdict"):
        return None
    if row.get("stage_stopped_at") in _NON_JUDGE_STAGES:
        return "rules"
    if row.get("confidence") is None:
        return "rules"                      # 判据一步都没走
    if (row.get("got_detail") or "").startswith(audit_l3.BRAND_OVERRIDE_PREFIX):
        return "brand"
    return "policy"


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


def rejected_asins(conn) -> set:
    """输入:连接 → 输出:**任何一家店给过下架原因**的全部 asin(经 sku_asin 规则)。

    正例的"干净"判据,`_POS_SQL` 里那条 `NOT EXISTS` 挡不住的那一半:
    身份是 **asin 级**,而 `walmart_items` 的行是 **sku 级** —— 同一个产品在
    A 店的订货号 `XKJ-B0X-39.98` 在架、在 B 店的 `YP-B0X-88.00` 被拒,两条行
    的 `sku` 根本不相等,SQL 自己比不出来。比不出来的后果不是报错:那个品会
    以正例身份进样本,新链判拒它反而被算成"误伤",直接污染所有者唯一的底线指标。

    所以在 Python 里按**唯一那份规则**(`services/sku_asin`)把下架侧的 sku
    全量折成 asin。只取 `sku` 一列(不带下架原因长文本),生产是几万行的量级。
    ⚠ **不设上限,也不许设**:抽样面可以封顶(抽不到就是没抽到),判据面封顶
    等于随机漏掉几个"其实被拒过"的品,而且不会报错。

    ⚠ 并上 `audit.walmart_error_records` 的**历史**账本(2026-09-04):
    `unpublished_reasons` 是**就地覆盖的当前值**,半年前被拒、后来改好的品在
    那一列上看着干干净净 —— 而所有者要的是「**从未**被沃尔玛报错」。
    只查当前值就会把这批品当成正例,而它们恰恰是最该被拒的那类。
    ⚠ 已知缺口:`catalog.product_events` 里的历史下架事件还没并进来
    (那张表的键是 sku/asin 混装,要过一次 sku_asin;暂不做,记在这里)。
    """
    rows = _rows(conn, _REJECTED_SKU_SQL, {})
    skus = [r[0] for r in rows if r[0]]
    mapping, _ = sku_asin.resolve_skus(conn, skus)
    ever = {r[0] for r in _rows(conn, _EVER_FLAGGED_SQL, {}) if r[0]}
    return set(mapping.values()) | ever


def _positives(conn, opts: Opts, neg_asins: set, rejected: set) -> tuple[list, dict]:
    """输入:连接 + 入参 + 反例池 asin + 曾被拒 asin → 输出:(正例候选, 漏斗计数)。

    两道排除各记各的账(合成一个数就看不出哪一道在起作用):
      · `neg_asins` = **整个反例池**(不只是抽中的那 600 条)—— 同一个 asin
        既当正例又当反例是自相矛盾的样本;
      · `rejected` = 任何一家店给过下架原因、**或历史报错账本里出现过**的
        asin(见 `rejected_asins`)。

    ⚠ 另有一道在 SQL 里(`_POS_SQL`):**在线不足 `pos_days` 天的不进正例**。
    正例这一侧沃尔玛没留下任何证据,"没拒过"对一条新 listing 几乎不含信息 ——
    曝光时长才是把沉默变成证据的那个量(所有者 2026-09-04 定)。
    漏斗把「干净在架总数」与「够天数的」都报出来:后者只剩零头就说明
    `created_at` 不是真的上架时间(整表重建过之类),那个"在线 N 天"是假的。
    """
    pool = _pool_size(opts.pos, _POS_POOL_FACTOR)
    raw = _rows(conn, _POS_SQL,
                {"seed": str(opts.seed), "pool": pool,
                 "min_days": opts.pos_days})
    total, oldest = _rows(conn, _POS_POOL_TOTAL_SQL, {})[0]
    skus = [r[0] for r in raw]
    ages = [r[1] for r in raw if r[1] is not None]
    mapping, _ = sku_asin.resolve_skus(conn, skus)
    cand = set(mapping.values())
    st = {"scanned": len(raw), "pool_cap": pool,
          "min_days": opts.pos_days, "clean_total": total,
          "pool_oldest": oldest,          # 全库最老的(不受天数闸影响)
          # ⚠ 空集给 None 不给 0:0 会在报告上显示成"中位 0 天",
          #   看着像"入选品都是当天上架的",而实际是**一条都没入选**
          "age_med": sorted(ages)[len(ages) // 2] if ages else None,
          "age_max": max(ages) if ages else None,
          "no_asin": len(skus) - len(mapping),
          "dup_neg": len(cand & neg_asins)}
    cand -= neg_asins
    st["ever_rejected"] = len(cand & rejected)
    cand -= rejected
    have = _with_product(conn, sorted(cand))
    st["no_product"] = len(cand - have)
    return sorted(have), st


def _stored_samples(conn, tag: str) -> list:
    """输入:连接 + run_tag → 输出:该 tag 已有的样本(期望值原样取回;没有给空)。

    **同 tag 重放 = 重放同一批 asin,不重新抽样**(2026-09-02 B2 复核修订)。
    理由:`seed` 只保证"同一份候选面上抽同一批",而候选面是
    `catalog.walmart_items` —— `catalog_sync` 每天重写它(上下架、缺席、新店),
    `ORDER BY md5(sku || seed) LIMIT` 的那个窗口跟着天天变。于是"改判据前后
    同 seed 对比"在**隔天**就不成立了,而两份报告长得一模一样,没有任何提示。

    有了这条:第一次跑某个 tag 抽样并落库,之后同 tag 再跑就按库里那批 asin
    逐条重判、原地覆盖结果 —— 前后两份报告比的确实是同一批产品。
    要换一批就换 tag(或换 seed 配新 tag)。
    """
    rows = _rows(conn, _TAG_ROWS_SQL, {"tag": tag})
    out = []
    for asin, exp_v, exp_c in rows:
        out.append(Sample(
            asin=asin, expected_verdict=exp_v,
            expected_category=exp_c,
            # 分层键库里没存(它只是报告的分组维度):带类别的按类别归堆,
            # 只比判定的两档归一堆,正例归正例
            stratum=("正例" if exp_v == "pass" else (exp_c or "(只比判定)")),
            source=("pos" if exp_v == "pass" else "neg")))
    return out


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
    """输入:连接 + asin → 输出:{asin: (旧判定, 旧类别)}(最近一次**旧链**行)。

    "旧链"= `audit_version` 不是当前 `AUDIT_RULES_VERSION` 的行(NULL 也算)。
    见 `_OLD_SQL` 头注:不排掉当前版本就是新链自己跟自己比。
    """
    if not asins:
        return {}
    rows = _rows(conn, _OLD_SQL, {"asins": sorted(set(asins)),
                                  "current": resources.AUDIT_RULES_VERSION})
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
    "  ⇒ 指标要**横向比**(改判据前后拿**同一个 run_tag** 重放同一批样本),"
    "别拿绝对值当准确率。",
]


def _pct(a: int, b: int) -> str:
    return f"{a}/{b} = {100.0 * a / b:.1f}%" if b else f"{a}/0 = —"


def _confusion(rows: list, limit: int) -> list:
    """输入:带类别的反例行 → 输出:混淆表文本行(期望 × 得到,按量降序)。"""
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
    origin = ("样本取自 run_tag 既有行(**未重新抽样**,与上一次同 tag 的报告"
              "逐条可比)" if meta.get("reused")
              else f"样本本轮抽样(seed={meta['seed']};每类封顶 {meta['cap']})")
    head = [
        f"回放评估 audit_replay(run_tag={meta['tag']};判据版本 "
        f"{resources.AUDIT_RULES_VERSION})",
        f"样本:反例 {len(neg)}(带类别 {len(labelled)})/ 正例 {len(pos)};"
        f"{origin};耗时 {meta.get('elapsed', 0):.0f}s",
        f"旧链基线 = 每个 asin 最近一次 **audit_version 不是 "
        f"{resources.AUDIT_RULES_VERSION}** 的 `audit.audit_runs` 行"
        f"(NULL 也算旧链;不排掉当前版本的话,mode=stale 跑过之后就是"
        f"新链自己跟自己比,而数字看着完全正常)",
        *_LIMITS,
    ]
    # ⚠ 要了正例却一条都没抽到:**底线指标整个缺席**,而下面每一节都照常打印,
    #   一眼扫过去像是"跑完了"。这种缺席必须顶到首行(与 store_absence
    #   「摘要首行点名」同一条纪律)。
    if meta.get("pos_wanted") and not pos:
        head.insert(1, f"⚠ **要了 {meta['pos_wanted']} 条正例,实际一条都没抽到**"
                       f" —— 正例误伤(所有者的底线)这一轮**没有数**,"
                       f"下面的反例读数照常看,底线那一栏当缺席处理")

    body: list = ["", "▍样本构成(反例按期望类别,前 %d)" % limit]
    strata = Counter(r["stratum"] for r in neg)
    for k, n in strata.most_common(limit):
        body.append(f"    {n:>5}  {k}")
    if len(strata) > limit:
        body.append(f"    …另有 {len(strata) - limit} 个类别")
    for name, st in (("反例", meta.get("neg_stats") or {}),
                     ("正例", meta.get("pos_stats") or {})):
        if st:
            if name == "正例" and st.get("min_days"):
                body.append(
                    f"  **正例口径**:在线 ≥ {st['min_days']} 天 且**从未**被沃尔玛"
                    f"报错(含历史账本)—— 干净在架 {st.get('clean_total', 0):,} 个,"
                    f"其中**最老的 {st.get('pool_oldest', 0)} 天**")
                if st["scanned"]:
                    body.append(
                        f"    入选品在线**中位 {st['age_med']} 天 / "
                        f"最长 {st['age_max']} 天**")
                # ⚠ 分两档说,因为下一步完全不同:
                #   一条都没过 = 闸比全库最老的还大,调闸就行;
                #   过了但只剩零头 = 数据本身可疑,调闸也救不回可信度。
                if not st["scanned"]:
                    body.append(
                        f"    ⚠ **一条正例都没入选**:天数闸 {st['min_days']} 天 > "
                        f"全库最老的 {st.get('pool_oldest', 0)} 天。"
                        f"`created_at` 是**我们第一次同步到这行**的时间,不是沃尔玛"
                        f"的上架时间 —— 表本身没那么老时,它就到不了 180 天。"
                        f"先查真实分布再定闸:`-p pos_days=N`(N 要 ≤ "
                        f"{st.get('pool_oldest', 0)})")
                elif (st.get("clean_total")
                      and st["scanned"] < st["clean_total"] * 0.02):
                    body.append(
                        "    ⚠ **够天数的不到干净在架的 2%** —— `created_at` 多半"
                        "挤在整表重建那天,那么「在线 N 天」这个判据是假的,"
                        "别拿这一轮的误伤下结论")
            body.append(f"  {name}漏斗:扫描 {st.get('scanned', 0)}"
                        f"(池上限 {st.get('pool_cap', 0)})"
                        + (f" → 主码在本集 {st['coded']}" if "coded" in st else "")
                        + f" → sku 提不出 asin {st.get('no_asin', 0)}"
                        + f" / 库里无产品行或无标题 {st.get('no_product', 0)}"
                        + (f" / 已被反例占用 {st['dup_neg']}"
                           if st.get("dup_neg") else "")
                        + (f" / **曾被沃尔玛报错过** {st['ever_rejected']}"
                           if st.get("ever_rejected") else ""))
    ns = meta.get("neg_stats") or {}
    if ns.get("policy_noname"):
        body.append(f"  通用政策拒**抽不出类别名** {ns['policy_noname']} 条不进本集"
                    f"(常态,不是缺口:没有可比的类别名)")
    if ns.get("policy_unjoined"):
        body.append(f"  ⚠ 抽出政策名但 join 不上政策表 {ns['policy_unjoined']} 条"
                    f"(同样不进本集;这一档是**政策表缺口**的信号,"
                    f"清单见 error_reclass_report)")

    # ① 反例召回 —— **必须按「结论是谁给的」拆开,不是按类别字符串拆**
    # ⚠ 2026-09-03 首测暴露的读数陷阱:反例取自沃尔玛拒过的品,而**我们拒过的
    #   品多半当场就进了 catalog 黑名单三表**(拒了就拉黑是既有流程)。于是
    #   L0 一眼认出 ASIN 就硬拒,判据一步都没走 —— 总召回因此天然虚高,
    #   而"政策判得对不对"这个问题一个字都没回答。
    #   首测实测:总召回 78/114=68.4% 看着不错,拆开看**判据召回 0/36**。
    # ⚠ 2026-09-03 第二次修正:原来用 `got_category == '内部黑名单'` 分道,
    #   **两个方向都漏**(见 `evidence_kind` 头注)—— 上游硬拒自报的是真政策名,
    #   全都混进了判据的分子分母。现在按 `evidence_kind`(层 + 置信 + 品牌覆写
    #   句式)分道,三桶都摊开报,不藏任何一桶。
    rec = [r for r in neg if r["got_verdict"] == "reject"]
    kinds = {k: [r for r in neg if evidence_kind(r) == k]
             for k in ("rules", "brand", "policy")}
    judged_neg = kinds["policy"]
    judged_rec = [r for r in judged_neg if r["got_verdict"] == "reject"]
    body += ["", f"▍反例召回(沃尔玛拒了,我们也拒):{_pct(len(rec), len(neg))}",
             f"    其中 pass {sum(1 for r in neg if r['got_verdict'] == 'pass')}"
             f" / pending {sum(1 for r in neg if r['got_verdict'] == 'pending')}"
             f" / 判定失败 {sum(1 for r in neg if r['error'])}"]
    if kinds["rules"] or kinds["brand"]:
        body.append("  ⚠ **拆开看结论是谁给的**(按停在哪一层分,不按类别名 ——"
                    "上游硬拒自报的也是真政策名):")
        for k in ("rules", "brand", "policy"):
            grp = kinds[k]
            if not grp:
                continue
            body.append(f"    {_KIND_CN[k]}:{len(grp)} 条,判拒 "
                        f"{sum(1 for r in grp if r['got_verdict'] == 'reject')}")
        body.append("    (前两桶**没有一条读过那 44 篇原文**:查表 / 正则 /"
                    "确定性后处理,算不进「判据」)")
        # 全都是判据判的时候不出这一行:那时它恒等于上面的总召回,重复 = 噪声
        body.append(f"  **判据召回**(只算 L3 政策判据的 {len(judged_neg)} 条反例):"
                    f"{_pct(len(judged_rec), len(judged_neg))}"
                    f" ← **要看判据行不行,只能看这个数**")

    # ② 类别准确率 + 混淆表
    # ⚠ 两个分母都要给:只报端到端(分母 = 全部带类别反例)会把"没判拒"的
    # 失败算进类别账上,改提示词的人分不清是召回问题还是类别问题;只报
    # "判拒的那些里"又会把召回的窟窿藏起来。混淆表只画**判拒的**那些 ——
    # 没判拒的行画进去全是「→ (无类别)」,那说的是召回不是类别。
    rej_lab = [r for r in labelled if r["got_verdict"] == "reject"]
    hit = [r for r in rej_lab
           if category_ok(r["expected_category"], r["got_category"])]
    body += ["", f"▍类别准确率(枚举精确等值,内容族两名互认):"
                 f"判拒的带类别反例里 {_pct(len(hit), len(rej_lab))};"
                 f"端到端(判拒且类别对 ÷ 全部带类别反例 {len(labelled)} 条)"
                 f"{_pct(len(hit), len(labelled))}"]
    # ⚠ 同上:记忆与规则给的类别不是"判据判出来的类别",混在分母里会让这个数
    #   恒等于 0 而看不出原因(首测 67 条判拒里 64 条是黑名单命中)。
    #   同样按 `evidence_kind` 分道,**不按类别字符串**。
    rej_judged = [r for r in rej_lab if evidence_kind(r) == "policy"]
    if len(rej_judged) != len(rej_lab):
        hit_j = [r for r in rej_judged
                 if category_ok(r["expected_category"], r["got_category"])]
        body += [f"  ⚠ 其中 {len(rej_lab) - len(rej_judged)} 条的类别**不是判据给的**"
                 f"(上游硬拒自报 / L3 品牌翻拒 —— 都没读过那 44 篇原文);"
                 f"**真正由判据给出类别的** {len(rej_judged)} 条里 "
                 f"{_pct(len(hit_j), len(rej_judged))}"]
    if rej_lab:
        body += _confusion(rej_lab, limit)

    # ③ 正例误伤:**共同子集**上新旧并排(所有者的底线)
    # ⚠ 两个分母不能拿来比大小(首版就是那么写的):新链的分母是全部正例、
    # 旧链的分母是"其中有旧链结论的那些",两批产品根本不一样,比出来的
    # "新链更好"可能纯粹是因为没有旧结论的那批本来就更干净。底线只判在
    # **同一批产品**上;全部正例上的新链误伤率另行单列(那是绝对水位,
    # 不是对照)。
    new_fp_all = [r for r in pos if r["got_verdict"] == "reject"]
    shared = [r for r in pos if r["old_verdict"]]
    new_fp = [r for r in shared if r["got_verdict"] == "reject"]
    old_fp = [r for r in shared if r["old_verdict"] == "reject"]
    if not shared:
        verdict_line = ("⚠ 本批正例**一条旧链结论都没有** —— 底线无从比起"
                        "(先确认 audit_runs 里有历史行,或换一批正例)")
    elif len(new_fp) > len(old_fp):
        verdict_line = ("⚠ **新链误伤高于旧链** —— 所有者定稿 §六.5 的底线是"
                        "「正例误伤率不高于旧链」,不达标就先修判据再回放,"
                        "别开 mode=stale")
    else:
        verdict_line = "新链 ≤ 旧链 ✓(所有者定稿的底线达标)"
    body += ["", "▍正例误伤(在架在售却被判拒)",
             f"  **共同子集**(正例里有旧链结论的 {len(shared)} 条,底线判这里):"
             f"新链 {_pct(len(new_fp), len(shared))};"
             f"旧链 {_pct(len(old_fp), len(shared))}",
             f"    {verdict_line}",
             f"  全部正例上的新链误伤(绝对水位;旧链在另外 "
             f"{len(pos) - len(shared)} 条上没有可比结论):"
             f"{_pct(len(new_fp_all), len(pos))}"]

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

        # **同 tag 有行 = 重放那一批**(不重新抽样,见 `_stored_samples` 头注):
        # seed 只保证"同一份候选面上抽同一批",而候选面 `catalog.walmart_items`
        # 每天被 catalog_sync 重写 —— 隔天同 seed 就不是同一批了,而两份报告
        # 长得一模一样。想换样本就换 tag。
        stored = _stored_samples(conn, opts.tag)
        neg_stats: dict = {}
        pos_stats: dict = {}
        cap = opts.cap or 0
        if stored:
            samples = stored
            picked = [s for s in stored if s.source == "neg"]
            pos_asins = [s.asin for s in stored if s.source == "pos"]
            neg_pool, pos_pool = picked, pos_asins
        else:
            neg_pool, neg_stats = _negatives(conn, opts, policy_names)
            cap = opts.cap or default_cap(
                opts.neg, len({s.stratum for s in neg_pool}))
            picked = stratify(neg_pool, opts.neg, cap, opts.seed)
            # 正例的两道排除(2026-09-02 B2 复核修订):
            #  ① **整个反例池**的 asin(不只是抽中的那批)—— 同一个 asin 两边
            #     都收就是自相矛盾的样本;
            #  ② 任何一家店给过下架原因的 asin —— `_POS_SQL` 的 NOT EXISTS 是
            #     **sku 级**的,而身份是 asin 级:同一产品 A 店订货号在架、
            #     B 店订货号被拒,SQL 自己比不出来(见 `rejected_asins`)
            pos_pool, pos_stats = _positives(
                conn, opts, {s.asin for s in neg_pool}, rejected_asins(conn))
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
            if stored:
                return (f"audit_replay:run_tag={opts.tag} 的 {len(stored)} 条"
                        f"既有样本**在 catalog.products 里一条都找不到**"
                        f"(产品行被清过?)—— 本轮什么都没做")
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
            how = (f"样本:反例 {len(picked)} / 正例 {len(pos_asins)}"
                   f"(**取自 run_tag 既有行,不重新抽样**)" if stored else
                   f"样本:反例 {len(picked)} / 正例 {len(pos_asins)}"
                   f"(每类封顶 {cap};反例池 {neg_stats['scanned']} 行 → 合格 "
                   f"{len(neg_pool)},正例池 {pos_stats['scanned']} 行 → 合格 "
                   f"{len(pos_pool)})")
            lines = [
                "🧪 audit_replay --dry-run(零 LLM、零落库、不写报告文件)",
                f"run_tag={opts.tag};seed={opts.seed};并发 {opts.workers}",
                how,
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

        # ⚠ **判定之前先提交**:抽样与打标那几条查询开的事务,如果一直挂到
        # 判定跑完(几百条 × 几秒 LLM = 几十分钟),这条连接就是几十分钟的
        # idle in transaction —— 它按住一个老快照,vacuum 清不掉那段时间里
        # 产生的死行,而回放期间生产链正在往同几张表写。读完就撒手。
        conn.commit()

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
            "cost_lines": cost_lines, "reused": bool(stored),
            "pos_wanted": opts.pos}          # 要了却没抽到 → 首行点名
    summary, full = report(rows, meta)
    paths.reports_dir().mkdir(parents=True, exist_ok=True)
    path = paths.audit_replay_report()
    path.write_text("\n".join(full) + "\n", encoding="utf-8")
    tail = [f"落库 audit.replay_results {written} 行(run_tag={opts.tag};"
            + ("**重放既有样本**,原地覆盖" if stored else
               "首次使用该 tag —— 之后同 tag 再跑会**重放这一批**,不重新抽样")
            + ");**结论表一个字都没碰**",
            f"▍全文报告(含逐条错判清单)→ {path}"]
    if opts.conn_note:
        tail.insert(0, opts.conn_note)
    return "\n".join(summary + [""] + tail)
