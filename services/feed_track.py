"""feed 生命周期追踪积木(所有 feed 操作共用的轮询机制)。

分工(2026-08-06 定稿):
  api/feeds     负责 提交 + feed_log/feed_items 的**落台账**(提交时)
  本模块        负责 轮询 submitted feed → SKU 级终态回写 ops.feed_items
                + feed_log 落 done/failed + pending 行对账告警
  workflows     feed_poll 薄壳全局轮询;各业务工作流(daily_retire 等)
                用 poll_feed 拿 {sku: 结果} 去刷各自的飞书投影列

SKU 级状态权威在 ops.feed_items;停用/删除/设置到期日期/未来的上架、改价、
改库存、改标题 feed 全走这一套,不许各工作流自造轮询。
"""

import logging
from datetime import datetime, timezone

from api import feeds
from registry import db, resources
from services import blacklist, product_events, stores as stores_svc

logger = logging.getLogger("services.feed_track")

# feedType → 业务动作名(摘要展示;未登记的原样显示)。
# ⚠ 与 api/feeds._SLICE_LIMITS 的八个 feedType 对齐:漏登记不报错,只是摘要里
#   蹦出一个裸 feedType(2026-09-11 实见「A171罗尹鸿 MP_INVENTORY(maintenance)」
#   ——运营看不出那是分仓库存)。
# MP_ITEM_MATCH 一个 feedType 挂着两条链(跟卖 match_listing / 改码 sku_migrate),
# 摘要里跟在后面的 `(workflow)` 才分得开,故标"跟卖/改码"。
_FEED_LABEL = {"DELETE_ITEM": "删除", "RETIRE_ITEM": "停用",
               "MP_MAINTENANCE": "维护", "MP_ITEM": "上架",
               "PRICE_AND_PROMOTION": "改价", "price": "改价",
               "inventory": "改库存", "MP_INVENTORY": "分仓库存",
               "MP_ITEM_MATCH": "跟卖/改码"}

#: 在途 feed 的**静默闸**(小时,唯一出处):提交超过它还没落定的 feed,摘要
#: 不再逐条复读明细,折成一行点名(几个、卡多久、怎么查)。
#: 判据是**年龄**,不是"看着眼熟":feed_poll 挂 0/30 分两班,一段明细一天原样
#: 发 48 遍(2026-09-11 所有者实见「这几条每次都通知,似乎是固定文案」),
#: 而人对固定文案的反应是不看——真出事的那一轮也一起漏掉。
#: ⚠ 它只管**摘要排版**:在途行照旧每轮轮询、照旧不落定,一个业务判断都不改。
#: 它**不是放弃期限**——在途 feed 等多久才该判死、判死之后防重闸开不开,
#: 是所有者要拍的板(docs/feed_closure_audit.md §三.4)。
FEED_QUIET_HOURS = 2.0

#: 折叠行里最多点几个名字(其余给 SQL 自己查:名字越多越没人看)
_FOLD_NAMES = 3

# SKU 台账状态 → 飞书表结果列文案。状态词只有一个出处(api/feeds.sku_outcome
# 的 success/failed/processing/unknown,加台账自己的 submitted/missing),中文面
# 此前有四份拷贝:clear_sheet:27 / maint_sheet:221 / maint_sheet:343(同一文件
# 里又内联一份)/ match_sheet:104。本常量是四份的**并集**——processing/unknown
# 两键只有 clear_sheet 那份有,而 product_clear 是 `RESULT_TEXT[outcome]` 直接
# 下标取(不是 .get),少一键就是 KeyError,不许"看着重复"就删。
RESULT_TEXT = {"success": "成功", "failed": "失败", "missing": "未查到",
               "submitted": "处理中", "processing": "处理中",
               "unknown": "处理中"}


def text_of(status: str, err: str = "") -> str:
    """输入:台账状态(+可选「码 | 人话」报错)→ 输出:飞书表结果列文案。

    未登记的状态一律按未落定报「处理中」:不装成功也不装失败,下轮反哺器
    还会再看一次(与 api/feeds.sku_outcome 对未知枚举的态度同口径)。
    `err` 只在 failed 档拼进去,形状照跟卖表现行的「失败:{报错}」;其余档
    给了 err 也不拼——成功/未查到后面挂一串报错码没有意义。

    ⚠ maint_sheet 补写口径(:262)是 `.get(status, status)`:未登记状态**原样
    落表**,给人看的是"台账里到底写了什么"。那一处与本函数的「处理中」不是
    等价替换,接线时(P1c)要么保留原样落表、要么请所有者拍一次口径,别当
    成同一件事悄悄改掉。
    """
    if err and status == "failed":
        return f"失败:{err}"
    return RESULT_TEXT.get(status, "处理中")


def ingestion_errors(node: dict) -> list[dict]:
    """输入:feed 汇总 head **或**逐条明细 item → 输出:ingestionError 列表。

    两种形态都认:`{"ingestionErrors": {"ingestionError": [...]}}` 与**裸 list**
    ——旧仓两种都实见过(docs/legacy_survey.md「ingestionErrors 有两种形态」,
    `poll_yesterday._first_error` 专门兼容过),按 dict 一种形态硬取的表现是
    裸 list 那一份**静默读成空**:报错原文丢了,而回执照样落定、没有任何告警。
    feed 级(head)与 SKU 级(item)共用这一份解析,不写第二份。
    """
    node = (node or {}).get("ingestionErrors")
    if isinstance(node, dict):
        return node.get("ingestionError") or []
    return list(node or [])


def error_text(errs: list[dict]) -> str:
    """输入:ingestionError 列表 → 输出:人话描述串(带字段名,多条以 ; 连接)。

    只存数字错误码无法诊断(2026-08-09 上架首跑教训:EXT_DATA_ERROR_507165…
    这种码本身不含任何信息),description/field 才是能修的线索。
    """
    parts = []
    for e in errs or []:
        desc = str(e.get("description") or e.get("message") or "").strip()
        field = str(e.get("field") or "").strip()
        if field and desc:
            parts.append(f"[{field}] {desc}")
        elif desc or field:
            parts.append(desc or f"[{field}]")
    return "; ".join(parts)[:900]


def _save_errors(cur, feed_id: str, store: str, all_errs: dict[str, list[dict]],
                 meta: dict) -> int:
    """输入:游标 + feed + 每 SKU 报错列表 → 输出:落账条数(幂等重入不重复)。

    一条 ingestionError 一行进 ops.feed_item_errors——**拉详情是标准动作**:
    报错是系统自我优化的燃料,聚合看 ops.v_feed_error_stats。
    """
    rows = []
    for sku, errs in all_errs.items():
        wf, ft = (meta.get(sku) or ("", ""))[:2]
        for i, e in enumerate(errs or []):
            rows.append((feed_id, sku, i,
                         str(e.get("type") or "") or None,
                         str(e.get("code") or "") or None,
                         str(e.get("field") or "") or None,
                         str(e.get("description") or e.get("message") or "")[:2000]
                         or None,
                         ft or None, store, wf or None))
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO ops.feed_item_errors (feed_id, sku, seq, error_type, code,"
        " field, description, feed_type, store, workflow)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (feed_id, sku, seq) DO NOTHING", rows)
    return len(rows)


def _progress(head: dict) -> str:
    """输入:feed 汇总 → 输出:进度串(feed 级 GET 自带计数,零明细翻页)。"""
    recv = head.get("itemsReceived") or 0
    ok = head.get("itemsSucceeded") or 0
    bad = head.get("itemsFailed") or 0
    pending = head.get("itemsProcessing")
    if pending is None:
        pending = max(recv - ok - bad, 0)
    return f"已收 {recv},成功 {ok},失败 {bad},待处理 {pending}"


def unresolved(results: dict) -> tuple[int, int]:
    """输入:poll_feed 的 {sku: (结局, 码)} → 输出:(未落定 SKU 数, 其中状态未知的数)。

    **「这个 feed 能不能收工」的唯一判据**:`poll_feed` 拿它决定调不调
    `mark_feed_done`,`poll_all` 拿它决定摘要说不说"已落定"。两处各数一遍的
    表现是摘要写「已落定 PROCESSED,成功 451,失败 42」而 feed_log 仍是
    submitted(轮询那边看见残留、没收工),于是**下一轮原样再播一遍,永远**,
    `落定 N` 还把同一个 feed 每轮重数一次 —— 每句话都对,合起来是假的
    (2026-09-11 所有者实见「这几条每次都通知,似乎是固定文案」)。

    第二个数只为摘要**分因**:残留是 processing(沃尔玛还在跑,等就行)还是
    unknown(`sku_outcome` 没认出来的枚举值,等到天荒地老也不会变,得补码表)
    —— 两者都卡住 feed 而处置完全不同;摘要不说,就只能去翻日志里那句
    「未知 SKU ingestionStatus=…」。
    """
    open_ = [o for o, _ in (results or {}).values()
             if o in ("processing", "unknown")]
    return len(open_), sum(1 for o in open_ if o == "unknown")


def age_hours(since) -> float | None:
    """输入:提交时刻 → 输出:至今几小时;拿不到时刻(None/非时间)给 None。

    在途年龄取 `ops.feed_log.updated_at`(这个 feedId 落 submitted 的时刻),
    **不是 created_at** —— 理由见 `api/feeds.query_pending` 的注释。
    """
    if not isinstance(since, datetime):
        return None
    if since.tzinfo is None:            # 裸时间按 UTC 读(库里存的是 timestamptz)
        since = since.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - since).total_seconds() / 3600, 0.0)


def is_stuck(age_h: float | None) -> bool:
    """输入:在途年龄(小时)→ 输出:是否老到不该再逐条复读(见 FEED_QUIET_HOURS)。

    年龄拿不到(None)一律当**新鲜**:折叠的语义是"这条别再播了",拿不确定的
    年龄去折,会把刚提交的 feed 从摘要里抹掉 —— 宁可多播一行,不可少播一行。

    摘要折叠(`poll_all`)与清单点名(`feed_poll -p stuck=1`)共用这一处口径:
    两处各写一个阈值的表现是通知里折掉了、清单里却不认为它卡住。
    """
    return age_h is not None and age_h >= FEED_QUIET_HOURS


def _who(rec: dict) -> str:
    """输入:在途 feed 记录 → 输出:摘要里的"谁"(店铺 动作(工作流) feed 头)。"""
    return f"{rec['store']} {rec['label']}({rec['workflow']}) {rec['fid']}"


#: 破坏类 feed 的两个类型 —— **唯一出处**(2026-09-09 归一)。
#: 它是 `dispositions.DESTRUCTIVE_ACTIONS`(delete/retire)的 feed 面,
#: workflows/problem_scan 的在途分档与下面的 `receipt_blocked` 共用这一份:
#: 各写一份的表现是有人加了第三种破坏 feed,另一处静默漏判(不报错)。
DESTRUCTIVE_FEED_TYPES = ("DELETE_ITEM", "RETIRE_ITEM")

#: 「这个 (店, SKU) **最近一次**破坏类尝试的回执码命中给定码集了吗」。
#: 三个形状上的决定,每个都有理由:
#:   ① **DISTINCT ON 最近一次**,不是 EXISTS(历史上出现过就永久拉黑):WFS 件
#:      转出 WFS 之后就该能删了,下一次尝试的回执会把它自己放出来。写成 EXISTS
#:      的话它永远删不了,而且没人看得出是被自己的闸拦着。
#:   ② **不限定 status**(2026-09-09 放宽):死档码里的 QARTH「No matching
#:      record found for the SKU」那一条是 `status=success` 带回来的,只收
#:      failed/missing 就永远收不到那几百条(backlog §十三 A085 611 条)。
#:      码集本身是判据,status 不是。
#:   ③ **DELETE_ITEM 与 RETIRE_ITEM 都算**:两种破坏动作对同一个死档 SKU 得到
#:      的是同一句话,只看删不看停会让顽固件的 retire 那一半继续每天空烧配额。
#: 别名一律经 `catalog.sku_aliases` 继承**一跳**(代际继承的唯一出处):改码后
#: 新码在 ops.feed_items 里一条历史都没有,不继承这道闸对它整个失明。
#: ⚠ 先取最近一次、**再**比码集(不是在内层就按码过滤):内层过滤会把"最近一次
#: 其实成功了"的行跳过去、拿更早那次失败当结论 —— 闸永远放不开人。
_RECEIPT_BLOCKED_SQL = """
SELECT store, sku FROM (
    SELECT DISTINCT ON (store, sku) store, sku, error_code FROM (
        SELECT f.store, f.sku, f.error_code, f.submitted_at
        FROM ops.feed_items f
        WHERE f.feed_type = ANY(%(feeds)s::text[])
          AND (%(store)s::text IS NULL OR f.store = %(store)s::text)
        UNION ALL
        SELECT a.store, a.sku, f.error_code, f.submitted_at
        FROM catalog.sku_aliases a
        JOIN ops.feed_items f
          ON f.store = a.store AND f.sku = a.alias_sku
        WHERE f.feed_type = ANY(%(feeds)s::text[])
          AND (%(store)s::text IS NULL OR a.store = %(store)s::text)
    ) u ORDER BY store, sku, submitted_at DESC
) t WHERE t.error_code = ANY(%(codes)s::text[])
"""


def receipt_blocked(conn, codes, store: str | None = None
                    ) -> set[tuple[str, str]]:
    """输入:连接 + 错误码集合(+ 限定店铺)→ 输出:{(店铺, SKU)} —— 最近一次
    破坏类尝试的回执码落在该集合里的行。只读。

    **两个消费方共用这一份 SQL**(2026-09-09 上移到 services):
      · workflows/problem_scan —— 死档 / 永久拒的行不再每天重建议、重发;
      · workflows/sku_migrate  —— 死档的旧码不改码(发 MP_ITEM_MATCH 会**新建**
        一条 listing,不是改码,A131吕灿荣 B09L3WXJ96 真双挂实证)。
    码集由调用方从 `registry.resources` 传进来(`WALMART_ERR_ITEM_GONE` /
    `WALMART_ERR_DESTRUCTIVE_PERMANENT`),本函数**不认识任何具体的码** ——
    清单只在 registry 出生一次(铁律 3)。

    ⚠ 判据是「**最近一次**尝试的回执」,**没有时间窗**:下一次尝试若回执变了
    (人工把件转出 WFS 之后重删成功),它自然就从这个集合里出去了。
    """
    codes = sorted(set(codes))
    if not codes:
        return set()
    with conn.cursor() as cur:
        cur.execute(_RECEIPT_BLOCKED_SQL,
                    {"feeds": list(DESTRUCTIVE_FEED_TYPES), "codes": codes,
                     "store": store})
        return {(st, sk) for st, sk in cur.fetchall()}


def poll_feed(store: dict, feed_id: str) -> tuple[dict, dict | None]:
    """输入:店铺 + feed_id → 输出:(feed 汇总 head, SKU 结果)。

    未终态:结果为 None(head 自带 itemsReceived/Succeeded/Failed 进度计数,
    不翻明细);终态:ops.feed_items 逐 SKU 落 success/failed(+错误码),
    台账里有而明细里查无的 SKU 落 missing;feed_log 落 done/failed。

    **例外:feed 级拒收(终态 ERROR + 一条明细都没有)⇒ 台账逐 SKU 落 failed**,
    回执用 head 里 feed 级 `ingestionErrors` 的第一条(见下面那段注释)。
    """
    head = feeds.get_feed_status(store, feed_id)
    if head.get("feedStatus") not in feeds.FEED_TERMINAL:
        return head, None

    results: dict[str, tuple[str, str]] = {}
    descs: dict[str, str] = {}
    all_errs: dict[str, list[dict]] = {}
    for item in feeds.iter_feed_items(store, feed_id):
        sku = str(item.get("sku") or "")
        if not sku:
            continue
        errs = ingestion_errors(item)
        code = str(errs[0].get("code") or errs[0].get("type") or "") if errs else ""
        descs[sku] = error_text(errs)
        all_errs[sku] = errs
        results[sku] = (feeds.sku_outcome(item.get("ingestionStatus")), code)

    _STATUS = {"success": "success", "failed": "failed",
               "processing": "submitted", "unknown": "submitted"}
    n_unresolved, _ = unresolved(results)      # 收工判据只有这一处,见 unresolved()
    with db.pg_conn() as conn, conn.cursor() as cur:
        # 先取更新前状态:残留 processing/unknown 时 feed 会被重轮询,
        # 回执事件只对"本轮才落定"的 SKU 记,重轮询不得重复灌账
        cur.execute("SELECT sku, workflow, feed_type, status FROM ops.feed_items "
                    "WHERE feed_id = %s", (feed_id,))
        meta = {sku: (wf, ft, st) for sku, wf, ft, st in cur.fetchall()}
        # ── feed 级拒收:终态 ERROR 而**一条明细都没有**(itemsReceived=0)────
        # 沃尔玛这时把整个 feed 退回,报错只挂在 feed 级 `ingestionErrors` 上,
        # `iter_feed_items` 一条都翻不出来。按"明细里查无 ⇒ missing"办的后果:
        #   · sku_migrate 的 `_verdict` 只认 failed 才当场回滚,missing 要等 24h
        #     观测反证 —— 整店 2740 条改码全卡在 pending;
        #   · 上架链/维护链同样把"整 feed 被拒"读成"查无",没有任何东西会说
        #     这批货压根没进沃尔玛。
        # 2026-09-07 A131吕灿荣 整店改码实证:三条 MP_ITEM_MATCH feed 全部
        # feedStatus=ERROR、itemsReceived=0,feed 级 ingestionError
        # `EXT_DATA_ERROR_50575703577001`「You have exceeded your item setup
        # limit of 5000…」(店内现有 item 数 + 本 feed 条数 超该店上限 ⇒ 整 feed 拒收)。
        # 旧仓本来就有这条路径(docs/legacy_survey.md「feed 整体 ERROR 且
        # itemDetails 为空 ⇒ 回查预写的 SKU 列表逐个打 FEED_ERROR」),重写时丢了。
        # **有逐条明细的 ERROR feed 行为一字不变**:那种 feed 的真相在明细里。
        # 台账行**全部**落 failed(不挑 status):整 feed 被退回 = 里面没有一条到达。
        if not results and head.get("feedStatus") == "ERROR" and meta:
            head_errs = ingestion_errors(head)
            head_code = str(head_errs[0].get("code")
                            or head_errs[0].get("type") or "") if head_errs else ""
            head_desc = error_text(head_errs) or (
                "feed 级 ERROR,沃尔玛未给逐条明细(itemsReceived="
                f"{head.get('itemsReceived') or 0})")
            for sku in meta:
                results[sku] = ("failed", head_code)
                descs[sku] = head_desc
                all_errs[sku] = head_errs
            logger.warning("feed %s 整条被拒(feedStatus=ERROR,itemsReceived=%s,"
                           "零明细):台账 %d 个 SKU 全部落 failed,回执 %s | %s",
                           feed_id, head.get("itemsReceived") or 0, len(meta),
                           head_code or "(无码)", head_desc)
        cur.executemany(
            "UPDATE ops.feed_items SET status = %s, error_code = %s, "
            "error_desc = %s, resolved_at = now() "
            "WHERE feed_id = %s AND sku = %s",
            [(_STATUS[o], code or None, descs.get(sku) or None, feed_id, sku)
             for sku, (o, code) in results.items()])
        # 报错明细同步落账(标准动作,不是排障时才拉)
        _save_errors(cur, feed_id, store["name"], all_errs, meta)
        # 台账里有、终态明细里查无 → missing(不装成功也不装失败)
        cur.execute(
            "UPDATE ops.feed_items SET status = 'missing', resolved_at = now() "
            "WHERE feed_id = %s AND status = 'submitted' AND NOT (sku = ANY(%s))",
            (feed_id, list(results) or [""]))
        n_missing = cur.rowcount
        # 产品事件账本:逐 SKU 回执落账(success 是沃尔玛的一面之词,
        # 删除的最终真相由 catalog_sync 观测核验)。
        # 入账白名单(所有者定稿 2026-08-07):改价/改库存/改标题/清库存等
        # 维护回执不进病历,流水已在 ops.feed_items——receipt_in_ledger 收口
        product_events.record_many(conn, [
            {"sku": sku, "store": store["name"],
             "event": f"{product_events.feed_kind(meta[sku][1])}_feed_{o}",
             "source": meta[sku][0] or "feed_poll",
             "error_code": code or None, "detail": {"feed_id": feed_id}}
            for sku, (o, code) in results.items()
            if sku in meta and o in ("success", "failed")
            and meta[sku][2] == "submitted"
            and product_events.receipt_in_ledger(
                product_events.feed_kind(meta[sku][1]), meta[sku][0])])
        # 违禁回执自动进 ASIN 黑名单(所有者 2026-08-12:上架失败事件要能
        # 反哺"上架前拦截")。三违禁码 = 沃尔玛官方判定的政策违禁
        # (Military/Law Enforcement、Firearm Accessories、General Prohibited),
        # 2026-09-03 换轨后归新码 **POLICY**(旧码是 B=禁售;两者都在
        # PERMANENT 里,拦截行为一字不变,变的只是码名统一到新表)。
        # DO NOTHING 幂等 —— list_new/match_listing 的黑名单闸下次自动拦,
        # 同一产品不再烧 UPC 与配额。
        # 只收 kind=list(MP_ITEM)。⚠ **「sku=asin 约定」已随切码作废**:黑名单键
        # 由 blacklist.record_asins 经登记簿按 (店,sku) 反查 —— 本函数只负责把
        # store + sku 原样递过去,**不许在这里自己解 ASIN**(那就是第二份规则,
        # conventions §六)。跟卖走 MP_ITEM_MATCH ⇒ kind=match ⇒ 天然不进这个桶,
        # 其行内终态由跟卖表 F/J 列承担。
        # ⚠ **改码失败不是政策违禁,不得反哺黑名单**(SKU 改造批次 3,O8):
        # 形态 B 下 sku_migrate 走 MP_ITEM ⇒ kind=list ⇒ 正好命中这段反哺。
        # 一次改码被拒若碰巧带上违禁码,会把一个**正在正常销售**的 ASIN 永久
        # 拉黑(record_asins 是 PERMANENT),list_new/match_listing 的黑名单闸
        # 下一轮就开始拦,而没有任何摘要会说是改码干的。既有工作流名一个都不
        # 叫 sku_migrate ⇒ 改码前逐字节零行为变化。meta[sku][0] 是提交来源工作流。
        prohibited = [
            {"store": store["name"], "sku": sku, "category": "POLICY",
             "reasons": f"上架回执违禁 {(code or '').strip()}|"
                        f"{(descs.get(sku) or '')[:150]}"}
            for sku, (o, code) in results.items()
            if o == "failed" and sku in meta
            and product_events.feed_kind(meta[sku][1]) == "list"
            and meta[sku][0] != "sku_migrate"
            and (code or "").strip() in resources.WALMART_ERR_PROHIBITED]
        if prohibited:
            n_bl = blacklist.record_asins(conn, prohibited, src="feed")
            logger.warning("上架回执命中政策违禁 %d 个,新入 ASIN 黑名单 %d 个"
                           "(POLICY=违反禁售政策,上架前拦截自此生效):%s",
                           len(prohibited), n_bl,
                           ",".join(p["sku"] for p in prohibited[:10]))
    if n_missing:
        logger.warning("feed %s:%d 个 SKU 在终态明细中查无,已标 missing",
                       feed_id, n_missing)
    if n_unresolved:
        # feed 终态但个别 SKU 仍 INPROGRESS/未知:feed_log 保持 submitted,
        # 下轮再查——否则这些行永久卡 submitted,cleanup 在途拦截会永远跳过它们
        logger.warning("feed %s 已终态但 %d 个 SKU 仍 processing/unknown,"
                       "保持在途下轮重查", feed_id, n_unresolved)
    else:
        feeds.mark_feed_done(feed_id, head.get("feedStatus") == "PROCESSED")
    return head, results


def poll_all(stores_by_name: dict) -> str:
    """输入:{店铺名: store dict} → 输出:全局轮询摘要。

    扫 feed_log 全部 submitted 行**跨店并发、店内串行**地轮询;pending 行
    (提交结局不确定,仅网络 UNKNOWN 结局产生)只告警不自动补交——写操作宁停不重。

    为什么跨店能并发:每店有自己的固定出口代理,沃尔玛配额按 `(store, endpoint)`
    计,`api/_client` 的令牌桶也按这个维度限流——店与店之间不抢同一个桶。
    店内保持串行则是因为 `feeds.get_feed_status` / `iter_feed_items` 对**同一个店**
    才是同一个桶,并发只会让自己排队等退避。

    (所有者定稿 2026-08-17:「feed_poll 应该也设置为跨店并发,但店内可以串行」。
    改之前一个店挂了会顶着整轮的轮询时间,而 feed_poll 是挂高频调度的那一条。)
    """
    rows = feeds.query_pending()
    submitted = [r for r in rows if r["status"] == "submitted" and r["feed_id"]]
    pendings = [r for r in rows if r["status"] == "pending"]

    by_store: dict[str, list[dict]] = {}
    for r in submitted:
        by_store.setdefault(r["store"], []).append(r)

    def _one_store(store_name: str, srows: list[dict]) -> list[dict]:
        """输入:店铺名 + 该店在途 feed → 输出:逐 feed 的**事实记录**(不排版)。

        **各店各自的局部列表**,主线程再按店铺序合并:`done += 1` 是"读-加-写"
        三步,两个线程交错会丢计数(丢得随机、不报错);记录直接 append 进共享
        列表则会按完成先后乱序交织,同一轮跑两次输出不一样。

        排版整个留给 `poll_all`(这里只出事实):折不折叠要看全局 ——「这一轮
        一共有几个长期在途、最久多久」只有汇总时才知道。
        """
        out: list[dict] = []
        store = stores_by_name.get(store_name)
        for r in srows:
            rec = {
                "store": r["store"],
                "label": _FEED_LABEL.get(r["feed_type"], r["feed_type"]),
                "workflow": r["workflow"] or "-",
                # feed_id 只留头 18 位:整串占满一行,头几位已够去后台对
                "fid": ((r["feed_id"][:18] + "…") if len(r["feed_id"]) > 19
                        else r["feed_id"]),
                # 年龄取 updated_at —— 这个 feedId 落 submitted 的时刻。
                # **不是 created_at**:`_log_claim` 重占终态行时只改
                # status/feed_id/updated_at,created_at 留的是这个 payload_key
                # 第一次提交的时刻(可能是几个月前),拿它当年龄会把刚提交的
                # feed 一上来就判成"卡了三个月"、当场从摘要里折掉。
                "age_h": age_hours(r.get("updated_at")),
            }
            out.append(rec)
            if store is None:
                rec.update(state="skipped", detail="店铺凭证缺失,跳过")
                continue
            try:
                head, results = poll_feed(store, r["feed_id"])
            except Exception as e:
                logger.warning("feed %s 轮询失败(下轮再试): %s", r["feed_id"], e)
                rec.update(state="open", detail=f"查询失败({e}),下轮再试")
                continue
            if results is None:
                rec.update(state="open",
                           detail=f"{head.get('feedStatus')},{_progress(head)}")
                continue
            n_open, n_unk = unresolved(results)
            if n_open:
                # ⚠ **feed 终态 ≠ 落定**:残留 processing/unknown 时 poll_feed
                # 不调 mark_feed_done(那些 SKU 要留在在途队列里下轮重查),
                # 摘要跟着说"已落定"就是假的 —— 行还在 feed_log 里,下一轮
                # 一字不差再播一遍,而 `落定 N` 每轮把它重数一次。
                tail = (f",其中 {n_unk} 个状态未知(沃尔玛枚举可能已扩,查日志"
                        f"「未知 SKU ingestionStatus」)" if n_unk else "")
                rec.update(state="open",
                           detail=f"{head.get('feedStatus')} 已终态,但 {n_open} 个 "
                                  f"SKU 未落定{tail},保持在途下轮重查")
                continue
            n_ok = sum(1 for o, _ in results.values() if o == "success")
            n_bad = sum(1 for o, _ in results.values() if o == "failed")
            rec.update(state="settled",
                       detail=f"已落定 {head.get('feedStatus')},"
                              f"成功 {n_ok},失败 {n_bad}")
        return out

    done = still = skipped = 0
    detail_lines: list[str] = []
    stuck: list[dict] = []
    # 摘要按人看的店铺序排(sort_key),**不是**按完成先后:query_pending 本身
    # 没有 ORDER BY,原来那份顺序是 PG 的堆序,本来就不稳定。
    todo = sorted(by_store.items(), key=lambda kv: stores_svc.sort_key(kv[0]))
    if todo:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        per_store: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(
                max_workers=min(stores_svc.STORE_WORKERS, len(todo))) as pool:
            futs = {pool.submit(_one_store, sn, sr): sn for sn, sr in todo}
            for f in as_completed(futs):
                per_store[futs[f]] = f.result()
        for sn, _ in todo:
            for rec in per_store[sn]:
                if rec["state"] == "settled":
                    done += 1
                elif rec["state"] == "skipped":
                    skipped += 1
                else:
                    still += 1
                # 落定的**永远**出明细行:那是新信息,而且下一轮这个 feed 就
                # 不在队列里了,只播这一次。仍在途的按年龄分档:新鲜的照旧出
                # 明细(人正等着它),老的折进下面那一行(见 FEED_QUIET_HOURS)。
                if rec["state"] != "settled" and is_stuck(rec["age_h"]):
                    stuck.append(rec)
                else:
                    detail_lines.append(f"  {_who(rec)}:{rec['detail']}")

    oldest = max((r["age_h"] for r in stuck), default=0.0)
    if stuck:
        # ⚠ 自带处置(排版规矩 3):只说"5 个卡住了"等于把排查甩给读的人。
        # 这些行**不会自己好** —— 沃尔玛那边早就没动静了,而在途行永不老化
        # (docs/feed_closure_audit.md §三.4),放着只会一天原样播 48 遍。
        names = "、".join(f"{r['store']} {r['label']}(卡 {r['age_h']:.0f}h)"
                          for r in stuck[:_FOLD_NAMES])
        if len(stuck) > _FOLD_NAMES:
            names += f" 等 {len(stuck)} 个"
        detail_lines.append(
            f"  ⏳ 长期在途 {len(stuck)} 个(提交超过 {FEED_QUIET_HOURS:g}h 仍未"
            f"落定,最久 {oldest:.1f}h,明细不再逐轮复读):{names}")
        # ⚠ 上面那些 feed_id 是**截断**的(头 18 位),飞书里复制到的就是那一段
        # ——所以指引不能是"拿 feed_id 去查"(2026-09-11 所有者:「我找不到这些
        # feed 的完整的码了」)。`-p stuck=1` 只读台账,直接给完整码与现成命令。
        detail_lines.append(
            "    完整码 + 现成命令:`python cli.py feed_poll -p stuck=1`"
            "(只读台账,不调沃尔玛);处理见 docs/feed_closure_audit.md §三.4")

    if pendings:
        logger.warning("feed_log 有 %d 条 pending(提交结局不确定),"
                       "请人工核对后处理:%s", len(pendings),
                       [(p["store"], p["feed_type"], str(p["created_at"]))
                        for p in pendings[:10]])
    line = (f"feed 轮询:{len(submitted)} 个在途,落定 {done},仍处理中 {still}")
    if skipped:
        line += f",店铺凭证缺失跳过 {skipped}"
    if stuck:
        # 首行 = 结论 + 最重要的那个数(排版规矩 1:飞书列表/手机推送/ops.runs
        # 都只显示第一行)。长期在途是**例外计数**,0 则整段消失(规矩 2)。
        line += f";⏳ 长期在途 {len(stuck)}(最久 {oldest:.1f}h)"
    if pendings:
        # ⚠ 只报个数**没法处理**(2026-08-16 feed 闭环审计):摘要是发去飞书的
        # 那一份,人看到"pending 3"接下来要干什么?明细只在日志里,而 pending
        # 行**永不老化**——不落定就永远挂着,数字只增不减,几轮之后这行警告就
        # 成了背景噪音。把店铺/类型/时间摊开,至少能拿去 Walmart 后台对。
        line += f";⚠ pending 待人工核对 {len(pendings)}"
        detail_lines.append(
            "  pending(提交结局不确定,**系统不会自动补交**——"
            "核对后手工处理,见 docs/feed_closure_audit.md):")
        for p in pendings[:10]:
            detail_lines.append(
                f"    {p['store']} {p['feed_type']}"
                f"({p.get('workflow') or '-'}) 提交于 {p['created_at']}")
        if len(pendings) > 10:
            detail_lines.append(f"    …另有 {len(pendings) - 10} 条,查 "
                                f"ops.feed_log WHERE status='pending'")
    return "\n".join([line] + detail_lines)


def feed_statuses(feed_ids) -> dict[str, str]:
    """输入:feed_id 列表 → 输出:{feed_id: ops.feed_log.status}(pending/submitted/done/failed)。

    给回执消费方分辨「整 feed 被拒」用:feed 级 ERROR 且沃尔玛不给逐条明细时,台账
    行历史上落的是 missing(2026-09-07 之前的轮询;之后落 failed),消费方看到 missing +
    feed_log failed 就该按 failed 处理,而不是等 24h 观测反证。
    """
    ids = [f for f in dict.fromkeys(feed_ids) if f]
    if not ids:
        return {}
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT feed_id, status FROM ops.feed_log WHERE feed_id = ANY(%s)",
                    (ids,))
        return {fid: st for fid, st in cur.fetchall()}


def item_results(feed_id: str, workflow: str | None = None
                 ) -> dict[str, tuple[str, str]]:
    """输入:feed_id(+ 可选提交来源工作流)→ 输出:{sku: (status, error_code)}
    (读 ops.feed_items 台账)。

    `workflow` 给了就**只认那条工作流提交的行**(正向过滤,不是黑名单)。给它的
    是**回写方**:一张飞书表的反哺器只该回写自己那条链发出去的回执。2026-09-06
    起改码(sku_migrate)与跟卖(match_listing)**共用 MP_ITEM_MATCH 这个 feedType**,
    单靠 feed_type 已经分不开两条链 —— 不过滤的表现是一条改码回执被写进跟卖表的
    「feed 结果」列(行还是跟卖那一行),而且不报错。
    """
    sql = "SELECT sku, status, error_code FROM ops.feed_items WHERE feed_id = %s"
    args: tuple = (feed_id,)
    if workflow:
        sql += " AND workflow = %s"
        args += (workflow,)
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return {sku: (status, code or "") for sku, status, code in cur.fetchall()}


def merge_error(code: str | None, desc: str | None, limit: int = 900) -> str:
    """输入:错误码 + 人话描述 → 输出:回写业务表用的「码 | 人话」。

    各业务表(停用/删除、维护记录、跟卖、上架)的报错列统一用本函数拼——
    光有 EXT_DATA_ERROR_507165… 这种数字码,运营和我们都无从下手。
    """
    code, desc = (code or "").strip(), (desc or "").strip()
    if code and desc:
        return f"{code} | {desc}"[:limit]
    return (code or desc)[:limit]


def item_codes(feed_id: str) -> dict[str, set[str]]:
    """输入:feed_id → 输出:{sku: 全部错误码集合}(读 ops.feed_item_errors)。

    ops.feed_items.error_code 只留了第一个码;而一个 SKU 可能同时返回
    合规审核 + UPC 冲突 + 字段校验多个码,**正交处置**(如 UPC 冲突要标池)
    必须看全集,不能只看第一个。
    """
    out: dict[str, set[str]] = {}
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT sku, code FROM ops.feed_item_errors "
                    "WHERE feed_id = %s AND code IS NOT NULL", (feed_id,))
        for sku, code in cur.fetchall():
            out.setdefault(sku, set()).add(code.strip())
    return out


def item_errors(feed_id: str, workflow: str | None = None) -> dict[str, str]:
    """输入:feed_id(+ 可选提交来源工作流)→ 输出:{sku: 人话报错描述}
    (空描述的 SKU 不出现)。`workflow` 的语义与 `item_results` 逐字相同。"""
    sql = ("SELECT sku, error_desc FROM ops.feed_items "
           "WHERE feed_id = %s AND error_desc IS NOT NULL")
    args: tuple = (feed_id,)
    if workflow:
        sql += " AND workflow = %s"
        args += (workflow,)
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return {sku: desc for sku, desc in cur.fetchall()}
