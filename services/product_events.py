"""产品事件账本积木(catalog.product_events):SKU(=ASIN)一生的病历。

事件码唯一出处 = **本文件下方的常量与 EVENTS 集合**(2026-08-11 起是代码级
约束,不再只是 docstring 约定):record_many 对未登记的码直接抛错。
此前全仓散着字符串字面量,拼错不报错——账本是只追加的,消费方按精确字符串
过滤,一个拼错的码意味着那些事件**永远查不到**,而写入时一声不吭。
schema.sql / db_schema.md 里的清单只是导览,以这里为准(三处清单曾各漂各的:
maintenance_submitted、problem_categorized 发了大半个月都没登记)。

事件码(新增先在下方登记常量,再使用):
  item_appeared          catalog_sync 观测到新上架(店铺目录里首次/重新出现为新行)
  item_reappeared        曾标缺席(missing_since)后又被扫到
  item_missing           本轮全量扫未见(被删/被平台移除的观测事实)
  status_changed         published_status 变化(detail 含 old/new/官方原因)
  delete_submitted       提交 DELETE_ITEM(detail 含 feed_id/操作原因);来源
                         product_clear(飞书驱动)、problem_product_cleanup、
                         maintenance(采集永久偏移,detail.reason=variant_offset
                         ——维护类不入病历的唯一例外)——按 source 字段区分
  retire_submitted       product_clear 提交 RETIRE_ITEM
  maintenance_submitted  problem_product_cleanup 的**反补**提交(MP_MAINTENANCE
                         仅此来源入病历,见 receipt_in_ledger;"反补满 2 次
                         转删"的计数就数它,漏发 = 过期商品被无限反补)
  problem_categorized    problem_product_cleanup 的问题归类快照(13 类之一,
                         detail 含 category/name/reason;类别未变不重复记)
  match_submitted        match_listing 提交跟卖 MP_ITEM_MATCH(上架类=生死事件;
                         注意跟卖商品无 amz 侧身份,sku≠asin 是此类行的例外)
  list_submitted         list_new 提交上架 MP_ITEM(L2d 接线;回执
                         list_feed_{success|failed} 由 feed_track 白名单放行)
  {delete|retire|maintenance}_feed_{success|failed}
                         feed 终态的逐 SKU 回执(feed_track 写;success 是
                         沃尔玛的一面之词,删除以观测核验为准)
  delete_verified        观测核验:回执成功且商品确实从目录消失
  delete_not_effective   观测核验:回执成功但宽限期后商品仍在架(真实案例,
                         所有者实证)——告警,人工处置
  product_ingested       product_ingest 新 ASIN 首次入库(审核迁入批次 A 登记,
                         批次 B 接线写入;detail 建议含 source_id/slow_hash)
  audit_passed           product_audit 审核通过(批次 B 起写;detail 建议含
                         walmart_pt/audit_version/run_id)
  audit_rejected         product_audit 审核拒绝(批次 B 起写;detail 建议含
                         reason/rule_code/run_id)

原则:只追加永不改;回执与观测分开记,互相印证。

上架拦截口径(所有者 2026-08-12):**防呆=黑名单,不看删除史**——拦
"出现过侵权/审查等拉黑类别"的(catalog.asin_blacklist / brand_blacklist,
由 problem_categorized 时间线按最新类别投影),不拦"因产品问题删过"的
(可修复类删除后重上是正常经营)。product_risk 视图因此只是**风险档案
查询入口**(人工/AI SELECT),不是拦截条件;list_new 仅用其
unexplained_missing 标志做"疑似平台下架"的报警(不拦截)。

读侧视图(schema.sql 定义,人工/AI SELECT 入口,身份键一律
coalesce(asin, sku)):product_risk(全局风险档案,**含审核维度**)/
product_risk_store(店铺维度:"这个产品在哪些店被删过几次")/
status_changes(平台状态迁移与官方下架原因)/ feed_failures(五类 feed
的逐 SKU 失败回执)/ **audit_listing_conflicts**(审核结论 × 上架现状)。

⚠ **审核事件的 store 是 NULL**(审核不分店铺,一个 ASIN 一个结论),所以
audit_passed/audit_rejected 进得了全局 product_risk,却进不了
product_risk_store(那个视图 WHERE store IS NOT NULL)。要回答"哪个店里
哪些产品拒了还挂着",必须走 audit_listing_conflicts —— 它拿全局审核结论
JOIN 店铺维度的现状表。problem_scan 的"审核判拒仍在架"就是它的消费方。

这两个码 2026-08-14 前**零读者**:全库 119 万条事件写了没人看,而
"审核拒了但还在架"却要靠现拼 JOIN 回答。所有者定稿:接消费端而不是停写
——病历的价值本就是把审核结论、上架动作、平台观测串在一条时间线上。

审核接缝落地(2026-08-13 批次 A):入库/审核三码已登记(product_ingested /
audit_passed / audit_rejected),写入方 product_ingest 与 product_audit 在
批次 B 接线。登记先行不算休眠码——批次序列已定稿
(docs/audit_migration_plan.md 第十一节),写入方已在路上。

入账边界(所有者定稿 2026-08-07):病历只记**产品生死**(删除/停用/反补)
与观测事实。标题/价格/库存维护(含清库存)一律不进——清库存是店铺维度的
运营操作;此类操作的流水在 ops.feed_log/feed_items,现状在
catalog.walmart_items 快照,若引发平台下架等后果,由 catalog_sync 的
status_changed 观测自动入账(动作不记,生死后果必记)。
⚠ 「本系统不设店铺维度病历」这句 2026-08-30 被所有者推翻:店铺维度账本
已立(ops.store_events,见 services/store_events.py,TRO 封店预警需求)。
两本账分工不变:本表记产品生死,店铺账本记店铺状态迁移/风险波及/按轮汇总
的运营动作 —— 逐 SKU 维护流水仍然哪本都不进(在 ops.feed_items)。
"""

import json
import logging

from services.sku_asin import extract_asin

logger = logging.getLogger("services.product_events")

_FEED_KIND = {"DELETE_ITEM": "delete", "RETIRE_ITEM": "retire",
              "MP_MAINTENANCE": "maintenance", "MP_ITEM_MATCH": "match",
              "MP_ITEM": "list"}

# ── 事件码常量(唯一出处;新增先在此登记)──────────────────────────────────────
ITEM_APPEARED = "item_appeared"
ITEM_REAPPEARED = "item_reappeared"
ITEM_MISSING = "item_missing"
STATUS_CHANGED = "status_changed"
DELETE_SUBMITTED = "delete_submitted"
RETIRE_SUBMITTED = "retire_submitted"
MAINTENANCE_SUBMITTED = "maintenance_submitted"
MATCH_SUBMITTED = "match_submitted"
LIST_SUBMITTED = "list_submitted"
PROBLEM_CATEGORIZED = "problem_categorized"
DELETE_VERIFIED = "delete_verified"
DELETE_NOT_EFFECTIVE = "delete_not_effective"
PRODUCT_INGESTED = "product_ingested"
AUDIT_PASSED = "audit_passed"
AUDIT_REJECTED = "audit_rejected"
# 码级事件(SKU 改造批次 0a 登记;写入点是 services/sku_codec.abandon,批次 2 接线)。
# 提前登记零副作用:EVENTS 只是 record_many 的入参校验白名单,不登记就会在批次 2
# 当场抛 ValueError。detail 结构约定(**必须带 source_key** —— 不透明码在 asin
# 列里提不出来,list_new 的代际过滤读的是 detail->>'source_key'):
#   sku_abandoned:{old_sku, reason, source_type, source_key, burned_upcs}
#   sku_replaced :{old_sku, new_sku, reason, source_type, source_key, burned_upcs}
SKU_ABANDONED = "sku_abandoned"
SKU_REPLACED = "sku_replaced"

# 合法事件码全集:上面的显式码 + 五类 feed 的 {kind}_feed_{success|failed} 回执。
# record_many 只认这个集合——宁可提交时炸,不要账本里静默多出一支没人查的分叉。
EVENTS = frozenset({
    ITEM_APPEARED, ITEM_REAPPEARED, ITEM_MISSING, STATUS_CHANGED,
    DELETE_SUBMITTED, RETIRE_SUBMITTED, MAINTENANCE_SUBMITTED,
    MATCH_SUBMITTED, LIST_SUBMITTED, PROBLEM_CATEGORIZED,
    DELETE_VERIFIED, DELETE_NOT_EFFECTIVE,
    PRODUCT_INGESTED, AUDIT_PASSED, AUDIT_REJECTED,
    SKU_ABANDONED, SKU_REPLACED,
} | {f"{k}_feed_{st}" for k in _FEED_KIND.values()
     for st in ("success", "failed")})

# 回执入账白名单:生死类恒记(删除/停用/跟卖上架);MP_MAINTENANCE 是
# 通用部分更新 feed,只有反补来源(登记制,未来 listing 反补在此登记)
# 才记,标题/到期日期等常规维护走同一 feedType 但不入病历
_RECEIPT_KINDS = {"delete", "retire", "match", "list"}
_MAINT_LEDGER_WORKFLOWS = {"problem_product_cleanup"}


def feed_kind(feed_type: str) -> str:
    return _FEED_KIND.get(feed_type, feed_type.lower())


def receipt_in_ledger(kind: str, workflow: str | None) -> bool:
    """输入:feed 业务类别 + 提交来源工作流 → 输出:该回执是否进病历。

    维护事件入账定稿(所有者 2026-08-07):改价/改库存/改标题/清库存的
    回执不进 product_events(流水已在 ops.feed_items);delete/retire 恒进;
    maintenance 仅反补来源进(反补计数依赖其提交/回执链)。
    """
    if kind in _RECEIPT_KINDS:
        return True
    return kind == "maintenance" and (workflow or "") in _MAINT_LEDGER_WORKFLOWS


def record_many(conn, rows: list[dict]) -> int:
    """输入:连接 + 事件行 [{sku, store, event, source, error_code?, detail?,
    occurred_at?}] → 输出:写入数。detail 自动 JSON 序列化。

    occurred_at 只给**历史导入**用(把旧库的 run_ts 原样带进时间线);
    实时链路一律不传,吃列默认值 now()——实时事件自带发生时刻,倒填时间戳
    只会让"账本只追加"的时序性变得不可信。"""
    if not rows:
        return 0
    # 未登记的码直接拒收:账本只追加、消费方按精确字符串过滤,拼错的码
    # 写进去就是一支永远没人查的分叉,而且不报错。fail loud 是唯一解。
    bad = sorted({r["event"] for r in rows} - EVENTS)
    if bad:
        raise ValueError(f"未登记的事件码 {bad}:先在 services/product_events.py "
                         f"的常量与 EVENTS 登记,再使用(唯一出处纪律)")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO catalog.product_events "
            "(sku, asin, store, event, source, error_code, detail, occurred_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, coalesce(%s, now()))",
            # asin 由 sku 清洗得出(services/sku_asin 唯一规则出处);提不出
            # 存 NULL——消费方用 coalesce(asin, sku),等规则扩了跑 sku_normalize 补
            [(r["sku"], extract_asin(r["sku"]), r.get("store"), r["event"],
              r["source"], r.get("error_code"),
              json.dumps(r["detail"], ensure_ascii=False, default=str)
              if r.get("detail") is not None else None,
              r.get("occurred_at"))
             for r in rows])
    return len(rows)


def diff_catalog(old: dict, new_rows: list[dict], store: str,
                 source: str = "catalog_sync") -> list[dict]:
    """输入:旧状态 {sku: (published_status, missing_since)} + 本轮行 + 店铺
    → 输出:状态迁移事件列表(纯函数,便于测试)。

    - 旧表没有 → item_appeared
    - 旧表标缺席又出现 → item_reappeared
    - published_status 变化 → status_changed(detail 含官方 unpublished 原因,
      这就是"平台把它下架了、为什么"的观测记录)
    """
    events = []
    for r in new_rows:
        sku = r.get("sku")
        if not sku:
            continue
        prev = old.get(sku)
        new_st = r.get("published_status")
        if prev is None:
            events.append({"sku": sku, "store": store, "event": ITEM_APPEARED,
                           "source": source,
                           "detail": {"published_status": new_st}})
            continue
        prev_st, prev_missing = prev
        if prev_missing is not None:
            # 复现只记 reappeared(detail 已含新状态);缺席行状态列已被清空,
            # 再比对必然"变化",叠记 status_changed(old=None)是噪音
            events.append({"sku": sku, "store": store,
                           "event": ITEM_REAPPEARED, "source": source,
                           "detail": {"published_status": new_st}})
            continue
        if new_st != prev_st:
            events.append({"sku": sku, "store": store,
                           "event": STATUS_CHANGED, "source": source,
                           "detail": {"old": prev_st, "new": new_st,
                                      "reasons": r.get("unpublished_reasons")}})
    return events


_VERIFY_SQL = """
WITH last_ok AS (
    SELECT DISTINCT ON (store, sku) store, sku, occurred_at
    FROM catalog.product_events
    WHERE event = 'delete_feed_success'
    ORDER BY store, sku, occurred_at DESC),
open_ok AS (
    SELECT l.* FROM last_ok l
    WHERE NOT EXISTS (
        SELECT 1 FROM catalog.product_events v
        WHERE v.store = l.store AND v.sku = l.sku
          AND v.event IN ('delete_verified', 'delete_not_effective')
          AND v.occurred_at >= l.occurred_at))
SELECT o.store, o.sku,
       CASE WHEN w.sku IS NULL OR w.missing_since IS NOT NULL
                 OR w.lifecycle_status = 'RETIRED' THEN 'gone'
            WHEN w.last_seen_at > o.occurred_at + make_interval(hours => %s)
                 THEN 'still'
            ELSE 'wait' END AS verdict
FROM open_ok o
LEFT JOIN catalog.walmart_items w ON w.store = o.store AND w.sku = o.sku
"""


def verify_deletions(conn, grace_hours: int = 48) -> tuple[int, int]:
    """输入:连接 + 宽限小时数 → 输出:(核验生效数, 未生效数)。

    删除核验(不信回执,信观测):delete_feed_success 之后,
    - 商品从目录消失/标缺席/RETIRED → delete_verified;
    - 宽限期后 catalog_sync 仍扫到它在架 → delete_not_effective + 告警
      (回执说成了但后台没删,所有者实证的真实故障模式);
    - 还没等到下一轮扫描 → 保持待核验,不落判。
    """
    with conn.cursor() as cur:
        cur.execute(_VERIFY_SQL, (grace_hours,))
        rows = cur.fetchall()
    events = []
    gone = still = 0
    for store, sku, verdict in rows:
        if verdict == "gone":
            gone += 1
            events.append({"sku": sku, "store": store, "event": DELETE_VERIFIED,
                           "source": "catalog_sync"})
        elif verdict == "still":
            still += 1
            events.append({"sku": sku, "store": store,
                           "event": DELETE_NOT_EFFECTIVE,
                           "source": "catalog_sync"})
    if still:
        logger.warning("删除核验:%d 个 SKU 回执成功但仍在架(delete_not_effective),"
                       "样本=%s,请人工处置",
                       still, [(s, k) for s, k, v in rows if v == "still"][:5])
    record_many(conn, events)
    return gone, still
