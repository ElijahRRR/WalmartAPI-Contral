"""DATA_ROOT 与所有本地路径的唯一出处。

全部以函数暴露而非模块常量:launchd 不读 shell 配置,环境变量可能在
cli.py 加载 .env 之后才就位,call-time 求值保证拿到最终值。
默认值必须能在无任何环境变量时独立工作(launchd 场景)。
"""

import os
from pathlib import Path

_SUBDIRS = ("specs", "cache", "logs", "backups", "locks")


def data_root() -> Path:
    """输入:无 → 输出:DATA_ROOT 绝对路径(env WALMART_DATA_ROOT 覆盖,默认 ~/walmart_data)。"""
    return Path(os.environ.get("WALMART_DATA_ROOT", str(Path.home() / "walmart_data"))).expanduser()


def env_file() -> Path:
    """输入:无 → 输出:密钥文件 <DATA_ROOT>/.env 的路径(文件应 chmod 600)。"""
    return data_root() / ".env"


def specs_dir() -> Path:
    return data_root() / "specs"


def cache_dir() -> Path:
    return data_root() / "cache"


def logs_dir() -> Path:
    return data_root() / "logs"


def backups_dir() -> Path:
    return data_root() / "backups"


def locks_dir() -> Path:
    return data_root() / "locks"


def stores_snapshot_file() -> Path:
    """输入:无 → 输出:店铺凭证本地快照文件路径(含密钥,写入后 chmod 600,不进 git)。"""
    return cache_dir() / "stores_snapshot.json"


def ensure_data_root() -> list[str]:
    """输入:无 → 输出:本次实际新建的目录名列表(幂等,已存在则跳过)。"""
    created = []
    for name in _SUBDIRS:
        p = data_root() / name
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(name)
    return created
