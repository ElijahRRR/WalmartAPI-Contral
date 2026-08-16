"""alloc_products — 产品分体检(只读)。分配引擎动工前先看这张表。

用法:
  python cli.py alloc_products                     # 近 90 天销量窗口
  python cli.py alloc_products -p days=180
  python cli.py alloc_products -p as_of=2026-08-15 # 钉住窗口右端
  python cli.py alloc_products -p export=0         # 只看摘要不落 csv

与 `alloc_stores` 同一路数:**引擎的产品侧决策全建在这套分数上,先摊开
给人看一眼**。这份报告要回答三个问题:

  ① **漏斗**:候选池逐层收窄到多少,每层被什么闸掉的;
  ② **信号覆盖率**:五个信号各有多少产品拿得到 —— 这是最要紧的一栏。
     设计稿给了权重,但**权重再合理,信号采不到就是空的**;某个信号覆盖率
     很低时,它的权重会被摊回其余信号,分数实际由剩下几项决定,
     那和设计意图可能差很远,得先看见才能调;
  ③ **分数分布**:淘汰线(默认 40)切下去还剩多少可分的货。

**只读**:不写任何表、不调沃尔玛、不调 LLM。
"""

import csv
import logging
from collections import Counter

from registry import db, paths
from services import alloc_survey as sv
from services import amz_source, product_score as ps
from services import textfmt

DANGEROUS = False

logger = logging.getLogger("workflows.alloc_products")

# 候选池 + 最新快照。口径与 amz_source._SQL 同源(LATERAL 取最近一次采集,
# zip_verify='mismatch' 的观测不算);评分/评论从 raw 取 —— 契约字段表没登记,
# 但 2026-08-15 P2 探针实测命中率 100%(§四)
_SQL_POOL = """
SELECT p.asin, p.brand, p.walmart_pt, r.category,
       s.price, s.shipping, s.stock_count, s.stock_state, s.delivery_days,
       s.raw ->> 'rating'        AS rating,
       s.raw ->> 'review_count'  AS reviews
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

# 窗口内销量:**按 asin 聚合**(A1.5 补的列,99.2% 行有值)。
# asin IS NULL 的行进不了这个维度 —— 它们只在店×SKU 维度起作用
_SQL_SALES = """
SELECT asin, sum(coalesce(qty, 0))::bigint AS units
FROM orders.order_lines
WHERE asin IS NOT NULL
  AND order_date >= %(as_of)s::timestamptz - make_interval(days => %(days)s)
  AND order_date <  %(as_of)s::timestamptz
  AND coalesce(sale_status, '') <> 'Cancelled'
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

_SQL_RISK = """
SELECT asin, delete_times, unexplained_missing, audit_reject_times
FROM catalog.product_risk
WHERE delete_times > 0 OR unexplained_missing OR audit_reject_times > 0
"""


def _pct(n, d):
    return f"{n / d:.1%}" if d else "—"


def run(params: dict) -> str:
    """输入:params(days/as_of/export)→ 输出:产品分体检报告。"""
    days = int(params.get("days", 90))
    win = sv.sales_window(str(params.get("as_of", "")), days)
    export = str(params.get("export", "1")).lower() not in {"0", "false", "no"}

    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_POOL)
        pool = cur.fetchall()
        cur.execute(_SQL_SALES, win)
        sales = {a: int(u) for a, u in cur.fetchall()}
        cur.execute(_SQL_REFUND, win)
        refund = {a: (int(sold), int(ret)) for a, sold, ret in cur.fetchall()}
        try:
            cur.execute(_SQL_RISK)
            risk = {a: {"delete_times": d, "unexplained_missing": um,
                        "audit_reject_times": ar}
                    for a, d, um, ar in cur.fetchall()}
        except Exception as e:                  # noqa: BLE001 视图缺了不该拖垮体检
            conn.rollback()
            risk, risk_err = {}, str(e).strip().splitlines()[0]
        else:
            risk_err = None

    gated: Counter = Counter()
    have: Counter = Counter()
    scores: list = []
    rows: list = []
    for (asin, brand, pt, cat, price, shipping, stock, stock_state, lead,
         rating, reviews) in pool:
        row = {"price": float(price) if price is not None else None,
               "shipping": float(shipping) if shipping is not None else None,
               "stock": stock, "stock_state": stock_state}
        why = ps.gate(row, amz_source.MIN_INVENTORY, amz_source.IN_STOCK_QTY)
        if why:
            gated[why.split("(")[0]] += 1
            continue
        sold, ret = refund.get(asin, (0, 0))
        sig = {"sales": sales.get(asin), "rating": rating, "reviews": reviews,
               "lead": lead, "refund": (ret / sold if sold else None)}
        r = ps.score(sig, risk.get(asin))
        for k in ps.WEIGHTS:
            if k not in r["missing"]:
                have[k] += 1
        if r["score"] is None:
            gated["信号全缺(不判分)"] += 1
            continue
        scores.append(r["score"])
        rows.append((asin, brand or "", cat, round(r["score"], 1),
                     sales.get(asin), rating, reviews, lead,
                     round(r["penalty"], 1), r["penalty_why"],
                     "|".join(r["missing"])))

    n_pool, n_scored = len(pool), len(scores)
    passed = [s for s in scores if s >= ps.CUTOFF]
    L = ["", "═══ 产品分体检 ═══", "",
         f"▍漏斗(候选池口径:approved ∧ 有标题 ∧ PT 有效 ∧ 大类查得到)"]
    L += textfmt.table(
        ["", "条数", "占比"],
        [["候选池", f"{n_pool:,}", "100%"],
         # 「未进入打分」= 硬闸淘汰 + 信号全缺。后者**不是闸**,是"我们对这个品
         # 一无所知",归到"淘汰"里会让人以为它有什么毛病 —— 下面的原因分解拆开说
         ["未进入打分", f"{n_pool - n_scored:,}", _pct(n_pool - n_scored, n_pool)],
         ["有分可判", f"{n_scored:,}", _pct(n_scored, n_pool)],
         [f"≥ 淘汰线 {ps.CUTOFF:.0f}", f"{len(passed):,}", _pct(len(passed), n_pool)]],
        align="<>>")
    if gated:
        L.append("  其中:" + " · ".join(f"{k} {v:,}"
                                            for k, v in gated.most_common()))

    L += ["", "▍信号覆盖率(**权重再合理,信号采不到就是空的**)"]
    L += textfmt.table(
        ["信号", "设计权重", "有值", "覆盖率", ""],
        [[ps.LABELS[k], f"{ps.WEIGHTS[k]:.0%}", f"{have[k]:,}",
          _pct(have[k], n_scored),
          "⚠ 覆盖太低,它的权重实际被摊给了别的信号"
          if n_scored and have[k] / n_scored < 0.5 else ""]
         for k in sorted(ps.WEIGHTS, key=lambda x: -ps.WEIGHTS[x])],
        align="<>>><")

    if scores:
        scores.sort()
        q = [scores[int(len(scores) * p)] for p in (0.1, 0.25, 0.5, 0.75, 0.9)]
        L += ["", "▍分数分布"]
        L += textfmt.table(
            ["P10", "P25", "中位", "P75", "P90"],
            [[f"{v:.1f}" for v in q]], align=">>>>>")
        L.append(f"  淘汰线 {ps.CUTOFF:.0f} 切下去,可分配 {len(passed):,} 个"
                 f"({_pct(len(passed), n_scored)} 的可判分产品)")
    if risk_err:
        L.append(f"  ⚠ product_risk 读不到({risk_err}):黑历史罚分本轮全为 0")

    if not export:
        L += ["", "(-p export=0:未落 csv)"]
        return "\n".join(L)

    paths.reports_dir().mkdir(parents=True, exist_ok=True)
    p = paths.reports_dir() / "alloc_产品分.csv"
    rows.sort(key=lambda r: -r[3])
    with p.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["ASIN", "品牌", "大类", "产品分", f"近{days}天销量(件)",
                    "评分", "评论数", "配送天数", "罚分", "罚分原因", "缺失信号"])
        w.writerows(rows)
    L += ["", f"▍明细 → {p}(按产品分降序,{len(rows):,} 行)",
          "  「缺失信号」列告诉你这一行的分是靠哪几项算出来的 ——"
          "分数说不清来源就没法推翻它"]
    return "\n".join(L)
