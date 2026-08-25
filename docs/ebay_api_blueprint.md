# eBay api 层蓝图 — 端点调研与设计定稿

> 目的:eBay 侧任何工作流开工前,先在这里确认端点/配额/认证/提交台账形态已就绪;之后只是"拿参数调 api 层现成函数",不再改造 api 层。本文是 `docs/api_blueprint.md` 的 eBay 版,逐节同构。分工:本文只管"接口怎么调对"(铁律 2),"该不该调"归 `docs/ebay_plan.md` 与 services 层。
> 证据:2026-08-25 五路 eBay 官方调研(W1 认证 / W2 上架 / W3 订单售后结算 / W4 配额限流 / W5 风控合规)+ 仓库工程契约底稿 R4。**每个硬数据(配额/上限/有效期/批量大小)带 [verified] 或标 ⚠未核验;⚠未核验项集中在 §8.3,动手前必须补核。**
>
> **取证方法论(放最前面,省下一轮的时间)**:`curl` 对 developer.ebay.com **全域 403**(eBay 侧 Akamai 拦爬,返回 eBay 自家错误页,**不是代理问题**),只能用 WebFetch;`/api-docs/**/resources/**/methods/**` 与 `/types/**` 是 Angular SPA 壳抓不到正文;方法级细节**只能**从 `/api-docs/master/<域>/<api>/openapi/3/*_oas3.json` 取,大 spec 会被从尾部截断(`components.schemas` 基本拿不到),截掉的部分改用同 API 的 `static/release-notes-archive.html` 补。⚠ **WebFetch 摘要器在截断处会补脑编造**(实测把 Finances 的 tokenUrl 编成 `api.ebay.com/oauth/authorize`,与三份能完整读到的 spec 冲突),故关键数字一律要求两个独立来源交叉命中才记 verified。
>
> ⚠ **先于技术的阻塞项**:本项目「亚马逊采集 → eBay 上架 → 出单后从亚马逊下单直发买家」逐字命中 eBay 明文禁令——"Listing an item on eBay and then purchasing the item from another retailer or marketplace that ships directly to your customer is not allowed on eBay." [verified ebay.com/help/.../drop-shipping?id=4176],且与多账号连坐条款 [id=4232] 相乘(一店受限,关联店可能同批受限)。**所有者书面拍板前,eBay 上架链不得起调度。** 本文端点面在"换货源合规化"与"明知违规只控爆炸半径"两条出路下都成立,但"起不起调度"不是 api 层能定的。

## 1. 端点清单(46 组 / 63 个端点,只列本项目工作流会用到的)

host 逐族不同见 §4.4,scope 见 §4.3。**收录规则同沃尔玛蓝图:只实现 §2 矩阵出现过的端点,一个端点一个函数,分页/切片/防重藏在函数内。**

| # | 族 · scope · 归属文件 | 方法 + 路径 | 用途与关键约束 |
|---|---|---|---|
| 1 | OAuth · — · `_client` | POST /identity/v1/oauth2/token | 三个 `grant_type`:`client_credentials`(应用令牌)/ `authorization_code`(首次换码)/ `refresh_token`(刷新) |
| 2 | OAuth · — · `_client` | GET auth.ebay.com/oauth2/authorize | 卖家浏览器同意页。**非代码调用**,api 层只提供 URL 构造器(`redirect_uri` 填 **RuName** 不是真 URL) |
| 3 | Account · `sell.account` · `account.py` | POST /program/opt_in;GET /program/get_opted_in_programs | opt-in `SELLING_POLICY_MANAGEMENT`(不 opt-in 就没有商务政策)+ 幂等校验 |
| 4 | Account · `sell.account` | POST /fulfillment_policy;GET /fulfillment_policy?marketplace_id= | 建物流政策 + 回读 policyId 落库 |
| 5 | Account · `sell.account` | POST /payment_policy;GET /payment_policy | 收款政策 |
| 6 | Account · `sell.account` | POST /return_policy;GET /return_policy | 退货政策(TRS+ 要 30 天免费退,与定价联动) |
| 7 | Account · `sell.account` | GET /privilege | `sellingLimit` = **上限**,不是余量;另给注册是否完成 |
| 8 | Inventory · `sell.inventory` · `account.py` | POST /location/{merchantLocationKey};GET /location | 建库存地点 + 回读。**缺 merchantLocationKey 时 createOffer 不报错、publishOffer 必失败** |
| 9 | Taxonomy · `api_scope`(应用令牌)· `taxonomy.py` | GET /get_default_category_tree_id?marketplace_id= | 拿 treeId + `categoryTreeVersion`(**每日 1 次的版本哨兵**) |
| 10 | Taxonomy · `api_scope` | GET /category_tree/{treeId} | 全树,**巨型 payload 必须开 gzip**;版本变了才拉 |
| 11 | Taxonomy · `api_scope` | GET /category_tree/{treeId}/fetch_item_aspects | **整站叶子类目 aspects 一次性下载** = 沃尔玛 PT spec 的等价物 |
| 12 | Taxonomy · `api_scope` | GET /category_tree/{treeId}/get_item_aspects_for_category?category_id= | 单类目补漏。**禁止逐 SKU 调**,5,000/day 会见底 |
| 13 | Taxonomy · `api_scope` | GET /category_tree/{treeId}/get_category_suggestions?q= | 类目候选。⚠ **官方明示 sandbox 不支持**;返回是建议不是判据,最终映射入库定稿 |
| 14 | Inventory · `sell.inventory` · `inventory.py` | PUT /inventory_item/{sku} | 建/改商品事实。🔴 **全量覆盖不是 PATCH**(§5.2) |
| 15 | Inventory · `sell.inventory` | POST /bulk_create_or_replace_inventory_item | 批量,**≤25** |
| 16 | Inventory · `sell.inventory` | GET /inventory_item/{sku};POST /bulk_get_inventory_item | 单查(pending 复核/补漏)+ 批量回读(≤25) |
| 17 | Inventory · `sell.inventory` | GET /inventory_item?limit=&offset= | 全量分页,catalog_sync 的骨架 |
| 18 | Inventory · `sell.inventory` | DELETE /inventory_item/{sku} | **最高危**:删 item 连带删掉其 offer 与在架 listing |
| 19 | Inventory · `sell.inventory` | POST /bulk_update_price_quantity | 改价 + 改库存,**≤25**(⚠ 25 的语义官方自相矛盾,§3.2) |
| 20 | Inventory · `sell.inventory` | PUT /inventory_item_group/{key};GET 同路径 | 变体组。`inventoryItemGroupKey` **一经设定不可改** |
| 21 | Offer · `sell.inventory` · `offers.py` | POST /offer;POST /bulk_create_offer | 建未发布 offer(站点/价格/三政策/仓位),批量 **≤25** |
| 22 | Offer · `sell.inventory` | PUT /offer/{offerId} | 改 offer(维护链改价改量的另一条路,§5.5 路由) |
| 23 | Offer · `sell.inventory` | GET /offer/{offerId};GET /offer?sku=&marketplace_id= | **提交台账落定与反查的唯一抓手**(status / listingId / 按 SKU 反查 offerId) |
| 24 | Offer · `sell.inventory` | POST /offer/{offerId}/publish;POST /bulk_publish_offer | 发布成 listing,回执含 `listingId`;批量 **≤25** |
| 25 | Offer · `sell.inventory` | POST /offer/publish_by_inventory_item_group | 变体组一次发成多变体 listing,**全或无** |
| 26 | Offer · `sell.inventory` | POST /offer/{offerId}/withdraw;POST /offer/withdraw_by_inventory_item_group | 下架:结束 listing,**offer 记录保留**(≈ 可逆下架) |
| 27 | Offer · `sell.inventory` | DELETE /offer/{offerId} | 删 offer。**破坏动作**,归清理链不归维护链(§2) |
| 28 | Offer · `sell.inventory` | POST /offer/get_listing_fees | 预估刊登费,**≤250**;**只能查未发布 offer,且按 marketplace 汇总不分摊**(§5.5) |
| 29 | Fulfillment · `sell.fulfillment` · `orders.py` | GET /order | 订单增量拉取 `filter=lastmodifieddate:[...]`。⚠ **绝不与 creationdate 同传** |
| 30 | Fulfillment · `sell.fulfillment` | GET /order/{orderId} | 单查:深历史点取 / 退款状态回查 |
| 31 | Fulfillment · `sell.fulfillment` | POST /order/{orderId}/shipping_fulfillment;GET 同路径 | 标发货回传运单(**一包裹一次,发运后不可增删行**)+ 回读防重复标发 |
| 32 | Fulfillment · `sell.fulfillment` | POST /order/{order_id}/issue_refund | 主动退款,**异步**(回查 `paymentSummary.refunds.refundStatus`);EU/UK 需数字签名 |
| 33 | Post-Order · IAF 头 · `postorder.py` | GET /return/search;GET /return/{returnId} | 退货创建时间窗扫描(limit 25/200)+ **未闭合单点查 = 追状态的唯一手段** |
| 34 | Post-Order · IAF 头 | POST /return/{returnId}/decide | 批准 / 拒绝退货 |
| 35 | Post-Order · IAF 头 | POST /return/{returnId}/mark_as_received;POST /return/{returnId}/issue_refund | 标收到退货;退货退款 |
| 36 | Post-Order · IAF 头 | GET /cancellation/search;GET /cancellation/{cancelId} | 取消单扫描(limit 10/500)+ 点查 |
| 37 | Post-Order · IAF 头 | POST /cancellation/{cancelId}/approve \| /reject | 处置取消。**不响应 = 3 个工作日后自动拒绝**;批准后 FVF 自动退回 |
| 38 | Post-Order · IAF 头 | GET /inquiry/search;GET /inquiry/{inquiryId} | INR 扫描(25/200)+ 点查。**已升级为 case 的不在结果里** |
| 39 | Post-Order · IAF 头 | POST /inquiry/{inquiryId}/provide_shipment_info | 补运单信息(escalate 后返回 `caseId`,与 inquiry 是两个 id 空间) |
| 40 | Finances · `sell.finances` · `finances.py` | GET /payout;GET /payout/{payout_Id} | 放款列表(limit 20/200)+ 详情 —— **对账链的驱动源** |
| 41 | Finances · `sell.finances` | GET /transaction | 交易明细,**必须 `filter=payoutId:{...}`**;`orderId` / `payoutId` 双向可 filter |
| 42 | Finances · `sell.finances` | GET /transaction_summary | 期间汇总,对拍用 |
| 43 | Dev Analytics · `api_scope` · `_client` 私有 | GET /rate_limit/ | 配额对表 + 429 后读 `reset`。**应用令牌即可,不需卖家授权** |
| 44 | Trading · IAF 头 · `trading.py` | POST /ws/api.dll `GetMyeBaySelling`(`SellingSummary.Include=true`) | 取 **selling limit 余量** `AmountLimitRemaining` / `QuantityLimitRemaining` |

**#44 是全项目唯一一条 Trading 调用,且不是双轨**:#7 给的是**上限**,余量在 REST 侧**没有等价物**,按 CLAUDE.md「能力不同的两个端点 → 两个显式函数、services 层显式 if 路由」处理。⚠ **后来的人不要按"grep 不到别的 Trading 调用"就把它当死代码删掉**(CLAUDE.md「判不准就判活」)。

**预留(只登记不实现)**:Sell Compliance `getListingViolations`(问题商品扫描的 eBay 对应物)、Sell Analytics `getSellerStandardsProfile` / `getCustomerServiceMetric`(直读官方等级与服务指标,100 与 400 calls/day)——⚠ 两者 API 面本轮均未取到;Commerce Media `createImageFromFile/Url`(若 `product.imageUrls` 不接受自有外链则必须走它,⚠未核验);Commerce Notification `/destination` + `/subscription`(**仅** `MARKETPLACE_ACCOUNT_DELETION` 合规订阅);Fulfillment `payment_dispute` 子链(250k/day,业务未开);Commerce Identity `getUser`;Inventory `bulkMigrateListing`(本项目纯新地用不上,批量上限⚠未核验)。
**明确不做**:Trading `AddFixedPriceItem` 上架路径、Sell Feed(LMS)XML feed 上架路径、业务事件 webhook 订阅(三条均见 §8.1 被否方案);Buy API 族(非卖家域,需独立资质);Marketing/Ads/Negotiation(本期不做站内促销)。

## 2. 工作流 × 端点矩阵

| workflow | DANGEROUS | 用到的端点(§1 编号) | 备注 |
|---|---|---|---|
| 1 ebay_bootstrap_account | True | 3,4,5,6,8,7 | **一次性户口链,不进 `schedule.JOBS`**;产物 policyId×3 + merchantLocationKey 落库。**它没跑通之前上架链一条也发不出去** |
| 2 ebay_taxonomy_sync | False | 9,(10),(11),12 | 每日只调 9 比版本,`categoryTreeVersion` 变了才拉 10/11。全量落库,上架时走库 |
| 3 ebay_list_new | True | 12(读库),14/15,20,21,24/25,(28),23 | 最大;api 面在此收口。提交前落 pending(§5.4) |
| 4 ebay_submit_poll | True | 23,16,31 | **eBay 无 feed 概念**:本链是"pending/submitted 台账落定器 + 启动对账",不是轮询器(§5.4) |
| 5 ebay_catalog_sync | False | 17,23,16 | 全店 item + offer 对拍,回填 `catalog.ebay_items`;缺席只标不删 |
| 6 ebay_order_sync | False | 29,30 | `lastmodifieddate` 真增量;**时间窗封顶 90 天** |
| 7 ebay_returns_sync | False | 33,36,38 | **创建时间窗全量重扫 + 本地未闭合单点查,双管**(§3.2 理由) |
| 8 ebay_maintenance | True | 19,22,14/15 | **只做 title/price/inventory**(MAINT_ACTIONS);破坏动作不在此链 |
| 9 ebay_problem_cleanup | True | 26,27,18,24 | **破坏动作唯一出口**:withdraw≈retire、delete offer/item≈delete、republish≈relist |
| 10 ebay_returns_action | True | 34,35,37,39,32 | 售后处置(批准/收货/退款/取消/补运单) |
| 11 ebay_settlement_sync | False | 40,41,42 | **payout 驱动**:先 getPayouts,再按 `payoutId` 拉 transaction |
| 12 ebay_account_health | False | 7,44,43 | 每日巡检:refresh token 到期 + selling limit 余量 + getRateLimits 对表 |

**对给定清单的三处增删,逐条给理由**:**拆出 9 `ebay_problem_cleanup`** —— withdraw/delete 是破坏动作,CLAUDE.md 08-24 定稿「破坏动作只有一个出口」「破坏组存在即压制同 SKU 的维护组」,塞进 `ebay_maintenance` 等于把 08-19 那条"说不清是哪条链干的"事故重演一遍。**拆出 10 `ebay_returns_action`** —— `ebay_returns_sync` 是只读同步(`DANGEROUS=False`)而 decide/refund 是不可逆写,混在一条链里会让 `--dry-run` 的语义在同一个 `run()` 里分叉(`DANGEROUS=False` 读 `dry_run`、`True` 读 `execute`,两套判据不能共存于一个模块)。**新增 12 `ebay_account_health`** —— 三件事合并成一条,理由是它们**同为每日一次、只读、告警型**;其中 refresh token 巡检是 **eBay 独有的必需品**:OAuth **没有** `HardExpirationWarning`(那是 Auth'n'Auth 旧令牌才有的 7 天预警),eBay 不会提醒你 refresh token 快过期。⚠ **refresh token 失效必须是显式停链告警状态,绝不能让 workflow 静默跳过该账号** —— 否则重演 CLAUDE.md 里"每天空转而且报成功"那类事故。

**不是 workflow 但必须有的一格**:`MARKETPLACE_ACCOUNT_DELETION` 合规订阅。它是 Application Growth Check 的门槛项("能不能跑"而非"跑多快"),需要一个**公网入站 HTTPS 端点**,是本项目唯一的入站面。**建议单独最小 webhook,不与任何业务链耦合。** ⚠ 是否强制 webhook、有无替代路径——未核验(§8.3 #12)。

## 3. 配额表(全部来自 W4 逐条核验,核验状态照抄)

`refdata/ebay_rate_limits.tsv`(72 行 6 列)**由本表生成**;两者不一致时**以本蓝图为准**(与沃尔玛侧 tsv 同一纪律:tsv 是来源之一,个别行已被核验修正)。
**形状先说清楚,别照抄沃尔玛的直觉**:沃尔玛的痛点是短窗口速率(价格三件套 6/天、单品 PUT 100/小时);eBay 的痛点是**少数几个日配额极小的 API**,主力写路径(Inventory 2M/天)几乎不设防,而**真正的天花板是"你能上多少货"的 selling limits,不是"你能调多少次"**。

### 3.1 日配额与令牌桶定稿值(来源 develop/get-started/api-call-limits + oauth-rate-limits.html,全部 verified)

| API / resource | 官方缺省 | 定稿(写进令牌桶) | 备注 |
|---|---|---|---|
| Sell Inventory(整个 API 一个桶) | 2,000,000 / day | 1,600,000 / day | 上架/改价/改库存主力,几乎撞不到 |
| Sell Account(整个 API) | 25,000 / day | 20,000 / day | 户口链冷数据,应缓存 |
| Sell Fulfillment · order(全部方法) | 100,000 / day | 80,000 / day | 订单轮询无压力;`getPaymentDispute` 系另计 250,000/day |
| Sell Finances(整个 API) | 15,000 / day | **12,000 / day** | **第二紧**;禁止按日全量扫 transaction |
| Commerce Taxonomy(整个 API) | 5,000 / day | **4,000 / day** | **最紧之一**;全树与 aspects 必须落库缓存 |
| Post-Order · Return / Cancellation / Inquiry / Case | **各自** 5,000 / day | 各 4,000 / day | 退货链主力;未闭合单点查必须有本地态过滤 |
| Trading(整个 API 共享一个桶) | 5,000 / day | 500 / day | 只有 #44 一条调用 |
| Developer Analytics(含 getRateLimits 自身) | 5,000 / day | 200 / day | **拿 5,000 的桶去保护 2,000,000 的桶 = 自杀**,只做对表 |
| OAuth `client_credentials` | **1,000 / day** | 落库缓存,提前 300s 续 | 🔴 **最硬的一条**:不缓存则一条稍密的 Taxonomy 链就打爆整个 App,**且是 App 级共享,一炸全账号全链条一起炸** |
| OAuth `authorization_code` / `refresh_token` | 10,000 / 50,000 per day | 每账号每 2h 至多刷 1 次 | 令牌只活在进程内存时刷新次数 = **进程数 × 账号数**,落库后与进程数无关 |

⚠ **配额按 App/keyset 计还是按卖家账号计 —— 未核验**(从 `getRateLimits` "for an application" 与 `getUserRateLimits` "for an application user" 的措辞对照推得,证据强但非官方直述)。**这条若判错,Post-Order 5k/day 与 Taxonomy 5k/day 的多账号容量规划全错。** → 令牌桶第一参数**默认取"应用身份"(全账号共享一个桶)**,理由是判错方向不对称:按应用算而实际按账号算 = 白白慢一点;按账号算而实际按应用算 = **超配额 429 打全店**。

### 3.2 批量与分页上限

| 端点 | 上限 | 核验状态 |
|---|---|---|
| bulkCreateOrReplaceInventoryItem / bulkGetInventoryItem | **25 条** | verified(Inventory OAS3) |
| bulkCreateOffer / bulkPublishOffer | **25 条** | verified(release-notes-archive;方法页是 SPA,⚠ 建议沙箱二次确认) |
| bulkUpdatePriceQuantity | **25** | verified 但**语义官方自相矛盾**:OAS3 说「1 个 item 的 25 个 offer」,静态指南 bulk-updates.html 说「25 个 item 记录」→ **实测前一律按 25 条记录保守切片** |
| getListingFees | **250 个未发布 offer** | verified(expected-listing-fees.html) |
| 单 listing 变体数 / 变体维度 / 每维取值 | **250 / 5 / 30** | verified(trading-user-guide/variations.html) |
| getOrders `limit` / `offset` / `orderIds` | **200 max(default 50)** / default 0 / **一次最多 50 且吃掉其它 query 参数** | verified(Fulfillment OAS3)。**offset 分页在边写边读时会漏会重** → 按日期区间切片 + offset,不裸 offset 翻到底 |
| getOrders 时间窗深度 | filter 只回**近 90 天**;按 orderId 可取 2 年内 | ⚠ **官方两处冲突**(OAS3 说 2 年 / 静态指南说 90 天)。可调和读法:**深历史只能按 orderId 点取,时间窗扫描封顶 90 天**,但**未证实** |
| Post-Order search | return 25/200、cancellation 10/500、inquiry 25/200;回溯 **≤18 个月**;**三个 search 全无 last-modified 参数** | verified |
| getPayouts / getTransactions `limit` | 20/200 与 20/1000 | verified(Finances OAS3) |

**`EBAY_BULK_MAX = 25` 必须登记进 registry,不许散落。** REST bulk 粒度固定 25 意味着"一次全店改价"= N/25 次 HTTP 调用,是**调用次数密集型**,与沃尔玛"一个 feed 文件几千行"是相反的形状。**Post-Order 无 last-modified** 是 §2 第 7 条"窗口重扫 + 未闭合单点查双管"的直接理由:窗口重扫只捞新单,而退货状态可以在创建 18 个月后仍在变,窗口覆盖不到老单;沃尔玛靠窗口重拉就能覆盖状态迁移,eBay 必须多这一步。

### 3.3 卖家级 / 短窗口限制 + 超限形态

- **Trading Revise Listing 类**(ReviseItem / ReviseFixedPriceItem / ReviseSellingManagerTemplate):**1200 calls / 30 秒**,超限 ErrorCode **21919144** [verified KB2137]。⚠ **这是卖家级不是应用级**:不会被"多开 App"绕过,也不会因店多而稀释。Trading 通用超限错误 **518**(其自查端点 `GetApiAccessRules` 的参考页现已 404,字段清单未核验)。Sell Feed(LMS)每卖家 per feedType 400 requests/day、单文件 ≤15 MB、超限 error **160025**(本项目不用 LMS,登记备查)。
- **selling limits(真正的天花板)**:`getPrivileges` 官方示例即 `$100 / 10 件`。⚠ **日限还是月限官方三比一冲突**(OAS3 写 "on a given day",帮助中心与 KB5104 写 monthly)→ **代码只存「上限值 + 剩余值 + 采样时间」,不写死周期语义**。提额是站内人工流程**无 API**,只能监控 + 飞书告警(§2 #12)。
- **429 形态**:HTTP **429** / **errorId 2001** / domain `ACCESS` / category `REQUEST` [verified rest-response-components.html]。🔴 **eBay REST 响应不带任何限流头**:官方 header 全集页只列 `Content-type` / `Content-Language` / `Location` / `Warning`,**没有** `X-RateLimit-*`、没有 `Retry-After` [verified rest-request-components.html]。⚠ 这是**否定性结论**(官方未文档化),**未实调 dump 过真实响应头**。**官方零 retry/backoff 指导** [verified handling-error-messages.html] → **退避策略是本仓自定,§6.4 必须显式标注"非官方"**。
- ⚠ **日配额计数器归零时刻(PT 午夜 vs 滚动 24h)未核验** —— 官方页遍寻不着。**上生产前实调 `getRateLimits` 读 `reset` 实值,以它为唯一判据,别信午夜传说。**

## 4. 认证与令牌模型(eBay 特有的重头,沃尔玛无对应物)

### 4.1 两种令牌

| 项 | Application access token | User access token |
|---|---|---|
| grant | `client_credentials` | `authorization_code` → 之后 `refresh_token` |
| 有效期 | **7200 秒** [verified oauth-tokens.html] | **7200 秒** [verified 双页命中] |
| refresh | **没有**,过期只能重新 mint(无人参与) | refresh token,**`refresh_token_expires_in = 47304000`(547.5 天 ≈ 18 个月)** |
| 用在哪 | Taxonomy(#9-13)、getRateLimits(#43) | **全部 sell.***(#3-8、#14-32、#40-42)、Post-Order(#33-39)、Trading(#44) |
| 人工参与 | 无,只要 client_id/secret | 首次授权卖家浏览器点同意;**吊销后必须重走同意,无法自动化** |

⚠ **引用纪律**:refresh token 18 个月是真的,依据是响应字段 `refresh_token_expires_in=47304000` [verified oauth-auth-code-grant-request.html + KB5075],**不是**文档里那句 "Tokens are valid for 18 months across multiple sessions" —— 那句属于 **Auth'n'Auth 旧令牌**(WebFetch 跨页串味的实证)。**写文档时引数值,不引那句英文。** 机器可读的分工证据:OAS3 的 `securitySchemes` 把 `sell.*` 全挂在 `authorizationCode` flow 下(⇒ 必须用户令牌),Taxonomy 只声明 `clientCredentials`(⇒ 应用令牌即可),与静态页那句 "metadata or taxonomy calls" 双证吻合。

### 4.2 刷新与过期运维(五条硬规矩 + 三个 eBay 独有闭环)

1. **提前 300 秒主动刷,不等 401。** 令牌半路过期会让一次写操作落在"不知有没有到达"的三态里,而写操作永不自动兜底。
2. 🔴 **刷新响应不含新的 refresh_token,严禁 `row.refresh_token = resp.get("refresh_token")`** —— `.get()` 返回 None 会把好 token 洗成空。eBay 的 refresh token **不轮转**,写一次管 18 个月。这是最容易踩的一个坑,`_client` 里必须留显式注释。
3. **单飞**:同一 `(account, env, kind)` 的刷新走 `SELECT ... FOR UPDATE`,或复用 cli.py 现成的 flock 思路,防并发调度同时刷。**应用令牌按 scope 集合做缓存 key** —— app token 是按 scope 铸的,key 不含 scope 会拿窄 scope 的令牌调宽 scope 的端点拿 403。
4. **401 只允许"刷新一次 → 再失败即抛错告警"**,**严禁 user token 失败自动降级成 app token**(本仓"能力不同的两个端点必须显式路由")。
5. **令牌落库共享,不放进程内存**(建议 `ops.ebay_tokens`,主键 `account + env + token_kind`,列含 access_token / access_expires_at / refresh_token / refresh_expires_at / scopes)。理由是算出来的不是偏好:本仓一天几十条 workflow 各自启停,令牌只活在进程内 ⇒ 刷新次数 = 进程数 × 账号数,而应用令牌只有 1,000/day 且 App 级共享。真密钥 client_id / client_secret / RuName 仍**只进 `<DATA_ROOT>/.env`**,仓库只出现变量名;落库令牌属运行态凭据,`docs/db_schema.md` 要标注该表**禁止进任何导出/快照**。
6. **到期预警必须自建**(§2 #12):`refresh_expires_at - now < 30 天` 即飞书告警;`scopes` 列同时用于"改了 scope 必须重授权"的比对。
7. **卖家改密码 / 改登录名 → 该账号全部 refresh token 当场被吊销** [verified:"if a seller changes their eBay member log-in name or the password ... any active refresh tokens associated with the account will be automatically revoked"]。→ 运营 SOP:**先停调度 → 改密 → 浏览器重走同意 → 落库 → 起调度**,与本仓"新旧系统严禁并跑:先停调度 → 搬状态 → 起新调度"是同一套纪律。

### 4.3 scope × API 矩阵

| scope 字符串 | 令牌 | 覆盖端点 | 核验状态 |
|---|---|---|---|
| `https://api.ebay.com/oauth/api_scope` | 应用 | 9-13、43 | verified(Taxonomy / Analytics spec 双证,描述 "View public data from eBay") |
| `.../api_scope/sell.inventory` | 用户 | 8、14-28 | verified(描述 "View and manage inventory") |
| `.../api_scope/sell.account` | 用户 | 3-7 | 字符串 verified(spec 端点 `security` 引用);⚠ **描述文本未取到**(大 spec 截断) |
| `.../api_scope/sell.fulfillment` | 用户 | 29-32、33-39 | 同上 |
| `.../api_scope/sell.finances` | 用户 | 40-42;#32 issueRefund 亦引用 | 同上 |

⚠ **`commerce.taxonomy` 这个 scope 不存在** —— Taxonomy 只声明 `clientCredentials` flow,scope 是 `api_scope`(+ `metadata.insights`);任何写着 `api_scope/commerce.taxonomy` 的申请单都是错的。`.readonly` 变体**不单独申请**(写权限覆盖读);"给只读任务配一套降权令牌"会让令牌管理成本翻倍,与本仓"每个能力只有一条实现路径"冲突 → **默认不做,需所有者拍板**。**scope 字符串必须做成 registry 常量**,理由同"飞书字段名只准引用 registry 常量"。

### 4.4 base URL 全表(逐族登记,**不许全局假设 `api.ebay.com`**)

| 族 | Production | 族 | Production |
|---|---|---|---|
| OAuth token | `https://api.ebay.com/identity/v1/oauth2/token` | OAuth consent | `https://auth.ebay.com/oauth2/authorize` |
| Sell Inventory / Account / Fulfillment | `https://api.ebay.com/sell/{inventory\|account\|fulfillment}/v1` | **Sell Finances** | **`https://apiz.ebay.com/sell/finances/v1`** |
| Commerce Taxonomy | `https://api.ebay.com/commerce/taxonomy/v1`(⚠ #13 sandbox 不支持) | Developer Analytics | `https://api.ebay.com/developer/analytics/v1_beta` |
| Post-Order v2 | `https://api.ebay.com/post-order/v2` | Trading(XML) | `https://api.ebay.com/ws/api.dll` |

**Sandbox 通则**(官方明文):`api.ebay.com → api.sandbox.ebay.com`、`auth.ebay.com → auth.sandbox.ebay.com`,路径不变。⚠ **`apiz.ebay.com` 是真东西不是笔误**:Finances 与 Identity 的 spec 把它列为(或首选)Production host;Fulfillment 的 spec 里 `api` 与 `apiz` **两条并列**都标 Production。**定稿:Fulfillment 用 `api.ebay.com`(与文档正文一致),Finances 用 `apiz.ebay.com`(spec 首列)**;⚠ 两 host 是否等价**未核验** → 上线前 sandbox 实测各自是否 200,以实测为准在 registry 里逐族钉死。**生产 keyset 在 sandbox 无效;测试用户名强制 `TESTUSER_` 前缀。** 凭证形状:一个 eBay App = {App ID(client_id)、Dev ID、Cert ID(client_secret)、RuName} × 2 套(sandbox / production);Dev ID 只有 Trading 用,Sell REST 只需 client_id/client_secret;RuName 是 consent 的 `redirect_uri` 取值。

### 4.5 三种 Authorization 前缀(同一个令牌,三种写法)

```
Sell REST      Authorization: Bearer <user token>
Post-Order     Authorization: IAF <user token>      ← IAF 与令牌之间一个空格
Trading XML    X-EBAY-API-IAF-TOKEN: <user token>
```
必带头:Inventory 全族要 `Content-Type` + **`Content-Language`**;Post-Order 全族要 **`X-EBAY-C-MARKETPLACE-ID`**(EBAY_US 等)。**这全部属于"把接口调对"(铁律 2),由 `_client` 按族路由,严禁在 services/workflows 里判断前缀。**

## 5. 上架体系设计(`api/ebay/inventory.py` + `offers.py`,对应沃尔玛的 feed 体系)

### 5.1 三步链与职责切分

| 步 | 端点 | 管什么 | 本仓对位 |
|---|---|---|---|
| ① inventory item | 14/15 | **平台中立的商品事实**:sku、condition、availability、product(title/description/aspects/imageUrls/brand/mpn/upc/ean/epid)、包装尺寸 | `catalog.products` / `snapshots` |
| ② offer | 21 | **站点与商务条款**:marketplaceId、format、categoryId、price、availableQuantity、三条 policy、merchantLocationKey | `catalog.ebay_items` 投影 |
| ③ publish | 24/25 | 把未发布 offer 变成活 listing,回执含 `listingId` | 沃尔玛的 feed 提交 + 落定 |

**这个二分正是选 Inventory API 而不是 Trading 的第三条理由**(§8.1):它天然对齐本仓「平台中立产品库 + 平台投影表」的既定架构;Trading 的 `AddFixedPriceItem` 是一个大扁平 payload,反而要把中立层与投影层揉回去。

### 5.2 三个必须内化的工程陷阱

1. 🔴 **`createOrReplaceInventoryItem` 是全量覆盖不是 PATCH** —— 官方原文 "all fields that are currently defined for the inventory item record are required in that update action, regardless of whether their values changed."。→ **`catalog.ebay_items` 必须存全量字段,每次 PUT 从投影表整体重放,禁止"只改变的字段"。** 这与沃尔玛 feed 的增量语义**相反**,是最容易翻车的地方。
2. **必填是分层的**:"Fields may be optional or conditionally required when calling this method, but become required when publishing the offer." → **校验必须放在 publish 前的 services 层**,不能指望 createOffer 报错。
3. 🔴 **判类目必填属性只准读 `aspectRequired` 布尔,绝不能读 `aspectUsage`** —— 官方自陈必填项在 `aspectUsage` 里返回的是 `RECOMMENDED`。读错会**漏掉全部必填项**,后果是上架被拒或 listing 质量分掉底。

### 5.3 前置户口链、变体、产品标识(全部是 publish 的硬门槛)

- **户口链**(§2 #1,一次性、极低频):opt-in `SELLING_POLICY_MANAGEMENT` → 建 fulfillment/payment/return 三条政策 → `createInventoryLocation`。三条政策**全部必须存在并挂到 offer 上**;**缺 `merchantLocationKey` 时 createOffer 不报错、publishOffer 必失败**。policyId / merchantLocationKey **落库并从 registry 取,不许硬编码**。⚠ 反证记录:某次抓取归纳出过一句 "The Inventory API does not require business policies to function" —— 那是摘要模型的自行推断,与三条逐字引语直接冲突,**以逐字引语为准:必须建**(记此条是为了下一轮别被同一句误导)。
- **变体**:`createOrReplaceInventoryItemGroup`(`inventoryItemGroupKey` **一经设定不可改**,`variesBy.specifications` 逐维给,`aspectsImageVariesBy` 至少一维)→ 各成员 createOffer → `publishOfferByInventoryItemGroup`(**全或无**)。硬上限 **250 变体 / 5 个维度 / 每维 30 值**,超 250 **必须在 services 层拆组**,不能指望 API 报错兜底。⚠ PBSE 类目下变体**不能用 ePID,必须逐变体给 GTIN** ⇒ 产品库要保证每个变体行都有 UPC/EAN。⚠「同组内只有 sku/qty/price 可变,categoryId / 三政策 / merchantLocationKey / marketplaceId 必须一致」**未取到官方原文句**,仅由必填清单结构反推,实现前需再核。
- **产品标识**:标识**不是全站硬必填,是按类目条件必填**(publish 必填清单里 upc/ean/epid/brand/mpn 一个都没有)。优先级 **ePID > GTIN(UPC/EAN/ISBN)> brand+MPN**;类目要求 GTIN 而商品没有时填**站点专属替代文本**(US `Does not apply`、DE/AT/CH `Nicht zutreffend`、FR/BE法/CA法 `Non applicable`、IT `Non applicabile`、NL/BE荷 `Niet van toepassing`、ES `No aplicable`、PL `Nie dotyczy`)。🔴 **这张表必须进 registry,严禁代码里写 `"Does not apply"` 英文字面量** —— 一旦上多站点,硬编码会在 DE/FR 站静默造出一批不合规 listing。无品牌商品用 `Unbranded`,MPN 不得只含特殊字符。
- **SKU 约束**:`maxLength=50`、卖家内唯一(verified);**字符集官方未给白名单**。SKU 走 URI path(`PUT /inventory_item/{sku}`),含 `/ # ? %` 空格必然出事 → **本仓自我约束 `[A-Za-z0-9._-]{1,50}` 并在入库时校验**(这是工程决定,不是官方要求)。

### 5.4 提交防重三态怎么映射(**eBay 无 feed,这是本节的核心**)

沃尔玛一批 SKU 换回**一个 feedId**、状态靠轮询;eBay 的批量端点是**逐条部分成功、同步返回、没有整批 id**。三层防重的**语义原样保留**,载体逐项改写:

| 沃尔玛(现状) | eBay 对应物 | 变了什么 |
|---|---|---|
| `ops.feed_log` 一行 = 一个 feed(一批 SKU) | 一行 = **一个 SKU 的一次提交** | 🔴 **防重键按单 SKU 载荷指纹算,不按整批**;批量端点只是传输优化,不是防重单位 |
| `payload_key` = SKU 集合哈希 | 单条载荷 `json.dumps(sort_keys=True)` 的 sha256 前 32 | 语义不变:**在途拒绝、终态可重占、不设时间防重窗** |
| `feed_id` | **`submission_id`**:上架阶段存 `offerId`,publish 成功后存 `listingId`,发货存 `fulfillmentId` | eBay 的回执 id 分阶段,存最新一个 |
| 反查 `GET /v3/feeds`(feedType + 精确条数 + 时间窗) | **#23 getOffer / getOffers?sku=,#16 getInventoryItem,#31 getShippingFulfillments** | ⚠ eBay 这里**反而更干净**:按 SKU 精确点查,不需要"同尺寸兄弟切片会撞指纹"那套排除逻辑 |
| FOUND / NOT_FOUND / UNKNOWN | offer 存在且 `status=PUBLISHED`/有 `listingId` = **FOUND**;offer 不存在或无 listingId = **NOT_FOUND**;查询本身失败 = **UNKNOWN** | **"确认未达才补交"这条语义一个字不许放松** |

**处置表(照抄沃尔玛的分野,理由同源)**:token/代理阶段失败(请求未发出)→ `failed` + retryable;2xx 且拿到 id → `submitted` + 落 item 级台账;**4xx 明确拒绝 → `failed`,绝不自动换姿势重试**;**5xx → 不当终态拒,走反查三态**(2026-08-19 沃尔玛侧实证:边缘 5xx 时资源可能已建成而响应丢了,这条普适性对 eBay 同样成立);网络异常(status=None)→ 反查;FOUND 收编不补交 / NOT_FOUND **同方法**补交一次 / **UNKNOWN 保持 pending 留给启动对账,人不在环时宁停不重**。🔴 **429 属 `category=REQUEST`:写类调用撞 429 一律不换方法重试,只能在 `reset` 后同方法补交,补交前先查三态。** **item 级台账**要保留 `missing` 这一档("台账里有、复核时查无"必须有名字,不装成功也不装失败);**pending 行永不老化**,摘要必须摊开 `账号/类型/工作流/提交时间`(只报个数没法处理,几轮之后就成了背景噪音)。
⚠ **一处 eBay 独有的失败分类**:账号被限制时 "Create new listings or revise existing listings" 是**账号级失败**不是条目级失败 [verified id=4190],逐条重试会把整批打成假失败 → 提交台账需要一个**账号级健康位**,命中即整账号熔断,不再逐条重试。⚠ **待核验且直接影响补交逻辑**:同一 `(sku, marketplaceId, format)` 重复 `createOffer` 的行为(报"已存在"还是建出第二个 offer)——**沙箱必测**;若是后者,NOT_FOUND 的补交必须先走 #23 的 `getOffers?sku=` 反查。

### 5.5 双路由与刊登费

**单品端点与批量端点是能力不同的两个端点**:两个显式函数 + services 层显式 if 路由,**严禁"批量失败自动退单品"**——那是重复提交制造机。**改价改库存有两条路**:`bulkUpdatePriceQuantity`(#19,批量、offer 维度)与 `updateOffer`(#22,单条、整体覆盖 offer),**路由阈值归 services,api 层只提供两个函数**(与沃尔玛"同步 PUT vs feed"同款)。**刊登费不在 publishOffer 回执里**,回执只有 `listingId`;`getListingFees`(#28)有两个硬限制:**只能查未发布的 offer**、**按 marketplace 汇总不分摊到 offer** → **上架链不承担单 SKU 成本核算**,费用归集交给结算链(#40-42)。这是 Inventory API 相对 Trading 的一处真实能力退化(Trading 回执直接带费用),但不足以翻盘选型。⚠ 另记:**刊登费在 eBay 是真实固定成本**(非订阅仅 250 条/月免费,超出 $0.35/条;一万条 ≈ $3,412/月),沃尔玛侧无此项——"能上就上"的铺货策略**不能沿用**(属策略问题不属 api 层,记此提醒)。

## 6. 横切能力(`api/ebay/_client.py`,eBay 侧唯一 HTTP 出口)

沃尔玛侧 `api/_client.py` 的横切能力逐项对齐,**照抄的不重写、不同的写明为什么**:

1. **令牌缓存与刷新**:落库(§4.2),缓存 key = **账号身份**(不是 client_id —— 应用级 client_id 全账号共用,拿它当 key 会互相顶掉);缓存里同样存回刷新所需材料(refresh_token / proxy),理由与沃尔玛侧一字不差:401 时能就地刷新,不需要调用方再传一次。
2. **三种 Authorization 前缀 + 必带头**由 `_client` 按族路由(§4.5);`make_headers()` 统一现造,**业务代码不拼 header**。
3. **每账号固定出口代理注入**:代理 URL 由 `services/ebay_accounts._normalize` 造好放进账号 dict,**api 层不读凭证表**。⚠ eBay 侧这条是**保守工程默认,不是 eBay 的要求**:官方确认多账号合法、且 "If we apply restrictions or limits to one account, then similar restrictions or limits may be applied to the member's other linked accounts" [verified id=4232],但**关联判据官方从不公开**(API License Agreement 里 IP / proxy 均 NOT PRESENT)。**写文档时不许写成"eBay 官方要求固定 IP"或"eBay 按 IP 判定关联",那是编造。** 🔴 **代理必须按域名放行,不许做目的地 IP 白名单**:eBay sandbox 已 CDN 化(2026-05-15 公告给的是让你放行 eBay 的 IP 段),且官方明说不承诺固定 IP、让你自己定期 nslookup —— 按 IP 放行会随时静默断线。
4. **退避策略(⚠ 非官方,本仓自定;前提见 §3.3)**:(a) **主路径不探余额,靠本地计数器** —— eBay 绝大多数桶是**日配额**而非滑动窗口,本地按 `(apiContext, apiName, resource)` 累加就能算准;(b) **每条工作流开跑前拉一次 `getRateLimits` 对表**(24 次/天,占 5,000 桶的 0.5%),校准漂移、顺便发现"别的进程偷偷用掉了配额",⚠ **对表不做准入判据**(官方未声明刷新延迟,必须当作可能滞后的数据);(c) **429 时立即拉一次该 resource 的 `reset`,睡到 `reset`** —— 这是唯一有官方字段支撑的做法,**不做指数退避猜时间,也不信"等到第二天"那种论坛口径**;触发必须记日志计数(兜底静默常态化 = 主路径已坏没人知道)。`getUserRateLimits` **不进集中巡检**(要 user token、每账号跑一遍、scope 面很宽),只在排查卖家级限流时手工用。
5. **令牌桶维度与登记制**:维度 = `(应用身份, bucket)` **不是账号**(§3.1 的方向性理由);例外是 Trading Revise 类的**卖家级**桶,那一格第一参数才是账号——**两种维度显式登记,不许猜**。**未登记的 bucket 默认拒绝而非放行**(沃尔玛侧 `RETIRE_ITEM` 零限速跑了数月就是放行放的)。🔴 **bucket 名必须带平台前缀**(如 `ebay.inventory.bulk_create`):`ops.rate_events` 是同一张表,与沃尔玛的 `inventory.put` 撞名 = **两个平台互相扣对方的配额**。⚠ 沃尔玛侧 `_is_persistent` 的判据(窗口 ≥600s 或上限 ≤10)会把 **eBay 全部日窗口桶推进 PG** ⇒ 每次调用都要打一趟 PG,且 **PG 挂了 = eBay 全停(fail hard)**;**要么接受,要么显式改判据 —— 不许静默降级进程内**。⚠ 两处 `_RATE_BUCKETS` 登记表必须在头注里**互相点名**,否则下次扩桶只改一处。
6. **账号失效语义**:`EbayAccountDeadError(account, status)` = 跳过该账号全部剩余调用,不逐页重试。🔴 **必须一开始就枚举"哪些状态码 = 这套凭证被拒"**,别等踩到 —— 沃尔玛侧的判例是 `client_credentials` 授权失败回的是 **400 不是 401**,以原生异常冒出去后落进各 workflow 的 `except Exception`,**一家店凭证坏掉判整轮失败**,而回 401 的店走的却是"跳过、整轮成功"。eBay 至少要覆盖:token 端点 400/401、业务端点经 401 自愈仍失败、refresh token 已被吊销。
7. **写操作零自动重试**:`safe_post_ex` 默认 `max_retries=0`,**连 docstring 一起抄**(POST 非幂等,自动重试 = 重复提交:发运后不可增删行、退款异步、offer 可能建出第二个)。**失败只走台账反查三态 → 确认未达 → 同一方法补交。**
8. **返回契约**:`(status:int|None, headers:dict(小写键), data:dict|None)`,**不抛异常、不 raise_for_status**;`status=None` = 网络/超时;非 2xx 时 `data=None`;非 2xx 且 POST/PUT 时截 **500 字符**响应体进日志(200 字符正好截在 Akamai 的 Reference # 前面,而开工单要的就是那个号)。
9. **进程级 socket 兜底超时**:沃尔玛侧 `socket.setdefaulttimeout(90)` 在模块顶层、**import 即生效、全进程含飞书**。⚠ **eBay 客户端不要再设一次**——两处设不同值时后 import 的赢,而且看不出来。连接池 / transport / `_parse_retry_after` / 半死连接自愈 / `download_bytes` 与平台无关 ⇒ 走 `api/_http.py` 中立件,**限速状态尤其不许存在第二份实现**(两套会漂,漂了的后果是 429 与封号)。
10. **错误码登记去向(registry 常量,不许散落)**:REST `429 / errorId 2001 / ACCESS / REQUEST`;Fulfillment `34200`(GSP 与非 GSP 行不能同 fulfillment)、`34300`(多 GSP 行不支持合并标发)、`34903`(退款原因必填)、`34905`(orderLevelRefundAmount 与 refundItems 二选一);Trading `518`、`21919144`;LMS `160025`。⚠ 更多枚举(`orderPaymentStatus` / `reasonForRefund` / `refundStatus` / `TransactionTypeEnum` / `PayoutStatusEnum` / `condition`)**全部未取到**,见 §8.3 #9。
11. **EU/UK `issueRefund` 需数字签名**(OAS3 描述含 "an additional security verification via Digital Signatures is required")。**美站不需要**;若上欧站,签名机制做在 `_client`,不进业务层。

## 7. 各域文件函数面(设计定稿,实现按工作流进度分期)

命名规则沿用沃尔玛蓝图:`list_*` 分页拉全量、`iter_*` 生成器、`get_*` 单对象、`create_*`/`put_*` 写操作。**所有函数第一参数 `account`(dict,来自 `services/ebay_accounts.py`),不接触凭证细节。**

```
api/ebay/_client.py    唯一出口:令牌 + 三种 Authorization 前缀 + 代理 + 令牌桶 + 退避
  get_app_token(scopes) / get_user_token(account)           # 落库缓存,提前 300s 刷,不回写 refresh_token
  bearer_headers(account, *, content_language=None) / iaf_headers(account, marketplace_id)
  trading_headers(account, call_name)                       # X-EBAY-API-IAF-TOKEN
  safe_get_ex / safe_post_ex / safe_put_ex / safe_delete_ex  # (status, headers, data);写操作默认零重试
  iter_paged(fn, *, limit, page_key="offset")               # limit/offset 分页模型,全仓唯一实现
  consent_url(account, scopes)                              # 拼同意页 URL,不发请求
  _fetch_rate_limits()                                      # 私有:getRateLimits,对表与 429 后读 reset
api/ebay/account.py
  opt_in_selling_policy(account) / opted_in_programs(account)
  create_fulfillment_policy / create_payment_policy / create_return_policy(account, payload)
  list_policies(account, kind, marketplace_id)              # kind ∈ fulfillment|payment|return
  create_location(account, location_key, payload) / list_locations(account)
  get_privileges(account)                                   # sellingLimit 上限 + 注册是否完成
api/ebay/taxonomy.py                                        # 全族应用令牌
  get_default_tree(marketplace_id) -> (tree_id, version) / get_category_tree(tree_id)   # 后者 gzip 必开
  fetch_item_aspects(tree_id) / get_category_aspects(tree_id, category_id)  # 后者补漏,禁止逐 SKU 调
  suggest_categories(tree_id, query)                        # ⚠ sandbox 不支持
api/ebay/inventory.py
  put_item(account, sku, payload)                           # docstring 必写"整体覆盖,不是 PATCH"
  bulk_put_items(account, items) / bulk_update_price_quantity(account, rows)   # 内部按 25 切片
  get_item(account, sku) / bulk_get_items(account, skus) / iter_items(account)
  delete_item(account, sku)                                 # 最高危:连带删 offer 与在架 listing
  put_item_group(account, group_key, payload) / get_item_group(account, group_key)
api/ebay/offers.py
  create_offer(account, payload) -> offer_id / bulk_create_offers(account, payloads)    # ≤25
  update_offer(account, offer_id, payload) / get_offer(account, offer_id)
  iter_offers(account, *, sku=None, marketplace_id=None)
  publish_offer(account, offer_id) -> listing_id / bulk_publish_offers(account, offer_ids)  # ≤25
  publish_group(account, group_key)                         # 全或无
  withdraw_offer / withdraw_group / delete_offer(account, ...)
  get_listing_fees(account, offer_ids)                      # ≤250;按 marketplace 汇总,不分摊
api/ebay/orders.py
  iter_orders(account, *, last_modified_start, last_modified_end=None, limit=200)
      # ⚠ 绝不与 creationdate 同传(后者优先、前者被静默丢弃);时间窗封顶 90 天
  get_order(account, order_id) / get_orders_by_ids(account, order_ids)   # ids ≤50 且吃掉其它参数
  create_shipping_fulfillment(account, order_id, payload) -> fulfillment_id
  list_shipping_fulfillments(account, order_id) / issue_refund(account, order_id, payload)  # 后者异步
api/ebay/postorder.py                                       # 全族 IAF 头 + 必填 marketplace 头
  iter_returns / iter_cancellations / iter_inquiries(account, *, created_from, created_to, **f)
  get_return / get_cancellation / get_inquiry(account, id)  # 未闭合单点查(search 无 last-modified)
  decide_return / mark_return_received / refund_return(account, return_id, payload)
  approve_cancellation / reject_cancellation(account, cancel_id, payload)
  provide_shipment_info(account, inquiry_id, payload)
api/ebay/finances.py
  iter_payouts(account, *, payout_date_from, payout_date_to) / get_payout(account, payout_id)
  iter_transactions(account, *, payout_id=None, order_id=None, date_from=None, date_to=None)
      # ⚠ 结算链只准按 payout_id 驱动;按日全量扫会打爆 15k/day
  transaction_summary(account, **filters)
api/ebay/trading.py
  get_selling_limit_remaining(account)                      # GetMyeBaySelling;全仓唯一一条 XML 调用
```

**模块增删,逐条给理由(这是对 `docs/ebay_plan.md` §2.1 预排骨架的修订)**:**`returns.py` → 改名 `postorder.py`** —— 分文件的依据是**认证体系与 base URL 不同**(IAF 前缀 + 必填 marketplace 头 + `/post-order/v2`),而 return 只是它三条链之一;叫 `returns.py` 会让下一个人把 cancellation/inquiry 塞进 `orders.py`,那就跨了认证体系。**删掉骨架里的 `analytics.py`** —— ① 退避要用的 `getRateLimits` 属 **developer.analytics**,是 `_client` 的自用依赖,单独成文件会造成 `_client ↔ analytics` **循环 import**,故收进 `_client` 私有;② 骨架写的"账号表现"要的是 **sell.analytics** 的 `getSellerStandardsProfile`,那是**另一个 API** 且字段面本轮**未核验** ⇒ 按"预留端点只登记不实现"处理,**补核后再建**。**新增 `trading.py`(单端点)** —— XML 网关 + 第三种认证头 + 独立的 5,000/day 桶,混进 `account.py` 会破坏"一个文件一种协议"的可读性;单独成文件也便于将来 REST 补上能力后**整体删掉**。结果:`__init__.py` + 9 个实体文件。⚠ 这是本仓**第一个 api 子包**,**必须同步改 `api/__init__.py` 的"文件划分"人读索引**,漂开会让下一个 AI 照旧索引找文件。

**明确不进 api 层的(业务规则,归 services/workflows)**:批量 vs 单条的路由阈值、"价未变跳过"节流、publish 前的必填校验与 aspects 匹配、selling limit 余量够不够、VeRO/违禁品闸、`platform` 参数取值(registry 常量,不给默认值)、dry-run 门禁(api 层无 dry-run 概念,cli 层强制)。

## 8. 选型定稿、官方核验结论与遗留问题

### 8.1 四个选型定稿(结论 + 理由 + 被否方案)

**① 上架走 Sell Inventory API(REST)。** 理由:(a) 官方对新集成的**唯一明示推荐**——"If you're considering a new integration, eBay recommends using Inventory API" [verified develop/guides-v2/listing-creation];(b) item/offer 二分**天然对齐**本仓「中立产品库 + 平台投影表」的既定架构;(c) 日配额 2M 几乎不设防;(d) 铁律"每个能力只有一条实现路径,禁止双轨"。**被否 A:Trading `AddFixedPriceItem`** —— 它本身**没有**弃用声明,但配套调用正在被逐个凌迟:`GetCategories`/`GetCategoryFeatures` **已下线**、`UploadSiteHostedPictures` **2026-09-30 下线** [verified api-deprecation-status],即"选 Trading"实际会变成"Trading + 一堆 REST",**双栈成本 > 纯 REST**;且它的大扁平 payload 要把中立层与投影层揉回去。**被否 B:Sell Feed(LMS)XML feed** —— 它承载的就是 Trading 语义(feedType 直接叫 `AddFixedPriceItem`),选它等于把被否 A 的模型背进来,还额外多一层 15MB 文件与 100~500 feeds/天的限制。

**② 订单/售后/结算走轮询,不做业务事件订阅。** 理由:现代 Notification API 公布的 topic **只有 8 条,全部是 listing/账号类**(ITEM_AVAILABILITY / LISTING / LISTING_PREVIEW_* / SELLER_STANDARDS_PROFILE_METRICS / AUTHORIZATION_REVOCATION / FEEDBACK_STAR_RATING / MARKETPLACE_ACCOUNT_DELETION / SELLER_CUSTOMER_SERVICE_METRIC_RATING),**订单、退货、取消、INR、纠纷、放款一条都没有**(两次独立抓取一致);配额也完全支持轮询。**被否 A:Notification API webhook** —— 为 8 条与主链无关的 topic 引入一条会**静默丢事件**的故障面(端点挂 → destination 转 `MARKED_DOWN`),正撞本仓"兜底静默常态化 = 主路径已坏没人知道"的忌讳;且本项目是"脚本 + launchd + PostgreSQL",没有常驻公网 HTTPS 服务。**被否 B:Trading Platform Notifications** —— 它确实有订单事件(`ItemSold`/`ReturnCreated`/`BuyerCancelRequested` 等),但正被逐条退役(`ItemMarkedPaid` 弃用 2026/05/27、停用 2026/06/22),押注它 = 押注一个正在被拆的通道。**例外(必须做)**:`MARKETPLACE_ACCOUNT_DELETION` 合规订阅,单独最小 webhook,不与业务链耦合。

**③ 退货/取消/INR 走 Post-Order v2,背下这套老 API 的债,不等新 REST。** 理由:2026 年 eBay 砍了 Post-Order 的 **16 个 Return + 4 个 Case + 5 个 Inquiry** 方法,但砍的是"买家侧 + 低使用率"方法,**卖家侧主干**(`/return/search`、`/decide`、`/issue_refund`、`/mark_as_received`、`/cancellation/*`、`/inquiry/*`)**仍在**;且**停用列的 replacement 全为空**——eBay 没给 REST 替代品。**被否:等 Sell Fulfillment 长出退货能力** —— 官方没有任何这样的路线图。⚠ 一条必须记住的二次核对纠正:"should make plans to migrate to the Post-Order API" 这句属于 **Return Management API 那一行**(它全量弃用、迁去 Post-Order),**不是**说要迁出 Post-Order —— 第一次抓取时被摘要器错挂到 Post-Order 名下,**引用退役表前务必看行归属**。

**④ 退避走"本地日计数 + 低频对表 + 429 触发查 reset"(§6.4)。** **被否 A:照抄沃尔玛的头驱动令牌桶** —— eBay 没有 `x-current-token-count` 的对应物;**被否 B:每次业务调用前探余额** —— 拿 5,000 的桶去保护 2,000,000 的桶,自杀式设计;**被否 C:429 后指数退避猜时间 /「等到第二天」** —— 后者来自论坛不是官方,且 eBay 给了 `reset` 字段,没有理由去猜。

### 8.2 官方核验结论(§1–§5 的表内已逐条带 [verified];此处只列**没有别处安放**的几条)

1. **getOrders 状态过滤**:`orderfulfillmentstatus` 只有两种合法组合(`{NOT_STARTED|IN_PROGRESS}` 与 `{FULFILLED|IN_PROGRESS}`),**单值不在文档允许范围,别自创**;行级状态三态 `NOT_STARTED → IN_PROGRESS → FULFILLED`;**未完成 checkout 的单根本不出现在 Fulfillment API 里**(与沃尔玛"Created 即可见"不同)。
2. **Finances 可闭环**:transaction 上同时挂 `orderId` 与 `payoutId`、两侧都能 filter ⇒ 订单 ↔ 交易 ↔ 放款三级闭环,比沃尔玛的双周 CSV 账期干净一个量级。⚠ 放款周期官方文档取不到(`/managed-payments` 返 404)→ **对账链不要把放款周期写死成常量,按 `payoutDate` 事件驱动**。⚠ finances-landing 页有一句语义晦涩的告诫("You should not use any API response or notification, other than the Notification API, as a source to query the Finances API."),**引用前需二次确认上下文**。
3. 🔴 **买家脱敏已生效(2025-09-26)**:受影响辖区含 **"China (and its territories)"**,`buyer.username` 在本项目返回的是 **immutable userId**,美国买家支付明细一律 `CustomCode`。**禁止把 username 当买家自然键、禁止字符串比对**;Post-Order 的 `sort=BUYER_LOGIN_NAME` 与 Finances 的 `filter=buyerUsername` 在本项目下语义已变,**慎用**;对账链不得依赖买家支付明细。
4. **合并订单(Combined Invoice)真实存在** ⇒ 订单行唯一键必须是 `(orderId, lineItemId)`,**不能是 `(orderId, sku)`**;同时另存 `legacyOrderId` / `itemId` / `transactionId`(Post-Order 的 search 按 item_id + transaction_id 或 order_id 检索),且 orderId 空间不止一种格式(实见 `itemId!transactionId` 形态)。
5. **退货 `status` 与 `state` 是两套枚举**(25 值 vs 43 值,部分同名语义不同)⇒ **两列都存**,合并即重演本仓"source 当 action 用"的事故;`REPLACEMENT_*` 12 值官方自述 "not currently supported",**可收录枚举但不要写分支逻辑**;`CancelStatusEnum` 8 值已核全。
6. **GSP 硬约束**:GSP 行与非 GSP 行不能装同一个 fulfillment(34200);多 GSP 行不支持合并标发(34300);发运后**不可增删 fulfillment 行**。**取消链可以"不作为"**:不响应则 3 个工作日后自动拒绝,批准后 eBay 自动冲正交易且 FVF 自动退回。
7. **类目缓存的官方口径是版本比对,不是"缓存 N 小时"**:比对 `categoryTreeVersion`,变了才重拉;类目层级"updated on a monthly basis, but may be updated more frequently",官方建议**每天至少查一次版本**。⚠ 广为流传的"类目约每季度更新"**未核验,别写进文档**。

### 8.3 ⚠ 未核验项 —— **动手前必须补核**(按危险度排序)

| # | 事项 | 影响 | 补核方式 |
|---|---|---|---|
| 1 | 🔴 **GSP/eIS 双地址结构**:`finalDestinationAddress` 是否仅在 `ebaySupportedFulfillment=true` 时返回?面单该用 `shippingStep.shipTo`(转运仓)还是它?`shipToReferenceId` 语义? | **拿反 = 全部国际单寄错地址**;沃尔玛没有这个双地址结构,是 orders 域最容易踩的坑 | 沙箱实拉一单 GSP 订单看真实 JSON(类型页是 SPA) |
| 2 | 🔴 **配额按 App 还是按卖家账号计** | 判错则 Post-Order 5k/day 与 Taxonomy 5k/day 的多账号容量规划全错 | 真账号实调 `getRateLimits` + 多账号对拍 |
| 3 | 🔴 **重复 `createOffer` 的行为**(报"已存在" vs 建出第二个 offer) | 决定 NOT_FOUND 补交前要不要先反查(§5.4) | 沙箱必测 |
| 4 | **日配额计数器归零时刻**(PT 午夜 vs 滚动 24h) | 本地计数器的归零判据 | 实调读 `reset` 实值,以它为唯一判据 |
| 5 | **getOrders 时间窗能回溯 90 天还是 2 年** | 决定 `ebay_order_sync` 冷启动能否回捞历史 | 实测 |
| 6 | **`bulkUpdatePriceQuantity` 的 25 数什么** | 切片粒度;实测前一律按 25 条记录 | 沙箱实测 |
| 7 | **Post-Order 索引页(自报 v2.9.0)是否滞后于退役表**(Release Notes 已到 v2.9.1,且 2.9.0 条目就叫 "Decommissioned calls and types") | 别把已死端点写进 api 层 | §1 的 #33-39 逐端点对退役表复核 |
| 8 | **`api.ebay.com` 与 `apiz.ebay.com` 在 Fulfillment/Finances 上是否等价** | registry 逐族钉死哪个 | sandbox 实测两个 host 各自是否 200 |
| 9 | **枚举全集缺失**:`orderPaymentStatus` / `reasonForRefund` / `refundStatus` / `TransactionTypeEnum` / `PayoutStatusEnum` / `TransactionStatusEnum` / `condition` | 建表与状态机 | OAS3 `components` 被截断 + 类型页 SPA → 换取证手段(沙箱实调 / 可执行 JS 的抓取) |
| 10 | **图片托管**:`product.imageUrls` 能否直接给自有外链,还是必须过 Commerce Media | 决定要不要建 `media.py`;⚠ 直接搬亚马逊主图另有 VeRO 风险(那是政策问题不是接口问题) | 沙箱实测 |
| 11 | **多变体"只有 sku/qty/price 可变"** | 变体组拆分规则 | 未取到官方原文句,仅结构反推 |
| 12 | **`MARKETPLACE_ACCOUNT_DELETION` 是否强制 webhook、有无替代路径** | 本项目唯一的公网入站面,且是 Growth Check 门槛项 | 查合规页,单独定方案 |
| 13 | **买家 email / 收货地址是否也被脱敏** | 决定 orders 域能否做地址风控与买家去重 | 官方 Data Handling 页只讲 username 与支付明细 |
| 14 | **eBay 响应实际是否带非文档化限流头** | 若有,§6.4 可大幅简化为带内驱动 | 实调 dump 一次响应头 |
| 15 | **`createShippingFulfillment` 的 carrier + trackingNumber 是否"可选但必须成对"、无追踪单如何标发** | 发货链的入参校验 | 仅见搜索摘要未见原文 |
| 16 | 🔴 **Sell Compliance / Sell Analytics 的 API 面**(`getListingViolations` + `complianceType` 枚举、`getSellerStandardsProfile` 返回字段) | 问题商品扫描与"直读官方等级"的入口;**在补证之前 eBay 维护链的指标来源无法定稿** | ⚠ developer.ebay.com 的 SPA 与 403 使本轮全部取不到 → 需可执行 JS 的抓取,或所有者用浏览器另存 |
| 17 | 低危三项:`sell.account`/`sell.fulfillment`/`sell.finances` 的 **scope 描述文本**(⚠ **scope 字符串本身已核验,可直接用**);**Post-Order 是否处于弃用路径**(官方 index 页未表态);**`bulkMigrateListing` 批量上限**(传言 5) | 不阻塞第一批实现 | 分别:找小 spec / 单列一轮调研 / 启用前补核 |

### 8.4 已知会咬人、但不属 api 层的四条(记在此处防遗忘,详见 `docs/ebay_plan.md` 与 W5 底稿)

1. **selling limits 是平台强制硬上限** —— `services/store_limits.py` 那条"读不到就回落默认值"的语义在 eBay 侧**必须反转**:读不到时回落默认值 = 直接撞 id=4232 的"绕限制"违规。
2. **跨店排他单位是 identical item(官方 13 属性判据)不是品牌** —— 不要复用 `services/brand_key.py` 的品牌级占用(在 eBay 既过严又拦不住真违规:无货源商品大量 Generic/OEM,品牌为空时一条都拦不住)。⚠ **沃尔玛侧的品牌占用不要改**,catalog 层需同时承载两套排他。
3. **费率不是常量,是「店铺当前等级 × 类目服务指标」的函数**(TRS+ −10%、Below Standard +6/7%、INAD Very High +5/6%、国际 +1.65%,最坏叠加有效抽成 >30%),且**计费基数含运费含税**(按裸价算每单系统性少算约 1.1 个百分点,**而且两侧都不报错**)。
4. **因资格未获批而被下架的商品官方明文禁止 relist** [id=5271] ⇒ `ops.dispositions` 现有的 retire/delete/relist 三值**表达不了这一态**,eBay 侧需新增;VeRO 只是新 `source`,**不新开执行出口**(08-24 定稿在 eBay 侧完整成立)。
