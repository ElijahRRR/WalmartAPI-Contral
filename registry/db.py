"""数据库连接唯一入口(工程规范:禁止在其他任何文件自行 psycopg.connect / sqlite3.connect)。

- 业务数据连本机 PostgreSQL 17 库 walmart_data(五 schema 见 docs/db_schema.md)。
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
def pg_conn(autocommit: bool = False):
    """输入:(可选 autocommit)→ 输出:psycopg 连接上下文;总是 close。

    默认事务模式:正常退出 commit,异常 rollback。autocommit=True 给并发
    worker 的只读+幂等缓存写用(product_audit workers>1:每 worker 一条
    连接,写路径仍归主线程的事务连接)。

    用法:
        with db.pg_conn() as conn:
            conn.execute("INSERT ...", (...,))
    """
    import psycopg  # 惰性导入:让不碰 PG 的 workflow 在缺 psycopg 的环境也能运行

    conn = psycopg.connect(pg_dsn(), autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except BaseException:
        if not autocommit:
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


def legacy_audit_dsn() -> str:
    """输入:无 → 输出:旧审核库 walmart_audit 的 DSN。

    审核迁入批次 A(audit_import)与批次 B 双跑校准专用。旧库与中心库在
    同一台生产 Mac 同一 PG 实例(调研定稿 docs/audit_migration_plan.md);
    若不同实例用 LEGACY_AUDIT_DSN 覆盖。地址只准从这里取(铁律 3)。
    """
    return os.environ.get("LEGACY_AUDIT_DSN", "dbname=walmart_audit")


@contextlib.contextmanager
def legacy_audit_conn():
    """输入:无 → 输出:旧审核库**只读**连接上下文。

    搬迁与校准的读取端。旧库是待归档真值,本仓对它只有读的权利。
    """
    import psycopg

    conn = psycopg.connect(legacy_audit_dsn())
    try:
        conn.read_only = True
        yield conn
    finally:
        conn.close()


def uspto_dsn() -> str:
    """输入:无 → 输出:USPTO 商标库 DSN(env USPTO_DSN 覆盖,默认本机 uspto 库)。

    批复 #3(2026-08-13):R5 商标反查继续跨库连它;灌库链路在外部仓,
    本仓永远只读。
    """
    return os.environ.get("USPTO_DSN", "dbname=uspto")


@contextlib.contextmanager
def uspto_conn():
    """输入:无 → 输出:USPTO 库**只读、autocommit**连接上下文(1400 万行)。

    autocommit 是批量消费的关键:整批共用一个连接,若开事务,第一条报错后
    事务进 aborted 态,后续每条查询都 InFailedSqlTransaction——"fail-soft"
    变成整轮静默失效(审核 R5 评审实证 2026-08-13)。只读查询无需事务语义。
    """
    import psycopg

    conn = psycopg.connect(uspto_dsn())
    try:
        conn.autocommit = True
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
