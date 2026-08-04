# WalmartAPI-Contral · 沃尔玛卖家 ERP(重构版)

对旧仓库 `erpAPI` 的整体重构:按沃尔玛 API 域分层封装 + 统一接线盒(registry)+
Postgres 中心库 + 飞书多维表格作为人机界面。旧项目仍在生产,本仓库逐条工作流对拍替换。

## 架构一图流

```
触发层    launchd 定时 │ 手动命令 │ (未来)网页按钮 / MCP 工具
              └────────────┴──────────┘
                      python cli.py <workflow>
                  (加载.env → flock锁 → ops.runs记录 → 执行 → 飞书通知)
                              │
workflows/   一条业务工作流一个文件:run(params) -> 结果摘要
                              │
services/    跨工作流复用积木:遍历店铺并发执行、批量写飞书去重、feed提交防重…
                              │
api/         按外部系统分文件,只做接口适配,不含业务逻辑
             ├─ 沃尔玛(按API域):items prices inventory orders returns feeds reports insights
             │   └─ _client.py  认证/token缓存/每店固定代理/429退避 ← 店铺关联风险的生死线
             ├─ feishu.py       多维表格+电子表格(分批500/单表串行写/重试)
             └─ scraper.py      亚马逊采集服务客户端
                              │
registry/    接线盒(全项目唯一允许出现 token/表ID/路径/地址的地方)
             ├─ resources.py   飞书表格与字段名清单、服务器地址
             ├─ paths.py       DATA_ROOT(默认值+环境变量覆盖)推导全部路径
             └─ db.py          唯一数据库连接入口
```

## 数据版图

| 存储 | 内容 | 说明 |
|---|---|---|
| PostgreSQL(本机 17) | catalog / listing / orders / ops 四个 schema | 业务数据+运行记录+防重状态,见 `docs/db_schema.md` |
| 飞书多维表格 | 店铺凭证、业务登记表、结果回写 | 人机界面;按表头(字段名)索引,字段名清单在 registry |
| `<DATA_ROOT>/` | .env(密钥)、specs/(官方规范,按版本)、cache/、logs/、backups/ | 不进 git |
| SQLite | 仅 cache/ 下可重建缓存 | 业务数据一律不放 SQLite |

只读访问(Metabase / NocoDB / AI 的 MCP 连接)使用 Postgres 只读角色;
写路径全世界只有 cli.py 一道门。

## 快速上手(给执行 AI)

1. 先读 `CLAUDE.md`(铁律)。
2. 再读 `docs/plan.md`(当前进度与下一步)、`docs/db_schema.md`(建表)。
3. 旧项目的踩坑参数与必须保留的行为,见 `docs/legacy_reference.md`——重构是重写代码,
   不是重新踩一遍坑。
4. 采集服务同步改造的对接约定,见 `docs/scraper_migration_brief.md`。

## 文档索引

| 文档 | 内容 |
|---|---|
| `CLAUDE.md` | 三条铁律 + 安全铁律 + 工程规范(AI 开工必读) |
| `docs/plan.md` | 迁移总计划:阶段、顺序、切换规程、验收标准 |
| `docs/db_schema.md` | Postgres 四 schema 设计与 DDL 草案 |
| `docs/feishu_tables.md` | 飞书多维表格清单、字段约定、读写规范 |
| `docs/legacy_reference.md` | 旧仓库事实清单:魔数、事故教训、各工作流行为规格 |
| `docs/scraper_migration_brief.md` | 采集服务改造简报(给采集侧 AI) |
