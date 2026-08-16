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
from services import product_pool as pool_svc, product_score as ps
from services import textfmt

DANGEROUS = False

logger = logging.getLogger("workflows.alloc_products")

# ⚠ 候选池 SQL、打分、硬闸计数全在 `services/product_pool` —— 体检与真分配
# (`alloc_plan`)必须看到**逐字相同**的池子和分数。两边各写一遍的话,体检说
# "可分配 27 万"、分配实际只认 19 万,这份体检就是假的。
_SQL_POOL = pool_svc._SQL_POOL          # 保留别名:回归按名字盯着这几段口径
_SQL_SALES = pool_svc._SQL_SALES
_SQL_REFUND = pool_svc._SQL_REFUND
_SQL_RISK = pool_svc._SQL_RISK


def _pct(n, d):
    return f"{n / d:.1%}" if d else "—"


def run(params: dict) -> str:
    """输入:params(days/as_of/export)→ 输出:产品分体检报告。"""
    days = int(params.get("days", 90))
    win = sv.sales_window(str(params.get("as_of", "")), days)
    export = str(params.get("export", "1")).lower() not in {"0", "false", "no"}

    with db.pg_conn() as conn:
        data = pool_svc.load(conn, win)
    scored, gated_raw = pool_svc.score_all(data)
    risk_err = data["risk_err"]

    gated: Counter = Counter(gated_raw)
    have: Counter = Counter()
    scores: list = []
    rows: list = []
    for c in scored:
        # ⚠ 覆盖率的分母是「有分可判」,所以计数必须在剔除之后 ——
        # 放在前面会把没进打分的品也算进分子,覆盖率能超过 100%
        for k in ("rating", "reviews", "sales"):
            if k not in c["missing"]:
                have[k] += 1
        if c["lead"] is not None:
            have["lead"] += 1
        if data["refund"].get(c["asin"], (0, 0))[0]:
            have["refund"] += 1
        scores.append(c["score"])
        rows.append((c["asin"], c["brand"] or "", c["category"],
                     round(c["score"], 1), round(c["base"], 1),
                     round(c["bonus"], 1), round(c["penalty"], 1), c["why"],
                     c["sales"], c["rating"], c["reviews"], c["lead"],
                     "|".join(c["missing"])))

    n_pool, n_scored = len(data["pool"]), len(scores)
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
        ["信号", "作用", "有值", "覆盖率", ""],
        [[ps.LABELS["rating"], f"口碑基础分 {ps.WEIGHTS['rating']:.0%}",
          f"{have['rating']:,}", _pct(have["rating"], n_scored), ""],
         [ps.LABELS["reviews"], f"口碑基础分 {ps.WEIGHTS['reviews']:.0%}",
          f"{have['reviews']:,}", _pct(have["reviews"], n_scored), ""],
         ["历史销量", f"加分 最多 +{ps.SALES_BONUS_MAX:.0f}", f"{have['sales']:,}",
          _pct(have["sales"], n_scored),
          "⚠ 有订单史的品极少 —— 加分项几乎只在这批上生效"
          if n_scored and have["sales"] / n_scored < 0.5 else ""],
         ["配送时效", f"罚分 最多 −{ps.LEAD_PENALTY_MAX:.0f}", f"{have['lead']:,}",
          _pct(have["lead"], n_scored), ""],
         ["退货率", f"罚分 最多 −{ps.REFUND_PENALTY_MAX:.0f}", f"{have['refund']:,}",
          _pct(have["refund"], n_scored),
          "⚠ 只有 API 期算得出,历史期一律不罚"
          if n_scored and have["refund"] / n_scored < 0.5 else ""]],
        align="<<>><")
    L.append("  口碑缺项**按 0 分算**(走到打分这步的品一定有快照,"
             "「没有评分」是真实观测不是采集缺口);"
             "加分/罚分项没数据则 +0/−0,既不拉高也不拉低")

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
        w.writerow(["ASIN", "品牌", "大类", "产品分", "口碑分", "销量加分",
                    "罚分", "罚分原因", f"近{days}天销量(件)",
                    "评分", "评论数", "配送天数", "缺失信号"])
        w.writerows(rows)
    L += ["", f"▍明细 → {p}(按产品分降序,{len(rows):,} 行)",
          "  「缺失信号」列告诉你这一行的分是靠哪几项算出来的 ——"
          "分数说不清来源就没法推翻它"]
    return "\n".join(L)
