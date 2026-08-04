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
- **危险工作流默认 dry-run。** 提交 feed、DELETE_ITEM、清库存类操作,默认只打印
  "将对哪些 SKU 做什么",真跑必须显式 `--execute`。cli.py 对标记 `dangerous=True`
  的 workflow 强制此行为。
- **AI 改完代码必须先 dry-run,人眼确认输出后才允许 --execute。**
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
                scraper_migration_brief.md
```

## 写沃尔玛调用代码之前

先查 `refdata/walmart_rate_limits.tsv` 确认端点配额。高危限制:
PRICE_AND_PROMOTION feed = 6/天;单品价格 PUT = 100/小时;Insights 类全部 1/分钟;
`GET /v3/items` 带 query 参数 60/分钟。响应头 `x-current-token-count` 与
`X-Next-Replenishment-Time` 用于自适应退避(api/_client.py 已内置,勿自行实现)。
