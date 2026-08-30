"""把**指定发货节点**的库存整节点清零(一次性搬仓收尾,不进调度)。

用途只有一个(所有者定稿 2026-08-30,谭总12 搬仓):切到受管仓之后,旧节点
(通常是 Virtual Node)上的存量货**自动链一律不碰** —— 它会继续被沃尔玛算进
可售、继续出单。等受管仓的库存充起来之后,用本工作流把旧节点清空,可售量
才回到"只由受管仓说了算"。

  python cli.py node_clear -p store=谭总12 -p node=10003247367 --dry-run
  python cli.py node_clear -p store=谭总12 -p node=10003247367            # 真跑

⚠ **拒绝清受管仓**:那是自动链正在维护的节点,清了它下一轮维护链又写回来
(两条规则打架,而且没人看得出是谁在跟谁较劲)。要停售整店走 stockzero。

为什么走单品 PUT 而不是 MP_INVENTORY feed:这是**一次性破坏动作**,逐条的
成败当场就要知道(feed 要等回执、失败混在批里)。3600 条按 160/min 的桶
约 23 分钟,可以接受。写 0 是幂等的,中断了重跑一遍即可,不需要防重台账。
"""

import logging

from api import inventory as inv_api
from services import store_limits, stores as stores_svc

DANGEROUS = True
SUPPORTS_STORE = True

logger = logging.getLogger("workflows.node_clear")

_DEFAULT_LIMIT = 5000       # 单轮上限:防手滑对超大店整店重写


def run(params: dict) -> str:
    """输入:params(store/node/limit)→ 输出:清零结果摘要。"""
    name = str(params.get("store") or "").strip()
    node = str(params.get("node") or "").strip()
    if not name or not node:
        return ("⚠ 缺参数:-p store=<店铺名> -p node=<要清零的 FC ID>\n"
                "   (受管仓 FC ID 在限额表「维护仓库」列;这里要填的是**旧节点**)")
    preview = bool(params.get("dry_run"))
    limit = int(params.get("limit", _DEFAULT_LIMIT))

    matched = stores_svc.load_stores(filter_names=[name])
    if not matched:
        return f"⚠ 店铺 {name} 不在可调用列表里(未启用/没配代理/没凭证?)"
    store = matched[0]

    # ⚠ 拒绝清受管仓:清了下一轮维护链就写回来,两条规则互相拆台
    managed = store_limits.maint_nodes().get(name)
    if managed and str(managed) == node:
        return (f"⚠ 拒绝执行:{node} 正是 {name} 的**受管仓**(限额表「维护仓库」)。\n"
                f"   自动链每轮都在维护它,清了下一轮就写回来。\n"
                f"   要停售整店走 stockzero(限额表「库存特殊要求」=0);"
                f"要换仓先改「维护仓库」再清旧仓。")

    nodes = inv_api.list_inventory_nodes(store)
    targets = {sku: nd[node] for sku, nd in nodes.items()
               if nd.get(node, 0) > 0}
    total_qty = sum(targets.values())
    lines = [f"节点清零 · {name} · 节点 {node}"
             f"(受管仓={managed or '(未配置)'})",
             f"  全店 {len(nodes)} SKU,该节点上有货的 {len(targets)} 个,"
             f"合计 {total_qty} 件"]
    if not targets:
        return "\n".join(lines + ["  该节点已经是空的,无事可做"])

    order = sorted(targets.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(order) > limit:
        lines.append(f"  ⚠ 超单轮上限 {limit},本轮只清前 {limit} 个"
                     f"(按数量降序,其余下轮;-p limit= 可调)")
        order = order[:limit]
    lines.append(f"  数量最多的 5 个:{order[:5]}")

    if preview:
        return "\n".join(lines + [
            f"(dry-run:一件都没写;真跑将把 {len(order)} 个 SKU 在该节点清零)"])

    n_ok = 0
    fails: list[tuple[str, str]] = []
    for sku, qty in order:
        try:
            ok, why = inv_api.put_inventory(store, sku, 0, node)
        except Exception as e:                          # noqa: BLE001
            ok, why = False, f"{type(e).__name__}: {e}"
        if ok:
            n_ok += 1
        else:
            fails.append((sku, why))
            logger.warning("%s %s 节点 %s 清零失败:%s", name, sku, node, why)
    lines.append(f"  清零成功 {n_ok}/{len(order)}")
    if fails:
        # 失败必须点名:写 0 是幂等的,重跑本工作流即可补上;静默的话
        # 那批货还在旧节点上继续卖,而摘要看起来一切正常
        lines.append(f"  ⚠ 失败 {len(fails)} 个(写 0 幂等,重跑本工作流即补):"
                     + ",".join(s for s, _ in fails[:10])
                     + (" …" if len(fails) > 10 else ""))
        lines.append(f"    首个失败原因:{fails[0][1][:200]}")
    return "\n".join(lines)
