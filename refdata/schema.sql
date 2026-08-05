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
    asin            text PRIMARY KEY,
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
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS catalog.snapshots (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
CREATE INDEX IF NOT EXISTS snapshots_asin_scraped_idx ON catalog.snapshots (asin, scraped_at DESC);

CREATE OR REPLACE VIEW catalog.latest_snapshot AS
  SELECT DISTINCT ON (asin, scrape_params) *
  FROM catalog.snapshots ORDER BY asin, scrape_params, scraped_at DESC;

-- 沃尔玛侧在线商品:每 (店铺, SKU) 一行,catalog_sync 全量扫店 upsert
-- (替代旧飞书「在线产品总表」的沃尔玛列;amz 侧数据在 products/snapshots,按 sku=asin JOIN)
CREATE TABLE IF NOT EXISTS catalog.walmart_items (
    store        text NOT NULL,
    sku          text NOT NULL,
    wpid         text,
    item_id      text,               -- walmart.com 数字商品ID(邮件/工单定位用);
                                     -- GET /v3/items 不返回,由 catalog/search 按 sku 回填;
                                     -- 行缺席后复现时重置为 NULL 触发重查(下架重上可能换 ID)
    upc          text,               -- 必须 text:前导零(旧事故教训)
    gtin         text,
    product_name text,
    shelf        text,               -- 已美化为 'A > B' 路径
    product_type text,
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

CREATE TABLE IF NOT EXISTS ops.dedupe (
    scope       text NOT NULL,      -- 如 'cleanup:submitted_sku'
    key         text NOT NULL,
    meta        jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key)
);
