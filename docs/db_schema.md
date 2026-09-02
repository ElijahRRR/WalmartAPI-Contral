# Postgres 数据库设计

> 本机 PostgreSQL 17,库名 `walmart_data`。五个 schema,职责互不越界。
> 本文档是唯一的表结构事实来源:任何 AI 建表/改表必须同步更新这里。
> 可执行同步产物是 `refdata/schema.sql`(幂等),执行走 `python cli.py db_init`。
> 连接只准通过 `registry/db.py`;Metabase/NocoDB/MCP 用只读角色 `readonly`。

## Schema 总览

| schema | 职责 | 写入者 |
|---|---|---|
| `catalog` | 产品主数据:产品身份 + 采集快照 + 黑名单中心 + 占用台账 | catalog_sync(在线商品)/ product_ingest(采集摄取)/ **product_audit 经 services.audit_store 直写 audit_* 五列(已落地,不再是「未来」)** / risk_sync·blacklist_push(黑名单中心)/ 分配链(claims) |
| `listing` | 上架域:**现在只剩 `retire_cooldown`**(SKU_LOCKED 自愈冷却);tasks / upc_pool 已于 2026-08-12 退役删除,在用的 UPC 池是 `catalog.upc_pool` | sku_locked_heal |
| `orders` | 订单域:订单、审核结果、结算、售后 | order_audit / returns_sync / settlement 相关工作流 |
| `ops` | 运行域:运行记录、防重状态、游标 | cli.py 与各工作流 |
| `audit` | 审核域:规则字典、审核结论明细(2026-08-13 批次 A 迁自 walmart-audit-system) | audit_import(一次性)/ risk_sync(镜像)/ product_audit(批次 B 起) |

设计原则:
- 同一业务域的表放同一 schema,跨域 JOIN 随便写,不再有跨库之苦。
- 会给人看/未来网页端会读的数据在 catalog/listing/orders;
  只给脚本自己用的状态在 ops;可重建缓存不进数据库(放 DATA_ROOT/cache 的 SQLite)。
- 核心业务表统一带:`store`(归属店铺)、`owner`(归属人,团队协作预埋)、
  `created_at` / `updated_at`。
- 时间一律 `timestamptz`,存 UTC。

## catalog — 产品主数据(设计核心:身份与观测分离)

采集服务反复采同一产品(不同邮编/参数)产生多条记录是**正确的**,它们是观测快照;
缺的是稳定的产品身份层。因此拆两张表:

```sql
-- 产品身份:一个 ASIN 一行,终身唯一(慢变字段 + 审核结论 + 复用资产)
CREATE TABLE catalog.products (
    marketplace     text NOT NULL DEFAULT 'US',  -- 站点;复合主键,加站点零迁移(2026-08-06 拍板)
    asin            text NOT NULL,
    title           text,
    brand           text,
    amazon_category text,
    image_url       text,
    slow_hash       text,        -- 慢变字段哈希:变了才需要重审
    -- 审核结论(由审核服务产出)
    audit_status    text,        -- pending / approved / rejected
    audit_reason    text,
    walmart_pt      text,        -- 映射的沃尔玛 Product Type
    pt_source       text,        -- PT 来历(2026-08-14 定稿):walmart_confirmed=沃尔玛
                                 -- 真接受过 / audit_llm=审核链 LLM 推断。catmap_mine 只数
                                 -- 前者,否则 LLM 猜的 PT 会被自己反复确认再放大到整类目
    slow            jsonb,       -- 采集 slow 段全量留存(卖点/描述/重量/尺寸/变体);
                                 -- 契约的 raw 是裁剪过的,只存 raw 会丢卖点与重量
    browse_node_chain text,      -- 根→叶 browse node ID 链(采集 slow.category_id_chain)
    browse_node_id  text,        -- 叶子 node;类目名会漂 ID 不会,类目闸与审核 L1 ②级
                                 -- 优先按 ID 判(索引 products_browse_node_idx)
    audited_at      timestamptz,
    audit_version   text,        -- 审核规则版本,规则升级后可按版本批量重审
    -- (原"复用资产/归属"五列 assigned_upc/listing_attrs/last_feed_id/store/owner
    --  已于 2026-08-12 退役:零读写,职责被 catalog.upc_pool / catalog.llm_cache /
    --  ops.feed_log / 飞书上架表接管;audit_* 五列保留 = 二期审核服务接缝)
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (marketplace, asin)
);

-- 采集快照:追加不改,永不去重(快变字段)
CREATE TABLE catalog.snapshots (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    marketplace  text NOT NULL DEFAULT 'US',
    asin         text NOT NULL,
    scrape_params jsonb NOT NULL DEFAULT '{}',  -- 邮编等采集参数,参与"最新值"分组
    price        numeric,
    stock_state  text,           -- in_stock / out_of_stock / unknown(封闭集)
    stock_count  integer,        -- ⚠ NULL=没采到,0=确实是 0(下游禁止 or 0)
    delivery_days integer,       -- 同上
    shipping     numeric,        -- 运费:⚠ 同上 NULL≠0。0.0=确认免运费(FREE),
    shipping_raw text,           --   NULL=没采到(N/A)⇒ 落地价算不出来;
                                 --   raw 存原始串,出现新形态不必等契约改版
    buybox       jsonb,
    raw          jsonb,          -- 采集器原始载荷(裁剪后)
    scraped_at   timestamptz NOT NULL,
    source_id    text,           -- 采集器侧记录 ID,幂等去重用
    outcome      text,           -- ok/not_found/blocked/parse_failed/stale
    completeness_ok boolean      -- 采到了但不完整(空值不覆盖旧值)
);
CREATE UNIQUE INDEX ON catalog.snapshots (source_id);
CREATE INDEX ON catalog.snapshots (marketplace, asin, scraped_at DESC);

-- 最新快照视图:每个 (marketplace, asin, 参数组合) 取最新一条
CREATE VIEW catalog.latest_snapshot AS
  SELECT DISTINCT ON (marketplace, asin, scrape_params) *
  FROM catalog.snapshots ORDER BY marketplace, asin, scrape_params, scraped_at DESC;
```

使用约定:审核服务只关心 products(slow_hash 未变则不重审);
价格库存维护读 latest_snapshot;上架 workflow 两层 JOIN 取完整输入。

**"这个 ASIN 为什么没有新数据"的两条查法**(2026-08-09 补齐,互补不重叠):

| 情形 | 落在哪 | 怎么查 |
|---|---|---|
| 采到了但降级/不完整 | `catalog.snapshots.outcome ≠ 'ok'` / `completeness_ok=false` | 该 ASIN **有**快照行,看 outcome |
| 根本没采到(验证码/超时/404/封禁) | `ops.scrape_failures` | 该 ASIN **没有**快照行,按 batch_name 或 asin 查 |

第二种在增量流里完全不出现(没产出记录就没有可导出的行),必须由
`product_refresh` 在批次落定时主动拉 `/api/batches/{batch_id}/failures` 落库
——与 feed 报错同款口径:**拉详情是标准动作,不是排障时才做**。

```sql
-- 沃尔玛侧在线商品:每 (店铺, SKU) 一行,catalog_sync 全量扫店 upsert
-- (替代旧飞书「在线产品总表」的沃尔玛列;amz 数据在 products/snapshots,sku=asin JOIN)
CREATE TABLE catalog.walmart_items (
    store text NOT NULL, sku text NOT NULL,
    wpid text,
    item_id text,                            -- walmart.com 数字商品ID(邮件/工单定位);
                                             -- 来源:On-request ITEM 报表(Item ID 列/Page URL);
                                             -- 其余路径实证排除(items/catalog_search 无此字段,
                                             -- 搜索召回 3/131);缺席复现重置 NULL 触发重查
    upc text, gtin text,                     -- upc/gtin 必须 text:前导零教训
    product_name text, shelf text, product_type text,
    variant_group_id text,                   -- 变体组 ID(同组共享;listing 工作流复用)
    variant_group_info jsonb,                -- 变体组详情(isPrimary/分组维度,原样存)
    price numeric, currency text,
    avail_qty integer,                       -- GET /v3/inventories 合并(**全节点合计**)
    node_count smallint,                     -- 该 SKU 铺在几个发货节点(多仓批次 0)。
                                             -- 现状恒 1;价值全在"什么时候不再是"
                                             -- ——catalog_sync 摘要按它告警。
                                             -- 与 avail_qty 同源于 merge_rows 的一份入参
    published_status text, lifecycle_status text, unpublished_reasons text,
    last_seen_at timestamptz NOT NULL,       -- 最近一次全量扫描见到它的时间
    missing_since timestamptz,               -- 连续缺席起点;NULL=最近一轮仍在
                                             -- 标缺席时 published/lifecycle_status
                                             -- 同步清空(2026-08-07 定稿):旧观测
                                             -- 不再展示,复现时 upsert 写回新状态
    created_at / updated_at,
    PRIMARY KEY (store, sku)
);
```

"整表重写"语义的 PG 等价:每轮扫完 upsert 所见行(清 missing_since),
再把本轮未见的行标 missing_since 并清空两个状态列(**不删除,永不清理**
——所有者拍板 2026-08-12;技术上事件在 product_events 独立账本、删主表行
不丢病历,但拍板保守留行,13 万行 PG 无压力)。飞书「在线产品总表」投影只写在架行
(missing_since IS NULL),缺席商品不进表;last_seen_at/missing_since
两列也不投影(追踪在 PG 与事件账本,表只给人看在架现状)。

```sql
-- 分节点库存明细(多仓批次 1):每 (店铺, SKU, 发货节点) 一行,
-- catalog_sync 与 walmart_items 同轮落库(同一份 GET /v3/inventories 响应)。
-- ⚠ 存在的理由是 walmart_items.avail_qty 是**合计**,而写只写一个节点:
-- 多节点店里"合计 == 单仓目标值"永远不成立 ⇒ 每轮判有差异 ⇒ 每轮全量重发,
-- 而 settle 又永远判 ineffective。维护链的比对基准与落定判据都改读这张表
-- (受管仓 = 限额表「维护仓库」填的 FC ID;未填的店不碰这张表,行为零变化)。
CREATE TABLE catalog.item_node_inventory (
    store text NOT NULL, sku text NOT NULL,
    ship_node text NOT NULL,                 -- FC ID(17-18 位数字);Virtual Node 时为空串
    avail_qty integer NOT NULL,              -- 该节点 availToSellQty
    seen_at timestamptz NOT NULL,            -- 本轮观测时刻;落定判据靠它比 executed_at
    PRIMARY KEY (store, sku, ship_node)
);
CREATE INDEX item_node_inventory_node_idx ON catalog.item_node_inventory (ship_node);
```

```sql
-- 产品来源登记簿(2026-08-07 所有者定稿):每个上架产品登记"出身"
-- sku=asin 只对 amz 搬运品成立;跟卖/自建/1688 各有身份。谁上架谁登记,
-- 自动化按出身路由(源数据缺失驱动的破坏动作必须限定 source_type;
-- unknown 不自动动),手动通道全格式通吃。存量按 SKU 格式一次性回填。
CREATE TABLE catalog.listing_sources (
    store text, sku text,            -- PK (store, sku)
    source_type text NOT NULL,       -- amz / match / self / 1688 / unknown
    source_key  text,                -- amz=asin;match=匹配GTIN;1688=offer_id
    workflow    text,                -- 登记来源(backfill=格式回填)
    created_at  timestamptz
);
-- listing_sources_key_idx (source_key) WHERE source_key IS NOT NULL
-- (2026-08-30 补):主键是 (store, sku),按 source_key 反查"这个 ASIN 被哪些店
-- 登记过"用不上它,原本全表扫。风险追溯 services/risk_trace ②号证据源要按
-- ASIN 反查,故补;局部条件是因为 self/自建行这一列本就空(索引更小)。
```

```sql
-- UPC 池(L2a,2026-08-07 定稿):PG 权威,飞书「UPC池」表=注入口+投影
-- 领号=单事务 FOR UPDATE SKIP LOCKED(旧三层并发补丁消灭);状态机:
-- ''未用→claimed已领→used已用;回收仅三类(提交前失败/双确认未达/4xx),
-- Unknown 永不回收;conflict/bad_prefix(首位非 016789)永久弃用
CREATE TABLE catalog.upc_pool (
    upc text PRIMARY KEY,            -- 规范化 12 位
    status text NOT NULL DEFAULT '', -- ''/claimed/used/conflict/bad_prefix
    asin text, store text, sku text,
    put_date text,                   -- 运营注入日期(表格 B 列原样)
    claimed_at / used_at / created_at timestamptz
);
```

```sql
-- LLM 输入哈希缓存(L2c;旧 llm_cache.sqlite 的 PG 化,旧数据不迁)。
-- 一级:input_hash 精确命中(键含 model,换模型即失效——接受的语义)。
-- 二级(2026-08-18 所有者定稿):hash miss 时按 (asin, pt) 反查该 ASIN
-- 最近一次出参,reuse_sig 相等(spec 字段面/brand/category/变体属性都没变)
-- 且新旧标题过"规格 token 验证"才复用——标题描述图片等文案本就由系统
-- 每轮从最新采集数据覆盖,不靠 LLM,所以复用只赌"结构化字段没变"。
-- 四个元数据列只在 list_new 出参路径写入;audit_l3 等其它用途留 NULL
-- (它们的复用语义不同,不参与二级)。
CREATE TABLE catalog.llm_cache (
    input_hash text PRIMARY KEY,     -- sha256(model+messages+温度+tokens)[:32]
    model text NOT NULL, response jsonb NOT NULL,
    hit_count int DEFAULT 0, created_at, last_hit_at,
    asin text, pt text,              -- 二级反查键(部分索引 asin IS NOT NULL)
    src_title text,                  -- 出参时的亚马逊标题(规格 token 验证输入)
    reuse_sig text                   -- 硬条件签名:变了不许复用,直接重打
);
```

```sql
-- 风控库(L2b,2026-08-07):飞书两表镜像,闸门读库不读表(表格将停用)
-- 同步只增改不删;拦截条件:准入状态='禁售' 或 中国卖家可做 以'否'开头;
-- 品牌 casefold 精确匹配(brand_key)
CREATE TABLE catalog.risk_product_types (product_type PK, category, ptg,
    admit_status, cn_seller, cert_required, note,
    field_total, field_required, field_list, synced_at);
CREATE TABLE catalog.brand_blacklist (brand_key PK, brand, source,
    added_date, synced_at);
-- ↑ 黑名单品牌**总清单**镜像(risk_sync 飞书→PG)+ 否决闸数据源;
--   cleanup 自产品牌 DO NOTHING 补进闸门。src_sku/biz_cn/pushed_at 三列为
--   2026-08-11 过渡遗留;2026-08-14 逐列复核订正:**src_sku 仍是活的**
--   (risk_sync 把飞书总表 D 列 ASIN 镜像进来做溯源,services/risk_gate.sync_brands
--   的 INSERT 列清单里),真正零消费的只有 biz_cn / pushed_at 两列 —— 两列未删,
--   理由与「要删先连库自证」的判据见 schema.sql 该表注释。
--   2026-08-13 起同时是审核 Phase0 品牌闸的数据源(黑名单中心统一)。

-- 黑名单中心两张单列镜像表(所有者定稿 2026-08-13:审核四闸直读黑名单
-- 中心,不再维护旧审核系统的独立三列表)。同 wiki(黑名单品牌总表所在
-- 文档)sheet=B19LKn / sheet=twjmql;risk_sync 以 TRUNCATE 全量重灌镜像
-- (与"只增改不删"家族不同:飞书删行必须跟着消失),两道护栏:空读绝不
-- 重灌 + 骤缩超 50% 拒绝;各走独立事务
CREATE TABLE catalog.seller_blacklist (seller_id PK, synced_at);
-- ⚠ 类目表 2026-08-20 扩成**类目闸的唯一判据来源**(所有者定稿「代码里面
-- 的类目可以拿到数据库里来」):原 audit_phase0.FORBIDDEN_AMAZON_TOPS 四个
-- 硬编码顶级已迁进表内。三种匹配由 match_type 区分:
--   node_subtree  产品 browse_node_chain 里出现 browse_node_id ⇒ 拦整棵子树
--                 (**首选**;解决父级不覆盖子级 + 类目改名两个老毛病)
--   top_name      按顶级类目名(亚马逊顶级 browse node 无 ID,只能按名字)
--   path_exact    归一化完整路径等值(飞书镜像历史行,兼容保留)
-- source 只记来源('feishu'/'cleanup'/'seed'),**不再按它分家**(2026-08-20 所有者
-- 定稿:飞书那张五列表是本表的唯一维护面)—— risk_sync 是**整表镜像**:单事务
-- TRUNCATE + 全量重灌,表里有什么库里就是什么;分家会让「飞书里删了库里还在拦」
-- 的幽灵长期存在。代价是 category_blacklist_import 灌的行会被下次同步覆盖,
-- 那个工作流从此只作首次灌种 / 应急。判定件 services/category_blacklist.py 零 DB。
CREATE TABLE catalog.amazon_cat_blacklist (
    category_norm PK,   -- 归一化路径(audit_phase0.normalize_amazon_category,
                        -- 入库/查询两侧共用同一函数,读取端不再二次归一化)
    category_raw, match_type DEFAULT 'path_exact', match_value, browse_node_id,
    category_zh, reason, walmart_policy, enabled DEFAULT true,
    source DEFAULT 'feishu', synced_at);

-- 品牌·后台报错渠道表(beyKyi 投影源,PG 权威):完整记录沃尔玛后台问题
-- 商品拿到过哪些品牌;渠道内按品牌去重,**不与总清单去重**(所有者厘清
-- 2026-08-11)。历史重建走 blacklist_push -p rebuild_brand=1(擦净重灌 +
-- beyKyi 整表重写),日常由 problem_scan 尾段实时入账
-- (2026-08-14 批次 E:归类跟着决策搬到扫描件,黑名单收集是归类的副产品)。
CREATE TABLE catalog.brand_err_hits (brand_key PK, brand, source,
    added_date, src_sku, src_store, biz_cn, pushed_at, created_at);
-- 采集库缺品牌的候选走 brand_scrape 工作流补货(推采集→摄取→入账;
-- 防循环:非标准 asin 过滤 + ops.dedupe('cleanup:brand_scrape') 尝试台账)
```

```sql
-- 占用台账(分配 A1,docs/allocation_plan.md §五):品牌与产品的排他归属。
-- **占用是决策不是观测**:只有「分配」和「释放」两个显式动作能改它,在线快照
-- 怎么抖都不影响(店铺暂停、商品下架、从没上架成功,占用都不动)。排他性由
-- 部分唯一索引 claims_active_uniq (kind, claim_key) WHERE status='active' 保证,
-- 不靠代码自觉;released 行永久保留 —— 它是「这个品牌当初属于谁、什么时候为什么
-- 放出来」的唯一答案。读写唯一出处 services/claims.py;消费方含 list_new 占用闸。
CREATE TABLE catalog.claims (
    id         bigint IDENTITY PRIMARY KEY,
    kind       text NOT NULL,            -- brand / product
    claim_key  text NOT NULL,            -- brand=services/brand_key 归一键;product=ASIN
    store      text NOT NULL,
    status     text NOT NULL DEFAULT 'active',   -- active / released
    walmart_pt text, pt_source text, audit_version text,  -- 决策时快照(2026-08-15)
    source     text NOT NULL,            -- 落它的工作流(alloc_backfill / allocate)
    note text, claimed_at timestamptz, released_at timestamptz, released_reason text
);

-- 产品事件账本(2026-08-06 所有者需求:产品全生命周期追踪,"病历")
CREATE TABLE catalog.product_events (
    id bigint IDENTITY PRIMARY KEY,
    sku text NOT NULL,              -- 沃尔玛侧订货号**原文**(2026-08-11 推翻
                                    -- 旧约定 sku=asin:三段式订货号/纯数字
                                    -- item id 实证)
    asin text,                      -- 产品源头侧标准码,record_many 按
                                    -- services/sku_asin 规则自动清洗;提不出
                                    -- 存 NULL,消费方 coalesce(asin, sku);
                                    -- 存量补洗走 sku_normalize 工作流
    store text,                     -- 平台级事件可空
    event text NOT NULL,            -- 事件码唯一出处 services/product_events.py:
事件码唯一出处 = `services/product_events.py` 的常量与 `EVENTS` 集合(`record_many` 对未登记码抛错);本文档不再复述清单——三处清单曾各漂各的,`maintenance_submitted`/`problem_categorized` 发了大半个月没登记就是这么漏的。
    source text NOT NULL, error_code text, detail jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
-- 读侧视图 ×4(2026-08-11 补齐消费面;身份键一律 coalesce(asin, sku)——
-- 按订货号原文聚合时,三段式 sku 名下的删除史拦不住同 ASIN 换号重上):
--   product_risk        全局风险档案(上架/提交/删除/停用/缺席/未生效计数,
--                       最近移除时间)——**只是查询档案,不是拦截条件**
--                       (所有者口径 2026-08-12:防呆=黑名单,按拉黑类别拦,
--                       不按删除史拦);list_new 仅消费 unexplained_missing
--                       (消失过且从未提交删/停=疑似平台下架)做报警,不拦截
--   product_risk_store  同口径按 (asin, store) 聚合:"这个产品在哪些店被删过
--                       几次";store 为空的事件只出现在全局视图
--   status_changes      status_changed 平铺(old/new/官方 reasons)——查"谁被
--                       平台下架、为什么":WHERE new_status <> 'PUBLISHED'
--   feed_failures       五类 feed 的逐 SKU 失败回执(kind/error_code/detail)
```

事件账本三条纪律:只追加永不改;**回执与观测分开记**(feed 回执 success 是
沃尔玛的一面之词,删除以 catalog_sync 观测核验为准——回执成功但宽限期后仍
在架 → delete_not_effective 告警,所有者实证的真实故障模式);写入点分布:
catalog_sync(观测迁移)/ feed_track(回执)/ product_clear(提交)/ product_ingest(入库)/
list_new·match_listing(上架)/ product_audit(审核)/ problem_scan(归类)/
problem_product_cleanup(破坏动作)/ sku_locked_heal(退役)——「未来 listing·审核」
那一档已在批次 B/E 全部接线,不再是待办。

## listing — 上架域

```sql
CREATE TABLE listing.retire_cooldown (  -- SKU_LOCKED 自愈链状态(sku_locked_heal)
    id bigint IDENTITY PRIMARY KEY,
    store text NOT NULL, sku text NOT NULL,
    feed_id text NOT NULL,              -- RETIRE_ITEM 的 feed
    retired_at timestamptz DEFAULT now(),
    status text DEFAULT 'pending',      -- pending 冷却中 / cleared 已清列重上 /
                                        -- failed RETIRE 回执失败,人工处置
    cleared_at timestamptz
);  -- 部分唯一索引 (store, sku) WHERE pending:同对只许一条在途冷却,防重复退役
-- 链路(旧实证:SKU 绑死旧 UPC,不先退役换 UPC 重发也失败):
-- RETIRE_ITEM → 24h 冷却 → 回执成功才清列(K~M/O~Q)→ list_new 领新 UPC 重上

-- (listing.tasks 与 listing.upc_pool 已于 2026-08-12 退役删除:全仓零代码
--  引用——上架状态权威 = 飞书上架表 + catalog.upc_pool + retire_cooldown,
--  从未经过 listing.tasks;在用的 UPC 池是 catalog.upc_pool,两者状态机定义
--  冲突;UPC 历史池已拍板不迁,有用号所有者手动写入 catalog.upc_pool)
```

## orders — 订单域(行级统一建模,2026-08-06 定稿)

order_audit / returns_sync / 绩效问题订单 / 对账明细四条链路共用同一行级标识:

```
order_line_id = 'ol_' + sha256(po_id + '\x1f' + sku)[:24]
```

身份设计(2026-08-06 v3 定稿;v2 曾用行号,项目所有者按业务规则改定 SKU):
- **真正的身份锚点是自然键 `UNIQUE (po_id, sku)`**;哈希 ID 是它的
  派生物,价值只在四表单列 JOIN,可随时从自然键重算。
- **店铺不参与身份**(v2 起,弃订单中心v1 方案):PO 号是沃尔玛发的、平台
  全局唯一;店铺名是我方标签(飞书凭证表),改名/换人是真实运营事件,参与
  哈希会瞬间作废全部行标识。店铺存列+索引,只做归属与过滤。
- **SKU vs 行号**(v3 修订):同一 PO 内同一 SKU 必合并为一行(所有者实证
  规则),(PO,SKU) 与 (PO,行号) 同样唯一;而绩效报表只给 PO+SKU 不给行号,
  SKU 身份使绩效事件**写入时直接建键**(订单不在库也成立),消掉 v2 两段回填
  的主缺口。行号存列做展示/对账。SKU 仅去首尾空白、大小写敏感。前提被打破
  (同 PO 同 SKU 多行)时 extract 告警,后行覆盖前行——兜底不许静默。
  售后行身份取 item.sku(必有;行号引用字段旧数据有缺失记录);对账 CSV 的
  SKU 两级解析:自带列(Partner Item Id 等候选)→ 按 (po,行号) 反查订单行,
  都无则跳过计数。无 SKU 的老版绩效报表行留 NULL,单行订单可回填,不硬造。
- **绩效跨周期**:逐周期累积,主键 (po_id, metric, period)(拒绝订单中心v1 的
  同键覆盖——丢历史后"影响范围"无法回答)。`perf_event_spans` 视图给出每条
  违规的存续区间与 still_active(以最新报表是否仍包含该单为准,不自行推算
  官方统计窗口)。
- **审核结论落在 order_lines 自身**(2026-08-09 定稿,不另建表):
  `audit_status`(✓ 通过 / 建议拒绝 / 待人工)+ `audit_detail` jsonb + `audited_at`。
  安全前提已核:`order_sync` 的 upsert 只覆盖它自己给出的列,拉单永远冲不掉
  审核结论;反之 order_audit 的 UPDATE 也只碰这三列。
  `audit_detail` 结构(order_audit 写,飞书审核列由它投影):

  ```jsonc
  {
    "note": "成本 54.0 ≤ 限价 75.0;采购方 甲",   // →「脚本审核」列
    "asin": "B001", "zip": "10001",              // 判定用的是哪个邮编的快照
    "amz_price": 50, "stock_qty": 5, "ship_method": "FBA",
    "ship_days": 3, "seller": "Acme", "amz_title": "...",
    "shipping": 0.0, "shipping_raw": "FREE",      // NULL 表示没采到,不是免运费
    "scraped_at": "...",
    "supplier": "甲", "rate": 1.0,                // 本行实际套用的采购方与汇率
    "price_cap": 75.0, "cost": 54.0,
    "title_similarity": 0.9673,                   // →「标题相似度」列
    "rules": {                                    // 各道审核的过程值,事后可复盘
      "phishing": {"hit": false},
      "title":    {"similarity": 0.9673, "min": 0.9},
      "delivery": {"days": 3, "max": 9},
      "supplier": {"hit": true, "name": "甲", "rate": 1.0},
      "price":    {"cap": 75.0, "cost": 54.0}
    }
  }
  ```

  配置(黑名单邮编/采购方表)不入库,每次运行现读飞书;**每行实际套用的
  采购方与汇率写进 audit_detail**,所以"当时按什么算的"事后仍可追溯。

生成函数唯一出处:`services/order_lines.py`;审核规则唯一出处:`services/order_audit.py`。

**两条 upsert 语义**(2026-08-10 生产实证后定稿,`services/order_lines._upsert`):

- **内容没变就整行不写**(`ON CONFLICT ... DO UPDATE ... WHERE 旧值 IS DISTINCT FROM 新值`)。
  原先无条件 `updated_at = now()`,而 order_sync 每轮**全量重拉 45 天窗口**
  ⇒ 窗口内每行 updated_at 都被刷新 ⇒ order_center_push 把它当「拉取时间」写进
  飞书载荷、载荷参与指纹 ⇒ **指纹必变 ⇒ 每轮重推窗口内全部行**
  (实证:销售订单 7100 行更新 3122,正是 45 天窗口行数;售后表没有「拉取时间」
  列,同一轮只更新真变化的 7 行——天然对照组)。改完 `updated_at` 才真正表示
  "这行什么时候变的",飞书指纹这道写放大治理也随之恢复效力。
- **电话全 0 不覆盖真电话**:沃尔玛常态把买家电话打码成 `0000000000`
  (实证 45 天窗口 2964/3542 = 84%),原样覆盖会把库里真值冲掉且**找不回**
  (`raw` 也是每次一起被覆盖的)。旧系统的「电话全 0 保护」,legacy_survey
  明列必须照搬。反向不设防:真电话覆盖全 0 是正常修复。

| 表 | 主键 | 内容 | 写入者 |
|---|---|---|---|
| `orders.order_lines` | order_line_id(UNIQUE po+sku) | 销售明细行:商品/状态/金额/物流/收件人 + 审核结论(audit_status/audit_detail);行号存列做展示。**`source`**:NULL=API 完整行,`'历史数据'`=order_history_import 导入的残缺行(只有下单时间/店铺/PO/SKU/品名/数量/金额,状态一律 Delivered),order_center_push 据此不推飞书;order_sync 覆盖同一行时会把它写回 NULL,API 拉到真行后自动回到推送流。**`order_date` 观测→定稿**(所有者定稿 2026-09-02「下单时间不应该被修改」;事故:沃尔玛 GET /v3/orders 的 orderDate 单次读取不可信,偶发给出别的订单的时间甚至未来日期):首见只写候选(`order_date_confirmed=false`;`order_date_seen` 记最近一轮观测值),**连续两轮拉取一致才定稿**,定稿后锁死不再改;未定稿时连续两轮出现同一个不同值则改判(首见就错的自愈通道);未来日期拒写留 NULL,晚于本行状态时间的记存疑;每次不一致(冲突/改判/待定)与拒写/存疑逐条告警并进 order_sync 摘要首行;`order_meta` 存首见信封摘要(orderDate 原值/customerOrderId/预计发货送达/各行状态时间)取证,只在插入时写;`order_date_streak` 记定稿后同一异值连续出现的轮数,到 3 在摘要首行报「疑错」并给修复命令(定稿值不自动动);观测记账不碰 `updated_at`(不触发飞书重推)。**详情接口第二来源**(所有者方案 2026-09-02,探针 4 实证 `GET /v3/orders/{po}` 可信):新单首见以详情值落库并直接定稿(`order_date_source='detail'`);没被详情核对过的存量行(`order_date_source` 为 NULL/`'list'`)每轮查详情直到定稿;详情定稿后列表再不一致只计数、不查不改(同一异值连续三轮才补查一次详情作保险);列表值明显异常时拒写并用详情补正;详情不可用退回两轮机制。语义唯一出处 `services/order_lines._ORDER_DATE_GUARD`/`_ORDER_DATE_STATE_SQL`;修复已定稿错行走 `order_sync -p repair_order_date=<PO 列表>` 显式模式(只改列出的 PO,裸开关报错);**加列后须 `python cli.py db_init`**。**`asin`**(A1.5,2026-08-15):源头 ASIN,由 `order_asin_normalize` 按 `services/sku_asin` 补填,**提不出留 NULL**;分配引擎的产品/品牌销量维度按 `asin IS NOT NULL` 过滤,**不许拿 sku 原文当 asin** | 订单拉取工作流 + order_audit 回写审核 + order_history_import 补历史 + order_asin_normalize 补 asin |
| `orders.return_lines` | (return_order_id, order_line_id) | 售后单行(一条 returnOrderLine 一行);行级状态实证在 returnOrderLines 内,物流在 returnLineGroups[].labels[].carrierInfoList[] | returns_sync |
| `orders.perf_events` | (po_id, metric, period) | 绩效问题订单,**逐周期累积**——同一违规在多个周期出现即多行,影响范围按 period 查询;历史累计 COUNT(DISTINCT (po_id,metric))(2026-08-26 所有者定稿:一单只属一店、PO 全局唯一,store 不进去重键,与 schema.sql 注释一致) | `perf_problems`(2026-08-08 从 daily_report 摘出独立成流,已落地;写库经 services/order_lines) |
| `ops.store_settlements` | (store, report_date) | **结算账期台账**(2026-08-31):一个账期一行,存该期 PaymentSummary 的 Total Payable。**累计回款 = SUM(total_payable)**。⚠ 不能用 `settlement_lines` 求和代替(它按订单行聚合、过滤掉订单不在库的行、不含账期级费用);也不能按天求和 `store_kpi_daily.payout`(那是"当前待打款"快照,打款前天天出现 ⇒ 同一笔重复计)。另一半价值:沃尔玛的 `availableReconFiles` 只保留有限期,落库之后就永远留着 —— 这份累计随运行时间**越来越完整** | 结算同步 |
| `orders.settlement_lines` | (order_line_id, period) | 对账明细按行×账期聚合:net/gross/product/commission + 佣金明细。gross=各行绝对值和,用于区分"净 0=全额退款"与"净 0=无金额"(实证:Sale/Refund 同期相消) | 结算同步 |

视图:
- `orders.settlement_by_line` — 跨账期合并 + 入账状态推导(net>0 已入账 / net<0 已冲销 / net=0 且 gross>0 已退款 / 其余待入账;金额 round6 吸收浮点相消误差——实证 +52.68-52.68 = 4.44e-16 会误判);
- ~~`orders.order_center` 主视图~~(2026-08-12 退役删除:order_center_push 直连三张明细表与两个在用视图,主视图零读者)。

完整列清单见 `refdata/schema.sql`。旧 po 级表 `orders.orders` 已于 2026-08-12 退役(schema.sql 的退役清理节:确认为空表才 DROP,防手滑)。

## ops — 运行域(状态与业务同库,可同事务修改)

```sql
CREATE TABLE ops.runs (             -- 运行记录:cli.py 每次执行写一行
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow    text NOT NULL,
    params      jsonb,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    status      text NOT NULL,      -- running / success / failed
    summary     text,               -- run() 返回的结果摘要
    operator    text                -- launchd / manual / web / mcp
);

CREATE TABLE ops.feed_log (         -- feed 防重(核心安全表):先落 pending 再调接口
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow    text NOT NULL,
    store       text NOT NULL,
    feed_type   text NOT NULL,      -- DELETE_ITEM / MP_MAINTENANCE / MP_ITEM ...
    payload_key text NOT NULL,      -- 内容指纹(如 SKU 集合哈希),防重复提交
    feed_id     text,               -- 提交成功后回填
    status      text NOT NULL,      -- pending / submitted / done / failed
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON ops.feed_log (feed_type, store, payload_key);
-- 启动对账:凡 status='pending'/'submitted' 的行,先查 Walmart 实际 feed 状态再决定补交
-- 防重语义(2026-08-07 定稿):唯一索引拦的是在途行(pending/submitted);
-- 终态行(done/failed)被 _log_claim 重占回 pending 后同载荷可再发(不设时间防重窗)

CREATE TABLE ops.feed_items (       -- feed 的 SKU 级台账(所有 feed 操作共用)
    feed_id     text NOT NULL,      -- 提交时由 api/feeds 落行(status=submitted)
    sku         text NOT NULL,
    workflow    text NOT NULL,
    store       text NOT NULL,
    feed_type   text NOT NULL,
    status      text NOT NULL,      -- submitted / success / failed / missing
    error_code  text,
    error_desc  text,               -- 沃尔玛给的人话描述(+字段名):光有数字码
                                    -- 无法诊断(2026-08-09 首跑 DATA_ERROR 教训)
    submitted_at timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz,
    PRIMARY KEY (feed_id, sku)
);
-- 终态由 services/feed_track 轮询回写(feed_poll 工作流全局扫,业务工作流也可
-- 单 feed 轮询);SKU 级状态权威在此,飞书驱动表的"结果"列只是投影。
-- 停用/删除/设置到期日期 + 上架/改价/改库存/改标题 feed 全走这一套 —— 七种
-- feedType 均已接线(DELETE_ITEM / RETIRE_ITEM / MP_MAINTENANCE / MP_ITEM /
-- MP_ITEM_MATCH / price / inventory),载荷构造唯一出处 api/feeds.py。

CREATE TABLE ops.feishu_sync_state (   -- 飞书投影同步状态(order_center_push)
    table_id    text NOT NULL,      -- 飞书 table_id
    row_key     text NOT NULL,      -- 行去重键(order_line_id / 唯一键 / perf_key)
    record_id   text NOT NULL,      -- 飞书行内部编号(更新按它定位)
    pushed_hash text,               -- 上次写入飞书时的载荷指纹
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_id, row_key)
);
-- 日常同步零拉表:本地比指纹定位要写的行;状态为空(首轮)或任何写失败
-- 自动清状态 → 下轮全量拉表重建映射(自愈);-p reconcile=1 强制对账。
-- 前提纪律:六表不删行、不复制行、键列不手改(打破由对账发现并告警)。

CREATE TABLE ops.cursors (          -- 各同步任务的增量游标(替代旧系统散落的 _meta sheet)
    name        text PRIMARY KEY,   -- 如 'order_sync:A085' / 'recon_done:A085朱丽霖'
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
-- recon_done:<店铺> = 已处理对账账期数组(台账):烂账入库过滤后某期可能
-- 0 行落库,只看 settlement_lines DISTINCT period 会把它当缺失账期无限重拉
-- store_config = 治理配置快照(services/store_config,2026-08-30):上下架限额表
--   的**登记列**原文 + 凭证表「启用」+ 规划外名单,与上一版逐格比,变化落
--   ops.store_events 的治理类事件。**只留最近一版**(整份覆盖);飞书读失败时
--   既不产事件也不覆盖 —— 把"读不到"记成"被清空"会造两轮假事件,而账本只追加、
--   删不掉。形状带版本号 `v`,**版本不同一律当首次快照**(不产事件、只覆盖)。
--   ⚠ 密钥列绝不进快照:凭证表只点名取「店铺」「启用」两列(本表无 chmod 600)
--   ⚠ v2(2026-09-01 生产实跑改口径):限额表**未登记列只存列名不存值**
--   (`limits_extra_cols`)。v1 存的是整表原文,里面有飞书内部字段 `SourceID`——
--   它的值是 base64 复合键、**含行内容的哈希**,谁动一格就全表跟着变,于是
--   凭空刷出一批 store_limits_changed 把真信号淹掉。口径:未登记列没有任何代码
--   消费它,改了系统行为一个字节都不变 ⇒ 不产逐格事件;只在**首次出现/整体
--   消失**时产一条 store_limits_columns_changed(store=NULL,表结构级)

-- 店铺日报域(daily_report 工作流;字段语义对齐旧飞书「店铺KPI」32 列)
CREATE TABLE ops.rate_events (           -- 跨进程限速事件(api/_client 稀缺桶)
    client_id  text NOT NULL,            -- 店铺维度
    bucket     text NOT NULL,            -- 桶名(唯一出处 _client._RATE_BUCKETS)
    called_at  timestamptz NOT NULL DEFAULT now()
);  -- 判据 window≥600s 或 limit≤10 的桶才落库(feeds.post.*/prices.put/
    -- reports.request/insights/SPEC 日额度);插入顺手清 2 天前旧行;
    -- PG 不可达稀缺桶 fail hard(所有者拍板 2026-08-12,写操作永不自动兜底)

CREATE TABLE ops.store_kpi_daily (
    store            text NOT NULL,
    data_date        date NOT NULL,
    seller_name      text,           -- 影刀前台抓取(可 stale 补);无则空
    partner_id       text,
    seller_id        text,
    store_status     text,
    payment_status   text,
    sales_status     text,           -- 影刀前台抓取;不新鲜宁可留空不回填(旧事故规则)。
                                     -- store_status 有值且非 ACTIVE(SUSPENDED/
                                     -- TERMINATED 等)→ 默认「不可售」:这些店不进
                                     -- 影刀清单,不给默认值会永远空着(2026-08-15)。
                                     -- 推导自本轮 store_status,非跨日回填
    items_online     integer,        -- 来自 catalog.walmart_items(PG 复用,不再调 API)
    items_in_stock   integer,
    items_out_stock  integer,
    orders_count     integer,        -- 24h 窗口(中国时间 06:30 锚)
    sales_amount     numeric,
    otd_rate         numeric, cancel_rate numeric, vtr_rate numeric,
    srr_rate         numeric, refund_rate numeric, negative_rate numeric,
    return_rate      numeric, inr_rate numeric,
    period_sales     numeric, commission numeric, refund_amount numeric,
    closing_balance  numeric, reserve_to_date numeric,
    payout           numeric,        -- 非 ACTIVE 强制 0;负归 0(业务规则)
    payout_date      text,
    payment_processor text, settle_cycle text,
    no_hold          boolean,        -- 仅 ACTIVE 且 payout>=closing 时 true
    prev_payout      numeric,        -- **已停用**(所有者 2026-08-31「这个字段
                                     -- 不需要」)。原口径 = 严格 -14 天那一期。
                                     -- 列不删(历史行的值是当时的真实观测),
                                     -- 日报不再写、看板不再投影
    total_payout     numeric,        -- **累计回款**:沃尔玛总共已付的钱
                                     -- = ops.store_settlements 各账期之和
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (store, data_date)
);

-- 店铺事件账本(2026-08-30 所有者需求:店铺维度病历,TRO 封店预警)。
-- 与 catalog.product_events 同构不同表:只追加、事件码唯一出处
-- services/store_events.py、record_many fail loud。与 store_kpi_daily 的
-- 分工:KPI 表是日粒度截面,本表是变化流;事件 detail 只记 {old,new},
-- 绝不复制 KPI 数值(要全貌按 store+日期回查 KPI 表)。
-- severity 按迁移方向写入时定级(同一码两个方向级别不同):
--   high = 任意→TERMINATED / store ACTIVE→SUSPENDED / payment ACTIVE→INACTIVE
--   mid  = 可售→不可售(影刀列)及未知迁移;info = 恢复方向(入账不推送)
-- 写入方(截至 2026-08-30):
--   risk       daily_report(三状态迁移)、product_audit(tro_brand_hit 源头 +
--              tro_brand_exposure 波及)、order_audit(phishing_order 收单店 +
--              phishing_brand_exposure 波及);
--   governance services/store_config(限额表**登记列**逐格 diff + 未登记列的
--              表结构 diff、凭证表在册/启用、规划外名单;快照存
--              ops.cursors['store_config'];**由 store_watch 每轮调用** ——
--              它是这个模块唯一的属主,别再从别处调)、alloc_plan /
--              alloc_backfill(claim_created,**按 claim_many 的真落库行**计数,
--              用"成功数"会把幂等重跑记成天天新占)、store_release
--              (claim_released:整店 high、点名/csv mid)。
--              治理类**与业务动作同事务**:台账落了而事件没落,事后按事件流
--              回查"这个品牌当初什么时候归的它"会查不到。
--   ops        五条执行链**每店每轮一条**(severity 恒 info):list_new /
--              maintenance / problem_product_cleanup / product_clear /
--              match_listing,detail = 该店本轮的计数字典。
--              ⚠ **绝不逐 SKU**(逐 SKU 归 catalog.product_events 与
--              ops.feed_items;五条链每天几万行,记几个月就是上千万行,
--              而且会把风险/治理两类淹到查不出来);**计数全 0 的店不落行**
--              (没活干不是事件);二轮重试的店两轮计数相加**只记一条**。
--              运营类与业务动作**不同事务**(services/store_events.
--              record_round_safe 自开连接 + 兜底):货已经提交出去了,
--              账本缺一轮可以补,而记账炸掉整轮不可以 —— 与治理类方向相反,
--              因为治理类的两半必须同生共死,运营类的账本只是事后对时间线。
-- 防重不在本表(只追加、无唯一键),两条链各有各的办法:
--   · TRO   ops.dedupe 两个 scope:'audit:tro_brand'(一个品牌一条源头)与
--           'audit:tro_expand'(整品牌展开一次,**不按店**)。分开是为了让
--           "先以未判身份报过、后被 L3 确认"的品牌仍能补做波及展开;
--   · 钓鱼  身份键在 detail 里(store_events.record_line_events 的 NOT EXISTS:
--           (event, store IS NOT DISTINCT FROM, detail->>'order_line_id')),
--           不占 dedupe —— order_audit 每轮重判窗口内的行,写入口自己幂等,
--           而且它还要**每轮重扫窗口补记**账本里漏掉的钓鱼行。
CREATE TABLE ops.store_events (
    id bigint PK,
    store text,                  -- NULL = 全局源头事件(TRO 命中本体,波及店
                                 -- 由 services/risk_trace 展开成逐店行)
    event text NOT NULL,         -- 合法值见 services/store_events.EVENTS
    severity text NOT NULL,      -- high / mid / info
    source text NOT NULL, detail jsonb,
    occurred_at timestamptz DEFAULT now(),
    notified_at timestamptz      -- store_watch 已推送标记;NULL=待扫描
);  -- 索引:(store, occurred_at DESC) / (event, occurred_at DESC) /
    -- 局部 (severity, occurred_at DESC) WHERE notified_at IS NULL
```

事件码唯一出处 = `services/store_events.py` 的常量、`CLASS` 分类表与 `EVENTS`
集合(`record_many` 对未登记码抛错);**本文档不复述清单**,照 `product_events`
的老规矩 —— 三处清单必然各漂各的。上面按 risk/governance/ops 三类列的是
**写入方**(谁在什么场景落行),不是码表;要看有哪些码、各归哪一类,读那份代码。
一条码的摘要文案也在同一处(`store_events.brief`,全事件码唯一渲染出处)。

**唯一消费方 = `store_watch`**(每小时 :45,launchd)。写入方一律只落行不发通知
——谁发谁就得各自实现去重与限流,而同一次封店会从三个地方各响一次。
`notified_at` 只由它写:扫「未推送 + 高危 + 窗口内」→ 一轮一条飞书 → 标已推;
**推送失败一条都不标**(账本只追加,标了就是永久埋掉)。首次上线要先
`python cli.py store_watch -p seed=1` 把存量标掉,上线三步见 `docs/store_events.md`。

读侧视图 ×2(2026-08-30;**零程序读者是设计如此**,留给人工与 AI 排查,
判死前先查 `pg_stat_statements`):

| 视图 | 一行是什么 | 回答什么 |
|---|---|---|
| `ops.v_store_timeline` | 一事件 | 这家店身上按时间发生过什么。`old/new/data_date` 三个常看的 jsonb 键摊平,`明细` 列仍给整个 jsonb(TRO/钓鱼/治理三族的 detail 里根本没有 old/new,只留摊平列会显示成一片空);`store IS NULL` 渲染成 `(全局)` |
| `ops.v_store_profile` | 一店 | 此刻什么样 + 身上压着几条高危 + 还有几条没推出去 + 五条运营链上次动它是什么时候。**店铺全集来自 `store_kpi_daily`**(没跑过 daily_report 的店不出现,全局事件也不在这里);DISTINCT ON 先把 KPI 压成每店最新一行再 LATERAL 聚合,关联条件用裸列吃 `store_events_store_idx` |

⚠ `v_store_profile` 里五个 `*_round` 事件码是**唯一一处字面量副本**(视图是 SQL,
取不到 Python 常量)。`tests/test_store_watch.py` 有一条用例把它们与常量对拍 ——
漏改的表现是那五列永远为空,不报错。
```

CREATE TABLE ops.perf_problem_orders (   -- 永久累积,首次发现日期不被覆盖
    id bigint, first_seen_date date, store text,
    sales_order_no / po_no / order_date / indicator(带 emoji 契约) /
    sub_category / accountable / description / item / carrier / tracking_no / note,
    raw jsonb,                           -- xlsx 原始行,对拍校准用
    UNIQUE (sales_order_no, indicator, sub_category, tracking_no, item)
);
-- 写入一律 INSERT ... ON CONFLICT DO NOTHING:天然实现旧系统"永久累积+全局去重+
-- 保留首次发现日期"语义,消掉旧系统清空飞书全表重写的丢数据风险

CREATE TABLE ops.audit_scrape (     -- 订单审核的按邮编采集台账(一个 ASIN×邮编一行)
    asin text, zip text,            -- zip = 5 位标准邮编
    batch_name text, state text,    -- pending / done / failed
    error_type text,                -- 采集侧失败类型,**单独成列不埋在 reason 文本里**:
                                    -- 判定链按它分流 —— RETRYABLE 换时段重采可能就好;
                                    -- variant_offset / parse_error / server_reject 重采
                                    -- 多少次都一样,给终局结论并停止重推(免得白烧配额)
    reason text, requested_at timestamptz, settled_at timestamptz,
    first_requested_at timestamptz, -- 这一轮重试从什么时候开始(重推不刷新)
    attempts integer,               -- 只作运维观察
    PRIMARY KEY (asin, zip)
);
-- 三个作用:① **先落 pending 再调接口**(铁律)——旧系统没有任何防重记录,
-- 提交中途一断就丢,重启后无从对账;② **一批混邮编,同一 ASIN 的多邮编拆批**
-- (采集侧 tasks 是 UNIQUE(batch_id, asin)):批次内一个 ASIN 只可能有一个邮编,
-- 所以 batch_name 同时是取图的隔离键(落盘 `<批次名>/<asin>.png`)和排障抓手;
-- ③ 落定三层判据,谁也替代不了谁:
--    快照真出现 → done。**只有它能证明数据到了我们库里** —— 批次 completed
--                 不等于落库,中间还隔着增量导出 + product_ingest 两跳。
--    批次已落定(tasks.open==0 且 screenshots.open==0)仍无快照 → 认账 failed,
--                 原因去 /api/batches/{batch_id}/failures 拿真值写进 reason
--                 (验证码可换时段重试,variant_offset 重试也没用,处置不同)。
--    兜底超时 20 分钟 → 只打在**批次已不在途**的组合上。在途批次不判超时:
--                 采集侧正干着我们又重推一遍 = 白烧一批配额。
-- 重试窗口见 refdata/schema.sql 里 first_requested_at 那段注释(可重试一天)。

### catalog.asin_blacklist(ASIN 黑名单,收集侧)

**只收永久禁止类 B/C/E/F/G/K**(`services/blacklist.PERMANENT`;13 类词表只是
飞书来源列的格式约定,不是入选范围——所有者拍板 2026-08-11)。入选按**当轮
=最新**类别(历史实证类别翻动频繁,"曾命中过"不能作数)。一次入选不更新
(DO NOTHING)。`biz_cn` 是独立维度(中国卖家专属禁售);`pushed_at` 自 2026-08-17
起**不再是待推水位** —— blacklist_push 已改成整表重写(库里有什么表里就是什么),
这一列现在只表示「这行投影过了」,给探针与对账用(投影到「黑名单ASIN」表,PG 权威)。
另收 **category='LEGACY'**(source='历史继承'):旧审核系统随迁的历史黑名单
ASIN,经 `asin_blacklist_import` 一次性导入(2026-08-13 黑名单中心统一)。
写入方 problem_scan 尾段(2026-08-14 批次 E 前是 problem_product_cleanup) + asin_blacklist_import(一次性);
消费方:上架拦截 + 审核 Phase0 ASIN 闸(全表,不分类别)。

### ops.cleanup_seen_categories(问题商品历史:(sku, 类别) 唯一对)

旧 `seen_sku_categories.json`(20.1 万对)的落点,「错误统计」报表累计数的
唯一真值来源——报表(旧 Step 3/4/5)迁移前必须先导入,否则累计口径当场跳变。
写入方:`cleanup_history_import`(历史)+ 未来 cleanup 报表尾段(增量);
`category` 是 A~L/Z 类别码。主键 (sku, category),ON CONFLICT DO NOTHING。


CREATE TABLE ops.dedupe (           -- 通用防重记录(替代旧 cache/*.json)
    scope       text NOT NULL,      -- 如 'cleanup:submitted_sku'
    key         text NOT NULL,
    meta        jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key)
);
```

本节未逐张列出、但**确实在库里也在用**的 ops 表与视图(DDL 与注释在
`refdata/schema.sql`,别再当成「没有」而重复建表):`ops.scrape_batches`
(采集推送批次台账,product_refresh 与 order_audit 共用一张,超时口径按批次名
前缀各圈各的)、`ops.scrape_failures`(批次落定时拉 `/api/batches/{id}/failures`
的逐 ASIN 真失败,与 `snapshots.outcome` 互补)、`ops.feed_item_errors`(一条
ingestionError 一行,字段级报错聚合的燃料)、只读聚合视图 `ops.v_feed_error_stats`
与 `ops.v_scrape_failure_stats`(**零程序读者是设计如此**,留给人与 AI 排障)。

### ops.dispositions(处置建议台账:「建议」与「执行」的分界面)

两条链共用一张表:`maintenance_scan`/`problem_scan` 写建议,
`maintenance`/`problem_product_cleanup` 领取执行。DDL 与状态机注释在
`refdata/schema.sql`,交界处的五条纪律在 `services/dispositions.py` 头注,
全案在 `docs/production_cutover.md` §六·三。这里只记**读这张表时最容易读错的
三列**(2026-08-24 路由器改造后):

| 列 | 答的问题 | ⚠ |
|---|---|---|
| `source` | 谁**首先**建议的(maint/scan/audit/tro) | **不是执行者**。拿它反推谁干的,就会读出 08-19 那条「维护链执行 + 审核链原因」的记录 |
| `action` | **该谁干**:delete/retire/relist → `problem_product_cleanup`;title/price/inventory → `maintenance` | 执行件按它领取(`claim(actions=…)`),与 source 无关 |
| `executed_by` | 最终**是谁**提交的 feed | 2026-08-24 新增;此前只能靠 source 猜 |
| `detail->>'ship_node'` | 这条建议要写**哪个发货节点**(多仓批次 2) | 未配置「维护仓库」的店**不带这个键**(建议行与改造前逐字节一致,执行件走 legacy 路径)。带了就决定两件事:写通道(分节点 PUT / MP_INVENTORY feed)与落定判据(按 `catalog.item_node_inventory` 而非 `walmart_items.avail_qty`) |
| `sources` | 每个支撑来源各一格:`{来源: {action, code, reason, at}}` | 展示用的 reason/category 由 `claim()` 按它现算(单来源逐字不变,多来源拼成「维护:… \| 审核:…」);`reason`/`category` 两列是**首次建议**的病历,不再被后写方覆盖 |

未落定唯一性是 `(store, sku, action)` 的部分唯一索引 —— **动作在键里不能去掉**:
`problem_scan` 对顽固件同时建议 retire 与 delete(双 feed 齐发),合成一条会让
其中一个的落定结果覆盖另一个。「破坏类存在即压制同 SKU 的维护类」不靠索引,
靠 `claim()`(索引管不了跨行的条件,而且压制必须与两个扫描件谁先跑无关)。

## audit — 审核域(2026-08-13 批次 A,迁自 walmart-audit-system)

审核系统迁入的落库形态(全案见 `docs/audit_migration_plan.md`)。结论权威在
`catalog.products.audit_*` 五列;本 schema 存规则字典与逐次明细。

| 表 | 用途 | 数据来源/维护方 |
|---|---|---|
| `blacklist_brands` | ⚠ **退役历史快照**(2026-08-13 黑名单中心统一):品牌闸改读 catalog.brand_blacklist,本表不再被读取,留档不删 | audit_import 首灌(终态) |
| `walmart_category_map` | Amazon 类目 → Walmart PT 映射(L1 快速通道) | audit_import 首灌;后续同步链批次 B 定 |
| `phase0_blacklist_sellers/asins/amazon_cats` | ⚠ **退役历史快照**(2026-08-13):卖家/ASIN/类目三闸改读 catalog.{seller_blacklist, asin_blacklist, amazon_cat_blacklist},留档不删 | audit_import 首灌(终态) |
| `blacklist_brand_ip_stats` | 品牌 IP precision 分层(**含人工 override 三列,重算永不覆盖**) | audit_import 搬入(不可重算) |
| `violation_groundtruth` | 打标真值(批次 B 双跑校准黄金集) | audit_import 搬入 |
| `walmart_error_records` | 错误商品日报 97k 行(precision 证据 + 实证类目反哺源) | audit_import 搬入;增量同步链批次 B 定 |
| `walmart_pt_meta` / `walmart_pt_spec` / `walmart_prohibited_policy` | PT 元数据 7033 / 官方 spec 摘要 6942 / 禁售政策 43 类 | ⚠ 反推表(旧仓无 DDL):列类型按 sync 脚本推定,audit_import dry-run 与生产实表对照后才准导入。⚠ **`walmart_pt_meta` 已不是死快照**:risk_sync 每次同步 TRUNCATE + 全量重灌(services/risk_gate.sync_pt_meta,空读拒绝重灌 + 骤缩护栏),它是 R1 准入闸 / R3 认证闸唯一查的表,改前先看下面 `pt_meta_change_log` 那行;`walmart_pt_spec` 由 pt_spec_sync 重建,2026-08-21 起 R3 已不再读它 |
| `audit_runs` / `audit_hits` | 逐次审核结论 + 逐条规则命中(reject 永久短路的依据) | audit_import 搬历史;product_audit 批次 B 起追加 |
| `amazon_taxonomy` / `amazon_node_paths` | 亚马逊类目树:节点级属性按 node_id 一行 / **路径级**关系按 (node, parent, full_path) 三元组 —— browse tree 是 DAG,同一 node 可挂多个父,按 ID 去重会静默丢掉多路径 | taxonomy_import(文件段)+ taxonomy_derive(中间层反推,source=derived_products) |
| `category_path_alias` | 类目路径别名(叶子相等 + 顶级相等 + 段集重叠 ≥0.5):映射精确匹配未命中时折到 canonical 再查 | catmap_align |
| `category_map_suggestions` | 类目映射缺口建议 —— **纯建议、零消费**,人工确认后升级进 walmart_category_map 才生效 | catmap_suggest / catmap_mine |
| `pt_meta_change_log` | **PT 判据变更台账**(2026-08-21 加,只追加):`sync_pt_meta` 每次全量重灌**前**逐 PT 比对 `access_state` / `zh_can_do` / `requirements` 三列,只落真变了的(前后值都记)。存在的理由:飞书类目表一改,R1 准入闸与 R3 认证闸的判据整批换掉,而 `products.audit_version` 是**仓库侧**的规则版本号,不会因为数据变了而递增 —— 于是 `rerule` / `mode=nonpass` 那两条带版本谓词的通道对数据变更完全无感(所有者 2026-08-21 实遇:全量扫过一遍之后双双报「共 0 个」)。有了它,`product_audit -p repts=1` 按`changed_at > products.audited_at` 精确取候选,既不依赖版本号也不用人记得加开关 | risk_sync(每次重灌时写) |

**历史实证 PT 不设边表**(所有者定稿 2026-08-13):PT 是产品属性,直接回填
`catalog.products.walmart_pt`(pt_backfill 工作流:删除历史 error_items
41.7 万行 + 报错日报双源、每 ASIN 取最新、**只填空不覆盖**;库中没有的
ASIN 建占位行,title 空不进审核候选,待采集摄取填充)。审核 L1 ①b 级
直接读产品行,不查证据边表。**按 `products.pt_source` 分道**(所有者定稿
2026-08-14):`walmart_confirmed`(沃尔玛回执实证)→ L1 记
`historical_confirmed`/置信高;其余(含上一轮 LLM 结论、NULL 存量)→ 记
`audit_cached`/置信中,仍直出但不冒充实证。`catmap_mine` 只数
`walmart_confirmed` 的票——否则 LLM 猜一个 PT 会被自己反复确认,再被挖进
映射表放大到整个类目。

与旧仓的有意差异(理由见 schema.sql audit 节头注):audit_runs 不挂
products 外键;三个自增主键 GENERATED BY DEFAULT(带原 id 搬入后 setval 续接)。
不迁:products / llm_cache / sync_runs / llm_usage / llm_route_events。

## 角色与备份

```sql
CREATE ROLE readonly LOGIN PASSWORD '...(在 .env)';
GRANT USAGE ON SCHEMA catalog, listing, orders, ops, audit TO readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA catalog, listing, orders, ops, audit TO readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA catalog, listing, orders, ops, audit
    GRANT SELECT ON TABLES TO readonly;
```

备份:`backup` 工作流每日 `pg_dump -Fc walmart_data` 到 `<DATA_ROOT>/backups/`,
保留 14 天,完成/失败均发飞书通知。

> 注:`...` 处的列清单由执行 AI 在实现对应工作流时,按旧系统实际字段补全并回写本文档。


## catalog.audit_listing_conflicts(2026-08-14 新增)

**审核结论 × 上架现状的冲突面。** 所有者要求"病历里一眼看到审核结论",
让「审核拒了但还在架」「刚上架就被拒」一条 SQL 出来。

两个标志的口径差别(**别混用**):

| 标志 | 看的是 | 用途 |
|---|---|---|
| `rejected_still_listed` | **现状**:当前结论 reject 且商品此刻在架 | problem_scan 按它建"删除"建议 |
| `rejected_after_listing` | **时序**:最近一次判拒晚于最近一次上架 | 上架时那道闸没拦住(或当时还没审)= **审核链漏拦线索** |

⚠ 两件事,别当成一件:前者问"该不该下架",后者问"我们的闸为什么没拦住"。

**为什么不能只看 product_risk**:审核事件的 `store` 是 NULL(审核不分店铺),
所以它们进得了全局 `product_risk`,却进不了 `product_risk_store`
(`WHERE store IS NOT NULL`)。店铺维度的问题必须 JOIN 现状表。

**为什么本视图不依赖 product_risk**:`product_risk` 是 DROP+CREATE
(列改名的历史遗留),而 PG 不允许 DROP 一个还有依赖者的视图 —— 依赖它
等于给 `db_init` 埋一个"第二次跑就报错"的雷。所以时间线就地聚合。

### ⚠ 首版查询挂死的教训(2026-08-14 生产实遇,已修)

首版写完当天就把生产库查挂了。两个错叠在一起:

1. **表达式关联没有对应索引**。LATERAL 用 `coalesce(ev.asin, ev.sku) = ...`
   关联,而 `product_events` 当时只有 `(sku, occurred_at)` 与 `(store, sku)`
   两个建在**裸 sku** 上的索引 —— 表达式匹配不上,于是对外层每一行做一次
   几百万行的全表扫描。
   **已补** `product_events_identity_idx ON (coalesce(asin, sku), occurred_at DESC)`。
   ⚠ 表达式索引必须与查询里的表达式**逐字一致**才会被用上,改一边就得改另一边。
   本表的身份键 2026-08-11 就定稿为 `coalesce(asin, sku)`,索引却一直建在裸 sku
   上 —— 这个缺口在本视图之前没人踩到,因为别的消费方都是**一次**全表聚合
   (product_risk 那种 GROUP BY),不是逐行关联。
2. **外层 WHERE 引用了 LATERAL 的产出**(`... OR e.last_rejected_at IS NOT NULL`)
   ⇒ PG 必须先为每一行算完 LATERAL 才能过滤,一行都剪不掉。
   现在改成先用 CTE 把基集缩到"在架 且 当前判拒"(小),再去碰事件表。

顺带一个**语义收紧**:只看**当前**结论是 reject 的。曾经拒过、现在已经过了的
不是冲突(那是审核改判,正常)——首版把它们捞进来既慢又答非所问。
