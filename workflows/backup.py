"""backup — 中心库 pg_dump 备份(db_schema.md「角色与备份」的落地;批次 A 前置)。

用法:
  python cli.py backup                 # 备份 + 校验 + 按保留天数清理
  python cli.py backup -p days=30      # 自定保留天数(默认 14,最小 1)

流程:pg_dump 自定义格式(-Fc)先写 *.dump.part(命名模式之外,清理永不碰)
→ pg_restore --list 校验目录可读(捕获半截/损坏文件)→ rename 成正式名
(原子落地:backups/ 里只会出现完整且可读的备份)→ 按文件名日期清理超期。
清理只碰本工作流命名模式的文件,且永不删本轮刚生成的备份;任何失败/超时
都会清掉 .part 残件后抛错(残件比没有备份更危险:恢复时才发现是半截)。
成功/失败通知由 cli.py 统一发(工程规范),本文件不自带通知。

恢复注意(写给未来的自己):
- -Fc 只含单库;readonly 是实例级角色不在 dump 里——异机恢复先
  `createuser readonly`,或 `pg_restore --no-acl`。
- WALMART_PG_DSN 若配了口令,DSN 会出现在进程列表——请改用 ~/.pgpass;
  已带 --no-password,凭证缺失会快速失败而不是挂住等交互输入。
- Homebrew 多版本:PATH 里的 pg_dump 与服务器大版本不匹配会拒绝 dump,
  在 <DATA_ROOT>/.env 配 PG_BIN_DIR=<PG17 的 bin 目录>(registry.paths.pg_tool)。
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
    其余文件名(含解析失败的、.part 残件)一律不进候选——宁可漏删,不可误删。
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
    """输入:params(可选 days=保留天数,≥1)→ 输出:备份文件、大小、清理数摘要。"""
    days = int(params.get("days", 14))
    if days < 1:
        raise ValueError("days 必须 ≥1:保留 0 天等于连今天的备份一起删")
    out_dir = paths.backups_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    target = out_dir / f"walmart_data_{now:%Y%m%d_%H%M%S}.dump"
    part = target.with_name(target.name + ".part")
    try:
        proc = subprocess.run(
            [paths.pg_tool("pg_dump"), "--format=custom", "--no-password",
             "--file", str(part), db.pg_dsn()],
            capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0 or not part.exists() or part.stat().st_size == 0:
            raise RuntimeError(
                f"pg_dump 失败(exit={proc.returncode}):{proc.stderr.strip()[-500:]}")
        check = subprocess.run([paths.pg_tool("pg_restore"), "--list", str(part)],
                               capture_output=True, text=True, timeout=600)
        if check.returncode != 0:
            raise RuntimeError(
                f"备份校验失败(pg_restore --list):{check.stderr.strip()[-300:]}")
    except BaseException:
        part.unlink(missing_ok=True)    # 超时/中断/失败,残件一律不留
        raise
    part.rename(target)                 # 校验过关才落正式名,目录里没有半截货

    doomed = [n for n in prune_candidates(
        [p.name for p in out_dir.iterdir() if p.is_file()], now, days)
        if n != target.name]            # 双保险:本轮产物永不删
    for name in doomed:
        (out_dir / name).unlink(missing_ok=True)

    size_mb = target.stat().st_size / 1024 / 1024
    return (f"backup:{target.name} 完成并通过校验({size_mb:.1f} MB),"
            f"清理超期备份 {len(doomed)} 个(保留 {days} 天)")
