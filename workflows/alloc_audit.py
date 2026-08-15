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
  C3 同 ASIN 跨店处置 / C4 同品牌跨店处置 —— 按"留销量大的店"给出保留/下架,
     **两边都零销量的组单独标记**(机器判不出谁该留,要人眼看)。

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
from collections import Counter, defaultdict

from registry import db, paths
from services import brand_key as bk
from services import sku_asin, store_targets, stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.alloc_audit")

_CHUNK = 5000
UNCLASSIFIED = "(未归类)"
UNKNOWN_CHANNEL = "(未知)"

# ── P1 候选池:五道谓词逐层收窄(口径与 product_audit/catalog_health 对齐)──
# title 非空:排掉 pt_backfill 造的占位行;pt <> 'unknown':旧系统量产字面量;
# 末层 EXISTS:PT 在字典里查得到**非空大类**——查不到大类的产品过不了
# "一店两大类"这道硬闸,不该计进"引擎能分的量"
_SQL_POOL = """
WITH p AS (
    SELECT walmart_pt, pt_source, brand,
           audit_status = 'approved'                     AS ok_audit,
           title IS NOT NULL AND btrim(title) <> ''       AS ok_title,
           walmart_pt IS NOT NULL AND walmart_pt <> 'unknown' AS ok_pt
    FROM catalog.products WHERE marketplace = 'US'
)
SELECT count(*)                                                  AS total,
       count(*) FILTER (WHERE ok_audit)                          AS approved,
       count(*) FILTER (WHERE ok_audit AND ok_title)              AS with_title,
       count(*) FILTER (WHERE ok_audit AND ok_title AND ok_pt)    AS with_pt,
       count(*) FILTER (WHERE ok_audit AND ok_title AND ok_pt
                          AND pt_source = 'walmart_confirmed')    AS pt_evid,
       count(*) FILTER (WHERE ok_audit AND ok_title AND ok_pt
                          AND EXISTS (SELECT 1 FROM catalog.risk_product_types r
                                       WHERE r.product_type = p.walmart_pt
                                         AND btrim(coalesce(r.category, '')) <> ''))
                                                                 AS with_cat,
       count(*) FILTER (WHERE ok_audit AND brand IS NOT NULL
                          AND btrim(brand) <> '')                AS with_brand
FROM p
"""

# ── P2 打分信号探针:契约 v1 字段表没有 rating/review_count,设计稿断言
#    "随 raw 落进来"——本仓零证据,必须实测。取样而非全表:上亿行 jsonb
#    全扫没必要,有没有这回事看 5 万条最新快照就够了
_SQL_SIGNAL = """
SELECT count(*)                                            AS n,
       count(*) FILTER (WHERE raw ? 'rating')              AS n_rating,
       count(*) FILTER (WHERE raw ? 'review_count')        AS n_review,
       count(*) FILTER (WHERE raw ? 'is_fba')              AS n_fba
FROM (SELECT raw FROM catalog.snapshots
      WHERE outcome = 'ok' ORDER BY scraped_at DESC LIMIT 50000) t
"""

# ⚠ audit.walmart_pt_meta 的主键列叫 **walmart_product_type**,不是
#   product_type(refdata/schema.sql:1132;全仓另 6 处消费方同款)。
#   2026-08-15 审查抓到:写成 m.product_type 会 UndefinedColumn 崩掉整份报告
_SQL_PT_DICT = """
SELECT (SELECT count(*) FROM catalog.risk_product_types)               AS n_risk,
       (SELECT count(*) FROM catalog.risk_product_types
         WHERE category IS NOT NULL AND btrim(category) <> '')         AS n_risk_cat,
       (SELECT count(*) FROM audit.walmart_pt_meta)                    AS n_meta,
       (SELECT count(*) FROM catalog.risk_product_types r
         WHERE NOT EXISTS (SELECT 1 FROM audit.walmart_pt_meta m
                            WHERE m.walmart_product_type = r.product_type)) AS only_risk,
       (SELECT count(*) FROM audit.walmart_pt_meta m
         WHERE NOT EXISTS (SELECT 1 FROM catalog.risk_product_types r
                            WHERE r.product_type = m.walmart_product_type)) AS only_meta,
       (SELECT count(*) FROM catalog.risk_product_types r
          JOIN audit.walmart_pt_meta m ON m.walmart_product_type = r.product_type
         WHERE btrim(coalesce(r.category, '')) <>
               btrim(coalesce(m.walmart_category, '')))                AS cat_diff
"""

_SQL_CATEGORIES = """
SELECT btrim(category) AS cat, count(*) AS n
FROM catalog.risk_product_types
WHERE category IS NOT NULL AND btrim(category) <> ''
GROUP BY 1 ORDER BY 2 DESC, 1
"""

# ORDER BY 固定:同一份数据两次跑要出同一份清单(样例截断才有意义)
_SQL_ONLINE = """
SELECT store, sku, product_type, published_status
FROM catalog.walmart_items WHERE missing_since IS NULL
ORDER BY store, sku
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

# 店铺状态:全仓统一写法(每店最新一行)
_SQL_STATUS = """
SELECT DISTINCT ON (store) store, store_status
FROM ops.store_kpi_daily ORDER BY store, data_date DESC
"""

# 冲突处置要的销量:按 (store, sku) 聚合——**不需要 asin 列**,
# order_lines 与 walmart_items 共用 (store, sku) 主键口径。
# 只算"有效销售"类行:统计状态在 raw 里(历史导入),API 行没有该键,
# 用 coalesce 让两种来源都算进来(API 行本就是真实销售)。
_SQL_SALES = """
SELECT store, sku,
       count(*)                                   AS orders,
       coalesce(sum(product_amount), 0)::numeric  AS gmv
FROM orders.order_lines
WHERE order_date >= now() - make_interval(days => %s)
  AND coalesce(raw ->> '统计状态', '有效销售') = '有效销售'
GROUP BY store, sku
"""


# ── 纯函数(逻辑都在这里,好测)────────────────────────────────────────────

def enrich(items, meta, pt2cat):
    """输入:在线行 [(store, sku, product_type, published_status)] +
    {asin: 元数据} + {PT: 大类} → 输出:(富化行 list, 统计 Counter)。

    每行补:asin(提不出为 None)、品牌占用键、大类、大类来源、渠道、是否已发布。
    大类主路取在线 PT(沃尔玛认过的),兜底取产品审核 PT——两条来源分开计数,
    因为兜底那部分可能是 LLM 推断的(pt_source),开新类目时不能当实证用。
    """
    rows, st = [], Counter()
    for it in items:
        store, sku, pt, published = (list(it) + [None] * 4)[:4]
        st["online"] += 1
        asin = sku_asin.extract_asin(sku)
        if asin is None:
            st["no_asin"] += 1
            st[f"form_{sku_asin.classify(sku)}"] += 1
        m = meta.get(asin) if asin else None
        if asin and m is None:
            st["asin_not_in_products"] += 1
        item_pt = (pt or "").strip()
        prod_pt = ((m or {}).get("walmart_pt") or "").strip()
        cat, src = pt2cat.get(item_pt), "item"
        if not cat:
            cat, src = pt2cat.get(prod_pt), "product"
        if not cat:
            src = None
            st["no_category"] += 1
        else:
            st[f"cat_from_{src}"] += 1
        key = bk.brand_key((m or {}).get("brand"),
                           (m or {}).get("manufacturer")) if m else None
        if m and key is None:
            st["no_brand"] += 1
        ch = ((m or {}).get("fulfillment") or "").strip().upper() or None
        if ch and ch not in store_targets.CHANNELS:
            st["channel_weird"] += 1
        rows.append({"store": store, "sku": sku, "asin": asin,
                     "brand_key": key, "category": cat, "cat_source": src,
                     "channel": ch, "pt": item_pt or prod_pt or None,
                     "pt_source": (m or {}).get("pt_source"),
                     "published": (published or "").upper() == "PUBLISHED"})
    return rows, st


def cross_store(rows, field):
    """输入:富化行 + 键名('asin'/'brand_key')→ 输出:跨店冲突
    [(键, {店: 件数})],按 涉及店铺数 → 总件数 → 键名 三级降序(键名升序)。

    三级排序是为了**可复现**:样例只打印前 N 条,排序不稳定时两次跑给所有者
    看的是不同的冲突。只看在线行——占用台账还不存在,存量冲突只能从观测看出来。
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
    """输入:富化行 → 输出:{店: {n, published, categories, channels, cat_src}}。"""
    prof: dict[str, dict] = {}
    for r in rows:
        p = prof.setdefault(r["store"], {
            "n": 0, "published": 0, "categories": Counter(),
            "channels": Counter(), "cat_src": Counter()})
        p["n"] += 1
        p["published"] += 1 if r["published"] else 0
        p["categories"][r["category"] or UNCLASSIFIED] += 1
        p["channels"][r["channel"] or UNKNOWN_CHANNEL] += 1
        p["cat_src"][r["cat_source"] or "none"] += 1
    return prof


def real_cats(p) -> list:
    """输入:店铺画像 → 输出:真实大类名列表(剔除未归类占位)。

    筛选/排序/展示三处共用同一个定义——曾经三处各写一遍表达式,
    排序把"(未归类)"也数进去,截断后最碎的店反而被挤出样例。
    """
    return [c for c in p["categories"] if c != UNCLASSIFIED]


def channel_mismatch(prof, cfg):
    """输入:店铺画像 + 限额表配置 → 输出:[(店, 限制渠道, 不符件数, 分布)]。

    只对**填了配送限制**的店对拍;**白名单判定**:只有渠道确实是另一个已知
    值(FBA↔FBM)才算不符。采集没采到、或采出第三种值,都不算不符——
    把"没采到"算成"货不对"会让无辜商品进下架清单;第三种值恒高说明采集侧
    is_fba 解析坏了,那是要修采集,不是要下架商品(该计数由调用方单列)。
    """
    out = []
    for store, p in prof.items():
        want = (cfg.get(store) or {}).get("channel")
        if not want:
            continue
        bad = sum(n for ch, n in p["channels"].items()
                  if ch in store_targets.CHANNELS and ch != want)
        if bad:
            out.append((store, want, bad, dict(p["channels"])))
    out.sort(key=lambda x: (-x[2], x[0]))
    return out


def _fmt_counter(c: Counter, top=4) -> str:
    return ", ".join(f"{k}×{n}" for k, n in c.most_common(top))


def suggest_categories(prof, cfg, top=2):
    """输入:店铺画像 + 配置 → 输出:[(店, 建议类目列表, 在线件数分布, 已填值)]。

    所有者口径(2026-08-15):**超 2 类目的店保留在线数量最多的两类**。
    本函数只出建议,不写任何地方——所有者填进飞书「类目1/2/3」三列,
    那三列才是准入权威(§三.1a)。已填的店也出一行,便于对拍"填的 vs
    实际最多的"是否一致。
    """
    out = []
    for store, p in sorted(prof.items()):
        ranked = [(c, n) for c, n in p["categories"].most_common()
                  if c != UNCLASSIFIED]
        if not ranked:
            continue
        out.append((store, [c for c, _ in ranked[:top]], ranked,
                    list((cfg.get(store) or {}).get("categories") or [])))
    return out


def channel_offenders(rows, cfg):
    """输入:富化行 + 配置 → 输出:不符渠道的**逐行**清单(给人去下架)。

    与 `channel_mismatch` 的计数同口径(白名单:只有确实是另一个已知渠道
    才算不符),但输出到 SKU 级——"存在不符 9 件"没法照着做,一行行的
    店铺/SKU/ASIN/渠道才行。只列已发布行:未发布的下架没有意义。
    """
    out = []
    for r in rows:
        if not r["published"]:
            continue
        want = (cfg.get(r["store"]) or {}).get("channel")
        ch = r["channel"]
        if want and ch in store_targets.CHANNELS and ch != want:
            out.append(r)
    out.sort(key=lambda r: (r["store"], r["sku"]))
    return out


def resolve_conflicts(rows, sales, field):
    """输入:富化行 + {(店,sku): (单量, 金额)} + 冲突键名
    → 输出:[(键, 保留店, 保留店销量, [(店, sku, asin, 销量, 金额, 判定)])]。

    所有者口径(2026-08-15):**同品牌/同 ASIN 跨店的,留销量大的店**。
    销量按该店该键名下**全部在线 SKU 的订单金额**合计;
    打平顺序:金额 → 单量 → 在线件数 → 店名(全打平也要给确定答案,
    否则同一份数据两次跑给出两份不同的下架清单)。
    **零销量组单列标记**:两边都没卖过时"留销量大的"无从判起,
    这类必须让人看见,不能悄悄按店名字典序决定谁生谁死。
    """
    groups: dict = defaultdict(list)
    for r in rows:
        v = r.get(field)
        if v:
            groups[v].append(r)
    out = []
    for key, items in groups.items():
        by_store: dict[str, dict] = {}
        for r in items:
            o, g = sales.get((r["store"], r["sku"]), (0, 0.0))
            st = by_store.setdefault(r["store"], {"orders": 0, "gmv": 0.0,
                                                  "items": 0})
            st["orders"] += o
            st["gmv"] += float(g)
            st["items"] += 1
        if len(by_store) < 2:
            continue
        ranked = sorted(by_store.items(),
                        key=lambda kv: (-kv[1]["gmv"], -kv[1]["orders"],
                                        -kv[1]["items"], kv[0]))
        keep = ranked[0][0]
        tie = all(v["gmv"] == 0 and v["orders"] == 0 for _, v in ranked)
        detail = []
        for r in sorted(items, key=lambda r: (r["store"], r["sku"])):
            s = by_store[r["store"]]
            detail.append((r["store"], r["sku"], r["asin"] or "",
                           s["orders"], round(s["gmv"], 2),
                           "保留" if r["store"] == keep else "下架"))
        out.append((key, keep, ranked[0][1], detail, tie))
    # 涉及店铺数多、销量大的排前面:人先处理影响面大的
    out.sort(key=lambda x: (-len(x[3]), -x[2]["gmv"], str(x[0])))
    return out


def _write_csv(name: str, header: list, rows: list) -> str:
    """输入:文件名 + 表头 + 行 → 输出:落盘路径(报告目录,每次覆盖)。"""
    paths.reports_dir().mkdir(parents=True, exist_ok=True)
    p = paths.reports_dir() / name
    with p.open("w", newline="", encoding="utf-8-sig") as fh:   # BOM:Excel 直开不乱码
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return str(p)


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


def _row(cur, sql) -> dict:
    cur.execute(sql)
    return dict(zip([d[0] for d in cur.description], cur.fetchone()))


def run(params: dict) -> str:
    """输入:params(sample/channel)→ 输出:探针 + 存量审计报告。"""
    sample = int(params.get("sample", 10))
    with_channel = str(params.get("channel", "1")).lower() not in {"0", "false", "no"}
    sales_days = int(params.get("sales_days", 365))
    export = str(params.get("export", "1")).lower() not in {"0", "false", "no"}
    L: list[str] = []

    with db.pg_conn() as conn, conn.cursor() as cur:
        pool = _row(cur, _SQL_POOL)
        sig = _row(cur, _SQL_SIGNAL)
        # P3 是对拍探针不是主线:字典表出问题不该拖垮整份存量审计
        try:
            dic = _row(cur, _SQL_PT_DICT)
        except Exception as e:                  # noqa: BLE001 降级并明说
            conn.rollback()                     # 事务已 aborted,后续查询要先回滚
            dic, dic_err = None, str(e).strip().splitlines()[0]
        else:
            dic_err = None
        cur.execute(_SQL_CATEGORIES)
        cats = cur.fetchall()
        cur.execute(_SQL_PT2CAT)
        pt2cat = {pt: c for pt, c in cur.fetchall() if c}
        cur.execute(_SQL_ONLINE)
        items = cur.fetchall()
        cur.execute(_SQL_STATUS)
        status = {s: (st or "").strip().upper() for s, st in cur.fetchall()}
        cur.execute(_SQL_SALES, (sales_days,))
        sales = {(s, k): (int(o), float(g)) for s, k, o, g in cur.fetchall()}

        asins = sorted({a for a in (sku_asin.extract_asin(it[1])
                                    for it in items) if a})
        meta = _fetch_meta(cur, asins, with_channel)

    rows, st = enrich(items, meta, pt2cat)
    prof_all = store_profiles(rows)          # 全量(含不在册店):A4/A7 点名用

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
    prof = store_profiles(live_rows)
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
             + ("(形态:" + _fmt_counter(Counter(
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

    a1 = cross_store(live_rows, "asin")
    L.append(f"A1 同 ASIN 跨店在线:{len(a1)} 个 ASIN"
             + (";" + "; ".join(f"{a}→{_fmt_counter(Counter(d))}"
                                for a, d in a1[:sample]) if a1 else "(无)"))
    a2 = cross_store(live_rows, "brand_key")
    L.append(f"A2 同品牌跨店在线:{len(a2)} 个品牌"
             + (";" + "; ".join(f"{b}→{len(d)}店/{sum(d.values())}件"
                                for b, d in a2[:sample]) if a2 else "(无)"))

    over = sorted(((s, p) for s, p in prof.items() if len(real_cats(p)) > 2),
                  key=lambda x: (-len(real_cats(x[1])), -x[1]["n"], x[0]))
    L.append(f"A3 每店大类:在册且有在线行的 {len(prof)} 家;超 2 大类的 {len(over)} 家"
             + (";" + "; ".join(f"{s}({len(real_cats(p))}类:"
                                f"{_fmt_counter(p['categories'], 3)})"
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
            mism = channel_mismatch(store_profiles(pub_rows), cfg)
            L.append(f"A5 渠道对拍(已发布行口径:未发布的下架无意义):"
                     f"填了配送限制的店 {n_cfg_ch} 家;存在不符商品的 {len(mism)} 家"
                     + (";" + "; ".join(
                         f"{s}(限{w},不符{n}件:{_fmt_counter(Counter(d))})"
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
        sug = suggest_categories(prof, cfg)
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
        off = channel_offenders(live_rows, cfg)
        p2 = _write_csv("alloc_渠道不符下架清单.csv",
                        ["店铺", "限定渠道", "SKU", "ASIN", "实际渠道",
                         "大类", "PT"],
                        [(r["store"], cfg[r["store"]]["channel"], r["sku"],
                          r["asin"] or "", r["channel"], r["category"] or "",
                          r["pt"] or "") for r in off])
        L.append(f"C2 渠道不符 {len(off)} 件(已发布行)→ {p2}"
                 f"——只列确实是另一个已知渠道的;N/A 与未采到不进清单")

    # C3/C4 冲突处置:留销量大的店(所有者口径 2026-08-15)
    for tag, field, fname in (("C3 同 ASIN 跨店", "asin", "alloc_同ASIN冲突处置.csv"),
                              ("C4 同品牌跨店", "brand_key", "alloc_同品牌冲突处置.csv")):
        res = resolve_conflicts(live_rows, sales, field)
        rows_x = [(key, keep, "是" if tie else "",
                   st, sku, asin, o, g, verdict)
                  for key, keep, _, detail, tie in res
                  for st, sku, asin, o, g, verdict in detail]
        p = _write_csv(fname, ["冲突键", "保留店", "零销量打平", "店铺", "SKU",
                               "ASIN", f"近{sales_days}天单量", "销售额", "处置"],
                       rows_x)
        ties = sum(1 for r in res if r[4])
        L.append(f"{tag}:{len(res)} 组、{len(rows_x)} 行 → {p};"
                 f"其中**两边都零销量、按在线件数/店名打平的 {ties} 组**"
                 f"(这些要人眼看一下,机器判不出谁该留)")

    L.append("→ 下一步:①按 C1 填飞书类目三列(填了才限制,空=不限制);"
             "②按 C2 自行下架渠道不符商品;③C3/C4 确认后进 A1 回填 claims")
    return "\n".join(L)
