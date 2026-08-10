"""order_audit — 沃尔玛订单审核(五道审核出结论,只建议不真拒单)。

用法:
  python cli.py order_audit                    # 审最近 3 天该判的行 + 推本轮采集
  python cli.py order_audit -p days=7
  python cli.py order_audit -p stores=店铺A,店铺B
  python cli.py order_audit -p line=<order_line_id>   # 单行重审(忽略窗口)
  python cli.py order_audit -p recheck=1       # 连终局结论的行一起重审(钓鱼行除外)
  python cli.py order_audit -p scrape=0        # 不推采集(只对账 + 判定)
  python cli.py order_audit -p push=0          # 只判定落库,不回写飞书
  python cli.py order_audit -p wait=1          # 等这批采完 + 就地摄取 + 重判

`-p wait=1`(所有者定稿 2026-08-10)——**一条命令出真结论**:
推采集 → 轮询批次到落定(20 分钟兜底)→ 就地跑增量摄取 → 重新对账重判 →
回写飞书。默认关:调度里每小时跑一轮,结论滞后一轮无所谓,而阻塞 20 分钟
的一次运行会把那一小时的位置占掉。手动排查、以及想立刻看到结论时开。
就地摄取**借 product_ingest 的 flock**——游标独占推进,两个进程同推会静默
丢掉中间一段(详见 services/runlock 与 services/product_ingest.pump)。

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

采集接入(2026-08-09 接线,2026-08-10 按所有者实测定稿):
- 审核输入来自 catalog.latest_snapshot 里**该订单收件邮编那一组**快照,两层
  JOIN 取标题(services.order_audit.from_snapshot 是唯一翻译点)。
- **快照超 24 小时视同没有**:审单看的是价格/库存/货期,这几个字段变得快;
  拿昨天以前的价格算限价,和拿错邮编的价格性质相近。
- **什么会进待采清单**:由 judge 显式标记 `rescrape` 的四种——没快照 /
  outcome≠ok / 缺配送方式或配送时长 / 运费没采到。**无匹配采购方、标题不符
  不进**(重采解决不了,只是白烧配额)。钓鱼行是终局,连采都不用采。
- **一批混不同 ASIN 的不同邮编**(逐 ASIN 带 `items[].zip_code`,采集侧邮编
  三档:逐 ASIN > 批次级 > 服务端默认)。唯一要拆批的是**同一 ASIN 的多个
  邮编**——`tasks` 上有 `UNIQUE(batch_id, asin)`,同批会被 400 拒掉。
  故拆波次,**所有波次同一轮内推完**,不跨轮等待。
  批次内一个 ASIN 只可能有一个邮编,所以 `(批次名, asin)` 已唯一定位一个
  (ASIN,邮编),截图落盘 `<批次名>/<asin>.png` 天然不冲突。
  (取数本身只走 `/api/export/incremental` 按 `scrape_params.zipcode` 分组,
   由 product_ingest 负责——批次只用来判完成度和取图。)
- **先落 pending 再调接口**(CLAUDE.md 铁律):台账 ops.audit_scrape 一个
  (ASIN,邮编) 一行。每轮开工先对账,三层判据各管一段:
  ① 快照真出现 → done(只有它能证明数据到了**我们**库里);
  ② 批次已落定(`tasks.open == 0 且 screenshots.open == 0`)仍无快照 → 认账
     失败,原因去 `/api/batches/{batch_id}/failures` 拿真值写进台账;
  ③ 兜底超时 20 分钟 → 只打在**批次已查不到**的组合上,防台账永远挂着。
  在途批次不判超时、不重推。进程中途死掉不丢状态,这正是旧系统缺的那块。
- **重试一天封顶**:同一组合首次请求超 24 小时仍拿不到可用数据就不再推,
  免得一个采不出来的 ASIN 每小时白烧一次配额;上次推送已过期则视为新需求,
  窗口重置(见 _BLOCKED_SQL 与 _MARK_PENDING_SQL)。
- 采集侧 `zip_verify == "mismatch"` 的快照直接判废(切邮编失败拿回的是默认
  地区价格,拿它算限价等于按错地区审单)。
- **截图**先用 `GET /api/screenshots?batch_name=` 拿整批清单,只对
  `status == "done"` 的去取图(其余状态那张图根本不存在,取只会撞 404)。
  三种结局分别处置:未就绪→本轮不写这一列、下轮再来;失败→记墓碑不再请求;
  done→取图上传飞书换 file_token。**截图从不阻断审核结论**。
- **运费**取 `fast.shipping`(采集侧同日追加,落 snapshots.shipping 列):
  FREE→0.0 是"确认免运费",N/A→NULL 是"这次没采到"⇒ **成本算不出来,转待人工**。
  绝不 `or 0`——当 0 的话成本偏小,本该拒的单被放行,而两侧都不报错。
"""

import json
import logging
import time
from datetime import datetime
from decimal import Decimal

import httpx

from api import feishu, scraper
from registry import db, resources
from services import (kpi, order_audit as rules, product_ingest as ingest,
                      runlock, scrape_batches as batches)

DANGEROUS = False

logger = logging.getLogger("workflows.order_audit")

_DEFAULT_DAYS = 3
_SCRAPE_TIMEOUT_MIN = 20       # 兜底超时(所有者定稿 2026-08-10:切邮编+截图
                               # 几分钟就跑完)。**主判据是批次 open==0**,
                               # 这个只在批次查不到时兜底,防台账永远挂着
_SNAPSHOT_FRESH_HOURS = 24     # 快照超此时长即视同没有(所有者定稿 2026-08-10)
_RESCRAPE_WINDOW_HOURS = 24    # 同一组合的重采窗口:超此时长仍拿不到可用数据就放弃
_SCREENSHOT_SCOPE = "order_audit:screenshot"   # ops.dedupe:批次名|ASIN → file_token

# 待审:窗口内、未取消、还没结论的行。sku 即 ASIN(catalog 侧同一约定)。
_PICK_SQL = """
SELECT order_line_id, store, sku, product_name, qty, product_amount,
       shipping_amount, postal_code, sale_status, audit_status, audit_detail
FROM orders.order_lines
WHERE order_date >= now() - make_interval(days => %(days)s)
  AND coalesce(sale_status, '') <> 'Cancelled'
  {store_filter}
  {audit_filter}
ORDER BY order_date DESC
"""

_ONE_SQL = """
SELECT order_line_id, store, sku, product_name, qty, product_amount,
       shipping_amount, postal_code, sale_status, audit_status, audit_detail
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
SELECT s.asin, s.price, s.stock_count, s.delivery_days,
       s.shipping, s.shipping_raw, s.buybox,
       s.scrape_params, s.raw, s.outcome, s.scraped_at, p.title
FROM catalog.latest_snapshot s
LEFT JOIN catalog.products p
       ON p.marketplace = s.marketplace AND p.asin = s.asin
WHERE s.marketplace = 'US' AND s.asin = ANY(%(asins)s)
  AND s.scraped_at >= now() - make_interval(hours => %(fresh)s)
"""

# ── 采集台账 ──────────────────────────────────────────────────────────────────
# 本轮**不可推**的组合,两类:
# ① 在途(pending):已经推过一次,等它落定,别重复推;
# ② 重试窗口已耗尽:首次请求超过 24 小时、且最近 24 小时内还在推——说明是同
#    一轮死磕(采不出来就是采不出来),再推只是每小时白烧一次配额。
#    反之若最近 24 小时都没推过,那是**新需求**(比如同邮编来了新订单),
#    窗口重置,允许再推。
# 两类分开标记:摘要要能区分"在等"和"已放弃"——混成一个数的话,
# 一个采不出来的 ASIN 堆了几百个就只显示"在途 N",没人看得出该去人工处理了。
_BLOCKED_SQL = """
SELECT asin, zip,
       CASE WHEN state = 'pending' THEN 'inflight' ELSE 'gaveup' END AS why
FROM ops.audit_scrape
WHERE state = 'pending'
   OR (first_requested_at < now() - make_interval(hours => %(retry)s)
       AND requested_at   >= now() - make_interval(hours => %(fresh)s))
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
      AND s.scraped_at >= a.requested_at
      -- 与判定侧同口径:切邮编失败(zip_verify=mismatch)的快照在
      -- from_snapshot 里被整条判废,等于这次采集没回来,不能算落定,
      -- 否则台账显示 done 而行仍卡在待采集,排障时对不上
      AND coalesce(s.scrape_params ->> 'zip_verify', '') <> 'mismatch')
"""

# 兜底超时:**只打在批次已不在途的组合上**。批次还在跑(pushed/running)时
# 判它失败会导致"采集侧正干着,我们这边已经重推一遍"——白烧一批配额。
# 批次能查到时,落定与失败原因都由 _reap_batches 按采集侧真实状态处理;
# 这条只兜"批次在采集侧查不到了 / 台账里压根没这个批次"的漏。
_TIMEOUT_SQL = """
UPDATE ops.audit_scrape a
SET state = 'failed', reason = '兜底超时(批次已不在途且始终无快照)',
    settled_at = now()
WHERE a.state = 'pending'
  AND a.requested_at < now() - make_interval(mins => %(mins)s)
  AND NOT EXISTS (SELECT 1 FROM ops.scrape_batches b
                  WHERE b.batch_name = a.batch_name
                    AND b.status IN ('pushed', 'running'))
"""

# 本工作流推的批次:批次名前缀区分,不碰 product_refresh 的 wm-refresh-*
_BATCH_PREFIX = "wm-audit-"

_OPEN_BATCHES_SQL = """
SELECT batch_name, batch_id, asin_count FROM ops.scrape_batches
WHERE status IN ('pushed', 'running') AND batch_name LIKE %(prefix)s
ORDER BY submitted_at
"""

# 批次落定后仍没拿到快照的组合 → 认账失败(原因由 /failures 给)
_STILL_PENDING_SQL = """
SELECT asin, zip FROM ops.audit_scrape
WHERE state = 'pending' AND batch_name = %(batch)s
"""

_FAIL_PAIR_SQL = """
UPDATE ops.audit_scrape SET state = 'failed', reason = %(reason)s,
       settled_at = now()
WHERE asin = %(asin)s AND zip = %(zip)s AND state = 'pending'
"""

_MARK_PENDING_SQL = """
INSERT INTO ops.audit_scrape (asin, zip, batch_name, state,
                              requested_at, first_requested_at, attempts)
VALUES (%(asin)s, %(zip)s, %(batch)s, 'pending', now(), now(), 1)
ON CONFLICT (asin, zip) DO UPDATE SET
    batch_name = EXCLUDED.batch_name, state = 'pending', reason = NULL,
    requested_at = now(), settled_at = NULL,
    attempts = ops.audit_scrape.attempts + 1,
    -- 上次推送已过期 ⇒ 这是新一轮需求,重试窗口从头算;否则保持原点,
    -- 好让"重试一天"真的是一天,而不是每推一次就续一天
    first_requested_at = CASE
        WHEN ops.audit_scrape.requested_at
             < now() - make_interval(hours => %(fresh)s) THEN now()
        ELSE ops.audit_scrape.first_requested_at END
"""


def _detail(row: dict) -> dict:
    """输入:带 audit_detail 的行 → 输出:解析后的 detail dict(缺失给空 dict)。"""
    d = row.get("audit_detail")
    if isinstance(d, str):
        try:
            d = json.loads(d or "{}")
        except ValueError:
            return {}
    return d if isinstance(d, dict) else {}


def _marked_phishing(row: dict) -> bool:
    """输入:订单行 → 输出:是否已被标过钓鱼(不可覆盖)。

    ⚠ 标记落在 **audit_detail.note**,不在 audit_status——status 是
    「✓ 通过 / 建议拒绝 / 待人工」三值封闭集(飞书单选字段要固定选项),
    钓鱼行的 status 就是「建议拒绝」,里面根本没有"钓鱼"二字。
    (查 status 是本函数存在前的写法,那样这道不可覆盖闸等于没有:
    黑名单里删掉一个邮编,历史钓鱼结论会被下一轮 recheck 悄悄抹掉——
    正是旧系统明列的坑。)status 也一并查:人工在库里手写过标记时仍然算数。
    """
    return (rules.PHISHING_MARK in (row.get("audit_status") or "")
            or rules.PHISHING_MARK in str(_detail(row).get("note") or ""))


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

    两条取值纪律:
    - **超 24 小时的快照视同没有**(SQL 里过滤):审单看的是价格/库存/货期,
      这几个字段变得快;拿昨天以前的价格算限价,和拿错邮编的价格性质相近。
    - **同一 (ASIN, 邮编) 可能有多行**:latest_snapshot 按整个 scrape_params
      jsonb 分组,而它还含 zip_observed / parse_engine 等——同一邮编换了解析
      引擎就是另一组,各留各的最新。所以这里必须按 scraped_at **取最新那条**,
      不能让字典后写覆盖先写(那等于随机挑一条,时好时坏且无法复现)。
    """
    asins = sorted({(r["sku"] or "").strip().upper() for r in lines if r.get("sku")})
    if not asins:
        return {}
    with conn.cursor() as cur:
        cur.execute(_SNAP_SQL, {"asins": asins, "fresh": _SNAPSHOT_FRESH_HOURS})
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    out: dict[tuple[str, str], dict] = {}
    for row in rows:
        snap = rules.from_snapshot(row)
        if not snap or not snap.get("zip"):
            continue
        key = ((snap["asin"] or "").upper(), snap["zip"])
        cur_best = out.get(key)
        if cur_best is None or _newer(snap.get("scraped_at"),
                                      cur_best.get("scraped_at")):
            out[key] = snap
    return out


def _newer(a, b) -> bool:
    """输入:两个采集时间 → 输出:a 是否比 b 新(取不出时间的一律不覆盖)。"""
    if a is None:
        return False
    if b is None:
        return True
    try:
        return a > b
    except TypeError:               # 类型混杂(字符串 vs datetime):不冒险换
        return False


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
        # 该不该重采由 judge 显式标记(res.rescrape):没快照、outcome≠ok、
        # 缺配送方式/时长、运费没采到——这四种重采能解决。无匹配采购方、
        # 标题不符那类重采也一样,不进清单(白烧配额)。
        # 钓鱼行是终局,连采都不用采,judge 那边本来就不标 rescrape。
        if res.rescrape and asin and zip5:
            want.append((asin, zip5))
        results.append((line, res))
    return results, want


def _reap_batches(conn) -> tuple[int, int, list[str]]:
    """输入:连接 → 输出:(落定批次数, 认账失败的组合数, 摘要行)。

    **批次生命周期由采集侧说了算**(所有者 2026-08-10 实测确认):

        completed ⇔ tasks.open == 0 AND screenshots.open == 0

    `open` = 既不是 done 也不是 failed 的数量。failed 算终态,所以一张永远
    截不出来的图不会把批次卡死(实测 1 done + 1 failed → completed)。

    在途批次(open > 0)**不判超时、不重推**——它还在干活,盲超时会误判重推、
    白烧一批(所以兜底超时那条 SQL 显式排除了在途批次的组合)。
    批次落定后仍没拿到快照的组合才是真失败,这时拉
    `/api/batches/{batch_id}/failures` 把**真实原因**(captcha / variant_offset /
    zip_switch_failed …)写进台账,而不是一律记"超时未见快照"——验证码
    (换时段可重试)和 404(该去删链接)的处置完全不同。
    """
    with conn.cursor() as cur:
        cur.execute(_OPEN_BATCHES_SQL, {"prefix": _BATCH_PREFIX + "%"})
        open_batches = cur.fetchall()
    settled_batches, failed_pairs, notes = 0, 0, []

    for name, batch_id, n in open_batches:
        try:
            st = scraper.batch_status(name)
        except LookupError:
            # 批次在采集侧查不到了(被清理/名字丢了)——交给兜底超时收尾
            batches.finish(name, "failed", None, None, "采集侧查不到该批次")
            notes.append(f"{name}:采集侧查不到")
            continue
        except Exception as e:
            logger.warning("批次 %s 状态查询失败(本轮跳过):%s", name, e)
            continue
        # 台账没记下 batch_id 时(推送时响应异常)从状态响应补:失败明细端点
        # 只认 batch_id,缺了这批的失败原因就永远问不出来
        batch_id = batch_id or st.get("batch_id")
        if not batches.is_settled(st):
            stats = st.get("stats") or {}
            shots = st.get("screenshots") or {}
            batches.record(name, batch_id, n, "running",
                           f"open={stats.get('open')} shots_open={shots.get('open')}")
            continue

        stats = st.get("stats") or {}
        batches.finish(name, "completed", stats.get("done"), stats.get("failed"),
                       f"screenshots={(st.get('screenshots') or {}).get('done')}")
        settled_batches += 1

        with conn.cursor() as cur:
            cur.execute(_STILL_PENDING_SQL, {"batch": name})
            stuck = cur.fetchall()
        if not stuck:
            continue
        # 批次采完了这些组合却没快照 ⇒ 真失败,去问原因
        summary, by_asin = batches.pull_failures(name, batch_id)
        for asin, zip5 in stuck:
            et = by_asin.get(asin)
            reason = (f"采集失败:{et}" if et
                      else "批次已采完但无快照(增量导出里没有这条)")
            with conn.cursor() as cur:
                cur.execute(_FAIL_PAIR_SQL,
                            {"asin": asin, "zip": zip5, "reason": reason})
            failed_pairs += 1
        notes.append(f"{name}:{len(stuck)} 个组合无快照({summary})")
    conn.commit()
    return settled_batches, failed_pairs, notes


def _settle_ledger(conn) -> tuple[int, int, dict, list[str]]:
    """输入:连接 → 输出:(落定数, 判失败数, {(asin,邮编): 'inflight'|'gaveup'}, 摘要)。

    每轮开工先对账——这就是"重启后先查实际状态再决定是否补交"(CLAUDE.md
    铁律)的落地:进程死了没关系,pending 记录还在,下轮照样能判断该不该重推。

    三层判据各管一段,**谁也替代不了谁**:
      快照真出现   → done(只有它能证明数据到了**我们**库里;批次 completed
                     不等于落库,中间还隔着增量导出 + product_ingest 两跳)
      批次已落定   → 该认账的失败(原因来自 /failures)
      兜底超时     → 批次查不到时防台账永远挂着
    """
    with conn.cursor() as cur:
        cur.execute(_SETTLE_SQL)
        settled = cur.rowcount or 0
    conn.commit()

    reaped, failed_pairs, notes = _reap_batches(conn)

    with conn.cursor() as cur:
        cur.execute(_TIMEOUT_SQL, {"mins": _SCRAPE_TIMEOUT_MIN})
        timed_out = (cur.rowcount or 0)
        cur.execute(_BLOCKED_SQL, {"retry": _RESCRAPE_WINDOW_HOURS,
                                   "fresh": _SNAPSHOT_FRESH_HOURS})
        blocked = {(r[0], r[1]): r[2] for r in cur.fetchall()}
    conn.commit()

    if timed_out:
        logger.warning("采集台账:%d 个 (ASIN,邮编) 超 %d 分钟兜底超时"
                       "(批次状态查不到),下轮会重推",
                       timed_out, _SCRAPE_TIMEOUT_MIN)
    if reaped:
        notes.insert(0, f"批次落定 {reaped} 个,认账失败 {failed_pairs} 个组合")
    return settled, timed_out + failed_pairs, blocked, notes


def _push_scrape(conn, want: list, blocked: dict) -> tuple[str, list]:
    """输入:连接 + 待采 pair + 本轮不可推的 pair → 输出:(摘要, 本轮推出的批次名)。

    **一批混不同 ASIN 的不同邮编**(逐 ASIN 带 `items[].zip_code`);只有同一
    ASIN 的多个邮编才拆到不同波次——库结构决定(`UNIQUE(batch_id, asin)`),
    见 services.order_audit.plan_waves。**所有波次同一轮内推完**,不跨轮等待。

    **先落 pending 再调接口**(CLAUDE.md 铁律):批次名先写进 ops.audit_scrape,
    再 POST。反过来的话(先推后记)网络一断就成了"推上去了但库里没记录",
    下轮重复推同一批。批次本身也要记 ops.scrape_batches——**batch_id 只有推送
    响应里有**,不当场记下来,以后就没法拉 `/failures` 问失败原因了。
    """
    waves = rules.plan_waves(want, set(blocked))
    held = [blocked[p] for p in {(a, z) for a, z in want} if p in blocked]
    inflight = held.count("inflight")
    gaveup = held.count("gaveup")
    if gaveup:
        # 单独喊出来:这些是"采了一天还是采不出来"的,系统不会再管,
        # 只能人工看。混在"在途"里就等于没人知道
        logger.warning("采集台账:%d 个 (ASIN,邮编) 已超 %d 小时重试窗口,"
                       "本轮起不再重推,需人工处理",
                       gaveup, _RESCRAPE_WINDOW_HOURS)
    if not waves:
        if not want:
            return "推采集:0(无待采)", []
        return ((f"推采集:0(待采 {len(want)} 行"
                 + (f",在途 {inflight}" if inflight else "")
                 + (f",**已放弃 {gaveup}**(超 {_RESCRAPE_WINDOW_HOURS}h 重试窗口,"
                    f"需人工)" if gaveup else "") + ")"), [])

    stamp = datetime.now(kpi.CN_TZ).strftime("%Y%m%d-%H%M%S")
    pushed, failed, notes, sent = 0, 0, [], []
    for i, wave in enumerate(waves, 1):
        # 单波(常态)不带序号:批次名是取图与排障的抓手,越简单越好
        name = (f"{_BATCH_PREFIX}{stamp}" if len(waves) == 1
                else f"{_BATCH_PREFIX}{stamp}-{i:02d}")
        with conn.cursor() as cur:
            cur.executemany(_MARK_PENDING_SQL,
                            [{"asin": a, "zip": z, "batch": name,
                              "fresh": _SNAPSHOT_FRESH_HOURS} for a, z in wave])
        conn.commit()
        zips = len({z for _, z in wave})
        try:
            res = scraper.submit_json(name, wave, needs_screenshot=True)
            batches.record(name, res.get("batch_id"), len(wave), "pushed",
                           f"inserted={res.get('inserted')}")
            pushed += len(wave)
            sent.append(name)
            notes.append(f"{len(wave)} 个/{zips} 邮编")
            got = res.get("per_asin_zip_count")
            if got is not None and int(got) != len(wave):
                # 逐 ASIN 邮编没被全盘采纳(格式被退回批次邮编)⇒ 那些 ASIN
                # 会按**服务端默认邮编**采回价格,拿它审单就是按错地区审
                logger.warning("批次 %s:逐 ASIN 邮编只认了 %s/%d 个"
                               "(invalid_zip_rows=%s),未认的会按默认邮编采,"
                               "结果对不上收件地址", name, got, len(wave),
                               res.get("invalid_zip_rows"))
            logger.info("推采集批次 %s:%d 个 ASIN / %d 个邮编"
                        "(batch_id=%s inserted=%s)", name, len(wave), zips,
                        res.get("batch_id"), res.get("inserted"))
        except scraper.BatchExistsError as e:
            # 撞名 = 上次其实推成功了(v4 绝不静默合并):台账保持 pending,
            # 顺手把既有 batch_id 记下来,不然这批的失败明细就查不了
            batches.record(name, e.batch_id, len(wave), "pushed",
                           "撞名沿用既有批次")
            pushed += len(wave)
            sent.append(name)          # 撞名 = 上次真推成功了,照样要等它
            notes.append(f"{len(wave)} 个(沿用既有批次)")
        except Exception as e:
            failed += len(wave)
            batches.record(name, None, len(wave), "failed", str(e)[:200])
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE ops.audit_scrape SET state = 'failed', "
                    "reason = %s, settled_at = now() "
                    "WHERE asin = %s AND zip = %s AND state = 'pending'",
                    [(str(e)[:200], a, z) for a, z in wave])
            conn.commit()
            logger.exception("推采集批次 %s 失败", name)
    wave_note = f",{len(waves)} 波" if len(waves) > 1 else ""
    out = (f"推采集:{pushed} 个 ASIN×邮编{wave_note}({'、'.join(notes)})"
           if pushed else "")
    if inflight:
        out += f";在途 {inflight} 未重推"
    if gaveup:
        out += (f";**已放弃 {gaveup}**(超 {_RESCRAPE_WINDOW_HOURS}h 重试窗口,"
                f"需人工)")
    if failed:
        out += f";失败 {failed}"
    return (out or f"推采集:全部失败({failed})"), sent


def _wait_for_batches(names: list, timeout_min: int) -> tuple[str, int]:
    """输入:本轮推出的批次名 + 超时分钟 → 输出:(摘要, 仍未落定的批次数)。

    只轮询 `GET /api/batches/{名}/status`,**不碰台账**——这一步的唯一问题是
    "采集侧还在跑吗"。台账对账必须等**摄取之后**再做:批次 completed 只说明
    采集侧干完了,数据还在增量流里,这时去对账会把每一条都判成"批次已采完
    但无快照",一轮全军覆没。

    退避从 3 秒涨到 30 秒:本地采集器几秒就完,别为了一个小批次死等 30 秒;
    真慢的批次也不该每 3 秒问一次。超时不是失败——已采到的照常进增量流,
    没采到的下一轮由台账三层判据处置。
    """
    if not names:
        return "", 0
    started = time.monotonic()
    deadline = started + timeout_min * 60
    pending = set(names)
    delay = 3.0
    while pending and time.monotonic() < deadline:
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * 1.5, 30.0)
        for name in sorted(pending):
            try:
                st = scraper.batch_status(name)
            except LookupError:
                # 采集侧查不到了:再等也没意义,交给对账那层认账
                pending.discard(name)
                continue
            except Exception as e:                  # 查不动:下一轮再问
                logger.warning("等批次 %s:状态查询失败(继续等):%s", name, e)
                continue
            if batches.is_settled(st):
                pending.discard(name)
        if pending:
            logger.info("等采集:%d/%d 批已落定,继续等(已 %.0f 秒)",
                        len(names) - len(pending), len(names),
                        time.monotonic() - started)
    mins = (time.monotonic() - started) / 60
    if pending:
        return (f"等采集:{len(names) - len(pending)}/{len(names)} 批落定,"
                f"**{len(pending)} 批超 {timeout_min} 分钟仍在跑**"
                f"(已采到的照常进增量流,其余下轮处置)"), len(pending)
    return f"等采集:{len(names)} 批全部落定(耗时 {mins:.1f} 分钟)", 0


def _ingest_now() -> str:
    """输入:无 → 输出:就地增量摄取的摘要(拿不到锁则说明原因)。

    **借的是 product_ingest 的活,就得借它的锁**:增量游标
    (`ops.cursors` name='product_ingest')是独占推进的,两个进程同时拉
    `/api/export/incremental` 并各自落 next_cursor,后写的会盖掉先写的,
    中间那段记录**永远不会再被拉一次**(游标只前进不回头)——两侧都不报错,
    只是产品中心少了一批数据。

    拿不到锁不是失败:说明 product_ingest 正在跑,数据照样会进来,
    只是本轮看不到,下轮再判。
    """
    with runlock.hold(ingest.CURSOR_NAME) as got:
        if not got:
            return ("就地摄取:跳过(product_ingest 正在跑,别和它抢游标);"
                    "数据仍会由它摄入,下轮再判")
        res = ingest.pump(scraper, db)
    return "就地" + ingest.pump_summary(res)


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


# 截图终态:这张图不会再有了,记墓碑别再问。**其余状态一律当"还没好"**
# ——未知状态按可重试处理,最多下轮再问一次;反过来把未知当终态,一次网络
# 抖动就永久放弃一张本来能拿到的图。
_SHOT_DEAD = {"failed", "gone", "expired", "cancelled", "canceled"}


def _shot_index(batch_names) -> dict[tuple[str, str], str]:
    """输入:批次名集合 → 输出:{(批次名, ASIN): 截图状态}。

    **按批次一次拿清单**(`GET /api/screenshots?batch_name=`),不逐 ASIN 试探:
    一批 50 个 ASIN 只有 10 张图好了,逐个试要发 50 次(40 次收 409),
    这里 1 次清单 + 10 次取图。批次名同时是隔离键(落盘 `<批次名>/<asin>.png`),
    所以同一 ASIN 的不同邮编批次各有各的图。

    查不到清单只当"这批本轮没有图"——截图是佐证材料,永不阻断审核结论。
    """
    out: dict[tuple[str, str], str] = {}
    for name in sorted(n for n in batch_names if n):
        try:
            items = scraper.screenshot_list(name)
        except (LookupError, RuntimeError, httpx.HTTPError) as e:
            logger.warning("截图清单查询失败(批次 %s,本轮跳过取图):%s", name, e)
            continue
        for it in items:
            asin = str(it.get("asin") or "").strip().upper()
            if asin:
                out[(name, asin)] = str(it.get("status") or "").strip().lower()
    return out


def _screenshot_token(conn, batch_name: str, asin: str, status: str) -> str | None:
    """输入:连接 + 批次名 + ASIN + 清单里的状态 → 输出:飞书 file_token(无则 None)。

    三种结局分别处置:
    - done → 取图上传飞书换 file_token,记 ops.dedupe(上传接口不幂等,
      重复上传会在飞书网盘堆垃圾)
    - 终态失败 → 记墓碑,以后不再为这张图发请求
    - 其余(还没好 / 清单里根本没这条)→ 本轮不写这一列,下轮再来

    截图是佐证材料:任何一步失败都只告警,绝不让整行审核失败。
    """
    if not batch_name or not asin:
        return None
    key = f"{batch_name}|{asin}"
    with conn.cursor() as cur:
        cur.execute("SELECT meta->>'file_token', meta->>'gone' FROM ops.dedupe "
                    "WHERE scope = %s AND key = %s", (_SCREENSHOT_SCOPE, key))
        hit = cur.fetchone()
    if hit:
        return hit[0] or None           # 已上传过,或已记墓碑(不再重试)
    if status in _SHOT_DEAD:
        logger.info("截图不会再有,记墓碑不再重试:%s(status=%s)", key, status)
        _remember(conn, key, {"gone": True, "reason": f"status={status}"})
        return None
    if status != "done":
        return None                     # 还没好 / 没这条:下轮再来,不记墓碑
    try:
        png = scraper.fetch_screenshot(batch_name, asin)
    except scraper.ScreenshotPending:
        return None                     # 清单说好了但取图说没好:下轮再来
    except scraper.ScreenshotGone as e:
        logger.info("截图不会再有,记墓碑不再重试:%s", e)
        _remember(conn, key, {"gone": True, "reason": str(e)[:200]})
        return None
    except (LookupError, RuntimeError, httpx.HTTPError) as e:
        logger.warning("取截图失败(%s):%s", key, e)
        return None
    try:
        token = feishu.upload_media(resources.ORDER_SALES_AUDIT,
                                    f"{asin}.png", png, mime="image/png")
    except (feishu.FeishuError, LookupError) as e:
        logger.warning("截图上传飞书失败(%s):%s", key, e)
        return None
    _remember(conn, key, {"file_token": token, "batch_name": batch_name,
                          "asin": asin})
    return token


def _remember(conn, key: str, meta: dict) -> None:
    """输入:连接 + 防重键 + 元信息 → 输出:无(写 ops.dedupe,已存在则不动)。"""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ops.dedupe (scope, key, meta) "
                    "VALUES (%s, %s, %s::jsonb) ON CONFLICT DO NOTHING",
                    (_SCREENSHOT_SCOPE, key, json.dumps(meta, ensure_ascii=False)))
    conn.commit()


def _num(v):
    """输入:PG 数值 → 输出:float(飞书数字字段不吃 Decimal);None 原样。"""
    return float(v) if isinstance(v, Decimal) else v


_BATCH_OF_SQL = """
SELECT asin, zip, batch_name FROM ops.audit_scrape
WHERE batch_name IS NOT NULL AND (asin, zip) = ANY(%(pairs)s)
"""


def _batch_names(conn, rows: list[dict]) -> dict[tuple, str]:
    """输入:连接 + 已判定行 → 输出:{(asin, 邮编): batch_name}(取图的抓手)。"""
    pairs = [(d.get("asin"), d.get("zip")) for d in map(_detail, rows)
             if d.get("asin") and d.get("zip")]
    if not pairs:
        return {}
    with conn.cursor() as cur:
        cur.execute(_BATCH_OF_SQL, {"pairs": pairs})
        return {(a, z): b for a, z, b in cur.fetchall()}


# 在途批次里没有图可拿(截图和任务一起在跑),问了也是白问。
# 同一 ASIN 的多邮编会拆波次,重采多的日子波次不止一个,少了这道过滤
# 就是每轮多发几个必然空手而归的清单请求。
_SETTLED_BATCH_SQL = """
SELECT batch_name FROM ops.scrape_batches
WHERE batch_name = ANY(%(names)s) AND status NOT IN ('pushed', 'running')
"""


def _settled_batches(conn, names: set) -> set:
    """输入:连接 + 批次名集合 → 输出:其中已不在途的那些(值得去问截图的)。"""
    real = [n for n in names if n]
    if not real:
        return set()
    with conn.cursor() as cur:
        cur.execute(_SETTLED_BATCH_SQL, {"names": real})
        return {r[0] for r in cur.fetchall()}


def _payload(conn, rows: list[dict]) -> dict[str, dict]:
    """输入:连接 + 已判定行 → 输出:{order_line_id: 飞书审核列载荷}。"""
    f = resources.ORDER_SALES_AUDIT.fields
    batch_of = _batch_names(conn, rows)
    shots = _shot_index(_settled_batches(conn, set(batch_of.values())))
    out: dict[str, dict] = {}
    for r in rows:
        d = _detail(r)
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
        asin = (d.get("asin") or "").upper()
        batch = batch_of.get((d.get("asin"), d.get("zip")))
        token = (_screenshot_token(conn, batch, asin,
                                   shots.get((batch, asin), ""))
                 if batch and asin else None)
        if token:
            fields[f.screenshot] = [{"file_token": token}]
        out[r["order_line_id"]] = fields
    return out


def _yes(v) -> bool:
    return str(v).lower() in {"1", "true", "yes"}


def run(params: dict) -> str:
    """输入:params(days/stores/line/recheck/scrape/wait/push)→ 输出:审核摘要。"""
    days = int(params.get("days", _DEFAULT_DAYS))
    stores = [s.strip() for s in str(params.get("stores", "")).split(",") if s.strip()]
    line_id = str(params.get("line", "")).strip()
    recheck = _yes(params.get("recheck", ""))
    do_push = str(params.get("push", "1")).lower() not in {"0", "false", "no"}
    do_scrape = str(params.get("scrape", "1")).lower() not in {"0", "false", "no"}
    do_wait = _yes(params.get("wait", ""))

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

        # 钓鱼行不可覆盖:已标钓鱼的行任何情况下都不再改写(复审须人工清空)
        lines = [r for r in lines if not _marked_phishing(r)]

        # ② 采集台账先对账(重启安全:pending 还在,能判断该不该重推)
        settled, gone, blocked, batch_notes = _settle_ledger(conn)

        results, want = _judge_all(conn, lines, blacklist, suppliers)
        saved = _save(conn, results)
        tally: dict[str, int] = {}
        for _, res in results:
            tally[res.status] = tally.get(res.status, 0) + 1

        # ③ 推采集:一批混邮编,同一 ASIN 的多邮编拆波次
        scrape_note, sent = "", []
        if do_scrape:
            scrape_note, sent = _push_scrape(conn, want, blocked)

        # ③′ wait=1:等这批采完 → 就地摄取 → 重新对账重判(一条命令出结论)
        #
        # 顺序不能换。批次 completed 只说明**采集侧**干完了,数据还在增量流里;
        # 不先摄取就去对账,每一条都会被判成"批次已采完但无快照",一轮全军覆没。
        #     等批次落定 → 摄取(拿 product_ingest 的锁)→ 对账 → 重判
        wait_notes: list[str] = []
        if do_wait and sent:
            note, _stuck = _wait_for_batches(sent, _SCRAPE_TIMEOUT_MIN)
            wait_notes.append(note)
            wait_notes.append(_ingest_now())
            settled2, gone2, _blocked2, notes2 = _settle_ledger(conn)
            settled += settled2
            gone += gone2
            batch_notes.extend(notes2)
            # 重判的是**同一批行**:第一遍它们多半是"待采集/待人工",这遍才
            # 拿到真数据。已出终局结论的行 judge 会照常重算,不受影响。
            results, _want2 = _judge_all(conn, lines, blacklist, suppliers)
            saved = _save(conn, results)
            tally = {}
            for _, res in results:
                tally[res.status] = tally.get(res.status, 0) + 1

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
    if settled or gone:
        parts.append(f"采集落定 {settled}"
                     + (f",判失败 {gone}" if gone else ""))
    parts.extend(batch_notes)
    if scrape_note:
        parts.append(scrape_note)
    parts.extend(n for n in wait_notes if n)
    if do_push:
        parts.append(f"飞书回写 {pushed} 行"
                     + (f",{missing} 行尚未建出(等 order_center_push)" if missing else ""))
    return ";".join(parts)
