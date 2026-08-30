# feed 闭环审计(2026-08-16)

> 所有者的原话:「feed 收尾闭环我担心的是**是否有写入数据库,产品事件是否完善
> 且闭环**。这个我无法直接观测到,就表格中各处 feed 来说,是完整的。」
>
> 所以本文只审**库侧**。飞书侧他已经确认完整,不重复审。
> 方法:把六个提交点、三张台账表、五个反哺器逐一对照代码读一遍(不连库)。
> 结论先说:**主干是闭的,有两处漏、一处断头路。**

## 一、六个提交点(全项目 `feeds.submit_feed` 的全部调用者)

| 工作流 | feedType | `workflow=` 标签 | 提交事件 | 回执事件 | 飞书反哺 |
|---|---|---|---|---|---|
| `list_new` | MP_ITEM | list_new | `list_submitted` | `list_feed_*` | 上架表 + 自愈 |
| `match_listing` | MP_ITEM_MATCH | match_listing | `match_submitted` | `match_feed_*` | 跟卖表 |
| `product_clear` | DELETE_ITEM / RETIRE_ITEM | product_clear | `delete/retire_submitted` | `delete/retire_feed_*` | 停用/删除表 |
| `sku_locked_heal` | RETIRE_ITEM | sku_locked_heal | `retire_submitted` | `retire_feed_*` | 无(冷却表自管)|
| `problem_product_cleanup` | DELETE_ITEM / RETIRE_ITEM / MP_MAINTENANCE | problem_product_cleanup | 三种 `*_submitted` | 三种 `*_feed_*` | 维护记录(2026-08-24 起)|
| `maintenance` | MP_MAINTENANCE / price / inventory | maintenance | 无(维护类不入病历)| 无 | 维护记录 |

**每一条 feed 都落两张台账**,无例外 —— `ops.feed_log`(提交前 pending,成功转
submitted)与 `ops.feed_items`(SKU 级,提交成功即落),两者都在
`api/feeds.submit_feed` 内部无条件写,工作流无从绕过。这是所有者问的
"是否有写入数据库"的答案:**是,而且不依赖任何工作流记得写**。

`sku_locked_heal` 那一处"无飞书反哺"是**设计如此**,不是漏:它的账在
`listing.retire_cooldown`,结果由 24h 后的清列动作自证。

⚠ `problem_product_cleanup` 曾经也是这一档(账在 `ops.dispositions`:建议行 →
executing → 按观测落定),**2026-08-24 起不再是**:删除归口到它之后,它也往
`registry.MAINT_SHEET` 写维护记录流水(`maint_sheet.build_row` 造行、
`maint_sheet.publish` 写表,与 `maintenance` 同一对函数;裁剪仍只由
`maintenance` 一处做)。判据全文以 `docs/production_cutover.md` 六·三为准。

`maintenance` 的 price / inventory / MP_MAINTENANCE **回执不进病历**,是所有者
2026-08-07 的定稿(流水已在 `ops.feed_items`),由
`product_events.receipt_in_ledger()` 一处收口,不是各写各的。

⚠ **2026-08-24 起 `maintenance` 不再发 DELETE_ITEM**(所有者定稿):破坏动作
只留 `problem_product_cleanup` 与 `product_clear`(人工通道)两个出口。此前
维护链也能发删除,于是配额、在途防重、病历口径各有一套 —— 同一个 SKU 被两条
链先后删两次是生产实证过的(`api/feeds` 的在途防重按**整批载荷指纹**算,
两条链批次内容不同就撞不上)。维护链照常**建议**删除,领它的换成了问题链。

## 二、闭环的四段链条

```
提交  submit_feed → ops.feed_log(pending→submitted) + ops.feed_items(submitted)
                  → catalog.product_events 的 *_submitted(工作流各自写)
                  → defer_settle=True(目前只有 list_new):不确定的片子当轮
                    什么终态都不写(outcome=deferred),整轮跑完由
                    settle_deferred() 统一「先反查、后补交」,最多
                    SETTLE_ATTEMPTS(=3)轮,落地共用 _ok_result
轮询  feed_poll → feed_track.poll_all → poll_feed
                  → ops.feed_items 落 success/failed/missing + 报错明细
                  → ops.feed_log 落 done/failed
                  → catalog.product_events 的 *_feed_success/failed
投影  feed_poll 的五个反哺器 → 四张飞书表(纯读库,零沃尔玛调用)
核验  catalog_sync → product_events.verify_deletions
                  → delete_verified / delete_not_effective
                  → services.dispositions.settle() 把判决登记到建议行
```

**"不信回执信观测"这条在链上是真的存在的**:`*_feed_success` 只是沃尔玛的
一面之词,删除的最终真相由 `catalog_sync` 下一轮扫店时的缺席/RETIRED 观测判定。
维护三类(标题/价格/库存)没有对应的核验事件,2026-08-16 起由
`dispositions.settle_maintenance()` 比对 `catalog.walmart_items` 现值补上。

## 三、审出来的三个问题

### 1. `pending` 行(所有者定稿 2026-08-16:**不做对账器,遇到了再说**)

> 所有者:「我旧工作流生产了几个月,没遇到过 pending。以后遇到了再说。」
> —— 采纳。本节保留是为了**真遇到时能一眼认出来**,它长得像正常防重。

#### 什么情况下产生

提交 feed 是先落库再调接口:`ops.feed_log` 先写一行 `status='pending'`,
POST 之后按结果改成 submitted / failed。**只有一条路会把它留在 pending**:
POST 网络异常(不知道到没到)→ `find_recent_feed` 反查三态 → 三态里的 UNKNOWN。

⚠ **UNKNOWN 不是"查到了但说不清",是"根本没查成"**(2026-08-16 所有者质疑
"联通了就肯定能查到有还是没有"后逐行核对代码更正 —— 此前本文把它写成前者,
是错的):

| 三态 | 判据(`_probe()` 的返回) | 处置 |
|---|---|---|
| FOUND | GET 通了,列表里有一条同 feedType、条数精确相同、30 分钟窗内、feedId 未被本系统占用的 | 收编,不补交 |
| NOT_FOUND | GET 通了,列表里没有;**30 秒后再查一次仍没有**(防沃尔玛索引滞后) | **当没提交过 → 同一载荷补交一次** |
| UNKNOWN | `_probe()` 返回 None ——**GET 请求自己就没成功**(重试 2 次后仍非 200,或响应不是 JSON) | 保持 pending,不补交 |

所以"没查到就当没提交过"这条规则**代码已经在执行**(NOT_FOUND 那一支)。
UNKNOWN 留 pending 也是对的:`status != 200` 里混着两种东西 —— 超时/连接断
(确实没连上)与 **429 被限流 / 401 token 失效**(连上了,但对方没回答你的问题)。
后者按"没查到 = 没提交过"去补交,如果原来那笔其实到了,就是双删除/双上架。
**"查了,没有" 与 "没查成" 必须分开,这一点不该改。**

⚠ 还有一条路径:**反查时 `get_token` 直接抛异常**(POST 时 token 还拿得到、
POST 之后代理才断)。⚠ **提交侧的同款失败自 2026-08-26 起不再留 pending**:
`_post()` 用 `_PRE_FAIL` 哨兵把 token/代理阶段的异常收成「请求未发出、确定
未达」,直接落 `failed`(outcome=failed + retryable=True,下轮可重占),压根
走不到反查;只有 `StoreDeadError` 仍原样上抛,那种行照旧停 pending。
`_probe()` 里那句
`get_token` 没有 try,异常一路抛出 `find_recent_feed` → `_submit_one`,
`_log_update` 根本没机会执行,行**同样停在 pending** —— 而且连 UNKNOWN
那行日志都不会有,只有工作流单店隔离打的那条"提交异常已跳过"。
代理不稳的店这条路径仍在,但窗口已被 `_PRE_FAIL` 收窄到「POST 拿得到 token、
反查时拿不到」这一小段,不再比 UNKNOWN 更常见。

第三条:进程在"写完 pending 行"与"POST"之间被 kill。

第四条(2026-08-26 #91 新增,目前只有上架链走):**延后结算**。`list_new` 用
`submit_feed(..., defer_settle=True)` 提交,遇 5xx / 网络异常当场什么终态都不写
(outcome=`deferred`,只交回重放句柄),整轮跑完再由 `settle_deferred()` 统一
「先反查、后补交」,最多 `SETTLE_ATTEMPTS`(=3)轮。三种收尾:反查 FOUND 或
补交成功 ⇒ submitted;末轮 NOT_FOUND ⇒ failed;末轮仍 UNKNOWN、**或结算这一步
自己抛异常** ⇒ 行保持 pending,与上面三条同一个下场。

#### 真遇到了长什么样(**认得出来比修得掉更重要**)

后果不止"这批的结局不明",而是**那批 SKU 被堵死**:

```
防重第①层拦的是「在途行」,而 pending 算在途。
下一轮同一批 SKU → payload_key 一样(条目集合的哈希,顺序无关)
                → _log_claim 撞唯一索引 → 既有行 status='pending'
                → 不在 (failed, done) 里 → 不给重占 → 返回 outcome='dedup'
```

于是**每一轮都被拦下**,日志与飞书显示「**在途防重跳过**」—— 看起来完全正常,
实际上那批商品的那个动作**再也发不出去**,而且不报错。
`problem_product_cleanup` 更绕一层:dedup 不转 executing → 建议行停在
suggested → 下轮再建议 → 再被拦 → **无限空转**。

**删除/停用类最容易中招**:feed 没落定说明什么都没变,下一轮扫描算出来还是
同一批 SKU、同一个指纹。改价/改库存因为亚马逊价格在动,指纹会漂,反而不易卡死。

识别信号(**这三条同时出现才是它**,单看第一条会与真防重混淆):
1. 某批 SKU 连着几轮都报「在途防重跳过 N」,N 不变;
2. `ops.feed_log` 里有 `status='pending'` 且 `feed_id IS NULL` 的行;
3. `ops.feed_items` 里那批 SKU **一行都没有**(提交成功才落)。

```sql
SELECT id, workflow, store, feed_type, created_at
FROM ops.feed_log WHERE status = 'pending' ORDER BY created_at;
```

`feed_poll` 的摘要现在会把这几行摊开(店铺 / 类型 / 来源工作流 / 提交时间),
并明说「系统不会自动补交」—— 之前只报个数,人看到之后无从下手。

#### 真遇到了怎么处理(人工,两步)

拿摘要里的店铺 + 时间 + feedType 去 Walmart Seller Center 的 Feed Status 对:

- **那个 feed 在** ⇒ 当时其实到了。给这行补 `feed_id`、`status` 改
  `submitted`,下轮 `feed_poll` 自动接手轮询回执。
- **不在** ⇒ 确认未达。`status` 改 `failed`,下轮业务工作流会重占这个
  payload_key 正常重发。

#### 将来真要做自动对账器的话(不是现在)

判定逻辑**一行都不用新写**:`find_recent_feed` 已经在生产上跑过,GET
`/v3/feeds` 只读、查一百次也没副作用。差的只是**事后调不起来** ——
它的匹配键是 `items_received`(精确条数),而 `ops.feed_log` 没存。

要动的:① `ALTER TABLE ops.feed_log ADD COLUMN item_count int,
ADD COLUMN skus text[]`(后者是收编成功后补 `ops.feed_items` 用的,不然那批
SKU 在台账里仍然没有);② 一个**只读、自己一个 feed 都不发**的工作流,扫
pending 行 → 调 `find_recent_feed` → FOUND 补 feed_id 转 submitted /
NOT_FOUND 转 failed / 仍 UNKNOWN 留着下轮再来。补交仍由原业务工作流按原规则做。

### 2. `dedup` 时重复写 `*_submitted` 事件(已修)

`product_clear` 与 `sku_locked_heal` 在 `outcome in ("submitted", "dedup")` 的
同一个分支里写产品事件。dedup 的含义是**同载荷已在途、这一轮什么都没提交**,
挂的是**旧** feed_id —— 记进去就是幽灵事件。

其余四个提交点(`list_new` / `match_listing` / `maintenance` /
`problem_product_cleanup`)都显式只在 `submitted` 时记,注释里还各自写了理由。
这两处是**同一条纪律的漏网**,不是有意的例外。

有消费方:`catalog.product_risk` 视图直接数
`delete_times` / `retire_times` / `submit_times`。灌水之后"这个 SKU 被删过
几次"就不再是事实。

(`unexplained_missing` 那个布尔**不受影响** —— dedup 说明确实有过一次真提交,
那次已经记过 `*_submitted` 了,所以不会造出假阴性。所以这是计数失真,不是
判断失真,严重度中等。)

修法:把事件写入收进 `if res["outcome"] == "submitted"`。表格列与冷却表照写
不误 —— 在途 feed 的结果**确实**会落到这些行上,而冷却表的 insert 本身
`ON CONFLICT DO NOTHING` 幂等。

### 3. `pending` 行永不老化(与 1 同源,同样不做)

没有任何机制把陈年 pending 行升级告警或归档(曾预留的 `_PENDING_ALARM_HOURS`
常量从未被引用,2026-08-27 死件清理已删)。将来做对账器时一并设计。

## 四、复核过、确认没问题的几处

- **`unknown` 结局不落 `feed_items`** —— 对的。没拿到 feed_id 就没有可挂的
  主键,硬造一行只会让台账里多出一批永远查不到回执的孤儿。
- **重轮询不重复灌回执事件** —— `poll_feed` 先取更新前状态,只对
  `meta[sku][2] == "submitted"`(本轮才落定)的 SKU 记事件。有用例钉着。
- **feed 终态但个别 SKU 仍 processing** —— 不 `mark_feed_done`,下轮重查;
  否则那些 SKU 永久卡 submitted,在途拦截会永远跳过它们。
- **台账有、终态明细里查无的 SKU** → 落 `missing`,不装成功也不装失败。
- **违禁回执自动进 ASIN 黑名单** —— 上架失败反哺"上架前拦截",闭环成立。
- **`problem_scan` 的反补计数**读 `maintenance_submitted` 且限
  `source ∈ {problem_product_cleanup, problem_scan}`。`maintenance` 工作流的
  MP_MAINTENANCE 是**改标题**,不是反补,不该计入 —— 现状正确,别手贱去加。

## 五、一句话结论

所有者担心的"是否有写入数据库":**是,六个提交点无一例外,而且写在 api 层,
工作流绕不过去**。"产品事件是否完善且闭环":提交 → 回执 → 观测核验三段齐全,
两处 dedup 幽灵事件已修。

**唯一没闭的口子是 `pending`**,所有者定稿**不做**(旧系统跑了几个月没遇到过,
遇到了再说)。本轮只补了"遇到时能被发现":`feed_poll` 摘要摊开明细并明说系统
不会自动补交,以及上面第三节那份识别信号 —— 它的麻烦不在于难修,
而在于**长得像正常防重**。
