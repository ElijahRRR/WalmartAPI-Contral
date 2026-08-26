# 迁移总计划

> 角色分工:**架构 AI**(云端,本文档作者)负责技术栈、框架、规范、计划与评审;
> **执行 AI**(本地)负责真实编码、联网测试、打通各处连接。本文档是两者的共同契约。
> 状态标记请执行 AI 随进度更新:`[ ]` 未开始 / `[~]` 进行中 / `[x]` 完成并已对拍验收。

## 0. 技术栈(定稿,不再讨论)

| 项 | 选择 | 理由 |
|---|---|---|
| Python | ≥ 3.12 | 旧项目生产机已是新版;不用任何 <3.10 兼容写法 |
| HTTP | httpx | 同步为主;订单拉取等需要并发的地方用其 async |
| 数据库 | PostgreSQL 17(本机已有)+ psycopg 3 | 业务数据;SQLite 仅用于可重建缓存 |
| 配置/密钥 | python-dotenv 读 `<DATA_ROOT>/.env` | 密钥不进 git |
| CLI | argparse(标准库) | 不引入额外框架;cli.py 是薄分发器 |
| 飞书 | 直连开放平台 HTTP(tenant_access_token) | 多维表格为主;不再依赖 lark-cli 二进制 |
| 调度 | launchd 调 `python cli.py <workflow>` | 与触发方式解耦,未来网页/MCP 走同一入口 |
| 依赖管理 | uv + pyproject.toml,`uv pip install -e .` | 消灭旧项目的 sys.path hack |

不引入:ORM(直接写 SQL,psycopg 3 参数化)、消息队列、容器。这个量级用不上。

## 迁移进度总览(2026-08-17 定格:**迁移完成,已在生产按调度运行**)

> 一句话:**功能侧与运维侧都已收官**。全部业务工作流代码完成、生产验收通过;
> 调度 2026-08-17 全部上线并跑通首轮(11 条自动任务:电脑 launchd 2 条 +
> 智能体定时任务 9 条),旧系统冲突调度已停。
>
> **本文件从"任务派活单"转为"迁移过程留档"。** 想知道系统现在是什么样、
> 有哪些工作流、怎么跑 —— 看 **`README.md`**;想知道调度现状 ——
> 看 `docs/schedule_plan.md`;想知道某条判据当初为什么这么定 —— 看本文件的
> Phase 2 矩阵行与 `docs/production_cutover.md`。
>
> 下表的「剩余」列里那些"挂调度"**已经全部完成**,原文保留是为了留下
> "当时卡在哪"的记录,不要再照着它派活。

### 2026-08-17 上线后的现状(一句话版)

| 维度 | 现状 |
|---|---|
| 工作流 | 72 条(`workflows/*.py`),22 条进调度、50 条手动/一次性 |
| 调度 | launchd 3 条(feed_poll 每 30 分 / 订单链每小时 :20 / product_ingest 每小时 :50)+ 智能体 9 条(每日/每周);验收记录见 `production_cutover.md` §九 |
| 并发 | 跨店统一 `services.stores.STORE_WORKERS=24`;审核默认 128 worker(按 PG 连接余量自动钳);飞书电子表格写按表加锁 |
| 数据库 | PostgreSQL 17 `walmart_data`,五 schema、52 表、10 视图 |
| 测试 | 1975 passed, 1 skipped |
| 仍在跑的旧链路 | 旧上架 / 审核 worker(所有者 2026-08-17 判定:不写表,留作备用) |

| 工作流 | 生产验收 | 剩余 |
|---|---|---|
| product_query | ✅ 2026-08-05 | — |
| returns_sync | ✅ 单店+全店 | 挂调度 |
| daily_report | ✅ 对拍/历史导入/看板/全店 | problems 列映射对拍校准;订单列双算对拍收口;挂调度 |
| order_sync + order_audit | ✅ 151 行全链 2026-08-10 | 挂调度(收敛旧双重调度) |
| order_center_push | ✅ 全店建库 | 挂调度;Lookup 列人工关联 |
| perf_problems | ✅ 明细已映射飞书(运营在用) | 挂独立调度 |
| maintenance | 🟡 三 provider 实跑过;**清零链路未生产验证** | 清零验证(⚠ FEISHU_LIMITS_* 已于 2026-08-17 配好,前置解除,可以验了);涨跌幅闸暂不做;~~挂调度~~✅ |
| product_clear | ✅ 2026-08-07 | RETIRE_ITEM 动作实测;切旧 15:00 cron;挂调度 |
| problem_scan | ✅ 2026-08-14(批次 E 拆出) | 只读定性,产 ops.dispositions 建议行 |
| problem_product_cleanup | ✅ 2026-08-07(21 店真跑) | 停旧每 6h cron;挂调度。⚠ 批次 E 后改为**纯执行件**:只消费建议行,自己不做决策 |
| catalog_sync | ✅ 47 店全量 | 每日并跑对拍;挂调度 |
| product_ingest / product_refresh | ✅ 生产实跑(2026-08-13 所有者确认) | VPS 后配 EXPORT_TOKEN;挂调度 |
| settlement_sync | ✅ 首跑 | 挂调度(双周账期) |
| **listing / list_new** | ✅ **端到端 2026-08-13**(3/3 SUCCESS,六轮错误账收官);**变体分组 2026-08-15 接线完成**(所有者四条批复:增量归组/只查本店/沿用在架组 ID/家族>20 退单品) | 变体分组生产验收;挂调度+停旧切换 |
| match_listing(跟卖) | 代码完;试点后置(所有者:暂不用) | 启用前单店试点 |
| feed_poll / 反哺器×5 | ✅ 生产在用 | 挂高频调度(30 分钟) |
| sku_locked_heal / heal_unknown | 代码完+测试 | 等首个真实场景;随调度上线 |
| risk_sync / upc_sync / blacklist_push / brand_scrape | ✅ 均已生产验证 | 挂调度 |
| **backup** | ✅ **生产首跑完成**(2026-08-13,1554.5 MB 过校验——生产库第一份备份) | 挂每日调度 |

**功能侧代码余项**(全部非阻塞):~~变体分组(唯一业务大项)~~ **代码已落地**(2026-08-15 决策层+载荷层,2026-08-17 补多维/错位重映射/组内标题差异化),只余 list_new 生产验收 /
产品中心黑名单增量脚本(飞书表停用后才需要)/
llm_cache 清理器(暂不做,上量后再议;审核批次 C 上量时按约重议)/
~~审核迁入批次 B~E~~ **B/C/D/E 均已落地**(B:`services/audit_l1_llm.py`+`audit_models.py`;C:`workflows/product_audit.py` 全链含 LLM 层,并已进 `product_chain`/`audit_sheet` 调度;D:`workflows/audit_import.py` 一次性迁库;E:`problem_scan` / `problem_product_cleanup` 扫描件与执行件拆分),明细见 docs/audit_migration_plan.md 第十一节。
**已明确不做**(所有者拍板 2026-08-13 增补):update_listed 五字段集、
upc_audit、cli.py health、UPC 造号、退款、涨跌幅闸(暂)、erp-core 相关、
历史数据迁移(整批关闭)。**等外部**:二期审核服务(入库/审核事件接缝)、
TRO 跨仓边界(暂放)。
**运维战役**(见 backlog 第六/八节与 docs/legacy_schedules.md):生产取证 →
配 env 三件 → 挂全部调度(顺序:catalog_sync→product_refresh→product_ingest→
maintenance/list_new)→ 按域停旧切换。
**✅ 2026-08-17 全部完成** —— 验收记录见 `docs/production_cutover.md` §九。

## 1. 阶段划分

### Phase 0 — 地基(一次性)

- [x] 仓库骨架:cli.py / registry / api / services / workflows / refdata / docs
- [x] `<DATA_ROOT>` 目录初始化脚本(specs/ cache/ logs/ backups/,.env 模板,chmod 600)
      → `python cli.py init_data_root`(已容器内冒烟)
- [x] registry/paths.py(默认 DATA_ROOT + 环境变量覆盖;launchd 不读 shell 配置,
      所以默认值必须能独立工作)
- [x] Postgres:建五个 schema(见 db_schema.md;audit 为 2026-08-13 审核迁入批次 A 新增)+ 只读角色 `readonly`
      → `refdata/schema.sql` + `python cli.py db_init`(幂等)。
      生产机 PG17 验收通过(2026-08-05):4 schema / 9 表 / 1 视图,幂等回读正常。
      ⚠️ 保留项:readonly 角色口令(READONLY_DB_PASSWORD)尚未配置,配置后重跑
      db_init 即生效
- [x] registry/db.py:唯一连接入口(业务连 PG;cache 连 SQLite 时内置 WAL+busy_timeout)
- [x] cli.py:分发 + flock 单实例锁 + ops.runs 运行记录 + 飞书成败通知 +
      dangerous 工作流强制 dry-run(真跑需 `--execute`)(已容器内逐项冒烟)
- [x] api/feishu.py:多维表格客户端(查询/批量写≤500/单表串行/重试),字段名走 registry 常量
      (重试/退避/瞬时码参数照抄旧 lark_io 实测值;7 个单测覆盖)
- [x] api/_client.py:从旧 walmart_client.py 移植认证核心
      (token 缓存 900s、每店固定代理、401 自愈、429/5xx 自适应退避、连接池)
      **移植而非重写——这 498 行是旧项目质量最高、事故最少的代码**
      (逐行移植,仅 print→logging、店铺读取剥离到 services/stores.py;6 个单测覆盖)
- [x] 店铺凭证多维表格(用户在飞书建)→ registry 登记 → stores 读取 + 本地快照兜底
      (飞书故障时用最近一次快照,快照文件在 DATA_ROOT,不进 git)。
      → 已建表接通:ping_stores 经该表读取 49 家店铺凭证(2026-08-05 生产验收)
- [x] 核实 erp-core(外部工作区 `~/Projects/erp服务/erp-core`)的 celery beat
      是否在跑——它是唯一可能在新系统之外仍在写沃尔玛库存/feed 的进程
      (30s poll_pending_feeds、6h 推库存)。必须在 Phase 0 查清,不能拖到迁
      maintenance/listing 时
      → 用户已确认(2026-08-05):erp-core 未启用,亦不在迁移考虑范围。三方并跑风险解除
- [x] 端到端验证:`python cli.py ping_stores` —— 读凭证表 → 每店经代理调一个只读
      沃尔玛端点 → 结果写 ops.runs → 飞书发汇总。**此条通过 = 地基验收。**
      → 生产机实跑 46/49 连通(2026-08-05),其余 3 家为已知异常,项目所有者确认
      按此结果通过 Phase 0 验收。
      ⚠️ 保留项:运行通知 webhook(FEISHU_WEBHOOK_URL)尚未配置,当前通知降级为
      仅日志,填入 .env 即生效

### Phase 1 — api 层补齐(按需推进,不求一次全量)

每迁一条工作流,只补它需要的 api 域文件。必须一开始就做对的三件事
(旧项目 8 处旁路代码的根因):

- [~] api/feeds.py:统一 feed 提交/状态查询(蓝图 §5 定稿落地,2026-08-06):
      header 分发(DELETE_ITEM/RETIRE_ITEM/MP_MAINTENANCE)+ 条数×字节双约束切片
      + 三层防重(feed_log 抢占/反查三态/query_pending 启动对账)+ 明细 50/页翻页
      + 未知状态告警;版本字符串唯一出处 registry.FEED_SPEC_VERSIONS;
      feeds 限速桶登记(POST 各 feedType 独立,未登记默认拒绝)。
      errorReport 下载与其余 feedType 随 listing 补
- [x] api/reports.py:报告类下载(daily_report/perf_problems 在用:请求/轮询/下载三段 + CSV 解析)
- [x] async 支持(2026-08-13):`api/orders.fetch_orders_bulk` 同步门面,
      内部 asyncio + httpx.AsyncClient 跨店并发(默认 12,单店内翻页仍串行
      ——cursor 语义);`_get_async` 镜像 `_request_ex` 重试口径(429 按
      Retry-After / 5xx 与网络指数退避 / 401 刷 token 一次);持久化经
      handler 回调注入(api 不 import services);**测试缝与同步世界共用**
      `_build_transport`(MockTransport 双栈,async 路径绝不绕出桩真打
      沃尔玛——首版测试实测绕出去吃了真 401,已堵);order_sync 已接线,
      ThreadPoolExecutor 退役
- [x] **每店 per-(store, bucket) 令牌桶**(设计定稿见 docs/api_blueprint.md 第 3/6 节,
      官方 2026-08-05 核验):各 feedType **独立** 10/hour(MP_ITEM_MATCH 20/hour),
      唯一共享桶是价格三件套;未登记的 bucket **默认拒绝而非放行**(旧系统 RETIRE_ITEM
      零限速就是未知键放行漏的)。令牌桶做在 _client 层,跨 workflow 生效。
      ※ 此条修正 legacy_survey C6 的"共享桶"推断——官方表为准
      **2026-08-12 完成"跨进程"这一半**:稀缺桶(window≥600s 或 limit≤10:
      全部 feeds.post.*/prices.put/reports.request/insights/SPEC 日额度)限速
      状态落 ops.rate_events(advisory 事务锁 + PG now() 单一时钟),跨
      workflow 进程共享且跨运行不失忆;高频大配额桶留进程内(分钟窗自然
      重置)。**PG 不可达稀缺桶 fail hard 不降级**(所有者拍板;静默降级 =
      旧事故换马甲)。调用方零改动(rate_acquire 签名不变,内部分档路由)

### Phase 2 — 工作流逐条迁移(核心阶段)

顺序按"最独立→最复杂",每条走同一循环:
**实现 → dry-run 对拍(与旧系统输出比对,≥3 天或 ≥3 个周期)→ 切换(停旧→搬状态→起新)→ 观察一周 → 打勾**

| # | 新 workflow | 替代旧模块 | 危险 | 备注 |
|---|---|---|---|---|
| 1 | product_query | 产品ID查询产品详情 | 否 | 零状态零调度,练手验证 api 层。**[x] 完成**(2026-08-05 生产实跑通过,PR #3) |
| 2 | returns_sync | 售后订单同步 | 否 | **[~] 单店生产验证通过**(2026-08-06,10 售后行入库并挂上订单行);全店已跑(所有者确认 2026-08-13);待挂调度 |
| 3 | daily_report | 沃尔玛店铺日报 | 否 | 影刀 RPA 部分保持原样(仅 macOS),只改数据落点。**[~] kpi 阶段单店对拍通过**(2026-08-06,A085,绩效/订单/结算全列对齐;结算解析改递归查找修复)。**影刀已接入**(2026-08-08,-p yingdao=1):总览 A:H 影刀输入投影(空 sellerId 过滤,A147 防线)→ spawn → 新鲜度轮询 → 回填当日卖家名称/销售状态;默认关,**停旧 walmart-kpi-daily 前严禁开启**(双 spawn 互抢)。**历史数据导入就绪**(kpi_history_import):旧「店铺KPI」每店 sheet(72 张)按表头关键词映射入 ops.store_kpi_daily(七个真实中文表头 2026-08-08 预览实证补齐),ON CONFLICT DO NOTHING 绝不覆盖,默认预览 -p apply=1 真跑;卖家名称空白问题已修(跨日延续)。**问题订单摘出**(所有者定稿 2026-08-08):绩效问题订单明细(insights report 端点 xlsx,全 API 最脆一族)+ 订单中心 perf_events 独立为 `perf_problems` 工作流,由独立调度驱动;daily_report 只取指标比率(insights summary),不再被那条链拖慢。**KPI 看板定稿**(所有者 2026-08-08):新表格两页(总览=每店最新一行全 32 列;历史=全店合一近 90 天),phase=board 整表重写;旧表 72 张分页停更归档(仅剩导入源 + 影刀输入 A:H 两个角色)。kpi_history_import apply / 看板建表+首刷 / 全店跑已完成(所有者确认 2026-08-13);待:problems 列映射对拍校准、挂调度观察。**订单列去重复拉取**(所有者认可 2026-08-08):昨日出单/销售额改读 orders.order_lines,当前双算对拍期(API 权威,库算差异记日志+摘要计数),连续对平后摘 API 拉取、order_sync 成为调度前置——"一个外部源只有一个拉取方"审计后全项目最后一处重复拉取。**看板列序调整**(所有者定稿 2026-08-15):首列店铺、次列日期,两页均按店铺排序(历史页店内按日期降序)。**影刀衔接反转**(所有者定稿 2026-08-15,所有者已复制出独立的新影刀应用):不再「写飞书总览→影刀读飞书」,改为本仓写 `input.json`(paths.yingdao_input_file,原子写、空 sellerId 过滤、只含本轮真跑到的店)→ 影刀读文件 → 影刀写 latest.json → 本仓回填,两端都是文件、都归 registry 管;飞书「店铺KPI」表退为 kpi_history_import 的只读导入源,KPI_SHEET.columns 置空。⚠ 未做:latest.json 输出格式(sellerId 为键)**故意不动**,一次只改一端 |
| 4 | order_audit | 沃尔玛订单审核 | 否 | 收敛旧的双重调度(launchd 每小时 + skill 13:30 二选一);依赖采集服务。**[~] 取数前半生产验证通过**(order_sync,2026-08-06 单店 38 行;statusDate/trackingURL 按线上实证修正)。**审核后半已落地**(2026-08-09,所有者定稿口径):四道审核 = 钓鱼(**只匹配邮编**,旧的黑名单地址整套不迁)→ 采集完整性 → **配送时长 ≥9 天**(旧值 12,同日收紧)→ 采购方匹配(配送方式+单价区间+启用,多候选取最低汇率)→ 限价(限价 = 商品金额×0.75;成本 =(亚马逊单价×数量×**采购方汇率** + 运费)×1.08;旧的写死汇率 6.8 废除,改按采购方取);任一道给不出确定答案一律「待人工」,**绝不当通过**(null-0 铁律:采不到的配送时长不能当 0 天放行);钓鱼行不可覆盖语义保留。结论落 order_lines.audit_status/audit_detail,飞书审核列由 ORDER_SALES_AUDIT 独占写(**只更新不新建行**),截图经 feishu.upload_media 上传成附件、按 URL 哈希防重复上传。**范围到审核结论为止**(所有者定稿:不做自动下单,与旧系统一致只出建议)。**商品一致性已接**(所有者定稿 2026-08-09 要「标题相似度」列):沃尔玛商品名 vs 亚马逊标题归一化后相似度,阈值 0.9 沿旧值,低于阈值转待人工,数值另出一列给人看;排在配送时长与限价**之前**(采到的若不是同一个商品,拿它算的限价和货期全无意义)。**采集已接线**(2026-08-09):①`from_snapshot` 按契约 v1 取真实字段——邮编 `scrape_params.zipcode`、配送方式 `raw->>'is_fba'`(与 maintenance/list_new 同一处)、卖家 `buybox.buybox_seller`、标题两层 JOIN 取 products;`zip_verify='mismatch'` 的快照**直接判废**(切邮编失败拿回的是默认地区价格,拿它算限价等于按错地区审单);②按收件邮编推采集(逐 ASIN 带邮编,详见下);③台账 `ops.audit_scrape`(ASIN×邮编)**先落 pending 再调接口**,每轮开工先对账——这正是旧系统缺的那块(重启即丢);④推批次带 `needs_screenshot=true`。**采集侧 2026-08-10 追加已接**(amazon-scraper-v4#7,只增不改):**运费** `fast.shipping`(FREE→0.0 确认免运费 / N/A→NULL 没采到)落 `snapshots.shipping/shipping_raw` 两列,**NULL 即成本算不出来 → 转待人工,严禁 or 0**(当 0 则成本偏小、本该拒的单被放行,两侧都不报错);**截图**先用 `GET /api/screenshots?batch_name=` 拿整批清单、只对 `status == "done"` 的取图(逐 ASIN 试探要发一堆 409),取图端点的四状态码仍用于兜底(409 下轮再来 / 404·410 记墓碑 / 200 上传飞书),截图从不阻断结论;**批次编排口径 2026-08-10 定稿(经所有者两次纠正,以采集侧源码为准):一批混不同 ASIN 的不同邮编,只有同一 ASIN 的多个邮编才拆波次**。采集侧 `POST /api/batches` 邮编三档独立(`items[].zip_code` 逐 ASIN > 顶层 `zip_code` 批次级 > 服务端默认,见 server/api/batches.py 文档串),逐 ASIN 带邮编是一等能力;唯一硬约束是 `tasks` 的 `UNIQUE(batch_id, asin)`——同批给同一 ASIN 两个不同邮编回 `400 conflicting_zip_for_asin`(明确拒绝不静默取第一个),故 `plan_waves` 把同一 ASIN 的第 k 个邮编放进第 k 波,**所有波次同一轮内推完**。推送后校验响应的 `per_asin_zip_count`:对不上说明有 ASIN 的邮编没被采纳、会按**服务端默认邮编**采回价格(按错地区审单,两侧都不报错),必须告警。⚠ 中途我按"一个邮编一个批次"收紧过一版,两条理由查证后都不成立并已记进 brief:①截图不会串——批次内一个 ASIN 只可能有一个邮编(就是那条 400 保证的),`(批次名, asin)` 已唯一定位一个 (ASIN,邮编);②按批次取数分不出邮编属实但不相干——本侧取数只走 `/api/export/incremental` 按 `scrape_params.zipcode` 分组。实际代价是订单收件邮编两两不同,收紧后 134 行推出 127 个批次。**批次生命周期接完**(采集侧实测:`completed ⇔ tasks.open==0 且 screenshots.open==0`,failed 算终态):落定判据三层——快照真出现 → done(批次 completed 不等于落库,中间隔着增量导出 + product_ingest 两跳);批次已落定仍无快照 → 认账失败并去 `/api/batches/{batch_id}/failures` 拿**真实原因**写台账(验证码可换时段重试、variant_offset 重试也没用,处置不同;`error_type` 11 类 + unknown 封闭集登记在 `services/scrape_batches.ERROR_TYPES`,采集侧新增类型会告警);兜底超时 20 分钟且**只打在批次已不在途的组合上**(在途批次盲超时重推 = 白烧一批配额)。批次台账三件套提到 `services/scrape_batches`,与 product_refresh 共用,两边按批次名前缀(`wm-refresh-` / `wm-audit-`)各查各的在途批次。**有快照但缺关键信息的行改为重采**(outcome≠ok / 缺配送方式或时长 / 运费 N/A,由 judge 显式标 `rescrape`;无匹配采购方、标题不符不进——重采解决不了只是白烧配额),**重试三天封顶**(2026-08-22 由一天放宽,对齐 days=3 的审核窗口;台账加 first_requested_at+attempts,首次请求超 72h 仍拿不到可用数据就不再推;上次推送已过期——按快照新鲜度 24h 判——视为新需求、窗口重置)。**快照 24 小时新鲜度门槛**:超期视同没有(审单看的价格/库存/货期变得快);同一 (ASIN,邮编) 有多组 scrape_params(zip_observed/parse_engine 不同)时按 scraped_at 取最新,不让字典后写覆盖先写(那等于随机挑、无法复现)。**首跑通过**(2026-08-10 生产:两张配置表登记完成、54 行出结论、飞书审核列回写正常;登记 `FEISHU_SUPPLIER_TABLE_ID` 时误填了 `vew...` 视图 ID 会报飞书 1254004 WrongTableId,要取 URL 里的 `table=tbl...`)。**轮询已接且默认开**(所有者定稿 2026-08-10;`-p wait=0` 关):推采集 → 轮询批次到落定(20 分钟兜底,退避 3s→30s)→ **就地跑增量摄取** → 重新对账重判 → 回写飞书。顺序不能换:批次 completed 只说明采集侧干完了,数据还在增量流里,不先摄取就对账会把每条都判成「批次已采完但无快照」,一轮全军覆没。就地摄取**借 product_ingest 的 flock**(新增 `services/runlock`,cli.py 同源):增量游标独占推进,两个进程同推、后写的盖掉先写的,中间那段记录永不再拉——两侧都不报错。拿不到锁按「这轮跳过」处理(数据仍会由 product_ingest 摄入)。游标/翻页/落库实现提到 `services/product_ingest.pump`,workflows/product_ingest 变薄壳,两边共用同一份游标纪律(空页不推进 / 只认 next_cursor / 409 停在原地)并首次有了单测。默认开:忘了加开关只会看到上一轮结论**而且不报错**,这种「忘了就静默降级」的默认值不该留着;挂调度时在 plist 里显式写 `-p wait=0` 即可(参数本来就要逐条写,不会漏)。**2026-08-10 生产实跑修掉两个 bug**:① `_batch_names` 用 `(asin,zip) = ANY(%(pairs)s)` 传元组列表,PG 报 `FeatureNotSupported: input of anonymous composite types`——改两个平行数组 unnest;② **认账失败问早了**:批次 completed 只说明采集侧干完了,数据还在增量流里,结果 127 个组合全被判「批次已采完但无快照」,紧接着一次 product_ingest 就把这 127 条原样摄了进来。修法是新增**摄取水位线** `ops.cursors['product_ingest:last_run']`(每轮跑完都刷,哪怕 0 条),`_reap_batches` 拆两段:轮询落定归轮询落定,**只有水位线越过批次落定时刻才认账失败**。原设计的代价不止台账写错——failed 不挡重推,下一轮会把这些组合再采一遍,每小时白烧一轮配额而两侧都不报错。**采购方表已补全、轮询全链生产验收通过**(2026-08-10:151 行重判 → 通过 101 / 建议拒绝 31 / 待人工 19,台账全 done、截图 144 张已贴;剩余待人工均为标题不符/SKU 非 ASIN,需人工非代码)。待:挂调度观察 |
| 5 | upc_generator | 沃尔玛UPC生成器 | 否 | **[x] 不迁移**(所有者定稿 2026-08-07):旧版未上生产,不做迁移;新系统以后若需要此功能,按新架构新建脚本(UPC 池状态届时入 ops) |
| 6 | maintenance | 沃尔玛商品维护 | **是** | **[~] 管道就绪,清零链路做实**(2026-08-07):单一 workflow(旧三段式的 sync/poll 分别被 PG 数据源与 feed_poll 反哺器替代);意图 provider 可插拔——清零(限额表「库存特殊要求」=0 整店清零,不设二次确认,所有者定稿)已做实,**改价/改库存/改标题三 provider 已做实**(2026-08-09,采集接入后):从产品中心自动算(amz 现价×区间倍率 / stock_count / 处理后 amz 标题 vs 沃尔玛现值),**驱动方式与旧系统不同**——旧的读飞书运营决策列,新表是程序投影没有那些列;路由铁律只作用 source_type='amz' 且整店排除 stockzero;改价按**配送方式**选区间(FBA/FBM 两套边界),**定价输入是落地价 =(亚马逊单价 + 运费)× 倍率**(所有者指出 2026-08-10:此前漏了运费——旧系统读的是采集侧导出的虚拟列「总价」本就含运费,新链路改吃增量导出 fast 段后漏掉;区间也按落地价选,两处同一个数才自洽),**运费没采到(采集侧 N/A → NULL)一律不改价/不上架**,与「配送方式未知不定价」同口径(当 0 定出来的价偏低、越贵的运费亏得越多,而两侧都不报错);存量快照已从 `raw.buybox_shipping` 就地回填(否则上线当天全线停改价),配送方式取 latest_snapshot 的 `raw->>'is_fba'`(采集侧 parser 读 buybox 的 Ships from 行;契约 fast 段未列为一等字段,但 raw 未裁剪它)——**未知则不改价/不上架**(所有者定稿 2026-08-09:「这个是必须要获取的信息」,猜错一档 = 拿错倍率);改价阈值 ≥1分且≥1%,**价格出界按 300% 兜底定价**(所有者定稿 2026-08-09,此前是淘汰;只有区间内倍率未配置才不动),**库存 NULL 也写 0**(所有者定稿 2026-08-09:采不到就不卖;库里 NULL/0 仍分得清,只在决策层折叠),**货期闸 >8 天清零**(同日从旧值 12 收紧,list_new 共用),标题复用上架文案处理;路由 改价≤5/改库存≤10 走单品 PUT 否则 feed(标题恒 feed);维护记录=在线产品总表内「维护记录」工作表(只追加,反哺器回填);**飞书只留近 7 天**(所有者定稿 2026-08-09:一天几千行,旧系统靠一天一个表格绕开;`-p prune_sheet=1` 手动裁,每轮提交后自动裁,删的只是展示面板——流水永久在 ops.feed_items),配套**超 3 天未落定判「未查到」并推进水位**(免得一行悬着把水位钉死、每轮重读整段)。**维护事件入账定稿**(所有者 2026-08-07):标题/价格/库存维护(含清库存)一律**不进** catalog.product_events——清库存是店铺维度运营操作,系统不设店铺维度病历;流水在 ops.feed_log/feed_items,状态后果由 status_changed 观测入账(配套闸:receipt_in_ledger 白名单 + 反补计数 source 过滤)。**删除类并入本工作流**(所有者定稿 2026-08-09:「variant_offset_cleanup 的功能也应该放进 maintenance,这属于一个工作流」):kind='delete' 第四类 provider——`variant_offset`(亚马逊把 /dp/<ASIN> 返回成兄弟变体页,parser 拒绝写入,采集侧列为不自动重试)⇒ 价格/库存永远拿不到新数据,留着只会被前三个 provider 拿陈旧快照一轮轮跟。三个原因:variant_offset(门槛 min_batches=1,所有者:偏移了就不会恢复,不设观察期)+ **商品不存在**(amz 标题占位符,旧系统只跳过标题维护,所有者 2026-08-09 改为删除)+ **连续无货 15 天**(`-p oos_days=N` 可调;三道判据:窗口内无任何有货观测 + 至少一条明确缺货观测(防全 unknown 误判)+ 窗口两端都有观测(防中间断采);⚠ 采集接线于 2026-08-08,历史攒够 15 天前这条恒空);唯一防呆:最后一次偏移后若有 outcome=ok 快照则移出名单;守路由铁律只删 source_type='amz';单店单轮上限取限额表「下架限制」列(与 product_clear 同一口径,缺该店退 300 并告警);dry-run 单独列名单;**删除名单从其余三类里剔除**(将死的行不值得再烧配额);删除是维护事件不入病历的**唯一例外**(生死类恒记 delete_submitted)。**重复提交抑制**(2026-08-09 生产实证):首轮真跑后 feed 报错排行里最大两组是 `ERR_EXT_DATA_0101198` stale update(删除 119 + 标题 89)——provider 比的是 amz 值 vs walmart_items 上次扫店快照,提交后本地快照不变 ⇒ 下轮重算出同样的意图重发同样的载荷。已加 ops.dedupe 抑制(店铺|SKU|类型|新值,20 小时窗口,值变了照样提交)。另 38 条改库存 + 30 条改价报 SKU not found ⇒ **调度顺序硬约束:catalog_sync → product_refresh → product_ingest → maintenance**。**意图上限按店化**(所有者定稿 2026-08-26):全局 5000/类闸废除 —— 08-25 倍率调整日它把 12,766 条合法改价截成 5,000(无 ORDER BY 物理序随机截、截断只进日志不进摘要、链一天一轮"下轮"="明天"),谭总 7 家店拖了三天。改为 `MAX_INTENTS_PER_STORE`(单店单轮,数字=按店速率桶×单 feed 切片:price (8−1)/时×8000 条/feed=56000(桶:官方 10/hour 三件套共享,四代理三源复核后从 6/天上调,6/day 确证只属本仓不用的 feedType=promo;切片:官方硬限 10000 条留两成,1000 条只是建议值——所有者定稿新鲜度优先,单店整量当轮连发,如 15000 条=8000+7000 两个 feed 连续提交)、inventory 7×4000=28000、title 7×1000=7000;**−1 是补交余量**(feeds 的"双确认未达→补交一次"每次多烧一个桶名额,吃满桶时一次补交就抱锁睡一小时),三个数均远超单店目录、纯属失控护栏,三处连同窗口一致有测试钉住);delete 不设扫描期闸(执行件 cap_destructive 是唯一闸,08-24 归一)。截断按店进飞书摘要,超限组内按优先级截(价格保大偏差、库存保清零)。⚠ 代价(两侧都记):整店清零不再被数量闸挡(单店目录 < 单店上限);改价侧同理,倍率误填/区间事故会一轮全量出闸(此前全局 5000 至少封住错价面,涨跌幅闸仍在下方待办)。防线=扫描摘要**首行**的「清零 N」与「⚠ 截断」(链通知只发成功步骤首行,2026-08-26 对抗校验实证后压进首行)+ dry-run 纪律;被截断顺延的落榜行留在 withdraw keep 里不撤(撤了会被记成「商品自己恢复正常了」)。待:清零链路生产验证、涨跌幅闸(价格 provider 做实时;上量前重议)。~~maintenance.db 历史并入~~(2026-08-12 所有者拍板历史迁移整批关闭,不迁)。⚠切换前停旧 12:00 walmart-maintenance-all-stores 并先收干净旧在途 feed |
| 7 | product_clear | 沃尔玛批量下架(旧 daily_retire) | **是** | **[~] 生产验收通过**(2026-08-06,所有者确认:A107 首测 5+放量 1221 个 DELETE_ITEM,识别/限额/防重/轮询/台账/事件账本全链路);待:切旧 15:00 cron、挂调度、停用(RETIRE_ITEM)动作实测。命名原则(所有者 2026-08-06):新工作流按功能命名,不继承旧系统名 |
| 8 | problem_product_cleanup | 沃尔玛问题商品清理(旧 daily_cleanup) | **是** | **[~] 生产验收通过,PR #9 已合并**(2026-08-07,所有者确认):759 行首次全量真跑(21 店,27 反补 + 231 删除;dry-run 账目自洽对拍通过)。验收期修复:20 代理对抗审查 6 项(dedup 幽灵事件/防重只拦在途/反查排除已占用 feedId/顽固绑代际/轮询卡死/摘要分列)+ 单店隔离 + 网络波动二轮重试 + 在途/待观测拦截(均生产实证)。待:次日 catalog_sync 删除核验观测、停旧 0/6/12/18 点 cron、挂调度(catalog_sync 先行)。**归因收集尾段**(所有者定稿 2026-08-08):品牌限制/侵权类问题产品 → 品牌写 catalog.brand_blacklist + 飞书「禁止品牌收集」(旧链路 C/E 类语义;B/C/E/F/G/K 六类 ASIN 黑名单旧表消费方待确认);**品牌名取自亚马逊产品库**——随产品中心库接入后实施(新系统内部重上架已由 product_risk 防呆闸免疫) |
| 9 | catalog_sync | tools/sync_online_products | 否 | 改为写 PG catalog + 回写飞书;与采集服务改造联动。**[~] 沃尔玛侧已上线**(PR #4,47 店全量验证);待每日并跑对拍+挂调度;item_id 报表回填封存(-p item_ids=1)。**采集侧增量已接线**(2026-08-08,见 product_ingest 行) |
| — | product_ingest | (新增) | 否 | 采集服务(amazon-scraper-v4)增量 → 产品中心 catalog.products/snapshots。**全项目唯一从采集器取数的工作流**(漏斗铁律;2026-08-09 措辞校正:推送侧另有 product_refresh 全量重推与 order_audit 按邮编推,**取回只有这一条路**——数据入库口径唯一才是漏斗的本意);契约 v1 + §5.1 补遗(409 硬停/三值 stock_state/slow_hash 不透明/空值不覆盖/404 非空数据);游标 ops.cursors,空页不推进。**[~] 本机接线验收通过**(2026-08-09):88 条→44 ASIN 两层落库(标题/品牌/类目/哈希 44/44 全覆盖)、二次拉取 0 条游标不动(幂等实证)、补采 4 条后 list_new「待数据源」归零;IN_STOCK_QTY 已拍板终值 10(所有者 2026-08-12)。**生产已实跑**(所有者确认 2026-08-13:首跑为游标 0 起的一次性存量回填,此后按游标增量)。待:采集服务上 VPS 后两侧配 EXPORT_TOKEN、挂调度 |
| 10 | listing | auto_listing + match_listing | **是** | **[~] L1 跟卖验收通过 + L2 上架主链代码全就绪**(2026-08-07,PR #11/#12):L2a UPC 池 PG 化+定价、L2b 风控入库(禁售 825/黑名单 42064)、L2c spec/LLM/feed 外部件(PT 6951 全覆盖)均生产验证;L2d 主链七道闸门链 dry-run 实战通过(去重/防呆实拦),**端到端验收待采集服务**;变体分组后置。L3 自愈链**暂缓**。**L2d 攻坚暂停**(所有者定稿 2026-08-09):四轮真跑把载荷问题从 30 个错收敛到只剩 UPC 撞库(运气问题,重试自愈);代码全保留不回退,四轮错误账与续做指引见子计划「L2d 攻坚暂停」节。**2026-08-12 攻坚续做,代码迁移收官**(PR #24/#25):旧仓三路全量对照 → 六点批复当日落地(K=Unknown 自愈/跟卖库存 provider/配额切片后置/缺数据推采集闭环/manufacturer 双字段)+ 载荷漏迁补齐(LLM 提示词富元数据、Orderable 交还 LLM、零认证第四档、per-PT keyFeatures minItems、图片保序)+ 第 5 轮日期字段闸 + 接线批次三(PROHIBITED 回执分类/淘汰行回显/LLM 落盘/跟卖两处回归)+ **违禁回执自动入 ASIN 黑名单**(失败反哺上架前拦截闭环)。**当前状态:端到端验收通过(2026-08-13,3/3 SUCCESS,所有者确认)——上架功能迁移完成**;余项=三件生产验证(L1 试点/L2a/L2b 核对)+ 调度挂载 + 切换清单(旧两条调度链必须同停)+ 后置(变体分组/L4 收尾)。攻坚期沉淀的通用设施已惠及全部 feed 类型(ops.feed_item_errors 报错明细 + 排行视图 + 提交前 spec 预检 + FAILED 行重试上限)。**前端不迁移**;价格/库存同步归 maintenance provider。子计划 docs/listing_plan.md |
| — | order_center_cleanup | (新增) | **是** | 建库一次性烂账清理:删除订单不在库的售后/绩效/对账行(dry-run 默认);配套**入库侧永久过滤**(returns_sync/daily_report 已内置,防每日回流)+ recon_done 账期台账(防整期清空后被当缺失重拉)。**[x] 全店建库已执行**(2026-08-06,用户确认) |
| — | settlement_sync | (新增,原 daily_report settlement 阶段) | 否 | **[x] 摘出**(所有者定稿 2026-08-10):账期是**双周**节奏(实证 06/02→06/16→06/30→07/14→07/28),KPI 是**每日**,绑一起等于每天为一件十四天才变一次的事扫全店 48 家;与 perf_problems 同一处理方式。关账快照不可变(DISTINCT period + recon_done 台账双判据)、v3 身份 PO+SKU、烂账治理三条语义原样搬。`daily_report -p phase=settlement` 保留指路提示,不静默变成空跑 |
| — | order_center_push | (新增) | 否 | 订单中心投影到用户既有「订单中心V1」bitable 六表:销售/售后/绩效/对账程序写(按表内真实字段类型自适应),主订单表/采购信息只补首列键(人工域);全表不删行(枢纽有关联字段);PG 权威。**[~] 全店建库完成**(2026-08-06,用户确认;含本地状态零拉表+烂账治理);待:挂调度进入日常增量;Lookup 列(下单时间等)依赖主订单表关联字段接线,程序暂不写关联 |
| — | (产品事件账本) | (新增,非工作流) | 否 | catalog.product_events:产品全生命周期"病历"(上架/下架及官方原因/删除提交/回执/观测核验)+ product_risk 防呆视图;写入点 catalog_sync/feed_track/product_clear,listing 期补 入库/审核/上架前防呆。**[~] 地基就绪**(2026-08-06);cleanup 归类事件**已接**(problem_categorized,problem_product_cleanup._record_categories,类别未变不重复记);旧库 41.7 万行历史导入**已完成**(2026-08-11,cleanup_history_import);上架回执 list_feed_* 已接且违禁自动入黑名单(2026-08-12);入库/审核三事件码已登记(2026-08-13 批次 A:product_ingested/audit_passed/audit_rejected),写入方批次 B 接线 |
| — | feed_poll | (新增) | 否 | 全局 feed 轮询(所有 feed 操作共用):feed_log submitted 行 → 终态 → SKU 级结果落 ops.feed_items 权威台账;逐 feed 展示店铺/动作/进度计数;pending 行告警待人工。**[~] 生产验收通过**(2026-08-06,双 feed 实时进度实证);待挂高频调度 |
| — | perf_problems | (拆自 daily_report) | 否 | 绩效问题订单明细(insights report xlsx)→ ops.perf_problem_orders + 订单中心 orders.perf_events。**[~] 代码就绪**(2026-08-08 拆出,逻辑与拆分前一致):所有者定稿由独立调度驱动,日报不再管;待挂独立调度 |
| — | product_refresh | (新增) | **是** | 在线产品全量重推采集(**旧维护三步的第 2 步**,所有者澄清 2026-08-09:旧流程 = 获取在线产品 → 推送采集拿最新 amz 数据并自动计算 → 读决策并提交)。没有它 latest_snapshot 会越来越陈旧,而 maintenance 三 provider 照样拿陈旧数据提交。口径:每次改价前全量重推 PUBLISHED + 店铺 ACTIVE(跨店去重),不做优先级;采集器吞吐 2000~3000/分钟(不切邮编不截图);**确认推上去(拿到 batch_id)才开始计时**,超时 1 小时;命名批次便于按批次取回。v4 撞名返 409 不静默合并 ⇒ 推送可安全重试。**非 ASIN 形态的历史 SKU 直接过滤**(所有者定稿 2026-08-09:采集侧建任务时本来就丢弃——推 27722 只建了 27170——且它们永远不会有 amz 数据,不在维护范围内)。批次落定时拉 `/api/batches/{batch_id}/failures` 落 `ops.scrape_failures`(所有者问 2026-08-09「取回时会拿到某个产品没采成功的原因吗」:增量流里只有 `outcome≠ok` 的降级采集,**根本没采到的 ASIN 压根不产出记录**,必须主动拉)。**[~] 生产已实跑**(所有者确认 2026-08-13);待挂调度 |
| — | cleanup_history_import | (新增,一次性) | 否 | 旧问题商品三笔历史(error_items 41.7 万行时间线折叠 → product_events;seen 20.1 万对 → ops.cleanup_seen_categories;brand_cache → ops.dedupe)。**[x] 生产实跑完成**(2026-08-11):error_items 485,345 行 → 变迁事件 239,253 条(时间线折叠;重灌数与首跑一致,确定性实证)、seen 207,355 对、brand 2,609 ASIN(pending 实为 0)。实操修了三处:文件检查前置到重活前、seen/brand 传反按形状指纹拒绝、seen 真实形状 {"seen": [...]} 补分支(形状一直记在 legacy_survey:1350,教训:写解析器前先 grep 摸底文档) |
| — | blacklist_push | (新增) | 否 | PG 黑名单自产行 → 飞书两张收集表(投影只追加;镜像行按 src_sku 指纹排除,绝不回推;水位 pushed_at 每块落,崩了重推不丢行;追加定位先读列 A 找真空行,防 +append 富文本误判)。**[~] 生产首推已完成**(所有者确认 2026-08-13);待挂调度 |
| — | backup | (新增) | 否 | 每日 pg_dump + 备份校验,失败飞书告警(cli 统一发)。**[~] 代码完成**(2026-08-13 批次 A:-Fc 先写 .part → pg_restore --list 校验 → 原子换名;保留期清理只碰自家命名且永不删当轮产物;days≥1 硬闸)。待生产首跑 + 挂每日调度。异机恢复注意:readonly 角色不在单库 dump 里 |
| — | audit_import | (新增,一次性) | **是** | 旧审核库 walmart_audit(同实例隔壁 database)13 表 → audit schema。dry-run 逐表体检(pg_attribute/format_type 含精度列对照;源独有列/类型不符/清单表缺失=致命拒迁;replace 的 TRUNCATE 必预告);--execute 单事务 COPY + 行数校验 + 标识列 setval;源连接 REPEATABLE READ 只读。**[x] 生产实跑完成**(2026-08-13:dry-run 13 表全绿后 --execute 单事务约 614 万行全 ✓,含 audit_runs 204 万 / audit_hits 388 万;blacklist_brands 实测 42,726 行) |
| — | allocation(占用与分配) | (新增) | **是** | 品牌/产品/类目/渠道四重排他占用台账(catalog.claims,与在线快照解耦,释放只走显式动作)+ 分配引擎(硬约束闸→产品分→店铺-产品贪心匹配,第一版规则打分不用 ML/LLM)。**[~] A0 收口 + A0.5 代码就绪**(2026-08-15 校准:main 迁入审核系统后 audit_sync 撤销——审核结论由 product_audit 直写产品表;候选宇宙=catalog.products 走 product_ingest;`alloc_audit` 存量审计工作流就绪待生产首跑(P 探针 4 项 + A 审计 7 项);services/brand_key(占用键唯一出处)与 services/store_targets(限额表目标四列 loader)已建。后续 A1 占用台账 → A1.5 订单 ASIN 归一(A2 硬前置)→ A2 引擎)。子计划 docs/allocation_plan.md

| — | services_review | (新增) | 否 | 每月一次:AI 巡检 services/ 合并重复积木 |

**订单域地基(2026-08-06 v3 定稿,先于 #2/#4 落地)**:order_audit/returns_sync/绩效订单/
对账明细四链路统一行级建模——`order_line_id = ol_+sha256(PO+SKU)[:24]`,**店铺与行号
都不参与身份**(PO 沃尔玛全局唯一,店铺名是我方标签;同 PO 同 SKU 必合并为一行——
所有者实证规则,v3 由行号改 SKU,使绩效事件可直接建键)。已就绪:orders.order_lines/
return_lines/perf_events/settlement_lines 四表 + settlement_by_line/order_center/
perf_event_spans 视图;api/returns.py(时间窗成对+cursor 拼 URL 实证);recon 明细改走
CSV 端点(JSON 每期截断 1000 行实证弃用),SKU 两级解析(自带列→订单行反查);
services/order_lines.py 三源归一化积木。绩效:按 (po,metric,period) 逐周期累积,
影响范围看 perf_event_spans.still_active;带 SKU 直接建键,无 SKU 老报表行退单行订单
回填,不硬造。⚠ v2→v3 迁移:db_init 守卫自动重建三表(订单/售后/对账窗口重拉即回),
perf_events 保历史仅重算关联;生产机需重跑 db_init + order_sync/returns_sync +
daily_report settlement(-p periods=99)。

旧仓库中**不迁移**:tools/ 的 10 个救场脚本(**不含 sync_online_products.py**,
它是 #9 catalog_sync 的源文件,是活代码)、auto_listing 5 个 fix 脚本、
recover_lark_writeback.py、审核服务.py、类目映射 legacy/ 与 intermediate/、
walmart_client.py.bak、各种零引用大文件。类目映射的**产物**(映射表)作为数据导入
catalog,pipeline 代码留在旧仓库归档。

### Phase 3 — 旧系统退役

- [x] **--execute 默认值切换**(所有者 2026-08-06 立项,**2026-08-16 落地**):
      迁移期曾是"危险工作流默认 dry-run,真跑需 --execute";走进生产起改成
      **缺省即真跑,空跑用 `--dry-run`**。理由是调度:漏写 `--execute` 的后果
      是那条链**每天空转而且报成功**,比误跑更难发现。`--execute` 保留为空
      操作别名。CLAUDE.md 安全铁律条文与各调度命令已同步

- [x] **历史数据迁移**(所有者定稿 2026-08-07;**2026-08-12 所有者逐项拍板整批关闭**,明细见 backlog 第七节):系统(工作流)迁移完成后,
      还需迁移旧系统的历史数据——如以前的上架数据、错误商品记录等;
      旧格式与新格式可能不一致,届时具体规划和操作。与产品事件账本的
      旧库 41.7 万行历史导入同批统筹(格式映射、去重、时间线拼接)

- [ ] 全部工作流切换完成后,旧仓库 launchd/scheduled-tasks 清空,旧仓库转只读归档
- [ ] erp_listing_server / erp_web / erp_worker(旧 ERP 链路)不属于本次迁移,
      维持现状直至被替代

## 2. 切换规程(每条工作流必须走完)

1. **对拍**:新系统 dry-run vs 旧系统**真跑**的输出,比对结果集差异,
   连续 3 个周期无未解释差异才准切。
   ⚠️ **严禁跑旧系统的 dry-run 来对拍**:已证实旧批量下架的 `--dry-run` 只挡
   飞书写回,DELETE_ITEM 照提交不误且不留记录(legacy_survey C5)。旧系统其他
   模块的 dry-run 同样不可信,一律当真跑对待。
2. **切换窗口**:停旧调度 → 迁移该工作流的状态数据(防重记录、游标)→ 起新调度。
   危险工作流切换期间宁可空跑一轮,不许新旧并跑。
3. **回滚预案**:新版连续失败 2 次即停新起旧;因此旧调度配置删除前先归档到
   `docs/legacy_schedules/`。
4. **验收**:观察一周,ops.runs 无异常、飞书表数据正确,plan.md 打勾。

## 3. 给执行 AI 的固定工作循环

每个会话:读 CLAUDE.md → 读本文件找当前 `[~]` 项 → 干活 → dry-run 给用户看 →
用户确认后 --execute → 更新本文件状态与相关文档 → 提交。
提交信息格式:`feat(workflow名): 一句话` / `fix:` / `docs:`。
