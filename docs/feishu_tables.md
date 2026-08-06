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
| 在线产品总表(新) | 沃尔玛在线商品投影(约 13 万行) | Postgres(catalog.walmart_items)权威,程序整表重写 | **电子表格**(非 bitable:超 5 万行套餐上限);用户已建,token/sheet_id 待填 .env(FEISHU_ONLINE_SHEET_TOKEN / FEISHU_ONLINE_SHEET_ID);列序登记在 registry ONLINE_PRODUCTS_SHEET |
| 订单中心四表 | 销售/售后/绩效/对账投影 | Postgres(orders schema)权威,order_center_push 键对齐同步 | 代码就绪;**待用户创建**(建表规格见下节),app_token/table_id 填 .env |

## 订单中心四表建表规格(order_center_push 写)

同一个多维表格应用里建 **4 张数据表**,应用 token 填 `FEISHU_ORDER_APP_TOKEN`。
字段名必须与下表**逐字一致**(registry 常量已按此登记);每表第一个字段是
去重键(同步对齐锚点),**人工不得编辑该列**。所有表都是程序独占的展示投影,
程序会删除数据库里已不存在的行——不要在这四张表里手工加行。

**表 1:销售订单**(table_id → `FEISHU_ORDER_SALES_TABLE_ID`;窗口默认近 90 天)

| 字段名 | 类型 | | 字段名 | 类型 |
|---|---|---|---|---|
| 行ID | 文本(去重键) | | 承运商 | 文本 |
| 店铺 | 文本 | | 物流单号 | 文本 |
| PO号 | 文本 | | 物流链接 | 文本 |
| 行号 | 文本 | | 售后状态 | 文本 |
| 客户单号 | 文本 | | 售后退款状态 | 文本 |
| SKU | 文本 | | 售后金额 | 数字 |
| 商品名 | 文本 | | 绩效指标 | 文本 |
| 数量 | 数字 | | 入账净额 | 数字 |
| 订单状态 | 文本 | | 入账状态 | 文本 |
| 状态时间 | 日期 | | 审核结果 | 文本 |
| 下单时间 | 日期 | | 商品金额 | 数字 |
| 取消原因 | 文本 | | 运费 | 数字 |
| 退款金额 | 数字 | | | |

**表 2:售后订单**(table_id → `FEISHU_ORDER_RETURNS_TABLE_ID`;窗口默认近 90 天)

| 字段名 | 类型 | | 字段名 | 类型 |
|---|---|---|---|---|
| 唯一键 | 文本(去重键,RMA\|行ID) | | 退款金额 | 数字 |
| RMA号 | 文本 | | 退货原因 | 文本 |
| 订单行ID | 文本 | | 数量 | 数字 |
| 店铺 | 文本 | | 已退数量 | 数字 |
| PO号 | 文本 | | 承运商 | 文本 |
| SKU | 文本 | | 物流单号 | 文本 |
| 商品名 | 文本 | | 发起时间 | 日期 |
| 售后状态 | 文本 | | 退回截止 | 日期 |
| 退款状态 | 文本 | | 免退回 | 复选框 |
| 退货方式 | 文本 | | | |

**表 3:绩效订单**(table_id → `FEISHU_ORDER_PERF_TABLE_ID`;全量,按违规事件一行)

| 字段名 | 类型 | 说明 |
|---|---|---|
| 唯一键 | 文本(去重键,PO\|指标) | |
| 店铺 / PO号 / SKU / 商品名 | 文本 | SKU 未回填的老单可能为空 |
| 指标 | 文本 | emoji 展示名(🚚 OTD 等,与日报同契约) |
| 首次周期 / 最近周期 | 文本 | 报表拉取日(ISO 日期字符串) |
| 出现周期数 | 数字 | |
| 计入绩效 | 复选框 | 报表任一周期计入即勾 |
| 仍在影响 | 复选框 | 该单仍出现在最近一期报表 = 仍拖当前绩效分 |

**表 4:对账明细**(table_id → `FEISHU_ORDER_SETTLE_TABLE_ID`;跨账期按订单行合并,
窗口默认最近入账日 90 天内。逐账期原始明细在 PG orders.settlement_lines,按期核数走数据库)

| 字段名 | 类型 | 说明 |
|---|---|---|
| 行ID | 文本(去重键) | |
| 店铺 / PO号 / 行号 / SKU / 商品名 | 文本 | |
| 净额 / 交易额 / 商品金额 / 佣金 | 数字 | 跨账期合计;佣金为负值 |
| 账期数 | 数字 | 该行出现过的账期个数 |
| 最近入账日 | 日期 | |
| 入账状态 | 文本 | 已入账/已冲销/已退款/待入账(四值规则见 db_schema.md) |

设计规则:
1. **登记类表**(人写程序读):程序同步后回写状态列,永不删除人写的行。
2. **展示类表**(程序写人看):权威在 Postgres,飞书表可随时整表重建;
   按 record_id 增量更新,用去重键字段对齐。
3. 每张表建好后,把 app_token/table_id/字段清单登记进 registry 并更新本文档。
4. 密钥类字段(ClientSecret、代理密码)只在店铺凭证表;该表访问权限收紧到最小人群。
