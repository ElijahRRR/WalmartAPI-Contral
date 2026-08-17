"""claim_audit — 占用台账对账(只读)。已落的占用,现在还站得住吗?

用法:
  python cli.py claim_audit                     # 全量对账 + 落 csv
  python cli.py claim_audit -p export=0         # 只看摘要

**为什么需要这张表**:占用没有任何自动释放(§六,那是有意的——KPI 是外部
观测,误报触发自动释放的话品牌被别店占走就撤不回来了)。代价是台账只进不出:
回填时闸门漏了一道、或者你后来改了飞书的类目/配送限制,**旧占用不会跟着松开**,
而且**没有任何地方会告诉你**。这条工作流就是那个"告诉你"。

## 只报一种情况:**当初就不该占**

  该店**此刻仍有这个键的在架行**,而这些行**全都**踩了类目或渠道闸
  ⇒ 它们正躺在下架清单上,占用当初就不该产生。

⚠ **商品下架了 ≠ 占用该释放**(§六铁律)。占用是决策不是观测,店铺暂停不释放、
商品下架不释放、KPI 报 TERMINATED 也不释放。所以"该店已经没有这个键的在架行"
这一类**只计数、不列进清单**——那正是占用该保持的样子,列出来会诱人误删。

**只读**:不写任何表、不调沃尔玛。要真释放走 `store_release`(唯一释放路径),
本报告末尾会把命令拼好。
"""

import logging
from collections import Counter

from registry import db, paths
from services import alloc_survey as sv
from services import claims, sku_asin, store_targets, stores as stores_svc
from services import textfmt

DANGEROUS = False

logger = logging.getLogger("workflows.claim_audit")


_SAMPLE = 20        # 单元格里塞几百个 SKU 谁也读不了,超了就截断并标出来


def _reason(rows, cfg) -> str:
    """输入:某键在某店的在架行 + 配置 → 输出:它们踩了哪道闸(给人看的原因)。"""
    bad = Counter()
    for r in rows:
        if sv.offends_category(r, cfg):
            bad[f"类目不符({r.get('category') or '?'})"] += 1
        if sv.offends_channel(r, cfg):
            bad[f"渠道不符({r.get('channel') or '?'})"] += 1
    return " · ".join(f"{k}×{v}" for k, v in bad.most_common())


def _join(rows, field) -> str:
    """输入:在架行 + 字段名 → 输出:去重后的值,竖线分隔(超 _SAMPLE 截断)。

    **品牌占用必须带上是哪几个 SKU/ASIN 惹的**(所有者 2026-08-16:
    「没有给 asin 清单呢?」):`product` 行的占用键本身就是 ASIN,而 `brand`
    行只有品牌名,拿它没法去下架 —— 得回头翻渠道/类目不符下架清单再对一次。
    这两列把那一步省掉,一份 csv 既能改账也能下架。
    """
    vals = sorted({str(r.get(field)) for r in rows if r.get(field)})
    head = "|".join(vals[:_SAMPLE])
    return head + (f"|…共{len(vals)}" if len(vals) > _SAMPLE else "")


def run(params: dict) -> str:
    """输入:params(export)→ 输出:占用对账报告(+ 该释放清单 csv)。"""
    export = str(params.get("export", "1")).lower() not in {"0", "false", "no"}

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sv._SQL_PT2CAT)
            pt2cat = {pt: c for pt, c in cur.fetchall() if c}
            cur.execute(sv._SQL_ONLINE)
            items = cur.fetchall()
            asins = sorted({a for a in (sku_asin.extract_asin(it[1])
                                        for it in items) if a})
            # 必须带渠道:渠道闸判不了的话这张表会漏掉一半问题,且不报错
            meta = sv._fetch_meta(cur, asins, True)
        held = {k: claims.load_active(conn, k)
                for k in (claims.BRAND, claims.PRODUCT)}

    rows, _st = sv.enrich(items, meta, pt2cat)
    try:
        registered = stores_svc.registered_names()
    except Exception as e:                          # noqa: BLE001
        return f"⛔ 凭证表读不到({e}):分不清在册店与冻结快照,拒绝对账"
    try:
        cfg = store_targets.load_targets()
    except Exception as e:                          # noqa: BLE001
        return f"⛔ 限额表读不到({e}):没有类目与渠道就无从对账"

    # 两个集合与 alloc_audit 同名同义:占着货位的 / 该产生占用的
    slot_rows = sv.claimable(rows, registered)
    claim_rows = sv.claimable(rows, registered, cfg)
    ok_keys, slot_keys = {}, {}
    for tag, field in ((claims.BRAND, "brand_key"), (claims.PRODUCT, "asin")):
        ok_keys[tag] = {(r["store"], r[field]) for r in claim_rows if r.get(field)}
        slot_keys[tag] = {}
        for r in slot_rows:
            if r.get(field):
                slot_keys[tag].setdefault((r["store"], r[field]), []).append(r)

    stale, gone, fine = [], Counter(), Counter()
    for kind, owners in held.items():
        for key, store in owners.items():
            here = slot_keys[kind].get((store, key))
            if not here:
                # 该店已经没有这个键的在架行 —— **占用照旧保持**(下架不释放)
                gone[kind] += 1
                continue
            if (store, key) in ok_keys[kind]:
                fine[kind] += 1
                continue
            # 只留**踩闸的那些行**去凑 SKU/ASIN 列:这个键的行此刻全都踩闸
            # (否则不会走到这儿),所以 here 本身就是要下架的那批
            stale.append((kind, key, store, _reason(here, cfg), len(here), here))

    stale.sort(key=lambda x: (x[2], x[0], x[1]))
    n_held = sum(len(v) for v in held.values())
    by_store = Counter(x[2] for x in stale)
    by_kind = Counter(x[0] for x in stale)

    L = ["", "═══ 占用对账 ═══", "",
         f"▍在册占用 {n_held:,}(品牌 {len(held[claims.BRAND]):,} · "
         f"产品 {len(held[claims.PRODUCT]):,})"]
    L += textfmt.table(
        ["", "品牌", "产品", "说明"],
        [["站得住", f"{fine[claims.BRAND]:,}", f"{fine[claims.PRODUCT]:,}",
          "该店仍有在架行,且过类目与渠道闸"],
         ["**当初就不该占**", f"{by_kind[claims.BRAND]:,}",
          f"{by_kind[claims.PRODUCT]:,}", "在架行全踩闸 —— 正躺在下架清单上"],
         ["不判", f"{gone[claims.BRAND]:,}", f"{gone[claims.PRODUCT]:,}",
          "该店已无在架行;**下架不释放**,这是占用该有的样子"]],
        align="<>><")

    if not stale:
        L += ["", "✅ 没有该释放的占用。"]
        return "\n".join(L)

    L += ["", f"▍要你处理:{len(stale):,} 条占用当初就不该产生",
          "  涉及 " + "、".join(f"{s}×{n}" for s, n in by_store.most_common(8))]
    L += textfmt.table(
        ["类型", "占用键", "店铺", "原因", "要下架的 SKU"],
        [[k, key, store, why, _join(here, "sku")]
         for k, key, store, why, _n, here in stale[:12]],
        align="<<<<<")
    if len(stale) > 12:
        L.append(f"  …… 另 {len(stale) - 12:,} 条见 csv")

    if not export:
        L += ["", "(-p export=0:未落 csv)"]
        return "\n".join(L)

    paths.reports_dir().mkdir(parents=True, exist_ok=True)
    p = paths.reports_dir() / "alloc_该释放占用.csv"
    import csv as _csv
    with p.open("w", newline="", encoding="utf-8-sig") as fh:
        w = _csv.writer(fh)
        w.writerow(["类型", "占用键", "占用店", "原因", "该店在架件数",
                    "要下架的SKU", "要下架的ASIN", "释放命令"])
        for k, key, store, why, n, here in stale:
            arg = "brand" if k == claims.BRAND else "asin"
            w.writerow([k, key, store, why, n,
                        _join(here, "sku"), _join(here, "asin"),
                        f"python cli.py store_release -p {arg}={key}"])
    n_sku = len({(r["store"], r["sku"]) for _k, _key, _s, _w, _n, here in stale
                 for r in here})
    L += ["", f"▍明细 → {p}",
          f"  「要下架的 SKU / ASIN」两列 = 这条占用是被哪几件货拖下水的,"
          f"去重后共 {n_sku} 件 —— **一份 csv 既能改账也能下架**,",
          "  不用再回头对渠道/类目不符下架清单。",
          "  ⚠ 同一件货会在 brand 行与 product 行各出现一次(一件货同时拖着",
          "  它的品牌占用和它自己的产品占用),按 SKU 去重后才是真的件数。",
          "", "  最后一列是拼好的释放命令。**先挑几条 dry-run 看清楚再批量跑** ——",
          "  释放本身可逆(released 行永不删),但释放后这个品牌会被下一轮",
          "  分配给别的店,那一步就不可逆了。"]
    return "\n".join(L)
