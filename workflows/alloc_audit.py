"""alloc_audit — 分配动工前的存量审计 + 数据探针(只读,随时可跑)。

用法:
  python cli.py alloc_audit                 # 全部检查,摘要 + 各表前 10 行样例
  python cli.py alloc_audit -p sample=30    # 样例条数
  python cli.py alloc_audit -p channel=0    # 跳过渠道探测(最慢的一段)

这是 docs/allocation_plan.md §十三 的 **A0.5 批次**:占用台账(A1)与分配
引擎(A2)动工前,必须先知道存量长什么样、设计稿里的假设数字实际是多少。
**只读**——不写任何表、不调沃尔玛、不调 LLM。

两部分:

**P 探针**(把设计稿里的假设换成实测;出处 2026-08-14 实现校准表)
  P1 候选池分母:approved 有多少,过完"合格候选"四道谓词还剩多少;
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
  A4 死店冻结行 —— 有在线行但已不在凭证表的店(§十二.11 的存量面);
  A5 每店渠道分布 vs「配送限制」列对拍 —— 不一致的进过渡下架清单;
  A6 店铺配置完备度 —— 四列没填齐的店(引擎硬闸的前置)。

sku→asin 走 services/sku_asin 唯一规则,提不出的**单列计数**不猜;
提不出即该行不参与 A1/A2/A5(报告里能看到这部分有多大)。
"""

import logging
from collections import Counter, defaultdict

from registry import db
from services import brand_key as bk
from services import sku_asin, store_targets, stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.alloc_audit")

_CHUNK = 5000

# ── P1 候选池:四道谓词逐层收窄(口径与 product_audit/catalog_health 对齐)──
# title 非空:排掉 pt_backfill 造的占位行;pt <> 'unknown' :旧系统量产字面量
_SQL_POOL = """
SELECT count(*)                                                   AS total,
       count(*) FILTER (WHERE audit_status = 'approved')          AS approved,
       count(*) FILTER (WHERE audit_status = 'approved'
                          AND title IS NOT NULL AND btrim(title) <> '') AS with_title,
       count(*) FILTER (WHERE audit_status = 'approved'
                          AND title IS NOT NULL AND btrim(title) <> ''
                          AND walmart_pt IS NOT NULL
                          AND walmart_pt <> 'unknown')            AS with_pt,
       count(*) FILTER (WHERE audit_status = 'approved'
                          AND title IS NOT NULL AND btrim(title) <> ''
                          AND walmart_pt IS NOT NULL
                          AND walmart_pt <> 'unknown'
                          AND pt_source = 'walmart_confirmed')    AS pt_evid,
       count(*) FILTER (WHERE audit_status = 'approved'
                          AND brand IS NOT NULL AND btrim(brand) <> '') AS with_brand
FROM catalog.products WHERE marketplace = 'US'
"""

# ── P2 打分信号探针:契约 v1 字段表没有 rating/review_count,设计稿断言
#    "随 raw 落进来"——本仓零证据,必须实测。取样而非全表:1.2 亿行 jsonb
#    全扫没必要,有没有这回事看 5 万条最新快照就够了
_SQL_SIGNAL = """
SELECT count(*)                                            AS n,
       count(*) FILTER (WHERE raw ? 'rating')              AS n_rating,
       count(*) FILTER (WHERE raw ? 'review_count')        AS n_review,
       count(*) FILTER (WHERE raw ? 'is_fba')              AS n_fba
FROM (SELECT raw FROM catalog.snapshots
      WHERE outcome = 'ok' ORDER BY scraped_at DESC LIMIT 50000) t
"""

_SQL_PT_DICT = """
SELECT (SELECT count(*) FROM catalog.risk_product_types)               AS n_risk,
       (SELECT count(*) FROM catalog.risk_product_types
         WHERE category IS NOT NULL AND btrim(category) <> '')         AS n_risk_cat,
       (SELECT count(*) FROM audit.walmart_pt_meta)                    AS n_meta,
       (SELECT count(*) FROM catalog.risk_product_types r
         WHERE NOT EXISTS (SELECT 1 FROM audit.walmart_pt_meta m
                            WHERE m.product_type = r.product_type))    AS only_risk,
       (SELECT count(*) FROM audit.walmart_pt_meta m
         WHERE NOT EXISTS (SELECT 1 FROM catalog.risk_product_types r
                            WHERE r.product_type = m.product_type))    AS only_meta,
       (SELECT count(*) FROM catalog.risk_product_types r
          JOIN audit.walmart_pt_meta m ON m.product_type = r.product_type
         WHERE btrim(coalesce(r.category, '')) <>
               btrim(coalesce(m.walmart_category, '')))                AS cat_diff
"""

_SQL_CATEGORIES = """
SELECT btrim(category) AS cat, count(*) AS n
FROM catalog.risk_product_types
WHERE category IS NOT NULL AND btrim(category) <> ''
GROUP BY 1 ORDER BY 2 DESC
"""

_SQL_ONLINE = """
SELECT store, sku, product_type
FROM catalog.walmart_items WHERE missing_since IS NULL
"""

# 一次拿齐品牌/PT/渠道:渠道那段是 amz_source._SQL 的 LATERAL 口径
# (latest_snapshot 按 scrape_params 分组会一个 ASIN 出多行,不能裸 JOIN;
#  zip_verify='mismatch' 的观测不算数)
_SQL_META = """
SELECT p.asin, p.brand, p.slow ->> 'manufacturer', p.walmart_pt, p.pt_source,
       s.fulfillment
FROM catalog.products p
LEFT JOIN LATERAL (
    SELECT raw ->> 'is_fba' AS fulfillment
    FROM catalog.latest_snapshot ls
    WHERE ls.marketplace = p.marketplace AND ls.asin = p.asin
      AND coalesce(ls.scrape_params ->> 'zip_verify', '') <> 'mismatch'
    ORDER BY ls.scraped_at DESC LIMIT 1
) s ON true
WHERE p.marketplace = 'US' AND p.asin = ANY(%s)
"""

_SQL_META_NO_CHANNEL = """
SELECT p.asin, p.brand, p.slow ->> 'manufacturer', p.walmart_pt, p.pt_source,
       NULL AS fulfillment
FROM catalog.products p
WHERE p.marketplace = 'US' AND p.asin = ANY(%s)
"""

_SQL_PT2CAT = """
SELECT product_type, btrim(coalesce(category, '')) FROM catalog.risk_product_types
"""

# 店铺状态:全仓统一写法(每店最新一行);无记录 fail-open 视同 ACTIVE
_SQL_STATUS = """
SELECT DISTINCT ON (store) store, store_status
FROM ops.store_kpi_daily ORDER BY store, data_date DESC
"""


# ── 纯函数(逻辑都在这里,好测)────────────────────────────────────────────

def enrich(items, meta, pt2cat):
    """输入:在线行 [(store, sku, product_type)] + {asin: 元数据} + {PT: 大类}
    → 输出:(富化行 list, 统计 dict)。

    每行补:asin(提不出为 None)、品牌占用键、大类(在线 PT 优先,缺则用
    产品审核 PT 兜底——在线 PT 是沃尔玛认过的,比审核推断更硬)、渠道。
    """
    rows, st = [], Counter()
    for store, sku, pt in items:
        st["online"] += 1
        asin = sku_asin.extract_asin(sku)
        if asin is None:
            st["no_asin"] += 1
            st[f"form_{sku_asin.classify(sku)}"] += 1
        m = meta.get(asin) if asin else None
        if asin and m is None:
            st["asin_not_in_products"] += 1
        item_pt = (pt or "").strip()
        prod_pt = (m or {}).get("walmart_pt") or ""
        cat = pt2cat.get(item_pt) or pt2cat.get(prod_pt.strip()) or None
        if cat is None:
            st["no_category"] += 1
        key = bk.brand_key((m or {}).get("brand"),
                           (m or {}).get("manufacturer")) if m else None
        if m and key is None:
            st["no_brand"] += 1
        rows.append({"store": store, "sku": sku, "asin": asin,
                     "brand_key": key, "category": cat,
                     "channel": ((m or {}).get("fulfillment") or "").strip().upper() or None,
                     "pt": item_pt or prod_pt or None,
                     "pt_source": (m or {}).get("pt_source")})
    return rows, st


def cross_store(rows, field):
    """输入:富化行 + 键名('asin'/'brand_key')→ 输出:跨店冲突
    [(键, {店: 件数})],按涉及店铺数降序。

    只看**在线**行:占用台账还不存在,存量冲突只能从观测看出来。
    """
    idx = defaultdict(Counter)
    for r in rows:
        v = r.get(field)
        if v:
            idx[v][r["store"]] += 1
    out = [(k, dict(c)) for k, c in idx.items() if len(c) > 1]
    out.sort(key=lambda kv: (-len(kv[1]), -sum(kv[1].values()), str(kv[0])))
    return out


def store_profiles(rows):
    """输入:富化行 → 输出:{店: {n, categories: Counter, channels: Counter}}。"""
    prof: dict[str, dict] = {}
    for r in rows:
        p = prof.setdefault(r["store"], {"n": 0, "categories": Counter(),
                                         "channels": Counter()})
        p["n"] += 1
        p["categories"][r["category"] or "(未归类)"] += 1
        p["channels"][r["channel"] or "(未知)"] += 1
    return prof


def channel_mismatch(prof, cfg):
    """输入:店铺画像 + 限额表配置 → 输出:[(店, 限制渠道, 不符件数, 分布)]。

    只对**填了配送限制**的店对拍;渠道未知的在线行不算不符(采集没采到,
    不是货不对)——把"没采到"算成"不符"会让下架清单混进无辜商品。
    """
    out = []
    for store, p in prof.items():
        want = (cfg.get(store) or {}).get("channel")
        if not want:
            continue
        bad = sum(n for ch, n in p["channels"].items()
                  if ch not in ("(未知)", want))
        if bad:
            out.append((store, want, bad, dict(p["channels"])))
    out.sort(key=lambda x: -x[2])
    return out


def _fmt_counter(c: Counter, top=4) -> str:
    return ", ".join(f"{k}×{n}" for k, n in c.most_common(top))


# ── 取数 ────────────────────────────────────────────────────────────────

def _fetch_meta(cur, asins: list[str], with_channel: bool) -> dict:
    sql = _SQL_META if with_channel else _SQL_META_NO_CHANNEL
    out: dict[str, dict] = {}
    for i in range(0, len(asins), _CHUNK):
        cur.execute(sql, (asins[i:i + _CHUNK],))
        for asin, brand, manu, pt, src, ful in cur.fetchall():
            out[asin] = {"brand": brand, "manufacturer": manu,
                         "walmart_pt": pt, "pt_source": src, "fulfillment": ful}
    return out


def run(params: dict) -> str:
    """输入:params(sample/channel)→ 输出:探针 + 存量审计报告。"""
    sample = int(params.get("sample", 10))
    with_channel = str(params.get("channel", "1")).lower() not in {"0", "false", "no"}
    L: list[str] = []

    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_POOL)
        pool = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        cur.execute(_SQL_SIGNAL)
        sig = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        cur.execute(_SQL_PT_DICT)
        dic = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        cur.execute(_SQL_CATEGORIES)
        cats = cur.fetchall()
        cur.execute(_SQL_PT2CAT)
        pt2cat = {pt: c for pt, c in cur.fetchall() if c}
        cur.execute(_SQL_ONLINE)
        items = cur.fetchall()
        cur.execute(_SQL_STATUS)
        status = {s: (st or "").upper() for s, st in cur.fetchall()}

        asins = sorted({a for a in (sku_asin.extract_asin(sku)
                                    for _, sku, _ in items) if a})
        meta = _fetch_meta(cur, asins, with_channel)

    rows, st = enrich(items, meta, pt2cat)
    prof = store_profiles(rows)

    # 店铺清单与配置(飞书;任一路读不到就降级那一节,不让整份报告失败)
    live: set[str] = set()
    store_err = None
    try:
        live = {s["name"] for s in stores_svc.load_stores()}
    except Exception as e:                      # noqa: BLE001 报告降级,不阻断
        store_err = str(e)
    cfg: dict[str, dict] = {}
    cfg_err = None
    try:
        cfg = store_targets.load_targets()
    except Exception as e:                      # noqa: BLE001 同上
        cfg_err = str(e)

    # ── P 探针 ──
    L.append("═══ P 探针(设计稿假设 → 实测)═══")
    L.append(f"P1 候选池:US 产品 {pool['total']} / audit_status=approved "
             f"{pool['approved']} → 有标题 {pool['with_title']} → PT 有效 "
             f"{pool['with_pt']}(**合格候选**;其中 PT 实证 {pool['pt_evid']}、"
             f"推断 {pool['with_pt'] - pool['pt_evid']})")
    L.append(f"   approved 里 brand 非空 {pool['with_brand']}"
             f"(占位符还要再筛,见 P4)")
    L.append(f"P2 打分信号(近 5 万条 ok 快照):rating {sig['n_rating']} / "
             f"review_count {sig['n_review']} / is_fba {sig['n_fba']}"
             + ("——**rating/review 为 0:v1 权重必须删掉这两项**(禁止 or 0)"
                if not sig["n_rating"] and not sig["n_review"] else ""))
    L.append(f"P3 PT 字典对拍:risk_product_types {dic['n_risk']}(带大类 "
             f"{dic['n_risk_cat']})vs audit.walmart_pt_meta {dic['n_meta']};"
             f"仅前者有 {dic['only_risk']} / 仅后者有 {dic['only_meta']} / "
             f"大类取值不一致 {dic['cat_diff']}")
    L.append(f"   大类取值域实测 {len(cats)} 个(设计稿写 27):"
             + ", ".join(f"{c}×{n}" for c, n in cats[:12])
             + (" …" if len(cats) > 12 else ""))
    n_key = sum(1 for r in rows if r["brand_key"])
    L.append(f"P4 品牌占用键(在线行口径):可占用 {n_key} / 真·无品牌 "
             f"{st['no_brand']} / 产品库无此行 {st['asin_not_in_products']}")

    # ── A 存量审计 ──
    L.append("═══ A 存量审计(在线口径 missing_since IS NULL)═══")
    L.append(f"A0 在线行 {st['online']};sku 提不出 ASIN {st['no_asin']}"
             + (f"(形态:" + _fmt_counter(Counter(
                 {k[5:]: v for k, v in st.items() if k.startswith("form_")})) + ")"
                if st["no_asin"] else "")
             + f";归不到大类 {st['no_category']}")

    a1 = cross_store(rows, "asin")
    L.append(f"A1 同 ASIN 跨店在线:{len(a1)} 个 ASIN"
             + (";" + "; ".join(f"{a}→{_fmt_counter(Counter(d))}"
                                for a, d in a1[:sample]) if a1 else "(无)"))
    a2 = cross_store(rows, "brand_key")
    L.append(f"A2 同品牌跨店在线:{len(a2)} 个品牌"
             + (";" + "; ".join(f"{b}→{len(d)}店/{sum(d.values())}件"
                                for b, d in a2[:sample]) if a2 else "(无)"))

    over = [(s, p) for s, p in prof.items()
            if len([c for c in p["categories"] if c != "(未归类)"]) > 2]
    over.sort(key=lambda x: -len(x[1]["categories"]))
    L.append(f"A3 每店大类:共 {len(prof)} 家店有在线行;超 2 大类的 {len(over)} 家"
             + (";" + "; ".join(
                 f"{s}({len([c for c in p['categories'] if c != '(未归类)'])}类:"
                 f"{_fmt_counter(p['categories'], 3)})" for s, p in over[:sample])
                if over else ""))

    if store_err:
        L.append(f"A4 死店冻结行:跳过(店铺凭证表读取失败:{store_err})")
    else:
        dead = sorted(((s, p["n"]) for s, p in prof.items() if s not in live),
                      key=lambda x: -x[1])
        L.append(f"A4 死店冻结行:{len(dead)} 家店已不在凭证表却仍有在线行,"
                 f"合计 {sum(n for _, n in dead)} 行"
                 + (";" + ", ".join(f"{s}×{n}" for s, n in dead[:sample])
                    if dead else "(无)")
                 + ("——这些行永久冻结为「在架」,污染在线表投影/list_new 全局"
                    "去重闸/maintenance;处置见 allocation_plan §十二.11"
                    if dead else ""))

    if cfg_err:
        L.append(f"A5/A6 渠道对拍与配置完备度:跳过(限额表读取失败:{cfg_err})")
    else:
        mism = channel_mismatch(prof, cfg)
        L.append(f"A5 渠道对拍:填了配送限制的店 "
                 f"{sum(1 for c in cfg.values() if c.get('channel'))} 家;"
                 f"存在不符商品的 {len(mism)} 家"
                 + (";" + "; ".join(f"{s}(限{w},不符{n}件:{_fmt_counter(Counter(d))})"
                                    for s, w, n, d in mism[:sample]) if mism else ""))
        alive_with_items = sorted(s for s in prof if not live or s in live)
        miss = store_targets.missing_config(cfg, alive_with_items)
        L.append(f"A6 店铺配置:{len(alive_with_items)} 家在营有货店中 {len(miss)} 家缺列"
                 + (";" + "; ".join(f"{s}:{'/'.join(v)}"
                                    for s, v in list(miss.items())[:sample])
                    if miss else "(已填齐)"))

    non_active = sorted(s for s in prof if status.get(s, "ACTIVE") != "ACTIVE")
    L.append(f"A7 店铺状态:有在线行但状态非 ACTIVE 的 {len(non_active)} 家"
             + (";" + ", ".join(f"{s}={status[s]}" for s in non_active[:sample])
                if non_active else "")
             + f"(KPI 无记录视同 ACTIVE:{sum(1 for s in prof if s not in status)} 家)")

    L.append("→ 下一步:所有者按本报告定各店保留大类/渠道与下架清单;"
             "A1/A2 冲突项的处置口径定了才能回填 claims(A1 批次)")
    return "\n".join(L)
