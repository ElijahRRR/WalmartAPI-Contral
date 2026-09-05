## 横切|批次 0a/0b/1/2/3 共用(DDL + docs 同步 + 守门测试总表 + registry 登记 + 调度 + 测试策略 + 依赖图 + 验收模板 + 回滚)

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

> ⚠ 本包为**未经修订的初稿**(修订代理输出超长失败)。执行前先按附录 A 中点名本包的 findings 修订。

**目标**:把 SKU 改造里「不属于任何单条业务链、但每条链都依赖」的那一层做实并冻结:登记簿三列与两个索引的幂等 DDL(含存量脏数据下不炸 db_init 的写法)、六份文档的同步点(db_schema / feishu_tables / api_blueprint §retire 语义更正 / conventions 新增 §九 / CLAUDE.md / README)与 skills 生成物、一张守门测试总表(extract_asin 调用点 / ASIN 形态正则 / `= w.sku` 硬等号 / `abandoned_at IS NULL` 三处 / abandon 调用点 / 不透明码正则两处一致 / 飞书字段常量)、registry 新常量与字段登记清单、调度不新增任务与 sources_backfill 报警语义变更、测试策略、批次依赖图与合并顺序、每批 PR 验收模板、逐批回滚方案。

**零行为变化**:是

三类事逐类论证:①DDL 全是加法——listing_sources 三列全 NULL 默认、无 NOT NULL；两个新索引不改任何现存查询的结果集（现存查询都不带 abandoned_at 谓词）；upc_pool 只改注释不改列与约束（status 无 CHECK，已核 schema.sql:244-255）。唯一有语义的是 audit_listing_conflicts 视图改经登记簿反查，其存量等价性成立：登记簿存量行由 schema.sql:230-236 按 SKU 格式回填，裸 ASIN 形态的 source_key 逐字等于 sku，故 `p.asin = coalesce(ls.source_key, w.sku)` 与旧式 `p.asin = w.sku` 在存量上返回同一集合；「在架但未登记」的行由 LEFT JOIN + coalesce 兜住，仍返回旧结果。②文档与 skills 生成物不进运行路径，命令行与参数一字不变。③守门测试只读源码做断言，白名单按现状预填，合并当天全绿。真正的行为变化全在别的工作包（0a 六处 SQL、0b 十四处消费方、2 的 mint 与四个弃码点、3 的 SkuUpdate）。

### 改动清单

#### C0-DDL-1 · `refdata/schema.sql` · 在 221(listing_sources 的 `);`)与 222(key_idx 注释块起始)之间插入

**改动**:三条幂等加列:`ALTER TABLE catalog.listing_sources ADD COLUMN IF NOT EXISTS abandoned_at timestamptz;` / `abandoned_reason text;` / `replaced_by text;`。注释三件套:①列名用 abandoned 不用 retired,钉死「码弃用 ≠ 沃尔玛 lifecycle RETIRED ≠ product_clear 停用」;②abandoned_at IS NULL 的行叫活码,弃码只有一个实现 services/sku_codec.abandon,四个弃码点;③本表行永不 DELETE、除 abandon 外全仓不许 UPDATE。加列必须先于下面依赖新列的索引。

**为什么**:登记簿是身份唯一出处,三列是四个弃码点、mint 复用活行、批次 3 三态状态机的落点。位置纪律是 schema.sql:56-58 已写死的(2026-08-06 生产实证:旧库表已存在时 CREATE IF NOT EXISTS 跳过,列靠 ALTER 补,先建索引会 UndefinedColumn)。

**测试**:
- tests/test_sku_schema.py::test_listing_sources_has_the_three_lifecycle_columns
- tests/test_sku_schema.py::test_abandoned_not_retired_is_pinned_in_the_ddl_comment

**验收**:python cli.py db_init && python cli.py db_init(连跑两次都成功);psql -d walmart_data -c "\d catalog.listing_sources"

#### C0-DDL-2 · `refdata/schema.sql` · 紧接 C0-DDL-1 之后(原 227 行 key_idx 语句之后、228 行回填 INSERT 注释之前)

**改动**:先建体检视图 `CREATE OR REPLACE VIEW catalog.v_listing_sources_dupe_live AS SELECT store, source_type, source_key, count(*) AS n, array_agg(sku ORDER BY created_at) AS skus FROM catalog.listing_sources WHERE abandoned_at IS NULL AND source_key IS NOT NULL GROUP BY 1,2,3 HAVING count(*) > 1;`;再用 DO 块条件建活码唯一索引:`IF NOT EXISTS (SELECT 1 FROM catalog.v_listing_sources_dupe_live) THEN EXECUTE 'CREATE UNIQUE INDEX IF NOT EXISTS listing_sources_live_uidx ON catalog.listing_sources (store, source_type, source_key) WHERE abandoned_at IS NULL AND source_key IS NOT NULL'; ELSE RAISE WARNING ...; END IF;`。注释写明:该索引是并发双 mint 的唯一拦截,没建成必须被 catalog_health F 段带 ⚠ 报出来。

**为什么**:db_init 幂等可重跑是硬约束,而存量登记簿是否已有重复活行未连库核对过(match 行 source_key 是 GTIN,同店同 GTIN 重上过就会有两个 SKU 行)。裸建唯一索引会让 db_init 在生产上一次性炸死,而 conventions §五「未连库核对之前一律不执行」正是禁止这种赌。视图用 CREATE OR REPLACE 而非 DROP+CREATE,理由同 schema.sql:500-503 记的教训。

**测试**:
- tests/test_sku_schema.py::test_live_unique_index_is_created_conditionally
- tests/test_sku_schema.py::test_dupe_health_view_uses_create_or_replace

**验收**:python cli.py db_init;psql -c "SELECT * FROM catalog.v_listing_sources_dupe_live"(期望 0 行);psql -c "SELECT indexname FROM pg_indexes WHERE indexname='listing_sources_live_uidx'"(期望 1 行)

#### C0-DDL-3 · `refdata/schema.sql` · 紧接 C0-DDL-2 之后

**改动**:`CREATE UNIQUE INDEX IF NOT EXISTS listing_sources_opaque_sku_uidx ON catalog.listing_sources (sku) WHERE sku ~ '^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{12}$';`。注释两条:①字符集与 services/sku_codec._ALPHABET 必须逐字一致(同 product_events_identity_idx 的表达式索引纪律,schema.sql:188 已有先例),改一边必须改另一边,守门 tests/test_sku_guard.py;②不加 abandoned_at 条件——跨店永不复用,已弃码行照样占着这个串。

**为什么**:全表 UNIQUE(sku) 建不起来:存量 sku=asin 时同一 ASIN 在两家店是同一串,跨店重复是既成事实(sku_plan §6)。按形态做部分唯一索引是唯一能同时满足「新码全局唯一」与「存量不动」的形态;30 符号字母表(剔 0/O、1/I/L、U)天然把裸 ASIN(10 位)、三段式(含 -)、纯数字 item_id(含 0/1)排除在索引条件外。

**测试**:
- tests/test_sku_guard.py::test_opaque_sku_regex_matches_the_codec_alphabet(sku_codec 未落地时 skip,0a 合并后转正)
- tests/test_sku_schema.py::test_opaque_index_has_no_abandoned_filter

**验收**:python cli.py db_init;psql -c "SELECT count(*) FROM catalog.listing_sources WHERE sku ~ '^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{12}$'"(批次 2 之前必须为 0)

#### C0-DDL-4 · `refdata/schema.sql` · 238-255(upc_pool 状态机注释与建表块)

**改动**:只改注释、不改列不加约束:状态机注释补 `burned_delete`(DELETE 经观测核验后随码一起烧)/ `burned_lock`(SKU_LOCKED 退役后烧),并写明为什么不复用 conflict(conflict = 全站已存在;burned_* = 跟着一个已弃的码下岗;混在一起后 upc_pool_status_idx 分组的 pool_stats 报表再也分不开)。同时注明 status 列**没有 CHECK 约束**,新增取值不需要 DDL,登记面是 services/upc_pool.STATUS_CN。

**为什么**:synthesis rule §2 要求烧号用独立状态值;而「不需要 DDL」这件事本身必须写在 schema.sql 里,否则下一个人会以为漏了迁移而去加约束。

**测试**:
- tests/test_upc_pricing.py::test_burn_statuses_are_documented_in_the_schema

**验收**:python cli.py db_init;psql -c "SELECT status, count(*) FROM catalog.upc_pool GROUP BY 1"(批次 2 之前不应出现两个新值)

#### C0-DDL-5 · `refdata/schema.sql` · 521-551(audit_listing_conflicts);要改的是 526-533 的 live_rejected CTE,第 527 行 `JOIN catalog.products p ON p.asin = w.sku AND p.marketplace = 'US'`

**改动**:live_rejected CTE 改成 `FROM catalog.walmart_items w LEFT JOIN catalog.listing_sources ls ON ls.store = w.store AND ls.sku = w.sku JOIN catalog.products p ON p.marketplace = 'US' AND p.asin = coalesce(ls.source_key, w.sku) WHERE w.missing_since IS NULL AND p.audit_status = 'rejected'`。LEFT JOIN 不可改成 JOIN;不加 abandoned_at 过滤;LATERAL 段(536-551)一字不动。

**为什么**:sku_plan §3.2 第 6 处:不改则 problem_scan 的审核来源建议对新码归零(rejected_still_listed 恒空)且不报错。放在本包是因为它是 DDL——与 0a 的六处 Python SQL 分批合并会让 db_init 与代码脱节。LEFT JOIN 是硬要求:未登记的在架行必须按旧口径回落 w.sku,否则 sources_backfill 跑之前的新在架行会从冲突面里静默消失。

**测试**:
- tests/test_problem_scan.py::test_conflicts_view_join_matches_its_index(现有 :290-303,补断言 `coalesce(ls.source_key, w.sku)` 与 `LEFT JOIN catalog.listing_sources` 在视图文本里;原有两条断言一字不动)
- tests/test_problem_scan.py::test_conflicts_view_does_not_filter_abandoned(新增)

**验收**:python cli.py db_init;python -m pytest -q tests/test_problem_scan.py;psql -c "SELECT count(*) FROM catalog.audit_listing_conflicts"(改前改后行数必须一致)

#### C0-DDL-6 · `refdata/schema.sql` · 631-688(order_lines 建表与索引块)之后、689 行之前

**改动**:加只读体检视图:`CREATE OR REPLACE VIEW orders.v_order_line_dupes AS SELECT store, po_id, line_number, count(DISTINCT order_line_id) AS n, array_agg(DISTINCT sku) AS skus FROM orders.order_lines WHERE line_number IS NOT NULL GROUP BY 1,2,3 HAVING count(DISTINCT order_line_id) > 1;`。注释写明它只为批次 3 存在。

**为什么**:改码后若沃尔玛对改码前的 PO 返回新码,会被当成新行插入(行身份 = sha256(PO+SKU),旧行不删)⇒ 同一笔货双算;订单侧没有任何现成告警(sku_plan §7 批次 3 末段)。视图是最便宜的兜底,纯只读零行为变化。

**测试**:
- tests/test_sku_schema.py::test_order_line_dupe_view_exists

**验收**:python cli.py db_init;psql -c "SELECT * FROM orders.v_order_line_dupes LIMIT 20"(批次 3 之前应为 0 行)

#### C0-HEALTH-1 · `workflows/catalog_health.py` · docstring 的 A~E 清单(8-16 行)加一条 F;_SQL(32 行起)之后新增 `_SKU_SQL`;run()(97 行起)报告拼装末尾追加 F 段

**改动**:新增 F 段「SKU 登记簿体检」五项纯只读计数:①在架未登记行数(复用 workflows/sources_backfill._SQL_GAP 的 NOT EXISTS 形态);②活码重复组数(查 catalog.v_listing_sources_dupe_live);③活码唯一索引在不在(查 pg_indexes,0 就在摘要写「⚠ 并发双 mint 无护栏」);④不透明码行数与已弃码行数;⑤订单双算组数(查 orders.v_order_line_dupes)。任一项非零行首带 ⚠。**docstring 首行一字不动**——它被 services/gpt_skill._one_liner 取用。

**为什么**:db_init 只执行 SQL 并返回固定摘要(workflows/db_init.py:38-56),RAISE WARNING 进不了摘要;三个索引/视图的状态必须有人看得见,否则「唯一索引没建成」会静默存在到某天两个进程同时 mint。体检归 catalog_health 是既有分工(它就是「纯 SQL 只读体检」),不新开工作流 = 每个能力一条实现路径。

**测试**:
- tests/test_catalog_health.py(新文件)::test_report_has_a_sku_section
- tests/test_catalog_health.py::test_missing_unique_index_is_flagged
- tests/test_readme.py::test_every_workflow_appears_in_the_readme(不变)

**验收**:python cli.py catalog_health(F 段五项都打印;合并 0a 前 ④⑤ 应全 0)

#### C0-GUARD-1 · `tests/test_sku_guard.py` · 新文件,整篇骨架(白名单区 → 取材区 → 断言区 → 白名单不许烂掉)

**改动**:结构照抄 tests/test_feishu_guard.py 的三段式。取材复用其形状:`_prod_files()` 返回 cli.py + services/ + workflows/ + registry/ + api/ 下全部 .py(排除 __pycache__),`_calls_with_scope(tree, name)` 返回 [(Call, 所在函数名)]。**射程不含 tests/**。五张白名单全部是 dict[(相对路径, 函数名) | 相对路径, 理由字符串],理由里必须写「批次 X 收口后删」。末尾一条 `test_the_whitelists_do_not_rot`:每条白名单必须还指得着文件/函数、理由非空(照抄 test_feishu_guard.py:348-371)。

**为什么**:sku_plan §7 明写要加这条守门:extract_asin 调用点与硬等号 SQL 只允许出现在白名单文件里,新增即红,防止切换后又长出新洞。所有者要求波及面一次做完,守门是这句话在时间上的保险。

**测试**:
- tests/test_sku_guard.py::test_the_whitelists_do_not_rot

**验收**:python -m pytest -q tests/test_sku_guard.py(合并当天必须全绿——白名单按现状预填)

#### C0-GUARD-2 · `tests/test_sku_guard.py` · 白名单 `_EXTRACT_ASIN_OK: dict[tuple[str,str], str]`;断言 `test_extract_asin_is_only_called_from_the_whitelist`

**改动**:AST 扫全部生产文件对 extract_asin 的调用(含 `sku_asin.extract_asin` 属性调用与 from-import 后的裸名),(文件, 所在函数) 不在白名单即红。白名单按现状预填 12 处待收口 + 3 处永久豁免,逐条写批次归属:services/sku_asin.py(规则本体,永久)/ services/order_lines.py:169(→0b)/ services/audit_rules.py:175,180(→0b)/ services/product_events.py:167(→0b)/ services/alloc_survey.py:291,796(→0a)/ services/blacklist.py:99,157(→0b)/ workflows/alloc_push.py:72(→0a)/ workflows/alloc_plan.py:127(→0a)/ workflows/alloc_products.py:101(→0a)/ workflows/order_audit.py:1239(→0b)/ workflows/order_history_import.py:167(永久,只导旧数据)/ workflows/pt_backfill.py:96(永久,旧库回填)。

**为什么**:extract_asin 对 12 位不透明码必返 None(形态本身就是分流器),所有调用点都是「静默返回 None 然后功能没了」的候选。冻结成白名单后,0a/0b 每收口一处删一条,清单归零那天波及面才算真的做完。

**测试**:
- 自身即测试;配套 test_the_whitelists_do_not_rot

**验收**:python -m pytest -q tests/test_sku_guard.py -k extract_asin

#### C0-GUARD-3 · `tests/test_sku_guard.py` · 白名单 `_ASIN_SHAPED_REGEX_OK: dict[str, str]`(文件粒度);断言 `test_no_new_asin_shaped_regex_outside_the_whitelist`

**改动**:文本轨 + AST 常量轨扫 ASIN 形态正则字面量(`B\[0-9A-Z\]\{9\}`、`^B[0-9A-Z]{9}$`、`\^B0\[A-Z0-9\]\{8\}` 三种写法),文件不在白名单即红。现状预填:services/sku_asin.py(规则本体,永久)、services/order_audit.py(:358-361,→0b 改读 line['asin'] 后删)、workflows/sources_backfill.py(:46 _ASIN_RE,→0b 改摘要分桶时保留但注释改口径)、workflows/product_refresh.py(:58,89,→0b 改查登记簿后删)、workflows/scrape_missing.py / asin_blacklist_import.py / product_query.py / brand_scrape.py(语义是校验外部输入是不是 ASIN,永久豁免,理由逐条写清)。

**为什么**:sku_plan §3.3 初稿只列 7 处、二稿补到 14 处,漏掉的那一半正是「不调 extract_asin、自己手写正则」的形态(services/order_audit.py:358-361 明写「最容易漏」)。只守函数调用点等于只守了一半。

**测试**:
- 自身即测试

**验收**:python -m pytest -q tests/test_sku_guard.py -k regex

#### C0-GUARD-4 · `tests/test_sku_guard.py` · 白名单 `_SKU_ASIN_EQUALS_OK: dict[str, str]`(文件粒度);断言 `test_sku_equals_asin_hard_joins_stay_in_the_whitelist` 与防误报用例 `test_sku_to_sku_joins_are_not_flagged`

**改动**:文本轨扫 .py 与 .sql 里四种硬等号形状(`=\s*w\.sku\b`、`w\.sku\s*=\s*\w+\.asin`、`\.asin\s*=\s*\w*\.?sku\b`、`=\s*live\.sku\b`)。**必须先剔掉两边都是 sku 的合法 JOIN**(`ls.sku = w.sku` / `ni.sku = w.sku` / `w.sku = d.sku` / `w.sku = f.sku` / `w.sku = o.sku`)。现状预填六处:services/maintenance_intents.py(:192,202,233,322 →0a)、workflows/product_audit.py(:411 →0a)、refdata/schema.sql(:527 →本包 C0-DDL-5 改完即删)。

**为什么**:sku_plan §3.2 六处硬等号是「维护链对新品永久失明」「在架复审候选恒空」「problem_scan 审核来源归零」三条最贵的静默失效。正则必须先排 sku=sku,否则守门会红在 12 个无辜的地方(services/maintenance_intents.py:191,235,283,651 等)而被人整条注释掉。

**测试**:
- tests/test_sku_guard.py::test_sku_to_sku_joins_are_not_flagged(用内联 SQL 样例断言 `ON ls.store = w.store AND ls.sku = w.sku` 不被判红)

**验收**:python -m pytest -q tests/test_sku_guard.py -k equals

#### C0-GUARD-5 · `tests/test_sku_guard.py` · 白名单 `_ABANDONED_FILTER_OK: frozenset[str]`;断言 `test_abandoned_is_null_appears_in_exactly_three_places`

**改动**:文本轨扫全部生产文件 + refdata/schema.sql 里的 `abandoned_at IS NULL`,文件必须在四元素集合里:services/sku_codec.py(mint 查活行)、workflows/list_new.py(去重闸 _SQL_LISTED_ASINS 与同口径的 _FAMILY_LISTED_SQL,同文件算一处)、workflows/alloc_push.py(_SQL_ONLINE,决策 C)、refdata/schema.sql(listing_sources_live_uidx 的部分索引条件——DDL 不是消费方过滤,理由写在集合旁)。批次 2 后加反向断言:这四个文件里必须各至少出现一次。

**为什么**:synthesis rule §6 的消费方契约:resolve / 维护链 JOIN / 事件归并 / 订单反查一律不按 abandoned_at 过滤——订单带旧码回来必须查得到。多长一个过滤点 = 一条链对已弃码的历史静默失明。schema.sql 那一处是本包引入的,不豁免掉守门会红在自己身上。

**测试**:
- 自身即测试

**验收**:python -m pytest -q tests/test_sku_guard.py -k abandoned_is_null

#### C0-GUARD-6 · `tests/test_sku_guard.py` · 白名单 `_ABANDON_CALLERS_OK` 与硬清单 `_MUST_NOT_ABANDON`;三条断言

**改动**:(a) `test_abandon_is_called_only_from_the_four_points`:AST 扫 abandon( 调用,(文件, 函数) 必须在四元素白名单里(delete_verified 观测落地点 / workflows/sku_locked_heal._relist / services/listing_sheet._mark_upc_conflicts(决策 B) / workflows/sku_migrate)。sku_codec 未落地时 skip。(b) `test_the_five_non_abandon_workflows_never_call_it`:workflows/product_clear.py、workflows/problem_product_cleanup.py、workflows/maintenance.py、services/walmart_catalog.py、services/feed_track.py 里出现 abandon 字样即红,断言消息写「product_clear 停用(RETIRE)不弃码:沃尔玛侧记录仍在、仍绑着我们的 UPC,抽新码 = 同店两条同内容记录 + 白烧一个 UPC」。(c) `test_only_sku_codec_updates_the_registry_lifecycle_columns`:文本轨扫 `UPDATE catalog.listing_sources`,只允许出现在 services/sku_codec.py。

**为什么**:synthesis required_changes #3 的原话:守门测试反向钉死非弃码点不得调 abandon。这是「四个弃码点、只有四个」唯一可执行的表达;没有它,下一个人在 product_clear 里顺手加一行 abandon,每次停用烧一个 UPC 且永久失去可恢复性,而且不报错。(b)(c) 从合并当天就生效,不依赖 codec 存在。

**测试**:
- 自身即测试;批次 2 后加 tests/test_sku_guard.py::test_abandon_records_a_ledger_event(桩 conn,断言一次 UPDATE + 一次 record_many(sku_abandoned))

**验收**:python -m pytest -q tests/test_sku_guard.py -k abandon

#### C0-GUARD-7 · `tests/test_sku_guard.py` · 断言区末尾 `test_feishu_field_names_come_from_registry_constants` 与防误报用例

**改动**:AST 常量轨扫 services/ 与 workflows/,禁止 "ASIN" / "SKU" / "来源码" 三个串作为 dict 键或 fields= 实参出现;白名单 `_FIELD_LITERAL_OK` 现状预填 registry/resources.py 自身。断言消息指向 CLAUDE.md:61。注释里说明:注释与 docstring 天然不在 AST 常量轨的射程,不需要豁免。

**为什么**:批次 0b 要给两张订单表加「ASIN」列、批次 1 要加上架表 V「SKU」与在线产品总表「来源码」——四个新字段名是本轮最容易被顺手写成字面量的地方。services/order_center.py:370,405 现有写法(`f.sku: r["sku"]`)证明这条纪律在仓里是活的,仓内此前没有对应守门(已核:test_feishu_guard 只守端点路径与限额常量)。

**测试**:
- tests/test_sku_guard.py::test_field_literal_guard_does_not_flag_registry

**验收**:python -m pytest -q tests/test_sku_guard.py -k field

#### C0-REG-1 · `registry/resources.py` · 507-524(LISTING_SHEET 的列契约注释与 columns 元组)

**改动**:columns 元组**末尾追加** `"sku"`(共 22 项,A~V);注释加一行 `V=SKU`,并写死「加在末尾是硬要求:listing_sheet 里所有写入 range 都是字母硬编码的连续段(C{r}、H{r}:N{r}、K{r}:Q{r}),插在中间全体错位」。同批 services/listing_sheet.py:41 `_COLS = 21` → `22`(那一处属批次 1 的工作包,本包只负责 registry 这一条与两处同步的守门)。

**为什么**:sku_plan §1 问 4 与 §7 批次 1;registry 是列序唯一出处(services/listing_sheet.py:10-11 已写死「表头再动一次,只改这里」)。

**测试**:
- tests/test_sku_guard.py::test_listing_sheet_cols_matches_registry(断言 listing_sheet._COLS == len(LISTING_SHEET.columns)——两处数字脱节会让 read_rows 静默少读一列)
- tests/test_list_new.py 读表夹具补第 22 列(处理原则见 C0-TEST-3)

**验收**:python -m pytest -q tests/test_list_new.py tests/test_sku_guard.py;python cli.py list_new --dry-run -p check_spec=1

#### C0-REG-2 · `registry/resources.py` · 294-312(ORDER_SALES.fields)与 330-347(ORDER_RETURNS.fields)

**改动**:两处 `_fields(...)` 各加一项 `asin="ASIN"`,紧跟 `sku="SKU"` 之后。注释补一句:加列 ⇒ 行指纹全变 ⇒ 下一次 push 把 90 天窗口全量重推一遍,**预告不是故障**。消费点在 services/order_center.py:51(_SALES_SQL 补 asin)、:68(_RETURNS_SQL 补 l.asin,该 SQL 已 LEFT JOIN order_lines)、:370 与 :405 的投影字典各加 `f.asin: r["asin"]`——那四处归批次 0b。

**为什么**:sku_plan §3.5:订单表现在只有「SKU」列,新码切换后运营在飞书上再也认不出这一单是什么产品;asin 列在 order_lines 已存在(schema.sql:686),投影是最后一跳。

**测试**:
- tests/test_order_center_push.py 销售/售后投影用例的期望字典各加一键(处理原则见 C0-TEST-3)
- tests/test_sku_guard.py::test_feishu_field_names_come_from_registry_constants(保证消费点不写字面量)

**验收**:python -m pytest -q tests/test_order_center_push.py;python cli.py order_center_push --dry-run(摘要里销售订单行数 = 90 天窗口全量,与预告一致)

#### C0-REG-3 · `registry/resources.py` · 252-268(ONLINE_PRODUCTS_SHEET,columns 在 261-267)

**改动**:columns 元组末尾追加 `"source_key"`(飞书表头写「来源码」)。注释补:本表列序 = catalog.walmart_items 字段序 + 尾部投影列;source_key 由 catalog_sync 写入时 LEFT JOIN catalog.listing_sources 取,**不按 abandoned_at 过滤**(消费方契约)。追加在末尾,理由同 LISTING_SHEET。

**为什么**:sku_plan §3.5:切换后在线产品总表只有随机码,运营与人工排查失去「这行是哪个 ASIN」的唯一入口;来源码列是反查的人看面。

**测试**:
- tests/test_catalog_sync.py 投影用例补一列(处理原则见 C0-TEST-3)

**验收**:python -m pytest -q tests/test_catalog_sync.py;python cli.py catalog_sync --dry-run -p store=<单店>

#### C0-REG-4 · `registry/resources.py:936-943(UPC_SHEET)+ services/upc_pool.py:27-28(STATUS_CN)` · registry/resources.py:936-943;services/upc_pool.py:27-28

**改动**:(a) UPC_SHEET.columns 六项不变,但注释定口径:**E 列「SKU」从批次 2 起存真 SKU(不透明码),不再存 ASIN**;ASIN 不另投影(要查走登记簿或在线产品总表的来源码列)。理由写明:列名一直叫 SKU,现状存 ASIN 才是错的,改回去是纠正不是变更。(b) services/upc_pool.STATUS_CN 加两项 `"burned_delete": "已烧(删除)"`、`"burned_lock": "已烧(锁死)"`;project_to_sheet(:106-127)已是 `STATUS_CN.get(status, status)`,零代码改动即生效。

**为什么**:sku_plan §3.4 第 8 条与 §8 待决项:不定口径的话 mark_used 改传真 SKU 后 E 列会变成一半 ASIN 一半随机码,而 _mark_upc_conflicts 又按 upc_pool.sku 反查 ⇒ 撞库标记静默半失效。STATUS_CN 是烧号状态的唯一人看面,不加两项则表格显示英文原值。

**测试**:
- tests/test_upc_pricing.py::test_status_cn_covers_every_status(STATUS_CN 键集合 ⊇ 代码里出现过的全部状态字面量,含两个 burned_*)

**验收**:python -m pytest -q tests/test_upc_pricing.py;python cli.py upc_sync --dry-run

#### C0-REG-5 · `registry/resources.py` · 在 FEED_SPEC_VERSIONS 块之前(第 105-107 行的横幅注释之前)新开一节「SKU 编码登记」

**改动**:只加一个 registry 级常量:`SKU_SOURCE_LETTERS = {"amz": "", "match": "", "1688": "", "self": ""}`(四个字母由所有者填,取自 `23456789ABCDEFGHJKMNPQRSTVWXYZ`、互不相同、不助记)。注释三条:①键必须与 services/listing_sources.SOURCE_* 对得上,unknown 不发码不进本表;②字母表、11 位随机长度、查重重试次数**不在这里**,唯一出处是 services/sku_codec(铁律 3 管的是路径/token/表 ID/服务器地址,编码规则不是外部资源);③值为空时 mint 必须抛错而不是回落默认字母——空字母会造出 11 位码,与 12 位形态判据全线错开。

**为什么**:sku_plan §2 明写「registry 常量表 SKU_SOURCE_LETTERS」;把字母表也塞进 registry 会造出第三个出处(schema.sql 的部分索引正则已是第二处,靠守门绑住两处,再多一处就绑不住)。

**测试**:
- tests/test_sku_guard.py::test_source_letters_are_registered_for_every_minting_source
- tests/test_sku_guard.py::test_empty_source_letter_is_a_hard_error(批次 2 后生效)

**验收**:python -m pytest -q tests/test_sku_guard.py -k letter

#### C0-REG-6 · `api/_client.py:232-247(_RATE_BUCKETS 的 feeds.post.* 段)+ api/feeds.py:68-79(_SLICE_LIMITS)` · api/_client.py:232-247;api/feeds.py:68-79

**改动**:**不新增任何 feedType 桶、不新增切片项**,只加注释:批次 3 的 SkuUpdate 走既有 MP_MAINTENANCE(若实测证明最小载荷可行)或既有 MP_ITEM(若必须全量),两者的桶(各 8/hour)与切片限额都已登记。注释写死一条运行纪律:**sku_migrate 与 13:00 的 product_chain 抢同一个 MP_MAINTENANCE 桶**(维护链的标题 provider 恒走 feed),故 sku_migrate 不许与 product_chain 并跑、也不进调度。若实测发现需要另一个 feedType,那是新增登记,必须先在 api/feeds.build_payload 加分支 + _SLICE_LIMITS 加行 + _RATE_BUCKETS 加桶(未登记的 feedType 一律拒绝——api/_client.py:232 的既有设计)。

**为什么**:sku_plan §7 批次 3 节奏按 10/hour × 2000 条/feed 走;桶是按店按 feedType 的跨进程滑动窗口(_is_persistent:窗口 ≥600s ⇒ 落 PG),两条链同时吃同一个桶不会报错,只会让维护链抱锁睡一小时。这条纪律不写进 api 层注释就只活在计划文档里。

**测试**:
- tests/test_rate_bucket.py::test_no_new_feed_bucket_for_sku_update(断言 _RATE_BUCKETS 里没有 SkuUpdate/SKU_UPDATE 之类的新键——反向钉死不自创 feedType)

**验收**:python -m pytest -q tests/test_rate_bucket.py tests/test_feeds.py

#### C0-SCHED-1 · `registry/schedule.py` · 191-207(product_chain 那条 job 的 note 参数)

**改动**:note 末尾追加:「⚠ 2026-09 SKU 改造后 `sources_backfill` 摘要**分两桶**:『旧格式存量』(不报警,随批次 3 收敛到 0)/『新码漏登记』(**报警**——不透明码在架却查不到登记簿行,说明有人绕过 mint 上架,或 mint 与提交之间断了事务)。原来的『非零即报警』作废。」**不新增任何 job、不改 params、不改时间**;sku_migrate 不进 JOBS(手动)。

**为什么**:sources_backfill 的「非零即报警」语义写在它 docstring 的 16-19 行、schedule note 里、README 6.2 那一行三处;新码上线后旧格式存量会长期非零(存量迁移是批次 3 的长活),不分桶就等于把这条报警永久关掉,而且没人会发现。schedule note 是三处里唯一会被渲染进智能体提示词的那一处。

**测试**:
- tests/test_gpt_skill.py::test_repo_copy_matches_the_schedule_table(改 note 后必须重跑 skill_export,否则仓库副本与调度表不一致 ⇒ 红)
- tests/test_readme.py::test_schedule_table_matches_the_registry(只比时间与 label,note 不参与;仍要复核一遍)

**验收**:python cli.py skill_export --dry-run(应报 product_chain.md 与 SKILL.md 两个文件『改动』)→ python cli.py skill_export → python -m pytest -q tests/test_gpt_skill.py tests/test_readme.py

#### C0-SKILL-1 · `skills/walmart-schedule/tasks/product_chain.md + skills/walmart-schedule/SKILL.md` · product_chain.md:16(steps_table 行)、:26(备注行);SKILL.md:23(product_chain 行)

**改动**:**不手改**——这两份是 skill_export 的渲染产物(文件头与 README 都写了「生成物,不要手改」)。改法只有一条:改完 registry/schedule.py 的 note(C0-SCHED-1)与 workflows/sources_backfill.py 的 docstring 首行(若 0b 改了它),跑 `python cli.py skill_export --dry-run` 看差异 → 跑 `python cli.py skill_export` 落盘 → 把改动一起进同一个 PR。⚠ 若 0b 改了 sources_backfill 的 docstring **首行**,product_chain.md:16 那一行也会变,两处必须在同一次生成里落。

**为什么**:services/gpt_skill._one_liner 取 workflow docstring 首行、task_prompt 取 schedule note;test_gpt_skill.py::test_repo_copy_matches_the_schedule_table 会比对仓库副本与现渲染,漂了就红。这是仓里唯一「文档失真会让测试变红」的第二处(第一处是 test_readme)。

**测试**:
- tests/test_gpt_skill.py::test_repo_copy_matches_the_schedule_table
- tests/test_gpt_skill.py::test_no_stale_task_files_left_behind(不新增 job ⇒ 任务文件数应保持 9 个)

**验收**:python cli.py skill_export --dry-run(第二次跑应报『N 个文件与调度表一致,无需改动』)

#### C0-DOC-1 · `docs/db_schema.md` · 161-177(listing_sources 的 sql 块)

**改动**:CREATE TABLE 块加三列 `abandoned_at timestamptz`(NULL=活码)/ `abandoned_reason text` / `replaced_by text`;索引注释加两条(listing_sources_live_uidx 与 listing_sources_opaque_sku_uidx,各写清条件与「存量有重复时 db_init 条件跳过并告警,查 catalog.v_listing_sources_dupe_live」「字符集与 sku_codec 逐字一致」)。段末三句钉死:①行永不 DELETE;②除 sku_codec.abandon 外全仓不许 UPDATE 本表;③`abandoned_at IS NULL` 在消费方 SQL 里只允许出现在三个文件,守门在 tests/test_sku_guard.py。

**为什么**:docs/db_schema.md 是表结构唯一事实来源(该文档第 3 行原话),refdata/schema.sql 是它的同步产物;不同步就等于下一个 AI 读到的表结构是旧的。

**测试**:
- tests/test_sku_schema.py::test_db_schema_doc_mentions_the_three_columns(断言三个列名 + 两个索引名都在——两处同名对象必漂,本仓已有先例)

**验收**:python -m pytest -q tests/test_sku_schema.py;人工对读 docs/db_schema.md 161-177 与 refdata/schema.sql 213-236

#### C0-DOC-2 · `docs/db_schema.md` · 179-191(upc_pool sql 块);416(orders.order_lines 那一行)

**改动**:(a) upc_pool 的 status 注释补两个值 `burned_delete` / `burned_lock`,并写清与 conflict 的分工(conflict = 全站已存在;burned_* = 跟着一个已弃的码下岗;分开是为了 pool_stats 与投影分得清)。(b) order_lines 行里 **`asin`** 的说明改口径:「由 `order_asin_normalize` 按 `services/sku_asin.resolve`(**登记簿优先,提不出再按形态**)补填,**提不出留 NULL**;对已弃码行照常返回 source_key——订单/售后带旧码回来必须查得到」。

**为什么**:sku_plan §6 与 synthesis rule §6;order_lines.asin 是分配引擎销量/退货率维度的唯一入口(该行原文已写「不许拿 sku 原文当 asin」),口径写模糊会让 0b 的实现者以为 resolve 要过滤弃码行。

**测试**:
- tests/test_sku_schema.py::test_order_lines_asin_doc_says_registry_first(断言该行含「登记簿优先」与「已弃码」两个词)

**验收**:python -m pytest -q tests/test_sku_schema.py

#### C0-DOC-3 · `docs/db_schema.md` · 781-821(## catalog.audit_listing_conflicts 节);在 800 行「为什么本视图不依赖 product_risk」段之后插入

**改动**:新增小节「2026-09 SKU 改造:身份关联改经登记簿」:说明 live_rejected CTE 从 `p.asin = w.sku` 改成 `LEFT JOIN listing_sources ls ... p.asin = coalesce(ls.source_key, w.sku)`。三句必须写:①LEFT JOIN 不可改 JOIN(未登记的在架行要按旧口径回落,否则 sources_backfill 跑之前的新行静默消失);②不加 abandoned_at 过滤(消费方契约);③LATERAL 那一段一字不动,`coalesce(ev.asin, ev.sku)` 与 product_events_identity_idx 逐字一致的纪律**依然有效**——两个 coalesce 是两件事,别顺手统一。

**为什么**:本节已经记过一次「首版查询挂死」的教训,改这个视图的人一定会先读它;第 ③ 条是最容易在改动中被顺手破坏的地方(两个 coalesce 长得像)。

**测试**:
- tests/test_problem_scan.py::test_conflicts_view_join_matches_its_index(C0-DDL-5 已补的两条断言就是这一节的可执行版)

**验收**:python -m pytest -q tests/test_problem_scan.py

#### C0-DOC-4 · `docs/feishu_tables.md` · 61(在线产品总表行)、63(上架表行)、71(UPC池行)、72(维护记录行)、75(停用删除表行)、105-109(销售订单程序拥有列)、127-130(售后订单程序拥有列)、52-78 表格

**改动**:六处改:①:63 上架表「21 列 A~U」→「22 列 A~V」,补 `V=SKU`(提交时与 K/L/M 同一次写回;**存量行 V 为空 ⇒ 回落 B 列 ASIN**,回执反哺/heal_unknown/sku_locked_heal 从此读 `r["sku"] or r["asin"]`);列序唯一出处仍是 registry.LISTING_SHEET.columns。②:61 在线产品总表列清单末尾加「来源码」。③:71 UPC 池 E 列口径定稿(批次 2 起存真 SKU,ASIN 不另投影)。④:105-109 与 :127-130 各加「ASIN」,并各补一句「加列 ⇒ 行指纹全变 ⇒ 下一次 push 全量重推 90 天窗口,是预告不是故障」。⑤:72 与 :75 各补一句「SKU 列从此可能是随机码,查产品先经登记簿或来源码列」(手动通道全格式通吃,程序不改)。⑥表格区补一条建列纪律:新列由所有者在飞书先建,建之前用 list_fields 确认没有同名人工列(有的话程序一登记就开始覆盖它)。

**为什么**:sku_plan §3.5 六张表全在这一份文档里登记;飞书列是运营的唯一操作面,建列顺序(人建列 → 程序登记常量)搞反会覆盖人工列,这是 2026-08-17 已经吃过一次亏的形态(该文档 :77 有原话「人在维护的表除非明确要求一律只读」)。

**测试**:
- tests/test_sku_guard.py::test_feishu_doc_lists_the_new_columns(断言「22 列 A~V」「来源码」「ASIN」三处字样在——文档失真不报错,这条就是它的报错)

**验收**:python -m pytest -q tests/test_sku_guard.py -k feishu_doc

#### C0-DOC-5 · `docs/api_blueprint.md` · 318-323(§8 第 4 条 retire 语义)与 366-367(遗留问题 1、2);§8 内新增一小条

**改动**:三处更正,全部带官方 URL:①:318-323 保留「官方 API 层面 retire = 单品 DELETE /v3/items/{sku}」,把「API 侧无 reactivate 端点」改成「**无专用 reactivate 端点;官方 unretire = 把 end date 改成未来**:『Set the end date to the past to remove the item from sale… To unretire an item, change the end date to the future. Note that this API only retires the item, it does not delete it』(https://developer.walmart.com/us-marketplace/docs/item-inventory);Seller Center 对应 Site End Date 改未来;旧仓实证形态 = MP_MAINTENANCE {sku, productIdentifiers, endDate}(legacy_survey.md:1375)」,并补「退役 item 的 SKU 与 Product ID 不能给别的 item 用(https://developer.walmart.com/ca-marketplace/reference/retireanitem)——这正是本仓『码复用到显式弃码』的官方依据」。②:367 遗留 2 **结案**:『wait for a 48-hour interval, and then set up a new item… using the same or a different SKU number』出自 https://developer.walmart.com/us-marketplace/docs/update-my-existing-items,**是 Marketplace 文档不是 1P**;旧写法「仅 1P」是错的;GTIN 24h 后可复用。补一句:本仓策略仍是 DELETE 后新码新 UPC,官方允许复用不等于我们要复用。③:366 遗留 1 保留待实测,补「批次 2 前单品实测清单见 docs/sku_plan.md §8」。④§8 新增小条「SkuUpdate(改 SKU)」:官方支持、匹配键 = Product ID、评价评分保留、WFS 不可改;API 侧对应 feed 里的 SkuUpdate 属性(CA 文档明文,US 文档指向 Maintain an item);**本地 spec 里 SkuUpdate 在哪份、最小载荷是什么,批次 3 前必须实测**。

**为什么**:蓝图是「写沃尔玛调用前必查」的定稿件(CLAUDE.md:90-92);两处错误的官方结论会直接误导批次 2/3 的实现者——「无 reactivate」会让人以为停用不可逆从而选错弃码策略,「仅 1P」会让人以为 DELETE 后同 SKU 永不可用。

**测试**:
- tests/test_sku_guard.py::test_blueprint_retire_wording_is_corrected(断言 :318 段落里不再出现『API 侧无 reactivate 端点』这句原文)

**验收**:grep -n 'API 侧无 reactivate 端点' docs/api_blueprint.md(应无输出);grep -n '仅 1P' docs/api_blueprint.md(应无输出或已标『已结案』)

#### C0-DOC-6 · `docs/conventions.md` · 文件末尾(200 行之后)新增「## 九、SKU 身份与码的寿命(2026-09-02 定稿)」

**改动**:八小段,只写规则与依据:①身份唯一出处 = catalog.listing_sources 的 (店, SKU)→(来源类型, 来源码);SKU = 12 位 `<来源字母><11 位随机>`,字母表 `23456789ABCDEFGHJKMNPQRSTVWXYZ`;抽码在 list_new._prep_rows 预备期,**不能在 _one_store 里**(串行补试会重跑 _one_store,抽新码 ⇒ 载荷不再一字不差 ⇒ payload_key 在途防重不命中 ⇒ 双上架)。②码的寿命 = 沃尔玛侧那条 (店, SKU) 记录对我们还有用的寿命;abandoned_at IS NULL 的行叫活码;登记簿行永不 DELETE。③三个同名异义永远别混:码弃用 / 沃尔玛 lifecycle RETIRED / product_clear「停用」。④弃码只有四个点(delete_verified 观测 / SKU_LOCKED RETIRE 回执成功+冷却期满,唯一绑回执的点,因锁死 SKU 可能从未进过 walmart_items / UPC 撞库 0101119,决策 B 未拍板默认换 / SkuUpdate 经观测确认);**其余一切下架都不弃码**,理由写清(沃尔玛侧记录仍在、仍绑着我们的 UPC,抽新码 = 同店两条同内容记录 + 白烧一个 UPC)。⑤不信回执信观测:弃码点 1 绑 catalog_sync 观测而非 DELETE 回执——「回执成功但后台没删」是所有者实证过的故障模式。⑥单一实现路径:弃码只有 sku_codec.abandon,发码只有 mint,反查只有 sku_asin.resolve/resolve_many(登记簿优先,对已弃码行照常返回 source_key)。⑦消费方契约:`abandoned_at IS NULL` 只允许出现在三处 + schema.sql 的部分索引,守门在 tests/test_sku_guard.py。⑧三个未拍板决策 A/B/C 各写默认假设与「选另一个会怎样」,指向 docs/backlog.md 待办。

**为什么**:CLAUDE.md 是常驻上下文只留规则,conventions 是展开层(该文件第 2-4 行原话)。SKU 这一轮引入了三个同名异义与一个「四个点、只有四个」的封闭集,不落到 conventions 里就只活在计划文档,而计划文档不是每次会话必读。

**测试**:
- tests/test_sku_guard.py::test_conventions_has_the_sku_section(断言含「## 九」与「四个弃码点」「码弃用 ≠」两个关键短语——防这一节被后来的瘦身顺手删掉)

**验收**:python -m pytest -q tests/test_sku_guard.py -k conventions

#### C0-DOC-7 · `CLAUDE.md` · 工程规范节:60 行(services 查重那条)之后、61 行(飞书字段名那条)之前插入;目录速查 74 行(services/ 那行);78-83 的 docs 清单

**改动**:(a) 插入一条(两行以内,与周围同密度):「- **SKU 身份只从两个积木出**:发码/弃码 `services/sku_codec`(mint / abandon,四个弃码点,`abandoned_at IS NULL` 只许出现在三处),SKU→源头码反查 `services/sku_asin.resolve`;**product_clear 停用不弃码**,三个同名异义(码弃用 / lifecycle RETIRED / 停用)别混(§九)。」(b) 74 行 `services/` 的括注补 `sku_codec(发码弃码) sku_asin(反查)`。(c) 78-83 的 docs 清单加 `sku_plan.md(SKU 编码与影响范围)`。

**为什么**:CLAUDE.md 是每次会话必读的那一份;SKU 最容易踩的三条规矩(单一出处、四个点、三个同名异义)不在这里,下一个 AI 会在 product_clear 里顺手加一行 abandon。

**测试**:
- tests/test_sku_guard.py::test_claude_md_points_at_the_sku_rules(断言含 `sku_codec` 与「不弃码」两个串)

**验收**:python -m pytest -q tests/test_sku_guard.py -k claude_md

#### C0-DOC-8 · `README.md` · 12(工作流数)、275(sources_backfill 行)、330-336(6.4 上架域表格)、602(测试数)、612-631(文档索引表)

**改动**:①:12 `**76 条工作流**` → `**77 条工作流**`(**只在批次 3 加了 workflows/sku_migrate.py 的那个 PR 里改**;批次 0/1/2 不新增工作流,这一行不动)。②:275 sources_backfill 描述末尾改成「…摘要**分两桶**:旧格式存量(不报警)/ 新码漏登记(**报警**)」,与 C0-SCHED-1 的 note 同口径。③6.4 上架域表格追加一行:`| \`sku_migrate\` | 危 一 | 存量 SKU 改码(官方 SkuUpdate):mint 新码 → 先落库(旧行 replaced_by=pending)→ 提交 feed → **观测确认**才 abandon 旧行;失败走 rolled_back。**不进调度、手动一店一批**,与 13:00 product_chain 抢同一个 MP_MAINTENANCE 桶,不许并跑 |`。④:602 `# 1726 passed` 改成实跑数字(每批 PR 都改)。⑤文档索引「当前有效」表加两行:`docs/sku_plan.md` 与 `docs/sku_workplan.md`。

**为什么**:tests/test_readme.py 有四条会红的断言:工作流数(:47-50)、每条工作流必须在 README 出现(:31-34)、README 不许列不存在的工作流(:37-45)、危险工作流必须带「危」标(:78-88)。sku_migrate 一旦落地,不改 README 就是三条同时红。

**测试**:
- tests/test_readme.py::test_workflow_count_matches
- tests/test_readme.py::test_every_workflow_appears_in_the_readme
- tests/test_readme.py::test_dangerous_workflows_are_marked(建议把 'sku_migrate' 加进该用例的抽查元组——它会写沃尔玛且改码不可逆)

**验收**:python -m pytest -q tests/test_readme.py

#### C0-DOC-9 · `docs/feed_closure_audit.md + docs/backlog.md + docs/plan.md` · feed_closure_audit.md:68 之后(## 二 四段链条之后);backlog.md:110-121(## 五、决策未决汇总);plan.md 第 10 行 listing 那一行的备注末尾

**改动**:①feed_closure_audit.md 新增「六、SKU 弃码与 feed 闭环的关系(2026-09)」:弃码**不是**闭环的第五段;四个弃码点里只有 SKU_LOCKED 那一个绑回执,理由写清(锁死的 SKU 可能从未进过 walmart_items,无观测可等,是唯一例外);并点名 feed_track 落 retire_feed_success / delete_feed_success 之后登记簿 abandoned_at **仍为 NULL**,守门反向钉死。②backlog.md「五、决策未决汇总」追加三条(决策 A/B/C,各写默认假设与阻塞的批次)+ 两条实测待办(SkuUpdate 三件事、批次 2 前单品实测七项,清单在 sku_plan §8)。③plan.md listing 那一行备注追加「**2026-09 SKU 改造**(docs/sku_plan.md):SKU 从 ASIN 改 12 位不透明码,分五批(0a/0b/1/2/3),存量走 SkuUpdate 迁移;波及面 §3 全景」。

**为什么**:三份都是过程档案与待办总账,不写就等于所有者拍板前这几条会从项目记忆里掉出去;feed_closure_audit 那一节尤其重要——它是仓里唯一系统盘过「哪些动作绑回执、哪些绑观测」的文档,SKU_LOCKED 那个例外必须在那里留档,否则下一个人会把四个点统一成「都绑回执」。

**测试**:
- 无自动化(散文文档);验收靠 PR review 逐条对读

**验收**:grep -n 'SKU_LOCKED' docs/feed_closure_audit.md(有输出);grep -n '决策 A' docs/backlog.md(有输出)

#### C0-TEST-1 · `services/product_events.py:95-121(常量区 + EVENTS)+ tests/test_product_events_registry.py:48-57` · services/product_events.py:95-121;tests/test_product_events_registry.py:48-57

**改动**:批次 2 同批加两个事件码:常量区加 `SKU_ABANDONED = "sku_abandoned"` 与 `SKU_REPLACED = "sku_replaced"`,并加进 EVENTS 集合(113-121);test_constants_match_ledger_strings 追加两条断言。detail 结构约定写进常量上方注释:abandoned 记 `{old_sku, reason, burned_upcs}`;replaced 记 `{old_sku, new_sku, feed_id}`——product_risk 时间线要能回答「这个 ASIN 在这家店用过哪些码、为什么换」。

**为什么**:record_many 对未登记码抛错(:156-159),不登记就是批次 2 一提交就炸;而 detail 结构不约定,时间线里会出现三种形状的同名事件。EVENTS 是代码级唯一出处(2026-08-11 起),文档不复述。

**测试**:
- tests/test_product_events_registry.py::test_constants_match_ledger_strings(补两行)
- tests/test_product_events_registry.py::test_record_many_accepts_every_registered_code(自动覆盖新码,不用改)
- tests/test_sku_guard.py::test_abandon_records_a_ledger_event(批次 2 后:桩 conn,断言 abandon 一次 UPDATE + 一次 record_many(sku_abandoned))

**验收**:python -m pytest -q tests/test_product_events_registry.py

#### C0-TEST-2 · `tests/test_sku_asin.py` · 全文件(13-37 行是形态用例)

**改动**:批次 0a 同批补四组用例,构成「存量一个字节不变」的可执行证明:(a) `test_opaque_code_is_not_an_asin`——12 位不透明码经 extract_asin 必返 None、经 classify 落 'other';(b) `test_resolve_matches_extract_asin_on_every_legacy_shape`——三种存量形态(裸 ASIN / 三段式 / 纯数字倒查)经新的 `resolve(conn, store, sku)` 与旧的 `extract_asin(sku)` **逐字相同**(纯数字那一跳用桩 conn);(c) `test_resolve_finds_opaque_codes_via_the_registry`;(d) `test_resolve_still_answers_for_abandoned_rows`——已弃码行照常返回 source_key。

**为什么**:批次 0a 的「零行为变化」论证整个压在 (b) 上;(d) 是消费方契约里最容易被实现者顺手加过滤条件破坏的那一条(订单/售后带旧码回来必须查得到)。

**测试**:
- 上述四条即为要加的测试
- tests/test_order_asin_normalize.py::test_rules_are_not_reimplemented_here(既有守门必须仍绿——登记簿那一跳只准放在 services/sku_asin,不能放工作流)

**验收**:python -m pytest -q tests/test_sku_asin.py tests/test_order_asin_normalize.py

#### C0-TEST-3 · `tests/(夹具 sku=asin 同值的清单与处理原则)` · test_list_new.py:570,689;test_sku_locked_heal.py(9 处 ASIN 字面量);test_claims.py:372;test_alloc_plan.py:122;test_alloc_push.py(2 处);test_sources_backfill.py:42;test_product_ingest.py:603;test_order_audit.py:1334;test_alloc_audit.py:91-105;test_blacklist.py:116,177;test_order_asin_normalize.py 全篇;test_problem_scan.py:301;test_blacklist_push.py:164;test_risk_trace.py:123

**改动**:**先分三类再动手**:①钉「按形态可解析」的(test_sku_asin / test_sources_backfill:42 / test_product_ingest:603 / test_order_audit:1334 / test_alloc_audit:91-105 / test_order_asin_normalize / test_blacklist:116,177)——**不删**,加一组不透明码的平行用例:同一断言换成 12 位码走登记簿路径,原用例保留证明存量不变。②钉 SQL 文本的(test_problem_scan:301 / test_blacklist_push:164 / test_risk_trace:123)——只改被改动的那一句断言,其余(尤其表达式索引逐字一致那条)一个字不动。③夹具里 sku=asin 只是图省事的(test_list_new:570,689 / test_sku_locked_heal / test_claims:372 / test_alloc_plan:122 / test_alloc_push)——**必须改成 sku ≠ asin**(sku 用 12 位不透明码,asin 用 B0…),并在夹具旁写一行注释「sku ≠ asin 是本轮的默认世界」。**机械检出法**:`grep -rn 'B0[0-9A-Z]\{8\}' tests/*.py` 得 40 个文件,逐个人眼判断该 ASIN 是不是被同时当 SKU 用(判据:同一 dict 里 sku 与 asin 取同值,或 walmart_items 夹具的 sku 字段被填了 ASIN)。**判不准就归类 ①**(加平行用例、不改原值)——conventions §五「判不准就判活」的同源纪律。

**为什么**:第 ③ 类是最危险的一种:它让「把 sku 当 asin 用」的 bug 在测试里恒绿,而本轮全部十四处消费方收口的正确性恰恰要靠夹具区分这两个值才能被测出来。第 ① 类反过来:删掉它们就失去「存量一个字节不变」的证据。

**测试**:
- 按上面三类逐条处理;可选加严 tests/test_sku_guard.py::test_no_fixture_uses_the_same_value_for_sku_and_asin(AST 扫 tests/ 里同时含 'sku' 与 'asin' 两键且值相等的 dict 字面量,白名单登记故意为之的存量用例)。**默认不加这一条**——射程含 tests/ 会让守门自己变成维护负担,先靠机械检出法 + review。

**验收**:python -m pytest -q(全绿);再跑一次 `grep -rn 'B0[0-9A-Z]\{8\}' tests/*.py | wc -l` 与改前对照,变化的行逐条能说出属于 ①②③ 哪一类

#### C0-TEST-4 · `tests/test_sku_schema.py` · 新文件

**改动**:新建「DDL 与文档一致性」测试文件(与 test_sku_guard.py 分工:那边守代码边界,这边守 schema.sql ↔ db_schema.md ↔ 索引名/表达式三处一致)。用例见 C0-DDL-1..6 与 C0-DOC-1..2 各自 tests 栏;另加两条通用的:`test_schema_sql_is_idempotent_by_construction`(文本轨断言本轮新增的每条 DDL 都带 IF NOT EXISTS 或包在 DO 块里;DROP VIEW+CREATE VIEW 例外,登记在小白名单并写理由)与 `test_every_new_object_name_appears_in_the_doc`(四个新对象名 listing_sources_live_uidx / listing_sources_opaque_sku_uidx / v_listing_sources_dupe_live / v_order_line_dupes 在 docs/db_schema.md 里都找得到)。

**为什么**:db_init 幂等是硬约束,而幂等性目前只靠人记得写 IF NOT EXISTS(2026-08-13 已经因为平铺 AND 的 DROP 块炸过一次,schema.sql:590-596 有原话);文本轨守门是最便宜的复发防线。schema.sql 与 db_schema.md 两处必漂,同样只能靠断言绑住。

**测试**:
- 自身即测试

**验收**:python -m pytest -q tests/test_sku_schema.py;python cli.py db_init && python cli.py db_init

### 新模块

- `tests/test_sku_guard.py`
  - API:无公开 API(纯测试文件)。五张模块级白名单:_EXTRACT_ASIN_OK: dict[tuple[str,str], str] / _ASIN_SHAPED_REGEX_OK: dict[str,str] / _SKU_ASIN_EQUALS_OK: dict[str,str] / _ABANDONED_FILTER_OK: frozenset[str] / _ABANDON_CALLERS_OK: dict[tuple[str,str], str],外加硬清单 _MUST_NOT_ABANDON: tuple[str, ...] 与 _FIELD_LITERAL_OK: dict[str,str]。取材辅助 _prod_files() -> list[tuple[str, Path]] / _calls_with_scope(tree, name) -> list[tuple[ast.Call, str]] / _module_of(rel),形状照抄 tests/test_feishu_guard.py:99-186,以免两份守门各写一套 AST 遍历。
  - docstring 规则:首段写「为什么要守」:sku_plan §2 问 2 的结论——全仓没有一处会报错,全部是静默失效(摘要看起来正常、功能悄悄没了),这是最危险的形态;守门不测行为,只守边界。第二段逐条列七道守门与它们各自防的那个具体事故形态(去重闸失效 → 同店重复上架烧 UPC 烧配额;黑名单键被灌随机码 → 违禁品拦不住;订单审核每一单判待人工 → 审核链事实停摆;product_clear 顺手 abandon → 每次停用烧一个 UPC 且永久失去可恢复性)。第三段写白名单纪律,逐字照搬 test_feishu_guard.py:42-47 的口径(「要改守门,先改这里,别删断言;例外必须显式登记、写得出理由;空表 = 当前零例外,不是还没启用」),并补一条本轮特有的:**每条白名单的理由里必须写清它属于哪个批次、收口后删**——这张表是会缩短的清单,归零那天波及面才算做完。末段给分工指针:与 tests/test_sku_schema.py 的分工(那边守 DDL 与文档一致性,这边守代码边界),规则全文在 docs/conventions.md §九。
- `tests/test_sku_schema.py`
  - API:无公开 API。模块级常量 _SCHEMA = (ROOT/'refdata/schema.sql').read_text() 与 _DOC = (ROOT/'docs/db_schema.md').read_text();辅助 _object_ddl(name) -> str(从 schema.sql 里切出某个对象的 DDL 文本段,给逐条断言用)。
  - docstring 规则:首段写这份文件守的是三处必漂:refdata/schema.sql(可执行产物)↔ docs/db_schema.md(事实来源)↔ 索引名与表达式(与代码里的正则、与查询里的表达式必须逐字一致)。第二段点名两条已发生过的事故形态作为立此文件的理由:①2026-08-06 生产实证——旧库表已存在时 CREATE IF NOT EXISTS 跳过、列靠 ALTER 补,先建索引会 UndefinedColumn;②2026-08-13 生产实证——平铺 AND 的 DROP 块首跑成功、重跑必炸 UndefinedTable,db_init 被卡死。第三段写它**不做**什么:不连库、不验证语义正确性(那要真库),只做文本一致性——所以它便宜到可以每次 CI 跑。
- `tests/test_catalog_health.py`
  - API:无公开 API。桩 _Conn(照抄 tests/test_product_events_registry.py:14-25 的形状,cursor() 返回可迭代固定行的 _Cur)。
  - docstring 规则:首段写清 catalog_health 的 F 段为什么必须有:db_init 只执行 SQL 并返回固定摘要(workflows/db_init.py:38-56),RAISE WARNING 进不了摘要,所以「活码唯一索引到底建成没有」这件事在生产上没有任何发现渠道——而它没建成的后果是并发双 mint 给同一 (店, 来源码) 发两个码,静默。第二段写测试口径:只测摘要文本里该出现的数字与 ⚠ 标记,不测 SQL 本身(那要真库)。

### DDL

```sql
-- ① 登记簿三列(批次 0a;插在 refdata/schema.sql 的 221 与 222 之间,必须先于下面的索引)
-- 列名用 abandoned 不用 retired:码弃用 ≠ 沃尔玛 lifecycle=RETIRED ≠ product_clear「停用」,三个同名异义。
-- abandoned_at IS NULL 的行叫「活码」;弃码只有一个实现 services/sku_codec.abandon,四个弃码点见 docs/conventions.md §九。
-- 本表行永不 DELETE;除 abandon 外全仓不许 UPDATE 本表(守门 tests/test_sku_guard.py)。
ALTER TABLE catalog.listing_sources ADD COLUMN IF NOT EXISTS abandoned_at     timestamptz;
ALTER TABLE catalog.listing_sources ADD COLUMN IF NOT EXISTS abandoned_reason text;
ALTER TABLE catalog.listing_sources ADD COLUMN IF NOT EXISTS replaced_by      text;
```
```sql
-- ② 重复活行体检视图(必须先于 ③ 的 DO 块;CREATE OR REPLACE 而非 DROP+CREATE ——
--    PG 不允许 DROP 有依赖者的视图,DROP+CREATE 会给 db_init 埋「第二次跑就报错」的雷,schema.sql:500-503 已记过这个教训)
CREATE OR REPLACE VIEW catalog.v_listing_sources_dupe_live AS
  SELECT store, source_type, source_key,
         count(*) AS n,
         array_agg(sku ORDER BY created_at) AS skus
  FROM catalog.listing_sources
  WHERE abandoned_at IS NULL AND source_key IS NOT NULL
  GROUP BY 1, 2, 3
  HAVING count(*) > 1;
```
```sql
-- ③ 活码部分唯一索引:并发双 mint 的**唯一**拦截。
-- ⚠ 条件建而不是裸建:存量登记簿是否已有重复活行未连库核对过(match 行 source_key 是 GTIN,
--    同店同 GTIN 重上过就会有两个 SKU 行)。裸建会让 db_init 在生产上一次性炸死,而 db_init
--    幂等可重跑是硬约束(conventions §五:未连库核对之前一律不执行)。
-- ⚠ 没建成不是无害:catalog_health 的 F 段会把「索引在不在」带 ⚠ 报出来。
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM catalog.v_listing_sources_dupe_live) THEN
    EXECUTE 'CREATE UNIQUE INDEX IF NOT EXISTS listing_sources_live_uidx '
            'ON catalog.listing_sources (store, source_type, source_key) '
            'WHERE abandoned_at IS NULL AND source_key IS NOT NULL';
  ELSE
    RAISE WARNING 'listing_sources_live_uidx 未建:存量有重复活行,查 catalog.v_listing_sources_dupe_live';
  END IF;
END $$;
```
```sql
-- ④ 不透明码全局唯一(**含已弃码行** —— 跨店永不复用,码一旦发出就永久占着这个串)。
-- ⚠ 字符集必须与 services/sku_codec._ALPHABET **逐字一致**(同 product_events_identity_idx 的表达式索引纪律,
--    schema.sql:188 已有同款先例);改一边必须改另一边,守门 tests/test_sku_guard.py::test_opaque_sku_regex_matches_the_codec_alphabet。
-- 30 符号(剔 0/O、1/I/L、U)天然把裸 ASIN(10 位)、三段式(含 '-')、纯数字 item_id(含 0/1)排除在索引条件外;
-- 全表 UNIQUE(sku) 建不起来:存量 sku=asin 时同一 ASIN 在两家店是同一串,跨店重复是既成事实。
CREATE UNIQUE INDEX IF NOT EXISTS listing_sources_opaque_sku_uidx
  ON catalog.listing_sources (sku)
  WHERE sku ~ '^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{12}$';
```
```sql
-- ⑤ 审核冲突面视图改经登记簿反查(批次 0a;替换 refdata/schema.sql:521-551 里 live_rejected 那段 CTE)。
-- ⚠ LEFT JOIN 不可改 JOIN:未登记的在架行必须按旧口径回落到 w.sku,否则 sources_backfill 跑之前的新行静默消失。
-- ⚠ 不加 abandoned_at 过滤:消费方契约(除 mint / 去重闸 / alloc_push 三处外一律不按弃码过滤)。
-- ⚠ 下面 LATERAL 段的 coalesce(ev.asin, ev.sku) 与 product_events_identity_idx 逐字一致,一个字都不许动 —— 两个 coalesce 是两件事。
DROP VIEW IF EXISTS catalog.audit_listing_conflicts;
CREATE VIEW catalog.audit_listing_conflicts AS
  WITH live_rejected AS (
      SELECT w.store, w.sku, w.published_status, w.last_seen_at,
             p.asin, p.audit_status, p.audit_reason, p.audited_at, p.audit_version
      FROM catalog.walmart_items w
      LEFT JOIN catalog.listing_sources ls
             ON ls.store = w.store AND ls.sku = w.sku
      JOIN catalog.products p
             ON p.marketplace = 'US' AND p.asin = coalesce(ls.source_key, w.sku)
      WHERE w.missing_since IS NULL
        AND p.audit_status = 'rejected'
  )
  -- …以下 SELECT 与 LEFT JOIN LATERAL 段与现状(schema.sql:534-551)逐字相同…
  ;
```
```sql
-- ⑥ 批次 3 的订单双算体检视图(纯只读)。改码后若沃尔玛对**改码前的 PO** 返回新码,
--    会被当成新行插入(行身份 = sha256(PO+SKU),旧行不删)⇒ 同一笔货双算,而订单侧没有任何现成告警。
CREATE OR REPLACE VIEW orders.v_order_line_dupes AS
  SELECT store, po_id, line_number,
         count(DISTINCT order_line_id) AS n,
         array_agg(DISTINCT sku)       AS skus
  FROM orders.order_lines
  WHERE line_number IS NOT NULL
  GROUP BY 1, 2, 3
  HAVING count(DISTINCT order_line_id) > 1;
```
```sql
-- ⑦ upc_pool:**无 DDL 变更**。status 列没有 CHECK 约束(已核 refdata/schema.sql:244-255),
--    新增取值 burned_delete / burned_lock 只需改注释 + services/upc_pool.STATUS_CN 登记。
--    不复用 conflict 是因为语义不同(conflict = 全站已存在;burned_* = 跟着一个已弃的码下岗),
--    混在一起之后 upc_pool_status_idx 分组的 pool_stats 报表再也分不开。
```

### 文档同步

- docs/db_schema.md:161-177 — listing_sources 三列 + 两个新索引 + 体检视图 + 三条纪律(行永不 DELETE / 只有 abandon 能 UPDATE / abandoned_at IS NULL 只许三处)
- docs/db_schema.md:179-191 — upc_pool status 补 burned_delete / burned_lock 及其与 conflict 的分工(并写明无 DDL 变更这件事)
- docs/db_schema.md:416 — orders.order_lines 的 asin 列口径改「按 services/sku_asin.resolve,登记簿优先;对已弃码行照常返回 source_key」
- docs/db_schema.md:781-821 — audit_listing_conflicts 节补「2026-09 SKU 改造:身份关联改经登记簿」小节(LEFT JOIN 不可改 JOIN / 不加弃码过滤 / LATERAL 那个 coalesce 别顺手统一)
- docs/db_schema.md — 新增 catalog.v_listing_sources_dupe_live 与 orders.v_order_line_dupes 两个体检视图的登记
- docs/feishu_tables.md:61 — 在线产品总表列清单加「来源码」
- docs/feishu_tables.md:63 — 上架表 21 列 A~U → 22 列 A~V,补 V=SKU 与「存量 V 为空回落 B 列」
- docs/feishu_tables.md:71 — UPC 池 E 列「SKU」口径定稿(批次 2 起存真 SKU,ASIN 不另投影)
- docs/feishu_tables.md:105-109 与 127-130 — 销售/售后订单「程序拥有的列」各加 ASIN,并各补「加列 ⇒ 指纹全变 ⇒ 下一次 push 全量重推 90 天窗口,是预告不是故障」
- docs/feishu_tables.md:72、75 — 维护记录 B 列与停用删除表 SKU 列补注「从此可能是随机码,查产品先经登记簿或来源码列」
- docs/feishu_tables.md:52-78 表格 — 补建列纪律:新列由所有者先建,建之前用 list_fields 确认无同名人工列
- docs/api_blueprint.md:318-323 — §8 第 4 条 retire 语义更正:「API 侧无 reactivate 端点」→「无专用端点;官方 unretire = end date 改未来」(item-inventory 原句 + URL),补「退役 item 的 SKU/Product ID 不能给别的 item 用」(CA retireanitem)
- docs/api_blueprint.md:367 — 遗留 2 结案:DELETE 后 48h 可用同一或不同 SKU 重建,出自 update-my-existing-items,**是 Marketplace 文档不是 1P**;并注明本仓策略仍是新码新 UPC
- docs/api_blueprint.md:366 — 遗留 1 补「批次 2 前单品实测清单见 docs/sku_plan.md §8」
- docs/api_blueprint.md §8 — 新增「SkuUpdate(改 SKU)」小条:官方支持、按 Product ID 匹配、评价评分保留、WFS 不可改;本地 spec 里在哪份 + 最小载荷,批次 3 前必须实测
- docs/conventions.md — 新增「## 九、SKU 身份与码的寿命(2026-09-02 定稿)」八小段(见 C0-DOC-6)
- CLAUDE.md:60 后 — 工程规范新增一条 SKU 规则;:74 目录速查补 sku_codec / sku_asin;:78-83 docs 清单加 sku_plan.md
- README.md:12 — 工作流数 76 → 77(**只在批次 3 加 sku_migrate 的那个 PR 里改**)
- README.md:275 — sources_backfill 描述改「摘要分两桶:旧格式存量(不报警)/ 新码漏登记(报警)」
- README.md:330-336(6.4 上架域)— 批次 3 追加 sku_migrate 行,标「危 一」,写明不进调度、与 product_chain 抢 MP_MAINTENANCE 桶不许并跑
- README.md:602 — 测试数 1726 改实跑值(每批 PR 都改)
- README.md:612-631 — 文档索引加 docs/sku_plan.md 与 docs/sku_workplan.md
- docs/feed_closure_audit.md:68 后 — 新增「六、SKU 弃码与 feed 闭环的关系」:弃码不是闭环第五段;四个点里只有 SKU_LOCKED 绑回执(唯一例外);feed_track 落 retire/delete_feed_success 后 abandoned_at 仍为 NULL
- docs/backlog.md:110-121 — 追加决策 A/B/C 与两组单品实测待办,各写默认假设与阻塞批次
- docs/plan.md listing 那一行 — 备注追加「2026-09 SKU 改造五批 + 存量 SkuUpdate 迁移,全景 docs/sku_plan.md」
- skills/walmart-schedule/SKILL.md:23 与 tasks/product_chain.md:16,26 — **不手改**,由 python cli.py skill_export 重新渲染并进同一个 PR

### 守门测试

- tests/test_sku_guard.py::test_extract_asin_is_only_called_from_the_whitelist — AST 扫全部生产文件对 extract_asin 的调用(含属性调用与 from-import 后的裸名),(文件, 所在函数) 不在 _EXTRACT_ASIN_OK 即红;白名单按现状预填 12 处待收口 + 3 处永久豁免,每条写清批次归属与「收口后删」
- tests/test_sku_guard.py::test_no_new_asin_shaped_regex_outside_the_whitelist — 扫 ASIN 形态正则字面量三种写法;守的是 services/order_audit.py:358-361 那种**不调 extract_asin、自己手写正则**的形态(sku_plan §3.3 明写它「最容易漏」)
- tests/test_sku_guard.py::test_sku_equals_asin_hard_joins_stay_in_the_whitelist — 扫 .py 与 .sql 里四种硬等号;**先剔掉两边都是 sku 的合法 JOIN**,否则会红在 12 个无辜的地方而被整条注释掉;现状预填六处(maintenance_intents ×4、product_audit:411、schema.sql:527)
- tests/test_sku_guard.py::test_sku_to_sku_joins_are_not_flagged — 守门自身的防误报用例:用内联 SQL 样例断言 `ON ls.store = w.store AND ls.sku = w.sku` 不被判红
- tests/test_sku_guard.py::test_abandoned_is_null_appears_in_exactly_three_places — `abandoned_at IS NULL` 只允许出现在 services/sku_codec.py、workflows/list_new.py(去重闸与同口径的 _FAMILY_LISTED_SQL,同文件算一处)、workflows/alloc_push.py 三个消费方文件 + refdata/schema.sql(部分索引条件,DDL 不是消费方过滤,理由写在集合旁);批次 2 后加反向断言:四个文件各至少出现一次
- tests/test_sku_guard.py::test_abandon_is_called_only_from_the_four_points — AST 扫 abandon( 调用,(文件, 函数) 必须在四元素白名单里(delete_verified 观测落地点 / sku_locked_heal._relist / listing_sheet._mark_upc_conflicts / sku_migrate);sku_codec 未落地时 skip
- tests/test_sku_guard.py::test_the_five_non_abandon_workflows_never_call_it — 反向硬清单:product_clear.py / problem_product_cleanup.py / maintenance.py / walmart_catalog.py / feed_track.py 里出现 abandon 字样即红,断言消息写清「product_clear 停用不弃码」的理由;**从合并当天就生效**,不依赖 codec 存在
- tests/test_sku_guard.py::test_only_sku_codec_updates_the_registry_lifecycle_columns — 文本轨扫 `UPDATE catalog.listing_sources`,只允许出现在 services/sku_codec.py
- tests/test_sku_guard.py::test_opaque_sku_regex_matches_the_codec_alphabet — 从 schema.sql 的 listing_sources_opaque_sku_uidx 正则里抽字符集,与 services/sku_codec._ALPHABET 逐字比(同 product_events_identity_idx 的表达式索引纪律)
- tests/test_sku_guard.py::test_feishu_field_names_come_from_registry_constants — services/ 与 workflows/ 里禁止 "ASIN" / "SKU" / "来源码" 作为字段名字面量(AST 常量轨,注释与 docstring 天然不在射程);配 test_field_literal_guard_does_not_flag_registry 防误报
- tests/test_sku_guard.py::test_source_letters_are_registered_for_every_minting_source — SKU_SOURCE_LETTERS 的键 == {SOURCE_AMZ, SOURCE_MATCH, SOURCE_1688, SOURCE_SELF},SOURCE_UNKNOWN 不在里面(unknown 不发码)
- tests/test_sku_guard.py::test_empty_source_letter_is_a_hard_error — 字母未填时 mint 抛 ValueError,不静默发 11 位短码(短码会与 12 位形态判据全线错开)
- tests/test_sku_guard.py::test_listing_sheet_cols_matches_registry — services/listing_sheet._COLS == len(resources.LISTING_SHEET.columns);两处数字脱节会让 read_rows 静默少读一列
- tests/test_sku_guard.py::test_the_whitelists_do_not_rot — 照抄 test_feishu_guard.py:348-371:每条白名单必须还指得着文件/函数、理由非空;指空了就是该删的历史,不是豁免
- tests/test_sku_guard.py 的四条文档守门 — test_conventions_has_the_sku_section / test_claude_md_points_at_the_sku_rules / test_feishu_doc_lists_the_new_columns / test_blueprint_retire_wording_is_corrected:文档失真不会让任何测试变红,除了这几条(与 tests/test_readme.py 同源纪律)
- tests/test_sku_schema.py::test_schema_sql_is_idempotent_by_construction — 本轮新增的每条 DDL 都带 IF NOT EXISTS 或包在 DO 块里(DROP VIEW+CREATE VIEW 例外,小白名单登记并写理由)
- tests/test_sku_schema.py::test_every_new_object_name_appears_in_the_doc — 四个新对象名在 docs/db_schema.md 里都找得到(schema.sql 是 db_schema.md 的同步产物,两处必漂)
- tests/test_problem_scan.py::test_conflicts_view_join_matches_its_index(既有,补两条断言)+ ::test_conflicts_view_does_not_filter_abandoned(新增)
- tests/test_product_events_registry.py::test_constants_match_ledger_strings — 批次 2 补 SKU_ABANDONED / SKU_REPLACED 两行
- tests/test_rate_bucket.py::test_no_new_feed_bucket_for_sku_update — _RATE_BUCKETS 里不许出现 SkuUpdate 之类的新键(不自创 feedType;SkuUpdate 骑既有 MP_MAINTENANCE / MP_ITEM 桶)
- tests/test_upc_pricing.py::test_status_cn_covers_every_status — STATUS_CN 键集合 ⊇ 代码里出现过的全部状态字面量,含两个 burned_*
- tests/test_readme.py 全部五条(既有)— 工作流数、每条工作流在册、不列不存在的、危险标、调度表一致;sku_migrate 落地那一批会同时红三条
- tests/test_gpt_skill.py::test_repo_copy_matches_the_schedule_table(既有)— 改 schedule note 或 workflow docstring 首行后必须重跑 skill_export

### 验收命令

```bash
python cli.py db_init && python cli.py db_init   # 幂等:连跑两次都必须成功(2026-08-13 生产实证:重跑炸 UndefinedTable 曾卡死 db_init)
```
```bash
python -m pytest -q                              # 全绿;README:602 的数字同步改成实跑值
```
```bash
python -m pytest -q tests/test_sku_guard.py tests/test_sku_schema.py tests/test_catalog_health.py
```
```bash
python -m pytest -q tests/test_readme.py tests/test_gpt_skill.py tests/test_problem_scan.py tests/test_product_events_registry.py tests/test_rate_bucket.py tests/test_upc_pricing.py tests/test_sku_asin.py
```
```bash
python cli.py catalog_health                     # F 段五项:在架未登记 / 重复活行组数 / 活码唯一索引在不在 / 不透明码与已弃码行数 / 订单双算组数
```
```bash
python cli.py skill_export --dry-run             # 改 schedule note 后应报 product_chain.md 与 SKILL.md 两个文件『改动』
```
```bash
python cli.py skill_export && python cli.py skill_export --dry-run   # 第二次必须报『N 个文件与调度表一致,无需改动』
```
```bash
python cli.py sources_backfill --dry-run         # 盲区统计;批次 0b 后应看到两桶分列
```
```bash
psql -d walmart_data -c "SELECT * FROM catalog.v_listing_sources_dupe_live"                                                    # 期望 0 行;非 0 则活码唯一索引不会建成
```
```bash
psql -d walmart_data -c "SELECT indexname FROM pg_indexes WHERE indexname IN ('listing_sources_live_uidx','listing_sources_opaque_sku_uidx')"   # 期望 2 行
```
```bash
psql -d walmart_data -c "SELECT count(*) FROM catalog.listing_sources WHERE sku ~ '^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{12}$'"    # 批次 2 之前必须为 0
```
```bash
psql -d walmart_data -c "SELECT count(*) FROM catalog.listing_sources WHERE abandoned_at IS NOT NULL"                            # 批次 2 之前必须为 0
```
```bash
psql -d walmart_data -c "SELECT * FROM orders.v_order_line_dupes LIMIT 20"                                                       # 批次 3 之前应为 0 行
```
```bash
psql -d walmart_data -c "SELECT count(*) FROM catalog.audit_listing_conflicts"                                                   # 视图改造前后行数必须一致(零行为变化的实测)
```
```bash
psql -d walmart_data -c "SELECT status, count(*) FROM catalog.upc_pool GROUP BY 1 ORDER BY 2 DESC"                               # 批次 2 之前不应出现 burned_delete / burned_lock
```
```bash
python cli.py maintenance_scan --dry-run -p preview=1   # 批次 0a 六处 SQL 收口的唯一实测:改动前先存一份摘要做对照,切换前后意图集合必须相同
```
```bash
grep -n 'API 侧无 reactivate 端点' docs/api_blueprint.md   # 应无输出(§retire 语义已更正)
```
```bash
grep -rn 'B0[0-9A-Z]\{8\}' tests/*.py | wc -l            # 夹具清理前后对照,变化的行逐条能说出属于 ①②③ 哪一类
```

### 决策点

- **D1|活码唯一索引在存量有重复时的行为**
  - 默认:包在 DO 块里条件建:先查 catalog.v_listing_sources_dupe_live,0 行才 CREATE UNIQUE INDEX,否则 RAISE WARNING 并跳过;缺失状态由 catalog_health F 段带 ⚠ 报出来。
  - 备选:(a) 裸 CREATE UNIQUE INDEX,存量有重复就让 db_init 炸——fail loud,但违反 db_init 幂等可重跑这条硬约束,而且是在生产上炸;(b) 先跑一段 UPDATE 把重复组里较旧的行打 abandoned_at 再建索引——自动改数据,违反「判不准就判活」与「写操作永不自动兜底」。
  - 影响:选默认:索引没建成期间并发双 mint 没有护栏(mint 事务内查活行仍在,但不防两个进程同时查到空),所以 catalog_health F 段第 ③ 项不可省,PR 验收清单里必须有一条 psql 直查 pg_indexes。选 (a):某天 db_init 在生产上失败,整个建库/改表流程停摆。选 (b):静默改了登记簿数据,而登记簿是身份唯一出处。
- **D2|不透明码全局唯一索引的形态**
  - 默认:表达式部分唯一索引 `(sku) WHERE sku ~ '^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{12}$'`,**不带 abandoned_at 条件**(含已弃码行,码永不复用);正则字符集与 services/sku_codec._ALPHABET 逐字一致,守门钉住。
  - 备选:(a) 加 STORED 生成列 is_opaque 再对它建索引——多一列多一处逻辑,生成列表达式同样要与 codec 一致,没省掉任何一致性负担;(b) 全表 UNIQUE(sku)——建不起来,存量 sku=asin 跨店重复是既成事实;(c) 索引带 `WHERE abandoned_at IS NULL`——等于允许已弃的码被重新发出去。
  - 影响:选默认:如果存量里恰好有一个 12 位、且只用那 30 个符号的旧 SKU,建索引会失败——所以验收命令里有一条专门数它(批次 2 之前必须为 0)。选 (c):跨代际复用同一串,而复用一个已 DELETE 过的串正是「同店重复 listing」的入口。
- **D3|`abandoned_at IS NULL` 白名单的粒度**
  - 默认:**文件级**四元素集合:services/sku_codec.py、workflows/list_new.py、workflows/alloc_push.py、refdata/schema.sql(DDL 豁免,理由写在集合旁)。list_new 里的 _SQL_LISTED_ASINS 与 _FAMILY_LISTED_SQL 同文件,算一处。
  - 备选:(a) 按 (文件, SQL 常量名) 的三元素精确粒度——更严,但 _FAMILY_LISTED_SQL 会变成第四条,而 synthesis 明写「只允许出现在三处」,清单与规则文字对不上;(b) 不豁免 schema.sql,把部分索引条件写成别的等价形态(如 `abandoned_at IS NOT DISTINCT FROM NULL`)绕开正则——靠拼写绕守门,最坏的一种。
  - 影响:选默认:粒度粗一档,同一文件里多长出一个过滤点不会被拦(靠 review);但白名单短、说得清、不会因为一个合法新增就被人整条注释掉。选 (a):conventions §九 的「三处」这句话要改写成「三个 SQL 常量 + 一个索引」,规则变绕。
- **D4|SKU 编码常量落 registry 还是 services**
  - 默认:只有 SKU_SOURCE_LETTERS(四个来源字母的映射)落 registry/resources.py;字母表 _ALPHABET、随机段长度 11、总长 12、查重重试 5 次全部落 services/sku_codec.py。
  - 备选:(a) 全部落 registry——铁律 3 管的是路径/token/表 ID/服务器地址,编码规则不是外部资源;而且字母表已被 schema.sql 的索引正则引用一次,再多一个出处就绑不住了;(b) 全部落 services——sku_plan §2 明写「registry 常量表 SKU_SOURCE_LETTERS」,且来源字母是所有者要填的业务配置,放 services 里人不会去找。
  - 影响:选默认:一致性负担只有一对(codec 字母表 ↔ schema.sql 索引正则),由 test_opaque_sku_regex_matches_the_codec_alphabet 钉住。选 (a):变成三处一致(registry ↔ codec ↔ schema.sql),必漂。
- **D5|sku_migrate 是否进调度**
  - 默认:**不进**。手动跑,README 标「危 一」,一店一批;文档里写死它与 13:00 product_chain 抢同一个 MP_MAINTENANCE 速率桶,不许并跑。
  - 备选:(a) 进 gpt 调度按夜间跑——存量改码是不可逆的破坏性动作(一个 Product ID 只挂一个 SKU,旧串弃了就不回来),而 registry/schedule.py 头注写死「破坏性一律先手动 --dry-run 人眼确认再 load」;(b) 进调度但只跑 dry-run——空转报成功,正是 2026-08-16 改默认值时要消灭的那种形态。
  - 影响:选默认:进度靠人推,慢;但每一批都过人眼,而 sku_plan §7 批次 3 的节奏本来就是「1 个品 → 10 个 → 一家店 → 其它店」。选 (a):某夜一条链自己把一家店的存量全改了码,而改码期间该店的 dispositions 在途行会指向已消失的 SKU。
- **D6|三个未拍板决策(A/B/C)在文档与代码里怎么落**
  - 默认:conventions §九 第 8 段逐条写「默认假设 + 选另一个会怎样」并明确标**未拍板**;backlog.md 各记一条待办、写清阻塞哪个批次。代码按默认假设实现,两种选择的分叉点各集中在一处(A→problem_scan 扫描面;B→listing_sheet._mark_upc_conflicts;C→alloc_push._SQL_ONLINE),改主意时只改那一处,且那一处要带注释指向决策编号。
  - 备选:(a) 等所有者拍板再动代码——批次 0/1 与三个决策完全无关,等它们会把整条战线停住;(b) 只在 sku_plan.md 里记——那份文档不是必读件,拍板后没人回去改代码。
  - 影响:选默认:三处分叉点必须带显式注释指向决策编号,否则拍板后找不回来;守门 test_abandon_is_called_only_from_the_four_points 的白名单里,决策 B 那一条要写「若所有者选不换则删本条」。
- **D7|db_init 是否负责报 SKU 体检**
  - 默认:**不报**。db_init 保持现状(执行 schema.sql + 返回固定摘要);体检归 catalog_health 新增的 F 段。
  - 备选:(a) 让 db_init 跑完体检 SQL 并把结果拼进摘要——db_init 的职责是「执行 schema.sql 的同步产物」,加体检会让它变成两件事;而且 db_init 通常只在建库/改表时跑,体检需要能随时跑;(b) 靠 RAISE WARNING——psycopg3 把 server notice 送到 connection 的 notice handler,进不了 run() 的返回摘要,等于没人看见。
  - 影响:选默认:改表当天必须记得多跑一条 catalog_health,PR 验收清单里有这一行。选 (a):db_init 摘要变长且职责不清,而且它不在任何调度里,长期没人跑。
- **D8|批次 3 的 SkuUpdate 走哪个 feedType**
  - 默认:**不新增 feedType 桶**;按单品实测结果二选一:MP_MAINTENANCE 最小载荷({sku 新码, GTIN 现号, SkuUpdate: Yes})优先,不行则 MP_ITEM 全量载荷。两者的桶(各 8/hour)与切片限额都已登记。
  - 备选:(a) 直接按 MP_ITEM 全量实现——保险,但改码 = 重发全部内容,标题/属性会被我们再生成的内容覆盖,这是副作用要所有者接受;(b) 猜一个新 feedType 名先登记桶——api/_client.py:232 的设计是「未登记的 feedType 一律拒绝」,猜一个进去等于把那道防线打开。
  - 影响:选默认:批次 3 被一条实测阻塞(`grep -rl SkuUpdate <DATA_ROOT>/specs/MP_ITEM/5.0.20260608-18_15_07-api/`),这条实测必须排在批次 3 动工之前;结果决定改码要不要重发内容,是所有者要点头的那一件事。无论哪种,sku_migrate 与 product_chain 抢桶这条纪律都成立。
- **D9|upc_pool 两个新状态是否进 STATUS_CN 投影**
  - 默认:进,文案「已烧(删除)」/「已烧(锁死)」。project_to_sheet 已是 STATUS_CN.get(status, status),加两项即自动生效,零代码改动。
  - 备选:(a) 不进,飞书上显示英文原值——运营看不懂,而 UPC 池表是运营的注入口;(b) 复用「冲突」文案——正是 synthesis 明确禁止的语义混同(conflict = 全站已存在;burned = 跟着已弃的码下岗)。
  - 影响:选默认:多两个状态文案,pool_stats 报表天然分得开。选 (b):飞书上「冲突」数字暴涨,而它本来是「有人抢了我们的号」的信号,从此再也不能当信号用。
- **D10|跟卖存量(PHUMWMT+日期+序号)与存量 relist 行是否迁**
  - 默认:**不迁**(未拍板,默认保守):跟卖 SKU 不含 ASIN,改码收益只有「货源隐匿」而跟卖本来就不暴露亚马逊来源;文档里明写「未迁」,并在 sources_backfill 的『旧格式存量』桶里长期计数。
  - 备选:(a) 一起迁——批次 3 的面扩大一倍,而 match 行的 source_key 是 GTIN、UPC 池不参与、弃码语义(只标不烧)与 amz 行不同,状态机要多一条分支;(b) 迁一部分——制造第三种状态,最坏。
  - 影响:选默认:批次 3 完成后 sources_backfill 的『旧格式存量』桶不会归零,报警口径必须写成「新码漏登记才报警」而不是「存量归零才算完」——这正是 C0-SCHED-1 分两桶的理由之一。所有者若改主意选 (a),match 行的 abandon 分支(只标不烧)已在 sku_codec 里预留,不用重构。

### 依赖

- 【本包内部】C0-DDL-1 → C0-DDL-2 → C0-DDL-3:加列必须先于依赖新列的索引;体检视图必须先于引用它的 DO 块(schema.sql:56-58 已写死这条纪律,2026-08-06 生产实证 UndefinedColumn)
- 【本包内部】C0-DDL-2/3 → C0-HEALTH-1:catalog_health F 段要查这两个索引与那个视图,对象不存在则 SQL 报错
- 【本包内部】C0-DDL-1 → C0-DOC-1:文档同步 DDL(反过来不成立,可执行产物要先落地才谈得上『同步』)
- 【本包内部】C0-SCHED-1 → C0-SKILL-1:skills/ 是 skill_export 的渲染产物,note 不改就没东西可生成;顺序反了 test_gpt_skill 会红
- 【本包 → 0a】C0-DDL-1/2/3 必须先合:services/sku_codec.mint 的 INSERT 要写三列、活行查询要读 abandoned_at,列不在就是 UndefinedColumn
- 【本包 → 0a】C0-GUARD-1..7 建议与 0a 同一个 PR 合(白名单按现状预填 ⇒ 合并当天全绿);若单独先合,必须确认白名单预填无遗漏,否则 CI 立刻红在 19 个地方
- 【本包 → 0a】C0-REG-5(SKU_SOURCE_LETTERS)必须先合:sku_codec.mint 查它取来源字母;值可先留空串(mint 抛错,批次 2 之前无人调用)
- 【0a → 本包】sku_codec 落地后,test_opaque_sku_regex_matches_the_codec_alphabet 与 test_abandon_is_called_only_from_the_four_points 从 skip 转正
- 【0a → 0b】0b 的十四处消费方收口全部依赖 0a 的 services/sku_asin.resolve / resolve_many
- 【本包 C0-REG-2 → 0b】ORDER_SALES / ORDER_RETURNS 的 asin 字段常量必须先在 registry 登记,0b 才能在 services/order_center.py:370,405 写 f.asin
- 【0a + 0b → 1】批次 1 的「V 为空回落 B 列」依赖 resolve 兜底存量;C0-REG-1(LISTING_SHEET 22 列)与 services/listing_sheet._COLS=22 必须同一个 PR
- 【1 → 2】mint 在预备期抽码后要写 V 列;V 列不在位就等于新码只存在于登记簿,飞书上运营看不到、回执反哺找不回行
- 【本包 C0-TEST-1 → 2】SKU_ABANDONED / SKU_REPLACED 两个事件码必须与批次 2 同批登记进 EVENTS,否则 abandon 一调 record_many 就抛 ValueError
- 【2 →(生产跑满一轮)→ 3】sku_plan §7 批次 3 前置:批次 0/1/2 全部合并且新码在生产跑过至少一轮
- 【D8 实测 → 3】`grep -rl SkuUpdate <DATA_ROOT>/specs/…` 定 feed 类型与最小载荷,是批次 3 的硬前置(所有者机器)
- 【本包 C0-DDL-6 → 3】orders.v_order_line_dupes 必须先落地并确认基线为 0 行,才谈得上用它发现改码造成的双算
- 【本包 C0-DOC-8 → 3】README 工作流数 76→77 与 6.4 表格新行必须与 workflows/sku_migrate.py 同一个 PR,否则 test_readme 三条同时红
- 【外部前置,不由本包解决】所有者在飞书建四列(上架表 V「SKU」/ 销售订单「ASIN」/ 售后订单「ASIN」/ 在线产品总表「来源码」),建之前用 list_fields 确认无同名人工列;所有者定四个来源字母填进 SKU_SOURCE_LETTERS
- 【外部前置】决策 A/B/C 拍板 —— A 阻塞 problem_scan 扫描面(批次 2 之后可补),B 阻塞 listing_sheet._mark_upc_conflicts 的弃码点(批次 2),C 阻塞 alloc_push._SQL_ONLINE 口径(批次 0a 或 2;不对齐则分配链派、list_new 每轮拦)

### 风险

- **活码唯一索引建不成而没人发现**:存量登记簿若已有重复活行,DO 块跳过建索引只留一条 RAISE WARNING,而 warning 进不了 db_init 摘要。并发双 mint 于是全程无护栏。缓解:catalog_health F 段第 ③ 项必须带 ⚠ 报出来,且 PR 验收清单里有一条 psql 直查 pg_indexes。**这是本包最需要人眼盯的一条。**
- **不透明码正则与 codec 字母表漂移**:两处一致只靠一条守门测试;若有人改了 codec 字母表而 sku_codec 尚未存在(0a 之前守门是 skip 状态),漂移会到批次 2 才被发现,而那时已经有码发出去了。缓解:0a 合并当天把 skip 转正,并在 _ALPHABET 上方写一行「改这里必须同改 refdata/schema.sql 的 listing_sources_opaque_sku_uidx」。
- **守门白名单被当成筛子**:19 条预填白名单里任何一条在收口后忘了删,守门就对那一处永久失效,而 test_the_whitelists_do_not_rot 只查『指得着东西』,查不出『该删没删』。缓解:每条理由里写死批次归属与「收口后删」,每批 PR 的验收清单里有一行「白名单删了几条、还剩几条」。
- **audit_listing_conflicts 视图改造的等价性只在存量成立**:论证前提是登记簿存量行的 source_key 逐字等于 sku(schema.sql:230-236 的格式回填),而『在架但未登记』的行靠 LEFT JOIN + coalesce 兜住。若某天有人改了 sources_backfill 的回填规则(比如把三段式也回填成 amz),等价性就不再成立而且不报错。缓解:验收命令里有一条改造前后行数对照;conventions §九 记下这个前提。
- **批次 3 的 MP_MAINTENANCE 桶争用**:sku_migrate 与 13:00 product_chain 的维护链共用按店 8/hour 的桶;桶是跨进程滑动窗口(落 PG),抢桶不报错,只会让维护链抱锁睡到下一个小时,表现是『那天维护链跑了三小时还没完』。缓解:纪律写进 api/_client.py 注释 + README sku_migrate 那一行 + PR 验收清单;真跑 sku_migrate 前先确认 ops.runs 里 product_chain 已收工。
- **飞书加列的一次性全量重推被当成故障**:销售订单加 ASIN 列 ⇒ 行指纹全变 ⇒ 下一次 push 把 90 天窗口(实证约 7100 行)全量重推。已有先例(2026-08 加「拉取时间」列时 7100 行更新 3122)。缓解:三处写清『是预告不是故障』(registry 注释 / feishu_tables.md / PR 验收清单)。
- **上架表加列在飞书侧撞上同名人工列**:程序一登记 V 列常量就开始按位置写,若所有者建列时表里已有别的东西在 V 位,会被静默覆盖。2026-08-17 已经吃过一次『人在维护的表被覆盖』的亏。缓解:建列前跑 list_fields 确认,写进 feishu_tables.md 的建列纪律。
- **夹具 sku=asin 的清理判错方向**:把该归第 ① 类(加平行用例)的错判成第 ③ 类(改夹具值),会把原本证明『存量不变』的用例改没了,而测试仍然绿。缓解:处理原则里写死「判不准就归类 ①」(conventions §五同源纪律),并要求每条改动能说出属于哪一类。
- **conventions §九 被后来的瘦身顺手删掉**:CLAUDE.md 与 conventions 有过一次瘦身迁移(2026-08-26),SKU 这一节是新写的、最容易被当成过程记录删掉。缓解:test_conventions_has_the_sku_section 守住关键短语。
- **决策 A 未拍板期间的现实后果**:现行 problem_scan 扫描面是『一切非 PUBLISHED 且未缺席』,退役 item 的观测形态正是 UNPUBLISHED +「end date has passed」⇒ product_clear 停用的品一到两轮后会被自动链 DELETE 掉,而 DELETE 是四个弃码点之一 ⇒ 停用最终变成弃码 + 烧 UPC。这不是本包引入的,但本包的文档必须把它写明白(workflows/product_clear.py:20-21「停用(可恢复)」要加注「现行策略下可恢复窗口 ≈ 下一轮扫描之前」),否则所有者会以为默认假设『RETIRE 不弃码』已经保住了可恢复性。

### PR 切分

【合并顺序】共 7 个 PR,横切件分两次进场(前置一次、收口一次)。

**PR-C0|横切前置(与 0a 同批合,或紧邻其前)**:C0-DDL-1/2/3/4/6 + C0-HEALTH-1 + C0-GUARD-1..7(白名单按现状预填,合并当天全绿)+ C0-REG-5(SKU_SOURCE_LETTERS,值可空)+ C0-REG-6(feed 桶注释)+ C0-TEST-4 + C0-DOC-1/2。零行为变化,约 400 行,其中 250 行是守门测试。
**PR-0a|身份积木 + 维护/审核/分配 SQL 收口**:sku_codec + sku_asin.resolve + §3.2 六处硬等号(**含本包 C0-DDL-5 的视图——DDL 与 Python SQL 必须同批,否则 db_init 与代码脱节**)+ C0-TEST-2 + 删白名单里标 0a 的 6 条。
**PR-0b|订单/事件/黑名单/order_audit 收口 + 飞书 ASIN 列**:§3.3 十四处消费方 + C0-REG-2 + services/order_center 四处 + sources_backfill 摘要分桶 + C0-SCHED-1 + C0-SKILL-1(skill_export 重新生成)+ 删白名单里标 0b 的 8 条。
**PR-1|上架表 V 列 + 回执自愈链读 V**:C0-REG-1(22 列)+ listing_sheet._COLS=22 + 单列写函数 + §3.4 里依赖 V 列的五处 + C0-REG-3(来源码列)+ C0-REG-4(UPC 池 E 列口径)+ C0-DOC-4。存量 V 为空回落 B 列 ⇒ 仍零行为变化。
**PR-2|写侧切换**(唯一有行为变化的批次):mint 挂 r["_sku"] + 四个弃码点 + 代际上限 + 24h 冷却泛化 + C0-TEST-1(两个事件码)+ 守门从 skip 转正 + 删白名单剩余条目。**合并后先跑 sku_plan §7 的七步试点,不直接放全店。**
**PR-C1|横切收口(PR-2 之后)**:C0-DOC-5(api_blueprint §retire 三处更正)+ C0-DOC-6(conventions §九)+ C0-DOC-7(CLAUDE.md)+ C0-DOC-9 + README:275、602。纯文档,可与 PR-2 并行写、串行合。
**PR-3|存量改码**:workflows/sku_migrate.py + 三态状态机 + C0-DOC-8(README 76→77 与 6.4 新行)。前置:PR-0..2 全合、新码生产跑满一轮、D8 实测通过。

【每批 PR 的验收清单模板 —— 逐条打勾,少一条不合】
1. `python cli.py db_init && python cli.py db_init` 连跑两次成功(**动了 schema.sql 的 PR 必跑**)
2. `python -m pytest -q` 全绿;README:602 的数字改成实跑值
3. `python cli.py catalog_health` 的 F 段五项贴进 PR 描述(尤其第 ③ 项:活码唯一索引在不在)
4. 三条 psql 基线贴进 PR:重复活行组数 / 不透明码行数 / 已弃码行数(批次 2 之前后两项必须为 0)
5. **动了 workflow 的**:`python cli.py <workflow> --dry-run` 输出贴进 PR,人眼确认后才谈真跑(CLAUDE.md 纪律,没有默认值替你挡)
6. **动了 SQL 判据的**:`maintenance_scan --dry-run -p preview=1` 改前改后意图集合对照(批次 0a 六处收口的唯一实测)
7. **动了 registry/schedule.py 或 workflow docstring 首行的**:`skill_export --dry-run` → 落盘 → 再 dry-run 报「无需改动」
8. **动了飞书列的**:所有者已建列 + list_fields 确认无同名人工列;PR 描述里写明「下一次 push 会全量重推 90 天窗口,是预告」
9. 守门白名单本批**删了几条**、还剩几条,写进 PR 描述(清单归零 = 波及面真的做完)
10. 文档同步逐条对照 docs_to_sync 清单,勾掉本批该动的那几行
11. 回滚方案写进 PR 描述(照下面那张表)

【逐批回滚方案】
· **PR-C0**:DDL 全是加法。回滚 = `DROP INDEX IF EXISTS listing_sources_live_uidx, listing_sources_opaque_sku_uidx;` + `DROP VIEW IF EXISTS catalog.v_listing_sources_dupe_live, orders.v_order_line_dupes;`。**三个新列不 DROP**(conventions §五:DROP COLUMN 不可回滚,未连库核对一律不执行;零消费方 = 零影响)。代码侧 git revert。
· **PR-0a**:git revert;audit_listing_conflicts 靠 DROP VIEW + CREATE VIEW 回旧版本(schema.sql 里本来就是这个形态,天然可回)。0a 不调 mint,登记簿不会有新写入。
· **PR-0b**:git revert;飞书两张订单表的 ASIN 列留着不删(空列无害),registry 常量 revert 后程序不再写它。skills/ revert 后重跑一次 skill_export 确认一致。
· **PR-1**:git revert 到 _COLS=21 与 21 项 columns;飞书 V 列留着不删。**已写进 V 列的值留着无害**——回滚后所有读点回落 B 列 ASIN,与切换前完全一致。
· **PR-2**:**唯一有不可回滚成分的一批**。代码可 revert(载荷 sku 改回 r["asin"]),但**已发出的新码 item 在沃尔玛侧真实存在**且占着一个 UPC。回滚后的安全性由一件事保证:**去重闸的键是 coalesce(ls.source_key, w.sku) 而不是 sku**——新码行的 source_key 就是 ASIN,revert 后去重闸照样拦得住这些品,不会重复上架。处置:把已发出的新码品列一份清单交所有者;登记簿里的活码行**不要手工删**(删了去重闸就漏了)。**这正是试点必须一店一品的理由。**
· **PR-C1**:纯文档,git revert 无副作用。
· **PR-3**:三态状态机自带回滚路径(feed failed/未达/Unknown ⇒ rolled_back:清旧行 replaced_by、新码行 abandon('sku_update_failed'))。**真正不可回滚的是沃尔玛侧已改成功的 SKU**——一个 Product ID 只挂一个 SKU、旧串已 abandoned,再改回去等于第二次改码。处置:停止后续批次,已改的认;旧码行的 replaced_by 保留做反查(订单/售后带旧码回来仍查得到)。**所以节奏是 1 个品 → 10 个 → 一家店 → 其它店,每一级都跑 maintenance_scan preview 与 node_probe 对比。**
