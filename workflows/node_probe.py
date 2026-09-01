"""多仓实测探针:跑 docs/multi_node_plan.md §2.4 的四条官方文档空白验证。

用法(建好第二个仓之后,对该店跑一次):
  python cli.py node_probe -p store=A154杨凯迪
  python cli.py node_probe -p store=A154杨凯迪 -p sku=B0XXXXXXX   # 指定探测 SKU

**纯只读**:只调 GET 端点,不写沃尔玛、不写库、不写飞书。输出直接贴回给 AI
核对,结论回填 docs/multi_node_plan.md §2.4。每新开一个仓的店都值得重跑一次
(不同店铺的响应形状官方不保证一致,而形状正是这四条要验的东西)。

四条各验什么、为什么非实测不可:
  ① Virtual Node 在不在 GET shipnodes 列表里 —— 官方页面自述只覆盖
     physical/3PL,决定"未配置店"的校验口径能不能收紧。
  ② GET /v3/inventories/{sku} 的真实响应结构 —— 官方样例是 PUT 风格的
     status:"Success",几乎肯定是文档错误;api/inventory._nodes 认不出会
     返回 None(告警不误判),但要知道真形状才能确认解析分支走对。
  ③ 多自发货仓时订单行带不带 shipNode —— 官方仅 3PL 样例带 id,决定
     按仓对账靠订单接口还是靠自建 SKU→FC 映射。
  ④ 新建 PHYSICAL 节点多久出现在 GET /v3/inventories —— 决定"配了仓但
     明细没扫到"的跳过状态要持续多久算正常。
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from api import _client, inventory as inv_api, orders, settings
from services import store_limits, stores as stores_svc

DANGEROUS = False       # 只调沃尔玛 GET;不写任何东西

logger = logging.getLogger("workflows.node_probe")

_RAW_LIMIT = 1500       # 原始 JSON 片段截断长度(够核对形状,不刷屏)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)[:_RAW_LIMIT]


def _find_ship_nodes(obj, found: list, path: str = "") -> None:
    """递归收集 JSON 里所有名为 shipNode/shipNodes 的键(形状未知,按键名扫)。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if k in ("shipNode", "shipNodes", "shipNodeType"):
                found.append((p, v))
            _find_ship_nodes(v, found, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _find_ship_nodes(v, found, f"{path}[{i}]")


def run(params: dict) -> str:
    """输入:params(store 必填,sku 选填)→ 输出:四条实测的原始证据摘要。"""
    name = str(params.get("store") or "").strip()
    if not name:
        return "⚠ 缺参数:-p store=<店铺名>(对哪家店探测)"
    matched = stores_svc.load_stores(filter_names=[name])
    if not matched:
        return f"⚠ 店铺 {name} 不在可调用列表里(未启用/没配代理/没凭证?)"
    store = matched[0]
    lines = [f"多仓实测探针 · {name}(全部只读)"]

    # ── ① shipnodes 列表 vs Partner ID ────────────────────────────────────
    partner_id = settings.get_partner_id(store)
    nodes = settings.list_ship_nodes(store)
    lines.append(f"① GET shipnodes:{len(nodes)} 个节点;Partner ID = {partner_id}")
    for nid, raw in sorted(nodes.items()):
        lines.append(f"   {nid}: {_dump(raw)}")
    lines.append(f"   → Virtual Node(=Partner ID)"
                 f"{'**在**' if partner_id in nodes else '**不在**'}列表里")
    configured = store_limits.maint_nodes().get(name)
    if configured:
        lines.append(f"   「维护仓库」已填 {configured}:"
                     + ("✓ 在列表里,校验会通过" if configured in nodes
                        else "✗ **不在列表里,校验会整店跳过** —— 先核对 FC ID"))
    else:
        lines.append("   「维护仓库」未填(该店现状 = Virtual Node)")

    # ── ② 单品端点真实响应形状 ────────────────────────────────────────────
    sku = str(params.get("sku") or "").strip()
    bulk = inv_api.list_inventory_nodes(store)
    if not sku:
        sku = next(iter(sorted(bulk)), "")
    if sku:
        _client.rate_acquire("inventory.get", store["client_id"])
        token = _client.get_token(store["client_id"], store["client_secret"],
                                  store["proxy"])
        status, _, data = _client.safe_get_ex(
            f"{_client.base_url()}/v3/inventories/{sku}",
            token, store["client_id"], store["proxy"], max_retries=3)
        lines.append(f"② GET /v3/inventories/{sku} → HTTP {status}")
        lines.append(f"   原始响应:{_dump(data)}")
        parsed = inv_api._nodes(data or {}, f"{name} {sku} 探针")
        lines.append(f"   _nodes() 解析结果:{parsed}"
                     + ("(⚠ None = 形状认不出,把上面的原始响应贴回来)"
                        if parsed is None else ""))
    else:
        lines.append("② 跳过:bulk 里一个 SKU 都没有,也没传 -p sku=")

    # ── ④ bulk 里的节点分布(顺带回答"新节点出现了没")──────────────────
    dist: dict[str, int] = {}
    for nd in bulk.values():
        for node_id in nd:
            dist[node_id] = dist.get(node_id, 0) + 1
    lines.append(f"④ GET /v3/inventories 全店 {len(bulk)} SKU,节点分布:")
    for node_id, cnt in sorted(dist.items(), key=lambda kv: -kv[1]):
        tag = ""
        if node_id == partner_id:
            tag = "(= Partner ID / Virtual Node)"
        elif node_id in nodes:
            tag = f"(shipnodes 在册:{(nodes[node_id] or {}).get('shipNodeName', '')})"
        lines.append(f"   {node_id or '(空=响应未带节点身份)'}: {cnt} SKU {tag}")
    new_nodes = [nid for nid in nodes if nid not in dist]
    if new_nodes:
        lines.append(f"   → shipnodes 在册但库存响应里**还没出现**的节点:{new_nodes}"
                     f"(刚建仓属正常;持续多天不出现才是问题)")

    # ── ③ 订单行带不带 shipNode ──────────────────────────────────────────
    start = (datetime.now(timezone.utc) - timedelta(days=14)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    n_orders, hits, sample = 0, [], ""
    for order in orders.iter_orders(store, created_start=start):
        n_orders += 1
        found: list = []
        _find_ship_nodes(order, found)
        if found:
            hits.extend(found)
            if not sample:
                sample = _dump(found[:5])
        if n_orders >= 50:      # 形状问题 50 单足够回答,不翻全量
            break
    lines.append(f"③ 近 14 天订单扫了 {n_orders} 单(上限 50):"
                 + (f"**{len(hits)} 处出现 shipNode 键**,样例:{sample}"
                    if hits else "**没有任何 shipNode 键**"
                    "(单/双仓订单都不带 → 按仓对账只能靠自建 SKU→FC 映射)"))

    lines.append("把以上全文贴回给 AI,核对后回填 docs/multi_node_plan.md §2.4。")
    return "\n".join(lines)
