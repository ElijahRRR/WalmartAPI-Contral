# eBay 一期实现级参考(字段 / 请求体 / 错误码)

> 2026-08-30 自调研笔记提炼(W1 上架三步链字段级 · W2 认证与户口调用序列 · W3 Taxonomy 字段级),**证据标注随原笔记**;本文只管"字段抄什么",**设计判断一律以 `docs/ebay_phase1_design.md` 为准**(该文 §6.4 那句"全量字段表在 w1 笔记"指向的就是本文)。判据、排期、建表、沙箱清单、文档勘误都不在本文,在设计方案与 `docs/ebay_plan.md`。
>
> **证据标记(三份笔记各自的分级,原样保留,不合并不升格)**:`[V-spec]` = WebFetch 官方 `sell_inventory_v1_oas3.json` 原文(**1.18.5**,最高级)· `[V-static]`/`[V-fetch]`/`[verified URL]` = WebFetch 到 developer.ebay.com 静态页或小 spec 原文,逐字引 · `[M]` = 第三方镜像 `api-evangelist/ebay`(eBay 原文措辞,**1.17.4 = 2024-02-27**)⚠ 非官方直取 · `[M+V]` = 镜像与某官方直取源独立交叉命中,按"两个独立来源交叉才记 verified"当 verified 用 · `[S]`/`[V-index]`/`⚠[indexed URL]` = 仅 WebSearch 索引到的官方正文片段,未见原始渲染页 ⇒ ⚠ 可写进设计但须沙箱二次确认。
>
> **机读物(W1 随笔记落盘在调研会话 scratchpad,非仓内,会话结束即失效;此处只登记存在与用途,不整段贴入)**:`inv_item.yml`(Inventory-Item OAS3 全文,含 `components.schemas` 与 `x-response-codes`)· `offerapi.yml`(Offer 全族同上)· `inv_group.yml`(变体组)· `location.yml`(仓位)· `errorcodes.json`(**按 operationId × HTTP 码抽出的错误码全表**,镜像版 1.17.4;§1.8 的逐条明细出自它与官方 1.18.5 的合并)· `ppc_ebay.json`(微软 PowerPlatform 的 eBay connector,**只作第三方旁证,枚举已过时勿抄**)。本文没列全的码去这几份查。

---

## 一、上架三步链字段参考
### 1.1 调用面与 headers 对照

```
① PUT  /sell/inventory/v1/inventory_item/{sku}        body=InventoryItem            → 200|201|204
② POST /sell/inventory/v1/offer                       body=EbayOfferDetailsWithKeys → 201 {offerId}
③ POST /sell/inventory/v1/offer/{offerId}/publish     无 body                       → 200 {listingId, warnings[]}
回读 GET /sell/inventory/v1/offer/{offerId}     → EbayOfferDetailsWithAll(status / listing.listingId)
撤架 POST /sell/inventory/v1/offer/{offerId}/withdraw  无 body → 200 {listingId, warnings[]}
删除 DELETE /sell/inventory/v1/inventory_item/{sku}            → 204
```
host `https://api.ebay.com`(sandbox `https://api.sandbox.ebay.com`),scope `.../oauth/api_scope/sell.inventory`,**用户令牌**。

| 端点 | Authorization | Content-Type | Content-Language |
|---|---|---|---|
| PUT /inventory_item/{sku} | ✔ | ✔ 必填 [V-spec] | ✔ 必填 [V-spec] |
| POST /bulk_create_or_replace_inventory_item | ✔ | ✔ 必填 [V-spec] | ✔ 必填 [V-spec] |
| POST /bulk_get_inventory_item | ✔ | ✔ 必填 [V-spec] | ✘ **不声明**(逐条确认)[V-spec] |
| POST /bulk_update_price_quantity | ✔ | ✔ 必填 [V-spec] | ✘ |
| GET /inventory_item/{sku}、GET /inventory_item、DELETE /inventory_item/{sku} | ✔ | ✘ | ✘ |
| POST /offer(createOffer) | ✔ | ✔ 必填 [M] | ✔ 必填 [M] |
| POST /bulk_create_offer、PUT /offer/{offerId} | ✔ | ✔ [M] | ✔ [M] |
| POST /bulk_publish_offer、/offer/get_listing_fees、/offer/publish_by_inventory_item_group、/offer/withdraw_by_inventory_item_group | ✔ | ✔ [M] | ✘ |
| **POST /offer/{offerId}/publish、/offer/{offerId}/withdraw** | ✔ | ✘ **无 body、无这两个头** [M] | ✘ |
| GET /offer、GET /offer/{offerId}、DELETE /offer/{offerId} | ✔ | ✘ | ✘ |

`getOffers` 官方原句 [M]:"**The authorization header is the only required HTTP header for this call.**"(同句也在 `deleteInventoryItem` 描述里)。⚠ 头里的 locale 是连字符 `en-US`,**bulk 请求体里的 `locale` 字段是下划线** `en_US`/`en_GB`/`de_DE` [M] —— 两种形态不同。

### 1.2 ① `createOrReplaceInventoryItem` 请求体(`InventoryItem`)
顶层四容器:`availability` / `condition`(+`conditionDescription`/`conditionDescriptors`)/ `packageWeightAndSize` / `product`。**spec 的 `required` 列表为空**,必填是分层的(publish 前必填清单见 §1.5)。语义硬事实(全 [V-spec] 逐字):

- **全量覆盖不是 PATCH**:"all fields that are currently defined for the inventory item record are required in that update action, **regardless of whether their values changed**.";首建 "Upon first creating an inventory item record, only the SKU value in the call path is required.";改了会自动同步在架 listing:"a successful call will automatically update these eBay listings."
- 🔴 "**Each listing can be revised up to 250 times in one calendar day.**"(同句也在 publishOffer / bulkPublishOffer 描述里)—— 这是**每 listing / 每自然日的卖家级硬上限,不是 API 调用配额**。
- "any eBay listing created using the Inventory API **cannot be revised or relisted using the Trading API** calls."

**路径参数 `sku`**:必填,**Max length : 50** [V-spec]+[M];字符集看错误码 **25707** 原文 [V-spec `x-response-codes`] + [M] 逐字一致:"This is an invalid value for a SKU. **Only alphanumeric characters can be used for SKUs, and their length must not exceed 50 characters.**" ⚠ 文案与实际校验是否一致(`-` `_` `.` 三个字符)未实测。

**`availability`**

| 字段 | 类型 | 必填性 | 上限 / 枚举 | 来源 |
|---|---|---|---|---|
| `availability.shipToLocationAvailability.quantity` | integer | **publish 前必填**(官方必填清单逐字列名) | — | [V-static] + [M] |
| `availability.shipToLocationAvailability.availabilityDistributions[]` | array | 可选(多仓才用) | `{merchantLocationKey, quantity, fulfillmentTime{unit,value}}` | [M] |
| `availability.pickupAtLocationAvailability[]` | array | In-Store Pickup(仅 US/UK/DE/AU 大商家) | `availabilityType ∈ IN_STOCK\|OUT_OF_STOCK\|SHIP_TO_STORE` | [M] |

🔴 全量覆盖第一坑,官方点名 [M]:"this container should be included again, even if the value is not changing, **or the available quantity data will be lost**."

**`condition` 枚举全集(16 值,含 conditionId 映射)** [V-static `sell/static/metadata/condition-id-values.html`]:`1000 NEW`(New / Brand New / New with box / New with tags / Factory Sealed / Digital Good)· `1500 NEW_OTHER`(New other, see details / Open box)· `1750 NEW_WITH_DEFECTS` · `2000 CERTIFIED_REFURBISHED` / `2010 EXCELLENT_REFURBISHED` / `2020 VERY_GOOD_REFURBISHED` / `2030 GOOD_REFURBISHED`(限定站点/类目 + 需卖家资质)· `2500 SELLER_REFURBISHED`(部分站点已被上面三个取代)· `2750 LIKE_NEW`(交易卡/钱币类目下含"已评级"语义)· `2990 PRE_OWNED_EXCELLENT`(**仅服饰类目**,1.18.2 新增)· `3000 USED_EXCELLENT`(Used / Pre-owned)· `3010 PRE_OWNED_FAIR`(**仅服饰类目**,1.18.2 新增)· `4000 USED_VERY_GOOD`(交易卡下含"未评级"语义)· `5000 USED_GOOD` · `6000 USED_ACCEPTABLE` · `7000 FOR_PARTS_OR_NOT_WORKING`。

- ⚠ `MANUFACTURER_REFURBISHED` **已死**:"has essentially been replaced with the `CERTIFIED_REFURBISHED` enumeration value with Version 1.13.0",eBay 自动转换存量 ⇒ **别照抄 2023 年前的枚举表**(PowerPlatform connector 那份就是旧的:含 MANUFACTURER_REFURBISHED、缺 PRE_OWNED_*)。
- ⚠ 可用值按类目变 [M]:"Supported item condition values will vary by eBay site and category … use the **getItemConditionPolicies** method of the **Metadata API**."
- `conditionDescription`:string,**Max length: 1000** [M];只对非 NEW/LIKE_NEW/NEW_OTHER/NEW_WITH_DEFECTS 生效,用在 NEW 上会被忽略并**回一条 warning**。`conditionDescriptors[]`:仅交易卡三类目(183050 / 183454 / 261328)与(1.18.5 起)钱币类目强制。

**`packageWeightAndSize`** [M]

| 字段 | 类型 | 必填性 | 枚举 |
|---|---|---|---|
| `weight.value` / `weight.unit` | number / string | 计算运费 或 flat+重量附加费 时必填 | `POUND` `OUNCE` `KILOGRAM` `GRAM` |
| `dimensions.{length,width,height}` / `dimensions.unit` | number / string | 计算运费时必填,**四个字段一给就全给** | `INCH` `FEET` `CENTIMETER` `METER` |
| `packageType` | string | 可选 | `PackageTypeEnum`(约 30 值,常用 `MAILING_BOX` / `PACKAGE_THICK_ENVELOPE` / `LARGE_ENVELOPE` / `LETTER`)⚠ **全集未逐字取到** |
| `shippingIrregular` | boolean | 可选 | 1.17.4 新增 |

⚠ flat rate 且不加重量附加费时整个容器可不传;传过一次就要每次全量重放。相关码 **25715**「Invalid values for dimensions and/or weight of shipping package」。

**`product`(抄得最多的一张表)**

| 字段 | 类型 | 必填性 | 上限 / 格式 | 来源 |
|---|---|---|---|---|
| `product.title` | string | **publish 前必填** | **Max Length : 80** | [M] × [V-static listing-title.html] ⇒ **[M+V]** |
| `product.subtitle` | string | 可选,**收费** | **Max Length : 55** | 同上 ⇒ **[M+V]** |
| `product.description` | string | 与 `offer.listingDescription` **至少有一个** | 🔴 **Max Length : 4000** | [M] + [S slr:Product] ⇒ ⚠ **两源但都非直取,沙箱确认** |
| `product.aspects` | **map<string, list<string>>** | **publish 前必填** | 名 **≤40** / 值 **≤50** | [M] + [S] ⇒ ⚠ 双源 |
| `product.imageUrls` | array&lt;string&gt; | **publish 前必填,≥1 张** | 必须 `https://`;单品 **≤24**;变体组成员 **≤12** | [M] × [蓝图已 verified 的 picture-hosting] ⇒ **[M+V]** |
| `product.brand` / `product.mpn` | string | 类目条件必填(两者配对) | 各 **Max Length : 65** | [M] |
| `product.upc` / `product.ean` / `product.isbn` | array&lt;string&gt; | 类目条件必填 | **数组,不是标量** | [M] |
| `product.epid` | string | 可选;**优先级高于 GTIN** | 标量 | [M] |
| `product.videoIds` | array&lt;string&gt; | — | 每 listing 只支持 1 个视频 | [M] |

**`aspects` 三条实现级细节**:① 🔴 eBay 自家 OAS3 把 `aspects` 声明成 `type: string`(镜像里逐字如此,`enum`/`additionalProperties` 一个都没有)—— **这是 spec bug,不是 wire 格式**;真正的 wire 格式由官方描述里的例子逐字给出 `"aspects": { "Brand": ["GoPro"], "Storage Type": ["Removable"] }` [M] ⇒ Python 侧是 `dict[str, list[str]]`,**值恒为 list**(哪怕只有一个值)。② 一个名可多值:"one item specific name, such as 'Features', can have more than one value" [M]。③ 必填判据只读 `aspectConstraint.aspectRequired` 布尔(字段面见 §4.4)。

**产品标识与 catalog 联动** [M]:给了 `epid` 或 GTIN 且 eBay 匹配到 catalog product ⇒ "the inventory item is automatically populated with available product details such as a title, a product description, product aspects, and a link to any stock image" —— **title / description / aspects / 图片会被 catalog 值覆盖**;开关在 offer 侧 `includeCatalogProductDetails`(§1.3)。

### 1.3 ② `createOffer` 请求体(`EbayOfferDetailsWithKeys`,与 bulkCreateOffer 共用)
**创建时必填只有三个** [M 官方 description 逐字]:"Upon first creating an offer, the following fields are required in the request payload: **`sku`, `marketplaceId`, and (listing) `format`**."

| 字段 | 类型 | 必填性 | 上限 / 枚举 / 缺省 | 来源 |
|---|---|---|---|---|
| `sku` | string | **创建必填** | **Max Length : 50**;🔴 "**Only one offer (in unpublished or published state) may exist for each `sku` / `marketplaceId` / `format` combination.**" | [M] |
| `marketplaceId` | string | **创建必填** | `MarketplaceEnum`,美站 `EBAY_US` ⚠ 值本身只有 [S],全集未取到 | [M]+[S] |
| `format` | string | **创建必填** | **`FIXED_PRICE` \| `AUCTION`** | [M] |
| `availableQuantity` | integer | publish 前必填(官方清单),**有条件豁免** | "not necessarily required, even for published offers, **if the general quantity of the inventory item has already been set in the inventory item record**" | [M] |
| `categoryId` | string | publish 前必填 | eBay 叶子类目 id(**字符串不是 int**) | [M] |
| `listingDuration` | string | publish 前必填 | **fixed-price 恒 `GTC`** | [M] |
| `listingPolicies.fulfillmentPolicyId` / `.paymentPolicyId` / `.returnPolicyId` | string | publish 前必填(三个都要) | — | [M]+[V-static] |
| `merchantLocationKey` | string | publish 前必填(**创建时可缺**) | **Max length : 36**;一经设定不可改 | [M] |
| `pricingSummary.price.value` | **string** | publish 前必填 | 🔴 **字符串不是数字** —— "A string representation of a dollar value" | [M] |
| `pricingSummary.price.currency` | string | 与 value 成对 | 三位码如 `USD`;`CurrencyCodeEnum` ⚠ 全集未取 | [M] |
| `listingDescription` | string | 与 `product.description` 至少有一个 | 🔴 **Max Length : 500000(含 HTML 标签,标签计入)** | [M] 两处互证 + [V-static item-description.html] ⇒ **[M+V]** |
| `includeCatalogProductDetails` | boolean | 可选 | 🔴 **缺省 `true`** —— "the parameter **defaults to `true`** if omitted"(createOffer 与 bulkCreateOffer 描述里各出现一次) | [M] |
| `quantityLimitPerBuyer` | integer | 可选 | 每买家限购 | [M] |
| `storeCategoryNames` | array&lt;string&gt; | 可选,**最多 2 个** | 全路径写法 `"/Fashion/Men/Shirts"` | [M] |
| `secondaryCategoryId` | string | 可选 | **会产生费用** | [M] |
| `hideBuyerDetails` / `lotSize` / `listingStartDate` | boolean / integer / string | 可选 | 私密 listing(回读时**恒返回**,缺省 `false`)/ 打包件数 / UTC `2023-05-30T19:08:00Z` 定时上架 | [M] |
| `tax.applyTax` / `.thirdPartyTaxCategory` / `.vatPercentage` | bool/string/number | 可选 | US 已由 eBay 代征代缴 | [M] |
| `charity` / `regulatory` / `extendedProducerResponsibility` | object | 可选 | `regulatory` 在 1.17.6+ 扩了 GPSR 子字段(**镜像未含** ⇒ 欧盟合规容器不完整) | [M] |
| `listingPolicies.bestOfferTerms` / `eBayPlusIfEligible` / `shippingCostOverrides[]` / `takeBackPolicyId` / `productCompliancePolicyIds[]`(**≤6**)/ `regionalProductCompliancePolicies` / `regionalTakeBackPolicies` | 混合 | 可选 | — | [M] |

**`listingDescription` 与 `product.description` 的关系**(官方两处字段描述互证 [M]):offer 侧 "if the `listingDescription` field was omitted in the createOffer call …, the offer entity **should have picked up the text provided in the `product.description` field** of the inventory item record";item 侧 "**If neither the `product.description` field for the inventory item nor the `listingDescription` field for the offer exist, the publishOffer call will fail.**" ⇒ 回落方向 **offer ← item,只有一层**;两个都空是 **publish** 失败(不是 createOffer 失败);两者上限不同(4000 vs 500000)。

**`availableQuantity` 与 item 侧数量**:item 侧 `availability.shipToLocationAvailability.quantity` 是 ship-to-home 总量(跨 marketplace),offer 侧 `availableQuantity` 是**该 marketplace 上放出的量**;官方口径称 item 侧已设置时 offer 侧可豁免 [M];改 item 侧数量会自动同步在架 listing [V-spec]。

**响应**:`201 Created` → `OfferResponse { offerId: string, warnings: Error[] }` [M]。`offerId` **只在 createOffer 成功时返回,updateOffer 的响应里不返回**;失败时无 `offerId`,读 `warnings`(该容器同时装 errors 与 warnings)。

### 1.4 ③ `publishOffer` 调用面与响应

**路径**:🔴 spec 里带尾斜杠 `/offer/{offerId}/publish/` [M](PowerPlatform connector 同形);⚠ **官方静态页正文示例无尾斜杠** `https://api.ebay.com/sell/inventory/v1/offer/36445435465/publish` [V-static pbse-phase1-rest-workflows.html] ⇒ 两源不一致。**头 / 参数**:**只有 Authorization**,无 body、无 Content-Type / Content-Language [M];`offerId`(path,必填)。

**200 → `PublishResponse`** [M]:`{ "listingId": "110465561234", "warnings": [ /* Error[] */ ] }`。`listingId` = "The unique identifier of the **newly created eBay listing**." —— 只有成功转成 listing 才返回;🔴 **成功也可能带 warnings**。

**publishOffer 在 200 上会返回的警告码(7 个,全集)** [M]

| 码 | 文案 |
|---|---|
| 25028 | `{field}` is not applicable and has been dropped(**字段被静默丢弃**) |
| 25030 | `{field}` is not applicable for the condition and has been dropped |
| 25033 | Duplicate policy IDs found |
| 25037 | Item level Eco Participation Fee will be ignored |
| 25401 | Invalid listing format removed `{additionalInfo}` |
| 25402 | System warning. `{additionalInfo}` |
| 25753 | listingStartDate is in the past or the offer is live. Value is not updated on the listing. |

**错误/警告对象结构(REST 通用)** [V-static `api-docs/static/rest-response-components.html`]

| 字段 | 类型 | 说明 |
|---|---|---|
| `errorId` | number/long | "A positive integer that uniquely identifies the specific error condition" |
| `domain` | string | Inventory 侧恒 `API_INVENTORY` |
| `subDomain` | string | 🔴 通用页写 `subDomain`(大写 D),**Inventory OAS3 的 `Error` schema 写 `subdomain`(小写 d)** [M] ⇒ 解析时两个键都认 |
| `category` | enum | **`APPLICATION` \| `BUSINESS` \| `REQUEST`**(三值封闭) |
| `message` / `longMessage` | string | "at most 50 characters long" / "around 100-200 characters" |
| `inputRefIds` / `outputRefIds` | string[] | 指向请求 / 响应里出问题的元素 |
| `parameters` | object[] | `{ name, value }` —— 变量槽 `{fieldName}` / `{additionalInfo}` 的真值在这里 |

官方口径:"HTTP status code 200 is returned when **warnings do not stop processing**";"When errors occur, warnings … **are not included in any response with an error component**" ⇒ **一次响应不会同时有 errors 和 warnings**。⚠ 429 形态:429 / errorId 2001 / `ACCESS` / `REQUEST` —— `category=REQUEST` 与业务参数错误同类,**按 errorId 分流而非 category**。

### 1.5 成功码语义与 publish 前必填清单

| 码 | 体 | 说明 |
|---|---|---|
| **200 / 201** | `BaseResponse { warnings: Error[] }` | [V-spec];201 = Created |
| **204** | **无体** | [V-spec] 🔴 **正常成功码**。`BaseResponse` 官方自陈 "A response payload will only be returned for these three calls **if one or more errors or warnings occur**" [M] ⇒ **无警告的成功就是 204 + 空体** |
| 400 / 500 | 错误 | 见 §1.8;500 = 25001 系统错误 / 25025 并发访问 |

⇒ `put_item()` 的成功判据是 **`status in (200, 201, 204)`**,且 **204 下 `data is None` 属正常**(蓝图"非 2xx 时 `data=None`"没错,但**这里 2xx 也 data=None**)。

**`publishOffer` 必填清单(逐字,四组)** [V-static `publishing-offers.html`]

| 组 | 必填字段 |
|---|---|
| **Location** | `merchantLocationKey`;`location.address` 需 (city + stateOrProvince + country) **或** (postalCode + country) |
| **Inventory item** | `sku`;`availability.shipToLocationAvailability.quantity`;`condition`;`product`(`title`, `description`, `aspects`, `imageUrls`) |
| **Offer** | `offerId`;`sku`;`availableQuantity`;`marketplaceId`;`format`;`categoryId`;`listingPolicies`(`paymentPolicyId`, `returnPolicyId`, `fulfillmentPolicyId`);`merchantLocationKey`;`pricingSummary.price`;`listingDuration` |
| **Inventory item group**(多 SKU) | `inventoryItemGroupKey`, `variantSKUs`, `aspects`, `description`, `imageUrls`, `title`, `variesBy` |

同页逐字:"**For example, a missing `merchantLocationKey` will not cause issues when first creating an offer (the createOffer is successful), but calling the publishOffer method will fail if the offer does not have the `merchantLocationKey`.**"

### 1.6 bulk 端点:207 与逐条结构
三个 inventory bulk 端点在官方 spec 里都声明了 **`207 Multi-Status`** [V-spec 逐条确认];offer 侧 `bulk_create_offer` / `bulk_publish_offer` 同样声明 207 [M]。三档语义 [V-fetch listing-creation 指南]:**200 OK** = 全成功;**207** = 部分成功,"The `statusCode` field in the response will indicate which objects failed";**400** = 整个请求失败。官方逐字(bulkPublishOffer)[M]:"**It is possible that some unpublished offers will be successfully created into eBay listings, but others may fail.** The response payload will show the results for each `offerId` value…"

```json
// POST /bulk_create_or_replace_inventory_item(Content-Type + Content-Language)· 请求 BulkInventoryItem ≤25 条 / 响应 200 BulkInventoryItemResponse
{ "requests":  [ { "sku": "...", "locale": "en_US", "availability": {...}, "condition": "NEW", "product": {...}, "packageWeightAndSize": {...} } ] }
{ "responses": [ { "sku": "...", "locale": "en_US", "statusCode": 200, "errors": [ /* Error[] */ ], "warnings": [ /* Error[] */ ] } ] }
// POST /bulk_publish_offer(Content-Type,无 Content-Language)· 请求 BulkOffer ≤25 / 响应 200 BulkPublishResponse
{ "requests":  [ { "offerId": "..." } ] }
{ "responses": [ { "offerId": "...", "listingId": "...", "statusCode": 200, "errors": [...], "warnings": [...] } ] }
```

- 每条 item 是 `InventoryItemWithSkuLocale` = `InventoryItem` 全部字段 **+ `sku` + `locale`**;`sku` 逐字 "This field is **required**. Max Length : 50"。上限 25:"create and/or update up to 25" [V-spec] + [M] ⇒ **[M+V]**。逐条 `statusCode` 定义:"The HTTP status code returned in this field indicates the success or failure of creating or updating the inventory item record …" [M]。**没有整批 id**。
- `bulkPublishOffer` 的 `listingId` **只在该条成功时出现** [M]。
- `bulkCreateOffer`:请求 `{ "requests": [ EbayOfferDetailsWithKeys × ≤25 ] }`;响应 `{ "responses": [ { sku, marketplaceId, format, offerId, statusCode, errors[], warnings[] } ] }`;逐条 `statusCode` 成功值官方明写是 **200**(不是 201)[M];⚠ **一次只能针对一个 marketplace**:"the `bulkCreateOffer` method can only be used to create offers for one eBay marketplace at a time" [M]。逐行响应类型 `OfferResponseWithListingId` 含 `statusCode`、成功时 `listingId`(= Item ID)、失败时 `errors`/`warnings`,并有一句 "it is possible that an offer can be created successfully even if one or more warnings are triggered" [V-index]。
- **bulk 专属 400 码** [M]:item 侧 `25727` 条数超限 · `25728` 请求内 InventoryItems 必须唯一 · `25733` 每条都要有合法 sku 与 locale;offer 侧 `25709` Invalid offerId · `25730` 条数超限 · `25731` 请求内 offerId 必须唯一 · `25732` 变体组的 SKU 不能用本端点发布(要用 `publishOfferByInventoryItemGroup`)· `25729` / `25735`(见 §1.8)。
- ⚠ `bulkUpdatePriceQuantity` 的 "25" 语义官方自相矛盾:静态指南写 "**Up to 25 inventory item records (and the active offers associated with them) may be updated**" [V-fetch bulk-updates.html],OAS3 那侧是 offer 口径。

### 1.7 读端点与撤架端点关键字段
**`GET /inventory_item?limit=&offset=`** [M]:`limit` 缺省 **25**,Min 1 / **Max 200**;`offset` 缺省 0。⚠ eBay 自己的 `offset` 描述写成 "sets the **page number** to retrieve. The first page of records has a value of `0`" —— 名叫 offset、语义写成 page number。响应 `InventoryItems` = `{ href, inventoryItems[], limit, next, prev, size, total }`;⚠ **`size` 语义两个响应类型不一致**:`InventoryItems.size` = "the total number of **pages** of results",`Offers.size` = "the number of offers being displayed on the **current page**" [M]。400:`25706` 分页值非法 · `25709` 字段值非法 · `25710` 找不到实体。

**`GET /offer?sku=&marketplace_id=&format=&limit=&offset=`** [M]:`sku` ⚠ **spec 标 `required: false`,正文却写 "the required `sku` query parameter"、方法描述是 "retrieves all existing offers for the specified SKU value"** ⇒ 自相矛盾(Max length 50);`marketplace_id` 官方自陈目前无实际用途("the same SKU value can not be offered across multiple eBay marketplaces");`format` = `FIXED_PRICE`/`AUCTION`;`limit` **缺省 100**(⚠ 与 getInventoryItems 的 25 不同);`offset` 同样是 "page number" 措辞。响应 `Offers` = `{ href, limit, next, prev, size, total, offers[] }`,`offers` **Max Occurs: 25**;同一 SKU 最多同时一个 auction + 一个 fixed-price ⇒ **正常只回 1 条,回 2 条说明还挂着拍卖** [M]。

**`GET /offer/{offerId}` → `EbayOfferDetailsWithAll`**

| 字段 | 说明 | 来源 |
|---|---|---|
| `offerId` / `sku` | `sku` Max Length 50 | [M] |
| **`status`** | 🔴 **`PUBLISHED` \| `UNPUBLISHED`,两值封闭** —— "The enumeration value in this field specifies the status of the offer - either `PUBLISHED` or `UNPUBLISHED`." | [M] + [S/V-index `slr:OfferStatusEnum`] ⇒ 双源,**均非官方直取** |
| **`listing`** | 🔴 "The `listing` container is **not returned at all for unpublished offers**." | [M] |
| `listing.listingId` | eBay listing id(= Item ID) | [M] |
| `listing.listingStatus` | ⚠ 字段存在 [M];取值 [V-index `slr:ListingStatusEnum`]:`ACTIVE` / `OUT_OF_STOCK` / `INACTIVE` / `ENDED` / `EBAY_ENDED`("eBay customer service has administratively ended the eBay listing")/ `NOT_LISTED` —— **6 值,封闭性未确认** | [V-index] |
| `listing.soldQuantity` / `listing.listingOnHold` | 已售数量;因政策违规被 hold(hold 时买家看不到、搜索隐藏、下单出价被拦) | [M] |
| `availableQuantity` / `pricingSummary` / `categoryId` / `listingPolicies` / `merchantLocationKey` / `marketplaceId` / `format` / `listingDuration` / `listingDescription` | 回写投影 | [M] |

| 端点 | 方法 / 头 | 响应 | 语义(官方逐字) |
|---|---|---|---|
| `withdrawOffer` | `POST /offer/{offerId}/withdraw`,仅 Authorization | **200** `WithdrawResponse { listingId, warnings[] }` | "end a single-variation listing … **the offer object remains, but it goes into the unpublished state, and will require a `publishOffer` call to relist the offer**";`listingId` "will **not** be returned if the eBay listing was not successfully ended" [M] |
| `deleteOffer` | `DELETE /offer/{offerId}`,仅 Authorization | **204** 无体 | 未发布 offer → 永久删除;已发布 → 结束单品 listing,或从多变体 listing 与 item group 里移除该变体。⚠ **有成交的变体删不掉**,官方替代做法是把该变体数量置 0 [M] |
| `deleteInventoryItem` | `DELETE /inventory_item/{sku}`,仅 Authorization | **204** 无体 | 🔴 三条连带:① "Delete any and all **unpublished offers** associated with that SKU";② "Delete any and all **single-variation eBay listings** associated with that SKU";③ "Automatically remove that SKU from a multiple-variation listing and remove that SKU from any and all inventory item groups in which that SKU was a member" [M] |

幂等性形态:`withdrawOffer` 400 = `25002` · `25713`(已 unpublished / offerId 不存在)⇒ **重复 withdraw 回 25713 而不是 200**;`deleteInventoryItem` 400 = `25702` · `25709` · `25710` ⇒ **重复删返 400 而不是 204**。

### 1.8 错误码表
**`createOrReplaceInventoryItem` 400 全量 96 条**(✅ [V-spec] 官方 1.18.5,全部 `domain=API_INVENTORY` / `category=REQUEST`;按主题分组):

- **通用/输入**:25002 · 25016 `{fieldName}` 值无效 · 25017 `{fieldName}` 缺失(1.18.5 起该码指向多个可能错误)· 25022 属性无效 · 25502 属性信息无效 · 25503 产品信息无效 · 25601 · 25604 · 25709 · 25710
- **SKU**:25701 一个或多个 SKU 找不到 · 25702 `{skuValue}` 找不到 · **25707 SKU 只能字母数字且 ≤50** · 25708 Invalid SKU
- **价格/数量**:25003 · 25004 · 25759 数量不足(拍卖分配)· 25760 数量不足以建拍卖
- **类目/属性**:25005 类目 ID 无效 · 25029 `{field}` is required for this category · 25031 数值越界 · 25120 / 25121 该类目必须采用 eBay catalog 数据
- **政策**:25007 履约 · 25008 收款 · 25009 退货 · 25034 政策数量超限 · 25035 站点找不到政策 · 25036 政策类型不符 · 25089–25095 合规/回收政策数量与地区限制 · 25123 P&A 类目退货政策不合规(1.18.4)· 25766 / 25767 政策 id 必须是 long
- **图片**:25014 图片无效 · 25015 图片 URL 无效 · 25501 图片无效 · **25086 URL 必须是 eBay 图片服务 URL**
- **仓位/账号**:25012 仓位无效(`merchantLocationKey` 没建或错)· 25018 账号信息不完整
- **成色/包裹**:25021 成色信息无效 · 25020 / 25715 包裹尺寸重量无效
- **Refurbished 资质**:25041–25052(25047 卖家无资质 · 25048 类目无资质 · 25049 品牌无资质 · 25041–25046 六条硬性政策要求:处理时长/免运费/接受退货/Money Back/退货天数/卖家承担退运费 · 25050/25051 标题副标题禁用词 · 25052 最少图片数)
- **listing 状态**:25019 不能改 listing · 25038/25039/25040 有出价或 12 小时内结束 · 25097 listing 因违规被 hold 无法修改 · 25098 父 listing 被 hold · 25713 offer 不可用 · 25752 listingStartDate 无效
- **配额**:🔴 **25026 Selling limit exceeded** · **变体组**:25013(1.18.5 起指向多个可能错误)· **兼容性**:25023 · **字段超长**:25080 "Field must not exceed `{replaceable_value}` characters"
- **欧盟合规(GPSR/EPR)**:25076–25081 · 25083–25085 · 25088 · 25104 · 25106–25119 · 25122

**200/201 警告(7 条)**:`25096` listing 因违规被 hold **但仍可修改** · `25124` P&A 退货政策已被 eBay 自动改 · `25126` Certification/Grade 等 aspect 即将弃用 · `25401` 无效 listing format 被移除 · `25402` 系统警告 · `25504` service · `25753` listingStartDate 在过去。**500**:`25001` 系统错误 · `25025` 同一 Inventory/InventoryItemGroup **不允许并发访问**。

**逐条语义速查**

| 码 | 语义(官方文案要点) | 出处 |
|---|---|---|
| 25002 | 通用用户错误;⚠ **同一码被用于多种用户错误**("Offer entity already exists" 与 "Add at least 1 photo" —— 后者是社区来源)⇒ 不能只按 errorId 分支 | [V-spec]/[M];混用说明 ⚠ 社区 |
| 25012 / 25018 / 25019 | 仓位无效(`merchantLocationKey` 未建或错)/ 账号信息不完整 / Cannot revise listing | [V-spec]、[M] |
| 25014 / 25015 / 25501 / 25086 | 图片无效 / 图片 URL 无效 / 图片无效 / URL 必须是 eBay 图片服务 URL(**Inventory 侧图片错误只有这四个**) | [V-spec] |
| 25016 / 25017 / 25029 | `{fieldName}` 值无效 / `{fieldName}` 缺失 / `{field}` is required for this category(= aspects 必填项没给) | [V-spec]、[M] |
| 25025 | **500** —— "Concurrent access of the same Inventory or Inventory Item Group object is not allowed. Please try again later." | [V-spec] |
| 25026 | Selling limit exceeded(平台硬闸) | [V-spec] |
| 25702 / 25707 / 25710 | `{skuValue}` 找不到 / SKU 只能字母数字且长度 ≤ 50 / 找不到实体 | [V-spec] |
| 25713 | **This Offer is not available**(offer 不存在或状态不对) | [M] |
| 25729 | "The combination of SKU, marketplaceId and format should be unique."(**只在 bulkCreateOffer 的 400 清单里,单条 createOffer 清单没有**) | [M] |

**offer 侧错误码(⚠ 全部 [M] 1.17.4,上线前必须补当前版)**:`createOffer` 400(11)= 25702 · 25709 · 25752 · 25755–25758 · 25761–25764(后八条全是 auction 相关);`bulkCreateOffer` 400(14)= 上面 + `25730` + `25729` + `25735`;`publishOffer` 400(60)/ 200 警告(7)/ 500(2);`bulkPublishOffer` 400 = publishOffer 60 条 + `25709` + `25730` + `25731` + `25732`;`getOffer`/`getOffers` 400 = `25706` · `25709` · `25713`;`withdrawOffer` 400 = `25002` · `25713`;`deleteOffer` 400 = `25709`;`getListingFees` 400 = `25709` · `25713`。

🔴 **`190204` 不是 Inventory 侧错误码**:官方 Inventory OAS3(1.18.5)`createOrReplaceInventoryItem` 的 400 全表 96 条里**没有 190204**;Inventory 侧图片错误是 25014 / 25015 / 25501 / 25086。`190204` 的量级(19xxxx)属于 **Trading API** 错误码空间。

### 1.9 可直接抄的三段请求体(一期单 SKU FIXED_PRICE)

```http
PUT https://api.ebay.com/sell/inventory/v1/inventory_item/EB0000012345
Authorization: Bearer <user token>   /   Content-Type: application/json   /   Content-Language: en-US
```
```json
{ "availability": { "shipToLocationAvailability": { "quantity": 5 } }, "condition": "NEW",
  "product": { "title": "≤80 chars, 已过 scrub_brand 与停用词",
    "description": "≤4000 chars 的短摘要(长文案不放这里)",
    "aspects": { "Brand": ["Unbranded"], "Type": ["Wireless Charger"], "Color": ["Black"] },
    "imageUrls": ["https://…/1.jpg", "https://…/2.jpg"],
    "brand": "Unbranded", "mpn": "Does not apply", "upc": ["012345678905"] },
  "packageWeightAndSize": { "weight": { "value": 1.2, "unit": "POUND" },
    "dimensions": { "length": 8.0, "width": 6.0, "height": 2.0, "unit": "INCH" } } }
```
> ⚠ `mpn` / GTIN 的替代文本必须从 registry 的 21 行站点专属表取,**代码里不许出现 `"Does not apply"` 字面量** —— 上面是示意值。

```http
POST https://api.ebay.com/sell/inventory/v1/offer
Authorization: Bearer <user token>   /   Content-Type: application/json   /   Content-Language: en-US
```
```json
{ "sku": "EB0000012345", "marketplaceId": "EBAY_US", "format": "FIXED_PRICE",
  "availableQuantity": 5, "categoryId": "175673", "listingDuration": "GTC",
  "merchantLocationKey": "WH-US-01",
  "listingDescription": "<h3>…</h3> 长文案,≤500000 含 HTML",
  "includeCatalogProductDetails": false,
  "pricingSummary": { "price": { "value": "29.99", "currency": "USD" } },
  "listingPolicies": { "fulfillmentPolicyId": "6…", "paymentPolicyId": "6…", "returnPolicyId": "6…" } }
```
> 🔴 三个易错点:`price.value` 是**字符串**;`includeCatalogProductDetails` **必须显式给**(缺省 true);长文案走 `listingDescription` 不走 `product.description`。

```http
POST https://api.ebay.com/sell/inventory/v1/offer/{offerId}/publish       # 只带 Authorization,无 body
→ 200  { "listingId": "110465561234", "warnings": [] }
```

---

## 二、OAuth 参考
### 2.1 token 端点 `POST /identity/v1/oauth2/token`

三种 grant 共同形状 [verified oauth-client-credentials-grant.html / oauth-auth-code-grant-request.html / oauth-refresh-token-request.html]:`POST https://api.ebay.com/identity/v1/oauth2/token`(sandbox `https://api.sandbox.ebay.com/identity/v1/oauth2/token`,**路径逐字不变**),`Content-Type: application/x-www-form-urlencoded`,`Authorization: Basic <B64-encoded-oauth-credentials>`。**Basic 串构造(官方逐字两句)**:"Base64 encode the following: <`client_id`>**:**<`client_secret`>";"precede your B64-encoded credentials with the word "`Basic `" and a space."

⚠ **scope 串不跟着换 host**:官方 curl 打的是 `api.sandbox.ebay.com` 的 token 端点,而 body 里的 scope 是 `https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope`(**api.ebay.com**)[verified curl 原样];另有官方句 "it's possible the Sandbox and Production environments support different sets of **scopes** for your application." [verified oauth-scopes.html]。keyset 分环境 [verified oauth-credentials.html]:"eBay provides different sets of credentials for the **Sandbox** and **Production** environments",且 "application specific—if you have multiple applications, you'll have different sets of credentials for each app."

**① `grant_type=client_credentials`(应用令牌)**:`grant_type`(必填,字面量)+ `scope`(必填,"URL-encoded space-separated list of the scopes")。

```bash
curl -X POST 'https://api.sandbox.ebay.com/identity/v1/oauth2/token' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -H 'Authorization: Basic UkVTVFRlc3...wZi1hOGZhLTI4MmY=' \
  -d 'grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope'
```
响应(**无 refresh_token**):`{ "access_token": "v^1.1#…", "expires_in": 7200, "token_type": "Application Access Token" }`;官方逐字 "expires_in element is set to 7,200 seconds, meaning this token is valid for two hours."

**② `grant_type=authorization_code`(首次换码)** [verified oauth-auth-code-grant-request.html;⚠ 该 grant 官方页未给 curl,只给字段表与响应样例]

| 字段 | 值 | 必填 | 说明 |
|---|---|---|---|
| `grant_type` | `authorization_code` | 是 | 字面量 |
| `code` | `<authorization-code-value>` | 是 | consent 回调拿到的码,编码规则见 §2.3 |
| `redirect_uri` | `<RuName-value>` | 是 | 🔴 **填 RuName,不是真 URL**,且必须与 consent 请求里那个一致 |

响应**五个字段**(两页同形):`{ "access_token": "v^1.1#…", "expires_in": 7200, "refresh_token": "v^1.1#…", "refresh_token_expires_in": 47304000, "token_type": "User Access Token" }`。`refresh_token_expires_in = 47304000` 秒 = **547.5 天 ≈ 18 个月**;`token_type` 两个取值 **`"Application Access Token"` / `"User Access Token"`** 是响应里真实存在的判别位。

**③ `grant_type=refresh_token`(刷新)**:`grant_type`(必填)+ `refresh_token`(必填,18 个月内不变)+ `scope`(**可选**)。

```bash
curl -X POST 'https://api.sandbox.ebay.com/identity/v1/oauth2/token' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -H 'Authorization: Basic RGF2eURldmUtRG2 ... ZTVjLTIxMjg=' \
  -d 'grant_type=refresh_token&refresh_token=v^1.1#i^1#p^3# ... fMSNFXjEyODQ=&
      scope=https://api.ebay.com/oauth/api_scope/sell.account%20https://api.ebay.com/oauth/api_scope/sell.inventory'
```
响应**三个字段,结构上就没有 `refresh_token` 键**:`{ "access_token": "v^1.1#…", "expires_in": 7200, "token_type": "User Access Token" }`;官方逐字 "You can continue to use the refresh token to mint new User access tokens for a specific user, as long as the refresh token associated with their account is valid."
🔴 **scope 子集规则(官方逐字)** [verified oauth-scopes.html]:"If you do specify a **scope** parameter, the included scope values must be equal to or a subset of the scope values included in the consent request."

**三种 grant 的日配额**(同一张官方表)[verified oauth-tokens.html]:`client_credentials` **1,000/day** · `authorization_code` **10,000/day** · `refresh_token` **50,000/day**。
**失效形态** [verified KBid=5218]:`{"error":"invalid_grant","error_description":"the provided authorization grant code is invalid or was issued to another client"}`;⚠ 凭证错时是 `{"error":"invalid_client","error_description":"client authentication failed"}` / HTTP 401 ⚠[indexed]。同页逐字列的四条失效原因:① eBay 客服吊销;② 用户改登录名或密码;③ 用户改登录凭据;④ "If a user that was associated with your application **revoked the token from their MyeBay page**"。补救句:"If your refresh token gets revoked (or if it expires), then you must **redo the consent-request flow** in order to get a new access token and refresh token for the associated user."
**令牌类型判据两句** [verified oauth-tokens.html]:应用令牌 "Good examples of methods that require **application tokens** are **metadata or taxonomy** calls.";用户令牌 "**user tokens**, which are used for methods that **post or return data that is specific to an eBay user**."

### 2.2 consent URL 七参数(`GET https://auth.ebay.com/oauth2/authorize`,sandbox `auth.sandbox.ebay.com`)

| 参数 | 必填 | 官方描述(逐字) |
|---|---|---|
| `client_id` | Yes | "The **client_id** value for the environment you're targeting." |
| `redirect_uri` | Yes | "The **RuName** value for the environment you're targeting."(🔴 不是真 URL) |
| `response_type` | Yes | "Set to `code` to have eBay generate and return an _authorization code_." |
| `scope` | Yes | "A list of OAuth scopes that provide access to the resources used by your application." |
| `state` | Optional | "An opaque value used by the client to maintain state between the request and callback." |
| `prompt` | Optional | "If needed, you can force a user to log in when you redirect them to the **Grant Application Access** page, even if they already have an existing user session." |
| `locale` | Optional | "The **locale** parameter to localize the OAuth consent page for the marketplace you're targeting." |

[verified oauth-consent-request.html —— 七行描述句逐字出自该页参数表]。`prompt` 的**取值** `login` 出自另一页:"`prompt` - Optional; set to `login` to force re-authentication" [verified oauth-authorization-code-grant.html]。官方示例 URL 形状(`scope` 是 URL 编码的空格分隔串)[verified]:`GET https://auth.sandbox.ebay.com/oauth2/authorize?client_id=<app-client-id>&redirect_uri=<app-RuName>&response_type=code&scope=<scopeList>&state=<custom-state-value>`。

### 2.3 code 处理要点
授权后用户在 **Grant Application Access** 页点 **Agree**,eBay **302 到 RuName 里配置的 Auth Accepted URL**,回调带三个 query 参数 `code` / `state`(传了才回)/ `expires_in` [verified 两页]:`https://www.example.com/acceptURL.html?state=<client_supplied_state_value>&code=v%5E1.1%…NjA%3D&expires_in=299`。

- 🔴 **URL 编码规则(官方逐字)** [verified oauth-auth-code-grant-request.html]:"The authorization code returned by eBay is URL-encoded. This value must be URL-encoded when you pass the value in the `code` parameter of the authorization code grant request. However, **if the method you use to make the request URL-encodes the values you pass, then you must URL-decode the authorization code before using it.**" ⇒ `httpx` 的 `data={...}` 自己会做一次 form 编码 ⇒ **必须先 `unquote()` 恰好一次**,否则双重编码、eBay 回 `invalid_grant`。
- **长度上限**:"The authorization code is a maximum of **1024 characters** in length." [verified oauth-consent-request.html]
- **有效期**:官方**没有一句"有效 N 分钟"的直述句**;唯一的数字是回调示例里的 **`expires_in=299`**(≈5 分钟)⇒ ⚠ **示例值,不是文档承诺值,不要写死 299**。
- **拿 code 两条路**:**A. 自建 Auth Accepted URL** —— RuName 填一个自己控制的 https 页面,点 Agree 后从**浏览器地址栏**拷 `code=`;eBay 只做 302,**页面 404 也拿得到码**。**B. 开发者后台令牌工具** —— Application Keys → **User Tokens** → **Get a User Token Here** → **OAuth (new security)** → sign-in → **Agree**,后台直接显示;🔴 官方只写 "The User access token is returned"(2 小时),⚠ **是否连 refresh token 一起给未取到确证** [verified oauth-ui-tokens.html 六步与按钮名逐字]。

### 2.4 RuName 要点
- 是什么(官方逐字)[verified oauth-redirect-uri.html]:RuName(也叫 "eBay Redirect URL name")"contains several pieces of information, including the accept URL and reject URL values, which lets you customize different pages, depending on how the user responds to the permissions grant request."
- 后台六步(逐字):① **Your Account > Application Keys** → ② 点 Client ID 旁的 **User Tokens** → ③ 展开 **Get a Token from eBay via Your Application** → ④ 没建过点 **"You have no Redirect URLs. Click here to add one."** → ⑤ 填 **Confirm the Legal Address for the Primary Contact or Business** → ⑥ **Continue to create RuName**。
- 承载三条 URL(官方描述):**Privacy Policy URL** "The URL where you host your privacy policy";**Auth Accepted URL** "eBay redirects the user to this URL if the user grants your application the permissions";**Auth Declined URL** "…if the user does not grant your application the permissions"。
- 与 `redirect_uri` 的关系(逐字):"set **redirect_uri** to the _RuName_ value assigned to your application" —— **consent 请求与换码请求两处都填它**。🔴 `.env` 里存的是 **RuName 字符串本身**(形如 `Your-AppName-XXXX-abcdef`),**不是那三条 URL**;三条 URL 只活在 eBay 后台。

---

## 三、户口链参考(最小可用请求体)
**base URL 与族级头** [verified 两份 OAS3 的 servers / parameters 段]:Account `https://api.ebay.com/sell/account/v1`、Inventory `https://api.ebay.com/sell/inventory/v1`(sandbox 只换 host,路径不变)。🔴 **Account API 的写操作只声明了一个 header 参数**:`Content-Type`(header,**required: true**),描述逐字 "Its value should be set to application/json";**`Content-Language` / `X-EBAY-C-MARKETPLACE-ID` 在 Account 的三个 POST 政策与 opt_in 里都没有声明**。⚠ Inventory 族 location 端点的头要求**本轮未取到原文**(OAS3 截断;方法页索引称除 `Authorization` 外 "All other standard RESTful request headers are optional" ⚠[indexed])。令牌:本节全部端点用**用户令牌**,Account 族 scope `sell.account`(读可用 `.readonly`),`/location` 是 `sell.inventory` ⚠[indexed]。

### 3.1 opt-in `SELLING_POLICY_MANAGEMENT`

```http
POST {account_base}/program/opt_in          # Content-Type: application/json;scope sell.account(仅写这一条)
{ "programType": "SELLING_POLICY_MANAGEMENT" }
```
- path / operationId / 必填 `Content-Type` / 响应码 **200, 400, 404, 409, 500** / scope —— [verified sell_account_v1_oas3.json];请求体 schema 名 = `Program`(属性因 components 截断未取到)。请求体只有 `programType` 一个字段,枚举含 `SELLING_POLICY_MANAGEMENT`,另两值 `OUT_OF_STOCK_CONTROL` / `PARTNER_MOTORS_DEALER` ⚠[indexed]。
- ⚠ **409 存在于 spec 是 verified 的,其确切语义(是否 = 已 opt-in)未取到原文**。⚠ "It can take up to **24-hours** for eBay to process your request to opt-in to a Seller Program. Use the getOptedInPrograms call to check the status of your request after the processing period has passed." ⚠[indexed optInToProgram 方法页]
- 依据链(**verified**)[business-policies.html]:"To take advantage of the business policies you create, you must opt-in to the `SELLING_POLICY_MANAGEMENT` seller program using the **optInToProgram** call in the Account API." ⇒ 沙箱与生产同一套前置;`optInToProgram` "is supported in Sandbox environment" ⚠[indexed]。
- **回读**:`GET {account_base}/program/get_opted_in_programs`(无任何参数;scope `sell.account` 或 `.readonly`),响应 ⚠[indexed]:`{ "programs": [ { "programType": "SELLING_POLICY_MANAGEMENT" } ] }`;"an empty array is returned if the seller is not opted in to any of the seller programs"。

### 3.2 fulfillment policy —— `handlingTime` 位置官方两页矛盾
`POST {account_base}/fulfillment_policy/`(🔴 **OAS3 里这个 path 带尾斜杠**),`Content-Type: application/json` [verified OAS3:operationId `createFulfillmentPolicy`,响应码 **201 / 400 / 500**,scope `sell.account`]。

✅ **顶层**(官方完整样例逐字)[verified `ht_shipping-worldwide.html`]:
```json
{ "categoryTypes": [ { "name": "ALL_EXCLUDING_MOTORS_VEHICLES" } ], "marketplaceId": "EBAY_US",
  "name": "Worldwide shipping options: Free domestic, CALCULATED int'l", "globalShipping": "false",
  "handlingTime": { "unit" : "DAY", "value" : "1" },
  "shippingOptions": [
    { "costType": "FLAT_RATE", "optionType": "DOMESTIC", "shippingServices": [ { "buyerResponsibleForShipping": "false",
        "freeShipping": "true", "shippingCarrierCode": "USPS", "shippingServiceCode": "USPSPriorityFlatRateBox",
        "shippingCost": { "currency": "USD", "value": "0.0" } } ] },
    { "costType": "CALCULATED", "optionType": "INTERNATIONAL", "shippingServices": [ { "buyerResponsibleForShipping": "true",
        "freeShipping": "false", "shippingCarrierCode": "USPS", "shippingServiceCode": "USPSPriorityMailInternational",
        "shipToLocations": { "regionIncluded": [ { "regionName": "Worldwide" } ] } } ] } ] }
```
❌ **同族另一页把 `handlingTime` 嵌进 `shippingOptions[i]` 里**(样例逐字)[verified `ht_shipping-free.html`]:`{ …, "shippingOptions": [ { "costType": "FLAT_RATE", "optionType": "DOMESTIC", "shippingServices": [ {…} ], "handlingTime": { "unit": "DAY", "value": "1" } } ] }`。第三方判据:`FulfillmentPolicyRequest` 类型页把 `handlingTime` 列为**策略对象的字段**(`TimeDuration` 容器,`unit` ∈ `TimeDurationUnitEnum{YEAR, MONTH, DAY}` + `value` 整数),`ShippingOption` 的字段清单里没有它 ⚠[indexed]。⚠ **两种形状哪种被拒未实测**。

**最小体(字段形状 verified、取值非官方)**:
```json
{ "name": "EBAY-US-DROPSHIP-STD", "marketplaceId": "EBAY_US",
  "categoryTypes": [ { "name": "ALL_EXCLUDING_MOTORS_VEHICLES" } ],
  "handlingTime": { "unit": "DAY", "value": "3" },
  "shippingOptions": [ { "costType": "FLAT_RATE", "optionType": "DOMESTIC",
    "shippingServices": [ { "sortOrder": 1, "shippingCarrierCode": "USPS",
      "shippingServiceCode": "USPSPriorityFlatRateBox", "freeShipping": true,
      "buyerResponsibleForShipping": false } ] } ] }
```
`handlingTime` 官方定义:"the maximum number of business days the seller commits to for preparing and shipping an order after receiving a cleared payment ... does not include the transit time" ⚠[indexed];另一句 verified:"the **handlingTime** field specifies how much time the seller will take to ship the order from the time of sale." [ht_shipping-free.html]。**回读**:`GET {account_base}/fulfillment_policy?marketplace_id=EBAY_US`(`marketplace_id` **required**)[verified OAS3];另有 `GET /fulfillment_policy/{fulfillmentPolicyId}` / `PUT` / `DELETE`。

### 3.3 payment policy(managed payments 下不填 `paymentMethods`)
`POST {account_base}/payment_policy`(**无尾斜杠**,与 fulfillment 不同,照 OAS3 逐字),`Content-Type: application/json` [verified OAS3:`createPaymentPolicy`,响应码 **201 / 400 / 500**,scope `sell.account`]。最小体(字段名 ⚠[indexed PaymentPolicyRequest / createPaymentPolicy]):
```json
{ "name": "EBAY-US-MP-STD", "marketplaceId": "EBAY_US",
  "categoryTypes": [ { "name": "ALL_EXCLUDING_MOTORS_VEHICLES" } ], "immediatePay": true }
```
🔴 **`paymentMethods` 在 managed payments 下不填**,官方两句(索引到的正文)⚠[indexed]:"Because eBay controls all electronic payment methods, sellers do not need to specify a payment method and the `deposit.paymentMethods` array is not needed.";"Sellers do not have to specify any electronic payment methods for listings, so this array will often be returned empty unless the payment business policy is intended for motor vehicle listings or other items in categories where offline payments are required or supported." ⚠ 到底可不可省未实测。`immediatePay: true` 是**工程建议非官方必填**;官方注意句 "Immediate payment is not applicable for auction listings ... Best Offer ... or transactions that happen offline" ⚠[indexed]。

### 3.4 return policy
`POST {account_base}/return_policy`,`Content-Type: application/json`。⚠ **该 path 在本轮读到的 OAS3 片段里 NOT PRESENT**(spec 尾部截断)⇒ 路径与响应码按方法页索引 ⚠[indexed createReturnPolicy]。
```json
{ "name": "EBAY-US-RET-30D-SELLER", "marketplaceId": "EBAY_US",
  "categoryTypes": [ { "name": "ALL_EXCLUDING_MOTORS_VEHICLES" } ],
  "returnsAccepted": true, "returnPeriod": { "unit": "DAY", "value": 30 },
  "returnShippingCostPayer": "SELLER", "refundMethod": "MONEY_BACK" }
```
- 字段语义 ⚠[indexed ReturnPolicyRequest / ReturnPolicy]:`returnsAccepted` 布尔;`returnPeriod` = 买家可退的**日历天**,"begins when the item is marked 'delivered'","Most categories support 30-day and 60-day return periods";`returnShippingCostPayer` ∈ {`BUYER`, `SELLER`};`refundMethod` = 卖家提供的退款方式。
- ✅ `returnPeriod` 条件必填有 verified 出处:Account API v1 **1.2.0(2018-05-31)** 逐字 "**returnsPeriod** - This field is now required if the seller accepts returns for either domestic or international returns." [verified release-notes-archive.html] —— ⚠ **官方那行把字段名写成了 `returnsPeriod`,实际字段名是 `returnPeriod`**,别照 release note 抄字段名。
- ⚠ **`categoryTypes` 填不填两来源打架**:一处称 "the return policy schema does not provide for a categorytypes field ... categorytypes is hard-set to ALL_EXCEPT_MOTORS_VEHICLES";另一处 `ReturnPolicyRequest` 字段清单里**明列 `categoryTypes`** ⚠[两条均 indexed]。
- 业务侧(verified business-policies.html):"For motor vehicle listings, all returns are handled at the seller's discretion and are processed outside of the eBay flow. Because of this, you cannot associate an eBay return policy with a motor vehicle item"。

### 3.5 `createInventoryLocation`
`POST {inventory_base}/location/{merchantLocationKey}`,`Content-Type: application/json`,scope `https://api.ebay.com/oauth/api_scope/sell.inventory` ⚠[indexed createInventoryLocation —— OAS3 截断,path 与 scope 取自索引正文]。**最小体(仓库型 warehouse,美站)**:`{ "location": { "address": { "postalCode": "95125", "country": "US" } }, "name": "EBAY-US-WH-01" }`

必填规则(**三句 verified**)[verified `managing-inventory-locations.html`]:warehouse —— "Warehouse locations are used for traditional shipping and **only require the name and basic address (postalCode and country OR city, state, and country) fields** to be specified.";store —— "A full address (**addressLine1**, **city**, **stateOrProvince**, **postalCode**, and **country**) is required";fulfillment center 同 store。`merchantLocationKey`(**放在 URI 里,不在 body 里**):"A **merchantLocationKey** value cannot be changed once it is set, and its length cannot exceed **50** characters." [verified 同页]

其余三条 ⚠[indexed createInventoryLocation / InventoryLocationFull]:`locationTypes` —— "If the `locationTypes` container is omitted ... the location will default to **WAREHOUSE**",取值 `STORE` / `WAREHOUSE` / `FULFILLMENT_CENTER`;`merchantLocationStatus` —— "set its value to `DISABLED` to create a disabled location. If this field is omitted, a successful createInventoryLocation call will automatically **enable** the location."(与 verified 的 "Although the default behavior for a createInventoryLocation call is to enable that inventory location…" 互相印证;⚠ `ENABLED` 这个取值本轮未在原文见到);响应 —— "A successful call will return an HTTP status value of **204 No Content**. Unless one or more errors and/or warnings occurs with the call, **there is no response payload for this call**." ⇒ **不回 body、不回 id**,`merchantLocationKey` 只能由调用方自己生成;回读用 `GET {inventory_base}/location`。⚠ 政策端点与本端点**都没有幂等键**,重复 POST 会建出重名的第二条(**该行为本身未核验**)。

### 3.6 `getPrivileges` 响应结构
`GET {account_base}/privilege` [verified OAS3:operationId `getPrivileges`,**无任何参数**,响应码 200/400/500,scope `sell.account` 或 `.readonly`];⚠ 官方 how-to 页示例写作 `GET https://api.ebay.com/sell/account/v1/privilege/`(**带尾斜杠**),"The request uses no body or URI parameters." [verified `ht_get-selling-limits.html`] ⇒ 两处尾斜杠不一致。完整响应样例(逐字)[verified 同页]:

```json
{ "sellingLimit": { "amount": { "value": "100.0", "currency": "USD" }, "quantity": 10 },
  "sellerRegistrationCompleted": true }
```
- `sellerRegistrationCompleted`:"returned as `true` if the seller's registration is completed, or `false` if the registration process is not complete" ⚠[indexed SellingPrivileges]。
- 🔴 **周期语义官方两页对着写**:Account OAS3 的 `getPrivileges` 描述写 "site-wide `sellingLimit` (the amount and quantity they can sell **on a given day**)";`SellingPrivileges` 类型页写 "This container lists the **monthly** cap for the quantity of items sold and total sales amount allowed for the seller's account, though it **may not be returned if a seller does not have a monthly cap**." ⚠[indexed] ⇒ **`sellingLimit` 可能整个不返回**。
- ⚠ 该页的"提额"指路句是 "To increase your selling limit, see the _Increasing your call limits_ section on the Support for application development page." —— 它指的是 **call limits**(配额)不是 selling limits(上架量),**官方这句自己串味了**。

---

## 四、Taxonomy 参考
### 4.1 端点全集与 scope

spec 元信息 [V-fetch Taxonomy OAS3]:`info.version` = **`v1.1.1`**;`servers` = `https://api.ebay.com{basePath}`,`basePath` 默认 **`/commerce/taxonomy/v1`**。⚠ `sell-categories.html` 示例 URL 里写的还是 `/commerce/taxonomy/v1_beta/` —— **陈旧页面**;Taxonomy API **v1.0.0 于 2020-10-15 GA**。九个端点:`getDefaultCategoryTreeId`(`GET /get_default_category_tree_id?marketplace_id=`)· `getCategoryTree`(`GET /category_tree/{id}`)· `getCategorySubtree`(`.../get_category_subtree?category_id=`)· `getCategorySuggestions`(`.../get_category_suggestions?q=`)· `getItemAspectsForCategory`(`.../get_item_aspects_for_category?category_id=`)· `fetchItemAspects`(`.../fetch_item_aspects`)· `getCompatibilityProperties` / `getCompatibilityPropertyValues`(汽配兼容性)· `getExpiredCategories`(`.../get_expired_categories`)。

**认证**:`securitySchemes` 只有一个 `api_auth`,**只声明 `clientCredentials` flow**(全族应用令牌)[V-fetch]:
```json
"api_auth": { "type":"oauth2", "flows": { "clientCredentials": { "tokenUrl": "https://api.ebay.com/identity/v1/oauth2/token",
   "scopes": { "https://api.ebay.com/oauth/api_scope": "View public data from eBay",
               "https://api.ebay.com/oauth/api_scope/metadata.insights": "View metadata insights such as aspect relevance." }}}}
```
逐端点 `security` 块 [V-fetch]:除 **`getItemAspectsForCategory`** 与 **`fetchItemAspects`** 两个端点同时列了 `api_scope` **+** `api_scope/metadata.insights` 外,其余七个只列 `api_scope`。⚠ OpenAPI 语义上"一个 security requirement object 里列两个 scope"= 两个都要,**但 eBay 是否真的强制没有直述句**。

### 4.2 `getDefaultCategoryTreeId`
请求 `GET /commerce/taxonomy/v1/get_default_category_tree_id?marketplace_id=EBAY_US`;描述(逐字)[V-fetch]:"…This call retrieves a reference to the default category tree associated with the specified eBay marketplace ID. The response includes only the tree's unique identifier and version, which you can use to retrieve more details about the tree, its structure, and its individual category nodes." 响应 schema = `BaseCategoryTree`,**只有两个字段**:`categoryTreeId` string "The unique identifier of the eBay category tree for the specified marketplace." / `categoryTreeVersion` string "The version of the category tree identified by `categoryTreeId`."

**EBAY_US 的实值**:`{"categoryTreeId": "0", "categoryTreeVersion": "117"}` [V-fetch buy-categories.html 官方样例] —— 🔴 `categoryTreeId="0"` 是 EBAY_US 的固定树 ID;⚠ **`"117"` 是文档样例值,不是当前值**。HTTP 码:200 / **204 No content** / 400(**62002** 缺 marketplace ID、**62003** marketplace ID 不存在)/ 500(62000)。⚠ **204 时 body 为空,`resp.json()` 会抛**。

### 4.3 `getCategoryTree` / `getCategorySubtree` / `getExpiredCategories`
描述(逐字)[V-fetch]:"This method retrieves the complete category tree that is identified by the `category_tree_id` parameter … The response contains details of all nodes of the specified eBay category tree, as well as the eBay marketplaces that use this category tree." **gzip(逐字)**:"This method can return a very large payload, so gzip compression is supported.";"To enable gzip compression, include the `Accept-Encoding` header and set its value to `gzip`";规模 [V-fetch buy-categories / sell-categories 同句]:"This call can return a very large payload (**tens of thousands of categories**), so you are strongly advised to submit the request with the following HTTP header: `Accept-Encoding: gzip`"。⚠ **官方没有给"US 树共 N 个类目 / 响应 N MB"的具体数字**,"tens of thousands" 是唯一官方量级词。

`CategoryTree` 四字段:`applicableMarketplaceIds` array of string "A list of one or more identifiers of the eBay marketplaces that use this category tree." / `categoryTreeId` / `categoryTreeVersion` / `rootCategoryNode` `CategoryTreeNode` "Contains details of all nodes of the category tree hierarchy."

`CategoryTreeNode`(**递归节点**,整棵树就是它嵌套出来的)

| 字段 | 类型 | 官方描述(逐字) |
|---|---|---|
| `category` | `Category` | "Contains details about the current category tree node." |
| `categoryTreeNodeLevel` | integer | "The absolute level of the current category tree node in the hierarchy." |
| `childCategoryTreeNodes` | array of `CategoryTreeNode` | "An array of one or more category tree nodes that are the immediate children." |
| `leafCategoryTreeNode` | boolean | "A value of true indicates that the current node is a leaf node." |
| `parentCategoryTreeNodeHref` | string | "The href portion of the `getCategorySubtree` call that retrieves the subtree below the parent." |

`Category`:`categoryId` "The unique identifier of the eBay category within its category tree." / `categoryName` "The name of the category identified by `categoryId`."
🔴 **`parentCategoryTreeNodeHref` 是单数,`CategoryTreeNode` 没有"多父"表达** ⇒ eBay 类目树是**严格树不是 DAG**(与亚马逊 browse tree 相反)。🔴 eBay **只允许把 listing 挂在叶子类目上** —— 机读证据是 `getItemAspectsForCategory` 的错误码 **62009**「category ID must be leaf」与 **62008**「category ID is root」[V-fetch]。HTTP 码:200 / 400(**62004** 树 ID 不存在)/ 404 / 500(62000)。

**`getCategorySubtree`**:描述逐字 "This call retrieves the details of all nodes of the category tree hierarchy (the subtree) below a specified category of a category tree.";参数 `category_tree_id`(path)+ **`category_id`(query,required)**"The unique identifier of the category at the top of the subtree being requested." + `Accept-Encoding`(同上两句 gzip 原文);响应 `CategorySubtree` = `{ categorySubtreeNode: CategoryTreeNode, categoryTreeId, categoryTreeVersion }`。

**版本比对官方口径(两处互证)**:[V-fetch buy-categories]"You will not need to retrieve this category tree again, unless eBay publishes a new version. You can determine this by using the **getDefaultCategoryTreeId** method to retrieve and compare the `categoryTreeVersion` field to the one you have cached.";[V-fetch listing-metadata-guide]"Use `categoryTreeVersion` to monitor changes in the category tree. If a new version is detected, call `getCategoryTree` or `getCategorySubTree` to fetch the updated categories." ⚠ `sell-categories.html` 同句主语写成 `getCategoryTree` —— **陈旧页面写法**。**更新节奏两句并存**:[V-fetch bb-categories.html]"Categories and/or the category hierarchy for an eBay marketplace are typically updated **about once per quarter**.";另一处写 **monthly**("but may be updated more frequently")。

**`getExpiredCategories`** 描述(逐字)[V-fetch]:"This method retrieves the mappings of expired leaf categories in the specified category tree to their corresponding active leaf categories. Note that in some cases, several expired categories are mapped to a single active category.";并明确 "this method only returns information about categories that have been mapped (i.e., combined categories and split categories). It does not return information about expired categories that have no corresponding active categories."

### 4.4 `getItemAspectsForCategory`
描述(逐字,完整)[V-fetch]:"This call returns a list of aspects that are appropriate or necessary for accurately describing items in the specified leaf category … For each aspect, `getItemAspectsForCategory` provides complete metadata, including: The aspect's data type, format, and entry mode; Whether the aspect is required in listings; Whether the aspect can be used for item variations; Whether the aspect accepts multiple values for an item; Allowed values for the aspect … **Once you collect those values, include them as product aspects when creating inventory items using the Inventory API.**"

`AspectMetadata` = `{ aspects: Aspect[] }` —— "A list of item aspects (for example, color) that are appropriate for describing items in a leaf category."。`Aspect` 四字段:`localizedAspectName` "The localized name of this aspect." / `aspectConstraint` "Information about the formatting, occurrence, and support of this aspect." / `aspectValues` "A list of valid values for this aspect." / `relevanceIndicator` "The relevance of this aspect based on search performance data."

**`AspectConstraint` —— 11 个字段全表**(字段与描述 [V-fetch];枚举取值 [V-index `txn:AspectConstraint`])

| 字段 | 类型 | 官方描述(逐字) | 取值 |
|---|---|---|---|
| `aspectRequired` | boolean | "A value of true indicates this aspect is required." | true/false |
| `aspectMode` | string(`AspectModeEnum`) | "The manner in which values must be specified (free text or selection)." | **`FREE_TEXT` / `SELECTION_ONLY`** [V-index] |
| `itemToAspectCardinality` | string(`ItemToAspectCardinalityEnum`) | "Indicates whether this aspect accepts single or multiple values." | **`SINGLE` / `MULTI`** [V-index] |
| `aspectMaxLength` | integer | "The maximum length of the aspect's value." | — |
| `aspectDataType` | string(`AspectDataTypeEnum`) | "The data type of this aspect." | **`STRING` / `NUMBER` / `DATE`**(⚠ 是否还有 `STRING_ARRAY` 未取到)[V-index] |
| `aspectFormat` | string | "Specific formatting requirements (DATE: YYYY, YYYYMM, YYYYMMDD; NUMBER: int32, double)." | 见描述 |
| `aspectUsage` | string(`AspectUsageEnum`) | "Indicates if the aspect is recommended or optional." | **`RECOMMENDED` / `OPTIONAL`**(两个都不是"必填")[V-index] |
| `aspectEnabledForVariations` | boolean | "A value of true indicates this aspect can identify item variations." | true/false |
| `aspectApplicableTo` | array(`AspectApplicableToEnum`) | "Indicates if the aspect is a product or item/instance aspect." | **`ITEM` / `PRODUCT`** [V-index] |
| `expectedRequiredByDate` | string | "The expected date after which the aspect will be required." | 日期 |
| `aspectAdvancedDataType` | string | "Indicates additional data type requirements." | ⚠ 取值未取到 |

> `aspectAdvancedDataType` 是 **2025-05-07 / Taxonomy v1.1.1** 新增字段 [V-fetch release-notes:"new **aspectConstraint** field, **aspectAdvancedDataType**, was added to the fetchItemAspects and getItemAspectsForCategory responses"]。

`AspectValue`:`localizedValue` "The localized value of this aspect." / `valueConstraints` "List of dependencies identifying when this value is available."。`ValueConstraint`:`applicableForLocalizedAspectName` "The name of the control aspect on which the current value depends." / `applicableForLocalizedAspectValues` "List of control aspect values enabling this value." ⇒ 同一 aspect 的合法取值**可能依赖另一个 aspect 的取值**(典型:`Model` 的可选值取决于 `Brand`)。`RelevanceIndicator`:含 `searchCount` —— "the number of recent searches (**based on 30 days of data**) for the aspect";容器 "is returned if eBay has data on how many searches have been performed for listings in the category using this item aspect" [V-fetch + V-index]。

**`aspectApplicableTo` 的官方句** [V-fetch `pbse_product_vs_item_aspects.html`]:返回 `PRODUCT` 表示该 aspect 由 eBay 目录产品定义、**卖家改不了**;返回 `ITEM` 才是卖家可填的。同页另一句:"Only the item specifics that are returned in the responses of these two calls can be specified by the seller when listing/revising PBSE products. If a seller tries to pass in one or more product aspects…**those product aspects will either be ignored or an error may occur**."
⚠ **两种 aspects 载荷形状别写混** [V-index `slr:Product`]:`product.aspects` 是 **name → 字符串数组的 map**(`{"Brand": ["GoPro"]}`);而 `inventoryItemGroup.variesBy.specifications` 是 **`[{name, values[]}]` 的数组**。

### 4.5 `getCategorySuggestions`
完整描述(逐字)[V-fetch]:"This call returns an array of category tree leaf nodes in the specified category tree that are considered by eBay to most closely correspond to the query string `q`. Returned with each suggested node is a localized name for that category (based on the `Accept-Language` header specified for the call), and details about each of the category's ancestor nodes, extending from its immediate parent up to the root of the category tree … **Important: This call is not supported in the Sandbox environment. It will return a response payload in which the `categoryName` fields contain random or boilerplate text regardless of the query submitted.**" `q` 参数(逐字):"A quoted string that describes or characterizes **the item being offered for sale**."

`CategorySuggestionResponse` = `categorySuggestions` "Details about suggested categories matching provided keywords." / `categoryTreeId` / `categoryTreeVersion`。

| `CategorySuggestion` 字段 | 官方描述(逐字) |
|---|---|
| `category` | `Category`(`categoryId` + `categoryName`)— "Contains details about the suggested category." |
| `categoryTreeNodeAncestors` | `AncestorReference[]` — "An ordered list describing the location of the suggested category." |
| `categoryTreeNodeLevel` | integer — "The absolute level of the category tree node in its hierarchy." |
| 🔴 `relevancy` | string — **"Reserved for internal or future use."** |

`AncestorReference`:`categoryId` / `categoryName` / `categorySubtreeNodeHref` / `categoryTreeNodeLevel` —— **祖先链有序**,可直接拼出 eBay 侧完整类目路径文本。⚠ 另有一句 [V-index 同族方法页,**OAS3 描述里没有**]:"The array of suggested categories is sorted in order of eBay's confidence of the relevance of each category (the first category is the most relevant)"。

### 4.6 `fetchItemAspects` —— 禁用理由原文
描述(逐字)[V-fetch]:"This method returns a complete list of aspects for all of the leaf categories that belong to an eBay marketplace. The eBay marketplace is specified through the `category_tree_id` URI parameter." 成功响应是 "a **gzipped JSON file** sent as a **binary file** using the **content-type:application/octet-stream** in the response. **This file may be large (over 100 MB, compressed).**" "The open source Taxonomy SDK can be used to compare the aspect metadata that is returned in this response."

⚠ **spec 自相矛盾一处**:同一份 spec 的 `responses.200.content` 声明的是 `application/json`,而描述里写的是 `application/octet-stream` 的 gzip 二进制 ⇒ 笔记按**描述为准**(描述是人写的、更具体):该端点回包**不能丢给 `resp.json()`**。Taxonomy SDK 是**开源比对工具**(GitHub),职责是"比对两次 bulk 文件的 new/modified/removed" [V-fetch migration-taxonomy-sdk.html]。
