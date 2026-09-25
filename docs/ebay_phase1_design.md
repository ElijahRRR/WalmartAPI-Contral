# eBay 一期设计方案(跑通到上架)

> 2026-08-30 定稿(同日经三路对抗校验修订:官方事实 18 条复核 0 错,仓库
> 一致性与完备性共 5 blocker + 约 30 项已全部收编)。基于批次 0 拍板(§0)
> 与六路代码级调研。**实现一期以本文为准**;字段级参考(请求体/错误码/
> 户口最小请求体/AspectConstraint 全表)在 **`docs/ebay_phase1_reference.md`**
> ——除该文件显式承接的四块外,调研证据结论已收敛进本文。与
> `docs/ebay_plan.md`、`docs/ebay_api_blueprint.md` 冲突处本文胜(08-25
> 两文档的已知错误集中在 §9 勘误清单,P1-1 首任务统一回改)。一期不做的域
> (订单/售后/结算/维护巡检/变体)仍以 ebay_plan 批次 5~11 为纲。
> **2026-09-03 对齐 main #85/#99/#100~#110**(一店多仓、店铺事件账本+风险
> 追溯、报错归类、下单时间、绩效归因):新增 §4.4(risk_trace 四证据源平台
> 谓词,blocker)、§2.1 事件桥跳过 eBay、§6.1 多仓与报错归类两条边界;
> claims/load_active 调用面已现场重核。
> **2026-09-07 对齐 main(71 个提交,含 SKU 改造 PR #104 全批次落地)**:🔴
> **§3.3 的 SKU 策略被推翻重写**——沃尔玛侧已全面切 12 位不透明码
> (`sku_codec`)、`catalog.listing_sources` 成为 SKU 身份唯一登记簿,eBay 一期
> 因此改为**复用 `sku_codec.mint`** 而非原稿的"SKU=ASIN 原文"(四条理由与
> 用法见 §3.3);连带改 §4.1 登记簿行、§4.3 asin 反解理由、§6.4 与 §6.9
> (变体组号走 `mint_group_code`)。
> **2026-09-10 对齐 main(#122~#128)**:补两条纪律边界——快照新鲜度与补采
> 判据唯一出生地 `amz_source.SNAPSHOT_FRESH_HOURS/latest_seen`(§2.3)、
> LLM 模型与 thinking 登记走 `registry.LLM_THINKING`(§5.3);其余(catalog_sync
> 切片、item_id 报表、problem_scan 原子归类、sku_migrate 影子双挂)与一期
> 无交集。
> **2026-09-14 对齐 main #132**:§6.1 补**二手/翻新闸**——判据唯一出处
> `maintenance_intents.used_offer()`;库存三态闸挡不住二手(二手 offer 通常
> 有货),而一期 `condition` 固定 NEW,源是二手却按新品上架是硬违规。
> **2026-09-17 对齐 main #133**(alloc_plan `-p from_sheet=1` 点名分配):
> ① §2.1 claims 调用面重数——alloc_plan 现在是**两条并列的领用路径**,
> 平台化与"事件桥跳过 eBay"两件事各要落两遍(11 读 / 3 写 / 3 事件桥);
> ② §2.2 UPC 改动清单整段是死指针,按现场重核重写(`burn_for_retire` 早
> 已删除,真正的平台化落点是 `sku_codec.abandon` → `upc_pool.burn`);
> ③ §6.1 补 `product_pool.load(asins=)` / `norm_channel()` /
> `score_all(gated_by_asin=)` 三个接口对齐,并把新的 `services/sheet_layout.py`
> 定为按表头认列的唯一实现(eBay 上架表投影必须用它 + 登记守门测试)。
> **2026-09-19 对齐 main #134**(多仓校验根治:校验记忆落库、读不到≠不认识、
> 补试、原因进摘要):§3.2 补 🔴 **vendor 词两处同源**(#134 把沃尔玛 api 文案
> 改成「沃尔玛返回 {status}」,而 `diagnose` 的正则 vendor-blind,归类词的
> vendor 只来自参数——eBay 两处各写各的会静默对不上)+ 守门行号 241→247 +
> 「守门只扫 workflows 不扫 services」;§3.4 补 🔴 **「200 但解析不出」抛错
> 且不进缓存** 与 🔴 **「读不到」≠「不认识」** 两条定式(令牌/getPrivileges/
> 类目树三处同形;入料的 fail-closed 拦下要把两种原因分开计数)。
> **2026-09-23 对齐 main #135**(alloc_plan 点名模式缺省不设淘汰线):§6.1 补
> 🔴 **eBay 入料一期不设分数淘汰线**——#135 的判据逐字适用(那条线量的是
> "证据多不多",而证据来自评论数与**我们自己店里**的销量,eBay 候选两样
> 天然没有),照搬 `product_score.CUTOFF` 会把整池一票否决;分数只决定
> 排序。§2.1 的点名分配行号随 #135 重定位(1222/1223→1229/1230、
> 1300→1307、1301→1308)。
> **2026-09-24 对齐 main #136**(order_audit 批次收口漏洞):§6.5 补三条 🔴
> ——① `submit_poll` 选行面取**并集**(`feed_log` 自己 pending ∪ `feed_items`
> 有未落定行),#136 正是只看下游信号、被另一条路径抹掉信号后永远停在
> running;② 在途超 `_INFLIGHT_STALE_HOURS=24` 按 timeout 收口,但 **timeout
> ≠ 确认未达**,eBay 侧绝不自动重发(写操作不兜底);③ §6.1 去重谓词必须
> **并上 `feed_log` 仍 pending/timeout 的 (account, sku)**,不许靠"对账跑在
> 推送前"这个调度顺序——丢响应的提交没有 `feed_items` 行,谓词看不见它。
> **2026-09-25 对齐 main #131 + #137**(feed 台账 v2:按官方期限收口、首次落定
> 即定稿、实际结果分表、pending 对账;每店最大库存):§6.5 把昨天自造的
> `_INFLIGHT_STALE_HOURS=24` **作废**,改照 main 的整套口径 —— 期限表
> (`walmart_slas.tsv`「按官方值、不加余量、不猜测」)→ `FEED_DEADLINE_MINUTES`
> 按 feedType 分档 + `UNREADABLE_GRACE_HOURS`,eBay 另起 `refdata/ebay_slas.tsv`;
> 🔴 照搬 `NO_VERDICT_STATUSES`(overdue/unrecognized/unreadable = **平台没给
> 结论,不是失败**)与「首次落定即定稿 + `raw_status`/`settled_by` 留证」;
> 🔴 pending 对账三条(只读反查 / SKU 集合完全一致才收编 / 到期落 failed 不
> 自动补交),**`post_started_at IS NULL` 是「确定没发出」的唯一硬证据**;
> 🔴 在途口径唯一出处(对位 `feed_track.IN_FLIGHT_SQL`);🔴 去重谓词**改排除
> 法**(白名单在 status 扩到七档后必漏);🔴 新增「提交成功 ≠ 真的生效」,
> 对位 `ops.feed_effects`/`feed_effect.judge`,一期只做前者且不挂自动化。
> §2.3 补 🔴 库存换算唯一出生地 `store_limits.stock_for`(门槛
> `amz_source.MIN_INVENTORY=5` 管卖不卖、每店 N 管卖多少,eBay 复用函数只换
> cap 来源)。§4.1 补 feed 两表新列的 eBay 填法。
> ⚠ 本文引用的仓库行号以 **2026-09-23 的 main(06140c3)** 为快照(§2.1/§2.2/
> §3.2 已按它重定位;其余节仍是 09-07 快照),批次实施时现场重定位。

## 〇、批次 0 拍板记录(2026-08-30,所有者)

| # | 判据 | 拍板 | 备注 |
|---|---|---|---|
| ① | 品牌/产品冻结 | **不跨平台**(每平台唯一;沃尔玛占用的品牌/ASIN eBay 也可上) | 所有者当日先拍"跨"后更正为"不跨",与推荐一致 |
| ② | 双平台库存 | **各自与亚马逊保持同步**(库存来源=采集快照,无跨平台扣减器) | 与推荐一致 |
| ③ | UPC | **跨平台复用**(同一产品两平台同一个号) | 推翻 §3.4 的 S2,激活 S1 拆表(§2.2) |
| ④ | 账号与代理 | 每账号固定出口代理必做、缺代理整账号跳过;**账号数量待所有者填实值**(设计按 ≥1 且可多) | 与推荐一致 |
| ⑤ | 审核链 | **一期复用沃尔玛 `audit_status`**(approved 才可上 eBay) | 方向 A |
| ⑥ | 无货源模式 | **书面确认要做**(从亚马逊下单直发买家)。⚠ 风险登记不变:eBay 政策明文不允许从其他零售商代发,执法风险由经营侧承担,工程侧以账号健康盯盘与单账号试点控暴露面 | 书面拍板完成 |
| ⑦ | 合规入站面 | 已解释(webhook 收 eBay 用户注销通知、删其个人数据;我方落库订单数据 ⇒ 豁免不适用)。**一期不阻塞**;界定见 §8 P1-5 行 | 排期待拍 |
| ⑧ | 订单行身份键 | **暂定**(订单链不在一期,不阻塞) | 悬置 |
| ⑨ | 类目映射 | **LLM 做 amazon node → eBay categoryId 匹配;只能测试做**——LLM 只产建议,人工放行后上架链才消费(§5.3) | 细化落地 |

## 一、一期范围

**目标:单账号在 eBay 美站完成真实上架并拿到回执闭环。** 链条:
授权(人工一次)→ 户口(政策/仓位)→ 类目树与映射(LLM 建议+人工放行)→
选品(库侧取数,复用中立积木)→ 领冻结/领 UPC → 构造并提交
inventoryItem→offer→publish → 提交台账三态 → `ebay_submit_poll` 回执收编 →
飞书投影。**非目标**(一期明确不做,防止顺手实现):订单/发货/售后/结算、
价格库存维护巡检、变体组(§6.9)、跟卖、Promoted Listings、合规入站面
(批次 11)、`ebay_catalog_sync` 回读链(批次 5;一期 `catalog.ebay_items`
是**空表**,不许给它造写入方——SKU↔UPC 权威在 `catalog.upc_usage`,
SKU↔offerId/listingId 权威在 `ops.feed_items`)。
**冷启动顺序**(首轮必须按此序,漏一步的表现是下游 fail-closed 全拦且不
报错):db_init → 飞书账号表填好 → `ebay_authorize` → `ebay_bootstrap_account`
→ `ebay_taxonomy_sync`(树)→ `ebay_catmap_suggest` → `ebay_catmap_promote`
(置高)→ `ebay_taxonomy_sync`(补新放行类目的 aspects)→ `ebay_list_new
--dry-run` 人眼确认 → 真跑。

## 二、判据落地总设计

### 2.1 拍板 ①(不跨平台)→ claims 平台化

回到 ebay_plan §3.3 的方向,按当前代码(#93 之后)校准:

- 唯一索引:`claims_active_uniq (kind, claim_key) WHERE active` →
  **`claims_active_platform_uniq (platform, kind, claim_key) WHERE active`**
  (schema.sql 原行原地替换 + `DROP INDEX IF EXISTS claims_active_uniq`,
  迁移块按 §4.5 纪律)。加 `platform text NOT NULL DEFAULT 'walmart'` 列
  (SQL DEFAULT 里的字面量是唯一例外;代码一律引 `registry.platforms` 常量,
  §3.3)。
- `services/claims.py` 现为**五条 SQL**(#93 已删 `_BY_STORE`/`owner_of`/
  `counts_by_store`——ebay_plan 写"六条"过期)。改法分两类:
  **`_INSERT`:加 platform 列 + `ON CONFLICT` 推断目标改
  `(platform, kind, claim_key) WHERE status='active'`**(索引换名后推断
  子句不同步会直接报"找不到匹配的唯一约束";只加谓词不加列则 eBay 行静默
  落 DEFAULT walmart)——**其余四条(`_OWNER/_LOAD/_RELEASE/_PREVIEW`)加
  platform 谓词**。`load_active(conn, kind, *, platform)` 的 platform 做成
  **必填关键字参数**;每平台唯一后 `_LOAD` 的 `dict(cur.fetchall())` 塌陷坑
  真实存在(同 key 两平台各一行),谓词本身就是修复。
- **调用面全量**(2026-09-17 对齐 main #133 后现场重核;P1-2 验收写明
  "以下红/改是预期内的"):读侧 `load_active` **6 文件 11 处**
  (alloc_push:83 / alloc_plan:254,255 **+1229,1230** / claim_audit:96 /
  alloc_audit:187,188 / alloc_products:102 / list_new:453,456)全部显式传
  walmart 常量;写侧 `claim_many` **3 处**(alloc_plan:430 **+1307**、
  alloc_backfill:162)——⚠ 它在 #85 后
  **返回三元组 `(ok, conflicts, landed)`**,两处调用点已按三元组解包,平台化
  不改这个形态;测试替身 test_alloc_push:43、test_alloc_plan:221/265/326…、
  test_claims:104/186 等。**`try_claim` 仍返回 store 字符串**(同平台性由
  `_OWNER` 的 platform 谓词保证;Owner 具名改形留二期)。
- 🔴 **alloc_plan 现在有两条领用路径(2026-09-17 对齐 main #133)**:
  缺省的全库分配(254/255 读、430 写、434 记事件、`SOURCE`)之外,
  `-p from_sheet=1` 点名分配(口径 #19)自带一整套(1229/1230 读、1307 写、
  1308 记事件、**`SOURCE_SHEET`** = `alloc_plan_sheet`)。平台化改造**两条都要改**——它们不是
  同一段代码的两个分支,是并列的两段;只改缺省那条,点名分配会拿**不带
  platform 谓词**的占用表去判冲突(eBay 行会挡住沃尔玛点名),而且
  **事件桥那条 🔴 "跳过 eBay" 的规则在 1301 也得再落一遍**。P1-2 验收的
  "预期红清单"按 **11 读 + 3 写 + 3 事件桥** 数,不是旧稿的 9+2+2。
- 🔴 **事件桥必须显式跳过 eBay(#99 新增,本设计原稿完全没有)**:`claims`
  在 #99 后多了两个桥函数 `claim_created_rows`(alloc_plan:434 **+1308**、
  alloc_backfill:165)与 `released_rows`(store_release:216/325/385),它们把
  占用/释放写进 **`ops.store_events`**——而那张账本的身份键是 `store`、
  语义是**沃尔玛店铺状态迁移与 TRO 封店预警**(`store_watch` 按 store 扫描
  并分级)。eBay 账号名写进去 = 账本里出现一家**不存在的沃尔玛店**,其占用
  事件还会混进沃尔玛店的封店预警计数,**两个方向都不报错**。一期定稿:
  **eBay 上架链不记 `store_events`**(eBay 没有对位的店铺状态迁移语义),
  在 eBay 领用分支显式不调这两个桥并写明理由;二期若要给 eBay 建店铺账本,
  `ops.store_events` 需要自己的平台维度,不许直接混写。
- `_RELEASE/_PREVIEW` 的 platform 对 **store_release 必填**:它支持
  `-p asin=`/`-p brand=` 不带 store 的手工释放,每平台唯一后按 key 释放会把
  **另一平台的占用一起放掉且不报错**(r3 当时按旧口径判"不触发"的豁免已随
  ① 更正作废)⇒ `store_release` 加 `-p platform=`(缺省 walmart),摘要点名
  释放了哪个平台;runbook 的 eBay 账号释放条目同步(连同 `-p mark_offline=0`)。
- try_claim 写侧时机:放在**领 UPC 的同一事务里**(入料到提交之间隔着构造
  与校验,期间可能被同平台另一账号占走)。
- **账号名互斥硬约束**:`services/ebay_accounts._normalize` 断言
  `stores.registered_names() ∩ eBay 账号名 == ∅`,不满足即抛。🔴 用
  **registered**(在册)不用 enabled——被守的表按 `store` 圈定与启用位无关,
  停用店名仍占着行(`services/stores.py:70-74` docstring 点名过)。

### 2.2 拍板 ③(UPC 跨平台复用)→ 用量拆表(S1)

`catalog.upc_pool` 主键是 `upc`、领用信息是主表裸列——一个号只记得住一个
使用者 ⇒ 新建用量表,把挤在一个 status 里的两件事拆开:**池位**
(`upc_pool.status`,取值集一个不改)答"还能不能发给新人";**用量**
(`catalog.upc_usage.status ∈ claimed/used/retired/released`)答"谁正在用"。

- `catalog.upc_usage`:PK `(upc, platform, store, asin)` + sku/claimed_at/
  used_at/released_at。存量回填自 upc_pool 裸列(platform=walmart 常量)。
  ⚠ 不建 `(platform,store,asin)` 唯一索引——同店同 ASIN 历史上合法地有过
  多个号,建了回填当场炸。
- `claim()` 三级取号:**L1** 本平台本店该 asin 活跃用量取最早(保住
  2026-08-19 `ERR_EXT_DATA_0101211` 实证语义)→ **L2** 全平台该 asin 活跃
  用量取最早,复用同号并为本平台 INSERT 新用量行(拍板 ③,**双向**)→
  **L3** 新领(`FOR UPDATE SKIP LOCKED` 同今天)。
  🔴 **L2 候选必须排除「该 upc 在本 (platform, store, asin) 已有 retired
  用量」的号**——烧号优先级高于复用:否则沃尔玛 SKU_LOCKED 烧号后(池位因
  eBay 在用保持 used),下一轮 L1 落空、L2 会把**刚烧掉的号原样拿回来**,
  踩回 0101211 死亡路径,且 INSERT 撞已存在的 retired 行主键。配单测:
  "eBay 在用同号时,沃尔玛烧号后重上必须领到新号"。
  🔴 **`claim()` 入口 `pg_advisory_xact_lock(hashtext('upc:'||asin))`**:
  每平台唯一下"两平台同时为同一 ASIN 首领各拿一个新号"的竞态是活的,锁是
  唯一防线(写进头注)。
- **烧号保护**:`burn_for_retire` 改两步——先只标**本平台**用量 `retired`;
  仅当该号再无任何平台活跃用量时才置池位 `conflict`。单平台场景与今天逐字
  一致(沃尔玛回归对拍能过的唯一原因)。撞库 `mark_conflict` 反向:池位
  无条件 conflict(号不再发),活跃用量不动(不碰 eBay 在架 listing)。
  ⚠ 烧号后两平台号分叉(沃尔玛重上领新号),可接受,记 `product_events`。
- **改动清单(2026-09-17 对齐 main 现场重核;旧稿这一条整段是死指针)**:
  **写侧 7 处**——`claim/mark_used/release/burn/retag_sku` 签名加 platform
  (+store),调用方 list_new:1991(claim)、list_new:280(mark_used)、
  list_new:297(release)、listing_sheet:800/802、**sku_codec:601**、
  sku_migrate:855(retag_sku)。**读/投影侧 6 处**随 `lookup`/
  `project_to_sheet` 改造同批:upc_sync:34/37/38、list_new:1306/1335/1336。
  **测试 2 文件**:tests/test_upc_pricing.py:61-170、
  tests/test_list_new.py:338/388/400。
  ⚠ **旧稿点名的 `upc_pool.burn_for_retire` 早就不存在了**(批次 2 决策 D
  已删,烧号只剩 `upc_pool.burn(conn, pairs, status)` 一条实现路径),
  平台化真正要改的是弃码链 **`sku_codec.abandon` → `upc_pool.burn`**
  (sku_codec:601 是全仓唯一写入点,`_BURN_STATUS` 三个原因映射在
  sku_codec:146-148)——照旧稿去找那个函数会扑空,然后漏掉整条弃码链。
  ⚠ **`sku_locked_heal` 已不碰 upc_pool**(旧稿 :210 是死指针):
  SKU_LOCKED 退役的烧号现在由 `sku_codec.abandon` 代劳,**不要**在那里
  加 platform。**`listing_sheet._mark_upc_conflicts`(本体 :562、调用 :871)
  改查 `catalog.upc_usage`**(platform=walmart + status IN (claimed,used);
  它读的 `upc_pool.sku` 即将变死列,原查询会恒返回零行、撞库号不再被弃用
  ——"行为不变"仅指查询范围不带 store 这点不变)。**`upc_pool.lookup` 与
  `project_to_sheet` 改走 `upc_pool LEFT JOIN upc_usage`**(状态取池位、
  店铺/SKU 按活跃用量拼、日期取 max(used_at);STATUS_CN 与列序不改)——
  不改的话飞书「UPC池」表会被批量刷成空白(投影只写"变了的行",死列让每行
  都"变了")。P1-2 验收补:飞书 UPC 表投影改造前后**逐行对拍**。
- upc_pool 五个领用列照 `catalog.products` 五死列先例**先留列不删、不读
  不写**(上述三处读点改完之后这句才成立)。

### 2.3 拍板 ②⑤ → 入料与库存口径

- 库存:上架时 `availableQuantity` 来自 `catalog.latest_snapshot` 三态
  (NULL≠0 铁律沿用),缺货/未采到不上。持续同步是二期维护链。
  🔴 **但"写多少件"的换算只有一个出生地(2026-09-25 对齐 main #137 所有者
  定稿)**:`services/store_limits.stock_for(stock, cap) -> (数量, 判定码)`。
  门槛与上限**各管一件事**:全局门槛 `amz_source.MIN_INVENTORY = 5` 决定
  **卖不卖**(低于它不上架),每店「最大库存」N 决定**卖多少**
  (`min(亚马逊库存, N)`);判定码 `QTY_NO_COUNT / QTY_BELOW_MIN /
  QTY_CAPPED` **只在那里出生**,两条链按码分支、不各自重判。eBay 侧
  **复用这个函数,只换 cap 的来源**(沃尔玛按店读飞书限额表;eBay 按账号读
  §3.3 登记的 eBay 账号表),**严禁自写 min 或自设门槛**。⚠ 门槛是**源侧**
  判据(亚马逊那边有没有货),与卖到哪个平台无关,所以 eBay 用同一个全局
  常量、不另立一个——另立就是两条链对"这个品能不能卖"给出不同答案。
  ⚠ 顺带照抄一条读数纪律:限额表里"填了但不是数字"(「3件」「1,000」)按
  没填处理**但要出声**(#137 补的 warning)——静默当空 = 那家店的闸悄悄
  失效,而表上看着明明填了。
  🔴 **补采与新鲜度判据只有一个出生地**(2026-09-10 对齐 main #126):
  `services/amz_source.SNAPSHOT_FRESH_HOURS = 12` 与 `latest_seen()`——
  「12 小时内采过就不重推」,**快照不分结局**(not_found/blocked 也算采过,
  这条同时是失败重推的冷却期),新鲜与否在库端按 `now()` 算。eBay 一期若要
  为选中行补采,**必须调 `latest_seen()` 判、走既有补采通道**,严禁自定义
  新鲜期或另写一套判据——#126 消灭的正是"一边认为新鲜一边认为过旧"。
  一期默认**不自己推采集**,吃沃尔玛链当轮的刷新结果即可。
- 审核:入料谓词 `p.audit_status='approved'`(结论是沃尔玛政策口径,代价=
  品类连坐收窄;`risk_product_types` 内连接 + `walmart_pt<>'unknown'` 三重
  收窄**一期保留**作 fail-safe,P1-4 验收量化一次"去掉后候选数变化")。

## 三、P1-1 基建批次(约 7 人日)

### 3.1 api/_http.py 中立层抽取(动沃尔玛生产代码的唯一一批)

**照 r2 逐行搬家表执行(33 行:19 搬 / 14 留,其中 6 项在 `_client` 留
别名/薄壳)**;仅 `rate_acquire(bucket, key, buckets)` 与
`_is_persistent(bucket, buckets)` 改签名。要点与硬修正:

1. 搬家项含 `_rate_state` **与 `_rate_lock`**(被 `_acquire_mem` 使用,
   必须同去)、`backoff/BACKOFF_LADDER`(#92 上提,全项目退避唯一出处)、
   `SOCKS_ERRORS/_NET_ERRORS`、`_HAS_H2/_HTTP2`(env 名保留 `WALMART_HTTP2`
   头注说明)、`socket.setdefaulttimeout(90)`、连接池/transport 族、
   `download_bytes`、`_parse_retry_after`。⚠ `test_store_retry_standard`
   对 `backoff` **与 `BACKOFF_LADDER`** 用 `is` 断言——搬后必须同一对象。
   ⚠ `_parse_retry_after` 含沃尔玛独有的 `x-next-replenishment-time`,
   eBay REST 不带限流头,调它会恒回默认 60 伪装成"按头精确等"——头注写死
   「eBay 侧严禁把它当 429 退避来源」+ 守门测试(`api/ebay/**` 零调用)。
2. `_is_persistent` 判据 = **`(window >= 600.0 or limit <= 10) and
   limit <= 1000`**——蓝图的"≥600 且 ≤1000"会把沃尔玛 insights 16 个
   1/min 桶踢出跨进程共享。副作用知情:eBay 的 Taxonomy/Finances/Post-Order
   桶留进程内(各链每天单进程一跑,可接受,头注写明)。
3. 测试同批改**三类、共 10 个文件 18 处** `monkeypatch.setattr(_client, …)`:
   conftest:20(autouse 夹具打 `_acquire_pg`,搬家后**静默失效**、稀缺桶
   用例会连真 PG)、test_rate_bucket:17/:23、打 `_build_transport` 的
   test_catalog_sync/test_daily_report/test_feeds×2/test_items/
   test_order_lines/test_order_workflows×2/test_orders_async/
   test_walmart_client、test_store_retry_standard:49 打 `_get_client`。
   P1-1 验收"纯搬家全绿"写明这批例外。
4. `_invalidate_client(proxy)` 保持单参(蓝图双参签名查无此物)。

### 3.2 店级失败标准的跨平台接入(动沃尔玛侧 3 处)

- `api/_http.py` 立中立基类 **`IdentityDeadError`**;两侧 dead 异常继承它。
  沃尔玛侧改 **3 处**:`store_retry.fan_out:148`、`serial_second_pass:52`、
  **`diagnose:93`**(漏了它,eBay 账号在串行补试阶段暴露的凭证死会被归进
  「其他」档、进 absent 不进 dead,归类与分流同时错)。
- `store_retry.diagnose(err, vendor="沃尔玛")` 参数化,六档不加档;eBay api
  层报错逐字用同一文案形状(`返回 {status}`/`返回 None`)。守门:
  `test_store_retry_standard:247`
  (`test_classify_message_patterns_match_the_api_layer`)的
  `Path("api").glob("*.py")` **换 rglob**(现扫不到 api/ebay/),
  并加 `diagnose(err, vendor="eBay")` 六档逐档断言。
- 🔴 **vendor 词必须两处同源(2026-09-19 对齐 main #134)**:#134 把沃尔玛
  api 层的文案改成「**沃尔玛**返回 {status}」——vendor 词进了消息体,而
  `diagnose` 认的正则是 `返回 (\d{3})`,**vendor-blind**:归类词里的 vendor
  只来自那个新参数,消息体里的 vendor 只来自 api 层文案。两处各写各的 ⇒
  eBay 的报错正文说「eBay返回 404」、摘要归类词却说「沃尔玛404」,**两边都
  不报错**。定稿:api/ebay 文案写「eBay返回 {status}」+ 调用点显式传
  `vendor="eBay"`,守门测试把这对同源关系也钉住(rglob 出来的 api/ebay/
  文案与 `diagnose(..., vendor="eBay")` 的输出对拍)。
- ⚠ **守门只扫 workflows,不扫 services**:`test_store_retry_standard:612`
  的 `test_workflows_that_use_store_retry_import_it` 只 AST 扫
  `workflows/*.py`;#134 起 `services/store_limits` 也调 store_retry 了,
  这条守门扫不到它。eBay 若把店级失败标准用在 `services/ebay_*` 里,同样
  没有守门兜着"复制了分诊块却漏 import"那个事故形状——自己保证 import,
  或顺手把该测试的扫描面扩到 services(本批不强制)。
- 账号 dict 用 **`"name"` 键**(fan_out 只认它)。
- **一期不继承标准③④**(水位避让+链尾重赛:数据源与触发条件都写死沃尔玛)
  ——eBay 链不声明 SUPPORTS_STORE,随批次 5 回读链一起补。

### 3.3 registry 登记(先登记后引用)

- **`registry/platforms.py`**:`WALMART/EBAY/PLATFORMS` 常量集——本设计
  新起了跨六张表的平台键空间,必须有唯一出处;`claims._row` 与
  `upc_pool.claim` 对未登记平台抛 ValueError(照 `product_events.EVENTS`
  的 fail loud 先例)。正文与代码禁平台字面量(SQL DEFAULT 除外)。
- env 定名(值进 `<DATA_ROOT>/.env`,`init_data_root._ENV_TEMPLATE` 同步):
  `EBAY_CLIENT_ID/EBAY_CLIENT_SECRET/EBAY_RUNAME` + `_SANDBOX` 三变体、
  `EBAY_ENV`(sandbox|production,缺省 production)。
- `ebay_base_url(family, env)`:host 按族(sell REST=api / finances=apiz /
  auth=auth);**scope 的 host 恒为 `https://api.ebay.com/...`**(官方 curl
  实证),但官方明示 **sandbox 与 production 支持的 scope 集合可能不同** ⇒
  scope 常量做"单份默认 + 可按 env 覆写";P1-1 验收:两环境**各**用完整
  `EBAY_SCOPES_USER` 铸一次令牌,任一报 invalid_scope 即按环境拆分。
- scope 集:`EBAY_SCOPES_USER = sell.account + sell.inventory +
  sell.fulfillment`(consent 一次要齐——refresh 只能 ≤ consent 那组;
  fulfillment 为批次 6 预留)、`EBAY_SCOPES_TAXONOMY = api_scope +
  metadata.insights`(双 scope 是否强制进沙箱 C9)。
- **飞书两表登记**:eBay 账号凭证表(Bitable/Spreadsheet 条目 + 字段常量 +
  `.env` 变量名)与 eBay 上架结果投影表——先登记再引用,读写只走
  `sheet_values_rows/sheet_write_ranges/list_records` 标准通道
  (`test_feishu_guard` 对 api 用 rglob,已覆盖 api/ebay/),同步
  `docs/feishu_tables.md`。一期**不新增任何限额常量**。
- SKU 正则 **`^[A-Za-z0-9]{1,50}$`**(官方 Inventory 侧 25707 原文:
  "Invalid sku. sku has to be alphanumeric with upto 50 characters in
  length"——引这句,勿引 Trading 侧同义句)。
- 🔴 **SKU 定稿改为复用 `services/sku_codec.mint`(2026-09-07 推翻原稿的
  "SKU=ASIN 原文")**:main 已把沃尔玛侧全面切到 **12 位不透明码**
  (`<来源字母><11 位随机>`,如 `AK7QM2X9RT4W`;批次 0a~3 全部实现、
  `list_new._prep_rows` 抽码即登记),`catalog.listing_sources` 因此成为
  **SKU 身份的唯一登记簿**(新增 `abandoned_at/abandoned_reason/replaced_by/
  replaces/replaced_at` 五列,活码 = `abandoned_at IS NULL`)。四条理由:
  ① 两平台共用同一张登记簿(PK `(store, sku)`),eBay 另走 ASIN 原文就是
  **同一张表两套码制 = 双轨**,直接违反"每个能力只有一条实现路径";
  ② 12 位码是纯字母数字,**天然满足上面那条 25707 白名单**,原稿担心的
  分隔符问题一并消失;③ 发码键是 `(store, source_type, source_key)`,
  `store` 传 eBay 账号名即天然按账号隔离(账号名互斥硬约束 §2.1 已保证
  键空间不撞);④ 无货源模式下 **ASIN 做 SKU 等于把亚马逊货源写在明面上**
  给 eBay 与竞品反查。
  用法:`sku_codec.mint(conn, account, source_type='amz', source_key=asin,
  workflow='ebay_list_new')`——⚠ **来源字母是货源类型不是销售平台**
  (`SKU_SOURCE_LETTERS = {amz:A, match:B, 1688:C, self:H}`),eBay 搬运品
  的货源仍是 amz、字母仍是 A,**不许为 eBay 新增字母**;空跑用
  `DRYRUN_PLACEHOLDER`,不写 dry-run 分支。
- **一期不弃码**:`sku_codec.abandon` 的四个弃码点(DELETE 经观测核验 /
  SKU_LOCKED 退役 + 冷却 / UPC 撞库 / 改码)全是沃尔玛语义,而一期只到上架、
  没有删除与自愈链。二期 eBay 若要弃码,新原因**必须进 `ABANDON_REASONS`**
  (那是唯一出处),不许在 eBay 侧另立一套。⚠ 与 §2.2 的交集:`ABANDON_
  UPC_CONFLICT` 与 UPC 池的 `mark_conflict` 是同一件事的两侧,eBay 侧撞库
  一期只置池位、不弃码(码还活着,下轮换号重上)。
- 错误码常量(语义见 reference §1):25707/25729/25713/25702/25710/25025/
  25002(多义禁单判)/25014/25015/25501/25086。⚠ **190204 是 Trading 侧码,
  Inventory 侧图片错是 2501x/25501**。
- `EBAY_BULK_MAX = 25`、`EBAY_LISTING_REVISE_PER_DAY = 250`(官方:每
  listing 每自然日修订上限,卖家级,API 桶挡不住)。
- 桶登记 `api/ebay/_client._RATE_BUCKETS`,键一律 `ebay.` 前缀(两平台共写
  `ops.rate_events`,撞名互扣配额;守门断言前缀)。

### 3.4 services/ebay_accounts.py + api/ebay/_client.py + 令牌

- 三层判据仿 stores.py:`registered_names()`/`enabled_names()`(`is_enabled`
  照抄含 bool 分支)/`load_accounts()`(能调 API=库里有未过期 refresh_token,
  跨层读 `ops.ebay_tokens`;凭证飞书表只放 启用+代理三件套+marketplace)。
  快照 `paths.ebay_accounts_snapshot_file`(新函数,不复用 stores 文件)。
  `EBAY_ACCOUNT_WORKERS = 4` 起步。
- `ops.ebay_tokens`:PK **`(account, env, token_kind, scopes)`**。刷新响应
  **没有 refresh_token 键** ⇒ UPDATE 的 SET 列白名单**不许出现
  refresh_token**。泄漏三孔同批堵:backup `--exclude-table-data=
  ops.ebay_tokens`;**REVOKE readonly 写进 schema.sql 但必须包在
  `pg_roles` 角色存在守卫里**(单层 DO $$——覆盖"以前建过 readonly 这次
  没设口令"的机器:`db_init._READONLY_SQL` 只在 `READONLY_DB_PASSWORD`
  有值时执行,而 ALTER DEFAULT PRIVILEGES 是持久的,新表会自动带上 readonly
  SELECT,只堵 db_init 一头堵不住);守门测试断言两条路径都覆盖;
  db_schema.md 标注。
- `workflows/ebay_authorize.py`(DANGEROUS=True)+ `docs/ebay_runbook.md`:
  拼 consent URL(七参数,**带 `state=<account 派生值>`**;redirect_uri 填
  RuName 字符串)→ 人工浏览器同意 → 从 Auth Accepted 地址栏**把 code 与
  state 一起拷**(页面 404 也拿得到;5 分钟内粘回,官方示例 299s)→
  `-p code=` 必须配 `-p state=`,与 account 不符**抛多行 RuntimeError**
  (多账号连着授权时把 A 的 code 粘进 B 的命令行是静默落错账号)→
  `exchange_code`(🔴 恰好 unquote 一次)→ 落库 → 打印 refresh_expires_at。
- `workflows/ebay_account_health.py`:每日**真发一次 refresh grant 探活**
  (invalid_grant=已吊销——卖家可在 My eBay 撤授权)+ **重采
  `getPrivileges` 刷新 `catalog.ebay_accounts.privileges_json/sampled_at`**
  (§6.2 的 selling limit 闸靠它);已吊销/<7 天抛停链,<30 天首行预警。
- 🔴 **"200 但解析不出" 一律抛错、且不进缓存(2026-09-19 对齐 main #134)**:
  #134 给 `api/settings._cached_ship_nodes` 定的形状——响应 200 但一个条目
  都没解析出来(空体 / 非 JSON / 形状变了)时**抛 RuntimeError,不返回空**。
  两条理由逐字适用于 eBay:① 返回空会被调用方判成"这东西不存在"(配置错),
  **瞬时故障伪装成配置错**;② 空结果一旦进 `lru_cache`,**整个进程再也不
  重打这个接口**,一次抖动毒死一整轮。eBay 侧同形的三处照此办:令牌缓存
  (`api/ebay/_client`)、`getPrivileges` 采样(§6.2 的 selling limit 闸靠它,
  空 privileges 会被读成"配额为 0"或"无限制",两种都危险)、
  `ebay_taxonomy_sync` 的类目树。**非 200 才是"接口回了什么"**,200 空体是
  "我们没读懂",两者的归类词不能撞在一起。
- 🔴 **"读不到" ≠ "不认识"(同批,#134 的另一半)**:#134 把
  `NodeConfigError` 拆成 `NodeUnknownError`(沃尔玛**明确否定**:这个节点
  不在你的列表里)/ `NodeUnreachableError`(**没读到**:超时、代理波动、
  200 空体),只有前者算配置错,后者走 `store_retry.serial_second_pass` 补试
  并沿用旧记忆。对位到 eBay 一期有两处要照此分开:**§6.1 入料的"类目映射
  approved+高 fail-closed 拦下"**——"库里确实没有 approved 映射"与"这一轮
  没读到映射表"拦下动作相同,但**归类与计数必须分开**,否则一次读故障会在
  摘要里伪装成"这批品没有类目映射",查都没处查;**§3.4 账号/令牌读取**同理
  (refresh_token 读不到 ≠ 账号未授权,后者才该停链)。
- 摘要与参数标准件:`-p` 布尔一律 `from services.params import flag`
  (⚠ 按名导入,模块名会被 run(params) 形参遮住);摘要一律
  `notify_fmt.head/summary`。八条新 workflow 统一,不许各自手搓。

## 四、P1-2 库批次(约 7 人日)

### 4.1 DDL 清单(照 §4.5 迁移纪律)

| 对象 | 动作 |
|---|---|
| `catalog.claims` | + `platform`;索引换名重建(§2.1) |
| `catalog.upc_usage` | 新表(§2.2)+ 存量回填 + 三条索引 |
| `ops.feed_log` | + `platform`;`feed_log_dedupe_uidx` → `feed_log_dedupe_v2_uidx (platform, feed_type, store, payload_key)` 原地替换。⚠ **#131 给本表加了 5 列,eBay 行必须一起填**:`item_count` / `skus`(对账收编的精确匹配依据 —— eBay 三步链每步的 SKU 集合)、**`post_started_at`**(请求真正发出前落;为空 = 确定没发出,是 §6.5 里唯一能证明「可安全重发」的格子)、`recon_count`、`close_basis`。索引 `feed_log_feed_id_idx` 已存在,eBay 行直接受益 |
| `ops.feed_items` / `feed_item_errors` | + `platform` 标注列;⚠ **#131 起 status 七档**(+ overdue/unrecognized/unreadable,见 §6.5)且新增 `raw_status` / `settled_by` 两列 —— eBay 落定同样要留事实依据,别只写一个折算过的结论。主键不动——**该论证只覆盖 offer/publish 两阶段**:eBay 台账 `feed_items` 只落这两阶段的行,`feed_id` 分别存 offerId/listingId,`(feed_id, sku)` 不撞;**ebay_item 阶段不落 feed_items**,由 `feed_log` pending/submitted 承接——PUT 幂等可重放、204 无 id 可记,硬造 feed_id=sku 会在换账号重上时撞主键静默丢行) |
| `catalog.product_events` | + `platform` + 5 视图 DROP 重建(`audit_listing_conflicts` 的 EXPLAIN 必须仍走 `product_events_identity_idx`);**按事件码过滤的非视图消费方(blacklist/_LATEST_CTE、problem_scan 三条、sku_normalize、audit_history_fold、cleanup_history_import、dispositions._SETTLE_DELETE_SQL)一期不改,依据=eBay 事件码与沃尔玛零重合(逐条列名进实现注释);二期给沃尔玛加任何同名事件码前必须先补谓词**。🔴 **例外:`services/risk_trace` 不按事件码过滤,必须补平台谓词——见 §4.4** |
| `catalog.listing_sources` | + `platform` 标注列(**不进 PK**:`sku_codec.mint` 发的 12 位随机码全局唯一,`(store, sku)` 天然不撞);schema.sql 存量回填 INSERT 显式补 walmart。⚠ 两条已被 main 推翻的原稿表述:① "三处消费方都锚在 walmart_items 上,JOIN 即天然谓词"被 #99 推翻(`risk_trace` 按 `source_key` 反查、不 JOIN 任何平台表——见 §4.4);② 本表在 2026-09-07 后**已是 SKU 身份的唯一登记簿**(+`abandoned_at/abandoned_reason/replaced_by/replaces/replaced_at` 五列),eBay 行由 `mint` 在抽码同一事务里登记(§3.3),**不许另建 eBay 专用登记表** |
| `catalog.ebay_items` | 新表(一期空表;状态列 **text 不加 CHECK**——沙箱 C3 卡的是 submit_poll 的 withdraw 后判据与状态字面量首次落 SQL,**不卡建表**) |
| `catalog.ebay_accounts` | 新表:account, marketplace_id, 三 policyId, merchant_location_key, opted_in_at, privileges_json, sampled_at,PK (account, marketplace_id) |
| `ops.ebay_tokens` | 新表(§3.4,含 REVOKE 守卫) |
| `audit.ebay_categories` | 新表(严格树单表;`is_leaf` 硬闸列) |
| `audit.ebay_category_tree_versions` | 新表 |
| `audit.ebay_aspects_cache` | 新表(🔴 `constraint_raw/values_raw` jsonb 原样列必留) |
| `audit.ebay_category_map` + `_suggestions` | 新表(§5.3;`candidates_raw/agreement` 不许省) |
| 不动 | `ops.dispositions`(一期零写入方)、orders 三表(v2 守卫地雷)、products/snapshots(仅 marketplace 语义两处纯注释) |

### 4.2 台账消费方谓词补全(P1-2 同批,漏一条就互相污染)

`api/feeds` 与 `services/feed_track` 的读写面逐条加平台维度:
`_log_claim`(ON CONFLICT 改四元组新索引)/`_log_update`/`mark_feed_done`/
**`query_pending`**(现无平台谓词——不改的话沃尔玛高频 `feed_poll` 会捞走
eBay 的 pending 行:进沃尔玛摘要"待人工核对"永不老化,submitted 行拿
offerId 去店铺凭证表找不到账号)/`find_recent_feed`;`feed_track` 每个读
函数 platform **必填漏传即抛**(不许静默落 walmart)。eBay 反哺器挂在
`ebay_submit_poll` 自己的登记结构上(不进沃尔玛 `feed_poll._REFLECTOR_
CHAINS`),同表串行纪律照抄。

### 4.3 product_events 契约

`record_many` 加显式 `asin` 入参,eBay 行由调用方给(理由写死:**平台身份
键由调用方显式给出,不靠 SKU 形态猜**——`extract_asin` 是沃尔玛订货号形态
专属规则;**SKU 改走不透明码后 `extract_asin` 对 eBay SKU 一律提不出、
只会误命中或返回 NULL**,原稿"一期 SKU=ASIN 时碰巧同值"的说法随 §3.3
改稿一并作废)。eBay 事件码同批进 `EVENTS` 与 `_FEED_KIND`。

### 4.4 🔴 风险追溯四证据源的平台谓词(P1-2 同批,blocker 级)

**2026-09-03 合并 main #99 后新增,原稿完全没有这一节。** `services/risk_trace`
是店铺事件账本(`ops.store_events`)的波及展开引擎,四证据源里**三个直接读
共享表且既不带平台谓词、也不 JOIN 任何平台专属表**——`claims_key_all_idx`
与 `listing_sources_key_idx` 这两条新索引就是为它建的:

| 证据源 | SQL 形态 | eBay 行进表后的后果 |
|---|---|---|
| ① `catalog.walmart_items` | 按 sku 查 | 平台专属表,**天然安全** |
| ② `catalog.listing_sources` | `WHERE source_type='amz' AND source_key = ANY(...)` | eBay 的登记行被算成沃尔玛店的波及范围 |
| ③ `catalog.product_events` | `WHERE coalesce(asin,sku) = ANY(...) AND store IS NOT NULL` — **不按事件码过滤** | §4.1 的"eBay 事件码零重合"论证对它**不成立**,eBay 事件行全被捞进来 |
| ④ `catalog.claims` | `WHERE kind=? AND claim_key = ANY(...)`,**含 released 行** | eBay 占用/历史归属混进沃尔玛追溯 |

消费方是**生产在跑的风控链**:`workflows/order_audit`(钓鱼单按品牌展开)、
`workflows/product_audit`(TRO 波及逐店)、`services/store_events`(exposure
行)。后果形状:波及展开报出一家**不存在的沃尔玛店**(store 列装的是 eBay
账号名),运营照着它去停店/清货,而**两个方向都不报错**——正是 CLAUDE.md
点名的那类事故。

**定稿**:②③④ 三条 SQL 在 P1-2 同批补 `platform = 'walmart'` 谓词(常量引
`registry.platforms`);`stores_of_brand` 等入口签名加 `platform` **必填关键字
参数不给默认值**,逼 order_audit / product_audit / store_events 三个调用点
显式声明意图。⚠ 加谓词后 ④ 的 `claims_key_all_idx (kind, claim_key)` 仍可用
(前导列匹配、platform 回表过滤),**不要**顺手把它改成带 platform 的三列索引
——它的存在理由是读 released 行做历史归属,与 active 唯一索引是两回事。
验收:P1-2 加一格「造一条 eBay claims + listing_sources + product_events 行,
`risk_trace` 展开结果必须一行不含该 eBay 账号」。

### 4.5 迁移块纪律(订正 ebay_plan §3.6)

schema.sql 现有**四处** `DO $$`(:66/:579/:608/:1300),**:579-589 是嵌套 IF
范例**,注释逐字:"平铺 AND 会在计划期解析表名,重跑必炸 UndefinedTable
(2026-08-13 生产实证)"。判据:守卫只查 information_schema/pg_indexes/
to_regclass/pg_roles ⇒ 单层够;守卫内层要引用可能不存在的对象 ⇒ 嵌套 IF。
另三条(整份 schema.sql 单事务):一句报错整份回滚;禁 CONCURRENTLY;
REVOKE 按 §3.4 带角色守卫。验收:db_init 幂等两跑 + r3 六格专项对拍
(upc_usage 行数/池位分布逐值/回填无漏/claims 新索引在旧索引亡/feed dedupe
只剩新名/order_lines 行数不变)+ **既有读 SQL 逐条 count+md5 对拍** +
飞书 UPC 表投影逐行对拍(§2.2)。⚠ `db_init --dry-run` 是真跑——人眼确认
只能靠 git diff + 影子库;顺手补 dry_run 与守门测试加强。

## 五、P1-3 户口与类目批次(约 6 人日)

### 5.1 workflows/ebay_bootstrap_account.py(可重入)

① `getOptedInPrograms` 回读 → 未 opt-in 才 POST(`SELLING_POLICY_MANAGEMENT`;
sandbox 同样需要;最长 24h 生效——未生效摘要点名、正常返回不抛);
② 三政策**先按 name 回读再建**(POST 无幂等键);最小请求体见 reference §3
(handlingTime **顶层**,官方两页矛盾抄进头注;payment 不带 paymentMethods,
400 再调);③ location:key 自己生成、**先落 `catalog.ebay_accounts` 后调
接口**(204 无 body),GET 对账;④ `getPrivileges` 落 privileges_json:
`sellerRegistrationCompleted=false` 整账号跳过;sellingLimit 容器可能整个
缺失——fail-closed 报告"字段缺失/采样过期/调用失败"**三态分开报**。

### 5.2 workflows/ebay_taxonomy_sync.py

应用令牌。每日版本哨兵(treeVersion 变了才拉整树拆行入库 + 原始 gzip 按
版本归档 `<DATA_ROOT>`,归档零消费、明禁读回);**aspects 拉取判据与树
哨兵解耦**:`映射表 approved ∧ 缓存无该 (marketplace_id, category_id,
tree_version) 行 ⇒ 拉`(promote 平日随时发生,挂在哨兵上会让新放行类目在
下个树版本前拿不到 aspects、被 ebay_conform 全拦且看起来像映射错了),
版本变更日叠加全量复拉;🔴 `fetchItemAspects` 显式禁用(gzip 二进制可超
100MB,spec 还自相矛盾声明 json)。版本变更时映射表体检(active 行存在性+
is_leaf,不过标 stale 摘要首行点名;`getExpiredCategories` 登记不实现)。

### 5.3 类目映射测试链(拍板 ⑨;同构沃尔玛 catmap_suggest→promote)

- `workflows/ebay_catmap_suggest.py`:按 **amazon_node_id** 出建议(一 node
  一次 LLM,**禁止逐 ASIN/SKU 调**——唯一能打爆 taxonomy 桶的方式,头注
  写死;`-p limit` 硬上限);召回=`getCategorySuggestions`(**只在
  production 跑**,env=sandbox 直接抛错——sandbox 返回样板文本假成功;
  q 传商品标题;`relevancy` 官方 "Reserved for internal use" 禁当置信度,
  顺序才是信号);LLM=排序器(走 `api/llm`+`llm_cache`,**不写死模型名**
  ——模型缺省值与 thinking 开关的唯一出处是 `registry.LLM_THINKING` +
  `resources.llm_thinking(model)`(2026-09-10 起,模型名以 `GET /models`
  为准);eBay 侧若要指定模型,**先在那张表补一行登记**,未登记会告警且
  thinking 形状不可控;输出
  ebay_category_id/confidence 高中低/reason);落库两道机器闸:∈ 本次候选集
  + JOIN `audit.ebay_categories AND is_leaf`。🔴 LLM 只写 suggested 永不
  自写 approved(照 `products.pt_source` 洗白教训)。
- `workflows/ebay_catmap_promote.py`(DANGEROUS=True):**参数面完整定稿
  ——`-p nodes=<逐条点名>`(必填)+ `-p as=高|中|低`(缺省中);`as=高`
  必须同时给 nodes,禁止批量置高**。这是把行提到"高"的**唯一入口**——
  没有它,map 表永远没有 confidence='高' 的行,入料恒 0 候选且不报错
  (CLAUDE.md 点名的事故形状)。ops.runs 留痕,一期不建飞书表。
- **入料只吃 `status='approved' AND confidence='高'`**;promote 缺省写中的
  实证照引(沃尔玛首批 50 条里 Building Sets→Advent Calendars 被判"高")。
  部分唯一索引 `(amazon_node_id, marketplace_id) WHERE approved`;
  `category_tree_version` 列。验收三格:缺省中、入料只吃高、**置高路径可用
  且需逐条点名**。
- 接线口:`m.amazon_node_id = p.browse_node_id` 单条 JOIN;
  **P1-2 开工第一步连库 count 一次 browse_node_id 空行占比**(fail-closed
  全拦的实际代价),写进验收。

## 六、P1-4 上架闭环批次(约 15 人日,含中立抽取 3 人日)

### 6.0 中立抽取(ebay_plan 批次 4a 的承接,显式认领防排期失真)

`services/listing_copy.py`(从 mp_mapper 抽 scrub_brand/_clean_copy/
sort_images/_sentences/title_spec_compatible)、`pricing.landed_price/
parse_multiplier` 转中立 + `pick_band` 改收 bands、变体三件改名。
验收=沃尔玛链 pytest 全绿 + list_new/maintenance_scan 输出逐字不变。

### 6.1 驱动源与入料

沃尔玛驱动表是飞书上架表;**eBay 一期驱动源=库侧选品**,飞书 eBay 上架表
只是结果投影(防照抄 read_rows)。取数:**`product_pool.load()+score_all()`
之后在 Python 侧按分数排序取前 cap 条**(cap = min(selling limit 余量,
日上架条数闸) × 放大系数)——分数不在库里,"SQL 里 ORDER BY 分数"不可
实现;🔴 **`_SQL_POOL` 本体一个字不动**(它是分配链两件套逐字同源的存在
理由),eBay 侧谓词收窄走 `services/ebay_admission` 自己的过滤,不就地改
共享 SQL。
🔴 **分数只管排序,一期不设淘汰线(2026-09-23 对齐 main #135 所有者定稿)**:
#135 给点名分配定的理由**逐字适用于 eBay 一期**——那条 40 分线量的是
"证据多不多",而证据的两个来源(评论数、**我们自己店里**的销量)在这里
天然缺席:eBay 候选**从没在 eBay 上过架**,`score_all` 的销量维度又只吃
沃尔玛店的销售,所以"4.8 分零评论也只有 36 分"那个形状在 eBay 侧是**全池
普遍状态**,不是个别差品。照搬 `product_score.CUTOFF` = 整池被一票否决。
定稿:eBay 入料**不设 cutoff**,分数只决定 `ORDER BY`(前 cap 条),
拦品全靠上面那串硬闸与逐行闸。⚠ 两条附带后果写明白:① 排序天然偏向
"沃尔玛已验证"的品(它们的销量维度非零)——这是有意的(先上验证过的),
**但不许把它升级成闸**;② 若二期真要加线,得先有 eBay 自己的销量回流,
拿沃尔玛的证据去卡 eBay 的新品就是 #135 定稿前那个坑。
入料谓词:audit approved + 类目映射 approved+高(fail-closed
拦下计数)+ claims 平台内未占 + **去重谓词写死
`feed_items.feed_type IN ('ebay_offer','ebay_publish') AND status IN
('submitted','success')`**(按"存在任意行"判会把 item 步成功 publish 步
失败的 SKU 永久排除)+ upc_usage 活跃用量去重。🔴 **这条谓词两处不够,都在
§6.5 第三条展开**:① 必须并上 `feed_log` 里该 (account, sku) **未收口**的行
(丢响应的提交没有 `feed_items` 行,谓词看不见它,下一轮就重发);② 白名单
写法在 status 扩档后必漏(#131 起多了 overdue/unrecognized/unreadable 三档),
**改成排除法**——只有拿得出"确定没发出"证据(`post_started_at IS NULL`)的
才放行重上。**重试闸**:按 (account,
sku) 计次上限 3(对位沃尔玛 `MAX_LIST_ATTEMPTS=3`;没有它,永久失败的
SKU 每天白烧配额与 LLM),4xx 终态拒的错误码集不进重试通道。
逐行闸:brand_key 黑名单/库存三态/渠道/运费/落地价/lead/定制品闸
`is_customized`/图片全 https(services 层预校验)/🔴 **二手翻新闸**。
🔴 **二手/翻新必须单独一条闸(2026-09-14 对齐 main #132 补)**:判据**只准**
调 `services/maintenance_intents.used_offer()`(源侧 buybox 品相,数据在
`snapshots.raw -> AMZ_OFFER_CONDITION_KEY`,`_NOT_USED_CONDITIONS` 反着写、
**未知不当二手**——与 `is_fba` 未知方向一致,把未知当二手会整批误删),严禁
自写判据。两条理由:① §6.4 定的是 **`condition` 一期固定 `NEW`**,源是二手
却按新品上架 = 虚假描述,比无货源本身更硬的违规,真发货时买家收到二手品直接
打到账号健康;② **库存三态闸挡不住它**——#132 逐字点名"二手 offer 通常是
**有货**的,不剔的话每条观测都算 sellable",这是该维度最容易漏的一处。
⚠ **两条与 main #85/#102 的边界(2026-09-03 补)**:① `list_new` 在 #85 后按
**受管仓节点**切 shipNode、`catalog.walmart_items` 有 `node_count`、库存读
`avail_qty` 全节点合计——eBay 一期是**单 `merchantLocationKey`**,不做多节点,
对位的是"一个仓位常量",不许照抄多仓分配那段(二期若上多仓需另设计);
② 沃尔玛报错归类引擎(`services/error_taxonomy` + `refdata/policy_pages/`
42 类中英语料)是**沃尔玛政策页专属**,eBay 错误码走 `registry` 常量 +
`docs/ebay_phase1_reference.md` §1 的官方码表,**一期不接入 error_taxonomy**
(语料与判据不同源,接进去就是双轨);二期若要 eBay 报错归类,另立语料。
⚠ **三条与 main #133 的接口对齐(2026-09-17 补)**:① `product_pool.load`
签名变成 `load(conn, win, asins=None)`——点名白名单**筛在 SQL 里**
(`_POOL_ASIN_FILTER` 追加在 WHERE 末尾,`asins=None` 时 SQL 逐字不变)。
eBay 一期走缺省路径,但**二期若要点名入料,用这个参数,别拉全库回来再
过滤**(全库 LATERAL 取最近快照是几十万行的成本),更别为此改 `_SQL_POOL`
本体——那正是上面"一个字不动"那条。② **渠道归一的唯一出生地现在是
`product_pool.norm_channel(fulfillment)`**(FBA/FBM,认不出归 None、
**不猜**):eBay 侧的渠道闸与 eBay 上架表的「配送方式」列**只准调它**,
自己再写一遍 `upper()+CHANNELS` 白名单就是第二条实现路径(#133 拆出它
的理由逐字就是"两处各写一遍迟早分叉")。③ `score_all(data,
gated_by_asin=None)` 现在能回收**逐 ASIN 的完整淘汰原因**:eBay 入料摘要
与飞书投影要写「未上架原因」时直接传这个收集器,不要再拼一份自己的原因串
(计数那份只留括号前的归类名,原因全文在收集器里)。
⚠ **按表头认列的算法已搬出来共用(main #133)**:`services/sheet_layout.py`
是全仓唯一的"按表头名算列字母"实现(从 `listing_sheet` 里搬出),飞书
**eBay 上架表投影**(§3.3 登记的两表之一)**只准用它**算列区间,严禁写死
列字母;并且新表的文件名要登记进 `tests/test_sku_guard.py`
的 `_HEADER_LAYOUT_FILES`——**不登记守门测试就扫不到这个文件**,写死列
字母能一路绿灯合进来(表头一改名列就错位,静默写错列)。

### 6.2 双闸配额与熔断

- **selling limit 闸 fail-closed,一期口径写死**:上限取
  `catalog.ebay_accounts.privileges_json`(由 `ebay_account_health` 每日
  重采,`sampled_at` 过期视同读不到);已用量按 `ops.feed_items
  (platform=ebay, feed_type='ebay_publish', status='success')` 本地计数;
  GetMyeBaySelling 精确余量明确列二期(一期无 Trading 通道)。三态分报
  (§5.1④)。**日上架条数闸的数值列入待所有者实值**(刊登费真实成本:
  免费额度 250 条/月/账号,超出 $0.35/条)。
- 账号级熔断(账号级失败不逐条重试)、零账号完成即判失败、25025 同账号同
  SKU 三步串行(一期单链天然满足,头注写明;二期维护链前按 advisory 锁
  串行化)。lead/channel 闸保持 fail-open(非平台硬限,与沃尔玛上架侧同)。

### 6.3 定价

落地价(运费 NULL 不定价)× 区间倍率(一期沿用沃尔玛限额表列,eBay 专属列
待所有者配置);**出界不上架**(不沿用 300% 兜底,理由头注写明)。

### 6.4 三步链字段要点(全量字段表与错误码表:`docs/ebay_phase1_reference.md` §1)

- **PUT /inventory_item/{sku}**:`condition` 一期固定 `NEW`——⚠ 因此入料
  必须有 §6.1 的二手/翻新闸兜住,否则源是二手也会按新品发出去;
  SKU = `sku_codec.mint` 发的 12 位不透明码
  (§3.3);header `Content-Language: en-US`
  (body 里 locale 是 `en_US` 下划线——两形态别混);aspects=
  `dict[str, list[str]]`;product.description ≤4000 只放短摘要;图片全
  https、≥1 ≤24;成功码 200/201/204 且 **204+空体=正常成功**(`if not
  data: fail` 会把干净成功判失败);PUT 幂等可重放。🔴 **GTIN 触发的
  catalog 自动填充在 ① 步就会发生**(item 的 description/图片等会被 eBay
  catalog 按 GTIN 自动填充——这与 offer 级开关无关,而我们每条都带 UPC)
  ⇒ 明确 UPC 在 ① 步载荷的落点字段,并进沙箱清单 #0(带 UPC PUT 后立即
  GET 回读,比对 title/description/aspects/imageUrls 是否被改写——它决定
  ebay_conform 要不要做"回读校验"层)。
- **POST /offer**:唯一性 (sku, marketplaceId, format)——重复 createOffer
  会被拒不会建出第二个;`pricingSummary.price.value` 是**字符串**;长文案写
  `offer.listingDescription`(≤500,000 含 HTML)不是 product.description;
  categoryId=映射 approved 行;三政策 id + merchantLocationKey 出自
  `catalog.ebay_accounts`;`includeCatalogProductDetails` 显式传 false
  (**分层表述:offer 级 false 只挡 listing 侧套用;item 侧 GTIN 自动填充
  与本开关无关**,防线在 ① 步回读);缺 merchantLocationKey createOffer
  静默过、publish 必失败(官方直述)⇒ 必填校验放 publish 前 services 层。
- **POST /offer/{offerId}/publish**:必填四组(product.aspects 官方点名
  必填 ⇒ `ebay_conform` 是关键路径;只读 aspectRequired;
  `aspectApplicableTo=PRODUCT` 的卖家改不了硬塞被静默忽略;
  `valueConstraints` 非空一期 fail-closed);响应 listingId;errors 结构
  errorId/domain/category/message/parameters。
- **bulk 207 Multi-Status**:逐条读 `responses[].statusCode`;失败行只按
  原方法补交一次。一期**先走单条端点**,bulk 留签名(保序性沙箱 C5)。

### 6.5 提交台账与 submit_poll

- 三态时机:`feed_log` 每步 POST/PUT **前**落 pending(feed_id NULL);
  `feed_items` 仅 offer/publish 两阶段 2xx **后**落(feed_id 分别存
  offerId/listingId;ebay_item 阶段不落,§4.1)。延后结算:载荷 builder
  确定性重建。
- `ebay_submit_poll` 反查:ebay_item→`GET /inventory_item/{sku}` 200=FOUND;
  ebay_offer→`GET /offer?sku=` **并按唯一性三元组 (sku, marketplaceId,
  format) 过滤后**判 0/1(官方允许同 SKU 在第二站点/第二 format 下合法
  ≥2 条;一期单站点单 format 是前提不是判据——marketplace_id/format 两个
  query 参数是否存在并入沙箱 #8,不存在则在返回数组里自行筛),同三元组下
  ≥2=不许自动选、落 missing+账号级告警;ebay_publish→`GET /offer/{offerId}`,
  FOUND=**`status=='PUBLISHED'`**(封闭集 {PUBLISHED, UNPUBLISHED} 已证;
  listingId 只作辅证——withdraw 后残留形态待沙箱 C3)。NOT_FOUND 同方法
  补交一次;双确认 5~10s(工程值)。
- 🔴 **选行面取并集,别只靠下游信号(2026-09-24 对齐 main #136)**:#136 的
  生产事故形状——复查 SQL 只选「还有 pending 子行」的批次,而**另一条路径**
  (结算)把子行全标 done 之后,**台账自己仍在途的那一行再没人看**,永远停
  在 running;下游门禁只放行"不在途"的批次,现成的产物永不发出(3 天窗口
  252 行卡住 12 行)。对位到 `ebay_submit_poll`:选行 SQL 必须是**并集**
  ——「`feed_log` 自己 `status='pending'`」∪「`feed_items` 还有未落定的行」,
  **不许只靠其中一个信号**。两个信号由不同代码路径维护,总有一天对不上。
- 🔴 **收口期限按官方值,不加余量、不猜测(2026-09-25 对齐 main #131 所有者
  定稿)**:昨天这里写的自造常量 `_INFLIGHT_STALE_HOURS = 24` **作废**——main
  已经把这件事做成一套完整口径,照搬它而不是自己拍脑袋。沃尔玛侧现状:
  期限表 `refdata/walmart_slas.tsv`(头注逐字写「期限按官方值、不加余量,
  请实际查看官方给的期限值,不猜测,不凭记忆回答」,查无的写「官方未给」、
  **不许推断**)→ 常量 `services/feed_track.FEED_DEADLINE_MINUTES`(**按
  feedType 分档**,不是一个全局数)+ `UNREADABLE_GRACE_HOURS = 24`(只管
  "到期后读不到明细"的宽限)。eBay 对位:**另起 `refdata/ebay_slas.tsv`**
  (与 `ebay_rate_limits.tsv` 分开——一个是配额、一个是期限,混在一张表里
  下次谁都不敢改),三步链各自一档,官方没给就写「官方未给」并在设计里
  说明按什么兜底,**不许拿沃尔玛的分钟数顶上**。
- 🔴 **"平台没给结论" 是独立的一档,不是失败(#131 新增,照搬语义)**:
  `feed_items.status` 现在是 submitted / success / failed / missing +
  **overdue / unrecognized / unreadable**,后三档由
  `feed_track.NO_VERDICT_STATUSES` 点名,库里逐字写着"**沃尔玛没给结论,
  不是失败**"。eBay 侧必须照搬这个分档:到期仍 INPROGRESS、状态值不认识、
  到期后读不到,三种都**不许折成 failed**——折了就等于替平台下结论,而下游
  是按 failed 决定"可以重发"的。落定还要留下事实依据:`raw_status`(平台
  原始状态原值)+ `settled_by`(head / deadline / unreadable),且
  **首次落定即定稿,之后不改**。
- 🔴 **pending 对账三条纪律(`feed_track.reconcile_pending`,所有者 2026-09-25
  批)**:① **只读反查**,查到就收编;② 收编的门槛是**明细 SKU 集合与台账
  `skus` 列完全一致**(不是"看着像"——`item_count` 先精确匹配条数);
  ③ 到期还查不到,落 `failed` 并把依据写进 `close_basis`,**不自动补交**。
  ⚠ 这里有一格是判"能不能安全重发"的唯一硬证据:**`post_started_at IS NULL`
  = 请求确定没发出**,只有这一种情形可以直接重发;其余一律人判。eBay 侧
  三步链每步都要有这一格(尤其 `publishOffer`——它不幂等)。
  ⚠ eBay 比沃尔玛还要再保守一档:#136 里收口放行的是取图上传(只读产物),
  eBay 这边"放行"意味着 SKU 重进上架候选,那是**写操作自动兜底**,安全红线
  明令禁止。收口只做三件事:停轮询、摘要**首行**点名、留痕 `product_events`。
- 🔴 **"还在途吗"只有一个出处**:沃尔玛侧是 `feed_track.IN_FLIGHT_SQL`
  (按 feed_id 反查 `feed_log` 是否已收口,`problem_scan`/`sku_migrate` 每轮
  对成千上万行做 EXISTS,为它加了 `feed_log_feed_id_idx`)。eBay 侧同样要有
  **一条**在途谓词常量,所有消费方引用它;各处自己写 EXISTS = 双轨,迟早
  两处对不上(#136 就是两个信号对不上造成的)。
- 🔴 **去重谓词必须自己覆盖"在途",不许靠调度顺序**:§6.1 的去重谓词只看
  `feed_items` 的 submitted/success,而**丢响应的那次提交根本没有
  `feed_items` 行**(§6.5 第一条:offer/publish 两阶段 2xx **后**才落),
  只在 `feed_log` 留一条 pending。所以 `submit_poll` 一旦漏查、或到期收口成
  `failed`(提交未确认),`list_new` 下一轮就把同一个 SKU 再发一遍。#136 是靠"同一轮 ②对账
  在 ④推送之前"兜住的,但铁律写死**调度顺序不许承载判据**——eBay 这边要把
  去重谓词**并上 `feed_log` 该 (account, sku) 未收口的行**,谓词自己站得住。
  🔴 **而且要改成排除法写(2026-09-25 对齐 #131)**:白名单式的
  `status IN ('submitted','success')` 在 status 扩到七档之后**必然漏**——
  overdue / unrecognized / unreadable 三档一个都不在白名单里,于是"平台还没
  给结论"的 SKU 会被当成"没提交过"重发一遍。定稿:**只有能拿出"确定没发出"
  证据的行才放它重上**(`post_started_at IS NULL`),其余一切状态一律拦下;
  以后平台再加状态值,默认落在"拦下"那边,不是"放行"那边。
  ⚠ 代价对比说清楚:沃尔玛那次漏的是一张截图,eBay 这边漏的是**一条重复
  listing + 一个烧掉的 UPC**,而重复 listing 触发的是 eBay 的 duplicate
  listing 政策。P1-4 验收补一条:造一条"`feed_log` pending
  但无 `feed_items` 行"的残局,断言 `list_new` 下一轮**不选中**该 SKU。
- 🔴 **"提交成功" 与 "真的生效" 是两件事,一期只做前者(2026-09-25 对齐
  main #131)**:main 新立了 `ops.feed_effects` + 唯一写入方
  `services/feed_effect.judge`——feed 结果(沃尔玛收没收)与**实际结果**
  (生效 / 未生效)分表、**判一次不回头改**,复核清单 `feed_poll -p review=1`
  给人看,**不挂任何自动化**(同批把"观测驱动的自动重做"改成人工)。
  eBay 侧这条**一样成立而且更明显**:`publishOffer` 回了 listingId 只说明
  eBay 收下了,listing 是否真的在售(政策下架 / 搜索不可见 / 站点不投放)是
  另一回事,§6.4 拿 `status=='PUBLISHED'` 判的是**前者**。一期定稿:
  **只做 feed 结果侧,实际结果侧显式不做**——但要把话说死,免得后来人拿
  "PUBLISHED" 当"在卖":二期建 eBay 对位的实际结果表时,照 `feed_effects`
  的三条形状(唯一写入方 / 判一次不改 / 只出复核清单不挂自动化),**不许**
  做成"未生效就自动重发"。
- 自适应降档一期不做(`api/ebay/_client` 已有官方处方 429 读 reset,
  workflow 层再做=两套限流;头注防补)。起跑抖动保留(应用桶全账号共享)。

### 6.9 变体一期不做

沃尔玛侧该段约 1040 行占对位体量 22%、"不报错的错"高发(08-17 后连修五
处);eBay 侧:inventoryItemGroupKey 不可改、publish 全或无(与"单行失败
不拖垮整批"正面冲突)、增量归组不成立(显式对象全量重放)、250/5/30 上限、
§8.3#11 未核验。**从第一天落 `catalog.ebay_items.variant_group_id` 列**
(⚠ 二期真做变体时,组号**必须走 `sku_codec.mint_group_code`**——main 已把
沃尔玛变体组号也改成不透明码 `G+11 位`、登记进 `catalog.variant_groups`,
且守门测试断言组号字母不与来源字母重合;eBay 侧不许自造组号格式),
api 留签名。要做=+4~5 人日,四项前置:①UPC 结构裁决落地(本文 §2.2 已解)
②沙箱 #11 变体组约束实测 ③PBSE 类目逐变体 GTIN 供给 ④publish 全或无的
失败账设计。

## 七、沙箱实测清单(优先级序;C3 卡 submit_poll 状态字面量,**不卡建表**)

0. 🔴 **GTIN catalog 自动填充**:带 UPC 的 PUT 后 GET 回读比对四字段是否被
   改写(决定 ebay_conform 的回读校验层);
1. SKU 白名单(25707:`-`、`_`、`.` 各一测)——决定 registry 正则;
2. 🔴 C3 withdraw 后回读:status/listingId/listingStatus 残留形态;
3. 重复 createOffer 单条回码;
4. publish 六刀砍必填(parameters[] 是否点名字段/条目级 vs 账号级/图片非
   https 落哪个码——不是 190204);
5. C6 aspects 载荷形状与 `aspectApplicableTo` ITEM/PRODUCT 过滤(conform
   关键路径,不实测只能靠推断);
6. C5 bulk 207:responses[] 保序性与是否带回 sku;
7. product.description 4000 上限;
8. getOffers 参数面:sku 必传 + **marketplace_id/format 过滤参数是否存在**
   (§6.5 反查判据依赖);
9. publish 路径尾斜杠两种各一打;
10. C9 taxonomy 双 scope(metadata.insights)是否强制;
11. opt-in 24h 生效计时、409 语义;handlingTime 顶层 vs 嵌套(被拒的记进
    注释);code 有效期(等 6 分钟验证);
12. C7 sandbox 类目树与生产是否一致(不一致则沙箱冒烟只能验调用形状,
    验收格写成条件式);C10 建议顺序稳定性(不稳则 agreement 删 agree_top1)。

## 八、排期与验收

| 批次 | 内容 | 人日 | 验收要点 |
|---|---|---|---|
| P1-1 | _http 抽取+registry(含 platforms.py/飞书两表)+ebay_accounts+_client+tokens+authorize+runbook+文档勘误回改 | 7 | 沃尔玛 pytest 全绿(18 处 monkeypatch 例外清单)/sandbox 与 production 各铸令牌+getRateLimits/进程超时 90.0 冒烟/账号互斥断言单测(含停用店名反例)/state 不符抛错单测 |
| P1-2 | 全部 DDL+claims/upc 改造+台账谓词补全+**risk_trace 四证据源谓词(§4.4)**+events 契约 | 7 | db_init 两跑+六格对拍+读 SQL count·md5 对拍/沃尔玛上架与分配链 --dry-run 摘要逐字一致(claims 11 读+3 写+3 事件桥、upc 7 写+6 读投影,**含 `alloc_plan -p from_sheet=1` 点名分配那条路径**——预期红清单按 §2.1/§2.2 的现场重核数,别按旧稿的 9+2)/UPC 三级取号+烧号保护+「烧后重上领新号」单测/飞书 UPC 表投影逐行对拍/browse_node_id 空行占比落数/**risk_trace 展开结果不含 eBay 账号**(造 claims+listing_sources+product_events 三条 eBay 行) |
| P1-3 | bootstrap+taxonomy+catmap 测试链+account_health | 6 | sandbox 户口链重入两遍/生产拉真树+版本哨兵/aspects 解耦拉取(新 promote 类目当日拿到 aspects)/promote 三格(缺省中、入料只吃高、置高路径逐条点名)/refresh 探活停链 |
| P1-4 | 中立抽取(3)+admission/conform/pricing+api 两文件+list_new+submit_poll+**`refdata/ebay_slas.tsv`(官方期限登记表,§6.5)**+飞书投影 | 15 | 沙箱清单 13 项完成/sandbox 端到端 3 SKU PUBLISHED/防重三态+熔断+双闸+重试闸单测/**抽码即登记单测**(eBay 行落 `listing_sources` 且 `abandoned_at IS NULL`、码形符 `OPAQUE_SQL_PREDICATE`、`-p dry_run` 走 `DRYRUN_PLACEHOLDER` 不落码)/**残局单测:造一条「`feed_log` pending 但无 `feed_items` 行」,断言 `list_new` 下一轮不选中该 SKU、`submit_poll` 的选行面选得中它**/--dry-run 人眼确认/中立抽取后沃尔玛输出逐字不变 |
| P1-5 | 生产单账号试点 | 2+观察 | 首批 ≤10 条人工放行类目/真实 PUBLISHED/错误账收官/两周观察后再谈放量。**界定:试点 ≠ ebay_plan 批次 10;试点期不拉订单、库内无买家数据,不触发合规订阅义务;放量、拉订单、提额之前批次 11 仍是硬门槛** |

合计 **≈37 人日 + 2 周试点观察**。P1-3 的 taxonomy 半批**编码**可与 P1-2
并行,**跑通与验收必须等 P1-2 的三张 audit 表落地**(ebay_plan §5.0 同款
前置教训)。**一期全部 eBay workflow 不进 `registry/schedule.JOBS`**(手动
跑,`ebay_authorize` 同 store_release 归"不在表里=手动"清单;上调度随二期
批次 10,届时才动 test_launchd/skill_export)。文档同步全程适用:README
三处(test_readme 守门)/db_schema.md/**feishu_tables.md**/本文与 runbook。

## 九、文档勘误清单(P1-1 首任务回改两份 08-25 文档)

蓝图:§5.3 SKU 白名单(引 Inventory 侧 25707 原文)/§6.10 删 190204 改
2501x·25501/§4.5 headers 按端点表收窄(Account 族只要 Content-Type)/
§8.3 状态更新(#3 方向关、#8 半关、#11 半关、#18 offer 半关、condition 关)/
§8.2#7 类目季更官方原句反向勘误/§7 `_invalidate_client` 单参/补
getExpiredCategories 与 getCategorySubtree 登记/§4.3·§7 补 metadata.insights
scope/§7 fetch_item_aspects 补 gzip>100MB 警告/§3.2 补 Taxonomy 行。
计划:§3.3 按当前代码校准(五条 SQL)/§3.4 S2 作废 S1 激活/§3.6 范例 B
整段重写(嵌套 IF 范例在 :579)/批次 4a ①②③ 作废/§七#9 作废/4b 体量
+9.5%(list_new 1839 行)/rate_acquire 25 处 9 文件/4b 验收行号
1258-1273→1568-1583/4a 反哺器行号(listing_sheet 519→516、399→394、
feed_poll 55-64→55-67)/oauth-quick-ref-user-tokens.html 已 404 删引用。

## 十、待所有者一句话确认(均有默认值,不阻塞开工)

1. UPC L2 复用按**双向**实现(沃尔玛也复用 eBay 先领的号);
2. 上架入料只吃 confidence='高' 且逐条人工 promote 过的类目映射(一期能上
   的类目集=人工放行过的那些);
3. eBay 定价一期沿用沃尔玛限额表倍率列、出界不上;
4. 变体一期不做(§6.9;要做 +4~5 人日与四项前置);
5. `_is_persistent` 封顶 1000 副作用知情(Taxonomy/Finances/Post-Order 桶
   留进程内);
6. **日上架条数闸的数值**(与 ④ 的账号数量、代理三件套同批提供实值)。
