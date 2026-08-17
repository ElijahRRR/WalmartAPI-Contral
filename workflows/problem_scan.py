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
  scan   catalog.walmart_items 里 UNPUBLISHED/SYSTEM_PROBLEM 且未缺席的行
         —— 归类规则逐字沿用 services/problem_products,一个字没改
  audit  审核链判 reject 但**还在架**的产品 —— 审核说不该卖、沃尔玛后台还挂着,
         这个缺口原来没有任何工作流盯着(批复 #8 要求补上)

⚠ **只建议,不动状态、不发 feed。** 本工作流写库的只有两处:产品事件(归类)
与 ops.dispositions 建议行,都是可重跑的幂等写。

去重口径全部沿用原实现(逐字迁移,注释一并搬来——它们记的是生产事故的教训):
  ① 在途/待观测:feed_items 有 submitted 未落定(滚动 48h 封顶),或已落定
     success 但 catalog_sync 尚未重新观测 → 不建议
  ② 反补计数:product_events 的 maintenance_submitted 30 天窗口计数
  ③ 归类事件:同 (店铺,SKU) 类别未变不重复记
注:①在这里是**预筛**,不是最终闸门——真正的在途防重在 api/feeds.submit_feed
的 ops.feed_log 里(提交时判,返回 outcome=dedup)。预筛只是省得把注定被拦下的
行也建成建议。

店铺闸:ops.store_kpi_daily 最新 store_status 非 ACTIVE 的店整体跳过。

调度顺序:catalog_sync → problem_scan → problem_product_cleanup(真跑)。
"""

import logging

from registry import db
from services import blacklist, dispositions
from services import problem_products as pp
from services import product_events

DANGEROUS = False       # 只读沃尔玛;写库仅限事件与建议行,都可重跑

logger = logging.getLogger("workflows.problem_scan")

_SQL_ITEMS = """
SELECT store, sku, gtin, upc, unpublished_reasons
FROM catalog.walmart_items
WHERE published_status IN ('UNPUBLISHED', 'SYSTEM_PROBLEM')
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
_SQL_INFLIGHT = """
SELECT DISTINCT f.store, f.sku
FROM ops.feed_items f
JOIN catalog.walmart_items w ON w.store = f.store AND w.sku = f.sku
WHERE (f.status = 'submitted'
       AND f.submitted_at > now() - interval '48 hours')
   OR (f.status = 'success' AND f.resolved_at > w.last_seen_at)
"""
# 事件名一律绑参传常量,不写字面量:读侧拼错 = 静默查空(计数恒 0,
# "反补满 2 次转删"永不触发),和写侧拼错是同一类病,但更难发现。
_SQL_ATTEMPTS = """
SELECT store, sku, count(*) FROM catalog.product_events
WHERE event = %s
  AND source = ANY(%s)
  AND occurred_at > now() - make_interval(days => %s)
GROUP BY store, sku
"""
# source 过滤(2026-08-07):MP_MAINTENANCE 是通用部分更新 feed,未来
# maintenance 工作流的标题/到期日期操作若记事件,不得污染反补转删计数。
# ⚠ 批次 E 拆分后这里必须**同时认两个 source**:历史事件的 source 是
# 'problem_product_cleanup'(拆分前),拆分后执行件仍用同一个 source 写事件
# ——但万一将来改名,漏掉旧值会让 30 天窗口内的历史反补次数归零,
# "反补满 2 次转删"重新从 0 数起,商品被无限反补。
_ATTEMPT_SOURCES = ["problem_product_cleanup", "problem_scan"]

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
# (否则重上架的同 ASIN 首次出问题就被双 feed 直删,跳过反补机会)。
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
        items = [dict(zip(("store", "sku", "gtin", "upc", "reasons"), r))
                 for r in cur.fetchall()]
        cur.execute(_SQL_INFLIGHT)
        inflight = set(cur.fetchall())
        cur.execute(_SQL_ATTEMPTS, (product_events.MAINTENANCE_SUBMITTED,
                                    _ATTEMPT_SOURCES, pp.ATTEMPT_RESET_DAYS))
        attempts = {(s, k): n for s, k, n in cur.fetchall()}
        cur.execute(_SQL_LAST_CAT, (product_events.PROBLEM_CATEGORIZED,))
        last_cat = {(s, k): c for s, k, c in cur.fetchall()}
        cur.execute(_SQL_STUBBORN)
        stubborn = {(st, k) for st, k, ev in cur.fetchall()
                    if ev == 'delete_not_effective'}
        cur.execute(_SQL_STATUS)
        inactive = {s for s, st in cur.fetchall()
                    if st and st.upper() != "ACTIVE"}
    return items, inflight, attempts, last_cat, inactive, stubborn


def plan(items, inflight, attempts, inactive, stubborn=frozenset()):
    """输入:问题商品与去重状态 → 输出:(计划 dict, 计数 dict)。纯函数,可测。

    计划形如 {店铺: {"relist": [item行], "delete": [item行], "retire": [...]}}
    每行附 category/cat_name。**逐字迁自 problem_product_cleanup(批次 E 拆分)**
    ——行为一个字没改,只是换了个住处:决策归扫描件,执行件不再自己决策。
    """
    out: dict[str, dict] = {}
    n = {"stage": 0, "inflight": 0, "inactive": 0,
         "relist": 0, "delete": 0, "fallback": 0, "stubborn": 0}
    for it in items:
        key = (it["store"], it["sku"])
        if it["store"] in inactive:
            n["inactive"] += 1
            continue
        if pp.is_stage_pending(it["reasons"]):
            n["stage"] += 1
            continue
        if key in inflight:
            n["inflight"] += 1
            continue
        code, name = pp.categorize(it["reasons"])
        it["category"], it["cat_name"] = code, name
        bucket = out.setdefault(it["store"],
                                {"relist": [], "delete": [], "retire": []})
        if key in stubborn:
            # 删除未生效的顽固 SKU(所有者定稿):
            # 停用+删除双 feed 齐发——能删的删,删不掉的至少停用
            bucket["retire"].append(it)
            bucket["delete"].append(it)
            n["stubborn"] += 1
            continue
        if code == "A":
            if attempts.get(key, 0) >= pp.MAX_ATTEMPTS:
                n["fallback"] += 1          # 反补满 2 次仍过期 → 转删除兜底
            elif pp.build_relist_item(it["sku"], it["gtin"], it["upc"]):
                bucket["relist"].append(it)
                n["relist"] += 1
                continue
            # A 类无 productId 同样落到删除(旧规则)
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
        for action in ("relist", "delete", "retire"):
            for it in b.get(action, []):
                rows.append({
                    "store": store, "sku": it["sku"], "source": "scan",
                    "action": action, "category": it.get("category"),
                    "reason": it.get("reasons") or "",
                    "detail": {"cat_name": it.get("cat_name"),
                               "gtin": it.get("gtin"), "upc": it.get("upc")},
                })
    return rows


def drop_conflicting_relists(rows: list[dict],
                             audit_rows: list[dict]) -> tuple[list[dict], int]:
    """输入:scan 建议 + audit 建议 → 输出:(去掉矛盾反补后的 scan 建议, 剔除数)。

    纯函数,可测。⚠ **生产 dry-run 实遇(2026-08-14)**:同一个 SKU 同时被
    scan 建议"反补"(A 类过期,救活)与 audit 建议"删除"(审核判拒)。两条
    建议 action 不同 ⇒ 部分唯一索引不冲突 ⇒ 两条都落 ⇒ 执行件按
    _ACTION_ORDER 先发 relist 再发 delete:**先花配额把它救活,再花配额把它
    删掉**,两个 feed 都提交、结果还不确定。

    优先级:**审核判拒压过一切救活动作**。审核说这产品不该卖,把它救活是反向
    操作;而"过期"只是它当前不可售的一个技术原因,救活了照样该被删。
    删除建议保留(该删还是要删),只砍掉同 SKU 的反补建议。
    """
    banned = {(r["store"], r["sku"]) for r in audit_rows
              if r["action"] == "delete"}
    if not banned:
        return rows, 0
    kept = [r for r in rows
            if not (r["action"] == "relist" and (r["store"], r["sku"]) in banned)]
    return kept, len(rows) - len(kept)


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
    out = [f"problem_scan:问题商品 {n_items} 行 → 建议 反补 "
           f"{by_act.get('relist', 0)},删除 {by_act.get('delete', 0)}"
           f"(其中审核判拒 {sum(1 for r in allrows if r.get('source') == 'audit')}"
           f",反补满额转删 {n['fallback']}),"
           f"顽固停用 {by_act.get('retire', 0)};"
           f"Stage 排除 {n['stage']},在途/待观测跳过 {n['inflight']},"
           f"非 ACTIVE 店跳过 {n['inactive']}"]
    per_store: dict[str, dict] = {}
    for r in allrows:
        b = per_store.setdefault(r["store"],
                                 {"relist": [], "delete": [], "retire": []})
        b[r["action"]].append(r)
    for store, b in sorted(per_store.items()):
        cats: dict[str, int] = {}
        for r in b["delete"] + b["relist"]:
            k = r.get("category") or "-"
            cats[k] = cats.get(k, 0) + 1
        line = (f"  {store}:反补 {len(b['relist'])},删除 {len(b['delete'])}"
                + (f",顽固停用 {len(b['retire'])}" if b["retire"] else "")
                + ",类别={" + ",".join(f"{c}:{v}" for c, v in sorted(cats.items()))
                + "}")
        if b["delete"]:
            line += f",删除样本={[(r['sku'], r.get('category')) for r in b['delete'][:5]]}"
        if b["relist"]:
            line += f",反补样本={[(r['sku'], r.get('category')) for r in b['relist'][:3]]}"
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
    当轮 B/C/E/F/G/K 入 ASIN 黑名单,C/E 的品牌从 catalog.products.brand 取、
    按品牌去重入 brand_blacklist。
    **任何失败只告警不阻断**——黑名单是扫描的副产品,收集炸了不该把建议
    产出拖下水;漏一轮下一轮照样补(入选条件不变)。
    ⚠ **这里只写 PG,一个格子也不碰飞书**(所有者 2026-08-17 问「黑名单的品牌和
    asin 出来了会立刻推送到飞书表格吗」—— 不会,而且两条时间线不一样):

      · **否决闸立刻生效**:写完 `brand_blacklist`/`asin_blacklist` 那一刻,
        上架与审核就拦得住了 —— 它们读 PG,从不读飞书表。
      · **表格要等 `blacklist_push`**(调度里的 `blacklist` 任务,15:00),
        按库里全量整表重写。想立刻看见就手动跑一次 `cli.py blacklist_push`。

    (水位追加那套 2026-08-17 已废除,`pushed_at` 只剩"这行投影过了"的含义。)
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


def _audit_rejected_rows(conn, inflight: set, inactive: set,
                         only: str | None) -> list[dict]:
    """输入:连接 + 去重状态 → 输出:审核判拒但仍在架的建议行。

    与 scan 来源共用同一套闸(非 ACTIVE 店跳过、在途不建议),但**不走归类**
    ——审核已经给出结论了,这里不需要再猜沃尔玛为什么不高兴。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_AUDIT_REJECTED)
        rows = cur.fetchall()
    out = []
    for store, sku, asin, reason, after_listing in rows:
        if only and store != only:
            continue
        if store in inactive or (store, sku) in inflight:
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
    items, inflight, attempts, last_cat, inactive, stubborn = _load_state()
    if only:
        items = [i for i in items if i["store"] == only]

    plans, n = plan(items, inflight, attempts, inactive, stubborn)
    rows = to_dispositions(plans)
    lines: list[str] = []

    with db.pg_conn() as conn:
        audit_rows = _audit_rejected_rows(conn, inflight, inactive, only)
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
        rows, n_conflict = drop_conflicting_relists(rows, audit_rows)
        if n_conflict:
            lines.append(
                f"  ⚠ 剔除矛盾反补 {n_conflict} 条:这些 SKU 同时被判"
                f"「审核拒→删」与「过期→救活」,审核判拒压过救活"
                f"(否则会先花配额救活、再花配额删掉)")
        allrows = rows + audit_rows
        # ⚠ 摘要在**剔矛盾之后**才生成,报的是真正会落库的数。首版在剔除之前
        # 就把 plan() 的原始数打出来了(反补 10),而实际只落 8 —— 人眼闸门看的
        # 就是这几个数,不该还要自己做减法。分店明细同理。
        # 另:这里按建议行统计,不是按 plan() 的桶 —— n['delete'] 不含顽固双击
        # 那 22 个(那支 continue 前没有 n['delete'] += 1),照它报会少 22。
        head = _summarize(allrows, audit_rows, n, len(items))
        lines[:0] = head        # 总览 + 分店明细排在最前,审核/剔除说明跟其后
        if preview:
            lines.append(f"(preview:未落建议行;本轮将写 {len(allrows)} 条"
                         f"——实际落库可能更少:同 (店铺,SKU,动作) 被两个来源"
                         f"命中时按唯一索引合并)")
            return "\n".join(lines)
        n_cat = _record_categories(conn, items, last_cat)
        bl_note = _collect_blacklists(conn, items)
        n_sug = dispositions.suggest_many(conn, allrows)
        # 撤销本轮不再建议的陈旧行(按来源各撤各的):否则昨天建议删、今天
        # 已恢复正常的 SKU,那条 suggested 还挂着,执行件照样会删
        n_wd = 0
        for src, srows in (("scan", rows), ("audit", audit_rows)):
            # ⚠ store=only 不能省:`-p store=X` 那一轮只扫了一个店,keep 里
            # 只有该店的行,不限范围会把其余全部店铺的待执行建议一次清空
            n_wd += dispositions.withdraw_stale(
                conn, src, [(r["store"], r["sku"], r["action"]) for r in srows],
                why=f"本轮扫描不再建议{f'(限 {only})' if only else ''}",
                store=only or None)
        # 限本链来源:维护链共用同一张建议表,不限的话摘要报的数会把它的
        # 待执行也算进来,与本链执行件领到的数对不上
        n_open = dispositions.count_open(
            conn, sources=dispositions.PROBLEM_SOURCES)
        # ⚠ 本链**没有** expire_executing 那道兜底(删除/反补靠观测判定,
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
    lines.append("执行走 `python cli.py problem_product_cleanup`(先 --dry-run 看破坏面)"
                 "(本工作流不发任何 feed)")
    return "\n".join(lines)
