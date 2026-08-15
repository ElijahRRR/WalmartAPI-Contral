"""店铺分配配置:从「上下架限额表」读目标三列 + 配送限制(分配链唯一出处)。

四列都是所有者在飞书人工维护(2026-08-12 / 08-13 建列),程序只读:
  目标销售额 / 目标订单  —— **日目标**(不是月目标,公式里别当月用)
  单店最大在线数        —— 总容量上限(≠「上架限制」列的日配额)
  配送限制              —— fba / fbm,一店一渠道的权威(未填=不接自由流分配)

与 `workflows/list_new._load_quota` 的分工:那边读「上架限制」日配额,
本模块读分配用的四列;同一张表两个读者,字段常量都在 registry。

**空值语义**:未填一律 None,**绝不退化成 0**——0 是"目标为零"(不该接货),
None 是"还没填"(引擎报告里点名要求补填)。两者混淆会让没填目标的店被算成
缺口 0 而永远分不到货,且没有任何报错。
"""

import logging

from api import feishu
from registry import resources

logger = logging.getLogger("services.store_targets")

CHANNELS = ("FBA", "FBM")


def _num(v):
    """输入:单元格 → 输出:float 或 None(空/非数字都是 None,不退 0)。"""
    s = feishu._plain_text(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _channel(v) -> tuple[str | None, str]:
    """输入:配送限制单元格 → 输出:(归一渠道 或 None, 原文)。

    大小写随手填(fba/FBA/Fba)都认;认不出的原文回传给调用方报告,
    **不猜也不默认**——猜错等于把 FBM 的货分给 FBA 店。
    """
    raw = feishu._plain_text(v).strip()
    up = raw.upper()
    return (up if up in CHANNELS else None), raw


def load_targets() -> dict[str, dict]:
    """输入:无 → 输出:{店铺: {gmv/orders/max_online/channel/channel_raw}}。

    表未登记(.env 缺 FEISHU_LIMITS_*)时抛 LookupError,由调用方决定
    是降级还是停——分配引擎必须停(没有容量上限与渠道就没法过硬闸),
    审计报告可以降级成"这一节跳过"。
    """
    t = resources.RETIRE_LIMITS.require()
    f = t.fields
    recs = feishu.list_records(t, field_names=[
        f.store, f.target_gmv_daily, f.target_orders_daily,
        f.max_online, f.channel_limit])
    out: dict[str, dict] = {}
    for rec in recs:
        fields = rec.get("fields", {})
        name = feishu._plain_text(fields.get(f.store)).strip()
        if not name:
            continue
        channel, channel_raw = _channel(fields.get(f.channel_limit))
        out[name] = {
            "gmv": _num(fields.get(f.target_gmv_daily)),
            "orders": _num(fields.get(f.target_orders_daily)),
            "max_online": _num(fields.get(f.max_online)),
            "channel": channel,
            "channel_raw": channel_raw,
        }
    return out


def missing_config(cfg: dict[str, dict], stores: list[str]) -> dict[str, list]:
    """输入:配置表 + 在营店名 → 输出:{店铺: [缺哪几项]}(全填的店不出现)。

    引擎硬闸的前置检查:缺 channel 不接自由流,缺 max_online 算不了容量,
    缺 gmv 算不了缺口。报告点名比静默跳过强——静默跳过时所有者只会看到
    "这家店怎么一件都没分到"。
    """
    out: dict[str, list] = {}
    for s in stores:
        c = cfg.get(s)
        if c is None:
            out[s] = ["未在限额表登记"]
            continue
        miss = [label for key, label in (
            ("channel", "配送限制"), ("max_online", "单店最大在线数"),
            ("gmv", "目标销售额"), ("orders", "目标订单"))
            if c.get(key) is None]
        if c.get("channel") is None and c.get("channel_raw"):
            miss = [m if m != "配送限制" else f"配送限制(填了「{c['channel_raw']}」认不出)"
                    for m in miss]
        if miss:
            out[s] = miss
    return out
