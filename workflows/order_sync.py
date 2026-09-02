"""order_sync — 订单拉取入订单中心(plan #4 order_audit 的取数前半)。

用法:
  python cli.py order_sync                     # 全部启用店铺,近 45 天
  python cli.py order_sync -p store=A085朱丽霖  # 单店
  python cli.py order_sync -p days=90          # 自定窗口
  python cli.py order_sync -p workers=8        # 跨店并发
  python cli.py order_sync -p repair_order_date=PO1,PO2   # 显式修复:只对列出的 PO 允许 API 下单时间覆盖库值
                                                         #(详情接口定稿后列表永不覆盖;详情不可用时退回连续两轮一致)

每店:GET /v3/orders(createdStartDate=now-days)→ 展开订单行 → upsert
orders.order_lines。窗口全量重拉而非游标增量:订单状态在创建后持续变化
(Created→Shipped→Delivered/Cancelled),按创建时间增量会漏老订单的状态迁移,
窗口重拉 + 幂等 upsert 天然覆盖(旧系统同样按日全刷 45 天)。

下单时间例外(所有者定稿 2026-09-02,语义唯一出处 services/order_lines):
列表接口的 orderDate 单次读取不可信(每轮约 2% 回错),详情接口可信。新单首见
查详情落库并定稿;没被详情核对过的存量行每轮查详情直到定稿;定稿后列表再不一致
只计数不改。详情不可用退回"连续两轮一致才定稿"。改判/拒写/疑错点名进摘要首行;
只有显式 repair_order_date=<PO 列表> 才对列出的 PO 按 API 值直接改写。

并发形态(2026-08-13,蓝图 §6.3 async 变体落地):跨店并发由
api/orders.fetch_orders_bulk 承担(asyncio 藏在 api 层内部,默认 12 店
同时拉,网络与入库在线程池重叠);本文件只提供持久化回调,保持同步世界。

审核列(audit_status/audit_detail/audited_at)不在本工作流的 upsert 列内,
重拉不会冲掉审核结论——审核规则与采集对接后由 order_audit 补全。
"""

import functools
import logging
from datetime import datetime, timedelta, timezone

from api import _client, orders as orders_api
from registry import db
from services import notify_fmt as nf, order_center, store_retry
from services import order_lines as ol
from services import stores as stores_svc

DANGEROUS = False

logger = logging.getLogger("workflows.order_sync")


_ANOMALY_KINDS = ("疑错", "改判", "拒写", "详情补正", "详情失败", "冲突", "待定", "存疑")


def _persist(store: dict, orders: list[dict], *, anomalies: list | None = None,
             repair: frozenset = frozenset(), detail_stats: dict | None = None) -> int:
    """输入:店铺 + 该店全部订单(+异常收集器/修复 PO 集/详情计数器)→ 输出:入库行数(fetch_orders_bulk 回调)。

    下单时间(所有者定稿 2026-09-02):新单首见以**详情接口**的值落库并定稿;没被
    详情核对过的存量行每轮查详情直到定稿;详情定稿后列表再不一致只计数、不查不改。
    详情不可用时退回观测→定稿两轮机制。每类异常(疑错/改判/拒写/详情补正/详情失败/
    冲突/待定/存疑)逐条记日志并收进 anomalies(进摘要);repair 里的 PO 才允许 API 值直接覆盖。
    """
    rows: list[dict] = []
    for order in orders:
        rows.extend(ol.extract_order_lines(store["name"], order))
    found: list[dict] = []
    for r in rows:
        for mark, kind in (("_order_date_rejected", "拒写"), ("_order_date_suspect", "存疑")):
            if r.get(mark):
                found.append({"kind": kind, "po": r["po_id"], "sku": r["sku"],
                              "db": None, "api": r.get("order_date")})

    def detail(po: str):
        if detail_stats is not None:
            detail_stats["calls"] = detail_stats.get("calls", 0) + 1
        return orders_api.get_order(store, po)

    try:
        with db.pg_conn() as conn:
            found.extend(ol.screen_order_dates(conn, rows, detail=detail))
            fix = [r for r in rows if r["po_id"] in repair]
            rest = [r for r in rows if r["po_id"] not in repair]
            n = ol.upsert_order_lines(conn, rest) if rest else 0
            if fix:
                n += ol.upsert_order_lines(conn, fix, repair_order_date=True)
    except Exception as e:
        if type(e).__name__ == "UndefinedColumn":   # 表没跟上代码:说人话,别让人猜
            raise RuntimeError("orders.order_lines 缺下单时间定稿列(order_date_seen 等):"
                               "请先执行 python cli.py db_init 再跑") from e
        raise
    if anomalies is not None:
        anomalies.extend({"store": store["name"], **c} for c in found)
    return n


def _fmt(dt) -> str:
    return f"{dt:%m/%d %H:%M}" if dt else "-"


def _repair_pos(params: dict) -> frozenset:
    """输入:params → 输出:允许按 API 值改写下单时间的 PO 集合(-p repair_order_date=PO1,PO2)。

    只接受明确的 PO 列表:沃尔玛每轮都会回错十来条,整库"按 API 值改写"等于把
    当轮的错值抄进库并定稿。裸开关(1/true/yes)一律报错,不自动改口。
    """
    raw = str(params.get("repair_order_date", "") or "").strip()
    if raw.lower() in ("", "0", "false", "no", "n", "off"):
        return frozenset()
    if raw.lower() in ("1", "true", "yes", "y", "on"):
        raise ValueError("repair_order_date 需要 PO 列表(逗号分隔),不接受裸开关:"
                         "沃尔玛每轮都会回错十来条下单时间,整库按 API 值改写会把错值抄进来")
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def run(params: dict) -> str:
    """输入:params(可选 store/days/workers)→ 输出:各店拉取统计摘要。"""
    names = [params["store"]] if params.get("store") else None
    store_list = stores_svc.load_stores(names)
    if not store_list:
        return f"店铺凭证未找到:{params.get('store') or '(全部)'}"
    days = int(params.get("days", 45))
    workers = int(params.get("workers", stores_svc.STORE_WORKERS))
    created_start = (datetime.now(timezone.utc) - timedelta(days=days)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    repair = _repair_pos(params)
    anomalies: list[dict] = []
    detail_stats: dict = {}
    persist = functools.partial(_persist, anomalies=anomalies, repair=repair,
                                detail_stats=detail_stats)

    results, dead, failed = orders_api.fetch_orders_bulk(
        store_list, created_start=created_start, concurrency=workers,
        handler=persist)

    # 标准②(所有者定稿 2026-08-26,补齐:此前本文件只做隔离+分诊,同链
    # returns_sync 是全套):失败店跑完别人后串行补试一遍。单一落地路径 ——
    # 补试重调**同一个** fetch_orders_bulk(单店入参),店间串行由一次一店成立
    absent: list[tuple[str, str]] = []
    gate_note = ""
    if failed:
        by_name = {s["name"]: s for s in store_list}

        def _retry_one(store):
            r2, dead2, failed2 = orders_api.fetch_orders_bulk(
                [store], created_start=created_start, concurrency=1,
                handler=persist)
            if dead2:   # 补试中才暴露的凭证死:还原成异常交标准件归类
                raise _client.StoreDeadError(store["name"], "401/403(补试)")
            if failed2:
                raise failed2[0][1]
            return r2[0]

        recovered, still, gate_note = store_retry.serial_second_pass(
            [(by_name[n], e) for n, e in failed], _retry_one,
            total_stores=len(store_list))
        results.extend(r for _s, r in recovered)
        for s, e in still:
            cls = store_retry.diagnose(e)
            if cls == "凭证失效":
                dead.append(s["name"])
            else:
                absent.append((s["name"], cls))

    total_lines = sum(r["lines"] for r in results)
    # 标准③:缺席店点名进摘要**首行**(链通知只发成功步骤的首行);
    # 订单窗口全量重拉 + 幂等 upsert,缺席店下轮整点自然补上,无需水位避让
    counts = {k: sum(1 for a in anomalies if a["kind"] == k) for k in _ANOMALY_KINDS}
    # 首行只放要人看的:改判(库值真的变了)/ 拒写(未来日期);沃尔玛回错但被
    # 挡住的(冲突/待定)只报一个数——生产实测每轮都有十来条,逐条上首行会把
    # 通知训练成"看见 ⚠ 就划走"
    acted = " / ".join(f"{k} {counts[k]}" for k in ("疑错", "改判", "拒写") if counts[k])
    blocked = counts["冲突"] + counts["待定"] + counts["详情补正"]
    tail = ""
    if acted:
        tail += f";⚠ 下单时间:{acted}"
    if blocked or counts["存疑"]:
        tail += (f";沃尔玛下单时间回错 {blocked} 条已挡" if blocked else "") \
            + (f";下单时间存疑 {counts['存疑']} 条" if counts["存疑"] else "")
    if detail_stats.get("calls"):
        tail += f";详情复核 {detail_stats['calls']} 次" \
            + (f"(失败 {counts['详情失败']})" if counts["详情失败"] else "")
    if repair:
        tail += f"(修复模式:{len(repair)} 个 PO 已按 API 值改写)"
    lines = [f"order_sync:{len(results)}/{len(store_list)} 店完成"
             f"(窗口 {days} 天),订单行入库 {total_lines}"
             + nf.absent_tail(absent, gate_note, tail="下轮整点自然重拉") + tail]
    if anomalies:
        # 第二行给人看细节:按 改判/拒写/冲突/待定/存疑 的轻重排,前 5 条
        rank = {k: i for i, k in enumerate(_ANOMALY_KINDS)}
        shown = sorted(anomalies, key=lambda a: rank[a["kind"]])[:5]
        lines.append("  " + ";".join(
            f"{a['store']} PO {a['po']}[{a['kind']}]:库 {_fmt(a['db'])} / API {_fmt(a['api'])}"
            for a in shown) + (" …" if len(anomalies) > 5 else "") + "(详见日志)")
    suspect = [a["po"] for a in anomalies if a["kind"] == "疑错"]
    if suspect:
        # 定稿值几乎可断定是错的:给出可直接复制的修复命令,定稿值本身不自动动
        lines.append("  修复疑错(核对后执行):python cli.py order_sync -p repair_order_date="
                     + ",".join(dict.fromkeys(suspect)))
    if gate_note:
        lines.append(gate_note)
    if dead:
        lines.append(f"凭证失效跳过:{','.join(dead)}")
    if not results:
        # ⚠ 零店完成不许报成功(2026-08-26 补齐,与 returns_sync 同款闸;
        # 此前同一条 order_chain 里两步不对称):全部店都没拉到 = 凭证表
        # 整体出问题或请求形状被改坏,报成功没人会来看,订单数据静默停更
        lines.append("⚠ 零店完成 —— 本轮没有同步任何订单数据")
        raise RuntimeError("\n".join(lines))
    # 跑完就写飞书(所有者定稿 2026-08-16:已对接飞书表的执行完就写,
    # 不做成单独的)。**永不因投影失败而失败** —— 订单已经落 PG 了
    lines.append(order_center.push_after(
        order_center.BY_WORKFLOW["order_sync"], days=max(days, 90)))
    return "\n".join(lines)
