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
答"该谁干"——`PROBLEM_ACTIONS`(delete/retire/relist)归
`problem_product_cleanup`,`MAINT_ACTIONS`(title/price/inventory)归
`maintenance`。**破坏动作只有一个出口**;破坏组存在即压制同 SKU 的维护组,
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
