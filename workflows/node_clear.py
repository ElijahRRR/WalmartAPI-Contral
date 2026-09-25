"""把**受管仓以外**的发货节点库存清零(一次性搬仓收尾,不进调度)。

用途(所有者定稿 2026-08-30,谭总12 搬仓):切到受管仓之后,旧节点(通常是
Virtual Node)上的存量货**自动链一律不碰** —— 它会继续被沃尔玛算进可售、
继续出单。等受管仓的库存充起来之后,用本工作流把旧节点清空,可售量才回到
"只由受管仓说了算"。设了「最大库存」的店尤其要清:维护链只往受管仓写 N,
旧仓的存量还在,线上可售 = 两者之和,上限就被绕过去了。

**旧仓编号不用人查**(所有者定稿 2026-09-25:「api 应该可以拿到仓库编号,
如果有设置维护仓的,运行脚本时直接把非维护仓的库存清零」):
`GET /v3/inventories` 逐 SKU 返回各节点(`nodes[].shipNode`),凡不是受管仓
的节点、有货就清。

  python cli.py node_clear --dry-run                  # 所有填了「维护仓库」的店,先看
  python cli.py node_clear                            # 真跑
  python cli.py node_clear -p store=谭总12 --dry-run   # 只看一家店
  python cli.py node_clear -p store=谭总12 -p node=10003247367   # 只清这一个旧仓

⚠ **受管仓必须校验过**(`store_limits.managed_nodes`,与维护链/上架链同一个
入口):"受管仓以外全部清零"建在一个填错的编号上,等于把真正在卖的仓清空。
认不出/读不到的店整店跳过并点名,不猜。

⚠ **只清「受管仓已接管」的 SKU**(受管仓有库存行 = 维护链写过它 = 清完仍
可售)。未接管的跳过并点名 —— 一把清完会让那批在所有节点都是 0,直接断售
(谭总12 搬仓当天实见 112 个未接管、其中 83 个旧节点还有货)。
确实要连它们一起清用 `-p include_untaken=1`。

⚠ **拒绝清受管仓**:那是自动链正在维护的节点,清了它下一轮维护链又写回来
(两条规则打架,而且没人看得出是谁在跟谁较劲)。要停售整店走 stockzero。

⚠ **节点身份未知的库存不碰**:接口没给 shipNode(键为空串或 `?序号`)的那份
数量,拿去写只能走不带节点的旧接口 = 写到官方无定义的默认节点,可能正是
受管仓。只点名,不写。

没配「维护仓库」的店:全船队模式不碰;单店模式须显式给旧仓
`-p node=<FC ID> -p include_untaken=1`(判不出接管与否,等于整节点清空,先想清楚)。

为什么走单品 PUT 而不是 MP_INVENTORY feed:这是**一次性破坏动作**,逐条的
成败当场就要知道(feed 要等回执、失败混在批里)。3600 条按 160/min 的桶
约 23 分钟,可以接受;店间并发(每店自己的代理与配额桶)。写 0 是幂等的,
中断了重跑一遍即可,不需要防重台账。店级失败按店维标准(`store_retry.fan_out`:
跨店并发 → 失败店串行补试一遍 → 仍失败点名)。
"""

import logging

from api import inventory as inv_api
from services import store_limits, store_retry, stores as stores_svc

DANGEROUS = True
SUPPORTS_STORE = True

logger = logging.getLogger("workflows.node_clear")

_DEFAULT_LIMIT = 5000       # 单店单轮上限(SKU×节点 条数):防手滑对超大店整店重写


def _unknown_node(node: str) -> bool:
    """输入:节点键 → 输出:是否"接口没给 shipNode"(空串 / `?序号`,见 api.inventory._nodes)。"""
    return not node or node.startswith("?")


def plan(nodes: dict[str, dict[str, int]], managed: str | None,
         only_node: str | None = None, include_untaken: bool = False
         ) -> tuple[list[tuple[str, str, int]], dict, dict, int]:
    """输入:{sku: {节点: 数量}} + 受管仓(+只清的节点、是否连未接管的也清)
    → 输出:(待清 [(sku, 节点, 数量)], {旧节点: (有货 SKU 数, 件数)}, 未接管 {sku: 件数}, 身份未知的有货份数)。

    纯函数:判据全在这里,run() 只管读、写、报。
    """
    targets: list[tuple[str, str, int]] = []
    per_node: dict[str, tuple[int, int]] = {}
    untaken: dict[str, int] = {}
    n_unknown = 0
    for sku, nd in nodes.items():
        taken = bool(managed) and managed in nd
        for node, qty in nd.items():
            if qty <= 0 or (managed and node == managed):
                continue
            if only_node and node != only_node:
                continue
            if _unknown_node(node):
                n_unknown += 1
                continue
            c, t = per_node.get(node, (0, 0))
            per_node[node] = (c + 1, t + qty)
            if not taken and not include_untaken:
                untaken[sku] = untaken.get(sku, 0) + qty
                continue
            targets.append((sku, node, qty))
    targets.sort(key=lambda x: (-x[2], x[0], x[1]))
    return targets, per_node, untaken, n_unknown


def _clear_store(store: dict, managed: str | None, only_node: str | None,
                 include_untaken: bool, limit: int, preview: bool) -> dict:
    """输入:店铺 + 受管仓 + 参数 → 输出:{name, lines, planned, qty, ok, failed}。

    读节点失败(含凭证死)直接抛,交 fan_out 按店维标准处理;逐条写失败
    当场收集点名(写 0 幂等,重跑即补)。
    """
    name = store["name"]
    nodes = inv_api.list_inventory_nodes(store)
    targets, per_node, untaken, n_unknown = plan(
        nodes, managed, only_node, include_untaken)
    lines = [f"  {name}(受管仓 {managed or '未配置'}"
             + (f",只清 {only_node}" if only_node else "") + f"):全店 {len(nodes)} SKU"]
    if per_node:
        lines.append("    非受管仓有货:" + ",".join(
            f"{nd} {c} 个/{t} 件" for nd, (c, t) in sorted(per_node.items())))
    if n_unknown:
        lines.append(f"    ⚠ 节点身份未知(接口没给 shipNode)的有货 {n_unknown} 份:"
                     f"不碰 —— 不带节点只能写默认节点,可能正是受管仓")
    if untaken:
        lines.append(
            f"    ⚠ 受管仓**尚未接管** {len(untaken)} 个(旧仓合计 "
            f"{sum(untaken.values())} 件):本轮跳过不清(清了会断售)。"
            f"样本:{sorted(untaken)[:5]}")
        lines.append(
            "      成因两类:① 非 amz 出身的行维护链不碰 → 先跑 "
            "`sources_backfill` 认领;② amz 行本轮没产意图(断货/待删/"
            "防重)→ 多数下轮自愈。补完再跑本工作流收尾;确实要连它们"
            "一起清加 -p include_untaken=1")
    order = targets
    if len(order) > limit:
        lines.append(f"    ⚠ 超单店单轮上限 {limit},本轮只清前 {limit} 条"
                     f"(按数量降序,其余下轮;-p limit= 可调)")
        order = order[:limit]
    qty = sum(q for _, _, q in order)
    res = {"name": name, "lines": lines, "planned": len(order), "qty": qty,
           "ok": 0, "failed": 0}
    if not order:
        lines.append("    没有可清的,无事可做")
        return res
    lines.append(f"    待清 {len(order)} 条(SKU×节点),合计 {qty} 件;"
                 f"数量最多的 3 条:{order[:3]}")
    if preview:
        return res
    fails: list[tuple[str, str, str]] = []
    for sku, node, _q in order:
        try:
            ok, why = inv_api.put_inventory(store, sku, 0, node)
        except Exception as e:                          # noqa: BLE001
            ok, why = False, f"{type(e).__name__}: {e}"
        if ok:
            res["ok"] += 1
        else:
            fails.append((sku, node, why))
            logger.warning("%s %s 节点 %s 清零失败:%s", name, sku, node, why)
    res["failed"] = len(fails)
    lines.append(f"    清零成功 {res['ok']}/{len(order)}")
    if fails:
        # 失败必须点名:写 0 是幂等的,重跑本工作流即可补上;静默的话
        # 那批货还在旧节点上继续卖,而摘要看起来一切正常
        lines.append(f"    ⚠ 失败 {len(fails)} 条(写 0 幂等,重跑本工作流即补):"
                     + ",".join(f"{s}@{n}" for s, n, _ in fails[:10])
                     + (" …" if len(fails) > 10 else ""))
        lines.append(f"      首个失败原因:{fails[0][2][:200]}")
    return res


def run(params: dict) -> str:
    """输入:params(store/node/include_untaken/limit)→ 输出:清零结果摘要。"""
    name = str(params.get("store") or "").strip()
    only_node = str(params.get("node") or "").strip() or None
    preview = bool(params.get("dry_run"))
    limit = int(params.get("limit", _DEFAULT_LIMIT))
    include_untaken = str(params.get("include_untaken", "")).strip() == "1"
    mode = "[DRY-RUN] " if preview else ""

    store_list = (stores_svc.load_stores(filter_names=[name]) if name
                  else stores_svc.load_stores())
    if name and not store_list:
        return f"⚠ 店铺 {name} 不在可调用列表里(未启用/没配代理/没凭证?)"

    # 受管仓:与维护链/上架链**同一个入口**,填了就必须被沃尔玛认过
    node_stats: dict = {}
    managed, skipped = store_limits.managed_nodes(stores=store_list,
                                                  stats=node_stats)
    if name:
        # 传了店铺列表时,列表外那些填了「维护仓库」的店会被记成「不在可调用
        # 店铺列表里」—— 单店模式下它们与本轮无关,不能摊进摘要当成跳过
        skipped = {k: v for k, v in skipped.items() if k == name}
    head_skip = ""
    if skipped:
        words = node_stats.get("words") or {}
        head_skip = (f";⚠ 受管仓校验失败整店跳过 {len(skipped)} 店:"
                     + ",".join(f"{s}({words.get(s, '校验失败')})"
                                for s in sorted(skipped)))

    if name:
        if name in skipped:
            return (f"⚠ {name}:受管仓校验失败,本轮不清 —— {skipped[name]}")
        own = managed.get(name)
        if own and only_node and only_node == own:
            # ⚠ 拒绝清受管仓:清了下一轮维护链就写回来,两条规则互相拆台
            return (f"⚠ 拒绝执行:{only_node} 正是 {name} 的**受管仓**(限额表「维护仓库」)。\n"
                    f"   自动链每轮都在维护它,清了下一轮就写回来。\n"
                    f"   要停售整店走 stockzero(限额表「库存特殊要求」=0);"
                    f"要换仓先改「维护仓库」再清旧仓。")
        if not own and not (only_node and include_untaken):
            return (f"⚠ 拒绝执行:{name} 没配「维护仓库」,判不出「哪个是旧仓、"
                    f"受管仓是否已接管」——清空节点可能让商品直接断售。\n"
                    f"   先在限额表填「维护仓库」;确实要整节点清空这家店的某个仓:"
                    f"-p node=<FC ID> -p include_untaken=1(**先想清楚**)。")
        targets = store_list
    else:
        targets = [s for s in store_list if s["name"] in managed]
        if not targets:
            return ("节点清零:没有填了「维护仓库」且校验通过的店,无事可做"
                    + head_skip)

    def _one(store: dict) -> dict:
        return _clear_store(store, managed.get(store["name"]), only_node,
                            include_untaken, limit, preview)

    results, dead, absent, gate_note = store_retry.fan_out(
        targets, _one, stores_svc.STORE_WORKERS, log_label="节点清零")
    results.sort(key=lambda r: stores_svc.sort_key(r["name"]))
    planned = sum(r["planned"] for r in results)
    qty = sum(r["qty"] for r in results)
    done = sum(r["ok"] for r in results)
    failed = sum(r["failed"] for r in results)
    missed = [f"{s}(凭证失效)" for s in sorted(dead)] + \
        [f"{s}({w})" for s, w in sorted(absent)]
    head = (f"{mode}节点清零(受管仓以外):{len(targets)} 店,"
            f"待清 {planned} 条 SKU×节点 共 {qty} 件"
            + ("(dry-run:一件都没写)" if preview
               else f",成功 {done}" + (f",⚠ 失败 {failed}" if failed else ""))
            + (f";⚠ 读库存失败 {len(missed)} 店:{','.join(missed)}"
               "(写 0 幂等,修好后重跑即补)" if missed else "")
            + head_skip)
    lines = [head]
    if gate_note:
        lines.append(f"  {gate_note}")
    for r in results:
        lines.extend(r["lines"])
    return "\n".join(lines)
