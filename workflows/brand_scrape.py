"""brand_scrape — 采集库缺品牌的 C/E 类 ASIN:推采集补货 → 摄取 → 品牌入账。

用法:
  python cli.py brand_scrape               # 预览:缺口清单体检,零推送
  python cli.py brand_scrape -p apply=1    # 推采集 + 等落定 + 摄取 + 入账
  python cli.py brand_scrape -p apply=1 -p wait=0   # 只推不等(下次重跑收账)

所有者定稿 2026-08-11:asin 从亚马逊产品库(catalog.products)查,库里
没有的走**推送采集**补货——不设邮编、不截图(最快形态);采完借
product_ingest 的公共泵摄进中心库,再走 collect_brands 入账(渠道表 +
否决闸 + beyKyi 投影三处随日常链路自然到位)。

防无限循环的三道闸(缺一条就会永远采不到 → 永远缺品牌 → 永远再推):
  ① **非标准 asin 过滤**(sku_asin.is_standard_asin):纯数字 item id、
    11 位源头码、原文兜底的键推去采集必然采空,直接排除并报数;
  ② **尝试台账** ops.dedupe('cleanup:brand_scrape'):推过一次的 ASIN 不再推
    (页面已下架/被封的产品永远采不到,重推只是烧采集配额);
  ③ 收集本身按 ops.dedupe('cleanup:brand_asin') 防重,入账过的不再进清单。

顺序纪律(order_audit 踩过的同一坑):批次 completed 只说明**采集侧**干完,
数据还在增量导出流里——必须先摄取(借 product_ingest 的 flock,游标独占)
再入账,否则整批被冤枉成"还是没品牌"。
"""

import logging
import time

from api import scraper
from registry import db
from services import blacklist, product_ingest as ingest, runlock, sku_asin

DANGEROUS = False

logger = logging.getLogger("workflows.brand_scrape")

_POLL_SECS = 15
_DEFAULT_TIMEOUT_MIN = 30
_DEFAULT_LIMIT = 1000       # 单批上限(2000~3000/分钟,1000 个几分钟内采完)


def _pump() -> str:
    """输入:无 → 输出:就地摄取摘要。借 product_ingest 的锁(游标独占,
    两个进程同推会静默丢一段);拿不到锁不是失败,数据会由它摄入。"""
    with runlock.hold(ingest.CURSOR_NAME) as got:
        if not got:
            return "摄取:跳过(product_ingest 正在跑,别和它抢游标)"
        return ingest.pump_summary(ingest.pump(scraper, db))


def _wait_settled(batch_name: str, timeout_min: int) -> str:
    """输入:批次名 + 超时分钟 → 输出:等待结果一句话。
    只看 stats(done+failed 达 total)或 status 终态;超时不算失败——
    台账已记,下次重跑 brand_scrape 会先摄取再入账,自然收尾。"""
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        st = scraper.batch_status(batch_name)
        stats = st.get("stats") or {}
        total = int(stats.get("total") or 0)
        settled = int(stats.get("done") or 0) + int(stats.get("failed") or 0)
        if st.get("status") in ("completed", "failed") or \
                (total and settled >= total):
            return (f"批次 {batch_name} 落定:done {stats.get('done')} / "
                    f"failed {stats.get('failed')} / total {total}")
        time.sleep(_POLL_SECS)
    return (f"⚠ 批次 {batch_name} 等待超时({timeout_min} 分钟)——"
            f"不算失败,稍后重跑本工作流收账即可")


def _collect(items: list[dict]) -> dict:
    with db.pg_conn() as conn:
        return blacklist.collect_brands(conn, items)


def run(params: dict) -> str:
    """输入:params(apply/wait/limit/timeout_min)→ 输出:补货摘要。"""
    apply = str(params.get("apply", "")).lower() in {"1", "true", "yes"}
    do_wait = str(params.get("wait", "1")).lower() not in {"0", "false", "no"}
    limit = int(params.get("limit", _DEFAULT_LIMIT))
    timeout_min = int(params.get("timeout_min", _DEFAULT_TIMEOUT_MIN))
    lines = []

    # 先摄取再看缺口:上一轮推的采集也许已经出数了,先收账少推一批
    if apply:
        lines.append(_pump())

    with db.pg_conn() as conn:
        items = blacklist.missing_brand_items(conn)
        standard = [it for it in items if sku_asin.is_standard_asin(it["asin"])]
        nonstd = len(items) - len(standard)
        attempted = blacklist.scrape_attempted(
            conn, [it["asin"] for it in standard])
    todo = [it for it in standard if it["asin"] not in attempted]

    if apply and standard:
        # 上一轮推过的先入账(摄取刚追平,新到货的品牌在这里落袋)
        st = _collect([it for it in standard if it["asin"] in attempted])
        if st["brand_new"] or st["brand_known"]:
            lines.append(f"上轮已采的入账:新品牌 {st['brand_new']},"
                         f"已知 {st['brand_known']}")

    if not apply:
        return (f"品牌补采预览:缺品牌候选 {len(items)} 个,其中标准 ASIN "
                f"{len(standard)} 个(非标准过滤 {nonstd} 个,防循环闸①),"
                f"已推过采集 {len(attempted)} 个(闸②,不再推),"
                f"本轮将推 {min(len(todo), limit)} 个(单批上限 {limit});"
                f"加 -p apply=1 执行(不设邮编不截图,最快形态)")

    if not todo:
        return ";".join(lines + [
            f"品牌补采:无可推候选(非标准 {nonstd} 个已过滤,"
            f"已尝试 {len(attempted)} 个不再推——页面已下架的产品采不回来,"
            f"属正常损耗)"])

    batch = todo[:limit]
    name = f"brandfix-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        res = scraper.submit_batch(name, [it["asin"] for it in batch])
        lines.append(f"推采集批次 {name}:{res.get('inserted')} 个 ASIN"
                     f"(不设邮编不截图)")
    except scraper.BatchExistsError as e:
        # 撞名 = 上次其实推成功了,接着往下走等它落定即可
        name = e.batch_name
        lines.append(f"批次已存在(上次已推成功):{name}")
    with db.pg_conn() as conn:
        blacklist.mark_scrape_attempted(conn, [it["asin"] for it in batch], name)

    if do_wait:
        lines.append(_wait_settled(name, timeout_min))
        lines.append(_pump())
        st = _collect(batch)
        lines.append(f"入账:新品牌 {st['brand_new']},已知 {st['brand_known']},"
                     f"仍缺品牌 {st['no_brand']}(采集失败/页面已下架);"
                     f"跑 blacklist_push 可把新品牌推上 beyKyi")
    else:
        lines.append("未等待(wait=0):稍后重跑本工作流即自动摄取并入账")
    if len(todo) > limit:
        lines.append(f"⚠ 还有 {len(todo) - limit} 个候选超出单批上限,再跑一轮即可")
    return ";".join(lines)
