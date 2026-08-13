# 审核系统迁入 + 采集合库裁决(2026-08-13 调研定稿)

> 所有者提问:是否把采集服务和审核系统一起迁进本仓,共用一个数据库"搞定一切"?
> 前端要不要像 listing 那样重构统一?
> 本文是七路并行代理对三个仓(walmart-audit-system / amazon-scraper-v4 / 本仓)
> 全量取证后的裁决与方案。全部结论带证据(文件:行号);行号对应
> walmart-audit-system@a565d95、amazon-scraper-v4@e8036c4、本仓@98aa8ad。

## 〇、结论先行(两个独立决策,不捆绑)

| 决策 | 裁决 | 一句话理由 |
|---|---|---|
| **审核系统迁入** | ✅ **迁,分四批** | 它的业务 PG 与中心库**同一台 Mac 同一 PG 实例**(只是隔壁 database,跨库不能 JOIN);它的队列/worker/前端是为"人工上传文件批量预审"旧形态服务的,改成"入库自动触发"后整套退役,真正要迁的只有判定引擎(大半纯规则)+ 数据资产 |
| **采集服务合库** | ❌ **不合;把同步做密** | 契约 v1 明文禁止直写(故障互不传染);旧系统 erpAPI 39 个文件依赖采集库,过渡期数月不可拆;schema 风格不相容;"不是实时"的真因是 product_ingest 没挂分钟级调度,不是架构缺陷 |
| **统一前端** | ❌ **不建;飞书为人机界面** | 审核前端(纯任务提交+进度看板,无人工复审功能)随旧形态退役;采集前端是爬虫运维控制台,留在采集侧;将来要网页控制台,按 CLAUDE.md"入口唯一"做薄壳调 cli.py,三期再议 |

## 一、审核系统真实形态(纠正三处普查偏差)

旧普查(`legacy_survey.md`)只记到"新审核系统 PG 三表 ~25k 行 + 10 worker",实际全貌:

**三体架构**:
- **任务调度器**:阿里云 47.108.92.213,FastAPI + **SQLite 队列**(不是 PG;`server/schema.sql:1-16`、`server/main.py:9-10` "不存业务数据"),nginx `/audit` 反代,7 天保留期即弃。
- **业务真身**:**开发机 Mac 本地 PG** `walmart_audit`(`config/settings.py:179` 默认 `localhost:5432/walmart_audit`)——**与中心库 walmart_data 同一 PG 实例**(本仓 `registry/db.py` 本机连接)。这是本裁决的决定性事实:跨 database 无法 JOIN,审核自建的 `products` 表与 `catalog.products` 重复存同一批 ASIN 档案,靠 scraper HTTP 再拉一遍,双份慢变字段沿不同链路更新。
- **worker**:Mac 上 10 进程 × 每进程 50 协程 = 500 ASIN 并发(`worker/poll_loop.py:5-7`、`scripts/start_workers.sh`),轮询阿里云领 CSV 切片。

**普查偏差三处**:①审核 server 部署脚本名叫 `deploy_to_dmit.sh` 但目标是阿里云,不在 DMIT VPS;②它对接的采集是 **scraper v3**(阿里云反代 `47.108.92.213/amazon`,`integrations/dmit_client.py:1`),不是 v4;③"独立部署"名不副实——worker 与业务库都在 Mac 本机,只有队列 server 在云上。

**判定流水线**(`pipelines/orchestrator.py:309-411`,21 文件 6767 行):

```
历史短路 → Phase0 → L1 类目 → L2 规则 → L3 LLM → L4 视觉 → reason_mapper → 落库
(45天TTL) (四件套)  (PT判定)  (硬命中拒) (语义4维) (图7类)   (37政策映射)
```

- **纯规则可直迁**(零 LLM):历史短路(纯 SQL:真 reject 永久短路、真 pass 45 天 TTL)、Phase0 四件套(飞书黑名单三表等值/8 大类目禁售/®™ 符号正则/36k+ 品牌黑名单等值)、L2 全部(R0 中国卖家 8 大类硬禁、R1 双白名单闸、R2 禁售大类、R3a 硬认证;打分体系名存实亡——软规则全 0 分,实质"任一硬命中即拒",`l2_rules.py:1345-1389`)、三个 yaml 小分类器、reason_mapper。
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
1. **erpAPI 39 个文件**引用采集服务,契约明令"新旧并存数月,不改现有接口"——采集库在过渡期内根本不可拆,合库只能是"加一路直写"= 双写,恰是 CLAUDE.md 禁止的形态。
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

## 五、批次分解

- **批次 A|数据地基**(纯搬运,零行为变更):audit schema 建表(三张无 DDL 表从生产
  pg_dump 反推)、A 类数据迁入、seed yaml 进 refdata/ 并改 registry 取路径、
  blacklist_brands 与 catalog.brand_blacklist 并轨方案、product_events 登记
  入库/审核事件码、ingest 触发一行 SQL(slow_hash 变更置 pending)。
- **批次 B|纯规则 MVP**(零 LLM,即可拦截全部明确违禁):services/audit_rules.py
  (历史短路+Phase0 四件套+L2 硬规则+reason_mapper)+ workflows/product_audit.py
  (run(params),支持 asins=/force_rerun=/limit=)+ risk_sync 扩展 phase0 三表镜像
  + E/D 列投影。首跑 dry-run,用 walmart_error_records(97k)与 violation_groundtruth
  做双跑校准后才 --execute。
- **批次 C|LLM 层**(逐层带决策项):L1 PT 判定(walmart_pt 直接喂 list_new,
  替代现在 LLM 每次现猜 PT)→ L3 语义 → L4 视觉。每层独立验收,供应商见第七节决策项。
- **批次 D|切换退役**:双跑对比(force_rerun 抽样比对新旧结论)→ 停旧
  (07:05 sync-blacklist-brands-daily 归新仓 risk_sync;stop_workers;阿里云下线)
  → 旧仓归档。遵循"先停旧调度 → 搬状态 → 起新调度"铁律。

## 六、顺手取证的安全/质量发现(独立于迁移决策,建议尽快处理)

1. **采集侧鉴权近乎裸奔**:worker 的 X-Worker-Api-Key **服务端无校验代码**(全仓 grep
   零命中);ADMIN_TOKEN 可选且只保 9 个破坏性端点,`DELETE /api/database` 可清全库;
   EXPORT_TOKEN 可选。**上 VPS(公网)前必须配齐三 token**——与既有待办
   SCRAPER_EXPORT_TOKEN 合并执行。
2. **L3 故障不对称是坑,迁移时不照搬**:旧代码"LLM 全链故障→pending 待人工"是对的,
   但"坏 JSON/其余异常→pass"(`l3_llm.py:757-804`)意味着 LLM 抽风即放行。
   新实现改为:解析失败重试,重试尽→pending,绝不默认放行。
3. **不迁的死代码**:l2 的商标符号规则(与 phase0_trademark 重复,evaluate 已不调用);
   reason_mapper docstring 与代码矛盾(以代码为准:硬规则优先于 L3)。
4. **文档滞后陷阱**:采集侧 local_macos_setup.md 还说默认 SQLite,实际 dbfactory
   默认已是 postgres——按文档操作会误判生产后端。

## 七、待所有者拍板的决策项

1. **LLM 供应商面**(批次 C 前必须定):旧审核主链是第三方 Claude 网关
   (kiro-claude@zz.211b.site)+ 备链 DeepSeek + 豆包视觉 + DashScope embedding;
   本仓现只有 DeepSeek 直连。迁入时用哪套?(涉及成本、稳定性与合规,纯业务决策;
   多供应商路由器 llm_router 1029 行是否值得随迁,取决于此答案。)
2. **L4 视觉层去留**:每产品 ≤5 图的视觉审核成本最高,旧系统仅 pass 产品才跑。
   增量形态下要不要保留?
3. **uspto 库(1400 万行)与 trademark-data/tro-scraper-matrix 外部仓边界**:
   建议维持 D 类暂留(R5 商标反查继续跨库连它),外部灌库链不进本仓——与
   backlog 第十节"边界暂放"一致,此处只是把"暂放"具体化为 D 类。
4. **E/F/G 列权责**:从"人工域"改为"审核服务域"(listing_sheet.py:4-11 口径注释
   要改)。人工改判走什么通道(直接改 E 列?还是加人工 override 列)?
5. **存量首刷范围**:catalog.products 现有存量是否全量补审,还是只审新增量?
   (历史短路表迁入后,旧系统已审过的 ASIN 会自动短路,增量成本可控。)
6. **调度密度**:product_ingest 提到 5 分钟级、product_audit 挂每小时,是否同意。
7. **阿里云 47.108.92.213 去留**(队列 server 退役后该机上还剩 scraper v3 反代,
   erpAPI 过渡期仍在用,建议随停旧切换一并退役)。

## 八、本文与既有文档的关系

- `docs/backlog.md:114` "TRO/商标/新审核系统三条跨仓黑名单链边界**暂放**"
  → 本文第七节第 3 项给出具体化建议,**拍板前 backlog 状态不变**。
- `docs/scraper_migration_brief.md:66-68` 二期审核对接("审核服务将读 catalog.products
  待审行、写回结论")→ 本文即其落地方案;brief 第四节第 3 条(不直写中心库)继续有效。
- `docs/plan.md` 迁移进度总览 → 待所有者批准本方案后,增补 product_audit 工作流行。
