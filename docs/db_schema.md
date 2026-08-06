# Postgres 数据库设计

> 本机 PostgreSQL 17,库名 `walmart_data`。四个 schema,职责互不越界。
> 本文档是唯一的表结构事实来源:任何 AI 建表/改表必须同步更新这里。
> 可执行同步产物是 `refdata/schema.sql`(幂等),执行走 `python cli.py db_init`。
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
    stock_state  text,
    buybox       jsonb,
    raw          jsonb,          -- 采集器原始载荷(裁剪后)
    scraped_at   timestamptz NOT NULL,
    source_id    text            -- 采集器侧记录 ID,幂等去重用
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
    avail_qty integer,                       -- GET /v3/inventories 合并
    published_status text, lifecycle_status text, unpublished_reasons text,
    last_seen_at timestamptz NOT NULL,       -- 最近一次全量扫描见到它的时间
    missing_since timestamptz,               -- 连续缺席起点;NULL=最近一轮仍在
    created_at / updated_at,
    PRIMARY KEY (store, sku)
);
```

"整表重写"语义的 PG 等价:每轮扫完 upsert 所见行(清 missing_since),
再把本轮未见的行标 missing_since(不删除,保历史;连续缺席多久后清理另议)。

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
生成函数唯一出处:`services/order_lines.py`。

| 表 | 主键 | 内容 | 写入者 |
|---|---|---|---|
| `orders.order_lines` | order_line_id(UNIQUE po+sku) | 销售明细行:商品/状态/金额/物流/收件人 + 审核结论(audit_status/audit_detail);行号存列做展示 | 订单拉取工作流 + order_audit 回写审核 |
| `orders.return_lines` | (return_order_id, order_line_id) | 售后单行(一条 returnOrderLine 一行);行级状态实证在 returnOrderLines 内,物流在 returnLineGroups[].labels[].carrierInfoList[] | returns_sync |
| `orders.perf_events` | (po_id, metric, period) | 绩效问题订单,**逐周期累积**——同一违规在多个周期出现即多行,影响范围按 period 查询;历史累计 COUNT(DISTINCT (po_id,metric)) | 绩效同步(daily_report problems 后续并轨) |
| `orders.settlement_lines` | (order_line_id, period) | 对账明细按行×账期聚合:net/gross/product/commission + 佣金明细。gross=各行绝对值和,用于区分"净 0=全额退款"与"净 0=无金额"(实证:Sale/Refund 同期相消) | 结算同步 |

视图:
- `orders.settlement_by_line` — 跨账期合并 + 入账状态推导(net>0 已入账 / net<0 已冲销 / net=0 且 gross>0 已退款 / 其余待入账;金额 round6 吸收浮点相消误差——实证 +52.68-52.68 = 4.44e-16 会误判);
- `orders.order_center` — 订单中心主视图:销售行 LEFT JOIN 售后聚合/绩效指标聚合/入账状态(采购信息是人工域,留在飞书 bitable,不进 PG)。

完整列清单见 `refdata/schema.sql`。旧 po 级表 `orders.orders`(空表)保留待确认后删除,新代码禁止写入。

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
    name        text PRIMARY KEY,   -- 如 'order_sync:A085' / 'catalog_sync'
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- 店铺日报域(daily_report 工作流;字段语义对齐旧飞书「店铺KPI」32 列)
CREATE TABLE ops.store_kpi_daily (
    store text, data_date date, PRIMARY KEY (store, data_date),
    -- 身份/状态:seller_name+sales_status 来自影刀(空值不覆盖旧值,COALESCE);
    -- 商品三列读 catalog.walmart_items(PG 复用,不调 API);
    -- 绩效 8 率 / 结算字段 / 24h 订单窗口(中国时间 06:30 锚)/ prev_payout(-14 天规则)
    ...  -- 完整 32 列见 refdata/schema.sql
);

CREATE TABLE ops.perf_problem_orders (   -- 永久累积,首次发现日期不被覆盖
    id bigint, first_seen_date date, store text,
    sales_order_no / po_no / order_date / indicator(带 emoji 契约) /
    sub_category / accountable / description / item / carrier / tracking_no / note,
    raw jsonb,                           -- xlsx 原始行,对拍校准用
    UNIQUE (sales_order_no, indicator, sub_category, tracking_no, item)
);
-- 写入一律 INSERT ... ON CONFLICT DO NOTHING:天然实现旧系统"永久累积+全局去重+
-- 保留首次发现日期"语义,消掉旧系统清空飞书全表重写的丢数据风险

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

备份:`backup` 工作流每日 `pg_dump -Fc walmart_data` 到 `<DATA_ROOT>/backups/`,
保留 14 天,完成/失败均发飞书通知。

> 注:`...` 处的列清单由执行 AI 在实现对应工作流时,按旧系统实际字段补全并回写本文档。
