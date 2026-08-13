# 飞书多维表格规范

> 多维表格是本系统的"人机界面":人看数据、登记任务、改配置都在飞书;
> 程序的事实来源是 Postgres。谁是权威以本文档每张表的标注为准。

## 多维表格的关键特性(设计依据)

- **按字段名(表头)索引**,不按列号。读写记录形如 `{"fields": {"店铺": "A085", ...}}`。
  → 表头改名会静默弄坏硬编码引用,因此**字段名只准写在 registry/resources.py 的
  字段常量里**,业务代码禁止出现字段名字符串字面量。
- 字段有类型(数字/单选/多选/日期毫秒时间戳/复选框/人员/超链接),读回即结构化数据。
- 服务端过滤查询:按字段名下条件(and/or 组合)+ 排序 + 分页(每页 ≤500),
  不要整表拉回自己扫。
- 每行有 record_id,更新按 record_id;需要"同步"语义的表必须设计去重键字段。
- 写限制:批量增/改 ≤500 条/次,整批全成功或全失败;写 QPS 约 10/s,同表串行写。

## registry 登记格式(约定)

```python
# registry/resources.py(实际实现)
STORE_CREDENTIALS = Bitable(
    name="店铺凭证表",
    app_token=os.environ.get("FEISHU_STORE_TABLE_APP_TOKEN", ""),
    table_id=os.environ.get("FEISHU_STORE_TABLE_ID", ""),
    fields=_fields(store="店铺", client_id="ClientId", client_secret="ClientSecret",
                   proxy_type="代理类型", proxy_host="IP地址或域名", proxy_port="端口",
                   proxy_user="IP登录账号", proxy_pass="IP登录密码", enabled="启用"))
```

代码里永远 `STORE_CREDENTIALS.fields.client_id`。飞书改表头 = registry 改一行。
app_token/table_id 走 `<DATA_ROOT>/.env` 登记(键名在 registry 声明,值不进 git);
未登记时 `Bitable.require()` 抛错并提示,不会静默空跑。

## 表格清单(随建随登记,执行 AI 维护)

| 表 | 用途 | 权威方 | 状态 |
|---|---|---|---|
| 店铺凭证表 | 店铺 ClientId/Secret + 代理配置 | 飞书(人工维护)→ 程序读 + 本地快照兜底 | **已建成接通**(2026-08-05 生产验收,ping_stores 经此读 49 家凭证) |
| 上架登记表 | 人工登记待上架 ASIN | —— | **已作废**:由「上架表(新)」承接(下方);listing.tasks 表同期废弃(见 docs/backlog.md 第四节) |
| 下架登记表 | 人工登记待下架 ASIN+店铺 | —— | **已作废**:由「商品停用删除表」承接(product_clear 在用) |
| 错误商品记录 | 问题商品每日汇总(展示) | —— | **已裁撤不迁**(所有者拍板 2026-08-11:Step 3/4/5 报表不再维护飞书投影,需要数据直接读库;累计真值在 ops.cleanup_seen_categories) |
| 店铺日报 KPI | 每日 KPI 展示 | Postgres 权威,飞书展示 | 方案已定稿(2026-08-08):看板两页(总览/历史),registry 已登记 KPI_BOARD_OVERVIEW/HISTORY;**待建表 + 填 .env + 首刷** |
| 订单审核结果 | 审核结论展示与人工复核 | Postgres 权威,飞书展示+人工改判回收 | **已由订单中心销售订单表的审核列承接**(ORDER_SALES_AUDIT,只更新不新建行;2026-08-10 生产回写 151 行) |
| 店铺KPI(旧 workbook) | ① 总览页=影刀输入投影(daily_report yingdao=1 只写 A:H,过滤空 sellerId——A147 事故防线);② 每店分页=KPI 历史导入源(kpi_history_import) | Postgres 权威;此表仅为影刀衔接与历史迁移保留,72 张分页停更归档后随影刀改造退役 | **电子表格**(存量);registry KPI_SHEET;.env FEISHU_KPI_SHEET_TOKEN / FEISHU_KPI_OVERVIEW_SHEET_ID |
| 在线产品总表(新) | 沃尔玛在线商品投影(约 13 万行) | Postgres(catalog.walmart_items)权威,程序整表重写 | **电子表格**(非 bitable:超 5 万行套餐上限);**已接通**(catalog_sync 47 店全量验证;token/sheet_id 在 .env);列序登记在 registry ONLINE_PRODUCTS_SHEET;**只写在架行**(缺席商品不进表,2026-08-07 定稿),last_seen_at/missing_since 两列不投影(追踪在 PG + 事件账本) |
| 订单中心六表(订单中心V1 应用) | 主订单/销售/采购/售后/绩效/对账 | Postgres(orders schema)权威,order_center_push 键对齐同步;主订单表/采购信息为人工域,程序只补键 | 代码已对齐用户既有表头(2026-08-06);**app_token/table_id 已在 .env 生效**(2026-08-10 审核列生产回写 151 行为证);遗留待办:售后表补「唯一键」字段 |
| 上架表(新) | listing 主驱动表(L2) | 运营填 A/B/D~G 人工域,机器列由 list_new/反哺器写;21 列 A~U(较旧 26 列砍 状态跟踪/最近跟踪日期,产品事件账本承接;U=核验 UPC 一致性) | **电子表格**:「在线产品总表」内工作表(所有者建 2026-08-07);sheet_id 填 .env FEISHU_LISTING_SHEET_ID |
| 跟卖表(新) | match_listing 驱动表(替代旧 xlsx 输入,单路飞书读) | 运营填 A=UPC C=售价 D=重量 E=店铺;脚本填 B=SKU F=跟卖状态 G=匹配GTIN H=上架时间 I=feedId;J/K 由 feed_poll 反哺器回填 | **电子表格**:「在线产品总表」内工作表(所有者建 2026-08-07);sheet_id 填 .env FEISHU_MATCH_SHEET_ID |
| 沃尔玛类目表 | 风控·类目准入(禁售/中国卖家可做) | risk_sync 每日镜像入 PG(catalog.risk_product_types,只增改不删);上架否决闸读库不读表;**表格随时会停用**(所有者 2026-08-07),停用后 PG 唯一权威 | **wiki 承载电子表格**(api/feishu 自动解析节点 token);.env FEISHU_RISK_PT_WIKI_TOKEN / FEISHU_RISK_PT_SHEET_ID;10 列 A~J |
| 黑名单品牌总表 | 风控·品牌黑名单**总清单**(各渠道由所有者人工归拢;2026-08-11 换新表,旧「禁止品牌收集」退役) | **飞书→PG**:risk_sync 镜像入 catalog.brand_blacklist(casefold 键,upsert 不碰 pushed_at);否决闸读库不读表 | **wiki 承载电子表格**;.env FEISHU_BRAND_WIKI_TOKEN / FEISHU_BRAND_SHEET_ID;列 品牌名/来源/入库日期(/SKU 可选) |
| 黑名单ASIN | 永久禁止类 ASIN(B/C/E/F/G/K)投影 | **PG→飞书**:blacklist_push 按 pushed_at 水位只追加;库(catalog.asin_blacklist)的全量映射 | **wiki 承载电子表格**(所有者建 2026-08-11);.env FEISHU_BLACKLIST_WIKI_TOKEN / FEISHU_ASIN_BLACKLIST_SHEET_ID;3 列 黑名单ASIN/来源/日期 |
| 黑名单品牌(后台报错集成) | **只承接沃尔玛后台问题商品拿到的品牌**——归拢总表的一条增量渠道(与总表方向相反,别混) | **PG→飞书**:blacklist_push 只推自产行(src_sku IS NOT NULL);总表镜像行不回推 | **wiki 承载电子表格**(所有者建 2026-08-11);.env FEISHU_BRAND_ERR_WIKI_TOKEN(可空,回落 FEISHU_BLACKLIST_WIKI_TOKEN)/ FEISHU_BRAND_ERR_SHEET_ID;4 列 品牌/来源/入库日期/SKU |
| UPC池(新) | UPC 号段注入与领用展示 | 运营填 A=UPC B=放入日期注入;upc_sync 校验(首位白名单 016789)入库并回写 C=状态(已领/已用/冲突/非法前缀)D=店铺 E=SKU F=上架日期;**PG(catalog.upc_pool)权威**,领用/回收由上架主链在事务内操作 | **电子表格**:「在线产品总表」内工作表(所有者建 2026-08-07);sheet_id 填 .env FEISHU_UPC_SHEET_ID |
| 维护记录 | maintenance 流水账(只追加) | 程序唯一写入方:提交时追加(feed 路径 F=feedid H=处理中;PUT 路径 F=sync H=当场落定),H/I 由 feed_poll 反哺器按 ops.feed_items 回填;水位在 ops.cursors('maint_sheet') | **电子表格**:「在线产品总表」spreadsheet 内的「维护记录」工作表(所有者已建 2026-08-07;bitable 5 万行上限装不下);表头 店铺/SKU/动作/旧值/新值/feedid/日期/结果/报错;sheet_id 填 .env FEISHU_MAINT_SHEET_ID |
| 黑名单邮编 | 订单审核·钓鱼检测(所有者定稿 2026-08-09:**只匹配邮编**,旧系统的黑名单地址/街道双向 substring 整套不迁) | 每次运行现读,不入库;A 列邮编、无表头,zip+4 自动收敛到前 5 位;范围按实际行数取(旧系统写死 A1:A500,超出静默截断→漏放行) | **wiki 承载电子表格**;.env FEISHU_ZIP_BLACKLIST_WIKI_TOKEN / FEISHU_ZIP_BLACKLIST_SHEET_ID |
| 采购方 | 订单审核·按 配送方式 + 亚马逊单价区间 选采购方与汇率 | 每次运行现读,不入库;**一行都没启用就直接失败**(拿旧配置继续算钱比不出结论危险);每行实际套用的采购方/汇率落 audit_detail 可追溯 | 多维表格;.env FEISHU_SUPPLIER_APP_TOKEN / FEISHU_SUPPLIER_TABLE_ID;6 列 采购方/配送方式/价格区间起/价格区间止/汇率/是否启用(配送方式填 FBA\|FBM;是否启用支持复选框或「是」;区间只填一端=以上/以下;多个候选取**最低汇率**) |
| 商品停用删除表 | product_clear 驱动表 | 登记类:运营填 A~D(store/sku/停用或删除/操作原因),程序写 E~H(feedid/操作日期/结果/报错);状态权威在 ops.feed_log,G/H 由 feed_poll 反哺器或 product_clear 回写(2026-08-07 定稿:feed 结果统一交轮询回填) | **电子表格**(列序契约);已建,token/sheet_id 在 .env(FEISHU_RETIRE_SHEET_TOKEN / FEISHU_RETIRE_SHEET_ID) |
| 上下架限额表 | **按店铺分行**的单日上/下架限额 + 店铺目标 + 渠道限制(店铺/fba区间1/fba区间2/FBM区间1/FBM区间2/上架限制/下架限制/库存特殊要求/**目标销售额/目标订单/单店最大在线数**(2026-08-12 建列,销售额与订单为**日目标**,最大在线数是总容量上限)/**配送限制**(2026-08-13 建列,填 fba/fbm,一店一渠道的权威,未填=不接自由流分配)) | 飞书人工维护 → 程序读(product_clear 读「下架限制」;list_new 读「上架限制」;分配引擎读目标三列与配送限制) | 多维表格;代码已接入,token 待填 .env(FEISHU_LIMITS_APP_TOKEN / FEISHU_LIMITS_TABLE_ID) |

## 订单中心六表同步契约(order_center_push ↔ 用户既有「订单中心V1」应用)

应用 token 填 `FEISHU_ORDER_APP_TOKEN`;六张数据表的 table_id 分别填:
`FEISHU_ORDER_MAIN_TABLE_ID`(主订单表)/ `FEISHU_ORDER_PURCHASE_TABLE_ID`(采购信息)/
`FEISHU_ORDER_SALES_TABLE_ID`(销售订单)/ `FEISHU_ORDER_RETURNS_TABLE_ID`(售后订单)/
`FEISHU_ORDER_PERF_TABLE_ID`(绩效订单)/ `FEISHU_ORDER_SETTLE_TABLE_ID`(对账明细)。

总规则(registry 只登记程序拥有的字段,未登记的列程序碰不到):

1. **任何表程序都不删行**:主订单表是永久枢纽、行间有关联字段,删行断链。
   滑出窗口(销售/售后/对账默认 90 天)的行只是停止刷新,不会消失。
2. **键列人工不得改动**:销售/主订单/采购/对账键 = `order_line_id`,
   绩效键 = `perf_key`(PO\|指标),售后键 = `唯一键`(RMA号\|order_line_id)。
3. **售后订单表需新增一个「唯一键」文本字段**(同一订单行可多次售后,
   首列 order_line_id 不唯一,道理同绩效表的 perf_key)。
4. **类型要求**(与现有列核对):日期类字段(下单时间/状态更新时间/预计发货/
   预计送达/退货创建/退货截止/结算日期/拉取时间)须为**日期**类型;金额/数量/
   佣金率为**数字**;keep-it单、计入绩效为**复选框**;其余文本。类型不符会写入报错。
5. **首跑前清空旧数据行**:v1 草稿留下的旧行键格式不同(含店铺哈希),
   程序不删行,旧行会变成永远不更新的死行——复制/沿用旧表请先清空数据。

各表程序拥有的列(此外的列——人工列、采集列、关联字段、公式列——程序一律不写):

- **主订单表 / 采购信息**:只有 `order_line_id`(缺行补建,只写这一列;
  既有行永不更新/删除)。
- **销售订单**:order_line_id、下单时间、店铺、采购订单号、行号、SKU、商品名称、
  数量、销售状态、审核状态、状态更新时间、预计发货时间、预计送达时间、商品金额、
  运费金额、取消原因、行内退款金额、退款备注、承运商、物流单号、物流链接、
  收件人姓名、电话、地址1、地址2、城市、州、邮编、国家、拉取时间(=order_sync
  最后写库时间)。**不碰**:采购数量、币种、主订单表、父记录。
- **销售订单(审核列)**:同一张表的第二个登记条目 `ORDER_SALES_AUDIT`,
  由 **order_audit** 独占写:脚本审核、亚马逊单价、库存数量、配送方式、配送时长、
  卖家店铺名、产品截图、采购方、限价、**标题相似度**。**不碰**:建议采购日期
  (人工域,所有者定稿 2026-08-09 明确排除)。
  - 分成两个条目是为了分家所有权:同步只覆盖载荷里出现的列,
    order_center_push 的载荷没有审核列 → 拉单冲不掉审核结论,反之亦然。
  - 「审核状态」是两条工作流都会写的**唯一一列**,但两边取的都是
    `orders.order_lines.audit_status` 同一个值,不会打架。
  - order_audit **只更新不新建行**(`feishu.update_by_key`):建行是
    order_center_push 的职责,否则会造出只有审核列、没有订单本体的半截行。
    尚未建出的行不报错,下一轮自然补上。
  - **类型要求**:亚马逊单价/库存数量/配送时长/限价/标题相似度为**数字**
    (标题相似度是 0~1 的小数,建议设 4 位小数),
    产品截图为**附件**(值 `[{"file_token": ...}]`,token 由
    `feishu.upload_media` 上传换取,与 app_token 绑定不能跨表复用),其余文本。
    「审核状态」若是**单选**字段,选项须含「✓ 通过 / 建议拒绝 / 待人工」三值,
    否则写入报错(见 services/order_audit.py 的结论常量)。
- **售后订单**:唯一键、order_line_id、下单时间、店铺、RMA号、客户订单ID、
  采购订单号、行号、SKU、售后状态、退款状态、退货方式、退款方式、总退款金额、
  退货原因、退货描述、退货截止日期、退货创建时间、状态更新时间、客户姓名、
  客户邮箱、数量、已退款数量、承运商、物流单号、keep-it单。**不碰**:主订单表、主订单表 2。
- **绩效订单**:perf_key、order_line_id(多行订单无法定位时为空)、下单时间、店铺、
  采购订单号、指标类型(emoji 展示名,日报同契约)、计入绩效、问题描述(最近一期
  报表行摘要)、绩效状态(影响中=仍出现在最近一期报表/已滚出窗口)、统计周期
  (首期 ~ 末期,共 N 期)、明细(最近一期原始行 JSON)、拉取时间。
- **对账明细**(跨账期按订单行合并;逐账期原始明细在 PG orders.settlement_lines):
  order_line_id、下单时间、店铺、采购订单号、行号、入账状态(已入账/已冲销/已退款/
  待入账)、结算净额USD/商品销售额USD/实扣佣金USD(跨账期合计)、佣金率/原始佣金USD/
  佣金优惠USD/优惠计划/账期(最近账期值)、结算日期(最近)、拉取时间。**不碰**:主订单表。

设计规则:
1. **登记类表**(人写程序读):程序同步后回写状态列,永不删除人写的行。
2. **展示类表**(程序写人看):权威在 Postgres,飞书表可随时整表重建;
   按 record_id 增量更新,用去重键字段对齐。
3. 每张表建好后,把 app_token/table_id/字段清单登记进 registry 并更新本文档。
4. 密钥类字段(ClientSecret、代理密码)只在店铺凭证表;该表访问权限收紧到最小人群。
