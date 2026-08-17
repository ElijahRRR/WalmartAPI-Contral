"""skill_export — 生成智能体定时任务的技能包(每日/每周那部分调度)。

用法:
  python cli.py skill_export --dry-run    # 只看会写哪些文件、内容变没变 ← 先跑这个
  python cli.py skill_export              # 写进仓库 skills/walmart-schedule/
  python cli.py skill_export -p dest=/tmp/skill    # 写到别处先看

所有者定稿 2026-08-16:高频链(feed 轮询 + 订单链)进电脑 launchd
(`launchd_install`),**其余每日/每周一次的用 skill 形式给智能体注册成定时任务**。
本工作流渲染的就是后半部分:一份 SKILL.md 总纲 + 每条任务一份提示词。

调度表在 `registry/schedule.JOBS`(唯一出处),渲染在 `services/gpt_skill`。
产物进 git —— 它是文档,而且必须跟着调度表一起改;`tests/test_gpt_skill.py`
会比对"仓库里的"和"从调度表现渲染的"是否一致,漂了就红。

⚠ 生成完**还没生效**:得由人把 `tasks/<任务名>.md` 逐条粘进智能体的定时任务里。
这一步故意不自动化 —— 注册定时任务等于把破坏性链接上秒表,值得人过一次眼。
"""

import logging
from pathlib import Path

from registry import paths
from services import gpt_skill

DANGEROUS = False       # 只写仓库内的 md,不碰沃尔玛也不碰库

logger = logging.getLogger("workflows.skill_export")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def run(params: dict) -> str:
    # ⚠ DANGEROUS=False ⇒ cli 把 execute 恒置真(它没有写接口可关),
    # `--dry-run` 是单独透传的那个键。只看 execute 的话 --dry-run 对本工作流
    # 完全无效而且不报错(与 maintenance_scan 同款处理)
    execute = bool(params.get("execute")) and not params.get("dry_run")
    dest = Path(params["dest"]) if params.get("dest") else _repo_root()
    want = gpt_skill.files()

    changed, same = [], 0
    for rel, text in sorted(want.items()):
        p = dest / rel
        old = p.read_text(encoding="utf-8") if p.exists() else None
        if old == text:
            same += 1
        else:
            changed.append((rel, "新增" if old is None else "改动"))

    mode = "" if execute else "🧪 [DRY-RUN] "
    jobs = gpt_skill.jobs()
    lines = [f"{mode}智能体定时任务技能包:{len(jobs)} 条任务 → {dest / gpt_skill.SKILL_DIR}"]
    for j in jobs:
        lines.append(f"  {gpt_skill.when(j):<14} {' → '.join(j['workflows'])}")
    lines.append("")
    if not changed:
        lines.append(f"  {same} 个文件与调度表一致,无需改动")
        return "\n".join(lines)
    lines.append(f"  {len(changed)} 个文件要写(另 {same} 个无变化):")
    lines += [f"    [{how}] {rel}" for rel, how in changed]

    if not execute:
        lines += ["", "(dry-run:未写任何文件;去掉 --dry-run 才落盘)"]
        return "\n".join(lines)

    for rel, _ in changed:
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(want[rel], encoding="utf-8")
        logger.info("写 %s", p)

    lines += [
        "",
        "已写盘。**还没生效** —— 注册这一步交办给智能体:",
        f"  把 {gpt_skill.SKILL_DIR}/REGISTER.md **整篇**发给它(一次性注册全套的话术)",
        f"  每条的提示词正文在 {gpt_skill.SKILL_DIR}/tasks/<任务名>.md,让它整篇粘、不要改写",
        "  时间用 SKILL.md 任务表里对应时区那一列(台北/UTC 都算好了,别自己减)",
        "",
        "⚠ 注册完两件事必须做:手动跑一遍同一条命令确认能跑通;"
        "让它把注册结果回读一遍逐行对表",
        f"  日志与运行记录:{paths.logs_dir()} 与库里 ops.runs",
    ]
    return "\n".join(lines)
