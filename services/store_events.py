"""店铺事件账本积木(ops.store_events):店铺一生的病历,TRO 封店预警的地基。

与 catalog.product_events 同构不同表,三条纪律照搬:
  ① 只追加永不改;② 事件码唯一出处 = 本文件常量与 EVENTS 集合;
  ③ record_many 对未登记码直接抛错(拼错的码 = 一支永远没人查的分叉)。

与 ops.store_kpi_daily 的分工(所有者定稿 2026-08-30):
  KPI 表 = **日粒度截面**(当下是什么样,30+ 列每天一行);
  本表   = **变化流**(发生了什么变化 / 做了什么动作,只追加)。
  事件里**绝不重复存 KPI 的数值**:状态类事件只记 {old, new},要看当时全貌
  按 (store, occurred_at::date) 回查 KPI 表 —— 同一个数字两处存,一定会漂。

事件三分类(按"要不要叫醒人"分,不混着记):
  risk       风险类:状态迁移 / TRO 品牌波及 / 钓鱼订单。极低频,高危推送。
  governance 治理类:倍率/限额/分配占用变更。低频,审计用(三期接线)。
  ops        运营类:上架/维护/清理等**按轮汇总**,每店每轮一条,绝不逐 SKU
             (逐 SKU 是 ops.feed_items 的职责,重复记几个月就上千万行)。

severity 与事件码是两个维度:同一个码两个方向级别不同(ACTIVE→SUSPENDED 是
high,SUSPENDED→ACTIVE 只是 info),所以 severity 由写入方按内容定级后落行,
不能从码推。分级依据 2026-08-30 生产数据核对(90 天真实迁移分布):
  high:任意→TERMINATED(终局,90 天 10 次)/ store ACTIVE→SUSPENDED(31 次)
        / payment ACTIVE→INACTIVE(资金冻结,TRO 标志动作,37 次)≈ 每天 <1 条
  mid: 可售→不可售(影刀列,新鲜度差)及未知迁移
  info:全部恢复方向(→ACTIVE / →可售)——入账不推送,时间线要完整
        (查档案要能看到「被封 → 3 天后恢复」整个来回)

状态 diff 的三条铁规(② 号查询实证:INACTIVE/SUSPENDED 是常驻态不是异常态):
  只对**迁移**入账,绝不对状态本身入账;比较对象是**上一条非空观测**
  (影刀列 30 天 570 行空,严格比昨天会把跨空档的迁移漏掉);首次观测不算变化。
"""

import json
import logging

logger = logging.getLogger("services.store_events")

# ── 事件码常量(唯一出处;新增先在此登记)──────────────────────────────────────
STORE_STATUS_CHANGED = "store_status_changed"      # risk
PAYMENT_STATUS_CHANGED = "payment_status_changed"  # risk
SALES_STATUS_CHANGED = "sales_status_changed"      # risk
# TRO 品牌(product_audit 接线,2026-08-30):**源头一条 + 波及逐店**。
# 源头 store=NULL —— 「这个 TRO 品牌被我们上过架」不属于任何一家店,它是
# 全局事实;哪几家店挨着由 services/risk_trace 四证据源展开成 exposure 行。
# 两个码分开,是为了让"发现了几个 TRO 品牌"与"波及了几家店"两个数各查各的
# (合成一个码之后,按 store IS NULL 过滤才数得出源头,谁都会忘)。
TRO_BRAND_HIT = "tro_brand_hit"                    # risk(源头,store=NULL)
TRO_BRAND_EXPOSURE = "tro_brand_exposure"          # risk(波及,逐店)
# 钓鱼订单(order_audit 接线,2026-08-30):**收单店一条 + 品牌波及逐店**。
# 与 TRO 那对的形状故意不同 —— 钓鱼是**订单**级事件,收单店是确定的(货真发到
# 那个黑名单邮编去了),所以源头行带 store;波及问的是"同品牌的货还在哪几家店
# 挂着"(黑产盯的是品牌不是店)。身份键是 detail.order_line_id。
PHISHING_ORDER = "phishing_order"                  # risk(收单店,逐订单行)
PHISHING_BRAND_EXPOSURE = "phishing_brand_exposure"  # risk(波及,逐店)

# ── 治理类(services/store_config 的配置快照 diff + 占用两个动作)────────────
# 「谁把这家店的类目改了」「这个品牌什么时候归的它」——出事那天回查的就是这些。
# 全部由**人的动作**产生,极低频:配置 diff 一天一轮多半零事件,占用只在
# alloc_plan/alloc_backfill/store_release 三条链的真跑里落。
STORE_LIMITS_CHANGED = "store_limits_changed"        # governance(一列一条)
STORE_LIMITS_ROW_ADDED = "store_limits_row_added"    # governance(限额表新增店行)
STORE_LIMITS_ROW_REMOVED = "store_limits_row_removed"  # governance(店行消失)
STORE_ENABLED_CHANGED = "store_enabled_changed"      # governance(凭证表「启用」)
STORE_REGISTERED = "store_registered"                # governance(凭证表新增店)
STORE_DEREGISTERED = "store_deregistered"            # governance(凭证表删店)
STORE_SCOPE_CHANGED = "store_scope_changed"          # governance(规划外名单,store=NULL)
CLAIM_CREATED = "claim_created"                      # governance(按店按轮汇总)
CLAIM_RELEASED = "claim_released"                    # governance(同上)

# ── 运营类(五条执行链,**每店每轮一条**)──────────────────────────────────
# 回答的是"这家店这一轮到底干了多少活":上了几条、清了几条、维护改了几处。
# ⚠ **绝不逐 SKU**:逐 SKU 是 catalog.product_events / ops.feed_items 的职责,
# 五条链每天几万行,逐条记几个月就是上千万行,而且把治理/风险两类淹没到
# 查不出来。severity 一律 info —— 干活是常态,不叫醒任何人;它们存在的意义
# 是**事后能按时间线对齐**:"封店那天这家店正在大批量删商品"这种事,
# 只有把运营流水和风险事件放在同一张表上才看得出来。
LIST_ROUND = "list_round"          # ops(list_new)
MAINT_ROUND = "maint_round"        # ops(maintenance)
CLEANUP_ROUND = "cleanup_round"    # ops(problem_product_cleanup)
CLEAR_ROUND = "clear_round"        # ops(product_clear)
MATCH_ROUND = "match_round"        # ops(match_listing)

# 分类表:码 → risk/governance/ops(消费方按类过滤;码登记时必须同时归类)
CLASS = {
    STORE_STATUS_CHANGED: "risk",
    PAYMENT_STATUS_CHANGED: "risk",
    SALES_STATUS_CHANGED: "risk",
    TRO_BRAND_HIT: "risk",
    TRO_BRAND_EXPOSURE: "risk",
    PHISHING_ORDER: "risk",
    PHISHING_BRAND_EXPOSURE: "risk",
    STORE_LIMITS_CHANGED: "governance",
    STORE_LIMITS_ROW_ADDED: "governance",
    STORE_LIMITS_ROW_REMOVED: "governance",
    STORE_ENABLED_CHANGED: "governance",
    STORE_REGISTERED: "governance",
    STORE_DEREGISTERED: "governance",
    STORE_SCOPE_CHANGED: "governance",
    CLAIM_CREATED: "governance",
    CLAIM_RELEASED: "governance",
    LIST_ROUND: "ops",
    MAINT_ROUND: "ops",
    CLEANUP_ROUND: "ops",
    CLEAR_ROUND: "ops",
    MATCH_ROUND: "ops",
}

EVENTS = frozenset(CLASS)

SEVERITIES = ("high", "mid", "info")


def _check(rows: list[dict]) -> None:
    """输入:事件行 → 输出:无(码/severity 不合法直接抛;两个写入口共用)。"""
    bad = sorted({r["event"] for r in rows} - EVENTS)
    if bad:
        raise ValueError(f"未登记的店铺事件码 {bad}:先在 services/store_events.py "
                         f"的常量与 CLASS 登记,再使用(唯一出处纪律)")
    bad_sev = sorted({r["severity"] for r in rows} - set(SEVERITIES))
    if bad_sev:
        raise ValueError(f"非法 severity {bad_sev}(可用:{SEVERITIES})")


def record_many(conn, rows: list[dict]) -> int:
    """输入:连接 + 事件行 [{store, event, severity, source, detail?}] → 输出:写入数。

    store 可为 None(全局源头事件);未登记的码 / 非法 severity 直接抛 ——
    账本只追加、消费方按精确字符串过滤,写错的行是永远没人查的分叉,宁炸不吞。
    **不去重**:调用方自己保证只喂新行(如 TRO 那条链先占 ops.dedupe 键)。
    要按订单行去重的走 `record_line_events`。
    """
    if not rows:
        return 0
    _check(rows)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ops.store_events (store, event, severity, source, detail) "
            "VALUES (%s, %s, %s, %s, %s::jsonb)",
            [(r.get("store"), r["event"], r["severity"], r["source"],
              json.dumps(r["detail"], ensure_ascii=False, default=str)
              if r.get("detail") is not None else None)
             for r in rows])
    return len(rows)


# ── 运营类:每店每轮一条(五条执行链共用的薄封装)───────────────────────────

def _has_activity(v) -> bool:
    """输入:计数字典里的一个值 → 输出:它算不算"这一轮真发生了什么"。

    数字按非零算,dict 递归下去(维护链按动作类分桶,计数嵌在第二层)。
    True 也算(match_listing 的 `{"exception": True}` —— 整店炸掉本身就是
    这一轮发生的事,而它一个计数都没有);0 / False / 空串不算。
    """
    if isinstance(v, dict):
        return any(_has_activity(x) for x in v.values())
    if isinstance(v, (list, tuple, set)):
        return any(_has_activity(x) for x in v)
    return bool(v)


def record_round(conn, source: str, event: str,
                 per_store: dict[str, dict]) -> int:
    """输入:连接 + 来源 + 事件码 + {店铺: 计数字典} → 输出:落行数。

    每店一条,detail = 计数字典**原样**(不复制任何流水:哪几个 SKU 归
    catalog.product_events,哪几片 feed 归 ops.feed_items,这里只记数)。

    ⚠ **计数全为 0 的店不落行**:五条链每轮都会遍历一批店,其中大半是
    "领到 0 条建议""这轮没它的货"—— 没活干不是事件。不滤掉的话账本每天
    多出几十条全零行,风险与治理两类会被淹到查不出来。
    """
    rows = [{"store": st, "event": event, "severity": "info",
             "source": source, "detail": cnt}
            for st, cnt in sorted(per_store.items())
            if _has_activity(cnt)]
    return record_many(conn, rows)


def record_round_safe(source: str, event: str, per_store: dict[str, dict],
                      lines: list[str] | None = None) -> int:
    """输入:同 `record_round`(自开连接)+ 可选摘要行 → 输出:落行数。

    **记账失败绝不拖垮业务链**(与 product_audit 的 TRO 接线同款纪律):
    货已经提交出去了,账本缺一轮是可以补的,而整轮炸掉不可以。
    但**失败必须见人** —— 告警 + 摘要一行(兜底静默常态化 = 主路径已坏
    而没人知道,conventions §六)。连接本身也在 try 内:PG 连不上是这里
    最可能的失败方式。
    """
    from registry import db
    try:
        with db.pg_conn() as conn:
            return record_round(conn, source, event, per_store)
    except Exception as e:                                      # noqa: BLE001
        logger.error("%s 店铺事件账本本轮没记上(%d 店):%s",
                     source, len(per_store), e)
        if lines is not None:
            lines.append(f"  ⚠ 店铺事件账本本轮没记上({e})——"
                         f"业务动作已完成,只是这一轮的运营流水没进账本")
        return 0


# ── KPI 三状态列的迁移检测(纯函数,便于测试)──────────────────────────────────

#: 列名 → (事件码, 该列的"在营"值)。sales_status 是影刀前台抓的中文值。
_STATUS_COLS = {
    "store_status": (STORE_STATUS_CHANGED, "ACTIVE"),
    "payment_status": (PAYMENT_STATUS_CHANGED, "ACTIVE"),
    "sales_status": (SALES_STATUS_CHANGED, "可售"),
}

#: 终局值:进入即 high,不管从哪来(90 天 10 次,全部在 store_status)
_TERMINAL = frozenset({"TERMINATED"})


def _sev(col: str, old: str, new: str) -> str:
    """输入:列 + 迁移两端(已归一非空)→ 输出:severity。规则见头注分级依据。"""
    _, active = _STATUS_COLS[col]
    if new in _TERMINAL:
        return "high"
    if new == active:
        return "info"                      # 恢复方向:入账不推送
    if old == active:
        # 从在营跌落:店铺/支付是 high(封店/冻结),销售列(影刀,新鲜度差)mid
        return "mid" if col == "sales_status" else "high"
    return "mid"                           # 其余(未知值之间的迁移)


def _norm(v) -> str | None:
    """输入:单元格原值 → 输出:btrim 后的值;空串归 None(空=没抓到,不是状态)。"""
    s = str(v or "").strip()
    return s or None


def kpi_status_events(store: str, data_date, prev: dict, new: dict) -> list[dict]:
    """输入:店铺 + KPI 日期 + 上一条非空观测 {列: 值} + 本轮值 → 输出:事件行列表。

    三列各自独立比,互不掺和。跳过规则(与头注三条铁规一一对应):
      prev 无值(首次观测)不算变化;new 为空(影刀没抓到)不算变化;
      值相同不入账。detail 只带 {old, new, data_date} —— 不复制 KPI 数值。
    """
    events = []
    for col, (code, _active) in _STATUS_COLS.items():
        old, cur = _norm(prev.get(col)), _norm(new.get(col))
        if old is None or cur is None or old == cur:
            continue
        events.append({
            "store": store, "event": code, "severity": _sev(col, old, cur),
            "source": "daily_report",
            "detail": {"old": old, "new": cur, "data_date": str(data_date)},
        })
    return events


def tro_signature(events: list[dict]) -> bool:
    """输入:同一店同一轮的事件行 → 输出:是否命中「疑似 TRO 封店」组合。

    封店(store ACTIVE→SUSPENDED/TERMINATED)与资金冻结(payment
    ACTIVE→INACTIVE)**同时**出现 = TRO 冻结的典型形状(所有者定稿
    2026-08-30)。两条事件照记两条,这里只负责认出组合、让通知把话说重。
    """
    kinds = {e["event"] for e in events if e["severity"] == "high"}
    return (STORE_STATUS_CHANGED in kinds and PAYMENT_STATUS_CHANGED in kinds)


# ── 落库(带同日去重:daily_report 一天可跑多轮,影刀下午还会补刷)────────────

# 同一 (店, 码, KPI 日, old, new) 只落一次。参数全带 ::text 显式铸型
# (services/dispositions 生产实炸三次的教训,tests 有 lint 用例同款盯法)。
_INSERT_DEDUP_SQL = """
INSERT INTO ops.store_events (store, event, severity, source, detail)
SELECT %(store)s::text, %(event)s::text, %(severity)s::text, %(source)s::text,
       %(detail)s::jsonb
WHERE NOT EXISTS (
    SELECT 1 FROM ops.store_events
    WHERE store = %(store)s::text AND event = %(event)s::text
      AND detail->>'data_date' = %(day)s::text
      AND detail->>'old' = %(old)s::text AND detail->>'new' = %(new)s::text)
"""

_PREV_SQL = """
SELECT
  (SELECT store_status FROM ops.store_kpi_daily
    WHERE store = %(store)s::text AND data_date < %(day)s::date
      AND nullif(btrim(store_status), '') IS NOT NULL
    ORDER BY data_date DESC LIMIT 1),
  (SELECT payment_status FROM ops.store_kpi_daily
    WHERE store = %(store)s::text AND data_date < %(day)s::date
      AND nullif(btrim(payment_status), '') IS NOT NULL
    ORDER BY data_date DESC LIMIT 1),
  (SELECT sales_status FROM ops.store_kpi_daily
    WHERE store = %(store)s::text AND data_date < %(day)s::date
      AND nullif(btrim(sales_status), '') IS NOT NULL
    ORDER BY data_date DESC LIMIT 1)
"""


def record_kpi_diff(conn, store: str, data_date, new_row: dict) -> list[dict]:
    """输入:连接 + 店铺 + KPI 日期 + 本轮行(含三状态列)→ 输出:实际落库的事件行。

    上一条非空观测**逐列各查各的**(_PREV_SQL 三个子查询):同一店三列的
    最近非空值可能在三个不同日期 —— 影刀列常年跳空,合在一行查会把它漏掉。
    同日去重在 SQL 里做(NOT EXISTS):一天多轮只有第一轮落行,当天内
    A→B→A 的来回因 old/new 不同仍各落各的。
    """
    with conn.cursor() as cur:
        cur.execute(_PREV_SQL, {"store": store, "day": data_date})
        p_ss, p_ps, p_ls = cur.fetchone()
    prev = {"store_status": p_ss, "payment_status": p_ps, "sales_status": p_ls}
    events = kpi_status_events(store, data_date, prev, new_row)
    landed = []
    with conn.cursor() as cur:
        for e in events:
            d = e["detail"]
            cur.execute(_INSERT_DEDUP_SQL, {
                "store": e["store"], "event": e["event"],
                "severity": e["severity"], "source": e["source"],
                "detail": json.dumps(d, ensure_ascii=False),
                "day": d["data_date"], "old": d["old"], "new": d["new"]})
            if cur.rowcount:
                landed.append(e)
    return landed


# ── 按订单行去重的落库(钓鱼订单接线;order_audit 每轮重判同一批行)──────────

# 身份键 = (事件码, 店, detail.order_line_id)。三样都要:
#   · 同一订单行会扇出**多家店**的波及行(店不同 = 不同的事实),所以 store
#     必须进键;而收单店那条 store 非空、波及那些也非空,但**将来若有 store=NULL
#     的行**,`=` 会静默变成 NULL 比较(永远不 TRUE)⇒ 每轮重复插一行。
#     所以用 `IS NOT DISTINCT FROM` 而不是 `=`(TRO 源头行就是 store=NULL,
#     那条链吃过这个亏才特意分了两个 dedupe scope)。
#   · order_line_id 从 detail 里取:store_events 没有订单列,也不该为一条链加列。
_INSERT_LINE_DEDUP_SQL = """
INSERT INTO ops.store_events (store, event, severity, source, detail)
SELECT %(store)s::text, %(event)s::text, %(severity)s::text, %(source)s::text,
       %(detail)s::jsonb
WHERE NOT EXISTS (
    SELECT 1 FROM ops.store_events
    WHERE event = %(event)s::text
      AND store IS NOT DISTINCT FROM %(store)s::text
      AND detail->>'order_line_id' = %(line)s::text)
"""


def record_line_events(conn, rows: list[dict]) -> list[dict]:
    """输入:连接 + 事件行(detail 里**必须**有 order_line_id)→ 输出:真落库的行。

    order_audit 每轮把窗口内的行重判一遍(`-p wait=1` 时同一批还判两遍),
    所以这条写入口必须自己幂等:去重在 SQL 里做(NOT EXISTS),同一
    (码, 店, 订单行) 只会有一行。返回的是**真落库的那些**,摘要按它报数 ——
    报"本轮又发现 N 条"却每轮都是同一条,比不报还坏。

    detail 缺 order_line_id 直接抛:那样的行会绕过去重每轮插一条,而且事后
    没有任何办法把它们认出来(账本只追加)。
    """
    if not rows:
        return []
    _check(rows)
    missing = [r for r in rows if not (r.get("detail") or {}).get("order_line_id")]
    if missing:
        raise ValueError(f"{len(missing)} 行 detail 缺 order_line_id:"
                         f"record_line_events 的去重键靠它,缺了就是每轮重复插")
    landed = []
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(_INSERT_LINE_DEDUP_SQL, {
                "store": r.get("store"), "event": r["event"],
                "severity": r["severity"], "source": r["source"],
                "detail": json.dumps(r["detail"], ensure_ascii=False,
                                     default=str),
                "line": r["detail"]["order_line_id"]})
            if cur.rowcount:
                landed.append(r)
    return landed


# ── 预警扫描面(workflows/store_watch 消费;写入方一概不看这一段)────────────
#
# ⚠ severity 写成**等值**不是风格:局部索引 store_events_unnotified_idx 是
# `(severity, occurred_at DESC) WHERE notified_at IS NULL`,首列 severity ——
# 等值才能把它当前缀用上并顺着第二列拿到有序结果。写成 `= ANY(...)`
# / `IN (...)` / `severity <> 'info'` 都退化成扫整个局部索引再排序,
# 而且**不报错**(账本小的时候一样快,大了才慢下来,那时没人记得是这里)。
#
# `FOR UPDATE SKIP LOCKED` 是 flock 之外的**第二道保险**(upc_pool.claim 先例):
# 手动跑一条、调度同时到点这种事,flock 挡得住;而 flock 是按工作流名的进程锁,
# 换个入口(未来的网页按钮、MCP 工具)就绕过去了。行锁挡的是"两轮同时扫到
# 同一批事件、各推一遍"。跳过被锁的行 = 那几条留给持锁的那一轮,不重不漏。
_SCAN_SQL = """
SELECT id, store, event, severity, source, detail, occurred_at
FROM ops.store_events
WHERE notified_at IS NULL
  AND severity = %(sev)s::text
  AND occurred_at >= now() - (%(hours)s::int * interval '1 hour')
ORDER BY occurred_at
LIMIT %(limit)s::int
FOR UPDATE SKIP LOCKED
"""

# 窗口内待推 / 窗口外滞留,一次扫描两个数(都走同一个局部索引)。
# 窗口外那个数**必须有人报**:时间窗是防"首轮上线被历史洪水刷屏"的闸,
# 但它同时意味着**超过窗口还没推出去的高危就永远推不出去了**。恒 0 时不打,
# 一旦 >0 就是"有漏网的",人该去看为什么(推送连着失败?限额一直吃不下?)。
_COUNTS_SQL = """
SELECT
  count(*) FILTER (
      WHERE occurred_at >= now() - (%(hours)s::int * interval '1 hour')),
  count(*) FILTER (
      WHERE occurred_at <  now() - (%(hours)s::int * interval '1 hour'))
FROM ops.store_events
WHERE notified_at IS NULL AND severity = %(sev)s::text
"""

# `AND notified_at IS NULL` 不是多余的:行锁只在本事务内挡得住并发,而这条
# UPDATE 的语义是"把**我这轮真推出去的**那些标掉"。加上它,任何时序意外
# (别处已标过)都只会少标一行,不会把别人的推送时间覆盖成我的。
_MARK_SQL = """
UPDATE ops.store_events SET notified_at = now()
WHERE id = ANY(%(ids)s::bigint[]) AND notified_at IS NULL
"""


def scan_unnotified(conn, severity: str = "high", hours: int = 48,
                    limit: int = 50) -> list[dict]:
    """输入:连接 + 级别/时间窗/上限 → 输出:待推送事件行(锁在本事务内)。

    行被 `FOR UPDATE SKIP LOCKED` 锁住,**必须在同一事务里推完并标记**:
    连接一提交锁就没了,别的轮次立刻能扫到同一批。
    """
    with conn.cursor() as cur:
        cur.execute(_SCAN_SQL, {"sev": severity, "hours": int(hours),
                                "limit": int(limit)})
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def unnotified_counts(conn, severity: str = "high",
                      hours: int = 48) -> tuple[int, int]:
    """输入:连接 + 级别/时间窗 → 输出:(窗口内待推数, 窗口外滞留数)。"""
    with conn.cursor() as cur:
        cur.execute(_COUNTS_SQL, {"sev": severity, "hours": int(hours)})
        n_in, n_out = cur.fetchone()
    return int(n_in), int(n_out)


def mark_notified(conn, ids: list[int]) -> int:
    """输入:连接 + 事件 id 列表 → 输出:真标上的行数(空列表 0 次查询)。"""
    if not ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(_MARK_SQL, {"ids": [int(i) for i in ids]})
        return cur.rowcount


def tro_stores(rows: list[dict]) -> list[str]:
    """输入:同一轮扫到的事件行 → 输出:命中「疑似 TRO 封店」组合的店铺(排序)。

    按 (店, detail.data_date) 分组再问 `tro_signature`:封店与资金冻结必须是
    **同一店同一天**的两条 —— 跨店凑、跨日凑都不算(A 店周一被封 + B 店周三
    冻结,合起来什么也不是)。store 为空的全局行(TRO 品牌源头)不参与分组,
    它不属于任何一家店。
    """
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        if not r.get("store"):
            continue
        groups.setdefault((r["store"], (r.get("detail") or {}).get("data_date")),
                          []).append(r)
    return sorted({st for (st, _day), evs in groups.items()
                   if tro_signature(evs)})


# ── 摘要渲染(**全事件码唯一出处**)──────────────────────────────────────────
#
# 一个码一句人话。收在这里而不是各消费方各写一份,是因为消费方已经有三个
# (daily_report 的状态节、store_watch 的推送明细、将来的店铺档案页),
# 而它们要说的是同一件事。
# ⚠ **兜底一律不许渲染成 `None→None`**:治理/TRO/钓鱼三族的 detail 里根本
# 没有 old/new 两个键,早先那版兜底 `f"{store} {event} {old}→{new}"` 对它们
# 全部输出 `? tro_brand_hit None→None` —— 通知照发、字数照占,人一个字
# 都读不出来。未登记特化的码退到"谁 + 码名",宁可干瘪不许骗人。

_STATUS_LABEL = {STORE_STATUS_CHANGED: "店铺", PAYMENT_STATUS_CHANGED: "支付",
                 SALES_STATUS_CHANGED: "销售"}

_ROUND_LABEL = {LIST_ROUND: "上架", MAINT_ROUND: "维护", CLEANUP_ROUND: "清理",
                CLEAR_ROUND: "下架", MATCH_ROUND: "跟卖"}


def _brief_body(event: str, d: dict) -> str:
    """输入:事件码 + detail → 输出:主体短句(不含店铺前缀);未特化返回空串。"""
    if event in _STATUS_LABEL:
        return f"{_STATUS_LABEL[event]} {d.get('old')}→{d.get('new')}"
    # ── 风险类:TRO / 钓鱼 ──
    if event == TRO_BRAND_HIT:
        judged = "已判定" if d.get("judged") else "嫌疑未判"
        return f"TRO 品牌「{d.get('brand')}」{judged}(首见 {d.get('first_asin')})"
    if event == TRO_BRAND_EXPOSURE:
        n = d.get("asin_total") or len(d.get("asins") or [])
        listed = "在架" if d.get("still_listed") else "不在架"
        return f"TRO 品牌「{d.get('brand')}」波及 {n} 品({listed})"
    if event == PHISHING_ORDER:
        who = d.get("po_id") or d.get("order_line_id")
        return f"钓鱼订单 {who}(邮编 {d.get('zip')})"
    if event == PHISHING_BRAND_EXPOSURE:
        listed = "在架" if d.get("still_listed") else "不在架"
        return (f"钓鱼品牌「{d.get('brand')}」波及({listed},"
                f"源 {d.get('origin_store')})")
    # ── 治理类 ──
    if event == STORE_LIMITS_CHANGED:
        return f"{d.get('field')}「{d.get('old')}」→「{d.get('new')}」"
    if event == STORE_ENABLED_CHANGED:
        return f"启用 {d.get('old')}→{d.get('new')}"
    if event == STORE_SCOPE_CHANGED:
        return f"规划外名单 {d.get('old')}→{d.get('new')}"
    if event == STORE_LIMITS_ROW_ADDED:
        return "限额表新增店行"
    if event == STORE_LIMITS_ROW_REMOVED:
        return "限额表店行消失(全表回落默认值)"
    if event == STORE_REGISTERED:
        return "凭证表新增在册"
    if event == STORE_DEREGISTERED:
        return "凭证表删店(不再在册)"
    if event in (CLAIM_CREATED, CLAIM_RELEASED):
        verb = "占用" if event == CLAIM_CREATED else "释放"
        got = [f"{k} {d[k]}" for k in ("brand", "product") if d.get(k)]
        scope = f",{d['scope']}" if d.get("scope") else ""
        return f"{verb} {'/'.join(got) or '0'}{scope}"
    # ── 运营类:detail 就是计数字典本身,原样摊平(键由各链自定,不在此登记)──
    if event in _ROUND_LABEL:
        got = ", ".join(f"{k} {v}" for k, v in sorted(d.items()))
        return f"{_ROUND_LABEL[event]}轮" + (f"({got})" if got else "")
    return ""


def brief(e: dict) -> str:
    """输入:事件行 → 输出:摘要用短句(`店铺 主体`;全事件码通用)。"""
    body = _brief_body(e["event"], e.get("detail") or {})
    who = e.get("store") or ("全局" if e["event"] in
                             (TRO_BRAND_HIT, STORE_SCOPE_CHANGED) else "?")
    return f"{who} {body or e['event']}"
