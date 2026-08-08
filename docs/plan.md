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

## 1. 阶段划分

### Phase 0 — 地基(一次性)

- [x] 仓库骨架:cli.py / registry / api / services / workflows / refdata / docs
- [x] `<DATA_ROOT>` 目录初始化脚本(specs/ cache/ logs/ backups/,.env 模板,chmod 600)
      → `python cli.py init_data_root`(已容器内冒烟)
- [x] registry/paths.py(默认 DATA_ROOT + 环境变量覆盖;launchd 不读 shell 配置,
      所以默认值必须能独立工作)
- [x] Postgres:建四个 schema(见 db_schema.md)+ 只读角色 `readonly`
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
- [ ] api/reports.py:报告类下载(xlsx 二进制,不强制解析 JSON)
- [ ] async 支持:仅订单拉取需要,做在 api/orders.py 内部,不另起体系
- [ ] **每店 per-(store, bucket) 令牌桶**(设计定稿见 docs/api_blueprint.md 第 3/6 节,
      官方 2026-08-05 核验):各 feedType **独立** 10/hour(MP_ITEM_MATCH 20/hour),
      唯一共享桶是价格三件套;未登记的 bucket **默认拒绝而非放行**(旧系统 RETIRE_ITEM
      零限速就是未知键放行漏的)。令牌桶做在 _client 层,跨 workflow 生效。
      ※ 此条修正 legacy_survey C6 的"共享桶"推断——官方表为准

### Phase 2 — 工作流逐条迁移(核心阶段)

顺序按"最独立→最复杂",每条走同一循环:
**实现 → dry-run 对拍(与旧系统输出比对,≥3 天或 ≥3 个周期)→ 切换(停旧→搬状态→起新)→ 观察一周 → 打勾**

| # | 新 workflow | 替代旧模块 | 危险 | 备注 |
|---|---|---|---|---|
| 1 | product_query | 产品ID查询产品详情 | 否 | 零状态零调度,练手验证 api 层。**[x] 完成**(2026-08-05 生产实跑通过,PR #3) |
| 2 | returns_sync | 售后订单同步 | 否 | **[~] 单店生产验证通过**(2026-08-06,10 售后行入库并挂上订单行);待全店跑+挂调度 |
| 3 | daily_report | 沃尔玛店铺日报 | 否 | 影刀 RPA 部分保持原样(仅 macOS),只改数据落点。**[~] kpi 阶段单店对拍通过**(2026-08-06,A085,绩效/订单/结算全列对齐;结算解析改递归查找修复)。**影刀已接入**(2026-08-08,-p yingdao=1):总览 A:H 影刀输入投影(空 sellerId 过滤,A147 防线)→ spawn → 新鲜度轮询 → 回填当日卖家名称/销售状态;默认关,**停旧 walmart-kpi-daily 前严禁开启**(双 spawn 互抢)。**历史数据导入就绪**(kpi_history_import):旧「店铺KPI」每店 sheet(72 张)按表头关键词映射入 ops.store_kpi_daily(七个真实中文表头 2026-08-08 预览实证补齐),ON CONFLICT DO NOTHING 绝不覆盖,默认预览 -p apply=1 真跑;卖家名称空白问题已修(跨日延续)。**问题订单摘出**(所有者定稿 2026-08-08):绩效问题订单明细(insights report 端点 xlsx,全 API 最脆一族)+ 订单中心 perf_events 独立为 `perf_problems` 工作流,由独立调度驱动;daily_report 只取指标比率(insights summary),不再被那条链拖慢。**KPI 看板定稿**(所有者 2026-08-08):新表格两页(总览=每店最新一行全 32 列;历史=全店合一近 90 天),phase=board 整表重写;旧表 72 张分页停更归档(仅剩导入源 + 影刀输入 A:H 两个角色)。待:kpi_history_import 生产实跑(apply=1)、看板新表建表+登记+首刷、problems 列映射对拍校准、全店跑、挂调度观察。**订单列去重复拉取**(所有者认可 2026-08-08):昨日出单/销售额改读 orders.order_lines,当前双算对拍期(API 权威,库算差异记日志+摘要计数),连续对平后摘 API 拉取、order_sync 成为调度前置——"一个外部源只有一个拉取方"审计后全项目最后一处重复拉取 |
| 4 | order_audit | 沃尔玛订单审核 | 否 | 收敛旧的双重调度(launchd 每小时 + skill 13:30 二选一);依赖采集服务。**[~] 取数前半生产验证通过**(order_sync,2026-08-06 单店 38 行;statusDate/trackingURL 按线上实证修正);审核规则待采集对接后补 |
| 5 | upc_generator | 沃尔玛UPC生成器 | 否 | **[x] 不迁移**(所有者定稿 2026-08-07):旧版未上生产,不做迁移;新系统以后若需要此功能,按新架构新建脚本(UPC 池状态届时入 ops) |
| 6 | maintenance | 沃尔玛商品维护 | **是** | **[~] 管道就绪,清零链路做实**(2026-08-07):单一 workflow(旧三段式的 sync/poll 分别被 PG 数据源与 feed_poll 反哺器替代);意图 provider 可插拔——清零(限额表「库存特殊要求」=0 整店清零,不设二次确认,所有者定稿)已做实,改价/改库存/改标题**预留接口**待采集(catalog.latest_snapshot)接入填实;路由 改价≤5/改库存≤10 走单品 PUT 否则 feed(标题恒 feed);维护记录=在线产品总表内「维护记录」工作表(只追加,反哺器回填)。**维护事件入账定稿**(所有者 2026-08-07):标题/价格/库存维护(含清库存)一律**不进** catalog.product_events——清库存是店铺维度运营操作,系统不设店铺维度病历;流水在 ops.feed_log/feed_items,状态后果由 status_changed 观测入账(配套闸:receipt_in_ledger 白名单 + 反补计数 source 过滤)。待:清零链路生产验证、涨跌幅闸(价格 provider 做实时)、maintenance.db 历史并入(历史数据迁移批次)。⚠切换前停旧 12:00 walmart-maintenance-all-stores 并先收干净旧在途 feed |
| 7 | product_clear | 沃尔玛批量下架(旧 daily_retire) | **是** | **[~] 生产验收通过**(2026-08-06,所有者确认:A107 首测 5+放量 1221 个 DELETE_ITEM,识别/限额/防重/轮询/台账/事件账本全链路);待:切旧 15:00 cron、挂调度、停用(RETIRE_ITEM)动作实测。命名原则(所有者 2026-08-06):新工作流按功能命名,不继承旧系统名 |
| 8 | problem_product_cleanup | 沃尔玛问题商品清理(旧 daily_cleanup) | **是** | **[~] 生产验收通过,PR #9 已合并**(2026-08-07,所有者确认):759 行首次全量真跑(21 店,27 反补 + 231 删除;dry-run 账目自洽对拍通过)。验收期修复:20 代理对抗审查 6 项(dedup 幽灵事件/防重只拦在途/反查排除已占用 feedId/顽固绑代际/轮询卡死/摘要分列)+ 单店隔离 + 网络波动二轮重试 + 在途/待观测拦截(均生产实证)。待:次日 catalog_sync 删除核验观测、停旧 0/6/12/18 点 cron、挂调度(catalog_sync 先行)。**归因收集尾段**(所有者定稿 2026-08-08):品牌限制/侵权类问题产品 → 品牌写 catalog.brand_blacklist + 飞书「禁止品牌收集」(旧链路 C/E 类语义;B/C/E/F/G/K 六类 ASIN 黑名单旧表消费方待确认);**品牌名取自亚马逊产品库**——随产品中心库接入后实施(新系统内部重上架已由 product_risk 防呆闸免疫) |
| 9 | catalog_sync | tools/sync_online_products | 否 | 改为写 PG catalog + 回写飞书;与采集服务改造联动。**[~] 沃尔玛侧已上线**(PR #4,47 店全量验证);待每日并跑对拍+挂调度;采集侧增量待契约定稿;item_id 报表回填封存(-p item_ids=1) |
| 10 | listing | auto_listing + match_listing | **是** | **[~] L1 跟卖验收通过 + L2 上架主链代码全就绪**(2026-08-07,PR #11/#12):L2a UPC 池 PG 化+定价、L2b 风控入库(禁售 825/黑名单 42064)、L2c spec/LLM/feed 外部件(PT 6951 全覆盖)均生产验证;L2d 主链七道闸门链 dry-run 实战通过(去重/防呆实拦),**端到端验收待采集服务**;变体分组后置。L3 自愈链**暂缓**(所有者定稿:以后需要再做)。**前端不迁移**;价格/库存同步归 maintenance provider。子计划 docs/listing_plan.md |
| — | order_center_cleanup | (新增) | **是** | 建库一次性烂账清理:删除订单不在库的售后/绩效/对账行(dry-run 默认);配套**入库侧永久过滤**(returns_sync/daily_report 已内置,防每日回流)+ recon_done 账期台账(防整期清空后被当缺失重拉)。**[x] 全店建库已执行**(2026-08-06,用户确认) |
| — | order_center_push | (新增) | 否 | 订单中心投影到用户既有「订单中心V1」bitable 六表:销售/售后/绩效/对账程序写(按表内真实字段类型自适应),主订单表/采购信息只补首列键(人工域);全表不删行(枢纽有关联字段);PG 权威。**[~] 全店建库完成**(2026-08-06,用户确认;含本地状态零拉表+烂账治理);待:挂调度进入日常增量;Lookup 列(下单时间等)依赖主订单表关联字段接线,程序暂不写关联 |
| — | (产品事件账本) | (新增,非工作流) | 否 | catalog.product_events:产品全生命周期"病历"(上架/下架及官方原因/删除提交/回执/观测核验)+ product_risk 防呆视图;写入点 catalog_sync/feed_track/product_clear,listing 期补 入库/审核/上架前防呆。**[~] 地基就绪**(2026-08-06);待:cleanup 归类事件 + 旧库 41.7 万行历史导入 |
| — | feed_poll | (新增) | 否 | 全局 feed 轮询(所有 feed 操作共用):feed_log submitted 行 → 终态 → SKU 级结果落 ops.feed_items 权威台账;逐 feed 展示店铺/动作/进度计数;pending 行告警待人工。**[~] 生产验收通过**(2026-08-06,双 feed 实时进度实证);待挂高频调度 |
| — | perf_problems | (拆自 daily_report) | 否 | 绩效问题订单明细(insights report xlsx)→ ops.perf_problem_orders + 订单中心 orders.perf_events。**[~] 代码就绪**(2026-08-08 拆出,逻辑与拆分前一致):所有者定稿由独立调度驱动,日报不再管;待挂独立调度 |
| — | backup | (新增) | 否 | 每日 pg_dump + 备份校验,失败飞书告警,Phase 0 后尽早上线 |
| — | allocation(占用与分配) | (新增) | **是** | 品牌/产品/类目三重排他占用台账(catalog.claims,与在线快照解耦,释放只走显式动作)+ 分配引擎(硬约束闸→产品分→店铺-产品贪心匹配,第一版规则打分不用 ML/LLM)。**[ ] 立案,全线暂缓**(所有者定稿 2026-08-07:含 A1 地基,等产品中心库建成、审核链接通、可见真实结构与数据后校准再动工)。子计划 docs/allocation_plan.md |

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

- [ ] **--execute 默认值切换**(所有者定稿 2026-08-06):迁移期间危险工作流
      保持默认 dry-run(真跑需 --execute);全部工作流正式上线后统一评估
      改为默认真执行(届时同步修订 CLAUDE.md 安全铁律条文与各调度命令)

- [ ] **历史数据迁移**(所有者定稿 2026-08-07):系统(工作流)迁移完成后,
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
