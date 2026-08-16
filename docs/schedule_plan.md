# 调度计划 v4(2026-08-16,三轮批复后;**输入齐了,四件代码活已做三件**)

> v1 是我按旧系统形态推的;所有者 2026-08-16 给了七条批复,**结构按他的四条业务线
> 重排**,并答掉了六个开放问题中的五个。本文是 v2。
> 旧调度权威清单见 `docs/legacy_schedules.md`(停旧依据)。

## 零、所有者批复(逐条落地位置)

| # | 批复 | 落地 |
|---|---|---|
| 1 | 台湾时间 | ✅ **不用换算**:台北与上海同为 UTC+8 且都无夏令时,代码里的 `CN_TZ`(Asia/Shanghai)与机器本地时间**日界一致**。时间表原样可用 |
| 2 | 不关机 | ✅ 睡眠补跑那条坑作废;`StartCalendarInterval` 直接可用 |
| 3 | 虚拟环境跑 | ⚠ 仍需要**那个 venv 的 python 绝对路径**(见第六节) |
| 4 | `product_audit` / `brand_scrape` 不进调度 | ✅ 划入手动 |
| 5 | 「等待会占锁吗?什么锁」 | 见第五节(会,`<DATA_ROOT>/locks/order_audit.lock`);结论:**保持默认 `wait=1`**,不写 `wait=0` |
| 6 | 采集器 3000/分钟扛得住 | ✅ **改变了 v1 的结论**:全量重推约 50 分钟采完,产品线**能一次性做完**(见第一节) |
| 7 | 没有 hermes,现在是 GPT 调度;每小时订单/审核是 cron;feed 轮询也照此 | ✅ 全部走 launchd/cron,一律不接 GPT。⚠ `legacy_schedules.md` A 表写的"hermes 平台"已过时,停旧取证时按 GPT 侧的实际注册表核对 |
| 8 | python 路径 `/Users/nextderboy/Projects/WalmartAPI-Contral/.venv/bin/python3` | ✅ 全部 plist 的解释器 |
| 9 | `sku_locked_heal` **暂不挂调度** | ✅ 从时间表移除,划入手动 |
| 10 | 飞书接收人 `17882211182`(手机号) | ✅ 已实现自动换 open_id;⚠ 要应用有 `contact:user.id:readonly` 权限,见第七节 C |

## 一、v1 的一处判断被批复 6 推翻(重要)

v1 §零 我写:"十几万个 ASIN 不可能二十分钟采完,所以 `product_ingest` 摄的是
前几天推的那批"。**按 3000/分钟,~15 万个 ASIN 约 50 分钟采完** ——
`product_refresh` 自己的摘要就按 2500/分钟估算并打印预计时间,
`TIMEOUT_HOURS = 1` 也正是照这个量级定的。

所以所有者说的「产品线这些我认为可以一次性做完」**成立**,一条链:

```
catalog_sync → product_refresh → (等采完) → product_ingest
             → maintenance_scan → maintenance
             → problem_scan → problem_product_cleanup
```

### ⚠ 但有个前提:`product_refresh` 的 `-p wait=1` **根本没实现**

它的用法行写着 `-p wait=1  # 真推后阻塞等采完(默认不等)`,而 `run()` 里
**从头到尾没有读过 `params["wait"]`** —— 传了等于没传,推完就返回。
(对照:`order_audit` 的 `wait` 是真实现了的。)

不修的话这条链会退化成:推完立刻跑 `product_ingest`,而那时采集器刚开始干活,
摄回来的还是上一轮的数据 —— **而且不报错**,摘要看起来一切正常。

**这是起这条链之前必须先补的代码**(见第七节工作项 A)。

## 二、四条业务线(按所有者的划分)

### 线 1 · 产品维护线 —— 每日一次,一条链跑完

```
python cli.py catalog_sync product_refresh product_ingest \
              maintenance_scan maintenance problem_scan problem_product_cleanup \
              -p product_refresh:wait=1
```

| 步 | 干什么 | 预估时长 |
|---|---|---|
| `catalog_sync` | 48 店全量扫在架商品 + 库存 | ? 待实测 |
| `product_refresh` | 推 ~15 万 ASIN 给采集器,**等采完** | ~50 分钟 |
| `product_ingest` | 把采集产出摄进产品中心 | ? |
| `maintenance_scan` | 定性,落建议行 | ? |
| `maintenance` | 提交 feed(改标题/价/库存/删除) | ? |
| `problem_scan` → `problem_product_cleanup` | 问题商品链 | ? |

整条估计 **2 小时以上**。建议 **09:00 起跑**(日报 06:40 之后,避开订单链整点)。

⚠ 一条链里前一步失败就停后面的(cli 串联语义)。对这条链是**对的**:
`catalog_sync` 没跑成,后面的判据就是拿隔夜现值去比,会对已下架 SKU 建议改价改
库存(生产实证 38 条改库存 + 30 条改价 not found)。

⚠ `product_refresh` 是 DANGEROUS,缺省即真跑,plist 里**不要**写 `--dry-run`。

### 线 2 · 订单线

| 频率 | 命令 |
|---|---|
| **每小时 :20** | `order_sync order_audit returns_sync` |
| **每日 07:30** | `perf_problems` |
| **双周三 08:00** | `settlement_sync`(下一次 2026-08-26) |

⚠ `returns_sync` 每小时跑是所有者定的(「每小时的订单拉取、订单审核、售后订单
拉取」)。它是 **45 天窗口全量重拉 × 48 店**,比 `order_sync` 更重。
起了之后先看 `ops.runs` 的实测时长 —— 如果一轮超过 40 分钟,建议降到 2~4 小时
一次(售后状态 2~4 周才走到终态,小时级刷新的信息增量很小)。

⚠ 日报依赖订单(依赖 3):06:20 那一轮必须成功,`daily_report` 才有正确的订单列。
**06:20 就是每小时那条,不另挂**。

### 线 3 · KPI 日报 —— 每日一次

```
06:40  python cli.py daily_report
```

KPI 窗口锚在 06:30,必须 ≥06:35。默认全链 = KPI + 影刀 + 看板 + 真发日报。

⚠ **开它之前必须先停旧的 KPI 调度**(旧 `walmart-kpi-daily`,现在挂在 GPT 侧)。
双 spawn 互抢会让影刀新鲜度校验反复失败到超时 —— **不报错**,只表现为
"影刀没数据",是这条链最难查的故障。

### 线 4 · 黑名单中心 —— 每日一次

```
02:30  python cli.py risk_sync blacklist_push
```

排在所有上架/审核之前,当天的黑名单是新鲜的。

### 基础设施(不属四条线,但必须有)

| 频率 | 命令 | 说明 |
|---|---|---|
| 每 30 分 | `feed_poll` | 所有者:与订单链同款,cron 定时,不接 GPT |
| 每日 02:00 | `backup` | pg_dump,离峰 |

## 三、其余四条(所有者 2026-08-16 批复已定)

| 工作流 | 定稿 | 说明 |
|---|---|---|
| `product_clear` | **每日 15:00 进调度** | 消费运营填的「停用/删除表」;不定时跑 = 运营填了没人执行(旧系统同款时间) |
| `sku_locked_heal` | **暂不挂调度**(所有者定稿) | 手动跑。见下面「它是什么」——不跑的话撞上 SKU_LOCKED 的行会在上架表里积压,上架频率不高时手动清即可 |
| `upc_sync` | **不进调度**,并进 `list_new` | 所有者:「放到上架里,上架前执行一次就好了」 |
| `catalog_health` | 不进调度 | 纯 SQL 只读体检,想看时手动跑 |

### `sku_locked_heal` 是什么(所有者问)

沃尔玛报 `ERR_EXT_DATA_0101211` = **这个 SKU 已经和旧 UPC 绑死了**。
旧实证(`legacy_survey.md:1667`):**不先退役、直接换个新 UPC 重发同一个 SKU
也会失败**。所以它不是"永久放弃",而是要走三步:

```
① 对上架表 O=SKU_LOCKED 的行提交 RETIRE_ITEM(退役旧 offer)
② 冷却 24 小时(listing.retire_cooldown,状态权威在库)
③ 回执成功 + 冷却期满 → 清列(K~M / O~Q 清空,N 写自愈标记)
   → 那一行变成"新行",下一轮 list_new 按正常闸门链领**新 UPC** 重上
```

旧系统是拆开的:launchd `retire_daily` 23:30 干 ①,清列重上在
`retire_and_relist`。新系统合成一个工作流,**每天跑一次即可**
—— ① 和 ③ 在同一轮里各自推进(今天退役的,明天这一轮才够 24 小时被清列)。

所以它服务的是**上架链的自愈**:没有它,撞上 SKU_LOCKED 的行就永久卡在上架表里。
上架虽然手动,这条清障**所有者定为暂不挂调度**,手动跑。⚠ 代价是撞上 SKU_LOCKED
的行会一直躺在上架表里(不会自己好),上架前顺手跑一次即可 ——
`python cli.py sku_locked_heal --dry-run` 先看,再去掉 --dry-run。

### `upc_sync` 并进 `list_new` 的做法(有个建议)

所有者要的效果是「不用单独调度它」。`upc_sync` 其实是两件事,建议**分开摆**:

| 步 | 函数 | 放哪 | 为什么 |
|---|---|---|---|
| 注入(表格新号 → `catalog.upc_pool`) | `upc_pool.sync_from_sheet` | 上架**之前**(已落地) | 运营刚贴进表格的号,这一轮就要能领 |
| 回写(PG 权威 → 表格 C~F 状态列) | `upc_pool.project_to_sheet` | 上架**之后**(待做) | 放前面回写的是**上一轮**的状态;放后面才能立刻看到"这一轮消耗了哪些号" |

`upc_sync` 工作流本身保留,当手动体检入口用。

## 四、「已对接飞书表的,执行完就写,不要做成单独的」

所有者这条要求指向一个具体的不一致:**订单中心六表是唯一由独立工作流
(`order_center_push`)推的**,其余各链都是自己写自己的表 ——

| 链 | 谁写飞书 |
|---|---|
| `maintenance` | 自己写「维护记录」 |
| `product_clear` | 自己写「停用/删除表」 |
| `list_new` / `match_listing` | 自己写「上架表」/「跟卖表」 |
| `feed_poll` | 五个反哺器回填结果列 |
| **订单中心六表** | **`order_center_push` 单独一条** ← 不一致在这里 |

**改法**(工作项 B):把 `order_center_push` 的五个 pusher(`_push_sales` /
`_push_returns` / `_push_perf` / `_push_settlement` / `_push_keys`)抽到
`services/order_center.py`,各链跑完调自己那一张:

```
order_sync        → 写销售表 + 键表
returns_sync      → 写售后表
perf_problems     → 写绩效表
settlement_sync   → 写对账表
```

`order_center_push` 保留为**手动全量补推 / 对账入口**(`-p reconcile=1` 那套),
不再进调度。铁律 1 照旧:抽到 services,不是让工作流互相 import。

⚠ ~~销售表 90 天窗口每小时全推,写放大很大~~ —— **这条我判断错了,所有者纠正后
复核代码属实,撤回**。`_sync_stateful()` 是**本地状态 + 行指纹**驱动:
每轮只把「本地没有的键」建、「指纹变了的行」更新,其余全部跳过
(摘要里那个「跳过 N」就是它)。日常连飞书表都不拉(只有 `reconcile=1` 才全量
对账)。所以每小时按默认 90 天窗口跑没有问题,真正写出去的就是几十到几百行。
**不需要分窗口。**

## 五、回答「等待会占锁吗?什么锁」

会。`cli.py` 每跑一个工作流,先拿 `<DATA_ROOT>/locks/<工作流名>.lock` 的
**flock 独占锁**,整轮持有,进程退出才释放。所以 `order_audit -p wait=1`
阻塞等采集的那段时间(最长 20 分钟),`order_audit.lock` 一直被占着。

具体影响三条:

1. **手动再跑一次会直接退出**(退出码 3,不排队)。这是有意的 —— 两个
   `order_audit` 同时判同一批行会互相覆盖结论。
2. **下一轮定时**:每小时跑一次,最长占 20 分钟 < 60 分钟,正常不会撞上。
   真撞上了(某轮异常慢),那一轮的链会在 `order_audit` 这步停,
   `returns_sync` 不跑 —— 但 `order_sync` 已经跑完了,数据不丢,下一小时自动恢复。
3. **它还会短暂借 `product_ingest` 的锁**:`wait` 期间要就地跑一次增量摄取,
   而游标是独占推进的(两个进程同推会静默丢掉中间一段)。借不到就**跳过**
   (不是失败)—— 说明 `product_ingest` 真的在跑,数据照样会进来。
   ⚠ 反过来也成立:产品线 09:00 跑 `product_ingest` 时,如果 09:20 那轮
   `order_audit` 想就地摄取,它会跳过。**这是安全的,不是故障。**

**结论:保持默认 `wait=1`**(v1 的建议是 `wait=0`,现在收回)。理由:20 分钟
远小于一小时,一条命令出真结论,而 `wait=0` 会让结论恒定滞后一轮 ——
"忘了就静默降级"正是这个默认值当初要避免的。

## 六、输入已齐

| 项 | 值 |
|---|---|
| 解释器 | `/Users/nextderboy/Projects/WalmartAPI-Contral/.venv/bin/python3` |
| 仓库 | `/Users/nextderboy/Projects/WalmartAPI-Contral` |
| DATA_ROOT | `/Users/nextderboy/Projects/WalmartAPI_data`(与仓库平级,`registry/paths` 默认推导) |
| 通知接收人 | `17882211182`(手机号 → 自动换 open_id) |

⚠ plist **不需要 activate venv**:直接把上面那个 `.venv/bin/python3` 当解释器写进
`ProgramArguments` 就行 —— venv 的 python 会自己把 `site-packages` 摆对。

## 七、起调度之前必须先做的四件代码活

| | 工作项 | 状态 |
|---|---|---|
| **A** | 实现 `product_refresh` 的 `wait`(轮询批次到落定,`TIMEOUT_HOURS=1` 兜底) | ✅ **已做**。落在 `services.scrape_batches.wait_settled()`;等完再跑一次 `check_open` 落台账。超时不停链(已采到的照常能用),但摘要点名 |
| **B** | 订单中心五表拆到 `services/order_center.py`,各链跑完自己写 | ⬜ **待做**(下一步) |
| **C** | 飞书通知改用**应用直接发给所有者** | ✅ **已做**。三条路依次退;手机号自动换 open_id 并缓存 |
| **D** | `list_new` 上架**之后**补一次 UPC 池回写 | ✅ **已做**(`_writeback_upc`,dry-run 不写,失败不阻断) |

### 关于 C:飞书通知改应用直发(已实现)——**旧项目就是这么做的**

查了旧仓摸底底稿,证据确凿:

- **身份**:`lark-cli im +messages-send --as bot`,AppID `cli_a9561a4f8dfadcd2`
  (`legacy_survey.md:649/1818`)—— 一直是**应用身份**,不是群机器人 webhook。
- **收件人**:open_id `ou_36c5f91668c42a735e7b9d4ae74eedc1`(运营苏里 /
  freafish006@gmail.com),硬编码在 `summary.py:38`。
- **判型规则**:`ou_` 前缀 → `--user-id`,`oc_` → `--chat-id`(`notify.py:137`)。

所以群机器人 webhook 是**本仓新引入的第二套身份**,至今没配上 —— 改回应用直发
等于回到旧系统一直在用的那条路。判型规则逐字沿用,换人接手不用重学。

新增环境变量 **`FEISHU_NOTIFY_TO`**,取值可以是四种写法,类型自动认:

| 写法 | receive_id_type |
|---|---|
| `ou_…` | open_id |
| `oc_…` | chat_id(群) |
| 含 `@` | email |
| 11 位数字 | ⚠ 飞书**没有**这一档,先换 open_id(见下) |

所有者给的是**手机号 `17882211182`**。飞书 `im/v1/messages` 的
`receive_id_type` 只有 open_id / user_id / union_id / email / chat_id 五档,
**没有手机号**,所以代码里先走一步通讯录换 ID:

```
POST /open-apis/contact/v3/users/batch_get_id?user_id_type=open_id
{"mobiles": ["17882211182"]}          → open_id,进程内缓存,只换一次
```

⚠ **这一步要应用有 `contact:user.id:readonly` 权限**(后台加完要发布版本)。
没有权限时只告警、退到 webhook 或只记日志,**不会把工作流拖垮**。
嫌麻烦的话:直接把 `FEISHU_NOTIFY_TO` 填成 **open_id 或飞书账号邮箱**,
就不需要这条权限。

⚠ 实现里踩住的那个坑:`content` 必须是**字符串化的 JSON**
(`"{\"text\":\"…\"}"`),传成嵌套对象飞书直接拒 —— 有用例钉着。

三条路依次退:应用直发 → webhook → 只记日志。切换期不会把通知打断。

**要配的 .env 两行**:

```
FEISHU_NOTIFY_TO=17882211182
# FEISHU_WEBHOOK_URL=…        # 可留空;应用发通了就不需要它
```

## 八、plist 模板与四个坑(批复 2 之后少了一个)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key> <string>com.walmartapi.order_chain</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/nextderboy/Projects/WalmartAPI-Contral/.venv/bin/python3</string>
    <string>/Users/nextderboy/Projects/WalmartAPI-Contral/cli.py</string>
    <string>order_sync</string><string>order_audit</string><string>returns_sync</string>
  </array>
  <key>WorkingDirectory</key> <string>/Users/nextderboy/Projects/WalmartAPI-Contral</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>WALMART_OPERATOR</key> <string>launchd</string>
    <key>PATH</key> <string>/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin</string>
  </dict>
  <key>StartCalendarInterval</key> <dict><key>Minute</key><integer>20</integer></dict>
  <key>StandardOutPath</key>  <string>/Users/nextderboy/Projects/WalmartAPI_data/logs/launchd/order_chain.out</string>
  <key>StandardErrorPath</key><string>/Users/nextderboy/Projects/WalmartAPI_data/logs/launchd/order_chain.err</string>
  <key>RunAtLoad</key> <false/>
</dict></plist>
```

**坑 1 — `ProgramArguments` 不过 shell。** 没有 `&&`、`~`、通配符、变量展开。
串联必须用 cli 的多工作流参数(这也是当初把串联做进 cli 的原因之一)。

**坑 2 — launchd 不读 shell 配置。** `~/.zshrc` 的 `PATH`/`export` 一概不生效。
密钥走 `<DATA_ROOT>/.env`(cli 自己加载),但 `PATH` 要显式给 ——
`pg_dump`(backup 用)不在默认 PATH 里就报"命令未找到"。
**venv 不需要 activate**,直接用 venv 里的 python 绝对路径即可。

**坑 3 — `StandardOutPath` 不能省**,即使 cli 已经写 `logs/<workflow>.log`。
两者盖的不是同一段:cli 的日志从"Python 起来了"才开始;
**解释器路径写错 / venv 被删 / import 期就炸**发生在那之前,只会出现在
launchd 的 stdout/stderr 里。没有它,故障表现是"这条链每天什么都不做,
日志里一个字也没有"。

**坑 4 — 一条链只挂一个 plist。** 每小时那条给 `Minute=20` 不给 `Hour` 就是
每小时的 :20;别再单挂一个 06:20,两个会撞车,后到的整链拿不到锁退 3 空跑。

(v1 的"睡眠补跑"坑作废 —— 批复 2:不关机。)

## 九、时间表(v4 汇总)

| 时间 | 命令 | 线 |
|---|---|---|
| 02:00 | `backup` | 基础 |
| 02:30 | `risk_sync blacklist_push` | 线 4 |
| 06:40 | `daily_report` | 线 3 |
| 07:30 | `perf_problems` | 线 2 |
| 09:00 | `catalog_sync product_refresh product_ingest maintenance_scan maintenance problem_scan problem_product_cleanup -p product_refresh:wait=1` | 线 1 |
| 15:00 | `product_clear` | 定稿 |
| 每小时 :20 | `order_sync order_audit returns_sync` | 线 2 |
| 每 30 分 | `feed_poll` | 基础 |
| 双周三 08:00 | `settlement_sync` | 线 2 |

## 十、上线顺序(分三批,每批观察一天)

| 批 | 开什么 | 先停什么旧的 | 看什么 |
|---|---|---|---|
| **一(只读/低危)** | `backup`、`risk_sync blacklist_push`、`feed_poll` | 无 | `ops.runs` 的时长与失败率 |
| **二(订单 + 日报)** | 每小时订单链、`perf_problems`、`settlement_sync`、`daily_report` | 旧订单同步(GPT 侧 13:30 + cron 每时:15,**两条同停**)、旧 KPI(08:00 + 14:00) | 订单列对拍差异 8 店是否收敛;**影刀有没有数据** |
| **三(破坏性)** | 产品线整条、`product_clear`、`sku_locked_heal` | 旧维护 12:00、旧下架 15:00、旧 cleanup 0/6/12/18、旧 retire 23:30 | 每条**先手动 `--dry-run` 人眼确认**再挂 plist |

⚠ 批三的 `maintenance_scan -p preview=1` 要重点看「标题不匹配」那类删除的条数
—— 五条删除判据里唯一没有生产数据背书的一条。
