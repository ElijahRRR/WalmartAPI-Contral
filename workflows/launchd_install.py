"""launchd_install — 生成高频链的 launchd plist(危险:装完就是真调度)。

⚠ **只管 `runner="launchd"` 那几条**(所有者定稿 2026-08-16:feed 轮询 +
订单链这种每半小时/每小时的东西写死在电脑上最稳)。其余每日/每周一次的
归智能体定时任务,跑 `python cli.py skill_export` 生成那份技能包。
**同一条链绝不许两边都挂** —— 撞上了后到的退 3 空跑一轮,而且报"成功"。

用法:
  python cli.py launchd_install --dry-run     # 只打印将生成什么,不落盘 ← **先跑这个**
  python cli.py launchd_install               # 写 ~/Library/LaunchAgents/
  python cli.py launchd_install -p batch=1    # 只写第 1 批(灰度上线)
  python cli.py launchd_install -p dest=/tmp/plists   # 写到别处先看
  python cli.py launchd_install -p uninstall=1        # 打印卸载命令(不自动执行)

调度表在 `registry/schedule.JOBS`(唯一出处),渲染在 `services/launchd`。

⚠ **本工作流只写文件,不自动 `launchctl load`。** 上线是分三批灰度的
(docs/schedule_plan.md §十),一把全 load 就没有"每批观察一天"这回事了;
而且批二开之前必须先停旧 KPI 与旧订单同步(**两条同停**,否则双跑)。
所以它把 load 命令打印出来,由你按批执行。

⚠ 写出来的 plist 里全是**绝对路径**(解释器/仓库/日志),来自
`registry.schedule` —— 换机器只改那一处。
"""

import logging
import os
from pathlib import Path

from registry import paths, schedule
from services import launchd

DANGEROUS = True        # 装完就是真调度,值得走 dry-run 那道纪律

logger = logging.getLogger("workflows.launchd_install")

_AGENTS = Path.home() / "Library" / "LaunchAgents"


def _jobs(batch: str):
    # ⚠ 只渲染 runner="launchd" 那些。其余归智能体定时任务(skill_export)——
    # 两边都挂同一条链 = 撞锁,后到的退 3 空跑一轮而且看起来一切正常
    mine = schedule.jobs_for("launchd")
    if not batch:
        return mine
    want = {int(b) for b in str(batch).split(",") if b.strip()}
    return [j for j in mine if j["batch"] in want]


def _load_cmds(jobs) -> list[str]:
    """按批打印 launchctl 命令 —— 分批是所有者定的灰度节奏,不是可选项。"""
    lines = []
    for b in sorted({j["batch"] for j in jobs}):
        lines.append(f"  # 批 {b}")
        for j in [x for x in jobs if x["batch"] == b]:
            lines.append(f"  launchctl load -w "
                         f"{_AGENTS}/{launchd.label_of(j)}.plist")
    return lines


def run(params: dict) -> str:
    execute = bool(params.get("execute"))
    jobs = _jobs(str(params.get("batch", "")).strip())
    if not jobs:
        return f"batch 参数没匹配到任何调度(可用批次 1/2/3):{params.get('batch')}"
    dest = Path(params.get("dest") or _AGENTS)
    log_dir = paths.logs_dir() / "launchd"

    if params.get("uninstall"):
        # 只打印,不代执行:卸载会让那条链**立刻停跑**,得由人确认
        return "\n".join(
            ["卸载(逐条确认后执行;卸载 = 那条链立刻停跑):"]
            + [f"  launchctl unload -w {dest}/{launchd.label_of(j)}.plist"
               for j in jobs]
            + ["", "文件本身不删 —— 想彻底清掉再 rm 那些 .plist"])

    lines = [f"{'' if execute else '🧪 [DRY-RUN] '}调度 {len(jobs)} 条 "
             f"→ {dest}", f"  解释器 {schedule.PYTHON}",
             f"  launchd 自身日志 {log_dir}(⚠ 与 cli 的 logs/<工作流>.log 不是"
             f"同一份:解释器路径写错/venv 被删/import 期就炸只会出现在这里)", ""]
    for j in jobs:
        lines.append(f"  [批{j['batch']}] {launchd.human(j)}")
        if j["note"]:
            lines.append(f"           {j['note']}")

    if not execute:
        lines += ["", "(dry-run:未写任何文件;去掉 --dry-run 才落盘)",
                  "落盘后按批执行:"] + _load_cmds(jobs)
        return "\n".join(lines)

    dest.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    for j in jobs:
        p = launchd.plist_path(j, dest)
        p.write_bytes(launchd.render(j, log_dir))
        logger.info("写 %s", p)
    lines += ["", f"已写 {len(jobs)} 个 plist。**还没生效** —— 按批执行:"]
    lines += _load_cmds(jobs)
    lines += ["", "⚠ 批二开之前先停旧 KPI 与旧订单同步(两条同停,见 "
              "docs/legacy_schedules.md);批三每条先手动 --dry-run 人眼确认",
              "看某条跑没跑:launchctl list | grep com.walmartapi"]
    lines += _yingdao_lines(dest, log_dir)
    return "\n".join(lines)


def _yingdao_lines(dest: Path, log_dir: Path) -> list[str]:
    """影刀启动代理:落盘 + 给出 load 命令(**不定时**,只等 kickstart)。

    它不在调度表里 —— 它没有时间点,是 daily_report 写完 input.json 之后
    按需 kickstart 的一个跳板。存在的唯一理由是**跨越进程上下文**:
    日报链跑在智能体上下文里,那里没有 Aqua session,直接 spawn 影刀
    必崩(2026-09-01 崩溃报告实证);launchd 的 gui/<uid> 有,所以让它代劳。
    """
    uuid = os.environ.get("YINGDAO_ROBOT_UUID", "").strip()
    app = paths.yingdao_app()
    out = ["", "── 影刀启动代理(不定时,只等 daily_report 按需 kickstart)──"]
    if not uuid:
        return out + ["  ⚠ YINGDAO_ROBOT_UUID 未配置,跳过 —— 配好再跑一次本工作流"]
    p = Path(dest) / f"{launchd.YINGDAO_LABEL}.plist"
    p.write_bytes(launchd.render_yingdao(str(app), uuid, log_dir))
    logger.info("写 %s", p)
    if not app.exists():
        out.append(f"  ⚠ 影刀主程序不在 {app}(装在别处就设 env YINGDAO_APP "
                   f"后重跑本工作流;plist 里那条路径是写死的)")
    return out + [
        f"  已写 {p}",
        f"  launchctl load -w {p}",
        "  ⚠ **必须在图形登录会话里 load**(plist 声明 LimitLoadToSessionType=Aqua):",
        "     ssh 进去 load 会装不上,而症状是日报那边 kickstart 退非 0。",
        f"  自检:launchctl kickstart -k gui/$(id -u)/{launchd.YINGDAO_LABEL}"
        " —— 影刀应当弹出 Runner 窗口",
    ]
