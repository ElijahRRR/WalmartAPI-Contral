"""feed 明细的**实际结果**积木(生效 / 未生效)—— 所有者 2026-09-25 定稿。

  judge(conn, store=None) -> dict      给到期的 feed 明细判一次实际结果,落 ops.feed_effects
  review_rows(conn, days, store) -> list[dict]   复核清单:feed 成功(或没给结论)∧ 未生效
  verdict(row) -> tuple | None         单行判词(纯函数,可测)

所有者原话:「feed 结果和实际结果……是两个东西,应该分开。feed 结果是为了知道沃尔玛侧
的执行情况,而实际结果是为了让开发人员或者运营知道自己之前做的操作实际上在沃尔玛是否
成功生效……对于实际结果,就只需要生效/未生效。如果 feed 显示该明细是成功的,但是观测
结果是未生效,这种是人需要看的。」

三条规矩(docs/feed_ledger_v2_design.md「〇.3」):
  · **两本账互不写对方**:本模块只读 ops.feed_items(feed 结果)与观测
    (catalog.walmart_items / item_node_inventory),只写 ops.feed_effects;feed_poll
    不读本表。**不挂任何自动化**(所有者:自动再处理「目前来说没有必要」)。
  · **判一次,不回头改**:只判过了落定期限(feed_track.FEED_DEADLINE_MINUTES)的明细,
    用期限之后的观测;一旦落行不再改(ON CONFLICT DO NOTHING)。观测与实际可能错开
    (先生效后消失,观测只看到未生效)—— 表里记的就是"那次观测看到了什么、什么时候",
    不推断中间发生过什么。
  · **被覆盖的不判**:同一 (店, SKU, feedType) 之后又提交过新的一次,旧的那次不判 ——
    否则新改价会把旧改价判成未生效。

判据**只有一份,复用现有,不另写**:
  · 改价 / 改库存 / 改标题 = dispositions.maint_effective(维护链落定用的同一个现值比对,
    目标值取处置建议行的 detail.new;受管仓的库存看该节点的现值);
  · 删除 = product_events.GONE_SQL(删除核验同一个"已经不在了"口径);
  · 上架 / 跟卖 = 目录里出现了这个 SKU;停用 = 生命周期 RETIRED 或标了缺席。
判哪些:feed 结果不是 failed 的(failed = 沃尔玛已拒,没什么可判),且在回看窗口内。
"""

import logging

from services import dispositions, feed_track, product_events

logger = logging.getLogger("services.feed_effect")

EFFECTIVE, NOT_EFFECTIVE = "effective", "not_effective"
EFFECT_TEXT = {EFFECTIVE: "生效", NOT_EFFECTIVE: "未生效"}

#: 只判最近这么多天提交的明细(天)。实际结果回答的是"我最近做的操作生效了没有";
#: 最长的落定期限是删除 72 小时,观测按日来,一周足够把每条都判到。更老的不回溯
#: (首轮上线不把几个月的存量一次灌进复核清单)。
LOOKBACK_DAYS = 7

#: 判定暂停(2026-09-26,所有者复核):「回看 7 天」拿**今天的快照**去比一周前的提交,
#: 同一参数这期间可能又被改过好几次(下一轮维护、单品 PUT、订单扣库存),判不准;
#: 而判一次不回头改,判错的会永久留在表里。新口径定下来之前 catalog_sync 不调 judge,
#: 摘要只报这一行(docs/feed_ledger_v2_design.md「〇.8」)。清空即恢复。
PAUSED = "实际结果判定暂停中:「回看 7 天」口径判不准,等新口径(docs/feed_ledger_v2_design.md 〇.8)"

#: 本步单条 SQL 的超时(秒)。附属判定不许拖住日报链:2026-09-26 生产实见候选查询
#: 逐条全表扫 ops.dispositions 跑了 44 分钟不报错(当时库里没设超时)。超时 = 本步失败,
#: catalog_sync 照常往下走(_judge_effects 的失败隔离)。
STATEMENT_TIMEOUT_S = 300

#: 值比对的三类 feed(目标值在处置建议行里,维护链是它们唯一的提交方)。
_VALUE_FEEDS = ("price", "PRICE_AND_PROMOTION", "inventory", "MP_INVENTORY",
                "MP_MAINTENANCE")
_PRESENCE_FEEDS = ("MP_ITEM", "MP_ITEM_MATCH")
_JUDGED_FEEDS = _VALUE_FEEDS + _PRESENCE_FEEDS + ("RETIRE_ITEM", "DELETE_ITEM")

#: feed 结果里可判的状态:沃尔玛接受了(success)或没给结论 / 明细无此条。
#: failed 不判 —— 沃尔玛拒了,没什么可生效的。
_JUDGEABLE = ("success", "missing") + feed_track.NO_VERDICT_STATUSES

#: 候选 + 观测一次取齐,判词在 Python 里出(verdict,纯函数)。
#: ⚠ 每个参数带显式 ::类型(本仓 SQL 纪律:PG 推不出类型在生产上连炸过三次)。
#: ⚠ 目标值**整批一次 join**(disp CTE),不许写成逐候选的 LATERAL:2026-09-26 生产
#:   那版是 `LATERAL (… FROM ops.dispositions WHERE feed_id = … ORDER BY id DESC LIMIT 1)`,
#:   feed_id 上没有索引,1.88 万个候选每个都把 65.8 万行的处置表从尾到头扫一遍
#:   (删除 / 上架候选根本没有维护处置行,次次扫到底),44 分钟不出结果。现在只对
#:   值比对类候选取目标值,走 dispositions_feed_sku_idx。
_CANDIDATES_SQL = f"""
WITH due AS (
    SELECT f.feed_id, f.sku, f.store, f.feed_type, f.workflow,
           f.status AS feed_status, f.submitted_at,
           f.submitted_at + make_interval(
               mins => (%(deadlines)s::jsonb ->> f.feed_type)::int) AS due_at
    FROM ops.feed_items f
    WHERE f.status = ANY(%(judgeable)s::text[])
      AND f.feed_type = ANY(%(feed_types)s::text[])
      AND f.submitted_at > now() - make_interval(days => %(days)s::int)
      -- 已过落定期限(未到期的不进后面的判定,判也判不了)
      AND f.submitted_at + make_interval(
              mins => (%(deadlines)s::jsonb ->> f.feed_type)::int) <= now()
      AND (%(store)s::text IS NULL OR f.store = %(store)s::text)
      AND NOT EXISTS (SELECT 1 FROM ops.feed_effects e
                      WHERE e.feed_id = f.feed_id AND e.sku = f.sku)
      -- 被覆盖的不判:同一 (店, SKU, feedType) 之后又提交过
      AND NOT EXISTS (SELECT 1 FROM ops.feed_items n
                      WHERE n.store = f.store AND n.sku = f.sku
                        AND n.feed_type = f.feed_type
                        AND n.submitted_at > f.submitted_at)
),
scans AS (      -- 每店最近一次被扫描的时刻(没有扫描轮次表,取目录里最新的 last_seen_at)
    SELECT s.store, max(s.last_seen_at) AS scanned_at
    FROM catalog.walmart_items s
    WHERE s.store IN (SELECT DISTINCT store FROM due)
    GROUP BY s.store
),
disp AS (       -- 值比对类的目标值:同 (feed, SKU) 取最新一条维护处置行
    SELECT DISTINCT ON (x.feed_id, x.sku) x.feed_id, x.sku, x.action, x.detail
    FROM due d
    JOIN ops.dispositions x ON x.feed_id = d.feed_id AND x.sku = d.sku
    WHERE d.feed_type = ANY(%(value_feeds)s::text[])
      AND x.action = ANY(%(maint_actions)s::text[])
    ORDER BY x.feed_id, x.sku, x.id DESC
)
SELECT d.feed_id, d.sku, d.store, d.feed_type, d.workflow, d.feed_status,
       d.submitted_at, d.due_at, sc.scanned_at,
       (w.sku IS NOT NULL) AS seen, w.missing_since, w.lifecycle_status,
       w.last_seen_at, w.price, w.avail_qty, w.product_name,
       {product_events.GONE_SQL} AS gone,
       disp.action, disp.detail ->> 'new' AS want,
       disp.detail ->> 'ship_node' AS ship_node, nq.avail_qty AS node_qty
FROM due d
LEFT JOIN catalog.walmart_items w ON w.store = d.store AND w.sku = d.sku
LEFT JOIN scans sc ON sc.store = d.store
LEFT JOIN disp ON disp.feed_id = d.feed_id AND disp.sku = d.sku
LEFT JOIN catalog.item_node_inventory nq      -- 主键 (store, sku, ship_node):至多一行
       ON nq.store = d.store AND nq.sku = d.sku
      AND nq.ship_node = disp.detail ->> 'ship_node' AND nq.seen_at > d.due_at
"""

_COLS = ("feed_id", "sku", "store", "feed_type", "workflow", "feed_status",
         "submitted_at", "due_at", "scanned_at", "seen", "missing_since",
         "lifecycle_status", "last_seen_at", "price", "avail_qty", "product_name",
         "gone", "action", "want", "ship_node", "node_qty")

_INSERT_SQL = """
INSERT INTO ops.feed_effects (feed_id, sku, store, feed_type, workflow,
    feed_status, effect, want, observed, observed_at, basis)
VALUES (%(feed_id)s, %(sku)s, %(store)s, %(feed_type)s, %(workflow)s,
    %(feed_status)s, %(effect)s, %(want)s, %(observed)s, %(observed_at)s, %(basis)s)
ON CONFLICT (feed_id, sku) DO NOTHING
"""


def _after(ts, cut) -> bool:
    return ts is not None and cut is not None and ts > cut


def _shown(action: str, price, qty, name) -> str:
    """输入:动作 + 三个现值 → 输出:复核清单里给人看的现值(价格两位小数)。"""
    try:
        if action == "price":
            return f"{float(price):.2f}"
        if action == "inventory":
            return str(int(qty))
    except (TypeError, ValueError):
        return "无"         # 现值拿不到:maint_effective 同样判未生效
    return str(name or "")


def verdict(r: dict) -> tuple[str, str, str, object] | None:
    """输入:一行候选(_CANDIDATES_SQL 的列)→ 输出:(生效与否, 观测到的, 判据, 观测时刻)
    或 None(还判不了:期限后的观测还没来 / 目标值不在库里)。纯函数。

    未生效**只认期限之后的观测**(官方期限内没变不算没生效);生效只要提交后看到了。
    """
    ft = r["feed_type"]
    if ft in _VALUE_FEEDS:
        action = r.get("action")
        if not action:
            return None     # 没有处置建议行 = 目标值不在库里(反补等),判不了
        if not (r["seen"] and _after(r["last_seen_at"], r["due_at"])):
            return None     # 这个 SKU 期限后还没被重新观测过
        qty = r["avail_qty"]
        if r.get("ship_node"):
            if r.get("node_qty") is None:
                return None     # 受管仓:该节点期限后还没扫到,拿合计比就是错比
            qty = r["node_qty"]
        ok = dispositions.maint_effective(action, r.get("want"), r["price"], qty,
                                          r["product_name"])
        return (EFFECTIVE if ok else NOT_EFFECTIVE, _shown(action, r["price"], qty,
                                                           r["product_name"]),
                f"catalog_sync 现值比对({action})", r["last_seen_at"])
    if ft in _PRESENCE_FEEDS:
        if r["seen"] and r["missing_since"] is None and _after(
                r["last_seen_at"], r["submitted_at"]):
            return EFFECTIVE, "在架", "目录里出现了这个 SKU", r["last_seen_at"]
        if _after(r["scanned_at"], r["due_at"]) and (
                not r["seen"] or r["missing_since"] is not None):
            return (NOT_EFFECTIVE, "缺席", "期限后该店扫描过,目录里没有这个 SKU",
                    r["scanned_at"])
        return None
    if ft == "RETIRE_ITEM":
        if not r["seen"]:
            return None     # 从没观测到过的 SKU,停没停无从说起
        if r["lifecycle_status"] == "RETIRED" or r["missing_since"] is not None:
            return (EFFECTIVE, r["lifecycle_status"] or "缺席",
                    "目录里生命周期 RETIRED 或已缺席", r["last_seen_at"])
        if _after(r["last_seen_at"], r["due_at"]):
            return (NOT_EFFECTIVE, f"在架({r['lifecycle_status'] or '-'})",
                    "期限后仍在架且未 RETIRED", r["last_seen_at"])
        return None
    if ft == "DELETE_ITEM":
        if r["gone"]:
            return (EFFECTIVE, "已不在", "目录缺席 / 标了缺席 / RETIRED(删除核验同口径)",
                    r["missing_since"] or r["last_seen_at"] or r["scanned_at"])
        if _after(r["last_seen_at"], r["due_at"]):
            return NOT_EFFECTIVE, "仍在架", "期限后观测仍在架", r["last_seen_at"]
        return None
    return None


def judge(conn, store: str | None = None) -> dict:
    """输入:连接(+限定店铺)→ 输出:{effective, not_effective, review, waiting, no_target}。

    review = 本轮新判的「feed 成功 ∧ 未生效」条数(复核清单的新增);waiting = 已到期
    但期限后的观测还没来、本轮判不了的条数(下一轮 catalog_sync 再判);no_target =
    值比对类 feed 找不到处置建议行(目标值不在库里,如反补的 MP_MAINTENANCE),永远
    判不了,单独计数免得冒充"等观测"。
    调用方负责事务(空跑时 rollback,本函数不提交)。
    """
    with conn.cursor() as cur:
        # 本事务内的超时(SET LOCAL 随事务结束失效,不影响调用方连接上的其他步骤)
        cur.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_S * 1000)}")
        cur.execute(_CANDIDATES_SQL, {
            "deadlines": _deadlines_json(), "judgeable": list(_JUDGEABLE),
            "feed_types": list(_JUDGED_FEEDS), "days": int(LOOKBACK_DAYS),
            "store": store, "maint_actions": list(dispositions.MAINT_ACTIONS),
            "value_feeds": list(_VALUE_FEEDS)})
        rows = [dict(zip(_COLS, r)) for r in cur.fetchall()]
    out = {EFFECTIVE: 0, NOT_EFFECTIVE: 0, "review": 0, "waiting": 0, "no_target": 0}
    params = []
    for r in rows:
        if r["feed_type"] in _VALUE_FEEDS and not r.get("action"):
            out["no_target"] += 1
            continue
        v = verdict(r)
        if v is None:
            out["waiting"] += 1
            continue
        effect, observed, basis, observed_at = v
        out[effect] += 1
        if effect == NOT_EFFECTIVE and r["feed_status"] == "success":
            out["review"] += 1
        params.append({"feed_id": r["feed_id"], "sku": r["sku"], "store": r["store"],
                       "feed_type": r["feed_type"], "workflow": r["workflow"],
                       "feed_status": r["feed_status"], "effect": effect,
                       "want": r.get("want") if r.get("action") else None,
                       "observed": (observed or "")[:500], "observed_at": observed_at,
                       "basis": basis})
    if params:
        with conn.cursor() as cur:
            cur.executemany(_INSERT_SQL, params)
    return out


def _deadlines_json() -> str:
    import json
    return json.dumps(feed_track.FEED_DEADLINE_MINUTES)


_REVIEW_SQL = """
SELECT e.store, e.feed_type, e.workflow, e.feed_id, e.sku, e.feed_status,
       e.want, e.observed, e.observed_at, e.basis, e.judged_at
FROM ops.feed_effects e
WHERE e.effect = 'not_effective'
  AND e.feed_status = ANY(%(statuses)s::text[])
  AND e.judged_at > now() - make_interval(days => %(days)s::int)
  AND (%(store)s::text IS NULL OR e.store = %(store)s::text)
ORDER BY e.store, e.feed_type, e.judged_at DESC, e.sku
"""


def review_rows(conn, days: int = 7, store: str | None = None) -> list[dict]:
    """输入:连接 + 回看天数(+店铺)→ 输出:复核清单行(未生效,且 feed 成功或没给结论)。只读。

    「feed 成功 ∧ 未生效」是所有者点名要人看的那一档;「没给结论(超期未完成 / 未知
    状态 / 无法查询)∧ 未生效」一并列出 —— 同样是"沃尔玛没说成、线上也没变"。
    """
    with conn.cursor() as cur:
        cur.execute(_REVIEW_SQL, {
            "statuses": ["success", *feed_track.NO_VERDICT_STATUSES],
            "days": int(days), "store": store})
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
