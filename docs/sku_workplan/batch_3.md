## 批次 3|存量改码(SkuUpdate 三态状态机 + 新工作流 workflows/sku_migrate.py,DANGEROUS=True)

**目标**:把存量在架商品的沃尔玛 SKU(裸 ASIN / 三段式)迁到批次 2 的 12 位不透明码:提交前先落库并 **commit**(新码行 replaces + 旧行 replaced_by = pending)→ 发 SkuUpdate feed → 回执成功不定案 → catalog_sync 观测到「新码在架且旧码缺席」才 confirmed(旧行 abandon('sku_update'),不烧 UPC)→ 观测反证或回执失败则 rolled_back(新码 abandon('sku_update_failed'),旧行 replaced_by 清空)→ 判不准超期落 stalled 点名人工。同批把在途期与改码后的静默塌陷全部堵住:缺席事件/问题商品建议对 replaced_by 旧行免疫、新码不被记成 item_appeared、顽固/归类/WFS/在途防重四段代际经 catalog.sku_aliases 沿 replaces 链继承、分配链销量归属跟着继承、upc_pool.sku 与上架表 V 列跟着改(带 sheet_synced_at 补写)、订单侧「同 (store,po_id,line_number) 多 order_line_id」有唯一一处体检视图。节奏(1→10→一店→全店)由 _stage_cap 硬闸承载而不是纪律。目标是**止血**:改码只让切换后的记录干净,收不回沃尔玛已掌握的旧 SKU=ASIN 关联(SkuUpdate feed 本身就是显式映射)。

**零行为变化**:否

本批次**有行为变化,而且是全仓最重的一次**:它主动改变沃尔玛侧在架商品的身份列。逐条说明变化面与止损:① 新增 workflows/sku_migrate.py 是唯一发起方,DANGEROUS=True、缺省即真跑、store 参数必填、单轮 limit 有节奏硬闸(该店零 confirmed ⇒ limit≤1;<10 ⇒ limit≤10),不进 registry/schedule.py(手动、人盯);不跑它则本批次对生产零影响。② 对既有链路的改动全部是**抑制型或继承型**(只在 replaced_by/replaces 非空时才改变行为,存量数据这两列恒 NULL,故存量路径逐字节不变):walmart_catalog.mark_missing 少写一类事件、product_events.diff_catalog 少记一条 item_appeared 改记 sku_replaced、problem_scan._SQL_ITEMS 多一条 NOT EXISTS、problem_scan 四段 SQL(_SQL_STUBBORN/_SQL_LAST_CAT/_SQL_WFS_BLOCKED/_SQL_INFLIGHT)各多一段 UNION ALL 经 catalog.sku_aliases(视图对无替换关系的行返回空集,UNION ALL 加空集,结果集逐行不变)、alloc_survey._SQL_SALES 多一个 LEFT JOIN sku_aliases(同理)、feed_track 违禁反哺与 receipt_in_ledger 多一条 workflow='sku_migrate' 例外(既有工作流名都不命中)。③ **本批次不再改任何既有约束**:原稿要收紧「活码部分唯一索引」的条件,三个工作包三个索引名、批次 3 的 DROP 打空、裸 CREATE UNIQUE 还会在存量重复活行上让 db_init 整份回滚(四位审查者全部点名,blocker)。修订后由**批次 0a 一次把索引建成最终条件**(含 `replaced_by IS NULL`,replaced_by 列同批加,存量恒 NULL 故与 0a 自身判定等价),批次 3 只核验索引名与条件、只加两个反查索引与两列,不 DROP 不重建。④ api 层零新增端点、零新增桶:形态 A 复用已收录的 MP_MAINTENANCE(api/feeds.py:70 切片、api/_client.py:236 桶),形态 B 复用 MP_ITEM(api/feeds.py:77 切片、api/_client.py:238 桶),本批次只加注释与测试。⑤ 全部新增写库动作在 dry-run 下不执行 —— **_settle 与 _migrate 都有 execute 分支**(原稿只有 _migrate 有,_settle 却是全包写得最重的一段,已修),不 mint、不提交、不定案、不写飞书、不改处置、不删节点库存,摘要用占位码打印计划。

### 改动清单

#### S1 · `refdata/schema.sql` · 213-221(catalog.listing_sources 表体,锚点 `CREATE TABLE IF NOT EXISTS catalog.listing_sources (`)、226-227(既有 listing_sources_key_idx)、229-236(存量回填 INSERT,末行 `ON CONFLICT (store, sku) DO NOTHING;`)。新增语句插在 227 之后、229 的回填注释之前

**改动**:① 追加两列(ALTER TABLE ... ADD COLUMN IF NOT EXISTS,与本文件既有加列风格一致,如 :156-158):`replaces text`(新码行指回被它替换的旧码;与旧行的 replaced_by 互为反向指针)、`replaced_at timestamptz`(旧行进入 pending 的时刻,定案超时判据的唯一时间源)。② **不动任何既有索引**:批次 0a 已把活码部分唯一索引建成最终条件 `WHERE abandoned_at IS NULL AND replaced_by IS NULL AND source_key IS NOT NULL AND sku ~ <sku_codec 的不透明码谓词>`(replaced_by 列由 0a 同批建,存量恒 NULL 故与 0a 自身的判定逐行等价)。本条只做**核验**:db_init 后跑 `SELECT indexdef FROM pg_indexes WHERE schemaname='catalog' AND tablename='listing_sources'`,确认条件已含 `replaced_by IS NULL`,且该索引名在 schema.sql 全文只出现一次。③ 新增两个反查索引:`CREATE INDEX IF NOT EXISTS listing_sources_replaced_by_idx ON catalog.listing_sources (store, replaced_by) WHERE replaced_by IS NOT NULL;` 与 `CREATE UNIQUE INDEX IF NOT EXISTS listing_sources_replaces_uidx ON catalog.listing_sources (store, replaces) WHERE replaces IS NOT NULL;`(两者都是**局部**索引,存量零行命中,建索引不会因存量脏数据失败)。④ 在列旁写死语义注释:replaces/replaced_by/replaced_at 只由 services/sku_codec 写(mint_replacement / settle_replacement);replaced_by 非空 = 在途改码(pending),abandoned_at 非空且 abandoned_reason='sku_update' = 已定案。

**为什么**:**采纳四位审查者一致的 blocker**:原稿写 `DROP INDEX IF EXISTS catalog.listing_sources_active_uidx` 再裸建同名唯一索引 —— 仓内实测 refdata/schema.sql:226-227 只有 `listing_sources_key_idx` 一条索引,`*_active_uidx` / `*_live_uidx` / `*_live_key_uk` 三个名字谁都没建过,IF EXISTS 让 DROP 静默 no-op(收紧根本没发生),而随后那条**不带局部形态条件**的 CREATE UNIQUE 会在存量重复活行上失败,db_init 是整份 schema.sql 一次 execute、一条失败整份回滚 ⇒ 生产建库当场停摆。改由 0a 一次建到最终条件,是「每个能力只有一条实现路径」的字面落地:索引名与条件只有一处出生。③ 的反查索引是 mark_missing / diff_catalog / problem_scan 每轮都要做的「这行是不是在途被替换」查询的数据面,没有它就是每轮全表扫;唯一索引堵住「两个新码抢同一个旧码」这种只会在并发重跑里出现、且出现后无法自动分辨的脏状态。

**测试**:
- tests/test_sku_guard.py::test_live_code_unique_index_name_appears_once_in_schema
- tests/test_sku_guard.py::test_live_code_unique_index_condition_contains_replaced_by_is_null(读 schema.sql 文本断言,不连库)
- tests/test_sku_codec.py::test_pending_replacement_two_active_rows_pass_the_partial_unique_index(needs_pg)
- tests/test_sku_codec.py::test_two_new_codes_cannot_claim_the_same_old_sku(needs_pg)

**验收**:python cli.py db_init 连跑两次(幂等);psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "\d+ catalog.listing_sources" 确认两列与两个新索引在位;psql ... -c "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='catalog' AND tablename='listing_sources';" 确认活码唯一索引条件已含 replaced_by IS NULL 且只有一条

#### S2 · `refdata/schema.sql` · 576-577(retire_cooldown_open_uk 索引末行 `ON listing.retire_cooldown (store, sku) WHERE status = 'pending';`)之后、579 行注释 `-- ── 退役清理(2026-08-12 …` 之前插入

**改动**:新建工作流过程台账 `listing.sku_migrations`(DDL 全文见 ddl 字段)。列:id / store / old_sku / new_sku / source_type / source_key / feed_type / feed_id / status(pending|confirmed|rolled_back|stalled)/ submitted_at / settled_at / **sheet_synced_at** / error / detail jsonb / created_at。索引:`sku_migrations_open_uidx (store, old_sku) WHERE status='pending'`、`sku_migrations_new_uidx (new_sku)` 全表唯一、`sku_migrations_status_idx (status, created_at)`。表头注释写死分工:**身份权威在 catalog.listing_sources(replaces/replaced_by/abandoned_at),本表只是 sku_migrate 的过程账**;两者的状态迁移必须在同一事务里完成(与 listing.retire_cooldown 之于 upc_pool 同款分工,见 refdata/schema.sql:560-577)。

**为什么**:改码是「先落库 → 调接口 → 等观测」的三态流程,需要 feed_id、提交时刻、失败原因、重跑幂等键,这四样都不属于身份表;把它们塞进 listing_sources 会让一张被十几个消费方 JOIN 的身份表长出五个只有一个工作流看的过程列。本仓已有同款先例(listing.retire_cooldown),照抄形态而不是新发明。`status='pending'` 的部分唯一索引是崩溃重入的防重键。**新增 sheet_synced_at 是采纳审查意见**:原稿把飞书 V 列回写放在事务外、一次性、无补写路径 —— 一次写失败(飞书频控 99991400 / 行号找不到)之后该行已是 confirmed、不再进 _settle 的 pending 集,V 列永远停在旧码,而批次 1 的 row_sku 正是靠 V 列认 SKU,回执找行与退役从此对不上且不报错(conventions §八「当轮写完,攒到下一轮 = 悄悄少写」)。有了这一列,_settle 每轮补写 `status='confirmed' AND sheet_synced_at IS NULL` 的行。

**测试**:
- tests/test_sku_migrate.py::test_pending_row_is_written_and_committed_before_the_feed_is_posted
- tests/test_sku_migrate.py::test_second_run_does_not_open_a_second_pending_row_for_the_same_old_sku
- tests/test_sku_migrate.py::test_confirmed_row_with_null_sheet_synced_at_is_retried_next_round

**验收**:python cli.py db_init;psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "\d+ listing.sku_migrations";python -m pytest tests/test_sku_migrate.py -q

#### S3 · `refdata/schema.sql` · 237(存量回填 INSERT 结束于 236 `ON CONFLICT (store, sku) DO NOTHING;`,238 起是 UPC 池注释段)——视图插在 237 这一空行处

**改动**:新建视图 `catalog.sku_aliases`(DDL 见 ddl 字段):`SELECT store, sku, replaces AS alias_sku FROM catalog.listing_sources WHERE replaces IS NOT NULL;`。视图注释写死三条:① **「这个新码继承那个旧码的历史代际」的唯一出处**,消费方(problem_scan 四段 SQL、alloc_survey._SQL_SALES、将来任何按 (store,sku) 读历史的判据)一律经它取别名,不许各自现写 `listing_sources.replaces` 的 JOIN;② 只继承**一跳**,前提是「旧码改码后立即弃码、永不再改码」——若将来允许对同一个品连续改两次码,必须把本视图改成递归 CTE;③ 它是视图不是表,改码前恒为空集,所有消费方的 UNION ALL / LEFT JOIN 在改码前都是加空集。

**为什么**:改码之后,新码在 product_events / ops.feed_items / orders.order_lines 里**一条历史都没有**。五处按 (store, sku) 读历史的判据会同时失明:顽固件(delete_not_effective 代际,workflows/problem_scan.py:137-143)、问题归类(problem_categorized 最近类别,:132-136)、WFS 拦截(上次 DELETE 回执 0101218,:124-131)、在途防重(:102-114)、分配链销量归属(services/alloc_survey.py:236-245)。前者的后果已实证——顽固件的双 feed 加压静默丢失;WFS 那条的后果是每天重建议、重发、同一个错、白烧 DELETE_ITEM 配额(生产实见 11 条)。判据只能有一处出生(conventions §六),所以做成视图而不是五处各写一遍的 JOIN。

**测试**:
- tests/test_problem_scan.py::test_new_code_inherits_the_old_code_generation_through_sku_aliases
- tests/test_problem_scan.py::test_sku_aliases_is_empty_for_rows_without_replaces
- tests/test_sku_guard.py::test_only_sku_aliases_expresses_the_replacement_chain(全仓 *.py 里出现 `listing_sources` 与 `replaces` 同一条 SQL 的文件只允许 services/sku_codec.py 与 services/listing_sources.py)

**验收**:python cli.py db_init;psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT count(*) FROM catalog.sku_aliases;"(改码前应为 0)

#### S4 · `refdata/schema.sql` · 434(`DROP VIEW IF EXISTS catalog.product_risk;`)-463(`FROM catalog.product_events GROUP BY 1;`);两列插在 451-454 的「审核维度」注释块之前(即 450 行 last_removed_at 结束之后)

**改动**:视图加两列:`count(*) FILTER (WHERE event = 'sku_replaced') AS sku_replaced_times` 与 `max(occurred_at) FILTER (WHERE event = 'sku_replaced') AS last_sku_replaced_at`。视图是 DROP+CREATE 幂等重建,照既有写法(434-435),不动 `coalesce(asin, sku) AS asin` 的身份键。

**为什么**:所有者要的是「这个 ASIN 在这家店用过哪些码、为什么换」能从时间线上答出来。product_risk 是风险档案的人工/AI 查询入口,身份键已是 coalesce(asin, sku)(新旧码经登记簿都解析到同一 ASIN),加这两列就把改码接进既有时间线;不加的话 sku_replaced 事件写了没人看,与 2026-08-14 那次 audit_passed/audit_rejected「零读者」是同一个坑(注释就写在 :451-454)。

**测试**:
- tests/test_product_events.py::test_product_risk_view_exposes_sku_replaced_columns(按既有视图列断言写法)

**验收**:python cli.py db_init;psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT asin, sku_replaced_times, last_sku_replaced_at FROM catalog.product_risk WHERE sku_replaced_times > 0 LIMIT 5;"

#### S5 · `refdata/schema.sql` · 688(`CREATE INDEX IF NOT EXISTS order_lines_asin_idx ... WHERE asin IS NOT NULL;` 结束)之后、690(`CREATE TABLE IF NOT EXISTS orders.return_lines`)之前

**改动**:新建只读体检视图 `orders.v_order_line_dupes`(DDL 见 ddl 字段):按 (store, po_id, line_number) 分组,`HAVING count(DISTINCT order_line_id) > 1`,输出 store / po_id / line_number / n / skus / first_order_date。**不带时间窗口**——窗口由消费方自己加 `WHERE first_order_date > …`。视图注释写死:它是「订单双算」这条判据的**唯一出处**,services/order_lines.duplicate_po_lines 与 catalog_health、手工 psql 全部读它,谁都不许再写一遍 GROUP BY/HAVING。

**为什么**:**采纳两位审查者的 major**:原稿把同一条体检做了两份(批次 3 的 services 函数 `count(*)` + 120 天窗口 + LIMIT 200,横切包的视图 `count(DISTINCT order_line_id)` + 无窗口),两者报出的数字对不上时没有判据说该信哪个,而这条体检正是本批次唯一能发现「改码后销量双算」的手段。修订后由**本批次拥有这份 DDL**(横切包对应条目删除,理由写进 R5 决策日志),函数退化成薄壳。口径取 `count(DISTINCT order_line_id)` 而不是 `count(*)`:orders.order_lines 的主键就是 order_line_id(refdata/schema.sql:632),要问的正是「同一个 PO 行下有几个不同的行 id」。

**测试**:
- tests/test_order_lines.py::test_duplicate_po_lines_reads_the_view_not_a_local_group_by(断言函数 SQL 文本含 `orders.v_order_line_dupes` 且不含 `GROUP BY`)

**验收**:python cli.py db_init;psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT count(*) FROM orders.v_order_line_dupes;"(改码前基线,记录下来,改码后必须一致)

#### C1 · `services/sku_codec.py` · 批次 0a 新建文件;在 mint() 之后追加(mint/abandon/is_opaque/source_of 是批次 0a 的既有 API)

**改动**:新增 `mint_replacement(conn, store, old_sku, source_type, source_key, *, workflow='sku_migrate') -> str`。docstring 首行:「输入:连接 + 店 + 旧码 + 出身 → 输出:新抽的不透明码(同事务写完两条指针,先落库)」。行为固定为:① 若该 (store, old_sku) 的登记行已有 replaced_by 且对应新码行未 abandoned ⇒ **直接返回既有新码**(幂等,崩溃重入不换码);② 否则抽码(复用批次 0a 的抽码 + 全局查重 + 上限次数)→ INSERT 新行 {store, sku=新码, source_type, source_key, workflow, replaces=old_sku} → UPDATE 旧行 SET replaced_by=新码, replaced_at=now();③ 全程同一个 conn 同一事务,**不自己开连接、不自己 commit**(commit 归调用方,见 W4);④ **不复用活行**(与 mint 的语义相反,这正是要单独一个函数而不是给 mint 加开关的原因);⑤ INSERT 的冲突处理照 0a 修订后的 mint 分两支:撞 sku 全局唯一 ⇒ 重抽并计数;撞活码键唯一 ⇒ 重跑一次步骤①的查询、查到就返回对方刚写入的码(并发双 mint 的正解),查不到才当随机撞码 —— 不用无 target 的 `ON CONFLICT DO NOTHING`(catch-all 兜底,违反 conventions §六三要件)。

**为什么**:mint 的契约是「查到活行就复用同一码」,改码要的恰恰是「明知有活行也要新码」。给 mint 加 force 开关 = 同一个能力两条语义分支,踩 conventions §六;两个名字不同、语义各自单一的函数才是本仓的写法。幂等①是「防重状态先落库再调接口」的必然要求:pending 行已经写了、feed 还没发就崩了,下轮必须拿回同一个码,不然第二次抽码 = 载荷变了 = feeds.payload_key 防重不命中 = 同一个 item 被改两次码。⑤ 是采纳两位审查者对 0a mint 的同款意见:活码键冲突与随机撞码的正确处置完全相反,一锅端会把并发误诊成「随机源坏了」。

**测试**:
- tests/test_sku_codec.py::test_mint_replacement_writes_both_pointers_in_one_transaction
- tests/test_sku_codec.py::test_mint_replacement_is_idempotent_and_returns_the_same_code
- tests/test_sku_codec.py::test_mint_replacement_never_reuses_the_live_row
- tests/test_sku_codec.py::test_mint_replacement_returns_the_other_process_code_on_live_key_conflict

**验收**:python -m pytest tests/test_sku_codec.py -q

#### C2 · `services/sku_codec.py` · 紧接 C1 之后

**改动**:新增 `settle_replacement(conn, store, old_sku, new_sku, verdict: str, reason: str = '') -> None`,verdict ∈ {'confirmed','rolled_back'}。docstring 首行「输入:连接 + 新旧码 + 判词 → 输出:无(同事务把身份两端一次改到位)」。confirmed:调既有 `abandon(conn, store, old_sku, reason='sku_update')`(批次 2 已定 reason='sku_update' **不烧 UPC**)+ 写 product_events `sku_replaced`(sku=新码, store, detail={'old_sku': old_sku, 'reason': reason})。rolled_back:UPDATE 旧行 SET replaced_by=NULL, replaced_at=NULL + `abandon(conn, store, new_sku, reason='sku_update_failed')`(新码从未配 UPC,烧号分支天然空转,但必须走同一个 abandon 出口)。两个分支都幂等(已定案再调 = no-op)。**函数只动身份与病历**;UPC 改标、飞书 V 列、dispositions、节点库存归调用方在同一事务里各调各的积木。

**为什么**:「弃码只有一个实现」是批次 2 定的守门条款,第四个弃码点(SkuUpdate confirmed)必须从 abandon 这个唯一出口走,不能在工作流里自己 UPDATE 登记簿。rolled_back 分支把新码显式弃掉而不是删行,是因为登记簿行永不删除,且全局 UNIQUE(sku) 含已弃码行 —— 弃掉的码从此不会被任何人抽到,这正是「码是免费的、失败就换一个」的落地方式。不烧旧 UPC 的理由:GTIN 仍挂在同一个 item 上,烧了下次 claim 会去领新号,等于自造 SKU_LOCKED。

**测试**:
- tests/test_sku_codec.py::test_confirmed_abandons_old_row_with_sku_update_and_does_not_burn_upc
- tests/test_sku_codec.py::test_rolled_back_clears_replaced_by_and_abandons_the_new_code
- tests/test_sku_codec.py::test_settle_replacement_is_idempotent

**验收**:python -m pytest tests/test_sku_codec.py -q

#### C3 · `services/sku_codec.py` · 批次 0a 的 mint() 内部「查活行」那条 SELECT(锚点:该 SELECT 的 `WHERE ... abandoned_at IS NULL`)

**改动**:活行查询的 WHERE 加一条 `AND replaced_by IS NULL`,**与 0a 建的活码部分唯一索引条件逐字一致**;同时在注释里写死「索引条件与本查询条件必须逐字相同,改一处必须改另一处」,并由守门测试比对两段文本。

**为什么**:不加的话,pending 期间 list_new 对同一 (store, source_type, source_key) 调 mint 会拿回**正在被替换的旧码**,把已经宣告要退休的码又发一次 MP_ITEM。索引条件与代码条件必须逐字一致,否则「索引允许插入的状态,代码查得出两行」——这类不一致在本仓的表现一律是静默的。守门测试是采纳审查意见:S1 修订后本批次不再动索引,唯一的保护就剩这条断言。

**测试**:
- tests/test_sku_codec.py::test_mint_skips_a_row_that_is_being_replaced
- tests/test_sku_guard.py::test_mint_live_row_filter_matches_the_index_condition(比对 sku_codec 的 WHERE 片段与 schema.sql 里活码索引的 WHERE 片段)

**验收**:python -m pytest tests/test_sku_codec.py tests/test_sku_guard.py -q

#### C4 · `services/listing_sources.py` · 42(文件末尾,register 结束于 42 行 `return len(rows)`)

**改动**:新增 `replacement_map(conn, store) -> dict[str, str]`,docstring 首行「输入:连接 + 店 → 输出:{新码: 旧码}(在途改码的反向指针,catalog_sync 用)」。SQL:`SELECT sku, replaces FROM catalog.listing_sources WHERE store = %s AND replaces IS NOT NULL`。同时新增 `replaced_skus(conn, store) -> set[str]`,SQL:`SELECT sku FROM catalog.listing_sources WHERE store = %s AND replaced_by IS NOT NULL`。两个函数只读,不写。

**为什么**:walmart_catalog.upsert_items / mark_missing 每轮都要回答「本轮新出现的这个 sku 是不是某个旧码的替身」「本轮缺席的这个 sku 是不是正在被替换」。这两个查询是登记簿的知识,应当出生在登记簿积木里而不是在 walmart_catalog 里现写 SQL(services 新增积木先查重:services/listing_sources.py 现只有 register 一个函数,无重复)。

**测试**:
- tests/test_catalog_sync.py::test_replacement_map_only_returns_rows_with_replaces
- tests/test_catalog_sync.py::test_replaced_skus_only_returns_rows_with_replaced_by

**验收**:python -m pytest tests/test_catalog_sync.py -q

#### C5 · `services/upc_pool.py` · 219-220(mark_used 结束于 219 `return len(pairs)`,222 起是 release)——插在 220-221 空行处

**改动**:新增 `retag_sku(conn, triples: list[tuple[str, str, str]]) -> int`,入参 [(store, asin, new_sku)]。docstring 首行「输入:连接 + [(店, ASIN, 新 SKU)] → 输出:改标行数(改码后号还是那个号,只是它现在挂在新 SKU 名下)」。SQL:`UPDATE catalog.upc_pool SET sku = t.new_sku FROM unnest(%s::text[],%s::text[],%s::text[]) AS t(s,a,new_sku) WHERE store = t.s AND asin = t.a AND status IN ('claimed','used')`。**asin 列不动**(sku_plan §6 定稿:asin 列继续存 ASIN),status 与 used_at 不动(不是烧号也不是新消耗,是改标)。状态条件 `status IN ('claimed','used')` 与 burn_for_retire(services/upc_pool.py:198-203)逐字一致。函数体内 logger.info 报数。

**为什么**:upc_pool.sku 列的语义是「这个号现在被哪个沃尔玛 SKU 占着」(refdata/schema.sql:250 注释「已用时的沃尔玛 SKU」)。改码后不改它,列里存的是一个已经不存在于沃尔玛的串;而 listing_sheet._mark_upc_conflicts 与 UPC 池表投影都按它反查,反查不到就是撞库标不上、运营在表上看到的归属是错的。用独立函数而不是复用 mark_used(:210-219),是因为 mark_used 会把 status 推成 'used' 并刷 used_at —— 改码不是一次新消耗,时间戳不该被改写。状态条件与 burn_for_retire 对齐是采纳审查意见:两个改 upc_pool 的出口口径不一致本身就是漂移源。

**测试**:
- tests/test_upc_pricing.py::test_retag_sku_moves_the_code_without_touching_asin_status_or_used_at
- tests/test_upc_pricing.py::test_retag_sku_skips_rows_that_are_not_claimed_or_used

**验收**:python -m pytest tests/test_upc_pricing.py -q

#### C6 · `services/sku_codec.py` · 批次 0a 新建文件;紧跟 `_ALPHABET` / `is_opaque` 之后

**改动**:新增模块常量 `OPAQUE_SQL_PREDICATE: str`,由 `_ALPHABET` 与长度常量**派生**(不是手打):形如 `"({col} ~ '^[<字母表>]{12}$' AND {col} ~ '[A-Z]')"` 的模板串,消费方用 `OPAQUE_SQL_PREDICATE.format(col="w.sku")` 拼进自己的 SQL。docstring 写死:这是 `is_opaque()` 在 SQL 侧的等价表达,**唯一出处**;任何 .py 或 .sql 里再出现第二处 12 位字符集正则即违规。

**为什么**:**采纳两位审查者的 blocker**:不透明码字母表原本被安排了三个家(registry.SKU_ALPHABET / sku_asin.OPAQUE_ALPHABET / sku_codec._ALPHABET),批次 3 的 `_SQL_CANDIDATES` 又抄了第四份正则,两条守门断言互斥。按横切决策 D4 收敛到 services/sku_codec 之后,SQL 侧仍然需要一个可拼接的表达 —— 用派生常量而不是让工作流手打正则,是让「字母表只有一份」这条纪律在 SQL 侧也成立。拼接的是我们自己的常量、不是外部输入,无注入面。

**测试**:
- tests/test_sku_codec.py::test_opaque_sql_predicate_is_derived_from_the_alphabet(改 _ALPHABET 后常量跟着变)
- tests/test_sku_guard.py::test_is_opaque_and_the_sql_predicate_agree(对同一组样本:裸 ASIN / 三段式 / PHUMWMT 串 / 12 位纯数字 item id / 新码,Python 判定与 SQL 判定逐条一致,needs_pg)
- tests/test_sku_guard.py::test_no_second_opaque_regex_in_the_repo(全仓 *.py + refdata/schema.sql 里 12 位字符集正则只允许出现在 sku_codec 与 schema.sql 的索引条件)

**验收**:python -m pytest tests/test_sku_codec.py tests/test_sku_guard.py -q

#### M1 · `services/mp_mapper.py` · 641-646(ORDERABLE_SYSTEM_FIELDS 元组,锚点 `ORDERABLE_SYSTEM_FIELDS = (`;末行 646 `)`)

**改动**:元组里加 `"SkuUpdate"`,并在上方 636-640 的注释里加一行:SkuUpdate 是系统专属开关字段,只有 sku_migrate 才允许写,LLM 填了一律剔除。

**为什么**:这张表的作用是「LLM 不该填、填了也被系统值覆盖」(注释 :636-640),同一元组还被 `_fields_for_llm(ospec, ORDERABLE_SYSTEM_FIELDS, 10)` 用来剔提示词字段(:481、:560)。SkuUpdate 一旦被 LLM 塞进普通上架载荷,后果不是报错而是**沃尔玛把一次普通上架当成改码请求**——这是本仓能想到的最贵的静默失效。

**测试**:
- tests/test_mp_mapper.py::test_sku_update_is_a_system_field_and_never_reaches_the_llm
- tests/test_mp_mapper.py::test_llm_supplied_sku_update_is_stripped_from_orderable

**验收**:python -m pytest tests/test_mp_mapper.py -q

#### M2 · `services/mp_mapper.py` · 649-651(build_orderable 签名)、684-696(o.update 块)、697(`return o`)

**改动**:签名末尾加**关键字参数** `sku_update: bool = False`(不动前 5 个位置参数与既有 3 个默认参数,三个既有调用点一字不改);函数体在 `o.update({...})`(684-696)之后、`return o`(697)之前加:`if sku_update: o["SkuUpdate"] = "Yes"`。docstring 补一段:sku_update=True 只给 workflows/sku_migrate 的**形态 B**(MP_ITEM 全量重发)用,含义是「按 productIdentifiers 找到现有 item,把它的 SKU 改成 Orderable.sku」;普通上架永远不传。

**为什么**:形态 A/B 都要发这个字段,字段名必须只有一处出生(与 productIdentifiers/endDate 同款纪律)。做成默认 False 的关键字参数,存量三个调用点行为逐字节不变。

**测试**:
- tests/test_mp_mapper.py::test_build_orderable_without_sku_update_is_byte_identical_to_before
- tests/test_mp_mapper.py::test_build_orderable_with_sku_update_emits_yes

**验收**:python -m pytest tests/test_mp_mapper.py tests/test_list_new.py -q

#### M3 · `services/mp_mapper.py` · 698-699(build_orderable 结束于 697 `return o`,700 起是 assemble_mp_item)——插在 698-699 空行处

**改动**:新增 `build_sku_update_item(new_sku: str, product_id: str, product_id_type: str = "UPC") -> dict`,返回 `{"Orderable": {"sku": str(new_sku), "productIdentifiers": {"productId": str(product_id), "productIdType": str(product_id_type)}, "SkuUpdate": "Yes"}}`。docstring 首行「输入:新码 + 现有 Product ID → 输出:MP_MAINTENANCE 改码 MPItem(形态 A 最小载荷)」;正文写死四条:① 匹配键是 Product ID 不是 SKU(官方:Enter the correct SKU for that Product ID);② 一个 Product ID 只允许挂一个 SKU,所以同批不得对同一 productId 发两条;③ MP_MAINTENANCE 官方必填仅 SKU+GTIN,其余可选(蓝图 §5.4,docs/api_blueprint.md:192),故不带 Visible 段;④ **形态待所有者机器单品实测确认**(见决策 E),实测前只许 dry-run。product_id_type 取值由调用方从 catalog.walmart_items 的 upc/gtin 决定,不在此处猜。

**为什么**:形态 A 是默认方案:最小载荷 = 不重发内容 = 标题/属性不会被我们再生成的文案覆盖。载荷构造是 mp_mapper 的职责(MPItem 载荷唯一之家),工作流只负责决定发给谁;api/feeds 的 `build_payload("MP_MAINTENANCE", entries)`(api/feeds.py:92)已经能吃 MPItem dict 列表,api 层因此零改动 —— 这正是铁律 2「api 层只做接口适配」的形状。

**测试**:
- tests/test_mp_mapper.py::test_build_sku_update_item_shape_matches_the_minimal_maintenance_payload
- tests/test_feeds.py::test_maintenance_payload_wraps_sku_update_items_unchanged

**验收**:python -m pytest tests/test_mp_mapper.py tests/test_feeds.py -q

#### M4 · `api/feeds.py` · 257-263(_chunk_skus)、67-78(_SLICE_LIMITS);另核 api/_client.py:236(feeds.post.MP_MAINTENANCE)与 :238(feeds.post.MP_ITEM)

**改动**:**代码零改动,只加注释与测试**:在 _chunk_skus(257-263)的注释里补一句「改码载荷的 Orderable.sku 是**新码**,故 ops.feed_items 台账按新码落账 —— sku_migrate 的回执反查、feed_poll 的反哺都按新码找行,这是有意的」(现行 260-262 已经能从 `(e.get("Orderable") or {}).get("sku")` 取到,无需改代码)。同时在 _SLICE_LIMITS 上方注释里点明:MP_MAINTENANCE 切片 (1000, 24MB)(:70)、MP_ITEM 切片 (2000, 24MB)(:77)对改码够用,`feeds.post.MP_MAINTENANCE`=8/hour(api/_client.py:236)与 `feeds.post.MP_ITEM`=8/hour(:238)桶均已登记,**本批次不新增 feedType、不新增桶**。

**为什么**:工作包点名要「feed 类型桶登记」,核对结论是已登记、不必改 —— 但必须写下来,否则下一个人会照着计划再登记一遍,变成双轨。台账落新码这一条是回执链路的隐含契约,不钉住的话有人把 _chunk_skus 改成取旧码,sku_migrate 的回执就永远查不到而且不报错。

**测试**:
- tests/test_feeds.py::test_chunk_skus_takes_the_new_code_from_a_sku_update_payload
- tests/test_rate_bucket.py::test_sku_update_uses_the_already_registered_feed_buckets

**验收**:python -m pytest tests/test_feeds.py tests/test_rate_bucket.py -q;grep -n 'feeds.post.MP_MAINTENANCE\|feeds.post.MP_ITEM' api/_client.py

#### M5 · `services/mp_conform.py` · 687-709(strip_unknown,锚点 `def strip_unknown(spec: dict, ospec: dict, visible: dict, orderable: dict`;Orderable 段裁剪在 699-705)、906 与 913(两处调用点)

**改动**:新增模块常量 `ORDERABLE_SYSTEM_SWITCHES = ("SkuUpdate",)`(注释:沃尔玛的**系统开关字段**,不是内容字段;spec 里有没有它取决于版本,但它一旦被裁掉,一条改码 feed 会静默退化成一条普通上架)。strip_unknown 的 Orderable 循环(699-705)条件改为 `if k in okeys or k in ORDERABLE_SYSTEM_SWITCHES:`,并在 dropped 说明里对被保留的开关字段记一条 info。docstring 补一段说明为什么这不是 catch-all 豁免:名单是**穷举的一元组**、触发记日志、条件明确(conventions §六 真兜底三要件)。**若决策 E 落形态 A(默认),本条仍要做**——形态 A 走 MP_MAINTENANCE 最小载荷、根本不过 conform,本条是形态 B 的前置;不做就等于形态 B 不可用。

**为什么**:**采纳审查者 4 的 blocker,原稿完全没有这一跳**。实测 services/mp_conform.py:699-705:`for k, v in orderable.items(): if k in okeys: no[k] = v else: dropped.append(...)` —— okeys 来自 Orderable spec 的 properties。SkuUpdate 是否在 20260608 版 Orderable spec 里,批次 3 自己列为「待实测」。若不在,形态 B 的 SkuUpdate 会被静默剔掉,发出去的就是一条普通 MP_ITEM,沃尔玛按新 sku **建一条新 listing**,旧 listing 原样活着 —— 正好是 W3 分支 (b) 的「同店双挂」,而且是**每一行都双挂**,不是偶发。这是本批次第二条能造成不可逆损失的路径(第一条是 O4 的竞态)。

**测试**:
- tests/test_mp_conform.py::test_sku_update_survives_strip_unknown_when_absent_from_the_spec
- tests/test_mp_conform.py::test_strip_unknown_is_byte_identical_for_payloads_without_sku_update
- tests/test_mp_conform.py::test_no_other_unknown_orderable_field_survives(反向:随便塞一个 spec 外字段仍被剔)

**验收**:python -m pytest tests/test_mp_conform.py tests/test_list_new.py -q

#### O1 · `services/product_events.py` · 176-211(diff_catalog;签名 176-177,`if prev is None:` 分支 193-197)

**改动**:签名加关键字参数 `replaced: dict | None = None`(默认 None ⇒ 行为与今天逐字节相同)。函数体 193-197 那个 `if prev is None:` 分支改为:先查 `old_sku = (replaced or {}).get(sku)`,有值 ⇒ append `{sku, store, event: SKU_REPLACED, source, detail: {'old_sku': old_sku, 'published_status': new_st}}`;无值 ⇒ 照旧 ITEM_APPEARED。docstring(178-186)加一条:改码后新码第一次被观测到时记 sku_replaced 而不是 item_appeared。⚠ 前置:`SKU_REPLACED` 常量与 EVENTS 集合(services/product_events.py:95-121)由批次 0a 登记;未登记则 record_many(:159-162)fail loud。

**为什么**:对抗验证已经指出这一条:改码后新码行在 diff_catalog 眼里 prev is None,会记出一次**没有重上架事实的假代际**。而 problem_scan 的顽固判定(workflows/problem_scan.py:137-143 + :183-185)正是拿「最新事件是不是 item_appeared/item_reappeared」来决定要不要清掉上一代的 delete_not_effective(注释写在 :144-148)—— 假代际一记,已实证「删除未生效」的顽固件就静默丢掉双 feed 加压,回到每天删一次删不掉的循环。同时 catalog.product_risk.listed_times(refdata/schema.sql:437)也会被灌水。

**测试**:
- tests/test_catalog_sync.py::test_new_code_first_seen_records_sku_replaced_not_item_appeared
- tests/test_catalog_sync.py::test_diff_catalog_without_replaced_map_is_unchanged

**验收**:python -m pytest tests/test_catalog_sync.py tests/test_product_events.py -q

#### O2 · `services/walmart_catalog.py` · 106-123(upsert_items;with 块 114-118,diff_catalog 调用 119-121)

**改动**:文件顶部 import 增加 `from services import listing_sources`(本文件 :9 已 `from services import product_events`,层级仍是 services→services,不违反铁律 1)。在 114-118 的 with 块里、`cur.executemany(_UPSERT_SQL, rows)`(118)之前加一次取图:`replaced = listing_sources.replacement_map(conn, store)`;119-121 的调用改为 `product_events.record_many(conn, product_events.diff_catalog(old, rows, store, replaced=replaced))`。

**为什么**:diff_catalog 是纯函数(docstring :178 自述「便于测试」)所以自己不查库,替换关系必须由调用方喂进来;upsert_items 已经在同一事务里取过 old 状态(115-117),顺手多取一张小表是最省的接法,也保证「新码第一次出现」与「事件落账」是同一轮观测。

**测试**:
- tests/test_catalog_sync.py::test_upsert_items_feeds_the_replacement_map_into_diff_catalog

**验收**:python -m pytest tests/test_catalog_sync.py -q

#### O3 · `services/walmart_catalog.py` · 125-137(mark_missing;_MARK_MISSING_SQL 在 45-55,**不改**;事件写入在 133-136)

**改动**:SQL 不动(旧码仍然照常被标 missing_since —— 那是客观观测,也是定案的证据)。改的是事件写入:132 行拿到 gone 之后,先 `replaced = listing_sources.replaced_skus(conn, store_name)`,133-136 的事件列表改为只对 `sku not in replaced` 的行写 ITEM_MISSING;函数返回值仍是被标缺席的总行数(不改契约,workflows/catalog_sync.py:102 的调用方一字不动),被抑制的条数写进 logger.info。docstring(126-129)补一句:在途改码(replaced_by 非空)的旧码消失是**我们自己造成的**,不是平台下架,不进病历。

**为什么**:改码生效后旧码必然从目录消失。若照记 item_missing:① catalog.product_risk 的 unexplained_missing(refdata/schema.sql:446-448,「我们没提交过删/停 + 消失过」)会对每一个被改码的品置真,list_new 每轮对着几千行报「疑似平台强制下架」;② missing_times 灌水,风险档案失真。保留 missing_since 的写入是有意的:_settle 判 confirmed 的「旧码缺席」证据就取自它,抑制掉标记会让定案永远等不到。

**测试**:
- tests/test_catalog_sync.py::test_mark_missing_still_sets_missing_since_for_replaced_rows
- tests/test_catalog_sync.py::test_mark_missing_writes_no_item_missing_event_for_replaced_rows
- tests/test_catalog_sync.py::test_mark_missing_return_value_and_behaviour_unchanged_for_ordinary_rows

**验收**:python -m pytest tests/test_catalog_sync.py -q;python cli.py catalog_sync -p store=<试点店> --dry-run

#### O4 · `workflows/problem_scan.py` · 77-83(_SQL_ITEMS;现行 SQL **无表别名**,`FROM catalog.walmart_items`)

**改动**:给主表加别名 `w` 并把三列 SELECT 改成 `w.store, w.sku, w.unpublished_reasons`(列序不动 —— :174-176 的 `dict(zip(("store","sku","reasons"), r))` 按位置解包),WHERE 追加一条:`AND NOT EXISTS (SELECT 1 FROM catalog.listing_sources ls WHERE ls.store = w.store AND ls.sku = w.sku AND ls.replaced_by IS NOT NULL)`。SQL 上方 68-76 的注释加第四个边界:「在途改码的旧码不进扫描面 —— 改码期间沃尔玛可能把旧码置成非 PUBLISHED,那是改码的过程态不是问题商品;判不准就判活」。

**为什么**:改码生效有 15 分钟到 4 小时的窗口(官方),窗口内旧码可能被观测成 UNPUBLISHED 且 missing_since 仍为 NULL —— 正好落进 problem_scan「一切非 PUBLISHED 且未缺席」的扫描面(:77-83),当轮就被建议 DELETE_ITEM。删除不可逆,而我们**正在**改的就是这个 item:一次成功的改码会被自己的自动链当场删掉。这是整个批次里第一条能造成不可逆损失的竞态,必须在扫描面这一层堵死,不能靠调度顺序(conventions §三「调度顺序不许承载判据」)。原稿的 ddl 写成 `catalog.walmart_items.store` 是错的(现行 SQL 无别名也无法这样限定),已按实测改成加别名。

**测试**:
- tests/test_problem_scan.py::test_rows_being_replaced_are_out_of_the_scan_surface
- tests/test_problem_scan.py::test_ordinary_unpublished_rows_still_enter_the_scan_surface
- tests/test_problem_scan.py::test_items_sql_column_order_is_unchanged(钉住 :174-176 的位置解包)

**验收**:python -m pytest tests/test_problem_scan.py -q;python cli.py problem_scan --dry-run(改码 pending 期间跑,确认被改码的 SKU 不在建议里)

#### O5 · `workflows/problem_scan.py` · 124-131(_SQL_WFS_BLOCKED)、132-136(_SQL_LAST_CAT)、137-143(_SQL_STUBBORN);消费点 :181(_SQL_LAST_CAT)、:183(_SQL_STUBBORN)、:189(_SQL_WFS_BLOCKED)

**改动**:三段 SQL 各自改成「本码事件 UNION ALL 别名码事件」的形态,别名一律经 `catalog.sku_aliases`(S3 的视图)取,外层保持既有的 `DISTINCT ON (store, sku) … ORDER BY store, sku, <时间> DESC`(SQL 文本全文见 ddl 字段)。_SQL_STUBBORN 与 _SQL_LAST_CAT 的别名侧 JOIN catalog.product_events;_SQL_WFS_BLOCKED 的别名侧 JOIN ops.feed_items。**参数占位符形态不变**(_SQL_LAST_CAT 与 _SQL_WFS_BLOCKED 现在各用一个 `%s`,UNION 后同一个值要用两次 ⇒ 改成命名占位符 `%(ev)s` / `%(code)s`,:181 与 :189 的 execute 实参跟着从元组改成 dict)。三段的注释各补一句:改码后新码没有自己的历史,代际经登记簿 replaces 链继承一跳(旧码改码后即弃码、永不再改码,故一跳足够;连改两次要改成递归 CTE,见 S3 视图注释)。

**为什么**:三段判据全部按 (store, sku) 读历史,改码后新码是一张白纸:顽固加压丢失(已实证的故障模式,注释在 :144-148)、问题归类每次都当第一次见、WFS 拦截失效导致每天重发同一个删不掉的 DELETE_ITEM 并烧 6/hour 的桶(:115-119 注释记着生产实见 11 条)。用视图而不是三段各写一遍 JOIN,是因为「这个新码继承那个旧码」是一条判据,判据只能有一处出生。占位符改命名式是实测出来的必要动作:原稿没提,照抄会 ProgrammingError。

**测试**:
- tests/test_problem_scan.py::test_stubborn_marker_follows_the_replacement_chain
- tests/test_problem_scan.py::test_last_category_follows_the_replacement_chain
- tests/test_problem_scan.py::test_wfs_block_follows_the_replacement_chain
- tests/test_problem_scan.py::test_three_history_sqls_are_unchanged_when_sku_aliases_is_empty
- tests/test_problem_scan.py::test_item_appeared_on_the_new_code_would_clear_the_generation(反向钉住 O1)

**验收**:python -m pytest tests/test_problem_scan.py -q

#### O6 · `workflows/problem_scan.py` · 102-114(_SQL_INFLIGHT;消费点 :177-180)

**改动**:同 O5 的形态:在 `FROM ops.feed_items f JOIN catalog.walmart_items w ON w.store = f.store AND w.sku = f.sku` 之外 UNION ALL 一段别名侧 —— `FROM catalog.sku_aliases a JOIN ops.feed_items f ON f.store = a.store AND f.sku = a.alias_sku JOIN catalog.walmart_items w ON w.store = a.store AND w.sku = a.sku`,输出 `a.store, a.sku` 作为分组键,其余条件(status/submitted_at 48h/resolved_at > w.last_seen_at)逐字照抄。外层 `GROUP BY store, sku` 与 `bool_or(...) AS disposal` 不变。SQL 上方 :95-101 的在途口径注释补一句:改码后新码继承旧码的在途 feed —— 旧码上还没落定的上架/维护/处置 feed 仍然指着同一个 item,新码不该被当成「从没提交过任何东西」。

**为什么**:**采纳审查者 3 的 missing**,原稿完全没覆盖这一处。实测 :102-114 的在途防重按 (store, sku) 把 ops.feed_items 关到 catalog.walmart_items;改码 confirmed 之后新码行在 feed_items 里没有任何历史,旧码上在途的 MP_ITEM / MP_MAINTENANCE feed 就拦不住新码行了 —— 而该 SQL 的在途口径**有意不分 feed 类型**(注释 :95-99 写明「上架 feed 在途也算在途…QARTH 复审期内追发 DELETE_ITEM 属于过早」)。W2 的前置闸(第 6 道,见下)挡的是「提交改码时不许有在途」,挡不住「改码定案之后、旧 feed 才落定」这段;两条一起才把窗口关严。

**测试**:
- tests/test_problem_scan.py::test_inflight_follows_the_replacement_chain
- tests/test_problem_scan.py::test_inflight_sql_unchanged_when_sku_aliases_is_empty

**验收**:python -m pytest tests/test_problem_scan.py -q

#### O7 · `services/product_events.py` · 125-126(_RECEIPT_KINDS / _MAINT_LEDGER_WORKFLOWS)、133-142(receipt_in_ledger)

**改动**:receipt_in_ledger 函数体(140-142)第一行之前加:`if (workflow or "") == "sku_migrate": return False`,并在 docstring(134-139)补一段:改码回执不进病历 —— 改码的**事实**是观测定案(sku_replaced / sku_abandoned),不是沃尔玛回执;形态 B 走 MP_ITEM 时若不挡,会记出一串 list_feed_success,把「上架」病历和 product_risk 的 submit_times(refdata/schema.sql:438-439)一起灌水。`_MAINT_LEDGER_WORKFLOWS`(:126)**不加** sku_migrate。

**为什么**:两种形态下 kind 不同(A=maintenance 本就不入账、B=list 恒入账,见 :125 的 _RECEIPT_KINDS),不在 receipt_in_ledger 这一处统一挡住,形态一切换病历就被污染,而且是静默的。这一处是回执入账的唯一收口点(services/feed_track.py:173-174 调它),挡在这里就两种形态都覆盖。

**测试**:
- tests/test_product_events.py::test_sku_migrate_receipts_never_enter_the_ledger_in_either_feed_form
- tests/test_feed_track.py::test_sku_migrate_receipt_writes_no_product_event

**验收**:python -m pytest tests/test_product_events.py tests/test_feed_track.py -q

#### O8 · `services/feed_track.py` · 181-188(prohibited 列表推导;`meta[sku][0]` 是提交来源 workflow,见 :171 的用法)

**改动**:列表推导的过滤条件(186-188)追加 `and meta[sku][0] != "sku_migrate"`,并在上方 175-180 的注释里补一句:改码失败不是政策违禁,不得反哺黑名单。

**为什么**:形态 B 下改码走 MP_ITEM,`product_events.feed_kind(meta[sku][1]) == "list"`(:187)正好命中这段「上架回执违禁 → 自动进 ASIN 黑名单(B=禁售)」的反哺。一次改码被拒若碰巧带上违禁码,会把一个正在正常销售的 ASIN 永久拉黑(:190 的 record_asins 是 PERMANENT),而 list_new/match_listing 的黑名单闸下一轮就开始拦 —— 后果是永久的,且没有任何摘要会说是改码干的。

**测试**:
- tests/test_feed_track.py::test_sku_migrate_failure_never_blacklists_the_asin
- tests/test_feed_track.py::test_list_new_prohibited_feedback_still_works

**验收**:python -m pytest tests/test_feed_track.py -q

#### O9 · `services/dispositions.py` · 815(文件末尾,stuck_note 结束)

**改动**:新增 `open_executing_count(conn, store: str) -> int`,docstring 首行「输入:连接 + 店 → 输出:该店 status='executing' 的处置行数(改码前置闸用)」。SQL:`SELECT count(*) FROM ops.dispositions WHERE store = %s AND status = 'executing'`。只读。

**为什么**:改码前置闸「该店无 executing 行」的 SQL 必须出生在 dispositions 这个所有权模块里,不能在 sku_migrate 里现写 —— 处置状态机的判据散出去正是 08-19 那次「谁也说不清是哪条链干的」的成因(refdata/schema.sql:1277-1280 记着这条教训)。用计数而不是取行,是因为闸只需要 0/非 0。

**测试**:
- tests/test_dispositions_router.py::test_open_executing_count_only_counts_executing_rows_of_that_store

**验收**:python -m pytest tests/test_dispositions_router.py -q

#### O10 · `services/dispositions.py` · 紧接 O9 之后(文件末尾)

**改动**:新增 `rekey_suggested(conn, store: str, old_sku: str, new_sku: str) -> tuple[int, list[str]]`,docstring 首行「输入:连接 + 店 + 新旧码 → 输出:(迁走的建议行数, 因唯一索引冲突未迁的 action 列表)」。行为:① 先查该店 new_sku 名下已存在的未落定 action 集合(`status IN ('suggested','executing')`);② `UPDATE ops.dispositions SET sku = %(new_sku)s, asin = coalesce(asin, %(asin)s) WHERE store=%s AND sku=%(old_sku)s AND status='suggested' AND action <> ALL(%(taken)s::text[])`;③ 剩下的(action 撞车)**不迁、不删**,返回 action 列表让调用方点名人工,并 logger.warning。**executing 行本函数不碰**(前置闸已排除,注释写死理由)。

**为什么**:**采纳审查者 2 的 major**:原稿在工作流里裸写 `UPDATE ops.dispositions SET sku=…`,绕过处置所有权模块,而 refdata/schema.sql:1313-1315 上有 `dispositions_open_uidx (store, sku, action) WHERE status IN ('suggested','executing')` —— 撞上就抛,原稿没有分支;而且 asin 列(:1263)没跟着更新。同一批次已经为读侧建了 O9,写侧留在工作流里是自相矛盾。撞车不自动合并、只点名,是 conventions §五「判不准就判活」:两条同动作的建议合成一条会让其中一个的落定结果覆盖另一个(schema.sql:1304-1311 明写这条设计)。

**测试**:
- tests/test_dispositions_router.py::test_rekey_suggested_moves_only_suggested_rows
- tests/test_dispositions_router.py::test_rekey_suggested_skips_and_reports_action_collisions
- tests/test_dispositions_router.py::test_rekey_suggested_never_touches_executing_rows

**验收**:python -m pytest tests/test_dispositions_router.py -q

#### O11 · `services/walmart_catalog.py` · 104-105(upsert_node_inventory 结束于 103 `return len(payload)`,106 起是 upsert_items)——插在 104-105 空行处

**改动**:新增 `drop_node_rows(conn, store_name: str, sku: str) -> int`,docstring 首行「输入:连接 + 店 + SKU → 输出:删除的分节点库存行数(**只给改码定案用**:该 SKU 已不存在于沃尔玛,留着就是永不更新的幽灵行)」。SQL:`DELETE FROM catalog.item_node_inventory WHERE store = %s AND sku = %s`。docstring 正文写死:这是 catalog.item_node_inventory 的**唯一删除出口**,与 refdata/schema.sql:1319-1322 的「本轮没扫到的行不删」不冲突 —— 那条讲的是分页漏 SKU,这里讲的是我们自己把这个 SKU 改没了。

**为什么**:**采纳审查者 2 的 major**:原稿在工作流里裸写 `DELETE FROM catalog.item_node_inventory`,而这张表的所有权在 services/walmart_catalog(:86-103 的 upsert_node_inventory 是它唯一写入方)。catalog_sync 对没扫到的行永不删(schema.sql:1319-1322 的成文纪律),留着旧码行就是一条永远不会更新的幽灵节点库存,维护链的受管仓判据会读到它。

**测试**:
- tests/test_catalog_sync.py::test_drop_node_rows_removes_only_that_store_and_sku

**验收**:python -m pytest tests/test_catalog_sync.py -q

#### O12 · `services/alloc_survey.py` · 236-245(_SQL_SALES);消费点 :515、:523、:652;执行点 :790-791

**改动**:_SQL_SALES 的 `GROUP BY store, sku` 之前加一次别名映射:`FROM orders.order_lines o LEFT JOIN catalog.sku_aliases a ON a.store = o.store AND a.alias_sku = o.sku`,输出键改为 `o.store AS store, coalesce(a.sku, o.sku) AS sku`(改码后旧码的历史销量归到新码名下),GROUP BY 同步改成 `1, 2`。其余条件(order_date 窗口 :241-242、`sale_status <> 'Cancelled'` :243)一字不动。SQL 上方补注释:改码后 catalog.walmart_items.sku 变成新码而 orders.order_lines 的历史行仍是旧码,不映射的话迁过码的品销量/GMV 恒 0;映射走 catalog.sku_aliases 这一条继承链,与 problem_scan 四段判据同源。**:790-791 的 `sales = {(s, k): …}` 与三个消费点一字不改**(键的形状没变)。

**为什么**:**采纳审查者 3 的 missing**,原稿与 sku_plan §7 的「受牵连的键表」清单都漏了这一处。实测 :236-245 按 (store, sku) 把历史订单销量挂到在架行上,消费点 :515(店铺产出)、:523(resolve_conflicts 按销量选冲突赢家)、:652(alloc_audit 明细)。改码 confirmed 之后 walmart_items.sku 是新码、order_lines 历史行是旧码 ⇒ 迁过码的品销量恒 0,冲突裁决、店铺产出、审计明细全部对迁移过的品失真,**而且不报错**。对照组:services/product_pool.py 已按 orders.order_lines.asin 聚合、不受影响 —— 说明正确写法仓内已有先例;这里不改聚合键而是加别名映射,是因为改聚合键会连带改 :515/:523/:652 三个消费点的键形状(那是行为变化,不属于本批次)。

**测试**:
- tests/test_alloc_engine.py::test_sales_of_a_migrated_sku_follow_the_replacement_chain
- tests/test_alloc_engine.py::test_sales_sql_is_unchanged_when_sku_aliases_is_empty

**验收**:python -m pytest tests/test_alloc_engine.py tests/test_alloc_audit.py -q;改码前后各跑 python cli.py alloc_survey --dry-run 对比该店销量维度

#### X1 · `services/order_lines.py` · 659(文件末尾,backfill_perf_line_ids 结束)

**改动**:新增 `duplicate_po_lines(conn, days: int | None = 120) -> list[dict]`,docstring 首行「输入:连接(+回看天数,None=不限)→ 输出:同 (store, po_id, line_number) 出现多个 order_line_id 的行组」。**函数体只读 S5 的视图**:`SELECT store, po_id, line_number, n, skus FROM orders.v_order_line_dupes WHERE %(days)s IS NULL OR first_order_date > now() - make_interval(days => %(days)s) ORDER BY n DESC, store, po_id LIMIT 200`。docstring 写死:判据在视图里,本函数只是加窗口与排序的薄壳,**不许在这里重写 GROUP BY/HAVING**。

**为什么**:orders.order_lines 的身份是 order_line_id,唯一约束是 (po_id, sku)(refdata/schema.sql:632、:662)。若沃尔玛对**改码之前的 PO** 日后返回新码,那一行会被当成新行插入而旧行不删 ⇒ 同一笔销售被算两次(销量、产品分、日报、对账全受影响),而且**不报错**。官方没有一个字说改码后旧 PO 会返回哪个码(实测清单第 6 件),所以只能用体检兜住。**改成读视图是采纳两位审查者的 major**:原稿在这里重写了一遍 GROUP BY/HAVING,与横切包的视图形成两份口径不同的实现,而这条体检正是发现双算的唯一手段。

**测试**:
- tests/test_order_lines.py::test_duplicate_po_lines_flags_two_skus_under_one_po_line
- tests/test_order_lines.py::test_duplicate_po_lines_is_empty_on_healthy_data
- tests/test_order_lines.py::test_duplicate_po_lines_reads_the_view_not_a_local_group_by

**验收**:python -m pytest tests/test_order_lines.py -q;psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT * FROM orders.v_order_line_dupes LIMIT 20;"(改码前后各跑一次,基线必须一致)

#### W1 · `workflows/sku_migrate.py` · 新建文件,1-90(头注 + import + 模块常量)

**改动**:文件头注按 workflows/product_clear.py、workflows/sku_locked_heal.py 同款体例写:一句话定位「存量 SKU 迁到不透明码(批次 3);危险:缺省即真跑,空跑用 --dry-run」+ 用法四行 + 三态状态机图 + 六条安全约束 + **前置清单**(批次 0a/0b/1/2 已合并且新码在生产跑过一轮;实测六件通过;改码前必须先停该店的手动上架动作;**旧仓 product_clear/daily_cleanup/auto_listing 调度已停**)。模块级:`DANGEROUS = True`;**不声明 SUPPORTS_STORE**(注释写明理由:改码是一次性、按批、人盯的动作,cli.py:454-519 的链尾对缺席店逐店自动重赛对它没有意义而且危险)。常量(全部带官方/实证出处注释):`FEED_TYPE = "MP_MAINTENANCE"`(形态 A;切形态 B 只改这一处 + _build_items 的分支,注释写明实测依据与切换条件,**不提供参数覆盖**以免双轨)、`SOURCE_TYPES = (listing_sources.SOURCE_AMZ,)`(跟卖是否迁 = 决策 D,默认不迁)、`OBSERVE_HOURS = 24`(官方 15min~4h 生效,取 24h 留量)、`STALE_HOURS = 72`(超期不自动定案,点名人工)、`FEEDS_PER_STORE_PER_RUN = 2`(MP_MAINTENANCE 桶 8/hour,api/_client.py:236,与维护链共享;形态 B 时 MP_ITEM 桶 8/hour,:238,与 list_new 共享,留 6 给主链)、`DEFAULT_LIMIT = 10`、`INFLIGHT_HOURS = 48`(与 problem_scan._SQL_INFLIGHT 的 48h 口径同源,workflows/problem_scan.py:105-106)。import 只从 api/services/registry 取(api: feeds;services: sku_codec, listing_sources, listing_sheet, upc_pool, dispositions, walmart_catalog, order_lines, product_events, feed_track, store_absence, stores, mp_mapper, notify_fmt;registry: db, resources),**不 import 任何 workflows**。

**为什么**:头注是本仓工作流的契约面;DANGEROUS/SUPPORTS_STORE 是 cli 的开关(cli.py:303-311、:501),写错一个字就是「调度里空转报成功」或「链尾自动把改码重跑一遍」。常量集中在文件顶部且单一出处,是为了让形态 A→B 的切换只有一个改动点(conventions §六)。配额留量必须写进代码而不是留给人记:MP_ITEM 桶被吃光的表现是 list_new 当晚上不了架,而且摘要只会说「配额不足」。前置清单里加「旧仓调度已停」是采纳审查意见 —— CLAUDE.md 安全红线「新旧系统严禁对同一破坏性任务并跑」原本只在 depends_on 里以散文出现。

**测试**:
- tests/test_sku_migrate.py::test_module_flags_dangerous_true_and_no_supports_store
- tests/test_sku_migrate.py::test_workflow_imports_no_other_workflow(AST 断言,同 tests/test_readme.py 的守门写法)
- tests/test_sku_migrate.py::test_feed_type_constant_is_the_only_place_that_names_a_feedtype

**验收**:python cli.py sku_migrate --dry-run -p store=<试点店>(应打印 🧪 [DRY-RUN] 前缀与前置闸结论,不写任何库)

#### W2 · `workflows/sku_migrate.py` · 新建文件,_preflight 段

**改动**:`_preflight(conn, store_name) -> tuple[bool, list[str]]`,**六道闸**全过才放行,任一不过 ⇒ 整店本轮不迁并在摘要点名(不抛异常):① 在营:`store_name in services.stores.enabled_names()`(唯一在营判据,CLAUDE.md 工程规范);② 目录水位新鲜:`services.store_absence.stale_or_note(conn, only=store_name)`(services/store_absence.py:92)判该店不在缺席集;③ 处置无在途:`dispositions.open_executing_count(conn, store_name) == 0`(O9);④ 自愈链无在途:`SELECT count(*) FROM listing.retire_cooldown WHERE store=%s AND status='pending'` 为 0(refdata/schema.sql:566-577);⑤ 本工作流无在途 feed:`feeds.query_pending()`(api/feeds.py:240)里没有 (workflow='sku_migrate', store=该店) 的行;⑥ **该店本轮候选的旧码上没有任何在途 feed**:`ops.feed_items` 里该 (store, old_sku) 无 `status='submitted' AND submitted_at > now() - interval '<INFLIGHT_HOURS> hours'` 的行 —— 口径与 workflows/problem_scan.py:102-114 同源,这一道是**逐候选**判(不过的候选跳过并点名,不整店拦)。每道闸的失败文案写清「为什么不能改」而不是只报 False。

**为什么**:①②是本仓既有的两条唯一判据,照用不新造。③是所有者点名的闸:executing 行意味着一条 DELETE/RETIRE feed 已经在沃尔玛队列里指着**旧码**,这时改码 = 那条 feed 打空、dispositions 永远等不到观测判决、部分唯一索引(schema.sql:1313-1315)把该 SKU 的这类处置永久堵住。④同理:retire_cooldown 的 pending 行也指着旧码。⑤是「防重状态先落库再调接口」的启动对账要求:上一轮结局不确定就不许发新的。**⑥是采纳审查者 3 的 missing**:原稿只挡三样,不挡上架/维护在途 —— 而 problem_scan 的在途口径**有意不分 feed 类型**,一条刚发出去的 MP_ITEM/MP_MAINTENANCE 在途时改码,会让那条 feed 打在一个即将不存在的 SKU 上。

**测试**:
- tests/test_sku_migrate.py::test_disabled_store_is_refused
- tests/test_sku_migrate.py::test_stale_catalog_watermark_blocks_the_store
- tests/test_sku_migrate.py::test_executing_disposition_blocks_the_store
- tests/test_sku_migrate.py::test_open_retire_cooldown_blocks_the_store
- tests/test_sku_migrate.py::test_pending_feed_log_row_blocks_the_store
- tests/test_sku_migrate.py::test_candidate_with_an_inflight_feed_is_skipped_and_named

**验收**:python cli.py sku_migrate --dry-run -p store=<有 executing 行的店>(摘要必须点名被哪道闸拦住)

#### W3 · `workflows/sku_migrate.py` · 新建文件,_settle 段(每次调用最先跑,settle_only=1 时只跑这一段)

**改动**:`_settle(conn, store_name, execute: bool) -> tuple[dict, list[str]]` —— **签名带 execute**。对该店全部 `listing.sku_migrations` 里 status='pending' 且 submitted_at IS NOT NULL 的行逐条判(证据 SQL 见 ddl `_SQL_OBSERVE`),判据优先级固定:先取观测新鲜度 `fresh`(至少一轮 catalog_sync 在提交之后跑完过:该店存在 `last_seen_at > submitted_at` 的 walmart_items 行),再取 `new_present`(新码行存在且 missing_since IS NULL)与 `old_gone`(旧码无行 或 missing_since IS NOT NULL),再取回执 `feed_track.item_results(feed_id).get(new_sku)`。判决:(a) new_present 且 old_gone ⇒ **confirmed**;(b) new_present 且 not old_gone ⇒ **不定案 + 摘要首行响亮告警「同店双挂」**;(c) not new_present 且回执 status='failed' ⇒ **rolled_back**(error 记回执码);(d) not new_present 且 fresh 且 now-submitted_at > OBSERVE_HOURS ⇒ **rolled_back**;(e) now-submitted_at > STALE_HOURS 仍判不出 ⇒ 标 status='stalled' 并点名人工,**不自动定案**;(f) 其余保持 pending。
**execute=False(dry-run)时:六处写全部跳过**(settle_replacement / retag_sku / rekey_suggested / drop_node_rows / ledger UPDATE / 飞书 write_sku_col),只算判决并打印「将定案 N 条 confirmed、M 条 rolled_back」。
execute=True 时,confirmed 分支在**一个 `with db.pg_conn() as conn` 事务**里按序做完:`sku_codec.settle_replacement(...,'confirmed')` → `upc_pool.retag_sku(conn, [(store, source_key, new_sku)])`(C5) → `dispositions.rekey_suggested(conn, store, 旧码, 新码)`(O10,撞车的 action 进摘要) → `walmart_catalog.drop_node_rows(conn, store, 旧码)`(O11) → `UPDATE listing.sku_migrations SET status='confirmed', settled_at=now() WHERE id=%s`。事务外再做飞书 V 列回写(批次 1 的单列写函数,按批次 1 定的实际签名接;**若批次 1 定的名字不是 `write_sku_col` 就按实际名字接,不新写一个**),写成功才 `UPDATE listing.sku_migrations SET sheet_synced_at = now()`;**每轮开头先补写** `status='confirmed' AND sheet_synced_at IS NULL` 的行。rolled_back 分支同事务里 `settle_replacement(...,'rolled_back')` + ledger 落 rolled_back;**不做任何补交**(写操作永不自动兜底),摘要点名让人决定是否下一轮重发(重发会抽一个新码,旧的失败码永久弃用)。

**为什么**:「回执成功不定案、观测说了算」是本仓已经用血换来的口径(delete_not_effective 是实证的故障模式,refdata/schema.sql:1257-1259 记着「不信回执信观测」)。分支 (b) 是整条规则里唯一的双上架入口,必须是告警而不是自动处置。分支 (e) 落 'stalled' 而不是自动 rolled_back,是 conventions §五「判不准就判活」:回滚一个其实已经生效的改码,会让登记簿说旧码当前、沃尔玛说新码当前,新码在 sources_backfill 眼里成 unknown 出身、从此退出全部自动化,而且不报错。**execute 参数是采纳审查者 2 的 blocker**:原稿 _settle 一个字没写 dry-run 分支,却是全包写得最重的一段,而它自己的验收命令带 `--dry-run`、guard_tests 里还挂着 `test_dry_run_touches_neither_db_nor_walmart` —— 规格与验收互相打架,执行者必须自己补设计决定。**dispositions/节点库存改调 O10/O11 而不是裸 SQL**,以及**飞书补写路径**,同为采纳审查意见(见 O10/O11/S2 的 why)。飞书写在事务外是因为外部 IO 不进数据库事务(飞书失败下一轮补写,登记簿不会因此回滚)。

**测试**:
- tests/test_sku_migrate.py::test_confirmed_needs_new_present_and_old_gone_and_a_fresh_sweep
- tests/test_sku_migrate.py::test_receipt_success_alone_does_not_settle
- tests/test_sku_migrate.py::test_both_codes_live_raises_a_double_listing_warning_and_does_not_settle
- tests/test_sku_migrate.py::test_failed_receipt_rolls_back_and_abandons_the_new_code
- tests/test_sku_migrate.py::test_timeout_after_a_fresh_sweep_rolls_back
- tests/test_sku_migrate.py::test_stalled_is_named_for_humans_and_never_auto_settled
- tests/test_sku_migrate.py::test_confirmed_retags_upc_rekeys_dispositions_and_drops_node_rows
- tests/test_sku_migrate.py::test_confirmed_writes_the_v_column_after_the_transaction_and_stamps_sheet_synced_at
- tests/test_sku_migrate.py::test_unsynced_sheet_rows_are_retried_next_round
- tests/test_sku_migrate.py::test_settle_in_dry_run_writes_nothing_and_calls_no_feishu
- tests/test_sku_migrate.py::test_settle_never_resubmits_anything

**验收**:python cli.py sku_migrate -p store=<试点店> -p settle_only=1 --dry-run(必须零写库、零飞书),人眼确认判决后再真跑;psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT status, count(*) FROM listing.sku_migrations GROUP BY 1;"

#### W4 · `workflows/sku_migrate.py` · 新建文件,_candidates / _build_items / _migrate 段

**改动**:候选 SQL `_SQL_CANDIDATES`(全文见 ddl):walmart_items 在架(missing_since IS NULL)× listing_sources 活码(abandoned_at IS NULL AND replaced_by IS NULL)× source_type = ANY(SOURCE_TYPES) × `NOT <sku_codec.OPAQUE_SQL_PREDICATE.format(col="w.sku")>`(C6,语义唯一出处) × 该 (store, old_sku) 在 sku_migrations 里无 pending/confirmed/stalled 行 × `w.upc IS NOT NULL OR w.gtin IS NOT NULL`,按 sku 排序取 limit。**Product ID 取 catalog.walmart_items 的 upc(空则 gtin),不取 upc_pool**;两列都空 ⇒ 跳过并点名(不猜)。取到候选后逐条过 W2 第⑥道闸(在途 feed)。
**事务边界写死**(与原稿不同):① `with db.pg_conn() as conn:` 里对全部候选做 `sku_codec.mint_replacement` + `INSERT INTO listing.sku_migrations (... status='pending', feed_id=NULL)`;**该 with 在组载荷之前退出并 commit**(registry/db.py:19-40 的 pg_conn 是正常退出才 commit);② 组载荷:形态 A 走 `mp_mapper.build_sku_update_item(new_sku, product_id, product_id_type)`;形态 B 走 `mp_mapper.build_orderable(..., sku_update=True)` + `mp_conform.conform` + `mp_mapper.assemble_mp_item`(数据来自 catalog.products 的该 ASIN,与 list_new 同一套积木,**不 import list_new**);③ `feeds.submit_feed(store, FEED_TYPE, items, workflow="sku_migrate")`,配 `feeds.iter_result_slices`(api/feeds.py:313)逐片对位;④ 另开短事务按 outcome 落账:submitted/dedup 且有 feed_id ⇒ UPDATE ledger 落 feed_id + submitted_at;failed(4xx 或 `_PRE_FAIL`,api/feeds.py:337、:491,**确认未达**)⇒ 当场 `settle_replacement(...,'rolled_back')` + ledger rolled_back;unknown ⇒ **保持 pending 不回滚**(api/feeds.py:445 的既定处置)。单店本轮最多发 FEEDS_PER_STORE_PER_RUN 个 feed。dry-run:不 mint、不写 ledger、不提交,用占位码(`sku_codec.placeholder(...)`,批次 0a/2 定的那一个,**不自造第二种占位码**)打印将迁哪些、载荷形态、Product ID 来源。

**为什么**:「先落库再调接口」是安全铁律,顺序不可调换。**事务边界是采纳审查者 2 的 blocker**:原稿写「① mint + INSERT(同事务)② 组载荷 ③ submit_feed」,全程没说 commit 在 ③ 之前;registry/db.py:19-40 默认「正常退出才 commit」,照字面实现就是在**未提交事务里 POST** —— 进程死在 POST 之后、with 退出之前,新码行与 pending 台账全部 rollback,而沃尔玛已经受理,新码成了没有出身的孤儿行(正是决策 F 自己论证要避免的形态)。failed 与 unknown 的处置**必须不同**:4xx/_PRE_FAIL 是 api/feeds 已经判定的「确认未达」,回滚安全;unknown 是「不知道到没到」,回滚会造成最坏后果。Product ID 取观测值而不是池值,是因为改码按 Product ID 匹配,池里的号若与沃尔玛现挂的号不一致(历史换过号),载荷会匹配到别的 item 或直接被拒。候选正则改用 C6 的派生常量,是为了不在这里出生第二份字母表。

**测试**:
- tests/test_sku_migrate.py::test_registry_rows_are_committed_before_submit_feed_is_called(桩记录调用序 + commit 时点)
- tests/test_sku_migrate.py::test_payload_uses_the_new_code_and_the_observed_product_id
- tests/test_sku_migrate.py::test_candidate_without_upc_and_gtin_is_skipped_and_named
- tests/test_sku_migrate.py::test_opaque_codes_are_never_candidates
- tests/test_sku_migrate.py::test_match_source_rows_are_excluded_by_default
- tests/test_sku_migrate.py::test_4xx_and_pre_fail_roll_back_immediately
- tests/test_sku_migrate.py::test_unknown_outcome_stays_pending_and_is_not_rolled_back
- tests/test_sku_migrate.py::test_slice_results_line_up_with_their_own_rows
- tests/test_sku_migrate.py::test_feeds_per_run_cap_is_enforced
- tests/test_sku_migrate.py::test_dry_run_mints_nothing_and_posts_nothing

**验收**:python cli.py sku_migrate --dry-run -p store=<试点店> -p limit=1;人眼确认载荷 sku 是占位码、productIdentifiers 是该品现挂的号;再真跑 python cli.py sku_migrate -p store=<试点店> -p limit=1

#### W5 · `workflows/sku_migrate.py` · 新建文件,_stage_cap 段(在 _migrate 取候选之前施加)

**改动**:`_stage_cap(conn, store_name, asked_limit) -> tuple[int, str]`:查该店 `listing.sku_migrations` 中 status='confirmed' 的条数 n;n==0 ⇒ 上限 1;0<n<10 ⇒ 上限 10;n>=10 ⇒ 上限 = asked_limit(不再压)。返回生效上限与一句人话说明,进摘要。另一条硬闸:该店存在 status IN ('pending','stalled') 的行时,本轮**不提交新的**(只跑 _settle),摘要说明「上一批未定案,先把账清干净」。

**为什么**:所有者定的节奏是 1 → 10 → 一店 → 全店。纪律没有默认值替你挡(CLAUDE.md 安全红线「缺省即真跑…这条纪律,没有默认值替你挡」),把节奏写成代码里的闸,漏敲一个 limit 也不会一次改一千个。第二条闸是三态状态机的自我保护:pending 未清就发下一批,一旦形态选错就是成批的双挂,而双挂只能人工一条条收。(审查者建议批次 2 照抄这个形状给 list_new 加 limit —— 那条已转给批次 2,本批不承载。)

**测试**:
- tests/test_sku_migrate.py::test_first_batch_is_capped_at_one
- tests/test_sku_migrate.py::test_second_stage_is_capped_at_ten
- tests/test_sku_migrate.py::test_no_new_submissions_while_pending_or_stalled_rows_exist

**验收**:python cli.py sku_migrate --dry-run -p store=<新店> -p limit=100(摘要必须说本轮上限被压到 1)

#### W6 · `workflows/sku_migrate.py` · 新建文件,run(params) 段

**改动**:`run(params: dict) -> str`,docstring 首行「输入:params(store 必填 / limit / settle_only / observe_hours / stale_hours)→ 输出:定案 + 提交摘要」。`store` **必填**,缺省直接返回 `⛔` 开头的提示不执行(cli.py:350/366 的 REFUSED_MARK 早退机制,不抛异常)。执行序:读参(`execute = bool(params.get("execute", True))`,由 cli.py:307 注入)→ _preflight → `_settle(conn, store, execute)` → _stage_cap → `_migrate(store, rows, execute)` → 订单体检 `order_lines.duplicate_po_lines(conn)`(X1)→ 组摘要。摘要用 `services/notify_fmt.head/summary`(services/notify_fmt.py:89、:113),**首行**必须容纳:定案 confirmed/rolled_back/stalled 计数、本轮提交数、以及四类必须见人的告警(同店双挂 / stalled 超期 / 订单双行体检非零 / 飞书 V 列未同步条数);dry-run 前缀 `🧪 [DRY-RUN] ` **拼在首行行首**(不许用 `lines.insert(0, ...)` 把告警顶到抬头行之前 —— cli 的链通知只取首行 `notify_fmt.first_line_of`,那样会让一条 dry-run 摘要以真跑的面目出现)。零候选不算失败;前置闸拦住整店也不抛异常,只在摘要点名。

**为什么**:cli 对成功步骤只发首行(cli.py:519 附近的 _fold_success 走 notify_fmt.first_line_of),告警不在首行等于只写日志。store 必填是把「一店一批」从口头节奏变成接口约束。用 notify_fmt 的标准件是 conventions §六对新写工作流的要求。dry-run 前缀位置是采纳审查者 3 对 0b-21 同款问题的意见,提前在本批次避免。

**测试**:
- tests/test_sku_migrate.py::test_store_param_is_required_and_refuses_with_the_refused_mark
- tests/test_sku_migrate.py::test_first_line_carries_double_listing_stalled_and_sheet_lag_warnings
- tests/test_sku_migrate.py::test_dry_run_prefix_stays_at_the_head_of_the_first_line
- tests/test_sku_migrate.py::test_zero_candidates_is_success_not_failure

**验收**:python cli.py sku_migrate --dry-run -p store=<试点店>;python -m pytest tests/test_sku_migrate.py -q

#### R1 · `registry/schedule.py` · 24-27(「不在表里的一律**手动**」清单;26 行 `自愈(sku_locked_heal)、一次性迁移与体检`)

**改动**:26 行的 `自愈(sku_locked_heal)` 之后加 `、存量改码(sku_migrate)`,并加一句括注:改码按批、需人盯定案,**永不进调度**。JOBS 元组(74 行起)不加任何条目。

**为什么**:调度表是唯一出处(文件头注第 1 行自述「全项目唯一出处」),不写进这份清单的工作流在别人读表时会显得「被漏掉了」,下一个人很可能好心把它排进 product_chain(:167)—— 那就是让一条 DANGEROUS 的一次性迁移每天自动跑,并且和 list_new(:204,20:00)抢同一个 MP_ITEM 桶。

**测试**:
- tests/test_launchd.py::test_sku_migrate_is_not_scheduled(断言 JOBS 里没有 sku_migrate,且头注手动清单里有它)

**验收**:python -m pytest tests/test_launchd.py -q;python cli.py skill_export

#### R2 · `docs/db_schema.md` · 165-176(catalog.listing_sources 代码块)、182-190(catalog.upc_pool 代码块)、284-320(catalog.product_events)、321-341(listing 域)、434-463 对应的 product_risk 说明段、684-691(ops.cleanup_seen_categories)、692-700(ops.dedupe)、781-821(audit_listing_conflicts 段之后)

**改动**:① listing_sources 段(165-176)补 replaces / replaced_at 两列与两个新索引,并写死 replaced_by/replaces 的语义与「只由 services/sku_codec 写(mint_replacement / settle_replacement)」;② upc_pool 段(182-190)补一句:改码只改 sku 列(retag_sku),asin 列恒存 ASIN、status 与 used_at 不动;③ product_events 段(284-320)补 sku_replaced / sku_abandoned 的语义导览(以 services/product_events.py 的常量为准,此处只是导览,不复述清单——:295 已有这条纪律);④ listing 域段(321-341)新增 listing.sku_migrations 全表说明 + 与 listing_sources 的分工(身份 vs 过程账)+ sheet_synced_at 的用途;⑤ 新增 `catalog.sku_aliases` 视图登记 + product_risk 两个新列 + `orders.v_order_line_dupes` 视图登记(放在 781 之后的视图段);⑥ cleanup_seen_categories 段(684-691)补结论:**本批次不迁**——全仓只有 workflows/cleanup_history_import.py 写它、**零读者**(2026-09-02 grep 复核),改码不会让任何在跑的判据失效;将来若接报表消费方,键应走登记簿 source_key 而不是原文 sku;⑦ ops.dedupe 段(692-700)补三条改码结论:`maintenance:submitted` 键含 sku(services/maintenance_intents.py:511-525),改码后旧键失配、**自然过期**(SUPPRESS_HOURS=20 < 一轮观测期 24h),最坏是被改码的品在定案后多发一次同值维护意图(可能收到 0101198 stale update,非破坏),**不迁**;`cleanup:brand_asin` / `cleanup:brand_scrape` 键是 ASIN,**不受影响**;`catalog.claims.claim_key` 是 ASIN/品牌键(refdata/schema.sql:404-405),**不受影响**。

**为什么**:「动了表同步 docs/db_schema.md」是硬规矩(CLAUDE.md 工程规范)。⑥⑦ 尤其要写:sku_plan §7(:422-423)把 cleanup_seen_categories 列成「按 replaced_by 迁一次」、把 drop_recent 一笔带过,复核后发现前者当前无读者(迁数据反而会让那张 (sku, category) 唯一对表的累计计数多算)、后者自然过期、claims/dedupe 另两组键根本不含 SKU —— **结论与依据必须留下**(采纳审查者 1 与 2 的 missing),否则下一轮盘点又会把它们当成待办。

**测试**:
- tests/test_readme.py(若已有 docs 与 schema 对齐的守门断言,补 listing.sku_migrations / catalog.sku_aliases / orders.v_order_line_dupes 三条)

**验收**:diff 人眼过;grep -n 'sku_migrations\|sku_aliases\|v_order_line_dupes\|replaces' docs/db_schema.md refdata/schema.sql

#### R3 · `docs/api_blueprint.md` · 26(端点矩阵 #10 `POST /v3/feeds?feedType=MP_MAINTENANCE`)、73-90(工作流×端点矩阵,末行 88 是 `| 10 list_new |`)、128(配额表 MP_MAINTENANCE 行)、192(MP_MAINTENANCE 官方限制)

**改动**:① 26 行 #10 的「用途」补 `/ 改 SKU(SkuUpdate=Yes,按 Product ID 匹配)`;② 工作流矩阵在 88 行「10 list_new」之后插一行:`| 11 sku_migrate | 10(形态 A)或 8(形态 B), 17 | 存量改码;载荷形态按单品实测定,常量在 workflows/sku_migrate.FEED_TYPE;回执不入病历,定案靠 2 的观测;形态 B 需 mp_conform 放行 SkuUpdate(见 M5) |`;③ 128 行配额行补一句:MP_MAINTENANCE 桶与维护链共享,sku_migrate 单店单轮上限 2 个 feed;④ 192 行「MP_MAINTENANCE 必填仅 SKU+GTIN」旁补 SkuUpdate 的官方出处(CA manage-items「look for the SkuUpdate attribute … set it to Yes」;US 侧 Update my existing items 未点名,故列为待实测)。

**为什么**:蓝图是端点与配额的定稿,api 层的收录规则也在这里(CLAUDE.md 铁律 2「只实现蓝图矩阵出现过的端点」)。不登记就等于 sku_migrate 用了一个「蓝图没批」的端点用法;配额共享关系不写下来,下一个人会以为 MP_MAINTENANCE 那 8/hour 是维护链独享的。

**测试**:
- tests/test_feeds.py::test_blueprint_matrix_covers_sku_migrate(若已有蓝图对齐守门测试则补一条,否则人眼)

**验收**:grep -n 'sku_migrate\|SkuUpdate' docs/api_blueprint.md

#### R4 · `docs/sku_plan.md` · 409-436(§7 批次 3;409 行 `**批次 3|存量产品改码…`)、439-475(§8 待决清单)、207-213(§4 待单品实测的三件事)、184-206(§4 官方证据段)

**改动**:① §7 批次 3(409-436)改写成本工作包的落地形态:三态判据表(confirmed/rolled_back/stalled 的证据组合)、抑制型/继承型改动清单(缺席事件、假代际、四段历史 SQL、销量归属)、节奏硬闸、形态 A/B 的两条接法 + 形态 B 的 strip_unknown 前置、以及**目标改写为「止血」**(存量改码收不回沃尔玛已掌握的旧关联,收益只覆盖切换后新上架的品);② 415 行「回执成功后:上架表 V 列回写…」那句改成「**观测确认后**才回写,且带 sheet_synced_at 补写路径」(与三态状态机一致);③ 422-423 的「受牵连的键表」逐条给结论:`ops.cleanup_seen_categories` **不迁**(2026-09-02 复核零读者,依据见 docs/db_schema.md:684-691)、`maintenance` drop_recent 防重键 **自然过期**(SUPPRESS_HOURS=20 < OBSERVE_HOURS=24,最坏多发一次同值维护意图)、`ops.dedupe` 另两个 scope 与 `catalog.claims` 键是 ASIN **不受影响**、`services/alloc_survey._SQL_SALES` **补入清单**(经 sku_aliases 继承,见 O12)、`workflows/problem_scan._SQL_INFLIGHT` **补入清单**(见 O6);④ §4(207-213)实测清单从三件扩到**六件**:加「对 lifecycle=RETIRED 的 item 是否可用」「改码**之前**的 PO 日后返回旧码还是新码」「MP_ITEM 形态下 SkuUpdate 是否在 Orderable spec 里(决定 M5 的必要性)」;⑤ §4(184-206)把「退役后旧 UPC 永久烧号」与「24h 冷却」标注为**本仓保守策略 / 旧仓实证,非官方规则**(依据 docs/legacy_survey.md:1649);⑥ §8(439-475)勾掉「存量产品」并新增决策 D(跟卖存量是否迁,默认不迁)、E(feed 形态,默认 A)、H(销量归属口径)、I(SkuUpdate 穿透 conform 的实现方式);⑦ §8 保留并明确 §3.5 那两条建议(退役表 B 列是否回显来源码、维护记录表是否加来源码展示列)的勾选状态,不让它们从待办里掉出去。

**为什么**:计划文档是这条改造的记忆;三态状态机与「回执成功即回写」是两套不同语义,留着旧句子下一个人会照旧句子实现。目标改成止血是所有者需要知道的真相:SkuUpdate feed 本身就是「旧串→新码」的显式映射,历史订单与 feed 记录里的关联收不回来。③④⑤⑦ 全部是采纳审查意见:原稿把 cleanup_seen_categories 的结论只写进 db_schema、把 drop_recent 与 §4 保守策略标注、§3.5 两条建议整条漏掉 —— sku_plan 是总账,留着与实现相反的旧稿,下一轮盘点会有人照旧稿去写迁移。

**测试**:
- (无)

**验收**:人眼 diff;grep -n '止血\|三态\|stalled\|自然过期\|零读者' docs/sku_plan.md

#### R5 · `docs/sku_plan.md(新增 §9「决策日志」)+ docs/conventions.md §六 末尾` · docs/sku_plan.md:475(文件末尾)追加 §9;docs/conventions.md:115-133(§六)末尾追加两条

**改动**:① docs/sku_plan.md 追加 §9「决策日志」(**实测确认 docs/plan.md 没有决策日志段**,它的记录方式是 Phase 小节里的 `[x] + 日期`;把批次 3 的决策记在 sku_plan 更贴切,plan.md 只在 Phase 2 表格里补一行指针):记 2026-09-XX 批次 3 落地形态、与 synthesis 的三处**有意出入**(POST outcome=unknown 不回滚 / cleanup_seen_categories 不迁 / 活码唯一索引由 0a 一次建到位而非批次 3 收紧)及其依据、九个决策点(A-I)的默认取值与所有者拍板结果留白、以及本次评审**驳回**的两条意见(见 risks)。② docs/conventions.md §六 末尾追加两条通用条款:「**POST outcome=unknown 一律保持 pending**,不回滚也不补交(api/feeds.py:445 的既定处置,人不在环时宁停不重)」与「**`abandoned_at IS NULL` 的允许出现处**:消费方 .py 三处(sku_codec.mint / list_new 去重闸 / alloc_push._SQL_ONLINE),批次 3 起加第四处 workflows/sku_migrate._SQL_CANDIDATES;refdata/schema.sql 的部分索引条件是 DDL 不是消费方过滤,不计入」—— 若 0a/横切已把这两条写进新建的 §九,本条只做核验、不重复写。

**为什么**:本仓的记录纪律是「跑过的都有 [x]、决策都有日期与依据」(conventions §五第 2 条自述);三处有意出入若不写在决策日志里,下一次复核会把它们当成实现漏洞改回去。②是采纳三位审查者对「白名单三种口径互相判红」的意见:规则文字与守门清单必须逐字对得上,而规则的家只能有一个。

**测试**:
- tests/test_sku_guard.py::test_abandoned_at_whitelist_matches_the_conventions_text(白名单条目数与 §六/§九 里写的处数一致)

**验收**:人眼 diff;grep -n 'unknown\|abandoned_at' docs/conventions.md | tail

### 新模块

- `workflows/sku_migrate.py`
  - API:模块级:DANGEROUS = True(不声明 SUPPORTS_STORE);常量 FEED_TYPE / SOURCE_TYPES / OBSERVE_HOURS=24 / STALE_HOURS=72 / FEEDS_PER_STORE_PER_RUN=2 / DEFAULT_LIMIT=10 / INFLIGHT_HOURS=48。对外只暴露 run(params: dict) -> str(cli 唯一入口,不含 argparse、不自理锁与调度)。内部私有:_preflight(conn, store_name) -> (bool, list[str]);_settle(conn, store_name, execute: bool) -> (计数 dict, list[str]);_stage_cap(conn, store_name, asked_limit) -> (int, str);_candidates(conn, store_name, limit) -> list[dict];_build_items(rows) -> list[dict](形态 A/B 的唯一分叉点);_migrate(store, rows, execute) -> (计数 dict, list[str])。params:store(必填)、limit、settle_only、observe_hours、stale_hours、execute(cli.py:307 注入)、dry_run(cli.py:311 注入)。
  - docstring 规则:头注第一行按本仓体例:`sku_migrate — 存量 SKU 迁到不透明码(批次 3;危险:缺省即真跑,空跑用 --dry-run)`,随后四行用法示例、三态状态机图(pending → confirmed / rolled_back / stalled 各自的证据组合)、前置清单(批次 0a/0b/1/2 已合并 + 新码生产跑过一轮 + 六件单品实测通过 + **旧仓 product_clear/daily_cleanup/auto_listing 调度已停**)、六条安全约束(先落库**并 commit**再调接口 / 回执不定案 / 写操作不自动兜底 / 一店一批的硬闸 / 永不进调度 / dry-run 下 _settle 与 _migrate 都零写)。每个函数 docstring **首行写「输入→输出」**;三处必须写死同名异义的辨析:『码弃用(abandoned_at) ≠ 沃尔玛 lifecycle RETIRED ≠ product_clear 停用』;凡是与 synthesis 或 sku_plan 取值不同的地方(POST unknown 不回滚)在 docstring 里写明理由与依据(api/feeds.py:445)。

### DDL

```sql
-- S1 listing_sources 迁移态两列 + 两个反查索引(refdata/schema.sql,插在 227 之后、229 的回填注释之前)
-- ⚠ 本批次**不动**活码部分唯一索引:它由批次 0a 一次建成最终条件
--   (WHERE abandoned_at IS NULL AND replaced_by IS NULL AND source_key IS NOT NULL
--    AND <sku_codec 的不透明码谓词>);本条只核验其条件已含 replaced_by IS NULL。
-- replaces / replaced_by / replaced_at 三列只由 services/sku_codec 写
-- (mint_replacement / settle_replacement);replaced_by 非空 = 在途改码(pending),
-- abandoned_at 非空且 abandoned_reason='sku_update' = 已定案。
ALTER TABLE catalog.listing_sources ADD COLUMN IF NOT EXISTS replaces text;
ALTER TABLE catalog.listing_sources ADD COLUMN IF NOT EXISTS replaced_at timestamptz;
CREATE INDEX IF NOT EXISTS listing_sources_replaced_by_idx
    ON catalog.listing_sources (store, replaced_by) WHERE replaced_by IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS listing_sources_replaces_uidx
    ON catalog.listing_sources (store, replaces) WHERE replaces IS NOT NULL;
```
```sql
-- S2 改码过程台账(refdata/schema.sql,插在 577 的 retire_cooldown_open_uk 之后、
--    579 的「退役清理」注释之前)
-- 身份权威在 catalog.listing_sources(replaces/replaced_by/abandoned_at);本表只是
-- sku_migrate 的过程账(feed_id/时刻/失败原因/飞书同步态)。两者的状态迁移必须同事务完成
-- (与 listing.retire_cooldown 之于 catalog.upc_pool 同款分工)。
CREATE TABLE IF NOT EXISTS listing.sku_migrations (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    store          text NOT NULL,
    old_sku        text NOT NULL,
    new_sku        text NOT NULL,
    source_type    text NOT NULL,
    source_key     text,
    feed_type      text NOT NULL,   -- MP_MAINTENANCE(形态 A)/ MP_ITEM(形态 B)
    feed_id        text,            -- 提交成功后落;NULL = 还没发出去
    status         text NOT NULL DEFAULT 'pending',  -- pending/confirmed/rolled_back/stalled
    submitted_at   timestamptz,
    settled_at     timestamptz,
    sheet_synced_at timestamptz,    -- 上架表 V 列已回写新码的时刻;NULL = 待补写
    error          text,
    detail         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS sku_migrations_open_uidx
    ON listing.sku_migrations (store, old_sku) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS sku_migrations_new_uidx
    ON listing.sku_migrations (new_sku);
CREATE INDEX IF NOT EXISTS sku_migrations_status_idx
    ON listing.sku_migrations (status, created_at);
```
```sql
-- S3 代际继承的唯一出处(refdata/schema.sql:237,回填 INSERT 之后、UPC 池注释段之前)
-- 「这个新码继承那个旧码的历史」只在这里定义;problem_scan 四段判据与
-- alloc_survey._SQL_SALES 一律经它取别名,不许各自现写 replaces 的 JOIN。
-- ⚠ 只继承**一跳**:前提是旧码改码后立即弃码、永不再改码。若将来允许连改两次,
--   本视图必须改成递归 CTE,否则第二跳静默断链。
CREATE OR REPLACE VIEW catalog.sku_aliases AS
  SELECT store, sku, replaces AS alias_sku
  FROM catalog.listing_sources
  WHERE replaces IS NOT NULL;
```
```sql
-- S4 product_risk 加改码维度(refdata/schema.sql:434-463 的 DROP+CREATE 幂等重建时,
--    插在 451-454「审核维度」注释之前)
         count(*) FILTER (WHERE event = 'sku_replaced')           AS sku_replaced_times,
         max(occurred_at) FILTER (WHERE event = 'sku_replaced')   AS last_sku_replaced_at,
```
```sql
-- S5 订单双算体检的唯一判据(refdata/schema.sql,插在 688 的 order_lines_asin_idx 之后、
--    690 的 orders.return_lines 之前)
-- orders.order_lines 的主键是 order_line_id = sha256(PO + SKU),唯一约束是 (po_id, sku);
-- 改码后若沃尔玛对旧 PO 返回新码,会插出第二行而旧行不删 ⇒ 同一笔销售算两次且不报错。
-- **本视图是这条判据的唯一出处**:services/order_lines.duplicate_po_lines、catalog_health、
-- 手工 psql 全部读它,谁都不许再写一遍 GROUP BY/HAVING。窗口由消费方自己加。
CREATE OR REPLACE VIEW orders.v_order_line_dupes AS
  SELECT store, po_id, line_number,
         count(DISTINCT order_line_id)   AS n,
         array_agg(sku ORDER BY sku)     AS skus,
         min(order_date)                 AS first_order_date
  FROM orders.order_lines
  WHERE line_number IS NOT NULL
  GROUP BY store, po_id, line_number
  HAVING count(DISTINCT order_line_id) > 1;
```
```sql
-- O4 problem_scan 扫描面排除在途改码的旧码(workflows/problem_scan.py:77-83)
-- ⚠ 现行 SQL 无表别名,本次加别名 w;三列的**位置顺序不变**(:174-176 按位置解包)
_SQL_ITEMS = """
SELECT w.store, w.sku, w.unpublished_reasons
FROM catalog.walmart_items w
WHERE w.published_status IS NOT NULL
  AND w.published_status <> 'PUBLISHED'
  AND w.missing_since IS NULL
  AND NOT EXISTS (SELECT 1 FROM catalog.listing_sources ls
                  WHERE ls.store = w.store AND ls.sku = w.sku
                    AND ls.replaced_by IS NOT NULL)
"""
```
```sql
-- O5 顽固代际沿 replaces 链继承一跳(workflows/problem_scan.py:137-143)
_SQL_STUBBORN = """
SELECT DISTINCT ON (store, sku) store, sku, event FROM (
    SELECT e.store, e.sku, e.event, e.occurred_at
    FROM catalog.product_events e
    WHERE e.event IN ('delete_verified', 'delete_not_effective',
                      'item_appeared', 'item_reappeared')
    UNION ALL
    SELECT a.store, a.sku, e.event, e.occurred_at
    FROM catalog.sku_aliases a
    JOIN catalog.product_events e
      ON e.store = a.store AND e.sku = a.alias_sku
    WHERE e.event IN ('delete_verified', 'delete_not_effective',
                      'item_appeared', 'item_reappeared')
) t
ORDER BY store, sku, occurred_at DESC
"""
```
```sql
-- O5 问题归类最近类别沿链继承(workflows/problem_scan.py:132-136)
-- ⚠ 占位符从 %s 改成命名式 %(ev)s(UNION 后同一个值用两次);:181 的 execute
--   实参跟着从元组改成 {"ev": product_events.PROBLEM_CATEGORIZED}
_SQL_LAST_CAT = """
SELECT DISTINCT ON (store, sku) store, sku, cat FROM (
    SELECT e.store, e.sku, e.detail->>'category' AS cat, e.occurred_at
    FROM catalog.product_events e WHERE e.event = %(ev)s
    UNION ALL
    SELECT a.store, a.sku, e.detail->>'category', e.occurred_at
    FROM catalog.sku_aliases a
    JOIN catalog.product_events e
      ON e.store = a.store AND e.sku = a.alias_sku
    WHERE e.event = %(ev)s
) t
ORDER BY store, sku, occurred_at DESC
"""
```
```sql
-- O5 WFS 删除拦截沿链继承(workflows/problem_scan.py:124-131)
-- ⚠ 占位符改 %(code)s;:189 的 execute 实参改 {"code": _WFS_BLOCKED_CODE}
_SQL_WFS_BLOCKED = """
SELECT store, sku FROM (
    SELECT DISTINCT ON (store, sku) store, sku, error_code FROM (
        SELECT f.store, f.sku, f.error_code, f.submitted_at
        FROM ops.feed_items f
        WHERE f.feed_type = 'DELETE_ITEM' AND f.status IN ('failed', 'missing')
        UNION ALL
        SELECT a.store, a.sku, f.error_code, f.submitted_at
        FROM catalog.sku_aliases a
        JOIN ops.feed_items f
          ON f.store = a.store AND f.sku = a.alias_sku
        WHERE f.feed_type = 'DELETE_ITEM' AND f.status IN ('failed', 'missing')
    ) u ORDER BY store, sku, submitted_at DESC
) t WHERE error_code = %(code)s
"""
```
```sql
-- O6 在途防重沿 replaces 链继承(workflows/problem_scan.py:102-114)
_SQL_INFLIGHT = """
SELECT store, sku, bool_or(disposal) AS disposal FROM (
    SELECT f.store, f.sku,
           (f.feed_type = ANY(%(disposal)s::text[])) AS disposal
    FROM ops.feed_items f
    JOIN catalog.walmart_items w ON w.store = f.store AND w.sku = f.sku
    WHERE (f.status = 'submitted'
           AND f.submitted_at > now() - interval '48 hours')
       OR (f.status = 'success' AND f.resolved_at > w.last_seen_at)
    UNION ALL
    SELECT a.store, a.sku,
           (f.feed_type = ANY(%(disposal)s::text[])) AS disposal
    FROM catalog.sku_aliases a
    JOIN ops.feed_items f ON f.store = a.store AND f.sku = a.alias_sku
    JOIN catalog.walmart_items w ON w.store = a.store AND w.sku = a.sku
    WHERE (f.status = 'submitted'
           AND f.submitted_at > now() - interval '48 hours')
       OR (f.status = 'success' AND f.resolved_at > w.last_seen_at)
) t
GROUP BY store, sku
"""
```
```sql
-- O12 分配链销量归属沿 replaces 链继承(services/alloc_survey.py:236-245)
-- 改码后 walmart_items.sku 是新码、orders.order_lines 历史行仍是旧码;
-- 不映射的话迁过码的品销量/GMV 恒 0(:515 店铺产出 / :523 冲突裁决 / :652 审计明细)。
-- ⚠ 返回键的形状不变((store, sku)),:790-791 与三个消费点一字不改。
_SQL_SALES = """
SELECT o.store AS store, coalesce(a.sku, o.sku) AS sku,
       count(*)                                   AS orders,
       coalesce(sum(o.product_amount), 0)::numeric  AS gmv
FROM orders.order_lines o
LEFT JOIN catalog.sku_aliases a
       ON a.store = o.store AND a.alias_sku = o.sku
WHERE o.order_date >= %(as_of)s::timestamptz - make_interval(days => %(days)s)
  AND o.order_date <  %(as_of)s::timestamptz
  AND coalesce(o.sale_status, '') <> 'Cancelled'
GROUP BY 1, 2
"""
```
```sql
-- W4 改码候选(workflows/sku_migrate.py:_SQL_CANDIDATES)
-- 形态判据经 services/sku_codec.OPAQUE_SQL_PREDICATE 派生(C6),**不在此处手打正则**:
--   _SQL_CANDIDATES = _SQL_CANDIDATES_TMPL.format(
--       opaque=sku_codec.OPAQUE_SQL_PREDICATE.format(col="w.sku"))
_SQL_CANDIDATES_TMPL = """
SELECT w.store, w.sku AS old_sku, ls.source_type, ls.source_key,
       coalesce(w.upc, w.gtin) AS product_id,
       CASE WHEN w.upc IS NOT NULL THEN 'UPC' ELSE 'GTIN' END AS product_id_type
FROM catalog.walmart_items w
JOIN catalog.listing_sources ls
  ON ls.store = w.store AND ls.sku = w.sku
WHERE w.store = %(store)s
  AND w.missing_since IS NULL
  AND ls.abandoned_at IS NULL
  AND ls.replaced_by IS NULL
  AND ls.source_type = ANY(%(source_types)s::text[])
  AND NOT {opaque}
  AND (w.upc IS NOT NULL OR w.gtin IS NOT NULL)
  AND NOT EXISTS (SELECT 1 FROM listing.sku_migrations m
                  WHERE m.store = w.store AND m.old_sku = w.sku
                    AND m.status IN ('pending', 'confirmed', 'stalled'))
ORDER BY w.sku
LIMIT %(limit)s
"""
```
```sql
-- W4 逐候选的在途 feed 闸(W2 第⑥道;口径与 problem_scan._SQL_INFLIGHT 同源)
_SQL_INFLIGHT_OLD = """
SELECT DISTINCT sku FROM ops.feed_items
WHERE store = %(store)s AND sku = ANY(%(skus)s::text[])
  AND status = 'submitted'
  AND submitted_at > now() - make_interval(hours => %(hours)s)
"""
```
```sql
-- W3 定案证据(workflows/sku_migrate.py:_SQL_OBSERVE)
_SQL_OBSERVE = """
SELECT m.id, m.old_sku, m.new_sku, m.source_key, m.feed_id, m.submitted_at,
       (nw.sku IS NOT NULL AND nw.missing_since IS NULL)          AS new_present,
       (ow.sku IS NULL OR ow.missing_since IS NOT NULL)           AS old_gone,
       EXISTS (SELECT 1 FROM catalog.walmart_items s
               WHERE s.store = m.store AND s.last_seen_at > m.submitted_at) AS fresh
FROM listing.sku_migrations m
LEFT JOIN catalog.walmart_items nw ON nw.store = m.store AND nw.sku = m.new_sku
LEFT JOIN catalog.walmart_items ow ON ow.store = m.store AND ow.sku = m.old_sku
WHERE m.store = %(store)s AND m.status = 'pending' AND m.submitted_at IS NOT NULL
"""
```
```sql
-- W3 confirmed 分支:除 settle_replacement / retag_sku 外的三步
-- ⚠ dispositions 与 item_node_inventory **不在工作流里裸写 SQL**:
--   走 services/dispositions.rekey_suggested(O10)与
--   services/walmart_catalog.drop_node_rows(O11)。本条只列 ledger 那一句。
UPDATE listing.sku_migrations
   SET status = 'confirmed', settled_at = now() WHERE id = %(id)s;
-- 事务外飞书回写成功后:
UPDATE listing.sku_migrations SET sheet_synced_at = now() WHERE id = %(id)s;
-- 每轮开头的补写集合:
SELECT id, store, old_sku, new_sku FROM listing.sku_migrations
 WHERE store = %(store)s AND status = 'confirmed' AND sheet_synced_at IS NULL;
```
```sql
-- O10 dispositions 迁键(services/dispositions.rekey_suggested)
-- 先取 new_sku 名下已占用的 action,避开 dispositions_open_uidx
-- (refdata/schema.sql:1313-1315,(store, sku, action) WHERE status IN ('suggested','executing'))
SELECT action FROM ops.dispositions
 WHERE store = %(store)s AND sku = %(new_sku)s
   AND status IN ('suggested', 'executing');

UPDATE ops.dispositions
   SET sku = %(new_sku)s, asin = coalesce(asin, %(asin)s)
 WHERE store = %(store)s AND sku = %(old_sku)s
   AND status = 'suggested'
   AND action <> ALL(%(taken)s::text[]);
```
```sql
-- O11 节点库存清旧行(services/walmart_catalog.drop_node_rows)
DELETE FROM catalog.item_node_inventory
 WHERE store = %(store)s AND sku = %(sku)s;
```
```sql
-- X1 订单双算体检(services/order_lines.duplicate_po_lines;**只读 S5 的视图**)
SELECT store, po_id, line_number, n, skus
FROM orders.v_order_line_dupes
WHERE %(days)s IS NULL
   OR first_order_date > now() - make_interval(days => %(days)s)
ORDER BY n DESC, store, po_id
LIMIT 200
```

### 文档同步

- docs/db_schema.md:165-176(listing_sources 加 replaces/replaced_at 与两个反查索引;三列只由 sku_codec 写)
- docs/db_schema.md:182-190(upc_pool:改码只改 sku 列,asin/status/used_at 不动)
- docs/db_schema.md:284-320(product_events 事件码导览补 sku_replaced/sku_abandoned)
- docs/db_schema.md:321-341(listing 域新增 listing.sku_migrations 全表说明 + 与 listing_sources 的分工 + sheet_synced_at)
- docs/db_schema.md:684-691(ops.cleanup_seen_categories:本批次不迁,零读者复核结论)
- docs/db_schema.md:692-700(ops.dedupe:maintenance:submitted 键含 sku、20h 自然过期不迁;brand_asin/brand_scrape 键是 ASIN 不受影响;catalog.claims.claim_key 同理)
- docs/db_schema.md:781-821 之后(新增 catalog.sku_aliases 视图 + orders.v_order_line_dupes 视图 + product_risk 两个新列)
- docs/api_blueprint.md:26(#10 用途补「改 SKU(SkuUpdate=Yes,按 Product ID 匹配)」)
- docs/api_blueprint.md:73-90(工作流×端点矩阵在 88 行 list_new 之后新增 sku_migrate 一行)
- docs/api_blueprint.md:128 与 192(MP_MAINTENANCE 桶共享说明 + SkuUpdate 官方出处与待实测标注)
- docs/sku_plan.md:409-436(§7 批次 3 改写成三态落地形态,目标改「止血」,受牵连键表逐条给结论)
- docs/sku_plan.md:439-475(§8 勾掉存量产品项,新增决策 D/E/H/I,保留 §3.5 两条建议的勾选状态)
- docs/sku_plan.md:184-213(§4 保守策略标注 + 实测三件扩到六件)
- docs/sku_plan.md:475 之后(新增 §9 决策日志:三处有意出入 + 九个决策点默认值 + 本次评审驳回的两条意见)
- docs/plan.md Phase 2 表格(补一行指针:SKU 改造批次 3 的决策日志在 docs/sku_plan.md §9;plan.md 本身无决策日志段,已实测)
- docs/conventions.md:115-133(§六 末尾补「POST unknown 一律保持 pending」与「abandoned_at IS NULL 的四类允许出现处」;若 0a/横切已建 §九则只核验)
- skills/(改了 registry/schedule.py 头注后跑 python cli.py skill_export 重新生成,不要手改)

### 守门测试

- **守门测试只有一个家**:tests/test_sku_guard.py(由批次 0a 建,形态照抄仓内既有的 tests/test_feishu_guard.py:42-47 的白名单 dict + 「白名单不许烂掉」用例)。批次 3 **只往这一份里增删白名单条目与断言**,绝不新建第二份守门文件;tests/test_sku_migrate.py 只放行为测试。——采纳四位审查者一致意见(原稿把 abandon 守门塞进 tests/test_sku_migrate.py,与 0a/0b/2/横切 的四个守门文件形成五份重复白名单)。
- tests/test_sku_guard.py::test_abandon_is_only_called_through_sku_codec —— 全仓 grep:`UPDATE catalog.listing_sources` 与 `INSERT INTO catalog.listing_sources` 里出现 abandoned_at/replaced_by/replaces 的语句只允许在 services/sku_codec.py(mint/mint_replacement/abandon/settle_replacement)与 services/listing_sources.register;workflows/sku_migrate.py 里零登记簿写 SQL 字面量。
- tests/test_sku_guard.py::test_non_abandon_points_still_never_abandon —— 反向钉死批次 2 的守门条款在批次 3 之后仍成立:product_clear / problem_product_cleanup / maintenance / catalog_sync.mark_missing / feed_track 走完一轮后 listing_sources.abandoned_at 仍为 NULL。
- tests/test_sku_guard.py::test_abandoned_at_filter_whitelist —— 白名单四类:消费方 .py 三处(services/sku_codec.mint、workflows/list_new 去重闸、workflows/alloc_push._SQL_ONLINE)+ 批次 3 起的第四处 workflows/sku_migrate._SQL_CANDIDATES;refdata/schema.sql 的部分索引条件是 DDL 不计入(注释写明理由)。白名单条目数与 docs/conventions.md 里写的处数逐字对齐。
- tests/test_sku_guard.py::test_live_code_unique_index_name_appears_once_in_schema —— 活码部分唯一索引在 refdata/schema.sql 全文只出现一次(一个名字、一份条件);批次 3 不 DROP 不重建。
- tests/test_sku_guard.py::test_mint_live_row_filter_matches_the_index_condition —— sku_codec.mint 的活行 WHERE 片段与 schema.sql 里活码索引的 WHERE 片段逐字一致(C3)。
- tests/test_sku_guard.py::test_no_second_opaque_regex_in_the_repo —— 12 位不透明码字符集正则只允许出现在 services/sku_codec.py(_ALPHABET 派生的 OPAQUE_SQL_PREDICATE)与 refdata/schema.sql 的索引条件;workflows/sku_migrate.py 里零手打正则。
- tests/test_sku_guard.py::test_is_opaque_and_the_sql_predicate_agree —— Python 侧 is_opaque 与 SQL 侧谓词对同一组样本(裸 ASIN / 三段式 / PHUMWMT 串 / 12 位纯数字 item id / 新码)判定完全一致(needs_pg)。
- tests/test_sku_guard.py::test_only_sku_aliases_expresses_the_replacement_chain —— 代际继承只经 catalog.sku_aliases;全仓 .py 里同时出现 `listing_sources` 与 `replaces` 的 SQL 只允许在 services/sku_codec.py 与 services/listing_sources.py。
- tests/test_sku_migrate.py::test_registry_write_is_committed_before_the_post —— 用打桩记录调用序与 commit 时点,断言 mint_replacement + ledger INSERT 的事务**已提交**才调 feeds.submit_feed(安全铁律「防重状态先落库再调接口」;registry/db.py:19-40 是正常退出才 commit)。
- tests/test_sku_migrate.py::test_no_write_operation_ever_falls_back_to_another_method —— 断言 sku_migrate 全程只调一种 feed_type,失败分支零重试、零换姿势。
- tests/test_sku_migrate.py::test_serial_rerun_produces_a_byte_identical_payload —— 同一批连跑两次,第二次因 mint_replacement 幂等拿回同一新码,载荷逐字节相同,feeds 的 payload_key 防重命中(不重发)。
- tests/test_sku_migrate.py::test_dry_run_touches_neither_db_nor_walmart —— dry-run 下 _settle 与 _migrate **都**零写:pg_conn 只被读、submit_feed 零调用、飞书零写、abandon/retag/rekey/drop_node_rows 零调用。
- tests/test_problem_scan.py::test_replaced_rows_never_produce_a_disposition —— 在途改码的旧码不进扫描面、不产建议(O4 的反向钉死;这是本批次两条能造成不可逆损失的路径之一)。
- tests/test_mp_conform.py::test_sku_update_survives_strip_unknown_when_absent_from_the_spec —— 形态 B 的另一条不可逆路径(M5):SkuUpdate 被剔 ⇒ 每一行都建成第二条 listing。
- tests/test_catalog_sync.py::test_no_item_missing_event_for_replaced_rows_but_missing_since_is_still_set —— 抑制的是事件不是观测。
- tests/test_product_events.py::test_sku_migrate_receipts_never_enter_the_ledger_in_either_feed_form —— 形态 A(maintenance)与形态 B(list)都不入病历。
- tests/test_feed_track.py::test_sku_migrate_failure_never_blacklists_the_asin —— 改码被拒不得反哺 ASIN 黑名单。
- tests/test_launchd.py::test_sku_migrate_is_not_scheduled —— registry/schedule.py 的 JOBS 里永不出现 sku_migrate。
- tests/test_sku_asin.py::test_abandoned_and_replaced_rows_still_resolve_to_the_asin —— 旧码(已 abandoned + replaced_by)与新码经 resolve/resolve_many 都返回同一个 ASIN(订单/售后带旧码回来必须查得到)。
- tests/test_order_lines.py::test_duplicate_po_lines_reads_the_view_not_a_local_group_by —— 订单双算判据只有一份实现(S5 的视图)。

### 验收命令

```bash
python -m pytest -q  # 全量必须全绿
```
```bash
python -m pytest tests/test_sku_migrate.py tests/test_sku_guard.py tests/test_sku_codec.py tests/test_catalog_sync.py tests/test_problem_scan.py tests/test_product_events.py tests/test_feed_track.py tests/test_mp_mapper.py tests/test_mp_conform.py tests/test_feeds.py tests/test_rate_bucket.py tests/test_upc_pricing.py tests/test_order_lines.py tests/test_dispositions_router.py tests/test_alloc_engine.py tests/test_launchd.py -q
```
```bash
python cli.py db_init && python cli.py db_init  # 连跑两次验幂等:两列 + 新表 + 两视图 + 两索引
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='catalog' AND tablename='listing_sources';"  # 确认活码唯一索引**只有一条**且条件已含 replaced_by IS NULL(0a 交付,本批只核验)
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT count(*) FROM catalog.sku_aliases;"  # 改码前必须为 0
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT * FROM orders.v_order_line_dupes;"  # 改码前基线,存档;改码后必须一致
```
```bash
python -c "from registry import db; from workflows import sku_migrate as m; c=db.pg_conn().__enter__(); print(c.execute('EXPLAIN '+m._SQL_CANDIDATES, {'store':'T1','source_types':['amz'],'limit':1}).fetchall())"  # 候选 SQL 走索引(计划里不得出现 Seq Scan on walmart_items)
```
```bash
python cli.py sku_migrate --dry-run -p store=<试点店> -p limit=1  # 人眼确认:六道前置闸结论、候选、载荷 sku 是占位码、productIdentifiers 是该品现挂的号、零写库
```
```bash
python cli.py sku_migrate -p store=<试点店> -p limit=1  # 第一级:1 个品(_stage_cap 会把任何 limit 压到 1)
```
```bash
python cli.py catalog_sync -p store=<试点店>  # 等一轮完整观测
```
```bash
python cli.py sku_migrate -p store=<试点店> -p settle_only=1 --dry-run  # 先空跑看判决,必须零写库零飞书
```
```bash
python cli.py sku_migrate -p store=<试点店> -p settle_only=1  # 定案;摘要首行必须给出 confirmed/rolled_back/stalled 与双挂/飞书未同步告警
```
```bash
python cli.py maintenance_scan --dry-run -p preview=1 -p store=<试点店>  # 与改码前的意图集合逐条对比:必须只有 SKU 串变了
```
```bash
python cli.py node_probe -p store=<试点店>  # 改码前后库存/节点对比
```
```bash
python cli.py alloc_survey --dry-run  # 改码前后对比该店销量维度:迁过码的品销量不得掉到 0(O12)
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT status,count(*) FROM listing.sku_migrations WHERE store='<试点店>' GROUP BY 1;"
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT id,new_sku,sheet_synced_at FROM listing.sku_migrations WHERE store='<试点店>' AND status='confirmed';"  # sheet_synced_at 必须非空,否则下轮补写
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT sku,status,asin,used_at FROM catalog.upc_pool WHERE store='<试点店>' AND asin='<试点 ASIN>';"  # sku 已是新码、asin/status/used_at 未变
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT sku,replaces,replaced_by,replaced_at,abandoned_at,abandoned_reason FROM catalog.listing_sources WHERE store='<试点店>' AND (sku='<旧码>' OR replaces='<旧码>');"
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT event,count(*) FROM catalog.product_events WHERE store='<试点店>' AND sku IN ('<旧码>','<新码>') GROUP BY 1;"  # 必须有 sku_replaced/sku_abandoned,必须没有 item_appeared/item_missing
```
```bash
psql "$(python -c 'from registry import db;print(db.pg_dsn())')" -c "SELECT key,created_at FROM ops.dedupe WHERE scope='maintenance:submitted' AND key LIKE '<试点店>|%' ORDER BY created_at DESC LIMIT 20;"  # 核验 drop_recent 键的 20h 窗口短于一轮观测期(R2⑦ 的结论依据)
```
```bash
python cli.py problem_scan --dry-run  # 被改码的 SKU 不得出现在任何建议里
```
```bash
python cli.py sku_migrate -p store=<试点店> -p limit=10  # 第二级:10 个品(节奏闸放行到 10)
```
```bash
python cli.py sku_migrate -p store=<试点店> -p limit=100000  # 第三级:整店(前提是前两级全部 confirmed)
```
```bash
python cli.py skill_export  # 改了 registry/schedule.py 头注后重新生成 skills/
```

### 决策点

- **A|product_clear 停用(RETIRE)是否给 problem_scan 加豁免**
  - 默认:RETIRE 不弃码;**不加**豁免(豁免另议)。对批次 3 的影响:O4 在 _SQL_ITEMS 里加的「排除在途改码旧码」那条 NOT EXISTS 与将来的 lifecycle 豁免是**并列的两条独立条件**,互不冲突,先加哪条都不影响另一条。
  - 备选:① 加豁免:_SQL_ITEMS 再加一条「lifecycle=RETIRED 且本仓提交过 retire_submitted 则跳过」,「停用可恢复」在本系统里才真正成立;② 改采「RETIRE 回执成功即弃码」的简化版:分支更少,但永久失去可恢复、每次停用烧一个 UPC,且违背「不信回执信观测」。
  - 影响:对批次 3 只有一处代码影响(同一段 WHERE 多一条条件),无结构性影响;两种选择本批次都能跑。真正的影响在批次 2 的弃码点集合与 workflows/product_clear.py:20-21 的措辞(那条注释更正由批次 2 或横切承载,本批不做,已在 risks 说明)。
- **B|UPC 撞库 0101119 时码与 UPC 是否一起换**
  - 默认:一起换(批次 2 的第三个弃码点)。对批次 3 的影响:改码 confirmed 时 `abandon(reason='sku_update')` 必须**不烧号**,与撞库弃码的 burned_lock/burned_delete 走不同分支 —— 两者共用 abandon 这一个出口,分支由 reason 决定。
  - 备选:不换(维持 2026-08-09「撞库只是 UPC 被占、照常领新号重试」):批次 3 无需改动,只是 upc_pool 的状态值少一个。
  - 影响:对批次 3 仅影响 sku_codec.abandon 的 reason→烧号分支表与 upc_pool 的状态值枚举;两种选择本批次都能跑,测试用例名不变。
- **C|alloc_push 派工口径是否对齐去重闸**
  - 默认:对齐。对批次 3 的影响:pending 期间旧码行 abandoned_at IS NULL 且在架,按对齐后的口径仍算「已在架」⇒ 不会被重新派工,**不需要**在 alloc_push._SQL_ONLINE 里额外加 replaced_by 条件。
  - 备选:不对齐(alloc_push 继续按 lifecycle 排 RETIRED):批次 3 仍不需要改动,但退市且未弃码的 ASIN 会被分配链派、被 list_new 拦,与改码无关的既有摩擦保持原样。
  - 影响:对批次 3 零代码影响;需要在 docs 里写明「pending 期间不必额外加条件」的推理,否则下一个人会好心补一条冗余条件。⚠ 审查者指出 alloc_survey._SQL_ONLINE 的对齐与否在 0a/2 两包里指令矛盾 —— 那一条不属本批,本批只声明:无论怎么定,批次 3 都零改动。
- **D|跟卖存量(source_type='match',PHUMWMT+日期+序号)是否也迁**
  - 默认:**不迁**。`SOURCE_TYPES = (SOURCE_AMZ,)`,候选 SQL 天然排除 match 行。理由:PHUMWMT 串本就不含 ASIN,货源隐匿收益为零;match 行的 source_key 是匹配 GTIN 而不是 ASIN(services/listing_sources.py:23),改码后 upc_pool.retag_sku 的 (store, asin) 键无从对上(跟卖不用 UPC 池),实测面直接翻倍。
  - 备选:迁:把 SOURCE_TYPES 加上 SOURCE_MATCH,并额外确认三件事——(a) MP_ITEM_MATCH(v4.2)是否也认 SkuUpdate(官方零文档);(b) services/match_sheet 的回执找行在改码后仍能对上;(c) abandon 对 match 行「只标不烧」的分支已实现。
  - 影响:默认下批次 3 的实测面减半、代码分支减一;若所有者要迁,需追加一轮单品实测与一个 _build_items 分支,不改状态机。
- **E|SkuUpdate 的 feed 形态:A(MP_MAINTENANCE 最小载荷)还是 B(MP_ITEM 全量)**
  - 默认:**A**,常量 `workflows/sku_migrate.FEED_TYPE = "MP_MAINTENANCE"`,载荷由 `mp_mapper.build_sku_update_item()` 组。依据:US 官方 Update my existing items 明写 MP_MAINTENANCE「requires only the SKU and GTIN attributes」(docs/api_blueprint.md:192);CA manage-items 明写 SkuUpdate 属性。**必须先在所有者机器实测**(`grep -rl SkuUpdate <DATA_ROOT>/specs/MP_ITEM/5.0.20260608-18_15_07-api/` 定位它在哪份 spec、最小载荷能否改码),实测前只许 dry-run。
  - 备选:B:FEED_TYPE 改 "MP_ITEM",载荷走 `build_orderable(..., sku_update=True)` + `mp_conform.conform` + `assemble_mp_item`。**形态 B 必须先做 M5**(services/mp_conform.py:687-709 的 strip_unknown 会把 spec 外的 Orderable 字段静默剔掉),否则每一行都退化成普通上架 = 每一行都双挂。api/feeds 两种形态都零改动(两个 feedType 都已收录、切片与桶都已登记)。
  - 影响:形态 B 有一个所有者必须接受的副作用:改码 = 重发全部内容,**标题与属性会被我们再生成的内容覆盖**;并且它吃的是 list_new 的 MP_ITEM 桶(api/_client.py:238,8/hour),FEEDS_PER_STORE_PER_RUN=2 就是为它留的余量。若实测判定只有形态 B 可行,建议批次 3 暂停回到计划层让所有者拍板内容覆盖这件事,而不是照做。
- **F|POST outcome=unknown 时是否回滚(与 synthesis 的有意出入)**
  - 默认:**不回滚,保持 pending**,留给下一轮 _settle 与 feeds 的启动对账。理由:unknown 的语义是「不知道到没到」(api/feeds.py:445 的既定处置就是保持 pending);若沃尔玛其实已经改成新码而我们回滚了登记簿,新码会成为一条没有出身的孤儿行(sources_backfill 判 unknown ⇒ 退出全部自动化),而且不报错。synthesis 里「failed/未达/Unknown ⇒ rolled_back」说的是**回执三态**(_settle 的输入),本条说的是 POST 的 outcome,两者不冲突。
  - 备选:按字面回滚:分支更少,但引入上述孤儿行风险,且与本仓「人不在环时宁停不重」的口径相反。
  - 影响:只影响 _migrate 的一个分支与两条测试;已按审查意见把这处出入写进 workflow docstring 与 docs/sku_plan.md §9 决策日志(R5),否则下次复核会当成实现漏洞改回去。
- **G|ops.cleanup_seen_categories / ops.dedupe / catalog.claims 是否随 replaced_by 迁**
  - 默认:**三者都不迁**。复核证据:① cleanup_seen_categories 全仓只有 workflows/cleanup_history_import.py 写它、**零读者**(2026-09-02 grep 复核),而它的主键是 (sku, category) 的唯一对、是「错误统计」累计数的真值来源,复制一份会多算、重命名又会偷走另一家店的历史(表无 store 维度);② ops.dedupe 的 `maintenance:submitted` 键含 sku(services/maintenance_intents.py:511-525),改码后旧键失配,但窗口只有 20h(SUPPRESS_HOURS=20)且短于一轮观测期 24h ⇒ **自然过期**,最坏是定案后多发一次同值维护意图(可能收到 0101198 stale update,非破坏);`cleanup:brand_asin` / `cleanup:brand_scrape` 两个 scope 的键是 ASIN,不受影响;③ catalog.claims.claim_key 是 ASIN 或品牌归一键(refdata/schema.sql:404-405),不受影响。
  - 备选:① 复制(INSERT…SELECT 新码 ON CONFLICT DO NOTHING):累计计数多算 N 对;② 重命名(UPDATE sku):跨店同串时会偷走别店历史;③ 将来接报表消费方时改成按登记簿 source_key 读写(推荐的长期解);④ 改码时顺手 UPDATE ops.dedupe 的 maintenance 键:多一处写、收益只是省一次 stale update,不值。
  - 影响:默认下批次 3 零代码,只在 docs/db_schema.md:684-700 与 docs/sku_plan.md §7 各留一句结论与依据(R2⑥⑦、R4③)。**这一条是采纳审查者 1 与 2 的 missing 后扩写的**:原稿只对 cleanup_seen_categories 给了结论,drop_recent/dedupe/claims 三张表既没结论也没「为什么不用管」的论证。
- **H|改码后的历史销量归属:经 sku_aliases 映射,还是把聚合键改成 ASIN**
  - 默认:**经 sku_aliases 映射**(O12):services/alloc_survey._SQL_SALES 加 `LEFT JOIN catalog.sku_aliases`,输出键 `coalesce(a.sku, o.sku)`,返回字典的键形状不变,:790-791 与三个消费点(:515/:523/:652)一字不改。
  - 备选:改按 orders.order_lines.asin 聚合(services/product_pool.py 已是这个写法,仓内有先例):口径更根本、连三段式/纯数字历史行也能归并,但要同步改三个消费点的键形状(那是行为变化,且 asin 列有 NULL 面),不属本批次。
  - 影响:默认下批次 3 只改一段 SQL、零消费点改动,且改码前 sku_aliases 为空集 ⇒ 结果集逐行不变。若所有者要走 ASIN 聚合,应另立批次并给「asin IS NULL 的历史行怎么办」的口径。
- **I|形态 B 下 SkuUpdate 如何穿过 mp_conform.strip_unknown**
  - 默认:在 services/mp_conform.py 显式登记 `ORDERABLE_SYSTEM_SWITCHES = ("SkuUpdate",)` 并让 strip_unknown 放行(M5)。理由:名单穷举、触发记日志、条件明确,满足 conventions §六 真兜底三要件;而且它与 mp_mapper.ORDERABLE_SYSTEM_FIELDS(M1,管 LLM 输入)是两件不同的事,不能合并。
  - 备选:① conform 之后由 sku_migrate 重新写回 `orderable['SkuUpdate']='Yes'`:能跑,但把「哪些字段能发」的知识散到工作流里,踩单一路径;② 等实测确认 SkuUpdate 在 Orderable spec 里就什么都不做:赌 spec 版本,而 spec 每次换版都可能变——一次静默剔除就是成批双挂。
  - 影响:默认下多一个模块常量与三条测试;形态 A(默认)用不到它,但**仍必须先做**,否则决策 E 一旦翻到形态 B 就是不可用状态。这一条是采纳审查者 4 的 blocker 新增的决策点。

### 依赖

- 批次 0a|身份积木与 SQL 收口已合并:services/sku_codec.py 的 mint/abandon/is_opaque/source_of/_ALPHABET/placeholder 存在;**不透明码字母表、长度、「至少一个字母」三条口径只在 sku_codec 出生**(横切决策 D4;registry 只留 SKU_SOURCE_LETTERS,services/sku_asin 不再有 OPAQUE_* 一份);catalog.listing_sources 已有 abandoned_at/abandoned_reason/replaced_by 三列;**活码部分唯一索引已由 0a 一次建成最终条件**(含 `replaced_by IS NULL`,一个名字一份条件,schema.sql 全文只出现一次)—— 批次 3 只核验不重建;守门测试的唯一之家 tests/test_sku_guard.py 已建。
- 批次 0b|订单/事件/黑名单读侧收口已合并:services/sku_asin.resolve/resolve_many/resolve_pairs 对 abandoned 行照常返回 source_key;catalog.product_events.asin 经登记簿反查(否则 sku_replaced/sku_abandoned 事件的 asin 恒 NULL,product_risk 归并失效)。
- 批次 1|上架表 V 列「SKU」已建且 registry 已登记:**W3 按批次 1 定的单列写函数实际签名接**(本工作包按 `listing_sheet.write_sku_col(updates, execute)` 描述;若批次 1 定了别的名字,按实际名字接,**不新写一个**)。⚠ 审查者提醒:批次 1 的 `_mark_upc_conflicts` 改键这一条**批次 0a 并没有做**,不可跳过,否则 upc_pool.sku 存真码后撞库标记静默归零。
- 批次 2|写侧切换已合并且新码在生产跑过至少一轮:list_new._prep_rows 已 mint、四个弃码点已接、services/product_events.py:95-121 已登记 sku_abandoned / sku_replaced 两个事件码(常量 + EVENTS 集合)。若批次 2 未登记这两个码,批次 3 必须先补登记,否则 record_many(:159-162)会 fail loud。
- **横切包的三处所有权已归位**(本批次前置协调项,四位审查者一致):活码唯一索引 → 0a 唯一交付;守门测试 → tests/test_sku_guard.py 唯一交付;订单双算体检 → **本批次 S5 的视图唯一交付**(横切 C0-DDL-6 删除)。这三件不先拍平,0a 合并当天就有守门测试互相判红。
- 所有者机器单品实测**六件**(全部通过前只许 dry-run):(1) `grep -rl SkuUpdate <DATA_ROOT>/specs/MP_ITEM/5.0.20260608-18_15_07-api/` 定位它在 MP_ITEM 还是 MP_MAINTENANCE spec 里(同时决定 M5 是「保险」还是「必需」);(2) MP_MAINTENANCE {sku 新码, GTIN 现号, SkuUpdate: Yes} 最小载荷能否改码;(3) 改码后库存/价格/item_id(wpid)/变体组是否原样保留(node_probe + GET /v3/items/{新sku} 前后对比);(4) 旧串改码后能否再次使用;(5) 对 lifecycle=RETIRED 的 item 是否可用;(6) 改码**之前**的 PO 日后返回旧码还是新码(决定 X1 体检的严重级别)。
- 生产纪律(代码之外,必须在 PR 里贴证据):① 改码期间该店不得有人手动在 Seller Center 改同一批 item 的 SKU/Product ID;② **旧仓调度已停** —— `crontab -l | grep -Ei 'auto_listing|retire_and_relist|product_clear|daily_cleanup'`(或 launchctl list)输出为空,截图贴 PR。CLAUDE.md 安全红线「新旧系统严禁对同一破坏性任务并跑」原本只在散文里,本次按审查意见提成硬前置。

### 风险

- **审查意见处理总表(逐条)——已采纳的 blocker/major**:① 活码唯一索引三包三个名字、批次 3 的 DROP 打空且裸 CREATE 会让 db_init 整份回滚(四位审查者一致)⇒ S1 改为「0a 一次建成最终条件,批次 3 只核验 + 加两个局部反查索引」,并加两条读 schema.sql 文本的守门。② _settle 没有 dry-run 分支而它是全包写得最重的一段 ⇒ W3 签名加 execute,六处写全部受控,guard test 落到 W3 名下。③ _migrate 的事务边界没定、按字面是在未提交事务里 POST ⇒ W4 写死「mint+ledger 的 with 必须在组载荷前退出并 commit」,加调用序+commit 时点的测试。④ 字母表三个家、两条守门互斥 ⇒ 收敛到 sku_codec,新增 C6 的 OPAQUE_SQL_PREDICATE 让 SQL 侧也只有一份。⑤ 守门测试四个文件、白名单重复三处 ⇒ 全部并入 tests/test_sku_guard.py,本批只增删条目。⑥ 订单双算体检两份实现 ⇒ S5 视图唯一交付,X1 退化成薄壳。⑦ 形态 B 的 SkuUpdate 会被 mp_conform.strip_unknown 静默剔掉(实测 services/mp_conform.py:699-705 确认) ⇒ 新增 M5 与决策 I。⑧ W3 裸 SQL 改 dispositions(会撞 dispositions_open_uidx)与裸 DELETE 节点库存 ⇒ 新增 O10/O11 两个 services 函数。⑨ 飞书 V 列回写一次性、无补写路径 ⇒ S2 加 sheet_synced_at,W3 每轮补写。⑩ alloc_survey._SQL_SALES 销量归属改码后恒 0(全套材料都漏了) ⇒ 新增 O12。⑪ problem_scan._SQL_INFLIGHT 在途防重改码后失效 ⇒ 新增 O6 + W2 第⑥道闸。⑫ 批次 3 行号系统性漂 3-4 行 ⇒ 全部 file:line 重开文件核对(upsert_items 实为 106-123、mark_missing 121/125-137、_SQL_ITEMS 无表别名、product_risk 434-463、retire_cooldown 566-577、cleanup_seen_categories 684-691)。⑬ 缺 rollback 段、缺工时估算、psql DSN 三种写法、EXPLAIN 粘不进 psql ⇒ 见 estimated_pr_split 与 acceptance_commands。⑭ drop_recent / ops.dedupe / claims 三张表没结论 ⇒ 决策 G 扩写 + R2⑦ + R4③。⑮ sku_plan §7 留着与实现相反的旧稿 ⇒ R4 逐条改写。
- **审查意见处理总表——驳回或转出的**:① 「批次 3 顺手给 list_new 加 limit 参数」(审查者 2/4 的 major,实测确认 workflows/list_new.py 的 params 里确实没有 limit)—— **驳回归属,不驳回内容**:那是批次 2 的止损闸,写进批次 3 会让一个 DANGEROUS 的一次性工作流去改上架主链;已在 R5 决策日志里记明「转批次 2,照 W5 的 _stage_cap 形状做」。② 「POST outcome=unknown 按 synthesis 字面回滚」—— 驳回,理由见决策 F(api/feeds.py:445 的既定处置),并按审查建议把这处出入写进 docstring 与决策日志。③ 「workflows/product_clear.py:20-21 的注释更正」(审查者 1 的 missing)—— 确认该注释确在 :20-21、确实需要改,但它属于决策 A 的落地面(批次 2 或横切),批次 3 加了 O4 之后并不改变停用品的命运;**转出并在 R5 记明,不静默丢弃**。④ 「services/blacklist.py:205-243 黑名单键被灌随机码」(审查者 1 的 missing)—— 真问题,但它在**批次 2 新码上线当天**就会发生,不能等批次 3;转 0b,R5 记明。⑤ 「variant_group.group_id 仍把 ASIN 递给沃尔玛」—— 目标级漏洞,但改它属于编码规则层,转 sku_plan §8 待决清单(R4⑥),批次 3 不承载。
- **最贵的一条**:改码生效有 15 分钟到 4 小时的窗口,窗口内旧码可能被观测成非 PUBLISHED 且未缺席,正好落进 problem_scan 的扫描面被建议 DELETE_ITEM —— 一次成功的改码被自己的自动链当场永久删掉。止损全靠 O4 那一条 NOT EXISTS,它必须先于任何一次真跑合并,并且有反向守门测试钉住。
- **第二贵的一条(本次新识别)**:形态 B 下若 SkuUpdate 被 mp_conform.strip_unknown 剔掉,发出去的是一条普通 MP_ITEM,沃尔玛按新 sku **建第二条 listing**,旧的原样活着 —— 不是偶发的「同店双挂」,是每一行都双挂,而且回执一片成功。止损是 M5 + 实测第 1 件;在实测确认之前形态 B 一行都不许真跑。
- 「同店双挂」(new_present 且 old 仍在架):状态机对这一分支**不定案、只告警**,但发现它需要人看摘要;节奏闸(第一批只许 1 个品)是它的主要防线。
- 回执成功但后台没改(delete_not_effective 的同款形态):本设计已用观测定案兜住,代价是定案要等一轮 catalog_sync;若该店恰好缺席,pending 会堆积到 STALE_HOURS 变成 stalled。前置闸②(水位新鲜)是为此设的,但缺席发生在提交之后就挡不住。
- 配额挤兑:形态 B 用 MP_ITEM 桶(api/_client.py:238,8/hour)与 list_new 主链共享,改码跑在 20:00 前后会让当晚上不了架,而摘要只会说「配额不足」看不出是谁吃的。FEEDS_PER_STORE_PER_RUN=2 是留量,但人手动连跑几轮就绕过了它 —— 运行纪律:改码只在 20:00 之后、次日 13:00 之前跑。
- 订单双算:官方对「改码前的 PO 日后返回哪个码」零文档。若返回新码,orders.order_lines 会因 UNIQUE(po_id, sku)(refdata/schema.sql:662)插出第二行,销量/产品分/日报/对账全部多算且不报错。S5+X1 的体检只能**发现**不能**阻止**;发现后需要人工决定合并口径。改码前后各跑一次基线 SQL 是唯一的判据。
- catalog.sku_aliases 只继承**一跳**。设计前提是「旧码改码后立即弃码、永不再改码」;若将来允许对同一个品连续改两次码,顽固/归类/WFS/在途/销量五处判据会在第二跳断链。视图注释里已写明这条前提,但没有任何东西**强制**它 —— 靠 sku_migrations 的 `(store, old_sku) WHERE status='pending'` 唯一索引与候选 SQL 的 `NOT EXISTS … status IN ('pending','confirmed','stalled')` 两道软闸。
- 跟卖(match)默认不迁,意味着切换完成后仓内会长期并存两种码形态(不透明码 + PHUMWMT 串)。这不是缺陷但会让「按形态分流」的直觉失效:所有形态判断必须走 sku_codec.is_opaque / OPAQUE_SQL_PREDICATE / 登记簿,不许有人写第二处正则(守门 test_no_second_opaque_regex_in_the_repo 钉住)。
- 本批次的收益上限必须写清:存量改码**收不回**沃尔玛已掌握的旧 SKU=ASIN 关联(SkuUpdate feed 本身就是「旧串→新码」的显式映射,历史订单与 feed 记录里的关联也在),它是止血不是清创。若所有者以为改完码历史关联就没了,后续的风险判断会建立在错误前提上。
- **abandon 是单向不可逆,全套工作包没有「撤销弃码」的人工入口**(synthesis open_questions #13 未闭合,审查者 2 点名)。中间窗口内运营在 Seller Center 手工改 Site End Date、或形态实测推翻判据时,只能靠人裸 UPDATE 登记簿 —— 而所有守门都禁止 sku_codec 之外的 UPDATE。本批次的缓解是:rolled_back 分支弃的是**新码**(免费),旧码只在 confirmed 时才弃;confirmed 之后要撤销只能靠人工 + 一次反向 SkuUpdate,这条路径**没有代码支持**,是已知缺口,记在 R5 决策日志里等所有者定。

### PR 切分

**三个 PR,前两个可先合并并在生产静置一轮再上第三个。**

**PR-1「地基 + 抑制型/继承型改动」(可独立合并,零行为变化)**:S1/S2/S3/S4/S5(schema 两列 + 新表 + 三视图 + 两索引)、C1/C2/C3/C4/C5/C6(sku_codec 三处 + listing_sources 两个只读函数 + upc_pool.retag_sku)、M1/M2/M3/M4/M5(mp_mapper 三处 + api/feeds 注释与测试 + mp_conform 系统开关放行)、O1-O12(观测侧全部抑制/继承型改动 + dispositions 读写两个积木 + walmart_catalog.drop_node_rows + alloc_survey 销量继承)、X1(订单体检薄壳)、R2/R3(db_schema 与蓝图同步)。存量 replaces/replaced_by 恒 NULL、sku_aliases 恒空集,这一整个 PR 对生产逐字节零行为变化,可以先合先跑一轮,把「抑制型改动没写错」这件事从改码那天剥离出去。**规模**:约 20 个文件、净增约 500-600 行(含测试)。

**PR-2「工作流」**:W1-W6(workflows/sku_migrate.py 全文,约 450-550 行)+ tests/test_sku_migrate.py + R1(schedule 手动清单)+ R4/R5(sku_plan 改写与决策日志)。合并后**只做 dry-run**,等所有者六件实测结果。

**PR-3「实测反哺」**(实测完成后):按实测结果确认或切换 FEED_TYPE(决策 E)、把六件实测结论回写 docs/sku_plan.md §4 与 docs/api_blueprint.md、按需追加决策 A/B/D 的落地条件。

**生产投放不属于任何 PR**:按验收命令里的四级节奏(1 → 10 → 一店 → 全店)人工推进,每级之间必须跑 maintenance_scan preview、node_probe、alloc_survey 三项对比。

---
**回滚方案(本批次此前完全没有,按审查意见补)**
- PR-1:`git revert` 即可;**新列与新表不 DROP**(conventions §五:DROP COLUMN/TABLE 不可回滚,未连库核对一律不执行),三个视图可留可 DROP(零读者后无害)。飞书零改动。
- PR-2:`git revert` 即可(工作流从未进调度,revert 后无人调用)。**但若已经真跑过**:已 confirmed 的改码**不可回滚** —— 沃尔玛侧已是新码,登记簿也已定案;正确处置是停止后续批次、已改的认下来、旧行的 replaced_by/replaces 保留做反查(它们正是 sku_aliases 的数据面,删了顽固/归类/WFS/在途/销量五处判据当场断链)。仍是 pending 的行:先跑一轮 `-p settle_only=1` 让它们各自落定或 stalled,**不要**带着 pending 行 revert(revert 后没人再定案,那些行会永久挂着而且旧码的缺席事件被抑制着没人报)。
- PR-3:纯文档与常量,revert 无副作用。

---
**工时与日历(按审查意见补;关键路径不是写代码)**
- 代码工时:PR-1 约 4-5 人日(O 段十二条虽小但每条要配零行为变化的对照测试);PR-2 约 4-5 人日;PR-3 约 0.5 人日。
- **等待项(关键路径)**:所有者六件单品实测(其中第 1 件 `grep -rl SkuUpdate` 十分钟内可出结果,决定 M5 与决策 E/I,建议**最先做、与 PR-1 并行**);批次 0a/0b/1/2 全部合并并在生产跑过一轮;四级投放节奏之间各等一轮 catalog_sync(每级至少隔一天)。
- **前置协调项(必须在写第一行代码前拍平,否则 0a 合并当天守门互相判红)**:活码唯一索引归 0a、守门测试归 tests/test_sku_guard.py、订单双算体检归本批次 S5 —— 三件都是跨包所有权问题,不是本批次内部能解决的。
