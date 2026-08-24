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

from api import feishu, settings
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


def maint_nodes() -> dict[str, str]:
    """输入:无 → 输出:{店铺: 受管发货节点 FC ID};**未填的店不在字典里**。

    多仓改造的唯一配置入口(所有者定稿 2026-08-24)。与「配送限制」同款治理:
    人在飞书填、程序直读、没填就是现状(Virtual Node)。

    ⚠ **这里只读不校验**。"填的这个 FC ID 认不认识"要调沃尔玛
    (`api.settings.list_ship_nodes`),而本模块在 services 层、只碰飞书 ——
    校验归 `resolve_node()`,它才是各链的入口。分开的理由是读表要一次拿全店
    (一个飞书请求),校验却是逐店调沃尔玛。
    """
    t = resources.RETIRE_LIMITS
    f = t.fields
    try:
        recs = feishu.list_records(t, field_names=[f.store, f.maint_node])
    except LookupError:
        return {}
    out: dict[str, str] = {}
    for rec in recs:
        name = feishu._plain_text(rec["fields"].get(f.store)).strip()
        node = feishu._plain_text(rec["fields"].get(f.maint_node)).strip()
        if name and node:
            out[name] = node
    if out:
        logger.info("受管发货节点:%d 家店已配置「维护仓库」", len(out))
    return out


class NodeConfigError(RuntimeError):
    """「维护仓库」填了但沃尔玛不认识 —— 该店整店跳过的信号(fail-closed)。"""


def resolve_node(store: dict, nodes: dict[str, str]) -> str | None:
    """输入:店铺 + maint_nodes() 的字典 → 输出:受管节点 FC ID;未配置返回 None。

    **各链取受管仓的唯一入口**(上架/维护/清零都走它,别各写一遍)。

    校验是 fail-closed 的:填了值就必须在 `GET shipnodes` 的列表里找得到,
    找不到 **抛 NodeConfigError**,由调用方整店跳过并告警。
    ⚠ 为什么不"认不出就回落 Virtual Node":那等于把本该进新仓的货写到旧节点,
    而且全程不报错 —— 比"这店今天没动"坏得多。填错一个字符的代价必须是
    响亮失败,不是静默走偏。

    ⚠ **查不到节点列表(接口失败)也算认不出**:同理,宁可这店今天不动。
    未配置的店根本不调沃尔玛(现状零成本、零行为变化)。
    """
    node = (nodes or {}).get(store["name"])
    if not node:
        return None
    try:
        known = settings.list_ship_nodes(store)
    except Exception as e:                          # noqa: BLE001
        raise NodeConfigError(
            f"{store['name']}:「维护仓库」填了 {node},但发货节点列表读不到"
            f"({e})—— 本轮整店跳过,不回落 Virtual Node") from e
    if node not in known:
        raise NodeConfigError(
            f"{store['name']}:「维护仓库」填的 {node} 不在该店发货节点列表里"
            f"(认识的:{sorted(known) or '(空)'})—— 本轮整店跳过。"
            f"FC ID 见 Seller Center → Shipping Profile → Seller Fulfillment")
    return node


def managed_nodes(stores: list[dict] | None = None
                  ) -> tuple[dict[str, str], dict[str, str]]:
    """输入:店铺列表(None=按需自取)→ 输出:({店铺: 已校验的受管仓}, {跳过的店: 原因})。

    **各链拿受管仓表的唯一入口**(维护链/上架链/清零共用)。把「读表」与
    「逐店校验」合成一次调用,是因为两件事必须成对发生:只读不校验 =
    填错一个字符就静默写到别的节点;只校验不汇总 = 摘要报不出"今天几家店
    生效、几家被跳过"(计划 §6 第 6 条要求配置生效与否天天见人)。

    跳过的店(NodeConfigError)**不进第一个字典** —— 于是各链对它们
    既不按受管仓办、也不回落 Virtual Node,而是整店不动(fail-closed)。
    调用方必须把第二个返回值摊到摘要里,否则"这店今天没动"没人看得见。
    """
    from services import stores as stores_mod

    configured = maint_nodes()
    if not configured:
        return {}, {}
    rows = stores_mod.load_stores() if stores is None else stores
    by_name = {s["name"]: s for s in rows}
    ok: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for name in sorted(configured):
        store = by_name.get(name)
        if store is None:
            # 填了「维护仓库」但这店根本调不了 API(未启用/没配代理/没凭证):
            # 不算配置错误,各链本来就不会碰它 —— 但也不能悄悄当"未配置",
            # 否则哪天它恢复了,行为会从"按合计"跳到"按受管仓"而无人知情
            skipped[name] = "不在可调用店铺列表里"
            continue
        try:
            node = resolve_node(store, configured)
        except NodeConfigError as e:
            logger.warning("%s", e)
            skipped[name] = str(e)
            continue
        if node:
            ok[name] = node
    return ok, skipped


def managed_note(ok: dict[str, str], skipped: dict[str, str]) -> str:
    """输入:managed_nodes() 的两个返回值 → 输出:摘要里那一行(空配置返回 "")。"""
    if not ok and not skipped:
        return ""
    parts = [f"{n}={ok[n]}" for n in sorted(ok)]
    line = f"受管仓:{len(ok)} 家店已生效(" + ",".join(parts) + ")"
    if skipped:
        line += (f";⚠ 校验失败整店跳过 {len(skipped)} 家:"
                 + ",".join(sorted(skipped))
                 + "(不回落默认节点,见 services/store_limits.resolve_node)")
    return line


def listing_fc(store: dict, ok: dict[str, str]) -> str:
    """输入:店铺 + managed_nodes() 的已生效字典 → 输出:上架用的 FC ID。

    **上架链取 fulfillmentCenterID 的唯一入口**(MP_ITEM 的
    `Orderable.inventory[].fulfillmentCenterID`)。官方口径:建了自建仓就填
    该仓的 shipNode,没建过才用 Virtual Node(= Partner ID)。

    ⚠ 调用方必须**先把 `skipped` 里的店整店排掉**再调本函数:校验失败的店
    不在 `ok` 里,直接调会回落 Partner ID —— 那就是把本该进新仓的货上到旧
    节点,正是 resolve_node 拼命避免的那件事。
    """
    return ok.get(store["name"]) or settings.get_partner_id(store)
