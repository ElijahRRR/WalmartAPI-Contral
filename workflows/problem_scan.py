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
  scan   catalog.walmart_items 里 publishedStatus 非 PUBLISHED 且未缺席的行
         —— **一律建议删除**(所有者定稿 2026-08-28:「不再修改 End Date 救
         商品」,反补机制整体退役);归类(services/problem_products)保留,
         但只进病历/黑名单/摘要,不再决定走向
  audit  审核链判 reject 但**还在架**的产品 —— 审核说不该卖、沃尔玛后台还挂着,
         这个缺口原来没有任何工作流盯着(批复 #8 要求补上)

⚠ **只建议,不动状态、不发 feed。** 本工作流写库的只有两处:产品事件(归类)
与 ops.dispositions 建议行,都是可重跑的幂等写。

去重口径(2026-08-28 反补退役后剩两条,注释记的是生产事故的教训):
  ① 在途/待观测:feed_items 有 submitted 未落定(滚动 48h 封顶),或已落定
     success 但 catalog_sync 尚未重新观测 → 不建议
  ② 归类事件:同 (店铺,SKU) 类别未变不重复记
注:①在这里是**预筛**,不是最终闸门——真正的在途防重在 api/feeds.submit_feed
的 ops.feed_log 里(提交时判,返回 outcome=dedup)。预筛只是省得把注定被拦下的
行也建成建议。

店铺闸:ops.store_kpi_daily 最新 store_status 非 ACTIVE 的店整体跳过。

调度顺序:catalog_sync → problem_scan → problem_product_cleanup(真跑)。
"""

import logging
import re

from registry import db
from services import blacklist, blacklist_sheet, dispositions
from services import error_taxonomy
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

# 扫描面 = **一切非 PUBLISHED**(所有者定稿 2026-08-28:「publishedStatus
# 不是 PUBLISHED 的,都进行删除,不再修改 End Date 救商品」)。三个边界:
#   · 范围从 UNPUBLISHED/SYSTEM_PROBLEM 扩到含 STAGE/READY_TO_PUBLISH/
#     IN_PROGRESS:刚上架的行在发布管道里有几小时~48h 的过渡态,靠既有的
#     在途预筛护住(上架 feed 在 feed_items 挂 submitted/待观测即跳过,
#     QARTH 复审同一机制,见下)——过了 48h 还卡在过渡态的就是真卡死,照删。
#   · published_status IS NULL(状态没采到)**不进扫描**:删除不可逆,
#     判不准就判活,不拿未知赌。
#   · Stage 不再按行豁免(旧 is_stage_pending 已退役):所有者定稿——
#     『stage status until you go live』一般只在店铺非 ACTIVE 时出现(那时
#     全店皆然),而店铺闸(_SQL_STATUS 非 ACTIVE 整店跳过)已经挡住那种店;
#     ACTIVE 店里的 Stage 行 = 翻出来的老档(2026-08-28 事件实证),照删。
_SQL_ITEMS = """
SELECT store, sku, unpublished_reasons
FROM catalog.walmart_items
WHERE published_status IS NOT NULL
  AND published_status <> 'PUBLISHED'
  AND missing_since IS NULL
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
_DISPOSAL_FEEDS = ("DELETE_ITEM", "RETIRE_ITEM")
_SQL_INFLIGHT = """
SELECT f.store, f.sku,
       bool_or(f.feed_type = ANY(%(disposal)s::text[])) AS disposal
FROM ops.feed_items f
JOIN catalog.walmart_items w ON w.store = f.store AND w.sku = f.sku
WHERE (f.status = 'submitted'
       AND f.submitted_at > now() - interval '48 hours')
   OR (f.status = 'success' AND f.resolved_at > w.last_seen_at)
GROUP BY f.store, f.sku
"""
# WFS 件删不掉(2026-08-24,多仓批次 0)。沃尔玛回执原话:
# "The item you are trying to delete is WFS eligible. At this time, you can not
#  delete WFS eligible items." —— 官方也记着 DELETE_ITEM 仅 SFF/FBM 支持
# (docs/legacy_survey.md:1265)。这类件**每天重建议、每天重发、每天同一个错**,
# 生产实见 11 条(L001/A152/A154/A170)连着几轮空烧配额。
# 口径:取该 (店铺,SKU) **最近一次**删除回执,是这个错误码才拦 —— 不是"历史上
# 出现过就永久拉黑":商品转出 WFS 之后就该能删了,下一次删除尝试的回执会把它
# 放出来。没有时间窗(WFS 状态不会自己变,靠人在 Seller Center 转出)。
# ⚠ **拦掉不等于改判 retire**:RETIRE_ITEM 对 WFS 件行不行官方没有明文
# (docs/multi_node_plan.md §2.4 的同款空白),按本仓纪律不许按推断编码 ——
# 这里只跳过并**响亮报数**,把"要不要转出 WFS"交回给人。
_WFS_BLOCKED_CODE = "ERR_EXT_DATA_0101218"
_SQL_WFS_BLOCKED = """
SELECT store, sku FROM (
    SELECT DISTINCT ON (store, sku) store, sku, error_code
    FROM ops.feed_items
    WHERE feed_type = 'DELETE_ITEM' AND status IN ('failed', 'missing')
    ORDER BY store, sku, submitted_at DESC
) t WHERE error_code = %s
"""
_SQL_LAST_CAT = """
SELECT DISTINCT ON (store, sku) store, sku, detail->>'category'
FROM catalog.product_events WHERE event = %s
ORDER BY store, sku, occurred_at DESC
"""
_SQL_STUBBORN = """
SELECT DISTINCT ON (store, sku) store, sku, event
FROM catalog.product_events
WHERE event IN ('delete_verified', 'delete_not_effective',
                'item_appeared', 'item_reappeared')
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
        items = [dict(zip(("store", "sku", "reasons"), r))
                 for r in cur.fetchall()]
        cur.execute(_SQL_INFLIGHT, {"disposal": list(_DISPOSAL_FEEDS)})
        rows_if = cur.fetchall()
        inflight = {(st, sk) for st, sk, _ in rows_if}
        inflight_disposal = {(st, sk) for st, sk, d in rows_if if d}
        cur.execute(_SQL_LAST_CAT, (product_events.PROBLEM_CATEGORIZED,))
        last_cat = {(s, k): c for s, k, c in cur.fetchall()}
        cur.execute(_SQL_STUBBORN)
        stubborn = {(st, k) for st, k, ev in cur.fetchall()
                    if ev == 'delete_not_effective'}
        cur.execute(_SQL_STATUS)
        inactive = {s for s, st in cur.fetchall()
                    if st and st.upper() != "ACTIVE"}
        cur.execute(_SQL_WFS_BLOCKED, (_WFS_BLOCKED_CODE,))
        wfs_blocked = {(st, k) for st, k in cur.fetchall()}
    return (items, inflight, inflight_disposal, last_cat,
            inactive, stubborn, wfs_blocked)


def plan(items, inflight, inactive, stubborn=frozenset(),
         inflight_disposal=frozenset(), wfs_blocked=frozenset()):
    """输入:问题商品与去重状态 → 输出:(计划 dict, 计数 dict)。纯函数,可测。

    计划形如 {店铺: {"delete": [item行], "retire": [item行]}},每行附
    category/cat_name(归类只进病历/黑名单/摘要,不再决定走向)。

    **一律删除**(所有者定稿 2026-08-28:「publishedStatus 不是 PUBLISHED 的,
    都进行删除,不再修改 End Date 救商品」)。此前的 A/L 类反补通道、反补计数
    30 天窗、Stage 按行豁免全部退役 —— A 类的语病见 problem_products 头注:
    「end date has passed」本身就是退市标记,反补它 = 对退市档案走官方复活
    通道。顽固双击(retire+delete 齐发)保留:那是对「删除未生效」的加压,
    方向与本定稿一致。
    """
    out: dict[str, dict] = {}
    n = {"inflight": 0, "inflight_listing": 0, "inactive": 0,
         "delete": 0, "stubborn": 0, "wfs": 0}
    for it in items:
        key = (it["store"], it["sku"])
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
        # 2026-09-03 换轨:归类改吃新 16 码(services/error_taxonomy),
        # 不再是 problem_products 的 A-L 单字母码。入选黑名单的判据随之变成
        # `blacklist.PERMANENT`(所有者逐码裁决的七个 + OTHER 两个显式词条)。
        # `unlisted_term` 一并带上:`OTHER` 是混装桶,只有 business decision /
        # trust & safety 算永久拉黑,判据在引擎里(is_permanent)。
        res = error_taxonomy.classify_reasons(
            error_taxonomy.split_reasons(it["reasons"]))
        it["category"], it["cat_name"] = res.code, res.name
        it["unlisted_term"] = res.unlisted_term
        it["policy_name"] = res.policy_name
        bucket = out.setdefault(it["store"], {"delete": [], "retire": []})
        if key in stubborn:
            # 删除未生效的顽固 SKU(所有者定稿):
            # 停用+删除双 feed 齐发——能删的删,删不掉的至少停用
            if key in wfs_blocked:
                n["wfs"] += 1       # 顽固件里的 WFS 件同样删不掉,见下
                continue
            bucket["retire"].append(it)
            bucket["delete"].append(it)
            n["stubborn"] += 1
            continue
        if key in wfs_blocked:
            # WFS 件:上一次删除回执明说删不掉(见 _SQL_WFS_BLOCKED)。
            # 反补通道 2026-08-28 已随「一律删除」定稿退役,本函数产出的全部
            # 是破坏动作 —— WFS 件一条都发不出去,跳过并报数,把"要不要转出
            # WFS"交回给人。
            n["wfs"] += 1
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
                    "detail": {"cat_name": it.get("cat_name")},
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
    out = [f"problem_scan:非 PUBLISHED 商品 {n_items} 行 → 建议 删除 "
           f"{by_act.get('delete', 0)}"
           f"(其中审核判拒 {sum(1 for r in allrows if r.get('source') == 'audit')}),"
           f"顽固停用 {by_act.get('retire', 0)};"
           f"WFS 删不掉跳过 {n['wfs']},"
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


def _record_categories(conn, items: list[dict], last_cat: dict) -> int:
    """归类事件:仅 (店铺,SKU) 类别变化时落账(病历不灌水)。"""
    fresh = [it for it in items if "category" in it
             and last_cat.get((it["store"], it["sku"])) != it["category"]]
    product_events.record_many(conn, [
        {"sku": it["sku"], "store": it["store"],
         "event": product_events.PROBLEM_CATEGORIZED,
         "source": "problem_scan",
         "detail": {"category": it["category"], "name": it["cat_name"],
                    "reason": (it["reasons"] or "")[:200]}}
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
                         wfs_blocked: set = frozenset()) -> list[dict]:
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
        if (store, sku) in wfs_blocked:
            # 审核说该删,但 WFS 件照样删不掉(与 scan 来源同一道闸)。
            # 这里不单独计数:摘要那行报的是总数,分不分来源无碍于"要人去
            # Seller Center 转出 WFS"这个唯一动作
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
     inactive, stubborn, wfs_blocked) = _load_state()
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
                    inflight_disposal, wfs_blocked)
    rows = to_dispositions(plans)
    lines: list[str] = []

    with db.pg_conn() as conn:
        audit_rows = _audit_rejected_rows(conn, inflight, inactive, only,
                                          wfs_blocked)
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
        # 观察面用 items_all(缺席不连坐,见上)
        for note in (_k_cluster_note(items_all), _policy_gap_note(conn, items_all)):
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
