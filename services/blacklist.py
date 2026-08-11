"""黑名单收集积木(problem_product_cleanup 尾段用)——自产回路的落库端。

旧系统语义逐字保留(legacy_survey:1435 / blacklist_sync.py:18-21):

  ASIN 黑名单只收 **PERMANENT = {B,C,E,F,G,K}**(永久产品级禁止),
  明确排除 A/D/H/I/J/L/Z(可修复/临时/平台类)——进了会误杀重上架拦截。
  所有者 2026-08-11 再次拍板:飞书表来源列的 13 类词表只是**格式约定**,
  不是入选范围。

  入选按**当轮类别**判(cleanup 跑的就是今天的问题清单,当轮=最新)。
  历史数据实证类别翻动频繁(48.5 万行折叠出 23.9 万次变迁),
  「曾经命中过 B」不能作数——那会把短暂误判过的商品永久拉黑。

  品牌收集只看 C(品牌)/E(知产)两类;品牌名从 **catalog.products.brand**
  取(所有者定稿 2026-08-11:从采集库读,不再走旧系统的 DMIT 逐个采);
  **去重按品牌**,SKU 只是溯源列。已处理 ASIN 记 ops.dedupe
  ('cleanup:brand_asin',历史 2,609 个已导入)——品牌还没采到的 ASIN
  **不标已处理**,等产品中心补上品牌后自然重试。

  品牌落**两张表**(所有者厘清 2026-08-11):
    catalog.brand_err_hits   渠道表(beyKyi 投影源):完整记录沃尔玛后台
                             拿到过哪些品牌,渠道内按品牌去重,**不与总
                             清单去重**——挤在 brand_blacklist 里按品牌
                             冲突,总表已有的品牌就永远进不了渠道(第一版
                             的建模缺陷,当天修正)。brand_new/known 按
                             渠道表算。
    catalog.brand_blacklist  总清单镜像 + 否决闸:自产品牌 DO NOTHING
                             补进闸门,立刻挡住重上架;总表真值不覆盖。

  BIZ-CN 独立成维度(legacy_survey:2077:唯一明确标注中国卖家专属禁售的
  错误码,不能被 C 品牌类的关键词匹配吸收)——两张表都带 biz_cn 布尔列。

写入方向:cleanup → PG(本文件)→ 飞书投影(blacklist_push 工作流按
pushed_at 水位推)。PG 权威,飞书只是人机界面。
"""

import json
import logging

from services.sku_asin import extract_asin

logger = logging.getLogger("services.blacklist")

# 永久产品级禁止(入选集合);排除理由见模块头。改这个集合 = 改业务口径,
# 必须先过所有者。
PERMANENT = frozenset({"B", "C", "E", "F", "G", "K"})

# 品牌收集的触发类别:品牌限制 / 知产
BRAND_CATEGORIES = frozenset({"C", "E"})

BRAND_ASIN_SCOPE = "cleanup:brand_asin"     # ops.dedupe:已做过品牌收集的 ASIN

_NAMES = {"B": "禁售", "C": "品牌", "E": "知产",
          "F": "限类", "G": "药品", "K": "审查"}


def is_biz_cn(reason_text) -> bool:
    """输入:报错原文 → 输出:是否 BIZ-CN(中国卖家专属禁售)。"""
    t = (reason_text or "").lower()
    return "biz-cn" in t or "reference code biz" in t


def source_label(code: str) -> str:
    """输入:类别码 → 输出:飞书来源列格式「沃尔玛-〈类名〉」。"""
    return f"沃尔玛-{_NAMES.get(code, code)}"


_ASIN_SQL = """
INSERT INTO catalog.asin_blacklist
    (asin, category, source, reason, src_store, biz_cn, src_sku)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (asin) DO NOTHING
"""


def record_asins(conn, items: list[dict]) -> int:
    """输入:连接 + 当轮已归类 item(store/sku/category/reasons)
    → 输出:新入选数。永久禁止 = 一次入选,已在名单的不更新(DO NOTHING)。

    黑名单键 = 清洗后的标准 asin(sku_asin 规则;提不出用订货号原文兜底,
    宁可键不标准也不丢行),订货号原文存 src_sku 溯源。"""
    added = 0
    with conn.cursor() as cur:
        for it in items:
            code = it.get("category")
            if code not in PERMANENT:
                continue
            asin = extract_asin(it["sku"]) or it["sku"]
            cur.execute(_ASIN_SQL, (
                asin, code, source_label(code),
                (it.get("reasons") or "")[:200] or None,
                it.get("store"), is_biz_cn(it.get("reasons")), it["sku"]))
            added += cur.rowcount or 0
    return added


_PROCESSED_SQL = "SELECT key FROM ops.dedupe WHERE scope = %s AND key = ANY(%s)"

_BRAND_OF_SQL = """
SELECT asin, brand FROM catalog.products
WHERE marketplace = 'US' AND asin = ANY(%s)
  AND coalesce(btrim(brand), '') <> ''
"""

_CHANNEL_SQL = """
INSERT INTO catalog.brand_err_hits
    (brand_key, brand, source, added_date, src_sku, src_store, biz_cn)
VALUES (%s, %s, %s, CURRENT_DATE::text, %s, %s, %s)
ON CONFLICT (brand_key) DO NOTHING
"""

_GATE_SQL = """
INSERT INTO catalog.brand_blacklist (brand_key, brand, source, added_date)
VALUES (%s, %s, %s, CURRENT_DATE::text)
ON CONFLICT (brand_key) DO NOTHING
"""

_MARK_SQL = """
INSERT INTO ops.dedupe (scope, key, meta)
VALUES (%s, %s, %s::jsonb) ON CONFLICT DO NOTHING
"""


def collect_brands(conn, items: list[dict]) -> dict:
    """输入:连接 + 当轮已归类 item → 输出:统计 dict。

    C/E 类 → 查品牌 → 入渠道表 brand_err_hits(brand_new/known 按渠道算)
    + DO NOTHING 补进否决闸 brand_blacklist(总表真值不覆盖)→ 标 ASIN
    已处理。**品牌缺失的 ASIN 不标已处理**:产品中心还没这行或 brand 为空,
    等 product_ingest 补上后下一轮自然重试,标了就永远漏了。
    """
    stats = {"brand_new": 0, "brand_known": 0, "no_brand": 0, "skipped": 0}
    cands = {it["sku"]: it for it in items
             if it.get("category") in BRAND_CATEGORIES}
    if not cands:
        return stats
    with conn.cursor() as cur:
        cur.execute(_PROCESSED_SQL, (BRAND_ASIN_SCOPE, list(cands)))
        done = {r[0] for r in cur.fetchall()}
        stats["skipped"] = len(done)
        todo = {a: it for a, it in cands.items() if a not in done}
        if not todo:
            return stats
        # 采集库按**清洗后的标准 asin** 查(订货号原文直接查必然全空——
        # 2026-08-11 生产实证:2,702 个 C/E 品牌 0 命中,教训在此)
        asin_of = {sku: (extract_asin(sku) or sku) for sku in todo}
        cur.execute(_BRAND_OF_SQL, (list(set(asin_of.values())),))
        brand_of = {a: b for a, b in cur.fetchall()}
        for sku, it in todo.items():
            brand = (brand_of.get(asin_of[sku]) or "").strip()
            if not brand:
                stats["no_brand"] += 1      # 不标已处理:等品牌到位重试
                continue
            label = source_label(it["category"])
            cur.execute(_CHANNEL_SQL, (
                brand.casefold(), brand, label,
                sku, it.get("store"), is_biz_cn(it.get("reasons"))))
            if cur.rowcount:
                stats["brand_new"] += 1
            else:
                stats["brand_known"] += 1
            cur.execute(_GATE_SQL, (brand.casefold(), brand, label))
            cur.execute(_MARK_SQL, (BRAND_ASIN_SCOPE, sku,
                                    json.dumps({"brand": brand.casefold()},
                                               ensure_ascii=False)))
    return stats


# ── 历史回填(blacklist_push -p backfill=1 / rebuild_brand=1 用,一次性)────────
#
# 从 product_events 的归类时间线按「每个 ASIN 的**最新**类别」推导入选——
# 与实时链路同一条原则(最新类别命中才算,"曾命中过"不作数)。跨店取全局
# 最新:同一 ASIN 在 A 店旧类别 B、B 店新类别 A(过期)⇒ 最新是可修复类,
# 不入选。来源标签的 CASE 必须与 source_label 同表(有测试钉住,别漂)。

# 身份 = coalesce(asin, sku):清洗出的标准码优先,提不出用订货号原文兜底。
# 多个订货号(不同店同一产品)归并到同一 asin,最新类别看**产品级**全局最新。
_LATEST_CTE = """
WITH latest AS (
    SELECT DISTINCT ON (coalesce(asin, sku)) coalesce(asin, sku) AS asin,
           sku, store, occurred_at,
           detail->>'category' AS cat, detail->>'reason' AS reason
    FROM catalog.product_events
    WHERE event = 'problem_categorized'
    ORDER BY coalesce(asin, sku), occurred_at DESC)
"""

_BACKFILL_COUNT_SQL = _LATEST_CTE + """
SELECT count(*) FILTER (WHERE cat = ANY(%(perm)s)) AS permanent,
       count(*) FILTER (WHERE cat = ANY(%(brandcats)s)) AS brand_cand,
       count(*) AS total
FROM latest
"""

_BACKFILL_ASIN_SQL = _LATEST_CTE + """
INSERT INTO catalog.asin_blacklist
    (asin, category, source, reason, src_store, biz_cn, src_sku, created_at)
SELECT asin, cat,
       '沃尔玛-' || CASE cat WHEN 'B' THEN '禁售' WHEN 'C' THEN '品牌'
                             WHEN 'E' THEN '知产' WHEN 'F' THEN '限类'
                             WHEN 'G' THEN '药品' WHEN 'K' THEN '审查' END,
       left(reason, 200), store,
       (lower(coalesce(reason, '')) LIKE '%%biz-cn%%'
        OR lower(coalesce(reason, '')) LIKE '%%reference code biz%%'),
       sku, occurred_at
FROM latest WHERE cat = ANY(%(perm)s)
ON CONFLICT (asin) DO NOTHING
"""

# ASIN 黑名单重建(rebuild_asin):SKU 清洗后表内键还是订货号原文/多店重复,
# 擦净按标准 asin 重灌(created_at=报错发生时刻,表格日期列因此有意义)。
_ASIN_WIPE_SQL = "DELETE FROM catalog.asin_blacklist"

def backfill_counts(conn) -> dict:
    """输入:连接 → 输出:回填预览计数(不写任何东西)。"""
    with conn.cursor() as cur:
        cur.execute(_BACKFILL_COUNT_SQL,
                    {"perm": sorted(PERMANENT), "brandcats": sorted(BRAND_CATEGORIES)})
        permanent, brand_cand, total = cur.fetchone()
    return {"permanent": permanent, "brand_cand": brand_cand, "total": total}


def backfill_from_events(conn) -> dict:
    """输入:连接 → 输出:ASIN 回填统计(集合级 INSERT,万级量逐行太慢)。
    品牌渠道的历史重建走 rebuild_brand_channel(单独命令,含清表重灌)。"""
    with conn.cursor() as cur:
        cur.execute(_BACKFILL_ASIN_SQL, {"perm": sorted(PERMANENT)})
        return {"asin_new": cur.rowcount or 0}


def rebuild_asin_blacklist(conn) -> dict:
    """输入:连接 → 输出:{wiped, inserted}。擦净按标准 asin 重灌
    (黑名单是时间线的投影,投影可以重投——与品牌渠道重建同一权衡)。"""
    with conn.cursor() as cur:
        cur.execute(_ASIN_WIPE_SQL)
        wiped = cur.rowcount or 0
        cur.execute(_BACKFILL_ASIN_SQL, {"perm": sorted(PERMANENT)})
        return {"wiped": wiped, "inserted": cur.rowcount or 0}


# ── 品牌渠道重建(blacklist_push -p rebuild_brand=1,一次性)───────────────────
#
# 两条腿,先认领后推导,同键 DO NOTHING(认领的历史日期优先):
#
# ① **从总表认领**:旧系统后台收集的品牌当年写进旧「禁止品牌收集」表,
#   来源列固定「沃尔玛-品牌限制/沃尔玛-侵权/沃尔玛后台」(legacy_survey:1360),
#   经所有者归拢进总表、risk_sync 镜像进 brand_blacklist——来源以「沃尔玛」
#   开头的镜像行**就是**历史沃尔玛渠道,零成本直接认领。不这么做的话只能
#   为 2,701 个历史 ASIN 补采(采集侧保留期早裁掉了,2026-08-11 实证
#   catalog.products 只命中 1 个)。
# ② **从时间线推导**:C/E 最新类 ASIN × catalog.products.brand,每品牌取
#   最早报错日当 added_date。绕过 ops.dedupe——那本账管"这个 ASIN 别重复
#   采集",不管"渠道该不该记账"。

_CHANNEL_WIPE_SQL = "DELETE FROM catalog.brand_err_hits"

_CHANNEL_SEED_SQL = """
INSERT INTO catalog.brand_err_hits (brand_key, brand, source, added_date)
SELECT brand_key, brand, source, added_date
FROM catalog.brand_blacklist
WHERE source LIKE '沃尔玛%%'
ON CONFLICT (brand_key) DO NOTHING
"""

_MASTER_WM_COUNT_SQL = """
SELECT count(*) FROM catalog.brand_blacklist WHERE source LIKE '沃尔玛%%'
"""

_CHANNEL_COUNT_SQL = _LATEST_CTE + """
SELECT count(*) FILTER (WHERE coalesce(btrim(p.brand), '') <> ''),
       count(*) FILTER (WHERE coalesce(btrim(p.brand), '') = ''),
       count(DISTINCT lower(btrim(p.brand)))
           FILTER (WHERE coalesce(btrim(p.brand), '') <> '')
FROM latest l
LEFT JOIN catalog.products p ON p.marketplace = 'US' AND p.asin = l.asin
WHERE l.cat = ANY(%(brandcats)s)
"""

_CHANNEL_REBUILD_SQL = _LATEST_CTE + """
INSERT INTO catalog.brand_err_hits
    (brand_key, brand, source, added_date, src_sku, src_store, biz_cn)
SELECT DISTINCT ON (lower(btrim(p.brand)))
       lower(btrim(p.brand)), btrim(p.brand),
       '沃尔玛-' || CASE l.cat WHEN 'C' THEN '品牌' WHEN 'E' THEN '知产' END,
       l.occurred_at::date::text, l.sku, l.store,
       (lower(coalesce(l.reason, '')) LIKE '%%biz-cn%%'
        OR lower(coalesce(l.reason, '')) LIKE '%%reference code biz%%')
FROM latest l
JOIN catalog.products p ON p.marketplace = 'US' AND p.asin = l.asin
WHERE l.cat = ANY(%(brandcats)s) AND coalesce(btrim(p.brand), '') <> ''
ORDER BY lower(btrim(p.brand)), l.occurred_at
ON CONFLICT (brand_key) DO NOTHING
"""


def channel_counts(conn) -> dict:
    """输入:连接 → 输出:渠道重建预览计数(不写任何东西)。"""
    with conn.cursor() as cur:
        cur.execute(_CHANNEL_COUNT_SQL, {"brandcats": sorted(BRAND_CATEGORIES)})
        with_brand, no_brand, brands = cur.fetchone()
        cur.execute(_MASTER_WM_COUNT_SQL)
        master = cur.fetchone()[0]
    return {"with_brand": with_brand, "no_brand": no_brand,
            "brands": brands, "master": master}


# ── 缺品牌候选(brand_scrape 工作流用:推采集补品牌)──────────────────────────

BRAND_SCRAPE_SCOPE = "cleanup:brand_scrape"     # ops.dedupe:已推过采集的 ASIN

_MISSING_BRAND_SQL = _LATEST_CTE + """
SELECT l.asin, l.sku, l.store, l.cat, l.reason
FROM latest l
LEFT JOIN catalog.products p
    ON p.marketplace = 'US' AND p.asin = l.asin
   AND coalesce(btrim(p.brand), '') <> ''
WHERE l.cat = ANY(%(brandcats)s) AND p.asin IS NULL
  AND NOT EXISTS (SELECT 1 FROM ops.dedupe d
                  WHERE d.scope = %(done_scope)s AND d.key = l.sku)
ORDER BY l.occurred_at
"""


def missing_brand_items(conn) -> list[dict]:
    """输入:连接 → 输出:C/E 最新类中**采集库查无品牌**且未做过品牌收集的
    候选 [{asin, sku, store, category, reasons}](brand_scrape 的进货清单)。"""
    with conn.cursor() as cur:
        cur.execute(_MISSING_BRAND_SQL,
                    {"brandcats": sorted(BRAND_CATEGORIES),
                     "done_scope": BRAND_ASIN_SCOPE})
        return [{"asin": a, "sku": sku, "store": store,
                 "category": cat, "reasons": reason}
                for a, sku, store, cat, reason in cur.fetchall()]


def scrape_attempted(conn, asins: list[str]) -> set:
    """输入:连接 + ASIN 列表 → 输出:已推过采集的子集(防无限循环的账)。"""
    if not asins:
        return set()
    with conn.cursor() as cur:
        cur.execute(_PROCESSED_SQL, (BRAND_SCRAPE_SCOPE, asins))
        return {r[0] for r in cur.fetchall()}


def mark_scrape_attempted(conn, asins: list[str], batch_name: str) -> None:
    """输入:连接 + 本次推送的 ASIN + 批次名 → 输出:无(记台账)。"""
    with conn.cursor() as cur:
        for a in asins:
            cur.execute(_MARK_SQL, (BRAND_SCRAPE_SCOPE, a,
                                    json.dumps({"batch": batch_name},
                                               ensure_ascii=False)))


def rebuild_brand_channel(conn) -> dict:
    """输入:连接 → 输出:{wiped, seeded, derived}。擦净重灌(渠道表是
    历史的投影,投影可以重投);先认领(真历史日期)后推导(补漏)。"""
    with conn.cursor() as cur:
        cur.execute(_CHANNEL_WIPE_SQL)
        wiped = cur.rowcount or 0
        cur.execute(_CHANNEL_SEED_SQL)
        seeded = cur.rowcount or 0
        cur.execute(_CHANNEL_REBUILD_SQL, {"brandcats": sorted(BRAND_CATEGORIES)})
        return {"wiped": wiped, "seeded": seeded, "derived": cur.rowcount or 0}
