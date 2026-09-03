# 工程规范与判据全文(CLAUDE.md 的展开层)

> **规则本身以 `CLAUDE.md` 为准**(它每次会话必读);本文件存放各条规则的
> 完整依据、事故史与展开细节,按需查阅。两处冲突时以 CLAUDE.md 为准并修本文件。
> 2026-08-26 从 CLAUDE.md 迁出(瘦身:常驻上下文只留规则与指针)。

## 一、安全铁律的事故背景

- **缺省即真跑(2026-08-16 改默认值)**:此前是"危险工作流缺省 dry-run,真跑加
  `--execute`"。改的理由:进了调度之后,"缺省 dry-run"这条防线只会伤到自己 ——
  launchd 里漏写一个 `--execute` 的后果是**那条链每天空转而且报成功**,比误跑
  更难发现(误跑至少留下痕迹)。`--execute` 保留为兼容别名(空操作),调度里
  写了也不会错。"AI 改完代码必须先 `--dry-run`,人眼确认输出后才跑真的"这条
  **没有取消**,只是从"默认值兜底"降级成"纪律"——默认值不再替你挡,更要自觉。
- **防重状态先落库再调接口**:提交 feed 前先写 pending,成功后改 done;程序
  重启时,所有 pending 记录先去 Walmart 查实际状态再决定是否补交。
- **新旧系统严禁对同一破坏性任务并跑**:切换某条工作流时,先停旧调度 →
  搬状态 → 起新调度。

## 二、店铺状态三层判据(所有者定稿 2026-08-22)

「这家店还在不在营」只有一个判据:`services/stores.enabled_names()`
(在册 ∧ 凭证表勾了「启用」)。三层别混:

| 函数 | 回答的问题 |
|---|---|
| `registered_names()` | 在不在**册**(连停用的都算) |
| `enabled_names()` | 在不在**营**(唯一在营判据) |
| `load_stores()` | 现在能不能**调 API**(还筛 ClientId/代理) |

用错的两个方向都会出事:拿 `load_stores()` 判在营,「在营但代理没配」的店
会被当成死店而整店下线;拿 `registered_names()` 判在营,停用的店照样占着
品牌、照样用冻结行拦着别的店上架。
第四维「本轮数据新鲜不新鲜」是 `services/store_absence`(缺席 ≠ 停用,
判据从 `catalog.walmart_items` 每店 `max(last_seen_at)` 水位派生)。

## 三、处置建议路由(所有者定稿 2026-08-24)

「这条处置建议该谁执行」只看 `action`,不看 `source`。`ops.dispositions`
一张表两条链共用:`source` 答"为什么建议"(maint/scan/audit/tro),`action`
答"该谁干"——`PROBLEM_ACTIONS`(delete/retire)归
`problem_product_cleanup`,`MAINT_ACTIONS`(title/price/inventory)归
`maintenance`(relist 2026-08-28 所有者定稿退役:非 PUBLISHED 一律删除,
不再改 End Date 救商品;存量 relist 行不在任何领取集,由 withdraw/settle 收尾)。**破坏动作只有一个出口**;破坏组存在即压制同 SKU 的维护组,
压制在 `dispositions.claim()` 里判,**与两个扫描件谁先跑无关**(调度顺序
不许承载判据);破坏组内部**不合并**(retire+delete 同 SKU 是顽固件双 feed
齐发,合成一条会让一个的落定覆盖另一个)。合并行的每个来源各占 `sources`
一格,撤销只删自己那一格、全空才 withdrawn。

拿 source 当执行者用的下场:08-19 生产实见一行「维护链执行 + 审核链原因」的
维护记录,谁也说不清是哪条链干的;更早的后果是同一个 SKU 被两条链先后删了
两次(提交期防重按**整批载荷指纹**算,两条链批次不同就撞不上)。

## 四、店维工作流的失败处理标准(所有者定稿 2026-08-26)

起因:两家店 SOCKS 代理报 "Malformed reply"(socksio 异常不在 httpx 异常树上,
落进泛化桶)放倒 product_chain 八步。标准四步:

1. **单店异常隔离**:一家店的失败不中断其它店。
2. **串行补试**:跑完别人后,失败店串行补试一遍(`services/store_retry`;
   StoreDeadError 不补试——凭证死是确定性的;退避走 `_client.backoff`
   唯一阶梯;失败店数超 max(3, 总数//5) 判系统性故障,止损点名不补试)。
3. **缺席不炸整轮**:补试仍失败,工作流照常报成功,摘要**首行**点名缺席店
   与归类词(链通知只发成功步骤的首行);零店完成仍必须失败;daily_report
   链例外(`catalog_sync:strict=1`,同步不全就不出日报)。下游店维工作流按
   目录水位避让缺席店(`services/store_absence`,不建新表、不靠调度顺序);
   两个执行件同样避让(缺席店的存量 suggested 留在原地)。
4. **链尾重赛**:cli 对缺席店把链内声明 `SUPPORTS_STORE` 的步骤逐店重跑一次,
   **再失败即止**。四道闸:主链带 store= 范围参数不重赛/长期缺席(落后船队
   >72h)只点名不逐日空跑/今日缺席超 5 店判系统性故障/重赛后按水位复核,
   步骤全绿但水位未推进不发假 ✅。

配套判据:`cap_destructive` 按日记账(当日已放行先扣,重赛不把「下架限制」
翻倍);`settle_maintenance` 2 小时落定宽限(防"太早看"被判"未生效")。
SOCKS 层报错由 `_client.get_token` 收口成 `StoreProxyError`(⊂
httpx.ProxyError,现有分类分支天然接住)。

**失败归类词唯一出处 `store_retry.diagnose`**,六档各指一条处置路,
进各工作流摘要首行:

| 归类词 | 判据 | 处置 |
|---|---|---|
| 凭证失效 | StoreDeadError(沃尔玛拒凭证) | 修凭证表 |
| 代理无效 | 代理服务器拒认证(username/password、407) | 修代理账号密码 |
| 代理波动 | SOCKS 隧道其他故障(Malformed reply/断线) | 找代理商;补试/重赛常自愈 |
| 沃尔玛NNN | 端点回 HTTP NNN(429=配额、5xx=沃尔玛侧) | 看配额/等沃尔玛 |
| 网络未达 | api 层重试耗尽仍未打到(状态码 None) | 查该店代理链路 |
| 其他 | 以上都不是 | 看该工作流日志 |

分诊只影响**报告**,不影响重试行为(补试与否只看异常类型)。

## 五、判某样东西"没用了"之前(2026-08-14 全项目盘点的教训)

全项目死代码盘点做过一轮:**仓库是干净的**,真能删的只有 4 处代码。但同一轮里
**10 条被判死的东西反证后全是活的**,险些误删生产链路。三条缺陷务必内化:

1. **`grep 不到调用者` 对 workflow 完全无效。** `cli.py` 是
   `importlib.import_module(f"workflows.{args.workflow}")`,无白名单——活性不在
   import 图上,而在**"它写的表还有没有别的补给线"**。正确检索式:
   `grep -rn 'INSERT INTO <schema>\.|UPDATE <schema>\.|COPY <schema>\.' --include=*.py .`
2. **"docstring 自述一次性 + 文档记 `[x] 已跑" ≠ 死。** 迁移期脚本有三种状态:
   已跑完**且数据源已冻结**(才可能死,本仓一条都没有)/ 已跑过**但数据源仍在
   生产增长**(活)/ **从未跑过、在批次待办里**(活)。
   ⚠ **"查不到执行记录"在本仓是"还没跑"的证据,不是"跑完被遗忘"的证据**——
   本仓记录纪律良好,跑过的都有 `[x]`。
3. **按名字 grep 双向出错**:有假阳性(同名局部变量)、也有假阴性(注释里提到
   函数名会让它看起来活着)。**必须 AST 引用集 + 文本 grep 双做,每个命中
   人眼确认是调用还是注释。**

**判不准就判活。** 误删一条生产链路的代价,远大于多留一个死文件。
DROP TABLE/COLUMN/VIEW 不可回滚,**未连库核对 `pg_stat_user_tables` /
`pg_stat_statements` 之前一律不执行**——"代码不读"不等于"没人 SELECT",
本仓有一批视图/列就是专门留给人工与 AI 排查用的。

## 六、同一目的多种方法的取舍(简化 vs 兜底)

- **纯历史重复**(同端点多套写法)→ 只留蓝图选定的一种,其余不迁,零兜底。
- **能力不同的两个端点**(如单品 PUT vs 批量 feed)→ 两个显式函数,由 services
  层显式 if 路由;严禁"试 A 失败自动落 B"式隐式降级。
- **真兜底**(外部 API 自身缺口,如 offset 截断补漏、飞书挂走快照)→ 三要件:
  藏在同一个函数内、触发必须记日志计数(兜底静默常态化 = 主路径已坏没人知道)、
  触发条件明确而非 catch-all。
- **双轨过渡遗留**(旧 shim 开关式并存)→ 新系统一律禁止,每个能力只有一条
  实现路径。
- **写操作永不自动兜底**:失败只走 ops.feed_log 反查三态 → 确认未达 → 同一方法
  补交。换方法重试 = 重复提交制造机。

- **POST 的 `outcome=unknown` 一律保持 pending**(SKU 改造批次 3 补,2026-09-02):
  不回滚、也不补交,留给 `api/feeds` 的启动对账与下一轮的观测定案。unknown 的语义
  是「不知道到没到」——「写操作永不自动兜底」管的是**不许换姿势重发**,这一条管的
  是**不许把不确定当成失败去撤销自己这边的状态**:沃尔玛若其实已经受理,回滚就造出
  一条我们这边没有记录的孤儿状态,而且全程不报错。**人不在环时宁停不重。**
  (与"确认未达"要分清:4xx 与 token/代理阶段失败是 `api/feeds` 已经判定的确认未达,
  那种可以安全回滚;`_PRE_FAIL` 与 4xx 都落 `outcome='failed'`,unknown 是第三态。)
- **`abandoned_at IS NULL` 的允许出现处**:规则正文在 §九②(消费方 .py 四处:
  `sku_codec.mint` / `list_new` 去重闸 / `alloc_push._SQL_ONLINE` / 批次 3 起的
  `sku_migrate._SQL_CANDIDATES`;`refdata/schema.sql` 的部分索引条件是 DDL 不计入)。
  **规则的家只有一个** —— 这里只留指针,守门白名单(`tests/test_sku_guard.py`)的条目
  与 §九② 的文字必须逐字对得上,否则就是"三种口径互相判红"。

口诀:兜底是补偿外部世界的缺陷,不是补偿自己的不确定。

**通知排版标准件的定位**(所有者定稿 2026-08-27):`services/notify_fmt`
是通知文本公共积木之家 —— 采纳标准件、按实际情况推行,不做全仓化妆式
重写;新写/大改的工作流起用 `head/summary`;已收口的成品尾巴
(`feed_outcome_tail`/`absent_tail`)只管排版,进飞书的字样由调用方给,
积木不替所有者统一措辞。

## 七、沃尔玛配额高危速记(明细以蓝图 §3 为准)

价格三件套 feed 共享桶:官方 10/hour,2026-08-26 三源复核后按 8/小时配置
(6/day 只属本仓不用的 feedType=promo)。本仓在用的其余 feedType:
MP_ITEM/MP_MAINTENANCE/DELETE_ITEM/inventory 各自独立 10/hour,
MP_ITEM_MATCH 20/hour,RETIRE_ITEM 官方无值按 DELETE 同档保守——
**不可外推到未列的 feedType**(官方限额分六档,20/hour、50/hour、6/day 皆有)。
单品价格 PUT = 100/小时;Insights performance 类 1/分钟;`GET /v3/items` 带
query 参数 60/分钟。响应头 `x-current-token-count` 与
`X-Next-Replenishment-Time` 用于自适应退避(api/_client.py 已内置,勿自行实现)。
维护链单店单轮意图上限 = (按店速率桶−1)×单 feed 切片
(`services/maintenance_intents.MAX_INTENTS_PER_STORE`,三处一致有测试钉住)。

## 八、飞书读写限额速记(所有者定稿 2026-08-27)

**取值 = 官方限制 × 95%,向下取整**(官方 5000 行 → 本仓 4750 行);官方另给
了更严的自荐值时**取更严者**(单元格:硬上限 50000×95%=47500,但官方自荐
40000 → 取 40000)。官方没给数字的用工程预算值并**明写「工程值,非官方」**
(写请求体预算 9,000,000 字节 = 读侧 10MB 的 ~86%,余量补偿 JSON 估算误差)。
**全部限额常量只在 `api/feishu.py` 顶部的「限额登记表」出生**,每条行内注释
三件套:官方原值 | 官方 URL | 核对日期。官方原句对照全表(含「官方未说明」
项,与 walmart_rate_limits.tsv 同款纪律)在 **`refdata/feishu_limits.tsv`**,
与登记表出自同一次调研,改一处同步另一处。守门在 `tests/test_feishu_guard.py`
(通道外零端点字面量 / 小范围薄壳只接小范围 / 限额常量不许在登记表外出生)。

**读写各只有一条通道**:写 `sheet_write_ranges`(底座 `_sheet_put`,
`sheet_overwrite` 等一切写路径都落到它)、读 `sheet_values_rows`(按行分块);
「已知小范围」(表头/单行/固定几行)走薄壳 `sheet_values_small` —— 它不分块
不兜底,范围上界随表长增长的读取一律不准用它;裸读 `_values_raw` 是私有的,
只有那两条通道能调。别的写法一律不新开(§六「每个能力只有一条实现路径」)。

**当轮写完**(所有者原话「当轮写完不留下一轮」):写通道逐段累加,批内总行数
满 4750 / 估算字节满 9,000,000 / 段数满 100,任一先到即封批提交,**剩余继续
循环,本次调用把段全部写完才返回**。攒到下一轮 = 悄悄少写,而调用方拿到的
行数还是全量,对不上账。批间节流 0.3s + 同表串行锁,后者有官方原句背书
(「单个文档只能串行调用」——并发不是慢,是被明令禁止)。

**两层字符闸分工别混**(总控裁决 2026-08-27):

- `_SHEET_CELL_MAX_CHARS`=20000 是**业务脏数据闸**:超了**截断 + 告警、
  轮次照走**。采集来的标题/描述超两万字必是脏数据,不能因为一行脏数据把整轮
  写入炸掉 —— 这是既有能力,不许被硬闸吃掉。
- `_SHEET_CELL_HARD_MAX_CHARS`=40000 是**通道硬闸**:超了**直接抛
  ValueError**(错在本仓调用方,不在飞书)。对着官方自荐值定,进到它那儿的
  东西根本不是表格数据,截断只会把 bug 藏进飞书。
- 故清洗路径 `sheet_write_ranges` 的顺序是**先截断后硬闸**,硬闸在该路上对
  字符长度天然不触发,只剩列数 >95 这条结构错误;**40000 硬闸的真正岗位在
  不清洗的 `sheet_overwrite`**(那条路不 scrub:KPI 看板靠写数字配
  formatter)。列超限**分批救不了**(切批只切得出行),所以是抛不是切。

**兜底只做保险丝,不做主防线**(三要件见 §六:同函数内 / 触发记日志计数 /
条件明确非 catch-all):写侧预算失算撞 90227 时,对该批**对半重切一次**再发,
`logger.warning` + 进程内计数(`_oversize_retries`),两半里再有一半失败即抛;
读侧同理,90221 对半重读并记日志。主防线是上面的预算,不是这根保险丝。

**四个错误码分工**(别照码去猜阈值):

| 码 | 含义 | 处置 |
|---|---|---|
| 90221 | **读**超:单响应超官方 10MB(data exceeded) | 按行分块;单块仍超则对半重读,记日志 |
| 90227 | **写**超:请求体过大(TooLargeRequest,官方不给阈值) | 主防线是行/字节/段数预算切批;对半重切一次兜底,记日志计数 |
| 99991400 | **频控**(旧版 OpenAPI 限流时 HTTP 码是 400 不是 429,判据只能认 code) | 优先按官方 `x-ogw-ratelimit-reset` 头精确等(上限 60s 工程值),无头退回 1/2/4/8 阶梯 |
| 99991403 | **月度 API 配额耗尽**,不是频控 | **不可重试**:自然月 1 号才刷新,退避/换 token 都不会好,继续重试只是把剩下的额度也烧掉 —— 直接抛并提示升级版本或等下月 |

⚠ 90204 实证 2026-08-05:增/删行列超量时飞书报的是 90204,不是超限码。

## 九、SKU 身份口径(SKU 改造批次 0a 建立,2026-09-02;后续批次只补条目)

背景:SKU 从「就是 ASIN」变成 12 位不透明码之后,「这条 walmart_items 记录对应
哪个源头产品」不再能从 SKU 本身看出来,身份必须过登记簿
`catalog.listing_sources`。收口只值钱一次,**守不守得住**才决定它三个月后还在不在
(守门 `tests/test_sku_guard.py`,与 `tests/test_feishu_guard.py` 同族同形态)。

**① 身份表达式只有两条可复制的字面量**(别再发明第三种写法):

- SQL 侧:`coalesce(ls.source_key, w.sku)`,其中 ls 是
  `... JOIN catalog.listing_sources ls ON ls.store = w.store AND ls.sku = w.sku
  AND ls.source_type = 'amz'`(按取数语义选 LEFT / INNER)。
  - `source_type = 'amz'` **不许省**:match 行的 source_key 是匹配 GTIN,拿它去撞
    `products.asin` 语义上就是错的(存量下结论碰巧相同,不构成等价性论证)。
  - 用 `coalesce` 不用裸 `ls.source_key`:register 允许 source_key 缺省,NULL 键的
    amz 行今天靠 `p.asin = w.sku` 命中,裸取会把它们静默丢掉。
  - **相关子查询里不要写 coalesce**:对 products 每行做的 EXISTS 用不上
    `walmart_items_sku_idx`,几十万行候选会退化成逐行全表扫(2026-08-14 视图挂死
    同一类事故)。那种位置改写成两条腿的 OR,各走各的索引。
- Python 侧:`sku_asin.pick_asin(source_key, sku)`。**两条腿同口径**:登记簿键也要
  先 strip+upper 再过裸 ASIN 形态闸,不过就落回 `extract_asin(sku)` ——
  「登记簿只是优先级,不是免检通道」。全表级取数一律在 SQL 里 JOIN 取键,别拿
  十万对 (store, sku) 去 `unnest`。

**② `abandoned_at IS NULL` 的权威白名单**:消费方 .py **只有三处** ——
`sku_codec.mint` 的复用查询、`list_new` 的本店去重闸、`alloc_push._SQL_ONLINE`
(批次 3 起增 `sku_migrate` 的候选选取为第四处)。`refdata/schema.sql` 里那几条
部分索引条件是 DDL,不计入这张白名单。**resolve / 维护链 JOIN / 事件归并 /
订单反查一律不按它过滤** —— 旧码带着订单、售后回来时必须还查得到。

**③ 不透明码编码规则的唯一之家是 `services/sku_codec.py`**:字母表 / 长度 /
随机段长 / 重抽上限 / 占位码 / `is_opaque` 判据都在那里出生;registry 只登记
`SKU_SOURCE_LETTERS`(所有者要拍的取值,属外部配置)。schema.sql 两条部分唯一
索引的字符类与该模块常量由守门测试逐字对齐。理由:铁律 3 管的是路径 / token /
表 ID / 服务器地址这类**外部资源**,12 位码的字母表是**内部编码规则**。

**③′ SQL 侧的形态判据也只有一处** `sku_codec.OPAQUE_SQL_PREDICATE`(由字母表与
长度**派生**,消费方 `.format(col="w.sku")` 拼进自己的 SQL)。任何 `.py` 里再手打
一份 12 位字符集正则即违规(守门 `test_no_second_opaque_regex_in_the_repo`);
`refdata/schema.sql` 的两条部分索引条件是同源的另一半,由守门逐字对齐。

**④ 登记簿的写入出口**:INSERT 只有 `listing_sources.register` 与
`sku_codec.mint` 家族两个(批次 3 起 mint 家族含 `mint_replacement`);
`abandoned_at` / `abandoned_reason` / `replaced_by` / `replaces` / `replaced_at`
**五列**只准 `services/sku_codec` 写(`abandon` / `mint_replacement` /
`settle_replacement`)。行永不 DELETE。

**④′ UPDATE 有两条写线,写的列不许交叉**(所有者 2026-09-03 补):除了 ④ 那五列,
`source_type` / `source_key` **两列**的唯一修改入口是
`services/listing_sources.reclassify`(唯一调用方 `workflows/sources_reclassify`,
`-p apply=1` 才写;写入前 `source_key` 必须过 `sku_asin.is_standard_asin`)。
交叉了两种后果都静默:归类那条顺手清 `abandoned_at` = 把死码拉回自动化(下一轮
新码新 UPC 去上同一个 item);弃码那条若能改 `source_key` = 身份键在一次弃码里被
悄悄换掉,按 ASIN 反查的消费方当场失明。守门
`test_the_two_registry_update_lines_do_not_cross` 逐条钉死,白名单条目与本节文字
必须对得上。
⚠ 归类的语义是**把商品交还自动链**(改完才第一次满足消费方
`source_type='amz' AND source_key IS NOT NULL` 那条 JOIN),纪律与
`sources_backfill` 同款:改完先 `maintenance_scan --dry-run` 看破坏面。
它与 ⑥ 那三个同名异义并列的第四组辨析是:**归类**(改出身,SKU 不变)≠
**首次登记**(register,补一条不存在的行)≠ **改码**(换沃尔玛侧的 SKU 本身)。

**⑤ 守门只有一份** `tests/test_sku_guard.py`:白名单 dict 在文件顶部,每条写清
理由与**预期收口批次**,永久豁免显式标 permanent。后续批次只准增删这里的白名单
条目,**不许再建第二份守门文件**(四份并存正是 §六要禁的形态,而且白名单一定会
互相打架)。

**⑥ 三个同名异义,别混**:「码弃用(abandoned)」≠「沃尔玛 lifecycle RETIRED」
≠「product_clear 停用」。登记簿列名故意用 abandoned 不用 retired。

**⑦ 码的寿命与四个弃码点**(批次 2 接线,2026-09-02):

- **码的寿命** = 沃尔玛侧那条 (店, SKU) 记录对我们还有用的寿命,**不是上架/下架
  次数**。登记簿的行**永不 DELETE**;`abandoned_at IS NULL` 的行叫**活码**。
- **弃码只有一个实现**:`services/sku_codec.abandon(conn, store, sku, reason)`。
  它在同一事务里做三件事:标登记簿三列 → 按分派表烧号 → 记码级事件
  (`sku_abandoned` / `sku_replaced`,detail 必带 `source_key`)。
- **弃码点只有四个**(守门 `_ABANDON_CALLERS_OK` 逐条登记,多一个即红):

  | # | 在哪 | 触发 | 烧号状态 |
  |---|---|---|---|
  | 1 | `workflows/catalog_sync.py` | DELETE 经**观测核验** `delete_verified` | `burned_delete` |
  | 2 | `workflows/sku_locked_heal.py` | SKU_LOCKED 自愈 RETIRE **回执成功 + 冷却期满** | `burned_lock` |
  | 3 | `services/listing_sheet.py` | UPC 撞库 `ERR_EXT_DATA_0101119`(决策 B:码与 UPC 一起换) | `conflict` |
  | 4 | 批次 3 `workflows/sku_migrate.py` | 改码 SkuUpdate 经观测确认 | **不烧**(item 还在、UPC 还绑着) |

  第 1 点**不按回执弃**:「回执成功但后台没删」是所有者实证过的故障模式;
  第 2 点是四点中唯一绑回执的一个(锁死的 SKU 可能从未进过 walmart_items,
  没有观测可等)。烧号唯一写入函数 `upc_pool.burn(conn, pairs, status)`,
  状态只由 `sku_codec._BURN_STATUS` 给(决策 D)。
- **其余一切「下架」都不弃码**(五项,守门 `_ABANDON_FORBIDDEN` 反向钉死):
  `product_clear` 停用(RETIRE)、库存归零、缺席 `missing_since`、被沃尔玛
  unpublish、提交失败/被拒/Unknown/PROHIBITED —— 沃尔玛侧记录仍在、仍绑着我们的
  UPC,抽新码 = 同店两条同内容记录 + 白烧一个 UPC。
- **mint 的复用语义**:同 (店, 来源, 源头键) 有活码就复用它(依据是沃尔玛
  「一个 Product ID 只能挂一个 SKU」),所以失败行下一轮拿到的是同一个码,载荷
  一字不差 ⇒ `api/feeds` 的 payload_key 在途防重仍然有效。
- **三条护栏跟码走**:重试上限、在途防重、UPC 原号复用都按当前活码计数 ——
  每换一次码,三条全部重新开始。这正是**代际上限闸**(同 (店, 来源, 源头键)
  已弃码行数 ≥ `MAX_SKU_GENERATIONS`)与**退役冷却闸**
  (`RETIRE_COOLDOWN_HOURS`)要堵的闭环;两个常量的唯一之家是
  `services/sku_codec.py`(守门 `test_cooldown_and_generation_constants_have_one_home`)。
- **跨店永不复用码**:同一个码串在两家店合法,但那正是"两家店有关联"的信号,
  而关联就是封号线(schema.sql 的 `listing_sources_opaque_sku_uidx` 拦它)。
- **改码(批次 3 地基,2026-09-02)三个函数、两条指针、三态**:
  `mint_replacement`(先落库:新码行 `replaces`=旧码 + 旧行 `replaced_by`=新码,
  同一事务,**commit 归调用方**;幂等 —— 崩溃重入拿回同一个码,换码 = 载荷变了 =
  payload_key 防重不命中 = 同一个 item 被改两次)、`settle_replacement('confirmed')`
  (旧行走 `abandon(reason='sku_update')`,**不烧 UPC**,并给新码记一条出生事件)、
  `settle_replacement('rolled_back')`(清旧行指针 + 新码 `abandon('sku_update_failed')`)。
  `sku_update_failed` 是词表里的**第五个原因但不是第五个弃码点** —— 它弃的是我们
  自己刚抽、从未上过沃尔玛的新码。回滚作废的新码行**保留 `replaces` 当病历**,但
  不再占旧码的认领位(索引与视图 `catalog.sku_aliases` 的条件都带 `abandoned_at
  IS NULL`);不这样的话同一个旧码这辈子只能改一次码,而失败会被误诊成"随机撞码"。
- **代际继承只有一处出生**:视图 `catalog.sku_aliases`(新码 → 它继承的旧码,
  **只继承一跳**)。五处按 (店, SKU) 读历史的判据一律经它取别名,不许各自现写
  `replaces` 的 JOIN(守门 `test_only_sku_aliases_expresses_the_replacement_chain`)。
- **发码只有一条路**:`sku_codec.mint`。跟卖侧旧的 `PHUMWMT + 日期 + 序号`
  生成器已于批次 2 删除(守门 `test_no_second_sku_generator_survives`),
  跟卖表 B 列人工号仍然优先,人工号走 `listing_sources.register` 在**提交前**登记。

**⑧ 回执事件码不写字面量**:`{kind}_feed_{status}` 是推导出来的,具名常量在
`services/product_events.py`(`RETIRE_FEED_SUCCESS` / `DELETE_FEED_SUCCESS`)。
`_FEED_KIND` 一改取值,写字面量的那条 SQL 会**静默返回空集** —— 闸门形同虚设
而且不报错。守门 `test_no_receipt_code_literals_in_business_sql` 扫 services/ 与
workflows/。

**⑨ 派工闸与去重闸同口径,占用口径故意不同**(决策 C,2026-09-02):
`workflows/alloc_push._SQL_ONLINE` 与 `list_new` 的去重闸都判「没缺席 + 码还
活着」,**不筛 lifecycle**;`services/alloc_survey._SQL_ONLINE` 保持筛 RETIRED
不变(它答的是"有没有活货位")。两处都有反向守门,**不要顺手统一**。

**⑩ 不可逆的写必须认 `--dry-run`**:弃码与烧号都没有撤销路径。`feed_poll` 的
`DANGEROUS=False`(不调沃尔玛写接口)⇒ cli 恒传 `execute=True`,所以它**自己读
`params["dry_run"]`** 并把 `execute` 透传给五个反哺器;五个反哺器一律带
`execute: bool = True` 关键字(守门 `test_every_reflector_takes_an_execute_flag`),
`execute=False` 时一行飞书、一行 PG 都不写。
