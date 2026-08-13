"""backup 清理候选的纯函数测试:宁可漏删,不可误删。"""

from datetime import datetime

from workflows.backup import prune_candidates

_NOW = datetime(2026, 8, 13, 12, 0, 0)


def test_prune_keeps_recent_drops_old():
    names = ["walmart_data_20260813_090000.dump",   # 今天
             "walmart_data_20260801_090000.dump",   # 12 天前(保留)
             "walmart_data_20260720_090000.dump"]   # 24 天前(该删)
    assert prune_candidates(names, _NOW, 14) == ["walmart_data_20260720_090000.dump"]


def test_prune_ignores_foreign_files():
    """目录里其他东西(手动备份/无关文件/坏名字)一概不碰。"""
    names = ["manual_snapshot.dump", "notes.txt", ".DS_Store",
             "walmart_data_2026081_090000.dump",       # 位数不对
             "walmart_data_20260720_090000.dump.gz",   # 后缀不对
             "walmart_data_99999999_990000.dump"]      # 日期解析失败
    assert prune_candidates(names, _NOW, 14) == []


def test_prune_boundary_exact_cutoff_kept():
    """恰好在保留边界上的不删(< cutoff 才删,不是 <=)。"""
    names = ["walmart_data_20260730_120000.dump"]      # 恰 14 天整
    assert prune_candidates(names, _NOW, 14) == []
