# eBay 一期设计方案(跑通到上架)

> 2026-08-30 定稿。基于批次 0 拍板(§0)与六路代码级调研(仓库三路:上架链精读/
> 基建接入面/库与 UPC·claims;官方三路:上架 API 字段级/认证户口序列/Taxonomy
> 与沙箱清单;调研笔记在会话 scratchpad,证据结论已收敛进本文)。
> **实现一期以本文为准**;与 `docs/ebay_plan.md`、`docs/ebay_api_blueprint.md`
> 冲突处本文胜(两份文档是 08-25 快照,已知错误集中在 §9 勘误清单,P1-1 首任务
> 统一回改)。一期不做的域(订单/售后/结算/维护巡检/变体)仍以 ebay_plan 批次
> 5~11 为纲。

## 〇、批次 0 拍板记录(2026-08-30,所有者)

| # | 判据 | 拍板 | 与 08-25 推荐值的关系 |
|---|---|---|---|
| ① | 品牌/产品冻结 | **不跨平台**(每平台唯一;沃尔玛占用的品牌/ASIN eBay 也可上)| 与推荐一致(所有者当日先拍"跨"后更正为"不跨") |
| ② | 双平台库存 | **各自与亚马逊保持同步**(库存来源=采集快照,无跨平台扣减器) | 与推荐一致 |
| ③ | UPC | **跨平台复用**(同一产品两平台同一个号) | 推翻 §3.4 的 S2 起步案,激活 S1 拆表(§4.2) |
| ④ | 账号与代理 | 每账号固定出口代理必做、缺代理整账号跳过;**账号数量待所有者填实值**(设计按 ≥1 且可多) | 与推荐一致 |
| ⑤ | 审核链 | **一期复用沃尔玛 `audit_status`**(approved 才可上 eBay) | 方向 A(与批次 4b/10 接线一致) |
| ⑥ | 无货源模式 | **书面确认要做**(从亚马逊下单直发买家)。⚠ 风险登记不变:eBay 政策明文不允许从其他零售商代发,执法风险由经营侧承担,工程侧以账号健康盯盘与单账号试点控暴露面 | 书面拍板完成 |
| ⑦ | 合规入站面 | 已向所有者解释(webhook 收 eBay 用户注销通知、删其个人数据;我方落库订单数据 ⇒ 豁免不适用)。**一期不阻塞**(一期不拉订单、库内无买家数据);二期订单链与提额前必须落地(ebay_plan 批次 11) | 待所有者拍排期 |
| ⑧ | 订单行身份键 | **暂定**(订单链不在一期,不阻塞) | 悬置 |
| ⑨ | 类目映射 | **先拉 eBay 类目树,LLM 做 amazon node → eBay categoryId 匹配;只能测试做**——LLM 只产建议,人工放行后上架链才消费(§5.3) | 细化落地 |

## 一、一期范围

**目标:单账号在 eBay 美站完成真实上架并拿到回执闭环。** 链条:
授权(人工一次)→ 户口(政策/仓位)→ 类目树与映射(LLM 建议+人工放行)→
选品(库侧 SQL,复用中立积木)→ 领冻结/领 UPC → 构造并提交
inventoryItem→offer→publish → 提交台账三态 → `ebay_submit_poll` 回执收编 →
飞书投影。**非目标**(一期明确不做,防止顺手实现):订单/发货/售后/结算、
价格库存维护巡检、变体组(§6.9,理由六条)、跟卖、Promoted Listings、
合规入站面(批次 11)、`ebay_catalog_sync` 回读链(批次 5;一期
`catalog.ebay_items` 是**空表**,不许给它造写入方——SKU↔UPC 权威在
`catalog.upc_usage`,SKU↔offerId/listingId 权威在 `ops.feed_items`)。

## 二、判据落地总设计

### 2.1 拍板 ①(不跨平台)→ claims 平台化

回到 ebay_plan §3.3 的方向,按当前代码(#93 之后)校准:

- 唯一索引:`claims_active_uniq (kind, claim_key) WHERE active` →
  **`claims_active_platform_uniq (platform, kind, claim_key) WHERE active`**
  (schema.sql 原行原地替换 + `DROP INDEX IF EXISTS claims_active_uniq`,
  迁移块按 §4.6 纪律)。加 `platform text NOT NULL DEFAULT 'walmart'` 列。
- `services/claims.py` 现为**五条 SQL**(`_INSERT/_OWNER/_LOAD/_RELEASE/_PREVIEW`,
  #93 已删 `_BY_STORE`/`owner_of`/`counts_by_store`——ebay_plan 写"六条"过期):
  全部加 platform 谓词;`load_active(conn, kind, *, platform)` 的 **platform
  做成必填关键字参数不给默认值**,逼六个现存调用点(list_new 闸/alloc_plan/
  alloc_products/alloc_push/claim_audit/alloc_audit)显式声明 `'walmart'`,
  eBay 链传 `'ebay'`。每平台唯一后 `_LOAD` 的 `dict(cur.fetchall())` 塌陷坑
  **真实存在**(同 key 两平台各一行)——platform 谓词本身就是修复。
- `try_claim(..., platform)`:"返回值等于自己=成功"的判定同时比
  `(store, platform)`;写侧时机放在**领 UPC 的同一事务里**(入料到提交之间
  隔着构造与校验,期间可能被同平台另一账号占走)。
- **账号名互斥硬约束**(claims 键空间正确性前提):`services/ebay_accounts.
  _normalize` 断言 `stores.registered_names() ∩ eBay 账号名 == ∅`,不满足即抛。
  🔴 用 **registered**(在册)不用 enabled——被守的表按 `store` 圈定与启用位
  无关,停用店名仍占着行(`services/stores.py:70-74` docstring 点名过)。

### 2.2 拍板 ③(UPC 跨平台复用)→ 用量拆表(S1)

`catalog.upc_pool` 主键是 `upc`、领用信息是主表裸列——**一个号只记得住一个
使用者**,拍板 ③ 结构上装不下 ⇒ 新建用量表,把挤在一个 status 里的两件事拆开:
**池位**(`upc_pool.status`,取值集一个不改)答"还能不能发给新人";**用量**
(`catalog.upc_usage.status ∈ claimed/used/retired/released`)答"谁正在用"。

- `catalog.upc_usage`:PK `(upc, platform, store, asin)` + sku/claimed_at/
  used_at/released_at。存量回填自 upc_pool 裸列(platform='walmart')。
  ⚠ 不建 `(platform,store,asin)` 唯一索引——同店同 ASIN 历史上合法地有过
  多个号,建了回填当场炸。
- `claim()` 三级取号:**L1** 本平台本店该 asin 活跃用量取最早(保住
  2026-08-19 `ERR_EXT_DATA_0101211` 实证语义,逐字不变)→ **L2** 全平台该
  asin 活跃用量取最早,复用同号并为本平台 INSERT 新用量行(拍板 ③,**双向**:
  eBay 复用沃尔玛的号,沃尔玛也复用 eBay 的;单向=双轨)→ **L3** 新领
  (`FOR UPDATE SKIP LOCKED` 同今天)。
  🔴 **`claim()` 入口必须 `pg_advisory_xact_lock(hashtext('upc:'||asin))`**:
  拍板 ① 更正为每平台唯一后,"两平台同时为同一 ASIN 首领各拿一个新号"的
  竞态是**活的**(claims 拦不住跨平台同 ASIN 并存),锁是唯一防线。写进头注。
- **烧号保护**(今天会殃及:池位 conflict=永久弃用 ⇒ eBay 摸不到 ⇒ 领新号 ⇒
  一个产品两个码,不报错):`burn_for_retire` 改两步——先只标**本平台**用量
  `retired`;仅当该号再无任何平台活跃用量时才置池位 `conflict`。单平台场景
  与今天逐字一致(沃尔玛回归对拍能过的唯一原因)。撞库 `mark_conflict` 反向:
  池位无条件 conflict(号不再发),活跃用量不动(不碰 eBay 在架 listing)。
  ⚠ 沃尔玛 SKU_LOCKED 自愈重上会领新号 ⇒ 两平台号从此分叉,可接受,记
  `product_events` 事件。
- 池外同批改:`listing_sheet._mark_upc_conflicts`(现查询连 store 都不带,
  只补平台、行为不变)与 `heal_unknown` 的 UPC 池写路径;`mark_used/release/
  burn_for_retire` 签名加 `platform`(+store),调用方 7 处逐条改(r3 笔记有表)。
  upc_pool 五个领用列照 `catalog.products` 五死列先例**先留列不删、不读不写**。

### 2.3 拍板 ②⑤ → 入料与库存口径

- 库存:上架时 `availableQuantity` 来自 `catalog.latest_snapshot` 的三态
  (与沃尔玛同源积木;NULL≠0 铁律沿用),缺货/未采到不上。持续同步是二期
  维护链(ebay_plan 批次 7)。
- 审核:入料 SQL 谓词 `p.audit_status='approved'`(平台注记:结论是沃尔玛
  政策口径,代价=品类被连坐收窄;`risk_product_types` 内连接 + `walmart_pt
  <>'unknown'` 三重收窄**一期保留**作 fail-safe,P1-4 验收量化一次"去掉后
  候选数变化"作为二期审核平台化的收益上界)。

## 三、P1-1 基建批次(约 7 人日)

### 3.1 api/_http.py 中立层抽取(动沃尔玛生产代码的唯一一批)

按 r2 逐函数搬家表:`api/_client.py`(701 行,33 顶层符号)**搬 14**
(socket.setdefaulttimeout/连接池两常量/_build_transport/_get_client/
_invalidate_client/_close_all_clients/_rate_state/_is_persistent/_PG_COUNT_SQL/
_acquire_pg/rate_acquire/_acquire_mem/download_bytes/_parse_retry_after)
**留 13、壳 6**;仅 `rate_acquire(bucket, key, buckets)` 与
`_is_persistent(bucket, buckets)` 改签名。四条硬修正(蓝图按字面写会出事):

1. `_is_persistent` 判据 = **`(window >= 600.0 or limit <= 10) and limit <= 1000`**
   ——蓝图定稿的"≥600 且 ≤1000"会把沃尔玛 insights 16 个 1/min 桶踢出跨进程
   共享,`test_rate_bucket` 当场红。副作用知情:eBay 的 Taxonomy 4k/Finances
   12k/Post-Order 4k 桶留在进程内(各链每天单进程一跑,可接受,头注写明)。
2. `tests/conftest.py:20` 的 autouse 夹具 monkeypatch 的是 `_client._acquire_pg`
   ——搬家后**静默失效**,全仓稀缺桶用例会连真 PG。同批改三处:conftest、
   `test_rate_bucket` 的 `_REAL_ACQUIRE_PG` 与 `_is_persistent` 单参调用、
   MockTransport 打桩点。P1-1 验收"纯搬家全绿"必须写明这条例外。
3. `_invalidate_client(proxy)` 保持单参(蓝图 §7 的双参签名查无此物)。
4. 搬家清单补三组:`backoff/BACKOFF_LADDER`(#92 上提,全项目退避唯一出处;
   不搬则纯 eBay 链要 import 沃尔玛 _client,自相矛盾)、`SOCKS_ERRORS/
   _NET_ERRORS`、`_HAS_H2/_HTTP2`(env 名保留 `WALMART_HTTP2` 并头注说明)。
   ⚠ `test_store_retry_standard` 用 `is` 断言 `feeds._backoff is _client.backoff`
   ——搬后必须同一对象,不能复制。

### 3.2 店级失败标准的跨平台接入(动沃尔玛侧 3 行)

- `api/_http.py` 立中立基类 **`IdentityDeadError`**;`_client.StoreDeadError`
  与 `api/ebay/_client.EbayAccountDeadError` 都继承它(仓里有同款先例
  `StoreProxyError ⊂ httpx.ProxyError`);`store_retry.fan_out/serial_second_pass`
  的判据从 StoreDeadError 改为基类(2 行)。
- `store_retry.diagnose(err, vendor="沃尔玛")` 参数化,归类词保持唯一出处、
  六档不加档;eBay api 层报错**逐字用同一文案形状**(`返回 {status}`/
  `返回 None`),测试钉两端。
- 账号 dict 用 **`"name"` 键**(fan_out 只认它)。
- **一期不继承标准③④**(水位避让+链尾重赛:`store_absence` 判据 SQL 写死
  `catalog.walmart_items`,`cli._replay_absent` 触发写死 `"catalog_sync"`)——
  eBay 链**不声明 SUPPORTS_STORE**,设计上写明"随批次 5 回读链一起补"。

### 3.3 registry 登记(先登记后引用)

- env 定名(值进 `<DATA_ROOT>/.env` 与 `init_data_root._ENV_TEMPLATE` 同步):
  `EBAY_CLIENT_ID/EBAY_CLIENT_SECRET/EBAY_RUNAME` + `_SANDBOX` 三变体、
  `EBAY_ENV`(sandbox|production,缺省 production)。
- `ebay_base_url(family, env)`:host 按族(sell REST=api / finances=apiz /
  auth=auth / token=api…),**scope 串与环境无关恒为 `https://api.ebay.com/...`**
  (官方 curl 实证:打 sandbox 端点带 api.ebay.com 的 scope)——scope 常量集
  单份。
- scope 集:`EBAY_SCOPES_USER = sell.account + sell.inventory + sell.fulfillment`
  (**consent 一次要齐**:refresh 的 scope 只能 ≤ consent 那组,事后加 scope=
  重走浏览器;fulfillment 为批次 6 预留省一次人工)、`EBAY_SCOPES_TAXONOMY =
  api_scope + metadata.insights`(getItemAspectsForCategory 双 scope 实证)。
- SKU 正则 **`^[A-Za-z0-9]{1,50}$`**(官方 25707 白名单,比旧稿更窄——含
  `._-` 的旧稿正则会整批被拒);**一期 SKU=ASIN 原文**(天然合规,零发明)。
- 错误码常量:25707(SKU)/25002(多义,禁单判)/25025(并发)/25729(offer 唯一)/
  25713(offer 不存在)/25702·25710(重复删)/25014·25015·25501·25086(图片)——
  ⚠ **190204 是 Trading 侧码,Inventory 侧图片错是 2501x/25501**(勘误 §9)。
- 桶登记 `api/ebay/_client._RATE_BUCKETS`,**键一律 `ebay.` 前缀**(两平台共写
  `ops.rate_events`,撞名互扣配额;守门测试断言前缀)。
- `EBAY_LISTING_REVISE_PER_DAY = 250`(官方:每 listing 每自然日修订上限,
  卖家级,API 桶挡不住——二期维护链按 listing 计数)。

### 3.4 services/ebay_accounts.py + api/ebay/_client.py + 令牌

- 三层判据仿 stores.py:`registered_names()`(在册)/`enabled_names()`(在营,
  `is_enabled` 照抄含 bool 分支)/`load_accounts()`(能调 API=库里有未过期
  refresh_token,**跨层读 ops.ebay_tokens**,凭证飞书表只放 启用+代理三件套+
  marketplace)。快照走 `paths.ebay_accounts_snapshot_file`(新函数,绝不复用
  stores 的文件)。并发常量 `EBAY_ACCOUNT_WORKERS = 4` 起步(令牌桶按应用身份
  共享,起跑抖动因此比沃尔玛更必要)。
- `ops.ebay_tokens`:PK **`(account, env, token_kind, scopes)`**(应用令牌按
  scope 集合铸,一期就有窄/宽两种;w3 实证)。刷新响应**没有 refresh_token 键**
  ⇒ UPDATE 的 SET 列白名单**不许出现 refresh_token**(比".get() 洗 None"更硬
  的防线)。泄漏三孔同批堵:backup `--exclude-table-data=ops.ebay_tokens`、
  REVOKE readonly(🔴 写进 `workflows/db_init.py._READONLY_SQL`,**不进
  schema.sql**——无 readonly 角色的机器会炸掉整份 DDL)、db_schema.md 标注。
- `workflows/ebay_authorize.py`(DANGEROUS=True)+ `docs/ebay_runbook.md`:
  拼 consent URL(七参数;redirect_uri 填 **RuName 字符串**,三条回调 URL 只活
  在 eBay 后台)→ 人工浏览器同意 → 从 Auth Accepted 地址栏拷 code(页面 404
  也拿得到码;**5 分钟内**粘回,官方示例 299s)→ `exchange_code`(🔴 **必须且
  只能 unquote 一次**——code 是 URL-encoded,httpx data= 会再编码,不解码永远
  invalid_grant)→ 落库 → 打印 refresh_expires_at(47,304,000s≈547 天)。
  runbook 另记:RuName 后台六步、sandbox 测试卖家(TESTUSER_ 前缀、周末刷回
  50 万 play money)、store_release 对 eBay 账号要 `-p mark_offline=0`。
- `workflows/ebay_account_health.py`:每日**真发一次 refresh grant 探活**
  (`invalid_grant`=已吊销——卖家可在 My eBay 撤授权,只算天数会漏)→ 已吊销/
  <7 天:抛多行 RuntimeError 停链;<30 天:摘要首行预警。

## 四、P1-2 库批次(约 5 人日)

### 4.1 DDL 清单(全部照 §4.6 迁移纪律写)

| 对象 | 动作 |
|---|---|
| `catalog.claims` | + `platform` DEFAULT 'walmart';索引换名重建(§2.1) |
| `catalog.upc_usage` | 新表(§2.2)+ 存量回填 + 三条索引 |
| `ops.feed_log` | + `platform`;`feed_log_dedupe_uidx` → `feed_log_dedupe_v2_uidx (platform, feed_type, store, payload_key)` 原地替换 |
| `ops.feed_items` / `feed_item_errors` | + `platform` 标注列(**主键不动**——eBay 一阶段一行、`feed_type` 三值区分,`(feed_id,sku)` 天然不撞;submission_id 列**不需要**,ebay_plan 批次 4a ①②③ 作废) |
| `catalog.product_events` | + `platform` + 5 视图 DROP 重建(`audit_listing_conflicts` 改后 EXPLAIN 必须仍走 `product_events_identity_idx`,表达式逐字一致) |
| `catalog.listing_sources` | + `platform` 标注列;schema.sql 的存量回填 INSERT 显式补 `'walmart'`(三处消费方都锚在 walmart_items 上,JOIN 即天然谓词,一期不改) |
| `catalog.ebay_items` | 新表(一期空表,写入方=批次 5;状态列**先 text 不加 CHECK**,枚举待沙箱 C3) |
| `catalog.ebay_accounts` | 新表:account, marketplace_id, 三 policyId, merchant_location_key, opted_in_at, privileges_json, sampled_at,PK (account, marketplace_id) |
| `ops.ebay_tokens` | 新表(§3.4) |
| `audit.ebay_categories` | 新表(严格树单表——CategoryTreeNode 只有单数父指针,**不抄**亚马逊 DAG 双表;`is_leaf` 硬闸列) |
| `audit.ebay_category_tree_versions` | 新表(node_count/leaf_count/payload_bytes 实调回填) |
| `audit.ebay_aspects_cache` | 新表(🔴 `constraint_raw/values_raw` jsonb 原样列必留——官方刚加过 aspectAdvancedDataType,拆死列静默丢新字段) |
| `audit.ebay_category_map` + `_suggestions` | 新表(§5.3;`candidates_raw/agreement` 列不许省) |
| 不动 | `ops.dispositions`(一期零写入方)、orders 三表(v2 守卫地雷,整批做)、products/snapshots(仅两处纯注释:marketplace 语义钉死) |

### 4.2 product_events 契约

`record_many` 加显式 `asin` 入参;**eBay 行一律不反解**(`extract_asin` 是
沃尔玛订货号形态规则,eBay SKU 会误命中 `_PLAIN` 写入假 ASIN,而 5 个视图
身份键正是 `coalesce(asin, sku)`)。eBay 事件码(`ebay_item_submitted` 等)
同批进 `EVENTS` 与 `_FEED_KIND`。

### 4.6 迁移块纪律(订正 ebay_plan §3.6)

schema.sql 现有 **四处** `DO $$`(:66/:579/:608/:1300),其中 **:579-589 就是
嵌套 IF 范例**,注释逐字:"平铺 AND 会在计划期解析表名,重跑必炸
UndefinedTable(2026-08-13 生产实证)"。判据:守卫只查
information_schema/pg_indexes/to_regclass ⇒ 单层够;**守卫内层要引用可能已
不存在的对象 ⇒ 必须嵌套 IF**。另三条硬约束(整份 schema.sql 跑在一个事务):
一句报错整份回滚;禁 CREATE INDEX CONCURRENTLY;REVOKE 不进 schema.sql(§3.4)。
验收:db_init 幂等两跑 + r3 六格专项对拍(upc_usage 行数/池位分布逐值/回填
无漏/claims 新索引在旧索引亡/feed dedupe 只剩新名/order_lines 行数不变)。
⚠ `db_init` 的 `--dry-run` 是真跑(DANGEROUS=False 且 run() 不读 dry_run)
——库改动人眼确认只能靠 git diff + 影子库;顺手补 dry_run 与守门测试加强。

## 五、P1-3 户口与类目批次(约 6 人日)

### 5.1 workflows/ebay_bootstrap_account.py(可重入,不是直线脚本)

① `getOptedInPrograms` 回读 → 未 opt-in 才 POST(`SELLING_POLICY_MANAGEMENT`;
sandbox 同样需要;**最长 24h 生效**——未生效时摘要点名并正常返回,不抛);
② 三政策**先按 name 回读再建**(POST 无幂等键,重复=重名第二条);最小请求体
按 w2 笔记(fulfillment 含 handlingTime **顶层**——官方两页矛盾,头注抄矛盾
防止"照样例改回去";payment 在 managed payments 下不带 paymentMethods,400 再
调);③ location:**merchantLocationKey 自己生成、先落 `catalog.ebay_accounts`
后调接口**(POST 回 204 无 body),事后 GET 对账——与"防重先落库"同款;
④ `getPrivileges` 落 privileges_json:`sellerRegistrationCompleted=false` 即
整账号跳过(硬准入闸);sellingLimit 容器可能整个缺失——fail-closed 报告时
**"字段缺失"与"调用失败"分开报**。

### 5.2 workflows/ebay_taxonomy_sync.py

应用令牌。每日版本哨兵(`getDefaultCategoryTreeId` → treeVersion 变了才拉
`getCategoryTree` 整树拆行入库 + 原始 gzip 按版本归档 `<DATA_ROOT>`,归档
零消费只为回溯,明禁从归档读数回来判断);aspects 逐类目
`getItemAspectsForCategory` **只对映射表 approved 类目拉**(版本变更日 ~300
次,对 4k/day 桶留一个数量级);🔴 **`fetchItemAspects` 显式禁用**(官方:
gzip 二进制可超 100MB;spec 还自相矛盾声明 json——绝不能进 resp.json())。
版本变更时**映射表体检**:校验 active 行存在性+is_leaf,不过标 stale 并摘要
首行点名(`getExpiredCategories` 只登记不实现)。

### 5.3 类目映射测试链(拍板 ⑨;与沃尔玛 catmap_suggest→promote 同构)

- `workflows/ebay_catmap_suggest.py`:按 **amazon_node_id** 出建议(一 node
  一次 LLM,禁止逐 ASIN/逐 SKU 调——唯一能打爆 taxonomy 桶的方式,头注写死;
  `-p limit` 硬上限);召回器=`getCategorySuggestions`(**只在 production 跑**
  ——sandbox 返回样板文本假成功,env=sandbox 直接抛错禁止落库;q 传**商品
  标题**不传类目路径;`relevancy` 官方"Reserved for internal use"**禁当置信
  度**,顺序才是信号);LLM=排序器(输入:node 的 DAG 全路径+样本标题品牌,
  候选 10~15 封顶;输出 ebay_category_id/confidence 高中低/reason),走
  `api/llm` + `llm_cache`;落库两道机器闸:必须 ∈ 本次候选集 + JOIN
  `audit.ebay_categories AND is_leaf`。🔴 **LLM 只准写 suggested 永不自写
  approved**(照 `products.pt_source` 洗白教训,schema.sql:26-31 原句)。
- `workflows/ebay_catmap_promote.py`(DANGEROUS=True,`-p nodes=` 逐条点名):
  suggested→approved/rejected,ops.runs 留痕(一期不建飞书表,零登记成本;
  二期再投影)。**promote 缺省写「中」、上架入料只吃 confidence='高' 且
  status='approved'**——这道握手位是"人工放行"的全部实现,写死并配测试
  (沃尔玛首批 50 条里就有 Building Sets→Advent Calendars 被判「高」的实证,
  照引)。部分唯一索引 `(amazon_node_id, marketplace_id) WHERE approved`
  兑现"一个 node 只一条生效行";`category_tree_version` 列(树换版要复检)。
- 接线口:`m.amazon_node_id = p.browse_node_id` **单条 JOIN**(两条=两套
  口径);`browse_node_id` 为空的行被 fail-closed 全拦——**P1-2 开工第一步
  连库 count 一次空行占比**,写进验收。

## 六、P1-4 上架闭环批次(约 12 人日)

### 6.1 驱动源与入料(与沃尔玛根本不同,写死防照抄)

沃尔玛驱动表是飞书上架表(读表领任务);**eBay 一期没有分配链,驱动源=库侧
SQL 选品**,飞书 eBay 上架表只是**结果投影**(否则实现者照抄 read_rows 而那
张表永远是空)。入料 SQL(复用 `product_pool` 中立取数):
`audit_status='approved'` + 类目映射 approved+高(fail-closed 拦下计数)+
claims 平台内未占 + `upc_usage`/`feed_items` 双去重 + risk_product_types
三重收窄(§2.3)+ **`ORDER BY 分数 LIMIT cap`**(沃尔玛靠飞书表天然封顶,
eBay 全库扫描没有上限——没有 LIMIT 会对几十万行打 LLM,一期独有新风险)。
逐行闸(每条写理由):brand_key 黑名单/库存三态/渠道/运费/落地价/lead/
定制品闸 `is_customized`(#96,判据来自亚马逊源侧与平台正交)/图片(**全部
https**——http 图会让 publish 整批失败且 §5.4 会当条目级失败逐条重试,必须
services 层预校验)。

### 6.2 双闸配额与熔断

- **selling limit 闸 fail-closed**(平台强制硬上限,读不到不上——方向与
  沃尔玛 `_load_quota` 的"读不到默认 999"相反;⚠ 别扩大化:lead/channel 闸
  保持 fail-open 与沃尔玛上架侧同)。+ **自定日上架条数闸**(刊登费是真实
  固定成本,免费额度 250 条/月/账号,超出 $0.35/条——"能上就上"不成立)。
- **账号级熔断**:eBay 的"账号被限制"是账号级失败,逐条重试会把整批打成假
  失败 ⇒ 进程内熔断器按 account,命中即剩余行不再落 pending、直接写理由。
- **零账号完成即判失败**(§六#8 继承)。
- 25025 并发约束:同账号同 SKU 的三步必须串行(一期单链内天然满足;头注写明
  约束,二期维护链上线前按 `ops.dedupe`/advisory 锁串行化)。

### 6.3 定价

落地价(单价+运费,运费 NULL 不定价)× 区间倍率(一期沿用沃尔玛限额表列,
eBay 专属列待所有者配置);**出界不上架**(不沿用 300% 兜底——刊登费与无货
源毛利结构下"能上就上"不成立;差异点头注写明)。

### 6.4 三步链字段要点(全量字段表在 w1 笔记,api 层照抄)

- **PUT /inventory_item/{sku}**:SKU=ASIN;header `Content-Language: en-US`
  (连字符;body 里 locale 是下划线 `en_US`——两种形态别混);aspects=
  **`dict[str, list[str]]`**(值恒为 list;spec 的 type:string 是 spec bug);
  product.description ≤4000 只放短摘要;图片 imageUrls 全 https、≥1 ≤24;
  成功码 **200/201/204 且 204+空体=无警告的正常成功**(`if not data: fail`
  会把干净成功判失败);PUT 幂等可重放。
- **POST /offer**:唯一性=(sku, marketplaceId, format) 官方约束——重复
  createOffer **会被拒不会建出第二个**(§8.3 #3 方向关闭;单条回哪个码沙箱
  一测);`pricingSummary.price.value` 是**字符串**;长文案写
  **`offer.listingDescription`(≤500,000 含 HTML)** 不是 product.description
  (塞错位置=第一步被拒且排查方向跑偏);categoryId=映射表 approved 行;
  listingPolicies 三 id + merchantLocationKey 出自 `catalog.ebay_accounts`;
  `includeCatalogProductDetails` **显式传 false**(缺省 true 会让 eBay catalog
  覆盖我方文案);缺 merchantLocationKey createOffer 静默过、publish 必失败
  (官方直述已证)⇒ 必填校验放 **publish 前的 services 层**。
- **POST /offer/{offerId}/publish**:必填四组(含 **product.aspects 官方点名
  必填** ⇒ `ebay_conform` 是关键路径不是加分项;只读 `aspectRequired` 判必填,
  不读 aspectUsage;`aspectApplicableTo=PRODUCT` 的卖家改不了、硬塞会被静默
  忽略;`valueConstraints` 非空一期 fail-closed 拦下);响应 listingId;
  warnings/errors 结构 errorId/domain/category/message/parameters。
- **bulk 端点 207 Multi-Status**:逐条读 `responses[].statusCode`,"2xx 即
  整批成功"在 bulk 上是**错的**;失败行**只按原方法补交一次**(整批重发会让
  已成功的撞 25729);responses[] 是否保序/带回 sku 沙箱 C5 确认前,一期
  **先走单条端点**(量小、语义清晰),bulk 留 api 签名。

### 6.5 提交台账与 submit_poll

- 三态逐字继承、时机重定:`feed_log` 在每步 POST/PUT **前**落 pending
  (feed_id NULL,`feed_log.feed_id` 本就可空);`feed_items` 拿到 2xx **后**
  落(ebay_item 行存 sku、ebay_offer 行存 offerId、ebay_publish 行存
  listingId)。延后结算:载荷由 builder **确定性重建**,不扛首发那份走全轮。
- `ebay_submit_poll` 反查:ebay_item→`GET /inventory_item/{sku}` 200=FOUND;
  ebay_offer→`GET /offer?sku=` **恰好 1 个**=收编 offerId、0=NOT_FOUND、
  **≥2=不许自动选,落 missing+账号级告警交人**(对 #3 的兜底);ebay_publish→
  `GET /offer/{offerId}`,FOUND 判据 **`status=='PUBLISHED'`**(取值封闭集
  {PUBLISHED, UNPUBLISHED} 已文档级证实;listingId 只作辅证——withdraw 后
  listingId 是否残留待沙箱 C3,不拿它当主判据)。NOT_FOUND 同方法补交一次;
  30s 双确认缩到 5~10s(标"工程值非官方")。沃尔玛的 `_claimed_ids` 兄弟切片
  排除/itemsReceived 指纹整段不需要。
- 自适应降档**一期不做**:eBay 有官方处方(429 读 reset 睡到 reset)且已在
  `api/ebay/_client` 落——workflow 层再做一套=两套限流实现,`ebay_list_new`
  头注写明防人补上。起跑抖动**保留**(eBay 令牌桶全账号共享一个应用桶,
  抖动治桶争用,理由比沃尔玛硬)。

### 6.9 变体一期不做(留六条依据速记)

沃尔玛侧该段约 1040 行占对位体量 22%、是"不报错的错"高发区(08-17 后连修
五处);eBay 侧门槛更硬:inventoryItemGroupKey 不可改、publish 全或无(与
"单行失败不拖垮整批"正面冲突、无条目级部分成功)、增量归组不成立(显式对象
全量重放)、250/5/30 上限、变体约束 §8.3#11 未核验。**从第一天落
`catalog.ebay_items.variant_group_id` 列**(计划性,不发);api 侧留签名不留
实现。要做=+4~5 人日与四项前置(见 r1 笔记)。

## 七、沙箱实测清单(w1+w3 合并,执行在 P1-4 前半)

优先级排序;🔴 C3 是**唯一卡建表的**(在 ebay_items 状态列写死前完成):

1. **SKU 白名单**(25707 文案 vs 实际:`-`、`_`、`.` 各一测)——决定 registry 正则,错了整批失败;
2. 🔴 **C3 withdraw 后回读**:status/listingId/listingStatus 残留形态;
3. 重复 createOffer 单条回码(25729/25709?);
4. publish 六刀砍必填(重点:parameters[] 是否点名字段/条目级 vs 账号级/图片非 https 落哪个码——**不是 190204**);
5. C5 bulk 207:responses[] 保序性与是否带回 sku;
6. product.description 4000 上限确认(决定长文案切分);
7. publish 路径尾斜杠两种各一打;
8. getOffers 的 sku 参数按必传(spec 自相矛盾);
9. opt-in 24h 生效计时、409 语义;
10. handlingTime 顶层 vs 嵌套(被拒的那种记进注释);
11. code 有效期(故意等 6 分钟验证 299s);
12. C7 sandbox 类目树与生产是否一致——**不一致则沙箱冒烟只能验调用形状**,
    验收格写成条件式;C10 建议顺序稳定性(不稳则 agreement 删 agree_top1 档)。

## 八、排期与验收

| 批次 | 内容 | 人日 | 验收要点 |
|---|---|---|---|
| P1-1 | _http 抽取+registry+ebay_accounts+_client+tokens+authorize+runbook | 7 | 沃尔玛 pytest 全绿(3 处测试例外声明)/sandbox 两种令牌+getRateLimits/`python -c "import api.ebay._client, socket; print(socket.getdefaulttimeout())"`→90.0/账号互斥断言单测(含停用店名反例) |
| P1-2 | 全部 DDL+claims/upc 改造+events 契约 | 5 | db_init 两跑+六格对拍/沃尔玛上架与分配链 --dry-run 摘要逐字一致/UPC 三级取号+烧号保护单测/browse_node_id 空行占比落数 |
| P1-3 | bootstrap+taxonomy+catmap 测试链+account_health | 6 | sandbox 户口链跑通重入两遍/生产拉真树+版本哨兵/LLM 建议 20 node 人工过目/promote 握手位测试(缺省中、入料只吃高) |
| P1-4 | admission/conform/pricing+api 两文件+list_new+submit_poll+飞书投影 | 12 | 沙箱清单 12 项完成/sandbox 端到端 3 SKU PUBLISHED/防重三态+熔断+双闸单测/--dry-run 人眼确认 |
| P1-5 | 生产单账号试点 | 2+观察 | 首批 ≤10 条人工放行类目/真实 PUBLISHED/错误账收官/两周观察后再谈放量 |

合计 **≈32 人日 + 2 周试点观察**。串行关键路径 P1-1→P1-2→P1-3→P1-4→P1-5
(P1-3 的 taxonomy 半批可与 P1-2 并行)。每批完成即提交,AI 改码先 --dry-run
纪律、README/db_schema.md/test_readme 守门同步(每新增一条 workflow 都要动
README 三处)全程适用。

## 九、文档勘误清单(P1-1 首任务回改两份 08-25 文档,各 0.5 人日内)

蓝图:§5.3 SKU 白名单(25707)/§6.10 删 190204 改 2501x·25501/§4.5 headers
按端点表收窄(Account 族只要 Content-Type)/§8.3 状态更新(#3 方向关、#8 半关、
#11 半关、#18 offer 半关、condition 关)/§8.2#7 类目季更官方原句反向勘误/
§7 `_invalidate_client` 单参/补 getExpiredCategories 登记。
计划:§3.3 按当前代码校准(五条 SQL、_BY_STORE/owner_of 已删)/§3.4 S2 作废
S1 激活/§3.6 范例 B 整段重写(嵌套 IF 范例在 :579,"查无此物"是错的)/
批次 4a ①②③ 作废(台账不需要 submission_id 列与主键改动)/§七#9 作废
(eBay 要写 claims)/4b 体量 +9.5%(list_new 现 1839 行)/rate_acquire 25 处
9 文件(非 22 处 10 文件)/`oauth-quick-ref-user-tokens.html` 已 404 删引用。

## 十、待所有者一句话确认(均有默认值,不阻塞开工)

1. UPC L2 复用按**双向**实现(沃尔玛也复用 eBay 先领的号)——拍板 ③ 原话
   读起来是双向,单向=双轨;
2. 上架入料**只吃 confidence='高' 且人工 promote 过**的类目映射——意味着
   一期能上的类目集=人工逐条放行过的那些;
3. eBay 定价一期**沿用沃尔玛限额表倍率列、出界不上**(不沿用 300% 兜底);
4. **变体一期不做**(§6.9 六条依据;要做 +4~5 人日);
5. `_is_persistent` 封顶 1000 的副作用知情(Taxonomy/Finances/Post-Order 桶
   留进程内,各链每天单进程一跑)。
