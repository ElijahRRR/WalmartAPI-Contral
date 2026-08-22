"""店铺分配配置:从「上下架限额表」读目标三列 + 配送限制(分配链唯一出处)。

四列都是所有者在飞书人工维护(2026-08-12 / 08-13 建列),程序只读:
  目标销售额 / 目标订单  —— **日目标**(不是月目标,公式里别当月用)
  单店最大在线数        —— 总容量上限(≠「上架限制」列的日配额)
  配送限制              —— fba / fbm,一店一渠道的权威(未填=不接自由流分配)
  配送时长限制          —— 只分配 delivery_days ≤ 该值的产品(未填=不限)

与 `workflows/list_new._load_quota` 的分工:那边读「上架限制」日配额,
本模块读分配用的那几列;同一张表两个读者,字段常量都在 registry。

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
        f.max_online, f.channel_limit, f.lead_limit,
        f.category1, f.category2, f.category3])
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
            "lead_limit": _num(fields.get(f.lead_limit)),
            "categories": cats,
        }
    return out


def super_categories_of(cfg_row: dict | None) -> set:
    """输入:某店配置行 → 输出:该店准入的**品类桶**集合(五大品类 + 「其他」)。

    ★ 2026-08-22 改判 `super_bucket`(所有者:建议列要「填写 5 大类和其他」)。
    改之前填「其他 / Safety & Emergency / Everything Else」的店会折成**空集**
    ⇒ 按「填了就只准入填的那几个」判 ⇒ **谁也接不了**,一家店被静默废掉。
    改之后这类店的语义是"专收归不到五品类的货",与所有者 2026-08-21 那句
    「不归,可以分配给没有确定类目的店」相容 —— 那是"可以给没填的店",
    不是"只能给没填的店"。

    ⚠ 由此 **空集 ⟺ 三列全空**(唯一来源),原来那条"两种空集不能混"的
    警告随之消失:任何非空填写值都折得出一个桶(认不出的归「其他」)。
    代价是拼写错不再"响亮地废掉一家店",而是让它静默变成只收「其他」——
    所以 `alloc_audit` 必须逐店点名认不出的填写值(`known_category_literal`)。
    """
    cats = (cfg_row or {}).get("categories") or []
    return {resources.super_bucket(c) for c in cats} - {None}


def allowed(cfg_row: dict | None, category: str | None) -> bool:
    """输入:某店配置行 + 产品大类 → 输出:该店能不能接这个大类。

    两条口径(所有者 2026-08-15):**表里三列都空 = 不限制**(放行一切);
    填了就**只准入填的那几个**。产品归不到大类时,受限店拒收(宁可不分也
    不错分),不限制店放行。

    ★ **判定在五大品类那一层**(所有者 2026-08-21 拍 Q1)。两边都折一次:
    店填「Furniture」= 它做 Home 品类,产品是 Furniture 也是 Home 品类,
    过。**准入类目列不用重填** —— 26 类与五品类的名字都认。
    为什么改:按 26 类判时,品牌组内的少数派件 156,188 件(全池 24.2%)会
    被锁死在做不了那个大类的店里(品牌排他要求整组同店);折到五品类是
    105,571 件(16.3%)。⚠ 代价所有者已认:「一店最多两大类」在这一层几乎
    失效 —— Home + Hardlines = 他 91% 的货。

    ★ **「其他」是一等值**(所有者 2026-08-22)。归不到五品类的货
    (Safety & Emergency / Everything Else)可以去**没填类目的店**,也可以去
    **明确填了「其他」的店** —— 后者是 2026-08-22 新开的一条,此前填「其他」
    会把店折成空集而废掉。两边都走 `super_bucket`,所以自洽。

    ★ **一个绝不许合并的区分**:`category` 为**空**(大类没采到)返回 False,
    而不是当成「其他」。空是数据缺口(处置是补采集),「其他」是业务归类
    (处置是找一家收「其他」的店)。合并会让填了「其他」的店开始收一批
    **我们根本不知道是什么**的货 —— 而且 `category_offenders` 那条
    "不知道不算违规"的纪律也会跟着塌。
    """
    cats = (cfg_row or {}).get("categories") or []
    if not cats:
        return True                          # 三列全空 = 不限制(唯一的放行一切)
    want = resources.super_bucket(category)
    if want is None:
        return False                         # 大类采不到 → 受限店一律拒收
    return want in super_categories_of(cfg_row)


def lead_cap_of(cfg_row: dict | None) -> int:
    """输入:某店配置行 → 输出:该店**生效**的配送时长上限(天)。

    未填 ⇒ 回落 `amz_source.MAX_LEAD_DAYS`(7),与上架链
    `store_limits.cap_for(caps, store, MAX_LEAD_DAYS)` 同一个回落。
    """
    from services import amz_source          # 惰性:避免 registry ← services 绕回
    cap = (cfg_row or {}).get("lead_limit")
    return int(amz_source.MAX_LEAD_DAYS if cap is None else cap)


def lead_ok(cfg_row: dict | None, lead) -> bool:
    """输入:某店配置行 + 产品配送天数 → 输出:该店收不收这个货期。

    口径(所有者 2026-08-16 建列,**2026-08-21 改了回落方向**):
    填了就只准入 `delivery_days <= 限制`;**未填回落 7 天**,不是"不限"。

    ★ 为什么改:同一列「配送时长限制」,两条链的"未填"回落**方向相反** ——
    上架链 `store_limits.cap_for(caps, store, MAX_LEAD_DAYS)` 未填回落 7,
    分配这边原本未填就放行一切。所有者要求两边相互关联(2026-08-21),
    统一到 7。影响面:只要有**一家店**空着这一列,原写法会让
    `alloc_plan._pool_reach` 的并集变成"不限",慢货全池涌入 —— 而且不报错。

    ⚠ **产品没采到配送天数(lead 为 None)时一律拒收**:与类目那条
    「归不到大类的,受限店拒收」同一纪律 —— 宁可不分也不错分。拿"没采到"
    当"够快"是替所有者做了他没做的决定。**现在没有"不限"的店了,所以这条
    对每一家店都生效**(改回落之前,未填的店会把未知货期照单全收)。
    """
    return lead is not None and float(lead) <= float(lead_cap_of(cfg_row))


def accepts_allocation(cfg_row: dict | None) -> bool | None:
    """输入:某店配置行 → 输出:参不参与分配;**没填返回 None**(三态)。

    所有者定稿 2026-08-15 晚:**不想让它接货的店,「单店最大在线数」填 0**。
    这一列于是既是容量上限也是参与开关——与类目、配送限制同一治理方式:
    改表格一格就改了行为,不建表、不改代码、不看店铺状态、**也不动占用**。

    ⚠ 与店铺状态是两件事:SUSPENDED 的店占用照旧保持、在线行照常计入冲突
    (「暂停不释放」,plan §六.2);它接不接新货只看这一格。反过来也一样:
    这一格填 0 不释放它任何占用 —— 释放只有 `store_release` 一条路。

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
        # ⚠ 「配送时长限制」不进这张清单:未填 = 不限,是**合法配置**,
        # 不是漏填(与类目三列同一治理方式)。点名它会让人以为必须填
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
