# api 层总蓝图 — 全量端点调研与设计

> 目的:把旧仓库**所有**工作流用到的沃尔玛接口一次摸清,交叉出完整的 api 层设计。
> 以后开发任何工作流,都是"拿参数调 api 层现成函数",不再改造 api 层。
> 证据来源:①旧仓库 19 路并行代码通读(每条结论带 文件:行号,旧仓库 commit d5237fb);
> ②独立 grep 全仓完备性审计;③developer.walmart.com 官方文档核验(2026-08-05);
> ④官方限速表 refdata/walmart_rate_limits.tsv(2026-04-15 抓取)。
> 详细模块级证据见 docs/legacy_survey.md;本文件是 api 层视角的汇总与设计定稿。

## 1. 全量端点清单(旧系统真实调用过的全部 30 个)

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
| 13 | POST /v3/feeds?feedType=PRICE_AND_PROMOTION | feeds | 批量改价(三件套共享 10/hour) | safe_post_ex | auto_listing |
| 14 | POST /v3/feeds?feedType=price | feeds | 批量改价(旧版) | safe_post_ex | 商品维护 |
| 15 | POST /v3/feeds?feedType=inventory | feeds | 批量改库存 | safe_post_ex | 2 个模块 |
| 16 | GET /v3/feeds | feeds | feed 列表(提交防重反查/轮询) | safe_get_ex | 2 个模块 |
| 17 | GET /v3/feeds/{feedId} | feeds | feed 状态+逐 SKU 明细 | safe_get_ex | 9 个模块 |
| 18 | GET /v3/feeds/{feedId}/errorReport | feeds | 失败明细 CSV(**二进制**) | 裸 httpx | auto_listing |
| 19 | PUT /v3/price | prices | 单品改价(同步快路径) | safe_put_ex | 3 个模块 |
| 20 | PUT /v3/inventory | inventory | 单品改库存(同步快路径) | safe_put_ex | 4 个模块 |
| 21 | GET /v3/inventories | inventory | 全店库存分页(bulk) | safe_get_ex | sync_online_products |
| 22 | GET /v3/inventories/{sku} | inventory | 单品库存(bulk 漏数据兜底,**全节点合计**) | safe_get_ex | sync_online_products |
| 23 | GET /v3/orders | orders | 订单增量拉取/销量统计 | **裸 httpx(async)** | 订单审核/店铺日报 |
| 24 | GET /v3/returns | returns | 售后单全量拉取 | safe_get_ex | 售后同步 |
| 25 | GET /v3/report/payment/statement | reports | 结算摘要/店铺状态/sellerId | 混合(裸×1) | 3 个模块 |
| 26 | GET /v3/report/reconreport/availableReconFiles | reports | 可下载对账账期列表 | 混合 | 2 个模块 |
| 27 | GET /v3/report/reconreport/reconFileJson | reports | 对账明细 JSON | 混合 | 2 个模块 |
| 28 | GET /v3/insights/performance/{8 项}/summary | insights | 绩效比率(8 端点) | safe_get_ex | 店铺日报 |
| 29 | GET /v3/insights/performance/{8 项}/report | insights | 问题订单明细 **xlsx 二进制** | 裸 httpx | 店铺日报 |
| 30 | GET /v3/settings/partnerprofile | settings | Partner ID(**无自建仓时**的上架 shipNode) | safe_get_ex | auto_listing |
| 31 | GET /v3/orders/{purchaseOrderId} | orders | 单单详情:**下单时间定稿的第二来源**(新单首见 / 列表值与库不一致时单查;2026-09-02 新增,旧系统未用;探针 4 实证 550 单详情全对) | safe_get_ex | order_sync |
| 32 | PUT /v3/inventories/{sku} | inventory | **按发货节点**改库存(shipNode 在 body、**部分成功语义**) | safe_put_ex | maintenance(受管仓的店,多仓批次 2) |
| 33 | GET /v3/settings/shipping/shipnodes | settings | 该店发货节点列表(校验「维护仓库」填的 FC ID) | safe_get_ex | maintenance/listing(多仓批次 1) |
| 31 | POST /v3/reports/reportRequests + GET .../{id} + GET downloadReport | reports | On-request 报表(ITEM 报表=数字 itemId 唯一批量来源,2026-08-05 新增实证;旧系统未用) | safe_post_ex/safe_get_ex + download_bytes | catalog_sync |

**预留(旧系统文档记载/规划但未实现,新 api 层留接口位):**
POST /v3/settings/shipping/shipnodes(**建仓**,人工 runbook,不自动化)、
POST /v3/returns/{returnOrderId}/refund(售后退款,旧系统人工执行)、
GET /v3/insights/items/unpublished/items|counts(被下架商品清单,清理工作流可换用)、
GET /v3/insights/items/buybox 类(旧系统承认缺失)、DELETE /v3/items/{sku}(单品 retire,全仓未用过)、
GET /v3/items/taxonomy、GET /v3/token/detail(ping_stores 已用)。

**⚠ 单仓假设(2026-08-24 由无意识默认转为有意识决策)。** 此前 inventory 域的
函数面 `(store, sku, qty)` 把 `(店铺, SKU)` 当成库存的完整主键,而沃尔玛的库存
主键是 `(店铺, SKU, 发货节点)`。这不是当初权衡过的取舍——本仓决策留痕密度极高
而此事零留痕,分节点端点连"预留"都没进(现已补上)。它成立的业务依据是
`api/settings.py:30-31` 记的那句:当时全部卖家"无实体仓 → 每店一个 Virtual
Node",一店一节点。**该依据正在失效**(所有者 2026-08-24 起自建第二个自发货仓),
改造范围/批次/官方端点形状全部定稿在 `docs/multi_node_plan.md`。
读侧已于批次 0 统一为"全节点合计"(#22 换端点);写侧已于批次 2 切换
(#32 分节点 PUT + MP_INVENTORY feed,按「维护仓库」显式路由,未配置的店
逐字节维持 legacy 单仓路径)。

**明确不做:** Walmart Affiliate API(非 marketplace 域,需独立资质)、
marketplacelearn.walmart.com 政策页爬虫(类目映射 pipeline 归档不迁移)、
影刀 RPA 抓前台页(保持原样,只改数据落点)。

## 2. 工作流 × 端点矩阵

计划中的每条工作流(docs/plan.md Phase 2)开工前,先查此表确认 api 层函数已就绪。

| workflow | 用到的端点(上表编号) | 备注 |
|---|---|---|
| 1 product_query | 5(DEFAULT+SPEC), 6 | 全只读,零状态 |
| 2 returns_sync | 24 | 支持真增量,时间窗成对下发(§4.3 实证;原记"不支持时间过滤"已于 2026-08-14 勘误) |
| 3 daily_report | 23, 25, 26, 27, 28 | 29(xlsx 二进制)已分给 perf_problems;商品三列改读 catalog.walmart_items(靠链上前置的 catalog_sync),不再自己调 4/2 |
| 4 order_sync | 23, 31 | 窗口全量重拉(缺省 days=45,不走 lastModifiedStartDate 增量,故无 179d 坑);order_audit 只读 PG/采集器,零沃尔玛调用 |
| 5 upc_sync | 无沃尔玛调用 | 号源由运营填「UPC池」表,注入/回写只走飞书 + catalog.upc_pool;计划中的 items.search 查重未实现(UPC 造号已定案不做) |
| 6 maintenance | 10, 14, 15, 19, 20, 17, (32, 33) | 同步/feed 双路由是 services 层职责;配了「维护仓库」的店走 32 + MP_INVENTORY feed(多仓批次 2) |
| 7 product_clear | 11, 12, 17 | 消费飞书「停用/删除表」:停用/下架→RETIRE_ITEM,删除或 C 列留空→DELETE_ITEM;防重走 ops.feed_log |
| 8 problem_product_cleanup | 10, 11, 12, 17 | 反补(MP_MAINTENANCE)+删除+停用;定性决策拆在 problem_scan(零沃尔玛调用),删除是否生效靠 catalog_sync 的 2 观测,本工作流不调 2/25 |
| 9 catalog_sync | 2(fast 两轮), 3(offset 超限补漏), 21, 22, 31(itemId 回填) | sync_online_products 的接口面 |
| 10 list_new | 8, 30, 16, (33) | 主链只发 MP_ITEM(+ partnerprofile;反查/延后结算用 GET /v3/feeds);上架仓 FC ID 走 33 校验(未配置店仍用 30,多仓批次 3);跟卖的 9 与 5(SPEC) 在 match_listing;7 未用(spec 读本地 <DATA_ROOT>/specs),18 不可用(见 §5.3) |
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
| GET /v3/items/walmart/search | 200/min;**SPEC 格式另有 1000/day**(新发现);DEFAULT 最多返回 40 条(旧记 20 过时);只返回 published 商品;asin 参数仅 SPEC | tsv 原缺 1000/day,本次已补 | 180/min 桶 + SPEC 每日计数器 |
| POST /v3/items/catalog/search | 200/min(与 associations 共享);⚠官方 schema 声明可选字段 itemId,**线上实测不返回**(2026-08-05 两店 195/195 含 PUBLISHED 全无;数字 itemId 唯一可靠来源=Item Search DEFAULT) | 一致 | 与 walmart/search 分桶,180/min |
| POST /v3/items/spec | 限流表 10/min,但 Get Spec 指南写 **3 TPM/seller**(官方自相矛盾) | 旧代码 3/60s 恰与指南一致 | **3/min(保守)** |
| GET /v3/feeds 与 /{feedId} | 5000/min 共享;列表 limit≤50;明细 includeDetails 时 limit 官方两页矛盾(参考页 ≤50/指南页 1000) | 一致 | 3000/min 共享桶;明细 limit=50(保守) |
| GET /v3/feeds/{id}/errorReport | 60/hour;**官方仅支持 FITMENT 类 feed**;响应是 zip(内含 CSV);204=无错误 | 旧代码用在 MP_ITEM 上,官方现文不支持 | 50/hour;api 层限定 fitment |
| GET /v3/inventories | 200/min | 一致;"单店 cursor 强制串行"是生产实证(2026-05-15 起),非官方文档结论 | 180/min,串行翻页 |
| GET /v3/orders | 5000/min | 一致 | 3000/min |
| GET /v3/orders/{purchaseOrderId} | 5000/min(tsv:119「An order」) | 新登记(2026-09-02) | 3000/min,与列表分桶(orders.get);探针 4:并发 8、550 次/69s 无 429 |
| GET /v3/returns | 50/min | 一致(旧 sleep1.3s≈46/min) | 46/min(沿用) |
| GET /v3/report/payment/statement | 15/min | 一致 | 12/min |
| reconreport 两端点 | reconFile 100/min(availableReconFiles 未单列) | 一致 | 80/min;**明细只准走 CSV 端点 reconFile**(ZIP 包,Accept: application/octet-stream,text/csv 406)——reconFileJson 每账期硬截断 1000 行且 offset 只收 0,nextOffset 是字节偏移,超千行账期必丢数据(订单中心v1 2026-08-04 实证) |
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
| RETIRE_ITEM | **官方限流表无此行;guide 页已消失**;itembulkuploads 页仍保留 feedType 枚举**及 RetireItemHeader 请求示例**(仍可用的正面证据) | 未知 | 旧系统在用且实际零限速 | **6/hour**(实际落地值:按 DELETE_ITEM 同档保守;原定稿 10/day 未进代码)+ **迁移前实测是否仍被接受** |
| MP_ITEM_MATCH | **20/hour**(比 item 类宽一倍) | 25MB | 旧未登记 | 15/hour |
| PRICE_AND_PROMOTION | **10/hour(价格三件套共享)** | 硬限 10000 条;建议 1000 条/<10MB(413 口径官方标 Not applicable) | **tsv 的 6/day 是错的**(6/day 属 legacy promo feed);官方页内 promo* 行自相矛盾 | **8/hour**(2026-08-26 三源复核:三处官方一致 10/hour;6/day 确证只挂 feedType=promo 行且本仓无该路径;promo 行内矛盾官方未修,与三件套无关) |
| price(Legacy) | 10/hour(三件套共享) | 10MB;硬限 10000 条(1000 条/<10MB 是官方 "we recommend" 建议值,2026-08-26 核) | 一致 | 与 PRICE_AND_PROMOTION 同桶 |
| inventory | 10/hour | 10MB(旧记 ≤10000 item/ship node 无美区官方出处——属 DSV 文档,2026-08-26 降级为自设批次上限) | 旧 50/hr vs 10/hr 之争:**官方 10/hour** | 8/hour |
| MP_INVENTORY | 50/hour | 1MB;多 ship node(spec 1.5,JSON only) | 旧未用;**官方站点已无 BETA 标记**(2026-08-24 核验),与 legacy inventory 中立并列,未见弃用/推荐表述 | ✅ **多仓批次 2 已实现**(2026-08-24):`build_payload` v1.5 小写 key、每 SKU `shipNodes[]`(恒单元素=受管仓)、切片 1000 条/950KB、桶 `feeds.post.MP_INVENTORY` 40/hour;缺 `ship_node` 直接报错不发。见 docs/multi_node_plan.md |
| PUT /v3/price | 100/hour(2026-08-26 复核:被弃用的是 Price management **文档族**,端点级零弃用标记、仍列 100/hour;Sunset 栏只有"2026"无月日,按无预告断供防御——断供即改走价格 feed,函数面已双轨) | — | 一致;维护 README 的 200/min 是错的 | 80/hour |
| PUT /v3/inventory | 200/min | — | 一致 | 160/min |

**feed 轮询官方建议节奏**:INPROGRESS 时 15 分钟 → 1 小时 → 2 小时 → 此后每 4 小时;
价格 feed 至少等 5 分钟再查(SLA 15 分钟)。

## 4. 分页模型(4 种,互不兼容,api 层各自封装)

旧系统 7 个 GET /v3/items 实现里 4 套翻页写法并存,其中 1 套是 bug(fetch_my_walmart_items
只拉了第 1 页)。新 api 层把 4 种模型分别封装,调用方不接触分页细节:

1. **items 型(cursor 锚定 + offset 翻页)**:首页 nextCursor='*' 换真 token 后**全程不变**
   (是快照会话 ID 不是游标),真翻页靠 offset 递增;offset 硬上限 10000 为官方明文,
   "超限返 400"是旧代码实证(官方未写明错误码);
   cursor 约 2 分钟过期(400→重置 '*' 重试一次);limit 生产实证 1000。
   超 10000 的部分用 GET /v3/items/{sku} 单查兜底。
   (sync_status_track.py:76-140 是唯一被生产验证的正确实现,已对拍 99,197 商品)
   **新系统生产实证(2026-08-05,两店对拍)**:① 某状态组合零商品时返回 **404 而非空列表**,
   必须按空轮处理;② **无参数调用返回全部状态的并集**(含 RETIRED,甚至含逐状态 5 轮
   组合覆盖不到的状态——实测多出 1 条,推断 IN_PROGRESS/ARCHIVED,即旧 5 轮配方有盲区),
   且无参限速 300/min(带参仅 60/min)。**定稿:全量扫店默认"无参全量 + RETIRED 兜底轮"**,
   逐状态 5 轮降级为对拍/回退用(api/items.py _SWEEP_MODES)。
2. **orders 型(cursor 即 URL 后缀)**:meta.nextCursor 返回带 `?` 的完整 query 串,
   直接拼在 /v3/orders 后;单店内**必须串行**翻页。
3. **returns 型(与 orders 同款,2026-08-06 实证修正)**:meta.nextCursor 形如
   `?sellerId=...&limit=200&offset=200`,**直接拼 URL 重发**(parse_qs 拆参重发
   会被服务端忽略未知参数、原样返回第一页——订单中心v1 实证,原"需解析"描述作废);
   时间过滤可用但 returnCreationStartDate/EndDate **必须成对**,只传 start 返 400
   (legacy_survey"不支持时间过滤"结论过时);同 cursor 重复出现=服务端未推进,立即停。
4. **inventories 型(透明 cursor,严格串行)**:meta.nextCursor 透明 token;
   终止**只能看 cursor 是否为空,不能看页长**(某页可能 <limit 但仍有下页,历史 bug);
   2026-05-15 起单店 cursor 强制串行;limit 上限 50。

## 5. feed 体系设计(api/feeds.py,全项目唯一 feed 通道)

旧系统 6 套 header schema、3 处裸 httpx 提交 DELETE_ITEM、防重语义七零八落。
新系统全部收口到 api/feeds.py:

### 5.1 header schema 分发表(实测值,官方 sample 不可信)

| feedType | header | version(旧系统实测在用) | item 结构 | 切片上限(实践值) |
|---|---|---|---|---|
| MP_ITEM | MPItemFeedHeader{businessUnit,locale,version} **只准 3 字段** | 5.0.20260608-18_15_07-api(2026-08-20 换版,完整时间戳,"5.0"拒收) | MPItem[{Visible:{PT:{}},Orderable:{}}] | 2000 条+24MB(单店打包后按此切片) |
| MP_MAINTENANCE | 同上 | 同上 | 同上(Visible 可空) | 1000 条+24MB |
| MP_ITEM_MATCH | MPItemFeedHeader{processMode:REPLACE,subset:EXTERNAL,locale,sellingChannel:mpsetupbymatch,version} | 4.2(sellingChannel 制,与 v5 businessUnit 制不同套) | MPItem[{Item:{}}] | 1000 条 |
| DELETE_ITEM | ItemFeedHeader{locale,version,businessUnit}(官方示例同名,已核验) | 5.0.20250919-16_45_47-api(**仍是官方现值**) | Item[{Deletable:{sku}}] | 官方 400KB;定稿 350KB+2500 条双约束 |
| RETIRE_ITEM | RetireItemHeader{feedDate,version} | 1.0(不是 1.5;feedDate 必须真 UTC)⚠官方 guide 已消失,仅存枚举,**迁移前实测** | RetireItem[{sku}] | 1000 条+350KB(按 DELETE_ITEM 同档保守) |
| PRICE_AND_PROMOTION | MPItemFeedHeader | 2.0.20240126-12_25_52-api(独立版本线) | MPItem[{"Promo&Discount":{sku,price}}] | 10000 条 |
| price(旧版) | PriceHeader{version} | 1.7 **无外层包装**(加 PriceFeed 包装→ERROR) | Price[{sku,pricing[]}] | 8000 条+9.5MB(所有者定稿 2026-08-26:官方硬限 10000 条留两成,1000 条只是官方建议值,新鲜度优先——单店整量当轮连发;单条载荷约 130B 字节远不顶 10MB。旧代码 25MB 超官方上限已收紧) |
| inventory | InventoryHeader{version} | 1.4 **Inventory 首字母大写**(小写→ERR_EXT_DATA_0503009) | Inventory[{sku,quantity}] | 4000 条+9.5MB(官方 10MB 留余量;旧代码 25MB **超官方上限**,收紧) |

version 字符串全部进 registry(不准散落硬编码),且**必须定期核对**:官方版本表约 4-6 周滚动一版(观察值,官方无更新频率承诺),
旧仓库在用的 MP_ITEM/MP_MAINTENANCE 版本 5.0.20260304 已过时(官方当前 5.0.20260608-18_15_07-api);**MP_MAINTENANCE 早已切,MP_ITEM 2026-08-20 切**。换版前用 `spec_split -p diff=1` 量过差集:PT 6951→6951 零增零减,Orderable 24→23(仅移除可选的 specProductType),顶层必填有变化的 PT 仅 48 个,**新增必填只有 center_bore、影响 1 个 PT**(轮毂中心孔径,汽车整顶级不做),其余 5 个字段全是「不再必填」。
仅 DELETE_ITEM 的 5.0.20250919 仍是现值。来源:官方 Item spec versioning and diff reporting 页。
数值字段一律 round 到 ≤2 位小数(sanitize 兜底,Walmart 拒收 >2 位)。
endDate/日期字段必须 ISO DateTime(spec 声称 yyyy-mm-dd 实际拒收)。
MP_MAINTENANCE 官方明确限制:**COO(原产国)不可改**;必填仅 SKU+GTIN,其余可选(partial update)。

### 5.2 提交防重(三层,缺一不可)

1. **先落库**:提交前写 ops.feed_log(status=pending, payload_key=SKU 集合哈希)——
   CLAUDE.md 安全铁律,旧系统三大事故(2026-05-07 写回丢失重删、feed 重复提交)的总解。
   防重只拦**在途行**(pending/submitted);终态行(done/failed)允许重占再发
   (2026-08-07 审查修正:所有者定稿"不设时间防重窗",同载荷在上一笔完结后
   重发是合法新操作——顽固 SKU 每日双 feed 重发、反补第 2 次尝试依赖此语义)。
2. **反查三态**:网络异常后 GET /v3/feeds 按 (feedType, itemsReceived 精确数, feedDate 时间窗)
   匹配"刚才那笔"→ FOUND/NOT_FOUND/UNKNOWN;NOT_FOUND 还要 30s 后二次确认(防索引滞后)。
   候选**排除 ops.feed_log 已占用的 feedId**(2026-08-07 审查修正:同尺寸兄弟切片
   会满足同一 (feedType, 条数) 指纹,不排除会把片 2 误收编到片 1 整片静默丢失)。
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
   (catalog/search+associations 同桶,walmart/search **独立桶**;feeds GET 全家同桶;
   item 类 feed POST **各 feedType 独立桶**——官方已核验,无共享)。
   未登记的 bucket **默认拒绝而非放行**
   ——旧系统 rate_limiter 对未知键直接放行,RETIRE_ITEM 实际零限速就是这么漏的。
   自适应:消费 x-current-token-count / X-Next-Replenishment-Time
   (三格式:秒/epoch 毫秒/ISO,旧系统已踩全)。
2. **raw/binary 响应模式**:落地成独立函数 safe_get_raw(...) → (status, headers, bytes)(可选 accept 覆盖),预签名 URL 另有 download_bytes(url, proxy);safe_get_ex 没有 raw 参数——
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
  iter_all_items(store, stats=None, mode="full")
      # mode='fast' = 无参全量 + RETIRED 兜底两轮(catalog_sync 缺省即此档);
      # mode='full' = 逐状态 5 轮(api 层默认值,轮里仍含 STAGE),对拍/回退用。
      # ⚠ 2026-08-14 勘误:原写 5 轮组合器且列了 STAGE —— STAGE 状态已作废
      # (§3.1/§8.5 的枚举里都没有它),逐状态 5 轮也已按 §4.1 降级为对拍/回退用。
  get_item(store, sku)           # 单查;只作补漏,禁止用于批量拿 PT(旧教训:454 SKU=8min)
  count_items(store, status)     # GET /v3/items/count
  search_walmart(store, *, query=None, upc=None, gtin=None)         # DEFAULT 格式
  search_walmart_spec(store, *, upc=None, gtin=None, asin=None)     # SPEC 格式(跟卖路由)
  catalog_search(store, field, value)                               # 本店目录(field=itemId 无效,用 sku)
  get_spec(store, product_types)  # ≤20 PT/批
api/feeds.py
  submit_feed(store, feed_type, entries, *, workflow="", defer_settle=False)
      -> [{"feed_id","count","outcome"}] 逐切片结果       # 唯一提交口:schema 分发+切片+
                                                          # sanitize+ops.feed_log 防重+反查三态
  get_feed_status(store, feed_id)                         # 汇总
  iter_feed_items(store, feed_id)                         # 逐 SKU 明细(50/页自动翻)
  get_error_report(store, feed_id) -> bytes               # CSV
  find_recent_feed(store, feed_type, items_received, window_minutes=30)   # 反查三态
  settle_deferred(store, settle)   # defer_settle=True 的延后结算:先反查后补交,最多 3 轮
api/prices.py
  put_price(store, sku, amount)                           # 单品(100/hour,慎用)
  # 批量走 feeds.submit_feed(feed_type="price");PRICE_AND_PROMOTION 尚未收录
  # (_SLICE_LIMITS 与 FEED_SPEC_VERSIONS 里都没有它,传进去直接 ValueError)
api/inventory.py
  put_inventory(store, sku, qty, ship_node=None)          # 不带 node = legacy 单仓;带 node 走端点 32(多仓批次 2,逐节点解析 status)
  list_inventory_nodes(store, expected_skus=None)         # {sku:{节点:数量}};分页模型4;单品兜底(端点 22)只在传 expected_skus 时才跑(catalog_sync 已撤接线,2026-08-28)
  list_inventories(store, expected_skus=None)             # ↑ 的求和包装({sku: 合计})
  get_inventory(store, sku)                               # 全节点合计(GET /v3/inventories/{sku})
api/orders.py
  iter_orders(store, *, created_start=None, created_end=None,
              last_modified_start=None, limit=200, stats=None)  # 分页模型2;
                                                          # 多店 async 并发在 api 内部:fetch_orders_bulk(stores, ...) 同步门面(§6.3)
  get_order(store, po) -> dict | None       # #31 单单详情(replacementInfo=true);404 返 None。
                                            # 下单时间定稿的第二来源(2026-09-02),只在新单首见/列表值与库不一致时调
api/returns.py
  iter_returns(store, *, created_start, created_end=None, limit=200)
      # 分页模型3;⚠ created_start 是**必填关键字参数**,时间窗必须成对下发
      # (只传 start 返 400,见 §4.3 实证)。本行 2026-08-14 勘误:原写
      # `iter_returns(store)  # 无时间过滤,全量`,照抄直接 TypeError。
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
2. **item 类 feed 各 feedType 独立配额**(官方 Feed type usage limits 表 26-27 行逐条给值,两路核验计数差一行),
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
6. **inventory feed = 10/hour**(10MB),旧代码 50/hr 登记值是错的;MP_INVENTORY 才是 50/hour(**已无 BETA 标记**,2026-08-24 核验;本仓桶按 40/hour 保守配)。
7. **nextCursor 官方口径与实证一致**:"cursor 在所有翻页请求中保持不变,有效 2 分钟,
   过期返回 400 Invalid Cursor";offset ≤10000 官方明文。

六路对抗验证:C1-C6 **全部 CONFIRMED**(COMPLETE 卡死链条、假 dry-run、RETIRED 跨日重删、
cursor+offset 翻页模型、DELETE_ITEM 三处同构与容量 2628 条实测、清库存不存在)。
其中 C1 的严重性因官方枚举无 COMPLETE 而降级为"防御性设计要求"。

### 8.1 补充核验(2026-08-26,「提交 5xx」专题)

起因:2026-08-24/25 连续两晚,20:00 那一轮多家店的 MP_ITEM 提交被边缘 Akamai
回「Internal Server Error - Read」。查了四页官方(feeds / api-integration-usage /
rate-limiting / error-codes),四条结论:

1. **请求体 gzip:官方一个字都没提。** 四页均无 `Content-Encoding` / 压缩相关
   记载。→ **不做**。压缩几 MB 的 JSON 本可以把上传与源站读取时间同时打下来,
   但写操作靠猜不得:万一半接受半不接受,就是一笔状态不明的提交。
   要用先向 Partner Support 问明,或在**非破坏性** feedType 上实测。
2. **5xx 的官方处置是「retry with jitter」,逐条有原文**:
   `INVALID_SYSTEM_STATE(500)` = "Retry with jitter. If repeated, open a support
   ticket with examples.";`SYSTEM_ERROR(500)` = "Confirm Content-Type and payload
   format, then retry with jitter.";`DOWNSTREAM_SYSTEM_TIME_OUT(504)` =
   "Retry with exponential backoff.";平台可用性节 = "Retry with exponential
   backoff and jitter."。退避阶梯示例 **2/4/8/16/32 秒**,"Cap retries
   (for example, 5 attempts)"。
   ⚠ **抖动是官方明写的要求**。此前 `api/feeds` 只有一个**固定 30 秒**的二次
   确认,零抖动 —— 一批同时失败的店会在 30 秒后**再次同时**打过去,把洪峰
   原样复制一遍。已按官方阶梯 + 抖动重写(`feeds._backoff`)。
3. **429 之后官方要求降速续跑**,不是停:"Sleep until `x-next-replenish-time`,
   then **resume at a lower rate**"。→ `list_new._AdaptiveGate` 的降档阶梯
   (24→16→12→8→4,只降不升)与此同源。
4. **官方明确建议拆分大 feed**(feeds 页原文):"Keep your bulk files small
   enough to process reliably. **Split very large submissions into multiple
   feeds.**" —— 而 §5.1 现行策略是 MP_ITEM「单店单 feed 打包」、切片
   `(2000 条, 24MB)` 贴着官方 25MB 上限。**这是与官方建议相左的一处**,
   所有者 2026-08-26 暂未采纳切片(先上延后结算 + 降档看效果),
   留档待议:切片同时能把单次 5xx 的影响面从"整店"降到"一片"。

### 遗留问题(官方文档查不到,按保守处理并择机实测)

1. RETIRE_ITEM feed 是否仍被接受(guide 已消失、限流表无行、仅存枚举)——迁移 daily_cleanup 前实测。
2. DELETE_ITEM 删除后同 SKU 能否重建/等待期(仅 1P 文档有"48h 后可重建",非 Marketplace 结论)。
3. **已结案**(2026-08-26 三源复核,四代理交叉裁决):价格三件套共享桶三处官方一致 = 10/hour(rate-limiting 页脚注 + update-bulk-prices 页 + update-promotional-pricing 页逐字相同);6/day 只挂 feedType=promo 行(本仓无该路径)。promo 行内矛盾(单元格 6/day vs 脚注共享 10/hour)官方仍未修,但与三件套无关。生产从 6/day 上调为 **8/hour** 留余量;⚠ 将来若引入 feedType=promo 路径,须单独按 6/day 限流,不得并入共享桶计数。
4. GET /v3/feeds/{id} 明细 limit 50 vs 1000 官方两页矛盾——保守按 50。
5. **已核**(2026-08-26):被弃用的是 "Price management" **API 族/文档集**(Status 2025-10-24,
   Sunset 栏只有光秃秃的"2026",官方至今未给月日;旧 overview 页的 EOL 08-31-2025 已过期一年
   而端点未下线,属陈旧数据)。PUT /v3/price 端点页零弃用标记、仍列 100/hour,当下可用;
   但族级迁移可能无预告带走它——防御:断供即改走价格 feed(单品 PUT 与批量 feed 已是双轨显式函数)。
6. MP_MAINTENANCE 能否改价格/库存无官方明文——以 Get Spec 拉回的 schema 是否含相应字段为准。
7. 提交过时 spec version 的后果官方未写——registry 登记 + 每次上架季度性核对版本表。
