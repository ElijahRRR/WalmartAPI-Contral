"""product_audit — 产品审核主流程(批次 C:全链含 LLM 层;危险,缺省即真跑)。

用法(2026-08-16 起**缺省即真跑**,空跑加 `--dry-run`;`--execute` 是兼容别名):
  python cli.py product_audit                       # 真跑:判定 + 落 runs/hits + 写结论
  python cli.py product_audit --dry-run             # 空跑:判定照跑,不写 products 五列
  python cli.py product_audit -p limit=2000
  python cli.py product_audit -p asins=B0A,B0B             # 指定 ASIN(无视现有结论强审)
  python cli.py product_audit -p mode=backfill             # 补刷:只审无结论,历史结论直接采用
  python cli.py product_audit -p mode=pending              # 待定专刷:只重判 pending,无退避
  python cli.py product_audit -p rerule=phase0_forbidden_category
                                                    # 改了某条规则后**定点**重审被它拒过的
  python cli.py product_audit -p stages=L0                 # 只跑 Phase0:纯查库零 LLM;
                                                    # 未命中的不落结论不盖版本(不"复活")
  python cli.py product_audit -p rerule=phase0_lark_blacklist_seller -p stages=L0
                                                    # 零 LLM 翻新黑名单历史行的标准姿势
  python cli.py product_audit -p mode=pass -p stages=L0 -p limit=1000000
                                                    # 现役 pass 全量重过 L0(黑名单翻案);
                                                    # 未命中不退出候选,**一次大 limit 扫完**
  python cli.py product_audit -p r5=on                     # 开 USPTO 商标反查(默认关)
  python cli.py product_audit -p l3=off                    # 关 L3 语义层(省 LLM 配额)
  python cli.py product_audit -p l4=on                     # 开 L4 视觉(默认关,批复 #2)
  python cli.py product_audit -p from_sheet=1              # 上架表驱动:审真待审的 + 回填 C~G
  python cli.py product_audit -p from_sheet=1 -p limit=3000   # 存量大时加大一轮的量
  python cli.py product_audit -p from_sheet=1 -p gap_wait=45   # 缺数据等采集最多 45 分钟
  python cli.py product_audit -p from_sheet=1 -p gap_wait=0    # 缺数据只推采集不等(采集侧病了时)

链路(批次 C 全链):领 catalog.products 待审行 → Phase0 四件套 →
L1(实证→报错实证→哨兵→映射表→候选+rerank)→ L2 硬规则 → [L3 语义 →
L4 视觉] → 37 政策理由映射 → 落 audit.audit_runs/audit_hits;真跑才写
products.audit_* 五列与审核事件(空跑用 --dry-run)。**`-p from_sheet=1` 时另把结论投影回上架表
C~G 五列**(2026-08-16 开闸,并跑期"只落库不投影"的纪律到此结束)。

⚠ `from_sheet` **不是强审**:表 E 列为空只说明表里没有结论,库里可能早就有。
已有结论的直接投影回表(零 LLM),只有 `_DEFAULT_CANDIDATE` 认定的真待审
(未审 / pending 过退避)才进判定引擎 —— 什么时候才重审见那条常量的注释。
领取口径含 **E=pending**(2026-08-17):pending 是中间态不是结论,写进 E 之后
若不再领回来,那批就永久停在表上的 `pending`(见 `listing_sheet.audit_targets`)。

**缺数据同轮补采闭环**(所有者定稿 2026-08-17:「产品审核不能等下一次,要轮询
等采完拿数据审核,下一次运行是第二天,时间很长,并且不审核,后面的上架也做不了」):
表里轮到审、但库里压根没有(或有行无标题=采集降级)的 ASIN → 推采集批次
`audit_gap_<日界>` → **轮询等它采完**(缺省 20 分钟,`-p gap_wait=N` 调,0=只推
不等)→ **就地按批摄取**(批次端点,无锁)→ 采回来的**这一轮就判掉**。
仍缺的把采集侧真实 `error_type` 写进表格 **F 列、E 列留空**(留空才会被下轮重领)。
整段跑在候选查询**之前**,所以不需要第二遍判定循环。见 `_close_gap`。
⚠ **在库的待审行不做"先刷新再审"**(所有者复议定稿 2026-08-19):审核判的是
"这个产品卖的是什么",第一次就定性了,改标题/描述不改变它是什么——
强刷带来的翻案更大可能是 LLM 随机性(上架链相反,**必须**先刷新:价格库存
是要写到沃尔玛的真金白银)。

dry-run 语义(计划 B4 定稿):判定照跑、runs/hits 照落,但不碰 products
五列、不发事件、不投影。⚠ 批次 C 起 dry-run **同样产生真实 LLM 调用与费用**
(L1 rerank / L3;L4 需显式 l4=on)——验收抽样时用 limit 控制成本。

补刷(mode=backfill,批复 #5"只补刷"):候选限 audit_status IS NULL;先查
audit.audit_runs 历史结论(谓词必须 stage_stopped_at IS DISTINCT FROM
'SHORTCUT'——204 万存量里有短路影子行;排序键 (verdict='reject') DESC
实现旧 reject 粘性),有历史者**直接采用**写五列+事件(detail 带
referenced_run_id,不写新 run——方案 A,不制造影子行),无历史者进正常判定。

pending 两来源(reason 区分):L1=类目解不出(候选/rerank 均无解);
L3=LLM 故障(10.2 单链:重试尽→pending 绝不默认放行)。均按每日退避重试。
`mode=pending` 是这条退避的人工旁路:判定逻辑刚改过时要立刻拿存量 pending
验证效果,等一天等的是自己。**不采用历史结论**(backfill=False)——pending
行要的是重判,拿旧 run 顶上等于把这次改动的效果盖掉。只手动跑,不进调度。
无标题产品跳过不审(采集降级,不够格判定;amz_source:103 先例)并计数。
seller 闸依赖 snapshots.buybox->>'buybox_seller_id'(契约外字段,可能恒缺)
——缺失计数在摘要亮出,恒缺说明卖家闸未生效,需向采集侧提契约扩展。

R5(USPTO)默认关:spec_l2 §5.6f——brand_nice_class 覆盖率仅 ~2.6 万/1400 万,
先离线抽样出数据再决定常开;开时全程复用一个只读连接。
"""

import logging
import re
import time
from datetime import datetime

from api import scraper
from registry import db, resources
from services import audit_reason, audit_rules, audit_store, db_guard, \
    kpi, \
    listing_sheet, product_events, product_ingest, scrape_batches

DANGEROUS = True

logger = logging.getLogger("workflows.product_audit")

# 上架表取回来的 ASIN 长这样才算 ASIN(与 scrape_missing/product_refresh 同款)。
# 用途只有一个:列错位守卫 —— 见 run() 里 from_sheet 那段
_ASIN_RE = re.compile(r"^B[0-9A-Z]{9}$")

# 分段提交与进度播报的粒度(生产事故 2026-08-14:34 万行跑在同一个未提交
# 事务里 —— 外部查不到任何进度、Ctrl-C 全部回滚、长事务还挡住 vacuum)。
# 每 N 行提交一次 + 打一行进度,判定结果就变成"跑到哪算到哪"。
_COMMIT_EVERY = 500

# 判定并发:线程等的是 HTTP 不是 CPU,所以远超核数是正常的;真正的天花板在
# LLM 侧(撞限流只会静默退避变慢,看 RETRY_STATS 判断)。
# 默认 128(所有者定稿 2026-08-17:「审核默认设置为 128,之前已经实测调大
# 并发是有效果的」)。此前默认 4 而上限 64 —— 不显式传 -p workers= 就只跑 4,
# "上限 64"看着高其实从没生效过。
_DEFAULT_WORKERS = 128
_MAX_WORKERS = 256

# 落库批大小:并发调到 128 之后,主线程"逐行 savepoint + 逐行 INSERT"成了
# 新瓶颈,改成攒一批 executemany(见 audit_store.persist_runs)。
# 批太大则一次失败要退回逐行的代价也大,200 是速度与隔离代价的折中。
_PERSIST_BATCH = 200

_CANDIDATE_SQL = """
SELECT p.asin,
       p.title,
       p.brand,
       p.walmart_pt,
       p.pt_source,
       p.browse_node_id,
       p.amazon_category AS amazon_category_path,
       p.slow -> 'bullet_points' AS bullet_points,
       coalesce(p.slow ->> 'description',
                p.slow ->> 'long_description',
                p.slow ->> 'product_description') AS long_description,
       sn.buybox ->> 'buybox_seller_id' AS seller_id,
       sn.buybox ->> 'buybox_seller'    AS seller_name
FROM catalog.products p
LEFT JOIN LATERAL (
    SELECT s.buybox FROM catalog.snapshots s
    WHERE s.marketplace = p.marketplace AND s.asin = p.asin
      AND s.outcome = 'ok'
    ORDER BY s.scraped_at DESC LIMIT 1
) sn ON true
WHERE p.marketplace = %(marketplace)s AND ({where}){recent_guard}
  AND p.title IS NOT NULL AND p.title <> ''
ORDER BY p.audited_at NULLS FIRST, p.updated_at
LIMIT %(limit)s
"""
# ↑ title 过滤挡两类:采集降级空标题行,以及 pt_backfill 的占位行(只有
#   asin+walmart_pt)。占位行若进候选,循环级跳过会让同一批空壳行每轮
#   霸占 LIMIT 名额 → 真候选饿死。注:asins= 点名的空壳行也被过滤,
#   会体现在"库中命中 N"的缺口提示里(空壳行没有可审内容,过滤是对的)

# dry-run 复烧护栏(评审 P1-1):dry-run 不动 audited_at,同一批候选会被
# 连续 dry-run 反复领走——L1 rerank/L4 不缓存,每轮全额重付 LLM 费用。
# runs 是 dry-run 也落的,拿它做 24h 排除;asins= 强审除外(点名就要审)
_RECENT_RUN_GUARD = """
 AND NOT EXISTS (
    SELECT 1 FROM audit.audit_runs r
    WHERE r.asin = p.asin AND r.created_at > now() - interval '24 hours')"""
# 排序契约:从未审过的(audited_at NULL)永远先于重试的 pending——
# 否则 pending 存量 ≥ limit 时新入库产品会被饿死(评审 P1-3)

# 历史结论(补刷用):SHORTCUT 排除 + reject 粘性排序键——
# (verdict='reject') DESC 把旧 history_shortcut 的"reject 查询先跑"压成一个
# 排序键,语义等价(spec_shortcut §3.4C),别当成可随手删的排序
_HISTORY_SQL = """
SELECT DISTINCT ON (asin) asin, run_id, verdict, score_final,
       walmart_product_type, l3_reason_category, stage_stopped_at, created_at,
       pt_source
FROM audit.audit_runs
WHERE asin = ANY(%s)
  AND verdict IN ('reject', 'pass')
  AND stage_stopped_at IS DISTINCT FROM 'SHORTCUT'
ORDER BY asin, (verdict = 'reject') DESC, created_at DESC
"""

# 有历史结论可采用(与 _HISTORY_SQL 同谓词:排除 SHORTCUT 影子行)
_HAS_HISTORY_SQL = """EXISTS (
    SELECT 1 FROM audit.audit_runs r
    WHERE r.asin = p.asin AND r.verdict IN ('reject', 'pass')
      AND r.stage_stopped_at IS DISTINCT FROM 'SHORTCUT')"""

# ⚠ **实证行的 PT 一个字都不许动**(生产事故 2026-08-14:首版用
# COALESCE(新PT, 旧值) 覆盖,把 pt_backfill 回填的 9 万条沃尔玛回执实证
# 换成了旧系统的判定结论,来源一并降级成 audit_llm,挖掘燃料从 16.8 万
# 腰斩到 7.7 万)。采用的是**我们自己旧系统的推断**,它压不过沃尔玛回执。
# 审核结论(status/reason)照写——那与 PT 来源无关。
_ADOPT_SQL = """
UPDATE catalog.products
SET audit_status = %(status)s, audit_reason = %(reason)s,
    walmart_pt = CASE WHEN pt_source = 'walmart_confirmed' THEN walmart_pt
                      ELSE COALESCE(%(pt)s, walmart_pt) END,
    pt_source = CASE WHEN pt_source = 'walmart_confirmed' THEN pt_source
                     WHEN %(pt)s IS NULL THEN pt_source
                     ELSE %(pt_source)s END,
    audited_at = now(), audit_version = %(version)s
WHERE marketplace = %(marketplace)s AND asin = %(asin)s
"""


_KNOWN_PARAMS = {"asins", "limit", "mode", "r5", "force_rerun", "rerule",
                 "l3", "l4", "stages", "workers", "adopt_only", "from_sheet",
                 "gap_wait"}
# cli 自己塞进 params 的键,不是人敲的 —— 白名单必须放行,否则每加一个
# cli 级开关就会把所有"宁炸不吞"的工作流一起炸掉(2026-08-16 `dry_run`
# 上线当天就是这么炸的:`--dry-run` 直接让 product_audit 起不来)
_CLI_INJECTED = {"execute", "dry_run"}
# mode 取值白名单:backfill=只补没审过的;pending=只重刷待定(无退避)
_MODES = {"backfill", "pending", "pass"}

# **什么才算"待审"**(所有者定稿的重审政策,唯一出处):
#   · 没结论(新品 / 从没审过)             → 审
#   · pending(L1 解不出类目 / L3 LLM 故障)→ 隔天重试一次
#   · approved / rejected                  → **不重审**。approved 只有在
#     `slow_hash` 变了(产品本身改了)时由 product_ingest 翻回 pending;
#     rejected 永不自动重审(45 天 TTL 那套已废除)
#   · 要整批重审只有一条路:`-p force_rerun=<规则版本>`(人工显式)
_DEFAULT_CANDIDATE = (
    "(p.audit_status IS NULL OR (p.audit_status = 'pending' "
    "AND (p.audited_at IS NULL OR p.audited_at < now() - interval '1 day')))")


def _pick_where(params: dict) -> tuple[str, dict]:
    unknown = set(params) - _KNOWN_PARAMS - _CLI_INJECTED
    if unknown:
        # 静默吞参数 = "全量重审跑完了"的假象(评审 P1-4),宁炸不吞
        raise ValueError(f"未识别参数 {sorted(unknown)}(可用:{sorted(_KNOWN_PARAMS)})")
    asins = [a.strip() for a in str(params.get("asins", "")).split(",")
             if a.strip()]
    if asins:
        if params.get("from_sheet"):
            # ⚠ 上架表驱动**不是强审**(所有者纠正 2026-08-16:「按我们的运行
            # 逻辑,不是应该直接从库里读取结果吗」)。E 列为空只说明**表里**
            # 没有结论,不说明库里没有 —— 库里已有结论的直接投影回表(零 LLM),
            # 只有真待审的才进判定引擎。当成强审的后果是每轮把已审过的几万个
            # ASIN 重判一遍:钱白花、慢得离谱,而且看不出哪里不对。
            return f"p.asin = ANY(%(asins)s) AND {_DEFAULT_CANDIDATE}", \
                {"asins": asins}
        # 指定 ASIN = 无视现有结论强审(与旧仓 force_rerun 不同:这里没有
        # 运行时短路可绕,绕的是 audit_status 候选谓词)
        return "p.asin = ANY(%(asins)s)", {"asins": asins}
    fr = str(params.get("force_rerun", "")).strip()
    if fr:
        # 按版本批量重审(B7):audit_version 不等于目标版本的全部重审,
        # 含已 approved/rejected 的存量
        return "p.audit_version IS DISTINCT FROM %(force_rerun)s", \
            {"force_rerun": fr}
    rule = str(params.get("rerule", "")).strip()
    if rule:
        # 按**规则码**定点重审(2026-08-17 加):改了一条规则之后,要动的只有
        # "被这条规则拒过"的那批。此前唯一的批量通道是 force_rerun=<版本>,
        # 而版本一递增库里没有一条是新版本 ⇒ **全量**十几万条重审,为了几千条
        # 误杀烧掉全库的 LLM 钱。触发场景:裁决 A 摘掉 Phase0 四个大类
        # (礼品袋被判药品),要翻的就是 phase0_forbidden_category 拒过的那批。
        #
        # ⚠⚠ **锚在 catalog.products,不能锚在"最近一轮 audit_runs"**。
        # 首版就是锚最近一轮(verdict='reject' 且该轮有这条 hit),所有者第一次
        # dry-run 当场炸出问题:**dry-run 也落 runs/hits**,于是那 500 条的
        # "最近一轮"变成了本次 dry-run 的结果 —— 被救回来的 45 条新一轮判 pass、
        # 也不再带这条 hit,直接**掉出候选集**;而 dry-run 不写 products 五列,
        # 它们的 audit_status 还是 rejected。净效果:45 条产品被"验证"了一次就
        # 永久搁浅,任何通道都不会再捞它们,而且全程不报错。
        #
        # 现在的口径(每条都是为了让 dry-run 与真跑说同一件事):
        #  · `audit_status = 'rejected'` —— 要翻的是**现行结论**,而 dry-run 碰
        #    不到这一列,所以反复 dry-run 候选集恒定,可重复验证;
        #  · `audit_version IS DISTINCT FROM <当前版本>` —— 真跑判过的会盖上新
        #    版本号,自动退出候选集。这同时是**天然分页**:limit 撞满就再跑一轮,
        #    接着判剩下的,不会每轮都重判同一批(首版没有这条,真跑会原地打转);
        #  · `EXISTS` 任意一轮命中过该规则 —— 不是"最近一轮"。理由同上:最近
        #    一轮会被 dry-run 覆盖掉。代价是"早年被它拒、后来改判别的原因仍是
        #    rejected"的行也会进来,那批重判一次结论不变,只多花一轮 LLM。
        return ("p.audit_status = 'rejected'"
                " AND p.audit_version IS DISTINCT FROM %(rerule_ver)s"
                " AND EXISTS (SELECT 1 FROM audit.audit_runs r"
                "             JOIN audit.audit_hits h ON h.run_id = r.run_id"
                "             WHERE r.asin = p.asin"
                "               AND h.rule_code = %(rerule)s)",
                {"rerule": rule,
                 "rerule_ver": resources.AUDIT_RULES_VERSION})
    mode = str(params.get("mode", "")).strip()
    if mode and mode not in _MODES:
        # 与未识别参数同理:静默落回默认 = "补刷跑完了"的假象,宁炸不吞
        raise ValueError(f"未识别 mode={mode!r}(可用:{sorted(_MODES)})")
    if mode == "backfill":
        return "p.audit_status IS NULL", {}
    if mode == "pending":
        # 待定专刷:**无 1 天退避**——判定逻辑刚改过时要立刻拿存量 pending
        # 验证效果,等一天等的是自己。人工显式动作,不进任何定时调度
        return "p.audit_status = 'pending'", {}
    if mode == "pass":
        # 现役 pass 全量重过 L0(所有者 2026-08-19:「对仓库里所有 pass 的
        # 产品重跑L0」)——黑名单是活的,拉黑常发生在放行**之后**,放行过的
        # 行不重扫就等于黑名单只管新品。只与 stages=L0 连用(run() 钉死):
        # 全链重审全部 pass = 重烧全库 LLM,要那么干请 force_rerun=<版本>。
        # ⚠ 本模式**没有天然分页**:命中翻案(status 变 rejected)会退出
        # 候选,但未命中不落结论不盖版本(截断链没资格,#49 语义)、
        # **不退出候选** —— 小批多轮每轮都从头扫同一批,必须一次大 limit
        # 扫完(L0 纯查库,几十万行也就是多花几分钟)。
        return "p.audit_status = 'approved'", {}
    # 默认:新品 + pending 重试(退避 1 天:批次 B 的 pending 多为 PT 解不出,
    # 每小时重判只会无界追加 audit_runs,评审 P1-3)
    return _DEFAULT_CANDIDATE, {}


# 定点重审的"还剩多少"计数(与 _CANDIDATE_SQL 同一 where,去掉 JOIN 与 LIMIT)
_RERULE_COUNT_SQL = """
SELECT count(*) FROM catalog.products p
WHERE p.marketplace = %(marketplace)s AND ({where})
  AND p.title IS NOT NULL AND p.title <> ''
"""


def _rerule_head(conn, rule: str, where: str, extra: dict,
                 limit: int) -> list[str]:
    """输入:连接 + 规则码 + 候选谓词 → 输出:摘要前言(总量/本轮/还剩)。

    没有这一行的话,摘要只会说"候选 500",而 500 正是 limit ——**看不出是刚好
    500 个还是撞了上限**(所有者 2026-08-17 首轮 dry-run 实遇)。误杀规模是他
    决定要不要真跑的唯一依据,必须报出来。与 `_claim_from_sheet` 同款纪律。
    """
    with conn.cursor() as cur:
        cur.execute(_RERULE_COUNT_SQL.format(where=where),
                    {"marketplace": "US", **extra})
        total = int(cur.fetchone()[0])
    head = [f"定点重审 rerule={rule}:命中过该规则且**现结论仍是 rejected**、"
            f"且未按当前规则版本判过的,共 {total} 个"]
    if total > limit:
        head.append(f"  ⚠ 本轮 limit={limit},**只判 {limit} 个,还剩 "
                    f"{total - limit} 个** —— 真跑一轮会给判过的盖上 "
                    f"{resources.AUDIT_RULES_VERSION},它们自动退出候选集,"
                    f"再跑一次接着判(或 -p limit=N 加大)")
    if not total:
        head.append("  (一个都没有:规则码拼错?或这批已经全部按当前版本判过了)")
    return head


def _is_forced(params: dict, extra: dict) -> bool:
    """输入:params + _pick_where 产出的 extra → 输出:本轮算不算**强审**。

    只用来决定要不要挂 `_RECENT_RUN_GUARD`(dry-run 复烧护栏)。强审 = 人点名
    要审的,点了就得审,哪怕 24 小时内刚审过。两种:

    · `asins=` 点名 —— 但 **`from_sheet` 不算**:它也往 extra 塞 asins,走的却是
      默认候选谓词(E 列为空 ≠ 库里没结论),该吃护栏。
    · `rerule=` 定点重审 —— 它翻的正是**刚刚被拒**的那批(改规则当天就要验),
      全在 24 小时内。吃了护栏的话 dry-run 稳定报"0 候选",紧跟着真跑翻出几千
      条,又是一次"dry-run 说没事、真跑吓一跳"(所有者 2026-08-16 被 from_sheet
      坑过一次,那次差异在回填行数,这次会差在候选数)。

    代价说清:强审下重复 dry-run 会重复烧 LLM —— 这是点名的固有代价,不是 bug。
    """
    if str(params.get("rerule", "")).strip():
        return True
    return bool(extra.get("asins")) and not params.get("from_sheet")


def _adopt_history(conn, asins: list[str], execute: bool) -> tuple[int, set]:
    """输入:候选 ASIN 列表 → 输出:(采用数, 已采用 ASIN 集)。

    方案 A(spec_shortcut §1.6,待所有者追认):历史结论直接写五列+事件,
    不写新 run。读库失败让异常冒泡整轮停——静默按"无历史"重审会把
    rejected 产品翻出来(spec_shortcut §6.1)。
    """
    if not asins:
        return 0, set()
    with conn.cursor() as cur:
        cur.execute(_HISTORY_SQL, (asins,))
        rows = cur.fetchall()
    adopted = set()
    events = []
    adopt_rows = []
    for (asin, run_id, verdict, _score, pt, reason_cat, stage, created,
         src) in rows:
        adopted.add(asin)
        if not execute:
            continue
        status = "approved" if verdict == "pass" else "rejected"
        if verdict == "reject":
            # 存量大头是 L0/L2 拒,l3_reason_category 本就 NULL——不留空
            # (rejected 说不出理由 = 排查断线),也不迁旧'history_shortcut'字面量
            reason = reason_cat or f"历史结论(阶段 {stage or '未知'},理由未留存)"
        else:
            reason = None
        adopt_rows.append({
            "status": status,
            "reason": reason,
            "pt": (pt if pt and not pt.startswith("(") else None),
            # 旧结论的 PT 来源照搬 runs 记录;非实证一律记 audit_llm
            # (来历不明的 PT 不当实证——它会被 catmap_mine 投票放大)
            "pt_source": ("walmart_confirmed"
                          if src in ("walmart_confirmed",
                                     "historical_confirmed")
                          else "audit_llm"),
            "version": resources.AUDIT_RULES_VERSION,
            "marketplace": "US", "asin": asin,
        })
        code = (product_events.AUDIT_PASSED if verdict == "pass"
                else product_events.AUDIT_REJECTED)
        events.append({"sku": asin, "event": code, "source": "audit_backfill",
                       "detail": {"referenced_run_id": run_id,
                                  "original_created_at":
                                      created.isoformat() if created else "",
                                  "audit_version":
                                      resources.AUDIT_RULES_VERSION}})
    if execute and adopt_rows:
        # 批量:86 万条采用逐行往返要几十分钟,executemany 一次搞定
        with conn.cursor() as cur:
            cur.executemany(_ADOPT_SQL, adopt_rows)
    if execute and events:
        product_events.record_many(conn, events)
    return len(adopted), adopted


_SQL_VERDICT = """
SELECT asin, title, walmart_pt, audit_status, audit_reason, audited_at
FROM catalog.products
WHERE marketplace = 'US' AND asin = ANY(%s)
"""


_SQL_SHEET_STATE = """
SELECT coalesce(audit_status, '未审') AS st, count(*)
FROM catalog.products
WHERE marketplace = 'US' AND asin = ANY(%s)
GROUP BY 1
"""


def _claim_from_sheet(limit: int) -> tuple[list[dict], list[str], list[str]]:
    """输入:本轮 limit → 输出:(上架表待审行, 交给候选谓词的 ASIN, 摘要前言)。

    所有者定稿 2026-08-16:「审核直接读取上架表的 ASIN 与审核结果两列
    (结果为空就审核)」。实现上**走 asins= 那条既有路径** —— 审核引擎只有
    一条实现,这里只是换了个领任务的地方(与 problem_scan/maintenance_scan
    那种"决策与执行分家"同理)。

    ⚠ **ASIN 列表不在这里截断**(所有者纠正 2026-08-16:「不是应该直接从库里
    读取结果吗」)。E 列为空 ≠ 库里没结论:整批交给 `_DEFAULT_CANDIDATE` 谓词,
    已有结论的**根本不进候选**(零 LLM,靠 `_project_to_sheet` 把库里的结论
    投影回表),`LIMIT` 只限制**真要判的**那部分。
    先截断的话已审过的会占满名额,每轮都在重判老货,新品永远排不上。

    一行都没有时 ASIN 列表为空,前言里是那句说明(调用方直接返回它)。
    """
    rows = listing_sheet.audit_targets()
    want = sorted({r["asin"] for r in rows})
    if not want:
        return [], [], [
            "上架表:没有待审行(ASIN 有值且审核结果为空的一行都没有)。"
            "⚠ **清空 E 列不是重审入口**:清空只是让这行重新被领,而结论以库为准,"
            "库里已有结论的会被原样投影回来(见下方 from_sheet 非强审那条注释)。"
            "真要重审走 `-p asins=<逗号分隔>`(点名强审)或 "
            "`-p rerule=<规则码>`(改了某条规则后定点翻案)"]
    bad = [a for a in want if not _ASIN_RE.match(a)][:5]
    if bad:
        # ⚠ 列错位的唯一征兆。表头再被调一次而 registry 的列元组没跟着改,
        # 这里拿到的就是店铺名/标题,而审核会照样跑完、照样回填,只是全都
        # 判"库里没有" —— 看起来像"这批产品还没采集",查半天查不到根因。
        # 宁炸不吞:在动库之前就停下,并说清怀疑的是列序
        raise ValueError(
            f"上架表取到的 ASIN 不像 ASIN(样例 {bad});"
            f"多半是表头列序变了而 registry.resources.LISTING_SHEET.columns "
            f"没跟着改(现登记:{resources.LISTING_SHEET.columns[:3]}…)")
    head = [f"上架表 E 列为空 {len(want)} 个 ASIN"
            f"({len(rows)} 行,同 ASIN 多店铺算多行)"]
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_SHEET_STATE, (want,))
        st = {k: int(n) for k, n in cur.fetchall()}
    absent = len(want) - sum(st.values())        # 库里压根没有这个 ASIN
    done = st.get("approved", 0) + st.get("rejected", 0)
    todo = st.get("未审", 0) + st.get("pending", 0)
    # 这三个数是"为什么这轮还在审"的全部答案,必须在摘要里(只写日志的话
    # 飞书通知看着像"审完了")
    head.append(
        f"  库里已有结论 {done}(过 {st.get('approved', 0)}/拒 "
        f"{st.get('rejected', 0)})→ **直接回填,不重审**"
        f";待审 {todo}(未审 {st.get('未审', 0)}/待定 {st.get('pending', 0)})"
        # ⚠ 这个数是**补采之前**的快照(补采跑在它后面)。别写成"本轮审不了"
        # —— 同轮闭环正是为了把它们救回来,救回来多少看下面那段
        + (f";⚠ 不在库 {absent}(补采前口径,见下方补采段:"
           f"推采集 → 等采完 → 摄取,救回来的本轮就审)" if absent else ""))
    if todo > limit:
        logger.warning("上架表待审 %d 个 ASIN,本轮 limit=%d", todo, limit)
        head.append(f"  ⚠ 本轮 limit={limit},**只判 {limit} 个,还剩 "
                    f"{todo - limit} 个**待审;再跑一次接着判"
                    f"(或 -p limit=N 加大)")
    return rows, want, head


def _project_to_sheet(sheet_rows: list[dict], execute: bool) -> str:
    """输入:本轮领的上架表行 → 输出:回填摘要一行。写 C/D/E/F/G。

    所有者定稿 2026-08-16。⚠ 三条:

    · **E 列写 "pass" 不是 "approved"** —— `list_new` 的领任务闸判的是
      `audit_result.lower() == "pass"`。写别的那行永远上不去,而且不报错。
      映射收在 `listing_sheet.AUDIT_RESULT_CN`。
    · **库里没有的 ASIN 一行都不写**(留 E 空)。写个 pending 会让人以为审过了;
      留空则下轮自动重领,而且 `list_new` 只认 pass,留空绝不会误上架。
      摘要里点名有多少行卡在这。
    · 同一个 ASIN 可能在表里有**多行**(不同店铺),按 ASIN 回填到每一行。

    回填失败只告警不失败:结论已经落 PG 了(products 五列 + audit_runs),
    飞书只是人机界面 —— 与订单中心那条同款纪律。
    """
    try:
        asins = sorted({r["asin"] for r in sheet_rows})
        with db.pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL_VERDICT, (asins,))
                got = {r[0]: r for r in cur.fetchall()}
            # F 列写**人话**:`products.audit_reason` 存的是 37 条沃尔玛政策的
            # 类目名,而其中 `General-Use Products` 是"以上全不中"的兜底 ——
            # 落在一把锤子、一个土豆压泥器上时人只会一头雾水(所有者
            # 2026-08-16)。真正的原因在命中的规则里,翻出来放前面
            reasons = audit_store.reject_reasons(conn, asins)
        updates, absent = [], 0
        for r in sheet_rows:
            row = got.get(r["asin"])
            if not row or not row[3]:       # 库里没有 / 还没结论 → 留空
                absent += 1
                continue
            _, title, pt, status, reason, at = row
            why = (audit_reason.human_reason(reasons.get(r["asin"], []), reason)
                   if status == "rejected" else (reason or ""))
            updates.append((r["rownum"], [
                title or "", pt or "",
                listing_sheet.AUDIT_RESULT_CN.get(status, status),
                why[:500],
                at.strftime("%Y-%m-%d") if at else ""]))
        listing_sheet.write_audit_cols(updates, execute)
        # ⚠ 回填的是**整张表里所有已有结论的行**,不是本轮判的那 limit 个 ——
        # 库里早有结论的行本来就该把结论投影出来(那正是"从库里读结果")。
        # dry-run 必须说出真跑会写多少行:所有者 2026-08-16 实遇 dry-run 6 秒、
        # 真跑写了几万行,差异全在这一步而摘要当时只说"回填 0 行"
        out = (f"上架表{'回填' if execute else '**将**回填'} {len(updates)} 行 C~G"
               f"(整表已有结论的都投影,不只本轮判的那些)"
               f"{'' if execute else ';dry-run 一格未写'}")
        if absent:
            out += (f";⚠ {absent} 行库里没有结论,**E 列留空**"
                    f"(下轮自动重领;没数据的那些见下方补采段,已推采集)")
        return out
    except Exception as e:                                      # noqa: BLE001
        logger.warning("上架表回填失败(结论已在 PG,不影响本轮): %s", e)
        return (f"⚠ 上架表回填失败:{e}"
                f"(结论已在 catalog.products;重跑 "
                f"`python cli.py product_audit -p from_sheet=1` 补写)")


# 缺数据自动补采(所有者定稿 2026-08-17)。批次名带北京日界 ⇒ 天然防重:
# 当天第二轮撞名走 409(BatchExistsError),沿用不重推。前缀独立成一档,
# 这样 check_open 圈自己的批次不会碰到 listing_gap_/scrape_missing 的
_GAP_PREFIX = "audit_gap_"
_GAP_CHUNK = 5000          # 单批上限(表驱动的缺口通常几十个,这是护栏不是常态)
_GAP_TIMEOUT_H = 24        # 超过一天没采完就标 timeout(下一轮日界批次会重推)
# 等采集多久(分钟)。与 order_audit 的 _SCRAPE_TIMEOUT_MIN 同量级,理由相同:
# 采集侧对可重试类型走 cap=3 + 最多 2 轮自动重试(间隔 5 分钟),总尝试上限
# 约 9 次 —— 一个批次收敛得多慢由它决定,20 分钟是那条曲线的兜底位置。
# ⚠ 上限还有一层来自调度:审核 18:10、上架 20:00,中间只有 110 分钟,
# 而这段等待是**串在审核里**的(锁被本进程握着)。调大到吃掉上架的时间,
# 表现是上架那条链拿不到锁退 3 空跑一轮 —— 看起来一切正常
_GAP_WAIT_MIN = 20

# 哪些 ASIN 叫"审不了":库里压根没有,或者有行但没标题(采集降级)。
# 两类的处置一样(重采),但写进表格的话要说得不一样 —— 运营看到"没采集过"
# 和"采过但抓不到标题"该做的事不同(后者多半是详情页结构变了/被拦)
_SQL_GAP = """
SELECT asin, (title IS NULL OR title = '') AS no_title
FROM catalog.products
WHERE marketplace = 'US' AND asin = ANY(%s)
"""


def _find_gap(want: list[str]) -> tuple[list[str], list[str]]:
    """输入:待审 ASIN → 输出:(不在库的, 在库但没标题的)。都是"审不了"的。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_GAP, (want,))
        got = dict(cur.fetchall())
    return (sorted(set(want) - set(got)),
            sorted(a for a, no_title in got.items() if no_title))


# ⚠ 审核**不做**"先推采集刷新拿最新数据"(所有者定稿 2026-08-19,当天曾
# 短暂加过又按所有者复议撤回):审核判的是**这个产品卖的是什么**(标题里的
# 品牌词是不是真品牌、是否碰沃尔玛政策)——第一次审核就定性了,改标题/描述
# 不改变它是什么,重采+重审带来的"翻案"更大可能只是 LLM 的随机性。
# 缺口(不在库/无标题)照旧同轮补采——那是"审不了",不是"数据旧"。


def _push_gap(gap: list[str], day: str,
              out: list[str]) -> list[tuple[str, object]]:
    """输入:缺口 ASIN + 日界 → 输出:[(批次名, batch_id)];摘要写进 out。

    日界批次名 ⇒ 天然防重:当天第二轮撞名走 409,沿用既有批次不重复烧配额
    (沿用的那个也要返回 —— 后面要拿它的 batch_id 查失败明细)。
    单批推送失败不连坐其余批次。
    """
    sent = []
    for i in range(0, len(gap), _GAP_CHUNK):
        chunk = gap[i:i + _GAP_CHUNK]
        name = (f"{_GAP_PREFIX}{day}" if len(gap) <= _GAP_CHUNK
                else f"{_GAP_PREFIX}{day}-{i // _GAP_CHUNK + 1:02d}")
        try:
            res = scraper.submit_batch(name, chunk)
            bid = res.get("batch_id")
            scrape_batches.record(name, bid, len(chunk), "pushed",
                                  f"inserted={res.get('inserted')}")
            sent.append((name, bid))
            out.append(f"  已推采集 {name}:{len(chunk)} 个"
                       f"(inserted={res.get('inserted')})"
                       + ("" if scrape_batches.prioritize(name, bid)
                          else ",⚠ 插队没成功(按常规优先级采,可能等不到)"))
        except scraper.BatchExistsError as e:
            scrape_batches.record(name, e.batch_id, len(chunk), "pushed",
                                  "同日已推,沿用既有批次")
            sent.append((name, e.batch_id))
            scrape_batches.prioritize(name, e.batch_id)
            out.append(f"  {name}:今天已推过,沿用既有批次 {e.batch_id}"
                       f"(接着等它采完)")
        except Exception as e:                                  # noqa: BLE001
            logger.exception("补采批次 %s 推送失败", name)
            scrape_batches.record(name, None, len(chunk), "failed",
                                  str(e)[:200])
            out.append(f"  ❌ {name} 推送失败:{e}(表格照样写原因,下轮重推)")
    return sent


def _ingest_batches(names: list[str]) -> str:
    """输入:本轮推的批次名 → 输出:按批摄取摘要。

    2026-08-19 起走采集侧批次端点(`export_batch_records`,契约 §4.11):
    只拉**自己这批**的事件,批内游标每次从 0 拉到底,不碰全局游标、
    **不需要 product_ingest 的锁**——此前是借锁抽全库到当刻头部,与
    product_chain / order_chain 的抽水互相排队。幂等靠 snapshots.source_id,
    与全局泵重复摄取无害。没拉到底的批次摘要里点名,那部分退回下轮审。
    """
    _, note = product_ingest.pump_batches(scraper, db, names)
    return note


def _gap_reasons(sent: list[tuple[str, object]]) -> dict[str, str]:
    """输入:[(批次名, batch_id)] → 输出:{asin: 采集侧 error_type 的人话}。

    所有者定稿 2026-08-17:「有可能有些产品没有采集到或者怎么样,需要把理由
    记录到表格中」。**理由要真的是理由** —— 采集侧的 `error_type` 封闭集
    (`scrape_batches.ERROR_TYPES`)才说得出"验证码"和"页面解析不出"的区别,
    而这两者运营该做的事完全不同(前者换时段能好,后者要去看链接还在不在)。
    统一写"未采集"等于把十一种成因压成一句废话。

    拉不到明细返回空字典,调用方退回泛化措辞 —— 不编。
    """
    out: dict[str, str] = {}
    for name, bid in sent:
        if not bid:
            continue
        try:
            _, by_asin = scrape_batches.pull_failures(name, bid)
        except Exception as e:                                  # noqa: BLE001
            logger.warning("拉批次 %s 失败明细失败(理由退回泛化): %s", name, e)
            continue
        for asin, et in by_asin.items():
            out[asin] = f"{et}({scrape_batches.ERROR_TYPES.get(et, '未登记类型')})"
    return out


def _close_gap(want: list[str], sheet_rows: list[dict], execute: bool,
               wait_min: int) -> list[str]:
    """输入:待审 ASIN + 待审行 + 真跑? + 等采集分钟 → 输出:摘要行。

    **同轮闭环**(所有者定稿 2026-08-17):「产品审核不能等下一次,要轮询等采完
    拿数据审核,下一次运行是第二天,时间很长,并且不审核,后面的上架也做不了」。

    所以这一段必须跑在**判定之前** —— 采回来的产品这一刻就进了
    `catalog.products`,主候选查询照常把它们捞起来判掉,不需要第二遍判定循环。
    (首版把它放在判定之后,只推不等 ⇒ 采回来要等第二天 18:10 才审、20:00 才上,
    整条上架链每引进一批新品就白等一天。)

    五步:

      ① 推今天的缺口(日界批次名,撞名沿用)+ 插队。**只推缺口**:在库的
         待审行不做"先刷新再审"(所有者复议定稿 2026-08-19,理由见模块头注
         ——审核判的是"它是什么",第一次就定性了)。
      ② **轮询等它采完**(`wait_settled`)。超时不是失败:已采到的照常进增量流。
      ③ **就地按批摄取**(批次端点,见 `_ingest_batches`;不需要锁)——
         批次 completed **不等于**我们库里有数据,中间还隔着一次导出。
         少这一步的话等了半天照样"库里没有",而且看起来像采集侧没干活。
      ④ 复查还缺谁,把**采集侧给的真实 error_type** 写进表格 F 列
         (`_gap_reasons`);E 列一个字不动(`write_audit_notes` 头注说了为什么)。
      ⑤ **落定台账**(`check_open`)。

    ⚠ ⑤ 为什么在最后、而不是开头报"上一批"(所有者 2026-08-17 质疑:「我都已经
    是当时轮询了,不应该还存在上一批吧」—— 对,而且首版把它放开头是个真 bug):
    `wait_settled` **故意不碰台账**(见它的头注:台账落定归 check_open)。所以
    每一轮成功跑完都会把自己这批留在 `ops.scrape_batches` 的 `pushed` 上,
    而开头那次 check_open 看到的就是**昨天自己没关的那笔** —— 于是天天报一条
    "上一批 ✅ 采完",看着像有积压,其实只是自己的台账迟了一天关。
    放到最后:同一轮里推的、等的、关的是同一批,台账当轮闭合,正常情况下
    第二天开头没有任何遗留。

    **真正还会有遗留的三种情况**(这时 ⑤ 会报出来,而且**该报**):
    等采集超时(那批还在采集侧跑)、`gap_wait=0`(只推不等)、
    本轮中途炸了/被打断(推出去了但没等到)。台账 24 小时没关会被标 timeout。

    `wait_min=0` = 只推不等(退回跨轮形态)。采集侧病了、或者人只想让它把队排上
    时用;摘要里会明说这一轮不等。

    **插队**(`scrape_batches.prioritize`,判据就是"本侧在等这批采集")。
    这是时间账逼出来的:审核 18:10 起跑,而 `product_refresh` 13:00 推的十几万个
    任务这时很可能还在排。不插队的话那 20 分钟几乎注定等不到 —— 同轮闭环写了
    但从不生效,而且表现是"每天都超时",看着像采集侧慢。插队失败只告警
    (best-effort),摘要里点明"可能等不到"。

    全程 best-effort:采集侧挂了不该把审核链拖下水(库里已有数据的那些照常判)。
    但**每一种失败都要出现在摘要里** —— 静默跳过就退回原来那个"永远空着而且
    没人知道为什么"的坑。
    """
    if not want:
        return []
    out: list[str] = []
    absent, degraded = _find_gap(want)
    gap = absent + degraded
    if gap:
        _run_gap_round(gap, absent, degraded, sheet_rows, execute, wait_min,
                       want, out)
    # ⑤ 台账落定:**本轮缺口为空也要跑**。缺口为空只说明今天没新批次,
    #    不说明没有遗留(上一轮超时/gap_wait=0/中途被打断的那批还挂着)——
    #    只在有缺口时才关台账的话,那笔遗留会一直挂到有下一次缺口
    out += _settle_ledger(execute)
    return out


def _settle_ledger(execute: bool) -> list[str]:
    """输入:是否真跑 → 输出:台账落定摘要(没有在途批次则空,不出空节)。

    `check_open` 会顺手把已采完的标 completed、超 24 小时的标 timeout,并拉失败
    明细落 `ops.scrape_failures`。放在整段最后,所以正常情况下它关掉的就是
    **本轮自己推的那批** —— 不会隔一天再报一次(首版放开头就是那个毛病)。
    """
    if not execute:
        return []               # 查在途会改台账状态,dry-run 不碰
    try:
        lines = scrape_batches.check_open(_GAP_PREFIX, _GAP_TIMEOUT_H)
    except Exception as e:                                      # noqa: BLE001
        logger.warning("补采台账落定失败(不影响本轮): %s", e)
        return [f"  ⚠ 补采台账落定失败:{e}"]
    if lines == ["无在途采集批次"]:
        return []
    return ["补采批次台账:"] + lines


def _run_gap_round(gap: list[str], absent: list[str], degraded: list[str],
                   sheet_rows: list[dict], execute: bool, wait_min: int,
                   want: list[str], out: list[str]) -> None:
    """输入:本轮缺口 + 上下文 → 输出:无(摘要写进 out)。见 `_close_gap` 头注。"""
    day = datetime.now(kpi.CN_TZ).strftime("%Y%m%d")
    head = (f"⚠ 审不了 {len(gap)} 个 ASIN:不在库 {len(absent)}"
            + (f" / 采集降级无标题 {len(degraded)}" if degraded else ""))
    if not execute:
        out.append(f"{head} —— 真跑时会推采集批次 {_GAP_PREFIX}{day}、"
                   f"等它采完(最多 {wait_min} 分钟)、就地按批摄取,"
                   f"**采回来的这一轮就审掉**;仍缺的把理由写进表格 F 列"
                   f"(dry-run 一格未写)")
        _note_gap(sheet_rows, set(gap), set(absent), {}, day, False, out)
        return

    out.append(head)
    sent = _push_gap(gap, day, out)

    if not sent:
        out.append("  一个批次都没推成:本轮这些行审不了,理由照写")
    elif wait_min <= 0:
        out.append(f"  gap_wait=0:只推不等,这批退回下轮审"
                   f"(采回来在 {_GAP_PREFIX}{day},下一轮自动捞起)")
    else:
        line, stuck = scrape_batches.wait_settled([n for n, _ in sent],
                                                  wait_min)
        out.append(f"  {line}")
        if stuck:
            out.append(f"  ⚠ {stuck} 个批次超时仍在跑:这部分退回下轮审"
                       f"(已采到的下面就摄进来)")
        out.append(f"  {_ingest_batches([n for n, _ in sent])}")

    # ④ 复查:摄取之后还缺谁。**必须重查库** —— 拿推送前那份 gap 写理由的话,
    #    刚采回来的那些会被误报成"未采集",而它们其实这一轮就要被判掉
    still_absent, still_degraded = _find_gap(want)
    still = set(still_absent) | set(still_degraded)
    rescued = len(gap) - len(still & set(gap))
    if rescued:
        out.append(f"  ✅ 补采回来 {rescued} 个,**本轮就审**(已进候选)")
    if still:
        out.append(f"  ⚠ 仍缺 {len(still)} 个:理由写进表格 F 列,下轮重试")
    _note_gap(sheet_rows, still, set(still_absent),
              _gap_reasons(sent) if still else {}, day, True, out)


def _note_gap(sheet_rows: list[dict], still: set, absent: set,
              reasons: dict[str, str], day: str, execute: bool,
              out: list[str]) -> None:
    """输入:待审行 + 仍缺集合 + 采集侧理由 → 输出:无(写表格 F 列,摘要进 out)。

    ⚠ **只写 F,E 列一个字不动**。E 一有值这行就不再被 `audit_targets` 领走,
    往里写个"未采集"就等于这行从此退出审核通道 —— 采回来了也没人再审它,
    而表面上"表里写着原因呢"。
    """
    if not still:
        return
    notes = []
    for r in sheet_rows:
        if r["asin"] not in still:
            continue
        why = ("未采集(库里没有这个 ASIN)" if r["asin"] in absent
               else "采集降级(采到了但没有标题)")
        det = reasons.get(r["asin"])
        notes.append((r["rownum"],
                      f"{why};采集失败:{det}({day})" if det
                      else f"{why},已推采集但本轮没等到,下轮重试({day})"))
    try:
        n = listing_sheet.write_audit_notes(notes, execute)
        out.append(f"  表格 F 列{'已写' if execute else '**将**写'} "
                   f"{n if execute else len(notes)} 行原因"
                   f"(**E 列留空**,下轮照样重新领取)")
    except Exception as e:                                      # noqa: BLE001
        logger.warning("缺数据原因回写失败(不影响本轮): %s", e)
        out.append(f"  ⚠ 原因回写飞书失败:{e}(采集已推,下轮重试回写)")


# 连接余量钳制已抽到 services/db_guard(list_new 的 LLM 出参期共用同一护栏);
# 这里留同名别名:调用点、docstring 指路、tests 里按名字钉住的断言全都不动。
_cap_by_connections = db_guard.cap_workers


def run(params: dict) -> str:
    """输入:params(asins/limit/mode/r5/execute/from_sheet)→ 输出:判定统计摘要。"""
    execute = bool(params.get("execute"))
    limit = int(params.get("limit", 500))
    backfill = str(params.get("mode", "")).strip() == "backfill"
    adopt_only = str(params.get("adopt_only", "")).strip() == "1"
    if adopt_only and not backfill:
        raise ValueError("adopt_only=1 只在 mode=backfill 下有意义"
                         "(它采用的是 audit_runs 里的历史结论)")
    r5_on = str(params.get("r5", "")).strip().lower() == "on"
    # L3 默认开(旧仓 run_l3 默认 True);L4 默认关(批复 #2,显式 l4=on)
    run_l3 = str(params.get("l3", "")).strip().lower() != "off"
    run_l4 = str(params.get("l4", "")).strip().lower() == "on"
    # stages=L0:只跑 Phase0,纯查库零 LLM(所有者 2026-08-18)。
    # 命中 → 正常 reject 落库;未命中 → 不落结论不盖版本(见 audit_one)。
    # 值域先只放 L0 —— L1 起每一层都可能要 LLM,"指定到哪层"再扩时
    # 必须逐层想清楚"没走完的行算什么",不预开口子。
    stages = str(params.get("stages", "")).strip().upper()
    if stages not in ("", "L0"):
        raise ValueError(f"stages 只支持 L0(收到 {stages!r});"
                         f"L3/L4 已有独立开关 -p l3=off / -p l4=on")
    only_l0 = stages == "L0"
    if str(params.get("mode", "")).strip() == "pass" and not only_l0:
        # pass 全量重扫只准走零 LLM 的 L0(黑名单翻案场景);全链重审全部
        # pass = 重烧全库 LLM,真要做请用 force_rerun=<版本> 显式来
        raise ValueError("mode=pass 只与 stages=L0 连用"
                         "(加 -p stages=L0;全链重审请用 force_rerun)")
    # 判定并发(旧仓 10 worker 常驻先例):worker 只做判定(LLM+只读+幂等
    # 缓存写,各自 autocommit 连接),落库仍归主线程单连接(savepoint 语义
    # 不变)。r5=on 强制 1(uspto 单连接不可跨线程)
    want_workers = max(1, int(params.get("workers", _DEFAULT_WORKERS)))
    workers = min(want_workers, _MAX_WORKERS)
    if workers != want_workers:
        # 静默钳制 = 拿着错的数做并发决策(生产实测 2026-08-14:所有者用
        # workers=32 测吞吐,实际跑的是 16 而输出只字未提)
        logger.warning("workers=%d 超上限,实际用 %d(I/O 密集,上限由 LLM "
                       "侧承受力定,不是本机核数)", want_workers, workers)
    if r5_on:
        workers = 1
    workers, conn_note = _cap_by_connections(workers)
    # ── 上架表驱动(所有者定稿 2026-08-16;领任务在 _claim_from_sheet)──────
    sheet_rows: list[dict] = []
    sheet_head: list[str] = []
    sheet_want: list[str] = []
    if params.get("from_sheet"):
        sheet_rows, sheet_want, sheet_head = _claim_from_sheet(limit)
        if not sheet_want:
            return sheet_head[0]
        # ⚠ 补采闭环必须在**候选查询之前**(所有者定稿 2026-08-17:「产品审核
        # 不能等下一次,要轮询等采完拿数据审核」)。跑在这里,采回来的产品
        # 这一刻已经在 catalog.products 里,下面的候选查询照常把它们捞起来判掉
        # —— 不需要第二遍判定循环,也不用等到第二天
        sheet_head += _close_gap(sheet_want, sheet_rows, execute,
                                 int(params.get("gap_wait",
                                                _GAP_WAIT_MIN)))
        params = {**params, "asins": ",".join(sheet_want)}

    where, extra = _pick_where(params)
    if adopt_only:
        # 只采用模式下**只挑有历史结论的行**:否则候选按 audited_at NULLS
        # FIRST 取前 N,没历史的那批不会被消耗、每轮都排在前面重复捞
        # (生产实测 2026-08-14:采用率 122k→88k→65k→47k→34k→25k 一路塌,
        #  第 6 轮 20 万候选里 17.5 万是上轮已确认无历史的行)
        where = f"({where}) AND {_HAS_HISTORY_SQL}"
    if "asins" in extra:
        # 指定 ASIN 时 limit 不许截断(评审 I-6:传 600 只审 500 且无提示)
        limit = max(limit, len(extra["asins"]))

    import contextlib
    uspto_cm = db.uspto_conn() if r5_on else contextlib.nullcontext()
    with db.pg_conn() as conn, uspto_cm as uspto:
        ctx = audit_rules.load_context(conn, uspto=uspto)
        query_params = {"marketplace": "US", "limit": limit, **extra}
        # 复烧护栏只在 dry-run 生效:execute 写 audited_at 天然推进;
        # dry-run 后紧跟的 --execute 也不能被自己刚落的 runs 拦掉
        guard = "" if (execute or _is_forced(params, extra)) \
            else _RECENT_RUN_GUARD
        if str(params.get("rerule", "")).strip():
            sheet_head = _rerule_head(conn, params["rerule"].strip(),
                                      where, extra, limit) + sheet_head
        with conn.cursor() as cur:
            cur.execute(_CANDIDATE_SQL.format(where=where,
                                              recent_guard=guard),
                        query_params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        adopted_n, adopted = (0, set())
        if backfill:
            adopted_n, adopted = _adopt_history(
                conn, [r["asin"] for r in rows], execute)
        if adopt_only:
            # 只采用不判定(所有者 2026-08-14:先零成本把有历史结论的扫完,
            # 再单独安排要真判的那批)。86 万可采用 vs 33 万要真判,混在
            # 一起跑等于为了采用而顺带付 33 万次 LLM
            return (f"product_audit(仅采用历史,零 LLM):候选 {len(rows)} → "
                    f"采用 {adopted_n}"
                    + ("" if execute else "(dry-run:未写库)")
                    + f";其余 {len(rows) - adopted_n} 条无历史,需另跑判定")

        counts = {"pass": 0, "reject": 0, "pending": 0}
        no_title = seller_missing = policy_unknown = 0
        stage_stats = {"L3_ran": 0, "L3_reject": 0, "L3_pending": 0,
                       "L4_ran": 0, "L4_reject": 0}
        l4_fail: dict = {}           # rule_code → 次数(评审 P1-2:层死≠层净)
        audit_rules.audit_l1_llm.reset_stats()   # 本轮 rerank 计数从零起
        from api import llm as _llm
        _llm.reset_retry_stats()                 # 退避计数同样每轮从零
        events = []
        row_errors, consec_errors = 0, 0
        l0_untouched = 0
        done_n = 0
        t0 = time.monotonic()
        todo = []
        for row in rows:
            if row["asin"] in adopted:
                continue
            if not row.get("title"):
                no_title += 1        # 采集降级无标题:不够格判定,跳过不写结论
                continue
            if not row.get("seller_id"):
                seller_missing += 1  # 卖家闸未生效面(契约外字段,摘要必亮)
            todo.append((row["asin"], audit_rules.product_info_from_row(row)))

        # 判定并发:worker 各领一条 autocommit 连接跑 audit_one(LLM 秒级,
        # 是墙钟大头);结果按完成序回主线程,落库/计数全在主线程单连接上
        # (savepoint 语义与串行版完全一致)。连错 ≥5 = 系统性故障,炸停
        import queue as _queue
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from contextlib import ExitStack

        with ExitStack() as stack:
            pool: _queue.SimpleQueue = _queue.SimpleQueue()
            for _ in range(workers):
                pool.put(stack.enter_context(db.pg_conn(autocommit=True)))

            def _judge(product):
                c = pool.get()
                try:
                    return audit_rules.audit_one(product, ctx, c,
                                                 run_l3=run_l3, run_l4=run_l4,
                                                 only_l0=only_l0)
                finally:
                    pool.put(c)

            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_judge, p): asin for asin, p in todo}
                # 落库缓冲:判定并发调到 128 之后,主线程"逐行 savepoint +
                # 逐行 INSERT"就是新的瓶颈(所有者定稿 2026-08-17:批量落库)。
                # 攒够 _PERSIST_BATCH 一次 executemany 落 runs+hits。
                buf: list = []

                def _flush(force: bool = False) -> int:
                    """输入:是否强制 → 输出:本次落库失败的行数(已计数的不算)。

                    ⚠ 批量一炸整批回滚,而里面多半只有一行是脏的 —— 已付费的
                    LLM 结果不能陪葬。所以 except 后对这一批**改走逐行**,
                    坏行单独隔离,好行照落:好路径拿批量的速度,坏路径保住
                    「一行落库报错不炸整批」那条评审结论。
                    """
                    nonlocal buf
                    if not buf or (not force and len(buf) < _PERSIST_BATCH):
                        return 0
                    batch, buf = buf, []
                    try:
                        with conn.transaction():
                            ids = audit_store.persist_runs(conn, batch)
                            if execute:
                                for rid, oc in zip(ids, batch):
                                    audit_store.write_conclusion(conn, oc)
                                    ev = audit_store.event_row(oc, rid)
                                    if ev:
                                        events.append(ev)
                        return 0
                    except Exception as e:              # noqa: BLE001
                        logger.warning("批量落库失败(%d 行),改逐行隔离:%s",
                                       len(batch), e)
                    bad = 0
                    for oc in batch:
                        try:
                            with conn.transaction():
                                rid = audit_store.persist_run(conn, oc)
                                if execute:
                                    audit_store.write_conclusion(conn, oc)
                                    ev = audit_store.event_row(oc, rid)
                                    if ev:
                                        events.append(ev)
                        except Exception as e2:         # noqa: BLE001
                            bad += 1
                            logger.error("单行审核落库失败 asin=%s:%s",
                                         oc.asin, e2)
                    return bad

                for fut in as_completed(futs):
                    asin = futs[fut]
                    try:
                        outcome = fut.result()
                        if outcome is None:     # stages=L0 未命中:保持原结论
                            l0_untouched += 1
                            consec_errors = 0
                            continue
                        buf.append(outcome)
                        bad = _flush()
                        if bad:
                            row_errors += bad
                    except Exception as e:  # noqa: BLE001 —— 单行隔离,计数亮出
                        row_errors += 1
                        consec_errors += 1
                        logger.error("单行审核失败 asin=%s:%s", asin, e)
                        if consec_errors >= 5:
                            for f in futs:
                                f.cancel()
                            raise RuntimeError(
                                f"连续 {consec_errors} 行失败(共 {row_errors}),"
                                f"疑似系统性故障,停批。最后错误:{e}") from e
                        continue
                    consec_errors = 0
                    done_n += 1
                    if done_n % _COMMIT_EVERY == 0:
                        # 分段落定:此刻之前判的都已持久,中断只丢最后一段
                        # ⚠ 先把缓冲冲干净再 commit —— 缓冲里还压着没落库的行时
                        # 提交,那句"都已持久"就是谎话(它们要等下一批才落)
                        row_errors += _flush(force=True)
                        conn.commit()
                        if events:
                            product_events.record_many(conn, events)
                            conn.commit()
                            events = []
                        rate = done_n / max(time.monotonic() - t0, 1e-6)
                        logger.info("进度 %d/%d(%.0f 条/秒,已提交)",
                                    done_n, len(todo), rate)
                    if outcome.l3 is not None:
                        stage_stats["L3_ran"] += 1
                        if outcome.l3.verdict == "reject":
                            stage_stats["L3_reject"] += 1
                        elif outcome.l3.verdict == "pending":
                            stage_stats["L3_pending"] += 1
                    if outcome.l4 is not None:
                        stage_stats["L4_ran"] += 1
                        if outcome.l4.verdict == "reject":
                            stage_stats["L4_reject"] += 1
                        for h in outcome.l4.hits:
                            if h.penalty == 0 and h.rule_code.startswith("l4_"):
                                l4_fail[h.rule_code] = \
                                    l4_fail.get(h.rule_code, 0) + 1
                    counts[outcome.verdict] += 1
                    if (outcome.verdict == "reject" and ctx.known_policies
                            and not audit_reason.known_policies_check(
                                outcome.final_reason_category,
                                ctx.known_policies)):
                        policy_unknown += 1
                # 收尾冲刷:最后不满一批的那些也要落库,漏了就是"判了没存"
                row_errors += _flush(force=True)
        if execute and events:
            product_events.record_many(conn, events)

        # pending 可见性(一致性审查 3.5):只报总量——audited_at 是"审核动作
        # 时刻"不是"进入 pending 时刻",拿它算龄期两种来源口径相反(评审 P1-3/
        # I-3);诚实的龄期需要 pending_since 列,批次 C 随 L1 一并定
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM catalog.products "
                        "WHERE marketplace = 'US' AND audit_status = 'pending'")
            (pending_total,) = cur.fetchone()

    judged = sum(counts.values())
    lines = list(sheet_head)        # 上架表领任务的口径放最前(含"还剩多少没审")
    lines += [f"product_audit({resources.AUDIT_RULES_VERSION}"
              f"{',补刷' if backfill else ''}{',R5开' if r5_on else ''}"
              f"{',只跑L0' if only_l0 else ''}"
              f"{',L3关' if not run_l3 and not only_l0 else ''}"
              f"{',L4开' if run_l4 else ''}):"
              f"候选 {len(rows)},判定 {judged}"
              f"(过 {counts['pass']}/拒 {counts['reject']}/待定 {counts['pending']})"]
    if only_l0:
        lines.append(
            f"stages=L0:Phase0 未命中 {l0_untouched} 个**保持原结论**"
            f"(不落 runs、不盖版本,仍在候选集 —— 要终局请补全链重审)")
    l1s = audit_rules.audit_l1_llm.STATS
    if l1s.get("llm_called", 0) or l1s.get("no_candidate", 0):
        lines.append(f"L1 rerank:调用 {l1s['llm_called']}"
                     f"(失败 {l1s.get('llm_failed', 0)}/坏 JSON {l1s.get('bad_json', 0)}),"
                     f"unknown→待定 {l1s.get('unknown', 0)},"
                     f"字典回落 {l1s.get('dict_fallback', 0)},"
                     f"无候选→待定 {l1s.get('no_candidate', 0)},"
                     f"低置信采纳 {l1s.get('conf_low', 0)}")
        # 候选路归因:哪一路把最终 PT 送进来的(新加的祖先/字典两路是否有用)
        picked = {k[len("picked_"):]: v for k, v in l1s.items()
                  if k.startswith("picked_") and v}
        if picked:
            lines.append("  选中候选来自:" + " / ".join(
                f"{k} {v}" for k, v in sorted(picked.items(),
                                              key=lambda kv: -kv[1])))
        opened = {k: v for k, v in l1s.items()
                  if k.startswith("open_") and v}
        if opened:
            lines.append("  零参考两阶段:" + " / ".join(
                f"{k} {v}" for k, v in sorted(opened.items())))
        if l1s.get("unknown_retry_called", 0):
            called = l1s["unknown_retry_called"]
            saved = l1s.get("unknown_retry_saved", 0)
            lines.append(f"  候选都不合适的二次机会:重判 {called},救回 {saved}"
                         f"({called - saved} 条换开放候选面仍解不出 → 待定)")
    # 限流观测:退避是静默的,不亮出来就只能靠耗时反推"是不是加并发没用"
    from api import llm as _llm2
    retries = {k: v for k, v in _llm2.RETRY_STATS.items() if v}
    # 只有 http_429 才叫撞限流:other=网络/解析抖动、5xx=对端故障,三者
    # 处置完全不同(生产实测 2026-08-14:19 次 other 被这行说成"已撞限流",
    # 把所有者引向了错误的结论——降并发对网络抖动毫无用处)
    if retries.get("http_429"):
        tail = (",LLM 退避 " + " / ".join(f"{k} {v}" for k, v
                                          in sorted(retries.items()))
                + " ⚠ **已撞限流**,再加并发只会更慢")
    elif retries:
        tail = (",LLM 退避 " + " / ".join(f"{k} {v}" for k, v
                                          in sorted(retries.items()))
                + "(无 429 = 没撞限流;other/5xx 是网络抖动与对端故障,"
                  "降并发解决不了)")
    else:
        tail = ",LLM 零退避(未撞限流,可继续加并发)"
    lines.append(f"并发 {workers}{tail}")
    if conn_note:
        # 钳制/查不到余量都必须进摘要 —— 只写日志的话表现是"并发调了没效果"
        lines.append(conn_note)
    l1_blocked = (l1s.get("seed_excluded_direct", 0)
                  + l1s.get("llm_excluded", 0) + l1s.get("seed_excluded", 0)
                  + l1s.get("publication_forbidden", 0))
    if l1_blocked:
        lines.append(f"L1 硬拦:直出级 seed {l1s.get('seed_excluded_direct', 0)}"
                     f" / rerank 级 excluded {l1s.get('llm_excluded', 0)}"
                     f"(seed 补位 {l1s.get('seed_excluded', 0)})"
                     f" / 出版物 {l1s.get('publication_forbidden', 0)}")
    if stage_stats["L3_ran"]:
        lines.append(f"L3 语义:判 {stage_stats['L3_ran']}"
                     f"(拒 {stage_stats['L3_reject']}/"
                     f"LLM 故障待定 {stage_stats['L3_pending']})")
    if stage_stats["L4_ran"]:
        lines.append(f"L4 视觉:判 {stage_stats['L4_ran']}"
                     f"(拒 {stage_stats['L4_reject']})")
    if l4_fail:
        # 层死与层净必须长得不一样(评审 P1-2):故障回落 pass 逐码亮出
        detail = ", ".join(f"{k}×{v}" for k, v in sorted(l4_fail.items()))
        lines.append(f"⚠ L4 故障回落 pass:{detail}"
                     f"(全故障=层未生效,先查 ARK_API_KEY/取图)")
    if row_errors:
        lines.append(f"⚠ 单行失败跳过 {row_errors}(savepoint 隔离,详见日志)")
    if "asins" in extra and len(rows) < len(extra["asins"]):
        lines.append(f"⚠ 指定 ASIN {len(extra['asins'])} 个,库中命中 {len(rows)}"
                     f"——缺的 {len(extra['asins']) - len(rows)} 个不在 catalog.products")
    if adopted_n:
        lines.append(f"历史结论采用 {adopted_n}(不写新 run,detail 指回原 run_id)")
    if no_title:
        lines.append(f"无标题跳过 {no_title}(采集降级,不够格判定)")
    if seller_missing:
        lines.append(f"⚠ 卖家字段缺失 {seller_missing}/{judged}"
                     f"(buybox_seller_id 契约外字段;恒缺=卖家闸未生效,需契约扩展)")
    if policy_unknown:
        lines.append(f"⚠ 理由映射落 37 政策外 {policy_unknown} 条(详见日志,只记不改判)")
    if r5_on and getattr(ctx, "uspto_failures", 0):
        lines.append(f"⚠ R5 查询失败 {ctx.uspto_failures} 次"
                     f"{'(≥5 已自动关停本轮 R5)' if ctx.uspto is None else ''}")
    lines.append(f"全库 pending 存量 {pending_total}")
    if sheet_rows:
        # ⚠ 投影必须在补采**之后**跑(它在 _close_gap 之前就跑了的话,
        # 这一轮刚补采回来、刚判出结论的那些行还写不进表格)
        lines.append(_project_to_sheet(sheet_rows, execute))
    if not execute:
        lines.append("(dry-run:runs/hits 已落,products 五列与事件未写)")
    return "\n".join(lines)
