"""backup 的清理纯函数与 run() 级保护测试:宁可漏删,不可误删;残件不落地。"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from workflows import backup

_NOW = datetime(2026, 8, 13, 12, 0, 0)


def test_prune_keeps_recent_drops_old():
    names = ["walmart_data_20260813_090000.dump",   # 今天
             "walmart_data_20260801_090000.dump",   # 12 天前(保留)
             "walmart_data_20260720_090000.dump"]   # 24 天前(该删)
    assert backup.prune_candidates(names, _NOW, 14) == \
        ["walmart_data_20260720_090000.dump"]


def test_prune_ignores_foreign_files():
    """目录里其他东西(手动备份/残件/坏名字)一概不碰。"""
    names = ["manual_snapshot.dump", "notes.txt", ".DS_Store",
             "walmart_data_20260101_000000.dump.part",  # 残件模式,不碰
             "walmart_data_2026081_090000.dump",        # 位数不对
             "walmart_data_99999999_990000.dump"]       # 日期解析失败
    assert backup.prune_candidates(names, _NOW, 14) == []


def test_prune_boundary_exact_cutoff_kept():
    names = ["walmart_data_20260730_120000.dump"]       # 恰 14 天整
    assert backup.prune_candidates(names, _NOW, 14) == []


def test_days_below_one_rejected(monkeypatch, tmp_path):
    """days=0 会连今天的备份一起删——必须在动手前拒绝。"""
    monkeypatch.setattr(backup.paths, "backups_dir", lambda: tmp_path)
    with pytest.raises(ValueError, match="≥1"):
        backup.run({"days": 0})


def _fake_subprocess(created: dict):
    """pg_dump 假实现:把 --file 后面的路径写出非空内容;pg_restore 校验通过。"""
    def fake_run(cmd, **kw):
        if cmd[0] == "pg_dump":
            path = cmd[cmd.index("--file") + 1]
            with open(path, "wb") as f:
                f.write(b"FAKEDUMP")
            created["dump_cmd"] = cmd
            return SimpleNamespace(returncode=0, stderr="")
        if cmd[0] == "pg_restore":
            created["verified"] = cmd
            return SimpleNamespace(returncode=0, stderr="")
        raise AssertionError(cmd)
    return fake_run


def test_run_atomic_rename_and_prune_spares_today(monkeypatch, tmp_path):
    """成功路径:.part 原子换名、正式名落地、pg_restore 校验被调用;
    旧备份被清、今天的产物永不删。"""
    monkeypatch.setattr(backup.paths, "backups_dir", lambda: tmp_path)
    created: dict = {}
    monkeypatch.setattr(backup.subprocess, "run", _fake_subprocess(created))
    (tmp_path / "walmart_data_20200101_000000.dump").write_bytes(b"old")

    out = backup.run({"days": 14})
    dumps = sorted(p.name for p in tmp_path.iterdir())
    assert len(dumps) == 1 and dumps[0].endswith(".dump")      # 旧的删了,新的在
    assert not any(n.endswith(".part") for n in dumps)         # 无残件
    assert "verified" in created                               # 校验真的跑了
    assert "完成并通过校验" in out and "清理超期备份 1 个" in out


def test_run_verify_failure_leaves_no_residue(monkeypatch, tmp_path):
    """pg_restore 校验失败:抛错,.part 与正式名都不许留在目录里。"""
    monkeypatch.setattr(backup.paths, "backups_dir", lambda: tmp_path)

    def fake_run(cmd, **kw):
        if cmd[0] == "pg_dump":
            path = cmd[cmd.index("--file") + 1]
            with open(path, "wb") as f:
                f.write(b"HALFDUMP")
            return SimpleNamespace(returncode=0, stderr="")
        return SimpleNamespace(returncode=1, stderr="corrupt")
    monkeypatch.setattr(backup.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="备份校验失败"):
        backup.run({"days": 14})
    assert list(tmp_path.iterdir()) == []
