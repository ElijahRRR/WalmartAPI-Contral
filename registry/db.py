"""数据库连接唯一入口(工程规范:禁止在其他任何文件自行 psycopg.connect / sqlite3.connect)。

- 业务数据连本机 PostgreSQL 17 库 walmart_data(四 schema 见 docs/db_schema.md)。
- 可重建缓存用 <DATA_ROOT>/cache 下的 SQLite(内置 WAL + busy_timeout)。
"""

import contextlib
import os
import sqlite3

from registry import paths


def pg_dsn() -> str:
    """输入:无 → 输出:PostgreSQL DSN(env WALMART_PG_DSN 覆盖,默认本机 socket 连 walmart_data)。"""
    return os.environ.get("WALMART_PG_DSN", "dbname=walmart_data")


@contextlib.contextmanager
def pg_conn():
    """输入:无 → 输出:psycopg 连接上下文;正常退出 commit,异常 rollback,总是 close。

    用法:
        with db.pg_conn() as conn:
            conn.execute("INSERT ...", (...,))
    """
    import psycopg  # 惰性导入:让不碰 PG 的 workflow 在缺 psycopg 的环境也能运行

    conn = psycopg.connect(pg_dsn())
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def sqlite_cache(name: str) -> sqlite3.Connection:
    """输入:缓存库文件名(不含路径,如 'scraper_cache.db')→ 输出:sqlite3 连接。

    库文件固定在 <DATA_ROOT>/cache/ 下;启用 WAL 与 30s busy_timeout,
    避免旧项目多进程并发写 SQLite 的 database-is-locked 事故。
    仅限可重建缓存 —— 业务数据一律进 PostgreSQL。
    """
    paths.cache_dir().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.cache_dir() / name, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn
