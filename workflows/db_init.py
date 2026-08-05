"""db_init — 执行 refdata/schema.sql 建四 schema(幂等),可选创建只读角色 readonly。

用法:python cli.py db_init
前置:本机 PostgreSQL 17 已建库 walmart_data(createdb walmart_data)。
表结构事实来源是 docs/db_schema.md;本工作流只执行其同步产物 refdata/schema.sql。
只读角色:设了 READONLY_DB_PASSWORD 才创建/授权,供 Metabase/NocoDB/MCP 使用。
"""

import logging
import os
from pathlib import Path

from registry import db

DANGEROUS = False

logger = logging.getLogger("workflows.db_init")

_SCHEMA_SQL = Path(__file__).resolve().parent.parent / "refdata" / "schema.sql"

_READONLY_SQL = """
DO $do$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'readonly') THEN
        EXECUTE format('CREATE ROLE readonly LOGIN PASSWORD %L', {pw});
    ELSE
        EXECUTE format('ALTER ROLE readonly WITH LOGIN PASSWORD %L', {pw});
    END IF;
END $do$;
GRANT USAGE ON SCHEMA catalog, listing, orders, ops TO readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA catalog, listing, orders, ops TO readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA catalog, listing, orders, ops
    GRANT SELECT ON TABLES TO readonly;
"""


def run(params: dict) -> str:
    """输入:params(无参数)→ 输出:建库结果摘要(schema 数、readonly 角色是否配置)。"""
    sql = _SCHEMA_SQL.read_text(encoding="utf-8")
    with db.pg_conn() as conn:
        conn.execute(sql)

        readonly_note = "readonly 角色:跳过(READONLY_DB_PASSWORD 未设)"
        pw = os.environ.get("READONLY_DB_PASSWORD", "").strip()
        if pw:
            from psycopg import sql as _sql

            # DO 块不支持服务端参数绑定,口令用 psycopg 客户端 Literal 转义后拼入;
            # DO 块内部再经 format(%L) 二次转义成 CREATE/ALTER ROLE 字面量
            conn.execute(_sql.SQL(_READONLY_SQL).format(pw=_sql.Literal(pw)))
            readonly_note = "readonly 角色:已创建/更新并授权"

    return f"schema.sql 执行完成(catalog/listing/orders/ops 幂等就绪);{readonly_note}"
