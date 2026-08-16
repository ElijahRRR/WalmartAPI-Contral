"""alloc_plan — 产品分配方案(§七)。**危险,默认 dry-run。**

用法:
  python cli.py alloc_plan                          # 首批 3000,只出方案表
  python cli.py alloc_plan -p batch=10000
  python cli.py alloc_plan -p directed_share=0.8    # 这一批多补已占品牌
  python cli.py alloc_plan -p directed_share=0      # 这一批只拓新品牌
  python cli.py alloc_plan -p as_of=2026-08-16      # 钉住销量窗口右端
  python cli.py alloc_plan --execute                # 审完方案表才落占用

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
· **两条流分账,谁也不许把对方饿死**:定向流(补齐已占品牌)最多吃
  `批量 × directed_share`(默认 50%),自由流(拓新品牌)拿其余 —— 一方吃不满时
  另一方取走余额。⚠ 这是**上限**不是优先级:实测不设上限时定向流一口吃光
  3,000 批量、自由流 0,而后面还排着 27,208 件,要十批之后自由流才轮得到。
  定向流不进分层(强制路由),类目**按件筛**而非整组淘汰;容量够而额度用完的
  **排队等下一批**,与"去不了占用店"分开计数 —— 前者加大 `-p batch=` 就能发,
  后者要你去改配置或释放品牌。

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

# 定向流最多吃批量的这个比例。**不是"优先"而是"分账"**:定向流(补齐已占品牌)
# 与自由流(拓新品牌)是两件不同的事,谁也不该把对方饿死。
# 实测 2026-08-16:不设上限时定向流一口吃光 3,000 批量、自由流 0,而后面还排着
# 27,208 件 —— 按每批 3,000 算要十批之后自由流才轮得到。
# 一方吃不满时另一方**可以取走余额**(不浪费批量),所以这是上限不是配额。
DIRECTED_SHARE = 0.5

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


def _fit_to_store(grp: dict, st: dict) -> tuple[dict | None, int]:
    """输入:定向流的组 + 占用店 → 输出:(该店收得了的那部分, 被剪掉的件数)。

    **只用于定向流**:去向店已被品牌占用固定死,不存在"该给谁"的竞争,所以按件
    筛是良定义的、且严格更划算。自由流不许这么做 —— 那边组的完整性参与竞争
    (组分、size 都会变),按件筛等于让同一个品牌在不同店之间被拆开,破坏排他。

    渠道整组同进退(建组时已按多数派统一过);类目逐件判 —— 一个品牌横跨两个
    大类时,占用店收得了的那部分**本来就能上架**,不该被组里的多数派连累。
    全被剪光返回 (None, 原件数)。
    """
    ok = [it for it in grp["items"]
          if store_targets.allowed(st_cfg(st), it["category"])]
    if not ok:
        return None, grp["size"]
    if len(ok) == len(grp["items"]):
        return grp, 0
    return {**grp, "items": ok, "size": len(ok),
            "score": max(x["score"] for x in ok),
            "category": alloc_groups._major(x["category"] for x in ok)}, \
        grp["size"] - len(ok)


def st_cfg(st: dict) -> dict:
    """输入:引擎口径的店铺行 → 输出:`store_targets.allowed` 认得的配置行。

    两处对类目的表示必须走同一个判定函数,不能在这里另写 `in` —— 「三列全空 =
    不限制」这条正着写反着写都像对的,判定只留一处(store_targets.allowed)。
    """
    return {"categories": st.get("categories") or []}


def run(params: dict) -> str:
    """输入:params(batch/days/as_of/export/execute)→ 输出:方案摘要。"""
    execute = bool(params.get("execute"))
    batch = int(params.get("batch", 3000))
    dir_share = float(params.get("directed_share", DIRECTED_SHARE))
    if not 0.0 <= dir_share <= 1.0:
        return f"⛔ directed_share 要落在 [0, 1],给的是 {dir_share}"
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

    # ── 定向流先走:品牌已占,只能去那家店(强制路由,不是选择)────────
    # ⚠ 两条纪律,缺一条就会炸:
    #   ① **容量要累计**。逐组判 `size <= room` 的话,十几个组各自都"塞得下",
    #      加起来能撑爆好几倍(生产实测 2026-08-16:A142 剩余容量 1,918,
    #      定向流塞了 8,384)。
    #   ② **也吃批量**。所有者要的是"这一批上 N 个货位",定向流不受批量约束的话,
    #      写 batch=3000 会落 4 万条占用 —— 而占用撤不回。定向流优先(它没得选),
    #      吃剩的才归自由流。
    dir_budget = int(batch * dir_share)
    used: Counter = Counter()
    dir_ok, dir_out, dir_wait = [], [], []
    dir_trim = 0
    for orig in sorted(directed, key=lambda x: (-x["score"], x["key"])):
        st = stores.get(orig["store"])
        if not st:
            dir_out.append((orig, "占用店本批不接货"))
            continue
        # ★ 定向流按**件**筛,不整组淘汰(§7.3 那句"整组淘汰"写的是自由流的
        #   竞争场景)。这里去向店已经被品牌占用**固定死**了,没有"该给谁"
        #   的问题,所以留下该店收得了的那些件、其余才淘汰。
        #   不这么做的话:一个品牌 60% 厨房 / 40% 家居,组大类取多数派=厨房,
        #   只做家居的占用店会把**那 40% 本来能上架的家居商品一起拒掉**。
        grp, trimmed = _fit_to_store(orig, st)
        dir_trim += trimmed
        if grp is None:
            dir_out.append((orig, "过不了占用店的类目/渠道闸"))
        elif used[grp["store"]] + grp["size"] > st["room"]:
            dir_out.append((grp, "占用店容量不足"))
        elif sum(used.values()) + grp["size"] > dir_budget:
            # 容量够、只是这一批的额度用完了 —— 与"去不了"分开计数:
            # 前者下一批照样能发,后者要所有者去改配置或释放品牌
            dir_wait.append(grp)
        else:
            used[grp["store"]] += grp["size"]
            dir_ok.append(grp)
    dir_items = sum(used.values())
    for s in stores:                       # 定向流吃掉的容量,自由流不能再用
        stores[s]["room"] = max(0, stores[s]["room"] - used[s])

    # ── 自由流:剩下的批量按 §7.4a 分配额 → 切批 → 发牌 ──────────────
    free_batch = max(0, batch - dir_items)
    W_GAP, W_ROOM, W_EFF = 0.6, 0.25, 0.15
    need = {}
    for s in stores:
        qq = q[s]
        gap = qq.get("gap")
        # ⚠ 缺口算不出(没填日目标)时**按 0 计**而不是跳过:公式主项对它是空的,
        # 但它仍该按容量与效率拿到一份。摘要会点名这些店要补填目标
        need[s] = (W_GAP * (gap if gap is not None else 0.0)
                   + W_ROOM * min(1.0, stores[s]["room"] / max(1, free_batch))
                   + W_EFF * min(1.0, (qq.get("eff") or 0.0) / 2))
    tot_need = sum(need.values())
    for s in stores:
        stores[s]["fit"] = need[s]
        # ⚠ 必须落成 int:`-(-a // b)` 那个整数向上取整的写法对 float 是
        # 地板除,配额会变成 10.0 这种东西,一路带到报告和 csv 里
        stores[s]["quota"] = min(stores[s]["room"],
                                 math.ceil(free_batch * need[s] / tot_need)
                                 if tot_need else 0)
    no_gap = [s for s in stores if q[s].get("gap") is None]

    cut = int(free_batch * HEADROOM)
    pool_sorted = sorted(free, key=lambda x: (-x["score"], x["key"]))
    deck = pool_sorted[:cut]
    result = ae.deal(deck, stores) if deck else {
        "assign": [], "unplaced": [], "by_store": {}, "layers": [],
        "params": {"thickness": ae.LAYER_THICKNESS, "slack": ae.LAYER_SLACK,
                   "stores": 0, "skipped_stores": len(stores)}}
    acc = ae.acceptance(result)

    placed_items = sum(v["items"] for v in result["by_store"].values())
    L = ["", "═══ 分配方案 ═══", "",
         f"▍批量 {batch:,} 货位 = 定向流 {dir_items:,} + 自由流 {placed_items:,}"
         f";销量窗口 {win['day']} 往前 {days} 天",
         f"  定向流(补齐已占品牌)上限 {dir_budget:,} = 批量 ×{dir_share:.0%}"
         f"(`-p directed_share=` 可调);自由流(拓新品牌)分到 {free_batch:,},"
         f"候选切口 {cut:,} 组(×{HEADROOM})",
         "  两条流是不同的事,谁也不该把对方饿死 —— 一方吃不满时另一方取走余额"]
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
    L.append("  顶层比值 = 「拿到的 L1 **货位**占比」÷「配额占比」(同单位),"
             "**要落 [0.7, 1.3]**;越界就是参数没调对,不是模型判断(§7.4b)")
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
    if dir_trim:
        L.append(f"  定向流按件筛掉 {dir_trim:,} 件(品牌的类目跨度比占用店的准入宽);"
                 f"**同组里占用店收得了的那些件照常发** —— 不因为组里多数派是别的"
                 f"大类就整组扔掉")
    if dir_wait:
        L.append(f"  定向流还有 {len(dir_wait):,} 组 / "
                 f"{sum(x['size'] for x in dir_wait):,} 个货位**排队等下一批**"
                 f"(容量够,只是本批额度用完了)—— 加大 -p batch= 就能一次多上些")
    if dir_out:
        L.append(f"  定向流淘汰 {len(dir_out):,} 组 / "
                 f"{sum(x['size'] for x, _ in dir_out):,} 件"
                 f"(品牌已被占,但**去不了**占用店):"
                 + " · ".join(f"{k} {v}" for k, v in
                              Counter(w for _, w in dir_out).most_common())
                 + " —— 这批要你去改配置或释放品牌,不是等下一批就能好")
        # 光给总数没法动手。按「店 × 缺的大类」摊开,所有者一眼看出
        # "给 A085 开厨房能救回多少件" —— 那是他真能做的决定
        blocked: Counter = Counter()
        for grp, w in dir_out:
            if w == "过不了占用店的类目/渠道闸":
                blocked[(grp["store"], grp["category"] or "(未归类)")] += grp["size"]
        if blocked:
            L.append("  其中类目挡下的,按「店 × 缺的大类」:"
                     + " · ".join(f"{s} 缺「{c}」{n:,} 件"
                                  for (s, c), n in blocked.most_common(6))
                     + f"(共 {len(blocked)} 组合)—— 给该店开这个大类就能救回")
    if no_gap:
        L.append(f"  ⚠ {len(no_gap)} 家没填日目标销售额,配额公式的主项对它们是空的:"
                 + "、".join(no_gap[:6]))

    to_claim = _to_claim(result["assign"], dir_ok)
    if not export and not execute:
        return "\n".join(L)

    if export:
        paths.reports_dir().mkdir(parents=True, exist_ok=True)
        # ⚠ **要动手的**和**诊断用的**分两张表。合成一张的实测后果:批量 3,000
        # 却出了 48,816 行,其中 45,815 行是排队与淘汰 —— 那张表没法用,而所有者
        # 第一眼看到的就是那个总行数。摘要里两个数都报,不存在"藏起来"的问题
        p_plan, n_plan = _write_plan(result["assign"], dir_ok)
        p_out, n_out = _write_rejects(result["unplaced"], dir_out, dir_wait)
        L += ["", f"▍要上架的 {n_plan:,} 行 → {p_plan}",
              "  逐产品一行(品牌组 / 组分 / 去向店 / 逐段得分 / 层号 / 流别)。"
              "**先看上面的验收指标再看明细** —— 一家独吞是参数错了,不是模型判断"]
        if n_out:
            L += [f"▍没进这一批的 {n_out:,} 行 → {p_out}",
                  "  「排队」下一批加大 batch 就能发;「淘汰」要你改配置或释放品牌;"
                  "「未发出」看原因列"]

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


_HEADER = ["流别", "去向店", "层", "品牌组", "组分", "组件数", "大类",
           "渠道", "ASIN", "产品分", "口碑分", "销量加分", "罚分",
           "罚分原因", "近期销量", "评分", "评论数", "配送天数"]


def _write_plan(assign, dir_ok) -> tuple[str, int]:
    """输入:发牌结果 + 定向流 → 输出:(路径, 产品行数)。**只放要上架的。**"""
    p = paths.reports_dir() / "alloc_分配方案.csv"
    n = 0
    with p.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(_HEADER)
        for a in sorted(assign, key=lambda x: (x["layer"], -x["group"]["score"])):
            n += _rows(w, "自由流", a["store"], a["layer"], a["group"])
        for grp in sorted(dir_ok, key=lambda x: -x["score"]):
            n += _rows(w, "定向流", grp["store"], "", grp)
    return str(p), n


def _write_rejects(unplaced, dir_out, dir_wait) -> tuple[str, int]:
    """输入:三类没进这一批的 → 输出:(路径, 产品行数)。

    与方案表分开,是因为它们的处置**完全不同**:排队的下一批加大 batch 就能发,
    淘汰的要所有者改配置或释放品牌。混在一张表里,3,000 行要动手的会被 45,815 行
    诊断淹掉(2026-08-16 实测:所有者第一眼看到的就是 48,816 这个总行数)。
    """
    p = paths.reports_dir() / "alloc_未入选.csv"
    n = 0
    with p.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(_HEADER)
        for grp in dir_wait:
            n += _rows(w, "定向流排队(本批额度用完)", grp["store"], "", grp)
        for grp, why in dir_out:
            n += _rows(w, f"定向流淘汰({why})", grp["store"], "", grp)
        for u in unplaced:
            n += _rows(w, f"未发出({ae.REASON_LABEL[u['reason']]})", "", "",
                       u["group"])
    return str(p), n


def _rows(w, flow, store, layer, grp) -> int:
    for it in grp["items"]:
        w.writerow([flow, store, layer, grp["key"], round(grp["score"], 1),
                    grp["size"], grp["category"], grp["channel"], it["asin"],
                    round(it["score"], 1), round(it["base"], 1),
                    round(it["bonus"], 1), round(it["penalty"], 1), it["why"],
                    it["sales"], it["rating"], it["reviews"], it["lead"]])
    return len(grp["items"])
