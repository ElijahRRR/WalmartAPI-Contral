"""feed_poll — 全局 feed 轮询(所有 feed 操作共用,plan 表外基础设施)。

用法:
  python cli.py feed_poll                 # 轮询 ops.feed_log 全部在途 feed

职责:扫 feed_log 的 submitted 行 → 查沃尔玛终态 → SKU 级结果落
ops.feed_items(权威台账)→ feed_log 落 done/failed;pending 行
(提交结局不确定)告警待人工。只读沃尔玛 + 记账,非危险。

轮询完执行**反哺器列表**(所有者定稿 2026-08-07:一切 feed 结果的表格
回写都交给轮询,业务表状态不依赖"记得再跑一次业务工作流"):每个反哺器
是一个 services 积木,纯读 ops.feed_items 台账写自己的业务表,幂等;
单个失败只告警不拖垮轮询本体和其它反哺器。未来上架/改价/改库存/改标题
feed 上线时,各自的反哺器在 _REFLECTORS 登记一行即接入。

与各业务工作流的关系:product_clear 等提交后自己也会轮询并刷新飞书投影列;
本工作流是**兜底与加密度**——业务工作流一天跑一次,它可以挂高频调度
(如每 30 分钟),让台账尽快落定、各业务表尽快见到结果。
"""

import logging

from services import clear_sheet, feed_track, maint_sheet, match_sheet, \
    stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.feed_poll")

# 业务表反哺器登记处:(名称, 无参函数 → 摘要行或 None)。
# 新 feed 工作流上线时在此追加,例:("上架表", listing_sheet.sync_from_ledger)
_REFLECTORS: list[tuple[str, object]] = [
    ("停用/删除表", clear_sheet.sync_from_ledger),
    ("维护记录", maint_sheet.sync_from_ledger),
    ("跟卖表", match_sheet.sync_from_ledger),
]


def run(params: dict) -> str:
    """输入:params(可选 store)→ 输出:轮询摘要(含各业务表反哺结果)。"""
    names = [params["store"]] if params.get("store") else None
    store_list = stores_svc.load_stores(names)
    stores_by_name = {s["name"]: s for s in store_list}
    lines = [feed_track.poll_all(stores_by_name)]
    for label, sync in _REFLECTORS:
        try:
            line = sync()
        except Exception as e:
            # 反哺失败不拖垮轮询本体:台账已落定,下轮或业务工作流补写
            logger.warning("%s 回写失败(台账已落定,下轮补写): %s", label, e)
            line = f"⚠ {label}回写失败:{e}"
        if line:
            lines.append(line)
    return "\n".join(lines)
