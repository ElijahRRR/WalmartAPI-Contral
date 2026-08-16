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

    查不到该店 ⇒ 调用方回落 `services.amz_source.MAX_LEAD_DAYS`(**7 天**,
    2026-08-15 从 8 收紧)。⚠ 常量名是 `lead_limit`,与分配链共用同一列同一常量
    —— 2026-08-16 合并时两边各建过一个,已合并为一个(见 registry 那处注释)。
    """
    caps = _int_map(resources.RETIRE_LIMITS.fields.lead_limit)
    if not caps:
        logger.info("限额表「配送时长限制」读到 0 店,全店回落默认上限"
                    "(表未登记/该列为空/列名对不上都会走到这里)")
    return caps


def cap_for(caps: dict[str, int], store: str, default: int) -> int:
    """输入:上限字典 + 店铺 + 默认值 → 输出:该店生效的上限。"""
    return caps.get(str(store or ""), default)


def retire_caps() -> dict[str, int]:
    """输入:无 → 输出:{店铺: 单轮下架/删除上限}(限额表「下架限制」列)。

    删除上限不该由代码拍脑袋(所有者问 2026-08-09「300 这个上限是从哪来的」):
    这张表就是运营给每家店定的下架配额,维护链的删除走同一个配额口径
    (与 product_clear 同一列)。店铺不在表内由调用方退
    `maintenance_intents.DELETE_PER_STORE` 并告警。
    """
    caps = _int_map(resources.RETIRE_LIMITS.fields.max_daily_retire)
    if not caps:
        logger.warning("限额表「下架限制」读到 0 店,删除上限全店走默认值")
    return caps


def stockzero_stores() -> list[str]:
    """输入:无 → 输出:整店清零的店名单(限额表「库存特殊要求」= 0 的店)。

    ⚠ 旧系统 `str(v or "")` 的 **0-falsy 陷阱**(整数 0 变成空串,名单静默为空
    ⇒ 该清零的店一件都没清,而且不报错)在这里用 _plain_text + 显式比较规避。
    不能复用上面的 `_int_map`:那个只收 >0 的值,而这里要的恰恰是 0。
    """
    t = resources.RETIRE_LIMITS
    f = t.fields
    try:
        recs = feishu.list_records(t, field_names=[f.store, f.inventory_note])
    except LookupError:
        return []
    out = []
    for rec in recs:
        name = feishu._plain_text(rec["fields"].get(f.store)).strip()
        note = feishu._plain_text(rec["fields"].get(f.inventory_note)).strip()
        if name and note == "0":
            out.append(name)
    return out


def price_multipliers() -> dict[str, dict]:
    """输入:无 → 输出:{店铺: 四个区间倍率的原文}(改价 provider 消费)。

    与 list_new 同一张表同一口径 —— 上架价与维护价两套口径会自己跟自己打架。
    """
    t = resources.RETIRE_LIMITS
    f = t.fields
    keys = ("fba_range1", "fba_range2", "fbm_range1", "fbm_range2")
    try:
        recs = feishu.list_records(
            t, field_names=[f.store] + [getattr(f, k) for k in keys])
    except LookupError:
        logger.warning("限额表未登记,改价意图本轮为空")
        return {}
    out: dict[str, dict] = {}
    for rec in recs:
        name = feishu._plain_text(rec["fields"].get(f.store)).strip()
        if name:
            out[name] = {k: feishu._plain_text(rec["fields"].get(getattr(f, k)))
                         for k in keys}
    return out


def over_lead_cap(lead_days, cap: int) -> bool:
    """输入:实测配送天数 + 上限 → 输出:是否超限。

    ⚠ **没采到(None)不算超限**:`or 0` 会把"未知"读成"当天达",方向反了。
    这条与 list_new 原注释同款,搬过来集中一处,免得两个消费方各判各的。
    """
    return lead_days is not None and int(lead_days) > int(cap)
