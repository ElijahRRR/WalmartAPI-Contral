"""分配存量勘察积木:在线商品富化、跨店冲突判定、店铺画像、处置清单素材。

为什么是 services 而不是留在 workflow 里:`alloc_audit`(出报告)与
`alloc_backfill`(落占用)必须用**同一套判定**——报告说"留 A085"、回填却
落到别家,那份给人看的清单就成了假的。而工作流之间不准互相 import(铁律 1),
所以共用的口径只能沉到这里,两边各自 import。

本模块只读、无副作用:SQL 常量 + 取数函数 + 纯函数判定。
落盘、拼报告、写台账分别在两个 workflow 里。
"""

import logging
from collections import Counter, defaultdict

from registry import resources
from services import brand_key as bk
from services import sku_asin, store_targets

logger = logging.getLogger("services.alloc_survey")


def is_excluded(store: str) -> bool:
    """输入:店铺名 → 输出:是否**不纳入分配规划**(registry 的子串名单)。

    排除的是"归属"不是"数据":这些店不判类目、不占品牌与产品、其在线商品
    不拦别人上架;但它们的订单销量照常进全局维度,给别的店当选品信号。
    """
    name = str(store or "")
    return any(p in name for p in resources.alloc_excluded_stores())


def claimable(rows, registered) -> list[dict]:
    """输入:富化行 + 在册店名集合 → 输出:**占得住货位的行**。

    冲突判定的唯一入行口径。`alloc_audit`(出清单)与 `alloc_backfill`
    (落占用)都必须走这里 —— 两边各写各的筛法,报告说"留 A085"、回填却
    落到别家,那份给人照着做的清单就是假的(本模块存在的理由)。

    三条筛法:
      · **在册**:不在册店的行是冻结快照(catalog_sync 早不扫它了),
        混进来会让所有者为一家不存在的店去下架另一家店真在卖的 listing;
      · **规划内**:范围外的店(店名含「谭总」等)不占任何品牌与产品;
      · **已发布**:未发布的不算真占着货位。

    ⚠ 第三条曾经只写在回填侧,报告侧漏了(2026-08-15 实证:同一个品牌
    报告判"留 B"、回填落"留 A"——未发布行进了报告的「在线件数」那一级)。
    真打平组靠件数定序,多算几条未发布的就能把冠亚军换个位置。
    """
    return [r for r in rows
            if r["store"] in registered and r["published"]
            and not is_excluded(r["store"])]

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
#
# 口径两条(2026-08-15 按 order_history_import(#35)的落库事实校准):
# 1. **只排 Cancelled**。API 行的 sale_status 是真实状态,历史导入行一律
#    'Delivered'(该工作流只取销售六列)。曾按 `raw->>'统计状态'` 过滤,
#    但历史行**根本没有 raw**——那个条件对它们恒真,等于没写。
# 2. ⚠ **历史行里的退款单算进了销量**:源表的「统计状态」列没有被导入,
#    退款/无效行与有效销售一起进来了。对"留销量大的店"这类相对比较影响
#    很小(各店同样口径),但绝对额会偏高;真要精确得让导入侧补这一列。
_SQL_SALES = """
SELECT store, sku,
       count(*)                                   AS orders,
       coalesce(sum(product_amount), 0)::numeric  AS gmv
FROM orders.order_lines
WHERE order_date >= %(as_of)s::timestamptz - make_interval(days => %(days)s)
  AND order_date <  %(as_of)s::timestamptz
  AND coalesce(sale_status, '') <> 'Cancelled'
GROUP BY store, sku
"""

# 订单侧店名 vs 凭证表:对不上的店,其销量进不了"店×类目"维度
# (只进产品/品牌/类目三个全局维度)。15 万历史行来自 494 家店,
# 与现凭证表 505 家不是同一套命名的话,店铺适配分会凭空少掉一截。
_SQL_ORDER_STORES = """
SELECT store, count(*) AS n,
       count(*) FILTER (WHERE source IS NOT NULL) AS hist
FROM orders.order_lines
WHERE order_date >= %(as_of)s::timestamptz - make_interval(days => %(days)s)
  AND order_date <  %(as_of)s::timestamptz
GROUP BY store ORDER BY n DESC
"""


def sales_window(as_of: str = "", days: int = 365) -> dict:
    """输入:可选 as_of(YYYY-MM-DD)+ 窗口天数 → 输出:两条 SQL 的参数字典。

    **窗口右端默认取「今天 UTC 零点」而不是 now()**,于是同一天内跑多少次、
    先跑 alloc_audit 再跑 alloc_backfill,吃到的销量**逐行一致**。
    用 now() 时两次调用差几秒就可能差几张单,而阶梯的前三级全看销量——
    报告说"留 A"、几分钟后回填落"留 B",两边都没错,只是各自看了不同的世界。

    要复现某一天出的那份清单(比如清单跑完隔了几天才回填),两个工作流
    传同一个 `-p as_of=YYYY-MM-DD` 即可;报告会把该重跑什么原样打出来。
    """
    from datetime import datetime, timezone
    day = (as_of or "").strip() or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    datetime.strptime(day, "%Y-%m-%d")      # 格式不对就当场报错,别悄悄退回 now()
    return {"as_of": f"{day} 00:00:00+00", "days": days, "day": day}


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


def store_metrics(rows, sales):
    """输入:富化行 + {(店,sku): (单量, 金额)} → 输出:
    ({店: {gmv, orders, online}}, {(店, 大类): 金额})。

    冲突判定的**降级阶梯**要用:绝大多数冲突组的两家店在该商品上都零销量
    (实测 687 组同 ASIN 里 660 组、1657 组同品牌里 1432 组),只看该商品
    销量等于判不了——但"这家店整体卖得怎么样""这家店在这个大类卖得怎么样"
    是有数的,拿它们当代理指标,决定就有依据且能解释。

    ⚠ **整体销量按订单表全量算,不按"当前还在线的 SKU"算**(2026-08-15 修)。
    原来是遍历 rows 累加,于是有一条反馈回路:所有者照本报告的建议下架了
    一批商品 → 那批商品的历史销量跟着退出统计 → 这家店的"整体销量"下降 →
    **下一轮别的冲突组判定跟着翻**。照建议做事反而惩罚自己,而且第二天重跑
    结果对不上,没人看得出为什么。销量是既成事实,不该随货架变。
    (`per_cat` 仍按在线行推大类——订单行还没有 asin/大类列,A1.5 补上之后
     这一层也应改成订单侧口径。)
    """
    per_store: dict[str, dict] = {}
    per_cat: dict[tuple, float] = {}
    for (store, _sku), (o, g) in sales.items():
        st = per_store.setdefault(store, {"gmv": 0.0, "orders": 0, "online": 0})
        st["gmv"] += float(g)
        st["orders"] += int(o)
    for r in rows:
        st = per_store.setdefault(r["store"], {"gmv": 0.0, "orders": 0, "online": 0})
        st["online"] += 1                      # 件数只数行,金额上面已按订单表算完
        if r.get("category"):
            _o, g = sales.get((r["store"], r["sku"]), (0, 0.0))
            key = (r["store"], r["category"])
            per_cat[key] = per_cat.get(key, 0.0) + float(g)
    return per_store, per_cat


#: 判定阶梯:上一级分不出来才看下一级。名字会写进 csv,让人知道这条是怎么定的
LADDER = ("按该商品销量", "按该店该大类销量", "按该店整体销量", "按在线件数", "按店名")
#: 类目准入是**硬闸不是阶梯**:店不准入这个大类,货就必须离开它,与销量无关
BY_CATEGORY = "按类目准入"
#: 机器判不出、必须人眼看的依据级别(所有者定稿 2026-08-15 晚)。
#: **「按在线件数」不在其中**——在线数是库里查得到的客观数据,"这个品牌你在
#: 哪家店铺得更开"是站得住的经营判断,不需要人工复核。只有落到**店名**才是
#: 真打平:那一级等于拿字典序当经营决策,机器确实判不出。
NEEDS_HUMAN = (LADDER[4],)


def resolve_conflicts(rows, sales, field, metrics=None, cfg=None):
    """输入:富化行 + {(店,sku): (单量, 金额)} + 冲突键名(+ store_metrics 产物)
    → 输出:[(键, 保留店, 保留店统计, 明细行, 判定依据)]。

    所有者口径(2026-08-15):**同品牌/同 ASIN 跨店的,留销量大的店**。
    但实测 96% 的同 ASIN 组、86% 的同品牌组**两边都零销量**——只看该商品
    销量就得把两千多组丢给人工。所以按 LADDER 逐级降级:

      ① 该商品在该店的销量 → ② 该店该大类的销量 → ③ 该店整体销量
      → ④ 在线件数 → ⑤ 店名

    ②③ 是代理指标:同样没卖过的两家店,把货留给"这个大类卖得更好"或
    "整体卖得更好"的那家,是可解释的经营判断。判定依据写进每一行,
    人一眼能看出这条是靠什么定的、要不要推翻。
    **只有连店名都要用上(④⑤)才算真打平**——那才是机器确实判不出的。
    """
    per_store, per_cat = metrics if metrics else ({}, {})
    groups: dict = defaultdict(list)
    for r in rows:
        v = r.get(field)
        if v:
            groups[v].append(r)
    out = []
    for key, items in groups.items():
        # ── 硬闸先于阶梯:类目定了之后,不准入该大类的店直接出局 ──
        # 所有者的处理顺序(2026-08-15):先判每店类目,再判品牌与冲突。
        # 顺序是有意义的——某店不再做这个大类,那批货就必须离开它,
        # 不是"销量谁大谁留"能决定的事。
        admitted = items
        gated = False
        if cfg:
            keep_rows = [r for r in items
                         if store_targets.allowed(cfg.get(r["store"]),
                                                  r.get("category"))]
            # 全被类目闸挡下时不作数(那说明这批货哪家都不该有,交给人看),
            # 只剩一家或多家时才用它收窄
            if keep_rows and len({r["store"] for r in keep_rows}) < len(
                    {r["store"] for r in items}):
                admitted, gated = keep_rows, True
        if len({r["store"] for r in admitted}) < 2:
            if gated:      # 类目闸一刀定音:留下的那家就是答案
                keep = admitted[0]["store"]
                detail = [(r["store"], r["sku"], r["asin"] or "", 0, 0.0, 0.0, 0.0,
                           "保留" if r["store"] == keep else "下架")
                          for r in sorted(items, key=lambda r: (r["store"], r["sku"]))]
                out.append((key, keep, {"gmv": 0.0}, detail, BY_CATEGORY))
            continue
        items = admitted
        by_store: dict[str, dict] = {}
        for r in items:
            o, g = sales.get((r["store"], r["sku"]), (0, 0.0))
            st = by_store.setdefault(r["store"], {"orders": 0, "gmv": 0.0,
                                                  "items": 0, "cat_gmv": 0.0})
            st["orders"] += o
            st["gmv"] += float(g)
            st["items"] += 1
            if r.get("category"):
                st["cat_gmv"] = max(st["cat_gmv"],
                                    per_cat.get((r["store"], r["category"]), 0.0))
        if len(by_store) < 2:
            continue
        for name, st in by_store.items():
            st["store_gmv"] = (per_store.get(name) or {}).get("gmv", 0.0)
        ranked = sorted(
            by_store.items(),
            key=lambda kv: (-kv[1]["gmv"], -kv[1]["cat_gmv"], -kv[1]["store_gmv"],
                            -kv[1]["items"], kv[0]))
        keep = ranked[0][0]
        # 判定依据 = 第一个把冠亚军分开的那一级
        level = LADDER[-1]
        for i, f in enumerate(("gmv", "cat_gmv", "store_gmv", "items")):
            if len(ranked) > 1 and ranked[0][1][f] != ranked[1][1][f]:
                level = LADDER[i]
                break
        detail = []
        for r in sorted(items, key=lambda r: (r["store"], r["sku"])):
            s = by_store[r["store"]]
            detail.append((r["store"], r["sku"], r["asin"] or "",
                           s["orders"], round(s["gmv"], 2),
                           round(s["cat_gmv"], 2), round(s["store_gmv"], 2),
                           "保留" if r["store"] == keep else "下架"))
        out.append((key, keep, ranked[0][1], detail, level))
    # 涉及店铺数多、销量大的排前面:人先处理影响面大的
    out.sort(key=lambda x: (-len(x[3]), -x[2]["gmv"], str(x[0])))
    return out


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


