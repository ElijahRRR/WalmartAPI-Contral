"""alloc_backfill — 存量在线商品 → 占用台账(一次性回填)。**危险,默认 dry-run。**

用法:
  python cli.py alloc_backfill                      # 预览:占多少、冲突怎么判
  python cli.py alloc_backfill --execute            # 真回填
  python cli.py alloc_backfill -p include_ties=1 --execute
        # 连"两边都零销量"的冲突组也一并落(默认跳过等人工)
  python cli.py alloc_backfill -p sales_days=180    # 判定销量的窗口(默认 365)
  python cli.py alloc_backfill -p as_of=2026-08-15  # **与出清单那次传同一个值**

把当前在线的商品变成占用台账的初始状态。跨店冲突按所有者口径
(2026-08-15)**留销量大的店**——与 `alloc_audit` C3/C4 清单同一套判定,
先跑 alloc_audit 看清单、认可了再跑这个落库。

**四条纪律**:
1. **只回填在册店的已发布行**:不在册店的行是冻结快照(§九.4),未发布的
   不算真占着货位;
2. **零销量打平的组默认跳过**:两边都没卖过时"留销量大的"无从判起,
   机器按店名定序等于替人做了不可撤销的决定 —— 报告列出来,人认了再
   `-p include_ties=1`;
3. **幂等**:占用键已存在就跳过(ON CONFLICT DO NOTHING),重复跑不会翻倍,
   也不会把已有归属改掉——改归属只能先 store_release 再重来;
4. **落快照**:每条占用记下当时的 PT/pt_source/audit_version,回答"当初
   按什么分的"。

回填之后 list_new 的占用闸才有数据可查(在此之前它恒放行,靠原有的
在线快照去重兜着)。
"""

import logging
from collections import Counter

from registry import db
from services import claims
from services import alloc_survey as sv           # 判定口径与 alloc_audit 同一套
from services import sku_asin, store_targets, stores as stores_svc

DANGEROUS = True

logger = logging.getLogger("workflows.alloc_backfill")

SOURCE = "alloc_backfill"


def _pick(rows, sales, field, include_ties, metrics=None, cfg=None):
    """输入:富化行 + 销量 + 冲突键名 + 是否含打平组
    → 输出:({键: 归属店}, 跳过的打平组数)。

    无冲突的键直接归它唯一那家店;有冲突的按 `alloc_survey.resolve_conflicts`
    判(留销量大的店),零销量打平组按开关决定收不收。
    """
    owner: dict[str, str] = {}
    for r in rows:
        v = r.get(field)
        if v and v not in owner:
            owner[v] = r["store"]
    skipped = 0
    for key, keep, _stat, _detail, level in sv.resolve_conflicts(
            rows, sales, field, metrics, cfg):
        # 只有落到**店名**才算真打平(拿字典序当经营决策)。在线件数是库里
        # 查得到的客观数据,算判得出 —— 名单见 sv.NEEDS_HUMAN
        if level in sv.NEEDS_HUMAN and not include_ties:
            owner.pop(key, None)
            skipped += 1
        else:
            owner[key] = keep
    return owner, skipped


def run(params: dict) -> str:
    """输入:params(execute/include_ties/sales_days)→ 输出:回填摘要。"""
    execute = bool(params.get("execute"))
    include_ties = str(params.get("include_ties", "")).lower() in {"1", "true", "yes"}
    sales_days = int(params.get("sales_days", 365))
    win = sv.sales_window(str(params.get("as_of", "")), sales_days)

    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sv._SQL_PT2CAT)
        pt2cat = {pt: c for pt, c in cur.fetchall() if c}
        cur.execute(sv._SQL_ONLINE)
        items = cur.fetchall()
        cur.execute(sv._SQL_SALES, win)
        sales = {(s, k): (int(o), float(g)) for s, k, o, g in cur.fetchall()}
        asins = sorted({a for a in (sku_asin.extract_asin(it[1])
                                    for it in items) if a})
        # ⚠ **必须带渠道**(2026-08-16 改):渠道不符的行不产生占用,而这个
        # 判定要 latest_snapshot 里的 is_fba。这里省掉的话,`sv.claimable` 的
        # 渠道闸对回填恒为假(channel 全 None ⇒ 一行都不算不符),
        # 于是 alloc_audit 判一套、回填落另一套 —— 正是 alloc_survey 要防的事。
        # 代价是这段 LATERAL 慢一些;回填是一次性动作,值这个钱
        meta = sv._fetch_meta(cur, asins, True)

    rows, st = sv.enrich(items, meta, pt2cat)

    # 纪律 1:只认在册店的已发布行
    try:
        registered = stores_svc.registered_names()
    except Exception as e:                            # noqa: BLE001
        return f"⛔ 凭证表读不到({e}):无法判定哪些行是冻结快照,拒绝回填"
    try:
        cfg = store_targets.load_targets()      # 类目/渠道准入都是硬闸
    except Exception as e:                      # noqa: BLE001
        return f"⛔ 限额表读不到({e}):类目/渠道准入判不了,拒绝回填"
    # 入行口径走 sv.claimable(在册 ∧ 已发布 ∧ 规划内 ∧ 类目 ∧ 渠道),与 alloc_audit 同一处
    # ——两边各写各的筛法,报告说"留 A085"、回填落到别家,清单就是假的。
    # ⚠ **不按店铺状态筛**:SUSPENDED 的店照常回填占用(「暂停不释放、
    # 占用保持」,§六.2)。停用只是暂时不给它分新货,不代表它手上的品牌与
    # 产品可以被别店拿走 —— 要某店不接新货走「单店最大在线数」填 0
    live = sv.claimable(rows, registered, cfg)
    dropped = len(rows) - len(live)
    # "排除 N" 不写明分类等于让人猜(所有者 2026-08-15 就是看到这个数才问
    # "是不是把历史上架过的也算进去了")。五类各自计数,每类都可自行核对;
    # 五项之和必须 == dropped,不然有一类被静默吞掉了
    why = Counter()
    for r in rows:
        if r["store"] not in registered:
            why["不在册店(冻结快照)"] += 1
        elif sv.is_excluded(r["store"]):
            why["规划范围外(谭总系)"] += 1
        elif not r["published"]:
            why["未发布(不占货位)"] += 1
        elif sv.offends_category(r, cfg):
            why["类目不符(在下架清单上)"] += 1
        elif sv.offends_channel(r, cfg):
            why["渠道不符(在下架清单上)"] += 1

    metrics = sv.store_metrics(live, sales)
    brand_owner, brand_ties = _pick(live, sales, "brand_key", include_ties,
                                    metrics, cfg)
    prod_owner, prod_ties = _pick(live, sales, "asin", include_ties, metrics, cfg)

    snap = {}
    for r in live:
        snap.setdefault(r["asin"], r)
    to_claim = [
        {"kind": claims.BRAND, "claim_key": k, "store": s, "source": SOURCE}
        for k, s in sorted(brand_owner.items())
    ] + [
        {"kind": claims.PRODUCT, "claim_key": k, "store": s, "source": SOURCE,
         "walmart_pt": (snap.get(k) or {}).get("pt"),
         "pt_source": (snap.get(k) or {}).get("pt_source")}
        for k, s in sorted(prod_owner.items())
    ]

    per_store = Counter(r["store"] for r in to_claim)
    head = (f"在线行 {st['online']}(已排 RETIRED 退市行),"
            f"入选(在册∧已发布∧规划内∧类目准入∧渠道准入){len(live)}、排除 {dropped}"
            + (";其中 " + "、".join(f"{k} {v}" for k, v in why.most_common())
               if why else "")
            + f"\n   将占品牌 {len(brand_owner)}、产品 {len(prod_owner)}"
            f";涉及 {len(per_store)} 家店"
            f"\n   销量窗口 {win['day']} 往前 {sales_days} 天 —— "
            f"**与出清单那次必须一致**,不一致就是照 A 的清单落 B 的判定")
    ties_note = (f";**无销售依据、只能按件数/店名定序而跳过:品牌 {brand_ties} 组 / "
                 f"产品 {prod_ties} 组**"
                 "(机器判不出谁该留,先看 alloc_audit 的 C3/C4 清单,"
                 "认了再 -p include_ties=1)"
                 if (brand_ties or prod_ties) and not include_ties
                 else (";打平组已按 include_ties=1 一并纳入" if include_ties else ""))

    if not execute:
        top = ", ".join(f"{s}×{n}" for s, n in per_store.most_common(10))
        return (f"🧪 回填预览:{head}{ties_note}\n"
                f"   占用最多的店:{top}\n"
                f"   幂等:已存在的占用键会跳过(不覆盖已有归属;"
                f"改归属要先 store_release)\n"
                f"   确认后加 --execute")

    with db.pg_conn() as conn:
        ok, conflicts = claims.claim_many(conn, to_claim)
    logger.warning("alloc_backfill 落库:成功 %d,已被别店占 %d", ok, len(conflicts))
    csample = "; ".join(f"{k}:{key} 想给 {want} 但属于 {has}"
                        for k, key, want, has in conflicts[:10])
    return (f"✅ 回填完成:{head}{ties_note};落库 {ok} 条"
            + (f";与已有占用冲突 {len(conflicts)} 条(保持原归属不动):{csample}"
               if conflicts else ";无冲突")
            + ";接着可开 list_new 的占用闸")
