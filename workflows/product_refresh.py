"""product_refresh — 在线产品全量重推采集(维护链的数据新鲜度源头)。

用法:
  python cli.py product_refresh --dry-run       # 空跑:列出将推多少个 ASIN
  python cli.py product_refresh                 # 真推(建批次 + 落台账)
  python cli.py product_refresh -p wait=1       # 真推后阻塞等采完(默认不等;
                                                #   产品线一条链跑完就靠它)
  python cli.py product_refresh -p check=1      # 只查在途批次状态(不推新的)

批次落定(采完/超时)时**顺手拉失败明细落 ops.scrape_failures**:验证码/超时/
封禁这类"根本没采到"的 ASIN 在增量流里压根不出现(没产出记录就没有可导出
的行),不主动拉就永远答不上"这个 ASIN 为什么没有新数据"。
降级采到的(outcome≠ok)则在 catalog.snapshots,两者互补,见 docs/db_schema.md。

**这是旧工作流第 2 步的等价物**(所有者澄清 2026-08-09:旧维护三步 =
获取在线产品 → 推送采集拿最新 amz 数据并自动计算 → 读决策并提交)。
没有这一步,latest_snapshot 会越来越陈旧,而 maintenance 的三个 provider
照样会拿着陈旧数据算出价格和库存去提交——**比任何字段问题都危险**。

口径(所有者定稿 2026-08-09):
  · 每次改价前**全量重推**在线产品(PUBLISHED + 店铺 ACTIVE),不做优先级
  · 采集器吞吐 2000~3000/分钟(不切邮编、不截图)
  · **一次性提交一个批次**(上传上限 50MB,几十万 ASIN 也只有几 MB)
  · **必须确认推上去了才开始计时**;超时 1 小时
  · 批次命名:取回按批次比整库查快得多

批次语义(v4 实证,与 v3 不同):批次名撞名返 **409 且绝不静默合并**,
响应体带既有 batch_id ⇒ 本端点**可以安全重试**,200 恒等于新建批次,
inserted 无歧义。所以不需要 v3 那套毫秒精度躲合并的把戏。
"""

import logging
import re
from datetime import datetime

from api import scraper
from registry import db
from services import kpi, scrape_batches as batches

DANGEROUS = True        # 会给采集器压十几万个任务,空跑用 --dry-run

logger = logging.getLogger("workflows.product_refresh")

# **默认一次性全量提交一个批次**(所有者质疑 2026-08-09,复核后改):
# 采集侧上传上限 50MB,txt 每个 ASIN 约 11 字节 —— 27722 个才 ~300KB(0.6%),
# 百万级也只有 ~11MB。分批不省任何资源,反而把"按批次取回"拆成多次聚合、
# 台账多行、查进度要合并。BATCH_SIZE 因此只是**安全阀**(远超当前规模),
# 不是常规切分:真到那个量级再考虑分批的可观测性。
BATCH_SIZE = 200000
TIMEOUT_HOURS = 1           # 推上去后多久没采完算超时(所有者定稿)

# 本工作流的批次名前缀。ops.scrape_batches 是全项目共用台账(order_audit 的
# 按邮编批次也在里面),**查在途必须按前缀圈自己的**——否则 check 会拿
# 维护链的 1 小时超时口径去把订单审核的批次标成 timeout,而那边正等着它。
BATCH_PREFIX = "wm-refresh-"

# 合法 ASIN 形态(与采集侧 common/core/idents.ASIN_RE 同口径):B + 9 位大写字母数字
_ASIN_RE = re.compile(r"^B[0-9A-Z]{9}$")

# 在线且店铺 ACTIVE 的 ASIN。同一 ASIN 多店只推一次(采集结果按 ASIN 共享)。
# 店铺状态取 ops.store_kpi_daily 每店最新一行;无 KPI 记录的店视为 ACTIVE
# (与 list_new 闸门同口径,fail-open)。
_SQL_TARGETS = """
WITH latest_status AS (
    SELECT DISTINCT ON (store) store, store_status
    FROM ops.store_kpi_daily ORDER BY store, data_date DESC
)
SELECT DISTINCT w.sku
FROM catalog.walmart_items w
LEFT JOIN latest_status s ON s.store = w.store
WHERE w.missing_since IS NULL
  AND w.published_status = 'PUBLISHED'
  AND (s.store_status IS NULL OR upper(s.store_status) = 'ACTIVE')
ORDER BY w.sku
"""


def _targets() -> tuple[list[str], int]:
    """输入:无 → 输出:(合法 ASIN 形态的在线 SKU, 被过滤掉的行数)。

    历史遗留:一部分在线 SKU 根本不是 ASIN 形态(旧系统留下的自定义编码)。
    采集侧建任务时就把它们丢掉(2026-08-09 实证:推 27722,采集侧只建了
    27170 个任务),所以推了也是白推。所有者定稿:**不在维护范围内,直接
    过滤**——它们也永远不会有 amz 数据,维护链本来就碰不到。
    """
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_TARGETS)
        skus = [r[0] for r in cur.fetchall()]
    ok = [s for s in skus if _ASIN_RE.fullmatch(s or "")]
    return ok, len(skus) - len(ok)


# 批次台账三件套(record / finish / pull_failures)住在 services/scrape_batches:
# order_audit 的按邮编批次要用同一套语义,工作流之间不准互相 import(铁律 1),
# 抄第二份则两边迟早漂。


def _check_open() -> list[str]:
    """输入:无 → 输出:本链在途批次的状态行(落定语义在 services 共用一份)。"""
    return batches.check_open(BATCH_PREFIX, TIMEOUT_HOURS)



def run(params: dict) -> str:
    """输入:params(execute/check/wait)→ 输出:推送与批次状态摘要。"""
    if params.get("check"):
        # check 不受 dry-run 约束,且**确实写库**(批次落定 + 失败明细落台账)。
        # 这不是破坏性动作(不推任务、不碰沃尔玛,只是把采集侧的既成事实抄回
        # 本地台账),但 cli 的 DRY-RUN 横幅会说"只打印将做什么"——这里明说,
        # 免得看输出的人以为台账没动。
        return "\n".join(["在途采集批次(check:只读采集侧,结果同步进台账):"]
                         + _check_open())

    asins, dropped = _targets()
    if not asins:
        return "无在线产品可推(catalog_sync 是否跑过?)"

    chunks = [asins[i:i + BATCH_SIZE]
              for i in range(0, len(asins), BATCH_SIZE)]
    est_min = len(asins) / 2500        # 所有者口径:2000~3000/分钟
    skip = f",非 ASIN 形态 SKU 已过滤 {dropped} 个" if dropped else ""
    if not params.get("execute"):
        split = "一个批次" if len(chunks) == 1 else f"{len(chunks)} 个批次"
        return (f"🧪 [DRY-RUN] 将全量重推 {len(asins)} 个在线 ASIN"
                f"(PUBLISHED + 店铺 ACTIVE,跨店去重){skip},{split};"
                f"按 2500/分钟估算约 {est_min:.0f} 分钟采完\n"
                + "\n".join(["在途批次:"] + _check_open()))

    stamp = datetime.now(kpi.CN_TZ).strftime("%Y%m%d-%H%M%S")
    pushed, lines, pushed_names = 0, [], []
    for i, chunk in enumerate(chunks, 1):
        # 单批(常态)不带序号:批次名就是取回的抓手,越简单越好
        name = (f"{BATCH_PREFIX}{stamp}" if len(chunks) == 1
                else f"{BATCH_PREFIX}{stamp}-{i:02d}")
        try:
            res = scraper.submit_batch(name, chunk)
            # 200 恒等于新建批次:拿到 batch_id 才算"确认推上去了",此刻起计时
            batches.record(name, res.get("batch_id"), len(chunk), "pushed",
                           f"inserted={res.get('inserted')}")
            pushed += len(chunk)
            pushed_names.append(name)
            lines.append(f"  {name}:推送 {len(chunk)} 个"
                         f"(inserted={res.get('inserted')})")
        except scraper.BatchExistsError as e:
            # 撞名 = 上一次其实推成功了(v4 绝不静默合并):接着用既有批次
            batches.record(name, e.batch_id, len(chunk), "pushed",
                           "撞名沿用既有批次")
            pushed += len(chunk)
            pushed_names.append(name)
            lines.append(f"  {name}:已存在,沿用既有批次 {e.batch_id}")
        except Exception as e:
            batches.record(name, None, len(chunk), "failed", str(e)[:200])
            lines.append(f"  {name}:❌ 推送失败 {e}")
            logger.exception("批次 %s 推送失败", name)

    head = (f"全量重推:{pushed}/{len(asins)} 个 ASIN 已确认推上"
            f"({len(chunks)} 个批次){skip},预计约 {est_min:.0f} 分钟采完;"
            f"超时阈值 {TIMEOUT_HOURS} 小时")

    if not params.get("wait"):
        tail = ["", "查进度:python cli.py product_refresh -p check=1",
                "采完后拉数据:python cli.py product_ingest"]
        return "\n".join([head] + lines + tail)

    # ⚠ wait 是**产品线一条链跑完**的前提(所有者定稿 2026-08-16)。
    # 2026-08-16 之前:用法行写着 `-p wait=1`,run() 里从头到尾没读过它 ——
    # 传了等于没传。后果不是报错,是**静默降级**:推完立刻返回,链里下一步
    # product_ingest 摄回来的还是上一轮的数据,而摘要看起来一切正常。
    line, unsettled = batches.wait_settled(pushed_names, TIMEOUT_HOURS * 60)
    lines.append(line)
    # 等完再落一次台账:批次状态、失败明细都归 check_open 那一份实现
    lines += ["批次落定:"] + _check_open()
    if unsettled:
        # 不抛错、不停链:已采到的部分照常能用,硬停反而让整条产品链今天全废。
        # 但必须说出来 —— 这一轮的维护判据会比平时旧一些
        lines.append(f"⚠ 仍有 {unsettled} 个批次未采完,本轮 product_ingest 只会"
                     f"摄到已完成的部分;下一轮 catalog_sync 之后自然补上")
    return "\n".join([head] + lines)
