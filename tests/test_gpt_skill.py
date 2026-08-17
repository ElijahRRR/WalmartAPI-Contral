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


def test_skill_says_the_launchd_pair_still_needs_installing(monkeypatch):
    """⚠ 这一节**不能写成「不归你管」**(所有者 2026-08-17 指出逻辑不对)。

    原文是「不归你管的(在电脑 launchd 上,别重复挂)」—— 它把"已经装好了"
    当成既成事实陈述,而实际上装 launchd 那一步本身就还没做。两个后果:
      · 读到这一节的人/智能体不会去装;
      · 而**没装的表现和"别人在管"长得一模一样** —— 所有已注册的任务照常报
        成功,只有飞书上的「处理中」永远不消失、日报订单列空着。

    所以总纲必须做到三件事:点名那两条(否则被顺手注册一个 → 撞锁)、
    说清装法在哪、给出一条能自己查"到底装没装"的命令。
    """
    md = gpt_skill.skill_md()
    for j in schedule.jobs_for("launchd"):
        assert j["label"] in md
    assert "撞锁" in md                       # 装好之后别再挂一份
    assert "不归你管" not in md                # ⚠ 就是这句话不许再出现
    assert "REGISTER.md" in md                # 装法指得出去
    assert "launchctl list" in md             # 能自己查装没装
    assert "从来不跑" in md                    # 没装的后果说明白


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

def test_step_table_comes_from_the_workflow_docstrings():
    """每一步"干什么"取各 workflow 的 docstring 第一行,**不另写一份说明**。

    另写一份的话两份迟早对不上,而**没有任何东西会报错** —— 这份技能包是给
    智能体看的提示词,对不上的表现是它照着过时的描述去判断该不该重跑。
    """
    from workflows import risk_sync
    rows = gpt_skill.steps_table(
        {"workflows": ["risk_sync", "blacklist_push"]})
    head = risk_sync.__doc__.strip().split("\n")[0]
    # 名字前缀被剥掉,正文一字不改地进表
    assert "risk_sync —" not in rows[2]
    assert head.split("— ", 1)[1] in rows[2]
    assert rows[0].startswith("| 步 | 工作流")


def test_step_table_never_invents_a_description():
    """取不到 docstring 时留"见代码"而不是编一句 —— 编的那句会被当成事实。"""
    rows = gpt_skill.steps_table({"workflows": ["根本不存在的工作流"]})
    assert "(见代码 docstring)" in rows[2]


def test_multi_step_prompt_states_the_order_is_binding():
    """多步链要明写"前一步不成功就不跑后面的" —— 智能体不知道 cli 的串联语义,
    看不到这句会以为某一步失败了可以单独重跑那一步。"""
    multi = next(j for j in gpt_skill.jobs() if len(j["workflows"]) > 1)
    single = next(j for j in gpt_skill.jobs() if len(j["workflows"]) == 1)
    assert "顺序是硬约束" in gpt_skill.task_prompt(multi)
    assert "顺序是硬约束" not in gpt_skill.task_prompt(single)


def test_utc_cron_is_precomputed_including_the_day_rollover():
    """⚠ 时区弄反是唯一"每天准时在错的时间跑"且不报错的故障。

    所以两个 cron 都印、**不让人自己减 8 小时**:台北 02:00 / 06:40 / 07:30
    对应 UTC **前一天**,带星期的还得把星期一起往前挪 —— 让人手算,跨日那三条
    几乎注定有一条减错。
    """
    got = {j["label"]: gpt_skill.cron_utc(j) for j in gpt_skill.jobs()}
    assert got["backup"] == "0 18 * * *"          # 台北 02:00 → UTC 前一天 18:00
    assert got["daily_report"] == "40 22 * * *"   # 06:40 → 前一天 22:40
    assert got["product_chain"] == "0 5 * * *"    # 13:00 → 同日 05:00
    assert got["audit_sheet"] == "10 10 * * *"
    assert got["list_new"] == "0 12 * * *"
    # 周三 08:00 台北 = 周三 00:00 UTC —— 刚好不跨日,星期不动
    assert got["settlement"] == "0 0 * * 3"
    # 表里两列都在
    md = gpt_skill.skill_md()
    assert "cron(台北)" in md and "cron(UTC)" in md
    assert "别自己减" in md


def test_utc_cron_moves_the_weekday_back_when_it_crosses_midnight():
    """周一 02:00 台北 = **周日** 18:00 UTC —— 星期不跟着挪就是整整差一天。"""
    job = schedule.job("x", ["backup"], batch=1, hour=2, minute=0,
                       weekday=1, runner="gpt")
    assert gpt_skill.cron_utc(job) == "0 18 * * 0"
    job0 = schedule.job("y", ["backup"], batch=1, hour=2, minute=0,
                        weekday=0, runner="gpt")
    assert gpt_skill.cron_utc(job0) == "0 18 * * 6"     # 周日 → 周六


# ── 交办单 REGISTER.md ──────────────────────────────────────────────────────
#
# 它也是生成的(受 test_repo_copy_matches_the_schedule_table 管),这里钉的是
# 它必须**自带校验**:注册定时任务这件事没有退出码,注册漏一条的表现是
# 那条链从此每天不跑而没有任何东西会说一声。

def test_register_lists_every_gpt_job_with_both_crons_and_its_prompt_file():
    md = gpt_skill.register_md()
    for j in gpt_skill.jobs():
        assert f"`{j['label']}`" in md
        assert f"`{gpt_skill.cron(j)}`" in md
        assert f"`{gpt_skill.cron_utc(j)}`" in md
        # 提示词正文不复制进交办单,只给路径 —— 复制一份必然与 tasks/ 漂
        assert f"{gpt_skill.SKILL_DIR}/tasks/{j['label']}.md" in md
        assert gpt_skill.task_prompt(j) not in md


def test_register_names_the_launchd_jobs_as_off_limits():
    """两边都挂 = 撞锁,而撞锁的那次退 3 空跑一轮,外表一切正常。"""
    md = gpt_skill.register_md()
    for j in schedule.jobs_for("launchd"):
        assert f"`{j['label']}`" in md
    assert "不要注册" in md


def test_register_states_the_exact_count_so_a_missing_one_is_catchable():
    """⚠ 条数是唯一能一眼看出"漏注册了一条"的东西 —— 少一条不会有人报错。"""
    md = gpt_skill.register_md()
    n = len(gpt_skill.jobs())
    assert f"注册这 {n} 条" in md
    assert f"正好 {n} 条" in md
    assert "回读" in md            # 注册完必须把结果读回来对表


def test_register_forbids_touching_the_old_erpapi_schedules():
    """旧调度的停用有顺序(先停旧 → 搬状态 → 起新),别人替他删会错位。"""
    md = gpt_skill.register_md()
    assert "不要删" in md and "erpAPI" in md


def test_register_sends_the_high_frequency_pair_to_launchd_not_to_the_agent():
    """⚠ 那两条**不能**注册成智能体的定时任务(所有者 2026-08-17 要给 gpt 交办)。

    两个理由都不是偏好:
      · 每半小时 / 每小时固定 :20 这种频率,常见的智能体定时任务排不出来,
        而排不准的后果不是报错,是**悄悄少跑几轮**;
      · 两边都挂 = 撞锁,后到的退 3 空跑一轮而且外表一切正常。
    所以交办单必须给出 launchd 那条路(install → load → 回读),
    而不是把它们塞进第 1 步那张表。
    """
    md = gpt_skill.register_md()
    ld = schedule.jobs_for("launchd")
    assert ld, "launchd 侧一条都没有?调度表变了"
    for j in ld:
        assert f"`{j['label']}`" in md
        # ⚠ 绝不能出现在"按这张表注册"那一段里
        assert f"`{gpt_skill.cron(j)}`" not in md
    assert "launchd_install --dry-run" in md
    assert "launchctl load -w" in md
    assert "launchctl list" in md              # 装完回读校验
    assert str(len(ld)) in md                  # 应当正好几行
    # 先空跑给人看过再落盘(装完就是真调度)
    assert md.index("--dry-run") < md.index("cli.py launchd_install\n")
    # 别自己拼 plist 路径
    assert schedule.LABEL_PREFIX in md
