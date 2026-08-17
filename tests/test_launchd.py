"""launchd 调度回归。

调度表在 `registry/schedule.JOBS`(唯一出处),渲染在 `services/launchd`。
本文件钉住的是**那四个坑**——每一条错了都表现成"这条链每天什么都不做,
而且不报错",没有一条会在测试之外被发现。
"""

import plistlib
from pathlib import Path

import pytest

from registry import schedule
from services import launchd

_LOGS = Path("/tmp/logs/launchd")


def _rendered(job):
    return plistlib.loads(launchd.render(job, _LOGS))


_LAUNCHD = schedule.jobs_for("launchd")


@pytest.mark.parametrize("job", _LAUNCHD, ids=lambda j: j["label"])
def test_every_job_renders_a_valid_plist(job):
    d = _rendered(job)
    assert d["Label"].startswith("com.walmartapi.")
    # 坑 ①:ProgramArguments 不过 shell —— 任何一项都不许含 shell 元字符
    for a in d["ProgramArguments"]:
        assert not any(c in a for c in "&|><*~$"), a
    # 坑 ②:launchd 不读 shell 配置,PATH 必须显式给
    assert d["EnvironmentVariables"]["PATH"]
    assert d["EnvironmentVariables"]["WALMART_OPERATOR"] == "launchd"
    # 坑 ③:StandardOutPath 不能省 —— 解释器路径写错/venv 被删/import 期就炸
    # 只会出现在这里,cli 的 logs/<工作流>.log 那时还没被创建
    assert d["StandardOutPath"] and d["StandardErrorPath"]
    # 装载那一刻不许把破坏性链拉起来
    assert d["RunAtLoad"] is False


def test_interpreter_and_cli_are_absolute():
    """launchd 不做变量展开也不认 `~` —— 相对路径的表现是静默什么都不跑。"""
    for job in _LAUNCHD:
        py, cli, *_ = launchd.program_args(job)
        assert py.startswith("/") and py.endswith("python3")
        assert cli.startswith("/") and cli.endswith("cli.py")
    assert schedule.REPO_DIR.startswith("/")


def test_hourly_job_has_no_hour_key():
    """坑 ④:每小时那条给 Minute **不给 Hour**。

    再单挂一个整点的 plist 会撞车 —— 两个各拿各的锁,后到的整链退 3 空跑一轮。
    所以每小时的链只能有这一个 plist。
    """
    job = next(j for j in _LAUNCHD if j["label"] == "order_chain")
    cal = _rendered(job)["StartCalendarInterval"]
    assert cal == {"Minute": 20}                # 没有 Hour = 每小时
    # 而且全表里不许再有第二条跑同一批工作流的
    same = [j for j in schedule.JOBS
            if set(j["workflows"]) & {"order_sync"} and j is not job]
    assert same == []


def test_half_hourly_job_renders_a_list():
    job = next(j for j in _LAUNCHD if j["label"] == "feed_poll")
    assert _rendered(job)["StartCalendarInterval"] == [{"Minute": 0},
                                                       {"Minute": 30}]


def test_every_workflow_in_the_table_actually_exists():
    """⚠ 调度表里打错一个工作流名,launchd 会**每天准时失败**。

    cli 会在跑第一步之前验名(退出码 2),但那是运行期;这条在提交期就挡住。
    """
    import importlib
    for job in schedule.JOBS:
        for name in job["workflows"]:
            importlib.import_module(f"workflows.{name}")


def test_dangerous_chains_carry_no_dry_run_flag():
    """⚠ plist 里**不许**出现 --dry-run:缺省即真跑,写了它就是每天空转报成功。"""
    for job in _LAUNCHD:
        assert "--dry-run" not in launchd.program_args(job)


def test_manual_only_workflows_are_not_scheduled():
    """所有者定稿:上架/跟卖/分配/审核/自愈一律手动,不进调度。"""
    scheduled = {w for j in schedule.JOBS for w in j["workflows"]}
    for name in ("list_new", "match_listing", "sku_locked_heal",
                 "product_audit", "scrape_missing", "brand_scrape",
                 "alloc_plan", "alloc_backfill", "order_center_push"):
        assert name not in scheduled, name


def test_only_the_high_frequency_chains_live_on_this_machine():
    """所有者定稿 2026-08-16:电脑上只留高频链,其余交给智能体定时任务。

    ⚠ 同一条工作流**绝不许两个 runner 都挂**:撞上了后到的那次拿不到 flock
    直接退 3 —— 那一轮什么都没做,而通知里写的是"⚠ 已有实例在运行",
    看久了就当常态了。
    """
    assert {j["label"] for j in _LAUNCHD} == {"feed_poll", "order_chain"}
    mine = {w for j in _LAUNCHD for w in j["workflows"]}
    theirs = {w for j in schedule.jobs_for("gpt") for w in j["workflows"]}
    assert mine & theirs == set()
    # runner 只有这两个值;打错一个字(比如 "GPT")在 job() 里就炸
    assert {j["runner"] for j in schedule.JOBS} <= set(schedule.RUNNERS)


def test_batches_match_the_greyscale_plan():
    """分三批灰度是所有者定的节奏 —— 批号只有 1/2/3,且破坏性链都在批 3。"""
    assert {j["batch"] for j in schedule.JOBS} == {1, 2, 3}
    dangerous = {"product_chain", "product_clear"}
    for j in schedule.JOBS:
        if j["label"] in dangerous:
            assert j["batch"] == 3, j["label"]
        elif j["batch"] == 3:
            raise AssertionError(f"批 3 只放破坏性链,{j['label']} 不该在这里")
