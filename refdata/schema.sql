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

-- ── 产品事件账本(2026-08-06 所有者需求:产品全生命周期追踪)────────────────
-- 一个 SKU(=ASIN,业务约定贯通)一生的病历:何时上架/何时下架及官方原因/
-- 何时提交删除/删除是否真生效/报了什么错。只追加永不改;
-- 事件码常量表在 services/product_events.py。
CREATE TABLE IF NOT EXISTS catalog.product_events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku         text NOT NULL,
    store       text,               -- 平台级事件可空
    event       text NOT NULL,      -- item_appeared/item_missing/item_reappeared/
                                    -- status_changed/delete_submitted/retire_submitted/
                                    -- {delete|retire|maintenance}_feed_{success|failed}/
                                    -- delete_verified/delete_not_effective …
    source      text NOT NULL,      -- 来源工作流
    error_code  text,
    detail      jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS product_events_sku_idx ON catalog.product_events (sku, occurred_at DESC);
CREATE INDEX IF NOT EXISTS product_events_store_sku_idx ON catalog.product_events (store, sku);

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
    -- 审核结论(order_audit 工作流写;四道审核明细进 audit_detail)
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
    submitted_at timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz,
    PRIMARY KEY (feed_id, sku)
);
CREATE INDEX IF NOT EXISTS feed_items_store_sku_idx ON ops.feed_items (store, sku);
CREATE INDEX IF NOT EXISTS feed_items_status_idx ON ops.feed_items (status);

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
