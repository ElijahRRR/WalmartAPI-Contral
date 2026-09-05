"""alloc_plan — 产品分配方案(§七)。**危险:缺省即真跑,空跑用 `--dry-run`。**

用法:
  python cli.py alloc_plan --dry-run             # 只出方案表,不落占用
  python cli.py alloc_plan                       # 各店按自己的容量与缺口分满 + 落占用
  python cli.py alloc_plan -p batch=3000         # 安全阀:总量封顶,等比缩配额
  python cli.py alloc_plan -p as_of=2026-08-16   # 钉住销量窗口右端

把候选池打分、组队、切批、发牌,产出**分配方案表**。真跑只多做一件事:
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

import logging
import math
from collections import Counter

from registry import db, resources
from services import alloc_engine as ae
from services import alloc_groups, alloc_survey as sv
from services import claims, product_pool, product_score as ps
from services import report_csv
from services import store_events as se
from services import store_perf, store_targets, stores as stores_svc
from services import textfmt

DANGEROUS = True

logger = logging.getLogger("workflows.alloc_plan")

SOURCE = "alloc_plan"

# ⚠ `HEADROOM`(候选切口倍数 1.5)2026-08-22 **删掉了**。它有两个毛病:
#   · **单位错配** —— `cut = int(total_q * HEADROOM)` 里 total_q 的单位是
#     货位(产品),却拿去切**组**的列表。平均一组 k 件,牌堆实际装了约
#     1.5×k×total_q 件货,比标称多出 k 倍,而报告还写着"切口 N 组(×1.5)";
#   · **留余量这件事本身**已由"一轮不够就取下一个 N"取代(所有者 2026-08-22),
#     那是按实际缺口续取,比拍一个倍数准。

# 未入选表里,自由流"排队中"最多写多少组(按组分降序取头部)。
# 全写会把这张表撑到十万行;一组不写则"我那个高分品怎么没分出去"两张表里
# 都查不到 —— 头部是所有者真正会翻的那一段。
QUEUE_SAMPLE = 2000

# 归不到五品类的那一桶在画像里的名字 —— 直接用 registry 的常量,不另起一个。
# 2026-08-22 之前这里自造了「(不归五品类)」,而所有者管它叫「其他」并且要
# 能填进限额表:同一个桶两个名字,报告说的和表里填的对不上。
NOT_SUPER = resources.SUPER_OTHER

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
    # 只要装载口的公共骨架(PT→大类 + 在线行 + meta),`need` 一项都不点名:
    # 本件不看 pool/signal/status/sales,更不要 order_stores。
    # 必须带渠道:`offends_channel` 判不了的话预支只剩类目那一半,且不报错
    rows = sv.load_rows(conn, with_channel=True).rows
    slot = sv.claimable(rows, registered)
    out: Counter = Counter()
    for r in slot:
        # 去重:一行同时踩两道闸也只空出一个货位。不去重会把 room 高估
        if sv.offends_category(r, cfg) or sv.offends_channel(r, cfg):
            out[r["store"]] += 1
    return dict(out)


# published_status 那一条是本工作流自己的口径(不同于 alloc_push 的排 RETIRED)。
# 身份键经登记簿 amz 行,存量下 source_key = sku,集合逐个相同。
_SQL_ONLINE_SKU = """
SELECT w.store, w.sku, ls.source_key
FROM catalog.walmart_items w
LEFT JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz'
WHERE w.missing_since IS NULL AND w.published_status = 'PUBLISHED'
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
    return {a for store, sku, k in rows
            if store in registered and not sv.is_excluded(store)
            and (a := sku_asin.pick_asin(k, sku))}


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
        registered = stores_svc.enabled_names()
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

    # ── 店铺(先建,漏斗要用它们的条件)──────────────────────────────
    # ⚠ **顺序是有意的**:候选池的准入条件全部来自这批店的限额表行(渠道 /
    #   配送时长 / 准入类目),所以店必须先建好。所有者 2026-08-21 原话:
    #   "每一个店,对于配送时间的限制、配送方式的限制都在表格里……分配的时候
    #   会读取表,这些信息都能拿到,再拿着条件去拿品过来分配"。
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

    # ── 配额(**在漏斗之前算**,所有者 2026-08-22 纠正)────────────────
    # 为什么必须先算:切口的单位是**产品**,数量 = 各店配额之和。不先算配额
    # 就不知道该从候选池里取多少件,只能像旧版那样在组这一层拍一个倍数 ——
    # 而组分取最高分时,那等于把一堆低分品跟着高分同门牌一起拉进牌堆。
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
    # ★ **没有任何店的条件容得下的货,不进牌堆**(所有者 2026-08-21)。
    #   条件全在限额表同一行里(渠道 / 配送时长 / 准入类目),拿着条件去取品。
    live, out_of_reach = _in_reach(live, _pool_reach(stores))
    if out_of_reach:
        detail = " / ".join(f"{k} {v:,}" for k, v in out_of_reach)
        funnel.append((f"去掉没有店要的({detail})", len(live)))

    # 已占位但还没上架 ⇒ 绑定到占用店。这批是上一轮定了、还没执行的上架指令
    bound = {a: s for a, s in held_prod.items() if a not in listed}

    # ── 发牌:分片 → 片内组队 → 轮转(全在 alloc_engine 里,纯函数)──────
    # ★ 组队**不在这里**做了(所有者 2026-08-22 纠正)。旧版在这里全池组队、
    #   再按组分切口,于是"一个 95 分带四十个 41 分"的品牌整组排进前排。
    #   现在切口在产品层,组队挪进片内 —— 见 alloc_engine.deal 的 docstring。
    result = ae.deal(live, stores, held_brand=held_brand, bound=bound)
    dir_out, dir_trim = result["dir_out"], result["dir_trim"]
    below_cut = result["queued"]
    free = [g for g in result["groups"] if not g.get("store")]
    directed = [g for g in result["groups"] if g.get("store")]
    # ⚠ 定向流分两种,**不许合并报**:
    #   · 已占品牌 —— 所有者手上既有的占用,这是业务事实;
    #   · 同轮续发 —— 同一个品牌被切口切在两片里,第二片看到它已经有主了。
    #     那是本轮自己刚发的,是分片的机械后果。合起来报的话,一条占用都没有
    #     的时候报告也会说"定向流 45 件(补齐已占品牌)",纯属误导
    held_grp = [g for g in directed if g.get("bound_by") == "claim"]
    same_rnd = [g for g in directed if g.get("bound_by") != "claim"]
    grouped = [("片内组队·自由流", len(free), sum(x["size"] for x in free)),
               ("片内组队·定向流(已占品牌)", len(held_grp),
                sum(x["size"] for x in held_grp))]
    if same_rnd:
        grouped.append(("片内组队·同轮续发(品牌被切口切在两片)", len(same_rnd),
                        sum(x["size"] for x in same_rnd)))
    # 画像在**剪之前**算:定向流会按件剪掉少数派(`ae._fit_to_store`),
    # 剪完再量等于量自己的处置结果,那个数永远好看
    brand_lines, brand_rows = _brand_profile(free, directed)

    acc = ae.acceptance(result)
    taken = sum(r["taken"] for r in result["rounds"])
    dir_items = sum(int(a["group"]["size"]) for a in result["assign"]
                    if a["group"].get("bound_by") == "claim")
    rnd_items = sum(int(a["group"]["size"]) for a in result["assign"]
                    if a["group"].get("store")
                    and a["group"].get("bound_by") != "claim")
    placed_items = sum(v["items"] for v in result["by_store"].values())
    L = ["", "═══ 分配方案 ═══", "",
         f"▍本轮可分 {total_q:,} 个货位 = {len(stores)} 家店各自"
         f"「min(剩余容量, 缺口 ÷ 单品日产出)」之和",
         f"  实发 {placed_items:,} 个货位(定向流 {dir_items:,} = 补齐已占品牌"
         + (f",同轮续发 {rnd_items:,} = 品牌被切口切在两片" if rnd_items else "")
         + f",自由流 {placed_items - dir_items - rnd_items:,} = 拓新品牌)"
         + (f";⚠ `-p batch={batch:,}` 把配额等比缩过" if batch else ""),
         f"  取货 {taken:,} 件 / {result['params']['rounds']} 轮 × "
         f"{ae.SLICES} 片(每轮取「各店剩余差额之和」件,不够就再取一轮);"
         f"窗口 {win['day']} 往前 —— 店铺经营水平 {days} 天、"
         f"产品销量信号 {sales_days} 天"]
    L += _take_ledger(taken, placed_items, result)
    # ★ 「本轮取货」单独一行(2026-08-22)。不加这一行的话,漏斗从
    #   「去掉没有店要的 60 件」直接跳到「片内组队 12 件」,读起来像丢了 48 件
    #   —— 其实那是**切口**,它们在排队,下一轮/下一批照常轮到。
    funnel.append((f"本轮取货(N = 各店剩余差额之和,不够再取一轮)", taken))
    L += ["", "▍候选漏斗(前五行单位是**产品**,后两行是**组**——别拿组数除产品数)"]
    L += textfmt.table(
        ["", "产品", "占候选池", "组"],
        [[k, f"{v:,}", _pct(v, len(data["pool"])), ""] for k, v in funnel]
        + [[k, f"{n:,}", _pct(n, len(data["pool"])), f"{c:,}"]
           for k, c, n in grouped],
        align="<>><")
    if gated:
        L.append("  硬闸淘汰:" + " · ".join(f"{k} {v:,}"
                                                for k, v in sorted(gated.items())))
    if result["dropped"]:
        L.append("  组队时剔除:" + " · ".join(
            f"{k} {v:,}" for k, v in result["dropped"].most_common()))
    if data["risk_err"]:
        L.append(f"  ⚠ product_risk 读不到({data['risk_err']}):黑历史罚分全为 0")
    L += brand_lines

    L += _report_deal(result, acc, stores, basis)
    L += _report_ratio_gap(acc, result, stores)
    over = [s for s in stores if result["by_store"].get(s, {}).get("items", 0)
            > stores[s]["quota"]]
    if over:
        # 这不是 bug:组是原子的,配额在挑人**之前**判,所以最后一张牌可以把它
        # 顶过头(否则 40 件的组遇上剩 12 配额的店就永远发不出去)。不说破的话,
        # "配额 10 却分了 12"看起来像算错了
        L.append(f"  ({len(over)} 家分到的货位多于配额:组是原子的,配额在挑人**之前**判,"
                 f"最后一张牌可以顶过头;真正不许越的是剩余容量)")

    L += _report_unplaced(result, live, taken, below_cut)
    # ★ **「搭车上架」现在结构上不可能发生了**(2026-08-22 重排)。
    # 旧版的毛病:切口切在组这一层、组分取组内最高 ⇒ 一个 95 分的爆款能把
    # 同品牌四十个 41 分的货一起拉进牌堆,那些货位挤掉的是排队里更好的组。
    # 所有者 2026-08-16 追问「产品分 38.6 和 91.4 怎么混到一起去的」问的就是它。
    # 重排之后组只由**取到的产品**组成,而取到的全在切口之上 ⇒ 组内不可能有
    # 低于切口线的件。这一节因此撤掉 —— 报一个结构上恒为 0 的数只是噪声。
    # 不变量由 test_no_low_scorer_can_ride_along_any_more 钉着。
    if dir_trim:
        L.append(f"  定向流按件筛掉 {dir_trim:,} 件(品牌的类目跨度或货期超出占用店的准入);"
                 f"**同组里占用店收得了的那些件照常发** —— 不因为组里多数派是别的"
                 f"大类就整组扔掉")
    L += _report_dir_out(dir_out)
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
        # ⚠ **要动手的**和**诊断用的**分两张表。合成一张的实测后果:批量 3,000
        # 却出了 48,816 行,其中 45,815 行是排队与淘汰 —— 那张表没法用,而所有者
        # 第一眼看到的就是那个总行数。摘要里两个数都报,不存在"藏起来"的问题
        p_plan, n_plan = _write_plan(result["assign"])
        p_out, n_out = _write_rejects(result["unplaced"], dir_out,
                                      below_cut[:QUEUE_SAMPLE])
        p_brand, n_brand = _write_brands(brand_rows)
        L += ["", f"▍要上架的 {n_plan:,} 行 → {p_plan}",
              "  逐产品一行(品牌组 / 组分 / 去向店 / 逐段得分 / 层号 / 流别)。"
              "**先看上面的验收指标再看明细** —— 一家独吞是参数错了,不是模型判断"]
        if n_out:
            L += [f"▍没进这一批的 {n_out:,} 行 → {p_out}",
                  "  「排队」等店铺腾出容量、或缺口变大,下一轮自然轮到;"
                  "「淘汰」要你改配置或释放品牌;「未发出」看原因列"]
            if len(below_cut) > QUEUE_SAMPLE:
                L.append(f"  ⚠ 排队的只写了**产品分最高的 {QUEUE_SAMPLE:,} 件**"
                         f"(共 {len(below_cut):,} 件)—— 全写这张表要十万行。"
                         f"要看更靠后的,先下架腾出容量让切口下移")
        if n_brand:
            L += [f"▍横跨多个大类的品牌组 {n_brand:,} 组 → {p_brand}",
                  "  按**少数派件数**降序:排在最前面的就是"
                  "「一个品牌拖着一堆做不了的货」最严重的那些(§11.3 #5 要处置的)"]

    if not execute:
        L += ["", f"🧪 dry-run:未落任何占用。审完方案表后去掉 --dry-run 重跑"
              f"(将落品牌 {sum(1 for c in to_claim if c['kind'] == claims.BRAND):,}"
              f" + 产品 {sum(1 for c in to_claim if c['kind'] == claims.PRODUCT):,} 条)"]
        return "\n".join(L)

    with db.pg_conn() as conn:
        ok, conflicts, landed = claims.claim_many(conn, to_claim)
        # 店铺事件账本(治理类):每店一条,**同事务** —— 台账落了而事件没落
        # 的话,事后按事件流回查"这个品牌当初什么时候归的它"会查不到。
        # 计数只数 `landed`(真落库行):幂等重跑那些行本轮什么都没写
        se.record_many(conn, claims.claim_created_rows(landed, SOURCE))
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
def _pool_reach(stores: dict) -> dict:
    """输入:参与分配的店 → 输出:候选池准入条件的**并集**(限额表同一行三列)。

    所有者 2026-08-21 原话:「每一个店,对于配送时间的限制、配送方式的限制都在
    表格里,和该店的目标销量、销售额、产品数量限制、类目,都在同一个表里,
    分配的时候会读取表,这些信息都能拿到,再拿着条件去拿品过来分配」。

    所以这里**一个字面量都不写**,三条全从店铺行推:

    | 条件 | 并集怎么取 | 谁能让它失效 |
    |---|---|---|
    | 渠道 | 各店 `channel` 的集合 | 无(未填的店本就不接自由流) |
    | 货期 | 各店**生效上限**的 **max**(未填回落 7 天) | 无 —— 没有"不限"的店 |
    | 品类 | 各店准入品类的并集 | **任一店三列全空 = 不限制** ⇒ 整条不筛 |

    ⚠ 后两条的"任一店放开就整条不筛"不是偷懒,是**并集的定义**:只要存在
    一家店可能要它,这件货就有去处,池口没有资格替发牌阶段做决定。反过来写
    (取 min / 取交集)会把一家店的严格条件强加给所有店,静默丢货。

    ⚠ 用的是**参与分配的店**(在营 ∧ 规划内 ∧ participates),不是全部在营店:
    拿一家不接货的店的限额表行去放宽池口,等于让货进来又没人接。已满的店仍在这个集合里
    (`room` 是发牌阶段的事),所以"今天恰好满了"不会被读成"我们不做这个渠道"。
    """
    rows = list(stores.values())
    limits = [store_targets.lead_cap_of(r) for r in rows]
    cats = [store_targets.super_categories_of({"categories": r.get("categories")})
            if (r.get("categories") or []) else None for r in rows]
    return {
        "channels": {r.get("channel") for r in rows} - {None},
        # 未填回落 7 天(所有者 2026-08-21 统一到上架链的口径)⇒ 恒有上限
        "lead_cap": max(limits) if limits else None,
        # 三列全空 = 不限制(store_targets.allowed 的口径),同理
        "super_cats": None if any(c is None for c in cats)
                      else set().union(*cats) if cats else set(),
    }


def _in_reach(cands: list, reach: dict) -> tuple[list, list]:
    """输入:候选 + 准入并集 → 输出:(留下的, [(报告用标签, 件数), …])。

    ⚠ 归因**按第一道拦下它的条件**分,不合并成一个总数:三者的处置完全不同
    (开一家该渠道的店 / 放宽某店配送时长或换货源 / 给某店开这个品类),而且
    货期里还要再分「超期」与「没采到」—— 后者**补一次采集就能进池**。
    自由流的未发出归因刚为同一个毛病返过工(见 alloc_engine 常量段)。
    """
    kept, bad = [], Counter()
    cap, cats = reach["lead_cap"], reach["super_cats"]
    for c in cands:
        ch, lead = c.get("channel"), c.get("lead")
        if ch not in reach["channels"]:
            bad[f"渠道 {ch or '未知'}"] += 1
        elif cap is not None and lead is None:
            bad["配送天数没采到(补一次采集就能进池)"] += 1
        elif cap is not None and int(lead) > int(cap):
            bad[f"配送超 {int(cap)} 天(各店限制里最宽的那个)"] += 1
        elif cats is not None and resources.super_bucket(c.get("category")) not in cats:
            # ⚠ 与 `store_targets.allowed` 同一个折法(`super_bucket`)。
            # 这里用 `super_category` 的实测后果:填了「其他」的店在 allowed
            # 那边收得了 Everything Else,池口却把它当"归不到"筛掉 —— 那家店
            # 于是永远等不到它唯一能收的那批货,而且报告说的是"品类 归不到"
            bad[f"品类 {resources.super_bucket(c.get('category')) or '大类未知'}"] += 1
        else:
            kept.append(c)
    return kept, bad.most_common()


def _report_deal(result: dict, acc: dict, stores: dict, basis: dict) -> list[str]:
    """输入:发牌结果 + 验收指标 + 参与分配的店 + 配额依据 → 输出:▍发牌结果一节。"""
    # ⚠ 与 run 里的 `placed_items` **同源同式**(`result["by_store"]` 各店 items
    #   之和)。本节标题里的「个货位」与上面「实发 N 个货位」必须是同一个数,
    #   两处不一致等于报告自己跟自己对不上 —— 改一处必须改两处
    placed_items = sum(v["items"] for v in result["by_store"].values())
    # ★ **真正的入场线是"最后发出去的那张牌的分",不是候选切口**。
    #   实测 2026-08-16:切口 42,705 组(组分 43.0),但配额在第 6,517 张牌上
    #   就填满了 —— 只报切口会让人以为 43 分的货都进来了,差着十几分。
    #   所有者按"每层 23,000 个、要选两万多"推算时,用的就是那个错前提。
    dealt = sorted((a["group"]["score"] for a in result["assign"]))
    L = ["", f"▍发牌结果:{len(result['assign']):,} 组 / {placed_items:,} 个货位"
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
    return L


def _report_ratio_gap(acc: dict, result: dict, stores: dict) -> list[str]:
    """输入:验收指标 + 发牌结果 + 参与分配的店 → 输出:比值为空/越界的归因行。"""
    L: list[str] = []
    # ★ **全表 15 个「—」有两种完全不同的成因,不说破就分不出来**
    #   (2026-08-16 生产实测:定向流 25,420 / 自由流 3,078 那一跑,15 家全是「—」):
    #     ① 每家的自由流量都没过门槛 —— 那确实是"样本太小,别当信号";
    #     ② **顶层(L1)一件自由流都没有** ⇒ 全体占比 base = 0 ⇒ 除法没有分母,
    #        所有店一律 None。这时门槛过没过根本不影响结果。
    #   ② 不是"看不出问题",而是**反堆积这一轮压根没被验证过** —— 牌堆顶部被
    #   定向流吃光了。照 ① 去读会以为"量小而已,没事",那是读反了。
    tot_top = sum(v.get("top_items", 0) for v in acc.values())
    tot_own = sum(v.get("own_items", 0) for v in acc.values())
    if tot_own and not tot_top:
        L.append(f"  ⚠ **本轮全表无比值,原因是顶层一件自由流都没有**(自由流共 "
                 f"{tot_own:,} 件,全落在 L1 以下)——不是样本太小。牌堆顶部被"
                 f"定向流占满了,**反堆积这一轮没有被验证过**:好货有没有堆到"
                 f"一家店,这份报告答不了。要它重新有效,得先把已占位的货上架"
                 f"(占用消化掉,定向流才会退回去)")
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
    return L


def _report_unplaced(result: dict, live: list, taken: int,
                     below_cut: list) -> list[str]:
    """输入:发牌结果 + 候选 + 本轮取货量 + 切口外的产品 → 输出:未发出与切口说明。"""
    L: list[str] = []
    if result["unplaced"]:
        why = Counter(u["reason"] for u in result["unplaced"])
        # ⚠ **组数与件数一起报**(所有者 2026-08-22 追问「只有 26,785 个货位
        #   为什么取了 50,457 件」)。只报组数的话,这一行跟上面的「取货」
        #   「实发」不同单位,账对不上,人只能自己去减
        why_n = Counter()
        for u in result["unplaced"]:
            why_n[u["reason"]] += int(u["group"]["size"])
        L.append(f"  未发出 {len(result['unplaced']):,} 组 / "
                 f"{sum(why_n.values()):,} 件:"
                 + " · ".join(f"{ae.REASON_LABEL[k]} {why_n[k]:,} 件"
                              for k, _ in why.most_common()))
        L += _unplaced_breakdown(result["unplaced"])
    # ★ 「我那个高分品怎么没分出去」必须答得上来(所有者 2026-08-16 追问)。
    # 分数最高的品也可能只是**排在切口之外** —— 它既不在方案表也不在未入选表,
    # 哪儿都查不到。把切口位置显式报出来:低于这条线的就是"排队中",不是被闸挡了
    if below_cut:
        L.append(f"  候选 {len(live):,} 件,本轮取了 {taken:,} 件;"
                 + (f"切口在产品分 {below_cut[0]['score']:.1f} —— "
                    f"⚠ **这不是入场线**,配额早在那之前就填满了"
                    f"(入场线见上一节);低于切口的 {len(below_cut):,} 件是"
                    f"**排队中**(连片都没进,不是被闸挡了 —— 两者处置不同)"
                    if taken else
                    f"**本轮可分为 0,{len(below_cut):,} 件一件都没发** —— "
                    f"所有店的剩余容量或缺口都是 0,先下架腾位"))
    return L


def _report_dir_out(dir_out: list) -> list[str]:
    """输入:定向流淘汰的 (组, 原因) → 输出:总述 + 按真实拦路闸摊开的三段。"""
    if not dir_out:
        return []
    L: list[str] = []
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
    return L


def _unplaced_breakdown(unplaced: list) -> list[str]:
    """输入:自由流未发出的组 → 输出:按**真实拦路闸**摊开的摘要行。

    与定向流那边(`dir_out` 的三段)同一条纪律,只是维度不同:定向流的去向店
    是固定的,所以摊「店 × 缺的大类」;自由流没有固定去向,所以摊的是
    **这批货自己的属性** —— 缺哪个大类 / 卡在几天 / 差哪个渠道。

    ⚠ 光报总数没法动手。所有者能做的动作只有三种(给某店开大类 / 放宽某店
    的配送时长 / 给某店配上渠道),摘要就得按这三种摊开,否则他只能看着一个
    五万件的总数干瞪眼 —— 2026-08-21 实测,他照着旧摘要去开类目,而实际拦路
    的是货期,一件没救回来。
    """
    L: list[str] = []
    by_cat: Counter = Counter()
    by_lead: Counter = Counter()
    by_ch: Counter = Counter()
    for u in unplaced:
        g, n = u["group"], int(u["group"]["size"])
        if u["reason"] == ae.NO_CATEGORY:
            by_cat[g.get("category") or "(未归类)"] += n
        elif u["reason"] == ae.NO_LEAD:
            by_lead[g.get("lead")] += n
        elif u["reason"] == ae.NO_CHANNEL:
            by_ch[g.get("channel") or "(未知)"] += n
    if by_cat:
        L.append(f"  其中**类目**挡下的 {sum(by_cat.values()):,} 件,按缺的大类:"
                 + " · ".join(f"{c} {n:,} 件" for c, n in by_cat.most_common(8))
                 + f"(共 {len(by_cat)} 个大类)—— 给任意一家店开这个大类就能救回")
    if by_lead:
        # 货期 None = 组里有件没采到配送天数(§7.2 组货期取最长,任一件未知就整组未知)。
        # 它和"确实慢"的处置不同:前者补采集就好,后者要放宽限制或换货源
        unk = by_lead.pop(None, 0)
        if by_lead:
            L.append(f"  其中**货期**挡下的 {sum(by_lead.values()):,} 件,按组货期"
                     f"(= 组内最长的那一件):"
                     + " · ".join(f"{d} 天 {n:,} 件"
                                  for d, n in sorted(by_lead.items())[:8])
                     + " —— 放宽某店「配送时长限制」或换货源才救得回,**开类目没用**")
        if unk:
            L.append(f"  其中**货期未知**挡下的 {unk:,} 件:组里有件没采到配送天数,"
                     f"受限店一律拒收(§7.2「任一件采不到就整组算未知」)—— "
                     f"这批**补采集就能救**,不用改任何配置")
    if by_ch:
        L.append(f"  其中**渠道**挡下的 {sum(by_ch.values()):,} 件:"
                 + " · ".join(f"{c} {n:,} 件" for c, n in by_ch.most_common())
                 + " —— 没有一家参与分配的店把「配送限制」列填成这个渠道")
    return L


def _take_ledger(taken: int, placed: int, result: dict) -> list[str]:
    """输入:取货量 + 实发量 + 发牌结果 → 输出:**取货去哪了**的对账行。

    所有者 2026-08-22 追问:「只有 26,785 个货位为什么取了 50,457 件?」——
    因为取货是按**产品**取的,而取进来的产品未必发得出去:过不了闸、占用店
    不接、组队时被剔除。这三笔此前分散在报告的三个地方,且**单位还不一样**
    (未发出报的是组数),人只能自己去减。

    ⚠ 这条一定要**能对上**。对不上就说明有一笔货在实现里凭空消失了 ——
    那种 bug 从任何单项数字上都看不出来,只有做减法才会露头,所以差额不为 0
    时直接把它印出来,不许悄悄吞掉。
    """
    unplaced = sum(int(u["group"]["size"]) for u in result["unplaced"])
    dir_out = sum(int(g["size"]) for g, _ in result["dir_out"])
    trimmed = int(result["dir_trim"])
    dropped = int(sum(result["dropped"].values()))
    known = placed + unplaced + dir_out + trimmed + dropped
    parts = [("实发", placed), ("过不了闸(未发出)", unplaced),
             ("定向淘汰(去不了占用店)", dir_out),
             ("定向按件剪掉", trimmed), ("组队时剔除", dropped)]
    line = ("  取货去向:" + " + ".join(f"{k} {v:,}" for k, v in parts if v)
            + f" = {known:,}")
    out = [line if known == taken else
           line + f" ⚠ **对不上取货 {taken:,},差 {taken - known:,} 件**"
                  f" —— 这不该发生,有一笔货在实现里丢了"]
    if taken > placed:
        out.append(f"  ⇒ 取了 {taken:,} 件只发出 {placed:,} 件,是因为取货按"
                   f"**产品**取,而取进来的未必发得出去(上面那几笔)。"
                   f"每轮取「各店剩余差额之和」件,发不满就再往下取一轮 ——"
                   f"所以总取货量高于总货位数是**正常的**,它等于"
                   f"「为了填满配额一共往下翻了多深」")
    cap = result["params"].get("capped_tiers") or []
    if cap:
        out.append(f"  ⚠⚠ **梯队 {'、'.join(map(str, cap))} 撞到轮次护栏"
                   f"({result['params']['max_rounds']} 轮)就停了,不是发完了** ——"
                   f"配额还没填满、池子也还有货。上面的「未发出」因此**偏小**,"
                   f"少的那部分连片都没进。要么提高 alloc_engine.MAX_ROUNDS,"
                   f"要么先查为什么每轮发得这么少(通常是闸把大半挡住了)")
    return out


def _brand_profile(free: list, directed: list) -> tuple[list, list]:
    """输入:组队后的自由流 + 定向流 → 输出:(控制台行, csv 行)。

    **只读画像,不参与任何判断**,回答 §11.3 #5/#6 两条未决要的那几个数:
    组有多大、一个品牌横跨几个大类、**少数派件数**在两个口径下各是多少。

    少数派 = 组内不属于组大类的那些件。它们跟着品牌去了一家做不了这个大类的
    店,**既上不了架、又占着位置挡别人** —— 这个数就是方案 A 的标的。

    ⚠ 调用点在漏斗之后:进来的候选**都已经过了「至少有一家店的条件容得下」**
    那一闸(`_in_reach`)。所以少数派的语义是"**有店能收它,只是不是它品牌
    去的那家**",这正是方案 A 能救的那部分;一件都没店能收的货不在此列,
    它们的处置是开类目或放宽货期,漏斗里单独报。§11.6 的 105,571 是**全池**
    口径,比这个数大 —— 两者不能互相印证,也不该互相印证。

    ⚠ 两个口径都要报。26 类是代码现行判据,五品类是所有者心智里的大类
    (§11.6),两者差着约 5 万件 —— 只报一个,"折到上层能救回多少"这个问题
    就问不出来。
    ⚠ 归不到五品类的(Safety & Emergency / Everything Else)单列
    `resources.SUPER_OTHER`「其他」这一桶,不许并进别处 —— 并进去等于把
    "谁都能收"算成"某一家专收",正好反了。名字取自 registry:它同时是
    限额表里**可填**的值,报告与表格必须是同一个词。
    ⚠ 组大类不在这里重算:26 类那个直接用 `alloc_groups.build` 定的
    `g["category"]`,五品类那个走同一个 `_major`(并列按值定序)。画像与发牌
    对不上同一个组大类,这份画像就是假的。
    """
    real = ([(g, "自由流") for g in free if g["brand"]]
            + [(g, "定向流") for g in directed if g["brand"]])
    solo = [g for g in free if not g["brand"]] + \
           [g for g in directed if not g["brand"]]

    def _super(cat) -> str:
        # 走 `super_bucket`(与闸门同一个折法);它对空值回 None,
        # 而画像里"大类采不到"也该单独看得见,所以这里再兜一次
        return resources.super_bucket(cat) or NOT_SUPER

    buckets = (("1 件", 1, 1), ("2–5 件", 2, 5), ("6–20 件", 6, 20),
               ("21–100 件", 21, 100), ("100 件以上", 101, 10 ** 9))
    by_size = {b[0]: [0, 0] for b in buckets}          # 名 → [组数, 件数]
    sp26_g, sp26_i, sp5_g, sp5_i = (Counter() for _ in range(4))
    minor26 = minor5 = 0
    rows: list = []
    for grp, flow in real:
        n = int(grp["size"])
        for label, lo, hi in buckets:
            if lo <= n <= hi:
                by_size[label][0] += 1
                by_size[label][1] += n
                break
        c26 = Counter(x["category"] for x in grp["items"])
        c5 = Counter(_super(x["category"]) for x in grp["items"])
        maj26 = grp["category"]
        maj5 = alloc_groups._major(_super(x["category"]) for x in grp["items"])
        m26, m5 = n - c26.get(maj26, 0), n - c5.get(maj5, 0)
        minor26 += m26
        minor5 += m5
        # 跨 4 类以上归一个桶:再往上分对处置没有区别,徒增表格行数
        sp26_g[min(len(c26), 4)] += 1
        sp26_i[min(len(c26), 4)] += n
        sp5_g[min(len(c5), 4)] += 1
        sp5_i[min(len(c5), 4)] += n
        if m26 or m5:
            rows.append([flow, grp.get("store") or "", grp["brand"], n,
                         round(grp["score"], 1), maj26, maj5,
                         len(c26), len(c5), m26, m5, grp["channel"],
                         " | ".join(f"{k}×{v}" for k, v in sorted(
                             c26.items(), key=lambda kv: (-kv[1], kv[0])))])
    rows.sort(key=lambda r: (-r[10], -r[9], -r[3], r[2]))

    tot = sum(int(grp["size"]) for grp, _ in real)
    L = ["", "▍品牌组画像(组是原子的 —— 一张牌打不出去,卡住的是**一整块**货)"]
    L.append(f"  真品牌组 {len(real):,} 组 / {tot:,} 件 · "
             f"无品牌单品组 {len(solo):,} 组 / {len(solo):,} 件"
             f"(每 ASIN 自成一组,不参与品牌排他)")
    L += textfmt.table(
        ["组规模", "组数", "件数", "占件数"],
        [[k, f"{v[0]:,}", f"{v[1]:,}", _pct(v[1], tot)]
         for k, v in by_size.items()], align="<>>>")
    L += textfmt.table(
        ["一个品牌横跨", "组数(26类)", "件数(26类)", "组数(五品类)",
         "件数(五品类)"],
        [[("4 类以上" if k == 4 else f"{k} 类"), f"{sp26_g[k]:,}",
          f"{sp26_i[k]:,}", f"{sp5_g[k]:,}", f"{sp5_i[k]:,}"]
         for k in sorted(set(sp26_g) | set(sp5_g))], align="<>>>>")
    L.append(f"  **少数派件数**:26 类口径 {minor26:,} 件({_pct(minor26, tot)})"
             f" · 五品类口径 {minor5:,} 件({_pct(minor5, tot)})")
    L.append("  少数派 = 跟着品牌去了一家做不了它那个大类的店:上不了架、"
             "还占着位置挡别人(§11.3 #5 的标的)")
    L.append("  ⚠ 分母是**过了漏斗「去掉没有店要的」那一闸之后**的池子 —— 一件"
             "都没有店能收的货不算少数派(它的处置是开类目/放宽货期,在漏斗那行"
             "已单独报了)。所以这个数会**小于** §11.6 全池量出来的 105,571,"
             "两个数说的不是同一件事")
    return L, rows


_BRAND_HEADER = ["流别", "去向店", "品牌", "组件数", "组分",
                 "组大类(26类)", "组品类(五大类)",
                 "跨大类数(26类)", "跨品类数(五大类)",
                 "少数派件(26类)", "少数派件(五大类)",
                 "渠道", "组内大类分布(26类)"]


def _write_brands(rows) -> tuple[str, int]:
    """输入:品牌组画像行 → 输出:(路径, 行数)。**只写有少数派的组** ——
    没有少数派的组在这张表上没有任何要处置的东西,写进来只会淹掉要看的那些。"""
    return report_csv.write("alloc_品牌组.csv", _BRAND_HEADER, rows), len(rows)


# ⚠ 四列类目并排是有意的(所有者 2026-08-22:"我看不清晰")。组大类是**多数派**,
# 而一张牌整组去一家店 —— 商品自己的大类跟组大类不一样的那些件就是「少数派」,
# 它们上不了架却占着位置(§11.3 #5)。只给组大类的话,那批件在表上根本看不出来。
_HEADER = ["流别", "去向店", "层", "品牌组", "组分", "组件数",
           "组大类(26类)", "组品类(五大类)", "渠道",
           "ASIN", "商品大类(26类)", "商品品类(五大类)",
           "产品分", "口碑分", "销量加分", "罚分", "罚分原因",
           "售价", "运费", "落地价", "窗口销量(件)", "窗口销售额(毛额)",
           "评分", "评论数", "配送天数"]


def _write_plan(assign) -> tuple[str, int]:
    """输入:发牌结果 → 输出:(路径, 产品行数)。**只放要上架的。**"""
    rows: list = []
    for a in sorted(assign, key=lambda x: (x["layer"], -x["group"]["score"])):
        flow = "定向流" if a["group"].get("store") else "自由流"
        rows += _rows(flow, a["store"], a["layer"], a["group"])
    return report_csv.write("alloc_分配方案.csv", _HEADER, rows), len(rows)


def _write_rejects(unplaced, dir_out, queued=()) -> tuple[str, int]:
    """输入:三类没进这一轮的 → 输出:(路径, 产品行数)。

    与方案表分开,是因为它们的处置**完全不同**:排队的下一批加大 batch 就能发,
    淘汰的要所有者改配置或释放品牌。混在一张表里,3,000 行要动手的会被 45,815 行
    诊断淹掉(2026-08-16 实测:所有者第一眼看到的就是 48,816 这个总行数)。

    `queued` 是**排在切口之外的产品**(按产品分降序,只写前 `QUEUE_SAMPLE` 件)。
    2026-08-22 起它是产品不是组 —— 切口挪到了产品层,这批压根没进过任何一片,
    也就没有"组"可言。不写的话,"我那个高分品怎么没分出去"在两张表里都查不到
    —— 那才是真的藏。全写又会把这张表撑到十万行,所以取头部并说明截断。
    """
    rows: list = []
    for grp, why in dir_out:
        rows += _rows(f"定向流淘汰({why})", grp["store"], "", grp)
    for u in unplaced:
        rows += _rows(f"未发出({ae.REASON_LABEL[u['reason']]})", "", "",
                      u["group"])
    # ★ 排队的现在是**产品**不是组(2026-08-22 重排:切口在产品层)。
    #   包成一件一组再走同一个写行函数 —— 表头只有一套,别为它另开一张表
    for c in queued:
        rows += _rows("排队(产品分排在本轮切口之外)", "", "",
                      {"key": "(未组队)", "score": c["score"], "size": 1,
                       "category": c.get("category"),
                       "channel": c.get("channel"), "items": [c]})
    return report_csv.write("alloc_未入选.csv", _HEADER, rows), len(rows)


def _rows(flow, store, layer, grp) -> list:
    """输入:流别 + 去向店 + 层号 + 组 → 输出:该组逐产品一行的 csv 行。"""
    return [[flow, store, layer, grp["key"], round(grp["score"], 1),
             grp["size"], grp["category"],
             resources.super_label(grp["category"]), grp["channel"],
             it["asin"], it.get("category") or "",
             resources.super_label(it.get("category")),
             round(it["score"], 1), round(it["base"], 1),
             round(it["bonus"], 1), round(it["penalty"], 1), it["why"],
             it.get("price"), it.get("shipping"),
             None if it.get("price") is None or it.get("shipping") is None
             else round(it["price"] + it["shipping"], 2),
             it["sales"], round(it.get("gross") or 0, 2),
             it["rating"], it["reviews"], it["lead"]]
            for it in grp["items"]]
