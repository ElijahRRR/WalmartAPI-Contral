"""维护意图 provider(maintenance 工作流的可插拔数据源)。

意图 dict 统一形态(管道契约,provider 只管产出):
  {"store": 店铺名, "sku": SKU, "kind": "title"|"price"|"inventory"|"delete",
   "old": 旧值, "new": 新值}
  title 意图额外携带 product_type / product_id(UPC)两键(feed 载荷必需;
  按「三缺一跳过」旧防线过滤后再产出)。
清零是 inventory 的 new=0 特例,由 zero_intents 产出。
delete 是唯一的**不可逆**类(采集永久偏移 / 商品不存在),由 delete_intents
产出,old/new = 在线/删除,并带 reason 与 label(维护记录 C 列)。

**驱动方式的变化(2026-08-09,采集接入后)**:旧系统读飞书「在线产品总表」
的运营决策列(是否更新价格+新价 / 是否更新库存+新库存 / amz标题+相似度);
新系统的在线产品总表是程序写的投影,没有那些列——改为**从产品中心自动算**:
  amz 最新观测(catalog.latest_snapshot / products.slow)
    × 沃尔玛在线现值(catalog.walmart_items)
    × 定价规则(限额表倍率,services/pricing)
  → 差异超阈值才产出意图。人工要临时改某个 SKU 走 product_clear 同款的
  驱动表(尚未建;需要时再说),自动链不承担一次性人工指令。

路由铁律(所有者定稿 2026-08-07):意图产出必须 JOIN catalog.listing_sources
按出身路由——amz 快照驱动的意图只作用于 source_type='amz' 的行;
"源数据查不到"绝不可对 match/unknown 行推导出清库存/删除等破坏动作
(旧系统按 SKU 格式排除的补丁废止,以登记簿为准)。

路由阈值与标题载荷结构逐字移植旧系统(erpAPI 沃尔玛商品维护,实证勿改)。
"""

import logging
import os

from services import mp_mapper, order_audit, pricing, store_limits, \
    store_targets

logger = logging.getLogger("services.maintenance_intents")

# 单店该类型条数 ≤ 阈值走同步 PUT(结果当场已知),超过走 feed;
# 标题无同步接口永远 feed(旧 SYNC_THRESHOLDS 原值)
SYNC_THRESHOLDS = {"price": 5, "inventory": 10}

# M 列占位符:命中则跳过标题维护(否则 Walmart 退回;旧系统原值)
TITLE_PLACEHOLDERS = {"[商品不存在]"}

# 改价触发阈值:差额绝对值 ≥ 1 分且相对变化 ≥ 该比例才提交
# (亚马逊价格日内小幅抖动很常见,逐分钱跟会把 feed 配额烧光)
PRICE_MIN_DELTA = 0.01
PRICE_MIN_RATIO = 0.01          # 1%

# 单店单轮每类意图上限(所有者定稿 2026-08-26:「上限做成按店,数字对齐真实
# 配额」)。此前是全局 5000/类 —— 那道闸防的是"采集侧事故一次产出几万条",
# 但它分不清事故与**故意的**大面积调整:08-25 倍率调整日 12,766 条改价被
# 截成 5,000,谭总 7 家店拖了三天,且截断只进日志不见人。沃尔玛的配额全部
# **按店**计,全局闸只会卡住合法的大改,废除。
# 数字 =(该类 feed 的按店速率桶 **− 1**)× 单 feed 切片条数(api/_client.
# _RATE_BUCKETS × api/feeds._SLICE_LIMITS,连同窗口一致有测试钉住;改桶/改
# 切片先看那条测试)。**为什么 −1**:api/feeds 的"网络异常 → 双确认未达 →
# 同一载荷补交一次"路径每次补交**多烧一个桶名额**,切片数吃满桶时一次补交
# 就会在令牌桶上睡到窗口滑出(≈1 小时),整条链抱着 flock 陪等:
#   price     (8−1)/小时 × 8000 条/feed = 56000
#             (桶:官方 10/hour 三件套共享留余量,2026-08-26 复核后从 6/天
#             上调,6/day 只属本仓不用的 feedType=promo;切片:官方硬限
#             10000 条留两成,1000 只是建议值 —— 所有者定稿新鲜度优先,
#             单店当天扫出多少当轮连发多少,15000 条 = 8000+7000 连续提交)
#   inventory (8−1)/小时 × 4000 条/feed = 28000
#   title     (8−1)/小时 × 1000 条/feed = 7000
# 三个数都远大于单店在线目录(千级)——它们不再是吞吐规划,纯粹是失控护栏:
# 真咬合的那天多半是采集/配置事故在疯狂产单,而下面的截断优先级恰恰会
# **先放行事故本体**(错价越离谱越靠前、批量 NULL 清零全是 new=0),
# 所以看到首行"⚠ 截断"先查原因再放行,别急着调大上限。
# ⚠ 改价侧的数量兜底随全局闸一并消失(与库存侧"挡不住整店清零"同款代价):
# 倍率误填/区间事故会一轮全量出闸,自动闸仅剩 PRICE_MIN_DELTA/RATIO
# (下限闸,拦不住大偏差);涨跌幅闸在 plan.md 待办里,上量后需重议
# (此前全局 5000 至少把错价面封在 5000 行/天,现在没有了)。
# delete **不在表里**:破坏类的按店数量闸唯一在执行件(problem_product_cleanup
# 领取时 cap_destructive 按限额表「下架限制」截),扫描件如实报待办
# (2026-08-24 归一口径,扫描期再截一道就是"每店最多 N 实际 2N"的老坑)。
MAX_INTENTS_PER_STORE = {"price": 56000, "inventory": 28000, "title": 7000}

# 超上限时**截谁**(按店组内排序,升序取前 cap 条):
#   price     偏差比例大的先走 —— 错得越离谱的价越该当天纠;
#   inventory 清零(new=0)先走 —— "别卖错"优先于"补货上量";
#   title     停闸期低相似度同步(title_mismatch_sync)先走 —— 抄错标题
#             嫌疑最大的行最该当天见人。
# 三类都以 SKU 为第二键:产出序来自无 ORDER BY 的 SQL,会随 VACUUM/HOT
# 更新漂移,截断名单必须跨轮可预期(08-25 "随机截"的一半病根就在这)。
# ⚠ 这套优先级的职责是"正常拥挤时先做最要紧的",不是事故过滤器 ——
# 事故日它放行的正是事故本体(见上面护栏注释)。
_TRUNC_PRIORITY = {
    "price": lambda it: (-abs(it["new"] - it["old"])
                         / max(float(it["old"] or 0), 0.01),
                         str(it.get("sku") or "")),
    "inventory": lambda it: (0 if it.get("new") == 0 else 1,
                             str(it.get("sku") or "")),
    "title": lambda it: (0 if it.get("code") == "title_mismatch_sync" else 1,
                         str(it.get("sku") or "")),
}

# 删除类专属:批次数门槛与单店单轮上限
MIN_OFFSET_BATCHES = 1          # 出现一次即删(所有者:偏移了就不会恢复)
LONG_OOS_DAYS = 15              # 连续这么多天没有库存 → 删除(所有者定稿)

# ── 受管发货节点(多仓批次 2)────────────────────────────────────────────────
# 三个库存 provider 的比对基准从「全店合计」改成「受管仓现值」。**只改配置了
# 「维护仓库」的店**,其余店逐字节维持现状。
#
# 为什么必须改:`walmart_items.avail_qty` 是 GET /v3/inventories 的**全节点
# 合计**,而写只写一个节点。多节点店里 `合计 == 单仓目标值` 永远不成立 ⇒
# 每轮都判「有差异」⇒ 每轮全量重发(drop_recent 的 20h 窗口比日跑间隔还短,
# 压不住),而 settle 又永远判 ineffective —— 三条故障同一个根。
#
# ⚠ **配置店而节点明细还没扫到时不回落合计**:回落就是拿合计跟单仓目标比,
# 正是上面那条故障本身。这种行本轮**跳过并计数**(见 current_qty 返回 None)。
# 多参数 unnest 的写法与 dispositions._WITHDRAW_SQL 同款(record <> ALL 那种
# 写法 PG 直接报类型不匹配,别改回去)。
_MANAGED_NODE_JOIN = """
LEFT JOIN LATERAL (
    SELECT ni.avail_qty
    FROM unnest(%(mn_stores)s::text[], %(mn_nodes)s::text[]) AS mn(store, node)
    JOIN catalog.item_node_inventory ni
      ON ni.store = w.store AND ni.sku = w.sku AND ni.ship_node = mn.node
    WHERE mn.store = w.store
    LIMIT 1
) nq ON true
"""


def managed_params(managed: dict[str, str] | None) -> dict:
    """输入:{店铺: 受管仓} → 输出:_MANAGED_NODE_JOIN 要的两个平行数组参数。"""
    m = managed or {}
    return {"mn_stores": list(m), "mn_nodes": [m[k] for k in m]}


def current_qty(store: str, avail_qty, node_qty,
                managed: dict[str, str] | None) -> int | None:
    """输入:店铺 + 合计 + 受管仓值 + 配置表 → 输出:该行的"线上现值";未知 None。

    **三个库存 provider 的唯一比对基准出处**(散在各处写一遍就是本仓反复踩的
    "一条判据散在多处":改了其中一处,另外几处不报错、只是悄悄按旧规矩办事)。

      未配置店 → walmart_items.avail_qty(全店合计,现状,逐字节不变)
      配置店   → item_node_inventory 里受管仓那一行的 avail_qty
      配置店而受管仓**没有那一行** → **0**(见下),不是 None、更不是合计

    ⚠ 「受管仓没有这一行」= 该 SKU 在这个仓里就是没货,**0 才是它的真实现值**。
    2026-08-30 生产实测定案(谭总12 B008LUW4CI):节点行是**第一次写库存时
    创建的** —— 不需要先做 SKU×FC 关联(此前按"要先关联"推断编码,实测推翻)。
    于是"缺行就跳过"会造成**死锁**:永远不写 → 永远没有行 → 永远跳过,
    3663 条存量行一直卡着而日志只说"预期暂停"。

    ⚠ 那"不回落合计"的原则还算数吗?算数,而且这里没有违反它:回落合计是拿
    **别的节点的货**冒充受管仓的现值(多节点店永远判有差异 → 每轮重发);
    判 0 是如实陈述"这个仓里没有" —— 两回事。
    ⚠ "整店还没扫到"由**缺席避让**兜(store_absence:目录水位落后船队的店
    整店不产意图),不该由这条判据兼职 —— 一条判据只答一个问题。
    """
    if store in (managed or {}):
        return int(node_qty or 0)
    return int(avail_qty) if avail_qty is not None else None


_SQL_ZERO = """
SELECT w.store, w.sku, w.avail_qty, nq.avail_qty AS node_qty
FROM catalog.walmart_items w
""" + _MANAGED_NODE_JOIN + """
WHERE w.store = ANY(%(stores)s::text[]) AND w.missing_since IS NULL
"""
# ⚠ 「> 0」的判定**从 SQL 挪到 Python**(多仓批次 2):配置店要看的是受管仓
# 那个数,而它可能为 NULL(明细没扫到)——写在 SQL 里就得在 WHERE 里区分
# 「配置店 NULL = 跳过」与「未配置店 NULL = 未知不动」,两条 NULL 语义相反,
# 混在一个表达式里没人读得懂。判定见 current_qty。
# avail_qty > 0 是显式条件:旧系统 `None != 0` 也触发清零是坑(库存未知的行
# 被盲清)——新规矩:未知库存不动,只清确知有货的行。

# amz 侧最新观测 × 沃尔玛在线现值。三个 provider 共用一条取数(一次 JOIN,
# 各自挑字段),避免同一张大表扫三遍。
#   · 只取 source_type='amz'(路由铁律)
#   · 只取在架行(missing_since IS NULL)
#   · zip_verify='mismatch' 的观测不参与(请求邮编未生效,价格不属于该分组)
#   · stockzero 店整店排除(它们归 zero_intents,不能被自动同步顶回去)
_SQL_AMZ_JOIN = """
SELECT w.store, w.sku, w.product_name, w.product_type, w.upc,
       w.price AS wm_price, w.avail_qty, nq.avail_qty AS node_qty,
       s.price AS amz_price, s.stock_count, s.delivery_days,
       p.slow, s.fulfillment, s.shipping,
       -- 处置三信号(所有者定稿 2026-08-16):采集结局 / 亚马逊在架状态 /
       -- 契约的 fast.stock_state。三者取自**同一条最新快照**,不能各查各的
       -- ——分开查会出现"按昨天的 outcome 配今天的库存"这种错配。
       s.outcome, s.stock_status, s.stock_state
FROM catalog.walmart_items w
JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz'
JOIN catalog.products p ON p.marketplace = 'US' AND p.asin = w.sku
LEFT JOIN LATERAL (
    -- 配送方式(FBA/FBM)决定用哪套定价区间。采集契约的 fast 段没把它列成
    -- 一等字段,但 raw 是"裁剪后的原样载荷",is_fba 就在里面(采集侧
    -- worker/parser._parse_fulfillment 读 buybox 的 Ships from 行)。
    SELECT price, stock_count, delivery_days, shipping, outcome, stock_state,
           raw ->> 'is_fba' AS fulfillment,
           -- 亚马逊在架状态原文(采集侧存 raw,契约未列为一等字段)
           raw ->> 'stock_status' AS stock_status
    FROM catalog.latest_snapshot l
    WHERE l.marketplace = 'US' AND l.asin = w.sku
      AND coalesce(l.scrape_params ->> 'zip_verify', '') <> 'mismatch'
    ORDER BY l.scraped_at DESC LIMIT 1
) s ON true
""" + _MANAGED_NODE_JOIN + """
WHERE w.missing_since IS NULL
  AND w.published_status = 'PUBLISHED'
  AND NOT (w.store = ANY(%(stores)s::text[]))
"""


# 采集永久偏移(variant_offset)的在线行 → 删除意图。
# · 同样守路由铁律:只删 source_type='amz' 的行(破坏动作绝不碰 match/unknown)
# · 店铺 ACTIVE 才删(无 KPI 记录 fail-open,与其他闸同口径)
# · snapshots.outcome 在补列之前的历史行是 NULL,那些都是成功采集,按 ok 处理
#   ——否则老 SKU 会因为"查不到 ok 快照"被误判该删
_SQL_VARIANT_OFFSET = """
WITH vo AS (
    SELECT asin,
           count(DISTINCT batch_name) AS batches,
           min(recorded_at) AS first_seen,
           max(recorded_at) AS last_seen
    FROM ops.scrape_failures
    WHERE error_type = 'variant_offset'
    GROUP BY asin
), latest_status AS (
    SELECT DISTINCT ON (store) store, store_status
    FROM ops.store_kpi_daily ORDER BY store, data_date DESC
)
SELECT w.store, w.sku, vo.batches, vo.first_seen, vo.last_seen
FROM vo
JOIN catalog.walmart_items w ON w.sku = vo.asin
JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz'
LEFT JOIN latest_status s ON s.store = w.store
WHERE w.missing_since IS NULL
  AND w.published_status = 'PUBLISHED'
  AND (s.store_status IS NULL OR upper(s.store_status) = 'ACTIVE')
  AND vo.batches >= %(min_batches)s
  AND NOT EXISTS (
        SELECT 1 FROM catalog.snapshots sn
        WHERE sn.asin = vo.asin
          AND COALESCE(sn.outcome, 'ok') = 'ok'
          AND sn.scraped_at > vo.last_seen)
ORDER BY w.store, w.sku
"""


# 连续 N 天**本店渠道下**没有库存 → 删除(所有者定稿 2026-08-09;
# 渠道那一维 2026-08-25 加)。三道判据,缺一个都会误删:
#   1. 窗口内**没有任何一条"本店卖得了"的观测**(有货 ∧ 渠道不是确定的另一个)
#   2. **至少有一条确定卖不了的观测**——明确缺货,或明确是另一个渠道。
#      防"这 15 天页面一直采不全、全是 unknown"被读成"一直缺货"(采不到 ≠ 没货,
#      这是删除,不是清零,不能含糊)
#   3. 窗口**两端都有观测**(最早一条在窗口起点 36 小时内、最新一条在近 3 天内)
#      ——防"15 天前采过一次、昨天采过一次,中间断 13 天"被当成连续观测
# 只看 outcome=ok 的观测(降级采集的 fast 段基本是空的,拿它判缺货是冤案)。
#
# ★ **渠道这一维怎么进来的**(所有者定稿 2026-08-25:「该渠道下库存连续不足
# N 天,下架」):限定了渠道的店,货源翻到另一个渠道 = 这家店卖不了,与缺货
# **同一条阶梯** —— 库存 provider 先清零(可逆),窗口熬满 N 天才删(不可逆)。
# 两个必须钉死的方向,写反了都不报错:
#   · 渠道**采不到 / 采出第三种值** ⇒ 算"卖得了",**挡住删除**。把未知当成
#     "渠道不对"会因为采集侧 is_fba 解析坏掉而整批删货 —— 那时该修采集。
#   · 店**没标**「配送限制」⇒ want='',两个渠道条件恒假,判据逐字退回旧口径
#     (`sellable_obs = in_stock_obs`、`wrong_ch_obs = 0`)。没标的店行为**一个
#     字都不变**,这是所有者"没标就都能上"在删除侧的对应面。
# ⚠ 窗口是**向后看**的:某店今天刚把「配送限制」填上,窗口立刻看见过去 15 天
# 的另一渠道历史 ⇒ 当轮就能给出整批删除建议。破坏动作有两道人闸接着
# (建议行落 ops.dispositions 给人看 + 执行件按「下架限制」逐店截断),
# 但填这一列之前先跑一次 `maintenance_scan --dry-run` 看删除名单。
_SQL_LONG_OOS = """
WITH req AS (
    -- 店铺渠道要求。**没标的店不在这张表里** ⇒ 下面 LEFT JOIN 出 NULL ⇒
    -- coalesce 成 '' ⇒ 所有渠道条件恒假 ⇒ 判据退回旧口径
    SELECT store, upper(btrim(want)) AS want
    FROM unnest(%(ch_stores)s::text[], %(ch_wants)s::text[]) AS t(store, want)
), live AS (
    SELECT w.store, w.sku, coalesce(req.want, '') AS want
    FROM catalog.walmart_items w
    JOIN catalog.listing_sources ls
      ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'amz'
    LEFT JOIN (SELECT DISTINCT ON (store) store, store_status
               FROM ops.store_kpi_daily
               ORDER BY store, data_date DESC) st ON st.store = w.store
    LEFT JOIN req ON req.store = w.store
    WHERE w.missing_since IS NULL
      AND w.published_status = 'PUBLISHED'
      AND (st.store_status IS NULL OR upper(st.store_status) = 'ACTIVE')
), obs AS (
    -- ⚠ 三个派生值全部 coalesce 成**二值**:三值逻辑下 `NOT (… AND NULL)` 是
    -- NULL,而 FILTER 把 NULL 当不命中 —— 渠道采不到的那批观测会因此从
    -- "挡住删除"翻成"不挡",方向正好反了,且一个字的报错都没有
    SELECT sn.asin, sn.scraped_at,
           coalesce(sn.stock_count > 0
                    OR sn.stock_state = 'in_stock', false) AS has_stock,
           coalesce(sn.stock_state = 'out_of_stock'
                    OR sn.stock_count = 0, false) AS no_stock,
           coalesce(upper(btrim(sn.raw ->> 'is_fba')), '') AS channel
    FROM catalog.snapshots sn
    WHERE sn.scraped_at > now() - make_interval(days => %(days)s)
      AND COALESCE(sn.outcome, 'ok') = 'ok'
), win AS (
    SELECT live.store, live.sku, live.want,
           min(o.scraped_at) AS first_seen,
           max(o.scraped_at) AS last_seen,
           count(*) AS obs,
           -- 本店卖得了的观测:有货,且**不是确定的另一个渠道**
           count(*) FILTER (
               WHERE o.has_stock
                 AND NOT (live.want <> ''
                          AND o.channel IN ('FBA', 'FBM')
                          AND o.channel <> live.want)) AS sellable_obs,
           count(*) FILTER (WHERE o.no_stock) AS oos_obs,
           -- 确定是另一个渠道的"有货"观测:它既不算卖得了,又是一条**明确**的
           -- 卖不了证据(判据 2 认它,等价于缺货观测)
           count(*) FILTER (
               WHERE o.has_stock AND live.want <> ''
                 AND o.channel IN ('FBA', 'FBM')
                 AND o.channel <> live.want) AS wrong_ch_obs
    FROM live JOIN obs o ON o.asin = live.sku
    GROUP BY live.store, live.sku, live.want
)
SELECT store, sku, obs, first_seen, last_seen, wrong_ch_obs, want
FROM win
WHERE sellable_obs = 0
  AND oos_obs + wrong_ch_obs > 0
  AND first_seen <= now() - make_interval(days => %(days)s) + interval '36 hours'
  AND last_seen >= now() - interval '3 days'
ORDER BY store, sku
"""


# ── 处置判据(所有者定稿 2026-08-16 走进生产)────────────────────────────────
# **全项目唯一一处**决定"这个在线商品该拿它怎么办"。四个 provider 都问它,
# 不各判各的 —— 分开写迟早飘成"删除链按 A 判、库存链按 B 判"而两边都不报错。

TITLE_SIM_FLOOR = 0.70      # 相似度低于此 → 删除;不低于 → 改标题
# 2026-08-19 所有者:「暂时关闭'删除(title_mismatch)'这个维护」。08-17/18
# 两轮该原因的删除建议超两千条、占删除九成,而删除不可逆——先停闸观察
# (采集标题质量/占位符干扰的嫌疑未排除)。恢复改回 True。
#
# ⚠ **2026-08-20 所有者修正停闸期口径**:「删除(title_mismatch)已停闸,
# 那么就要对这批行同时改价改标题改库存」。停闸头一版顺手把这批行**冻结**了
# (删除不发,价格/标题/库存也一律不动),那是最坏的一种状态:删除出口关着
# 的时候冻结等于**没人管** —— 亚马逊涨价了不跟、缺货了不清零、标题错着不改,
# 两百多条在架商品挂着错价错标题过夜,而日志只说一句"压制 N 条"。
# 停闸的本意是"先别删",不是"先别管"。所以闸**只关删除这一个出口**:
#   · 删除                → 不产出(本开关关着时)
#   · 改价/改库存/改标题  → **照常产出**(与其余在架行同口径)
# 判据本身一个字没动:开关改回 True 立刻回到"低相似度 → 删除、且不改价不改
# 标题"的停闸前行为(两侧都有用例钉住)。
# ⚠ 停闸期改标题 = 把亚马逊标题抄到一个"可能不是同一个商品"的在架行上,
# 这是所有者知情后的取舍(标题改错可再改回,删除不可逆),但**必须见人**:
# title_intents 单独计数并 warning,不许它混进"标题 N 条"里无声发生。
TITLE_MISMATCH_DELETE = False

def processed_title(slow) -> str:
    """输入:products.slow → 输出:过完上架文案处理的标题(去品牌/去符号/截 199)。

    ⚠ **相似度必须拿它去比,不能比亚马逊原始标题**:`force_amazon_copy` 会去掉
    品牌名,我们的沃尔玛标题天生就是"亚马逊标题减品牌"。拿原始标题比,品牌一长
    (`SuperMegaBrandName Cup` → `Cup`)相似度就掉到 24%,一个完全正常的商品
    会被判成"不是同一个东西"而删除。两边过同一套处理,剩下的差异才是真差异。
    """
    if not isinstance(slow, dict):
        return ""
    t = slow.get("title")
    if not t or str(t).strip() in TITLE_PLACEHOLDERS:
        return ""
    return mp_mapper.force_amazon_copy(
        {}, {"title": t, "brand": slow.get("brand"), "attrs": slow}
    ).get("productName") or ""


def main_processed_title(slow) -> str:
    """输入:products.slow → 输出:**主标题**过完同一套文案处理;拆不出返回 ""。

    2026-08 亚马逊把标题拆两段,采集侧按 " | " 拼回 `slow.title` 并把后半段
    单独放 `slow.subtitle`(采集契约 §slow.subtitle)。这里做**精确逆操作**:
    removesuffix(" | " + subtitle)——不是按 "|" 猜切(契约明令禁止:标题
    正文本来就可能含 |),结尾对不上(改版前老记录 subtitle 为 null)
    一律返回 "",调用方退回单基准。
    """
    if not isinstance(slow, dict):
        return ""
    t, sub = str(slow.get("title") or ""), str(slow.get("subtitle") or "")
    if not t or not sub or str(t).strip() in TITLE_PLACEHOLDERS:
        return ""
    tail = " | " + sub
    if not t.endswith(tail) or not t[: -len(tail)].strip():
        return ""
    return mp_mapper.force_amazon_copy(
        {}, {"title": t[: -len(tail)], "brand": slow.get("brand"),
             "attrs": slow}
    ).get("productName") or ""


def title_sim_dual(wm_name, slow):
    """输入:沃尔玛在线商品名 + products.slow → 输出:双基准相似度(None=算不了)。

    基准一 = 完整拼接标题;基准二 = 主标题(subtitle 拆得出时)。取 **max**:
    「像两种形态里的任何一种都算像」(所有者定稿 2026-08-19)。为什么:
    上架默认口径是长标题,但**在架存量里有一批是主标题(短标题)上的**
    ——单基准下这些行相似度只有 ~0.6,title_mismatch 会把它们当成串货删掉。
    ⚠ 当初造这批短标题行的「内容拒捞回」通道已于 2026-08-23 撤除,但**行还在线**,
    所以双基准不能跟着撤;将来整体切短标题口径也天然兼容,不用再改比对。
    """
    sims = [order_audit.title_similarity(wm_name, processed_title(slow))]
    mt = main_processed_title(slow)
    if mt:
        sims.append(order_audit.title_similarity(wm_name, mt))
    real = [s for s in sims if s is not None]
    return max(real) if real else None


# 无货三档的映射搬去 `order_audit.stock_block`(维护链与审核链的唯一出处);
# 本模块已经 import order_audit,方向不变。

def title_mismatched(title_similarity) -> bool:
    """输入:相似度(可为 None)→ 输出:是否算「标题不匹配」。

    **唯一定义处**:classify 用它决定删不删,delete_intents 用它数停闸压了
    多少条,title_intents 用它决定(停闸恢复后)跳不跳过。三处各写一遍
    `x is not None and x < FLOOR` 的下场是改阈值时漏改一处,而且不报错。
    None 不算不匹配:算不出来 ≠ 不像(有一边没标题是采集问题,不是证据)。
    """
    return title_similarity is not None and title_similarity < TITLE_SIM_FLOOR


def classify(*, outcome=None, stock_status=None, stock_state=None,
             title_similarity=None, over_lead=False, lead_note="",
             channel_bad=False, channel_note="") -> tuple:
    """输入:一行在线商品的处置信号 → 输出:(动作, 原因码, 原因文案);无动作返回 (None,'','')。

    所有者定稿的判据,逐条对应他给的伪代码:

      outcome == 'not_found'                     → 删除(ASIN 已从亚马逊下架)
      **本店渠道 ≠ 产品渠道**                     → 库存 0(所有者定稿 2026-08-25)
      stock_status == 'Currently unavailable'    → 库存 0(在架但不可售,拿不到价格库存)
      stock_status == 'No Featured Offer'        → 库存 0(无 Buy Box)
      stock_state == 'out_of_stock'              → 库存 0(普通缺货)
      配送超本店上限                              → 库存 0
      标题相似度 < 0.70                           → 删除(**停闸中:见下**)
      标题相似度 ≥ 0.70 且标题有差异              → 改标题

    ⚠ **渠道不符排在无货三档之前**(2026-08-25):两条都命中时动作一样(清零),
    差别只在原因码 —— 而这两个原因的处置完全不同。「缺货」是暂时的、等回货;
    「渠道不符」是结构性的,回了货也卖不了(这家店只做另一个渠道),运营要做的
    是换店或改店铺配置。排在后面就会被「缺货」盖住,而那一栏天天都有几百条。

    ⚠ `TITLE_MISMATCH_DELETE=False`(当前现状)时,"标题相似度 < 0.70 → 删除"
    这一条**整条跳过**:不返回删除,继续往下判库存,该行照常改价/改标题/改
    库存(停闸口径见 `TITLE_MISMATCH_DELETE` 头注)。

    ⚠ 顺序即优先级,**删除压过一切**:一个 SKU 一轮只出一个动作,否则执行件
    会先花配额去改一个马上要删的商品(批次 E 踩过同款坑:同一 SKU 既建议反补
    又建议删除,先花配额救活再花配额删掉)。

    ⚠ 相似度 None(有一边根本没标题)**不算不匹配**:算不出来 ≠ 不像。
    走到这一步说明标题缺失,那是采集问题,不该拿它当删除依据。
    """
    if str(outcome or "").strip().lower() == "not_found":
        return ("delete", "not_found", "亚马逊已下架(采集 not_found)")
    if title_mismatched(title_similarity):
        if TITLE_MISMATCH_DELETE:
            return ("delete", "title_mismatch",
                    f"标题相似度 {title_similarity:.0%} < {TITLE_SIM_FLOOR:.0%}")
        # 停闸中:**不判删除,也不早退**——早退回 (None,"","") 是另一种冻结,
        # 缺货了也不清零,等于删除停闸顺手把库存链也关了。继续往下判无货三档
        # 与货期(停闸口径见 `TITLE_MISMATCH_DELETE` 头注)。
    if channel_bad:
        # 本店只做一个渠道,而这个货现在是另一个渠道 ⇒ 在这家店卖不了。
        # **清零不删除**:清零可逆(货源渠道翻回来自动回补),删除不可逆。
        # 真要下架由删除链的「渠道不符 N 天」窗口收尾 —— 与"缺货清零 → 连续
        # 无货 N 天才删"完全同一条阶梯(所有者定稿 2026-08-25)。
        return ("inventory", "channel_mismatch",
                channel_note or "本店渠道与产品渠道不符")
    # 无货三档的判据是**审核链共用的**(order_audit.stock_block 是唯一出处):
    # 维护链据此把库存写 0,审核链据此报「无货」而不是「采集缺字段」。
    # 各写一份的下场是同一个商品两条链说两种话,而且两边都不报错。
    hit = order_audit.stock_block(stock_status, stock_state)
    if hit:
        return ("inventory", hit[0], hit[1])
    if over_lead:
        return ("inventory", "lead_days", lead_note or "配送时长超本店上限")
    return (None, "", "")


def _rows(conn, stockzero_stores: list[str],
          managed: dict[str, str] | None = None) -> list[dict]:
    """输入:连接 + stockzero 名单 → 输出:在线商品行(**dict,不是元组**)。

    ⚠ 2026-08-16 从元组改成 dict:SQL 加了 outcome/stock_status/stock_state 三列,
    四个 provider 的位置解包**全部要改**,漏一处轻则 ValueError、重则静默取错
    字段(元组长度对得上、字段错位时不报错)。按名字取之后,加列只改 SQL。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_AMZ_JOIN,
                    {"stores": list(stockzero_stores or []),
                     **managed_params(managed)})
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


_DEDUPE_SCOPE = "maintenance:submitted"
SUPPRESS_HOURS = 20     # 同 (店铺,SKU,类型,新值) 多久内不重复提交


def _suppress_key(it: dict) -> str:
    """输入:意图 → 输出:防重键(store|sku|kind|new[|node])。

    ⚠ **受管仓的意图要带 node**(多仓批次 2 补,2026-08-30 搬仓时发现):
    昨天走 legacy 写到 Virtual 的意图,与今天要写到受管仓的同名意图,
    店铺/SKU/类型/新值**全都一样** —— 不带节点就会被当成"同一件事再做一遍"
    而压掉,受管仓于是收不到这一笔。平时只是晚一轮(20h 窗口),但搬仓那天
    "充受管仓 → 清旧节点"紧挨着做,被压掉的那批两个节点都是 0 = **直接不可售**。

    ⚠ 未配置店**一个字节都不加**(不是 `|` 空段):加了会让 ops.dedupe 里
    全部存量键失配 ⇒ 全店一轮重发,正是这道闸当初要防的 208 条 stale update。
    """
    base = f"{it['store']}|{it['sku']}|{it['kind']}|{it.get('new')}"
    node = it.get("ship_node")
    return f"{base}|{node}" if node else base


def drop_recent(conn, intents: list[dict],
                hours: int = SUPPRESS_HOURS) -> tuple[list[dict], int]:
    """输入:连接 + 意图 → 输出:(去掉近期已提交过的, 被压掉的条数)。

    **为什么必须有这道闸**(2026-08-09 生产实证:208 条 ERR_EXT_DATA_0101198
    "stale update request"):provider 比的是 amz 值 vs `catalog.walmart_items`
    的**上次扫店快照**。提交成功后本地快照不变(要等 catalog_sync 再扫一遍),
    下一轮同样的差异又算出来 → 重发一模一样的载荷 → 烧配额且被沃尔玛判重。

    键含**新值**:值真变了(amz 又调价了)照样能提交,压的只是"同一件事
    再做一遍"。窗口 20 小时(维护日跑一轮,留 4 小时余量)。
    """
    if not intents:
        return intents, 0
    keys = [_suppress_key(i) for i in intents]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT key FROM ops.dedupe WHERE scope = %s AND key = ANY(%s)"
            " AND created_at > now() - make_interval(hours => %s)",
            (_DEDUPE_SCOPE, keys, int(hours)))
        recent = {r[0] for r in cur.fetchall()}
    if not recent:
        return intents, 0
    kept = [i for i, k in zip(intents, keys) if k not in recent]
    logger.info("近 %d 小时内已提交过的意图压掉 %d 条(防 stale update)",
                hours, len(intents) - len(kept))
    return kept, len(intents) - len(kept)


def record_submitted(conn, intents: list[dict]) -> int:
    """输入:连接 + 已提交的意图 → 输出:记账条数(供 drop_recent 抑制)。"""
    if not intents:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ops.dedupe (scope, key, meta) VALUES (%s,%s,%s::jsonb)"
            " ON CONFLICT (scope, key) DO UPDATE SET created_at = now()",
            [(_DEDUPE_SCOPE, _suppress_key(i), None) for i in intents])
    return len(intents)


def cap_per_store(intents: list[dict]) -> tuple[list[dict], list[dict]]:
    """输入:意图列表 → 输出:(截到单店单轮上限的列表, 截断报告)。

    按 (店铺, 类型) 分组截断,组内先按 _TRUNC_PRIORITY 排序再取前 cap 条
    (排序只在**真要截**的组里发生,不超限的组一个字不动 —— 平日零成本)。
    截断报告一行一个被截的组:{"store","kind","total","kept","deferred_keys"},
    调用方必须做两件事:
      ① 把它带进运行摘要**首行**(链通知对成功步骤只发首行)—— 08-25 的
        教训是截断只写日志 warning,通知看起来一切正常;
      ② 把 deferred_keys 留在 withdraw_stale 的 keep 里 —— 配额顺延不是
        "不再建议",撤掉落榜行上一轮挂着的 suggested 行会被记成
        「商品自己恢复正常了」(错误取证),且下轮又重建。
    """
    by: dict[tuple, list[dict]] = {}
    for it in intents:
        by.setdefault((it["store"], it["kind"]), []).append(it)
    out, report = [], []
    for (store, kind), group in by.items():
        cap = MAX_INTENTS_PER_STORE.get(kind)
        if cap is None or len(group) <= cap:
            out.extend(group)
            continue
        key = _TRUNC_PRIORITY.get(kind)
        ranked = sorted(group, key=key) if key else group   # sorted 稳定
        out.extend(ranked[:cap])
        report.append({"store": store, "kind": kind,
                       "total": len(group), "kept": cap,
                       "deferred_keys": [(i["store"], i["sku"], i["kind"])
                                         for i in ranked[cap:]]})
        logger.warning("%s %s 意图 %d 条超单店单轮上限 %d,本轮只提交 %d 条"
                       "(按截断优先级取,其余下轮)",
                       store, kind, len(group), cap, cap)
    return out, report


def zero_intents(conn, stockzero_stores: list[str],
                 managed: dict[str, str] | None = None) -> list[dict]:
    """输入:连接 + stockzero 店名单(+受管仓表)→ 输出:整店清零意图(→ 0)。

    ⚠ 多仓下清的是**受管仓**(所有者定稿:自动链只管自建自发货仓)。按合计选
    行、只清一个节点的话,合计永远 > 0 ⇒ 每轮重选重清,而"这家店停售"从未真
    达成 —— 摘要还一直显示"清零 N 条"。这是多仓故障清单里唯一后果是钱在漏的。
    """
    if not stockzero_stores:
        return []
    with conn.cursor() as cur:
        cur.execute(_SQL_ZERO, {"stores": list(stockzero_stores),
                                **managed_params(managed)})
        rows = cur.fetchall()
    out = []
    for store, sku, avail, node_q in rows:
        cur_q = current_qty(store, avail, node_q, managed)
        if not cur_q or cur_q <= 0:     # None(未知/明细没扫到)与 0 都不动
            continue
        out.append({"store": store, "sku": sku, "kind": "inventory",
                    "old": cur_q, "new": 0, "code": "stockzero",
                    "reason": "整店清零(限额表「库存特殊要求」=0)",
                    **_node_of(store, managed)})
    return out


def _node_of(store: str, managed: dict[str, str] | None) -> dict:
    """输入:店铺 + 受管仓表 → 输出:{"ship_node": ...} 或 {}(未配置店)。

    未配置店**不带这个键** —— 建议行的 detail 与改造前逐字节一致,
    执行件也就走原来那条 legacy 路径,行为零变化。
    """
    node = (managed or {}).get(store)
    return {"ship_node": node} if node else {}


# 跟卖品铺货量:跟卖无 amz 侧库存可跟,固定保守值(与 AMZ_IN_STOCK_QTY
# 同源口径,所有者拍板 2026-08-12 终值 10)
MATCH_INVENTORY_QTY = int(os.environ.get("MATCH_INVENTORY_QTY", "10"))

# 跟卖品在架且库存为 0/未知 → 铺货。不要求 published_status='PUBLISHED':
# 0 库存本身就可能导致 UNPUBLISHED(reason=Inventory),要求已发布会死锁
# (没库存→不发布→不给库存)。RETIRED/ARCHIVED 不碰。
_SQL_MATCH_INV = """
SELECT w.store, w.sku, w.avail_qty, nq.avail_qty AS node_qty
FROM catalog.walmart_items w
JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku AND ls.source_type = 'match'
""" + _MANAGED_NODE_JOIN + """
WHERE w.missing_since IS NULL
  AND coalesce(upper(w.lifecycle_status), 'ACTIVE') = 'ACTIVE'
  AND NOT (w.store = ANY(%(stores)s::text[]))
"""
# ⚠ 「库存为 0/未知」的判定同样挪到 Python(理由见 _SQL_ZERO 那处注释)。
# 多仓下这条尤其要紧:受管仓为 0 而别的节点有货时,合计非 0 会让**该铺的行
# 选不出来、永远不铺**,且完全静默。


def match_inventory_intents(conn, stockzero_stores: list[str] | None = None,
                            managed: dict[str, str] | None = None) -> list[dict]:
    """输入:连接 + stockzero 名单 → 输出:跟卖品铺货意图(0/未知 → 保守值)。

    补结构洞(所有者批复 2026-08-12):跟卖 offer 建成即 0 库存不可售,
    而 amz 驱动的 inventory_intents 按路由铁律永远排除 source_type='match'
    ——旧系统同病(inventory_push 因 --no-poll 从未真跑)。本 provider 是
    跟卖库存的**唯一**实现路径:新 offer 进目录(catalog_sync)后自动铺货,
    stockzero 店解除后也自动回补(修清零/回补不对称)。
    ⚠ 手动把个别跟卖品清零会被本 provider 回填——单品停售走停用/删除流程,
    整店停售走 stockzero,清零不是停售手段。
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_MATCH_INV, {"stores": list(stockzero_stores or []),
                                     **managed_params(managed)})
        rows = cur.fetchall()
    out = []
    for store, sku, avail, node_q in rows:
        # 配置店缺节点行 = 受管仓里没货 = 该铺(current_qty 判 0)。
        # ⚠ 这会让受管仓与旧节点**同时有货**:旧节点的存量归人工清理,
        # 自动链按定稿只碰受管仓(见 docs/multi_node_plan.md §6 末段)。
        cur_q = current_qty(store, avail, node_q, managed)
        if cur_q:                       # 只铺 0/未知;None(未知)照旧算要铺
            continue
        out.append({"store": store, "sku": sku, "kind": "inventory",
                    "old": cur_q, "new": MATCH_INVENTORY_QTY,
                    "code": "match_restock",
                    "reason": f"跟卖品铺货(库存 0/未知 → {MATCH_INVENTORY_QTY})",
                    **_node_of(store, managed)})
    return out


def price_intents(conn, multipliers: dict[str, dict],
                  stockzero_stores: list[str] | None = None) -> list[dict]:
    """输入:连接 + {店铺: 限额表倍率行} + stockzero 名单 → 输出:改价意图。

    新价 = services.pricing.walmart_price(**落地价(amz 现价 + 运费)** × 该店
    对应区间倍率),
    与上架用的是**同一套定价规则**(避免上架价与维护价两套口径)。
    区间按**配送方式**分两套(FBA 0-30/30-1000、FBM 15-80/80-1000),配送方式
    取 latest_snapshot 的 raw.is_fba(采集侧 parser 读 buybox 的 Ships from);
    **未知则不改价**——猜错一档就是拿错倍率改线上价。
    出界按 300% 兜底(所有者定稿 2026-08-09);只有**区间内倍率未配置**
    才不产出(不是改成 0,是不动)。
    差异需同时满足 PRICE_MIN_DELTA 与 PRICE_MIN_RATIO 才提交。
    """
    out = []
    skipped_no_rule = skipped_no_channel = skipped_no_shipping = 0
    for r in _rows(conn, stockzero_stores):
        store, sku = r["store"], r["sku"]
        wm_price, amz_price = r["wm_price"], r["amz_price"]
        fulfillment, shipping = r["fulfillment"], r["shipping"]
        # 删除类压过改价(序见 dispositions.ACTION_RANK):两条删除判据都要判到。
        # ⚠ title_mismatch 停闸期间 classify 不再判它删,这批行于是照常改价
        # (口径见 `TITLE_MISMATCH_DELETE` 头注)。这里**不要**另加相似度
        # 判断:那就成了"删除关了、改价还自己关着"的第二把暗闸。
        if classify(outcome=r["outcome"],
                    title_similarity=title_sim_dual(
                        r["product_name"], r["slow"]))[0] == "delete":
            continue
        if amz_price is None or wm_price is None:
            continue                    # 缺任一侧现值:没有可比基准,不动
        channel = str(fulfillment or "").strip().upper()
        if channel not in pricing.PRICE_BANDS:
            # 配送方式未知 → **不改价**(所有者 2026-08-09:这是必须要获取的
            # 信息)。FBA/FBM 两套区间边界不同,猜错一档就是拿错倍率改线上价,
            # 比不改危险得多。
            skipped_no_channel += 1
            continue
        if shipping is None:
            # 运费没采到 ⇒ 落地价算不出来。与"配送方式未知不改价"同一个道理:
            # 当 0 定出来的价偏低,越贵的运费亏得越多,而两侧都不报错
            skipped_no_shipping += 1
            continue
        new_price = pricing.walmart_price(channel, amz_price,
                                          multipliers.get(store, {}), shipping)
        if new_price is None:
            skipped_no_rule += 1
            continue
        old = float(wm_price)
        delta = abs(new_price - old)
        if delta < PRICE_MIN_DELTA or (old > 0 and delta / old < PRICE_MIN_RATIO):
            continue
        out.append({"store": store, "sku": sku, "kind": "price",
                    "old": old, "new": new_price, "code": "price_sync",
                    "reason": f"{channel} 落地价 × 区间倍率 → "
                              f"{old:.2f}→{new_price:.2f}"})
    if skipped_no_rule:
        logger.info("改价:%d 行因该区间倍率未配置跳过(不动,非改 0)",
                    skipped_no_rule)
    if skipped_no_channel:
        logger.warning("改价:%d 行配送方式(FBA/FBM)未知,本轮不改价"
                       "——采集侧 raw.is_fba 缺失或该 ASIN 尚未重采",
                       skipped_no_channel)
    if skipped_no_shipping:
        logger.warning("改价:%d 行运费未采到(采集侧 N/A),落地价算不出来,"
                       "本轮不改价——**绝不当免运费处理**", skipped_no_shipping)
    return out


def inventory_intents(conn, stockzero_stores: list[str] | None = None,
                      managed: dict[str, str] | None = None,
                      store_channels: dict[str, str] | None = None
                      ) -> list[dict]:
    """输入:连接 + stockzero 名单(+ {店铺: 限定渠道})→ 输出:改库存意图。

    `store_channels` = 限额表「配送限制」标了 fba/fbm 的店(唯一取数口
    `store_targets.store_channels()`,由 `collect_all` 取一次分发)。
    **不传 = 不限制**:直接调本函数的测试与排查不会因此静默拿到一份 Feishu 读。

    库存决策(所有者定稿 2026-08-09;渠道那条 2026-08-25 加):
      · **本店渠道 ≠ 产品渠道 → 写 0**。清零可逆、删除不可逆,所以这里只清零;
        真下架交给删除链的「渠道不符 N 天」窗口(与缺货走同一条阶梯)
      · stock_count 有值 → 同步该值(0 就是 0)
      · **stock_count 为 NULL(没采到)→ 也写 0**。采不到就不卖,是运营口径;
        库里 NULL 与 0 仍然分得清(catalog.snapshots 原样存),只在决策这一层
        把"不知道"当成"别卖"。
      · 配送超上限 → 写 0。上限取限额表**本店**「配送时长限制」
        (`lead_limit`,与分配链共用同一列同一常量);该店没填就回落全局
        `MAX_LEAD_DAYS`(**7 天**,所有者两次收紧 12 →08-09→ 8 →08-15→ 7)。
        ⚠ 与上架侧口径不同:上架侧超限是**不上架**,这里已经在架了只能压库存
    ⚠ 血量提醒:采集服务中断一整轮会让大批行的 stock_count 变 NULL,
    按本规则即全线清零。2026-08-26 起单轮上限改按店(所有者定稿:及时性
    优先,配额是按店的,全局闸卡的全是合法大改),**这道闸挡不住整店清零**
    (单店目录远小于单店上限)—— 防线只剩两道:扫描摘要里的「清零合计
    (按原因码摊开)」必须有人看;AI 改完代码先 --dry-run 的纪律没有取消。
    """
    from services import amz_source
    lead_caps = store_limits.lead_day_caps()
    chans = store_channels or {}
    out = []
    n_channel = n_zeroed = 0
    for r in _rows(conn, stockzero_stores, managed):
        store, sku = r["store"], r["sku"]
        # 比对基准 = 受管仓现值(配置店)/ 全店合计(未配置店),唯一出处
        avail_qty = current_qty(store, r["avail_qty"], r.get("node_qty"), managed)
        stock_count = r["stock_count"]
        if stock_count is None:
            stock_count = 0             # 没采到 → 不卖(所有者定稿)
        cap = store_limits.cap_for(lead_caps, store, amz_source.MAX_LEAD_DAYS)
        over = store_limits.over_lead_cap(r["delivery_days"], cap)
        # 渠道判定走 store_targets 唯一谓词(上架/分配/对账同一处):店没标
        # 不算不符,产品渠道采不到或采出第三种值也不算 —— 把"没采到"当"货不对"
        # 在这条链上的后果是无辜商品先被清零、再被删除链的窗口删掉
        want_ch = chans.get(store)
        bad_ch = store_targets.channel_conflict(want_ch, r["fulfillment"])
        n_channel += 1 if bad_ch else 0
        # 处置判据集中在 classify():四条清零判据(不可售/无 BuyBox/缺货/超时)
        # 各自带原因码,飞书「原因」列靠它才分得清 —— 表里四条长得一模一样
        act, code, why = classify(
            outcome=r["outcome"], stock_status=r["stock_status"],
            stock_state=r["stock_state"], over_lead=over,
            # ⚠ 相似度也要给:不给的话"标题不匹配 → 删除"这条在本 provider 看不见,
            # 于是该删的行照样产一条库存意图,执行件先花配额清零再花配额删
            # (2026-08-16 演练实见 B0MISMATCH 10 → 7)。
            # ⚠ 用 title_sim_dual,与删除/改价/改标题**同一个算法**(2026-08-20
            # 对齐):单基准把"在架的短标题存量行"算成 ~0.6,停闸恢复后
            # 本 provider 会判它该删而删除链(双基准)不删 —— 那批行既不清零
            # 也不删,悄悄脱管。四个 provider 问同一个判据,就得喂同样的数。
            title_similarity=title_sim_dual(r["product_name"], r["slow"]),
            lead_note=f"配送 {r['delivery_days']} 天 > 本店上限 {cap} 天",
            channel_bad=bad_ch,
            channel_note=(f"本店只做 {want_ch},该品现在是 "
                          f"{str(r['fulfillment']).strip().upper()}")
                         if bad_ch else "")
        if act == "delete":
            continue        # 删除类归 delete_intents,这里不抢
        new_qty = 0 if act == "inventory" else int(stock_count)
        if avail_qty is not None and int(avail_qty) == new_qty:
            continue
        out.append({"store": store, "sku": sku, "kind": "inventory",
                    "old": avail_qty, "new": new_qty,
                    "code": code, "reason": why,
                    **_node_of(store, managed)})
        n_zeroed += 1 if code == "channel_mismatch" else 0
    if n_channel:
        # 渠道不符必须出声:它是结构性的(不像缺货会自己好),而且这批行
        # 会顺着删除链的窗口走到不可逆的删除 —— 一次大面积出现,多半是
        # 某家店的「配送限制」刚填上/刚改了,人要先知道再决定要不要放它跑
        # ⚠ 两个数分开报,别拿"看见几行"当"清零几行":其余那些是**已经是 0**
        # (上一轮清过)或**本轮要删**的 —— 混成一个数会让"闸在持续起作用"和
        # "今天又新坏了一批"长得一模一样(本仓在进度日志上栽过这个跟头)
        logger.warning("库存:%d 行**本店渠道与产品渠道不符**,其中 %d 行本轮"
                       "清零(原因码 channel_mismatch;其余已经是 0 或本轮要删)"
                       "——持续不符会被删除链的「渠道不符 N 天」窗口下架",
                       n_channel, n_zeroed)
    return out


def title_intents(conn, stockzero_stores: list[str] | None = None
                  ) -> list[dict]:
    """输入:连接 + stockzero 名单 → 输出:标题维护意图。

    新标题 = 亚马逊标题过**与上架同一套文案处理**(force_amazon_copy:
    去品牌名、去项目符号、折叠空白、截 199)——两处口径必须一致,
    否则上架写一个标题、维护又改成另一个,自己跟自己打架。

    旧防线原样保留:占位符跳过;**productType / UPC / 标题三缺一跳过**
    (缺任一沃尔玛必退回)。标题相同则不产出。

    相似度 < 70% 的行**跟着删除开关走**(停闸口径见 `TITLE_MISMATCH_DELETE` 头注):
      · `TITLE_MISMATCH_DELETE=True`  → 跳过(交给删除链,停闸前的老行为)
      · `TITLE_MISMATCH_DELETE=False` → 照改并计数告警
    """
    out = []
    skipped_incomplete = skipped_mismatch = paused_mismatch = 0
    for r in _rows(conn, stockzero_stores):
        store, sku = r["store"], r["sku"]
        product_name, product_type, upc = r["product_name"], r["product_type"], r["upc"]
        slow = r["slow"]
        amz_title = ((slow or {}).get("title") if isinstance(slow, dict)
                     else None)
        if not amz_title or str(amz_title).strip() in TITLE_PLACEHOLDERS:
            continue
        # 70% 闸(所有者定稿 2026-08-16):相似度过低说明**采到的可能不是同一个
        # 商品**,那时把亚马逊标题抄过去是把错的抄进线上 —— 交给删除链处置。
        # 相似度算不出来(有一边没标题)不算不匹配,照旧走改标题。
        # ⚠ 这道闸的前提是"交得出去" —— 删除链停闸时它交不出去,跳过就等于
        # 把这批行冻结在错标题上。所以**闸随删除开关走**:删除开着才跳过,
        # 删除关着照改、单独计数见人(口径见 `TITLE_MISMATCH_DELETE` 头注)。
        sim = title_sim_dual(product_name, slow)
        low = title_mismatched(sim)
        if low and TITLE_MISMATCH_DELETE:
            skipped_mismatch += 1
            continue
        new_title = processed_title(slow)    # 与上架同款处理(去品牌/截 199)
        if not new_title or new_title == (product_name or ""):
            continue
        if (product_name or "") == main_processed_title(slow):
            # 在架标题 = 主标题(短标题存量行):**不改回长标题** —— 这批行当初
            # 就是长标题被内容审查拒了才换的短标题,改回去等于自找再拒一遍
            continue
        if not product_type or not upc:
            skipped_incomplete += 1     # 三缺一跳过(旧防线)
            continue
        if sim is None:
            why = "同步亚马逊标题(相似度算不出)"
        elif low:
            # 计数落在**真产出**这一步,不落在闸那一步:后面还有"标题没变/
            # 三缺一"两道过滤,在闸上数会把没提交的也报成"改了"。
            paused_mismatch += 1
            why = f"相似度 {sim:.0%}(删除停闸,照常同步标题)"
        else:
            why = f"相似度 {sim:.0%},同步亚马逊标题"
        out.append({"store": store, "sku": sku, "kind": "title",
                    "old": product_name, "new": new_title,
                    "product_type": product_type, "product_id": upc,
                    # 原因码分组(飞书「原因」列):停闸期照改的低相似度行与
                    # 常规同步长得一模一样,不分码就数不出这个口径影响了多少行
                    "code": "title_mismatch_sync" if low else "title_sync",
                    "reason": why})
    if skipped_incomplete:
        logger.info("标题:%d 行缺 productType/UPC 跳过(三缺一防线)",
                    skipped_incomplete)
    if skipped_mismatch:
        logger.info("标题:%d 行相似度 < %.0f%% 不改标题(交给删除链)",
                    skipped_mismatch, TITLE_SIM_FLOOR * 100)
    if paused_mismatch:
        # 抄的是"可能不是同一个商品"的标题,必须 warning 级别见人:
        # 这是删除停闸期的临时口径,恢复删除后这批行会回到"跳过"
        logger.warning("标题:%d 行相似度 < %.0f%% **仍改标题** —— 删除"
                       "(title_mismatch)停闸期口径(所有者 2026-08-20:"
                       "停闸不冻结),原因码 title_mismatch_sync 可单独查",
                       paused_mismatch, TITLE_SIM_FLOOR * 100)
    return out


def delete_intents(conn, stockzero_stores: list[str] | None = None,
                   min_batches: int = MIN_OFFSET_BATCHES,
                   oos_days: int = LONG_OOS_DAYS,
                   store_channels: dict[str, str] | None = None) -> list[dict]:
    """输入:连接(+批次数门槛/无货天数/{店铺: 限定渠道})→ 输出:删除意图(kind='delete')。

    三个原因(所有者定稿 2026-08-09),都是"这个产品已经不值得留在架上了":

    variant_offset —— 亚马逊把 /dp/<ASIN> 返回成兄弟变体页面,parser 比对
      页面 ASIN 不一致后**拒绝写入**,采集侧列为**不自动重试** ⇒ 价格/库存
      **永远拿不到新数据**。门槛 min_batches 默认 1(所有者:偏移了就不会
      恢复,不设观察期);唯一防呆:最后一次偏移之后若有 outcome=ok 快照
      则移出名单——真能采就不该删。

    商品不存在 —— amz 标题是占位符(TITLE_PLACEHOLDERS)。旧系统在这里只是
      跳过标题维护,所有者 2026-08-09 改为**删除该产品**:亚马逊页面都没了,
      留在沃尔玛就是个卖不出去的死链。

    连续无货 N 天 —— 窗口内一条"有货"观测都没有(判据三条见 _SQL_LONG_OOS)。
      库存 provider 早就把它清零了,清零后还这么久不回货 = 这个货源没了。
      ⚠ 采集接线于 2026-08-08,历史攒够 15 天之前这条恒返空,不是坏了。

    渠道不符 N 天(2026-08-25 新增,所有者:「该渠道下库存连续不足 N 天,下架」)
      —— **同一个窗口、同一条阶梯**,只是"有货"要算成"本店渠道下有货":限定了
      渠道的店,货源翻到另一个渠道就等于这家店没货。原因码单独一个
      (`渠道不符N天`)而不是混进「连续无货」:两者的处置完全不同 —— 无货是
      货源断了,渠道不符是这个货该换一家店(或者改这家店的「配送限制」),
      而删除预览是按原因码分组给人看的。
      店没标「配送限制」时这一档**恒不触发**,判据逐字退回旧口径。

    ⚠ **本函数不设任何数量闸**(2026-08-24 归一;2026-08-26 连全局 5000 闸
    一并废除)。限额表「下架限制」由执行件 `problem_product_cleanup` 在领取时
    施加一次(services.dispositions.cap_destructive)。此前两条扫描件各截一次
    同一张表,每店最多 N 条实际变成最多 2N —— 扫描件如实报待办、执行件按
    配额取件,才只有一处上限。cap_per_store 的按店上限表也**刻意不含 delete**,
    理由相同。
    """
    seen, out = set(), []
    paused_mismatch = 0

    def _take(store, sku, code, why="", extra=None):
        """code = 机器码(飞书「原因」列的分组依据 / 建议行 category);
        why = 人读原因文案。2026-08-16 前两者是同一个字符串,「原因」列加进来
        之后必须分开:`title_mismatch` 分得了组但读不出"低到什么程度"。"""
        if (store, sku) in seen:
            return          # 两个原因都命中只删一次
        seen.add((store, sku))
        out.append({"store": store, "sku": sku, "kind": "delete",
                    "old": "在线", "new": "删除",
                    "code": code, "reason": why or code,
                    "label": f"删除({code})", **(extra or {})})

    with conn.cursor() as cur:
        cur.execute(_SQL_VARIANT_OFFSET, {"min_batches": int(min_batches)})
        for store, sku, batches, first_seen, last_seen in cur.fetchall():
            _take(store, sku, "variant_offset",
                  "采集永久偏移(拿不到新数据)",
                  {"batches": batches, "first_seen": first_seen,
                   "last_seen": last_seen})

    for r in _rows(conn, stockzero_stores):
        store, sku = r["store"], r["sku"]
        slow = r["slow"]
        title = ((slow or {}).get("title") if isinstance(slow, dict) else None)
        if str(title or "").strip() in TITLE_PLACEHOLDERS and title:
            _take(store, sku, "商品不存在", "亚马逊标题是占位符,页面已不存在")
            continue
        # 所有者定稿 2026-08-16 新增两条删除判据(judgement 在 classify):
        #   outcome == 'not_found'  → ASIN 已从亚马逊下架
        #   标题相似度 < 70%         → 采到的可能不是同一个商品,抄标题会抄错
        sim = title_sim_dual(r["product_name"], slow)
        act, code, why = classify(outcome=r["outcome"], title_similarity=sim)
        if act == "delete":
            _take(store, sku, code, why)   # 停闸由 classify 拦,这里只管收
        elif title_mismatched(sim) and (store, sku) not in seen:
            # 停闸压制条数**必须见人**(静默关掉的闸没人记得它关着)。
            # 报的是"因停闸少删了多少行",所以两类要排除:not_found 同时命中的
            # (上面已按 not_found 删了)、以及已被 variant_offset 收走的
            # ——它们照删不误,算进来会把"少删了多少"报大。
            paused_mismatch += 1

    chans = store_channels or {}
    with conn.cursor() as cur:
        cur.execute(_SQL_LONG_OOS,
                    {"days": int(oos_days),
                     "ch_stores": list(chans.keys()),
                     "ch_wants": [chans[k] for k in chans]})
        rows = cur.fetchall()
    n_wrong_ch = 0
    for store, sku, obs, first_seen, last_seen, wrong_ch_obs, want in rows:
        if wrong_ch_obs:
            # 窗口里出现过"有货但是另一个渠道" ⇒ 这行是渠道不符走到头的,
            # 不是货源断了。两个原因码分开,删除预览按码分组才说得清
            n_wrong_ch += 1
            _take(store, sku, f"渠道不符{oos_days}天",
                  f"{oos_days} 天窗口内 {obs} 次观测无一是本店渠道({want})"
                  f"可售的货,其中 {wrong_ch_obs} 次确认为另一渠道",
                  {"obs": obs, "first_seen": first_seen, "last_seen": last_seen})
        else:
            _take(store, sku, f"连续无货{oos_days}天",
                  f"{oos_days} 天窗口内 {obs} 次观测无一有货,货源已断",
                  {"obs": obs, "first_seen": first_seen, "last_seen": last_seen})
    if n_wrong_ch:
        # 向后看的窗口 ⇒ 某店刚填上「配送限制」的当轮就可能整批命中。
        # 必须出声,别让它混在"删除 N 条"里跟着日常波动过去
        logger.warning("删除:%d 行是**渠道不符满 %d 天**(不是货源断)。"
                       "某家店刚填/刚改「配送限制」时会成批出现——窗口是"
                       "向后看的,先看建议行再放执行件跑", n_wrong_ch, oos_days)
    if not rows:
        # 采集历史不足窗口长度时这条恒空——说出来,免得被读成"没有长期缺货的"
        logger.info("连续无货 %d 天:本轮 0 个候选(采集历史不足 %d 天时属正常)",
                    oos_days, oos_days)

    if paused_mismatch:
        logger.warning("删除(title_mismatch)已停闸(所有者 2026-08-19),"
                       "本轮压制 %d 条 —— 这批行照常改价/改标题/改库存"
                       "(所有者 2026-08-20:停闸不冻结)", paused_mismatch)
    return out


def collect_all(conn, stockzero: list[str], oos_days: int = 0,
                managed: dict[str, str] | None = None
                ) -> tuple[list[dict], list[dict]]:
    """输入:连接 + stockzero 名单(+无货天数 +受管仓表)→ 输出:
    (本轮全部维护意图, 截断报告)。

    单店单轮上限(cap_per_store)在**最后**施加 —— 在 doomed 剔除与 drop_recent
    之后,名额不浪费在注定不发的意图上(截在 provider 里的话,先截再被防重
    压掉的部分等于白扔名额)。⚠ 但"进了名额 = 一定提交"不成立,cap 之后还有
    三条泄漏:领取时的破坏组压制(claim 按库里**所有**未落定 delete/retire 判,
    含别的链写的)、同键 executing 行挡新建议(摘要的"写入 < 意图"就是它)、
    执行件缺凭证整店跳过 —— 将来调小上限时别按"名额=提交数"估算。
    截断报告必须随摘要**首行**见人,不许只进日志(链通知只发成功步骤的首行)。

    ⚠ **住在 services 而不是 workflow 里**:2026-08-16 拆成
    maintenance_scan(建议)+ maintenance(执行)之后,只有扫描件调它;但铁律
    禁止 workflow 互相 import,而 dry-run 口径必须与真跑一致 —— 收在这里,
    两边看到的是同一份意图。

    stockzero 店整店排除在三个自动 provider 之外 —— 它们归 zero_intents,
    否则"跟随 amz 库存"会把刚清零的货又顶回去(两条规则打架)。

    店铺渠道(限额表「配送限制」)在这里**取一次**分发给库存与删除两个
    provider:两边问的是同一个问题(这家店做哪个渠道),各读一次飞书除了慢
    还会漂 —— 一轮里前后两次读到不同的表,清零按新配置、删除按旧配置。
    """
    mults = store_limits.price_multipliers()
    chans = store_targets.store_channels()
    deletes = delete_intents(conn, stockzero,
                             oos_days=oos_days or LONG_OOS_DAYS,
                             store_channels=chans)
    doomed = {(d["store"], d["sku"]) for d in deletes}
    intents = list(deletes)
    # ⚠ 三个**库存** provider 都要收 managed:比对基准是受管仓现值。
    # 漏传一个的表现不是报错,而是那一路照旧拿全店合计比单仓目标 ⇒ 每轮重发。
    # 标题/价格与仓库无关,不传(传了也没有用武之地,别加无意义的参数)。
    for it in (title_intents(conn, stockzero)
               + price_intents(conn, mults, stockzero)
               + inventory_intents(conn, stockzero, managed,
                                   store_channels=chans)
               # 跟卖品铺货(所有者批复 2026-08-12):amz 三 provider 按路由
               # 铁律只碰 source_type='amz',跟卖品的库存唯一由它负责
               + match_inventory_intents(conn, stockzero, managed)
               + zero_intents(conn, stockzero, managed)):
        # 将被删除的行不再改价/改库存/改标题:它们的 amz 数据本来就是陈旧的
        # (采不到才要删),再跟一轮既烧配额又是拿错数据改线上
        if (it["store"], it["sku"]) not in doomed:
            intents.append(it)
    # 近期已提交过同一件事的压掉:提交成功后本地快照要等 catalog_sync 才更新,
    # 不压就会一轮轮重发同样的载荷(生产实证 208 条 stale update)
    intents, _n = drop_recent(conn, intents)
    return cap_per_store(intents)


# ── 意图 ⇄ 建议行(ops.dispositions)的互转 ────────────────────────────────────
# maintenance_scan 用 to_disposition() 落建议,maintenance 用 from_disposition()
# 还原成意图。**两个方向必须写在一起**:分散在两个 workflow 里,加一个键时
# 只改了写侧,读侧就静默丢那个键 —— 最典型的是 title 的 product_type/product_id,
# 丢了之后 build_title_item 组出 None,feed 被沃尔玛整批退回。
#
# 列位对应(建议行 → 飞书维护记录):
#   action   = kind      → 「建议」列(执行件另写「动作」列)
#   category = code      → 「原因」列的分组依据
#   reason   = 人读原因   → 「原因」列正文
#   detail   = 其余全部   → 旧值/新值/标题载荷/删除证据
# ⚠ `ship_node`(多仓批次 2)必须在这里:它决定执行件走哪条写通道
# (带 node → PUT /v3/inventories/{sku} 的 body nodes;不带 → legacy)。
# 漏登记的表现是**扫描件判对了受管仓、执行件却写到默认节点**,而且两边
# 都不报错 —— 正是本注释顶上那段说的"只改了写侧读侧静默丢键"。
_DETAIL_KEYS = ("old", "new", "label", "product_type", "product_id",
                "batches", "first_seen", "last_seen", "obs", "ship_node")

#: kind → 「建议」列的中文标签。**全项目唯一出处。**
#: 2026-08-17 起它不再只是显示文案,而是 maintenance_scan 与 maintenance 之间
#: 的**连接键**:扫描件按 (店铺, SKU, 建议) append 半行,执行件按同一个键找回
#: 那一行就地补齐动作/旧值/新值/feedid。两个 workflow 各存一份副本的话,改一处
#: 忘一处的表现是**执行件找不到扫描件写的行,于是每条都另起一行**——表里一条
#: 建议对两行、飞书行数翻倍,而两边都不报错。
#: 铁律 1 禁止 workflow 互相 import,所以这份唯一副本住在 services。
KIND_LABEL = {"delete": "删除", "title": "标题",
              "price": "价格", "inventory": "库存"}


def to_disposition(it: dict) -> dict:
    """输入:维护意图 → 输出:ops.dispositions 建议行(services.dispositions 收)。"""
    return {
        "store": it["store"], "sku": it["sku"], "source": "maint",
        "action": it["kind"], "category": it.get("code") or "",
        "reason": it.get("reason") or "",
        "detail": {k: it[k] for k in _DETAIL_KEYS if k in it},
    }


def from_disposition(row: dict) -> dict:
    """输入:ops.dispositions 建议行 → 输出:维护意图(带 disposition_id)。"""
    detail = row.get("detail") or {}
    it = {"disposition_id": row["id"], "store": row["store"], "sku": row["sku"],
          "kind": row["action"], "code": row.get("category") or "",
          "reason": row.get("reason") or ""}
    it.update({k: detail[k] for k in _DETAIL_KEYS if k in detail})
    return it


def build_title_item(sku: str, product_type: str, product_id: str,
                     title: str) -> dict:
    """输入:sku/productType/UPC/新标题 → 输出:MP_MAINTENANCE 标题维护 MPItem。

    结构旧系统实证:Visible 直接以 productType 名作命名空间(中间没有
    productCategory 层);Orderable 与 Visible 是并列顶级对象,不是 MPProduct。
    """
    return {"Orderable": {"sku": str(sku),
                          "productIdentifiers": {"productId": str(product_id),
                                                 "productIdType": "UPC"}},
            "Visible": {str(product_type): {"productName": str(title)}}}
