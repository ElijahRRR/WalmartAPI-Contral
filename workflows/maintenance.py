"""maintenance — 商品维护执行件(批次四拆分后;危险,缺省即真跑)。

用法:
  python cli.py maintenance                      # 真跑(提交 + 写维护记录表)
  python cli.py maintenance --dry-run            # 空跑:将执行哪些建议
  python cli.py maintenance -p store=A085朱丽霖
  python cli.py maintenance -p only=inventory    # 只执行某一类(delete/title/price/inventory)
  python cli.py maintenance -p resync_sheet=1    # 只补维护记录表(写表炸过之后)
  python cli.py maintenance -p prune_sheet=1     # 只裁维护记录表(默认留 7 天)

**本工作流不再做任何决策**(批次四,所有者定稿 2026-08-16)。它只做四件事:
  ① 落定上一轮:删除类按观测事件、维护三类按"线上值改过来了没有";
  ② 超期放行:3 天没等到 catalog_sync 复核的 executing 行放行(见下);
  ③ 领取 ops.dispositions 里 source='maint' 且 status='suggested' 的建议行;
  ④ 按 (店铺, 动作) 分桶提交,提交成功的转 executing 并落 feed_id。

决策(查库 → 定性 → 该改什么)在 `maintenance_scan`;判据在
`services.maintenance_intents.classify()`。为什么拆:见 maintenance_scan 头注。

⚠ **调度顺序是硬约束**:catalog_sync → product_refresh → maintenance_scan →
本工作流。没跑 scan 就跑本工作流 = 消费上一轮的陈旧建议(或者什么都没有)。

⚠ **超期放行不是洁癖**:ops.dispositions 的部分唯一索引只允许同
(店铺,SKU,动作) 有一条未落定行。executing 行永远不落定 = 那个 SKU 的那类维护
**永久停摆且完全静默**(扫描件照常算出意图,upsert 撞索引写不进去,rowcount 0)。
等不到观测的常见原因:商品下架了(walmart_items 缺席,JOIN 不上)、
catalog_sync 连着几轮没扫到那家店。

路由(旧系统实证,原样保留):单店 改价 ≤5 / 改库存 ≤10 走单品 PUT(结果当场
已知),超过走 feed;标题无同步接口永远 MP_MAINTENANCE feed。单店内
删除→标题→价格→库存 串行(同店 token 桶互挤);跨店顺序执行,单店异常隔离
不炸整轮。

去重与观测纪律(拆分后各归其位,一条没丢):
  · 决策期预筛(20 小时内已提交过同一件事)→ 归 maintenance_scan
    (services.maintenance_intents.drop_recent,生产实证 208 条 stale update);
  · **提交期权威防重** → api/feeds.submit_feed 的 ops.feed_log(返回
    outcome=dedup,本文件如实计数);
  · 建议行本身的防重 → ops.dispositions 的部分唯一索引 + claim() 只取 suggested;
  · 生效确认 → services.dispositions.settle()(删除,读 catalog_sync 落的
    delete_verified/delete_not_effective)与 settle_maintenance()(标题/价格/
    库存,比对 catalog.walmart_items 的现值)。

维护记录(registry.MAINT_SHEET,电子表格「维护记录」工作表,只追加,11 列):
  **本轮领到的每一条建议都写一行**,不只是提交成功的那些 —— 所有者要的是
  「建议」与「动作」两列的分歧:

    | 建议 | 动作 | 含义 |
    | 删除 | 删除 | 正常执行 |
    | 删除 | 跳过 | 在途防重命中 |
    | 删除 | (空) | 领到了但没执行(凭证缺失/提交异常),结果列写明原因 |

  feed 路径 feedid=真 feedid、结果=处理中(feed_poll 反哺器回填结果/报错);
  PUT 路径 feedid="sync"、结果=成功/失败 当场落定。

维护事件不进 catalog.product_events(所有者定稿 2026-08-07):流水在
ops.feed_log/feed_items,现状在 catalog.walmart_items,状态后果由
catalog_sync 观测入账。**唯一例外是删除**——生死类事件恒记
(delete_submitted,source=maintenance,detail.reason 记删除原因码)。

⚠ 切换纪律:上调度前必须停旧 12:00 walmart-maintenance-all-stores(AI 调度
任务,非 cron);停旧前先收干净旧系统在途 feed(见 legacy_survey 切换清单)。
"""

import logging
from datetime import datetime

from api import _client, feeds, inventory as inv_api, prices
from registry import db
from services import dispositions, kpi, maint_sheet, \
    maintenance_intents as mi, product_events, stores as stores_svc

DANGEROUS = True

logger = logging.getLogger("workflows.maintenance")

_KIND_LABEL = {"delete": "删除", "title": "标题",
               "price": "价格", "inventory": "库存"}
# 单店内串行顺序(旧系统纪律);**删除排最前**:同一 SKU 既要删又要改价时,
# 先删掉就不必再为它烧改价/改库存的 feed 配额(maintenance_scan 的
# collect_all 已把删除名单从其余三类里剔掉,这里的顺序是双保险)
_KIND_ORDER = ("delete", "title", "price", "inventory")


def group_by_store(intents: list[dict]) -> dict[str, dict]:
    """输入:意图列表 → 输出:{店铺: {动作: [意图]}}。纯函数,可测。"""
    out: dict[str, dict] = {}
    for it in intents:
        bucket = out.setdefault(it["store"], {k: [] for k in _KIND_ORDER})
        if it["kind"] in bucket:
            bucket[it["kind"]].append(it)
        else:               # 建议表里出现了不认识的动作:宁炸不吞
            raise ValueError(f"未知 kind={it['kind']!r}"
                             f"(建议行 id={it.get('disposition_id')})")
    return out


def _record_deletes(store: str, rows: list[dict], feed_id) -> None:
    """删除提交入病历(catalog.product_events;回执由 feed_track 另记)。"""
    with db.pg_conn() as conn:
        product_events.record_many(conn, [
            {"sku": r["sku"], "store": store, "event": product_events.DELETE_SUBMITTED,
             "source": "maintenance",
             "detail": {"feed_id": feed_id,
                        "reason": r.get("code") or "variant_offset",
                        "disposition_id": r.get("disposition_id"),
                        "batches": r.get("batches"),
                        "first_seen": r.get("first_seen"),
                        "last_seen": r.get("last_seen")}}
            for r in rows])


def _mark(items: list[dict], feed_id) -> None:
    """提交成功的意图:建议行转 executing + 落 ops.dedupe(供下轮 drop_recent)。

    两件事绑在一起,因为它们的触发条件必须完全一致 —— 只有 outcome=submitted
    才做。dedup 携带的是**旧 feed_id 但什么都没提交**,记了它就是幽灵:建议行
    会卡在 executing 等一个不存在的提交的判决。
    """
    with db.pg_conn() as conn:
        mi.record_submitted(conn, items)
        dispositions.mark_executing(
            conn, [i["disposition_id"] for i in items if i.get("disposition_id")],
            feed_id)


def _record(name: str, it: dict, action: str, feed_id, today: str,
            result: str, err) -> tuple:
    """输入:店铺 + 意图 + **实际动作** + 结果 → 输出:维护记录表的一行(11 列)。

    列序 = registry.MAINT_SHEET.columns:
      店铺 SKU 建议 原因 动作 旧值 新值 feedid 日期 结果 报错

    **唯一造行处**。散在各分支手拼元组的话,漏改一处就整行错位且不报错。

    「建议」取自建议行(scan 定的),「动作」是本执行件真做了什么 —— 两者分歧
    才是这两列的价值:建议删除但动作为空 = 领到了没执行,结果列写明为什么。
    """
    kind = it.get("kind", "")
    suggestion = it.get("label") or _KIND_LABEL.get(kind, kind)
    return (name, it["sku"], suggestion, it.get("reason", ""), action,
            it.get("old"), it.get("new"), feed_id, today, result,
            err if err is not None else "")


def _submit_kind(store: dict, kind: str, items: list[dict], today: str,
                 lines: list[str], records: list[tuple], done: set) -> None:
    """输入:店铺 + 类型 + 意图 → **就地** append 维护记录行。路由 PUT/feed(显式 if)。

    ⚠ records/done 是传进来的、不是返回的:提交到一半抛异常时(单店代理断线),
    已经提交出去的那几片必须留下记录。首版是 `return records` —— 异常一抛,
    局部列表连同"这几条其实发出去了"一起丢掉,外层再给它们补一行「未执行」,
    表里就写着未执行、沃尔玛队列里却真在跑。
    """
    name = store["name"]
    label = _KIND_LABEL[kind]

    def _add(it: dict, action: str, feed_id, result: str, err="") -> None:
        records.append(_record(name, it, action, feed_id, today, result, err))
        done.add(it.get("disposition_id"))

    if kind != "title" and len(items) <= mi.SYNC_THRESHOLDS.get(kind, 0):
        n_ok = 0
        for it in items:    # 小批量:单品 PUT,结果当场已知
            if kind == "price":
                ok, why = prices.put_price(store, it["sku"], it["new"])
            else:
                ok, why = inv_api.put_inventory(store, it["sku"], it["new"])
            _add(it, label, "sync", "成功" if ok else "失败", why)
            if ok:
                _mark([it], "sync")
                n_ok += 1
        lines.append(f"  {name}:{label} 同步 PUT {len(items)},成功 {n_ok}")
        return

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
        if res["outcome"] == "submitted":
            # 删除是生死类:提交入病历(维护类不入,见模块 docstring)。
            # 只记 submitted——dedup 挂的是旧 feed_id 但什么都没提交
            if kind == "delete" and res["feed_id"]:
                _record_deletes(name, batch, res["feed_id"])
            _mark(batch, res["feed_id"])
            for it in batch:
                _add(it, label, res["feed_id"], "处理中")
        elif res["outcome"] == "dedup":
            # 动作写「跳过」而不是类型名:在途防重是**没有提交**,写成"删除"
            # 会让表格看起来做了两次删除。feedid 挂旧 feed 便于顺藤查回执
            for it in batch:
                _add(it, "跳过", res["feed_id"], "在途防重")
        elif res["outcome"] == "failed":
            for it in batch:
                _add(it, label, "", "提交被拒")
        else:                       # unknown:结局不确定,留 pending 待对账
            for it in batch:
                _add(it, label, res["feed_id"] or "", "处理中")
    line = f"  {name}:{label} feed 提交 {n['submitted']}"
    if n["dedup"]:
        line += f",在途防重跳过 {n['dedup']}"
    if n["failed"]:
        line += f",⚠ 提交被拒 {n['failed']}(查日志)"
    if n["unknown"]:
        line += f",⚠ 结局不确定留 pending {n['unknown']}(待对账)"
    lines.append(line)


def _settle(lines: list[str]) -> None:
    """上一轮落定 + 超期放行,结果写进摘要。任何失败只告警不阻断本轮提交。"""
    with db.pg_conn() as conn:
        dele = dispositions.settle(conn)            # 删除:按观测事件
        maint = dispositions.settle_maintenance(conn)   # 三类:按线上现值
        expired = dispositions.expire_executing(conn)
    ok = dele["confirmed"] + maint["confirmed"]
    bad = dele["ineffective"] + maint["ineffective"]
    if ok or bad:
        lines.append(f"上一轮落定:生效 {ok},**未生效 {bad}**"
                     f"(提交成功但 catalog_sync 复核时线上值没变,"
                     f"下轮 maintenance_scan 会重新建议)")
    if expired:
        lines.append(f"⚠ 超期放行 {expired} 条(超 {dispositions.EXPIRE_DAYS} 天"
                     f"没等到 catalog_sync 复核 —— 多半是商品已下架;"
                     f"不放行会把这些 SKU 的该类维护永久堵住)")


def _write_sheet(all_records: list[tuple], lines: list[str]) -> None:
    """维护记录写表 + 裁剪。**写表失败绝不能把"feed 已提交"埋进异常里**。"""
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


def run(params: dict) -> str:
    """输入:params(execute/store/only/resync_sheet)→ 输出:落定 + 提交摘要。"""
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

    lines: list[str] = []
    if execute:
        _settle(lines)
    with db.pg_conn() as conn:
        rows = dispositions.claim(conn, dispositions.MAINT_SOURCES)
    intents = [mi.from_disposition(r) for r in rows]
    if params.get("store"):
        intents = [i for i in intents if i["store"] == params["store"]]
    if only:
        intents = [i for i in intents if i["kind"] == only]

    mode = "" if execute else "🧪 [DRY-RUN] "
    if not intents:
        return "\n".join(lines + [
            f"{mode}没有待执行的维护建议 —— 先跑 "
            f"`python cli.py maintenance_scan`"
            f"(catalog_sync → maintenance_scan → 本工作流,顺序是硬约束)"])

    by_store = group_by_store(intents)
    n_kind = {k: sum(1 for i in intents if i["kind"] == k) for k in _KIND_ORDER}
    lines.append(f"{mode}待执行建议 {len(intents)} 条:删除 {n_kind['delete']},"
                 f"标题 {n_kind['title']},价格 {n_kind['price']},"
                 f"库存 {n_kind['inventory']}")

    if not execute:
        # 执行件的 dry-run 只回答"要提交什么、走哪条路由"。
        # "为什么是这些商品"归 maintenance_scan —— 决策不在这里,别在这里解释
        for store_name, kinds in sorted(by_store.items()):
            for kind in _KIND_ORDER:
                items = kinds.get(kind)
                if not items:
                    continue
                route = ("feed" if kind == "title"
                         or len(items) > mi.SYNC_THRESHOLDS.get(kind, 0)
                         else "PUT")
                sample = [(it["sku"], f"{it.get('old')}→{it.get('new')}")
                          for it in items[:5]]
                lines.append(f"  {store_name}:{_KIND_LABEL[kind]} {len(items)} 条"
                             f"(路由 {route}),样本={sample}"
                             + (" …" if len(items) > 5 else ""))
        lines.append("(dry-run:未提交任何 feed;确认无误去掉 --dry-run;"
                     "破坏面明细看 `python cli.py maintenance_scan -p preview=1`)")
        return "\n".join(lines)

    stores_by_name = {s["name"]: s for s in stores_svc.load_stores()}
    today = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d")
    all_records: list[tuple] = []
    for store_name, kinds in sorted(by_store.items()):
        store = stores_by_name.get(store_name)
        pending = [it for k in _KIND_ORDER for it in kinds.get(k) or []]
        done: set = set()

        def _unexecuted(why: str, err="") -> None:
            """领到了却没执行的建议**也要写表**(动作留空)。

            否则这些行在飞书完全不可见,看起来像"扫描件没建议它",
            而它其实每天都在建议、每天都没做成 —— 这正是所有者要「建议」
            与「动作」两列分开的原因。
            """
            all_records.extend(_record(store_name, it, "", "", today, why, err)
                               for it in pending
                               if it.get("disposition_id") not in done)

        if store is None:
            lines.append(f"  {store_name}:凭证缺失,跳过")
            _unexecuted("未执行(凭证缺失)")
            continue
        try:    # 单店隔离:单店代理/网络异常不炸整轮(与 cleanup 同款纪律)
            for kind in _KIND_ORDER:
                if kinds.get(kind):
                    _submit_kind(store, kind, kinds[kind], today, lines,
                                 all_records, done)
        except _client.StoreDeadError as e:
            logger.error("店铺 %s 凭证失效,跳过(不重试): %s", store_name, e)
            lines.append(f"  {store_name}:凭证失效跳过")
            _unexecuted("未执行(凭证失效)", str(e))
        except Exception as e:
            logger.exception("店铺 %s 维护提交异常,跳过继续其它店: %s",
                             store_name, e)
            lines.append(f"  ⚠ {store_name}:提交异常已跳过({e}),下轮重试")
            _unexecuted("未执行(提交异常)", str(e))

    _write_sheet(all_records, lines)
    lines.append("生效确认在下一轮本工作流开头(等 catalog_sync 重新观测)")
    return "\n".join(lines)
