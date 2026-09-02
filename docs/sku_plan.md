# SKU 编码规则:货源隐匿 + 多源共存 —— 影响范围全景与整体计划

> 状态:**计划待所有者批准,未动生产代码**。
> 2026-09-01 初稿;09-02 按所有者改稿(去店铺前缀、加来源字母);09-02 晚
> 按所有者四问做全仓二次调研(四路并行:数据流全景 / 上架链与生成时点 /
> 订单财务飞书侧 / 仓内沃尔玛硬约束),本版为整体计划;09-02 深夜按所有者
> 三条批复改稿(多店多码可处理 / 波及面一次做完 / **存量产品迁到新码**,官方
> 支持改 SKU,见 §4);09-02 生命周期工作流(官方/仓内/社区三路 → 三方案评审 →
> 对抗验证)定稿 §5.3 四个弃码点与复用规则。
> **执行工作包**(逐批次文件级改动/测试/验收/DDL/决策/风险)见 `docs/sku_workplan.md`
> 及 `docs/sku_workplan/`(2026-09-02 立项,Fable 规划调度、Opus 5 执行)。
> 所有者定稿:「沃尔玛侧通过 SKU 倒查产品来源,我不想让沃尔玛知道我的货源
> 是哪里来的」+ 多源共存(amz / 1688 / 自建)+「前缀不要店铺,我需要让我们
> 内部可以看出来这个产品是怎么来的」。

## 0. 一句话模型

**SKU = 12 位连写:`<1 位来源字母><11 位随机不透明码>`;身份唯一出处 =
`catalog.listing_sources`;在 list_new 预备期抽码登记,提交前已落库;**码的寿命 =
沃尔玛侧那条 (店, SKU) 记录对我们还有用的寿命**——同一 (店, 来源类型, 来源码)
的码复用到显式弃码为止,弃码只在四个点发生(§5)。**

对沃尔玛:一串 12 位随机串,看不出 ASIN、看不出上架日期。对内部:首字母一眼
看出来源(amz / 跟卖 / 1688 / 自建),细节靠登记簿反查。

## 1. 所有者四问的答案(先给结论,依据在后面各节)

**问 1|SKU 什么时候生成:入库还是上架?** → **上架时**,具体是 `list_new`
预备期(`_prep_rows`,所有闸门与配额切片之后、组载荷之前),跟卖对应
`match_listing` 的逐行循环。理由只有一条硬的:**本仓的"入库"(`product_ingest`
→ `catalog.products`)那一刻没有店铺**,而沃尔玛 SKU 是按 seller 唯一的对象,
`(店, 品)` 维度在入库时根本不存在。硬要在入库抽码,要么全店共用一个码(跨店
同码 = 关联信号,与本计划目标冲突),要么给 `catalog.products` 加回 2026-08-12
刚退役的 store 类列,并给几十万个永不上架的空壳行发码。详见 §5。
**"多店多码指向同一 ASIN,系统能处理吗?"——能,而且现在就是这样。**
`walmart_items` / `listing_sources` 主键都是 (store, sku),一个 ASIN 在库里本来
就可以有多行;存量 sku=asin 时同一 ASIN 在两家店是**同一个 SKU 串**,改码后
变成两个不同串,差别只在"从 SKU 认 ASIN"这一步——由登记簿按 (store, sku) 反查
承担。产品级归并(事件视图 `coalesce(asin, sku)`、黑名单键、销量维度、分配的
"已在架"集合)全部按 ASIN 归并,不看 SKU 串。需要注意的只有两条:两条清洗
工作流的 `_FILL_SQL` 目前只按 sku 不带 store,换登记簿反查后必须带 store;
规划内店的"一 ASIN 一店"是 `claims` 占用闸压出来的业务规则,与编码无关。

**问 2|波及面有多广?** → 全仓穷举后:**没有一处会报错,全部是静默失效**。
SQL 硬等号 5 处 + 1 个视图;按 SKU 形态倒推 ASIN 的调用点 **14 处**(初稿写 7
处,漏了一半);上架链里以 ASIN 当 SKU 对账的点 **9 处**(去重闸、重试上限、
回执找行、Unknown 自愈、UPC 撞库标记、SKU_LOCKED 退役、UPC 池写入……);飞书
表 6 张受影响。全景表在 §3。最危险的三条:本店去重闸失效(同店重复上架,烧
UPC 烧配额)、黑名单键被灌随机码(违禁品拦不住)、订单审核把每一单判"待人工"。

**问 3|存量产品过渡期怎么办?改了在线产品的 SKU,旧订单会不会跟着变?**
→ 分三句(2026-09-02 所有者拍板:**存量产品要迁到新码**;官方查证结果见 §4):
- **在线产品的 SKU 可以改,官方支持。** Seller Center 单品编辑或批量模板
  「SKU Update = Yes」,按 Product ID(UPC/GTIN)匹配现有 item,评价与评分保留;
  API 侧对应 feed 里的 `SkuUpdate` 属性(CA 文档明文,US 文档指向"Maintain an
  item / Bulk create/update items")。WFS 品不能改(本仓 WFS 一律删除,无影响)。
  迁移作为**批次 3**,机制与代价见 §7;做之前必须单品实测三件事(§4)。
- **旧订单不会变。** `orders.order_lines.sku` 是下单当时沃尔玛返回的快照,
  行标识 = sha256(PO + SKU),全仓没有任何一条 SQL 会改历史订单行的 sku;
  asin 列只在原来为空时补填,永不覆盖。改码后新订单带新码、旧订单带旧码,
  登记簿里旧行标 `replaced_by`,两个码 `resolve` 到同一个 ASIN,销量不断档。
- **飞书订单表加 ASIN 列:对。** 销售订单表、售后订单表现在只有「SKU」列;
  加「ASIN」列从 `order_lines.asin` 投影(登记簿反查后新旧码都有值)。一次性
  代价:加列 ⇒ 行指纹全变 ⇒ 下一次 push 把 90 天窗口全量重推一遍,预告不是
  故障。

**问 4|上架表要加 SKU 列。** → 对。**所有者 2026-09-02 已重排表头,SKU 在 C 列**
(21 列:店铺 / ASIN / SKU / walmart上架标题 / walmart_product_type / 审核结果 / 类别 /
具体内容 / 审核日期 / amz价格 / 库存 / walmart价格 / 是否上架 / 上架feedid / 上架日期 /
未上架理由 / 上架结果 / 报错 / feed查询日期 / 登记日期 / 查询编码)。旧「理由」拆成
「类别」(37 政策类目)+「具体内容」(人话);旧尾部四列删除;「登记日期 / 查询编码」
是所有者新列,程序不读不写。`listing_sheet` 的所有写入 range 从写死字母改为按
`columns` 元组算列字母,此后再挪列只改元组。提交时与 是否上架/feedid/上架日期 同一次
写回 SKU;回执反哺、Unknown 自愈、SKU_LOCKED 退役从此读 C 列(C 为空的存量行回落
B 列 ASIN)。同理:在线产品总表 **Q 列「来源码」**、销售/售后订单表**「来源码」**
(已建)、UPC 池表 E 列「SKU」改存真 SKU、退役表 B 列运营手填 SKU 从此要先查登记簿。

## 2. 编码规则(2026-09-02 定稿,未变)

```
<来源字母><11 位随机码>      共 12 位,无分隔符
AK7QM2X9RT4W                 A = amz(映射只在 registry)
```

- **来源字母**(第 1 位):registry 常量表 `SKU_SOURCE_LETTERS`,**所有者定稿
  2026-09-02**:`{"amz": "A", "match": "B", "1688": "C", "self": "H"}`。工作流按
  自己的 `source_type` 查表,没人手填。不用分隔符:`A-K7QM…` 会把"前面有个分类
  段"写在脸上。注:跟卖码以 B 开头、12 位,与 ASIN 的 `B0` + 10 位形态不冲突
  (`extract_asin` / `sources_backfill` 的正则都锚定 10 位)。
- **随机码**(后 11 位):`secrets.choice`(操作系统密码学随机源)从字母表
  `23456789ABCDEFGHJKMNPQRSTVWXYZ`(30 符号,剔除 0/O、1/I/L、U)逐位独立抽
  11 次。不含时间戳/序号/机器号,没有任何可被学习的生成规律。
- **重复**:空间 30^11 ≈ 1.77×10^16;累计 100 万个码出现过任何一次撞码的总
  概率约 3×10^-5。但**不靠概率**:`mint` INSERT 前先全局(不分店)查重,撞了
  重抽,5 次仍撞抛错(随机源坏了,不是运气)。全局唯一而非按店唯一:两家店
  同一 SKU 串在沃尔玛合法,但那正是"两家店有关联"的信号。
- **12 位不是 10 位**:10 位正是 ASIN 长度;12 位与全部存量形态都对不上,
  `extract_asin` 必返 None,调用方于是走登记簿——形态本身就是分流器。
- 沃尔玛约束:按 seller 唯一、**可改**(官方「SKU Update」,§4);长度上限与
  字符集待本地 spec 核(§8)。本规则 12 位纯大写字母数字,任何合理上限都在内。

## 3. 影响范围全景(2026-09-02 四路调研合并)

### 3.1 总判断

全仓**没有一处会抛异常**;全部是"不报错、摘要看起来正常、功能悄悄没了"。
这是最危险的形态,也是为什么必须先做读侧收口(批次 0)、写侧最后切。

### 3.2 SQL 硬等号(sku 与 asin 直接比)—— 6 处,必改

| 位置 | 现状 | 失效后果 | 改法 |
|---|---|---|---|
| `services/maintenance_intents.py:192` | `p.asin = w.sku`(amz 三 provider 共用取数) | **维护链对新品永久失明**:不改价、不清零 | 该 SQL 已 JOIN `listing_sources ls`,右边换 `ls.source_key` |
| `services/maintenance_intents.py:202` | `l.asin = w.sku`(latest_snapshot) | 同上 | 同上 |
| `services/maintenance_intents.py:233` | `w.sku = vo.asin`(变体偏移删除) | 永久偏移的品删不掉 | 经 `listing_sources` 反查 (store, sku) |
| `services/maintenance_intents.py:322` | `o.asin = live.sku`(连续缺货删除) | 长期缺货删除失明 | `live` CTE 带出 source_key |
| `workflows/product_audit.py:411` | `w.sku = p.asin`(mode=online 候选) | 在架 pass 复审候选恒空 | EXISTS 改经 `listing_sources` |
| `refdata/schema.sql:527` 视图 `audit_listing_conflicts` | `p.asin = w.sku` | `problem_scan` 的审核来源建议归零 | 视图改 JOIN 登记簿(同步 `tests/test_problem_scan.py:301`) |

范本:`maintenance_intents.py:649-654` `_SQL_MATCH_INV`(跟卖 provider)已经是
正确写法。

### 3.3 按 SKU 形态倒推 ASIN —— 15 处(初稿只列 7 处;第 15 处 `_LATEST_CTE` 是 0b 执行时补的)

| 调用点 | 后果 | 改法 |
|---|---|---|
| `services/order_audit.py:358-361` `judge`(**直接正则 `^B[0-9A-Z]{9}$`,不调 extract_asin,最容易漏**) | **新品每一单判"待人工",订单审核链事实停摆** | 收口成 `services/order_audit.line_asin`(asin 列优先、形态兜底),judge 与工作流四处共用【0b 已闭合】|
| `workflows/order_audit.py:423/461-462/479/1239` | 同上,采集推不出去、钓鱼波及不展开 | 同上(含 `_phish_record`)【0b 已闭合】|
| `services/blacklist.py:99` `extract_asin(sku) or sku` | **黑名单键被灌随机码 ⇒ list_new 的黑名单闸拦不住** | 键取 `listing_sources.source_key`;`or sku` 原文兜底保留但加日志计数(D-0b-1)【0b 已闭合】|
| `services/blacklist.py:157` | 品牌收集 0 命中,每轮空转 | 同上【0b 已闭合】|
| `services/blacklist.py:205-215` `_LATEST_CTE`(回填/重建侧取键) | ASIN 黑名单被整表重灌成随机码键,拦不住任何东西 | 经登记簿 LEFT JOIN 取 `coalesce(ls.source_key, e.asin, e.sku)`【0b 已闭合,见工作包 0b-14】|
| `services/order_lines.py:169` | `order_lines.asin` 恒 NULL ⇒ 产品分退出销量/退货率维度 | 落库当场由 `upsert_order_lines._fill_asins` 每批一条 SELECT 经登记簿补【0b 已闭合】|
| `services/product_events.py:167` | 事件身份退化成随机码,同产品跨店/重上不归并 | 同上;store 为空的平台级事件保持形态提取(D-0b-7)【0b 已闭合】|
| `services/audit_rules.py:176-181` | 实证 PT 对新品失明 | JOIN 登记簿 |
| `services/alloc_survey.py:291 / 796` | 全落 `no_asin`,冲突判定/品牌占用失明 | `_SQL_ONLINE` LEFT JOIN 登记簿直接取 source_key |
| `workflows/alloc_push.py:72`、`alloc_plan.py:127`、`alloc_products.py:101` | **"已在架"集合恒空 ⇒ 已上架的品被重新派工、重复上架** | 同上 |
| `services/feed_track.py:179-190` | 违禁回执反哺黑名单写错键 | 传 source_key(键的推导在 blacklist 侧)【0b 已闭合】|
| `workflows/product_refresh.py:58/89` `_ASIN_RE` | **推采集目标静默归零 ⇒ 维护链新鲜度源头断** | 改查登记簿 amz 行的 source_key |
| `workflows/sources_backfill.py:46/66/90` | 新 SKU 全判 unknown;"非零即报警"语义作废 | 摘要分三桶:amz / 旧格式存量 / 新码漏登记(后者才报警,不透明码判据调 `sku_codec.is_opaque`)【0b 已闭合】|
| `workflows/sku_normalize.py` / `order_asin_normalize.py` | 变空转,"可解析 0 个"只增不减 | 两条共用 `sku_asin.resolve_pairs`(带 store,倒查两级);`_DISTINCT_SQL`/`_FILL_SQL` 加 store 维度【0b 已闭合】|

不改的(语义是"过滤非标准码",新码天然不是 ASIN,行为恰好正确):
`order_history_import.py:167`(只导旧数据)、`pt_backfill.py:96`(旧库)、
`brand_scrape.py:91`(输入来自 products.asin)、`asin_blacklist_import.py:57`
(校验导入值)、`product_query.py:47`(判用户输入)。

### 3.4 上架链里以 ASIN 当 SKU 对账 —— 9 处(初稿完全没列)

| 位置 | 角色 | 失效后果 | 状态 |
|---|---|---|---|
| `workflows/list_new.py:304-306` + `:1227` `_SQL_LISTED_ASINS` | **本店去重闸** | **同店同 ASIN 反复上架,烧 UPC 烧 MP_ITEM 配额,不报错** | 批次 2 |
| `workflows/list_new.py:662-669` + `:695` `_SQL_ATTEMPTS` | FAILED 重试上限 3 次 | 每次新码 count 恒 0 ⇒ 无限重试(→ 这是"码复用到退役"的理由之一,§5) | 批次 2 |
| `workflows/list_new.py:705-731` `_FAMILY_LISTED_SQL` | 变体组查同族已在架 | 变体组决策退化 | 批次 2 |
| `services/listing_sheet.sync_from_ledger` 台账三个 dict | 回执找回行 | 回执三列永不回填 | ✅ 批次 1:改 `row_sku` |
| `services/listing_sheet.heal_unknown` | 是否上架=Unknown 自愈 | 行永久卡 Unknown,UPC 永久占用 | ✅ 批次 1:台账/目录按 `row_sku`,UPC 池按 (店, ASIN),键已拆开 |
| `services/listing_sheet._mark_upc_conflicts` | UPC 撞库标记 | 撞库的号永不标 conflict,反复领到坏号 | ✅ 批次 1:反查键改 **(店铺, ASIN)**(本批唯一有意的差异) |
| `workflows/sku_locked_heal.py` 五个 (店, SKU) 键 | RETIRE 用 `r["asin"]` 当 SKU | **退役发的是 ASIN,退不到/退错** | ✅ 批次 1:五处同源走 `row_sku`;烧号键取自冷却表 |
| `workflows/list_new.py` + `listing_sheet.heal_unknown` 的 `mark_used` | UPC 池 `sku` 列 | 列名叫 SKU 实际存 ASIN(现状已如此,切换后必须定口径) | ✅ 批次 1:改传 `row_sku`(批次 1 值仍是 ASIN) |
| `workflows/list_new.py:603/608/1053/1057` | 载荷 `sku=r["asin"]`(真跑 + check_spec 预检两条路) | 这是原点,两条路必须一起改 | 批次 2 |

### 3.5 飞书表

| 表 | 现状 | 要做的 |
|---|---|---|
| 上架表 `LISTING_SHEET`(21 列 A~U) | 无 SKU 列;B 列 ASIN 兼作 SKU 全链对账 | ✅ **批次 1 已实现**:`columns` 按新序重排 + 新增 `headers`(字段→中文表头);写入 range 改为**按表头名**经 `layout()` 算(源码零硬编码字母,fail-closed);新增 `row_sku` / `write_sku_col` / `write_submit_cols` 可选第 9 值。**本批只加列不写值**(写入随批次 2 通电) |
| 订单中心-销售订单 `ORDER_SALES` | 有「SKU」无来源码 | **「来源码」已建**:registry 常量(值 `order_lines.asin`)+ `_SALES_SQL` + 投影 + 测试夹具 |
| 订单中心-售后订单 `ORDER_RETURNS` | 有「SKU」无来源码;`return_lines` 表无 asin 列 | **「来源码」已建**,SQL 已 LEFT JOIN order_lines,顺手 `SELECT l.asin` |
| 在线产品总表 `ONLINE_PRODUCTS_SHEET` | 有 sku 无来源码 | **Q 列「来源码」已建**(第 17 列,登记簿 JOIN `source_key`),反向可对 |
| UPC 池表 `UPC_SHEET` E 列「SKU」 | 实存 ASIN | 定口径:E 列存真 SKU,ASIN 另列或不投影(§8) |
| 退役表 `RETIRE_SHEET` B 列 | 运营手填 SKU | 手动通道全格式通吃不用改;但运营从"贴 ASIN"变"先查登记簿",建议读表后回显来源码 |
| 维护记录表 `MAINT_SHEET` | 逐 SKU | 不改;建议加来源码展示列 |
| 绩效/对账/主订单表 | 无 sku 无 asin | 不动 |

### 3.6 表与工作流受影响程度

| 程度 | 表 | 工作流 |
|---|---|---|
| **高** | listing_sources(锚点)、walmart_items、product_events + 4 视图、asin_blacklist、upc_pool | list_new、maintenance_scan/maintenance、sku_locked_heal、feed_poll(回执/自愈)、sources_backfill、product_refresh、product_audit(online)、problem_scan(audit 来源)、blacklist_push/brand_scrape、alloc_push/plan/products/backfill、order_audit |
| **中** | order_lines(asin 列)、dispositions(asin 列)、cleanup_seen_categories、ops.dedupe、claims | sku_normalize、order_asin_normalize、alloc_audit、claim_audit、risk_sync |
| **低/无** | feed_items、return_lines、perf_events、settlement_lines、item_node_inventory、retire_cooldown | catalog_sync、daily_report、settlement_sync、returns_sync、perf_problems、order_sync、order_center_push、match_listing、product_clear、node_* |

### 3.7 会失效的测试(改造时一并处理,不列全)

钉住"按形态可解析"的:`test_sku_asin.py`、`test_sources_backfill.py:42`、
`test_product_ingest.py:603`、`test_order_audit.py:1334`、`test_alloc_audit.py:91-105`、
`test_order_asin_normalize.py`(含守门测试 `test_rules_are_not_reimplemented_here`
——登记簿那一跳必须放在 `services/sku_asin`,不能放工作流)、`test_blacklist.py:116/177`。
钉住 SQL 文本的:`test_problem_scan.py:301`、`test_blacklist_push.py:164`、
`test_risk_trace.py:123`。夹具里 sku=asin 同值的:`test_list_new.py:570/689`、
`test_sku_locked_heal.py`、`test_claims.py:372`、`test_alloc_plan.py:122`。

## 4. 沃尔玛侧硬约束(仓内证据 + 官方查证 2026-09-02)

**SKU 可以改(官方,推翻初稿"建后不可改")**:
- Seller Center 批量:`Catalog → Add items → Upload in bulk → 全量 item setup
  模板`,填新 SKU + 其余必填字段,Optional 段「SKU Update」选 Yes,上传;
  15 分钟至 4 小时生效。"You can't change SKUs for items fulfilled through WFS."
  "To update a SKU using API, refer to the steps listed under Maintain an item."
  ([Update SKUs in bulk in Seller Center](https://marketplacelearn.walmart.com/guides/Catalog%20management/Item%20management/Update-SKUs-in-bulk-in-Seller-Center))
- 匹配键 = Product ID:"Enter the correct SKU for that Product ID. Enter Yes in
  the SKU Update column … The item will retain all of its ratings and reviews."
  "You are not allowed to submit two SKUs with the same Product Identifier."
  ([Update an item's SKU](https://marketplacelearn.walmart.com/ca/guides/Catalog%20management/Item%20management/update-an-item-s-sku))
- API:"look for the SkuUpdate attribute in the payload and set it to Yes …
  provide the new SKU … when the feed is successfully processed, the item will
  have the new SKU."([Manage items, CA](https://developer.walmart.com/ca-marketplace/docs/manage-items))
  US 侧 [Update my existing items](https://developer.walmart.com/us-marketplace/docs/update-my-existing-items)
  只讲 MP_MAINTENANCE 做部分更新("requires only the SKU and GTIN attributes"),
  未点名 SkuUpdate。
- 第三方实操一致([GeekSeller](https://support.geekseller.com/knowledgebase/how-to-change-sku-on-walmart-seller-center/)、
  [Zentail](https://help.zentail.com/en/articles/1118297-walmart-product-id-or-sku-update)):
  按 Product ID 找 item;「SKU Update」与「Product ID Update」互斥;同 SKU 换
  UPC 报 "This SKU is already set up with a different Product ID";处理要几小时。

**待单品实测的三件事**(官方文档没写,本仓纪律"不按推断编码"):
1. `SkuUpdate` 在本地 spec 的哪份里:`grep -rl SkuUpdate <DATA_ROOT>/specs/MP_ITEM/5.0.20260608-18_15_07-api/`
   (MP_MAINTENANCE 与 MP_ITEM 同版同布局)。若 MP_MAINTENANCE 收 `{sku 新码,
   GTIN 现号, SkuUpdate: Yes}` 最小载荷就能改 ⇒ **不用重发内容**;若只有 MP_ITEM
   全量载荷才行 ⇒ 改码 = 重发全部内容(标题/属性会被我们再生成的内容覆盖,
   这是副作用,要所有者接受)。
2. 改码后库存、价格、item_id/wpid、变体组是否原样保留(`node_probe` + `GET
   /v3/items/{新sku}` 前后对比)。
3. 旧 SKU 串改码后能否再次使用(不打算复用,只为知道撞库风险)。

**下架后 SKU 的状态(官方查证 2026-09-02,§5 生命周期的依据)**:
- RETIRE(停用):SKU **保留**。item 留在目录,lifecycle=RETIRED,内容/历史/评论
  保留,本质是 end date 置过去;"To unretire an item, change the end date to the
  future … this API only retires the item, it does not delete it"
  ([Item inventory FAQ](https://developer.walmart.com/us-marketplace/docs/item-inventory));
  Seller Center 复活 = Site End Date 改未来。退役 item 的 SKU 与 Product ID 不能给
  别的 item 用("You can't reuse the SKU or Product ID from a retired item",
  [CA retireanitem](https://developer.walmart.com/ca-marketplace/reference/retireanitem))。
  蓝图 §retire 里"API 无 reactivate"应更正为"无专用端点,unretire = endDate 改未来"。
- DELETE:SKU **不保留**,永久;48h 内删、最多 72h 从目录消失;GTIN 24h 后可复用;
  同一 SKU 串 48h 后可重新 setup("wait for a 48-hour interval, and then set up a
  new item … using the same or a different SKU number",
  [Update my existing items](https://developer.walmart.com/us-marketplace/docs/update-my-existing-items),
  是 Marketplace 文档——蓝图 §遗留 2 写的"仅 1P"应更正)。
- 库存归零:SKU 完全不变,官方把它列为 DELETE 的可逆替代。
- unpublish:发布状态不是生命周期,item 与 SKU 原样在;连续 unpublished 超 90 天
  沃尔玛自动 retire([duplicate listings policy](https://marketplacelearn.walmart.com/guides/Policies%20&%20standards/Product%20listings/duplicate-listings-policy))。
- 沃尔玛侧自然缺席(missing_since):官方无此概念,不是 SKU 状态本身。

其余可确认(仓内有记载):SKU_LOCKED = SKU 绑死首次提交的 UPC,不先退役换 UPC
重发必败;退役后旧 UPC 永久烧号(本仓保守策略,非官方规则);24h 冷却是旧系统
实证,官方无明文;订单行只给 `item.sku` +
`productName`,行身份 = sha256(PO+SKU) ⇒ **订单只能靠 SKU 对到产品**,登记簿
反查是订单侧唯一通路;`walmart_items` 身份列 sku(PK)/wpid/item_id/upc/gtin。

仍待核(§8):SKU 长度上限与字符集(本地 spec Orderable.sku 定义)。

## 5. 生成时点与生命周期(问 1 的展开;2026-09-02 生命周期工作流定稿)

### 5.1 三个候选时点

| 时点 | 那时有店铺吗 | 判断 |
|---|---|---|
| A. `product_ingest` 入库 | 没有 | **出局**:维度不匹配;要给几十万空壳行发码 |
| B. `alloc_push` 派工(写上架表 A/B) | 有 | 可行但不推荐:派工与上架之间隔着审核 + 12 道闸,历史淘汰率 40%,登记簿会留大量幽灵行 |
| C. `list_new._prep_rows` 预备期 | 有,且是"确定要发"的最后一道 | **推荐**:与现有 `_UPC_PLACEHOLDER`"预备期占位、提交期回填"同构;跟卖已经是提交前生成 |

### 5.2 两条硬约束(缺一条就静默出事)

1. **mint 必须在 `_prep_rows`,不能在 `_one_store` 内**。串行补试
   (`store_retry.serial_second_pass`)会重跑 `_one_store`;若抽码在里面,第二次
   抽出新码 ⇒ 载荷不再一字不差 ⇒ `feeds.payload_key` 在途防重不命中 ⇒ 首轮已
   发出的片子被真的再发一次 = **双上架**。预备期抽码挂到 `r["_sku"]`,
   `_one_store` 只回填不抽码。
2. **码复用到显式弃码,不是"每次重上抽新码"**。三条现存护栏全绑在"同一个品
   同一个 SKU"上:FAILED 重试上限 `_SQL_ATTEMPTS`、`payload_key` 防重、UPC 池
   `claim` 的先复用后新领(键 (store, asin),存在理由就是 SKU 绑死首个 UPC)。
   每次重上抽新码会让 `claim` 把旧 item 已占的 UPC 发给新 SKU ⇒ 必撞
   ERR_EXT_DATA_0101119 ⇒ 每次重上白烧一个号,而且看起来像"运气差"。

### 5.3 生命周期规则(工作流三方案评审 + 对抗验证:8 条支撑断言 5 条站住、3 条被驳,已按驳回改稿)

**码的寿命 = 沃尔玛侧那条 (店, SKU) 记录对我们还有用的寿命,不是上架/下架次数。**

- 登记簿一行一码,**永不删除**;加列 `abandoned_at` / `abandoned_reason` /
  `replaced_by`。列名用 abandoned 不用 retired——"码弃用 ≠ 沃尔玛 lifecycle
  RETIRED ≠ product_clear 停用"三个同名异义,docstring 钉死。`abandoned_at IS NULL`
  的行叫活码。
- **弃码只有一个实现** `sku_codec.abandon(conn, store, sku, reason)`,同一事务
  内 UPDATE 登记簿 + 对 amz 行烧掉该 (店, ASIN) 名下 claimed/used 的 UPC(码与
  UPC 同寿命;烧号用独立状态值 `burned_delete` / `burned_lock`,不复用语义为
  "撞库"的 conflict);match 行只标不烧;reason=sku_update 不烧。
- **四个弃码点,只有四个**:
  1. DELETE 经 catalog_sync 观测核验 `delete_verified` 时(不是回执——"回执成功
     但后台没删"是所有者实证过的故障模式;若按回执弃码,下次新码新 UPC 去上一个
     还活着的 item = 同店重复 listing,沃尔玛不会替你拦);
  2. SKU_LOCKED 自愈链 RETIRE 回执成功 + 冷却期满(唯一绑回执的弃码点:锁死的
     SKU 可能从未进过 walmart_items,无观测可等);
  3. UPC 撞库 ERR_EXT_DATA_0101119 时码与 UPC 一起换(**决策 B**);
  4. 改码 SkuUpdate 经观测确认后旧行 `abandoned_at` + `replaced_by`。
- **其余一切"下架"都不弃码**:product_clear 停用(RETIRE)、库存归零、缺席
  `missing_since`、被沃尔玛 unpublish、提交失败/被拒/Unknown/PROHIBITED——沃尔玛侧
  记录仍在、仍绑着我们的 UPC,抽新码等于同店两条同内容记录 + 白烧一个 UPC。
  守门测试反向钉死:product_clear / problem_product_cleanup / maintenance /
  catalog_sync.mark_missing / feed_track 不得调用 abandon。
- **mint(store, source_type, source_key)**:先查活行 ⇒ 复用同一码(UPC 池 claim
  按 (店, ASIN) 复用原号);无活行 ⇒ 抽码 + 全局查重 + INSERT(同函数同事务),
  新码必配新 UPC。dry-run 用占位码不写库。**复用的理由是"一个 Product ID 只能挂
  一个 SKU"这条官方约束**(抽新码必撞),不是"同码重发能复活"——后者官方无
  明文,对抗验证 3/3 驳回:官方 reactivate 全走更新通道(Seller Center 改 Site
  End Date / MP_MAINTENANCE 最小载荷改 endDate,旧仓反补实证形态),MP_ITEM
  载荷里的 2028 endDate 只是格式实证留下的遗留常量。缺席/退役后同码重发 MP_ITEM
  到底是复活、被拒还是新建,**批次 2 前单品实测**(§8);显式"恢复"动作若要做,
  走 MP_MAINTENANCE `{sku, productIdentifiers, endDate}`,不走 MP_ITEM。
- **本店去重闸** `_SQL_LISTED_ASINS` 改为 walmart_items LEFT JOIN 登记簿,
  `missing_since IS NULL AND abandoned_at IS NULL`,键 `coalesce(source_key, sku)`;
  **不加 lifecycle 条件**——RETIRED 行只要码未弃就拦,退市档案不由 list_new 复活
  (2026-08-28 定稿:退市档案不许被自动链批量复活,plan.md:166)。
  `alloc_push._SQL_ONLINE` 排 RETIRED 的口径与去重闸不一致,但对抗验证指出派工
  去重键是**全表 ASIN 单列、不带店铺**(`append_assignments`),表里出现过的 ASIN
  不会被重复写入,所以不一致的实际后果只是"新 ASIN 被派、list_new 拦"——
  **决策 C** 降为建议对齐、非阻塞;派工去重不带店铺是既有口径差,另记。
- **护栏跟码走**:`_SQL_ATTEMPTS` 改按 (店, ASIN) 经登记簿 JOIN、按代际计(只数
  最近一次弃码之后的提交;无弃码事件则跨码累计);**代际上限**:同 (store,
  source_type, source_key) 弃码行数 ≥ 3 ⇒ list_new 写 N「换码次数达上限,待人工」;
  24h 冷却从 sku_locked_heal 自管泛化为 list_new 闸门(常量单一出处,官方无明文
  按旧实证保留)。
- **消费方契约**:resolve / 维护链 JOIN / 事件归并 / 订单反查对 (store, sku)
  一律不按 abandoned_at 过滤;全仓 SQL 里 `abandoned_at IS NULL` 只允许出现在
  `sku_codec.mint`、list_new 去重闸、`alloc_push._SQL_ONLINE` 三处(守门测试)。
  码级事件 `sku_abandoned` / `sku_replaced` 进 product_events。
- **跨店永不复用**:码全局唯一(含已弃码行),UPC 按店领。

### 5.4 同店同 ASIN 再上架:复用还是新抽(问答速查)

| 之前发生了什么 | 再上架时 | 依据 |
|---|---|---|
| 库存归零 / 被 unpublish | 不是"再上架":item 在架,恢复 = 推库存/修条件;去重闸拦 | 沃尔玛侧记录、码、UPC 全在 |
| 沃尔玛侧缺席(missing_since) | **复用**同码同 UPC(24h 冷却闸后);沃尔玛怎么处理这次重发待实测 | 抽新码撞 Product ID |
| product_clear 停用(RETIRE) | 记录仍被扫到 ⇒ 去重闸拦(恢复走显式动作);从响应集消失 ⇒ 按缺席复用 | 退市档案不由 list_new 复活 |
| 提交失败 / 被拒 / Unknown | **复用**,重试上限 3 次照旧 | 三条护栏 |
| DELETE 经观测核验 | **新码 + 新 UPC** | 沃尔玛侧无物可复活 |
| SKU_LOCKED 自愈链退役成功 | **新码 + 新 UPC** | 旧码绑死坏 UPC |
| UPC 撞库 0101119 | **新码 + 新 UPC**(决策 B) | 拆"撞库 → 同 SKU 换 UPC → 0101211"死循环 |
| 改码 SkuUpdate 后 | 不是再上架;旧串永不复用 | 一个 Product ID 只挂一个 SKU |

### 5.5 调研顺带发现的现状问题(与编码无关,要所有者定)

- **`SITE_END_DATE = "2028-12-31"` 是写死的日历日**(mp_mapper.py:32,注释自称
  "旧值"),全仓没有任何守门断言它相对提交时刻在未来;2028-12-31 之后同一段代码
  发出的就是过去的 endDate = 上架即退役。另立待办:改为"提交时刻 + N 年"并加
  守门测试。
- **维护类 feed 回执只有 `problem_product_cleanup` 来源才记事件**
  (`receipt_in_ledger`):stockzero / 维护链清零的回执不进病历,与"清零不入病历"
  口径一致,但意味着 24h 冷却闸只能依赖 `retire_feed_success`(kind=retire 恒记),
  不能依赖维护类回执。

`problem_scan` 的扫描面是"一切非 PUBLISHED 且未缺席",没有 lifecycle 豁免;退役
item 的观测形态正是 UNPUBLISHED +「end date has passed」。所以 product_clear
停用一个品,**一到两轮后它就会被自动链当问题商品 DELETE 掉**,"停用可恢复"在本
系统里只是个窗口(**决策 A**)。同理,若 0 库存真会触发 UNPUBLISHED(reason=
Inventory,仅代码注释无生产记录),可逆清零也会被升级成永久删除。

## 6. 身份积木

> **状态:已实现(批次 0a)。** 积木与 schema 随 PR-0a-1(commit `4e27789`)落地,
> 十五处读侧收口随 PR-0a-2 落地。下面三条索引的**名字与局部条件由批次 0a 一次
> 建到位,批次 2/3 与横切包一律引用、不许 DROP/CREATE**(批次 3 只做 indexdef
> 核验)。守门:`tests/test_sku_guard.py`(全套改造唯一一份)。

- `catalog.listing_sources`:加 `abandoned_at timestamptz`、`abandoned_reason text`、
  `replaced_by text`(三列只由 `services/sku_codec` 写);行永不 DELETE。
  三条索引(**定名定条件**,DDL 全文见 `refdata/schema.sql` 与
  `docs/sku_workplan/batch_0a.md` 的 ddl 段):
  · `listing_sources_opaque_sku_uidx` —— 全局 `(sku)` 唯一,局部条件 =
    不透明码形态 `AND sku ~ '[A-Z]'`。**只能对新码生效**:存量 sku=asin 跨店重复
    是既成事实,无条件唯一在存量上一定建不起来,而 db_init 一次 execute 整份
    schema.sql,一条失败全份回滚 ⇒ 生产建库停摆。
  · `listing_sources_live_uidx` —— `(store, source_type, source_key)` 唯一,局部条件 =
    `abandoned_at IS NULL AND replaced_by IS NULL AND source_key IS NOT NULL` +
    不透明码形态。**`replaced_by IS NULL` 批次 0a 就带上**(该列全库 NULL,谓词
    恒真),批次 3 因此一条索引都不必重建。拦并发双 mint 的就是它。
  · `listing_sources_live_key_idx` —— 非唯一 `(store, source_type, source_key)
    WHERE abandoned_at IS NULL AND replaced_by IS NULL`,给 mint 的复用查询用
    (要看得见存量活行,故不限形态;条件与 mint 的 WHERE 逐字对齐)。
  同步 `docs/db_schema.md`。
- `services/sku_codec.py`(新,批次 0a **只建不接线**,接线在批次 2):
  `mint` / `abandon` / `is_opaque` / `source_of`,语义见 §5.3。**它是 12 位不透明码
  编码规则的唯一之家**(字母表 / 长度 / 随机段长 / 重抽上限 / 占位码 / is_opaque
  判据都在这里出生);`registry` 只登记 `SKU_SOURCE_LETTERS`(所有者拍的取值,
  已定稿 `{amz: A, match: B, 1688: C, self: H}`)。schema.sql 两条唯一索引的字符类
  与本模块 `_ALPHABET` 由守门测试逐字对齐。与现有 `listing_sources.register`
  (批量 DO NOTHING)的关系:register 保留给 backfill 与跟卖 B 列人工号,
  自动抽码只走 mint —— 登记簿的 INSERT 出口只有这两个,守门钉住。
- `services/sku_asin.pick_asin(source_key, sku)` / `resolve(conn, store, sku)` /
  `resolve_many(conn, pairs)`:登记簿优先(amz → source_key;其它来源 → None),
  查不到再 `extract_asin`(只为存量兜底)。**登记簿那条腿不是免检通道** ——
  归一(strip+upper)后仍要过 `is_standard_asin`,两条腿同口径,否则运营在上架表
  B 列填的小写 ASIN 会变成垃圾键(后果:已在架的品被 alloc_push 重新派工)。
  放在 `services/sku_asin` 内,守门测试 `test_rules_are_not_reimplemented_here`
  才不会拦。对 abandoned 行照常返回 source_key(订单/售后带旧码回来必须查得到)。
  **分工**:`resolve` / `resolve_many` 是 services 内部的**有界批量反查**(几十几百对);
  **全表级取数一律在 SQL 里 LEFT JOIN 登记簿再调 `pick_asin`**,不要拿十万对去 unnest。
- `upc_pool`:`claim` 复用键保持 (store, asin) 不动;`mark_used` 改传真 SKU,
  `asin` 列继续存 ASIN;新增状态值 `burned_delete` / `burned_lock`(批次 0a 只登记
  取值与中文标签,**不改任何写入点** —— 改写入点随批次 2 接 abandon 一起做,决策 D)。

## 7. 批次(整体计划)

**批次 0|身份积木 + 读侧收口(零行为变化)**
codec(mint/abandon)/resolve + 登记簿索引与 abandoned_at/replaced_by + §3.2 六处 SQL 收口 +
§3.3 十四处消费方收口 + §3.4 里不依赖 V 列的四处(去重闸、重试上限、变体组、
UPC 撞库标记改按 (store, asin))+ 两条清洗工作流接 resolve_many +
sources_backfill 摘要分桶。存量 SKU 走的路一个字节不变(登记簿里存量行的
source_key 就是回填的 asin,等号右边换成它结果相同)。

> **批次 0a:已实现。** 工作包 `docs/sku_workplan/batch_0a.md`,两个 PR:
> · **PR-0a-1「积木 + schema + 守门」**(items 0a-01~0a-11、0a-27、0a-28)——
>   commit `4e27789`。交付:`services/sku_codec.py`(编码规则唯一之家,零接线)、
>   `sku_asin.pick_asin/resolve/resolve_many`、登记簿弃码三列 + 三条定名索引、
>   `audit_listing_conflicts` 视图身份键经登记簿、db_init 存量回填正则右锚
>   (与 `sources_backfill._ASIN_RE` 同口径,修掉一处会在 db_init 当场制造
>   `source_key ≠ sku` 行的双轨)、`upc_pool` 两个烧号状态值、事件码
>   `sku_abandoned`/`sku_replaced`、`registry.SKU_SOURCE_LETTERS`、守门文件
>   `tests/test_sku_guard.py`、`conventions §九`。
> · **PR-0a-2「十五处读侧收口」**(items 0a-12~0a-26)—— 维护链四处
>   (`_SQL_AMZ_JOIN` 的 products JOIN 与 latest_snapshot LATERAL、
>   `_SQL_VARIANT_OFFSET`、`_SQL_LONG_OOS` 的 live CTE)、`product_audit`
>   mode=online 候选(两条腿 OR,**不写 coalesce**:相关子查询要走索引)、
>   `risk_trace._ITEMS_SQL`(UNION 两条腿)、`product_refresh._SQL_TARGETS`、
>   `audit_rules` 实证 PT(走 Python 侧 `pick_asin`,纯 SQL coalesce 会破坏
>   三段式 SKU 的取中段口径)、`alloc_survey` / `alloc_push` / `alloc_plan` /
>   `alloc_products` 四处「已在架」、`list_new` 的去重闸 / 重试上限 / 变体同族。
>   守门里 PR-0a-1 留的六条临时白名单条目**已全部删除**,这六个文件从此
>   出现硬等号或 `extract_asin` 即红。
>
> **批次 0a 的两处有意保留(不是遗漏)**:
> · `workflows/product_audit.py` mode=online 的**第一条腿**故意保留
>   `w.sku = p.asin`(守门白名单里唯一的**永久**豁免):那是对 products 每行做的
>   相关子查询,写成 coalesce 就用不上 `walmart_items_sku_idx`,几十万行候选退化
>   成逐行全表扫(2026-08-14 视图挂死同一类事故)。新码由第二条腿覆盖。
> · `services/alloc_survey._SQL_ONLINE` 的 `lifecycle` 条件**不动** —— 这是对
>   synthesis required_changes #6 后半句(「也对齐、不再排 RETIRED」)的**显式驳回**:
>   ① 它管的是占用与冲突口径,不是派工口径,2026-08-15「退市行不算活货位」
>   仍成立;② 仓内两条守门钉着它(`test_alloc_audit::test_online_sql_excludes_retired_rows`、
>   `test_store_perf`);③ 去掉它是真行为变化,不属零变化批次。
>   `alloc_push` 的口径对齐(去掉 lifecycle 那一行)同样**不在 0a**,随批次 2 上
>   (决策 C 第二步),`test_alloc_push::test_online_set_still_excludes_retired`
>   反向钉着它。
测试钉:三种存量形态经 `resolve` 与 `extract_asin` 逐字相同;不透明码经
`extract_asin` 必返 None、经 `resolve` 能查到;`maintenance_scan -p preview=1`
在切换前后意图集合相同。
> **批次 0b:已实现。** 工作包 `docs/sku_workplan/batch_0b.md`,两个 PR:
> · **PR-0b-1「订单/事件/黑名单/审核链收口」**(items 0b-01~0b-23)—— commit
>   `153741c`。订单行落库当场补 asin(`upsert_order_lines._fill_asins`)、
>   `product_events.record_many` 带 store 走登记簿、黑名单实时侧
>   (`record_asins`/`collect_brands`)与**回填/重建侧 `_LATEST_CTE`**(原表漏列的
>   第 15 处,见 §3.3)身份键经登记簿、`feed_track` 传 source_key、
>   两条清洗工作流接 `resolve_pairs`、`sources_backfill` 三桶分类、守门七条。
> · **PR-0b-2「飞书列接线 + 文档」**(items 0b-24~0b-31)—— registry 三处列常量
>   (销售/售后订单「来源码」、在线产品总表第 17 列 `source_key`)、
>   `order_center` 两条 SQL 与两处载荷、`walmart_catalog._PROJECTION_SQL`
>   LEFT JOIN 登记簿。**所有者先建列、代码后合**(D-0b-4),窗口期为零。
>   ⚠ **验收预告**:加列 ⇒ 行指纹全变 ⇒ 合并后**第一次** order_center push 会把
>   90 天窗口(销售 + 售后各一次)全量重推一遍,这是**一次性的,不是故障**;
>   第二次跑必须回到「跳过 N」。在线产品总表同理,但所有者须先把该工作表列数
>   扩到 ≥17,否则 catalog_sync 撞 90204 拖累 product_chain 整链。
>
> **本工作包新增/撤回的三件事**:新增积木 `services/sku_asin.resolve_pairs`
> (两条清洗工作流共用的批量入口,带 store、倒查两级)与
> `services/order_audit.line_asin`(订单链取 ASIN 的唯一出处,四个调用点共用);
> **撤回**「0b 自建不透明码形态判据」—— 字母表唯一之家是 `services/sku_codec`,
> 守门测试唯一之家是 `tests/test_sku_guard.py`。
> **本批唯一的判定口径变化**(D-0b-2):`order_audit.judge` 改读 `line_asin` 后,
> 存量三段式 / 纯数字 item_id 形态的订单行从「待人工、不推采集」转入正常判定链。

**波及面一次做完(所有者 2026-09-02):** §3 全部条目在批次 0/1 内闭合,不留
"切换后再补";并加一条守门测试:`extract_asin` 的调用点与 `= w.sku`/`= sku`
形态的 SQL 硬等号只允许出现在白名单文件里,新增即红——防止切换后又长出新洞。

**批次 1|上架表按表头定位列 + SKU 列 + 回执自愈链(✅ 已实现,零行为变化)**
- **按表头名定位列**(所有者 2026-09-02:「以后再调整列顺序也能准确写入」):
  `registry.LISTING_SHEET.headers`(字段名→中文表头,21 项)是飞书表头名的
  唯一出生地;`services/listing_sheet.layout()` 每进程读一次表头行认列,
  **本文件所有 range 由它算,源码里一个写死的列字母都没有**
  (守门 `tests/test_sku_guard.py::test_listing_sheet_has_no_hardcoded_column_letters`)。
  fail-closed:登记的表头缺失/重复 → 抛错拒绝一切读写;多出的列只告警。
  相邻字段仍粘成一段,不相邻自动拆段(审核五列因中间夹着「类别」拆成两段)。
- **SKU 列(C)**:唯一出处 `listing_sheet.row_sku(r)`(SKU 列,空则回落 ASIN,
  逐字节等价);只写 SKU 列的 `write_sku_col`;`write_submit_cols` 收可选第 9 值
  (与 是否上架/feedid/上架日期 同一次写出);`clear_for_relist` **不清 SKU**。
- **五个 (店, SKU) 键 + 回执/自愈链**改读 `row_sku`:`sync_from_ledger` 的台账
  三个 dict、`heal_unknown` 的台账/目录腿、`sku_locked_heal` 的 todo 过滤 /
  RETIRE 载荷 / 冷却表 / 病历 / 回表找行(烧号键取自冷却表,天然同源)、
  `list_new` 的 `mark_used`。UPC 池那一腿仍按领号键 (店, ASIN)。
- **唯一有意的差异**:`_mark_upc_conflicts` 反查键由 `upc_pool.sku` 改为
  **(店铺, ASIN)**(见 §3.4;顺带修掉跨店误烧号与 missing 计数可为负)。
- **本批不写的三列**:「类别」归另一条 PR;「登记日期」「查询编码」人工填,程序永不读写。
(销售/售后订单表与在线产品总表的「来源码」列**已随批次 0b 的第二个 PR 落地**,
不在批次 1 范围内。)
存量行 SKU 列为空 ⇒ 回落 ASIN ⇒ 行为不变。

**批次 2|写侧切换(唯一有行为变化的批次)**
`SKU_SOURCE_LETTERS` 常量;`list_new._prep_rows` mint 挂 `r["_sku"]`,载荷/
`mark_used`/事件/登记/V 列回写全改 `r["_sku"]`(两条路:真跑 + check_spec);
`match_listing` 的 `make_sku` 换 mint(B 列人工优先保留);dry-run 占位码;
四个弃码点接 `codec.abandon`(§5.3);24h 冷却泛化为 list_new 闸门;代际上限;
守门测试反向钉死非弃码点不得调 abandon;测试钉"载荷 sku ≠ asin 且 = 登记簿里
那个"、"串行补试两次载荷一字不差"。
切换是全店同时的(码里没有店维配置),**试点靠 dry-run + 单店单品**:
1. `list_new --dry-run -p check_spec=1` 看载荷 sku 是占位码、其余字段正常;
2. 挑一家店、上架表只留一行待上,真跑 1 个品(`list_new` 目前无 limit 参数,
   要么加 `-p limit=`,要么手工只留一行);
3. `catalog_sync -p store=<店>` → 在线产品总表看到新 SKU 与来源码;上架表 V 列有值;
4. **`maintenance_scan -p preview=1 -p store=<店>` 必须能看见这个品**——批次 0
   的 SQL 收口做没做对的唯一实测;
5. 该品出一单后查 `orders.order_lines.asin` 有值、飞书销售订单表「来源码」列有值;
6. 对该行人为制造一次 FAILED 重试,确认复用同一 SKU、同一 UPC;
7. 通过后全店按常规节奏上。

**批次 3|存量产品改码(所有者 2026-09-02 拍板:要做)**
前置:批次 0/1/2 全部合并且新码在生产跑过至少一轮(读侧对两种码都认、上架表
V 列在位);§4 三件事单品实测通过。
机制:对每个存量 SKU(裸 ASIN / 三段式;跟卖 `PHUMWMT…` 不含 ASIN,是否迁 §8
定)——`mint` 抽新码并在登记簿写 `replaces=旧sku`,旧行标 `replaced_by=新sku`(pending;观测确认后 `abandoned_at`)+
`replaced_by=新sku`(**先落库再调接口**)→ 提交 `SkuUpdate=Yes` feed(载荷形态
按实测结果:MP_MAINTENANCE 最小载荷或 MP_ITEM 全量)→ 回执成功后:上架表 V 列
回写新码、`upc_pool.sku` 改新码、`walmart_items` 由 `catalog_sync` 自然出现新行;
旧行的缺席**不得**产生缺席事件/处置(按 `replaced_by` 压制)。
节奏:一店一批、按 MP_ITEM/MP_MAINTENANCE 10/hour × 2000 条/feed 的配额走;
先 1 个品 → 10 个 → 一家店 → 其它店;每一级都跑 `maintenance_scan preview`
与 `node_probe` 对比改码前后意图集合与库存。
受牵连的 (store, sku) 键表:`ops.dispositions` 在途行(改码前该店必须无
executing 行)、`maintenance` 的 drop_recent 防重键(自然过期)、
`ops.cleanup_seen_categories`(按 replaced_by 迁一次)、`listing.retire_cooldown`、
`catalog.item_node_inventory`(sync 重建)、`ops.feed_items`(历史,不动)、
**`catalog.product_events` 按 (store, sku) 读的三段 SQL**(problem_scan 的
`_SQL_STUBBORN` / `_SQL_LAST_CAT` / `_SQL_WFS_BLOCKED`,对抗验证发现):改码后
新码行在 `diff_catalog` 眼里 `prev is None` ⇒ 记 ITEM_APPEARED = **制造一次没有
重上架事实的假代际**,已实证 `delete_not_effective` 的顽固件会静默丢掉双 feed
加压。处理:diff_catalog 对 `replaced_by` 指向的新码行记 `sku_replaced` 而非
`item_appeared`;顽固判定改经登记簿按 ASIN 归并,或改码时把顽固标记随
replaced_by 迁到新码。
订单侧:改码后新单带新码;若沃尔玛对**改码前的 PO** 日后返回新码,会被当成
新行插入(旧行不删)⇒ 双算——切换前加一条"同 (store, po_id, line_number) 多个
order_line_id"的体检告警兜住。

**批次 4|另议**:`sku_locked_heal` 简化(有了"退役 ⇒ 新码",24h 冷却可能不
需要;官方无明文,留待实测)。

## 8. 待所有者决定 / 核验

- [ ] **生成时点**:C(list_new 预备期,推荐)还是 B(alloc_push 派工时,运营
      更早看到码,代价是幽灵行)。
- [x] **码的寿命**:复用到显式弃码(§5.3 四个弃码点,2026-09-02 工作流定稿)。若坚持"每次重上新码",
      须同时改 `upc_pool.claim` 复用语义与 `_SQL_ATTEMPTS`。
- [x] **存量产品**:迁到新码(2026-09-02 拍板),走 §7 批次 3。
- [ ] **决策 A|停用要不要成为真正可恢复态**:给 problem_scan 加「lifecycle=RETIRED
      且本仓提交过 retire_submitted」豁免(推翻 08-28「非 PUBLISHED 一律删除」的一
      部分)+ RETIRE 不弃码(推荐);或不豁免、改采「停用回执成功即弃码」简化版
      (永久失去可恢复,每次停用烧一个 UPC,且违背"不信回执信观测")。
- [ ] **决策 B|撞库 0101119 时码与 UPC 一起换**(推荐换):改变 08-09「撞库只是
      UPC 被占、照常领新号重试」的机制;不换有重演 SKU_LOCKED 死循环的风险,换最坏
      只是多耗一个免费的码。
- [ ] **决策 C|alloc_push 派工口径对齐去重闸**(建议对齐,非阻塞):退市且未弃码
      的 ASIN 从「该派工」变「等 delete_verified 后派工」;不对齐则新 ASIN 被派、
      list_new 每轮拦并写理由(派工去重键是全表 ASIN,已在表里的不会重复写入)。
- [ ] **批次 2 前单品实测**(所有者机器):停用后该 SKU 在 GET items 里是缺席还是
      RETIRED 可见;缺席/退役后同码同 UPC 重发 MP_ITEM 是复活同一 item、被拒还是
      新建(官方无明文,本仓与旧仓无一条实测);MP_MAINTENANCE 最小载荷改 endDate
      能否单独复活;RETIRE_ITEM feed 是否仍被受理;人为制造 0101119 看码与 UPC
      同换、代际上限生效。
- [ ] **§4 三件事单品实测**(所有者机器):`grep -rl SkuUpdate` 定 feed 类型与
      最小载荷;改码后库存/价格/item_id 是否保留;旧串能否复用。
- [ ] **跟卖存量**(`PHUMWMT+日期+序号`,不含 ASIN)是否也迁。
- [ ] **沃尔玛 SKU 规格**:本地 spec Orderable.sku 的长度上限、字符集。
- [x] **飞书建列**(所有者 2026-09-02 已建):上架表 R「SKU」;销售订单「来源码」;
      售后订单「来源码」;在线产品总表 Q「来源码」。统一叫「来源码」。
      【0b:代码分第二个 PR,建完列再合 —— 建列前程序载荷里没有这一列,
      零 WARNING 零重推;合并后第一次 push 全量重推一次是预告不是故障】
- [ ] **UPC 池表 E 列「SKU」口径**:改存真 SKU(ASIN 另列)还是保持现状。
- [x] **四个来源字母**(所有者 2026-09-02):amz=A、跟卖 match=B、1688=C、自建 self=H。
- [ ] **黑名单 `or sku` 兜底口径**:订单链是"提不出留 NULL",黑名单链是"原文
      兜底"。切换后原文兜底 = 往黑名单灌随机码;建议统一到"登记簿查不到就
      不入选",但这会改变拦截行为,要你拍板。
      【0b 默认:保持原文兜底,加日志计数 + 回填侧 `opaque` 只读计数;见 D-0b-1】
- [ ] **跟卖旧续号**(`PHUMWMT+日期+序号`)停用是否影响运营习惯(B 列人工优先不变)。
- [ ] **退役表 B 列**:运营从此填的是随机码,是否要程序回显来源码。
