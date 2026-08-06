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

-- ── orders:订单域 ─────────────────────────────────────────────────────────

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

CREATE TABLE IF NOT EXISTS ops.dedupe (
    scope       text NOT NULL,      -- 如 'cleanup:submitted_sku'
    key         text NOT NULL,
    meta        jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key)
);
