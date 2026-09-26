"""数据库连接唯一入口(工程规范:禁止在其他任何文件自行 psycopg.connect / sqlite3.connect)。

- 业务数据连本机 PostgreSQL 17 库 walmart_data(五 schema 见 docs/db_schema.md)。
- SQLite 缓存已整体 PG 化(旧 llm_cache.sqlite 见 docs/db_schema.md L2c),
  本文件不再开 SQLite 门;上面那条禁令依旧有效,真要用先在这里加入口。
"""

import contextlib
import os


def pg_dsn() -> str:
    """输入:无 → 输出:PostgreSQL DSN(env WALMART_PG_DSN 覆盖,默认本机 socket 连 walmart_data)。"""
    return os.environ.get("WALMART_PG_DSN", "dbname=walmart_data")


#: 单条 SQL 的缺省超时(秒),每条连接都带(2026-09-26)。库里原本没设超时:当天一条缺
#: 索引的查询逐条全表扫了 44 分钟,不报错、日报链整条卡住,没人知道。超时 = 那条 SQL
#: 报错 → 工作流失败 → cli 通知,**响亮失败好过无声卡死**。给得宽:本仓正常查询都在秒级;
#: 建大索引这类一次性操作(db_init)显式传 statement_timeout=0 关掉。附属步骤要更紧的
#: 自己在事务里 `SET LOCAL statement_timeout`(如 services/feed_effect)。
STATEMENT_TIMEOUT_S = 30 * 60


def _options(dsn: str, timeout_s: float) -> str:
    """输入:DSN + 超时秒数 → 输出:libpq options(保留 DSN 里原有的 options,追加超时)。"""
    from psycopg.conninfo import conninfo_to_dict

    base = str(conninfo_to_dict(dsn).get("options") or "").strip()
    return f"{base} -c statement_timeout={int(timeout_s * 1000)}".strip()


@contextlib.contextmanager
def pg_conn(autocommit: bool = False, statement_timeout: float | None = None):
    """输入:(可选 autocommit、单条 SQL 超时秒数)→ 输出:psycopg 连接上下文;总是 close。

    默认事务模式:正常退出 commit,异常 rollback。autocommit=True 给并发
    worker 的只读+幂等缓存写用(product_audit workers>1:每 worker 一条
    连接,写路径仍归主线程的事务连接)。

    statement_timeout:缺省 STATEMENT_TIMEOUT_S;0 = 不限(只给 db_init 这类一次性
    重操作用)。走连接参数(libpq options)而不是连上之后 SET:事务模式下 SET 会随
    第一个事务回滚而失效。

    用法:
        with db.pg_conn() as conn:
            conn.execute("INSERT ...", (...,))
    """
    import psycopg  # 惰性导入:让不碰 PG 的 workflow 在缺 psycopg 的环境也能运行

    dsn = pg_dsn()
    timeout_s = STATEMENT_TIMEOUT_S if statement_timeout is None else statement_timeout
    conn = psycopg.connect(dsn, autocommit=autocommit,
                           options=_options(dsn, timeout_s))
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

    批复 #3(2026-08-13):R5 商标反查跨库连它;灌库链路在外部仓,本仓永远只读。
    ⚠ **2026-09-03 C 批起本仓无消费方**:L2 R5 整条删除(默认关、
    `brand_nice_class` 覆盖率 2.6 万/1400 万)。库与外部灌库链路都还在,
    登记保留在这里等"按新流程重建"(所有者定稿 `docs/audit_pipeline.md` §10);
    在那之前**没有任何代码调它**。
    """
    return os.environ.get("USPTO_DSN", "dbname=uspto")


@contextlib.contextmanager
def uspto_conn():
    """输入:无 → 输出:USPTO 库**只读、autocommit**连接上下文(1400 万行)。

    autocommit 是批量消费的关键:整批共用一个连接,若开事务,第一条报错后
    事务进 aborted 态,后续每条查询都 InFailedSqlTransaction——"fail-soft"
    变成整轮静默失效(审核 R5 评审实证 2026-08-13)。只读查询无需事务语义。
    ⚠ 同 `uspto_dsn`:2026-09-03 C 批之后本仓无消费方。
    """
    import psycopg

    conn = psycopg.connect(uspto_dsn())
    try:
        conn.autocommit = True
        conn.read_only = True
        yield conn
    finally:
        conn.close()
