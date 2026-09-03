"""launchd plist 渲染(macOS 定时调度)。调度表在 `registry/schedule`。

只做**渲染与落盘**,不自动 `launchctl load` —— 上线是分三批灰度的
(schedule_plan §十),一把全 load 就没有"每批观察一天"这回事了。
本模块把命令打印出来,由人按批执行。

plist 的四个坑(每一条都对应一种"这条链每天什么都不做"的故障):

  ① `ProgramArguments` **不过 shell**:没有 `&&`、`~`、通配符、变量展开。
     串联只能靠 cli 的多工作流参数(这也是当初把串联做进 cli 的原因之一)。
  ② launchd **不读 shell 配置**:`~/.zshrc` 的 PATH/export 一概不生效。
     所以 PATH 要在 plist 里显式给(registry.schedule.ENV)。
  ③ `StandardOutPath` **不能省**,即使 cli 已经写 logs/<workflow>.log ——
     两者盖的不是同一段:cli 的日志从"Python 起来了"才开始,而**解释器路径
     写错 / venv 被删 / import 期就炸**发生在那之前,只会出现在这里。
     没有它,故障表现是"这条链每天什么都不做,日志里一个字也没有"。
  ④ 一条链只挂**一个** plist。每小时那条给 Minute 不给 Hour 就是每小时;
     再单挂一个整点的会撞车,后到的整链拿不到锁退 3。
"""

import plistlib
from pathlib import Path

from registry import schedule


def label_of(job: dict) -> str:
    return schedule.LABEL_PREFIX + job["label"]


def plist_path(job: dict, dest: Path) -> Path:
    return Path(dest) / f"{label_of(job)}.plist"


def program_args(job: dict) -> list[str]:
    """输入:一条调度 → 输出:ProgramArguments 数组(纯函数,可测)。"""
    args = [schedule.PYTHON, f"{schedule.REPO_DIR}/cli.py", *job["workflows"]]
    for p in job["params"]:
        args += ["-p", p]
    return args


def calendar(job: dict):
    """输入:一条调度 → 输出:StartCalendarInterval 的值(dict 或 dict 列表)。

    minute 给列表 ⇒ 一天内多次(每 30 分那种);hour=None ⇒ **每小时**的那一分钟。
    """
    minutes = job["minute"] if isinstance(job["minute"], (list, tuple)) \
        else [job["minute"]]
    out = []
    for m in minutes:
        d: dict = {"Minute": int(m)}
        if job["hour"] is not None:
            d["Hour"] = int(job["hour"])
        if job["weekday"] is not None:
            d["Weekday"] = int(job["weekday"])
        out.append(d)
    return out[0] if len(out) == 1 else out


def render(job: dict, logs_dir: Path) -> bytes:
    """输入:一条调度 + launchd 日志目录 → 输出:plist 字节串。"""
    label = label_of(job)
    body = {
        "Label": label,
        "ProgramArguments": program_args(job),
        "WorkingDirectory": schedule.REPO_DIR,
        "EnvironmentVariables": dict(schedule.ENV),
        "StartCalendarInterval": calendar(job),
        "StandardOutPath": str(Path(logs_dir) / f"{job['label']}.out"),
        "StandardErrorPath": str(Path(logs_dir) / f"{job['label']}.err"),
        # 装载时不立刻跑一遍 —— 否则 `launchctl load` 那一刻就把破坏性链拉起来了
        "RunAtLoad": False,
    }
    return plistlib.dumps(body, sort_keys=False)


# ── 影刀启动代理(2026-09-01 生产崩溃后加)────────────────────────────────
# 这个 agent **没有 StartCalendarInterval**:它不定时,只等 `launchctl kickstart`
# 按需拉起(daily_report 写完 input.json 的下一秒)。
#
# ⚠ 为什么非得绕 launchd,不能由 daily_report 直接 spawn(崩溃报告实证
# 2026-09-01,incident 6B391891):日报链的 runner 是 `gpt`,进程跑在智能体的
# 上下文里(crash log:coalitionName=com.openai.codex、responsibleProc=ChatGPT、
# procRole=Unspecified)。影刀是 Electron/AppKit 应用,启动时要向 LaunchServices
# 注册,那个上下文里没有 Aqua GUI session ⇒ `_RegisterApplication` → abort()
# → SIGABRT。同一条命令在终端里手敲**完全正常**(终端有 GUI session)。
# 2026-08-24 那次 `open` 退 1 是**同一个根因的另一种表现**(分发被拦),
# 当时换成直启主程序 = 换汤不换药,从"起不来"变成"崩溃"。
# 任何 argv/URL 写法都救不了 —— 启动动作必须由**本来就在 Aqua session 里**的
# 东西发起,而 launchd 的 gui/<uid> 域正是它。
YINGDAO_LABEL = schedule.LABEL_PREFIX + "yingdao"


def render_yingdao(app: str, robot_uuid: str, logs_dir: Path) -> bytes:
    """输入:影刀主程序路径 + 机器人 UUID + 日志目录 → 输出:plist 字节串。

    `LimitLoadToSessionType: Aqua` 是这份 plist 的**要害**:它声明本 agent 只
    在图形会话里装载,launchd 于是把子进程放进那个会话 —— 正是崩溃时缺的东西。
    """
    return plistlib.dumps({
        "Label": YINGDAO_LABEL,
        "ProgramArguments": [app, f"shadowbot:Run?robot-uuid={robot_uuid}"],
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(Path(logs_dir) / "yingdao.out"),
        "StandardErrorPath": str(Path(logs_dir) / "yingdao.err"),
        # 不定时、不常驻、装载时也不跑:唯一的触发是 launchctl kickstart
        "RunAtLoad": False,
        "KeepAlive": False,
    }, sort_keys=False)


def human(job: dict) -> str:
    """输入:一条调度 → 输出:人读的一行(什么时候 / 跑什么)。"""
    if isinstance(job["minute"], (list, tuple)):
        when = "每小时 " + "/".join(f":{m:02d}" for m in job["minute"])
    elif job["hour"] is None:
        when = f"每小时 :{job['minute']:02d}"
    else:
        wd = f"周{'一二三四五六日'[job['weekday'] - 1]} " if job["weekday"] else ""
        when = f"{wd}{job['hour']:02d}:{job['minute']:02d}"
    cmd = " ".join(job["workflows"])
    tail = "".join(f" -p {p}" for p in job["params"])
    return f"{when:<12} {cmd}{tail}"
