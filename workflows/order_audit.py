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

采集接入(2026-08-09 接线):
- 审核输入来自 catalog.latest_snapshot 里**该订单收件邮编那一组**快照,两层
  JOIN 取标题(services.order_audit.from_snapshot 是唯一翻译点)。缺快照 →
  该行「待人工(待采集)」并进入本轮待采清单。
- **同一 ASIN 的不同邮编严禁同批提交**(所有者定稿:采集侧按 ASIN 唯一存结果,
  同批会互相覆盖丢数据)。故每轮每个 ASIN 只放行一个邮编、一个邮编一个批次,
  同 ASIN 的其余邮编等下一轮——本工作流按小时跑,多邮编的单几轮内收敛。
- **先落 pending 再调接口**(CLAUDE.md 铁律):台账 ops.audit_scrape 一个
  (ASIN,邮编) 一行。每轮开工先对账——requested_at 之后该组合真出现了新快照
  才算 done(不看批次状态:批次说成功但数据没落地照样要抓出来);超 3 小时
  仍无快照判失败,下轮重推。进程中途死掉不丢状态,这正是旧系统缺的那块。
- 采集侧 `zip_verify == "mismatch"` 的快照直接判废(切邮编失败拿回的是默认
  地区价格,拿它算限价等于按错地区审单)。
"""

import hashlib
import json
import logging
from datetime import datetime
from decimal import Decimal

import httpx

from api import feishu, scraper
from registry import db, resources
from services import kpi, order_audit as rules

DANGEROUS = False

logger = logging.getLogger("workflows.order_audit")

_DEFAULT_DAYS = 3
_SCRAPE_TIMEOUT_HOURS = 3      # 在途超此时长仍无快照 → 判失败,下轮重推
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
# 标题在身份层(products),两层 JOIN 才拿得到——商品一致性要用它。
_SNAP_SQL = """
SELECT s.asin, s.price, s.stock_count, s.delivery_days, s.buybox,
       s.scrape_params, s.raw, s.outcome, s.scraped_at, p.title
FROM catalog.latest_snapshot s
LEFT JOIN catalog.products p
       ON p.marketplace = s.marketplace AND p.asin = s.asin
WHERE s.marketplace = 'US' AND s.asin = ANY(%(asins)s)
"""

# ── 采集台账 ──────────────────────────────────────────────────────────────────
_INFLIGHT_SQL = """
SELECT asin, zip, batch_name, requested_at
FROM ops.audit_scrape WHERE state = 'pending'
"""

# 落定判据:requested_at 之后该 (ASIN, 邮编) 真的出现了新快照。
# 不看批次状态——批次说成功但数据没落地的情况照样要被抓出来。
_SETTLE_SQL = """
UPDATE ops.audit_scrape a
SET state = 'done', settled_at = now()
WHERE a.state = 'pending' AND EXISTS (
    SELECT 1 FROM catalog.snapshots s
    WHERE s.marketplace = 'US' AND s.asin = a.asin
      AND s.scrape_params ->> 'zipcode' = a.zip
      AND s.scraped_at >= a.requested_at)
"""

_TIMEOUT_SQL = """
UPDATE ops.audit_scrape
SET state = 'failed', reason = '超时未见快照', settled_at = now()
WHERE state = 'pending'
  AND requested_at < now() - make_interval(hours => %(hours)s)
"""

_MARK_PENDING_SQL = """
INSERT INTO ops.audit_scrape (asin, zip, batch_name, state, requested_at)
VALUES (%s, %s, %s, 'pending', now())
ON CONFLICT (asin, zip) DO UPDATE SET
    batch_name = EXCLUDED.batch_name, state = 'pending', reason = NULL,
    requested_at = now(), settled_at = NULL
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


def _judge_all(conn, lines: list[dict], blacklist, suppliers):
    """输入:连接 + 待审行 + 配置 → 输出:([(行, 结论)], 待采 [(asin, 邮编)])。"""
    snaps = _snapshots(conn, lines)
    results: list = []
    want: list = []
    for line in lines:
        asin = (line.get("sku") or "").strip().upper()
        zip5 = rules.norm_zip(line.get("postal_code"))
        snap = snaps.get((asin, zip5)) if zip5 else None
        res = rules.judge(line, snap, suppliers, blacklist)
        # 缺快照才需要采;钓鱼行连采都不用采(已经终局了,省一次请求)
        if snap is None and res.status == rules.MANUAL and asin and zip5:
            want.append((asin, zip5))
        results.append((line, res))
    return results, want


def _settle_ledger(conn) -> tuple[int, int, set]:
    """输入:连接 → 输出:(本轮落定数, 判超时数, 仍在途的 {(asin, 邮编)})。

    每轮开工先对账——这就是"重启后先查实际状态再决定是否补交"(CLAUDE.md
    铁律)的落地:进程死了没关系,pending 记录还在,下轮照样能判断该不该重推。
    """
    with conn.cursor() as cur:
        cur.execute(_SETTLE_SQL)
        settled = cur.rowcount or 0
        cur.execute(_TIMEOUT_SQL, {"hours": _SCRAPE_TIMEOUT_HOURS})
        timed_out = cur.rowcount or 0
        cur.execute(_INFLIGHT_SQL)
        inflight = {(r[0], r[1]) for r in cur.fetchall()}
    conn.commit()
    if timed_out:
        logger.warning("采集台账:%d 个 (ASIN,邮编) 超 %d 小时未见快照,判失败"
                       "(下轮会重推)", timed_out, _SCRAPE_TIMEOUT_HOURS)
    return settled, timed_out, inflight


def _push_scrape(conn, want: list, inflight: set) -> str:
    """输入:连接 + 待采 pair + 在途 pair → 输出:推送结果摘要(一行)。

    **先落 pending 再调接口**:批次名先写进台账,再 POST。反过来的话
    (先推后记)网络一断就成了"推上去了但库里没记录",下轮重复推同一批。
    """
    todo = rules.plan_round(want, inflight)
    if not todo:
        waiting = len({a for a, _ in want} & {a for a, _ in inflight})
        return (f"推采集:0(在途 {len(inflight)},其中 {waiting} 个 ASIN 的"
                f"其余邮编排队中)" if inflight else "推采集:0(无待采)")

    stamp = datetime.now(kpi.CN_TZ).strftime("%Y%m%d-%H%M%S")
    # 一个邮编一个批次:同批只能有一个邮编(采集侧按 ASIN 唯一存结果,
    # 同批混邮编会互相覆盖丢数据——所有者定稿 2026-08-09)
    by_zip: dict[str, list[str]] = {}
    for asin, zip5 in todo:
        by_zip.setdefault(zip5, []).append(asin)

    pushed, failed, notes = 0, 0, []
    for zip5, asins in sorted(by_zip.items()):
        name = f"wm-audit-{zip5}-{stamp}"
        with conn.cursor() as cur:
            cur.executemany(_MARK_PENDING_SQL,
                            [(a, zip5, name) for a in asins])
        conn.commit()
        try:
            res = scraper.submit_batch(name, asins, zip_code=zip5,
                                       needs_screenshot=True)
            pushed += len(asins)
            notes.append(f"{zip5}×{len(asins)}")
            logger.info("推采集批次 %s:%d 个 ASIN(inserted=%s)",
                        name, len(asins), res.get("inserted"))
        except scraper.BatchExistsError as e:
            # 撞名 = 上次其实推成功了(v4 绝不静默合并):台账保持 pending 即可
            pushed += len(asins)
            notes.append(f"{zip5}×{len(asins)}(沿用既有批次 {e.batch_id})")
        except Exception as e:
            failed += len(asins)
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE ops.audit_scrape SET state = 'failed', "
                    "reason = %s, settled_at = now() "
                    "WHERE asin = %s AND zip = %s AND state = 'pending'",
                    [(str(e)[:200], a, zip5) for a in asins])
            conn.commit()
            logger.exception("推采集批次 %s 失败", name)
    out = f"推采集:{pushed} 个 ASIN({'、'.join(notes)})" if pushed else ""
    if failed:
        out += f";失败 {failed}"
    return out or f"推采集:全部失败({failed})"


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
            f.title_similarity: _num(d.get("title_similarity")),
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
    do_scrape = str(params.get("scrape", "1")).lower() not in {"0", "false", "no"}

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

        # ② 采集台账先对账(重启安全:pending 还在,能判断该不该重推)
        settled, timed_out, inflight = _settle_ledger(conn)

        results, want = _judge_all(conn, lines, blacklist, suppliers)
        saved = _save(conn, results)
        tally: dict[str, int] = {}
        for _, res in results:
            tally[res.status] = tally.get(res.status, 0) + 1

        # ③ 推采集:每轮每个 ASIN 只放行一个邮编(同批混邮编会漏数据)
        scrape_note = ""
        if do_scrape:
            scrape_note = _push_scrape(conn, want, inflight)

        # ④ 推送:窗口内所有已判定行(不止本轮新判的),漏推的行下轮自愈
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
    if want:
        parts.append(f"待采集 {len(want)} 行(该邮编下无快照)")
    if settled or timed_out:
        parts.append(f"采集落定 {settled}"
                     + (f",超时判失败 {timed_out}" if timed_out else ""))
    if scrape_note:
        parts.append(scrape_note)
    if do_push:
        parts.append(f"飞书回写 {pushed} 行"
                     + (f",{missing} 行尚未建出(等 order_center_push)" if missing else ""))
    return ";".join(parts)
