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
    avail_qty integer,                       -- GET /v3/inventories 合并
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
再把本轮未见的行标 missing_since 并清空两个状态列(不删除,保历史;
连续缺席多久后清理另议)。飞书「在线产品总表」投影只写在架行
(missing_since IS NULL),缺席商品不进表;last_seen_at/missing_since
两列也不投影(追踪在 PG 与事件账本,表只给人看在架现状)。

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
-- 风控库(L2b,2026-08-07):飞书两表镜像,闸门读库不读表(表格将停用)
-- 同步只增改不删;拦截条件:准入状态='禁售' 或 中国卖家可做 以'否'开头;
-- 品牌 casefold 精确匹配(brand_key)
CREATE TABLE catalog.risk_product_types (product_type PK, category, ptg,
    admit_status, cn_seller, cert_required, note,
    field_total, field_required, field_list, synced_at);
CREATE TABLE catalog.brand_blacklist (brand_key PK, brand, source,
    added_date, synced_at);
```

```sql
-- 产品事件账本(2026-08-06 所有者需求:产品全生命周期追踪,"病历")
CREATE TABLE catalog.product_events (
    id bigint IDENTITY PRIMARY KEY,
    sku text NOT NULL,              -- 业务约定 sku=asin,贯通两侧身份
    store text,                     -- 平台级事件可空
    event text NOT NULL,            -- 事件码唯一出处 services/product_events.py:
                                    -- item_appeared/item_missing/item_reappeared/
                                    -- status_changed(含官方下架原因)/
                                    -- {delete|retire|maintenance}_{submitted|feed_success|feed_failed}/
                                    -- delete_verified/delete_not_effective
    source text NOT NULL, error_code text, detail jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
-- 视图 catalog.product_risk:按 SKU 汇总(上架次数/删除次数/删除未生效次数/
-- 最近移除时间)——未来 listing 上架前防呆的查询入口
```

事件账本三条纪律:只追加永不改;**回执与观测分开记**(feed 回执 success 是
沃尔玛的一面之词,删除以 catalog_sync 观测核验为准——回执成功但宽限期后仍
在架 → delete_not_effective 告警,所有者实证的真实故障模式);写入点分布:
catalog_sync(观测迁移)/ feed_track(回执)/ product_clear(提交)/
未来 listing·审核(入库/审核/上架)。

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
    submitted_at timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz,
    PRIMARY KEY (feed_id, sku)
);
-- 终态由 services/feed_track 轮询回写(feed_poll 工作流全局扫,业务工作流也可
-- 单 feed 轮询);SKU 级状态权威在此,飞书驱动表的"结果"列只是投影。
-- 停用/删除/设置到期日期 + 未来的上架/改价/改库存/改标题 feed 全走这一套。

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

CREATE TABLE ops.audit_scrape (     -- 订单审核的按邮编采集台账(一个 ASIN×邮编一行)
    asin text, zip text,            -- zip = 5 位标准邮编
    batch_name text, state text,    -- pending / done / failed
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
