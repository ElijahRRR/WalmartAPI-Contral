"""订单中心六表投影积木(PG 权威,飞书是人机界面)。

⚠ **住在 services 而不是某个 workflow**(2026-08-16 搬家,所有者定稿:
「已经对接了飞书表的,在执行完成后都需要及时写入,不要做成单独的了」)。

搬之前的形态:六张表由 `order_center_push` 一条工作流统一推,而其余各链
(维护记录 / 停用删除表 / 上架表 / 跟卖表)都是自己跑完自己写。订单中心是
唯一的例外 —— 于是"数据已经在 PG 了但飞书还没更新"这个窗口只在订单域存在,
且长度取决于下一次调度什么时候跑。

搬完之后各归各的:

    order_sync       跑完 → push_sales() + push_keys()
    returns_sync     跑完 → push_returns()
    perf_problems    跑完 → push_perf()
    settlement_sync  跑完 → push_settlement()

`order_center_push` 工作流保留,退为**手动全量补推 / 对账入口**
(`-p reconcile=1` 那套),不再进调度。

设计(对齐用户既有六表结构,2026-08-06):
- 程序写四张:销售 ← order_lines;售后 ← return_lines;绩效 ← perf_event_spans
  (事件级,统计周期=首末拉取日);对账 ← settlement_by_line 跨账期合并
  (金额=合计,佣金率/优惠/账期=最近账期值;逐账期明细留在 PG)。
- 键补齐两张:主订单表、采购信息只建缺失的 order_line_id 行(只写键列),
  其余列全归人工与关联字段,永不更新。
- **任何表都不删行**(_sync_stateful 压根不实现删除):主订单表是永久枢纽,
  行间有关联字段,删行断链;滑出窗口的行只是停止刷新。
- 只覆盖 registry 登记的程序字段;人工列/采集列/关联字段不出现在载荷里,
  绝不会被碰。售后表键 = RMA号|order_line_id。
- 表格未在 .env 登记时逐表跳过并提示;字段契约见 docs/feishu_tables.md。

⚠ **写量与窗口无关**:`_sync_stateful` 是本地状态 + 行指纹驱动,每轮只建
"本地没有的键"、只更新"指纹变了的行",其余全部跳过,日常连飞书表都不拉。
所以按 90 天窗口每小时跑也只写几十到几百行 —— 别为了"省写入"去缩窗口,
缩了反而让滑出窗口的行停止刷新。
"""

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from api import feishu
from registry import db, resources
from services import kpi, order_lines

logger = logging.getLogger("services.order_center")

_SALES_SQL = """
SELECT order_line_id, store, po_id, line_number, sku, asin, product_name, qty,
       sale_status, audit_status, status_date, order_date, est_ship_date,
       est_delivery_date, product_amount, shipping_amount, cancel_reason,
       refund_amount, refund_comments, carrier, tracking_no, tracking_url,
       ship_name, phone, address1, address2, city, state, postal_code,
       country, updated_at
FROM orders.order_lines
WHERE order_date >= now() - make_interval(days => %s)
  -- 历史导入的残缺行不推飞书(所有者定稿 2026-08-15):它们只有下单时间/店铺/
  -- PO/SKU/品名/数量/金额,没有物流地址与审核结论,推过去运营看到的是半截行。
  -- 用 IS DISTINCT FROM 而非 <>:source 绝大多数是 NULL,`<> '历史数据'` 对
  -- NULL 求值为 NULL ⇒ 会把**全部 API 行**一起过滤掉。
  AND source IS DISTINCT FROM %s
"""

# asin 从 order_lines 借(return_lines 没这一列;订单行滚出库或孤儿退货时为
# NULL,飞书那格空着 —— 不猜)
_RETURNS_SQL = """
SELECT r.return_order_id, r.order_line_id, r.store, r.po_id, r.line_number,
       r.customer_order_id, r.sku, r.return_status, r.refund_status,
       r.return_method, r.refund_mode, r.is_keep_it, r.refund_total,
       r.return_reason, r.return_comment, r.return_by, r.return_created,
       r.last_modified, r.customer_name, r.customer_email, r.qty,
       r.refunded_qty, r.carrier, r.tracking_no, l.order_date, l.asin
FROM orders.return_lines r
LEFT JOIN orders.order_lines l USING (order_line_id)
WHERE r.return_created >= now() - make_interval(days => %s)
"""

# 绩效表全量:行数按 (po, metric) 事件去重,增长缓慢,自身有界。
# 只推挂上订单行的事件(所有者决策 2026-08-06):挂不上的 = PO 早于拉单窗口的
# 老单,报表每天仍会重报它们(滚动窗口),不能删库只能推送侧过滤;
# 滚出统计窗口后自然消失。PG 保留全量供统计。
# 最近一期原始行(detail/status)经 LATERAL 取;period 为 ISO 日期串,字符序即时间序
_PERF_SQL = """
SELECT s.store, s.po_id, s.metric, s.order_line_id, s.first_period,
       s.last_period, s.periods_seen, s.ever_accountable, s.still_active,
       l.order_date, le.detail, le.status, le.last_seen_at
FROM orders.perf_event_spans s
LEFT JOIN orders.order_lines l ON l.order_line_id = s.order_line_id
LEFT JOIN LATERAL (
    SELECT e.detail, e.status, e.last_seen_at FROM orders.perf_events e
    WHERE e.po_id = s.po_id AND e.metric = s.metric
    ORDER BY e.period DESC LIMIT 1) le ON true
WHERE s.order_line_id IS NOT NULL
"""

# 账期 MMDDYYYY:最近账期按 YYYY+MMDD 重排后取最大
_SETTLE_SQL = """
SELECT b.order_line_id, b.store, b.po_id, b.line_number, b.net_amount,
       b.product_amount, b.commission_amount, b.settle_status,
       b.last_settle_date, l.order_date,
       lp.period, lp.commission_rate, lp.original_commission,
       lp.commission_saving, lp.incentive, lp.updated_at
FROM orders.settlement_by_line b
LEFT JOIN orders.order_lines l USING (order_line_id)
LEFT JOIN LATERAL (
    SELECT s2.period, s2.commission_rate, s2.original_commission,
           s2.commission_saving, s2.incentive, s2.updated_at
    FROM orders.settlement_lines s2
    WHERE s2.order_line_id = b.order_line_id
    ORDER BY substr(s2.period, 5, 4) || substr(s2.period, 1, 4) DESC
    LIMIT 1) lp ON true
WHERE b.last_settle_date IS NULL
   OR b.last_settle_date >= current_date - %s
"""


# 程序不写的字段类型:人员/附件/关联/查找/公式/地理/群组/系统列——
# 这些列即便被登记进 registry 也整列跳过(计算列由飞书自己算)
_UNWRITABLE_TYPES = {11, 17, 18, 19, 20, 21, 22, 23,
                     1001, 1002, 1003, 1004, 1005}


def _conv(ftype: int, v):
    """输入:飞书字段类型码 + 规范值(_cell 产物)→ 输出:该类型可写的值。

    转不动时返回 None(调用方计数告警),绝不让整批 batch_create 因单列炸掉
    (飞书批量写整批原子,一列类型错 = 全表推不上,2026-08-06 首跑实证)。
    """
    if v is None:
        return None
    if ftype == 2:                          # 数字
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (int, float)):
            return v
        try:
            return float(str(v).strip())
        except ValueError:
            return None
    if ftype == 5:                          # 日期:只收毫秒时间戳
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if ftype == 7:                          # 复选框
        return v if isinstance(v, bool) else None
    if ftype == 15:                         # 超链接:要求 {link, text} 结构
        s = str(v)
        return {"link": s, "text": s}
    if ftype == 4:                          # 多选
        return v if isinstance(v, list) else [str(v)]
    if ftype in (1, 3, 13):                 # 文本/单选/电话:一律字符串
        if isinstance(v, bool):
            return "是" if v else "否"
        return str(v)
    return v                                # 未知类型原样试写


_HASH_FIELD = "同步指纹"     # 程序自建的变更检测列,人工请勿改动


def _adapt_rows(table, desired: dict[str, dict]) -> dict[str, dict]:
    """输入:表 + 规范载荷 → 输出:按表内**实际字段类型**转换后的载荷。

    先 list_fields 读真实类型再逐列适配(用户的表,类型不归我们定);
    表中不存在的列与计算/系统型列整列跳过并告警;键字段缺失直接报错
    (同步对齐锚点,不能没有)。「同步指纹」列缺失时自动创建
    (变更检测:指纹一致的行跳过不写,万行级表的写放大治理)。
    """
    types = {f["field_name"]: f["type"] for f in feishu.list_fields(table)}
    key_field = table.fields.key
    if key_field not in types:
        raise feishu.FeishuError(
            None, f"表「{table.name}」缺少键字段「{key_field}」,请在飞书补建该文本字段后重跑")
    if _HASH_FIELD not in types:
        feishu.create_field(table, _HASH_FIELD)
    skipped: dict[str, str] = {}
    lost: dict[str, int] = {}
    out: dict[str, dict] = {}
    for k, row in desired.items():
        new = {}
        for name, v in row.items():
            ftype = types.get(name)
            if ftype is None:
                skipped.setdefault(name, "表中不存在")
                continue
            if ftype in _UNWRITABLE_TYPES:
                skipped.setdefault(name, f"计算/系统型(type={ftype})")
                continue
            cv = _conv(ftype, v)
            if cv is None and v is not None:
                lost[name] = lost.get(name, 0) + 1
            new[name] = cv
        out[k] = new
    if skipped:
        logger.warning("表「%s」以下列不写入:%s", table.name, skipped)
    if lost:
        logger.warning("表「%s」以下列有值因类型不符被置空(建议核对字段类型):%s",
                       table.name, lost)
    return out


# ── 本地同步状态(ops.feishu_sync_state):日常零拉表 ─────────────────────────
# **这是飞书投影同步的唯一实现路径**(所有者定稿 2026-08-14)。
# 曾经并存过一份通用版 `api/feishu.sync_by_key` / `ensure_keys`(与本文件同一个
# 提交落地,零生产调用),违反 CLAUDE.md「每个能力只有一条实现路径」,已删除。
# 两者不是抄了两份,是两种策略:通用版**每轮全量拉飞书表**现建键→record_id 映射,
# 本地版拿一张 PG 状态表把这个开销换掉——订单中心六表是万行级高频推送,
# 每轮拉全表要走飞书分页并吃速率限制。代价是要维护状态一致性,所以配了自愈
# (写失败即清状态,下轮全量对账重建)。
# 通用版唯一值得留的是「登记错表守卫」,已移植进 _bootstrap_state。
# **将来若第二张表也要这套同步**:那时才把本文件的私有实现上收进 `services/`
# (状态表读写属持久化编排,不属 api 层职责),而不是重新写一份通用版。
# 只有一个消费方时先建积木 = 投机性抽象,等第二个消费方出现才知道该抽什么。
# 键 → (record_id, 上次写入指纹)。推送时现算载荷指纹与存的比:一致跳过,
# 不一致按 record_id 更新,新键建行并回存 record_id。
# 自愈:状态为空(首轮/被清)→ 全量拉表重建映射(record_id 取自飞书,
# 指纹取行内「同步指纹」列);任何写失败 → 清该表状态,下轮自动重建。
# 前提纪律(所有者承诺):六表不删行、不复制行、键列不手改。


def _load_state(table) -> dict[str, tuple[str, str | None]]:
    """输入:表 → 输出:{键: (record_id, 上次写入指纹)}。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT row_key, record_id, pushed_hash "
                    "FROM ops.feishu_sync_state WHERE table_id = %s",
                    (table.table_id,))
        return {k: (rid, h) for k, rid, h in cur.fetchall()}


def _save_state(table, entries: list[tuple[str, str, str | None]]) -> None:
    """输入:表 + [(键, record_id, 指纹)] → 输出:无(upsert)。"""
    if not entries:
        return
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ops.feishu_sync_state "
            "(table_id, row_key, record_id, pushed_hash) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (table_id, row_key) DO UPDATE SET "
            "record_id = EXCLUDED.record_id, pushed_hash = EXCLUDED.pushed_hash, "
            "updated_at = now()",
            [(table.table_id, k, rid, h) for k, rid, h in entries])


def _drop_state(table) -> None:
    with db.pg_conn() as conn:
        conn.execute("DELETE FROM ops.feishu_sync_state WHERE table_id = %s",
                     (table.table_id,))


def _bootstrap_state(table, *, with_hash: bool = True) -> dict:
    """全量拉表重建键→record_id 映射(首轮/自愈/对账),写回状态表。

    with_hash=False 用于主订单表/采购信息(表内无「同步指纹」列)。
    重复键(承诺不该出现)保留首条并告警,绝不静默。
    """
    key_field = table.fields.key
    names = [key_field] + ([_HASH_FIELD] if with_hash else [])
    state: dict[str, tuple[str, str | None]] = {}
    dupes = 0
    seen = 0
    for rec in feishu.list_records(table, field_names=names):
        seen += 1
        k = feishu._plain_text(rec["fields"].get(key_field)).strip()
        if not k:
            continue
        if k in state:
            dupes += 1
            continue
        h = (feishu._plain_text(rec["fields"].get(_HASH_FIELD)).strip() or None) \
            if with_hash else None
        state[k] = (rec["record_id"], h)
    if seen and not state:
        # 登记错表守卫(2026-08-14 从已删除的 feishu.sync_by_key 移植过来):
        # 表里明明有行,却**一行都读不出键字段** ⇒ 大概率 .env 里 table_id 填错、
        # 指向了别人的表。此时状态是空的,下一步会把窗口内全部行当"缺失"新建
        # ——把人家的表写坏,而且不可逆。宁可炸也不要写。
        raise feishu.FeishuError(
            None, f"表「{table.name}」现有 {seen} 行均无「{key_field}」字段值,"
                  f"疑似 table_id 登记错表,拒绝同步(会写坏对方数据)")
    if dupes:
        logger.warning("表「%s」对账发现 %d 行重复键(纪律要求不复制行),"
                       "已按首条对齐,请人工清理", table.name, dupes)
    _save_state(table, [(k, rid, h) for k, (rid, h) in state.items()])
    logger.info("表「%s」同步状态重建:%d 行", table.name, len(state))
    return state


def _sync_stateful(t, desired: dict[str, dict],
                   reconcile: bool = False) -> tuple[int, int, int]:
    """输入:表 + {键: 规范载荷} → 输出:(新建, 更新, 指纹一致跳过)。

    本地状态驱动:日常不拉飞书表;reconcile=True 强制清状态走全量对账。
    """
    adapted = _adapt_rows(t, desired)
    for f in adapted.values():
        f[_HASH_FIELD] = feishu._row_hash(f, _HASH_FIELD)
    if reconcile:
        _drop_state(t)
    state = _load_state(t) or _bootstrap_state(t)

    creates = {k: f for k, f in adapted.items() if k not in state}
    updates = {k: f for k, f in adapted.items()
               if k in state and state[k][1] != f[_HASH_FIELD]}
    skipped = len(adapted) - len(creates) - len(updates)
    try:
        if creates:
            ids = feishu.batch_create(t, list(creates.values()))
            _save_state(t, [(k, rid, creates[k][_HASH_FIELD])
                            for k, rid in zip(creates, ids)])
        if updates:
            feishu.batch_update(t, [{"record_id": state[k][0], "fields": f}
                                    for k, f in updates.items()])
            _save_state(t, [(k, state[k][0], f[_HASH_FIELD])
                            for k, f in updates.items()])
    except feishu.FeishuError:
        # 映射可能已与远端漂移(如行被手删):清状态,下轮全量对账自愈
        _drop_state(t)
        logger.warning("表「%s」写入失败,已清同步状态,下轮自动全量对账重建", t.name)
        raise
    logger.info("表「%s」同步:新建 %d,更新 %d,指纹一致跳过 %d",
                t.name, len(creates), len(updates), skipped)
    return len(creates), len(updates), skipped


def _cell(v):
    """输入:PG 单元值 → 输出:bitable 可写值(日期→毫秒,Decimal→float,None 原样)。

    None 必须保留并显式写入(更新语义:省略字段=保留飞书旧值,
    送 null 才是清空)。
    """
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, datetime):
        return int(v.timestamp() * 1000)
    if isinstance(v, date):
        return int(datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
                   .timestamp() * 1000)
    if isinstance(v, Decimal):
        return float(v)
    return v


def _detail_desc(detail, limit: int = 300) -> str | None:
    """输入:perf_events.detail(报表原始行 dict)→ 输出:人读摘要 "k:v; k:v"。"""
    if not isinstance(detail, dict):
        return None
    parts = [f"{k}:{str(v).strip()[:40]}" for k, v in detail.items()
             if str(v or "").strip()]
    return "; ".join(parts)[:limit] or None


def _fetch(sql: str, args: tuple) -> list[dict]:
    """输入:SQL + 参数 → 输出:行 dict 列表(列名 → 值)。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def push_sales(days: int, reconcile: bool = False) -> tuple[str, set[str]]:
    """输入:窗口天数 + 是否全量对账 → 输出:(结果行, 本窗口键集合)。

    键集合供主订单表/采购信息补齐。
    """
    t = resources.ORDER_SALES
    f = t.fields
    desired = {}
    for r in _fetch(_SALES_SQL, (days, order_lines.HISTORY_SOURCE)):
        desired[r["order_line_id"]] = {
            f.key: r["order_line_id"], f.order_date: _cell(r["order_date"]),
            f.store: r["store"], f.po_id: r["po_id"],
            f.line_number: r["line_number"], f.sku: r["sku"],
            f.source_key: r["asin"],
            f.product_name: r["product_name"], f.qty: _cell(r["qty"]),
            f.sale_status: r["sale_status"], f.audit_status: r["audit_status"],
            f.status_date: _cell(r["status_date"]),
            f.est_ship_date: _cell(r["est_ship_date"]),
            f.est_delivery_date: _cell(r["est_delivery_date"]),
            f.product_amount: _cell(r["product_amount"]),
            f.shipping_amount: _cell(r["shipping_amount"]),
            f.cancel_reason: r["cancel_reason"],
            f.refund_amount: _cell(r["refund_amount"]),
            f.refund_comments: r["refund_comments"],
            f.carrier: r["carrier"], f.tracking_no: r["tracking_no"],
            f.tracking_url: r["tracking_url"], f.ship_name: r["ship_name"],
            f.phone: r["phone"], f.address1: r["address1"],
            f.address2: r["address2"], f.city: r["city"], f.state: r["state"],
            f.postal_code: r["postal_code"], f.country: r["country"],
            f.pulled_at: _cell(r["updated_at"]),
        }
    c, u, s = _sync_stateful(t, desired, reconcile)
    return (f"销售订单:{len(desired)} 行(窗口 {days} 天),新建 {c} 更新 {u} 跳过 {s}",
            set(desired))


def push_returns(days: int, reconcile: bool = False) -> str:
    t = resources.ORDER_RETURNS
    f = t.fields
    desired = {}
    for r in _fetch(_RETURNS_SQL, (days,)):
        key = f"{r['return_order_id']}|{r['order_line_id']}"
        desired[key] = {
            f.key: key, f.order_line_id: r["order_line_id"],
            f.order_date: _cell(r["order_date"]), f.store: r["store"],
            f.rma: r["return_order_id"],
            f.customer_order_id: r["customer_order_id"],
            f.po_id: r["po_id"], f.line_number: r["line_number"],
            f.sku: r["sku"], f.source_key: r["asin"],
            f.return_status: r["return_status"],
            f.refund_status: r["refund_status"],
            f.return_method: r["return_method"],
            f.refund_mode: r["refund_mode"],
            f.refund_total: _cell(r["refund_total"]),
            f.return_reason: r["return_reason"],
            f.return_comment: r["return_comment"],
            f.return_by: _cell(r["return_by"]),
            f.return_created: _cell(r["return_created"]),
            f.last_modified: _cell(r["last_modified"]),
            f.customer_name: r["customer_name"],
            f.customer_email: r["customer_email"],
            f.qty: _cell(r["qty"]), f.refunded_qty: _cell(r["refunded_qty"]),
            f.carrier: r["carrier"], f.tracking_no: r["tracking_no"],
            f.is_keep_it: r["is_keep_it"],
        }
    c, u, s = _sync_stateful(t, desired, reconcile)
    return f"售后订单:{len(desired)} 行(窗口 {days} 天),新建 {c} 更新 {u} 跳过 {s}"


def push_perf(reconcile: bool = False) -> str:
    t = resources.ORDER_PERF
    f = t.fields
    desired = {}
    for r in _fetch(_PERF_SQL, ()):
        key = f"{r['po_id']}|{r['metric']}"
        span = (r["first_period"] if r["first_period"] == r["last_period"]
                else f"{r['first_period']} ~ {r['last_period']}")
        desired[key] = {
            f.key: key, f.order_line_id: r["order_line_id"],
            f.order_date: _cell(r["order_date"]), f.store: r["store"],
            f.po_id: r["po_id"],
            # 指标类型单选的预设选项是无 emoji 纯文本(订单中心v1 init_bitable
            # 实证:OTD/取消率/退货率…)——剥掉日报契约的 emoji 前缀再写,
            # 否则飞书自动建出一套带 emoji 的重复选项;未知指标原样
            f.metric: kpi.METRIC_LABELS.get(r["metric"], r["metric"])
                         .split(" ", 1)[-1],
            f.accountable: r["ever_accountable"],
            f.description: _detail_desc(r["detail"]),
            # 仍出现在最近一期报表 = 仍拖当前绩效分;消失 = 滚出官方统计窗口
            f.status: "影响中" if r["still_active"] else "已滚出窗口",
            f.period_span: f"{span}(共 {r['periods_seen']} 期)",
            f.detail: (json.dumps(r["detail"], ensure_ascii=False)[:1000]
                       if r["detail"] is not None else None),
            f.pulled_at: _cell(r["last_seen_at"]),
        }
    c, u, s = _sync_stateful(t, desired, reconcile)
    return f"绩效订单:{len(desired)} 行(全量),新建 {c} 更新 {u} 跳过 {s}"


def push_settlement(days: int, reconcile: bool = False) -> str:
    t = resources.ORDER_SETTLE
    f = t.fields
    desired = {}
    for r in _fetch(_SETTLE_SQL, (days,)):
        desired[r["order_line_id"]] = {
            f.key: r["order_line_id"], f.order_date: _cell(r["order_date"]),
            f.store: r["store"], f.po_id: r["po_id"],
            f.line_number: r["line_number"],
            f.settle_status: r["settle_status"],
            f.net_amount: _cell(r["net_amount"]),
            f.product_amount: _cell(r["product_amount"]),
            f.commission_amount: _cell(r["commission_amount"]),
            # 佣金率/优惠/账期为最近账期值,金额为跨账期合计
            f.commission_rate: _cell(r["commission_rate"]),
            f.original_commission: _cell(r["original_commission"]),
            f.commission_saving: _cell(r["commission_saving"]),
            f.incentive: r["incentive"], f.period: r["period"],
            f.settle_date: _cell(r["last_settle_date"]),
            f.pulled_at: _cell(r["updated_at"]),
        }
    c, u, s = _sync_stateful(t, desired, reconcile)
    return f"对账明细:{len(desired)} 行(窗口 {days} 天),新建 {c} 更新 {u} 跳过 {s}"


def push_keys(sales_keys: set[str], reconcile: bool = False) -> list[str]:
    """输入:销售窗口键集合 + 是否全量对账 → 输出:结果行 list(每表一行)。

    主订单表/采购信息:补齐销售窗口内缺失的 order_line_id 行(只写键列)。
    同样走本地状态:缺键按状态判断,不逐轮拉表;首轮/自愈时全量重建映射。
    """
    lines = []
    for t in (resources.ORDER_MAIN, resources.ORDER_PURCHASE):
        try:
            t.require()     # 未登记先跳过,别碰状态库
            if reconcile:
                _drop_state(t)
            state = _load_state(t) or _bootstrap_state(t, with_hash=False)
            missing = sorted(k for k in sales_keys if k and k not in state)
            if missing:
                try:
                    ids = feishu.batch_create(
                        t, [{t.fields.key: k} for k in missing])
                    _save_state(t, [(k, rid, None)
                                    for k, rid in zip(missing, ids)])
                except feishu.FeishuError:
                    _drop_state(t)
                    logger.warning("表「%s」键补齐失败,已清同步状态,"
                                   "下轮自动全量对账重建", t.name)
                    raise
            lines.append(f"{t.name}:键补齐 {len(missing)} 行")
        except LookupError as e:
            lines.append(f"{t.name}:跳过(未登记)— {e}")
    return lines


# 六表的程序侧表名(主订单/采购信息合称 keys —— 它们只补键不写内容)。
# **公开常量**:order_center_push 的参数校验要用它,别在工作流里抄第二份。
TABLES = ("sales", "returns", "perf", "settlement", "keys")

# 每条业务链负责哪张表(所有者定稿 2026-08-16:执行完就写)。
# 收在这里而不是各工作流里写死字符串:加一张表时只改这一处,
# 漏改的那条链会静默不写,而且看不出来。
BY_WORKFLOW = {
    "order_sync": ("sales", "keys"),
    "returns_sync": ("returns",),
    "perf_problems": ("perf",),
    "settlement_sync": ("settlement",),
}


def push_tables(names, days: int = 90, reconcile: bool = False
                ) -> tuple[list[str], list[str]]:
    """输入:表名列表(+窗口/对账)→ 输出:(结果行, 失败表名)。

    单表失败不挡其余表 —— 调用方决定"失败要不要算整轮失败"
    (手动补推入口算,业务链跑完顺手写的那次不算:数据已经在 PG 了)。
    """
    lines: list[str] = []
    failed: list[str] = []
    sales_keys: set[str] = set()
    for name in names:
        try:
            if name == "sales":
                line, sales_keys = push_sales(days, reconcile)
                lines.append(line)
            elif name == "returns":
                lines.append(push_returns(days, reconcile))
            elif name == "perf":
                lines.append(push_perf(reconcile))
            elif name == "settlement":
                lines.append(push_settlement(days, reconcile))
            elif name == "keys":
                if not sales_keys:
                    # 单独跑 keys 时销售没拉过,现查窗口键集合。
                    # ⚠ 两个占位符都要给 —— 搬家前这里只传了 (days,),
                    # `order_center_push -p table=keys` 一跑就是参数个数不匹配
                    sales_keys = {r["order_line_id"] for r in _fetch(
                        _SALES_SQL, (days, order_lines.HISTORY_SOURCE))}
                lines.extend(push_keys(sales_keys, reconcile))
        except LookupError as e:
            lines.append(f"{name}:跳过(未登记)— {e}")
        except feishu.FeishuError as e:
            logger.exception("表 %s 同步失败: %s", name, e)
            lines.append(f"{name}:失败 — {e}")
            failed.append(name)
    return lines, failed


def push_after(names, days: int = 90) -> str:
    """输入:本链负责的表名 → 输出:一行摘要。**业务链跑完顺手写飞书**用。

    与 `push_tables` 的差别只有一条:**永不抛错**。数据已经落 PG 了,
    飞书写失败不该让那一轮数据拉取被记成 failed —— 但必须说出来,
    否则表现成"跑成功了可表格没动"(所有者 2026-08-16 在日报上实遇过这种)。
    """
    try:
        lines, failed = push_tables(list(names), days)
    except Exception as e:                                      # noqa: BLE001
        logger.warning("飞书投影失败(数据已在 PG,不影响本轮): %s", e)
        return f"⚠ 飞书投影失败:{e}(数据已在 PG;补推 `python cli.py order_center_push`)"
    out = "飞书投影:" + ";".join(lines)
    if failed:
        out += (f" ⚠ 失败 {len(failed)} 张(数据已在 PG;"
                f"补推 `python cli.py order_center_push -p table={failed[0]}`)")
    return out
