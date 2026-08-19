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


def test_hint_splits_sandbox_from_real_permission(tmp_path, monkeypatch):
    """⚠⚠ 生产两次踩反方向,这条守的就是那两次。

    ① 2026-08-17 上午:`order_daily` 报「拒绝访问 …/perf_problems.lock」,
       智能体建议"恢复该锁目录的写入权限" —— 错。
    ② 同日下午我据此写的提示是"多半是 sudo 跑过、归了 root" —— **也错**。
       实测:当前用户 = PG 用户 = nextderboy,locks/logs 属主都是 nextderboy:staff,
       库里建临时表也成功。errno 是 **EPERM(1)** 不是 EACCES(13) ——
       拦住的是 **Codex 沙箱**(它只放行仓库目录,而 DATA_ROOT 是仓库的同级目录)。

    所以判据是:**按普通文件权限看本该写得动、却仍被拒 ⇒ 沙箱,不是权限。**
    这一档去 chmod/chown 是白费力气。
    """
    locks = tmp_path / "WalmartAPI_data" / "locks"
    locks.mkdir(parents=True)
    target = locks / "daily_report.lock"
    target.write_text("")

    # —— 沙箱档:DAC 说能写(os.access True),却被拒
    monkeypatch.setattr(runlock, "_looks_writable", lambda p: True)
    msg = runlock._permission_hint(target, OSError(1, "Operation not permitted"))
    assert "errno=1(EPERM)" in msg
    assert "属主与权限都正常" in msg and "沙箱" in msg
    assert "writable_roots" in msg              # 说清到底该改哪儿
    # ⚠ 这一档绝不许再**劝人** chown。注意只能查"建议形态"(`sudo chown …`):
    #   文案里"别去 chmod/chown"本身就带这个词,按裸词查会被自己的禁令绊倒
    #   —— 正是 CLAUDE.md 记的那种 grep 假阳性
    assert "sudo chown" not in msg
    assert "别去 chmod/chown" in msg
    for wrong in ("chmod 777", "WALMART_DATA_ROOT", "软链接"):
        assert wrong in msg, f"要点名这条歧路:{wrong}"

    # —— 真权限档:DAC 就说写不动
    monkeypatch.setattr(runlock, "_looks_writable", lambda p: False)
    msg2 = runlock._permission_hint(target, OSError(13, "Permission denied"))
    assert "errno=13(EACCES)" in msg2
    assert "sudo" in msg2 and "chown" in msg2
    assert "沙箱" not in msg2
    # ⚠ 两档都不许建议 rm:删锁文件不会让正持锁的进程放手,只会让新进程锁到
    #   另一个 inode 上 —— 两个实例同时跑同一条危险链,而互斥"看起来还在"
    assert "别用 rm" in msg2 and "rm -f" not in msg2


def test_both_branches_report_owner_and_current_user(tmp_path):
    """不管哪一档,属主/权限/当前用户三样都要在 —— 那是判断走哪一档的依据本身。"""
    locks = tmp_path / "locks"
    locks.mkdir()
    target = locks / "x.lock"
    target.write_text("")
    msg = runlock._permission_hint(target)
    assert "属主=" in msg and "权限=" in msg and "当前用户=" in msg
    assert "锁目录" in msg and "DATA_ROOT" in msg


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


def test_hold_waits_for_lock_release(tmp_path, monkeypatch):
    """wait_secs>0 = 等锁模式(2026-08-19):占用方释放后等待方拿到;
    超时仍占着 → False(与老语义同款,只是晚一点放弃)。缺省 0 不等。"""
    import threading
    import time as _t

    locks = tmp_path / "locks"
    locks.mkdir()
    monkeypatch.setattr(runlock.paths, "locks_dir", lambda: locks)
    monkeypatch.setattr(runlock, "_POLL_SECS", 0.02)

    fh = runlock.acquire("pi", holder="order_audit._ingest_now")
    assert "holder=order_audit._ingest_now" in (locks / "pi.lock").read_text()

    # 0.1s 后对方释放;等待方最多等 2s → 必然拿到
    threading.Timer(0.1, lambda: (runlock.__dict__, fh.close())).start()
    with runlock.hold("pi", wait_secs=2) as got:
        assert got is True

    # 占着不放 + 超时很短 → False;缺省不等 → 立刻 False
    fh2 = runlock.acquire("pi")
    t0 = _t.monotonic()
    with runlock.hold("pi", wait_secs=0.06) as got:
        assert got is False
    assert _t.monotonic() - t0 < 1.5             # 是"短等后放弃",不是死等
    with runlock.hold("pi") as got:
        assert got is False
    fh2.close()
