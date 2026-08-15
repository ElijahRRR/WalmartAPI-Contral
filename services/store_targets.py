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
    """输入:无 → 输出:{店铺: {gmv/orders/max_online/channel/channel_raw/categories}}。

    `categories` 是**准入大类集合**(类目1/2/3 三列非空值去重);
    **空集合 = 该店不限制类目**(所有者定稿 2026-08-15),调用方用
    `allowed(cfg_row, cat)` 判定,别自己写 `if not categories: pass`
    ——那句话正着写反着写都像对的,判定只留一处。

    表未登记(.env 缺 FEISHU_LIMITS_*)时抛 LookupError,由调用方决定
    是降级还是停——分配引擎必须停(没有容量上限与渠道就没法过硬闸),
    审计报告可以降级成"这一节跳过"。
    """
    t = resources.RETIRE_LIMITS.require()
    f = t.fields
    recs = feishu.list_records(t, field_names=[
        f.store, f.target_gmv_daily, f.target_orders_daily,
        f.max_online, f.channel_limit, f.category1, f.category2, f.category3])
    out: dict[str, dict] = {}
    for rec in recs:
        fields = rec.get("fields", {})
        name = feishu._plain_text(fields.get(f.store)).strip()
        if not name:
            continue
        channel, channel_raw = _channel(fields.get(f.channel_limit))
        cats = []
        for key in (f.category1, f.category2, f.category3):
            v = feishu._plain_text(fields.get(key)).strip()
            if v and v not in cats:
                cats.append(v)
        out[name] = {
            "gmv": _num(fields.get(f.target_gmv_daily)),
            "orders": _num(fields.get(f.target_orders_daily)),
            "max_online": _num(fields.get(f.max_online)),
            "channel": channel,
            "channel_raw": channel_raw,
            "categories": cats,
        }
    return out


def allowed(cfg_row: dict | None, category: str | None) -> bool:
    """输入:某店配置行 + 产品大类 → 输出:该店能不能接这个大类。

    两条口径(所有者 2026-08-15):**表里三列都空 = 不限制**(放行一切);
    填了就**只准入填的那几个**。产品归不到大类(category 为 None)时,
    受限店拒收(宁可不分也不错分),不限制店放行。
    """
    cats = (cfg_row or {}).get("categories") or []
    if not cats:
        return True
    return bool(category) and category in cats


def accepts_allocation(cfg_row: dict | None) -> bool | None:
    """输入:某店配置行 → 输出:参不参与分配;**没填返回 None**(三态)。

    所有者定稿 2026-08-15 晚:**不想让它接货的店,「单店最大在线数」填 0**。
    这一列于是既是容量上限也是参与开关——与类目、配送限制同一治理方式:
    改表格一格就改了行为,不建表、不改代码、不看店铺状态。
    (店铺 SUSPENDED **不**代表不参与:那只让它当全新店,见
     `alloc_survey.is_dormant`。要不要接货只看这一格。)

    ⚠ 三态不许压成两态:`0` = 所有者按下了"不接货"这个开关,
    `None` = 还没填(报告点名补填,见 `missing_config`)。写成
    `not max_online` 会把没填的店一并算成不接货 —— 它于是永远分不到货,
    而且不报错,正是本模块开头那条空值纪律要防的事。
    """
    v = (cfg_row or {}).get("max_online")
    return None if v is None else v > 0


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
        if accepts_allocation(c) is False:
            continue        # 「单店最大在线数」填了 0 = 不接货,其余三列填不填都无所谓
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
