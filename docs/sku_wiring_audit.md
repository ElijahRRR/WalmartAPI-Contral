# SKU 改造接线审计(2026-09-06,PR #104)

> 问题:把「SKU = ASIN」改成自建 12 位不透明码之后,代码层面各个部位是否都接线完成?
> 方法:9 个维度各一名审计者只读全仓,**以代码为准、不以文档为准**,每个触点给 file:line;
> 68 条 not_wired / partial / gap 发现里前 30 条各派一名反驳者独立复核;最后一名批评者
> 对照 PR 的 114 个改动文件找九维漏看的部分。40 名代理、1,183 次工具调用、约 98 分钟。
> 生产验收侧的记录见 §4,由主持者按会话与 docs/sku_plan.md §8/§9 整理。

## 0. 一句话结论

**读侧与上架链已经闭合,守门测试扎实;存量改码链定案半边完整、提交半边停用;
剩下的是 7 个明确缺口 + 一批测试覆盖缺口,没有一处是"新码进来会静默失明"的
主路径断线。** 改码提交通道今天已由所有者的 Seller Center 实测定案(§3)。

| 维度 | 触点 | wired | partial | not_wired | n/a | 复核后仍成立的缺口 |
|---|---|---|---|---|---|---|
| 身份积木 | 29 | 26 | 3 | 0 | 0 | classify 无 opaque 桶;弃码点 1 无 dry-run;PG 守门全在 needs_pg 后面 |
| SQL 硬等号六处 | 27 | 23 | 2 | 0 | 2 | audit_replay 三处不带 store;db_init 回填 INSERT 抢先登记新码为 unknown |
| 形态倒推十五处 | 34 | 23 | 4 | 0 | 7 | resolve_pairs 第三跳仍走形态腿;两条 ASIN 正则口径不一 |
| 上架链写侧 | 16 | 15 | 1 | 0 | 0 | 变体 variantGroupId 明文外带 parent ASIN |
| 飞书表与 registry | 20 | 14 | 2 | 2 | 2 | MAINT_SHEET 无来源码列(已知待办);只有上架表按表头名认列 |
| 存量改码链 | 23 | 21 | 1 | 0 | 1 | 提交通道停用;_sync_sheet 按 (店, ASIN) 反查会撞重复行 |
| 弃码四点与生命周期 | 16 | 15 | 1 | 0 | 0 | 代际上限不分弃码原因;sku_locked_heal 飞书写失败卡死 |
| 归类回填与错误画像 | 29 | 18 | 5 | 2 | 4 | sources_backfill 第三桶告警一次性 |
| 测试与文档 | 51 | 44 | 7 | 0 | 0 | 夹具里的"新码"大多过不了 is_opaque;三条最贵的收口只有 SQL 子串断言 |

30 条复核结果:17 条成立(含 6 条"比审计者写的更严重"),9 条被反驳(审计者看漏了
写侧已固化的 asin 列、或把"没测试"当"没接线"),4 条改判 not_applicable。
被反驳的典型:`blacklist._ASIN_WIPE_SQL` / `risk_trace._EVENTS_SQL` / `error_source.SRC_EVENTS`
的 `coalesce(asin, sku)` 不是漏改 —— `product_events.asin` 在写入当场就由登记簿反查填好
(services/product_events.py:206-215),读侧 coalesce 对新码解出的就是 ASIN。
**这条不变量是全仓十几处读侧的承重墙,目前只靠代码注释,没有守门测试**(见 §2 T-1)。

## 1. 已闭合的部分(代码证据,不是文档)

- **身份积木**:字母表/长度/冷却/代际常量只在 services/sku_codec.py 出生(:74-77/:99/:104),
  SQL 谓词由同一对常量派生(:89);`mint` 抽码与登记同事务,活行查询与两条部分唯一索引
  条件逐字对齐;`abandon` 调用点全仓 AST 实测**恰好四个**(catalog_sync:239 / sku_locked_heal:229 /
  listing_sheet:699 / sku_codec.settle_replacement 内部两处即改码点),反向名单
  product_clear / problem_product_cleanup / maintenance / walmart_catalog / feed_track 零调用。
- **§3.2 六处 SQL 硬等号**:maintenance_intents 四处(:219/:229/:265/:313)、product_audit
  online 两条腿 OR(:488-496,第一条腿裸等号是索引例外、有反向守门)、视图
  audit_listing_conflicts(:681)全部走登记簿。
- **§3.3 十五处形态倒推**:全部 wired 且各有具名测试;`extract_asin` 调用点白名单 AST+文本双轨。
- **§3.4 上架链**:list_new 预备期 mint(:695-726)、预检路占位码(:1194)、三条对账 SQL
  (:334/:790/:850)经登记簿;listing_sheet 的 row_sku / 回执三 dict / heal_unknown 双键 /
  _mark_upc_conflicts 一次 abandon(upc_conflict);sku_locked_heal 五键同源;upc_pool.sku 存真码;
  跟卖链 match_listing:198 走 mint,旧 PHUMWMT 发码器已删。
- **飞书**:上架表 21 列按表头名认列、缺列重名 fail-closed、源码零硬编码字母、SKU 列已真写
  (文档 §3.5「本批只加列不写值」已过时);订单两表「来源码」、在线产品总表 Q 列都在 registry 出生。
- **存量改码链定案半边**:九条判据单一出处、六道闸、_verdict 只信观测、_confirm 先查滞留动作、
  观测侧两条豁免(item_appeared / item_missing)落在 walmart_catalog.py:144-178。
- **api 层**:本 PR 对 api/ 的改动是 8 行注释,零可执行代码,铁律 2 未破。
- **registry/schedule.py** 只改 docstring,JOBS 未动,skills/ 无需重生成。

## 2. 复核后仍成立的缺口(按优先级)

### 2.1 会改坏数据或违反纪律的(建议本 PR 内修)

> **2026-09-06 处置结果**:G-1 / G-2 / G-4 已修;G-3 与 G-6 按所有者决定**整体删除**
> 那两处判型代码(db_init 回填 INSERT 删除,sources_backfill 改成只登记不猜,人工归类走
> sources_reclassify);G-5 按所有者决定**删掉代际上限**(不筛原因,而是整道闸不要);
> G-7 变体组 ID 留待变体分组那一批。全量测试 3048 passed / 48 skipped。

| # | 位置 | 事实 | 后果 | 修法(工作量) |
|---|---|---|---|---|
| G-1 | workflows/catalog_sync.py:35/:237-241 | DANGEROUS=False 且全文不读 dry_run;弃码点 1(abandon + 烧号)在 `--dry-run` 下照样执行 | 空跑会真弃码真烧号,不可逆,横幅也不打 | abandon 段读 params["dry_run"],空跑只报数(小) |
| G-2 | workflows/audit_replay.py:126/:152/:234 + :537/:582/:610 | 三条 SQL 不取 store,三处 `resolve_pairs([(None, sku)])` ⇒ 登记簿腿永不执行,新码只能走形态腿必返 None | 新码被拒过的品以正例身份进底线样本;代码注释自认缺口 | SQL 带出 store 并传对(小) |
| G-3 | refdata/schema.sql:318-324 + workflows/sources_backfill.py:105-112 | db_init 每次都跑的回填 INSERT 与 sources_backfill 的 register 都把「walmart_items 有、登记簿无」的不透明码登成 unknown/NULL | 「疑似新码漏登记」告警只响一次即永久沉默;maintenance 三条 amz JOIN 对该码永久失明 | 两处排除 OPAQUE_SQL_PREDICATE,orphan 留着继续报警(小) |
| G-4 | workflows/sku_migrate.py:586 | `_sync_sheet` 按 (店, ASIN) 反查上架表行,同店同 ASIN 多行只留最后一行 | 新码写到另一行,原行 SKU 列停在旧码,两边不报错 | 改按 (店, 旧码) 用 row_sku 定位(小) |
| G-5 | workflows/list_new.py:377-383 | 代际上限 `_SQL_ABANDONED_GEN` 不筛 abandoned_reason | sku_migrate 一次成功 + 两次回滚就让该 (店, ASIN) 永久「换码次数达上限」 | 只数烧号类原因(小;先请所有者确认语义) |
| G-6 | refdata/schema.sql:320 vs workflows/sources_backfill.py:50 | 注释说「同一条口径」,实际 `^B0[A-Z0-9]{8}$` 与 `^B[0-9A-Z]{9}$` 不等;守门只钉锚点 | 两处对同一批旧串判型不同 | 统一成一条,守门钉逐字相等(小) |
| G-7 | services/variant_group.py:150-156 + services/mp_conform.py:587 | 变体家族的 variantGroupId 仍是 `vg_<parent ASIN>` 明文发给沃尔玛;单品口径已换新码 | **目标级漏洞**:变体品仍能从沃尔玛侧看出 ASIN(sku_plan §8 已列为转出项) | 组 ID 改由 mint 出的家族码派生;变体分组本身是后置项,与所有者定时点 |

### 2.2 可观测性与语义(下一批)

- **L-1** services/sku_asin.py:114-123 `classify` 无 opaque 桶,12 位新码落「其他」;
  `resolve_pairs` 计数与 `samples` 都在反查之前按 classify 分桶 ⇒ 清洗链摘要的「其他 N」会随
  新码增长,人会以为规则漏了。第三跳(:197-205,item_id 全局倒查)仍 `extract_asin`,结构上不带 store。
- **L-2** services/listing_sheet.py:698-712 `_mark_upc_conflicts` 对登记簿无活行的对既不弃码也不烧号,
  只 warning,返回值只数成功 ⇒ 摘要「撞库 N」少报。
- **L-3** workflows/sku_locked_heal.py:208-235 顺序是 冷却台账 cleared → abandon+burn 提交 → 写飞书;
  飞书写失败时该行既不进 ripe 也不进 FAILED 重试,没有工作流再管它。
- **L-4** workflows/catalog_sync.py:237 `verify_deletions` 全库无 store 过滤,`-p store=X` 排障也会
  处置其它店的待核验删除;摘要只有一行汇总。另:services/product_events.py:304 把 RETIRED 判 gone
  ⇒ 弃码 + 烧号,与 09-06「RETIRED 是删不掉的死档」定稿的关系待所有者定(号确实还绑在那条记录上,烧是对的;码弃掉后同 (店, ASIN) 会拿新码新号重上,是否允许?)。
- **L-5** services/blacklist.py:239-241 `collect_brands` 的 `or sku` 兜底没有 record_asins 那样的日志计数,
  违反 conventions §六「真兜底三要件」。
- **L-6** registry/resources.py:1295 MAINT_SHEET 无来源码列、RETIRE_SHEET 不回显来源码 —— 都是 §8 已知待办,不算回归。
- **L-7** 只有 LISTING_SHEET 有 headers/fail-closed;其余四张电子表格仍按位置读写;
  services/maint_sheet.py:376 有 11 条中文表头字面量(prune 重写表头用),与铁律「飞书字段名只准引用 registry 常量」相抵。
- **L-8** workflows/error_reclass.py:299、services/product_pool.py:98、services/product_score.py:227 都以
  `coalesce(asin, sku)` 当 ASIN,正确性完全依赖「product_events.asin 写入时已填」这条不变量;
  事件 asin 为 NULL 时风险档案会从 ASIN 一行裂成两行,唯二消费方零告警。
- **L-9** workflows/audit_history_fold.py:76-77 绕过 `product_events.record_many` 直插事件表,是一条旁路(§六)。
- **L-10** 术语撞车:workflows/error_reclass.py / blacklist_route.py 里的「新码」指品类归类码,与本 PR 的「新码」无关,注释与文档要分清。

### 2.3 测试覆盖(不是接线缺口,但让上面的一切只靠人眼)

- **T-1** 「product_events.asin 写入时经登记簿填好」这条承重不变量没有守门测试;十几处读侧的
  `coalesce(asin, sku)` 都靠它。
- **T-2** tests/ 里当「新码」用的串大多过不了 `is_opaque`(含 0/1/O/L/U 或只有 11 位):
  test_list_new.py:90/852、test_alloc_*:89/129/310/547/1257、test_dispositions_router.py:461、
  test_risk_trace.py:285、test_catalog_sync.py:885、test_sku_codec.py:211;其中三处是真往
  listing_sources INSERT 的 PG 用例,落不进部分唯一索引 ⇒ 证明的不是它们声称的东西。
  建议 conftest 出 `opaque_code()` 工厂 + 一条守门扫 tests/。
- **T-3** 弃码点 1 在整套测试里从未执行(test_catalog_sync 四处把 verify_deletions 桩成空)。
- **T-4** 三条最贵的读侧收口(maintenance_intents 三条 SQL、list_new 三条 SQL、product_refresh)只有
  SQL 子串断言,没有「不透明码 + 已登记 source_key ⇒ 被选中」的行为用例;test_alloc_products 夹具
  sku = source_key 同值,改回 extract_asin 全套仍绿。
- **T-5** 五条 PG 守门(两条部分唯一索引方向、在途双活行、认领位唯一与让位、is_opaque↔SQL 谓词)
  全在 needs_pg 后面;容器里 48 skipped 中 47 条是「沙箱 PG 未启动」;仓内无 CI。
  所有者机器上跑过 3022 passed 的那次 skip 数请核对是否更少。
- **T-6** tests/conftest.py:63-80 autouse 预置 `_LAYOUT`,真表头识别的 fail-closed 路径在全套里默认短路
  (守门用例自行覆盖那个夹具,所以那四条仍有效;但其它 3,000 条用例都看不见表头错位)。
- **D-1** docs/conventions.md:299 四个弃码点表第 4 行写 `workflows/sku_migrate.py`,守门白名单实际是
  `services/sku_codec.py`(settle_replacement);refdata/schema.sql:356 upc_pool 注释仍是未来时。

## 3. 存量改码链:停用原因与今日定案的通道

> **2026-09-06 已切换**:`FEED_TYPE = "MP_ITEM_MATCH"`,`SUBMIT_DISABLED` 清空,载荷走
> `match_feed.build_match_item`(GTIN 优先、现挂价与采集重量原样发回、重量兜底值不许发),
> 反哺器加 workflow 过滤,形态 A 残留(build_sku_update_item / SkuUpdate 放行)已删。
> **2026-09-06 第一级投放又补一条:REPLACE 会把载荷没带的库存写空**。当时的修法是
> 定案时回写(`_restore_inventory`),⛔ **2026-09-07 已作废**:通道升 v5 之后
> **库存随改码 feed 一起写**(`Item.inventory`,qty = 旧码最后观测的
> `catalog.walmart_items.avail_qty`,FC 走 `store_limits.listing_fc`),**定案不回写**
> (所有者定稿:同一份数据不走第二条写路径);没带上库存的行交给维护链下一轮按 amz
> 库存重算。全文见 docs/sku_plan.md §9.12;下文保留切换前的记录。

- **现状**:`workflows/sku_migrate.SUBMIT_DISABLED`(:122)非空 ⇒ run() 把 cap 硬置 0,dry-run 与真跑都
  不列候选、不 mint、不发 feed;`_settle` 照跑。技术原因:US MP_MAINTENANCE 5.0.20260608 的
  Orderable 无 SkuUpdate,09-04 两条改码 feed SUCCESS 而线上不动(sku_plan §9.10);两条 pending
  已于 09-05 由 settle_only 判 rolled_back,旧码复活。
- **通道定案(2026-09-06 所有者 Seller Center 实测,A085朱丽霖 feed `18D2A25BB3895D7096CF1357C17C2A36@AYYBBwA`)**:
  「Match items」模板头部 `Version=5.0.20260703-18_22_27,MP_ITEM_MATCH,mp_item_setup_by_match`,
  行内只有 specProductType / productId(GTIN 14 位)/ productIdType / productName / sku / condition /
  mainImageUrl / ShippingWeight / price,**无 SkuUpdate 字段**。探针结果:新旧码 wpid 同为
  `5FK5P1SAT7OM`,库存 5 → 5 跟着过来(模板库存列为空),价格不变,旧码 GET 404,feed 1 条 SUCCESS。
  ⇒ **同一 item 原地换码**,机制 = 同 GTIN + 新 SKU + REPLACE。模板同时给出沃尔玛 SKU 规格:
  「Alphanumeric, 50 characters」(§8「沃尔玛 SKU 规格」待决项由此关闭,12 位在内)。
- **切换清单**(一次改完,不分批):
  ① `FEED_TYPE = "MP_ITEM_MATCH"`,清空 `SUBMIT_DISABLED`;
  ② `_build_items` 复用 `services/match_feed.build_match_item`:按 GTIN 拉 SPEC 预填(api/items.search_walmart_spec),
     填新码 + walmart_items 现挂价格;重量沿用 mp_mapper.shipping_weight 同一来源(REPLACE 会覆盖重量,不能兜 1.0);
  ③ 删 `mp_mapper.build_sku_update_item` 与 `mp_conform.ORDERABLE_SYSTEM_SWITCHES` 的 SkuUpdate 放行(形态 A 残留 = 双轨);
  ④ 反哺器加 workflow 过滤:services/match_sheet.sync_from_ledger 与 listing_sheet._SQL_HEAL_RECEIPT(:727-735)
     都不看 workflow,改码回执不得被当成跟卖/上架回执;
  ⑤ 配额:MP_ITEM_MATCH 桶 15/h 与跟卖链共享(api/_client.py:238),FEEDS_PER_STORE_PER_RUN 注释改口;
  ⑥ 候选 SQL 带出 gtin / price;`listing.sku_migrations.feed_type` 记 MP_ITEM_MATCH;
  ⑦ 守门测试 `test_feed_type_constant_is_the_only_place_that_names_a_feedtype` 同步;文档 §8 决策 E/I 收口、§9.12。
  未决:仓里 MP_ITEM_MATCH 是 v4.2(sellingChannel header),模板是 v5.0;v4.2 能否同样换码由第一级投放
  (limit=1)实测定,不另写探针。
- **手工改码遗留的数据**:登记簿里 B0CRKFQZWF 仍是活码(未标 replaced_by),Test851 被 sources_backfill
  登成 unknown ⇒ 维护链对 Test851 失明。修法:sources_reclassify 把 Test851 归 amz/B0CRKFQZWF;
  `sku_codec.abandon(conn, 'A085朱丽霖', 'B0CRKFQZWF', ABANDON_SKU_UPDATE, replaced_by='Test851')`。

## 4. 验收情况(生产侧,截至 2026-09-06)

| 批次 | 验收动作 | 结果 |
|---|---|---|
| 0a/0b 读侧收口 | main ⇄ 分支 maintenance_scan dry-run 对拍(谭总11) | 意图 343 → 351,+8 全部解释为收口买到的行(§9.5);两次同分支连跑逐字节相同 |
| 0b 飞书来源码 | 订单两表 / 在线产品总表 Q 列 | 所有者 09-02 建列,程序已写值 |
| 1 上架表 | 21 列 layout()、SKU 列 C | list_new 真跑落 C 列;T/U 人工列不被碰 |
| 2 写侧切换 | `list_new -p store=谭总12 -p limit=1` | 第一个不透明码 `AACSVCEH397R` 上线(09-04):登记簿 / 上架表 / feed 台账 / UPC 池四处同一个品说同一件事 |
| 2 归类 | sources_reclassify 待归类 CSV(含「确认来源类型」列) | 所有者 09-03 补全并导入;来源码抽取扩到双横杠形态,剩余人工 |
| 2 回归 | list_new / feed_poll / sources_reclassify dry-run;product_audit from_sheet;maintenance_scan dry-run | 通过;product_audit 的 418 条 rejected 建议 `-p from_sheet=1 -p force=1` 复审(未做) |
| 3 存量改码 | 谭总12 / A085朱丽霖 各 1 条真跑 | 回执 SUCCESS、线上不动 ⇒ settle 判 rolled_back;通道停用;今日定案 MP_ITEM_MATCH(§3),代码未切 |
| 3 判据 | 「已上架」/ 逐候选只拦破坏组 | 生产 dry-run 逐条理由可读,真跑按预期被拦/放行 |
| 附带 | 标题维护整路停闸(09-05)、RETIRED 全豁免(09-06) | 已落地;RETIRED 豁免的 problem_scan dry-run 计数待所有者跑一次对比 |

**尚未验收 / 仍暂停**:定时任务(product_audit from_sheet / list_new / feed_poll / sku_locked_heal)
仍停;谭总4/谭总5 配送限制核对后再放 product_chain;标题链恢复条件两件(沃尔玛口径标题生成 +
「未采纳」记账)未做;§8 未拍板项:生成时点(现按 C)、决策 D 跟卖不迁(默认)、决策 H 销量归属
(别名视图,已实现)、UPC 池表 E 列(默认 a,已是真码)、退役表 B 列与维护记录表来源码列(待办)。
