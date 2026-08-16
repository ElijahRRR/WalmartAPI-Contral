# WalmartAPI-Contral 项目总纲

> 本文件是每个 AI 会话开工前的必读文档。本项目是对旧仓库 erpAPI 的重构迁移:
> 在新仓库重新实现全部沃尔玛业务工作流(本质是脚本+调度+数据库,不是 ERP 系统)。旧项目仍在生产运行,本仓库逐条工作流替换它。
> 完整背景与计划见 `docs/plan.md`,数据库设计见 `docs/db_schema.md`。

## 三条铁律(任何代码不得违反)

1. **依赖只准自上而下,严禁反向。**
   层次:`cli.py → workflows → services → api → registry`。
   workflows 可以调 services 和 api;api 不准 import services;任何层不准 import workflows。

2. **api 层只做外部接口适配,不写业务判断。**
   api/ 里的代码只负责"把接口调对":认证、代理、重试、分页、分批、速率限制。
   出现"如果价格低于成本就…"这类业务逻辑,说明放错层了,应上移到 services 或 workflows。

3. **一切路径、token、表 ID、服务器地址只准从 registry 取。**
   任何文件出现硬编码的飞书 token、表 ID、绝对路径、IP 地址,都是违规。
   要新增一个外部资源,先登记进 registry,再引用。

## 安全铁律(来自旧项目的事故教训)

- **沃尔玛 API 必须走 api/_client.py,每店铺固定出口代理,严禁直连。**
  每个店铺凭证绑定固定出口 IP,直连会导致店铺关联封号。这是业务生死线。
- **⚠ 2026-08-16 走进生产后改了默认值:缺省即真跑,空跑用 `--dry-run`。**
  此前是"危险工作流缺省 dry-run,真跑加 `--execute`"。改的理由:进了调度之后,
  "缺省 dry-run"这条防线只会伤到自己 —— launchd 里漏写一个 `--execute` 的后果是
  **那条链每天空转而且报成功**,比误跑更难发现(误跑至少留下痕迹)。
  `--execute` 保留为兼容别名(空操作),调度里写了也不会错。
- **AI 改完代码必须先 `--dry-run`,人眼确认输出后才跑真的。** 这条**没有取消**,
  只是从"默认值兜底"降级成"纪律" —— 默认值不再替你挡,所以更要自觉。
- **防重状态先落库再调接口。** 提交 feed 前先写 pending,成功后改 done;
  程序重启时,所有 pending 记录先去 Walmart 查实际状态再决定是否补交。
- **新旧系统严禁对同一破坏性任务并跑。** 切换某条工作流时:先停旧调度 → 搬状态 → 起新调度。

## 工程规范

- **入口唯一**:所有执行走 `python cli.py <workflow> [参数]`。cli.py 统一负责:
  加载 .env → flock 单实例锁 → 写 ops.runs 运行记录 → 执行 → 飞书通知(成功/失败都发)。
  launchd 定时、手动触发、未来的网页按钮和 MCP 工具,全部走这一条路径。
- **workflow 形态**:每个 workflow 文件只暴露 `run(params) -> 结果摘要` 函数,不含 argparse,
  不自行处理调度/通知/锁。
- **数据库连接唯一入口**:只准通过 `registry/db.py` 的连接函数访问数据库,
  禁止自行 `psycopg.connect` / `sqlite3.connect`。
- **services 新增积木前必须先通读 services/ 现有函数确认无重复**;每个函数 docstring
  第一行写清"输入什么 → 输出什么"。
- **飞书字段名只准引用 registry 中的字段常量**,不准在代码里写字段名字符串字面量
  (多维表格按表头索引,表头改名会静默弄坏所有硬编码引用)。
- **写完/改完一个 workflow,同步更新 `docs/db_schema.md`(若动了表)和对应文档。**
- 密钥永远不进 git:真密钥全部在 `<DATA_ROOT>/.env`(chmod 600),仓库中只出现变量名。

## 目录速查

```
cli.py          唯一入口(锁/日志/运行记录/通知/dry-run 强制)
registry/       接线盒:resources.py(表格与字段名) paths.py(DATA_ROOT 与路径) db.py(连接)
api/            按外部系统与沃尔玛 API 域分文件:_client, items, prices, inventory,
                orders, returns, feeds, reports, insights, feishu, scraper
services/       跨 workflow 复用的积木(先查重再新增)
workflows/      每文件一个 run(),对应一条业务工作流
refdata/        小型只读参考资料(进 git):walmart_rate_limits.tsv 等
docs/           plan.md / db_schema.md / feishu_tables.md / legacy_reference.md /
                legacy_survey.md(旧仓库全量摸底,证据级) / scraper_migration_brief.md /
                api_blueprint.md(端点定稿) / audit_migration_plan.md(审核链) /
                category_mapping.md(**类目映射链九条工作流的唯一文档**)
```

## 判某样东西"没用了"之前(2026-08-14 全项目盘点的教训)

全项目死代码盘点做过一轮:**仓库是干净的**,真能删的只有 4 处代码。但同一轮里
**10 条被判死的东西反证后全是活的**,险些误删生产链路。三条缺陷务必内化:

1. **`grep 不到调用者` 对 workflow 完全无效。** `cli.py` 是
   `importlib.import_module(f"workflows.{args.workflow}")`,无白名单——活性不在
   import 图上,而在**"它写的表还有没有别的补给线"**。正确检索式:
   `grep -rn 'INSERT INTO <schema>\.|UPDATE <schema>\.|COPY <schema>\.' --include=*.py .`
2. **"docstring 自述一次性 + 文档记 `[x] 已跑" ≠ 死。** 迁移期脚本有三种状态:
   已跑完**且数据源已冻结**(才可能死,本仓一条都没有)/ 已跑过**但数据源仍在生产
   增长**(活)/ **从未跑过、在批次待办里**(活)。
   ⚠ **"查不到执行记录"在本仓是"还没跑"的证据,不是"跑完被遗忘"的证据**——
   本仓记录纪律良好,跑过的都有 `[x]`。
3. **按名字 grep 双向出错**:有假阳性(同名局部变量)、也有假阴性(注释里提到函数名
   会让它看起来活着)。**必须 AST 引用集 + 文本 grep 双做,每个命中人眼确认是调用
   还是注释。**

**判不准就判活。** 误删一条生产链路的代价,远大于多留一个死文件。
DROP TABLE/COLUMN/VIEW 不可回滚,**未连库核对 `pg_stat_user_tables` /
`pg_stat_statements` 之前一律不执行**——"代码不读"不等于"没人 SELECT",
本仓有一批视图/列就是专门留给人工与 AI 排查用的。

## 写沃尔玛调用代码之前

**先查 `docs/api_blueprint.md`**(端点/配额/分页模型/feed schema 的设计定稿,
2026-08-05 官方核验)确认目标端点的函数是否已定义;配额明细以蓝图第 3 节三源
对照表为准(`refdata/walmart_rate_limits.tsv` 是其来源之一,个别行已被官方核验
修正)。高危限制速记:价格三件套 feed 共享桶(保守按 6/天配置);其余 feedType
各自独立 10/hour;单品价格 PUT = 100/小时;Insights performance 类 1/分钟;
`GET /v3/items` 带 query 参数 60/分钟。响应头 `x-current-token-count` 与
`X-Next-Replenishment-Time` 用于自适应退避(api/_client.py 已内置,勿自行实现)。

api 层收录规则:**只实现「工作流×端点矩阵」(蓝图第 2 节)出现过的端点**,
一个端点一个函数,分页/切片/防重等机制藏在函数内;预留端点只登记不实现;
新增函数前先对照蓝图第 7 节函数面,不自创签名。

## 同一目的多种方法的取舍(简化 vs 兜底)

- **纯历史重复**(同端点多套写法)→ 只留蓝图选定的一种,其余不迁,零兜底。
- **能力不同的两个端点**(如单品 PUT vs 批量 feed)→ 两个显式函数,由 services
  层显式 if 路由;严禁"试 A 失败自动落 B"式隐式降级。
- **真兜底**(外部 API 自身缺口,如 offset 截断补漏、飞书挂走快照)→ 藏在同一个
  函数内、触发必须记日志计数(兜底静默常态化 = 主路径已坏没人知道)、触发条件
  明确而非 catch-all。
- **双轨过渡遗留**(旧 shim 开关式并存)→ 新系统一律禁止,每个能力只有一条实现路径。
- **写操作永不自动兜底**:失败只走 ops.feed_log 反查三态 → 确认未达 → 同一方法补交。
  换方法重试 = 重复提交制造机。

口诀:兜底是补偿外部世界的缺陷,不是补偿自己的不确定。
