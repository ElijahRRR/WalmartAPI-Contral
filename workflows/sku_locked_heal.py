"""sku_locked_heal — SKU_LOCKED 自愈链(listing L3 首条,旧 retire_and_relist 等价物;危险:缺省即真跑,空跑用 --dry-run)。

用法:
  python cli.py sku_locked_heal --dry-run       # 空跑:打印将退役/将清列哪些行
  python cli.py sku_locked_heal                 # 真跑(提交 RETIRE + 清列)
  python cli.py sku_locked_heal -p store=A085朱丽霖
  python cli.py sku_locked_heal -p cooldown_hours=24

背景(所有者纠正 2026-08-12:SKU_LOCKED 不是永久跳过):
ERR_EXT_DATA_0101211 = 该 SKU 已绑死旧 UPC。旧实证(legacy_survey.md:1667):
**不先退役直接换新 UPC 重发同一 SKU 也会失败**,唯一解是三步链:
  ① 对上架表 O=SKU_LOCKED 的行提交 RETIRE_ITEM(退役旧 offer)
  ② 24h 冷却(listing.retire_cooldown 表,状态权威在库)
  ③ 回执成功 + 冷却期满 → 清列(K~M/O~Q 清空,N 写自愈标记)——
     行变"新行",下一轮 list_new 按正常闸门链领**新 UPC** 重上
旧系统由 launchd retire_daily 23:30 干 ①,清列重上在 retire_and_relist;
新系统合成一个 workflow,每天跑一次即可(① 与 ③ 同轮各自推进)。

防重与安全:
  - 同 (店铺,SKU) 只允许一条在途冷却(retire_cooldown 部分唯一索引);
    crash 在提交后/落库前 → 下轮 feeds 层同载荷 dedup 返回同 feed_id 补落库
  - RETIRE 回执**失败**的冷却记录标 failed,**不自动重试**(写操作永不
    自动兜底),摘要点名人工处置
  - 清列只动**当前仍 O=SKU_LOCKED** 的行;行已被人工改过就不碰,
    冷却记录直接关闭(避免覆盖运营手工处理的行)
  - retire_submitted 入病历(source=sku_locked_heal);回执 retire_feed_*
    由 feed_track 白名单自动入账
"""

import logging

from api import _client, feeds
from registry import db
from services import feed_track, listing_sheet, product_events, \
    store_retry, stores as stores_svc, upc_pool

DANGEROUS = True

logger = logging.getLogger("workflows.sku_locked_heal")

_SQL_OPEN = """
SELECT store, sku, feed_id, retired_at, status,
       (status = 'pending' AND
        retired_at <= now() - make_interval(hours => %s)) AS ripe
FROM listing.retire_cooldown WHERE status IN ('pending', 'failed')
"""
_SQL_INSERT = """
INSERT INTO listing.retire_cooldown (store, sku, feed_id)
VALUES (%s, %s, %s)
ON CONFLICT (store, sku) WHERE status = 'pending' DO NOTHING
"""
_SQL_CLOSE = """
UPDATE listing.retire_cooldown SET status = %s, cleared_at = now()
WHERE store = %s AND sku = %s AND status = 'pending'
"""


def _retire(locked: list[dict], open_pairs: set, failed_pairs: set,
            stores_by_name: dict, execute: bool) -> list[str]:
    """① 对未在冷却/未标失败的 SKU_LOCKED 行提交 RETIRE_ITEM。"""
    lines = []
    todo = [r for r in locked
            if (r["store"], r["asin"]) not in open_pairs
            and (r["store"], r["asin"]) not in failed_pairs]
    if failed_pairs:
        lines.append(f"  ⚠ {len(failed_pairs)} 个 (店铺,SKU) 此前 RETIRE 回执失败,"
                     f"不自动重试,请人工处置:"
                     + ",".join(f"{s}/{k}" for s, k in sorted(failed_pairs)[:5]))
    if not todo:
        return lines

    by_store: dict[str, list[dict]] = {}
    for r in todo:
        by_store.setdefault(r["store"], []).append(r)
    def _retire_store(store: dict, store_name: str,
                      srows: list[dict]) -> list[str]:
        """单店提交(第一轮与串行补试共用**同一条**路径,单一落地纪律)。"""
        out: list[str] = []
        skus = [r["asin"] for r in srows]        # 上架 sku=asin 约定
        n_ok = 0
        i = 0
        for res in feeds.submit_feed(store, "RETIRE_ITEM", skus,
                                     workflow="sku_locked_heal"):
            batch = srows[i:i + res["count"]]
            i += res["count"]
            if res["outcome"] in ("submitted", "dedup") and res["feed_id"]:
                n_ok += len(batch)
                with db.pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.executemany(_SQL_INSERT, [
                            (store_name, r["asin"], res["feed_id"])
                            for r in batch])
                    # ⚠ 事件**只在 submitted 时记**(2026-08-16 feed 闭环审计):
                    # dedup 挂的是旧 feed_id、这一轮什么都没提交,记了就是幽灵
                    # 事件 —— catalog.product_risk 的 retire_times 直接数它,
                    # 灌水后"这个 SKU 被停用过几次"就不再是事实。
                    # 冷却表那条不用管:ON CONFLICT DO NOTHING 本身幂等,
                    # 而且 dedup 说明 RETIRE 确实在途,冷却本来就该起算。
                    if res["outcome"] == "submitted":
                        product_events.record_many(conn, [
                            {"sku": r["asin"], "store": store_name,
                             "event": product_events.RETIRE_SUBMITTED,
                             "source": "sku_locked_heal",
                             "detail": {"feed_id": res["feed_id"],
                                        "reason": "sku_locked"}}
                            for r in batch])
            elif res["outcome"] == "failed":
                out.append(f"  ⚠ {store_name} RETIRE 一批 {res['count']} 条"
                           f"提交被拒,下轮重试")
            else:
                out.append(f"  ⚠ {store_name} RETIRE 一批 {res['count']} 条"
                           f"结局不确定,已留 pending 待对账,本轮不落冷却")
        if n_ok:
            out.append(f"  {store_name}:退役提交 {n_ok} 条,进入 24h 冷却")
        return out

    to_retry: list[tuple] = []
    for store_name, srows in sorted(by_store.items()):
        store = stores_by_name.get(store_name)
        if store is None:
            lines.append(f"  {store_name}:凭证缺失,{len(srows)} 行跳过")
            continue
        if not execute:
            skus = [r["asin"] for r in srows]
            lines.append(f"  [DRY-RUN] {store_name} 将退役 {len(skus)} 个:"
                         f"{','.join(skus[:8])}{' …' if len(skus) > 8 else ''}")
            continue
        try:
            lines.extend(_retire_store(store, store_name, srows))
        except _client.StoreDeadError as e:
            logger.error("%s", e)
            lines.append(f"  {store_name}:凭证失效跳过({len(srows)} 行下轮再试)")
        except Exception as e:
            # 店级隔离(标准①,2026-08-26):此前这是串行循环里的裸奔点 ——
            # 第一家店抛异常,后面的店一家都轮不到(2026-08-07 cleanup 的
            # 同款事故形态,那次修的是 cleanup,这里漏了)
            logger.exception("店铺 %s RETIRE 提交异常(待串行补试): %s",
                             store_name, e)
            to_retry.append(({"name": store_name, "_store": store,
                              "_srows": srows}, e))
    if to_retry:
        recovered, still, gate_note = store_retry.serial_second_pass(
            to_retry,
            lambda st: _retire_store(st["_store"], st["name"], st["_srows"]),
            total_stores=len(by_store))
        if gate_note:
            lines.append(gate_note)
        for _st, out in recovered:
            lines.extend(out)
        for st, e in still:
            lines.append(f"  ⚠ {st['name']}:提交异常已跳过(串行补试仍失败,"
                         f"{store_retry.diagnose(e)}:{e});已发出的分片已"
                         f"逐片落冷却台账,下轮自动排除,未发出的下轮接续")
    return lines


def _relist(ripe: list[tuple], locked_by_pair: dict, stores_by_name: dict,
            cooldown_hours: int, execute: bool) -> list[str]:
    """③ 冷却期满 + 回执成功 → 清列;回执失败 → 标 failed 点名人工。"""
    lines = []
    if not ripe:
        return lines
    if not execute:
        # dry-run 不动台账不轮询:只报告将核验多少条
        lines.append(f"  [DRY-RUN] 冷却期满(≥{cooldown_hours}h)待核回执 "
                     f"{len(ripe)} 条,回执成功即清列重上")
        return lines
    clear_rows, waiting, failed = [], 0, []
    burn_pairs: list[tuple[str, str]] = []   # RETIRE 成功即烧旧号(标 conflict)
    receipts: dict[str, dict] = {}
    for store_name, sku, feed_id, _at in ripe:
        if feed_id not in receipts:
            receipts[feed_id] = feed_track.item_results(feed_id)
            if sku not in receipts[feed_id]:
                # 台账还没有终态:主动轮询一次(product_clear 同款)
                store = stores_by_name.get(store_name)
                if store is not None:
                    try:
                        _head, st = feed_track.poll_feed(store, feed_id)
                        if st:
                            receipts[feed_id] = st
                    except Exception as e:
                        logger.warning("RETIRE feed %s 轮询失败,本轮跳过: %s",
                                       feed_id, e)
        st = receipts[feed_id].get(sku)
        if st is None or st[0] == "submitted":
            waiting += 1
            continue
        row = locked_by_pair.get((store_name, sku))
        if st[0] == "success":
            with db.pg_conn() as conn:
                conn.execute(_SQL_CLOSE, ("cleared", store_name, sku))
            # 旧号永久弃用(2026-08-19,配 claim 的原号复用逻辑):SKU 绑死过
            # 它,退役后谁也不能再用;不烧的话下一轮 claim 会把它复用回来,
            # "清列重上领新号"就成了空话
            burn_pairs.append((store_name, sku))
            if row is not None:
                clear_rows.append(row["rownum"])
            else:
                # 行已被人工改过(不再 SKU_LOCKED):关闭冷却但不碰表
                logger.info("%s/%s 冷却期满但行已非 SKU_LOCKED,只关冷却不清列",
                            store_name, sku)
        else:
            with db.pg_conn() as conn:
                conn.execute(_SQL_CLOSE, ("failed", store_name, sku))
            failed.append(f"{store_name}/{sku}({st[1]})")
    if burn_pairs:
        with db.pg_conn() as conn:
            n_burn = upc_pool.burn_for_retire(conn, burn_pairs)
        if n_burn:
            lines.append(f"  退役烧号 {n_burn} 个(标 conflict,重上必领新号)")
    n = listing_sheet.clear_for_relist(clear_rows, execute)
    if n:
        lines.append(f"  清列重上 {n} 行(下一轮 list_new 领新 UPC 重提交)")
    if waiting:
        lines.append(f"  冷却期满但回执未到 {waiting} 条,继续等")
    if failed:
        lines.append(f"  ⚠ RETIRE 回执失败 {len(failed)} 条,标 failed 请人工处置:"
                     + ",".join(failed[:5]))
    return lines


def run(params: dict) -> str:
    """输入:params(execute/store/cooldown_hours)→ 输出:退役+清列摘要。"""
    execute = bool(params.get("execute"))
    cooldown_hours = int(params.get("cooldown_hours", 24))

    rows = listing_sheet.read_rows()
    locked = [r for r in rows if r["list_result"] == "SKU_LOCKED"]
    if params.get("store"):
        locked = [r for r in locked if r["store"] == params["store"]]
    locked_by_pair = {(r["store"], r["asin"]): r for r in locked}

    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_OPEN, (cooldown_hours,))
        state = cur.fetchall()
    if params.get("store"):
        # 店铺过滤必须同时作用于冷却记录,否则会把别店的冷却
        # "行已非 SKU_LOCKED"误判并关闭
        state = [r for r in state if r[0] == params["store"]]
    open_pairs = {(r[0], r[1]) for r in state if r[4] == "pending"}
    failed_pairs = {(r[0], r[1]) for r in state if r[4] == "failed"
                    and (r[0], r[1]) in locked_by_pair}
    ripe = [(r[0], r[1], r[2], r[3]) for r in state
            if r[4] == "pending" and r[5]]

    mode = "" if execute else "🧪 [DRY-RUN] "
    lines = [f"{mode}sku_locked_heal:上架表 SKU_LOCKED {len(locked)} 行,"
             f"冷却中 {len(open_pairs)},其中期满 {len(ripe)}"]
    if not locked and not ripe:
        return lines[0]

    stores_by_name = {s["name"]: s for s in stores_svc.load_stores()}
    lines += _retire(locked, open_pairs, failed_pairs, stores_by_name, execute)
    lines += _relist(ripe, locked_by_pair, stores_by_name,
                     cooldown_hours, execute)
    return "\n".join(lines)
