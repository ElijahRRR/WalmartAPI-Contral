"""alloc_backfill — 存量在线商品 → 占用台账(一次性回填)。**危险,默认 dry-run。**

用法:
  python cli.py alloc_backfill                      # 预览:占多少、冲突怎么判
  python cli.py alloc_backfill --execute            # 真回填
  python cli.py alloc_backfill -p include_ties=1 --execute
        # 连"两边都零销量"的冲突组也一并落(默认跳过等人工)
  python cli.py alloc_backfill -p sales_days=180    # 判定销量的窗口(默认 365)

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
        # 只有"连店铺整体销量都分不出"才算真打平(靠在线件数/店名定序)
        if level in (sv.LADDER[3], sv.LADDER[4]) and not include_ties:
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

    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sv._SQL_PT2CAT)
        pt2cat = {pt: c for pt, c in cur.fetchall() if c}
        cur.execute(sv._SQL_ONLINE)
        items = cur.fetchall()
        cur.execute(sv._SQL_SALES, (sales_days,))
        sales = {(s, k): (int(o), float(g)) for s, k, o, g in cur.fetchall()}
        asins = sorted({a for a in (sku_asin.extract_asin(it[1])
                                    for it in items) if a})
        meta = sv._fetch_meta(cur, asins, False)   # 回填不需要渠道

    rows, st = sv.enrich(items, meta, pt2cat)

    # 纪律 1:只认在册店的已发布行
    try:
        registered = stores_svc.registered_names()
    except Exception as e:                            # noqa: BLE001
        return f"⛔ 凭证表读不到({e}):无法判定哪些行是冻结快照,拒绝回填"
    # 纪律 1 追加:规划范围外的店(店名含「谭总」等)不占任何品牌与产品
    # ——所有者定稿 2026-08-15,其他店可与它们重复上架
    # ⚠ **不按店铺状态筛**:SUSPENDED 的店照常回填占用(「暂停不释放、
    # 占用保持」,§六.2)。停用只是暂时不给它分新货,不代表它手上的品牌与
    # 产品可以被别店拿走 —— 要某店不接新货走「单店最大在线数」填 0
    live = [r for r in rows if r["store"] in registered and r["published"]
            and not sv.is_excluded(r["store"])]
    dropped = len(rows) - len(live)

    try:
        cfg = store_targets.load_targets()      # 类目准入是冲突判定的硬闸
    except Exception as e:                      # noqa: BLE001
        return f"⛔ 限额表读不到({e}):类目准入判不了,拒绝回填"
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
    head = (f"在线行 {st['online']},入选(在册∧已发布){len(live)}、"
            f"排除 {dropped};将占品牌 {len(brand_owner)}、产品 {len(prod_owner)}"
            f";涉及 {len(per_store)} 家店")
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
