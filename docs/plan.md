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

- [ ] api/feeds.py:统一 feed 提交/状态查询/错误报告下载(含 CSV/二进制响应)
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
| 2 | returns_sync | 售后订单同步 | 否 | 单文件,写飞书;顺手修"整表覆盖残留旧行"缺陷(多维表格按 record_id 更新,天然解决) |
| 3 | daily_report | 沃尔玛店铺日报 | 否 | 影刀 RPA 部分保持原样(仅 macOS),只改数据落点。**[~] 已实现**(kpi/problems/push 三阶段→PG;影刀只读 latest.json 不 spawn;问题订单列映射待对拍校准) |
| 4 | order_audit | 沃尔玛订单审核 | 否 | 收敛旧的双重调度(launchd 每小时 + skill 13:30 二选一);依赖采集服务 |
| 5 | upc_generator | 沃尔玛UPC生成器 | 否 | 旧版未上生产,可直接按新架构实现;UPC 池状态入 ops |
| 6 | maintenance | 沃尔玛商品维护 | **是** | 含清库存;maintenance.db 数据并入 PG listing schema |
| 7 | daily_retire | 沃尔玛批量下架 | **是** | DELETE_ITEM 不可恢复;防重状态先行(ops.feed_log) |
| 8 | daily_cleanup | 沃尔玛问题商品清理 | **是** | 旧 PG walmart_cleanup 库并入;cache JSON 状态迁入 ops |
| 9 | catalog_sync | tools/sync_online_products | 否 | 改为写 PG catalog + 回写飞书;与采集服务改造联动。**[~] 沃尔玛侧已上线**(PR #4,47 店全量验证);待每日并跑对拍+挂调度;采集侧增量待契约定稿;item_id 报表回填封存(-p item_ids=1) |
| 10 | listing | auto_listing + match_listing | **是** | 最大最后;spec 文件先入 `<DATA_ROOT>/specs/<版本>/`;分子阶段另立计划 |
| — | backup | (新增) | 否 | 每日 pg_dump + 备份校验,失败飞书告警,Phase 0 后尽早上线 |
| — | services_review | (新增) | 否 | 每月一次:AI 巡检 services/ 合并重复积木 |

旧仓库中**不迁移**:tools/ 的 10 个救场脚本(**不含 sync_online_products.py**,
它是 #9 catalog_sync 的源文件,是活代码)、auto_listing 5 个 fix 脚本、
recover_lark_writeback.py、审核服务.py、类目映射 legacy/ 与 intermediate/、
walmart_client.py.bak、各种零引用大文件。类目映射的**产物**(映射表)作为数据导入
catalog,pipeline 代码留在旧仓库归档。

### Phase 3 — 旧系统退役

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
