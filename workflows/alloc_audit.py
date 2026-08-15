"""alloc_audit — 分配动工前的存量审计 + 数据探针(只读,随时可跑)。

用法:
  python cli.py alloc_audit                    # 全部检查 + 落四份处置清单 csv
  python cli.py alloc_audit -p sample=30       # 摘要里的样例条数
  python cli.py alloc_audit -p channel=0       # 跳过渠道探测(最慢的一段)
  python cli.py alloc_audit -p export=0        # 只看摘要不落 csv
  python cli.py alloc_audit -p sales_days=180  # 冲突处置的销量窗口(默认 365 天)

这是 docs/allocation_plan.md §十三 的 **A0.5 批次**:占用台账(A1)与分配
引擎(A2)动工前,必须先知道存量长什么样、设计稿里的假设数字实际是多少。
**只读**——不写任何表、不调沃尔玛、不调 LLM。

两部分:

**P 探针**(把设计稿里的假设换成实测;出处 2026-08-15 实现校准 §十二.14)
  P1 候选池分母:approved → 有标题 → PT 有效 → **大类查得到**(逐层收窄;
     最后一层才是引擎真能分的量);
  P2 打分信号:评分/评论数在不在快照 raw 里(不在就把这两项从 v1 权重删掉,
     **绝不 or 0**);
  P3 PT 字典对拍:risk_product_types(日更)vs audit.walmart_pt_meta(一次性
     搬迁)——设计稿说"同源",但入库通道不同,漂移无人监控;顺带列出大类
     取值域(设计稿写 27 个,实际以本报告为准);
  P4 品牌覆盖:占用键取得出来的比例(brand → manufacturer 兜底后仍是占位符
     = 真·无品牌,逐 ASIN 分配)。

**A 存量审计**(在线口径 = walmart_items.missing_since IS NULL)
  A1 同 ASIN 跨店在线 —— 产品占用的存量冲突;
  A2 同品牌跨店在线 —— 品牌占用的存量冲突(占用键见 services/brand_key);
  A3 每店大类分布 + 超 2 大类的店 —— store_categories 回填清单;
  A4 已不在册的店仍有在线行 —— 冻结行(§十二.11 的存量面);
  A5 每店渠道分布 vs「配送限制」列对拍 —— 不一致的进过渡下架清单;
  A6 店铺配置完备度 —— 四列没填齐的店(引擎硬闸的前置);
  A7 店铺状态 —— 有在线行但非 ACTIVE 的店。

**C 处置清单**(落 `<DATA_ROOT>/reports/*.csv`,每次覆盖;所有者照着做)
  C1 类目建议 —— 每店"在线数量最多的两类",所有者据此填飞书「类目1/2/3」;
     同时对拍已填值与实际 top2 是否一致;
  C2 渠道不符逐行清单 —— 所有者自行下架(只列确实是另一个已知渠道的);
  C3 同 ASIN 跨店处置 / C4 同品牌跨店处置 —— 按"留销量大的店"给出保留/下架。
     ⚠ 实测 96% 的同 ASIN 组、86% 的同品牌组**两边该商品都零销量**,只看
     商品销量等于把两千多组丢给人工,所以按**降级阶梯**判(见
     services/alloc_survey.LADDER):该商品销量 → 该店该大类销量 → 该店整体
     销量 → 在线件数 → 店名。**判定依据写进每一行**,人一眼看出这条靠什么
     定的、要不要推翻;只有落到最后两级才是机器真判不出、需要人眼的。

三条口径纪律(2026-08-15 对抗式审查后定,每条都对应一次会算错数的实例):

1. **冻结行不进冲突**:A1/A2/A3/A5 只吃"仍在册店铺"的行。已从凭证表删除的
   店,其 walmart_items 行永久冻结为"在架"(catalog_sync 只扫在册店),
   混进来会让所有者为一家不存在的店去下架另一家店真在卖的 listing。
   排除了多少必须打印——静默兜底等于主路径坏了没人知道。
2. **"不在册" ≠ "被过滤"**:A4 用 `stores.registered_names()`(凭证表全集)
   判在册,不用 `load_stores()`(它按启用/代理筛过)——后者会把"代理没配的
   在营店"误判成死店,而死店清单直通整店释放。
3. **未知不算不符**:渠道判不符只在两侧都是 FBA/FBM 且不同时;采集没采到、
   或采出第三种值,都单列计数——把"没采到"算成"货不对"会让无辜商品进下架清单。

sku→asin 走 services/sku_asin 唯一规则,提不出的**单列计数**不猜。
"""

import csv
import logging
from collections import Counter

from registry import db, paths
from services import alloc_survey as sv
from services import sku_asin, store_targets, stores as stores_svc

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
    """输入:params(sample/channel)→ 输出:探针 + 存量审计报告。"""
    sample = int(params.get("sample", 10))
    with_channel = str(params.get("channel", "1")).lower() not in {"0", "false", "no"}
    sales_days = int(params.get("sales_days", 365))
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
        cur.execute(sv._SQL_SALES, (sales_days,))
        sales = {(s, k): (int(o), float(g)) for s, k, o, g in cur.fetchall()}
        cur.execute(sv._SQL_ORDER_STORES, (sales_days,))
        order_stores = cur.fetchall()

        asins = sorted({a for a in (sku_asin.extract_asin(it[1])
                                    for it in items) if a})
        meta = sv._fetch_meta(cur, asins, with_channel)

    rows, st = sv.enrich(items, meta, pt2cat)
    prof_all = sv.store_profiles(rows)          # 全量(含不在册店):A4/A7 点名用

    # 在册店名(凭证表全集,不做启用/代理过滤——见模块 docstring 纪律 2)
    registered, reg_err = None, None
    try:
        registered = stores_svc.registered_names()
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

    # 冻结行(不在册店的在线行)不进冲突分析——纪律 1
    frozen = ({s for s in prof_all if s not in registered}
              if registered is not None else set())
    live_rows = [r for r in rows if r["store"] not in frozen]
    dropped = len(rows) - len(live_rows)
    prof = sv.store_profiles(live_rows)
    pub_rows = [r for r in live_rows if r["published"]]

    # ── P 探针 ──
    L.append("═══ P 探针(设计稿假设 → 实测)═══")
    L.append(f"P1 候选池:US 产品 {pool['total']} / approved {pool['approved']} → "
             f"有标题 {pool['with_title']} → PT 有效 {pool['with_pt']} → "
             f"**大类查得到 {pool['with_cat']}(引擎可分候选)**;PT 实证 "
             f"{pool['pt_evid']} / 推断 {pool['with_pt'] - pool['pt_evid']}")
    L.append(f"   ⚠ 该数未扣渠道未知与运费缺失两关,引擎实际候选还会再收窄;"
             f"approved 里 brand 非空 {pool['with_brand']}(占位符另计,见 P4)")
    L.append(f"P2 打分信号(近 5 万条 ok 快照):rating {sig['n_rating']} / "
             f"review_count {sig['n_review']} / is_fba {sig['n_fba']}"
             + ("——**两者为 0:v1 权重必须删掉评分/评论项**(禁止 or 0)"
                if not sig["n_rating"] and not sig["n_review"] else ""))
    if dic_err:
        L.append(f"P3 PT 字典对拍:跳过({dic_err})")
    else:
        L.append(f"P3 PT 字典对拍:risk_product_types {dic['n_risk']}(带大类 "
                 f"{dic['n_risk_cat']})vs audit.walmart_pt_meta {dic['n_meta']};"
                 f"仅前者有 {dic['only_risk']} / 仅后者有 {dic['only_meta']} / "
                 f"大类取值不一致 {dic['cat_diff']}")
    L.append(f"   大类取值域实测 {len(cats)} 个(设计稿写 27,以本行为准):"
             + ", ".join(f"{c}×{n}" for c, n in cats[:12])
             + (" …" if len(cats) > 12 else ""))
    n_key = sum(1 for r in rows if r["brand_key"])
    L.append(f"P4 品牌占用键(在线行口径):可占用 {n_key} / 真·无品牌 "
             f"{st['no_brand']} / 产品库无此行 {st['asin_not_in_products']}")

    # ── A 存量审计 ──
    L.append("═══ A 存量审计(在线口径 missing_since IS NULL)═══")
    n_pub = sum(1 for r in rows if r["published"])
    L.append(f"A0 在线行 {st['online']}(已发布 {n_pub} / 未发布 "
             f"{st['online'] - n_pub}——KPI 表的在线数只算已发布,两个数不同源);"
             f"sku 提不出 ASIN {st['no_asin']}"
             + ("(形态:" + sv._fmt_counter(Counter(
                 {k[5:]: v for k, v in st.items() if k.startswith("form_")})) + ")"
                if st["no_asin"] else "")
             + f";归不到大类 {st['no_category']}"
             + f"(大类来源:在线PT {st['cat_from_item']} / 审核PT兜底 "
               f"{st['cat_from_product']})")
    if registered is None:
        L.append(f"⚠ 凭证表读取失败({reg_err}),**本轮未排除已不在册店的冻结行**"
                 f"——A1/A2/A3/A5 的数含幻影店铺,只可参考不可据以下架")
    else:
        L.append(f"   已排除不在册店的冻结行 {dropped} 行 / {len(frozen)} 家店"
                 f"(下面 A1~A3、A5 均为在册店口径)")

    a1 = sv.cross_store(live_rows, "asin")
    L.append(f"A1 同 ASIN 跨店在线:{len(a1)} 个 ASIN"
             + (";" + "; ".join(f"{a}→{sv._fmt_counter(Counter(d))}"
                                for a, d in a1[:sample]) if a1 else "(无)"))
    a2 = sv.cross_store(live_rows, "brand_key")
    L.append(f"A2 同品牌跨店在线:{len(a2)} 个品牌"
             + (";" + "; ".join(f"{b}→{len(d)}店/{sum(d.values())}件"
                                for b, d in a2[:sample]) if a2 else "(无)"))

    over = sorted(((s, p) for s, p in prof.items() if len(sv.real_cats(p)) > 2),
                  key=lambda x: (-len(sv.real_cats(x[1])), -x[1]["n"], x[0]))
    L.append(f"A3 每店大类:在册且有在线行的 {len(prof)} 家;超 2 大类的 {len(over)} 家"
             + (";" + "; ".join(f"{s}({len(sv.real_cats(p))}类:"
                                f"{sv._fmt_counter(p['categories'], 3)})"
                                for s, p in over[:sample]) if over else "")
             + f";全局大类来源:在线PT {st['cat_from_item']}、审核PT兜底 "
               f"{st['cat_from_product']}(兜底那部分可能是 LLM 推断,"
               f"开新类目时需按 §十二.14⑥ 复核 pt_source)")

    if registered is None:
        L.append(f"A4 不在册店冻结行:跳过(凭证表读取失败:{reg_err})")
    else:
        dead = sorted(((s, prof_all[s]["n"]) for s in frozen), key=lambda x: (-x[1], x[0]))
        L.append(f"A4 不在册店冻结行:{len(dead)} 家店已不在凭证表却仍有在线行,"
                 f"合计 {sum(n for _, n in dead)} 行"
                 + (";" + ", ".join(f"{s}×{n}" for s, n in dead[:sample])
                    if dead else "(无)")
                 + ("——这些行永久冻结为「在架」,污染在线表投影/list_new 全局"
                    "去重闸/maintenance;处置见 allocation_plan §十二.11"
                    if dead else ""))
        if live_api is not None:
            filtered = sorted((registered & set(prof_all)) - live_api)
            L.append(f"   在册但被凭证过滤(启用=否/缺 ClientId/缺代理)的 "
                     f"{len(filtered)} 家:{', '.join(filtered[:sample]) or '无'}"
                     f"——**这些不是死店**,是配置缺失,绝不进整店释放清单")
        else:
            L.append(f"   ⚠ load_stores 读取失败({live_err}),无法区分"
                     f"「配置缺失」与「真不在册」")

    if cfg_err:
        L.append(f"A5/A6 渠道对拍与配置完备度:跳过(限额表读取失败:{cfg_err})")
    else:
        n_cfg_ch = sum(1 for c in cfg.values() if c.get("channel"))
        if not with_channel:
            L.append(f"A5 渠道对拍:**跳过**(-p channel=0,本轮未取渠道)"
                     f"——填了配送限制的店 {n_cfg_ch} 家,去掉该参数重跑才有结论")
        else:
            mism = sv.channel_mismatch(sv.store_profiles(pub_rows), cfg)
            L.append(f"A5 渠道对拍(已发布行口径:未发布的下架无意义):"
                     f"填了配送限制的店 {n_cfg_ch} 家;存在不符商品的 {len(mism)} 家"
                     + (";" + "; ".join(
                         f"{s}(限{w},不符{n}件:{sv._fmt_counter(Counter(d))})"
                         for s, w, n, d in mism[:sample]) if mism else "")
                     + f";渠道值认不出的行 {st['channel_weird']}"
                     + ("(恒高说明采集侧 is_fba 解析坏了,是修采集不是下架商品)"
                        if st["channel_weird"] else ""))
        # A6 分母 = 在册店全集(空店也要点名:它们是梯队 2 的入场券)
        scope = sorted(registered | set(prof)) if registered is not None else sorted(prof)
        miss = store_targets.missing_config(cfg, scope)
        empty = [s for s in scope if s not in prof]
        L.append(f"A6 店铺配置:{len(scope)} 家在册店中 {len(miss)} 家缺列"
                 + ("(分母已退化为「有在线行的店」:凭证表读取失败)"
                    if registered is None else f",其中空店 {len(empty)} 家")
                 + (";" + "; ".join(f"{s}:{'/'.join(v)}"
                                    for s, v in list(miss.items())[:sample])
                    if miss else "(已填齐)"))

    # 状态 fail-open 与全仓一致:无记录 / 状态列为空 一律视同 ACTIVE
    non_active = sorted(s for s in prof_all if (status.get(s) or "ACTIVE") != "ACTIVE")
    no_status = sum(1 for s in prof_all if not status.get(s))
    L.append(f"A7 店铺状态:有在线行但非 ACTIVE 的 {len(non_active)} 家"
             + (";" + ", ".join(f"{s}={status[s]}" for s in non_active[:sample])
                if non_active else "")
             + f"(无 KPI 记录或状态为空、按 fail-open 视同 ACTIVE 的 {no_status} 家)"
             + "——SUSPENDED 店的占用按设计保持,其在线行仍计入 A1/A2 冲突")

    # A8 订单店名对账:对不上的店,其销量进不了"店×类目"维度
    if registered is None:
        L.append("A8 订单店名对账:跳过(凭证表读取失败)")
    else:
        miss = [(s, n, h) for s, n, h in order_stores if s not in registered]
        L.append(f"A8 订单店名对账(近 {sales_days} 天):订单里 {len(order_stores)} 家店,"
                 f"与凭证表对不上 {len(miss)} 家、{sum(n for _, n, _ in miss)} 行"
                 + (";" + ", ".join(f"{s}×{n}" for s, n, _ in miss[:sample])
                    if miss else "(全部对得上)")
                 + ("——这些行照样在库里(事实表永存原文),只是销量只进产品/品牌/"
                    "类目三个全局维度,不进店×类目维度;若其中有**还在营只是改过名**"
                    "的店,它的近期信号会凭空少一截,需要一张改名映射表"
                    if miss else ""))

    # ── C 处置清单(落盘 csv,给人照着做)──
    if not export:
        L.append("═══ C 处置清单:跳过(-p export=0)═══")
        return "\n".join(L)

    L.append(f"═══ C 处置清单(csv 落 {paths.reports_dir()},每次覆盖)═══")
    n_sales_keys = len(sales)
    L.append(f"   销量口径:近 {sales_days} 天「有效销售」行按 (店,SKU) 聚合,"
             f"命中 {n_sales_keys} 个组合"
             + ("——**订单历史还没导入,冲突清单只能按在线件数打平**,"
                "先跑 order_history_import -p apply=1 再重跑本报告"
                if n_sales_keys == 0 else ""))

    if cfg_err:
        L.append("   类目建议/渠道清单:跳过(限额表读不到,无法对拍已填值)")
    else:
        # C1 类目建议:所有者据此填飞书「类目1/2/3」三列
        sug = sv.suggest_categories(prof, cfg)
        rows_c1 = [(s, len([c for c, _ in rk]), "|".join(top),
                    "|".join(filled), "一致" if set(filled) == set(top)
                    else ("未填" if not filled else "不一致"),
                    "; ".join(f"{c}×{n}" for c, n in rk[:8]))
                   for s, top, rk, filled in sug]
        p1 = _write_csv("alloc_类目建议.csv",
                        ["店铺", "在线大类数", "建议类目(在线数 top2)",
                         "表格已填", "对拍", "在线大类分布"], rows_c1)
        unfilled = sum(1 for r in rows_c1 if r[4] == "未填")
        diff = sum(1 for r in rows_c1 if r[4] == "不一致")
        L.append(f"C1 类目建议 {len(rows_c1)} 家店 → {p1};其中表格未填 "
                 f"{unfilled} 家、已填但与在线 top2 不一致 {diff} 家"
                 f"(**三列都空 = 不限制类目**,填了才生效)")

        # C2 渠道不符逐行清单:所有者自己去下架
        off = sv.channel_offenders(live_rows, cfg)
        p2 = _write_csv("alloc_渠道不符下架清单.csv",
                        ["店铺", "限定渠道", "SKU", "ASIN", "实际渠道",
                         "大类", "PT"],
                        [(r["store"], cfg[r["store"]]["channel"], r["sku"],
                          r["asin"] or "", r["channel"], r["category"] or "",
                          r["pt"] or "") for r in off])
        L.append(f"C2 渠道不符 {len(off)} 件(已发布行)→ {p2}"
                 f"——只列确实是另一个已知渠道的;N/A 与未采到不进清单")

    # C3/C4 冲突处置:留销量大的店 → 降级阶梯(所有者口径 2026-08-15;
    # 实测 96% 的组两边都零销量,只看商品销量等于把两千多组丢给人工)
    metrics = sv.store_metrics(live_rows, sales)
    for tag, field, fname in (("C3 同 ASIN 跨店", "asin", "alloc_同ASIN冲突处置.csv"),
                              ("C4 同品牌跨店", "brand_key", "alloc_同品牌冲突处置.csv")):
        res = sv.resolve_conflicts(live_rows, sales, field, metrics)
        rows_x = [(key, keep, level, st, sku, asin, o, g, cg, sg, verdict)
                  for key, keep, _, detail, level in res
                  for st, sku, asin, o, g, cg, sg, verdict in detail]
        p = _write_csv(fname, ["冲突键", "保留店", "判定依据", "店铺", "SKU",
                               "ASIN", f"近{sales_days}天单量", "该商品销售额",
                               "该店该大类销售额", "该店整体销售额", "处置"],
                       rows_x)
        by_level = Counter(r[4] for r in res)
        hard = by_level.get(sv.LADDER[3], 0) + by_level.get(sv.LADDER[4], 0)
        L.append(f"{tag}:{len(res)} 组、{len(rows_x)} 行 → {p};判定依据分布:"
                 + ", ".join(f"{k}×{v}" for k, v in by_level.most_common())
                 + (f";**其中 {hard} 组连店铺整体销量都分不出、只能按在线件数/"
                    f"店名定序**(这些才需要人眼看)" if hard else
                    ";全部有销售依据可判"))

    L.append("→ 下一步:①按 C1 填飞书类目三列(填了才限制,空=不限制);"
             "②按 C2 自行下架渠道不符商品;③C3/C4 确认后进 A1 回填 claims")
    return "\n".join(L)
