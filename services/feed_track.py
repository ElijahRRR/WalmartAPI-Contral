"""feed 生命周期追踪积木(所有 feed 操作共用的轮询机制)。

分工(2026-08-06 定稿):
  api/feeds     负责 提交 + feed_log/feed_items 的**落台账**(提交时)
  本模块        负责 轮询 submitted feed → SKU 级终态回写 ops.feed_items
                + feed_log 落 done/failed + pending 行对账告警
                + 按 feedType 落定期限收口(所有者 2026-09-25:追到期限为止,
                  到期读明细强制落定,见 FEED_DEADLINE_MINUTES)
  workflows     feed_poll 薄壳全局轮询;各业务工作流(daily_retire 等)
                用 poll_feed 拿 {sku: 结果} 去刷各自的飞书投影列

SKU 级状态权威在 ops.feed_items;停用/删除/设置到期日期/未来的上架、改价、
改库存、改标题 feed 全走这一套,不许各工作流自造轮询。
"""

import logging
from datetime import datetime, timezone

from api import feeds
from registry import db, resources
from services import blacklist, product_events, runlock, store_retry, \
    stores as stores_svc

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
#: ⚠ 它只管**摘要排版**:在途行照旧每轮轮询,一个业务判断都不改。
#: 在途 feed 追到多久为止是另一件事:FEED_DEADLINE_MINUTES(所有者 2026-09-25
#: 定稿,到期读明细强制落定)。
FEED_QUIET_HOURS = 2.0

#: 各 feedType 的**落定期限**(分钟,唯一出处)。所有者定稿 2026-09-25:
#: 「提交 feed,追踪 feed 直至该 feed 的最长期限……达到最长期限还没有完全落定的,
#: 就查询明细(正常情况下在总终态落定后也会查询,这里相当于限制了最长时间)来落定」
#: 「期限按官方值、不加余量,请实际查看官方给的期限值,不猜测,不凭记忆回答」。
#: 官方原句与 URL 逐条在 refdata/walmart_slas.tsv(2026-09-25 逐页重核);同一操作
#: 官方给了几个数时取最长那个。期限起点 = 拿到 feedId 的时刻(feed_log.updated_at,
#: 理由见 `age_hours`)。
#:   · price / PRICE_AND_PROMOTION 15 分钟:批量改价 SLA 15 分钟(旧版、新版两页同句);
#:   · inventory / MP_INVENTORY 4 小时:Seller Center 批量改库存「最长 4 小时」
#:     (开发者文档没写时间);
#:   · MP_ITEM / MP_MAINTENANCE 24 小时:处理最长 4 小时,但**单条合规审核最长
#:     24 小时**(审核期间 ingestionStatus 一直是 INPROGRESS,feed-item-status-api)、
#:     Seller Center 状态更新最长 24 小时。危险品合规审核(3 个工作日)与 GTIN 豁免
#:     人工审核(官方不给时长)是例外:到期仍 INPROGRESS 的落 overdue,生效与否交
#:     实际结果;
#:   · MP_ITEM_MATCH 24 小时:美国站「最长 24 小时」(09-12 登记的「无法导入时最长
#:     72 小时」美国站页面已无,只剩加拿大站页面);
#:   · RETIRE_ITEM 48 小时;DELETE_ITEM 72 小时(「最长 72 小时从 Catalog 消失」)。
#: 取代 2026-09-24 的 HEAD_STALE_HOURS(汇总停更 1 小时改读明细):那一类(09-22
#: A131吕灿荣 改价 feed 汇总 31 小时停在 INPROGRESS、明细 71/71 SUCCESS)现在按
#: 改价期限 15 分钟到期读明细收口;其他类型汇总停更就等到期。
#: 守门:tests/test_feed_track.py 钉住每个值,且 api/feeds 能发的 feedType 全部在册。
FEED_DEADLINE_MINUTES = {
    "price": 15, "PRICE_AND_PROMOTION": 15,
    "inventory": 4 * 60, "MP_INVENTORY": 4 * 60,
    "MP_ITEM": 24 * 60, "MP_MAINTENANCE": 24 * 60,
    "MP_ITEM_MATCH": 24 * 60,
    "RETIRE_ITEM": 48 * 60,
    "DELETE_ITEM": 72 * 60,
}

#: 到期后**读不到明细**的宽限(小时,所有者 2026-09-25 定稿):GET 返回 404(官方:
#: feedId 不存在或本账号不可见)到期后当场落 unreadable;其他读取失败(代理 / 网络 /
#: 凭证 / 沃尔玛 5xx,归类见 store_retry.diagnose)与店铺已不可调用,再等这么久
#: 仍读不到才落 unreadable。期限前任何读取失败都只算"下轮再读"。
UNREADABLE_GRACE_HOURS = 24

#: 台账(ops.feed_items.status)里"沃尔玛**没给结论**"的三个终态(唯一出处)。
#: 与 failed(沃尔玛拒了)不是一回事:消费方**不许**拿它们回收 UPC 等资源、也不许
#: 当失败定案;需要关单的(处置建议、冷却表)按"没给结论"关单并注明依据。
#:   overdue      到期仍 INPROGRESS,或汇总没收工而明细里查无(raw_status 分得开)
#:   unrecognized 到期时 ingestionStatus 是本系统不认识的值(原值在 raw_status)
#:   unreadable   到期后读不到明细(404 当场;其他错误宽限后)
#: missing(汇总终态、明细里查无此条)**不在**这里:那是沃尔玛收工后的明确陈述,
#: 各消费方对它的既有处理不变。
NO_VERDICT_STATUSES = ("overdue", "unrecognized", "unreadable")

#: 折叠行里最多点几个名字(其余给 SQL 自己查:名字越多越没人看)
_FOLD_NAMES = 3

# SKU 台账状态 → 飞书表结果列文案。状态词只有一个出处(api/feeds.sku_outcome
# 的 success/failed/processing/unknown,加台账自己的 submitted/missing),中文面
# 此前有四份拷贝:clear_sheet:27 / maint_sheet:221 / maint_sheet:343(同一文件
# 里又内联一份)/ match_sheet:104。本常量是四份的**并集**——processing/unknown
# 两键只有 clear_sheet 那份有,而 product_clear 是 `RESULT_TEXT[outcome]` 直接
# 下标取(不是 .get),少一键就是 KeyError,不许"看着重复"就删。
# 2026-09-25 所有者定稿按期限收口后新增三个终态词(见 NO_VERDICT_STATUSES);
# missing 的中文从「未查到」改「明细无此条」—— 原词与维护表 3 天超期的「未查到」
# 同字不同义(一个是沃尔玛明细里没有,一个是表侧等烦了)。
# ⚠ processing/unknown 是**轮询中间态**(明细说还在跑 / 枚举没认出来),不进台账;
#   到期时它们分别落 overdue / unrecognized。
RESULT_TEXT = {"success": "成功", "failed": "失败", "missing": "明细无此条",
               "overdue": "超期未完成", "unrecognized": "未知状态",
               "unreadable": "无法查询",
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


def deadline_hours(feed_type: str) -> float:
    """输入:feedType → 输出:落定期限(小时,见 FEED_DEADLINE_MINUTES)。

    未登记的 feedType 直接 KeyError(宁炸不吞):按某个缺省值收口,等于替官方
    编一个数。守门用例保证 api/feeds 能发的每个 feedType 都在册。
    """
    return FEED_DEADLINE_MINUTES[feed_type] / 60


def past_deadline(feed_type: str | None, age_h: float | None) -> bool:
    """输入:feedType + 在途年龄(小时)→ 输出:是否已到落定期限。

    年龄或 feedType 拿不到(业务工作流提交后就地的即时轮询不传)一律**未到期**:
    不确定时只信汇总终态,不拿不确定的年龄去强制收口。
    """
    if age_h is None or not feed_type:
        return False
    return age_h >= deadline_hours(feed_type)


def past_grace(feed_type: str | None, age_h: float | None) -> bool:
    """输入:feedType + 在途年龄 → 输出:是否已过「期限 + 读不到的宽限」
    (UNREADABLE_GRACE_HOURS)。用于读不到明细时落 unreadable,与 pending 对账里
    「查不动 / 核不清」的收口(reconcile_pending)。"""
    return (past_deadline(feed_type, age_h)
            and age_h >= deadline_hours(feed_type) + UNREADABLE_GRACE_HOURS)


def deadline_text(feed_type: str) -> str:
    """输入:feedType → 输出:期限的人话(15 分钟 / 24 小时);未登记给「未登记」。"""
    m = FEED_DEADLINE_MINUTES.get(feed_type)
    if m is None:
        return "未登记"
    return f"{m} 分钟" if m < 60 else f"{m / 60:g} 小时"


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


#: 「这一行 feed 还在途」的**唯一 SQL 口径**(2026-09-25):台账仍 submitted,**且**
#: 它的 feed 在 ops.feed_log 里还没收口(pending / submitted)。替代各处的
#: 「submitted 且 48 小时内」:feed 按落定期限收口(FEED_DEADLINE_MINUTES),在途的
#: 上限由期限给,不需要第二个时钟(所有者 09-25:在途闸留,48 小时上限随期限表去掉)。
#: feed_log 已收口而行仍 submitted 的孤儿(人工改过台账等)不算在途 —— 不会把
#: SKU 永久挡住。用法:`IN_FLIGHT_SQL.format(t="f")`,t 是 ops.feed_items 的别名。
#: 消费方:workflows/problem_scan._SQL_INFLIGHT、workflows/sku_migrate._SQL_INFLIGHT_OLD。
IN_FLIGHT_SQL = ("{t}.status = 'submitted' AND EXISTS (SELECT 1 FROM ops.feed_log l"
                 " WHERE l.feed_id = {t}.feed_id AND l.status IN ('pending', 'submitted'))")

#: 明细里查无的 SKU 落账时 raw_status 记的字样(沃尔玛没给这一条,不是某个枚举值)
_ABSENT = "明细缺席"

#: 逐 SKU 落定。**只改仍 submitted 的行**(首次落定即定稿):落过的不再改状态、
#: 不再刷 resolved_at —— 每轮重写会把 problem_scan 在途闸「success 且
#: resolved_at > last_seen_at ⇒ 待观测」永远撑开(2026-09-24 实见:A171罗尹鸿
#: 942 个 + 谭总22 29 个 SKU 因终态残留 feed 每轮重读而长期"待观测")。
_LAND_SQL = ("UPDATE ops.feed_items SET status = %s, error_code = %s, "
             "error_desc = %s, raw_status = %s, settled_by = %s, "
             "resolved_at = now() "
             "WHERE feed_id = %s AND sku = %s AND status = 'submitted'")

#: 台账里有、明细里查无的行一次扫掉:汇总终态 ⇒ missing(明细无此条),
#: 到期而汇总没收工 ⇒ overdue。两个词与 settled_by 都来自本模块的固定集合,
#: 按字面拼进 SQL(不是用户输入)。
_ABSENT_SQL = ("UPDATE ops.feed_items SET status = '{status}', "
               f"raw_status = '{_ABSENT}', settled_by = '{{by}}', "
               "resolved_at = now() "
               "WHERE feed_id = %s AND status = 'submitted' AND NOT (sku = ANY(%s))")


def poll_feed(store: dict, feed_id: str, *, age_h: float | None = None,
              feed_type: str | None = None, execute: bool = True
              ) -> tuple[dict, dict | None]:
    """输入:店铺 + feed_id(+ 在途年龄、feedType、是否真落账)→ 输出:(汇总 head, SKU 结果)。

    所有者定稿(2026-09-25):feed 追踪到 feedType 的落定期限(FEED_DEADLINE_MINUTES)
    为止 —— 汇总终态就读明细落定;**到期不管汇总怎么说都读明细强制落定**。

    读不读明细:汇总终态(PROCESSED / ERROR)或已到期 ⇒ 读;都不是 ⇒ 结果 None,
    不翻明细(head 自带进度计数)。

    怎么落(**首次落定即定稿**,见 _LAND_SQL):
      · 明细 SUCCESS / *_ERROR ⇒ success / failed(+错误码与人话);
      · 汇总终态、明细里查无 ⇒ missing(明细无此条);
      · 已到期:明细仍 INPROGRESS、或汇总没收工而明细里查无 ⇒ overdue;
        ingestionStatus 不认识 ⇒ unrecognized;
      · 汇总终态但未到期,仍有 INPROGRESS / 不认识的 ⇒ 这几行留在途,下轮再读,
        最迟到期强制落定(官方:PROCESSED 之后单条仍可能在复核,
        GTIN 豁免 / 合规审核期间 ingestionStatus 一直是 INPROGRESS)。
    每行都记 raw_status(沃尔玛原始 ingestionStatus;查无记「明细缺席」)与
    settled_by(head = 汇总终态时落 / deadline = 到期强制落)。这个 feed 在台账里
    没有 submitted 行了 ⇒ feed_log 收口(ERROR 落 failed,其余 done)。

    **例外:feed 级拒收(终态 ERROR + 一条明细都没有)⇒ 台账逐 SKU 落 failed**,
    回执用 head 里 feed 级 `ingestionErrors` 的第一条(见下面那段注释)。

    `execute=False`(`feed_poll --dry-run`):同一份判据照算、照返回,落账在同一个
    事务里 rollback,feed_log 不收口 —— 摘要说的就是"将要落定"的那一份。

    `age_h` / `feed_type` 只有全局轮询 `poll_all` 传;业务工作流提交后就地的即时
    轮询(product_clear / sku_locked_heal)不传 ⇒ 只认汇总终态,到期收口归 feed_poll。

    返回 {sku: (状态, 错误码)}:落定的给台账词(success / failed / missing /
    overdue / unrecognized),仍在途的给轮询中间态(processing / unknown,见 `unresolved`)。
    """
    head = feeds.get_feed_status(store, feed_id)
    terminal = head.get("feedStatus") in feeds.FEED_TERMINAL
    due = past_deadline(feed_type, age_h)
    if not terminal and not due:
        return head, None

    results: dict[str, tuple[str, str]] = {}
    descs: dict[str, str] = {}
    raws: dict[str, str] = {}
    all_errs: dict[str, list[dict]] = {}
    for item in feeds.iter_feed_items(store, feed_id):
        sku = str(item.get("sku") or "")
        if not sku:
            continue
        errs = ingestion_errors(item)
        code = str(errs[0].get("code") or errs[0].get("type") or "") if errs else ""
        # 合规审核中的行官方给 pendingStatusDescription(「审核最长 24 小时」,
        # feed-item-status-api):没有 ingestionErrors 时它就是"为什么还在跑"的原话
        descs[sku] = error_text(errs) or str(
            item.get("pendingStatusDescription") or "").strip()[:900]
        all_errs[sku] = errs
        raws[sku] = str(item.get("ingestionStatus") or "")
        results[sku] = (feeds.sku_outcome(item.get("ingestionStatus")), code)

    with db.pg_conn() as conn, conn.cursor() as cur:
        # 先取更新前状态:回执事件 / 违禁入黑名单只对"本轮才落定"的 SKU 做,
        # 重轮询(终态残留的 feed 到期前每轮都会重读)不得重复灌账
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
        # 官方:「ERROR The feed failed as a whole. No items were processed.」
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
                raws[sku] = "ERROR(整 feed 拒收)"
                all_errs[sku] = head_errs
            logger.warning("feed %s 整条被拒(feedStatus=ERROR,itemsReceived=%s,"
                           "零明细):台账 %d 个 SKU 全部落 failed,回执 %s | %s",
                           feed_id, head.get("itemsReceived") or 0, len(meta),
                           head_code or "(无码)", head_desc)
        by_head = "head" if terminal else "deadline"
        # 本轮要落的:明细给了结论的照落;到期了,还在跑的落 overdue、不认识的落
        # unrecognized;未到期的残留不落(留在途,下轮再读)
        land: dict[str, tuple[str, str]] = {}
        settled_by: dict[str, str] = {}
        for sku, (o, code) in results.items():
            if o in ("success", "failed"):
                land[sku], settled_by[sku] = (o, code), by_head
            elif due:
                land[sku] = ("overdue" if o == "processing" else "unrecognized", code)
                settled_by[sku] = "deadline"
        cur.executemany(_LAND_SQL, [
            (st, code or None, descs.get(sku) or None, raws.get(sku) or None,
             settled_by[sku], feed_id, sku)
            for sku, (st, code) in land.items()])
        # 报错明细同步落账(标准动作,不是排障时才拉;幂等)
        _save_errors(cur, feed_id, store["name"], all_errs, meta)
        # 台账里有、明细里查无:汇总终态 ⇒ missing;到期而汇总没收工 ⇒ overdue
        absent_status = "missing" if terminal else "overdue"
        cur.execute(_ABSENT_SQL.format(status=absent_status, by=by_head),
                    (feed_id, list(results) or [""]))
        n_absent = cur.rowcount
        # 产品事件账本:逐 SKU 回执落账(success 是沃尔玛的一面之词,
        # 删除的最终真相由 catalog_sync 观测核验)。
        # 入账白名单(所有者定稿 2026-08-07):改价/改库存/改标题/清库存等
        # 维护回执不进病历,流水已在 ops.feed_items——receipt_in_ledger 收口
        fresh = {sku: v for sku, v in land.items()
                 if sku in meta and meta[sku][2] == "submitted"}
        product_events.record_many(conn, [
            {"sku": sku, "store": store["name"],
             "event": f"{product_events.feed_kind(meta[sku][1])}_feed_{st}",
             "source": meta[sku][0] or "feed_poll",
             "error_code": code or None, "detail": {"feed_id": feed_id}}
            for sku, (st, code) in fresh.items()
            if st in ("success", "failed")
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
        # 只取本轮才落定的(fresh):终态残留的 feed 到期前每轮重读,上一轮已入
        # 黑名单的不必每轮再报一遍「命中违禁」
        prohibited = [
            {"store": store["name"], "sku": sku, "category": "POLICY",
             "reasons": f"上架回执违禁 {(code or '').strip()}|"
                        f"{(descs.get(sku) or '')[:150]}"}
            for sku, (st, code) in fresh.items()
            if st == "failed"
            and product_events.feed_kind(meta[sku][1]) == "list"
            and meta[sku][0] != "sku_migrate"
            and (code or "").strip() in resources.WALMART_ERR_PROHIBITED]
        if prohibited:
            n_bl = blacklist.record_asins(conn, prohibited, src="feed")
            logger.warning("上架回执命中政策违禁 %d 个,新入 ASIN 黑名单 %d 个"
                           "(POLICY=违反禁售政策,上架前拦截自此生效):%s",
                           len(prohibited), n_bl,
                           ",".join(p["sku"] for p in prohibited[:10]))
        if not execute:
            conn.rollback()         # 空跑:判据照算,台账一行不留
    if n_absent:
        logger.warning("feed %s:%d 个 SKU 在明细中查无,已标 %s(%s)", feed_id,
                       n_absent, absent_status, RESULT_TEXT[absent_status])
    out: dict[str, tuple[str, str]] = {
        sku: land.get(sku, r) for sku, r in results.items()}
    for sku, (_wf, _ft, st) in meta.items():
        if st == "submitted" and sku not in results:
            out[sku] = (absent_status, "")
    n_unresolved, _ = unresolved(out)       # 收工判据只有这一处,见 unresolved()
    if n_unresolved:
        # 汇总终态但未到期,个别 SKU 仍 INPROGRESS / 未认出:feed_log 保持
        # submitted,下轮再读明细,最迟到期(FEED_DEADLINE_MINUTES)强制落定
        logger.warning("feed %s 已终态但 %d 个 SKU 仍 processing/unknown,"
                       "留在途,最迟到期(%s)强制落定", feed_id, n_unresolved,
                       deadline_text(feed_type) if feed_type else "由 feed_poll 判")
    else:
        if not terminal:
            logger.warning("feed %s 到期(%s)汇总仍停在 %s(提交 %.1fh,%s),"
                           "已按明细强制落定", feed_id, deadline_text(feed_type),
                           head.get("feedStatus"), age_h, _progress(head))
        if execute:
            feeds.mark_feed_done(feed_id, head.get("feedStatus") == "PROCESSED"
                                 or not terminal)
    return head, out


def settle_unreadable(feed_id: str, cls: str, why: str, *,
                      execute: bool = True) -> int:
    """输入:feed_id + 读不到的归类(沃尔玛404 / 代理波动 / 店铺不可调用…)+ 原话
    → 输出:落 unreadable 的 SKU 数(并收口 feed_log)。

    只在 `poll_all` 判定「到期后 404」或「过了期限 + UNREADABLE_GRACE_HOURS 仍读不到」
    时调(所有者 2026-09-25 定稿)。只改仍 submitted 的行(首次落定即定稿);
    归类进 raw_status、原话进 error_desc —— 给人看"为什么没有结论"。
    `execute=False` 同 poll_feed:照算、rollback、不收口。
    """
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.feed_items SET status = 'unreadable', raw_status = %s, "
            "error_desc = %s, settled_by = 'unreadable', resolved_at = now() "
            "WHERE feed_id = %s AND status = 'submitted'",
            (cls[:200], why[:900], feed_id))
        n = cur.rowcount
        if not execute:
            conn.rollback()
    if execute:
        feeds.mark_feed_done(feed_id, True)
    return n


def _undecided_why(feed: dict | None) -> str:
    """输入:反查 UNKNOWN 带回的候选(find_recent_feed)→ 输出:依据里的人话。"""
    matched = [str(x) for x in (feed or {}).get("matched") or []]
    unverified = [str(x) for x in (feed or {}).get("unverified") or []]
    if not matched and not unverified:
        return "feed 列表读取失败"
    tail = (f"另有 {len(unverified)} 条候选明细还核对不了({'、'.join(unverified[:3])})"
            if unverified else "")
    if len(matched) > 1:
        return (f"{len(matched)} 条候选条数与 SKU 集合都对得上({'、'.join(matched[:3])}),"
                f"分不清是哪一笔" + (f";{tail}" if tail else ""))
    if matched:
        return f"{matched[0]} 对得上,但{tail},核清之前不收编"
    return (f"{len(unverified)} 条候选明细还核对不了({'、'.join(unverified[:3])}):"
            f"读不到、还是空的,或只露出本片一部分")


def reconcile_pending(pendings: list[dict], stores_by_name: dict,
                      execute: bool = True) -> list[dict]:
    """输入:pending 台账行(query_pending)+ 店铺表(+ 是否真写)→ 输出:逐行的对账记录。

    所有者 2026-09-25 批(推翻 2026-08-16「pending 不做对账器」):pending = POST 结局
    不确定、没拿到 feedId —— **不等于没提交上**(请求可能已到沃尔玛、只是回包丢了)。
    每轮 feed_poll 对每行只读反查,按落定期限收口,**绝不补交**(写操作永不自动兜底;
    落 failed = 载荷解锁,要不要再发由原业务工作流下一轮按原方法决定):

      · 原工作流还在跑(它的运行锁被占着)⇒ 一格不碰:这一笔可能正在它手里当场结算
        (反查 / 补交),这时收编会让它的反查把收编的 feed 当"已记账"排除、判未达、
        同一载荷补交 = 重复提交;锁探不了就等过期限 + 宽限再动;
      · 存量行(item_count 为空,09-25 之前 claim 的,没记条数 / SKU / 发送标记)
        ⇒ 无法反查;过了期限 + 宽限落 failed「提交未确认」;
      · post_started_at 为空 = 请求**从没开始发**(原工作流已不在跑)⇒ 落 failed「未发出」;
      · 店铺不可调用(不在营 / 凭证缺失)⇒ 过了期限 + 宽限落 failed「提交未确认」;
      · 反查(api.feeds.find_recent_feed:按发送时刻开窗、翻页、条数 + **SKU 集合核对**):
          FOUND     ⇒ 收编(feed_log 转 submitted、补落 feed_items,时刻用发送时刻),
                      下一轮起按落定期限正常追踪;
          NOT_FOUND ⇒ 到期仍没有 ⇒ failed「提交未确认」;没到期下轮再查;
          UNKNOWN   ⇒ 反查本身失败 / 候选核不清(不止一条对得上,或明细还核对不了)
                      ⇒ 期限 + 宽限后 failed「提交未确认」,依据里点名候选。
    期限与宽限同 feed 结果(FEED_DEADLINE_MINUTES、UNREADABLE_GRACE_HOURS),起点 =
    发送时刻(没有就用 claim 时刻)。`execute=False`:照查照判,一行不写。
    """
    out: list[dict] = []
    for p in pendings:
        ft = p["feed_type"]
        wf = p.get("workflow") or ""
        posted = p.get("post_started_at")
        age = age_hours(posted or p.get("updated_at"))
        registered = ft in FEED_DEADLINE_MINUTES
        due = registered and past_deadline(ft, age)
        grace_over = registered and past_grace(ft, age)
        limit = deadline_text(ft)
        rec = {"store": p["store"], "label": _FEED_LABEL.get(ft, ft),
               "workflow": wf or "-", "feed_type": ft, "age_h": age,
               "state": "open", "detail": ""}
        out.append(rec)

        def _close(basis: str, _rec=rec, _p=p) -> None:
            ok = feeds.close_pending(_p["id"], basis) if execute else True
            _rec.update(state="closed" if ok else "open",
                        detail=(f"落 failed:{basis}" if ok
                                else "行已不是 pending(别处刚处理过),不动"))

        # 原工作流还在跑 ⇒ 这一行还在它手里,一格不碰:它可能正在当场结算这一笔
        # (提交当场的 30 秒复查 / list_new 整轮跑完才做的延后结算)。
        # 这时收编,它自己的反查就会把收编的 feed 当"已记账"排除掉 → 判未达 → 同一
        # 载荷补交 = 重复提交。锁探不了(锁文件打不开)就等过期限 + 宽限:当场结算
        # 没有哪一轮会拖那么久。
        held = runlock.is_held(wf) if wf else None
        if held or (held is None and not grace_over):
            rec["detail"] = (
                f"请求还没开始发送,{wf} 正在跑(可能正排队等配额),下轮再看"
                if held and posted is None and p.get("item_count") is not None else
                f"{wf} 正在跑,这一笔可能正在它手里当场结算(反查 / 补交),跑完再对账"
                if held else
                f"判不了 {wf or '原工作流'} 在不在跑(锁文件打不开),落定期限 {limit}"
                f" + 宽限 {UNREADABLE_GRACE_HOURS}h 内先不动")
            continue
        if p.get("item_count") is None:
            if grace_over:
                _close("提交未确认:存量 pending(09-25 之前 claim,没记条数、SKU 与发送标记),"
                       f"无法反查;过了落定期限 {limit} + 宽限 {UNREADABLE_GRACE_HOURS}h")
            else:
                rec["detail"] = (f"存量 pending(没记条数与 SKU,无法反查),到落定期限 {limit}"
                                 f" + 宽限 {UNREADABLE_GRACE_HOURS}h 落 failed(提交未确认)")
            continue
        if posted is None:
            _close(f"未发出:{wf} 已不在运行,请求从没开始发送(发送标记为空)" if held is False
                   else f"未发出:请求从没开始发送(发送标记为空),claim 已过落定期限 {limit}"
                        f" + 宽限 {UNREADABLE_GRACE_HOURS}h")
            continue
        store = stores_by_name.get(p["store"])
        if store is None:
            if grace_over:
                _close("提交未确认:店铺不可调用(不在营或凭证缺失),落定期限 "
                       f"{limit} + 宽限 {UNREADABLE_GRACE_HOURS}h 内一直没法反查")
            else:
                rec["detail"] = "店铺不可调用(不在营或凭证缺失),暂不能反查"
            continue
        err = ""
        try:
            verdict, feed = feeds.find_recent_feed(
                store, ft, int(p["item_count"]), since=posted,
                expect_skus=list(p.get("skus") or []), recheck=False)
        except Exception as e:      # noqa: BLE001 —— 反查失败就是 UNKNOWN,原话留给依据
            verdict, feed, err = "UNKNOWN", None, f"{store_retry.diagnose(e)}:{e}"
        n = int(p.get("recon_count") or 0) + 1
        if execute:
            feeds.note_reconcile(p["id"])
        if verdict == "FOUND":
            ok = feeds.adopt_pending(p, feed["feedId"], posted) if execute else True
            rec.update(state="adopted" if ok else "open",
                       detail=(f"收编 feedId={feed['feedId']}(反查第 {n} 次找到,条数与"
                               f" SKU 集合一致),下轮起按落定期限 {limit} 追踪"
                               if ok else "行已不是 pending(别处刚处理过),不动"))
        elif verdict == "NOT_FOUND":
            if due:
                _close(f"提交未确认:落定期限 {limit} 内反查 {n} 次,沃尔玛 feed 列表里"
                       f"都没有这一笔(条数 {p['item_count']} + SKU 集合)")
            else:
                rec["detail"] = f"反查第 {n} 次没找到,落定期限 {limit} 前每轮再查"
        else:
            why = err or _undecided_why(feed)
            if grace_over:
                _close(f"提交未确认:落定期限 {limit} + 宽限 {UNREADABLE_GRACE_HOURS}h 内"
                       f"反查一直没有结论({n} 次,最后:{why})"[:500])
            else:
                rec["detail"] = f"反查第 {n} 次没有结论({why}),下轮再查"
    return out


def poll_all(stores_by_name: dict, execute: bool = True,
             only: str | None = None) -> str:
    """输入:{店铺名: store dict}(+ 是否真落账、限定店铺)→ 输出:全局轮询摘要。

    扫 feed_log 全部 submitted 行**跨店并发、店内串行**地轮询,每条带上它的
    feedType 与在途年龄(所有者 2026-09-25 定稿的期限收口):
      · 到了落定期限(FEED_DEADLINE_MINUTES)⇒ `poll_feed` 读明细强制落定;
      · 到期后读不到明细 ⇒ 404 当场、其他失败(含店铺已不可调用)过了
        UNREADABLE_GRACE_HOURS ⇒ `settle_unreadable` 落「无法查询」;期限前的读取
        失败一律"下轮再读"。
    pending 行(提交结局不确定)先过 `reconcile_pending`:只读反查,查到收编(下一轮
    起按期限追踪)、到期查不到落 failed「提交未确认」、确定没发出落 failed「未发出」,
    **绝不补交** —— 写操作宁停不重(所有者 2026-09-25 批)。

    为什么跨店能并发:每店有自己的固定出口代理,沃尔玛配额按 `(store, endpoint)`
    计,`api/_client` 的令牌桶也按这个维度限流——店与店之间不抢同一个桶。
    店内保持串行则是因为 `feeds.get_feed_status` / `iter_feed_items` 对**同一个店**
    才是同一个桶,并发只会让自己排队等退避。

    (所有者定稿 2026-08-17:「feed_poll 应该也设置为跨店并发,但店内可以串行」。
    改之前一个店挂了会顶着整轮的轮询时间,而 feed_poll 是挂高频调度的那一条。)

    `execute=False`(`feed_poll --dry-run`):照读沃尔玛、照算,台账一行不落
    (poll_feed / settle_unreadable 同事务 rollback),摘要首行标 [DRY-RUN]。

    `only`(`feed_poll -p store=X`):只轮询这一家店的在途行。⚠ 不能靠
    stores_by_name 自然缺席来过滤 —— 缺席的店会被当成「店铺不可调用」,过了
    期限 + 宽限就落「无法查询」;限定一家店跑,其他店的老 feed 不许被顺手判掉。
    """
    rows = feeds.query_pending()
    if only:
        rows = [r for r in rows if r["store"] == only]
    submitted = [r for r in rows if r["status"] == "submitted" and r["feed_id"]]
    pendings = [r for r in rows if r["status"] == "pending"]

    by_store: dict[str, list[dict]] = {}
    for r in submitted:
        by_store.setdefault(r["store"], []).append(r)
    # pending 对账先做:收编的行这一轮不追(下一轮 query_pending 就是 submitted 了)
    recon = reconcile_pending(pendings, stores_by_name, execute=execute)

    def _unreadable(rec: dict, feed_id: str, cls: str, why: str) -> None:
        n = settle_unreadable(feed_id, cls, why, execute=execute)
        rec.update(state="settled", unreadable=True,
                   detail=f"无法查询({cls}):过了落定期限 "
                          f"{deadline_text(rec['feed_type'])} 仍读不到明细,"
                          f"{n} 个 SKU 落「无法查询」|{why[:160]}")

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
            ft = r["feed_type"]
            rec = {
                "store": r["store"],
                "label": _FEED_LABEL.get(ft, ft),
                "workflow": r["workflow"] or "-",
                "feed_type": ft,
                # feed_id 只留头 18 位:整串占满一行,头几位已够去后台对
                "fid": ((r["feed_id"][:18] + "…") if len(r["feed_id"]) > 19
                        else r["feed_id"]),
                # 年龄取 updated_at —— 这个 feedId 落 submitted 的时刻。
                # **不是 created_at**:`_log_claim` 重占终态行时只改
                # status/feed_id/updated_at,created_at 留的是这个 payload_key
                # 第一次提交的时刻(可能是几个月前),拿它当年龄会把刚提交的
                # feed 一上来就判成"卡了三个月"、当场从摘要里折掉 —— 更会被
                # 当成早已到期而强制落定。
                "age_h": age_hours(r.get("updated_at")),
            }
            out.append(rec)
            # 期限未登记的 feedType(不该有:守门用例钉着 api/feeds 能发的全部
            # 在册)只认汇总终态,摘要点名,不替官方编一个期限
            registered = ft in FEED_DEADLINE_MINUTES
            due = registered and past_deadline(ft, rec["age_h"])
            grace_over = registered and past_grace(ft, rec["age_h"])
            if store is None:
                if grace_over:
                    _unreadable(rec, r["feed_id"], "店铺不可调用",
                                "店铺未加载(不在营或凭证缺失),期限 + 宽限内一直没法读")
                else:
                    rec.update(state="skipped", detail="店铺凭证缺失,跳过")
                continue
            try:
                head, results = poll_feed(store, r["feed_id"], age_h=rec["age_h"],
                                          feed_type=ft if registered else None,
                                          execute=execute)
            except Exception as e:
                http = getattr(e, "status", None)       # api.feeds.FeedQueryError
                if due and (http == 404 or grace_over):
                    _unreadable(rec, r["feed_id"], store_retry.diagnose(e), str(e))
                    continue
                logger.warning("feed %s 轮询失败(下轮再试): %s", r["feed_id"], e)
                detail = f"查询失败({e}),下轮再试"
                if due:
                    detail += (f";已过落定期限 {deadline_text(ft)},读不到满 "
                               f"{UNREADABLE_GRACE_HOURS}h 落「无法查询」")
                rec.update(state="open", detail=detail)
                continue
            fs = head.get("feedStatus")
            if results is None:
                rec.update(state="open", detail=f"{fs},{_progress(head)}"
                           + ("" if registered else
                              f";⚠ {ft} 未登记落定期限,只能等汇总终态"))
                continue
            terminal = fs in feeds.FEED_TERMINAL
            n_open, n_unk = unresolved(results)
            cnt: dict[str, int] = {}
            for o, _ in results.values():
                cnt[o] = cnt.get(o, 0) + 1
            tail = (f",其中 {n_unk} 个状态未知(沃尔玛枚举可能已扩,查日志"
                    f"「未知 SKU ingestionStatus」)" if n_unk else "")
            if n_open:
                # 只剩一种可能:汇总终态、还没到期、个别 SKU 仍在跑 —— 到期的
                # poll_feed 已强制落定,不会留残留
                rec.update(state="open",
                           detail=f"{fs} 已终态,但 {n_open} 个 SKU 仍在处理"
                                  f"{tail},留在途,最迟到期"
                                  f"({deadline_text(ft)})按明细强制落定")
                continue
            parts = f"成功 {cnt.get('success', 0)},失败 {cnt.get('failed', 0)}"
            for st in ("missing", "overdue", "unrecognized"):
                if cnt.get(st):
                    parts += f",{RESULT_TEXT[st]} {cnt[st]}"
            if terminal and not (cnt.get("overdue") or cnt.get("unrecognized")):
                rec.update(state="settled", detail=f"已落定 {fs},{parts}")
            else:
                # 汇总与明细自相矛盾(汇总"处理中"、明细有结论)这句要原样摆出来:
                # 它就是沃尔玛汇总停更的证据(2026-09-22 A131吕灿荣 实证)
                why = (f"沃尔玛汇总仍停在 {fs}:{_progress(head)}" if not terminal
                       else f"沃尔玛汇总 {fs}")
                rec.update(state="settled", by_deadline=True,
                           detail=f"到期收口(落定期限 {deadline_text(ft)},"
                                  f"{why}):{parts}")
        return out

    done = still = skipped = by_deadline = unreadable = 0
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
                    by_deadline += bool(rec.get("by_deadline"))
                    unreadable += bool(rec.get("unreadable"))
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
        # 这些行各自到了落定期限就会被强制落定(FEED_DEADLINE_MINUTES,删除最长
        # 72 小时),期限前没什么可做的 —— 逐轮复读只会一天原样播 48 遍。
        names = "、".join(f"{r['store']} {r['label']}(卡 {r['age_h']:.0f}h)"
                          for r in stuck[:_FOLD_NAMES])
        if len(stuck) > _FOLD_NAMES:
            names += f" 等 {len(stuck)} 个"
        detail_lines.append(
            f"  ⏳ 长期在途 {len(stuck)} 个(提交超过 {FEED_QUIET_HOURS:g}h 仍未"
            f"落定,最久 {oldest:.1f}h;各自到了落定期限即按明细强制落定,"
            f"明细不再逐轮复读):{names}")
        # ⚠ 上面那些 feed_id 是**截断**的(头 18 位),飞书里复制到的就是那一段
        # ——所以指引不能是"拿 feed_id 去查"(2026-09-11 所有者:「我找不到这些
        # feed 的完整的码了」)。`-p stuck=1` 只读台账,直接给完整码与现成命令。
        detail_lines.append(
            "    完整码 + 现成命令:`python cli.py feed_poll -p stuck=1`"
            "(只读台账,不调沃尔玛);期限见 refdata/walmart_slas.tsv")

    line = f"feed 轮询:{len(submitted)} 个在途,落定 {done}"
    extras = []
    if by_deadline:
        # 例外计数(规矩 2:0 则整段消失)。个数本身就是信号:到期还没收工的,
        # 多半是沃尔玛汇总停更或单条卡在审核
        extras.append(f"到期收口 {by_deadline}")
    if unreadable:
        extras.append(f"无法查询 {unreadable}")
    if extras:
        line += f"(其中{'、'.join(extras)})"
    line += f",仍处理中 {still}"
    if skipped:
        line += f",店铺凭证缺失跳过 {skipped}"
    if stuck:
        # 首行 = 结论 + 最重要的那个数(排版规矩 1:飞书列表/手机推送/ops.runs
        # 都只显示第一行)。长期在途是**例外计数**,0 则整段消失(规矩 2)。
        line += f";⏳ 长期在途 {len(stuck)}(最久 {oldest:.1f}h)"
    if recon:
        # pending 对账(2026-09-25):首行报三档个数(0 的整段消失),明细逐行报
        # 依据 —— 收编 / 落 failed 是新信息(只播这一次),仍待的说清在等什么
        n_ad = sum(1 for r in recon if r["state"] == "adopted")
        n_cl = sum(1 for r in recon if r["state"] == "closed")
        n_op = len(recon) - n_ad - n_cl
        parts = [f"{w} {n}" for w, n in (("收编", n_ad), ("落 failed", n_cl),
                                         ("仍待", n_op)) if n]
        line += f";pending 对账 {len(recon)}:{'、'.join(parts)}"
        detail_lines.append("  pending(提交结局不确定;每轮只读反查,**不自动补交**):")
        shown = sorted(recon, key=lambda r: r["state"] == "open")   # 新信息在前
        for r in shown[:10]:
            detail_lines.append(f"    {r['store']} {r['label']}({r['workflow']}):"
                                f"{r['detail']}")
        if len(shown) > 10:
            detail_lines.append(f"    …另有 {len(shown) - 10} 条,查 "
                                f"ops.feed_log WHERE status='pending'")
    if not execute:
        # cli 只给 DANGEROUS 工作流打 [DRY-RUN] 横幅,feed_poll 不是 ⇒ 自己标在首行
        line = f"[DRY-RUN] {line}(空跑:台账一行未落,以上是将要落定的)"
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
