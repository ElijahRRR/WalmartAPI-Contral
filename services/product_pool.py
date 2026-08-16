"""候选池:打分所需的全部取数,一处定义。

`alloc_products`(体检)与 `alloc_plan`(真分配)必须看到**逐字相同**的候选池
与逐字相同的分数 —— 体检说"可分配 27 万",分配实际只认 19 万,那份体检就是
假的。两边各写一遍 SQL 是这个项目反复栽跟头的地方(报告与回填的行口径、
两套 `_SQL_ONLINE`、重复的 `MAX_LEAD_DAYS`),所以取数与打分都只留这一份。

**只取数与打分,不做业务判断**:占用怎么排除、批量怎么切、分给谁,
全在调用方(见三条铁律第二条的同理:这里越界就会出现两套分配口径)。
"""

import logging

from services import amz_source, product_score as ps

logger = logging.getLogger("services.product_pool")

# 口径与 amz_source._SQL 同源(LATERAL 取最近一次采集,zip_verify='mismatch'
# 的观测不算);评分/评论从 raw 取 —— 契约字段表没登记,但 2026-08-15 P2
# 探针实测命中率 100%。
# `fulfillment` 就是渠道闸的产品侧(raw->>'is_fba'),**必须走这条 LATERAL**:
# 裸 JOIN latest_snapshot 会因 scrape_params 分组让一个 ASIN 出多行。
_SQL_POOL = """
SELECT p.asin, p.brand, p.slow ->> 'manufacturer', p.walmart_pt, r.category,
       s.price, s.shipping, s.stock_count, s.stock_state, s.delivery_days,
       s.raw ->> 'rating'        AS rating,
       s.raw ->> 'review_count'  AS reviews,
       s.raw ->> 'is_fba'        AS fulfillment
FROM catalog.products p
JOIN catalog.risk_product_types r ON r.product_type = p.walmart_pt
LEFT JOIN LATERAL (
    SELECT price, shipping, stock_count, stock_state, delivery_days, raw
    FROM catalog.latest_snapshot ls
    WHERE ls.marketplace = p.marketplace AND ls.asin = p.asin
      AND coalesce(ls.scrape_params ->> 'zip_verify', '') <> 'mismatch'
    ORDER BY ls.scraped_at DESC LIMIT 1
) s ON true
WHERE p.marketplace = 'US'
  AND p.audit_status = 'approved'
  AND p.title IS NOT NULL AND btrim(p.title) <> ''
  AND p.walmart_pt IS NOT NULL AND p.walmart_pt <> 'unknown'
  AND btrim(coalesce(r.category, '')) <> ''
"""

# 窗口内销量与销售额:**按 asin 聚合**(A1.5 补的列,99.2% 行有值)。
# asin IS NULL 的行进不了这个维度 —— 它们只在店×SKU 维度起作用。
# ⚠ 金额是**毛额**(未扣退款):逐产品的退款只有 API 期算得出(§7.4e),
# 拿它当净额会与店铺侧的净额口径混淆。报告里必须标明"毛额"
_SQL_SALES = """
SELECT asin, sum(coalesce(qty, 0))::bigint AS units,
       sum(coalesce(product_amount, 0))::numeric AS gross
FROM orders.order_lines
WHERE asin IS NOT NULL
  AND order_date >= %(as_of)s::timestamptz - make_interval(days => %(days)s)
  AND order_date <  %(as_of)s::timestamptz
  AND coalesce(sale_status, '') <> 'Cancelled'
GROUP BY asin
"""

# 全历史销量/销售额(**不进打分**,只给报告看)。两年订单已入库,其中
# `source='历史数据'` 那批只有下单时间/店铺/PO/SKU/品名/数量/金额 —— 数量与
# 金额是有的,所以这个维度对历史期同样成立(退款不成立,见上)
_SQL_SALES_ALL = """
SELECT asin, sum(coalesce(qty, 0))::bigint AS units,
       sum(coalesce(product_amount, 0))::numeric AS gross,
       min(order_date)::date AS first_sold, max(order_date)::date AS last_sold
FROM orders.order_lines
WHERE asin IS NOT NULL AND coalesce(sale_status, '') <> 'Cancelled'
GROUP BY asin
"""

# 退货率的分母只能是**同期 API 行**:历史导入行没有退款数据,拿它们当分母
# 会把退货率系统性稀释(§7.4e)。所以这里显式只算 source IS NULL 的行
_SQL_REFUND = """
WITH o AS (
    SELECT asin, order_line_id, coalesce(qty, 0) AS qty
    FROM orders.order_lines
    WHERE asin IS NOT NULL AND source IS NULL
      AND order_date >= %(as_of)s::timestamptz - make_interval(days => %(days)s)
      AND order_date <  %(as_of)s::timestamptz
      AND coalesce(sale_status, '') <> 'Cancelled'
), r AS (
    SELECT order_line_id, sum(coalesce(refunded_qty, 0)) AS rq
    FROM orders.return_lines
    WHERE order_line_id IN (SELECT order_line_id FROM o)
    GROUP BY order_line_id
)
SELECT o.asin, sum(o.qty)::bigint AS sold,
       sum(coalesce(r.rq, 0))::bigint AS returned
FROM o LEFT JOIN r ON r.order_line_id = o.order_line_id
GROUP BY o.asin
"""

# ⚠ `missing_times` 必须一起查:`risk_penalty` 要用它判「消失够不够 3 次」,
# 漏这一列等于该项永不扣分(0 >= 3 恒假),而且**不会报错**
_SQL_RISK = """
SELECT asin, delete_times, unexplained_missing, missing_times, audit_reject_times
FROM catalog.product_risk
WHERE delete_times > 0 OR unexplained_missing OR audit_reject_times > 0
"""


def load(conn, win: dict) -> dict:
    """输入:连接 + 销量窗口 → 输出:{pool, sales, refund, risk, risk_err}。

    `product_risk` 读不到只降级(黑历史罚分全为 0)并把错误回传 —— 视图缺了
    不该拖垮整条分配链,但**必须让人看见**,否则"这批怎么没人扣分"查不出来。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_POOL)
        pool = cur.fetchall()
        cur.execute(_SQL_SALES, win)
        rows = cur.fetchall()
        sales = {a: int(u) for a, u, _g in rows}
        gross = {a: float(g or 0) for a, _u, g in rows}
        cur.execute(_SQL_REFUND, win)
        refund = {a: (int(s), int(r)) for a, s, r in cur.fetchall()}
        try:
            cur.execute(_SQL_RISK)
            risk = {a: {"delete_times": d, "unexplained_missing": um,
                        "missing_times": mt, "audit_reject_times": ar}
                    for a, d, um, mt, ar in cur.fetchall()}
        except Exception as e:                  # noqa: BLE001
            conn.rollback()
            risk, risk_err = {}, str(e).strip().splitlines()[0]
        else:
            risk_err = None
    return {"pool": pool, "sales": sales, "gross": gross, "refund": refund,
            "risk": risk, "risk_err": risk_err}


def lifetime_sales(conn) -> dict:
    """输入:连接 → 输出:{asin: {units, gross, first_sold, last_sold}}(全历史)。

    **不进打分**,只给报告看 —— 分数用的是窗口内销量(§7.6),拿全历史当信号
    会让一个三年前卖得好、现在没人要的品一直高分。单独一个函数就是为了让
    "它不参与打分"这件事在调用处也看得见。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_SALES_ALL)
        return {a: {"units": int(u), "gross": float(g or 0),
                    "first_sold": f, "last_sold": l}
                for a, u, g, f, l in cur.fetchall()}


def score_all(data: dict) -> tuple[list, dict]:
    """输入:`load()` 产物 → 输出:(打过分的候选 list, 硬闸淘汰计数)。

    每个元素:{asin, brand, manufacturer, pt, category, channel, score,
              base, bonus, penalty, why, missing, sales, rating, reviews, lead}
    `channel` 已归一到 FBA/FBM;认不出的值归 None(**不猜**,由调用方决定
    是丢是留 —— 猜错等于把 FBM 的货分给 FBA 店)。
    """
    from services import store_targets           # 只为 CHANNELS 白名单,避免循环
    out, gated = [], {}
    for (asin, brand, manuf, pt, cat, price, shipping, stock, stock_state,
         lead, rating, reviews, ful) in data["pool"]:
        row = {"price": float(price) if price is not None else None,
               "shipping": float(shipping) if shipping is not None else None,
               "stock": stock, "stock_state": stock_state}
        why = ps.gate(row, amz_source.MIN_INVENTORY, amz_source.IN_STOCK_QTY)
        if why:
            k = why.split("(")[0]
            gated[k] = gated.get(k, 0) + 1
            continue
        sold, ret = data["refund"].get(asin, (0, 0))
        r = ps.score({"sales": data["sales"].get(asin), "rating": rating,
                      "reviews": reviews, "lead": lead,
                      "refund": (ret / sold if sold else None)},
                     data["risk"].get(asin))
        ch = (ful or "").strip().upper() or None
        out.append({"asin": asin, "brand": brand, "manufacturer": manuf,
                    "pt": pt, "category": cat,
                    "price": row["price"], "shipping": row["shipping"],
                    "gross": data.get("gross", {}).get(asin),
                    "channel": ch if ch in store_targets.CHANNELS else None,
                    "score": r["score"], "base": r["base"], "bonus": r["bonus"],
                    "penalty": r["penalty"], "why": r["why"],
                    "missing": r["missing"], "sales": data["sales"].get(asin),
                    "rating": rating, "reviews": reviews, "lead": lead})
    return out, gated
