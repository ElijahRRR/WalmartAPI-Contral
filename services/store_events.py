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


def brief(e: dict) -> str:
    """输入:事件行 → 输出:摘要用短句(店铺 列 old→new)。"""
    col = {STORE_STATUS_CHANGED: "店铺", PAYMENT_STATUS_CHANGED: "支付",
           SALES_STATUS_CHANGED: "销售"}.get(e["event"], e["event"])
    d = e.get("detail") or {}
    return f"{e.get('store') or '?'} {col} {d.get('old')}→{d.get('new')}"
