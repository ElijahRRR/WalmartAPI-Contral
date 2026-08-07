# listing 迁移子计划(plan #10;2026-08-07 立案)

> 旧系统最大业务域(auto_listing + match_listing),按本文档分阶段迁移。
> 证据基础:docs/legacy_survey.md「auto_listing + match_listing +
> sync_online_products」章(全量摸底);蓝图 §2 端点矩阵。
> 每阶段独立 PR、独立生产验收,验收通过才进下一阶段。

## 总纲定稿(所有者 2026-08-07)

- **前端不迁移**:erp_listing_server / erp_web / erp_worker 不搬。基础脚本
  迁移完毕后可能统一重构前端,所有功能届时放到前端上。旧 server 承担的
  UPC 集中分配、ASIN 去重 cache、anchor 重定向,在新系统由 **PG 事务直接
  替代**(SELECT FOR UPDATE 才是"强一致"的正确实现;旧的文件锁 + 飞书
  read-after-write 补丁全部不要)。
- **采集依赖与 maintenance 同款处理**:上架主链的 Amazon 数据源(旧 DMIT)
  做成可插拔 provider,采集服务改造完成前支持 xlsx 输入模式;接口预留,
  接入时管道零改动。
- **价格/库存同步不在 listing 重做**:旧系统两套并存(auto_listing/
  sync_price_inventory 与 沃尔玛商品维护),新系统统一归 maintenance 的
  price/inventory provider(已预留),listing 不再自带同步链。
- LLM(DeepSeek/Qwen)API key、采集地址等全部进 `<DATA_ROOT>/.env`,
  经 registry 引用;spec 文件入 `<DATA_ROOT>/specs/<版本>/`。

## 阶段划分

### L0 地基(无沃尔玛写操作,可随时动工)

- [ ] PG listing schema:`listing.tasks`(上架任务,映射上架表 26 列语义)、
      `catalog.upc_pool`(UPC/标记三态/领用归属)、`listing.retry_state`
      (失败状态机,阈值淘汰)、`listing.retire_cooldown`(24h 冷却)——
      旧 state/*.json + sqlite 全部 PG 化;llm_cache 换模型即失效,只迁结构
      不迁 462MB 旧数据
- [ ] registry 登记 6 张飞书表:上架表(26+AA 列,**消掉 config.py 与实现
      的列名分歧**)、UPC 池、定价/配额表(倍率是'275%'格式化值,解析积木
      同步移植)、店铺状态表、类目映射「沃尔玛类目」、「禁止品牌收集」
- [ ] api/feeds 收录 MP_ITEM(v5 header 只 3 字段、version 完整时间戳)与
      MP_ITEM_MATCH(v4.2);MP_ITEM 同店打包单 feed(10/hour 硬限);
      速率桶登记(旧系统 RETIRE_ITEM 零限速的教训:未登记默认拒绝已内置)
- [ ] UPC 池**只读导入** PG 并与飞书对拍(权威仍在飞书,切换点在 L3;
      标记字符串'已领|已用|冲突'三态格式是隐式协议,解析积木先建)

### L1 match_listing(跟卖;独立链路,零 DMIT 依赖,最先见效)

- [ ] workflow `match_listing`(dangerous):跟卖清单 xlsx → SPEC 预检
      (upc 12 位/gtin 13-14 位按位数生成候选 + zfill;退化码判无效)→
      MP_ITEM_MATCH feed(offer-only 按 sku REPLACE,重复提交安全)→
      feed_poll 统一轮询 → 结果回写(跟卖结果表)
- [ ] 新 offer 默认 0 库存是正常现象(spec 无库存字段),不当失败

### L2 前置定稿(所有者六点答复 2026-08-07)

1. UPC 池表 6 列定稿(UPC/放入日期|状态/店铺/SKU/上架日期);
2. 领用状态机照搬旧实证语义,按新架构重写(领→用;回收仅三类,Unknown
   永不回收;冲突/非法前缀永久弃用);
3. **定价区间定稿**(表格不可见,口述定稿):FBA 区间1=0~30、区间2=30~75;
   FBM 区间1=15~80、区间2=80~1000;amz 价 × 限额表对应倍率;
   **重叠/边界向下兼容**(30 美金用 0-30 倍数);出界不上架;
4. 风控表(类目映射/禁止品牌)仍在维护,**须入 PG**(表格随时会停用);
5. LLM 映射继续 DeepSeek(key 入 .env);
6. 产品数据源暂时不可用 → L2c/d 端到端验收顺延,先建地基。

### L2a 实施状态(2026-08-07)

- [x] catalog.upc_pool + services/upc_pool(FOR UPDATE SKIP LOCKED 领号,
      旧三层并发补丁消灭;三类回收断言把关)+ upc_sync 工作流(注入
      校验/状态投影/池余量摘要)+ UPC_SHEET registry
- [x] services/pricing:区间常量 + '275%' 格式化值解析(2355 行事故防线)
      + walmart_price 积木
- [ ] 生产验证:注入一批 UPC → upc_sync → 核对状态列与池余量

### L2b 实施状态(2026-08-07)

- [x] 风控入库(所有者选 A:保留提交前否决闸,防审核→上架时间差):
      catalog.risk_product_types / brand_blacklist + risk_sync 工作流
      (wiki 表自动解析,只增改不删)+ services/risk_gate(拦截条件旧实证:
      准入状态='禁售' 或 中国卖家可做 以'否'开头;品牌 casefold)
- [ ] 生产验证:.env 两组 wiki token → risk_sync → 核对禁售/黑名单计数
- 黑名单体系远景(所有者 2026-08-07):产品中心库建成后,新增黑名单
  (沃尔玛类目/品牌/产品/amz 类目)以脚本增量跑库;清理来源数据入库须
  **清洗走流程**(对应产品/店铺对上,该进黑名单的进)——归黑名单建设批次

### L2 上架主链 list_new(最大;内部再分批,依赖 L0)

- [ ] 输入 provider:xlsx 模式先行(DMIT 导出 47 列),采集 API 模式预留
- [ ] 闸门链:店铺状态(fail-open)→ 日配额(北京 0 点重置;不在表默认
      999)→ 风控(禁售 PT + 品牌黑名单)→ 库存/价格过滤(<5 跳过;
      配送 >12 天上架但库存 0)→ 全局 ASIN 去重(读 catalog.walmart_items,
      不再依赖飞书总表/server cache)
- [ ] UPC 池切换:PG 事务领号(此时飞书池转投影,**切换前必停旧 morning
      launchd**,新旧同跑 = 重复领号重复上架)
- [ ] 变体分组 + LLM 映射(mapper 的全部实证约束照搬:零认证八项强制
      覆盖、文案长度硬约束、图片 minItems=5、productIdentifiers 单对象、
      price 裸 number、fulfillmentCenterID=Partner ID)+ llm 缓存(PG)
- [ ] 提交:走 api/feeds 唯一通道(三层防重已内置,旧 L4 反查语义同源);
      Yes/No/Unknown 三态 + UPC 回收三条路径(仅 提交前失败/RolledBack/
      4xx 拒绝 回收;Unknown 永不回收)
- [ ] 回执:reconcile 错误码四集合(UPC_COLLISION/ASYNC_PENDING/RETRIABLE/
      SKU_LOCKED)+ 优先级(SKU_LOCKED > 真SUCCESS > INPROGRESS > 全ASYNC >
      SUCCESS_WITH_WARNING > DATA_ERROR)+ 异步审核假错误绝不当失败重发;
      做成 feed_poll 反哺器回写上架表
- [ ] 产品事件账本接线:入库/审核/上架事件 + 上架前防呆闸(product_risk)

### L3 自愈链(依赖 L2)

- [ ] SKU_LOCKED → RETIRE_ITEM → 24h 冷却 → 清列重上(retire_cooldown 表);
      RETIRE_ITEM schema 与 MP_ITEM 完全不同(已在 api/feeds)
- [ ] 状态跟踪:旧 sync_status_track 的"反查真实状态 + Unknown 自愈"由
      catalog_sync(已上线)+ product_events 观测地基承接,只补
      "上架表 K=Unknown 而目录已在线 → 自愈回写"这一条

### L4 收尾

- [ ] upc_audit(全站 UPC 冲突审计,只读)
- [ ] 历史数据迁移批次:上架表 26 列全量、UPC 池 12 万行、pending_feeds
      在途收干净、retry_state 永久淘汰名单(丢了会重拉几万个已死 ASIN)
- [ ] 切换清单:停 launchd 4 条(morning/reconcile_hourly/retire_daily/
      health_4x)+ scheduled-tasks 的 dedup_sync(前端不迁,此任务作废)

## 确认记录(所有者 2026-08-07)

1. ✅ 上架表**新建**于在线产品表格,21 列 A~U(砍掉 状态跟踪/最近跟踪日期,
   产品事件账本承接);已进 registry(LISTING_SHEET)。
2. ✅ 「UPC是否一致」按代码实际行为登记 = 核验的 UPC 一致性。
3. ✅ L1 match_listing 先行;跟卖表 = 驱动表(单路飞书读,替代旧 xlsx;
   以后要 xlsx 再加)。UPC 池列初案(UPC/放入日期|状态/店铺/SKU/上架日期)
   待 L2 做 UPC 读取使用逻辑时专题讨论定稿。

## L1 实施状态(2026-08-07)

- [x] registry:LISTING_SHEET(21 列)/MATCH_SHEET(11 列)
- [x] api/feeds:MP_ITEM_MATCH v4.2(sellingChannel 制 header,REPLACE 幂等,
      15/hour 桶);SPEC 预检复用 api/items.search_walmart_spec
- [x] workflow match_listing:行状态机(待处理/可跟卖重排队/终态清 F 重试)
      + SPEC 候选(位数路由+zfill+退化码拒查)+ 按店打包 + 单店隔离
      + match_submitted/回执进事件账本(sku≠asin 登记例外)
- [x] feed_poll 反哺器第三行:跟卖表 J/K 回填
- [x] **对拍定稿**(2026-08-07,所有者提供旧 feed 备份):header 与五字段
      全部命中;condition 缺省补 New;SKU=旧系统人工编号(所有者澄清)→
      新系统 B 列人工优先、留空按其格式 PHUMWMT+YYYYMMDD+序号 自动续号
      (序号自 ops.feed_items 续,人工行不占号)
- [ ] 生产验收:.env 两 sheet_id → dry-run → 单店试点 → feed_poll 回填 → 验收
