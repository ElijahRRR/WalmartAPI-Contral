"""variant_offset_cleanup — 采集永久偏移的 SKU 删除(危险,默认 dry-run)。

用法:
  python cli.py variant_offset_cleanup                    # dry-run:列出将删什么
  python cli.py variant_offset_cleanup --execute          # 真删(DELETE_ITEM,永久)
  python cli.py variant_offset_cleanup -p min_batches=2   # 收紧到 2 个批次才删
  python cli.py variant_offset_cleanup -p limit=100       # 单店单轮上限(默认 300)
  python cli.py variant_offset_cleanup -p store=A085朱丽霖

**为什么这些 SKU 必须走掉**(所有者定稿 2026-08-09):
`variant_offset` = worker 请求 /dp/<ASIN>,亚马逊返回的是**兄弟变体的页面**,
parser 比对页面 ASIN 与任务 ASIN 不一致 → 拒绝写入(宁可判失败,也不把隔壁
变体的标题/价格当成这个 ASIN 的数据)。采集侧把它列入**不自动重试**类型。
后果:这些 SKU 的价格/库存**永远拿不到新数据**,维护链会一直拿陈旧快照跟价
跟库存——比缺数据更危险。既然采不到就没法维护,直接删。

门槛(所有者定稿 2026-08-09:**偏移了就不会恢复,不需要观察期**):
  1. `min_batches` 默认 **1** —— 出现一次就够。想收紧用 -p min_batches=N。
  2. **后来采到了就不删**:最后一次偏移之后若有 outcome=ok 的快照,自动
     移出名单。这条不是观察期,是防呆——真出现就说明它还能采,删了会误杀。

只作用于在线(PUBLISHED + 未缺席)且店铺 ACTIVE 的行;一个 ASIN 在多店在线
就每店各删一次(占用是店铺维度的)。

飞书投影:提交时往「维护记录」追加行(C=删除(variant_offset),F=真 feedid,
H=处理中),结果由 feed_poll 的 maint_sheet 反哺器按 ops.feed_items 回填
H/I——删除不需要自己的反哺器。状态权威始终在 PG,飞书只是展示面板。
"""

import logging
from datetime import datetime

from api import feeds
from registry import db
from services import kpi, maint_sheet, product_events, stores as stores_svc

DANGEROUS = True

logger = logging.getLogger("workflows.variant_offset_cleanup")

MIN_BATCHES = 1         # 出现一次即删(所有者定稿:偏移了就不会恢复)
STORE_LIMIT = 300       # 单店单轮上限(与 product_clear 同款防呆)
# 维护记录 C 列:动作里带上原因,一眼能和改价/改库存/改标题区分开
MAINT_ACTION = "删除(variant_offset)"

# 候选:偏移过的 ASIN × 在线行。
# snapshots.outcome 在补列之前的历史行是 NULL,那些行都是成功采集,
# 所以按 ok 处理(COALESCE)——否则老 SKU 会因为"查不到 ok 快照"被误判该删。
_SQL_CANDIDATES = """
WITH vo AS (
    SELECT asin,
           count(DISTINCT batch_name) AS batches,
           min(recorded_at) AS first_seen,
           max(recorded_at) AS last_seen
    FROM ops.scrape_failures
    WHERE error_type = 'variant_offset'
    GROUP BY asin
), latest_status AS (
    SELECT DISTINCT ON (store) store, store_status
    FROM ops.store_kpi_daily ORDER BY store, data_date DESC
)
SELECT w.store, w.sku, vo.batches, vo.first_seen, vo.last_seen
FROM vo
JOIN catalog.walmart_items w ON w.sku = vo.asin
LEFT JOIN latest_status s ON s.store = w.store
WHERE w.missing_since IS NULL
  AND w.published_status = 'PUBLISHED'
  AND (s.store_status IS NULL OR upper(s.store_status) = 'ACTIVE')
  AND vo.batches >= %(min_batches)s
  AND NOT EXISTS (
        SELECT 1 FROM catalog.snapshots sn
        WHERE sn.asin = vo.asin
          AND COALESCE(sn.outcome, 'ok') = 'ok'
          AND sn.scraped_at > vo.last_seen)
ORDER BY w.store, w.sku
"""


def _candidates(min_batches: int) -> list[dict]:
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_CANDIDATES, {"min_batches": min_batches})
        return [{"store": r[0], "sku": r[1], "batches": r[2],
                 "first_seen": r[3], "last_seen": r[4]}
                for r in cur.fetchall()]


def _record(store: str, rows: list[dict], feed_id) -> None:
    """删除提交入病历(生死类事件恒记;回执由 feed_track 另记)。"""
    with db.pg_conn() as conn:
        product_events.record_many(conn, [
            {"sku": r["sku"], "store": store, "event": "delete_submitted",
             "source": "variant_offset_cleanup",
             "detail": {"feed_id": feed_id, "reason": "variant_offset",
                        "batches": r["batches"],
                        "first_seen": r["first_seen"],
                        "last_seen": r["last_seen"]}}
            for r in rows])


def _submit_store(store: dict, rows: list[dict], today: str,
                  lines: list[str]) -> list[tuple]:
    """单店提交 DELETE_ITEM;多切片滑窗对位记账(与 cleanup 同款)。

    返回维护记录表行:F=真 feedid + H=处理中,交给 feed_poll 的 maint_sheet
    反哺器按台账回填 H/I——不需要为删除单独写反哺器。
    """
    name = store["name"]
    i = 0
    n = {"submitted": 0, "dedup": 0, "failed": 0, "unknown": 0}
    records: list[tuple] = []
    for res in feeds.submit_feed(store, "DELETE_ITEM",
                                 [r["sku"] for r in rows],
                                 workflow="variant_offset_cleanup"):
        batch = rows[i:i + res["count"]]
        i += res["count"]
        n[res["outcome"]] = n.get(res["outcome"], 0) + len(batch)
        # 只有 submitted 才落事件:dedup 带着旧 feed_id 但什么都没提交,
        # 记了就是幽灵事件(病历不灌水)
        if res["outcome"] == "submitted" and res["feed_id"]:
            _record(name, batch, res["feed_id"])
        # 表格是展示面板,口径与 maintenance 一致:submitted/dedup 都出行
        # (dedup 挂的是旧 feedid,反哺器照样能把它落定)
        if res["outcome"] in ("submitted", "dedup") and res["feed_id"]:
            records += [(name, r["sku"], MAINT_ACTION, "在线", "删除",
                         res["feed_id"], today, "处理中", "") for r in batch]
        elif res["outcome"] == "failed":
            records += [(name, r["sku"], MAINT_ACTION, "在线", "删除",
                         "", today, "提交被拒", "") for r in batch]
    line = f"  {name}:删除提交 {n['submitted']}"
    if n["dedup"]:
        line += f",在途防重跳过 {n['dedup']}"
    if n["failed"]:
        line += f",⚠ 提交失败 {n['failed']}(查日志)"
    if n["unknown"]:
        line += f",⚠ 结局不确定留 pending {n['unknown']}(待对账)"
    lines.append(line)
    return records


def run(params: dict) -> str:
    """输入:params(execute/min_batches/limit/store)→ 输出:删除摘要。"""
    execute = bool(params.get("execute"))
    min_batches = int(params.get("min_batches", MIN_BATCHES))
    limit = int(params.get("limit", STORE_LIMIT))

    rows = _candidates(min_batches)
    if params.get("store"):
        rows = [r for r in rows if r["store"] == params["store"]]
    if not rows:
        loose = len(_candidates(1)) if min_batches > 1 else 0
        tail = (f";门槛放到 1 个批次则有 {loose} 行"
                f"(-p min_batches=1 可删)" if loose else "")
        return f"无 variant_offset 待删行(门槛 ≥{min_batches} 个批次){tail}"

    by_store: dict[str, list[dict]] = {}
    for r in rows:
        by_store.setdefault(r["store"], []).append(r)
    capped = {s: v[:limit] for s, v in by_store.items()}
    over = sum(len(v) - len(capped[s]) for s, v in by_store.items())

    mode = "" if execute else "🧪 [DRY-RUN] "
    n_sku = len({r["sku"] for r in rows})
    lines = [f"{mode}variant_offset 永久删除:{sum(len(v) for v in capped.values())} 行"
             f"({n_sku} 个 ASIN × {len(by_store)} 店,门槛 ≥{min_batches} 个批次)"
             + (f",超单店上限 {limit} 留到下轮 {over} 行" if over else "")]

    if not execute:
        # 人眼闸门:DELETE_ITEM 不可逆,名单必须看得见(不能只给个数)
        if min_batches > 1:
            loose = len(_candidates(1))
            if loose != len(rows):
                lines.append(f"  门槛对比:≥1 个批次 {loose} 行,"
                             f"≥{min_batches} 个批次 {len(rows)} 行"
                             f"(差额是只偏移过一次的,本轮不动)")
        for store_name, items in sorted(capped.items()):
            sample = [(r["sku"], f"{r['batches']}批") for r in items[:5]]
            lines.append(f"  {store_name}:删除 {len(items)} 行,样本={sample}"
                         + (" …" if len(items) > 5 else ""))
        lines.append("⚠ DELETE_ITEM 永久且不可逆;确认名单后加 --execute")
        return "\n".join(lines)

    stores_by_name = {s["name"]: s for s in stores_svc.load_stores()}
    today = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d")
    all_records: list[tuple] = []
    for store_name, items in sorted(capped.items()):
        store = stores_by_name.get(store_name)
        if store is None:
            lines.append(f"  {store_name}:凭证缺失,跳过")
            continue
        try:    # 单店隔离:单店代理/网络异常不炸整轮
            all_records += _submit_store(store, items, today, lines)
        except Exception as e:
            logger.exception("店铺 %s 删除提交异常,跳过继续其它店: %s",
                             store_name, e)
            lines.append(f"  ⚠ {store_name}:提交异常已跳过({e}),下轮重试")

    try:
        written = maint_sheet.append_records(all_records)
        lines.append(f"维护记录追加 {written} 行(H/I 由 feed_poll 反哺器回填)")
    except LookupError as e:
        lines.append(f"⚠ 维护记录表未登记,流水未写表(台账已在 PG):{e}")
    lines.append("结果轮询走 feed_poll;删除是否真生效以 catalog_sync 观测为准")
    return "\n".join(lines)
