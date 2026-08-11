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

  BIZ-CN 独立成维度(legacy_survey:2077:唯一明确标注中国卖家专属禁售的
  错误码,不能被 C 品牌类的关键词匹配吸收)——两张表都带 biz_cn 布尔列。

写入方向:cleanup → PG(本文件)→ 飞书投影(blacklist_push 工作流按
pushed_at 水位推)。PG 权威,飞书只是人机界面。
"""

import json
import logging

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
    (asin, category, source, reason, src_store, biz_cn)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (asin) DO NOTHING
"""


def record_asins(conn, items: list[dict]) -> int:
    """输入:连接 + 当轮已归类 item(store/sku/category/reasons)
    → 输出:新入选数。永久禁止 = 一次入选,已在名单的不更新(DO NOTHING)。"""
    added = 0
    with conn.cursor() as cur:
        for it in items:
            code = it.get("category")
            if code not in PERMANENT:
                continue
            cur.execute(_ASIN_SQL, (
                it["sku"], code, source_label(code),
                (it.get("reasons") or "")[:200] or None,
                it.get("store"), is_biz_cn(it.get("reasons"))))
            added += cur.rowcount or 0
    return added


_PROCESSED_SQL = "SELECT key FROM ops.dedupe WHERE scope = %s AND key = ANY(%s)"

_BRAND_OF_SQL = """
SELECT asin, brand FROM catalog.products
WHERE marketplace = 'US' AND asin = ANY(%s)
  AND coalesce(btrim(brand), '') <> ''
"""

_BRAND_SQL = """
INSERT INTO catalog.brand_blacklist
    (brand_key, brand, source, added_date, src_sku, biz_cn)
VALUES (%s, %s, %s, CURRENT_DATE::text, %s, %s)
ON CONFLICT (brand_key) DO NOTHING
"""

_MARK_SQL = """
INSERT INTO ops.dedupe (scope, key, meta)
VALUES (%s, %s, %s::jsonb) ON CONFLICT DO NOTHING
"""


def collect_brands(conn, items: list[dict]) -> dict:
    """输入:连接 + 当轮已归类 item → 输出:统计 dict。

    C/E 类 → 查品牌 → 新品牌入 brand_blacklist(**DO NOTHING**:risk_sync
    镜像来的行不覆盖——镜像行是飞书人工登记的真值,自产行只补空白)→
    标 ASIN 已处理。**品牌缺失的 ASIN 不标已处理**:产品中心还没这行或
    brand 为空,等 product_ingest 补上后下一轮自然重试,标了就永远漏了。
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
        cur.execute(_BRAND_OF_SQL, (list(todo),))
        brand_of = {a: b for a, b in cur.fetchall()}
        for asin, it in todo.items():
            brand = (brand_of.get(asin) or "").strip()
            if not brand:
                stats["no_brand"] += 1      # 不标已处理:等品牌到位重试
                continue
            cur.execute(_BRAND_SQL, (
                brand.casefold(), brand, source_label(it["category"]),
                asin, is_biz_cn(it.get("reasons"))))
            if cur.rowcount:
                stats["brand_new"] += 1
            else:
                stats["brand_known"] += 1
            cur.execute(_MARK_SQL, (BRAND_ASIN_SCOPE, asin,
                                    json.dumps({"brand": brand.casefold()},
                                               ensure_ascii=False)))
    return stats
