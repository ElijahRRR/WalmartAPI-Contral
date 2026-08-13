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
from services import audit_l1_llm, audit_l2, audit_phase0, audit_reason
from services.audit_models import AuditOutcome, L1Info, RuleHit
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
    error_confirmed: dict = field(default_factory=dict)      # asin → PT(报错日报实证,已 pt_meta 闸;批次 C)
    unmapped_paths: frozenset = frozenset()                  # 哨兵'无对应Walmart PT'的 amazon 路径(Layer 0)


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
        # 批次 C:①b 报错日报实证(批复 #10)与 Layer 0 哨兵路径集
        error_confirmed=audit_l1_llm.error_confirmed_map(conn, pt_meta),
        unmapped_paths=_frozen(
            conn, "SELECT DISTINCT amazon_category FROM "
                  "audit.walmart_category_map "
                  f"WHERE walmart_product_type = '{_UNMAPPED_SENTINEL}'"),
    )


def resolve_pt(product, ctx: AuditContext) -> L1Info:
    """输入:产品 + 上下文 → 输出:L1Info(免 LLM 的 PT 解析;解不出 PT=None)。

    批次 C 定稿的级序(合同 L1-8/L1-10):
      ① 沃尔玛在架实证(walmart_items,跨店唯一)         pt_source='walmart_confirmed'
      ①b 历史报错日报实证(walmart_error_records 最新条)  pt_source='walmart_error_confirmed'
      ⓪ 哨兵硬拒(映射表明确标记'无对应Walmart PT')——批复 #10 实证最优先,
        故哨兵从旧仓的最前挪到实证之后、映射之前(差异会进双跑校准报告)
      ② 映射表精确(catmap 高置信唯一)                   pt_source='map_direct'
    直出各级统一过 seed 硬拦(带 PT 调,旧 Layer1 快速通道 :804 同款)与
    出版物硬禁(合同 L1-6:旧仓对全部三级生效,批次 B 漏接本批归还)。
    命中硬拦时返回的 L1Info 带 -100 hit——audit_l2.evaluate 会累加 l1.hits,
    分数自然判死,stage 落 'L2' 与旧库口径一致,无需特判。
    数据在 load_context 已过三道闸,此处 pt_meta 再兜一道防御。
    """
    pt = ctx.walmart_confirmed.get(product.asin)
    source, conf = None, None
    if pt:
        source, conf = "walmart_confirmed", "高"
    else:
        pt = ctx.error_confirmed.get(product.asin)
        if pt:
            source, conf = "walmart_error_confirmed", "高"
    path = (product.amazon_category_path or "").strip()
    if not pt and path and path in ctx.unmapped_paths:
        # Layer 0 哨兵(l1_category.py:779-797 字面量逐字:unknown/低/none 三件
        # + detail.amazon_path 用原文不 strip)
        l1 = L1Info(walmart_product_type="unknown", pt_confidence="低",
                    pt_source="none",
                    excluded_category_reason="无对应 Walmart PT (映射表明确标记)")
        l1.hits.append(RuleHit(
            stage="L1", rule_code="unmapped_amazon_path", penalty=-100,
            detail={"reason": "Amazon 路径在映射表被标记为 '无对应Walmart PT', "
                              "上架 Walmart 会失败",
                    "amazon_path": product.amazon_category_path}))
        return l1
    if not pt:
        pt = ctx.catmap.get(path)
        if pt:
            source, conf = "map_direct", "高"
    if pt and pt not in ctx.pt_meta:      # 防御:废弃 PT 宁 pending 不假 pass
        pt, source, conf = None, None, None
    meta = ctx.pt_meta.get(pt) if pt else None
    l1 = L1Info(walmart_product_type=pt, pt_confidence=conf, pt_source=source,
                walmart_category=(meta or {}).get("walmart_category"))
    if pt:
        seed = audit_l1_llm.check_seed_excluded(product, pt)
        if seed:
            l1.excluded_category_reason = seed
            l1.hits.append(RuleHit(
                stage="L1", rule_code="excluded_category", penalty=-100,
                detail={"reason": seed, "pt": pt, "from_seed_yaml": True}))
        ban = audit_l1_llm.check_publication_ban(
            pt, l1.excluded_category_reason)
        if ban is not None:
            l1.excluded_category_reason = ban.detail["reason"]
            l1.hits.append(ban)
    return l1


def audit_one(product, ctx: AuditContext, conn=None, *,
              run_l3: bool = True, run_l4: bool = False) -> AuditOutcome:
    """输入:ProductInfo + 上下文(+连接与层开关)→ 输出:AuditOutcome。

    批次 C 全链:phase0 → L1(实证→哨兵→映射→候选+rerank)→ L2 → [L3 → L4]。
    conn=None 时退化为批次 B 形态(L1 第三级与 L3/L4 需要查库/调 LLM,全跳过,
    PT 解不出照旧 pending)——测试与离线路径复用。流转语义逐字迁自
    orchestrator.py:378-398:进 L3 条件 = l2 pass;L3 reject/pending 改判不动分;
    L4 仅 outcome pass 且开关开,只认 reject(默认关,批复 #2)。
    """
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
    if not l1.walmart_product_type and not l1.hits and conn is not None:
        # L1 第三级:候选召回 + rerank(哨兵命中带 -100 hit 者不进——已判死)。
        # 空候选由 rerank 自己短路(合同 L1-5:不调 LLM 直接解不出)
        cands = audit_l1_llm.candidates(conn, product)
        pt_dict = ctx.pt_meta.keys() | ctx.pt_spec.keys()
        l1_llm = audit_l1_llm.rerank(product, cands, pt_dict)
        if l1_llm is not None:
            l1 = l1_llm
            meta = ctx.pt_meta.get(l1.walmart_product_type)
            if meta:
                l1.walmart_category = meta.get("walmart_category")
    if not l1.walmart_product_type and not l1.hits:
        # PT 解不出(rerank unknown/LLM 失败/坏 JSON/无候选)→ pending,
        # 绝不默认放行(10.2)。哨兵/excluded 命中(有 -100 hit)不走此路
        return AuditOutcome(asin=product.asin, verdict="pending",
                            score_final=None, stage_stopped_at="L1",
                            l1=l1, phase0=p0)

    l2 = audit_l2.evaluate(product, l1, ctx)
    verdict = l2.verdict
    outcome = AuditOutcome(
        asin=product.asin, verdict=verdict, score_final=l2.score_final,
        stage_stopped_at="L2" if verdict == "reject" else None,
        l1=l1, phase0=p0, l2=l2)

    # L3 语义(orchestrator.py:378-389):唯一条件 = l2 pass 且开关开;
    # reject/pending 改判 + stage='L3',分数保留 L2 值(L3 不动分)
    if verdict == "pass" and run_l3 and conn is not None:
        from services import audit_l3
        l3 = audit_l3.judge_l3(product, l1, l2, ctx, conn)
        outcome.l3 = l3
        if l3.verdict == "reject":
            outcome.verdict = "reject"
            outcome.stage_stopped_at = "L3"
        elif l3.verdict == "pending":
            outcome.verdict = "pending"
            outcome.stage_stopped_at = "L3"

    # L4 视觉(orchestrator.py:392-398):条件 = outcome pass(非 l3 pass)
    # 且开关开;只认 reject;默认关(批复 #2),故障→pass 由 audit_l4 内保证
    if outcome.verdict == "pass" and run_l4 and conn is not None:
        from services import audit_l4
        l4 = audit_l4.judge_l4(product, l1, l3=outcome.l3, conn=conn)
        outcome.l4 = l4
        if l4.verdict == "reject":
            outcome.verdict = "reject"
            outcome.stage_stopped_at = "L4"

    if outcome.verdict == "reject":
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
