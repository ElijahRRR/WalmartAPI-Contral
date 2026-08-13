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
    brand_blacklist: dict          # 规整小写 → 原文(黑名单中心,first-wins)
    pt_meta: dict                  # PT → row dict
    pt_spec: dict                  # PT → row dict
    ac_automaton: object           # ahocorasick.Automaton 或 None(R4)
    mega: list
    nrtl_small: list
    nrtl_whole: list
    nice_mapping: dict
    nice_default: list
    uspto: object = None           # psycopg 连接或 None(R5 开关)
    walmart_confirmed: dict = field(default_factory=dict)   # asin → PT(跨店唯一,已 pt_meta 闸)
    catmap: dict = field(default_factory=dict)               # amazon_category → PT(高置信唯一,已 pt_meta 闸)
    known_policies: frozenset = frozenset()                  # 37 政策 category_en 集合
    uspto_failures: int = 0        # R5 连续失败计数(audit_l2 递增,≥5 自动关停)


def _brand_map(conn) -> tuple[dict, set]:
    """输入:连接 → 输出:(Phase0 品牌 dict, R4 词集)——同源黑名单中心,两套口径。

    源 = **catalog.brand_blacklist**(黑名单中心品牌总表镜像;所有者定稿
    2026-08-13:黑名单只维护一份,不再读 audit.blacklist_brands 快照,也不再
    合并 compat yaml 的 34 个手补牌子——要补进飞书品牌总表,单源)。
    Phase0 dict(规整小写→原文):strip → lower → 空白压单空格。
    R4 词集:只 strip+lower(保留词内空白,旧 l2 加载器口径)。
    """
    phase0: dict = {}
    r4: set = set()
    with conn.cursor() as cur:
        cur.execute("SELECT brand FROM catalog.brand_blacklist")
        for (brand,) in cur.fetchall():
            raw = (brand or "").strip()
            if not raw:
                continue
            r4.add(raw.lower())
            norm = " ".join(raw.lower().split())
            if norm and norm not in phase0:
                phase0[norm] = raw
    logger.info("品牌黑名单加载(黑名单中心):Phase0 %d 键 / R4 %d 键",
                len(phase0), len(r4))
    return phase0, r4


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


_UNMAPPED_SENTINEL = "无对应Walmart PT"   # 映射表哨兵值(675 行),不是真 PT


def load_context(conn, *, uspto=None) -> AuditContext:
    """输入:中心库连接(+可选 uspto 只读连接)→ 输出:装配完成的 AuditContext。

    实证/映射两级 PT 源在装配期就过 pt_meta 闸(评审 P0-1:废弃 PT——如
    'Office Chairs' 已改名 'Desk Chairs'——直出会让 R0/R1/R2/R3 四闸集体失明
    产出假 pass;旧仓 l1_category.py:605-620 用 INNER JOIN pt_meta 防的正是它)。
    """
    brand, r4_keys = _brand_map(conn)
    nice_mapping, nice_default = audit_l2.load_nice_mapping()
    nrtl_small, nrtl_whole = audit_l2.load_nrtl_keywords()
    pt_meta = _rows_dict(conn, "SELECT walmart_product_type, walmart_category, "
                               "walmart_ptg, access_state, zh_can_do, requirements, "
                               "notes FROM audit.walmart_pt_meta",
                         "walmart_product_type")
    with conn.cursor() as cur:
        # 实证 PT:SKU 先归一成 ASIN(所有者 2026-08-11 推翻 sku=asin 全局约定,
        # 生产 SKU 形如 XKJ-B0XXX-39.98,唯一规则出处 services/sku_asin)——
        # 直接拿 sku 当键会让实证级对三段式 SKU 全部失明(评审 I-1);
        # 归一后仍保持"同一 ASIN 跨店多 PT 不采信"
        from services.sku_asin import extract_asin
        cur.execute("SELECT sku, product_type FROM catalog.walmart_items "
                    "WHERE product_type IS NOT NULL AND product_type <> ''")
        by_asin: dict = {}
        for sku, pt in cur.fetchall():
            asin = extract_asin(sku) or sku
            by_asin.setdefault(asin, set()).add(pt)
        confirmed = {a: next(iter(pts)) for a, pts in by_asin.items()
                     if len(pts) == 1 and next(iter(pts)) in pt_meta}
        # 映射表:先筛 confidence='高' 再数 DISTINCT PT(旧仓快速通道同款,
        # l1_category.py:605-620),剔哨兵、过 pt_meta 闸,装配期直接压成 str
        cur.execute(
            "SELECT amazon_category, walmart_product_type "
            "FROM audit.walmart_category_map "
            "WHERE confidence = '高' AND walmart_product_type <> %s",
            (_UNMAPPED_SENTINEL,))
        cat_pts: dict = {}
        for cat, pt in cur.fetchall():
            if cat and pt:
                cat_pts.setdefault(cat.strip(), set()).add(pt)
        catmap = {cat: next(iter(pts)) for cat, pts in cat_pts.items()
                  if len(pts) == 1 and next(iter(pts)) in pt_meta}
    # 四闸全部直读黑名单中心(所有者定稿 2026-08-13,一份数据):
    # 卖家/类目 = risk_sync 镜像的两张新表;ASIN = 自产黑名单(问题商品清理
    # + 违禁回执 + 历史继承导入,5.6 万+ 行)——比旧 Phase0 三列表覆盖大得多
    return AuditContext(
        phase0_sellers=_frozen(conn, "SELECT seller_id FROM catalog.seller_blacklist"),
        phase0_asins=_frozen(conn, "SELECT asin FROM catalog.asin_blacklist"),
        phase0_cats=_frozen(conn, "SELECT category_norm FROM catalog.amazon_cat_blacklist"),
        brand_blacklist=brand,
        pt_meta=pt_meta,
        pt_spec=_rows_dict(conn, "SELECT walmart_product_type, has_real_cert, "
                                 "real_cert_fields, has_soft_cert, soft_cert_fields "
                                 "FROM audit.walmart_pt_spec",
                           "walmart_product_type"),
        ac_automaton=_build_automaton(r4_keys),
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
    """输入:产品 + 上下文 → 输出:L1Info(批次 B 两级 PT 解析;解不出 PT=None)。

    两级数据在 load_context 已过三道闸(pt_meta 存在/剔哨兵/高置信唯一),
    此处再兜一道防御(闸的唯一出处在装配期,这里只是断言式保护)。
    批次 B 裁剪项(批次 C 归还):①级只用 walmart_items,计划 10.3① 的
    "历史成功上架记录"(audit.walmart_error_records 反哺)未消费;
    旧仓对映射到哨兵'无对应Walmart PT'的类目是 -100 硬拒,本批落 pending
    (保守方向,批次 C 接 L1 后按旧语义处置)。
    """
    pt = ctx.walmart_confirmed.get(product.asin)
    source, conf = None, None
    if pt:
        source, conf = "walmart_confirmed", "高"
    else:
        pt = ctx.catmap.get((product.amazon_category_path or "").strip())
        if pt:
            source, conf = "map_direct", "高"
    if pt and pt not in ctx.pt_meta:      # 防御:废弃 PT 宁 pending 不假 pass
        pt, source, conf = None, None, None
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
