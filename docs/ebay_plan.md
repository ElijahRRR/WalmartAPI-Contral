# eBay 平台扩展计划

> 状态:**决策、契约与开发计划全部定稿,未动任何生产代码**(2026-08-25)。
> 🔴 **先于一切的阻塞项**:本项目「亚马逊采集 → eBay 上架 → 出单后从亚马逊下单直发买家」逐字命中 eBay 明文禁令,且与多账号连坐
> 条款相乘 —— 见判据 ⑥(依据蓝图头 blockquote)。**所有者书面拍板前,eBay 上架链不得起调度**;写代码、跑 `--dry-run`、沙箱冒烟
> 不受此限。
> 范围:**本仓内**新增 eBay 平台的全套业务工作流,与沃尔玛链共享产品库、采集摄取链、运行纪律(cli.py / registry / 锁 / 台账 /
> 通知)。来源:仓库侧三份证据级底稿(库影响 / services 积木清点 / 工程契约,2026-08-25,行号可回溯到 `refdata/schema.sql` 与各
> `.py`)。端点/配额/认证/提交通道形态见 `docs/ebay_api_blueprint.md`(**2026-08-25 定稿,八节,44 组 62 端点**),本文引用它
> 一律写「蓝图 §N」**不重抄**;⚠ 蓝图 §8.3 的未核验项(**2026-08-25 校验轮后为 18 条,其中 5 条已关闭 ✅、1 条降级 🟢**)是本计划
> **全部批次的共同前置**,动手前**按状态列**逐条过,未关闭的必须补核,不许按推断编码。
> 前置事实:全仓 `grep -rni "ebay|易贝"` 只命中 `services/audit_stopwords.py:60` 一个停用词
> ——**eBay 是纯新地**,没有半成品、没有旧 shim、没有并跑切换的旧系统。

**一句话模型:产品库共享、判决分平台、出口各自独立。** 亚马逊侧产品身份与观测(`catalog.products`/`snapshots`,主键
`(marketplace, asin)`,其中 `marketplace` 是**亚马逊源站点** 'US' 而非销售平台)只有一份;每个平台各有一张投影表
(`catalog.walmart_items` / 新建 `catalog.ebay_items`)、一套 api 出口、一份准入判决;共写台账加 `platform` 维度。
**不做双轨、不做隐式路由、不给 platform 参数默认值。**
⚠ **`marketplace` 的语义在仓库里现有两份互相矛盾的注释,而"产品库共享"整条前提压在它身上**:`refdata/schema.sql:15/39` 只写了列与
主键、**未定义语义**;`services/amz_source.py:37` 写的是 `MARKETPLACE = "US"  # 上架目的地(契约:与 (marketplace,asin) 主键对齐)`
—— 与本文断言**字面相反**。现状两个读法结果相同(两平台都卖美国)⇒ **不报错**,正是本仓最贵的那一类。**批次 2a 顺手把语义钉死在
`schema.sql:15` 注释里**(与 `products` 六个 audit 列同一次纯注释改动),并同步修正 `amz_source.py:37` 那行注释。

## 一、决策定稿:不另起项目,同仓扩展

**① 产品库共享且持续增长,两仓写一库 = schema 主权分裂。** `products`/`snapshots` 是两平台共同的货源池,且**不是冻结的历史数据**
——`product_ingest` 每天仍在灌新采集。拆仓后 `docs/db_schema.md`、`refdata/schema.sql`、`db_init` 归谁**无解**:两边都改必漂,一边
跟一边改则永远滞后且不报错。**铁律 3 在跨仓时直接失效**——另一个仓的 registry 不是本仓的 registry,表 ID 与路径必然各存一份。

**② cli.py 全套运行纪律是平台无关资产,拆仓 = 全量复制 + 双仓同步维护。** 逐条点名:`.env` 加载与重复键告警(2026-08-17 那次"值明明
在文件里却读成未登记")、flock 锁与**借锁**语义(eBay 链动 `product_ingest` 游标必须借同一把锁,拆仓后借不到)、`ops.runs`、通知与
串联停链、`--dry-run` 两键注入、`paths` 的 call-time 求值;api 侧 `feishu`/`llm`/`llm_vision`/`scraper`,services 侧 `notify_fmt`。
复制一份 = 同一条纪律有两个"唯一出处",本仓吃过这个亏至少三处(事件码清单各漂、`stores.py` 六个字面量、skill_export 正副本)。

**③ 目录本就按外部系统分,命名早为 eBay 留了位。** `api/` 的划分原则是"按外部系统与沃尔玛 API 域分文件",飞书与采集器本就与沃尔玛平铺
共存;`catalog.walmart_items` 这名字**天然给 `catalog.ebay_items` 留位**;`products` 主键 `(marketplace, asin)` 与 `latest_snapshot`
(`SELECT DISTINCT ON (...) *`,加列自动流过)平台中立。同仓扩展是顺着既有结构长,不是硬塞。

**何时才值得拆(三条判据,现均不成立;任一成立时重新评估)**:① **不同运维人**——不同人值班、互不看对方告警与 `ops.runs`(现:同所有者同飞书群);
② **不同机器且不共库**——eBay 跑在另一台机器、连不到本机 PG(现:同机同库,唯一入口 `registry/db.py`);③ **产品库服务化**——`products/snapshots`
前挡一层 API,两侧只读契约不读表(现:两侧直连表,靠 `product_ingest` 漏斗纪律保证一致)。三条都不成立时,拆仓只买到"目录干净",代价是 ①②③ 全部。

## 二、代码布局定稿

**总原则:现有沃尔玛文件一律不动**(除 §3.1 明列的共享表谓词补全)。eBay 一切以**新增文件**落地;抽公共积木时只做"搬出去 + 原处 import",不改沃尔玛侧调用形态。

### 2.1 api 层:`api/ebay/` 子包(本仓第一个 api 子包)

`__init__.py` 分层说明 | **`_client.py`** eBay 唯一 HTTP 出口(OAuth refresh_token→access_token / 令牌缓存按账号 / 401 自愈一次 /
429 退避 / 连接池 / 令牌桶 / `getRateLimits` 私有对表) | `account.py` 账号与政策档案 | `taxonomy.py` 类目树与 aspects |
`inventory.py` 库存 | `offers.py` Offer 建/发布/改价/下架 | `orders.py` 订单与发货回传 | **`postorder.py`** 退货/取消/INR |
`finances.py` 交易结算 | **`trading.py`** selling limit 余量。**⚠ 本段已按蓝图 §7 修正**(原骨架"以蓝图第 7 节为准"的三处兑现):
`returns.py` → **`postorder.py`**(分文件依据是**认证体系与 base URL 不同** —— IAF 前缀 + 必填 marketplace 头 + `/post-order/v2`,
return 只是它三条链之一;叫 returns 会让下一个人把 cancellation/inquiry 塞进 `orders.py`,那就跨了认证体系);**删掉 `analytics.py`**
(① `getRateLimits` 属 developer.analytics、是 `_client` 的**自用依赖**,单独成文件会造成 `_client ↔ analytics` 循环 import;
② 骨架说的"账号表现"要的是 sell.analytics 的 `getSellerStandardsProfile`,那是**另一个 API 且面未取到**,按"预留只登记不实现"处理);
**新增 `trading.py`**(XML 网关 + 第三种认证头 + **独立 Trading 桶:官方缺省 5,000/day,本仓定稿 500/day —— 🔴 registry 登记的是
**定稿值 500**,别把官方缺省抄进去,蓝图 §3.1**,全仓唯一一条 XML 调用;将来 REST 补上能力后整体删掉)。
结果:`__init__.py` + 9 个实体文件(与原骨架数量相同、成分不同)。
选子包不选平铺:eBay 端点域一定超过 5 个文件,平铺后两平台文件交错、一眼看不出归属。
收录规则同沃尔玛:只实现「工作流×端点矩阵」出现过的端点,预留端点只登记不实现。⚠ 加子包**必须同步改 `api/__init__.py:6-10` 的
"文件划分"人读索引**,漂开会让下一个 AI 照旧索引找文件;⚠ **那段索引本来就已经漂了** —— `api/` 下现有 `settings.py` / `llm.py` /
`llm_vision.py` 三个文件不在索引里,批次 1 既然要动这一段,**顺手把这三个补进去**。

**`_client.py` 的拆法(唯一 api 灰色件)**:连接池 / transport / `_parse_retry_after` / `rate_acquire` 的跨进程滑动窗
(`ops.rate_events` + advisory 锁)/ `download_bytes` 与平台无关,OAuth 与 `WM_*` 头是沃尔玛的 ⇒ 抽 `api/_http.py`(中立)供两侧
import。**限速状态尤其不许重写第二份**——两套会漂的限速状态,漂了的后果是 429 与封号。🔴 **抽取的三处已定稿,不许留给实现者临场决定**
(全部依据**修后蓝图 §6.5 与 §7 的 `api/_http.py` 函数面**,本文不重抄签名):
① **限速器签名定稿** `rate_acquire(bucket, key, buckets)` / `_is_persistent(bucket, buckets)` —— **登记表作参数传入**,各平台 `_client`
各留一个同名薄封装注入自己的 `_RATE_BUCKETS`。原稿"只改 import,沃尔玛侧调用形态零变化"与蓝图"两处 `_RATE_BUCKETS` 登记表互相点名"
**本是互斥的**:现状 `api/_client.py:257-269` 的 `rate_acquire(bucket, client_id)` 与 `:199-211` 的 `_is_persistent(bucket)` **都直接读
模块全局 `_RATE_BUCKETS`**(`:160-196`,未登记键 `raise KeyError`),调用点 **22 处跨 10 个 `api/*.py`**;表跟着搬 = 只剩一份表,改签名
= 22 处全动。薄封装这条第三路让**调用形态逐字不变、两份登记表各自独立、限速实现只有一份**。⚠ `KeyError` 文案里写死的"先在
`api/_client._RATE_BUCKETS` 登记"同批改成按传入表报名。
② 🔴 **`_is_persistent` 判据必须改,不能照搬**:现判据 `window >= 600.0 or limit <= 10`,**eBay 全部日窗口桶(86400s)必然命中** ⇒ 全部
走 `_acquire_pg`(`:222-256`:advisory 事务锁 + 24 小时窗 `count(*)` + 每次调用 INSERT 一行 + 顺手 DELETE 两天前)。以 Sell Inventory
定稿桶 **1,600,000/day** 计:`ops.rate_events` **单桶一天最多 160 万行**(清理阈值 2 天 ⇒ 常驻上限约 320 万行),**每一次 API 调用都要在
advisory 锁下数一遍这个窗口**,而该锁把同一应用身份的**全部 eBay 调用串行化** —— 与 `EBAY_ACCOUNT_WORKERS=4` 直接对冲,再加 **PG 挂 =
eBay 全停(fail hard)**。**定稿取蓝图 §6.5 的前者:判据改成「窗口 ≥600s **且** 上限 ≤1000」**(大桶回进程内、小桶仍进 PG);被否的
"日桶换成每日计数行 UPSERT"要动沃尔玛侧共用的 `_acquire_pg` 与 `rate_events` 写入形态 = 为 eBay 改沃尔玛生产链路,**留作备选并把理由抄进
函数头注**。⚠ **这条必须在批次 1 定死**,别拖到放量才发现。
③ **`socket.setdefaulttimeout(90)` 随 `api/_http.py` 一起搬到中立件模块顶层**(`api/_client.py:41` 现在的位置,注释 `:40` 自陈"import
本模块即影响进程内所有 socket"):纯 eBay 的链(`ebay_taxonomy_sync` / `ebay_account_health` / `ebay_catalog_sync`)**根本不 import
`api/_client`** ⇒ 那一行永不执行 ⇒ **纯 eBay 进程完全没有进程级兜底超时**。搬后两侧 `_client` 都 import 它、**值只有一处**,
`api/_client.py` 原处留一行指向注释。

### 2.2 workflows:`ebay_` 前缀平铺,cli.py 零改动

`cli.py` 是 `importlib.import_module(f"workflows.{args.workflow}")`、**无白名单** ⇒ 新增 `workflows/ebay_catalog_sync.py` 即刻可跑,锁(按
workflow 名取)、`ops.runs`、飞书通知、dry-run 注入全部直接继承。硬约束:只暴露 `run(params) -> str`;顶层 `DANGEROUS = True|False`;无 argparse /
flock / `ops.runs` / 通知 / `load_dotenv` / `basicConfig`;**`-p k=v` 传入的值永远是 str,自己 coerce;`execute` 与 `dry_run` 两个键
由 cli 注入且是 bool**(`cli.py:301` `params["execute"] = (not dry_run) if dangerous else True`、`:305`
`params.setdefault("dry_run", bool(dry_run))`)。⚠ **`DANGEROUS=False` 的工作流必须读
`params["dry_run"]` 而非 `execute`**——只读 execute 的话 `--dry-run` 对它完全无效**而且不报错**;dry-run 是**把要干的事逐行打出来**,不是"少干活"。摘要**第一行自成结论 + 最重要的那一个数**(链通知只显示第一行),排版用
`services/notify_fmt` 不另写,入库截 4000 字符;失败抛**多行 `RuntimeError`**(取最后一行的写法曾让一家店 400 时只发出一行 MDN 链接)。
docstring 第一行写成 `"""ebay_xxx — 一句话说清干什么(危险性)。"""`,它是 skill_export 技能包那一格的**唯一出处**,取不到就留白
(不编);参数白名单若做,`{"execute","dry_run"}` 必须放行。

### 2.3 `services/ebay_accounts.py`:仿 `stores.py` 三层判据

从第一天就拆开(沃尔玛侧是后来才把判据从 `_normalize` 循环里抠出来的,此前没有任何函数单独回答得了"在不在营"):`registered_names()`
答在不在**册**(只用于"查无此账号 vs 在册但停用"的报错分流,飞书失败直接抛)/ `enabled_names()` 答在不在**营**(判死账号、判占用、判
分配范围一律用它,**不兜快照**——兜底快照是过滤后的结果,拿它当在营名录会把"配好了但某项缺失的在营账号"算成停用,而这个名录直通整店
下线)/ `load_accounts(filter_names=None)` 答现在能不能**调 API**(回落快照 + warning)。照抄:`is_enabled` 独立谓词(⚠ **两条分支
一条都不许漏**,照 `services/stores.py:82-84`:先 `if v is False: return False` —— **飞书复选框字段返回的是 bool 不是字符串**,再判
字符串 `v.strip() in ("否","false","0")`;缺省视为启用)、`_cell()` 单元格归一(文本字段可能返回 `[{'text':...}]` 段列表)、
`filter_names` 落空**必抛**且分「不在册 / 在册但被过滤」两种文案、快照写完 `chmod 600`、每个函数 docstring 第一行"输入什么 → 输出什么"。

🔴 **三层判据的数据源在 eBay 侧必须显式分家(定稿,2026-08-25 校验轮点出的两处互斥)**:沃尔玛侧三层都只读飞书凭证表,照抄会把
`refresh_token` 写成飞书表一列 —— 而蓝图 §4.2 #5 已钉死 **`refresh_token` 只有一个出处 = `ops.ebay_tokens`,严禁进飞书凭证表**
(理由三条:同一凭证两个出处直接违反 §六#5「只有一个出处」;飞书侧账号表会随 `stores.py:240-250` 那类 `_write_snapshot` 落盘,而本表
**禁止进任何快照** = 同一份密钥两套保管纪律;18 个月的长期凭据进第三方 SaaS 多维表格,与"密钥只进 `.env`"的口径精神冲突)。⇒ 定稿:
- **飞书 eBay 账号表只放四类**:`账号`(名)、`启用`、**代理三件套**、`marketplace_id`。**没有任何令牌列。**
- **`enabled_names()` 只判「在册 ∧ 启用位」**,数据源纯飞书,与令牌无关。
- **`load_accounts()` 的技术就绪判据 = 库里有未过期 `refresh_token`**(`ops.ebay_tokens` 查 `refresh_expires_at > now()`)。
  ⚠ **这条判据是跨层读库的** —— `services` 读 `registry/db`,合法;但**照抄 `services/stores.py` 一定会写成读飞书**,故此处显式点名。
- 代理三件套任一缺 ⇒ `load_accounts` 跳过(判据 ④ 未拍板前从严),**但不影响 `enabled_names()`** —— 三层别混(CLAUDE.md 定稿 08-22)。

🔴 **账号名与店名互斥是硬约束,不是假设(2026-08-25 校验轮升级)**:`catalog.upc_pool` 的复用查询(`services/upc_pool.py:145-152`)与
`burn_for_retire`(`:194-200`)**都只按 `(store, asin)`、一个字都不看平台**;同一假设还压在 `ops.dispositions` 唯一索引
`(store, sku, action)`(`schema.sql:1148-1149`)、`ops.feed_log (feed_type, store, payload_key)`、`catalog.claims(store)`、
`orders.order_lines(store)` 上,而**全仓没有任何地方强制它**。店名与账号名都来自飞书自由文本(`registry/resources.py:983-997`
`STORE_CREDENTIALS.fields.store = "店铺"`)。⇒ **定稿:eBay 账号统一命名规则(如统一前缀 `EB-`),并在
`services/ebay_accounts._normalize` 里校验 —— `stores.registered_names() ∩ ebay 账号名集合` 非空即抛**(多行 `RuntimeError`,点名撞名的
那几个)。代价一条断言;不做的代价是**永久烧号且两侧都不报错**(沃尔玛一次 RETIRE 把 eBay 在用的 UPC 标成 `conflict`,而 conflict
是永久弃用,`upc_pool.py:16/:189-195`)。**写进批次 1 的单测与 §3.4。**

🔴 **这一处必须是 `registered_names()`(在不在册),不是 `enabled_names()`(在不在营)—— 全文四处断言统一按此写**:
被守的那五处(`catalog.upc_pool` / `ops.dispositions` / `ops.feed_log` / `catalog.claims` / `orders.order_lines`)
**全部按 `store` 字符串圈定,与启用位无关**;`services/stores.py:70-74` 的 docstring 逐字点名过这个坑
——「勾了停用的店照样占着品牌、照样用冻结行拦着别的店上架」,即**一家已停用的沃尔玛店名不在 `enabled_names()` 里,
却照样在这五张表里留着行**。拿 `enabled_names()` 断言,撞上这种停用店名会**当场放行**,而危害原样存在
(eBay 复用它在用的 UPC / 沃尔玛一次 RETIRE 把 eBay 在用的号烧成 `conflict` / 唯一索引照撞),
**而且两个方向仍旧都不报错**。三层分工见 CLAUDE.md 2026-08-22 定稿:`registered_names()` 答在不在**册**(连停用的都算)、
`enabled_names()` 答在不在**营**、`load_accounts()` 答现在能不能**调 API** —— 本条守的是**表级同源假设**,
只认最宽的那一层(在册)。⚠ 别把它与 §2.3 上面那条「`enabled_names()` 只判在册 ∧ 启用位」搞混:那条答的是"这账号还做不做",
本条答的是"这个名字有没有被沃尔玛侧占用过"。

| 项 | 沃尔玛现状 | eBay 差异落点 |
|---|---|---|
| 凭证 | `client_id`/`client_secret` 在飞书表,谓词判 client_id 非空非 `"0"` | App 级 id/secret **只进 `.env`**(§2.4 ⑦)+ 每账号 `refresh_token` **只进 `ops.ebay_tokens`**。⚠ **技术就绪判据 = 库里有未过期 refresh_token,不是判 client_id**:client_id 全账号共用,拿它判"这账号配好了没"**永远为真**。⚠ 飞书表里**没有任何令牌列**(见上方定稿) |
| 代理 | 每店固定出口代理,三件套任一缺即整店跳过(**生死线**) | 见判据 ④;若判不需要,那条过滤要**显式删掉并写明为什么**,不许留永远为真的空判 |
| 快照 | `paths.stores_snapshot_file()` | 新登记 `paths.ebay_accounts_snapshot_file()`。⚠ **绝不复用同一文件**:后写的整表覆盖前者 |
| 并发 | `STORE_WORKERS = 24` | ⚠ **不要照抄这个数字**。24 安全的前提是"每店自有代理 + 配额按 `(store, endpoint)` 计,店间不抢同一桶";**蓝图 §3.1/§6.5 已定稿令牌桶第一参数取「应用身份」(全账号共享一个桶)**⇒ 前提正好相反 ⇒ 另立 `EBAY_ACCOUNT_WORKERS`,**起步 4**,理由写进常量头注(⚠ 配额到底按 App 还是按卖家账号计**本身仍未核验**,蓝图 §8.3 #2;判错方向不对称,故取严) |

### 2.4 registry 登记先行(铁律 3)

写第一行业务代码前落**九类**登记:① `registry/platforms.py` 的 `WALMART`/`EBAY`/`PLATFORMS` 常量(**代码里禁止平台名字面量**,与
`product_events.EVENTS`、`listing_sources.SOURCE_*` 同款纪律);② **base URL 登记的不是 host,是「(族 × 环境) → host」**(⚠ **已按
修后蓝图 §4.4 定稿**:不是原稿说的"可能两个",是 **8 个 host 族** —— OAuth token `api.ebay.com/identity/v1/oauth2/token`、consent
`auth.ebay.com`、Sell Inventory/Account/Fulfillment `api.ebay.com`、**Finances `apiz.ebay.com`**(不是笔误)、Taxonomy、Post-Order
`/post-order/v2`、Developer Analytics、Trading `/ws/api.dll`;sandbox 通则 `api|auth → *.sandbox.*` 路径不变)。🔴 **环境不是一个 URL
变量能切的**(沃尔玛先例是单个 `WALMART_BASE_URL`,`registry/resources.py:21`)⇒ **registry 定稿两件**:`EBAY_ENV`(取值
`sandbox|production`,**缺省 `production`**,与"缺省即真跑"同款方向)+ **`base_url(family, env) -> str`** 的确切签名(蓝图 §7 已把它
写进 `_client` 函数面)。⚠ **`ops.ebay_tokens` 主键里的 `env` 取值必须与 `EBAY_ENV` 同源**(同一个 registry 常量集,不许在代码里另写
`"sandbox"` 字面量);⚠ 两 host 的等价性已降级为「文档级已证」(蓝图 §8.3 #8,两份 OAS3 都把两 host 登记为 Production server)⇒
上线前 sandbox 抽验即可,不再是阻塞项;③ **scope 字符串常量集**(蓝图 §4.3;⚠ **`api_scope/commerce.taxonomy` 这个 scope 不存在**,
写了就是错的);④ `EBAY_BULK_MAX = 25`(蓝图 §3.2 原文"必须登记进 registry,不许散落");⑤ **GTIN 站点专属替代文本表:进
registry 的是 21 行全表**(蓝图 §5.3 定稿 —— 官方 `product-identifier-text.html` 逐行列的是 21 个站点,**以官方页为准**;
蓝图正文只举例、不是清单);⚠ **Hong Kong 那条是中文串,本轮未逐字取到,建表时必须从官方页抄,不许自译**;🔴 代码里严禁写
`"Does not apply"` 英文字面量 —— 照残表建 registry,上 UK/AU/IE/SG 会命中 KeyError 或静默回落成 US 值,上 DE/FR 更会静默造出
不合规 listing;⑥ **错误码常量**(蓝图 §6.10:REST `429/2001/ACCESS/REQUEST`;**Inventory/上架 `190204`**(外链取图失败或非
HTTPS,批次 4b 验收 #6 要用);Fulfillment 34200/34300/34903/34905 + **`30830`**(`getOrders` 时间区间超 2 年,批次 6 关键点
要用);Trading `518`(⚠ 待复现)/21919144;**LMS `160025`**(⚠ 待复现;本项目不做 LMS,登记备查,任何设计不得依赖它));
⑦ **env 变量名(定名表,见下)**(A 类没有就抛 / B 类回落 None,真值只在 `<DATA_ROOT>/.env`);⑧ 飞书表条目 + 同步
`docs/feishu_tables.md`;⑨ 新路径以**函数**暴露。

**⑦ 的定名表(2026-08-25 定稿,原稿一个名字都没给 = 批次 1 当场卡住的那类洞)**。凭证形状按蓝图 §4.4:一个 eBay App =
{App ID(client_id)、Dev ID、Cert ID(client_secret)、RuName} **× 2 套(sandbox / production)**;Dev ID 只有 Trading 用,Sell REST
只需 client_id/client_secret;RuName 是 consent 的 `redirect_uri` 取值。

| 变量名 | 类 | 说明 |
|---|---|---|
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | A(缺即抛) | 生产 keyset 的 App ID / Cert ID |
| `EBAY_DEV_ID` | A | Trading(#44)专用;Sell REST 不用 |
| `EBAY_RUNAME` | A | consent 的 `redirect_uri` 填的就是它,**不是真 URL** |
| `EBAY_CLIENT_ID_SANDBOX` / `EBAY_CLIENT_SECRET_SANDBOX` / `EBAY_DEV_ID_SANDBOX` / `EBAY_RUNAME_SANDBOX` | B(回落 None) | 沙箱 keyset;**生产 keyset 在 sandbox 无效**,反之亦然 |
| `EBAY_ENV` | B(缺省 `production`) | `sandbox\|production`,喂 `base_url(family, env)` 与 `ops.ebay_tokens.env` |
| `FEISHU_EBAY_ACCOUNT_APP_TOKEN` / `FEISHU_EBAY_ACCOUNT_TABLE_ID` | A | eBay 账号表(启用 + 代理三件套 + marketplace,**无令牌列**) |

🔴 **`.env` 变量名在本仓的实际权威是 `workflows/init_data_root.py` 的 `_ENV_TEMPLATE`(`:14-83`,新机部署照它填)**,原稿两文档
**0 次提到它** ⇒ 不同批改它,新部署的 `.env` 模板里**不会出现任何 eBay 变量**,而这类缺失表现为"值明明该填却没人知道要填"。**批次 1 的
改动文件清单必须含 `workflows/init_data_root.py`**,上表逐行同步进 `_ENV_TEMPLATE`(照它现有的分段注释形态,含"沙箱才需要设"那种说明)。

### 2.5 services 直接复用清单

| 类 | 文件 | 理由 |
|---|---|---|
| 采集与数据源 | `product_ingest` `scrape_batches` `api/scraper` `amz_source` | 采的是亚马逊,与销售平台正交。⚠ **全项目只有一条采集入口**,eBay 不得另开;`amz_source` 产出统一「产品数据契约」dict,**eBay 上架链入参就该是它,不要另造形态** |
| 产品判定与变体 | `product_score` `brand_key` `catpath` `category_blacklist` `audit_phase0` `audit_stopwords` `variant_group` `variant_remap` `variant_title` | 信号全来自亚马逊源侧。变体三件实为中立、**只是名字骗人**(`pick_walmart_dims` 的 enum 由调用方注入)——改名 `pick_target_dims` + `MAX_LEN` 收参数即转正 |
| 台账骨架 | `dispositions`(状态机 / 按 action 领 / 破坏组压制 / `sources` 分格)、`product_events`(只追加 + 事件码唯一注册表) | **08-19 生产事故换来的纪律,必须继承同一套,不许重写一份** |
| 闸门·分配·店铺算法 | `blacklist.load_banned_asins`、`risk_gate` 品牌闸、`alloc_groups`、`alloc_engine` 发牌、`alloc_survey` 纯函数判定、`store_limits`「读不到 vs 填了 0 必须分开」、`store_targets` 三态空值、`store_perf` 收缩与中位数 | ⚠ **一期只复用「闸 + 店铺算法」,分配链本身一期不做**(见 §七 #9:账号少,`ebay_list_new` 直接走 `product_pool` 选品;多账号分配链与 `claims` 集成列二期)。**黑名单绝不分平台**(品牌在沃尔玛因知产被拉黑,拿去 eBay 同样是知产问题);发牌认 store/asin 两个身份键 + **四闸 `brand`/`category`/**`lead`**/`channel`**(`services/alloc_engine.py:66` `_GATES`,原稿写"五个键"漏了 **lead** —— 配送时长上限,`store_limits.lead_day_caps()` + `amz_source.MAX_LEAD_DAYS = 7`,eBay 侧对位的是 **handling time**,是**必须分平台**的一格,与 `store_limits` 同批处理);三条店铺算法是所有者反复定稿的资产,只有列名与数据源要分平台。🔴 **两处按蓝图 §8.4 修正**:① `store_limits` 那条「读不到就回落默认值」的语义在 eBay 侧**必须反转成 fail-closed** —— selling limits 是**平台强制硬上限**,读不到还照上 = 直接撞 id=4232 的"绕限制"违规;② **跨店排他单位是 identical item(官方 13 属性判据)不是品牌** —— 别把 `brand_key` 的品牌级占用照搬到 eBay(既过严又拦不住真违规:无货源商品大量 Generic/OEM,品牌为空时一条都拦不住),**沃尔玛侧的品牌占用不要动**,catalog 层需同时承载两套排他 |
| 基础设施 | `runlock` `db_guard` `notify_fmt` `textfmt` `llm_cache` `llm_cost` `launchd` `gpt_skill` `api/feishu` `api/llm` `api/llm_vision` | 与卖到哪个平台无关,零改动 |

### 2.6 services 必须另立清单(含灰色件拆法)

拆法一律:**两个显式函数 + 调用方显式 if 路由**。任何"沃尔玛路径失败自动落 eBay"或按 SKU 格式猜平台的写法都是重复提交制造机。

| 沃尔玛件 | eBay 对应物 | 拆法 / 差异要点 |
|---|---|---|
| `walmart_catalog` / `api/_client` | **`services/ebay_catalog.py`** / `api/_http.py` + `api/ebay/_client.py` | `ebay_catalog` 是整条链的地基,**先建它**,四条语义原样继承(§3.2);`_client` 拆法见 §2.1,限速桶与连接池只准一份中立实现 |
| `pricing` / `mp_mapper` | 抽 `landed_price`/`parse_multiplier` 中立 + 新增 `ebay_price(...)`;抽 **`services/listing_copy.py`**(`scrub_brand`/`_clean_copy`/`sort_images`/`title_spec_compatible`)+ `ebay_item.py` | ⚠ **不要**给 `walmart_price` 加 platform 参数——那会让两套倍率表在一个函数里分叉;`pick_band` 改收 `bands`。`listing_copy` 是**最高性价比的一次抽取**:文案与图片处理约占 mp_mapper 一半且平台无关。⚠ **eBay 的成本口径与沃尔玛不同**(蓝图 §5.5/§8.4 #3):费率不是常量而是「店铺当前等级 × 类目服务指标」的**函数**(TRS+ −10%、Below Standard +6/7%、INAD Very High +5/6%、国际 +1.65%,最坏叠加有效抽成 >30%),且**计费基数含运费含税**(按裸价算每单系统性少算约 1.1 个百分点,**而且两侧都不报错**);刊登费是**真实固定成本**(非订阅仅 250 条/月免费,超出 $0.35/条),沃尔玛侧无此项 ⇒ **"能上就上"的铺货策略不能沿用** |
| `audit_l2` `audit_l3` `audit_l4` `audit_models` | `audit_l2_common`(R4/R5/R7/R8/R10)+ `_walmart`(R1/R3)+ `_ebay`;L3/L4 引擎中立、政策语料与 system prompt 分平台;`audit_models` 字段改中立名 + 加 `platform` | 执行模型(100 起分、六条固定顺序全跑不短路、下界 -1000)共用。⚠ L4 平台名做成**显式必传不给默认值**,否则会有一天用沃尔玛立场审 eBay 的图**且不报错**;前缀缓存按 system 逐字节命中 ⇒ 两份 system 各一条缓存链,不互相打散。⚠ `audit_models` 改名是**全仓改名**,🔴 **实测规模是 26 个文件约 140 处**(2026-08-25 校验轮:排除同名 DB 列 `audit.walmart_category_map.walmart_product_type` 后,非测试 18 个 —— `audit_l1_llm` 19 处、`pt_census` 14、`audit_rules` 8、`audit_l2` 8、`catmap_export` 6、`catmap_prune` 5、`audit_l4` 5、`audit_l3` 4、`alloc_survey` 4、`catmap_suggest` 4…;测试 8 个 —— `test_audit_rules_wiring` 34、`test_audit_l1_llm` 9、`test_pt_census` 8),**原稿"六个文件"低估约 4 倍**。**越早改越便宜**,且**不能塞在原批次 4 里当顺手抽取**(9 人日的批次会被它吃穿)⇒ **并入批次 2a**(纯改名 + 全绿回归,与那批的全仓谓词补全同类,可与 2b 并行) |
| `audit_l1_llm` `audit_reason` `audit_rules` `pt_admission` `pt_spec` `mp_conform` | `audit_l1_ebay` / `audit_reason_ebay` / `audit_rules_ebay` / `ebay_admission` / `ebay_aspects` / `ebay_conform` | ⚠ 形态不同别硬套:沃尔玛 spec 是本地大文件按 PT 拆分,eBay aspects 是**整站叶子类目一次性下载 + 落库缓存**(蓝图 §1 #11),刷新口径是**比对 `categoryTreeVersion`、变了才拉**而不是"缓存 N 小时"(§8.2 #7);单类目补漏用 #12 但**禁止逐 SKU 调**(5,000/day 会见底)。🔴 判必填只准读 `aspectRequired` 布尔,**绝不能读 `aspectUsage`**(官方自陈必填项在那里返回的是 `RECOMMENDED`,读错会**漏掉全部必填项**,§5.2 #3) |
| `maintenance_intents` / `product_pool` `risk_gate` `alloc_survey` `store_targets` `store_limits` | 抽 `maint_rules.py`(纯判定收两个 dict)中立,取数与载荷按平台各一份;候选池 SQL / 闸数据 / 限额表字段常量按平台各一条 | ⚠ 20 小时抑制的 `ops.dedupe` scope 串**必须带平台**,否则两平台互相压制对方的意图;算法函数一律保持一份 |
| `listing_sheet` `maint_sheet` `clear_sheet` `feed_track` `order_center` `kpi` `problem_products` `order_lines` | 各建 eBay 对应物 | ⚠ **别在同一张飞书表里加 eBay 列**——列序即契约,混平台会让"表头改名静默弄坏"的老坑翻倍 |
| `sku_asin` `spec_split` / `cleanup_history` `yingdao` | 前两个:`is_standard_asin()` 中立、numeric 倒查按平台注入 lookup、`spec_split` 路径常量做参数 / 后两个:**不建** | 纯数字 SKU 分支现在写死"倒查 `catalog.walmart_items`";一次性迁移件与沃尔玛卖家页 RPA 不跨平台 |

## 三、库改动定稿

**主力手法**:`platform text NOT NULL DEFAULT 'walmart'` 加列——存量行自动正确、现有 INSERT(列清单显式)不改就继续写 walmart、回滚只需
DROP COLUMN。⚠ **坏处也在这里**:eBay 写入方忘传 platform 就**静默落成 walmart**;对策是写入一律走 services,签名里 `platform` **必填**,
值不在 registry 常量集就抛。⚠ **加列本身零破坏**(已核,⚠ **计数与口径按 2026-08-25 校验轮修正**:`.py` 里 `SELECT *` 是 **6 处**不是
5 处 —— `services/dispositions.py:704` 与 `workflows/catmap_suggest.py:202` **只是给人读的 SQL 字符串**、`workflows/order_audit.py:234`
与 `:967` 是 `SELECT * FROM unnest(...)`、`workflows/scrape_missing.py:67` 与 `workflows/catmap_gap.py:74` 在 CTE 内部;**要加列的那四张
表上一处都没有**,结论不变。⚠ 口径要说全:**视图也算** —— `refdata/schema.sql:117` 的 `catalog.latest_snapshot` 就是
`SELECT DISTINCT ON (…) *` 打在共享表 `catalog.snapshots` 上,而 `snapshots` **零改动**,故同样不受影响),成本全在"eBay 行进表之后"的
谓词补全 ⇒ **加列与放 eBay 行必须分两批**:先加列 + 补谓词(库里仍只有 walmart 行,行数与 EXPLAIN 可逐条对拍),再放 eBay 写入。

### 3.1 共享表逐张取向

| 表 | 取向 | 要点 |
|---|---|---|
| `products`/`snapshots`/`latest_snapshot` | **纯读,零改动** | ⚠ `audit_status`/`audit_reason`/`walmart_pt`/`pt_source`/`audit_version`/`audited_at` 六列是**沃尔玛口径**,一行装不下两个平台的判决 → 见判据 ⑤,并在 schema.sql 注释里把六列正式改称"沃尔玛审核结论"(纯注释改动,防下一个 AI 写错地方) |
| `catalog.product_events` | 加 `platform` + **5 视图 DROP 重建 + 读 SQL 补谓词(⚠ 清单见 §五 批次 2a,**按表分栏**;原稿那句"12 处"把 `listing_sources` 的消费方混了进来,且漏了 `workflows/cleanup_history_import.py:63` 的 `_WIPE_SQL`(`DELETE FROM catalog.product_events WHERE source = %s`,另 `:11` 链路注释)与 `services/dispositions.py:152-154` 的 `_SETTLE_DELETE_SQL` —— **动手前照 2a 那句"现场重数一遍"执行**)** | 🔴 最高危。`_VERIFY_SQL` 的 `LEFT JOIN walmart_items` 对 eBay 行必然 `w.sku IS NULL` ⇒ 判 `gone` ⇒ **凭空落 `delete_verified`**,绕过"不信回执信观测";视图 `product_risk` 按 `coalesce(asin,sku)` 全局聚合,eBay 的 missing 会并进同一 ASIN 的 `unexplained_missing`(`list_new` 正消费它报警) |
| `catalog.listing_sources` | 加 `platform`(**标注列,不进 PK** —— 依据见右)+ **五处代码消费 SQL 带谓词 + 一处 schema 存量回填显式补 `'walmart'`** | `source_type`(amz/match/self/1688)答"产品出身",**与销售平台正交**。⚠ **校验轮点出的自相矛盾已裁定**:该表 `PRIMARY KEY (store, sku)`(`schema.sql:213-221`),`register()` 是 `ON CONFLICT (store, sku) DO NOTHING`(`services/listing_sources.py:36-42`,首次登记优先不覆盖)⇒ platform 不进 PK 时,**同名 (账号, sku) 的 eBay 行会被静默丢弃并继承沃尔玛行的 `source_type`**,而 `source_type` 正是"自动破坏动作路由铁律"的判据(头注 `:9-12`)—— 这恰恰就是在靠账号名不重名侥幸。**二选一取后者:保持标注列 + 显式互斥校验**,依据是 §2.3 已把"账号名与店名互斥"**从假设升成硬约束**(命名规则 + `_normalize` 里 `stores.registered_names() ∩ ebay 账号名 ≠ ∅ 即抛` + 批次 1 单测),该约束同时保护 `upc_pool`/`dispositions`/`feed_log`/`claims`/`order_lines` 五处同源假设 —— 把 platform 塞进这一张表的 PK 只堵一个洞、还要改 `ON CONFLICT` 推断子句与 `schema.sql:225-231` 的回填 INSERT,**性价比反而低**。🔴 **"一条断言守五处"能成立的前提是那条断言按「在册」判**:五处(含本表 `(store, sku)` 主键)全按 `store` 字符串圈定、与启用位无关,停用店名仍在表里留行 ⇒ 断言必须用 `registered_names()`;若写成 `enabled_names()`,撞上停用店名会放行,本格"不进 PK"的全部依据当场落空(§2.3)。⚠ 回填那条(`FROM catalog.walmart_items … ON CONFLICT (store, sku) DO NOTHING`)**每次 `db_init` 都跑**,必须显式补 `'walmart'`。消费方清单:`services/maintenance_intents.py` 四处 JOIN(`:79/:122/:170/:489`)+ `workflows/sources_backfill.py:51` 的 `NOT EXISTS` + schema 回填那条 |
| `catalog.claims` / `ops.dispositions` | **同型改造**:加列 + 唯一索引换名替换 + `ON CONFLICT` 推断子句同批改(claims 详见 §3.3) | 🔴 claims 不改就**拦死 eBay**。⚠ dispositions 的动作在键里**不能去掉**(顽固件 retire+delete 双 feed 齐发);⚠ **压制判定(`claim()` 内)必须同时按平台圈定**,否则 eBay 一条 delete 会压制沃尔玛同 SKU 的维护建议。⚠ **eBay 侧要新增第四种 action**(蓝图 §8.4 #4):因**资格未获批**而被下架的商品官方明文**禁止 relist**,现有 retire/delete/relist 三值表达不了这一态;而 VeRO 只是新 `source`,**不新开执行出口**(08-24「只看 action 不看 source」定稿在 eBay 侧完整成立) |
| `seller_blacklist`/`amazon_cat_blacklist` 纯读零改动;`asin_blacklist`/`brand_blacklist`/`brand_err_hits` | **eBay 只读做闸,不写渠道表** | 类别码 A~L 由**沃尔玛报错文本**解析而来,一行装不下两个平台的理由;eBay 下架原因先落 `product_events`,归拢另议 |
| `ops.runs`/`cursors`/`dedupe`/`feishu_sync_state`/`scrape_batches`/`rate_events` | **共写,零 DDL 改动** | 键天然带命名空间(workflow 名 / `'ebay_order_sync:<账号>'` / scope 前缀)。⚠ 但 `rate_events` 的 **bucket 名必须带平台前缀**(如 `ebay.inventory.bulk_create`):两套客户端写同一张表,撞名即互扣配额 |
| `ops.store_kpi_daily`/`orders.perf_events` | **不并轨**,另建 eBay 表 | 32 列逐列对齐沃尔玛口径(otd/vtr/srr/payout);`perf_event_spans.still_active` 判据是"是否还出现在最新一期报表里",硬塞会污染 |
| `ops.feed_log`/`feed_items`/`feed_item_errors` | ✅ **已冻结:复用**(蓝图 §5.4) | 端点形状查清了:eBay 是**同步 REST + 逐条部分成功 + 没有整批 id**,但结论**不是**"另设计",而是**同表同语义、粒度从「批」降到「单 SKU」**:🔴 防重键按**单 SKU 载荷指纹**算(批量端点的 25 只是传输优化,**不是防重单位**,这是与沃尔玛最大的语义差)。🔴 **`feed_id` 不改名(2026-08-25 校验轮定稿,推翻原稿"泛化为 `submission_id`")**:那是**全域列改名**不是一句话 —— `grep -rn "feed_id" --include=*.py services workflows api` = **183 行 / 17 个文件**(`api/feeds.py` 41、`services/feed_track.py` 32、`workflows/feed_poll.py` 17、`workflows/maintenance.py` 13、`workflows/sku_locked_heal.py` 15…),`refdata/schema.sql` 里 11 处(其中 `ops.feed_items` 主键就是 `PRIMARY KEY (feed_id, sku)`、`ops.dispositions.feed_id`、`ops.feed_item_errors`、视图 `catalog.feed_failures` 与 `ops.v_feed_error_stats`);而 `grep -n "RENAME" refdata/schema.sql` **零输出** —— **全仓没有列改名先例**,`ALTER TABLE … RENAME COLUMN` **不幂等**,`db_init` 第二次跑必炸(正是 §3.6 想防的 2026-08-13 那类),且直接违反 §2「现有沃尔玛文件一律不动」。⇒ **只加 `platform`,列名 `feed_id` 保持不动** —— **列名不是语义承诺**;eBay 侧的分阶段语义(上架存 `offerId` → publish 后存 `listingId` → 发货存 `fulfillmentId`,**存最新一个**)写进 **`docs/db_schema.md` 与 `services/feed_track.py` 头注**,由文档承载而不是由列名承载。⚠ 若所有者坚持改名:**单独立一批**,用 `DO $$ + information_schema.columns` 守卫包住 RENAME,验收补「沃尔玛 feed 链改前后 `ops.feed_items` `count(*)` 与最近 7 天摘要逐字一致」。反查反而更干净——`getOffer`/`getOffers?sku=` **精确点查**,沃尔玛那套"同尺寸兄弟切片会撞指纹"的排除逻辑不需要。⚠ `ebay_submit_poll` **不是轮询器**(eBay 没有 INPROGRESS 态可轮),是"台账落定器 + 启动对账",pending 行只可能来自崩溃/网络异常。🔴 **"同表同语义"不等于"零 DDL":`ops.feed_items` 现有形态装不下单 SKU 粒度,四条必须在批次 4a 开工前定死**(校验轮点出,原稿只写"复用"就过去了):① 提交前落 pending 时**还没有任何 id**,而 `feed_items` 主键 `(feed_id, sku)` 的 `feed_id text NOT NULL`(`schema.sql:832-847`)**成不了行** —— 沃尔玛是靠 `feed_log.feed_id` **可空**承接 pending、item 级台账**提交成功后才落**;② `submission_id` "分阶段存最新一个" = **改主键值**,同一行做不到;③ 一行一 SKU 后 `feed_log` 与 `feed_items` 变 1:1,两表分工消失;④ 防重唯一索引 `feed_log_dedupe_uidx (feed_type, store, payload_key)`(`schema.sql:829`)加 `platform` 后**必须按 §3.6 范例 B 换新名重建**。**定稿见 §五 批次 4a** |
| `ops.ebay_tokens` | **新建**(蓝图 §4.2 第 5 条;**DDL 草案见本表下方**,归**批次 1**建) | 令牌**落库共享,不放进程内存**:本仓一天几十条 workflow 各自启停,令牌只活在进程内 ⇒ 刷新次数 = 进程数 × 账号数,而 `client_credentials` 只有 **1,000/day 且 App 级共享,一炸全账号全链条一起炸**。🔴 **刷新响应不含新的 refresh_token**,严禁 `row.refresh_token = resp.get("refresh_token")`(`.get()` 返回 None 会把好 token 洗成空;eBay 的 refresh token **不轮转**,写一次管 18 个月)。真密钥 client_id/secret/RuName 仍**只进 `.env`**;`refresh_token` **只在本表**(§2.3 定稿,严禁进飞书凭证表)。🔴 **本表的三个泄漏孔必须与建表同批堵上,见下方"凭证泄漏三孔"** |
| `audit.walmart_*` **五表**(⚠ 原稿写"四表",实为五张,逐一点名:`walmart_category_map` `schema.sql:1183`、`walmart_error_records` `:1258`、`walmart_pt_meta` `:1387`、`walmart_pt_spec` `:1437`、`walmart_prohibited_policy` `:1452`) | **eBay 全不用** | `audit.amazon_taxonomy`(`:1308`)/ **`audit.amazon_node_paths`**(`:1336`,⚠ 原稿写成 `audit.node_paths`,**表名错**,全文已改)/ `audit.category_path_alias`(`:1357`)是亚马逊侧类目树,eBay 建映射时**直接复用左手边**(要的是 `amazon node → eBay categoryId` 新表,见 §五 批次 2b 与判据 ⑨) |

**`ops.ebay_tokens` DDL 草案(批次 1 建,范例 A)**:

```sql
CREATE TABLE IF NOT EXISTS ops.ebay_tokens (
    account            text NOT NULL,          -- 应用令牌行填 '_app'(不与任何真账号名撞)
    env                text NOT NULL,          -- sandbox / production;取值来源 = registry 的 EBAY_ENV 常量集(§2.4 ②)
    token_kind         text NOT NULL,          -- app / user
    scopes             text NOT NULL,          -- 排序后空格连接;🔴 app 令牌按 scope 集合做缓存 key 的那一半(蓝图 §4.2 #3)
    access_token       text,
    access_expires_at  timestamptz,            -- 提前 300s 续(蓝图 §4.2 #1)
    refresh_token      text,                   -- 🔴 只有 user 行有;刷新响应不含它 ⇒ 永不回写(蓝图 §4.2 #2)
    refresh_expires_at timestamptz,            -- 🔴 到期预警的唯一依据(约 18 个月);ebay_account_health 读它
    obtained_at        timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, env, token_kind, scopes));
-- ⚠ 本表 = 运行态凭据,**禁止进任何导出/快照**(见"凭证泄漏三孔");单飞靠 SELECT … FOR UPDATE(蓝图 §4.2 #3)
```
⚠ 主键把 `scopes` 也纳入,是因为**应用令牌按 scope 集合铸**(key 不含 scope 会拿窄 scope 令牌调宽 scope 端点拿 403,蓝图 §4.2 #3);
user 行 scope 集合固定 ⇒ 天然只有一行,不受影响。

🔴 **凭证泄漏三孔(2026-08-25 校验轮补,原稿两文档"备份/backup"命中 0 次)—— 与建表同批(批次 1)堵上,并写进 §六**:
① **全库备份**:`workflows/backup.py:71-73` 是**全库 `pg_dump --format=custom`**,产物落 `<DATA_ROOT>/backups` 保留 14 天,
`registry/schedule.py` 里**每日 02:00 跑** ⇒ 每天一份含 18 个月长期凭证的 dump。**改法:`pg_dump` 加 `--exclude-table-data=ops.ebay_tokens`**
(**只排数据不排结构**),并在 `backup.py` 头注与 `docs/db_schema.md` 同时写明 🔴 **"从备份恢复后 eBay 令牌为空,必须重走一次 consent
(`workflows/ebay_authorize.py`)才能起 eBay 链"** —— 不写这句,恢复当天会表现为"所有 eBay 链 401 而没人知道为什么"。
② **readonly 角色**:`workflows/db_init.py:30-33` 的 `GRANT SELECT ON ALL TABLES IN SCHEMA … ops … TO readonly` 会把本表**自动授给
readonly**,而 readonly 是给 Metabase/NocoDB/MCP 用的 ⇒ `db_init` 在该 GRANT 之后**对本表显式 `REVOKE SELECT … FROM readonly`**
(⚠ 同时要覆盖 `ALTER DEFAULT PRIVILEGES` 那条的后续影响,REVOKE 写在建表与 GRANT 之后)。
③ **文档同步**:`docs/db_schema.md` 本表条目 + `workflows/backup.py` 头注 + `services/` 侧读令牌的函数 docstring **三处**都写明该表
禁止进任何导出/快照/日志(令牌值**不许进 `ops.runs.summary`,也不许进异常文案**)。

### 3.2 `catalog.ebay_items` 草案(镜像 `walmart_items` 的角色)

原则:**逐列对位** `walmart_items`,让两条链 SQL 形状一致(维护链/日报/分配链的查询才好照抄)。

```sql
CREATE TABLE IF NOT EXISTS catalog.ebay_items (
    account text NOT NULL, sku text NOT NULL,          -- 对位 (store, sku)
    marketplace_id text, offer_id text,                -- offer_id = 改价/改库存/下架的抓手(对位 wpid)
    listing_id text,                                   -- eBay Item ID(对位 item_id;ended 后重上必换号)
    epid text, gtin text, upc text, mpn text, brand text,      -- ⚠ upc 必须 text:前导零
    title text, category_id text, category_path text,         -- 对位 product_name/product_type/shelf
    condition_id text, listing_format text, price numeric, currency text, avail_qty integer,
    listing_status text, offer_status text, status_reasons text,  -- 对位 published_status/lifecycle_status(schema.sql:143-144)、unpublished_reasons
    variant_group_id text, variant_group_info jsonb, policies jsonb,
    last_seen_at timestamptz NOT NULL, missing_since timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, sku));      -- 索引:sku / listing_id 对位 walmart_items 的两条(schema.sql:152/:157);
                                      -- offer_id 那条是**新增**,理由:offer_id 是改价/改库存/下架的抓手,
                                      -- 反查台账(getOffer)按它点查 —— 不是"逐列对位"的产物,标出来别当对位漏项
```
**四条必须原样继承的语义**:① 本轮没拿到库存 → `COALESCE(EXCLUDED.x, 旧值)`,**不刷 NULL**;② 缺席**只标不删,永不清理**(`WHERE
last_seen_at < run_at AND missing_since IS NULL`,首次缺席时间不被刷新;所有者 2026-08-12 拍板保守留行);③ 标缺席时**同步清空状态列**
(旧观测在商品消失后保留会误导);④ 缺席后复现 → **平台数字 ID 重置为 NULL 触发重查**。事件账本与 upsert **同事务**落;eBay 事件码必须
先登记进 `EVENTS`(未登记码 `record_many` 抛错),写入带 `platform='ebay'`。
🔴 **蓝图 §5.2 第 1 条给本表加了一条硬要求**:`createOrReplaceInventoryItem` 是**全量覆盖不是 PATCH**(官方原文:更新时该条目当前
已定义的全部字段都必须重给)⇒ **本表必须存全量字段,每次 PUT 从投影表整体重放,禁止"只改变的字段"** —— 这与沃尔玛 feed 的增量语义
**相反**,是最容易翻车的地方。⚠ **原稿那"三格"的现状**(逐条对修后蓝图 §8.3):(a) `listing_status`/`offer_status` 的**取值封闭集仍未
取到** —— ⚠ **引用已改挂:是 §8.3 #18(2026-08-25 新开),不是 #9**(#9 的清单是 `orderPaymentStatus`/`reasonForRefund`/`refundStatus`/
`TransactionTypeEnum`/`PayoutStatusEnum`/`TransactionStatusEnum`/`condition`,**根本不含这两项** ⇒ 挂在 #9 上等于"§8.3 逐条补核"那道闸
**放不住它**,故蓝图单列了 #18)⇒ **沙箱实调 `getOffer` 看真实取值、补齐后**才许把字面量写进 SQL(沃尔玛侧 `'PUBLISHED'` 这类字面量散在
约 10 处 SQL,定错要返工);⚠ 顺带:`ebay_submit_poll` 的 FOUND 判据就是"`status=PUBLISHED` 或有 `listingId`",取值集不封闭**这条判据也
写不死**;(b) **一个 SKU 多个 Offer 已确认**(Offer 是 sku×marketplaceId×format 维度,蓝图 §5.1)——但**本期只做
EBAY_US 单站点单格式**(见 §七),一行成立,子表 `catalog.ebay_offers` **备着不建**;要上多站点/拍卖时照抄 `multi_node_plan` §5:
合计/代表值留主表(现有消费方零改动),明细进子表;(c) `epid` 大概率大面积 NULL,**不许拿它当身份键**。

### 3.3 `catalog.claims`:现状会跨平台拦死 eBay

现状 `claims_active_uniq (kind, claim_key) WHERE status='active'` = 一个品牌/ASIN **全局**只能一条 active。沃尔玛在架商品的品牌与 ASIN
已被 `alloc_backfill` 倒推占满,且占用**没有自动释放**(只有 `store_release`)⇒ eBay 账号 `try_claim` 必撞 DO NOTHING、被当成"顺延次优
店",**几乎拿不到任何货,而且是永久的**。改造(DDL 与代码**同批**):新索引 `claims_active_platform_uniq (platform, kind, claim_key)
WHERE status='active'` + `ALTER … ADD COLUMN IF NOT EXISTS platform` + `DROP INDEX claims_active_uniq` + `services/claims.py` **六条**
SQL 与 `_row()` 校验全改(⚠ 原稿写"五条",实为六条,逐一点名:`_INSERT` `:24`、`_OWNER` `:34`、`_LOAD` `:39`、`_RELEASE` `:44`、
`_PREVIEW` `:54`、`_BY_STORE` `:63`)。⚠ 三个坑一个都不能少:① **必须原地替换 schema.sql 里的旧 CREATE INDEX 行**,只追加 DROP 会被下次 `db_init`
全文重跑建回来(顺序对了侥幸没事,错一次就恢复成跨平台拦死);② **新索引必须换新名字**——`CREATE UNIQUE INDEX IF NOT EXISTS` **只按名字
判存在**,沿用旧名会让那行变成静默 no-op;③ **最阴的一条是 dict 塌陷,而且它有两处不是一处**:`_LOAD`/`load_active`(`:39`/`:106` 一带)
用 `dict(cur.fetchall())` 压成 `{键:店}`,同品牌两平台各一条 active 时 **dict 静默只留后一条,谁赢取决于行序**;⚠ **`_OWNER`/`owner_of`
(`:34`/`:118-124)完全同款** —— 同样 `SELECT claim_key, store … WHERE status='active'` + 同样 `return dict(cur.fetchall())`,而
`owner_of` 是 `try_claim` **"顺延次优店"那条路径的输入**,漏改的后果与 `_LOAD` 等价。**两处必须并列点名、并列补测**(批次 2a 验收 #1)。
而 `_INSERT` 的 `ON CONFLICT` 推断子句漏改反而**会 fail loud**(PG 直接报错),是好事。

### 3.4 `catalog.upc_pool`:跨平台复用语义

结构事实:**主键是 `upc`,领用信息(store/sku/asin/claimed_at/used_at)是主表裸列 ⇒ 一个 UPC 只能记录一个使用者**,"同产品双平台复用同一
UPC"**结构上装不下**。**S2(推荐起步,零结构改动)**:两平台各领各号——复用键 `(store, asin)` 天然按账号隔离,eBay 账号名 ≠ 沃尔玛店名 ⇒
`claim()` 走"新领"分支;DDL 0(可选加**纯标注列** `platform` 供池子统计与飞书投影分平台展示),代码 0;意外收益:`burn_for_retire` 按
store 过滤 ⇒ 沃尔玛 RETIRE 烧号**天然不会误烧 eBay 的号**;⚠ 代价:**池子消耗翻倍**,"余量不足"告警的注入节奏要跟上,**写进上线检查单**。
🔴 **S2 的"零改动"全部压在「eBay 账号名 ≠ 沃尔玛店名」这一条上,而它在仓库里零强制**:`services/upc_pool.py:145-152` 的复用查询是
`ON store = t.s AND asin = t.a WHERE status IN ('claimed','used')`、`:194-200` 的 `burn_for_retire` 同样只按 `(store, asin)`,**两处都不看
平台**;店名与账号名都是飞书自由文本。同名一旦发生**两个方向都不报错**:eBay 上架直接复用沃尔玛该 ASIN 的在用 UPC;沃尔玛一次 RETIRE 把
eBay 正在用的号标成 `conflict`,而 **conflict 是永久弃用**(`upc_pool.py:16`、`:189-195`)。⇒ **该假设已在 §2.3 升级为硬约束**(eBay 账号
统一命名规则 + `services/ebay_accounts._normalize` 里 `stores.registered_names() ∩ ebay 账号名 ≠ ∅ 即抛` + 批次 1 单测;
⚠ **是 `registered_names()` 不是 `enabled_names()`** —— `upc_pool` 的两条 SQL 按 `store` 圈定不看启用位,
停用店名照样在池子里留着领用行,理由见 §2.3)。**S2 依然是零 DDL
零代码,但它的前提现在有人守了。** ⚠ 另补一条 S2 的代价:**`listing_sheet.heal_unknown` 的 `catalog.upc_pool` 写路径必须带平台条件**
—— 它是那份反哺器名单里**唯一写 UPC 池**的那个(`workflows/feed_poll.py:60-63` 注释自陈"只写飞书与 UPC 池"),见 §五 批次 4a。
**S1(语义正确,要拆表)**:新建 `catalog.upc_assignments (upc, platform, store, asin, sku, status, …, PK (upc, platform, store))`,
`upc_pool.status` 退化为**池位**状态;改动横跨领号事务、烧号、飞书投影三处,且 🔴 `burn_for_retire` 不加平台过滤会把 **eBay 正在用的 UPC
标成 conflict,而 conflict 是永久弃用** ⇒ **备着 DDL,等"存量告急"或所有者要求"一个产品一个码"时单独立批**。两条路共同的生死规则:
**同一个 UPC 绝不能同时被两个平台的在途上架领走**;**Unknown 永不回收**。

### 3.5 `orders` 域:加 `platform` 列,不建 `orders.ebay_order_lines`

**理由一**:身份模型本就平台无关——`order_line_id = 'ol_' + sha256(po+\x1f+sku)` 那段注释论证的是"PO 是**平台**发的、全局唯一;店铺名是我方
标签所以不进身份",把"沃尔玛"换成"eBay"逐字成立。**理由二**:建新表要复制的不是一张表而是一个域——下游 3 张附属表 + 2 视图 + ≥12 处读侧全挂
在 `order_line_id`/`po_id` 上,复制一份就是明禁的"双轨"。**理由三**:列适配度高,取值集不同的只有 `sale_status`,而它本就是 text。DDL:
`order_lines`/`return_lines`/`settlement_lines` 各加 `platform`,`order_lines` 加 `(platform, store, order_date DESC)` 索引。🔴 **唯一真
风险:哈希不含平台。** 若某 eBay orderId 恰好等于某沃尔玛 PO,两条订单算出同一个 `order_line_id`,upsert 直接覆盖**而且两侧都不报错**。
**不要动哈希**——v2→v3 改哈希的代价记录在案(DROP 重建三张表 + `UPDATE perf_events SET order_line_id = NULL`),改一次 = 整个订单域重来。
**推荐做法(便宜且 fail loud)**:加**平台一致性守卫**——目标行已存在且 `platform` 与来源不同 ⇒ **抛错,不覆盖**。
⚠ **机制必须明写,不能只写"加个守卫"(2026-08-25 校验轮:原稿那句把既有 `guards` 的性质说反了)**:`services/order_lines.py:382-418` 的
`guards` 参数是 **`ON CONFLICT DO UPDATE` 里的 SQL 表达式替换**(docstring `:398-402`),两个现存实例 `_ASIN_GUARD =
"COALESCE(EXCLUDED.asin, t.asin)"`(`:375`)与 `_PHONE_GUARD`(`:377-379`)**恰恰都是静默保留旧值的兜底,一个都不会抛错**;而 `_upsert`
走 `cur.executemany`(`:417`),在 SQL 里 fail loud 只能靠触发器或 `CASE … 1/0` 这类脏写法。⇒ **定稿机制:预查询函数,不走 guards** ——
`upsert_order_lines` 在 `executemany` **之前**先
`SELECT order_line_id, platform FROM orders.order_lines WHERE order_line_id = ANY(%s)`,发现 `platform` 与来源不同即抛**多行
`RuntimeError`**(与 `_missing_msg` 同款分类文案,逐条列出撞上的 `order_line_id`/两边平台),**写成独立函数并配单测**(批次 6 验收 #1
已有断言,现在有了实现载体)。⚠ 现实概率极低(沃尔玛 PO 约 13 位纯数字,eBay orderId 带连字符),**但未实测,存疑**。

🔴 **蓝图 §8.2 第 4 条推翻了本节"eBay 也拿 SKU 做身份"这半,必须连带改一处 DDL**:eBay 的**合并订单(Combined Invoice)真实存在**
⇒ 订单行唯一键必须是 `(orderId, lineItemId)`,**不能是 `(orderId, sku)`**。而现表有一条**表级**约束 `UNIQUE (po_id, sku)`
(`refdata/schema.sql:651`),`_upsert` 的 `ON CONFLICT` 却打在主键 `order_line_id` 上(`services/order_lines.py:456`)⇒ **同一 eBay
订单里同 SKU 的两个 lineItem 会算出两个不同的 `order_line_id`、却撞同一条 `UNIQUE (po_id, sku)`;这个冲突不被 `ON CONFLICT` 接住,
整个 `executemany` 事务中止,那一轮订单同步全炸。** 而"合并成一行"这条退路也不通:发货回传要**按 lineItemId 逐行给数量**,合并后拿不回 lineItemId
⇒ **标发货必失败**。⚠ **这句的出处要说准**:蓝图 §1 #31 只写了"一包裹一次、发运后不可增删行 + 回读防重",
`createShippingFulfillment` 的**入参形态属蓝图 §8.3 #15 的未核验项**(carrier + trackingNumber 是否"可选但必须成对"、
`lineItems` 怎么给)⇒ **此处论据暂以保守假设成立,补核 #15 后确认**;两个方向代价不对称(假设错了只是多留两条索引,
假设漏了则整轮订单同步炸),故仍按它排。**推荐**:eBay 行的身份哈希第二段喂 **lineItemId** 而非 SKU(**两个显式函数,
platform 不给默认值**),并把表级 `UNIQUE (po_id, sku)` 换成**两条按平台的部分唯一索引**(walmart:`(po_id, sku) WHERE
platform='walmart'`;ebay:`(po_id, line_number) WHERE platform='ebay'`),照抄 §3.6 范例 B 的 `DO $$` + information_schema 守卫
—— **沃尔玛侧语义逐字不变,且不动哈希**。

🔴 **换约束的三条写法必须逐字照做(2026-08-25 校验轮补;原稿两处硬事实都没提到,而第二条会静默删掉整个订单域)**:
1. **旧的是表级约束不是索引** —— `refdata/schema.sql:651` 的 `UNIQUE (po_id, sku)` 写在 `CREATE TABLE` 里,PG 自动命名
   **`order_lines_po_id_sku_key`**,只能 `ALTER TABLE orders.order_lines DROP CONSTRAINT IF EXISTS order_lines_po_id_sku_key`;
   **`DROP INDEX` 对它无效**(会静默找不到、旧排他继续拦着 eBay 行)。同时**原地删掉 `CREATE TABLE` 里那一行**,否则下次 `db_init`
   在新库上又建回来(与 §3.3 claims 的第①个坑同源)。
2. 🔴 **两条新排他必须是显式命名的 `CREATE UNIQUE INDEX`(如 `order_lines_wm_po_sku_uidx` / `order_lines_ebay_po_line_uidx`),
   严禁 `ADD CONSTRAINT UNIQUE`** —— `refdata/schema.sql:607-618` 有一条**每次 `db_init` 都执行**的 v2→v3 一次性守卫,判据是
   `constraint_name = 'order_lines_po_id_line_number_key'`,命中即
   `DROP VIEW orders.perf_event_spans, orders.settlement_by_line; DROP TABLE orders.order_lines, orders.return_lines,
   orders.settlement_lines; UPDATE orders.perf_events SET order_line_id = NULL;`。而本方案给 eBay 定的键**正是 `(po_id, line_number)`**
   ⇒ 写成 `ADD CONSTRAINT UNIQUE (po_id, line_number)` 时 **PG 自动命名恰好就是那个名字**,**下一次 `db_init` 静默删掉整个订单域**
   (eBay 行进表之后,连 eBay 订单一起删)。显式命名的 `CREATE UNIQUE INDEX` 不进 `constraint_column_usage` 的这条判据,天然避开。
3. **验收补一格**:批次 6 的 `db_init` 幂等两跑后**`orders.order_lines` 行数不变** —— **专验第 2 条那个守卫没被误触发**
   (只验"零报错"抓不住它:守卫是静默 DROP,第二次跑照样"成功")。
⚠ **顺带给那条 v2 守卫加一层保护(建议,批次 6 同批)**:在它的 `IF EXISTS(…)` 里再 `AND NOT EXISTS (SELECT 1 FROM
orders.order_lines WHERE platform = 'ebay')` —— 语义是"**库里已有 eBay 行就拒绝执行**"。理由:那条守卫写于只有沃尔玛的年代,
爆炸半径被默认成"窗口重拉即回"(注释 `:605-607` 自陈),而 eBay 行进表后**这个前提不再成立**。

⚠ "同一 eBay 订单会不会真出现同 SKU 两行"**未实测**,但两个方向代价不对称(不做 = 整轮
订单同步炸;做了 = 多两条索引)⇒ 取严,**列为判据 ⑧,批次 6 的前置**。

### 3.6 schema.sql 改法:照抄既有范例,不自创

| 范例 | 用在哪 | 要点 |
|---|---|---|
| **A · 原地改 CREATE + 追加 ALTER** | `platform` 加列全部照抄 | **两处都写**:CREATE 里一份给新库、`ALTER … IF NOT EXISTS` 一份给已部署库。⚠ **加列的 ALTER 必须排在依赖该列的索引/视图之前**(2026-08-06 生产实证:先建索引会 UndefinedColumn) |
| **B · `DO $$` + information_schema 守卫** | 换唯一索引/换表级约束(claims / dispositions / `order_lines`) | 🔴 **形态照仓库真有的那条写,不许自创**(⚠ 原稿"嵌套 IF 是幂等的关键、平铺 `AND` 重跑必炸"**在仓库里查无此物**:`grep -n "DO \$\$" refdata/schema.sql` 只有三处 `:66`/`:608`/`:1299`,**三处全是单层 `IF (…) THEN … END IF;`,没有任何嵌套 IF** ⇒ 照"范例 B"去找会找不到样板,照描述写会自创一套本仓从未验证过的形态,故删除该表述)。**真正的范例形态**:<br>`DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.<视图> WHERE table_schema='…' AND table_name='…' AND constraint_name='…'/column_name='…') THEN <破坏动作>; END IF; END $$;`<br>**要点是判据必须精确到 schema + table + 列名/约束名**,以免误伤新表(依据 `schema.sql:1296-1298` 的生产实测注释);幂等来自"守卫命中一次之后条件不再成立",不来自 IF 的层数。⚠ 与之配套的两条实操教训仍然有效:`CREATE UNIQUE INDEX IF NOT EXISTS` **只按名字判存在**(换索引必须换新名)、**原地替换旧 `CREATE INDEX` 行**(只追加 DROP 会被下次 `db_init` 全文重跑建回来) |
| **C · DROP VIEW + CREATE VIEW**;**D · 存量回填靠 WHERE 保证幂等** | C:视图加列的**唯一**做法(PG 的 REPLACE 不允许改列名);D:给存量行打标记 | ⚠ **新建的任何 eBay 视图禁止建立在别的视图之上**——依赖它就等于给 db_init 埋一个"第二次跑就报错"的雷。D 在本方案里几乎用不上:`NOT NULL DEFAULT 'walmart'` 让存量行**自动正确**,理想情况下**一条回填都不用写** |

## 四、待所有者拍板的判据清单

> **✅ 批次 0 已拍板(2026-08-30,所有者)**,结果与落地设计收录在
> `docs/ebay_phase1_design.md` §0(一期实现以该文为准):
> ① **不跨平台**(每平台唯一;所有者当日先拍"跨"后更正为"不跨",与推荐
> 一致)/② **各自与亚马逊保持同步**/③ **UPC 跨平台复用**(激活 §3.4 的
> S1 拆表,S2 作废)/④ 每账号固定出口代理必做(账号数量待填实值)/
> ⑤ **一期复用沃尔玛 `audit_status`**(方向 A)/⑥ **无货源模式书面确认
> 要做**/⑦ 已解释,一期不阻塞、二期订单链与提额前必须落地(批次 11 排期
> 待拍)/⑧ **暂定**(订单链不在一期)/⑨ **LLM 做映射、只能测试做**
> (人工放行后上架链才消费)。
> 以下原文保留作拍板依据存档;"拍板前不许按推荐值编码"对 ①~⑥⑨ 已解除。

每条给**推荐默认值**与**两个方向各自的后果**;拍板前不许按推荐值编码。

**① 品牌/产品冻结是否跨平台?推荐:不跨(`claims` 加 platform 维度)。** 线索:占用的业务目的写的是"归属是决策""店铺终止运营 → 释放",没一句
提到防平台关联;而关联封号风险是**出口 IP 维度**,不是品牌维度。判成跨平台唯一 ⇒ **eBay 几乎拿不到任何货**,且占用不自动释放 ⇒ 永久,更糟的是
**它不报错**,表现为"eBay 侧天天跑、天天拿不到货"(⚠ **措辞按 §七 #9 更正**:一期没有分配链,`ebay_list_new` 直接走
`product_pool` 选品;但 `claims` 的跨平台 active 行**照样会被读到**,所以本判据一期就要拍);判成每平台唯一而业务实际要全公司唯一 ⇒ 同一品牌被两平台同时铺货,品牌方一封信同时打
到两条链,**而占用台账显示两边都"合法持有"**。

**② 双平台库存口径?推荐:各自限额 + 共享观测,且先定防超卖规则再上链。** 亚马逊侧观测(`snapshots.stock_count`)两平台共读,可售数量由各平台
自己的限额表决定。先上链后定规则 ⇒ 同一批货被两平台各按"全量可售"挂出去,超卖的第一现场是买家订单,补救成本是两边绩效指标同时掉;定成"共享一个
总量池" ⇒ 需要跨平台实时扣减器,而两条链是**不同进程、不同调度节奏**,扣减器本身成为新单点,⚠ 这条一旦选,`ops.dedupe` 式轻量防重挡不住,要设计
真正的库存账,**本期不建议做**。⚠ 两个方向都照抄 `NULL ≠ 0` 铁律:下游禁止 `or 0`。

**③ UPC 跨平台复用?推荐:同产品同 UPC(语义正确),落地先走 S2 各领各号;领用前先查该 ASIN 是否已领。** 即接口语义上认"一个产品一个码",结构上
等 S1 拆表后才真正兑现。现在就做 S1 ⇒ 改动横跨领号事务、烧号、飞书投影,且触及一条生死规则(Unknown 永不回收)与一条生产实证(SKU 在沃尔玛端绑死
首次提交的 UPC,换号重发必败 `ERR_EXT_DATA_0101211`),风险高收益低;一直停在 S2 ⇒ 池子消耗翻倍(注入节奏要跟上),GS1 语义上"一个产品两个码"不
干净,⚠ **eBay 是否会因两平台不同 UPC 触发目录匹配问题,未核,存疑**。

**④ eBay 账号数量与每账号固定出口代理?推荐:与沃尔玛同级——每账号固定出口代理,严禁直连,缺代理即整账号跳过。** 这是本仓唯一被写成"业务生死线"
的安全铁律,eBay 侧是否有同等封号机制**未核**。判"不需要"而实际需要 ⇒ 多账号从同一出口 IP 调用,**关联封号**,且不是渐进的,一次性打掉一批;判
"需要"而实际不需要 ⇒ 多付代理钱、多一层故障点(代理挂 = 该账号整轮跳过)。**两个方向代价不对称,所以默认取严。** ⚠ 若最终判不需要,`_normalize`
那条代理过滤必须**显式删掉并写明为什么**——留个永远为真的空判会让后来者以为这条防线还在。⚠ 同批要定**账号数量与并发常量**(见 §2.3 表末行)。

**⑤ 审核链平台化?推荐:方向 A —— 一期直接复用沃尔玛的 `products.audit_status` 当保守闸;平台化列二期。** `products.audit_status` 单列装的是
**沃尔玛政策结论**(37 条 Prohibited Products Policy + PT 准入 + 中国搬运卖家可做性),eBay 违禁规则是另一套(VeRO、Restricted Items、
类目 required aspects)—— 两套判决迟早要分家,判的只是**一期用哪个闸**。
- **方向 A(推荐值):一期 `ebay_list_new` 直接读沃尔玛的 `audit_status`,只上"沃尔玛已通过"的品。**
  **代价**:品类被沃尔玛政策**白白砍掉一截** —— eBay 能卖的很多品沃尔玛本来就不让卖,这部分一期一条都上不了。
  **好处**:**零新审核工程**(不用建闸、不用人工名单),且方向是 fail-safe(漏放风险低,错的方向是"少上"不是"上错");
  与**已接线的两处一致** —— 批次 4b 的入料是 `product_pool` 选品 + 四闸(隐含吃沃尔玛审核结论)、批次 10 把
  `ebay_list_new` 排在 20:30(`audit_sheet` 18:10 → `list_new` 20:00 之后),两处都建在这个方向上。
- **方向 B:一期另立人工/简化闸,`ebay_list_new` 不读 `audit_status`。**
  **代价**:要**新建一道闸**(人工名单或简化规则),有人维护、有人填,且一期就多一份人工负担;闸没建好之前上架链没有准入判据。
  **好处**:**品类面不受沃尔玛政策连坐** —— 沃尔玛不让卖但 eBay 允许的品一期就能上。
  ⚠ 选它必须连带改两处已接线的地方:批次 4b 的入料判据、批次 10 的 20:30 排期理由(那个时刻的全部依据就是"吃同一天的审核结论")。

🔴 **两个方向共同的明禁:一期都不许把 eBay 判决写进 `products.audit_status`。** 一个 ASIN 一行**装不下两个判决**,后写覆盖先写,
`audit_listing_conflicts` 的 `rejected_after_listing` 判错,且**账混了就分不开** —— 审核结论不是账本,没有 platform 列可以事后补,
**不许"先混着跑跑看"**。二期落法(与推荐值无关,两个方向都走它):eBay 判决落 `audit` schema 新表(如 `audit.ebay_verdicts`),
`products` 六列**只归沃尔玛**并在 schema.sql 注释里钉死。

**⑥ 🔴 无货源(从亚马逊下单直发买家)这条业务模式,做还是不做?没有推荐值,只有所有者书面拍板。**(**本条为蓝图定稿后补入**,依据
蓝图头 blockquote —— 原稿四节写于蓝图之前,漏了它,而它是全链最硬的一条。)本项目「亚马逊采集 → eBay 上架 → 出单后从亚马逊下单
直发买家」**逐字命中** eBay 明文禁令:"Listing an item on eBay and then purchasing the item from another retailer or marketplace
that ships directly to your customer is not allowed on eBay." [verified drop-shipping id=4176],且与多账号连坐条款 [id=4232]
相乘 —— **一店受限,关联店可能同批受限**。判"照做" ⇒ 爆炸半径是**全部关联账号一起**,不是渐进的;判"换货源合规化"(自有库存 /
供应商直发协议)⇒ 上架链的技术面**不变**(蓝图端点面在两条出路下都成立),变的是选品与履约。⚠ **本条不阻塞任何一行代码**,只阻塞
"起调度真发":批次 1-9 照做、`--dry-run` 与沙箱冒烟不受限,**批次 10 的第一步就是核对本条已书面拍板**。

**⑦ `MARKETPLACE_ACCOUNT_DELETION` 合规入站面怎么落地?推荐:单独最小 webhook,不与任何业务链耦合,立为批次 11。**
✅ **"要不要做"已不是判据(蓝图 §8.3 #12 于 2026-08-25 关闭)**:官方是**二选一** —— 订阅通知,**或**走 opt-out 流程;而 opt-out 的门槛
逐字是 "For any developer application that is **not persisting any eBay data**, there is an option to opt out",而**本项目把订单/买家/
catalog 全量落 PostgreSQL ⇒ opt-out 不适用 ⇒ 只剩订阅这一条路**。不合规的后果逐字:"Failure to comply ... will result in termination
of your access to the Developer Tools, and/or reduced access to all or some APIs."(⚠ 原稿"是否强制、有无替代路径未核验"与"申请豁免"这条
退路**都已作废,不要再按它排期**。)⇒ **剩下的判据只有"怎么建、谁维护"**:它需要一个**公网入站 HTTPS 端点**,而本项目是"脚本 +
launchd + PostgreSQL",**没有任何常驻公网服务**,这是全项目唯一的入站面,**等于新增一类运行形态**,不该藏在一条判据里 ⇒ **已立
§五 批次 11**(选型 + 每日订阅状态自检巡检 + 工时)。塞进业务链 ⇒ 业务链挂了合规订阅跟着 `MARKED_DOWN`
(⚠ **`MARKED_DOWN` 这个状态本轮未核验** —— `getDestinations` 方法页是 SPA,取不到枚举,蓝图 §8.1 ② 同款标注;
结论不靠它,但**告警判据不许直接照它写死**),而**丢事件是静默的**;
不建 ⇒ Growth Check 过不去,配额与账号数**上不去**,且面临上面那句"终止访问"。端点四条硬要求(https、路径不得含内网 IP/localhost、
`challengeCode` hash 回 200、通知即时 200/201/202/204)与配额桶见蓝图 §2 末格。

**⑧ 订单行身份键:eBay 用 `(orderId, lineItemId)` 还是照沃尔玛用 `(po, sku)`?推荐:lineItemId(理由与 DDL 见 §3.5 末段)。**
判成 sku ⇒ 合并订单同 SKU 两行撞 `UNIQUE (po_id, sku)`,**整轮订单同步事务中止**;而合并成一行则拿不回 lineItemId,**标发货必失败**。
判成 lineItemId ⇒ 要换掉一条表级唯一约束(可回滚,范例 B 守卫),沃尔玛侧语义逐字不变。⚠ 触发条件未实测,**取严**。

**⑨ 「亚马逊 node → eBay categoryId」映射:一期手填还是立链?推荐:一期所有者手填定死类目集,链化列二期。**(**本条为 2026-08-25
校验轮补入**:原稿批次 2b 建了 `audit.ebay_category_map`、批次 4(现 4b)的 `createOffer` 要读 `categoryId`,**中间没有任何工作流去填这张表**;
沃尔玛侧同一件事是**九条工作流** `workflows/catmap_*.py` + 一份 CLAUDE.md 点名的唯一文档 `docs/category_mapping.md`,而两份 eBay 文档里
"catmap / 类目映射"命中 **0 次**。)判"一期手填"⇒ 上架品类被限死在手填的那几个类目里,但**表有人填、批次 4b 能跑**,且避开
`getCategorySuggestions` 那条"**官方明示 sandbox 不支持、且 sandbox 是假成功返回样板 `categoryName`**"(蓝图 §1 #13)的坑 —— 链化后
连冒烟都只能挪到生产试点;判"一期就链化"⇒ 至少 import/suggest/promote 三条工作流 **4~6 人日**,压在已经最长的关键路径上,且
`suggest` 的结果**是建议不是判据**,仍要人工定稿,一期收益极低。**一期落法(写进 §七 #11 与批次 2b)**:`audit.ebay_category_map` 由
所有者**手填**,给出**填表规范**(每行 = `amazon_node_id` + `amazon_node_path`(取自 `audit.amazon_node_paths`,人读校对用)+
`ebay_category_id` + `ebay_category_path` + `marketplace_id` + `confirmed_by` + `confirmed_at`;**一个 amazon node 只准一条生效行**)
与 **registry 字段常量**(飞书投影那份的表头只准引常量,不许写字面量);`ebay_admission` 读不到映射的 ASIN **一律拦下并计数**
(fail-closed,**不许回落到"猜一个类目"** —— 类目错了 aspects 必填集就错,publish 被拒还算好的,更糟的是上成一个错类目的活 listing)。

## 五、开发计划

**总原则四条**:① **批次 0 不通过,后面一条都不许起调度**(判据 ⑥ 是全链阻塞,不是某一个批次的阻塞);② **每批次自带验收,验收
不过不进下一批** —— 验收里"`--dry-run` 人眼确认"这一格**任何写类批次都不许省**(2026-08-16 定稿后默认值不再替你挡,所以更要自觉);
③ **加列与放 eBay 行分两批**(§3 开头那条安全序),库改动的两小批之间可以插任意只读开发;
④ 🔴 **每批验收分「开发段」与「试点段」两截,别混(2026-08-25 校验轮解开的一处循环依赖)**:原稿给批次 6/7/8/9 的验收里写着"拉一个
真实窗口 / 核对一次真实放款 / 先只放 title 观察一天",而**真实订单与真实放款要先有真实在架 listing ⇒ 要判据 ⑥ 拍板 + 批次 10 起调度**;
可总原则②又说"验收不过不进下一批",批次 10 还要求"全部批次完成" ⇒ **这几批永远拿不到自己的验收**。⇒ 定稿:**开发段验收 = sandbox
冒烟 + 单测 + `--dry-run` 人眼确认**(过了就算这批过,可进下一批);**试点段验收全部统一挪进批次 10 的放量阶梯**,在那里按链逐条打勾。
工作量按**一个熟悉本仓的人**估,单位人日,**不含判据拍板的等待时长与试点观察期**;**合计 ≈ 64.5 人日 + 2 周试点观察**。
⚠ **合计从原稿的 45 涨到 64.5,分两块,逐条可追**:(a) **重估**:批次 4 按沃尔玛对位实现的实测体量反推(`list_new.py` 1467 +
`mp_mapper` 702 + `mp_conform` 924 + `listing_sheet` 565 + `feed_track` 330 + `upc_pool` 261 ≈ **4250 行**,而批次 1 对位的
`_client` 615 + `stores` 258 行给的是 6 人日)从 9 上调到 **16**(拆 4a/4b,落在校验轮给的 15~18 区间内),批次 2a 因收进
`audit_models` 全仓改名 +1.5;(b) **把六个原本无人认领的交付物显式化**:`workflows/ebay_authorize.py` + runbook(批次 3)、
`ebay_ship_confirm` 与 `ebay_order_audit`(批次 6)、KPI 底座(批次 9)、`tests/test_launchd.py` 与 runner/时刻定稿(批次 10)、
**批次 11 合规入站面**。**这不是工作量膨胀,是把黑洞标进预算** —— 原稿的 45 人日买不到一条能起调度的链。
⚠ 批次号是**依赖顺序不是优先级**(与 `schedule.JOBS` 的 batch 同款约定)。

### 5.0 批次总览

| # | 批次 | 阻塞判据 | 人日 | 可与谁并行 |
|---|---|---|---|---|
| 0 | 判据拍板(无代码) | — | 0.5 | — |
| 1 | registry 登记(含 `.env` 定名表 + `init_data_root`)+ `services/ebay_accounts` + `api/_http` 抽取 + `api/ebay/_client` + `ops.ebay_tokens` 建表与凭证三孔 | ④ | 6 | 2a / 2b |
| 2a | 共享表加 `platform` + 谓词补全 + 视图重建(**库里仍只有 walmart 行**)+ **`audit_models` 全仓改名** | ①(claims/dispositions 那半) | 4.5 | 1 |
| 2b | 新建 `catalog.ebay_items` / **`catalog.ebay_accounts`** / `audit.ebay_category_map` / `audit.ebay_aspects_cache` + 事件码登记 | ⑨(只挡映射表的填表规范那半) | 2.5 | 1 / 2a |
| 3 | 户口链 + 类目链 + **账号巡检** + **`ebay_authorize` 兑换链与 runbook**(四条) | ④ | 5 | 2a |
| 4a | 中立抽取(`listing_copy`/`pricing`/变体三件)+ **台账平台化**(feed 三表加 `platform`、`feed_track` 按平台过滤、四个 `sync_from_ledger` + `heal_unknown` 同改) | — | 6 | 3(改的不是同一批文件) |
| 4b | 最小上架闭环 `ebay_list_new` + `ebay_submit_poll` + `ebay_*` 六件 services | ①②③⑤⑨(⑥ 只挡真发) | 10 | — |
| 5 | 回读 `ebay_catalog_sync` | — | 3 | 6 |
| 6 | 订单链 `ebay_order_sync` + **`ebay_ship_confirm`** + **`ebay_order_audit`** | ⑧ | 7 | 5 |
| 7 | 维护链 `ebay_maintenance` + **`ebay_problem_cleanup`** | ①②⑤ | 5 | 8 |
| 8 | 售后 `ebay_returns_sync` + **`ebay_returns_action`** | — | 4 | 7 |
| 9 | 结算 `ebay_settlement_sync` + **KPI 底座 `ebay_kpi`** | — | 5 | 7 / 8 |
| 10 | 调度上线(`schedule.JOBS` + `test_launchd` 同批改 + `skill_export` 再生成 + **全部试点段验收** → 放量) | **⑥⑦ + 全部** | 3 | — |
| 11 | **合规入站面**(`MARKETPLACE_ACCOUNT_DELETION` 最小 webhook + 每日订阅自检) | ⑦ | 3 | 任意(不碰本仓任何既有文件) |

**关键路径(按各批次自己声明的前置真算,不按批次号,也不把并行批次串起来加)**:
`0 → 1 → 3 → 4b → 6 → 9 → 10` = **36.5 人日**(0.5 + 6 + 5 + 10 + 7 + 5 + 3)。**口径 = 各批次前置依赖的最长链**,三条算法说明:
① **4a 不在链上** —— 它的前置只有批次 2a(见 4a 段首),总览表"可与谁并行"那格写的就是"3(改的不是同一批文件)"⇒
2a(0.5+4.5=5.0 完工)→ 4a(11.0 完工)这一支比 `1 → 3`(11.5 完工)短,**4b 的开工时刻由批次 3 决定,不由 4a 决定**;
② **批次 3 的前置是 1 + 2b**,而 2b 只 2.5 人日、与 1 并行 ⇒ 3 的开工时刻由批次 1 决定;
③ **批次 6 之后取 9 不取 8** —— 9 的前置就是批次 6(见 9 段首"**前置**:批次 6")且 5 人日 > 批次 8 的 4 人日,
两支都挂在 6 后面,最长的那支才是关键路径。⚠ **上一版那条把 4a 串进链里、又在 6 之后取批次 8 的算式,连同它的"次长支"一并删除**
—— 把文档自己在总览表里声明可并行的两批相加、再在分支处取短的那支,**方向对但算法与自身依赖声明打架**;更早的一版则是漏了
批次 10 阻塞列自己写的"全部"。**本节只留这一个口径,别再并列第二条路径。⚠ 后来的人要改批次工时或前置,必须回来重算这条链。**
**总工时 64.5 人日不变**(总览表 14 行相加),关键路径变短只是因为并行度算对了,不是工作量变少了。
**其余可并行**:2a/2b 与 1 并行;**批次 3 不再与 2b 并行**(见下条);4a 与 3 并行;9 与 7/8 并行;
**批次 11 与任何批次并行,但必须在批次 10 之前完成**(它是 Growth Check 门槛)。
🔴 **批次 3 的前置是「批次 1 + 批次 2b」,不是只有批次 1**(原稿漏):`ebay_taxonomy_sync` 要把整站 aspects **全量落库**进
`audit.ebay_aspects_cache`、`services/ebay_aspects.py` 也读它,而这张表在 2b 建;户口链四件产物的落点表 `catalog.ebay_accounts`
同样在 2b 建。总览表"可与谁并行"那格已把 2b 从批次 3 一行删掉。
**对给定骨架的调整,逐条给理由**(依据修后蓝图 §2「工作流清单 9 → **13** 条业务链」;⚠ 蓝图 §2 矩阵另有**第 14 行
`ebay_authorize`** —— 一次性人工触发、不进 `schedule.JOBS`,不计进这 13 条,见批次 3):批次 3 多一条 `ebay_account_health`、批次 6 多一条
**`ebay_ship_confirm`**(蓝图 §2 新增的第 13 条)、批次 7 拆出 `ebay_problem_cleanup`、批次 8 拆出 `ebay_returns_action` ——
理由分别写在各批次段首,**不是为了多写文件,是既有铁律与端点生产者缺口的落点**。

### 批次 0|判据拍板(0.5 人日,无代码)

**目标**:把 §四 ①~⑨ 逐条送到所有者面前,拍完写回 §四并标日期。**拍板前不许按推荐值编码**(§四 开头那句)。

| 判据 | 阻塞哪些批次 | 不拍板就动手的后果 |
|---|---|---|
| ① 品牌/产品冻结跨不跨平台 | 2a(索引换名那半)、4b、7 | eBay 侧**天天跑、天天拿不到货,而且不报错**;占用不自动释放 ⇒ 永久 |
| ② 双平台库存口径 | 4b、7 | 同批货被两平台各按"全量可售"挂出,**超卖的第一现场是买家订单**,两边绩效同时掉 |
| ③ UPC 跨平台复用 | 4b | S2→S1 返工要停上架链、动领号事务与烧号,且触及"Unknown 永不回收"生死规则 |
| ④ 每账号固定出口代理 + 账号数量/并发常量 | 1、3 | 多账号同出口 IP ⇒ **关联封号,一次性打掉一批** |
| ⑤ 审核链平台化 | 4b、7 | 判决写进 `products.audit_status` 后**账混了就分不开**(没有 platform 列可以事后补) |
| ⑥ 🔴 无货源红线 | **只阻塞"起调度真发"**(10);1-9、11 照做 | 违规被抓的爆炸半径 = 全部关联账号 |
| ⑦ 合规入站面**怎么建**(要不要建已定:必须订阅) | 11(它本身)、10(上生产门槛) | Growth Check 过不去 ⇒ 配额与账号数上不去,且官方原文写着"终止 Developer Tools 访问" |
| ⑧ 订单行身份键 | 6(它决定 DDL) | 合并订单同 SKU 两行 ⇒ **整轮订单同步事务中止** |
| ⑨ 类目映射一期手填 vs 立链 | 2b(填表规范那半)、4b(`createOffer` 要 `categoryId`) | 表建了没人填 ⇒ **批次 4b 的 offer 组装当场没有 `categoryId` 可用**;或反过来临时"猜一个类目",aspects 必填集随之全错 |

**验收**:一份带日期的拍板留痕写回 §四(照 CLAUDE.md 里"所有者定稿 2026-08-xx"的既有写法);⑥ 必须是**书面**的。

### 批次 1|registry 登记 + 账号积木 + eBay 唯一出口(6 人日)

**目标**:让 `api/ebay/_client.py` 拿着某个账号的凭证、按蓝图 §4.4 的族路由发出第一个 200。

**前置**:判据 ④ ——它**阻塞 `_normalize` 第三条(代理)过滤的存废与 `EBAY_ACCOUNT_WORKERS` 取值**;判据 ⑦ 不阻塞本批。
`ops.ebay_tokens` 是 `_client` 的自用表、与共享表改造无关,**归本批建**(不等 2b)。

**改动文件清单**:新增 `registry/platforms.py`;改 `registry/resources.py`(§2.4 九类登记里的 ②~⑥ + **`EBAY_ENV` 与
`base_url(family, env)`** + eBay 账号表 Bitable 条目)、`registry/paths.py`(`ebay_accounts_snapshot_file()`,⚠ **绝不复用**
`stores_snapshot_file()`)、**`workflows/init_data_root.py`(`_ENV_TEMPLATE` 同步 §2.4 ⑦ 的定名表 —— 不改它,新机部署的 `.env`
模板里不会出现任何 eBay 变量)**、`refdata/schema.sql`(`ops.ebay_tokens` DDL 草案见 §3.1,范例 A;**db_init 侧对该表显式
`REVOKE SELECT … FROM readonly`**)、**`workflows/backup.py`(`pg_dump` 加 `--exclude-table-data=ops.ebay_tokens` + 头注写明
"恢复后须重走 consent")**;⚠ **`refdata/ebay_rate_limits.tsv` 已入仓**(2026-08-25 落仓,**75 行 6 列 + 22 行头注**,原稿写的
"已备在 scratchpad / 尚未入仓"作废)—— 本批**只引用不改**,**不要改 tsv 去迁就蓝图的定稿值**(tsv 记官方原值,蓝图 §3.1 记桶配置,
🔴 **写 registry 取蓝图的定稿列**);新增 `api/_http.py`(**函数面与签名以修后蓝图 §7 的 `api/_http.py` 段为唯一出处,本文不重抄**:
`socket.setdefaulttimeout(90)` 模块顶层 / `_get_client` / `_build_transport` / `_invalidate_client` / `_close_all_clients` /
`_parse_retry_after` / **`rate_acquire(bucket, key, buckets)`** / **`_is_persistent(bucket, buckets)`** / `download_bytes`)、改
`api/_client.py`(**留同名薄封装注入自己的 `_RATE_BUCKETS` ⇒ 22 处调用点一行不改**;`socket.setdefaulttimeout` 原处只留指向注释)、
改 `api/__init__.py:6-10` 人读索引(**顺手补进已漂掉的 `settings.py`/`llm.py`/`llm_vision.py`**);新增 `api/ebay/__init__.py` +
`api/ebay/_client.py`、`services/ebay_accounts.py`;新增 `tests/test_ebay_client.py`、`tests/test_ebay_accounts.py`;同步
`docs/db_schema.md`(`ops.ebay_tokens`,标注**禁止进任何导出/快照**并写明备份排除与 readonly REVOKE)与 `docs/feishu_tables.md`。

**关键函数面**:蓝图 §7 的 `api/_http.py` 与 `api/ebay/_client.py` 两段签名 + §6 十一项横切能力,**不重抄**。五条不许漏:
`get_user_token` **绝不回写 refresh_token**(§4.2 #2,`_client` 里留显式注释);**`exchange_code(account, code)` 是全仓唯一一处
`authorization_code` grant**(§4.2 #7,职责只有"换 + 返回",落库与打印归调用方 —— 调用方是批次 3 的 `workflows/ebay_authorize.py`);
`_RATE_BUCKETS` 桶名带 `ebay.` 前缀,且与 `api/_client.py` 的登记表**头注互相点名**(§6.5,否则下次扩桶只改一处),⚠ `KeyError`
文案同批改成按传入表报名;**`_is_persistent` 判据改「窗口 ≥600s **且** 上限 ≤1000」**(§6.5 定稿,理由与被否的备选一并抄进函数头注
—— 见 §2.1 ②,**这一格必须在本批定死**);`EbayAccountDeadError` 的状态码**一开始就枚举**(§6.6,含 token 端点 400/401/**403**;
别等踩到 —— 沃尔玛侧那次是授权失败回 400 不回 401,导致"一家店凭证坏掉判整轮失败"),🔴 **配套的 workflow 侧闸「零账号完成即判失败」
同批立为纪律**(§六 #8;只抄状态码枚举是抄了一半 —— 请求形状被改坏时表现是**全账号一起 dead**,没有这道闸整条链会**每天空转而且
报成功**)。`ops.ebay_tokens` 的 DDL 见 §3.1。

**验收**:
1. **单测**:`ebay_accounts` 三层判据各一条(`enabled_names` **只判启用位、不兜快照** / `filter_names` 落空必抛且分「不在册 / 在册但
   被过滤」两种文案 / 快照写完 `chmod 600`);`is_enabled` 三种假值串 + **显式 `False` 也算停用** + 缺省视为启用;`load_accounts` 的
   **技术就绪判据 = 库里有未过期 `refresh_token`**(⚠ 不是判 client_id —— 应用级 client_id 全账号共用,拿它判永远为真;⚠ 也不是
   读飞书 —— **这条是跨层读库的**,§2.3);🔴 **账号名互斥断言**:构造一个与 `stores.registered_names()` 撞名的 eBay 账号 ⇒
   `_normalize` **必须抛**(§2.3 的硬约束,守的是 `upc_pool`/`dispositions`/`feed_log`/`claims`/`order_lines` 五处同源假设);
   ⚠ **同批补一条反向用例:与一个「在册但已停用」的沃尔玛店名撞名时也必须抛** —— 断言用的是 `registered_names()` 不是
   `enabled_names()`,这条用例就是钉住这个差别的(停用店名仍在那五张表里留着行,§2.3);
   `_RATE_BUCKETS` 未登记键**拒绝而非放行**;`safe_post_ex` 默认 `max_retries=0`;**令牌刷新响应不含 `refresh_token` 时旧值不被洗成
   None**(这条直接钉蓝图 §4.2 #2 那个坑);**`_is_persistent` 对 86400s 大桶返回 False、对小桶返回 True**(钉住新判据)。
2. **sandbox 冒烟(本批的硬验收)**:`api.sandbox.ebay.com` 用 `client_credentials` 取应用令牌 → `GET
   /developer/analytics/v1_beta/rate_limit/` **200**,打印 `reset` 实值并 dump 一次完整响应头(补核蓝图 §8.3 #14「是否真带非文档化
   限流头」)。⚠ **sandbox 的 `reset` 只能用来验证解析路径,不能当生产判据** —— sandbox 的配额与计数器未必与生产同构(同族的
   §8.3 #2 明确要求**真账号**实调),**生产归零时刻以批次 10 放量前的真账号实调为准**(蓝图 §8.3 #4 的补核方式已同步这句)。
   ⚠ 机制侧 §8.3 #4 已关闭(`reset` 是 UTC ISO-8601 + 滚动窗口),**别信午夜传说**,但**实值仍待实调**。
3. **回归**:`pytest tests/` 全绿 —— `api/_http.py` 抽取**除限速器签名外是纯搬家**,任何一条红都说明搬错了;⚠ 限速器那三处走的是
   "同名薄封装"路线,**沃尔玛侧 22 处调用点应当一行未改**,`git diff` 里出现调用点改动就是走错了路。
4. **进程级超时一句话可查**:`python -c "import api.ebay._client, socket; print(socket.getdefaulttimeout())"` → **`90.0`**
   (钉住 §2.1 ③:纯 eBay 进程也有兜底超时)。
5. **凭证泄漏三孔逐条验**:`pg_dump` 命令行含 `--exclude-table-data=ops.ebay_tokens`;`db_init` 后以 readonly 角色
   `SELECT * FROM ops.ebay_tokens` **必须被拒**;`docs/db_schema.md` 与 `backup.py` 头注两处文案就位。
6. **`--dry-run` 人眼确认**:`python cli.py ping_stores --dry-run` 输出与抽取前**逐字一致**(沃尔玛链未受影响的人眼证据)。

⚠ 本批**不碰任何 `sell.*` 端点**:那要用户令牌,而用户令牌要先人工走一次同意页(蓝图 §4.1「吊销后必须重走同意,无法自动化」),
同意页归批次 3 的 runbook。

### 批次 2a|共享表加 `platform` + 谓词补全 + `audit_models` 全仓改名(4.5 人日)

**目标**:把平台维度加进共写表,**此时库里仍只有 walmart 行,行为必须逐字不变**。

**前置**:判据 ①(它决定 `claims` 唯一索引是换成 `(platform, kind, claim_key)` 还是不动)。⚠ **① 未拍板时本批可先做
`product_events` / `listing_sources` 那两张**(与 ① 无关),claims/dispositions 那半留到 ① 落定 —— 这是本计划里唯一一处"判据阻塞
可以切开做"的地方。

**改动文件清单**:`refdata/schema.sql`(四条 `ALTER … ADD COLUMN IF NOT EXISTS platform`,范例 A **CREATE 与 ALTER 两处都写**、
且 ALTER **必须排在依赖该列的索引/视图之前**;claims/dispositions 唯一索引**原地替换 + 换新名 + DROP 旧索引**,范例 B;5 个视图
DROP 重建,范例 C;**`catalog.listing_sources` 的存量回填 INSERT(`:225-231`)显式补 `'walmart'`**;**纯注释改动两处**:`products`
六个 audit 列正式改称"沃尔玛审核结论"、**`marketplace` 的语义钉死在 `:15`**);`services/claims.py`(**六条** SQL + `_row()` 平台
校验 + 三个签名)、`services/dispositions.py`(`ON CONFLICT` 推断子句 + `claim()` 压制判定**按平台圈定** + `_SETTLE_DELETE_SQL` 的
`DISTINCT ON`)、`services/product_events.py`(`record_many` 的 `platform` **必填**,🔴 **外加 `asin` 入参,见下** + `_VERIFY_SQL`
两段 CTE 加谓词)、`services/listing_sources.py`(`register` 加必填 platform)、**`services/amz_source.py:37`(那行 `MARKETPLACE`
注释改成"亚马逊源站点",与 schema 注释对齐)**;同步 `docs/db_schema.md`。

**谓词补全清单(⚠ 按表分栏 —— 原稿把两张表的消费方混成一串,照单执行会给错表加谓词;⚠ 且**动手前现场重数一遍**,行号来自同日底稿
不是本文复核,检索式照 CLAUDE.md 那条的读侧对应版)**:
- **`catalog.product_events` 的消费方**:`services/blacklist.py` 的 `_LATEST_CTE`、`workflows/problem_scan.py` 三条 SQL、
  `workflows/sku_normalize.py`、`workflows/audit_history_fold.py`(显式写 `'walmart'`)、**`workflows/cleanup_history_import.py:63`
  的 `_WIPE_SQL`(`DELETE FROM catalog.product_events WHERE source = %s`,另 `:11` 链路注释)**、
  **`services/dispositions.py:152-154` 的 `_SETTLE_DELETE_SQL`**(它在 dispositions 那格另有交代,但**必须同时进本清单**),
  外加 5 个视图。
- **`catalog.listing_sources` 的消费方**:`services/maintenance_intents.py` 四处 JOIN(`:79/:122/:170/:489`,⚠ 该文件
  **一次都没读过 `catalog.product_events`**,别把它算进上一栏)、`workflows/sources_backfill.py:51` 的 `NOT EXISTS`,
  外加 `schema.sql:225-231` 的存量回填。

**`audit_models` 全仓改名(本批新收,理由见 §2.6)**:字段改中立名 + 加 `platform`,**实测 26 个文件约 140 处**(非测试 18 / 测试 8)。
放这里而不是原批次 4:它与本批的"全仓谓词补全"同类(机械改名 + 全绿回归),且 §2.6 自陈"越早改越便宜";留在原批次 4 会被 9 人日的
上架链挤成隐形工作量,留到二期(判据 ⑤ 把审核链推二期)则正是最贵的时候。⚠ **它是纯改名批,与 `platform` 加列互不依赖,可以先做**。

**关键 DDL**:见 §3.1/§3.3/§3.6,**不重抄**。三个坑一个都不能少:原地替换旧 `CREATE INDEX` 行(只追加 DROP 会被下次 `db_init`
全文重跑建回来)/ 新索引**换新名**(`CREATE UNIQUE INDEX IF NOT EXISTS` 只按名字判存在)/ **dict 塌陷的平台谓词有两处不是一处**
—— `_LOAD`/`load_active` 与 **`_OWNER`/`owner_of`** 都是 `dict(cur.fetchall())`,漏改会**静默只留后一条,谁赢取决于行序**
(§3.3 ③)。

🔴 **`product_events` 的 `asin` 不许从 eBay SKU 反解**:`services/product_events.py:163` 对每一行**无条件**跑
`extract_asin(r["sku"])`,而 `services/sku_asin` 的规则是**沃尔玛订货号形态**(裸 ASIN / 三段式 `前缀-源头码-价格` / 纯数字 item id);
eBay 自造 SKU(蓝图 §5.3 定的 `[A-Za-z0-9._-]{1,50}`)多半提不出,**少数会误命中 `_PLAIN`(10 位含字母)⇒ 写进
`product_events.asin` 的是假 ASIN**,而 `catalog.product_risk` 等 5 个视图的身份键正是 `coalesce(asin, sku)`。⇒ **eBay 行的 asin
由调用方显式传入**(选品时本来就知道那个亚马逊 ASIN):`record_many` 加显式 `asin` 入参或按 `platform` 路由,**eBay 分支一律不反解**。

**验收**:
1. **单测**:同 `claim_key` 两平台各能拿一条 active(① 判"不跨"时);🔴 **`owner_of` 也要单测** —— 同 `claim_key` 两平台各一条
   active 时 **`owner_of` 必须两条都返回**(它是 `try_claim` 顺延次优店那条路径的输入,与 `load_active` 同款 dict 塌陷坑,
   §3.3 ③);`dispositions.claim()` 的 eBay delete **不压制** walmart 同 SKU 维护组,且**与两个扫描件谁先跑无关**;`record_many`
   未登记 platform 值抛错;**eBay 行的 `asin` 由入参给定、`extract_asin` 不参与**;`listing_sources.register` 漏传 platform
   **抛错而不是静默落 walmart**。
2. **既有读 SQL 回归(本批的核心验收)**:改动前后对 5 个视图与上面**两栏清单里的每一条**读 SQL 各跑一次 `count(*)` 与
   `md5(string_agg(…::text, '|' ORDER BY …))`,**逐条一致** —— 此时库里没有 eBay 行,任何差异都是谓词写错。
3. **EXPLAIN**:`catalog.audit_listing_conflicts` 必须仍走 `product_events_identity_idx`(§3.1 那行记着 2026-08-14 它把生产库
   查挂过一次,表达式索引要与查询逐字一致)。
4. **`db_init` 幂等两跑**:真跑 → **再跑一次**,第二次零报错零变更(范例 B 守卫的唯一验收方式;2026-08-13 就是重跑炸的)。
   ⚠ **本仓一个现成的坑,本批顺手补**:`workflows/db_init.py:15` 是 `DANGEROUS = False` 而 `run()`(**`:37-53`**,⚠ 原稿写
   `:37-41`,行号修正,结论不变)**根本不读 `dry_run`** ⇒ `python cli.py db_init --dry-run` **不是空跑,是真跑 schema.sql,而且
   不报错**。所以库改动的"人眼确认"**不能靠 `--dry-run`**,只能靠 `git diff refdata/schema.sql` + 影子库先跑一遍;**本批给
   `db_init` 补上 `dry_run`(只打印将执行的语句块)** —— 这是 §2 "现有沃尔玛文件一律不动"的一处显式例外,理由:它是 infra 不是
   沃尔玛业务件,且这条坑正是 CLAUDE.md 点名的那一类。
   🔴 **光补 `db_init` 堵不住同一个洞,守卫测试必须同批加强**:`tests/test_cli_params.py:205`
   `test_non_dangerous_workflows_must_honor_dry_run` **只抓 `execute = params.get("execute")` 且不含 `dry_run` 的写法**,
   对**根本不碰 `execute`** 的工作流完全不管 —— 而 `db_init` / `catalog_sync` / `order_sync` / `returns_sync` /
   `settlement_sync` / `feed_poll` **全是这一类**。⇒ 把断言改成「**`DANGEROUS = False` 的 `run()` 源码里必须出现
   `dry_run`**」(先给现有沃尔玛件补齐,再让 eBay 的只读链天然受它约束)。
5. 沃尔玛链 `pytest tests/` 全绿 + `python cli.py maintenance_scan -p preview=1` 输出与改前逐字一致;
   **`audit_models` 改名后 26 个文件的引用全绿**(改名批的唯一验收就是"一条红都没有")。

### 批次 2b|新建 eBay 专属表(2.5 人日)

**目标**:把 eBay 侧要写的表建齐,**空表**。**前置**:无(纯新增,与 2a 无依赖,可并行);判据 ⑨ 只挡"映射表填表规范"那半,
**建表不等它**。⚠ **本批是批次 3 的前置**(`audit.ebay_aspects_cache` 与 `catalog.ebay_accounts` 都在这里建)。

**改动文件清单**:`refdata/schema.sql`:
- `catalog.ebay_items` 见 §3.2 草案 + 三条索引;
- 🔴 **`catalog.ebay_accounts` —— 户口链四件产物的落点表(原稿完全没有,而这正是批次 3 会当场卡死的洞:蓝图只说"落库并从 registry
  取",两个去向都不具体;缺 `merchant_location_key` 时 **`createOffer` 不报错、`publishOffer` 必失败**)**:
  ```sql
  CREATE TABLE IF NOT EXISTS catalog.ebay_accounts (
      account text NOT NULL, marketplace_id text NOT NULL,
      fulfillment_policy_id text, payment_policy_id text, return_policy_id text,
      merchant_location_key text,
      opted_in_at timestamptz,          -- SELLING_POLICY_MANAGEMENT opt-in 的时间
      privileges_json jsonb,            -- getPrivileges 原样存(sellingLimit 上限 + 注册是否完成)
      sampled_at timestamptz,           -- 🔴 上限/余量只存「值 + 采样时间」,不写死周期语义(蓝图 §3.3)
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (account, marketplace_id));
  ```
  registry 只登记**表名与字段常量,不登记值**(蓝图 §5.3 定稿);
- `audit.ebay_category_map`(amazon node → eBay categoryId,**左手边直接复用现有 `audit.amazon_taxonomy` /
  **`audit.amazon_node_paths`** / `audit.category_path_alias`**;⚠ **一期由所有者手填**,填表规范与"一个 amazon node 只准一条生效行"
  见判据 ⑨,`ebay_admission` 读不到映射即 **fail-closed 拦下并计数**);
- `audit.ebay_aspects_cache`(整站 aspects 落库);
- 条件件 `catalog.ebay_offers` —— **本期不建**,形态见 §3.2 第 (b) 格;

外加 `services/product_events.py`(eBay 事件码进 `EVENTS`)、`registry/resources.py`(`audit.ebay_category_map` 与
`catalog.ebay_accounts` 的字段常量)、`docs/db_schema.md`。

**关键点**:🔴 **事件码必须在 `EVENTS` 与 `_FEED_KIND` 两处同时登记** —— `feed_kind()` 对未登记类型**回落 `feed_type.lower()`**,
eBay 的新提交类型会拼出比如 `ebay_offer_feed_success`,不在 `EVENTS` 里 ⇒ `record_many` 抛 `ValueError`,**整轮回写炸掉**。
事件码继续**只有一份注册表**(分成两份就会重演"`maintenance_submitted` 发了大半个月没登记"那种漏)。

**验收**:1) **`db_init` 幂等两跑**零报错零变更;2) `\d catalog.ebay_items` 列序与 §3.2 逐列对齐;3) **单测**:对每个 eBay 提交
类型跑一遍 `feed_kind()` → 拼出的两个事件码都在 `EVENTS` 里(**这条测试就是防上面那颗地雷**)。

### 批次 3|户口链 + 类目链 + 账号巡检 + 授权兑换链(5 人日)

**对给定骨架的调整**:本批多两条。**① `ebay_account_health`(蓝图 §2 第 12 条)**:(a) **selling limit 余量是批次 4b 的硬准入
输入** —— 蓝图 §3.3 定稿"真正的天花板是你能上多少货,不是你能调多少次";(b) refresh token 到期巡检是 **eBay 独有的必需品** ——
OAuth **没有** `HardExpirationWarning`(那是 Auth'n'Auth 旧令牌才有的 7 天预警),**eBay 不会提醒你 refresh token 快过期**。
两者都必须早于任何写链上线,所以不能拖到批次 10。
🔴 **② `workflows/ebay_authorize.py` —— consent code 兑换链(2026-08-25 校验轮补,原稿这一步"没有函数、没有入口、没有 runbook")**:
蓝图 §1 #1 明写三个 `grant_type` 含 `authorization_code`,而原稿 §7 只有 `consent_url()`(拼 URL 不发请求)与 `get_user_token()`
(刷新),**中间"拿 code 换 token"这一步整个缺失**;原稿批次 3 的前置写着"一次人工 runbook:浏览器走一次 consent → refresh_token 落
`ops.ebay_tokens`",却**没有工作流、没有 CLI 子命令、没有 docs 文件名** —— 而风险登记簿 #1 的运维 SOP(吊销后重授权)依赖的正是同一个
不存在的东西。⇒ **它是交付物,不是一句 SOP。**

**目标**:把"这个账号能不能发东西"的地基落库 —— **一次授权兑换** + 三条商务政策 + 库存地点 + 类目树/aspects,外加每日巡检。

**前置**:**批次 1 + 批次 2b**(⚠ 原稿只写批次 1:`ebay_taxonomy_sync` 的整站 aspects 要落 `audit.ebay_aspects_cache`、
`services/ebay_aspects.py` 读它、户口链四件产物要落 `catalog.ebay_accounts`,**三张表都在 2b 建**);判据 ④。

**改动文件清单**:`api/ebay/account.py`、`api/ebay/taxonomy.py`、`api/ebay/trading.py`;
🔴 **`workflows/ebay_authorize.py`(`DANGEROUS=True`)** —— 职责四步,**一步都不许省**:① 按 `-p account=<名>` 与 registry 的 scope
常量集拼出 **consent URL 并打印**(`redirect_uri` 填 **RuName**,不是真 URL);② 收人工粘回的 `-p code=<授权码>`;③ 调蓝图 §7 的
**`exchange_code(account, code)`**(**全仓唯一一处 `authorization_code` grant**)并**落 `ops.ebay_tokens`**;④ **打印
`refresh_expires_at`**(人眼确认拿到的是 ~18 个月而不是 7200 秒)。⚠ 它**一次性、不进 `schedule.JOBS`**(归 `schedule.py` 那份
"不在表里 = 手动"的清单);⚠ **令牌值绝不进摘要**(摘要进 `ops.runs`,§3.1 凭证三孔③);
🔴 **`docs/ebay_runbook.md`(新建,本批交付物)** —— 记浏览器侧步骤:去哪点同意、RuName 配在哪、code 从回调 URL 哪一段取、
**吊销后重授权的完整 SOP(先停调度 → 改密/重授权 → 落库 → 起调度)**、以及"从备份恢复后令牌表为空必须重走本流程"(§3.1 凭证三孔①);
`workflows/ebay_bootstrap_account.py`(`DANGEROUS=True`)、`workflows/ebay_taxonomy_sync.py`(`DANGEROUS=False` ⇒ **必须读
`params["dry_run"]`**)、`workflows/ebay_account_health.py`(`DANGEROUS=False`,同上);`services/ebay_aspects.py`;
`tests/test_ebay_taxonomy.py`、`tests/test_ebay_account_health.py`、`tests/test_ebay_authorize.py`。

**关键函数面**:蓝图 §7 那三个模块 + `_client.exchange_code`;端点见 §1 #1/#2(授权)、#3-#8(户口)、#9-#13(类目)、
#7/#44/#43(巡检)。三条口径不许自创:缓存是**版本比对不是"缓存 N 小时"**(§8.2 #7);**只读 `aspectRequired` 不读 `aspectUsage`**
(§5.2 #3);selling limit **只存「上限 + 剩余 + 采样时间」,不写死日限/月限周期语义**(⚠ **措辞按修后蓝图 §3.3 更正**:不是原稿说的
"官方三比一冲突",而是「**Account OAS3 的 `getPrivileges` 描述逐字写 "on a given day"(verified);monthly 那一侧本轮未取证**」——
**处方不变**,在周期语义未决时"只存值 + 采样时间"就是对的)。
🔴 **`ebay_account_health` 的"显式停链告警"必须写死成什么(2026-08-25 校验轮:本仓没有"停链告警"这种机制)**:`cli.py:290-330` 只按
`run()` 抛不抛异常分成败,**要停链就得抛**,而本链是只读巡检、抛了 `ops.runs` 就记 `failed` —— 这个取舍原稿没做。⇒ 定稿两档:
**`refresh_expires_at - now < 30 天` → 不抛,但摘要第一行直接给结论**(如 `⚠ 2 个账号的 refresh token 将在 30 天内过期:EB-A(12天)…`
—— 链通知只显示第一行);**`< 7 天,或已被吊销/`EbayAccountDeadError`` → 抛多行 `RuntimeError`**,**接受 `ops.runs` 记 `failed`,
这正是要的"停链"**。⚠ 另定一格:全仓只有一个 `FEISHU_WEBHOOK_URL`(`registry/resources.py:76`),**eBay 告警与沃尔玛同群**、
不另分流 —— 摘要第一行必须自带 `[eBay]` 前缀,否则两条链的告警在同一个群里分不开。

**验收**:
1. **单测**:`get_default_tree` 版本未变时**不发第二个请求**(4,000/day 是最紧的桶之一);aspects 解析只认 `aspectRequired`;
   `ebay_account_health` 的**两档告警**各一条(`<30 天` 摘要第一行给结论、`<7 天/已吊销` **抛多行 `RuntimeError`**),
   ⚠ **绝不能让 workflow 静默跳过该账号**(蓝图 §2 原话;否则重演"每天空转而且报成功");🔴 **「零账号完成即判失败」**
   —— 三条链每条一测:全部账号 dead 时 `run()` **必须抛**,不许正常返回(§六 #8);`ebay_authorize` 在 `exchange_code` 返回的
   响应里**没有 `refresh_token` 字段时直接抛**(而不是落一行空令牌)。
2. **`--dry-run` 人眼确认**:`ebay_bootstrap_account --dry-run` 必须**逐行打出**"将建哪三条政策、哪个 merchantLocationKey、
   将 opt-in 什么",不是"少干活"。
3. **sandbox 冒烟**:先 `ebay_authorize` 走通一次 sandbox 账号的 consent → code → token,**`ops.ebay_tokens` 里有一行且
   `refresh_expires_at` ≈ 18 个月后**;再跑通 opt-in `SELLING_POLICY_MANAGEMENT` → 三条政策 → `createInventoryLocation` →
   `getPrivileges`,**四件产物落进 `catalog.ebay_accounts`**(批次 2b 建的那张);⚠ `getCategorySuggestions`(#13)**官方明示
   sandbox 不支持,而且是"假成功"不是报错** —— 返回随机/样板 `categoryName`(蓝图 §1 #13)⇒ `suggest_categories` 在
   `env=sandbox` 时**必须直接抛错、结果禁止落任何类目映射表**,它的冒烟只能挪到批次 10 的生产单账号试点。
4. **`docs/ebay_runbook.md` 人眼走一遍**:另一个人只照文档、不问作者,能把一个新账号从 consent 走到 `ops.ebay_tokens` 有行
   —— 这是本批唯一一条"文档也要验收"的格,因为**吊销后重授权是人在环的操作,文档不通 = 那天没人救得回来**。

⚠ 本批产物是批次 4b 的**硬门槛**:**缺 `merchantLocationKey` 时 `createOffer` 不报错、`publishOffer` 必失败**(§5.3);
三条政策**全部必须存在并挂到 offer 上**(⚠ 反证记录:某次抓取归纳出过"Inventory API 不需要商务政策",那是摘要模型的自行推断,
**以逐字引语为准:必须建**)。

### 批次 4a|中立抽取 + 台账平台化(6 人日)

⚠ **原批次 4 拆成 4a/4b(2026-08-25 校验轮:9 人日按沃尔玛对位实现体量反推明显失真,见 §五 开头那段"合计从 45 涨到 64.5"的
逐条追溯)**。拆的依据不是"太大要切开",而是**两半的验收对象根本不同**:4a 的验收是**沃尔玛链行为逐字不变**(纯抽取 + 纯加列),
4b 的验收是**eBay 侧走通一条 listing**。混在一批里,一旦 4b 出问题就分不清是抽取搬错了还是上架链写错了。

**目标**:把上架链要用的中立积木抽好、把提交台账加上平台维度,**此时 eBay 侧一行业务代码都还没跑,沃尔玛链行为必须逐字不变**。

**前置**:批次 2a(`audit_models` 改名在那里做完,本批不重复)。

**改动文件清单**:
- **中立抽取(零业务改动)**:新增 `services/listing_copy.py`(从 `mp_mapper` 抽 `scrub_brand` / `_clean_copy` / `sort_images` /
  `_sentences` / `force_amazon_copy` / `title_spec_compatible`,mp_mapper **原处 import**);`services/pricing.py`
  (`landed_price`/`parse_multiplier` 转中立、`pick_band` 改收 `bands`);`services/variant_group.py`
  (`pick_walmart_dims` → `pick_target_dims`)+ `services/variant_title.py`(`MAX_LEN` 收参数)。
- **台账平台化**:`refdata/schema.sql` 给 `ops.feed_log`/`feed_items`/`feed_item_errors` 加 `platform`
  (🔴 **`feed_id` 不改名**,§3.1 那行已推翻原稿的 `submission_id` 泛化;eBay 的分阶段语义写进 `docs/db_schema.md` 与
  `services/feed_track.py` 头注)+ `services/feed_track.py` 按平台过滤。
  🔴 **台账形态四条,本批开工前必须定死(§3.1 feed_log 行列的那四条)**,建议落法:
  ① **`submission_id` 作可空普通列,不进主键**(承接 `offerId → listingId → fulfillmentId` 的分阶段覆盖 —— 主键值不能改,普通列可以);
  ② **item 级台账主键改成含 `platform` 与 `payload_key` 的形态**(如 `(platform, account, feed_type, sku, payload_key)`),
  **或**直接引一个自增 `id` 主键 + 一条唯一索引 —— 两者都行,**但必须选一个写进 schema,不许留给实现者临场决定**;
  ③ **pending 行如何成立要写清楚**:eBay 侧提交前落 pending 时**没有任何 id**,靠 ①② 之后 `feed_id` 可空、主键不含它才成立
    (沃尔玛侧是靠 `feed_log.feed_id` 可空承接 pending、item 级台账提交成功后才落,**两条链的落行时机不同,不要照抄时机**);
  ④ **`feed_log_dedupe_uidx (feed_type, store, payload_key)`(`schema.sql:829`)加 `platform` 后必须换新名重建**
    (`CREATE UNIQUE INDEX IF NOT EXISTS` **只按名字判存在**,沿用旧名 = 静默 no-op,§3.6 范例 B)。
- **反哺器同改**(⚠ **名单按 2026-08-25 校验轮更正**:是 **四个 `sync_from_ledger`(`services/clear_sheet.py:68`、
  `listing_sheet.py:519`、`maint_sheet.py:278`、`match_sheet.py:77`)+ 第五个 `listing_sheet.heal_unknown`(`:399`)** ——
  原稿写的 `blacklist_sheet.sync_from_ledger` **根本不存在**(该文件只有 `push_all`/`push_after`/`rewrite_sheet`/`next_empty`),
  而漏掉的 `heal_unknown` 恰恰是**唯一写 `catalog.upc_pool`** 的那个,正是 §3.4 S2 方案下最需要按平台圈定的一处)。
  注册处是 `workflows/feed_poll.py:55-64` 的 `_REFLECTOR_CHAINS`。⚠ **eBay 侧的反哺器注册处要一并定**:`feed_poll` 是沃尔玛轮询器,
  eBay 的落定器是 `ebay_submit_poll` ⇒ **在它里面另立一份同形态的 `_REFLECTOR_CHAINS`**,并**照抄"同一张表的反哺器必须留在同一条
  链里、按登记顺序跑"那条串行纪律**(读-改-写,顺序错了后写的盖掉先写的)。
- 同步 `docs/db_schema.md`;`tests/test_feed_track_platform.py`。

**验收**:
1. **沃尔玛链行为逐字不变(本批唯一的核心验收)**:`pytest tests/` 全绿;`python cli.py feed_poll --dry-run` 与
   `python cli.py maintenance_scan -p preview=1` 输出与改前**逐字一致**;`ops.feed_log`/`feed_items` 的 `count(*)` 与最近 7 天摘要
   改前改后一致。
2. **`db_init` 幂等两跑**零报错零变更(动了唯一索引,范例 B 守卫)。
3. **单测**:`feed_track` 的每个读函数**漏传 platform 即抛**(不许静默落 walmart);`heal_unknown` 的 `upc_pool` 写路径带平台条件。

### 批次 4b|最小上架闭环(10 人日,全程最大的一批)

**目标**:一个 ASIN 从中立选品走到 eBay 在架 listing,且**提交前先落库**。

**前置**:批次 1/2a/2b/3/4a 全部;判据 ①②③⑤⑨;**判据 ⑥ 只挡真发,不挡开发**(sandbox 与 `--dry-run` 不受限)。
⚠ **eBay 倍率表待拍板且待建**:`ebay_price()` 读的是 eBay 自己的限额表列,由所有者建列填值(与 `multi_node_plan` 的「维护仓库」
同款治理:所有者建列 → registry 登记字段常量 → 代码直读);**表没建之前本批只能跑 `--dry-run`**。
⚠ **一期没有分配链**(§七 #9):`ebay_list_new` 的入料**直接走 `product_pool` 选品 + 四闸**,不经 `alloc_*` 发牌、不写 `claims`
—— 这是一期账号少时的显式取舍,**不是忘了**;多账号时的分配链与 `claims` 集成是二期立批。

**改动文件清单**:
- **新增 services**:`ebay_item.py`(载荷组装)、`ebay_conform.py`(aspects 必填/枚举/多值校验,**放在 publish 前**)、
  `ebay_admission.py`、`ebay_price.py`、`ebay_listing_sheet.py`、`ebay_submit.py`(三层防重的 eBay 载体)。
- **新增 api**:`api/ebay/inventory.py`、`api/ebay/offers.py`。
- **新增 workflows**:`ebay_list_new.py`、`ebay_submit_poll.py`(**两条都 `DANGEROUS=True`**;⚠ `ebay_submit_poll` 会**自动补交**
  —— 它的灰度批与试点开关见批次 10)。
  ⚠ **`ebay_submit_poll` 本批只做端点 23/16 那两半,#31 的 `fulfillmentId` 反查随批次 6 接上**:蓝图 §2 第 4 条给它列的端点是
  `23,16,31`,而 #31 所在的 `api/ebay/orders.py` **在批次 6 才建**(本批新增 api 只有 `inventory.py`/`offers.py`);
  4b 阶段台账里根本还不会有 `fulfillmentId`(它的唯一生产者 `ebay_ship_confirm` 也在批次 6)⇒ **不阻塞本批**,
  但两份文档合读容易读成"4b 就要调 #31",故点明。
- registry:eBay 上架表条目 + `docs/feishu_tables.md`;`tests/test_ebay_list_new.py`、`tests/test_ebay_submit.py`。

**关键函数面 / 契约**:蓝图 §7 `inventory.py`/`offers.py`;三步链职责切分 §5.1;三个工程陷阱 §5.2;变体 **250/5/30** 上限
**必须在 services 层拆组,不能指望 API 报错兜底**(§5.3);产品标识优先级 ePID > GTIN > brand+MPN 与站点替代文本表(§5.3);
SKU 自我约束 `[A-Za-z0-9._-]{1,50}`(SKU 走 URI path,含 `/ # ? %` 空格必然出事);防重三态映射 §5.4;单品/批量**两个显式函数 +
services 层显式 if 路由**,**严禁批量失败自动退单品**(§5.5)。

**验收**:
1. **单测(本批最厚)**:载荷指纹**按单 SKU 算不按整批**;在途拒重 / 终态可重占 / **不设时间防重窗**;4xx 落 failed **绝不换姿势
   重试** / 5xx 走反查 / `status=None` 走反查;FOUND 收编、NOT_FOUND **同方法**补交**一次**、UNKNOWN 保持 pending;**429 只能等
   `reset` 后同方法补交,补交前先查三态**;**账号级失败整账号熔断不逐条重试**(蓝图 §5.4 那条 eBay 独有分类:账号被限制时
   "Create new listings or revise existing listings" 是账号级不是条目级,逐条重试会把整批打成假失败);变体 >250 在 services 层拆;
   GTIN 缺失走**站点替代文本表**而不是英文字面量;pending 行**永不老化**且摘要摊开 `账号/类型/工作流/提交时间`;
   🔴 **「零账号完成即判失败」** —— `ebay_list_new` / `ebay_submit_poll` 每条一测:**全部账号 dead 时 `run()` 必须抛**,
   不许正常返回(§六 #8;这是最早的多账号**写**链,没有这道闸,请求形状被改坏时整条链**每天空转而且报成功**)。
2. **`--dry-run` 人眼确认(不可省)**:照抄 `list_new.py:1258-1273` 的形态 —— 前 15 条被拦理由 + 前 10 行"将提交什么(定价/库存/
   类目/aspects)" + 一行 `[DRY-RUN] 共 N 行将进入 领UPC→校验→提交`,摘要行首 `🧪 [DRY-RUN] `。
3. **sandbox 冒烟**:1 个 SKU 走完 item → offer → publish → `getOffer` 回读 `listingId`;再跑一遍**同载荷**,确认被在途/终态防重挡住。
4. **sandbox 必测三条未核验项**(蓝图 §8.3 #3/#6/**#18**,**直接改代码分支**):重复 `createOffer` 是报"已存在"还是**建出第二个
   offer**(决定 NOT_FOUND 补交前要不要先 `getOffers?sku=` 反查);`bulkUpdatePriceQuantity` 的 25 到底数什么(实测前一律按 25 条
   记录切片);🔴 **`getOffer` 回读 `offer.status` / listing 侧状态的真实取值**(#18,**它直接卡住 `catalog.ebay_items` 那两列的
   字面量与约 10 处 SQL,也卡住 `ebay_submit_poll` 的 FOUND 判据** —— 取值集补齐前**不许把状态字面量写进 SQL**)。
   ⚠ 同批把蓝图 §8.3 #11(变体组同组是否必须 categoryId/三政策/仓位一致)的沙箱实测捎上:建一组各不相同的 offer 调
   `publishOfferByInventoryItemGroup` 看是否被拒 —— **找官方原文句是死路,官方大概率根本没写**。
5. **UPC 余量检查单**:S2 起步 ⇒ **池子消耗翻倍**,"余量不足"告警的注入节奏要跟上,**这一格必须进上线检查单**(§3.4);
   同批确认 §2.3 那条**账号名互斥断言**在真实账号名下不误报(它是 S2 "零改动"的守门人)。
6. **图片四条前置校验(蓝图 §5.3 的第三道硬门槛)**:`imageUrls` **全部 `https://`**、每 SKU **≥1 且 ≤24**、变体成员 **≤12**
   —— 🔴 **必须做成批次级前置校验,不是提交后按条目级失败逐条重试**:亚马逊采集来的图 URL 若混进 `http://`,会在 publish 阶段
   **整批失败**并落错误码 `190204`,而 §5.4 的处置表会把它当条目级失败逐条重试 = **白烧配额且永远修不好**。

### 批次 5|回读 `ebay_catalog_sync`(3 人日)

**目标**:全店 item + offer 对拍,回填 `catalog.ebay_items`,缺席只标不删。

**前置**:批次 2b + 批次 4b 的 api 面(`iter_items`/`iter_offers`/`bulk_get_items`)。**不依赖上架真发** —— sandbox 手工建的
listing 就够验收,所以本批可以在判据 ⑥ 悬着的时候照做。

**改动文件清单**:`services/ebay_catalog.py`(§2.6 说的"整条链的地基,先建它")、`workflows/ebay_catalog_sync.py`
(`DANGEROUS=False` ⇒ 读 `dry_run`)、`tests/test_ebay_catalog.py`;`services/product_events.py` 接线(事件与 upsert **同事务**)。

**关键点**:§3.2 **四条必须原样继承的语义**(COALESCE 不刷 NULL / 缺席只标不删且 `missing_since` 首次值不被刷新 / 标缺席时同步
清空状态列 / 复现时 `listing_id` 重置 NULL 触发重查);端点 §1 #17/#23/#16。

**验收**:1) **单测**四条语义各一条(照抄 `tests/test_catalog_sync.py` 的形状);🔴 **外加「零账号完成即判失败」一格** ——
全部账号 dead 时 `run()` **必须抛**,不许正常返回(§六 #8;这是最早的多账号**只读**链,只读链更容易被当成"跳过就跳过",
而它一旦全账号 dead 就是每天空转报成功);2) **`--dry-run` 人眼确认**摘要第一行是"结论 +
最重要的那一个数"(链通知只显示第一行);3) **sandbox 冒烟**:建 2 条 listing → 扫描 → 下架 1 条 → 再扫描 → 第三轮再扫,确认第二轮
**标缺席不删行**、第三轮 `missing_since` **首次值不被刷新**。

### 批次 6|订单链 `ebay_order_sync` + 发货回传 + 订单风控(7 人日)

**对给定骨架的调整,两条新增,逐条给理由**:
🔴 **① `ebay_ship_confirm`(`DANGEROUS=True`,端点 #31 —— 修后蓝图 §2 新增的第 13 条工作流)**。**理由**:原稿的工作流清单里
**#31 只出现在只读的 `ebay_submit_poll`**,等于台账里的 `fulfillmentId` **永远不会被写入**,而 `submit_poll` 会去**反查一个从未提交过
的东西**;更要命的是**运单回传是 late shipment / INR 绩效的命门**(直接喂风险登记簿 #6 的 eBay 风控冷启动)。端点、台账字段、api 函数面
**三样都有了,唯独没有生产者** —— 这不是"漏了一条链",是"整条链没有起点"。
🔴 **② `ebay_order_audit`(`DANGEROUS=False`)—— 订单风控**。**理由**:沃尔玛侧 `order_audit`(钓鱼单 / 黑名单邮编)挂在
`order_chain` 每小时跑,而 eBay 侧原稿**命中 0 次**;**在无货源模式下,一张诈骗单会被链路直接拿去亚马逊下单直发** —— 损失是真金,
不是数据错。⇒ 一期至少复用同一套判据(黑名单邮编 + 采购方名单 + 金额/地址异常),落 `orders.order_lines.audit_status`
(该列本就是 text 且平台无关)。

**目标**:`lastmodifieddate` 真增量拉单落进 `orders.order_lines` 的 eBay 那一维;发货后把运单回传;拉回来的单先过一道风控。

**前置**:🔴 **判据 ⑧ 未拍板不许建行** —— 它决定 DDL(见 §3.5 末段);批次 4b(**开发段冒烟要 sandbox 里先有在架 listing 才出得来
单**,这也是它在关键路径上排在 4b 之后的原因)。

**改动文件清单**:`refdata/schema.sql`(三张表加 `platform` + `(platform, store, order_date DESC)` 索引 + **`UNIQUE (po_id, sku)`
换成两条按平台的部分唯一索引** —— 🔴 **三条写法逐字照 §3.5 末段那个编号列表**:`DROP CONSTRAINT IF EXISTS
order_lines_po_id_sku_key`(**表级约束不是索引**)、两条新排他用**显式命名的 `CREATE UNIQUE INDEX`**、**严禁 `ADD CONSTRAINT
UNIQUE`**;并给 `schema.sql:607-618` 那条 v2 守卫加上「库里已有 `platform='ebay'` 行即拒绝执行」的保护)、
`services/order_lines.py`(按平台**两个显式**身份函数 + **`upsert_order_lines` 的平台一致性预查询守卫**,机制见 §3.5 ——
⚠ **不是走 `guards` 参数**,那两个现存实例是静默保留旧值的兜底、一个都不会抛)、`api/ebay/orders.py`、
`workflows/ebay_order_sync.py`(`DANGEROUS=False` ⇒ **必须读 `params["dry_run"]`**)、**`workflows/ebay_ship_confirm.py`
(`DANGEROUS=True`)**、**`workflows/ebay_order_audit.py`(`DANGEROUS=False` ⇒ 同上)**、`services/ebay_order_center.py` +
eBay 订单飞书表条目;`tests/test_ebay_orders.py`、`tests/test_ebay_ship_confirm.py`。

**关键点**:端点 §1 #29/#30/#31;⚠ **`lastmodifieddate` 绝不与 `creationdate` 同传**(后者优先、前者被静默丢弃);
🔴 **时间窗深度是 2 年,不是 90 天(2026-08-25 校验轮推翻原稿,修后蓝图 §3.2 三源交叉 + §8.3 #5 已关闭)** ——
`getOrders` 时间窗过滤与 orderId 点取**同为 2 年**,**冷启动可以直接回捞 2 年历史**,原稿"封顶 90 天 + 深历史只能按 orderId 点取"
的调和读法**一并作废**(那个 90 天出自一份 **2023-02-16 就作废的旧页**,不存在"官方两处冲突");⚠ **唯一剩余边界**:官方逐字覆盖的是
`creationdate` filter 的 2 年上限,**`lastmodifieddate` 未被逐字覆盖**,而本仓的增量键正是它 ⇒ **冷启动第一次用 `lastmodifieddate`
回捞长历史前,实调确认一次**(拉一个 2 年前的窗口看是否报 `30830`),结果补进蓝图 §3.2 那一行;
`orderfulfillmentstatus` 只有两种合法组合,**单值别自创**(§8.2 #1);**未完成 checkout 的单根本不出现在 Fulfillment API 里**
(与沃尔玛"Created 即可见"不同);🔴 **买家脱敏已生效且辖区含"China (and its territories)"**(§8.2 #3):`buyer.username` 返回的是
immutable userId,**禁止当买家自然键、禁止字符串比对**;🔴 **GSP/eIS 双地址结构未核验**(§8.3 #1,**拿反 = 全部国际单寄错地址**),
沙箱实拉一单 GSP 订单看真实 JSON 才能定面单取哪个地址;⚠ **`ebay_ship_confirm` 的入参校验有一条未核验项**(§8.3 #15:
`createShippingFulfillment` 的 carrier + trackingNumber 是否"可选但必须成对"、无追踪单如何标发)—— **动手前补核**;
**一包裹一次、发运后不可增删 fulfillment 行**,提交前先 `GET` 回读**防重复标发**。

**飞书推送复用评估(本批要给结论)**:`services/order_center.py` 的六张表载荷全按沃尔玛订单形状(采购订单号/绩效/对账账期)⇒
**不复用同一张表**(§2.6「别在同一张飞书表里加 eBay 列」,列序即契约);eBay 另建表条目 —— 并照抄 `ORDER_SALES` /
`ORDER_SALES_AUDIT` 那个"一张表两个所有权条目"的技巧(`registry/resources.py:259-290`),让拉单**永远冲不掉审核结论**。
🔴 **同步机制那半按 2026-08-25 校验轮更正**:原稿写的"`sync_by_key` 那套机制照用"**引的是已删除的东西** —— `api/feishu.sync_by_key`
在 **2026-08-14 死代码盘点中被删**(`api/feishu.py:263`「唯一的调用方 sync_by_key 被删」;`services/order_center.py:201-207`
「曾经并存过一份通用版 … 已删除」),全仓只剩 4 处注释、零定义零调用。**真正活着的实现是 `order_center.py` 的私有本地状态版**
(`ops.feishu_sync_state` + 载荷指纹 + 写失败清状态自愈)。⇒ 正确做法是**把 `order_center.py` 的私有实现上收进 `services/` 通用件**
—— 被删文件的注释(`:206-208`)恰好写了这一刻的既定安排:「将来若第二张表也要这套同步:那时才把本文件的私有实现上收进 `services/`,
**而不是重新写一份通用版**」,而 **eBay 订单中心就是那第二个消费方**。上收后沃尔玛侧改成 import,行为逐字不变。

**验收(开发段)**:
1. **单测**:同 `(po, sku)` 不同 platform 的 upsert **抛错不覆盖**(预查询守卫);合并订单同 SKU 两 lineItem **各落一行**;
   `iter_orders` 同时传 `creationdate` **直接抛**;`buyer.username` 不参与任何键;`ebay_ship_confirm` 对**已有 fulfillment 的订单
   行不再提交**(回读防重复标发);`ebay_order_audit` 的判定与沃尔玛侧同一组样例给出同一结论。
2. **既有读 SQL 回归**:沃尔玛订单链改前后 `count(*)` 与最近 7 天摘要**逐字一致**(动了 `orders` 域的唯一约束,这条不能省)。
3. 🔴 **`db_init` 幂等两跑后 `orders.order_lines` 行数不变** —— **专验 §3.5 那条 v2 守卫没被误触发**(只验"零报错"抓不住它:
   守卫是**静默 DROP**,第二次跑照样报"成功")。
4. **sandbox 冒烟**:sandbox 造一单 → 拉回 → 标发货 → 回读 fulfillment;GSP 单单独拉一次看真实 JSON(补核 §8.3 #1)。
5. **`--dry-run` 人眼确认**:`ebay_ship_confirm --dry-run` **逐行打出**"将给哪个订单的哪几个 lineItem 回传什么运单"。
⚠ **试点段验收(拉真实窗口人眼核 1 单的地址与金额、GSP 单单独核一次)已统一挪进批次 10 的放量阶梯** —— 它要真实在架 listing 出的
真单,本批拿不到(总原则 ④)。

### 批次 7|维护链 + 破坏动作唯一出口(5 人日)

**对给定骨架的调整**:拆出 `ebay_problem_cleanup`(蓝图 §2 第 9 条)。**理由**:withdraw / deleteOffer / deleteInventoryItem /
republish 是**破坏动作**,CLAUDE.md 08-24 定稿「**破坏动作只有一个出口**」「破坏组存在即压制同 SKU 的维护组」—— 塞进
`ebay_maintenance` 等于把 08-19 那条"说不清是哪条链干的"事故重演一遍,而且 `dispositions.claim()` 的压制判定在 eBay 侧**没有落点**。

**目标**:`ebay_maintenance` 只做 title/price/inventory(MAINT_ACTIONS);`ebay_problem_cleanup` 独占 withdraw≈retire /
delete≈delete / republish≈relist(PROBLEM_ACTIONS)。

**前置**:批次 4b/5;判据 ①②⑤(⚠ **② 也阻塞本批**:维护链要改 `availableQuantity`,双平台库存口径没定就等于两边各按
"全量可售"改量 —— 与批次 0 表里 ② 那行的阻塞清单一致);⚠ **第四种 action 要先定名**(§3.1 dispositions 行:资格未获批被下架者**禁止 relist**,三值表达不了)。

**改动文件清单**:新增 `services/maint_rules.py`(抽中立纯判定,收"在线现值行 + amz 观测行"两个 dict)、
`services/ebay_maintenance_intents.py`、`services/ebay_problem.py`、`services/ebay_maint_sheet.py`、`services/ebay_clear_sheet.py`;
改 `services/dispositions.py`(新 action 登记 + 平台化 verifier 注入)、`services/store_limits.py`(eBay 限额读法,**语义反转**);
新增 `workflows/ebay_maintenance.py`、`workflows/ebay_problem_cleanup.py`(**两条都 `DANGEROUS=True`**);
`tests/test_ebay_maintenance.py`、`tests/test_ebay_problem_cleanup.py`。

**关键点**:端点 §1 #19/#22/#14/#15(维护)与 #26/#27/#18/#24(破坏);**批量 vs 单条的路由阈值归 services,api 层只给两个函数**
(§5.5);⚠ **`ops.dedupe` 的 20 小时抑制 scope 串必须带平台**,否则两平台互相压制对方的意图;🔴 `delete_item`(#18)是**最高危**
—— 删 item **连带删掉其 offer 与在架 listing**;🔴 `store_limits` 的回落语义**必须反转成 fail-closed**(§2.5 那条修正)。
⚠ **卖家标准盯盘本批只做半套**:`getSellerStandardsProfile` 的 API 面**蓝图 §8.3 #16 明说本轮未取到**(developer.ebay.com 的 SPA
与 403 使本轮全部取不到)⇒ 本批盯盘先只做 `getPrivileges` 上限 + `GetMyeBaySelling` 余量 + 本地缺陷计数,**官方等级直读补证后
再接**,不许按传言编字段名。

**验收(开发段)**:1) **单测**:破坏组存在即压制同 SKU 维护组,且**与两个扫描件谁先跑无关**(顺序不许承载判据);破坏组内部
withdraw + delete 同 SKU **不合并**(顽固件双通道齐发,合成一条会让一个的落定覆盖另一个);领任务**只看 `action` 不看 `source`**;
合并行的每个来源各占 `sources` 一格、**全空才 withdrawn**;`store_limits` 的 eBay 读法**读不到即拒绝上架**(fail-closed,
**不是回落默认值**)。2) **`--dry-run` 人眼确认(两条链都必须,破坏链尤其)**。3) **sandbox 冒烟**:改一次价 / 下架一条 / 删一条,
各自回读确认。
⚠ **试点段验收(单账号试点阶梯:先只放 title → 观察一天 → 再放 price/inventory → 最后才放破坏组)已统一挪进批次 10 的放量阶梯**
(总原则 ④)。

### 批次 8|售后 `ebay_returns_sync` + `ebay_returns_action`(4 人日)

**对给定骨架的调整**:拆成"只读同步"与"不可逆处置"两条。**理由(⚠ 已按仓库实际改正,2026-08-25)**:`DANGEROUS` 是**模块级**开关,
混在一条链里会把**只读同步的那半边一起标成危险链**(整链进 `--dry-run` 纪律与破坏动作审计口径),且**破坏动作必须只有一个出口**。
🔴 **原稿写的"`DANGEROUS=False` 读 `dry_run`、`True` 读 `execute`,两套判据不能共存于一个 `run()`"是错的**:`cli.py:297-305`
**恒同时注入两个键**(`params["execute"] = (not dry_run) if dangerous else True`;`params.setdefault("dry_run", bool(dry_run))`),
`DANGEROUS=True` 时二者等价。**拆分结论不变,但错理由必须记下来** —— 否则下次有人问"能不能合并"时,会拿它给出错误答案。

**前置**:批次 6(退货挂在 `orders.return_lines` 上,要先有订单行)。

**改动文件清单**:`api/ebay/postorder.py`;`workflows/ebay_returns_sync.py`(`DANGEROUS=False` ⇒ **必须读 `params["dry_run"]`**)、
`workflows/ebay_returns_action.py`(`DANGEROUS=True`);`services/ebay_returns.py`;`refdata/schema.sql`
(`orders.return_lines` 的 eBay 列:两套枚举各一列);`tests/test_ebay_returns.py`。

**关键点**:端点 §1 #33-#39;**创建时间窗全量重扫 + 本地未闭合单点查,双管**(§3.2 的直接理由:**三个 search 全无 last-modified
参数**,而退货状态可以在创建 **18 个月**后仍在变,窗口重扫只捞新单 —— 沃尔玛靠窗口重拉就能覆盖状态迁移,eBay 必须多这一步);
**退货 `status` 与 `state` 是两套枚举(25 值 vs 43 值,部分同名语义不同),两列都存**,合并即重演"source 当 action 用"的事故
(§8.2 #5);`REPLACEMENT_*` 12 值官方自述 "not currently supported",**可收录枚举但不要写分支逻辑**;**取消链可以"不作为"**
(不响应则 3 个工作日后自动拒绝,批准后 FVF 自动退回,§8.2 #6);GSP 硬约束 34200/34300 + **发运后不可增删 fulfillment 行**;
EU/UK `issueRefund` 需数字签名(**美站不需要**,若上欧站签名做在 `_client`)。

**验收(开发段)**:1) **单测**:未闭合单点查**必须有本地态过滤**(否则各 4,000/day 见底);两套枚举分两列落;`decide`/`refund` 在
`execute=False` 时**一条请求都不发**。2) **`--dry-run` 人眼确认**(处置链)。3) **sandbox 冒烟**:造一单退货走 search → 点查 →
decide 的读写两侧。
⚠ **试点段验收(先只跑 `ebay_returns_sync` 观察三天,再放 `ebay_returns_action`)已统一挪进批次 10 的放量阶梯**(总原则 ④)。

### 批次 9|结算 `ebay_settlement_sync` + KPI 底座 `ebay_kpi`(5 人日)

**对给定骨架的调整**:🔴 **多一条 KPI 底座(2026-08-25 校验轮补)**。**理由**:§3.1 已定"`ops.store_kpi_daily` / `orders.perf_events`
**不并轨,另建 eBay 表**",但原稿**没有任何批次建这两张表、也没有 `ebay_kpi` 链** ⇒ 后果有两处:(a) **批次 10 的放量阶梯说"观察 3 天
`ops.runs` 的时长与失败率"就放量,而"值不值得放量"的业务判据(出单率 / 缺陷率 / 退货率 / 毛利)一格都没有** —— 只看链跑没跑挂,
等于拿"程序没崩"当"生意没亏";(b) §2.5 说复用的 `store_perf`(收缩与中位数)**读不到数**,它的输入正是 KPI 表。
⇒ **一期只做轻量版**:`ebay_kpi` 每日汇总(在架数 / 新上架数 / 出单数与金额 / 退货数 / 未标发货超时数 / 缺陷计数)落
`ops.ebay_kpi_daily`(逐列对齐沃尔玛 `store_kpi_daily` 的口径命名,但**另建表不并轨**);官方等级与服务指标那半**不做**
(蓝图 §8.3 #16 未取到 API 面,§七 #8)。⚠ **它是批次 10 放量判据的数据来源 ⇒ 必须早于放量,不能"最晚做"。**

**目标**:payout 驱动的三级闭环(订单 ↔ 交易 ↔ 放款)+ 一张能支撑放量决策的日报表。**前置**:批次 6。

**改动文件清单**:`api/ebay/finances.py`、`workflows/ebay_settlement_sync.py`(`DANGEROUS=False` ⇒ **必须读 `params["dry_run"]`**)、
**`workflows/ebay_kpi.py`(`DANGEROUS=False`,同上)**、`services/ebay_settlement.py`、**`services/ebay_kpi.py`**、
`refdata/schema.sql`(`orders.settlement_lines` 加 `platform`,批次 6 已加则复用;**新建 `ops.ebay_kpi_daily`**)、
`tests/test_ebay_settlement.py`、`tests/test_ebay_kpi.py`。

**关键点**:端点 §1 #40-#42;**payout 驱动**——先 `getPayouts`,再按 `payoutId` 拉 transaction,🔴 **禁止按日全量扫 transaction**
(12,000/day 是第二紧的桶);**放款周期不写死成常量,按 `payoutDate` 事件驱动**(§8.2 #2,官方文档取不到周期);⚠ 对账链
**不得依赖买家支付明细**(脱敏后美国买家一律 `CustomCode`,§8.2 #3);⚠ 费率是函数不是常量、**计费基数含运费含税**(§2.6 那条
修正)——对账差异先怀疑基数,别先怀疑接口。

**验收(开发段)**:1) **单测**:`iter_transactions` 缺 `payout_id` 且缺日期范围时**直接抛**(防"按日全量扫"复活);`ebay_kpi` 的
每个指标在"零数据"时给 **NULL 不给 0**(`NULL ≠ 0` 铁律,下游禁止 `or 0`)。2) **sandbox / 造数对拍**:与
`orders.settlement_lines` 对拍一期,笔数与金额合计一致。
⚠ **试点段验收(核对一次真实放款的三级链路)已统一挪进批次 10 的放量阶梯**(总原则 ④)。

### 批次 10|调度上线 + 全部试点段验收(3 人日 + 2 周试点观察)

**第一步不是写代码,是核对两条门槛**:判据 ⑥ 已**书面**拍板(蓝图头 blockquote:拍板前 eBay 上架链不得起调度);
判据 ⑦ 的合规入站面(**批次 11**)已上线并自检通过 —— ⚠ **"申请豁免"这条退路已作废**(蓝图 §8.3 #12 关闭:opt-out 仅限
"not persisting any eBay data" 的应用,**本项目落库 ⇒ 必须订阅**)。

**改动文件清单**:`registry/schedule.py` 的 `JOBS` 加条目 → 🔴 **`tests/test_launchd.py` 同批改两处断言(原稿漏,而这两处
必然变红)**:
- `test_only_the_high_frequency_chains_live_on_this_machine`(`:125-141`)是**集合相等断言**
  `{j["label"] for j in _LAUNCHD} == {"feed_poll", "order_chain", "product_ingest"}` ⇒ **任何 eBay job 挂 launchd 必红**
  (而原稿验收 #1 写的是"那条仍绿",自相矛盾);
- `test_batches_match_the_greyscale_plan`(`:150-161`)里破坏性链是**硬编码集合** `{"product_chain","product_clear",
  "audit_sheet","list_new"}`,且 `elif j["batch"] == 3: raise AssertionError` ⇒ **把 `ebay_list_new` 排进灰度批 3 会直接抛**;
- 顺带 `test_manual_only_workflows_are_not_scheduled`(`:90-104`)的手动清单里**要加 `ebay_bootstrap_account` 与
  `ebay_authorize`**(两条一次性链,不进 `JOBS`);
→ `python cli.py skill_export --dry-run`(先跑这个)→ 真跑 → `skills/walmart-schedule/` 生成物**进 git 但不手改**;
launchd 那半 `python cli.py launchd_install --dry-run`;`docs/schedule_plan.md` 的人读投影同步。
⚠ **`skills/walmart-schedule/` 这个目录名要不要改**(它现在是"沃尔玛调度"的技能包,而里面即将出现 eBay 的任务):**本批要给结论**。
建议**不改名、只改包内说明**(改名会让已注册的智能体定时任务全部指向失效,代价 > 收益),但**必须在包头写清它现在同时承载两个平台**。

**排法(照 `registry/schedule.py:8-11` 的 batch 语义,不是优先级)**。⚠ **仓库的 batch 语义是**:1 = 只读/低危、
2 = **订单 + 日报**(且注明"开之前必须先停旧 KPI 与旧订单同步")、3 = 破坏性。eBay 侧**沿用同一套语义**(不另起编号,避免同一张表
两种读法):

| 灰度批 | eBay 链 | runner | 时刻 |
|---|---|---|---|
| 1(只读/低危) | `ebay_taxonomy_sync` | gpt | 每日 09:00(类目版本哨兵,只比版本) |
| 1 | `ebay_account_health` | gpt | 每日 09:20(令牌到期 + selling limit 余量 + `getRateLimits` 对表,**全仓只有这一条调 #43**) |
| 1 | `ebay_catalog_sync` | gpt | 每日 11:00 |
| 1 | `ebay_returns_sync` | gpt | 每日 12:00 |
| 1 | `ebay_settlement_sync` | gpt | 每日 12:30 |
| 2(订单 + 日报) | `ebay_order_sync` | **launchd** | 每小时 :10(高频链住在这台机器上,与 `order_chain` 错开) |
| 2 | `ebay_order_audit` | **launchd** | 每小时 :25(**必须排在 `ebay_order_sync` 之后**,拿的是同一小时刚落库的单) |
| 2 | `ebay_ship_confirm` | **launchd** | 每小时 :40(排在风控之后:**没过风控的单不许标发货**;**破坏链排批 2 的豁免理由见下方 🔴**) |
| 2 | `ebay_kpi` | gpt | 每日 23:30(当天数据齐了再汇总)。⚠ **它是日报,按仓库 batch 语义「2 = 订单 + 日报」归批 2,不是批 1**(`registry/schedule.py:8-11`;eBay 侧沿用同一套语义,不另起编号)。⚠ **batch 答的是"属于哪一类",不是"哪天 load"**:它是下方验收 #3 里**每一段放量准入的数据源** ⇒ **试点第 1 段就要跟着挂上**,别等批 2 |
| 3(破坏性,**每条先手动 `--dry-run` 人眼确认再 load**) | `ebay_list_new` | gpt | 每日 **20:30** |
| 3 | `ebay_submit_poll` | gpt | 每日 21:00(见下方 🔴) |
| 3 | `ebay_maintenance` | gpt | 每日 16:00 |
| 3 | `ebay_problem_cleanup` | gpt | 每日 16:30(维护之后,破坏动作唯一出口) |
| 3 | `ebay_returns_action` | gpt | 每日 13:00 |

🔴 **`ebay_ship_confirm` 留在灰度批 2 是一处显式豁免,不是漏排** —— 它 `DANGEROUS=True`、对 eBay 真写、且**发运后不可增删
fulfillment 行**(不可逆),按 `ebay_submit_poll` 那条理由本该进批 3。**豁免理由**:发货回传是**履约必需的时效动作**,
延迟直接吃 late shipment 绩效(并连带 INR),**必须按小时跑** —— 而批 3 的语义是"每条先手动 `--dry-run` 人眼确认再 load",
一条每小时的链没法逐轮人眼确认,排进批 3 等于要么天天人肉盯、要么这条链形同虚设。**替代闸两道,一道都不许省**:
① **台账 pending 先落库的三态防重**(提交前写 pending → 在途同指纹拒重 → 提交前先 `GET` 回读防重复标发,§六 #2)——
这道闸挡的正是批 3 想挡的"重复不可逆写";② **试点期挂上去但先只打印不回传** —— 照 `ebay_submit_poll` 的 `resubmit=0` 同款手法,
给它一个**自己的开关参数**(如 `schedule.JOBS` 条目里写死 `params=["confirm=0"]`,由 `run()` 自己认),先跑若干天,
每天人眼看摘要里"将给哪个订单的哪几个 lineItem 回传什么运单",确认无误后改成 `confirm=1` 并重跑 `skill_export`。
🔴 **这道闸不许用 `-p dry_run=1` 来做**:`cli.py:301` 是 `params["execute"] = (not dry_run) if dangerous else True`,
`execute` **只由命令行的 `--dry-run` 决定**,`-p dry_run=…` 走的是 `:305` 那条 `setdefault`、**碰不到 `execute`** ⇒
对 `DANGEROUS=True` 的链写 `-p dry_run=1` 会**照样真发**,而摘要里还印着 dry_run —— 正是本仓最怕的那种"不报错"。
⚠ **连带一处测试改动**:`tests/test_launchd.py:150-161` 的 `dangerous` **硬编码集合必须把 `ebay_ship_confirm` 登记进去**
(它确实是破坏链,不登记就等于在测试里撒谎);**但它不在批 3**,而现有断言是双向的(`if label in dangerous: assert batch == 3`)⇒
**登记它会让"破坏链 ⇒ 批 3"那一半当场红**。⇒ **断言的改法在批次 10 的改动清单里一并处理**:给这条断言加一份**显式豁免清单**
(名字 + 一行理由),**不是把 `ebay_ship_confirm` 从 `dangerous` 里拿掉,也不是把断言删掉或放宽** —— 语义必须保留成
"破坏链默认进批 3,例外必须逐条留名留理由"。

🔴 **`ebay_submit_poll` 排灰度批 3,不是批 2(原稿排错)**:它 `DANGEROUS=True` 且**会对 eBay 真写** —— NOT_FOUND 时**同方法补交
一次**(§六 #2),是**全仓唯一一条会自动补交的链**,排进批 2 等于绕过批 3 那道"每条先手动 `--dry-run` 人眼确认再 load"的闸。
**试点期它的补交默认关闭**:`schedule.JOBS` 条目里写死 `params=["resubmit=0"]`,观察三天确认台账三态判得对了,再改成 `resubmit=1`
并重跑 `skill_export`。
🔴 **`ebay_list_new` 必须排在黑名单与审核之后(当天次序是硬约束,不是偏好)**:`registry/schedule.py:33-34` 明写并被
`tests/test_launchd.py:118` 一带钉死 —— `product_chain 13:00 → blacklist 15:00 → audit_sheet 18:10 → list_new 20:00`;
**`ebay_list_new` 吃的是同一天的黑名单与 `catalog.products.audit_status`**(判据 ⑤ 一期复用沃尔玛审核结论 ——
**推荐值,待拍板**;若最终拍成方向 B「一期另立人工/简化闸、不读 `audit_status`」,本条排期理由里"审核"那一半随之作废,
20:30 这个时刻要重定,黑名单那一半仍成立)⇒ 排在 **20:30**,
在 `list_new` 之后。⚠ **写清楚这一条,是因为不写就等于"把判据承载在调度顺序上"** —— 而 §六 #7 自己禁止这件事:
**顺序只做优化,判据要写进代码**;所以 `ebay_list_new` 内部**也要有"今天的黑名单/审核跑过没有"的显式前置检查**,顺序只是让它别白等。
`ebay_bootstrap_account` 与 `ebay_authorize` **一次性,不进 `JOBS`**(蓝图 §2 #1;归 `schedule.py` 里那份"不在表里 = 手动"的清单)。

**三条硬纪律**:同一条链**绝不 launchd 与 gpt 两边都挂**(撞上了后到的退 3 空跑一轮**而且报"成功"**;上表已保证两个 runner
**零交集**);每条 workflow 的 docstring 第一行是技能包那一格的**唯一出处**,写成 `"""ebay_xxx — 一句话说清干什么(危险性)。"""`,
取不到就留白**不编**;⚠ **eBay 链动 `product_ingest` 游标必须借同一把锁**(`runlock.hold("product_ingest")`)—— 两个进程各自落
`next_cursor`,后写的盖掉先写的,中间那段记录**永远不会再被拉一次,而且两侧都不报错**;凡带 `product_ingest` 的条目**必须有
`lock_wait=` 参数**(`test_launchd.py` 那条断言钉着:手动撞车时**等锁而不是退 3 空转**)。

**验收**:
1. `tests/test_gpt_skill.py` 三条钉子全绿(仓库副本 == 从调度表现渲染 / 无遗留任务文件 / 不许丢参数);
   `tests/test_launchd.py` **改后**全绿 —— ⚠ 改的是**断言的内容**,**不是把断言删掉或放宽**:那两条测试守的是
   "只有高频链住在这台机器上"和"批 3 只放破坏性链",**语义必须原样保留**。
   🔴 **不在这里写死条数** —— **破坏性集合与灰度批次登记一律按本文上面那张排法表逐条核对(排法表是唯一口径)**,
   `tests/test_launchd.py` 两处断言(高频链集合相等断言 `:125-141`、批次/破坏性断言 `:150-161`)**同批同步、改后全绿**。
   ⚠ 逐条核对时特别注意两格:排法表里 launchd 的那几条要与高频链集合逐字对齐;`ebay_ship_confirm` 是
   **登记进 `dangerous` 但排在批 2** 的显式豁免,需要按上面那条 🔴 给断言加豁免清单,不是把它从 `dangerous` 里拿掉
   —— 写死一个数字正是上一版在这里出错的原因(数字与排法表对不上,少登记的那条会被 `elif j["batch"] == 3` 当场抛)。
2. **`--dry-run` 人眼确认**:`skill_export --dry-run` 与 `launchd_install --dry-run` 各看一遍。
3. **单账号试点 → 放量(阶梯不许跳),并在此逐条打勾批次 6/7/8/9 的「试点段验收」**(总原则 ④ 把它们统一挪到了这里):
   - **第 1 段(只读)**:先只挂**一个账号的只读链**,观察 3 天 `ops.runs` 的时长与失败率;此段内完成
     **批次 6 的"拉一个真实窗口、人眼核对 1 单的地址与金额、GSP 单单独核一次"**、**批次 9 的"核对一次真实放款的三级链路"**、
     **批次 8 的"先只跑 `ebay_returns_sync` 观察三天"**、以及**批次 3 那条只能在生产做的 `getCategorySuggestions` 冒烟**。
   - **第 2 段(写链)**:挂 `ebay_list_new`(`resubmit=0` 的 `ebay_submit_poll` 同批)、观察 3 天;此段内完成
     **批次 7 的阶梯**(先只放 title → 观察一天 → 再放 price/inventory → **最后才放破坏组**)与**批次 8 的 `ebay_returns_action`**。
   - **第 3 段(放量)**:再放量到全部账号。⚠ **每段的准入看 `ebay_kpi` 的业务指标,不只看 `ops.runs` 有没有红**
     (只看链跑没跑挂 = 拿"程序没崩"当"生意没亏")。
4. **放量前再跑一遍 `getRateLimits` 对表**:确认按"应用身份"计的桶在多账号下没被打穿 —— **蓝图 §8.3 #2 那条判错就在这一刻现形**
   (按账号算而实际按应用算 = 超配额 429 打全店);**同一刻用真账号读一次 `reset` 实值,定死生产的日配额归零时刻**
   (§8.3 #4:sandbox 的 `reset` 只验证过解析路径,**不能当生产判据**),结果回填蓝图 §8.3 并标日期。
   ⚠ 对表**不做准入判据**(官方未声明刷新延迟,必须当作可能滞后的数据)。
5. 试点期每天人眼看一次飞书通知的**第一行**(链通知只显示第一行,第一行不成结论 = 这条链的摘要写错了,当场改);
   ⚠ eBay 与沃尔玛**同一个群**,eBay 侧摘要第一行必须自带 `[eBay]` 前缀(批次 3 定的那条)。

### 批次 11|合规入站面(3 人日,与任何批次并行,但必须早于批次 10)

**为什么单列一批**:它**不碰本仓任何既有文件**,却要给本项目**新增一类运行形态** —— 当前架构是"脚本 + launchd + PostgreSQL",
**没有任何常驻公网服务**,而 `MARKETPLACE_ACCOUNT_DELETION` 需要一个**公网入站 HTTPS 端点**。原稿把它整个压在判据 ⑦ 里:
**没有批次、没有工时、没有技术方案(放哪、谁续证书、挂了怎么发现)** —— 而端点挂了 destination 转 `MARKED_DOWN`
(⚠ **状态名未核验**,见下方交付物 ④)、**丢事件是静默的**。

**前置**:判据 ⑦ 的"怎么建"那半("要不要建"已定:**必须订阅**,蓝图 §2 末格 + §8.3 #12)。

**交付物**:① **选型定稿**(三选一并写理由:云函数 / 托管小服务 + 队列 / 第三方转发到本机;**评价维度是"挂了怎么发现"而不是"多便宜"**);
② 端点实现,**四条硬要求逐条对**(https;路径**不得含内网 IP 或 `localhost``;`challengeCode` + `verificationToken` + `endpointURL`
**hash 到一起回 `200 OK`**;每条通知**即时**以 `200/201/202/204` 之一应答);③ 用 Notification API 建 destination + subscription
(桶见蓝图 §3.1,定稿 200/day);④ 🔴 **每日订阅状态自检**:并进 `ebay_account_health`(它本来就是每日巡检链)——
读 destination 状态,**一旦不是正常态就按"< 7 天"那档抛多行 `RuntimeError` 停链告警**。
⚠ **destination 的状态枚举本轮未核验**(`getDestinations` 方法页是 SPA,`MARKED_DOWN` 这个名字取自二手描述)⇒
**实调 `GET /commerce/notification/v1/destination` 拿到真实取值集、补证并标日期后,再把告警判据写死**;
在那之前判据只准写成"**不等于已知正常态即告警**"(fail-closed 的方向,宁可误报),**不许按 `MARKED_DOWN` 这一个字面量做等值判断**
—— 判据挂在一个没核过的枚举值上,状态名一变就变成"永远不告警",正是本项目最贵的那类不报错;⑤ 证书续期责任人与到期提醒写进
`docs/ebay_runbook.md`。

**验收**:1) 官方控制台的 challenge 校验一次通过;2) 人为把端点停掉 → **自检当天必须告警**(验的是"挂了能发现",不是"能收事件");
   ⚠ **这一格同时是 destination 状态枚举的补证时刻**:停掉端点后实调一次
   `GET /commerce/notification/v1/destination`,把返回的真实状态值抄回蓝图 §8.1 ② 与本批交付物 ④ 并标日期
   —— 验收当天拿不到真实取值,就说明这条告警的判据仍是猜的;
3) 端点日志里能看到至少一条真实通知并回了 2xx。

## 六、纪律继承清单

eBay 链从第一行代码起适用,不设过渡期、不设例外:

1. **每账号固定出口代理,严禁直连**(待判据 ④;拍板前按沃尔玛同级从严)。代理 URL 由 services 层造好注入,**api 层不读凭证表**;用户名密码
   `quote(safe="")` 编码。🔴 **代理必须按域名放行,不许做目的地 IP 白名单**(蓝图 §6.3):eBay sandbox 已 CDN 化,官方明说不承诺
   固定 IP、让你自己定期 nslookup,按 IP 放行会**随时静默断线**。⚠ 措辞纪律:**不许写成"eBay 官方要求固定 IP"或"eBay 按 IP 判定
   关联"** —— 官方确认多账号合法、也确认关联店会被连坐,但**关联判据从不公开**;写成那样就是编造。
2. **提交防重先落库三态**:提交前写 pending → 在途同指纹拒重 → 终态可重占(**不设时间防重窗**);出事后反查三态 **FOUND 收编 / NOT_FOUND 同方法
   补交一次 / UNKNOWN 保持 pending 挂着**;4xx 终态拒、5xx 走不确定通道;item 级台账要有 `missing` 这一档("台账里有、终态明细里查无"必须有名字,
   不装成功也不装失败);错误明细**当标准动作拉**;pending 行**永不老化**,摘要必须摊开 `账号/类型/工作流/提交时间`(只报个数没法处理,几轮后就
   成了背景噪音)。⚠ **写操作永不自动兜底,绝不换方法重试**——换方法重试 = 重复提交制造机。
3. **蓝图先行 + 官方核验**:`docs/ebay_api_blueprint.md` **已定稿**(2026-08-25,八节;本条原写"写第一行调用代码前先出",现更新为
   已完成),**一个端点一个函数,不自创签名**;配额进令牌桶**登记制**(未登记键直接拒绝——旧系统对未知键放行,`RETIRE_ITEM` 实际
   零限速跑了数月);✅ **`refdata/ebay_rate_limits.tsv` 已入仓**(2026-08-25,75 行 6 列 + 22 行头注,原稿"尚未入仓,批次 1 落"
   作废),作为来源之一,**两者不一致以蓝图 §3 为准**(tsv 记官方缺省值、蓝图记定稿桶配置,本就不同;🔴 **写 registry 取蓝图的
   定稿列,不要抄 tsv 的默认列**;改任一边必须同步改另一边)。⚠ 蓝图里全部 `[verified]` 都是**转引调研底稿的原文引语**,"转引"这一层
   没有第二双眼睛 ⇒ 三条 🔴 级结论(整体覆盖语义 / `aspectRequired` vs `aspectUsage` / `refresh_token` 不轮转)**实现前重开原页
   各看一眼**;§8.3 那张表(**现为 18 条,其中 5 条已关闭 / 1 条降级,动手前逐条按状态列过**)未关闭的项动手前逐条补核。
4. **AI 改完代码必须先 `--dry-run`,人眼确认输出后才跑真的。** 缺省即真跑(2026-08-16 定稿),默认值不再替你挡;摘要行首带 `🧪 [DRY-RUN] ` 前缀。
5. **services 新增积木前先通读现有函数确认无重复**;每个函数 docstring 第一行写清"输入什么 → 输出什么";事件码/平台名/字段名一律**只有一个出处**,
   未登记即抛(三处清单各漂各的、`maintenance_submitted` 发了大半个月没登记,已吃过两次)。
6. **动了表就同步 `docs/db_schema.md`**,新增飞书表同步 `docs/feishu_tables.md`;改调度只改 `registry/schedule.JOBS` 再重跑 `cli.py skill_export`
   (**先 `--dry-run`**),**不手改 `skills/` 生成物**;⚠ 同一条链**绝不 launchd 与 gpt 两边都挂**(撞上了后到的退 3 空跑一轮,**而且报"成功"**)。
7. ⚠ **判据不许承载在调度顺序上**:eBay 链与沃尔玛链若有数据依赖,依赖写进判据,顺序只做优化(`dispositions.claim()` 的压制判定是先例:顺序改了
   结果也不变)。⚠ **eBay 链动 `product_ingest` 游标必须借同一把锁**(`runlock.hold("product_ingest")`)——两个进程各自落 `next_cursor`,后写的盖掉
   先写的,中间那段记录**永远不会再被拉一次**,而**两侧都不报错**。
8. 🔴 **每条多账号 workflow 必须有「零账号完成即判失败」这道闸**(2026-08-25 补,沃尔玛侧 `api/_client.py:388-390` 的注释自陈:
   "若请求形状被改坏,表现是**全部店一起 dead**,由 workflow 侧『零店完成即判失败』那道闸兜住")。只抄 `EbayAccountDeadError` 的
   状态码枚举是**抄了一半** —— 全账号 dead 时每个账号都被"跳过"、`run()` 正常返回,**这一整条链每天空转而且报"成功"**,正是 CLAUDE.md
   安全铁律点名的那种事故形状(与风险登记簿 #1 的 refresh token 静默跳过是同一个病)。
   **落点:批次 3(验收 #1)、批次 4b(验收 #1)、批次 5(验收 #1)三处已各写死一格 —— 分别是最早的多账号只读巡检链、
   最早的多账号写链、最早的多账号只读业务链;批次 6-9 的多账号链**同规则适用**,新建一条多账号 workflow 就补一格,
   不再逐批复述。**
9. 🔴 **运行态凭据不进任何导出、快照、日志与摘要**:`ops.ebay_tokens` 的三个泄漏孔(全库 `pg_dump` / `readonly` 角色 GRANT /
   文档缺失)与堵法见 §3.1"凭证泄漏三孔",**与建表同批(批次 1)落**;`refresh_token` **只有一个出处 = 该表**,严禁进飞书凭证表
   (§2.3);真密钥 client_id / client_secret / RuName **只进 `<DATA_ROOT>/.env`**,仓库与 `_ENV_TEMPLATE` 里只出现变量名。
   ⚠ **从备份恢复后 eBay 令牌为空,必须重走 `workflows/ebay_authorize.py`** —— 这句要同时出现在 `backup.py` 头注、
   `docs/db_schema.md` 与 `docs/ebay_runbook.md` 三处。

10. **对齐 2026-08-29 合并 main 后的两条新全仓标准**(本文一~九节定稿于 2026-08-25,main 其后落了它们;实现各批次时按
    CLAUDE.md 现行文与 `docs/conventions.md` 对应节执行,与本文冲突处以新标准为准):
    ① **店维工作流失败处理唯一标准**(2026-08-26 定稿,conventions §四:单店隔离 → `store_retry` 串行补试 → 缺席不炸整轮、
    摘要首行点名 → `store_absence` 水位避让 → 链尾重赛一次;归类词唯一出处 `store_retry.diagnose` 六档)——**eBay 多账号链
    (批次 3/4b/5/6-9)同样适用**,`ebay_accounts` 的账号维失败处理照这套标准落,不自创第二套;本条与 #8 的「零账号完成即判失败」
    互补不互替。② **飞书读写只准走 api/feishu 标准通道**(限额 = 官方 × 95%,常量只在「限额登记表」出生,守门测试拦通道外直连)——
    eBay 侧全部飞书表(账号表、上架表、订单推送)自动在此约束内,批次 1 的 registry 登记与批次 4/6 的表写入不得绕通道。
    ⚠ 本文引用仓库代码的行号均以 **2026-08-25 的 main 为快照**;其后 main 变更约 136 文件(上架/问题/维护链大改),
    批次实施时**现场重定位行号与函数形态**,以当日代码为准,行号对不上不等于结论作废。

## 七、范围外声明(本期明确不做)

1. **Promoted Listings / 站内广告**(Marketing / Ads / Negotiation API 族,蓝图 §1「明确不做」)。理由:铺货链尚未证明单位经济性
   之前投广告是放大亏损;且广告归因要另一套报表口径与另一层配额。
2. **国际站点与多站点上架**:只做 `EBAY_US` 单站点、单 `FIXED_PRICE` 格式。连带三条:`catalog.ebay_offers` 子表**备着不建**
   (§3.2);GTIN 站点专属替代文本表**仍要进 registry**(登记不实现 —— 防的正是将来有人写英文字面量);EU/UK `issueRefund` 的
   数字签名**不实现**(美站不需要)。
3. **店铺装修与 Store 订阅档位选型**:属运营决策,且刊登费免费额度与订阅档位绑定,归所有者。
4. **买家消息与客服**:站内消息、纠纷协商(`payment_dispute` 子链,250k/day,业务未开)一律人工在站内处理,本期无 API 需求。
5. **蓝图 §8.1 的三个被否方案**:Trading `AddFixedPriceItem` 上架路径、Sell Feed(LMS)XML feed 上架路径、业务事件 webhook 订阅
   —— 三条都不做;`MARKETPLACE_ACCOUNT_DELETION` 合规订阅是**唯一例外**(判据 ⑦)。Buy API 族(非卖家域,需独立资质)同样不做。
6. **eBay 侧的 RPA 补数**(`services/yingdao.py` 的对应物)与**一次性迁移件**(`cleanup_history` 的对应物):纯新地,没有存量要迁。
7. **跨平台实时库存扣减器**(判据 ② 的另一个方向):两条链是不同进程、不同调度节奏,扣减器本身会成为新单点,**本期不建**。
8. **`getSellerStandardsProfile` 直读官方等级**:API 面本轮未取到(蓝图 §8.3 #16),按"预留端点只登记不实现",**补证后另立批次**。
   ⚠ 取证路**没有判死**:蓝图 §8.3 #16 已撤回"必须上浏览器抓"的结论(404 是路径错不是 SPA),改法是 WebSearch 定位正文句 →
   再试正确的 spec 文件名;**配额面已取到**(Sell Analytics 400/100 per day,Sell Compliance 在官方配额页 NOT PRESENT)。
9. 🔴 **eBay 侧的分配链(`alloc_*` 发牌 / `claims` 占用 / `store_release`)一期不做**(**本条为 2026-08-25 校验轮补入** ——
   原稿既没把它放进任何批次,也没写进本节,而判据 ① 与批次 0 的表格却反复提"eBay 分配链天天分不出东西",默认它存在)。**一期落法**:
   账号数少 ⇒ `ebay_list_new` **直接走 `product_pool` 选品 + 四闸**(`brand`/`category`/`lead`/`channel`),**不发牌、不写 `claims`**。
   **二期立批**:多账号时才需要"哪个账号上哪个品"的分配链,那时把 `alloc_engine`/`alloc_groups`/`alloc_survey` 按平台接进来,
   并按判据 ① 的结论决定 `claims` 怎么圈平台。⚠ **注意这不影响 §3.3 的 `claims` 改造**:那条改造是为了**沃尔玛已占满的 active 行
   不会拦死 eBay**,一期即使不写 `claims` 也要改 —— **不改的话 eBay 侧任何将来要读占用的地方都会拿到跨平台的错答案**。
10. **跟卖 `match_listing` 的 eBay 对应物**:不做。理由:沃尔玛侧那条链**自己都还在手动**(`registry/schedule.py` 那份"不在表里 =
   手动"的清单里,头注写着"对拍未完成前只许 `--dry-run`"),**把一条没定稿的链复制到新平台是双倍的不确定**;且跟卖要的
   "identical item 判定"在 eBay 侧是**官方 13 属性判据**(蓝图 §8.4 #2),与沃尔玛的品牌级判据不是一回事,照搬既过严又拦不住。
11. **类目映射链化(`catmap_*` 的 eBay 对应物)**:一期不做,由所有者手填 `audit.ebay_category_map`(判据 ⑨ 与批次 2b 给了填表规范);
   `import/suggest/promote` 三条链**列二期**。⚠ 这条与上面几条不同:**表要建、规范要写、fail-closed 要做** —— 不做的只是"自动填表"。

⚠ **"本期不做" ≠ "永远不做",但更不等于"先留个开关"**:上面每条都**不许在代码里留半成品分支或双轨开关**(明禁的"双轨过渡遗留");
要做时新起一个批次,走同一套验收。
⚠ **本节是 2026-08-25 校验轮的重点补丁位**:原稿的 §七 "防得住『留半成品分支』,防不住『压根没想到』" —— 六个经营域
(分配链 / 订单风控 / KPI 底座 / 跟卖 / 发货回传 / 类目映射)当时**既不在任何批次、也不在本节**。现在**逐条有了归宿**:
发货回传与订单风控**进批次 6**、KPI 底座**进批次 9**、类目映射**表进 2b 链推二期**(判据 ⑨)、分配链与跟卖**进本节**。

## 八、风险登记簿

登记规则:每条给**触发形态(尤其"会不会报错")→ 现有防线 → 缺口与处置**。⚠ 本仓最贵的教训都是"不报错"那一类,所以这一列写在最前。

| # | 风险 | 触发形态 / 会不会报错 | 防线与处置 |
|---|---|---|---|
| 1 | **refresh token 过期或被吊销,需人工重授权** | 18 个月不轮转,而 OAuth **没有** `HardExpirationWarning`(那是 Auth'n'Auth 旧令牌才有的 7 天预警)⇒ **eBay 不会提醒你**;卖家改密码/改登录名 → 该账号全部 refresh token **当场被吊销**。表现:该账号所有写调用 401 自愈一次后仍失败。🔴 **若 workflow 写成静默跳过该账号,就是"每天空转而且报成功"** | `ebay_account_health` 的**两档告警**(`<30 天` 摘要第一行给结论;`<7 天/已吊销` **抛多行 `RuntimeError`,接受 `ops.runs` 记 failed —— 这正是要的停链**,批次 3);`scopes` 列用于"改了 scope 必须重授权"的比对。🔴 **运维 SOP 有可执行物**:`docs/ebay_runbook.md`(批次 3 交付物)+ `workflows/ebay_authorize.py`(consent → code → `exchange_code` → 落库 → 打印 `refresh_expires_at`)—— ⚠ **原稿这条 SOP 指向的是一个不存在的东西**(全仓既无兑换函数也无 runbook 文件),现已补齐。步骤照本仓既有纪律:**先停调度 → 改密 → 浏览器重走同意 → 落库 → 起调度** |
| 2 | **多账号关联连坐** | 官方确认多账号合法,但 "If we apply restrictions or limits to one account, then similar restrictions or limits may be applied to the member's other linked accounts" [id=4232];**关联判据官方从不公开**,所以事前无信号、事后一次性打掉一批 | 每账号固定出口代理(判据 ④,**取严**)+ **代理按域名放行不按目的地 IP**;缺代理即整账号跳过。⚠ 与风险 4 相乘:违规被抓的爆炸半径 = 全部关联账号。⚠ 文档措辞:**不许说成"eBay 按 IP 判定关联"** |
| 3 | **双平台超卖** | 亚马逊侧观测(`snapshots.stock_count`)两平台共读、可售数量各自算 ⇒ 同批货被两边各按"全量可售"挂出。**不报错**,第一现场是买家订单,补救成本是两边绩效指标同时掉 | 判据 ②(各自限额 + 共享观测,**先定防超卖规则再上链**);`NULL ≠ 0` 铁律照抄(下游禁止 `or 0`)。⚠ 实时扣减器本期不建(§七 #7) |
| 4 | 🔴 **无货源模式本身违规** | 逐字命中 eBay 明文禁令 [id=4176];被抓的表现是账号级限制("Create new listings or revise existing listings" 受限),**不是条目级** ⇒ 逐条重试会把整批打成假失败 | 判据 ⑥ 所有者**书面**拍板(阻塞起调度,不阻塞开发);提交台账的**账号级健康位**,命中即整账号熔断;放量走批次 10 的阶梯以控爆炸半径 |
| 5 | **新账号 selling limits 冷启动** | 官方示例即 `$100 / 10 件`;⚠ **周期语义待定**(⚠ **措辞按修后蓝图 §3.3 更正**:不是"官方三比一冲突",而是 **Account OAS3 逐字写 "on a given day"(verified),monthly 那一侧本轮未取证**)⇒ 代码只存「上限值 + 剩余值 + 采样时间」,**不写死周期语义**(冲突未决时它就是对的);提额是站内**人工流程无 API** | `ebay_account_health` 每日采 `getPrivileges`(上限)+ `GetMyeBaySelling`(余量,REST 无等价物,是全仓唯一一条 Trading 调用);🔴 `store_limits` 的「读不到就回落默认值」在 eBay 侧**必须反转成 fail-closed** —— 读不到还照上 = 直接撞"绕限制"违规 |
| 6 | **eBay 风控冷启动** | 新账号 + 大批量新 listing + 中国卖家画像叠加。表现:listing 被限、账号级失败、类目资格未获批下架 —— 后者**官方明文禁止 relist**,而现有 retire/delete/relist 三值**表达不了这一态** | 放量阶梯(批次 10 验收 #3);`dispositions` 新增第四种 action(§3.1);VeRO 只新增 `source`、**不新开执行出口** |
| 7 | **配额口径判错(按 App vs 按卖家账号)** | 蓝图 §8.3 #2 未核验。判错方向不对称:按应用算而实际按账号算 = 白白慢;**按账号算而实际按应用算 = 超配额 429 打全店** | 已按"应用身份"取严;批次 10 放量前**再对一次表**——这是它现形的唯一时刻。⚠ 对表**不做准入判据**(官方未声明刷新延迟) |
| 8 | **OAuth `client_credentials` 1,000/day 打爆** | 🔴 **最硬的一条**:App 级共享,**一炸全账号全链条一起炸**;不落库时刷新次数 = 进程数 × 账号数,而本仓一天几十条 workflow 各自启停 | 令牌**落库**(`ops.ebay_tokens`)+ 提前 300s 续 + 单飞(`SELECT … FOR UPDATE` 或复用 flock)+ **应用令牌按 scope 集合做缓存 key**(key 不含 scope 会拿窄 scope 令牌调宽 scope 端点拿 403) |
| 9 | **刊登费是真实固定成本** | 非订阅仅 250 条/月免费,超出 $0.35/条(一万条 ≈ $3,412/月);沃尔玛侧**无此项**。不报错,只是月底账单 | **"能上就上"的铺货策略不能沿用** —— 属策略问题,登记在此防遗忘;费用归集交给结算链(上架链不承担单 SKU 成本核算:`getListingFees` 只能查未发布 offer 且按 marketplace 汇总不分摊) |
| 10 | **限速状态漂成两份 + 日窗口桶把 PG 打穿** | `ops.rate_events` 是同一张表,eBay 桶名若不带平台前缀就与沃尔玛的 `inventory.put` 撞名 ⇒ **两个平台互相扣对方的配额**。🔴 **规模那半原稿只讲了一半**:`_is_persistent` 现判据 `window >= 600.0 or limit <= 10` 会把 **eBay 全部日窗口桶(86400s)推进 PG**,而 `_acquire_pg`(`api/_client.py:222-256`)是 **advisory 事务锁 + 24 小时窗 `count(*)` + 每次调用 INSERT 一行 + 顺手 DELETE 两天前**;以 Sell Inventory 定稿桶 **1,600,000/day** 计 ⇒ **单桶一天最多 160 万行、常驻上限约 320 万行**,**每一次 API 调用都要在 advisory 锁下数一遍这个窗口**,而该锁把同一应用身份的**全部 eBay 调用串行化**(与 `EBAY_ACCOUNT_WORKERS=4` 直接对冲),再加 **PG 挂 = eBay 全停(fail hard)**。沃尔玛不暴露此问题,是因为它的持久桶全是 6/天、10/小时这种个位数 | 桶名 `ebay.` 前缀;两处 `_RATE_BUCKETS` 登记表**头注互相点名**;🔴 **`_is_persistent` 判据改「窗口 ≥600s **且** 上限 ≤1000」**(大桶回进程内、小桶仍进 PG),**批次 1 定死**,理由与被否的备选(日桶换每日计数行 UPSERT —— 要动沃尔玛侧共用的 `_acquire_pg`)一并抄进函数头注,见 §2.1 ②;fail hard **要么接受、要么显式改判据,不许静默降级进程内** |
| 11 | **蓝图 §8.3 的未核验项(现 18 条,已关 5 条 + 降级 1 条)** | 逐条影响不同批次:🔴 #1 GSP 双地址(拿反 = **全部国际单寄错地址**)→ 批次 6;🔴 #2 配额口径 → 批次 10;🔴 #3 重复 `createOffer` 行为 → 批次 4b 的补交逻辑;🔴 **#18 offer/listing status 取值封闭集 → 卡住 `catalog.ebay_items` 建表与约 10 处 SQL 字面量、以及 `ebay_submit_poll` 的 FOUND 判据**(⚠ **2026-08-25 新开**:原稿把它挂在 #9 上,而 #9 清单里根本没有这两项 ⇒ "逐条补核"那道闸放不住它);🔴 #16 Compliance/Analytics API 面 → 批次 7 的指标来源;#15 `createShippingFulfillment` 入参 → 批次 6 的 `ebay_ship_confirm`。✅ **已关闭的别再排工时**:#5(时间窗 = **2 年**)、#7、#10、#12、#4 机制侧;#8 降级为上线前抽验 | **动手前必须补核,不许按推断编码**;补核方式蓝图逐条给了(多数是沙箱实调)。补核结果**回填蓝图 §8.3 并标日期**;⚠ **编号永不复用、永不重排**,关闭项留原位标 ✅ |
| 12 | **共享表并跑对象是沃尔玛链** | eBay 是新平台没有旧系统并跑问题,**但它碰 `catalog.products/snapshots`、`ops.rate_events`、`catalog.product_events`、`orders.*`、`ops.feed_log/feed_items`** ⇒ "新旧严禁并跑"这条铁律的对象变成了沃尔玛链 | 批次 2a 的"加列时库里仍只有 walmart 行 + 行数逐条对拍";批次 4a 的"沃尔玛 feed 链行为逐字不变";批次 6 的"沃尔玛订单链改前后摘要逐字一致" + **`db_init` 两跑后 `order_lines` 行数不变**(专验 v2 守卫);eBay 动 `product_ingest` 游标必须**借同一把锁** |
| 13 | 🔴 **`ops.ebay_tokens` 泄漏(2026-08-25 补,原稿两文档"备份/backup"命中 0 次)** | 三个孔全部**不报错**:① 每日 02:00 的全库 `pg_dump` 把 18 个月长期凭证打进 `<DATA_ROOT>/backups`(保留 14 天);② `db_init` 的 `GRANT SELECT ON ALL TABLES IN SCHEMA … ops … TO readonly` 把令牌表授给 Metabase/NocoDB/MCP 用的 readonly 角色;③ 令牌值若进异常文案或 `ops.runs.summary` 就永久留痕 | §3.1"凭证泄漏三孔"逐条堵:`--exclude-table-data=ops.ebay_tokens` + 显式 `REVOKE` + 三处文档头注;**批次 1 与建表同批**,验收在批次 1 #5。⚠ **恢复后必须重走 consent**,写进 `docs/ebay_runbook.md` |
| 14 | **账号名与店名撞名(2026-08-25 补)** | `upc_pool` 的复用与烧号、`ops.dispositions` 唯一索引、`ops.feed_log`、`catalog.claims`、`orders.order_lines` **五处都按 `store` 圈定且不看平台**,而店名/账号名都是飞书自由文本 ⇒ 撞名时**两个方向都不报错**:eBay 复用沃尔玛在用的 UPC;沃尔玛一次 RETIRE 把 eBay 在用的号标成 `conflict`(**永久弃用**) | §2.3 的硬约束:eBay 账号统一命名规则 + `_normalize` 里 `stores.registered_names() ∩ ebay 账号名 ≠ ∅ 即抛,**批次 1 单测**;代价一条断言。⚠ **判据取「在册」不取「在营」**:五处全按 `store` 圈定不看启用位,停用店名仍在表里留行,`enabled_names()` 会放行(§2.3) |
