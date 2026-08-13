"""审核规则引擎门面(批次 B:零 LLM 纯规则层;全案 docs/audit_migration_plan.md)。

组装三块积木:audit_phase0(四件套短路)→ PT 解析(本文件,批次 B 版三级的
前两级)→ audit_l2(R0-R8)→ audit_reason(37 政策理由映射)。

PT 解析(架构 10.3 的批次 B 裁剪版,批次 C 接 LLM rerank 前只有两级):
  ① 沃尔玛实证:catalog.walmart_items.product_type(sku=asin,跨店唯一才采信)
     —— 沃尔玛自己认过的类目就是标准答案(批复 #10),pt_source='walmart_confirmed'
  ② 映射表精确:audit.walmart_category_map 按完整 amazon_category 等值,
     恰一个 PT 且置信度'高'才直出,pt_source='map_direct'
  解不出 → verdict='pending'(批次 B 自定义行为,旧仓由 L1 LLM 保底;
  这里保守不放行,等批次 C 接 L1 后自动重审——审核宁缺勿滥)。

上下文加载 fail-fast(与旧仓 phase0 三表的 fail-soft 有意不同):旧 worker
是常驻进程,单表挂了降级放行还能干活;本仓是批处理工作流,同一个库连不上
连落库都做不了,空集放行只会批量产出假 pass——宁可整轮报错停在原地。
"""

import json
import logging
from dataclasses import dataclass, field

from registry import paths
from services import audit_l2, audit_phase0, audit_reason
from services.audit_models import AuditOutcome, L1Info
from services.audit_stopwords import is_stopword

logger = logging.getLogger("services.audit_rules")


@dataclass
class AuditContext:
    """规则引擎的全部数据依赖(load_context 一次装配,规则函数零 DB 访问)。"""
    phase0_sellers: frozenset
    phase0_asins: frozenset
    phase0_cats: frozenset
    brand_blacklist: dict          # 规整小写 → 原文(DB first-wins + yaml 补)
    pt_meta: dict                  # PT → row dict
    pt_spec: dict                  # PT → row dict
    ac_automaton: object           # ahocorasick.Automaton 或 None(R4)
    mega: list
    nrtl_small: list
    nrtl_whole: list
    nice_mapping: dict
    nice_default: list
    uspto: object = None           # psycopg 连接或 None(R5 开关)
    walmart_confirmed: dict = field(default_factory=dict)   # asin → PT(跨店唯一)
    catmap: dict = field(default_factory=dict)               # amazon_category → [(pt, confidence)]
    known_policies: frozenset = frozenset()                  # 37 政策 category_en 集合


def _brand_map(conn) -> dict:
    """输入:连接 → 输出:品牌黑名单 dict(规整小写→原文;DB first-wins,yaml 补)。

    规整算法与 audit_phase0 品牌规则同款:strip → lower → 空白压单空格。
    yaml 只消费 additional_hard_brands(spec_phase0 §7:其余键是 R6 死配置)。
    """
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute("SELECT brand FROM audit.blacklist_brands")
        for (brand,) in cur.fetchall():
            raw = (brand or "").strip()
            if not raw:
                continue
            norm = " ".join(raw.lower().split())
            if norm and norm not in out:
                out[norm] = raw
    yaml_path = paths.audit_seed_file("compat_ip_brands.yaml")
    if yaml_path.exists():
        import yaml as _yaml
        data = _yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        for item in data.get("additional_hard_brands") or []:
            raw = (str(item) or "").strip()
            if not raw:
                continue
            norm = " ".join(raw.lower().split())
            if norm and norm not in out:
                out[norm] = raw
    logger.info("品牌黑名单加载 %d 键(DB + yaml additional)", len(out))
    return out


def _frozen(conn, sql: str) -> frozenset:
    with conn.cursor() as cur:
        cur.execute(sql)
        return frozenset(r[0] for r in cur.fetchall() if r[0])


def _rows_dict(conn, sql: str, key: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        out = {}
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            k = d.pop(key)
            if k:
                out[k] = d
        return out


def _build_automaton(brand_keys) -> object:
    """输入:规整小写品牌键 → 输出:Aho-Corasick 自动机(R4;逐字迁 spec_l2 §5.3)。

    过滤:len<2 跳过、is_stopword 剔除(min_len=4 默认)——42,726 → 有效词数记日志。
    """
    import ahocorasick
    a = ahocorasick.Automaton()
    kept = 0
    for b in brand_keys:
        if not b or len(b) < 2 or is_stopword(b):
            continue
        a.add_word(b, b)
        kept += 1
    a.make_automaton()
    logger.info("R4 品牌自动机:%d/%d 词(停用词过滤后)", kept, len(brand_keys))
    return a


def load_context(conn, *, uspto=None) -> AuditContext:
    """输入:中心库连接(+可选 uspto 只读连接)→ 输出:装配完成的 AuditContext。"""
    brand = _brand_map(conn)
    nice_mapping, nice_default = audit_l2.load_nice_mapping()
    nrtl_small, nrtl_whole = audit_l2.load_nrtl_keywords()
    with conn.cursor() as cur:
        # 实证 PT:跨店同 SKU(=ASIN)只认唯一口径,多 PT 并存的少数行不采信
        cur.execute(
            "SELECT sku, min(product_type) FROM catalog.walmart_items "
            "WHERE product_type IS NOT NULL AND product_type <> '' "
            "GROUP BY sku HAVING count(DISTINCT product_type) = 1")
        confirmed = dict(cur.fetchall())
        cur.execute(
            "SELECT amazon_category, walmart_product_type, confidence "
            "FROM audit.walmart_category_map")
        catmap: dict = {}
        for cat, pt, conf in cur.fetchall():
            if cat and pt:
                catmap.setdefault(cat, []).append((pt, conf))
    return AuditContext(
        phase0_sellers=_frozen(conn, "SELECT seller_id FROM audit.phase0_blacklist_sellers"),
        phase0_asins=_frozen(conn, "SELECT asin FROM audit.phase0_blacklist_asins"),
        phase0_cats=_frozen(conn, "SELECT category_norm FROM audit.phase0_blacklist_amazon_cats"),
        brand_blacklist=brand,
        pt_meta=_rows_dict(conn, "SELECT walmart_product_type, walmart_category, "
                                 "walmart_ptg, access_state, zh_can_do, requirements, "
                                 "notes FROM audit.walmart_pt_meta",
                           "walmart_product_type"),
        pt_spec=_rows_dict(conn, "SELECT walmart_product_type, has_real_cert, "
                                 "real_cert_fields, has_soft_cert, soft_cert_fields "
                                 "FROM audit.walmart_pt_spec",
                           "walmart_product_type"),
        ac_automaton=_build_automaton(brand.keys()),
        mega=audit_l2.load_mega_categories(),
        nrtl_small=nrtl_small, nrtl_whole=nrtl_whole,
        nice_mapping=nice_mapping, nice_default=nice_default,
        uspto=uspto,
        walmart_confirmed=confirmed,
        catmap=catmap,
        known_policies=_frozen(conn, "SELECT category_en FROM "
                                     "audit.walmart_prohibited_policy "
                                     "WHERE category_en IS NOT NULL"),
    )


def resolve_pt(product, ctx: AuditContext) -> L1Info:
    """输入:产品 + 上下文 → 输出:L1Info(批次 B 两级 PT 解析;解不出 PT=None)。"""
    pt = ctx.walmart_confirmed.get(product.asin)
    source, conf = None, None
    if pt:
        source, conf = "walmart_confirmed", "高"
    else:
        rows = ctx.catmap.get(product.amazon_category_path or "") or []
        pts = {p for p, _ in rows}
        if len(pts) == 1:
            only_pt, only_conf = rows[0][0], rows[0][1]
            if all(c == "高" for _, c in rows):
                pt, source, conf = only_pt, "map_direct", only_conf
    meta = ctx.pt_meta.get(pt) if pt else None
    return L1Info(walmart_product_type=pt, pt_confidence=conf, pt_source=source,
                  walmart_category=(meta or {}).get("walmart_category"))


def audit_one(product, ctx: AuditContext) -> AuditOutcome:
    """输入:ProductInfo + 上下文 → 输出:AuditOutcome(不落库,纯判定)。"""
    p0 = audit_phase0.check(product, ctx)
    if p0.blocked:
        # 旧仓字面量三件套照迁(orchestrator.py:340-343):score_final 硬写 0
        outcome = AuditOutcome(
            asin=product.asin, verdict="reject", score_final=0,
            stage_stopped_at="L0",
            l1=L1Info(walmart_product_type="(phase0_blocked)",
                      pt_confidence="低", pt_source="skipped"),
            phase0=p0)
        outcome.final_reason_category = audit_reason.compute_final_reason(
            outcome, product)
        return outcome

    l1 = resolve_pt(product, ctx)
    if not l1.walmart_product_type:
        # 批次 B 自定义:PT 解不出 → pending 等批次 C(不放行——旧仓此处有
        # L1 LLM 保底,零 LLM 形态下四条硬规则会整体失明,approve 等于裸奔)
        return AuditOutcome(asin=product.asin, verdict="pending",
                            score_final=None, stage_stopped_at="L1",
                            l1=l1, phase0=p0)

    l2 = audit_l2.evaluate(product, l1, ctx)
    verdict = l2.verdict
    outcome = AuditOutcome(
        asin=product.asin, verdict=verdict, score_final=l2.score_final,
        stage_stopped_at="L2" if verdict == "reject" else None,
        l1=l1, phase0=p0, l2=l2)
    if verdict == "reject":
        outcome.final_reason_category = audit_reason.compute_final_reason(
            outcome, product)
        if ctx.known_policies and not audit_reason.known_policies_check(
                outcome.final_reason_category, ctx.known_policies):
            # 兜底触发必须记日志计数(铁律):落在 37 政策集合外只记不改判
            logger.warning("理由映射落在 37 政策外:%s(asin=%s)",
                           outcome.final_reason_category, product.asin)
    return outcome


def product_info_from_row(row: dict):
    """输入:product_audit 取数行 dict → 输出:ProductInfo(空值/形态归一)。

    bullet_points 兼容两种形态(mp_mapper.py:182-184 先例):jsonb 数组或
    换行分隔字符串;long_description 三键兜底在 SQL 侧已 coalesce。
    """
    from services.audit_models import ProductInfo
    bullets = row.get("bullet_points")
    if isinstance(bullets, str):
        try:
            bullets = json.loads(bullets)
        except ValueError:
            bullets = [x for x in bullets.splitlines() if x.strip()]
    if isinstance(bullets, list):
        bullets = [str(b) for b in bullets if b]
    else:
        bullets = []
    return ProductInfo(
        asin=row["asin"],
        title=row.get("title") or "",
        brand=row.get("brand") or "",
        bullet_points=bullets,
        long_description=row.get("long_description") or "",
        amazon_category_path=row.get("amazon_category_path") or "",
        seller_id=row.get("seller_id") or "",
        seller_name=row.get("seller_name") or "",
    )
