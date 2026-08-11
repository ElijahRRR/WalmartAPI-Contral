"""problem_product_cleanup — 问题商品清理(plan #8,替代旧 daily_cleanup;危险,默认 dry-run)。

用法:
  python cli.py problem_product_cleanup                # dry-run:各店各类别统计+样本
  python cli.py problem_product_cleanup --execute      # 真跑(反补/删除 + 落账)
  python cli.py problem_product_cleanup -p store=A085朱丽霖

架构变更(所有者定稿 2026-08-06):**不再自行调 API 拉问题商品**,直接读
catalog.walmart_items(catalog_sync 每日全量维护)决策——⚠ 调度纪律:
catalog_sync 必须先于本工作流跑,数据新鲜度 = 上一次扫店时间。

流程:查库(UNPUBLISHED/SYSTEM_PROBLEM 且未缺席)→ 归类(services/
problem_products,规则逐字移植旧系统)→ 三路:
  Stage 待发布            → 排除不动
  A 过期(有 productId,30 天内反补 <2 次)→ MP_MAINTENANCE 设置商品到期日期(2049)救活
  其余(含 A 类落选/反补满 2 次转删)      → DELETE_ITEM 永久删除
  删除未生效顽固 SKU(delete_not_effective)→ 停用+删除双 feed 齐发

去重(全部查库;所有者定稿:不设时间防重窗,重复删除无实害,真相以观测为准):
  ① 在途/待观测:feed_items 有 submitted 未落定(**滚动 48h 封顶**,超时
     视为 feed 丢失放行重发——所有者拍板 2026-08-11,替代旧"同一自然日"),
     或已落定 success 但
     catalog_sync 尚未重新观测(resolved_at > last_seen_at)→ 跳过
     (feed 处理中不叠发;动作结果未经观测确认前不重复动作);
  ② 反补计数:product_events 的 maintenance_submitted 30 天窗口计数(替代
     revived_skus.json);
  ③ 归类事件:同 (店铺,SKU) 类别未变不重复记(替代 seen_sku_categories.json)。

店铺闸:ops.store_kpi_daily 最新 store_status 非 ACTIVE 的店整体跳过
(数据驱动,替代旧的逐店 payment/statement 查询;无 KPI 记录视为 ACTIVE)。

容错(2026-08-07 所有者定稿):单店提交异常只跳过该店不炸整轮;第一轮全部
店跑完后,对网络波动失败的店(token/代理阶段确定未达 retryable,或提交块
抛异常)整店**二轮重提一次**——已成功切片被在途防重拦下,不重复不漏;
二轮仍失败交下轮调度。凭证失效(StoreDeadError)与 4xx 被拒不触发重试。

首版裁剪(所有者确认):砍邮件;监管合规定点删除由 product_clear 的
停用/删除表承担;品牌采集/黑名单/飞书统计表后置;旧库 41.7 万行历史导入另做。

⚠ 切换纪律:上调度前必须停旧 walmart-daily-cleanup cron(0/6/12/18 点)。
"""

import logging

from api import _client, feeds
from registry import db
from services import blacklist
from services import problem_products as pp
from services import product_events, stores as stores_svc

DANGEROUS = True

logger = logging.getLogger("workflows.problem_product_cleanup")

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
# 在途/待观测拦截(2026-08-07 生产实证修正):submitted=feed 处理中不叠发;
# success 且 resolved_at 晚于该行 last_seen_at = 动作已生效但 catalog_sync
# 还没重新扫到——结果未经观测确认前不重复动作("真相以观测为准"的闸门形态)。
# 否则 feed 落定后、次日扫店前重跑本工作流,会把同一批 SKU 全量重发一遍。
# 观测后自然解锁:扫到即 last_seen_at 前移;删成了即缺席出清单;failed 不拦(该重试)。
# 事件名一律绑参传常量,不写字面量:读侧拼错 = 静默查空(计数恒 0,
# "反补满 2 次转删"永不触发),和写侧拼错是同一类病,但更难发现。
_SQL_ATTEMPTS = """
SELECT store, sku, count(*) FROM catalog.product_events
WHERE event = %s
  AND source = 'problem_product_cleanup'
  AND occurred_at > now() - make_interval(days => %s)
GROUP BY store, sku
"""
# source 过滤(2026-08-07):MP_MAINTENANCE 是通用部分更新 feed,未来
# maintenance 工作流的标题/到期日期操作若记事件,不得污染反补转删计数
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


def _load_state():
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_ITEMS)
        items = [dict(zip(("store", "sku", "gtin", "upc", "reasons"), r))
                 for r in cur.fetchall()]
        cur.execute(_SQL_INFLIGHT)
        inflight = set(cur.fetchall())
        cur.execute(_SQL_ATTEMPTS, (product_events.MAINTENANCE_SUBMITTED,
                                    pp.ATTEMPT_RESET_DAYS))
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

    计划形如 {店铺: {"relist": [item行], "delete": [item行]}};
    每行附 category/cat_name。
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


def _record(store: str, event: str, rows: list[dict], feed_id) -> None:
    with db.pg_conn() as conn:
        product_events.record_many(conn, [
            {"sku": r["sku"], "store": store, "event": event,
             "source": "problem_product_cleanup",
             "detail": {"feed_id": feed_id, "category": r["category"],
                        "reason": (r["reasons"] or "")[:200]}}
            for r in rows])


def _collect_blacklists(items: list[dict]) -> str:
    """输入:当轮已归类 item → 输出:黑名单收集摘要(一行)。

    归因收集尾段(plan.md「品牌限制/侵权类问题产品 → 品牌黑名单」的落地,
    2026-08-11 接通自产回路):当轮 B/C/E/F/G/K 入 ASIN 黑名单,C/E 的品牌
    从 catalog.products.brand 取、按品牌去重入 brand_blacklist。
    **任何失败只告警不阻断**——黑名单是清理的副产品,收集炸了不该把
    删除/反补主链拖下水;漏一轮下一轮照样补(入选条件不变)。
    飞书投影由 blacklist_push 按 pushed_at 水位另推,这里只写 PG。
    """
    cand = [it for it in items if "category" in it]
    if not cand:
        return ""
    try:
        with db.pg_conn() as conn:
            asin_new = blacklist.record_asins(conn, cand)
            st = blacklist.collect_brands(conn, cand)
    except Exception as e:                              # noqa: BLE001
        logger.error("黑名单收集失败(主链不受影响,下轮重收): %s", e)
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


def _record_categories(items: list[dict], last_cat: dict) -> int:
    """归类事件:仅 (店铺,SKU) 类别变化时落账(病历不灌水)。"""
    fresh = [it for it in items if "category" in it
             and last_cat.get((it["store"], it["sku"])) != it["category"]]
    with db.pg_conn() as conn:
        product_events.record_many(conn, [
            {"sku": it["sku"], "store": it["store"],
             "event": product_events.PROBLEM_CATEGORIZED,
             "source": "problem_product_cleanup",
             "detail": {"category": it["category"], "name": it["cat_name"],
                        "reason": (it["reasons"] or "")[:200]}}
            for it in fresh])
    return len(fresh)


def run(params: dict) -> str:
    """输入:params(execute/store)→ 输出:归类统计与提交结果摘要。"""
    execute = bool(params.get("execute"))
    items, inflight, attempts, last_cat, inactive, stubborn = _load_state()
    only = params.get("store")
    if only:
        items = [i for i in items if i["store"] == only]
    if not items:
        return "无问题商品(UNPUBLISHED/SYSTEM_PROBLEM 且在架)"

    plans, n = plan(items, inflight, attempts, inactive, stubborn)
    mode = "" if execute else "🧪 [DRY-RUN] "
    lines = [f"{mode}问题商品 {len(items)} 行:反补 {n['relist']},删除 {n['delete']}"
             f"(含反补满额转删 {n['fallback']}),Stage 排除 {n['stage']},"
             f"在途/待观测跳过 {n['inflight']},非 ACTIVE 店跳过 {n['inactive']},"
             f"顽固双击 {n['stubborn']}"]

    if not execute:
        # 各店各类别统计 + 按动作分开的样本(人眼闸门的判断依据:
        # 反补=救活方向,删除=不可逆方向,混排会误导)
        for store, b in sorted(plans.items()):
            if not (b["relist"] or b["delete"] or b["retire"]):
                continue
            cats: dict[str, int] = {}
            for r in b["delete"] + b["relist"]:
                cats[r["category"]] = cats.get(r["category"], 0) + 1
            line = (f"  {store}:反补 {len(b['relist'])},删除 {len(b['delete'])}"
                    + (f",顽固双击 {len(b['retire'])}" if b["retire"] else "")
                    + ",类别={" + ",".join(f"{c}:{n}" for c, n in sorted(cats.items())) + "}")
            if b["delete"]:
                line += f",删除样本={[(r['sku'], r['category']) for r in b['delete'][:5]]}"
            if b["relist"]:
                line += f",反补样本={[(r['sku'], r['category']) for r in b['relist'][:3]]}"
            lines.append(line)
        return "\n".join(lines)

    stores_by_name = {s["name"]: s for s in stores_svc.load_stores()}
    n_cat = _record_categories(items, last_cat)
    bl_note = _collect_blacklists(items)
    retry_stores: list[str] = []
    for store_name, b in sorted(plans.items()):
        store = stores_by_name.get(store_name)
        if store is None:
            lines.append(f"  {store_name}:凭证缺失,跳过")
            continue
        if _submit_store(store_name, store, b, lines):
            retry_stores.append(store_name)

    if retry_stores:
        # 二轮重试(所有者定稿 2026-08-07):第一轮全部店跑完后,对网络波动
        # 失败的店整店重提一次。衔接安全:第一轮已成功的切片仍在途,被
        # feed_log 在途防重拦下(dedup 不落账不重发);token 阶段确定未达的
        # 切片已落 failed 可重占,同载荷重提。二轮仍失败交下轮调度,不无限重试。
        lines.append(f"二轮重试 {len(retry_stores)} 店:{','.join(retry_stores)}")
        for store_name in retry_stores:
            if _submit_store(store_name, stores_by_name[store_name],
                             plans[store_name], lines):
                lines.append(f"  ⚠ {store_name}:二轮仍失败,待下轮调度")

    lines.append(f"归类事件新记 {n_cat} 条;结果轮询走 feed_poll")
    if bl_note:
        lines.append(bl_note)
    return "\n".join(lines)


def _submit_store(store_name: str, store: dict, b: dict,
                  lines: list[str]) -> bool:
    """输入:店铺 + 三桶计划 → 输出:是否需二轮重试(网络类失败)。

    多切片滑窗对位记账(与 product_clear._submit_new 同款);只有
    outcome=submitted 才落事件——dedup 携带旧 feed_id 但什么都没提交,
    记了就是幽灵事件,反补计数被灌水会导致少一次真实反补就转永久删除。
    retryable=token/代理阶段确定未达;凭证失效(StoreDeadError)不重试。
    """
    need_retry = False

    def _submit(feed_type: str, entries: list, rows: list[dict],
                event: str, label: str) -> None:
        nonlocal need_retry
        i = 0
        n = {"submitted": 0, "dedup": 0, "failed": 0, "unknown": 0}
        for res in feeds.submit_feed(store, feed_type, entries,
                                     workflow="problem_product_cleanup"):
            rows_slice = rows[i:i + res["count"]]
            i += res["count"]
            n[res["outcome"]] = n.get(res["outcome"], 0) + len(rows_slice)
            if res["outcome"] == "submitted" and res["feed_id"]:
                _record(store_name, event, rows_slice, res["feed_id"])
            elif res.get("retryable"):
                need_retry = True
        line = f"  {store_name}:{label}提交 {n['submitted']}"
        if n["dedup"]:
            line += f",在途防重跳过 {n['dedup']}"
        if n["failed"]:
            line += f",⚠ 提交失败 {n['failed']}(查日志)"
        if n["unknown"]:
            line += f",⚠ 结局不确定留 pending {n['unknown']}(待对账)"
        lines.append(line)

    # 单店隔离(2026-08-07 生产实证:单店代理 TLS 断线炸掉整轮,
    # 后面的店全部没轮到):任何异常只跳过该店,其余店继续
    try:
        if b["relist"]:
            _submit("MP_MAINTENANCE",
                    [pp.build_relist_item(r["sku"], r["gtin"], r["upc"])
                     for r in b["relist"]],
                    b["relist"], product_events.MAINTENANCE_SUBMITTED, "反补")
        if b["retire"]:
            _submit("RETIRE_ITEM", [r["sku"] for r in b["retire"]],
                    b["retire"], product_events.RETIRE_SUBMITTED, "顽固停用")
        if b["delete"]:
            _submit("DELETE_ITEM", [r["sku"] for r in b["delete"]],
                    b["delete"], product_events.DELETE_SUBMITTED, "删除")
    except _client.StoreDeadError as e:
        logger.error("店铺 %s 凭证失效,跳过(不重试): %s", store_name, e)
        lines.append(f"  {store_name}:凭证失效跳过")
        return False
    except Exception as e:
        logger.exception("店铺 %s 提交异常: %s", store_name, e)
        lines.append(f"  ⚠ {store_name}:提交异常({e}),转入二轮重试")
        return True
    return need_retry
