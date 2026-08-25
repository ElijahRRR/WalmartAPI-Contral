# eBay 平台扩展计划

> 状态:**决策、契约与开发计划全部定稿,未动任何生产代码**(2026-08-25)。
> 🔴 **先于一切的阻塞项**:本项目「亚马逊采集 → eBay 上架 → 出单后从亚马逊下单直发买家」逐字命中 eBay 明文禁令,且与多账号连坐
> 条款相乘 —— 见判据 ⑥(依据蓝图头 blockquote)。**所有者书面拍板前,eBay 上架链不得起调度**;写代码、跑 `--dry-run`、沙箱冒烟
> 不受此限。
> 范围:**本仓内**新增 eBay 平台的全套业务工作流,与沃尔玛链共享产品库、采集摄取链、运行纪律(cli.py / registry / 锁 / 台账 /
> 通知)。来源:仓库侧三份证据级底稿(库影响 / services 积木清点 / 工程契约,2026-08-25,行号可回溯到 `refdata/schema.sql` 与各
> `.py`)。端点/配额/认证/提交通道形态见 `docs/ebay_api_blueprint.md`(**2026-08-25 定稿,八节,46 组 63 端点**),本文引用它
> 一律写「蓝图 §N」**不重抄**;⚠ 蓝图 §8.3 的 17 条未核验项是本计划**全部批次的共同前置**,动手前必须补核,不许按推断编码。
> 前置事实:全仓 `grep -rni "ebay|易贝"` 只命中 `services/audit_stopwords.py:60` 一个停用词
> ——**eBay 是纯新地**,没有半成品、没有旧 shim、没有并跑切换的旧系统。

**一句话模型:产品库共享、判决分平台、出口各自独立。** 亚马逊侧产品身份与观测(`catalog.products`/`snapshots`,主键
`(marketplace, asin)`,其中 `marketplace` 是**亚马逊源站点** 'US' 而非销售平台)只有一份;每个平台各有一张投影表
(`catalog.walmart_items` / 新建 `catalog.ebay_items`)、一套 api 出口、一份准入判决;共写台账加 `platform` 维度。
**不做双轨、不做隐式路由、不给 platform 参数默认值。**

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
**新增 `trading.py`**(XML 网关 + 第三种认证头 + 独立 5,000/day 桶,全仓唯一一条 XML 调用;将来 REST 补上能力后整体删掉)。
结果:`__init__.py` + 9 个实体文件(与原骨架数量相同、成分不同)。
选子包不选平铺:eBay 端点域一定超过 5 个文件,平铺后两平台文件交错、一眼看不出归属。
收录规则同沃尔玛:只实现「工作流×端点矩阵」出现过的端点,预留端点只登记不实现。⚠ 加子包**必须同步改 `api/__init__.py:6-10` 的
"文件划分"人读索引**,漂开会让下一个 AI 照旧索引找文件。**`_client.py` 的拆法(唯一 api 灰色件)**:连接池 / transport /
`_parse_retry_after` / `rate_acquire` 的跨进程滑动窗(`ops.rate_events` + advisory 锁)/ `download_bytes` 与平台无关,OAuth 与
`WM_*` 头是沃尔玛的 ⇒ 抽 `api/_http.py`(中立)供两侧 import。**限速状态尤其不许重写第二份**——两套会漂的限速状态,漂了的后果是 429 与封号。

### 2.2 workflows:`ebay_` 前缀平铺,cli.py 零改动

`cli.py` 是 `importlib.import_module(f"workflows.{args.workflow}")`、**无白名单** ⇒ 新增 `workflows/ebay_catalog_sync.py` 即刻可跑,锁(按
workflow 名取)、`ops.runs`、飞书通知、dry-run 注入全部直接继承。硬约束:只暴露 `run(params) -> str`;顶层 `DANGEROUS = True|False`;无 argparse /
flock / `ops.runs` / 通知 / `load_dotenv` / `basicConfig`;`params` 值**永远是 str**,自己 coerce。⚠ **`DANGEROUS=False` 的工作流必须读
`params["dry_run"]` 而非 `execute`**——只读 execute 的话 `--dry-run` 对它完全无效**而且不报错**;dry-run 是**把要干的事逐行打出来**,不是"少干活"。摘要**第一行自成结论 + 最重要的那一个数**(链通知只显示第一行),排版用
`services/notify_fmt` 不另写,入库截 4000 字符;失败抛**多行 `RuntimeError`**(取最后一行的写法曾让一家店 400 时只发出一行 MDN 链接)。
docstring 第一行写成 `"""ebay_xxx — 一句话说清干什么(危险性)。"""`,它是 skill_export 技能包那一格的**唯一出处**,取不到就留白
(不编);参数白名单若做,`{"execute","dry_run"}` 必须放行。

### 2.3 `services/ebay_accounts.py`:仿 `stores.py` 三层判据

从第一天就拆开(沃尔玛侧是后来才把判据从 `_normalize` 循环里抠出来的,此前没有任何函数单独回答得了"在不在营"):`registered_names()`
答在不在**册**(只用于"查无此账号 vs 在册但停用"的报错分流,飞书失败直接抛)/ `enabled_names()` 答在不在**营**(判死账号、判占用、判
分配范围一律用它,**不兜快照**——兜底快照是过滤后的结果,拿它当在营名录会把"配好了但某项缺失的在营账号"算成停用,而这个名录直通整店
下线)/ `load_accounts(filter_names=None)` 答现在能不能**调 API**(回落快照 + warning)。照抄:`is_enabled` 独立谓词(缺省视为启用,
假值串 `("否","false","0")`)、`_cell()` 单元格归一(文本字段可能返回 `[{'text':...}]` 段列表)、`filter_names` 落空**必抛**且分
「不在册 / 在册但被过滤」两种文案、快照写完 `chmod 600`、每个函数 docstring 第一行"输入什么 → 输出什么"。

| 项 | 沃尔玛现状 | eBay 差异落点 |
|---|---|---|
| 凭证 | `client_id`/`client_secret`,谓词判 client_id 非空非 `"0"` | App 级 id/secret + **每账号 `refresh_token`**。⚠ 谓词必须判 **refresh_token**:client_id 全账号共用,拿它判"这账号配好了没"**永远为真** |
| 代理 | 每店固定出口代理,三件套任一缺即整店跳过(**生死线**) | 见判据 ④;若判不需要,那条过滤要**显式删掉并写明为什么**,不许留永远为真的空判 |
| 快照 | `paths.stores_snapshot_file()` | 新登记 `paths.ebay_accounts_snapshot_file()`。⚠ **绝不复用同一文件**:后写的整表覆盖前者 |
| 并发 | `STORE_WORKERS = 24` | ⚠ **不要照抄这个数字**。24 安全的前提是"每店自有代理 + 配额按 `(store, endpoint)` 计,店间不抢同一桶";**蓝图 §3.1/§6.5 已定稿令牌桶第一参数取「应用身份」(全账号共享一个桶)**⇒ 前提正好相反 ⇒ 另立 `EBAY_ACCOUNT_WORKERS`,**起步 4**,理由写进常量头注(⚠ 配额到底按 App 还是按卖家账号计**本身仍未核验**,蓝图 §8.3 #2;判错方向不对称,故取严) |

### 2.4 registry 登记先行(铁律 3)

写第一行业务代码前落**九类**登记:① `registry/platforms.py` 的 `WALMART`/`EBAY`/`PLATFORMS` 常量(**代码里禁止平台名字面量**,与
`product_events.EVENTS`、`listing_sources.SOURCE_*` 同款纪律);② **base URL 逐族登记**(⚠ **已按蓝图 §4.4 修正**:不是原稿说的"可能
两个",是**逐族六个以上函数** —— OAuth token `api.ebay.com/identity/v1/oauth2/token`、consent `auth.ebay.com`、Sell
Inventory/Account/Fulfillment `api.ebay.com`、**Finances `apiz.ebay.com`**(不是笔误)、Taxonomy、Post-Order `/post-order/v2`、
Developer Analytics、Trading `/ws/api.dll`;sandbox 通则 `api|auth → *.sandbox.*` 路径不变;⚠ 两 host 是否等价未核,上线前 sandbox
实测各自是否 200 后**逐族钉死**);③ **scope 字符串常量集**(蓝图 §4.3;⚠ **`api_scope/commerce.taxonomy` 这个 scope 不存在**,
写了就是错的);④ `EBAY_BULK_MAX = 25`(蓝图 §3.2 原文"必须登记进 registry,不许散落");⑤ **GTIN 站点专属替代文本表**(US
`Does not apply` 等 8 行,蓝图 §5.3 —— 🔴 代码里严禁写 `"Does not apply"` 英文字面量,一上多站点就会在 DE/FR 静默造出不合规
listing);⑥ **错误码常量**(蓝图 §6.10:REST `429/2001/ACCESS/REQUEST`、Fulfillment 34200/34300/34903/34905、Trading
518/21919144);⑦ env 变量名(A 类没有就抛 / B 类回落 None,真值只在 `<DATA_ROOT>/.env`);⑧ 飞书表条目 + 同步
`docs/feishu_tables.md`;⑨ 新路径以**函数**暴露。

### 2.5 services 直接复用清单

| 类 | 文件 | 理由 |
|---|---|---|
| 采集与数据源 | `product_ingest` `scrape_batches` `api/scraper` `amz_source` | 采的是亚马逊,与销售平台正交。⚠ **全项目只有一条采集入口**,eBay 不得另开;`amz_source` 产出统一「产品数据契约」dict,**eBay 上架链入参就该是它,不要另造形态** |
| 产品判定与变体 | `product_score` `brand_key` `catpath` `category_blacklist` `audit_phase0` `audit_stopwords` `variant_group` `variant_remap` `variant_title` | 信号全来自亚马逊源侧。变体三件实为中立、**只是名字骗人**(`pick_walmart_dims` 的 enum 由调用方注入)——改名 `pick_target_dims` + `MAX_LEN` 收参数即转正 |
| 台账骨架 | `dispositions`(状态机 / 按 action 领 / 破坏组压制 / `sources` 分格)、`product_events`(只追加 + 事件码唯一注册表) | **08-19 生产事故换来的纪律,必须继承同一套,不许重写一份** |
| 闸门·分配·店铺算法 | `blacklist.load_banned_asins`、`risk_gate` 品牌闸、`alloc_groups`、`alloc_engine` 发牌、`alloc_survey` 纯函数判定、`store_limits`「读不到 vs 填了 0 必须分开」、`store_targets` 三态空值、`store_perf` 收缩与中位数 | **黑名单绝不分平台**(品牌在沃尔玛因知产被拉黑,拿去 eBay 同样是知产问题);发牌只认 store/asin/brand_key/category/channel 五个键;三条店铺算法是所有者反复定稿的资产,只有列名与数据源要分平台。🔴 **两处按蓝图 §8.4 修正**:① `store_limits` 那条「读不到就回落默认值」的语义在 eBay 侧**必须反转成 fail-closed** —— selling limits 是**平台强制硬上限**,读不到还照上 = 直接撞 id=4232 的"绕限制"违规;② **跨店排他单位是 identical item(官方 13 属性判据)不是品牌** —— 别把 `brand_key` 的品牌级占用照搬到 eBay(既过严又拦不住真违规:无货源商品大量 Generic/OEM,品牌为空时一条都拦不住),**沃尔玛侧的品牌占用不要动**,catalog 层需同时承载两套排他 |
| 基础设施 | `runlock` `db_guard` `notify_fmt` `textfmt` `llm_cache` `llm_cost` `launchd` `gpt_skill` `api/feishu` `api/llm` `api/llm_vision` | 与卖到哪个平台无关,零改动 |

### 2.6 services 必须另立清单(含灰色件拆法)

拆法一律:**两个显式函数 + 调用方显式 if 路由**。任何"沃尔玛路径失败自动落 eBay"或按 SKU 格式猜平台的写法都是重复提交制造机。

| 沃尔玛件 | eBay 对应物 | 拆法 / 差异要点 |
|---|---|---|
| `walmart_catalog` / `api/_client` | **`services/ebay_catalog.py`** / `api/_http.py` + `api/ebay/_client.py` | `ebay_catalog` 是整条链的地基,**先建它**,四条语义原样继承(§3.2);`_client` 拆法见 §2.1,限速桶与连接池只准一份中立实现 |
| `pricing` / `mp_mapper` | 抽 `landed_price`/`parse_multiplier` 中立 + 新增 `ebay_price(...)`;抽 **`services/listing_copy.py`**(`scrub_brand`/`_clean_copy`/`sort_images`/`title_spec_compatible`)+ `ebay_item.py` | ⚠ **不要**给 `walmart_price` 加 platform 参数——那会让两套倍率表在一个函数里分叉;`pick_band` 改收 `bands`。`listing_copy` 是**最高性价比的一次抽取**:文案与图片处理约占 mp_mapper 一半且平台无关。⚠ **eBay 的成本口径与沃尔玛不同**(蓝图 §5.5/§8.4 #3):费率不是常量而是「店铺当前等级 × 类目服务指标」的**函数**(TRS+ −10%、Below Standard +6/7%、INAD Very High +5/6%、国际 +1.65%,最坏叠加有效抽成 >30%),且**计费基数含运费含税**(按裸价算每单系统性少算约 1.1 个百分点,**而且两侧都不报错**);刊登费是**真实固定成本**(非订阅仅 250 条/月免费,超出 $0.35/条),沃尔玛侧无此项 ⇒ **"能上就上"的铺货策略不能沿用** |
| `audit_l2` `audit_l3` `audit_l4` `audit_models` | `audit_l2_common`(R4/R5/R7/R8/R10)+ `_walmart`(R1/R3)+ `_ebay`;L3/L4 引擎中立、政策语料与 system prompt 分平台;`audit_models` 字段改中立名 + 加 `platform` | 执行模型(100 起分、六条固定顺序全跑不短路、下界 -1000)共用。⚠ L4 平台名做成**显式必传不给默认值**,否则会有一天用沃尔玛立场审 eBay 的图**且不报错**;前缀缓存按 system 逐字节命中 ⇒ 两份 system 各一条缓存链,不互相打散。⚠ `audit_models` 改名是**全仓改名**,涉六个文件,**越早改越便宜** |
| `audit_l1_llm` `audit_reason` `audit_rules` `pt_admission` `pt_spec` `mp_conform` | `audit_l1_ebay` / `audit_reason_ebay` / `audit_rules_ebay` / `ebay_admission` / `ebay_aspects` / `ebay_conform` | ⚠ 形态不同别硬套:沃尔玛 spec 是本地大文件按 PT 拆分,eBay aspects 是**整站叶子类目一次性下载 + 落库缓存**(蓝图 §1 #11),刷新口径是**比对 `categoryTreeVersion`、变了才拉**而不是"缓存 N 小时"(§8.2 #7);单类目补漏用 #12 但**禁止逐 SKU 调**(5,000/day 会见底)。🔴 判必填只准读 `aspectRequired` 布尔,**绝不能读 `aspectUsage`**(官方自陈必填项在那里返回的是 `RECOMMENDED`,读错会**漏掉全部必填项**,§5.2 #3) |
| `maintenance_intents` / `product_pool` `risk_gate` `alloc_survey` `store_targets` `store_limits` | 抽 `maint_rules.py`(纯判定收两个 dict)中立,取数与载荷按平台各一份;候选池 SQL / 闸数据 / 限额表字段常量按平台各一条 | ⚠ 20 小时抑制的 `ops.dedupe` scope 串**必须带平台**,否则两平台互相压制对方的意图;算法函数一律保持一份 |
| `listing_sheet` `maint_sheet` `clear_sheet` `feed_track` `order_center` `kpi` `problem_products` `order_lines` | 各建 eBay 对应物 | ⚠ **别在同一张飞书表里加 eBay 列**——列序即契约,混平台会让"表头改名静默弄坏"的老坑翻倍 |
| `sku_asin` `spec_split` / `cleanup_history` `yingdao` | 前两个:`is_standard_asin()` 中立、numeric 倒查按平台注入 lookup、`spec_split` 路径常量做参数 / 后两个:**不建** | 纯数字 SKU 分支现在写死"倒查 `catalog.walmart_items`";一次性迁移件与沃尔玛卖家页 RPA 不跨平台 |

## 三、库改动定稿

**主力手法**:`platform text NOT NULL DEFAULT 'walmart'` 加列——存量行自动正确、现有 INSERT(列清单显式)不改就继续写 walmart、回滚只需
DROP COLUMN。⚠ **坏处也在这里**:eBay 写入方忘传 platform 就**静默落成 walmart**;对策是写入一律走 services,签名里 `platform` **必填**,
值不在 registry 常量集就抛。⚠ **加列本身零破坏**(已核:全仓 `SELECT *` 只 5 处,**没有一处**打在共享表上),成本全在"eBay 行进表之后"的
谓词补全 ⇒ **加列与放 eBay 行必须分两批**:先加列 + 补谓词(库里仍只有 walmart 行,行数与 EXPLAIN 可逐条对拍),再放 eBay 写入。

### 3.1 共享表逐张取向

| 表 | 取向 | 要点 |
|---|---|---|
| `products`/`snapshots`/`latest_snapshot` | **纯读,零改动** | ⚠ `audit_status`/`audit_reason`/`walmart_pt`/`pt_source`/`audit_version`/`audited_at` 六列是**沃尔玛口径**,一行装不下两个平台的判决 → 见判据 ⑤,并在 schema.sql 注释里把六列正式改称"沃尔玛审核结论"(纯注释改动,防下一个 AI 写错地方) |
| `catalog.product_events` | 加 `platform` + **5 视图 DROP 重建 + 12 处读 SQL 补谓词** | 🔴 最高危。`_VERIFY_SQL` 的 `LEFT JOIN walmart_items` 对 eBay 行必然 `w.sku IS NULL` ⇒ 判 `gone` ⇒ **凭空落 `delete_verified`**,绕过"不信回执信观测";视图 `product_risk` 按 `coalesce(asin,sku)` 全局聚合,eBay 的 missing 会并进同一 ASIN 的 `unexplained_missing`(`list_new` 正消费它报警) |
| `catalog.listing_sources` | 加 `platform`(**标注列,不进 PK**)+ 四处消费 SQL 带谓词 | `source_type`(amz/match/self/1688)答"产品出身",**与销售平台正交**;加平台是为把破坏动作的路由谓词写全,不靠"账号名不重名"侥幸 |
| `catalog.claims` / `ops.dispositions` | **同型改造**:加列 + 唯一索引换名替换 + `ON CONFLICT` 推断子句同批改(claims 详见 §3.3) | 🔴 claims 不改就**拦死 eBay**。⚠ dispositions 的动作在键里**不能去掉**(顽固件 retire+delete 双 feed 齐发);⚠ **压制判定(`claim()` 内)必须同时按平台圈定**,否则 eBay 一条 delete 会压制沃尔玛同 SKU 的维护建议。⚠ **eBay 侧要新增第四种 action**(蓝图 §8.4 #4):因**资格未获批**而被下架的商品官方明文**禁止 relist**,现有 retire/delete/relist 三值表达不了这一态;而 VeRO 只是新 `source`,**不新开执行出口**(08-24「只看 action 不看 source」定稿在 eBay 侧完整成立) |
| `seller_blacklist`/`amazon_cat_blacklist` 纯读零改动;`asin_blacklist`/`brand_blacklist`/`brand_err_hits` | **eBay 只读做闸,不写渠道表** | 类别码 A~L 由**沃尔玛报错文本**解析而来,一行装不下两个平台的理由;eBay 下架原因先落 `product_events`,归拢另议 |
| `ops.runs`/`cursors`/`dedupe`/`feishu_sync_state`/`scrape_batches`/`rate_events` | **共写,零 DDL 改动** | 键天然带命名空间(workflow 名 / `'ebay_order_sync:<账号>'` / scope 前缀)。⚠ 但 `rate_events` 的 **bucket 名必须带平台前缀**(如 `ebay.inventory.bulk_create`):两套客户端写同一张表,撞名即互扣配额 |
| `ops.store_kpi_daily`/`orders.perf_events` | **不并轨**,另建 eBay 表 | 32 列逐列对齐沃尔玛口径(otd/vtr/srr/payout);`perf_event_spans.still_active` 判据是"是否还出现在最新一期报表里",硬塞会污染 |
| `ops.feed_log`/`feed_items`/`feed_item_errors` | ✅ **已冻结:复用**(蓝图 §5.4) | 端点形状查清了:eBay 是**同步 REST + 逐条部分成功 + 没有整批 id**,但结论**不是**"另设计",而是**同表同语义、粒度从「批」降到「单 SKU」**:🔴 防重键按**单 SKU 载荷指纹**算(批量端点的 25 只是传输优化,**不是防重单位**,这是与沃尔玛最大的语义差);`feed_id` 泛化为 `submission_id`(上架存 `offerId`,publish 后存 `listingId`,发货存 `fulfillmentId`,**分阶段存最新一个**);反查反而更干净——`getOffer`/`getOffers?sku=` **精确点查**,沃尔玛那套"同尺寸兄弟切片会撞指纹"的排除逻辑不需要。⚠ `ebay_submit_poll` **不是轮询器**(eBay 没有 INPROGRESS 态可轮),是"台账落定器 + 启动对账",pending 行只可能来自崩溃/网络异常 |
| `ops.ebay_tokens` | **新建**(蓝图 §4.2 第 5 条) | 令牌**落库共享,不放进程内存**:本仓一天几十条 workflow 各自启停,令牌只活在进程内 ⇒ 刷新次数 = 进程数 × 账号数,而 `client_credentials` 只有 **1,000/day 且 App 级共享,一炸全账号全链条一起炸**。主键 `account + env + token_kind`,列含 access_token / access_expires_at / refresh_token / refresh_expires_at / scopes。🔴 **刷新响应不含新的 refresh_token**,严禁 `row.refresh_token = resp.get("refresh_token")`(`.get()` 返回 None 会把好 token 洗成空;eBay 的 refresh token **不轮转**,写一次管 18 个月)。真密钥 client_id/secret/RuName 仍**只进 `.env`**;本表属运行态凭据,`docs/db_schema.md` 要标注**禁止进任何导出/快照** |
| `audit.walmart_*` 四表 | **eBay 全不用** | `audit.amazon_taxonomy`/`node_paths`/`category_path_alias` 是亚马逊侧类目树,eBay 建映射时**直接复用左手边**(要的是 `amazon node → eBay categoryId` 新表) |

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
    listing_status text, offer_status text, status_reasons text,  -- 对位 published/lifecycle_status、unpublished_reasons
    variant_group_id text, variant_group_info jsonb, policies jsonb,
    last_seen_at timestamptz NOT NULL, missing_since timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, sku));      -- 索引:sku / listing_id / offer_id 各一条
```
**四条必须原样继承的语义**:① 本轮没拿到库存 → `COALESCE(EXCLUDED.x, 旧值)`,**不刷 NULL**;② 缺席**只标不删,永不清理**(`WHERE
last_seen_at < run_at AND missing_since IS NULL`,首次缺席时间不被刷新;所有者 2026-08-12 拍板保守留行);③ 标缺席时**同步清空状态列**
(旧观测在商品消失后保留会误导);④ 缺席后复现 → **平台数字 ID 重置为 NULL 触发重查**。事件账本与 upsert **同事务**落;eBay 事件码必须
先登记进 `EVENTS`(未登记码 `record_many` 抛错),写入带 `platform='ebay'`。
🔴 **蓝图 §5.2 第 1 条给本表加了一条硬要求**:`createOrReplaceInventoryItem` 是**全量覆盖不是 PATCH**(官方原文:更新时该条目当前
已定义的全部字段都必须重给)⇒ **本表必须存全量字段,每次 PUT 从投影表整体重放,禁止"只改变的字段"** —— 这与沃尔玛 feed 的增量语义
**相反**,是最容易翻车的地方。⚠ **原稿那"三格"的现状**(逐条对蓝图 §8.3):(a) `listing_status`/`offer_status` 的**取值封闭集仍未取到**
(§8.3 #9,OAS3 `components` 被截断 + 类型页是 SPA)⇒ **沙箱实调补齐后**才许把字面量写进 SQL(沃尔玛侧 `'PUBLISHED'` 这类字面量散在
约 10 处 SQL,定错要返工);(b) **一个 SKU 多个 Offer 已确认**(Offer 是 sku×marketplaceId×format 维度,蓝图 §5.1)——但**本期只做
EBAY_US 单站点单格式**(见 §七),一行成立,子表 `catalog.ebay_offers` **备着不建**;要上多站点/拍卖时照抄 `multi_node_plan` §5:
合计/代表值留主表(现有消费方零改动),明细进子表;(c) `epid` 大概率大面积 NULL,**不许拿它当身份键**。

### 3.3 `catalog.claims`:现状会跨平台拦死 eBay

现状 `claims_active_uniq (kind, claim_key) WHERE status='active'` = 一个品牌/ASIN **全局**只能一条 active。沃尔玛在架商品的品牌与 ASIN
已被 `alloc_backfill` 倒推占满,且占用**没有自动释放**(只有 `store_release`)⇒ eBay 账号 `try_claim` 必撞 DO NOTHING、被当成"顺延次优
店",**几乎拿不到任何货,而且是永久的**。改造(DDL 与代码**同批**):新索引 `claims_active_platform_uniq (platform, kind, claim_key)
WHERE status='active'` + `ALTER … ADD COLUMN IF NOT EXISTS platform` + `DROP INDEX claims_active_uniq` + `services/claims.py` 五条
SQL 与 `_row()` 校验全改。⚠ 三个坑一个都不能少:① **必须原地替换 schema.sql 里的旧 CREATE INDEX 行**,只追加 DROP 会被下次 `db_init`
全文重跑建回来(顺序对了侥幸没事,错一次就恢复成跨平台拦死);② **新索引必须换新名字**——`CREATE UNIQUE INDEX IF NOT EXISTS` **只按名字
判存在**,沿用旧名会让那行变成静默 no-op;③ `_LOAD` 漏改平台谓词是**最阴的一条**:`load_active` 用 `dict(cur.fetchall())` 压成
`{键:店}`,同品牌两平台各一条 active 时 **dict 静默只留后一条,谁赢取决于行序**;而 `_INSERT` 的 `ON CONFLICT` 推断子句漏改反而**会 fail loud**(PG 直接报错),是好事。

### 3.4 `catalog.upc_pool`:跨平台复用语义

结构事实:**主键是 `upc`,领用信息(store/sku/asin/claimed_at/used_at)是主表裸列 ⇒ 一个 UPC 只能记录一个使用者**,"同产品双平台复用同一
UPC"**结构上装不下**。**S2(推荐起步,零结构改动)**:两平台各领各号——复用键 `(store, asin)` 天然按账号隔离,eBay 账号名 ≠ 沃尔玛店名 ⇒
`claim()` 走"新领"分支;DDL 0(可选加**纯标注列** `platform` 供池子统计与飞书投影分平台展示),代码 0;意外收益:`burn_for_retire` 按
store 过滤 ⇒ 沃尔玛 RETIRE 烧号**天然不会误烧 eBay 的号**;⚠ 代价:**池子消耗翻倍**,"余量不足"告警的注入节奏要跟上,**写进上线检查单**。
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
**推荐做法(便宜且 fail loud)**:`services/order_lines._upsert` 加**平台一致性守卫**——目标行已存在且 `platform` 与来源不同 ⇒ **抛错,不
覆盖**(与该文件既有"兜底不许静默"同款)。⚠ 现实概率极低(沃尔玛 PO 约 13 位纯数字,eBay orderId 带连字符),**但未实测,存疑**。

🔴 **蓝图 §8.2 第 4 条推翻了本节"eBay 也拿 SKU 做身份"这半,必须连带改一处 DDL**:eBay 的**合并订单(Combined Invoice)真实存在**
⇒ 订单行唯一键必须是 `(orderId, lineItemId)`,**不能是 `(orderId, sku)`**。而现表有一条**表级**约束 `UNIQUE (po_id, sku)`
(`refdata/schema.sql:651`),`_upsert` 的 `ON CONFLICT` 却打在主键 `order_line_id` 上(`services/order_lines.py:456`)⇒ **同一 eBay
订单里同 SKU 的两个 lineItem 会算出两个不同的 `order_line_id`、却撞同一条 `UNIQUE (po_id, sku)`;这个冲突不被 `ON CONFLICT` 接住,
整个 `executemany` 事务中止,那一轮订单同步全炸。** 而"合并成一行"这条退路也不通:发货回传(蓝图 §1 #31)是**按 lineItemId 逐行给
数量**的,合并后拿不回 lineItemId ⇒ **标发货必失败**。**推荐**:eBay 行的身份哈希第二段喂 **lineItemId** 而非 SKU(**两个显式函数,
platform 不给默认值**),并把表级 `UNIQUE (po_id, sku)` 换成**两条按平台的部分唯一索引**(walmart:`(po_id, sku) WHERE
platform='walmart'`;ebay:`(po_id, line_number) WHERE platform='ebay'`),照抄 §3.6 范例 B 的 `DO $$` + information_schema 守卫
—— **沃尔玛侧语义逐字不变,且不动哈希**。⚠ "同一 eBay 订单会不会真出现同 SKU 两行"**未实测**,但两个方向代价不对称(不做 = 整轮
订单同步炸;做了 = 多两条索引)⇒ 取严,**列为判据 ⑧,批次 6 的前置**。

### 3.6 schema.sql 改法:照抄既有范例,不自创

| 范例 | 用在哪 | 要点 |
|---|---|---|
| **A · 原地改 CREATE + 追加 ALTER** | `platform` 加列全部照抄 | **两处都写**:CREATE 里一份给新库、`ALTER … IF NOT EXISTS` 一份给已部署库。⚠ **加列的 ALTER 必须排在依赖该列的索引/视图之前**(2026-08-06 生产实证:先建索引会 UndefinedColumn) |
| **B · `DO $$` + information_schema 守卫** | 换唯一索引(claims/dispositions) | ⚠ 嵌套 IF 是幂等的关键——平铺 `AND` 在计划期解析表名 ⇒ 首跑成功、**重跑必炸**(2026-08-13 卡死过 db_init) |
| **C · DROP VIEW + CREATE VIEW**;**D · 存量回填靠 WHERE 保证幂等** | C:视图加列的**唯一**做法(PG 的 REPLACE 不允许改列名);D:给存量行打标记 | ⚠ **新建的任何 eBay 视图禁止建立在别的视图之上**——依赖它就等于给 db_init 埋一个"第二次跑就报错"的雷。D 在本方案里几乎用不上:`NOT NULL DEFAULT 'walmart'` 让存量行**自动正确**,理想情况下**一条回填都不用写** |

## 四、待所有者拍板的判据清单

每条给**推荐默认值**与**两个方向各自的后果**;拍板前不许按推荐值编码。

**① 品牌/产品冻结是否跨平台?推荐:不跨(`claims` 加 platform 维度)。** 线索:占用的业务目的写的是"归属是决策""店铺终止运营 → 释放",没一句
提到防平台关联;而关联封号风险是**出口 IP 维度**,不是品牌维度。判成跨平台唯一 ⇒ **eBay 几乎拿不到任何货**,且占用不自动释放 ⇒ 永久,更糟的是
**它不报错**,表现为"eBay 分配链天天跑、天天分不出东西";判成每平台唯一而业务实际要全公司唯一 ⇒ 同一品牌被两平台同时铺货,品牌方一封信同时打
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

**⑤ 审核链平台化?推荐:列为二期判据,一期先用人工/简化闸,不复用沃尔玛的 `audit_status`。** `products.audit_status` 单列装的是**沃尔玛政策结论**
(37 条 Prohibited Products Policy + PT 准入 + 中国搬运卖家可做性),eBay 违禁规则是另一套(VeRO、Restricted Items、类目 required aspects)。一期
直接复用沃尔玛结论(保守方向)⇒ 只上"沃尔玛已通过"的品,漏放风险低,但**品类被沃尔玛政策白白砍掉一大截**,而 eBay 能卖的很多品沃尔玛本来就不让卖;
一期把 eBay 判决写进 `audit_status` ⇒ 一个 ASIN 一行**装不下两个判决**,后写覆盖先写,`audit_listing_conflicts` 的 `rejected_after_listing` 判错,
且**账混了就分不开**——审核结论不是账本,没有 platform 列可以事后补,**不许"先混着跑跑看"**。二期落法:eBay 判决落 `audit` schema 新表(如
`audit.ebay_verdicts`),`products` 六列**只归沃尔玛**并在 schema.sql 注释里钉死。

**⑥ 🔴 无货源(从亚马逊下单直发买家)这条业务模式,做还是不做?没有推荐值,只有所有者书面拍板。**(**本条为蓝图定稿后补入**,依据
蓝图头 blockquote —— 原稿四节写于蓝图之前,漏了它,而它是全链最硬的一条。)本项目「亚马逊采集 → eBay 上架 → 出单后从亚马逊下单
直发买家」**逐字命中** eBay 明文禁令:"Listing an item on eBay and then purchasing the item from another retailer or marketplace
that ships directly to your customer is not allowed on eBay." [verified drop-shipping id=4176],且与多账号连坐条款 [id=4232]
相乘 —— **一店受限,关联店可能同批受限**。判"照做" ⇒ 爆炸半径是**全部关联账号一起**,不是渐进的;判"换货源合规化"(自有库存 /
供应商直发协议)⇒ 上架链的技术面**不变**(蓝图端点面在两条出路下都成立),变的是选品与履约。⚠ **本条不阻塞任何一行代码**,只阻塞
"起调度真发":批次 1-9 照做、`--dry-run` 与沙箱冒烟不受限,**批次 10 的第一步就是核对本条已书面拍板**。

**⑦ `MARKETPLACE_ACCOUNT_DELETION` 合规入站面放在哪?推荐:单独最小 webhook,不与任何业务链耦合。** 它是 Application Growth
Check 的**门槛项**("能不能跑"而非"跑多快"),需要一个**公网入站 HTTPS 端点** —— 而本项目是"脚本 + launchd + PostgreSQL",
**没有任何常驻公网服务**,这是全项目唯一的入站面。塞进业务链 ⇒ 业务链挂了合规订阅跟着 `MARKED_DOWN`,而**丢事件是静默的**;
不做/不申请豁免 ⇒ Growth Check 过不去,配额与账号数**上不去**。⚠ 是否强制 webhook、有无替代路径**未核验**(蓝图 §8.3 #12),
拍板前先补核。

**⑧ 订单行身份键:eBay 用 `(orderId, lineItemId)` 还是照沃尔玛用 `(po, sku)`?推荐:lineItemId(理由与 DDL 见 §3.5 末段)。**
判成 sku ⇒ 合并订单同 SKU 两行撞 `UNIQUE (po_id, sku)`,**整轮订单同步事务中止**;而合并成一行则拿不回 lineItemId,**标发货必失败**。
判成 lineItemId ⇒ 要换掉一条表级唯一约束(可回滚,范例 B 守卫),沃尔玛侧语义逐字不变。⚠ 触发条件未实测,**取严**。

## 五、开发计划

**总原则三条**:① **批次 0 不通过,后面一条都不许起调度**(判据 ⑥ 是全链阻塞,不是某一个批次的阻塞);② **每批次自带验收,验收
不过不进下一批** —— 验收里"`--dry-run` 人眼确认"这一格**任何写类批次都不许省**(2026-08-16 定稿后默认值不再替你挡,所以更要自觉);
③ **加列与放 eBay 行分两批**(§3 开头那条安全序),库改动的两小批之间可以插任意只读开发。
工作量按**一个熟悉本仓的人**估,单位人日,**不含判据拍板的等待时长与试点观察期**;合计 ≈ 45 人日 + 2 周试点观察。
⚠ 批次号是**依赖顺序不是优先级**(与 `schedule.JOBS` 的 batch 同款约定)。

### 5.0 批次总览

| # | 批次 | 阻塞判据 | 人日 | 可与谁并行 |
|---|---|---|---|---|
| 0 | 判据拍板(无代码) | — | 0.5 | — |
| 1 | registry 登记 + `services/ebay_accounts` + `api/_http` 抽取 + `api/ebay/_client` | ④ | 6 | 2a |
| 2a | 共享表加 `platform` + 谓词补全 + 视图重建(**库里仍只有 walmart 行**) | ①(claims/dispositions 那半) | 3 | 1 |
| 2b | 新建 `catalog.ebay_items` 等 eBay 专属表 + 事件码登记 | — | 2 | 3 |
| 3 | 户口链 + 类目链 + **账号巡检**(三条) | ④ | 4 | 2a / 2b |
| 4 | 最小上架闭环 `ebay_list_new` + 提交台账 + `ebay_submit_poll` | ①②③⑤(⑥ 只挡真发) | 9 | — |
| 5 | 回读 `ebay_catalog_sync` | — | 3 | 6 |
| 6 | 订单链 `ebay_order_sync` | ⑧ | 4 | 5 |
| 7 | 维护链 `ebay_maintenance` + **`ebay_problem_cleanup`** | ①⑤ | 5 | 8 |
| 8 | 售后 `ebay_returns_sync` + **`ebay_returns_action`** | — | 4 | 7 |
| 9 | 结算 `ebay_settlement_sync` | — | 3 | — |
| 10 | 调度上线(`schedule.JOBS` + `skill_export` 再生成 + 单账号试点 → 放量) | **⑥⑦ + 全部** | 2 | — |

**关键路径**:0 → 1 → 3 → 4 → 10。2a/2b 与 1/3 可并行(改的不是同一批文件);5/6 与 7/8 可两人并行;9 最晚做也不挡任何人。
**对给定骨架的三处调整,逐条给理由**(依据蓝图 §2「工作流清单 9 → 12」):批次 3 多一条 `ebay_account_health`、批次 7 拆出
`ebay_problem_cleanup`、批次 8 拆出 `ebay_returns_action` —— 理由分别写在各批次段首,**不是为了多写文件,是三条既有铁律的落点**。

### 批次 0|判据拍板(0.5 人日,无代码)

**目标**:把 §四 ①~⑧ 逐条送到所有者面前,拍完写回 §四并标日期。**拍板前不许按推荐值编码**(§四 开头那句)。

| 判据 | 阻塞哪些批次 | 不拍板就动手的后果 |
|---|---|---|
| ① 品牌/产品冻结跨不跨平台 | 2a(索引换名那半)、4、7 | eBay 分配链**天天跑、天天分不出东西,而且不报错**;占用不自动释放 ⇒ 永久 |
| ② 双平台库存口径 | 4、7 | 同批货被两平台各按"全量可售"挂出,**超卖的第一现场是买家订单**,两边绩效同时掉 |
| ③ UPC 跨平台复用 | 4 | S2→S1 返工要停上架链、动领号事务与烧号,且触及"Unknown 永不回收"生死规则 |
| ④ 每账号固定出口代理 + 账号数量/并发常量 | 1、3 | 多账号同出口 IP ⇒ **关联封号,一次性打掉一批** |
| ⑤ 审核链平台化 | 4、7 | 判决写进 `products.audit_status` 后**账混了就分不开**(没有 platform 列可以事后补) |
| ⑥ 🔴 无货源红线 | **只阻塞"起调度真发"**(10);1-9 照做 | 违规被抓的爆炸半径 = 全部关联账号 |
| ⑦ 合规入站面 | 10(上生产门槛) | Growth Check 过不去 ⇒ 配额与账号数上不去 |
| ⑧ 订单行身份键 | 6(它决定 DDL) | 合并订单同 SKU 两行 ⇒ **整轮订单同步事务中止** |

**验收**:一份带日期的拍板留痕写回 §四(照 CLAUDE.md 里"所有者定稿 2026-08-xx"的既有写法);⑥ 必须是**书面**的。

### 批次 1|registry 登记 + 账号积木 + eBay 唯一出口(6 人日)

**目标**:让 `api/ebay/_client.py` 拿着某个账号的凭证、按蓝图 §4.4 的族路由发出第一个 200。

**前置**:判据 ④ ——它**阻塞 `_normalize` 第三条(代理)过滤的存废与 `EBAY_ACCOUNT_WORKERS` 取值**;判据 ⑦ 不阻塞本批。
`ops.ebay_tokens` 是 `_client` 的自用表、与共享表改造无关,**归本批建**(不等 2b)。

**改动文件清单**:新增 `registry/platforms.py`;改 `registry/resources.py`(§2.4 九类登记里的 ②~⑥ + eBay 账号表 Bitable 条目)、
`registry/paths.py`(`ebay_accounts_snapshot_file()`,⚠ **绝不复用** `stores_snapshot_file()`)、`refdata/schema.sql`
(`ops.ebay_tokens`,范例 A);新增 `refdata/ebay_rate_limits.tsv`(72 行 6 列,已备在 scratchpad,**落仓时不要改 tsv 去迁就蓝图的
80% 定稿值** —— tsv 记官方原值,蓝图记桶配置);新增 `api/_http.py`(从 `api/_client.py` 抽 `_get_client` / `_build_transport` /
`_parse_retry_after` / `rate_acquire`+`_acquire_pg` / `_invalidate_client` / `_close_all_clients` / `download_bytes`)、改
`api/_client.py`(**只改 import,沃尔玛侧调用形态零变化**)、改 `api/__init__.py:6-10` 人读索引;新增 `api/ebay/__init__.py` +
`api/ebay/_client.py`、`services/ebay_accounts.py`;新增 `tests/test_ebay_client.py`、`tests/test_ebay_accounts.py`;同步
`docs/db_schema.md`(`ops.ebay_tokens`,标注**禁止进任何导出/快照**)与 `docs/feishu_tables.md`。

**关键函数面**:蓝图 §7 `api/ebay/_client.py` 那 8 个签名 + §6 十一项横切能力,**不重抄**。三条不许漏:`get_user_token`
**绝不回写 refresh_token**(§4.2 #2,`_client` 里留显式注释);`_RATE_BUCKETS` 桶名带 `ebay.` 前缀,且与 `api/_client.py` 的登记表
**头注互相点名**(§6.5,否则下次扩桶只改一处);`EbayAccountDeadError` 的状态码**一开始就枚举**(§6.6,别等踩到 —— 沃尔玛侧那次
是授权失败回 400 不回 401,导致"一家店凭证坏掉判整轮失败")。`ops.ebay_tokens` 的列见蓝图 §4.2 #5。

**验收**:
1. **单测**:`ebay_accounts` 三层判据各一条(`enabled_names` **不兜快照** / `filter_names` 落空必抛且分「不在册 / 在册但被过滤」两种
   文案 / 快照写完 `chmod 600`);`is_enabled` 三种假值串 + 缺省视为启用;`_normalize` **缺 refresh_token 即跳过**(⚠ 不是缺
   client_id —— 应用级 client_id 全账号共用,拿它判永远为真);`_RATE_BUCKETS` 未登记键**拒绝而非放行**;`safe_post_ex` 默认
   `max_retries=0`;**令牌刷新响应不含 `refresh_token` 时旧值不被洗成 None**(这条直接钉蓝图 §4.2 #2 那个坑)。
2. **sandbox 冒烟(本批的硬验收)**:`api.sandbox.ebay.com` 用 `client_credentials` 取应用令牌 → `GET
   /developer/analytics/v1_beta/rate_limit/` **200**,打印 `reset` 实值(顺手补核蓝图 §8.3 #4「日配额归零时刻」,以实值为唯一判据,
   **别信午夜传说**),并 dump 一次完整响应头(补核 §8.3 #14「是否真带非文档化限流头」)。
3. **回归**:`pytest tests/` 全绿 —— `api/_http.py` 抽取是**纯搬家**,任何一条红都说明搬错了。
4. **`--dry-run` 人眼确认**:`python cli.py ping_stores --dry-run` 输出与抽取前**逐字一致**(沃尔玛链未受影响的人眼证据)。

⚠ 本批**不碰任何 `sell.*` 端点**:那要用户令牌,而用户令牌要先人工走一次同意页(蓝图 §4.1「吊销后必须重走同意,无法自动化」),
同意页归批次 3 的 runbook。

### 批次 2a|共享表加 `platform` + 谓词补全(3 人日)

**目标**:把平台维度加进共写表,**此时库里仍只有 walmart 行,行为必须逐字不变**。

**前置**:判据 ①(它决定 `claims` 唯一索引是换成 `(platform, kind, claim_key)` 还是不动)。⚠ **① 未拍板时本批可先做
`product_events` / `listing_sources` 那两张**(与 ① 无关),claims/dispositions 那半留到 ① 落定 —— 这是本计划里唯一一处"判据阻塞
可以切开做"的地方。

**改动文件清单**:`refdata/schema.sql`(四条 `ALTER … ADD COLUMN IF NOT EXISTS platform`,范例 A **CREATE 与 ALTER 两处都写**、
且 ALTER **必须排在依赖该列的索引/视图之前**;claims/dispositions 唯一索引**原地替换 + 换新名 + DROP 旧索引**,范例 B;5 个视图
DROP 重建,范例 C);`services/claims.py`(五条 SQL + `_row()` 平台校验 + 三个签名)、`services/dispositions.py`(`ON CONFLICT`
推断子句 + `claim()` 压制判定**按平台圈定** + `_SETTLE_DELETE_SQL` 的 `DISTINCT ON`)、`services/product_events.py`
(`record_many` 的 `platform` **必填** + `_VERIFY_SQL` 两段 CTE 加谓词)、`services/listing_sources.py`(`register` 加必填
platform);谓词补全(§3.1 那 12 处读 SQL 里视图之外的那些,⚠ **动手前现场重数一遍**,行号来自同日底稿不是本文复核):
`services/blacklist.py` 的 `_LATEST_CTE`、`workflows/problem_scan.py` 三条 SQL、
`workflows/sku_normalize.py`、`workflows/audit_history_fold.py`(显式写 `'walmart'`)、`services/maintenance_intents.py` 四处
JOIN、`workflows/sources_backfill.py`;同步 `docs/db_schema.md`。

**关键 DDL**:见 §3.1/§3.3/§3.6,**不重抄**。三个坑一个都不能少:原地替换旧 `CREATE INDEX` 行(只追加 DROP 会被下次 `db_init`
全文重跑建回来)/ 新索引**换新名**(`CREATE UNIQUE INDEX IF NOT EXISTS` 只按名字判存在)/ `_LOAD` 的平台谓词(`load_active` 用
`dict(cur.fetchall())` 压成 `{键:店}`,漏改会**静默只留后一条,谁赢取决于行序**)。

**验收**:
1. **单测**:同 `claim_key` 两平台各能拿一条 active(① 判"不跨"时);`dispositions.claim()` 的 eBay delete **不压制** walmart 同
   SKU 维护组,且**与两个扫描件谁先跑无关**;`record_many` 未登记 platform 值抛错;`listing_sources.register` 漏传 platform
   **抛错而不是静默落 walmart**。
2. **既有读 SQL 回归(本批的核心验收)**:改动前后对 5 个视图与 12 处读 SQL 各跑一次 `count(*)` 与
   `md5(string_agg(…::text, '|' ORDER BY …))`,**逐条一致** —— 此时库里没有 eBay 行,任何差异都是谓词写错。
3. **EXPLAIN**:`catalog.audit_listing_conflicts` 必须仍走 `product_events_identity_idx`(§3.1 那行记着 2026-08-14 它把生产库
   查挂过一次,表达式索引要与查询逐字一致)。
4. **`db_init` 幂等两跑**:真跑 → **再跑一次**,第二次零报错零变更(范例 B 嵌套 IF 的唯一验收方式;2026-08-13 就是重跑炸的)。
   ⚠ **本仓一个现成的坑,本批顺手补**:`workflows/db_init.py:15` 是 `DANGEROUS = False` 而 `run()`(:37-41)**根本不读
   `dry_run`** ⇒ `python cli.py db_init --dry-run` **不是空跑,是真跑 schema.sql,而且不报错**。所以库改动的"人眼确认"**不能靠
   `--dry-run`**,只能靠 `git diff refdata/schema.sql` + 影子库先跑一遍;**建议本批给 `db_init` 补上 `dry_run`(只打印将执行的
   语句块)** —— 这是 §2 "现有沃尔玛文件一律不动"的一处显式例外,理由:它是 infra 不是沃尔玛业务件,且这条坑正是 CLAUDE.md
   点名的那一类。
5. 沃尔玛链 `pytest tests/` 全绿 + `python cli.py maintenance_scan -p preview=1` 输出与改前逐字一致。

### 批次 2b|新建 eBay 专属表(2 人日)

**目标**:把 eBay 侧要写的表建齐,**空表**。**前置**:无(纯新增,与 2a 无依赖,可并行)。

**改动文件清单**:`refdata/schema.sql`(`catalog.ebay_items` 见 §3.2 草案 + 三条索引;`audit.ebay_category_map`(amazon node →
eBay categoryId,**左手边直接复用现有 `audit.amazon_taxonomy`/`node_paths`**);`audit.ebay_aspects_cache`(整站 aspects 落库);
条件件 `catalog.ebay_offers` —— **本期不建**,形态见 §3.2 第 (b) 格)、`services/product_events.py`(eBay 事件码进 `EVENTS`)、
`docs/db_schema.md`。

**关键点**:🔴 **事件码必须在 `EVENTS` 与 `_FEED_KIND` 两处同时登记** —— `feed_kind()` 对未登记类型**回落 `feed_type.lower()`**,
eBay 的新提交类型会拼出比如 `ebay_offer_feed_success`,不在 `EVENTS` 里 ⇒ `record_many` 抛 `ValueError`,**整轮回写炸掉**。
事件码继续**只有一份注册表**(分成两份就会重演"`maintenance_submitted` 发了大半个月没登记"那种漏)。

**验收**:1) **`db_init` 幂等两跑**零报错零变更;2) `\d catalog.ebay_items` 列序与 §3.2 逐列对齐;3) **单测**:对每个 eBay 提交
类型跑一遍 `feed_kind()` → 拼出的两个事件码都在 `EVENTS` 里(**这条测试就是防上面那颗地雷**)。

### 批次 3|户口链 + 类目链 + 账号巡检(4 人日)

**对给定骨架的调整**:本批多一条 `ebay_account_health`(蓝图 §2 新增的第 12 条工作流)。**理由**:① **selling limit 余量是批次 4
的硬准入输入** —— 蓝图 §3.3 定稿"真正的天花板是你能上多少货,不是你能调多少次";② refresh token 到期巡检是 **eBay 独有的必需品**
—— OAuth **没有** `HardExpirationWarning`(那是 Auth'n'Auth 旧令牌才有的 7 天预警),**eBay 不会提醒你 refresh token 快过期**。
两者都必须早于任何写链上线,所以不能拖到批次 10。

**目标**:把"这个账号能不能发东西"的三块地基落库 —— 三条商务政策 + 库存地点 + 类目树/aspects,外加每日巡检。

**前置**:批次 1;判据 ④;**一次人工 runbook**:浏览器走一次 consent → refresh_token 落 `ops.ebay_tokens`(蓝图 §4.1,无法自动化)。

**改动文件清单**:`api/ebay/account.py`、`api/ebay/taxonomy.py`、`api/ebay/trading.py`;`workflows/ebay_bootstrap_account.py`
(`DANGEROUS=True`)、`workflows/ebay_taxonomy_sync.py`(`DANGEROUS=False` ⇒ **必须读 `params["dry_run"]`**)、
`workflows/ebay_account_health.py`(`DANGEROUS=False`,同上);`services/ebay_aspects.py`;`tests/test_ebay_taxonomy.py`、
`tests/test_ebay_account_health.py`。

**关键函数面**:蓝图 §7 那三个模块;端点见 §1 #3-#8(户口)、#9-#13(类目)、#7/#44/#43(巡检)。三条口径不许自创:缓存是
**版本比对不是"缓存 N 小时"**(§8.2 #7);**只读 `aspectRequired` 不读 `aspectUsage`**(§5.2 #3);selling limit **只存「上限 +
剩余 + 采样时间」,不写死日限/月限周期语义**(§3.3,官方三比一冲突)。

**验收**:
1. **单测**:`get_default_tree` 版本未变时**不发第二个请求**(4,000/day 是最紧的桶之一);aspects 解析只认 `aspectRequired`;
   `ebay_account_health` 在 `refresh_expires_at - now < 30 天` 时产出**显式停链告警**,⚠ **绝不能让 workflow 静默跳过该账号**
   (蓝图 §2 原话;否则重演"每天空转而且报成功")。
2. **`--dry-run` 人眼确认**:`ebay_bootstrap_account --dry-run` 必须**逐行打出**"将建哪三条政策、哪个 merchantLocationKey、
   将 opt-in 什么",不是"少干活"。
3. **sandbox 冒烟**:sandbox 账号跑通 opt-in `SELLING_POLICY_MANAGEMENT` → 三条政策 → `createInventoryLocation` →
   `getPrivileges`,四件产物落库;⚠ `getCategorySuggestions`(#13)**官方明示 sandbox 不支持**,该函数的冒烟只能挪到批次 10 的
   生产单账号试点。

⚠ 本批产物是批次 4 的**硬门槛**:**缺 `merchantLocationKey` 时 `createOffer` 不报错、`publishOffer` 必失败**(§5.3);
三条政策**全部必须存在并挂到 offer 上**(⚠ 反证记录:某次抓取归纳出过"Inventory API 不需要商务政策",那是摘要模型的自行推断,
**以逐字引语为准:必须建**)。

### 批次 4|最小上架闭环(9 人日,全程最大的一批)

**目标**:一个 ASIN 从中立选品走到 eBay 在架 listing,且**提交前先落库**。

**前置**:批次 1/2a/2b/3 全部;判据 ①②③⑤;**判据 ⑥ 只挡真发,不挡开发**(sandbox 与 `--dry-run` 不受限)。
⚠ **eBay 倍率表待拍板且待建**:`ebay_price()` 读的是 eBay 自己的限额表列,由所有者建列填值(与 `multi_node_plan` 的「维护仓库」
同款治理:所有者建列 → registry 登记字段常量 → 代码直读);**表没建之前本批只能跑 `--dry-run`**。

**改动文件清单**:
- **先做抽取(零业务改动,做完沃尔玛链行为必须逐字不变)**:新增 `services/listing_copy.py`(从 `mp_mapper` 抽 `scrub_brand` /
  `_clean_copy` / `sort_images` / `_sentences` / `force_amazon_copy` / `title_spec_compatible`,mp_mapper **原处 import**);
  `services/pricing.py`(`landed_price`/`parse_multiplier` 转中立、`pick_band` 改收 `bands`);`services/variant_group.py`
  (`pick_walmart_dims` → `pick_target_dims`)+ `services/variant_title.py`(`MAX_LEN` 收参数)—— ⚠ **这是全仓改名,涉六个文件,
  越早改越便宜**。
- **新增 services**:`ebay_item.py`(载荷组装)、`ebay_conform.py`(aspects 必填/枚举/多值校验,**放在 publish 前**)、
  `ebay_admission.py`、`ebay_price.py`、`ebay_listing_sheet.py`、`ebay_submit.py`(三层防重的 eBay 载体)。
- **新增 api**:`api/ebay/inventory.py`、`api/ebay/offers.py`。
- **台账**:`refdata/schema.sql` 给 `ops.feed_log`/`feed_items`/`feed_item_errors` 加 `platform` 且 `feed_id` 泛化 `submission_id`
  (§3.1 那行,蓝图 §5.4 已冻结)+ `services/feed_track.py` 按平台过滤 + 五个反哺器(listing/maint/clear/match/blacklist_sheet 的
  `sync_from_ledger`)同改。
- **新增 workflows**:`ebay_list_new.py`、`ebay_submit_poll.py`(**两条都 `DANGEROUS=True`**)。
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
   GTIN 缺失走**站点替代文本表**而不是英文字面量;pending 行**永不老化**且摘要摊开 `账号/类型/工作流/提交时间`。
2. **`--dry-run` 人眼确认(不可省)**:照抄 `list_new.py:1258-1273` 的形态 —— 前 15 条被拦理由 + 前 10 行"将提交什么(定价/库存/
   类目/aspects)" + 一行 `[DRY-RUN] 共 N 行将进入 领UPC→校验→提交`,摘要行首 `🧪 [DRY-RUN] `。
3. **sandbox 冒烟**:1 个 SKU 走完 item → offer → publish → `getOffer` 回读 `listingId`;再跑一遍**同载荷**,确认被在途/终态防重挡住。
4. **sandbox 必测两条未核验项**(蓝图 §8.3 #3/#6,**直接改代码分支**):重复 `createOffer` 是报"已存在"还是**建出第二个 offer**
   (决定 NOT_FOUND 补交前要不要先 `getOffers?sku=` 反查);`bulkUpdatePriceQuantity` 的 25 到底数什么(实测前一律按 25 条记录切片)。
5. **UPC 余量检查单**:S2 起步 ⇒ **池子消耗翻倍**,"余量不足"告警的注入节奏要跟上,**这一格必须进上线检查单**(§3.4)。

### 批次 5|回读 `ebay_catalog_sync`(3 人日)

**目标**:全店 item + offer 对拍,回填 `catalog.ebay_items`,缺席只标不删。

**前置**:批次 2b + 批次 4 的 api 面(`iter_items`/`iter_offers`/`bulk_get_items`)。**不依赖上架真发** —— sandbox 手工建的
listing 就够验收,所以本批可以在判据 ⑥ 悬着的时候照做。

**改动文件清单**:`services/ebay_catalog.py`(§2.6 说的"整条链的地基,先建它")、`workflows/ebay_catalog_sync.py`
(`DANGEROUS=False` ⇒ 读 `dry_run`)、`tests/test_ebay_catalog.py`;`services/product_events.py` 接线(事件与 upsert **同事务**)。

**关键点**:§3.2 **四条必须原样继承的语义**(COALESCE 不刷 NULL / 缺席只标不删且 `missing_since` 首次值不被刷新 / 标缺席时同步
清空状态列 / 复现时 `listing_id` 重置 NULL 触发重查);端点 §1 #17/#23/#16。

**验收**:1) **单测**四条语义各一条(照抄 `tests/test_catalog_sync.py` 的形状);2) **`--dry-run` 人眼确认**摘要第一行是"结论 +
最重要的那一个数"(链通知只显示第一行);3) **sandbox 冒烟**:建 2 条 listing → 扫描 → 下架 1 条 → 再扫描 → 第三轮再扫,确认第二轮
**标缺席不删行**、第三轮 `missing_since` **首次值不被刷新**。

### 批次 6|订单链 `ebay_order_sync`(4 人日)

**目标**:`lastmodifieddate` 真增量拉单,落进 `orders.order_lines` 的 eBay 那一维。

**前置**:🔴 **判据 ⑧ 未拍板不许建行** —— 它决定 DDL(见 §3.5 末段)。

**改动文件清单**:`refdata/schema.sql`(三张表加 `platform` + `(platform, store, order_date DESC)` 索引 + **`UNIQUE (po_id, sku)`
换成两条按平台的部分唯一索引**,范例 B 守卫)、`services/order_lines.py`(按平台**两个显式**身份函数 + `_upsert` 平台一致性守卫)、
`api/ebay/orders.py`、`workflows/ebay_order_sync.py`(`DANGEROUS=False`)、`services/ebay_order_center.py` + eBay 订单飞书表条目。

**关键点**:端点 §1 #29/#30/#31;⚠ **`lastmodifieddate` 绝不与 `creationdate` 同传**(后者优先、前者被静默丢弃);时间窗**封顶
90 天**、深历史按 orderId 点取(§3.2/§8.3 #5 官方两处冲突);`orderfulfillmentstatus` 只有两种合法组合,**单值别自创**(§8.2 #1);
**未完成 checkout 的单根本不出现在 Fulfillment API 里**(与沃尔玛"Created 即可见"不同);🔴 **买家脱敏已生效且辖区含"China (and
its territories)"**(§8.2 #3):`buyer.username` 返回的是 immutable userId,**禁止当买家自然键、禁止字符串比对**;🔴 **GSP/eIS
双地址结构未核验**(§8.3 #1,**拿反 = 全部国际单寄错地址**),沙箱实拉一单 GSP 订单看真实 JSON 才能定面单取哪个地址。

**飞书推送复用评估(本批要给结论)**:`services/order_center.py` 的六张表载荷全按沃尔玛订单形状(采购订单号/绩效/对账账期)⇒
**不复用同一张表**(§2.6「别在同一张飞书表里加 eBay 列」,列序即契约);但 `sync_by_key`「只覆盖 fields 里给出的列」那套机制照用,
eBay 另建表条目 —— 并照抄 `ORDER_SALES` / `ORDER_SALES_AUDIT` 那个"一张表两个所有权条目"的技巧,让拉单**永远冲不掉审核结论**。

**验收**:1) **单测**:同 `(po, sku)` 不同 platform 的 upsert **抛错不覆盖**;合并订单同 SKU 两 lineItem **各落一行**;
`iter_orders` 同时传 `creationdate` **直接抛**;`buyer.username` 不参与任何键。2) **既有读 SQL 回归**:沃尔玛订单链改前后
`count(*)` 与最近 7 天摘要**逐字一致**(动了 `orders` 域的唯一约束,这条不能省)。3) **单账号试点**:拉一个真实窗口,**人眼核对
1 单**的地址与金额;GSP 单单独核一次。

### 批次 7|维护链 + 破坏动作唯一出口(5 人日)

**对给定骨架的调整**:拆出 `ebay_problem_cleanup`(蓝图 §2 第 9 条)。**理由**:withdraw / deleteOffer / deleteInventoryItem /
republish 是**破坏动作**,CLAUDE.md 08-24 定稿「**破坏动作只有一个出口**」「破坏组存在即压制同 SKU 的维护组」—— 塞进
`ebay_maintenance` 等于把 08-19 那条"说不清是哪条链干的"事故重演一遍,而且 `dispositions.claim()` 的压制判定在 eBay 侧**没有落点**。

**目标**:`ebay_maintenance` 只做 title/price/inventory(MAINT_ACTIONS);`ebay_problem_cleanup` 独占 withdraw≈retire /
delete≈delete / republish≈relist(PROBLEM_ACTIONS)。

**前置**:批次 4/5;判据 ①⑤;⚠ **第四种 action 要先定名**(§3.1 dispositions 行:资格未获批被下架者**禁止 relist**,三值表达不了)。

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

**验收**:1) **单测**:破坏组存在即压制同 SKU 维护组,且**与两个扫描件谁先跑无关**(顺序不许承载判据);破坏组内部 withdraw +
delete 同 SKU **不合并**(顽固件双通道齐发,合成一条会让一个的落定覆盖另一个);领任务**只看 `action` 不看 `source`**;合并行的
每个来源各占 `sources` 一格、**全空才 withdrawn**。2) **`--dry-run` 人眼确认(两条链都必须,破坏链尤其)**。3) **单账号试点阶梯**:
先只放 title → 观察一天 → 再放 price/inventory → 最后才放破坏组。

### 批次 8|售后 `ebay_returns_sync` + `ebay_returns_action`(4 人日)

**对给定骨架的调整**:拆成"只读同步"与"不可逆处置"两条。**理由**:`DANGEROUS` 是**模块级**开关 —— `DANGEROUS=False` 读 `dry_run`、
`True` 读 `execute`,**两套判据不能共存于一个 `run()`**;混在一条链里会让 `--dry-run` 的语义在同一个函数里分叉。

**前置**:批次 6(退货挂在 `orders.return_lines` 上,要先有订单行)。

**改动文件清单**:`api/ebay/postorder.py`;`workflows/ebay_returns_sync.py`(`DANGEROUS=False`)、
`workflows/ebay_returns_action.py`(`DANGEROUS=True`);`services/ebay_returns.py`;`refdata/schema.sql`
(`orders.return_lines` 的 eBay 列:两套枚举各一列);`tests/test_ebay_returns.py`。

**关键点**:端点 §1 #33-#39;**创建时间窗全量重扫 + 本地未闭合单点查,双管**(§3.2 的直接理由:**三个 search 全无 last-modified
参数**,而退货状态可以在创建 **18 个月**后仍在变,窗口重扫只捞新单 —— 沃尔玛靠窗口重拉就能覆盖状态迁移,eBay 必须多这一步);
**退货 `status` 与 `state` 是两套枚举(25 值 vs 43 值,部分同名语义不同),两列都存**,合并即重演"source 当 action 用"的事故
(§8.2 #5);`REPLACEMENT_*` 12 值官方自述 "not currently supported",**可收录枚举但不要写分支逻辑**;**取消链可以"不作为"**
(不响应则 3 个工作日后自动拒绝,批准后 FVF 自动退回,§8.2 #6);GSP 硬约束 34200/34300 + **发运后不可增删 fulfillment 行**;
EU/UK `issueRefund` 需数字签名(**美站不需要**,若上欧站签名做在 `_client`)。

**验收**:1) **单测**:未闭合单点查**必须有本地态过滤**(否则各 4,000/day 见底);两套枚举分两列落;`decide`/`refund` 在
`execute=False` 时**一条请求都不发**。2) **`--dry-run` 人眼确认**(处置链)。3) **单账号试点**:先只跑 `ebay_returns_sync` 观察
三天,再放 `ebay_returns_action`。

### 批次 9|结算 `ebay_settlement_sync`(3 人日)

**目标**:payout 驱动的三级闭环(订单 ↔ 交易 ↔ 放款)。**前置**:批次 6。

**改动文件清单**:`api/ebay/finances.py`、`workflows/ebay_settlement_sync.py`(`DANGEROUS=False`)、
`services/ebay_settlement.py`、`refdata/schema.sql`(`orders.settlement_lines` 加 `platform`,批次 6 已加则复用)、
`tests/test_ebay_settlement.py`。

**关键点**:端点 §1 #40-#42;**payout 驱动**——先 `getPayouts`,再按 `payoutId` 拉 transaction,🔴 **禁止按日全量扫 transaction**
(12,000/day 是第二紧的桶);**放款周期不写死成常量,按 `payoutDate` 事件驱动**(§8.2 #2,官方文档取不到周期);⚠ 对账链
**不得依赖买家支付明细**(脱敏后美国买家一律 `CustomCode`,§8.2 #3);⚠ 费率是函数不是常量、**计费基数含运费含税**(§2.6 那条
修正)——对账差异先怀疑基数,别先怀疑接口。

**验收**:1) **单测**:`iter_transactions` 缺 `payout_id` 且缺日期范围时**直接抛**(防"按日全量扫"复活)。2) 与
`orders.settlement_lines` **对拍一期**:笔数与金额合计一致。3) **单账号试点**:核对一次真实放款的三级链路。

### 批次 10|调度上线(2 人日 + 2 周试点观察)

**第一步不是写代码,是核对两条门槛**:判据 ⑥ 已**书面**拍板(蓝图头 blockquote:拍板前 eBay 上架链不得起调度);判据 ⑦ 的合规
入站面已就绪或已拿到正式豁免(Application Growth Check 的门槛项)。

**改动文件清单**:`registry/schedule.py` 的 `JOBS` 加条目 → `python cli.py skill_export --dry-run`(先跑这个)→ 真跑 →
`skills/walmart-schedule/` 生成物**进 git 但不手改**;launchd 那半 `python cli.py launchd_install --dry-run`;
`docs/schedule_plan.md` 的人读投影同步。

**排法(照 `schedule.py` 的 batch 语义,不是优先级)**:批次 1(只读/低危)= `ebay_taxonomy_sync` / `ebay_catalog_sync` /
`ebay_order_sync` / `ebay_returns_sync` / `ebay_settlement_sync` / `ebay_account_health`;批次 2 = `ebay_submit_poll`;
批次 3(破坏性,**每条先手动 `--dry-run` 人眼确认再 load**)= `ebay_list_new` / `ebay_maintenance` / `ebay_problem_cleanup` /
`ebay_returns_action`。`ebay_bootstrap_account` **一次性,不进 `JOBS`**(蓝图 §2 #1;归 `schedule.py` 里那份"不在表里 = 手动"的清单)。

**三条硬纪律**:同一条链**绝不 launchd 与 gpt 两边都挂**(撞上了后到的退 3 空跑一轮**而且报"成功"**);每条 workflow 的
docstring 第一行是技能包那一格的**唯一出处**,写成 `"""ebay_xxx — 一句话说清干什么(危险性)。"""`,取不到就留白**不编**;
⚠ **eBay 链动 `product_ingest` 游标必须借同一把锁**(`runlock.hold("product_ingest")`)—— 两个进程各自落 `next_cursor`,后写的
盖掉先写的,中间那段记录**永远不会再被拉一次,而且两侧都不报错**。

**验收**:
1. `tests/test_gpt_skill.py` 三条钉子全绿(仓库副本 == 从调度表现渲染 / 无遗留任务文件 / 不许丢参数);
   `tests/test_launchd.py` 那条"只有高频链住在这台机器上"仍绿。
2. **`--dry-run` 人眼确认**:`skill_export --dry-run` 与 `launchd_install --dry-run` 各看一遍。
3. **单账号试点 → 放量(阶梯不许跳)**:先只挂**一个账号的只读链**,观察 3 天 `ops.runs` 的时长与失败率 → 再挂写链、观察 3 天 →
   再放量到全部账号。
4. **放量前再跑一遍 `getRateLimits` 对表**:确认按"应用身份"计的桶在多账号下没被打穿 —— **蓝图 §8.3 #2 那条判错就在这一刻现形**
   (按账号算而实际按应用算 = 超配额 429 打全店)。⚠ 对表**不做准入判据**(官方未声明刷新延迟,必须当作可能滞后的数据)。
5. 试点期每天人眼看一次飞书通知的**第一行**(链通知只显示第一行,第一行不成结论 = 这条链的摘要写错了,当场改)。

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
   零限速跑了数月);`refdata/ebay_rate_limits.tsv` 进 git 作为来源之一,**两者不一致以蓝图 §3 为准**(tsv 记官方原值、蓝图记桶
   配置,本就不同)——⚠ 该文件**尚未入仓**,批次 1 落。⚠ 蓝图里全部 `[verified]` 都是**转引调研底稿的原文引语**,"转引"这一层
   没有第二双眼睛 ⇒ 三条 🔴 级结论(整体覆盖语义 / `aspectRequired` vs `aspectUsage` / `refresh_token` 不轮转)**实现前重开原页
   各看一眼**;§8.3 那 17 行未核验项动手前逐条补核。
4. **AI 改完代码必须先 `--dry-run`,人眼确认输出后才跑真的。** 缺省即真跑(2026-08-16 定稿),默认值不再替你挡;摘要行首带 `🧪 [DRY-RUN] ` 前缀。
5. **services 新增积木前先通读现有函数确认无重复**;每个函数 docstring 第一行写清"输入什么 → 输出什么";事件码/平台名/字段名一律**只有一个出处**,
   未登记即抛(三处清单各漂各的、`maintenance_submitted` 发了大半个月没登记,已吃过两次)。
6. **动了表就同步 `docs/db_schema.md`**,新增飞书表同步 `docs/feishu_tables.md`;改调度只改 `registry/schedule.JOBS` 再重跑 `cli.py skill_export`
   (**先 `--dry-run`**),**不手改 `skills/` 生成物**;⚠ 同一条链**绝不 launchd 与 gpt 两边都挂**(撞上了后到的退 3 空跑一轮,**而且报"成功"**)。
7. ⚠ **判据不许承载在调度顺序上**:eBay 链与沃尔玛链若有数据依赖,依赖写进判据,顺序只做优化(`dispositions.claim()` 的压制判定是先例:顺序改了
   结果也不变)。⚠ **eBay 链动 `product_ingest` 游标必须借同一把锁**(`runlock.hold("product_ingest")`)——两个进程各自落 `next_cursor`,后写的盖掉
   先写的,中间那段记录**永远不会再被拉一次**,而**两侧都不报错**。

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

⚠ **"本期不做" ≠ "永远不做",但更不等于"先留个开关"**:上面每条都**不许在代码里留半成品分支或双轨开关**(明禁的"双轨过渡遗留");
要做时新起一个批次,走同一套验收。

## 八、风险登记簿

登记规则:每条给**触发形态(尤其"会不会报错")→ 现有防线 → 缺口与处置**。⚠ 本仓最贵的教训都是"不报错"那一类,所以这一列写在最前。

| # | 风险 | 触发形态 / 会不会报错 | 防线与处置 |
|---|---|---|---|
| 1 | **refresh token 过期或被吊销,需人工重授权** | 18 个月不轮转,而 OAuth **没有** `HardExpirationWarning`(那是 Auth'n'Auth 旧令牌才有的 7 天预警)⇒ **eBay 不会提醒你**;卖家改密码/改登录名 → 该账号全部 refresh token **当场被吊销**。表现:该账号所有写调用 401 自愈一次后仍失败。🔴 **若 workflow 写成静默跳过该账号,就是"每天空转而且报成功"** | `ebay_account_health` 的 `refresh_expires_at - now < 30 天` 飞书预警(批次 3)+ `EbayAccountDeadError` **显式停链告警**;`scopes` 列用于"改了 scope 必须重授权"的比对。运维 SOP 照抄本仓既有纪律:**先停调度 → 改密 → 浏览器重走同意 → 落库 → 起调度** |
| 2 | **多账号关联连坐** | 官方确认多账号合法,但 "If we apply restrictions or limits to one account, then similar restrictions or limits may be applied to the member's other linked accounts" [id=4232];**关联判据官方从不公开**,所以事前无信号、事后一次性打掉一批 | 每账号固定出口代理(判据 ④,**取严**)+ **代理按域名放行不按目的地 IP**;缺代理即整账号跳过。⚠ 与风险 4 相乘:违规被抓的爆炸半径 = 全部关联账号。⚠ 文档措辞:**不许说成"eBay 按 IP 判定关联"** |
| 3 | **双平台超卖** | 亚马逊侧观测(`snapshots.stock_count`)两平台共读、可售数量各自算 ⇒ 同批货被两边各按"全量可售"挂出。**不报错**,第一现场是买家订单,补救成本是两边绩效指标同时掉 | 判据 ②(各自限额 + 共享观测,**先定防超卖规则再上链**);`NULL ≠ 0` 铁律照抄(下游禁止 `or 0`)。⚠ 实时扣减器本期不建(§七 #7) |
| 4 | 🔴 **无货源模式本身违规** | 逐字命中 eBay 明文禁令 [id=4176];被抓的表现是账号级限制("Create new listings or revise existing listings" 受限),**不是条目级** ⇒ 逐条重试会把整批打成假失败 | 判据 ⑥ 所有者**书面**拍板(阻塞起调度,不阻塞开发);提交台账的**账号级健康位**,命中即整账号熔断;放量走批次 10 的阶梯以控爆炸半径 |
| 5 | **新账号 selling limits 冷启动** | 官方示例即 `$100 / 10 件`;⚠ **日限还是月限官方三比一冲突** ⇒ 代码只存「上限值 + 剩余值 + 采样时间」,**不写死周期语义**;提额是站内**人工流程无 API** | `ebay_account_health` 每日采 `getPrivileges`(上限)+ `GetMyeBaySelling`(余量,REST 无等价物,是全仓唯一一条 Trading 调用);🔴 `store_limits` 的「读不到就回落默认值」在 eBay 侧**必须反转成 fail-closed** —— 读不到还照上 = 直接撞"绕限制"违规 |
| 6 | **eBay 风控冷启动** | 新账号 + 大批量新 listing + 中国卖家画像叠加。表现:listing 被限、账号级失败、类目资格未获批下架 —— 后者**官方明文禁止 relist**,而现有 retire/delete/relist 三值**表达不了这一态** | 放量阶梯(批次 10 验收 #3);`dispositions` 新增第四种 action(§3.1);VeRO 只新增 `source`、**不新开执行出口** |
| 7 | **配额口径判错(按 App vs 按卖家账号)** | 蓝图 §8.3 #2 未核验。判错方向不对称:按应用算而实际按账号算 = 白白慢;**按账号算而实际按应用算 = 超配额 429 打全店** | 已按"应用身份"取严;批次 10 放量前**再对一次表**——这是它现形的唯一时刻。⚠ 对表**不做准入判据**(官方未声明刷新延迟) |
| 8 | **OAuth `client_credentials` 1,000/day 打爆** | 🔴 **最硬的一条**:App 级共享,**一炸全账号全链条一起炸**;不落库时刷新次数 = 进程数 × 账号数,而本仓一天几十条 workflow 各自启停 | 令牌**落库**(`ops.ebay_tokens`)+ 提前 300s 续 + 单飞(`SELECT … FOR UPDATE` 或复用 flock)+ **应用令牌按 scope 集合做缓存 key**(key 不含 scope 会拿窄 scope 令牌调宽 scope 端点拿 403) |
| 9 | **刊登费是真实固定成本** | 非订阅仅 250 条/月免费,超出 $0.35/条(一万条 ≈ $3,412/月);沃尔玛侧**无此项**。不报错,只是月底账单 | **"能上就上"的铺货策略不能沿用** —— 属策略问题,登记在此防遗忘;费用归集交给结算链(上架链不承担单 SKU 成本核算:`getListingFees` 只能查未发布 offer 且按 marketplace 汇总不分摊) |
| 10 | **限速状态漂成两份** | `ops.rate_events` 是同一张表,eBay 桶名若不带平台前缀就与沃尔玛的 `inventory.put` 撞名 ⇒ **两个平台互相扣对方的配额**;⚠ 另:`_is_persistent` 的判据(窗口 ≥600s)会把 **eBay 全部日窗口桶推进 PG** ⇒ 每次调用打一趟 PG,**PG 挂 = eBay 全停(fail hard)** | 桶名 `ebay.` 前缀;两处 `_RATE_BUCKETS` 登记表**头注互相点名**;fail hard **要么接受、要么显式改判据,不许静默降级进程内** |
| 11 | **蓝图 §8.3 的 17 条未核验项** | 逐条影响不同批次:🔴 #1 GSP 双地址(拿反 = **全部国际单寄错地址**)→ 批次 6;🔴 #2 配额口径 → 批次 10;🔴 #3 重复 `createOffer` 行为 → 批次 4 的补交逻辑;🔴 #16 Compliance/Analytics API 面 → 批次 7 的指标来源;#5 时间窗 90 天还是 2 年 → 批次 6 冷启动 | **动手前必须补核,不许按推断编码**;补核方式蓝图逐条给了(多数是沙箱实调)。补核结果**回填蓝图 §8.3 并标日期** |
| 12 | **共享表并跑对象是沃尔玛链** | eBay 是新平台没有旧系统并跑问题,**但它碰 `catalog.products/snapshots`、`ops.rate_events`、`catalog.product_events`、`orders.*`** ⇒ "新旧严禁并跑"这条铁律的对象变成了沃尔玛链 | 批次 2a 的"加列时库里仍只有 walmart 行 + 行数逐条对拍";批次 6 的"沃尔玛订单链改前后摘要逐字一致";eBay 动 `product_ingest` 游标必须**借同一把锁** |
