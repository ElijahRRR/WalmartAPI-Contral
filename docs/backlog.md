# 未完成工作总账(backlog)

> 2026-08-10 三路深查(文档承诺全量 / 代码孤儿扫描 / 事件账本·清理链·黑名单专题)
> 交叉合并的结果,每项带出处。**维护规则**:做完一项就删一行并回写对应文档的状态;
> 新发现的缺口先进这里再动工。plan.md 管"迁移到哪了",本文件管"欠了什么"。
>
> 状态标记:🔴 决策未决(等所有者拍板) / ⬜ 零代码 / 🟡 就绪未验收 / 📄 文档失真 / ⚙ 配置

## 一、黑名单/风控中心 —— 消费侧有了,收集侧整体为零

现状一句话:**新系统只会「读黑名单」,不会「产黑名单」。**
已有:`catalog.risk_product_types` + `catalog.brand_blacklist`(risk_sync 从飞书镜像)
+ `catalog.product_risk` 防呆视图;唯一实质消费方都是 `list_new`。

| 状态 | 缺口 | 出处/关键事实 |
|---|---|---|
| ✅ | ~~清理→品牌黑名单自产回路~~(2026-08-11 生产验收:渠道独立表 brand_err_hits(不与总清单去重,修掉"总表已有品牌进不了渠道"的建模缺陷)→ beyKyi 整表重写 2,012 行(总表认领 2,011 + 时间线推导 1);缺品牌候选走 brand_scrape 推采集闭环(非标准过滤 + 尝试台账防循环);实时链路 cleanup 尾段双落库:渠道表 + 否决闸) | `services/blacklist.py` / `workflows/brand_scrape.py` |
| ✅ | ~~BRAND_BAN_SHEET 缺 D 列~~(2026-08-11 已补登记;risk_sync 现把 D 列 ASIN 镜像进 src_sku,空不覆盖) | — |
| ✅ | ~~ASIN 黑名单~~(2026-08-11 PG 侧+投影生产验收:按标准 asin 重灌 56,812 行;numeric 1,739 键原文兜底。**2026-08-12 上架拦截消费方接通**:blacklist.load_banned_asins → list_new 闸门链(去重后、防呆前)+ match_listing 三道闸,N/F 列写来源与类别) | `services/blacklist.py` |
| 🟡 | **BIZ-CN 独立维度:收集侧已单列**(两张黑名单表 biz_cn 布尔列,`blacklist.is_biz_cn` 独立判定)。余:PT 5 维度预警里的 BIZ-CN 聚合——**后置**(所有者 2026-08-12:等给出具体数据再考虑处理) | `services/blacklist.py:45` |
| 🟡 | `risk_sync` 无调度、生产验证未做(env 模板已补齐 2026-08-11) | `docs/listing_plan.md:79` |
| ✅ | ~~match_listing 不过风控闸与防呆~~(2026-08-12 接通两道闸:SPEC 交叉字段过 risk_gate(PT/品牌)+ asin_blacklist(交叉 ASIN);交叉不出的字段跳过该道闸;命中写 F 终态,清 F 重排队。同日曾附加"删除史/GTIN 删除史"拦截,**当日按所有者口径拆除**——防呆=黑名单,不看删除史) | `workflows/match_listing.py:_gate_reason` |
| ✅ | ~~UPC `gs1_restricted_prefix` 6,665 条历史黑名单~~(所有者拍板 2026-08-12:**不需要管**,不导入) | — |
| 🔴 | PT 5 维度风险表 / 禁售政策知识库 / TRO·商标黑名单 / 新审核系统三表(~25k 行):跨仓,迁移边界未定 | `docs/legacy_survey.md:2000-2001,1806,1915,1954,1963` |
| ⬜ | "飞书表停用后的接班者"——产品中心黑名单增量脚本,四处文档承诺零代码 | `services/risk_gate.py:9` / `workflows/risk_sync.py:11` / `docs/listing_plan.md:80-82` / `refdata/schema.sql:225-227` |

## 已拍板(2026-08-11,所有者六条)

1. **品牌名来源**:从采集库读 `catalog.products.brand`(前置早已就位)。
2. **「禁止品牌收集」D 列语义**:溯源列(该品牌来自哪个 SKU);**去重按品牌**。
3. **ASIN 黑名单表已建**(收):wiki `UhZJw3EtsiFN9skbG0Ac24Dgn4b` / sheet `mPwUBu`,
   列 = 黑名单ASIN / 来源 / 日期;来源格式 = 「沃尔玛-〈13 类之一〉」
   (过期/禁售/品牌/价格/知产/限类/药品/信息/内容/特殊/审查/系统/其他),
   日期 = 入库日期。与邮编黑名单同一个 wiki 承载。
4. **品牌黑名单表已建**:同 wiki / sheet `beyKyi`,
   列 = 黑名单品牌(后台报错集成) / 来源 / 入库日期 / SKU。
5. **41.7 万行建模方向**:按 ASIN 去重后远小于 41.7 万;同一 ASIN 的多次报错
   保留**时间线**形态(⇒ 事件表,不是 per-run 快照平移);报错要能挂到产品,
   但产品库缺行的 ASIN 怎么办(建 stub 行 vs 只进事件表)**待定**。
   → **导入骨架已就绪**(cleanup_history_import,2026-08-11):时间线折叠进
   product_events(occurred_at=旧 run_ts,擦净重灌幂等)、seen 对进
   ops.cleanup_seen_categories、brand_cache 进 ops.dedupe;预览模式先探测
   旧表列名/类别分布再 apply。缺产品行不阻塞(事件不依赖 products 行)。
7. **ASIN 黑名单入选口径**(2026-08-11):只有永久禁止类进表(沿旧规则
   B/C/E/F/G/K),13 类词表只是来源列的格式约定。
6. **DELETE 防重口径**:旧的自然日防重是"一天跑 4 次"的产物,现按日执行,
   改**滚动 48h**,且**仅限 feed 无终态的**;有终态但商品又被扫到(= 提交成功、
   沃尔玛给了结果、实际没删掉)⇒ **直接再执行,不等 48h**。
8. **两张品牌表角色**(2026-08-11 厘清,曾混过一次):「黑名单品牌总表」
   (jF8dOw,FEISHU_BRAND_*)= 各渠道人工归拢的总清单,飞书→PG(risk_sync);
   「黑名单品牌(后台报错集成)」(beyKyi)= 沃尔玛后台渠道,PG→飞书
   (blacklist_push,源=brand_err_hits),**渠道不与总清单去重**。
9. **sku≠asin 两列口径**(2026-08-11):sku=沃尔玛侧订货号原文,asin=源头
   标准码(services/sku_asin 唯一规则出处,record_many 自动清洗,存量走
   sku_normalize);黑名单身份 = coalesce(asin, sku)。
10. **采集库缺 asin 走推送采集**(2026-08-11):不设邮编不截图;非标准 asin
   过滤 + 尝试台账,防无限循环(workflows/brand_scrape)。

## 二、问题商品清理链 —— 旧 7 个 Step 只迁了中间一段

| 旧 Step | 状态 | 说明 |
|---|---|---|
| Step 0 监管合规删除(飞书 eGjQRX) | ✅ | **不迁**(所有者拍板 2026-08-11:与 product_clear 是同一能力,不再另做——删除/停用登记走「商品停用删除表」一条通道;旧表 eGjQRX 与其 F 列幂等锚点随旧系统退役) |
| Step 1/1.5/1.6/2 识别/反补/停用/删除 | ✅ | `problem_product_cleanup`,2026-08-07 生产验收 |
| Step 3/4/5 报表(错误统计/店铺汇总/每日问题商品) | ✅ | **不迁**(所有者拍板 2026-08-11:以后需要数据让 AI 直接读库,不再维护飞书报表)。"累计语义保不保留"之争随之消解——且事实上口径早已动过:BIZ-CN 已独立成维度,旧口径本就没被逐字沿用 |
| Step 6 品牌采集 | ✅ | 2026-08-11 生产验收:brand_err_hits 渠道表 + beyKyi 2,012 行;缺口补采走 brand_scrape(预览→推采→摄取→入账闭环) |
| Step 7 黑名单同步 | ✅ | 2026-08-11 生产验收:ASIN 表按标准 asin 整表重写 56,812 行 |

旧数据入库:✅ **三笔全部完成**(2026-08-11 生产实跑,cleanup_history_import):
- error_items 485,345 行 → 变迁事件 239,253 条(时间线折叠;擦净重灌数与首跑
  完全一致,折叠确定性有实证)
- seen 207,355 对 → ops.cleanup_seen_categories
- brand_cache 2,609 个 ASIN → ops.dedupe('cleanup:brand_asin');
  **pending_batches 实为 0 条**——「在途批次结果永久丢失」的担忧自动消解
实操沉淀:seen 真实形状 {"seen": [[SKU, 码], ...]} 其实一直记在
legacy_survey.md:1350,写解析器前先 grep 摸底文档;seen/brand 参数传反已有
形状指纹护栏,文件检查已前置到重活之前。

待确认:
- ✅ ~~DELETE 防重口径~~(2026-08-11 拍板:滚动 48h 仅限无终态,有终态又扫到直接重发——见「已拍板」第 6 条;cleanup 完善时落实)
- ✅ ~~存量 feedId 历史回查~~(所有者拍板 2026-08-12:**不做**——feed 要完全有结果后才算完成,三态防重已覆盖,无需回查)
- ✅ ~~RETIRE_ITEM 与 DELETE 职责边界~~(所有者拍板 2026-08-13:旧脚本重复实现,product_clear 已覆盖相关功能,不再纠结)

## 三、产品事件账本(catalog.product_events)

- ✅ ~~事件码清单不一致 / 无代码常量~~(2026-08-11 已修:常量 + EVENTS 成为唯一出处,record_many 对未登记码抛错,schema.sql/db_schema.md 清单降级为指路;发出点与读侧 SQL 全部改绑常量)
- ⬜ **入库/审核事件未接**:product_ingest 不写账本;`catalog.products` 的 audit_status/audit_reason/audited_at/audit_version/walmart_pt 五列零触及(等二期审核服务,`docs/scraper_migration_brief.md:66-68`;接缝已在 `services/product_events.py` docstring 登记——届时补常量,休眠码不预进 EVENTS)
- ✅ ~~只写不读~~(2026-08-11:`status_changes` / `feed_failures` 两个读侧视图平铺 jsonb,AI/人工直接 SELECT;`list/match_submitted` 计入 risk 视图 submit_times;`retire_feed_success` 属回执流水,读侧走 feed_failures 之外的 ops.feed_items,不另建)
- ✅ ~~product_risk 只按 sku 聚合~~(2026-08-11:**身份键修成 coalesce(asin, sku)**——原按订货号原文聚合,三段式 sku 名下的删除史拦不住同 ASIN 换号重上,而 list_new 拿 ASIN 查,防呆实际是漏的;新增 `product_risk_store` 店铺维度;list_new 防呆理由带证据列(计数+最近移除时间),listed_times/last_removed_at 有了读者。**拦截条件口径**(所有者两次拍板 2026-08-12,后者为准):**防呆=黑名单,不看删除史**——拦"出现过侵权/审查等拉黑类别"的(asin_blacklist/brand_blacklist),不拦"因产品问题删过"的(可修复类删除后重上是正常经营)。product_risk 视图降级为纯查询档案;曾短暂上过"有删除史即拦",当日拆除。"不明原因消失"史(item_missing 且从未提交删/停=疑似平台下架)只提示不拦截(unexplained_missing 标志,list_new 摘要报警)。"要不要拦停用史"之争随之消解——停用同样看拉黑类别,不看动作)
- ✅ ~~旧库历史导入~~(2026-08-11 完成,见第二节:485,345 行 → 239,253 条时间线事件,occurred_at=旧 run_ts)
- ✅ ~~sku≠asin~~(2026-08-11:product_events 加 asin 列,record_many 自动清洗
  + sku_normalize 存量补填;残余 numeric 1,739 个 item id 键倒查零命中,
  原文兜底,等 walmart_items.item_id 覆盖扩大后重洗)

> ✅ 已核实非缺口(防止再被误报):`maintenance_submitted` **有生产者**——
> `problem_product_cleanup.py:293` 反补路径在发,"反补满 2 次转删"计数是通的。
> 2026-08-10 一次代码扫描曾误判为断链(grep 漏了作为参数传入的字面量)。

## 四、死表 / 死列 / 僵尸登记

- ✅ ~~`listing` schema 架空双表~~(2026-08-12 workflow 逐一核证零代码引用后清理:`listing.tasks`/`listing.upc_pool` DROP,schema.sql 退役清理节;legacy_reference.md:74 落点同步改正——UPC 池已拍板不迁)
- ✅ ~~orders.orders / order_center 视图~~(2026-08-12 清理:视图零读者直接 DROP;旧表带"仅空表才删"守卫防手滑)
- ✅ ~~catalog.products 十列死列~~(2026-08-12 判定:**assigned_upc/listing_attrs/last_feed_id/store/owner 五列删除**(零读写,职责被 catalog.upc_pool/llm_cache/ops.feed_log/飞书上架表接管);**audit_* 五列保留**=二期审核接缝,三处登记一致,非遗忘死列)
- ⬜ `LISTING_SHEET` R~U 四列(L3 暂缓遗留,`registry/resources.py:372-373`);listing_sheet 实际靠硬编码 range 坐标写列,columns 元组的"唯一权威"被绕过
- 🟡 只写不读的列(2026-08-12 逐列核证,三种命运):`ops.perf_problem_orders` 14 业务列=永久明细档案,是"档案(如 ops.runs)"还是该裁,**待所有者拍板**;`ops.scrape_failures.error_detail` **有读者**(v_scrape_failure_stats 视图,先前记录有误),status/retry_count 零读方但为采集契约镜像,随上一条一并拍;`catalog.snapshots.completeness_ok` **保留**(db_schema 登记的人工排查维度+采集契约字段);`catalog.llm_cache.hit_count/last_hit_at` **保留**(旧库同款缓存曾膨胀 462MB,清理器落地时要靠它,正确动作是补写低频清理器而非删列)
- ⬜ `ops.cleanup_seen_categories`(20.7 万对):原定消费方是 Step 3/4/5 报表的累计数,报表不迁(2026-08-11 拍板)后**暂无消费方**——数据保留,AI 读库出数时可用,不删
- ⚠ `ops.runs` 无程序读方——**设计如此**(人工/看板存档),不算缺口,记录在此防误报

## 五、决策未决汇总(等所有者拍板,阻塞下游)

3. ✅ ~~AMZ_IN_STOCK_QTY 终值~~(所有者拍板 2026-08-12:**定 10**,文档已划待定)
5. ✅ ~~listing 的 channel 口径分叉~~(所有者确认 2026-08-12:现行代码为定稿——读 `raw->>'is_fba'` 分两套区间,采不到不定价不上架;listing_plan 过时行已勘误)
6. ✅ ~~outcome=not_found~~(所有者拍板 2026-08-12:比照 TERMINAL **直接建议拒绝**,不再重采;已落代码 services/order_audit.py + 用例)
7. 🔴 TRO/商标/新审核系统三条跨仓黑名单链的边界(**暂放**,所有者 2026-08-12;已备边界建议:数据快照现收/审核功能二期建/TRO 采集链不进本仓)
8. ✅ ~~退款能力~~(所有者拍板 2026-08-13:**不纳入**,显式排除;蓝图预留位保留不实现)
9. ✅ ~~密钥轮换~~(所有者拍板 2026-08-12:**忽略**,不处理)
10. KPI 8 个阈值真告警 + 日报群发(**暂放**,所有者 2026-08-12;方案已备:daily_report 尾段扫 ops.store_kpi_daily 阈值,webhook 配好即生效)
11. ✅ ~~AK 图片历史截图~~(所有者拍板 2026-08-12:**不迁**,留旧表归档备查;新链路截图已在正常跑)
12. ✅ ~~missing_since 清理~~(所有者拍板 2026-08-12:**永不删**——技术上事件在 product_events 独立账本不受主表影响,但保守留行;db_schema.md 已回写)

## 六、配置与安全(便宜,但都在裸奔)

> **整节后置**(所有者拍板 2026-08-12):生产运维动作(盘旧调度/备份/配 env/挂调度)
> 全部等迁移完成——功能做完、数据库到位、飞书表对接无缺失后再考虑。本节只留账不动工。

| 状态 | 项 | 后果 |
|---|---|---|
| ⬜ | **backup 工作流零代码**(全仓 grep pg_dump 零命中;plan.md:104 "Phase 0 后尽早") | 生产库已装全部状态,无任何备份 |
| ⚙ | **FEISHU_WEBHOOK_URL 未配**(plan.md:57) | 采集类型告警、feed 未知状态告警、邮编未采纳告警……**全部只进日志没人看** |
| ⚙ | READONLY_DB_PASSWORD(plan.md:34)、SCRAPER_EXPORT_TOKEN(api/scraper.py 每轮告警)、FEISHU_LIMITS_*(maintenance 清零硬依赖,feishu_tables.md:55) | 各自阻塞一条链 |
| 🔴 | **旧调度未盘清**:`autolisting.morning` 06:00 若仍 loaded = 每天无人值守提交 MP_ITEM + 烧 UPC(legacy_survey.md:1957 判为最大风险);daily_cleanup 的调度器至今没定位(:1958);14 条 skill 的 cron 注册处未导出(:1956) | **新旧并跑可能正在发生**,需生产 Mac `launchctl list` 导出权威清单 |
| ⬜ | `docs/legacy_schedules/` 归档目录不存在(plan.md:153 回滚预案要求删除旧调度前先归档) | 切换时无回滚依据 |
| ⬜ | 旧仓库 `类目映射/.git-archive` 内嵌 git 仓含 7 个未推送 commit,归档前必须处理(legacy_survey.md:2172);`active/extract_pt_templates.py` 不是归档脚本,plan.md:126 "留在旧仓库归档"措辞会切断 erp-core 链(:2171) | 归档动作有前置 |
| ✅ | 新仓库无 `.claude/settings.json`,旧仓的 bypassPermissions 未被继承(已核实) | 结案 |

## 七、就绪未验收 / 调度 / 切换(概览,细节在 plan.md 各行)

> 调度/切换整体后置(见第六节批注)。验收进行中:**所有者 2026-08-12 起每日实测
> 在线产品拉取(catalog_sync)、几种订单拉取、日报拉取**。

- 生产验收:product_refresh(维护链前置,一次没跑过)、maintenance 清零、RETIRE_ITEM 实测(**registry :59-62 明写 spec 1.0 需先实测端点还活着**)、risk_sync、upc_sync、match_listing(--execute 前置对拍)、kpi_history_import apply、KPI 看板建表首刷、returns_sync/catalog_sync 全店与对拍(**每日实测中**)、daily_report 双算对拍收口(**每日实测中**)
- **涨跌幅闸**(maintenance.py:47,所有者 2026-08-07"暂不需要"):改价安全阀,上量前建议重议
- 挂调度:全部工作流一条没挂;顺序硬约束 `catalog_sync → product_refresh → product_ingest → maintenance`;feed_poll 高频
- 停旧 cron 五条:15:00 retire / 0·6·12·18 cleanup / 12:00 maintenance(先收干净在途 feed)/ order_audit 双重调度 / walmart-kpi-daily(停之前严禁开影刀)
- 采集侧一周连续验收(scraper_migration_brief.md:245)未开始;两侧契约副本的定期对账机制未建(:113-116)
- 连续无货 15 天删除条:2026-08-23 前恒空(采集 08-08 才接线),届时复查(maintenance.py:24)
- Phase 1:✅ ~~令牌桶~~(2026-08-12 完成:稀缺桶落 ops.rate_events 跨进程共享,PG 不可达 fail hard——所有者拍板;详见 plan.md Phase 1)、async 订单拉取、feeds errorReport 随 listing
- ✅ ~~历史数据迁移总批次~~(所有者逐项拍板 2026-08-12,**整批关闭**):
  上架表 26 列**不迁**;UPC 池 12 万行**不迁**(还有用的 UPC 所有者手动写入
  现 catalog.upc_pool);旧 pending_feeds **不处理**(所有者自己在旧系统看);
  retry_state 永久淘汰名单——历史报错已导入 product_events 且黑名单库已建,
  **视为已完成**;maintenance.db 旧维护记录**不迁**;settlement 账期对账明细
  旧系统本就没有(只有总对账单),**无需迁移**

## 八、上架续迁缺口(2026-08-12 旧仓 erpAPI 全量对照,三路并行调研,证据在 listing_plan.md 续迁节)

**P0 — 真实业务缺口(所有者批复 2026-08-12,同日实现)**
- ✅ **K=Unknown 自愈**(批复:接受,判据加 feed 轮询结果与产品事件时间线):`listing_sheet.heal_unknown` 反哺器(feed_poll 挂载)——源① ops.feed_items 按 (店铺,SKU) 反查 MP_ITEM 最新终态:success/审核中→K=Yes+L=feedid+O/P/Q 回填,UPC 标已用;failed→K=No+O=FAILED 进限次重试通道,UPC 回收;SKU_LOCKED→只落 O 移交自愈链;源② catalog.walmart_items 在架→K=Yes(L 保持原样,**不造伪 feedId**,旧 healed: 前缀契约整个消掉)。三层防护按新形态映射:目录读空→源② 本轮停用;**"查无"永不负向写**(K=No 只来自 feed 终态)——旧 80% 熔断与 48h 豁免所防的负向误写路径在新形态下不存在
- ✅ **跟卖库存**(批复:同意):`maintenance_intents.match_inventory_intents`——source_type='match' 在架且库存 0/未知 → 补到 MATCH_INVENTORY_QTY(默认 10)。走 maintenance 唯一库存写路径(不在 feed_poll 推:反哺器"只读沃尔玛"契约不破);stockzero 店排除,解除后自动回补=清零/回补不对称一并修掉。⚠ 手动清零单个跟卖品会被回填,单品停售走停用/删除
- ❌ **闸门前淘汰计次**(批复:**否决**——每次都有新价格/库存数据,这次不在下次就可能在;怕烧 LLM 把便宜过滤放前面即可)。现行顺序已满足:全部闸门与数据过滤在前,LLM/领 UPC 只对配额切片后的幸存行执行
- ✅ **缺数据自动推采集**(批复:接上闭环):`list_new._push_scrape`——数据源缺席 ASIN 推采集批次 `listing_gap_<北京日>`,撞名(BatchExistsError)=当天已推不重推,增量次日随新批次;dry-run 只报数不推;推送失败不阻塞上架
- ✅ **配额切片后置**(批复:配额以成功提交为准,淘汰放切片前):list_new 重排——先过全部闸门+数据过滤+定价,幸存者按店切配额;超额行不写终态,次日配额刷新自动续上
- ✅ **manufacturer 双字段风控**(批复:接受):amz_source 契约顶层加 manufacturer(取 slow 段);risk_gate.check 增第四参,品牌+制造商都查(文案去品牌词 force_amazon_copy 早已两字段都洗,无需改)

**P1 — 回归/契约类(2026-08-12 接线批次三,全部落地)**
- ✅ 跟卖:重量留空默认 1 磅(match_feed 补回旧 DEFAULT_WEIGHT);"预检失败"移出终态、每轮自动重新预检(网络抖动不再永久停摆)
- ✅ 淘汰行回显 C/H/I/J(listing_sheet.write_data_cols:拉到数据的淘汰行写标题与价库,算出定价的连 J 一起;待提交行仍由 write_submit_cols 写全套不重复)
- ✅ attrs.weight 形态可见性:ShippingWeight 兜 1.0 磅的行数进 list_new 摘要(持续大面积出现 = 采集契约 weight 形态对不上,凭摘要触发核实)

**P2 — 能力补齐**
- ⬜ update_listed 五个维护字段集(images/attributes/shipping/origin/dates)→ maintenance_intents 新 provider(顺带摆脱 307MB pt_templates_full.json)
- ⬜ 只读健康视图(旧 scheduler.cmd_health_report:待上架分布/UPC 池四态/在途 feed/错误分布,运营日看四次)→ cli.py health
- ✅ LLM 校验失败 payload 落盘诊断(2026-08-12:必填缺失行落 `<DATA_ROOT>/logs/llm_raw_*.json`,含 missing/notes/两段载荷)
- ✅ 三条实证抢救(2026-08-12 全部落位):日期字段硬闸进 mp_conform(第 5 轮,格式感知比 endDate 单点更广);PROHIBITED 三违禁码进回执分类(O=PROHIBITED 永不重试,heal 同步处理);"UPC 领过永久不再用"口径留档(历史迁移已关闭;该口径在 upc_audit 与未来注入校验中使用)
- ⬜ 变体分组(核心 ~190 行纯函数:full_variant_group_set 并集分组/PT 一致性/inject_variant_fields/标题差异化;**跨店重定向与 LLM remap 建议砍**;先决条件=采集契约顶层暴露 parent_asin/variation_asins/variation_attributes)

**P3 — 可选**:live_spec 在线快照过期校验;跟卖逐行 condition(9 种,现只 New);errorReport CSV 下载

**上架验收与收尾待办(2026-08-12 晚定格;代码迁移已收官,以下全是验收/运维/后置)**
- ✅ ~~L2d 端到端验收~~(2026-08-13:3/3 SUCCESS,六轮错误账 30 错→0 错收官;报告在 listing_plan「重跑验收通过」)
- ✅ 三件生产验证:L2a/L2b 已验(2026-08-12);L1 跟卖试点后置(所有者:暂时用不上,启用前再验)
- ⬜ 调度挂载(验收后):upc_sync/catalog_sync(早)→ maintenance → list_new(每日)、feed_poll(每 30 分钟)、sku_locked_heal(每日)、risk_sync(每日);**顺序硬约束 catalog_sync → maintenance/list_new**
- ⬜ 切换清单执行:停旧 launchd 5 条 + AI skill 链 erp-online-products-track(**两条同停**,新旧并跑=重复领号重复上架);旧在途 pending feed 先收干净
- ⬜ L4:仅剩 upc_audit(全站 UPC 冲突审计,只读)。~~历史数据迁移批次~~已整批关闭(所有者 2026-08-12 逐项拍板,含 retry_state 淘汰名单视为已完成——防重拉已死 ASIN 由黑名单/product_risk 承担)
- ⬜ FEISHU_WEBHOOK_URL 未配置(生产日志反复出现):配上后 cli 成功/失败通知才真发飞书

**切换清单增补(归第六节后置,但必须记)**:旧系统有**第二条调度链**——AI skill 平台 erp-online-products-track(07:30,reconcile→sync_online_products→sync_status_track,写上架表 O/P/Q 与 R~W)。停旧时 launchd 5 条之外必须一起停,否则新旧双写同列
**26→21 列迁移口径**:旧 V/W(真实UPC/UPC一致)左移至新 T/U——**按列名对齐,严禁按位对齐**;真丢语义仅旧 T/U(状态跟踪,已由 catalog.walmart_items+product_events 升级承接)与 AA(变体组,随变体后置)

## 十、旧仓全量普查新发现(2026-08-12 晚;停旧权威清单见 docs/legacy_schedules.md)

- ✅ ~~UPC 造号能力~~(所有者拍板 2026-08-13:**不需要**——号段外购,人工注入池即可)
- ✅ ~~settlement 前端依赖~~(所有者口径 2026-08-13:erp-core 不在迁移范围,不考虑其功能与脚本;settlement_sync 数据面已承接并首跑)
- ⬜ **spec 拆分产物取证**:`<DATA_ROOT>/specs/MP_ITEM/` 的源头 MPSetup_by_pt(458MB)不在 git,权威副本只在生产 Mac——备份策略必须涵盖
- 🟡 **类目映射链**(erp-core 依赖项已按所有者口径 2026-08-13 剔除):余两件——`.git-archive` 内嵌仓 7 个未推送 commit 归档前处理;"映射表产物导入 catalog"(plan.md 承诺)未见执行记录
- 🟡 **lark_io sheets_registry 漏登记** Amazon 选品黑名单 sheet(QNIp…Bb/8280e8)——确认新系统是否需要这张表
- ⚪ 已核实降级:erp-core Celery(所有者 2026-08-05 确认未启用,切换日 ps 复核即可);walmart-kpi-afternoon(参数 bug 从未成功写入,直接停);tools/ 10 个救场脚本与顶层一次性脚本全部可弃

## 九、文档失真待回写

✅ **2026-08-12 全部回写完成**:legacy_reference 状态迁移清单补 3 项且逐行更新拍板结果;db_schema 的 store_kpi_daily 32 列补全;feishu_tables 修 4 处(错误商品记录裁撤、补登 KPI_SHEET 旧 workbook、订单中心六表状态、STORE_CREDENTIALS 示例先前已含 enabled);scraper_migration_brief "127 个批次"遗留文字改为过去时收口;resources.py:271 / schema.sql:331 更早已修。
