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
| `services/listing_sheet._mark_upc_conflicts` | UPC 撞库标记 | 撞库的号永不标 conflict,反复领到坏号 | ✅ 批次 1:池反查键改 **(店铺, ASIN)**;✅ 批次 2(决策 B):入参改 (店, **行上 SKU**),一次 `abandon(reason=upc_conflict)` 弃码 + 烧号,SKU→ASIN 那一跳由 abandon 走登记簿 |
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

**待单品实测的六件事**(官方文档没写,本仓纪律"不按推断编码";批次 3 从三件扩到
六件,全部通过之前 `sku_migrate` 只许 --dry-run):
1. `SkuUpdate` 在本地 spec 的哪份里:`grep -rl SkuUpdate <DATA_ROOT>/specs/MP_ITEM/5.0.20260608-18_15_07-api/`
   (MP_MAINTENANCE 与 MP_ITEM 同版同布局)。**十分钟内能出结果,建议最先做** ——
   它同时决定形态 A/B(决策 E)与「mp_conform 放行 SkuUpdate」是保险还是必需(决策 I)。
2. MP_MAINTENANCE 收 `{sku 新码, GTIN/UPC 现号, SkuUpdate: Yes}` **最小载荷**能否改码
   ⇒ 能则**不用重发内容**(形态 A,当前实现);只有 MP_ITEM 全量载荷才行 ⇒ 改码 =
   重发全部内容(标题/属性会被我们再生成的内容覆盖,这是副作用,要所有者接受)。
3. 改码后库存、价格、item_id/wpid、变体组是否原样保留(`node_probe` + `GET
   /v3/items/{新sku}` 前后对比)。
4. 旧 SKU 串改码后能否再次使用(不打算复用,只为知道撞库风险)。
5. **对 lifecycle=RETIRED 的 item 是否可用**(存量里有停用未删的品;不可用的话
   `sku_migrate` 的候选面要再加一条 lifecycle 条件,而现在它只按"在架 = missing_since
   IS NULL"取)。
6. **改码之前的 PO 日后返回旧码还是新码**(官方零文档)。返回新码 ⇒
   `orders.order_lines` 会因 UNIQUE(po_id, sku) 插出第二行而旧行不删 ⇒ 销量/产品分/
   日报/对账全部多算且不报错。这一件决定 `orders.v_order_line_dupes` 体检的严重级别
   ——体检只能**发现**不能阻止,发现之后要人工决定合并口径。

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
  `alloc_push._SQL_ONLINE` **已于批次 2 对齐**(决策 C):去掉 lifecycle 条件,
  判据同为「没缺席 + 码还活着」——两处不同口径的可见后果是"退市未弃码的 ASIN
  被分配链每天派一次、被上架链每天拦一次";反向的坑更贵(排 RETIRED + 复用
  旧码 + 2028 endDate = 批量复活退市档案,plan.md:166)。
  `services/alloc_survey._SQL_ONLINE` **明确不改**:它答的是"占用/冲突里这家店
  有没有活货位",退市行不是活货位(2026-08-15 定稿);两处各有反向守门。
  派工去重键是**全表 ASIN 单列、不带店铺**(`append_assignments`),是既有口径差,
  另记。
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

**批次 2|写侧切换(唯一有行为变化的批次)** —— ✅ **已实现**(两块)

第一块(commit `50a76a4`):`SKU_SOURCE_LETTERS` 常量;`list_new._prep_rows`
在 ThreadPoolExecutor 之前单事务顺序 mint 挂 `r["_sku"]`,载荷 / `mark_used` /
事件 / 登记 / SKU 列回写全改 `r["_sku"]`(真跑 + check_spec 两条路);
dry-run 用 `DRYRUN_PLACEHOLDER` 不写库;**两道新闸**(退役冷却 / 代际上限,
阈值唯一出处 `sku_codec.RETIRE_COOLDOWN_HOURS` 与 `MAX_SKU_GENERATIONS`);
**`-p limit=N` 试点闸**(缺省 None = 与改造前逐字一致;截断在全部闸门与数据
过滤之后,被淘汰行不占名额)。

第二块(本次):
- **四个弃码点全部接 `sku_codec.abandon`**(§5.3):
  ① `catalog_sync` —— `product_events.verify_deletions` 返回第三元
  (生效的 (店, SKU) 名单),弃码与 `delete_verified` 事件**同一事务**;
  ② `sku_locked_heal` —— RETIRE 回执成功 + 冷却期满处,`burn_pairs` 改
  `abandon_pairs`(冷却表里存的是 SKU 码,ASIN 那一跳由 abandon 走登记簿完成;
  裸烧号在切码后匹配恒空、静默失效),`cooldown_hours` 默认值改读常量;
  ③ `listing_sheet._mark_upc_conflicts` —— **决策 B 落地**:入参第二元改成
  `row_sku(r)`,一次 abandon 把码弃掉、号由分派表烧成 `conflict`,不再另调
  `mark_conflict`;④ 改码留给批次 3(常量与"不烧号"分支已在位且被守门钉着
  零调用)。
- **决策 D 落地**:`upc_pool.burn_for_retire` 与 `mark_conflict` **删除**,烧号
  唯一函数 `burn(conn, pairs, status)`,状态只由 `sku_codec._BURN_STATUS` 给
  (delete_verified→burned_delete、sku_locked→burned_lock、upc_conflict→conflict)。
- **`match_listing` 分两趟**:第一趟纯网络(逐行 SPEC 预检 + 两道闸,**不开
  事务**),第二趟短事务里发码与登记(B 列人工号优先并在**提交前** register,
  留空的行 mint),commit 早于 `submit_feed`;提交成功后不再登记。
  `match_feed` 的 `SKU_PREFIX` / `make_sku` / `next_serial_start` **已删**
  (守门钉住第二条发码路径不许复活)。
- **决策 C 落地**:`alloc_push._SQL_ONLINE` 去掉 lifecycle 条件、与去重闸同口径;
  `alloc_survey._SQL_ONLINE` **一个字不改**(两处都有反向守门)。
- **修既有破口**:`feed_poll` 从此认 `--dry-run` —— 它 `DANGEROUS=False`、cli 恒
  传 `execute=True`,而反哺器里有不可逆的 PG 写(弃码 + 烧号 + UPC 标已用)。
  五个反哺器统一加 `execute` 关键字,空跑一行飞书、一行 PG 都不写。
- DDL:`listing_sources_abandoned_idx`(部分索引,代际上限闸的 GROUP BY 用)。
- 守门四组新断言:弃码调用点白名单 = 四处、五个破坏/清理链反向零弃码、
  `sku_update` 零调用且不烧号、冷却与代际两个常量各只有一个出生地;另加
  第二条发码路径零复活、回执码零字面量、新索引在位。

切换是全店同时的(码里没有店维配置),**试点靠 dry-run + 单店单品**:
1. `list_new --dry-run -p check_spec=1` 看载荷 sku 是占位码、其余字段正常;
2. 挑一家店真跑 1 个品:`list_new -p store=<店> -p limit=1`(`-p limit=N`
   由本批交付,不必再手工删行);
3. `catalog_sync -p store=<店>` → 在线产品总表看到新 SKU 与来源码;上架表 V 列有值;
4. **`maintenance_scan -p preview=1 -p store=<店>` 必须能看见这个品**——批次 0
   的 SQL 收口做没做对的唯一实测;
5. 该品出一单后查 `orders.order_lines.asin` 有值、飞书销售订单表「来源码」列有值;
6. 对该行人为制造一次 FAILED 重试,确认复用同一 SKU、同一 UPC;
7. 通过后全店按常规节奏上。

**批次 3|存量产品改码(所有者 2026-09-02 拍板:要做)** —— ✅ **已实现**(三块)

**目标改写为「止血」**(必须先说清,否则后续风险判断建立在错误前提上):存量改码
**收不回**沃尔玛已经掌握的旧 SKU=ASIN 关联 —— SkuUpdate feed 本身就是「旧串 → 新码」
的显式映射,历史订单与历史 feed 记录里的关联也还在。它只让**切换之后**的记录干净。

前置:批次 0/1/2 全部合并且新码在生产跑过至少一轮(读侧对两种码都认、上架表 SKU 列
在位);§4 **六件**单品实测通过;改码期间该店无人手工改 SKU/Product ID;**旧仓
product_clear / daily_cleanup / auto_listing 调度已停**(安全红线「新旧系统严禁对同一
破坏性任务并跑」)。

第一块(commit `cc08210`)**地基**:schema 加 `replaces` / `replaced_at` 与两个局部反查
索引、过程账 `listing.sku_migrations`、别名视图 `catalog.sku_aliases`、体检视图
`orders.v_order_line_dupes`、`product_risk` 加改码两列;`sku_codec.mint_replacement` /
`settle_replacement` / `OPAQUE_SQL_PREDICATE`;`listing_sources.replacement_map` /
`replaced_skus`;`upc_pool.retag_sku`;`mp_mapper.build_sku_update_item`(形态 A 最小载荷)
与 `ORDERABLE_SYSTEM_FIELDS` 登记 SkuUpdate;`mp_conform` 放行系统开关(决策 I);
`order_lines.duplicate_po_lines` 退化成读视图的薄壳。**api/feeds 零代码改动**(两个
feedType 与两个桶都已收录,只补注释与测试)。

第二块(commit `5565691`)**观测侧抑制/继承**:改码期间不记假代际(`diff_catalog` 对
新码首次被扫到不记 `item_appeared`)、旧码不记缺席(`mark_missing` 照标 `missing_since`
但不记 `item_missing`)、`problem_scan` 的扫描面排除在途改码旧码 + 顽固/归类/WFS/在途
四段判据经 `catalog.sku_aliases` **继承一跳**、`alloc_survey` 销量归属同样继承(决策 H)、
`sku_migrate` 的回执不进病历也不反哺黑名单;`dispositions.open_executing_count` /
`rekey_suggested`、`walmart_catalog.drop_node_rows` 两个积木。

第三块(本次)**工作流** `workflows/sku_migrate.py`(DANGEROUS=True、SUPPORTS_STORE=True、
`-p store=` **必填**、**永不进调度**):

- **三态判据表**(定案只信观测,回执成功单独不定案):

  | 判词 | 证据组合 | 后果 |
  |---|---|---|
  | `confirmed` | 新码在架 ∧ 旧码缺席 | 旧行 `abandon('sku_update')`(**不烧 UPC**)+ 新码记 `sku_replaced` + `upc_pool.retag_sku` + 上架表 SKU 列回写 + `dispositions.rekey_suggested` + `drop_node_rows` + 台账 confirmed |
  | `rolled_back` | 回执 `failed`;或**观测新鲜**且新码超 `OBSERVE_HOURS`(24h)仍未出现;或 POST 当场判 failed | 旧行 `replaced_by` 清空(复活)+ 新码 `abandon('sku_update_failed')` + 台账 rolled_back。**不自动补交**(写操作永不自动兜底);下一轮重来会抽新码 |
  | `stalled` | 超 `STALE_HOURS`(72h)仍判不出 | 只落台账 + 摘要点名人工,**不自动定案**(判不准就判活:回滚一个其实已生效的改码 = 登记簿说旧码、沃尔玛说新码,而且不报错) |
  | (不定案) | 新码在架 ∧ 旧码**也**在架 | **同店双挂**:只告警不处置,摘要**首行**点名。节奏闸是它的主要防线 |
  | (不定案) | POST `outcome=unknown` | **保持 pending 不回滚**(决策 F) |
  | (不定案) | pending 但 `submitted_at` 为空(**落库未提交**) | 进程死在 POST 前后、或提交当场抛异常。**只点名不自动定案**:从台账上分不出「确定没发」与「不知道到没到」。人工核:先 `feed_poll` 让 `ops.feed_log` 那条落定,再去后台看这个 Product ID 现在挂的是哪个 SKU |

- **节奏硬闸**(`_stage_cap`,把口头节奏变成代码):该店还有 pending/stalled ⇒ 本轮
  上限 0(只定案不提交);零 confirmed ⇒ 1;<10 ⇒ 10;≥10 ⇒ 按 `-p limit`。
  **`-p limit=` 只能收紧**;再叠一层配额留量硬顶 `FEEDS_PER_STORE_PER_RUN × ITEMS_PER_FEED`。
- **五道整店前置闸 + 一道逐候选闸**:在营 / 目录水位新鲜 / 该店 `executing` 处置为 0 /
  `retire_cooldown` 无 pending / 本工作流无在途 feed;逐候选再看旧码上有没有 48h 内的
  在途 feed(不整店拦,跳过并点名)。任一不过**不抛异常**,在摘要里点名"为什么不能改"。
- **事务边界**:`mint_replacement` + pending 台账在一个事务里写完并 **commit**,
  **之后**才组载荷、才 POST。在未提交事务里 POST = 进程一死就是"沃尔玛已受理、我们
  这边零记录"的孤儿码。
- **dry-run 三纪律**:`_settle` 与 `_migrate` **都**零写(不 mint、不定案、不提交、
  不写飞书、不改处置、不删节点库存),摘要用占位码打印将改的前 N 行与载荷样例;
  `🧪 [DRY-RUN]` 前缀拼在**首行行首**(cli 的链通知只取首行)。
- **上架表 SKU 列回写在事务之外**,写成功才盖 `sheet_synced_at`;每轮开头先补写
  `status='confirmed' AND sheet_synced_at IS NULL` 的行(一次写失败之后该行已是
  confirmed、不再进 pending 判决面,没有补写路径它就永远停在旧码而且不报错)。
- **形态 A**(决策 E 默认):`FEED_TYPE = "MP_MAINTENANCE"` + `build_sku_update_item`
  最小载荷,**不重发内容**;切形态 B 只改 `FEED_TYPE` 与 `_build_items` 两处,但
  **必须先**确认 `mp_conform` 放行 SkuUpdate(被剔掉 ⇒ 每一行都退化成普通上架 =
  每一行都双挂,而且回执一片成功)。**不提供参数覆盖**(双轨禁止)。

**受牵连的 (store, sku) 键表 —— 逐条结论**(2026-09-02 复核,结论与依据都留下,
免得下一轮盘点又把它们当待办):

| 键 | 结论 | 依据 |
|---|---|---|
| `ops.dispositions` 未落定建议 | **迁**(`rekey_suggested`,撞唯一索引的动作不迁不删、点名人工) | 建议是"这个 item 怎么处置",item 没变、只是身份列换了 |
| `ops.dispositions` executing 行 | **不迁**,改由前置闸③挡住(该店有 executing 就不许改码) | 搬键 = 把已提交 feed 的判决对象换掉 |
| `catalog.item_node_inventory` | **删旧码行**(`drop_node_rows`) | 旧码在沃尔玛侧已不存在,留着是永不更新的幽灵行,而维护链的受管仓判据照读不误 |
| `catalog.upc_pool` | **改标**(只动 `sku` 列;`asin`/`status`/`used_at` 不动) | 改码不是一次新消耗;领号复用键仍是 (店, ASIN) |
| `listing.retire_cooldown` | **不迁**,改由前置闸④挡住 | 冷却表里存的是旧码,pending 期间不许改码 |
| `ops.feed_items`(历史) | **不动** | 历史台账按提交时的码记账;新码的回执按新码落账(`_chunk_skus` 头注) |
| `ops.cleanup_seen_categories` | **不迁** | 全仓只有 `cleanup_history_import` 写它、**零读者**(2026-09-02 grep 复核);主键是 (sku, category) 唯一对、是累计计数的真值来源,复制会多算、重命名会偷走别店历史(表无 store 维度) |
| `ops.dedupe` scope=`maintenance:submitted` | **不迁,自然过期** | 键含 sku,但窗口 `SUPPRESS_HOURS=20` < 一轮观测期 24h;最坏是定案后多发一次同值维护意图(可能收到 0101198 stale update,非破坏) |
| `ops.dedupe` scope=`cleanup:brand_asin` / `cleanup:brand_scrape` | **不受影响** | 键是 ASIN |
| `catalog.claims.claim_key` | **不受影响** | 键是 ASIN 或品牌归一键 |
| `catalog.product_events` 的四段历史判据 | **经 `catalog.sku_aliases` 继承一跳** | 顽固代际 / 问题归类 / WFS 拦截 / 在途防重(第二块已落地) |
| `services/alloc_survey._SQL_SALES` | **经 `sku_aliases` 继承**(决策 H) | 不映射的话迁过码的品销量/GMV 恒 0,而且不报错 |
| `workflows/problem_scan._SQL_INFLIGHT` | **经 `sku_aliases` 继承** | 在途防重的代际跟着码走,断链 = 同一个品被重复加压 |

订单侧:改码后新单带新码;若沃尔玛对**改码前的 PO** 日后返回新码,会被当成新行插入
(旧行不删)⇒ 双算。判据只有一处:`orders.v_order_line_dupes` 视图
(`services/order_lines.duplicate_po_lines` 是读它的薄壳,`sku_migrate` 每轮跑一次并把
非零结果点在摘要首行)。**体检只能发现不能阻止**:改码前跑一次存档基线,改码后必须一致。

节奏(所有者定):**1 个品 → 10 个 → 一家店 → 其它店**,每一级之间至少隔一轮
`catalog_sync`,并跑 `maintenance_scan --dry-run -p preview=1`、`node_probe`、
`alloc_survey --dry-run` 三项前后对比(意图集合只应该有 SKU 串变了;销量不得掉到 0)。


**批次 4|另议**:`sku_locked_heal` 简化(有了"退役 ⇒ 新码",24h 冷却可能不
需要;官方无明文,留待实测)。

## 8. 待所有者决定 / 核验

- [ ] **生成时点**:C(list_new 预备期,推荐)还是 B(alloc_push 派工时,运营
      更早看到码,代价是幽灵行)。
- [x] **码的寿命**:复用到显式弃码(§5.3 四个弃码点,2026-09-02 工作流定稿)。若坚持"每次重上新码",
      须同时改 `upc_pool.claim` 复用语义与 `_SQL_ATTEMPTS`。
- [x] **存量产品**:迁到新码(2026-09-02 拍板),走 §7 批次 3 —— **三块全部实现**
      (工作流 `workflows/sku_migrate.py`);**生产投放尚未开始**,等六件单品实测。
- [ ] **决策 A|停用要不要成为真正可恢复态**(批次 2 按默认实现:**RETIRE 不弃码**,
      守门反向钉死 `product_clear` 不得调 abandon;problem_scan 豁免仍未拍板,
      `workflows/product_clear.py` 头注已写明「可恢复窗口 ≈ 到下一轮 problem_scan」):给 problem_scan 加「lifecycle=RETIRED
      且本仓提交过 retire_submitted」豁免(推翻 08-28「非 PUBLISHED 一律删除」的一
      部分)+ RETIRE 不弃码(推荐);或不豁免、改采「停用回执成功即弃码」简化版
      (永久失去可恢复,每次停用烧一个 UPC,且违背"不信回执信观测")。
- [x] **决策 B|撞库 0101119 时码与 UPC 一起换**(取默认「换」,**批次 2 已落地**):
      `listing_sheet._mark_upc_conflicts` 一次 `abandon(reason=upc_conflict)`,
      号仍烧成 `conflict`(那个值的语义就是"号被别人占了")。改变 08-09「撞库只是
      UPC 被占、照常领新号重试」的机制;不换有重演 SKU_LOCKED 死循环的风险,换最坏
      只是多耗一个免费的码(码空间 30^11)。
- [x] **决策 C|alloc_push 派工口径对齐去重闸**(取默认「对齐」,**批次 2 已落地**):
      只改 `alloc_push`(去掉 lifecycle 条件),`alloc_survey` 明确不改 —— 两条答的
      不是一个问题,两处都有反向守门钉着。
- [ ] **批次 2 前单品实测**(所有者机器):停用后该 SKU 在 GET items 里是缺席还是
      RETIRED 可见;缺席/退役后同码同 UPC 重发 MP_ITEM 是复活同一 item、被拒还是
      新建(官方无明文,本仓与旧仓无一条实测);MP_MAINTENANCE 最小载荷改 endDate
      能否单独复活;RETIRE_ITEM feed 是否仍被受理;人为制造 0101119 看码与 UPC
      同换、代际上限生效。
- [ ] **§4 六件事单品实测**(所有者机器,**批次 3 的关键路径**;全部通过前
      `sku_migrate` 只许 --dry-run):`grep -rl SkuUpdate` 定 feed 类型(十分钟可出,
      建议最先做);MP_MAINTENANCE 最小载荷能否改码;改码后库存/价格/item_id/变体组
      是否保留;旧串能否复用;**对 lifecycle=RETIRED 的 item 是否可用**;
      **改码之前的 PO 日后返回旧码还是新码**(决定订单双算体检的严重级别)。
- [ ] **决策 D|跟卖存量**(`PHUMWMT+日期+序号`,不含 ASIN)是否也迁。
      【批次 3 按默认实现:**不迁** —— `sku_migrate.SOURCE_TYPES = ('amz',)`,候选 SQL
      天然排除 match 行。理由:PHUMWMT 串本就不含 ASIN,货源隐匿收益为零;match 行的
      source_key 是匹配 GTIN,改码后 `upc_pool` 的 (店, ASIN) 键无从对上。要迁需追加
      一轮单品实测(MP_ITEM_MATCH v4.2 是否也认 SkuUpdate,官方零文档)+ 一个
      `_build_items` 分支,**状态机不用改**】
- [ ] **决策 E|SkuUpdate 的 feed 形态**:A(MP_MAINTENANCE 最小载荷)还是
      B(MP_ITEM 全量)。【批次 3 按默认实现 **A**,常量 `sku_migrate.FEED_TYPE`;
      由六件实测第 1、2 件裁定。**形态 B 有一个所有者必须先接受的副作用**:改码 =
      重发全部内容,标题与属性会被我们再生成的内容覆盖;而且它吃 list_new 的
      MP_ITEM 桶。若实测判定只有 B 可行,建议**回到计划层让所有者拍板**再动手】
- [ ] **决策 H|改码后的历史销量归属**:经 `catalog.sku_aliases` 映射(已实现),
      还是把聚合键整体改成 ASIN(更根本,但要改三个消费点的键形状,属另一个批次)。
- [ ] **决策 I|形态 B 下 SkuUpdate 如何穿过 `mp_conform.strip_unknown`**:
      【按默认实现:`ORDERABLE_SYSTEM_SWITCHES` 显式登记 + 放行。形态 A 用不到它,
      但**仍必须先做** —— 决策 E 一旦翻到 B,没有它就是每一行都双挂而且回执全绿】
- [ ] **沃尔玛 SKU 规格**:本地 spec Orderable.sku 的长度上限、字符集。
- [x] **飞书建列**(所有者 2026-09-02 已建):上架表 R「SKU」;销售订单「来源码」;
      售后订单「来源码」;在线产品总表 Q「来源码」。统一叫「来源码」。
      【0b:代码分第二个 PR,建完列再合 —— 建列前程序载荷里没有这一列,
      零 WARNING 零重推;合并后第一次 push 全量重推一次是预告不是故障】
- [ ] **UPC 池表 E 列「SKU」口径**:改存真 SKU(ASIN 另列)还是保持现状。
      【默认 (a) 不加列:E 列 = `catalog.upc_pool.sku` 的投影,批次 2 起显示真码;
      领号复用键仍是 `upc_pool.asin`。烧号新增两个状态文案「删除烧号/锁死烧号」】
- [x] **四个来源字母**(所有者 2026-09-02):amz=A、跟卖 match=B、1688=C、自建 self=H。
- [ ] **黑名单 `or sku` 兜底口径**:订单链是"提不出留 NULL",黑名单链是"原文
      兜底"。切换后原文兜底 = 往黑名单灌随机码;建议统一到"登记簿查不到就
      不入选",但这会改变拦截行为,要你拍板。
      【0b 默认:保持原文兜底,加日志计数 + 回填侧 `opaque` 只读计数;见 D-0b-1】
- [x] **跟卖旧续号**(`PHUMWMT+日期+序号`)**已于批次 2 停用并删除**:B 列人工优先
      不变(人工号在提交前 register 进登记簿),留空的行由 `sku_codec.mint` 发码。
      存量 PHUMWMT 行不受影响(读路径全格式通吃)。
- [ ] **退役表 B 列**(§3.5 建议 1,**未做,仍在待办**):运营从此填的是随机码,
      是否要程序回显来源码。
- [ ] **维护记录表是否加「来源码」展示列**(§3.5 建议 2,**未做,仍在待办**):
      与上一条同源 —— 运营在表上只看得到一串随机码时,"这是哪个品"要能一眼看出来。
- [ ] **`variant_group.group_id` 仍把 ASIN 递给沃尔玛**(批次 3 评审转出的目标级
      漏洞):改码把 SKU 里的 ASIN 摘掉了,变体组 ID 这条线还在往外递。属编码规则层,
      不属批次 3;要单独定口径(换成不透明组号 = 变体组的身份也要过登记簿)。

### 8.1 批次 3 待验收清单(所有者动作;代码已就绪,**生产投放尚未开始**)

**① 六件单品实测**(见 §4;全部通过前 `sku_migrate` 只许 `--dry-run`):
第 1 件十分钟可出结果、且同时决定形态 A/B 与 `mp_conform` 放行的必要性,**先做它**。

```bash
grep -rl SkuUpdate <DATA_ROOT>/specs/MP_ITEM/5.0.20260608-18_15_07-api/   # 实测 1
```

**② 体检 SQL(改码前跑一次存档基线,每一级投放之后再跑一次对比)**:

```bash
DSN="$(python -c 'from registry import db;print(db.pg_dsn())')"
# 订单双算:改码前后必须一致(这是"改码后销量双算"唯一能被发现的手段)
psql "$DSN" -c "SELECT count(*) FROM orders.v_order_line_dupes;"
# 别名链:改码前恒为 0
psql "$DSN" -c "SELECT count(*) FROM catalog.sku_aliases;"
# 活码部分唯一索引只有一条,且条件已含 replaced_by IS NULL(0a 交付,这里只核验)
psql "$DSN" -c "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='catalog' AND tablename='listing_sources';"
# 过程账三态分布(每一级投放之后看)
psql "$DSN" -c "SELECT status, count(*) FROM listing.sku_migrations WHERE store='<试点店>' GROUP BY 1;"
# 已定案的行必须都回写过上架表 SKU 列(非空);为空的下一轮会自动补写
psql "$DSN" -c "SELECT id, new_sku, sheet_synced_at FROM listing.sku_migrations WHERE store='<试点店>' AND status='confirmed';"
# 定案后的身份两端 + UPC(号不动、只换挂在它名下的 SKU)
psql "$DSN" -c "SELECT sku, replaced_by, replaced_at, abandoned_at, abandoned_reason FROM catalog.listing_sources WHERE store='<试点店>' AND sku='<旧码>';"
psql "$DSN" -c "SELECT sku, status, asin, used_at FROM catalog.upc_pool WHERE store='<试点店>' AND asin='<试点 ASIN>';"
# 事件账本:必须有 sku_replaced,必须**没有** item_appeared / item_missing
psql "$DSN" -c "SELECT event, count(*) FROM catalog.product_events WHERE store='<试点店>' AND sku IN ('<旧码>','<新码>') GROUP BY 1;"
```

**③ dry-run(人眼确认之后才跑真的;这是纪律,没有默认值替你挡)**:

```bash
python cli.py db_init && python cli.py db_init          # 连跑两次验幂等
python cli.py sku_migrate --dry-run -p store=<试点店> -p limit=1
#   人眼确认:六道闸的结论、候选、载荷 sku 是占位码、productIdentifiers 是该品**现挂**的号、零写库
python cli.py sku_migrate -p store=<试点店> -p limit=1  # 第一级:1 个品(节奏闸会把任何 limit 压到 1)
python cli.py catalog_sync -p store=<试点店>            # 等一轮完整观测
python cli.py sku_migrate -p store=<试点店> -p settle_only=1 --dry-run   # 先空跑看判决
python cli.py sku_migrate -p store=<试点店> -p settle_only=1             # 定案
# 三项前后对比(意图集合只应该有 SKU 串变了;销量不得掉到 0;被改码的 SKU 不得出现在任何建议里)
python cli.py maintenance_scan --dry-run -p preview=1 -p store=<试点店>
python cli.py node_probe -p store=<试点店>
python cli.py alloc_survey --dry-run
python cli.py problem_scan --dry-run
# 前两级全部 confirmed 之后再往上走
python cli.py sku_migrate -p store=<试点店> -p limit=10       # 第二级
python cli.py sku_migrate -p store=<试点店> -p limit=100000   # 第三级:整店
```

**④ 运行纪律**:改码只在 13:00 的 `product_chain` 之外跑(共享 MP_MAINTENANCE 桶);
改码期间该店不得有人在 Seller Center 手工改同一批 item 的 SKU/Product ID;
**旧仓 `product_clear` / `daily_cleanup` / `auto_listing` 调度必须已停**
(`crontab -l | grep -Ei 'auto_listing|retire_and_relist|product_clear|daily_cleanup'` 输出为空)。

## 9. 决策日志(SKU 改造批次 3,2026-09-02)

> 本仓的记录纪律是「跑过的都有 [x]、决策都有日期与依据」(conventions §五)。
> 三处**有意出入**若不写在这里,下一次复核会把它们当成实现漏洞改回去。
> `docs/plan.md` 没有决策日志段(它的记录方式是 Phase 小节里的 `[x] + 日期`),
> 所以批次 3 的决策记在这里,plan.md 只留一行指针。

### 9.1 与工作包 / synthesis 的三处有意出入

| # | 出入 | 采用的做法与依据 |
|---|---|---|
| 1 | POST `outcome=unknown` 是否回滚 | **不回滚,保持 pending**。synthesis 里「failed/未达/Unknown ⇒ rolled_back」说的是**回执**三态(`_settle` 的输入),与 POST 的 outcome 是两件事。unknown 的语义是「不知道到没到」,`api/feeds` 对它的既定处置就是保持 pending 待启动对账;若沃尔玛其实已改成新码而我们回滚了登记簿,新码就成了没有出身的孤儿行(`sources_backfill` 判 unknown ⇒ 退出全部自动化),而且不报错。写进了 `workflows/sku_migrate` 的头注与两条测试 |
| 2 | `ops.cleanup_seen_categories` 是否按 `replaced_by` 迁 | **不迁**。2026-09-02 grep 复核:全仓只有 `cleanup_history_import` 写它、**零读者**;它的主键是 (sku, category) 唯一对、是累计计数的真值来源 —— 复制一份会多算,重命名又会偷走别店历史(表无 store 维度)。将来若接报表消费方,键应走登记簿 `source_key` 而不是原文 sku |
| 3 | 活码部分唯一索引由谁收紧 | **批次 0a 一次建成最终条件**(含 `replaced_by IS NULL`),批次 3 **只核验、不 DROP、不重建**,另加两个**局部**反查索引。原稿让批次 3 去 `DROP INDEX IF EXISTS` 再裸建同名唯一索引:那三个索引名谁都没建过 ⇒ DROP 静默 no-op(收紧根本没发生),而不带局部条件的 `CREATE UNIQUE` 会在存量重复活行上失败 —— `db_init` 是整份 schema.sql 一次 execute,一条失败整份回滚 ⇒ 生产建库当场停摆 |

### 9.2 九个决策点的默认取值(所有者拍板结果留白)

| 决策 | 默认(已按此实现) | 所有者裁决 |
|---|---|---|
| A|`product_clear` 停用(RETIRE)是否弃码 / problem_scan 是否加豁免 | **RETIRE 不弃码**;豁免另议。批次 3 的 `_SQL_ITEMS` NOT EXISTS 与将来的 lifecycle 豁免是并列的两条独立条件,先加哪条都不冲突 | ☐ |
| B|UPC 撞库 0101119 时码与 UPC 是否一起换 | **一起换**(批次 2 已落地)。改码 confirmed 的 `abandon(reason='sku_update')` 必须**不烧号**,与撞库那支走不同分支,分支由 reason 决定 | ☐ |
| C|`alloc_push` 派工口径是否对齐去重闸 | **对齐**(批次 2 已落地)。pending 期间旧码行 `abandoned_at IS NULL` 且在架,按对齐后的口径仍算「已在架」⇒ 不会被重新派工,**不需要**在 `_SQL_ONLINE` 里额外加 `replaced_by` 条件(别好心补一条冗余条件) | ☐ |
| D|跟卖存量是否也迁 | **不迁**(`SOURCE_TYPES = ('amz',)`) | ☐ |
| E|feed 形态 A 还是 B | **A**(`FEED_TYPE = "MP_MAINTENANCE"`),由六件实测第 1、2 件裁定 | ☐ |
| F|POST `outcome=unknown` 是否回滚 | **不回滚,保持 pending**(见 9.1-1) | ☐ |
| G|`cleanup_seen_categories` / `ops.dedupe` / `catalog.claims` 是否随改码迁 | **三者都不迁**(逐条依据见 §7 批次 3 的键表) | ☐ |
| H|改码后历史销量归属 | **经 `catalog.sku_aliases` 映射**,返回键形状不变、三个消费点一字不改 | ☐ |
| I|形态 B 下 SkuUpdate 如何穿过 `strip_unknown` | **显式登记 `ORDERABLE_SYSTEM_SWITCHES` 并放行**(名单穷举、触发记日志、条件明确,满足 §六 真兜底三要件);形态 A 用不到但仍先做 | ☐ |

### 9.3 本次评审驳回或转出的意见(不静默丢弃)

- **驳回归属,不驳回内容**:「批次 3 顺手给 `list_new` 加 `-p limit`」—— 那是批次 2 的
  止损闸,写进批次 3 会让一个 DANGEROUS 的一次性工作流去改上架主链。**已在批次 2
  落地**(照 `_stage_cap` 的形状)。
- **驳回**:「POST `outcome=unknown` 按 synthesis 字面回滚」—— 理由见 9.1-1。
- **转出**:`workflows/product_clear.py` 头注关于「可恢复窗口」的措辞更正 —— 属决策 A
  的落地面(批次 2 或横切),批次 3 加了扫描面排除之后并不改变停用品的命运。
- **转出**:`services/blacklist.py` 的「黑名单键被灌随机码」—— 真问题,但它在**批次 2
  新码上线当天**就会发生,不能等批次 3;归 0b 的 `or sku` 兜底口径(§8 待决项)。
- **转出**:`variant_group.group_id` 仍把 ASIN 递给沃尔玛 —— 目标级漏洞,属编码规则层,
  已进 §8 待决清单。

### 9.4 已知缺口(记在案,等所有者定)

- **`abandon` 单向不可逆,全套工作包没有「撤销弃码」的人工入口**。中间窗口内运营在
  Seller Center 手工改 Site End Date、或形态实测推翻判据时,只能靠人裸 UPDATE 登记簿
  —— 而所有守门都禁止 `sku_codec` 之外的 UPDATE。本批次的缓解是:`rolled_back` 弃的是
  **新码**(免费),旧码只在 `confirmed` 时才弃;**confirmed 之后要撤销只能靠人工 +
  一次反向 SkuUpdate,这条路径没有代码支持**。
- **`catalog.sku_aliases` 只继承一跳**。设计前提是「旧码改码后立即弃码、永不再改码」,
  没有任何东西**强制**它 —— 靠 `sku_migrations` 的 `(store, old_sku) WHERE status='pending'`
  唯一索引与候选 SQL 的 `NOT EXISTS … status IN ('pending','confirmed','stalled')` 两道软闸。
  若将来允许连改两次,视图必须改成递归 CTE,否则五处历史判据在第二跳静默断链。
- **「落库未提交」的行没有自动出路**:`mint` + pending 台账已 commit、feed 却没发出去
  (进程死在 POST 前后)时,那条行既不进定案判据面(`submitted_at` 为空),又让节奏闸
  永远看见 pending ⇒ 整店发不出下一批。这是**有意的**(判不准就判活),摘要每轮点名并
  给出人工核的两步(`feed_poll` 落定 `ops.feed_log` → 后台看 Product ID 现挂哪个 SKU),
  但**收尾动作没有代码路径**,要人裸改台账。所有者若要自动化,得先给出「怎么判定它
  确实没发出去」的口径。
- **跟卖默认不迁 ⇒ 仓内长期并存两种码形态**(不透明码 + PHUMWMT 串)。这不是缺陷,
  但会让「按形态分流」的直觉失效:形态判断一律走 `sku_codec.is_opaque` /
  `OPAQUE_SQL_PREDICATE` / 登记簿,不许有人写第二处正则(守门钉住)。
- **最贵的一条**:改码生效有 15 分钟到 4 小时的窗口,窗口内旧码可能被观测成非
  PUBLISHED 且未缺席,正好落进 `problem_scan` 的扫描面被建议 DELETE_ITEM —— 一次成功的
  改码被自己的自动链当场永久删掉。止损全靠第二块那条 `NOT EXISTS`,它**必须先于任何
  一次真跑合并**,并且有反向守门测试钉住。
