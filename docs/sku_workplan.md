# SKU 改造工作包(执行计划)
> 状态:**已全部执行,待所有者验收**(2026-09-02 立项并当日完成:批次 0a/0b/1/2/3 共 10 个提交,
> 每块由 Opus 5 实现、Fable 审核提交;验收清单见 `docs/sku_plan.md` §8.1)。设计与决策见 `docs/sku_plan.md`,本文件是
> 逐批次的执行工作包:文件级改动、测试、验收命令、DDL、决策点、风险、回滚。
> 产出方式:Fable 规划调度;每个批次由一个 Opus 5 代理写初稿,四位 Opus 5 审查者
> (完整性 / 铁律红线 / 零行为变化证伪 / 可执行性)挑刺,再由 Opus 5 按意见修订。
> 横切包的修订稿因输出超长失败,**收录的是初稿**(审查意见见附录,执行前先按附录
> 修订)。行号为 2026-09-02 核对值,执行时必须重开文件再核。
>
> **阅读顺序**:§0 总览 → 各批次的 goal / zero_behavior_change_argument → items 表 →
> acceptance_commands。执行者按 item id 领活,一个 PR 对应 `estimated_pr_split`。

> ## ⚠ 所有者定稿覆盖(2026-09-02,优先级高于下文任何 item)
> 1. **上架表表头已由所有者重排(2026-09-02 第二次),SKU 在 C 列,不是 V 也不是 R**。
>    新 21 列(A~U)按顺序:店铺 / ASIN / **SKU** / walmart上架标题 / walmart_product_type /
>    审核结果 / **类别** / **具体内容** / 审核日期 / amz价格 / 库存 / walmart价格 / 是否上架 /
>    上架feedid / 上架日期 / 未上架理由 / 上架结果 / 报错 / feed查询日期 / **登记日期** / **查询编码**。
>    对应元组:store, asin, **sku**, list_title, product_type, audit_result, **audit_category**,
>    **audit_reason**, audit_date, amz_price, stock, walmart_price, listed, feed_id, list_date,
>    not_listed_reason, list_result, list_fail_reason, feed_check_date, **register_date**,
>    **query_code**(旧尾部 real_title / real_pt / real_upc / upc_match 四列已被所有者删除)。
>    与旧布局的差异:① SKU 插在 C ⇒ 旧 C~E 右移一列(标题 D、PT E、审核结果 F);② 旧 F「理由」
>    拆成 G「类别」+ H「具体内容」⇒ 旧 G 之后全部右移两列(审核日期 I、amz价格 J、库存 K、
>    walmart价格 L、是否上架 M、feedid N、上架日期 O、未上架理由 P、上架结果 Q、报错 R、
>    feed查询日期 S);③ T「登记日期」、U「查询编码」是所有者新列,**程序不读不写**(语义待所有者
>    说明),只在元组里占位。`_COLS` 仍为 21。
>    **所有硬编码 range 必须按新布局重排**(listing_sheet 里 `C{r}`→`D{r}`、`H{r}:N{r}`→`J{r}:P{r}`、
>    `C{r}:G{r}`→`D{r}:I{r}`、`F{r}`→`H{r}`、`N{rn}`→`P{rn}`、`K{r}:Q{r}`→`M{r}:S{r}`、`O:Q`→`Q:S`、
>    `K:N`→`M:P`、`A{r}:B{r}` 不变),并**改成由 columns 元组算列字母**(`col_letter("sku")`)而不是
>    再写死字母,这是本次重排最大的教训。审核链的「类别」写 `products.audit_reason`(37 政策类目),
>    「具体内容」写 `audit_reason.human_reason` 的人话(pass 行留空);`write_audit_notes` 的一句
>    人话写「具体内容」。工作包 item 文本里的 `V{r}` / 「V 列」/ `R{r}` 一律按此表换算。
>    ⚠ 生产上旧代码仍按旧布局写:表头改完到本分支上线之间,product_audit / list_new / feed_poll /
>    sku_locked_heal 任何一次真跑都会写错列(标题会盖掉 SKU 列)。
> 2. **飞书列名统一叫「来源码」**:销售订单表、售后订单表新增字段是「来源码」(不是
>    「ASIN」),值仍取 `order_lines.asin`;在线产品总表「来源码」在 **Q 列**(第 17 列),
>    元组元素名 `source_key`。四列所有者均已建好,飞书列接线 PR 不再等建列。
> 3. **来源字母定稿**:`SKU_SOURCE_LETTERS = {"amz": "A", "match": "B", "1688": "C", "self": "H"}`,
>    批次 0a 落 registry 时直接填值,不再留空。
>
## 0. 总览

| 批次 | 目标 | 零行为变化 | items | 依赖 |
|---|---|---|---|---|
| 0a | 建立 SKU 身份三块积木(services/sku_codec.py 为编码规则唯一之家 / services/sku_asin.pick_asin+resolve+resolv… | 是 | 28 | 无代码依赖:0a 是 SKU 改造的第一个批次,可直接在 main 上开分支。**也不再依赖 0b-11**(0a-25 |
| 0b | 把「从 SKU 认 ASIN」这件事在订单链、事件账本、黑名单链(含**回填/重建**那条,原工作包漏列)、审核判定链、两条清洗工作流、sources_backfill 七处全部改… | 否(见论证) | 31 | **批次 0a 必须先合**。0b 依赖 0a 交付的 `services/sku_asin.resolve(conn, |
| 1 | 给上架表加末列 V「SKU」,并把「这一行的 SKU 是什么」收口成唯一函数 services/listing_sheet.row_sku(V 列;空则回落 B 列 ASIN),让… | 否(见论证) | 24 | 【硬前置|所有者动作,阻塞合并】飞书上架表末尾新建 V 列,表头写「SKU」。建之前先用 `list_fields` / |
| 2 | 把「SKU = ASIN」这条隐含约定从写侧彻底摘掉:上架提交的 sku 从此是 registry 定值来源字母 + 11 位不透明随机码,身份唯一出处 catalog.listi… | 否(见论证) | 32 | **跨包前置(必须在写第一行代码前拍平,四位审查者一致列为 blocker)**:① 活码部分唯一索引的**名字与最终条 |
| 3 | 把存量在架商品的沃尔玛 SKU(裸 ASIN / 三段式)迁到批次 2 的 12 位不透明码:提交前先落库并 **commit**(新码行 replaces + 旧行 replac… | 否(见论证) | 40 | 批次 0a|身份积木与 SQL 收口已合并:services/sku_codec.py 的 mint/abandon/i |
| 横切 | 把 SKU 改造里「不属于任何单条业务链、但每条链都依赖」的那一层做实并冻结:登记簿三列与两个索引的幂等 DDL(含存量脏数据下不炸 db_init 的写法)、六份文档的同步点(d… | 是 | 35 | 【本包内部】C0-DDL-1 → C0-DDL-2 → C0-DDL-3:加列必须先于依赖新列的索引;体检视图必须先于引 |

合并顺序:0a → 0b → 1 → 2 → 3(横切包的 DDL / 守门测试随 0a 进,文档同步随各批进)。
每批 PR 的验收 = 该批 `acceptance_commands` 全绿 + `python -m pytest -q` 全绿 +
`--dry-run` 人眼确认 + 各批列出的 SQL 体检。

**所有者决策点汇总**(各批 decisions 去重后的关键项;完整清单在各批):
- **A|product_clear 停用(RETIRE)是否给 problem_scan 加豁免** —— 默认:默认假设:RETIRE **不弃码**(abandon 的原因词表里没有 product_clear_retire 这一项),豁免与否另议。0a 按此实现:ABANDON_REASONS = {delete_verified, sku_locked, upc_conflict, sku_update} 四项,守门测试反
- **B|UPC 撞库 0101119 时码与 UPC 是否一起换** —— 默认:默认假设:一起换。0a 在 ABANDON_REASONS 里保留 upc_conflict,并在 _BURN_STATUS 里**不给它配烧号状态**(该号已由 listing_sheet._mark_upc_conflicts 标成 conflict,再烧一次是重复动作)。调用点(services/listing_
- **C|alloc_push 派工口径是否对齐去重闸(含对 synthesis required_changes #6 后半句的处置)** —— 默认:默认假设:**alloc_push 对齐但拆两步;alloc_survey 不对齐**。0a 只做第一步(alloc_push 身份收口 + 落地恒真的 ls.abandoned_at IS NULL),零行为变化;第二步(去掉 workflows/alloc_push.py:52 的 lifecycle ACTIVE
- **D|upc_pool.burn_for_retire 何时改写状态值** —— 默认:默认假设:0a 只登记 burned_delete / burned_lock 两个值与中文标签,**不改任何写入点**(burn_for_retire:188-207 仍写 conflict),真正改写状态值与函数签名(burn(conn, pairs, status))推迟到批次 2 接 abandon 时一次做完
- **E|不透明码编码规则(字母表/长度/is_opaque)归哪个模块 —— 本包已裁决,记录在案** —— 默认:**唯一之家 = services/sku_codec.py**;registry/resources.py 只登记 SKU_SOURCE_LETTERS。理由:CLAUDE.md 铁律 3 管的是路径/token/表 ID/服务器地址这类外部资源,12 位码的字母表是内部编码规则;而来源字母是所有者要拍的取值,确属配
- **F|守门测试文件名 —— 本包已裁决,记录在案** —— 默认:**tests/test_sku_guard.py,全套 SKU 改造唯一一份**(与仓内既有 tests/test_feishu_guard.py 同族同形态:白名单 dict + test_the_whitelists_do_not_rot)。0a 建齐五张白名单,0b/1/2/3 与横切包的对应 item 全部降
- **D-0b-1|黑名单 `or sku` 原文兜底口径(sku_plan §8 未拍板项)** —— 默认:**保持现状**:`resolve_many(...) or sku` —— 登记簿与形态都解不出时仍用订货号原文入键,只额外加一条 logger.warning + 计数(conventions §六 真兜底三要件之「触发必须记日志计数」)。record_asins / collect_brands / _LATES
- **D-0b-2|order_audit.judge 改读 order_lines.asin 后,存量三段式/纯数字行的判定变化(**本批次唯一的判定口径变化**)** —— 默认:**采纳 sku_plan §3.3 的改法**:judge 用 `line_asin(line)`(asin 列优先、形态提取兜底)。后果是存量三段式(JTZW-B08M4D1GMT-38)与纯数字 item_id 形态的订单行,从今天的「SKU 不是 ASIN 形态 → 待人工、不推采集」转入正常判定链(推采集、算
- **D-0b-3|collect_brands 的 ops.dedupe 去重键(scope=cleanup:brand_asin)** —— 默认:**保持 sku 原文**(144 行 cands 键与 176 行 _MARK_SQL 实参都不动),加注释说明后果,加守门测试钉住;同时把「折叠后的 store 不得当查询键」写进注释并按全部出现过的店定序反查(0b-13②)。
- **D-0b-4|飞书三处加列的合并时机(**已按审查意见反转**)** —— 默认:**所有者先建列,代码后合**:0b-24/25/26/27/28/29 六条拆成**第二个 PR**「0b-飞书列接线」,只有在所有者(a) 在 ORDER_SALES 建文本字段「ASIN」、(b) 在 ORDER_RETURNS 建文本字段「ASIN」、(c) 把在线产品总表工作表列数扩到 ≥17 之后才合。合并
- **D-0b-5|在线产品总表新增列的列名(**已按审查意见改判**)** —— 默认:追加**一列**,元组元素取 **`source_key`**(值 = listing_sources.source_key),位置 = 末尾第 17 列;人读中文名「来源码」只写进 docs/feishu_tables.md。
- **D-0b-6|售后表 ASIN 的来源:借 order_lines 还是给 return_lines 加列** —— 默认:**借**:`_RETURNS_SQL`(services/order_center.py:66-76)已在 74 行 LEFT JOIN order_lines,顺手 `SELECT l.asin`。orders.return_lines **不加** asin 列(本批次零 DDL)。
- **D-0b-7|product_events 平台级事件(store 为空)的 asin 来源(**事实描述已按审查意见订正**)** —— 默认:**保持 extract_asin**,不查登记簿。平台级来源共四处,全部满足「sku 本来就是 asin,extract_asin 恒等返回」:services/product_ingest.py:266(sku=rec["asin"])、services/audit_store.py:196-215 event_r
- **D-0b-8|不透明码形态判据放哪(**原 0b-03 已撤回**)** —— 默认:**本批次不新增任何形态判据常量或函数**;sources_backfill 直接调 0a 交付的 `services/sku_codec.is_opaque`(services→services,依赖方向合法)。不透明码字母表 `_ALPHABET`、长度、「至少含一个字母」三条口径的唯一之家是 services/s
- **D1|决策 B(所有者未拍板)—— UPC 撞库 0101119 时码与 UPC 是否一起换** —— 默认:默认「换」(码与 UPC 同寿命)。对**批次 1 的代码没有影响**:本批只把 _mark_upc_conflicts 的反查键从 upc_pool.sku 改成 (store, asin)、并把状态条件与 burn_for_retire 对齐,烧号动作与语义一字不动(仍是 mark_conflict → 'conf
- **D2|存量行是否一次性把 B 列 ASIN 回填进 V 列** —— 默认:默认**不回填**,V 保持空。
- **D3|UPC 池表(UPC_SHEET)E 列「SKU」口径(sku_plan §8 待决项,本包是唯一归属)** —— 默认:默认 (a):不加列,只把口径写进 registry 注释 —— E 列 = catalog.upc_pool.sku 的投影,批次 2 起显示真 SKU;ASIN 留在 catalog.upc_pool.asin(领号复用键),要看再加列。见 B1-16。
- **D4|读 A2:V 之前是否在代码里加一次表头/列数核验** —— 默认:默认**不加代码检查**,但把实证从「上线纪律」升级为「阻塞式合并前置」:acceptance #1 的 `sheet_values_small(LISTING_SHEET, 'A1:V1')` 必须先跑通、输出贴进 PR,并把飞书对越界列区间的真实行为写进 read_rows 头注(B1-04 ③)。
- **D5|决策 A(product_clear 停用是否给 problem_scan 加豁免)与决策 C(alloc_push 派工口径是否对齐去重闸)** —— 默认:决策 A 默认「RETIRE 不弃码,豁免另议」;决策 C 默认「对齐」。**两者对批次 1 都是零影响**,本工作包不含任何相关改动。
- **D6|B1-01(registry 加列)与其余各条的合并时序** —— 默认:默认**整批一个 PR、但不许在 acceptance #1 通过之前合并**。合并顺序上把 B1-01 作为该 PR 的最后一个提交,便于评审一眼看清「加列」与「读列」的界线。
- **D7|_mark_upc_conflicts 的入参形状(2 元组 (store, ASIN) vs 含行上 SKU 的 3 元组)** —— 默认:默认 **2 元组 `(store, ASIN)`**,与 B1-09 一致。
- **D8|本批三条源码守门测试的落脚文件** —— 默认:默认放 **tests/test_listing_sku_col.py**;若合并时批次 0a 已交付 tests/test_sku_guard.py,则在同一个 PR 里搬进去,**两处不许都有**。
- **A|product_clear 停用(RETIRE)要不要给 problem_scan 加豁免、要不要弃码** —— 默认:RETIRE 不弃码(登记簿保持活码、UPC 不烧);problem_scan 的豁免另议,本批不实现。代码上体现为:守门测试反向钉死 product_clear 不得调 abandon;sku_locked_heal 的 RETIRE 弃码是「SKU_LOCKED 自愈链」这一条专用路径,与 product_clea
- **B|UPC 撞库 ERR_EXT_DATA_0101119 时,码是否与 UPC 一起换** —— 默认:一起换(弃码点 3):listing_sheet._mark_upc_conflicts 烧号处改为一次 sku_codec.abandon(reason=upc_conflict)(烧号由 abandon 内部完成,不再另调 mark_conflict),下一轮 mint 给新码、claim 给新号。无论选哪支,B2
- **C|alloc_push 派工口径是否对齐 list_new 去重闸(**本次修订已拍平原稿的自相矛盾**)** —— 默认:**只对齐 alloc_push,alloc_survey 明确不改**:workflows/alloc_push._SQL_ONLINE(:49-53)改为 missing_since IS NULL AND 登记簿该 (store, sku) 未弃码,去掉 :52 的 lifecycle 条件;services/a
- **D|变体品的 variantGroupId 仍从 parent ASIN 派生(货源隐匿的剩余漏洞)——新增决策项** —— 默认:本批不改,作为独立议题交所有者定,并由 B2-28 写进 docs/sku_plan.md §8 待决清单(不能只活在 risks 文本里)。默认假设:暂不改;单品口径反而变好(B2-04 之后单品的占位组 ID 从 ASIN 变成不透明码),变体品的组 ID 仍可倒推货源。
- **C|alloc_push 派工口径是否对齐去重闸** —— 默认:对齐。对批次 3 的影响:pending 期间旧码行 abandoned_at IS NULL 且在架,按对齐后的口径仍算「已在架」⇒ 不会被重新派工,**不需要**在 alloc_push._SQL_ONLINE 里额外加 replaced_by 条件。
- **D|跟卖存量(source_type='match',PHUMWMT+日期+序号)是否也迁** —— 默认:**不迁**。`SOURCE_TYPES = (SOURCE_AMZ,)`,候选 SQL 天然排除 match 行。理由:PHUMWMT 串本就不含 ASIN,货源隐匿收益为零;match 行的 source_key 是匹配 GTIN 而不是 ASIN(services/listing_sources.py:23),改
- **E|SkuUpdate 的 feed 形态:A(MP_MAINTENANCE 最小载荷)还是 B(MP_ITEM 全量)** —— 默认:**A**,常量 `workflows/sku_migrate.FEED_TYPE = "MP_MAINTENANCE"`,载荷由 `mp_mapper.build_sku_update_item()` 组。依据:US 官方 Update my existing items 明写 MP_MAINTENANCE「requ
- **F|POST outcome=unknown 时是否回滚(与 synthesis 的有意出入)** —— 默认:**不回滚,保持 pending**,留给下一轮 _settle 与 feeds 的启动对账。理由:unknown 的语义是「不知道到没到」(api/feeds.py:445 的既定处置就是保持 pending);若沃尔玛其实已经改成新码而我们回滚了登记簿,新码会成为一条没有出身的孤儿行(sources_backfil
- **G|ops.cleanup_seen_categories / ops.dedupe / catalog.claims 是否随 replaced_by 迁** —— 默认:**三者都不迁**。复核证据:① cleanup_seen_categories 全仓只有 workflows/cleanup_history_import.py 写它、**零读者**(2026-09-02 grep 复核),而它的主键是 (sku, category) 的唯一对、是「错误统计」累计数的真值来源,复制一
- **H|改码后的历史销量归属:经 sku_aliases 映射,还是把聚合键改成 ASIN** —— 默认:**经 sku_aliases 映射**(O12):services/alloc_survey._SQL_SALES 加 `LEFT JOIN catalog.sku_aliases`,输出键 `coalesce(a.sku, o.sku)`,返回字典的键形状不变,:790-791 与三个消费点(:515/:523/:
- **I|形态 B 下 SkuUpdate 如何穿过 mp_conform.strip_unknown** —— 默认:在 services/mp_conform.py 显式登记 `ORDERABLE_SYSTEM_SWITCHES = ("SkuUpdate",)` 并让 strip_unknown 放行(M5)。理由:名单穷举、触发记日志、条件明确,满足 conventions §六 真兜底三要件;而且它与 mp_mapper.OR
- **D1|活码唯一索引在存量有重复时的行为** —— 默认:包在 DO 块里条件建:先查 catalog.v_listing_sources_dupe_live,0 行才 CREATE UNIQUE INDEX,否则 RAISE WARNING 并跳过;缺失状态由 catalog_health F 段带 ⚠ 报出来。
- **D2|不透明码全局唯一索引的形态** —— 默认:表达式部分唯一索引 `(sku) WHERE sku ~ '^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{12}$'`,**不带 abandoned_at 条件**(含已弃码行,码永不复用);正则字符集与 services/sku_codec._ALPHABET 逐字一致,守门钉住。
- **D3|`abandoned_at IS NULL` 白名单的粒度** —— 默认:**文件级**四元素集合:services/sku_codec.py、workflows/list_new.py、workflows/alloc_push.py、refdata/schema.sql(DDL 豁免,理由写在集合旁)。list_new 里的 _SQL_LISTED_ASINS 与 _FAMILY_LIST
- **D4|SKU 编码常量落 registry 还是 services** —— 默认:只有 SKU_SOURCE_LETTERS(四个来源字母的映射)落 registry/resources.py;字母表 _ALPHABET、随机段长度 11、总长 12、查重重试 5 次全部落 services/sku_codec.py。
- **D5|sku_migrate 是否进调度** —— 默认:**不进**。手动跑,README 标「危 一」,一店一批;文档里写死它与 13:00 product_chain 抢同一个 MP_MAINTENANCE 速率桶,不许并跑。
- **D6|三个未拍板决策(A/B/C)在文档与代码里怎么落** —— 默认:conventions §九 第 8 段逐条写「默认假设 + 选另一个会怎样」并明确标**未拍板**;backlog.md 各记一条待办、写清阻塞哪个批次。代码按默认假设实现,两种选择的分叉点各集中在一处(A→problem_scan 扫描面;B→listing_sheet._mark_upc_conflicts;C→
- **D7|db_init 是否负责报 SKU 体检** —— 默认:**不报**。db_init 保持现状(执行 schema.sql + 返回固定摘要);体检归 catalog_health 新增的 F 段。
- **D8|批次 3 的 SkuUpdate 走哪个 feedType** —— 默认:**不新增 feedType 桶**;按单品实测结果二选一:MP_MAINTENANCE 最小载荷({sku 新码, GTIN 现号, SkuUpdate: Yes})优先,不行则 MP_ITEM 全量载荷。两者的桶(各 8/hour)与切片限额都已登记。
- **D9|upc_pool 两个新状态是否进 STATUS_CN 投影** —— 默认:进,文案「已烧(删除)」/「已烧(锁死)」。project_to_sheet 已是 STATUS_CN.get(status, status),加两项即自动生效,零代码改动。
- **D10|跟卖存量(PHUMWMT+日期+序号)与存量 relist 行是否迁** —— 默认:**不迁**(未拍板,默认保守):跟卖 SKU 不含 ASIN,改码收益只有「货源隐匿」而跟卖本来就不暴露亚马逊来源;文档里明写「未迁」,并在 sources_backfill 的『旧格式存量』桶里长期计数。

## 分批文件

- [批次 0a|身份积木 + 维护/审核/分配侧 SQL 收口(零行为变化)【第 2 版,已按四位审查者意见修订;行号 2026-09-02 逐](sku_workplan/batch_0a.md)  (94 KB)
- [0b|订单/事件/黑名单/审核侧收口 + 飞书 ASIN 列(零行为变化,一处例外)](sku_workplan/batch_0b.md)  (112 KB)
- [批次 1|上架表 V 列「SKU」+ 回执/自愈链读 V 列 + UPC 池口径(修订版 2026-09-02,已吸收四位审查者意见并逐条回](sku_workplan/batch_1.md)  (81 KB)
- [批次 2|写侧切换(唯一有行为变化的批次):list_new / match_listing 预备期 mint 不透明码 + 四个弃码点接 ](sku_workplan/batch_2.md)  (115 KB)
- [批次 3|存量改码(SkuUpdate 三态状态机 + 新工作流 workflows/sku_migrate.py,DANGEROUS=Tr](sku_workplan/batch_3.md)  (127 KB)
- [横切|批次 0a/0b/1/2/3 共用(DDL + docs 同步 + 守门测试总表 + registry 登记 + 调度 + 测试策略 ](sku_workplan/crosscut.md)  (90 KB)
- [附录 A|四路审查意见(修订前的原始 findings;已修订包的处置写在各 item 的 why/risks 里)](sku_workplan/review.md)  (121 KB)
