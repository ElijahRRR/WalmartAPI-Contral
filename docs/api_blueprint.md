# api 层总蓝图 — 全量端点调研与设计

> 目的:把旧仓库**所有**工作流用到的沃尔玛接口一次摸清,交叉出完整的 api 层设计。
> 以后开发任何工作流,都是"拿参数调 api 层现成函数",不再改造 api 层。
> 证据来源:①旧仓库 19 路并行代码通读(每条结论带 文件:行号,旧仓库 commit d5237fb);
> ②独立 grep 全仓完备性审计;③developer.walmart.com 官方文档核验(2026-08-05);
> ④官方限速表 refdata/walmart_rate_limits.tsv(2026-04-15 抓取)。
> 详细模块级证据见 docs/legacy_survey.md;本文件是 api 层视角的汇总与设计定稿。

## 1. 全量端点清单(旧系统真实调用过的全部 24 个)

按新 api 层文件归属排列。"调用方式"标注旧系统是否已走公共层(safe_* 系),
决定迁移时自愈能力是白得的还是要新补的。

| # | 端点 | 归属 | 旧系统用途 | 调用方式 | 使用模块 |
|---|---|---|---|---|---|
| 1 | POST /v3/token | _client | 认证(全部前置) | 公共层 | 全部 |
| 2 | GET /v3/items | items | 商品列表/按状态筛选 | safe_get_ex | 7 个模块 |
| 3 | GET /v3/items/{sku} | items | 单品查询(补漏用) | safe_get_ex | auto_listing |
| 4 | GET /v3/items/count | items | 按状态统计商品数 | safe_get | 店铺日报 |
| 5 | GET /v3/items/walmart/search | items | 全站目录搜索(DEFAULT/SPEC 双格式) | safe_get_ex | 4 个模块 |
| 6 | POST /v3/items/catalog/search | items | 本店目录精确查询 | safe_post_ex | 产品查询 |
| 7 | POST /v3/items/spec | items | 拉 PT 上架模板(≤20 PT/次) | safe_post_ex | auto_listing |
| 8 | POST /v3/feeds?feedType=MP_ITEM | feeds | 新品上架 | safe_post_ex | auto_listing |
| 9 | POST /v3/feeds?feedType=MP_ITEM_MATCH | feeds | 跟卖(v4.2 旧规范) | safe_post_ex | match_listing |
| 10 | POST /v3/feeds?feedType=MP_MAINTENANCE | feeds | 改标题/属性/endDate | safe_post_ex | 4 个模块 |
| 11 | POST /v3/feeds?feedType=DELETE_ITEM | feeds | **永久删除**(最高危) | **裸 httpx×3 处** | 批量下架/问题清理 |
| 12 | POST /v3/feeds?feedType=RETIRE_ITEM | feeds | 可恢复下架 | 混合 | 问题清理/auto_listing |
| 13 | POST /v3/feeds?feedType=PRICE_AND_PROMOTION | feeds | 批量改价(6/天!) | safe_post_ex | auto_listing |
| 14 | POST /v3/feeds?feedType=price | feeds | 批量改价(旧版) | safe_post_ex | 商品维护 |
| 15 | POST /v3/feeds?feedType=inventory | feeds | 批量改库存 | safe_post_ex | 2 个模块 |
| 16 | GET /v3/feeds | feeds | feed 列表(提交防重反查/轮询) | safe_get_ex | 2 个模块 |
| 17 | GET /v3/feeds/{feedId} | feeds | feed 状态+逐 SKU 明细 | safe_get_ex | 9 个模块 |
| 18 | GET /v3/feeds/{feedId}/errorReport | feeds | 失败明细 CSV(**二进制**) | 裸 httpx | auto_listing |
| 19 | PUT /v3/price | prices | 单品改价(同步快路径) | safe_put_ex | 3 个模块 |
| 20 | PUT /v3/inventory | inventory | 单品改库存(同步快路径) | safe_put_ex | 4 个模块 |
| 21 | GET /v3/inventories | inventory | 全店库存分页(bulk) | safe_get_ex | sync_online_products |
| 22 | GET /v3/inventory?sku= | inventory | 单品库存(bulk 漏数据兜底) | safe_get_ex | sync_online_products |
| 23 | GET /v3/orders | orders | 订单增量拉取/销量统计 | **裸 httpx(async)** | 订单审核/店铺日报 |
| 24 | GET /v3/returns | returns | 售后单全量拉取 | safe_get_ex | 售后同步 |
| 25 | GET /v3/report/payment/statement | reports | 结算摘要/店铺状态/sellerId | 混合(裸×1) | 3 个模块 |
| 26 | GET /v3/report/reconreport/availableReconFiles | reports | 可下载对账账期列表 | 混合 | 2 个模块 |
| 27 | GET /v3/report/reconreport/reconFileJson | reports | 对账明细 JSON | 混合 | 2 个模块 |
| 28 | GET /v3/insights/performance/{8 项}/summary | insights | 绩效比率(8 端点) | safe_get_ex | 店铺日报 |
| 29 | GET /v3/insights/performance/{8 项}/report | insights | 问题订单明细 **xlsx 二进制** | 裸 httpx | 店铺日报 |
| 30 | GET /v3/settings/partnerprofile | settings | Partner ID(上架注入 shipNode) | safe_get_ex | auto_listing |

**预留(旧系统文档记载/规划但未实现,新 api 层留接口位):**
POST /v3/returns/{returnOrderId}/refund(售后退款,旧系统人工执行)、
GET /v3/insights/items/unpublished/items|counts(被下架商品清单,清理工作流可换用)、
GET /v3/insights/items/buybox 类(旧系统承认缺失)、DELETE /v3/items/{sku}(单品 retire,全仓未用过)、
GET /v3/items/taxonomy、GET /v3/token/detail(ping_stores 已用)。

**明确不做:** Walmart Affiliate API(非 marketplace 域,需独立资质)、
marketplacelearn.walmart.com 政策页爬虫(类目映射 pipeline 归档不迁移)、
影刀 RPA 抓前台页(保持原样,只改数据落点)。

## 2. 工作流 × 端点矩阵

计划中的每条工作流(docs/plan.md Phase 2)开工前,先查此表确认 api 层函数已就绪。

| workflow | 用到的端点(上表编号) | 备注 |
|---|---|---|
| 1 product_query | 5(DEFAULT+SPEC), 6 | 全只读,零状态 |
| 2 returns_sync | 24 | 不支持时间过滤,只能全量+本地 diff |
| 3 daily_report | 4, 2, 23, 25, 26, 27, 28, 29 | 端点最多;29 是 xlsx 二进制 |
| 4 order_audit | 23 | createdStartDate=now-179d 坑必须带上 |
| 5 upc_generator | 5(upc=) | 复用 items.search;先落库再查的防重已同构 |
| 6 maintenance | 10, 14, 15, 19, 20, 17 | 同步/feed 双路由是 services 层职责 |
| 7 daily_retire | 11, 17, (2) | DELETE_ITEM;防重走 ops.feed_log |
| 8 daily_cleanup | 2, 11, 12, 10, 25, 17 | 反补(MP_MAINTENANCE 改 endDate)+删除+停用 |
| 9 catalog_sync | 2(5 轮), 21, 22 | sync_online_products 的接口面 |
| 10 listing | 7, 8, 9, 5(SPEC), 16, 17, 18, 30, 19, 20, 13, 15, 12 | 最大;api 面在此全部收口 |
| backup | 无沃尔玛调用 | — |

## 3. 配额表(三源对照,官方已核验)

三个来源:**官方现值**(developer.walmart.com/us-marketplace/docs/rate-limiting,2026-08-05 逐条核验)、
**tsv**(refdata/walmart_rate_limits.tsv,2026-04-15)、**旧代码认知**(rate_limiter.py 登记值/注释/README)。
官方限流按 **seller(店铺)级**分配(token bucket 算法,429 超限/413 超大小);
官方旧文档入口已迁移,tsv 表头注释同步更新。

### 3.1 读端点

| 端点 | 官方现值(2026-08-05) | vs tsv/旧代码 | 定稿(写进 api 层令牌桶) |
|---|---|---|---|
| GET /v3/items | 300/min;带 query 参数 60/min;**limit 上限 1000(默认 20)**;offset ≤10000;cursor 全程不变、2 分钟过期→400 | 一致;limit=1000 生产实证被官方背书 | 带参 55/min(页间 1.1s 沿用) |
| GET /v3/items/{id} | 900/min(带参 60/min) | 一致 | 800/min,补漏单查 ≤8 并发 |
| GET /v3/items/count | 200/min(与 taxonomy 共享);status 枚举 PUBLISHED/UNPUBLISHED/SYSTEM_PROBLEM/IN_PROGRESS/ALL(**无 STAGE**) | 一致 | 180/min 共享桶 |
| GET /v3/items/walmart/search | 200/min;**SPEC 格式另有 1000/day**(新发现);DEFAULT 最多返回 40 条(旧记 20 过时);只返回 published 商品;asin 参数仅 SPEC | tsv 缺 1000/day | 180/min 桶 + SPEC 每日计数器 |
| POST /v3/items/catalog/search | 200/min(与 associations 共享) | 一致 | 与 walmart/search 分桶,180/min |
| POST /v3/items/spec | 限流表 10/min,但 Get Spec 指南写 **3 TPM/seller**(官方自相矛盾) | 旧代码 3/60s 恰与指南一致 | **3/min(保守)** |
| GET /v3/feeds 与 /{feedId} | 5000/min 共享;列表 limit≤50;明细 includeDetails 时 limit 官方两页矛盾(参考页 ≤50/指南页 1000) | 一致 | 3000/min 共享桶;明细 limit=50(保守) |
| GET /v3/feeds/{id}/errorReport | 60/hour;**官方仅支持 FITMENT 类 feed**;响应是 zip(内含 CSV);204=无错误 | 旧代码用在 MP_ITEM 上,官方现文不支持 | 50/hour;api 层限定 fitment |
| GET /v3/inventories | 200/min;单店 cursor 强制串行 | 一致 | 180/min,串行翻页 |
| GET /v3/orders | 5000/min | 一致 | 3000/min |
| GET /v3/returns | 50/min | 一致(旧 sleep1.3s≈46/min) | 46/min(沿用) |
| GET /v3/report/payment/statement | 15/min | 一致 | 12/min |
| reconreport 两端点 | reconFile 100/min(availableReconFiles 未单列) | 一致 | 80/min |
| insights performance summary/report | **1/min/端点**;unpublished items/counts **100/min**;listingQuality score 10/hour | CLAUDE.md"Insights 全部 1/分钟"**不准确** | 按端点分档登记 |
| GET /v3/settings/partnerprofile | 50/min | 一致 | 40/min + lru_cache |
| DELETE /v3/items/{sku}(单品 retire) | 900/min | 全仓未用过 | 预留登记 |

### 3.2 写端点与 feed(官方"Feed type usage limits"表,按 feedType 独立配额)

**官方无"item 类 feed 共享一个桶"的说法**——各 feedType 独立 10/hour;
唯一官方共享桶是价格三件套(PRICE_AND_PROMOTION + Legacy bulk price + bulk promo 共享 10/hour)。
docs/legacy_survey.md 的"共享桶"结论与 CLAUDE.md 相应表述据此**修正**。

| feedType / 端点 | 官方配额 | 官方大小/条数上限 | vs 旧认知 | 定稿 |
|---|---|---|---|---|
| MP_ITEM | 10/hour | 25MB;≤10000 条 | 一致(大小旧记 10MB 过时) | 8/hour |
| MP_MAINTENANCE | 10/hour | 25MB;≤10000 条 | 一致 | 8/hour |
| DELETE_ITEM | 10/hour("代码零依据"的 10/hour 现已获官方背书) | **0.4MB(400KB)**;条数未单列(按 ≤10000 推定) | 旧 100KB 字节上限过于保守但方向对 | 6/hour;单 feed ≤350KB 且 ≤2500 条 |
| RETIRE_ITEM | **官方限流表无此行;guide 页已消失**,仅存于 itembulkuploads 的 feedType 枚举 | 未知 | 旧系统在用且实际零限速 | 10/day 保守 + **迁移前实测是否仍被接受** |
| MP_ITEM_MATCH | **20/hour**(比 item 类宽一倍) | 25MB | 旧未登记 | 15/hour |
| PRICE_AND_PROMOTION | **10/hour(价格三件套共享)** | ≤10000 条;建议 1000 条/10MB | **tsv 的 6/day 是错的**(6/day 属 legacy promo feed);官方页内 promo* 行自相矛盾 | 6/day 保守沿用(官方页自相矛盾期间不放宽) |
| price(Legacy) | 10/hour(三件套共享) | 10MB | 一致 | 与 PRICE_AND_PROMOTION 同桶 |
| inventory | 10/hour | 10MB;≤10000 item/ship node | 旧 50/hr vs 10/hr 之争:**官方 10/hour** | 8/hour |
| MP_INVENTORY(BETA) | 50/hour | 1MB;多 ship node,JSON only | 旧未用;官方未废弃 inventory | 暂不用,盯 BETA 走向 |
| PUT /v3/price | 100/hour(⚠官方 Deprecation Guide 列"Price management Sunset 2026",需另行核验) | — | 一致;维护 README 的 200/min 是错的 | 80/hour |
| PUT /v3/inventory | 200/min | — | 一致 | 160/min |

**feed 轮询官方建议节奏**:INPROGRESS 时 15 分钟 → 1 小时 → 2 小时 → 此后每 4 小时;
价格 feed 至少等 5 分钟再查(SLA 15 分钟)。

## 4. 分页模型(4 种,互不兼容,api 层各自封装)

旧系统 7 个 GET /v3/items 实现里 4 套翻页写法并存,其中 1 套是 bug(fetch_my_walmart_items
只拉了第 1 页)。新 api 层把 4 种模型分别封装,调用方不接触分页细节:

1. **items 型(cursor 锚定 + offset 翻页)**:首页 nextCursor='*' 换真 token 后**全程不变**
   (是快照会话 ID 不是游标),真翻页靠 offset 递增;offset 硬上限 10000(超返 400);
   cursor 约 2 分钟过期(400→重置 '*' 重试一次);limit 生产实证 1000。
   超 10000 的部分用 GET /v3/items/{sku} 单查兜底。
   (sync_status_track.py:76-140 是唯一被生产验证的正确实现,已对拍 99,197 商品)
2. **orders 型(cursor 即 URL 后缀)**:meta.nextCursor 返回带 `?` 的完整 query 串,
   直接拼在 /v3/orders 后;单店内**必须串行**翻页。
3. **returns 型(cursor 即 query 串,需解析)**:meta.nextCursor 形如
   `?sellerId=...&limit=200&offset=200`,用 parse_qs 解析后并入 params 重发。
4. **inventories 型(透明 cursor,严格串行)**:meta.nextCursor 透明 token;
   终止**只能看 cursor 是否为空,不能看页长**(某页可能 <limit 但仍有下页,历史 bug);
   2026-05-15 起单店 cursor 强制串行;limit 上限 50。

## 5. feed 体系设计(api/feeds.py,全项目唯一 feed 通道)

旧系统 6 套 header schema、3 处裸 httpx 提交 DELETE_ITEM、防重语义七零八落。
新系统全部收口到 api/feeds.py:

### 5.1 header schema 分发表(实测值,官方 sample 不可信)

| feedType | header | version(旧系统实测在用) | item 结构 | 切片上限(实践值) |
|---|---|---|---|---|
| MP_ITEM | MPItemFeedHeader{businessUnit,locale,version} **只准 3 字段** | 5.0.20260304-22_45_32-api(完整时间戳,"5.0"拒收) | MPItem[{Visible:{PT:{}},Orderable:{}}] | 单店单 feed 打包 |
| MP_MAINTENANCE | 同上 | 同上 | 同上(Visible 可空) | 1000 条+25MB |
| MP_ITEM_MATCH | MPItemFeedHeader{processMode:REPLACE,subset:EXTERNAL,locale,sellingChannel:mpsetupbymatch,version} | 4.2(sellingChannel 制,与 v5 businessUnit 制不同套) | MPItem[{Item:{}}] | 1000 条 |
| DELETE_ITEM | ItemFeedHeader{locale,version,businessUnit}(官方示例同名,已核验) | 5.0.20250919-16_45_47-api(**仍是官方现值**) | Item[{Deletable:{sku}}] | 官方 400KB;定稿 350KB+2500 条双约束 |
| RETIRE_ITEM | RetireItemHeader{feedDate,version} | 1.0(不是 1.5;feedDate 必须真 UTC)⚠官方 guide 已消失,仅存枚举,**迁移前实测** | RetireItem[{sku}] | — |
| PRICE_AND_PROMOTION | MPItemFeedHeader | 2.0.20240126-12_25_52-api(独立版本线) | MPItem[{"Promo&Discount":{sku,price}}] | 10000 条 |
| price(旧版) | PriceHeader{version} | 1.7 **无外层包装**(加 PriceFeed 包装→ERROR) | Price[{sku,pricing[]}] | 1000 条+25MB |
| inventory | InventoryHeader{version} | 1.4 **Inventory 首字母大写**(小写→ERR_EXT_DATA_0503009) | Inventory[{sku,quantity}] | 4000 条+25MB |

version 字符串全部进 registry(不准散落硬编码),且**必须定期核对**:官方版本表约 4-6 周滚动一版,
旧仓库在用的 MP_ITEM/MP_MAINTENANCE 版本 5.0.20260304 已过时(官方当前推荐 5.0.20260608-18_15_07-api),
仅 DELETE_ITEM 的 5.0.20250919 仍是现值。来源:官方 Item spec versioning and diff reporting 页。
数值字段一律 round 到 ≤2 位小数(sanitize 兜底,Walmart 拒收 >2 位)。
endDate/日期字段必须 ISO DateTime(spec 声称 yyyy-mm-dd 实际拒收)。
MP_MAINTENANCE 官方明确限制:**COO(原产国)不可改**;必填仅 SKU+GTIN,其余可选(partial update)。

### 5.2 提交防重(三层,缺一不可)

1. **先落库**:提交前写 ops.feed_log(status=pending, payload_key=SKU 集合哈希)——
   CLAUDE.md 安全铁律,旧系统三大事故(2026-05-07 写回丢失重删、feed 重复提交)的总解。
2. **反查三态**:网络异常后 GET /v3/feeds 按 (feedType, itemsReceived 精确数, feedDate 时间窗)
   匹配"刚才那笔"→ FOUND/NOT_FOUND/UNKNOWN;NOT_FOUND 还要 30s 后二次确认(防索引滞后)。
   此能力从 MP_ITEM 专用上提为全 feedType 通用。
3. **启动对账**:进程启动时凡 feed_log 里 pending/submitted 的行,先查沃尔玛实际状态再决定补交。

### 5.3 状态轮询

- includeDetails=true 时 limit 官方两页矛盾(≤50 vs 1000),**保守按 50** offset 分页;
  终止条件双轨:`offset+len ≥ itemsReceived` 或页长 <50。
- feedId 含 `@`,必须 `quote(safe="")`。
- 终态集合(官方枚举已核验):feedStatus = RECEIVED/INPROGRESS/**PROCESSED/ERROR** 四值,
  **不存在 COMPLETE**——旧手动 CLI 认 COMPLETE 属多余但无害,编排器只认 PROCESSED/ERROR 反而正确;
  新系统按官方四值,收到未知状态**告警**而非静默"处理中"(防官方未来加值导致行卡死,C1 验证的卡死链条真实存在)。
- SKU 级映射(官方五值):SUCCESS→成功;DATA_ERROR/SYSTEM_ERROR/TIMEOUT_ERROR→失败;
  **INPROGRESS**(旧系统遗漏的官方值)→处理中。
- errorReport:官方**仅支持 FITMENT 类 feed**,响应是 zip 内含 CSV,204=无错误勿重试;
  一般 feed 的逐条错误只能走 includeDetails=true——旧系统对 MP_ITEM 调 errorReport 的用法不再可行。

### 5.4 错误码登记(registry,旧系统散落两文件)

SKU_LOCKED=ERR_EXT_DATA_0101211(解法:RETIRE→24h→新 UPC 重上)、
UPC 冲突=ERR_EXT_DATA_0101119、异步审核、可重试类、PROHIBITED 类——集中进 registry 常量。

## 6. 横切能力(api/_client.py 增强,Phase 1 落地)

Phase 0 已移植:token 缓存/每店代理/401 自愈/429 退避/连接池。还缺四块,
全部做在 _client 层,各域文件白得:

1. **per-store 令牌桶**:键=(store, bucket),bucket 支持共享桶
   (walmart/search+catalog/search+associations 同桶;feeds GET 全家同桶;
   item 类 feed POST 是否同桶以官方核验为准)。未登记的 bucket **默认拒绝而非放行**
   ——旧系统 rate_limiter 对未知键直接放行,RETIRE_ITEM 实际零限速就是这么漏的。
   自适应:消费 x-current-token-count / X-Next-Replenishment-Time
   (三格式:秒/epoch 毫秒/ISO,旧系统已踩全)。
2. **raw/binary 响应模式**:safe_get_ex(..., raw=True) 返回 bytes——
   errorReport CSV、insights xlsx、未来 reports 域全需要;旧系统因为没有这个,
   养出 2 处裸 httpx + 1 套重复退避实现。
3. **async 变体**:仅 orders 拉取需要(30+ 店并发),做在 api/orders.py 内部
   复用 _client 的 token/代理/退避语义,不另起体系(plan.md 定稿)。
4. **店铺失效语义**:401/403(经 401 自愈仍失败)→ 抛 StoreDeadError,
   调用方跳过全店而不是每页重试——旧 sync_status_track 的正确做法标准化。

## 7. 各域文件函数面(设计定稿,实现按工作流迁移进度分期)

命名规则:list_*=分页拉全量;iter_*=生成器;get_*=单对象;submit_*=写操作。
所有函数第一参数 store(dict,来自 services/stores.py),不接触凭证细节。

```
api/items.py
  list_items(store, *, published_status=None, lifecycle_status=None,
             limit=1000, max_offset=10000) -> (items, truncated)   # 分页模型1
  iter_all_items(store)          # 5 轮组合器(ACTIVE/UNPUBLISHED/SYSTEM_PROBLEM/STAGE/RETIRED)
  get_item(store, sku)           # 单查;只作补漏,禁止用于批量拿 PT(旧教训:454 SKU=8min)
  count_items(store, status)     # GET /v3/items/count
  search_walmart(store, *, query=None, upc=None, gtin=None)         # DEFAULT 格式
  search_walmart_spec(store, *, upc=None, gtin=None, asin=None)     # SPEC 格式(跟卖路由)
  catalog_search(store, field, value)                               # 本店目录(field=itemId 无效,用 sku)
  get_spec(store, product_types)  # ≤20 PT/批
api/feeds.py
  submit_feed(store, feed_type, items, **kw) -> feed_id   # 唯一提交口:schema 分发+切片+
                                                          # sanitize+ops.feed_log 防重+反查三态
  get_feed_status(store, feed_id)                         # 汇总
  iter_feed_items(store, feed_id)                         # 逐 SKU 明细(50/页自动翻)
  get_error_report(store, feed_id) -> bytes               # CSV
  find_recent_feed(store, feed_type, items_received, time_window)   # 反查
api/prices.py
  put_price(store, sku, amount)                           # 单品(100/hour,慎用)
  # 批量走 feeds.submit_feed(feed_type="price"|"PRICE_AND_PROMOTION")
api/inventory.py
  put_inventory(store, sku, qty)
  list_inventories(store)                                 # 分页模型4+单品兜底补漏内置
  get_inventory(store, sku)
api/orders.py
  iter_orders(store, *, last_modified_start, created_start=auto_179d, ...)  # 分页模型2;
                                                          # 内部 async 并发多店由 services 组织
api/returns.py
  iter_returns(store)                                     # 分页模型3;无时间过滤,全量
api/reports.py
  payment_statement(store)                                # 含 sellerId 提取 helper
  available_recon_dates(store)
  iter_recon_records(store, report_date)
api/insights.py
  performance_summary(store, metric)                      # 8 指标;204=无数据是正常语义
  performance_report(store, metric) -> bytes              # xlsx
api/settings.py
  get_partner_id(store)                                   # lru_cache
```

**明确不进 api 层的(业务规则,归 services/workflows):**
价格/库存同步 vs feed 的路由阈值、"价未变跳过"节流、上期回款 -14 天精确匹配、
反补 attempts 计数、dry-run 门禁(api 层无 dry-run 概念,cli 层强制)。

## 8. 官方核验结论(2026-08-05,7 问全部有果)与遗留问题

原 7 个待决问题的裁定:

1. **limit 上限 = 1000**(官方原文"cannot be more than 1000 entities per request",默认 20);
   旧 OpenAPI spec 的"≤50 仅 includeDetails"说法在现行官方文档中已消失(includeDetails 参数已除名)。
2. **item 类 feed 各 feedType 独立配额**(官方 Feed type usage limits 表 26 行逐条给值),
   MP_ITEM/MP_MAINTENANCE/DELETE_ITEM 各 10/hour、MP_ITEM_MATCH 20/hour;
   唯一共享桶是价格三件套 10/hour。"DELETE_ITEM 10/hour"从"零依据"变为官方背书。
3. **feedStatus 无 COMPLETE**(官方四值枚举三处文档一致);itemIngestionStatus 官方五值(含 INPROGRESS)。
4. **retire 语义**:官方 API 层面 retire = 单品 DELETE /v3/items/{sku}(900/min,"permanently retire",
   catalog 更新至多 48h);Seller Center 侧 retire 保留数据且可通过改 Site End Date 复活,
   API 侧无 reactivate 端点。DELETE_ITEM feed 官方明文"Deletions are permanent;
   需要可逆先 unpublish 或库存归零"。→ **批量下架工作流的设计选择**:
   若业务语义是"可逆下架"应走库存归零/RETIRE;若真要永久删除才走 DELETE_ITEM。
5. **枚举核验**:items/count 的 status 枚举含 SYSTEM_PROBLEM/IN_PROGRESS 但**无 STAGE**;
   getAllItems 响应 publishedStatus 文档描述 6 值(含 READY_TO_PUBLISH)但无机器可读 enum
   → api 层对未知 status 容错,不做白名单硬校验。
6. **inventory feed = 10/hour**(10MB),旧代码 50/hr 登记值是错的;MP_INVENTORY(BETA)才是 50/hour。
7. **nextCursor 官方口径与实证一致**:"cursor 在所有翻页请求中保持不变,有效 2 分钟,
   过期返回 400 Invalid Cursor";offset ≤10000 官方明文。

六路对抗验证:C1-C6 **全部 CONFIRMED**(COMPLETE 卡死链条、假 dry-run、RETIRED 跨日重删、
cursor+offset 翻页模型、DELETE_ITEM 三处同构与容量 2628 条实测、清库存不存在)。
其中 C1 的严重性因官方枚举无 COMPLETE 而降级为"防御性设计要求"。

### 遗留问题(官方文档查不到,按保守处理并择机实测)

1. RETIRE_ITEM feed 是否仍被接受(guide 已消失、限流表无行、仅存枚举)——迁移 daily_cleanup 前实测。
2. DELETE_ITEM 删除后同 SKU 能否重建/等待期(仅 1P 文档有"48h 后可重建",非 Marketplace 结论)。
3. PRICE_AND_PROMOTION 官方页内自相矛盾(主表 10/hour vs promo* 行 6/day)——保守按 6/day 配置。
4. GET /v3/feeds/{id} 明细 limit 50 vs 1000 官方两页矛盾——保守按 50。
5. **官方 Deprecation Guide 列 "Price management – Sunset 2026"**,未细化端点——
   迁移 maintenance/价格类工作流前必须单独核验,可能影响 PUT /v3/price 与 PRICE_AND_PROMOTION。
6. MP_MAINTENANCE 能否改价格/库存无官方明文——以 Get Spec 拉回的 schema 是否含相应字段为准。
7. 提交过时 spec version 的后果官方未写——registry 登记 + 每次上架季度性核对版本表。
