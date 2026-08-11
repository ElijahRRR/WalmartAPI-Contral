-- walmart_data 建库脚本(幂等,可重复执行)。
-- 事实来源是 docs/db_schema.md:改表先改文档,再同步本文件。
-- 执行方式:python cli.py db_init(唯一入口;也可 psql -d walmart_data -f 本文件)
-- orders.returns / orders.settlement 两表按文档约定留待对应工作流迁移时补全。

CREATE SCHEMA IF NOT EXISTS catalog;
CREATE SCHEMA IF NOT EXISTS listing;
CREATE SCHEMA IF NOT EXISTS orders;
CREATE SCHEMA IF NOT EXISTS ops;

-- ── catalog:产品主数据(身份与观测分离)─────────────────────────────────────

CREATE TABLE IF NOT EXISTS catalog.products (
    marketplace     text NOT NULL DEFAULT 'US',
    asin            text NOT NULL,
    title           text,
    brand           text,
    amazon_category text,
    image_url       text,
    slow_hash       text,        -- 慢变字段哈希:变了才需要重审
    audit_status    text,        -- pending / approved / rejected
    audit_reason    text,
    walmart_pt      text,        -- 映射的沃尔玛 Product Type
    audited_at      timestamptz,
    audit_version   text,        -- 审核规则版本,规则升级后可按版本批量重审
    assigned_upc    text,
    listing_attrs   jsonb,       -- LLM 映射过的属性,按 PT 版本缓存
    last_feed_id    text,
    store           text,
    owner           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (marketplace, asin)
);

CREATE TABLE IF NOT EXISTS catalog.snapshots (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    marketplace   text NOT NULL DEFAULT 'US',
    asin          text NOT NULL,
    scrape_params jsonb NOT NULL DEFAULT '{}',  -- 邮编等采集参数,参与"最新值"分组
    price         numeric,
    stock_state   text,
    buybox        jsonb,
    raw           jsonb,          -- 采集器原始载荷(裁剪后)
    scraped_at    timestamptz NOT NULL,
    source_id     text            -- 采集器侧记录 ID,幂等去重用
);
CREATE UNIQUE INDEX IF NOT EXISTS snapshots_source_id_uidx ON catalog.snapshots (source_id);

-- 迁移块(2026-08-06 拍板:products 主键 asin → (marketplace, asin);对已部署空表幂等生效)
-- ⚠ 必须先于下面依赖 marketplace 列的索引执行:旧库表已存在(CREATE IF NOT EXISTS 跳过),
--   列要靠这里补;先建索引会 UndefinedColumn(2026-08-06 生产实证)
ALTER TABLE catalog.products  ADD COLUMN IF NOT EXISTS marketplace text NOT NULL DEFAULT 'US';
-- 采集 slow 段全量留存(2026-08-09):bullet_points/description/weight/
-- dimensions/variant 都在这里。契约的 raw 是**裁剪过的**(去掉已在 slow 给过的
-- 大文本),只存 raw 会丢卖点与重量 —— 上架文案(keyFeatures minItems)与
-- ShippingWeight 都指着它。
ALTER TABLE catalog.products  ADD COLUMN IF NOT EXISTS slow jsonb;
ALTER TABLE catalog.snapshots ADD COLUMN IF NOT EXISTS marketplace text NOT NULL DEFAULT 'US';
DO $$
BEGIN
  IF (SELECT count(*) FROM information_schema.key_column_usage
      WHERE table_schema='catalog' AND table_name='products'
        AND constraint_name='products_pkey') = 1 THEN
    ALTER TABLE catalog.products DROP CONSTRAINT products_pkey;
    ALTER TABLE catalog.products ADD PRIMARY KEY (marketplace, asin);
  END IF;
END $$;

-- 契约 v1 纯追加(采集侧 2026-08-09;contract_version 仍是 1):fast 段新增
-- stock_count / delivery_days。⚠ 两列的 NULL 与 0 **不是一回事**:
-- NULL = 本次没采到;0 = 采到了且确实是 0(stock_count=0 即缺货)。
-- 下游一律不得 `or 0` 兜底(与 price 同一条原则)。
ALTER TABLE catalog.snapshots ADD COLUMN IF NOT EXISTS stock_count integer;
ALTER TABLE catalog.snapshots ADD COLUMN IF NOT EXISTS delivery_days integer;
-- 运费(采集侧 2026-08-10 纯追加,contract_version 仍是 1;存量事件也拿得到,
-- 不需要回填)。FREE→0.0 是"确认免运费"这条真信息,N/A→NULL 是"这次没采到"
-- ⇒ **落地价算不出来**。与 stock_count 同一条不变量:NULL ≠ 0,下游禁止 or 0
-- (把没采到当免运费,落地价照样算得出来、看着正常,只是偏小,两侧都不报错)。
-- shipping_raw 存原始串:出现新形态(如满额免邮门槛)时不必等契约改版。
ALTER TABLE catalog.snapshots ADD COLUMN IF NOT EXISTS shipping numeric;
ALTER TABLE catalog.snapshots ADD COLUMN IF NOT EXISTS shipping_raw text;
-- 存量行回填:两列是新加的,历史快照全是 NULL,而定价链已改成"运费没采到
-- 就不定价"⇒ 不回填的话上线当天全线停改价、停上架,直到下一轮全量重采完。
-- 值本来就在 raw.buybox_shipping 里(采集侧 _RAW_DROP 没裁它),所以能就地补。
-- 映射与采集侧 export_incremental._shipping **逐条对齐**:
--   FREE(大小写不敏感)→ 0.0 确认免运费;$5.99 → 5.99;其余(N/A/空)→ 保持
--   NULL = 没采到。**只认 'free' 这一个词**——想再认 'free shipping'/'$0.00'
--   得先去采集侧确认它真会产出,放宽的代价是把没采到的算成 0。
UPDATE catalog.snapshots SET
    shipping_raw = btrim(raw ->> 'buybox_shipping'),
    shipping = CASE
        WHEN lower(btrim(raw ->> 'buybox_shipping')) = 'free' THEN 0
        WHEN btrim(raw ->> 'buybox_shipping') ~ '^\$?[0-9]+(\.[0-9]+)?$'
            THEN replace(btrim(raw ->> 'buybox_shipping'), '$', '')::numeric
        ELSE NULL END
WHERE shipping IS NULL AND shipping_raw IS NULL
  AND raw ? 'buybox_shipping';

-- 采集结局(契约扩展字段;2026-08-09 补存):outcome ∈ ok/not_found/blocked/
-- parse_failed/stale。此前只在摄取时计数不落库,导致"这个产品为什么没数据"
-- 查不了——而这正是维护/上架链遇到数据缺口时第一个要问的问题。
ALTER TABLE catalog.snapshots ADD COLUMN IF NOT EXISTS outcome text;
ALTER TABLE catalog.snapshots ADD COLUMN IF NOT EXISTS completeness_ok boolean;
CREATE INDEX IF NOT EXISTS snapshots_outcome_idx
    ON catalog.snapshots (outcome, scraped_at DESC) WHERE outcome <> 'ok';

CREATE INDEX IF NOT EXISTS snapshots_mkt_asin_scraped_idx ON catalog.snapshots (marketplace, asin, scraped_at DESC);

CREATE OR REPLACE VIEW catalog.latest_snapshot AS
  SELECT DISTINCT ON (marketplace, asin, scrape_params) *
  FROM catalog.snapshots ORDER BY marketplace, asin, scrape_params, scraped_at DESC;

-- 沃尔玛侧在线商品:每 (店铺, SKU) 一行,catalog_sync 全量扫店 upsert
-- (替代旧飞书「在线产品总表」的沃尔玛列;amz 侧数据在 products/snapshots,按 sku=asin JOIN)
CREATE TABLE IF NOT EXISTS catalog.walmart_items (
    store        text NOT NULL,
    sku          text NOT NULL,
    wpid         text,
    item_id      text,               -- walmart.com 数字商品ID(邮件/工单定位用);
                                     -- 来源:On-request ITEM 报表(Item ID 列/Item Page URL);
                                     -- 其余路径实证排除:GET /v3/items 与 catalog/search
                                     -- 无此字段,全站搜索按 gtin/upc 召回率 3/131;
                                     -- 行缺席后复现时重置为 NULL 触发重查(下架重上可能换 ID)
    upc          text,               -- 必须 text:前导零(旧事故教训)
    gtin         text,
    product_name text,
    shelf        text,               -- 已美化为 'A > B' 路径
    product_type text,
    variant_group_id   text,         -- 变体组 ID(同组共享;listing 工作流复用)
    variant_group_info jsonb,        -- 变体组详情(isPrimary/分组维度等,原样存)
    price        numeric,
    currency     text,
    avail_qty    integer,            -- GET /v3/inventories 合并进来
    published_status    text,
    lifecycle_status    text,
    unpublished_reasons text,
    last_seen_at  timestamptz NOT NULL,   -- 最近一次全量扫描见到它的时间
    missing_since timestamptz,            -- 连续缺席起点;NULL=最近一轮仍在
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (store, sku)
);
CREATE INDEX IF NOT EXISTS walmart_items_sku_idx ON catalog.walmart_items (sku);
-- 已建库的存量表补列(幂等)
ALTER TABLE catalog.walmart_items ADD COLUMN IF NOT EXISTS item_id text;
ALTER TABLE catalog.walmart_items ADD COLUMN IF NOT EXISTS variant_group_id text;
ALTER TABLE catalog.walmart_items ADD COLUMN IF NOT EXISTS variant_group_info jsonb;
CREATE INDEX IF NOT EXISTS walmart_items_item_id_idx ON catalog.walmart_items (item_id);

-- ── 产品事件账本(2026-08-06 所有者需求:产品全生命周期追踪)────────────────
-- 一个 SKU(=ASIN,业务约定贯通)一生的病历:何时上架/何时下架及官方原因/
-- 何时提交删除/删除是否真生效/报了什么错。只追加永不改。
-- 事件码唯一出处 = services/product_events.py 的常量与 EVENTS 集合
-- (record_many 对未登记码抛错)。**本文件不再维护事件码清单**——
-- 三处清单曾各漂各的,导览请直接看那份代码。
CREATE TABLE IF NOT EXISTS catalog.product_events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku         text NOT NULL,
    store       text,               -- 平台级事件可空
    event       text NOT NULL,      -- 合法值见 services/product_events.EVENTS
    source      text NOT NULL,      -- 来源工作流
    error_code  text,
    detail      jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS product_events_sku_idx ON catalog.product_events (sku, occurred_at DESC);
CREATE INDEX IF NOT EXISTS product_events_store_sku_idx ON catalog.product_events (store, sku);

-- ── 产品来源登记簿(2026-08-07 所有者定稿)─────────────────────────────────
-- 每个上架产品登记"出身":sku=asin 约定只对 amz 搬运品成立,跟卖/自建/1688
-- 各有身份。谁上架谁登记;自动化按出身路由(路由铁律:由"源数据缺失"驱动的
-- 自动破坏动作必须限定 source_type 匹配,unknown 一律不自动动),
-- 手动通道(product_clear 表等)不受限全格式通吃。调整=UPDATE 一行数据。
CREATE TABLE IF NOT EXISTS catalog.listing_sources (
    store       text NOT NULL,
    sku         text NOT NULL,
    source_type text NOT NULL,      -- amz / match(跟卖) / self(自建) / 1688 / unknown
    source_key  text,               -- amz=asin;match=匹配 GTIN;1688=offer_id;…
    workflow    text,               -- 登记来源工作流(backfill=格式回填)
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (store, sku)
);
-- 存量一次性回填(幂等;首次注册前的行按 SKU 格式猜:ASIN 形 → amz,
-- 其余 → unknown 待人工归类。此后新上架由各工作流显式登记,不再靠格式猜)
INSERT INTO catalog.listing_sources (store, sku, source_type, source_key, workflow)
SELECT store, sku,
       CASE WHEN sku ~ '^B0[A-Z0-9]{8}' THEN 'amz' ELSE 'unknown' END,
       CASE WHEN sku ~ '^B0[A-Z0-9]{8}' THEN left(sku, 10) END,
       'backfill'
FROM catalog.walmart_items
ON CONFLICT (store, sku) DO NOTHING;

-- ── UPC 池(L2a,2026-08-07 所有者定稿:PG 权威,飞书表=注入口+投影)────
-- 领号并发安全靠单事务 FOR UPDATE SKIP LOCKED(旧系统文件锁/本地声明簿/
-- server 集中分配三层补丁全部消灭)。状态机照搬旧实证语义:
--   ''(未用)→ claimed(已领:分配未提交)→ used(已用:feed 已提交,永久消耗)
--   回收仅三类(提交前失败/双确认未达/4xx 被拒)claimed→'';Unknown 永不回收
--   conflict(全站已存在)/bad_prefix(首位非 016789 白名单)永久弃用
CREATE TABLE IF NOT EXISTS catalog.upc_pool (
    upc         text PRIMARY KEY,           -- 规范化 12 位(zfill 补前导零)
    status      text NOT NULL DEFAULT '',   -- ''/claimed/used/conflict/bad_prefix
    asin        text,                       -- 领用归属
    store       text,
    sku         text,                       -- 已用时的沃尔玛 SKU
    put_date    text,                       -- 运营注入日期(表格 B 列原样)
    claimed_at  timestamptz,
    used_at     timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS upc_pool_status_idx ON catalog.upc_pool (status);

-- ── LLM 缓存(L2c;旧 llm_cache.sqlite 462MB 的 PG 化,旧数据不迁——
-- key 含 model 名,换模型即失效)────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS catalog.llm_cache (
    input_hash  text PRIMARY KEY,    -- sha256(model+messages+温度+tokens)[:32]
    model       text NOT NULL,
    response    jsonb NOT NULL,
    hit_count   int NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_hit_at timestamptz
);

-- ── 风控库(L2b,2026-08-07 所有者定稿:两张飞书表镜像入 PG,闸门读库
-- 不读表——表格随时会停用;同步只增改不删,未来产品中心黑名单增量以
-- 脚本跑库,清理来源数据入库须清洗对应产品/店铺,归黑名单建设批次)──
CREATE TABLE IF NOT EXISTS catalog.risk_product_types (
    product_type text PRIMARY KEY,   -- Walmart Product Type
    category text, ptg text,
    admit_status text,               -- 准入状态('禁售' → 拦截)
    cn_seller text,                  -- 中国卖家可做(以'否'开头 → 拦截)
    cert_required text, note text,
    field_total text, field_required text, field_list text,
    synced_at timestamptz NOT NULL DEFAULT now()
);
-- ASIN 黑名单(收集侧,2026-08-11 所有者拍板"收"):**只收永久禁止类**
-- B/C/E/F/G/K(services/blacklist.PERMANENT),可修复类(A/D/H/I/J/L/Z)进了
-- 会误杀重上架拦截——13 类词表只是「来源」列的格式约定,不是入选范围。
-- 写入方 problem_product_cleanup 尾段(按**当轮=最新**类别入选,历史里类别
-- 翻动频繁,"曾经命中过"不能作数);消费方:上架拦截(黑名单建设批次接)。
-- pushed_at 是飞书投影水位:NULL=待推(投影到「黑名单ASIN」表,PG 权威)。
CREATE TABLE IF NOT EXISTS catalog.asin_blacklist (
    asin        text PRIMARY KEY,
    category    text NOT NULL,       -- 入选时的类别码(B/C/E/F/G/K)
    source      text NOT NULL,       -- 「沃尔玛-<类名>」,与飞书来源列同款
    reason      text,                -- 命中原因样本(截 200)
    src_store   text,                -- 溯源:在哪个店铺撞的
    biz_cn      boolean NOT NULL DEFAULT false,  -- BIZ-CN 独立维度(中国卖家
                                     -- 专属禁售,legacy_survey:2077 要求单列)
    created_at  timestamptz NOT NULL DEFAULT now(),
    pushed_at   timestamptz
);

CREATE TABLE IF NOT EXISTS catalog.brand_blacklist (
    brand_key text PRIMARY KEY,      -- casefold 匹配键
    brand text NOT NULL,             -- 品牌名原文
    source text,                     -- 来源(产品清理报错扫描+商标库比对)
    added_date text,                 -- 入库日期(表格原样)
    synced_at timestamptz NOT NULL DEFAULT now()
);
-- 收集侧补列(2026-08-11 过渡遗留):src_sku/biz_cn/pushed_at 当天曾用于
-- "自产行投影"方案,同日被渠道独立表 brand_err_hits 取代(见下),三列
-- **不再被任何代码消费**,保留仅为不破坏已建库。本表回归单一职责:
-- 总清单镜像(risk_sync 飞书→PG)+ 否决闸数据源 + cleanup 自产品牌补进闸门。
ALTER TABLE catalog.brand_blacklist ADD COLUMN IF NOT EXISTS src_sku text;
ALTER TABLE catalog.brand_blacklist ADD COLUMN IF NOT EXISTS biz_cn boolean NOT NULL DEFAULT false;
ALTER TABLE catalog.brand_blacklist ADD COLUMN IF NOT EXISTS pushed_at timestamptz;

-- 品牌·后台报错渠道表(beyKyi 投影的数据源,PG 权威):完整记录"沃尔玛
-- 后台问题商品拿到过哪些品牌"。渠道内按品牌去重(brand_key PK),
-- **不与总清单去重**——品牌已在总表不挡渠道入账(所有者厘清 2026-08-11:
-- 渠道表是归拢总表的原料,挤在 brand_blacklist 里按品牌冲突会让"总表已有
-- 的品牌"永远进不了渠道)。added_date 存报错发生日(历史重建取时间线
-- occurred_at,实时取当天)。
CREATE TABLE IF NOT EXISTS catalog.brand_err_hits (
    brand_key  text PRIMARY KEY,             -- casefold/lower 品牌键
    brand      text NOT NULL,                -- 品牌名原文
    source     text,                         -- 沃尔玛-品牌 / 沃尔玛-知产
    added_date text,                         -- 报错发生日 YYYY-MM-DD
    src_sku    text,                         -- 溯源:来自哪个 SKU
    biz_cn     boolean NOT NULL DEFAULT false,
    pushed_at  timestamptz,                  -- 飞书投影水位(NULL=待推)
    created_at timestamptz NOT NULL DEFAULT now()
);

-- 风险档案:上架前防呆的查询入口(listing 工作流用;人工 SELECT 也方便)
CREATE OR REPLACE VIEW catalog.product_risk AS
  SELECT sku,
         count(*) FILTER (WHERE event = 'item_appeared')         AS listed_times,
         count(*) FILTER (WHERE event = 'delete_submitted')      AS delete_times,
         count(*) FILTER (WHERE event = 'delete_not_effective')  AS delete_not_effective_times,
         max(occurred_at) FILTER (WHERE event IN
             ('delete_submitted', 'retire_submitted', 'item_missing')) AS last_removed_at,
         max(occurred_at) AS last_event_at
  FROM catalog.product_events GROUP BY sku;

-- ── listing:上架域 ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS listing.tasks (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asin        text NOT NULL,
    store       text NOT NULL,
    status      text NOT NULL,      -- 生命周期状态机,沿用旧 auto_listing 的 9 状态语义
    sku         text,
    upc         text,
    feed_id     text,
    error       text,
    owner       text,
    feishu_record_id text,          -- 回写飞书用
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (asin, store)
);

CREATE TABLE IF NOT EXISTS listing.upc_pool (
    upc         text PRIMARY KEY,
    status      text NOT NULL,      -- available / claimed / used
    claimed_by  text,               -- store or task ref
    claimed_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ── orders:订单域(行级统一建模,2026-08-06 v3 定稿)──────────────────────────
--   order_line_id = 'ol_' + sha256(po_id + \x1f + sku)[:24]
-- 店铺不参与身份:PO 是沃尔玛发的、平台全局唯一;店铺名是我方标签,改名/换人
-- 会作废含店铺的哈希(订单中心v1 的半成品决策,已弃)。店铺存列只做归属过滤。
-- 用 SKU 而非行号做身份(v3,项目所有者定稿):同一 PO 内同一 SKU 必合并为一行,
-- (PO,SKU) 与 (PO,行号) 同样唯一;绩效报表只给 PO+SKU 不给行号,SKU 身份使绩效
-- 事件可直接建键(订单不在库也成立)。行号存列做展示/对账。
-- 生成函数唯一出处 services/order_lines.py。

-- 一次性守卫:v2 形态(UNIQUE (po_id, line_number),行号参与哈希)→ 重建为 v3。
-- 订单/售后/对账数据窗口重拉即回(45d/90d/账期快照);perf_events 逐周期历史
-- 不可重拉,故保表不删,仅置空旧哈希的 order_line_id,由 backfill 按 v3 重算
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.constraint_column_usage
             WHERE table_schema = 'orders' AND table_name = 'order_lines'
               AND constraint_name = 'order_lines_po_id_line_number_key') THEN
    DROP VIEW IF EXISTS orders.order_center, orders.perf_event_spans,
                        orders.settlement_by_line;
    DROP TABLE IF EXISTS orders.order_lines, orders.return_lines,
                         orders.settlement_lines;
    UPDATE orders.perf_events SET order_line_id = NULL;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS orders.order_lines (   -- 销售明细(订单域锚点表)
    order_line_id  text PRIMARY KEY,
    store          text NOT NULL,
    po_id          text NOT NULL,      -- purchaseOrderId
    line_number    text NOT NULL,      -- 官方行号,text 存(与哈希输入一致)
    customer_order_id text,
    sku            text,
    product_name   text,
    qty            integer,
    sale_status    text,               -- Created/Acknowledged/Shipped/Delivered/Cancelled
    status_date    timestamptz,
    order_date     timestamptz,
    est_ship_date  timestamptz,        -- 已发货订单回退取 trackingInfo.shipDateTime(实证)
    est_delivery_date timestamptz,     -- 回退取 fulfillment.pickUpDateTime(实证)
    product_amount numeric,
    shipping_amount numeric,
    cancel_reason  text,
    refund_amount  numeric,
    refund_comments text,
    carrier        text,
    tracking_no    text,
    tracking_url   text,
    ship_name      text, phone text, address1 text, address2 text,
    city text, state text, postal_code text, country text,
    -- 审核结论(order_audit 工作流写;判定链明细进 audit_detail)
    audit_status   text,               -- 通过/拒绝/钓鱼/待人工
    audit_detail   jsonb,
    audited_at     timestamptz,
    raw            jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (po_id, sku)                -- 真正的身份锚点(PO 全局唯一,店铺/行号不参与)
);
CREATE INDEX IF NOT EXISTS order_lines_store_idx ON orders.order_lines (store);
CREATE INDEX IF NOT EXISTS order_lines_po_idx    ON orders.order_lines (po_id);
CREATE INDEX IF NOT EXISTS order_lines_sku_idx   ON orders.order_lines (sku);
CREATE INDEX IF NOT EXISTS order_lines_date_idx  ON orders.order_lines (store, order_date DESC);
CREATE INDEX IF NOT EXISTS order_lines_audit_idx ON orders.order_lines (audit_status);

CREATE TABLE IF NOT EXISTS orders.return_lines (  -- 售后单行(一条 returnOrderLine 一行)
    return_order_id text NOT NULL,     -- RMA 号
    order_line_id   text NOT NULL,
    store text NOT NULL, po_id text NOT NULL, line_number text NOT NULL,
    customer_order_id text,
    sku            text,
    return_status  text,               -- INITIATED/DELIVERED/CLOSED…(行级,实证不在顶层)
    refund_status  text,               -- NOT_REFUNDED/REFUNDED
    return_method  text,               -- SHIPPED_TO_RETURN_CENTER/KEEP_ITEM…
    refund_mode    text,               -- FIRST_SCAN/POST_DELIVERY(订单级)
    is_keep_it     boolean,
    refund_total   numeric,            -- 订单级总退款金额
    return_reason  text,
    return_comment text,
    return_by      timestamptz,
    return_created timestamptz,
    last_modified  timestamptz,
    customer_name  text,
    customer_email text,
    qty integer, refunded_qty integer,
    carrier text, tracking_no text,    -- 实证在 returnLineGroups[].labels[].carrierInfoList[]
    raw jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (return_order_id, order_line_id)   -- 同行可多次售后,同 RMA 可多行
);
CREATE INDEX IF NOT EXISTS return_lines_line_idx ON orders.return_lines (order_line_id);

CREATE TABLE IF NOT EXISTS orders.perf_events (   -- 绩效问题订单(逐周期累积)
    store    text NOT NULL,
    po_id    text NOT NULL,
    metric   text NOT NULL,            -- otd/vtr/cancellations/returns/negativeFeedback/refunds/itemNotReceived/srr
    period   text NOT NULL,            -- 报表统计周期(数据日期);同一违规多周期出现=多行,影响范围按周期查
    order_line_id text,                -- 带 SKU 的事件写入时直接按 PO+SKU 建键(v3);
                                       -- 无 SKU 的老版报表行 NULL,单行订单可回填
    sku      text,
    accountable boolean,               -- 计入绩效
    status   text,                     -- 违规/达标
    detail   jsonb,                    -- 报表原始行
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (po_id, metric, period)    -- PO 全局唯一,店铺只做归属
);
CREATE INDEX IF NOT EXISTS perf_events_store_idx ON orders.perf_events (store, metric, period);
CREATE INDEX IF NOT EXISTS perf_events_line_idx ON orders.perf_events (order_line_id);
-- 口径:当期状态取 period 最新一行;历史累计 COUNT(DISTINCT (store,po_id,metric))

-- 影响范围视图:每条违规的存续区间。still_active = 该违规出现在此(店铺,指标)
-- 最近一次报表周期中,即"仍在拖当前绩效分";消失即代表滚出官方统计窗口——
-- 各指标窗口长短官方未一一公开,以报表自身是否还包含该单为准,不自行推算窗口
CREATE OR REPLACE VIEW orders.perf_event_spans AS
  SELECT e.store, e.po_id, e.metric,
         min(e.order_line_id) AS order_line_id,
         min(e.period)  AS first_period,
         max(e.period)  AS last_period,
         count(*)       AS periods_seen,
         bool_or(e.accountable) AS ever_accountable,
         (max(e.period) = m.latest_period) AS still_active
  FROM orders.perf_events e
  JOIN (SELECT store, metric, max(period) AS latest_period
        FROM orders.perf_events GROUP BY store, metric) m
    USING (store, metric)
  GROUP BY e.store, e.po_id, e.metric, m.latest_period;

CREATE TABLE IF NOT EXISTS orders.settlement_lines (  -- 对账明细(行级×账期聚合)
    order_line_id text NOT NULL,
    period        text NOT NULL,       -- 账期 MMDDYYYY
    store text NOT NULL, po_id text NOT NULL, line_number text NOT NULL,
    net_amount     numeric,            -- 该账期该行全部类型金额合计(round6 吸浮点误差)
    gross_amount   numeric,            -- 各行 |Amount| 之和:区分"净0=全额退款"与"净0=无金额"
    product_amount numeric,            -- Amount Type = 'Product Price'
    commission_amount numeric,         -- Amount Type = 'Commission on Product'(负值)
    commission_rate numeric, original_commission numeric, commission_saving numeric,
    incentive     text,
    settle_date   date,
    raw           jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (order_line_id, period)
);
CREATE INDEX IF NOT EXISTS settlement_lines_store_idx ON orders.settlement_lines (store, period);

-- 跨账期合并视图:入账状态由净额+交易额判定(订单中心v1 实证规则:
-- net>0 已入账;net<0 已冲销;net=0 且 gross>0 已退款(Sale/Refund 相消);其余待入账)
CREATE OR REPLACE VIEW orders.settlement_by_line AS
  SELECT order_line_id,
         min(store) AS store, min(po_id) AS po_id, min(line_number) AS line_number,
         round(sum(net_amount), 6)     AS net_amount,
         round(sum(gross_amount), 6)   AS gross_amount,
         round(sum(product_amount), 6) AS product_amount,
         round(sum(commission_amount), 6) AS commission_amount,
         count(*)          AS periods,
         max(settle_date)  AS last_settle_date,
         CASE WHEN sum(net_amount) > 0 THEN '已入账'
              WHEN sum(net_amount) < 0 THEN '已冲销'
              WHEN sum(gross_amount) > 0 THEN '已退款'
              ELSE '待入账' END AS settle_status
  FROM orders.settlement_lines GROUP BY order_line_id;

-- 订单中心主视图:一行订单行 = 销售 + 售后 + 绩效 + 入账(采购在飞书,人工域不进 PG)
CREATE OR REPLACE VIEW orders.order_center AS
  SELECT l.*,
         r.return_status, r.refund_status_agg AS refund_status, r.return_total,
         p.metrics AS perf_metrics,
         s.net_amount AS settled_net, s.settle_status
  FROM orders.order_lines l
  LEFT JOIN (SELECT order_line_id,
                    string_agg(DISTINCT return_status, ';') AS return_status,
                    string_agg(DISTINCT refund_status, ';') AS refund_status_agg,
                    sum(refund_total) AS return_total
             FROM orders.return_lines GROUP BY order_line_id) r USING (order_line_id)
  LEFT JOIN (SELECT order_line_id, string_agg(DISTINCT metric, ';') AS metrics
             FROM orders.perf_events WHERE order_line_id IS NOT NULL
             GROUP BY order_line_id) p USING (order_line_id)
  LEFT JOIN orders.settlement_by_line s USING (order_line_id);

-- 旧表(po 级,空表,由 order_lines 取代):保留待确认后删除,新代码禁止写入
CREATE TABLE IF NOT EXISTS orders.orders (
    po_id       text PRIMARY KEY,
    store       text NOT NULL,
    order_date  timestamptz,
    status      text,
    total       numeric,
    raw         jsonb,
    audit_phishing  text,
    audit_purchaser text,
    audit_price     text,
    audit_title     text,
    audit_final     text,
    owner       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ── ops:运行域(状态与业务同库,可同事务修改)────────────────────────────

CREATE TABLE IF NOT EXISTS ops.feishu_sync_state (
    -- 飞书投影同步状态:键 → record_id + 上次写入指纹(order_center_push)
    -- 日常同步零拉表:本地比指纹定位要写的行;状态缺失/写失败时全量拉表重建
    table_id    text NOT NULL,          -- 飞书 table_id
    row_key     text NOT NULL,          -- 行去重键(order_line_id / 唯一键 / perf_key)
    record_id   text NOT NULL,          -- 飞书行内部编号(更新按它定位)
    pushed_hash text,                   -- 上次写入飞书时的载荷指纹
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_id, row_key)
);

CREATE TABLE IF NOT EXISTS ops.runs (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow    text NOT NULL,
    params      jsonb,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    status      text NOT NULL,      -- running / success / failed
    summary     text,               -- run() 返回的结果摘要
    operator    text                -- launchd / manual / web / mcp
);

CREATE TABLE IF NOT EXISTS ops.feed_log (
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
CREATE UNIQUE INDEX IF NOT EXISTS feed_log_dedupe_uidx ON ops.feed_log (feed_type, store, payload_key);
-- 启动对账:凡 status='pending'/'submitted' 的行,先查 Walmart 实际 feed 状态再决定补交

CREATE TABLE IF NOT EXISTS ops.feed_items (
    -- feed 的 SKU 级台账(所有 feed 操作共用):提交时落行,feed_poll 轮询落终态。
    -- SKU 级状态的权威在此,飞书各驱动表的"结果"列只是投影(2026-05-07 教训)
    feed_id     text NOT NULL,
    sku         text NOT NULL,
    workflow    text NOT NULL,
    store       text NOT NULL,
    feed_type   text NOT NULL,
    status      text NOT NULL,     -- submitted / success / failed / missing(明细里查无此 SKU)
    error_code  text,
    error_desc  text,               -- 沃尔玛给的人话描述(+字段名):光有数字码
                                    -- 无法诊断(2026-08-09 首跑 DATA_ERROR 教训)
    submitted_at timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz,
    PRIMARY KEY (feed_id, sku)
);
ALTER TABLE ops.feed_items ADD COLUMN IF NOT EXISTS error_desc text;

-- 采集推送批次台账(所有者定稿 2026-08-09)。**两条工作流共用一张表**:
-- product_refresh 的全量重推批次(`wm-refresh-*`)与 order_audit 的按邮编
-- 批次(`wm-audit-<邮编>-*`)。两边超时口径不同(1 小时 vs 20 分钟),
-- 所以**查在途必须按批次名前缀圈自己的**——不过滤的话,维护链的 check
-- 会拿 1 小时口径把订单审核那些还在跑的批次标成 timeout。
-- 推送前先落账,**确认推上去了(拿到 batch_id)才开始计时**;batch_id 必须
-- 当场记下:/api/batches/{id}/failures 只认 id 不认名字,漏记就再也查不出
-- "这个 ASIN 为什么没采到"。
-- 落定判据统一在 services/scrape_batches.is_settled(采集侧 2026-08-10 实测):
--     completed ⇔ tasks.open == 0 AND screenshots.open == 0   (failed 算终态)
CREATE TABLE IF NOT EXISTS ops.scrape_batches (
    batch_name  text PRIMARY KEY,
    batch_id    text,               -- 采集侧 ID(200 新建 / 409 既有,都记)
    asin_count  integer NOT NULL,
    status      text NOT NULL,      -- pushed / running / completed / failed / timeout
    done        integer, failed integer,   -- 采集侧 stats 快照
    submitted_at timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    note        text
);
CREATE INDEX IF NOT EXISTS scrape_batches_status_idx
    ON ops.scrape_batches (status, submitted_at DESC);

-- 订单审核的按邮编采集台账(order_audit 用;一个 (ASIN, 邮编) 一行)。
-- 存在的三个理由:
-- ① **先落 pending 再调接口**(CLAUDE.md 铁律)——旧系统完全没有防重记录,
--    提交/回填中途一断就丢,重启后没有 pending 可对账(legacy_survey 明列此洞);
-- ② **一批可以混不同 ASIN 的不同邮编**(采集侧 items[].zip_code 逐 ASIN 带),
--    只有**同一 ASIN 的多个邮编**必须拆批(tasks 是 UNIQUE(batch_id, asin),
--    同批会回 400 conflicting_zip_for_asin)。所以批次内一个 ASIN 只可能有
--    一个邮编,(batch_name, asin) 已唯一定位一个 (ASIN,邮编)——batch_name
--    因此既是取图的抓手(落盘 <批次名>/<asin>.png)也是排障的抓手。
--    取数与批次无关,只走 /api/export/incremental 按 scrape_params.zipcode 分组。
-- ③ 落定三层判据(见 workflows/order_audit._settle_ledger):快照真出现 → done;
--    批次已落定仍无快照 → 认账 failed 并去 /failures 拿真实原因;
--    兜底超时 20 分钟只打在批次已不在途的组合上(在途批次不判超时,
--    否则采集侧正干着我们又重推一遍 = 白烧一批配额)。
--    **批次 completed 不等于我们库里有数据**:中间隔着增量导出 + product_ingest
--    两跳,所以"这条到没到"只能看快照,批次状态只回答"还要不要等"。
CREATE TABLE IF NOT EXISTS ops.audit_scrape (
    asin        text NOT NULL,
    zip         text NOT NULL,      -- 5 位标准邮编
    batch_name  text,
    state       text NOT NULL,      -- pending / done / failed
    reason      text,
    requested_at timestamptz NOT NULL DEFAULT now(),   -- 最近一次推送
    settled_at  timestamptz,
    PRIMARY KEY (asin, zip)
);
-- error_type 单独成列(不埋在 reason 文本里):判定链要按它分流——
-- RETRYABLE 的换时段重采可能就好了;variant_offset / parse_error /
-- server_reject 这类重采多少次都一样,这个 ASIN 的数据**永远拿不到**,
-- 该给终局结论(建议拒绝)并且不再重推。靠 LIKE '%variant_offset%' 去猜
-- 文本迟早对不上,而对不上的后果是每轮为一个采不出来的 ASIN 白烧一次配额。
ALTER TABLE ops.audit_scrape ADD COLUMN IF NOT EXISTS error_type text;
CREATE INDEX IF NOT EXISTS audit_scrape_state_idx
    ON ops.audit_scrape (state, requested_at);
-- 重试窗口(所有者定稿 2026-08-10「可重试一天」):有快照但缺关键信息的组合
-- 会被反复重采,得有个头——first_requested_at 记这一轮重试**从什么时候开始**
-- (重推时不刷新),超一天仍拿不到可用数据就放弃,免得一个采不出来的 ASIN
-- 每小时白烧一次配额。**新需求会开新一轮**:上次推送已过期(超新鲜度窗口)
-- 说明不是同一轮死磕,窗口重置。attempts 只作运维观察用。
ALTER TABLE ops.audit_scrape
    ADD COLUMN IF NOT EXISTS first_requested_at timestamptz;
ALTER TABLE ops.audit_scrape
    ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;
UPDATE ops.audit_scrape SET first_requested_at = requested_at
    WHERE first_requested_at IS NULL;

-- 采集失败明细(product_refresh 批次落定时拉;与 feed_item_errors 同款思路:
-- **拉详情是标准动作**)。采集侧 /api/batches/{id}/failures 的落库形态:
-- 真失败(captcha/404/timeout/blocked)在这里,降级采集(outcome≠ok)在
-- snapshots.outcome —— 两者互补,前者"根本没采到",后者"采到了但不完整"。
CREATE TABLE IF NOT EXISTS ops.scrape_failures (
    batch_name  text NOT NULL,
    asin        text NOT NULL,
    status      text,
    error_type  text,               -- captcha / not_found / timeout / blocked …
    error_detail text,
    retry_count integer,
    occurred_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_name, asin)
);
CREATE INDEX IF NOT EXISTS scrape_failures_asin_idx
    ON ops.scrape_failures (asin, recorded_at DESC);
CREATE INDEX IF NOT EXISTS scrape_failures_type_idx
    ON ops.scrape_failures (error_type, recorded_at DESC);

-- 采集失败排行 + 顽固失败(连续多批采不到的 ASIN = 该下架或人工看的信号)
CREATE OR REPLACE VIEW ops.v_scrape_failure_stats AS
  SELECT error_type,
         count(*) AS 次数, count(DISTINCT asin) AS 影响ASIN数,
         max(recorded_at) AS 最近, min(error_detail) AS 明细样本
  FROM ops.scrape_failures GROUP BY error_type ORDER BY count(*) DESC;

-- feed 报错明细:一条 ingestionError 一行。**拉详情是标准动作,不是排障时才做**
-- (所有者定稿 2026-08-09):报错是系统自我优化的燃料——哪个字段最常被拒、
-- 哪个 PT 最难过、改完有没有变好,全靠这张表聚合;只存一个错误码等于把线索扔了。
CREATE TABLE IF NOT EXISTS ops.feed_item_errors (
    feed_id     text NOT NULL,
    sku         text NOT NULL,
    seq         integer NOT NULL,   -- 同一 SKU 的第几条报错(沃尔玛一次可给十几条)
    error_type  text,               -- DATA_ERROR / SYSTEM_ERROR / TIMEOUT_ERROR
    code        text,
    field       text,               -- 出错字段名(聚合分析的主维度)
    description text,
    feed_type   text,
    store       text,
    workflow    text,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (feed_id, sku, seq)
);
CREATE INDEX IF NOT EXISTS feed_item_errors_field_idx
    ON ops.feed_item_errors (field, occurred_at DESC);
CREATE INDEX IF NOT EXISTS feed_item_errors_code_idx
    ON ops.feed_item_errors (code, occurred_at DESC);

-- 报错排行:改哪个字段收益最大,一眼可见
CREATE OR REPLACE VIEW ops.v_feed_error_stats AS
  SELECT feed_type, field, code,
         count(*)                      AS 次数,
         count(DISTINCT sku)           AS 影响SKU数,
         min(occurred_at)              AS 首次,
         max(occurred_at)              AS 最近,
         min(description)              AS 描述样本
  FROM ops.feed_item_errors
  GROUP BY feed_type, field, code
  ORDER BY count(*) DESC;
CREATE INDEX IF NOT EXISTS feed_items_store_sku_idx ON ops.feed_items (store, sku);
CREATE INDEX IF NOT EXISTS feed_items_status_idx ON ops.feed_items (status);

-- 两类行,别混:
--   'product_ingest'          增量导出游标(只在有新数据时前进)
--   'product_ingest:last_run' 摄取水位线(**每轮跑完都刷,哪怕 0 条**)
-- 分开是因为 order_audit 要问的不是"取到第几条",而是
-- **"我有没有资格说'这条数据没到'"**:采集侧批次 completed 只说明它干完了,
-- 数据还在增量流里。摄取没追上就断言"无快照 ⇒ 采集失败",会把采成功的整批
-- 冤枉掉(2026-08-10 实测:127 个组合全判失败,紧接着一次 product_ingest
-- 就把这 127 条全摄了进来),而 failed 不挡重推 ⇒ 下一轮再采一遍,每小时白烧。
CREATE TABLE IF NOT EXISTS ops.cursors (
    name        text PRIMARY KEY,   -- 如 'order_sync:A085' / 'catalog_sync'
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- 店铺日报 KPI:每 (店铺, 日期) 一行,32 列语义对齐旧飞书「店铺KPI」表
CREATE TABLE IF NOT EXISTS ops.store_kpi_daily (
    store            text NOT NULL,
    data_date        date NOT NULL,
    seller_name      text,           -- 影刀前台抓取(可 stale 补);无则空
    partner_id       text,
    seller_id        text,
    store_status     text,
    payment_status   text,
    sales_status     text,           -- 影刀前台抓取;不新鲜宁可留空不回填(旧事故规则)
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
    prev_payout      numeric,        -- 严格 -14 天账期,无则 0(业务规则)
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (store, data_date)
);

-- 绩效问题订单:永久累积,五字段唯一键,首次发现日期永不被覆盖(ON CONFLICT DO NOTHING)
CREATE TABLE IF NOT EXISTS ops.perf_problem_orders (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    first_seen_date date NOT NULL,
    store           text NOT NULL,
    sales_order_no  text NOT NULL DEFAULT '',
    po_no           text,
    order_date      text,
    indicator       text NOT NULL DEFAULT '',   -- 带 emoji 前缀(下游匹配契约)
    sub_category    text NOT NULL DEFAULT '',
    accountable     text,                        -- "✅ 是" / "⚪ 否"
    description     text,
    item            text NOT NULL DEFAULT '',
    carrier         text,
    tracking_no     text NOT NULL DEFAULT '',
    note            text,
    raw             jsonb,                       -- xlsx 原始行(对拍校准用)
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (sales_order_no, indicator, sub_category, tracking_no, item)
);
CREATE INDEX IF NOT EXISTS perf_problem_orders_store_idx
    ON ops.perf_problem_orders (store, first_seen_date DESC);

-- 问题商品历史:(sku, 类别) 唯一对 —— 旧 seen_sku_categories.json(20.1 万对)
-- 的落点,是「错误统计」报表累计数的唯一真值来源。报表(旧 Step 3/4/5)迁移
-- 前必须先导入,否则累计口径当场跳变。写入方 cleanup_history_import(历史)
-- + 未来 problem_product_cleanup 的报表尾段(增量);category 是 A~L/Z 类别码。
CREATE TABLE IF NOT EXISTS ops.cleanup_seen_categories (
    sku         text NOT NULL,
    category    text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sku, category)
);

CREATE TABLE IF NOT EXISTS ops.dedupe (
    scope       text NOT NULL,      -- 如 'cleanup:submitted_sku'
    key         text NOT NULL,
    meta        jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key)
);
