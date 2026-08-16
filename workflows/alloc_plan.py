"""alloc_plan — 产品分配方案(§七)。**危险,默认 dry-run。**

用法:
  python cli.py alloc_plan                       # 各店按自己的容量与缺口分满
  python cli.py alloc_plan -p batch=3000         # 安全阀:总量封顶,等比缩配额
  python cli.py alloc_plan -p as_of=2026-08-16   # 钉住销量窗口右端
  python cli.py alloc_plan --execute             # 审完方案表才落占用

把候选池打分、组队、切批、发牌,产出**分配方案表**。`--execute` 只做一件事:
把方案里的品牌与 ASIN 落成占用(`catalog.claims`)。上架表另说 —— 分配是
计划层,不受 list_new 日配额闸约束。

## 三个口径,读方案表之前先看懂

· **一张牌 = 一个品牌组**,不是一个产品(品牌排他 ⇒ 整组去一家店)。
  所以"发出 N 组"和"占了 M 个货位"是两个数,M 才是你在架上看到的。
· **配额不从一个人为总数里切**(所有者定稿 2026-08-16:"这个限制我甚至就
  认为不应该有")。每家店自己算:`min(剩余容量, 缺口 ÷ 单品日产出)` ——
  上限 3500、在线 2200 就是 1300 个位置,缺口要 1500 也只能上 1300。
  本轮可分 = 各店之和。`-p batch=` 只是想小步试跑时的安全阀。
· **定向流不是另一条流水线,就是同一副牌里"只有一家店能要"的牌**。
  它带着 `store` 进牌堆,`alloc_engine._gate` 的归属闸认它,记账只有一处。
  ⚠ 分成两个阶段的实测后果(同日):两边各有一套配额与容量记账,谁也不知道
  对方吃了多少 —— 定向流一口吃光批量、容量闸各判各的双双超容。
  定向流的类目**按件筛**而非整组淘汰(§7.3)。
· **切候选再分层,不是整池分层**:候选池 50 万、可分才 3 万,整池分层等于
  让层内上限把货摊到第 8 层去,慢 20 倍、未发清单 27 万行,而**发出去的还是
  那批 top 货**。实测切 `可分×1.5` 与整池逐字同结果(§7.4b)。

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

# 未入选表里,自由流"排队中"最多写多少组(按组分降序取头部)。
# 全写会把这张表撑到十万行;一组不写则"我那个高分品怎么没分出去"两张表里
# 都查不到 —— 头部是所有者真正会翻的那一段。
QUEUE_SAMPLE = 2000

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


_SQL_ONLINE_SKU = """
SELECT store, sku FROM catalog.walmart_items
WHERE missing_since IS NULL AND published_status = 'PUBLISHED'
"""


def _listed_asins(conn, registered) -> set:
    """输入:连接 + 在册店名 → 输出:**已经在架**的 ASIN 集合(规划内店)。

    ★ 候选池排除的是「已上架」,**不是「已占位」**(所有者定稿 2026-08-16:
    「已占位和已上架是两回事…分配即占位」)。占位了但没上架的货,下一轮
    照样要出现在方案表里 —— 否则方案表就成了增量,上一轮定下、还没执行的
    上架指令**从表上消失了**,而那恰恰是所有者要照着做的东西。
    ⚠ 规划外店(谭总系)的在架行不算:它们退出分配体系,不占任何品牌与产品,
    同一个 ASIN 在那边在架不妨碍规划内的店上它(§六)。
    """
    from services import sku_asin
    with conn.cursor() as cur:
        cur.execute(_SQL_ONLINE_SKU)
        rows = cur.fetchall()
    return {a for store, sku in rows
            if store in registered and not sv.is_excluded(store)
            and (a := sku_asin.extract_asin(sku))}


def _fit_to_store(grp: dict, st: dict) -> tuple[dict | None, int]:
    """输入:定向流的组 + 占用店 → 输出:(该店收得了的那部分, 被剪掉的件数)。

    **只用于定向流**:去向店已被品牌占用固定死,不存在"该给谁"的竞争,所以按件
    筛是良定义的、且严格更划算。自由流不许这么做 —— 那边组的完整性参与竞争
    (组分、size 都会变),按件筛等于让同一个品牌在不同店之间被拆开,破坏排他。

    渠道整组同进退(建组时已按多数派统一过);**类目与配送时长逐件判** ——
    一个品牌横跨两个大类、或者快慢货混在一起时,占用店收得了的那部分
    **本来就能上架**,不该被组里的多数派连累。

    全被剪光时返回 `(None, 件数, 原因)`,**原因要分清是类目还是货期**:
    两者的处置完全不同(开个大类 vs 放宽货期或换货),混成一个标签会把
    所有者送去改根本没用的那一项 —— 2026-08-16 实测,货期闸上线后被它挡下的
    组曾一律被记成"缺某大类"。
    """
    bad: Counter = Counter()
    ok = []
    for it in grp["items"]:
        if not store_targets.allowed(st_cfg(st), it["category"]):
            bad["类目"] += 1
        elif not store_targets.lead_ok({"lead_limit": st.get("lead_limit")},
                                       it.get("lead")):
            bad["货期"] += 1
        else:
            ok.append(it)
    if not ok:
        # 并列时按名字定序 —— 报告里的归因不许随行序漂
        top = sorted(bad.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        return None, grp["size"], top
    if len(ok) == len(grp["items"]):
        return grp, 0, None
    leads = [x.get("lead") for x in ok]
    return {**grp, "items": ok, "size": len(ok),
            "score": max(x["score"] for x in ok),
            "category": alloc_groups._major(x["category"] for x in ok),
            "lead": None if any(v is None for v in leads) else max(leads)}, \
        grp["size"] - len(ok), None


def _quota(qq: dict, m: dict, target) -> tuple[int, str]:
    """输入:配额输入 + 经营指标 + 该店日目标 → 输出:(本轮能接几个货位, 依据)。

    所有者定稿 2026-08-16(推翻原来的"从批量里按 need 分成"):
    **每家店自己能接多少就是多少,不从一个人为总数里切**。系统已经知道
    容量上限、当前在线、缺口,再套一个 `batch` 只会跟这些数打架。

        缺口货位数 = (日目标 − 日均净额实测) ÷ 单品日产出
        配额       = min(剩余容量, 缺口货位数)

    所有者原话:「上限 3500,在线 2200,还有 1300 个位置,按缺口算出来是 1500,
    但也只能上架 1300 个」——**容量是硬的,缺口是想要的,取小**。

    ⚠ 缺口换算成货位要除以**单品日产出**(收缩后,§7.4g #6a):它回答"这家店
    一个货位一天值多少钱",所以"还差多少钱 ÷ 一个货位值多少钱 = 还差多少货位"。
    ⚠ 已提示过的风险不再重复(§7.4a):除以货位值意味着**卖得越差的店要的货
    越多**。所有者已知情并选择缺口优先。
    ⚠ 算不出缺口(没填日目标)或算不出货位值时**退回剩余容量**,不是退回 0:
    「单店最大在线数」是所有者显式填的上限,拿它当界是尊重设置;退回 0 会让
    没填目标的店永远分不到货,而且不报错。报告会点名这些店。
    """
    room = int(qq.get("room") or 0)
    gap, slot = qq.get("gap"), m.get("slot_value")
    if gap is None or not slot or not target:
        return room, "剩余容量(缺口或货位值算不出)"
    want = math.ceil(gap * target / slot)        # gap 是比例,×日目标 = 缺口金额
    return (min(room, want),
            "剩余容量(缺口要得更多)" if want > room else "缺口换算")


def st_cfg(st: dict) -> dict:
    """输入:引擎口径的店铺行 → 输出:`store_targets.allowed` 认得的配置行。

    两处对类目的表示必须走同一个判定函数,不能在这里另写 `in` —— 「三列全空 =
    不限制」这条正着写反着写都像对的,判定只留一处(store_targets.allowed)。
    """
    return {"categories": st.get("categories") or []}


def run(params: dict) -> str:
    """输入:params(batch/days/as_of/export/execute)→ 输出:方案摘要。"""
    execute = bool(params.get("execute"))
    # 默认**不设总量上限**:每家店能接多少由容量与缺口算出来(见 `_quota`)。
    # `-p batch=` 是想小步试跑时的安全阀,不是模型的一部分
    batch = int(params.get("batch", 0)) or None
    # ★ **两个窗口,不是一个**(所有者定稿 2026-08-16):
    #   days       店铺经营水平 —— 要"这家店**现在**什么水平",90 天;
    #   sales_days 产品销量信号 —— 要"这个品到底卖没卖过",近一年。
    # 合成一个的话:窗口取 90 天则产品侧覆盖率只有 1.0%(信号形同虚设),
    # 取 365 天则店铺的缺口与货位值被一年前的经营状况稀释。
    days = int(params.get("days", 90))
    sales_days = int(params.get("sales_days", ps.SALES_WINDOW_DAYS))
    as_of = str(params.get("as_of", ""))
    win = sv.sales_window(as_of, days)                    # 店铺侧
    pwin = sv.sales_window(as_of, sales_days)             # 产品侧
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
        data = product_pool.load(conn, pwin)
        perf_raw = store_perf.load(conn, win)
        with conn.cursor() as cur:
            cur.execute(_SQL_ONLINE_NOW)
            online_now = {s: int(n) for s, n in cur.fetchall()}
        held_brand = claims.load_active(conn, claims.BRAND)
        held_prod = claims.load_active(conn, claims.PRODUCT)
        pending = _pending_delist(conn, cfg, registered)
        listed = _listed_asins(conn, registered)

    # ── 候选漏斗 ──────────────────────────────────────────────────────
    scored, gated = product_pool.score_all(data)
    # ⚠ 漏斗前四行的单位是**产品**,后两行是**组**。混在一列里报会骗人:
    # "60 个产品 → 20 组"会显示成 33.3%,读起来像丢了三分之二的货,
    # 其实一件没丢(每组 3 件)。所以组那两行同时给组数与货位数
    funnel = [("候选池", len(data["pool"])), ("过硬闸", len(scored))]
    live = [c for c in scored if c["score"] >= ps.CUTOFF]
    funnel.append((f"≥ 淘汰线 {ps.CUTOFF:.0f}", len(live)))
    # ★ 排除的是**已在架**的,不是已占位的。占位了没上架的照常进池,
    #   只是只能回它的占用店(见 alloc_groups.build 的 bound_asins)
    live = [c for c in live if c["asin"] not in listed]
    funnel.append(("去掉已在架 ASIN", len(live)))

    # 已占位但还没上架 ⇒ 绑定到占用店。这批是上一轮定了、还没执行的上架指令
    bound = {a: s for a, s in held_prod.items() if a not in listed}
    g = alloc_groups.build(live, held_brand, bound)
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
                     "lead_limit": (cfg.get(s) or {}).get("lead_limit"),
                     "fit": 0.0, "tier": 1 if online_now.get(s) else 2}
    if not stores:
        return ("⛔ 没有一家店可以接货(在册 ∧ 规划内 ∧「单店最大在线数」> 0)。"
                "先跑 alloc_stores 看是谁被挡下的")

    # ── 配额:每家店自己能接多少,**不从一个人为总数里分**(所有者定稿
    #    2026-08-16:"限制 3000 设置的也很奇怪…这个限制我甚至就认为不应该有")──
    basis, no_gap, at_target = {}, [], []
    for s in stores:
        stores[s]["quota"], basis[s] = _quota(
            q[s], metrics.get(s, {}), (cfg.get(s) or {}).get("gmv"))
        if q[s].get("gap") is None:
            no_gap.append(s)
        elif stores[s]["quota"] == 0 and (q[s].get("room") or 0) > 0:
            at_target.append(s)
    # `-p batch=` 只是**可选的安全阀**(想先小步试跑时用),不是模型的一部分。
    # 给了就按比例等比缩,保持各店之间的形状不变
    total_q = sum(v["quota"] for v in stores.values())
    if batch and total_q > batch:
        for s in stores:
            stores[s]["quota"] = math.ceil(stores[s]["quota"] * batch / total_q)
        total_q = sum(v["quota"] for v in stores.values())

    # ── 一副牌:定向流就是"只有一家店能要"的那种牌,不是另一条流水线 ──────
    # 分成两个阶段的实测后果(2026-08-16):两边各有一套配额与容量记账,
    # 谁也不知道对方吃了多少 —— 定向流一口吃光批量、容量闸各判各的双双超容。
    # 合成一副之后,`alloc_engine._gate` 的归属闸自然处理,记账只有一处。
    deck_all, dir_out, dir_trim = list(free), [], 0
    for orig in directed:
        st = stores.get(orig["store"])
        if not st:
            dir_out.append((orig, "占用店本批不接货"))
            continue
        # ⚠ **容量闸必须压在类目/货期之上**。店已经满了的时候,类目对不对根本
        # 不影响结果 —— 开大类一件也救不回来,得先下架腾位。不这么排的实测后果
        # (2026-08-16):A154杨凯迪 剩余容量 0,报告却把它记成「缺 Arts & Crafts
        # 161 件 / 缺 Garden & Patio 113 件」,还附一句"给该店开这个大类就能救回"
        # —— 把所有者送去做一件买不到任何货位的事。而且同一家满店,类目**碰巧
        # 对上**的那些组走的是另一条路(进牌堆 → 容量塞不下 → "等下架腾位"),
        # 同一个原因两种说法。
        if st["room"] <= 0:
            dir_out.append((orig, "占用店容量已满"))
            continue
        # ★ 定向流按**件**筛,不整组淘汰(§7.3 那句"整组淘汰"写的是自由流的
        #   竞争场景)。去向店已被品牌占用**固定死**,没有"该给谁"的问题,
        #   所以留下该店收得了的那些件、其余才淘汰。不这么做的话:一个品牌
        #   60% 厨房 / 40% 家居,组大类取多数派=厨房,只做家居的占用店会把
        #   **那 40% 本来能上架的家居商品一起拒掉**。
        grp, trimmed, blocker = _fit_to_store(orig, st)
        dir_trim += trimmed
        if grp is None:
            dir_out.append((orig, f"占用店{blocker}不符"))
        elif not ae._gate(grp, orig["store"], st):
            # 逐件筛过之后还过不了 = 组级的渠道闸(渠道整组同进退,剪不动)
            dir_out.append((grp, "占用店渠道不符"))
        else:
            deck_all.append(grp)            # 带着 store 进牌堆,归属闸会认它

    cut = int(total_q * HEADROOM)
    pool_sorted = sorted(deck_all, key=lambda x: (-x["score"], x["key"]))
    deck, below_cut = pool_sorted[:cut], pool_sorted[cut:]
    result = ae.deal(deck, stores) if deck else {
        "assign": [], "unplaced": [], "by_store": {}, "layers": [],
        "params": {"thickness": ae.LAYER_THICKNESS, "slack": ae.LAYER_SLACK,
                   "stores": 0, "skipped_stores": len(stores)}}
    acc = ae.acceptance(result)
    dir_items = sum(int(a["group"]["size"]) for a in result["assign"]
                    if a["group"].get("store"))
    placed_items = sum(v["items"] for v in result["by_store"].values())
    L = ["", "═══ 分配方案 ═══", "",
         f"▍本轮可分 {total_q:,} 个货位 = {len(stores)} 家店各自"
         f"「min(剩余容量, 缺口 ÷ 单品日产出)」之和",
         f"  实发 {placed_items:,} 个货位(其中定向流 {dir_items:,} = 补齐已占品牌,"
         f"自由流 {placed_items - dir_items:,} = 拓新品牌)"
         + (f";⚠ `-p batch={batch:,}` 把配额等比缩过" if batch else ""),
         f"  候选切口 {cut:,} 组(可分 ×{HEADROOM});窗口 {win['day']} 往前 ——"
         f"店铺经营水平 {days} 天、产品销量信号 {sales_days} 天"]
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

    # ★ **真正的入场线是"最后发出去的那张牌的分",不是候选切口**。
    #   实测 2026-08-16:切口 42,705 组(组分 43.0),但配额在第 6,517 张牌上
    #   就填满了 —— 只报切口会让人以为 43 分的货都进来了,差着十几分。
    #   所有者按"每层 23,000 个、要选两万多"推算时,用的就是那个错前提。
    dealt = sorted((a["group"]["score"] for a in result["assign"]))
    L += ["", f"▍发牌结果:{len(result['assign']):,} 组 / {placed_items:,} 个货位"
          + (f";**实际入场线:组分 {dealt[0]:.1f}**(最后发出的那张牌),"
             f"平均每组 {placed_items / len(dealt):.1f} 件" if dealt else "")]
    dir_by_store: Counter = Counter()
    for a in result["assign"]:
        if a["group"].get("store"):
            dir_by_store[a["store"]] += int(a["group"]["size"])
    rows_tbl = []
    for s in sorted(stores, key=lambda x: -result["by_store"].get(x, {}).get("items", 0)):
        b, a = result["by_store"].get(s, {}), acc.get(s, {})
        got = b.get("items", 0)
        rows_tbl.append([s, f"{stores[s]['room']:,}", f"{stores[s]['quota']:,}",
                         f"{got:,}", f"{dir_by_store[s]:,}",
                         f"{stores[s]['room'] - got:,}",
                         ("—" if a.get("top_ratio") is None
                          else f"{a['top_ratio']:.2f}"),
                         basis[s],
                         "⚠ 独吞" if a.get("over_cap") else
                         ("⚠ 顶层越界" if a.get("top_ratio") is not None
                          and not 0.7 <= a["top_ratio"] <= 1.3 else "")])
    L += textfmt.table(["店铺", "剩余容量", "本轮配额", "分到", "其中定向",
                        "分完还剩", "顶层比值", "配额依据", ""],
                       rows_tbl, align="<>>>>>><<")
    L.append("  顶层比值 = 「这家店拿到的**自由流**货里顶层占多大比例」÷"
             "「全体的同一比例」—— 1.0 = 质量构成与平均一致,1.5 = 好货比例是"
             "平均的一倍半。**要落 [0.7, 1.3]**(§7.4b)")
    L.append(f"  ⚠ 分母是**它自己拿到的自由流总量**,不是配额:定向流只有占用店"
             f"能要、跳过排队,拿配额当分母会让被定向流填满的店比值趋近 0 而被"
             f"误标越界。自由流不足 {ae.MIN_FREE_FOR_RATIO} 件、"
             f"或不足自己配额 {ae.MIN_FREE_SHARE_OF_QUOTA:.0%} 的店不出比值(—):"
             f"量太小时比值是数学假象(全落在 L1 就恒等于 1÷base),不是信号。"
             f"总量独吞看「分到」与「本轮配额」两列")
    # ⚠ 「顶层越界」只说了**有问题**,没说问题在哪。实测 2026-08-16:两家店
    # 比值 0.00(自由流各 3,868 / 2,139 件,远超小样本门槛),而报告里查不出
    # 它们的货究竟来自哪几层 —— 只能手工翻方案表的「层」列。越界的店直接把
    # 自由流层分布摊开:**全堆在末尾几层 = 这家店过不了前面几层的闸**
    # (类目/渠道/货期太窄,前面的牌它一张都要不了),不是运气。
    bad = [s for s in stores
           if acc.get(s, {}).get("top_ratio") is not None
           and not 0.7 <= acc[s]["top_ratio"] <= 1.3]
    if bad:
        L.append("  越界店的自由流层分布(层号×件数,层号越小分越高;"
                 "末位那层是发完所有层之后的兜底扫尾):")
        for s in sorted(bad, key=lambda x: acc[x]["top_ratio"]):
            hist = result["by_store"].get(s, {}).get("by_layer_free") or {}
            L.append(f"    {s}(比值 {acc[s]['top_ratio']:.2f}):"
                     + (" ".join(f"L{li}×{n:,}" for li, n
                                 in sorted(hist.items())) or "无自由流"))
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
    # ★ 「我那个高分品怎么没分出去」必须答得上来(所有者 2026-08-16 追问)。
    # 分数最高的组也可能只是**排在切口之外** —— 它既不在方案表也不在未入选表,
    # 哪儿都查不到。把切口位置显式报出来:低于这条线的就是"排队中",不是被闸挡了
    if below_cut:
        L.append(f"  牌堆 {len(deck_all):,} 组,本轮只取前 {len(deck):,} 组"
                 f"(可分 {total_q:,} ×{HEADROOM});"
                 + (f"候选切口在组分 {deck[-1]['score']:.1f} —— ⚠ **这不是入场线**,"
                    f"配额早在那之前就填满了(入场线见上一节);"
                    f"低于切口的 {len(below_cut):,} 组是**排队中**"
                    f"(连候选都没进,不是被闸挡了 —— 两者处置不同)"
                    if deck else
                    f"**本轮可分为 0,{len(below_cut):,} 组一件都没发** —— "
                    f"所有店的剩余容量或缺口都是 0,先下架腾位"))
    # ★ 排序按**组分**(组内最高产品分),而一张牌是整个品牌组 —— 组里分低的
    # 产品**跟着一起上架**。所有者 2026-08-16 追问「产品分 38.6 和 91.4 怎么混
    # 到一起去的」正是这件事。它不是 bug(品牌排他要求整组去一家店),但
    # **这些货位挤掉的是排队里组分更高的组**,所以得把量报出来让人判断值不值。
    # ⚠ 曾试过"组内低于 50 分的不跟车"来压它,已撤(口径 #17c):牌一瘦,填同样
    # 的货位要发更多张牌,发牌深度反而从 1.5 层掉到 3.5 层,入场线不升反降。
    if deck:
        floor = deck[-1]["score"]
        ride = sum(1 for a in result["assign"] for it in a["group"]["items"]
                   if it["score"] < floor)
        if ride:
            L.append(f"  其中 {ride:,} 个货位是**跟着同品牌高分品搭车上架**的"
                     f"(产品分低于本轮切口 {floor:.1f})—— 品牌排他要求整组去"
                     f"一家店,但它们挤掉的是排队里组分更高的组。方案表的"
                     f"「组分 / 组件数 / 产品分」三列并排,可以逐组核")
    if dir_trim:
        L.append(f"  定向流按件筛掉 {dir_trim:,} 件(品牌的类目跨度或货期超出占用店的准入);"
                 f"**同组里占用店收得了的那些件照常发** —— 不因为组里多数派是别的"
                 f"大类就整组扔掉")
    if dir_out:
        L.append(f"  定向流淘汰 {len(dir_out):,} 组 / "
                 f"{sum(x['size'] for x, _ in dir_out):,} 件"
                 f"(品牌已被占,但**去不了**占用店):"
                 + " · ".join(f"{k} {v}" for k, v in
                              Counter(w for _, w in dir_out).most_common())
                 + " —— 除「容量已满」外,这批要你去改配置或释放品牌,"
                   "不是等下一批就能好;**满店那部分反过来**:配置一个字都不用改,"
                   "下架腾出容量下一轮自然进来")
        # 光给总数没法动手。按「店 × 缺的大类」摊开,所有者一眼看出
        # "给 A085 开厨房能救回多少件" —— 那是他真能做的决定
        # ⚠ 归因必须按**真实原因**分。三个闸的处置完全不同(开个大类 /
        # 放宽货期 / 换渠道),混在一起报会把所有者送去改根本没用的那一项
        blocked: Counter = Counter()
        slow: Counter = Counter()
        full: Counter = Counter()
        for grp, w in dir_out:
            if w == "占用店类目不符":
                blocked[(grp["store"], grp["category"] or "(未归类)")] += grp["size"]
            elif w == "占用店货期不符":
                slow[grp["store"]] += grp["size"]
            elif w == "占用店容量已满":
                full[grp["store"]] += grp["size"]
        if full:
            L.append("  其中**容量已满**挡下的,按店:"
                     + " · ".join(f"{s} {n:,} 件" for s, n in full.most_common(6))
                     + " —— 这些店的剩余容量是 0,**开类目/放宽货期都救不回**,"
                       "只能先下架腾位(它们的类目配置对不对,等有容量了再看)")
        if blocked:
            L.append("  其中**类目**挡下的,按「店 × 缺的大类」:"
                     + " · ".join(f"{s} 缺「{c}」{n:,} 件"
                                  for (s, c), n in blocked.most_common(6))
                     + f"(共 {len(blocked)} 组合)—— 给该店开这个大类就能救回")
        if slow:
            L.append("  其中**货期**挡下的,按店:"
                     + " · ".join(f"{s} {n:,} 件" for s, n in slow.most_common(6))
                     + " —— 放宽该店「配送时长限制」或换货源才救得回,开类目没用")
    if bound:
        L.append(f"  其中 {len(bound):,} 个 ASIN 是**上一轮定了、还没上架**的,"
                 f"本轮照常参与(只能回占用店)—— 占位不等于上架,方案表始终是"
                 f"「现在该上什么」的完整清单,不是增量")
    if no_gap:
        L.append(f"  ⚠ {len(no_gap)} 家没填日目标销售额(或算不出货位值),"
                 f"**配额退回剩余容量** —— 它们能接多少全看容量,与经营水平无关:"
                 + "、".join(no_gap[:6]))
    if at_target:
        L.append(f"  {len(at_target)} 家已达日目标,本轮配额 0(所有者口径:"
                 f"把货给离目标最远的店):" + "、".join(at_target[:6]))

    to_claim = _to_claim(result["assign"])
    if not export and not execute:
        return "\n".join(L)

    if export:
        paths.reports_dir().mkdir(parents=True, exist_ok=True)
        # ⚠ **要动手的**和**诊断用的**分两张表。合成一张的实测后果:批量 3,000
        # 却出了 48,816 行,其中 45,815 行是排队与淘汰 —— 那张表没法用,而所有者
        # 第一眼看到的就是那个总行数。摘要里两个数都报,不存在"藏起来"的问题
        p_plan, n_plan = _write_plan(result["assign"])
        p_out, n_out = _write_rejects(result["unplaced"], dir_out,
                                      below_cut[:QUEUE_SAMPLE])
        L += ["", f"▍要上架的 {n_plan:,} 行 → {p_plan}",
              "  逐产品一行(品牌组 / 组分 / 去向店 / 逐段得分 / 层号 / 流别)。"
              "**先看上面的验收指标再看明细** —— 一家独吞是参数错了,不是模型判断"]
        if n_out:
            L += [f"▍没进这一批的 {n_out:,} 行 → {p_out}",
                  "  「排队」等店铺腾出容量、或缺口变大,下一轮自然轮到;"
                  "「淘汰」要你改配置或释放品牌;「未发出」看原因列"]
            if len(below_cut) > QUEUE_SAMPLE:
                L.append(f"  ⚠ 排队的只写了**组分最高的 {QUEUE_SAMPLE:,} 组**"
                         f"(共 {len(below_cut):,} 组)—— 全写这张表要十万行。"
                         f"要看更靠后的,先下架腾出容量让切口下移")

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


def _to_claim(assign: list) -> list:
    """输入:发牌结果 → 输出:待落占用行。

    ⚠ **定向流的组不再落品牌占用**:它带着 `store` 进的牌堆,就是因为品牌
    已经被占了。重复落不会出错(ON CONFLICT DO NOTHING),但会让"落库 N 条"
    这个数对不上人能数出来的东西,而那是所有者唯一的核对手段。
    """
    out = []
    for a in assign:
        grp, store = a["group"], a["store"]
        if grp.get("brand") and not grp.get("store"):
            out.append({"kind": claims.BRAND, "claim_key": grp["brand"],
                        "store": store, "source": SOURCE})
        out += _prod_claims(grp, store)
    return out


def _prod_claims(grp: dict, store: str) -> list:
    return [{"kind": claims.PRODUCT, "claim_key": it["asin"], "store": store,
             "source": SOURCE, "walmart_pt": it.get("pt"), "pt_source": None}
            for it in grp["items"]]


# ⚠ 与 `alloc_产品分.csv` 的同名列**必须是同一个数**(同一个 product_pool
# 取数、同一个窗口常量)—— 两张表对不上账时,人第一个怀疑的就是分配算错了
_HEADER = ["流别", "去向店", "层", "品牌组", "组分", "组件数", "大类", "渠道",
           "ASIN", "产品分", "口碑分", "销量加分", "罚分", "罚分原因",
           "售价", "运费", "落地价", "窗口销量(件)", "窗口销售额(毛额)",
           "评分", "评论数", "配送天数"]


def _write_plan(assign) -> tuple[str, int]:
    """输入:发牌结果 → 输出:(路径, 产品行数)。**只放要上架的。**"""
    p = paths.reports_dir() / "alloc_分配方案.csv"
    n = 0
    with p.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(_HEADER)
        for a in sorted(assign, key=lambda x: (x["layer"], -x["group"]["score"])):
            flow = "定向流" if a["group"].get("store") else "自由流"
            n += _rows(w, flow, a["store"], a["layer"], a["group"])
    return str(p), n


def _write_rejects(unplaced, dir_out, queued=()) -> tuple[str, int]:
    """输入:三类没进这一轮的 → 输出:(路径, 产品行数)。

    与方案表分开,是因为它们的处置**完全不同**:排队的下一批加大 batch 就能发,
    淘汰的要所有者改配置或释放品牌。混在一张表里,3,000 行要动手的会被 45,815 行
    诊断淹掉(2026-08-16 实测:所有者第一眼看到的就是 48,816 这个总行数)。

    `queued` 是**排在切口之外的自由流组**(按组分降序,只写前 `QUEUE_SAMPLE` 组)。
    不写的话,"我那个高分品怎么没分出去"在两张表里都查不到 —— 那才是真的藏。
    全写又会把这张表撑到十万行,所以取头部并在摘要里说明截断。
    """
    p = paths.reports_dir() / "alloc_未入选.csv"
    n = 0
    with p.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(_HEADER)
        for grp, why in dir_out:
            n += _rows(w, f"定向流淘汰({why})", grp["store"], "", grp)
        for u in unplaced:
            n += _rows(w, f"未发出({ae.REASON_LABEL[u['reason']]})", "", "",
                       u["group"])
        for grp in queued:
            n += _rows(w, "排队(分数排在本轮切口之外)",
                       grp.get("store") or "", "", grp)
    return str(p), n


def _rows(w, flow, store, layer, grp) -> int:
    for it in grp["items"]:
        w.writerow([flow, store, layer, grp["key"], round(grp["score"], 1),
                    grp["size"], grp["category"], grp["channel"], it["asin"],
                    round(it["score"], 1), round(it["base"], 1),
                    round(it["bonus"], 1), round(it["penalty"], 1), it["why"],
                    it.get("price"), it.get("shipping"),
                    None if it.get("price") is None or it.get("shipping") is None
                    else round(it["price"] + it["shipping"], 2),
                    it["sales"], round(it.get("gross") or 0, 2),
                    it["rating"], it["reviews"], it["lead"]])
    return len(grp["items"])
