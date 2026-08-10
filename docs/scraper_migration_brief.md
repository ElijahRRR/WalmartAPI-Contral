# 采集服务改造简报(交给采集侧 AI)

> 你负责的是亚马逊采集服务(amazon-scraper-v3,部署于 DMIT VPS)。
> 沃尔玛自动化脚本体系正在重构为新项目 WalmartAPI-Contral,其数据地基是本机 Postgres 中心库。
> 本简报说明:为什么要动数据库、要解决什么痛点、你要实现什么、注意什么。

## 一、背景

现状链路:采集服务反复采集亚马逊产品(为更新价格/库存,且可能带不同参数如邮编)
→ 数据落在采集服务自己的数据库 → 审核服务读它,产出"能否上架 + 沃尔玛类目映射"
→ 沃尔玛侧脚本按审核结论上架。

问题:产品的"当前完整状态"(最新价格库存、审核结论、历史上架生成的参数)散落在
采集库、审核库、飞书表和沃尔玛侧脚本各自的缓存里,靠链路顺序保证一致,没有统一查询入口。

新架构:沃尔玛侧建 Postgres 中心库(`walmart_data`,schema `catalog`),把产品数据
分成两层——

- **`catalog.products` 产品身份层**:一个 ASIN 一行,终身唯一。存慢变字段
  (标题/品牌/类目/图片)+ 审核结论 + 上架复用资产。
- **`catalog.snapshots` 观测层**:追加不改、永不去重。存每次采集的快变字段
  (价格/库存/BuyBox)+ 采集参数(邮编等)+ 采集时间。

**你的多条记录不是脏数据**——同一 ASIN 不同邮编、不同时间的多条记录是合法观测,
问题只是它们从前既当档案又当快照。分层后:审核服务只看身份层(慢变字段变了才重审),
价格库存维护只看观测层最新值,上架把两层 JOIN 起来。

完整表结构见新仓库 `docs/db_schema.md`。

## 二、要解决的痛点

1. **没有稳定的产品身份**:同一 ASIN 多条记录,消费方不知道该信哪条。
2. **审核结果无法复用**:每次重采都可能触发重审,而多数重采只是价格库存变了。
3. **上架参数无法复用**:曾经生成的 UPC、LLM 映射的属性,散在沃尔玛侧脚本的缓存里查不到。
4. **增量同步无从做起**:消费方没有可靠的"给我上次之后的新数据"方式。

## 三、你要实现的功能

采集服务**保持独立部署、保留自己的数据库**(高频写入自己扛,故障隔离)。
沃尔玛侧会写一个 `catalog_sync` 工作流定期从你这里拉增量。你需要提供:

1. **可靠的增量导出能力**(核心):
   - 每条记录有单调递增的游标(自增 ID 或 updated_at,须保证不回跳);
   - 提供"给我游标 > X 的记录"的 API(或允许直读你的库,二选一,和沃尔玛侧商定);
   - 每条记录带全局唯一 `source_id`,沃尔玛侧靠它幂等去重(重复推送无害)。
2. **输出里明确区分两类字段**:
   - 慢变(标题/品牌/类目/图片)→ 沃尔玛侧 upsert 进 products;
   - 快变(价格/库存/BuyBox)→ append 进 snapshots。
   最省事的做法:每条导出记录就是"一次采集的完整结果 + 采集参数 + 采集时间",
   分拆由沃尔玛侧做;你只需保证下面第 3 条。
3. **采集参数必须随数据落库**:邮编、站点等影响结果的参数,每条记录都要带
   (结构化字段,不是拼在备注里)。没有参数,多条记录就无法正确分组取最新值。
4. **时间戳规范**:采集时间用 UTC(或带时区),精确到秒;这是"最新值"判断的依据。
5. (可选,做了更好)**慢变字段哈希**:对标题/品牌/类目算个 hash 随记录输出,
   沃尔玛侧和审核服务据此跳过无变化产品的重审。

## 四、注意事项

1. **不要破坏现有消费方**。旧系统(erpAPI)仍在生产使用你的现有 API,迁移期间新旧
   并存数月。新能力用新增接口/字段实现,不改动现有接口的行为和返回结构。
2. **不要在你这边做去重或"清理历史"**。多条记录是特性;去重逻辑在沃尔玛侧的分层模型里。
3. **不要直写沃尔玛侧的中心库**。你只负责把数据可靠地暴露出来,落库由沃尔玛侧的
   catalog_sync 完成。这保证两边故障互不传染(将来若确有必要再评估直写)。
4. **游标语义要经得起边界测试**:同游标值多条记录、乱序写入、重跑补采——
   任何情况下"从游标 X 拉起"不能漏数据(宁可重复,靠 source_id 去重兜底)。
5. **审核服务的对接**(第二阶段,先知晓):未来审核服务将改为读 `catalog.products`
   的待审行、写回审核结论。你不需要为此做什么,但你输出的慢变字段质量直接决定
   审核触发是否精准。
6. 和沃尔玛侧对齐的接口契约(增量 API 的路径/参数/返回结构)确定后,写进你仓库的
   文档,并在本文件回填一份,两边各存一份契约。

## 五、接口契约 v1(沃尔玛侧已拍板,2026-08-06)

**端点范围**:你现有的全部端点(submit/upload、batches/{id}/status、results、
export/{batch}、export/all、screenshot)**维持现状,不改不删**——旧系统和新系统的
api/scraper.py 都在用。你只需要**新增一个增量导出端点**:

```
GET /api/export/incremental?cursor=<int>&limit=<int,≤1000,默认500>
可选鉴权:请求头 X-Export-Token(建议加上,服务器是公网 IP)

响应:
{
  "records": [ <record>, ... ],   // 按 cursor 升序
  "next_cursor": 12345,           // 下次请求的 cursor(最后一条的游标值)
  "has_more": true
}
```

**record 结构**(一条 = 一次采集的完整结果):

| 字段 | 必填 | 说明 |
|---|---|---|
| source_id | ✅ | 全局唯一(如自增主键或 uuid),幂等去重键 |
| cursor | ✅ | 单调递增整数,不回跳 |
| marketplace | ✅ | 站点,当前恒为 "US"(products 主键已定为 (marketplace, asin) 复合) |
| asin | ✅ | — |
| scraped_at | ✅ | UTC ISO8601,精确到秒 |
| scrape_params | ✅ | 对象:{"zipcode": "...", ...} 影响结果的全部采集参数 |
| slow | ✅ | 慢变字段对象:title、brand、category_path、images[](首图=主图);
可选:bullet_points[]、description、weight、dimensions、variant(parent_asin/theme) |
| slow_hash | 建议 | slow 字段的稳定哈希,16 位十六进制。**当不透明值用**(见下 §5.1) |
| fast | ✅ | 快变字段对象:price、currency、stock_state;
可选:buybox_seller、buybox_price、coupon、deal |
| raw | 可选 | 裁剪后的原始载荷(沃尔玛侧存 jsonb 备查) |

**边界语义**(验收会测):cursor 相同的多条记录不丢;`cursor=0` 从头拉;
重复返回无害(source_id 兜底);删除/下架的产品不需要特殊事件,照常输出最新采集结果。
双方各存一份本契约,变更需两侧同步改版本号(v1 → v2)。

### 5.1 行为补遗(2026-08-08 两侧对账后补;仍是 v1)

采集侧副本(`docs/incremental_export_contract.md`)在实现期确认了三条原文
未定义的行为。三条都是**填补空白、不改已定行为**(按 v1 写的消费者不会失效),
故不升版本号,但必须写进本文档——两份副本内容漂移正是"各存一份"要防的事。

1. **`409 cursor_below_retention`(原文没有的状态码,最要紧的一条)**:
   要的下一条已被采集侧保留期裁掉。消费侧动作 = **告警 + 停止推进游标 +
   转全量对账**,绝不当普通错误重试。
   完整状态码表:200(含空结果,`records: []` + `next_cursor` 原样不推进)/
   401 `invalid_export_token`(修 token,不重试)/ 409(硬停)/
   422 `invalid_parameter`(修请求,不重试)/ 503(退避重试 + 告警)。
2. **`fast.stock_state` 是三值封闭集**:`in_stock` / `out_of_stock` / `unknown`。
   **`fast.stock_count` / `fast.delivery_days`**(采集侧 2026-08-09 纯追加,
   `contract_version` 仍是 1;存量事件也带,不需回填重采):均 int 或 null,
   **`null` 与 `0` 不是一回事**——`null` = 本次没采到,`0` = 采到了确实是 0
   (`stock_count=0` 即缺货)。与 `price` 同一条原则:**下游一律不得 `or 0` 兜底**。
   本侧落地:snapshots 两列同名存放;provider 只搬运真值;
   list_new 三态判断(真值走 <5 闸 / null+in_stock 按 `AMZ_IN_STOCK_QTY` 铺货
   并在摘要亮出行数 / 其余不上架);配送 null **不当超时**(方向反了会误清零)。
3. **`slow_hash` 是不透明值**:采集侧算法(NFKC + 空白折叠 + 哨兵归一 +
   列表排序 + 图片 URL 归约到 image ID + 排序键 JSON + SHA-256 取前 16 位)
   与本文档第五节的文字描述不是同一套。**消费侧不得按收到的 `slow` 自行重算
   比对——两边必然不等**;它保证的只是"慢变字段真变了才变"。

### 5.2 本轮追加(采集侧 2026-08-10,PR amazon-scraper-v4#7;仍是 v1)

**只增不改**:既有端点一个字节没动,现有接入无需改动。

1. **`fast.shipping` / `fast.shipping_raw`(运费)**——纯追加,值本来就在
   `raw.buybox_shipping` 里,**存量事件也拿得到,不需要回填**:

   | 采集侧 | `shipping` | `shipping_raw` | 含义 |
   |---|---|---|---|
   | `"FREE"` | `0.0` | `"FREE"` | **确认免运费**,落地价 = price + 0 |
   | `"$5.99"` | `5.99` | `"$5.99"` | 确认运费 |
   | `"N/A"` / 空 | `null` | `null` | **这次没采到**,落地价算不出来 |

   与 `stock_count` 同一条不变量(3b):`null ≠ 0`,**下游禁止 `or 0`**。
   本侧落地:`snapshots.shipping / shipping_raw` 两列;order_audit 运费为
   NULL 即成本算不出 → **转待人工**(当 0 的话成本偏小,本该拒的单被放行,
   而两侧都不报错)。
   ⚠ 采集侧 UI 导出的「总价」列**把 N/A 当 0**(`server/api/export.py`),
   与本端点口径不同;本侧只认增量导出这一路,不复制那个行为。

2. **`GET /api/screenshots`**(列批次截图状态,游标分页,`url` 仅在
   `status == "done"` 时非 null)与 **`GET /api/screenshots/{批次名}/{asin}`**
   (取 PNG)。取图**四种结局是四个状态码**,据此决定要不要重试:

   | 码 | 含义 | 该怎么办 |
   |---|---|---|
   | 200 | 图在这儿(`image/png`) | — |
   | 404 | 没有记录 / 批次不存在 / 已清理 | **别重试** |
   | 409 | 有记录但还没截好(带 `Retry-After: 10`) | **稍后再来** |
   | 410 | 截图失败,不会再有 | **别重试** |

   旧的 `/static/screenshots/{批次名}/{asin}.png` 保留不变,但那条路上后三种
   全是同一个 404,分不出来。本侧走新端点:`api/scraper.fetch_screenshot`
   把 409 与 404/410 抛成两个不同异常,order_audit 据此决定"下轮再来"还是
   "记墓碑不再请求"。**但常规路径不逐 ASIN 试探**:先用
   `GET /api/screenshots?batch_name=`(`api/scraper.screenshot_list`)拿整批
   清单,只对 `status == "done"` 的去取图——一批 50 个 ASIN 只有 10 张图好了,
   逐个试要发 50 次(40 次收 409),拿清单只要 1 + 10 次。逐 ASIN 端点退居
   兜底(清单说 done 而图已被清理时,仍靠它区分该不该记墓碑)。

3. **`POST /api/batches`(JSON 推送)**——与 `POST /api/upload` 共用同一个
   核心函数,撞名 409、回调注册、回显读回值逐字一致。**order_audit 已改用**
   (`api/scraper.submit_json`),取的是不必把 ASIN 列表拼 txt 再 multipart
   上传这点方便;邮编按**批次级** `zip_code` 传。维护链的全量重推仍走
   `/api/upload`(不切邮编,形态最简,没有换的理由)。

4. **邮编是逐 ASIN 的(`items[].zip_code`),一批可以混多个邮编**。
   采集侧 `server/api/batches.py` 文档串写死了三档优先级:
   `items[].zip_code`(这一个 ASIN)> 顶层 `zip_code`(这一批)> 服务端默认。
   响应回显 `per_asin_zip_count` / `invalid_zip_rows`——**两者对不上说明有
   ASIN 的邮编没被采纳,那些会按服务端默认邮编采回价格**,拿它审单就是按错
   地区审,而两侧都不报错。本侧推送后校验这个数并告警。

   **唯一要拆批的是同一 ASIN 的多个邮编**:`tasks` 上有
   `UNIQUE(batch_id, asin)`,以前同批两个邮编静默取第一个(200、少采一个、
   响应里看不出来),现在明确回 `400 conflicting_zip_for_asin`。
   本侧编排 `services/order_audit.plan_waves` 把同一 ASIN 的第 k 个邮编放进
   第 k 波,每波内 ASIN 不重复;**所有波次同一轮内推完**,不跨轮等待。

   > ⚠ 本文档 2026-08-10 一度写成"必须一个邮编一个批次",是我读错接口后的
   > 收紧,已按采集侧源码纠正。当时给的两条理由都不成立,记在这里免得再犯:
   > - **"截图会串"**——不成立。批次内一个 ASIN 只可能有一个邮编(就是上面
   >   那条 400 保证的),所以 `(批次名, asin)` 已唯一定位一个 (ASIN,邮编),
   >   落盘的 `<批次名>/<asin>.png` 天然不冲突,批次名不必带邮编。
   > - **"按批次取数分不出邮编"**——属实但不相干。`/api/results?batch_id=`
   >   与 `/api/export/{批次名}` 确实对同一 ASIN 全库只有一行(两个批次返回
   >   完全相同的行且不报错),但**本侧取数只走 `/api/export/incremental`**
   >   按 `scrape_params.zipcode` 分组,与批次怎么切无关。
   >
   > 实际代价:订单收件邮编几乎两两不同,收紧后 134 行订单推出了 **127 个
   > 批次**,每轮对账要发 127 次状态查询。

5. **批次完成度判据(采集侧 2026-08-10 实测确认)**——
   `GET /api/batches/{批次名}/status`:

   ```
   completed  ⇔  tasks.open == 0  AND  screenshots.open == 0
   ```

   `open` = 既不是 done 也不是 failed 的数量。**failed 算终态**,所以一张永远
   截不出来的图不会把批次卡死(实测 1 done + 1 failed → completed)。
   本侧实现在 `services/scrape_batches.is_settled`,product_refresh 与
   order_audit 共用同一份判据。

   ⚠ **批次 completed 不等于我们库里有数据**:中间还隔着增量导出 →
   product_ingest 两跳。所以批次状态只用来判"还要不要等"和"失败原因是什么",
   "这条数据到没到"必须另看快照是否真出现。

   失败原因走 `GET /api/batches/{batch_id}/failures`(**认 batch_id 不认名字**),
   `error_type` 是 11 类 + `unknown` 的封闭集,登记在
   `services/scrape_batches.ERROR_TYPES`——采集侧新增类型而本侧不知道时会告警,
   不会被静默当成普通失败。

**另有两条实现语义,消费侧必须遵守**:

- **`null` / `[]` 一律表示"本次采集没取到",不表示"该商品没有这个属性"**
  (软降级页会整块剥掉面包屑/详情表)。**绝不能拿空值覆盖已有值**;
  用扩展字段 `outcome`(`ok`/`not_found`/`blocked`/`parse_failed`/`stale`)与
  `completeness_ok` 判断这条够不够格进 products——本侧定稿:
  **`outcome != "ok"` 只进 snapshots,绝不 upsert products**。
- **`404` 且响应体含「批次不存在」= 请求打歪或采集侧路由退化,不是没有数据**
  (`/api/export/incremental` 落在采集侧 `GET /api/export/{batch_name}`
  catch-all 的前缀里)。按 5xx 处理并告警,**绝不推进游标**。

采集侧还提供契约外的扩展字段(收着无害):`outcome`、`completeness_ok`、
`review_hash`、`recorded_at`,以及 `scrape_params` 里的 `zip_observed` /
`zip_verify` / `source_marketplace` / `parse_engine`。其中 `zip_verify ==
"mismatch"` 的记录不应进入该邮编的价格序列。

## 六、验收标准

- 沃尔玛侧 catalog_sync 每 N 分钟拉一次增量,连续运行一周:无漏采(抽样比对)、
  无重复入库(source_id 冲突计数为 0)、products/snapshots 分层数据正确。
- 现有旧系统(erpAPI)链路零感知、零故障。
