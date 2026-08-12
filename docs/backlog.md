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
| 🟡 | **BIZ-CN 独立维度:收集侧已单列**(两张黑名单表 biz_cn 布尔列,`blacklist.is_biz_cn` 独立判定)。余:PT 5 维度预警里的 BIZ-CN 聚合(随预警批次) | `services/blacklist.py:45` |
| 🟡 | `risk_sync` 无调度、生产验证未做(env 模板已补齐 2026-08-11) | `docs/listing_plan.md:79` |
| ✅ | ~~match_listing 不过风控闸与防呆~~(2026-08-12 接通三道闸:SPEC 交叉字段过 risk_gate(PT/品牌)+ asin_blacklist(交叉 ASIN)+ product_risk 防呆(交叉 ASIN 删除史 / 同 GTIN 旧跟卖 offer 删除史,后者经 listing_sources 把 GTIN→历史 sku→病历接回);交叉不出的字段跳过该道闸;命中写 F 终态,清 F 重排队) | `workflows/match_listing.py:_gate_reason` |
| ⬜ | UPC `gs1_restricted_prefix` **6,665 条**历史黑名单未导入(upc_generator 定稿不迁,但这批黑名单号的处置没有交代) | `docs/legacy_survey.md:998,1026,2251` |
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
- 🔴 存量 feedId 是否做一次历史回查(`legacy_survey.md:1476`)
- 🔴 RETIRE_ITEM 与常规 DELETE 的职责边界(旧系统只在合规路径调 RETIRE,刻意还是遗漏?`legacy_survey.md:1475`)

## 三、产品事件账本(catalog.product_events)

- ✅ ~~事件码清单不一致 / 无代码常量~~(2026-08-11 已修:常量 + EVENTS 成为唯一出处,record_many 对未登记码抛错,schema.sql/db_schema.md 清单降级为指路;发出点与读侧 SQL 全部改绑常量)
- ⬜ **入库/审核事件未接**:product_ingest 不写账本;`catalog.products` 的 audit_status/audit_reason/audited_at/audit_version/walmart_pt 五列零触及(等二期审核服务,`docs/scraper_migration_brief.md:66-68`;接缝已在 `services/product_events.py` docstring 登记——届时补常量,休眠码不预进 EVENTS)
- ✅ ~~只写不读~~(2026-08-11:`status_changes` / `feed_failures` 两个读侧视图平铺 jsonb,AI/人工直接 SELECT;`list/match_submitted` 计入 risk 视图 submit_times;`retire_feed_success` 属回执流水,读侧走 feed_failures 之外的 ops.feed_items,不另建)
- ✅ ~~product_risk 只按 sku 聚合~~(2026-08-11:**身份键修成 coalesce(asin, sku)**——原按订货号原文聚合,三段式 sku 名下的删除史拦不住同 ASIN 换号重上,而 list_new 拿 ASIN 查,防呆实际是漏的;新增 `product_risk_store` 店铺维度;list_new 防呆理由带证据列(计数+最近移除时间),listed_times/last_removed_at 有了读者。**拦截条件口径**(所有者拍板 2026-08-12):"不明原因消失"史(item_missing 且从未提交删/停 = 疑似平台下架)**只提示不拦截**——视图加 unexplained_missing 标志,list_new 放行但在摘要报警,积累观察后再定要不要升级成拦截;停用史不拦,等 RETIRE 职责边界(第二节 🔴))
- ✅ ~~旧库历史导入~~(2026-08-11 完成,见第二节:485,345 行 → 239,253 条时间线事件,occurred_at=旧 run_ts)
- ✅ ~~sku≠asin~~(2026-08-11:product_events 加 asin 列,record_many 自动清洗
  + sku_normalize 存量补填;残余 numeric 1,739 个 item id 键倒查零命中,
  原文兜底,等 walmart_items.item_id 覆盖扩大后重洗)

> ✅ 已核实非缺口(防止再被误报):`maintenance_submitted` **有生产者**——
> `problem_product_cleanup.py:293` 反补路径在发,"反补满 2 次转删"计数是通的。
> 2026-08-10 一次代码扫描曾误判为断链(grep 漏了作为参数传入的字面量)。

## 四、死表 / 死列 / 僵尸登记

- ⬜ **`listing` schema 整体架空**:`listing.tasks`、`listing.upc_pool` 全仓零引用(已核实);后者与在用的 `catalog.upc_pool` 状态机定义冲突(available/claimed/used vs ''/claimed/used/conflict/bad_prefix)。**历史 10 万 UPC 迁移动工前必须删一张**(`refdata/schema.sql:258-280`,`docs/legacy_reference.md:74` 的迁移落点还指向死表)
- ⬜ `orders.orders` 空表待确认后删(schema.sql:462 "新代码禁止写入");`orders.order_center` 视图无读者(push 走三张明细表)
- ⬜ `catalog.products` 十列死列:audit_* 五列 + assigned_upc/listing_attrs/last_feed_id/store/owner——职责被飞书上架表、catalog.upc_pool、catalog.llm_cache 三处各自顶掉
- ⬜ `LISTING_SHEET` R~U 四列(L3 暂缓遗留,`registry/resources.py:372-373`);listing_sheet 实际靠硬编码 range 坐标写列,columns 元组的"唯一权威"被绕过
- ⬜ 只写不读的列:`ops.perf_problem_orders` 14 个业务列(唯一读方只 count)、`ops.scrape_failures` 的 status/error_detail/retry_count、`catalog.snapshots.completeness_ok`、`catalog.llm_cache.hit_count/last_hit_at`(说好的低频清理器未写)
- ⬜ `ops.cleanup_seen_categories`(20.7 万对):原定消费方是 Step 3/4/5 报表的累计数,报表不迁(2026-08-11 拍板)后**暂无消费方**——数据保留,AI 读库出数时可用,不删
- ⚠ `ops.runs` 无程序读方——**设计如此**(人工/看板存档),不算缺口,记录在此防误报

## 五、决策未决汇总(等所有者拍板,阻塞下游)

3. `AMZ_IN_STOCK_QTY` 终值(`docs/listing_plan.md:162` / `docs/plan.md:95` 两处挂着)
5. **listing 的 channel 口径分叉**:采集侧已有 `raw->>'is_fba'`(maintenance 在用),listing 定价仍"一律 FBM 区间"(`docs/listing_plan.md:162`)——该跟进
6. `outcome=not_found` 是否比照 error_type 终局直接建议拒绝(order_audit,2026-08-10 提出)
7. TRO/商标/新审核系统三条跨仓黑名单链的边界
8. 退款能力(POST /returns/{id}/refund)纳入还是显式排除(`legacy_survey.md:608`)
9. **密钥轮换**:旧仓库 git 历史至今明文(48 ClientSecret + socks5 密码),无人认领(`legacy_survey.md:122,2198`;`legacy_reference.md:84-85`)
10. KPI 32 列表头里 8 个阈值要不要做成真告警(`legacy_survey.md:751`);日报单人 open_id 要不要改群发(`:754`)
11. 飞书「AK 图片单元格」历史截图迁移还是清空重采(`legacy_survey.md:889`)
12. `walmart_items.missing_since` 连续缺席多久后清理(`docs/db_schema.md:130`,表单调增长)

## 六、配置与安全(便宜,但都在裸奔)

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

- 生产验收:product_refresh(维护链前置,一次没跑过)、maintenance 清零、RETIRE_ITEM 实测(**registry :59-62 明写 spec 1.0 需先实测端点还活着**)、risk_sync、upc_sync、match_listing(--execute 前置对拍)、kpi_history_import apply、KPI 看板建表首刷、returns_sync/catalog_sync 全店与对拍、daily_report 双算对拍收口
- **涨跌幅闸**(maintenance.py:47,所有者 2026-08-07"暂不需要"):改价安全阀,上量前建议重议
- 挂调度:全部工作流一条没挂;顺序硬约束 `catalog_sync → product_refresh → product_ingest → maintenance`;feed_poll 高频
- 停旧 cron 五条:15:00 retire / 0·6·12·18 cleanup / 12:00 maintenance(先收干净在途 feed)/ order_audit 双重调度 / walmart-kpi-daily(停之前严禁开影刀)
- 采集侧一周连续验收(scraper_migration_brief.md:245)未开始;两侧契约副本的定期对账机制未建(:113-116)
- 连续无货 15 天删除条:2026-08-23 前恒空(采集 08-08 才接线),届时复查(maintenance.py:24)
- Phase 1:令牌桶(plan.md:73,**并发调度前必须补**——旧 RETIRE_ITEM 事故根因)、async 订单拉取、feeds errorReport 随 listing
- 历史数据迁移总批次(plan.md:134):上架表 26 列、UPC 池 12 万行、pending_feeds 收干净、retry_state 永久淘汰名单(丢了会重拉几万个已死 ASIN)、maintenance.db(落点需重定——legacy_reference.md:73 写 listing schema,实际流水在 ops.feed_*)、walmart_settlement.db(落点名已过时)、settlement_snapshots 历史是否全迁待确认

## 八、文档失真待回写(除本次已修正的)

- `docs/legacy_reference.md:67-78` 状态迁移清单漏 3 项(黑名单 ASIN 表 / risk_gate_cache.json / 影刀 latest.json,legacy_survey.md:2216 点名至今未补)
- `docs/db_schema.md:439` "列清单由执行 AI 补全并回写本文档"未执行(store_kpi_daily 仍是 `...`)
- `docs/feishu_tables.md` 若干条目已过时(本次修正一批,"错误商品记录:沿用旧表或新建"仍未决)
- `docs/scraper_migration_brief.md:203-206` 仍留着"一个邮编一个批次"收紧版遗留文字,与 :182-193 并存易误读
- `registry/resources.py:271` 注释指向不存在的 `orders.order_audit` 表(本次修正)
- `refdata/schema.sql:331` "四道审核" vs 实际五道(本次修正);`:4` "orders.returns/settlement 留待补全"是陈迹(实表为 return_lines/settlement_lines)
