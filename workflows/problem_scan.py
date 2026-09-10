"""problem_scan — 问题商品扫描定性(批次 E,批复 #8;只读沃尔玛,**不发任何 feed**)。

用法:
  python cli.py problem_scan                  # 扫描 + 落建议行(可随时跑)
  python cli.py problem_scan -p store=A085朱丽霖
  python cli.py problem_scan -p preview=1     # 只打印不落建议行
  python cli.py problem_scan --dry-run        # 同上(--dry-run 等价于 preview)

本工作流是问题商品链拆分后的**建议半边**。原来 problem_product_cleanup 一个
文件里既做"查库归类决定该怎么处置",又做"发 feed 真删真补"。两件事的风险等级
差着数量级:前者纯只读、随时可跑;后者 DELETE_ITEM 不可逆。合在一起的后果是
想看看该删哪些就得跑一个 DANGEROUS 工作流,而且建议本身不留痕,事后无从追
"当初为什么删它"。

拆开后:
  problem_scan(本文件,DANGEROUS=False)  查库 → 归类 → 写事件 + 落建议行
  problem_product_cleanup(DANGEROUS=True) 只消费建议行,自己不做任何决策

两个来源(source 列):
  scan   catalog.walmart_items 里**一切未缺席的行**(所有者定稿 2026-09-10:
         「扫描面不再限制,按分类结果处置」)。删不删由归类的**原子集合**判:
         原文只含可恢复原子(End Date 过期 / Stage 等上线,唯一出处
         services/error_taxonomy.RECOVERABLE_CODES)→ 不删;其余一律删,
         不看 published_status / lifecycle。无原因的行不是候选。
         逐原子明细落 detail.atoms(事件与建议行都记),不再只留主码。
         历史:2026-08-28「非 PUBLISHED 一律删除」、2026-09-06「RETIRED 全豁免」
         两条状态口径同日退役,依据见 _SQL_ITEMS 头注。
  audit  审核链判 reject 但**还在架**的产品 —— 审核说不该卖、沃尔玛后台还挂着,
         这个缺口原来没有任何工作流盯着(批复 #8 要求补上)

⚠ **只建议,不动状态、不发 feed。** 本工作流写库的只有两处:产品事件(归类)
与 ops.dispositions 建议行,都是可重跑的幂等写。

去重口径(2026-08-28 反补退役后剩两条,注释记的是生产事故的教训):
  ① 在途/待观测:feed_items 有 submitted 未落定(滚动 48h 封顶),或已落定
     success 但 catalog_sync 尚未重新观测 → 不建议
  ② 归类事件:同 (店铺,SKU) **原子码集合**未变不重复记(2026-09-10 前按主码)
注:①在这里是**预筛**,不是最终闸门——真正的在途防重在 api/feeds.submit_feed
的 ops.feed_log 里(提交时判,返回 outcome=dedup)。预筛只是省得把注定被拦下的
行也建成建议。

店铺闸:ops.store_kpi_daily 最新 store_status 非 ACTIVE 的店整体跳过。

调度顺序:catalog_sync → problem_scan → problem_product_cleanup(真跑)。
"""

import logging
import re

from registry import db
from registry import resources
from services import blacklist, blacklist_sheet, dispositions
from services import error_taxonomy, feed_track
from services import product_events, store_absence

DANGEROUS = False       # 只读沃尔玛;写库仅限事件与建议行,都可重跑
SUPPORTS_STORE = True   # 接受 -p store=X 单店范围(cli 链尾缺席店重赛靠它识别)

# 审核判拒的删除:**单店单轮上限**(限额表「下架限制」列,与维护链删除、
# product_clear 同一个配额口径)。
# 为什么必须有(2026-08-22 把 product_audit 接进 product_chain 时补):接链
# 之后「翻案 → 建议 → 删除」整条是**无人值守**的,而一次黑名单导入或规则
# 收紧可能同时翻掉上千个在架行 —— 没有刹车的话 13:00 那一轮会一次性全部
# 提交,而 DELETE_ITEM 不可逆。超上限的**不是丢弃,是留到下轮**:每天削一层,
# 人有时间在摘要里发现不对。
# ⚠ 限额表读不到时**退到常量而不是不限**(fail-closed):这道闸的存在意义
# 就是防"一次删光",读不到表就退回不限等于闸不存在。
logger = logging.getLogger("workflows.problem_scan")

# 扫描面 = **一切未缺席的行**(所有者定稿 2026-09-10:「扫描面不再限制,按分类结果
# 处置,所有状态的产品都需要扫描」)。published_status / lifecycle_status 都不再是
# 筛选条件 —— 删不删由 plan() 按原子归类判(原子集合 ⊆ 可恢复码不删,其余删),
# 状态列只带回来给摘要分档。此前两条**状态口径**同日退役,理由留档:
#   · 2026-08-28「非 PUBLISHED 一律删除」:状态即判据,单独一条 End Date 过期也删,
#     可恢复与不可恢复不分 —— 所有者 2026-09-10 要求分开。
#   · 2026-09-06「RETIRED 全豁免」:当时依据是 08-28 可见性变更翻回来的死档
#     (10,191 行)发 DELETE_ITEM 删不掉;2026-09-09 沃尔玛已把列表可见性改回去
#     (A109 在册 6858 → 3371,3487 行当轮判缺席),死档随 missing_since 出了
#     扫描面。RETIRED 行照扫:退市 = Site End Date 设成过去,通常只带「End Date
#     过期」一个原子,按可恢复留;带政策原子的照删;删不掉的(回执「已停用」
#     60706056565050 等死档码)由 2026-09-09 的死档/永久拒两道回执闸兜住,
#     见 _load_state 的 receipt_blocked,不会每天重发。
#   · published_status IS NULL 也进扫描面:判据是原文不是状态;没采到状态的行
#     原文照样是当轮扫回来的(walmart_catalog 每轮整行覆盖)。
# 保留的两条是**操作层**边界,不是判据:
#   · missing_since IS NULL:缺席行不在目录里,无从处置(缺席 ≠ 恢复正常)。
#   · **在途改码的旧码不进扫描面**(SKU 改造批次 3,O4):SkuUpdate 生效有
#     15 分钟到 4 小时的窗口(官方),窗口内旧码可能被观测成非 PUBLISHED 且
#     missing_since 仍为 NULL —— 正好落进上面这三条,当轮就被建议 DELETE_ITEM,
#     而 DELETE 不可逆:一次**成功**的改码会被自己的自动链当场删掉。改码期间
#     的非 PUBLISHED 是过程态不是问题商品,判不准就判活。这道闸必须钉在扫描面
#     这一层,不许靠"先跑谁后跑谁"(conventions §三:调度顺序不许承载判据)。
#     登记簿那一跳走 NOT EXISTS 而不是 JOIN:扫描面的行数不许被它改变。
#     改码前 replaced_by 全库为 NULL ⇒ NOT EXISTS 恒真,结果集逐行不变。
#   ⚠ 四列的**位置顺序不许动**(_load_state 按位置解包成
#     store/sku/reasons/published_status)。
_SQL_ITEMS = """
SELECT w.store, w.sku, w.unpublished_reasons, w.published_status
FROM catalog.walmart_items w
WHERE w.missing_since IS NULL
  AND NOT EXISTS (SELECT 1 FROM catalog.listing_sources ls
                  WHERE ls.store = w.store AND ls.sku = w.sku
                    AND ls.replaced_by IS NOT NULL)
"""
# 防重口径(所有者拍板 2026-08-11,替代旧系统的"同一自然日"——那是一天
# 跑 4 次的产物,现按日执行):
# ① submitted 无终态 → 拦,但**滚动 48h 封顶**:超 48h 还没终态,这个 feed
#    大概率丢了(feed_poll 的 pending 告警早该响了),继续拦等于让该商品
#    永久漏删。48h 内照拦——feed 还在沃尔玛队列里,叠发 = 重复提交制造机。
# ② success 且 resolved_at > last_seen_at(待观测)→ 拦到 catalog_sync 重扫
#    为止;重扫后商品**还在**问题清单里 = 沃尔玛说删成了实际没删掉 ⇒
#    本条不再命中,直接重发,不等 48h(所有者原话:"有终态但又扫到了,
#    说明提交成功、给了结果、事实上没操作成功,直接再次执行")。
# ③ failed 不拦(该重试)。
# 在途口径**不分 feed 类型是有意的**(2026-08-24 复核确认保留):上架 feed
# 在途也算在途 —— 刚上架就 SYSTEM_PROBLEM 的商品常在 QARTH 合规复审
# (最长 48h),复审期内追发 DELETE_ITEM 属于过早,复审通过它会自己恢复。
# 但摘要必须分开报(处置在途 vs 上架/维护在途),否则"跳过 778"读不出
# 里面有多少是等复审的新品(生产实遇 B018BDZQUQ 排查半天)。
# MP_MAINTENANCE 不再列处置类(2026-08-28 反补退役):本链不再发它,在途的
# MP_MAINTENANCE 都是维护链的标题/字段操作,按「上架/维护在途」分档报
# ⚠ 改码后新码继承旧码的在途 feed(SKU 改造批次 3,O6):旧码上还没落定的
# 上架/维护/处置 feed 指着的是**同一个 item**,新码在 ops.feed_items 里没有
# 任何历史,不继承就成了"从没提交过任何东西"⇒ 在途防重对它整个失效
# (而本 SQL 的在途口径**有意不分 feed 类型**,见上)。别名一律经
# catalog.sku_aliases(代际继承的唯一出处),只继承一跳;视图在改码前是空集,
# UNION ALL 加空集 ⇒ 结果集逐行不变。
#: 破坏类 feed 的两个类型:**唯一出处在 services.feed_track**
#: (同一份清单也是 receipt_blocked 的取数面,各写一份迟早只改一处)。
_DISPOSAL_FEEDS = feed_track.DESTRUCTIVE_FEED_TYPES
_SQL_INFLIGHT = """
SELECT store, sku, bool_or(disposal) AS disposal FROM (
    SELECT f.store, f.sku,
           (f.feed_type = ANY(%(disposal)s::text[])) AS disposal
    FROM ops.feed_items f
    JOIN catalog.walmart_items w ON w.store = f.store AND w.sku = f.sku
    WHERE (f.status = 'submitted'
           AND f.submitted_at > now() - interval '48 hours')
       OR (f.status = 'success' AND f.resolved_at > w.last_seen_at)
    UNION ALL
    SELECT a.store, a.sku,
           (f.feed_type = ANY(%(disposal)s::text[])) AS disposal
    FROM catalog.sku_aliases a
    JOIN ops.feed_items f ON f.store = a.store AND f.sku = a.alias_sku
    JOIN catalog.walmart_items w ON w.store = a.store AND w.sku = a.sku
    WHERE (f.status = 'submitted'
           AND f.submitted_at > now() - interval '48 hours')
       OR (f.status = 'success' AND f.resolved_at > w.last_seen_at)
) t
GROUP BY store, sku
"""
# 破坏类回执的两道闸(2026-09-09 由原「WFS 件删不掉」那一道泛化而来)。
# 判据与 SQL 都不在本文件:码集的唯一出处是 `registry.resources` 的两个
# frozenset,查询的唯一出处是 `services.feed_track.receipt_blocked`
# (sku_migrate 的死档闸读的是同一份 —— 两份 SQL 一漂,两条链对"这个 SKU 还在
# 不在"就会给出不同答案,而且不报错)。
#   · `WALMART_ERR_ITEM_GONE`(死档)沃尔玛说这个 SKU 已经不在了(删了/退役了/
#     停用了/匹配库里查无)⇒ 破坏动作的目的已达成,不再建议。这些行在**列表
#     接口里仍以 PUBLISHED/UNPUBLISHED 出现**(2026-08-28 起的僵尸列表),本闸
#     让它们不再每天烧 DELETE/RETIRE 配额;目录里死档行本身的根治归 backlog §十三。
#   · `WALMART_ERR_DESTRUCTIVE_PERMANENT`(永久拒)WFS 件不许删、RETIRE 的
#     通用异常 —— 重发必再拒,只能人工。
#     WFS 那一条的历史依据(2026-08-24,多仓批次 0)照旧成立,现在它只是**永久拒
#     集合里的一员**:沃尔玛回执原话 "The item you are trying to delete is WFS
#     eligible. At this time, you can not delete WFS eligible items.",官方也记着
#     DELETE_ITEM 仅 SFF/FBM 支持(docs/legacy_survey.md:1265);生产实见 11 条
#     (L001/A152/A154/A170)连着几轮空烧配额。
#     ⚠ **拦掉不等于改判 retire**:RETIRE_ITEM 对 WFS 件行不行官方没有明文
#     (docs/multi_node_plan.md §2.4 的同款空白),按本仓纪律不许按推断编码 ——
#     这里只跳过并**响亮报数**,把"要不要转出 WFS"交回给人。
# ⚠ 语义:按**最近一次尝试**的回执判,**没有时间窗**。下一次尝试若回执变了
# (人工把件转出 WFS 之后重删成功)它自然就放出来了 —— 不是"历史上出现过就
# 永久拉黑"。
# ⚠ 下面两段判据 + 上面的在途防重,都是按 (store, sku) 读历史的 —— 改码之后
# 新码在 product_events / ops.feed_items 里**一条历史都没有**,会同时失明。
# 一律经 catalog.sku_aliases 沿改码链继承**一跳**(前提:旧码改码后立即弃码、
# 永不再改码;要连改两次得把视图改成递归 CTE,见 schema.sql 的视图注释)。
# 为什么不在这里各写一遍登记簿指针的 JOIN:"这个新码继承那个旧码的历史"是
# **一条判据**,判据只能有一处出生(conventions §六),写几遍就是几份会各自
# 漂移的实现,而漂了不报错、只是某一处从此看不见历史。
# ⚠ 占位符改成命名式:UNION 之后同一个值要用两次,`%s` 位置参数给不了两遍。
# 最近一次归类的**签名**(2026-09-10):有 atoms 的事件取原子码集合(去重、字典序、
# 逗号拼),没有的(存量)退回 detail.category。单原子行的签名与主码相同,换判据
# 不会让整库在第一轮重记一遍;复合行会补记一次 —— 那正是此前缺的账。
# jsonb_typeof 护一层:atoms 不是数组(NULL/缺键/写坏)时不炸,退回主码。
_SQL_LAST_CAT = """
SELECT DISTINCT ON (store, sku) store, sku, cat FROM (
    SELECT e.store, e.sku,
           coalesce((SELECT string_agg(DISTINCT x->>'code', ',' ORDER BY x->>'code')
                     FROM jsonb_array_elements(
                          CASE WHEN jsonb_typeof(e.detail->'atoms') = 'array'
                               THEN e.detail->'atoms' END) x),
                    e.detail->>'category') AS cat,
           e.occurred_at
    FROM catalog.product_events e WHERE e.event = %(ev)s::text
    UNION ALL
    SELECT a.store, a.sku,
           coalesce((SELECT string_agg(DISTINCT x->>'code', ',' ORDER BY x->>'code')
                     FROM jsonb_array_elements(
                          CASE WHEN jsonb_typeof(e.detail->'atoms') = 'array'
                               THEN e.detail->'atoms' END) x),
                    e.detail->>'category'),
           e.occurred_at
    FROM catalog.sku_aliases a
    JOIN catalog.product_events e
      ON e.store = a.store AND e.sku = a.alias_sku
    WHERE e.event = %(ev)s::text
) t
ORDER BY store, sku, occurred_at DESC
"""
_SQL_STUBBORN = """
SELECT DISTINCT ON (store, sku) store, sku, event FROM (
    SELECT e.store, e.sku, e.event, e.occurred_at
    FROM catalog.product_events e
    WHERE e.event IN ('delete_verified', 'delete_not_effective',
                      'item_appeared', 'item_reappeared')
    UNION ALL
    SELECT a.store, a.sku, e.event, e.occurred_at
    FROM catalog.sku_aliases a
    JOIN catalog.product_events e
      ON e.store = a.store AND e.sku = a.alias_sku
    WHERE e.event IN ('delete_verified', 'delete_not_effective',
                      'item_appeared', 'item_reappeared')
) t
ORDER BY store, sku, occurred_at DESC
"""
# 顽固标记绑定当前上架代际(2026-08-07 审查修正):最新事件若是
# item_appeared/item_reappeared,说明商品经历了消失→重上架,旧的
# delete_not_effective 属上一代刊登,不再顽固——按正常归类路径走
# (否则重上架的同 ASIN 首次出问题就被双 feed 直删——顽固加压只该给
# 本代际已实证「删除未生效」的行)。
_SQL_STATUS = """
SELECT DISTINCT ON (store) store, store_status FROM ops.store_kpi_daily
ORDER BY store, data_date DESC
"""

# 审核判拒但还在架(批复 #8 要的第二个来源)。审核链说这个产品不该卖、
# 沃尔玛后台却还挂着 —— 拆分前没有任何工作流盯着这个缺口。
#
# **判据只有一处**:catalog.audit_listing_conflicts 视图(2026-08-14 建,
# 同时是 audit_passed/audit_rejected 事件的第一个消费方)。这里原本抄了一份
# 等价的 JOIN,与视图两份实现迟早漂 —— 口径要改就只改视图那一处。
# ⚠ 视图**不按 published_status 过滤**:审核拒的产品**正常在架**(PUBLISHED)
# 才是最该下架的那批,恰恰不会出现在问题商品清单里。
#
# 顺带取 rejected_after_listing(先上架、后被判拒)只为在摘要里亮个数:
# 它是**审核链漏拦**的线索,与"该不该下架"是两个问题,不影响建议本身。
_SQL_AUDIT_REJECTED = """
SELECT store, sku, asin, audit_reason, rejected_after_listing
FROM catalog.audit_listing_conflicts
WHERE rejected_still_listed
"""


def _load_state():
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_ITEMS)
        items = [dict(zip(("store", "sku", "reasons", "published_status"), r))
                 for r in cur.fetchall()]
        cur.execute(_SQL_INFLIGHT, {"disposal": list(_DISPOSAL_FEEDS)})
        rows_if = cur.fetchall()
        inflight = {(st, sk) for st, sk, _ in rows_if}
        inflight_disposal = {(st, sk) for st, sk, d in rows_if if d}
        cur.execute(_SQL_LAST_CAT, {"ev": product_events.PROBLEM_CATEGORIZED})
        last_cat = {(s, k): c for s, k, c in cur.fetchall()}
        cur.execute(_SQL_STUBBORN)
        stubborn = {(st, k) for st, k, ev in cur.fetchall()
                    if ev == 'delete_not_effective'}
        cur.execute(_SQL_STATUS)
        inactive = {s for s, st in cur.fetchall()
                    if st and st.upper() != "ACTIVE"}
        # 两桶各查一次(同一条 SQL、同一个函数,只是码集不同)——
        # 判据在 registry,查询在 services,本文件一份 SQL 都不留
        gone_blocked = feed_track.receipt_blocked(
            conn, resources.WALMART_ERR_ITEM_GONE)
        perm_blocked = feed_track.receipt_blocked(
            conn, resources.WALMART_ERR_DESTRUCTIVE_PERMANENT)
    return (items, inflight, inflight_disposal, last_cat,
            inactive, stubborn, gone_blocked, perm_blocked)


def plan(items, inflight, inactive, stubborn=frozenset(),
         inflight_disposal=frozenset(), gone_blocked=frozenset(),
         perm_blocked=frozenset()):
    """输入:扫描面全部行与去重状态 → 输出:(计划 dict, 计数 dict)。纯函数,可测。

    计划形如 {店铺: {"delete": [item行], "retire": [item行]}},每行附
    category/cat_name/atoms/cat_sig/recoverable(归类进病历/黑名单/摘要,
    **也决定走向**)。

    **按原子归类处置**(所有者定稿 2026-09-10,取代 2026-08-28「非 PUBLISHED
    一律删除」与 2026-09-06「RETIRED 全豁免」):
      · 无原因(unpublished_reasons 空)→ 不是候选(clean 桶):在售行的常态;
        非 PUBLISHED 而无原因的行也不删 —— 没有原文就没有判据,判不准就判活。
        这一档排在在途/店铺闸**之前**:那两个计数只该数真正的问题行。
      · 原子集合 ⊆ RECOVERABLE_CODES(`error_taxonomy.is_recoverable_only`)
        → 不删(recoverable 桶),归类照记进病历。
      · 其余一律删除,**不看 published_status / lifecycle**:复合原文里哪怕只有
        一个非可恢复原子(「End Date 过期; 禁售政策」)也删;OTHER 未识别的也删
        (所有者:「其他的都删除」),但逐条进摘要告警(_unknown_note)。
    顽固双击(retire+delete 齐发)与死档/永久拒回执闸、在途、非 ACTIVE 店预筛
    不变 —— 那些是操作层防重,不是"该不该删"的判据。
    """
    out: dict[str, dict] = {}
    n = {"inflight": 0, "inflight_listing": 0, "inactive": 0,
         "delete": 0, "stubborn": 0, "gone": 0, "permanent": 0,
         "clean": 0, "recoverable": 0, "unknown": 0}
    for it in items:
        key = (it["store"], it["sku"])
        if not (it.get("reasons") or "").strip():
            n["clean"] += 1             # 无原因 = 无判据,不是候选
            continue
        if it["store"] in inactive:
            n["inactive"] += 1
            continue
        if key in inflight:
            # 分开数:处置在途(我们的删/停还没落定)vs 上架/维护在途
            # (常见 = 新品在 QARTH 合规复审,最长 48h —— 复审期内追发
            # DELETE_ITEM 属于过早,复审通过它会自己恢复)。都跳过,分开报
            if key in inflight_disposal:
                n["inflight"] += 1
            else:
                n["inflight_listing"] += 1
            continue
        # 归类吃新 16 码(services/error_taxonomy,2026-09-03 换轨);入选黑名单
        # 的判据是 `blacklist.PERMANENT`(所有者逐码裁决的七个 + OTHER 两个显式
        # 词条),`unlisted_term` 一并带上(is_permanent 读它)。
        # 2026-09-10 起逐原子明细也带上:atoms 落事件与建议行的 detail,
        # cat_sig(原子码集合签名)是归类事件"变没变"的判据,recoverable 是走向。
        res = error_taxonomy.classify_reasons(
            error_taxonomy.split_reasons(it["reasons"]))
        it["category"], it["cat_name"] = res.code, res.name
        it["unlisted_term"] = res.unlisted_term
        it["policy_name"] = res.policy_name
        it["atoms"] = [{"code": c, "policy_name": pn, "text": t}
                       for c, pn, t in res.atoms]
        it["cat_sig"] = ",".join(sorted({c for c, _ in res.atom_codes}))
        it["unknown"] = list(res.unknown)
        it["recoverable"] = error_taxonomy.is_recoverable_only(res)
        if res.unknown:
            n["unknown"] += 1
        if it["recoverable"]:
            n["recoverable"] += 1       # 只含可恢复原子:不删,等它自己/运营恢复
            continue
        bucket = out.setdefault(it["store"], {"delete": [], "retire": []})
        # 两道回执闸(见上面 receipt_blocked 那段注释)。**死档优先于永久拒**:
        # 一个 SKU 只可能命中其中之一(判据是同一次回执的同一个码,两个码集
        # 不相交,守门用例钉着),这里的先后只是让读的人不用猜。
        # 顽固件(retire+delete 双发)与普通件走同一道闸:delete 注定被拒,
        # 而 RETIRE_ITEM 对这两类行不行官方都没有明文 —— 按本仓纪律不许按推断
        # 编码,整条跳过并**响亮报数**,不静默。
        if key in gone_blocked:
            n["gone"] += 1
            continue
        if key in perm_blocked:
            n["permanent"] += 1
            continue
        if key in stubborn:
            # 删除未生效的顽固 SKU(所有者定稿):
            # 停用+删除双 feed 齐发——能删的删,删不掉的至少停用
            bucket["retire"].append(it)
            bucket["delete"].append(it)
            n["stubborn"] += 1
            continue

        bucket["delete"].append(it)
        n["delete"] += 1
    return out, n


def to_dispositions(plans: dict) -> list[dict]:
    """输入:plan() 的计划 dict → 输出:建议行列表。纯函数,可测。

    一个 (店铺,SKU) 可能同时进 retire 与 delete 桶(顽固双击),那是**两条**
    建议行——它们是两个 feed、两次独立的生效判定,合成一行会让其中一个的
    落定结果覆盖另一个。
    """
    rows = []
    for store, b in sorted(plans.items()):
        for action in ("delete", "retire"):
            for it in b.get(action, []):
                rows.append({
                    "store": store, "sku": it["sku"], "source": "scan",
                    "action": action, "category": it.get("category"),
                    "reason": it.get("reasons") or "",
                    # atoms:逐原子 (码/政策名/原文),2026-09-10 起与事件同款落库
                    "detail": {"cat_name": it.get("cat_name"),
                               "atoms": it.get("atoms") or []},
                })
    return rows


def _summarize(allrows: list[dict], audit_rows: list[dict], n: dict,
               n_items: int) -> list[str]:
    """输入:**最终会落库的**建议行 + 计数 → 输出:总览与分店明细文本。纯函数。

    ⚠ **必须在剔矛盾之后调**(2026-08-14 生产实遇):首版在剔除前就把 plan()
    的原始数打出来了(报"反补 10"而实际只落 8),分店明细同理 —— 人眼闸门看的
    就是这几个数,不该还要自己拿底下那行"剔除 2 条"做减法。

    ⚠ **按建议行统计,不按 plan() 的桶**:`n['delete']` 不含顽固双击那批
    (那支 continue 前没有 `n['delete'] += 1`),照它报会少一大截 —— 本轮实测
    plan 报 195、实际 delete 桶 217。

    ⚠ **还要按 (店铺,SKU,动作) 去重**(2026-08-14 第二次修):同一个 SKU 被
    scan 与 audit 双双建议删除时,allrows 里是两条,但落库被部分唯一索引合成
    一条 —— 不去重就会报 489 而执行件只领到 440,两个摘要对不上账,分店明细里
    还会看到同一个 SKU 出现两次。
    去重口径与 upsert 一致:**后写的赢**(executemany 按序执行,audit 排在
    scan 之后,所以 category 会被 audit 的 None 覆盖 —— 摘要如实显示这一点,
    不美化)。
    """
    merged: dict[tuple, dict] = {}
    for r in allrows:
        merged[(r["store"], r["sku"], r["action"])] = r    # 后写的赢
    allrows = list(merged.values())
    by_act: dict[str, int] = {}
    for r in allrows:
        by_act[r["action"]] = by_act.get(r["action"], 0) + 1
    # 首行(链通知只发这一行):扫描面现在是目录全量,先报三档分流再报建议数
    out = [f"problem_scan:扫描 {n_items} 行(无原因 {n.get('clean', 0)},"
           f"仅可恢复原子不删 {n.get('recoverable', 0)})→ 建议 删除 "
           f"{by_act.get('delete', 0)}"
           f"(其中审核判拒 {sum(1 for r in allrows if r.get('source') == 'audit')}),"
           f"顽固停用 {by_act.get('retire', 0)};"
           f"已死档跳过 {n['gone']},"
           f"永久拒跳过 {n['permanent']},"
           f"处置在途/待观测跳过 {n['inflight']},"
           f"上架/维护在途跳过 {n['inflight_listing']}"
           f"(多为新品合规复审,复审完自动进扫描),"
           f"非 ACTIVE 店跳过 {n['inactive']}"]
    per_store: dict[str, dict] = {}
    for r in allrows:
        b = per_store.setdefault(r["store"], {"delete": [], "retire": []})
        b[r["action"]].append(r)
    for store, b in sorted(per_store.items()):
        cats: dict[str, int] = {}
        for r in b["delete"]:
            k = r.get("category") or "-"
            cats[k] = cats.get(k, 0) + 1
        line = (f"  {store}:删除 {len(b['delete'])}"
                + (f",顽固停用 {len(b['retire'])}" if b["retire"] else "")
                + ",类别={" + ",".join(f"{c}:{v}" for c, v in sorted(cats.items()))
                + "}")
        if b["delete"]:
            line += f",删除样本={[(r['sku'], r.get('category')) for r in b['delete'][:5]]}"
        out.append(line)
    return out


def _blocked_notes(gone_skipped: list, perm_skipped: list) -> list[str]:
    """输入:本轮被两道回执闸跳过的 (店,SKU) → 输出:摘要说明行(各带 5 个样本)。

    **不静默**:这两道闸挡掉的是成百上千条本来会天天重发的破坏建议,不报的话
    摘要看起来就是"今天问题商品少了"——本仓口诀:静默的闸没人记得它关着。
    """
    out = []
    if gone_skipped:
        out.append(
            f"  已死档跳过 {len(gone_skipped)}(沃尔玛回执说这个 SKU 已经不在了"
            f"——删了/退役了/停用了/匹配库里查无,破坏动作的目的已达成,不再建议;"
            f"它们仍在列表接口里以 PUBLISHED/UNPUBLISHED 出现,那是僵尸列表,"
            f"目录里的死档行根治归 docs/backlog.md §十三):"
            f"{sorted(gone_skipped)[:5]}")
    if perm_skipped:
        out.append(
            f"  永久拒跳过 {len(perm_skipped)}(WFS 件不许删 / RETIRE 通用异常:"
            f"重发必再拒,只能人工去 Seller Center 处理,例如把件"
            f"转出 WFS;转出后下一次尝试的回执会自动把它放出来):"
            f"{sorted(perm_skipped)[:5]}")
    return out


def _record_categories(conn, items: list[dict], last_cat: dict) -> int:
    """归类事件:仅 (店铺,SKU) **原子码集合**变化时落账(病历不灌水)。

    判据 2026-09-10 从主码换成原子码集合(`cat_sig`,_SQL_LAST_CAT 同一口径):
    主码没变、原子多了一个(「禁售」→「End Date 过期; 禁售」)也是一次变化,
    此前这种变化不留任何痕迹(所有者:「次要原子要落库」)。
    """
    fresh = [it for it in items if "category" in it
             and last_cat.get((it["store"], it["sku"])) != it["cat_sig"]]
    product_events.record_many(conn, [
        {"sku": it["sku"], "store": it["store"],
         "event": product_events.PROBLEM_CATEGORIZED,
         "source": "problem_scan",
         # ⚠ 全文,别截(2026-09-04):这本账是**产品历史**,而所有者定的判据是
         #   「看产品历史,够格拉黑的那条最高优先级」—— 截到 200 字符正好把
         #   沃尔玛写在**句尾**的判据串砍掉(「…To republish this item please
         #   make sure you have the appropriate product type selected.」),
         #   于是 PT_WRONG 被判成 POLICY、可修复的品被永久拉黑。
         #   截断属于展示层,不属于账本(同 services/blacklist 头注的考古结论)。
         # atoms / recoverable(2026-09-10):逐原子 (码/政策名/原文) 与走向,
         #   读侧不必再拆原文重判;_SQL_LAST_CAT 拿 atoms 的码集合当签名。
         "detail": {"category": it["category"], "name": it["cat_name"],
                    "reason": it["reasons"] or None,
                    "atoms": it["atoms"],
                    "recoverable": it["recoverable"]}}
        for it in fresh])
    return len(fresh)


def _collect_blacklists(conn, items: list[dict]) -> str:
    """输入:当轮已归类 item → 输出:黑名单收集摘要(一行)。

    归因收集尾段(plan.md「品牌限制/侵权类问题产品 → 品牌黑名单」的落地):
    当轮**够格永久拉黑的**(`error_taxonomy.is_permanent`:七个永久码 +
    `OTHER` 的两个显式词条)入 ASIN 黑名单,BRAND/IP 的品牌从
    catalog.products.brand 取、
    按品牌去重入 brand_blacklist。
    **任何失败只告警不阻断**——黑名单是扫描的副产品,收集炸了不该把建议
    产出拖下水;漏一轮下一轮照样补(入选条件不变)。
    ⚠ **本函数只写 PG**;飞书投影归 `run()` 收尾那一步(`_push_sheets`)——
    它必须在事务提交**之后**才看得见这一轮的行,详见那里的注释。
    """
    cand = [it for it in items if "category" in it]
    if not cand:
        return ""
    try:
        asin_new = blacklist.record_asins(conn, cand)
        st = blacklist.collect_brands(conn, cand)
    except Exception as e:                              # noqa: BLE001
        logger.error("黑名单收集失败(建议产出不受影响,下轮重收): %s", e)
        return f"黑名单收集失败:{e}"
    bits = [f"ASIN 黑名单 +{asin_new}", f"品牌 +{st['brand_new']}"]
    if st["brand_known"]:
        bits.append(f"品牌已知 {st['brand_known']}")
    if st["no_brand"]:
        # 不是错误:产品中心还没这些 ASIN 的品牌,标已处理才是错(永远漏)
        bits.append(f"待品牌 {st['no_brand']}(产品中心缺 brand,下轮重试)")
    if st["skipped"]:
        bits.append(f"已处理跳过 {st['skipped']}")
    return "黑名单收集:" + ",".join(bits)


_K_CLUSTER_WARN = 20    # 同店「内部标记」超过这个数就该当店铺风险信号看


def _k_cluster_note(items: list[dict]) -> str:
    """输入:已归类 item → 输出:K 桶(内部标记)按店聚集的告警行(无则空串)。

    「flagged by our internal team」沃尔玛不给理由,单条没有信息量;它的
    价值在**聚集度**:同一家店几十条 = 店铺被盯上的风险信号(2026-08-24
    漏判盘点实见:谭总11 一店 45 条)。ASIN 黑名单收集早就包含 K 类
    (_collect_blacklists),这里只补"按店看"这一眼。
    """
    by_store: dict[str, int] = {}
    for it in items:
        if it.get("category") == "FLAGGED":      # 换轨前是旧码 K(审查)
            by_store[it["store"]] = by_store.get(it["store"], 0) + 1
    hot = {st: n for st, n in by_store.items() if n >= _K_CLUSTER_WARN}
    if not hot:
        return ""
    return ("  ⚠ 「内部标记」按店聚集(≥%d 条,店铺风险信号,建议人工查该店):"
            % _K_CLUSTER_WARN
            + ",".join(f"{st}×{n}" for st, n in
                        sorted(hot.items(), key=lambda kv: -kv[1])))



def _recoverable_note(items: list[dict]) -> str:
    """输入:已归类 item → 输出:「仅可恢复原子不删」按店计数行(无则空串)。

    这批行以前一律删,现在留着 —— 人眼闸门要看得见每店留了多少、留的是什么
    (EXPIRED 还是 STAGE),否则"删除数骤降"读不出原因。
    """
    by_store: dict[str, dict[str, int]] = {}
    for it in items:
        if not it.get("recoverable"):
            continue
        d = by_store.setdefault(it["store"], {})
        d[it["cat_sig"]] = d.get(it["cat_sig"], 0) + 1
    if not by_store:
        return ""
    return ("  仅可恢复原子不删(按店):"
            + ",".join(f"{st}×{sum(d.values())}"
                        + "{" + ",".join(f"{k}:{v}" for k, v in sorted(d.items())) + "}"
                        for st, d in sorted(by_store.items(),
                                            key=lambda kv: -sum(kv[1].values()))))


def _unknown_note(items: list[dict]) -> str:
    """输入:已归类 item → 输出:未识别原子告警行(无则空串)。

    `classify_reasons` 的契约是"unknown 引擎不吞,调用方必须告警"—— 本工作流
    此前没接。2026-09-10 起未识别原子的行照删(所有者:「其他的都删除」),
    所以更要喊:沃尔玛换一种措辞说"等一等"(比如新的审查中文案),这里是唯一
    能看见的地方;看见了就去 error_taxonomy 加规则或加进 RECOVERABLE_CODES。
    """
    seen: dict[str, int] = {}
    for it in items:
        for atom in it.get("unknown") or ():
            seen[atom] = seen.get(atom, 0) + 1
    if not seen:
        return ""
    top = sorted(seen.items(), key=lambda kv: -kv[1])[:5]
    return (f"  ⚠ 未识别原子 {sum(seen.values())} 条(这些行按「其他」删除;"
            f"措辞样本:"
            + " | ".join(f"{a[:90]}…×{c}" if len(a) > 90 else f"{a}×{c}"
                         for a, c in top) + ")")


# 政策名提取(2026-08-24,审核反哺):沃尔玛下架原因里带政策名的两种写法
#   "||Children's Products Prohibited Products Policy@@@…"
#   "Prohibited Product Policy: Hazardous Items" / "… Policy on Made in USA claims"
_POLICY_NAME_PATTERNS = (
    re.compile(r"\|\|\s*([^@|]{3,60}?)\s+prohibited products? policy", re.I),
    re.compile(r"prohibited products? policy(?:\s*:\s*|\s+on\s+)([^.@|]{3,60})",
               re.I),
)
_SQL_POLICY_NAMES = "SELECT category_en FROM audit.walmart_prohibited_policy"


def _policy_gap_note(conn, items: list[dict]) -> str:
    """输入:连接 + 当轮 item → 输出:政策表未收录的政策名告警行(无则空串)。

    审核反哺的探针:沃尔玛点名了政策(如 Made in USA claims),而
    `audit.walmart_prohibited_policy` 里没有对应行 ⇒ L3 的 S4 政策块看不见它,
    语义匹配注定漏。政策表没有同步器(audit_import 一次性),缺口只能靠
    这里天天报,人工 audit_import 补录。任何失败只告警不阻断(与黑名单收集
    同款纪律)。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL_POLICY_NAMES)
            known = {str(r[0]).strip().lower() for r in cur.fetchall()}
    except Exception as e:                              # noqa: BLE001
        logger.warning("政策表读不到,政策名缺口本轮不报:%s", e)
        return ""
    missing: dict[str, int] = {}
    for it in items:
        text = it.get("reasons") or ""
        for pat in _POLICY_NAME_PATTERNS:
            for m in pat.finditer(text):
                name = " ".join(m.group(1).split()).strip(" *")
                low = name.lower()
                if not name or any(low in k or k in low for k in known):
                    continue
                missing[name] = missing.get(name, 0) + 1
    if not missing:
        return ""
    top = sorted(missing.items(), key=lambda kv: -kv[1])[:8]
    return ("  ⚠ 沃尔玛点名、政策表未收录的政策(L3 看不见它们,"
            "用 audit_import 补录):"
            + ",".join(f"{n}×{c}" for n, c in top))


def _push_sheets() -> str:
    """输入:无 → 输出:飞书投影摘要一行。**必须在 `with db.pg_conn()` 之外调**。

    所有者定稿 2026-08-17:「让 problem_scan 完成后立马推飞书」。做法与
    `order_center` 那次拆分同款(`docs/schedule_plan.md` §四:「已对接飞书表的,
    执行完就写,不要做成单独的」)—— 投影代码在 `services.blacklist_sheet`,
    不是 import `blacklist_push` 工作流(铁律 1)。

    ⚠ **位置有讲究:必须等本轮的写提交之后。** 投影是另开一条连接查全表
    (`SELECT … FROM catalog.asin_blacklist`),放在扫描那个事务里面调的话它
    **看不到刚写的那几行** —— 表现是"黑名单收集 +5,可表格一行没多",
    而且不报任何错。所以它长在这儿、由 `run()` 在 `with` 块退出后调用。

    ⚠ 两条时间线本来就不一样,这次只是把第二条提前了:
      · **否决闸**在 `_collect_blacklists` 写完那一刻就生效 —— 上架与审核读 PG,
        从不读飞书表。这条**从来不等投影**。
      · **表格**原先要等 15:00 的 `blacklist` 任务;现在这一轮顺手就写完。
        `blacklist_push` 仍留在调度里当兜底(整表重写幂等),也仍是手动补推入口。

    失败只告警不阻断(`push_after` 里那条纪律):黑名单已落 PG、闸门已生效,
    飞书写挂不该把一轮扫描记成 failed —— 但必须出现在摘要里。
    """
    return blacklist_sheet.push_after()


def _audit_rejected_rows(conn, inflight: set, inactive: set,
                         only: str | None,
                         gone_blocked: set = frozenset(),
                         perm_blocked: set = frozenset()) -> list[dict]:
    """输入:连接 + 去重状态 → 输出:判拒仍在架的建议行。

    与 scan 来源共用同一套闸(非 ACTIVE 店跳过、在途不建议),但**不走归类**
    ——审核已经给出结论了,这里不需要再猜沃尔玛为什么不高兴。

    ⚠ **本函数不再截单店上限**(2026-08-24 归一):限额表「下架限制」由执行件
    problem_product_cleanup 在领取时施加一次。此前维护链扫描件也按同一张表
    截一次,两处相加 ⇒ 每店实际可删 2N。
    视图按 store 有序与否不保证,所以**仍按 (店铺, SKU) 定序**——执行期截断
    按这个顺序取件,不定序的话每轮留下的是随机的一批,说不清削到哪儿了。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_AUDIT_REJECTED)
        rows = cur.fetchall()
    out: list[dict] = []
    for store, sku, asin, reason, after_listing in sorted(
            rows, key=lambda r: (str(r[0]), str(r[1]))):
        if only and store != only:
            continue
        if store in inactive or (store, sku) in inflight:
            continue
        if (store, sku) in gone_blocked or (store, sku) in perm_blocked:
            # 审核说该删,但沃尔玛最近一次回执说"这个 SKU 已经不在了"(死档)
            # 或"不许删"(WFS / PDI_0004)—— 与 scan 来源同两道闸。
            # 这里不单独计数:摘要那两行报的是总数,分不分来源无碍于"要人去
            # Seller Center 处理"这个唯一动作
            continue
        out.append({
            "store": store, "sku": sku, "asin": asin, "source": "audit",
            "action": "delete", "category": None,
            "reason": f"审核判拒仍在架:{reason or '(理由未留存)'}",
            "detail": {"audit_reason": reason,
                       "rejected_after_listing": bool(after_listing)},
        })
    return out


def run(params: dict) -> str:
    """输入:params(store/preview)→ 输出:归类统计 + 建议行落账摘要。"""
    # --dry-run 与 -p preview=1 等价:本工作流 DANGEROUS=False(不发 feed),
    # 但它**会写建议表与事件** —— 人敲 --dry-run 的本意就是"这轮别落库"
    preview = (str(params.get("preview", "")).strip() == "1"
               or bool(params.get("dry_run")))
    only = params.get("store")
    (items, inflight, inflight_disposal, last_cat,
     inactive, stubborn, gone_blocked, perm_blocked) = _load_state()
    if only:
        items = [i for i in items if i["store"] == only]
    # 缺席避让(店级重试标准③,所有者定稿 2026-08-26):缺席店的在架状态
    # 停在上一轮,拿它判「仍在架 → 删」会对可能已变的现实开破坏 feed。
    # 判据从库里水位派生(services/store_absence),与调度顺序无关。
    # 探测失败按"不避让"处理并在首行喊出来(preview 是纯 PG 查询,
    # 不该被一次飞书抖动整个拦下)。
    with db.pg_conn() as conn:
        # 降级与 only 范围收敛都在 store_absence.stale_or_note 里(四处同形,
        # 2026-08-27 收口);拼进首行的分号由调用方补
        absent, absence_note = store_absence.stale_or_note(conn, only)
    absence_gap = f";{absence_note}" if absence_note else ""
    # ⚠ 避让只挡**处置建议**(plan/audit_rows —— 会变成删除/停用 feed 的那些);
    # 观察面不连坐:黑名单收集、K 类聚集信号、归类事件都是只增不减的记录,
    # 静音一天会让 15:00 blacklist 链少一天的 ASIN/品牌(对抗校验 2026-08-26)
    items_all = items
    n_avoided = sum(1 for i in items if i["store"] in absent)
    if absent:
        items = [i for i in items if i["store"] not in absent]

    plans, n = plan(items, inflight, inactive, stubborn,
                    inflight_disposal, gone_blocked, perm_blocked)
    rows = to_dispositions(plans)
    # 被两道回执闸挡下的**本轮候选**(不是库里全部命中码的行):摘要要点名,
    # 而点名的对象必须是"今天本来会被建议删的那些",否则数字与总览那行对不上。
    # ⚠ 排除顺序必须与 `plan()` 里那几支 continue **逐条对齐**(非 ACTIVE 店 →
    # 在途 → 死档 → 永久拒):对不齐的话总览那行报 n['gone'],这里报另一个数,
    # 而两个数都"看起来对" —— 本仓 2026-08-14 摘要对不上账的老坑同款。
    # 2026-09-10 起 plan() 在店铺闸/在途之后还有归类与可恢复两档,到得了回执闸的
    # 行 = 归了类(有 category ⇔ 过了非 ACTIVE 店与在途两关)且不是仅可恢复原子。
    # 直接读 plan() 留在行上的标记,不在这里再抄一遍它的分流顺序。
    keys = {(i["store"], i["sku"]) for i in items
            if "category" in i and not i.get("recoverable")}
    gone_skipped = sorted(keys & set(gone_blocked))
    perm_skipped = sorted((keys & set(perm_blocked)) - set(gone_blocked))
    lines: list[str] = []

    with db.pg_conn() as conn:
        audit_rows = _audit_rejected_rows(conn, inflight, inactive, only,
                                          gone_blocked, perm_blocked)
        if absent:
            n_audit_avoided = sum(1 for r in audit_rows
                                  if r["store"] in absent)
            n_avoided += n_audit_avoided
            audit_rows = [r for r in audit_rows if r["store"] not in absent]
        if audit_rows:
            late = sum(1 for r in audit_rows
                       if r["detail"].get("rejected_after_listing"))
            lines.append(
                f"审核判拒但仍在架 {len(audit_rows)} 个 SKU → 建议删除"
                f"(样本={[r['sku'] for r in audit_rows[:5]]})")
            if late:
                lines.append(
                    f"  其中 {late} 个是**先上架后被判拒** —— 上架时那道闸没拦住"
                    f"(或当时还没审)。这是审核链的漏拦线索,值得单看,"
                    f"与本轮该不该删是两个问题")
        allrows = rows + audit_rows
        # (2026-08-28 反补退役后,scan 与 audit 对同一 SKU 只可能都建议删除,
        # 由部分唯一索引合并,不再存在「救活 vs 删除」的矛盾剔除段)
        # 摘要按建议行统计,不是按 plan() 的桶 —— n['delete'] 不含顽固双击
        # 那批(那支 continue 前没有 n['delete'] += 1),照它报会少一截。
        head = _summarize(allrows, audit_rows, n, len(items))
        if absent:
            # ⚠ 缺席避让要进**首行**:链通知只发成功步骤的第一行
            head[0] += (f";⚠ 缺席避让 {len(absent)} 店:"
                        f"{','.join(sorted(absent))}"
                        f"({n_avoided} 条候选不参与本轮处置)")
        head[0] += absence_gap
        lines[:0] = head        # 总览 + 分店明细排在最前,审核/剔除说明跟其后
        # 两道回执闸的点名紧跟总览:它们是"今天为什么少了这么多建议"的答案
        lines[len(head):len(head)] = _blocked_notes(gone_skipped, perm_skipped)
        # 观察面用 items_all(缺席不连坐,见上)
        for note in (_recoverable_note(items_all), _unknown_note(items_all),
                     _k_cluster_note(items_all), _policy_gap_note(conn, items_all)):
            if note:
                lines.append(note)
        if preview:
            lines.append(f"(preview:未落建议行;本轮将写 {len(allrows)} 条"
                         f"——实际落库可能更少:同 (店铺,SKU,动作) 被两个来源"
                         f"命中时按唯一索引合并)")
            return "\n".join(lines)
        n_cat = _record_categories(conn, items_all, last_cat)
        bl_note = _collect_blacklists(conn, items_all)
        n_sug = dispositions.suggest_many(conn, allrows)
        # 撤销本轮不再建议的陈旧行(按来源各撤各的):否则昨天建议删、今天
        # 已恢复正常的 SKU,那条 suggested 还挂着,执行件照样会删
        n_wd = 0
        for src, srows in (("scan", rows), ("audit", audit_rows)):
            # ⚠ store=only 不能省:`-p store=X` 那一轮只扫了一个店,keep 里
            # 只有该店的行,不限范围会把其余全部店铺的待执行建议一次清空
            # exclude_stores=缺席店:它们的行不在 keep 里(本轮避让了),
            # 不排除会被撤成"不再建议"——缺席 ≠ 恢复正常
            n_wd += dispositions.withdraw_stale(
                conn, src, [(r["store"], r["sku"], r["action"]) for r in srows],
                why=f"本轮扫描不再建议{f'(限 {only})' if only else ''}",
                store=only or None, exclude_stores=sorted(absent))
        # 限本链来源:维护链共用同一张建议表,不限的话摘要报的数会把它的
        # 待执行也算进来,与本链执行件领到的数对不上
        n_open = dispositions.count_open(
            conn, sources=dispositions.PROBLEM_SOURCES)
        # ⚠ 本链**没有** expire_executing 那道兜底(删除/停用靠观测判定,
        # 粗暴时限会抢先判掉真正在途的删除)。所以卡住就是一直卡着,
        # 至少要让人看见 —— 否则每轮照常报"建议 N 条",看不出少了谁。
        stuck = dispositions.stuck_executing(
            conn, sources=dispositions.PROBLEM_SOURCES)
    lines.append(f"建议行落账:本轮写入 {n_sug} 次 → **库里待执行 {n_open} 条**"
                 + (f"(差额 {n_sug - n_open} 是同一 (店铺,SKU,动作) 被两个来源"
                    f"命中、按唯一索引合并的)" if n_sug > n_open else "")
                 + (f";撤销陈旧建议 {n_wd} 条(本轮不再建议)" if n_wd else "")
                 + f";归类事件新记 {n_cat} 条")
    if stuck:
        lines.append(dispositions.stuck_note(stuck))
    if bl_note:
        lines.append(bl_note)
        lines.append(_push_sheets())
    lines.append("执行走 `python cli.py problem_product_cleanup`(先 --dry-run 看破坏面)"
                 "(本工作流不发任何 feed)")
    return "\n".join(lines)
