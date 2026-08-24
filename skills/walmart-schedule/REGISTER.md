# 交办:一次性注册这 9 条沃尔玛定时任务

> **本文件是生成的,不要手改**(出处 `registry/schedule.py`,改完跑 `python cli.py skill_export`)。把下面整篇给要注册的那个智能体。

---

请把下面 9 条定时任务一次性注册好。这几条是沃尔玛业务链的每日/每周调度,注册完之后由你到点触发。

## 第 0 步之前:先确认你能写仓库**外面**那个目录

本项目的状态目录是仓库的**同级**目录 `/Users/nextderboy/Projects/WalmartAPI_data`(锁/日志/备份/报告都在那儿)。默认沙箱只放行工作区,于是每条工作流在拿运行锁的第一步就死,报 `PermissionError: [Errno 1] Operation not permitted`。

**注册之前先自检**(30 秒,能省掉一整天的空转):

```bash
touch /Users/nextderboy/Projects/WalmartAPI_data/locks/_probe && rm /Users/nextderboy/Projects/WalmartAPI_data/locks/_probe && echo OK
```

被拒就先修沙箱:项目级 `/Users/nextderboy/Projects/WalmartAPI-Contral/.codex/config.toml` 已在仓库里(`sandbox_workspace_write.writable_roots` 指向 DATA_ROOT),拉下来即生效。
⚠ **别用 chmod 777 / 改 WALMART_DATA_ROOT / 建软链接**绕过去 —— 那几样都是把状态搬到别处,只会让两份状态各写各的。

## 第 0 步:先把时区定了(**唯一一个会静默出错的地方**)

先确认你这边的定时任务按哪个时区存:

- 能按**台北时间(UTC+8)**存 → 用下表「cron(台北)」那一列;
- 只能按 **UTC** 存 → 用「cron(UTC)」那一列,**已经替你算好了,不要自己减 8 小时**;
- 两列都不确定 → **先别注册,回来问苏里**。

为什么单独拎出来说:时区弄反的表现是「每天准时在错的时间跑」,不报任何错、不失败、通知照发 —— 02:00 的备份跑到上午 10 点去,13:00 的产品链跑到半夜。这套东西里只有这一处会这样坏。

## 第 1 步:按这张表注册

| 任务名 | 时间(台北) | cron(台北) | cron(UTC) | 提示词正文取自 |
|---|---|---|---|---|
| `backup` | 每天 02:00 | `0 2 * * *` | `0 18 * * *` | `skills/walmart-schedule/tasks/backup.md` |
| `daily_report` | 每天 06:40 | `40 6 * * *` | `40 22 * * *` | `skills/walmart-schedule/tasks/daily_report.md` |
| `order_daily` | 每天 07:30 | `30 7 * * *` | `30 23 * * *` | `skills/walmart-schedule/tasks/order_daily.md` |
| `product_chain` | 每天 13:00 | `0 13 * * *` | `0 5 * * *` | `skills/walmart-schedule/tasks/product_chain.md` |
| `blacklist` | 每天 15:00 | `0 15 * * *` | `0 7 * * *` | `skills/walmart-schedule/tasks/blacklist.md` |
| `product_clear` | 每天 15:00 | `0 15 * * *` | `0 7 * * *` | `skills/walmart-schedule/tasks/product_clear.md` |
| `audit_sheet` | 每天 18:10 | `10 18 * * *` | `10 10 * * *` | `skills/walmart-schedule/tasks/audit_sheet.md` |
| `list_new` | 每天 20:00 | `0 20 * * *` | `0 12 * * *` | `skills/walmart-schedule/tasks/list_new.md` |
| `settlement` | 每周三 08:00 | `0 8 * * 3` | `0 0 * * 3` | `skills/walmart-schedule/tasks/settlement.md` |

**任务名照抄第一列**,别改成中文、别加前缀 —— 出问题时苏里是按这个名字对回 `registry/schedule.py` 的。

## 第 2 步:每条的提示词 = 那个 md 文件的全文,原样粘

仓库在 `/Users/nextderboy/Projects/WalmartAPI-Contral`,九份提示词在 `skills/walmart-schedule/tasks/` 下。每条任务的提示词就是对应那份文件的**全文**。

- **不要改写、不要摘要、不要「提炼要点」。** 那些文件里看起来啰嗦的部分(退出码 3 不算失败、失败不许自动重跑、不许加 `--dry-run`)都是踩过的坑,删一条就复活一个。
- **不要自己合并任务。** 同一时间的两条(15:00 有两条)也分开注册 —— 合起来跑会撞锁,后到的那条直接空跑一轮还报成功。
- **读不到文件就停下来问苏里要**,不要凭任务名自己编一段提示词。(他这一行就能把九份全导出来给你:`cat skills/walmart-schedule/tasks/*.md`,每份以 `# 沃尔玛定时任务:<任务名>` 开头,按这行切开即可。)

## 第 3 步:这两条**不要注册成你的定时任务**,改用电脑的 launchd

`feed_poll`, `order_chain`, `product_ingest` 是高频链:

| 任务 | 频率 | 跑什么 |
|---|---|---|
| `feed_poll` | 每小时 :00/:30 | feed_poll |
| `order_chain` | 每小时 :20 | order_sync → order_audit → returns_sync |
| `product_ingest` | 每小时 :50 | product_ingest |

两个理由,都不是偏好问题:

1. **频率你那边多半排不出来** —— 每半小时一次、每小时固定 :20 这种,常见的智能体定时任务最细只到「每小时」甚至「每天」。排不准的后果不是报错,是**悄悄少跑几轮**。
2. **两边都挂 = 撞锁** —— 同一条链被 launchd 和你同时拉起来,后到的那次拿不到锁直接退出码 3 空跑一轮,而外表看起来一切正常。

**正确做法:让它们跑在电脑自己的 launchd 上,你只负责装。** 三步,都在 `/Users/nextderboy/Projects/WalmartAPI-Contral` 下:

```bash
# ① 先空跑,把它要生成什么打给苏里看(不落盘)
/Users/nextderboy/Projects/WalmartAPI-Contral/.venv/bin/python3 /Users/nextderboy/Projects/WalmartAPI-Contral/cli.py launchd_install --dry-run

# ② 他确认无误后落盘(只写 plist 文件,还没生效)
/Users/nextderboy/Projects/WalmartAPI-Contral/.venv/bin/python3 /Users/nextderboy/Projects/WalmartAPI-Contral/cli.py launchd_install

# ③ 装载(这一步才真的开始按秒表跑;命令由 ② 的输出原样给出,照抄)
#    形如:launchctl load -w ~/Library/LaunchAgents/com.walmartapi.<任务名>.plist
```

⚠ **① 必须给苏里看过再做 ②**:`launchd_install` 装完就是真调度,而这两条链会写沃尔玛、写数据库。
⚠ **③ 的命令照抄 ② 的输出,不要自己拼路径**(plist 名带前缀 `com.walmartapi.`,拼错的表现是 launchctl 说找不到文件)。

装完**回读校验**,和第 5 步一样的道理:

```bash
launchctl list | grep com.walmartapi
```

应当正好 3 行。然后**等到下一个整点/半点再确认它真的跑了** —— 装上了不等于跑得起来(解释器路径错、venv 被删这类问题只会出现在 launchd 自己的日志里,不会有人通知你):

```bash
cd /Users/nextderboy/Projects/WalmartAPI-Contral && tail -n 30 "$(/Users/nextderboy/Projects/WalmartAPI-Contral/.venv/bin/python3 -c 'from registry import paths; print(paths.logs_dir())')/launchd/"*.log
```

**上面第 1 步那张表里没有的,一条都不要注册成定时任务**。想加是改 `registry/schedule.py` 再重新生成这份交办单,不是在你这边手动加一条。

## 第 4 步:别动现有的任务

你这边可能还有旧 erpAPI 时期的定时任务。**不要删、不要改、不要顺手「清理重复的」** —— 旧调度的停用由苏里自己按顺序来(先停旧的、搬状态、再起新的),你替他删会让新旧两套在同一件破坏性任务上错位。有拿不准的就列出来问,不要动手。

## 第 5 步:注册完回读一遍(**这步不许省**)

注册这件事本身没有退出码,所以唯一能证明它对了的办法是回读。注册完请把结果按下面这个样子列出来发给苏里:

```
已注册 N 条,时区=<台北 | UTC>
任务名 | 存进去的 cron | 提示词第一行
```

苏里会拿它和上面那张表逐行对。**条数必须正好 9 条** —— 多出来的是重复注册(会撞锁),少掉的是那条链从此每天不跑而没人知道。
