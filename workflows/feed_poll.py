"""feed_poll — 全局 feed 轮询(所有 feed 操作共用,plan 表外基础设施)。

用法:
  python cli.py feed_poll                 # 轮询 ops.feed_log 全部在途 feed

职责:扫 feed_log 的 submitted 行 → 查沃尔玛终态 → SKU 级结果落
ops.feed_items(权威台账)→ feed_log 落 done/failed;pending 行
(提交结局不确定)告警待人工。只读沃尔玛 + 记账,非危险。

与各业务工作流的关系:daily_retire 等提交后自己也会轮询并刷新飞书投影列;
本工作流是**兜底与加密度**——业务工作流一天跑一次,它可以挂高频调度
(如每 30 分钟),让台账尽快落定,未来上架/改价/改库存/改标题 feed 同享。
"""

import logging

from services import feed_track, stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.feed_poll")


def run(params: dict) -> str:
    """输入:params(可选 store)→ 输出:轮询摘要。"""
    names = [params["store"]] if params.get("store") else None
    store_list = stores_svc.load_stores(names)
    stores_by_name = {s["name"]: s for s in store_list}
    return feed_track.poll_all(stores_by_name)
