# 一店多仓(自发货多节点)改造计划

> 状态:**计划定稿待所有者批准,未动任何生产代码**(2026-08-24)。
> 范围:部分店铺自建**第二个自发货仓**(seller-fulfilled PHYSICAL 节点),
> 按店配置「上架到指定仓、维护指定仓」。**不涉及 WFS 仓的任何操作**(所有者定稿)。
> 调研来源:仓库侧四份摸底(API 层/上架链/维护链/文档,2026-08-24)+
> 官方文档两轮核验(分节点库存端点、shipnodes 与上架 FC,URL 见文末)。

## 0. 一句话模型

**一店仍只有一个受管仓**:未配置的店 = Virtual Node(现状,行为零变化);
配置了「维护仓库」的店 = 指定的 PHYSICAL 节点。自动链(上架/维护/清零)只碰
受管仓;店里其它节点(若有)一律不读不写。不需要多元素 inventory 数组、
不需要按仓分配策略、分配链不动(「货位」= 在架 SKU 条数,与仓库正交)。

## 1. 现状定稿(仓库侧摸底结论)

单仓假设是**从旧系统无意识继承的默认,从未被拍板**(本仓决策留痕密度极高而
此事零留痕;蓝图预留机制有 6 项,inventory 域一项都没有)。它渗在三层:

| 层 | 现状 | 位置 |
|---|---|---|
| 数据 | `walmart_items` 主键 `(store, sku)`,库存单列 `avail_qty`(**全节点合计**) | refdata/schema.sql:142,150 |
| API 写侧 | `put_inventory` 无 node;inventory feed 载荷 `{sku, qty}` 无 node | api/inventory.py:94-97;api/feeds.py:135-142 |
| 上架载荷 | `inventory` 数组恒单元素,`fulfillmentCenterID` 恒 = partnerId | services/mp_mapper.py:684-685;api/settings.py:30-31 |

读侧反而已消费多节点响应:`api/inventory.py:22-35` `_qty()` 对 `nodes[]` 求和
——节点身份在求和时丢失。**读合计、写单仓**在多节点店会产生:
stockzero 静默失效(P0)、库存永久重写循环 + settle 恒 ineffective(P1)、
线上库存被放大(P2)、`avail_qty` 单列混两种口径(P2,bulk 合计 vs
`get_inventory` 兜底的 legacy 单值)。生产已实证多节点存在:WFS eligible
删除失败 11 条(L001/A152/A154/A170)。

## 2. 官方端点形状(2026-08-24 核验定稿)

### 2.1 分节点库存(REST)

| 端点 | shipNode 位置 | 要点 |
|---|---|---|
| `GET /v3/inventories` | 仅响应 | `elements.inventories[].nodes[].shipNode`;数量为 `{unit, amount}` 对象:读 `availToSellQty`(另有 `inputQty`/`reservedQty`);分页 `limit≤50` + `meta.nextCursor`(不透明,原样回传);**无 shipNode 过滤参数**。配额 200/min(tsv:81) |
| `GET /v3/inventories/{sku}` | query(可选) | ⚠ 官方页的样例响应是 PUT 风格(`status:"Success"`),几乎肯定是文档错误——**响应结构需实测**。配额 200/min(tsv:79) |
| `PUT /v3/inventories/{sku}` | **body** | 写入主力。`{"inventories":{"nodes":[{"inputQty":{"unit":"EACH","amount":N},"shipNode":"..."}]}}`,一次可写多节点;**部分成功语义**:HTTP 200 后必须逐个解析 `nodes[].status`(失败项带 `errors[]`)。配额 200/min(tsv:80) |
| legacy `PUT /v3/inventory` | query(可选) | 不带时写"默认节点"——**默认节点官方无定义,多仓下禁止依赖**;配置店一律显式走上面那条 |

⚠ **数量字段名三套并存**,序列化器不能共用:读 `availToSellQty`,REST 写
`inputQty`,feed 写 `quantity`;全部 `{unit, amount}` 对象(与 MPItem 里
`inventory[].quantity` 必须是**裸 int** 又相反,mp_mapper.py:656,685)。

### 2.2 feed 通道

| feedType | 多节点 | 要点 |
|---|---|---|
| `inventory`(v1.4) | ❌ 单文件单节点 | 载荷无 shipNode 字段;"可省略→默认虚拟节点";**节点到底怎么指定是官方文档空白** |
| `MP_INVENTORY`(v1.5) | ✅ `shipNodes[]` per SKU | **官方已无 BETA 标记**(蓝图 §3.2 的"暂不用,盯 BETA 走向"可更新);JSON only;⚠ 1.5 的 key 小写(`inventoryHeader`)与 1.4 大写(`InventoryHeader`)相反,builder 必须两套模板 |

**本计划的取舍(所有者定稿 2026-08-24:「有指定仓库的,也需要接批量上架
和维护」)**:配置店两条路都接——小批量走 `PUT /v3/inventories/{sku}`
(单品同步,结果当场已知),大批量走 **MP_INVENTORY feed(v1.5)**;
`SYNC_THRESHOLDS` 分流语义与现状一致。未配置店维持 legacy 两条路不动。
批量**上架**不需要新通道:MP_ITEM feed 本来就是批量,批次 3 只把
`fulfillmentCenterID` 换成配置节点。

### 2.3 节点管理与上架

- `GET/POST /v3/settings/shipping/shipnodes`:节点唯一标识字段 **`shipNode`**
  (17-18 位数字),另有 `shipNodeName`/`status`/`nodeType`(PHYSICAL/3PL)。
  建仓可走 API 或 Seller Center(Shipping Profile → Seller Fulfillment →
  Add Fulfillment Center);**FC ID 在同页「FC ID」列可见**。
- MPItem `Orderable.inventory[].fulfillmentCenterID` 官方原文:有 FC 填
  **shipNode 值**;"没建过 FC 走 Virtual Node 时才用 Partner ID"。现系统
  恒填 partnerId 是**退化写法**,对建了实体仓的店铺是错的。单次 item setup
  最多 5 个 FC(我们只用 1 个:受管仓)。
- **运费模板是 (SKU × 模板 × FC) 三元组绑定**;新建 FC/模板后**须等 4 小时**
  才能挂 SKU,不等会静默回落默认模板 → 建仓与挂 SKU 必须拆两步(见 §7)。

### 2.4 必须实测的官方文档空白 ✅ 已实测(2026-08-30,谭总12,`node_probe`)

1. **Virtual Node 会出现在 `GET shipnodes` 列表里**(官方页面自述只覆盖
   physical/3PL,实测多了 `nodeType: "VIRTUAL"` 一档,shipNode = Partner ID
   10003247367)。校验口径不用改:`resolve_node` 按"填的值在不在列表里"判,
   Virtual 在列表里只是让"有人把 Partner ID 填进维护仓库列"也能通过 ——
   语义等价于没填,无害。
2. **`GET /v3/inventories/{sku}` 真实响应**(官方 PUT 风格样例确认是文档错误):
   `{"sku": ..., "nodes": [{"shipNode": ..., "availToSellQty": {unit, amount},
   "inputQty": {...}, "reservedQty": {...}}]}` —— 与 bulk 同族,
   `api/inventory._nodes()` 现有解析分支即命中,零改动。
3. **订单行带 `shipNode`**:`{"id": "10003247367", "name": "...",
   "type": "SellerFulfilled"}`(官方仅 3PL 样例带 id,实测自发货也带)——
   按仓对账**可以走订单接口**,不需要自建 SKU→FC 映射。
4. **节点行是「第一次写库存」时创建的,不需要任何前置关联**(2026-08-30
   二次实测定案,谭总12 B008LUW4CI):`PUT /v3/inventories/{sku}` 带
   `shipNode` 写入 → 响应 `{"shipNode": "1011159562695573505",
   "status": "Success"}` → 读回立刻变成 `{中山仓: 5, Virtual: 0}`。
   建仓当天 shipnodes 已在册但库存响应里没有它,**只是因为还没往它写过货**。
   ⚠ **本条推翻了同日早些时候的一次推断**("门槛是 Seller Center 的
   SKU×FC 关联/Assign SKUs")—— 那是按官方运费模板文档推的,实测证伪。
   运费模板绑在仓上,自动链只需要写对节点。
   ⚠ 写后读有**秒级延迟**:紧接着读可能还是旧值(首次实测就撞上了),
   别拿"立刻读不到"当写失败 —— 判成败看响应的 `nodes[].status`。

另:同日实证透明游标是一次性的 —— 翻页中途断连后重试同 cursor 即 400,
`list_inventory_nodes` 已加 items 域同款"整轮重来一次"自愈。

## 3. 配置定义(所有者方案)

**上下架限额表(RETIRE_LIMITS)新增一列「维护仓库」**,与「配送限制」同款
治理:所有者建列填值、registry 登记字段常量(`maint_node="维护仓库"`)、
代码直读、**没填 = Virtual Node(现状)**。

- **填什么**:Seller Center → Shipping Profile → Seller Fulfillment 的
  **FC ID**(即 shipNode,17-18 位数字)。
- **校验**(fail-closed):读到非空值 → 调 `GET shipnodes` 比对;对不上 →
  该店维护/上架**整店跳过并告警**(填错了宁可不动,不能静默回落 Virtual
  Node——那会把新仓的货写到旧节点)。校验结果缓存一天(节点不会天天变)。

## 4. 批次 0|止血 + 探测 ✅ 已完成(2026-08-24)

1. ✅ 单品兜底换端点 `GET /v3/inventory?sku=` → `GET /v3/inventories/{sku}`
   ——读侧口径统一为"全节点合计",消掉 `avail_qty` 单列双语义。
   `_nodes()` 保留节点身份(`{shipNode: 数量}`),`_qty()` 变成它的求和;
   **认不出的形状返回 None 并告警,绝不当 0**(官方那份 PUT 风格的错误样例真
   撞上时,当 0 会让维护链把 amz 库存整店重推)。
2. ✅ `walmart_items.node_count` + `catalog_sync` 摘要告警行(现状恒 1/0,
   价值全在"什么时候不再是")。合计与节点数**同源**于 merge_rows 的一份入参。
3. ✅ 问题链 WFS 闸:按**最近一次**删除回执是否 `ERR_EXT_DATA_0101218` 拦
   (不是历史命中过就永久拉黑——转出 WFS 后要能自己放出来);**只拦破坏动作**,
   反补(MP_MAINTENANCE)照常;顽固件双发同样拦(RETIRE 对 WFS 件行不行官方
   无明文,不按推断编码);摘要单列报数。
4. ✅ 蓝图补账:#22 换端点、分节点写与 shipnodes 进预留、MP_INVENTORY 去 BETA
   标记并注明批次 2 实现、**单仓假设写成有意识的决策**(附失效时点与去向)。

⚠ 批次 0 **没有**改写侧:`put_inventory` 仍是 legacy 单仓。多节点店的四条
故障(§1)在配置「维护仓库」并跑完批次 2 之前依然存在 —— 探测告警只是让它
不再静默。

## 5. 批次 1|配置 + 读侧(含实测清单落账)✅ 代码完成(2026-08-24)

- registry:`RETIRE_LIMITS` 加 `maint_node` 字段常量;`store_limits.maint_nodes()
  -> dict[str, str]`(店铺 → FC ID,未填不出现)。
- `api/settings.list_ship_nodes(store)`(GET shipnodes,lru_cache 同
  `get_partner_id` 成例);配额进 `_client.py` 桶表(50/min,tsv:152)。
- 新表 `catalog.item_node_inventory (store, sku, ship_node, avail_qty,
  seen_at, PRIMARY KEY (store, sku, ship_node))`;`catalog_sync` 停止只求和:
  合计仍写 `walmart_items.avail_qty`(全部现有消费方零改动),节点明细同轮
  落新表。
- ✅ §2.4 四条实测已完成(2026-08-30,`node_probe` 工作流),结果见上。

## 6. 批次 2|维护链切受管仓(核心)✅ 代码完成(2026-08-24)

一个取数积木统一口径:`受管仓现值(store, sku) = 配置店 → item_node_inventory
里该 shipNode 的 avail_qty;未配置店 → walmart_items.avail_qty(现状)`。

1. 三个 provider 的比对基准换成受管仓现值:`inventory_intents`
   (maintenance_intents.py:660)、`zero_intents`(:538)、
   `match_inventory_intents`(:569)。
2. 意图契约与 `_DETAIL_KEYS`(:1003-1004)加 `ship_node` 键(未配置店不带,
   建议行与现状逐字节一致)。
3. 写通道两条(配置店;`SYNC_THRESHOLDS` 分流语义与现状一致):
   - 小批量:`put_inventory(store, sku, qty, ship_node=None)`——带 node 走
     `PUT /v3/inventories/{sku}`(body nodes、`inputQty`),**逐节点解析
     `nodes[].status`**;不带走 legacy(现状)。
   - 大批量:**MP_INVENTORY feed(v1.5)**接入 `api/feeds.build_payload`
     (小写 key `inventoryHeader`/`inventory`,每 SKU `shipNodes[]`,本仓
     恒单元素 = 受管仓;数量字段名 `quantity`)。切片按官方 1MB 上限保守配
     (字节封顶 + 条数封顶),令牌桶按 50/hour 进 `_client.py`;防重/回执
     走 ops.feed_log/feed_items 既有机械,feed_poll 零改动。
     ⚠ MP_INVENTORY 现在**哪儿都没登记**:`build_payload` 落到末尾的 catch-all
     `raise`(api/feeds.py:143),`FEED_SPEC_VERSIONS` 没有版本串、`_SLICE_LIMITS`
     没有切片、`_RATE_BUCKETS` 没有桶。两处测试钉着现状:`tests/test_feeds.py:106-107`
     (raise)与 :367-371(桶未登记必抛 KeyError)——批次 2 四处一并补齐并翻案。
   未配置店两条路径原样不动(legacy PUT + inventory feed v1.4)。
4. `settle_maintenance`(dispositions.py:644;判据 `maint_effective`:625)对带 `ship_node` 的建议行
   按 `item_node_inventory` 判生效;未配置店按 `avail_qty`(现状)。
5. stockzero:配置店清**受管仓**(运营语义=停售自发货;其它节点本就不归
   自动链管)。
6. 摘要新增一行:「受管仓=<FC ID> 的店 N 家;校验失败跳过 M 家」——
   配置生效与否必须天天见人。

## 7. 批次 3|上架链(最薄)+ 运维 runbook ✅ **生产验证通过(2026-08-30)**

> 实证:谭总12 新上架 B0HCNJTHM8 → `GET /v3/inventories/{sku}` 回
> `{"1011159562695573505": 20, "10003247367": 0}` —— 首铺库存 20 件直接进
> 受管仓(中山),`fulfillmentCenterID` 生效,**新品无需任何人工动作**。
> 附带观察:沃尔玛同时给 Virtual 建了一行 `0` —— 与本仓"缺节点行 = 那个仓
> 没货 = 判 0"的口径一致(见 §6 `current_qty`)。

代码:`list_new` 预取 `partners` 处改调 `store_limits.managed_nodes()` +
`listing_fc()`;`build_orderable` 的 `fulfillmentCenterID` = 配置店 FC ID /
未配置店 partnerId(数组仍单元素)。跟卖链零改动(v4.2 feed 本就无库存段)。
**校验失败的店整店跳过、不回落 Virtual Node** —— 与维护链同一条口径。

**Runbook(人工步骤,顺序是硬约束)**:
1. 建仓(Seller Center 或 API)→ 记下 FC ID;
2. 配运费模板并做 (SKU×模板×FC) 映射 → **等 4 小时**;
3. 限额表「维护仓库」填 FC ID;
4. `python cli.py maintenance_scan -p preview=1` 确认该店意图带 ship_node、
   校验通过;
5. 之后新上架自动进指定仓;
6. **存量 SKU 自动接管,无需人工搬仓**(§2.4 第 4 条实测定案):维护链
   下一轮就会把受管仓的库存写起来(缺节点行判 0 → 有差异 → 写)。
   ⚠ 先跑 `maintenance_scan -p preview=1 -p store=<店>` 看这一批的规模,
   再决定放不放行;并**先量一次旧节点的余量**(见 §6 末段:两节点同时
   有货 = 线上可售翻倍),决定是让它自然卖完还是人工清零。

### 落地实现速查(批次 1-3,2026-08-24)

| 关注点 | 唯一出处 | 说明 |
|---|---|---|
| 读表(不校验) | `store_limits.maint_nodes()` | 一个飞书请求拿全店 |
| 逐店校验(fail-closed) | `store_limits.resolve_node()` | 认不出抛 `NodeConfigError` |
| 读 + 校验 + 汇总 | `store_limits.managed_nodes()` | 返回 `(已生效, {跳过: 原因})` |
| 摘要那一行 | `store_limits.managed_note()` | 维护扫描 / 上架都摊这一行 |
| 上架仓 FC ID | `store_limits.listing_fc()` | 配置店 FC ID,否则 Partner ID |
| 维护比对基准 | `maintenance_intents.current_qty()` | 配置店取节点值,**明细没扫到返回 None 跳过** |
| 意图带节点 | `maintenance_intents._node_of()` + `_DETAIL_KEYS` | 未配置店不带这个键 |
| 小批量写 | `api.inventory.put_inventory(..., ship_node)` | 带节点走 `PUT /v3/inventories/{sku}`,**逐节点解析 status** |
| 大批量写 | `api.feeds.build_payload("MP_INVENTORY", …)` | v1.5 小写 key,每 SKU `shipNodes[]` |
| 落定判据 | `dispositions.settle_maintenance()` | 带 `ship_node` 的行按 `item_node_inventory` 判 |

三处**故意的响亮失败**(都不回落):FC ID 认不出 → 整店跳过;受管仓明细本轮
没扫到 → 该行跳过并计数;同批混着带/不带节点 → 本店不提交。回落的共同后果
是"写到官方无定义的默认节点且全程不报错",比少动一轮坏得多。

**存量行路由(所有者拍板 2026-08-30)**:配置店 **只维护受管仓**,不做
"按 SKU 所在仓路由"。§2.4 第 4 条实测定案后,这条**不再需要任何人工搬仓
前置** —— 受管仓缺节点行 = 那个仓里没货 = 现值 0,维护链照常算差异、照常
写,第一笔写入自动把节点行建出来。

- `current_qty` 对配置店把「缺节点行」判 **0**,不是 None。此前判 None
  (跳过)造成**死锁**:永远不写 → 永远没有行 → 永远跳过,3663 条存量行
  一直卡着而日志只说"预期暂停"。判 0 与"不回落合计"不冲突:回落是拿别的
  节点的货冒充受管仓现值,判 0 是如实说"这个仓里没有"。
- "整店还没扫到"由**缺席避让**(`store_absence`)兜,不由这条判据兼职。
- ⚠ **旧节点的存量货不会被自动清**:自动链只碰受管仓。于是接管后的一段
  时间里,同一 SKU 可能**两个节点都有货、线上可售量是两者之和**。
  谭总12 实测规模(2026-08-30):旧节点 3638/3680 个 SKU 有货、合计
  129,876 件(大量 999 的名义值)。

**搬仓收尾的顺序(所有者拍板 2026-08-30:先充受管仓,再清旧节点)**:
  1. `maintenance_scan -p preview=1 -p store=<店>` 看规模 → 真跑 maintenance,
     受管仓拿到正确库存(全程无断售空窗);
  2. `catalog_sync -p store=<店>` 复核受管仓已经有货;
  3. `node_clear -p store=<店> -p node=<旧节点> --dry-run` → 人眼确认 → 真跑。
  中间那段两节点都有货、可售量翻倍,是**知情接受**的代价(999 多为名义值,
  实际敞口小);反过来先清旧仓则会出现整店短暂不可售。
  ⚠ `node_clear` **拒绝清受管仓**:那是自动链每轮在维护的节点,清了下一轮
  就写回来 —— 两条规则互相拆台而且看不出是谁在跟谁较劲。停售整店走
  stockzero,换仓先改「维护仓库」再清旧仓。

## 8. 明确不做

- 不动 WFS(读写都不碰;WFS 库存归沃尔玛管)。
- 不自动建仓、不自动做运费模板映射(人工 runbook)。
- 不依赖 legacy 端点的"默认节点"语义(官方无定义)。
- 不做存量 SKU 搬仓、不做一品多仓分配。

## 9. 核验来源(2026-08-24)

- developer.walmart.com/us-marketplace:inventory-api-overview /
  return-your-entire-item-list / get-item-inventory-at-one-ship-node /
  update-item-inventory-per-ship-node / get-item-count-for-an-item /
  bulk-inventory / bulk-inventory-update-feed / get-all-fulfillment-centers /
  create-fulfillment-center / item-setup-schema-key-points
- developer.walmart.com/global-marketplace:post-item-setup-by-product-type-
  feed-file-structure-overview(fulfillmentCenterID 定义、"up to five" 原文)
- marketplacelearn.walmart.com:add-a-seller-managed-fulfillment-center /
  Shipping-templates: Assign SKUs(三元组映射、4 小时等待期原文)
