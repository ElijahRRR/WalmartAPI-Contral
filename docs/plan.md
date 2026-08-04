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

- [ ] 仓库骨架:cli.py / registry / api / services / workflows / refdata / docs
- [ ] `<DATA_ROOT>` 目录初始化脚本(specs/ cache/ logs/ backups/,.env 模板,chmod 600)
- [ ] registry/paths.py(默认 DATA_ROOT + 环境变量覆盖;launchd 不读 shell 配置,
      所以默认值必须能独立工作)
- [ ] Postgres:建四个 schema(见 db_schema.md)+ 只读角色 `readonly`
- [ ] registry/db.py:唯一连接入口(业务连 PG;cache 连 SQLite 时内置 WAL+busy_timeout)
- [ ] cli.py:分发 + flock 单实例锁 + ops.runs 运行记录 + 飞书成败通知 +
      dangerous 工作流强制 dry-run(真跑需 `--execute`)
- [ ] api/feishu.py:多维表格客户端(查询/批量写≤500/单表串行/重试),字段名走 registry 常量
- [ ] api/_client.py:从旧 walmart_client.py 移植认证核心
      (token 缓存 900s、每店固定代理、401 自愈、429/5xx 自适应退避、连接池)
      **移植而非重写——这 498 行是旧项目质量最高、事故最少的代码**
- [ ] 店铺凭证多维表格(用户在飞书建)→ registry 登记 → stores 读取 + 本地快照兜底
      (飞书故障时用最近一次快照,快照文件在 DATA_ROOT,不进 git)
- [ ] 端到端验证:`python cli.py ping_stores` —— 读凭证表 → 每店经代理调一个只读
      沃尔玛端点 → 结果写 ops.runs → 飞书发汇总。**此条通过 = 地基验收。**

### Phase 1 — api 层补齐(按需推进,不求一次全量)

每迁一条工作流,只补它需要的 api 域文件。必须一开始就做对的三件事
(旧项目 8 处旁路代码的根因):

- [ ] api/feeds.py:统一 feed 提交/状态查询/错误报告下载(含 CSV/二进制响应)
- [ ] api/reports.py:报告类下载(xlsx 二进制,不强制解析 JSON)
- [ ] async 支持:仅订单拉取需要,做在 api/orders.py 内部,不另起体系

### Phase 2 — 工作流逐条迁移(核心阶段)

顺序按"最独立→最复杂",每条走同一循环:
**实现 → dry-run 对拍(与旧系统输出比对,≥3 天或 ≥3 个周期)→ 切换(停旧→搬状态→起新)→ 观察一周 → 打勾**

| # | 新 workflow | 替代旧模块 | 危险 | 备注 |
|---|---|---|---|---|
| 1 | product_query | 产品ID查询产品详情 | 否 | 零状态零调度,练手验证 api 层 |
| 2 | returns_sync | 售后订单同步 | 否 | 单文件,写飞书;顺手修"整表覆盖残留旧行"缺陷(多维表格按 record_id 更新,天然解决) |
| 3 | daily_report | 沃尔玛店铺日报 | 否 | 影刀 RPA 部分保持原样(仅 macOS),只改数据落点 |
| 4 | order_audit | 沃尔玛订单审核 | 否 | 收敛旧的双重调度(launchd 每小时 + skill 13:30 二选一);依赖采集服务 |
| 5 | upc_generator | 沃尔玛UPC生成器 | 否 | 旧版未上生产,可直接按新架构实现;UPC 池状态入 ops |
| 6 | maintenance | 沃尔玛商品维护 | **是** | 含清库存;maintenance.db 数据并入 PG listing schema |
| 7 | daily_retire | 沃尔玛批量下架 | **是** | DELETE_ITEM 不可恢复;防重状态先行(ops.feed_log) |
| 8 | daily_cleanup | 沃尔玛问题商品清理 | **是** | 旧 PG walmart_cleanup 库并入;cache JSON 状态迁入 ops |
| 9 | catalog_sync | tools/sync_online_products | 否 | 改为写 PG catalog + 回写飞书;与采集服务改造联动 |
| 10 | listing | auto_listing + match_listing | **是** | 最大最后;spec 文件先入 `<DATA_ROOT>/specs/<版本>/`;分子阶段另立计划 |
| — | backup | (新增) | 否 | 每日 pg_dump + 备份校验,失败飞书告警,Phase 0 后尽早上线 |
| — | services_review | (新增) | 否 | 每月一次:AI 巡检 services/ 合并重复积木 |

旧仓库中**不迁移**:tools/ 的 10 个救场脚本、auto_listing 5 个 fix 脚本、
recover_lark_writeback.py、审核服务.py、类目映射 legacy/ 与 intermediate/、
walmart_client.py.bak、各种零引用大文件。类目映射的**产物**(映射表)作为数据导入
catalog,pipeline 代码留在旧仓库归档。

### Phase 3 — 旧系统退役

- [ ] 全部工作流切换完成后,旧仓库 launchd/scheduled-tasks 清空,旧仓库转只读归档
- [ ] erp_listing_server / erp_web / erp_worker(旧 ERP 链路)不属于本次迁移,
      维持现状直至被替代

## 2. 切换规程(每条工作流必须走完)

1. **对拍**:新旧同时 dry-run(或新 dry-run vs 旧真跑的输出),比对结果集差异,
   连续 3 个周期无未解释差异才准切。
2. **切换窗口**:停旧调度 → 迁移该工作流的状态数据(防重记录、游标)→ 起新调度。
   危险工作流切换期间宁可空跑一轮,不许新旧并跑。
3. **回滚预案**:新版连续失败 2 次即停新起旧;因此旧调度配置删除前先归档到
   `docs/legacy_schedules/`。
4. **验收**:观察一周,ops.runs 无异常、飞书表数据正确,plan.md 打勾。

## 3. 给执行 AI 的固定工作循环

每个会话:读 CLAUDE.md → 读本文件找当前 `[~]` 项 → 干活 → dry-run 给用户看 →
用户确认后 --execute → 更新本文件状态与相关文档 → 提交。
提交信息格式:`feat(workflow名): 一句话` / `fix:` / `docs:`。
