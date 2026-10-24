"""建仓积木:把人给的信息组成官方 shipNode 元素 + 建仓请求台账(ops.ship_node_requests)。

背景(所有者定稿 2026-10-24,推翻 multi_node_plan §8「不自动建仓」):
「建仓是在我有需要的时候给信息并手动执行创建的」—— 信息由人在命令行给
(`-p` 参数,见 `PARAM_HELP`),执行由人敲 `python cli.py node_create`,本模块
只做两件事:**载荷组装与校验**(纯函数,守门测试拿官方样例校键集合)、
**建仓请求台账**(POST 不幂等,防重状态必须先落库再调接口,见 CLAUDE.md 安全红线)。

为什么不建飞书表:建仓是低频手动动作(一家店一辈子建一两个仓),为它建表、
登记字段常量、配环境变量三道工序,比把地址敲进命令行还多;而且表里一行长期
放着「待建」会诱使哪条链将来去自动消费它 —— 恰是所有者不要的。

防重口径(写操作永不自动兜底):
  · 发之前先 `GET shipnodes` **按仓名查重**:同名已在 ⇒ 判「已建」,不再 POST;
  · 先 INSERT pending 再 POST;200 ⇒ created;4xx ⇒ failed(确认未达);
    网络中断 / 5xx / 200 读不出节点 ⇒ **留 pending**(不知道到没到);
  · 下次再跑同一家店:pending 行先反查 —— 同名已在列表 ⇒ 收编为 created;
    不在且已过 `PENDING_GRACE_MINUTES` ⇒ 落 failed「反查未见」后允许重发;
    不在但还在宽限期内 ⇒ **这家店本轮不动**(人不在环时宁停不重)。
"""

import json
import logging
import re
from datetime import date, datetime, timezone

logger = logging.getLogger("services.ship_nodes")

#: pending 行多久之内不许重发(分钟)。新建节点实证当天即在 shipnodes 在册
#: (multi_node_plan §2.4 ④),十分钟反查不到基本可以判没建成;**唯一出生地**。
PENDING_GRACE_MINUTES = 10

#: `-p` 参数清单(键 → 说明)。**唯一出处**,workflow 头注与报错都引用它。
PARAM_HELP = {
    "name": "仓名(shipNodeName,必填;也是防重键:同店同名不重建)",
    "address1": "地址行(addressLine1,必填;L001 实证一行拼完即可)",
    "address2": "地址行 2(addressLine2,官方 schema 有此字段;缺省不带)",
    "city": "城市(必填)",
    "state": "省/州代码(必填;L001 实证重庆填 CQ)",
    "country": "国家代码(必填;L001 实证国内仓填 CN)",
    "postal": "邮编(必填)",
    "tz": "时区(timeZone,必填,官方枚举见 TIME_ZONES;L001 实证国内仓填 CST)",
    "status": "ACTIVE / INACTIVE(缺省 ACTIVE)",
    "carrier": "承运商(carrierMethodName,如 FEDEX;填了就对该店 GET carriers 校验;"
               "不填 shippingDetails 发空数组 [],2026-10-24 实证该键不能为 null)",
    "carrier_type": "承运方式(carrierMethodType,缺省 GROUND;只在填了 carrier 时用)",
    "services": "distributorSupportedServices,逗号分隔;官方唯一允许值 TWO_DAY_DELIVERY;"
                "**缺省发空数组 []**(2026-10-24 首跑实证:这个键不能为 null)",
    "custom_id": "customNodeId,卖家自定义编号(缺省不带)",
    "workdays": "工作日,逗号或区间:mon,tue,wed 或 mon-fri(与 cutoff 必须同给)",
    "cutoff": "工作日截单时间 HH:MM 24 小时制(与 workdays 必须同给)",
    "days_off": "额外休息日,逗号分隔 YYYY-MM-DD(缺省不带)",
}
REQUIRED = ("name", "address1", "city", "state", "country", "postal", "tz")

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_KEY = {"mon": "monday", "tue": "tuesday", "wed": "wednesday",
            "thu": "thursday", "fri": "friday", "sat": "saturday", "sun": "sunday"}
_CUTOFF_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_STATUSES = ("ACTIVE", "INACTIVE")
#: 官方 create-fulfillment-center schema 的 timeZone 枚举(2026-10-24 核验原文)。
#: 国内仓 L001 实证 CST(沃尔玛建的 VIRTUAL 节点就是它);HKT/CTT 也在表里。
TIME_ZONES = ("PST", "MST", "CST", "EST", "PDT", "MDT", "CDT", "EDT", "ADT", "NDT",
              "GMT", "CET", "TRT", "IST", "CTT", "HKT", "SGT", "ICT", "JST", "KST",
              "VST", "CLT")
#: distributorSupportedServices 官方唯一允许值(schema 原文 "The allowed values: TWO_DAY_DELIVERY")
SUPPORTED_SERVICES = ("TWO_DAY_DELIVERY",)


class SpecError(ValueError):
    """人给的建仓信息不合格(缺项 / 格式错):一个字都不发,报出哪一项。"""


def _clean(v) -> str:
    return str(v if v is not None else "").strip()


def _split(v: str) -> list[str]:
    return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]


def parse_workdays(text: str) -> list[str]:
    """输入:'mon,tue' / 'mon-fri' / 'mon-wed,sat' → 输出:三字母日名列表(去重保序)。"""
    out: list[str] = []
    for part in _split(text.lower()):
        if "-" in part:
            a, _, b = part.partition("-")
            if a not in _DAYS or b not in _DAYS:
                raise SpecError(f"workdays 区间认不出:{part}(可用 {','.join(_DAYS)})")
            i, j = _DAYS.index(a), _DAYS.index(b)
            seq = _DAYS[i:j + 1] if i <= j else _DAYS[i:] + _DAYS[:j + 1]
            out.extend(seq)
        elif part in _DAYS:
            out.append(part)
        else:
            raise SpecError(f"workdays 认不出:{part}(可用 {','.join(_DAYS)},或 mon-fri 这样的区间)")
    return list(dict.fromkeys(out))


def _parse_dates(text: str) -> list[str]:
    out = []
    for s in _split(text):
        try:
            date.fromisoformat(s)
        except ValueError as e:
            raise SpecError(f"days_off 日期格式应为 YYYY-MM-DD:{s}") from e
        out.append(s)
    return out


def build_node(spec: dict) -> dict:
    """输入:`-p` 参数字典(键见 PARAM_HELP)→ 输出:官方 shipNode[] 的一个元素(纯函数)。

    必填缺一项即抛 SpecError;可选项**没给就不带键**(官方样例里有的键我们
    不替所有者编值:编了就是按推断编码)。
    """
    missing = [k for k in REQUIRED if not _clean(spec.get(k))]
    if missing:
        raise SpecError("建仓信息缺必填项:" + ",".join(
            f"-p {k}=<{PARAM_HELP[k].split('(')[0]}>" for k in missing))
    status = (_clean(spec.get("status")) or "ACTIVE").upper()
    if status not in _STATUSES:
        raise SpecError(f"status 只能是 {'/'.join(_STATUSES)},给的是 {status!r}")
    tz = _clean(spec["tz"]).upper()
    if tz not in TIME_ZONES:
        raise SpecError(f"tz 不在官方 timeZone 枚举里:{tz!r}(可用 {','.join(TIME_ZONES)})")
    services = [x.upper() for x in _split(_clean(spec.get("services")))]
    bad = [x for x in services if x not in SUPPORTED_SERVICES]
    if bad:
        raise SpecError(f"services 不在官方允许值里:{bad}(唯一允许 {','.join(SUPPORTED_SERVICES)};"
                        f"不提供就别填,缺省发空数组)")
    address = {"addressLine1": _clean(spec["address1"])}
    if _clean(spec.get("address2")):
        address["addressLine2"] = _clean(spec["address2"])
    address.update({
        "city": _clean(spec["city"]),
        "state": _clean(spec["state"]),
        "country": _clean(spec["country"]),
        "postalCode": _clean(spec["postal"]),
    })
    node: dict = {
        "shipNodeName": _clean(spec["name"]),
        "status": status,
        "timeZone": tz,
        # 2026-10-24 首跑实证(L001贾林红 400):「distributorSupportedServices can't be null」
        # —— 这个键**必须在**,不提供两日达就发空数组,不是不带键
        "distributorSupportedServices": services,
        "postalAddress": address,
    }
    custom_id = _clean(spec.get("custom_id"))
    if custom_id:
        node["customNodeId"] = custom_id
    carrier = _clean(spec.get("carrier"))
    # 2026-10-24 第二次真跑实证(L001贾林红 400):「shippingDetails can't be null」——
    # 与 distributorSupportedServices 同款:键必须在,不声明承运方式就发空数组
    node["shippingDetails"] = []
    if carrier:
        node["shippingDetails"] = [{"twoDayShipping": [{
            "carrierMethodName": carrier.upper(),
            "carrierMethodType": (_clean(spec.get("carrier_type")) or "GROUND").upper(),
        }]}]
    workdays, cutoff = _clean(spec.get("workdays")), _clean(spec.get("cutoff"))
    days_off = _clean(spec.get("days_off"))
    if bool(workdays) != bool(cutoff):
        raise SpecError("workdays 与 cutoff 必须同给:工作日没有截单时间官方不收,"
                        "截单时间没有工作日不知道挂在哪天")
    if workdays or days_off:
        cal: dict = {}
        if workdays:
            if not _CUTOFF_RE.match(cutoff):
                raise SpecError(f"cutoff 应为 HH:MM 24 小时制:{cutoff!r}")
            on = set(parse_workdays(workdays))
            cal["standardProcessingSchedule"] = {
                _DAY_KEY[d]: ({"isWorkingDay": True, "cutOffTime": cutoff}
                              if d in on else {"isWorkingDay": False})
                for d in _DAYS}
        cal["additionalDaysOff"] = _parse_dates(days_off) if days_off else []
        node["calendarDayConfiguration"] = cal
    return node


def _norm_name(s: str) -> str:
    return " ".join(_clean(s).casefold().split())


def find_existing(nodes: dict[str, dict], name: str,
                  custom_id: str | None = None) -> str | None:
    """输入:{shipNode: 节点字段}(list_ship_nodes 的输出)+ 仓名(+自定义编号)→ 输出:同名/同编号已在册的 shipNode,没有 None。

    仓名比对忽略大小写与多余空白(人敲两次不会逐字节一致);customNodeId 给了
    就也认(它是沃尔玛那边唯一能带过去的我方标识)。
    """
    want = _norm_name(name)
    for nid, raw in sorted(nodes.items()):
        raw = raw or {}
        if _norm_name(raw.get("shipNodeName", "")) == want:
            return nid
        if custom_id and _clean(raw.get("customNodeId")) == _clean(custom_id):
            return nid
    return None


# ── 台账(ops.ship_node_requests;唯一读写者)─────────────────────────────────
# ⚠ 每个参数带显式 ::text / ::jsonb(本仓 SQL 纪律:PG 推不出类型在生产上连炸过三次)

_SQL_OPEN = """
INSERT INTO ops.ship_node_requests (store, ship_node_name, payload, status)
VALUES (%(store)s::text, %(name)s::text, %(payload)s::jsonb, 'pending')
RETURNING id
"""
_SQL_SETTLE = """
UPDATE ops.ship_node_requests
   SET status = %(status)s::text, ship_node = %(ship_node)s::text,
       note = %(note)s::text, settled_at = now()
 WHERE id = %(id)s::bigint
"""
_SQL_PENDING = """
SELECT id, created_at FROM ops.ship_node_requests
 WHERE store = %(store)s::text AND ship_node_name = %(name)s::text
   AND status = 'pending'
 ORDER BY id
"""


def open_request(conn, store: str, name: str, node: dict) -> int:
    """输入:连接 + 店铺 + 仓名 + 将发的节点元素 → 输出:台账行 id(status=pending)。"""
    with conn.cursor() as cur:
        cur.execute(_SQL_OPEN, {"store": store, "name": name,
                                "payload": json.dumps(node, ensure_ascii=False)})
        return int(cur.fetchone()[0])


def settle(conn, req_id: int, status: str, ship_node: str | None = None,
           note: str | None = None) -> None:
    """输入:连接 + 台账行 id + 终态(created/failed)(+FC ID / 备注)→ 输出:无。"""
    if status not in ("created", "failed"):
        raise ValueError(f"台账终态只有 created/failed,给的是 {status!r}")
    with conn.cursor() as cur:
        cur.execute(_SQL_SETTLE, {"id": req_id, "status": status,
                                  "ship_node": ship_node, "note": note})


def reconcile_pending(conn, store: str, name: str, nodes: dict[str, dict],
                      now: datetime | None = None) -> tuple[str | None, bool, list[str]]:
    """输入:连接 + 店铺 + 仓名 + 该店当前节点列表 → 输出:(收编到的 FC ID, 是否要按兵不动, 说明行)。

    对该店该仓名的每一条 pending:同名已在列表 ⇒ created 收编;不在且超过宽限期
    ⇒ failed「反查未见」;不在且仍在宽限期 ⇒ 返回 blocked=True(调用方本轮不发)。
    """
    now = now or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(_SQL_PENDING, {"store": store, "name": name})
        rows = cur.fetchall()
    if not rows:
        return None, False, []
    found = find_existing(nodes, name)
    lines: list[str] = []
    blocked = False
    for req_id, created_at in rows:
        if found:
            settle(conn, req_id, "created", found, "反查收编:上次提交结局不明,列表里已有同名节点")
            lines.append(f"    上次提交(台账 #{req_id})结局不明,反查已在册 → 收编为 FC {found}")
            continue
        age_min = (now - created_at).total_seconds() / 60.0
        if age_min >= PENDING_GRACE_MINUTES:
            settle(conn, req_id, "failed", None,
                   f"反查未见:{age_min:.0f} 分钟后 shipnodes 列表仍无同名节点")
            lines.append(f"    上次提交(台账 #{req_id},{age_min:.0f} 分钟前)反查未见 → 判未建成,本轮可重发")
        else:
            blocked = True
            lines.append(f"    ⚠ 上次提交(台账 #{req_id},{age_min:.0f} 分钟前)结局不明且"
                         f"未满 {PENDING_GRACE_MINUTES} 分钟宽限期:本轮不动,稍后重跑反查")
    return found, blocked, lines
