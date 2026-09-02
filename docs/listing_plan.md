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

## 当前状态(2026-08-12 晚,代码迁移收官)

- **代码面:迁移完成**。L0~L2d 全部就绪;2026-08-12 四批收官:六点批复
  (批次一)+ 载荷漏迁补齐(批次二,旧仓逐字段对照)+ 接线(批次三)+
  违禁反哺黑名单闭环;两重自愈(SKU_LOCKED / K=Unknown)、跟卖库存
  provider、缺数据推采集闭环全部接通;第 5 轮错误(releaseDate 日期格式)
  已修,576 测试全绿。
- **验收面:✅ L2d 端到端验收通过**(2026-08-13:feed 18CB051E… 回执
  PROCESSED **成功 3 失败 0**,所有者确认)。**L2a 已验**(注入 47 →
  四态 28/0/10/9);**L2b 已验**(825 禁售 + 42102 品牌 + ASIN 黑名单
  5.6 万自产回路在库);**L1 跟卖试点后置**(所有者:功能暂时用不上,
  启用前再验)。
- **运维面(验收后)**:调度挂载(**2026-08-17 已挂**:product_chain 13:00 →
  blacklist 15:00 → audit_sheet 18:10 → list_new 20:00,当天次序硬约束;
  feed_poll 每 30 分)→ 切换清单(**旧 launchd 5 条 + AI skill 链
  两条调度必须同停**,新旧并跑=重复领号重复上架)→ L4 收尾(历史数据
  迁移批次/upc_audit)。
- **刻意后置**:update_listed 五字段集(归 maintenance)、健康仪表盘。
  (变体分组已于 2026-08-15 落地,且**不需要**采集侧把三字段提顶层 ——
  `services/variant_group` 直接读 `catalog.snapshots.raw`,详见「已知未做」节。)

## 阶段划分

> ⚠ 勘误说明(2026-08-12):L0/L1 规划段的复选框是**立案时原文**,一直没勾
> ——实况以各「实施状态」节与上方「当前状态」为准(L0/L1 均已完成;
> listing.tasks/retry_state 两表后来判定不建,见 backlog 第四节)。

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
3. **定价区间定稿**(表格不可见,口述定稿):FBA 区间1=0~30、区间2=**30~1000**
   (上界 2026-08-21 由 75 抬到 1000,所有者);FBM 区间1=15~80、区间2=80~1000;
   **落地价**(单价+运费)× 限额表对应倍率;**重叠/边界向下兼容**(30 美金用
   0-30 倍数);出界按 300% 定价(2026-08-09 改,此前是不上架);
4. 风控表(类目映射/禁止品牌)仍在维护,**须入 PG**(表格随时会停用);
5. LLM 映射继续 DeepSeek(key 入 .env);
6. 产品数据源暂时不可用 → L2c/d 端到端验收顺延,先建地基。

### L2a 实施状态(2026-08-07)

- [x] catalog.upc_pool + services/upc_pool(FOR UPDATE SKIP LOCKED 领号,
      旧三层并发补丁消灭;三类回收断言把关)+ upc_sync 工作流(注入
      校验/状态投影/池余量摘要)+ UPC_SHEET registry
- [x] services/pricing:区间常量 + '275%' 格式化值解析(2355 行事故防线)
      + walmart_price 积木。**定价输入是落地价 =(亚马逊单价 + 运费)× 倍率**
      (所有者定稿 2026-08-10;区间也按落地价选),运费没采到一律不定价——
      当 0 定出来的价偏低且两侧都不报错
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

### L2c 实施状态(2026-08-07)

- [x] spec 就位契约:`<DATA_ROOT>/specs/MP_ITEM/<版本串>/`(MPSetup_by_pt
      拆分产物;451MB 原始单文件与 307MB pt_templates 不拷)+
      services/pt_spec 加载器(lru_cache 512,未收录 PT 返 None 由调用方
      淘汰,缺目录报中文修复指引)
- [x] api/llm(DeepSeek,key 走 .env DEEPSEEK_API_KEY)+ catalog.llm_cache
      (旧 462MB sqlite 的 PG 化,旧数据不迁——key 含 model 换模型即失效)
- [x] MP_ITEM 进 feed 唯一通道:header 只收 3 字段 + version 完整时间戳
      (三个实证错误码注释在案);8/hour 桶;事件 kind=list 入生死类白名单
- [x] L2d 代码就绪(2026-08-07):amz_source 数据契约(fetch_products 预留,
      缺席行不写终态恢复自动续上)+ api/settings.partnerprofile + mapper
      (orderable 三陷阱/零认证强制+文档字段清理+enum 降级/文案硬约束/
      图片 minItems=5,逐条测试)+ list_new 主链(七道闸门链:店铺状态/
      日配额/PT spec/风控/全局去重/product_risk 防呆/数据过滤+定价;
      UPC 领号事务;LLM 映射走缓存;同店单 feed;三态结局 UPC 回收三类)
      + 上架表回执反哺器(四集合+优先级,SKU_LOCKED>SUCCESS>ASYNC>失败,
      错误码 strip \\t 实证)
- [x] **L2d 端到端 dry-run 打通**(2026-08-09,本机采集服务接线后):
      上架表 6 行 → 去重拦 2 + 库存不足拦 1(out_of_stock 正确识别)+
      **待数据源归零** → 3 行算出定价待提交(A107,$194.99/$82.48/$61.47,
      库存 = AMZ_IN_STOCK_QTY)。全链路证实:product_ingest → 中心库 →
      amz_source provider → 闸门链 → 定价
- [x] **L2d --execute 首跑打通全链**(2026-08-09,A107 三条):领 UPC → LLM
      映射(DeepSeek)→ MP_ITEM feed 提交 → feed_poll 回执 → 上架表 O/P/Q
      回填,机制无一处出错;但**三条全被沃尔玛以 DATA_ERROR 拒**
- [x] **spec 一致化流水线补迁**(2026-08-09,services/mp_conform):首跑暴露
      迁移缺口——旧 auto_listing/mapper.py 有 13 道后处理工序,此前只迁了
      强制覆盖/文案截断/图片三道,LLM 输出直接塞进载荷。补迁十道:条件必填
      不动点迭代 + 顶层必填兜底 + 类型对齐(标量→array/object、字符串→数字)
      + 枚举合法化 + 未知字段剔除(Orderable.productName 即因此被拒)+
      stateRestrictions 清理 + 空值/minItems 裁剪 + 小数位 + **提交前必填校验
      (不过就不提交,省 UPC 与配额)**;dry-run 加 -p check_spec=1 预检
- [x] **重跑验收通过**(2026-08-13,feed 18CB051E…):A121许家蕴 3 条
      (Bar Stools $194.99 / Bulb Planter Tools $82.48 / Fence Screens $61.47)
      **PROCESSED 成功 3 失败 0**,O/P/Q 反哺回填正常。六轮错误账收官:
      30 错 → 0 错。L2d 端到端验收 ✅

### L2d 攻坚(2026-08-09 暂停 → 2026-08-12 续做收官)

**状态:✅ 验收通过(2026-08-13,3/3 SUCCESS)**(续做内容见上方
「续迁批次一/二/三」与「续迁调研定稿」各节)。以下为暂停期存档。 四轮真跑把载荷问题从 30 个错收敛到只剩
UPC 撞库(运气问题,重试自愈)。所有者判断"上架这块复杂、先做别的",
本阶段暂停;续做时从「下次续做怎么走」直接接上。

#### 四轮错误账(每条都已修,注释与测试留档)

| 轮次 | 报错数 | 错误码 / 现象 | 根因 | 修法 |
|---|---|---|---|---|
| 1 | 30 | `50716566635066` 要 JSONArray/JSONObject(occasion/pattern/keyFeatures/material/尺寸类共 20+ 条) | **spec 一致化层整层没迁**——LLM 吐什么类型就塞什么 | 补迁 `mp_conform` 十道工序 |
| 1 | — | `72600149546850` 条件必填缺失(`country_of_origin_substantial_transformation` 三条全中、`seat_back_height_descriptor`) | 同上 + Orderable 段没给该字段 | 条件必填不动点迭代 + `build_orderable` 补齐 |
| 1 | — | `60670554076755` `Orderable.productName` 非法字段 | **旧实证在 v5 spec 下失效**(我照抄了"productName 两处同值") | `assemble_mp_item` 不再塞;`strip_unknown` 兜底 |
| 1 | — | `50716566635066` `quantity` 要 Number | `inventory[].quantity` 写成 `{unit,amount}` | 改裸 int(旧 `force_overrides` 原文) |
| 2 | 4 | `55506974520167` keyFeatures 需 ≥3 条(三条全中) | **`force_amazon_copy` 没迁**——文案本该用亚马逊原文,我们让 LLM 写 | 补迁:keyFeatures←bullet_points,不足拆句补齐 |
| 2 | — | `50716566635066` `swatchImages` 要 JSONObject | 缺"LLM 不该输出的系统后处理字段"清单 | `SYSTEM_OWNED_FIELDS` 主动丢弃 |
| 2 | — | (预检拦下)`ShippingWeight` / 卖点全空 | **`slow` 段没入库**——契约的 `raw` 是裁剪过的 | `catalog.products.slow` jsonb 全量留存 |
| 3 | 3 | `50716566635066` `[color]` 要 **String** 却给了数组 | **上一轮提示词改动的副作用**(LLM 过度套用"数组字段包数组") | 反向类型强制:标量字段收到数组取首元素 |
| 3 | — | `ERR_EXT_DATA_0101119` UPC 撞库 ×1 | 业务现实,非缺陷 | 池标 conflict 永久弃用 + 正交处置(多码并存也标) |
| 4 | 3 | `05570905585050` 变体三件套不完整 | **我们自己造成**:必填兜底填了 `variantAttributeNames` 没配套另两件 | `ensure_variant_bag`:单品 `isPrimaryVariant=Yes`,groupId 用 SKU 占位 |
| 4 | — | `ERR_EXT_DATA_0101119` UPC 撞库 ×2 | 同上,**所有者澄清:撞库只说明该 UPC 号被占,与产品是否已在沃尔玛无关** | 重试自愈(FAILED 行重新排队,上限 3 次) |
| 5 | 3 | `00030257670757` `[releaseDate]` 要 YYYY-MM-DD(批次二重跑,2026-08-12) | **批次二自伤**:新加的 Orderable 条件必填兜底把日期字段当普通字符串填了 'Not Available' | 日期字段感知(`_date_kind`:format 显式严格/名字推断两格式都认——endDate 要 DateTime 是同码反向实证)+ fix_type_mismatches 日期硬闸 + 提示词送 format |

#### 攻坚期沉淀下来的通用设施(已惠及全部 feed 类型)

- `ops.feed_item_errors`(一条报错一行,含 **field**)+ `ops.v_feed_error_stats`
  排行视图;**拉详情升为标准动作**(所有者定稿),六类 feed 的报错列统一写
  「码 \| 人话」;`feed_poll -p stats=1` 看排行、`-p feed_id=X` 看单 feed 详情
- 提交前 spec 预检(`list_new -p check_spec=1`):不领 UPC 不提交,
  **本地就知道哪行过不了**——攻坚后三轮全靠它,没再烧过 UPC
- FAILED 行自动重新排队 + 重试上限(旧 retry_state 阈值淘汰的等价物)

#### 下次续做怎么走

1. `python cli.py list_new -p check_spec=1` → 全 ✓ 再 `--execute`;
2. 回执有错:`feed_poll -p feed_id=X` 看 description(**不是看数字码**),
   按上表的模式定位是哪一层没对齐;
3. 新错误码进 `mp_conform` 对应工序 + 一条回归测试 + 本表追加一行;
4. 只剩 `0101119` 撞库 = **已经通了**,那是运气不是缺陷。

#### 续迁调研定稿(2026-08-12,旧仓 erpAPI 三路全量对照)

完整缺口清单(P0~P3 分级)在 **docs/backlog.md 第八节**,此处只记结论:

- **旧 auto_listing 40+ 模块逐一定性完毕**:主链/映射/定价/回执/UPC 池/风控/
  自愈/限速全部已被新系统承接(多数更强);死代码与一次性脚本共 8 个不迁;
  真缺口 6 个 P0(K=Unknown 自愈、跟卖库存、闸门前淘汰计次、缺数据推采集、
  配额增量补齐、manufacturer 双字段风控),回归/能力项见总账。
- **旧生产有两条调度链**:launchd 5 条 + AI skill 平台 erp-online-products-track
  (07:30,也写上架表 O/P/Q 与 R~W)。切换清单必须两条都停。
- **26→21 列**:旧 V/W 左移至新 T/U,迁移/对照一律**按列名**;旧读侧键名与
  config.py 列表早已过期,以 feishu_io.py docstring + 写侧函数为准。
- **变体分组评估**:核心 ~190 行纯函数,可做;跨店重定向(与人工域/配额
  口径冲突)与 LLM remap 建议砍;先决条件 = 采集契约顶层暴露
  parent_asin / variation_asins / variation_attributes。
- **旧 Excel/DMIT 47 列输入模式不迁**;excel_row_to_amazon_dict(120 行)是
  唯一完整的字段映射基准,已用于契约对照——结论:仅 manufacturer 需提顶层,
  其余都在 attrs 里;attrs.weight 形态需生产核实(否则 ShippingWeight 全量
  兜底 1.0 磅)。

#### 续迁批次一实施状态(2026-08-12,所有者六点批复当日落地)

- [x] K=Unknown 自愈:`listing_sheet.heal_unknown`(feed_poll 第五反哺器;
      feed 台账终态双向收尾 + 目录在线判定;"查无"永不负向写;UPC 随判定
      标已用/回收;SKU_LOCKED 移交自愈链)
- [x] 跟卖库存:`maintenance_intents.match_inventory_intents`(唯一路径,
      默认铺 10;stockzero 解除自动回补)
- [x] 配额切片后置(淘汰放切片前,配额以成功提交为准)
- [x] 缺数据自动推采集(`list_new._push_scrape`,日界批次名防重)
- [x] manufacturer 提顶层 + risk_gate 双字段
- [x] 闸门前淘汰计次:**否决不做**(所有者:数据每轮会变,不该永久淘汰;
      贵步骤已在切片后,无浪费)
- 批复原文与实现细节:docs/backlog.md 第八节

#### 续迁批次二:载荷构造漏迁补齐(2026-08-12,所有者"始终上不成"追查)

两路深比对(旧 mapper 全工序逐字段 / feed 信封逐键 + LLM 提示对照)结论:
**信封与三大陷阱形态逐键一致,洗清嫌疑;漏迁集中在 LLM 输入面与 Orderable**:

- [x] **LLM 提示词恢复旧富元数据**(最大嫌疑):此前只送 enum+desc——
      type 不送,模型只能猜数组还是标量(第 1/3 轮"要 JSONArray 给标量"
      "要 String 给数组"就是两种猜错方向);[:200] 硬截断会永久截掉排后面
      的必填字段。现恢复 type/required/minItems/items/object 结构、必填
      全量+可选截断(Visible 20/Orderable 10)、allOf 条件必填翻译块
- [x] **Orderable 交还 LLM**:旧结构=LLM 按 spec 填+force_overrides 覆盖
      10 项;此前写死 14 键,Orderable 条件必填永远给不出。LLM 输出改两段
      {"visible","orderable"}(旧缓存平铺形态兼容);**删掉旧金样从未发过
      的 Orderable.brand / countryOfOriginAssembly**(与 productName 同血统
      60670554076755);Orderable 段同过条件必填/类型/枚举一致化;
      ospec 空时不再把整个 Orderable 清空(改为保留+告警)
- [x] 零认证降级序列补回第四档 **"None"**(enum 只有 None 时旧选 None,
      漏这档掉到 enum[0]='Yes - Warranty Text' → 触发 warrantyText 必填)
- [x] 文档字段清单补 **suggested_number_of_people_for_assembly**(旧八项
      之一,留着与 isAssemblyRequired=No 自相矛盾)
- [x] safe_default array 分支优选 No/None/Not Applicable(此前 ["Yes","No"]
      兜成 Yes,方向反,还级联触发条件必填)
- [x] **keyFeatures 按 per-PT minItems 凑句**(旧 enforce_copy_limits;
      写死 4 会让 minItems=5/6 的 PT 被本地 validate 卡死**永远进不了 feed**
      ——"始终无法上架"的另一半可能是这个:不是被拒,是根本没提交)
- [x] 变体三件套改必填驱动:非必填半套整套剔除(旧系统单品从不发变体字段,
      SKU 占位组 ID 无实证),spec 必填才补全(第 4 轮实证 PT)
- [x] 图片保序去重(主图=亚马逊原序第一张;此前字典序把主图换掉)
- [x] DEEPSEEK_MODEL 可经 .env 切换(旧生产 deepseek-v4-flash),
      llm_cache 键与请求模型同源

**验收路径不变**:`list_new -p check_spec=1` 全 ✓ → `--execute` →
`feed_poll -p feed_id=X` 看 description。新错误码继续进 mp_conform+回归
测试+四轮错误账追加。

#### 已知未做(续做时的清单)

- ~~多变体分组(依赖采集 `slow.variant`)~~(已过时勘误:2026-08-15 落地
  `services/variant_group` —— 数据直接读 `catalog.snapshots.raw` 的 parent_asin /
  variation_asins / variation_attributes,**采集侧零改动**;slow.variant.theme 那段
  按 ':' 切而实际是 '=',只解析出 0.1% 全是垃圾,已绕开)
- ~~channel 一律走 FBM~~(已过时勘误:采集侧后来产出 `raw->>'is_fba'`,
  list_new 现行=读 is_fba 分 FBA/FBM 两套区间、**采不到不定价不上架**——
  所有者 2026-08-12 确认现行代码口径为定稿,与 maintenance"未知不猜"同源)
- [x] **店铺渠道闸**(2026-08-25 所有者定稿:「上架侧也补一下,读取配送限制列,
  我会在其中标记 fba/fbm,没标就都能上」)。此前上架链只用产品渠道选定价区间,
  **不看这家店做哪个渠道** —— 渠道硬闸只在分配链有,而 A/B 是人工域(运营可以
  直接填任意店+ASIN 绕过分配),且分配→上架之间货源渠道会翻转(list_new 按
  2026-08-19 定稿强制重采一轮再取数)。补法:限额表「配送限制」→
  `store_targets.store_channels()`,判定走唯一谓词 `channel_conflict`;
  **没标 = 不限制**(与分配侧"未填不接自由流"相反,照搬会把没配置的店整店废掉)。
  同批把维护侧接上:货翻成另一个渠道 ⇒ 库存写 0(`channel_mismatch`)⇒
  连续 N 天卖不了 ⇒ 删除链「渠道不符 N 天」下架,**与缺货同一条阶梯**。
- `AMZ_IN_STOCK_QTY`:仅在 `stock_count` 采不到时用;**终值=10**(所有者拍板 2026-08-12)
- [x] 变体分组:2026-08-15 落地(`variant_group.plan()` 增量归组、组大小上限
  `MAX_FAMILY=20` 超了退单品口径;`variant_remap` 补维度重映射、`variant_title`
  做标题差异化;同轮同族多主变体按 ASIN 字母序降级定序)

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
- [~] 产品事件账本接线:上架事件已接(list_submitted/match_submitted;
      回执 list_feed_{success|failed} 带错误码由 feed_track 落账,读侧
      catalog.feed_failures / ops.v_feed_error_stats);上架拦截 2026-08-12
      定稿=**黑名单双闸**(asin_blacklist + risk_gate,双链接通;防呆不看
      删除史,product_risk 只是查询档案 + unexplained_missing 报警);
      **失败反哺拦截闭环**(所有者 2026-08-12):上架回执命中三违禁码 →
      自动入 asin_blacklist B=禁售(feed_track 收口,幂等)→ 黑名单闸
      上架前拦截,同一产品不再烧 UPC/配额;其余 DATA_ERROR 只入事件
      不入黑名单(可修复类,入了会误杀重上架——与"防呆不看删除史"同源);
      **入库/审核两类事件未接**(等二期审核服务,见 docs/backlog.md 第三节)

### L3 自愈链(依赖 L2)——**暂缓**(所有者定稿 2026-08-07:暂时不用做,以后需要了再做)

- [x] SKU_LOCKED → RETIRE_ITEM → 24h 冷却 → 清列重上(listing.retire_cooldown 表)
      ——2026-08-12 `sku_locked_heal` 落地(所有者纠正:SKU_LOCKED 不是永久
      跳过;旧实证不先退役换 UPC 重发也失败,legacy_survey.md:1667)。危险
      工作流缺省即真跑(空跑加 `--dry-run`,2026-08-16 口径反转);回执失败标
      failed 人工处置不自动重试;**未进调度**(手动跑 —— registry/schedule.py:24-26
      把自愈类明确列进「不在表里的一律手动」,:5 记着它是被移出去的)
- [x] 状态跟踪:旧 sync_status_track 的"反查真实状态"由 catalog_sync 承接;
      "K=Unknown 自愈"已由 listing_sheet.heal_unknown 落地(2026-08-12,
      feed 台账终态双向 + 目录在线双源,挂 feed_poll 反哺器)

### L4 收尾

- [ ] upc_audit(全站 UPC 冲突审计,只读)
- ~~历史数据迁移批次~~(2026-08-12 所有者整批关闭:26 列/UPC 池 12 万行/
      pending_feeds/retry_state 全部不迁;防重拉已死 ASIN 由黑名单承担)
- [ ] 切换清单:**停旧清单与顺序以 docs/legacy_schedules.md §D 为准** ——
      launchd 上架 **5 条**(morning/store_status_hourly/reconcile_hourly/
      health_4x/retire_daily)+ erp_worker×20 + AI skill 07:30
      erp-online-products-track + 14:02 dedup-sync(dedup 是旧上架栈的去重命脉,
      **与旧上架栈同停**,erp_worker 还在跑就不能单停,不是「作废」)

## 确认记录(所有者 2026-08-07)

1. ✅ 上架表**新建**于在线产品表格,21 列 A~U(砍掉 状态跟踪/最近跟踪日期,
   产品事件账本承接);已进 registry(LISTING_SHEET)。
   ⚠ **2026-09-02 所有者第二次重排表头**:仍是 21 列,但内容换成
   店铺/ASIN/**SKU**/标题/PT/审核结果/**类别**/**具体内容**/审核日期/amz价格/
   库存/walmart价格/是否上架/上架feedid/上架日期/未上架理由/上架结果/报错/
   feed查询日期/**登记日期**/**查询编码**(旧尾部四列 真实标题/真实PT/真实UPC/
   UPC是否一致 已删)。同时**列定位改为按表头名**(services/listing_sheet.layout),
   写入 range 全部由它算 —— 以后再挪列顺序代码不用改,详见 docs/feishu_tables.md。
2. ✅ 「UPC是否一致」按代码实际行为登记 = 核验的 UPC 一致性(该列 2026-09-02 已被所有者删除)。
3. ✅ L1 match_listing 先行;跟卖表 = 驱动表(单路飞书读,替代旧 xlsx;
   以后要 xlsx 再加)。UPC 池列初案(UPC/放入日期|状态/店铺/SKU/上架日期)
   待 L2 做 UPC 读取使用逻辑时专题讨论定稿。

## L1 实施状态(2026-08-07)

- [x] registry:LISTING_SHEET(21 列 + `headers` 表头名,2026-09-02 重排)/
      MATCH_SHEET(11 列;**本次不动** —— 它的 B 列本来就是 SKU 列,
      services/match_sheet.py 按 `r["sku"]` 找行,不走上架表的 row_sku)
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
