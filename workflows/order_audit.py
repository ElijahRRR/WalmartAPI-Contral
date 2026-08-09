"""order_audit — 沃尔玛订单审核(四道审核出结论,只建议不真拒单)。

用法:
  python cli.py order_audit                    # 审最近 3 天未出结论的行
  python cli.py order_audit -p days=7
  python cli.py order_audit -p stores=店铺A,店铺B
  python cli.py order_audit -p line=<order_line_id>   # 单行重审(忽略窗口)
  python cli.py order_audit -p recheck=1       # 连已有结论的行一起重审(钓鱼行除外)
  python cli.py order_audit -p push=0          # 只判定落库,不回写飞书

设计(PG 权威,飞书是人机界面):
- 判定与推送**两段解耦**:判定只挑"这轮该判的行",推送则把窗口内**所有已判定行**
  推一遍。这样销售订单表里还没建出的行(order_center_push 尚未推到),
  下一轮会自然补上飞书侧,不会因为 PG 已有结论就永远漏掉。
  代价是每轮重写窗口内已判定行——**窗口(days,默认 3)就是写放大的上限**,
  调大 days 前先想清楚这一点。
- 结论落 orders.order_lines 的 audit_status/audit_detail/audited_at
  (order_sync 的 upsert 只覆盖自己给出的列,拉单冲不掉审核结论)。
- 飞书侧只写 registry ORDER_SALES_AUDIT 登记的审核列,且**只更新不新建行**
  (feishu.update_by_key)——建行是 order_center_push 的职责。
  「建议采购日期」属人工域,不登记不写。「审核状态」是两条工作流都会写的
  唯一一列,但两边取的都是 order_lines.audit_status 同一个值,不会打架。
- **钓鱼行不可覆盖**(旧系统语义):结论含「钓鱼」二字的行,后续轮次一律跳过,
  复审须人工清空审核状态。
- 配置(黑名单邮编 / 采购方表)每次运行现读飞书,读不到直接失败——
  拿旧配置继续算钱比不出结论危险得多。

采集依赖:审核输入来自 catalog.latest_snapshot 里**该订单收件邮编那一组**快照
(services.order_audit.from_snapshot 是唯一翻译点)。没有对应邮编的快照 → 该行
结论为「待人工(待采集)」,并计入待采清单摘要;**推送采集尚未接线**
(采集器端点改造中),接上后由 product_refresh 带邮编推批次,本工作流不直连采集器。
"""

import hashlib
import json
import logging
from decimal import Decimal

import httpx

from api import feishu
from registry import db, resources
from services import order_audit as rules

DANGEROUS = False

logger = logging.getLogger("workflows.order_audit")

_DEFAULT_DAYS = 3
_SCREENSHOT_SCOPE = "order_audit:screenshot"   # ops.dedupe 作用域:URL → file_token

# 待审:窗口内、未取消、还没结论的行。sku 即 ASIN(catalog 侧同一约定)。
_PICK_SQL = """
SELECT order_line_id, store, sku, qty, product_amount, shipping_amount,
       postal_code, sale_status, audit_status
FROM orders.order_lines
WHERE order_date >= now() - make_interval(days => %(days)s)
  AND coalesce(sale_status, '') <> 'Cancelled'
  {store_filter}
  {audit_filter}
ORDER BY order_date DESC
"""

_ONE_SQL = """
SELECT order_line_id, store, sku, qty, product_amount, shipping_amount,
       postal_code, sale_status, audit_status
FROM orders.order_lines WHERE order_line_id = %(line)s
"""

# 已判定行(推送阶段用):窗口内有结论的全部行
_PUSH_SQL = """
SELECT order_line_id, audit_status, audit_detail
FROM orders.order_lines
WHERE order_date >= now() - make_interval(days => %(days)s)
  AND audit_status IS NOT NULL
  {store_filter}
"""

# 按 (asin, 邮编) 取最新快照:scrape_params 里的邮编参与"最新值"分组,
# 故同一 ASIN 不同邮编互不覆盖(catalog.latest_snapshot 的设计初衷)。
_SNAP_SQL = """
SELECT asin, price, stock_count, delivery_days, buybox, scrape_params,
       outcome, scraped_at
FROM catalog.latest_snapshot
WHERE marketplace = 'US' AND asin = ANY(%(asins)s)
"""


def _load_config() -> tuple[set[str], list[rules.Supplier]]:
    """输入:无 → 输出:(黑名单邮编集合, 启用的采购方列表);任一表未登记则抛错。"""
    sheet = resources.ZIP_BLACKLIST_SHEET.require()
    # 范围按实际行数取,不写死上限:旧系统写死 A1:A500,黑名单超 500 条即
    # 静默截断——漏掉的钓鱼邮编会一路放行到通过
    n_rows = max(feishu.sheet_row_count(sheet), 1)
    rows = feishu.sheet_values(sheet, f"A1:A{n_rows}")
    blacklist = rules.zip_blacklist(rows)

    table = resources.SUPPLIER_TABLE.require()
    recs = feishu.list_records(table)
    suppliers = rules.parse_suppliers(recs, table.fields)
    if not suppliers:
        raise RuntimeError(
            f"采购方表「{table.name}」没有任何启用行:审核算不出限价,拒绝出结论")
    logger.info("配置就绪:黑名单邮编 %d 条,启用采购方 %d 行",
                len(blacklist), len(suppliers))
    return blacklist, suppliers


def _snapshots(conn, lines: list[dict]) -> dict[tuple[str, str], dict]:
    """输入:连接 + 订单行 → 输出:{(asin, 邮编): 审核输入形状}。

    一次取回窗口内全部 ASIN 的最新快照,再按邮编分组落位——邮编不匹配的
    快照直接丢弃(**绝不拿别的邮编的价格当本单依据**,旧系统硬校验语义)。
    """
    asins = sorted({(r["sku"] or "").strip().upper() for r in lines if r.get("sku")})
    if not asins:
        return {}
    with conn.cursor() as cur:
        cur.execute(_SNAP_SQL, {"asins": asins})
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    out: dict[tuple[str, str], dict] = {}
    for row in rows:
        snap = rules.from_snapshot(row)
        if snap and snap.get("zip"):
            out[((snap["asin"] or "").upper(), snap["zip"])] = snap
    return out


def _judge_all(conn, lines: list[dict], blacklist, suppliers) -> tuple[list, int]:
    """输入:连接 + 待审行 + 配置 → 输出:([(行, 结论)], 待采集行数)。"""
    snaps = _snapshots(conn, lines)
    results, pending_scrape = [], 0
    for line in lines:
        asin = (line.get("sku") or "").strip().upper()
        zip5 = rules.norm_zip(line.get("postal_code"))
        snap = snaps.get((asin, zip5)) if zip5 else None
        res = rules.judge(line, snap, suppliers, blacklist)
        if snap is None and res.status == rules.MANUAL:
            pending_scrape += 1
        results.append((line, res))
    return results, pending_scrape


def _save(conn, results: list) -> int:
    """输入:连接 + [(行, 结论)] → 输出:落库行数(audit_status/detail/audited_at)。"""
    if not results:
        return 0
    payload = [(res.status, json.dumps({**res.detail, "note": res.note},
                                       ensure_ascii=False, default=str),
                line["order_line_id"])
               for line, res in results]
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE orders.order_lines "
            "SET audit_status = %s, audit_detail = %s::jsonb, audited_at = now(), "
            "    updated_at = now() "
            "WHERE order_line_id = %s", payload)
    conn.commit()
    return len(payload)


def _screenshot_token(conn, url: str) -> str | None:
    """输入:连接 + 截图 URL → 输出:飞书 file_token(失败返回 None,不阻断审核)。

    防重:URL → file_token 记 ops.dedupe,同一张图只上传一次(上传接口本身
    不幂等,重复上传会在飞书网盘堆垃圾)。下载/上传任一步失败只告警——
    截图是佐证材料,不该因为它拿不到就让整行审核失败。

    ⚠ 采集接线待办:现在按裸 URL 直取。若采集器改造后截图端点需要鉴权
    (X-Export-Token 之类),取图应改走 api/scraper 的带头请求,改这一处即可。
    """
    if not url:
        return None
    key = hashlib.sha256(url.encode()).hexdigest()[:32]
    with conn.cursor() as cur:
        cur.execute("SELECT meta->>'file_token' FROM ops.dedupe "
                    "WHERE scope = %s AND key = %s", (_SCREENSHOT_SCOPE, key))
        hit = cur.fetchone()
    if hit and hit[0]:
        return hit[0]
    try:
        resp = httpx.get(url, timeout=30.0, trust_env=False, follow_redirects=True)
        resp.raise_for_status()
        token = feishu.upload_media(resources.ORDER_SALES_AUDIT,
                                    f"{key}.jpg", resp.content)
    except (httpx.HTTPError, feishu.FeishuError, LookupError) as e:
        logger.warning("截图取回/上传失败(%s):%s", url, e)
        return None
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ops.dedupe (scope, key, meta) "
                    "VALUES (%s, %s, %s::jsonb) ON CONFLICT DO NOTHING",
                    (_SCREENSHOT_SCOPE, key,
                     json.dumps({"file_token": token, "url": url})))
    conn.commit()
    return token


def _num(v):
    """输入:PG 数值 → 输出:float(飞书数字字段不吃 Decimal);None 原样。"""
    return float(v) if isinstance(v, Decimal) else v


def _payload(conn, rows: list[dict]) -> dict[str, dict]:
    """输入:连接 + 已判定行 → 输出:{order_line_id: 飞书审核列载荷}。"""
    f = resources.ORDER_SALES_AUDIT.fields
    out: dict[str, dict] = {}
    for r in rows:
        d = r.get("audit_detail") or {}
        if isinstance(d, str):
            d = json.loads(d)
        fields = {
            f.audit_status: r["audit_status"],
            f.script_audit: d.get("note"),
            f.amz_price: _num(d.get("amz_price")),
            f.stock_qty: _num(d.get("stock_qty")),
            f.ship_method: d.get("ship_method"),
            f.ship_days: _num(d.get("ship_days")),
            f.seller: d.get("seller"),
            f.supplier: d.get("supplier"),
            f.price_cap: _num(d.get("price_cap")),
        }
        token = _screenshot_token(conn, d.get("screenshot_url"))
        if token:
            fields[f.screenshot] = [{"file_token": token}]
        out[r["order_line_id"]] = fields
    return out


def run(params: dict) -> str:
    """输入:params(days/stores/line/recheck/push)→ 输出:审核结果摘要。"""
    days = int(params.get("days", _DEFAULT_DAYS))
    stores = [s.strip() for s in str(params.get("stores", "")).split(",") if s.strip()]
    line_id = str(params.get("line", "")).strip()
    recheck = str(params.get("recheck", "")).lower() in {"1", "true", "yes"}
    do_push = str(params.get("push", "1")).lower() not in {"0", "false", "no"}

    blacklist, suppliers = _load_config()
    store_filter = "AND store = ANY(%(stores)s)" if stores else ""
    args = {"days": days, "stores": stores, "line": line_id,
            "manual": rules.MANUAL}

    with db.pg_conn() as conn:
        # ① 选待审行
        if line_id:
            sql, audit_note = _ONE_SQL, "单行"
        else:
            # 「待人工」不是终局结论,是"这轮还判不了"(多半在等采集):每轮都要
            # 重判,否则快照采回来了这行也永远不会被再看一眼。已出终局结论
            # (通过/建议拒绝)的行只有 recheck=1 才重判。
            audit_filter = "" if recheck else (
                "AND (audit_status IS NULL OR audit_status = %(manual)s)")
            sql = _PICK_SQL.format(store_filter=store_filter,
                                   audit_filter=audit_filter)
            audit_note = f"最近 {days} 天" + ("(含重审)" if recheck else "")
        with conn.cursor() as cur:
            cur.execute(sql, args)
            cols = [d[0] for d in cur.description]
            lines = [dict(zip(cols, r)) for r in cur.fetchall()]

        # 钓鱼行不可覆盖:已标钓鱼的行任何情况下都不再改写
        lines = [r for r in lines
                 if rules.PHISHING_MARK not in (r.get("audit_status") or "")]

        results, pending_scrape = _judge_all(conn, lines, blacklist, suppliers)
        saved = _save(conn, results)
        tally: dict[str, int] = {}
        for _, res in results:
            tally[res.status] = tally.get(res.status, 0) + 1

        # ② 推送:窗口内所有已判定行(不止本轮新判的),漏推的行下轮自愈
        pushed = missing = 0
        if do_push:
            push_sql = _PUSH_SQL.format(store_filter=store_filter)
            if line_id:
                push_sql = ("SELECT order_line_id, audit_status, audit_detail "
                            "FROM orders.order_lines "
                            "WHERE order_line_id = %(line)s "
                            "AND audit_status IS NOT NULL")
            with conn.cursor() as cur:
                cur.execute(push_sql, args)
                cols = [d[0] for d in cur.description]
                done = [dict(zip(cols, r)) for r in cur.fetchall()]
            if done:
                pushed, miss_keys = feishu.update_by_key(
                    resources.ORDER_SALES_AUDIT,
                    resources.ORDER_SALES_AUDIT.fields.key,
                    _payload(conn, done))
                missing = len(miss_keys)

    parts = [f"{audit_note}待审 {len(lines)} 行,落库 {saved}"]
    if tally:
        parts.append("结论:" + " / ".join(f"{k} {v}" for k, v in sorted(tally.items())))
    if pending_scrape:
        parts.append(f"待采集 {pending_scrape} 行(该邮编下无快照)")
    if do_push:
        parts.append(f"飞书回写 {pushed} 行"
                     + (f",{missing} 行尚未建出(等 order_center_push)" if missing else ""))
    return ";".join(parts)
