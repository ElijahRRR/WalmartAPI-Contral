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
