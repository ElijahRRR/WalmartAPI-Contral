"""order_audit — 沃尔玛订单审核(五道审核出结论,只建议不真拒单)。

用法:
  python cli.py order_audit                    # 审最近 3 天该判的行 + 推本轮采集
  python cli.py order_audit -p days=7
  python cli.py order_audit -p stores=店铺A,店铺B
  python cli.py order_audit -p line=<order_line_id>   # 单行重审(忽略窗口)
  python cli.py order_audit -p recheck=1       # 连终局结论的行一起重审(钓鱼行除外)
  python cli.py order_audit -p scrape=0        # 不推采集(只对账 + 判定)
  python cli.py order_audit -p push=0          # 只判定落库,不回写飞书
  python cli.py order_audit -p wait=0          # 不等采集(推完就回写,结论滞后一轮)
  python cli.py order_audit -p refill_shots=1  # 为台账无批次记录的行重采一次(只为补图)

**轮询默认开**(所有者定稿 2026-08-10)——一条命令出真结论:
推采集 → 轮询批次到落定(20 分钟兜底)→ 就地按批摄取 → 重新对账重判 →
回写飞书。默认开是因为**忘了加开关就只能看到上一轮的结论,而且不报错**;
这种"忘了就静默降级"的默认值不该留着。
代价:一次运行可能阻塞到 20 分钟。挂调度后若不想让它占住那一小时的位置,
在 plist 里显式写 `-p wait=0`(参数本来就要逐条写,不会漏)。
注意 cli.py 有 flock:阻塞期间再手动跑一次会直接退出(退出码 3),不排队。
就地摄取走采集侧**批次端点**(2026-08-19 起,`export_batch_records`):
只拉本工作流自己推的那几批,批内游标每次从 0 拉到底,不碰全局游标、
不需要 product_ingest 的锁(此前借锁抽全库,整点一到就和 13:00 的
product_chain 排队等锁)。幂等靠 snapshots.source_id。
⚠ `product_ingest` **仍要单独挂调度**:按批摄取只覆盖本工作流推的那批,
product_refresh 那条链(维护/上架用的快照)还是靠它摄。

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
  ② 批次已落定(**只看 `tasks.open == 0`**)且**该批自己的事件流已按批
     拉到底**仍无快照 → 认账失败,原因去
     `/api/batches/{batch_id}/failures` 拿真值写进台账。
     「先拉到底再认账」这道闸的来历是 2026-08-10 实测:批次 completed 只
     说明**采集侧**干完了,数据还在导出流里,当时 127 个组合全被判「批次
     已采完但无快照」,紧接着一次摄取就把这 127 条原样摄了进来——采集全
     成功,是我们问早了。且 failed 不挡重推,每小时白烧一轮,两侧都不报错。
     (2026-08-19 前这道闸是全局摄取水位线;换成按批拉到底后凭据更强:
     **这批的流里真没有你**,而不是"全局抽水抽过了你落定的时刻");
  ③ 兜底超时 20 分钟 → 只打在**批次已查不到**的组合上,防台账永远挂着。
  **且落定要稳住 15 分钟才认账**(所有者 2026-08-10 澄清采集侧机制):server 会
  把失败任务推回 worker 再循环两轮(每轮间隔 5 分钟),`tasks.open` 归零**不代表
  最终**,归零后还可能重新入队。所以 ① 复查状态的范围是"**所有还有 pending 组合
  的批次**"而不只是在途的(否则记成 completed 之后就再没人看它一眼,重新入队
  完全看不见);② 批次弹回在途时 `record()` 清空 finished_at,稳定窗口从零重计。
  在途批次不判超时、不重推。进程中途死掉不丢状态,这正是旧系统缺的那块。
  **落定判据不含 `screenshots.open`**(2026-08-10 所有者实测后改):失败任务
  的截图槽位不会再有人去截,shots_open 永久 >0,带上它就永远落不定。
  `-p wait=1` 在数据齐了之后另给截图 180 秒宽限,到点照常出结论,图下轮补贴。
- **`outcome` 是粗粒度的,真实原因看 `error_type`**(所有者 2026-08-10 澄清):
  增量导出里一条 `outcome="parse_failed"` 底下可能是 network / timeout /
  parse_error 任意一种,只有 `/api/batches/{id}/failures` 的 error_type 说得准。
  拿 outcome 当原因去决定"要不要再采",等于把可重试的和不可重试的混成一桶——
  前者放弃得太早,后者每轮白烧一次配额。判定链两处都按 error_type 分流。
- **重采无效的采集失败给终局结论**:`error_type` 落 ops.audit_scrape 单独一列,
  命中 `services.scrape_batches.TERMINAL`(采集侧 NO_AUTO_RETRY_ERROR_TYPES 的
  镜像:variant_offset / parse_error / server_reject …)⇒ **建议拒绝且不再重推**。
  这类失败 cap=1 首次即终态、自动与手动重试都跳过,重采一百次是同一个结果;
  挂"待采集"等于让行永远等一个不来的快照,每轮还白烧一次配额。
  `unknown` 故意不算终局——兜底桶,拿不准就转人工。
- **重试三天封顶**(2026-08-22 由一天放宽,对齐 `days` 默认 3 的审核窗口):
  同一组合首次请求超 72 小时仍拿不到可用数据就不再推,免得一个采不出来的
  ASIN 每小时白烧一次配额;上次推送已过期(按**快照新鲜度** 24h 判)则视为
  新需求,窗口重置(见 _BLOCKED_SQL 与 _MARK_PENDING_SQL)。
- 采集侧 `zip_verify == "mismatch"` 的快照直接判废(切邮编失败拿回的是默认
  地区价格,拿它算限价等于按错地区审单)。
- **截图**先用 `GET /api/screenshots?batch_name=` 拿整批清单,只对
  `status == "done"` 的去取图(其余状态那张图根本不存在,取只会撞 404)。
  三种结局分别处置:未就绪→本轮不写这一列、下轮再来;失败→记墓碑不再请求;
  done→取图上传飞书换 file_token。**截图从不阻断审核结论**。
- **运费**取 `fast.shipping`(采集侧同日追加,落 snapshots.shipping 列):
  FREE→0.0 是"确认免运费",N/A→NULL 是"这次没采到"⇒ **落地价算不出来,
  转待人工**。绝不 `or 0`——当 0 的话选到偏低的档、成本偏小,本该拒的单被
  放行,而两侧都不报错。
- **采购区间按亚马逊落地价(单价 + 运费)选档**(所有者定稿 2026-08-10):
  不是裸单价,更不是沃尔玛那边的销售金额——区间说的是"我们要花多少钱买"。
  与 `walmart_price` 同源(共用 `services.pricing.landed_price`),两处口径
  必须一致,否则同一件商品选档和定价各说各话。
- **限价一进门就算**(商品金额 × 0.75),不等采集也不等采购方:它只跟沃尔玛
  这一侧有关。原先放在采购方命中之后,导致「无匹配采购方」「待采集」的行在
  飞书上连限价列都是空的,人工复核连参照都没有。
- **商品一致性存疑的行也要分配采购方**(所有者定稿 2026-08-10):标题不符时
  结论**降级**为待人工而不是就地 return,后面的落地价/选档/成本照算完填进
  detail——人工要看到"如果这真是同一个商品,该找谁采、成本多少、过不过限价"。
  原不变量仍守住:商品可能不是同一个,所以绝不给通过/建议拒绝,原因里两条都写。
"""

import json
import logging
import math
import time
from datetime import datetime
from decimal import Decimal

import httpx

from api import feishu, scraper
from registry import db, resources
from services import (kpi, order_audit as rules, product_ingest as ingest,
                      scrape_batches as batches)
# ⚠ 按名字导入:run() 的形参就叫 params,`from services import params` 会被它
# 遮住,`params.flag(...)` 当场 AttributeError(services/params.py 头注)
from services.params import flag

DANGEROUS = False

logger = logging.getLogger("workflows.order_audit")

_DEFAULT_DAYS = 3
_SCRAPE_TIMEOUT_MIN = 20       # 兜底超时(所有者定稿 2026-08-10:切邮编+截图
                               # 几分钟就跑完)。**主判据是批次 open==0**,
                               # 这个只在批次查不到时兜底,防台账永远挂着
_SNAPSHOT_FRESH_HOURS = 24     # 快照超此时长即视同没有(所有者定稿 2026-08-10)
# 同一 (ASIN,邮编) 的重采窗口:超此时长仍拿不到可用数据就不再推。
# **对齐审核窗口**(所有者定稿 2026-08-22:1 天 → 3 天):待审行按 `days`
# (默认 3)挑,重采只死磕 1 天的话,后两天那些行挂着「待采集」而系统早就
# 放弃了 —— 窗口内一直试、窗口滑走自然收手,两个数才自洽。
# ⚠ 与 `_SNAPSHOT_FRESH_HOURS` 是**两件事,不要一起改**:那个管"快照多久算
# 陈旧",也是 _MARK_PENDING_SQL 里"上次推送已过期 ⇒ 视为新需求、窗口重置"
# 的判据。重采窗口拉长不该让"新需求"的门槛跟着变。
_RESCRAPE_WINDOW_HOURS = 72
_SCREENSHOT_SCOPE = "order_audit:screenshot"   # ops.dedupe:批次名|ASIN → file_token
_AUTO_RETRY_SETTLE_MIN = 15    # 批次 tasks.open 归零后还要稳住这么久才认账失败。
                               # 采集侧 server 会把失败任务推回 worker 再循环两轮
                               # (每轮间隔 5 分钟),归零**不代表最终**——所有者
                               # 2026-08-10 澄清。这道闸只管"认账失败",不影响
                               # 快照到了就 done,也不影响 -p wait=1 何时收工
_SHOT_GRACE_SEC = 180          # 数据采完后额外等截图的上限(所有者定稿 2026-08-10)。
                               # **截图不是落定条件**(任务失败后它那张图永远不会
                               # 好,当闸会永久卡住),只是"顺手多等一会儿能这轮
                               # 就贴上"。到点照常出结论,图下轮补

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
SELECT s.asin, s.price, s.stock_count, s.stock_state, s.delivery_days,
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
# ② 重试窗口已耗尽:首次请求超过 `retry`(72 小时,对齐 3 天审核窗口)、
#    且最近 `fresh`(24 小时)内还在推——说明是同一轮死磕(采不出来就是采
#    不出来),再推只是每小时白烧一次配额。
#    反之若最近 24 小时都没推过,那是**新需求**(比如同邮编来了新订单),
#    窗口重置,允许再推。两个时长故意不同:一个管"死磕多久",一个管
#    "隔多久算新的一轮"。
# 两类分开标记:摘要要能区分"在等"和"已放弃"——混成一个数的话,
# 一个采不出来的 ASIN 堆了几百个就只显示"在途 N",没人看得出该去人工处理了。
_BLOCKED_SQL = """
SELECT asin, zip,
       CASE WHEN state = 'pending' THEN 'inflight'
            WHEN state = 'failed' AND error_type IS NOT NULL
                 AND NOT (error_type = ANY(%(retryable)s)) THEN 'dead'
            ELSE 'gaveup' END AS why
FROM ops.audit_scrape
WHERE state = 'pending'
   OR (state = 'failed' AND error_type IS NOT NULL
       AND NOT (error_type = ANY(%(retryable)s)))
   OR (first_requested_at < now() - make_interval(hours => %(retry)s)
       AND requested_at   >= now() - make_interval(hours => %(fresh)s))
"""

# 该 (ASIN,邮编) 上次采集失败的类型 —— 判定链据此分流:
# 不可重采的给终局结论(建议拒绝),可重采的照旧挂"待采集"。
_SCRAPE_FAIL_SQL = """
SELECT asin, zip, error_type FROM ops.audit_scrape
WHERE state = 'failed' AND error_type IS NOT NULL
  AND (asin, zip) IN (SELECT * FROM unnest(%(asins)s::text[], %(zips)s::text[]))
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

# 要复查状态的批次:**只要还有 pending 组合就复查,不管台账记的是什么状态**。
#
# 不能只查 pushed/running(2026-08-10 所有者澄清后改):采集侧的 server 会把
# 失败任务推回 worker 再循环两轮,`tasks.open` 归零**不代表最终** —— 归零之后
# 还可能重新入队。只查在途批次的话,一个批次被我们记成 completed 之后就再也
# 不会被看一眼,重新入队完全看不见,而我们已经按"它结束了"去认账失败了。
_OPEN_BATCHES_SQL = """
SELECT b.batch_name, b.batch_id, b.asin_count
FROM ops.scrape_batches b
WHERE b.batch_name LIKE %(prefix)s
  AND EXISTS (SELECT 1 FROM ops.audit_scrape a
              WHERE a.batch_name = b.batch_name AND a.state = 'pending')
ORDER BY b.submitted_at
"""

# 可以认账的批次:已落定、还有 pending 组合、且落定已稳住窗口期。
#
# 认账前的最后一道闸(「这批的数据真到我们库里了吗」)不在这条 SQL 里:
# _reap_batches 会对每个候选批次**把它自己的事件流按批拉到底**
# (services.product_ingest.pump_batch),拉到底且落定之后仍 pending 的
# 才判失败。2026-08-19 前这道闸是"全局摄取水位线已越过落定时刻"——
# 那是 2026-08-10 的 127 条冤案(批次 completed ≠ 数据落库,问早了整批
# 误判失败 + 每小时白烧重采)踩出来的防线;按批拉到底给出更强的逐批凭据,
# 水位线不再参与认账(它仍由全局泵维护,只作运维观测)。
_REAPABLE_SQL = """
SELECT b.batch_name, b.batch_id
FROM ops.scrape_batches b
WHERE b.batch_name LIKE %(prefix)s
  AND b.status NOT IN ('pushed', 'running')
  AND b.finished_at IS NOT NULL
  -- 落定得**稳住**这么久才算数:采集侧 server 把失败任务推回 worker 再循环
  -- 两轮(每轮间隔 5 分钟),期间 tasks.open 会从 0 弹回 >0。第一次归零就认账
  -- 失败,等于在数据正要回来的路上判它死刑 —— 而 failed 不挡重推,下一轮还会
  -- 再采一遍,白烧配额。窗口内批次若真的重新入途,record() 会把 finished_at
  -- 清空,这张表从零重计。
  AND b.finished_at < now() - make_interval(mins => %(stable)s)
  AND EXISTS (SELECT 1 FROM ops.audit_scrape a
              WHERE a.batch_name = b.batch_name AND a.state = 'pending')
ORDER BY b.finished_at
"""

# 批次落定后仍没拿到快照的组合 → 认账失败(原因由 /failures 给)
_STILL_PENDING_SQL = """
SELECT asin, zip FROM ops.audit_scrape
WHERE state = 'pending' AND batch_name = %(batch)s
"""

_FAIL_PAIR_SQL = """
UPDATE ops.audit_scrape SET state = 'failed', reason = %(reason)s,
       error_type = %(error_type)s, settled_at = now()
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
    -- 好让"重试三天"真的是三天,而不是每推一次就续三天
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


def _scrape_fails(conn, lines: list[dict]) -> dict[tuple, str]:
    """输入:连接 + 待审行 → 输出:{(ASIN, 邮编): error_type}(只含 failed 的)。

    判定链要按 error_type 分流:重采也没用的那类(variant_offset 等)该给
    终局结论,而不是永远挂"待采集"等一个不会来的快照。
    """
    pairs = {((r.get("sku") or "").strip().upper(),
              rules.norm_zip(r.get("postal_code"))) for r in lines}
    pairs = {(a, z) for a, z in pairs if a and z}
    if not pairs:
        return {}
    with conn.cursor() as cur:
        cur.execute(_SCRAPE_FAIL_SQL, {"asins": [a for a, _ in pairs],
                                       "zips": [z for _, z in pairs]})
        return {(a, z): et for a, z, et in cur.fetchall()}


def _judge_all(conn, lines: list[dict], blacklist, suppliers):
    """输入:连接 + 待审行 + 配置 → 输出:([(行, 结论)], 待采 [(asin, 邮编)])。"""
    snaps = _snapshots(conn, lines)
    fails = _scrape_fails(conn, lines)
    results: list = []
    want: list = []
    for line in lines:
        asin = (line.get("sku") or "").strip().upper()
        zip5 = rules.norm_zip(line.get("postal_code"))
        snap = snaps.get((asin, zip5)) if zip5 else None
        res = rules.judge(line, snap, suppliers, blacklist,
                          scrape_fail=fails.get((asin, zip5)))
        # 该不该重采由 judge 显式标记(res.rescrape):没快照、outcome≠ok、
        # 缺配送方式/时长、运费没采到——这四种重采能解决。无匹配采购方、
        # 标题不符那类重采也一样,不进清单(白烧配额)。
        # 钓鱼行是终局,连采都不用采,judge 那边本来就不标 rescrape。
        if res.rescrape and asin and zip5:
            want.append((asin, zip5))
        results.append((line, res))
    return results, want


def _judge_save_tally(conn, lines: list[dict], blacklist, suppliers):
    """输入:连接 + 待审行 + 配置 → 输出:(待采 [(asin, 邮编)], 落库行数, 结论计数)。

    「判定 → 落库 → 计数」这五行在 run() 里出现两次(wait=1 重判那一遍原样
    再走一次),收成一处免得两边飘。
    ⚠ 第二遍**必须判同一批 lines** —— 别在这里顺手改成重新取行:第一遍它们
    多半是"待采集/待人工",重取会换掉待判集合,重判就不是同一批了。
    `results` 只喂 `_save` 与计数,两个调用点都不再用它,故不往外返。
    """
    results, want = _judge_all(conn, lines, blacklist, suppliers)
    saved = _save(conn, results)
    tally: dict[str, int] = {}
    for _, res in results:
        tally[res.status] = tally.get(res.status, 0) + 1
    return want, saved, tally


def _reap_batches(conn) -> tuple[int, int, list[str]]:
    """输入:连接 → 输出:(落定批次数, 认账失败的组合数, 摘要行)。

    **批次生命周期由采集侧说了算**(所有者 2026-08-10 实测确认):

        completed ⇔ tasks.open == 0 AND screenshots.open == 0

    `open` = 既不是 done 也不是 failed 的数量。failed 算终态,所以一张永远
    截不出来的图不会把批次卡死(实测 1 done + 1 failed → completed)。

    在途批次(open > 0)**不判超时、不重推**——它还在干活,盲超时会误判重推、
    白烧一批(所以兜底超时那条 SQL 显式排除了在途批次的组合)。

    两段分开(2026-08-10 实测后改):
    ① 轮询在途批次的状态,落定的记 completed;
    ② **只对「已落定 且 该批自己的事件流已按批拉到底」的批次认账失败**,
       这时才拉 `/api/batches/{batch_id}/failures` 把**真实原因**
       (captcha / variant_offset / zip_switch_failed …)写进台账,而不是一律
       记"超时未见快照"——验证码(换时段可重试)和 404(该去删链接)的处置
       完全不同。

    为什么必须分两段:批次 completed **不等于我们库里有数据**,中间还隔着
    一次导出。合在一起写(落定即认账)会把采成功的整批冤枉成失败——实测
    127 个组合全军覆没,而紧接着一次摄取就把这 127 条原样摄了进来。且
    failed 不挡重推,下一轮会把它们再采一遍,每小时白烧。
    (2026-08-19 起 ② 的凭据从「全局摄取水位线已越过落定时刻」换成
    「pump_batch 把这批拉到底 + 落定后仍 pending」——逐批的确定性,
    不再依赖全局泵跑没跑过。)
    """
    with conn.cursor() as cur:
        cur.execute(_OPEN_BATCHES_SQL, {"prefix": _BATCH_PREFIX + "%"})
        open_batches = cur.fetchall()
    settled_batches, failed_pairs, notes = 0, 0, []

    # ① 在途批次:问状态,落定的记账(**本轮不认账失败**,等摄取追上)
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
            # 可能是首次在途,也可能是**已落定后又被 server 推回重采**——
            # 后者靠 record() 把 finished_at 清空,稳定窗口从零重计。
            stats = st.get("stats") or {}
            shots = st.get("screenshots") or {}
            batches.record(name, batch_id, n, "running",
                           f"open={stats.get('open')} shots_open={shots.get('open')}")
            continue

        stats = st.get("stats") or {}
        batches.finish(name, "completed", stats.get("done"), stats.get("failed"),
                       f"screenshots={(st.get('screenshots') or {}).get('done')}")
        settled_batches += 1

    # ② 已落定的批次:先把**这批自己的事件流**按批拉到底(批次端点,不碰
    #    全局游标、不需要锁),刚到的快照落定之后,仍然 pending 的才有资格
    #    判失败。拉不到底(采集侧故障)的批次这轮不认账,原样挂着等下轮。
    conn.commit()          # 让 ① 写的 finished_at 对下面这条查询可见
    with conn.cursor() as cur:
        cur.execute(_REAPABLE_SQL, {"prefix": _BATCH_PREFIX + "%",
                                    "stable": _AUTO_RETRY_SETTLE_MIN})
        candidates = cur.fetchall()

    waiting = 0
    pumped: list[tuple] = []
    for name, batch_id in candidates:
        res = ingest.pump_batch(scraper, db, name)
        if res["ok"]:
            pumped.append((name, batch_id))
        elif res["gone"]:
            # 批次在采集侧已查不到,事件也拉不回来了:pending 组合交给
            # 兜底超时收尾(_TIMEOUT_SQL 只放过在途批次,completed 不挡)
            notes.append(f"{name}:采集侧已无此批次,交兜底超时")
        else:
            waiting += 1        # 采集侧故障,这轮拉不了 ⇒ 没资格说它失败
            notes.append(f"{name}:批次导出失败,下轮再认账({res['error']})")
    if pumped:
        # 刚拉到的快照先落定台账(本函数之前那遍 _SETTLE_SQL 看不见这批
        # 新行),再问"还有谁 pending"——顺序反了就是把采成功的判失败,
        # 127 条冤案(2026-08-10)的形状
        with conn.cursor() as cur:
            cur.execute(_SETTLE_SQL)
        conn.commit()
    for name, batch_id in pumped:
        with conn.cursor() as cur:
            cur.execute(_STILL_PENDING_SQL, {"batch": name})
            stuck = cur.fetchall()
        if not stuck:
            continue
        # 这批的事件流已拉到底、快照也落定过了,仍无快照 ⇒ 真失败,去问原因
        summary, by_asin = batches.pull_failures(name, batch_id)
        for asin, zip5 in stuck:
            et = by_asin.get(asin)
            reason = (f"采集失败:{et}" if et
                      else "批次已采完且其事件流已拉到底,仍无快照")
            with conn.cursor() as cur:
                cur.execute(_FAIL_PAIR_SQL,
                            {"asin": asin, "zip": zip5, "reason": reason,
                             "error_type": et})
            failed_pairs += 1
        notes.append(f"{name}:{len(stuck)} 个组合无快照({summary})")
    if waiting:
        # 偶发是正常态(采集侧抖一下);一直不降就是采集侧病了,得有人看见
        logger.info("采集台账:%d 个已落定批次本轮拉不到事件流,下轮再认账",
                    waiting)
    conn.commit()
    return settled_batches, failed_pairs, notes


def _settle_ledger(conn) -> tuple[int, int, dict, list[str]]:
    """输入:连接 → 输出:(落定数, 判失败数, {(asin,邮编): 'inflight'|'gaveup'}, 摘要)。

    每轮开工先对账——这就是"重启后先查实际状态再决定是否补交"(CLAUDE.md
    铁律)的落地:进程死了没关系,pending 记录还在,下轮照样能判断该不该重推。

    三层判据各管一段,**谁也替代不了谁**:
      快照真出现   → done(只有它能证明数据到了**我们**库里;批次 completed
                     不等于落库,中间还隔着一次导出)
      批次已落定且事件流拉到底 → 该认账的失败(原因来自 /failures)
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
                                   "fresh": _SNAPSHOT_FRESH_HOURS,
                                   "retryable": sorted(batches.RETRYABLE)})
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
            # 插队(所有者定稿 2026-08-17,旧仓 `V3客户端.提交批次` 同款位置):
            # 本链**阻塞等采集**(默认 wait=1,最长 20 分钟),排在常规批次
            # 后面就是白等到超时,然后拿不到快照的单子全落"待采集"。
            # 只对 pending 生效 ⇒ 必须**紧跟提交**,别挪到轮询那段
            batches.prioritize(name, res.get("batch_id"))
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
            # 顺手把既有 batch_id 记下来,不然这批的失败明细就查不了。
            # **插队照做**:沿用的这批还在 pending,而本链正要阻塞等它
            batches.prioritize(name, e.batch_id)
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
    pending = set(names)        # 数据还没落定的
    gone: set = set()           # 采集侧查不到的:别再问
    shots: dict = {}            # 批次名 → 该批还有几张图没截完(问到就刷新)
    shots_deadline = None
    delay = 3.0
    while time.monotonic() < deadline:
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * 1.5, 30.0)
        # 数据没齐:只问没落定的(省请求)。
        # 数据齐了:**把所有批次再问一遍**——shots 要的是"此刻还剩几张",
        # 而先落定那些批次的计数还停在它落定那一刻,早就过期了。
        # (原先只累加"本轮新落定的",于是 A 先落定带着 2 张图没好、B 后落定
        #  带 0 张,总数就算成 0 直接收工,A 那 2 张白白错过一轮。)
        targets = pending or (set(names) - gone)
        for name in sorted(targets):
            try:
                st = scraper.batch_status(name)
            except LookupError:
                # 采集侧查不到了:再等也没意义,交给对账那层认账
                pending.discard(name)
                gone.add(name)
                shots.pop(name, None)
                continue
            except Exception as e:                  # 查不动:下一轮再问
                logger.warning("等批次 %s:状态查询失败(继续等):%s", name, e)
                continue
            shots[name] = batches.shots_open(st)
            if batches.is_settled(st):
                pending.discard(name)
        if pending:
            logger.info("等采集:%d/%d 批已落定,继续等(已 %.0f 秒)",
                        len(names) - len(pending), len(names),
                        time.monotonic() - started)
            continue
        open_shots = sum(shots.values())
        # 数据都齐了。截图**只给一小段宽限**,绝不当落定条件。
        # 2026-08-10 实测:一个 variant_offset 的**任务**其实是最快到终态的
        # (cap=1,首次上报即 failed,tasks.open 立刻减一,不拖慢批次);
        # 卡住的是**它那个截图槽位**——任务都失败了,那张图不会再有人去截,
        # shots_open 就永久停在 >0。拿它当闸 = 干等满 20 分钟兜底超时。
        if not open_shots:
            break
        if shots_deadline is None:
            shots_deadline = time.monotonic() + _SHOT_GRACE_SEC
            logger.info("数据已采完,再给截图 %d 秒宽限(%d 张未完成)",
                        _SHOT_GRACE_SEC, open_shots)
        if time.monotonic() >= shots_deadline:
            logger.info("截图宽限用尽(%d 张仍未完成),先出结论;"
                        "图后来好了下轮补贴", open_shots)
            break
        # 宽限期内继续问(targets 已经会覆盖所有未 gone 的批次),好了就早收工
    mins = (time.monotonic() - started) / 60
    if pending:
        return (f"等采集:{len(names) - len(pending)}/{len(names)} 批落定,"
                f"**{len(pending)} 批超 {timeout_min} 分钟仍在跑**"
                f"(已采到的照常进增量流,其余下轮处置)"), len(pending)
    return f"等采集:{len(names)} 批全部落定(耗时 {mins:.1f} 分钟)", 0


def _ingest_batches(names: list[str]) -> str:
    """输入:本轮推的批次名 → 输出:按批摄取摘要。

    2026-08-19 起走采集侧批次端点(`export_batch_records`,契约 §4.11):
    只拉**自己这几批**的事件,批内游标每次从 0 拉到底,不碰全局游标、
    **不需要 product_ingest 的锁**——此前是借锁抽全库到当刻头部,整点的
    order_chain 和 13:00 的 product_chain 一撞就是一场 15 分钟等锁。
    幂等靠 snapshots.source_id,与全局泵重复摄取无害。
    没拉到底的批次摘要里点名,那部分下轮再判。
    """
    _, note = ingest.pump_batches(scraper, db, names)
    return note


def _save(conn, results: list) -> int:
    """输入:连接 + [(行, 结论)] → 输出:落库行数(audit_status/detail/audited_at)。"""
    if not results:
        return 0
    payload = [(res.status, json.dumps({**res.detail, "note": res.note},
                                       ensure_ascii=False,
                                       default=_json_default),
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


def _json_default(o):
    """输入:json.dumps 不认识的对象 → 输出:Decimal 转 float,其余转字符串。

    ⚠ **别图省事写成 `default=str`**:PG 的 numeric 列取出来是 Decimal,
    被 str 掉之后 audit_detail 里的「亚马逊单价」就成了 `"25.99"` 这个**字符串**,
    回写飞书数字列时 `NumberFieldConvFail`——而落库、判定、日志全程正常,
    只有推送那一步炸,且飞书的报错不告诉你是哪行哪列。
    (2026-08-10 生产实测:前几轮没行拿到过价格,这个字段一直是空的,
     直到 102 行判通过才第一次撞上。)
    datetime 等仍旧转字符串,那是想要的。
    """
    return float(o) if isinstance(o, Decimal) else str(o)


def _num(v, field: str = "", key: str = ""):
    """输入:detail 里的数值 → 输出:数字或 None(飞书数字列只吃数字)。

    容忍数字字符串:**历史行**的 audit_detail 里价格已经是 `"25.99"` 形态
    (见 _json_default),那些行要等下次重判才会被改写,这轮照样得推得出去。

    非数字一律降成 None 并告警,不让它飞到飞书:一个坏值会让整批
    batch_update 全失败,而 `NumberFieldConvFail` 里既没有行号也没有列名,
    排障只能靠猜。宁可这一格空着,也不要 152 行一起推不上去。
    """
    if v is None or isinstance(v, bool):     # bool 是 int 的子类,别当 0/1 推
        return None
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, int):
        return v
    if not isinstance(v, float):
        try:
            v = float(str(v).strip())
        except (TypeError, ValueError):
            logger.warning("飞书数字列「%s」拿到非数字 %r(行 %s),本次写空",
                           field or "?", v, key or "?")
            return None
    # NaN/Inf:json.dumps 会吐出 JSON 规范里没有的 NaN/Infinity,飞书直接拒
    return v if math.isfinite(v) else None


# ⚠ 别写成 `(asin, zip) = ANY(%(pairs)s)` 传元组列表:PG 认不出匿名 record 的
# 类型,直接 FeatureNotSupported(input of anonymous composite types)。
# 两个平行数组 unnest 是唯一稳的写法。
_BATCH_OF_SQL = """
SELECT asin, zip, batch_name FROM ops.audit_scrape
WHERE batch_name IS NOT NULL
  AND (asin, zip) IN (SELECT * FROM unnest(%(asins)s::text[], %(zips)s::text[]))
"""


def _batch_names(conn, rows: list[dict]) -> dict[tuple, str]:
    """输入:连接 + 已判定行 → 输出:{(asin, 邮编): batch_name}(取图的抓手)。"""
    pairs = [(d.get("asin"), d.get("zip")) for d in map(_detail, rows)
             if d.get("asin") and d.get("zip")]
    if not pairs:
        return {}
    with conn.cursor() as cur:
        cur.execute(_BATCH_OF_SQL, {"asins": [a for a, _ in pairs],
                                    "zips": [z for _, z in pairs]})
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



# 有结论、有 (ASIN,邮编)、但台账里没有对应批次记录的组合 —— 这些行的截图
# **永远取不回来**:增量导出的记录不带批次名(契约字段表没这一项),
# 台账 batch_name 是唯一抓手,行没了就只能重采一次重新生成。
# 只在 -p refill_shots=1 时用:每个组合要烧一次采集配额,不该默认发生。
_ORPHAN_SHOTS_SQL = """
SELECT DISTINCT l.audit_detail ->> 'asin' AS asin,
                l.audit_detail ->> 'zip'  AS zip
FROM orders.order_lines l
WHERE l.order_date >= now() - make_interval(days => %(days)s)
  AND l.audit_status IS NOT NULL
  AND coalesce(l.audit_detail ->> 'asin', '') <> ''
  AND coalesce(l.audit_detail ->> 'zip', '')  <> ''
  {store_filter}
  AND NOT EXISTS (SELECT 1 FROM ops.audit_scrape a
                  WHERE a.asin = l.audit_detail ->> 'asin'
                    AND a.zip  = l.audit_detail ->> 'zip')
"""


def _orphan_pairs(conn, args: dict, store_filter: str) -> list:
    """输入:连接 + 查询参数 → 输出:[(ASIN, 邮编)] 台账里没有批次记录的组合。

    这些行判定是好的(快照在),缺的只是取图的抓手。**不会自愈** ——
    judge 只在缺价格/货期/运费时标 rescrape,缺图不标(重采一次只为一张
    佐证图,不该默认烧配额)。所以要人显式说一声。
    """
    with conn.cursor() as cur:
        cur.execute(_ORPHAN_SHOTS_SQL.format(store_filter=store_filter), args)
        return [(a, z) for a, z in cur.fetchall() if a and z]

def _payload(conn, rows: list[dict]) -> tuple[dict[str, dict], dict]:
    """输入:连接 + 已判定行 → 输出:({order_line_id: 飞书审核列载荷}, 截图分布)。

    **截图分布必须回到摘要里**:取不到图这件事以前是一声不吭的
    (`batch_of.get(...)` 拿到 None 就静默不写这一列),缺一半图只能靠人
    盯着飞书发现。四个桶各是不同的病,处置也不同:

    - `no_batch` —— 台账里没有这个 (ASIN,邮编) 的批次记录 ⇒ **这张图永远
      取不回来**。增量导出的记录里不带批次名(契约字段表没有这一项),
      台账是唯一抓手;台账行被删掉/从未写过,就只能靠重采重新生成。
      **它不会自愈**:judge 只在缺价格/货期/运费时标 rescrape,缺图不标。
    - `not_settled` —— 批次还在跑,或 ops.scrape_batches 里没这批的行。下轮再来。
    - `waiting` —— 图还没截好 / 已记墓碑。下轮再来或就这样了。
    - `ok` —— 拿到 file_token 写进去了。
    """
    f = resources.ORDER_SALES_AUDIT.fields
    batch_of = _batch_names(conn, rows)
    settled = _settled_batches(conn, set(batch_of.values()))
    shots = _shot_index(settled)
    tally = {"ok": 0, "no_batch": 0, "not_settled": 0, "waiting": 0}
    out: dict[str, dict] = {}
    for r in rows:
        d = _detail(r)
        key = r["order_line_id"]
        fields = {
            f.audit_status: r["audit_status"],
            f.script_audit: d.get("note"),
            f.amz_price: _num(d.get("amz_price"), f.amz_price, key),
            f.stock_qty: _num(d.get("stock_qty"), f.stock_qty, key),
            f.ship_method: d.get("ship_method"),
            f.ship_days: _num(d.get("ship_days"), f.ship_days, key),
            f.seller: d.get("seller"),
            f.supplier: d.get("supplier"),
            f.price_cap: _num(d.get("price_cap"), f.price_cap, key),
            f.title_similarity: _num(d.get("title_similarity"),
                                     f.title_similarity, key),
        }
        asin = (d.get("asin") or "").upper()
        token = None
        if asin and d.get("zip"):
            batch = batch_of.get((d.get("asin"), d.get("zip")))
            if not batch:
                tally["no_batch"] += 1
            elif batch not in settled:
                tally["not_settled"] += 1
            else:
                token = _screenshot_token(conn, batch, asin,
                                          shots.get((batch, asin), ""))
                tally["ok" if token else "waiting"] += 1
        if token:
            fields[f.screenshot] = [{"file_token": token}]
        out[key] = fields
    return out, tally


def _yes(v) -> bool:
    return str(v).lower() in {"1", "true", "yes"}


def run(params: dict) -> str:
    """输入:params(days/stores/line/recheck/scrape/wait/push)→ 输出:审核摘要。"""
    days = int(params.get("days", _DEFAULT_DAYS))
    stores = [s.strip() for s in str(params.get("stores", "")).split(",") if s.strip()]
    line_id = str(params.get("line", "")).strip()
    recheck = _yes(params.get("recheck", ""))
    do_push = flag(params, "push", default=True)
    do_scrape = flag(params, "scrape", default=True)
    # **默认开**(所有者定稿 2026-08-10):手工跑时忘了加 -p wait=1 就只能看到
    # 上一轮的结论,而且不报错——"忘了就静默降级"的默认值不该留着。
    # 挂调度那天在 plist 里显式写 -p wait=0 是很自然的一步(参数本来就要逐条写)。
    do_wait = flag(params, "wait", default=True)
    refill_shots = _yes(params.get("refill_shots", ""))

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

        want, saved, tally = _judge_save_tally(conn, lines, blacklist, suppliers)

        # ③ 推采集:一批混邮编,同一 ASIN 的多邮编拆波次
        scrape_note, sent = "", []
        if do_scrape and refill_shots:
            # 只为补图而重采:判定本身不需要它们,所以默认关
            orphans = [p for p in _orphan_pairs(conn, args, store_filter)
                       if p not in set(want)]
            if orphans:
                logger.info("补图重采:%d 个组合台账无批次记录", len(orphans))
                want = list(want) + orphans
        if do_scrape:
            scrape_note, sent = _push_scrape(conn, want, blocked)

        # ③′ wait=1:等这批采完 → 就地按批摄取 → 重新对账重判(一条命令出结论)
        #
        # 顺序不能换。批次 completed 只说明**采集侧**干完了,数据还在导出流里;
        # 不先摄取就去对账,每一条都会被判成"批次已采完但无快照",一轮全军覆没。
        #     等批次落定 → 按批摄取(批次端点,无锁)→ 对账 → 重判
        wait_notes: list[str] = []
        if do_wait and sent:
            note, _stuck = _wait_for_batches(sent, _SCRAPE_TIMEOUT_MIN)
            wait_notes.append(note)
            # 先记落定(写 finished_at,认账的稳定窗口从这里起算),再摄取
            _settle_ledger(conn)
            wait_notes.append(_ingest_batches(sent))
            settled2, gone2, _blocked2, notes2 = _settle_ledger(conn)
            settled += settled2
            gone += gone2
            batch_notes.extend(notes2)
            # 重判的是**同一批行**:第一遍它们多半是"待采集/待人工",这遍才
            # 拿到真数据。已出终局结论的行 judge 会照常重算,不受影响。
            _want2, saved, tally = _judge_save_tally(conn, lines, blacklist,
                                                     suppliers)

        # ④ 推送:窗口内所有已判定行(不止本轮新判的),漏推的行下轮自愈
        pushed = missing = 0
        shot_tally: dict[str, int] = {}
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
                fields_by_key, shot_tally = _payload(conn, done)
                pushed, miss_keys = feishu.update_by_key(
                    resources.ORDER_SALES_AUDIT,
                    resources.ORDER_SALES_AUDIT.fields.key, fields_by_key)
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
        if shot_tally:
            bits = [f"已贴 {shot_tally['ok']}"]
            if shot_tally["not_settled"]:
                bits.append(f"批次未落定 {shot_tally['not_settled']}")
            if shot_tally["waiting"]:
                bits.append(f"等图/已放弃 {shot_tally['waiting']}")
            if shot_tally["no_batch"]:
                # 唯一一个不会自愈的桶:台账没批次记录 = 抓手没了,
                # 只能 -p refill_shots=1 重采一次重新生成
                bits.append(f"**无批次记录 {shot_tally['no_batch']}**"
                            f"(取不回来,需 -p refill_shots=1 重采)")
            parts.append("截图:" + ",".join(bits))
    return ";".join(parts)
