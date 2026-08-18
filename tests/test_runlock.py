"""工作流单实例锁(services/runlock)的两条实质区分。

锁只有两种"拿不到",而它们的处置**相反**:
  · 被别人占着 → 返回 None,调用方按"这轮跳过"处理(退出码 3,不是失败);
  · 建不出来(权限/磁盘)→ 抛 LockUnavailable,必须炸出来让人修。
混成一种的后果二选一:权限坏了被当成"上一轮还在跑"每天静默跳过,
或者正常的并发撞锁被判失败天天刷告警。
"""

import os

import pytest

from services import runlock


def test_busy_and_broken_are_different_outcomes(tmp_path, monkeypatch):
    """被占着 → None(不抛);建不出来 → LockUnavailable(抛)。"""
    locks = tmp_path / "locks"
    locks.mkdir()
    monkeypatch.setattr(runlock.paths, "locks_dir", lambda: locks)
    held = runlock.acquire("x")
    assert held is not None
    assert runlock.acquire("x") is None          # 占着 → None
    held.close()
    assert issubclass(runlock.LockUnavailable, RuntimeError)


def test_os_error_becomes_lock_unavailable(tmp_path, monkeypatch):
    """任何建不出锁的 OSError 都要转成 LockUnavailable 并带上诊断。

    ⚠ 这里用"父路径是个文件"来触发(NotADirectoryError),而**不是**造一个
    只读目录 —— 只读那招在 root 下不成立(root 照写不误),而 CI/容器常以
    root 跑,那样的用例会假绿。
    """
    notadir = tmp_path / "iam_a_file"
    notadir.write_text("")
    monkeypatch.setattr(runlock.paths, "locks_dir", lambda: notadir / "locks")
    with pytest.raises(runlock.LockUnavailable) as ei:
        runlock.acquire("perf_problems")
    assert "建不了运行锁" in str(ei.value)


def test_the_hint_says_who_owns_it_not_just_denied(tmp_path):
    """⚠ 生产实遇 2026-08-17:`order_daily` 报「创建运行锁时被拒绝访问
    …/perf_problems.lock」,替我们看告警的智能体据此建议"恢复该锁目录的写入
    权限" —— **方向多半是错的**:最常见成因是某次用 sudo 跑过、**锁文件**归了
    root,而目录本身好好的。照着改目录,下一轮照样失败。

    所以诊断必须把属主/权限/当前用户三样都说出来,让人一眼看出该 chown 哪个。
    """
    locks = tmp_path / "locks"
    locks.mkdir()
    target = locks / "perf_problems.lock"
    target.write_text("")
    msg = runlock._permission_hint(target)
    assert "perf_problems.lock" in msg
    assert "属主=" in msg and "权限=" in msg and "当前用户=" in msg
    assert "锁目录" in msg and "DATA_ROOT" in msg      # 三层都报,免得猜
    assert "sudo" in msg and "chown" in msg
    # ⚠ 绝不能建议 rm:删锁文件不会让正持锁的进程放手,只会让新进程锁到另一个
    #   inode 上 —— 两个实例同时跑同一条危险链,而互斥"看起来还在"
    assert "别用 rm" in msg
    assert "rm -f" not in msg


def test_hint_survives_a_missing_path(tmp_path):
    """诊断本身不许再抛 —— 它是报错路径上的代码,炸了就把真正的错误盖掉。"""
    msg = runlock._permission_hint(tmp_path / "nope" / "x.lock")
    assert "不存在" in msg and "当前用户=" in msg


@pytest.mark.skipif(os.getuid() == 0, reason="root 无视文件权限,这条只在普通用户下有意义")
def test_real_permission_denied_is_caught(tmp_path, monkeypatch):
    """真·权限被拒的那条路径(非 root 环境才跑,比如所有者的 Mac)。"""
    locks = tmp_path / "locks"
    locks.mkdir()
    monkeypatch.setattr(runlock.paths, "locks_dir", lambda: locks)
    (locks / "perf_problems.lock").write_text("")
    (locks / "perf_problems.lock").chmod(0o400)
    with pytest.raises(runlock.LockUnavailable) as ei:
        runlock.acquire("perf_problems")
    assert "chown" in str(ei.value)
