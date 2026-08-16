"""alloc_plan — 产品分配方案(§七)。**危险,默认 dry-run。**

用法:
  python cli.py alloc_plan                       # 首批 3000,只出方案表
  python cli.py alloc_plan -p batch=10000
  python cli.py alloc_plan -p as_of=2026-08-16   # 钉住销量窗口右端
  python cli.py alloc_plan --execute             # 审完方案表才落占用

把候选池打分、组队、切批、发牌,产出**分配方案表**。`--execute` 只做一件事:
把方案里的品牌与 ASIN 落成占用(`catalog.claims`)。上架表另说 —— 分配是
计划层,不受 list_new 日配额闸约束。

## 三个口径,读方案表之前先看懂

· **一张牌 = 一个品牌组**,不是一个产品(品牌排他 ⇒ 整组去一家店)。
  所以"发出 N 组"和"占了 M 个货位"是两个数,M 才是你在架上看到的。
· **切批量再分层,不是整池分层**(所有者 2026-08-16 追问后定):候选池
  50 万、配额才 3 万,整池分层等于让层内上限把货摊到第 8 层去,慢 20 倍、
  未发清单 27 万行,而**发出去的还是那批 top 货**。实测切 `批量×1.5`
  与整池逐字同结果(§7.4b)。
· **定向流不进分层**:品牌已被占用的组只能去占用店,过不了那店的硬闸就
  整组淘汰(§7.3)。它们与自由流分开计数,别把两者加在一起看。

**只写占用,不碰沃尔玛。** 上架由 list_new 按自己的节奏执行。
"""

import csv
import logging
import math
from collections import Counter

from registry import db, paths
from services import alloc_engine as ae
from services import alloc_groups, alloc_survey as sv
from services import claims, product_pool, product_score as ps
from services import store_perf, store_targets, stores as stores_svc
from services import textfmt

DANGEROUS = True

logger = logging.getLogger("workflows.alloc_plan")

SOURCE = "alloc_plan"

# 候选切口 = 批量 × 这个倍数。**留余量是必须的**:有些组过不了类目/渠道闸,
# 池子刚好够数就没有腾挪余地 —— 实测切到 1.0× 时少发 7.5% 且顶层比值有
# 两家越界(轮到某店时它能接的货已被前面挑光)。1.5× 与整池同结果。
HEADROOM = 1.5

# 当前在线数(容量闸的分子)。⚠ 口径与 `alloc_stores._SQL_ONLINE_NOW`、
# KPI 表的 items_online 逐字一致(不筛 lifecycle)—— 三处必须同源,否则
# "剩余容量"在三张表里是三个数
_SQL_ONLINE_NOW = """
SELECT store, count(*) AS n
FROM catalog.walmart_items
WHERE missing_since IS NULL AND published_status = 'PUBLISHED'
GROUP BY store
"""


def _pct(n, d):
    return f"{n / d:.1%}" if d else "—"


def _pending_delist(conn, cfg, registered) -> dict:
    """输入:连接 + 配置 + 在册店 → 输出:{店: 待下架件数}(容量预支,§7.4f)。

    所有者是**一边下架一边上架**的:不预支的话,一家马上要空出 300 个货位的店
    会被算成"满了",这一批一件都分不进去。待下架 = 类目不符 ∪ 渠道不符
    (两份清单同源于 `alloc_survey` 的共享谓词,去重后按店计数)。
    ⚠ 预支**只影响配额**,不放宽容量硬闸 —— 硬闸走 `room_now`。
    """
    from services import sku_asin
    with conn.cursor() as cur:
        cur.execute(sv._SQL_PT2CAT)
        pt2cat = {pt: c for pt, c in cur.fetchall() if c}
        cur.execute(sv._SQL_ONLINE)
        items = cur.fetchall()
        asins = sorted({a for a in (sku_asin.extract_asin(it[1])
                                    for it in items) if a})
        meta = sv._fetch_meta(cur, asins, True)
    rows, _ = sv.enrich(items, meta, pt2cat)
    slot = sv.claimable(rows, registered)
    out: Counter = Counter()
    for r in slot:
        # 去重:一行同时踩两道闸也只空出一个货位。不去重会把 room 高估
        if sv.offends_category(r, cfg) or sv.offends_channel(r, cfg):
            out[r["store"]] += 1
    return dict(out)


def run(params: dict) -> str:
    """输入:params(batch/days/as_of/export/execute)→ 输出:方案摘要。"""
    execute = bool(params.get("execute"))
    batch = int(params.get("batch", 3000))
    days = int(params.get("days", 90))
    win = sv.sales_window(str(params.get("as_of", "")), days)
    export = str(params.get("export", "1")).lower() not in {"0", "false", "no"}

    try:
        cfg = store_targets.load_targets()
    except Exception as e:                          # noqa: BLE001
        return f"⛔ 限额表读不到({e}):没有类目/渠道/容量就没法分配"
    try:
        registered = stores_svc.registered_names()
    except Exception as e:                          # noqa: BLE001
        return f"⛔ 凭证表读不到({e}):分不清在营店与冻结行,拒绝分配"

    with db.pg_conn() as conn:
        data = product_pool.load(conn, win)
        perf_raw = store_perf.load(conn, win)
        with conn.cursor() as cur:
            cur.execute(_SQL_ONLINE_NOW)
            online_now = {s: int(n) for s, n in cur.fetchall()}
        held_brand = claims.load_active(conn, claims.BRAND)
        held_prod = claims.load_active(conn, claims.PRODUCT)
        pending = _pending_delist(conn, cfg, registered)

    # ── 候选漏斗 ──────────────────────────────────────────────────────
    scored, gated = product_pool.score_all(data)
    # ⚠ 漏斗前四行的单位是**产品**,后两行是**组**。混在一列里报会骗人:
    # "60 个产品 → 20 组"会显示成 33.3%,读起来像丢了三分之二的货,
    # 其实一件没丢(每组 3 件)。所以组那两行同时给组数与货位数
    funnel = [("候选池", len(data["pool"])), ("过硬闸", len(scored))]
    live = [c for c in scored if c["score"] >= ps.CUTOFF]
    funnel.append((f"≥ 淘汰线 {ps.CUTOFF:.0f}", len(live)))
    # 已占 ASIN:那是别人的货位,连定向流都不走(它已经在架上了)
    live = [c for c in live if c["asin"] not in held_prod]
    funnel.append(("去掉已占 ASIN", len(live)))

    g = alloc_groups.build(live, held_brand)
    free, directed = g["free"], g["directed"]
    grouped = [("组队后·自由流(牌堆)", len(free), sum(x["size"] for x in free)),
               ("组队后·定向流(已占品牌)", len(directed),
                sum(x["size"] for x in directed))]

    # ── 店铺配额 ─────────────────────────────────────────────────────
    metrics = store_perf.derive(perf_raw, days)
    q = store_perf.quota_inputs(metrics, cfg, online_now, pending)
    stores: dict = {}
    for s, qq in q.items():
        if s not in registered or sv.is_excluded(s) or not qq.get("participates"):
            continue
        stores[s] = {"quota": 0, "room": int(qq.get("room") or 0),
                     "categories": (cfg.get(s) or {}).get("categories") or [],
                     "channel": (cfg.get(s) or {}).get("channel"),
                     "fit": 0.0, "tier": 1 if online_now.get(s) else 2}
    if not stores:
        return ("⛔ 没有一家店可以接货(在册 ∧ 规划内 ∧「单店最大在线数」> 0)。"
                "先跑 alloc_stores 看是谁被挡下的")

    # §7.4a:need = w_gap·缺口 + w_room·容量比 + w_eff·效率
    W_GAP, W_ROOM, W_EFF = 0.6, 0.25, 0.15
    need = {}
    for s in stores:
        qq = q[s]
        gap = qq.get("gap")
        # ⚠ 缺口算不出(没填日目标)时**按 0 计**而不是跳过:公式主项对它是空的,
        # 但它仍该按容量与效率拿到一份。摘要会点名这些店要补填目标
        need[s] = (W_GAP * (gap if gap is not None else 0.0)
                   + W_ROOM * min(1.0, (qq.get("room") or 0) / max(1, batch))
                   + W_EFF * min(1.0, (qq.get("eff") or 0.0) / 2))
    tot_need = sum(need.values())
    for s in stores:
        stores[s]["fit"] = need[s]
        # ⚠ 必须落成 int:`-(-a // b)` 那个整数向上取整的写法对 float 是
        # 地板除,配额会变成 10.0 这种东西,一路带到报告和 csv 里
        stores[s]["quota"] = min(stores[s]["room"],
                                 math.ceil(batch * need[s] / tot_need)
                                 if tot_need else 0)
    no_gap = [s for s in stores if q[s].get("gap") is None]

    # ── 定向流:品牌已占,只能去那家店 ────────────────────────────────
    dir_ok, dir_out = [], []
    for grp in directed:
        st = stores.get(grp["store"])
        if st and ae._gate(grp, grp["store"], st) and grp["size"] <= st["room"]:
            dir_ok.append(grp)
        else:
            why = ("占用店本批不接货" if not st else
                   "过不了占用店的类目/渠道闸" if not ae._gate(grp, grp["store"], st)
                   else "占用店容量不足")
            dir_out.append((grp, why))

    # ── 自由流:切批量 → 发牌 ────────────────────────────────────────
    cut = int(batch * HEADROOM)
    pool_sorted = sorted(free, key=lambda x: (-x["score"], x["key"]))
    deck = pool_sorted[:cut]
    result = ae.deal(deck, stores)
    acc = ae.acceptance(result)

    placed_items = sum(v["items"] for v in result["by_store"].values())
    L = ["", "═══ 分配方案 ═══", "",
         f"▍批量 {batch:,} 货位;候选切口 {cut:,} 组(批量 ×{HEADROOM})"
         f";销量窗口 {win['day']} 往前 {days} 天"]
    L += ["", "▍候选漏斗(前四行单位是**产品**,后两行是**组**——别拿组数除产品数)"]
    L += textfmt.table(
        ["", "产品", "占候选池", "组"],
        [[k, f"{v:,}", _pct(v, len(data["pool"])), ""] for k, v in funnel]
        + [[k, f"{n:,}", _pct(n, len(data["pool"])), f"{c:,}"]
           for k, c, n in grouped],
        align="<>><")
    if gated:
        L.append("  硬闸淘汰:" + " · ".join(f"{k} {v:,}"
                                                for k, v in sorted(gated.items())))
    if g["dropped"]:
        L.append("  组队时剔除:" + " · ".join(f"{k} {v:,}"
                                                  for k, v in g["dropped"].most_common()))
    if data["risk_err"]:
        L.append(f"  ⚠ product_risk 读不到({data['risk_err']}):黑历史罚分全为 0")

    L += ["", f"▍发牌结果:自由流 {len(result['assign']):,} 组 / "
          f"{placed_items:,} 个货位"
          + (f";定向流 {len(dir_ok):,} 组 / "
             f"{sum(x['size'] for x in dir_ok):,} 个货位" if dir_ok else "")]
    rows_tbl = []
    for s in sorted(stores, key=lambda x: -result["by_store"].get(x, {}).get("items", 0)):
        b, a = result["by_store"].get(s, {}), acc.get(s, {})
        d = sum(x["size"] for x in dir_ok if x["store"] == s)
        rows_tbl.append([s, f"{stores[s]['quota']:,}", f"{b.get('items', 0):,}",
                         f"{d:,}", f"{stores[s]['room']:,}",
                         f"{a.get('top_ratio') or 0:.2f}",
                         "⚠ 独吞" if a.get("over_cap") else
                         ("⚠ 顶层越界" if a.get("top_ratio") is not None
                          and not 0.7 <= a["top_ratio"] <= 1.3 else "")])
    L += textfmt.table(["店铺", "配额", "自由流", "定向流", "剩余容量",
                        "顶层比值", ""], rows_tbl, align="<>>>>><")
    L.append("  顶层比值 = 「拿到的 L1 组数占比」÷「配额占比」,**要落 [0.7, 1.3]**;"
             "越界就是参数没调对,不是模型判断(§7.4b)")
    over = [s for s in stores if result["by_store"].get(s, {}).get("items", 0)
            > stores[s]["quota"]]
    if over:
        # 这不是 bug:组是原子的,配额在挑人**之前**判,所以最后一张牌可以把它
        # 顶过头(否则 40 件的组遇上剩 12 配额的店就永远发不出去)。不说破的话,
        # "配额 10 却分了 12"看起来像算错了
        L.append(f"  ({len(over)} 家分到的货位多于配额:组是原子的,配额在挑人**之前**判,"
                 f"最后一张牌可以顶过头;真正不许越的是剩余容量)")

    if result["unplaced"]:
        why = Counter(u["reason"] for u in result["unplaced"])
        L.append("  未发出 " + f"{len(result['unplaced']):,} 组:"
                 + " · ".join(f"{ae.REASON_LABEL[k]} {v:,}"
                              for k, v in why.most_common()))
    if dir_out:
        L.append(f"  定向流淘汰 {len(dir_out):,} 组(品牌已被占,但去不了占用店):"
                 + " · ".join(f"{k} {v}" for k, v in
                              Counter(w for _, w in dir_out).most_common()))
    if no_gap:
        L.append(f"  ⚠ {len(no_gap)} 家没填日目标销售额,配额公式的主项对它们是空的:"
                 + "、".join(no_gap[:6]))

    to_claim = _to_claim(result["assign"], dir_ok)
    if not export and not execute:
        return "\n".join(L)

    if export:
        paths.reports_dir().mkdir(parents=True, exist_ok=True)
        p = _write_plan(result["assign"], dir_ok, result["unplaced"], dir_out)
        L += ["", f"▍方案表 → {p}",
              "  逐产品一行(品牌组 / 组分 / 去向店 / 逐段得分 / 层号 / 流别)。"
              "**先看上面三个验收指标再看明细** —— 一家独吞是参数错了,不是模型判断"]

    if not execute:
        L += ["", f"🧪 dry-run:未落任何占用。审完方案表后加 --execute"
              f"(将落品牌 {sum(1 for c in to_claim if c['kind'] == claims.BRAND):,}"
              f" + 产品 {sum(1 for c in to_claim if c['kind'] == claims.PRODUCT):,} 条)"]
        return "\n".join(L)

    with db.pg_conn() as conn:
        ok, conflicts = claims.claim_many(conn, to_claim)
    logger.warning("alloc_plan 落库:成功 %d,已被别店占 %d", ok, len(conflicts))
    L += ["", f"✅ 已落占用 {ok:,} 条"
          + (f";与已有占用冲突 {len(conflicts)} 条(保持原归属不动)"
             if conflicts else ";无冲突"),
          "  货还没上架 —— 分配是计划层,上架由 list_new 按自己的节奏执行"]
    return "\n".join(L)


def _to_claim(assign: list, dir_ok: list) -> list:
    """输入:发牌结果 + 定向流 → 输出:待落占用行。

    ⚠ 定向流的**品牌占用已经存在**(它就是因为被占才走定向流的),只落它的
    产品占用。重复落品牌不会出错(ON CONFLICT DO NOTHING),但会让"落库 N 条"
    这个数对不上人能数出来的东西。
    """
    out = []
    for a in assign:
        grp, store = a["group"], a["store"]
        if grp.get("brand"):
            out.append({"kind": claims.BRAND, "claim_key": grp["brand"],
                        "store": store, "source": SOURCE})
        out += _prod_claims(grp, store)
    for grp in dir_ok:
        out += _prod_claims(grp, grp["store"])
    return out


def _prod_claims(grp: dict, store: str) -> list:
    return [{"kind": claims.PRODUCT, "claim_key": it["asin"], "store": store,
             "source": SOURCE, "walmart_pt": it.get("pt"), "pt_source": None}
            for it in grp["items"]]


def _write_plan(assign, dir_ok, unplaced, dir_out) -> str:
    p = paths.reports_dir() / "alloc_分配方案.csv"
    with p.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["流别", "去向店", "层", "品牌组", "组分", "组件数", "大类",
                    "渠道", "ASIN", "产品分", "口碑分", "销量加分", "罚分",
                    "罚分原因", "近期销量", "评分", "评论数", "配送天数"])
        for a in sorted(assign, key=lambda x: (x["layer"], -x["group"]["score"])):
            _rows(w, "自由流", a["store"], a["layer"], a["group"])
        for grp in sorted(dir_ok, key=lambda x: -x["score"]):
            _rows(w, "定向流", grp["store"], "", grp)
        for u in unplaced:
            _rows(w, f"未发出({ae.REASON_LABEL[u['reason']]})", "", "", u["group"])
        for grp, why in dir_out:
            _rows(w, f"定向流淘汰({why})", grp["store"], "", grp)
    return str(p)


def _rows(w, flow, store, layer, grp):
    for it in grp["items"]:
        w.writerow([flow, store, layer, grp["key"], round(grp["score"], 1),
                    grp["size"], grp["category"], grp["channel"], it["asin"],
                    round(it["score"], 1), round(it["base"], 1),
                    round(it["bonus"], 1), round(it["penalty"], 1), it["why"],
                    it["sales"], it["rating"], it["reviews"], it["lead"]])
