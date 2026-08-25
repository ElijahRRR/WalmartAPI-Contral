"""cli 串联回归(2026-08-16 加)。

所有者定稿:「订单链…每个运行后都需要自动同步到飞书,不要人来手动推」。
铁律 1 禁止 workflow 互相 import,所以链在 cli 这一层做:

    python cli.py order_sync order_audit order_center_push

本文件钉住的是**串联的语义**,不是某条业务链 —— 顺序、停链、通知条数、
名字先验、参数作用域,每一条错了都会在生产上表现成"某一步悄悄没跑"。
"""

import sys
import types

import pytest

import cli


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch, tmp_path):
    """把 cli 的四个外部依赖(日志目录/锁目录/ops.runs/飞书)拉进沙箱。"""
    from registry import paths
    monkeypatch.setattr(paths, "logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(paths, "locks_dir", lambda: tmp_path / "locks")
    monkeypatch.setattr(paths, "env_file", lambda: tmp_path / ".env")
    monkeypatch.setattr(cli, "_record_start", lambda *a: None)
    monkeypatch.setattr(cli, "_record_finish", lambda *a: None)


@pytest.fixture
def notes(monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "_notify", lambda t: sent.append(t))
    return sent


def _fake(name: str, fn, dangerous=False):
    """把一个假 workflow 挂进 sys.modules,让真 importlib 能 import 到。"""
    m = types.ModuleType(f"workflows.{name}")
    m.run = fn
    m.DANGEROUS = dangerous
    sys.modules[f"workflows.{name}"] = m
    return m


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def mk(name, ok=True):
        def _run(params):
            seen.append((name, dict(params)))
            if not ok:
                raise RuntimeError(f"{name} 炸了")
            return f"{name} 完成"
        _fake(name, _run)

    mk.seen = seen
    yield mk
    for k in [k for k in sys.modules if k.startswith("workflows.zz_")]:
        del sys.modules[k]


def test_single_run_notification_is_byte_identical(calls, notes):
    """单跑的通知形态**一个字都不能变** —— 人和告警规则都认这个格式。"""
    calls("zz_a")
    assert cli.main(["zz_a"]) == 0
    assert notes == ["✅ zz_a 成功\nzz_a 完成"]


def test_chain_runs_in_order_and_sends_one_notification(calls, notes):
    calls("zz_a")
    calls("zz_b")
    calls("zz_c")
    assert cli.main(["zz_a", "zz_b", "zz_c"]) == 0
    assert [n for n, _ in calls.seen] == ["zz_a", "zz_b", "zz_c"]
    # 整链一条通知:三步链每小时发三条会把群刷废,而且看不出属于同一次运行
    assert len(notes) == 1
    assert notes[0].startswith("✅ 链 [zz_a → zz_b → zz_c]")
    assert notes[0].count("✅") == 4         # 链头 + 三步


def test_chain_stops_at_the_first_failure(calls, notes):
    """⚠ 停链不是洁癖:后面几步吃的是这一步的输出。

    order_sync 没拉全就推飞书 = 表里少一批订单,而且整链报成功。
    """
    calls("zz_a")
    calls("zz_bad", ok=False)
    calls("zz_c")
    assert cli.main(["zz_a", "zz_bad", "zz_c"]) == 1
    assert [n for n, _ in calls.seen] == ["zz_a", "zz_bad"]   # zz_c 没跑
    assert len(notes) == 1
    assert "❌ 链" in notes[0]
    # 没跑的那步必须**在通知里写明**,不能只是消失
    assert "zz_c:上游 zz_bad 未成功,未执行" in notes[0]


def test_locked_step_stops_the_chain_with_exit_3(calls, notes, monkeypatch):
    """某步拿不到锁 = 那个工作流真的正在跑,同样停链(退出码 3)。"""
    from services import runlock
    calls("zz_a")
    calls("zz_busy")
    calls("zz_c")
    real = runlock.hold

    import contextlib

    @contextlib.contextmanager
    def fake_hold(name, wait_secs=0, holder=""):
        if name == "zz_busy":
            yield False
        else:
            with real(name) as got:
                yield got

    monkeypatch.setattr(runlock, "hold", fake_hold)
    assert cli.main(["zz_a", "zz_busy", "zz_c"]) == 3
    assert [n for n, _ in calls.seen] == ["zz_a"]
    assert "已有实例在运行" in notes[0]


def test_all_names_are_validated_before_the_first_step_runs(calls):
    """⚠ 打错第三个名字,不能让前两步(可能是破坏性的)先跑完再报错。"""
    calls("zz_a")
    with pytest.raises(SystemExit) as e:
        cli.main(["zz_a", "zz_does_not_exist"])
    assert e.value.code == 2
    assert calls.seen == []             # 一步都没跑


def test_scoped_params_go_to_one_step_only():
    got = cli._build_params(["days=30", "order_center_push:table=sales"],
                            ["order_sync", "order_center_push"])
    assert got["order_sync"] == {"days": "30"}
    assert got["order_center_push"] == {"days": "30", "table": "sales"}


def test_colon_in_a_value_is_not_a_scope_prefix():
    """⚠ 前缀只在冒号前那段**恰好是本次某个工作流名**时才生效。

    否则 `-p url=https://x` 这种值里带冒号的参数会被切成一个不存在的作用域,
    参数静默丢失(那一步照跑,只是没收到参数)。
    """
    got = cli._build_params(["url=https://x", "notachain:k=v"], ["zz_a"])
    assert got["zz_a"] == {"url": "https://x", "notachain:k": "v"}


def test_dry_run_reaches_every_step(calls, notes):
    calls("zz_a")
    calls("zz_b")
    cli.main(["zz_a", "zz_b", "--dry-run"])
    assert all(p["dry_run"] is True for _, p in calls.seen)


def test_each_step_logs_to_its_own_file(calls, notes, tmp_path):
    """串联不改"一个工作流一个日志文件"的约定 —— 人是按工作流名去 logs/ 找的。"""
    import logging

    def mk(name):
        def _run(params):
            logging.getLogger(f"workflows.{name}").info("%s 干活了", name)
            return "ok"
        _fake(name, _run)

    mk("zz_a")
    mk("zz_b")
    cli.main(["zz_a", "zz_b"])
    for name in ("zz_a", "zz_b"):
        text = (tmp_path / "logs" / f"{name}.log").read_text(encoding="utf-8")
        assert f"{name} 干活了" in text
    # 各进各的,不串味
    assert "zz_b 干活了" not in (tmp_path / "logs" / "zz_a.log").read_text(
        encoding="utf-8")


# ── 硬拒 ≠ 成功 ────────────────────────────────────────────────────────

def test_a_refusal_is_not_reported_as_success(notes):
    """⚠ 「前提不成立就别跑」的早退**不是成功**。

    全仓十几处硬拒都写成普通 `return "⛔ …"` 而不抛异常 —— 那是对的,它不是
    崩溃,不该打印 traceback。但 cli 原来照单记 status='success' 并发 ✅,
    实测 2026-08-16 的日志长这样:

        INFO cli: workflow alloc_plan 成功:
        ⛔ 限额表读不到(…尚未登记 app_token/table_id)
        ✅ [DRY-RUN] alloc_plan 成功

    后果是 `ops.runs.status` 对这几条工作流**失去判别力**:飞书告警里
    「什么都没干」和「分配了 2.8 万条」长得一模一样。
    """
    _fake("zz_refuse", lambda p: "⛔ 限额表读不到:没有类目/渠道/容量就没法分配")
    assert cli.main(["zz_refuse"]) == 1              # 退出码 = 活没干成
    assert notes and notes[0].startswith("⛔ ")       # 图标与 ✅/❌ 都不同
    assert "未执行(前提不成立)" in notes[0]
    del sys.modules["workflows.zz_refuse"]


def test_a_refusal_stops_the_chain(calls, notes):
    """⚠ 硬拒必须停链,否则后面几步拿着「前提没满足」一路跑到底。

    串联的约定是「前一个失败就不跑后面的」;拒跑计成功的话,这条约定对
    **最常见的那种没跑成**(配置没填、表读不到)恰好失效。
    """
    _fake("zz_refuse2", lambda p: "⛔ 前提不成立")
    calls("zz_after")
    assert cli.main(["zz_refuse2", "zz_after"]) == 1
    assert [n for n, _ in calls.seen] == []          # zz_after 一步没跑
    assert "zz_after:上游 zz_refuse2 未成功,未执行" in notes[0]
    del sys.modules["workflows.zz_refuse2"]


def test_a_refusal_marker_only_counts_at_the_start_of_the_summary(calls, notes):
    """⚠ 摘要**正文里**提到 ⛔ 不算硬拒 —— 报告里列举"哪些被拦下了"很常见。

    判据必须是首字符,否则一份正常跑完、但内容里带 ⛔ 的报告会被判成没跑。
    """
    _fake("zz_mentions", lambda p: "分配完成\n  其中 3 家被 ⛔ 拦下,详见清单")
    assert cli.main(["zz_mentions"]) == 0
    assert notes[0].startswith("✅ ")
    del sys.modules["workflows.zz_mentions"]
