# Postgres 数据库设计

> 本机 PostgreSQL 17,库名 `walmart_erp`。四个 schema,职责互不越界。
> 本文档是唯一的表结构事实来源:任何 AI 建表/改表必须同步更新这里。
> 连接只准通过 `registry/db.py`;Metabase/NocoDB/MCP 用只读角色 `readonly`。

## Schema 总览

| schema | 职责 | 写入者 |
|---|---|---|
| `catalog` | 产品主数据:产品身份 + 采集快照(与采集/审核服务共享的中心) | catalog_sync 工作流(未来:审核服务直写审核字段) |
| `listing` | 上架域:上架任务、feed 明细、UPC 池 | listing / maintenance / upc 相关工作流 |
| `orders` | 订单域:订单、审核结果、结算、售后 | order_audit / returns_sync / settlement 相关工作流 |
| `ops` | 运行域:运行记录、防重状态、游标 | cli.py 与各工作流 |

设计原则:
- 同一业务域的表放同一 schema,跨域 JOIN 随便写,不再有跨库之苦。
- 会给人看/未来 ERP 网页端会读的数据在 catalog/listing/orders;
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
    asin            text PRIMARY KEY,
    title           text,
    brand           text,
    amazon_category text,
    image_url       text,
    slow_hash       text,        -- 慢变字段哈希:变了才需要重审
    -- 审核结论(由审核服务产出)
    audit_status    text,        -- pending / approved / rejected
    audit_reason    text,
    walmart_pt      text,        -- 映射的沃尔玛 Product Type
    audited_at      timestamptz,
    audit_version   text,        -- 审核规则版本,规则升级后可按版本批量重审
    -- 复用资产(上架过程中生成,避免重复劳动/重复消耗)
    assigned_upc    text,
    listing_attrs   jsonb,       -- LLM 映射过的属性,按 PT 版本缓存
    last_feed_id    text,
    -- 归属
    store           text,
    owner           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- 采集快照:追加不改,永不去重(快变字段)
CREATE TABLE catalog.snapshots (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asin         text NOT NULL,
    scrape_params jsonb NOT NULL DEFAULT '{}',  -- 邮编等采集参数,参与"最新值"分组
    price        numeric,
    stock_state  text,
    buybox       jsonb,
    raw          jsonb,          -- 采集器原始载荷(裁剪后)
    scraped_at   timestamptz NOT NULL,
    source_id    text            -- 采集器侧记录 ID,幂等去重用
);
CREATE UNIQUE INDEX ON catalog.snapshots (source_id);
CREATE INDEX ON catalog.snapshots (asin, scraped_at DESC);

-- 最新快照视图:每个 (asin, 参数组合) 取最新一条
CREATE VIEW catalog.latest_snapshot AS
  SELECT DISTINCT ON (asin, scrape_params) *
  FROM catalog.snapshots ORDER BY asin, scrape_params, scraped_at DESC;
```

使用约定:审核服务只关心 products(slow_hash 未变则不重审);
价格库存维护读 latest_snapshot;上架 workflow 两层 JOIN 取完整输入。

## listing — 上架域

```sql
CREATE TABLE listing.tasks (        -- 上架任务(来自飞书登记表,同步进来)
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

CREATE TABLE listing.upc_pool (     -- UPC 池:领取即永不释放(旧系统语义,必须保留)
    upc         text PRIMARY KEY,
    status      text NOT NULL,      -- available / claimed / used
    claimed_by  text,               -- store or task ref
    claimed_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
```

## orders — 订单域

```sql
CREATE TABLE orders.orders (
    po_id       text PRIMARY KEY,
    store       text NOT NULL,
    order_date  timestamptz,
    status      text,
    total       numeric,
    raw         jsonb,
    -- 审核四道结论
    audit_phishing  text, audit_purchaser text, audit_price text, audit_title text,
    audit_final     text,
    owner       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE orders.returns    ( ... 按旧售后同步的 27 列映射,主键 return_order_id );
CREATE TABLE orders.settlement ( ... 从旧 walmart_settlement.db 迁入,增量拉取 );
```

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

CREATE TABLE ops.cursors (          -- 各同步任务的增量游标(替代旧系统散落的 _meta sheet)
    name        text PRIMARY KEY,   -- 如 'order_sync:A085' / 'catalog_sync'
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ops.dedupe (           -- 通用防重记录(替代旧 cache/*.json)
    scope       text NOT NULL,      -- 如 'cleanup:submitted_sku'
    key         text NOT NULL,
    meta        jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key)
);
```

## 角色与备份

```sql
CREATE ROLE readonly LOGIN PASSWORD '...(在 .env)';
GRANT USAGE ON SCHEMA catalog, listing, orders, ops TO readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA catalog, listing, orders, ops TO readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA catalog, listing, orders, ops
    GRANT SELECT ON TABLES TO readonly;
```

备份:`backup` 工作流每日 `pg_dump -Fc walmart_erp` 到 `<DATA_ROOT>/backups/`,
保留 14 天,完成/失败均发飞书通知。

> 注:`...` 处的列清单由执行 AI 在实现对应工作流时,按旧系统实际字段补全并回写本文档。
