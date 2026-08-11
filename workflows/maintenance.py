"""maintenance — 商品维护:标题/价格/库存(plan #6,替代旧 沃尔玛商品维护;危险,默认 dry-run)。

用法:
  python cli.py maintenance                      # dry-run:逐店逐类计数+逐 SKU 样本
  python cli.py maintenance --execute            # 真跑(提交 + 写维护记录表)
  python cli.py maintenance -p store=A085朱丽霖
  python cli.py maintenance -p only=inventory    # 只跑某一类(delete/title/price/inventory)
  python cli.py maintenance -p only=delete       # 只处理删除类
  python cli.py maintenance -p oos_days=30       # 连续无货多少天算该删(默认 15)
  python cli.py maintenance -p resync_sheet=1    # 只补维护记录表(写表炸过之后)
  python cli.py maintenance -p prune_sheet=1     # 只裁维护记录表(默认留 7 天)

架构(2026-08-07 所有者定稿,对比旧三段式 sync_lark/submit/poll_yesterday):
  单一 workflow——数据源已在 PG(sync 消失),结果轮询走全局 feed_poll +
  维护记录反哺器(poll_yesterday 消失)。

意图来源(services/maintenance_intents,可插拔 provider;2026-08-09 全部做实):
  删除   三个原因(所有者定稿 2026-08-09),都是"这产品不值得留在架上了":
         · **variant_offset**:采集永久偏移(亚马逊把 /dp/<ASIN> 返回成兄弟
           变体页面,parser 拒绝写入,采集侧列为不自动重试)⇒ 价格/库存永远
           拿不到新数据,留着只会被前三个 provider 拿陈旧快照一轮轮跟;
         · **商品不存在**:amz 标题是占位符(旧系统只跳过标题维护,现改为删);
         · **连续无货 15 天**(-p oos_days=N 可调):清零后这么久不回货 =
           货源没了。⚠ 采集接线于 2026-08-08,历史攒够之前这条恒空。
         DELETE_ITEM 不可逆:dry-run 单独列名单与原因分布;单店单轮上限取
         限额表「下架限制」(与 product_clear 同一列,缺则退 300 并告警);
         **删除名单从其余三类里剔除**(将死的行不值得再烧一轮配额)。
  清零   限额表「库存特殊要求」=0 的 stockzero 店整店清零(不依赖采集;
         所有者定稿:不设清零二次确认)
  改价   amz 现价 × 该店区间倍率(与上架同一套 services/pricing 规则)
         vs 沃尔玛现价,差异 ≥1 分且 ≥1% 才提交;出界/倍率未配置 → 不动
  改库存 amz stock_count vs 沃尔玛 avail_qty;**没采到(NULL)也写 0**
         (所有者定稿 2026-08-09:采不到就不卖);配送 **>8 天**写 0
         (同日从旧值 12 收紧,与 list_new 共用 amz_source.MAX_LEAD_DAYS)
  改标题 amz 标题过与上架同一套文案处理(去品牌/截 199)vs 沃尔玛现标题;
         占位符跳过、productType/UPC/标题三缺一跳过(旧防线)
  **重复提交抑制**(2026-08-09 生产实证 208 条 ERR_EXT_DATA_0101198
  "stale update request"):provider 比的是 amz 值 vs walmart_items 的**上次
  扫店快照**,提交成功后本地快照要等 catalog_sync 再扫才更新——不压就会一轮轮
  重发同样的载荷。ops.dedupe 记 (店铺,SKU,类型,新值),20 小时内不重发;
  值真变了照样提交。⚠ **调度上 catalog_sync 必须排在 maintenance 之前**,
  否则还会对已下架的 SKU 提交(实证:38 条改库存 + 30 条改价 not found)。
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
catalog_sync 观测入账。**唯一例外是删除**——生死类事件恒记
(delete_submitted,source=maintenance,detail.reason 记删除原因)。

⚠ 切换纪律:上调度前必须停旧 12:00 walmart-maintenance-all-stores(AI 调度
任务,非 cron);停旧前先收干净旧系统在途 feed(见 legacy_survey 切换清单)。
"""

import logging
from datetime import datetime

from api import feeds, feishu, inventory as inv_api, prices
from registry import db, resources
from services import kpi, maint_sheet, maintenance_intents as mi, \
    product_events, stores as stores_svc

DANGEROUS = True

logger = logging.getLogger("workflows.maintenance")

_KIND_LABEL = {"delete": "删除", "title": "标题",
               "price": "价格", "inventory": "库存"}
# 单店内串行顺序(旧系统纪律);**删除排最前**:同一 SKU 既要删又要改价时,
# 先删掉就不必再为它烧改价/改库存的 feed 配额(collect_intents 已把删除名单
# 从其余三类里剔掉,这里的顺序是双保险)
_KIND_ORDER = ("delete", "title", "price", "inventory")


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


def _load_delete_caps() -> dict[str, int]:
    """限额表「下架限制」列 → {店铺: 单轮删除上限}(与 product_clear 同一列)。

    删除上限不该由代码拍脑袋(所有者问 2026-08-09「300 这个上限是从哪来的」):
    这张表就是运营给每家店定的下架配额,删除走同一个配额口径。
    店铺不在表内退 mi.DELETE_PER_STORE(provider 内告警)。
    """
    t = resources.RETIRE_LIMITS
    f = t.fields
    try:
        recs = feishu.list_records(t, field_names=[f.store, f.max_daily_retire])
    except LookupError:
        logger.warning("限额表未登记,删除上限全店退 %d", mi.DELETE_PER_STORE)
        return {}
    caps: dict[str, int] = {}
    for rec in recs:
        name = feishu._plain_text(rec["fields"].get(f.store)).strip()
        try:
            v = int(float(feishu._plain_text(
                rec["fields"].get(f.max_daily_retire)) or 0))
        except ValueError:
            v = 0
        if name and v > 0:
            caps[name] = v
    return caps


def collect_intents(conn, stockzero: list[str],
                    oos_days: int = 0) -> list[dict]:
    """输入:连接 + stockzero 名单 → 输出:全部维护意图(各 provider 汇总)。

    stockzero 店整店排除在三个自动 provider 之外——它们归 zero_intents,
    否则"跟随 amz 库存"会把刚清零的货又顶回去(两条规则打架)。
    """
    mults = _load_multipliers()
    deletes = mi.delete_intents(conn, stockzero, _load_delete_caps(),
                                oos_days=oos_days or mi.LONG_OOS_DAYS)
    doomed = {(d["store"], d["sku"]) for d in deletes}
    intents = list(deletes)
    for it in (mi.title_intents(conn, stockzero)
               + mi.price_intents(conn, mults, stockzero)
               + mi.inventory_intents(conn, stockzero)
               + mi.zero_intents(conn, stockzero)):
        # 将被删除的行不再改价/改库存/改标题:它们的 amz 数据本来就是陈旧的
        # (采不到才要删),再跟一轮既烧配额又是拿错数据改线上
        if (it["store"], it["sku"]) not in doomed:
            intents.append(it)
    # 近期已提交过同一件事的压掉:提交成功后本地快照要等 catalog_sync 才更新,
    # 不压就会一轮轮重发同样的载荷(生产实证 208 条 stale update)
    intents, _n = mi.drop_recent(conn, intents)
    return intents


def _record_deletes(store: str, rows: list[dict], feed_id) -> None:
    """删除提交入病历(catalog.product_events;回执由 feed_track 另记)。"""
    with db.pg_conn() as conn:
        product_events.record_many(conn, [
            {"sku": r["sku"], "store": store, "event": product_events.DELETE_SUBMITTED,
             "source": "maintenance",
             "detail": {"feed_id": feed_id,
                        "reason": r.get("reason") or "variant_offset",
                        "batches": r.get("batches"),
                        "first_seen": r.get("first_seen"),
                        "last_seen": r.get("last_seen")}}
            for r in rows])


def _record_submitted(items: list[dict]) -> None:
    """已提交的意图记账(ops.dedupe),供下轮 drop_recent 抑制重复提交。"""
    with db.pg_conn() as conn:
        mi.record_submitted(conn, items)


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
            if ok:
                _record_submitted([it])
        n_ok = sum(1 for r in records if r[7] == "成功")
        lines.append(f"  {name}:{label} 同步 PUT {len(items)},成功 {n_ok}")
        return records

    if kind == "delete":
        entries = [it["sku"] for it in items]
        feed_type = "DELETE_ITEM"
    elif kind == "title":
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
        # 删除是生死类:提交入病历(维护类不入,见模块 docstring)。
        # 只记 submitted——dedup 挂的是旧 feed_id 但什么都没提交,记了是幽灵事件
        if (kind == "delete" and res["outcome"] == "submitted"
                and res["feed_id"]):
            _record_deletes(name, batch, res["feed_id"])
        if res["outcome"] == "submitted":
            _record_submitted(batch)
        if res["outcome"] in ("submitted", "dedup") and res["feed_id"]:
            for it in batch:
                # 删除类的 C 列带原因(variant_offset / 商品不存在),便于
                # 事后按原因统计;其余三类沿用类型标签
                records.append((name, it["sku"], it.get("label") or label,
                                it["old"], it["new"],
                                res["feed_id"], today, "处理中", ""))
        elif res["outcome"] == "failed":
            for it in batch:
                records.append((name, it["sku"], it.get("label") or label,
                                it["old"], it["new"],
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
    """输入:params(execute/store/only/resync_sheet)→ 输出:维护提交摘要。"""
    if params.get("resync_sheet"):
        # 只补表,不算维护动作:提交成功但写表炸了之后的恢复路径
        return maint_sheet.resync_from_ledger()
    if params.get("prune_sheet"):
        return maint_sheet.prune(int(params.get("days", 0))
                                 or maint_sheet.RETAIN_DAYS)
    execute = bool(params.get("execute"))
    only = params.get("only")
    if only and only not in _KIND_ORDER:
        return f"only 参数只接受 delete/title/price/inventory,收到:{only}"

    stockzero = _load_stockzero()
    with db.pg_conn() as conn:
        intents = collect_intents(conn, stockzero,
                                  int(params.get("oos_days", 0) or 0))
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
    lines = [f"{mode}维护意图 {len(intents)} 条:删除 {n_kind['delete']},"
             f"标题 {n_kind['title']},价格 {n_kind['price']},"
             f"库存 {n_kind['inventory']}(stockzero 店 {len(stockzero)} 家)"]

    if not execute:
        # 删除不可逆:名单必须看得见,且要与其余三类分开说(混在一起会误读)
        dels = [i for i in intents if i["kind"] == "delete"]
        if dels:
            by, why = {}, {}
            for d in dels:
                by[d["store"]] = by.get(d["store"], 0) + 1
                r = d.get("reason") or "?"
                why[r] = why.get(r, 0) + 1
            lines.append(
                f"  ⚠ 永久删除 {len(dels)} 行(原因:"
                + ",".join(f"{r}×{n}" for r, n in sorted(why.items())) + "):"
                + ",".join(f"{s}×{n}" for s, n in sorted(by.items())))
            lines.append(f"    样本={[d['sku'] for d in dels[:8]]}"
                         + (" …" if len(dels) > 8 else ""))
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
        # 一天几千行,不裁飞书很快装不下(所有者定稿 2026-08-09:只留 7 天)。
        # 裁的只是展示面板,流水永久在 ops.feed_items
        try:
            lines.append(maint_sheet.prune())
        except Exception as e:            # 裁剪失败不影响本轮维护结果
            logger.warning("维护记录裁剪失败(不影响提交): %s", e)
    except LookupError as e:
        lines.append(f"⚠ 维护记录表未登记,流水未写表(台账已在 PG):{e}")
    except Exception as e:
        # 飞书只是展示面板:写表失败绝不能把"feed 已经提交出去了"这件事
        # 埋进一个异常里(所有者 2026-08-09 实遇:提交成功但表格一行没写,
        # 事后只能靠 ops.cursors 的时间戳反推)。台账在 PG,补写靠
        # -p resync_sheet=1。
        logger.exception("维护记录写表失败(feed 已提交,台账在 PG): %s", e)
        lines.append(f"⚠ 维护记录写表失败:{e}"
                     f"(feed 已提交,{len(all_records)} 行流水只在 PG;"
                     f"补写:python cli.py maintenance -p resync_sheet=1)")
    return "\n".join(lines)
