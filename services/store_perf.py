"""店铺经营水平:在售天数归一的三个指标 + 配额输入(分配引擎 A2 的店铺侧)。

为什么单独一个模块:`alloc_survey` 管的是**存量勘察**(谁和谁撞了、哪些货
该走),这里管的是**每家店经营得怎么样**(该给它多少货)。两件事的消费方
不同、口径来源不同(那边吃 walmart_items,这边吃 store_kpi_daily + 订单),
混在一个模块里迟早互相牵连。

**核心口径:分母一律是「在售天数」,不是日历天数**(所有者定稿 2026-08-15
晚,推翻了设计稿原先写死的"固定除以 30")。三个月里店铺暂停过,那段时间
根本不存在销售额;拿日历天数当分母会把"停过业"读成"卖得差",而且后果是
反的 —— 缺口被高估 ⇒ 配额被放大 ⇒ **停过业的店反而被灌最多货**。

三条**必须一起报出来**的诚实边界:
1. **没有 KPI 记录的天既不算 active 也不算停用**——那是"没抓到",不是
   "没营业"。所以 `active` 只从有记录的天里数,同时给出 `cover`(覆盖率),
   低覆盖店的比值全靠收缩顶上去,不代表它真实水平;
2. **`active < 14` 一律取全店中位数**(所有者:少于 14 天就不信它的数据)。
   这是硬切不是收缩——那个区间一单大额就能把单品日产出抬十倍,而收缩公式
   在 active=2 时仍会留 12.5% 权重给它。新店 active=0 落在同一条规则里,
   天然冷启动;
3. **缺口用实测日均,效率用收缩后的值**(所有者拍板 2026-08-15 晚,两条决策
   各管各的量):薄样本的**比值**不可信(一单大额抬十倍)所以货位值要收缩;
   但**缺口**是"离目标多远",一家一件没卖的空店确实最远,拿中位数替换会让它
   看起来是平均水平、排在真卖得动的店后面——方向就反了。
4. **销量一律净额**(GMV − 退款)。⚠ 历史期算不出退款(`order_history_import`
   只导销售六列),所以 `hist_days` 也要报出来:窗口大量落在历史期的店,
   它的"净额"其实是毛额,与 API 期的店**不是同一个口径**,不能直接比。
"""

import logging
import statistics

from services import store_targets

logger = logging.getLogger("services.store_perf")

#: 少于这么多在售天数就不信它的数据,一律取全店中位数(所有者定稿 08-15)
MIN_ACTIVE_DAYS = 14
#: 收缩强度:active >= MIN_ACTIVE_DAYS 之后 w = active/(active+SHRINK_D0)
SHRINK_D0 = 14

# 在售天数 / 覆盖率 / 在线数均值:KPI 表逐店逐日一行(PK (store, data_date))
# ⚠ active 只数**有记录且 ACTIVE** 的天;`store_status` 为空按 ACTIVE 算
#   (全仓 fail-open,与 services/kpi.store_activity 同向)
_SQL_KPI = """
SELECT store,
       count(*)                                                    AS rec_days,
       count(*) FILTER (WHERE coalesce(upper(store_status), 'ACTIVE') = 'ACTIVE')
                                                                   AS active_days,
       avg(items_online) FILTER (WHERE coalesce(upper(store_status), 'ACTIVE') = 'ACTIVE'
                                   AND items_online IS NOT NULL)   AS avg_online
FROM ops.store_kpi_daily
WHERE data_date >= (%(as_of)s::timestamptz - make_interval(days => %(days)s))::date
  AND data_date <  %(as_of)s::timestamptz::date
GROUP BY store
"""

# 净销售额:毛额 − 退款。退款按 order_line_id 回连 return_lines
# ⚠ 一条订单行可能有多次售后(PK 是 (return_order_id, order_line_id)),
#   所以退款要先按 order_line_id 聚合再连,裸 JOIN 会把订单行乘出多份
_SQL_SALES = """
WITH o AS (
    SELECT store, order_line_id, product_amount, source
    FROM orders.order_lines
    WHERE order_date >= %(as_of)s::timestamptz - make_interval(days => %(days)s)
      AND order_date <  %(as_of)s::timestamptz
      AND coalesce(sale_status, '') <> 'Cancelled'
), r AS (
    SELECT order_line_id, sum(coalesce(refund_total, 0)) AS refund
    FROM orders.return_lines
    WHERE order_line_id IN (SELECT order_line_id FROM o)
    GROUP BY order_line_id
)
SELECT o.store,
       count(*)                                        AS orders,
       coalesce(sum(o.product_amount), 0)::numeric      AS gross,
       coalesce(sum(r.refund), 0)::numeric              AS refund,
       count(*) FILTER (WHERE o.source IS NOT NULL)     AS hist_rows
FROM o LEFT JOIN r ON r.order_line_id = o.order_line_id
GROUP BY o.store
"""


def load(conn, win: dict) -> dict:
    """输入:连接 + `alloc_survey.sales_window()` 产物 → 输出:{店铺: 原始计量}。

    只取数不判断:KPI 侧(在售天数/覆盖率/在线数均值)与订单侧(单量/毛额/
    退款/历史期行数)各查一次,按店合并。判定与收缩在 `derive()` 里,
    好测也好改口径。
    """
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute(_SQL_KPI, win)
        for store, rec, active, avg_online in cur.fetchall():
            out.setdefault(store, {}).update(
                rec_days=int(rec or 0), active_days=int(active or 0),
                avg_online=float(avg_online) if avg_online is not None else None)
        cur.execute(_SQL_SALES, win)
        for store, orders, gross, refund, hist in cur.fetchall():
            out.setdefault(store, {}).update(
                orders=int(orders or 0), gross=float(gross or 0.0),
                refund=float(refund or 0.0), hist_rows=int(hist or 0))
    for row in out.values():
        row.setdefault("rec_days", 0)
        row.setdefault("active_days", 0)
        row.setdefault("avg_online", None)
        row.setdefault("orders", 0)
        row.setdefault("gross", 0.0)
        row.setdefault("refund", 0.0)
        row.setdefault("hist_rows", 0)
        row["net"] = row["gross"] - row["refund"]
    return out


def _median(vals: list[float]) -> float:
    return statistics.median(vals) if vals else 0.0


def derive(raw: dict, win_days: int) -> dict:
    """输入:`load()` 产物 + 窗口天数 → 输出:{店铺: 指标(含收缩后的值)}。

    三个指标都以 `active_days` 为分母(见模块 docstring)。样本不足的店
    整体让位给中位数,**并且 `trusted=False` 标出来** —— 报告里必须看得见
    哪些数是它自己的、哪些是中位数顶上来的,否则一屏数字里没人分得清。
    """
    trusted = {s: r for s, r in raw.items() if r["active_days"] >= MIN_ACTIVE_DAYS}

    def _rate(r):
        a = r["active_days"]
        return {"daily_net": r["net"] / a, "daily_orders": r["orders"] / a,
                "slot_value": (r["net"] / a / r["avg_online"]
                               if r["avg_online"] else None)} if a else {}

    rates = {s: _rate(r) for s, r in trusted.items()}
    med = {k: _median([v[k] for v in rates.values() if v.get(k) is not None])
           for k in ("daily_net", "daily_orders", "slot_value")}

    out: dict[str, dict] = {}
    for store, r in raw.items():
        a = r["active_days"]
        m = dict(r)
        # **实测日均净额:不收缩、不替换**(所有者拍板 2026-08-15 晚)。
        # 缺口用它,效率用下面收缩后的那套 —— 两条决策各管各的量:
        #   「少于 14 天不信它的数据」管的是**比值**(货位值/效率),薄样本
        #     一单大额就能抬十倍,确实不可信;
        #   「把货给离目标最远的店」管的是**缺口**,而一家一件没卖的空店
        #     确实离目标最远。拿中位数替换会让它看起来是"平均水平",
        #     排在真卖得动的店后面 —— 方向反了。
        # active=0 且真的零销量 ⇒ 日均就是 0(缺口 100%),不是"算不出";
        # active=0 却有销量 ⇒ 数据自相矛盾(卖了但从没记为在营),**不猜**。
        m["daily_net_own"] = (r["net"] / a if a else
                              (0.0 if r["net"] == 0 else None))
        m["cover"] = r["rec_days"] / win_days if win_days else 0.0
        m["hist_share"] = (r["hist_rows"] / r["orders"]) if r["orders"] else 0.0
        m["trusted"] = a >= MIN_ACTIVE_DAYS
        if not m["trusted"]:
            # 硬切:少于 14 天的数据一点都不信,三个指标全取中位数
            m.update(med)
            m["basis"] = f"中位数(在售仅 {a} 天)"
        else:
            w = a / (a + SHRINK_D0)
            own = rates[store]
            for k in ("daily_net", "daily_orders", "slot_value"):
                v = own.get(k)
                m[k] = med[k] if v is None else w * v + (1 - w) * med[k]
            m["basis"] = f"自身 {w:.0%} + 中位数 {1 - w:.0%}"
        out[store] = m
    return out


def quota_inputs(metrics: dict, cfg: dict, online_now: dict,
                 pending_delist: dict | None = None) -> dict:
    """输入:`derive()` 产物 + 限额表 + 当前在线数(+ 待下架数)
    → 输出:{店铺: {gap, room, room_now, eff, participates}}。

    `room` **预支待下架名额**(所有者是一边下架一边上架的):不预支的话,
    一家马上要空出 300 个货位的店会被算成"满了",这一批一件都分不进去。
    ⚠ 预支只影响配额;`list_new` 真上架时的容量闸必须用 `room_now`
    (当前真实在线数),否则下架没做完就上架会真的超 `单店最大在线数`。
    """
    pending = pending_delist or {}
    slot_med = _median([m["slot_value"] for m in metrics.values()
                        if m.get("slot_value")])
    out: dict[str, dict] = {}
    for store, c in cfg.items():
        m = metrics.get(store) or {}
        target = c.get("gmv")
        # 缺口一律用**实测**日均(见 derive 里那段);收缩后的值只喂效率项
        daily = m.get("daily_net_own")
        gap = None
        if target and target > 0 and daily is not None:
            gap = min(1.0, max(0.0, (target - daily) / target))
        cap, now = c.get("max_online"), int(online_now.get(store, 0))
        room_now = max(0, int(cap) - now) if cap is not None else None
        room = (max(0, int(cap) - (now - int(pending.get(store, 0))))
                if cap is not None else None)
        sv = m.get("slot_value")
        out[store] = {
            "gap": gap, "room": room, "room_now": room_now,
            "pending_delist": int(pending.get(store, 0)),
            "eff": (min(2.0, sv / slot_med) if sv and slot_med else None),
            "participates": store_targets.accepts_allocation(c),
        }
    return out
