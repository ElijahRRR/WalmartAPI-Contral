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
  删除未生效顽固 SKU(delete_not_effective)→ 绕过防重窗,停用+删除双 feed 齐发

去重(全部查库,替代旧三个 cache JSON):
  ① 在途:ops.feed_items 有 submitted 未落定 → 跳过;
  ② 近提交:7 天内同 (店铺,SKU) 已有 DELETE 提交/成功 → 跳过(删除成功但
     catalog_sync 还没观测到消失时不重复提交,等观测核验);
  ③ 反补计数:product_events 的 maintenance_submitted 30 天窗口计数(替代
     revived_skus.json);
  ④ 归类事件:同 (店铺,SKU) 类别未变不重复记(替代 seen_sku_categories.json)。

店铺闸:ops.store_kpi_daily 最新 store_status 非 ACTIVE 的店整体跳过
(数据驱动,替代旧的逐店 payment/statement 查询;无 KPI 记录视为 ACTIVE)。

首版裁剪(所有者确认):砍邮件;监管合规定点删除由 product_clear 的
停用/删除表承担;品牌采集/黑名单/飞书统计表后置;旧库 41.7 万行历史导入另做。

⚠ 切换纪律:上调度前必须停旧 walmart-daily-cleanup cron(0/6/12/18 点)。
"""

import logging

from api import feeds
from registry import db
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
_SQL_RECENT = """
SELECT store, sku, feed_type, status FROM ops.feed_items
WHERE submitted_at > now() - interval '7 days'
"""
_SQL_ATTEMPTS = """
SELECT store, sku, count(*) FROM catalog.product_events
WHERE event = 'maintenance_submitted'
  AND occurred_at > now() - make_interval(days => %s)
GROUP BY store, sku
"""
_SQL_LAST_CAT = """
SELECT DISTINCT ON (store, sku) store, sku, detail->>'category'
FROM catalog.product_events WHERE event = 'problem_categorized'
ORDER BY store, sku, occurred_at DESC
"""
_SQL_STUBBORN = """
SELECT DISTINCT ON (store, sku) store, sku, event
FROM catalog.product_events
WHERE event IN ('delete_verified', 'delete_not_effective')
ORDER BY store, sku, occurred_at DESC
"""
_SQL_STATUS = """
SELECT DISTINCT ON (store) store, store_status FROM ops.store_kpi_daily
ORDER BY store, data_date DESC
"""


def _load_state():
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_ITEMS)
        items = [dict(zip(("store", "sku", "gtin", "upc", "reasons"), r))
                 for r in cur.fetchall()]
        cur.execute(_SQL_RECENT)
        recent = cur.fetchall()
        cur.execute(_SQL_ATTEMPTS, (pp.ATTEMPT_RESET_DAYS,))
        attempts = {(s, k): n for s, k, n in cur.fetchall()}
        cur.execute(_SQL_LAST_CAT)
        last_cat = {(s, k): c for s, k, c in cur.fetchall()}
        cur.execute(_SQL_STUBBORN)
        stubborn = {(st, k) for st, k, ev in cur.fetchall()
                    if ev == 'delete_not_effective'}
        cur.execute(_SQL_STATUS)
        inactive = {s for s, st in cur.fetchall()
                    if st and st.upper() != "ACTIVE"}
    inflight = {(s, k) for s, k, _ft, st in recent if st == "submitted"}
    recent_del = {(s, k) for s, k, ft, st in recent
                  if ft == "DELETE_ITEM" and st in ("submitted", "success")}
    return items, inflight, recent_del, attempts, last_cat, inactive, stubborn


def plan(items, inflight, recent_del, attempts, inactive,
         stubborn=frozenset()):
    """输入:问题商品与去重状态 → 输出:(计划 dict, 计数 dict)。纯函数,可测。

    计划形如 {店铺: {"relist": [item行], "delete": [item行]}};
    每行附 category/cat_name。
    """
    out: dict[str, dict] = {}
    n = {"stage": 0, "inflight": 0, "recent": 0, "inactive": 0,
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
            # 删除未生效的顽固 SKU(所有者定稿):绕过 7 天防重窗,
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
        if key in recent_del:
            n["recent"] += 1
            continue
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


def _record_categories(items: list[dict], last_cat: dict) -> int:
    """归类事件:仅 (店铺,SKU) 类别变化时落账(病历不灌水)。"""
    fresh = [it for it in items if "category" in it
             and last_cat.get((it["store"], it["sku"])) != it["category"]]
    with db.pg_conn() as conn:
        product_events.record_many(conn, [
            {"sku": it["sku"], "store": it["store"],
             "event": "problem_categorized",
             "source": "problem_product_cleanup",
             "detail": {"category": it["category"], "name": it["cat_name"],
                        "reason": (it["reasons"] or "")[:200]}}
            for it in fresh])
    return len(fresh)


def run(params: dict) -> str:
    """输入:params(execute/store)→ 输出:归类统计与提交结果摘要。"""
    execute = bool(params.get("execute"))
    (items, inflight, recent_del, attempts, last_cat, inactive,
     stubborn) = _load_state()
    only = params.get("store")
    if only:
        items = [i for i in items if i["store"] == only]
    if not items:
        return "无问题商品(UNPUBLISHED/SYSTEM_PROBLEM 且在架)"

    plans, n = plan(items, inflight, recent_del, attempts, inactive, stubborn)
    mode = "" if execute else "🧪 [DRY-RUN] "
    lines = [f"{mode}问题商品 {len(items)} 行:反补 {n['relist']},删除 {n['delete']}"
             f"(含反补满额转删 {n['fallback']}),Stage 排除 {n['stage']},"
             f"在途跳过 {n['inflight']},近 7 天已提交跳过 {n['recent']},"
             f"非 ACTIVE 店跳过 {n['inactive']},顽固双击 {n['stubborn']}"]

    if not execute:
        for store, b in sorted(plans.items()):
            if b["relist"] or b["delete"]:
                lines.append(f"  {store}:反补 {len(b['relist'])},"
                             f"删除 {len(b['delete'])},样本="
                             f"{[(r['sku'], r['category']) for r in (b['delete'] + b['relist'])[:5]]}")
        return "\n".join(lines)

    stores_by_name = {s["name"]: s for s in stores_svc.load_stores()}
    n_cat = _record_categories(items, last_cat)
    for store_name, b in sorted(plans.items()):
        store = stores_by_name.get(store_name)
        if store is None:
            lines.append(f"  {store_name}:凭证缺失,跳过")
            continue
        if b["relist"]:
            payload = [pp.build_relist_item(r["sku"], r["gtin"], r["upc"])
                       for r in b["relist"]]
            for res in feeds.submit_feed(store, "MP_MAINTENANCE", payload,
                                         workflow="problem_product_cleanup"):
                if res["feed_id"]:
                    _record(store_name, "maintenance_submitted",
                            b["relist"][:res["count"]], res["feed_id"])
            lines.append(f"  {store_name}:反补提交 {len(b['relist'])}")
        if b["retire"]:
            for res in feeds.submit_feed(store, "RETIRE_ITEM",
                                         [r["sku"] for r in b["retire"]],
                                         workflow="problem_product_cleanup"):
                if res["feed_id"]:
                    _record(store_name, "retire_submitted",
                            b["retire"][:res["count"]], res["feed_id"])
            lines.append(f"  {store_name}:顽固停用提交 {len(b['retire'])}")
        if b["delete"]:
            for res in feeds.submit_feed(store, "DELETE_ITEM",
                                         [r["sku"] for r in b["delete"]],
                                         workflow="problem_product_cleanup"):
                if res["feed_id"]:
                    _record(store_name, "delete_submitted",
                            b["delete"][:res["count"]], res["feed_id"])
            lines.append(f"  {store_name}:删除提交 {len(b['delete'])}")
    lines.append(f"归类事件新记 {n_cat} 条;结果轮询走 feed_poll")
    return "\n".join(lines)
