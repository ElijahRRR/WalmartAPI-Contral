"""feed_poll — 全局 feed 轮询(所有 feed 操作共用,plan 表外基础设施)。

用法:
  python cli.py feed_poll                 # 轮询 ops.feed_log 全部在途 feed
  python cli.py feed_poll -p feed_id=X    # 诊断:打印该 feed 逐 SKU 完整报错
                                          # (只读,不改台账、不跑反哺器)
  python cli.py feed_poll -p stats=1      # 报错排行(默认近 30 天,全 feed 类型)
  python cli.py feed_poll -p stats=1 -p days=7 -p feed_type=MP_ITEM

报错明细是**标准动作**(所有者定稿 2026-08-09):轮询时逐条落
ops.feed_item_errors(type/code/**field**/description),各业务表的报错列
统一写「码 | 人话」。上架/删除/停用/改价/改库存/改标题一视同仁——
数字错误码本身不含任何可修的信息,description 才是改进线索。

职责:扫 feed_log 的 submitted 行 → 查沃尔玛终态 → SKU 级结果落
ops.feed_items(权威台账)→ feed_log 落 done/failed;pending 行
(提交结局不确定)告警待人工。只读沃尔玛 + 记账,非危险。

轮询完执行**反哺器列表**(所有者定稿 2026-08-07:一切 feed 结果的表格
回写都交给轮询,业务表状态不依赖"记得再跑一次业务工作流"):每个反哺器
是一个 services 积木,纯读 ops.feed_items 台账写自己的业务表,幂等;
单个失败只告警不拖垮轮询本体和其它反哺器。未来上架/改价/改库存/改标题
feed 上线时,各自的反哺器在 _REFLECTOR_CHAINS 登记一条链即接入;
链与链之间**并发**跑,同一张表的多个反哺器留在同一条链里按序跑。

与各业务工作流的关系:product_clear 等提交后自己也会轮询并刷新飞书投影列;
本工作流是**兜底与加密度**——业务工作流一天跑一次,它可以挂高频调度
(如每 30 分钟),让台账尽快落定、各业务表尽快见到结果。
"""

import logging

from api import feeds
from registry import db
from services import clear_sheet, feed_track, listing_sheet, maint_sheet, \
    match_sheet, stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.feed_poll")

# 业务表反哺器登记处:**每个内层 list 是一条串行链,链与链之间并发**。
# 新 feed 工作流上线时在此追加一条链,例:[("上架表", listing_sheet.sync_from_ledger)]
#
# 为什么要分链而不是拍平了全并发(所有者定稿 2026-08-17「一起并」):
# 分链的判据是**写不写同一张表**,不是"看着相不相关"。上架表与上架表自愈都
# 走 `read_rows(LISTING_SHEET)` → 算 → 回写,是**读-改-写三步**;并发跑的话
# 后者会读到前者写之前的那份快照,回写时按旧值覆盖新值。
# api.feishu 的 `_sheet_locks` 只串得住"写"这一步,串不住跨了两次网络调用的
# 读-改-写,所以同表的反哺器必须留在同一条链里、按登记顺序跑。
#
# 反过来,不同表之间没有共享写:各自读 ops.feed_items(只读)写自己那张表,
# 库侧只有 maint_sheet 写 ops.cursors(自己的游标)、listing_sheet 写
# catalog.upc_pool(两个都在上架链里,已串行)。
_REFLECTOR_CHAINS: list[list[tuple[str, object]]] = [
    [("停用/删除表", clear_sheet.sync_from_ledger)],
    [("维护记录", maint_sheet.sync_from_ledger)],
    [("跟卖表", match_sheet.sync_from_ledger)],
    [("上架表", listing_sheet.sync_from_ledger),
     # K=Unknown 自愈(所有者批复 2026-08-12):feed 台账终态 + 目录在线
     # 双源收尾,替代旧 sync_status_track 的自愈半边。只写飞书与 UPC 池,
     # 不碰沃尔玛——反哺器"只读沃尔玛"的契约不破
     ("上架表自愈", listing_sheet.heal_unknown)],
]


def _explain(store: dict, feed_id: str) -> str:
    """输入:店铺 + feed_id → 输出:逐 SKU 的完整报错,并补落 ops.feed_item_errors。

    终态 feed 不会被再轮询,历史 feed 的报错靠这里补进明细表(幂等);
    状态列不动(status/error_code 已是终态),只补分析用的明细。
    """
    lines = [f"feed {feed_id} 明细:"]
    all_errs: dict[str, list[dict]] = {}
    for item in feeds.iter_feed_items(store, feed_id):
        sku = str(item.get("sku") or "?")
        status = item.get("ingestionStatus")
        errs = (item.get("ingestionErrors") or {}).get("ingestionError") or []
        all_errs[sku] = errs
        lines.append(f"  {sku} [{status}]")
        for e in errs:
            lines.append(f"    - type={e.get('type')} code={e.get('code')} "
                         f"field={e.get('field')}")
            desc = str(e.get("description") or e.get("message") or "").strip()
            if desc:
                lines.append(f"      {desc}")
        if not errs:
            lines.append("    (无 ingestionErrors)")

    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT sku, workflow, feed_type FROM ops.feed_items "
                    "WHERE feed_id = %s", (feed_id,))
        meta = {sku: (wf, ft) for sku, wf, ft in cur.fetchall()}
        n = feed_track._save_errors(cur, feed_id, store["name"], all_errs, meta)
        cur.executemany(
            "UPDATE ops.feed_items SET error_desc = %s"
            " WHERE feed_id = %s AND sku = %s AND error_desc IS NULL",
            [(feed_track.error_text(e), feed_id, s)
             for s, e in all_errs.items() if e])
    lines.append(f"(报错明细已补落 {n} 条,聚合看 ops.v_feed_error_stats)")
    return "\n".join(lines)


def _error_stats(days: int, feed_type: str = "") -> str:
    """输入:天数(+可选 feedType)→ 输出:报错排行(改哪个字段收益最大)。

    读 ops.feed_item_errors——所有 feed 类型通用(上架/删除/停用/改价/
    改库存/改标题),报错是系统自我优化的燃料。
    """
    sql = ("SELECT feed_type, field, code, count(*) n,"
           " count(DISTINCT sku) skus, min(description) sample"
           " FROM ops.feed_item_errors"
           " WHERE occurred_at >= now() - make_interval(days => %s)")
    args: list = [days]
    if feed_type:
        sql += " AND feed_type = %s"
        args.append(feed_type)
    sql += " GROUP BY 1,2,3 ORDER BY n DESC LIMIT 25"
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    if not rows:
        return f"近 {days} 天无 feed 报错记录"
    lines = [f"近 {days} 天 feed 报错排行(共 {len(rows)} 组):"]
    for ft, field, code, n, skus, sample in rows:
        lines.append(f"  {n:>4}次/{skus:>3}SKU  [{ft or '-'}] {field or '-'}"
                     f"  {code or '-'}")
        if sample:
            lines.append(f"        {str(sample)[:150]}")
    return "\n".join(lines)


def run(params: dict) -> str:
    """输入:params(store / feed_id 诊断 / stats 排行)→ 输出:摘要。"""
    if params.get("stats"):
        return _error_stats(int(params.get("days", 30)),
                            str(params.get("feed_type", "")))
    names = [params["store"]] if params.get("store") else None
    store_list = stores_svc.load_stores(names)
    stores_by_name = {s["name"]: s for s in store_list}

    if params.get("feed_id"):       # 诊断模式:只打印详情,不动台账不跑反哺器
        feed_id = str(params["feed_id"])
        store = stores_by_name.get(str(params.get("store") or ""))
        if store is None:
            with db.pg_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT store FROM ops.feed_log WHERE feed_id = %s",
                            (feed_id,))
                row = cur.fetchone()
            if not row or row[0] not in stores_by_name:
                return (f"feed {feed_id} 不在台账中或店铺未加载:"
                        f"请补 -p store=<店铺名>")
            store = stores_by_name[row[0]]
        # 明说这一模式不写任何东西:所有者 2026-08-09 用它查了维护 feed,
        # 看到结果却发现飞书没变——两条路径长得太像,不说就会被当成故障
        return (_explain(store, feed_id)
                + "\n(诊断模式:不动 ops.feed_items 台账、不回写飞书;"
                  "要落定并回写请跑不带 -p feed_id 的 python cli.py feed_poll)")
    lines = [feed_track.poll_all(stores_by_name)]
    lines.extend(_run_reflectors())
    return "\n".join(lines)


def _one_chain(chain: list) -> list[str]:
    """输入:一条反哺链 → 输出:该链的摘要行(链内按登记顺序串行)。

    单个反哺器失败**只吃掉它自己那一行**,同链后面的照跑 —— 与并发之前逐个
    try/except 的语义一字不差:台账已经落定了,回写是幂等的补写,下一轮或
    业务工作流还会再补一次,没有理由让一张表的故障顺带停掉另一张。
    """
    out: list[str] = []
    for label, sync in chain:
        try:
            line = sync()
        except Exception as e:
            # 反哺失败不拖垮轮询本体:台账已落定,下轮或业务工作流补写
            logger.warning("%s 回写失败(台账已落定,下轮补写): %s", label, e)
            line = f"⚠ {label}回写失败:{e}"
        if line:
            out.append(line)
    return out


def _run_reflectors() -> list[str]:
    """输入:无 → 输出:全部反哺器的摘要行(链间并发,链内串行)。

    ⚠ 摘要按 `_REFLECTOR_CHAINS` 的**登记顺序**拼,不按完成先后:五张表快慢
    差得远(上架表几万行、跟卖表几十行),按完成序拼的话每轮通知里的段落顺序
    都不一样,人对着看会以为少了一段。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    done: dict[int, list[str]] = {}
    with ThreadPoolExecutor(max_workers=len(_REFLECTOR_CHAINS)) as pool:
        futs = {pool.submit(_one_chain, c): i
                for i, c in enumerate(_REFLECTOR_CHAINS)}
        for f in as_completed(futs):
            done[futs[f]] = f.result()
    return [line for i in range(len(_REFLECTOR_CHAINS)) for line in done[i]]
