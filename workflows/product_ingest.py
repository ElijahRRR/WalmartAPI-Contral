"""product_ingest — 采集服务增量 → 产品中心(catalog.products / snapshots)。

用法:
  python cli.py product_ingest                    # 从上次游标续拉到追平
  python cli.py product_ingest -p limit=200       # 每页条数(≤1000,默认 500)
  python cli.py product_ingest -p max_pages=5     # 本轮最多拉几页(接线试跑用)
  python cli.py product_ingest -p cursor=0        # 强制从头拉(全量对账后重置用)

**全项目唯一直连采集服务的工作流**(漏斗铁律):其余一切消费方
(list_new / maintenance provider / 审核 / 分配)只读中心库,不碰采集器。

游标存 ops.cursors(name='product_ingest'),推进只认响应的 next_cursor——
空页原样不推进是唯一不丢数据的方向(契约边界语义)。

收到 409 cursor_below_retention(要的数据已被采集侧保留期裁掉):**立即停,
游标不动,飞书告警**,人工全量对账后用 -p cursor=N 重置。绝不当普通错误重试
——那会一路跳过被裁掉的区间,静默丢数据。
"""

import logging

from api import scraper
from registry import db
from services import product_ingest as ingest

DANGEROUS = False

logger = logging.getLogger("workflows.product_ingest")

_CURSOR_NAME = "product_ingest"


def _load_cursor() -> int:
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM ops.cursors WHERE name = %s",
                    (_CURSOR_NAME,))
        row = cur.fetchone()
    if not row:
        return 0
    return int((row[0] or {}).get("cursor", 0))


def _save_cursor(value: int) -> None:
    with db.pg_conn() as conn:
        conn.execute(
            "INSERT INTO ops.cursors (name, value, updated_at)"
            " VALUES (%s, jsonb_build_object('cursor', %s::bigint), now())"
            " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value,"
            " updated_at = now()", (_CURSOR_NAME, value))


def run(params: dict) -> str:
    """输入:params(limit/max_pages/cursor)→ 输出:摄取摘要。"""
    limit = int(params.get("limit", 500))
    max_pages = int(params.get("max_pages", 0)) or None
    if params.get("cursor") is not None:
        cursor = int(params["cursor"])
        logger.warning("游标被显式重置为 %d(全量对账场景)", cursor)
    else:
        cursor = _load_cursor()
    start_cursor = cursor

    total = {"snapshots": 0, "dup": 0, "products": 0, "skipped_outcome": 0,
             "incomplete": 0, "invalid": 0}
    pages = fetched = 0
    while True:
        try:
            records, next_cursor, has_more = scraper.export_incremental(
                cursor, limit)
        except scraper.RetentionGapError as e:
            # 游标停在原地:重置由人工在全量对账后显式做(-p cursor=N)
            return (f"⛔ 保留期缺口,已停止(游标仍为 {cursor},未推进):{e}\n"
                    f"本轮已入库:观测 {total['snapshots']} 条 / "
                    f"身份 {total['products']} 条;需全量对账后 "
                    f"-p cursor=<新起点> 重启")
        except scraper.ExportAuthError as e:
            return f"⛔ 导出鉴权失败(游标未推进):{e}"

        pages += 1
        fetched += len(records)
        if records:
            with db.pg_conn() as conn:
                counts = ingest.ingest_batch(conn, records)
            for k, v in counts.items():
                total[k] = total.get(k, 0) + v      # outcome_* 键是动态的

        # 空页不推进(next_cursor 原样返回);有数据才落游标
        if next_cursor != cursor:
            _save_cursor(next_cursor)
            cursor = next_cursor
        if not has_more or not records:
            break
        if max_pages and pages >= max_pages:
            logger.info("达到 max_pages=%d,本轮提前收尾(游标已落 %d)",
                        max_pages, cursor)
            break

    line = (f"增量摄取:{pages} 页 / 拉取 {fetched} 条;"
            f"观测入库 {total['snapshots']}(重复跳过 {total['dup']}),"
            f"身份更新 {total['products']};游标 {start_cursor} → {cursor}")
    if total["skipped_outcome"]:
        dist = ",".join(f"{k[len('outcome_'):]}×{v}"
                        for k, v in sorted(total.items())
                        if k.startswith("outcome_"))
        line += f",非 ok 采集只落观测 {total['skipped_outcome']}({dist})"
    if total["incomplete"]:
        line += f",completeness_ok=false {total['incomplete']}(空值未覆盖旧值)"
    if total["invalid"]:
        line += f",⚠ 缺 asin/source_id 丢弃 {total['invalid']}"
    return line
