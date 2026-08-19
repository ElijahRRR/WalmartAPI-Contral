# WalmartAPI-Contral

沃尔玛(Walmart Marketplace)多店铺运营的自动化工作流系统。

本质是**脚本 + 调度 + 数据库**,不是 ERP:一条业务动作对应一个工作流文件,
全部经同一个入口 `cli.py` 执行,状态落 PostgreSQL,人机界面在飞书表格。

```
python cli.py <workflow> [-p key=value ...] [--dry-run]
```

- **67 条工作流**,覆盖订单、产品数据、审核、上架、维护清理、风控黑名单、
  类目映射、店铺分配、KPI 日报八个业务域;
- **11 条自动任务**在生产运行(电脑 launchd 2 条高频 + 智能体定时任务 9 条每日/每周);
- **PostgreSQL 17** 单库五 schema(49 表 / 10 视图)为唯一权威状态;
- **1563 个单元测试**。

---

## 目录

- [一、四条铁律](#一四条铁律)
- [二、架构](#二架构)
- [三、执行入口 cli.py](#三执行入口-clipy)
- [四、数据版图](#四数据版图)
- [五、外部依赖](#五外部依赖)
- [六、业务域与工作流全表](#六业务域与工作流全表)
- [七、调度](#七调度)
- [八、部署](#八部署)
- [九、出问题看哪里](#九出问题看哪里)
- [十、开发约定](#十开发约定)
- [十一、文档索引](#十一文档索引)

---

## 一、四条铁律

任何代码不得违反。完整条文见 `CLAUDE.md`。

**1. 依赖只准自上而下。** `cli.py → workflows → services → api → registry`。
workflows 可以调 services 和 api;api **不准** import services;任何层**不准** import workflows。
工作流之间要复用逻辑,把那段代码提到 `services/`。

**2. api 层只做接口适配,不写业务判断。** `api/` 只负责"把接口调对":认证、代理、
重试、分页、分批、速率限制。出现"如果价格低于成本就…"这类判断,说明放错层了。

**3. 一切路径、token、表 ID、服务器地址只准从 `registry/` 取。** 任何文件出现硬编码的
飞书 token、表 ID、绝对路径、IP,都是违规。新增外部资源先登记进 registry 再引用。

**4. 沃尔玛 API 必须走 `api/_client.py`,每店铺固定出口代理,严禁直连。**
每个店铺凭证绑定固定出口 IP,直连会导致店铺关联封号。这是业务生死线。

### 三条安全纪律

| 纪律 | 为什么 |
|---|---|
| **缺省即真跑,空跑用 `--dry-run`** | 进了调度之后,"缺省 dry-run"这条防线只会伤到自己:调度里漏写一个开关的后果是**那条链每天空转而且报成功**,比误跑更难发现 |
| **改完代码先 `--dry-run`,人眼确认输出后才跑真的** | 默认值不再替你挡,所以更要自觉 |
| **防重状态先落库再调接口** | 提交 feed 前先写 `pending`,成功后改 `done`;重启时所有 pending 先去沃尔玛查实际状态再决定是否补交 |

> **贯穿全项目的一条判断标准:静默常态化 = 主路径已坏而没人知道。**
> 兜底触发必须记日志计数;钳制必须说出来;算不出的东西不许当 0 用。
> 代码里大量 `⚠` 注释记的都是这类"错了也不报错"的坑,别当噪声删掉。

---

## 二、架构

```
触发层    launchd 定时  │  智能体定时任务  │  手动命令
              └──────────────┴─────────────────┘
                              │
                    python cli.py <workflow>
        加载 .env → flock 单实例锁 → 写 ops.runs → 执行 → 飞书通知(成败都发)
                              │
workflows/    一条业务工作流一个文件,只暴露 run(params) -> 摘要字符串
              不含 argparse、不自行处理调度/通知/锁
                              │
services/     跨工作流复用的积木(先查重再新增)
              业务判断、飞书表读写、台账、纯函数规则引擎
                              │
api/          按外部系统与沃尔玛 API 域分文件,只做接口适配
              ├─ _client.py   认证/token 缓存/每店固定代理/令牌桶/429 退避
              ├─ 沃尔玛域      items prices inventory orders returns feeds
              │                reports insights settings
              ├─ feishu.py    多维表格 + 电子表格(批量/串行写/重试)
              ├─ scraper.py   亚马逊采集服务
              └─ llm.py / llm_vision.py
                              │
registry/     接线盒:全项目唯一允许出现 token / 表 ID / 路径 / 地址的地方
              ├─ resources.py  飞书表格与字段名常量、服务地址
              ├─ paths.py      DATA_ROOT 推导全部路径
              ├─ db.py         唯一数据库连接入口
              └─ schedule.py   调度表(时间/命令/参数的唯一出处)
```

### 分层的两个落地后果

**工作流之间没有 import,链靠 cli 串联。**

```bash
python cli.py order_sync order_audit returns_sync
```

每步各拿各的锁、各写一行 `ops.runs`、各进各的日志文件;**前一步不成功就不跑后面的**;
飞书**整链只发一条**通知。参数 `-p k=v` 发给每一步,`-p 工作流名:k=v` 只发给那一步。

**扫描与执行分家。** 破坏性动作一律拆成两条工作流:

```
maintenance_scan (DANGEROUS=False,只读,产建议行)
      ↓  ops.dispositions
maintenance      (DANGEROUS=True,纯执行件,只消费建议行,自己不做决策)
```

`problem_scan` → `problem_product_cleanup` 同款。好处是"想看看今天该删哪些"
不必跑一个危险工作流,而且建议留痕、事后追得到"当初为什么删它"。

### 并发模型

| 层次 | 并发度 | 出处 |
|---|---|---|
| **跨店** | 24 | `services.stores.STORE_WORKERS` —— 每店有自己的固定出口代理,配额按 `(store, endpoint)` 计,店与店不抢同一个令牌桶 |
| **店内跨接口** | 各链自定 | 按端点配额定(如 `catalog_sync._FILL_WORKERS=8` 对应 items.get 800/min) |
| **店内同接口** | 1(串行) | 同店同端点是同一个桶,并发只会让自己排队等退避 |
| **审核判定** | 默认 128 | `product_audit -p workers=N`;线程等的是 LLM 的 HTTP,不是 CPU。**按 PG `max_connections` 余量自动钳并在摘要里说明** |

并发下的两条硬要求:**各线程各写各的局部计数,主线程按店名排序合并**
(`n[k] += 1` 是读-加-写三步,交错会丢计数且不报错;明细直接 append 会按完成
先后乱序,同一轮跑两次输出不一样,没法对拍);**飞书电子表格写按表加锁**
(各写函数内部的节流只在一次调用内部生效,跨店线程池会整体绕过它)。

---

## 三、执行入口 cli.py

`cli.py` 是唯一的执行门。launchd 定时、智能体定时任务、手动触发、未来的网页按钮和
MCP 工具,全部走这一条路径。它统一负责六件事:

1. 加载 `<DATA_ROOT>/.env`
2. **flock 单实例锁**(`<DATA_ROOT>/locks/<工作流名>.lock`,整轮持有)
3. 写 `ops.runs` 运行记录(谁触发的、跑了多久、成没成、摘要全文)
4. 按 `importlib` 分发到 `workflows/<name>.run(params)`
5. 飞书通知(成功/失败都发,链只发一条)
6. 退出码

### 退出码

| 码 | 意思 | 该做什么 |
|---|---|---|
| `0` | 成功 | 什么都不做(通知会自己发) |
| `1` | 失败 | 取日志末尾,连同失败的那一步一起看;**不要自动重跑** |
| `2` | 工作流名写错 | 调度配置与 `workflows/` 脱节了 |
| `3` | 没抢到锁,上一轮还在跑 | **不是失败,不要重试**;连着两次才值得追 |

### 参数

```bash
python cli.py list_new --dry-run              # 空跑
python cli.py product_audit -p limit=2000     # 传参
python cli.py catalog_sync -p store=A085朱丽霖  # 限单店
python cli.py order_sync order_audit -p order_audit:wait=0   # 串联 + 定向传参
```

⚠ **参数名不校验**:打错的参数名会被静默吞掉(`catalog_sync -p stores=A085` 会跑
全部店而不报错)。除 `ping_stores` 用复数 `stores=` 外,其余一律单数 `store=`。

---

## 四、数据版图

| 存储 | 内容 | 说明 |
|---|---|---|
| **PostgreSQL 17** `walmart_data` | 五 schema、49 表、10 视图 | **唯一权威**。DDL 在 `refdata/schema.sql`,说明在 `docs/db_schema.md` |
| **飞书表格** | 店铺凭证、运营填的驱动表、结果回写 | **人机界面**,不是权威。程序按**表头字段名**索引,字段名常量在 registry |
| `<DATA_ROOT>/` | `.env`(密钥,chmod 600)、`specs/`、`cache/`、`logs/`、`backups/`、`locks/`、`reports/` | 不进 git;路径唯一出处 `registry/paths.py`,可用 `WALMART_DATA_ROOT` 覆盖 |
| SQLite | 仅 `cache/` 下可重建缓存(WAL + busy_timeout) | 业务数据一律不放 SQLite |

### 五个 schema

| schema | 装什么 | 代表表 |
|---|---|---|
| `catalog` | 产品与商品主数据 | `products`(亚马逊侧主档)、`snapshots`(采集快照)、`walmart_items`(沃尔玛在架现值)、`product_events`(产品一生的病历)、`upc_pool`、`claims`(占用台账)、四张黑名单表、`llm_cache` |
| `orders` | 订单域行级数据 | `order_lines`、`return_lines`、`perf_events`、`settlement_lines` |
| `audit` | 审核 | `audit_runs` / `audit_hits`(判定明细)、`walmart_pt_meta`、`walmart_category_map`、`amazon_taxonomy` |
| `ops` | 运行与台账 | `runs`(每次运行)、`feed_log` / `feed_items` / `feed_item_errors`(feed 三层台账)、`dispositions`(处置建议)、`cursors`、`dedupe`、`rate_events`、`scrape_batches` / `scrape_failures`、`store_kpi_daily` |
| `listing` | 上架状态 | `retire_cooldown` |

**只读访问**(Metabase / NocoDB / AI 的 MCP 连接)用 Postgres 只读角色 `readonly`;
**写路径全世界只有 `cli.py` 一道门**。

### 两条贯穿全库的账本

**`ops.feed_*` 三层 feed 台账。** 所有 feed 提交(上架 / 跟卖 / 删除 / 停用 / 维护)
无一例外经 `api/feeds.submit_feed`,提交即落 `feed_log`,回执落 `feed_items`,
报错明细落 `feed_item_errors`。写在 api 层,工作流绕不过去。`feed_poll` 负责把
`submitted` 推进到终态,再由五个反哺器把结果写回各自的飞书表。

**`catalog.product_events` 产品病历。** 一个 SKU(= ASIN)从入库、审核、上架、
观测、下架到删除核验的全部事件。它是"这个商品当初为什么被删/被拒"的唯一答案来源,
也是若干判据的输入(如反补计数、顽固 SKU 判定)。

---

## 五、外部依赖

| 系统 | 用途 | 关键约束 |
|---|---|---|
| **沃尔玛 Marketplace API** | 商品、价格、库存、订单、售后、feed、报表、Insights | 每店固定出口代理;配额按 `(store, endpoint)` 计;**价格三件套 feed 共享桶**,其余 feedType 各自独立;端点/配额定稿见 `docs/api_blueprint.md` |
| **飞书开放平台** | 多维表格 + 电子表格 + 群通知 | 批量写 ≤500/次;同表串行写 + 批间节流(写 QPS≈10);字段名按表头索引 |
| **亚马逊采集服务** | 产品数据来源(标题/价格/库存/类目/运费/图) | **取回只有 `product_ingest` 一条路**(漏斗铁律);推送有三条(`product_refresh` / `order_audit` 按邮编 / `product_audit` 补采) |
| **DeepSeek** | 类目 rerank、属性映射、语义审核 | 按用途路由模型;输入哈希缓存在 `catalog.llm_cache` |
| **火山方舟(豆包)** | 视觉审核 L4 | 默认关,`-p l4=on` 开 |
| **影刀 RPA** | 日报的店铺状态抓取 | 仅生产 macOS 有效;文件交接(`input.json` / `latest.json`) |
| **USPTO 商标库** | 审核 R5 商标反查 | 跨库只读,默认关 |

---

## 六、业务域与工作流全表

标记:**危** = `DANGEROUS=True`(会写沃尔玛/不可逆);**调** = 在调度里;
**一** = 一次性/建库用。全部命令形如 `python cli.py <名字>`。

### 6.1 订单域

从沃尔玛拉单 → 审核出结论 → 投影到订单中心六表。

| 工作流 | | 做什么 |
|---|---|---|
| `order_sync` | 调 | 拉销售订单入 `orders.order_lines`。行身份 `ol_+sha256(PO+SKU)[:24]`,**店铺与行号都不参与身份** |
| `order_audit` | 调 | 五道审核出结论:钓鱼(邮编)→ 采集完整性 → 商品一致性(标题相似度)→ 配送时长 → 采购方匹配 → 限价。任一道给不出确定答案一律**待人工,绝不当通过**。只出建议,不自动下单 |
| `returns_sync` | 调 | 售后单同步(45 天窗口全量重拉) |
| `perf_problems` | 调 | 绩效问题订单明细(Insights report)→ `ops.perf_problem_orders` + `orders.perf_events` |
| `settlement_sync` | 调 | 对账明细。账期双周节奏,已入库账期**永不重拉** |
| `order_asin_normalize` | 调 | 订单行 SKU→ASIN 清洗(只补 NULL,可反复跑) |
| `order_center_push` | | 订单中心六表**手动全量补推 / 对账入口**。日常增量由各链跑完自己写 |
| `order_center_cleanup` | 危 一 | 建库一次性:删掉对不上订单的售后/绩效/对账烂账行 |
| `order_history_import` | 一 | 旧订单汇总 xlsx → `order_lines` |

### 6.2 产品数据域

沃尔玛在架现值 + 亚马逊采集数据,两侧汇进产品中心。

| 工作流 | | 做什么 |
|---|---|---|
| `catalog_sync` | 调 | 全店扫沃尔玛在架商品 + 库存 → `catalog.walmart_items`。**它是几乎所有判据的现值来源**,链里必须排第一 |
| `product_refresh` | 危 调 | 在架产品全量重推采集(维护链的数据新鲜度源头)。`-p wait=1` 阻塞等采完 |
| `product_ingest` | 调 | 采集服务增量 → `catalog.products` / `snapshots`。**全项目唯一从采集器取数的工作流**;游标在 `ops.cursors`,空页不推进 |
| `scrape_missing` | 危 | 给库里缺亚马逊数据的产品补采(分类分档 + 冷却期) |
| `brand_scrape` | | 缺品牌的 C/E 类 ASIN:推采集 → 摄取 → 品牌入账 |
| `catalog_health` | | 产品库完备度体检(纯 SQL 只读) |
| `product_query` | | 按产品 ID 查沃尔玛商品详情 |
| `node_backfill` | | 从已存快照回填类目 ID 锚(零重采) |
| `pt_backfill` | | 历史实证 PT 回填产品主档 |
| `sources_backfill` | | 在架商品来源登记簿补齐(格式回填,幂等):维护链只维护 `listing_sources` 里 source_type=amz 的行,旧系统上的存量没登记就是维护盲区;dry-run=盲区统计,真跑按 SKU 格式回填(像 ASIN→amz,其余→unknown 不自动维护)。回填后先 `maintenance_scan --dry-run` 看破坏面 |
| `pt_census` | | 沃尔玛类目(PT)四源对账:哪些 PT 真实存在 |
| `sku_normalize` | | 事件账本 SKU→ASIN 清洗 |
| `taxonomy_import` | | 亚马逊类目树入库(ID 主键) |
| `taxonomy_derive` | | 从产品自有数据反推中间层类目节点(零采集) |

### 6.3 审核域

判"这个产品能不能上"。分层:Phase0 精准拦截 → L1 类目 → L2 八条硬规则 →
L3 语义(LLM)→ L4 视觉(LLM,默认关)→ 37 条政策理由映射。

| 工作流 | | 做什么 |
|---|---|---|
| `product_audit` | 危 调 | 审核主流程。判定落 `audit.audit_runs`/`audit_hits`,结论写 `catalog.products` 五列。`-p from_sheet=1` 由上架表驱动并把结论投影回表 C~G;缺数据的行**同轮**推采集 → 等采完 → 就地摄取 → 本轮判掉 |
| `audit_why` | | 这个 ASIN 为什么是这个结论(只读排查) |
| `audit_calibrate` | | 双跑校准报告 |
| `audit_import` | 危 一 | 旧审核库 13 表一次性搬迁 |
| `audit_history_fold` | 一 | 历史审核结论折叠进产品事件账本 |

**重审政策**(唯一出处 `product_audit._DEFAULT_CANDIDATE`):没结论的审;
`pending` 隔天重试;`approved`/`rejected` **不自动重审**。要整批重审只有显式通道:
`-p asins=`(点名强审)、`-p rerule=<规则码>`(改了某条规则后定点翻案)、
`-p force_rerun=<版本>`。

### 6.4 上架域

| 工作流 | | 做什么 |
|---|---|---|
| `list_new` | 危 调 | 上架主链。七道闸门(非 ACTIVE 店 / 配额 / 风控 / 黑名单 / 全局去重 / 防呆 / 数据过滤;缺数据同轮闭环:推采集→等窗口→就地摄取→本轮续走)→ 预备期(LLM 属性映射 + spec 一致化,128 并发占位号,缺必填本地拦)→ **通过的行才领 UPC** → 按店打包提交 MP_ITEM feed → 回写上架表。**首尾各同步一次 UPC 池**,等于顺带跑了 `upc_sync`。变体自动成组(单维/多维)、组内标题差异化 |
| `match_listing` | 危 | 跟卖上架(MP_ITEM_MATCH) |
| `upc_sync` | | UPC 池注入同步与投影回写(手动体检入口) |
| `sku_locked_heal` | 危 | `SKU_LOCKED` 自愈链:RETIRE → 冷却 24h → 清列重上新 UPC |
| `variant_probe` | | 变体维度为什么没发?(只读三路诊断) |

**提交结局三态**(UPC 回收只发生在其中一态):`submitted` → 上架表 K=Yes、
UPC 标已用;`failed`(4xx 拒)→ 理由回填、UPC 回收;`unknown` → K=Unknown
(**不重复提交**)、UPC **不回收**,交给 `feed_poll` 的自愈反哺器收尾。

### 6.5 维护与清理域

| 工作流 | | 做什么 |
|---|---|---|
| `maintenance_scan` | 调 | 扫描定性,产建议行(改价 / 改库存 / 改标题 / 删除)。判据全在 `services.maintenance_intents.classify()` —— 全项目唯一一处决定"这个在线商品该拿它怎么办" |
| `maintenance` | 危 调 | 维护执行件。改价 ≤5 / 改库存 ≤10 走单品 PUT,否则走 feed;标题恒 feed |
| `problem_scan` | 调 | 问题商品扫描定性(沃尔玛后台 UNPUBLISHED/SYSTEM_PROBLEM + 审核判拒但仍在架)。尾段顺手收黑名单并投影飞书 |
| `problem_product_cleanup` | 危 调 | 问题商品处置执行件(反补 / 删除 / 停用) |
| `product_clear` | 危 调 | 飞书停用/删除表驱动的商品清理 |

**定价口径**:落地价 =(亚马逊单价 + 运费)× 区间倍率,按**配送方式**(FBA/FBM)
选区间。⚠ 运费没采到(NULL)一律不改价不上架 —— 当 0 用会定出偏低的价,而两侧都不报错。

### 6.6 风控与黑名单域

| 工作流 | | 做什么 |
|---|---|---|
| `risk_sync` | 调 | 飞书四表 → PG 镜像(类目表 / 黑名单品牌总表 / 黑名单卖家 / 黑名单亚马逊类目) |
| `blacklist_push` | 调 | PG 自产黑名单 → 飞书两张收集表(**整表重写**,带骤缩护栏) |
| `asin_blacklist_import` | 危 一 | 黑名单 ASIN 批量导入 |

⚠ **两条时间线**:否决闸在 `problem_scan` 写完 PG 那一刻就生效(上架与审核读 PG,
从不读飞书表);飞书表格是投影,`problem_scan` 收尾顺手推一次,`blacklist_push` 兜底。

### 6.7 类目映射域

沃尔玛 PT ↔ 亚马逊类目的映射维护。九条工作流,唯一文档 `docs/category_mapping.md`。

| 工作流 | | 做什么 |
|---|---|---|
| `catmap_mine` | | 从产品实证数据挖映射(纯 SQL 零 LLM) |
| `catmap_align` | | 类目路径别名对齐(纯函数) |
| `catmap_suggest` | | 缺口批量出建议(LLM,按路径去重) |
| `catmap_promote` | 危 | 把 LLM 建议升级进映射表 |
| `catmap_gap` | | 缺口清单(树 × 产品 × 映射三方对账) |
| `catmap_fix` | 危 | 按 node 定点修正 |
| `catmap_prune` | 危 | 清掉指向不存在 PT 的行 |
| `catmap_export` | | 映射明细投影到飞书 |
| `catmap_import` | 危 | 飞书 → 库(与 export 反向) |

### 6.8 店铺分配域

品牌/产品/类目/渠道四重排他占用 + 分配引擎。子计划 `docs/allocation_plan.md`。

| 工作流 | | 做什么 |
|---|---|---|
| `alloc_audit` | | 动工前的存量审计 + 数据探针(只读) |
| `alloc_products` | | 产品分体检 |
| `alloc_stores` | | 店铺经营水平体检 |
| `alloc_plan` | 危 | 产品分配方案 |
| `alloc_backfill` | 危 一 | 存量在线商品 → 占用台账 |
| `claim_audit` | | 占用台账对账:已落的占用现在还站得住吗 |
| `store_release` | 危 | 释放占用(整店 / 点名品牌 / 点名 ASIN) |

### 6.9 KPI 日报

| 工作流 | | 做什么 |
|---|---|---|
| `daily_report` | 调 | 店铺日报:KPI 指标 + 影刀店铺状态 + 看板两页 + 真发日报。`-p yingdao=0` / `-p push=0` 可关 |
| `kpi_history_import` | 一 | 旧「店铺KPI」飞书历史 → `ops.store_kpi_daily` |

### 6.10 基础设施

| 工作流 | | 做什么 |
|---|---|---|
| `feed_poll` | 调 | 全局 feed 轮询(所有 feed 操作共用)+ 五个业务表反哺器(链间并发,同表串行) |
| `backup` | 调 | `pg_dump -Fc` 先写 `.part` → `pg_restore --list` 校验 → 原子换名;保留期清理只碰自家命名 |
| `db_init` | | 执行 `refdata/schema.sql` 建五 schema(幂等)+ 只读角色 |
| `init_data_root` | | 初始化 `<DATA_ROOT>` 目录结构与 `.env` 模板(幂等) |
| `ping_stores` | | 端到端验收:读凭证表 → 每店经代理调只读端点 → 汇总 |
| `launchd_install` | 危 | 生成高频链的 launchd plist(装完就是真调度) |
| `skill_export` | | 生成智能体定时任务的技能包(从调度表渲染) |
| `cleanup_history_import` | 一 | 旧问题商品三笔历史入库 |

---

## 七、调度

**时间、命令、参数的唯一出处是 `registry/schedule.py` 的 `JOBS`。**
文档里不再抄第二份 —— 同一张表写两处必然漂,而漂了没有任何东西会报错。

两个 runner,按频率分工:

### 电脑 launchd(高频,2 条)

写死在电脑上最稳,不依赖任何智能体在不在线。装载:`cli.py launchd_install`。

| 任务 | 时间 | 跑什么 |
|---|---|---|
| `feed_poll` | 每小时 :00/:30 | `feed_poll` |
| `order_chain` | 每小时 :20 | `order_sync` → `order_audit` → `returns_sync` |

### 智能体定时任务(每日/每周,9 条)

改时间不用改代码、不用 `launchctl unload/load`,而且每次执行有个**能读日志、
能当场判断要不要重跑的东西**在旁边。提示词由 `cli.py skill_export` 从调度表渲染到
`skills/walmart-schedule/`(进 git,**生成物不要手改**)。

| 任务 | 时间(台北) | 跑什么 |
|---|---|---|
| `backup` | 每天 02:00 | `backup` |
| `daily_report` | 每天 06:40 | `daily_report` |
| `order_daily` | 每天 07:30 | `perf_problems` → `order_asin_normalize` |
| `product_chain` | 每天 13:00 | `catalog_sync` → `product_refresh` → `product_ingest` → `maintenance_scan` → `maintenance` → `problem_scan` → `problem_product_cleanup` |
| `blacklist` | 每天 15:00 | `risk_sync` → `blacklist_push` |
| `product_clear` | 每天 15:00 | `product_clear` |
| `audit_sheet` | 每天 18:10 | `product_audit -p from_sheet=1` |
| `list_new` | 每天 20:00 | `list_new` |
| `settlement` | 每周三 08:00 | `settlement_sync` |

### 当天的次序是硬约束

```
product_chain 13:00 → blacklist 15:00 → audit_sheet 18:10 → list_new 20:00
```

谁提前谁就是拿昨天的数据做今天的判断,而且**不报错**。链内的依赖同理:
`catalog_sync` 没跑成,后面的判据就是拿隔夜现值去比,会对已下架 SKU 建议改价改库存。

### 两条铁规

**同一条链绝不许两个 runner 都挂。** 撞上了后到的那次拿不到锁直接退 3 空跑一轮,
而且看起来一切正常。`tests/test_launchd.py` 钉住这条。

**调度里永不出现 `--dry-run`。** 写进去的后果是那条链每天空转而且报成功。

---

## 八、部署

### 首次

```bash
uv pip install -e . --group dev
python cli.py init_data_root            # 建 <DATA_ROOT> + .env 模板(chmod 600)
vi ~/walmart_data/.env                  # 填飞书凭据、代理、DSN 等(密钥永不进 git)
createdb walmart_data                   # PostgreSQL 17
python cli.py db_init                   # 五 schema + 只读角色(幂等)
# 飞书建「店铺凭证表」,app_token/table_id 填进 .env(字段名见 registry/resources.py)
python cli.py ping_stores               # 每店经代理连通沃尔玛 = 地基就绪
```

### 挂调度

```bash
python cli.py launchd_install --dry-run   # 先看要生成什么
python cli.py launchd_install             # 写 plist(还没生效)
launchctl load -w ~/Library/LaunchAgents/com.walmartapi.<任务名>.plist
launchctl list | grep com.walmartapi      # 回读校验

python cli.py skill_export                # 生成智能体那 9 条的提示词
cat skills/walmart-schedule/REGISTER.md   # 整篇交给智能体去注册
```

⚠ 装完**等到下一个整点看 `<DATA_ROOT>/logs/launchd/*.log`**:装上了不等于跑得起来,
解释器路径错、venv 被删这类问题只出现在那里,`logs/<工作流>.log` 里一个字都没有。

### PG 配置

`db.pg_conn()` 是一次 `psycopg.connect`(不用连接池),**审核的每个 worker 独占一条
连接**。默认 128 worker 需要 129 条,而 PostgreSQL 的 `max_connections` 缺省是 100
—— 工作流会自动按余量往下钳并在摘要里说明。要跑满就调大 `max_connections`
(改 `postgresql.conf` 后重启),不是调 `-p workers=`。

---

## 九、出问题看哪里

**先看 `ops.runs`。** 每次运行都有一行:谁触发的、跑了多久、成没成、摘要全文。
"昨天那次到底跑没跑"看这张表,不要靠回忆。

```sql
SELECT started_at, workflow, status, operator,
       finished_at - started_at AS took, left(summary, 200)
FROM ops.runs ORDER BY started_at DESC LIMIT 20;
```

**日志**一个工作流一份。目录问代码要,别写死:

```bash
tail -n 60 "$(python -c 'from registry import paths; print(paths.logs_dir())')/<工作流名>.log"
```

### 按症状查

| 症状 | 先看哪里 |
|---|---|
| 某个 ASIN 为什么被拒 | `python cli.py audit_why -p asins=B0XXXXXXXX` |
| 变体没成组 / 维度没发出去 | `python cli.py variant_probe -p asins=…` |
| feed 提交了没结果 | `ops.feed_log` 找 `submitted` 行 → `ops.feed_items` 看 SKU 级结果 → `ops.feed_item_errors` 看报错码 |
| 上架表某行一直空着 | F 列的原因 → `ops.dispositions` / `catalog.products.audit_status` |
| 某商品当初为什么被删 | `catalog.product_events` 按 SKU 查时间线 |
| 采集为什么没数据 | `ops.scrape_batches`(批次状态)+ `ops.scrape_failures`(逐 ASIN 真实 `error_type`) |
| 黑名单表格没更新 | 闸门读 PG 已经生效了;表格投影跑 `cli.py blacklist_push -p probe=1` 体检 |
| 撞限流 / 变慢 | `ops.rate_events`(稀缺桶跨进程限速状态);摘要里的 LLM 退避计数只有 `http_429` 才叫撞限流 |

### 三个"长得像正常"的故障

这三种坏法不会报错,必须知道它们的样子:

1. **feed `pending` 堵死。** 那批 SKU 每轮都报「在途防重跳过 N」而 N 不变 ——
   看着像正常防重,实际是再也发不出去了。识别信号与处置见 `docs/feed_closure_audit.md` §三.1。
2. **调度时区弄反。** 每天准时在错的时间跑,不报错、通知照发。
   台北 02:00 / 06:40 / 07:30 对应 UTC **前一天**。技能包里两列 cron 都算好了。
3. **参数名打错被吞。** `-p stores=A085` 会跑全部店而不报错(见 §三)。

---

## 十、开发约定

- **入口唯一**:所有执行走 `python cli.py <workflow> [参数]`。
- **workflow 形态**:只暴露 `run(params) -> 结果摘要`,不含 argparse,
  不自行处理调度/通知/锁。docstring 第一行写清它干什么(技能包会取这一行)。
- **数据库连接唯一入口** `registry/db.py`,禁止自行 `psycopg.connect` / `sqlite3.connect`。
- **新增 services 积木前先通读 `services/` 确认无重复**;每个函数 docstring
  第一行写清"输入什么 → 输出什么"。
- **飞书字段名只准引用 registry 中的常量**,不准在代码里写字段名字符串字面量
  —— 表格按表头索引,表头改名会静默弄坏所有硬编码引用。
- **写完/改完一个 workflow,同步更新对应文档**(动了表就更新 `docs/db_schema.md`)。
- **密钥永远不进 git**:真密钥全在 `<DATA_ROOT>/.env`(chmod 600),仓库里只出现变量名。

### 同一目的多种方法的取舍

| 情形 | 做法 |
|---|---|
| 能力不同的两个端点(单品 PUT vs 批量 feed) | 两个显式函数,由 services 层显式 `if` 路由。**严禁"试 A 失败自动落 B"式隐式降级** |
| 真兜底(外部 API 自身缺口) | 藏在同一个函数内;**触发必须记日志计数**;触发条件明确而非 catch-all |
| 写操作失败 | 只走 `ops.feed_log` 反查三态 → 确认未达 → **同一方法**补交。换方法重试 = 重复提交制造机 |

口诀:**兜底是补偿外部世界的缺陷,不是补偿自己的不确定。**

### 测试

```bash
python -m pytest -q          # 1563 passed
```

测试钉的不是覆盖率,是**"错了也不报错"的那些接缝**:参数掉了那一段白跑、
两处词表错配导致"审过了但一行也上不去"、计数只加不看导致摘要里永远看不见、
生成物与调度表脱节。加用例时优先钉这类,而不是给纯函数补样例。

---

## 十一、文档索引

### 当前有效

| 文档 | 内容 |
|---|---|
| `CLAUDE.md` | 铁律 + 安全纪律 + 工程规范(**AI 开工必读**) |
| `docs/db_schema.md` | 五 schema 设计与 DDL 说明 |
| `docs/api_blueprint.md` | 沃尔玛端点 / 配额 / 分页模型 / feed schema 定稿(**写调用代码前必查**) |
| `docs/feishu_tables.md` | 飞书表格清单、字段约定、读写规范 |
| `docs/schedule_plan.md` | 调度为什么这么排(时间表本身在 `registry/schedule.py`) |
| `docs/category_mapping.md` | 类目映射链九条工作流的唯一文档 |
| `docs/allocation_plan.md` | 店铺占用与产品分配子计划 |
| `docs/listing_plan.md` | 上架子计划:闸门链、载荷构造、变体 |
| `docs/audit_migration_plan.md` | 审核链设计与分批 |
| `docs/audit_batch_c_decisions.md` | 审核 LLM 层裁决与 L1 候选面实证结论 |
| `docs/feed_closure_audit.md` | feed 闭环审计:六提交点 × 三台账 × 五反哺器 |
| `docs/scraper_migration_brief.md` | 采集服务对接约定;采集失败三档语义 |
| `docs/backlog.md` | 未完成工作总账 |
| `docs/frontend_brief.md` | 前端设计交办单(整篇发给设计侧;含库对象、状态枚举、必须区分的三组语义) |
| `skills/walmart-schedule/` | 智能体定时任务技能包(**生成物**,改调度表后重新生成) |

### 过程档案(留档,不当任务派活)

| 文档 | 内容 |
|---|---|
| `docs/plan.md` | 迁移总计划与逐条进度记录 |
| `docs/production_cutover.md` | 走进生产那一役的定稿与验收记录 |
| `docs/legacy_schedules.md` | 旧调度权威清单(停旧依据) |
| `docs/legacy_reference.md` / `docs/legacy_survey.md` | 迁移期的来源系统摸底,记的是必须保留的行为与踩过的坑 |
