"""order_center_push — 订单中心六表**手动全量补推 / 对账入口**(不进调度)。

用法:
  python cli.py order_center_push                  # 全部表(销售/售后窗口 90 天)
  python cli.py order_center_push -p table=sales   # 只推一张(sales/returns/perf/settlement/keys)
  python cli.py order_center_push -p days=180      # 自定窗口(销售按下单时间,售后按发起时间,
                                                   #   对账按最近入账日;绩效表全量,自身有界)
  python cli.py order_center_push -p reconcile=1   # 强制全量对账(清本地同步状态重建映射)

⚠ **日常不用跑它**(2026-08-16 起,所有者定稿:「已经对接了飞书表的,在执行
完成后都需要及时写入,不要做成单独的了」)。四条业务链各自跑完就写自己那张:

    order_sync       → 销售表 + 主订单/采购信息的键补齐
    returns_sync     → 售后表
    perf_problems    → 绩效表
    settlement_sync  → 对账表

投影逻辑全部住在 `services/order_center`(铁律 1:工作流之间不准互相 import)。
本工作流退化成一层壳,留给三种场合:
  ① 手动补推(某条链的投影那一步失败过,数据在 PG 但表格没动);
  ② `-p reconcile=1` 全量对账(怀疑表被手工动过、行被删过);
  ③ 建库/换表之后一次性铺满。

与业务链顺手写的那次唯一的差别:**这里失败会让本次运行记成 failed**
(你是专门来推的,推不成就得知道);业务链那次只告警不失败(数据已经在 PG,
不该让飞书拖累一轮数据拉取的成败判定)。
"""

import logging

from services import order_center

DANGEROUS = False

logger = logging.getLogger("workflows.order_center_push")


def run(params: dict) -> str:
    """输入:params(可选 table/days/reconcile)→ 输出:各表同步结果摘要。"""
    only = str(params.get("table", "")).strip()
    if only and only not in order_center.TABLES:
        return (f"table 只接受 {'/'.join(order_center.TABLES)},收到:{only}")
    days = int(params.get("days", 90))
    reconcile = str(params.get("reconcile", "")) in ("1", "true", "yes")

    lines, failed = order_center.push_tables(
        (only,) if only else order_center.TABLES, days, reconcile)
    if failed:
        # 手动补推入口:推不成必须记 failed(与业务链顺手写的那次相反)
        raise RuntimeError("部分表同步失败:" + ";".join(lines))
    return "\n".join(lines)
