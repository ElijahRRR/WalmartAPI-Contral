"""审核规则引擎门面(批次 B:零 LLM 纯规则层;全案 docs/audit_migration_plan.md)。

组装三块积木:audit_phase0(四件套短路)→ PT 解析(本文件,批次 B 版三级的
前两级)→ audit_l2(R1/R3-R8)→ audit_reason(37 政策理由映射)。

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

from registry import paths, resources
from services import (audit_l1_llm, audit_l2, audit_phase0, audit_reason,
                      category_blacklist)
from services.audit_models import AuditOutcome, L1Info, RuleHit
from services.audit_stopwords import is_stopword

logger = logging.getLogger("services.audit_rules")


@dataclass
class AuditContext:
    """规则引擎的全部数据依赖(load_context 一次装配,规则函数零 DB 访问)。"""
    phase0_sellers: frozenset
    phase0_asins: frozenset
    brand_blacklist: dict          # 规整小写 → 原文(黑名单中心,first-wins)
    pt_meta: dict                  # PT → row dict
    ac_automaton: object           # ahocorasick.Automaton 或 None(R4)
    nice_mapping: dict
    nice_default: list
    uspto: object = None           # psycopg 连接或 None(R5 开关)
    walmart_confirmed: dict = field(default_factory=dict)   # asin → PT(跨店唯一,已 pt_meta 闸)
    catmap: dict = field(default_factory=dict)               # amazon_category → PT(高置信唯一,已 pt_meta 闸)
    known_policies: frozenset = frozenset()                  # 37 政策 category_en 集合
    uspto_failures: int = 0        # R5 连续失败计数(audit_l2 递增,≥5 自动关停)
    unmapped_paths: frozenset = frozenset()                  # 哨兵'无对应Walmart PT'的 amazon 路径(Layer 0)
    path_alias: dict = field(default_factory=dict)            # 产品侧路径 → 映射表等价路径(catmap_align 产出)
    node_map: dict = field(default_factory=dict)              # browse_node_id → PT(高置信唯一,已 pt_meta 闸)
    # 类目黑名单(2026-08-20:代码里的类目常量搬进 DB)。三种匹配一次装配:
    # 子树 node_id / 顶级名 / 完整路径等值,判定见 services.category_blacklist.check
    cat_rules: object = None
    # R4 键 → 黑名单来源原文(2026-08-30,TRO 命中接线)。**带默认值**:
    # 测试里手搓的 ctx 不给它也照跑,TRO 那一路自然退化成"一个都不命中"。
    r4_source: dict = field(default_factory=dict)


def _brand_map(conn) -> tuple[dict, set, dict]:
    """输入:连接 → 输出:(Phase0 品牌 dict, R4 词集, R4 键→来源原文)——同源三套口径。

    源 = **catalog.brand_blacklist**(黑名单中心品牌总表镜像;所有者定稿
    2026-08-13:黑名单只维护一份,不再读 audit.blacklist_brands 快照,也不再
    合并 compat yaml 的 34 个手补牌子——要补进飞书品牌总表,单源)。
    Phase0 dict(规整小写→原文):strip → lower → 空白压单空格。
    R4 词集:只 strip+lower(保留词内空白,旧 l2 加载器口径)。
    R4 来源 dict:**键与 R4 词集同型**(strip+lower),值是 `source` 列原文 ——
    TRO 命中接线按它认「这个黑名单词是不是 TRO 品牌」(判据在
    services/audit_store.tro_hits,前缀常量在 registry)。
    ⚠ 同一个键多行时 **setdefault 先到先得**:总表按 brand_key 主键去重,同键
    多行只可能来自大小写/空白不同的两行,来源多半也一样;真不一样时取哪个都是
    猜,先到先得至少是确定的(而且 TRO 判据是"命中即报",漏报比乱报更容易
    被下一条同品牌产品补上)。
    """
    phase0: dict = {}
    r4: set = set()
    r4_source: dict = {}
    with conn.cursor() as cur:
        # source 是全表扫多带的一列(总表 4 万余行,无索引问题)
        cur.execute("SELECT brand, source FROM catalog.brand_blacklist")
        for brand, source in cur.fetchall():
            raw = (brand or "").strip()
            if not raw:
                continue
            key = raw.lower()
            r4.add(key)
            if source:
                r4_source.setdefault(key, source)
            norm = " ".join(key.split())
            if norm and norm not in phase0:
                phase0[norm] = raw
    logger.info("品牌黑名单加载(黑名单中心):Phase0 %d 键 / R4 %d 键",
                len(phase0), len(r4))
    return phase0, r4, r4_source


def _frozen(conn, sql: str) -> frozenset:
    with conn.cursor() as cur:
        cur.execute(sql)
        return frozenset(r[0] for r in cur.fetchall() if r[0])


def _pairs(conn, sql: str) -> dict:
    """输入:连接 + 两列 SQL → 输出:{第一列: 第二列}(两侧非空才收)。"""
    with conn.cursor() as cur:
        cur.execute(sql)
        return {k: v for k, v in cur.fetchall() if k and v}


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


_UNMAPPED_SENTINEL = audit_l1_llm.UNMAPPED_SENTINEL   # 单一出处


def load_context(conn, *, uspto=None) -> AuditContext:
    """输入:中心库连接(+可选 uspto 只读连接)→ 输出:装配完成的 AuditContext。

    实证/映射两级 PT 源在装配期就过 pt_meta 闸(评审 P0-1:废弃 PT——如
    'Office Chairs' 已改名 'Desk Chairs'——直出会让 R0/R1/R2/R3 四闸集体失明
    产出假 pass;旧仓 l1_category.py:605-620 用 INNER JOIN pt_meta 防的正是它)。
    """
    brand, r4_keys, r4_source = _brand_map(conn)
    # TRO 口径守夜(2026-08-30):source 是**自由文本**,而且由飞书同步整列覆盖
    # (risk_sync)。所有者哪天把「TRO品牌」改成别的写法,这里会静默变成 0,
    # 而 TRO 接线的表现是"从此再也不报警"——最难发现的那种坏法。恒 0 就是
    # 口径漂了,把数字打出来让人能对。
    tro_n = sum(1 for s in r4_source.values()
                if str(s).strip().lower().startswith(
                    resources.TRO_BRAND_SOURCE_PREFIX))
    logger.info("R4 品牌来源:带来源 %d 词,其中 TRO 前缀 %d 词",
                len(r4_source), tro_n)
    if not tro_n:
        logger.warning("R4 品牌来源里 **TRO 前缀 0 词** —— 生产实证应有两万余条,"
                       "多半是黑名单总表「来源」列改了写法(前缀常量:"
                       "registry.resources.TRO_BRAND_SOURCE_PREFIX);"
                       "在此之前 TRO 命中接线整条不会报警")
    nice_mapping, nice_default = audit_l2.load_nice_mapping()
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
        # ⚠ 这里**故意**保持"先数 DISTINCT 再过闸",与下面映射表那两处相反:
        # 实证的歧义是"两家店对同一 ASIN 报了不同 PT",那是真分歧,与字典无关;
        # 而且 walmart_items 里的 PT 是沃尔玛线上现有的,它不在 pt_meta 只说明
        # 我们那张表收得不全,不代表这个 PT 假。别照着下面"优化"这一行。
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
            # ⚠ **先过 pt_meta 闸,再数 DISTINCT**(2026-08-17 修)。
            # 原来是反的:死 PT(字典里没有,永远不可能被返回)也进集合参与
            # "是否唯一"的计票,于是"一条有效 + 一条死"的路径被判成两义 →
            # 整条路径丢弃。生产实测因此白白丢掉 **105 条本可直出的路径**。
            # 成因:映射修正是"插新行不删旧行"(catmap_fix 的保留证据惯例),
            # 同一路径于是长期同时挂着新 PT 与旧的无效 PT。
            if cat and pt and pt in pt_meta:
                cat_pts.setdefault(cat.strip(), set()).add(pt)
        catmap = {cat: next(iter(pts)) for cat, pts in cat_pts.items()
                  if len(pts) == 1}
        # browse_node_id → PT(所有者定稿 2026-08-14:名称会漂 ID 不会)。
        # 同款三闸:高置信 + 剔哨兵 + 该 ID 恰一个 PT + pt_meta 存在
        cur.execute(
            "SELECT browse_node_id, walmart_product_type "
            "FROM audit.walmart_category_map "
            "WHERE confidence = '高' AND walmart_product_type <> %s "
            "  AND browse_node_id IS NOT NULL AND btrim(browse_node_id) <> ''",
            (_UNMAPPED_SENTINEL,))
        node_pts: dict = {}
        for node, pt in cur.fetchall():
            if node and pt and pt in pt_meta:      # 同上:先过闸再计票
                node_pts.setdefault(node.strip(), set()).add(pt)
        node_map = {n: next(iter(pts)) for n, pts in node_pts.items()
                    if len(pts) == 1}
        logger.info("类目锚:browse_node %d 个 / 路径 %d 条", len(node_map),
                    len(catmap))
    # 四闸全部直读黑名单中心(所有者定稿 2026-08-13,一份数据):
    # 卖家/类目 = risk_sync 镜像的两张新表;ASIN = 自产黑名单(问题商品清理
    # + 违禁回执 + 历史继承导入,5.6 万+ 行)——比旧 Phase0 三列表覆盖大得多
    p0_sellers = _frozen(conn, "SELECT seller_id FROM catalog.seller_blacklist")
    p0_asins = _frozen(conn, "SELECT asin FROM catalog.asin_blacklist")
    # ⚠ p0_cats **只为报数,不进 ctx**:类目闸整体改吃下面装配的 cat_rules,
    # 这条查询留着单纯是为了下面那行日志里的「类目 N」这个数字有出处。
    p0_cats = _frozen(conn, "SELECT category_norm FROM catalog.amazon_cat_blacklist"
                            " WHERE enabled")
    # 类目闸的判据全在库里(2026-08-20 所有者定稿:代码里的类目搬进 DB):
    # 子树 ID / 顶级名 / 路径等值三种匹配,一次装配
    cat_rules = category_blacklist.load(conn)
    # 报数与品牌黑名单同款(2026-08-19 所有者在运行日志里找不到这三张表的
    # 加载痕迹——加载是真的,静默也是真的;三张表载成空集与没加载在行为上
    # 无法区分,必须让数字见人)
    logger.info("黑名单中心加载:卖家 %d / ASIN %d / 类目 %d",
                len(p0_sellers), len(p0_asins), len(p0_cats))
    return AuditContext(
        phase0_sellers=p0_sellers,
        phase0_asins=p0_asins,
        cat_rules=cat_rules,
        brand_blacklist=brand,
        pt_meta=pt_meta,
        # ⚠ 2026-08-21 起**不再取 audit.walmart_pt_spec**:R3 收敛成"只看飞书
        # requirements"(所有者定稿),那张表是批次 A 搬来的死快照,而"整机 vs
        # 小件"这类推断已移交 L3 判定维度 6。表本身不删(pt_spec_sync 仍写它、
        # audit_why / pt_census 仍查它做诊断),只是审核链不再拿它当判据。
        ac_automaton=_build_automaton(r4_keys),
        r4_source=r4_source,
        nice_mapping=nice_mapping, nice_default=nice_default,
        uspto=uspto,
        walmart_confirmed=confirmed,
        catmap=catmap,
        known_policies=_frozen(conn, "SELECT category_en FROM "
                                     "audit.walmart_prohibited_policy "
                                     "WHERE category_en IS NOT NULL"),
        # 批次 C:Layer 0 哨兵路径集(①b 历史实证改读产品行 walmart_pt,
        # 经 pt_backfill 回填,不再装配证据 map——所有者定稿 2026-08-13)
        # btrim 与 catmap 的 cat.strip() 对称(评审 P2:带尾空白的哨兵行漏拦,
        # 同路径另一行高置信 PT 反而 strip 后进 catmap → 硬拒变直出)
        unmapped_paths=_frozen(
            conn, "SELECT DISTINCT btrim(amazon_category) FROM "
                  "audit.walmart_category_map "
                  f"WHERE walmart_product_type = '{_UNMAPPED_SENTINEL}' "
                  "AND btrim(amazon_category) <> ''"),
        # 路径别名(catmap_align:三套 Amazon 名称的中间层漂移)——②级精确
        # 未命中时折一次再查;表不存在/空则为空 dict,行为退化回纯精确匹配
        path_alias=_pairs(conn, "SELECT path, canonical_path "
                                "FROM audit.category_path_alias"),
        node_map=node_map,
    )


def _blocked(l1: L1Info) -> bool:
    """输入:L1Info → 输出:是否已被硬拦判死(有扣分 hit)。

    判据是 **penalty<0 而不是"有没有 hit"**:哨兵改成 0 分留痕后仍会往
    hits 里放一条,若照旧按"有 hit"判,那条留痕会把产品挡在第三级门外
    ——恰好抵消所有者"标注不该终结判定"的定稿。
    """
    return any(h.penalty < 0 for h in l1.hits)


def resolve_pt(product, ctx: AuditContext) -> L1Info:
    """输入:产品 + 上下文 → 输出:L1Info(免 LLM 的 PT 解析;解不出 PT=None)。

    批次 C 定稿的级序(合同 L1-8/L1-10):
      ① 沃尔玛在架实证(walmart_items,跨店唯一)         pt_source='walmart_confirmed'
      ①b 产品行已知 PT(products.walmart_pt);**按 pt_source 分道**——
        沃尔玛回执实证 → 'historical_confirmed'/高;我们自己推断的(含上一轮
        LLM 结论)→ 'audit_cached'/中,直出但不冒充实证(2026-08-14)
      ⓪ 哨兵硬拒(映射表明确标记'无对应Walmart PT')——批复 #10 实证最优先,
        故哨兵从旧仓的最前挪到实证之后、映射之前(差异会进双跑校准报告)
      ②a browse_node_id 直查(名称会漂 ID 不会)          pt_source='map_node'
      ②b 映射表路径精确(catmap 高置信唯一)              pt_source='map_direct'
        ——查表前先过路径别名折叠(catmap_align:Amazon 三套名称的中间层
        漂移,精确等值会把已映射路径当缺口);无 ID 的老行只有这一条路
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
    elif product.known_pt and product.known_pt in ctx.pt_meta:
        # ①b 产品行已知 PT(pt_backfill 回填的历史实证 / 先前审核结论;
        # 所有者定稿 2026-08-13:PT 长在产品主档,不查证据边表)。
        # **按来源分道**(2026-08-14):这一列同时装沃尔玛回执实证与我们
        # 自己的推断,不分道就等于"LLM 猜一个 → 下轮以高置信实证复述",
        # 猜错会被自己反复确认,而且外面看不出来。
        # 推断行仍然直出(不分道地重付 LLM,百万级产品成本不可接受),
        # 但记独立来源 + 置信'中',让校准/报表能把它单独拎出来看。
        pt = product.known_pt
        if product.known_pt_source == "walmart_confirmed":
            source, conf = "historical_confirmed", "高"
        else:
            source, conf = "audit_cached", "中"
    # ②a browse_node_id 直查(所有者定稿 2026-08-14:名称会漂 ID 不会)——
    # 采集侧 category_id_chain 的最后一段 = 当前最细类目,与映射表的
    # browse_node_id 列精确等值,不受三套名称不一致影响。无 ID 的老行
    # (契约追加前入库)自动落到下面的字符串路径
    if not pt and product.browse_node_id:
        pt = ctx.node_map.get(product.browse_node_id)
        if pt:
            source, conf = "map_node", "高"
    path = (product.amazon_category_path or "").strip()
    # ②b 路径别名折叠(catmap_align):产品侧面包屑与映射表的中间层名可能
    # 漂移('Home Décor Products' vs 'Home Décor'),精确等值会误判成缺口。
    # 折叠只影响"查得到查不到",不改任何判定语义;别名表空则退化回精确匹配
    if path and path not in ctx.catmap and path not in ctx.unmapped_paths:
        path = ctx.path_alias.get(path, path)
    sentinel = bool(not pt and path and path in ctx.unmapped_paths)
    if not pt:
        pt = ctx.catmap.get(path)
        if pt:
            source, conf = "map_direct", "高"
    if pt and pt not in ctx.pt_meta:      # 防御:废弃 PT 宁 pending 不假 pass
        pt, source, conf = None, None, None
    meta = ctx.pt_meta.get(pt) if pt else None
    l1 = L1Info(walmart_product_type=pt, pt_confidence=conf, pt_source=source,
                walmart_category=(meta or {}).get("walmart_category"))
    if sentinel:
        # 所有者定稿 2026-08-14:**标注"无对应 Walmart PT"不再判死**。
        # 旧仓在此硬拒 -100,理由是"上架必失败";但那条标注是当年没数据时
        # 人工打的,不代表今天判不出来——判不出来才该 pending,不该拒。
        # 改为 0 分留痕,继续走候选+LLM;信息一并进提示词供 LLM 参考。
        l1.hits.append(RuleHit(
            stage="L1", rule_code="unmapped_amazon_path", penalty=0,
            detail={"reason": "映射表曾标注 '无对应Walmart PT'(仅留痕,"
                              "不判死;交 L1 第三级候选+LLM 判定)",
                    "amazon_path": product.amazon_category_path}))
    if pt:
        # ⚠ 2026-08-20:此处原先还有一道 seed yaml 硬拦(3C/服饰/汽配/带电禁售),
        # 已随 excluded 整条链下线(所有者定稿 A1)——类目能不能做只由 L2 R1
        # 的准入白名单说了算,不再有第二份平行清单。出版物硬禁保留:它不是
        # 类目准入判断,是拿 walmart_error_records 实证打出来的知产风险(E 占比 ≥96%)。
        ban = audit_l1_llm.check_publication_ban(pt)
        if ban is not None:
            l1.hits.append(ban)
    return l1


def audit_one(product, ctx: AuditContext, conn=None, *,
              run_l3: bool = True, run_l4: bool = False,
              only_l0: bool = False) -> AuditOutcome | None:
    """输入:ProductInfo + 上下文(+连接与层开关)→ 输出:AuditOutcome;
    only_l0 且 Phase0 未命中时返回 **None**(见下)。

    批次 C 全链:phase0 → L1(实证→哨兵→映射→候选+rerank)→ L2 → [L3 → L4]。
    conn=None 时退化为批次 B 形态(L1 第三级与 L3/L4 需要查库/调 LLM,全跳过,
    PT 解不出照旧 pending)——测试与离线路径复用。流转语义逐字迁自
    orchestrator.py:378-398:进 L3 条件 = l2 pass;L3 reject/pending 改判不动分;
    L4 仅 outcome pass 且开关开,只认 reject(默认关,批复 #2)。
    """
    p0 = audit_phase0.check(product, ctx)
    if only_l0 and not p0.blocked:
        # 只跑 L0(stages=L0,所有者 2026-08-18):纯查库、零 LLM。
        # 未命中 ⇒ 返回 None = **不落结论**:不写 runs、不动 products、
        # 不盖规则版本 —— 截断的链没资格发 pass/pending(不完整审核
        # 绝不当通过,与「任一道给不出确定答案一律待人工」同一条纪律)。
        # 用途:配合 rerule 零 LLM 翻新黑名单历史行 —— 仍命中的重判
        # (拿到新版本的理由映射),不再命中的保持原判、不被"复活"。
        return None
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
    if not l1.walmart_product_type and not _blocked(l1) and conn is not None:
        # L1 第三级:候选召回 + rerank(哨兵命中带 -100 hit 者不进——已判死)。
        # 空候选由 rerank 自己短路(合同 L1-5:不调 LLM 直接解不出)
        cands = audit_l1_llm.candidates(conn, product)
        if not cands:
            # 七路全空 = 映射表和标题都给不出参考。所有者定稿 2026-08-14:
            # 这时**仍要让 LLM 判**,走两阶段(先选沃尔玛大类,再在该大类
            # 的全部 PT 里挑),而不是直接 pending
            cands = audit_l1_llm.open_candidates(conn, product)
        # 字典收窄为 pt_meta(评审 P0 修正:旧仓 pt_meta∪pt_spec,但 L2 四硬闸
        # 全部只查 pt_meta——spec-only PT 直出会四闸失明产假 pass,还经 real_pt
        # 把 meta 表没有的 PT 写进身份层;候选 SQL 本就 JOIN pt_meta,零召回损失)
        l1_llm, why = audit_l1_llm.rerank_ex(product, cands, ctx.pt_meta.keys())
        if l1_llm is None and why == "unknown" and cands:
            # 二次机会(所有者定稿 2026-08-14:"真的都不合适,那也不行")。
            # 七路候选**不空**但 LLM 全否掉了——多半是七路召回的方向本就偏,
            # 不是"这产品没类目"。这时把候选面换成两阶段开放判定(先选大类、
            # 再在该大类全部 PT 里挑)再判一次;还 unknown 才真 pending。
            # 只在 unknown 分支重试:LLM 失败/坏 JSON 是链路故障,换候选面
            # 治不了,重试只是白烧一次调用(兜底不补偿自己的不确定)。
            audit_l1_llm.bump("unknown_retry_called")
            wide = audit_l1_llm.open_candidates(conn, product)
            if wide:
                l1_llm, why = audit_l1_llm.rerank_ex(
                    product, wide, ctx.pt_meta.keys())
                if l1_llm is not None:
                    audit_l1_llm.bump("unknown_retry_saved")
        if l1_llm is not None:
            pt3 = l1_llm.walmart_product_type
            if pt3 and pt3 != "unknown" and pt3 not in ctx.pt_meta \
                    and not _blocked(l1_llm):
                logger.warning("L1 第三级产出 pt_meta 外 PT %r,转 pending "
                               "(asin=%s)", pt3, product.asin)   # 防御,与 resolve_pt 同款
            else:
                l1 = l1_llm
                meta = ctx.pt_meta.get(l1.walmart_product_type)
                if meta:
                    l1.walmart_category = meta.get("walmart_category")
    if not l1.walmart_product_type and not _blocked(l1):
        # PT 解不出(rerank unknown/LLM 失败/坏 JSON/无候选)→ pending,
        # 绝不默认放行(10.2)。excluded 命中(有扣分 hit)不走此路
        return AuditOutcome(asin=product.asin, verdict="pending",
                            score_final=None, stage_stopped_at="L1",
                            l1=l1, phase0=p0)

    l2 = audit_l2.evaluate(product, l1, ctx)
    verdict = l2.verdict
    # L2 也会产 pending(R1 查不到这个 PT 的准入事实 ⇒ 判不了,2026-08-20):
    # 与 reject 一样停在 L2,不再往下走 L3/L4 —— 类目都没定,语义与视觉判了也白判
    outcome = AuditOutcome(
        asin=product.asin, verdict=verdict, score_final=l2.score_final,
        stage_stopped_at="L2" if verdict in ("reject", "pending") else None,
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
        known_pt=row.get("walmart_pt") or None,
        known_pt_source=row.get("pt_source") or None,
        browse_node_id=row.get("browse_node_id") or "",
        browse_node_chain=row.get("browse_node_chain") or "",
    )
