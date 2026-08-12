"""产品来源登记簿积木(catalog.listing_sources;所有者定稿 2026-08-07)。

背景:sku=asin 约定只对 amz 搬运品成立——跟卖 SKU 是人工/自动编号,
未来还有自建、1688 等来源。旧系统靠"SKU 格式不像 ASIN 就全排除"防误伤,
代价是这些产品成了自动化盲区。新规矩:**谁上架谁登记,自动化按出身路由,
手动通道全格式通吃**。

路由铁律(消费方契约):任何由"源数据查不到/缺失"驱动的自动破坏动作
(如 amz 采集不到 → 清库存/删除),必须 JOIN 本表限定 source_type 匹配;
source_type='unknown' 的行一律不自动动。maintenance 的 price/inventory/
title provider 做实时必须带上这条(services/maintenance_intents.py 契约)。

调整成本:出身是数据不是代码——改归类 = UPDATE 一行;新增来源 = 新
source_type 取值 + 对应 provider,管道零改动。
"""

import logging

logger = logging.getLogger("services.listing_sources")

# source_type 取值登记(新来源在此登记,消费方禁止散落字符串字面量)
SOURCE_AMZ = "amz"          # 亚马逊搬运(source_key=asin;sku=asin 约定适用)
SOURCE_MATCH = "match"      # 跟卖(source_key=匹配 GTIN;sku 为人工/自动编号)
SOURCE_SELF = "self"        # 自建产品库(预留)
SOURCE_1688 = "1688"        # 1688 货源(预留)
SOURCE_UNKNOWN = "unknown"  # 存量格式回填未能归类;不参与任何自动破坏动作


def register(conn, rows: list[dict]) -> int:
    """输入:连接 + [{store, sku, source_type, source_key?, workflow}]
    → 输出:写入数。首次登记优先(ON CONFLICT 不覆盖);改归类走人工 UPDATE。"""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO catalog.listing_sources "
            "(store, sku, source_type, source_key, workflow) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (store, sku) DO NOTHING",
            [(r["store"], r["sku"], r["source_type"],
              r.get("source_key"), r.get("workflow")) for r in rows])
    return len(rows)
