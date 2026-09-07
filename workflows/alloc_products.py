"""alloc_products — 产品分体检(只读)。分配引擎动工前先看这张表。

用法:
  python cli.py alloc_products                          # 销量窗口默认近一年
  python cli.py alloc_products -p sales_days=90         # 换窗口
  python cli.py alloc_products -p as_of=2026-08-15      # 钉住窗口右端
  python cli.py alloc_products -p export=0         # 只看摘要不落 csv

与 `alloc_stores` 同一路数:**引擎的产品侧决策全建在这套分数上,先摊开
给人看一眼**。这份报告要回答三个问题:

  ① **漏斗**:候选池逐层收窄到多少,每层被什么闸掉的;
  ② **信号覆盖率**:五个信号各有多少产品拿得到 —— 这是最要紧的一栏。
     设计稿给了权重,但**权重再合理,信号采不到就是空的**;某个信号覆盖率
     很低时,它的权重会被摊回其余信号,分数实际由剩下几项决定,
     那和设计意图可能差很远,得先看见才能调;
  ③ **分数分布**:淘汰线(默认 40)切下去还剩多少可分的货。

明细 csv 除了逐段分数,还带**配送方式、售价/运费/落地价、窗口内与全历史的
销量与销售额、占用店与在线店**(所有者 2026-08-16 要的)。三条读表纪律:
  · **销售额一律毛额**(未扣退款)—— 逐产品的退款只有 API 期算得出,
    与店铺侧的净额**不是同一口径**,两张表不能对账;
  · **历史销量不进打分** —— 分数只看窗口内(§7.6),否则一个三年前卖爆、
    现在没人要的品会一直挂高分;
  · **占用店与在线店是两列** —— 前者是台账里的决策(无自动释放),后者是货
    此刻实际挂在谁那儿。不一致本身就是信息,合成一列会把它抹掉。

**只读**:不写任何表、不调沃尔玛、不调 LLM。
"""

import logging
from collections import Counter

from registry import db, resources
from services import alloc_survey as sv
from services import claims, report_csv, sku_asin
from services import product_pool as pool_svc, product_score as ps
from services import textfmt
# ⚠ 按名字导入:run() 的形参就叫 params,`from services import params` 会被它
# 遮住,`params.flag(...)` 当场 AttributeError(services/params.py 头注)
from services.params import flag

DANGEROUS = False

logger = logging.getLogger("workflows.alloc_products")

# ⚠ 候选池 SQL、打分、硬闸计数全在 `services/product_pool` —— 体检与真分配
# (`alloc_plan`)必须看到**逐字相同**的池子和分数。两边各写一遍的话,体检说
# "可分配 27 万"、分配实际只认 19 万,这份体检就是假的。
_SQL_POOL = pool_svc._SQL_POOL          # 保留别名:回归按名字盯着这几段口径
_SQL_SALES = pool_svc._SQL_SALES
_SQL_REFUND = pool_svc._SQL_REFUND
_SQL_RISK = pool_svc._SQL_RISK

# 在架行(算「在线店」用)。⚠ 与「占用店」是**两个问题**,两列都要出:
#   占用店 = 台账里的**决策**(claims),没有自动释放;
#   在线店 = 此刻货**实际**挂在谁那儿(walmart_items 观测)。
# 两者不一致本身就是信息:占用在 A、货在 B = B 该下架;有货无占用 = 规划外店
# (谭总系不占任何品牌与产品)或回填时被闸挡下的行。合成一列会把这些全抹掉。
# 身份键经登记簿 amz 行(唯一写法见 conventions §九);存量下 source_key = sku,
# 「在线店」一列逐行不变。裸提取在切码后会让这一列恒空 ⇒「占用在 A、货在 B」
# 这类信息全被抹掉。
_SQL_ONLINE_SKU = """
SELECT w.store, w.sku, ls.source_key
FROM catalog.walmart_items w
LEFT JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz'
WHERE w.missing_since IS NULL AND w.published_status = 'PUBLISHED'
"""


def _pct(n, d):
    return f"{n / d:.1%}" if d else "—"


def run(params: dict) -> str:
    """输入:params(sales_days/as_of/export)→ 输出:产品分体检报告。"""
    # 产品侧销量窗口默认**近一年**(所有者定稿 2026-08-16)。
    # ⚠ 与 alloc_stores 的 90 天窗口是两码事:那边要"这家店现在什么水平",
    # 这边要"这个品到底卖没卖过" —— 窗口越短覆盖率越低(90 天只有 1.0%)
    #
    # ★ 参数名叫 `sales_days` 而不是 `days`,与 `alloc_plan` 对齐。
    #   原来这里也叫 `days`,于是同一个名字在两条工作流里装着不同的东西:
    #   `alloc_stores -p days=90` 是店铺窗口(对),`alloc_products -p days=90`
    #   却把**产品销量窗口从 365 砍到 90**。照文档 §12.4「两个窗口」的心智
    #   模型给例行一跑统一加 `-p days=90`,前两条都对、这条**静默**跑偏,
    #   而销量信号覆盖率本来就只有几个百分点,再砍窗口会把唯一有正面证据的
    #   那批品抹平 —— 报告上只会写"近 90 天销量",看不出是配错了。
    if "days" in params:
        raise ValueError(
            "alloc_products 的销量窗口参数叫 `sales_days`,不是 `days` ——"
            "`days` 在 alloc_stores/alloc_plan 里是**店铺经营水平**那个 90 天窗口,"
            "两者不是一回事(见 docs/allocation_plan.md 口径 #17a)。"
            f"你大概想要:-p sales_days={params['days']}")
    days = int(params.get("sales_days", ps.SALES_WINDOW_DAYS))
    win = sv.sales_window(str(params.get("as_of", "")), days)
    export = flag(params, "export", default=True)

    with db.pg_conn() as conn:
        data = pool_svc.load(conn, win)
        life = pool_svc.lifetime_sales(conn)
        held = claims.load_active(conn, claims.PRODUCT)
        with conn.cursor() as cur:
            cur.execute(_SQL_ONLINE_SKU)
            online: dict = {}
            for store, sku, k in cur.fetchall():
                a = sku_asin.pick_asin(k, sku)
                if a:
                    online.setdefault(a, set()).add(store)
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
        lf = life.get(c["asin"]) or {}
        rows.append((c["asin"], c["brand"] or "", c["category"],
                     resources.super_label(c["category"]),
                     round(c["score"], 1), round(c["base"], 1),
                     round(c["bonus"], 1), round(c["penalty"], 1), c["why"],
                     # 配送方式(渠道):渠道闸的产品侧,决定这个品能去哪些店
                     c["channel"] or "(未知)",
                     c["price"], c["shipping"],
                     # 落地价 = 售价 + 运费,是硬闸真正用的那个数(§7.6);
                     # 只看售价会让"9.9 美元 + 12 美元运费"看起来很便宜
                     None if c["price"] is None or c["shipping"] is None
                     else round(c["price"] + c["shipping"], 2),
                     c["sales"], round(c["gross"] or 0, 2),
                     lf.get("units"), round(lf.get("gross") or 0, 2),
                     lf.get("first_sold"), lf.get("last_sold"),
                     held.get(c["asin"]) or "",
                     "|".join(sorted(online.get(c["asin"], ()))),
                     c["rating"], c["reviews"], c["lead"],
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

    header = ["ASIN", "品牌", "大类(26类)", "品类(五大类)",
              "产品分", "口碑分", "销量加分", "罚分", "罚分原因",
              "配送方式", "售价", "运费", "落地价",
              f"近{days}天销量(件)", f"近{days}天销售额(毛额)",
              "历史总销量(件)", "历史总销售额(毛额)", "首次售出", "最近售出",
              "占用店", "在线店", "评分", "评论数", "配送天数", "缺失信号"]
    # ⚠ 按**表头算下标**排序,不写死列号。2026-08-22 插「品类」那一列时,
    #   原来的 `-r[3]` 正好排到了新插进来的字符串上 —— TypeError 还算走运,
    #   要是插的是个数字列就会静默按错的列排。同一天在冲突明细的 `d[7]` 上
    #   踩过同款(那次不报错,把「保留/下架」读成了一个数字)
    rows.sort(key=lambda r: -r[header.index("产品分")])
    p = report_csv.write("alloc_产品分.csv", header, rows)
    L += ["", f"▍明细 → {p}(按产品分降序,{len(rows):,} 行)",
          "  「缺失信号」列告诉你这一行的分是靠哪几项算出来的 ——"
          "分数说不清来源就没法推翻它",
          "  「占用店」是台账里的**决策**(claims,无自动释放),「在线店」是货"
          "此刻**实际**挂在谁那儿 —— 两列不一致本身就是信息:",
          "    占用在 A、货在 B ⇒ B 该下架;有货无占用 ⇒ 规划外店(谭总系不占"
          "任何品牌与产品)或回填时被类目/渠道闸挡下的行",
          "  ⚠ 销售额一律**毛额**(未扣退款):逐产品的退款只有 API 期算得出,"
          "与店铺侧的净额**不是同一口径**,别拿两张表对账",
          f"  ⚠ 历史销量**不进打分**:分数只看窗口内这 {days} 天(§7.6)。"
          f"拿全历史当信号"
          "会让一个三年前卖爆、现在没人要的品一直挂高分"]
    return "\n".join(L)
