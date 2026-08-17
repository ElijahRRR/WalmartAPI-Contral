"""upc_sync — UPC 池注入同步与投影回写(listing L2a;只读沃尔玛零调用,非危险)。

用法:
  python cli.py upc_sync            # 表格新号入库 + 状态列回写 + 池余量摘要

流程:读「UPC池」表(A=UPC B=放入日期,运营注入域)→ 新号入库
(catalog.upc_pool,首位白名单校验,非法前缀标注永不分配)→ 按 PG 权威
回写 C=状态 D=店铺 E=SKU F=上架日期(仅差异行)→ 摘要报池余量。

⚠ 两步的实现都在 `services/upc_pool`(`sync_from_sheet` / `project_to_sheet`),
本工作流只是把它们串起来 —— **注入那一步 `list_new` 也要用**
(所有者定稿 2026-08-16:上架运行时自动同步一次 UPC),而铁律 1 禁止
workflow 互相 import。各写一份迟早飘成"上架看到的池"与"本工作流看到的池"
不是同一个。

调度建议:每日一次;上架主链跑前不必单独安排,`list_new` 自己会先注入一次。
领号/用号/回收由主链在提交事务内调 services/upc_pool,本工作流只管注入与展示。
"""

import logging

from registry import db
from services import upc_pool

DANGEROUS = False

logger = logging.getLogger("workflows.upc_sync")


def run(params: dict) -> str:
    """输入:params(无参)→ 输出:注入与回写摘要。"""
    try:
        with db.pg_conn() as conn:
            got = upc_pool.sync_from_sheet(conn)
            if not got["rows"]:
                return "UPC池表无数据行"
            written = upc_pool.project_to_sheet(conn, got["rows"])
            stats = upc_pool.pool_stats(conn)
    except LookupError as e:
        return f"UPC池表未登记:{e}"

    return (f"UPC池:表内 {len(got['rows'])} 行,新入库 {got['new']}"
            + (f",非法前缀 {got['bad']}(已标注永不分配)" if got["bad"] else "")
            + f",状态回写 {written} 行;"
            f"池余量:未用 {stats.get('', 0)},已领 {stats.get('claimed', 0)},"
            f"已用 {stats.get('used', 0)},冲突 {stats.get('conflict', 0)}")
