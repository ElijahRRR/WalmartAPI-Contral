# feed 台账 v2:每种结局都落到有事实依据的终态(设计草案)

> **2026-09-25 更新:方向已由所有者修正 —— feed 结果与实际结果分开。以「〇」节为准;第一至九节是 09-24 草案原文,
> 与〇节冲突的部分作废(清单见〇.5)。〇节已全部拍板并实施(〇.6 / 〇.7;pending 即 Q3 同日批)。**
>
> **状态:草案,未实施,待所有者拍板(2026-09-24)。** 所有者原话:「在提交 feed 后,对于每种 feed 的查询和落帐……
> 每种可能在数据库里都要有有事实依据的终态」。
>
> 产出方式:两轮只读工作流。第一轮 12 个代理分 6 个范围读码(提交层 / 查询落账层 / 上架 / 跟卖改码 / 破坏 / 维护),
> 每个范围一名对抗复核员逐条核实,得 147 条断头路(确认 144、驳回 3)+ 复核员补 34 条;第二轮聚成 29 个根因簇,
> 三种角度(台账先行 / 观测定案 / 重发安全)各出一版设计,两名评审打分(胜者:台账先行),合成后经完整性审查修订。
> 引用的 file:line 以分支 claude/stoic-lovelace-far5o3 @ 86bbf0e 为准。生产数据相关的数字来自所有者贴的通知与既有文档,本设计未连库核对。

事实来源编号(全文通用):① 沃尔玛逐 SKU 回执 ② feed 级回执(汇总)③ HTTP 事实(POST 状态码/响应体、GET 404)
④ GET /v3/feeds 列表反查 ⑤ catalog 观测(catalog_sync)⑥ 单品 GET /v3/items/{sku} ⑦ 官方时限(refdata/walmart_slas.tsv)。

## 〇、所有者 2026-09-25 修正:feed 结果与实际结果分开(本节优先于下文)

所有者原话:「以前的设计有点问题,把 feed 结果和实际结果混在一起了,但实际上是两个东西,应该分开。feed 结果是为了
知道沃尔玛侧的执行情况,而实际结果是为了让开发人员或者运营知道自己之前做的操作实际上在沃尔玛是否成功生效。我对于
feed 的初步想法是,提交 feed,追踪 feed 直至该 feed 的最长期限(有些 feed 可能 15 分钟就生效,有些可能要 24 小时,
不同的 feed 根据官方的说明用不同的期限),达到最长期限还没有完全落定的,就查询明细(正常情况下在总终态落定后也会
查询,这里相当于限制了最长时间)来落定。对于实际结果,就只需要生效/未生效。如果 feed 显示该明细是成功的,但是观测
结果是未生效,这种是人需要看的。但是观测结果很多其实和实际情况在时间上可能是错开的,因为可能存在一开始生效但是后来
消失了,在结果观测的视角里只有未生效这一条。混在 feed 结果里,也会显得比较乱,一开始设计这个主要为了自动化的上架和
删除,因为沃尔玛存在 feed 显示成功,但实际上却没有生效的情况,方便后续再自动处理。但其实目前来说没有必要。」

### 〇.1 两本账,各答一个问题

| | feed 结果 | 实际结果 |
|---|---|---|
| 回答什么 | 沃尔玛说它执行了什么 | 线上到底变没变 |
| 唯一来源 | feed 接口:汇总 ② + 明细 ①(读不到时记 ③) | 观测 ⑤(必要时 ⑥ 单查) |
| 取值 | SKU 级:成功 / 失败 / 明细无此条 / 超期未完成 / 未知状态 / 无法查询 | 只有 生效 / 未生效(附观测时刻与依据) |
| 何时定 | 汇总终态后读明细落定;最迟到 feedType 期限读明细强制落定 | 期限后第一次观测判一次,之后不改 |
| 给谁用 | 各工作流、飞书反哺表 | 人:「feed 成功 ∧ 未生效」进复核清单 |

- 两本账互不写对方:feed_poll 不读观测;实际结果不改 feed 账。
- 实际结果**不挂新的自动化**;现有挂在观测上的自动动作逐条由所有者定留 / 停(〇.4)。
- 时间错位是观测的固有属性(先生效后消失,观测只看到未生效):实际结果只记"哪次观测、何时、看到什么",
  不推断中间发生过什么。

### 〇.2 feed 结果:按类型期限收口(提案)

**期限表**(所有者 2026-09-25:「期限按官方值、不加余量,请实际查看官方给的期限值,不猜测,不凭记忆回答」;
当天逐页抓官方原文重核,原句与 URL 见 refdata/walmart_slas.tsv;同一操作给了几个数时取最长;起点 = 拿到
feedId 的时刻,即 `feed_log.updated_at`):

| feedType | 官方口径(2026-09-25 核) | 期限(已实施) |
|---|---|---|
| price(及将来的 PRICE_AND_PROMOTION) | 批量改价 SLA 15 分钟(旧版、新版两页同句) | 15 分钟 |
| inventory / MP_INVENTORY | Seller Center:最快 15 分钟,最长 4 小时(开发者文档无时限) | 4 小时 |
| MP_ITEM / MP_MAINTENANCE | 处理最长 4 小时;**单条合规审核最长 24 小时**(审核期间 INPROGRESS);Seller Center 状态更新最长 24 小时。例外:危险品合规审核 3 个工作日、GTIN 豁免人工审核不给时长 | 24 小时 |
| MP_ITEM_MATCH | 美国站最长 24 小时。**09-12 登记的「无法导入时最长 72 小时」美国站页面已无**,只剩加拿大站 | 24 小时(原提案 72) |
| RETIRE_ITEM | catalog 最长 48 小时(开发者文档与 Seller Center 同) | 48 小时 |
| DELETE_ITEM | 48 小时内删除;最长 72 小时从 Catalog 消失(开发者文档无时限) | 72 小时 |

feed_poll 每 30 分钟一轮,实际收口发生在期限后的第一轮(改价:提交后 15~45 分钟)。期限常量只在一处出生,
feed_poll、实际结果、各在途闸都读它。

**每轮规则**:

1. 读汇总。汇总终态(PROCESSED / ERROR)或已过期限 → 读明细;否则本轮不读明细。
2. 明细里有结论的 SKU 当场落定,**首次落定即定稿**:之后再读不改状态、不改 resolved_at。顺带修掉终态残留 feed
   每轮把全部行的 resolved_at 刷成当下、problem_scan 在途闸把约 970 个 SKU 永远当"待观测"的旧毛病(C03)。
3. 汇总终态:SUCCESS / *_ERROR 当场落成功 / 失败;明细里查无的当场落「明细无此条」(即现在的 missing,
   消费方处理不变);全部有结论 → feed 收口(依据:汇总终态)。仍有 SKU 在处理、或状态值不认识 → 期限前每轮继续读明细。
4. 到期:最后读一次明细,剩下的强制落定 —— 仍在处理或明细里查无 → **超期未完成**(原始状态照存:INPROGRESS /
   明细缺席);不认识的状态值 → **未知状态**(原值照存)。feed 收口(依据:到期)。
5. 读不到:期限前任何错误(含 404)都只算"下轮再读";期限后 404 → **无法查询**(沃尔玛已不保留该 feed);其他错误
   (按 store_retry.diagnose 六档归类)、以及店铺已不可调用(停用 / 凭证删除),期限后再宽限 24 小时仍读不到 →
   **无法查询**(记最后一次错误归类)。
6. 每个 SKU 终态带依据:收口方式(汇总终态 / 到期 / 无法查询)、沃尔玛原始 ingestionStatus、读取时刻 —— 记在
   feed_items 行上(feed_log 行会被同载荷重占覆盖,依据不放那里)。
7. feed_log 仍是 pending / submitted / done / failed 四个词,不新增终态。
8. **pending**(POST 结局不确定、没拿到 feedId):期限内每轮只读反查(GET /v3/feeds,现成的 find_recent_feed),
   查到 → 收编 feedId 转 submitted 接着追;到期仍查不到 → failed(**提交未确认**),记反查次数与最后结果。
   **不自动补交**(红线:写操作永不自动兜底),再不再发由业务工作流下一轮按原方法决定。
   ⚠ 推翻 2026-08-16「pending 不做对账器」—— 所有者 09-25 已批(Q3,见〇.6)。
9. 86bbf0e 的 HEAD_STALE_HOURS(汇总停更 1 小时读明细)由期限表取代:改价期限 15 分钟已覆盖 09-22 那类停更;
   其他类型汇总停更就等到期读明细。
10. 其他"第二个时钟"随期限表去掉:problem_scan / sku_migrate 在途闸的 48 小时上限(在途 = feed 未收口,
    feed 到期必收口);maint_sheet 的 3 天「未查到」(改为直接转述 feed 结果,〇.4 第 6 条)。
11. **消费方口径**:「成功」= 沃尔玛接受;「失败」= 沃尔玛拒绝;「明细无此条」= 沃尔玛收工了但没有这一条(与现在
    的 missing 同义,heal_unknown / 处置 / sku_migrate 现有处理不变)。新增的超期未完成 / 未知状态 / 无法查询 /
    提交未确认都是"沃尔玛没给结论":**不许当失败回收 UPC 等资源,也不许当失败定案**;需要关单的(处置建议、
    冷却表)按"没给结论"关单并注明依据,由下一轮扫描重新判断要不要再做。逐个消费方的映射在实施 PR 里列表给所有者过目。
12. 飞书文案:成功 / 失败 / 明细无此条(原「未查到」)/ 超期未完成 / 未知状态 / 无法查询 / 处理中。
13. 上线首轮:存量在途 feed(含 30 天以上的 A109 / A162 等)全部已过期限,首轮一次收口。先 dry-run 看条数 ——
    ⚠ **feed_poll --dry-run 现在仍写台账**(只挡飞书反哺),本次改成零写入(原 D14)。

### 〇.3 实际结果:只有生效 / 未生效(提案)

- 独立一张表(暂名 `ops.feed_effects`):一行 = 某个 feed 里某个 SKU 的一次判定。列:feed_id、sku、store、操作类型、
  目标值(改价 / 改库存才有)、结论(生效 / 未生效)、观测到的值、观测时刻、依据(哪种观测)、判定时刻。
- **判一次,不回头改**:未生效只能用**期限之后**的观测判;生效可以更早(看到了就是生效)。先生效后消失的,这一行
  记的就是"那次观测看到了什么",人看时自己对时刻。
- **被覆盖的不判**:判定前同一 SKU 同类操作又提交了新的一次,旧的那次不判(否则新改价会把旧改价判成未生效)。
- **判据只有一份,复用现有,不另写**:改价 / 改库存 / 改标题 = `dispositions.maint_effective` 现值比对;
  删除 = `verify_deletions` 的判据;上架 / 跟卖 = 目录里出现该 SKU;停用 = lifecycle RETIRED 或目录缺席。
- 判哪些:feed 结果不是「失败」的都判(失败 = 沃尔玛已拒,没什么可判)。
- 在哪判:catalog_sync 刷新观测之后(与 verify_deletions 同一处)。
- **复核清单**:feed 成功 ∧ 未生效;超期未完成 / 无法查询 ∧ 未生效一并列出。放哪待拍板(Q5)。

### 〇.4 现有"观测驱动的自动动作"(逐条待所有者定 留 / 停)

口径:**"生效 → 记账"**(看到生效了,把码 / UPC / 表格状态记对)不算自动重做;**"未生效 → 自动重做 / 自动定案"**
才是所有者说"目前没有必要"的那类。

| # | 位置 | 现在做什么 | 性质 | 建议 |
|---|---|---|---|---|
| 1 | catalog_sync.py:232 → product_events.verify_deletions + sku_codec.abandon | 删除回执成功后,观测到商品没了 → 弃码 + 烧 UPC(弃码点 1) | 生效 → 记账 | 留 |
| 2 | problem_scan.py:273 / :352(顽固双击) | 观测到"删了没生效"(delete_not_effective)→ 每轮停用 + 删除双发(2026-08 所有者定稿) | 未生效 → 自动重做 | 停,改进复核清单(代价:人处理前这些 SKU 一直挂着) |
| 3 | sku_migrate._verdict (d) | 提交满 24 小时(OBSERVE_HOURS)、观测新鲜、新码没出现 → rolled_back 定案 | 未生效 → 自动定案(新码可能晚到,也可能先生效后消失) | 到跟卖落定期限(重核后 24 小时)仍未出现 → stalled 交人工,不自动回滚 |
| 4 | dispositions.settle / settle_maintenance / expire_executing | 处置建议按观测现值判 confirmed / ineffective;维护类 3 天没等到观测一律 ineffective | 维护链自己的闭环(没改成 → 下轮扫描重新建议),不是重发同一个 feed | 留;判据与〇.3 共用一份 |
| 5 | listing_sheet.heal_unknown | 台账没结论时,目录里看到在架 → 上架表标 Yes + UPC 标已用 | 生效 → 记账 | 留(feed 结果都有终态后,只有超期未完成 / 无法查询会走到这条) |
| 6 | maint_sheet.STALE_DAYS = 3 | 飞书维护记录 3 天没终态写「未查到」 | 表侧第二个时钟 | 删,改为转述 feed 结果 |
| 7 | problem_scan._SQL_INFLIGHT | feed 成功晚于最近一次观测 → 该 SKU 先不重判("待观测") | 用 feed 结果避免拿旧观测做决定 | 留(48 小时上限随期限表去掉) |

### 〇.5 下文(09-24 草案)哪些作废、哪些保留

- **作废**:feed_items 的 effect 维度(§四「effect 维度」、SC6);lapsed 终态与可重开(reopenable)语义;
  B4「观测核验入账、替代 verify_deletions」;B5 / B6 里让消费方改读 effect 的部分;`ops.feed_attempts` 本轮不做
  (每个 feed 的 SKU 行本就按 (feed_id, sku) 留在 feed_items,依据记在行上,重占不丢)。
- **保留**:首次落定即定稿(C03);每个终态带依据;404 与其他读取错误分开;pending 收口(原 D1,改为到期落
  failed「提交未确认」);dry-run 零写入(原 D14);删飞书表侧时钟(原 D15);期限唯一出处。
- **原 D1~D17 去向**:D1 → Q3;D2 被〇.2 取代;D3 / D4 → Q2;D5 / D8 / D15 / D16 并入〇.4;D7 随 lapsed 作废;
  D14 并入〇.2 第 13 条;D6、D9~D13、D17 与本次无关,暂缓。

### 〇.6 拍板结果(所有者 2026-09-25)

- **Q1 期限表**:「期限按官方值、不加余量,请实际查看官方给的期限值,不猜测,不凭记忆回答」⇒ 当天逐页重核
  (〇.2 表);跟卖因美国站页面已无 72 小时那句改为 24 小时。
- **Q2 读不到明细**:批。404 到期后立即「无法查询」,其他错误(含店铺不可调用)再宽限 24 小时。
- **Q3 pending**:所有者先问「说明可能没提交上?现在的逻辑是什么样子的」,已答复:pending ≠ 没提交上
  (POST 可能已达、只是没拿到 feedId);现行只告警、永不老化、同载荷被挡死;建议〇.2 第 8 条(只读反查、
  到期落 failed「提交未确认」、不自动补交),另需补提交条数 / SKU 列表 / 「请求已发出」标记三样。
  **所有者批:「pending 按你的建议做」**(实施见〇.7 ④)。
- **Q4 〇.4 七条**:「按你的建议来(你要重新调研官方的期限值,可能需要跟随调整时间)」⇒ 第 3 条时间点随跟卖
  期限改为 24 小时。
- **Q5 实际结果第一版**:「覆盖操作按你的建议来」⇒ 改价 / 库存 / 标题 / 上架 / 跟卖 / 停用 / 删除。复核清单
  `feed_poll -p review=1`;新判计数放 catalog_sync 摘要(判定发生在那里、一天一次),不放 feed_poll 摘要
  (一天 48 轮,固定计数行没人看)。

### 〇.7 实施记录(2026-09-25)

- **① feed 结果收口**(b4b8b67):FEED_DEADLINE_MINUTES;poll_feed 汇总终态或到期读明细,到期强制落定
  overdue / unrecognized,首次落定即定稿(`AND status='submitted'`);到期后 404 / 宽限后读不到落 unreadable
  (FeedQueryError 带 HTTP 码);raw_status / settled_by 两列;feed_poll --dry-run 零写入;-p store 只轮询那一家;
  消费方:处置建议 receipt_none 关单、上架表写原词不重试不回收 UPC、sku_locked_heal 中间态当未落定。
  真库(沙箱 PG16)验证落账 SQL。
- **③ 七处**(9563d61):顽固双击停用改交人工(problem_scan 首行「删除未生效交人工 N」+ 点名);sku_migrate (d)
  改 stalled;维护表 3 天时钟删除;在途口径唯一出处 feed_track.IN_FLIGHT_SQL(台账 submitted 且 feed_log 未
  收口),去掉 48 小时上限。#1 / #4 / #5 / #7 的"待观测"闸保留。
- **② 实际结果**:ops.feed_effects + services/feed_effect(判一次、被覆盖不判、期限后观测、回看 7 天、failed
  不判、复用 maint_effective 与 product_events.GONE_SQL);catalog_sync 观测之后判(空跑 rollback、失败隔离);
  `feed_poll -p review=1` 复核清单。删除核验的未生效宽限对齐删除期限 72 小时(原 48)。
- **④ pending 对账**:feed_log 加 item_count / skus / post_started_at / recon_count / close_basis;`_post` 在请求
  真正发出前单独提交 post_started_at;`services/feed_track.reconcile_pending` 在 poll_all 轮询之前跑(dry-run
  照查照判一行不写),规则:原工作流还在跑(`runlock.is_held`,只探不占)不碰 → 存量行期限 + 24h 落「提交未确认」
  → 发送标记为空落「未发出」→ 店不可调用期限 + 24h 落「提交未确认」→ 反查 FOUND 收编(feed_log + feed_items
  同事务,时刻用发送时刻)/ NOT_FOUND 到期落「提交未确认」/ UNKNOWN 期限 + 24h 落「提交未确认」。反查
  (`api/feeds.find_recent_feed` 事后模式)按发送时刻开窗、最多翻 20 页、不做 30 秒复查;候选按店排除已记账
  feedId(不分 feedType),必须条数精确 + 明细 SKU 集合完全一致,且**恰好一条**、没有核不了的候选才收编
  (明细只露出本片一部分算核不了,不算"不是")。每种 failed 的依据落 close_basis(未发出 / 沃尔玛拒收 HTTP 码 /
  反查未达补交未果 / 提交未确认…)。守门:对账器源码里不许出现任何提交入口。真库(沙箱 PG16)验证原语。
- **上线顺序**:`python cli.py db_init`(加 feed_items 两列、feed_log 五列、两个索引、一张表)→
  `python cli.py feed_poll --dry-run` 看首轮会按期限收口多少存量 feed / SKU、pending 对账三档各几条 → 真跑 →
  `python cli.py catalog_sync --dry-run` 看实际结果将判多少。

## 一、现状:每种 feed 的查询与落账

公共主干:`api/feeds.submit_feed` 先写 `ops.feed_log`(pending)再 POST,拿到 feedId 转 submitted 并写 `ops.feed_items`(submitted);
`feed_poll`(每 30 分钟)→ `feed_track.poll_all/poll_feed` 读汇总与明细,落 `feed_items` success/failed/missing、`feed_log` done/failed;
反哺器把台账投影到四张飞书表。**只有「沃尔玛回答了」这一种结局有终态**。

| feedType × 提交工作流 | 查询 | 业务结论依据 | 回执永远不来时停在哪 |
|---|---|---|---|
| MP_ITEM × list_new(延后结算) | feed_poll | ① 回执;目录在架只在 heal_unknown 里用 | 上架表「上架结果」空、feed 永远在途 |
| MP_ITEM_MATCH × match_listing | feed_poll | ① 回执;无观测核验 | 跟卖表 J=处理中 |
| MP_ITEM_MATCH × sku_migrate(手动) | feed_poll | ⑤⑥ 观测优先(新码在架/旧码缺席/单查 404) | 闸⑤ 整店停止改码;stalled 永不再判 |
| DELETE_ITEM × product_clear / problem_product_cleanup | feed_poll + product_clear 就地轮询 | ⑤ verify_deletions(只从 success 回执起算,宽限 48h) | 处置行永远 executing;表「处理中」 |
| RETIRE_ITEM × product_clear / sku_locked_heal / cleanup | 同上 | 只信 ① 回执,无观测核验 | 冷却表永远 pending;处置 executing |
| price / inventory / MP_INVENTORY / MP_MAINTENANCE × maintenance | feed_poll | ⑤ settle_maintenance 现值比对,不看回执 | 处置 3 天后一律 ineffective(expired,无依据);维护记录 3 天后写「未查到」 |
| PUT 单品改价/改库存 × maintenance(≤5/≤10 条) | 同步返回 | ③ HTTP 响应 | 不进台账;失败无逐 SKU 记录 |

## 二、断头路:29 个根因簇

- **C01**(high)**沃尔玛清除 feed 状态(约 30 天后 GET 404)被当成「查询失败下轮再试」,永久轮询;轮询失败从不分类**
  - 现在停在:ops.feed_log=submitted 永久;残留 SKU 的 ops.feed_items=submitted,无回执事件,每 30 分钟白发一次 GET;problem_product_cleanup 处置行 executing;listing.retire_cooldown pending(进而 sku_migrate 闸④整店拦);停用/删除表 G=处理中;维护记录「处理中」(3 天后被表侧改写「未查到」);上架表 是否上架=Yes 但 上架结果 空/处理中;跟卖表 J=处理中(I 有值,永不重排);sku_migrate 闸⑤对该店永久关闭。
  - 生产实证:有:A109(32 天)、A162(38 天,product_clear 删除 feed 18CC2F2D…)GET /v3/feeds/{id} 返回 404;A085 29 天仍可查,由此推出窗口约 30 天(经验值,walmart_slas.tsv 明记「feed 状态保留窗口:官方未给」);2026-09-11 摘要已列 A109/L001/A162/A171/A085 长期在途(feed_closure_audit.md:200-208)。注:「A109 那条 404 即 list_new MP_ITEM feed」是按时间推断,不是直接证据。MP_ITEM_MATCH 尚无实例。
  - 可用事实:③ HTTP 404 本身是可落库的事实:可落专用状态(如 feed_log=expired,记首次 404 时刻与 HTTP 码),它证明「沃尔玛不再回答」,但不证明任何 SKU 结局。SKU 结局须改由 ⑤ catalog 观测按链解释(DELETE:缺席/missing_since;RETIRE:lifecycle=RETIRED;MP_ITEM/MATCH:新码在架;维护:现值=目标)或 ⑥ 单品 GET /v3/items/{sku}(404=不在)补齐,行上记「依据=观测/单品查询+时刻」。404 与 401/429/5xx/代理错的区分走 store_retry.diagnose(③)。⑦「约 30 天清除」只是经验值,只能作触发推断的条件,不能单独作结论;更稳妥的是在 404 之前就用 C02 的按 feedType 判死期限收口,404 只作兜底。
- **C02**(high)**SKU 级结论永远不来且没有判死期限:终态 feed 残留 INPROGRESS、未知 ingestionStatus 枚举、汇总停更且明细无结论**
  - 现在停在:ops.feed_log=submitted;残留 SKU 的 ops.feed_items=submitted(并被每轮写上 resolved_at,见 C03),无 *_feed_* 事件;破坏链残留 SKU 进不了 verify_deletions,处置 executing、冷却 pending;上架表 O 空/处理中;跟卖表 J 处理中;sku_migrate 闸⑤关;约 30 天后转 C01。
  - 生产实证:有:2026-09-11 所有者实见五条(feed_closure_audit.md:200-208):A085朱丽霖 改价 18CEF5AC…、A109黄威威 与 L001贾林红 上架 18CE2095…、A162朱行 删除 18CC2F2D…、A171罗尹鸿 MP_INVENTORY 18D39180…;按文档期限试算 6 条该判死(75 SKU)、14 条该继续等(6,087 SKU)(feed_closure_audit.md:335-350)。未知枚举在新系统没有实例。汇总停更有实证(2026-09-22 A131 等 17 条改价 feed),但那批明细全终态,「停更且明细无结论」无实例。
  - 可用事实:② feed 级终态(PROCESSED/ERROR)+ ① 明细里该 SKU 仍 INPROGRESS,是沃尔玛当下的明确陈述(不是缺席),可把原始 ingestionStatus 与最后读取时刻落库;⑦ 官方生效窗口(改价 SLA 15 分钟、库存最长 4h、MP_ITEM/MP_MAINTENANCE 4h 处理/24h setup/48h Hazmat、MATCH 24h/72h、RETIRE catalog 48h、DELETE 72h)过后,可落「超期未决」终态——这是推断,须记录推断依据(最后一次明细状态、读取时刻、所用 feedType 期限及其出处)。SKU 真实结局再由 ⑤ catalog 观测或 ⑥ 单品 GET 按链定案。未知枚举:① 原值原样落库并告警,归「沃尔玛陈述但本系统不识别」,不应以等待处理。
- **C03**(high)**终态分支无条件全量重写 feed_items:resolved_at 每轮刷新使 problem_scan 长期失明,已落定结论被静默改写且不补事件**
  - 现在停在:ops.feed_items success 行的 resolved_at 永远是「刚刚」,直到 404 让轮询在 UPDATE 前抛异常;残留行 submitted+resolved_at 自相矛盾;ops.feed_item_errors 与 feed_items.error_code 可不一致;problem_scan 把这些 SKU 当「处置在途/上架维护在途」跳过,已挂 suggested 被 withdraw_stale 撤成 withdrawn(理由「本轮扫描不再建议」)。
  - 生产实证:有(机制层,已知事实 8):A171 MP_INVENTORY 942、A109 451、L001 439、A162 298 个 success 行自 2026-09-11 起每轮被重写;problem_scan 因此漏建议的条数没有统计。结论静默翻转无实例(依赖沃尔玛明细前后不一致,未核实)。P:DE-17 所引「A131 2740 行同一错误」经复核并未发生(那三条零明细 ERROR feed 在修复前轮询,落的是 missing)。
  - 可用事实:① 首次拿到的逐条回执及其时刻就是事实,应只写一次:终态分支与未终态分支同一口径,只 UPDATE status='submitted' 的行,首次落定时刻不可变;之后重读若与首版不同,作为新事实追加(回执变更记录+时刻)而不是覆盖,并由消费方决定是否重判;problem_scan 的「待观测」比较的应是首次落定时刻与 ⑤ last_seen_at。这与所有者 problem_scan 在途口径定稿一致(不推翻,只是让 resolved_at 回到定稿时假定的语义)。
- **C04**(high)**结局不确定被落成确定的 failed:NOT_FOUND 后补交遇 5xx/网络异常、延后结算末轮补交、2xx 无 feedId**
  - 现在停在:ops.feed_log=failed(feed_id NULL,可被重占),ops.feed_items 无行;sku_migrate 当场 _roll_back:新码 abandoned(sku_update_failed)、旧码复活、sku_migrations=rolled_back;list_new:upc_pool.release(…,'rejected') 把 claimed 号放回空闲池,表 K=No「提交被拒」,下轮当新行重发且无次数上限;match_listing:J=提交被拒、I 空,下轮重排;problem_product_cleanup/maintenance:建议行 suggested,下轮重发。
  - 生产实证:本组合无实例(代码路径确定)。首发 5xx 路径本身有生产实证:C017 的 297 条删除 5xx 反复搁浅(api/feeds.py:436-437 注释)、2026-08-24/25 两晚 Akamai 5xx(api_blueprint.md §8.1,延后结算即因此引入)。tests/test_feeds.py 只覆盖补交 200 的情形。
  - 可用事实:③ 补交(及首发)的 HTTP 结局原样落库即可区分:4xx=沃尔玛拒收(有依据的 failed)、_PRE_FAIL=确定未发出(有依据的 failed)、5xx/None=不确定、2xx 无 feedId=大概率已受理;④ 补交之后(包括 settle 末轮)再反查一次,把不确定收成 FOUND/NOT_FOUND;仍不确定就落 pending/unknown 交 C10 的对账,不得落 failed。只有 ③ 4xx/_PRE_FAIL,或 ④ 双确认 NOT_FOUND 且之后没有未决补交,才允许 list_new 回收 UPC、sku_migrate 当场回滚;事后可用 ⑤ 观测/⑥ 单品 GET(新码或 UPC 是否已在架)核验。
- **C05**(high)**「确定未发出」的提交被落成永不老化的 pending:StoreDeadError、POST 连接阶段失败、rate_acquire 异常、401 换 token 撞代理**
  - 现在停在:ops.feed_log=pending(feed_id NULL)永久,feed_items 无;下轮同载荷 dedup,不再调 token 接口:cleanup/maintenance 表写「跳过/在途防重」(feedid 空)、建议行 suggested;product_clear/sku_locked_heal 报「提交结果不确定,已留 pending」;list_new K=Unknown、UPC claimed(heal_unknown 无台账无在架可依,治不好);sku_migrate 台账「落库未提交」、闸⑤整店关;poll_all 每轮报「pending 待人工核对」。
  - 生产实证:诱因是生产常态:已知事实 6 的 SSL EOF、2026-08-07 SOCKS TLS 握手断线、2026-08-26「Malformed reply」;换 token 400→StoreDeadError 有实见(2026-08-17 catalog_sync 谭总10,_client.py:517-521);feed_closure_audit.md:101-102 自承「StoreDeadError 那种行照旧停 pending」,却以为其它代理故障已全被 _PRE_FAIL 收走(:99-106)。新系统尚无点名 pending 实例(需连库查)。
  - 可用事实:③ 本地 HTTP/传输事实即足以落有依据的终态,不需要 ④ 反查:异常类型区分连接阶段(ConnectError/ConnectTimeout/ProxyError/SOCKS 握手=字节未发出)与读阶段(ReadTimeout/ReadError=确实不确定);StoreDeadError 发生在 token 阶段=未发出;POST 401=沃尔玛已拒;rate_acquire 异常发生在 POST 之前=未发出。这几类应直接落 failed(原因=未发出/已拒,retryable),并按 store_retry.diagnose 六档归类(凭证失效/代理无效/代理波动)。_client 把连接阶段与读阶段失败分开返回属于 api 层「把接口调对」,不违反铁律 2;进程在 rate_acquire 睡眠中被杀这一支只能靠 C10 对账。
- **C06**(high)**sku_migrate 回滚/确认判据不看回执与观测质量:不可逆动作建立在单轮观测或空响应上,回滚后还会对同 GTIN 重发**
  - 现在停在:listing.sku_migrations=rolled_back(新码 listing_sources abandoned sku_update_failed、旧码 replaced_by 清空复活)或 confirmed(旧码不可逆弃码、UPC 改标、处置迁键、节点库存行删);ops.feed_items 新码=success 与台账判词矛盾且无对账,判决所用的是哪一轮观测不记录;在架新码作废,catalog_sync 因 replacement_map 不记 item_appeared;旧码消失时记 item_missing,被 product_risk 读成疑似平台下架。
  - 生产实证:「MP_ITEM_MATCH 打到沃尔玛侧已不存在的 item 会新建 listing」已实证:2026-09-07 A131吕灿荣 B09L3WXJ96(旧码 RETIRED 死档,新码新建 item;feed_closure_audit §三.4、sku_plan §9.15);A131 B08DR3TKQK/B09L3WXJ96 仍为 double;官方原文 "Updates may take up to 24 hours... up to 72 hours"(walmart_slas.tsv:16)。错误回滚、200 空体、截断确认本身无实例。复核指出:回滚后再发更可能是原地再换码,「新建 listing」仅在旧 item 已死档时有实证。
  - 可用事实:① 新码逐条回执应作 (d) 的否决条件:回执 SUCCESS 或 INPROGRESS 时 72h 前不得凭观测缺席回滚;回执 failed(①)或 feed 级拒收(② feedStatus=ERROR+itemsReceived=0+feed 级 ingestionErrors)才是回滚的事实依据。⑦ 官方 MATCH 24h/72h 作观察期下限。⑤ 观测须满足「扫描落库时刻 > 提交+观察期」且该轮扫描完整(非 truncated,报表兜底覆盖),台账记录判决所用观测轮次与时刻。⑥ 单品 GET 新码/旧码作为不可逆动作前的二次确认,且须区分真 404 与 200 空体(api 层返回值区分属接口适配)。① ops.feed_items 里新码 success 应进入候选闸,阻止对同 GTIN 自动重发——遵守所有者定稿:MATCH 对可能已成功的 SKU 重发会新建 listing,不能一刀切。
- **C07**(high)**sku_migrate 过程账无出口:stalled 永不再判、「落库未提交」永不进判据面、真双挂只能等僵尸列表里的旧码缺席**
  - 现在停在:listing.sku_migrations 停在 stalled / pending(submitted_at NULL)/ double,无 settled_at;旧码 listing_sources replaced_by=新码(身份层在途:不弃码、UPC 不改标、处置不迁键、节点库存行不删、problem_scan 不扫、catalog_sync 不为它记 item_missing);该店 _stage_cap=0;unknown/StoreDeadError 那支 ops.feed_log 同时 pending,闸⑤关闭。
  - 生产实证:有(double):A131吕灿荣 43 条 double 中 B08DR3TKQK(两个 wpid 都 PUBLISHED)、B09L3WXJ96(旧码 RETIRED 死档)按 §9.15 仍为 double,所有者实证后台「删也删不掉」(sku_plan.md:1773-1815)。stalled 与 unsent 无实例;所有者称旧系统未见 pending。「全仓无别处碰 sku_migrations」已由材料 grep 核实。
  - 可用事实:stalled:⑤ 观测恢复后照常可判,只需把 stalled 当「挂起」纳入判据面;店长期不可达按 C17 落「挂起:店不可达」。unsent:ops.feed_log/feed_items 里 workflow=sku_migrate、按新码落的 feedId 就是 ③ POST 回执,可按新码回填 feed_id/submitted_at;真 pending 的走 C10 的 ④ 反查对账;对账后仍无 feedId 的,用 ⑤ 观测(新码在架即已达)或 ⑥ 单品 GET 新码定案。真双挂:⑥ 单品 GET 旧码(不论 wpid 是否相同)返回真 404 或 lifecycleStatus=RETIRED 即可定案,不必等列表缺席(列表对死档不可信);⑦ MATCH 72h 作等待上限。
- **C08**(high)**删除观测核验的 gone 判据过宽,把「没看见」当「已删除」,并在同一事务里不可逆弃码烧号**
  - 现在停在:catalog.product_events delete_verified;ops.dispositions confirmed(含借判的 retire 行,以及本应一直点名的凭证坏店 executing 行);catalog.listing_sources abandoned_at(delete_verified)、catalog.upc_pool burned_delete、sku_abandoned 事件;walmart_items 那一行 missing_since 仍可能为 NULL(RETIRED)。
  - 生产实证:间接:列表可见性翻转有实证(2026-08-28 翻回 10,191 条死档;2026-09-09 A109 一轮判缺席 3487 行,problem_scan.py:78-81);store_release dead=1 自 2026-08-22 定稿投入使用;sku_wiring_audit.md L-4 已提出 RETIRED 判 gone 待所有者定(L-4 认为烧号本身合理,因 UPC 仍绑在 RETIRED 记录上)。误弃码、循环的具体条数未统计。已读码核实 _VERIFY_SQL 的三个 gone 条件。
  - 可用事实:⑤ 观测只有在「扫描完整(非 truncated)+ 观测时刻晚于回执 + 缺席来源是沃尔玛列表而非人工 store_release」时才能作删除事实——需要给 missing_since 记来源与所属扫描轮次;RETIRED 是另一种 ⑤ 事实(已退役≠已删除),应落独立结论(如 retired_observed)而不是 delete_verified,弃码与否另定;w.sku IS NULL(从未观测)没有任何删除事实,只能在 ⑥ 单品 GET 404 后判;⑥ 单品 GET /v3/items/{sku} 404 可作 gone 的二次确认(items.get 900/min 足够;须区分 200 空体,见 C06);⑦ DELETE 官方 72h 作宽限上限。不推翻「删除以观测定案」定稿,只收紧观测的质量要求。
- **C09**(high)**破坏类处置 executing 没有出口:回执永远不来、retire 行无自己的判据、店不被扫、存量 relist,且破坏类没有超期放行**
  - 现在停在:ops.dispositions executing 永久;部分唯一索引挡住同 (店,SKU,动作) 的新建议;sku_migrate「无未了结破坏建议」候选闸永久剔除该 SKU;stuck_executing 每轮只点名不放行。
  - 生产实证:部分有:2026-09-09 全船队约 800 条 delete/retire executing 停了数周(失败回执那一半已由 receipt_* 修复,回执永远不来的这一半未修,dispositions.py:203-210);A085 有 8 条因此被剔出改码面;凭证坏店 executing 形态记在 dispositions 头注。只做 retire 且回执成功那一档、存量 relist 行数未统计(不连库)。已读码核实 expire_executing 缺省只 MAINT_ACTIONS。
  - 可用事实:上游回执收口后(C01 的 ③ 404、C02 的 ⑦ 期限),⑤ 观测可直接判:DELETE 看缺席(按 C08 的质量要求)、RETIRE 看 lifecycle=RETIRED(retire 行应有自己的观测判据,不必借 delete),relist 看 published_status;⑥ 单品 GET 可补齐缺席品的定案。⑦ 官方 RETIRE 48h/DELETE 72h 过后仍无观测且店可达的,可落「超期未证」推断终态(记依据:最后回执、最后观测时刻、店可达性),释放唯一索引;店不可达的按 C17 落「挂起:店不可达」而不是永远 executing。
- **C10**(medium)**pending(POST 结局不确定)没有对账、不老化、不存可对账的信息;被多处承诺的「启动对账」不存在**
  - 现在停在:ops.feed_log=pending(feed_id NULL)永久,ops.feed_items 无行;list_new 表 是否上架=Unknown「待对账」、UPC claimed(只能靠目录在架自愈,真没到达则永久 Unknown);problem_product_cleanup/maintenance 建议行 suggested,维护记录 feedid 空的「处理中」既不被反哺也不被超期(C25);product_clear/sku_locked_heal 行不动、不落冷却,每轮误报「结局不确定」;match_listing 表不写、每轮重做 SPEC 预检占 1000 次/天额度;sku_migrate 见 C07。
  - 生产实证:新系统无点名实例(需连库);所有者 2026-08-16 称旧系统跑了几个月没见过(feed_closure_audit.md:71-74)。但 C05 的「确定未发出」路径很可能已在生产产生 pending,只是被当成本簇处理。
  - 可用事实:建议推翻 2026-08-16「pending 不做对账器」定稿,理由:① 新系统 pending 入口至少 8 条,其中 4 条(C05)其实确定未发出,而代理故障是生产常态;② pending 永久占防重闸与 sku_migrate 闸⑤,并被 5 处文案承诺由对账兜底,现状是承诺与实现不一致;③ 所需方法已存在——对账器只是在另一时点调用同一个 find_recent_feed(④),不是「换方法重试」,默认也不补交。对账的事实来源:④ GET /v3/feeds 列表按 feedType+条数+时间窗反查(窗口以本次 claim 的 updated_at 为准,不用 created_at),FOUND 收编(并按 C14 用 ① 明细 SKU 集核对),NOT_FOUND 落 failed(依据=列表双确认缺席);是否补交按 feedType 分:DELETE/RETIRE 对同 SKU 重发无实害,可按原载荷补交(同一方法);MP_ITEM/MP_ITEM_MATCH 不自动补交(可能已成功,重发会撞库或新建 listing),改由 ⑤ catalog 观测/⑥ 单品 GET 定业务结局。超过约 30 天(经验值)列表也查不到时,只能落「推断未达/推断已达」并记录依据。前提:pending 行补存条数、SKU 列表与本次提交时刻(C15),动表同步 docs/db_schema.md 与 refdata/schema.sql。
- **C11**(medium)**非 success 回执各档(missing、processing/unknown、ITEM_GONE、feed 级拒收推断)没有唯一解释出处,各消费方各判一套**
  - 现在停在:同一推断在各表是不同终态:listing.retire_cooldown failed(永久转人工,SKU_LOCKED 行不清列)或 pending(永远 waiting,且拦住整店改码);ops.dispositions ineffective(receipt_failed)或 confirmed(receipt_gone);listing.sku_migrations rolled_back(新码弃码);上架表 MISSING+Yes(冻结,不重试不核验,UPC used)或 No(UPC 回收重排);三表「未查到」;病历里只有 *_submitted,没有任何回执终态事件。
  - 生产实证:部分:2026-09-09 约 800 条 executing 中一部分就是 missing 回执没有落定路径(dispositions.py:204-210);backlog §十三 统计破坏类约 30 条 NULL 码或 missing;ITEM_GONE 码生产存在(resources.py 注释约 200、约 55 条);零明细整 feed 拒收推断有 A131 2026-09-07 实证(现已改为直接落 failed)。sku_locked_heal 上的具体实例无统计;复核指出 RETIRE 回执约 100% 为 ERR_PDI_0004,ITEM_GONE 只是较小一支。已读码核实 _relist 的 else 分支。
  - 可用事实:missing 本身没有 ①,应先按 C12 确认「明细确实读全」才允许产生;其业务结局由 ⑤ catalog 观测 / ⑥ 单品 GET 按链定案(删除:缺席或 404=已删;上架/跟卖:在架=已达、缺席=未达;RETIRE:lifecycle=RETIRED)。processing/unknown 是 ① 沃尔玛明说仍在处理,不得判失败,只能等 ⑦ 期限(C02)。ITEM_GONE 码是 ① 沃尔玛陈述「目标已达成/已不存在」,应在唯一出处解释一次(每个能力一条实现路径),sku_locked_heal 与 dispositions 共用。feed 级拒收以 ② feedStatus=ERROR+itemsReceived=0+feed 级 ingestionErrors 为依据(已实现);历史 missing 行与「有部分明细的 ERROR feed 里缺席的 SKU」不能沿用该推断,应走 ⑤/⑥。
- **C12**(medium)**台账 SKU 集、明细 SKU 集与 itemsReceived 从不核对:台账缺行则回执被丢而 feed 仍落 done,明细读不全则台账被推断成 missing**
  - 现在停在:ops.feed_log=done 但 ops.feed_items 零结论(或全部 missing 推断);违禁码、SKU_LOCKED、撞库码不写事件、不进黑名单;clear_sheet/match_sheet/listing_sheet 反哺永远「处理中」,maint_sheet 跳过推进水位、单元格停「处理中」后被 3 天规则写「未查到」;product_clear 就地轮询用明细直接写表而库里无记录;sku_locked_heal 就地轮询把 processing/unknown 判 failed;汇总非终态+台账零行则永远在途至 404;重复 SKU 只剩一条台账结论。
  - 生产实证:有:MP_INVENTORY 漏登记 _chunk_skus,台账 sku 存整条 dict 串,回执反哺永远「台账查无」,终态时脏行被标 missing(2026-09-07 谭总12,api/feeds.py:278-280,已修;存量脏行由所有者处置;成功回执丢失,失败 SKU 的报错进了 feed_item_errors 但 workflow 为 NULL)。_ok_result 两事务失败、itemsReceived 冻结为 0、单页上限小于 50 均无实例(2026-09-22 A131 停更 feed 明细翻页 71/71 正常);itemsReceived 截断机制维护链复核判为假想(W:M23 已剔除),查询层复核补为 P:MISS-02,此处保留但标「未核实」。
  - 可用事实:② 终态汇总的 itemsReceived/itemsSucceeded/itemsFailed、① 明细条数、本地提交条数(③,需落 feed_log)三方核对:不一致 ⇒ 判「明细未读全」,保留在途而不是标 missing;一致且 SKU 缺席才是有依据的 missing。① 明细里有而台账里没有的 SKU 可 INSERT 进台账(依据=明细原文,标来源),同时修复台账缺行与键形态漂移;键规范化(strip/大小写)在唯一出处做。零明细 PROCESSED 应视同「明细未读全」而非 missing。同 feed 重复 SKU 应在提交前(本地)拦截或合并。
- **C13**(medium)**提交层结果与调用方落账之间断裂:submit_feed 非逐片异常安全、调用方账写在另外的事务、补试拿到 dedup 后事实被降级、延后句柄被丢**
  - 现在停在:ops.feed_log=submitted、ops.feed_items 已落(沃尔玛侧真在跑),但:*_submitted 事件永久缺失;cleanup/maintenance 建议行 suggested(「跳过 在途防重」),其 feed 的观测判决无人等,生效后被 withdrawn;maintenance 表给已提交意图写「未执行(凭证失效/提交异常)」;list_new 表 K=Unknown 或空、UPC claimed、list_submitted 缺失;sku_migrations submitted_at NULL(C07);前 k-1 片的 deferred 句柄对应 feed_log 永久 pending;match/product_clear 同 SKU 被重复提交,feed_log 上一笔 done 被覆盖;sku_locked_heal cleared 未弃码的行永不再处理。
  - 生产实证:无点名实例;诱因常见:店级代理 SSL EOF 是生产常态(已知事实 6),飞书写接口出错是本仓常见故障类别,所有者 2026-08-09 实遇「提交成功但表格一行没写」;sku_wiring_audit.md L-3 已提出 sku_locked_heal 三步分事务的一部分。
  - 可用事实:事实其实已在库:ops.feed_log(submitted+feed_id)与 ops.feed_items(按 store/sku/workflow)就是 ③ POST 回执的落账。调用方应以台账为准重建自己的账(按 feed_id 或 (store,sku,workflow) 回填 executing、*_submitted、UPC used、sku_migrations.feed_id/submitted_at、飞书 I/E 列),不凭内存返回值;submit_feed 应逐片 yield 结果或在异常时携带已完成片;dedup 返回应附在途行的 feed_id、状态、提交工作流与时刻(本地 ③),让调用方区分「自己的」与「别人的」。之后 ① 回执、⑤ 观测照常收口。
- **C14**(medium)**反查三态本身是推断:FOUND 可能误收编别人的 feed,NOT_FOUND 可能误判已达的 feed,且库里不标 feedId 来源**
  - 现在停在:误收编:ops.feed_log=submitted 挂别人的 feedId,本片 feed_items 挂错 feed_id,终态时全部落 missing(推断),两行 feed_log 共享 feed_id 同被 mark_feed_done;本片真实 feed(可能已达、可能未发)无人跟踪也无人补交。误 NOT_FOUND:补交产生第二条 feed,首发成孤儿;补交再失败则叠加 C04。
  - 生产实证:同类问题(同尺寸兄弟切片误收编)在 2026-08-07 审查中发现并修过(api/feeds.py:548-550);反面实证:2026-08-24 有 4 家店在整轮后反查 FOUND 被救回(list_new.py:204-205);A131 feed 级拒收 itemsReceived=0 使该类 feed 永不匹配(已知事实 5,复核降级为「重复提交一条注定被拒的 feed」)。其余误收编/误判无实例;复核判「RECEIVED 阶段 itemsReceived=0 匹配不上」未核实。
  - 可用事实:④ 命中的列表条目(feedId、feedDate、itemsReceived、feedType)原样落库并标「来源=反查收编」;收编后第一次拿到 ① 明细时,用明细 SKU 集与本片 SKU 集比对——明细 SKU 全不在本片 ⇒ 判误收编,回退为 unknown 交对账(这一步把推断升级为事实);时间窗以本地首发 POST 时刻(③)为基准而非反查时刻;feedDate 解析不出时不应放开窗口。
- **C15**(medium)**ops.feed_log 作为事实账本的结构缺陷:不存依据、failed 一值多义、一个载荷键一行被重占覆盖历史**
  - 现在停在:库里只有一个 status 字样,依据只在日志;旧提交的 feed 级结局被覆盖,只剩 feed_items 逐条行;尝试次数不可知;收编行与直接回执行无法区分。
  - 生产实证:设计现状;feed_closure_audit.md:162-170 自承缺 item_count/skus 两列;created_at 陷阱已知(:300-302,submitted 行已改用 updated_at,pending 未改);pending 在生产未出现过。S:D20 经复核影响很小(同步路径时差约 30 秒)。
  - 可用事实:全部是已经拿到却没落库的事实:③ HTTP 状态码、响应体摘要、异常类别(连接/读/凭证);④ 反查命中的列表条目;② feedStatus、itemsReceived、feedDate、modifiedDtm、feed 级 ingestionErrors;本地提交时刻、条数、SKU 列表、payload_key。改为「每次尝试一行」(attempt 表,feed_log 仅作当前指针)+ 原因枚举 + 证据 JSON,即可让每个终态都带依据;动表须同步 docs/db_schema.md 与 refdata/schema.sql。
- **C16**(medium)**在途防重闸的设计:锁跟随永不终态的行而无期限、按整片载荷指纹而非 SKU、dedup 结局在各调用方各解一套**
  - 现在停在:建议行永远 suggested,维护记录每天一行「跳过/在途防重」(挡它的是 pending 时 feedid 为空);sku_migrate 整店不发;list_new K=Unknown、上架feedid 空(只能等 heal_unknown);或同 SKU 被再次提交。
  - 生产实证:部分:A109、A162、A171 证明 submitted 会永久卡住,但没有同载荷重提被拦、或闸⑤/闸④因此停摆的实例(复核收窄);同一 SKU 被两条链先后删两次(feed_closure_audit.md:41-43,属 2026-08-24 maintenance 停发 DELETE 之前的历史)。
  - 可用事实:锁的释放应跟随在途行拿到有依据的终态(C01 ③404、C02 ⑦期限、C05 ③本地事实、C10 ④对账),不另设时钟;在途判定应按 (store, feed_type, sku[, 目标值]) 查 ops.feed_items 的在途行(本地 ③ 提交记录),而不是整片指纹;dedup 返回应携带在途行的 feed_id、状态、提交工作流与时刻,由唯一出处解释;sku_migrate 闸⑤/④ 只拦与候选 SKU 有关的在途行。
- **C17**(medium)**店铺不可查/不可观测(停用、凭证失效、缺代理、代理长期故障)时,回执与观测两条事实通道同时中断,没有「店不可达」这一状态**
  - 现在停在:ops.feed_log/feed_items 永远 submitted,超约 30 天转 C01(停用店连 404 都走不到,因为根本不去问);跟卖表 J 处理中;维护记录 3 天后「未查到」、处置 3 天后 expired(C18);破坏类处置 executing、verify 永远 wait(C09);停用店建议永远 suggested,cleanup 每天写一行「未执行(凭证缺失)」;sku_migrate 72h 后 stalled(C07)。
  - 生产实证:有:店铺代理 SSL EOF 导致查询失败、下轮再试(已知事实 6);换 token 400 转 StoreDeadError 实见(2026-08-17 谭总10);dispositions.py 头注记凭证坏店 executing 形态;docs/backlog.md:108 死店「凭证缺失跳过」(2026-08-12,维护意图侧)。feed 侧无点名实例。
  - 可用事实:③ 凭证/代理错误经 store_retry.diagnose 六档归类(凭证失效/代理无效/代理波动/沃尔玛NNN/网络未达/其他)是可落库的事实,说明「为何查不了」;在营判据 services/stores.enabled_names() 与凭证表 启用=否 是本地事实——停用是所有者意图,可据此把该店在途 feed/处置落「放弃追踪(店停用)」终态,依据=启用=否+时刻;凭证死/代理坏只能落「挂起:店不可达(原因档)」非终态;店恢复后用 ①② 回执(若在 30 天内)或 ⑤ 观测/⑥ 单品 GET 补定案;超 ⑦ 期限与约 30 天经验值后按 C01 处理。
- **C18**(medium)**维护处置账只信观测、不看回执:观测不到就 3 天一刀切 ineffective(expired),观测质量不校验,标题「回执成功但未采纳」不记账**
  - 现在停在:ops.dispositions ineffective(expired 或 value_unchanged)与 ops.feed_items success 并存且互不对账;之后生效也不回改(扫描件只撤新建议,不动已落定行);标题通道 TITLE_SYNC=False 停闸中(开闸后原样复发)。
  - 生产实证:有:L001贾林红/B0FVDR7XJL 标题回执 SUCCESS 生效值不变(Seller Center 截图,2026-09-05 停闸);A171 691466(复核:文档记的是「将会被」expire,最终落定未记载,改码成因已由 09-08 rekey 修);谭总23 2026-09-17 235 条无节点建议走 legacy 判 ineffective(已被 _hold_nodeless 挡住,剩「未配仓但多节点」一档无实例);2026-08-28 A109 bulk 截断;2026-09-22 16 条改价 feed 停更 31h 时观测先于回执收口(结论碰巧一致);A171 55 个残留行与处置账是否一致未逐行核。
  - 可用事实:① 逐条回执(success/failed+码)应写进处置行 detail 并参与判定(回执 failed ⇒ 有依据的 ineffective;回执 success+观测未变 ⇒「沃尔玛已接收未生效/未采纳」),这不推翻 receipt_in_ledger 定稿——该定稿只管 product_events 病历白名单,处置账 detail 另说;⑤ 观测须带字段级新鲜度(avail_qty 本轮是否真从 bulk 拉到、节点 seen_at),陈旧值不得用于判定;⑦ 官方窗口(改价 SLA 15 分钟、库存最长 4h、catalog 可见最长 6h、MP_MAINTENANCE 4h)作「观测足以判定」的下限;expired 应改成带原因的「无观测」非终态(缺席/店不可达/节点缺失/回执未落),真要落终态则标「超期未证」并记依据;标题:① SUCCESS + ⑤ 生效值不变 本身就是「未采纳」的事实,应落台账(TITLE_SYNC 恢复条件②)。
- **C19**(medium)**删除核验只从 success 回执起算、宽限 48h 短于官方 72h:已删的收不回终态,处置账、病历、目录、登记簿互相矛盾**
  - 现在停在:ops.dispositions confirmed(receipt_gone)或 ineffective(receipt_failed/delete_not_effective);catalog.product_events 只有 delete_submitted 或 delete_not_effective;walmart_items 行 missing_since NULL(僵尸)或已缺席;listing_sources 仍是活码、UPC 不烧;list_new 在架闸按活码继续拦该 (店,ASIN)。
  - 生产实证:有:A162 删除 feed 的残留 SKU;A085 611 条 QARTH(feed_track.py:236-238;backlog §十三);2026-09-09 全船队 ITEM_GONE 回执分布约 450、200、55、5 条(resources.py:234-247,backlog §十三 已知未修)。两本账相反的条数、48~72h 误判条数无统计。
  - 可用事实:⑤ 缺席观测(按 C08 的质量要求:完整扫描、晚于提交、来源为沃尔玛列表)本身就是删除事实,核验起点可改为 delete_submitted(或任一回执),不必依赖 success 回执——仍是「以观测定案」,不推翻定稿;⑦ 官方 DELETE 72h 作宽限;① ITEM_GONE 码是沃尔玛「已不存在」的陈述,但僵尸列表会继续列出,应以 ⑥ 单品 GET 404 对质后再进身份层(sku_migrate (a′) 已用同法);处置行应按自己的 feed_id 绑定核验事件(本地 ③ 关联),借判须标注来源。
- **C20**(medium)**提交层失败不分类、不计次:确定性拒收每天无上限重发,feed 级拒收却被当逐 SKU 失败提前耗尽**
  - 现在停在:上架表 K=No「提交被拒」、停用删除表 G=提交被拒、维护记录「提交被拒」,每轮重复;ops.feed_log 在 pending 与 failed 之间来回;feed 级拒收的上架行进入 exhausted(是否上架=Yes、上架结果=FAILED、不再重试,exhausted 只进本轮摘要不落库)。
  - 生产实证:部分:feed 级整条拒收有实证(2026-09-07 A131 MP_ITEM_MATCH item setup limit,itemsReceived=0,已知事实 5),MP_ITEM 无实例;永久 4xx 循环无实例。
  - 可用事实:③ HTTP 状态码与响应体(4xx=沃尔玛拒收事实,可按码区分 429 限流与永久错误;_PRE_FAIL=未发出事实)应落库并逐次计数(C15 的尝试行);② feed 级 ERROR + feed 级 ingestionErrors 作为 feed 级事实计入「feed 级失败」,不摊成逐 SKU 尝试次数;重试策略据此分档:未发出/429 可重试,永久 4xx 停止并落终态(依据=最近一次 4xx 响应原文)。
- **C21**(medium)**上架链的行级终态只存在于飞书上架表:重试列不清、双入队、人工回归不生效、撞库弃码由飞书行驱动且只看首码**
  - 现在停在:上架表停在 Yes+新 fid+旧 FAILED(下一轮继续进 _retry_rows,被同店去重闸每天拦「本店已在架」,或三次后 exhausted 而商品可能已在架);同一 MP_ITEM feed 里同 SKU 两条、两个 UPC,feed_items 只剩一行,两个 UPC 都 mark_used(或一个永远 claimed);CONTENT_REJECTED 被每轮写回;在架码被弃、UPC 烧成 conflict,同店去重闸看不见它;UPC claimed 常驻;违禁码非 failed 时表写 PROHIBITED 而 asin_blacklist 无记录。
  - 生产实证:无记录实例;tests/test_list_new.py:449 只断言内存清空,没有断言表列被清空;0101119 能否与 SUCCESS 并存未核实;resources.py:211-216 里「清掉该列」仍用旧列字母 O(现为 Q)。
  - 可用事实:① ops.feed_items 与 ops.feed_item_errors(全码集)已在库,足以在库里按 (store, 身份键) 聚合每次尝试的回执、计次、判耗尽,飞书只作投影;⑤ catalog 观测可核验上架是否成功(C23);撞库弃码应以 ① status=failed 且全码集含 0101119 为依据,由库侧唯一触发(不依赖飞书行存在与否);分类读 feed_item_errors 全码集而非首码;同 feed 同 SKU 重复应在提交前本地去重。
- **C22**(medium)**heal_unknown 收尾的依据错位:读到的是同码上一次尝试的回执,分档漏 CONTENT_REJECTED,回收已达 UPC,不补事件**
  - 现在停在:上架表 是否上架=No(UPC 回收、重排)或 Yes(UPC used,上架feedid 空)或永远 Unknown;catalog.upc_pool '' 或 used 无事实依据;list_submitted 缺失;空 feedid 的 Yes 行永不再被 sync_from_ledger 轮询。
  - 生产实证:无;前提是出现 pending/unknown 结局,所有者称生产上未遇到,代码路径确定(已读码核实 listing_sheet.py:731-775 分档确无 CONTENT_REJECTED 分支、FAILED/MISSING 回收 claimed 号)。复核指出:常见 Unknown 对应的是 used 号,回收只在「feed 已落、_apply_submit_result 未提交、补试 dedup」这一窄路径发生。
  - 可用事实:Unknown 的正确依据是本次尝试的 feed_log 行:先按 C10 用 ④ 反查对账得到本次 feedId,再读该 feedId 的 ① 回执;没有本次回执时,⑤ catalog 观测按 SKU 在架(及 productIdentifiers 里的 UPC 是否为本次领的号)或 ⑥ 单品 GET 可直接判上架成败与 UPC 绑定,依据记为「观测/单品查询」;上一次尝试的 ① 回执只能作历史参考,不得收尾本次;回收 UPC 只能依据 ③ 确定未发出/4xx,或 ⑤/⑥ 确认该号未绑定。
- **C23**(medium)**只信回执的链没有观测核验终态(上架、跟卖、停用),RETIRE 回执还外溢到 DELETE 判据**
  - 现在停在:ops.feed_items success / 跟卖表 J=成功 / 停用表 G=成功 即终态,无观测核验事件;perm_blocked 的 SKU 在 problem_scan 永久跳过,摘要写「永久拒跳过(… 重发必再拒 …)」。
  - 生产实证:同类故障(回执成功但后台没动)在删除链有所有者实证(delete_not_effective,product_events.py:33-34);ERR_PDI_0004 覆盖近 100% 的 RETIRE(resources.py:255-256);上架/跟卖侧无实例;被误挡的 DELETE 条数无统计。
  - 可用事实:⑤ catalog 观测(在架、published_status、lifecycle_status)可为上架、跟卖、停用各提供一个按提交挂钩的核验事件(类比 delete_verified/not_effective),以 ⑦ 官方窗口作宽限(提交成功到 catalog 可见最长 6h;MATCH 24h/72h;RETIRE catalog 48h);sku_locked_heal 的锁死 SKU 不在 walmart_items 时用 ⑥ 单品 GET 核验 lifecycleStatus;DELETE 的死档/永久拒闸只看 DELETE 回执(① 码按 feedType 区分),RETIRE 的 ERR_PDI_0004 只挡 RETIRE。
- **C24**(medium)**单品 PUT 同步路由(改价/改库存)零台账:失败逐 SKU 无痕、成功只留 'sync' 标记、网络异常被判失败、抑制键不记结局**
  - 现在停在:失败:ops.dispositions suggested(每轮再领再 PUT,无计数、无退避、无终态);其实已生效的失败最终 withdrawn 而非 confirmed(C27);成功:executing(feed_id='sync')、detail 有 old/new;ops.dedupe 行无法回溯到结局,且无限增长。
  - 生产实证:反方向有:2026-08-30 节点 PUT 实测返回 (True,'') 但读回无变化(已修,inventory.py:178-181);2026-08-09 所有者实遇「提交成功但表格一行没写」(当时 PUT 行是否丢失未记录)。永久失败循环无实例。
  - 可用事实:③ 同步 HTTP 状态码与响应体(节点端点的 nodes[].status 与 errors[])就是现成的逐 SKU 回执,应像 feed 一样落库(每次尝试一行,C15),失败原因与次数随之有依据;status=None 为不确定,由 ⑤ catalog 观测或 ⑥ 单品 GET 读回现值定案;dedupe 行记结局与来源(feed_id 或 'sync'+尝试 id)。
- **C25**(medium)**飞书投影层自造终态、只投影一部分事实、按表格值而非台账键找行,与库永久脱节**
  - 现在停在:表上「未查到」(两义:台账 missing 推断 / 面板等不及,只有报错列可分)、「处理中」(fid 空或键漂移,永不回写)、「成功」(只凭回执);库里分别是 submitted/success/failed/ineffective/pending。
  - 生产实证:有:谭总12 MP_INVENTORY「表上永远处理中,超 3 天判未查到」(2026-09-07,multi_node_plan;复核:证明的是超期规则写出无依据的「未查到」,不是台账已落定仍被写);A085 改价 feed 卡 412h;L001贾林红 B0FVDR7XJL 标题回执 SUCCESS 生效值不变;2026-08-27 90221 事故中反哺器停摆(连续停摆超 3 天时已有回执的行也会被写「未查到」)。
  - 可用事实:投影只应转述库里已落定的事实(①②③⑤ 的落库结果):「未查到(超期)」应来自库里按 C02 用 ⑦ 期限得出、带依据的判死结论,而不是表侧时钟;观测结论(⑤,delete_verified/not_effective、settle_maintenance)应回写到表;反哺键以台账 (feed_id, sku) 为准并在表上保存,不从可被人改的列推导;fid 为空的 pending 行显示为「待对账」并随 C10 的结论回写。
- **C26**(low)**ASYNC 合规审核码的回执被当成终态冻结,审核通过的事实永远读不回来**
  - 现在停在:ops.feed_items failed(永久)+ *_feed_failed 事件(feed_failures 视图算失败);上架表 上架结果=ASYNC_PENDING 永不落定;跟卖表「失败」;可能 sku_migrations rolled_back。
  - 生产实证:registry 注释称旧系统有实证(审核中假错误会翻成 SUCCESS);新系统发生量无统计。
  - 可用事实:① 回执码本身说明「待审核」,应落「待定(审核中)」非终态而不是 failed;真实结局只能由 ⑤ catalog 观测(上线/published_status)或 ⑥ 单品 GET 获得;⑦ 官方 Hazmat 人工审核最长 48h、危险品合规审核最长 3 个工作日可作等待上限,超期仍未上线落「审核超期未上线」(推断,记依据)。
- **C27**(low)**withdrawn 的唯一依据是「本轮扫描没建议」,真实原因(闸挡、停闸、我方提交已生效)丢失**
  - 现在停在:ops.dispositions withdrawn(理由错误或缺失)。
  - 生产实证:无统计(代码路径;未连库核对 withdrawn 行数)。
  - 可用事实:本地闸事实(哪道闸拦的:在途 feed_id、死档码、停闸开关名)可直接写进 withdrawn_reason;我方提交已生效的情形,可用 ⑤ 观测值=目标 且存在同 SKU 同目标值的提交记录(① feed_items 或 C24 的 ③ PUT 记录)把行关联判 confirmed,而不是 withdrawn。
- **C28**(low)**并发竞态、诊断工具与异常处理和自述不符(工程缺陷类)**
  - 现在停在:孤儿 feed_items submitted(poll_all 只按 feed_log 轮询,永不收);重复 delete/retire_feed_* 事件;维护记录错行、水位错乱;诊断结论误导;maintenance 整轮失败。
  - 生产实证:均无实例;15:00 调度重叠真实存在(registry/schedule.py:101,207);裸 list 形态 ingestionErrors 在旧仓实见过(feed_track.py:98-101 注释)。
  - 可用事实:不需要外部事实,属本地一致性:条件 UPDATE(AND status IN ('failed','done'))或 SELECT … FOR UPDATE;事件按 (feed_id, sku, event) 唯一约束;反哺与 prune 共用一把锁或按主键写;诊断工具复用 feed_track 的唯一解析与唯一收工判据;_settle 按真兜底三要件加条件明确的告警兜底。
- **C29**(low)**feed_poll --dry-run 照写台账、事件与永久 ASIN 黑名单,三种 dry-run 语义不一**
  - 现在停在:空跑落下真实的台账终态、回执事件与永久黑名单;表未回填(下一轮真跑补齐)。
  - 生产实证:代码确定(任务已知事实 9);黑名单部分按代码推断。
  - 可用事实:写入台账的都是 ① 沃尔玛逐条回执,本身有依据,问题只在承诺与不可逆副作用:dry-run 至少不写 asin_blacklist 与 product_events,或在 cli 层明示各工作流 dry-run 语义;H 列格式由唯一渲染函数产出。

## 三、设计原则

1. 三类事实分列,互不覆盖。
   - 回执维度:沃尔玛说了什么。字段为 feed_items.status/basis/evidence/wm_status/resolved_at/last_read_at。
   - 观测维度:目录扫描和单查看到了什么。字段为 feed_items.effect/effect_basis/effect_at/effect_evidence。
   - 本地事实:我们做了什么、遇到了什么。包括 feed_attempts 的 claimed_at/post_started_at/http_status/net_phase/exc/body_snip;feed_log 的 last_poll_*/hold_reason;feed_items.projected_at;锁文件 pid;凭证表的在册/在营状态。

2. 终态必带依据。
   - 每个终态都要有 basis 枚举、evidence 原文、时刻三样。
   - 推断类 basis 在名字上就标明是推断(deadline、http_404、store_disabled、store_unregistered、reconcile_exhausted、arrived_unlinked、probe_inconclusive、legacy_*),evidence 写所用事实和规则出处(walmart_slas.tsv 的哪一行哪一列、哪两次 404、哪一轮 complete 扫描)。
   - 存量一律标 legacy 或 legacy_*,不补编事实。
   - B8 起库层用 IMMUTABLE 词表函数加 CHECK … NOT VALID 钉住六本账:feed_log 与 feed_items 校验 status 与 basis 的组合;dispositions、retire_cooldown、sku_migrations、upc_pool 校验「终态必须带依据键」。扩词表用 CREATE OR REPLACE,只放宽、不收窄。

3. 首次事实不可变,只有推断可以被升级。
   - 回执从 submitted 落到终态只写一次;resolved_at 就是首次落定时刻。ops.feed_item_errors 只收落定那一刻的码。
   - 重读结果不同时,只追加 evidence.rereads;中途码记 evidence.interim。
   - lapsed 可被后到的回执升级为 success/failed,此时才写码。
   - effect 的 not_effective/retired,在 14 天内可被 ⑥ 404 改判为 effective。
   - 可变列明示如下,变更前原值一律写进 history:feed_attempts.last_reread_at、log_final_*;feed_items.last_read_at、wm_status、basis(仅限 submitted 行)、projected_at。

4. 不确定不等于失败。failed 只有五个来源:
   - ③ 4xx(含 401);
   - ③ 确定未发出(not_sent);
   - ④ 有覆盖证明的双确认未达(probe_not_found);对账器对 MP_ITEM/MATCH 还要求 posted_at + EFFECT_WINDOW 之后 ⑥ 全部真 404;
   - ② feed 级 ERROR;
   - ① 逐条 *_ERROR。
   其余情况留 pending 交对账器,或落 lapsed 交观测。

5. 不可逆动作只挂强事实。这里的不可逆动作包括:弃码四点(位置不变)、烧号、UPC 回收、改码的当场回滚与定案、sku_locked_heal 清列。强事实清单如下,状态模型与此处一致:
   - ① 逐条 failed,且 receipt_class ∈ {refused, refused_permanent}。不含 review(ASYNC)、ambiguous(RETIRE 的 ERR_PDI_0004)、gone。
   - ① missing(detail_absent),前提是 detail_count == itemsReceived > 0,且明细 SKU ⊆ 本片 skus。
   - ② feed_error;legacy_feed_error 的推断链经读码核实(SC8),也在此列。
   - ③ http_4xx、not_sent。只有这两项允许回收 UPC。
   - ④ probe_not_found 只证明「本片没被受理」,允许同方法重发和释放载荷锁。MP_ITEM/MATCH 的 SKU 级不可逆动作(改码回滚)另需窗口后 ⑥ 真 404。UPC 不因 probe_not_found 回收,同 ASIN 重试时复用原号。
   - ⑤ 与 ⑥ 的组合,见原则 10。
   另外两条:
   - ⑥ 404 只在 posted_at + EFFECT_WINDOW 之后,才能作为「未达 / 未生效」的事实;窗口内一律不作数。
   - 「因我方动作才消失」这类结论(sku_locked_heal 清列),需要提交前的基线 ⑥=200。

6. 锁跟着台账终态走,不另设时钟,也不设时间防重窗。
   - 载荷锁:feed_log_dedupe_uidx,每个载荷一行,条件重占。可重占的状态只有 done、failed,以及 lapsed ∧ reopenable。reopenable 由 services 判定,api 只照办。
   - SKU 级在途判据只有一个出处:feed_track.open_skus(conn, …, cap_hours=None, success_until_seen=False, aliases=True)。
     - problem_scan 调 open_skus(cap_hours=48, success_until_seen=True),逐字保留 2026-08-11 定稿的口径与 sku_aliases 一跳继承。
     - 其余消费方默认不封顶;MP_ITEM/MATCH 在 effect 为 NULL 时一直拦着(落实 2026-09-07「MATCH 不一刀切」)。
   - 载荷锁防的是崩溃窗口里的并发双发,SKU 闸防的是业务重发。两者是不同的能力,不算双轨。
   - 投影未成功就先投影:台账行的 projected_at 为空,说明表还没回写,这时只补写表、不重发。这是状态判据,不是时间窗。

7. 对账器和核验器只读沃尔玛,永不 POST/PUT。
   - 重发只由原业务工作流在下一轮用同一方法完成;写操作永不自动兜底,换方法重试依旧禁止。
   - 内联反查后同载荷补交,是现行红线「确认未达 → 同一方法补交」,不在本条禁止之列。

8. 写者分区:一类写入只有一个写者,一份判据。
   - ops.feed_log:pending 的产生与全部出口(_log_claim、_post、_submit_one、settle_deferred、adopt_found、reject_adoption、close_pending)只在 api/feeds;submitted/lapsed 的出口(done、failed(feed_error)、lapsed 及其升级)只在 services/feed_track.close_feed。api/feeds.mark_feed_done 删除。
   - ops.feed_attempts:kind=post/repost/probe 只由 api/feeds 写(反查行由 find_recent_feed 自己写,内联与对账器调用同一函数);kind=put 只由 services/feed_track.sync_begin/sync_finish 写。
   - ops.feed_items:提交与收编时由 api/feeds.adopt_found 插入;之后只由 services/feed_track 写。
   - 判据的唯一出处:
     - services/feed_track:FEED_DEADLINE_HOURS、EFFECT_WINDOW_HOURS 及其出处表 EFFECT_WINDOW_SRC、receipt_class、open_skus、observe、probe_item、judge_effect、attempts、latest_attempt;
     - registry.resources:码集;
     - services/dispositions.settle_maintenance:维护类生效结论。
   - 守门:INSERT/UPDATE ops.feed_log 只出现在 api/feeds.py 和 services/feed_track.py 的上述函数里;sku_migrate、sku_locked_heal、各 sheet 模块不得直接引用 WALMART_ERR_* 码集(统一经 receipt_class)。

9. 分层遵守三条铁律。
   - api 层只做接口适配:
     - net 出参区分 pre/connect/read/refresh_after_401;
     - FeedQueryError/FeedNotFound;
     - 200 空体抛 EmptyItemResponse;
     - 翻页;
     - 原子抢占;
     - 按调用方给的 registry 词写防重账。
   - api 层不做 ⑥ 单查,不按 feedType 走业务支路,不 import services。
   - 新增的端点消费者,同批登记到蓝图「工作流×端点矩阵」。

10. 观测质量入账,观测不对称。观测判据只读 scan_rounds、w.last_seen_at 和 ⑥,不读 missing_since。
   - 正向结论(在架、已退役、现值等于目标):只要求观测时间晚于提交。
   - DELETE 的「已删」:posted_at 之后 ⑥ 真 404。
     - 若 posted_at 之后最近一次 complete 轮没有列出它(last_seen_at < 该轮 run_at,或 walmart_items 无行),一次 404 即可;
     - 否则(仍被列出,即僵尸;或提交后还没有 complete 轮),需要两次 404,间隔 ≥24h。
   - 负向结论(缺席、仍在架、值没变),需同时满足:
     - 已过 EFFECT_WINDOW;
     - 字段新鲜(avail_seen_at、节点 seen_at);
     - 窗口之后的 complete 轮没有列出它;
     - 有 ⑥ 佐证。
     没有 complete 轮的店,改为两次 ⑥ 404 间隔 ≥24h。
   - 200 空体一律 fail-closed,不当作 404。

11. 分批可独立合并,可 dry-run。
   - schema.sql 中本设计的新增段用 -- >>> ledger-v2 / -- <<< ledger-v2 包住。段内只写 DDL:不写 DROP、不写 INSERT/UPDATE/DELETE,也不对本设计新增的约束写 DROP CONSTRAINT。
   - 既有的 DROP VIEW/TABLE/COLUMN 与 UPDATE 不在本设计守门范围内;全文件不含 DROP INDEX 的既有守门保留。
   - 存量回填走一次性工作流 workflows/feed_ledger_backfill:不带 -p step 时 ⛔ 硬拒;每步先 --dry-run 报条数,再分批执行。
   - 动了表就同步 refdata/schema.sql 与 docs/db_schema.md。

12. 所有者定稿的处置。
   推翻或修改的有:
   - 2026-08-16「pending 不做对账器」(D1);
   - verify_deletions 的 gone 口径与 48h 宽限(D5);
   - 弃码点 2 只凭回执(D6);
   - expire 落 ineffective(D7);
   - sku_migrate 的 24h 观测期、闸⑤④ 整店口径、wpid 不同不探测(D8);
   - 2026-09-09 永久拒码不分 feedType,以及 ERR_PDI_0004 的定性(D9);
   - 提交层失败不限次重排(D12);
   - 「PUT 不进台账」(D13);
   - feed_poll 空跑照写(D14);
   - maint_sheet 表侧 3 天时钟(D15);
   - 维护统一宽限 2h(D16)。
   保留不动的有:
   - 删除以观测定案,且收紧观测质量;
   - receipt_in_ledger;
   - problem_scan 在途口径,含 48h 封顶;
   - 删除/停用重发无实害;
   - MATCH 不一刀切;
   - double 定稿;
   - 不设时间防重窗(本轮撤掉上一版的 96h);
   - conventions §四 店级失败标准(_PRE_FAIL 不上抛,原样保留)。

## 四、状态模型


### ops.feed_log

- **pending**(终态=否)
  - 含义:载荷锁已占,当前尝试(attempt_id 指向)的提交结局还没收口。 basis 细分: - claimed:已抢占,post_started_at 尚未写入;进程可能在 rate_acquire 稀缺桶里睡眠,或正在取 token。 - in_flight:post_started_at 已单独提交;POST 在飞,或进程死在 POST 中。 - post_uncertain:首发 5xx、read 阶段异常、2xx 却无 feedId 或响应不是 dict,且内联反查 UNKNOWN。 - repost_uncertain:④ 双确认未达后同载荷补交,补交又不确定,补交后再反查仍 UNKNOWN。 - deferred:list_new 延后结算的句柄仍在手。 - window_wait:MP_ITEM/MATCH;对账器 ④ 没找到,但仍在 EFFECT_WINDOW 内。 - adopt_mismatch:收编被首读 ⊆ 校验证伪。 - adopt_conflict:收编撞上 feed_log_feed_id_uidx。 - legacy:B1 之前的存量,没有条数和 SKU 清单。
  - 依据:③ 当前 attempt 行:claimed_at、post_started_at、http_status、net_phase(pre / connect / read / refresh_after_401)、exc、body_snip(截 500)。 ④ evidence.probes[]:每次反查的时刻、窗口、覆盖证明、候选。 被否决的 feedId 记 evidence.rejected_feed_ids。 item_count、skus、refs 在抢占事务内写入,B1 起必有。
  - 写者:只由 api/feeds 写: - _log_claim:INSERT,或条件重占 UPDATE … RETURNING; - _post:单独提交 post_started_at,并置 in_flight; - _submit_one、settle_deferred; - find_recent_feed:追加 probes; - reject_adoption:证伪回退,由 services/feed_track.poll_feed 判定后调用; - close_pending:对账器置 window_wait,由 services/feed_track.reconcile_pending 调用。
  - 防重/锁:占 feed_log_dedupe_uidx:同载荷返回 dedup,附 prev{status, basis, feed_id, workflow, attempt_id, updated_at};skus 计入 feed_track.open_skus。
- **submitted**(终态=否)
  - 含义:已持有 feedId,等回执。basis: - post_2xx:③ POST 2xx 且带 feedId; - probe_found:④ 列表条目命中,且首读明细 SKU 全部 ⊆ 本片 skus; - probe_found_unverified:④ 命中,但明细还是空的;首次读到非空明细时再验; - manual; - legacy。
  - 依据:③ POST 响应里的 feedId,或 ④ 命中条目原文(feedId / feedDate / itemsReceived / feedType)加覆盖证明。 posted_at 取被受理那次 POST 的 post_started_at;存量取 updated_at,并在 evidence 注明。 轮询侧只写 last_poll_at、last_poll_error、poll_error_class(store_retry.diagnose 六档)、first_404_at、last_404_at、hold_reason。
  - 写者:api/feeds.adopt_found(原 _ok_result 改名公开):feed_log、feed_items、attempt 三处在同一事务写。以下路径共用这一个函数:内联收编、延后结算、对账器收编、feed_poll -p resolve。
  - 防重/锁:占锁;是 poll_all 的轮询对象。
- **done**(终态=是)
  - 含义:沃尔玛已给出 feed 级结论,台账每个 SKU 都有回执终态。basis: - feed_processed:② PROCESSED,且 ① 明细读全(三方计数核对通过); - items_complete:② 汇总仍非终态,但 ① 台账 SKU 已全部终态,即汇总停更;保留 PR #131 行为; - manual; - legacy。
  - 依据:② head 快照 {feedStatus, itemsReceived, itemsSucceeded, itemsFailed, modifiedDtm},加 detail_count 与 item_count;basis_at 为读取时刻。
  - 写者:services/feed_track.close_feed,在落账的同一事务里完成: - 落账事务先 SELECT … FOR UPDATE 锁住该 feed_log 行; - 同时写当前 attempt 的 log_final_*。 api/feeds.mark_feed_done 删除。
  - 防重/锁:可重占(定稿:上一笔完结后,同载荷再发是新的合法操作)。
- **failed**(终态=是)
  - 含义:本次尝试沃尔玛一条都没受理。basis: - http_4xx:③ 4xx,含 POST 401;收到 401 后刷新 token 撞代理也归此档,evidence.refresh_exc 记换 token 时的异常; - not_sent:③ 本地确定没发出。pre 阶段:token 异常(含 StoreDeadError 与 token 阶段 StoreProxyError),或 rate_acquire 异常;connect 阶段;never_posted:post_started_at 从未写入,且原进程已不在; - probe_not_found:④ 覆盖证明加 30s 双确认,且反查发生在最后一次 POST 之后。内联反查只凭 ④;对账器对 MP_ITEM/MATCH 另需 posted_at + EFFECT_WINDOW 之后 ⑥ 本片全部真 404; - feed_error:② feedStatus=ERROR; - manual; - legacy。
  - 依据:- http_4xx:状态码加响应体截断;429 标 retryable。 - not_sent:异常类名、阶段、diagnose 词。 - probe_not_found:两次查询的时刻、窗口、覆盖证明;MP_ITEM/MATCH 另记 ⑥ 逐 SKU 结果与窗口出处。 - feed_error:feed 级 ingestionErrors 原文。
  - 写者:- 提交当场:api/feeds._post / _submit_one / settle_deferred; - pending 的其余出口:api/feeds.close_pending,由 feed_track.reconcile_pending 与 feed_poll -p resolve 调用; - feed_error:services/feed_track.close_feed。
  - 防重/锁:可重占。 - UPC 回收只认 http_4xx / not_sent; - 改码当场回滚只认 http_4xx / not_sent; - probe_not_found 之下的改码,要等窗口后 ⑥(见 sku_migrations)。
- **lapsed**(终态=是)
  - 含义:回执不会再来,回执维度关闭;SKU 的业务结局交给观测维度(推断)。basis: - deadline; - http_404; - store_disabled; - store_unregistered; - reconcile_exhausted:pending 已过 FEED_DEADLINE,期限后至少有一次成功的列表读取,结果仍 UNKNOWN; - arrived_unlinked:MP_ITEM/MATCH 窗口后 ⑥ 有 SKU 返回 200,说明已到达,但连不上 feedId; - manual。
  - 依据:- deadline:{hours, src:'refdata/walmart_slas.tsv#<操作行>/我方判死期限(建议)', posted_at, last_read_at, last_head},且期限点之后有一次成功读取。 - http_404:{两次 404 的时刻, age_days, 经验出处:A085 第 29 天仍可查,A109 第 32 天、A162 第 38 天为 404}。 - store_disabled:{registered 与 enabled 两个集合的读取时刻, 期限}。 - store_unregistered:{registered_names 读取时刻}。 - reconcile_exhausted:{probes 全量}。 - arrived_unlinked:{⑥ 逐 SKU 的 200/404 及时刻}。
  - 写者:- submitted → lapsed:services/feed_track.close_feed; - pending → lapsed:api/feeds.close_pending,由对账器调用。同事务按 attempt.skus/refs 插入 feed_items(feed_id='unlinked:<attempt_id>',source='reconcile',status=lapsed)。 reopenable 由 feed_track 判定后写入。
  - 防重/锁:只有 status='lapsed' 且 reopenable 才可重占。 - DELETE / RETIRE / price / inventory / MP_INVENTORY / MP_MAINTENANCE:收口即置 reopenable; - MP_ITEM / MP_ITEM_MATCH:本 feed 每个 lapsed SKU 都拿到 effect 后才置 true。

### ops.feed_attempts(kind=post/repost)

- **outcome ∈ accepted / rejected / not_sent / uncertain(NULL = 尚未返回)**(终态=是)
  - 含义:每次 POST、补交各一行,行不删。 只写一次的列:claimed_at、post_started_at、finished_at、http_status、net_phase、exc、body_snip、item_count、skus、refs、outcome、feed_id。 可变列: - log_final_status / log_final_basis / log_closed_at:所属 feed_log 收口或 lapsed 升级时写,旧值进 evidence.history; - last_reread_at。
  - 依据:③ 全部本地传输事实。net_phase 的取值:pre = token / rate / StoreDead 阶段;connect = 连接阶段白名单;read = 读阶段或其它;refresh_after_401 = 已收到 401、刷新 token 失败。
  - 写者:- api/feeds._log_claim / _post / _submit_one / settle_deferred; - log_final_*:由收口方在同一事务写(api/feeds.close_pending,或 services/feed_track.close_feed)。
  - 防重/锁:本身不作锁。是以下判定的数据源:attempts() 计次、latest_attempt() 绑定本次尝试、反查占用集(feed_log ∪ feed_attempts 的 feed_id)、never_posted、never_claimed、孤儿 UPC。「B1 切换时刻」取 min(feed_attempts.claimed_at);禁止往这张表回填历史行。

### ops.feed_attempts(kind=probe)

- **outcome ∈ found / not_found / probe_failed / misadopted**(终态=是)
  - 含义:一次反查、对账或首读校验的结论;parent_id 指向所属的 post 行。
  - 依据:- ④ 命中条目原文; - 窗口:[post_started_at−5min, post_started_at+60min]; - 覆盖证明方式; - 首读 ⊆ 校验结果; - 对账器窗口后的 ⑥ 逐 SKU 结果(由 services 算好后作为 evidence 传入); - 失败时记 diagnose 档。
  - 写者:- api/feeds.find_recent_feed:内联反查与对账器调用的是同一个函数,各自写行; - api/feeds.reject_adoption:写 misadopted。
  - 防重/锁:- found ⇒ adopt_found; - not_found ⇒ 按 feedType 与窗口,落 failed(probe_not_found) 或保持 window_wait; - misadopted ⇒ 回 pending,并排除该 feedId。

### ops.feed_attempts(kind=put)

- **outcome ∈ accepted / rejected / not_sent / uncertain(NULL = PUT 未返回)**(终态=是)
  - 含义:单品 PUT 每次一行:先写行、再调接口。
  - 依据:claimed_at;post_started_at(调用 api 前单独提交);finished_at、http_status、net_phase、exc、body_snip;目标值;节点端点另记 nodes[].status 与 errors[]。
  - 写者:- services/feed_track.sync_begin:调用前写,单独提交; - services/feed_track.sync_finish:返回后写,同一事务写入 feed_items 的 'sync:<attempt_id>' 行。
  - 防重/锁:进程死在 PUT 中时,outcome 保持 NULL。对账器借得该工作流的锁后处理:post_started_at 为空 ⇒ feed_items 落 failed(sync_not_sent);非空 ⇒ 落 lapsed(sync_uncertain)。

### ops.feed_items

- **submitted**(终态=否)
  - 含义:SKU 在途,还没有 ① 终态。basis 说明为什么还开着: - NULL:还没读过; - wm_inprogress; - wm_unknown_enum:wm_status 存原值; - async_review:失败状态但带 ASYNC 审核码; - detail_incomplete:② itemsReceived 与已读明细条数或本地 item_count 对不上;含 PROCESSED 且 itemsReceived=0、但 item_count>0 的情况; - detail_key_mismatch:明细含本片之外的 SKU,同时本片有 SKU 缺席; - head_stale; - legacy_bad_key。
  - 依据:wm_status 原值、last_read_at;中途码写 evidence.interim;resolved_at 恒为 NULL。
  - 写者:- api/feeds.adopt_found:提交与收编时插入,带 ref、attempt_id、source='local'; - services/feed_track.poll_feed:只写 wm_status、last_read_at、basis、evidence。首读 ⊆ 校验通过之后,才把明细里多出的 SKU 以 source='detail' 插入。
  - 防重/锁:open_skus 视为在途。problem_scan 的定稿口径经 open_skus(cap_hours=48) 调用。
- **success**(终态=是)
  - 含义:沃尔玛逐条回执 SUCCESS(带码即带警告的成功),或单品 PUT 成功。
  - 依据:basis: - item_receipt:① ingestionStatus=SUCCESS;落定那一刻的全码集写进 ops.feed_item_errors; - sync_http:③ PUT 2xx 且目标节点 OK; - manual; - legacy。 resolved_at 为首次落定时刻,之后不改。重读结果不同时,追加 evidence.rereads[{at, wm_status, codes}] 并告警。
  - 写者:- services/feed_track.poll_feed:UPDATE … WHERE status='submitted' RETURNING sku;回执事件、违禁黑名单、feed_item_errors 只由 RETURNING 驱动; - services/feed_track.sync_finish。
  - 防重/锁:problem_scan 定稿「success ∧ resolved_at > last_seen_at ⇒ 待观测」原样保留,经 open_skus(success_until_seen=True) 执行。resolved_at 回到首次落定语义后,catalog_sync 重扫一次即放行。
- **failed**(终态=是)
  - 含义:沃尔玛明确拒了这一条,或整个 feed 被拒收。
  - 依据:basis: - item_receipt:① *_ERROR 加全码集; - feed_error:② 零明细 ERROR,码取 feed 级 ingestionErrors; - legacy_feed_error:回填推断,条件见 SC8; - sync_http_4xx:③ PUT 4xx,或节点失败; - sync_not_sent:PUT 未发出,或进程死在发出之前; - manual; - legacy。
  - 写者:- services/feed_track.poll_feed、sync_finish; - feed_track.reconcile_pending:sync_not_sent; - feed_ledger_backfill:legacy_feed_error。
  - 防重/锁:不拦。要不要再发按 receipt_class 分档:refused_permanent 停止;refused 可以重试,同码连续 2 次停止(D12);ambiguous 与 review 交给观测。
- **missing**(终态=是)
  - 含义:明细已读全,沃尔玛确实没收到这一条。
  - 依据:basis: - detail_absent:② 汇总终态;已读明细原始条数 == head.itemsReceived > 0;明细 SKU 全部 ⊆ 本片 skus;该 SKU 不在其中。evidence 记 {items_received, detail_count, item_count}。 - legacy / legacy_bad_key / manual。 legacy 的 missing 被 receipt_class 判为 no_receipt,不算强事实。
  - 写者:services/feed_track.poll_feed:汇总终态,且三方计数核对通过时写。
  - 防重/锁:不拦。detail_absent 属于强事实(原则 5):处置账判 not_received;冷却表落 lapsed;改码可以回滚。
- **lapsed**(终态=是)
  - 含义:这一条的回执不会再来,回执维度关闭(推断)。
  - 依据:basis: - deadline; - async_deadline:ASYNC 码,120h; - http_404; - store_disabled; - store_unregistered; - adopt_mismatch; - sync_uncertain:PUT 返回 None 或 5xx,或进程死在 POST 已开始之后; - reconcile_exhausted; - arrived_unlinked; - legacy_unlinked:B5 回填时,为存量 executing 处置补的挂账行; - manual。 evidence 记最后一次 wm_status、last_read_at、期限值及出处。
  - 写者:- services/feed_track.close_feed、sync_finish、reconcile_pending; - api/feeds.close_pending:插入 unlinked 行; - feed_ledger_backfill:legacy_unlinked。
  - 防重/锁:- problem_scan 在途闸不计(与 failed 相同); - open_skus 对 MP_ITEM/MATCH 且 effect IS NULL 的行仍算在途; - 低频补读:每 4h 一次,最长 35 天;读到回执就升级为 success/failed,此时才写 feed_item_errors,原判写进 evidence.history。

### ops.feed_items(effect 维度)

- **effect IS NULL**(终态=否)
  - 含义:待观测核验。 - 只适用 DELETE_ITEM / RETIRE_ITEM / MP_ITEM / MP_ITEM_MATCH; - 维护类(price / inventory / MP_INVENTORY / MP_MAINTENANCE / PUT_*)不用 effect,生效结论只在 dispositions.settle_maintenance 给出。
  - 依据:无。核验窗口从 COALESCE(attempt.post_started_at, submitted_at) 起算。
  - 写者:—
  - 防重/锁:MP_ITEM/MATCH 的 lapsed 行计入 open_skus;对应 feed_log 的 reopenable 保持 false。
- **effective**(终态=是)
  - 含义:观测证明这次提交的目的已经达成。判据全部由 feed_track.judge_effect 一处实现,只读 scan_rounds、w.last_seen_at 和 ⑥。
  - 依据:DELETE: - gone_probe404:posted_at 之后 ⑥ 真 404,且 posted_at 之后最近一次 complete 轮没有列出它(walmart_items 无行,或 last_seen_at < 该轮 run_at); - gone_probe404x2:仍被 complete 轮列出(僵尸),或 posted_at 之后还没有 complete 轮;需要 ⑥ 两次真 404,间隔 ≥24h。 RETIRE: - retired_scan:posted_at 之后 ⑤ lifecycle=RETIRED; - retired_probe:⑥ 返回 200,且 lifecycleStatus=RETIRED; - gone_probe404 / gone_probe404x2:同 DELETE。 MP_ITEM / MATCH: - present_scan:⑤ last_seen_at > posted_at; - present_probe:⑥ 返回 200 且非空。 其它: - legacy_event:旧 verify_deletions 的结论,经有界回填搬入; - manual。 effect_evidence 记所用 complete 轮的 run_at、单查时刻、HTTP 状态、lifecycleStatus。
  - 写者:services/feed_track.verify_effects:feed_poll 每轮调用;每店有单查预算 EFFECT_PROBE_PER_STORE。
  - 防重/锁:解除 open_skus。DELETE 的 effective 行,只要 basis 不是 legacy_event,由 catalog_sync 在弃码点 1 读取并弃码。
- **not_effective**(终态=是)
  - 含义:过了官方窗口,观测结果与提交目的相反。
  - 依据:以下都在 posted_at + EFFECT_WINDOW 之后判定: - DELETE / RETIRE:active_probe200。⑥ 返回 200、响应非空、lifecycleStatus 不是 RETIRED。 - MP_ITEM / MATCH:absent_probe404。窗口后最近一次 complete 轮未列出它,加 ⑥ 真 404。 - MP_ITEM / MATCH:absent_probe404x2。窗口后还没有 complete 轮时,需要 ⑥ 两次 404,间隔 ≥24h。 - 带 ASYNC 码的行,窗口为 120h。 - 另有 legacy_event、manual。 14 天内出现 effective 事实可以改判,原判写进 effect_evidence.history。
  - 写者:services/feed_track.verify_effects
  - 防重/锁:解除 open_skus;允许原业务工作流下一轮用原方法重发。
- **retired(仅 DELETE)**(终态=是)
  - 含义:要删的条目还在目录里,状态是 RETIRED,并没有被删掉。
  - 依据:retired_probe:过 72h 窗口后,⑥ 返回 200 且 lifecycleStatus=RETIRED。⑤ 看到的 RETIRED 只用来触发单查。14 天内若 ⑥ 返回 404,可改判为 effective。
  - 写者:services/feed_track.verify_effects(同时记 delete_retired 事件)
  - 防重/锁:- 不弃码、不烧号; - 处置落 ineffective(settled_by=effect:retired); - problem_scan 的 retired_gate 跳过该 SKU(D5)。
- **not_applicable**(终态=是)
  - 含义:回执已表明没被受理或没被执行,不需要核验。
  - 依据:- receipt_refused:status=failed,且 receipt_class ∈ {refused, refused_permanent, feed_level};不含 gone、ambiguous、review。 - not_received:missing,且 basis=detail_absent。
  - 写者:services/feed_track.verify_effects 首轮
  - 防重/锁:无
- **unverifiable**(终态=是)
  - 含义:无法核验(推断,已关闭)。
  - 依据:- store_disabled / store_unregistered:已过窗口; - probe_inconclusive:窗口后 14 天内,⑥ 始终是 200 空体或查询失败; - legacy:B4 之前提交、且早于 14 天的存量。 在营但不可达的店不落这一档:effect 保持 NULL,由 hold_reason 点名。
  - 写者:services/feed_track.verify_effects;feed_ledger_backfill(legacy)
  - 防重/锁:- 解除 open_skus; - 对应处置落 lapsed; - MP_ITEM/MATCH 可以置 reopenable。

### ops.dispositions

- **suggested**(终态=否)
  - 含义:扫描件给出的建议(不变)。
  - 依据:sources 每格 {action, code, reason, at}
  - 写者:services/dispositions.suggest_many
  - 防重/锁:dispositions_open_uidx
- **executing**(终态=否)
  - 含义:本行对应的提交已进台账,按 ref='disp:<id>' 收编。
  - 依据:- feed_id 取值:真 feedId / 'sync:<attempt_id>' / 'unlinked:<attempt_id>' / 'legacy:<disp_id>'(B5 回填); - executed_at 取 attempt.post_started_at; - 店在营但不可达时,detail.hold 写 {reason, diagnose 档, since}。
  - 写者:services/dispositions.adopt_from_ledger:转 executing 的唯一路径。提交后与每轮开头都调;同时补写 *_submitted 事件,事件按 (店, SKU, 事件, feed_id) 先查重。
  - 防重/锁:dispositions_open_uidx;sku_migrate 的「无未了结破坏建议」。
- **confirmed**(终态=是)
  - 含义:处置目的已达成。
  - 依据:detail.settled_by 的取值: - effect:<effect_basis>:按本行 feed_id 对位;retire 行读 RETIRE 自己的 effect; - receipt_gone:① ITEM_GONE 码,沿用 2026-09-09 定稿;身份层仍要等 effect; - observed:维护类;窗口过后,字段新鲜的 ⑤ 现值等于目标,不论回执是什么; - manual。 detail 另带 feed_id、receipt_class、error_codes、obs_at。
  - 写者:services/dispositions.settle / settle_maintenance / resolve_manual
  - 防重/锁:释放索引
- **ineffective**(终态=是)
  - 含义:有事实证明没有生效。
  - 依据:detail.settled_by 的取值: - effect_not_effective; - effect:retired:DELETE 观测到 RETIRED,没删掉,只是退役; - receipt_refused:① refused 或 ② feed_error; - not_received:missing detail_absent; - accepted_not_applied:维护类;回执 success,但窗口后字段新鲜的现值 ≠ 目标; - value_unchanged:维护类;回执已终态,既非 success 也非 refused(例如 lapsed、sync_uncertain),窗口后字段新鲜的现值 ≠ 目标; - receipt_refused_unobserved:维护类;回执 refused,EXPIRE_DAYS 内一直没有新鲜观测; - sync_4xx_repeat:同 SKU、同目标值,连续 2 次同码 4xx; - manual。
  - 写者:services/dispositions.settle / settle_maintenance / resolve_manual
  - 防重/锁:释放索引,下轮可以重新建议。 - effect:retired 由 problem_scan 的 retired_gate 止损; - accepted_not_applied 连续 2 次,由 maintenance_scan 止损(D16)。
- **lapsed(新增)**(终态=是)
  - 含义:超期未证,或无法核验:已关闭,但没有结论性事实。
  - 依据:detail.settled_by 的取值: - expired:absent / store_unscanned / node_missing / inv_stale / receipt_open(维护三类,EXPIRE_DAYS=3 保留); - unverifiable:<effect_basis>(破坏类); - ledger_row_missing:feed_log 已终态,但台账缺本行; - relist_absent:存量反补行提交后缺席; - manual。
  - 写者:services/dispositions.settle / settle_maintenance / expire_executing / resolve_manual
  - 防重/锁:释放索引(部分唯一索引只覆盖 suggested/executing)。
- **withdrawn**(终态=是)
  - 含义:本轮不再建议。
  - 依据:detail.withdrawn_reason 必填且结构化,取值: - gate:inflight:<feed_id> - gate:item_gone:<code> - gate:permanent:<code> - gate:retired - gate:recoverable_only - gate:accepted_not_applied_repeat - gate:hold_multinode - switch:TITLE_SYNC=False - store_disabled - value_at_target:<最近一次台账 feed_id> - not_suggested
  - 写者:services/dispositions.withdraw_stale:新增 reasons 参数;keep 为空的分支也写原因。
  - 防重/锁:释放

### listing.sku_migrations

- **pending(submitted_at 为 NULL)**(终态=否)
  - 含义:改码行已落库,还没对上台账。
  - 依据:每轮 _settle 开头依次判定: ① ops.feed_items 里有 workflow='sku_migrate' 且 sku=new_sku 的行 ⇒ 回填 feed_id 与 submitted_at。新码全表唯一,匹配是精确事实。 ② feed_attempts 里有 skus 包含新码的行,看对应 feed_log: - pending ⇒ 等对账; - failed(http_4xx / not_sent)⇒ rolled_back; - failed(probe_not_found)⇒ 记 detail.submit='probe_not_found:<attempt_id>',submitted_at 取 post_started_at,等窗口后按 (d) 定案; - lapsed ⇒ feed_id 记 'unlinked:<attempt_id>',交观测。 ③ never_claimed,须同时满足:锁文件 pid = 本进程(本进程持有 sku_migrate 锁);行的 created_at 早于本进程启动;_settle 在 _migrate 之前运行;created_at 晚于 min(feed_attempts.claimed_at);没有任何 attempt 含该新码。满足 ⇒ rolled_back。 B1 之前的存量没有 attempt 信息,点名,交 sku_migrate -p resolve 人工处理。
  - 写者:workflows/sku_migrate._settle:新增 _backfill_unsent,替代只告警的 _SQL_UNSENT。
  - 防重/锁:sku_migrations_open_uidx;节奏闸计入 open。
- **pending(已提交)**(终态=否)
  - 含义:等回执或观测定案。
  - 依据:feed_id 与 submitted_at 取自台账。
  - 写者:workflows/sku_migrate._migrate / _settle
  - 防重/锁:同上。闸⑤ 只拦本店 workflow=sku_migrate 的 feed_log pending 行。
- **double**(终态=否)
  - 含义:新旧码同时在架(2026-09-07 定稿不变)。
  - 依据:⑤ 两码都在架。新增出口: - ⑥ 单查旧码真 404,不论 wpid 是否相同 ⇒ confirmed。同 wpid 记 shadow_404,不同 wpid 记 old_gone_404。 - 没有 complete 轮的店:两次 404,间隔 ≥24h。 - 旧码返回 200(包括 RETIRED)⇒ 保持 double,交人工(D8)。 - 200 空体 fail-closed。
  - 写者:workflows/sku_migrate._settle;观测与单查经 feed_track.observe / probe_item。
  - 防重/锁:不进节奏闸 open;候选判据挡住第二条台账。
- **stalled(改为非终态)**(终态=否)
  - 含义:挂起:超过 72h 仍判不出,或回执成功却看不到新码;每轮继续重判。
  - 依据:detail.stall_reason 取值:store_unreachable / no_complete_scan / probe_inconclusive / receipt_success_but_absent / both_absent / legacy。另记最后一次 complete 轮的时刻。
  - 写者:workflows/sku_migrate._settle(_SQL_OBSERVE 取 pending ∪ double ∪ stalled);sku_migrate -p resolve
  - 防重/锁:节奏闸仍计入 open(定稿:stalled 仍拦)。
- **confirmed**(终态=是)
  - 含义:改码已生效。
  - 依据:前提:新码 present(正向,只要求观测晚于提交)。detail.verdict_basis 取值: - old_absent_probe404:旧码在 submitted_at 之后最近一次 complete 轮未被列出,且 ⑥ 真 404; - old_probe404x2:没有 complete 轮时,⑥ 两次 404,间隔 ≥24h; - shadow_404; - old_gone_404; - manual。 detail 另记所用扫描轮的 run_at、单查时刻与状态。
  - 写者:workflows/sku_migrate._confirm(弃码点 4,位置不变;人工 resolve 也经此函数)
  - 防重/锁:—
- **rolled_back**(终态=是)
  - 含义:确定没有生效。
  - 依据:detail.verdict_basis 取值: - receipt_refused:① refused,不是 ASYNC,也不是 ambiguous; - feed_error / legacy_feed_error; - not_received:missing detail_absent; - submit_failed:http_4xx|not_sent; - submit_failed:probe_not_found+new_absent:窗口后,新码 absent_probe404 或 absent_probe404x2; - never_claimed; - effect_not_effective:72h 后新码 absent、旧码仍在,且新码回执不是 success / review / open; - manual。 回执为 success 或 review 而新码 404 时,不回滚,落 stalled(receipt_success_but_absent)。
  - 写者:workflows/sku_migrate._roll_back(人工 resolve 也经此函数)
  - 防重/锁:候选闸:同一 (店, 旧码) 的历次改码里,只要有一次新码回执为 success,或 effect 不是 not_effective,就不再自动成为候选。

### listing.retire_cooldown

- **pending**(终态=否)
  - 含义:RETIRE 已受理,冷却中。
  - 依据:feed_id 取自台账。evidence.baseline{at, http, lifecycle}:提交 RETIRE 之前由 sku_locked_heal 做一次 ⑥ 取得。按台账补建的冷却行没有基线,evidence 注明。
  - 写者:workflows/sku_locked_heal:提交后写入;每轮按台账补建 workflow=sku_locked_heal 的 RETIRE 行所缺的冷却行。
  - 防重/锁:retire_cooldown_open_uk;sku_migrate 只对同一个码的候选避让(D8)。
- **cleared**(终态=是)
  - 含义:退役已证实,可以清列重上(弃码点 2,位置不变)。
  - 依据:basis 取值: - probe_retired:冷却期满后 ⑥ 返回 200,且 lifecycleStatus=RETIRED; - probe_404_after_baseline:基线 ⑥ 为 200,冷却期满 ⑥ 为真 404; - manual。 回执是 success、ITEM_GONE 还是 ERR_PDI_0004,都只作辅证。
  - 写者:workflows/sku_locked_heal._relist / resolve
  - 防重/锁:—
- **failed**(终态=是)
  - 含义:退役没成,或无法证实,转人工。
  - 依据:basis 取值: - not_effective:回执 ok 或 ambiguous,posted_at+48h 后 ⑥ 仍为 ACTIVE; - receipt_repeat:同码 refused 连续 2 次,且 ⑥ 仍为 ACTIVE; - baseline_404_unverifiable:基线为 404,期满仍 404,没有观测可以证明(D6 默认); - manual。
  - 写者:workflows/sku_locked_heal._relist / resolve
  - 防重/锁:failed_pairs 只取这一状态。
- **lapsed(新增)**(终态=是)
  - 含义:没有结论性事实。关闭后允许自动重新退役。
  - 依据:basis 取值: - not_received; - receipt_lapsed; - receipt_refused:首次,⑥ 仍为 ACTIVE; - probe_inconclusive:持续 14 天。
  - 写者:workflows/sku_locked_heal._relist
  - 防重/锁:释放唯一索引(DELETE/RETIRE 重发无实害,定稿)。

### catalog.upc_pool

- **claimed / used / ''(回收)/ conflict / burned_***(终态=否)
  - 含义:- used:号已随被受理的 feed 发出。 - 回收(''):只回收确定没用上的号。 - uncertain、pending、lapsed、probe_not_found 状态的号永不回收(同 ASIN 重试时复用原号);Unknown 永不回收的生死规则不变。
  - 依据:新增 status_basis、status_ref、status_at 三列。回收原因与依据: - rejected ⇐ feed_log.failed(http_4xx); - not_sent(新增原因)⇐ failed(not_sent); - prep_failed ⇐ 提交前本地失败;或孤儿号:claimed,且 claimed_at 晚于 min(feed_attempts.claimed_at),没有任何 attempt 的 refs 含 'upc:<号>',list_new 锁文件 pid 为本进程,claimed_at 早于本进程启动(L26)。 - not_found 这一原因,list_new 不再使用。 status_ref 记 attempt_id。
  - 写者:services/upc_pool.claim / mark_used / release;调用方为 list_new(_apply_submit_result、开头的孤儿回收)与 listing_sheet(heal_unknown、adopt_listed)。
  - 防重/锁:同 (店, ASIN) 复用原号;回收错号就是撞库。

### catalog.walmart_items

- **avail_seen_at(新增);missing_since 语义不变,但不作核验判据**(终态=否)
  - 含义:- avail_seen_at:库存字段本轮真的从 bulk 或节点接口拉到时才写。 - 被某轮列出 ⇔ last_seen_at ≥ 该轮 run_at。核验只读这条关系加 scan_rounds,不读 missing_since(missing_since 的来源在首次缺席时就冻结了)。
  - 依据:对应的 catalog.scan_rounds 轮次
  - 写者:services/walmart_catalog.upsert_items
  - 防重/锁:—

### catalog.scan_rounds(新表)

- **complete = true / false**(终态=是)
  - 含义:每店每次同步成功记一行。complete = NOT truncated。报表兜底单查只要抛出非 EmptyItemResponse 的异常,本店同步就整体失败,不会落这一行。
  - 依据:run_at、mode、fetched、truncated、backstop / backstop_filled / backstop_404 / backstop_empty、inv_failed
  - 写者:services/walmart_catalog.record_scan_round:workflows/catalog_sync._sync_one_store 在 upsert/mark_missing 的同一事务里调用。
  - 防重/锁:judge_effect 与 sku_migrate 的所有负向判据、DELETE 的单次 404 路径,只认 complete 轮。

### catalog.product_events

- **新事件码:delete_retired、retire_verified / retire_not_effective、list_verified / list_not_effective、match_verified / match_not_effective;另有 match_presubmit_blocked(D17)。delete_verified / delete_not_effective 的 detail 改为带 feed_id 与 effect_basis**(终态=是)
  - 含义:按提交挂钩的观测事件。来源为 sku_migrate 的不记,与 receipt_in_ledger 同口径。catalog.product_risk 视图新增 delete_retired_times 列(沿用视图既有的 DROP+CREATE 做法)。
  - 依据:⑤⑥,与 effect_evidence 相同
  - 写者:services/feed_track.verify_effects,经 product_events.record_many 写入(事件码先登记在 EVENTS)
  - 防重/锁:problem_scan 的顽固判据照旧读 delete_* 事件,口径变严。

## 五、表结构变更(草案,未执行)

### SC1

```sql
-- >>> ledger-v2
-- 【SC1 · B0】ops.feed_log 事实列(纯加法;常量默认值只改元数据)
ALTER TABLE ops.feed_log
  ADD COLUMN IF NOT EXISTS basis text,
  ADD COLUMN IF NOT EXISTS basis_at timestamptz,
  ADD COLUMN IF NOT EXISTS evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS attempt_id bigint,
  ADD COLUMN IF NOT EXISTS item_count integer,
  ADD COLUMN IF NOT EXISTS skus text[],
  ADD COLUMN IF NOT EXISTS refs text[],
  ADD COLUMN IF NOT EXISTS posted_at timestamptz,
  ADD COLUMN IF NOT EXISTS reopenable boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS last_poll_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_poll_error text,
  ADD COLUMN IF NOT EXISTS poll_error_class text,
  ADD COLUMN IF NOT EXISTS first_404_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_404_at timestamptz,
  ADD COLUMN IF NOT EXISTS hold_reason text;
CREATE INDEX IF NOT EXISTS feed_log_open_idx ON ops.feed_log (status, updated_at)
    WHERE status IN ('pending', 'submitted', 'lapsed');
CREATE INDEX IF NOT EXISTS feed_log_pending_skus_gin ON ops.feed_log USING gin (skus)
    WHERE status = 'pending';
-- <<< ledger-v2
```

- 为什么:feed_log 只作当前指针,feed_log_dedupe_uidx 原样保留,不换索引、不写 DROP。新增列的用途: - basis、evidence、basis_at:终态依据(C15)。 - item_count、skus、refs:对账器按条数匹配;三方计数核对(C12);首读 ⊆ 校验(C14);open_skus 可以看到 pending 里的 SKU(C16)。 - attempt_id:指向当前尝试。 - posted_at:期限起算点。 - reopenable:由 services 判定、api 照办(C16)。 - 轮询失败分类与 404 时刻(C01、C17)。
- 存量回填:不写进 schema.sql。由 workflows/feed_ledger_backfill 的 step=legacy_basis 执行:幂等,先 --dry-run 报条数,每批 1 万行。 - basis 为 NULL 且 status ∈ (submitted, done, failed) 的行:basis='legacy',basis_at=updated_at。 - submitted 行:posted_at=updated_at,evidence.posted_at_src='updated_at'(_log_update 落 submitted 的时刻就是这个 feedId 的提交时刻)。 - done/failed 行:reopenable=true,与现状一致。 - item_count、skus 从 ops.feed_items 按 feed_id 聚合回填,并标 evidence.item_count_basis='ledger_rows'。这是台账行数,不等于提交条数,所以要标明。 - pending 行:basis='legacy',skus 留 NULL,交人工 -p resolve。

### SC2

```sql
-- >>> ledger-v2
-- 【SC2 · B0】每次尝试一行:事实列只写一次;log_final_* 与 last_reread_at 可变(旧值进 evidence.history);行不删
CREATE TABLE IF NOT EXISTS ops.feed_attempts (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    log_id           bigint,          -- ops.feed_log.id;单品 PUT 为 NULL
    parent_id        bigint,          -- probe/repost 行指向所属 post 行
    kind             text NOT NULL,   -- post / repost / probe / put
    workflow         text NOT NULL,   -- = cli 工作流名(七个 submit_feed 调用方与 PUT 调用方,守门钉住)
    store            text NOT NULL,
    feed_type        text NOT NULL,   -- 八个 feedType 或 PUT_PRICE / PUT_INVENTORY / PUT_INVENTORIES
    payload_key      text,
    item_count       integer,
    skus             text[],
    refs             text[],          -- 与 skus 同序:disp:<id> / upc:<号> / mig:<id>
    claimed_at       timestamptz NOT NULL DEFAULT now(),
    post_started_at  timestamptz,     -- rate_acquire 与 token 之后、发送之前单独提交;PUT 为调用 api 前
    finished_at      timestamptz,
    http_status      integer,
    net_phase        text,            -- pre / connect / read / refresh_after_401
    exc              text,
    body_snip        text,
    outcome          text,            -- accepted/rejected/not_sent/uncertain/found/not_found/probe_failed/misadopted
    feed_id          text,
    evidence         jsonb NOT NULL DEFAULT '{}'::jsonb,
    log_final_status text,
    log_final_basis  text,
    log_closed_at    timestamptz,
    last_reread_at   timestamptz
);
CREATE INDEX IF NOT EXISTS feed_attempts_log_idx   ON ops.feed_attempts (log_id, id DESC);
CREATE INDEX IF NOT EXISTS feed_attempts_store_idx ON ops.feed_attempts (store, feed_type, claimed_at DESC);
CREATE INDEX IF NOT EXISTS feed_attempts_feed_idx  ON ops.feed_attempts (feed_id) WHERE feed_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS feed_attempts_skus_gin  ON ops.feed_attempts USING gin (skus);
CREATE INDEX IF NOT EXISTS feed_attempts_refs_gin  ON ops.feed_attempts USING gin (refs);
CREATE INDEX IF NOT EXISTS feed_attempts_open_idx  ON ops.feed_attempts (workflow, claimed_at)
    WHERE outcome IS NULL AND kind IN ('post', 'repost', 'put');
CREATE INDEX IF NOT EXISTS feed_attempts_reread_idx ON ops.feed_attempts (log_closed_at)
    WHERE log_final_status = 'lapsed';
-- <<< ledger-v2
```

- 为什么:覆盖 C15、C04、C05、C07、C10、C20、C21、C22、C24。保留一载荷一行的 feed_log,每次尝试的历史落在这张只追加的表里。用途: - post_started_at 让「从未发送」成为可以落库的本地事实; - 按 skus/refs 计次,并绑定「本次尝试」; - 反查占用集改为 feed_log ∪ feed_attempts; - PUT 路由有逐 SKU、先写后调的尝试记录; - lapsed feed 的低频补读按这张表调度; - 孤儿 UPC 与 never_claimed 的判定,以 min(claimed_at) 作为 B1 切换时刻。 log_id 可以为空,也不设外键。
- 存量回填:不回填,历史尝试已无从得知;并由守门测试禁止往这张表回填历史行,否则会挪动 min(claimed_at) 切换锚点。attempts() 计次时,B1 之前的时段仍按旧口径数 ops.feed_items 里的 MP_ITEM 行。

### SC3

```sql
-- >>> ledger-v2
-- 【SC3 · B0】ops.feed_items 回执维度补列 + 观测维度 + 投影时刻
ALTER TABLE ops.feed_items
  ADD COLUMN IF NOT EXISTS basis text,
  ADD COLUMN IF NOT EXISTS evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS wm_status text,
  ADD COLUMN IF NOT EXISTS last_read_at timestamptz,
  ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'local',
  ADD COLUMN IF NOT EXISTS ref text,
  ADD COLUMN IF NOT EXISTS attempt_id bigint,
  ADD COLUMN IF NOT EXISTS effect text,
  ADD COLUMN IF NOT EXISTS effect_basis text,
  ADD COLUMN IF NOT EXISTS effect_at timestamptz,
  ADD COLUMN IF NOT EXISTS effect_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS projected_at timestamptz;
CREATE INDEX IF NOT EXISTS feed_items_ref_idx ON ops.feed_items (ref) WHERE ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS feed_items_open_idx ON ops.feed_items (feed_id) WHERE status = 'submitted';
CREATE INDEX IF NOT EXISTS feed_items_effect_open_idx ON ops.feed_items (store, feed_type, submitted_at)
    WHERE effect IS NULL AND feed_type IN ('DELETE_ITEM', 'RETIRE_ITEM', 'MP_ITEM', 'MP_ITEM_MATCH');
CREATE INDEX IF NOT EXISTS feed_items_unprojected_idx ON ops.feed_items (workflow, store, sku)
    WHERE projected_at IS NULL;
-- <<< ledger-v2
```

- 为什么:覆盖 C03、C12、C13、C08、C19、C23、C25、C26。 - 回执维度:basis、wm_status(原始 ingestionStatus)、last_read_at;resolved_at 从此只表示首次落定时刻。 - evidence.interim 放 ASYNC 等中途码,不进 ops.feed_item_errors(P0-2)。 - source 取值:local / detail / reconcile / sync / legacy。 - ref 把回执挂回调用方的业务键。 - effect 四列是按提交挂钩的观测维度。 - projected_at 由四个 sheet 模块在飞书写成功之后回写。「台账有、表没回写」据此成为本地事实,取代 product_clear 上一版的 96h 时间窗(P2-13)。
- 存量回填:feed_ledger_backfill 的 step=legacy_basis: 1. status='submitted' 且 resolved_at 非空的行:last_read_at=resolved_at,resolved_at=NULL。残留行上每轮被刷新的 resolved_at 实际就是最后一次读取时刻,这里只是搬回原位。 2. status ∈ (success, failed, missing) 且 basis 为 NULL:basis='legacy'。 3. feed_type='MP_INVENTORY' 且 sku LIKE '{%':basis='legacy_bad_key'。这是 2026-09-07 谭总12 那批,只标记,不改判。 4. 存量行的 projected_at 置为 resolved_at(存量已按旧反哺器投影过)。submitted 行保持 NULL。 5. 已被刷新过的 success 行,其 resolved_at 无法复原;停止刷新后,catalog_sync 再扫一轮即自愈。

### SC4

```sql
-- >>> ledger-v2
-- 【SC4 · B0】观测质量入账(上一版的 walmart_items.missing_source 删除)
CREATE TABLE IF NOT EXISTS catalog.scan_rounds (
    store           text NOT NULL,
    run_at          timestamptz NOT NULL,
    mode            text,
    fetched         integer,
    truncated       boolean NOT NULL,
    backstop        integer NOT NULL DEFAULT 0,
    backstop_filled integer NOT NULL DEFAULT 0,
    backstop_404    integer NOT NULL DEFAULT 0,
    backstop_empty  integer NOT NULL DEFAULT 0,
    inv_failed      boolean NOT NULL DEFAULT false,
    complete        boolean NOT NULL,          -- = NOT truncated
    PRIMARY KEY (store, run_at)
);
CREATE INDEX IF NOT EXISTS scan_rounds_complete_idx ON catalog.scan_rounds (store, run_at DESC) WHERE complete;
ALTER TABLE catalog.walmart_items
  ADD COLUMN IF NOT EXISTS avail_seen_at timestamptz;
-- <<< ledger-v2
```

- 为什么:覆盖 C08、C06、C18、C19,以及 P0-1。 - 被某轮列出 ⇔ w.last_seen_at ≥ 该轮 run_at(merge_rows 用本轮 run_at 作 last_seen_at)。所以「posted_at 之后最近一次 complete 轮是否列出」只靠 scan_rounds 加 last_seen_at 就能判定,不受 _MARK_MISSING_SQL 只标 missing_since IS NULL 行的影响(walmart_catalog.py:46-53)。 - 截断轮、store_release、legacy 缺席因此都不作数,也不会冻结。 - store_absence 的水位 SQL 会把截断轮也当作新鲜,所以不复用。 - avail_seen_at 修 COALESCE 沿用旧库存值、而 last_seen_at 照样刷新的问题(catalog_sync.py:103-109、walmart_catalog.py:34)。
- 存量回填:无。 - scan_rounds 不追溯;B4 上线后的第一轮扫描起才有 complete 轮。在此之前的核验一律走「两次 ⑥ 404 间隔 ≥24h」或正向路径。 - avail_seen_at 保持 NULL,等下一轮扫描写入;维护定案因此会多等一轮。

### SC5

```sql
-- >>> ledger-v2
-- 【SC5 · B0】下游表的依据列
ALTER TABLE listing.retire_cooldown
  ADD COLUMN IF NOT EXISTS basis text,
  ADD COLUMN IF NOT EXISTS evidence jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE catalog.upc_pool
  ADD COLUMN IF NOT EXISTS status_basis text,
  ADD COLUMN IF NOT EXISTS status_ref text,
  ADD COLUMN IF NOT EXISTS status_at timestamptz;
CREATE INDEX IF NOT EXISTS dispositions_exec_feed_idx ON ops.dispositions (feed_id, sku) WHERE status = 'executing';
-- <<< ledger-v2
```

- 为什么:覆盖 C11、C09、C21、C22、C19。 - 冷却行的落定要带依据;RETIRE 前的基线 ⑥ 记在 evidence.baseline(P1-10)。 - UPC 状态变化要能追到是哪次尝试。 - 处置账按 (feed_id, sku) 与台账对位,不再按 (店, SKU) 借用别处的判决。 - dispositions 与 retire_cooldown 新增的 lapsed 不需要 DDL:两个部分唯一索引只覆盖 suggested/executing 与 pending,新状态天然释放索引。 - sku_migrations 的依据写进既有的 detail 列(verdict_basis / stall_reason),不加列。
- 存量回填:feed_ledger_backfill 的 step=legacy_basis: - retire_cooldown 中 status 不是 pending 的行:basis='legacy'。其中 failed 行若对应的 feed_items 不是 failed,在 evidence 标 legacy_failed_without_sku_failure,并列出清单交所有者;不自动改写。 - upc_pool:status_basis 保持 NULL、status_at 保持 NULL(存量不补编;CHECK 只约束 status_at 非空的新写入)。 - dispositions:终态行缺 settled_by 的补 'legacy';withdrawn 行缺 withdrawn_reason 的补 'legacy'。 - sku_migrations:confirmed/rolled_back 缺 verdict_basis 的补 'legacy';stalled 缺 stall_reason 的补 'legacy'。

### SC6

```sql
-- >>> ledger-v2
-- 【SC6 · B1】feedId 唯一(防两个切片收编同一 feed)。条件建:存量有重复就只报不建,不拖垮 db_init
DO $$
BEGIN
  IF to_regclass('ops.feed_log_feed_id_uidx') IS NULL THEN
    IF EXISTS (SELECT 1 FROM ops.feed_log WHERE feed_id IS NOT NULL
               GROUP BY feed_id HAVING count(*) > 1) THEN
      RAISE NOTICE 'ops.feed_log 有共享 feed_id 的行(C14 误收编嫌疑),feed_log_feed_id_uidx 本次不建;先人工核对 SELECT feed_id, count(*) FROM ops.feed_log WHERE feed_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1';
    ELSE
      CREATE UNIQUE INDEX feed_log_feed_id_uidx ON ops.feed_log (feed_id) WHERE feed_id IS NOT NULL;
    END IF;
  END IF;
END $$;
-- <<< ledger-v2
```

- 为什么:覆盖 C14、C28①。list_new 并发结算两个同尺寸切片时,可能收编到同一个 feedId。db_init 一次执行整份 schema.sql,任何一步失败都会整份回滚(tests/test_sku_guard.py:684-698 的警告),所以用 DO 块按条件建。收编撞上这个索引时,adopt_found 整个事务回滚,本片落 pending(adopt_conflict)。
- 存量回填:无。索引建不成时,库里没有唯一保证;收编前仍先查占用集。摘要持续点名这一缺口,直到所有者处置完重复行,下次 db_init 自动补建。

### SC7

```sql
-- >>> ledger-v2
-- 【SC7 · B8】库层约束:IMMUTABLE 词表函数 + CHECK NOT VALID;扩词表 = CREATE OR REPLACE,只放宽不收窄
CREATE OR REPLACE FUNCTION ops.feed_log_state_ok(p_status text, p_basis text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $fn$
SELECT CASE p_status
  WHEN 'pending'   THEN coalesce(p_basis,'') IN ('claimed','in_flight','post_uncertain','repost_uncertain','deferred','window_wait','adopt_mismatch','adopt_conflict','legacy')
  WHEN 'submitted' THEN coalesce(p_basis,'') IN ('post_2xx','probe_found','probe_found_unverified','legacy','manual')
  WHEN 'done'      THEN coalesce(p_basis,'') IN ('feed_processed','items_complete','legacy','manual')
  WHEN 'failed'    THEN coalesce(p_basis,'') IN ('http_4xx','not_sent','probe_not_found','feed_error','legacy','manual')
  WHEN 'lapsed'    THEN coalesce(p_basis,'') IN ('deadline','http_404','store_disabled','store_unregistered','reconcile_exhausted','arrived_unlinked','manual')
  ELSE false END
$fn$;
CREATE OR REPLACE FUNCTION ops.feed_item_state_ok(p_status text, p_basis text, p_resolved timestamptz)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $fn$
SELECT CASE p_status
  WHEN 'submitted' THEN p_resolved IS NULL AND coalesce(p_basis,'') IN ('','wm_inprogress','wm_unknown_enum','async_review','detail_incomplete','detail_key_mismatch','head_stale','legacy_bad_key')
  WHEN 'success'   THEN p_resolved IS NOT NULL AND coalesce(p_basis,'') IN ('item_receipt','sync_http','legacy','manual')
  WHEN 'failed'    THEN p_resolved IS NOT NULL AND coalesce(p_basis,'') IN ('item_receipt','feed_error','legacy_feed_error','sync_http_4xx','sync_not_sent','legacy','manual')
  WHEN 'missing'   THEN p_resolved IS NOT NULL AND coalesce(p_basis,'') IN ('detail_absent','legacy','legacy_bad_key','manual')
  WHEN 'lapsed'    THEN p_resolved IS NOT NULL AND coalesce(p_basis,'') IN ('deadline','async_deadline','http_404','store_disabled','store_unregistered','adopt_mismatch','sync_uncertain','reconcile_exhausted','arrived_unlinked','legacy_unlinked','manual')
  ELSE false END
$fn$;
CREATE OR REPLACE FUNCTION ops.feed_effect_ok(p_effect text, p_basis text, p_at timestamptz)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $fn$
SELECT CASE
  WHEN p_effect IS NULL THEN p_basis IS NULL
  WHEN p_at IS NULL THEN false
  ELSE CASE p_effect
    WHEN 'effective'      THEN p_basis IN ('gone_probe404','gone_probe404x2','retired_scan','retired_probe','present_scan','present_probe','legacy_event','manual')
    WHEN 'not_effective'  THEN p_basis IN ('active_probe200','absent_probe404','absent_probe404x2','legacy_event','manual')
    WHEN 'retired'        THEN p_basis IN ('retired_probe','manual')
    WHEN 'not_applicable' THEN p_basis IN ('receipt_refused','not_received')
    WHEN 'unverifiable'   THEN p_basis IN ('store_disabled','store_unregistered','probe_inconclusive','legacy')
    ELSE false END
  END
$fn$;
CREATE OR REPLACE FUNCTION ops.ledger_word_ok(p_domain text, p_word text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $fn$
SELECT CASE p_domain
  WHEN 'attempt_kind'    THEN p_word IN ('post','repost','probe','put')
  WHEN 'attempt_outcome' THEN p_word IS NULL OR p_word IN ('accepted','rejected','not_sent','uncertain','found','not_found','probe_failed','misadopted')
  WHEN 'item_source'     THEN p_word IN ('local','detail','reconcile','sync','legacy')
  ELSE false END
$fn$;
CREATE OR REPLACE FUNCTION ops.disposition_final_ok(p_status text, p_detail jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $fn$
SELECT CASE
  WHEN p_status IN ('suggested','executing') THEN true
  WHEN p_status IN ('confirmed','ineffective','lapsed') THEN jsonb_exists(p_detail, 'settled_by')
  WHEN p_status = 'withdrawn' THEN jsonb_exists(p_detail, 'withdrawn_reason')
  ELSE false END
$fn$;
CREATE OR REPLACE FUNCTION ops.migration_final_ok(p_status text, p_detail jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $fn$
SELECT CASE
  WHEN p_status IN ('pending','double') THEN true
  WHEN p_status = 'stalled' THEN jsonb_exists(p_detail, 'stall_reason')
  WHEN p_status IN ('confirmed','rolled_back') THEN jsonb_exists(p_detail, 'verdict_basis')
  ELSE false END
$fn$;
CREATE OR REPLACE FUNCTION ops.cooldown_final_ok(p_status text, p_basis text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $fn$
SELECT CASE
  WHEN p_status = 'pending' THEN true
  WHEN p_status IN ('cleared','failed','lapsed') THEN p_basis IS NOT NULL
  ELSE false END
$fn$;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'feed_log_state_ck') THEN
    ALTER TABLE ops.feed_log ADD CONSTRAINT feed_log_state_ck CHECK (ops.feed_log_state_ok(status, basis)) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'feed_items_state_ck') THEN
    ALTER TABLE ops.feed_items ADD CONSTRAINT feed_items_state_ck CHECK (ops.feed_item_state_ok(status, basis, resolved_at)) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'feed_items_effect_ck') THEN
    ALTER TABLE ops.feed_items ADD CONSTRAINT feed_items_effect_ck CHECK (ops.feed_effect_ok(effect, effect_basis, effect_at)) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'feed_items_source_ck') THEN
    ALTER TABLE ops.feed_items ADD CONSTRAINT feed_items_source_ck CHECK (ops.ledger_word_ok('item_source', source)) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'feed_attempts_word_ck') THEN
    ALTER TABLE ops.feed_attempts ADD CONSTRAINT feed_attempts_word_ck CHECK (ops.ledger_word_ok('attempt_kind', kind) AND ops.ledger_word_ok('attempt_outcome', outcome)) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'dispositions_final_ck') THEN
    ALTER TABLE ops.dispositions ADD CONSTRAINT dispositions_final_ck CHECK (ops.disposition_final_ok(status, detail)) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sku_migrations_final_ck') THEN
    ALTER TABLE listing.sku_migrations ADD CONSTRAINT sku_migrations_final_ck CHECK (ops.migration_final_ok(status, detail)) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'retire_cooldown_final_ck') THEN
    ALTER TABLE listing.retire_cooldown ADD CONSTRAINT retire_cooldown_final_ck CHECK (ops.cooldown_final_ok(status, basis)) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'upc_pool_basis_ck') THEN
    ALTER TABLE catalog.upc_pool ADD CONSTRAINT upc_pool_basis_ck CHECK (status_at IS NULL OR status_basis IS NOT NULL) NOT VALID;
  END IF;
END $$;
-- <<< ledger-v2
```

- 为什么:兑现「终态必带依据」,覆盖六本账(P2-19)。 - feed_log 与 feed_items 校验 status 与 basis 的组合,顺带挡住「submitted 却带着 resolved_at」这种矛盾形态。 - 其余四本账校验终态必须带依据键:dispositions.detail.settled_by / withdrawn_reason、sku_migrations.detail.verdict_basis / stall_reason、retire_cooldown.basis、upc_pool.status_basis。 - 放在最后一批:NOT VALID 只跳过存量检查,新写入照样受约束,所以必须等所有写者都已按词表落账。 - 守门测试逐字比对函数体里的词与 registry 常量;断言本设计的新增段不含 DROP 和 DML,全文件不含 DROP INDEX。
- 存量回填:VALIDATE 不写进 schema.sql,由 feed_ledger_backfill 的 step=validate 执行,顺序如下: 1. 停调度。 2. 重跑 step=legacy_basis,把两批之间旧代码写下、缺依据的终态行标成 legacy。 3. 逐表 SELECT 词表外或缺依据键的行;有任何一条就中止并列出,不 VALIDATE。 4. 执行 db_init。 5. 发布 B8 代码。 6. 逐表 ALTER TABLE … VALIDATE CONSTRAINT。 说明:函数被放宽之后,VALIDATE 不会重验存量行,所以只允许扩词表、不允许收窄;这一点写进 docs/db_schema.md。

### SC8

```sql
-- 【SC8 · B2/B4/B5,一次性回填,不进 schema.sql】workflows/feed_ledger_backfill 的其余 step
-- (均幂等、先 --dry-run 报条数;不带 -p step 即 ⛔ 硬拒)
-- step=feed_error_missing(B2):零明细整 feed 拒收的历史 missing → 有界推断 legacy_feed_error
UPDATE ops.feed_items f
   SET status = 'failed', basis = 'legacy_feed_error',
       evidence = f.evidence || jsonb_build_object('derived', 'feed_ledger_backfill',
         'rule', 'feed_log.failed 且 feed_id 非空(只有 mark_feed_done(ok=False) 会留 feed_id,即 feedStatus=ERROR);该 feed 台账无一行 success/failed;ops.feed_item_errors 无该 feed 任何行(零明细签名);非 legacy_bad_key')
  FROM ops.feed_log l
 WHERE l.feed_id = f.feed_id AND l.status = 'failed' AND f.status = 'missing'
   AND coalesce(f.basis, '') <> 'legacy_bad_key'
   AND NOT EXISTS (SELECT 1 FROM ops.feed_items g
                   WHERE g.feed_id = f.feed_id AND g.status IN ('success', 'failed'))
   AND NOT EXISTS (SELECT 1 FROM ops.feed_item_errors e WHERE e.feed_id = f.feed_id);
-- step=legacy_effect(B4):旧 verify_deletions 的判决按时间有界对位
-- 核验事件不带 feed_id(product_events.py:340-347),只能按「本次 success 之后、下一次 success 之前的第一条核验」配对
WITH s AS (
  SELECT e.store, e.sku, e.occurred_at, e.detail->>'feed_id' AS feed_id,
         lead(e.occurred_at) OVER (PARTITION BY e.store, e.sku ORDER BY e.occurred_at) AS next_at
    FROM catalog.product_events e
   WHERE e.event = 'delete_feed_success'),
ok AS (
  SELECT s.store, s.sku, s.feed_id,
         (SELECT v.id FROM catalog.product_events v
           WHERE v.store = s.store AND v.sku = s.sku
             AND v.event IN ('delete_verified', 'delete_not_effective')
             AND v.occurred_at >= s.occurred_at
             AND (s.next_at IS NULL OR v.occurred_at < s.next_at)
           ORDER BY v.occurred_at LIMIT 1) AS vid
    FROM s WHERE s.feed_id IS NOT NULL)
UPDATE ops.feed_items f
   SET effect = CASE v.event WHEN 'delete_verified' THEN 'effective' ELSE 'not_effective' END,
       effect_basis = 'legacy_event', effect_at = v.occurred_at,
       effect_evidence = jsonb_build_object('event_id', v.id,
         'rule', '旧 verify_deletions:缺席/RETIRED/无行皆 gone,宽限 48h;按时间有界配对,质量未知,不据此再次弃码')
  FROM ok JOIN catalog.product_events v ON v.id = ok.vid
 WHERE f.feed_id = ok.feed_id AND f.sku = ok.sku AND f.feed_type = 'DELETE_ITEM' AND f.effect IS NULL;
-- 其余:B4 之前提交、且早于 14 天的 DELETE/RETIRE/MP_ITEM/MATCH 行 effect IS NULL ⇒ unverifiable/legacy;近 14 天的交给 verify_effects
-- step=legacy_executing(B5):台账对不上的存量破坏类 executing 处置,补一条挂账行,走同一套 effect 判据
WITH d AS (
  SELECT d.id, d.store, d.sku, d.action, d.feed_id AS orig_feed_id, d.executed_at
    FROM ops.dispositions d
   WHERE d.status = 'executing' AND d.action IN ('delete', 'retire')
     AND NOT EXISTS (SELECT 1 FROM ops.feed_items f WHERE f.feed_id = d.feed_id AND f.sku = d.sku))
INSERT INTO ops.feed_items (feed_id, sku, workflow, store, feed_type, status, submitted_at, resolved_at,
                            basis, source, ref, evidence)
SELECT 'legacy:' || d.id, d.sku, 'problem_product_cleanup', d.store,
       CASE d.action WHEN 'delete' THEN 'DELETE_ITEM' ELSE 'RETIRE_ITEM' END,
       'lapsed', coalesce(d.executed_at, now()), now(), 'legacy_unlinked', 'legacy', 'disp:' || d.id,
       jsonb_build_object('orig_feed_id', d.orig_feed_id, 'rule', '存量 executing 无台账行,补挂账交观测')
  FROM d
ON CONFLICT (feed_id, sku) DO NOTHING;
-- 随后同一 step:UPDATE ops.dispositions SET feed_id = 'legacy:' || id, detail = detail || {orig_feed_id} 对上述行
-- step=relabel_expired(B5,需 D7 批准):ops.dispositions ineffective 且 settled_by='expired' ⇒ lapsed,
--        detail 追加 {relabeled_from:'ineffective', relabeled_at}
```

- 为什么:这些都是业务行回填,不能每次 db_init 重跑,改由一次性工作流执行、先报数(P1-7)。 - feed_error_missing:推断链经读码核实。_log_update 用 COALESCE 保留 feed_id,_log_claim 重占会置 NULL,所以「failed 且 feed_id 非空」只可能来自 mark_feed_done(ok=False)(feed_track.py:471),即 feedStatus=ERROR。2026-09-07 之后,零明细 ERROR 会把 feed 级 errs 写进每个 SKU(feed_track.py:360-367),所以「feed_item_errors 无该 feed 行」可以作为 2026-09-07 之前零明细的签名。仍有残余不确定性,所以落 legacy_feed_error,不冒充 feed_error。 - legacy_effect:加了下一次 delete_feed_success 作上界,避免把后一次删除的核验记到前一个 feed 上;effect_basis 标 legacy_event,弃码点 1 不认这个 basis。 - legacy_executing:让存量 executing 走同一个 judge_effect,不另写第二套核验逻辑(C09/C19 存量缺口)。
- 存量回填:见 sql 各 step。每个 step 都先 --dry-run 报条数给所有者过目,再分批执行(每批 1 万行,低峰时段)。上线前连库确认 delete_feed_success.detail 带 feed_id 的覆盖率;不带的行不回填,之后归入 unverifiable/legacy。

## 六、每簇的落点

- **C01** → 正常情况下,C02 的期限路径会在 404 之前先收口。404 路径只兜「长期读不到」的存量: - feed_log 落 lapsed(http_404); - 该 feed 仍为 submitted 的 feed_items 落 lapsed(http_404); - SKU 的结局交给 effect。 其它 GET 失败(401/403/429/5xx、代理、网络、明细翻页中途失败)都不算终态:feed_log 仍为 submitted,每轮写 last_poll_at、last_poll_error、poll_error_class(diagnose 六档),摘要按档折叠点名。
  - 事实:③ GET /v3/feeds/{id} 返回 404。 - api 层抛 feeds.FeedNotFound(FeedQueryError 的子类),报文统一为「返回 404」,diagnose 因此能归到「沃尔玛404」。 - 记录 first_404_at 与 last_404_at。 - 「约 30 天清除」是经验值,只作门槛 FEED_PURGE_MIN_AGE_DAYS=20:A085 第 29 天仍可查,A109 第 32 天、A162 第 38 天 404。evidence 写明这一出处,不据此下任何 SKU 结论。
  - 触发:poll_all 每轮执行。收口条件:posted_at 满 20 天,且第二次 404 距首次 ≥6h。 - 不满 20 天就 404:只写 hold_reason='status_404_young' 并告警(可能是凭证串店,或 feedId 转义错),不收口。 - 明细翻页中途失败:已读到的页照样按写一次落账,不做 missing 判定。
  - 写者:- api/feeds.get_feed_status / iter_feed_items:非 200 时抛 FeedQueryError(status),报文为「返回 NNN」,属接口适配。 - services/feed_track.poll_all → close_feed:唯一的 submitted 出口。 - product_clear._poll_feeds 与 sku_locked_heal 的就地 poll_feed 在 B5 删除。
  - 下游:- 载荷锁按 reopenable 释放;problem_scan 的在途闸不再计入这些行。 - dispositions、冷却表、sku_migrate 改由 effect 定案。 - 四张表显示「超期无回执(404)」。 - sku_migrate 闸⑤ 不再因此永久关闭。
  - 风险:- 404 也可能另有成因,靠 20 天门槛加两次确认压住;收口不下任何 SKU 结局。 - 存量 A109、A162 会在 B3 首轮收口,须先 dry-run 列出清单。
- **C02** → - 期限过后仍非终态的 feed_items 落 lapsed(deadline)。适用情形:INPROGRESS、未知枚举、detail_incomplete、detail_key_mismatch、汇总停更且明细无结论。 - 带 ASYNC 码的行落 lapsed(async_deadline),期限 120h。 - feed_log 落 lapsed(deadline)。 - 未知枚举在期限到之前记 basis=wm_unknown_enum,原值写进 wm_status,每个 feed 告警一次(提示补 api/feeds._SKU_* 码表)。
  - 事实:- ① 期限点之后有一次成功的明细读取,该 SKU 仍非终态:记 wm_status 原值与 last_read_at。 - ⑦ FEED_DEADLINE_HOURS 逐项取自 refdata/walmart_slas.tsv「我方判死期限(建议)」列: - price 8h(批量改价行); - inventory / MP_INVENTORY 24h(库存更新行); - MP_ITEM / MP_MAINTENANCE 72h(批量建品改品行); - MP_ITEM_MATCH 72h(按匹配上架行); - RETIRE_ITEM 72h(停用退役行); - DELETE_ITEM 96h(删除行)。 - evidence.deadline 记 {hours, src, posted_at, last_read_at, last_head}。
  - 触发:poll_feed 内判定,与落账同一事务。条件:now > posted_at + 期限,且本轮汇总与明细都读成功。店读不到时不收口。
  - 写者:- services/feed_track 持有 FEED_DEADLINE_HOURS 与 ASYNC_DEADLINE_HOURS 两个常量(唯一出处,守门测试逐行对照 tsv)。 - close_feed 负责收口。
  - 下游:- 同 C01。 - MP_ITEM/MATCH 的 reopenable 要等所有 SKU 都拿到 effect 才置位。 - 低频补读:每 4h 一次,最长 35 天;读到回执就升级,原判留在 history,并补写 feed_item_errors。 - 上架表的「上架结果」由空或「处理中」变为「超期无回执」,再按 effect 决定写 Yes 还是重排。
  - 风险:期限是从生效窗口反推的,官方没给 feed 级最长处理时间,所以 lapsed 只关闭回执维度,不下失败结论。2026-09-11 试算:6 条 feed(75 SKU)会判死,14 条(6,087 SKU)继续等;首轮 dry-run 时核对这组数。
- **C03** → ops.feed_items 的首次终态不可改:status、basis、error_*、resolved_at 只在 status='submitted' 时写一次。 - ops.feed_item_errors 只收落定那一刻的全码集。 - 之后重读得到不同结论,只追加 evidence.rereads[{at, wm_status, codes}] 并告警。 - 唯一例外:lapsed 是推断,可以被后到的回执升级,此时才写码。
  - 事实:① 首次读到的逐条回执及读取时刻。之后的读取只刷新 last_read_at 与 wm_status;非终态期间读到的码写进 evidence.interim。
  - 触发:每次 poll_feed。汇总终态与未终态两个分支用同一口径(PR #131 只修了未终态那一支)。
  - 写者:services/feed_track.poll_feed: - 改为 UPDATE ops.feed_items f … FROM unnest(...) WHERE f.feed_id=%s AND f.status='submitted' RETURNING f.sku; - _save_errors、回执事件、违禁黑名单三者只对 RETURNING 返回的 SKU 执行,修掉 feed_track.py:388 对明细全量落码的问题; - 残留行只写 wm_status、last_read_at、basis、evidence.interim。
  - 下游:- problem_scan 改经 open_skus(cap_hours=48, success_until_seen=True) 判在途,口径与原 SQL 逐字相同,回到定稿语义。 - A171 的 942 行、A109 的 451 行、L001 的 439 行、A162 的 298 行,在下一次 catalog_sync 后自然出在途,不再被 withdraw_stale 错撤。 - 已按旧值做过的处置不再被静默改写。 - 并发轮询时,只有一个事务能迁移同一行(C28②)。
  - 风险:沃尔玛若事后改判,结论只进 rereads,需人看告警。存量 feed_item_errors 里已写入的中途码(如 ASYNC)无法区分,按 legacy 对待。
- **C04** → 结局不确定的一律留 ops.feed_log=pending(basis 为 post_uncertain 或 repost_uncertain),不再落 failed。每次 POST 与每次反查各写一行 feed_attempts。
  - 事实:- ③ 每次 POST 的 HTTP 结局与 net_phase:4xx 表示被拒;pre 与 connect 表示未发出;5xx、read、2xx 无 feedId、data 不是 dict,都表示不确定。 - ④ 补交之后再反查一次,带覆盖证明。 - 内联路径不做 ⑥。窗口内的 ⑥ 404 不是事实(P0-3),而且内联单查属于 api 层的业务分支。
  - 触发:- _submit_one:补交结果不是「2xx 带 feedId」,也不是 4xx 或 not_sent 时,立即再调一次 find_recent_feed。FOUND 就收编;补交之后的 NOT_FOUND(带覆盖证明)落 failed(probe_not_found);其余保持 pending。 - settle_deferred 末轮同样处理。 - 2xx 无 feedId 走反查,不再按 status<500 落 failed。
  - 写者:- api/feeds._submit_one、settle_deferred、adopt_found、close_pending。 - api/_client._request_ex 新增 net 出参(phase / exc / saw_401),safe_post_ex 与 safe_get_ex 透传,属接口适配。
  - 下游:- list_new 只在 basis ∈ {http_4xx, not_sent} 时回收 UPC,原因分别写 rejected 和 not_sent。probe_not_found 与 pending 一律不回收,同 ASIN 重试时复用原号,不再违反「Unknown 永不回收」。 - sku_migrate 只在 http_4xx 或 not_sent 时当场回滚;probe_not_found 要等窗口后 ⑥。 - match、cleanup、maintenance 表写「待对账 log:<id>」。 - 其余交给同批上线的对账器(C10)。
  - 风险:pending 会增多,所以与对账器同批(B1)上线。每次补交结果不确定,都要多一次 30s 反查。覆盖证明依赖列表接口的能力(open_risks 1);证明不了就判 UNKNOWN,绝不判 NOT_FOUND。
- **C05** → - 确定没发出的,落 ops.feed_log=failed(not_sent),evidence 记 {exc, phase: pre|connect|never_posted, diagnose 词}。 - 收到 401 后换 token 撞代理:落 failed(http_4xx),status=401,net_phase=refresh_after_401,evidence.refresh_exc 记换 token 时的异常。结论同样是「未受理」,但依据是「已发出、被拒」。
  - 事实:③ 本地传输事实,能证明请求没被受理: - pre 阶段:get_token 抛 StoreDeadError、token 阶段 StoreProxyError 或其它异常、rate_acquire 抛错,都发生在 POST 之前。 - connect 阶段:httpx.ConnectError / ConnectTimeout / PoolTimeout / ProxyError、socksio SOCKSError。已在本机 httpcore 1.0.9 核实:ProxyError 只在 socks_proxy.py:68/85/102 的握手与 http_proxy.py:297 的 CONNECT 阶段抛出。 - refresh_after_401:_client 在 net 里先记 saw_401=True,再原样上抛。 - never_posted:post_started_at 从未写入,且原进程已不在。
  - 触发:- 提交当场判定。 - 对账器补 never_posted:pending 行的 post_started_at 为空,且借得到该行 workflow 的锁(services/runlock.hold,不阻塞,在持锁期间判定),且行的 claimed_at 早于借锁时刻。不再用 3h 时钟。
  - 写者:- api/_client._request_ex:按异常类区分 connect 与 read;401 刷新失败时在 net 里记 saw_401。只看异常类,不做业务判断。 - api/feeds._post 分三种情况(保持 conventions §四 行为不变,P2-16): 1. token 阶段非 StoreDeadError 的异常(含 StoreProxyError),照旧走 _PRE_FAIL、不上抛,只是 basis 记为 not_sent; 2. StoreDeadError 照旧上抛,上抛前先落 failed(not_sent); 3. 401 刷新撞代理照旧上抛,上抛前先落 failed(http_4xx)。 上抛的异常对象上挂 feed_results(已完成片的结果),类型不变,store_retry 的分类不变。 - rate_acquire 挪进同一个 try,落 not_sent 后原样上抛;若 PG 不可达、写不进,就留给 never_posted 处理。 - connect 阶段返回 None 时,不再反查,直接落 not_sent。 - services/feed_track.reconcile_pending 处理 never_posted(经 api/feeds.close_pending 写)。
  - 下游:- 凭证修好后,同一载荷不再被永久 dedup。 - cleanup/maintenance 写「未发出(凭证失效 / 代理无效 / 代理波动)」,不再写「在途防重」。 - list_new 用新原因 not_sent 回收号。 - sku_migrate 可以当场回滚(确定未达)。
  - 风险:- WriteError、WriteTimeout、ReadTimeout、RemoteProtocolError、DecodingError 及其它未列出的异常,一律按不确定处理。 - 依赖升级时,connect 白名单要重新核对,已用测试钉住。
- **C06** → listing.sku_migrations 的 rolled_back 与 confirmed 只凭事实判定,依据写进 detail.verdict_basis。 - 新码回执为 success、review 或仍在途时,72h 内不回滚。 - 回执为 success 或 review,但 72h 后新码缺席且 ⑥ 404:落 stalled(receipt_success_but_absent),交人工。
  - 事实:- ① 新码回执,经 receipt_class 解释:refused、feed_error、legacy_feed_error、not_received(detail_absent)才算回滚依据。 - ③ 提交层 http_4xx、not_sent。 - ⑤ scan_rounds.complete 轮,且 run_at 晚于 submitted_at + 72h(⑦ MATCH 24h/72h,见 walmart_slas.tsv「按匹配上架」行)。 - ⑥ 单查新码与旧码。遇 200 空体时 api 抛 EmptyItemResponse,调用方 fail-closed。 - 长期截断的店:两次 ⑥ 404,间隔 ≥24h。
  - 触发:每次 sku_migrate._settle,包括 settle_only 模式。
  - 写者:- workflows/sku_migrate._verdict / _settle / _probe_shadows / _candidates。 - 观测与单查一律经 services/feed_track.observe / probe_item / judge_effect,sku_migrate 不再自写旧码缺席判据与影子单查,消除双轨。 - api/items 单品查询函数遇 200 空体抛 EmptyItemResponse。
  - 下游:- OBSERVE_HOURS 改为 72,取 feed_track.EFFECT_WINDOW_HOURS['MP_ITEM_MATCH']。 - fresh 判据改为「存在 complete 轮,且 run_at > submitted_at + 72h」。 - (a) 判据改为:旧码在 submitted_at 之后最近一次 complete 轮未被列出(last_seen_at < run_at),再加 ⑥ 旧码真 404。 - 候选闸:同一 (店, 旧码) 有过「可能已生效」的改码(新码回执 success,或 effect 不是 not_effective),不再自动成为候选,守住 2026-09-07 定稿。 - probe_not_found 提交的行,不当场回滚。
  - 风险:- 定案变慢,最长 72h 以上;stalled 仍计入节奏闸 open。 - ⑥ 单查占用 items.get 限流桶(800/min,共享)。 - 需所有者拍板 D8。
- **C07** → - stalled 改为非终态,每轮都参与定案,可转为 confirmed、rolled_back 或 double。 - 落库但未提交的行,按台账与 feed_attempts 判为以下之一:已提交、等对账、probe_not_found 等窗口、确定未发(rolled_back,依据 never_claimed 或 submit_failed)。 - 真双挂:单查旧码为真 404 ⇒ confirmed(old_gone_404);单查为 200(含 RETIRED)⇒ 保持 double,交人工。 - 其余交人工的行,经 sku_migrate -p resolve 收口。
  - 事实:- ③ ops.feed_items 里 workflow=sku_migrate、sku=new_sku 的行。新码全表唯一。 - ③ feed_attempts.skus 是否包含新码,以及该尝试最终的 feed_log 状态。 - 本地锁事实:锁文件 pid 等于本进程,说明本进程持有 sku_migrate 锁;行的 created_at 早于本进程启动,说明行出自已结束的旧进程。 - ⑥ 单查旧码。
  - 触发:每轮 _settle 开头先回填。_SQL_OBSERVE 的取数范围改为 pending ∪ double ∪ stalled。
  - 写者:workflows/sku_migrate: - _backfill_unsent 取代只告警的 _SQL_UNSENT; - _verdict 新增 never_claimed 与 old_gone_404; - _shadow_candidates 放宽为全部 double 行; - 新增 -p resolve=<mig_id> -p as=confirmed|rolled_back|stalled -p note=…,经 _confirm / _roll_back 执行,弃码仍在弃码点 4,守门 _ABANDON_CALLERS_OK 不变;DANGEROUS=True,可以 --dry-run。 services/runlock 新增 held_by_me(name):读锁文件 pid,与 os.getpid() 比较。
  - 下游:- 节奏闸不再被 stalled 或未提交行永久压成 0。 - A131 的 B09L3WXJ96(旧码是 RETIRED 死档):按 D8 默认仍为 double 等人工;所有者选 D8-B2 则自动 confirmed。 - B08DR3TKQK(两码都 PUBLISHED)仍为 double。 - 人工收口不再需要裸改登记簿。
  - 风险:never_claimed 只对 B1 之后创建的行成立(以 min(feed_attempts.claimed_at) 判定);更早的行点名,交 -p resolve 处理。
- **C08** → DELETE 行的 effect=effective,以及随后的 delete_verified 事件与弃码点 1 的弃码,只在 ⑥ 真 404 时发生,分两种依据: - gone_probe404:posted_at 之后最近一次 complete 轮没有列出它(walmart_items 无行,或 last_seen_at < 该轮 run_at),一次 404 即可; - gone_probe404x2:仍被 complete 轮列出(僵尸),或提交后还没有 complete 轮,需要两次 404,间隔 ≥24h。 以下都不算删除:RETIRED(另记 effect=retired,见 D5)、只有截断轮的缺席、store_release 缺席、从未单查过的行。
  - 事实:⑤ scan_rounds.complete 加 w.last_seen_at;⑥ GET /v3/items/{sku} 真 404(200 空体不算);⑦ DELETE 最长 72h,见 tsv「删除」行官方值列。
  - 触发:feed_poll 每轮调用 verify_effects:以 ⑤ 作触发条件,⑥ 受每店单查预算 EFFECT_PROBE_PER_STORE 约束。catalog_sync 每轮读取 effective 结果并弃码。
  - 写者:- services/feed_track.verify_effects / judge_effect:取代 product_events.verify_deletions,成为唯一判据。 - services/walmart_catalog.record_scan_round。 - workflows/catalog_sync:按 effect 查「DELETE、effective、effect_basis 不是 legacy_event、登记簿仍是活码」,再调 sku_codec.abandon。弃码点 1 位置不变;--dry-run 时跳过。弃码由状态推导,可幂等重复执行。
  - 下游:- 弃码与烧号只建立在删除事实上。 - store_release 整店下线、截断轮都不再触发误弃码,也不会因首轮缺席来源冻结而永远判不了(P0-1)。 - RETIRED 行不再每天循环一次「删除已核验」。
  - 风险:- 收紧之后弃码会变少。 - 单查量会上升(open_risks 9)。 - 需所有者拍板 D5。 - 从未被观测过、walmart_items 无行的 SKU,一次 404 即判 effective;这种情况下删除目标「不在目录」已经成立,但对应的 UPC 可能从未绑定过就被烧号,只是浪费,不会撞库。
- **C09** → 破坏类 ops.dispositions 的 executing 行,按本行 (feed_id, sku) 连到台账后,按以下优先级落定: 1. effect=effective ⇒ confirmed(effect:<basis>); 2. effect=retired ⇒ ineffective(effect:retired); 3. effect=not_effective ⇒ ineffective(effect_not_effective); 4. 回执带 ITEM_GONE 码 ⇒ confirmed(receipt_gone); 5. effect=not_applicable(receipt_refused)⇒ ineffective(receipt_refused); 6. not_applicable(not_received)⇒ ineffective(not_received); 7. effect=unverifiable ⇒ lapsed(unverifiable:<basis>); 8. feed_log 已终态,但台账缺本行 ⇒ lapsed(ledger_row_missing)。 - 存量 relist 行缺席:落 lapsed(relist_absent)。 - 存量 executing:B5 回填 'legacy:<id>' 挂账行后,走同一套判据。 - 店在营但当前不可达:保持 executing,detail.hold 写明原因。 - 人工可经 problem_product_cleanup -p resolve 收口。
  - 事实:- ① 回执:按 (feed_id, sku) 对位。 - ⑤⑥ effect:retire 行读 RETIRE 自己的 effect,ERR_PDI_0004 也照常观测(P0-6)。 - 本地事实:registered_names 与 enabled_names。
  - 触发:problem_product_cleanup 开头调用 dispositions.settle()。上游收口由 C01/C02(期限:DELETE 96h、RETIRE 72h)与 verify_effects 提供。
  - 写者:- services/dispositions.settle:改为读台账的单条 SQL,按主键 JOIN,不用 LATERAL;删除按「店 + SKU」借判的 _SETTLE_DELETE_SQL。 - services/dispositions.resolve_manual:人工收口的唯一实现;problem_product_cleanup 与 maintenance 都通过 -p resolve=<disp_id> -p as=… -p note=… 调用它。 - feed_ledger_backfill step=legacy_executing。
  - 下游:- 部分唯一索引释放,同一 SKU 可以重新建议。 - sku_migrate 的「无未了结破坏建议」不再把这些行永久剔除。 - stuck_executing 只剩「店在营但不可达」一种,并写明原因。
  - 风险:在营但长期不可达的店,effect 会一直是 NULL(D4 的代价),会点名原因,也可以人工 resolve。
- **C10** → pending 的出口: - submitted:probe_found 或 probe_found_unverified; - failed:probe_not_found,或 not_sent(never_posted); - lapsed:reconcile_exhausted 或 arrived_unlinked。lapsed 时,由 attempt.skus/refs 插入 feed_items 行,feed_id='unlinked:<attempt_id>',交观测。 MP_ITEM/MATCH 在 EFFECT_WINDOW 内只收编 FOUND,其余保持 pending(window_wait)。 B1 之前的存量 pending 没有 skus,保持 pending,经 feed_poll -p resolve 以 basis=manual 收口。
  - 事实:④ GET /v3/feeds?feedType: - 时间窗:以本次尝试的 post_started_at 为锚,[锚−5min, 锚+60min]; - 匹配条件:itemsReceived == item_count,且 feedId 不在占用集(feed_log ∪ feed_attempts)内; - feedDate 解析不出的条目,既不作候选,也不能用来证明覆盖; - 覆盖证明:totalResults 不大于已取回条数,或已按 offset 翻页到早于窗口起点(翻页能力须先核实); - 候选命中后读首页明细,做 ⊆ 校验。 MP_ITEM/MATCH 窗口之后:④ 未达 + ⑥ 本片全部真 404 ⇒ failed(probe_not_found);任一 SKU 返回 200 ⇒ lapsed(arrived_unlinked);⑥ 不确定 ⇒ 继续等。 其它类型:过 FEED_DEADLINE,且期限后至少有一次成功的列表读取,仍 UNKNOWN ⇒ lapsed(reconcile_exhausted)。
  - 触发:feed_poll 每轮先跑 reconcile_pending,只处理满足以下条件的 pending: - claimed_at 已超过 2h(列表有延迟);never_posted 不受这条限制; - 借得到该行 workflow 的锁(runlock.hold,不阻塞),并在持锁期间完成判定。锁空闲是本地事实,说明创建该行的进程已不在。
  - 写者:- services/feed_track.reconcile_pending:只读沃尔玛,永不 POST;⑥ 单查在 services 这一层做。 - 落账经 api/feeds 的公开函数:adopt_found(收编)、close_pending(failed / lapsed / window_wait)。 - api/feeds.find_recent_feed 新增 anchor 与 skus 参数,并附覆盖证明;仍是同一个端点、同一个函数。 - 人工收口:feed_poll -p resolve=<log_id> -p as=submitted|failed|lapsed [-p set_feed_id=…] -p note=…,basis=manual,evidence 记 WALMART_OPERATOR 与说明。-p feed_id 已被诊断模式占用,所以不复用它。
  - 下游:- 各调用方用同一个 adopt 函数,从台账回填自己的账。 - 非 MP_ITEM/MATCH 的 failed,由原业务工作流下一轮按原方法重发。 - MP_ITEM/MATCH 重发前,还要先过 open_skus 与观测。 - 兑现五处文案里「交启动对账」的承诺。
  - 风险:- 推翻 2026-08-16 定稿(D1)。 - D1 被否决时的降级:保留「不确定一律 pending、不落 failed」,C05 的 not_sent 照常落,-p resolve 工具照常上线,只是不自动对账;C07 unsent、C22 Unknown 改为人工 resolve。绝不退回 C04 的现状。 - MP_ITEM/MATCH 的 pending 最长会占载荷锁和 UPC 约 72h。
- **C11** → 回执的解释只有一个出处:services/feed_track.receipt_class(feed_type, status, basis, codes) → (cls, flags)。 cls 取值: - ok - gone:破坏类回执带 ITEM_GONE 码,不论 status - refused_permanent:该 feedType 的永久拒码(DELETE 的 ERR_EXT_DATA_0101218) - refused - ambiguous:RETIRE 的 ERR_PDI_0004,官方所谓「通用异常」 - feed_level:feed_error 或 legacy_feed_error - not_received:missing 且 detail_absent - review:带 ASYNC 码 - open:submitted - no_receipt:lapsed、legacy missing、legacy_bad_key flags 取值:upc_conflict(0101119)、sku_locked(0101211)、prohibited、content_rejected、qarth_dead。
  - 事实:- ① 落定时刻的全码集:读 ops.feed_item_errors,不只看首码;码集只出自 registry.resources。 - 台账 basis。
  - 触发:各消费方读取台账时调用。
  - 写者:- services/feed_track.receipt_class 一处解释。 - listing_sheet.classify_receipt 只剩「cls/flags → 表格文案」的投影,不再有第二份码判断。 - 守门测试:sku_migrate、sku_locked_heal、dispositions、各 sheet 模块不得直接引用 WALMART_ERR_*。 - error_taxonomy.classify_feed_error 是对照报告用的归类,读同一份码集,不属于第二份判据。
  - 下游:- sku_locked_heal、dispositions、sku_migrate、listing、match、maint 对同一条回执给出同一解释。 - ITEM_GONE 要进身份层,仍需 ⑥ 404。 - sku_migrate 删除「missing + feed_log failed」特判。 - sku_locked_heal 不再把 processing、unknown、missing 判成冷却 failed。
  - 风险:legacy_feed_error 回填之后,上架表里的一批 MISSING 行会进入 FAILED 重试通道;须先 dry-run 报条数。
- **C12** → poll_feed 读到汇总终态时,按以下规则落账: - 明细没读全,或 PROCESSED 且 itemsReceived=0 而本地 item_count>0:保持在途,basis=detail_incomplete,期限到后转 lapsed。 - 明细里有本片之外的 SKU,同时本片有 SKU 缺席:缺席的行 basis=detail_key_mismatch,期限到后转 lapsed,不标 missing。 - 满足 detail_count == itemsReceived > 0,且明细 SKU ⊆ 本片 skus:缺席的 SKU 落 missing(detail_absent)。 - 首读 ⊆ 校验通过之后,明细里有、台账里没有的 SKU 以 source='detail' 插入,workflow 与 feed_type 取自 feed_log。
  - 事实:三方计数核对: - ② 终态汇总的 itemsReceived / itemsSucceeded / itemsFailed; - ① 已读明细的原始条数与 SKU 集; - ③ 本地提交的 item_count 与 skus; 另有明细原文。
  - 触发:poll_feed 读到汇总终态时。
  - 写者:- services/feed_track.poll_feed:核对与落账;顺序固定为「收编首读校验 → 明细补插 → 条件 UPDATE」。 - api/feeds.iter_feed_items:只按短页终止,400 页上限。 - api/feeds.adopt_found:两本账同一事务写。 - api/feeds.norm_sku:SKU 键规范化的唯一出处。 - submit_feed:抢占之前检查,同一片内有重复或空 SKU 直接抛 ValueError。
  - 下游:- 不再出现「feed 已 done,却没有一条逐条事实」的情况。 - 零明细 PROCESSED 不再被推成「沃尔玛没收到」,也就不会触发 sku_migrate 回滚、冷却 lapsed、处置 ineffective(P0-5)。 - detail_absent 进入强事实清单,状态模型与原则 5 一致。 - 跟卖同店同 GTIN 的多行必须在提交前合并。
  - 风险:若 itemsReceived 与明细计数的口径本身对不上,这类 feed 会走到期限后的 lapsed,属于安全一侧的结果。
- **C13** → 调用方的账以台账为准,崩溃后可以重建: - ops.feed_items.ref 与 feed_attempts.refs 把回执挂回调用方的业务键; - 调用方在提交后立即 adopt 一次,每轮开头再 adopt 一次,两处调同一个函数; - 飞书回写成功与否,由 feed_items.projected_at 记录。
  - 事实:- ③ feed_log、feed_items、feed_attempts 三处本身就是 POST 回执的落账。 - dedup 返回 prev{status, basis, feed_id, workflow, attempt_id, updated_at}。 - 本地投影事实 projected_at。
  - 触发:提交返回时,以及每轮开头。
  - 写者:- api/feeds.submit_feed:透传 refs;dedup 带 prev;某片抛异常时,把已完成片的结果挂在原异常对象上(e.feed_results),类型不变后上抛;adopt_found 单事务。 - services/dispositions.adopt_from_ledger。 - services/listing_sheet.adopt_listed:list_new 提交当场与 heal_unknown 共用。 - sku_migrate._backfill_unsent;sku_locked_heal 按台账补建冷却行。 - list_new._settle_round 拆成「结算 / 落账 / 写表」三段各自 try;店级补试后合并首轮的 deferred 句柄。 - product_clear、match_listing 提交前查 open_skus,并查本工作流、本 (店, SKU, feedType) 在台账里 projected_at IS NULL 的已受理行:有就只补写表,不重发。这是状态判据,撤掉上一版的 96h 时间窗(P2-13)。 - 四个 sheet 模块在写表成功后回写 projected_at。
  - 下游:- *_submitted 事件、executing、UPC used、sku_migrations.feed_id、飞书 I/E 列,崩溃后都能补齐。 - 回写失败不再导致同一 SKU 被重复提交。 - 运营清空已投影的行重排时(projected_at 已有值),照常重发,语义不变。
  - 风险:adopt 若与旧路径并存,就成了双轨。「提交后直接 mark_executing / _record」的写法必须在同一批删除,并用守门测试钉住。
- **C14** → - 收编行的 basis:probe_found(已通过明细验证)或 probe_found_unverified(待验)。 - 首读若不满足「明细 SKU ⊆ 本片 skus」:经 api/feeds.reject_adoption,feed_log 回 pending(adopt_mismatch),被否的 feedId 记入 evidence.rejected_feed_ids;误挂上的 feed_items 行落 lapsed(adopt_mismatch)。 - 收编时撞上唯一索引:回 pending(adopt_conflict)。
  - 事实:- ④ 列表条目原文。 - ① 明细 SKU 集 ⊆ 本片 skus:这一步把推断升级为事实。上一版的「有交集」太弱,同店同类的重叠 SKU 集能轻易通过(P1-11)。 - ③ 本次首发 POST 的 post_started_at 作为锚点。 - feed_id 部分唯一索引。
  - 触发:反查时即读首页明细;明细为空时,在收编后首次读到非空明细时再验。校验排在明细补插之前。
  - 写者:- api/feeds.find_recent_feed:新增 anchor 与 skus 参数;feedDate 解析不出时不再放宽窗口;占用集改为 feed_log ∪ feed_attempts。 - services/feed_track.poll_feed:判定首读校验结果,经 api/feeds.reject_adoption 落账。
  - 下游:- list_new 并发结算不会再重复收编同一个 feed。 - 误收编的 SKU 不会以本工作流名进台账,也不会触发事件和黑名单。 - 旧系统、Seller Center 手工上传、同 feedType 的并发工作流,都会被 ⊆ 校验挡掉。
  - 风险:沃尔玛若对 SKU 做了规范化,⊆ 校验会误否;被否的行走对账,期限后落 lapsed,属于安全一侧。
- **C15** → - feed_log 作为当前指针,带 basis、basis_at、evidence、item_count、skus、refs、posted_at。 - feed_attempts 每次尝试一行;重占之后,上一笔的结论保留在它自己那一行的 log_final_* 里。 - 可变列清单写明(原则 3)。
  - 事实:③②④ 这三类事实本来都已拿到手,只是没有落库。
  - 触发:每次抢占、POST、反查、收口时。
  - 写者:- api/feeds._log_claim:条件重占,并在同一事务写 attempt 行。 - 收口方同事务写 log_final_*,分两种:pending 出口由 api/feeds.close_pending 写,submitted/lapsed 出口由 services/feed_track.close_feed 写。
  - 下游:- feed_statuses 与 -p feed_id 前缀诊断能经 feed_attempts 找到历史 feedId。 - pending 的展示时间取 attempt.claimed_at 与 post_started_at,不再取 created_at。 - 失败可以按 basis 分类统计。
  - 风险:feed_attempts 会持续增长:8000 条的改价片 skus 数组约 100KB。归档策略另议,但不得删除 B1 之后的行(它们是切换锚点与孤儿判定的依据)。
- **C16** → 锁跟随台账终态,不另设时钟: - 可重占:done、failed、lapsed ∧ reopenable。 - SKU 级在途只有一个出处:feed_track.open_skus(conn, stores=None, feed_types=None, cap_hours=None, success_until_seen=False, aliases=True)。
  - 事实:本地台账中的在途行: - feed_items 中 status=submitted 的行; - feed_log 中 pending 行的 skus; - MP_ITEM/MATCH 中 lapsed 且 effect 为 NULL 的行; - success 且 resolved_at > last_seen_at 的行,仅当 success_until_seen=True; - 经 catalog.sku_aliases 一跳继承。
  - 触发:业务工作流提交前、_log_claim 执行时、problem_scan 扫描时。
  - 写者:- api/feeds._log_claim:原子条件重占,并返回 prev。 - services/feed_track.open_skus:唯一出处;同时负责置 reopenable。 - workflows/problem_scan:删除 _SQL_INFLIGHT 原文,改为调用 open_skus(cap_hours=48, success_until_seen=True),口径逐字等价,有等价性测试钉住(P2-14)。 - workflows/sku_migrate:闸⑤ 只看本店 sku_migrate 的 pending;闸④ 改为逐候选检查同码冷却。
  - 下游:- 单个卡死的 feed 不再锁住整店改码。 - dedup 统一成三种说法:自己的在途(带 feed_id)、别人的在途(带 feed_id 与 workflow)、待对账(pending)。 - problem_scan 的 48h 封顶定稿保留。
  - 风险:SKU 级闸与载荷锁是两道不同的闸:前者防业务重发,后者防崩溃窗口内的并发双发。二者不构成双轨,docs/conventions §六 写清楚。
- **C17** → - 店停用(在册,但启用=否):超过期限后,feed_log 与 feed_items 落 lapsed(store_disabled),effect 落 unverifiable(store_disabled),处置落 lapsed。 - 已从凭证表整行删除的店:对应各处落 store_unregistered(P1-12)。 - 在营但不可达(凭证失效、缺代理、代理长期故障):保持非终态,hold_reason 写 diagnose 六档之一,每轮按原因折叠点名。 - -p store=X:只处理 X,不再对其它店报「凭证缺失」。
  - 事实:- 本地:services/stores.registered_names()、enabled_names()、load_stores() 的结果与读取时刻。 - ③:异常经 store_retry.diagnose 归档后的档位。
  - 触发:poll_all、verify_effects、reconcile_pending 每轮执行。
  - 写者:- services/feed_track.poll_all 新增 only 参数,跳店原因分四档:过滤、停用、未在册、不可加载。 - close_feed、verify_effects、api/feeds.close_pending。 - workflows/problem_scan 与 maintenance_scan:扫描面只取在营店;停用或未在册店名下的 suggested 撤为 withdrawn(store_disabled)。
  - 下游:- 摘要能区分:-p store 过滤、店停用、未在册、凭证坏。 - 停用店的建议与处置能够收口。
  - 风险:registered_names 与 enabled_names 依赖凭证表;读取失败时,本轮不做 store_* 收口(fail-closed)。在营但长期坏掉的店不会自动收口。
- **C18** → 维护处置改为观测优先(P1-9): - 过窗口、字段新鲜、现值等于目标:confirmed(observed),不论回执是什么。 - 过窗口、字段新鲜、现值不等于目标: - 回执 success ⇒ ineffective(accepted_not_applied); - 回执 refused 或 feed_level ⇒ ineffective(receipt_refused); - 回执 not_received ⇒ ineffective(not_received); - 回执 lapsed 或 sync_uncertain ⇒ ineffective(value_unchanged); - 回执 open ⇒ 等待,不下负向结论。 - EXPIRE_DAYS=3 内一直没有新鲜观测:回执 refused ⇒ ineffective(receipt_refused_unobserved);否则 ⇒ lapsed(expired:absent|store_unscanned|node_missing|inv_stale|receipt_open)。 止损: - 同一 (店, SKU, 动作, 目标值) 连续 2 次 accepted_not_applied:maintenance_scan 停止再建议,撤为 withdrawn(gate:accepted_not_applied_repeat)并点名。 - 多节点但未配「维护仓库」的店:库存意图挂起并点名(withdrawn gate:hold_multinode),扩展现有 _hold_nodeless,不再进入「提交 → 回执成功 → ineffective」的循环(W:M19)。
  - 事实:- ① 本行 (feed_id, sku) 的回执写进 detail.receipt,不推翻 receipt_in_ledger 定稿(那条只管病历白名单)。 - ⑤ 字段新鲜度:价格看 w.last_seen_at,库存看 avail_seen_at 或节点 seen_at,都须晚于 executed_at + 窗口。 - ⑦ EFFECT_WINDOW_HOURS: - price 2h:我方取值,官方 SLA 15 分钟,沿用 2026-08-26 的 2h; - inventory / MP_INVENTORY 4h:tsv「库存更新」行官方值; - MP_MAINTENANCE 6h:tsv「提交成功 → 数据可查」行官方值。
  - 触发:maintenance 开头调用 _settle;只对 psycopg.OperationalError 做条件明确的兜底,记日志并计数。
  - 写者:- services/dispositions.settle_maintenance:maint_effective 仍是唯一实现;新增回执 JOIN 与新鲜度条件。 - services/dispositions.expire_executing:改落 lapsed,并写明原因。 - services/walmart_catalog.upsert_items:写 avail_seen_at。 - services/maintenance_intents:接入多节点挂起与重复未采纳止损。
  - 下游:- A171 691466 这类「已生效却被记成 ineffective」的情况不再发生。 - 他人或旧系统已经改到位的值,落 confirmed,不再落 receipt_failed。 - L001 标题「回执成功但未采纳」有独立 basis,并有止损;满足 TITLE_SYNC 恢复条件 ②。
  - 风险:库存窗口拉到 4h、标题拉到 6h 后,链尾重赛之内判不出,会顺延一轮(D16)。
- **C19** → DELETE 核验: - 起点:回执为 success、lapsed、legacy missing,或 failed 但 receipt_class=gone 的行。不再只从 delete_feed_success 起算。 - 负向结论窗口 72h(官方口径)。 - 按 (feed_id, sku) 对位核验;处置账按本行 feed_id 绑定判决。 - not_effective 或 retired 在 14 天内出现 ⑥ 404,可改判为 effective。
  - 事实:- ⑤⑥⑦:同 C08。 - ① ITEM_GONE 码(含 QARTH 那种 status=success 却带死档码的情况):进身份层前必须经 ⑥ 404 对质。
  - 触发:feed_poll 每轮调用 verify_effects;catalog_sync 读取 effect 并弃码。
  - 写者:services/feed_track.verify_effects;services/dispositions.settle;workflows/catalog_sync(弃码点 1)。
  - 下游:- A162 的残留 SKU:404 收口后,因最近一次 complete 轮未列出它,一次 ⑥ 404 即判 effective 并弃码。上一版的 missing_source 口径下,这一条永远走不到(P0-1)。 - 约 700 条 ITEM_GONE failed、A085 的 611 条 QARTH,都经 ⑥ 定案。 - 处置账、病历、目录、登记簿四处同源。
  - 风险:not_effective 改判为 effective 时,对应处置可能已经落成 ineffective 并被重建议;处置保留历史、不回改,只影响统计口径。首轮单查约 1,300 个,分多轮消化。
- **C20** → 提交层失败按 basis 分档并计数: - 非 429 的 http_4xx:同一码连续 2 次,该行停止自动重发,落「永久拒」,依据为最近一次 4xx 的响应原文。 - not_sent、429、pending、probe_not_found:不计次,可以重试。 - feed 级 ERROR:不摊到逐个 SKU;同店同类连续 3 次,暂停该类提交并点名。 - 逐条 refused:同码连续 2 次停止。
  - 事实:③ 状态码与响应体;② feed 级 ERROR 与 ingestionErrors;feed_attempts 尝试行。
  - 触发:业务工作流领取待提交行之前。
  - 写者:- services/feed_track.attempts(conn, store, feed_type, workflow, skus):B1 之前的时段并入旧口径统计。 - workflows/list_new._retry_rows:改为调用 attempts;代际口径不变。 - product_clear、maintenance、problem_product_cleanup:按 basis 写文案。
  - 下游:- 永久 4xx 不再每天消耗稀缺令牌。 - 整 feed 拒收不再三次就把整批行永久耗尽。 - _PRE_FAIL 不再被写成「提交被拒」。 - MAX_LIST_ATTEMPTS=3 不变。
  - 风险:需所有者拍板 D12,包括上限值与豁免码。
- **C21** → 上架的行级状态由台账派生:latest_attempt、receipt_class(读全码集)与 effect 三者合成;飞书只做投影。 - 撞库弃码(弃码点 3)只在 status=failed、flags 含 upc_conflict、且仍是活码时触发。 - CONTENT_REJECTED 的人工回归:「上架结果」列被清空,且台账最近一次尝试为 refused ⇒ 进入 _retry_rows,不再要求运营另清 是否上架 / 上架feedid(L14)。 - 孤儿 claimed UPC 按 prep_failed 回收(L26)。
  - 事实:- ① ops.feed_items 与 ops.feed_item_errors 的全码集; - ③ feed_attempts(含 refs 'upc:<号>'); - ⑤⑥ effect; - 本地锁事实(锁文件 pid = 本进程)。
  - 触发:- feed_poll 反哺时; - list_new 领取时; - list_new 开头做孤儿回收,条件:claimed,claimed_at 晚于 min(feed_attempts.claimed_at) 且早于本进程启动,没有任何 attempt 的 refs 含该号,本进程持有 list_new 锁。
  - 写者:services/listing_sheet: - sync_from_ledger:按表上的 feedid 读台账; - heal_unknown、adopt_listed; - 撞库弃码改为台账驱动,调用点仍在 listing_sheet,弃码点位置不变。 workflows/list_new: - open_rows 排除「上架结果=FAILED」的行,与 _retry_rows 两路互斥; - write_submit_cols 在重提时清空上架结果、报错、feed查询日期三列; - 同一片内按 SKU 去重; - 孤儿回收。 services/upc_pool.release:白名单增加 not_sent。
  - 下游:- 重试后的新回执能回写到表上。 - SKU_LOCKED 能进入自愈链。 - MAX_LIST_ATTEMPTS 不再被绕过。 - SUCCESS 或 ASYNC 回执不再触发弃码。 - registry/resources.py:211-216 注释中的列字母 O 改为 Q,文档与代码一致。
  - 风险:表被人手改过时,「上架结果」以台账为准覆盖,须事先告知运营。
- **C22** → Unknown 行按本次尝试收尾。本次尝试 = feed_attempts 中 workflow='list_new'、skus 含该 SKU 的最新一行,按其状态处理: - pending:保持「待对账」。 - failed:写 No;只有 basis ∈ {http_4xx, not_sent} 且号仍为 claimed 时才回收;probe_not_found 不回收。 - submitted 或 done:读该 feed 的回执。refused 时写 No、mark_used,不回收;CONTENT_REJECTED、PROHIBITED、ASYNC 各走各的分支。 - lapsed:按 effect 判。effective(含 arrived_unlinked)时写 Yes、mark_used,并补 list_submitted 事件;not_effective 时写 No,不回收。
  - 事实:③ 本次尝试的 feed_log 行与 attempt 行;① 本次尝试所属 feed 的回执;⑤⑥ effect。上一次尝试的回执只作历史参考。
  - 触发:feed_poll 反哺链中的 heal_unknown。
  - 写者:services/listing_sheet.heal_unknown:经 feed_track.latest_attempt 取本次尝试,经 adopt_listed 写入。
  - 下游:- 已随 feed 发出的号不会被回收。 - 被内容标准拒掉的品不会被写成 Yes。 - list_submitted 事件能补齐。
  - 风险:没有 attempt 记录的存量 Unknown 行,只能靠观测或人工收尾:list_new -p resolve_unknown=<店>/<ASIN> -p as=listed|not_listed。not_listed 时号保持 claimed,不回收。
- **C23** → - MP_ITEM、MATCH、RETIRE 都有按提交挂钩的 effect,并记相应事件。 - RETIRE 回执为 ERR_PDI_0004 时,receipt_class=ambiguous,照常观测,不落 not_applicable(P0-6)。 - receipt_blocked 按 feedType 拆开: - ITEM_GONE 两类都挡; - ERR_EXT_DATA_0101218 只挡 DELETE 建议; - ERR_PDI_0004 不再单凭回执挡任何建议,只有「PDI_0004 ∧ 该 RETIRE 的 effect=not_effective」才挡 RETIRE 建议。
  - 事实:- ⑤ 在架状态、published_status、lifecycle。 - ⑥ 单查。 - ⑦ EFFECT_WINDOW_HOURS: - MP_ITEM 72h:我方取值,取 tsv「批量建品改品」行建议列,覆盖官方处理 4h、可见 6h、Hazmat 48h; - MP_ITEM(ASYNC)120h:我方取值,取「item setup 状态更新」行「危险品合规审核最长 3 个工作日」,含周末; - MATCH 72h:官方值; - RETIRE 48h:官方值。 - ① 码按 feedType 区分。
  - 触发:feed_poll 调 verify_effects;sku_locked_heal 在冷却期满时触发。
  - 写者:- services/feed_track.verify_effects / receipt_class。 - receipt_blocked 新增 feed_types 参数,并读 effect。 - registry/resources 新增 WALMART_ERR_PERMANENT_BY_FEED 与 WALMART_ERR_AMBIGUOUS_BY_FEED;WALMART_ERR_DESTRUCTIVE_PERMANENT 改为由它们派生的并集,不留第二份清单。 - workflows/problem_scan 按动作分两次调用。
  - 下游:- 跟卖表、上架表、停用表能显示「已生效 / 未生效(观测)」。 - ERR_PDI_0004 不再挡 DELETE 建议,也不再让 RETIRE 一律只信回执。 - product_clear 头注删掉「RETIRED 全豁免」。
  - 风险:会放出一批 DELETE 建议(不可逆),需 D9 确认,并按店分批放开。
- **C24** → 每次单品 PUT:先写一行 feed_attempts(kind=put),拿到结果后写一行 feed_items(feed_id='sync:<attempt_id>',feed_type 为 PUT_PRICE、PUT_INVENTORY 或 PUT_INVENTORIES): - 2xx 且目标节点成功 ⇒ success(sync_http); - 4xx 或节点失败 ⇒ failed(sync_http_4xx); - pre 或 connect ⇒ failed(sync_not_sent); - None 或 5xx ⇒ lapsed(sync_uncertain),处置转 executing,交观测。 进程死在 PUT 中,由对账器收口。
  - 事实:③ 同步响应:状态码、响应体,节点端点的 nodes[].status 与 errors[],以及 net_phase。
  - 触发:maintenance / node_clear 走单品 PUT 路由时:先 sync_begin,再调 api,最后 sync_finish。
  - 写者:- api/prices.put_price、api/inventory.put_inventory:ok 改为 True/False/None 三值,并带回 http_status 与 net,属接口适配。 - services/feed_track.sync_begin / sync_finish:PUT 的 attempt 与 feed_items 行都在 services 层写,api 不写台账(修正上一版把 record_sync_attempt 放在 api/feeds 的做法)。 - workflows/maintenance._submit_kind、workflows/node_clear:适配。
  - 下游:- 处置行 feed_id 记 'sync:<attempt_id>'。 - 同 SKU、同目标值,连续 2 次同码 4xx ⇒ ineffective(sync_4xx_repeat)。 - dedupe.meta 记 {feed_id, disposition_id, result}。 - resync_from_ledger 能补回 PUT 行。 - store_events 的两条路由改为同一口径。 - 存量 feed_id='sync' 的 executing 行只按观测判,detail.receipt='legacy_sync'。
  - 风险:problem_scan 在途闸会把 PUT 的 success 行算作「维护在途」,直到下一次扫描为止,与 feed 路由一致(D13)。
- **C25** → 投影只转述台账与处置账: - lapsed:「超期无回执(依据)」; - missing(detail_absent):「未收到」; - pending 或 feedid 为空:「待对账 log:<id>」; - effect:「已删除 / 已退役 / 已上线 / 未生效(观测)」; - 维护记录附处置 settled_by(含「未采纳」)。 删除表侧 3 天时钟。每次写表成功后,回写 projected_at。
  - 事实:台账的 status、basis、effect;处置账的 settled_by;本地投影事实 projected_at。
  - 触发:feed_poll 反哺器每轮执行。
  - 写者:- services/feed_track.RESULT_TEXT 与 text_of。 - services/maint_sheet:删除 STALE_DAYS;prune 与反哺共用会话级 pg_advisory_lock,finally 释放;resync 只补 RETAIN_DAYS 窗口;为 MP_INVENTORY 与 PUT_* 登记标签。 - clear_sheet、match_sheet、listing_sheet:反哺以台账 (feed_id, sku) 为键,不从可人改的列推导;写成功后回写 projected_at。
  - 下游:面板与库一致:「处理中」只表示台账里确实在途,「成功」不再等于「生效」。
  - 风险:新文案需所有者认可措辞(D15)。
- **C26** → 回执 status=failed 且带 ASYNC 审核码的 SKU,保持 submitted(basis=async_review): - 中途码写进 evidence.interim,不进 ops.feed_item_errors,也不写 *_feed_failed 事件; - 120h 后仍未落定,转 lapsed(async_deadline),由 effect 定案。 status=success 且带 ASYNC 码的,照常按 success 处理。 最终落定时,才写那一刻的全码集(P0-2)。这样 receipt_class 读到的是最终码,不会永远停在 review。
  - 事实:- ① registry.WALMART_ERR_ASYNC_REVIEW 码集。 - ⑦ tsv:危险品合规审核最长 3 个工作日;Hazmat 48h。
  - 触发:poll_feed 落账时映射;期限检查与 C02 相同。
  - 写者:services/feed_track.poll_feed(在 sku_outcome 之后,按码集改判为在途)与 close_feed。
  - 下游:- 上架表的 ASYNC_PENDING 能够落定。 - 跟卖表显示「审核中」。 - sku_migrate 不再因 ASYNC 回滚。 - 撞库检测等回执进入终态后再做。
  - 风险:含 ASYNC 码的 feed 最多在途 5 天,期间占用载荷锁(D10)。「同一 feed 明细会翻成 SUCCESS」只有旧仓实证。
- **C27** → withdrawn 必须带真实理由:扫描件按 (店, SKU, 动作) 传入跳过原因;keep 为空的分支也写理由。
  - 事实:本地闸门事实:在途 feed_id、死档码、永久拒码、停闸开关名、仅可恢复原子、重复未采纳、多节点挂起;现值已达目标时,附上最近一次台账提交的 feed_id。
  - 触发:problem_scan 与 maintenance_scan 每次调用 withdraw_stale 时。
  - 写者:services/dispositions.withdraw_stale:新增 reasons 参数;problem_scan、maintenance_scan 在扫描时汇集各行的跳过原因。
  - 下游:- 病历能区分:商品自己好了 / 被闸挡住 / 我方已改好。 - 我方提交因 ref 与 adopt 已对上账的,走 confirmed。
  - 风险:低。
- **C28** → 本地一致性修复: ① 抢占改为条件重占; ② 回执只在状态迁移那一刻落:事件由 RETURNING 驱动,落账事务先 SELECT … FOR UPDATE 锁住 feed_log 行; ③ 反哺与 prune 共用咨询锁; ④ -p stuck 与收工判据用同一个 unresolved,并按 basis 分档; ⑤ -p feed_id 诊断改为只读,复用 ingestion_errors; ⑥ maintenance._settle 只对条件明确的异常兜底; ⑦ 写者分区有守门测试(原则 8)。
  - 事实:不依赖外部事实。
  - 触发:—
  - 写者:- api/feeds._log_claim - services/feed_track.poll_feed / close_feed - services/maint_sheet - workflows/feed_poll._explain 与 _inflight_list - workflows/maintenance._settle:只捕获 psycopg.OperationalError,满足真兜底三要件
  - 下游:- 不再出现孤儿 feed_items、重复事件、错行回写。 - 诊断结论不再误导排查。 - 一次 PG 抖动不会让 product_chain 整段停下。
  - 风险:低。15:00 并发轮询时 FOR UPDATE 会排队,影响很小。
- **C29** → feed_poll --dry-run 全程只读:reconcile、poll、close、verify 各自在事务里算完后回滚;台账、事件、ASIN 黑名单、projected_at 一概不写,摘要标 [DRY-RUN]。 product_clear 的 H 列统一由 feed_track.merge_error 渲染。
  - 事实:写入台账的都是 ① 回执本身,内容无误。问题只在于「空跑不写」的承诺与不可逆的黑名单写入互相冲突。
  - 触发:--dry-run
  - 写者:- workflows/feed_poll.run:把 execute 透传给 services/feed_track 的 reconcile_pending、poll_all、verify_effects。 - product_clear 的就地轮询已在 B5 删除。
  - 下游:空跑与真跑走同一份判据,与 catalog_sync 的 rollback 做法一致。
  - 风险:空跑仍会真的 GET 沃尔玛(只读),包括对账器的列表 GET 与 ⑥ 单查,会占用读桶;摘要注明本轮新落的台账未落库(D14)。
- **G:MATCH-PRESUBMIT-SHEET-ONLY(无簇,复核判 REAL)** → 跟卖的提交前终局(目录无、需完整建品、码无效、风控或黑名单拦下、数据无效)除了写飞书 F 列,还入库一条 catalog.product_events 事件 match_presubmit_blocked,detail 记 {reason, code, sku(若已 mint), at}。 已 mint 未提交的活码,库里因此有「为何没提交」的事实。下次重跑时,mint 仍复用该码,不弃码。
  - 事实:本地预检结论(match_listing 的判定结果)与登记簿的 mint 记录。
  - 触发:match_listing 预检得出终局时。
  - 写者:workflows/match_listing 经 product_events.record_many 写入;事件码先登记在 EVENTS。这不是回执,不受 receipt_in_ledger 管辖。
  - 下游:病历能回答「这个 GTIN 为什么没跟卖」;登记簿里从没发出的活码可以追溯原因。
  - 风险:低。属于提交前,在「提交后查询与落账」的范围边缘,由 D17 决定做不做。

## 七、消费方改动

- **api/feeds.submit_feed 的七个调用方(共同契约)**:七个调用方:problem_product_cleanup.py:415、maintenance.py:253、sku_migrate.py:1610、product_clear.py:182、sku_locked_heal.py:98、list_new.py:2019、match_listing.py:266(传 match_sheet.WORKFLOW,即 'match_listing')。 契约: - 可选传入 refs,须与 entries 同序等长(disp:<id> / upc:<号> / mig:<id>)。 - 结果读 res['basis'],不再只看 outcome。 - dedup 结果带 prev,写表时分三种:自己的在途、别人的在途、待对账。 - 捕获异常后,先用 getattr(e, 'feed_results', []) 对已完成的片逐片落账,再按原异常类型处理;捕获 StoreDeadError 与 StoreProxyError 的各分支保持原样(conventions §四 不变)。 - 同片内有重复或空 SKU 会抛 ValueError,调用方须在提交前去重。 - workflow 参数必须等于 cli 工作流名,守门测试逐一钉住七处(对账器借锁判断依赖这一点)。
- **api/_client + api/items + api/prices + api/inventory(接口适配)**:- _request_ex、safe_get_ex、safe_post_ex、safe_put_ex 新增可选出参 net(dict),填入 phase(connect/read)、exc 类名、saw_401。 - connect 白名单:ConnectError、ConnectTimeout、PoolTimeout、ProxyError、SOCKSError;其余一律归 read。 - 401 刷新 token 撞 _NET_ERRORS 时,先在 net 里记 saw_401=True,再照旧上抛。 - 单品查询函数遇 200 空体抛 EmptyItemResponse(RuntimeError 子类)。 - put_price 与 put_inventory 的 ok 改为 True/False/None 三值,并带回 http_status 与 net。 - 以上都不写台账,不做业务判断。
- **api/feeds(防重账的写者)**:- _log_claim:条件重占,同一事务写 attempt 行与 skus/refs/item_count。 - _post:发送前单独提交 post_started_at;token 阶段非 StoreDeadError 的异常照旧 _PRE_FAIL、不上抛,basis 记 not_sent;StoreDeadError、401 刷新失败先落账再照旧上抛;rate_acquire 挪进 try。 - 2xx 无 feedId、补交不确定、settle 末轮一律走反查。 - 内联路径不做 ⑥。 - _ok_result 改名为公开的 adopt_found,单事务。 - 新增公开函数 reject_adoption、close_pending,供 services 调用。 - find_recent_feed:新增 anchor 与 skus 参数、覆盖证明、占用集并表;自写 probe 行。 - iter_feed_items:只按短页终止。 - get_feed_status / iter_feed_items:抛 FeedQueryError / FeedNotFound,报文为「返回 NNN」。 - 新增 norm_sku。 - 删除 mark_feed_done。
- **services/feed_track**:新增: - close_feed、reconcile_pending、verify_effects、judge_effect、observe、probe_item、sync_begin、sync_finish; - open_skus(参数化,含 aliases)、latest_attempt、attempts、receipt_class、item_ledger; - 常量:FEED_DEADLINE_HOURS、ASYNC_DEADLINE_HOURS=120、EFFECT_WINDOW_HOURS 及出处表 EFFECT_WINDOW_SRC、EFFECT_REVISE_DAYS=14、FEED_PURGE_MIN_AGE_DAYS=20、REREAD_EVERY_HOURS=4、REREAD_UNTIL_DAYS=35、EFFECT_PROBE_PER_STORE、RECONCILE_MIN_AGE_HOURS=2,均为唯一出处。 改写: - poll_feed:按固定顺序执行「收编首读 ⊆ 校验 → 明细补插 → 条件 UPDATE … RETURNING → 只对 RETURNING 写 feed_item_errors、事件、黑名单 → 三方计数与 key 漂移判定 → 同事务 close_feed」;支持 execute 参数。 - poll_all:新增 only 与 execute 参数;跳店分四档原因;对 lapsed feed 低频补读。 - item_results:保持原签名,内部改调 item_ledger。 - unresolved:改读台账 basis。 - RESULT_TEXT 增加 lapsed;missing 文案改为「未收到」。 删除: - 摘要里「pending 待人工核对」一段,改为报对账进展。
- **services/runlock**:新增 held_by_me(name):读锁文件里的 pid,与 os.getpid() 比较。供 sku_migrate 的 never_claimed 与 list_new 的孤儿 UPC 回收判定「本进程持锁」,不改变 acquire/hold 的行为。
- **workflows/feed_poll**:- 每轮顺序:reconcile_pending → poll_all → verify_effects(每店单查预算)→ 反哺器。 - --dry-run 全链回滚。 - -p stuck 与收工判据共用 unresolved,并按 basis 分档。 - -p feed_id 诊断改为只读。 - 新增 -p resolve=<log_id> -p as=submitted|failed|lapsed [-p set_feed_id=…] -p note=…,dry-run 同样生效。 - -p store=X 时只处理 X。
- **workflows/catalog_sync + services/walmart_catalog + workflows/store_release**:- 每店写一行 scan_rounds,与 upsert、mark_missing 同一事务。 - upsert 只有真拉到库存才写 avail_seen_at。 - 删除 verify_deletions 的调用;弃码点 1 改为按 effect 查「DELETE、effective、effect_basis 不是 legacy_event、登记簿仍是活码」,再调 sku_codec.abandon;--dry-run 时跳过。 - 报表兜底捕获 EmptyItemResponse,计入 backstop_empty,不算 gone,也不让整店同步失败。 - store_release 不改代码:核验不再读 missing_since,它造成的缺席自动不作数。 - _MARK_MISSING_SQL 不改。截断轮仍会调 mark_missing,影响见 open_risks 10。
- **services/product_events + catalog.product_risk 视图**:- 删除 verify_deletions,判据迁到 feed_track.judge_effect;本模块只负责事件落账。 - EVENTS 登记新事件:delete_retired、retire_verified、retire_not_effective、list_verified、list_not_effective、match_verified、match_not_effective、match_presubmit_blocked(D17)。 - delete_verified 与 delete_not_effective 的 detail 增加 feed_id 与 effect_basis。 - product_risk(及按店视图)新增 delete_retired_times 列,沿用 schema.sql 既有的 DROP+CREATE 做法。docs/db_schema.md 注明:B4 起 delete_verified 口径变严;adopt 会补齐崩溃窗口里漏记的 *_submitted,使 delete_times 等计数上升。
- **workflows/problem_scan**:- 删除 _SQL_INFLIGHT 原文,改为调用 feed_track.open_skus(cap_hours=48, success_until_seen=True),并用等价性测试钉住。 - receipt_blocked 按动作分次调用,口径见 D9。 - 新增 retired_gate:最近一次 DELETE 的 effect=retired,且当前 lifecycle 仍为 RETIRED,则跳过并单独计数。 - withdraw_stale 传入逐行原因。 - 扫描面只取在营店;registered/enabled 读取失败时不过滤,并在摘要点名。
- **services/dispositions**:- settle:改为按 (d.feed_id, d.sku) 连 ops.feed_items 的单条 UPDATE,优先级见 C09;删除 _SETTLE_DELETE_SQL;_RECEIPT_SETTLING 改为读 receipt_class。 - adopt_from_ledger(conn, ids=None):作为转 executing 的唯一路径,事件先查重。 - settle_maintenance:改为观测优先;增加回执 JOIN;窗口按 EFFECT_WINDOW_HOURS;加新鲜度条件;新增 accepted_not_applied、value_unchanged、receipt_refused_unobserved 三档。 - expire_executing:改落 lapsed,并写明原因。 - withdraw_stale:新增 reasons 参数。 - 新增 resolve_manual。 - 摘要计数增加 lapsed。
- **workflows/problem_product_cleanup**:- 提交时传 refs=['disp:<id>']。 - 提交后调 adopt_from_ledger(ids),取代直接 _record 加 mark_executing。 - 开头先 settle,再 adopt 一次。 - 维护记录文案按 basis 写。 - 新增 -p resolve=<disp_id> -p as=… -p note=…。
- **workflows/maintenance + services/maintenance_intents + workflows/node_clear**:- 提交时传 refs,统一经 adopt 转 executing。 - 单品 PUT 按 sync_begin → api → sync_finish 的顺序执行;处置 feed_id 记 'sync:<attempt_id>';结果为 None 时也转 executing。 - 同 SKU、同目标值、同码 4xx 连续两次 ⇒ ineffective(sync_4xx_repeat)。 - _settle 只对 OperationalError 兜底。 - record_submitted 的 meta 写 {feed_id, disposition_id, result}。 - 失败不再给已提交的意图写「未执行」。 - maintenance_intents:多节点但未配「维护仓库」的店,库存意图挂起(扩展 _hold_nodeless);同 (店, SKU, 动作, 目标) 连续 2 次 accepted_not_applied 时止损。 - 新增 -p resolve(维护类处置)。
- **workflows/maintenance_scan**:- 调 withdraw_stale 时传原因:TITLE_SYNC=False 时写 switch:TITLE_SYNC=False;另有 gate:hold_multinode、gate:accepted_not_applied_repeat。 - 扫描面只取在营店。
- **services/maint_sheet**:- 删除 STALE_DAYS,只转述台账。 - feedid 为空的行写「待对账 log:<id>」,对账收编后按 ref 补写。 - resync 限定在 RETAIN_DAYS 窗口,并登记 MP_INVENTORY 与 PUT_* 标签。 - sync_from_ledger 与 prune 共用会话级咨询锁。 - 结果列附上处置的 settled_by。 - 写成功后回写 projected_at。
- **workflows/product_clear + services/clear_sheet**:- 删除 _poll_feeds 里的就地 poll_feed,改为只读台账(item_ledger)。 - 提交前查 open_skus,以及本工作流、本 (店, SKU, feedType) 在台账里 projected_at IS NULL 的已受理行:有就只补写 E/F/G 列,不重发。撤掉上一版的 96h 时间窗。 - G 列新增:超期无回执 / 已删除(观测)/ 已退役(观测)/ 未生效(观测)。 - failed 按 basis 写文案;H 列统一由 merge_error 渲染。 - 「N 行落定」只计回执终态。 - 头注删掉「RETIRED 全豁免」。 - clear_sheet 写成功后回写 projected_at。
- **workflows/sku_locked_heal**:- 提交 RETIRE 前做一次 ⑥ 单查作为基线,写进冷却行的 evidence.baseline。 - _relist 只读台账,不再就地轮询。 - 冷却期满后先做 ⑥ 单查: - RETIRED ⇒ cleared(probe_retired); - 基线为 200 而此刻真 404 ⇒ cleared(probe_404_after_baseline); - 基线为 404 或缺失,此刻仍 404 ⇒ failed(baseline_404_unverifiable),D6 默认; - ACTIVE 且未满 48h ⇒ 继续等; - ACTIVE 且满 48h:回执 ok 或 ambiguous ⇒ failed(not_effective);同码 refused 连续 2 次 ⇒ failed(receipt_repeat);其余 ⇒ lapsed; - 单查不确定 ⇒ 继续等,满 14 天 ⇒ lapsed(probe_inconclusive)。 - failed_pairs 只取 status='failed'。 - 按台账补建缺失的冷却行。 - 新增 -p resolve=<店>/<SKU> -p as=cleared|failed|lapsed -p note=…;cleared 仍经弃码点 2。
- **workflows/list_new + services/listing_sheet + services/upc_pool**:- 提交时传 refs=['upc:<号>']。 - _apply_submit_result 按 basis 回收号:http_4xx→rejected,not_sent→not_sent;probe_not_found、pending、uncertain 一律不回收,同 ASIN 重试时复用原号。 - dedup 且 prev 是自己的 submitted ⇒ 走 adopt_listed。 - 计次改用 attempts。 - open_rows 与 _retry_rows 互斥;同片去重;提交前查 open_skus。 - 店级补试后合并 deferred;_settle_round 拆成三段 try。 - 开头做孤儿 UPC 回收(L26)。 - 新增 -p resolve_unknown。 - listing_sheet: - sync_from_ledger 改用 receipt_class 与全码集; - lapsed 与 ASYNC 投影 effect; - write_submit_cols 重提时清空三列; - heal_unknown 绑定本次尝试; - 新增 adopt_listed; - 撞库弃码改为台账驱动; - 「上架结果」被清空且台账最近一次为 refused ⇒ 进入重试(L14); - 写成功后回写 projected_at。 - upc_pool.release:白名单增加 not_sent,并写 status_basis、status_ref、status_at。
- **workflows/match_listing + services/match_sheet**:- 提交前用 open_skus 判断在途,并查 projected_at IS NULL 的已受理行,不再只看飞书 I 列。 - pending 或 unknown 写 J='待对账 log:<id>'。 - 同店同 GTIN 的多行在提交前合并。 - 反哺按台账 (feed_id, sku) 取数,并过滤 workflow='match_listing';lapsed 显示「超期无回执」,ASYNC 显示「审核中」,并投影 effect。 - MATCH 重发闸:最近一次尝试为 success,或为 lapsed 且 effect 不是 not_effective 的 SKU,不自动重发。 - 预检终局写 match_presubmit_blocked 事件(D17)。 - match_sheet 写成功后回写 projected_at。
- **workflows/sku_migrate**:- _settle 开头执行 _backfill_unsent;never_claimed 按本进程持锁判定(runlock.held_by_me)。 - _SQL_OBSERVE 纳入 stalled。 - 观测与单查一律经 feed_track.observe / probe_item / judge_effect。 - _verdict 改用 receipt_class 与 effect;OBSERVE_HOURS 改为 72;probe_not_found 提交的行不当场回滚。 - _shadow_candidates 扩大到全部 double 行;遇 EmptyItemResponse fail-closed。 - _candidates 排除改码可能已生效的旧码。 - 闸⑤ 只拦本店 pending;闸④ 逐候选检查。 - 判决依据写入 detail.verdict_basis、obs_run_at、receipt。 - 删除「missing + feed_log failed」特判。 - 新增 -p resolve。
- **services/store_events**:feed 路由与 PUT 路由统一按 attempt 与台账口径计数:submitted=已受理;failed=被拒或未发出;lapsed/pending=不确定。
- **workflows/feed_ledger_backfill(新增,一次性)**:- DANGEROUS=False,自己读取 dry_run;不进调度。 - 不带 -p step 时 ⛔ 硬拒。 - step 取值:legacy_basis / feed_error_missing / legacy_effect / legacy_executing / relabel_expired / validate。 - 每个 step 都先报条数,分批执行,可幂等重跑。
- **docs 与 refdata**:- db_schema.md:新表、新列、词表函数、依据键约束、backfill 与 VALIDATE 流程、product_risk 新列与口径变化。 - api_blueprint.md: - §2 端点 #3 GET /v3/items/{sku} 的使用模块,加 feed_poll(verify_effects、对账器 MP_ITEM/MATCH 窗口后核验)与 sku_locked_heal(基线与期满单查); - 端点 #16 GET /v3/feeds 列表加 feed_poll(对账器); - §7 第 7/9/11 行及 feed_poll、sku_locked_heal 行同步; - §3 注明 items.get 800/min 为共享预算(catalog_sync 兜底、verify_effects、对账器、sku_migrate、sku_locked_heal),每店由 EFFECT_PROBE_PER_STORE 限额; - §5.2 删去「启动对账」的空头承诺,改写为实现说明。 - feed_closure_audit.md:§三.1 改为「对账器 + -p resolve」;§三.4 按 D2 定稿。 - conventions.md:§六 加对账器与唯一出处清单、写者分区;§九⑦ 改写弃码点 1、2 的触发口径;§四 注明 _PRE_FAIL 行为不变。 - sku_plan.md §9.14、§9.15;multi_node_plan.md(多节点挂起);feishu_tables.md(新文案)。 - refdata/schema.sql 与以上文档同批修改。

## 八、分批落地

### B0 地基:纯加法,零行为变化

- schema.sql 加入 SC1–SC5,以 ledger-v2 标记包住:新列、ops.feed_attempts、catalog.scan_rounds、新索引。不加 CHECK,不写 DROP 和 DML。
- registry/resources 新增词表常量:FEED_LOG_STATES、FEED_ITEM_STATES、FEED_EFFECTS、FEED_ATTEMPT_OUTCOMES。
- services/feed_track 新增期限与窗口常量及出处表 EFFECT_WINDOW_SRC,本批不接线。
- 新增 workflows/feed_ledger_backfill,本批只实现 step=legacy_basis,缺 step 时 ⛔。
- 同步 docs/db_schema.md。

- 文件:refdata/schema.sql, docs/db_schema.md, registry/resources.py, services/feed_track.py, workflows/feed_ledger_backfill.py, tests/test_sku_guard.py, tests/test_feed_track.py
- 测试:守门: - FEED_DEADLINE_HOURS 与 walmart_slas.tsv「我方判死期限(建议)」列逐行相等。 - EFFECT_WINDOW_SRC 每项注明取 tsv 的哪一行、哪一列,或标「我方取值」;官方项断言数字出现在对应单元格。 - ledger-v2 段内不含 DROP、INSERT、UPDATE、DELETE;全文件不含 DROP INDEX(既有守门保留)。 - feed_ledger_backfill:缺 step ⛔;--dry-run 零写入。 测试库: - 连跑两次 db_init,结果幂等。 - pytest 全绿。 上线前连库执行 SELECT status, count(*) … GROUP BY 1,核对各表没有词表外的值。
- 风险:低。ADD COLUMN 带常量默认值只改元数据。feed_items 回填是百万级 UPDATE,要分批、低峰执行,先 dry-run 报条数。

### B1 提交层事实化 + 不确定交只读对账器(C04/C05/C10/C13/C14 提交侧/C15/C28①/C12 提交侧)(需先拍板)

- _client 增加 net 出参(connect/read/saw_401)。
- _log_claim 条件重占,同一事务写 attempt 与 skus/refs/item_count。
- _post:
  - 发送前单独提交 post_started_at;
  - token 阶段非 StoreDeadError 的异常照旧走 _PRE_FAIL、不上抛,basis 记 not_sent;
  - StoreDeadError 与 401 刷新失败先落账,再照旧上抛,异常上挂 feed_results;
  - rate_acquire 挪进 try。
- connect 阶段直接落 not_sent。
- 2xx 无 feedId、补交不确定、settle 末轮一律走反查;内联不做 ⑥。
- find_recent_feed:anchor、覆盖证明、⊆ 校验、占用集并表。
- adopt_found 单事务;新增 reject_adoption、close_pending;dedup 返回 prev;同片重复或空 SKU 抛 ValueError。
- SC6 条件建 feed_id 唯一索引。
- 新增 feed_track.reconcile_pending:借锁、只读;MP_ITEM/MATCH 窗口规则在 services 实现。
- 新增 feed_poll -p resolve / set_feed_id。
- 新增 runlock.held_by_me。
- list_new 按 basis 回收号(只认 4xx 与 not_sent);upc_pool 增加 not_sent。
- sku_migrate 只按强事实当场回滚;probe_not_found 不回滚;never_claimed 按本进程持锁判定。
- 七个调用方按 basis 写文案并处理 feed_results。
- api_blueprint 矩阵登记 feed_poll 为 GET /v3/feeds 与 GET /v3/items/{sku} 的消费方。

- 文件:api/_client.py, api/feeds.py, refdata/schema.sql, services/feed_track.py, services/runlock.py, services/upc_pool.py, workflows/feed_poll.py, workflows/list_new.py, workflows/sku_migrate.py, workflows/product_clear.py, workflows/sku_locked_heal.py, workflows/problem_product_cleanup.py, workflows/maintenance.py, workflows/match_listing.py, docs/db_schema.md, docs/api_blueprint.md, docs/feed_closure_audit.md, docs/conventions.md, tests/test_walmart_client.py, tests/test_feeds.py, tests/test_feed_track.py, tests/test_list_new.py, tests/test_sku_migrate.py, tests/test_sku_guard.py
- 测试:test_walmart_client: - 按异常类钉住 connect/read 的映射; - 401 刷新撞代理时 saw_401=True 且照旧上抛; - 断言 httpcore ProxyError 的抛出点。 test_feeds: - token 阶段 StoreProxyError ⇒ failed/not_sent,不上抛(conventions §四 行为不变); - StoreDeadError ⇒ failed/not_sent,照旧上抛且带 feed_results; - 401 刷新失败 ⇒ failed/http_4xx,照旧上抛; - rate_acquire 抛错 ⇒ not_sent; - ConnectError ⇒ not_sent,且不反查;ReadTimeout ⇒ 反查; - 补交 5xx ⇒ 再反查 ⇒ repost_uncertain; - 2xx 无 feedId ⇒ 反查; - 覆盖不足 ⇒ UNKNOWN; - 内联路径零单查(mock 断言 items 单查零调用); - 并发重占只有一方成功; - 同片重复 SKU ⇒ ValueError; - 撞唯一索引 ⇒ adopt_conflict。 test_feed_track: - 对账器 ⊆ 校验通过或否决; - 借不到锁 ⇒ 跳过; - never_posted 在持锁期间判定; - MP_ITEM/MATCH 窗口内 NOT_FOUND ⇒ window_wait;窗口后 ⑥ 全 404 ⇒ failed;窗口后任一 200 ⇒ arrived_unlinked; - 过期限仍 UNKNOWN ⇒ reconcile_exhausted,并插入 unlinked 行; - 全程零 POST。 test_list_new: - 回收原因词; - probe_not_found 不回收。 test_sku_migrate: - never_claimed 只在 held_by_me 为真时触发。 test_sku_guard: - 七个调用方的 workflow 名等于工作流名; - INSERT/UPDATE ops.feed_log 只出现在允许的函数里。 另需:list_new、sku_migrate、feed_poll 各跑一次 --dry-run;连库执行 SC6 前置重复核对。
- 风险:高,动的是核心防重路径。不换索引、不写 DROP,旧索引与新代码兼容。pending 会先增多,再被对账器收掉;首轮对存量 pending 的结论须先 dry-run 给所有者过目。GET /v3/feeds 的翻页与过滤能力须先按官方页核实。D1 被否决时,本批去掉 reconcile_pending,其余照上(降级方案见 C10)。

### B2 轮询落账事实化(C03/C12/C14 首读/C26 码/C28②④⑤/C29/C01 分类/C17 分因)(需先拍板)

poll_feed:
- 按固定顺序执行:首读 ⊆ 校验 → 明细补插 → UPDATE … WHERE status='submitted' RETURNING → _save_errors、事件、黑名单只对 RETURNING 执行 → 三方计数(detail_incomplete、detail_key_mismatch、detail_absent)。
- 中途码写进 evidence.interim;翻页中途失败时,已读部分照样落账。
- 新增 close_feed,与落账同事务并加 FOR UPDATE;删除 api/feeds.mark_feed_done。
其它:
- iter_feed_items 只按短页终止。
- FeedQueryError/FeedNotFound 按 diagnose 写 poll_error_class;404 只记录,不收口。
- poll_all 支持 only,跳店分因。
- --dry-run 全链回滚;_explain 改为只读;-p stuck 改用统一判据。
- backfill 新增 step=feed_error_missing(有界推断)。

- 文件:services/feed_track.py, api/feeds.py, workflows/feed_poll.py, workflows/feed_ledger_backfill.py, docs/feed_closure_audit.md, docs/db_schema.md, tests/test_feed_track.py, tests/test_feeds.py
- 测试:- 残留 feed 连续轮询两轮:success 行的 resolved_at 不变,事件只记一次;残留行 resolved_at 为 NULL。 - 中途读到 ASYNC 码、最终落定 SUCCESS:feed_item_errors 只有最终码。 - 明细把 success 报成 INPROGRESS:主表不变,rereads 追加。 - 收编 feed 明细里含本片外 SKU:判 misadopt,不补插,不写事件。 - post_2xx 的 feed 明细多出 SKU:插入 source='detail';同时本片有缺席的 ⇒ detail_key_mismatch,不判 missing。 - PROCESSED、itemsReceived=0、item_count>0 ⇒ detail_incomplete。 - itemsReceived 大于已读条数 ⇒ 不标 missing。 - 404 ⇒ 写 first_404_at,不收口。 - 两个事务并发落账同一 feed:只有一方迁移成功。 - dry-run 前后 feed_items、feed_log、feed_item_errors、asin_blacklist、product_events 均无变化。 - 裸 list 形态的 ingestionErrors 不会让诊断崩溃。 - 在测试库验证 feed_error_missing 的四个条件;连库 dry-run 报出转换条数。
- 风险:中。problem_scan 的在途面会明显缩小,当天可能新增一批删除建议,需人看首轮摘要。

### B3 在途收口:期限 / 404 / 店停用或未在册 / ASYNC → lapsed,SKU 级在途闸单一出处(C01/C02/C16/C17/C26)(需先拍板)

- close_feed 支持以下收口:
  - deadline:期限后有一次成功读取;
  - http_404:满 20 天且两次间隔 ≥6h;年轻的 404 只挂起并告警;
  - store_disabled / store_unregistered:读取失败时 fail-closed;
  - async_deadline:120h。
- ASYNC 的 failed 行改为在途。
- lapsed feed 每 4h 低频补读,最长 35 天,可升级。
- 置 reopenable:幂等类即刻置;MP_ITEM/MATCH 等 effect。
- open_skus 参数化;problem_scan 改为调用 open_skus(cap_hours=48, success_until_seen=True),并删除 _SQL_INFLIGHT 原文。
- _log_claim 按 reopenable 重占。
- RESULT_TEXT 与四张表认识 lapsed。
- 摘要按 basis 折叠点名。

- 文件:services/feed_track.py, api/feeds.py, workflows/problem_scan.py, services/clear_sheet.py, services/match_sheet.py, services/listing_sheet.py, services/maint_sheet.py, workflows/feed_poll.py, docs/feed_closure_audit.md, tests/test_feed_track.py, tests/test_problem_scan.py
- 测试:- 各 feedType 过期限、且有一次成功读取 ⇒ lapsed;过期限但店不可达 ⇒ 不收口,写 hold。 - 年轻 404 ⇒ 只写 hold;满 20 天第二次 404 ⇒ 收口。 - 停用店 ⇒ store_disabled;整行删除 ⇒ store_unregistered;读取失败 ⇒ 不收口。 - ASYNC failed ⇒ submitted/async_review,不写事件。 - 补读读到 success ⇒ 升级,history 留痕,补写 feed_item_errors。 - open_skus(cap_hours=48, success_until_seen=True) 与旧 _SQL_INFLIGHT 在夹具库上结果逐行相等,含 sku_aliases。 - 连库 dry-run 列出首轮将收口的 feed,与 feed_closure_audit 2026-09-11 那 6 条核对。
- 风险:中。首轮会集中收口存量,须先 dry-run 给所有者过目,并按店分批放开。

### B4 观测核验入账(effect,替代 verify_deletions)(C08/C19/C23 核验/C06 观测原语)(需先拍板)

- catalog_sync 写 scan_rounds 与 avail_seen_at;报表兜底捕获 EmptyItemResponse。
- api/items 遇 200 空体抛 EmptyItemResponse。
- 新增 feed_track.observe / probe_item / judge_effect / verify_effects:
  - 覆盖 DELETE/RETIRE/MP_ITEM/MATCH;
  - 只读 scan_rounds、last_seen_at 和 ⑥;
  - 正向与负向判据不对称,含纯单查路径;
  - RETIRE 的 PDI_0004 照常观测;
  - 由 feed_poll 调用,带单查预算;
  - 14 天内可改判;写新事件。
- catalog_sync 弃码点 1 改为读 effect;删除 product_events.verify_deletions。
- product_risk 新增 delete_retired_times。
- feed_track 对 MP_ITEM/MATCH 置 reopenable。
- backfill 新增 step=legacy_effect(有界)。
- 同步 api_blueprint 矩阵与 conventions §九⑦ 第 1 点。

- 文件:services/feed_track.py, services/product_events.py, services/walmart_catalog.py, workflows/catalog_sync.py, workflows/feed_poll.py, workflows/feed_ledger_backfill.py, api/items.py, refdata/schema.sql, docs/db_schema.md, docs/conventions.md, docs/api_blueprint.md, tests/test_feed_track.py, tests/test_product_events.py, tests/test_catalog_sync.py, tests/test_items.py, tests/test_sku_guard.py
- 测试:judge_effect 夹具: - 首轮缺席发生在截断轮、之后 complete 轮持续缺席 + ⑥ 404 ⇒ effective(gone_probe404)。这是 P0-1 的回归用例。 - store_release 标缺席、提交后无 complete 轮 ⇒ 要两次 404 间隔 ≥24h。 - 仍被 complete 轮列出(僵尸)⇒ 要两次 404。 - 200 空体 ⇒ 不判。 - 72h 后 ⑥ 200 非 RETIRED ⇒ not_effective;200 RETIRED ⇒ retired;14 天内 404 ⇒ 改判。 - RETIRE 回执 PDI_0004 ⇒ 照常观测,不落 not_applicable。 - MP_ITEM 窗口内 ⑥ 404 ⇒ 不下负向结论;无 complete 轮的店窗口后两次 404 ⇒ absent_probe404x2。 - 停用店过窗口 ⇒ unverifiable。 其它: - 报表兜底遇 EmptyItemResponse 不让整店失败。 - 守门:_ABANDON_CALLERS_OK 与 _ABANDON_FORBIDDEN 不变,feed_track 列入禁止名单;弃码点 1 不认 legacy_event。 - catalog_sync --dry-run 与 feed_poll --dry-run 零写入。 - 在测试库验证 legacy_effect 的上界配对(同 SKU 两次 success 的场景)。
- 风险:中高。弃码门槛收紧,product_risk 历史不可比;单查会拉长 feed_poll 耗时,需按预算观察。scan_rounds 从本批起才有数据,首日负向结论全部走两次单查路径。

### B5 破坏链消费方切到台账(C09/C11/C19 处置/C23 闸/C27 破坏侧)(需先拍板)

- dispositions.settle 改为台账单条 SQL,优先级见 C09;新增 lapsed;retire 行自己证明;relist 存量收口。
- 新增 adopt_from_ledger 与 resolve_manual;problem_product_cleanup 改走 adopt,删除直接调用 _record 与 mark_executing 的写法,新增 -p resolve。
- 新增 receipt_class(含 ambiguous 与 flags)。
- receipt_blocked 增加 feed_types 参数并读 effect;registry 新增 WALMART_ERR_PERMANENT_BY_FEED / AMBIGUOUS_BY_FEED,原集合改为派生。
- problem_scan:分次调用、加 retired_gate、传入原因、只取在营店。
- product_clear:去掉就地轮询;按 projected_at 防重;G/H 列改写。
- sku_locked_heal:提交前取基线;期满单查定案;冷却增加 lapsed;补建冷却行;failed_pairs 只取 failed;新增 -p resolve。
- backfill 新增 step=legacy_executing 与 relabel_expired(后者需 D7)。
- 同步 api_blueprint(sku_locked_heal 使用 #3)与 conventions §九⑦ 第 2 点。

- 文件:services/dispositions.py, services/feed_track.py, registry/resources.py, workflows/problem_product_cleanup.py, workflows/problem_scan.py, workflows/product_clear.py, services/clear_sheet.py, workflows/sku_locked_heal.py, workflows/feed_ledger_backfill.py, docs/conventions.md, docs/db_schema.md, docs/api_blueprint.md, tests/test_dispositions_router.py, tests/test_problem_scan.py, tests/test_problem_product_cleanup.py, tests/test_product_clear.py, tests/test_sku_locked_heal.py, tests/test_sku_guard.py
- 测试:dispositions: - 处置行按本行 feed_id 对位,不借用其它 feed 的判决。 - retire 行凭自己的 effect 落定。 - DELETE effect=retired ⇒ ineffective(effect:retired)。 - 店停用 ⇒ lapsed;台账缺行 ⇒ ledger_row_missing。 - legacy:<id> 挂账行走同一套判据。 problem_scan: - PDI_0004 不挡 DELETE;PDI_0004 ∧ not_effective 挡 RETIRE。 sku_locked_heal: - 基线 200、期满 404 ⇒ cleared;基线 404、期满 404 ⇒ failed(baseline_404_unverifiable);ERR_PDI_0004 但单查 RETIRED ⇒ cleared;missing 或 lapsed ⇒ 冷却 lapsed。 product_clear: - projected_at 为空的已受理行 ⇒ 只补写表,不重发;projected_at 非空、运营清空重排 ⇒ 重发。 守门: - adopt 之外没有转 executing 的路径。 - product_clear 与 sku_locked_heal 不再调用 poll_feed。 - 这些模块不直接引用 WALMART_ERR_*。 另需:连库 dry-run 报出约 800 条 executing 的去向分布。
- 风险:中。先 dry-run 报 executing 的去向;relabel_expired 需所有者批准;放开 DELETE 建议须按店分批(D9)。

### B6 上架 / 跟卖 / 改码消费方切到台账(C06/C07/C20/C21/C22/C26 表面/G:MATCH-PRESUBMIT)(需先拍板)

list_new 与 listing_sheet:
- refs、adopt_listed、attempts 计次;
- open 与 retry 互斥;重提时清空三列;heal 绑定本次尝试;
- 撞库弃码改为台账驱动,且只在 failed 时触发;
- 同片去重、open_skus 闸、按 basis 回收号;
- 孤儿 UPC 回收(L26);CONTENT_REJECTED 按台账回归(L14);-p resolve_unknown;
- projected_at 回写。
match_listing 与 match_sheet:
- open_skus、projected_at、待对账;
- 合并重复行、effect 投影、MATCH 重发闸;
- match_presubmit_blocked 事件(D17)。
sku_migrate:
- _backfill_unsent、never_claimed(held_by_me);stalled 继续判;
- 72h、complete 轮与 ⑥ 经 feed_track 原语;回执 success 仍不见新码 ⇒ stalled;
- 候选历史闸;闸④⑤ 收窄;probe_not_found 行等窗口;-p resolve。
文档:修正 resources.py 注释中的列字母。

- 文件:workflows/list_new.py, services/listing_sheet.py, services/upc_pool.py, workflows/match_listing.py, services/match_sheet.py, services/product_events.py, workflows/sku_migrate.py, registry/resources.py, docs/sku_plan.md, docs/db_schema.md, docs/feishu_tables.md, tests/test_list_new.py, tests/test_listing_l2c.py, tests/test_match_listing.py, tests/test_sku_migrate.py
- 测试:test_list_new: - FAILED 行不会同时进 fresh 与 retry;重提时清空结果列。 - SUCCESS 带 0101119 ⇒ 不弃码。 - not_sent、429、feed 级 ERROR、probe_not_found 均不计次。 - 同码 4xx 连续两次 ⇒ 停止。 - 孤儿 claimed 且无 attempt 引用 ⇒ 按 prep_failed 回收;有 attempt 引用 ⇒ 不回收。 - 「上架结果」被清空且台账为 refused ⇒ 进入重试。 heal_unknown: - 不拿上一次尝试的回执收尾。 - failed 回执 ⇒ mark_used,不回收。 test_sku_migrate: - 提交 24h、新码缺席、回执 success ⇒ 不回滚。 - 72h、complete 轮缺席、新码 404、回执 lapsed ⇒ 回滚。 - 回执 success 但新码 404 ⇒ stalled。 - probe_not_found 提交:窗口内不回滚;窗口后新码 404 ⇒ 回滚。 - never_claimed 只在本进程持锁时触发。 - stalled 在观测恢复后可转 confirmed。 - 旧码首轮在截断轮缺席、之后 complete 轮缺席 + 404 ⇒ confirmed。 - 旧码 RETIRED ⇒ 保持 double。 - 有改码成功史的旧码不再进候选。 另需:用 sku_migrate -p settle_only=1 --dry-run 跑 A131,结论须与 §9.15 一致。
- 风险:中高。改码定案变慢;上架表状态会被台账覆盖,须事先告知运营。

### B7 维护链 + PUT 入账 + 投影收口 + 工程缺陷(C18/C24/C25/C27 维护侧/C28③⑥)(需先拍板)

- put_price、put_inventory 改为三值返回。
- 新增 feed_track.sync_begin / sync_finish(先写后调);对账器处理 PUT 崩溃窗口。
- maintenance 与 node_clear 适配;处置 feed_id 记 sync:<id>;同码 4xx 连续两次即止损。
- settle_maintenance:观测优先、读回执、窗口按动作、字段新鲜度、新增 accepted_not_applied / value_unchanged / receipt_refused_unobserved。
- expire_executing 改落 lapsed 并写原因。
- maintenance_intents:多节点挂起、重复未采纳止损。
- dedupe.meta 记结局。
- maint_sheet:删除 3 天时钟、写待对账、resync 限窗口、加咨询锁、回写 projected_at。
- maintenance_scan 写原因、只取在营店。
- maintenance._settle 加兜底;新增 maintenance -p resolve。
- store_events 计数口径统一。

- 文件:api/prices.py, api/inventory.py, services/feed_track.py, services/dispositions.py, services/maintenance_intents.py, services/maint_sheet.py, services/store_events.py, workflows/maintenance.py, workflows/node_clear.py, workflows/maintenance_scan.py, docs/multi_node_plan.md, docs/feishu_tables.md, docs/db_schema.md, tests/test_maintenance.py, tests/test_dispositions_router.py, tests/test_store_events.py, tests/test_title_sync_switch.py
- 测试:- PUT 调用前 attempt 行已提交;进程在 PUT 中被杀 ⇒ 对账器落 sync_uncertain;从未开始 ⇒ sync_not_sent。 - PUT 返回 None ⇒ lapsed/sync_uncertain,处置转 executing;观测值等于目标 ⇒ confirmed。 - 同码 4xx 连续两次 ⇒ sync_4xx_repeat。 - 回执 failed,但窗口后新鲜现值等于目标 ⇒ confirmed(observed)。 - 回执 success,现值未变 ⇒ accepted_not_applied;连续两次 ⇒ 下轮不再建议。 - 多节点、未配仓的店 ⇒ 库存意图挂起。 - 库存观测字段陈旧 ⇒ 不下判定;回执在途 ⇒ 不下负向结论;超期 ⇒ lapsed 带原因。 - maint_sheet 不再写「超 3 天未查到」。 - maintenance 跑 --dry-run。
- 风险:中。维护链每天都在跑;窗口变长后,一部分判决会顺延一天(D16)。

### B8 约束收口(需先拍板)

- schema.sql 加入 SC7:四个状态词表函数、三个依据键函数,以及六本账的 CHECK … NOT VALID。
- feed_ledger_backfill 新增 step=validate,按手册执行:停调度 → 重跑 legacy_basis → 检查词表外的值与缺依据键的行(有则中止)→ db_init → 发布 → VALIDATE。
- 守门钉住:函数体里的词与 registry 常量逐字相等;ledger-v2 段不含 DROP 和 DML;新代码不写词表外的状态。

- 文件:refdata/schema.sql, workflows/feed_ledger_backfill.py, registry/resources.py, docs/db_schema.md, tests/test_sku_guard.py
- 测试:- 在测试库验证:约束存在;两次 db_init 幂等;词表外写入、缺依据键的终态写入当场报错;VALIDATE 幂等。 - 守门:函数词表与 registry 逐字对照;ledger-v2 段无 DROP、无 DML;全文件无 DROP INDEX。
- 风险:中。VALIDATE 遇到存量里的奇怪值会失败,由预检挡住。扩词表只能放宽、不能收窄,已写进文档。

## 九、待所有者拍板

### D1 要不要为 pending 建只读对账器?

A 维持现状:pending 只告警。
B 只读对账器,只调 GET /v3/feeds(④),在 services 层按规则落账:
- FOUND:经 ⊆ 校验后收编;
- NOT_FOUND:须有覆盖证明加双确认,非 MP_ITEM/MATCH 落 failed;
- MP_ITEM/MATCH:EFFECT_WINDOW 内只收编 FOUND,窗口后 ④ 未达且 ⑥ 全部 404 才落 failed,任一 200 落 lapsed(arrived_unlinked);
- 过期限仍 UNKNOWN:落 lapsed(reconcile_exhausted),交观测;
- post_started_at 为空、且借到原工作流锁:落 not_sent。
重发一律由原业务工作流在下一轮按原方法做。
C 同 B,另外对 DELETE/RETIRE 自动同载荷补交。

- **推荐**:B。若选 A,降级方案如下: - 保留「不确定一律 pending、不落 failed」; - C05 的 not_sent 照常落; - feed_poll -p resolve 工具照常上线; - C07 unsent 与 C22 Unknown 改由各自的 -p resolve 人工收口; - 不退回 C04 的现状(P2-24)。
- 理由:- pending 的入口至少 8 条,其中 4 条其实是确定未发出,已改落 not_sent。 - pending 会永久占住载荷锁、sku_migrate 闸⑤ 和 UPC。 - 五处文案承诺了「交启动对账」,代码里却不存在。 - 代理故障在生产上是常态。 - 对账器只是在另一个时点调同一个 find_recent_feed,不 POST。 - ⑥ 单查放在 services、且只在窗口之后用,不再落在 api 层(铁律 2)。 - C 会让对账器承担写操作。
- 是否推翻旧定稿:是,推翻 2026-08-16「pending 不做对账器,遇到了再说」。理由:新系统 pending 入口远多于旧系统;载荷锁与闸⑤ 会被永久占住;五处承诺的兜底并不存在。「写操作永不自动兜底」不推翻。

### D2 在途 feed 超过 feedType 期限后怎么收口?(feed_closure_audit §三.4 的待拍板项)

A 期限后至少有一次成功读取、且仍未终态的 SKU 落 lapsed(deadline),evidence 附 walmart_slas 出处;之后每 4h 补读一次,最长 35 天,可升级。锁的处理:幂等类即刻重开;MP_ITEM/MATCH 等 effect 定案后再开。
B 同样落 lapsed,但锁一律不开。
C 复用 missing 或 failed。
期限取 tsv 建议列:price 8h;inventory/MP_INVENTORY 24h;MP_ITEM/MP_MAINTENANCE/MATCH/RETIRE 72h;DELETE 96h;ASYNC 另给 120h。

- **推荐**:A
- 理由:- 2026-09-11 实测:6 条 feed 该判死,14 条该继续等,一个全局阈值分不开。 - 「期限后读到仍是 INPROGRESS」是可以落库的事实。 - lapsed 不下失败结论,也不触发不可逆动作。
- 是否推翻旧定稿:否。这是填补 §三.4 悬而未决的一项,落地后替代 2026-09-11「不自动放弃任何在途 feed」这条临时纪律。

### D3 GET feed 状态返回 404,满足什么条件才判 lapsed?

A 提交满 24h,且两次 404 间隔 ≥1h。
B 提交满 20 天,且两次 404 间隔 ≥6h;不满 20 天的 404 只挂起并告警。
C 永不收口。

- **推荐**:B
- 理由:- A085 第 29 天仍可查,A109 第 32 天、A162 第 38 天为 404。 - 早到的 404 更可能是凭证串店或 feedId 转义错误,应该告警,而不是静默收口。 - 正常情况下 D2 的期限会先把 feed 收掉。
- 是否推翻旧定稿:否

### D4 店铺不可达、停用或已从凭证表删除时,在途 feed 与处置怎么办?

A 一律保持非终态。
B 约 30 天后一律按推断收口。
C 按店的状态分三种处理:
- 在册但启用=否:过期限后落 lapsed(store_disabled);
- 已从凭证表整行删除:落 store_unregistered;
- 在营但不可达:保持在途,按 diagnose 六档折叠点名,也可以人工 resolve。

- **推荐**:C
- 理由:停用与删除都是所有者意图,属于本地事实;两者分开记,是因为「启用=否」这一事实只对仍在册的店成立(P1-12)。在营但不可达的店,那段时间确实没有事实可依。
- 是否推翻旧定稿:否

### D5 删除核验(弃码点 1)的观测口径

A 维持现状:缺席、RETIRED、从未观测到都算 gone,宽限 48h。
B 收紧:
- gone 必须 ⑥ 真 404。posted_at 之后最近一次 complete 轮未列出它时,一次 404 即可;仍被列出、或提交后没有 complete 轮时,要两次 404,间隔 ≥24h。
- 判据只读 scan_rounds、last_seen_at 与 ⑥,不读 missing_since。
- RETIRED 单列为 effect=retired:不弃码、不烧号,处置落 ineffective(effect:retired),problem_scan 加 retired_gate。
- not_effective 必须 72h 后 ⑥ 返回 200、非空、不是 RETIRED。
- 14 天内出现 404 可以改判。
C 同 B,但 RETIRED 仍算 gone、照常烧号。

- **推荐**:B
- 理由:- 弃码和烧号都不可逆。 - 截断轮与 store_release 会造出假缺席,而 missing_since 的来源在首次缺席时就冻结了,不能当判据。 - 列表会吐回已删的档案。 - RETIRED 的条目仍在目录里。 - 48h 短于官方的 72h。 - 处置目的是「删除」,观测到 RETIRED 就是「没删掉」,记 confirmed 与事实相反(P1-8)。
- 是否推翻旧定稿:推翻现行 verify_deletions 的 gone 口径与 48h 宽限。不推翻「删除以观测定案」,而是收紧观测质量来加强它。sku_wiring_audit L-4 原本就挂着待定。

### D6 SKU_LOCKED 自愈的弃码点 2:从「绑回执」改为「绑单查观测」?

A 维持:回执 success 且冷却期满就弃码,其余一律 failed。
B 提交 RETIRE 前先做一次 ⑥ 作为基线。冷却期满后再做 ⑥:
- RETIRED ⇒ cleared,弃码;
- 基线为 200、此刻真 404 ⇒ cleared,弃码;
- 基线为 404 或缺失、此刻仍 404 ⇒ failed(baseline_404_unverifiable),转人工;
- ACTIVE:等到 48h;回执 ok 或 ambiguous ⇒ failed;同码 refused 连续两次 ⇒ failed;其余 ⇒ lapsed,自动重新退役。
B′ 同 B,但「基线 404、回执 ok」时也允许 cleared(receipt_ok_unobservable)。这保住现有能力,代价是依据只剩回执。

- **推荐**:B。锁死 SKU 的单查基线分布不明,首跑 dry-run 报数后可以再议 B′。
- 理由:- RETIRE 回执近 100% 是 ERR_PDI_0004,现在这条链几乎全部转人工。 - 锁死的 SKU 不在 walmart_items 里,单查是唯一能等到的观测。 - 404 可能在 RETIRE 之前就一直是 404,没有基线就不能证明退役生效(P1-10)。
- 是否推翻旧定稿:改写 conventions §九⑦ 对弃码点 2 触发口径的描述(由「唯一绑回执的一个」改为「基线 + 单查观测」)。弃码点的位置、数量,以及 _ABANDON_CALLERS_OK 都不变。

### D7 处置账是否新增终态 lapsed?存量 expired 行与存量 executing 怎么处理?

A 新增 lapsed。存量 settled_by='expired' 的 ineffective 行改标 lapsed;存量破坏类 executing 经回填补一条 'legacy:<id>' 挂账行,走同一套观测判据。
A′ 同 A,但 expired 存量行不改标,只在文档注明。
B 不加新状态。

- **推荐**:A(存量是否改标,所有者可选 A′)
- 理由:- expired 行没有任何观测依据,而 A171 691466 已证明它可能与事实相反。 - lapsed 同样释放部分唯一索引。 - 存量 executing 如果对应的 feed_log 已被重占,就无法判定 ledger_row_missing;走挂账行,是唯一不另写一套判据的出路。
- 是否推翻旧定稿:修改 expire_executing 落 ineffective 的口径,以及 stuck_executing 头注「破坏类不自动放行」的立场,只限停用或未在册店、无法核验、台账缺行三种有依据的情形;在营但不可达的店仍然不放行。

### D8 sku_migrate 的定案、回滚与候选闸是否收紧?

A 维持现状。
B 收紧如下:
- 回滚只凭事实:回执 refused、feed_error、not_received;提交层 http_4xx 或 not_sent;probe_not_found 的,等窗口后新码 ⑥ 404;72h 后 complete 轮缺席、新码 404、且回执不是 success/review/open。
- 回执为 success 或 review,但新码 404 ⇒ stalled,交人工。
- (a) 改为:complete 轮未列出旧码 + ⑥ 旧码 404;没有 complete 轮时两次 404。
- double:⑥ 旧码真 404 ⇒ confirmed。
- stalled 继续判定。
- 有过可能已生效改码史的旧码,不再成为候选。
- 闸⑤ 只拦本店 pending;闸④ 逐候选判断。
- never_claimed 按本进程持锁判定。
- 提供 -p resolve 人工收口。
B1 同 B,但凡有改码史的旧码都不自动再改。
B2 同 B,但 double 的旧码为 RETIRED 时也判 confirmed。

- **推荐**:B
- 理由:- 官方 MATCH 最长 72h。 - 回滚与确认都不可逆。 - A131 已实证:对死档的旧 item 重发 MATCH 会新建 listing。 - 整店闸会被单个卡死行永久锁住。 - probe_not_found 只证明 feed 没被受理;窗口内的 ⑥ 404 不是事实。
- 是否推翻旧定稿:修改 OBSERVE_HOURS=24、stalled 不再读、闸⑤④ 整店口径、§9.15「wpid 不同不探测」。不推翻 double 定稿,也不推翻「MATCH 不一刀切」定稿。

### D9 receipt_blocked 的永久拒码是否按 feedType 分开?ERR_PDI_0004 怎么定性?

A 维持:DELETE 与 RETIRE 合并,取最近一次尝试;PDI_0004 视为「重发必再拒」。
B 按码分开处理:
- ITEM_GONE 仍然两类都挡;
- ERR_EXT_DATA_0101218 只挡 DELETE;
- ERR_PDI_0004 改定性为 ambiguous(通用异常):不再单凭回执挡任何建议,只有「PDI_0004 ∧ 该次 RETIRE 的 effect=not_effective」才挡 RETIRE 建议;对 DELETE 一律不挡。
registry 新增按 feedType 分组的码表,原集合改为派生。

- **推荐**:B。会放出一批 DELETE 建议(不可逆),建议按店分批放开。
- 理由:- ERR_PDI_0004 几乎覆盖全部 RETIRE 回执,本身并不说明是否已退役;观测才能说明。 - 顽固件双发时,最近一次尝试常常就是 retire 的 PDI_0004,于是该 SKU 永远进不了自动删除。 - 「不许停用」也不等于「不许删除」。
- 是否推翻旧定稿:部分推翻 2026-09-09 定稿:receipt_blocked「DELETE_ITEM 与 RETIRE_ITEM 都算」,以及 WALMART_ERR_DESTRUCTIVE_PERMANENT 把 ERR_PDI_0004 列为「重发必再拒」。死档码集(ITEM_GONE)不推翻。

### D10 ASYNC 合规审核码怎么记?

A 维持:当时回执为 failed 就冻结成终态。
B 该 SKU 保持在途(async_review):中途码只进 evidence.interim,不写 *_feed_failed 事件;120h 后转 lapsed(async_deadline),交观测;最终落定时才写码。

- **推荐**:B
- 理由:registry 注释明写:审核中的码几小时到几天会自然翻成 SUCCESS。现状会让 sku_migrate 误回滚。中途码若进 feed_item_errors,最终码会被丢掉,C26 永远收不了口(P0-2)。
- 是否推翻旧定稿:否

### D11 UPC 撞库弃码(弃码点 3)的触发条件

A 维持:由飞书行驱动,不看回执状态。
B 由台账驱动:status=failed,flags 含 upc_conflict(读全码集),且仍是活码;弃码点位置不变。

- **推荐**:B
- 理由:不可逆动作不应取决于飞书行是否存在;回执为 SUCCESS 却带 0101119 时,码其实已经在架。
- 是否推翻旧定稿:否

### D12 上架重试怎么计次,提交层失败怎么止损?

A 维持:只数 feed_items 里的 MP_ITEM 行。
B 按 feed_attempts 计次:
- not_sent、pending、probe_not_found、429、feed 级 ERROR 都不计入逐 SKU 次数;
- feed 级 ERROR 同店同类连续 3 次,暂停该类提交并点名;
- 非 429 的同码 4xx 连续 2 次,停止该行;
- 逐条 refused 同码连续 2 次,停止该行;
- MAX_LIST_ATTEMPTS=3 保持不变。

- **推荐**:B
- 理由:现状两头都与事实相反:永久 4xx 每天烧稀缺令牌;整 feed 拒收三次就把整批永久耗尽。
- 是否推翻旧定稿:改变 product_clear 与维护链「提交被拒 ⇒ 自动重排不限次」的设计立场。

### D13 单品 PUT 是否进入台账?

A 维持现状。
B 每次 PUT 先由 services 写 attempt 行(先写后调),返回后写 'sync:<attempt_id>' 行。结果为 None 或 5xx 时落 lapsed(sync_uncertain),交观测;进程死在 PUT 中由对账器收口;同码 4xx 连续两次即止损。

- **推荐**:B
- 理由:- PUT 失败目前在库里零痕迹。 - status 为 None 时改动可能已经生效。 - 先写后调,符合红线「先落库再调接口」(P2-15)。 - 由 services 写台账,api 保持纯适配。
- 是否推翻旧定稿:推翻 maintenance._submit_kind 注释里「PUT 不进 feed 台账」的立场。

### D14 feed_poll --dry-run 是否改为零写入?

A 维持现状。
B 各步在事务内算完后回滚;仍会发起只读 GET,包括对账列表与单查。

- **推荐**:B
- 理由:现状下空跑会写入永久的 ASIN 黑名单和病历事件,与「改完先 dry-run」的纪律矛盾。
- 是否推翻旧定稿:推翻 feed_poll 头注「--dry-run 只拦反哺器,台账落定空跑照写」。

### D15 飞书投影:删掉表侧 3 天时钟,只转述台账?文案是否认可?

A 删掉 STALE_DAYS,改为投影台账与处置账;写成功后回写 projected_at。新文案:超期无回执(依据)/ 未收到 / 待对账 log:<id> / 已删除 · 已退役 · 已上线 · 未生效(观测)/ 未采纳 / 审核中。
B 保留 3 天表侧超期。

- **推荐**:A
- 理由:表侧时钟写出的「未查到」没有依据。projected_at 还顺带取代了 product_clear 的时间防重窗(P2-13)。
- 是否推翻旧定稿:推翻 maint_sheet.STALE_DAYS=3 的兜底规则。

### D16 维护类的结论口径与止损

A 维持:统一宽限 2h,只看 last_seen_at。
B 按以下规则:
- 观测优先:窗口后、字段新鲜、现值等于目标 ⇒ confirmed,不论回执。
- 窗口按动作:price 2h(我方取值,官方 SLA 15 分钟)、inventory/MP_INVENTORY 4h(官方)、标题 6h(官方「数据可查」)。
- 库存须 avail_seen_at 或节点 seen_at 晚于提交加窗口。
- 回执在途时不下负向结论。
- 新增 accepted_not_applied,同目标连续两次即止损。
- 多节点但未配「维护仓库」的店,库存意图挂起。

- **推荐**:B
- 理由:- 他人或旧系统已经改到位的值,现状会被判成 receipt_failed。 - 库存拉取失败时,COALESCE 会把旧值当新值。 - 标题与多节点两种循环,现状只记账、不止损。
- 是否推翻旧定稿:修改 2026-08-26 引入的 MAINT_SETTLE_GRACE_HOURS=2 中库存与标题两档(价格不变);扩展 _hold_nodeless 的挂起范围。

### D17 跟卖提交前的终局要不要入库?(G:MATCH-PRESUBMIT-SHEET-ONLY,属提交前的范围边缘)

A 维持现状:只写飞书 F 列。
B 另写一条 catalog.product_events 事件 match_presubmit_blocked,detail 记 {reason, code, sku, at},不弃码。
C 另立项,放到本设计之外处理。

- **推荐**:B
- 理由:改动小。已 mint、却从未提交的活码,在库里有了「为何没提交」的事实。它不是回执,所以不受 receipt_in_ledger 管辖。
- 是否推翻旧定稿:否

## 十、开放风险

1. 外部接口的假设尚未核实
- GET /v3/feeds 列表:蓝图只登记了 limit≤50。offset 翻页、feedId 过滤、排序、是否返回 totalResults,都要在 B1 之前按官方 getallfeedstatuses 页核对。核对不了就只判 UNKNOWN,那样对账器的 NOT_FOUND 很少能成立,更多 pending 会走到期限后的 reconcile_exhausted。
- 以下几点未核实:RECEIVED 阶段 itemsReceived 是否已填好;刚提交的 feed 是否会短暂返回 404;ASYNC 码会在同一 feed 明细里翻成 SUCCESS(只有旧仓实证);约 30 天的状态保留窗口(经验值)。
- SKU_LOCKED 的锁死 SKU 单查会返回什么(200 / 404 / 空体),分布不明。D6 的 baseline_404_unverifiable 可能占多数,首跑要 dry-run 报数。

2. connect 白名单依赖 httpcore/socksio 的异常树
已在本机 httpcore 1.0.9 核实。生产版本需要钉住,依赖升级时必须复核。

3. 本地锁事实的前提
- 七个 submit_feed 调用方与 PUT 调用方(maintenance、node_clear)传入的 workflow,必须等于 cli 工作流名,守门测试钉住。
- held_by_me 依赖 runlock 写入锁文件的 pid;若 acquire 的写入格式改动,要同步修改。
- 「借锁即放」有一个窗口:新一轮同名工作流可能刚好在释放后启动。判定都在持锁期间完成,并且只处理 claimed_at 早于借锁时刻的行,可以接受。

4. B1 风险最高
它同时引入「不确定留 pending」和对账器,所以必须同批上线。D1 被否决时的降级方案见 D1 与 C10:只上 not_sent、事实记录和人工 resolve,不退回 C04 的现状。

5. MP_ITEM/MATCH 的 pending 最长约 72h
这是 P0-3 的代价:窗口内只收编 FOUND,期间占用载荷锁,UPC 保持 claimed。同 ASIN 重试会复用原号,所以不会撞库;但 list_new 在这段时间内看到的会是「待对账」。

6. 首轮集中收口
B1、B3、B5 上线的首轮,会一次关闭一批长期在途行(A085、A109、L001、A162、A171、存量 pending、约 800 条 executing),随后重发会让首日稀缺令牌突增。须先 dry-run 出清单交所有者过目,再按店分批放开。

7. 数据量
- feed_items 回填是百万级 UPDATE。
- feed_attempts.skus 在 8000 条的改价片上,每行约 100KB,落在 TOAST 里。
- effect_open 部分索引在 legacy_effect 回填之前会很大。
- feed_attempts 在 B1 之后的行不得删除或归档:它们是 min(claimed_at) 切换锚点,也是孤儿与 never_claimed 判定的依据。

8. 约束批次(B8)
- 词表函数放宽之后,VALIDATE 不会重验存量行,所以只能扩、不能收。
- 执行必须按手册顺序,并停掉调度。
- dispositions 与 sku_migrations 的依据键 CHECK,依赖所有写者都已按新口径写 settled_by、verdict_basis、stall_reason;B5–B7 都要先落地。

9. 单查预算与耗时
- 共享 items.get 800/min 的有:verify_effects、对账器(MP_ITEM/MATCH 窗口后)、sku_migrate、sku_locked_heal(基线与期满)、catalog_sync 报表兜底。
- 首轮积压约 1,300 个,另加 B4 上线后第一轮没有 complete 轮时,所有负向结论都走两次单查。
- 按 EFFECT_PROBE_PER_STORE 分多轮消化,摘要报积压量。feed_poll 耗时会变长,需观察它与 30 分钟调度的重叠。

10. 观测侧遗留
截断轮仍会调用 mark_missing,list_new 同店去重闸、problem_scan 扫描面、在线表投影仍把这些缺席当作不在架,可能引起重复上架或漏扫。本设计的核验已不受它影响(P0-1),但它本身没有修。建议另立一项交所有者:截断轮是否跳过 mark_missing。

11. 双轨风险
以下旧路径须与新路径同批删除,并由守门测试钉住:
- product_events.verify_deletions
- _SETTLE_DELETE_SQL
- problem_scan._SQL_INFLIGHT 原文
- api/feeds.mark_feed_done
- sku_migrate 自写的旧码缺席与影子判据
- listing_sheet.classify_receipt 里的码判断
- 各工作流「提交后直接 mark_executing / _record」
- product_clear 与 sku_locked_heal 的就地 poll_feed
- 只告警的 _SQL_UNSENT
- expire 落 ineffective
item_results 只作为 item_ledger 的薄包装保留。

12. 语义漂移
- delete_verified 变严后,product_risk 历史不可比;新增 delete_retired_times 列。
- adopt 会补齐崩溃窗口里漏记的 *_submitted,使计数上升。
- 四张飞书表的新文案需要所有者认可,并告知运营。

13. projected_at 依赖四个 sheet 模块在写表成功后回写
如果回写本身失败,下一轮会把已投影的行当作「未投影」再补写一次(只写表、不重发)。影响只是一次重复写表。

14. 新旧系统并跑
⊆ 校验能兜住误收编;但「严禁对同一破坏性任务并跑」仍要靠运维纪律。

15. 在营但长期不可达的店
这类店的在途 feed、executing 处置、effect 会一直是非终态,只点名原因(D4);出口是人工 resolve,或在凭证表取消「启用」。

16. 决策之间互相依赖
- D5 选 A:B4 退回旧口径。
- D8 否决:C06/C07 只剩 B1 的回填部分。
- D9 选 A:C23 的 DELETE 放行不生效,RETIRE 的 PDI_0004 仍然只凭回执。
- D6 选 A:sku_locked_heal 不做基线单查,api_blueprint 对应行不改。

17. 本设计没有连库
以下全部未知,每批首跑先 dry-run 报数;按 conventions §五,判不准就判活:
- 存量 pending 数、残留数、executing 的去向;
- 历史 missing 转换条数、expired 改标条数;
- 是否已有共享 feed_id;
- delete_feed_success.detail 带 feed_id 的覆盖率。
