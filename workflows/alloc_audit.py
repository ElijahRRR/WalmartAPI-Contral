"""alloc_audit — 分配动工前的存量审计 + 数据探针(只读,随时可跑)。

用法:
  python cli.py alloc_audit                    # 全部检查 + 落五份明细 csv
  python cli.py alloc_audit -p channel=0       # 跳过渠道探测(最慢的一段)
  python cli.py alloc_audit -p export=0        # 只看摘要不落 csv
  python cli.py alloc_audit -p sales_days=180  # 冲突处置的销量窗口(默认 365 天)
  python cli.py alloc_audit -p as_of=2026-08-15  # 钉住销量窗口右端(默认今天 UTC)

这是 docs/allocation_plan.md §十三 的 **A0.5 批次**:占用台账(A1)与分配
引擎(A2)动工前,必须先知道存量长什么样、设计稿里的假设数字实际是多少。
**只读**——不写任何表、不调沃尔玛、不调 LLM。

**输出分工:控制台只放"一眼能扫完的结论 + 下一步动作",逐条明细全进 csv。**
所有者 2026-08-15 反馈"太难看了,看不明白"——上一版把几十个计数、样例和
解释挤在一屏,结果谁也读不完。现在控制台每节至多四五行,明细去 csv 里翻。

控制台六节:
  ▍要你做的事 —— 放最前面。报告是为了让人动手,不是为了让人读。每条 =
     一件事 + 数量 + 照着做的那份 csv;查不了的那条必须显式写"跳过"及原因
     (拿 0 冒充"全合规"是这份报告最容易骗人的地方);
  ▍数据体检 —— 候选池逐层收窄(approved → 有标题 → PT 有效 → **大类查得到**,
     最后一层才是引擎真能分的量)、打分信号在不在(评分/评论为空就把这两项
     从 v1 权重删掉,**绝不 or 0**)、PT 两本字典对拍、品牌占用键覆盖;
  ▍在线商品 —— 在线口径 = walmart_items.missing_since IS NULL;规划内多少、
     排除了谁、数据缺口多少;
  ▍冲突 —— 同 ASIN / 同品牌跨店各多少组,**按判定依据分布**(见下);
  ▍店铺 —— 超 2 大类、非 ACTIVE、凭证被过滤、渠道有不符,各多少家;
  ▍要留意 —— 会让上面的数字读歪的那些事(渠道值认不出、订单店名对不上……)。

明细 csv(落 `<DATA_ROOT>/reports/*.csv`,每次覆盖;所有者照着做)
  alloc_类目建议.csv —— 每店"在线数量最多的两类",据此填飞书「类目1/2/3」;
     同时对拍已填值与实际 top2 是否一致;
  alloc_渠道不符下架清单.csv —— 所有者自行下架(只列确实是另一个已知渠道的);
  alloc_店铺总览.csv —— 逐店一行:在线/发布/大类/渠道/准入类目/状态/缺配置列/
     近窗销售额。控制台里"N 家"的那些计数,点名全在这;
  alloc_同ASIN冲突处置.csv / alloc_同品牌冲突处置.csv —— 给出保留/下架。
     ⚠ 实测 96% 的同 ASIN 组、86% 的同品牌组**两边该商品都零销量**,只看
     商品销量等于把两千多组丢给人工,所以按**降级阶梯**判(见
     services/alloc_survey.LADDER):该商品销量 → 该店该大类销量 → 该店整体
     销量 → 在线件数 → 店名。**判定依据写进每一行**,人一眼看出这条靠什么
     定的、要不要推翻;**只有落到最后一级(店名)才是机器真判不出、需要
     人眼的**——在线件数是库里查得到的客观数据,算机器判得出(所有者
     定稿 2026-08-15 晚)。

三条口径纪律(2026-08-15 对抗式审查后定,每条都对应一次会算错数的实例):

1. **冻结行不进冲突**:A1/A2/A3/A5 只吃"仍在营店铺"的行。已停用(或已从
   凭证表删除)的店,其 walmart_items 行永久冻结为"在架"(catalog_sync 只扫
   在营店),混进来会让所有者为一家不做了的店去下架另一家店真在卖的 listing。
   排除了多少必须打印——静默兜底等于主路径坏了没人知道。
2. **"不在营" ≠ "被过滤"**:判在营用 `stores.enabled_names()`(在册 ∧ 勾了
   「启用」,所有者定稿 2026-08-22),**不用** `load_stores()`——后者还筛
   ClientId/代理,会把"代理没配的在营店"误判成死店,而死店清单直通整店释放。
   ⚠ 也**不用** `registered_names()`:它连「启用」都不看,勾了停用的店会被
   当成还在营,于是它的冻结行继续拦着别的店上架(2026-08-22 前正是如此)。
3. **未知不算不符**:渠道判不符只在两侧都是 FBA/FBM 且不同时;采集没采到、
   或采出第三种值,都单列计数——把"没采到"算成"货不对"会让无辜商品进下架清单。

sku→asin 走 services/sku_asin 唯一规则,提不出的**单列计数**不猜。
"""

import csv
import logging
from collections import Counter

from registry import db, paths, resources
from services import alloc_survey as sv
from services import claims, sku_asin, store_targets, stores as stores_svc
from services import textfmt

DANGEROUS = False

logger = logging.getLogger("workflows.alloc_audit")


def _write_csv(name: str, header: list, rows: list) -> str:
    """输入:文件名 + 表头 + 行 → 输出:落盘路径(报告目录,每次覆盖)。"""
    paths.reports_dir().mkdir(parents=True, exist_ok=True)
    p = paths.reports_dir() / name
    with p.open("w", newline="", encoding="utf-8-sig") as fh:   # BOM:Excel 直开不乱码
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return str(p)


def run(params: dict) -> str:
    """输入:params(channel/sales_days/export)→ 输出:存量审计报告 + 明细 csv。"""
    with_channel = str(params.get("channel", "1")).lower() not in {"0", "false", "no"}
    sales_days = int(params.get("sales_days", 365))
    win = sv.sales_window(str(params.get("as_of", "")), sales_days)
    export = str(params.get("export", "1")).lower() not in {"0", "false", "no"}
    L: list[str] = []

    with db.pg_conn() as conn, conn.cursor() as cur:
        pool = sv._row(cur, sv._SQL_POOL)
        sig = sv._row(cur, sv._SQL_SIGNAL)
        # P3 是对拍探针不是主线:字典表出问题不该拖垮整份存量审计
        try:
            dic = sv._row(cur, sv._SQL_PT_DICT)
        except Exception as e:                  # noqa: BLE001 降级并明说
            conn.rollback()                     # 事务已 aborted,后续查询要先回滚
            dic, dic_err = None, str(e).strip().splitlines()[0]
        else:
            dic_err = None
        cur.execute(sv._SQL_CATEGORIES)
        cats = cur.fetchall()
        cur.execute(sv._SQL_PT2CAT)
        pt2cat = {pt: c for pt, c in cur.fetchall() if c}
        cur.execute(sv._SQL_ONLINE)
        items = cur.fetchall()
        cur.execute(sv._SQL_STATUS)
        status = {s: (st or "").strip().upper() for s, st in cur.fetchall()}
        cur.execute(sv._SQL_SALES, win)
        sales = {(s, k): (int(o), float(g)) for s, k, o, g in cur.fetchall()}
        cur.execute(sv._SQL_ORDER_STORES, win)
        order_stores = cur.fetchall()

        asins = sorted({a for a in (sku_asin.extract_asin(it[1])
                                    for it in items) if a})
        meta = sv._fetch_meta(cur, asins, with_channel)

    rows, st = sv.enrich(items, meta, pt2cat)
    prof_all = sv.store_profiles(rows)          # 全量(含不在册店):A4/A7 点名用

    # 在营店名(在册 ∧ 勾了「启用」;不做 ClientId/代理过滤——见纪律 2)
    registered, reg_err = None, None
    try:
        registered = stores_svc.enabled_names()
    except Exception as e:                    # noqa: BLE001 报告降级,不阻断
        reg_err = str(e)
    live_api, live_err = None, None
    try:
        live_api = {s["name"] for s in stores_svc.load_stores()}
    except Exception as e:                    # noqa: BLE001
        live_err = str(e)
    cfg, cfg_err = {}, None
    try:
        cfg = store_targets.load_targets()
    except Exception as e:                    # noqa: BLE001
        cfg_err = str(e)
    # ★ 已有占用(2026-08-22)。冲突判定必须先看它 —— 占用是**决策**不是观测,
    #   报告拿销量把一个已经有主的品牌判给别人,人照做就会"货下了、品牌还锁着"。
    #   ⚠ 读不到时**不静默退回销量阶梯**:那正是会给出相反建议的那条路,
    #     报告要显式说"本轮没看占用"。
    held, held_err = None, None
    try:
        with db.pg_conn() as _c:
            held = {"同品牌": claims.load_active(_c, claims.BRAND),
                    "同 ASIN": claims.load_active(_c, claims.PRODUCT)}
    except Exception as e:                    # noqa: BLE001
        held_err = str(e)

    # 冻结行(不在营店的在线行)不进冲突分析——纪律 1
    frozen = ({s for s in prof_all if s not in registered}
              if registered is not None else set())
    # 规划范围外的店(registry.alloc_excluded_stores,当前=店名含「谭总」):
    # 不判类目、不占品牌与产品、其在线商品不与别人算冲突——所有者定稿
    # 2026-08-15。**它们的订单销量不受影响**(排除的是归属不是数据)
    out_of_scope = {s for s in prof_all if sv.is_excluded(s)}
    # ⚠ **店铺状态不参与筛行**:SUSPENDED 的店占用保持、在线行照常计入冲突
    # (§六.2「暂停不释放」)。停用只是暂时不给它分新货,不代表别店可以来拿
    # 它的品牌与产品 —— 要某店不接新货是「单店最大在线数」填 0,与状态无关
    skipped = frozen | out_of_scope
    live_rows = [r for r in rows if r["store"] not in skipped]
    oos_rows = sum(prof_all[s]["n"] for s in out_of_scope)
    prof = sv.store_profiles(live_rows)


    # ══ 报告拼装:控制台只放"一眼能扫完的结论 + 要做的事",明细全进 csv ══
    n = lambda v: f"{int(v):,}"                       # noqa: E731 千分位
    L: list[str] = ["", "═══ 分配存量审计 ═══"]

    # 先算出各节要用的数。
    # ⚠ 冲突判定吃的是 **claim_rows(在册∧已发布∧规划内)**,与 alloc_backfill
    # 逐字同一口径(sv.claimable)——用 live_rows 算会把未发布行算进「在线件数」
    # 那一级,真打平组的冠亚军就换了位,报告与回填给出两个不同的保留店。
    # live_rows(含未发布)只用来看"这家店的目录长什么样":类目分布、渠道。
    reg = registered if registered is not None else set(prof_all)
    # 两个集合,别混:
    #   slot_rows —— **占着货位的行**(在册∧已发布∧规划内)。处置清单出自这里:
    #                类目/渠道不符的货此刻确实在架上卖着,必须列出来让人下架;
    #   claim_rows —— **该产生占用的行** = slot_rows 再去掉类目/渠道不符的。
    #                它们已经在下架清单上了,不该再被占走(占用无自动释放,
    #                占了就永远锁在这家不该做这个大类、或不做这个渠道的店上)。
    # ⚠ `-p channel=0` 时 rows 里的 channel 全是 None,渠道闸对 claim_rows
    #    恒为假 —— 于是这一轮的 claim_rows 比 alloc_backfill 的**宽**,冲突
    #    判定可能给出不同的保留店。下面「要你做的事」那节会显式警告不得据此回填
    slot_rows = sv.claimable(rows, reg)
    claim_rows = sv.claimable(rows, reg, cfg)
    metrics = sv.store_metrics(claim_rows, sales)
    conflicts = {}
    for tag, field in (("同 ASIN", "asin"), ("同品牌", "brand_key")):
        conflicts[tag] = sv.resolve_conflicts(
            claim_rows, sales, field, metrics, cfg,
            held=held.get(tag) if held else None)
    # ⚠ 「碎不碎」按**品类层**数(2026-08-22)。按 26 类数的实测后果:
    # 47 家有货店里 41 家"超 2 大类",而它们绝大多数只是 Home + Hardlines
    # 两个品类 —— 报出来的是一份所有者无从下手的名单,因为闸根本不按 26 类判
    over = sorted(((s, p) for s, p in prof.items() if len(sv.real_supers(p)) > 2),
                  key=lambda x: (-len(sv.real_supers(x[1])), -x[1]["n"], x[0]))
    # 渠道:没取渠道就没有结论。算出来必是 0,而"不符 0 件"读起来正好像
    # 全店合规 —— 这一节必须显式说"本轮没查",不能拿 0 冒充结论
    can_channel = bool(cfg) and with_channel
    mism = sv.channel_mismatch(sv.store_profiles(slot_rows), cfg) if can_channel else []
    offenders = sv.channel_offenders(slot_rows, cfg) if can_channel else []
    # 类目不符:所有者填完类目三列后的第一件事。与销量无关——类目是硬闸
    cat_bad, cat_unknown = (sv.category_offenders(slot_rows, cfg) if cfg
                            else ([], 0))
    scope = (sorted(set(registered) | set(prof)) if registered is not None
             else sorted(prof))
    miss_cfg = store_targets.missing_config(cfg, scope) if cfg else {}
    empty_stores = [s for s in scope if s not in prof]
    unfilled_cat = [s for s in prof if not (cfg.get(s) or {}).get("categories")]
    # 参与分配与否 = 限额表「单店最大在线数」是不是 0(所有者的开关),
    # 与店铺状态无关。三态分开数:填 0 / 填了正数 / 还没填
    opted_out = sorted(s for s in scope
                       if store_targets.accepts_allocation(cfg.get(s)) is False)
    non_active = sorted(s for s in prof_all
                        if (status.get(s) or "ACTIVE") != "ACTIVE")
    filtered = (sorted((set(registered) & set(prof_all)) - live_api)
                if registered is not None and live_api is not None else [])
    order_miss = ([(s, c, h) for s, c, h in order_stores if s not in registered]
                  if registered is not None else [])

    # ── ▍要你做的事(放最前面:报告是为了让人动手,不是为了让人读)──
    hard = sum(1 for c in conflicts.values() for x in c
               if x[4] in sv.NEEDS_HUMAN)
    # 冲突清单里**只有「下架」那些行要你动手**;「保留」行是上下文,列出来
    # 是为了让人看见判给了谁。两者数量本来就不等——一个品牌组里赢家保留它
    # 全部的行、输家下架它全部的行,各店 SKU 数不一样(所有者 2026-08-15 晚
    # 按 1:1 对不上而质疑)。所以摘要报的是"要下架几件",不是"清单几行"
    drop_all = sum(1 for c in conflicts.values() for x in c
                   for d in x[3] if d.verdict == "下架")
    todo = [("① 填类目三列",
             f"{len(unfilled_cat)} 家店待填" if not cfg_err else "本轮没查",
             "→ alloc_类目建议.csv" if not cfg_err else
             f"**跳过**:限额表读不到({cfg_err}),已填值无从对拍"),
            ("② 下架渠道不符",
             f"{len(offenders)} 件" if can_channel else "本轮没查",
             "→ alloc_渠道不符下架清单.csv" if can_channel else
             ("**跳过**:-p channel=0 没取渠道,去掉该参数重跑才有结论"
              if with_channel is False else "**跳过**:限额表读不到,没有配送限制可对拍")),
            ("③ 下架类目不符",
             f"{len(cat_bad)} 件" if cfg else "本轮没查",
             "→ alloc_类目不符下架清单.csv" if cfg else
             "**跳过**:限额表读不到,没有准入类目可对拍"),
            ("④ 确认冲突处置",
             f"要下架 {drop_all} 件"
             f"(同 ASIN {len(conflicts['同 ASIN'])} 组 · "
             f"同品牌 {len(conflicts['同品牌'])} 组)",
             "→ alloc_同ASIN冲突处置.csv 等 2 份"
             + (f",其中 {hard} 组机器判不出、要人眼看" if hard
                else ",全部可自动判"))]
    if miss_cfg:
        have = [s for s in miss_cfg if s in prof]
        todo.append(("⑤ 补店铺配置", f"有货店缺列 {len(have)} 家",
                     "→ alloc_店铺总览.csv「缺配置列」"
                     + (f";另有 {len(empty_stores)} 家空店没登记,"
                        f"要它们参与分配才用填" if empty_stores else "")))
    L += ["", "▍要你做的事"] + textfmt.grid(todo)
    # 渠道闸是 claimable 的第五条筛法,没取渠道时它对本轮恒为假 ⇒ 这一轮算出的
    # 保留店比 alloc_backfill 会落的**宽**。不点破的话,人照着这份清单去真跑,
    # 落库结果与清单不一致,而占用没有自动释放
    if cfg and not with_channel:
        L.append("  ⚠ 本轮 `-p channel=0` 没取渠道,冲突判定与占用口径里的"
                 "**渠道闸是关着的** —— 与 `alloc_backfill` 不一致。"
                 "**不要照这一份去回填**,去掉该参数重跑一次再落库")

    # ── ▍数据体检 ──
    body = [("候选池", f"产品 {n(pool['total'])} → 审核通过 "
                       f"{n(pool['approved'])} → **可分配 "
                       f"{n(pool['with_cat'])}**;其中 PT 实证 "
                       f"{n(pool['pt_evid'])} / 推断 "
                       f"{n(pool['with_pt'] - pool['pt_evid'])}"),
            ("打分信号", f"评分 {'✓' if sig['n_rating'] else '✗'} · "
                         f"评论 {'✓' if sig['n_review'] else '✗'} · "
                         f"渠道 {'✓' if sig['n_fba'] else '✗'}(近 5 万快照)"
                         + ("——**评分/评论为空,v1 权重要删掉这两项**"
                            if not sig["n_rating"] else ""))]
    if dic_err:
        body.append(("PT 字典", f"对拍跳过({dic_err})"))
    else:
        same = (dic["only_risk"] == dic["only_meta"] == dic["cat_diff"] == 0)
        body.append(("PT 字典",
                     f"两本各 {n(dic['n_risk'])}/{n(dic['n_meta'])} 条,"
                     + ("完全一致" if same else
                        f"**有差异:仅前者 {dic['only_risk']} / 仅后者 "
                        f"{dic['only_meta']} / 大类不一致 {dic['cat_diff']}**")
                     + f";大类 {len(cats)} 个"))
    n_key = sum(1 for r in rows if r["brand_key"])
    body.append(("品牌键", f"可占用 {n(n_key)} 行 · 真·无品牌 {n(st['no_brand'])}"
                           f" · 产品库缺行 {n(st['asin_not_in_products'])}"))
    L += ["", "▍数据体检"] + textfmt.grid(body)

    # ── ▍在线商品 ──
    n_pub = sum(1 for r in rows if r["published"])
    body = [("规划内", f"{len(prof)} 家店 {n(len(live_rows))} 行")]
    if out_of_scope:
        body.append(("已排除", f"规划外 {len(out_of_scope)} 家 {n(oos_rows)} 行"
                               f"({'、'.join(sorted(out_of_scope)[:6])})"
                               f" —— 不占用、不算冲突,**销量仍计入**"))
    if frozen:
        body.append(("已排除", f"不在营店冻结 {len(frozen)} 家"
                               f"({'、'.join(sorted(frozen)[:6])})"
                               f" —— 整店释放见 store_release"))
    if registered is None:
        body.append(("⚠ 未排除", f"凭证表读不到({reg_err}),幻影店一个没排,"
                                 f"下面的冲突数只可参考"))
    unpub = sum(1 for r in live_rows if not r["published"])
    body.append(("不占货位", f"未发布 {n(unpub)} 行 —— 在店里但没上架,"
                             f"不算占着货位,冲突与回填都不算它"))
    body.append(("数据缺口", f"归不到大类 {n(st['no_category'])} 行 · "
                             f"SKU 提不出 ASIN {n(st['no_asin'])} 行"))
    L += ["", f"▍在线商品 {n(st['online'])} 行(已发布 {n(n_pub)} / "
              f"未发布 {n(st['online'] - n_pub)};RETIRED 退市行已在 SQL 排除)"]
    L += textfmt.grid(body)

    # ── ▍冲突 ──
    body = []
    for tag in ("同 ASIN", "同品牌"):
        res = conflicts[tag]
        if not res:
            body.append((tag, "无", ""))
            continue
        by_level = Counter(x[4] for x in res)
        drop = sum(1 for x in res for d in x[3] if d.verdict == "下架")
        rows_n = sum(len(x[3]) for x in res)
        body.append((tag, f"{len(res)} 组 / {rows_n} 行",
                     f"**要下架 {drop} 行**(其余 {rows_n - drop} 行是保留方,"
                     f"列出来只为让你看判给了谁)",
                     "依据:" + " · ".join(
                         f"{k.removeprefix('按')} {v}"
                         for k, v in by_level.most_common(3))))
    L += ["", "▍冲突(只算规划内店铺)"] + textfmt.grid(body)
    if held_err:
        L.append(f"  ⚠ **占用台账读不到({held_err}):本轮没看占用**,上面全是"
                 f"按销量阶梯判的。已经有主的品牌可能被判给别人 —— "
                 f"**先修好再照着做**")
    elif held is not None:
        n_held = sum(len(v) for v in held.values())
        stuck = [x for tag in ("同 ASIN", "同品牌")
                 for x in conflicts[tag] if x[4] == sv.CLAIM_STUCK]
        by_claim = sum(1 for tag in ("同 ASIN", "同品牌")
                       for x in conflicts[tag] if x[4] == sv.BY_CLAIM)
        if not n_held:
            L.append("  占用台账是**空的**(还没跑过 alloc_backfill),"
                     "所以这一轮全按销量阶梯判 —— 回填之后重跑,结论可能变")
        else:
            L.append(f"  占用台账 {n_held:,} 条:其中 {by_claim} 组**按占用定**"
                     f"(不看销量,占用是决策不是观测)")
        if stuck:
            L.append(f"  ⚠ **{len(stuck)} 组占用站不住**:占用店手上的货踩了"
                     f"类目/渠道闸。报告**不替你改判给第二名** —— 那等于绕过"
                     f"store_release 撤一条不可逆的决策。先释放再重跑:"
                     + "、".join(f"{x[1]}:{x[0]}" for x in stuck[:4]))
    if not sales:
        # 销量全空时阶梯必然一路降到"在线件数/店名",那是打平不是判定。
        # 不说破的话,csv 里每行都写着判定依据,看起来像是真判过了
        L.append(f"  ⚠ **订单历史还没导入**(近 {sales_days} 天 0 个 (店,SKU) "
                 f"组合),上面全是按在线件数打平的,不是按销量判的;"
                 f"先跑 order_history_import -p apply=1 再重跑本报告")
    elif not hard and (conflicts["同 ASIN"] or conflicts["同品牌"]):
        L.append("  ✓ 全部有销量/类目依据,不需要人工逐条看")

    # ── ▍店铺 ──
    body = []
    if over:
        body.append(("超 2 品类", f"{len(over)} 家", "最碎:" + "、".join(
            f"{s}({len(sv.real_supers(p))} 品类 / {len(sv.real_cats(p))} 个 26 类)"
            for s, p in over[:3])))
    if non_active:
        body.append(("非 ACTIVE", f"{len(non_active)} 家",
                     "SUSPENDED,**占用按设计保持不动**;"
                     "要它暂时不接新货,把「单店最大在线数」填 0"))
    if cfg:
        body.append(("不参与分配", f"{len(opted_out)} 家",
                     ("「单店最大在线数」填 0 —— 所有者的开关,核对是不是这几家:"
                      + "、".join(opted_out[:6])) if opted_out else
                     "没有店填 0;**要哪家不接货就把这一列填 0**"))
    if filtered:
        body.append(("凭证被过滤", f"{len(filtered)} 家",
                     "缺代理/ClientId,**不是死店**,别整店释放"))
    if mism:
        body.append(("渠道有不符", f"{len(mism)} 家", f"共 {len(offenders)} 件待下架"))
    if cat_bad:
        from collections import Counter as _C
        by = _C(r["store"] for r in cat_bad)
        body.append(("类目有不符", f"{len(by)} 家", f"共 {len(cat_bad)} 件待下架;"
                     + "、".join(f"{s}×{c}" for s, c in by.most_common(3))))
    L += ["", f"▍店铺(规划内有货 {len(prof)} 家)"]
    L += textfmt.grid(body) if body else ["  ✓ 没发现异常"]

    # ── ▍要留意 ──
    notes = []
    if cat_unknown:
        notes.append(f"归不到大类 {n(cat_unknown)} 行(在准入受限的店里)—— "
                     f"**没进类目不符清单**:那是「不知道它属于哪类」,"
                     f"不是「属于不该有的类」,别拿它当违规下架")
    if st["channel_weird"]:
        notes.append(f"渠道值认不出 {n(st['channel_weird'])} 行 —— "
                     f"采集侧 is_fba 没给值,不是货不对,**别下架**")
    if order_miss:
        notes.append(f"订单店名对不上 {len(order_miss)} 家 "
                     f"{sum(c for _, c, _ in order_miss)} 行("
                     + "、".join(f"{s or '(空店名)'} {c}"
                                 for s, c, _ in order_miss[:4])
                     + ")—— 这些销量进不了店×类目维度")
    if live_err:
        # 读不到就没有「配置缺失 vs 真不在册」这一栏,而不是"一家都没有"
        notes.append(f"店铺配置读不到({live_err}),本轮没法区分"
                     f"「缺代理/ClientId 的在营店」和「已停用的死店」")
    if notes:
        L += ["", "▍要留意"] + [f"  · {x}" for x in notes]

    # ── 落 csv ──
    if not export:
        L += ["", "(-p export=0:未落明细 csv)"]
        return "\n".join(L)

    files = []
    if cfg is not None and not cfg_err:
        sug = sv.suggest_categories(prof, cfg)
        # ⚠ 建议出在**品类层**(所有者 2026-08-22)——闸判的就是这一层。
        # 出 26 类的建议 = 让所有者照着填一份闸门不按它判的东西:填
        # 「Home」+「Furniture」以为开了两类,闸看见的是同一个 Home。
        # 26 类留在最后一列当**佐证**:只说"建议 Hardlines"而不说它是
        # Home Improvement 218 + Garden & Patio 96 堆出来的,他没法判合不合理
        files.append(_write_csv(
            "alloc_类目建议.csv",
            ["店铺", "在线品类数(五大类)", "建议类目分类(填这一列·五大类)",
             "表格已填", "表格已填(折五大类)", "对拍", "认不出的填写值",
             "在线品类分布(五大类)", "在线大类分布(26类,佐证)"],
            [(r["store"], len(r["by_super"]), "|".join(r["suggest"]),
              "|".join(r["filled"]), "|".join(r["filled_super"]),
              # 对拍在**品类层**比:拿 26 类原文去比品类建议,永远"不一致"
              ("未填" if not r["filled"] else
               "一致" if set(r["filled_super"]) == set(r["suggest"])
               else "不一致"),
              "|".join(r["unknown"]),
              "; ".join(f"{c}×{x}" for c, x in r["by_super"]),
              "; ".join(f"{c}×{x}" for c, x in r["by_category"][:8]))
             for r in sug]))
    if can_channel:
        # ⚠ 只在真查过渠道时才写这份。`-p channel=0` 时 offenders 必空,照写
        # 会把上一轮跑出来的下架清单覆盖成空表 —— 所有者照着空表下架 = 什么
        # 都不下,而且看不出是被本轮清掉的
        files.append(_write_csv(
            "alloc_渠道不符下架清单.csv",
            ["店铺", "限定渠道", "SKU", "ASIN", "实际渠道",
             "大类(26类)", "品类(五大类)", "PT"],
            [(r["store"], cfg[r["store"]]["channel"], r["sku"], r["asin"] or "",
              r["channel"], r["category"] or "",
              resources.super_label(r["category"]), r["pt"] or "")
             for r in offenders]))
        # ⚠ 两侧都同时给**原文与折后**四列。只给原文的实测后果:所有者看到
        # 「准入 Furniture / 商品 Home Improvement」问不出为什么不符 —— 他得
        # 自己在脑子里折一遍才对得上闸的判断。判据在品类层,清单就得把品类摆出来
        files.append(_write_csv(
            "alloc_类目不符下架清单.csv",
            ["店铺", "准入类目(表格原文)", "准入品类(五大类)",
             "大类(26类)", "品类(五大类)", "SKU", "ASIN", "PT", "渠道"],
            [(r["store"], "|".join(cfg[r["store"]]["categories"]),
              "|".join(sorted(store_targets.super_categories_of(
                  cfg[r["store"]]))),
              r["category"],
              resources.super_label(r["category"]),
              r["sku"], r["asin"] or "", r["pt"] or "", r["channel"])
             for r in cat_bad]))
    # 店铺总览:逐店明细一次给全,控制台只留计数
    _ACCEPT = {True: "是", False: "否(填了0)", None: "(未填)"}
    files.append(_write_csv(
        "alloc_店铺总览.csv",
        ["店铺", "占用内", "参与分配", "单店最大在线数", "在线数", "已发布",
         "品类数(五大类)", "在售品类(五大类)", "大类数(26类)", "前3大类(26类)",
         "渠道限制", "渠道不符件数", "准入类目(表格原文)", "准入品类(五大类)",
         "店铺状态", "缺配置列", "近窗销售额"],
        [(s,
          # 「占用内」= 它的在线商品占不占货位。与「参与分配」是两回事:
          # SUSPENDED 店占用照旧保持(占用内=是),但可以把它的最大在线数
          # 填 0 让它暂时不接新货(参与分配=否)
          "否(规划外)" if s in out_of_scope else
          ("否(不在营)" if s in frozen else "是"),
          _ACCEPT[store_targets.accepts_allocation(cfg.get(s))] if cfg else "",
          "" if (cfg.get(s) or {}).get("max_online") is None
             else int(cfg[s]["max_online"]),
          p["n"], p["published"],
          len(sv.real_supers(p)), "|".join(sv.real_supers(p)),
          len(sv.real_cats(p)),
          "; ".join(f"{c}×{x}" for c, x in p["categories"].most_common(3)
                    if c != sv.UNCLASSIFIED),
          (cfg.get(s) or {}).get("channel") or "",
          next((m[2] for m in mism if m[0] == s), 0),
          "|".join((cfg.get(s) or {}).get("categories") or []),
          # 准入品类:闸真正看见的东西。与左边一列并排,人才能一眼看出
          # 「填了 Home + Furniture」其实只开了一个 Home
          "|".join(sorted(store_targets.super_categories_of(cfg.get(s))))
          if cfg else "",
          status.get(s) or "(无KPI)",
          "/".join(miss_cfg.get(s, [])),
          round((metrics[0].get(s) or {}).get("gmv", 0.0), 2))
         for s, p in sorted(prof_all.items(),
                            key=lambda kv: -kv[1]["n"])]))
    for tag, fname in (("同 ASIN", "alloc_同ASIN冲突处置.csv"),
                       ("同品牌", "alloc_同品牌冲突处置.csv")):
        files.append(_write_csv(
            fname,
            # 「组内店铺数/组内行数」两列是给人对数用的:一个品牌组里赢家
            # 保留它**全部**的行、输家下架它**全部**的行,各店 SKU 数不一样,
            # 所以保留数与下架数**本来就不相等**(所有者 2026-08-15 晚按 1:1
            # 对不上而质疑)。把分组摊在每一行上,这件事就不用解释了
            # 「已有占用」单独一列而不是只靠「判定依据」:阶梯判的组也要看得出
            # 它**确实没有占用**,而不是"我们没查"(空列有歧义,所以读不到占用
            # 时整列写「(本轮没读到)」)
            ["冲突键", "组内店铺数", "组内行数", "已有占用", "保留店", "判定依据",
             "店铺", "SKU", "ASIN", "大类(26类)", "品类(五大类)",
             f"近{sales_days}天单量", "该商品销售额", "该店该大类销售额",
             "该店整体销售额", "处置"],
            [(key, len({d[0] for d in detail}), len(detail),
              ("(本轮没读到)" if held is None
               else (held[tag].get(key) or "(无)")),
              keep, level, s2, sku, asin, cat, resources.super_label(cat),
              o, g, cg, sg, verdict)
             for key, keep, _, detail, level in conflicts[tag]
             for s2, sku, asin, cat, o, g, cg, sg, verdict in detail]))

    # 销量窗口必须打出来:阶梯前三级全看销量,窗口一滑判定就可能翻。
    # 回填要照这份清单落,就得吃同一个窗口 —— 命令原样给出,不让人自己拼
    L += ["", f"▍销量窗口 {win['day']} 往前 {sales_days} 天(右端不含当天)",
          f"  回填照这份清单落:python cli.py alloc_backfill "
          f"-p as_of={win['day']} -p sales_days={sales_days}"]
    L += ["", f"▍明细 {len(files)} 份 csv → {paths.reports_dir()}"]
    for f in files:
        L.append(f"  · {f.rsplit('/', 1)[-1]}")
    if cfg_err:
        L.append(f"  ⚠ 限额表读不到({cfg_err}):类目建议与渠道清单本轮没出")
    return "\n".join(L)
