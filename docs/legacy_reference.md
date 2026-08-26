# 旧仓库(erpAPI)事实清单

> 重构是重写代码,不是重新踩坑。本文档汇总旧系统用事故换来的参数、语义和教训,
> 执行 AI 实现对应工作流前必须读相关小节。旧仓库路径:`~/Projects/erpAPI`(生产 Mac)。

## 必须原样保留的行为语义

1. **每店铺固定出口代理**。凭证表里每个 ClientId 绑定固定 socks5/http 代理,
   所有沃尔玛请求必须经该店代理发出。这是防店铺关联的生死线,没有例外。
2. **token 900 秒内复用**,按 client_id 缓存,线程安全,401 时就地刷新重试一次。
3. **429/5xx 自适应退避**:解析 `Retry-After` 与沃尔玛特有的
   `X-Next-Replenishment-Time` 响应头;`x-current-token-count` 是剩余配额。
4. **UPC 池"领用即 claimed"**;回收**仅限三类**:提交前失败(prep_failed)、
   反查双确认未达(not_found)、4xx 被拒(rejected);**Unknown 永不回收**,
   conflict/bad_prefix 永久弃用。历史上因释放语义不一致出过重复使用事故。
   ⚠ 2026-08-14 勘误:原写「永不释放」并列在"必须原样保留"清单里——照它做会
   把 `services/upc_pool.py:107 release()` 当违规实现删掉,**造成 UPC 池只出不进**。
5. **DELETE_ITEM 只对 SFF 且不可恢复**;旧系统每店提交间隔 360s、单日上限从飞书表读取。
6. **auto_listing 的 9 状态生命周期**与 reconcile(按 feed 结果回写)语义,
   迁移 listing 时先读旧仓库 `auto_listing/README.md` 和 `docs/closed_loop.md`。

## 飞书踩坑参数(api/feishu.py 必须内置)

- 瞬时错误码 **90235 / 90217 / 50502** 需指数退避重试;其他错误码不重试
  (`1204` 曾被批量下架当超时并列处理但未进 lark_io 白名单,新实现需实测定夺)。
- **飞书调用必须显式绕开本机 HTTP 代理**(旧系统 `LARK_CLI_NO_PROXY=1` / no_proxy):
  2026-05-07 事故根因即本机代理 127.0.0.1:7897 拒连导致 14,610 个单元格写回失败。
- 电子表格单次写入上限 5000 格;实际稳定值更低,旧系统按 ARG_MAX 教训降到过 1000 行/批。
- 多维表格:批量增/改单次 ≤500 条,整批全成功或全失败;写 QPS 低(约 10/s),
  **同一张表串行写**。
- 旧系统三起数据事故(单日丢 6241 行)全部源于:分批写中断无补偿、重试语义不当、
  超时误判成功。新实现:每批写完校验返回,失败批次记录到 ops 后重试,不静默跳过。

## 沃尔玛 API 高危配额(refdata/walmart_rate_limits.tsv 全量,这里列高危)

| 端点 | 配额 |
|---|---|
| PRICE_AND_PROMOTION feed | **10/hour**(三件套共享桶,2026-08-26 官方复核;旧记 6/天只属本仓不用的 feedType=promo。代码按 8/hour 配置,见蓝图 §3.2) |
| PUT /v3/price 单品 | 100/小时 |
| GET /v3/items 带 query | 60/分钟(无 query 300/分钟) |
| Insights 绩效类 22 个端点 | 全部 1/分钟 |
| /v3/feeds 与 /v3/feeds/{id} | 共享 5000/分钟 |
| WFS Inventory Reconciliation | 1/小时(必须缓存) |

注意:`refunds/summary` 已废弃,用 `returns/*` 系列;negativeFeedback/returns/
itemNotReceived 六个端点不在官方限速表内,按 1/分钟保守节流。

## 旧系统的结构性缺陷(新系统靠架构消灭,不要复刻)

- 8 处绕过共享客户端的直连(全部老实传了代理,丢的是退避/自愈,不是关联安全),
  根因是旧客户端三缺口:无 feed 提交接口、无二进制响应支持、无 async。
  api 层第一天补齐(plan.md Phase 1)。
- "遍历店铺并发执行"样板被复制 ≥12 份 → services 写一次。
- 飞书"查元信息→扩行→分批写"被复制 5 份 → api/feishu.py 写一次。
- **旧批量下架 `--dry-run` 是地雷**:只挡飞书写回,DELETE_ITEM 照提交且不留 feedid
  (下轮还会重选同一批行)。新系统 dry-run 语义必须相反且由 cli.py 强制。
- **旧商品维护 `post_feed` 默认 max_retries=3**:超时发生在沃尔玛已接收之后会重复
  提交 feed——现存缺陷,勿复刻;feed 提交一律 0 自动重试 + ops.feed_log 反查补交。
- **售后 README"returns 不支持时间过滤"是错的**:官方 OpenAPI 支持
  returnCreationStartDate/EndDate 等参数,旧代码只是没传。新 returns_sync 做真增量,
  行主键 (returnOrderId, returnOrderLineNumber)。
- 凭证以 店铺API.xlsx **Sheet1 为权威**(48 家有效店铺,全 socks5,出口 IP 两两不同);
  Sheet2 是漂移的第二张代理清单,勿混用。README 各处"57 家"已过时。
- **各 feedType 各自独立 10/hour**(MP_ITEM_MATCH 20/hour);唯一的共享桶是
  **价格三件套**。`errorReport` 下载另有 60/小时 独立限制。
  ⚠ 2026-08-14 勘误:原写「除特例外所有 feed 共享一个通用桶」——那是旧仓推断,
  已于 2026-08-05 官方核验作废(见 api_blueprint §3.2)。代码侧反证:
  `api/_client.py:178-183` 逐 feedType 独立登记(DELETE_ITEM 6/3600、
  MP_MAINTENANCE 8/3600、MP_ITEM_MATCH 15/3600、MP_ITEM 8/3600、inventory 8/3600),
  只有 `feeds.post.price` 是共享桶。**按错的那条排期会把 5 个独立桶当成 1 个,
  严重低估 feed 吞吐。**
- `DELETE_ITEM_VER = "5.0.20250919-16_45_47-api"` 之类版本常量被抄 3 份 →
  唯一出处是 `registry/resources.py` 的 `FEED_SPEC_VERSIONS`,`api/feeds.py:79` 只取用
  (2026-08-14 勘误:原写"只在 api/feeds.py",与铁律 3「一切表 ID/常量只准从 registry 取」相悖)。
- 整表覆盖写飞书导致"新短旧长残留尾部旧行" → 多维表格按 record_id 更新,天然消灭。
- 双重调度(订单同步同时被 launchd 每小时 + skill 13:30 触发)→ 新系统一条工作流
  只允许一条调度,登记在 plan.md。

## 状态数据迁移清单(切换对应工作流时从生产 Mac 搬)

| 旧位置 | 内容 | 去处 |
|---|---|---|
| 沃尔玛问题商品清理/cache/*.json | 已提交 SKU(2日防重)、反补计数、品牌缓存 | ✅ ops.dedupe(2026-08-11 导入完成) |
| PostgreSQL walmart_cleanup 库 | 41.7 万行问题商品历史 | ✅ catalog.product_events 时间线(2026-08-11 导入完成) |
| 沃尔玛商品维护/maintenance.db | 维护任务与 feed 明细 | **不迁**(所有者拍板 2026-08-12:旧维护记录不要;新流水在 ops.feed_*) |
| 沃尔玛UPC生成器/upc_history.db | 10 万+ UPC 去重池 | **不迁**(所有者拍板 2026-08-12:有用的 UPC 手动写入 catalog.upc_pool;死表 listing.upc_pool 已删) |
| erpAPI/walmart_settlement.db | 结算快照 | **不迁**(所有者拍板 2026-08-12:旧系统只有总对账单,无账期明细) |
| auto_listing/state/ + logs/feed_*.json | feed 历史(reconcile 反查 SKU→UPC 的唯一凭据) | 原样归档即可(上架表 26 列已拍板不迁,2026-08-12) |
| 订单审核 飞书 _meta sheet | 每店增量游标 | ops.cursors |
| 飞书 QNIp…Bb/8280e8 黑名单 ASIN 表(blacklist_sync 写入,本仓找不到读者) | 历史黑名单 ASIN | ✅ catalog.asin_blacklist(只收永久类 B/C/E/F/G/K 过滤导入)→ blacklist_push 投影新「黑名单ASIN」wiki 表 |
| auto_listing/state/risk_gate_cache.json | 风控两表 24h TTL 读缓存 | 不搬(纯派生):risk_sync 已镜像入 catalog.risk_product_types / brand_blacklist,闸门读库;开 listing 前跑一次 risk_sync 即可 |
| <旧项目根>/data/frontend_scrape/latest.json(影刀应用内部写死此路径) | 影刀前台抓取结果(卖家名称/销售状态,日报降级源) | <DATA_ROOT>/frontend_scrape/latest.json(paths.frontend_scrape_file;并跑期 env FRONTEND_SCRAPE_JSON 指旧路径,切换需改影刀 RPA 输出) |
| 飞书「店铺KPI」总览页(旧影刀的**输入**来源) | 影刀读它拿 sellerId 决定抓哪些卖家页 | <DATA_ROOT>/frontend_scrape/input.json(paths.yingdao_input_file;env YINGDAO_INPUT_JSON 覆盖)。所有者定稿 2026-08-15:新影刀应用改读本地文件,不再经飞书中转 |

## 环境事实

- 生产机:macOS + launchd(不读 .zshrc,环境变量要么写进 plist 要么用 paths.py 默认值)。
- 影刀 RPA 仅 macOS,产出店铺前台数据(日报 A-H 列唯一来源),新系统只改其数据落点。
- lark-cli 二进制仍可用,但新系统统一直连 HTTP,不再依赖它。
- 旧凭证 `店铺API.xlsx` 已进过 git 历史(公开仓库),**上多维表格时建议顺手轮换
  可轮换的密钥**;新仓库永远不出现明文密钥。
- erp-core 与旧 ERP 链路(erp_listing_server/erp_web/erp_worker)不在本次范围,
  它们依赖旧仓库路径,旧仓库归档前保留原位。
