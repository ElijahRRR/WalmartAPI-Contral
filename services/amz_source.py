"""上架数据源 provider(listing L2d;采集服务改造中,接口预留)。

产品数据契约(provider 产出的统一形态,mapper/主链只认这个,来源无关):
  {"asin": str, "title": str, "brand": str|None, "category": str|None,
   "price": float|None,          # amz 现价(定价输入)
   "stock": int|None,            # amz 可见库存(<5 淘汰,MIN_INVENTORY)
   "lead_days": int|None,        # 配送时长(>12 天上架但库存写 0)
   "channel": "FBA"|"FBM"|None,  # 定价区间路由
   "images": [url, ...],         # 已按防御性排序(来源侧 set() 去重打乱顺序)
   "attrs": {...}}               # 其余原始属性(LLM 映射输入)

Provider 面:
  fetch_products(asins)  ⏳ 采集服务改造完成后接现有导出端点/增量契约填实;
                         当前返回空 dict 并告警"数据源不可用"。
接入时只填函数体,主链零改动(与 maintenance provider 同款纪律)。
"""

import logging

logger = logging.getLogger("services.amz_source")

MIN_INVENTORY = 5           # 库存 <5 不上架(旧 MIN_INVENTORY_THRESHOLD)
MAX_LEAD_DAYS = 12          # 配送 >12 天仍上架但库存写 0(旧值)


def fetch_products(asins: list[str]) -> dict[str, dict]:
    """输入:ASIN 列表 → 输出:{asin: 产品数据契约 dict}。

    ⏳ 预留(所有者 2026-08-07:产品数据源暂时不可用):采集服务改造完成后
    在此接入(现有 export 端点或增量契约 v1),缺席的 ASIN 不出现在结果里
    (主链按"数据缺失"跳过该行,不写终态——数据源恢复后自动续上)。
    """
    if asins:
        logger.warning("产品数据源不可用(采集服务改造中),%d 个 ASIN 本轮跳过",
                       len(asins))
    return {}
