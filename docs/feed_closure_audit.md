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
| `problem_product_cleanup` | DELETE_ITEM / RETIRE_ITEM / MP_MAINTENANCE | problem_product_cleanup | 三种 `*_submitted` | 三种 `*_feed_*` | 无(建议表自管)|
| `maintenance` | DELETE_ITEM / MP_MAINTENANCE / price / inventory | maintenance | 仅 `delete_submitted` | 仅 `delete_feed_*` | 维护记录 |

**每一条 feed 都落两张台账**,无例外 —— `ops.feed_log`(提交前 pending,成功转
submitted)与 `ops.feed_items`(SKU 级,提交成功即落),两者都在
`api/feeds.submit_feed` 内部无条件写,工作流无从绕过。这是所有者问的
"是否有写入数据库"的答案:**是,而且不依赖任何工作流记得写**。

两处"无飞书反哺"是**设计如此**,不是漏:
- `problem_product_cleanup` 的账在 `ops.dispositions`(建议行 → executing →
  按观测落定),不需要投影到表格;
- `sku_locked_heal` 的账在 `listing.retire_cooldown`,结果由 24h 后的清列动作
  自证。

`maintenance` 的 price / inventory / MP_MAINTENANCE **回执不进病历**,是所有者
2026-08-07 的定稿(流水已在 `ops.feed_items`),由
`product_events.receipt_in_ledger()` 一处收口,不是各写各的。

## 二、闭环的四段链条

```
提交  submit_feed → ops.feed_log(pending→submitted) + ops.feed_items(submitted)
                  → catalog.product_events 的 *_submitted(工作流各自写)
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

### 1. `pending` 行是一条**断头路**(最要紧,未修)

`ops.feed_log` 的 schema 注释白纸黑字写着:

```sql
-- 启动对账:凡 status='pending'/'submitted' 的行,先查 Walmart 实际 feed 状态再决定补交
```

安全铁律也写着「程序重启时,所有 pending 记录先去 Walmart 查实际状态再决定
是否补交」。**这件事没有任何代码在做。**

- `feeds.query_pending()` 的唯一消费方是 `feed_track.poll_all`,而它对 pending
  行只打一行 warning,不解析、不补交、不关闭。
- `find_recent_feed()`(反查三态)只在 `_submit_one` 内部、提交出网络异常的那
  一瞬间被调用;事后再没有任何路径能用它。

后果:一条 pending 行**永远挂着**。它的 SKU 在 `ops.feed_items` 里**一行都
没有**(`_items_record` 只在提交成功时调),飞书对应行停在 Unknown / 处理中,
而计数只增不减 —— 几轮之后 `⚠ pending N` 这行警告就成了背景噪音。

⚠ **就算现在想写这个对账器,也写不了**:`find_recent_feed` 的必需入参是
`items_received`(按条目数 + 时间窗匹配候选 feed),而 `ops.feed_log`
**没存条目数**,也**没存 SKU 列表**(收编成功后要拿它去落 `feed_items`)。
表里只有 `payload_key` 这个指纹,反推不出条目。

**本轮做的**:把 pending 明细(店铺 / feedType / 来源工作流 / 提交时间)摊进
`feed_poll` 的**摘要**——摘要是发去飞书的那一份,只报个数人看到之后无从下手。
并且明说「系统不会自动补交」。

**没做的**:自动对账器。它要 ① `ALTER TABLE ops.feed_log ADD COLUMN
item_count int, ADD COLUMN skus text[]`;② 一个新工作流按
`find_recent_feed` 三态收编或判未达。这是一个批次的量,而且它碰的是全项目最
危险的那条不变式(重复提交),**要所有者点头再动**。

在那之前,pending 的正确处置是**人工**:拿摘要里的店铺 + 时间去 Walmart 后台
对一眼那个 feed 在不在,在就手工把 `ops.feed_log` 那行补上 feed_id 改
submitted(下轮 `feed_poll` 会接手),不在就改 failed(下轮业务工作流重提)。
`pending` 罕见 —— 只在"网络异常 **且** 反查三态也不确定"时产生。

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

### 3. `pending` 行永不老化(未修,与 1 同源)

没有任何机制把陈年 pending 行升级告警或归档。`_PENDING_ALARM_HOURS = 6` 这个
常量**定义了但没有任何地方引用**。做对账器时一并处理。

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

**唯一真正没闭的口子是 `pending`** —— 它按设计"宁停不重"停在那里等人,但
既没人来、也没留下让人来的信息。本轮补上了信息(摘要里摊开明细),
自动对账器等所有者点头。
