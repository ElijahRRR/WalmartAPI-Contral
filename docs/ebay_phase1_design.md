# eBay 一期设计方案(跑通到上架)

> 2026-08-30 定稿(同日经三路对抗校验修订:官方事实 18 条复核 0 错,仓库
> 一致性与完备性共 5 blocker + 约 30 项已全部收编)。基于批次 0 拍板(§0)
> 与六路代码级调研。**实现一期以本文为准**;字段级参考(请求体/错误码/
> 户口最小请求体/AspectConstraint 全表)在 **`docs/ebay_phase1_reference.md`**
> ——除该文件显式承接的四块外,调研证据结论已收敛进本文。与
> `docs/ebay_plan.md`、`docs/ebay_api_blueprint.md` 冲突处本文胜(08-25
> 两文档的已知错误集中在 §9 勘误清单,P1-1 首任务统一回改)。一期不做的域
> (订单/售后/结算/维护巡检/变体)仍以 ebay_plan 批次 5~11 为纲。

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
  迁移块按 §4.6 纪律)。加 `platform text NOT NULL DEFAULT 'walmart'` 列
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
- **调用面全量**(P1-2 验收写明"以下红/改是预期内的"):读侧
  `load_active` **6 文件 9 处**(alloc_plan:192,193 / alloc_audit:187,188 /
  list_new:374,377 各两处 / alloc_products:96 / alloc_push:69 /
  claim_audit:96)全部显式传 walmart 常量;写侧 `claim_many` 2 处
  (alloc_plan:397、alloc_backfill:161);测试替身 8 处(test_alloc_push:43、
  test_alloc_plan:220/264/325/474、test_list_new:709/1364、test_claim_audit:26)
  与 test_claims:135 位置调用。**`try_claim` 仍返回 store 字符串**(同平台性
  由 `_OWNER` 的 platform 谓词保证,"同时比 platform"冗余;Owner 具名改形
  留二期)——`alloc_backfill:164` 的四元组解包因此不动。
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
- **改动清单(9 处代码 + 1 测试文件,全部同批)**:`mark_used/release/
  burn_for_retire` 签名加 platform(+store),调用方 list_new:1691/250/268、
  listing_sheet:356/498/500、sku_locked_heal:210、upc_sync:34/37/38 +
  tests/test_upc_pricing.py:49-95。**`listing_sheet._mark_upc_conflicts`
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
  `test_store_retry_standard:241` 的 `Path("api").glob("*.py")` **换 rglob**
  (现扫不到 api/ebay/),并加 `diagnose(err, vendor="eBay")` 六档逐档断言。
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
  length"——引这句,勿引 Trading 侧同义句);**一期 SKU=ASIN 原文**。
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
- 摘要与参数标准件:`-p` 布尔一律 `from services.params import flag`
  (⚠ 按名导入,模块名会被 run(params) 形参遮住);摘要一律
  `notify_fmt.head/summary`。八条新 workflow 统一,不许各自手搓。

## 四、P1-2 库批次(约 6 人日)

### 4.1 DDL 清单(照 §4.6 迁移纪律)

| 对象 | 动作 |
|---|---|
| `catalog.claims` | + `platform`;索引换名重建(§2.1) |
| `catalog.upc_usage` | 新表(§2.2)+ 存量回填 + 三条索引 |
| `ops.feed_log` | + `platform`;`feed_log_dedupe_uidx` → `feed_log_dedupe_v2_uidx (platform, feed_type, store, payload_key)` 原地替换 |
| `ops.feed_items` / `feed_item_errors` | + `platform` 标注列(主键不动——**该论证只覆盖 offer/publish 两阶段**:eBay 台账 `feed_items` 只落这两阶段的行,`feed_id` 分别存 offerId/listingId,`(feed_id, sku)` 不撞;**ebay_item 阶段不落 feed_items**,由 `feed_log` pending/submitted 承接——PUT 幂等可重放、204 无 id 可记,硬造 feed_id=sku 会在换账号重上时撞主键静默丢行) |
| `catalog.product_events` | + `platform` + 5 视图 DROP 重建(`audit_listing_conflicts` 的 EXPLAIN 必须仍走 `product_events_identity_idx`);**非视图消费方(blacklist/_LATEST_CTE、problem_scan 三条、sku_normalize、audit_history_fold、cleanup_history_import、dispositions._SETTLE_DELETE_SQL)一期不改,依据=eBay 事件码与沃尔玛零重合(逐条列名进实现注释);二期给沃尔玛加任何同名事件码前必须先补谓词** |
| `catalog.listing_sources` | + `platform` 标注列;schema.sql 存量回填 INSERT 显式补 walmart(三处消费方都锚在 walmart_items 上,JOIN 即天然谓词) |
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
专属规则,eBay SKU 口径将来会变;一期 SKU=ASIN 时两种写法碰巧同值,不许
以此当依据)。eBay 事件码同批进 `EVENTS` 与 `_FEED_KIND`。

### 4.6 迁移块纪律(订正 ebay_plan §3.6)

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
  顺序才是信号);LLM=排序器(走 `api/llm`+`llm_cache`;输出
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
共享 SQL。入料谓词:audit approved + 类目映射 approved+高(fail-closed
拦下计数)+ claims 平台内未占 + **去重谓词写死
`feed_items.feed_type IN ('ebay_offer','ebay_publish') AND status IN
('submitted','success')`**(按"存在任意行"判会把 item 步成功 publish 步
失败的 SKU 永久排除)+ upc_usage 活跃用量去重。**重试闸**:按 (account,
sku) 计次上限 3(对位沃尔玛 `MAX_LIST_ATTEMPTS=3`;没有它,永久失败的
SKU 每天白烧配额与 LLM),4xx 终态拒的错误码集不进重试通道。
逐行闸:brand_key 黑名单/库存三态/渠道/运费/落地价/lead/定制品闸
`is_customized`/图片全 https(services 层预校验)。

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

- **PUT /inventory_item/{sku}**:SKU=ASIN;header `Content-Language: en-US`
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
- 自适应降档一期不做(`api/ebay/_client` 已有官方处方 429 读 reset,
  workflow 层再做=两套限流;头注防补)。起跑抖动保留(应用桶全账号共享)。

### 6.9 变体一期不做

沃尔玛侧该段约 1040 行占对位体量 22%、"不报错的错"高发(08-17 后连修五
处);eBay 侧:inventoryItemGroupKey 不可改、publish 全或无(与"单行失败
不拖垮整批"正面冲突)、增量归组不成立(显式对象全量重放)、250/5/30 上限、
§8.3#11 未核验。**从第一天落 `catalog.ebay_items.variant_group_id` 列**,
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
| P1-2 | 全部 DDL+claims/upc 改造+台账谓词补全+events 契约 | 6 | db_init 两跑+六格对拍+读 SQL count·md5 对拍/沃尔玛上架与分配链 --dry-run 摘要逐字一致(9+2 处调用点改动预期红清单)/UPC 三级取号+烧号保护+「烧后重上领新号」单测/飞书 UPC 表投影逐行对拍/browse_node_id 空行占比落数 |
| P1-3 | bootstrap+taxonomy+catmap 测试链+account_health | 6 | sandbox 户口链重入两遍/生产拉真树+版本哨兵/aspects 解耦拉取(新 promote 类目当日拿到 aspects)/promote 三格(缺省中、入料只吃高、置高路径逐条点名)/refresh 探活停链 |
| P1-4 | 中立抽取(3)+admission/conform/pricing+api 两文件+list_new+submit_poll+飞书投影 | 15 | 沙箱清单 13 项完成/sandbox 端到端 3 SKU PUBLISHED/防重三态+熔断+双闸+重试闸单测/--dry-run 人眼确认/中立抽取后沃尔玛输出逐字不变 |
| P1-5 | 生产单账号试点 | 2+观察 | 首批 ≤10 条人工放行类目/真实 PUBLISHED/错误账收官/两周观察后再谈放量。**界定:试点 ≠ ebay_plan 批次 10;试点期不拉订单、库内无买家数据,不触发合规订阅义务;放量、拉订单、提额之前批次 11 仍是硬门槛** |

合计 **≈36 人日 + 2 周试点观察**。P1-3 的 taxonomy 半批**编码**可与 P1-2
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
