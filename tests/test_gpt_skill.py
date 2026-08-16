"""智能体定时任务技能包回归(所有者定稿 2026-08-16 的调度分工)。

高频链(feed 轮询 + 订单链)在电脑 launchd 上;**其余每日/每周一次的**用
skill 形式注册成智能体的定时任务 ——「前期稳定,也方便我维护和调整,
以后换个智能体也能用」。

这里钉的全是"错了也不报错"的那类:提示词与调度表脱节、把 `--dry-run` 写进
定时任务(每天空转报成功)、参数掉了(那一段白跑)、两个 runner 挂同一条链。
"""

from pathlib import Path

import pytest

from registry import schedule
from services import gpt_skill

_REPO = Path(__file__).resolve().parent.parent


def test_repo_copy_matches_the_schedule_table():
    """⚠ 仓库里的技能包必须与 `registry/schedule.JOBS` 现值一致。

    这是整个设计的命门:提示词是**给智能体看的调度表副本**,而副本与正本
    不一致时,没有任何东西会报错 —— 它会每天准时跑一条已经被改掉的命令。
    红了就跑 `python cli.py skill_export`。
    """
    for rel, want in gpt_skill.files().items():
        p = _REPO / rel
        assert p.exists(), f"{rel} 没生成(跑 python cli.py skill_export)"
        assert p.read_text(encoding="utf-8") == want, \
            f"{rel} 与调度表不一致(跑 python cli.py skill_export 重新生成)"


def test_no_stale_task_files_left_behind():
    """从调度表删掉一条任务,它的提示词文件也得跟着没 —— 否则那条会一直被跑。"""
    on_disk = {p.name for p in (_REPO / gpt_skill.SKILL_DIR / "tasks").glob("*.md")}
    assert on_disk == {f"{j['label']}.md" for j in gpt_skill.jobs()}


@pytest.mark.parametrize("job", gpt_skill.jobs(), ids=lambda j: j["label"])
def test_every_task_prompt_is_runnable_as_written(job):
    md = gpt_skill.task_prompt(job)
    cmd = gpt_skill.command(job)
    assert cmd in md                       # 命令原样出现在提示词里
    assert cmd.startswith(schedule.PYTHON)  # venv 的解释器,绝对路径
    assert f"{schedule.REPO_DIR}/cli.py" in cmd
    # ⚠ 定时任务里出现 --dry-run = 那条链每天空转而且报成功
    assert "--dry-run" not in cmd
    # 三条纪律每篇都得在(智能体触发时看到的只有这一篇)
    assert "不许加 `--dry-run`" in md
    assert "不要自动重跑" in md
    assert "`3`" in md and "不是失败" in md      # 退出码 3 = 锁,不是失败


def test_params_that_must_not_be_dropped_are_present():
    """两个"漏了就每天空转而且报成功"的参数,写进调度表就别再掉。

    · product_refresh:wait=1 —— 不等采集落定,product_ingest 摄的是上一轮数据;
    · order_asin_normalize:apply=1 —— 那个工作流缺省是**预览**。
    两者都不报错,只是那一段白跑,正是"缺省即真跑"这条定稿要消灭的东西。
    """
    cmds = {j["label"]: gpt_skill.command(j) for j in gpt_skill.jobs()}
    assert "-p product_refresh:wait=1" in cmds["product_chain"]
    assert "-p order_asin_normalize:apply=1" in cmds["order_daily"]


def test_cron_is_taipei_and_says_so():
    """时区弄反的表现是"每天准时在错的时间跑",不报任何错 —— 所以必须写明。"""
    by = {j["label"]: j for j in gpt_skill.jobs()}
    assert gpt_skill.cron(by["product_clear"]) == "0 15 * * *"
    assert gpt_skill.cron(by["settlement"]) == "0 8 * * 3"     # 3 = 周三
    assert gpt_skill.when(by["settlement"]) == "每周三 08:00"
    md = gpt_skill.skill_md()
    assert "台北" in md and "UTC" in md


def test_skill_lists_the_launchd_side_as_off_limits():
    """总纲必须点名电脑上那两条 —— 不点名就会被"顺手也注册一个"。"""
    md = gpt_skill.skill_md()
    for j in schedule.jobs_for("launchd"):
        assert j["label"] in md
    assert "撞锁" in md


def test_settlement_stays_weekly_because_no_scheduler_does_biweekly():
    """账期双周发布,而 launchd 和 cron 都没有"双周"。

    每周三跑一次是安全的:已入库账期**永不重拉**(DISTINCT period + recon_done
    台账),没有新账期那轮就是空转。用频率换掉一个调度器表达不了的周期。
    """
    job = next(j for j in schedule.JOBS if j["label"] == "settlement")
    assert (job["weekday"], job["hour"], job["minute"]) == (3, 8, 0)
    assert "永不重拉" in __import__("workflows.settlement_sync",
                                    fromlist=["x"]).__doc__


def test_export_dry_run_writes_nothing(tmp_path):
    from workflows import skill_export
    out = skill_export.run({"execute": True, "dry_run": True,
                            "dest": str(tmp_path)})
    assert "DRY-RUN" in out and "未写任何文件" in out
    assert list(tmp_path.rglob("*.md")) == []
    # 真跑一次:写出来的与渲染一致,再跑一次就"无需改动"(幂等)
    skill_export.run({"execute": True, "dest": str(tmp_path)})
    assert (tmp_path / f"{gpt_skill.SKILL_DIR}/SKILL.md").read_text(
        encoding="utf-8") == gpt_skill.skill_md()
    assert "无需改动" in skill_export.run({"execute": True,
                                           "dest": str(tmp_path)})
