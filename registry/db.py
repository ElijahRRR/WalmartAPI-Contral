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


def legacy_cleanup_dsn() -> str:
    """输入:无 → 输出:旧问题商品库 walmart_cleanup 的 DSN。

    历史导入(cleanup_history_import)专用。旧库是生产 Mac 上 peer 认证的
    本机库(legacy_survey C11);两库若不同实例,用 LEGACY_CLEANUP_DSN 覆盖。
    地址只准从这里取(铁律 3),工作流不许自带 DSN 参数。
    """
    return os.environ.get("LEGACY_CLEANUP_DSN", "dbname=walmart_cleanup")


@contextlib.contextmanager
def legacy_cleanup_conn():
    """输入:无 → 输出:旧清理库**只读**连接上下文。

    历史导入的读取端。显式 read-only 事务:导入器对旧库只有读的权利——
    它是待归档的历史真值,写坏了没有第二份。
    """
    import psycopg

    conn = psycopg.connect(legacy_cleanup_dsn())
    try:
        conn.read_only = True
        yield conn
    finally:
        conn.close()


def audit_dsn() -> str:
    """输入:无 → 输出:审核系统库 walmart_audit 的 DSN(env WALMART_AUDIT_DSN 覆盖)。

    审核系统(walmart-audit-system 仓库)无 JSON API,其现行下游对接方式就是
    直连库(该仓库 cli/get_problem_images.py 明写"上架脚本用法")。
    地址只准从这里取(铁律 3),工作流不许自带 DSN 参数。
    """
    return os.environ.get("WALMART_AUDIT_DSN", "dbname=walmart_audit")


@contextlib.contextmanager
def audit_conn():
    """输入:无 → 输出:审核库**只读**连接上下文(audit_sync 的读取端)。

    审核结论的权威永远在审核系统的库里,本侧只有读的权利——回流落点是
    catalog.products 的 audit_* 五列(写走 pg_conn,与这里无关)。
    """
    import psycopg

    conn = psycopg.connect(audit_dsn())
    try:
        conn.read_only = True
        yield conn
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
