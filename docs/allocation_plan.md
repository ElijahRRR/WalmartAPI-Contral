# 店铺占用与产品分配子计划(2026-08-07 立案)

> **实施状态:全线暂缓(所有者定稿 2026-08-07)——含 A1 地基。**
> 等产品中心库(catalog.products/snapshots 采集侧)真实建成、审核链接通、
> 能看到实际结构与真实数据后再动工,届时先按真实数据校准本文档再写代码。
> 本文档只做设计定稿与决策留痕,防止讨论结论丢失。
>
> **2026-08-12 更新:约定的"校准"已完成**(§十~十四,四外部仓库摸底 +
> 前置条件核对 + 引擎细化 + 待拍板问题清单)。
> **同日所有者八条全部拍板(§十四)——A0 数据接线批次解除暂缓,动工。**
> A1/A2 仍按 §十三 顺序推进,每批独立验收。

## 一、需求(所有者 2026-08-07 原述要点)

1. 一个品牌只能同时存在于一家店(采集数据带真实品牌;提交沃尔玛仍 Unbranded,两不冲突);
2. 一个产品(SKU/ASIN)只能同时存在于一家店;
3. 一个店最多做两个大类目;
4. 约束在**前期产品进来后的分配阶段**执行;
5. 店铺终止运营 → 释放其占用的品牌与产品;暂停 → 绑定保持;
   救不回来的暂停店 → 手动点名释放;
6. **只要没决定停止运营,品牌和产品就不能出现在其他店——
   即使当前没有上架到这个店也不行**(占用与在线状态无关);
7. 分配本身是复杂决策:好坏产品要筛,店铺间怎么分要参考订单/销量/销售额/
   在线数/容量等数据,需要"模型"。

## 二、核心原则

**占用是决策台账,不从在线快照推导。** 在线产品(catalog.walmart_items)
是观测,天天抖动;归属(占用)是决策,只有"分配"和"释放"两个显式动作能改。
两者解耦,占用才稳定——这与病历(product_events)的"决策/观测"分层同源。

**释放只走显式动作,没有任何代码自动释放占用。** 这是稳定性的根本保证。

## 三、定稿记录(所有者 2026-08-07)

1. ✅ **大类目 = Walmart Category**(与 catalog.risk_product_types 同口径,
   风控闸已入库的那套体系)。
2. ✅ **店铺状态从数据库读**:ACTIVE / SUSPENDED / TERMINATED,由 KPI 链路
   (daily_report)获取并保鲜。**TERMINATED 触发的是"可释放资格",不是自动
   释放**:审计发现"TERMINATED 且仍有 active 占用"→ 告警列清单 → 所有者跑
   `store_release --store X --execute` 落地(KPI 是外部观测,误报若触发自动
   释放,品牌被别店占走后不可撤销)。SUSPENDED 一律不动;救不回的店由所有者
   `--brand X` / `--asin Y` 点名释放。
3. ✅ **存量回填推迟**:依赖采集恢复 + 审核链接通(类目由审核产出)。
   回填前先出冲突审计报告(存量可能已有同品牌/同 ASIN 跨店),处置所有者拍板。
4. ✅ **分配引擎第一版 = 透明打分函数 + 确定性匹配,不用机器学习也不用 LLM**:
   可解释、可调参、可复现;LLM 只继续负责字段映射。学习型升级是远景
   (等分配→销售结果的回路数据在库里攒够,见 A3)。

## 四、占用台账设计(草案,动工时按真实数据校准)

**`catalog.claims`**(品牌占用 + 产品占用,一行一个占用):

| 列 | 说明 |
|---|---|
| kind | `brand` / `product` |
| claim_key | 品牌名(casefold)或 ASIN/GTIN(与 listing_sources.source_key 口径一致) |
| store | 占用店铺 |
| status | `active` / `released` |
| claimed_at / released_at / released_reason | 生命周期与审计(released 行保留不删) |

排他性由 PG 保证,不靠代码自觉:
`UNIQUE (kind, claim_key) WHERE status='active'`(部分唯一索引),
并发领用与 UPC 池同款事务纪律。

**`catalog.store_categories`**(店铺类目档案):(store, walmart_category)
两行封顶,分配事务内查数——店已有 2 个类目且新品类目不在其中,拒绝分配。

## 五、生命周期规则

1. **占用产生于分配,不产生于上架**:产品分给某店的那一刻,事务内同时
   占品牌 + 占产品 + 核类目,占不到就不能分给这家店。之后下架/清零/从未
   真正上架,占用都不动。品牌已属于某店的产品只能"定向"去那家店
   (还得过它的类目与容量),去不了就淘汰。
2. **暂停 = 什么都不做**,占用天然保持。
3. **终止 = 显式释放**:workflow `store_release --store X`(dangerous,
   默认 dry-run 列清单):该店全部 active 占用置 released
   (reason=terminated),类目档案清空,进 product_events 留痕。
4. **手动个别释放**:同 workflow `--brand` / `--asin` 精确释放。
   除以上两条显式路径外无任何自动释放。

## 六、与现有地基的融合点

- **list_new 闸门链**:现"全局 ASIN 去重"读在线快照(walmart_items),
  产品下架即失守——改为**占用闸**(读 claims),快照只做辅助;
  同时用采集真实品牌做品牌占用检查。
- **match_listing**:登记 product 占用(键用 GTIN)。
- **审计对拍(只告警不动手,符合路由铁律)**:只读 workflow 拿在线快照对
  claims:①在线但无占用 ②在线店与占用店不符 ③同品牌两店在线
  ④ TERMINATED 店仍有 active 占用。只出报告。

## 七、分配引擎(三层,一层比一层"软")

### 第 1 层 硬约束闸(一票否决,无参数)

品牌占用、产品占用、店铺 ≤2 类目、风控禁售/品牌黑名单、店铺状态非
ACTIVE 不分、容量已满不分(上架上限 − 已在线 ≤ 0)。

### 第 2 层 产品分(好坏筛选,0~100,低于淘汰线不分)

输入来自采集数据:价格带命中(定价区间能否落进)、库存深度、配送时效、
评分/评论数、类目饱和度等。**字段契约等采集侧定型**;权重放配置,随时可调。

### 第 3 层 店铺-产品匹配(贪心,带占用事务)

产品按产品分降序排队(好产品优先挑店);每个产品的候选店 = 过全部硬闸的店,
按**店铺适配分**加权排序;类目接近时靠店铺分分高下,再打平按剩余容量比
均衡,仍打平轮转(好货不堆一店,风险摊开)。每分配一个,事务内落占用,
后续产品看到的是更新后的世界,不超卖容量。

店铺适配分信号与数据来源(大半已在库):

| 信号 | 数据来源 | 状态 |
|---|---|---|
| 该店该类目近 30 天销量/销售额 | orders.order_lines + 审核类目 | 订单已入库 ✓ |
| 店铺整体近期营收趋势 | daily_report KPI | 已入库 ✓ |
| 剩余容量比(1 − 在线数/上限) | walmart_items + 上架限制表 | 都有 ✓ |
| 当日配额余量 | list_new 日配额闸 | 已有 ✓ |
| 店铺健康度(ODR 等) | KPI | 已入库 ✓ |
| 产品质量字段 | 采集服务 | ⏳ 待采集契约 |
| 产品类目 | 审核链 | ⏳ 待审核接通 |

### 决策留给人

分配引擎 dry-run 产出**分配方案表**(产品→店→各信号得分明细),
所有者审完 `--execute` 才落占用、写上架表。跑顺且被信任后再议放开自动。

## 八、分批(动工顺序,当前全部暂缓)

- **A1 地基**:catalog.claims + store_categories + services/claims 积木 +
  list_new 占用闸(替换在线快照去重)+ store_release workflow + 占用审计对拍。
- **A2 分配引擎规则版**:产品打分器 + 店铺适配分 + 贪心匹配器 +
  分配方案报告(依赖采集字段契约 + 审核类目;打分权重初值届时拟草案供砍)。
- **A3 学习型(远景)**:分配→销售结果回路数据攒够后,权重自动校准。

## 九、开工前置条件

1. 产品中心库采集侧(catalog.products/snapshots 经 /api/export/incremental)
   真实运转,能看到实际结构与数据;
2. 审核链接通(类目产出落库);
3. 店铺三态状态字段经 KPI 链路落库;
4. 满足后:先出存量冲突审计报告 → 校准本文档 → A1 动工。

---

# 2026-08-12 校准(执行 AI 全面摸底后追加;§一~九原文不动)

> 摸底范围:本仓库 + 四个外部仓库(amazon-scraper-v3 / amazon-scraper-v4 /
> walmart-audit-system / amazon-walmart-category-mapping),6 路并行调查,
> 关键结论均有 文件:行号 证据(调查报告全文在会话记录,要点收录于此)。
> 触发背景:所有者 2026-08-12 重提本需求,补充了分配算法细节诉求
> (5000 新品 × 12 店怎么分、目标销售额/订单参考、产品好坏筛选)与
> 数据资产清单(v3 存量 100 万、审核库、两年订单 excel、类目映射表)。

## 十、前置条件进度核对(对照 §九)

| 前置 | 2026-08-07 | 2026-08-12 实况 |
|---|---|---|
| ① 采集侧真实运转 | 未建成 | **机制全链建成 + 生产实跑**:product_ingest 2026-08-09 接线验收(88 条→44 ASIN 两层落库、二次拉取游标不动的幂等实证,plan.md:95);order_audit 2026-08-10 生产实跑 127 组合摄入。余欠:挂调度、SCRAPER_EXPORT_TOKEN 配置、一周连续验收(backlog 第六/七节) |
| ② 审核链接通 | 零实现 | 本侧仍零实现(products.audit_* 五列零触及,backlog 第三节),**但落地路径已探明**:审核系统是独立 PG 库,直连读即可(§十一.3)→ 新增 audit_sync 工作流即接通,不被"二期审核服务"卡死 |
| ③ 店铺三态经 KPI 落库 | 未落库 | 列与写入链已建成:ops.store_kpi_daily.store_status ← payment/statement 的 sellerStatus(daily_report.py:59),另有影刀链 sales_status 列;4 处消费方统一"非 ACTIVE 拦、无记录 fail-open"。**未闭环**:代码只做二分判断,SUSPENDED/TERMINATED 字面量零出现;sellerStatus 会不会产出 TERMINATED 无实证——旧系统三态实际出自人工维护的飞书店铺状态表(legacy_survey.md:1548,未迁移)→ **Q1** |

结论:"全线暂缓"的事实基础已大半消解,建议按 §十三 修订批次复议动工。

## 十一、外部数据资产盘点

### 1. 采集器 v3 vs v4:v4 就是 v3 的 PostgreSQL 迁移版

- v4 仓库即「v3 → PostgreSQL + 事件流」迁移产物(v4/.agent/pg_migration_plan.md);
  `/api/export/incremental`(brief §5 契约)的实现在 v4;v3 仓库已把该次迁移整体
  revert 回纯 SQLite,无增量导出。**本侧对接的"采集服务"以 v4 为准。**
- **「v3 存量要不要导进 v4」——不要。** 三条理由均有实证:
  ① v4 迁移计划明文拍板"数据迁移:不需要,切换后重新采集几小时即可";
  ② v4 事件流只由结果写入路径的写钩子发射,**绕过 API 直灌 asin_data 不产生
  事件 ⇒ 增量导出看不见 ⇒ 本侧 catalog 也看不见**,灌了等于白灌;
  ③ v4 主表 asin UNIQUE 覆盖式,旧数据会被新采覆盖,不承担历史候选库职责。
- **v3 存量的正确用途 = 选品候选池**:v3 有 `GET /api/export/all`(csv/xlsx
  流式,百万级不 OOM)可一次性导出 title/brand/类目三列/rating/review_count/
  价格/FBA 等 45+ 字段 → 导入本侧新表 `catalog.candidate_pool`(§十三 A0.4)。
  入选分配批次的 ASIN 在分配前推 v4 重采保鲜(POST /api/batches)+ 走审核,
  旧数据只用于粗筛,不用于定价与上架。规模口径:v3 README 实测 29 万 ASIN
  (tasks 表 113 万行);所有者口径 100 万,以导出实际行数为准。→ **Q2**

### 2. 产品分的评分/评论数数据源已经有,不需要改采集契约

契约 slow/fast 结构化字段没有 rating/review_count,但 v4 的 raw 裁剪名单
`_RAW_DROP`(export_incremental.py:246)不含它们 ⇒ 两值随 raw 原样落进本侧
`catalog.snapshots.raw`。产品打分直接 `raw->>'rating'` / `raw->>'review_count'`
(text 型,读侧解析,解析失败按"没采到",禁止 or 0)。

### 3. 审核系统(walmart-audit-system)对接事实

- 独立 PG 库 `walmart_audit`;审核记录 = `audit_runs`(asin / walmart_product_type
  =PT 小类 / verdict pass·reject·pending / score_final / created_at)+
  `audit_hits`(逐规则命中)。**产品标识是 ASIN**,与本侧 products 主键同口径。
- **大类不落列**:audit_runs 只存 PT;PT→Walmart Category(27 大类)在其字典表
  walmart_pt_meta——与本侧 `catalog.risk_product_types.category` **同源**(都
  镜像自飞书「沃尔玛类目」表)⇒ 本侧按 PT JOIN risk_product_types 即得大类,
  零新依赖,§三.1 的口径定稿原样成立。
- **无 JSON API**(其 api/ 目录只有 pydantic schema,无路由);现行机器对接方式
  就是直连库(其 cli/get_problem_images.py 明写"上架脚本用法")⇒ 本侧新增
  `audit_sync` 工作流直连 walmart_audit 只读拉取(凭证进 .env、registry 登记),
  写 products.audit_status/audit_reason/walmart_pt/audited_at/audit_version
  (五列死列复活,backlog 第三/四节两条缺口同时清账)。
- 取数语义三条(照搬其仓库自身写法):每 ASIN 取最新行 `DISTINCT ON (asin)
  … ORDER BY asin, run_id DESC`;stage_stopped_at='SHORTCUT' 行的 verdict/PT
  复制自历史真实审核,可直接用;pass 有 45 天 TTL 到期重审可能翻案 ⇒
  audit_sync 每轮全量对齐,不做只增量。

### 4. 类目映射表(amazon-walmart-category-mapping)

v5.5 主表 15,770 行(Amazon 叶子→PT,高置信 81.9%),已发布飞书。它是**审核系统
L1 的输入**,分配链不直接依赖(分配用类目一律取审核产出的 PT)。可选增强:导入
`catalog.amazon_to_walmart_pt`(legacy_survey.md:2098 早有此议)供候选池按店铺
大类目预筛(未审核产品只有 Amazon 类目时的粗过滤)。非阻塞。

### 5. 历史后台报错库:已导入完毕,零新工作

2026-08-11 三笔入库:error_items 48.5 万行 → product_events 23.9 万条时间线、
asin_blacklist 5.68 万、品牌黑名单渠道表 2,012。产品分的"黑历史"信号直接读
product_risk 视图与三张黑名单表。

### 6. 订单:库里只有 45 天窗口,两年 excel 需一次性导入

order_sync 每轮全量重拉 45 天窗口 ⇒ order_lines 只有接线以来的数据。店铺适配分
要"该店该大类近 30/90 天销量份额",冷启动需要更长历史 → 新增一次性
`order_history_import`(excel → order_lines,自然键 po+sku 幂等 DO NOTHING)。
类目归属:order_lines.sku ⋈ walmart_items.product_type,缺行走
products.walmart_pt 兜底(经 sku_asin 清洗)。**需所有者给文件 → Q3**

### 7. 两个信号完全没有数据源(必须新建)

- **店铺目标**(目标月销售额/目标月订单):任何表里都没有 → 新建飞书「店铺目标表」
  + 镜像 `ops.store_targets`(store PK / target_gmv / target_orders /
  max_online / 手工终止标记(见 Q1)/ synced_at)。**需所有者给数 → Q4**
- **单店总容量上限**(最多在线多少个):现有飞书限额表「上架限制」列是**日配额**
  不是总上限,剩余容量比的分母缺权威源 → 并入店铺目标表 max_online 列。

> **Q4 拍板后修正(2026-08-12)**:不建新表。所有者已在订单中心多维表格
> 「定价及上下架限制」数据表**新增三列:目标销售额、目标订单、单店最大在线数**
> (销售额与订单均为**日目标**)。落地方式:registry 的该表登记补三个字段常量,
> 运行时直读(与 list_new 日配额同款读法),**不建 PG 镜像表**;
> 手工终止标记列也不需要(Q1 拍板:三态取值域已实证,直接读库)。

## 十二、分配引擎校准(细化 §七;三层框架与"透明打分不用 ML"定稿不变)

### 1. 分配单元 = 品牌组,不是单品

品牌排他 ⇒ 同品牌所有产品必须同店 ⇒ 逐 SKU 分配会把同品牌拆到多店,天然违宪。
预处理按 brand_key(casefold+strip)分组:
- 品牌组整组定店;组过大时可只分组内产品分 top-N,其余不占 product claim——
  品牌已占用,后批这些品自动定向同店,排他不破;
- 品牌 ∈ 噪声词表(unbranded/n·a/unknown/generic/空,与 mp_mapper._BRAND_NOISE
  同源)= 无品牌:**不做品牌占用**,逐 ASIN 为单元,只占 product。

### 2. 两条流水:定向流与自由流

- **定向流**(品牌已被占用):只能去占用店;过该店硬闸(状态/类目/容量/黑名单)
  → 过不了**整组淘汰**(§五.1 原则),淘汰行进方案表并注明被哪道闸拦下,
  所有者可选择先点名释放品牌再重分。
- **自由流**(品牌未占用/无品牌):走打分匹配(下 §4)。

### 3. 店铺配额:目标缺口驱动

每家 ACTIVE 店先算本批期望接货量 quota,匹配循环再消耗:

```
gap_i    = clamp01((target_gmv_daily_i − avg_daily_gmv_30d_i) / target_gmv_daily_i)
                                                                  # 目标缺口(日口径,Q4 拍板目标为日目标)
room_i   = max(0, max_online_i − online_i)                        # 容量余量
health_i = KPI 8 率全达标=1,单项越线按权重扣,低于红线=0(整店出局)
need_i   = w_gap·gap_i + w_room·min(1, room_i/批量) + w_health·health_i
quota_i  = min(room_i, ceil(批量 × need_i / Σ need))
```

数据源全部已在库:avg_daily_gmv_30d ← ops.store_kpi_daily.sales_amount 近 30 天
均值(或 order_lines 聚合);online_i ← walmart_items(missing_since IS NULL);
target_gmv_daily/max_online ← 「定价及上下架限制」表三个新列(Q4 拍板,直读);
health ← store_kpi_daily 8 率。
权重初值 w_gap=0.5 / w_room=0.3 / w_health=0.2,进配置文件,供砍(Q6 拍板:
按草案跑首批 dry-run 对着方案表砍)。

### 4. 店铺-产品匹配(自由流;§七第 3 层贪心的具体化)

品牌组按组内最高产品分降序排队;对每组:
1. 候选店 = 过全部硬闸的 ACTIVE 店。类目闸:组主大类 ∈ 店已有类目;店类目<2
   时"开新大类"也算候选但适配分吃惩罚项,方案表高亮「将为店 X 开辟第 2 大类 Y」;
   一批内一家店最多开 1 个新大类(→ Q5);
2. 店铺适配分 = w1·类目份额(该店该大类近 90 天销量/全店销量)+ w2·gap_i +
   w3·剩余容量比 + w4·health_i − w5·(本批已接量/quota_i)(超配惩罚,
   好货不堆一店);
3. 取分最高店,**事务内**占品牌 + 逐 ASIN 占产品 + 核类目(占不到 = 并发被抢,
   顺延次优店);打平 → 剩余容量比 → 轮转(§七原语义);
4. 每分配一组即时更新该店 quota 消耗,后续组看到更新后的世界。
5. 全店 quota/容量耗尽后剩余产品标「本批未分」留池等下批——与产品分不及格的
   「淘汰」分开计数,方案表两个口径都可见。

### 5. 产品分数据源落位(§七第 2 层的"待采集契约"已解除)

| 信号 | 来源 | 备注 |
|---|---|---|
| 价格带命中 | latest_snapshot price+shipping × services/pricing 区间 | 运费 NULL 不定价=淘汰(既有口径) |
| 库存深度 | stock_count | NULL≠0 铁律 |
| 配送时效 | delivery_days | NULL 不当超时 |
| 评分/评论数 | snapshots.raw->>'rating' / 'review_count' | §十一.2;text 解析失败=没采到 |
| 黑历史 | product_risk 计数列 | 黑名单三表在硬闸,视图计数进减分项 |
| 类目饱和度 | 第一版不做 | A3 再议 |

淘汰线初值 40/100(Q6)。

### 6. "要不要模型"的回答(维持 §三.4,补落地钩子)

学习型模型的前提是标签——"分下去之后卖得怎么样",今天一条都没有。所以 A2 从
第一天起把**每次分配的完整信号快照**落库:`catalog.allocation_runs`(一批一行:
参数/权重版本/池规模)+ `catalog.allocation_items`(一品一行:产品→店→逐信号
得分→去向:assigned/directed/eliminated/unassigned + 原因)。跑 2-3 个月自然攒出
「分配决策 → 30/60/90 天销量」回路,A3 先离线回归校准权重,再谈学习排序。
不落快照,A3 永远没有起点。

### 7. 数字演示(所有者原题:5000 新品 × 12 店)

- 审核过滤:5000 → 设 4,600 pass 进池;
- 定向流:1,200 个品的品牌已被占用 → 其中设 950 过占用店闸,定向落店;
  250 被类目/容量/店状态拦下 → 整组淘汰进报告(附拦截原因);
- 自由流 3,400:设 2,100 个有品牌 → 约 400 个品牌组;1,300 无品牌逐 ASIN;
- 店侧:12 店中 2 家非 ACTIVE 出局;10 家按 §3 算 quota——缺口大健康好的店
  可能分到 600,已接近目标的店只分 150;
- 匹配循环按 §4 消耗 quota;耗尽即止,剩余标「本批未分」留池;
- 产出 dry-run 方案表:产品|品牌组|产品分|去向店|逐信号得分|开新类目标记|
  淘汰/未分原因 → 所有者审完 `--execute` 才落占用、写上架表。

### 8. 结果落地与既有链路的接缝

execute 之后:claims 落占用(§五.1)→ 分配结果写飞书上架表行(店铺+ASIN+PT,
E 列审核结果由 audit_sync 的结论投影)→ list_new 照常领任务;list_new 闸门链的
全局 ASIN 去重按 §六 改为**占用闸优先、在线快照辅助**,品牌占用检查同轮加入。
分配是计划层,不受 list_new 日配额闸影响(配额只约束执行节奏)。

### 9. 释放语义补一条(救不回的 SUSPENDED 店)

§五.3 的 `store_release --store X` 不限定 TERMINATED:所有者判死的 SUSPENDED 店
同样用它整店释放(dangerous + 默认 dry-run,本来就是人工确认流)。TERMINATED
审计告警只是"可释放资格"提示器,释放动作永远是人跑的同一个 workflow。

## 十三、修订批次(动工顺序;§八原文保留作历史)

- **A0 数据接线**(全部只读/幂等,零沃尔玛写操作,2026-08-12 拍板后动工;
  **[x] 1~4 代码就绪 2026-08-12(561 测试全过),待生产验收**):
  1. [x] `audit_sync`:直连 walmart_audit 拉审核结论 → products.audit_* 五列
     (§十一.3)。验收:.env 配 `WALMART_AUDIT_DSN` → `-p limit=100` 冒烟 →
     全量跑 → 抽查五列与缺行数;
  2. [x] 店铺目标三列:RETIRE_LIMITS 登记 target_gmv_daily/target_orders_daily/
     max_online 字段常量(读取积木随 A2 引擎实现,避免只写不读的僵尸);
  3. [x] `order_history_import`:两年订单 excel 一次性入库(Q3 文件表头当日
     已提供,精确匹配实现:合并键=PO+SKU 拼接拆回 PO、销售额USD→
     product_amount、退款原因→refund_comments、统计状态/采购成本/利润
     只进 raw)。验收:预览看店名分布(旧命名"1杨宜凡"式与现凭证表
     对不上时先定改名映射)→ apply;
  4. [x] `candidate_import`:v3 csv 导出 → catalog.candidate_pool(Q2 拍板:
     选 a;所有者另行把全量 ASIN 新采进 v4,候选池只做名单与粗筛,
     保鲜一律走 v4)。验收:v3 按 docstring 的 fields 参数导 csv → 预览 →
     apply → 行数对账;
  5. [ ] 复用既有待办:daily_report 全店化 + 挂调度、product_ingest 挂调度
     (plan.md/backlog 已列;分配依赖这两条链保鲜)。
- **A0.5 存量冲突审计 + 过渡方案**(只读报告,不写 claims;A0.1 后即可跑):
  ① 同 ASIN 跨店在线 ② 同品牌跨店在线(walmart_items ⋈ sku_asin ⋈
  products.brand)③ 每店大类分布(walmart_items.product_type →
  risk_product_types.category)+ 超 2 类目店清单 ④ 处置建议列。
  产出同时是 store_categories 与 claims 的**初始回填清单**(所有者审后灌,Q8)。
  **Q5 拍板追加**:所有者将按报告为每家在营店**确定保留类目与保留产品**,
  不合规存量走**下架过渡**——下架清单分批执行,复用既有 product_clear 通道
  (dangerous/dry-run/防重全套既有纪律),节奏放缓让店铺平稳过渡;
  过渡执行不新造工作流,A0.5 报告输出直接兼容 product_clear 的输入格式。
- **A1 占用台账地基**(§八 A1 原样:claims + store_categories + services/claims
  + list_new 占用闸 + store_release + 审计对拍)。
- **A2 分配引擎**(§十二;依赖 A0 全部到位)。
- **A3 学习型**(远景不变;allocation_runs/items 快照从 A2 第一天就落)。

## 十四、已拍板(2026-08-12 所有者八条答复,当日问当日决)

| # | 问题 | 拍板 |
|---|---|---|
| Q1 | 店铺三态权威源 | **直接读数据库状态字段(ops.store_kpi_daily.store_status,三态取值域所有者已实证)**;"释放某店全部品牌与产品"允许手工操作(store_release --store,含救不回的 SUSPENDED 店)。不引入手工终止登记列 |
| Q2 | v3 存量用途 | **a:导出入 candidate_pool**;所有者另行把全量数据**新采集进 v4**(候选池只承担名单+粗筛,保鲜数据一律来自 v4 重采) |
| Q3 | 两年订单 excel | 文件在生产 Mac:`~/Downloads/采购分配合并总表.xlsx`(路径不进代码,运行时 -p file= 传入;先预览探列再 apply) |
| Q4 | 店铺目标 | 已在订单中心多维表格「定价及上下架限制」数据表**新增三列:目标销售额、目标订单、单店最大在线数;销售额与订单为日目标**。registry 补字段常量直读,不建新表不建镜像 |
| Q5 | 开新大类 | **方案表提议制可接受**(dry-run 高亮、execute 即落)。追加:在营店存量要按数据确定保留类目与产品,不合规存量做**下架过渡**,平稳节奏(→ §十三 A0.5) |
| Q6 | 权重/淘汰线 | 按草案(淘汰线 40、配额权重 0.5/0.3/0.2)跑首批 dry-run,对着方案表砍 |
| Q7 | 品牌归一化 | **第一版 casefold+去噪声词;不要别名表**(定稿,不再以审计结果复议别名表) |
| Q8 | 存量冲突处置 | 接受"审计报告出来后议"——A0.5 报告先行,处置原则见报告再定 |
