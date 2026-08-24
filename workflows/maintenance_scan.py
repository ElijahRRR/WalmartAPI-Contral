"""maintenance_scan — 商品维护扫描定性(批次四;只读,**不发任何 feed**)。

用法:
  python cli.py maintenance_scan                  # 扫描 + 落建议行(可随时跑)
  python cli.py maintenance_scan -p store=A085朱丽霖
  python cli.py maintenance_scan -p preview=1     # 只打印不落建议行
  python cli.py maintenance_scan --dry-run        # 同上(--dry-run 等价于 preview)
  python cli.py maintenance_scan -p oos_days=30   # 连续无货多少天算该删(默认 15)

本工作流是维护链拆分后的**建议半边**(所有者定稿 2026-08-16:「python cli.py
maintenance 也应该像 problem_scan 和 problem_product_cleanup 一样分开,
然后走 list 串起来」)。原来 maintenance 一个文件里既做"查库算出该改什么",
又做不可逆的 DELETE_ITEM,两件事的风险等级差着数量级 —— 想看看今天该删哪些,
就得跑一个 DANGEROUS 工作流;而且建议不留痕,事后无从追"当初为什么删它"。

拆开后:
  maintenance_scan(本文件,DANGEROUS=False)  查库 → 定性 → 落建议行
  maintenance(DANGEROUS=True)                只消费建议行,自己不做任何决策

判据全部在 `services.maintenance_intents`(provider + classify()),本文件
不做任何新判断 —— 它只负责"把意图落成建议行 + 撤销不再成立的旧建议"。

⚠ **只建议,不动状态、不发 feed。** 写库的只有 ops.dispositions 一张表,
幂等(同 (店铺,SKU,动作) 未落定时只刷新,不堆行)。

调度顺序是硬约束:catalog_sync → product_refresh → 本工作流 → maintenance。
没跑 catalog_sync 就扫 = 拿上一轮的沃尔玛现值算差异,会对已下架的 SKU 建议
改价改库存(生产实证:38 条改库存 + 30 条改价 not found)。

⚠ **本工作流照常建议删除,但执行它的不是 maintenance**(2026-08-24):
破坏动作(delete/retire)的唯一出口是 `problem_product_cleanup`,建议按
**动作**分工领取,不按来源。本文件产出的 action='delete' 行归它执行,
action ∈ (title/price/inventory) 的行归 maintenance。
"""

import logging

from registry import db
from services import dispositions, maintenance_intents as mi, store_limits

DANGEROUS = False       # 只读沃尔玛(其实一次都不调);写库仅限建议行

logger = logging.getLogger("workflows.maintenance_scan")

_KIND_LABEL = mi.KIND_LABEL      # 唯一出处在 services(它是与执行件的连接键)
_KIND_ORDER = ("delete", "title", "price", "inventory")


def _preview_lines(intents: list[dict], stockzero: list[str]) -> list[str]:
    """输入:意图 → 输出:人眼闸门要看的几段(删除名单/清零合计/改价分布/分店)。

    ⚠ 这几段是**唯一**能在真提交之前看见破坏面的地方,拆分后更要留着:
    执行件已经不做决策了,它的 dry-run 只会告诉你"要提交多少条",
    而"为什么是这些商品"只有这里说得清。
    """
    lines: list[str] = []
    dels = [i for i in intents if i["kind"] == "delete"]
    if dels:
        # 删除不可逆:名单必须看得见,且要与其余三类分开说(混在一起会误读)
        by, why = {}, {}
        for d in dels:
            by[d["store"]] = by.get(d["store"], 0) + 1
            r = d.get("code") or "?"
            why[r] = why.get(r, 0) + 1
        lines.append(f"  ⚠ 建议永久删除 {len(dels)} 行(原因:"
                     + ",".join(f"{r}×{n}" for r, n in sorted(why.items()))
                     + "):"
                     + ",".join(f"{s}×{n}" for s, n in sorted(by.items())))
        lines.append(f"    样本={[d['sku'] for d in dels[:8]]}"
                     + (" …" if len(dels) > 8 else ""))
    if stockzero:
        # 整店清零的人眼闸门:名单必须可见(不能只给个数)
        lines.append(f"  stockzero 名单:{','.join(sorted(stockzero))}")
    prices_i = [i for i in intents if i["kind"] == "price"]
    if prices_i:
        # 改价是真金白银:给出涨跌分布,别让人只看到"改价 N 条"
        up = [i for i in prices_i if i["new"] > i["old"]]
        down = [i for i in prices_i if i["new"] < i["old"]]
        worst = max(prices_i,
                    key=lambda i: abs(i["new"] - i["old"]) / max(i["old"], 0.01))
        lines.append(
            f"  改价分布:涨 {len(up)} 跌 {len(down)};"
            f"最大变动 {worst['store']} {worst['sku']} "
            f"{worst['old']}→{worst['new']}"
            f"({(worst['new'] - worst['old']) / max(worst['old'], 0.01):+.0%})")
    zeroing = [i for i in intents if i["kind"] == "inventory" and i["new"] == 0]
    if zeroing:
        codes: dict[str, int] = {}
        for z in zeroing:
            codes[z.get("code") or "-"] = codes.get(z.get("code") or "-", 0) + 1
        # 四条清零判据在飞书表里长得一模一样(库存 12 → 0),这里按原因码摊开
        lines.append(f"  清零合计 {len(zeroing)} 条,原因:"
                     + ",".join(f"{c}×{n}" for c, n in sorted(codes.items())))
    by_store: dict[str, dict[str, int]] = {}
    for it in intents:
        by_store.setdefault(it["store"], {})[it["kind"]] = \
            by_store.setdefault(it["store"], {}).get(it["kind"], 0) + 1
    for store_name, kinds in sorted(by_store.items()):
        lines.append(f"  {store_name}:" + ",".join(
            f"{_KIND_LABEL[k]} {kinds[k]}" for k in _KIND_ORDER if kinds.get(k)))
    return lines


def run(params: dict) -> str:
    """输入:params(store/preview/oos_days)→ 输出:意图分布 + 建议行落账摘要。"""
    # --dry-run 与 -p preview=1 等价:本工作流 DANGEROUS=False(不碰沃尔玛写
    # 接口),但它**会写建议表** —— 人敲 --dry-run 的本意就是"这轮别落库"
    preview = (str(params.get("preview", "")).strip() == "1"
               or bool(params.get("dry_run")))
    only = params.get("store")

    stockzero = store_limits.stockzero_stores()
    with db.pg_conn() as conn:
        intents = mi.collect_all(conn, stockzero,
                                 int(params.get("oos_days", 0) or 0))
    if only:
        intents = [i for i in intents if i["store"] == only]

    n_kind = {k: sum(1 for i in intents if i["kind"] == k) for k in _KIND_ORDER}
    lines = [f"维护意图 {len(intents)} 条:删除 {n_kind['delete']},"
             f"标题 {n_kind['title']},价格 {n_kind['price']},"
             f"库存 {n_kind['inventory']}(stockzero 店 {len(stockzero)} 家)"]
    lines += _preview_lines(intents, stockzero)

    if not mi.TITLE_MISMATCH_DELETE:
        # 停闸必须天天见人(本仓口诀:静默关闭 = 没人记得它关着)。
        # 生成侧停了之后,存量 suggested 行会被下面的 withdraw_stale 顺带撤掉
        # 停闸期口径(所有者 2026-08-20):只关删除,这批行照常改价/改标题/
        # 改库存。其中"改标题"是知情取舍(抄的可能不是同一个商品的标题),
        # 条数要摊在人眼闸门上 —— 混在"标题 N"里等于没说
        n_low = sum(1 for i in intents if i.get("code") == "title_mismatch_sync")
        lines.append("  ⚠ 删除(title_mismatch)已停闸(所有者 2026-08-19 暂停,"
                     "恢复改 services/maintenance_intents.TITLE_MISMATCH_DELETE)"
                     "——**只停删除**:这批行照常改价/改标题/改库存"
                     f"(所有者 2026-08-20:停闸不冻结);其中低相似度改标题 "
                     f"{n_low} 条(原因码 title_mismatch_sync)")
    if preview:
        return "\n".join(lines + [
            f"(preview:未落建议行;本轮将写 {len(intents)} 条)"])

    rows = [mi.to_disposition(it) for it in intents]
    with db.pg_conn() as conn:
        n_sug = dispositions.suggest_many(conn, rows)
        # 撤销本轮不再建议的陈旧行:昨天建议清零、今天亚马逊回货了,扫描件
        # 不再建议它 —— 但昨天那条 suggested 还挂着,执行件照样会把它清零。
        # ⚠ store=only 不能省:`-p store=X` 那一轮 keep 里只有该店的行,
        # 不限范围会把**其余全部店铺**的待执行建议一次清空(批次 E 的坑)
        n_wd = dispositions.withdraw_stale(
            conn, "maint",
            [(r["store"], r["sku"], r["action"]) for r in rows],
            why=f"本轮扫描不再建议{f'(限 {only})' if only else ''}",
            store=only or None)
        n_open = dispositions.count_open(conn,
                                         sources=dispositions.MAINT_SOURCES)
        stuck = dispositions.stuck_executing(
            conn, sources=dispositions.MAINT_SOURCES)
    lines.append(
        f"建议行落账:本轮写入 {n_sug} 次 → **库里待执行 {n_open} 条**"
        + (f";撤销陈旧建议 {n_wd} 条(本轮不再建议 —— 商品自己恢复正常了)"
           if n_wd else "")
        + (f"。⚠ 写入 {n_sug} < 意图 {len(intents)}:差额是同 (店铺,SKU,动作) "
           f"已有 executing 行(上一轮提交了还没等到 catalog_sync 复核),"
           f"按部分唯一索引写不进去 —— 这是防重不是丢单"
           if n_sug < len(intents) else ""))
    if stuck:
        lines.append(dispositions.stuck_note(stuck))
    lines.append("执行:标题/价格/库存 走 `python cli.py maintenance`;"
                 "**删除走 `python cli.py problem_product_cleanup`**"
                 "(破坏动作只有一个出口,2026-08-24)。本工作流不发任何 feed")
    return "\n".join(lines)
