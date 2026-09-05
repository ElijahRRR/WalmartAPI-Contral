"""maintenance — 商品维护执行件(批次四拆分后;危险,缺省即真跑)。

用法:
  python cli.py maintenance                      # 真跑(提交 + 写维护记录表)
  python cli.py maintenance --dry-run            # 空跑:将执行哪些建议
  python cli.py maintenance -p store=A085朱丽霖
  python cli.py maintenance -p only=inventory    # 只执行某一类(title/price/inventory)
  python cli.py maintenance -p resync_sheet=1    # 只补维护记录表(写表炸过之后)
  python cli.py maintenance -p prune_sheet=1     # 只裁维护记录表(默认留 7 天)

**本工作流不再做任何决策**(批次四,所有者定稿 2026-08-16)。它只做四件事:
  ① 落定上一轮:维护三类按"线上值改过来了没有";
  ② 超期放行:3 天没等到 catalog_sync 复核的 executing 行放行(见下);
  ③ 领取 ops.dispositions 里 **action ∈ (title, price, inventory)** 且
     status='suggested' 的建议行;
  ④ 按 (店铺, 动作) 分桶提交,提交成功的转 executing 并落 feed_id。

⚠ **本工作流不再发任何删除 feed**(所有者定稿 2026-08-24)。破坏动作
(delete/retire)全部归 `problem_product_cleanup` —— 两个执行件都能发
DELETE_ITEM 的时候,配额、在途防重、病历口径各有一套,同一个 SKU 被两条链
先后删两次是生产实证过的。破坏面只留一个出口之后,这三样各只剩一处。
维护链照常**建议**删除(maintenance_scan 产 action='delete' 的行),只是
领取它的是问题链的执行件。

决策(查库 → 定性 → 该改什么)在 `maintenance_scan`;判据在
`services.maintenance_intents.classify()`。为什么拆:见 maintenance_scan 头注。
意图的六个来源(删除/清零/改价/改库存/改标题/跟卖铺货)与各自的取舍,
逐条写在 `services/maintenance_intents` 的模块头注与各 provider docstring 里 ——
拆分之后它们全部归扫描件,本文件不再复述(两处各写一份必然漂)。

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

容错走店级重试标准(所有者定稿 2026-08-26,展开 docs/conventions.md §四):
单店异常隔离 → 失败店跑完别人后**串行补试一遍**(`services/store_retry`,
凭证失效不补试)→ 仍失败不炸整轮、摘要**首行**点名缺席店与归类词。没提交
出去的建议行留在 suggested(claim 只读),下一轮照样领得到。补试跑的是同一个
_one_store,首轮已成功的部分会被在途防重(feed 路由)或绝对赋值(PUT 路由)
接住,终态不变 —— 两条路由的依据各不相同,写在 run() 里补试那一段。

去重与观测纪律(拆分后各归其位,一条没丢):
  · 决策期预筛(20 小时内已提交过同一件事)→ 归 maintenance_scan
    (services.maintenance_intents.drop_recent,生产实证 208 条 stale update);
  · **提交期权威防重** → api/feeds.submit_feed 的 ops.feed_log(返回
    outcome=dedup,本文件如实计数);
  · 建议行本身的防重 → ops.dispositions 的部分唯一索引 + claim() 只取 suggested;
  · 生效确认 → settle_maintenance()(标题/价格/库存,比对
    catalog.walmart_items 的现值)。删除类的落定归 problem_product_cleanup,
    本文件不再调 settle() —— 两个执行件都调会把同一批落定报两遍。

维护记录(registry.MAINT_SHEET,电子表格「维护记录」工作表,只追加,11 列):
  **本轮领到的每一条建议都写一行**,不只是提交成功的那些 —— 所有者要的是
  「建议」与「动作」两列的分歧:

    | 建议 | 动作 | 含义 |
    | 库存 | 库存 | 正常执行 |
    | 库存 | 跳过 | 在途防重命中 |
    | 库存 | (空) | 领到了但没执行(凭证缺失/提交异常),结果列写明原因 |

  feed 路径 feedid=真 feedid、结果=处理中(feed_poll 反哺器回填结果/报错);
  PUT 路径 feedid="sync"、结果=成功/失败 当场落定。

维护事件不进 catalog.product_events(所有者定稿 2026-08-07):流水在
ops.feed_log/feed_items,现状在 catalog.walmart_items,状态后果由
catalog_sync 观测入账。此前的唯一例外「删除恒记 delete_submitted」随删除
功能一并迁去 problem_product_cleanup —— 本文件已不产生生死类事件。

⚠ 切换纪律:上调度前必须停旧 12:00 walmart-maintenance-all-stores(AI 调度
任务,非 cron);停旧前先收干净旧系统在途 feed(见 legacy_survey 切换清单)。
"""

import logging
from datetime import datetime

from api import _client, feeds, inventory as inv_api, prices
from registry import db
from services import dispositions, kpi, maint_sheet, \
    maintenance_intents as mi, notify_fmt as nf, store_absence, \
    store_events, store_retry, stores as stores_svc

DANGEROUS = True
SUPPORTS_STORE = True   # 接受 -p store=X 单店范围(cli 链尾缺席店重赛靠它识别)

logger = logging.getLogger("workflows.maintenance")

_KIND_LABEL = mi.KIND_LABEL      # 唯一出处在 services(它是与扫描件的连接键)
# 单店内串行顺序(旧系统纪律;同店 token 桶互挤)。**删除不在其中**:
# 破坏动作归 problem_product_cleanup(见模块头注),本工作流领不到它。
# "要删的 SKU 不再花配额去改"这条压制也不在这里了 —— 它上移到
# services.dispositions.claim(),按库里所有未落定的破坏类建议压制,
# 而不是只看本轮自己算出来的那些。
_KIND_ORDER = dispositions.MAINT_ACTIONS


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
            feed_id, by="maintenance")


def _record(name: str, it: dict, action: str, feed_id, today: str,
            result: str, err) -> tuple:
    """输入:店铺 + 意图 + **实际动作** + 结果 → 输出:维护记录表的一行(11 列)。

    造行本身在 `services.maint_sheet.build_row`(唯一造行处,两个执行件共用);
    这里只负责把「意图」这套字段翻译过去 —— 「建议」取自建议行(扫描件定的),
    「动作」是本执行件真做了什么。
    """
    kind = it.get("kind", "")
    return maint_sheet.build_row(
        name, it["sku"], it.get("label") or _KIND_LABEL.get(kind, kind),
        it.get("reason", ""), action, feed_id, today, result, err,
        old=it.get("old"), new=it.get("new"))


def _submit_kind(store: dict, kind: str, items: list[dict], today: str,
                 lines: list[str], records: list[tuple], done: set,
                 cnt: dict | None = None) -> None:
    """输入:店铺 + 类型 + 意图 → **就地** append 维护记录行。路由 PUT/feed(显式 if)。

    ⚠ records/done/cnt 是传进来的、不是返回的:提交到一半抛异常时(单店代理
    断线),已经提交出去的那几片必须留下记录。首版是 `return records` ——
    异常一抛,局部列表连同"这几条其实发出去了"一起丢掉,外层再给它们补一行
    「未执行」,表里就写着未执行、沃尔玛队列里却真在跑。
    `cnt`(店铺事件账本用)同一条纪律,而且它还**跨补试累加** —— 补试是同一
    轮里的第二次动作,两次加起来才是"这家店这一轮改了多少处"。
    """
    name = store["name"]
    label = _KIND_LABEL[kind]
    bucket = cnt.setdefault(kind, {}) if cnt is not None else {}

    def _add(it: dict, action: str, feed_id, result: str, err="") -> None:
        records.append(_record(name, it, action, feed_id, today, result, err))
        done.add(it.get("disposition_id"))

    if kind != "title" and len(items) <= mi.SYNC_THRESHOLDS.get(kind, 0):
        n_ok = 0
        for it in items:    # 小批量:单品 PUT,结果当场已知
            if kind == "price":
                ok, why = prices.put_price(store, it["sku"], it["new"])
            else:
                # ship_node 由扫描件放进 detail(未配置「维护仓库」的店没有
                # 这个键)。带节点走 PUT /v3/inventories/{sku},不带走 legacy
                # —— **显式路由**,不是失败自动换端点重试
                ok, why = inv_api.put_inventory(store, it["sku"], it["new"],
                                                it.get("ship_node"))
            _add(it, label, "sync", "成功" if ok else "失败", why)
            if ok:
                _mark([it], "sync")
                n_ok += 1
        # PUT 路由的结局当场已知,记成 submitted/failed 两档(它不进 feed 台账,
        # 没有 dedup/unknown 两档 —— 强行凑四档会让账本看起来两条路由同构)
        bucket["submitted"] = bucket.get("submitted", 0) + n_ok
        bucket["failed"] = bucket.get("failed", 0) + len(items) - n_ok
        lines.append(f"  {name}:{label} 同步 PUT {len(items)},成功 {n_ok}")
        return

    if kind == "title":
        entries = [mi.build_title_item(it["sku"], it["product_type"],
                                       it["product_id"], it["new"])
                   for it in items]
        feed_type = "MP_MAINTENANCE"
    elif kind == "price":
        entries = [{"sku": it["sku"], "price": it["new"]} for it in items]
        feed_type = "price"
    elif any(it.get("ship_node") for it in items):
        # 受管仓的店:分节点批量库存走 MP_INVENTORY v1.5(v1.4 载荷里根本
        # 没有节点字段,发出去就是写到官方无定义的"默认节点")。
        # ⚠ 一店一个受管仓,所以同一店的这批要么全带节点、要么全不带;
        # 混着出现说明配置在本轮中途变了 —— 响亮失败,别挑着发一半
        missing = [it["sku"] for it in items if not it.get("ship_node")]
        if missing:
            raise RuntimeError(
                f"{name}:同一批库存意图里 {len(missing)} 条缺 ship_node "
                f"(如 {missing[:3]}),而其余带节点 —— 受管仓配置本轮中途变了?"
                f"本店本轮不提交,重跑 maintenance_scan 后再执行")
        entries = [{"sku": it["sku"], "qty": it["new"],
                    "ship_node": it["ship_node"]} for it in items]
        feed_type = "MP_INVENTORY"
    else:
        entries = [{"sku": it["sku"], "qty": it["new"]} for it in items]
        feed_type = "inventory"

    n = {"submitted": 0, "dedup": 0, "failed": 0, "unknown": 0}
    # 切片结果与意图的对位游标走 api/feeds 的公共件:submit_feed 每片只回
    # count 不回条目,手写 `batch = items[i:i+count]; i += count` 曾在 6 个
    # 工作流里各一份,错一位就是整批结局落到别人行上而且不报错
    for res, batch in feeds.iter_result_slices(
            feeds.submit_feed(store, feed_type, entries,
                              workflow="maintenance"), items):
        n[res["outcome"]] = n.get(res["outcome"], 0) + len(batch)
        if res["outcome"] == "submitted":
            # 只记 submitted——dedup 挂的是旧 feed_id 但什么都没提交
            _mark(batch, res["feed_id"])
            for it in batch:
                _add(it, label, res["feed_id"], "处理中")
        elif res["outcome"] == "dedup":
            # 动作写「跳过」而不是类型名:在途防重是**没有提交**,写成"库存"
            # 会让表格看起来做了两次。feedid 挂旧 feed 便于顺藤查回执
            for it in batch:
                _add(it, "跳过", res["feed_id"], "在途防重")
        elif res["outcome"] == "failed":
            for it in batch:
                _add(it, label, "", "提交被拒")
        else:                       # unknown:结局不确定,留 pending 待对账
            for it in batch:
                _add(it, label, res["feed_id"] or "", "处理中")
    # 四档计数的尾巴走 notify_fmt 的成品件(三个执行件逐字重复过);
    # failed 档字样由调用方给 —— 本件现行「提交被拒」,problem_product_cleanup
    # 现行「提交失败」,收口时逐字保留两处现状,不替所有者统一措辞
    for k, v in n.items():
        bucket[k] = bucket.get(k, 0) + v
    lines.append(f"  {name}:{label} feed "
                 + nf.feed_outcome_tail(n["submitted"], n["dedup"],
                                        n["failed"], n["unknown"],
                                        failed_word="提交被拒"))


def _settle(lines: list[str]) -> None:
    """上一轮落定 + 超期放行,结果写进摘要。任何失败只告警不阻断本轮提交。

    ⚠ **只落定维护三类**。删除/停用/反补的落定(dispositions.settle,按观测
    事件判)归 problem_product_cleanup —— 两个执行件都调会把同一批落定报两遍,
    人对账时会以为生效数翻倍。
    """
    with db.pg_conn() as conn:
        maint = dispositions.settle_maintenance(conn)   # 三类:按线上现值
        expired = dispositions.expire_executing(conn)
    ok = maint["confirmed"]
    bad = maint["ineffective"]
    if ok or bad:
        lines.append(f"上一轮落定:生效 {ok},**未生效 {bad}**"
                     f"(提交成功但 catalog_sync 复核时线上值没变,"
                     f"下轮 maintenance_scan 会重新建议)")
    if expired:
        lines.append(f"⚠ 超期放行 {expired} 条(超 {dispositions.EXPIRE_DAYS} 天"
                     f"没等到 catalog_sync 复核 —— 多半是商品已下架;"
                     f"不放行会把这些 SKU 的该类维护永久堵住)")


def _write_sheet(all_records: list[tuple], lines: list[str]) -> None:
    """维护记录写表 + 裁剪。收口在 services.maint_sheet.publish(两个执行件共用)。"""
    maint_sheet.publish(all_records, lines)


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
        return (f"only 参数只接受 {'/'.join(_KIND_ORDER)},收到:{only}"
                + ("(删除已迁去 problem_product_cleanup)"
                   if only == "delete" else ""))

    lines: list[str] = []
    if execute:
        _settle(lines)
    # 标题整路停闸(所有者 2026-09-05,唯一出处 mi.TITLE_SYNC):执行件也不领
    # 存量 title 建议 —— 只停生成侧的话,库里已有的 suggested 行下一轮照样被领走
    # 提交,停闸就只停了一半。留在 suggested 不撤:恢复后它们还在。
    actions = tuple(a for a in dispositions.MAINT_ACTIONS
                    if a != "title" or mi.TITLE_SYNC)
    with db.pg_conn() as conn:
        rows = dispositions.claim(conn, actions)
        # 压制必须见人:claim 少返回几行是静默的,不报的话摘要写着"没有待执行
        # 的维护建议",人会以为扫描件没算出东西来
        n_sup = dispositions.count_suppressed(conn, actions)
        n_title_held = (0 if mi.TITLE_SYNC
                        else dispositions.count_open_action(conn, "title"))
        # 缺席避让(店级重试标准③补全,2026-08-26 对抗校验):扫描件避让了
        # 缺席店、withdraw 还护住了它们的存量 suggested(cap 顺延/上轮提交
        # 异常留下的)—— 执行件不避让的话,同一轮链里这批按**隔夜现值**算的
        # 改价/清零照样被领走提交。缺席店的行留在 suggested 原地,等观测
        # 刷新后重新定夺。
        # ⚠ 探测失败**维持 fail-open**(不避让 = 加缺席避让之前的行为),
        # 与 problem_product_cleanup 的 fail-closed **方向相反,是有意的**:
        # 那边的最坏后果是按隔夜观测发 DELETE_ITEM(不可逆,且它是破坏动作
        # 的唯一出口),这边最坏是按隔夜现值改一次价/库存 —— 下一轮
        # catalog_sync 观测刷新后 maintenance_scan 会重新建议改回来,损失
        # 一轮配额而已;反过来把维护三类也停掉,一次飞书抖动就能让全船队
        # 的价格/库存整轮不更新,那个代价更大。
        absent, absence_note = store_absence.stale_or_note(
            conn, only=params.get("store"))
        if absence_note:
            lines.append(absence_note)
    intents = [mi.from_disposition(r) for r in rows]
    if absent:
        n_held = sum(1 for i in intents if i["store"] in absent)
        if n_held:
            intents = [i for i in intents if i["store"] not in absent]
            lines.append(f"⚠ 缺席避让:{n_held} 条建议属于缺席店(目录未刷新),"
                         f"留在 suggested 原地 —— 隔夜现值不配拿来改线上")
    if params.get("store"):
        intents = [i for i in intents if i["store"] == params["store"]]
    if only:
        intents = [i for i in intents if i["kind"] == only]

    mode = "" if execute else "🧪 [DRY-RUN] "
    if not mi.TITLE_SYNC:
        # 停闸必须天天见人(静默关闭 = 没人记得它关着)
        lines.append(f"  ⛔ 标题维护已整路停闸(所有者 2026-09-05):本轮不领 title 建议,"
                     f"库里 {n_title_held} 条 title 建议留在 suggested 不动;"
                     f"改价/改库存照常。恢复条件见 services/maintenance_intents.TITLE_SYNC")
    if not intents:
        return "\n".join(lines + [
            f"{mode}没有待执行的维护建议 —— 先跑 "
            f"`python cli.py maintenance_scan`"
            f"(catalog_sync → maintenance_scan → 本工作流,顺序是硬约束)"
            + (f";⚠ 另有 {n_sup} 条被压制(这些 SKU 挂着待执行的删除/停用,"
               f"要删的东西不再花配额去改;执行归 problem_product_cleanup)"
               if n_sup else "")])

    # 分桶件在 services.dispositions(两个执行件共用,2026-08-27 上移):
    # 「未知动作即抛」的宁炸不吞判据原样在里面(conventions §三的安全闸)
    by_store = dispositions.group_by_store(
        intents, key="kind", order=_KIND_ORDER, id_field="disposition_id")
    n_kind = {k: sum(1 for i in intents if i["kind"] == k) for k in _KIND_ORDER}
    # ⚠ 措辞随模式走(所有者 2026-08-17 实见):这一行在**提交之前**生成,
    # dry-run 说「待执行」是对的;真跑完再说「待执行」就是谎话——那些行此刻
    # 已经是 executing 了,而通知开头还写着 ✅ 成功,人会以为什么都没干。
    # 也不能改成「已执行」:领取的行里有一部分会撞上单店上限/在途防重/凭证
    # 缺失,并没有全部提交出去。准确的只有"本轮领取了多少",结果归后面几行。
    head = "待执行建议" if not execute else "本轮领取建议"
    lines.append(f"{mode}{head} {len(intents)} 条:标题 {n_kind['title']},"
                 f"价格 {n_kind['price']},库存 {n_kind['inventory']}"
                 + (f";因同 SKU 待删/待停用被压制 {n_sup} 条"
                    f"(留在建议表,删除没生效的话下轮还在)" if n_sup else ""))

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

    per_store: dict[str, list[str]] = {}    # 各店摘要行(补试重跑同店即覆盖)
    records_by_store: dict[str, list[tuple]] = {}   # 各店维护记录行(跨补试累加)
    done_by_store: dict[str, set] = {}      # 已写过记录的建议 id(同上,跨补试)
    cnt_by_store: dict[str, dict] = {}      # 各店动作计数(店铺事件账本,跨补试累加)

    def _one_store(store_name: str, kinds: dict) -> str:
        """输入:店铺名 + 该店三类意图 → 输出:店铺名;网络类失败**抛出**待补试。

        **各店各自的局部状态**,主线程再合并 —— 跨店并发之后不能再往共享
        list 上追加:追加本身在 GIL 下不会坏数据,但摘要的行序会按完成先后
        乱序交织,同一轮跑两次输出都不一样,没法对拍。

        ⚠ 记录行与 done 按店存在 records_by_store/done_by_store 里、**跨补试
        累加**(2026-08-27 接串行补试时定):局部列表随异常一起丢掉的话,首轮
        已经提交出去的那几片在表上一个字都没有(_submit_kind 头注的老坑);
        摘要行则每轮重置 —— 补试的叙述覆盖首轮,同一句话不报两遍。
        """
        lines_s: list[str] = []
        per_store[store_name] = lines_s     # 每轮重置:补试的叙述覆盖首轮
        records_s = records_by_store.setdefault(store_name, [])
        done = done_by_store.setdefault(store_name, set())
        cnt_s = cnt_by_store.setdefault(store_name, {})
        pending = [it for k in _KIND_ORDER for it in kinds.get(k) or []]

        def _unexecuted(why: str, err="") -> None:
            """领到了却没执行的建议**也要写表**(动作留空)。

            否则这些行在飞书完全不可见,看起来像"扫描件没建议它",
            而它其实每天都在建议、每天都没做成 —— 这正是所有者要「建议」
            与「动作」两列分开的原因。

            ⚠ 写过的记进 done:补试重跑同一家店时不再写第二遍「未执行」
            (同一条建议一轮一行);补试真提交成功的会照常再写一行真实动作
            —— 两行连起来正是这家店这一轮的经过。
            """
            fresh = [it for it in pending
                     if it.get("disposition_id") not in done]
            records_s.extend(_record(store_name, it, "", "", today, why, err)
                             for it in fresh)
            done.update(it.get("disposition_id") for it in fresh)
            # 领到了却没执行的也是这一轮的事实(账本上"领了 30 条、一条没动"
            # 与"这轮没它的货"是两回事,后者压根不落行)
            cnt_s["unexecuted"] = cnt_s.get("unexecuted", 0) + len(fresh)

        store = stores_by_name.get(store_name)
        if store is None:
            lines_s.append(f"  {store_name}:凭证缺失,跳过")
            _unexecuted("未执行(凭证缺失)")
            return store_name
        try:    # 单店隔离:单店代理/网络异常不炸整轮(与 cleanup 同款纪律)
            for kind in _KIND_ORDER:
                if kinds.get(kind):
                    _submit_kind(store, kind, kinds[kind], today, lines_s,
                                 records_s, done, cnt_s)
        except _client.StoreDeadError as e:
            # 凭证死是确定性的:就地点名写表,**不抛**(抛出去就进补试名单,
            # 而重试只会再死一次 —— 标准①的「StoreDeadError 不补试」)
            logger.error("店铺 %s 凭证失效,跳过(不重试): %s", store_name, e)
            lines_s.append(f"  {store_name}:凭证失效跳过")
            _unexecuted("未执行(凭证失效)", str(e))
        except Exception as e:
            logger.exception("店铺 %s 维护提交异常,跳过继续其它店: %s",
                             store_name, e)
            lines_s.append(f"  ⚠ {store_name}:提交异常已跳过"
                           f"({store_retry.diagnose(e)}:{e}),下轮重试")
            _unexecuted("未执行(提交异常)", str(e))
            raise       # 交给标准件串行补试(标准①,此刻不判生死)
        return store_name

    # 跨店并发(所有者定稿 2026-08-17:「跨店都应该并发,某个店当时有问题,
    # 最后补一次,一个店失败又不能说整个工作流都失败」)。安全性:每店有自己
    # 的固定出口代理,沃尔玛配额按 (store, endpoint) 计,令牌桶也是这个维度
    # —— 跨店并发不挤同一个桶。**店内三类动作仍然串行**(同店 token 桶互挤),
    # 那一层在 _submit_kind 里没动。
    # 单店失败隔离本来就有(上面的 try/except),并发只是让它们同时跑。
    from concurrent.futures import ThreadPoolExecutor, as_completed
    todo = sorted(by_store.items())
    kinds_by_store = dict(todo)
    to_retry: list[tuple] = []
    with ThreadPoolExecutor(
            max_workers=min(stores_svc.STORE_WORKERS, len(todo))) as pool:
        futs = {pool.submit(_one_store, n, k): n for n, k in todo}
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:      # noqa: BLE001 —— 归类与点名已在 _one_store
                # 标准①(所有者定稿 2026-08-26):此刻**不判生死**,跑完
                # 别人再串行补试。所有者原话「某个店当时有问题,最后补一次」
                # —— 此前本文件只把注释写在这儿,补一次的代码没有
                to_retry.append(({"name": futs[f]}, e))

    # ⚠ 这批店与上面「缺席避让」那批**不是同一批人**:那批是目录水位早于本轮
    # 的店(store_absence,本工作流作为下游要避让掉的);这批是本工作流自己
    # 这一轮没跑通、标准③要点名的店。同一个「缺席」词、两种人群,别混
    # (conventions §二 同款告诫),所以这里不复用 absent 这个名字。
    absent_after_retry: list[tuple[str, str]] = []   # (店名, 归类词,出处 diagnose)
    gate_note = ""
    saved: list = []
    if to_retry:
        # 标准①:失败店**串行**补试一遍(services/store_retry 是唯一实现,
        # 规模闸/退避阶梯/凭证死不补试三条判据都在里面,本文件不再另写)。
        # 单一落地路径 —— 补试跑的就是第一轮同一个 _one_store,不另写简化版。
        #
        # 写操作二次提交的安全性,**两条路由各自成立**(CLAUDE.md「写操作永不
        # 自动兜底」禁的是*换方法*重试;补试跑的是同一条路由、同一份载荷):
        #   · feed 路由:同一批意图重建的载荷 payload_key 一致,首轮已发出去
        #     的那几片被 api/feeds 的在途防重拦下(dedup 不重发、不落事件、
        #     不转态),与 problem_product_cleanup 的二轮重提同款;
        #   · PUT 路由(小批量价格/库存):**不进 feed 台账,没有在途防重**
        #     (api/prices.put_price 与 api/inventory.put_inventory 头注原话
        #     「不产生 feed_id 不进 feed 台账」)。它的安全性另有出处:两个
        #     端点都是**绝对赋值**(currentPrice.amount / quantity.amount),
        #     把同一个值重设一遍终态不变;转态侧也幂等(dispositions._MARK_SQL
        #     带 `AND status = 'suggested'`,重复 mark 不改 executed_at)。
        #     代价是首轮已成功的那几条会再花一次单品配额、并在维护记录表上
        #     多一行同样的「成功」——所以补试这件事必须进摘要(见下)。
        #     ⚠ 别为省这一次就在补试时按 done 跳过已成功的条目:feed 路由
        #     一旦少发几条,payload_key 就变了,在途防重当场失效。
        #
        # 救回的店不用在这里收:_one_store 已经把它这一轮的行写进 per_store、
        # 记录写进 records_by_store,补试成功与首轮成功走的是同一条落地路径
        saved, still, gate_note = store_retry.serial_second_pass(
            sorted(to_retry, key=lambda p: p[0]["name"]),
            lambda st: _one_store(st["name"], kinds_by_store[st["name"]]),
            total_stores=len(todo))
        for st, e in still:
            # 归类词唯一出处 diagnose。不像 catalog_sync 还分一路 dead:
            # 凭证失效到不了这里(_one_store 就地接住、不抛,见上)
            absent_after_retry.append((st["name"], store_retry.diagnose(e)))
    # 按店名排序合并:完成先后是随机的,摘要顺序不能跟着随机
    for name, _ in todo:
        lines.extend(per_store.get(name, []))
    if to_retry and not gate_note:
        # 补试必须见人(与 problem_product_cleanup 的「二轮重试 N 店(串行)」
        # 同款):摘要行每轮重置,补试救回的店把首轮那句「⚠ 提交异常已跳过」
        # 覆盖掉了,而维护记录表里首轮的「未执行(提交异常)」行**留着** ——
        # 摘要一个字不说,表上那几行就成了没出处的孤证,"这一轮花了两份配额"
        # 也没人看得见(conventions §六:兜底触发必须记日志计数,
        # 静默常态化 = 主路径已坏没人知道)。规模闸那行自己说清了「本轮不
        # 逐店补试」,不在它上面再造第二句
        lines.append(f"店级补试 {len(to_retry)} 店(串行):"
                     + ",".join(sorted(st["name"] for st, _e in to_retry))
                     + f",救回 {len(saved)},仍失败 {len(absent_after_retry)}")
    if absent_after_retry:
        # 标准③:缺席不炸整轮,但必须点名在**首行** —— 链通知对成功步骤
        # 只发首行(cli.first_line_of),写在后面等于只写进日志。
        # 尾句是本件的处置:领取只读(claim 不转态),没提交出去的建议行还是
        # suggested,下一轮照样领得到
        lines[0] += nf.absent_tail(absent_after_retry, gate_note,
                                   tail="存量建议保留,下轮重试")
    if gate_note:
        lines.append(gate_note)

    # 店铺事件账本(运营类):每店每轮一条,按动作类分桶(标题/价格/库存 ×
    # 四档结局 + 领到没执行的)。补试跑的是同一个 _one_store,计数跨补试累加
    # —— 一轮就是一轮,不因为补试记成两条。记账失败只告警,不拖垮维护链
    store_events.record_round_safe("maintenance", store_events.MAINT_ROUND,
                                   cnt_by_store, lines)

    all_records = [r for name, _ in todo for r in records_by_store.get(name, [])]
    _write_sheet(all_records, lines)
    lines.append("生效确认在下一轮本工作流开头(等 catalog_sync 重新观测)")
    return "\n".join(lines)
