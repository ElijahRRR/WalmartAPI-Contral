"""README 与代码的一致性(2026-08-17 建)。

README 是"这个系统是什么"的标准文档,而**文档失真不会让任何测试变红** ——
除了这一份。这里钉的都是"加了东西忘了写、改了名字没同步"那一类:

  · 新增一条工作流而 README 没列 ⇒ 那条工作流对外**不存在**(谁也不知道能跑它);
  · 删/改一条工作流而 README 还列着 ⇒ 照着 README 敲命令得到退出码 2;
  · 调度改了而 README 那张表没改 ⇒ 两份时间表,而漂了不报错。

⚠ 只钉**清单与出处**,不钉散文。措辞怎么写是人的自由;"有哪些工作流、
几点跑什么"必须与代码一致。
"""

import pathlib
import re

from registry import schedule
from services import gpt_skill

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_README = (_ROOT / "README.md").read_text(encoding="utf-8")


def _workflows() -> set[str]:
    return {p.stem for p in (_ROOT / "workflows").glob("*.py")
            if p.stem != "__init__"}


def test_every_workflow_appears_in_the_readme():
    """一条工作流没写进 README = 它对外不存在。"""
    missing = sorted(w for w in _workflows() if f"`{w}`" not in _README)
    assert not missing, f"README 未收录:{missing}(补进第六节对应业务域)"


def test_readme_does_not_list_workflows_that_do_not_exist():
    """⚠ 只查第六节那些**表格行**里的名字。

    正文里出现 `catalog` / `ops` / `product_chain` 这类词是 schema 名与调度
    任务名,不是工作流 —— 所以按"行首 | `名字` |"这个形状取,别宽泛地扫反引号。
    """
    body = _README[_README.index("## 六、业务域与工作流全表"):
                   _README.index("## 七、调度")]
    listed = set(re.findall(r"^\| `([a-z_]+)` \|", body, re.M))
    real = _workflows()
    labels = {j["label"] for j in schedule.JOBS}
    ghosts = sorted(listed - real - labels)
    assert not ghosts, f"README 列了不存在的工作流:{ghosts}"


def test_workflow_count_matches():
    """开头那个数字是给人看全貌的,漂了比不写更糟。"""
    n = len(_workflows())
    assert f"**{n} 条工作流**" in _README, f"实有 {n} 条,README 开头的数字要同步"


def test_schedule_table_matches_the_registry():
    """⚠ 时间表的唯一出处是 `registry/schedule.py`。

    README 抄了一份是为了让人不必读代码就看懂节奏 —— 那就必须与代码一致,
    否则就是"两份时间表",而漂了没有任何东西会报错(照着 README 排的调度会
    在错的时间跑,而且报成功)。
    """
    for j in gpt_skill.jobs():
        row = f"| `{j['label']}` | {gpt_skill.when(j)} |"
        assert row in _README, f"智能体调度那张表与代码不一致:缺 {row}"
    for j in schedule.jobs_for("launchd"):
        assert f"`{j['label']}`" in _README
        assert gpt_skill.when(j) in _README, j["label"]
    # 条数也要对
    n_gpt, n_ld = len(gpt_skill.jobs()), len(schedule.jobs_for("launchd"))
    assert f"电脑 launchd {n_ld} 条" in _README or \
        f"launchd {n_ld} 条" in _README
    assert f"{n_gpt} 条每日/每周" in _README or f"{n_gpt} 条)" in _README


def test_dangerous_workflows_are_marked():
    """危险工作流在 README 里必须带「危」标 —— 那一栏是人决定"要不要先
    --dry-run"的依据。抽查最要紧的几条(会写沃尔玛且不可逆的)。"""
    for name in ("list_new", "maintenance", "product_clear",
                 "problem_product_cleanup", "match_listing", "product_audit"):
        # 找到它那一行,行内必须有「危」
        rows = [ln for ln in _README.splitlines()
                if ln.startswith(f"| `{name}` |")]
        assert rows, f"README 第六节没有 {name} 那一行"
        assert "危" in rows[0], f"{name} 那一行少了「危」标:{rows[0]}"


def test_the_reversed_dry_run_default_is_stated_not_the_old_one():
    """⚠ 2026-08-16 起**缺省即真跑**。README 若还写着旧口径(危险工作流缺省
    dry-run、真跑加 --execute),照着做的人会以为默认安全 —— 而它不再安全。"""
    assert "缺省即真跑" in _README
    assert "空跑用 `--dry-run`" in _README
    assert "缺省 dry-run" not in _README.replace('"缺省 dry-run"这条防线', "")
    # --execute 只该以"兼容别名/空操作"的身份出现,不该是真跑的必要条件
    assert "人眼确认输出后才加 `--execute`" not in _README
