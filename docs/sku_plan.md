# SKU 编码规则:货源隐匿 + 多源共存 —— 影响范围全景与整体计划

> 状态:**批次 0a/0b/1/2/3 已全部实现(分支 claude/project-overview-dk0y54,PR #104),待所有者验收**;验收清单 §8.1。
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
| `workflows/sources_backfill.py:46/66/90` | 新 SKU 全判 unknown;"非零即报警"语义作废 | ~~摘要分三桶(amz / 旧格式存量 / 新码漏登记)~~ →**2026-09-06 所有者定稿:改成「只登记不猜」**,判型正则与三桶全删,未登记行一律登 `unknown`+`source_key=NULL`,摘要每轮报「本轮新增 N|累计 unknown 待归类 M」,人工归类走 `sources_reclassify`【已闭合】|
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
  最近一次弃码之后的提交;无弃码事件则跨码累计);24h 冷却从 sku_locked_heal
  自管泛化为 list_new 闸门(常量单一出处,官方无明文按旧实证保留)。
  ⚠ **代际上限**(同 (store, source_type, source_key) 弃码行数 ≥ 3 ⇒ list_new
  写 N「换码次数达上限,待人工」)—— **已删除**(所有者 2026-09-06:上架失败
  以 feed 报错为准优化上架方法,不设代数上限;代价是反复 SKU_LOCKED 的品每个
  冷却期烧一个 UPC)。`MAX_SKU_GENERATIONS`、`_SQL_ABANDONED_GEN`、`over_gen`
  字段、`listing_sources_abandoned_idx` 全部删净(§7 批次 2 那几行是**当时**
  的交付记录,不是现状);存量库里的索引由所有者手动 DROP,见
  `docs/db_schema.md`。守门 `test_generation_cap_is_gone_root_and_branch`。
  ⚠ 别与另外三个带"代际"的东西混:`_SQL_ATTEMPTS` 的**代际口径**、退役冷却闸、
  视图 `catalog.sku_aliases` 的**代际继承** —— 三个都在,与上限无关。
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

`problem_scan` 的扫描面曾是"一切非 PUBLISHED 且未缺席",没有 lifecycle 豁免;退役
item 的观测形态正是 UNPUBLISHED +「end date has passed」。所以 product_clear
停用一个品,**一到两轮后它就会被自动链当问题商品 DELETE 掉**,"停用可恢复"在本
系统里只是个窗口(**决策 A**;**2026-09-06 已定稿 RETIRED 全豁免**,见 §8 与 §9.11,
本段留作定稿前的事实记录)。同理,若 0 库存真会触发 UNPUBLISHED(reason=
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
`replaced_skus`;`upc_pool.retag_sku`;~~`mp_mapper.build_sku_update_item`~~ 与
~~`mp_conform` 放行系统开关(决策 I)~~ **2026-09-06 随通道定案删除**(§9.12),
改码载荷改走 `match_feed.build_match_item`;`ORDERABLE_SYSTEM_FIELDS` 仍登记
SkuUpdate(只为挡住 LLM);
`order_lines.duplicate_po_lines` 退化成读视图的薄壳。**api/feeds 零代码改动**(两个
feedType 与两个桶都已收录,只补注释与测试)。

第二块(commit `5565691`)**观测侧抑制/继承**:改码期间不记假代际(`diff_catalog` 对
新码首次被扫到不记 `item_appeared`)、旧码不记缺席(`mark_missing` 照标 `missing_since`
但不记 `item_missing`)、`problem_scan` 的扫描面排除在途改码旧码 + 顽固/归类/WFS/在途
四段判据经 `catalog.sku_aliases` **继承一跳**、`alloc_survey` 销量归属同样继承(决策 H)、
`sku_migrate` 的回执不进病历也不反哺黑名单;`dispositions.open_executing_count` /
`rekey_open`、`walmart_catalog.drop_node_rows` 两个积木。

第三块(本次)**工作流** `workflows/sku_migrate.py`(DANGEROUS=True、SUPPORTS_STORE=True、
`-p store=` **必填**、**永不进调度**):

- **判据表**(定案只信观测,回执成功单独不定案;2026-09-07 起 `double` 也是一个持久状态):

  | 判词 | 证据组合 | 后果 |
  |---|---|---|
  | `confirmed` | 新码在架 ∧ 旧码缺席 | 旧行 `abandon('sku_update')`(**不烧 UPC**)+ 新码记 `sku_replaced` + `upc_pool.retag_sku` + `dispositions.rekey_open` + `drop_node_rows` + 台账 confirmed |
  | `confirmed`(影子) | 新码在架 ∧ 旧码**也**在架 ∧ **两码 wpid 相同** ∧ **旧码单查 404**(`api.items.get_item`)| 判词 (a′)「影子双挂」(2026-09-08 所有者实证,见 §9.15):列表接口把已删档案照旧吐回(僵尸列表,backlog §十三),同 wpid 说明这两个码是同一条 listing、原地换码已生效。**后果与上一行逐字相同,不新增写动作**;两条证据缺一不可,探不出来(凭证/GET 失败)一律 fail-closed 判 `double` |
  | `rolled_back` | 回执 `failed`;或**观测新鲜**且新码超 `OBSERVE_HOURS`(24h)仍未出现;或 POST 当场判 failed | 旧行 `replaced_by` 清空(复活)+ 新码 `abandon('sku_update_failed')` + 台账 rolled_back。**不自动补交**(写操作永不自动兜底);下一轮重来会抽新码 |
  | `stalled` | 超 `STALE_HOURS`(72h)仍判不出 | 只落台账 + 摘要点名人工,**不自动定案**(判不准就判活:回滚一个其实已生效的改码 = 登记簿说旧码、沃尔玛说新码,而且不报错) |
  | `double` | 新码在架 ∧ 旧码**也**在架 | **同店双挂**:台账落持久状态 `double`(2026-09-07 所有者定稿,见 §9.14;此前是「留 pending 只告警」),摘要逐条 + **首行**点名,**不自动处置**。它**不进节奏闸的 open**(后续改码照发)、**不许再开第二条台账**(候选判据含 `'double'`),但每轮仍参与定案 —— 旧码哪天缺席就自动转 `confirmed`。身份层不动 |
  | (不定案) | POST `outcome=unknown` | **保持 pending 不回滚**(决策 F) |
  | (不定案) | pending 但 `submitted_at` 为空(**落库未提交**) | 进程死在 POST 前后、或提交当场抛异常。**只点名不自动定案**:从台账上分不出「确定没发」与「不知道到没到」。人工核:先 `feed_poll` 让 `ops.feed_log` 那条落定,再去后台看这个 Product ID 现在挂的是哪个 SKU |

- **节奏硬闸**(`_stage_cap`,把口头节奏变成代码):该店还有 pending/stalled ⇒ 本轮
  上限 0(只定案不提交;**double 不计** —— 2026-09-07 所有者定稿,见 §9.14);
  零 confirmed ⇒ 1;<10 ⇒ 10;≥10 ⇒ 按 `-p limit`。
  **`-p limit=` 只能收紧**;~~再叠一层配额留量硬顶 `FEEDS_PER_STORE_PER_RUN × ITEMS_PER_FEED`~~
  **2026-09-07 删除**(所有者纠正,见 §9.12「去掉每轮 1000 条自设上限」):那是形态 A
  时代为 MP_MAINTENANCE 桶自设的,不是官方限制。整店一轮发完,上限只有**速率桶**
  (api 层 15/h)与**切片**(api 层 1000 条/24MB)。
- **选谁改:点名 / 排除**(2026-09-03 所有者要求加;在此之前只能"按店 + 按数量",
  想挑着做只能靠 `ORDER BY w.sku` 的先来后到):
  `-p skus=<逗号分隔的旧 SKU>` 按旧码点名、`-p asins=<逗号分隔>` 按登记簿 `source_key`
  (= ASIN)点名,**两个可同时给、取并集**;`-p exclude_skus=` / `-p exclude_asins=`
  从候选里排除,**排除优先**(既点名又排除 ⇒ 排除)。分隔符逗号/空白/换行都认,
  去重保序,大小写**不动**(SKU 大小写敏感,口径同 `services/order_lines.norm_sku`)。
  · 四个参数都是**同一条候选 SQL 的参数化条件**(`_PICK` / `_DROP`),不为点名另开
    第二条选取路径(§六 双轨禁止:两条一漂,点名跑的就不再是全量跑的那套闸);
    没点名时 `unnamed=true` ⇒ 与加这个功能之前**逐字等价**。
  · **点名不越闸**:五道前置闸、逐候选在途闸、节奏硬闸一条都不放松 —— 点了 5 个而
    本轮只放 1 个是常态,摘要首行会说全("点名 5 个,命中 1 个(节奏闸本轮只放 1 个,
    其余下轮)")。
  · 点名了却没进候选面的**逐条给理由**(`_pick_report`:不满足哪一条判据 / 被排除 /
    在途 feed / Product ID 撞号 / 被节奏闸留到下轮 / 店下查无此行),一个都不省略。
    静默丢的表现是摘要看起来像"这家店没候选",而所有者以为自己点的名生效了。
    理由的判据来自 `_SQL_WHY`,与候选 SQL **共用同一份 `_CONDS` 文本**(七条判据只有
    一个出生地),所以不可能出现"摘要说它满足条件、可它就是不在候选面上"。
- **五道整店前置闸 + 一道逐候选闸**:在营 / 目录水位新鲜 / 该店 `executing` 处置为 0 /
  `retire_cooldown` 无 pending / 本工作流无在途 feed;逐候选再看旧码上有没有 48h 内的
  在途 feed(不整店拦,跳过并点名)。任一不过**不抛异常**,在摘要里点名"为什么不能改"。
- **事务边界**:`mint_replacement` + pending 台账在一个事务里写完并 **commit**,
  **之后**才组载荷、才 POST。在未提交事务里 POST = 进程一死就是"沃尔玛已受理、我们
  这边零记录"的孤儿码。
- **dry-run 三纪律**:`_settle` 与 `_migrate` **都**零写(不 mint、不定案、不提交、
  不改处置、不删节点库存、不回写库存),摘要用占位码打印将改的前 N 行与载荷样例;
  `🧪 [DRY-RUN]` 前缀拼在**首行行首**(cli 的链通知只取首行)。
- ~~**上架表 SKU 列回写在事务之外**,写成功才盖 `sheet_synced_at`;每轮开头先补写
  `status='confirmed' AND sheet_synced_at IS NULL` 的行~~ **已整段删除**
  (2026-09-06 所有者定稿,见 §9.12「改码不回写上架表」):身份映射的出口是登记簿
  `catalog.listing_sources` + 在线产品总表「来源码」列,上架表 SKU 列只由上架链写。
  台账列 `sheet_synced_at` **保留但不再写**(恒 NULL,只为不动存量库)。
- ~~**形态 A**(决策 E 默认):`FEED_TYPE = "MP_MAINTENANCE"` + `build_sku_update_item`~~
  **已作废(2026-09-05 §9.10)**。**现行通道:`FEED_TYPE = "MP_ITEM_MATCH"`**
  (2026-09-06 §9.12),载荷由 `_item_of` → `match_feed.build_match_item` 组:
  同 GTIN + 新 SKU + `processMode=REPLACE` 在同一 item 上原地换码,**没有 SkuUpdate**。
  换通道只改 `FEED_TYPE` 与 `_build_items`/`_item_of` 两处;**不提供参数覆盖**(双轨禁止)。
  ⚠ REPLACE 会覆盖载荷里给到的每个字段 ⇒ `price` 必须发**现挂价**(= 不改价),
  采不到现价的行由 `_CONDS` 的「有现挂价格」剔掉;`ShippingWeight` 则**有意重写**
  (2026-09-06 晚定稿,见 §9.12「改码顺便把重量统一到准确值」):按
  `mp_mapper.shipping_weight_ex` 的新口径解析,解析不出或 > 11 磅写 1 磅。

**受牵连的 (store, sku) 键表 —— 逐条结论**(2026-09-02 复核,结论与依据都留下,
免得下一轮盘点又把它们当待办):

| 键 | 结论 | 依据 |
|---|---|---|
| `ops.dispositions` 未落定建议 | **迁**(`rekey_open`,撞唯一索引的动作不迁不删、点名人工) | 建议是"这个 item 怎么处置",item 没变、只是身份列换了 |
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
- [x] **决策 A|停用要不要成为真正可恢复态**(批次 2 按默认实现:**RETIRE 不弃码**,
      守门反向钉死 `product_clear` 不得调 abandon)—— **2026-09-06 所有者定稿:
      problem_scan 对 lifecycle=RETIRED 全豁免**(比原提案的「且本仓提交过
      retire_submitted」更宽:不看谁退的,RETIRED 就不扫;NULL 不豁免)。落地
      `workflows/problem_scan._SQL_ITEMS`,守门测试钉住。定稿依据不是"可恢复",
      而是**实证删不掉**:08-28 可见性变更翻回来的 10,191 行 RETIRED 死档,DELETE_ITEM
      反复回 deleted/retired 类失败,只烧 MP_MAINTENANCE 配额;后台一般也不显示。
      副作用即原提案想要的:product_clear 停用的品从此不再被下一轮 problem_scan 建议
      删除,「可恢复窗口」不再只是一轮 —— `workflows/product_clear.py` 头注措辞待同步
      (见 §9.11 转出)。
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
- [x] **决策 E|改码走哪条 feed 通道** —— **2026-09-06 定案:`MP_ITEM_MATCH`**
      (`sku_migrate.FEED_TYPE`,依据 §9.12 所有者 Seller Center 实测)。
      原提的两个形态**都作废**:A(MP_MAINTENANCE 最小载荷 + SkuUpdate)被官方
      spec 原件证伪(§9.10),B(MP_ITEM 全量 + SkuUpdate)从未有过实证、而且要
      重发全部内容(标题/属性被覆盖)、还吃 list_new 的 MP_ITEM 桶。定案的通道
      不用 SkuUpdate:MP_ITEM_MATCH 按「**同 GTIN + 新 SKU + REPLACE**」在同一个
      item 上**原地换码**(wpid 不变、库存跟着过来、价格不变),换码是 REPLACE 的
      机械后果,不是一个开关。配额吃 `feeds.post.MP_ITEM_MATCH`(15/h,与跟卖链
      共享),不再与 13:00 的维护链抢 MP_MAINTENANCE。
      **未决(不阻塞)**:仓里的 MP_ITEM_MATCH 是 v4.2(sellingChannel header),
      所有者用的模板是 v5.0;v4.2 能否同样换码由第一级投放(limit=1)实测定,
      **不另写探针、不加参数开关**。
- [ ] **决策 H|改码后的历史销量归属**:经 `catalog.sku_aliases` 映射(已实现),
      还是把聚合键整体改成 ASIN(更根本,但要改三个消费点的键形状,属另一个批次)。
- [x] **决策 I|形态 B 下 SkuUpdate 如何穿过 `mp_conform.strip_unknown`** ——
      **2026-09-06 作废**(决策 E 定案 MP_ITEM_MATCH,载荷里根本没有 SkuUpdate)。
      落地:`mp_conform.ORDERABLE_SYSTEM_SWITCHES` 那条放行分支、
      `mp_mapper.build_sku_update_item`、`mp_mapper.build_orderable(sku_update=)`
      **三处一并删除** —— 留着就是第二条改码路径(§六 双轨禁止),而且删了放行
      分支之后它还是**不报错**的那一条(传了会被 strip 掉 ⇒ 改码退化成普通上架 ⇒
      同店双挂,回执全绿)。`SkuUpdate` 仍留在 `mp_mapper.ORDERABLE_SYSTEM_FIELDS`
      里,职责只剩挡住 LLM 往 Orderable 塞它;反向守门在
      `tests/test_mp_conform.py::test_sku_update_is_stripped_like_any_other_unknown_field`。
- [x] **沃尔玛 SKU 规格** —— 2026-09-06 由「Match items」模板给出:
      **Alphanumeric, 50 characters**(12 位不透明码远在限内,字符集也在内)。
- [x] **飞书建列**(所有者 2026-09-02 已建):上架表 R「SKU」;销售订单「来源码」;
      售后订单「来源码」;在线产品总表 Q「来源码」。统一叫「来源码」。
      【0b:代码分第二个 PR,建完列再合 —— 建列前程序载荷里没有这一列,
      零 WARNING 零重推;合并后第一次 push 全量重推一次是预告不是故障】
- [ ] **UPC 池表 E 列「SKU」口径**:改存真 SKU(ASIN 另列)还是保持现状。
      【默认 (a) 不加列:E 列 = `catalog.upc_pool.sku` 的投影,批次 2 起显示真码;
      领号复用键仍是 `upc_pool.asin`。烧号新增两个状态文案「删除烧号/锁死烧号」】
- [x] **四个来源字母**(所有者 2026-09-02):amz=A、跟卖 match=B、1688=C、自建 self=H。
- [x] **存量 unknown 行的人工归类**(所有者 2026-09-03 提出,**已实现**):
      **⚠ 2026-09-06 起这不再是"存量清尾",而是常规路径**:所有者定稿
      `sources_backfill` **只登记不猜**(判型正则与三桶全删,schema.sql 的存量
      回填 INSERT 同日删除),于是**每一条新出现的未登记在架行都落在 unknown**,
      唯一出路就是人工经 `sources_reclassify` 归类;backfill 摘要每轮都报
      「累计 unknown 待归类 M 行」,就是为了让这条队列不会没人看。
      `sources_backfill` 把 `CMSQ-B0CLCX3Q1Z-169.99`(应为 `B0CLCX3Q1Z`)、
      `B0822D9QQKS59`(应为 `B0822D9QQK`)这类非标准形态一律登记成
      `source_type='unknown'` + `source_key=NULL`,它们按路由铁律**被排除在全部
      自动维护之外**,也进不了 `sku_migrate` 的候选。新增 `workflows/sources_reclassify`
      (缺省预览+导出待归类 csv → 人核 → `-p file=` 读回 → `-p apply=1` 才写,
      `-p overwrite=1` 才盖已有键)与积木 `services/listing_sources.reclassify`
      (**全仓唯一一条 UPDATE source_type / source_key 的路径**,写入前过
      `is_standard_asin`)。机器提议只用已有规则:三段式走 `extract_asin`;
      「标准 ASIN + 尾巴」只标 `guess`、**不预填确认列**(它与"11~15 位的真源头
      码"形态相同,机器分不开)。⚠ 归类 = 把商品**交还自动链**,真跑后先
      `maintenance_scan --dry-run` 看破坏面(与 backfill 同款纪律)。
      **两件已结(2026-09-03 生产实跑)**:① 待归类 **2724 行**,形态分布
      三段式 1214 / 裸 ASIN 1 / 纯数字 item id 15 / 其他 1494;其中「其他」桶的
      主力是 `CMSQ--B07J2QNQCF-43` 这一支(前缀与码之间**两个横杠**),已把
      `sku_asin._WRAPPED` 的分隔符放宽成 `-{1,2}` 收进提取档(反向用例钉住
      三横杠/缺价格段/中段数字开头仍提不出);② guess 那一档所有者**逐行认了**,
      最终归类 **2716 行(amz 2578 + self 138)**。
      **归类支持四种来源类型(2026-09-03 补,所有者当场问出的口子)**:
      reclassify 此前写死 `source_type='amz'` —— 一个 1688/自建的品只要填了个
      形态合法的 ASIN 就被登记成搬运品,价格/标题/库存从此跟着某个亚马逊页面走,
      断货窗口一到还会被建议**永久删除**,全程不报错。现清单加「确认来源类型」列
      (amz/match/1688/self,`unknown` 显式拒收),键闸按类型分
      (`listing_sources.RECLASSIFY_TYPES` / `source_key_ok`:amz=标准 ASIN、
      match=GTIN 8~14 位、1688/self=非空货号),摘要每次报类型分布与
      「其中 N 行归成 amz」。缺列按 amz 算但**必须在摘要里喊出来**。
      ⚠ **破坏面是延后打开的**(所有者 2026-09-03 指出):归类当天绝大多数行
      看不到动静,因为那批 ASIN 从没被采集过,`latest_snapshot` 的 LATERAL 取不到
      行 ⇒ 三个 provider 算不出差异。等采集补上后才陆续生效;`连续无货15天`
      短期烧不到它们(采集历史不足),但 `not_found` 与 `variant_offset` 是
      **采到就能立刻判**的 —— 采集跟上后的第一轮 `maintenance_scan --dry-run`
      要再看一次删除段。
      **归类当天的破坏面基线**:全库 `maintenance_scan --dry-run` 删除 380 条
      (not_found×54 / variant_offset×286 / 渠道不符15天×17 / 连续无货15天×23);
      `ops.dispositions` 近十天的删除建议区间是 91~360(09-01=360、08-28=356),
      380 在上沿、非数量级失控。单店对照(谭总11)归类前后**删除同为 16**,
      标题/价格/库存各 +4。
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
- [x] **`variant_group.group_id` 仍把 ASIN 递给沃尔玛**(批次 3 评审转出的目标级
      漏洞):改码把 SKU 里的 ASIN 摘掉了,变体组 ID 这条线还在往外递。
      **2026-09-07 所有者定稿三条并实现,见 §9.13** —— 组号改不透明码、身份过新登记表
      `catalog.variant_groups`、存量组不回改。余生产验收(变体家族)。

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
# ~~已定案的行必须都回写过上架表 SKU 列(非空);为空的下一轮会自动补写~~
# ⛔ **本条作废**(2026-09-06 所有者定稿:改码不回写上架表,见 §9.12)——
#    sheet_synced_at 现在**恒 NULL**,非空只可能是 2026-09-06 之前的历史行。
#    要核"新码与来源码的对应关系",看登记簿与在线产品总表「来源码」列:
psql "$DSN" -c "SELECT sku, source_type, source_key, replaced_by FROM catalog.listing_sources WHERE store='<试点店>' AND sku='<新码>';"
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
#   要**挑着做**(不想按 SKU 升序碰运气)就点名 —— 点名不越闸,该压到 1 还是 1:
python cli.py sku_migrate --dry-run -p store=<试点店> -p skus=<旧SKU1>,<旧SKU2>
python cli.py sku_migrate --dry-run -p store=<试点店> -p asins=<ASIN1>,<ASIN2>
#   人眼确认:摘要首行的「点名 N 个,命中 H 个」,以及落选那几条的**逐条理由**
python cli.py sku_migrate -p store=<试点店> -p limit=1  # 第一级:1 个品(节奏闸会把任何 limit 压到 1)
python cli.py sku_migrate -p store=<试点店> -p skus=<旧SKU>   # 第一级也可以点名改哪一个
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
python cli.py sku_migrate -p store=<试点店> -p limit=100000   # 第三级:整店(**一轮发完**)
#   3371 个品 = 4 个 feed(1000+1000+1000+371),15/h 桶内一轮发完;**没有**每轮 1000 条
#   的自设上限(2026-09-07 所有者纠正,§9.12),跨过速率桶时会在 api 层等
#   整店时要留几个不动(比如正在做活动的品):-p exclude_skus= / -p exclude_asins=(排除优先)
python cli.py sku_migrate -p store=<试点店> -p limit=100000 -p exclude_asins=<不改的ASIN>
```

**④ 运行纪律**:改码吃 `feeds.post.MP_ITEM_MATCH` 桶(15/h),**与跟卖链
`match_listing` 共享**(2026-09-06 通道定案后不再与 13:00 的维护链抢 MP_MAINTENANCE)
—— 跟卖真跑那天别并跑;
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
| E|改码走哪条 feed 通道 | ~~A(MP_MAINTENANCE 最小载荷)~~ 与 ~~B(MP_ITEM 全量)~~ **两个形态都作废**;**2026-09-06 定案 `FEED_TYPE = "MP_ITEM_MATCH"`**(所有者 Seller Center 实测,同 GTIN + 新 SKU + REPLACE 原地换码,载荷无 SkuUpdate;§9.12) | ☑ |
| F|POST `outcome=unknown` 是否回滚 | **不回滚,保持 pending**(见 9.1-1) | ☐ |
| G|`cleanup_seen_categories` / `ops.dedupe` / `catalog.claims` 是否随改码迁 | **三者都不迁**(逐条依据见 §7 批次 3 的键表) | ☐ |
| H|改码后历史销量归属 | **经 `catalog.sku_aliases` 映射**,返回键形状不变、三个消费点一字不改 | ☐ |
| I|形态 B 下 SkuUpdate 如何穿过 `strip_unknown` | **2026-09-06 作废**(决策 E 定案 MP_ITEM_MATCH,载荷里没有 SkuUpdate)。`ORDERABLE_SYSTEM_SWITCHES` 放行分支与 `build_sku_update_item` / `build_orderable(sku_update=)` **三处一并删除**,反向守门钉住「没有开关字段放行名单」;§9.12 | ☑ |

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

### 9.5 生产 A/B 验收实测(2026-09-03):「零行为变化」的唯一已知例外

批次 0a 对外承诺的是「身份表达式换成 `coalesce(ls.source_key, w.sku)` 之后逐行
等价」。生产实跑 `maintenance_scan --dry-run -p preview=1 -p store=谭总11` 做
main ⇄ 本分支对照,**意图数 343 → 351,多出 8 条**(标题 +3、价格 +3、库存 +2;
删除两边都是 16)。逐条查清后确认:**多出来的 8 条是收口买到的东西,不是回归。**

- **成因**:main 的 `_SQL_AMZ_JOIN` 是 `p.asin = w.sku` 裸接。存量里有一批
  `source_type='amz' AND source_key <> sku` 的行(全库 318 条,`workflow=backfill`,
  成因是旧回填正则缺右锚 + `left(sku,10)`,形如 `B0FYWJH5M4S50` → `B0FYWJH5M4`、
  `CMSQ-B0CLCX3Q1Z-169.99` → `B0CLCX3Q1Z`)。这些行在 main 上接不到
  `catalog.products` 任何一行 ⇒ **整行从 JOIN 掉出去,维护链对它们永久失明**
  (不改价、不改标题、不清零、也不回补)。本分支经登记簿接上了真产品。
- **算术**:谭总11 有 **3 行**属于这一类(SQL 核实:JOIN 得上 `source_key`、
  JOIN 不上裸 `sku`)。3 行 × (标题 + 价格) = 6,其中 2 行库存也不一致
  (0→30、30→23;第 3 行 10→10 一致不产)= 2。合计 **+8**,与 diff 一字不差。
- **删除为什么不变**:删除走 `_SQL_VARIANT_OFFSET` / `_SQL_LONG_OOS` / 观测核验,
  判据是多天窗口;这 3 行 `outcome=ok`、`In Stock`,接上了也不到删除档。
- **顺带的业务收益**:`B0FYWJH5M4S50` 在沃尔玛挂着 0 库存、amz 有 30 件,在 main 上
  永远回不了血;合并后第一轮维护就会把它顶回去。这 318 行里的同类会自行恢复。

排查中被**证伪**的两条解释,记下来免得复查时重走:
① 「两次运行之间采集快照变了」—— 同分支间隔 30 秒连跑两次,除时间戳外逐字节相同
   (`压制 76 条` / `77 行` / `压掉 544 条` 全一致),数据在该窗口内是稳的;
② 「`only` 下推改变了压制/截断口径」—— `cap_per_store` 按 `(store, kind)` 分组、
   `doomed` 是 `(store, sku)` 精确对、`drop_recent` 按 `_suppress_key` 逐键匹配,
   三者都没有跨店依赖,先全量算再 Python 过滤与只算单店同集合。
   全店计数器(`压掉 12419` → `544`、`title_mismatch 1870` → `76`)的差异是下推的
   **预期**结果:main 那几行统计的是全库、本分支统计的是本店。

### 9.6 生产试点实录(2026-09-04):第一个不透明码上线

**批次 2 的新码上架试点通过**,`python cli.py list_new -p store=谭总12 -p limit=1`。
本仓第一个真上生产的不透明 SKU:**`AACSVCEH397R`**(ASIN `B0BWMVQHVJ`,
feed `18D215093E3B5EE8B5028024A6ED9780@AXkBBgA`)。

四处落地逐条核过,**三张表对同一个品说同一件事**:

| 落点 | 值 | 要看的点 |
|---|---|---|
| `catalog.listing_sources` | `sku=AACSVCEH397R` `source_type=amz` `source_key=B0BWMVQHVJ` `workflow=list_new`,`abandoned_at`/`replaced_by` 均空 | **`source_key` 是 ASIN 不是新码** —— 新码上线后唯一能反查回亚马逊的地方,而它只在我们库里 |
| 上架表 | C=`AACSVCEH397R`,M=Yes,N=feedId,O=2026-09-04(**同一次落地**) | 没出现「已提交但无码」;T 登记日期 / U 查询编码 仍空 ⇒ 程序确实不碰人工列 |
| `ops.feed_log` / `ops.feed_items` | `MP_ITEM` / `submitted`;SKU 级台账记的是新码 | 台账里不再有 ASIN |
| `catalog.upc_pool` | `sku=AACSVCEH397R` `upc=600819136176` `status=used` `asin=B0BWMVQHVJ` | **复用键仍是 `asin`,`sku` 列已是真码** —— 决策 E 默认 (a) 口径的生产实证 |

码形态合规:12 位、首位 `A`(amz)、其余 11 位全在 `23456789ABCDEFGHJKMNPQRSTVWXYZ` 内,
无 0/O、1/I/L、U。**沃尔玛侧已无法从 SKU 反查货源** —— 立项目标在这一行上达成。

同轮闭环一并验过:5 候选推采集 → 0 分钟落定 → 按批摄取 5 → 身份更新 5 →
预备期 1/1 通过 → 提交 1 条;LLM 零调用(二级复用旧出参)。

**顺带纠正 §5.4 的一条记录**:所有者 2026-09-04 上传的 US 官方 Item spec
(`specs/MP_ITEM/5.0.20260608-18_15_07-api/_orderable.json`)**含 `SkuUpdate` 字段**,
描述原文「allows the replacement of a previously submitted SKU associated with a
standardized unique identifier (i.e. GTIN, ISBN, UPC, EAN)」——此前蓝图记的是
「US 侧未点名 SkuUpdate」,现已有 US 官方 schema 出处。按蓝图既定判据
(§遗留问题 6:「以 Get Spec 拉回的 schema 是否含相应字段为准」)**形态 A 的依据成立**。
两条旁证:① `required` 里含 `country_of_origin_substantial_transformation`,而官方明写
MP_MAINTENANCE **不可改 COO** ⇒ 该 `required` 对 MP_MAINTENANCE 不生效,partial update
复用同一份 schema、放宽必填;② 本仓只下载 MP_ITEM 一份 spec bundle
(`registry/paths.mp_item_spec_dir()`、`workflows/spec_split`),**不存在
`specs/MP_MAINTENANCE/` 目录** —— 原验收清单里那条 `grep -rl SkuUpdate
"$SPECS/MP_MAINTENANCE/"` 天然返回空,**测法本身无效**,不能据它判形态 B。
⚠ 这仍是 schema 层面的推断:真正的实测是 `sku_migrate` 发一个真 MP_MAINTENANCE feed
并由观测定案 —— 即「六件实测第 1、2 件」与「批次 3 第一级投放」是**同一件事**。

### 9.7 候选面补第八条判据「已上架」(2026-09-04,所有者提)

所有者问「到时候我更新 sku,只对 publish 的产品发就可以了吧」—— 对,而且原来的
七条判据里**没有**这一条:`w.missing_since IS NULL` 只说"目录里还看得见",
UNPUBLISHED / RETIRED / STAGE 全都满足它,于是它们本来都会进候选面。

**为什么必须加**(两条,都不是理论风险):
① §4 六件实测的第 5 件正是「对 `lifecycle=RETIRED` 的 item 是否可用 SkuUpdate」——
   官方零文档、本仓零实证。改不动的话那条行卡到 `STALE_HOURS=72` 才落 stalled,
   期间一直占着 `_stage_cap` 的名额(零 confirmed ⇒ 上限 1,一条卡住 = 整店停摆)。
② 更贵的一条(§9.4「最贵的」那条的变体):改码生效有 15 分钟~4 小时窗口,窗口内
   旧码「非 PUBLISHED 且未缺席」,正好落进 `problem_scan` 的扫描面被建议
   DELETE_ITEM —— 一次改码把商品永久删掉。PUBLISHED 的行不在那条扫描面上。

第八条判据(`_CONDS` 单一出处,选取与解释两处同源):
`("已上架", …, "w.published_status = 'PUBLISHED'")`。

⚠ **不加参数开关放开它**:那样就是两条口径,而"哪些状态能改码"是判据不是偏好
(§六 双轨禁止)。要迁非 PUBLISHED 的行,先做第 5 件实测,再改这一条判据本身。
两条守门测试钉住:判据在两条 SQL 里逐字同源;点名一个非 PUBLISHED 的旧码时
**逐条说得出人话**,不许静默消失。

### 9.8 闸③从「整店拦截」改为「逐候选拦截」(2026-09-04,所有者复议)

所有者的生产事实:「每天每个店都有不少的在途 feed。特别是 13:00 最密集,价格、
库存、标题都是这个时候调整。但是其实不影响 —— 在晚上处理,第二天 13:00 会拉取
最新的后台数据,这时无论拉到的是新 sku 还是原来的 sku,都可以在库中找到 asin
并且更新对应数据。所以『一定要没有在途 feed』这个约束无效,真正要确认的是,
**我要修改的这个产品,他不能还有 feed 在跑**。」

**这条复议成立,而且代码里的注释自己就承认了**:`dispositions.open_executing_count`
的头注写的是「改码会把**它等的那个 (店, SKU)** 键换掉」—— 危害逐 (店,SKU),
而闸③却按整店计数拦,是粗放的过近似。判据侧也确实没有任何跨 SKU 依赖:
`settle` / `settle_maintenance` / `expire_executing` 全部逐 (店,SKU) 判。
按整店拦的实际后果是**改码永远开不了工**(生产实测:谭总12 829 条、
A085朱丽霖 899 条 executing,而这是 13:00 三条链齐发之后的常态)。

改法(两处,合起来**比原来更严**,只是严在该严的那一个品上):

| 位置 | 改前 | 改后 |
|---|---|---|
| `_preflight` 闸③ | 整店 executing 非零 ⇒ 整轮不发 | **只报数不拦**,数字仍进摘要(改码期间这家店有多少条在等判决,人该知道) |
| `_CONDS` 新增第 9 条「无未了结**破坏**建议」 | (无) | 逐候选:该 (店, 旧码) 有未落定的 **delete/retire**(suggested 或 executing)⇒ **不进候选面**,并逐条点名说明 |

**为什么逐候选那条要连 `suggested` 一起拦**(比原闸更严):
① `executing` 那条等的是 (店, 旧码) 的观测判决 —— 改码后旧码从目录里消失,而一条
   等着判决的 **DELETE** 建议会把"这个 SKU 不见了"读成 `delete_verified` =
   **删除成功**。那是一次**假确认**,而且全程不报错;
② `suggested` 那条马上会被 `claim` 成一条打在**旧码**上的 feed,而那时旧码可能
   已经不在了。原来的整店闸只看 executing,漏了这一档。

守门:三条测试(整店只报数不拦 / 逐候选判据的形状与两处同源 / 点名一个有未了结
处置的旧码要逐条说得出人话);另两条原本靠 executing 制造"闸未过"的用例改用闸④。

### 9.9 判据只拦破坏组(2026-09-04 当天二次收窄,生产实测触发)

9.8 的第一版把 `suggested`/`executing` **不分动作**一起拦。生产实测当场证伪:
所有者点名的两个品(谭总12/`B008LUW4CI`、A085朱丽霖/`B0000C8W8W`)挂的都是
`action='price'`、`source='maint'`、14:03 提交的改价 feed —— 正是 13:00 那批。
按"不分动作"拦,几乎所有品在多数时间里都改不了码。

查清三条事实之后收窄成**只拦破坏组**(`dispositions.DESTRUCTIVE_ACTIONS`):

| 档 | 改码后的下场 | 判断 |
|---|---|---|
| **破坏组 suggested** | 马上被 `claim` 成一条打在**旧码**上的 DELETE feed | **拦** |
| **破坏组 executing** | `settle` 的判据是 `delete_verified`(= 这个 SKU 不见了);改码后旧码正好消失 ⇒ **假确认**:商品还挂着,账本记「已确认删除」,不报错 | **拦** |
| 维护组 suggested | 定案时 `rekey_open` 把键从旧码搬到新码 —— 本来就有出路 | 不拦 |
| 维护组 executing | ~~rekey **故意不碰**(搬键 = 换判决对象)⇒ 滞留在旧码上 ⇒ `expire_executing` 判成 `ineffective` 收尾~~ **2026-09-08 改口(§9.15)**:同 wpid 的原地换码下,新码的现值就是那条 feed 作用的对象 ⇒ **一并迁到新码**,由 `settle_maintenance` 按新码观测落定(`expire_executing` 仍是最终兜底)| 不拦,但**点名** |

**连带修掉一处已经过期的注释**(这次查证的最大收获):
`services/dispositions.rekey_suggested`(2026-09-08 起改名 `rekey_open`,§9.15)的边界①原文写着「改码的前置闸
(`open_executing_count`)**保证这一刻该店没有 executing 行**,所以这里不需要
分支,只需要不碰」—— 9.8 拆掉那条整店闸之后,这句话已经不成立。现改写为:
保证从**整店**变成**逐候选**,且只覆盖破坏组;维护组的 executing 行确实可能存在,
照旧不碰,但由调用方点名。**注释与代码脱节比代码本身错更难查** —— 下一个人会
照着这句话推理,而它是错的。

**滞留不静默**:新增只读积木 `dispositions.executing_actions_on(conn, store, sku)`,
`_confirm` 在 rekey **之前**调它(rekey 之后 suggested 已搬走就读不到了),把滞留的
动作写进摘要;若滞留里出现破坏组,额外喊「候选判据本该把这行剔掉,多半是中间
窗口里新长出来的建议,请人工核」——`problem_scan._SQL_ITEMS` 的那条 `NOT EXISTS`
(在途改码的旧码不进扫描面)本该挡住它,挡不住就是那条闸出了问题。

### 9.10 决策 E 翻转:形态 A 作废(2026-09-05,官方 spec 原件核实)

**事实**(代理从开发者门户「Item spec: versioning and diff reporting」页直链下载的
官方 spec 原件,本机解析,不是摘要):

| spec | 版本 | `MPItemFeedHeader` | Orderable | `SkuUpdate` |
|---|---|---|---|---|
| **MP_MAINTENANCE** | 5.0.20260608-18_15_07-api(**我们 header 里的那一版**) | required=[businessUnit, locale, version],additionalProperties=false | **20 个属性**,required=[sku, productIdentifiers],additionalProperties=false | **0 次** |
| MP_MAINTENANCE | 5.0.20260501 / 5.0.20260703 | 同上 | 同上 | 0 次 |
| MP_ITEM | 5.0.20260703-18_22_27-api | 同上 | 23 个属性,required 含 price/ShippingWeight/COO | **1 次**(还有 `ProductIdUpdate`) |

三条结论:
① **MP_MAINTENANCE 与 MP_ITEM 是两份独立 spec,Orderable 布局不同**。§9.6 那条
   「schema 含 SkuUpdate ⇒ 形态 A 依据成立」的推断错了 —— 它看的是所有者上传的
   `specs/MP_ITEM/…/_orderable.json`,是 **MP_ITEM** 的 Orderable,不是 MP_MAINTENANCE 的。
   本仓从来只下载 MP_ITEM 一份 bundle(`registry/paths.mp_item_spec_dir()`),
   所以仓内没有任何东西能看出这两份不一样。
② **header 是对的**:三字段封闭校验,与我们发的一字不差;旧仓实证多传 subset 被
   60670554076755 拒收、漏 businessUnit 被 72600149546850 拒收。H8(header 缺
   processMode/sellingChannel)由此排除。
③ 生产实证与 ① 吻合:两条改码 feed(谭总12 `B008LUW4CI→AVZX97DTMTDN`、
   A085朱丽霖 `B0000C8W8W→AG28GAD7MF75`,2026-09-04 21:52)回执 SUCCESS,线上 SKU
   纹丝不动 —— 维护通道把 `SkuUpdate` 当未知字段**静默丢弃**,整条 feed 是一次
   "空维护"。官方 Seller Center 指引(US「Update SKU IDs in bulk」、CA「Update an
   item's SKU」)也一致:改 SKU 用**全量 Item Setup 模板**,「SKU Update」列填 Yes;
   GeekSeller 明言「You will need to provide all required data … not sufficient to
   provide just the data you wish to adjust」。

**所有者的成功路径**:在 Seller Center 用「Match items」按 GTIN 匹配、下载模板、改
SKU 后上传,SKU 变了、UPC 没动。这条路底层是 setup 类 feed(MP_ITEM_MATCH 或
MP_ITEM),具体哪一种由 `GET /v3/feeds` 列出那条 Seller Center feed 的 `feedType`
定案 —— **定了再改 `sku_migrate.FEED_TYPE` 与 `_build_items`**。

**已做的止损**:`workflows/sku_migrate.SUBMIT_DISABLED` 非空 ⇒ 本工作流只定案不提交,
dry-run 也不列候选(列了就是"将改码 N 个"的误导);定案留着,两条 pending 靠
`_verdict` 的观测反证(新码不在架 ∧ 旧码在架,超 OBSERVE_HOURS=24)走 `rolled_back`
把旧码复活、新码弃掉。守门测试钉住"缺省停用"。

**对抗验证顺带排除的**(三名反驳者各自独立驳倒,免得再走弯路):
· H3「我们把回执 SUCCESS 当生效」—— 官方 item 级 ingestionError 只有
  DATA_ERROR/SYSTEM_ERROR/TIMEOUT_ERROR 三型,没有 WARNING/INFO 分级;而且两条链
  本来就不按回执定案(`sku_migrate._verdict` 只信观测,`dispositions.settle_maintenance`
  按线上现值比对并在摘要报「未生效 N」)。
· H4「productIdentifiers 对不上」—— 对不上时沃尔玛是显式拒收(Zentail 引用原文
  "This SKU is already set up with a different Product ID");08-09 首跑 89 条
  0101198 stale update 证明沃尔玛按 SKU+productIdentifiers 找到了 item 并比对了字段。
· H5「Visible 的 PT 键不对」—— 改码载荷根本没有 Visible 段却同样失败;PT 键错的
  结局是整批 DATA_ERROR(60670554076755 同款),不可能 SUCCESS。
· H6/H7(回执解析 / httpx 字节形态)—— 前者只会让 feed 永远停在 submitted,
  后者对纯 ASCII 载荷零解释力且价格 feed 走同一路径正常。

**标题那件事另有原因**:我们的标题载荷与旧仓生产在用的**逐字相同**
(`services/maintenance_intents.build_title_item` ↔ `docs/legacy_survey.md:1089`,
唯一差异是 header version),旧仓同样"SUCCESS 但多数不变、少数会变"。所以它**不是
本次改造引入的回归**,而是一条长期存在的沃尔玛侧行为;根因待所有者机器上的
实例(我们发的标题 / 回执原文 / `GET /v3/items/{sku}` 现在的 productName)定。

### 9.11 决策 A 收口:RETIRED 全豁免(2026-09-06,所有者定稿)

**起因**:2026-09-05 维护链失败画像里最大三组(MP_MAINTENANCE deleted/retired 873、
价格 1259、库存 789 条 not found)全部来自同一批行 —— 08-28 沃尔玛列表接口可见性变更
翻回来的死档(`item_appeared` 按日统计当天尖峰,plan.md 工作流 8 行有记)。所有者
最初记忆"以前拉取时没有这些行、某天突然进库",与记录吻合:不是我们修了什么导致
进库,是沃尔玛那边把已删/已退役的档案重新吐进 `GET /v3/items` 列表(单条 GET 404)。

**两个决定**(所有者原话:「RETIRED 全豁免,僵尸列表无法修,因为你能请求到,且后台
确实可以查到这个产品,暂时不管这个」):
① **problem_scan 对 `lifecycle_status = 'RETIRED'` 全豁免**。库内当日 RETIRED 10,191
   行、非 PUBLISHED 23,318 行、PUBLISHED 70,879 行。落地 `_SQL_ITEMS` 一行条件 +
   守门测试;NULL 不豁免(没采到 ≠ RETIRED,照扫)。这比 §8 决策 A 原提案「RETIRED 且
   本仓提交过 retire_submitted」更宽 —— 定稿理由从"可恢复"换成了"实证删不掉":对这
   批行发 DELETE_ITEM 只会反复回 deleted/retired 类失败,烧配额零效果。
② **僵尸列表暂不处理**:列表接口对已删品仍返回 PUBLISHED/ACTIVE 而单条 GET 404 的
   那批行(六个样本实证),不改 catalog_sync 的缺席判定。它们继续被维护链当在架品
   发价格/库存 feed 并收 not found —— 已知代价,所有者接受;将来要修就在
   catalog_sync 加"列表在、单条 404 ⇒ 标缺席"的二次核验(不是本役)。

**联动改动**:`workflows/product_clear.py` 头注「可恢复窗口」措辞同步(§8 决策 A
转出项收口);plan.md 工作流 8 行、production_cutover §5 各记一句。
`services/problem_products` 归类不动 —— RETIRED 行本来就不再进扫描面,归类无从发生。

### 9.12 改码通道定案:MP_ITEM_MATCH(2026-09-06,所有者 Seller Center 实测)

**事实**(所有者在 Seller Center 亲手做的一次改码,不是文档推断,以此为准):
用「Match items」模板把 A085朱丽霖 的 `B0CRKFQZWF` 改成 `Test851`,feed
`18D2A25BB3895D7096CF1357C17C2A36@AYYBBwA`。

| 项 | 值 |
|---|---|
| 模板头部 | `Version=5.0.20260703-18_22_27,MP_ITEM_MATCH,mp_item_setup_by_match` |
| 行内字段 | specProductType / productId(**GTIN 14 位 `00121678236703`**)/ productIdType=GTIN / productName / sku / condition=New / mainImageUrl / ShippingWeight=**0.82** / price=**29.99** |
| **SkuUpdate** | **没有这个字段** |
| SKU 规格(模板给的) | Alphanumeric, 50 characters |

**探针结果**(改完之后逐条看的):

| 探针 | 结果 | 说明 |
|---|---|---|
| wpid | 新旧码**同为 `5FK5P1SAT7OM`** | 同一个 item,不是新建了一条 |
| 库存 | 5 → 5 跟着过来 | 模板库存列**是空的**,库存却没丢 ⇒ 不是重建 |
| 价格 | 不变 | 模板里发的就是现价 29.99 |
| 旧码 | `GET /v3/items/{旧码}` **404** | 且**不是**所有者手动删的 |
| feed | 1 条 SUCCESS | |

**结论**:MP_ITEM_MATCH 按「**同 GTIN + 新 SKU + REPLACE**」在**同一个 item 上原地
换码**。载荷不需要 `SkuUpdate` —— 换码是 `processMode=REPLACE` 的机械后果,不是一个
开关。这同时把 §9.10 留下的那个岔口("setup 类 feed 是 MP_ITEM 还是 MP_ITEM_MATCH")
关掉了:是 MP_ITEM_MATCH,而且**仓里早就有这条通道**(跟卖链 match_listing 生产在用)。

**切换清单**(2026-09-06 一次改完,不分批):

| # | 改动 | 位置 |
|---|---|---|
| ① | `FEED_TYPE = "MP_ITEM_MATCH"`;`SUBMIT_DISABLED = ""`(**机制保留**:非空即停闸,守门钉「缺省为空」+「非空时 cap=0」) | `workflows/sku_migrate.py` |
| ② | `_build_items` / `_preview` 共用新构造点 `_item_of`,复用 `services/match_feed.build_match_item(None, sku, price, weight, product_id=…, product_id_type=…)`;SPEC 预填传 **None**(改码不做 SPEC 预检:匹配键取自我们自己的观测);信封 `{"Item": …}` 由 `api/feeds.build_payload` 包 | 同上 |
| ③ | 删 `mp_mapper.build_sku_update_item`、`mp_mapper.build_orderable(sku_update=)`、`mp_conform.ORDERABLE_SYSTEM_SWITCHES` 的放行分支(形态 A/B 残留 = 双轨) | services |
| ④ | 候选行带出**现挂价**(`w.price`)与 **Product ID 优先 GTIN**(`coalesce(w.gtin, w.upc)`),重量经登记簿 `source_key` LEFT JOIN `catalog.products.slow`,由 `mp_mapper.shipping_weight_ex` 解析(`_weight_of` 是改码侧的唯一出口) | `_SQL_CANDIDATES` / `_FROM` |
| ⑤ | `_CONDS` 加一条判据:**有现挂价格**(`w.price IS NOT NULL AND w.price > 0`)。~~**有采集重量**(`(p.slow -> 'weight') IS NOT NULL`)+ Python 侧第二层剔除~~ **当天晚些时候整段删除**(见本节末「改码顺便把重量统一到准确值」):重量不再是判据,一律按新口径重写 | 同上 |
| ⑥ | 配额口径改口:桶是 `feeds.post.MP_ITEM_MATCH` **15/h**(`api/_client.py:238`),与跟卖链共享,**不再与维护链抢**。⚠ 当时只改了头注、**数字没跟着改**,两个常量已于 2026-09-07 整体删除(见本节末「去掉每轮 1000 条自设上限」) | ~~`FEEDS_PER_STORE_PER_RUN` / `ITEMS_PER_FEED` 头注~~(已删) |
| ⑦ | 反哺器防串扰:`match_sheet.sync_from_ledger` 与 `listing_sheet._SQL_HEAL_RECEIPT` 各加 **workflow 正向过滤**(改码与跟卖共用 feedType 之后,feed_type 已分不开两条链) | services |
| ⑧ | ~~缺口 **G-4**:`_sync_sheet` 改为「先按 (店, 旧码) 的 SKU 列定位 → 旧行 SKU 列为空才退回 (店, ASIN) → **多行命中不写并点名**」~~ **当天晚些时候整段作废**:改码不回写上架表(见本节末「改码不回写上架表」) | `workflows/sku_migrate.py` |
| ⑨ | 守门与文档:`test_feed_type_constant_is_the_only_place_that_names_a_feedtype` 只看可执行行;§8 决策 E/I 收口;`docs/api_blueprint.md` §5.1 与 `docs/db_schema.md` 的 `feed_type` 说明改口 | tests / docs |

**为什么价格必须原样发回去**(这一条比通道本身更容易出事):REPLACE 的语义
是"载荷给了什么,线上就变成什么"。所以采不到现挂价的行**一律不许发**——
猜一个价 = 一次改码顺手改了售价,而且**回执全绿、摘要正常**。判据是 `_CONDS`
的「有现挂价格」,选取与解释两处同源。

**改码顺便把重量统一到准确值**(2026-09-06 晚所有者定稿,当天早些时候的
「两层重量判据」整段作废):当天第二级投放实测发现 `mp_mapper.shipping_weight`
**只抓字符串里第一个数字、完全不看单位**,把两个品的 "300 grams" / "860 grams"
当成 300.0 / 860.0「磅」经 REPLACE 发进了沃尔玛(那一栏是 **Shipping Weight (lbs)**)。
所有者原话:「请勿猜测单位,一切以官方事实为主……如果解析不出重量或者重量大于
11 磅,则把重量都写为 1 磅。」于是:

- 解析器重写为 `mp_mapper.shipping_weight_ex(product) -> (磅, 归因)`,**单位从数据
  里读、不猜**:只认 pound/lb、ounce/oz、gram/g、kilogram/kg 四族显式记号,按官方
  常量(1 lb = 16 oz = 453.59237 g)折成磅;**没有单位记号的裸数字 = 解析不出**;
  解析不出 / ≤0 / > `MAX_SHIPPING_WEIGHT_LBS = 11` ⇒ `DEFAULT_SHIPPING_WEIGHT = 1.0`。
  归因六档(parsed / no_weight / no_unit / unknown_unit / over_cap / nonpositive)
  就是为了让调用方分得清"真 1.0"与"兜底 1.0";`shipping_weight()` 只是薄封装。
- 改码时**所有候选统一按这一口径重写重量**,覆盖是**有意为之**:存量行线上那个
  重量本来就是老实现按错单位发上去的。所以那条 SQL 粗判据与 Python 侧第二层剔除
  **一起删了**(留着等于"新口径管不到最该改的那批行"),候选面回到十条判据。
- 兜底不静默:dry-run 逐行标 `重量 3.5 磅(parsed)` / `重量 1.0 磅(兜底:超 11 磅)`,
  摘要给兜底行数,台账 `listing.sku_migrations.detail.weight_reason` 留档;
  上架链 `workflows/list_new` 的摘要把兜底行数**按归因分桶**(无采集重量 / 无单位
  记号 / 单位不认识 / 超 11 磅 / 非正数),上架行为不变(兜底值照发)。

**待第一级投放实测(不阻塞切换)**:仓里的 MP_ITEM_MATCH 通道是 **v4.2**
(`sellingChannel` 制 header,跟卖链生产在用),所有者上传的模板是 **v5.0**。
v4.2 能否同样原地换码,由第一级投放(节奏闸自动压到 `limit=1`)的那一个品定 ——
**不另写探针、不加 v5 header、不加参数开关**(一个能力一条实现路径)。
第一级投放的预期形状(dry-run 已核):

```
{"MPItemFeedHeader": {"processMode": "REPLACE", "subset": "EXTERNAL", "locale": "en",
                      "sellingChannel": "mpsetupbymatch", "version": "4.2"},
 "MPItem": [{"Item": {"sku": "<12 位不透明码>", "price": 29.99, "ShippingWeight": 0.82,
                      "condition": "New",
                      "productIdentifiers": {"productIdType": "GTIN",
                                             "productId": "00121678236703"}}}]}
```

**Test851 遗留数据的手工修法**(所有者手改的那一条不在任何台账里,程序看它是
"凭空多出来的一个新码"):`sources_reclassify` 把 `Test851` 归 `amz` / 源头码
`B0CRKFQZWF`,再
`sku_codec.abandon(conn, 'A085朱丽霖', 'B0CRKFQZWF', ABANDON_SKU_UPDATE, replaced_by='Test851')`
把旧码标成"已被替换"。**所有者已执行**,此处只留做法备查。

#### 第一级投放实录(2026-09-06 20:19,A085朱丽霖 `B0000C8W8W → AVW476VD6W3H`)

**通道实测结论:v4.2 能原地换码,但会把库存清成 0。**

| 探针 | 结果 |
|---|---|
| wpid | 新旧码同为 `3JLHQP1Z1JPX` ⇒ **同一个 item 原地换码**(v4.2 与所有者手测的 v5.0 一致) |
| 价格 | 不变(载荷发的就是现挂价) |
| 旧码 | `GET /v3/items/{旧码}` 404 |
| feed | 1 条 SUCCESS |
| **库存** | 旧码最后观测 `avail_qty=30`,**新码 0** |

**库存归零的诊断链**(为什么断定是沃尔玛干的,不是我们):

1. 改码前后我们**没有对这两个码发过任何库存动作** —— `ops.feed_items` 里该店该
   时段没有 inventory/MP_INVENTORY 记录,`ops.dispositions` 没有 inventory 类
   executing,维护链当轮也没产这两个码的意图;
2. 新码是这次改码**当场出生**的,它此前不存在,不可能被谁写过 0;
3. 载荷里根本没有库存字段(MP_ITEM_MATCH v4.2 的 Item 只有 sku / price /
   ShippingWeight / condition / productIdentifiers);
4. ⇒ 唯一解释:`processMode=REPLACE` 把「载荷没带的库存」当 0 写了。这与「价格
   与重量必须原样发回去」是**同一条语义**的另一面 —— 只是库存没法在载荷里发
   (v4.2 的 SPEC 预填模板只有 productIdentifiers + productCategory,加一个
   不可验证的字段进去,错了也没人会知道),所以只能在定案时补写。

**所有者当场的手工修复**:`api.inventory.put_inventory(store, 'AVW476VD6W3H', 30,
ship_node=None)` → `(True, '')`,库存已补回。

**所有者定稿**:「**库存直接使用在数据库中读取到的库存就可以了,不需要记库存**」
—— 即**不在提交前另抓一次现值另存一份**,定案时直接用 `catalog.walmart_items`
**旧码行**的 `avail_qty`(旧码消失后 catalog_sync 只给它盖 `missing_since`,
这一列保留最后一次观测值;上例正是 30)。

> ⛔ **以下这张「定案时回写库存」的修法表已于 2026-09-07 整段作废**,`_restore_inventory`
> 连同 `_SQL_INV_QTY` / `_SQL_LEDGER_INV` / `api.inventory` 依赖已从工作流删除 ——
> 见本节末「定案不再回写库存(2026-09-07 所有者定稿)」。表留在这里只为记录当时
> 为什么这么做、以及它是怎么被 v5 的载荷带库存取代的,**不是现行做法**。

**修法**(`workflows/sku_migrate._restore_inventory`,一条实现路径;**已作废**):

| 项 | 定稿 |
|---|---|
| 时点 | `_settle` 里 **confirmed 之后**(定案的后果之一,不是善后;`_sync_sheet` 当天晚些时候已整段删除) |
| qty | `catalog.walmart_items.avail_qty`,按 **(店, 旧码)** 读;一轮一条 SQL(新旧码一起问) |
| 不写的三种情况 | qty 为 NULL / 0;新码 `avail_qty` 已等于 qty(通道将来自己保住库存时这段空转);受管仓判不出(**fail-closed**,不回落 legacy 单仓) |
| 通道 | `api.inventory.put_inventory(store, new_sku, qty, ship_node=node)`;`node = store_limits.resolve_node(store, store_limits.maint_nodes())` —— **维护链同一个入口**,`maint_nodes()` 一轮只读一次 |
| 失败 | **只告警点名,不自动重试、不换方法**(安全红线);摘要说明维护链下一轮会按 amz 库存重算,人可先手工补 |
| 留档 | `listing.sku_migrations.detail` 加 `inventory_restored: qty`(jsonb `\|\|` 合并,不冲掉提交时的 product_id/price/weight) |
| 摘要 | 首行定案段「库存回写 N」,失败另加 ⚠;明细逐条列 (旧码→新码, qty);dry-run 只报「将回写库存 N 条(qty 来自旧码最后观测)」 |

**运维含义(写进了工作流头注的安全约束⑦)**:**提交到定案之间该品是停售的**
(线上库存 0)。所以第一级投放之后要**尽快**跑 `catalog_sync -p store=X`,
再 `sku_migrate -p store=X -p settle_only=1`,别隔夜。

**顺带降噪(已被下一条定稿取代:整段回写都删了)**:`_sync_sheet` 的「上架表找不到行」由 ⚠ 改成不带标记的计数行
(「上架表无对应行 N + 样本 3 个」)—— 旧系统上架的存量品本来就不在上架表里,
整店改码时是成百上千条的常态,带 ⚠ 会把真告警(重复 ASIN / 同店双挂 / 超期)
淹掉。**「重复 ASIN 无法定位,人工」那条 ⚠ 保持原样**:它是真要人动手的。

**第二级投放实录(2026-09-06 21:40,limit=10)**:候选 10 个,2 个因采集重量解析不出
被剔(B0001P03RO / B000BO79PE,摘要点名),8 个一条 feed 提交,9 分钟后 catalog_sync
观测:8 个新码全部在架、8 个旧码全部缺席,settle 判 confirmed 8 / rolled_back 0。
**库存这次全部跟随**(新旧逐一相等:12/19/27/30/11/999/9/0),`_restore_inventory`
判「新码现值已等于旧码」一条都没写 —— 行为正确。价格 8 个逐一不变。
结论修正:第一级的库存归零更像是沃尔玛把库存挂到新码上有几分钟延迟、被 8 分钟后的
catalog_sync 采到了过渡态,**不是 REPLACE 必然清零**;回写逻辑当时作为兜底保留(只在
观测到差异时写)——⛔ **这条"兜底保留"已于 2026-09-07 作废**,见本节末所有者定稿。3 个旧码名下滞留 executing 的 inventory 处置按设计不迁,由 expire_executing
收尾。上架表无对应行 9 条(旧系统上架的存量品),计数不告警;首行「未同步 N 行」的
终态口径待所有者定。

#### 改码不回写上架表(2026-09-06 所有者定稿)

**所有者原话**:「我们批量修改在线产品的 sku 无需回填上架表行,上架表我经常会清理,
我们的 sku 和对应的来源码已经填写到在线产品表格中了。上架表中的 sku 列由上架的填写
即可。」

**定稿**:`sku_migrate` 的上架表 SKU 列回写**整段删除** —— `_sync_sheet`、
`_SQL_LEDGER_SHEET_OK`、每轮的补写候选查询(`status='confirmed' AND sheet_synced_at
IS NULL`)、摘要首行的「⚠ 上架表 SKU 列未同步 N 行」与明细里的三行(将回写 / 补写
候选 / 上架表无对应行)一并去掉;`counts` 里的 `sheet` / `sheet_lag` 字段删除;
`services/listing_sheet` 不再被本工作流 import。

**理由**(这也是上一条「首行未同步 N 行的终态口径待所有者定」的答案):

- 身份映射已经有两处出口 —— 权威在登记簿 `catalog.listing_sources`,人看的那份在
  **在线产品总表的「来源码」列**(catalog_sync 的投影)。上架表 SKU 列是第三处,
  而它**会被所有者定期清理**:拿一张随时会被清空的工作表当身份映射就是双轨(§六),
  而且清空之后没有任何东西会报错。
- 上架表 SKU 列的**唯一写侧**回到上架链(`list_new` 落地时填),口径干净:一列一个
  写方。改码链不再为一张会被清理的表长出「写失败 → 盖时间戳 → 下一轮补写」这条
  补偿路径,`_settle` 少一处外部 IO。
- 实测背景:第二级投放(limit=10)那一轮里「上架表无对应行」9 条 —— 旧系统上架的
  存量品本来就不在上架表里,整店改码时是成百上千条的常态。那条计数行连同它上面的
  降噪讨论一起消失了。

**列不动**:`listing.sku_migrations.sheet_synced_at` **保留**(不 DROP、不 ALTER),
只为不动存量库;新行**恒 NULL**,旧行留着的时间戳是历史事实记录,不回填改写。
`refdata/schema.sql` 与 `docs/db_schema.md` 的该列注释已改口。

**守门**:`tests/test_sku_migrate.py::test_sku_migrate_never_writes_the_listing_sheet_sku_column`
(源码可执行行里不许再出现 `listing_sheet` / `write_sku_col` / `sheet_synced_at` /
`_sync_sheet`)+ `test_confirmed_never_touches_the_listing_sheet`(定案不碰上架表、
不写那一列)。缺口 G-4 的三条用例(按旧码定位 / 空 SKU 列退回 ASIN / 重复 ASIN 不猜)
随实现一起删除。

**重量解析器与全库单位直方图对照(2026-09-06,所有者 SQL)**:采集 weight 的值在
`item` 侧(`package` 1,243,098 行为空),形态是「数字 + 单位词」串。单位词分布:
pounds 275,426 / ounces 181,891 / kg 139,226 / g 67,726 / milligrams 197 /
hundredths pound 94 / lbs 86 / lb 32 / oz 18 / foot_ounces 16 / 空 16 / lbs. 4 / pound 4,
其余是整段文案或 tons、gravity 之类无重量定义的记号。解析器只认表内记号(pound/lb、
ounce/oz、gram/g、kilogram/kg、milligram/mg、hundredths pound),数字后紧跟的字母词当
单位(所以 "ounces(181.44 g)" / "kg/6.8lbs" 也能读),表外一律 unknown_unit 走 1 磅。
全库两侧皆空 587,615 / 1,252,457 行 —— 这些品上架与改码都写 1 磅(所有者定稿),
**不用大模型补重量**:重量不可由文案推出,且 ShippingWeight 是禁止 LLM 填写的系统字段;
要提高覆盖只能改采集侧(亚马逊详情页 Item Weight / Package Dimensions)。
第二级投放里发成 300 / 860「磅」的两个品真实重量是 0.66 / 1.9 磅(300 g / 860 g),
所有者手工纠正;此后改码统一按解析器写重量。

**节奏闸改按全船队计(2026-09-07,所有者定稿)**:所有者问「难道后面每个店都需要先跑
10 个才能做剩下的吗?」—— 不需要。1 → 10 两级验的是「通道能否原地换码」,店无关,
A085朱丽霖 已实证;`_stage_cap` 的 confirmed 改数全船队,pending/stalled 仍按店数
(该店账没清就不发下一批)。店相关风险另有闸:闸①凭证/在营、受管仓节点判不出则
**载荷不带库存**(`_fc_of` fail-closed,绝不回落 Partner ID)。
~~每轮仍有配额留量硬顶 1000 条(2 个 feed × 500)~~ **当天作废**,见下一条;整店按轮走,
每轮之间 catalog_sync + settle_only + feed_poll。

#### 去掉每轮 1000 条自设上限(2026-09-07,所有者纠正)

**现象**:整店真跑 A085朱丽霖(3371 个在线品)只发了 1000 条就停,摘要写着
「配额留量硬顶 1000 = 2 个 feed × 500 条」。所有者:「这个限制不对吧,是一个 feed
提 1000 个,直到全部提交完,而不是总共 1000 个吧?并且这个数量限制你是否查看了
官方 api 的限制……我们之前也定过关于提交 feed 时的相关规则。」

**来历**(核实结论):`FEEDS_PER_STORE_PER_RUN=2 × ITEMS_PER_FEED=500` 是批次 3
**还走 MP_MAINTENANCE**(桶 8/h,与 13:00 维护链共享)时,为了**给维护链留桶**
自设的一层,**不是官方限制**。2026-09-06 通道切到 MP_ITEM_MATCH(桶 15/h,与跟卖链
共享,不再与维护链抢)之后,只改了这两个常量的**头注口径**(本节切换清单第 ⑥ 行),
数字没跟着改 —— 于是一层"为已经不存在的冲突留的量"继续硬顶着整店改码,
而摘要把它说得像官方配额。

**官方限制出处**:MP_ITEM_MATCH = **20 feed/hour、单 feed 25MB**
(`refdata/walmart_rate_limits.tsv:194`;`docs/api_blueprint.md` §3 表同一行)。
仓内两道限**都已在 api 层**,不需要工作流再设第三道:

| 限 | 官方 | 仓内 | 出生地 |
|---|---|---|---|
| feed 频率 | 20/hour | **15/hour**(95% 留量) | `api/_client.py:238` `feeds.post.MP_ITEM_MATCH` |
| 单 feed 大小 | 25MB | **1000 条 / 24MB** | `api/feeds._SLICE_LIMITS["MP_ITEM_MATCH"]` |

**既定规则**(不是这次新定的):`docs/plan.md` 工作流 6(maintenance)「意图上限
按店化(所有者定稿 2026-08-26)……**新鲜度优先,单店整量当轮连发,如 15000 条 =
8000+7000 两个 feed 连续提交**」。改码照同一条规则走。

**改动**(`workflows/sku_migrate.py`,api 层**零改动**):

| # | 改动 |
|---|---|
| ① | 删 `FEEDS_PER_STORE_PER_RUN` / `ITEMS_PER_FEED` 两个常量及其头注;常量区留一段注释写清来历与官方出处(守门测试钉住:可执行行里不许再出现这两个名字) |
| ② | `_stage_cap` 去掉 `quota_cap` 那层,只剩 open→0 / 全船队 confirmed 的 1 → 10 → 按 `-p limit` |
| ③ | `_migrate` 去掉「候选超配额留量」截断与自己分批的循环:**一次** `feeds.submit_feed(store, FEED_TYPE, _build_items(rows), workflow="sku_migrate")` 把整批交给 api 层切片,再用 `feeds.iter_result_slices(results, rows)` 逐片对位落账(submitted/dedup/failed/unknown 四档逻辑一字未改) |
| ④ | 摘要/头注里「配额留量硬顶」全部改口:上限只有**速率桶(api 层)+ 切片(api 层)+ 节奏闸(1 → 10 → limit)** |

**吞吐**:3371 个品 = 4 个 feed(1000+1000+1000+371),15/h 桶内一轮发完;真跨过桶时
`api/_client.rate_acquire` 会抱锁等下一枚令牌 —— 这是**既有行为**,不在工作流层
另写一道闸(§六:一个能力一条实现路径)。节奏闸仍在:该店有 pending/stalled 就
本轮只定案不提交,全船队 confirmed < 10 时仍压到 1 / 10。

#### MP_ITEM_MATCH 升 v5、改码带库存(2026-09-07)

**依据是官方规范原件**,不是文档推断也不是模板反推:所有者从开发者门户下载的
`refdata/specs/MP_ITEM_MATCH_5.0.20260607-22_38_54-api.json`(draft-07,40KB)已进仓,
守门测试 `tests/test_match_spec_v5.py` **现读它**校载荷(不写第二份字段清单)。
原件说的四件事:

| 位置 | 原件事实 |
|---|---|
| `MPItemFeedHeader` | required = [businessUnit, locale, version],`additionalProperties:false`;businessUnit enum 含 WALMART_US;locale enum ['en'];version enum **只有 `5.0.20260607-22_38_54-api`** |
| `MPItem[]` | 每项 required=['Item']、`additionalProperties:false` ⇒ **仍是 `{Item:{…}}` 包装**,不是 MP_ITEM 的 Orderable/Visible 分段 |
| `Item` | required = [productIdentifiers, sku, condition, ShippingWeight, price],`additionalProperties:false`;可选 **inventory** / productName / mainImageUrl / externalProductIdentifier / stateRestrictions / productSecondaryImageURL / restoredProductIdentifier。productIdType enum [EAN,GTIN,ISBN,ISSN,UPC];sku maxLength 50;price multipleOf 0.01;ShippingWeight multipleOf 0.001 |
| `Item.inventory` | array,minItems 1,每项 required=[quantity(integer ≥0), fulfillmentCenterID(string)],`additionalProperties:false` |

⇒ **v4.2 那套 `{processMode, subset, sellingChannel}` header 在 v5 里不存在**,发过去
就是三个未知字段(而 `additionalProperties:false` 的东西发错了是整批退回,本地看不出
任何异常)。REPLACE 语义**没变**——同 GTIN + 新 SKU 原地换码仍然成立,它只是不再由
header 里的一个开关表达,而是这条 feedType 的固有行为。

**v4.2 同日退役,跟卖链与改码链一起升**(一个 feedType 一条实现路径,§六:留一条
v4.2 兼容路径就是双轨,而两条路径的副作用完全不同)。

| # | 改动 | 位置 |
|---|---|---|
| ① | `FEED_SPEC_VERSIONS["MP_ITEM_MATCH"] = "5.0.20260607-22_38_54-api"`(注释写明出处 = refdata/specs 那份原件 + v4.2 退役日期) | `registry/resources.py` |
| ② | `build_payload` 的 MP_ITEM_MATCH 分支:header → `{businessUnit: WALMART_US, locale: en, version: ver}`;条目仍包成 `{"Item": _sanitize(e)}`。api 层只改信封、不加业务判断(铁律 2) | `api/feeds.py` |
| ③ | `build_match_item(..., inventory: tuple[int, str] \| None = None)` ⇒ `base["inventory"] = [{"quantity": int(q), "fulfillmentCenterID": str(fc)}]`;**不给就不带**(跟卖链现状,行为逐字不变)。小数位按原件 multipleOf:price 2 位、ShippingWeight 3 位(api 的 `_sanitize` 仍统一收到 2 位,2 位也是 0.001 的整数倍,两处不冲突) | `services/match_feed.py` |
| ④ | 候选 SQL 带出 `w.avail_qty`(**不是判据**:没观测到照样改码,只是不带库存);`_item_of(row, sku, fc)` 在 avail_qty 非空且 ≥0 时带 inventory;FC 走 `store_limits.listing_fc(store, managed_nodes()[0])`(**上架链同一入口**,`managed_nodes()` 一轮只读一次,收在 `_fc_of`);受管仓校验失败的店**不带库存 + 摘要点名 + 不回落 Partner ID**;dry-run 逐行标「库存 N → FC xxx」/「不带库存(未观测)」 | `workflows/sku_migrate.py` |
| ⑤ | 规范守门:读原件断言 header 键集合 == required 且不多、Item 键都在 properties 里、inventory 项键 == {quantity, fulfillmentCenterID}、`FEED_SPEC_VERSIONS` 的值在 version enum 里、productIdType 在 enum 里;`_item_of` 的真实产物也过同一把尺子 | `tests/test_match_spec_v5.py`(新增) |

**载荷样例**(改码链一条,带库存):

```
{"MPItemFeedHeader": {"businessUnit": "WALMART_US", "locale": "en",
                      "version": "5.0.20260607-22_38_54-api"},
 "MPItem": [{"Item": {"sku": "AN3WC0DE2345", "price": 29.99, "ShippingWeight": 0.82,
                      "condition": "New",
                      "productIdentifiers": {"productIdType": "GTIN",
                                             "productId": "00121678236703"},
                      "inventory": [{"quantity": 30,
                                     "fulfillmentCenterID": "<listing_fc>"}]}}]}
```

**库存口径改口(安全约束⑦)**:**v5 起库存随改码 feed 一起写**。qty 是
`catalog.walmart_items` **旧码行**的 `avail_qty`。当时定的是「定案回写降为兜底」,
⛔ **一天后(2026-09-07)所有者把兜底也去掉了** —— 见本节末「定案不再回写库存」:
库存现在**只有改码 feed 这一条写路径**。**没带库存的那几条,提交到定案之间仍是停售的**,
运维口径不变(尽快 `catalog_sync` + `settle_only=1`),补库存的活交给维护链下一轮按
amz 库存重算。

**版本串为什么用 0607 而不是所有者上传成功的那个**:所有者 2026-09-06 用 Seller Center
模板成功上传过,模板头写的是 `5.0.20260703-18_22_27`;但**可下载的 API 规范原件是 0607
版**,而它的 version enum 只有 `5.0.20260607-22_38_54-api` 这一个值(与 MP_ITEM 一样带
`-api` 后缀 —— Seller Center 模板串与 API feed 串本来就不同源)。⇒ **以原件 enum 为准**,
试点(节奏闸的 limit=1 那一条)若被拒再议;那时该改的是原件 + registry 一处,不是在
代码里并排放两个版本串。

#### A131 整店被拒:item setup limit(2026-09-07)

**生产事实**(2026-09-07 10:10,A131吕灿荣 整店改码 2740 条,三个 MP_ITEM_MATCH feed):
三条 feed **全部** `feedStatus=ERROR`、`itemsReceived=0`,报错挂在 **feed 级**
`ingestionErrors` 上(**没有任何逐条明细**):

```
EXT_DATA_ERROR_50575703577001
You have exceeded your item setup limit of 5000. … Please resubmit your file to
ensure that the total number of items in your catalog is below your designated limit.
```

**机制**:沃尔玛按「**店内现有 item 数 + 本 feed 条数**」对每店的 item setup limit
判定,超了就**整个 feed 拒收**——不是拒掉超出的那几条,是一条都不收。
**MP_ITEM_MATCH 的改码在它眼里先算新增**(REPLACE 是"匹配到同一个 item 之后"的事,
配额闸在那之前)。A085朱丽霖(在架 3371 + 一批 1000 = 4371)没撞上,A131 撞上了。

**两处改动**(2026-09-07):

| # | 改动 | 位置 |
|---|---|---|
| ① | **feed 级 ERROR + 零明细 ⇒ 台账逐 SKU 落 `failed`**(不再落 `missing`):回执取 head 里 `ingestionErrors.ingestionError[0]` 的 code/description,`error_code` / `error_desc` 都落(走既有 `error_text`),`feed_log` 落 failed;**有逐条明细的 ERROR feed 行为一字不变**。`ingestion_errors()` 同时兼容官方两种形态(裸 list / `{"ingestionError": […]}`)。这条路径旧仓本来就有(`docs/legacy_survey.md`「feed 整体 ERROR 且 itemDetails 为空 ⇒ 回查预写的 SKU 列表逐个打 FEED_ERROR」),重写时丢了 | `services/feed_track.poll_feed` |
| ② | **上架上限闸**(安全约束⑧):`_stage_cap` 的生效上限再 min 一层**余量** = 该店上限 − 现观测在架 item 数;余量 ≤ 0 ⇒ 上限 0 并点名"先清理死档再改码" | `workflows/sku_migrate` |

**为什么①要紧**:`missing` 的语义是"沃尔玛没说这条的下落",而这里它说得很清楚
——**一条都没收**。读成 missing 之后:`_verdict` 只认 `failed` 才当场回滚,
2740 条要挂满 24h 观测期才可能被反证;上架链/维护链同样把"整批被拒"读成"查无"。
落 failed 之后,同一轮 `settle_only` 就把这 2740 条 `rolled_back`(旧码复活、新码弃掉),
而且摘要里带着沃尔玛的原话。

**上限从哪来**:上下架限额表(飞书)新增一列 **「商品上限」**
(`registry.resources.RETIRE_LIMITS.fields.item_setup_limit`,**所有者稍后建列**;
可在 Seller Center 查各店真实上限填进去)。读列走 `services/store_limits.setup_limits()`
(与「配送时长限制」同一个 `_int_map` 口径,**工作流不读飞书**);读不到该列 / 该店未填
⇒ 缺省 `registry.resources.WALMART_ITEM_SETUP_LIMIT_DEFAULT = 5000`
(出处就是上面那句报错原文 + A131 实证)。

**在架数怎么数**:`catalog.walmart_items` 该店 `missing_since IS NULL` 的行数,
**保守口径:含 RETIRED / UNPUBLISHED 一起数**。沃尔玛怎么数它自己的 limit 没有原文
(只说 "the total number of items in your catalog"),而两个方向的代价不对称 ——
数多了只是本轮批次变小(下一轮接着改),数少了是整个 feed 被拒、一条都进不去。
✅ **口径已校准(所有者 2026-09-07)**:Seller Center 显示 A131「目录中有 4463 个商品,
最多 5000 个」,与 `_SQL_ONLINE_ITEMS`(在架行含 RETIRED)逐字相等。各店上限不同
(A085 在架 4316 + 1000 没撞线 ⇒ 它的上限高于 5000),真实上限填限额表「商品上限」列
(所有者已建列);所有者定:人工观察余量、手动 `-p limit`,本闸只是护栏。

**摘要样例**(dry-run 与真跑同样报,在「本轮明细」段紧跟节奏闸那一行):

```
  节奏闸:本轮上限 100(请求 1000000;全船队已 confirmed 999 个…)
  上架上限闸:上限 5000(缺省,该店未填「商品上限」),在架 4900,本轮最多 100
```

余量 ≤ 0 时:

```
  ⛔ 上架上限闸:店内 item 数 5000 已达上架上限 5000(缺省,该店未填「商品上限」),
     先清理死档再改码 —— 本轮上限 0(超限沃尔玛会**整个 feed 拒收**,itemsReceived=0,
     一条都进不去)
```

**这道闸拦不住的只有一种情况**:上限填错、或沃尔玛的口径与我们数的不一样。那时仍会
整 feed 被拒 —— 但有了改动①,回执是 failed,当场回滚,不再卡 24 小时。

**A131 的善后**(所有者动作):那 2740 条台账行现在会被下一轮 `feed_poll` /
`sku_migrate -p store=A131吕灿荣 -p settle_only=1` 判成 `rolled_back`(旧码复活、
新码弃掉,旧码在沃尔玛侧从没变过);要继续改这家店,先把死档清到上限以下。

#### 定案不再回写库存(2026-09-07 所有者定稿)

**所有者原话**:「按我们现在修改 sku 的流程,已经不需要再提交一次改库存了。」

**背景**:MP_ITEM_MATCH 升 v5 之后,改码 feed 自带 `Item.inventory`
(quantity = 旧码最后观测的 `avail_qty`,fulfillmentCenterID 走
`store_limits.listing_fc`)。同一天 A131 第一批定案时,留作兜底的 `_restore_inventory`
仍**逐条 PUT `/v3/inventory` 写了一遍** —— 因为新码行在库里的 `avail_qty` 还没被下一轮
库存拉取更新,那条「新码现值已等于旧码就不写」的空转判据判成了"不相等"。于是:

- 这是**同一份数据的第二条写路径**(conventions §六 双轨禁止);
- 白烧库存接口配额(一条改码 = 一次额外 PUT);
- 更糟的是**可能把提交之后已经卖掉的数量原样写回去** —— 回写用的是改码之前的观测值。

**定案**:**定案不再回写库存**。库存只随改码 feed 写一次;**未观测到库存的行不带**,
由沃尔玛按 REPLACE 语义处理,维护链下一轮按 amz 库存重算 —— 本工作流不为库存补第二条腿。

**落地**(`workflows/sku_migrate`,2026-09-07):

| # | 删掉 | 备注 |
|---|---|---|
| ① | `_restore_inventory` 整个函数 | 连同它的 fail-closed 受管仓判定;`_fc_of`(载荷侧)的 fail-closed **保留**,那是主路 |
| ② | `_SQL_INV_QTY` / `_SQL_LEDGER_INV` | 定案不再读旧码 avail_qty、不再写 `detail` 补丁 |
| ③ | `from api import inventory as inv_api` | 库存接口不再是改码链的依赖 |
| ④ | `_settle` 的 `store_of` 入参与 `inventory` / `inventory_failed` / `inventory_todo` 三个计数、摘要那一行 | 定案段不调任何沃尔玛写接口,**连店铺凭证都不取**(`run()` 的 `_store_of` 只剩提交那一侧用) |
| ⑤ | 首行的「库存回写 N」与「⚠ 库存回写失败 N」 | 带没带库存改由提交段的「载荷带库存 N/M」报 |

**不动的两处**:`listing.sku_migrations.detail` 里历史行的 `inventory_restored` 键
**原样保留**(事实记录,不回填不改写),**库表 schema 无变更**。

**守门**:`tests/test_sku_migrate.test_settling_never_calls_the_inventory_api`
—— 可执行行里不许再出现 `put_inventory` / `_restore_inventory` / `inventory_restored`,
也不许再 import `api.inventory`。

### 9.13 变体组号改不透明码(2026-09-07,所有者定稿三条)

**背景**:SKU 已经是 12 位不透明码,但变体品发给沃尔玛的 `variantGroupId` 仍是
`vg_<父 ASIN>`(`services/variant_group.group_id` 派生 → `mp_conform._apply_variant_plan`
写进载荷)—— 等于**把亚马逊 ASIN 从后门递给沃尔玛**,货源隐匿在变体这条线上一直没
生效(§8 待决项、`docs/sku_wiring_audit.md` G-7 目标级漏洞)。

**所有者定稿三条**(原文口径):

1. 新增登记表 `catalog.variant_groups`,键为(店铺, 家族键),值为一个不透明组号;
   list_new 上架时先查表,没有就发号落库,再写进载荷。兄弟分批上架仍能并入同组,
   ASIN 不再外递。
2. 存量已在架的组**不回改**,继续用它们现在的 `vg_ASIN`;同族新成员并入时沿用在架
   成员的现有组 ID(这条规则 `_FAMILY_LISTED_SQL` 本来就有)。
3. **不许用 ASIN 取哈希当组号**(ASIN 空间公开可枚举,哈希等于没藏);也不许任何
   确定性从 ASIN 派生的串发出去。

**为什么不用哈希**:组号的威胁模型不是"看不看得懂",是"**能不能反查**"。ASIN 是公开
可枚举的十位串,任何哈希都能离线打一张全表彩虹表,把组号反解回 ASIN —— 那和明文
只差一步脚本。同理也不用"ASIN + 盐":盐一旦泄漏(或被同一批数据侧写出来)全部回退,
而我们并没有轮换盐的机制。随机号没有这个面:它与 ASIN 之间**只有我们库里那一行**。

**登记表设计**(`refdata/schema.sql`,`docs/db_schema.md` 同步):

```sql
CREATE TABLE IF NOT EXISTS catalog.variant_groups (
    store       text NOT NULL,
    family_key  text NOT NULL,   -- 家族键:variant_group.family_key 的产出,只进不出
    group_code  text NOT NULL,   -- 发给沃尔玛的 variantGroupId
    workflow    text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (store, family_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS variant_groups_code_uidx
    ON catalog.variant_groups (store, group_code);
```

- 唯一索引是 **(store, group_code) 而不是全局**:存量 `vg_<ASIN>` 号可能跨店重复
  (同一个 ASIN 被两家店上过),全局唯一在登记存量那一刻就插不进去;而"一个组号在
  一家店只能属于一族"才是真正要防的(两族共号 = 沃尔玛侧两族并成一组)。
- **不加 12 位字符集条件**:存量沿用的 `vg_…` 不是 12 位码,加了它们一条都进不来;
  而那个正则在 schema.sql 里只准出现两次(守门 `test_no_second_opaque_regex_in_the_repo`)。
- 行**永不 DELETE**(删一行 = 下一个兄弟重新发号 = 同一族被劈成两组);
  INSERT 只有 `sku_codec.mint_group_code` 一个出口(守门一条白名单)。

**落地**(七处):

| # | 位置 | 改动 |
|---|---|---|
| ① | `registry/resources.VARIANT_GROUP_LETTER` | 新增,值 `"G"`;**不是来源字母**,取值不许与 `SKU_SOURCE_LETTERS` 重合(守门钉住) |
| ② | `services/sku_codec.mint_group_code` | 组号的**唯一出生地**:① 查表 → ② `existing` 原样登记(存量不回改)→ ③ 抽 `G`+11 位随机段。与 mint 同纪律:无 `dry_run` 形参、抽号与登记同一事务、commit 归调用方。占位串 `DRYRUN_GROUP_PLACEHOLDER` 给空跑用 |
| ③ | `services/variant_group` | `group_id` → **`family_key`**(去掉 `vg_` 前缀,派生规则一字未改);`plan()` 多出 `family_key`,`group_id` 只可能是在架同族的现有号或空串,**不再派生** |
| ④ | `workflows/list_new._prep_rows` | **发号点**:紧跟 SKU mint 循环、同一个 `with db.pg_conn()`,按 (店, 家族键) 去重每族一次,写回该族所有行;发号后仍为空则 `raise`(不许静默退单品) |
| ⑤ | `workflows/list_new` 三个分组函数 | `_drop_degenerate_dims` / `_remap_unmapped_dims` / `_dedupe_primary` 的分组键 `(store, group_id)` → `(store, family_key)`(发号在它们之后,组号此刻恒空) |
| ⑥ | `workflows/list_new` dry-run | 逐行回显 `组号待发(家族键 …)`;`_spec_precheck` 用 `DRYRUN_GROUP_PLACEHOLDER` 填**副本**的 group_id。**空跑绝不写库**(守门:dry-run 底座把 `mint_group_code` 桩成抛断言) |
| ⑦ | `services/mp_conform._apply_variant_plan` | `plan["group_id"]` 为空**一律不发**(整套走单品口径 + note「变体组号缺失」),防上游漏发号时发出空组 ID |

**发号点为什么必须在 `_prep_rows`**(与 SKU mint 逐条相同的三条硬理由):① 不进
`_one_store` —— 店级失败的串行补试重跑的就是它,重发号 ⇒ 载荷不再一字不差 ⇒
`api/feeds.payload_key` 在途防重不命中 ⇒ **双上架且不报错**;② 单事务顺序做一遍,不放
进 128 路 autocommit worker(并发抢同一个 `(store, family_key)` 主键只会制造唯一冲突
重试);③ 排在任何外部调用之前(防重状态先落库再调接口,进程半路死掉重跑拿回同一
个号)。

**一处有意的兜底**(真兜底三要件,写在 `mint_group_code` 里):`existing` 非空但那个号
在本店**已登给另一个家族键**时(同一族的家族键随 parent_asin 补全变过一次,是实见
形态),登记不进去,**仍返回 `existing`** —— 它就是这批兄弟在沃尔玛侧真实所在的组,
换个号发出去反而把一族劈成两组。触发记 warning + 计数,不静默。

**验收步骤**(所有者动作):

```bash
python cli.py db_init                     # 跑两遍:第二遍必须零变化(幂等)
python cli.py list_new --dry-run          # 逐行回显看「组号待发(家族键 B0…)」;
                                          # 同族两行的家族键必须相同,且输出里没有 vg_
python cli.py list_new -p limit=2         # 真跑一小批(含一个变体家族)
```

```sql
-- 发出去的号与登记表对得上,且组号里不含 ASIN
SELECT * FROM catalog.variant_groups ORDER BY created_at DESC LIMIT 20;
SELECT w.store, w.sku, w.variant_group_id, g.family_key
  FROM catalog.walmart_items w
  JOIN catalog.variant_groups g
    ON g.store = w.store AND g.group_code = w.variant_group_id
 WHERE w.variant_group_id <> '' ORDER BY w.last_seen_at DESC LIMIT 20;
```

- **同族第二批上架进同一个组**:先上家族里的 1 个,回执成功、catalog_sync 观测到
  `variant_group_id` 之后,再上同族第 2 个 —— 两条的 `variantGroupId` 必须**一模一样**,
  且 `catalog.variant_groups` 里这一族**只有一行**。
- **存量家族不回改**:挑一个已在架的 `vg_…` 家族补上一个新成员,新成员发出去的仍是
  那个 `vg_…`,登记表里这一族的 `group_code` 也是它(不是新抽的 G 号)。

### 9.14 同店双挂不拦节奏闸(2026-09-07,所有者决定)

**所有者原话**:

> 不行,它就是存在于后台,删除也删除不掉,严重耽误的项目推进速度。逻辑改一下,
> 如果双挂,不应该影响我们后续的修改计划,双挂的就让他继续挂着,等到我其他的
> 处理完了,我再回头处理他,中途不重复提交这种双挂的就可以。

**现场**:A131吕灿荣 有 2 条改码被判成「同店双挂」(新码与旧码**同时在架**:
`B078G54MD6→AVCR7Y3X2JFZ`、`B07BFQNMNG→AJSSQRJBRSN6`)。改前双挂只告警、台账行
**留 pending**,于是 `_stage_cap` 的节奏闸把它们数成"该店还有 2 条改码未定案",
整店上限压成 **0** —— 后面 500 条一条都发不出去。而所有者在 Seller Center 后台
**删旧码也删不掉**(僵尸列表:列表接口把已删档案照旧吐回,单条 GET 404 而列表
PUBLISHED,见 `docs/backlog.md` §十三,本次**不碰**)。两件事叠起来,那 2 条删不掉
的行等于把整店改码永久停摆。

**为什么改**:节奏闸的原意是「上一批的账没清就别发下一批」—— 账没清指的是
"我们还不知道结果"(pending)或"判不出、要人看"(stalled)。双挂**不是不知道**:
我们知道得很清楚(新码上去了、旧码没下来),只是**处置只能人工**(写操作永不
自动兜底,本工作流不许自动去删那条旧 listing)。拿一条已经知道结论、而且暂时
删不掉的行去拦住其余全部改码,是把"要人回头处理一条"放大成"整店停工"。

**改了什么**(四点,都在 `workflows/sku_migrate.py`):

1. **台账新增持久状态 `double`**(常量 `_DOUBLE`)。`_settle` 判词为 double 且真跑时,
   走新 SQL `_SQL_LEDGER_DOUBLE` 把该行 status 写成 `double` 并留 error 文案(仿
   `_SQL_LEDGER_STALLED`);**幂等**(`WHERE id = … AND status <> 'double'`,配合
   `_SQL_OBSERVE` 选出的当前 status,已是 double 的行连 UPDATE 都不发)。
   `--dry-run` 只报 `[DRY-RUN] 将记 double …`,一行库都不写。
   **不写 settled_at**:double 不是终态,是"挂在那儿等人回头处置"的过程态。
2. **double 行继续参与每轮定案**:`_SQL_OBSERVE` 的面从 `status = 'pending'` 改成
   `status IN ('pending', 'double')`,并把 `m.status` 选出来供幂等判断。
   `_verdict` 的**六条规则与顺序一字未改** —— 所有者哪天在后台把旧码删掉、
   catalog_sync 记了缺席,下一轮自然落到规则 (a)「新码在架 ∧ 旧码缺席」给
   confirmed,弃旧码 / UPC 改标 / 处置迁键 / 节点库存清行全走现有定案路径,
   **不需要任何新逻辑**;仍是双挂就仍判 double(不再写库,只计数)。
3. **节奏闸不数 double**:`_SQL_STAGE` 的 `open` 仍是
   `count(*) FILTER (WHERE status IN ('pending', 'stalled'))` —— 不含 double。
   这一条本来就成立,现在把"这是所有者的决定"写进了 SQL 注释与 `_stage_cap`
   的 docstring,并加了守门用例钉住 `'double'` 不在那个 FILTER 里。
   **`stalled` 继续拦**(所有者没说放它:超期判不出是"我们不知道发生了什么")。
4. **double 的旧码永不重复提交**:候选判据「无未了结改码台账」的
   `m.status IN ('pending', 'confirmed', 'stalled')` 加上 `'double'`。
   ⚠ 部分唯一索引 `sku_migrations_open_uidx`(`WHERE status='pending'`)从此
   **不再覆盖 double 行**,这是**可接受的取舍**:防第二条台账靠的就是这条候选
   判据(索引只保「同一 (店,旧码) 不许有两条 pending」那一半),已写进
   `refdata/schema.sql` 与 `docs/db_schema.md` 的注释。

**摘要文案**:逐条告警改成「⚠ 同店双挂 {旧码→新码}:新码与旧码同时在架 —— 已记
double,**不拦后续提交、不会重复提交**,回头人工处置」;首行的「⚠ 同店双挂 N」
**保留** —— 所有者要回头处理它们,首行那个数就是待办清单。`_stage_cap` 那句
「该店还有 N 条改码未定案(pending/stalled)」不变。

**执行序为什么当轮就生效**:`run()` 里 `_settle` 先跑、`_stage_cap` 后读,而
`_settle` 的每一次写都走**自己的短事务**(`db.pg_conn()` 另开连接,退出即 commit),
与本轮那条只读连接不是同一个事务;PG 默认 READ COMMITTED,所以后面那条
`_SQL_STAGE` 看得见刚 commit 的 double。⇒ A131 那 2 条会在**同一次运行**里走完
「记 double → open 少 2 条 → 节奏闸放开 → 当轮提交后面那批」,不必多等一轮。

**不变的是什么**(别顺手改掉):

- **身份层一个字不动**:`catalog.listing_sources` 的旧行仍是 `replaced_by=新码` 的
  在途态、新码仍是活码 —— 换的只是过程账的状态,不是身份的结论。不弃码、不复活。
- **`_verdict` 的六条规则与优先级不动**(顺序即语义)。
- **stalled 仍拦节奏闸**。
- **不加任何自动处置双挂的动作**:不自动删旧 listing、不自动补交、不换方法重试
  (写操作永不自动兜底;换方法重试 = 重复提交制造机)。

**回头处置的操作**(所有者那天有空时):

```bash
# ① 在 Seller Center 后台把旧码那条 listing 删掉(僵尸列表问题另议,backlog §十三)
python cli.py catalog_sync -p store=A131吕灿荣            # ② 让观测记下旧码缺席
python cli.py sku_migrate -p store=A131吕灿荣 -p settle_only=1   # ③ 自动转 confirmed
```

```sql
-- 现在还有哪些双挂等着回头处置
SELECT store, old_sku, new_sku, feed_id, error
  FROM listing.sku_migrations WHERE status = 'double';
```

### 9.15 影子双挂定案 + 维护账随码迁移(2026-09-08,所有者实证)

两件事同一天从生产数据里查出来,都是「账比不了」而不是「事没做成」——**改码其实
早就生效了,只是我们拿来比对的那一行不是它**。

#### 一、现场证据(所有者按 SQL 逐条对过)

**A131吕灿荣:43 条改码台账停在 `double`(§9.14 的持久状态),其中 41 条新旧码
`wpid` 相同。**

`wpid` 是沃尔玛给**每条 listing** 的内部 id。改码通道 MP_ITEM_MATCH 是「同 GTIN +
新 SKU + `processMode=REPLACE`」在**同一个 item 上原地换码**(§9.12 所有者 Seller
Center 实测:新旧码 wpid 相同 `5FK5P1SAT7OM`、库存跟着过来、价格不变、旧码
GET 404),所以:

> **wpid 相同 = 这两个码是同一条 listing = 改码已经生效**;
> 「旧码还在架」是 **2026-08-28 起沃尔玛列表接口把已删档案照旧吐回**
> (僵尸列表,`docs/backlog.md` §十三:列表 PUBLISHED 而**单条** GET 404)的影子。

A085朱丽霖 早先那条实测同款:旧码单查 404、新码 wpid 与旧码相同。
按 §9.14 的现状,这 41 条**永远**停在 `double` —— 旧码永不弃码、UPC 永不改标、
处置建议永不迁键,而且没有任何东西会报。

**剩下 2 条是真双挂,仍留 `double` 交人工**(它们正是"为什么证据要两条"的活样本):

| 旧码 | 情形 |
|---|---|
| `B08DR3TKQK` | 新旧两个 wpid **都** PUBLISHED —— 真的多了一条 listing |
| `B09L3WXJ96` | 旧码是 **RETIRED 死档**,新码是**新建**的 item(wpid 不同) |

**A171罗尹鸿:改码定案后摘要点名「旧码名下还有 executing 的 `price`,不迁,由
`expire_executing` 判成 ineffective 收尾」。** 所有者查证:处置 691466
(`price`,建议 15.71 → 16.78,`executed_at` 2026-09-07 14:00)仍是 `executing`;
而新码 `AJ5K52FK5SME` 在 2026-09-08 06:41 的观测里 `price=16.78`,旧码同一轮记了
缺席,沃尔玛后台只有新码。⇒ **改价早就生效了,只是这条账拿旧码那一行比,而那一行
已经不在观测面上**(`_MAINT_OPEN_SQL` 是 `JOIN catalog.walmart_items`,旧码缺席就
JOIN 不上)。后果:要等 3 天 `expire_executing` 超期放行才落定(还落成
`ineffective` —— 与事实相反),而且**每一次改码定案都把它点名一遍**。

#### 二、改动 A:影子双挂按「同 wpid + 旧码单查 404」定案 `confirmed`

`workflows/sku_migrate.py`:

1. `_SQL_OBSERVE` 增选 `nw.wpid AS new_wpid, ow.wpid AS old_wpid`。
2. `_settle` 对 **同 wpid 的双挂行**(`_shadow_candidates`:新码在架 ∧ 旧码没缺席 ∧
   两码 wpid 相同且非空;**含状态已经是 `double` 的行** —— `_SQL_OBSERVE` 本来就取
   pending ∪ double)**逐条单查旧码** `api.items.get_item(store, old_sku)`:
   404 ⇒ 行上挂 `old_probe=404`,200 ⇒ `old_probe=200`。
   走 api/items → api/_client 的**每店固定出口代理**与 `items.get` 桶(900/min),
   **严禁直连**;店铺凭证 `services/stores.load_stores([store])` **按需加载、一轮一次**
   —— 一条影子候选都没有就**一次凭证都不读、一次沃尔玛都不调**。
3. `_verdict` 在规则 (b) 之前插一条 **(a′)**:
   `new_present ∧ ¬old_gone ∧ old_probe == 404 ∧ old_wpid == new_wpid ⇒ confirmed`,
   理由「新码在架,旧码与新码同 wpid 且单查 404 —— 列表接口的影子,改码已生效」。
   `_verdict` **仍是纯函数**(探测结果由 `_settle` 喂进来)。
4. 定案后走**现有** `_confirm` 路径(弃旧码 `sku_update` / UPC 改标 / 处置迁键 /
   清节点行 / 台账 confirmed),**不新增任何写动作**。
5. 摘要:首行新增「**影子改码 N**」,与「⚠ 同店双挂 N」**分开报** —— 前者是本轮
   自救掉的,后者是仍要所有者回头处理的待办;明细里每条影子定案各一行。
   dry-run **照样探测**(只读)并报 `[DRY-RUN] 将定案 … = confirmed(…同 wpid…
   单查 404…影子…)` —— 空跑正是人眼确认"这批到底是影子还是真双挂"的那一步。

**为什么证据必须是两条**(缺一不可,这是本节最容易被下一个人"简化"掉的地方):

- **只有 wpid 相同**:只说明"曾经是同一条 listing",不说明旧码现在没了。
  真双挂里 `B08DR3TKQK` 的两个 wpid 不同,但一个 wpid 相同却旧码真在架的组合
  (改码 feed 发过、后台又被人手工建回旧码)不是不可能 —— 单查 200 就是它的样子。
- **只有单查 404**:不说明"新码是由这个旧 item 换来的"。`B09L3WXJ96` 就是反例:
  旧码是 RETIRED 死档(单查很可能 404),而新码是**新建**的另一条 item ——
  按 404 一条就定案,等于把一条从未生效的改码记成成功,旧码从此弃用、UPC 改标,
  **而且不报错**。
- **单查是"读",不违反"定案只信观测"**:它本身就是一次逐条观测,而且比列表那一轮
  更新更准。这条纪律防的是"拿回执当判据",与逐条 GET 是两回事。

**fail-closed 的三条分支**(探不出来一律**不猜**,那些行照旧判 `double`,
并在摘要里**点名一次**「影子探测失败(异常类名),N 条按 double 处理」):

| 分支 | 处置 |
|---|---|
| `load_stores` 抛(飞书抖 / 快照也没有) | 整批不探测,点名 |
| 店不在可调用列表(未启用 / 没配代理 / 没凭证) | 整批不探测,点名 |
| 单条 GET 抛(429 / 超时 / 代理波动) | **只有那一条**不挂探测结果,其余照常定案;按异常类名归并计数点名 |

反过来(探不出来就当 404)会拿一次网络抖动去弃码、改 UPC、迁处置键,而且全程
不报错 —— 那是**不可逆**的一侧。判不准就判活(conventions §五)。

**旧码那一行 `catalog.walmart_items` 怎么办:本次什么都不做**(结论,别顺手加)。
查过 `services/walmart_catalog`:`_UPSERT_SQL` 的 `ON CONFLICT DO UPDATE` 里写死
`missing_since = NULL` —— 只要下一轮 catalog_sync 在**列表**里还看得见旧码
(僵尸列表正是这样),我们标的 `missing_since` **当轮就会被翻回 NULL**。
所以本次**不标** `missing_since`:标了也留不住,徒增抖动(还会让
`product_events` 的缺席/复现事件来回刷)。如实记下代价:

- 影子行仍留在维护面与问题面上(`maintenance_intents` / `problem_scan` 的取数只看
  `missing_since IS NULL` + `published_status`,**不看登记簿的 `abandoned_at`**),
  于是维护 feed 对它仍会失败、problem_scan 仍会对它建议删除;
- 那批失败正是 `docs/backlog.md` §十三(僵尸列表与破坏类处置卡死)的病灶,
  **根治归那一条**(建议修法已经写在那里:对可疑行逐条单查,404 ⇒ 当观测缺席)。
  本节只做改码链自己的自救,不越界去动 catalog_sync / problem_scan 的取数。

#### 三、改动 B:旧码名下**维护类** `executing` 行迁到新码

`services/dispositions.py`:`rekey_suggested` **改名 `rekey_open`**(函数名再叫
suggested 就与它做的事对不上;**不留别名、不留两个函数** —— 一个能力一条实现路径,
§六。全仓调用点与测试一起改)。

- 迁的面:**全部 `suggested` + 维护组(`MAINT_ACTIONS`:price/inventory/title)的
  `executing`**。条件与 suggested 那半完全一样:新码名下同 `(store, sku, action)`
  已有未落定行(`_REKEY_TAKEN_SQL`)⇒ **不迁不删、点名人工**;`asin` 列 coalesce 补;
  迁过的行 `detail` 留 `rekeyed_from` / `rekeyed_at`(回头查"这条账当初打在哪个码上"
  必须有答案)。
- **`executed_at` 一个字不改**:宽限期(`MAINT_SETTLE_GRACE_HOURS` = 2h)照旧从
  **原提交时刻**算 —— 刷新它等于把"提交多久了"重新计时,而那条 feed 早就发出去了。
- **破坏组(`DESTRUCTIVE_ACTIONS`)的 `executing` 照旧不迁**:它们的判据是
  `product_events` 的 `delete_verified`(「这个 SKU 不见了」),改码后旧码正好消失,
  搬到新码上就是拿另一个身份去等一个已经被污染的判决。它们本不该出现(候选判据
  「无未了结破坏建议」挑选时就剔掉了),真出现了由 `executing_actions_on` 点名人工。
- **迁过去之后不加任何新的落定代码**:`settle_maintenance` 的现有规则(新码
  `last_seen_at > executed_at + 2h` 之后按现值比 `detail->>'new'`)自然收尾 ——
  A171 那条 691466 会在下一次 maintenance 的落定里判成 `confirmed`(16.78 == 16.78)。
  `expire_executing` **不动**,仍是最终兜底。
- `workflows/sku_migrate._confirm`:先取 `executing_actions_on`(**必须在迁键之前**,
  之后就读不到了),再调 `rekey_open`;摘要按组分开说 —— 维护组「旧码名下 executing
  的 {actions} **已迁到新码**,由维护链按新码观测落定」,破坏组的告警**原样保留**,
  撞车的仍走「请人工处置」那一句。

**边界①为什么可以改**:原口径「executing 一律不碰,搬键等于把判决对象换掉」的
**前提**是"新码与旧码是两个不同的 item"。在 MP_ITEM_MATCH 的原地换码下这个前提
不成立:同一个 wpid,**新码的现值就是那条 feed 作用的对象**。所以搬过去不是换判决
对象,而是把账挪到唯一还比得了的那一行。破坏组的前提没变(它比的是"消失没消失",
而消失这件事被改码本身污染了),所以那一半一个字不改。

#### 四、纪律与不变量(别顺手改掉)

- 依赖方向仍是 workflows → services → api:**"是不是影子"这个业务判断在 `_verdict`**,
  api 层只回 `None`(404)/ `dict`(200)(铁律 2)。
- 写库函数**不设 `dry_run` 形参**;探测是读,所以 dry-run 可做、也**必须**做。
- **不加任何对影子行的自动删除**(写操作永不自动兜底);真双挂仍留 `double` 人工处置。
- 不动 catalog_sync / problem_scan / 维护链的取数 SQL。
- `_verdict` 的其余六条规则与优先级一字未改(顺序即语义)。

#### 五、死档不改码(2026-09-09 追加)

上面第三节说的是"改码**之后**那本账怎么收";这一节说的是"这一条**根本不该**改码"。

沃尔玛侧已不存在的 item(最近一次 DELETE/RETIRE 回执码 ∈
`registry.resources.WALMART_ERR_ITEM_GONE`:Invalid Item ID / deleted-retired /
already de-activated / QARTH「No matching record」)发 MP_ITEM_MATCH **不是改码,
是新建一条 listing** —— 它匹配不到任何现存 item,于是照着载荷建了个新的。

实证:**A131吕灿荣 B09L3WXJ96** 的那条**真双挂**(与本节第一段讲的"影子双挂"
不是一回事,两码 wpid **不同**):旧码是 RETIRED 死档、新码是这样新建出来的 item,
两条同时挂在店里。回执全绿、摘要正常,没有任何东西会说它建了个新品。

落地:`workflows/sku_migrate._candidates` 的**第三道后置闸**(与在途闸、撞号闸并列,
`_pick_report` 里是第七类落选理由:「沃尔玛回执说该 SKU 已经不在了(死档),改码会
新建 listing —— 不迁」),摘要首行单独报「死档 N」。判据经
`services/feed_track.receipt_blocked`,与 `problem_scan` 的死档闸**同一个函数、同一份
SQL**;dry-run 同样生效(三道后置闸全是只读判据)。

**不进 `_CONDS`** 是有意的:那份判据文本是"目录 × 登记簿"的行判据,而这一条读的是
`ops.feed_items` 的回执历史(还要经 `catalog.sku_aliases` 继承一跳)—— 塞进候选 SQL
等于在判据文本里再嵌一段两层子查询,而它的唯一出处已经在 services 里。

目录里那些死档行**本身**仍未根治(`walmart_items.missing_since` 仍是 NULL,维护链
对它们照旧发 feed 照旧失败),归 `docs/backlog.md` §十三的后半段。

#### 六、后置闸不占本轮名额(2026-09-09 追加)

A131吕灿荣 实证:`-p limit=500` 只发出 45 条 —— 候选 SQL 先按 `LIMIT 500` 截断,
在途闸再从这 500 里剔掉 455,被剔的行**白占了本轮名额**,候选面上还有几百条合格的
一条没轮到。改法:候选 SQL 按 `CANDIDATE_FETCH_CAP`(20000,远大于任何一家店的
在架行)整店取回,三道后置闸(在途 feed / 死档 / Product ID 撞号)过完**再**按
`-p limit` 截,截掉的在摘要里说出口(「合格候选 N 个,只发前 M 个,其余下一轮」)。
上限 0 的早退不变,`_pick_report` 的「没轮到」理由不变。
