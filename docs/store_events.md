# 店铺事件账本:上线三步与日常排查

> 表结构、索引、两个视图的口径在 `docs/db_schema.md`(ops 域);
> **事件码、分类、severity 分级、摘要文案的唯一出处是 `services/store_events.py`**
> —— 本文件不复述清单(三处清单必然各漂各的)。
>
> 这里只回答两件事:**这条链怎么走进生产**,以及**上线之后出问题去哪看**。

---

## 一、一页版:它是什么

`ops.store_events` 是**店铺维度的病历**,与 `catalog.product_events`(产品病历)
同构不同表。它回答的是"这家店身上发生过什么",而 `ops.store_kpi_daily` 回答的是
"这家店今天什么样" —— 一个是**变化流**(只追加),一个是**日粒度截面**。
事件里**绝不重复存 KPI 数值**:同一个数字两处存,一定会漂。

| | 谁在写 | 典型 severity |
|---|---|---|
| **risk** 风险 | `daily_report`(三状态迁移)、`product_audit`(TRO 品牌命中 + 波及)、`order_audit`(钓鱼订单 + 品牌波及) | high / mid |
| **governance** 治理 | `store_watch` 每轮的配置快照 diff、`alloc_plan` / `alloc_backfill` / `store_release` 的占用与释放 | 视内容,整店释放与凭证删店是 high |
| **ops** 运营 | 五条执行链**每店每轮一条**(上架/维护/清理/下架/跟卖) | 恒 info |

**写入方一律只落行,不发任何通知。** 通知只有一个出口:`store_watch`
(每小时 :45,launchd)。谁发谁就得各自实现去重与限流,而同一次封店会从三个
地方各响一次 —— 那样的通知没人看,等于没有通知。

`store_watch` 一轮做三件事:比对治理配置快照 → 扫「未推送 + 高危 + 48h 内」
→ 一轮一条飞书 → 标 `notified_at`。**推送失败一条都不标**(账本只追加,
标了就是永久埋掉),摘要会明说"未发出,下轮重试"。

### TRO 判据(所有者 2026-09-01 定稿,**推翻**了 08-30 那版)

**疑似 TRO = 支付被冻结(ACTIVE→INACTIVE),而店铺状态仍然是 ACTIVE。**
反常之处在于「店还开着、钱却被冻住」—— 那才是法院冻结令的形状。
**店被停了、钱跟着冻,那是后果,不是独立信号**,只是一次普通的店铺暂停。

| 本轮事件 | 店铺当前状态 | 判定 |
|---|---|---|
| 支付 ACTIVE→INACTIVE **+ 店铺 ACTIVE→SUSPENDED/TERMINATED** | 非 ACTIVE | **不是**(普通店铺暂停) |
| 支付 ACTIVE→INACTIVE,本轮无店铺事件 | ACTIVE | **是** |
| 支付 ACTIVE→INACTIVE,本轮无店铺事件 | 早就是 SUSPENDED | **不是**(旧暂停的延迟后果) |
| 只有店铺事件,无支付事件 | 任意 | **不是** |

起因是一次生产误报:2026-09-01 日报实跑,82杨乾良 同日三条(店铺
ACTIVE→SUSPENDED、支付 ACTIVE→INACTIVE、销售 可售→不可售)被旧判据
(「封店 + 资金冻结同日出现」)报成「疑似 TRO 封店」,所有者看日报当场指出
那就是一次普通的店铺暂停。

⚠ 第二、三种情形**本轮事件长得一模一样**(两者本轮都只有支付那条腿),
只有 `ops.store_kpi_daily` 里**那天那家店的 `store_status` 真值**分得开 ——
事件流只记「变了什么」,答不出「现在是什么」。所以:
`daily_report` 在 upsert 那一行时值就在手上,直接传给 `tro_signature`(零额外
查询);`store_watch` 手上只有事件行,由 `tro_stores(conn, rows)` 按 (店, 日)
回查截面表(高危每天个位数,一轮一次查询)。**状态拿不到就不报** —— 判据的
唯一出处是 `services/store_events.tro_signature` 的 docstring。

---

## 二、上线三步

三步**有先后**,而且第二步只做一次。整条链不碰沃尔玛任何写接口,
写库只有 `notified_at` 与治理事件两处,可以随时重跑。

### 第 1 步:建表建视图

```bash
python cli.py db_init
```

幂等。建 `ops.store_events`(三个索引,其中一个是给预警扫描用的局部索引)与
两个档案视图 `ops.v_store_timeline` / `ops.v_store_profile`。

### 第 2 步:seed —— 把存量一次吞掉(**只做一次,先看再做**)

```bash
python cli.py store_watch --dry-run     # 先看:将被吞掉的是哪些,逐条列出来
python cli.py store_watch -p seed=1     # 确认后执行:标记但不推送
```

**为什么必须有这一步。** 账本从 2026-08-30 起就一直在写,而推送这条出口
是后加的。第一次真跑会把窗口内(缺省 48 小时)**所有**未推送的 high 一次推出去
—— 那条消息里绝大多数是已经过去的事,真正今天该看的那条埋在里面。
比"刷屏"更糟的是它的长期后果:**人会被这一条训练成把店铺预警当噪声**,
下一次真出事时那条消息同样没人点开。

`seed=1` 的语义是**标记但不推送**:把当下窗口内的高危一次标成已推,
从下一轮起推的都是**新发生的**。

- ⚠ **seed 绝不能写进调度。** 挂上就是每小时静默吞事件,表现是"预警一条都不来",
  而且一切报成功。`tests/test_store_watch.py` 有一条用例钉住调度条目不带 `seed`。
- ⚠ **顺序不能换:先 seed,再装调度。** 反过来的话,launchd 那一轮先到,
  存量已经推出去了,seed 就没有意义了。
- 想连更早的历史一起吞:`-p seed=1 -p hours=99999`(窗口只影响扫得到哪些行)。

### 第 3 步:装调度

```bash
python cli.py launchd_install --dry-run    # 先看要写哪些 plist
python cli.py launchd_install              # 落盘(还没生效)
launchctl load -w ~/Library/LaunchAgents/com.walmartapi.store_watch.plist
```

装载命令照抄第二条命令的输出,别自己拼路径。装完回读:

```bash
launchctl list | grep com.walmartapi     # 应当有 store_watch 这一行
```

---

## 三、上线后第一天要确认的四件事

| 看什么 | 正常长什么样 |
|---|---|
| `ops.runs` 里的 `store_watch` | 每小时一条,`success`,耗时几秒(治理快照那两张飞书表是大头) |
| 第一条真推送 | 第一行是结论(`🚨 店铺预警:N 店 M 条高危`),明细每条一行说人话 —— **不该出现事件码原文**(`tro_brand_hit` 这种)或 `None→None` |
| 摘要里的「窗口外滞留」 | **恒 0 不打**。出现了就是有高危一直没推出去(见下表) |
| 治理快照第一轮 | `治理快照:无变更` —— 首次快照不产事件(没有上一版就没有"变化") |
| 治理快照的变更明细 | 每条都点得出**是谁的哪一列从什么改成了什么**。限额表里的未登记列(`SourceID` 之类飞书内部字段)**不该出现**:它们没有代码消费,只在列本身增减时报一条 `store_limits_columns_changed`(全局行) |

---

## 四、按症状排查

| 症状 | 多半是 | 怎么确认 |
|---|---|---|
| **预警一条都不来** | ① 调度没装;② `seed` 被写进了调度参数;③ 飞书没配 | `launchctl list \| grep com.walmartapi`;`registry/schedule.py` 里那条的 `params` 应当是空的;摘要里若写着「seed 标记 N 条」就是 ② |
| 每轮都说「**未发出**,下轮重试」 | `FEISHU_NOTIFY_TO` / `FEISHU_WEBHOOK_URL` 没配,或推送被拒 | 事件**没丢**(一条都没标),配好之后下一轮自动补推 |
| 「窗口外滞留 N 条」不为 0 | 连着几天没推成功,或高危产出速度长期超过 `limit=50` | `-p hours=N` 放宽窗口补推一次;先弄清为什么积压,再放宽 |
| **资金明明被冻结,却没报「疑似 TRO」** | ① 那天店铺状态本来就不是 ACTIVE —— 判据如此(见「一、TRO 判据」那张表的第一、三行),不是漏报;② KPI 表里那天那家店没有行或 `store_status` 为空,判不出就不报 | `SELECT store_status FROM ops.store_kpi_daily WHERE store = '…' AND data_date = '…'`;空/无行就是 ② |
| 明细行是干瘪的事件码 | 新登记了事件码却没在 `store_events._brief_body` 写渲染 | `tests/test_store_watch.py::test_brief_covers_every_registered_event_code` 会在提交期挡住,红了就是漏了 |
| 「还有 N 条待推」每轮都在 | 高危产出速度超过单轮上限 | 调 `-p limit=N`;但持续几十条高危本身就该有人看,不是调大上限能解决的 |
| 治理快照每轮都报「本轮没比对」 | 飞书读不到(表未登记 / 权限 / 网络) | 摘要里带着原因;**不产事件也不覆盖快照**,修好之后接着比上一版,不会漏 |
| 治理快照报「本轮没比对(快照结构 vN → vM 升级)」 | 改了快照形状(`store_config.SNAPSHOT_VERSION` +1)后的**第一轮**,只此一轮 | 这一轮**不产事件但覆盖快照**(与飞书失败那条正相反):没有可比的上一版就不是变化,硬比会刷满屏假事件。下一轮起恢复正常 |
| 治理事件里刷出一批看不懂的列 | 未登记列进了逐格比对 | **不该发生了**(2026-09-01 v2 起未登记列只记列名):限额表里 `SourceID` 的值含行内容的哈希,谁动一格就全表跟着变。真出现了就是有人往 registry 补登了不该登的列 —— 未登记名单与「为什么不登记」的定稿在 `store_config._limits_snapshot` 的 docstring(`SourceID` / `店铺状态` 两列,**有意不登记**,别顺手补) |

---

## 五、日常查档案

```sql
-- 这家店身上按时间发生过什么(封店那天前后它在干什么)
SELECT * FROM ops.v_store_timeline WHERE 店铺 = 'A085' LIMIT 50;

-- 一屏看全部店铺:谁身上压着高危、谁还有没推出去的
SELECT * FROM ops.v_store_profile;

-- 只看还没推出去的高危(store_watch 下一轮要推的就是这些)
SELECT * FROM ops.v_store_timeline WHERE 级别 = 'high' AND 已推送 IS NULL;
```

⚠ `v_store_profile` 的**店铺全集来自 `ops.store_kpi_daily`**:没跑过
`daily_report` 的店不出现在那里,`store IS NULL` 的全局事件(TRO 品牌源头、
规划范围变更)也不属于任何一家店 —— 两者都去 `v_store_timeline` 查。

两个视图都是**零程序读者、专给人与 AI 排查用的**。判它们"没用了"之前先连库:

```sql
SELECT query, calls FROM pg_stat_statements WHERE query ILIKE '%v_store_profile%';
```

"代码不读" ≠ "没人 SELECT"。
