# 审核系统迁入 + 采集合库裁决(2026-08-13 调研定稿)

> 所有者提问:是否把采集服务和审核系统一起迁进本仓,共用一个数据库"搞定一切"?
> 前端要不要像 listing 那样重构统一?
> 本文是七路并行代理对三个仓(walmart-audit-system / amazon-scraper-v4 / 本仓)
> 全量取证后的裁决与方案。全部结论带证据(文件:行号);行号对应
> walmart-audit-system@a565d95、amazon-scraper-v4@e8036c4、本仓@98aa8ad。
> 已经两路对抗校验复核(48 条主张逐条核对 + 与历次拍板一致性审查),
> 唯一实证纠正:erpAPI 依赖计数 39→31。

## 〇、结论先行(两个独立决策,不捆绑)

| 决策 | 裁决 | 一句话理由 |
|---|---|---|
| **审核系统迁入** | ✅ **迁,分四批** | 它的业务 PG 与中心库**同一台 Mac 同一 PG 实例**(只是隔壁 database,跨库不能 JOIN);它的队列/worker/前端是为"人工上传文件批量预审"旧形态服务的,改成"入库自动触发"后整套退役,真正要迁的只有判定引擎(大半纯规则)+ 数据资产 |
| **采集服务合库** | ❌ **不合;把同步做密** | 契约 v1 明文禁止直写(故障互不传染);旧系统 erpAPI 实测 31 个 Python 文件依赖采集库,过渡期数月不可拆;schema 风格不相容;"不是实时"的真因是 product_ingest 没挂分钟级调度,不是架构缺陷 |
| **统一前端** | ❌ **不建;飞书为人机界面** | 审核前端(纯任务提交+进度看板,无人工复审功能)随旧形态退役;采集前端是爬虫运维控制台,留在采集侧;将来要网页控制台,按 CLAUDE.md"入口唯一"做薄壳调 cli.py,三期再议 |

## 一、审核系统真实形态(纠正三处普查偏差)

旧普查(`legacy_survey.md`)只记到"新审核系统 PG 三表 ~25k 行 + 10 worker",实际全貌:

**三体架构**:
- **任务调度器**:阿里云 47.108.92.213,FastAPI + **SQLite 队列**(不是 PG;`server/schema.sql:1-16`、`server/main.py:9-10` "不存业务数据"),nginx `/audit` 反代,7 天保留期即弃。
- **业务真身**:**开发机 Mac 本地 PG** `walmart_audit`(`config/settings.py:179` 默认 `localhost:5432/walmart_audit`)——**与中心库 walmart_data 同一 PG 实例**(本仓 `registry/db.py` 本机连接)。这是本裁决的决定性事实:跨 database 无法 JOIN,审核自建的 `products` 表与 `catalog.products` 重复存同一批 ASIN 档案,靠 scraper HTTP 再拉一遍,双份慢变字段沿不同链路更新。
- **worker**:Mac 上 10 进程 × 每进程 50 协程 = 500 ASIN 并发(`worker/poll_loop.py:4-6`、`scripts/start_workers.sh`),轮询阿里云领 CSV 切片。

**普查偏差三处**:①审核 server 部署脚本名叫 `deploy_to_dmit.sh`(位于 `server/deploy/`)但目标是阿里云,不在 DMIT VPS;②它对接的采集是 **scraper v3**(阿里云反代 `47.108.92.213/amazon`,`integrations/dmit_client.py:1`),不是 v4;③"独立部署"名不副实——worker 与业务库都在 Mac 本机,只有队列 server 在云上。

**判定流水线**(`pipelines/orchestrator.py:309-411`,21 文件 6767 行):

```
历史短路 → Phase0 → L1 类目 → L2 规则 → L3 LLM → L4 视觉 → reason_mapper → 落库
(45天TTL) (四件套)  (PT判定)  (硬命中拒) (语义4维) (图7类)   (37政策映射)
```

- **纯规则可直迁**(零 LLM):历史短路(纯 SQL:真 reject 永久短路、真 pass 45 天 TTL)、Phase0 四件套(飞书黑名单三表等值/8 大类目禁售/®™ 符号正则/36k+ 品牌黑名单等值)、L2 全部(R0 中国卖家 8 大类硬禁、R1 双白名单闸、R2 禁售大类、R3a 硬认证;打分体系名存实亡——软规则全 0 分(R4/R5/R7/R8 的 PENALTY 常量,`l2_rules.py:718,776,931,1152`),实质"任一硬命中即拒")、三个 yaml 小分类器、reason_mapper。
- **依赖 LLM/embedding**:L1 PT 判定(映射表快速通道免 LLM;否则 embedding 召回 + LLM rerank,失败降级 DB-only)、L3 语义(真品牌/冒犯/IP/CPC)、L4 视觉(≤5 图,豆包 vision)。
- **旁路工具**:trademark_audit(批查 USPTO 1400 万行商标库 + LLM 判真伪),不在 L0-L4 主线内。

**结论模型**:verdict(pass/reject/pending)+ stage_stopped_at + 两层理由码(机器 rule_code + Walmart 37 条政策 category)+ 逐条命中明细(audit_hits 表)。**恰好对上本仓 audit_* 五列 + product_events 事件明细的接缝设计。**

**数据资产盘点**(迁移时数据与代码分开搬):

| 资产 | 量级 | 迁移分类 |
|---|---|---|
| phase0 三表(sellers/asins/amazon_cats) | ~25k 行,飞书 8280e8 三列日同步 TRUNCATE 重灌 | A:进中心库;C:飞书仍为源,归 risk_sync 家族 |
| blacklist_brands | 仓内三处注释 18k/36k/~41k 不一致,**迁前实测生产库** | A+并轨:与 catalog.brand_blacklist 同源化(都来自飞书品牌总表) |
| walmart_pt_meta / pt_spec / prohibited_policy | 7033 / 6942 / 43 行,**schema.sql 无 DDL**(只有 sync 脚本 INSERT) | A:生产库 pg_dump 反推 DDL;pt_spec 可由本仓既有 spec 链重生成(其源文件 304MB pt_templates 恰来自旧 erpAPI) |
| blacklist_brand_ip_stats | 含**人工 override 列** | A:必须搬数据,不能重算 |
| walmart_error_records / violation_groundtruth | 97k 行错误日报 / 打标真值 | A:precision 规则的证据基础,迁移后做双跑校准的标尺 |
| audit_runs / audit_hits 历史 | 每 ASIN 多次审的结论史 | A:历史短路依赖它,必须搬 |
| 5 个 seed yaml(禁售大类/L3 词表/兼容品牌/NRTL/NiceClass) | 共 ~1072 行,带 precision 验证注释 | B:进 refdata/,加载路径改走 registry |
| pt_embeddings.npz | 27.8MB,6832 PT × 1024 维 | 不搬:可由 walmart_category_map 重算 |
| uspto 库 | 1400 万行,独立 database,灌库链路在外部仓 | D:暂留原地,连接串隔离良好 |
| 阿里云 SQLite 队列 | 7 天即弃 | 不搬:被 cli.py + ops.runs 取代 |

## 二、本仓接缝验收(全部在位,零欠账)

- **五列 + slow_hash**:`refdata/schema.sql:20-28` 注释齐全(pending/approved/rejected、规则版本批量重审、"变了才需要重审");全仓 grep 确认 catalog 侧零写入方——接缝干净。
- **事件登记制**:`services/product_events.py:51-53` 预留入库/审核两类事件的意图,未登记码抛错(`:131-134`)——迁入时登记常量即可。
- **消费端单点**:list_new 领任务判定 `audit_result=="pass"` 只有两处(`workflows/list_new.py:284-287、226-228`);飞书 E/F/G 字段常量已登记(`registry/resources.py:399-409`)。
- **基础设施差集近零**:飞书(api/feishu.py 32 函数,旧系统的 lark-cli 子进程整体弃用)、采集客户端(api/scraper.py 7 函数全够)、LLM 缓存(services/llm_cache.py 同构)。**真差集只有三块**:审核提示词本身、LLM 多供应商路由(旧系统主链第三方 Claude 网关+备链 DeepSeek+豆包视觉,本仓只有 DeepSeek)、黑名单的 sellers/amazon_cats 两个维度(本仓现无卖家维)。
- **触发点(一行 SQL)**:⚠ `services/product_ingest.py` 的 `updated_at=now()` 是无条件刷新,**不能**拿"audited_at < updated_at"当重审条件。正解:`_PRODUCT_SQL` 的 ON CONFLICT 里加 `audit_status = CASE WHEN products.slow_hash IS DISTINCT FROM EXCLUDED.slow_hash THEN 'pending' ELSE products.audit_status END`(IS DISTINCT FROM 手法本仓已有先例 `services/order_lines.py`),审核工作流消费 `pending OR NULL`,与游标水位线解耦,不加第三个 cursor,绝不在 pump 循环里同步调 LLM。

## 三、采集合库:红蓝对辩后不合

**蓝方(合库)的真实痛点,逐条收编**:
- "批次 completed ≠ 数据落库"两跳延迟曾致 order_audit 首跑 127 个组合误判 → 已用摄取水位线修复,即时场景已有 wait=1 就地泵闭环;
- 保留期悬崖(409 cursor_below_retention)→ 分钟级调度追平后不会触发;
- "审核要同时读两边最别扭" → **由审核迁入解决,不需要采集合库**。

**红方(不合)的决定性事实**:
1. **erpAPI 实测 31 个 Python 文件引用采集服务**(算上 8899 端口引用共 33 个;/workspace/erpapi@d5237fb 实测),契约明令"新旧并存数月,不改现有接口"——采集库在过渡期内根本不可拆,合库只能是"加一路直写"= 双写,恰是 CLAUDE.md 禁止的形态。
2. 契约 v1 白纸黑字"不要直写中心库……故障互不传染"(brief 第四节第 3 条),且刚经两侧对账实测验收(409/null≠0/邮编分波全趟过),是沉没价值不是包袱。
3. schema 不相容:采集库为对齐 SQLite 钉死 text 时间戳/integer 布尔/列序即 API 契约(`SELECT d.*` 直接泄进 erpAPI 响应);outbox 与业务写同事务,拆表需 2PC。
4. 目标态(采集上 VPS)= 公网直连 Mac PG,而采集侧现状近乎零鉴权(见第六节安全发现),安全面不可接受。
5. 写入冲击:峰值 3000-5000 ASIN/min 的写流 + 分区 DROP,不该压在跑改价/订单审核的生产库上。

**"不是实时"的正解**:v4 当前与中心库同机,同步链路本身毫秒级;延迟全部来自 product_ingest 的调度间隔。挂 5 分钟级调度(游标增量,空转成本一次 HTTP 空页),延迟即压到分钟级。上 VPS 后同理。

## 四、目标架构

```
DMIT VPS(采集,目标态)                 生产 Mac(一个 PG 实例 = 所有业务真相)
┌──────────────────────┐               ┌───────────────────────────────────────┐
│ amazon-scraper-v4    │  HTTP 契约 v1  │ WalmartAPI-Contral(cli.py 唯一入口)   │
│ · 自库 scraper_dev   │ ◄──────────── │ PG walmart_data                        │
│ · 运维前端【保留】    │  增量导出(5min)│  ├ catalog.products(audit_* 五列启用) │
│ · worker 池          │  推批/截图/状态 │  ├ audit schema(规则字典+runs/hits)   │
└──────────────────────┘               │  ├ ops.*(runs/cursors/events)         │
                                       │ workflows/product_audit(增量自动触发)  │
【退役】阿里云 SQLite 队列+审核前端      │ workflows/risk_sync(+phase0 三表镜像)  │
【退役】10 worker 进程                  │ 人机界面 = 飞书表(pass 自动进上架表)   │
【暂留】uspto 库(D 类,同实例独立库)    └───────────────────────────────────────┘
```

数据流闭环:采集入库(product_ingest,slow_hash 变更自动置 pending)→ product_audit
增量审核(短路→Phase0→L1→L2→[L3→L4])→ 写 audit_* 五列 + audit_runs/hits + 审核事件
→ 投影飞书上架表 D(=walmart_pt)/E(=pass|reject)→ list_new 领任务上架。
人工从"逐个看产品"退到"只处理 pending(LLM 全链故障)与抽检"。

**漏斗纪律显式化**:product_audit 全程只读中心库,禁止 import api/scraper 取数
(漏斗铁律:取回只有 product_ingest 一条路);待审行缺数据(如 L4 缺图)走既有
推采集闭环——推送合规,取回仍等 ingest 落库,下一轮审核自然消化。

## 五、批次分解(粗版;细化定稿见第十一节,以十一节为准)

- **批次 A|数据地基**(纯搬运,零行为变更)。**前置:backup 工作流先上线**,或至少
  迁移当日两侧手动 pg_dump 留档——97k 错误日报、25k 三表、audit_runs 全史要灌进
  至今零备份的生产库,搬完旧库归档即孤本。内容:audit schema 建表(三张无 DDL 表
  从生产 pg_dump 反推;同步 docs/db_schema.md 与 db_init 幂等块)、A 类数据迁入、
  seed yaml 进 refdata/ 并改 registry 取路径、blacklist_brands 与
  catalog.brand_blacklist 并轨方案、registry 补两处登记(db.py 增 uspto 只读连接
  函数,杜绝实现期顺手 psycopg.connect;resources.py 登记飞书 8280e8 三列表并
  回销 backlog 第十节该条待办)、product_events 登记入库/审核事件码、ingest 触发
  一行 SQL(slow_hash 变更置 pending)。
  ⚠ 划界:搬 audit_runs/hits 属**功能状态搬迁**(切换规程"停旧→搬状态→起新"的
  搬状态——历史短路直接查它,不搬则迁入首日全量重审),不属 2026-08-12 已整批
  关闭的 erpAPI 历史数据迁移(那批范围 = backlog 第七节六项,且是另一个仓)。
- **批次 B|纯规则 MVP**(零 LLM,即可拦截全部明确违禁):services/audit_rules.py
  (历史短路+Phase0 四件套+L2 硬规则+reason_mapper)+ workflows/product_audit.py
  (run(params),支持 asins=/force_rerun=/limit=;**标 DANGEROUS=True**——它不碰
  沃尔玛,但写 E 列即直接驱动上架提交,属间接破坏性写;dry-run 语义 = 只落
  audit_runs/hits,不写 products.audit_* 五列、不投影飞书)+ risk_sync 扩展
  phase0 三表镜像(镜像语义定死为**单事务全量重灌**,与旧系统同款——risk_sync
  家族现行"只增改不删"语义会在飞书删行后残留幽灵行,本表不适用)。
  **并跑期纪律:E/D 列投影推迟到批次 D 切换日**——旧审核与人工仍在按旧结论填 E,
  切换前新系统只落库,双跑对比在库内做(audit_runs vs 旧库结论),绝不双写同列。
  交付含测试:audit_rules 纯函数单测 + violation_groundtruth 黄金集回归夹具
  (离线,不打 LLM);run 摘要带 pending 计数与最老龄期(超阈值飞书告警依赖
  FEISHU_WEBHOOK_URL,配置前只进日志——已知缺口);audit_version 语义随本批定稿
  (谁 bump/格式/按版本批量置 pending 的执行通道——乱定一次 = 全量重审成本事故)。
  用 walmart_error_records(97k)与 violation_groundtruth 双跑校准合格后才 --execute。
- **批次 C|LLM 层**(逐层带决策项):L1 PT 判定(walmart_pt 直接喂 list_new,
  替代现在 LLM 每次现猜 PT)→ L3 语义 → L4 视觉。每层独立验收,供应商见第七节决策项。
  两处旧系统隐式行为随迁裁决:①L1 "embedding 失败降级 DB-only" 只能按"真兜底"
  保留(触发记日志计数、条件封闭)或砍掉,不许静默;②L3 故障不对称按第六节第 2 条
  修正后迁。llm_cache 清理器"暂不做"的拍板原话是"上量后再议"——批次 C 就是上量,
  届时重议。
- **批次 D|切换退役**:双跑对比收官(库内抽样比对新旧结论)→ **E/D 列投影切换**
  (新系统开始写,listing_sheet 人工域口径注释同步改)→ 停旧:黑名单同步有
  **两套调度都要停**(生产 07:05 cron `sync-blacklist-brands-daily` + 审核仓内
  7:00 launchd plist `com.nextderboy.audit-sync-blacklist`),stop_workers,
  阿里云队列 server 下线 → 旧仓归档。遵循"先停旧调度 → 搬状态 → 起新调度"铁律。

## 六、顺手取证的安全/质量发现(独立于迁移决策;处置节奏见各条)

1. **采集侧鉴权近乎裸奔**:worker 的 X-Worker-Api-Key **服务端无校验代码**(全仓 grep
   零命中);ADMIN_TOKEN 可选且只保 9 个破坏性端点,`DELETE /api/database` 可清全库;
   EXPORT_TOKEN 可选(其 docstring 自己写"不配等于把整个商品库敞在互联网上")。
   **归入 backlog 第六节运维后置账,但列为采集上 VPS(公网)的硬前置**——与既有
   待办 SCRAPER_EXPORT_TOKEN 合并执行,上 VPS 前必须配齐三 token。
2. **L3 故障不对称是坑,迁移时不照搬**:旧代码"LLM 全链故障→pending 待人工"是对的,
   但"坏 JSON/其余异常→pass"(`l3_llm.py:757-804`)意味着 LLM 抽风即放行。
   新实现改为:解析失败重试,重试尽→pending,绝不默认放行。
3. **不迁的死代码**:l2 的商标符号规则(与 phase0_trademark 重复,evaluate 已不调用);
   reason_mapper docstring 与代码矛盾(以代码为准:硬规则优先于 L3)。
4. **文档滞后陷阱**:采集侧 local_macos_setup.md 还说默认 SQLite,实际 dbfactory
   默认已是 postgres——按文档操作会误判生产后端。

## 七、待所有者拍板的决策项(2026-08-13 已批复,记录见第九节;本节保留原问题供对照)

1. **LLM 供应商面**(批次 C 前必须定):旧审核主链是第三方 Claude 网关
   (kiro-claude@zz.211b.site)+ 备链 DeepSeek + 豆包视觉 + DashScope embedding;
   本仓现只有 DeepSeek 直连。迁入时用哪套?(涉及成本、稳定性与合规,纯业务决策;
   多供应商路由器 llm_router 1029 行是否值得随迁,取决于此答案。)同时要定
   **路由形态**:旧系统"主链失败自动落备链"是跨供应商自动 failover,按 CLAUDE.md
   取舍口诀只能定性为真兜底(触发记日志计数、条件封闭)或砍成单链。
2. **L4 视觉层去留**:每产品 ≤5 图的视觉审核成本最高,旧系统仅 pass 产品才跑。
   增量形态下要不要保留?
3. **uspto 库(1400 万行)与 trademark-data/tro-scraper-matrix 外部仓边界**:
   建议维持 D 类暂留(R5 商标反查继续跨库连它,连接经 registry/db.py 唯一入口),
   外部灌库链不进本仓——与 backlog 第五节第 7 条(:114)"边界暂放"一致,
   此处只是把"暂放"具体化为 D 类。
4. **E/F/G 列权责与存量过渡**:从"人工域"改为"审核服务域"(listing_sheet.py:4-11
   口径注释要改)。三个连带子问题一并定:①存量人工 pass 行首刷时是否豁免投影
   (不豁免会覆盖人工结论);②机器结论要不要带标记以便与人工结论区分(如 F 列
   理由前缀);③人工改判走什么通道——建议独立 override 列而非混写 E 列
   (旧系统 blacklist_brand_ip_stats 的正面先例:override 单列存放,重算不覆盖)。
5. **存量首刷范围**:catalog.products 现有存量是否全量补审,还是只审新增量?
   (历史短路表迁入后,旧系统已审过的 ASIN 会自动短路,增量成本可控。)
6. **调度密度**:product_ingest 提到 5 分钟级、product_audit 挂每小时,是否同意。
7. **阿里云 47.108.92.213 去留**(队列 server 退役后该机上还剩 scraper v3 反代,
   erpAPI 过渡期仍在用,建议随停旧切换一并退役)。
8. **在架商品重审 reject 的处置链**:slow_hash 变更会把已上架产品翻回 pending,
   重审若得 reject,结论接谁?(仅落库+告警?接问题商品清理链下架?)不定义,
   重审 reject 就是写了没人读的死结论;定义成下架,它就是破坏性动作,
   dry-run/限额/防重全套要跟上。
9. **45 天 pass TTL 存废**:旧系统靠"每次批量上传全过流水线"让 TTL 生效;新形态
   唯一重审入口是 slow_hash 变更——hash 不变的产品永不重审,TTL 变死条款。
   要么加定期补触发(pass 超 45 天置 pending,需评估 LLM 成本),要么显式宣布
   TTL 语义随批量形态退役。

## 八、本文与既有文档的关系

- `docs/backlog.md:114` "TRO/商标/新审核系统三条跨仓黑名单链边界**暂放**"
  → 本文第七节第 3 项给出具体化建议,**拍板前 backlog 状态不变**。
- `docs/scraper_migration_brief.md:66-68` 二期审核对接("审核服务将读 catalog.products
  待审行、写回结论")→ 本文即其落地方案;brief 第四节第 3 条(不直写中心库)继续有效。
- `docs/plan.md` 迁移进度总览 → 待所有者批准本方案后,增补 product_audit 工作流行。

## 九、所有者批复记录(2026-08-13)

| # | 决策项 | 批复 |
|---|---|---|
| 1 | LLM 供应商 | **DeepSeek,分用途选模型**;视觉 DeepSeek 无能力,暂用豆包(设计落地见 10.2) |
| 2 | L4 视觉 | **保留**,仅 pass 产品才跑,**默认关闭**(参数显式开启) |
| 3 | uspto/外部仓 | 商标继续连 uspto 库;品牌黑名单直接连本库(已有);tro-scraper-matrix 是 TRO 法院禁令案件的采集仓(黑名单的上游之一,非黑名单本身),采集链不进本仓 |
| 4 | D/E/F/G 权责 | **已定稿(2026-08-13 二次批复)**:所有者往上架表填 ASIN 后,D/E/F/G 四列全部由审核链映射投影,**表只是给所有者看的展示面**;上架链拿 ASIN **直接查库**读结论,不再以 E 列为判定依据(见 10.7) |
| 5 | 存量首刷 | 历史导入时产品事件已带审核记录,**只补刷**(无结论的才审) |
| 6 | 调度密度 | ingest 5 分钟级 / audit 每小时,按原建议执行(所有者未提异议;有异议再调) |
| 7 | 阿里云去留 | 所有者正式切换时一并处理(归批次 D) |
| 8 | 在架 reject 处置 | **建议与执行分离**:审核对在架产品只产处置建议+事件,绝不直接改在线状态;建议接问题商品清理链执行,以 feed 回执+真实在线观测确认生效后才改状态;并且"拉在线→扫描问题→建议→执行"整条链要拆开(架构见 10.4) |
| 9 | 45 天 TTL | **废除**;hash 驱动重审——slow_hash 变更时 pass 翻 pending,**reject 永不自动重审**(force_rerun 手动通道保留) |
| 10 | 实证类目反哺 | 海量在线产品与历史报错数据里有沃尔玛认定的真实类目,重审/重上架**最优先直接用它**(设计见 10.3) |

**#4 原后注(存量豁免/override 列方案)已被二次批复取代**:表降级为纯展示后,
"人工与机器双写同列"的冲突不复存在,override 列不需要建。人工改判今后不经表
——如需人工强制通过/拒绝,后续加 cli 通道或驱动表(与 maintenance 人工驱动表
同款"需要时再建"惯例)。

**架构整体确认**:所有者 2026-08-13 二次批复确认第十节架构按计划所定。

**三次批复(2026-08-13,黑名单中心统一)**:Phase0 从旧审核系统的独立三列表
读黑名单"逻辑不对,直接使用黑名单数据中心,只维护一份数据库"。落地:

- **四道闸全部直读 catalog 黑名单中心**:ASIN 闸 ← `catalog.asin_blacklist`;
  品牌闸 ← `catalog.brand_blacklist`(飞书黑名单品牌总表 wiki
  `UhZJw3EtsiFN9skbG0Ac24Dgn4b` sheet=jF8dOw,risk_sync 既有同步);
  卖家闸 ← `catalog.seller_blacklist`(**新表**,同 wiki sheet=B19LKn 镜像);
  类目闸 ← `catalog.amazon_cat_blacklist`(**新表**,同 wiki sheet=twjmql 镜像,
  存归一化值)。两张新表由 risk_sync 以 TRUNCATE 全量重灌镜像
  (空读绝不重灌 + 骤缩超 50% 拒绝两道护栏,独立事务)。
- **audit.phase0_sellers / phase0_asins / phase0_categories / blacklist_brands
  四表退役**,降级为批次 A 历史快照,不再被任何代码读取。
  兼容 yaml(additional_hard_brands,34 个品牌)一并废除——品牌单一来源
  是飞书总表,需要保留的品牌由所有者加进总表。
- **历史继承 ASIN 导入**:所有者提供 ~19,831 行黑名单 ASIN 文件,经
  `asin_blacklist_import` 工作流去重导入 `catalog.asin_blacklist`
  (category='LEGACY',source='历史继承',已有键 DO NOTHING 不覆盖)。

**病历回填(2026-08-13 所有者需求)**:病历里要一眼看到历史审核结论。
沿用 cleanup_history_import 先例(时间线折叠),`audit_history_fold`
一次性把历史 audit_runs(SHORTCUT 排除、product_audit 首跑前)折叠成
每 ASIN 的结论**变迁点**事件(audit_passed/audit_rejected,
source=audit_history_fold,occurred_at=原时间戳);幂等=按 source
擦净重灌。204 万 runs 预计折到十几万事件,预览模式先报体量。

## 十、审核域架构定稿(2026-08-13 草案;所有者确认后再出细化迁移计划)

### 10.1 总结论:现有五层架构完全适用,零破例

审核 = 规则+数据+判定,与本仓"脚本+调度+数据库"同型。逐层归位:

```
cli.py       product_audit(标 DANGEROUS,dry-run 强制)
workflows/   product_audit.py  增量主流程:领 pending → 判定 → 落库+事件
             problem_scan 与执行件分离(10.4;risk_sync 扩展黑名单中心
             卖家/类目两张镜像表——三次批复后取代原 phase0 三表方案)
services/    audit_rules.py    纯规则积木:实证类目短路→历史短路→Phase0→L2→理由映射
             audit_llm.py      L1 rerank / L3 语义的提示词构建与解析(复用 llm_cache)
api/         llm.py 扩 purpose 选模型(仍只 DeepSeek 一家、一条链)
             llm_vision.py 新增(豆包,仅 L4)——符合"api 按外部系统分文件"规范
registry/    resources.py 登记审核表族/黑名单中心 wiki 两 sheet/用途→模型映射
             db.py 增 uspto 只读连接(数据库连接唯一入口)
数据         catalog.products.audit_* 五列 = 结论权威
             audit schema:audit_runs/audit_hits 明细 + 规则字典表
             product_events:入库/审核事件码(登记制)
```

依赖方向、dry-run 铁律、registry 取物、事件登记制全部沿用。唯一的新形态
"处置建议"不是破例,恰是仓内既有**意图模式**(maintenance_intents:provider
产意图 → maintenance 消费执行)向问题商品域的推广。

### 10.2 LLM 设计(批复 #1/#2 落地)

- registry 登记"用途→模型"映射,env 逐用途覆盖:`DEEPSEEK_MODEL`(默认,
  现有 mapper 沿用)、`DEEPSEEK_MODEL_AUDIT_L1`、`DEEPSEEK_MODEL_AUDIT_L3`…
  未配置的用途回落默认模型。api/llm.py 的 chat_json 增 `purpose` 参数按
  登记选模型;llm_cache 键已含 model,不同用途天然分缓存。
- 视觉:api/llm_vision.py(豆包/火山方舟)仅 L4 用;L4 默认关闭,
  workflow 参数显式开启,且仅对 pass 产品跑(批复 #2)。
- **单链无自动降级**:DeepSeek 失败 = 同链重试,重试尽 → pending 待人工
  (顺带修正旧系统"坏 JSON→pass"的事故面);跨供应商 failover 不迁。

### 10.3 L1 类目判定:实证类目最优先(批复 #10)

判定顺序,前一级命中即出:
1. **沃尔玛实证 PT**:catalog.walmart_items.product_type(在架同 ASIN,
   catalog_sync 每日写,schema.sql:127 已有)或历史成功上架记录
   → 直接采用,pt_source='walmart_confirmed'。沃尔玛自己认过的类目就是
   标准答案,重审与重上架同吃这一级。
2. 映射表精确命中(walmart_category_map 唯一高置信直出,免 LLM)。
3. 关键词/前缀 SQL 候选 → DeepSeek rerank(purpose=audit_l1)。
旧系统的 embedding 召回层**裁掉**:上面三级已覆盖其功能,为一层召回引入
DashScope 整个外部依赖不值;精度不够再议。

### 10.4 在架商品链路:拉取/扫描/建议/执行四段分离(批复 #8)

```
catalog_sync(拉在线,已有,不动)
  ↓
扫描件 problem_scan:归类问题商品(现 problem_product_cleanup 的归类段
  + 审核 reject 的在架产品 + 未来 TRO 命中)→ 写产品事件 + 落处置建议
  ——不改任何状态、不提交任何 feed
  ↓
执行件(cleanup 家族):消费建议 → dry-run/--execute → 提交 feed
  ↓
生效追踪(复用既有纪律):feed 回执落定 + 下轮 catalog_sync 真实在线观测
  (resolved_at vs last_seen_at)确认后,才更新状态 + 记事件
```

审核对在架产品**只产建议不动状态**。建议表形态与 problem_product_cleanup
的拆分幅度进迁移计划定稿——它已具备"读库决策+观测确认"纪律,拆分是重排
不是重写。

### 10.5 重审触发(批复 #9 落地)

- ingest 一行 SQL:slow_hash 变更 → **仅 approved 翻 pending**;rejected
  永不自动重审(force_rerun 手动通道保留)。
- "改参数不改产品"的顾虑已被采集契约消化:slow_hash 只覆盖标题/品牌/类目/
  图片等慢变字段(brief §5.1,不透明值),价格/库存/邮编等参数变化不动它,
  不会误触发重审。
- 45 天 TTL 随批量形态退役,不迁。

### 10.7 消费端定稿(批复 #4 二次批复落地)

- **PG 是唯一判定权威**:上架链(list_new,将来 match_listing 同理)拿 ASIN
  直接查 `catalog.products.audit_status='approved'` 领任务,`walmart_pt`
  一并从库取——不再读表 E 列。
- **上架表 = 展示投影**:所有者往表里填 ASIN 即选品入口;审核链自动审
  (产品不在库则走既有推采集闭环补数),完成后把 D(=walmart_pt)/E(=结论)/
  F(=理由)/G(=审核日期)回填表中对应行,纯粹给人看。投影实现与
  heal_unknown/write_data_cols 同款形态(读表 ASIN 列→查库→批量回写)。
- **并跑期风险消解**:切换前旧流程照旧(人工填 E、list_new 读 E);切换日
  一次性完成"上架链改查库 + 投影开闸 + 表口径注释改",不存在双写同列窗口。

### 10.6 架构层面明确不进本仓的清单

阿里云 SQLite 队列与 worker 体系(cli.py+ops.runs 取代;补刷量小无需分布式)、
审核前端(飞书为界面)、lark-cli 子进程(api/feishu 取代)、llm_router
多供应商路由 1029 行(单链化后无用武之地)、embedding 召回(10.3)、
L2 商标符号死代码、"坏 JSON→pass"行为。

## 十一、细化迁移计划(2026-08-13,待所有者确认后开工)

五个批次,顺序 **A → B → C → E → D**(E=问题商品链四段分离,须在总切换 D
之前完成,使切换日审核→建议→执行链完整)。每批一个 PR、576+ 测试全绿、
所有者验收后进下一批。

### 批次 A|数据地基(纯搬运,零行为变更)

**前置 A0**:backup 上线——`workflows/backup.py`(pg_dump walmart_data 全库
→ DATA_ROOT/backups/,保留 N 天,cli 调度日跑);至少也要迁移当日两侧手动
pg_dump 留档。**这是硬前置**:97k 错误日报、25k 三表、audit_runs 全史进的是
至今零备份的库。

| # | 任务 | 文件 |
|---|---|---|
| A1 | audit schema DDL:规则字典 9 表(blacklist_brands、phase0 三表、walmart_category_map、pt_meta、pt_spec、prohibited_policy、blacklist_brand_ip_stats)+ 证据 2 表(violation_groundtruth、walmart_error_records)+ 结论 2 表(audit_runs、audit_hits);三张无 DDL 表先 `pg_dump -s walmart_audit` 反推 | `refdata/schema.sql`、`workflows/db_init.py` 幂等块、`docs/db_schema.md` 审核章节 |
| A2 | 数据搬迁(同实例跨库):`pg_dump walmart_audit --data-only -t <表>` 管道灌入 audit schema;搬迁脚本带两侧行数对比校验;blacklist_brands 顺手实测真实量级(三处注释 18k/36k/41k 之谜落定) | 一次性脚本(scripts/ 或 workflow 形态,dry-run 打印行数) |
| A3 | 5 个 seed yaml 进 `refdata/audit/`(原样拷贝,含 precision 验证注释) | `refdata/audit/*.yaml` |
| A4 | registry 登记:uspto 只读连接函数;飞书 8280e8 三列表;LLM 用途→模型映射(AUDIT_L1/AUDIT_L3,env 逐用途覆盖回落 DEEPSEEK_MODEL;视觉不进映射表——走 api/llm_vision.py 的 ARK env,见 10.2);事件码常量登记(product_ingested / audit_passed / audit_rejected)。audit 表名常量随批次 B 首个消费方登记(PG 内部表名仓内惯例为 SQL 内联,非铁律 3 的外部资源) | `registry/db.py`、`registry/resources.py`、`services/product_events.py`(仅登记常量) |
| A5 | 文档回销:backlog 第十节 8280e8 条、db_schema.md | docs |

**验收**:db_init 幂等重跑通过;逐表行数两侧对齐;测试全绿(零行为变更)。
**所有者侧**:提供生产 `walmart_audit` 库访问确认(同实例,无需网络配置);
批准 backup 上线。

> ✅ **批次 A 生产验收通过(2026-08-13)**:db_init 五 schema 幂等就绪;
> backup 首跑 1554.5 MB 并通过 pg_restore 校验(生产库第一份备份);
> audit_import dry-run 13 表全绿(三张反推表列对照零红)→ --execute
> 单事务搬迁全 ✓:blacklist_brands **42,726**(三处注释之谜落定)、
> category_map 15,987、phase0 三表 1,314/19,798/11,810、ip_stats 3,724、
> groundtruth 18,602、error_records 97,002、pt_meta 7,008、pt_spec 6,942、
> prohibited 37、**audit_runs 2,040,425、audit_hits 3,876,594**(合计约
> 614 万行,历史短路的本钱比预期厚得多)。
> 途中生产实证两修:db_init 退役清理块重跑幂等(嵌套 IF)、
> backup 支持 PG_BIN_DIR(Homebrew 双版本 16/17 共存)。

### 批次 B|纯规则 MVP(零 LLM,只落库不投影)

| # | 任务 | 文件 |
|---|---|---|
| B1 | ingest 触发:`_PRODUCT_SQL` ON CONFLICT 加 slow_hash IS DISTINCT FROM → **仅 approved 翻 pending**;新 ASIN 首次入库打 product_ingested 事件 | `services/product_ingest.py` + 单测 |
| B2 | 纯规则积木:实证类目短路(walmart_items.product_type / 历史认定)→ Phase0 四件套(黑名单中心 catalog 四表等值——原 audit.phase0 三表+seed yaml 方案被三次批复取代 / 8 类目 / ®™ 正则 / 品牌等值)→ L2 硬规则(R0/R1/R2/R3a,软证据仅收集留给 L3)→ reason_mapper(37 政策)。全部纯函数 | `services/audit_rules.py` |
| B3 | 落库积木:写 audit_runs/audit_hits、写 products.audit_* 五列(approved/rejected/pending + reason + walmart_pt + audited_at + audit_version)、审核事件打点 | 随 audit_rules 或独立 `services/audit_store.py` |
| B4 | 主工作流:领 `audit_status IS NULL OR 'pending'` → B2→B3;params:asins= / limit= / force_rerun= / mode=backfill(补刷=只审无结论,批复 #5);**DANGEROUS=True**,dry-run=只打统计+落 audit_runs,不写五列不投影;run 摘要带 pending 计数与最老龄期 | `workflows/product_audit.py` |
| B5 | ~~phase0 三表镜像 + blacklist_brands 并轨对账~~ **已被三次批复取代**(见第九节):risk_sync 改镜像黑名单中心卖家/类目两张新表(TRUNCATE 全量重灌+双护栏,独立事务);四闸直读 catalog;audit 四表退役;历史 ASIN 经 asin_blacklist_import 导入 | `workflows/risk_sync.py`、`workflows/asin_blacklist_import.py` |
| B6 | 测试:audit_rules 纯函数单测 + violation_groundtruth 黄金集回归夹具(离线不打网) | `tests/test_audit_rules*.py` |
| B7 | audit_version 定稿实现:规则集常量版本号(registry 登记,规则/seed 变更时手动递增);force_rerun 支持按版本批量重审 | registry + audit_rules |

**验收**:双跑校准——同批 ASIN 新规则结论 vs 旧库 audit_runs 结论,硬规则层
一致率报告给所有者;dry-run 输出人工抽检。**旧系统不停**,新系统只落库,
E 列仍由人工/旧流程管。
**所有者侧**:无需操作(校准数据同实例直读)。

### 批次 C|LLM 层(逐层验收)

> **实施记录(2026-08-13,PR #31)**:C1-C4 已落码——移植走批次 B 同款
> 纪律(四路 opus 规格 → 三路机械移植 → 双路对抗评审),39 条 OPEN 裁决
> 见 PR 附的移植合同。要点:模块拆为 audit_l1_llm / audit_l3 / audit_l4
> + api/llm_vision(与本表 C3 单文件的偏差,声明在案);L1 扩为
> 实证→报错实证(批复 #10)→哨兵(-100 归还)→映射→候选+rerank 五级,
> 直出级补 seed/出版物硬禁(批次 B 漏迁);LLM 失败语义 L1/L3→pending、
> L4→pass+告警计数(待所有者追认);strong 升级链不迁(生产两链主节点
> 同模型,能力事实已死);评审两 P0(L4 取图源按契约改 products.slow 主源;
> rerank 出口 pt_meta 闸)已修。C5 跳过(可选项,验收带成本数据再议)。
>
> **所有者追认(2026-08-14,批次 C 收口)**:①**strong 升级链不迁**——旧仓
> 给高风险产品换更强模型,但生产 yaml 里两条链主节点早已是同一个模型
> (claude-sonnet-5),能力事实已死 = 纯历史重复;②**L4 视觉默认关闭,
> 故障维持 pass**——L4 只对已全链通过的产品做增量视觉拦截,视觉端点故障
> 不该把 pass 翻成待定(L1/L3 失败仍一律 →pending 绝不放行,10.2 不变)。

| # | 任务 | 文件 |
|---|---|---|
| C1 | chat_json 加 purpose 参数,按 registry 映射选模型(单链,失败重试尽→pending,绝不默认放行) | `api/llm.py`、`services/llm_cache.py`(兼容确认) |
| C2 | 豆包视觉客户端(ARK env 三件:key/base_url/model) | `api/llm_vision.py` |
| C3 | L1 三级判定(实证→映射表→SQL 候选+rerank,purpose=audit_l1)、L3 语义(37 政策 prompt,静态段吃 DeepSeek 前缀缓存;purpose=audit_l3)、L4 视觉(默认关,参数 l4=on,仅 pass 产品,≤5 图) | `services/audit_llm.py` |
| C4 | product_audit 接入三层;L4 结果落 audit_runs.l4 明细(问题图索引供上架选图,对齐旧 get_problem_images 能力) | `workflows/product_audit.py` |
| C5 | (可选,所有者到时定)llm_usage 简版成本记录 | ops 表 |

**验收**:抽样 vs 旧系统 L1/L3 结论一致率;每千产品成本实测;llm_cache
命中率。**所有者侧**:配 env——ARK_API_KEY(豆包);可选 DEEPSEEK_MODEL_AUDIT_*。
llm_cache 清理器按原拍板"上量后再议",本批验收时带数据重议。

### 批次 E|问题商品链四段分离(批复 #8)

| # | 任务 | 文件 |
|---|---|---|
| E1 ✅ | 建议表 `ops.dispositions`(store/sku/asin、source(scan/audit/tro 预留)、建议动作、依据、状态 suggested→executing→confirmed/ineffective、时间戳);DDL+db_init | `refdata/schema.sql` |
| E2 ✅ | 扫描件:从 problem_product_cleanup 抽出"查库归类"段 → 写产品事件 + 落建议行;审核 reject 的在架产品(product_audit 发现 audit_status=rejected 且 walmart_items 在线)同样落建议行——**只建议,不动状态不发 feed** | `workflows/problem_scan.py`(新)、`services/problem_products.py`(既有归类规则不动) |
| E3 ✅ | 执行件:problem_product_cleanup 改为消费 dispositions(suggested→executing),全部既有防重/观测确认纪律原样保留;生效确认(feed 落定 + 下轮 catalog_sync 观测)后置 confirmed 并记事件,未生效置 ineffective 待下轮 | `workflows/problem_product_cleanup.py` |
| E4 ✅ | 调度顺序更新:catalog_sync → problem_scan → cleanup(execute) | docs/plan.md 调度约束 |

**验收**:分离后 dry-run 输出与分离前一致(重排不改行为);建议表状态机
走一轮真实生效追踪。⚠ 本批动的是危险工作流,切换纪律:上新调度前停旧
cleanup 调度,不并跑。

**代码完成 2026-08-14**(923 测试全绿),落地时的三处实现决定:
1. **决策逐字搬家,断言一个字没改**。`plan()` 从 cleanup 整体搬进 problem_scan,
   钉它的 `test_plan_routing_and_dedup` 一并搬到 `tests/test_problem_scan.py`
   ——"重排不改行为"这条验收标准,那个用例就是锁。
2. **生效判定不重写**。`services/dispositions.settle()` 读的是 catalog_sync 经
   `product_events.verify_deletions` 落的 delete_verified / delete_not_effective
   ——"不信回执信观测"那套(含 48h 宽限、RETIRED/缺席算 gone)已经在跑,
   再写一份只会产生两份会漂移的真相。反补的生效信号不同(没有对应核验事件),
   直接看 walmart_items 现状,但**必须等 last_seen_at > executed_at**,
   否则拿提交前的旧快照判,永远判成"没生效"。
3. **反补计数的 source 必须同时认新旧两个值**。历史事件的 source 是
   `problem_product_cleanup`;漏掉它会让 30 天窗口内的历史计数归零,
   "反补满 2 次转删"重新从 0 数起 ⇒ 商品被无限反补。
   执行件写事件时也刻意保持旧 source 不变。

**生产验收待办**:① 先 `problem_scan -p preview=1` 与拆分前的 cleanup dry-run
对拍(数应当一致);② `problem_scan` 落一轮建议行;③ `problem_product_cleanup`
dry-run 核对;④ `--execute` 跑一轮;⑤ 次日 catalog_sync 之后再跑一次 cleanup,
看开头的"上一轮落定"行是否给出 confirmed/ineffective。

### 批次 D|总切换日(一天内按序执行)

1. **补刷收官**:`product_audit -p mode=backfill` 跑完存量无结论产品(批复 #5);
2. **双跑对比收官**:库内一致率报告,所有者放行;
3. **停旧**:黑名单双调度(07:05 cron + 7:00 launchd plist)、stop_workers、
   阿里云队列 server 停(机器去留随所有者正式切换一并处理,批复 #7);
3b. **⚠ 增量重搬(2026-08-14 补,原计划漏了这一步)**:停旧**之后**再跑一次
   `audit_import`,把旧系统从上次搬迁到停写这段时间新产生的行补进来。
   为什么必须有:2026-08-13 那次 `--execute` 按设计是**停写前的快照**
   (见 `workflows/audit_import.py:23-27` 前置纪律 + 本文 §"全程旧审核系统
   照常运行直到批次 D 第 3 步"),旧系统在那之后照常运行、照常写 audit_runs。
   **漏了这一步的后果是静默的**:历史 reject 短路会漏拦最后一段时间的产品,
   而两侧都不报错——只会表现为"某些早就拒过的商品又被放上架了"。
   `audit_import` 专门实现了 `-p table=X` 与 `-p replace=yes` 就是为重跑存在的。
4. **上架链改查库**(批复 #4 二次批复):list_new 领任务判定
   E=pass → `audit_status='approved'`(walmart_pt 同步从库取,替代 LLM 现猜 PT
   的入口逻辑归 C 已就位);listing_sheet 口径注释改(D/E/F/G=审核链投影,仅展示);
   match_listing 的 D 列判定随 L1 跟卖试点时同步定;
5. **开投影**:product_audit 尾部回填上架表 D/E/F/G(读表 ASIN→查库→批量回写);
6. **挂新调度**:product_ingest 5 分钟级、product_audit 每小时(批复 #6),
   顺序约束 catalog_sync → product_refresh → product_ingest → product_audit
   → problem_scan → problem_product_cleanup --execute → list_new/maintenance;
   ⚠ **problem_scan 与 cleanup 之间的先后是硬约束**(批次 E 拆分后):
   scan 产建议、cleanup 消费建议,只跑 cleanup = 消费上一轮的陈旧建议或空转。
7. **文档收官**:plan.md 总览增行、legacy_schedules.md 停旧清单增审核条目、
   backlog 回销、旧仓归档标记。
8. **退役快照四表清理(切换日**之后**才谈,2026-08-14 盘点登记)**:
   `audit.phase0_blacklist_sellers` / `phase0_blacklist_asins` /
   `phase0_blacklist_amazon_cats` / `blacklist_brands` —— 读侧全部为零,
   消费方已全改道 `catalog.*` 黑名单中心。**前三张所有者已定「留档不删」**,
   只有 `blacklist_brands` 无裁决、是唯一待定项。
   🚫 **在第 3b 步之前绝对不能删**:它们的唯一写入方 `workflows/audit_import.py`
   的 `TABLES` 元组切换日还要再跑一次,而该工作流对「清单里的表在目标库不存在」
   判致命 —— **先删表 = 切换日搬迁直接报错**。
   删除三前置(全满足才谈):① 第 3b 步增量重搬已完成;
   ② `audit_import.py` 的 `TABLES` 元组同步删对应行;
   ③ `refdata/schema.sql` 与 `docs/db_schema.md` 同步更新。

**所有者侧**:切换日到场拍板放行(第 2 步)、执行停旧(第 3 步,生产 Mac 操作)。

### 跨批次纪律

- 每批一个 PR,所有者验收合并后才进下一批;
- 全程旧审核系统照常运行直到批次 D 第 3 步;
- 任何批次发现与本计划冲突的实底,先改计划文档再动代码。
