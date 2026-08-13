"""backup — 中心库 pg_dump 备份(db_schema.md「角色与备份」的落地;批次 A 前置)。

用法:
  python cli.py backup                 # 备份 + 按保留天数清理
  python cli.py backup -p days=30      # 自定保留天数(默认 14)

pg_dump 自定义格式(-Fc,可 pg_restore 单表恢复)写入
<DATA_ROOT>/backups/walmart_data_YYYYMMDD_HHMMSS.dump;完成后按文件名里的
日期清理超期备份——只碰本工作流命名模式的文件,目录里其他东西一概不动。
成功/失败通知由 cli.py 统一发(工程规范),本文件不自带通知。

失败保护:pg_dump 退出码非 0 或产物为空 → 删除残件并抛错(残件比没有备份
更危险:恢复时才发现是半截)。当天成功产物永不被本轮清理删除。
"""

import logging
import re
import subprocess
from datetime import datetime, timedelta

from registry import db, paths

DANGEROUS = False

logger = logging.getLogger("workflows.backup")

_NAME_RE = re.compile(r"^walmart_data_(\d{8})_(\d{6})\.dump$")


def prune_candidates(names: list[str], now: datetime, days: int) -> list[str]:
    """输入:备份目录文件名列表 + 当前时间 + 保留天数 → 输出:应删除的文件名。

    纯函数(便于测试)。只认 walmart_data_YYYYMMDD_HHMMSS.dump 命名模式,
    其余文件名(含解析失败的)一律不进候选——宁可漏删,不可误删。
    """
    cutoff = now - timedelta(days=days)
    doomed = []
    for name in names:
        m = _NAME_RE.match(name)
        if not m:
            continue
        try:
            stamp = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        if stamp < cutoff:
            doomed.append(name)
    return doomed


def run(params: dict) -> str:
    """输入:params(可选 days=保留天数)→ 输出:备份文件、大小、清理数摘要。"""
    days = int(params.get("days", 14))
    out_dir = paths.backups_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    target = out_dir / f"walmart_data_{now:%Y%m%d_%H%M%S}.dump"
    proc = subprocess.run(
        ["pg_dump", "--format=custom", "--file", str(target), db.pg_dsn()],
        capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)      # 半截产物必须清掉
        raise RuntimeError(
            f"pg_dump 失败(exit={proc.returncode}):{proc.stderr.strip()[-500:]}")

    doomed = prune_candidates(
        [p.name for p in out_dir.iterdir() if p.is_file()], now, days)
    for name in doomed:
        (out_dir / name).unlink(missing_ok=True)

    size_mb = target.stat().st_size / 1024 / 1024
    return (f"backup:{target.name} 完成({size_mb:.1f} MB),"
            f"清理超期备份 {len(doomed)} 个(保留 {days} 天)")
