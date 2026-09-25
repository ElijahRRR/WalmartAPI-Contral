"""把**受管仓以外**的节点库存清零(一次性搬仓收尾,不进调度)。

用途(所有者定稿 2026-08-30,谭总12 搬仓):切到受管仓之后,旧节点(通常是
Virtual Node)上的存量货**自动链一律不碰** —— 它会继续被沃尔玛算进可售、
继续出单。等受管仓接管之后,用本工作流把旧节点清空,可售量才回到"只由受管仓
说了算"。设了「最大库存」的店尤其要清:维护链只往受管仓写 N,旧仓的存量还在,
线上可售 = 两者之和,上限就被绕过去了。

**做法(所有者定稿 2026-09-25)**:不调接口读库存,也不逐条写 ——
  ① 取填了「维护仓库」且校验通过的店(受管仓编号来自限额表,校验有记忆);
  ② 从库里的分仓库存(`catalog.item_node_inventory`,catalog_sync 每轮按
     「SKU × 仓」刷新)查出**维护仓以外有货**的「SKU × 旧仓」;
  ③ 按「店 × 旧仓」分批,用分仓库存 feed(MP_INVENTORY)批量写 0,回执由
     feed_poll 记账(`ops.feed_items`,workflow=node_clear)。分批是因为同一个
     SKU 可能在两个旧仓都有货,同一个 feed 里 SKU 不能重复。

  python cli.py node_clear --dry-run                  # 所有填了「维护仓库」的店,先看
  python cli.py node_clear -p store=谭总12             # 先真跑一家,看回执
  python cli.py node_clear                            # 其余店

**判断条件**(所有者定稿 2026-09-25):**维护仓以外、有货就清**,不看维护仓里
有没有这个 SKU。维护仓里还没有记录的商品,清完在所有仓都是 0:
  · 亚马逊来源且目标库存大于 0 的,下一轮维护会往维护仓写库存,恢复可售;
  · 跟卖的,下一轮维护会在维护仓补到 10 件;
  · 亚马逊来源而目标是 0 的(缺货/不足门槛),本来就该停售;
  · 非亚马逊来源的(没有任何链往维护仓给它们补货),就此停售 —— 所有者知情接受。
⚠ 此前有一道「只清维护仓已接管的 SKU」的闸(2026-08-31,谭总12 搬仓当天实见
112 个未接管),2026-09-25 所有者定稿去掉:它会让"维护链判目标为 0 所以从不写
维护仓"的商品永远留在旧仓继续卖,也让非亚马逊来源的商品永远清不掉。
为了少一段"清完等补货"的空窗,**先跑维护链、等第二天早上的同步,再跑本工作流**。

⚠ **受管仓必须校验过**(`store_limits.managed_nodes`,与维护链/上架链同一个
入口):判据建在一个填错的编号上,等于把真正在卖的仓当旧仓清空。认不出/读不到
的店整店跳过并点名。

⚠ **节点身份未知的库存不碰**:同步时接口没给 shipNode 的那份数量(键为空串),
没有节点可写 —— 只点名。

⚠ 库里是**上一次 catalog_sync** 的数据(每天 06:40、13:00)。同步之后才出现在
旧仓的库存,要等下一次同步完再跑一遍才会被清;库里显示旧仓有货、实际已经清空
的,再写一次 0 也没有影响。同一批载荷还在处理中时重跑,feed 台账的防重会拦下
(`dedup`),不会重复提交。

⚠ 旧仓里通常有 Virtual 节点。用 feed 给 Virtual 写 0 没有实测记录(此前实测都是
单品 PUT),所以**第一次真跑先只跑一家店**,确认 feed 回执全部成功再跑其余店。

⚠ 顺带影响「实际结果」复核:它把同一 (店, SKU, feedType) 后提交的 feed 视为覆盖
了前一个,不看节点 —— 本工作流的 MP_INVENTORY feed 会让同一 SKU 此前那条维护链
MP_INVENTORY 明细不再被判生效与否。本工作流自己的明细没有处置建议行,在那里记为
「无目标值」,不进复核清单。
"""

import logging

from api import feeds
from registry import db
from services import store_limits, store_retry, stores as stores_svc

DANGEROUS = True
SUPPORTS_STORE = True

logger = logging.getLogger("workflows.node_clear")

_FEED_TYPE = "MP_INVENTORY"

# 该店受管仓以外、有货的「SKU × 仓」。
# 只看目录里还在的 SKU(missing_since IS NULL):不在架的码写了也没有意义。
# ⚠ 每个参数带显式 ::text(本仓 SQL 纪律:PG 推不出类型在生产上连炸过三次)。
_SQL_OLD_NODE_STOCK = """
SELECT n.sku, n.ship_node, n.avail_qty
FROM catalog.item_node_inventory n
JOIN catalog.walmart_items w
  ON w.store = n.store AND w.sku = n.sku AND w.missing_since IS NULL
WHERE n.store = %(store)s::text
  AND n.ship_node <> %(managed)s::text
  AND n.avail_qty > 0
ORDER BY n.ship_node, n.sku
"""


def plan(rows: list[tuple]) -> tuple[dict[str, list[str]], dict, int]:
    """输入:[(sku, 旧仓, 数量)] → 输出:(待清 {旧仓: [sku…]},
    {旧仓: (待清 SKU 数, 件数)}, 节点身份未知的份数)。

    纯函数:判断条件全在这里(见模块头注),run() 只管读、发、报。
    """
    targets: dict[str, list[str]] = {}
    per_node: dict[str, tuple[int, int]] = {}
    n_unknown = 0
    for sku, node, qty in rows:
        if not node:
            n_unknown += 1          # 同步时接口没给 shipNode:没有节点可写
            continue
        targets.setdefault(node, []).append(sku)
        c, t = per_node.get(node, (0, 0))
        per_node[node] = (c + 1, t + int(qty))
    return targets, per_node, n_unknown


def _clear_store(store: dict, managed: str, preview: bool) -> dict:
    """输入:店铺 + 已校验的受管仓 + 是否空跑 → 输出:{name, lines, planned, qty, 各结局计数}。

    读库失败或提交时凭证死直接抛,交 fan_out 按店维标准处理(失败店串行补试一遍;
    补试时同一批载荷若已提交成功,feed 台账防重会拦成 dedup,不会重复提交)。
    """
    name = store["name"]
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_OLD_NODE_STOCK, {"store": name, "managed": managed})
        rows = cur.fetchall()
    targets, per_node, n_unknown = plan(rows)
    planned = sum(len(v) for v in targets.values())
    qty = sum(t for _, t in per_node.values())
    res = {"name": name, "lines": [], "planned": planned, "qty": qty,
           "submitted": 0, "dedup": 0, "failed": 0, "unknown": 0, "feeds": []}
    lines = res["lines"]
    lines.append(f"  {name}(受管仓 {managed}):待清 {planned} 条(SKU×旧仓),"
                 f"合计 {qty} 件"
                 + ("" if not per_node else ";" + ",".join(
                     f"旧仓 {nd} {c} 个/{t} 件"
                     for nd, (c, t) in sorted(per_node.items()))))
    if n_unknown:
        lines.append(f"    ⚠ 节点身份未知(同步时接口没给 shipNode)的有货 "
                     f"{n_unknown} 份:不碰")
    if not planned or preview:
        return res
    for node, skus in sorted(targets.items()):
        entries = [{"sku": s, "qty": 0, "ship_node": node} for s in skus]
        for r in feeds.submit_feed(store, _FEED_TYPE, entries,
                                   workflow="node_clear"):
            # submit_feed 的结局:submitted / dedup / failed / unknown
            # (不传 defer_settle 就不会有 deferred;真出现了也按不确定报)
            outcome = (r["outcome"] if r["outcome"] in ("submitted", "dedup",
                                                        "failed") else "unknown")
            res[outcome] += r["count"]
            if r.get("feed_id"):
                res["feeds"].append(r["feed_id"])
    parts = [f"已提交 {res['submitted']} 条"]
    if res["dedup"]:
        parts.append(f"同一批还在处理中、防重拦下 {res['dedup']} 条")
    if res["failed"]:
        parts.append(f"⚠ 被拒 {res['failed']} 条(看日志里的 HTTP 响应)")
    if res["unknown"]:
        parts.append(f"⚠ 结局不确定 {res['unknown']} 条"
                     "(feed_poll 的 pending 对账会接手,不要手工补发)")
    lines.append("    " + ",".join(parts)
                 + (f";feed:{','.join(res['feeds'])}" if res["feeds"] else ""))
    return res


def run(params: dict) -> str:
    """输入:params(store 可选)→ 输出:清零摘要。"""
    name = str(params.get("store") or "").strip()
    preview = bool(params.get("dry_run"))
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
        if name in skipped:
            return f"⚠ {name}:受管仓校验失败,本轮不清 —— {skipped[name]}"
        if name not in managed:
            return (f"⚠ {name} 没配「维护仓库」:判不出哪个是受管仓,不清。"
                    f"先在限额表填「维护仓库」")
    targets = [s for s in store_list if s["name"] in managed]
    head_skip = ""
    if skipped:
        words = node_stats.get("words") or {}
        head_skip = (f";⚠ 受管仓校验失败整店跳过 {len(skipped)} 店:"
                     + ",".join(f"{s}({words.get(s, '校验失败')})"
                                for s in sorted(skipped)))
    if not targets:
        return ("节点清零:没有填了「维护仓库」且校验通过的店,无事可做"
                + head_skip)

    def _one(store: dict) -> dict:
        return _clear_store(store, managed[store["name"]], preview)

    results, dead, absent, gate_note = store_retry.fan_out(
        targets, _one, stores_svc.STORE_WORKERS, log_label="节点清零")
    results.sort(key=lambda r: stores_svc.sort_key(r["name"]))
    planned = sum(r["planned"] for r in results)
    qty = sum(r["qty"] for r in results)
    missed = [f"{s}(凭证失效)" for s in sorted(dead)] + \
        [f"{s}({w})" for s, w in sorted(absent)]
    if preview:
        tail = "(dry-run:一条都没发)"
    else:
        sums = {k: sum(r[k] for r in results)
                for k in ("submitted", "dedup", "failed", "unknown")}
        tail = (f",已提交 {sums['submitted']} 条,结果由 feed_poll 回写"
                + (f",防重拦下 {sums['dedup']}" if sums["dedup"] else "")
                + (f",⚠ 被拒 {sums['failed']}" if sums["failed"] else "")
                + (f",⚠ 结局不确定 {sums['unknown']}" if sums["unknown"] else ""))
    head = (f"{mode}节点清零(受管仓以外):{len(targets)} 店,"
            f"待清 {planned} 条 SKU×旧仓 共 {qty} 件{tail}"
            + (f";⚠ 失败 {len(missed)} 店:{','.join(missed)}"
               "(写 0 幂等,修好后重跑即补)" if missed else "")
            + head_skip)
    lines = [head]
    if gate_note:
        lines.append(f"  {gate_note}")
    for r in results:
        lines.extend(r["lines"])
    return "\n".join(lines)
