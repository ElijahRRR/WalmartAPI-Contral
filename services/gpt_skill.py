"""智能体定时任务的技能包渲染(runner="gpt" 那部分调度)。调度表在 `registry.schedule`。

所有者定稿 2026-08-16:高频链(feed 轮询 + 订单链)注册到电脑 launchd,
**其余每日/每周一次的用 skill 形式给智能体注册成定时任务** ——
「前期稳定,也方便我维护和调整,以后换个智能体也能用」。

所以这里渲染的东西有两条硬要求:

  · **不绑定任何一家智能体**。产物是纯 Markdown:一份总纲(怎么跑、怎么判成败、
    什么绝对不许做)+ 每条任务一段可直接粘进"定时任务提示词"的话。
    换个智能体只需要把同样几段话再粘一遍,不改代码。
  · **不重复调度表**。时间/命令/参数全部从 `registry.schedule.JOBS` 渲染,
    一处改处处改 —— 同一张表写两处必然漂(schedule.py 头注已经吃过两次)。

⚠ 与 launchd 那边最大的差别:launchd 是"秒表",智能体是"秒表 + 一双眼睛"。
所以技能里最重要的不是怎么起,而是**起完之后什么该管什么不该管**:
退出码 3(锁)不是失败、破坏性链失败**绝不自动重跑**、任何时候都不许自己加
`--dry-run`(加了就是每天空转还报成功 —— 这正是缺省即真跑那条定稿要消灭的)。
"""

import importlib

from registry import schedule

RUNNER = "gpt"

# 技能包落盘位置(仓库内,进 git)。它是文档不是密钥,而且要跟着代码一起改
SKILL_DIR = "skills/walmart-schedule"

_WEEK = "一二三四五六日"


def jobs() -> list[dict]:
    """输入:无 → 输出:归智能体触发的调度(按时间排)。"""
    return sorted(schedule.jobs_for(RUNNER),
                  key=lambda j: (j["weekday"] or 0,
                                 j["hour"] if j["hour"] is not None else -1,
                                 j["minute"] if isinstance(j["minute"], int)
                                 else j["minute"][0]))


def when(job: dict) -> str:
    """输入:一条调度 → 输出:人读的时间(台北时间,与机器本地时间同为 UTC+8)。"""
    if isinstance(job["minute"], (list, tuple)):
        return "每小时 " + "/".join(f":{m:02d}" for m in job["minute"])
    if job["hour"] is None:
        return f"每小时 :{job['minute']:02d}"
    hhmm = f"{job['hour']:02d}:{job['minute']:02d}"
    if job["weekday"]:
        return f"每周{_WEEK[job['weekday'] - 1]} {hhmm}"
    return f"每天 {hhmm}"


def cron(job: dict) -> str:
    """输入:一条调度 → 输出:5 段 cron(给认 cron 的那类定时任务界面用)。

    ⚠ **台北时间**。要是那家智能体的定时任务按 UTC 算,得自己减 8 小时
    —— 时区弄反的表现是"每天准时在错的时间跑",不报任何错。
    """
    m = job["minute"] if isinstance(job["minute"], int) \
        else ",".join(str(x) for x in job["minute"])
    h = "*" if job["hour"] is None else str(job["hour"])
    w = "*" if job["weekday"] is None else str(job["weekday"])
    return f"{m} {h} * * {w}"


def cron_utc(job: dict) -> str:
    """输入:一条调度 → 输出:同一时刻的 UTC cron(台北 −8 小时,跨日自动挪星期)。

    为什么直接给算好的:时区弄反是这套东西**唯一**会"每天准时在错的时间跑"
    而且不报任何错的故障(02:00 的备份跑到 10:00、13:00 的产品链跑到半夜)。
    让注册的人自己减 8 小时,迟早有一条减错 —— 尤其是跨日那三条:
    台北 02:00 / 06:40 / 07:30 对应 UTC **前一天**的 18:00 / 22:40 / 23:30,
    带 weekday 的还得把星期一起往前挪。这里算一次,两个都印出来。

    每小时那种(hour=None)UTC 与台北同形,原样返回。
    """
    m = job["minute"] if isinstance(job["minute"], int) \
        else ",".join(str(x) for x in job["minute"])
    if job["hour"] is None:
        w = "*" if job["weekday"] is None else str(job["weekday"])
        return f"{m} * * * {w}"
    back = job["hour"] - 8 < 0              # 减 8 之后落到前一天
    h = (job["hour"] - 8) % 24
    if job["weekday"] is None:
        w = "*"
    else:
        w = str((job["weekday"] - 1) % 7 if back else job["weekday"])
    return f"{m} {h} * * {w}"


def _logs_snippet() -> str:
    """日志目录**问代码要**,不要在文档里写死。

    DATA_ROOT 可被 `WALMART_DATA_ROOT` 覆盖(`registry/paths.py` 唯一出处),
    写死一个路径的表现是"照着做却 tail 到一个空目录",然后以为没日志。
    """
    return (f'cd {schedule.REPO_DIR} && '
            f'tail -n 60 "$({schedule.PYTHON} -c '
            f"'from registry import paths; print(paths.logs_dir())'"
            f')/<工作流名>.log"')


def command(job: dict) -> str:
    """输入:一条调度 → 输出:那一行要执行的命令(绝对路径,可直接粘)。"""
    parts = [schedule.PYTHON, f"{schedule.REPO_DIR}/cli.py",
             *job["workflows"]]
    for p in job["params"]:
        parts += ["-p", p]
    return " ".join(parts)


def _one_liner(name: str) -> str:
    """输入:工作流名 → 输出:它 docstring 第一行去掉"名字 — "前缀后的那句话。

    **不另写一份说明**:工程规范早就要求每个 workflow 的 docstring 第一行写清
    它干什么,拿它当唯一出处 ⇒ 改了代码说明自动跟着变。另写一份的话,两份
    迟早对不上,而**没有任何东西会报错**(这份技能包是给智能体看的提示词,
    对不上的表现是它照着过时的描述去判断该不该重跑)。

    取不到 docstring 时返回空串,由调用方决定要不要留白 —— 不编。
    """
    try:
        mod = importlib.import_module(f"workflows.{name}")
    except Exception:                                       # noqa: BLE001
        return ""
    head = ((mod.__doc__ or "").strip().split("\n") or [""])[0].strip()
    # docstring 第一行的形态是 "name — 做什么(附注)。",剥掉名字前缀
    for sep in ("—", "--", "-"):
        pre = f"{name} {sep} "
        if head.startswith(pre):
            head = head[len(pre):]
            break
    return head.strip()


def steps_table(job: dict) -> list[str]:
    """输入:一条调度 → 输出:这条链每一步干什么的表格行(取各 workflow 的 docstring)。"""
    rows = ["| 步 | 工作流 | 这一步干什么 |", "|---|---|---|"]
    for i, name in enumerate(job["workflows"], 1):
        rows.append(f"| {i} | `{name}` | {_one_liner(name) or '(见代码 docstring)'} |")
    return rows


def task_prompt(job: dict) -> str:
    """输入:一条调度 → 输出:粘进智能体「定时任务」提示词的那段话。

    写成对智能体说话的形态(它读到什么就该做什么),而不是对人的说明书 ——
    定时任务触发时没有人在旁边,提示词就是它当时能看到的全部上下文。
    """
    workflows = " → ".join(job["workflows"])
    lines = [
        f"# 沃尔玛定时任务:{job['label']}({when(job)},台北时间)",
        "",
        f"在 `{schedule.REPO_DIR}` 下执行这一行,**原样执行,不要改任何参数**:",
        "",
        "```bash",
        command(job),
        "```",
        "",
        f"这条链跑的是:{workflows}。",
        "",
        "## 这条链在做什么",
        "",
        *steps_table(job),
    ]
    if len(job["workflows"]) > 1:
        lines += ["", "**顺序是硬约束**:前一步不成功就不跑后面的,"
                      "整条链只发一条飞书通知。"]
    if job["note"]:
        lines += ["", f"备注:{job['note']}"]
    lines += [
        "",
        "## 跑完怎么判",
        "",
        "看**退出码**,不要靠读输出猜:",
        "",
        "- `0` 成功 —— **什么都不用做**。成功/失败飞书都会自己发通知,"
        "你再报一遍就是刷屏。",
        "- `3` 上一轮还在跑(没抢到锁)—— **不是失败,不要重试**。"
        "下一个整点它自己会再来一次。连着两次 3 才值得说一声。",
        "- `1` 失败 —— 见下。",
        "",
        "## 失败了怎么办",
        "",
        "1. 取日志末尾(工作流名 = 飞书通知里第一个 ❌ 的那一步):",
        "",
        "```bash",
        _logs_snippet(),
        "```",
        "",
        "2. 把**失败的那一步 + 日志最后那几行报错**发给苏里,一次说清。",
        "3. **不要自动重跑。** 这条链会写沃尔玛/写库,重跑的代价可能是重复提交;"
        "而且失败原因多半是外部的(凭证过期、代理不通、飞书表被改),重跑一遍还是那样。",
        "",
        "## 绝对不许做的三件事",
        "",
        "1. **不许加 `--dry-run`。** 缺省就是真跑;加了它每天空转,而且报成功 —— "
        "这是最难发现的一种坏法(真跑至少留痕迹)。",
        "2. **不许改工作流名、参数或顺序。** 要改就改 "
        "`registry/schedule.py` 再重新生成这份技能包,不要在提示词里手改 —— "
        "两处不一致时没有任何东西会报错。",
        "3. **不许并发跑第二条。** 同一条链撞上了后到的那条直接退 3 空跑一轮。",
    ]
    return "\n".join(lines) + "\n"


def _table() -> list[str]:
    """任务表。**两个 cron 都印**:平台按台北存就用第一个,按 UTC 存就用第二个。

    只给台北那一列、让人自己减 8 小时的话,跨日那三条(02:00 / 06:40 / 07:30
    → UTC 前一天)几乎注定有人减错,而时区弄反是唯一"每天准时在错的时间跑"
    且不报错的故障。
    """
    rows = ["| 任务 | 时间(台北) | cron(台北) | cron(UTC) | 跑什么 |",
            "|---|---|---|---|---|"]
    for j in jobs():
        rows.append(f"| `{j['label']}` | {when(j)} | `{cron(j)}` | "
                    f"`{cron_utc(j)}` | {' → '.join(j['workflows'])} |")
    return rows


def skill_md() -> str:
    """输入:无 → 输出:技能包总纲 SKILL.md 的全文。"""
    launchd_jobs = schedule.jobs_for("launchd")
    lines = [
        "---",
        "name: walmart-schedule",
        "description: 沃尔玛业务链的定时任务执行手册"
        "(每日/每周那部分;高频链在电脑 launchd 上,不归这里)",
        "---",
        "",
        "# 沃尔玛定时任务",
        "",
        "> **本文件是生成的,不要手改。** 出处是 `registry/schedule.py` 的 "
        "`JOBS`,改完跑 `python cli.py skill_export` 重新生成。",
        "> 手改会造成「提示词里写的」和「代码里定的」不一致,"
        "而这种不一致**没有任何东西会报错**。",
        "",
        "## 你要做的事",
        "",
        f"到点在 `{schedule.REPO_DIR}` 下跑一行命令,看退出码,失败了把日志发给苏里。",
        "就这些 —— 业务判断全在代码里,不需要你替它决定任何事。",
        "",
        "## 任务表",
        "",
        *_table(),
        "",
        "⚠ **时区:「时间」与「cron(台北)」两列是台北时间(UTC+8),"
        "和这台电脑的本地时间一致;平台按 UTC 存就用「cron(UTC)」那一列,"
        "**别自己减** —— 台北 02:00 / 06:40 / 07:30 对应的是 UTC **前一天**,"
        "带星期的还得把星期一起往前挪,已经替你算好了。时区弄反的表现是"
        "「每天准时在错的时间跑」,不报任何错(02:00 的备份跑到 10:00 去,"
        "13:00 的产品链跑到半夜)。",
        "",
        "每条任务的完整提示词在 `tasks/<任务名>.md`,注册定时任务时**整篇粘进去**。",
        "",
        "## 不归你管的(在电脑 launchd 上,别重复挂)",
        "",
        "| 任务 | 时间 | 跑什么 |",
        "|---|---|---|",
        *[f"| `{j['label']}` | {when(j)} | {' → '.join(j['workflows'])} |"
          for j in launchd_jobs],
        "",
        "⚠ **两边都挂 = 撞锁**。同一条链同时被 launchd 和你拉起来,"
        "后到的那次拿不到锁直接退出码 3 空跑一轮 —— 看起来一切正常。",
        "",
        "## 三条通用规则(每条任务都适用)",
        "",
        "### 退出码就是结论",
        "",
        "| 码 | 意思 | 你该做什么 |",
        "|---|---|---|",
        "| 0 | 成功 | 什么都不做(飞书通知它自己会发) |",
        "| 3 | 没抢到锁,上一轮还在跑 | **不是失败,不要重试**;连着两次才值得说一声 |",
        "| 1 | 失败 | 取日志末尾,连同失败的那一步一起发给苏里,**不要自动重跑** |",
        "| 2 | 工作流名写错了 | 说明提示词与 `registry/schedule.py` 脱节了,报给苏里 |",
        "",
        "### 不许加 `--dry-run`",
        "",
        "缺省即真跑(2026-08-16 定稿)。`--dry-run` 是给人改完代码后自己验的,"
        "**不进任何定时任务**:写进去的后果是那条链每天空转而且报成功,"
        "比误跑更难发现 —— 误跑至少留下痕迹。",
        "",
        "### 失败不自动重跑",
        "",
        "这些链会写沃尔玛、写数据库。重跑的代价可能是重复提交;"
        "而失败原因通常是外部的(凭证过期、代理不通、飞书表被人改了),"
        "重跑一遍还是同样的失败。",
        "",
        "## 出了怪事看哪里",
        "",
        "- **每次运行的记录**在库里 `ops.runs`(谁触发的、跑了多久、成没成、"
        "摘要全文)—— 想知道昨天那次到底跑没跑,看这张表,不要靠回忆。",
        "- **日志**一个工作流一份,目录问代码要(别写死,`WALMART_DATA_ROOT` "
        "能覆盖它):",
        "",
        "```bash",
        _logs_snippet(),
        "```",
        "",
        "- **通知**成功失败都会发飞书;一条链只发一条(不是每步一条)。",
    ]
    return "\n".join(lines) + "\n"


def register_md() -> str:
    """输入:无 → 输出:REGISTER.md —— 一次性把这几条注册成定时任务的交办话术。

    为什么这份也生成、不手写:注册话术里会出现任务条数、每条的时间和 cron。
    手写一份的话,以后改调度表(比如挪了个点、加了条链)没人会想起来回头改
    话术 —— 然后下一次照着旧话术注册,**注册出来的时间和代码里定的不一样,
    而没有任何东西会报错**。这正是 schedule.py 头注吃过两次的那个坑。

    受众是"要去注册的那个智能体",不是人:所以写成对它下的指令,
    并且**把校验步骤也写进去** —— 注册这件事本身没有退出码,
    唯一能证明注册对了的办法是让它把注册结果回读一遍给人对。
    """
    js = jobs()
    launchd_labels = ", ".join(f"`{j['label']}`"
                               for j in schedule.jobs_for("launchd"))
    lines = [
        f"# 交办:一次性注册这 {len(js)} 条沃尔玛定时任务",
        "",
        "> **本文件是生成的,不要手改**(出处 `registry/schedule.py`,"
        "改完跑 `python cli.py skill_export`)。把下面整篇给要注册的那个智能体。",
        "",
        "---",
        "",
        f"请把下面 {len(js)} 条定时任务一次性注册好。这几条是沃尔玛业务链的"
        "每日/每周调度,注册完之后由你到点触发。",
        "",
        "## 第 0 步:先把时区定了(**唯一一个会静默出错的地方**)",
        "",
        "先确认你这边的定时任务按哪个时区存:",
        "",
        "- 能按**台北时间(UTC+8)**存 → 用下表「cron(台北)」那一列;",
        "- 只能按 **UTC** 存 → 用「cron(UTC)」那一列,**已经替你算好了,"
        "不要自己减 8 小时**;",
        "- 两列都不确定 → **先别注册,回来问苏里**。",
        "",
        "为什么单独拎出来说:时区弄反的表现是「每天准时在错的时间跑」,"
        "不报任何错、不失败、通知照发 —— 02:00 的备份跑到上午 10 点去,"
        "13:00 的产品链跑到半夜。这套东西里只有这一处会这样坏。",
        "",
        "## 第 1 步:按这张表注册",
        "",
        "| 任务名 | 时间(台北) | cron(台北) | cron(UTC) | 提示词正文取自 |",
        "|---|---|---|---|---|",
        *[f"| `{j['label']}` | {when(j)} | `{cron(j)}` | `{cron_utc(j)}` | "
          f"`{SKILL_DIR}/tasks/{j['label']}.md` |" for j in js],
        "",
        "**任务名照抄第一列**,别改成中文、别加前缀 —— "
        "出问题时苏里是按这个名字对回 `registry/schedule.py` 的。",
        "",
        "## 第 2 步:每条的提示词 = 那个 md 文件的全文,原样粘",
        "",
        f"仓库在 `{schedule.REPO_DIR}`,九份提示词在 `{SKILL_DIR}/tasks/` 下。"
        "每条任务的提示词就是对应那份文件的**全文**。",
        "",
        "- **不要改写、不要摘要、不要「提炼要点」。** 那些文件里看起来啰嗦的部分"
        "(退出码 3 不算失败、失败不许自动重跑、不许加 `--dry-run`)都是踩过的坑,"
        "删一条就复活一个。",
        "- **不要自己合并任务。** 同一时间的两条(15:00 有两条)也分开注册 —— "
        "合起来跑会撞锁,后到的那条直接空跑一轮还报成功。",
        "- **读不到文件就停下来问苏里要**,不要凭任务名自己编一段提示词。"
        "(他这一行就能把九份全导出来给你:"
        f"`cat {SKILL_DIR}/tasks/*.md`,每份以 `# 沃尔玛定时任务:<任务名>` 开头,"
        "按这行切开即可。)",
        "",
        "## 第 3 步:这两条不要注册",
        "",
        f"{launchd_labels} 是高频链,已经挂在苏里电脑的 launchd 上。",
        "**你这边再挂一份 = 两边同时拉同一条链**,后到的那次拿不到锁直接退出码 3 "
        "空跑,而外表看起来一切正常。",
        "",
        "同样地:**上面表里没有的,一条都不要注册**。想加是改 "
        "`registry/schedule.py` 再重新生成这份交办单,不是在你这边手动加一条。",
        "",
        "## 第 4 步:别动现有的任务",
        "",
        "你这边可能还有旧 erpAPI 时期的定时任务。**不要删、不要改、不要顺手"
        "「清理重复的」** —— 旧调度的停用由苏里自己按顺序来"
        "(先停旧的、搬状态、再起新的),你替他删会让新旧两套在同一件破坏性任务上"
        "错位。有拿不准的就列出来问,不要动手。",
        "",
        "## 第 5 步:注册完回读一遍(**这步不许省**)",
        "",
        "注册这件事本身没有退出码,所以唯一能证明它对了的办法是回读。"
        "注册完请把结果按下面这个样子列出来发给苏里:",
        "",
        "```",
        "已注册 N 条,时区=<台北 | UTC>",
        "任务名 | 存进去的 cron | 提示词第一行",
        "```",
        "",
        f"苏里会拿它和上面那张表逐行对。**条数必须正好 {len(js)} 条** —— "
        "多出来的是重复注册(会撞锁),少掉的是那条链从此每天不跑而没人知道。",
    ]
    return "\n".join(lines) + "\n"


def files() -> dict[str, str]:
    """输入:无 → 输出:{相对路径: 文件内容},技能包全套。"""
    out = {f"{SKILL_DIR}/SKILL.md": skill_md(),
           f"{SKILL_DIR}/REGISTER.md": register_md()}
    for j in jobs():
        out[f"{SKILL_DIR}/tasks/{j['label']}.md"] = task_prompt(j)
    return out
