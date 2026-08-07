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
| 店铺凭证表 | 店铺 ClientId/Secret + 代理配置 | 飞书(人工维护)→ 程序读 + 本地快照兜底 | 代码就绪(services/stores.py);**待用户创建**,建好后 app_token/table_id 填 .env |
| 上架登记表 | 人工登记待上架 ASIN | 飞书登记 → 同步进 listing.tasks → 结果回写 | 待创建 |
| 下架登记表 | 人工登记待下架 ASIN+店铺 | 同上模式 | 待创建 |
| 错误商品记录 | 问题商品每日汇总(展示) | Postgres 权威,飞书是展示投影 | 沿用旧表或新建 |
| 店铺日报 KPI | 每日 KPI 展示 | Postgres 权威,飞书展示 | 待创建 |
| 订单审核结果 | 审核结论展示与人工复核 | Postgres 权威,飞书展示+人工改判回收 | 待创建 |
| 在线产品总表(新) | 沃尔玛在线商品投影(约 13 万行) | Postgres(catalog.walmart_items)权威,程序整表重写 | **电子表格**(非 bitable:超 5 万行套餐上限);用户已建,token/sheet_id 待填 .env(FEISHU_ONLINE_SHEET_TOKEN / FEISHU_ONLINE_SHEET_ID);列序登记在 registry ONLINE_PRODUCTS_SHEET;**只写在架行**(缺席商品不进表,2026-08-07 定稿),missing_since 列因此恒空、仅保列序稳定 |
| 订单中心六表(订单中心V1 应用) | 主订单/销售/采购/售后/绩效/对账 | Postgres(orders schema)权威,order_center_push 键对齐同步;主订单表/采购信息为人工域,程序只补键 | 代码已对齐用户既有表头(2026-08-06);售后表需补「唯一键」字段;app_token + 6 个 table_id 填 .env |
| 商品停用删除表 | product_clear 驱动表 | 登记类:运营填 A~D(store/sku/停用或删除/操作原因),程序写 E~G(feedid/操作日期/结果);状态权威在 ops.feed_log | **电子表格**(列序契约);待用户创建,token/sheet_id 填 .env(FEISHU_RETIRE_SHEET_TOKEN / FEISHU_RETIRE_SHEET_ID) |
| 上下架限额表 | **按店铺分行**的单日上/下架限额(店铺/fba区间1/fba区间2/FBM区间1/FBM区间2/上架限制/下架限制/库存特殊要求) | 飞书人工维护 → 程序读(product_clear 读「下架限制」,店铺不在表内退默认值并告警;未来 listing 读「上架限制」) | 多维表格;代码已接入,token 待填 .env(FEISHU_LIMITS_APP_TOKEN / FEISHU_LIMITS_TABLE_ID) |

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
  最后写库时间)。**不碰**:脚本审核、亚马逊单价、采购数量、库存数量、配送方式、
  配送时长、建议采购日期、卖家店铺名、产品截图、采购方、限价、币种、主订单表、父记录。
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
