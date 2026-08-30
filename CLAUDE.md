# WalmartAPI-Contral 项目总纲

> 对旧仓库 erpAPI 的重构迁移:重新实现全部沃尔玛业务工作流(本质是
> 脚本+调度+数据库,不是 ERP)。旧项目仍在生产,本仓逐条工作流替换它。
> 背景与计划 `docs/plan.md`;库表 `docs/db_schema.md`;
> **规则的完整依据与事故史在 `docs/conventions.md`**(本文件只留规则本身)。

## 常用命令

```bash
python cli.py <workflow> [-p k=v ...]   # 唯一入口;危险工作流缺省即真跑!
python cli.py <workflow> --dry-run      # 空跑;AI 改完代码必须先跑这个
python -m pytest -q                     # 全量测试,改完必须全绿
python cli.py db_init                   # 执行 refdata/schema.sql(幂等建表)
python cli.py skill_export              # 改 registry/schedule.py 后重新生成 skills/
```

## 三条铁律(任何代码不得违反)

1. **依赖只准自上而下,严禁反向。**
   层次:`cli.py → workflows → services → api → registry`。
   workflows 可以调 services 和 api;api 不准 import services;任何层不准 import workflows。

2. **api 层只做外部接口适配,不写业务判断。**
   api/ 只负责"把接口调对":认证、代理、重试、分页、分批、速率限制。
   业务逻辑上移 services 或 workflows;只实现蓝图「工作流×端点矩阵」出现过的
   端点,一端点一函数,不自创签名(api 层收录规则,蓝图 §2/§7)。

3. **一切路径、token、表 ID、服务器地址只准从 registry 取。**
   硬编码飞书 token/表 ID/绝对路径/IP 即违规;新增外部资源先登记 registry。

## 安全红线(事故背景见 conventions §一)

- **沃尔玛 API 必须走 api/_client.py,每店固定出口代理,严禁直连**(直连会
  导致店铺关联封号,业务生死线)。
- **缺省即真跑**(2026-08-16 定稿);空跑用 `--dry-run`;AI 改完代码先
  dry-run、人眼确认后才跑真的(纪律,没有默认值替你挡)。
- **防重状态先落库再调接口**:feed 先写 pending 后提交;重启先对账 pending
  (反查三态 → 确认未达 → 同一方法补交)。
- **写操作永不自动兜底;换方法重试 = 重复提交制造机。**
- **新旧系统严禁对同一破坏性任务并跑**(先停旧 → 搬状态 → 起新)。

## 工程规范(展开见 conventions 对应节)

- **入口唯一**:一切执行走 cli.py(锁/ops.runs/通知/链尾缺席店重赛);
  workflow 文件只暴露 `run(params) -> 摘要`,不含 argparse、不自理调度/锁。
- **数据库连接唯一入口** `registry/db.py`;禁自行 psycopg/sqlite3 connect。
- **在营判据唯一** `services/stores.enabled_names()`;三层别混:
  registered=在册、enabled=在营、load_stores=能调 API(§二)。
- **处置建议按 `action` 分工,不看 `source`**;破坏动作只有一个出口
  (problem_product_cleanup);破坏组压制维护组,判在 `dispositions.claim()`,
  **调度顺序不许承载判据**(§三)。
- **店维工作流的失败处理只有一套标准**(2026-08-26 定稿,§四):单店隔离 →
  失败店串行补试一遍(`store_retry`,凭证死不补)→ 缺席不炸整轮、摘要**首行**
  点名 → 下游按水位避让(`store_absence`,缺席 ≠ 停用)→ 链尾逐店重赛一次
  即止。**失败归类词唯一出处 `store_retry.diagnose`**(六档:凭证失效/代理
  无效/代理波动/沃尔玛NNN/网络未达/其他)。
- **每个能力只有一条实现路径**(双轨禁止);真兜底三要件:同函数内、触发记
  日志计数、条件明确非 catch-all(§六)。
- **services 新增积木前先通读现有函数查重**;docstring 首行写"输入→输出"。
- **飞书字段名只准引用 registry 字段常量**,不写字面量(表头改名会静默坏)。
- **飞书读写只准走 api/feishu 标准通道**;限额 = 官方 × 95%,常量只在其
  「限额登记表」出生(§八;守门测试拦通道外直连)。
- **动了表同步 `docs/db_schema.md`;改了 workflow 同步对应文档。**
- 密钥永远不进 git:真值在 `<DATA_ROOT>/.env`(chmod 600),仓内只有变量名。

## 目录速查

```
cli.py          唯一入口(锁/日志/运行记录/通知/链尾重赛)
registry/       接线盒:resources.py(表与字段) paths.py(路径) db.py(连接) schedule.py(调度表)
api/            外部接口适配:_client, items, prices, inventory, orders,
                returns, feeds, reports, insights, feishu, scraper
services/       跨 workflow 复用积木(先查重再新增)
workflows/      每文件一个 run(),对应一条业务工作流
refdata/        只读参考资料(schema.sql、walmart_rate_limits.tsv 等)
skills/         生成物:调度技能包,skill_export 渲染,不要手改
docs/           conventions.md(规范全文) plan.md(计划与决策日志)
                production_cutover.md(生产定稿) api_blueprint.md(端点/配额定稿)
                db_schema.md store_events.md(店铺事件账本:上线三步与排查)
                feishu_tables.md feed_closure_audit.md
                legacy_survey.md(旧仓摸底) category_mapping.md
                multi_node_plan.md allocation_plan.md 等
```

## 开工前两问

- **要判什么东西"没用了"?** 先读 conventions §五(2026-08-14 盘点教训:
  grep 不到调用者对 workflow 无效;判不准就判活;DROP 未连库核对一律不执行)。
- **要写沃尔玛调用?** 先查 `docs/api_blueprint.md`(端点/配额/分页/feed
  schema 定稿);配额明细以蓝图 §3 为准,高危速记在 conventions §七;
  自适应退避 api/_client.py 已内置,勿自行实现。
