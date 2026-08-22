---
name: walmart-schedule
description: 沃尔玛业务链的定时任务执行手册(每日/每周那部分;高频链在电脑 launchd 上,不归这里)
---

# 沃尔玛定时任务

> **本文件是生成的,不要手改。** 出处是 `registry/schedule.py` 的 `JOBS`,改完跑 `python cli.py skill_export` 重新生成。
> 手改会造成「提示词里写的」和「代码里定的」不一致,而这种不一致**没有任何东西会报错**。

## 你要做的事

到点在 `/Users/nextderboy/Projects/WalmartAPI-Contral` 下跑一行命令,看退出码,失败了把日志发给苏里。
就这些 —— 业务判断全在代码里,不需要你替它决定任何事。

## 任务表

| 任务 | 时间(台北) | cron(台北) | cron(UTC) | 跑什么 |
|---|---|---|---|---|
| `backup` | 每天 02:00 | `0 2 * * *` | `0 18 * * *` | backup |
| `daily_report` | 每天 06:40 | `40 6 * * *` | `40 22 * * *` | daily_report |
| `order_daily` | 每天 07:30 | `30 7 * * *` | `30 23 * * *` | perf_problems → order_asin_normalize |
| `product_chain` | 每天 13:00 | `0 13 * * *` | `0 5 * * *` | catalog_sync → sources_backfill → product_refresh → maintenance_scan → maintenance → product_audit → problem_scan → problem_product_cleanup |
| `blacklist` | 每天 15:00 | `0 15 * * *` | `0 7 * * *` | risk_sync → blacklist_push |
| `product_clear` | 每天 15:00 | `0 15 * * *` | `0 7 * * *` | product_clear |
| `audit_sheet` | 每天 18:10 | `10 18 * * *` | `10 10 * * *` | product_audit |
| `list_new` | 每天 20:00 | `0 20 * * *` | `0 12 * * *` | list_new |
| `settlement` | 每周三 08:00 | `0 8 * * 3` | `0 0 * * 3` | settlement_sync |

⚠ **时区:「时间」与「cron(台北)」两列是台北时间(UTC+8),和这台电脑的本地时间一致;平台按 UTC 存就用「cron(UTC)」那一列,**别自己减** —— 台北 02:00 / 06:40 / 07:30 对应的是 UTC **前一天**,带星期的还得把星期一起往前挪,已经替你算好了。时区弄反的表现是「每天准时在错的时间跑」,不报任何错(02:00 的备份跑到 10:00 去,13:00 的产品链跑到半夜)。

每条任务的完整提示词在 `tasks/<任务名>.md`,注册定时任务时**整篇粘进去**。

## ⚠ 跑之前:你得能写仓库**外面**那个目录

本项目的状态目录不在仓库里,而是仓库的**同级**目录 `/Users/nextderboy/Projects/WalmartAPI_data` —— 锁、日志、备份、报告全在那儿。

**沙箱默认只放行工作区(= 仓库目录),于是每条工作流在拿运行锁的第一步就死**,报 `PermissionError: [Errno 1] Operation not permitted: …/locks/<名>.lock`。

⚠ 这个报错**长得像权限问题但不是**:文件属主和 mode 都是对的,errno 是 **EPERM(1)** 而不是 EACCES(13)。所以**别去 chmod/chown**,也**别** chmod 777、别改 `WALMART_DATA_ROOT`、别建软链接 —— 那几样都是把状态搬到别处,只会让两份状态各写各的。

正确做法是给沙箱按目录授权。Codex 走项目级 `/Users/nextderboy/Projects/WalmartAPI-Contral/.codex/config.toml`(已在仓库里,拉下来即生效):

```toml
sandbox_mode = "workspace-write"
[sandbox_workspace_write]
network_access = true
writable_roots = ["/Users/nextderboy/Projects/WalmartAPI_data"]
```

**跑第一条任务之前先自检**(能写 = 一切就绪;被拒就先修沙箱,别急着注册):

```bash
touch /Users/nextderboy/Projects/WalmartAPI_data/locks/_probe && rm /Users/nextderboy/Projects/WalmartAPI_data/locks/_probe && echo OK
```

## 这两条不进你的定时任务表,但**得有人管**

| 任务 | 时间 | 跑什么 |
|---|---|---|
| `feed_poll` | 每小时 :00/:30 | feed_poll |
| `order_chain` | 每小时 :20 | order_sync → order_audit → returns_sync |
| `product_ingest` | 每小时 :50 | product_ingest |

它们跑在**电脑自己的 launchd** 上,而不是你的定时任务里 —— 频率太细(每半小时 / 每小时固定分钟),你那边多半排不准,而排不准的后果不是报错,是悄悄少跑几轮。

⚠ **别把这一节读成「不用管」。** 装 launchd 那一步本身**是要人做的**,做法在 `skills/walmart-schedule/REGISTER.md` 第 3 步(`launchd_install` → `launchctl load` → 回读校验)。**没装 = 这两条链从来不跑,而且没有任何东西会说一声** —— 表现是飞书上的「处理中」永远不消失(feed 回执没人反哺)、日报的订单列是空的(订单没人拉),而所有已注册的任务都报成功。拿不准装没装就去查:`launchctl list | grep com.walmartapi`,应当正好 3 行。

⚠ **装好之后,你这边不要再挂一份 —— 两边都挂 = 撞锁**:同一条链被 launchd 和你同时拉起来,后到的那次拿不到锁直接退出码 3 空跑一轮 —— 看起来一切正常。

## 三条通用规则(每条任务都适用)

### 退出码就是结论

| 码 | 意思 | 你该做什么 |
|---|---|---|
| 0 | 成功 | 什么都不做(飞书通知它自己会发) |
| 3 | 没抢到锁,上一轮还在跑 | **不是失败,不要重试**;连着两次才值得说一声 |
| 1 | 失败 | 取日志末尾,连同失败的那一步一起发给苏里,**不要自动重跑** |
| 2 | 工作流名写错了 | 说明提示词与 `registry/schedule.py` 脱节了,报给苏里 |

### 不许加 `--dry-run`

缺省即真跑(2026-08-16 定稿)。`--dry-run` 是给人改完代码后自己验的,**不进任何定时任务**:写进去的后果是那条链每天空转而且报成功,比误跑更难发现 —— 误跑至少留下痕迹。

### 失败不自动重跑

这些链会写沃尔玛、写数据库。重跑的代价可能是重复提交;而失败原因通常是外部的(凭证过期、代理不通、飞书表被人改了),重跑一遍还是同样的失败。

## 出了怪事看哪里

- **每次运行的记录**在库里 `ops.runs`(谁触发的、跑了多久、成没成、摘要全文)—— 想知道昨天那次到底跑没跑,看这张表,不要靠回忆。
- **日志**一个工作流一份,目录问代码要(别写死,`WALMART_DATA_ROOT` 能覆盖它):

```bash
cd /Users/nextderboy/Projects/WalmartAPI-Contral && tail -n 60 "$(/Users/nextderboy/Projects/WalmartAPI-Contral/.venv/bin/python3 -c 'from registry import paths; print(paths.logs_dir())')/<工作流名>.log"
```

- **通知**成功失败都会发飞书;一条链只发一条(不是每步一条)。
