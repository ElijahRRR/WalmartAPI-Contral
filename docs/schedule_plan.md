# launchd 调度计划(2026-08-16 拟,**待所有者批准**)

> 所有者:「launchd plist 全套并不需要与旧的完全一致,因为我们改造了很多东西。
> 先调研然后出个计划。」
>
> 本文是**计划**,不是定稿。逐条写清「跑什么 / 什么时候 / 为什么是这个时候 /
> 与旧系统差在哪」。旧调度权威清单见 `docs/legacy_schedules.md`(停旧依据),
> 本文只管**新的怎么起**。

## 零、先纠正一条我自己写错的"硬约束"

`production_cutover.md` §七 曾写:

```
catalog_sync → product_refresh → product_ingest → maintenance_scan → maintenance
```

排成一条链,像是"当天闭环"。**实际不是。** `product_refresh` 是把十几万个在架
ASIN **压给采集服务**(docstring 原话:"会给采集器压十几万个任务"),默认
`wait=0` 不等采完;`product_ingest` 拉的是采集服务**已经采完**的增量游标。
十几万个 ASIN 不可能在二十分钟内采完 —— 所以:

> **`product_ingest` 当天摄进来的,是前几天推的那批的产出;
> 今天 `product_refresh` 推的,要等到往后某天才被摄取。**

真正成立的依赖只有三条(其余是"同一天里顺手排在一起",不是数据依赖):

| # | 依赖 | 为什么 |
|---|---|---|
| 1 | `catalog_sync` → `product_refresh` | 要先知道在架哪些 SKU,才知道该推谁去采 |
| 2 | `catalog_sync` → `maintenance_scan` / `problem_scan` | 判据要拿**最新的沃尔玛现值**去比;隔夜的现值会对已下架 SKU 建议改价改库存(生产实证:38 条改库存 + 30 条改价 not found) |
| 3 | `order_sync` → `daily_report`(kpi 阶段) | 否则日报的订单列对拍必差(已知欠账 8 店) |
| 4 | `product_ingest` → `maintenance_scan` / `list_new` | 要先把采集产出摄进中心库,判据才有新数据可读 |

⚠ 第 4 条**不要求同一天的 refresh**:它只要求"摄取排在扫描之前",这样今天
摄进来的东西今天就能用上。

这条纠正的实际影响:**采集链的时间安排比我原来想的宽松得多**,
`product_refresh` 排在哪天几点都不影响当天的维护判据。

## 一、49 个工作流的去向(全表分类)

### A. 进定时调度(15 条)

| 工作流 | 频率 | 危险 | 说明 |
|---|---|---|---|
| `feed_poll` | 每 30 分 | 否 | 全局 feed 轮询 + 五个反哺器;纯读库 + 每个在途 feed 一次 GET |
| `order_sync` `order_audit` `order_center_push` | 每小时(链) | 否 | 所有者定稿:拉单/审核每小时,跑完自动推飞书 |
| `catalog_sync` | 每日 | 否 | 全量扫店;整条产品链的现值源头 |
| `product_refresh` | 每日 | **是** | 推采集(十几万任务)。见零节:它不供当天用 |
| `product_ingest` | 每日 | 否 | 摄取采集增量 |
| `daily_report` | 每日 | 否 | 默认全链 = KPI + 影刀 + 看板 + 真发日报 |
| `perf_problems` | 每日 | 否 | report 端点是全 API 最脆的一族,独立跑,失败不拖日报 |
| `returns_sync` | 每日 | 否 | 售后单 45 天窗口重拉 |
| `problem_scan` `problem_product_cleanup` | 每日(链) | 后者是 | 问题商品链 |
| `maintenance_scan` `maintenance` | 每日(链) | 后者是 | 维护链 |
| `product_clear` | 每日 | **是** | 消费运营填的「停用/删除表」——不定时跑,运营填了没人执行 |
| `sku_locked_heal` | 每日 | **是** | 24h 冷却,每天一轮刚好 |
| `upc_sync` | 每日 | 否 | 只为**回写状态列**;注入已由 `list_new` 自己做 |
| `risk_sync` `blacklist_push` | 每日(链) | 否 | 黑名单双向同步,要在上架/审核之前新鲜 |
| `backup` | 每日 | 否 | pg_dump |
| `settlement_sync` | 双周三 | 否 | 账期双周发布(实证 06/02→06/16→06/30→07/14→07/28) |

### B. 手动,永不进调度(4 条)

| 工作流 | 为什么 |
|---|---|
| `list_new` | 所有者定稿:上架手动运行。它自己会先注入一次 UPC |
| `match_listing` | 跟卖上架,同上 |
| `scrape_missing` | 存量补采,几万个任务,按需一次性 |
| `order_center_cleanup` | docstring 明写"正常只在建库期跑一次" |

### C. 待所有者定(2 条,见第六节问题 4)

`product_audit`(产品审核,有 LLM 成本)、`brand_scrape`(品牌补采)。

### D. 一次性迁移 / 排障工具,不进调度(28 条)

`db_init` `init_data_root` / 各 `*_import`(audit / asin_blacklist / kpi_history /
order_history / cleanup_history / taxonomy)/ `catmap_*` 六条 / `taxonomy_derive`
`node_backfill` `pt_backfill` `sku_normalize` `audit_history_fold`
`audit_calibrate` / `catalog_health` `product_query` `ping_stores`。

## 二、时间表(拟)

生产机时区 **必须是 Asia/Shanghai**(见第六节问题 1)。

| 时间 | `python cli.py` 之后跟什么 | 为什么是这个点 |
|---|---|---|
| 02:00 | `backup` | 离峰;pg_dump 期间不与业务链抢 IO |
| 02:30 | `risk_sync blacklist_push` | 黑名单要在当天所有上架/审核之前新鲜 |
| 05:00 | `catalog_sync` | 整条产品链的现值源头,排最前 |
| 05:40 | `product_refresh` | 依赖 1(要先知道在架哪些);它的产出供**往后几天**用 |
| 06:00 | `product_ingest` | 摄取采集器已完成的增量,供今天的扫描链用(依赖 4) |
| 06:20 | `order_sync order_audit order_center_push` | 依赖 3:必须在日报之前 |
| 06:40 | `daily_report` | KPI 窗口锚在中国时间 06:30,必须 ≥06:35 |
| 07:30 | `perf_problems` | 与日报错开:report 端点多店并发时边缘 520 面状抖动,让它自己失败自己重试 |
| 08:00 | `returns_sync` | 无强依赖,填在读操作的空档 |
| 08:30 | `problem_scan problem_product_cleanup` | 依赖 2(catalog_sync 之后) |
| 09:00 | `upc_sync` | 只回写状态列,放在上架时段之前让人能看到池余量 |
| 12:00 | `maintenance_scan maintenance` | 依赖 2、4;与 08:30 那条链隔 3.5 小时,避免同店 DELETE_ITEM 桶(10/hour)互挤 |
| 15:00 | `product_clear` | 运营上午填表,下午执行(旧系统同款节奏) |
| 23:30 | `sku_locked_heal` | 旧系统同款时间;24h 冷却按天对齐 |
| 每小时 :20 | `order_sync order_audit order_center_push` | 同 06:20 那条,见下面的坑 |
| 每 30 分 | `feed_poll` | feed 落定后越早反哺,飞书上的"处理中"越少 |
| 双周三 08:00 | `settlement_sync` | 下一次 2026-08-26 |

⚠ **06:20 那条与"每小时:20"那条是同一条链,只挂一个 plist**
(`StartCalendarInterval` 给 `Minute=20` 不给 `Hour`,就是每小时的 :20)。
挂两个会在 06:20 撞车:后到的那个整链拿不到锁、退出码 3 空跑一轮。

⚠ **08:30 与 12:00 两条链都发 DELETE_ITEM**。各 feedType 10/hour 且按店计,
隔 3.5 小时足够;但如果哪天把 `maintenance` 提前到 09:xx,同一家店当天删除量
大时会互相挤桶。要改时间先看这一条。

## 三、与旧系统的差异(所有者:不必一致)

| 旧 | 新 | 理由 |
|---|---|---|
| 14 条 AI skill(hermes 平台)+ 6 条 launchd,**两套调度器** | 全部 launchd,**一套** | 旧的 daily_cleanup 调度器至今没定位(`legacy_schedules.md` C 节),两套调度器是"停旧"最难的部分 |
| 08:00 KPI + 14:00 KPI 下午场 | 只留一次 06:40 | 下午场实证参数 bug 从未成功写入,直接不迁 |
| 每时:05 `dedup_sync --store-status-only` + 14:02 全量 14 万 ASIN 推 server cache | **无对应** | 全局去重改为直接查 `catalog.walmart_items`,不再需要外部 cache 与它的同步链 |
| 每时:15 两条并行(`reconcile_hourly` + `order_audit_hourly`) | 合成一条链 :20 | 旧的两条各写各的,新的用 cli 串联 |
| 0/6/12/18 点 `daily_cleanup` 一天四次 | 每天一次 08:30 | 拆成 scan + 执行件之后,建议行有台账;一天四次是"旧的没状态所以靠频率兜"的产物 |
| 12:00 商品维护三段式(sync_lark → submit → poll) | 12:00 `maintenance_scan maintenance`,轮询归全局 `feed_poll` | 数据源已在 PG(sync 消失),回执归全局轮询(poll 消失) |
| 06:00 `autolisting.morning` **无人值守上架** | **不进调度** | 所有者定稿:上架手动 |
| 23:30 `retire_daily` | 23:30 `sku_locked_heal` | 时间照搬,内容是等价物 |

## 四、plist 的形态与五个坑

统一模板(`~/Library/LaunchAgents/com.walmartapi.<label>.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key>            <string>com.walmartapi.order_chain</string>
  <key>ProgramArguments</key>
  <array>
    <string>/绝对路径/venv/bin/python</string>
    <string>/绝对路径/WalmartAPI-Contral/cli.py</string>
    <string>order_sync</string>
    <string>order_audit</string>
    <string>order_center_push</string>
    <string>-p</string><string>order_audit:wait=0</string>
  </array>
  <key>WorkingDirectory</key> <string>/绝对路径/WalmartAPI-Contral</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>WALMART_OPERATOR</key> <string>launchd</string>
    <key>PATH</key> <string>/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin</string>
  </dict>
  <key>StartCalendarInterval</key> <dict><key>Minute</key><integer>20</integer></dict>
  <key>StandardOutPath</key>  <string>/绝对路径/WalmartAPI_data/logs/launchd/order_chain.out</string>
  <key>StandardErrorPath</key><string>/绝对路径/WalmartAPI_data/logs/launchd/order_chain.err</string>
  <key>RunAtLoad</key>        <false/>
</dict></plist>
```

**坑 1 — `ProgramArguments` 不过 shell。** 没有 `&&`、没有 `~`、没有通配符、
没有变量展开。串联必须用 cli 的多工作流参数(2026-08-16 加的),这也是当初把
串联做进 cli 而不是让工作流互相调用的原因之一。

**坑 2 — launchd 不读 shell 配置。** `~/.zshrc` 里的 `PATH`、`PGHOST`、
任何 `export` 一概不生效。密钥走 `<DATA_ROOT>/.env`(cli 自己加载),
但 `PATH` 要显式给 —— `pg_dump`(backup 用)不在默认 PATH 里就会报"命令未找到"。
`WALMART_DATA_ROOT` 只在非默认路径时才需要写。

**坑 3 — `StandardOutPath` 不能省,即使 cli 已经写 `logs/<workflow>.log`。**
两者盖的不是同一段:cli 的日志从"Python 起来了"才开始;而
**解释器路径写错 / venv 被删 / import 期就炸**这类错误发生在那之前,
只会出现在 launchd 的 stdout/stderr 文件里。没有它,故障表现是
"这条链每天什么都不做,日志里一个字也没有"。

**坑 4 — 机器睡眠/关机。** `StartCalendarInterval` 错过的点在唤醒后**补跑一次**
(不是把错过的每一次都补)。对每日链是好兜底;对每小时链意味着唤醒瞬间立刻跑
一次,可接受。⚠ 但如果那台 Mac 会关机过夜,02:00~05:00 那几条(backup /
risk_sync / catalog_sync)会全部堆在开机那一刻抢跑 —— 见第六节问题 2。

**坑 5 — 时区。** launchd 按**本机时区**解释 `StartCalendarInterval`,
而 KPI 窗口、日界、账期在代码里一律按 `Asia/Shanghai`(`services/kpi.CN_TZ`)。
机器要是 UTC,06:40 本地 = 中国时间 14:40,整张表全错而且**不报错**
—— 日报照跑,只是拉的窗口不对。

## 五、上线顺序(分四批灰度,不要一天全开)

破坏性从低到高,每批观察满一天再开下一批。**每批开之前先停对应的旧调度**
(`legacy_schedules.md` 有逐条的"停旧动作")。

| 批 | 开什么 | 先停什么旧的 | 观察什么 |
|---|---|---|---|
| **一(只读)** | `backup` `risk_sync blacklist_push` `catalog_sync` `product_ingest` `feed_poll` `catalog_health` | 无(都不冲突) | `ops.runs` 里的时长与失败率;`feed_poll` 摘要有没有 pending |
| **二(读+推送)** | `order_sync order_audit order_center_push`、`returns_sync`、`perf_problems`、`settlement_sync` | A 表 13:30 订单同步 + B 表每时:15 `order_audit_hourly`(**两条同停**,双调度) | 订单列对拍差异 8 店是否收敛 |
| **三(日报,含影刀)** | `daily_report` | **A 表 08:00 `walmart-kpi-daily`(必须先停)** + 14:00 下午场 | 影刀有没有数据 —— 双 spawn 互抢**不报错**,只表现为"影刀没数据",是这条链最难查的故障 |
| **四(破坏性)** | `problem_scan problem_product_cleanup`、`maintenance_scan maintenance`、`product_clear`、`sku_locked_heal`、`product_refresh`、`upc_sync` | A 表 12:00 维护、15:00 下架、0/6/12/18 cleanup;B 表 23:30 retire | 每条**第一次都先手动 `--dry-run` 看一眼**再挂 plist |

⚠ 批四的每一条,挂 plist 之前必须先手动跑一次 `--dry-run` 并人眼确认输出
(安全铁律那条纪律没取消,只是默认值不再替你挡)。尤其
`maintenance_scan -p preview=1` 里「标题不匹配」那类删除的条数 —— 那是五条
删除判据里唯一没有生产数据背书的一条。

## 六、需要所有者拍板的六个问题

1. **生产 Mac 的时区是不是 `Asia/Shanghai`?**(`date +%Z` 一看便知)
   不是的话整张表要换算,而且错了不报错。
2. **那台机器会不会关机/休眠过夜?** 会的话 02:00~05:00 那几条要改成开机后
   顺序错开,或者改用 `StartInterval`。
3. **Python 解释器的绝对路径?** 仓库里没有 venv 约定。plist 必须写死绝对路径。
4. **`product_audit` 与 `brand_scrape` 进不进调度?**
   `product_audit` 有 LLM 成本且是 DANGEROUS;上架既然是手动的,审核跟着手动
   也说得通。倾向:先不进,手动跑,观察一段。
5. **`order_audit` 挂 `-p wait=0` 还是用默认?**
   默认 `wait=1` 会阻塞到 20 分钟等采集落定 —— 每小时跑一次的话,一小时里有
   三分之一时间占着锁。`wait=0` 则结论滞后一轮(下一小时自然收敛)。
   倾向:**每小时那条写 `-p order_audit:wait=0`**,靠频率收敛。
6. **`product_refresh` 每天全量重推十几万个采集任务,采集器扛得住吗?**
   要是采集器一天采不完十几万,每天推等于队列越堆越长。可选:隔天推、
   或按"最久没采过"分批推。这条要你按采集器的实际吞吐定。

## 七、还差的一件事

`FEISHU_WEBHOOK_URL` 至今未配置 —— **起调度之前必须配好**。
不然全部 15 条链的成功/失败通知一条都发不出去,等于起了一套没有仪表盘的调度。
(日报正文另有 webhook 依赖,同一个变量。)
