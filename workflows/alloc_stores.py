"""alloc_stores — 店铺经营水平体检(只读)。分配引擎动工前先看这张表。

用法:
  python cli.py alloc_stores                     # 近 90 天
  python cli.py alloc_stores -p days=30          # 换窗口
  python cli.py alloc_stores -p as_of=2026-08-15 # 钉住窗口右端(默认今天 UTC)
  python cli.py alloc_stores -p export=0         # 只看摘要不落 csv

**为什么先出这张表再写引擎**:引擎的店铺侧决策全建在三个数上(日均净销售额 /
单品日产出 / 缺口)。这三个数算歪了,后面所有分配都跟着歪,而且从方案表上
**看不出来**——只会看到"这家店怎么分了这么多"。所以先把它们摊开给人看一眼,
对不上的当场就能发现。

口径全部来自 `services/store_perf`(设计见 docs/allocation_plan.md §7.4~7.4g),
三条最容易读错的:
  · **分母是在售天数,不是日历天数** —— 停业期不算进去,否则"停过业"会被
    读成"卖得差",缺口虚高 ⇒ 停过业的店反而被灌最多货;
  · **销售额是净额**(已扣退款);⚠ 历史期算不出退款,所以「历史期占比」高的
    店,它那个"净额"其实是毛额,与 API 期的店**不是同一个口径**;
  · **在售 < 14 天的店,三个指标一律是全店中位数**(所有者拍板:那种数据
    一点都不信),表里「取值依据」列会写明这一行是自己的还是中位数顶上来的。

**只读**:不写任何表、不调沃尔玛。
"""

import csv
import logging

from registry import db, paths
from services import alloc_survey as sv
from services import store_perf, store_targets, stores as stores_svc
from services import textfmt

DANGEROUS = False

logger = logging.getLogger("workflows.alloc_stores")

# 当前在线数(容量闸的分子):与 alloc_audit 同一口径 —— 已发布、非退市
_SQL_ONLINE_NOW = """
SELECT store, count(*) AS n
FROM catalog.walmart_items
WHERE missing_since IS NULL
  AND coalesce(upper(lifecycle_status), 'ACTIVE') = 'ACTIVE'
  AND published_status = 'PUBLISHED'
GROUP BY store
"""


def _fmt(v, digits=2, dash="—"):
    return dash if v is None else f"{v:,.{digits}f}"


def run(params: dict) -> str:
    """输入:params(days/as_of/export)→ 输出:店铺经营水平表。"""
    days = int(params.get("days", 90))
    win = sv.sales_window(str(params.get("as_of", "")), days)
    export = str(params.get("export", "1")).lower() not in {"0", "false", "no"}

    with db.pg_conn() as conn:
        raw = store_perf.load(conn, win)
        with conn.cursor() as cur:
            cur.execute(_SQL_ONLINE_NOW)
            online_now = {s: int(n) for s, n in cur.fetchall()}

    metrics = store_perf.derive(raw, days)
    try:
        cfg = store_targets.load_targets()
    except Exception as e:                          # noqa: BLE001
        return f"⛔ 限额表读不到({e}):没有目标与容量就算不出缺口与配额"
    try:
        registered = stores_svc.registered_names()
    except Exception as e:                          # noqa: BLE001
        return f"⛔ 凭证表读不到({e}):分不清在营店与冻结行,拒绝出表"

    q = store_perf.quota_inputs(metrics, cfg, online_now)
    # 规划内 = 在册 ∧ 非规划外(谭总系)。范围外的店不参与分配,不进这张表
    scope = sorted(s for s in set(cfg) | set(registered)
                   if s in registered and not sv.is_excluded(s))

    rows = []
    for s in scope:
        m, qq = metrics.get(s, {}), q.get(s, {})
        rows.append({
            "店铺": s,
            "参与分配": {True: "是", False: "否(填了0)", None: "(未填)"}[
                qq.get("participates")],
            "在售天数": m.get("active_days", 0),
            "KPI覆盖": m.get("cover", 0.0),
            "日均净销售额": m.get("daily_net"),
            "日目标": (cfg.get(s) or {}).get("gmv"),
            "缺口": qq.get("gap"),
            "单品日产出": m.get("slot_value"),
            "效率倍数": qq.get("eff"),
            "日均订单": m.get("daily_orders"),
            "当前在线": online_now.get(s, 0),
            "容量上限": (cfg.get(s) or {}).get("max_online"),
            "剩余容量": qq.get("room_now"),
            "历史期占比": m.get("hist_share", 0.0),
            "取值依据": m.get("basis", "无数据"),
        })
    # 缺口大的排前面(所有者拍板:把货给离目标最远的店)
    rows.sort(key=lambda r: (-(r["缺口"] if r["缺口"] is not None else -1),
                             -(r["日均净销售额"] or 0)))

    part = [r for r in rows if r["参与分配"] == "是"]
    thin = [r for r in rows if r["取值依据"].startswith("中位数")]
    no_gap = [r for r in part if r["缺口"] is None]
    hist_heavy = [r for r in part if r["历史期占比"] >= 0.5]

    n = lambda v: f"{int(v):,}"                     # noqa: E731
    L = ["", "═══ 店铺经营水平 ═══",
         "", f"▍窗口 {win['day']} 往前 {days} 天(右端不含当天),销售额均为**净额**",
         f"  规划内 {len(rows)} 家,其中参与分配 {len(part)} 家"]
    if thin:
        L.append(f"  ⚠ {len(thin)} 家在售不足 {store_perf.MIN_ACTIVE_DAYS} 天,"
                 f"三个指标取全店中位数(不代表其真实水平):"
                 + "、".join(r["店铺"] for r in thin[:6]))
    if no_gap:
        L.append(f"  ⚠ {len(no_gap)} 家算不出缺口(没填日目标销售额),"
                 f"**配额公式的主项对它们是空的**:"
                 + "、".join(r["店铺"] for r in no_gap[:6]))
    if hist_heavy:
        L.append(f"  ⚠ {len(hist_heavy)} 家窗口过半落在历史期,其「净额」实为毛额"
                 f"(历史行没有退款数据),**与 API 期的店不是同一口径**")

    L += ["", "▍缺口最大的 10 家(货优先给它们)"]
    L += textfmt.table(
        ["店铺", "在售", "日均净额", "日目标", "缺口", "货位值", "剩余容量", ""],
        [[r["店铺"], r["在售天数"], _fmt(r["日均净销售额"]),
          _fmt(r["日目标"], 0),
          "—" if r["缺口"] is None else f"{r['缺口']:.0%}",
          _fmt(r["单品日产出"], 3), _fmt(r["剩余容量"], 0),
          "" if r["取值依据"].startswith("自身") else f"⚠ {r['取值依据']}"]
         for r in part[:10]],
        align="<>>>>>><")

    if not export:
        L += ["", "(-p export=0:未落 csv)"]
        return "\n".join(L)

    paths.reports_dir().mkdir(parents=True, exist_ok=True)
    p = paths.reports_dir() / "alloc_店铺经营水平.csv"
    with p.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["店铺"])
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else
                            (f"{v:.1%}" if k in ("缺口", "KPI覆盖", "历史期占比")
                             else v))
                        for k, v in r.items()})
    L += ["", f"▍明细 → {p}",
          "  逐店 15 列(含取值依据、KPI 覆盖、历史期占比)——"
          "分配跑出奇怪结果时,先回来对这三列"]
    return "\n".join(L)
