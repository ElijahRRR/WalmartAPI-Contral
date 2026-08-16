"""上下架限额表(registry.RETIRE_LIMITS)的按店读取积木。

三个 workflow(list_new / maintenance / product_clear)各自写过一份"拉这张表、
按店铺列建字典"的循环。本模块把那段收成一处 —— 新增一列时改一个地方,
而不是三处各改一遍(改漏的那处会静默读空,全店回落默认值且不报错)。

⚠ 「读不到」与「填了 0」必须分开:
  - 表未登记 / 该店没这一行 / 单元格为空 ⇒ **返回默认值**(不知道 ≠ 限死)
  - 单元格填了 0 或负数 ⇒ 视同没填(限额为 0 = 这条链整店停摆,不像人的本意;
    真要停某店有 `店铺状态` 与 stockzero 两条显式路径)
"""

import logging

from api import feishu
from registry import resources

logger = logging.getLogger("services.store_limits")


def _int_map(field_name: str) -> dict[str, int]:
    """输入:限额表列名常量 → 输出:{店铺: 正整数};读不到返回空 dict。"""
    t = resources.RETIRE_LIMITS
    f = t.fields
    try:
        recs = feishu.list_records(t, field_names=[f.store, field_name])
    except LookupError:
        return {}
    out: dict[str, int] = {}
    for rec in recs:
        name = feishu._plain_text(rec["fields"].get(f.store)).strip()
        try:
            v = int(float(feishu._plain_text(rec["fields"].get(field_name)) or 0))
        except ValueError:
            v = 0
        if name and v > 0:
            out[name] = v
    return out


def lead_day_caps() -> dict[str, int]:
    """输入:无 → 输出:{店铺: 配送时长上限(天)};未配置的店不在字典里。

    所有者定稿 2026-08-16(走进生产)。两个消费方口径**不同**,别混:
      - **上架**(list_new):超限 ⇒ **不上架**(此前是"上架但库存写 0")。
        不上架就不占 UPC、不占配额,比上一个卖不动的更省。
      - **维护**(maintenance):超限 ⇒ **库存写 0**。它已经在架了,只能压库存。

    查不到该店 ⇒ 调用方回落 `services.amz_source.MAX_LEAD_DAYS`(8 天)。
    """
    caps = _int_map(resources.RETIRE_LIMITS.fields.max_lead_days)
    if not caps:
        logger.info("限额表「配送时长限制」读到 0 店,全店回落默认上限"
                    "(表未登记/该列为空/列名对不上都会走到这里)")
    return caps


def cap_for(caps: dict[str, int], store: str, default: int) -> int:
    """输入:上限字典 + 店铺 + 默认值 → 输出:该店生效的上限。"""
    return caps.get(str(store or ""), default)


def over_lead_cap(lead_days, cap: int) -> bool:
    """输入:实测配送天数 + 上限 → 输出:是否超限。

    ⚠ **没采到(None)不算超限**:`or 0` 会把"未知"读成"当天达",方向反了。
    这条与 list_new 原注释同款,搬过来集中一处,免得两个消费方各判各的。
    """
    return lead_days is not None and int(lead_days) > int(cap)
