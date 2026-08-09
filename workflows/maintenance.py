"""maintenance — 商品维护:标题/价格/库存(plan #6,替代旧 沃尔玛商品维护;危险,默认 dry-run)。

用法:
  python cli.py maintenance                      # dry-run:逐店逐类计数+逐 SKU 样本
  python cli.py maintenance --execute            # 真跑(提交 + 写维护记录表)
  python cli.py maintenance -p store=A085朱丽霖
  python cli.py maintenance -p only=inventory    # 只跑某一类(title/price/inventory)

架构(2026-08-07 所有者定稿,对比旧三段式 sync_lark/submit/poll_yesterday):
  单一 workflow——数据源已在 PG(sync 消失),结果轮询走全局 feed_poll +
  维护记录反哺器(poll_yesterday 消失)。

意图来源(services/maintenance_intents,可插拔 provider;2026-08-09 全部做实):
  清零   限额表「库存特殊要求」=0 的 stockzero 店整店清零(不依赖采集;
         所有者定稿:不设清零二次确认)
  改价   amz 现价 × 该店区间倍率(与上架同一套 services/pricing 规则)
         vs 沃尔玛现价,差异 ≥1 分且 ≥1% 才提交;出界/倍率未配置 → 不动
  改库存 amz stock_count vs 沃尔玛 avail_qty;**没采到(NULL)不动**,
         确实缺货(0)才清零;配送 >12 天写 0(旧规则)
  改标题 amz 标题过与上架同一套文案处理(去品牌/截 199)vs 沃尔玛现标题;
         占位符跳过、productType/UPC/标题三缺一跳过(旧防线)
  三个自动 provider 都只作用于 source_type='amz' 的行(路由铁律),
  且**整店排除 stockzero 店**——否则"跟随 amz 库存"会把刚清零的货顶回去。
  ⚠ 与旧系统的驱动方式不同:旧的读飞书运营决策列(是否更新价格/新价…),
  新系统的在线产品总表是程序投影没有那些列,改为从产品中心自动算。
  涨跌幅闸位仍留在 price provider(所有者 2026-08-07:暂不需要)。

路由(旧系统实证):单店 改价 ≤5 / 改库存 ≤10 走单品 PUT(结果当场已知),
超过走 feed;标题无同步接口永远 MP_MAINTENANCE feed。单店内 标题→价格→库存
串行(同店 token 桶互挤);跨店顺序执行,单店异常隔离不炸整轮。

维护记录(registry.MAINT_SHEET,电子表格「维护记录」工作表,只追加):
  feed 路径 F=feedid、H=处理中(feed_poll 反哺器回填 H/I);
  PUT 路径 F="sync"、H=成功/失败 当场落定。

维护事件不进 catalog.product_events(所有者定稿 2026-08-07):流水在
ops.feed_log/feed_items,现状在 catalog.walmart_items,状态后果由
catalog_sync 观测入账。

⚠ 切换纪律:上调度前必须停旧 12:00 walmart-maintenance-all-stores(AI 调度
任务,非 cron);停旧前先收干净旧系统在途 feed(见 legacy_survey 切换清单)。
"""

import logging
from datetime import datetime

from api import feeds, feishu, inventory as inv_api, prices
from registry import db, resources
from services import kpi, maint_sheet, maintenance_intents as mi, \
    stores as stores_svc

DANGEROUS = True

logger = logging.getLogger("workflows.maintenance")

_KIND_LABEL = {"title": "标题", "price": "价格", "inventory": "库存"}
_KIND_ORDER = ("title", "price", "inventory")   # 单店内串行顺序(旧系统纪律)


def _load_stockzero() -> list[str]:
    """输入:无 → 输出:stockzero 店名单(限额表「库存特殊要求」= 0 的店)。

    旧系统 `str(v or "")` 的 0-falsy 陷阱(整数 0 变空串,名单静默为空)
    在这里用 _plain_text + 显式比较规避。
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


def _load_multipliers() -> dict[str, dict]:
    """限额表四个区间倍率列(改价 provider 消费;与 list_new 同一张表同一口径)。"""
    t = resources.RETIRE_LIMITS
    f = t.fields
    try:
        recs = feishu.list_records(t, field_names=[
            f.store, f.fba_range1, f.fba_range2, f.fbm_range1, f.fbm_range2])
    except LookupError:
        logger.warning("限额表未登记,改价意图本轮为空")
        return {}
    out: dict[str, dict] = {}
    for rec in recs:
        name = feishu._plain_text(rec["fields"].get(f.store)).strip()
        if name:
            out[name] = {k: feishu._plain_text(rec["fields"].get(getattr(f, k)))
                         for k in ("fba_range1", "fba_range2",
                                   "fbm_range1", "fbm_range2")}
    return out


def collect_intents(conn, stockzero: list[str]) -> list[dict]:
    """输入:连接 + stockzero 名单 → 输出:全部维护意图(各 provider 汇总)。

    stockzero 店整店排除在三个自动 provider 之外——它们归 zero_intents,
    否则"跟随 amz 库存"会把刚清零的货又顶回去(两条规则打架)。
    """
    mults = _load_multipliers()
    intents = []
    intents += mi.title_intents(conn, stockzero)
    intents += mi.price_intents(conn, mults, stockzero)
    intents += mi.inventory_intents(conn, stockzero)
    intents += mi.zero_intents(conn, stockzero)
    return intents


def _submit_kind(store: dict, kind: str, items: list[dict],
                 today: str, lines: list[str]) -> list[tuple]:
    """输入:店铺 + 类型 + 意图 → 输出:维护记录表行。路由 PUT/feed(显式 if)。"""
    name = store["name"]
    label = _KIND_LABEL[kind]
    records: list[tuple] = []

    if kind != "title" and len(items) <= mi.SYNC_THRESHOLDS.get(kind, 0):
        for it in items:    # 小批量:单品 PUT,结果当场已知
            if kind == "price":
                ok, why = prices.put_price(store, it["sku"], it["new"])
            else:
                ok, why = inv_api.put_inventory(store, it["sku"], it["new"])
            records.append((name, it["sku"], label, it["old"], it["new"],
                            "sync", today, "成功" if ok else "失败", why))
        n_ok = sum(1 for r in records if r[7] == "成功")
        lines.append(f"  {name}:{label} 同步 PUT {len(items)},成功 {n_ok}")
        return records

    if kind == "title":
        entries = [mi.build_title_item(it["sku"], it["product_type"],
                                       it["product_id"], it["new"])
                   for it in items]
        feed_type = "MP_MAINTENANCE"
    elif kind == "price":
        entries = [{"sku": it["sku"], "price": it["new"]} for it in items]
        feed_type = "price"
    else:
        entries = [{"sku": it["sku"], "qty": it["new"]} for it in items]
        feed_type = "inventory"

    i = 0
    n = {"submitted": 0, "dedup": 0, "failed": 0, "unknown": 0}
    for res in feeds.submit_feed(store, feed_type, entries,
                                 workflow="maintenance"):
        batch = items[i:i + res["count"]]
        i += res["count"]
        n[res["outcome"]] = n.get(res["outcome"], 0) + len(batch)
        if res["outcome"] in ("submitted", "dedup") and res["feed_id"]:
            for it in batch:
                records.append((name, it["sku"], label, it["old"], it["new"],
                                res["feed_id"], today, "处理中", ""))
        elif res["outcome"] == "failed":
            for it in batch:
                records.append((name, it["sku"], label, it["old"], it["new"],
                                "", today, "提交被拒", ""))
    line = f"  {name}:{label} feed 提交 {n['submitted']}"
    if n["dedup"]:
        line += f",在途防重跳过 {n['dedup']}"
    if n["failed"]:
        line += f",⚠ 提交被拒 {n['failed']}(查日志)"
    if n["unknown"]:
        line += f",⚠ 结局不确定留 pending {n['unknown']}(待对账)"
    lines.append(line)
    return records


def run(params: dict) -> str:
    """输入:params(execute/store/only)→ 输出:维护提交摘要。"""
    execute = bool(params.get("execute"))
    only = params.get("only")
    if only and only not in _KIND_ORDER:
        return f"only 参数只接受 title/price/inventory,收到:{only}"

    stockzero = _load_stockzero()
    with db.pg_conn() as conn:
        intents = collect_intents(conn, stockzero)
    if params.get("store"):
        intents = [i for i in intents if i["store"] == params["store"]]
    if only:
        intents = [i for i in intents if i["kind"] == only]
    if not intents:
        return (f"无维护意图(amz 侧与沃尔玛现值一致或差异未超阈值;"
                f"stockzero 店 {len(stockzero)} 家无在售库存可清)")

    by_store: dict[str, dict[str, list[dict]]] = {}
    for it in intents:
        by_store.setdefault(it["store"], {}).setdefault(it["kind"], []).append(it)

    mode = "" if execute else "🧪 [DRY-RUN] "
    n_kind = {k: sum(1 for i in intents if i["kind"] == k) for k in _KIND_ORDER}
    lines = [f"{mode}维护意图 {len(intents)} 条:标题 {n_kind['title']},"
             f"价格 {n_kind['price']},库存 {n_kind['inventory']}"
             f"(stockzero 店 {len(stockzero)} 家)"]

    if not execute:
        if stockzero:
            # 整店清零的人眼闸门:名单必须可见(不能只给个数)
            lines.append(f"  stockzero 名单:{','.join(sorted(stockzero))}")
        # 改价是真金白银:给出涨跌分布,别让人只看到"改价 N 条"
        prices_i = [i for i in intents if i["kind"] == "price"]
        if prices_i:
            up = [i for i in prices_i if i["new"] > i["old"]]
            down = [i for i in prices_i if i["new"] < i["old"]]
            worst = max(prices_i,
                        key=lambda i: abs(i["new"] - i["old"]) / max(i["old"], 0.01))
            lines.append(
                f"  改价分布:涨 {len(up)} 跌 {len(down)};"
                f"最大变动 {worst['store']} {worst['sku']} "
                f"{worst['old']}→{worst['new']}"
                f"({(worst['new'] - worst['old']) / max(worst['old'], 0.01):+.0%})")
        zeroing = [i for i in intents
                   if i["kind"] == "inventory" and i["new"] == 0]
        if zeroing:
            lines.append(f"  清零合计 {len(zeroing)} 条"
                         f"(含 stockzero 整店 + 缺货/货期超限跟随)")
        for store_name, kinds in sorted(by_store.items()):
            for kind in _KIND_ORDER:
                items = kinds.get(kind)
                if not items:
                    continue
                route = ("feed" if kind == "title"
                         or len(items) > mi.SYNC_THRESHOLDS.get(kind, 0)
                         else "PUT")
                sample = [(it["sku"], f"{it['old']}→{it['new']}")
                          for it in items[:5]]
                lines.append(f"  {store_name}:{_KIND_LABEL[kind]} {len(items)} 条"
                             f"(路由 {route}),样本={sample}"
                             + (" …" if len(items) > 5 else ""))
        return "\n".join(lines)

    stores_by_name = {s["name"]: s for s in stores_svc.load_stores()}
    today = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d")
    all_records: list[tuple] = []
    for store_name, kinds in sorted(by_store.items()):
        store = stores_by_name.get(store_name)
        if store is None:
            lines.append(f"  {store_name}:凭证缺失,跳过")
            continue
        try:    # 单店隔离:单店代理/网络异常不炸整轮(与 cleanup 同款纪律)
            for kind in _KIND_ORDER:
                if kinds.get(kind):
                    all_records += _submit_kind(store, kind, kinds[kind],
                                                today, lines)
        except Exception as e:
            logger.exception("店铺 %s 维护提交异常,跳过继续其它店: %s",
                             store_name, e)
            lines.append(f"  ⚠ {store_name}:提交异常已跳过({e}),下轮重试")

    try:
        written = maint_sheet.append_records(all_records)
        lines.append(f"维护记录追加 {written} 行;feed 结果轮询走 feed_poll")
    except LookupError as e:
        lines.append(f"⚠ 维护记录表未登记,流水未写表(台账已在 PG):{e}")
    return "\n".join(lines)
