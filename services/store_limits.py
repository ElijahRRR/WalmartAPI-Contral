"""上下架限额表(registry.RETIRE_LIMITS)的按店读取积木。

三个 workflow(list_new / maintenance / product_clear)各自写过一份"拉这张表、
按店铺列建字典"的循环。本模块把那段收成一处 —— 新增一列时改一个地方,
而不是三处各改一遍(改漏的那处会静默读空,全店回落默认值且不报错)。

⚠ 「读不到」与「填了 0」必须分开:
  - 表未登记 / 该店没这一行 / 单元格为空 ⇒ **返回默认值**(不知道 ≠ 限死)
  - 单元格填了 0 或负数 ⇒ 视同没填(限额为 0 = 这条链整店停摆,不像人的本意;
    真要停某店有 `店铺状态` 与 stockzero 两条显式路径)
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from api import feishu, settings
from registry import db, resources

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


def setup_limits() -> dict[str, int]:
    """输入:无 → 输出:{店铺: 沃尔玛 item setup limit}(限额表「商品上限」列)。

    未填/读不到的店**不在字典里**,调用方回落
    `registry.resources.WALMART_ITEM_SETUP_LIMIT_DEFAULT`(=5000,出处见那处注释:
    沃尔玛 EXT_DATA_ERROR_50575703577001 原文 + 2026-09-07 A131吕灿荣 实证)——
    与「配送时长限制」同款治理:人在飞书填、程序直读、没填就是缺省。

    ⚠ 这不是我们自己定的经营容量(那是「单店最大在线数」`max_online`,分配引擎读),
    而是**沃尔玛的硬限**:店内现有 item 数 + 本 feed 条数 超了它,整个 feed 被拒收
    (itemsReceived=0、零逐条明细),一条也进不去。
    """
    caps = _int_map(resources.RETIRE_LIMITS.fields.item_setup_limit)
    if not caps:
        logger.info("限额表「商品上限」读到 0 店,全店回落缺省 item setup limit"
                    "(表未登记/该列未建/该列为空都会走到这里)")
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
    """「维护仓库」这店本轮判不出受管仓 —— 整店跳过的信号(fail-closed)。

    两个子类分开"填错"与"读不到":前者是配置错,修表才能好;后者是瞬时故障,
    可补试。2026-09-19 前混成一个,摘要只能说「校验失败」,人分不清该改表
    还是该等 —— 而实际 15 次失败全是代理/token 那一跳(见文件尾注)。
    """


class NodeUnknownError(NodeConfigError):
    """填的 FC ID 沃尔玛**明确**不认识(200 且列表非空且不含它)—— 配置错。"""


class NodeUnreachableError(NodeConfigError):
    """节点列表**读不到**(代理/token/网络/5xx)且没有可沿用的记忆 —— 瞬时故障。"""


#: 受管仓校验记忆的保鲜期(小时)。多仓 §3 所有者原句「校验结果缓存一天
#: (节点不会天天变)」的落地(2026-09-19;此前只有进程内 lru,每天每进程
#: 从零校验)。期内不调沃尔玛。**全项目唯一出生地。**
NODE_VALIDATION_TTL_HOURS = 24
#: 接口读不到时可沿用旧记忆的上限(天)。超过它仍读不到 → 该店整店跳过。
#: 一家店 API 整月不通,别的链早就天天喊了,这里不必再独自撑着。
NODE_VALIDATION_MAX_AGE_DAYS = 30

_SQL_VALIDATION_GET = """
SELECT validated_at FROM ops.node_validations WHERE store = %s AND node = %s
"""
_SQL_VALIDATION_PUT = """
INSERT INTO ops.node_validations (store, node, validated_at, known_nodes)
VALUES (%s, %s, now(), %s::jsonb)
ON CONFLICT (store, node) DO UPDATE
   SET validated_at = EXCLUDED.validated_at, known_nodes = EXCLUDED.known_nodes
"""
_SQL_VALIDATION_DEL = """
DELETE FROM ops.node_validations WHERE store = %s AND node = %s
"""


def _validation_age(conn, name: str, node: str) -> timedelta | None:
    """输入:连接 + (店, FC ID) → 输出:距最近一次认过的时长;没认过返回 None。"""
    with conn.cursor() as cur:
        cur.execute(_SQL_VALIDATION_GET, (name, node))
        row = cur.fetchone()
    if not row or not row[0]:
        return None
    return datetime.now(timezone.utc) - row[0]


def _resolve(store: dict, nodes: dict[str, str], conn) -> tuple[str | None, str]:
    """输入:店铺 + 配置表 + 连接 → 输出:(受管仓 FC ID 或 None, 判定来源)。

    **各链取受管仓的唯一入口**(上架/维护/清零都走它,别各写一遍);对外的
    薄壳是 resolve_node()。判定来源四档,managed_nodes 按它计数进摘要
    (conventions §六 三要件之"兜底触发记日志计数"):
      unconfigured  没填 → None,不开连接、不调沃尔玛(现状零成本零变化)
      memory        记忆在保鲜期内 → 不调沃尔玛
      fresh         调了 shipnodes 且认识 → 记忆刷新
      memory_stale  调了 shipnodes 读不到,沿用过期记忆(≤ MAX_AGE)—— 这是兜底

    校验是 fail-closed 的:填了值就必须被 `GET shipnodes` 认过一次,列表
    **明确**不含它 → 抹掉记忆并抛 NodeUnknownError(配置错);读不到且没有
    可沿用的记忆 → 抛 NodeUnreachableError(瞬时,可补试)。两者调用方都
    整店跳过并告警。
    ⚠ 为什么不"认不出就回落 Virtual Node":那等于把本该进新仓的货写到旧节点,
    而且全程不报错 —— 比"这店今天没动"坏得多。填错一个字符的代价必须是
    响亮失败,不是静默走偏。
    ⚠ 为什么"读不到"可以沿用记忆(2026-09-19,所有者追问根因后定):校验
    的对象是**配置值**,值没变、昨天沃尔玛刚认过,今天代理抖一下不构成
    "不认识";此前把读不到当不认识,一次零重试的远程读就改判整店路由
    (2026-09-17 三家店 SSL EOF、09-18 代理账号错,库存全写到了默认节点)。
    记忆的失效只认沃尔玛的明确否定,不认沉默。
    """
    name = store["name"]
    node = (nodes or {}).get(name)
    if not node:
        return None, "unconfigured"
    age = _validation_age(conn, name, node)
    if age is not None and age < timedelta(hours=NODE_VALIDATION_TTL_HOURS):
        return node, "memory"
    try:
        known = settings.list_ship_nodes(store)
    except Exception as e:                          # noqa: BLE001
        if age is not None and age < timedelta(days=NODE_VALIDATION_MAX_AGE_DAYS):
            logger.warning("%s:发货节点列表读不到(%s: %s),沿用 %.0f 小时前的"
                           "校验记忆", name, e.__class__.__name__, e,
                           age.total_seconds() / 3600)
            return node, "memory_stale"
        raise NodeUnreachableError(
            f"{name}:「维护仓库」填了 {node},但发货节点列表读不到({e})"
            + (",且没有可沿用的校验记忆" if age is None
               else f",记忆已超 {NODE_VALIDATION_MAX_AGE_DAYS} 天")
            + "—— 本轮整店跳过,不回落 Virtual Node") from e
    if node not in known:
        with conn.cursor() as cur:
            cur.execute(_SQL_VALIDATION_DEL, (name, node))
        raise NodeUnknownError(
            f"{name}:「维护仓库」填的 {node} 不在该店发货节点列表里"
            f"(认识的:{sorted(known) or '(空)'})—— 本轮整店跳过。"
            f"FC ID 见 Seller Center → Shipping Profile → Seller Fulfillment")
    with conn.cursor() as cur:
        cur.execute(_SQL_VALIDATION_PUT, (name, node, json.dumps(sorted(known))))
    return node, "fresh"


def resolve_node(store: dict, nodes: dict[str, str], conn=None) -> str | None:
    """输入:店铺 + maint_nodes() 的字典(+连接)→ 输出:受管节点 FC ID;未配置 None。

    判据全在 _resolve(见其头注);这里只管连接:没给就自开(未配置的店
    连连接都不开)。
    """
    if conn is not None:
        return _resolve(store, nodes, conn)[0]
    if not (nodes or {}).get(store["name"]):
        return None
    with db.pg_conn() as c:
        return _resolve(store, nodes, c)[0]


def _cause(e: Exception) -> Exception:
    """输入:异常 → 输出:NodeConfigError 包着的原始异常(归类/补试判据看的是它)。"""
    if isinstance(e, NodeConfigError) and e.__cause__ is not None:
        return e.__cause__
    return e


def managed_nodes(stores: list[dict] | None = None, conn=None,
                  stats: dict | None = None
                  ) -> tuple[dict[str, str], dict[str, str]]:
    """输入:店铺列表(None=按需自取)(+连接,+stats 出参)→ 输出:({店铺: 已校验的受管仓}, {跳过的店: 原因})。

    **各链拿受管仓表的唯一入口**(维护链/上架链/清零共用)。把「读表」与
    「逐店校验」合成一次调用,是因为两件事必须成对发生:只读不校验 =
    填错一个字符就静默写到别的节点;只校验不汇总 = 摘要报不出"今天几家店
    生效、几家被跳过"(计划 §6 第 6 条要求配置生效与否天天见人)。

    跳过的店(NodeConfigError)**不进第一个字典** —— 于是各链对它们
    既不按受管仓办、也不回落 Virtual Node,而是整店不动(fail-closed)。
    调用方必须把第二个返回值摊到摘要里,否则"这店今天没动"没人看得见。

    「读不到」的店(NodeUnreachableError)按店维失败标准①**串行补试一遍**
    (services/store_retry 唯一实现;凭证死不补、规模闸都在里面)—— 此前
    managed_nodes 是全仓唯一不走这套标准的按店远程调用,一次抖动就整店改判。

    `stats`(出参,调用方给个空 dict 就会被填):memory/fresh/memory_stale/
    retried 四个计数、gate_note(补试规模闸说明)、words({跳过店: 归类词},
    瞬时故障用 store_retry.diagnose 六档,填错记「FC ID 不在列表」)。
    managed_note 把它摊成摘要那一行 —— 兜底(memory_stale)触发必须见人。
    """
    from services import stores as stores_mod

    configured = maint_nodes()
    if not configured:
        return {}, {}
    rows = stores_mod.load_stores() if stores is None else stores
    by_name = {s["name"]: s for s in rows}
    fleet = stores is None
    if conn is not None:
        return _managed(configured, by_name, conn, stats, fleet)
    with db.pg_conn() as c:
        return _managed(configured, by_name, c, stats, fleet)


def _managed(configured: dict[str, str], by_name: dict[str, dict], conn,
             stats: dict | None, fleet: bool
             ) -> tuple[dict[str, str], dict[str, str]]:
    from services import store_retry

    ok: dict[str, str] = {}
    skipped: dict[str, str] = {}
    st = {"memory": 0, "fresh": 0, "memory_stale": 0, "retried": 0,
          "gate_note": "", "words": {}}
    failures: list[tuple[dict, Exception]] = []    # (店, 原始异常) 给补试
    wrapped: dict[str, str] = {}                    # 店 → 首轮 NodeUnreachableError 全文
    for name in sorted(configured):
        store = by_name.get(name)
        if store is None:
            # 填了「维护仓库」但这店根本调不了 API(未启用/没配代理/没凭证):
            # 不算配置错误,各链本来就不会碰它 —— 但也不能悄悄当"未配置",
            # 否则哪天它恢复了,行为会从"按合计"跳到"按受管仓"而无人知情。
            # 全船队模式下要记日志:2026-09-06 谭总22/23/24 就是走的这条无日志
            # 分支(那一刻凭证表里没读到它们),事后只能靠摘要里三个店名猜
            skipped[name] = "不在可调用店铺列表里"
            st["words"][name] = "不可调用"
            if fleet:
                logger.warning("%s:填了「维护仓库」但不在可调用店铺列表里"
                               "(凭证表未启用/缺代理/读表落到快照?)", name)
            continue
        try:
            node, how = _resolve(store, configured, conn)
        except NodeUnknownError as e:
            logger.warning("%s", e)
            skipped[name] = str(e)
            st["words"][name] = "FC ID 不在列表"
            continue
        except NodeUnreachableError as e:
            logger.warning("%s", e)
            wrapped[name] = str(e)
            failures.append((store, _cause(e)))
            continue
        st[how] += 1
        ok[name] = node
    if failures:
        saved, still, gate = store_retry.serial_second_pass(
            failures, lambda s: _resolve(s, configured, conn),
            total_stores=len(configured))
        st["retried"] = len(failures)
        st["gate_note"] = gate
        for store, (node, how) in saved:
            st[how] += 1
            ok[store["name"]] = node
        for store, e in still:
            name = store["name"]
            # 规模闸拦下时 e 是首轮原始异常(没有「维护仓库」上下文),补试
            # 失败时 e 是第二轮的 NodeConfigError —— 两种都要说清楚是哪家店
            skipped[name] = str(e) if isinstance(e, NodeConfigError) else wrapped[name]
            st["words"][name] = ("FC ID 不在列表" if isinstance(e, NodeUnknownError)
                                 else store_retry.diagnose(_cause(e)))
    if stats is not None:
        stats.update(st)
    return ok, skipped


def managed_note(ok: dict[str, str], skipped: dict[str, str],
                 stats: dict | None = None) -> str:
    """输入:managed_nodes() 的两个返回值(+stats)→ 输出:摘要里那一行(空配置返回 "")。"""
    if not ok and not skipped:
        return ""
    st = stats or {}
    words = st.get("words") or {}
    parts = [f"{n}={ok[n]}" for n in sorted(ok)]
    line = f"受管仓:{len(ok)} 家店已生效(" + ",".join(parts) + ")"
    if st:
        # 判定来源要天天见人:兜底(接口读不到沿用旧记忆)静默常态化 = 主路径
        # 已坏没人知道(conventions §六)
        line += (f";记忆沿用 {st.get('memory', 0)} 家,接口校验 {st.get('fresh', 0)} 家"
                 + (f",⚠ 接口读不到沿用旧记忆 {st['memory_stale']} 家"
                    if st.get("memory_stale") else "")
                 + (f",补试 {st['retried']} 家" if st.get("retried") else ""))
    if skipped:
        line += (f";⚠ 校验失败整店跳过 {len(skipped)} 家:"
                 + ",".join(f"{n}({words[n]})" if words.get(n) else n
                            for n in sorted(skipped))
                 + "(不回落默认节点,见 services/store_limits.resolve_node)")
    if st.get("gate_note"):
        line += ";" + st["gate_note"]
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
