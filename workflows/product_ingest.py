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

# 游标/翻页/落库的实现在 services/product_ingest.pump —— order_audit 的
# `-p wait=1` 要就地跑同一件事,而工作流之间不准互相 import(铁律 1)。
# 本工作流仍是**全项目唯一直连采集服务取数的调度入口**;order_audit 借这条
# 泵时必须先拿本工作流的 flock(见 services/runlock),两个进程同推游标会
# 静默丢一段数据。


def run(params: dict) -> str:
    """输入:params(limit/max_pages/cursor)→ 输出:摄取摘要。"""
    cursor = params.get("cursor")
    if cursor is not None:
        logger.warning("游标被显式重置为 %s(全量对账场景)", cursor)
    res = ingest.pump(scraper, db,
                      limit=int(params.get("limit", 500)),
                      max_pages=int(params.get("max_pages", 0)) or None,
                      cursor=cursor)
    return ingest.pump_summary(res)
