"""node_create — 给店铺账户新建一个自发货物流中心(发货节点 / FC)。**危险:缺省即真跑,空跑用 `--dry-run`。**

用法(信息由人在命令行给;先 dry-run 看载荷与查重结果,再真跑):
  python cli.py node_create -p store=A154杨凯迪 -p name=中山仓 \\
      -p address1=zhongshanshi...17hao -p city=zhongshanshi -p state=GD \\
      -p country=CN -p postal=528400 -p tz=CST --dry-run
  python cli.py node_create -p store=A154杨凯迪 ...(同上去掉 --dry-run)   # 真建
  python cli.py node_create -p store=A154杨凯迪,A155张三 ...               # 多店同一个仓
参数清单的唯一出处 `services/ship_nodes.PARAM_HELP`(缺项时报错会把它打出来)。
值里有空格要加引号:`-p "address1=36 CALIFORNIA ST"`。

**这是全系统唯一的建仓路径**(所有者定稿 2026-10-24,推翻 multi_node_plan §8
「不自动建仓」:「建仓是在我有需要的时候给信息并手动执行创建的」)。它永不进调度:
什么时候建、建在哪、叫什么,全部由人当场给;代码只负责把接口调对、不重建、留账。

**建完之后两件事仍是人工,摘要会提醒**(multi_node_plan §7 runbook):
  ① Seller Center 配运费模板并挂到新仓 → **等 4 小时**;
  ② 要让维护链/上架链用它,再去限额表「维护仓库」填这个 FC ID(本工作流**不替你填**:
     填了就是整店切仓,后果是旧仓双节点有货、要跑 node_clear —— 独立决策)。

防重(POST 不幂等,重发 = 重名建两个仓;口径全在 `services/ship_nodes` 头注):
  发前 `GET shipnodes` 按仓名查重,同名已在 ⇒ 判「已存在」不发;先落台账 pending 再 POST;
  结局不明(网络中断 / 5xx)留 pending,**下次重跑同一家店先反查收编**,宽限期内不动。
**承运商若填了**(`-p carrier=`)先对该店 `GET carriers` 校验,不认识整店跳过不发
(官方说至少要一种 shipping method;不填就不带 shippingDetails,被拒会原文报出来,
 再按报错补 `-p carrier=`)。

失败处理按店维标准(conventions §四):跨店并发 → 凭证死不补 → 其余串行补试一遍
(补试跑的是同一个函数,它先查重后发,不会重复建)→ 仍失败按归类词点名。
⚠ 多店共用同一地址与多店共用代理同性质:是账户关联信号。摘要点名,**不拦**
(2026-10-21 口径:共享出口的关联风险由所有者承担,代码只负责看得见)。
"""

import json
import logging

from api import settings
from registry import db
from services import ship_nodes, store_events as se, store_retry, stores as stores_svc

DANGEROUS = True

logger = logging.getLogger("workflows.node_create")

_SOURCE = "node_create"

#: 结局词(摘要分桶用;与台账 status 不是一回事:台账只有 pending/created/failed)
_OUTCOMES = ("created", "exists", "blocked", "skipped", "rejected", "unknown", "preview")


def _usage() -> str:
    return "参数(-p k=v):\n" + "\n".join(
        f"  {k:<13}{v}" for k, v in ship_nodes.PARAM_HELP.items())


def _carrier_ok(carriers: list[dict], name: str, ctype: str) -> bool:
    return any(str(c.get("carrierMethodName", "")).upper() == name
               and str(c.get("carrierMethodType", "")).upper() == ctype
               for c in carriers)


def _create_one(store: dict, node: dict, preview: bool) -> dict:
    """输入:店铺 + 节点元素 + 是否空跑 → 输出:{name, outcome, ship_node, lines}。

    读节点列表 / 读库失败直接抛,交 fan_out 按店维标准处理(补试跑的还是本函数,
    它先查重、先反查 pending,再决定发不发 —— 不会重复建)。
    """
    name = store["name"]
    node_name = node["shipNodeName"]
    res = {"name": name, "outcome": "", "ship_node": None, "lines": []}
    lines = res["lines"]

    nodes = settings.list_ship_nodes(store)
    with db.pg_conn() as conn:
        found, blocked, rlines = ship_nodes.reconcile_pending(conn, name, node_name, nodes)
    lines.extend(rlines)
    existing = found or ship_nodes.find_existing(nodes, node_name, node.get("customNodeId"))
    if existing:
        res.update(outcome="exists", ship_node=existing)
        lines.append(f"  {name}:同名节点已在册 FC {existing},不重建"
                     f"(在册 {len(nodes)} 个节点)")
        return res
    if blocked:
        res["outcome"] = "blocked"
        lines.append(f"  {name}:上次提交结局不明,本轮不动(见上)")
        return res

    ship = (node.get("shippingDetails") or [{}])[0].get("twoDayShipping") or []
    if ship:
        carriers = settings.list_carrier_methods(store)
        cname, ctype = ship[0]["carrierMethodName"], ship[0]["carrierMethodType"]
        if not _carrier_ok(carriers, cname, ctype):
            avail = ",".join(sorted({f"{c.get('carrierMethodName')}/{c.get('carrierMethodType')}"
                                     for c in carriers}))
            res["outcome"] = "skipped"
            lines.append(f"  {name}:⚠ 承运商 {cname}/{ctype} 不在该店可用列表,不发"
                         f"(可用:{avail or '(空)'})")
            return res

    if preview:
        res["outcome"] = "preview"
        lines.append(f"  {name}:将新建「{node_name}」(在册 {len(nodes)} 个节点,无同名)")
        if not ship:
            # 没填承运商时顺带把该店可用的承运方法打出来(只读,10/min):2026-10-24
            # 第三次真跑实证 shippingDetails 发 [] 让沃尔玛校验器越界(520 HV000028),
            # 多半绕不开填一个 —— 人要先知道这店能填什么
            try:
                carriers = settings.list_carrier_methods(store)
                avail = ",".join(sorted({f"{c.get('carrierMethodName')}/{c.get('carrierMethodType')}"
                                         for c in carriers}))
                lines.append(f"    该店可用承运方法(-p carrier=<名> -p carrier_type=<类型>):"
                             f"{avail or '(空)'}")
            except Exception as e:                   # noqa: BLE001 —— 预览里的参考信息,读不到只说一句
                lines.append(f"    承运方法列表读不到:{e}")
        return res

    with db.pg_conn() as conn:
        req_id = ship_nodes.open_request(conn, name, node_name, node)
    try:
        created = settings.create_ship_node(store, node)
    except settings.ShipNodeOutcomeUnknown as e:
        # 不知道到没到:台账留 pending,绝不重发;下次重跑先反查
        res["outcome"] = "unknown"
        lines.append(f"  {name}:⚠ 结局不明(台账 #{req_id} 留 pending):{e}"
                     f" —— {ship_nodes.PENDING_GRACE_MINUTES} 分钟后重跑本命令反查收编")
        logger.warning("建仓结局不明 %s #%d: %s", name, req_id, e)
        return res
    except RuntimeError as e:
        with db.pg_conn() as conn:
            ship_nodes.settle(conn, req_id, "failed", None, str(e)[:500])
        res["outcome"] = "rejected"
        lines.append(f"  {name}:✗ 沃尔玛拒了(台账 #{req_id}):{e}")
        logger.warning("建仓被拒 %s #%d: %s", name, req_id, e)
        return res
    fc = str(created.get("shipNode"))
    with db.pg_conn() as conn:
        ship_nodes.settle(conn, req_id, "created", fc, f"nodeType={created.get('nodeType')}")
        se.record_many(conn, [{
            "store": name, "event": se.SHIP_NODE_CREATED, "severity": "info",
            "source": _SOURCE,
            "detail": {"ship_node": fc, "name": node_name,
                       "city": node["postalAddress"].get("city"),
                       "country": node["postalAddress"].get("country"),
                       "request_id": req_id}}])
    res.update(outcome="created", ship_node=fc)
    lines.append(f"  {name}:✓ 新建「{node_name}」FC {fc}"
                 f"(nodeType={created.get('nodeType')},台账 #{req_id})")
    return res


def run(params: dict) -> str:
    """输入:params(store 必填,可逗号分隔多店;建仓信息见 PARAM_HELP)→ 输出:建仓摘要。"""
    names = [s.strip() for s in str(params.get("store") or "").split(",") if s.strip()]
    if not names:
        return "⚠ 缺参数:-p store=<店铺名>(可逗号分隔多店)\n" + _usage()
    try:
        node = ship_nodes.build_node(params)
    except ship_nodes.SpecError as e:
        return f"⚠ {e}\n" + _usage()
    preview = bool(params.get("dry_run"))
    mode = "[DRY-RUN] " if preview else ""
    node_name = node["shipNodeName"]

    store_list = stores_svc.load_stores(filter_names=names)   # 名字落空就抛,不静默少跑

    def _one(store: dict) -> dict:
        return _create_one(store, node, preview)

    results, dead, absent, gate_note = store_retry.fan_out(
        store_list, _one, stores_svc.STORE_WORKERS, log_label="建仓")
    results.sort(key=lambda r: stores_svc.sort_key(r["name"]))
    n = {k: sum(1 for r in results if r["outcome"] == k) for k in _OUTCOMES}
    missed = [f"{s}(凭证失效)" for s in sorted(dead)] + \
        [f"{s}({w})" for s, w in sorted(absent)]

    parts = []
    if preview:
        parts.append(f"将新建 {n['preview']} 家")
    else:
        parts.append(f"新建 {n['created']} 家")
    for key, word in (("exists", "已存在"), ("skipped", "承运商不认跳过"),
                      ("blocked", "结局不明按兵不动"), ("rejected", "⚠ 被拒"),
                      ("unknown", "⚠ 结局不明")):
        if n[key]:
            parts.append(f"{word} {n[key]}")
    head = (f"{mode}建仓「{node_name}」:{len(store_list)} 店 → " + ",".join(parts)
            + (f";⚠ 失败 {len(missed)} 店:{','.join(missed)}" if missed else "")
            + ("(dry-run:一个字都没发)" if preview else ""))
    lines = [head]
    if gate_note:
        lines.append(f"  {gate_note}")
    if len(store_list) > 1:
        lines.append(f"  ⚠ {len(store_list)} 店共用同一仓库地址 —— 账户关联信号,"
                     f"与共用代理同性质(所有者知情承担,代码只点名不拦)")
    for r in results:
        lines.extend(r["lines"])
    lines.append("  载荷(shipNode[] 元素):" + json.dumps(node, ensure_ascii=False))
    if n["created"]:
        lines.append("  下一步(人工,顺序是硬约束):① Seller Center 配运费模板挂到新仓 → 等 4 小时;"
                     "② 要让维护/上架链用它,再到限额表「维护仓库」填上面的 FC ID"
                     "(本工作流不替你填;切仓后按 multi_node_plan §7 runbook 走)")
    return "\n".join(lines)
