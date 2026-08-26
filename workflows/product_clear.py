"""product_clear — 飞书停用/删除表驱动的商品清理(plan #7,替代旧 daily_retire/沃尔玛批量下架;危险:缺省即真跑,空跑用 --dry-run)。

用法:
  python cli.py product_clear --dry-run       # 空跑:打印将提交什么、将回写什么
  python cli.py product_clear                 # 真跑(提交 feed + 回写表格)
  python cli.py product_clear -p limit=100    # 单店单日上限覆盖(默认 300,限额表接入前)
  python cli.py product_clear -p store=A085朱丽霖

驱动表(registry.RETIRE_SHEET,电子表格,列序即契约):
  A=store  B=sku  C=停用/删除  D=操作原因 | E=feedid  F=操作日期  G=结果  H=报错
  A~D 运营填,E~H 程序写(读写积木在 services/clear_sheet,feed_poll 共用:
  全局轮询落台账后顺带反哺 G/H,不依赖本工作流再跑一次)。

行状态机:
  E 空 + C 合法        → 待提交(受单店单日上限约束,超限行留到下一轮)
  E 有 + G 空/处理中    → 轮询 feed 终态,逐 SKU 回写 G=成功/失败/未查到,H=报错码
  G=失败/未查到 的行    → **不自动重试**(高危写操作;运营核对原因后清空 E 列
                          即重新排队——feeds 层 failed 记录允许同载荷重占)

动作映射(2026-08-06 所有者定稿):停用/下架 → RETIRE_ITEM(可恢复);
删除或 **C 列留空 → DELETE_ITEM**(永久,仅自发货)。提交走 api/feeds 唯一通道

单店单日上限:优先读上下架限额表(registry.RETIRE_LIMITS,按店铺分行,
「下架限制」列);店铺不在表内退 -p limit 默认值并**告警**(旧系统静默兜底
300 无告警,店铺名拼错就漏放,已修);限额表未登记时全店统一 -p limit。
(切片/三层防重/反查三态),状态权威在 ops.feed_log,飞书 E~G 只是展示
——旧系统"状态只存飞书三列"的结构性风险(2026-05-07 事故根源)就此消除。

⚠ 切换纪律:本工作流上生产调度前,必须先停旧系统 walmart-daily-retire(旧名)
cron(每天 15:00),新旧并跑 = 重复删除。
"""

import logging
from datetime import datetime

from api import _client, feeds, feishu
from registry import db, resources
from services import clear_sheet, feed_track, kpi, product_events, store_retry, \
    stores as stores_svc

DANGEROUS = True

logger = logging.getLogger("workflows.product_clear")

# C 列留空默认删除(所有者定稿 2026-08-06)
_ACTIONS = {"停用": "RETIRE_ITEM", "下架": "RETIRE_ITEM",
            "删除": "DELETE_ITEM", "": "DELETE_ITEM"}


def _poll_feeds(rows: list[dict], stores_by_name: dict,
                execute: bool) -> tuple[list, list[str]]:
    """轮询已提交行的 feed 终态 → 逐 SKU 回写结果。"""
    updates, lines = [], []
    pollable = [r for r in rows
                if r["feed_id"] and r["result"] in clear_sheet.POLLABLE]
    by_feed: dict[str, list[dict]] = {}
    for r in pollable:
        by_feed.setdefault(r["feed_id"], []).append(r)

    done = still = 0
    for feed_id, frows in by_feed.items():
        store = stores_by_name.get(frows[0]["store"])
        if store is None:
            continue
        if not execute:
            # dry-run 连台账都不动:feed_items/feed_log 的落定交给真跑
            # 或 feed_poll;这里只报告在途数量
            still += len(frows)
            continue
        try:
            # 统一轮询积木:SKU 终态落 ops.feed_items 权威台账 + feed_log 落终态
            _head, sku_status = feed_track.poll_feed(store, feed_id)
        except Exception as e:
            logger.warning("feed %s 状态查询失败,本轮跳过: %s", feed_id, e)
            continue
        if sku_status is None:
            still += len(frows)
            continue
        for r in frows:
            outcome, code = sku_status.get(r["sku"], ("missing", ""))
            result = clear_sheet.RESULT_TEXT[outcome]
            err = code if result == "失败" else ""
            if result != r["result"] or err != r["error"]:
                updates.append((r["rownum"], r["feed_id"], r["op_date"],
                                result, err))
            done += 1
    if by_feed:
        lines.append(f"轮询:{len(by_feed)} 个 feed,{done} 行落定,{still} 行仍处理中")
    return updates, lines


def _load_limits(default: int) -> tuple[dict[str, int], str]:
    """输入:默认上限 → 输出:({店铺: 单日下架上限}, 摘要说明)。

    读上下架限额表「下架限制」列;未登记时返回空表(全店走默认)。
    """
    t = resources.RETIRE_LIMITS
    f = t.fields
    try:
        recs = feishu.list_records(t, field_names=[f.store, f.max_daily_retire])
    except LookupError:
        return {}, f"限额表未登记,全店统一上限 {default}"
    limits: dict[str, int] = {}
    for rec in recs:
        name = feishu._plain_text(rec["fields"].get(f.store)).strip()
        try:
            v = int(float(feishu._plain_text(rec["fields"].get(f.max_daily_retire))
                          or 0))
        except ValueError:
            v = 0
        if name and v > 0:
            limits[name] = v
    return limits, f"限额表生效({len(limits)} 店)"


def _submit_new(rows: list[dict], stores_by_name: dict, limits: dict[str, int],
                default_limit: int, execute: bool) -> tuple[list, list[str]]:
    """待提交行 → 按 店铺×动作 分组提交(受单店单日上限),回写 E~G。"""
    updates, lines = [], []
    today = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d")
    fresh = [r for r in rows if not r["feed_id"]]

    bad = [r for r in fresh if r["action"] not in _ACTIONS or
           r["store"] not in stores_by_name]
    for r in bad:
        why = "动作不识别" if r["action"] not in _ACTIONS else "店铺不识别"
        if r["result"] != why:
            updates.append((r["rownum"], "", "", why, ""))
    unknown_stores = sorted({r["store"] for r in bad
                             if r["store"] not in stores_by_name})
    if unknown_stores:
        logger.warning("店铺不识别 %d 个:表内样本=%s;凭证表样本=%s"
                       "(店名必须与凭证表逐字一致)",
                       len(unknown_stores), unknown_stores[:5],
                       sorted(stores_by_name)[:5])
    good = [r for r in fresh if r not in bad]

    by_store: dict[str, list[dict]] = {}
    for r in good:
        by_store.setdefault(r["store"], []).append(r)

    def _limit_of(store_name: str) -> int:
        """输入:店铺名 → 输出:该店单轮上限(限额表缺该店则回落默认并告警)。

        提交与摘要**共用这一处** —— 摘要那行原本拿的是 for 循环泄漏出来的
        `limit`,也就是用**最后一个店**的上限去估算所有店(各店上限不同时报的
        数就是错的,而且不报错)。并发化把它变成 NameError,才暴露出来。
        """
        limit = limits.get(store_name)
        if limit is None:
            if limits:      # 限额表在用但查不到该店:旧系统在这里静默兜底,已改告警
                logger.warning("店铺 %s 不在限额表,按默认上限 %d(请核对店铺名拼写)",
                               store_name, default_limit)
            limit = default_limit
        return limit

    def _one_store(store_name: str, srows: list[dict]) -> tuple:
        """输入:店铺 + 该店待提交行 → 输出:(店铺名, updates, lines, 提交数, 延后数)。

        **各店各自的局部状态**,主线程再合并 —— 跨店并发之后不能再往共享
        list/计数器上写:list.append 在 GIL 下不会坏数据,但 `submitted += n`
        是"读-加-写"三步,两个线程交错会**丢计数**(而且丢得随机、不报错);
        摘要行序也会按完成先后乱序交织,同一轮跑两次输出不一样,没法对拍。

        ⚠ updates 写进 `partial[store]`(逐片追加),不是纯局部变量
        (2026-08-26 对抗校验):第 2 片抛异常时第 1 片已经真的 submitted,
        局部变量随异常丢掉 = 那一片在停用/删除表上零记录 → 下一轮把它当
        「待提交」重占重发(feed_poll 30 分钟内就会把 feed_log 打成 done,
        在途防重只在这窗口内有效)→ DELETE 重发 + delete_times 事件灌水。
        补试重跑同一店会再走一遍全部分片:已落地的分片按 dedup 返回同一
        feed_id,updates 里最多出现取值相同的重复行,写表幂等。
        """
        updates_s: list = partial.setdefault(store_name, [])
        lines_s: list[str] = []
        submitted_s = 0
        limit = _limit_of(store_name)
        take, defer = srows[:limit], srows[limit:]
        by_action: dict[str, list[dict]] = {}
        for r in take:
            by_action.setdefault(_ACTIONS[r["action"]], []).append(r)
        for feed_type, arows in by_action.items():
            skus = [r["sku"] for r in arows]
            if not execute:
                logger.info("[DRY-RUN] %s %s 将提交 %d 个 SKU:%s%s",
                            store_name, feed_type, len(skus), skus[:8],
                            " …" if len(skus) > 8 else "")
                lines_s.append(f"[DRY-RUN] {store_name} {feed_type} 待提交 {len(skus)}")
                continue
            results = feeds.submit_feed(stores_by_name[store_name], feed_type,
                                        skus, workflow="product_clear")
            i = 0
            for res in results:
                slice_rows = arows[i:i + res["count"]]
                i += res["count"]
                if res["outcome"] in ("submitted", "dedup") and res["feed_id"]:
                    submitted_s += len(slice_rows)
                    for r in slice_rows:
                        updates_s.append((r["rownum"], res["feed_id"], today,
                                          "处理中", ""))
                    # 产品事件账本:提交事件(含操作原因,病历的"医嘱"部分)。
                    # ⚠ **只在 submitted 时记**(2026-08-16 feed 闭环审计):
                    # dedup 挂的是旧 feed_id、这一轮什么都没提交,记了就是幽灵
                    # 事件 —— catalog.product_risk 的 delete_times/retire_times
                    # 直接数它,灌水后"这个 SKU 被删过几次"就不再是事实。
                    # 表格那几列照写不误:在途 feed 的结果确实会落到这几行上。
                    if res["outcome"] == "submitted":
                        with db.pg_conn() as conn:
                            product_events.record_many(conn, [
                                {"sku": r["sku"], "store": store_name,
                                 "event": f"{product_events.feed_kind(feed_type)}_submitted",
                                 "source": "product_clear",
                                 "detail": {"feed_id": res["feed_id"],
                                            "reason": r["reason"]}}
                                for r in slice_rows])
                elif res["outcome"] == "failed":
                    for r in slice_rows:
                        updates_s.append((r["rownum"], "", "", "提交被拒", ""))
                else:   # unknown:保持 pending 待启动对账,行不动
                    lines_s.append(f"⚠ {store_name} {feed_type} 一批 {res['count']} 条"
                                   f"提交结果不确定,已留 pending 待对账")
        return store_name, updates_s, lines_s, submitted_s, len(defer)

    # 跨店并发(所有者定稿 2026-08-17)。每店有自己的固定出口代理,配额与令牌桶
    # 都按 (store, endpoint) 计,跨店不挤同一个桶;单店上限仍按限额表逐店算。
    # ⚠ **店内串行不变**:同一店的多个 feed_type 仍在 _one_store 里顺序提交。
    submitted = deferred = 0
    todo = sorted(by_store.items())
    if todo:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        per_store: dict[str, tuple] = {}
        to_retry: list[tuple] = []
        partial: dict[str, list] = {}     # 各店逐片追加的 updates(异常也不丢)
        with ThreadPoolExecutor(
                max_workers=min(stores_svc.STORE_WORKERS, len(todo))) as pool:
            futs = {pool.submit(_one_store, n, s): (n, s) for n, s in todo}
            for f in as_completed(futs):
                n, s = futs[f]
                try:
                    name, u, ls, sub, defr = f.result()
                    per_store[name] = (u, ls, sub, defr)
                except _client.StoreDeadError as e:
                    logger.error("%s", e)
                    per_store[n] = (partial.get(n, []),
                                    [f"  {n}:凭证失效跳过,"
                                     f"{len(s)} 行下轮重试"], 0, 0)
                except Exception as e:
                    # 店级隔离 + 补试(标准①,2026-08-26)。此前这里一家店抛
                    # 异常整轮 run() 直接炸,而 writeback 排在 _submit_new 之后
                    # —— **已经真发出去的 feed 在停用/删除表上一个字都不回写**,
                    # 运营看到"什么都没干"而沃尔玛队列里 DELETE_ITEM 正在跑
                    # (problem_product_cleanup 头注同款坑,那边修了这边没修)
                    logger.exception("店铺 %s 提交异常(待串行补试): %s", n, e)
                    to_retry.append(((n, s), e))
        if to_retry:
            recovered, still, gate_note = store_retry.serial_second_pass(
                [({"name": n, "_srows": s}, e) for (n, s), e in to_retry],
                lambda st: _one_store(st["name"], st["_srows"]),
                total_stores=len(todo))
            for _st, (name, u, ls, sub, defr) in recovered:
                per_store[name] = (u, ls, sub, defr)
            for st, e in still:
                # 已发出的分片的回写在 partial 里,照写不丢 —— 表上有 feed_id
                # 的行下一轮不会被当「待提交」重占重发
                per_store[st["name"]] = (
                    partial.get(st["name"], []),
                    [f"  ⚠ {st['name']}:提交异常已跳过(串行补试仍失败,"
                     f"{store_retry.classify(e)}:{e});已发出分片已回写表格,"
                     f"未发出的下轮接续"], 0, 0)
            if gate_note:
                lines.append(gate_note)
        # 按店名排序合并:完成先后随机,updates 与摘要的顺序不能跟着随机
        for name, _ in todo:
            u, ls, sub, defr = per_store[name]
            updates.extend(u)
            lines.extend(ls)
            submitted += sub
            deferred += defr

    if good or bad:
        lines.append(f"提交:有效待提交 {len(good)} 行,本轮"
                     f"{'提交' if execute else '将提交'} {submitted if execute else min(len(good), sum(min(len(v), _limit_of(k)) for k, v in by_store.items()))} 行,"
                     f"超限延后 {deferred} 行,无效行 {len(bad)}")
    return updates, lines


def run(params: dict) -> str:
    """输入:params(execute/limit/store)→ 输出:轮询+提交结果摘要。"""
    execute = bool(params.get("execute"))
    default_limit = int(params.get("limit", 300))

    rows = clear_sheet.read_rows()
    if not rows:
        return "停用/删除表无数据行"
    only = params.get("store")
    if only:
        rows = [r for r in rows if r["store"] == only]

    # 全量加载凭证(49 店,量小):店名对不上时诊断需要完整对照样本
    stores_by_name = {s["name"]: s for s in stores_svc.load_stores()}
    limits, limits_note = _load_limits(default_limit)

    updates_a, lines_a = _poll_feeds(rows, stores_by_name, execute)
    updates_b, lines_b = _submit_new(rows, stores_by_name, limits,
                                     default_limit, execute)
    written = clear_sheet.writeback(updates_a + updates_b, execute)

    mode = "" if execute else "🧪 [DRY-RUN] "
    lines = [f"{mode}product_clear:表内 {len(rows)} 行({limits_note})"] \
        + lines_a + lines_b
    if execute:
        lines.append(f"回写 {written} 行")
    return "\n".join(lines)
