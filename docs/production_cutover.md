# 走进生产:所有者定稿与待办(2026-08-16 起)

> 本文件是「起调度进生产」这一役的唯一交接底稿。所有者 2026-08-16 一次性下了
> 一批产品级定稿,分散在对话里容易丢,全部收录于此。**新会话接手先读本文件。**
> 逐条都标了「已落地 / 待做」,已落地的写清落在哪个文件,待做的写清依赖与坑。

## 一、执行语义(已落地)

**缺省即真跑,空跑用 `--dry-run`。** 与旧铁律相反(旧:危险工作流缺省 dry-run,
真跑加 `--execute`)。理由:进了调度之后,"缺省 dry-run"这条防线只会伤到自己
—— launchd 里漏写一个 `--execute` 的后果是**那条链每天空转而且报成功**,
比误跑更难发现(误跑至少留下痕迹)。

`--execute` 保留为兼容别名(空操作),调度里写了也不会错。

⚠ **「AI 改完代码必须先 dry-run,人眼确认输出后才跑真的」没有取消**,只是从
"默认值兜底"降级成"纪律"。默认值不再替你挡,所以更要自觉。

落地:`cli.py`(`--dry-run` 参数 + 分发处),`CLAUDE.md` 安全铁律那节。

## 二、并发(已落地)

**店铺级并发 6 → 16**(`workflows/daily_report._STORE_WORKERS`)。

⚠ 旧系统 README 曾写"店铺级并发不要调高(代理共享/全局风控)",这条被显式覆盖。
支撑理由:每店有**自己的固定出口代理**,店铺之间不共享出口;沃尔玛配额按
`(store, endpoint)` 计,`api/_client.py` 的令牌桶也按这个维度限流,所以加店铺
并发不会挤占同一个桶。真正的共享资源只有本机 CPU/连接数。
**若出现大面积 429 或代理超时,先降这个数再查别的。**

"店内不同接口并发"本来就在做(8 项绩效指标并发拉,各端点桶独立)。

## 三、日报(已落地)

**默认全链**:`python cli.py daily_report` = kpi(含影刀)+ 看板 + **真发日报**。
要关:`-p yingdao=0` / `-p push=0`。

三个默认值改的是同一类风险:调度里漏写一个开关,那一段每天空转而且报成功。

⚠ 相关:`FEISHU_WEBHOOK_URL` 未配置时摘要**不许说"日报已推送"** ——
2026-08-16 所有者实见日志三处写着未配置、摘要却报成功。现在报
「⚠ 日报**未发出**」并附正文(人能手动转发)。

## 四、配送时长限制(已落地)

限额表(`registry.RETIRE_LIMITS`)新增列 **`配送时长限制`**(列名一字不差,
飞书按表头索引)。读取收在 `services/store_limits.py`。

| 消费方 | 超限动作 |
|---|---|
| **上架** `list_new` | **不上架**(此前是"上架但库存写 0")。不上架就不占 UPC、不占配额 |
| **维护** `maintenance` | **库存写 0**(它已经在架了,只能压库存) |

查不到该店 ⇒ 回落 `services.amz_source.MAX_LEAD_DAYS`(8 天)。
「填了 0」与「没填」同样视同没配(上限 0 天 = 这条链整店停摆,不像人的本意)。

⚠ **没采到(None)不算超限**:`or 0` 会把"未知"读成"当天达",方向反了,
而两侧都不报错(上架侧照常上一批未知货期的,维护侧把它们全清零)。
两侧共用 `over_lead_cap`/`cap_for`,有用例钉住 —— 各写各的迟早飘成
"上架按 8 天拦、维护按 12 天清零"这种自相矛盾。

## 五、在线商品处置判据(判据已落地,provider 接线待做)

所有者给的伪代码,逐条落在 `services.maintenance_intents.classify()` ——
**全项目唯一一处**决定"这个在线商品该拿它怎么办"。

| 判据 | 动作 | 原因码 |
|---|---|---|
| `outcome == 'not_found'` | **删除**(ASIN 已从亚马逊下架) | `not_found` |
| `stock_status == 'Currently unavailable'` | 库存 0(在架但不可售) | `unavailable` |
| `stock_status == 'No Featured Offer'` | 库存 0(无 Buy Box) | `no_buybox` |
| `stock_state == 'out_of_stock'` | 库存 0(普通缺货) | `out_of_stock` |
| 配送超本店上限 | 库存 0 | `lead_days` |
| 标题相似度 **< 70%** | **删除** | `title_mismatch` |
| 标题相似度 **≥ 70%** 且有差异 | 改标题 | (title_intents) |

三个信号(`outcome` / `stock_status` / `stock_state`)取自**同一条最新快照**
(SQL 一次取出),不各查各的 —— 分开查会出现"按昨天的 outcome 配今天的库存"
这种错配。`stock_status` 在 `snapshots.raw` 顶层,契约未列为一等字段。

⚠ **删除压过一切**(`pick_one()`):一个 SKU 一轮只出一个动作。批次 E 踩过
同款坑(同一 SKU 既建议反补又建议删除,执行件先花配额救活再花配额删掉);
这里是先花配额改一个马上要删的商品。

⚠ **相似度 `None` 不算不匹配**:算不出来 ≠ 不像。走到这一步说明标题缺失,
那是采集问题;拿它当删除依据 = 采集抖动一次就删一批在架商品,而且不报错。
`0.0` 才是明确的不像。

## 六、维护记录表 11 列(已落地)

所有者 2026-08-16 在飞书加了两列,现为:

```
店铺  SKU  建议  原因  动作  旧值  新值  feedid  日期  结果  报错
```

「原因」不是装饰:四条清零判据在表里长得**一模一样**(库存 12 → 0),没有原因列
分不出是哪一条触发的;删除那类更要紧(`not_found` 与 `标题相似度 42%` 都是删除,
但正确性判断完全不同)。

⚠ `services/maint_sheet` 的列字母**全部从 `registry.MAINT_SHEET.columns` 的
下标推导**,不再硬编码 A/H/I。硬编码那版在加列后会整表错位且不报错;
`resync_from_ledger` 里写死的 `cells[5]` 更隐蔽 —— 取错列会让每轮把全部行
当"表里没有"重复补写。`workflows/maintenance._record()` 是唯一造行处。

「建议」与「动作」当前**恒等**(maintenance 还没拆)。拆完之后两者分歧才是
那一列的价值:

| 建议 | 动作 | 含义 |
|---|---|---|
| 删除 | 删除 | 正常执行 |
| 删除 | *(空)* | 建议了还没执行(配额满 / 没跑执行件) |
| 删除 | 跳过 | 在途防重命中 |
| 删除 | 撤销 | 商品自己恢复正常,建议作废 |

## 七、待做清单(按依赖顺序)

### 1. provider 接 `classify()`(三·下)

`inventory_intents` / `title_intents` / `delete_intents` 三个 provider 改成问
`classify()`,`title_intents` 加 70% 闸(低于阈值的交给删除,不再改标题)。
判据已就位,只是接线。

⚠ 接线时注意 `_SQL_AMZ_JOIN` 已补 `outcome/stock_status/stock_state` 三列,
`_rows()` 的解包元组长度随之变了 —— 所有解包处都要同步(现有 provider 用的是
位置解包,漏改一处会静默取错字段)。

### 2. maintenance 拆成 scan + 执行件(批次四)

照 `problem_scan` / `problem_product_cleanup` 的形态,走 `ops.dispositions` 串联:

```
maintenance_scan  (只读,产建议行,DANGEROUS=False)
      ↓ ops.dispositions
maintenance       (纯执行件,只消费建议行)
```

现成可抄的:`services/dispositions.py` 的
`suggest_many / withdraw_stale / claim / mark_executing / settle` 全套状态机
(`suggested → executing → confirmed/ineffective/withdrawn`,部分唯一索引只约束
未落定态)。批次 E 拆 `problem_product_cleanup` 时踩过的四个坑照单全收:
同一 SKU 出两个冲突动作 / 建议无撤销机制 / 单店扫描清空全库建议 / 摘要报数
与执行件对不上。

⚠ 铁律:workflow 之间不许互相 import。串联走调度,不走代码。

### 3. 订单链跑完自动推飞书

所有者要求:拉单/审核(每小时)、售后/绩效(每日)、对账(双周三)每个跑完
自动同步飞书,不要人手动推。

⚠ 不能让 `order_sync` import `order_center_push`(铁律:任何层不准 import
workflows)。**走调度串联**,在 plist 里把两条命令串起来。

### 4. 上架先同步 UPC

`list_new` 运行时先自动同步一次 UPC。同样不能 import workflow ——
要把 `upc_sync` 的核心抽到 services 层给 `list_new` 调。
上架**不进定时调度**,手动运行。

### 5. feed 闭环审计(所有者要的验证)

所有者担心的不是飞书侧(他确认表格侧完整),是**库侧**:「是否有写入数据库,
产品事件是否完善且闭环」。要做的是把每个提交 feed 的动作、它落的
`ops.feed_log` / `ops.feed_items` / `catalog.product_events` 行、以及
`feed_poll` 五个反哺器的覆盖面对一遍,缺口列出来。这是审计任务,不用连库。

### 6. launchd plist 全套 + 停旧清单

调度顺序的**硬约束**(文档里明写、颠倒会静默出错):

1. `catalog_sync → product_refresh → product_ingest → maintenance/list_new`
2. `catalog_sync → problem_scan → problem_product_cleanup`
3. `order_sync` 必须在 `daily_report -p phase=kpi` 之前(否则订单列对拍必差)

时间表(所有者定稿的节奏;KPI 窗口锚在中国时间 06:30,故日报 ≥06:35):

| 时间 | 命令 |
|---|---|
| 05:30 | `catalog_sync` |
| 05:50 | `product_refresh` |
| 06:10 | `product_ingest` |
| 06:20 | `order_sync` → `order_audit` → `order_center_push` |
| 06:40 | `daily_report`(默认全链) |
| 07:10 | `perf_problems` / `returns_sync` |
| 08:00 | `problem_scan` → `problem_product_cleanup` |
| 12:00 | `maintenance_scan` → `maintenance` |
| 每小时 | `order_sync` → `order_audit` → `order_center_push` |
| 每 30 分 | `feed_poll` |
| 双周三 | `settlement_sync`(下一次 2026-08-26) |
| 02:00 | `backup` / `risk_sync` / `blacklist_push` |
| 手动 | `list_new`(不进调度) |

⚠ **停旧清单见 `docs/legacy_schedules.md`**。最要紧一条:开
`daily_report`(默认已含影刀)之前必须先停旧 `walmart-kpi-daily` ——
双 spawn 互抢会让新鲜度校验反复失败到超时,**不报错**,只表现为
"影刀没数据",是这条链最难查的故障。

## 八、生产侧欠账(与本役并行)

- `FEISHU_WEBHOOK_URL` 未配置 ⇒ 日报发不出去
- `谭总10` 采集失败(非凭证类,日志有堆栈),既不入库也不进影刀清单
- 订单列对拍差异 8 店(多半是 `order_sync` 没跑在 `daily_report` 之前)
- 批次 E 真跑未执行(`problem_scan` → `problem_product_cleanup`,470 条建议)
- 变体分组生产验收未做(`list_new` dry-run 看变体口径分布,重点看 `no_dim`
  —— `_DIM_MAP` 那 20 行映射未经生产校验)
