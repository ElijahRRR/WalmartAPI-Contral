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

## 五、在线商品处置判据(已落地)

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

拆分之后(批次四)两列分歧才是那一列的价值:

| 建议 | 动作 | 结果 | 含义 |
|---|---|---|---|
| 删除(not_found) | 删除 | 处理中 | 正常执行 |
| 删除(not_found) | 跳过 | 在途防重 | 上一轮的 feed 还在途 |
| 删除(not_found) | *(空)* | 未执行(凭证缺失/凭证失效/提交异常) | 领到了没做成 |

⚠ 「领到了没做成」的行**必须也写表**:不写的话它在飞书完全不可见,看起来
像"扫描件没建议它",而它其实每天都在建议、每天都没做成。

「撤销」不进表:它是 `maintenance_scan` 发现商品自己恢复正常后把建议行置
withdrawn,那一轮执行件根本没领到它 —— 要看撤销走
`SELECT * FROM ops.dispositions WHERE status='withdrawn'`。

## 六·二、维护链拆分(批次四,已落地)

```
maintenance_scan  (DANGEROUS=False,只读,产建议行)
      ↓ ops.dispositions(source='maint')
maintenance       (DANGEROUS=True,纯执行件,只消费建议行)
```

搬家清单(拆分不是复制,原来住在 workflow 里的东西按层归位):

| 原位置 | 新位置 | 为什么 |
|---|---|---|
| `maintenance.collect_intents` | `services.maintenance_intents.collect_all` | 铁律 1:workflow 不许互相 import,而两侧口径必须是同一份代码 |
| `maintenance._load_stockzero` | `services.store_limits.stockzero_stores` | 同上;那张限额表已经有一个消费方模块了 |
| `maintenance._load_multipliers` | `services.store_limits.price_multipliers` | 同上 |
| `maintenance._load_delete_caps` | `services.store_limits.retire_caps` | 同上 |
| 破坏面明细(删除名单/清零原因/改价分布) | `maintenance_scan._preview_lines` | 决策在哪边,"为什么是这些商品"就该在哪边说 |

**两条链共用一张 `ops.dispositions`**,交界处三条纪律(写在
`services/dispositions.py` 头注,有用例钉着):

1. `claim(sources=…)` **必须传**。不传就领到对方的建议行 ——
   维护链的 `price` 落进 `problem_product_cleanup.group_by_store` 会直接抛。
2. 部分唯一索引 `(store, sku, action)` **跨来源**。同一 (店铺,SKU) 被两条链
   同时建议 `delete` 时合成一条,source 保留先落库那一方,于是只有那条链的
   执行件去删它。结果仍是"删一次",可接受;但 reason/category 会被后写的
   一方覆盖,查"当初为什么删"要两条链的日志一起看。
3. `withdraw_stale` / `count_open` 各撤各的、各数各的。

⚠ **超期放行(`expire_executing`,3 天)不是洁癖,是防死锁。** 部分唯一索引
只允许同 (店铺,SKU,动作) 有一条未落定行 —— executing 行永远不落定 =
那个 SKU 的那类维护**永久停摆且完全静默**(扫描件照常算出意图,upsert 撞索引
写不进去,rowcount 0)。等不到观测的常见原因:商品下架了
(`walmart_items` 缺席,JOIN 不上)、`catalog_sync` 连着几轮没扫到那家店。

维护三类的"生效"没有对应的核验事件(`catalog_sync` 只是把新值扫回
`catalog.walmart_items`,没人为"改价生效了"记一条事件),所以
`settle_maintenance()` 的判据就是最朴素那条:**重新观测之后线上的值是不是
我们要的值**。比对写在 Python 里不写进 SQL —— `(detail->>'new')::numeric`
遇到一条脏 detail 会炸掉**整条 UPDATE**(不是跳过那一行,是整轮落定失败)。

## 七、待做清单(按依赖顺序)

### 1. 订单链跑完自动推飞书

所有者要求:拉单/审核(每小时)、售后/绩效(每日)、对账(双周三)每个跑完
自动同步飞书,不要人手动推。

⚠ 不能让 `order_sync` import `order_center_push`(铁律:任何层不准 import
workflows)。**走调度串联**,在 plist 里把两条命令串起来。

### 2. 上架先同步 UPC

`list_new` 运行时先自动同步一次 UPC。同样不能 import workflow ——
要把 `upc_sync` 的核心抽到 services 层给 `list_new` 调。
上架**不进定时调度**,手动运行。

### 3. feed 闭环审计(所有者要的验证)

所有者担心的不是飞书侧(他确认表格侧完整),是**库侧**:「是否有写入数据库,
产品事件是否完善且闭环」。要做的是把每个提交 feed 的动作、它落的
`ops.feed_log` / `ops.feed_items` / `catalog.product_events` 行、以及
`feed_poll` 五个反哺器的覆盖面对一遍,缺口列出来。这是审计任务,不用连库。

### 4. launchd plist 全套 + 停旧清单

调度顺序的**硬约束**(文档里明写、颠倒会静默出错):

1. `catalog_sync → product_refresh → product_ingest → maintenance_scan → maintenance`
   (`list_new` 同样排在 `product_ingest` 之后,但它不进调度)
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
