"""feed 生命周期追踪积木(所有 feed 操作共用的轮询机制)。

分工(2026-08-06 定稿):
  api/feeds     负责 提交 + feed_log/feed_items 的**落台账**(提交时)
  本模块        负责 轮询 submitted feed → SKU 级终态回写 ops.feed_items
                + feed_log 落 done/failed + pending 行对账告警
  workflows     feed_poll 薄壳全局轮询;各业务工作流(daily_retire 等)
                用 poll_feed 拿 {sku: 结果} 去刷各自的飞书投影列

SKU 级状态权威在 ops.feed_items;停用/删除/设置到期日期/未来的上架、改价、
改库存、改标题 feed 全走这一套,不许各工作流自造轮询。
"""

import logging

from api import feeds
from registry import db, resources
from services import blacklist, product_events, stores as stores_svc

logger = logging.getLogger("services.feed_track")

# feedType → 业务动作名(摘要展示;未登记的原样显示)
_FEED_LABEL = {"DELETE_ITEM": "删除", "RETIRE_ITEM": "停用",
               "MP_MAINTENANCE": "维护", "MP_ITEM": "上架",
               "PRICE_AND_PROMOTION": "改价", "price": "改价",
               "inventory": "改库存"}

# SKU 台账状态 → 飞书表结果列文案。状态词只有一个出处(api/feeds.sku_outcome
# 的 success/failed/processing/unknown,加台账自己的 submitted/missing),中文面
# 此前有四份拷贝:clear_sheet:27 / maint_sheet:221 / maint_sheet:343(同一文件
# 里又内联一份)/ match_sheet:104。本常量是四份的**并集**——processing/unknown
# 两键只有 clear_sheet 那份有,而 product_clear 是 `RESULT_TEXT[outcome]` 直接
# 下标取(不是 .get),少一键就是 KeyError,不许"看着重复"就删。
RESULT_TEXT = {"success": "成功", "failed": "失败", "missing": "未查到",
               "submitted": "处理中", "processing": "处理中",
               "unknown": "处理中"}


def text_of(status: str, err: str = "") -> str:
    """输入:台账状态(+可选「码 | 人话」报错)→ 输出:飞书表结果列文案。

    未登记的状态一律按未落定报「处理中」:不装成功也不装失败,下轮反哺器
    还会再看一次(与 api/feeds.sku_outcome 对未知枚举的态度同口径)。
    `err` 只在 failed 档拼进去,形状照跟卖表现行的「失败:{报错}」;其余档
    给了 err 也不拼——成功/未查到后面挂一串报错码没有意义。

    ⚠ maint_sheet 补写口径(:262)是 `.get(status, status)`:未登记状态**原样
    落表**,给人看的是"台账里到底写了什么"。那一处与本函数的「处理中」不是
    等价替换,接线时(P1c)要么保留原样落表、要么请所有者拍一次口径,别当
    成同一件事悄悄改掉。
    """
    if err and status == "failed":
        return f"失败:{err}"
    return RESULT_TEXT.get(status, "处理中")


def error_text(errs: list[dict]) -> str:
    """输入:ingestionError 列表 → 输出:人话描述串(带字段名,多条以 ; 连接)。

    只存数字错误码无法诊断(2026-08-09 上架首跑教训:EXT_DATA_ERROR_507165…
    这种码本身不含任何信息),description/field 才是能修的线索。
    """
    parts = []
    for e in errs or []:
        desc = str(e.get("description") or e.get("message") or "").strip()
        field = str(e.get("field") or "").strip()
        if field and desc:
            parts.append(f"[{field}] {desc}")
        elif desc or field:
            parts.append(desc or f"[{field}]")
    return "; ".join(parts)[:900]


def _save_errors(cur, feed_id: str, store: str, all_errs: dict[str, list[dict]],
                 meta: dict) -> int:
    """输入:游标 + feed + 每 SKU 报错列表 → 输出:落账条数(幂等重入不重复)。

    一条 ingestionError 一行进 ops.feed_item_errors——**拉详情是标准动作**:
    报错是系统自我优化的燃料,聚合看 ops.v_feed_error_stats。
    """
    rows = []
    for sku, errs in all_errs.items():
        wf, ft = (meta.get(sku) or ("", ""))[:2]
        for i, e in enumerate(errs or []):
            rows.append((feed_id, sku, i,
                         str(e.get("type") or "") or None,
                         str(e.get("code") or "") or None,
                         str(e.get("field") or "") or None,
                         str(e.get("description") or e.get("message") or "")[:2000]
                         or None,
                         ft or None, store, wf or None))
    if not rows:
        return 0
    cur.executemany(
        "INSERT INTO ops.feed_item_errors (feed_id, sku, seq, error_type, code,"
        " field, description, feed_type, store, workflow)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (feed_id, sku, seq) DO NOTHING", rows)
    return len(rows)


def _progress(head: dict) -> str:
    """输入:feed 汇总 → 输出:进度串(feed 级 GET 自带计数,零明细翻页)。"""
    recv = head.get("itemsReceived") or 0
    ok = head.get("itemsSucceeded") or 0
    bad = head.get("itemsFailed") or 0
    pending = head.get("itemsProcessing")
    if pending is None:
        pending = max(recv - ok - bad, 0)
    return f"已收 {recv},成功 {ok},失败 {bad},待处理 {pending}"


def poll_feed(store: dict, feed_id: str) -> tuple[dict, dict | None]:
    """输入:店铺 + feed_id → 输出:(feed 汇总 head, SKU 结果)。

    未终态:结果为 None(head 自带 itemsReceived/Succeeded/Failed 进度计数,
    不翻明细);终态:ops.feed_items 逐 SKU 落 success/failed(+错误码),
    台账里有而明细里查无的 SKU 落 missing;feed_log 落 done/failed。
    """
    head = feeds.get_feed_status(store, feed_id)
    if head.get("feedStatus") not in feeds.FEED_TERMINAL:
        return head, None

    results: dict[str, tuple[str, str]] = {}
    descs: dict[str, str] = {}
    all_errs: dict[str, list[dict]] = {}
    for item in feeds.iter_feed_items(store, feed_id):
        sku = str(item.get("sku") or "")
        if not sku:
            continue
        errs = (item.get("ingestionErrors") or {}).get("ingestionError") or []
        code = str(errs[0].get("code") or errs[0].get("type") or "") if errs else ""
        descs[sku] = error_text(errs)
        all_errs[sku] = errs
        results[sku] = (feeds.sku_outcome(item.get("ingestionStatus")), code)

    _STATUS = {"success": "success", "failed": "failed",
               "processing": "submitted", "unknown": "submitted"}
    n_unresolved = sum(1 for o, _ in results.values()
                       if o in ("processing", "unknown"))
    with db.pg_conn() as conn, conn.cursor() as cur:
        # 先取更新前状态:残留 processing/unknown 时 feed 会被重轮询,
        # 回执事件只对"本轮才落定"的 SKU 记,重轮询不得重复灌账
        cur.execute("SELECT sku, workflow, feed_type, status FROM ops.feed_items "
                    "WHERE feed_id = %s", (feed_id,))
        meta = {sku: (wf, ft, st) for sku, wf, ft, st in cur.fetchall()}
        cur.executemany(
            "UPDATE ops.feed_items SET status = %s, error_code = %s, "
            "error_desc = %s, resolved_at = now() "
            "WHERE feed_id = %s AND sku = %s",
            [(_STATUS[o], code or None, descs.get(sku) or None, feed_id, sku)
             for sku, (o, code) in results.items()])
        # 报错明细同步落账(标准动作,不是排障时才拉)
        _save_errors(cur, feed_id, store["name"], all_errs, meta)
        # 台账里有、终态明细里查无 → missing(不装成功也不装失败)
        cur.execute(
            "UPDATE ops.feed_items SET status = 'missing', resolved_at = now() "
            "WHERE feed_id = %s AND status = 'submitted' AND NOT (sku = ANY(%s))",
            (feed_id, list(results) or [""]))
        n_missing = cur.rowcount
        # 产品事件账本:逐 SKU 回执落账(success 是沃尔玛的一面之词,
        # 删除的最终真相由 catalog_sync 观测核验)。
        # 入账白名单(所有者定稿 2026-08-07):改价/改库存/改标题/清库存等
        # 维护回执不进病历,流水已在 ops.feed_items——receipt_in_ledger 收口
        product_events.record_many(conn, [
            {"sku": sku, "store": store["name"],
             "event": f"{product_events.feed_kind(meta[sku][1])}_feed_{o}",
             "source": meta[sku][0] or "feed_poll",
             "error_code": code or None, "detail": {"feed_id": feed_id}}
            for sku, (o, code) in results.items()
            if sku in meta and o in ("success", "failed")
            and meta[sku][2] == "submitted"
            and product_events.receipt_in_ledger(
                product_events.feed_kind(meta[sku][1]), meta[sku][0])])
        # 违禁回执自动进 ASIN 黑名单(所有者 2026-08-12:上架失败事件要能
        # 反哺"上架前拦截")。三违禁码 = 沃尔玛官方判定的政策违禁
        # (Military/Law Enforcement、Firearm Accessories、General Prohibited),
        # 2026-09-03 换轨后归新码 **POLICY**(旧码是 B=禁售;两者都在
        # PERMANENT 里,拦截行为一字不变,变的只是码名统一到新表)。
        # DO NOTHING 幂等 —— list_new/match_listing 的黑名单闸下次自动拦,
        # 同一产品不再烧 UPC 与配额。
        # 只收 kind=list(MP_ITEM)。⚠ **「sku=asin 约定」已随切码作废**:黑名单键
        # 由 blacklist.record_asins 经登记簿按 (店,sku) 反查 —— 本函数只负责把
        # store + sku 原样递过去,**不许在这里自己解 ASIN**(那就是第二份规则,
        # conventions §六)。跟卖走 MP_ITEM_MATCH ⇒ kind=match ⇒ 天然不进这个桶,
        # 其行内终态由跟卖表 F/J 列承担。
        # ⚠ **改码失败不是政策违禁,不得反哺黑名单**(SKU 改造批次 3,O8):
        # 形态 B 下 sku_migrate 走 MP_ITEM ⇒ kind=list ⇒ 正好命中这段反哺。
        # 一次改码被拒若碰巧带上违禁码,会把一个**正在正常销售**的 ASIN 永久
        # 拉黑(record_asins 是 PERMANENT),list_new/match_listing 的黑名单闸
        # 下一轮就开始拦,而没有任何摘要会说是改码干的。既有工作流名一个都不
        # 叫 sku_migrate ⇒ 改码前逐字节零行为变化。meta[sku][0] 是提交来源工作流。
        prohibited = [
            {"store": store["name"], "sku": sku, "category": "POLICY",
             "reasons": f"上架回执违禁 {(code or '').strip()}|"
                        f"{(descs.get(sku) or '')[:150]}"}
            for sku, (o, code) in results.items()
            if o == "failed" and sku in meta
            and product_events.feed_kind(meta[sku][1]) == "list"
            and meta[sku][0] != "sku_migrate"
            and (code or "").strip() in resources.WALMART_ERR_PROHIBITED]
        if prohibited:
            n_bl = blacklist.record_asins(conn, prohibited)
            logger.warning("上架回执命中政策违禁 %d 个,新入 ASIN 黑名单 %d 个"
                           "(POLICY=违反禁售政策,上架前拦截自此生效):%s",
                           len(prohibited), n_bl,
                           ",".join(p["sku"] for p in prohibited[:10]))
    if n_missing:
        logger.warning("feed %s:%d 个 SKU 在终态明细中查无,已标 missing",
                       feed_id, n_missing)
    if n_unresolved:
        # feed 终态但个别 SKU 仍 INPROGRESS/未知:feed_log 保持 submitted,
        # 下轮再查——否则这些行永久卡 submitted,cleanup 在途拦截会永远跳过它们
        logger.warning("feed %s 已终态但 %d 个 SKU 仍 processing/unknown,"
                       "保持在途下轮重查", feed_id, n_unresolved)
    else:
        feeds.mark_feed_done(feed_id, head.get("feedStatus") == "PROCESSED")
    return head, results


def poll_all(stores_by_name: dict) -> str:
    """输入:{店铺名: store dict} → 输出:全局轮询摘要。

    扫 feed_log 全部 submitted 行**跨店并发、店内串行**地轮询;pending 行
    (提交结局不确定,仅网络 UNKNOWN 结局产生)只告警不自动补交——写操作宁停不重。

    为什么跨店能并发:每店有自己的固定出口代理,沃尔玛配额按 `(store, endpoint)`
    计,`api/_client` 的令牌桶也按这个维度限流——店与店之间不抢同一个桶。
    店内保持串行则是因为 `feeds.get_feed_status` / `iter_feed_items` 对**同一个店**
    才是同一个桶,并发只会让自己排队等退避。

    (所有者定稿 2026-08-17:「feed_poll 应该也设置为跨店并发,但店内可以串行」。
    改之前一个店挂了会顶着整轮的轮询时间,而 feed_poll 是挂高频调度的那一条。)
    """
    rows = feeds.query_pending()
    submitted = [r for r in rows if r["status"] == "submitted" and r["feed_id"]]
    pendings = [r for r in rows if r["status"] == "pending"]

    by_store: dict[str, list[dict]] = {}
    for r in submitted:
        by_store.setdefault(r["store"], []).append(r)

    def _one_store(store_name: str, srows: list[dict]) -> tuple:
        """输入:店铺名 + 该店在途 feed → 输出:(落定, 仍处理中, 跳过, 明细行)。

        **各店各自的局部计数**,主线程再合并:`done += 1` 是"读-加-写"三步,
        两个线程交错会丢计数(丢得随机、不报错);明细行直接 append 则会按完成
        先后乱序交织,同一轮跑两次输出不一样。
        """
        done_s = still_s = skipped_s = 0
        out: list[str] = []
        store = stores_by_name.get(store_name)
        for r in srows:
            label = _FEED_LABEL.get(r["feed_type"], r["feed_type"])
            fid_disp = ((r["feed_id"][:18] + "…") if len(r["feed_id"]) > 19
                        else r["feed_id"])
            who = f"{r['store']} {label}({r['workflow'] or '-'}) {fid_disp}"
            if store is None:
                skipped_s += 1
                out.append(f"  {who}:店铺凭证缺失,跳过")
                continue
            try:
                head, results = poll_feed(store, r["feed_id"])
            except Exception as e:
                logger.warning("feed %s 轮询失败(下轮再试): %s", r["feed_id"], e)
                still_s += 1
                out.append(f"  {who}:查询失败({e}),下轮再试")
                continue
            if results is None:
                still_s += 1
                out.append(f"  {who}:{head.get('feedStatus')},{_progress(head)}")
            else:
                done_s += 1
                n_ok = sum(1 for o, _ in results.values() if o == "success")
                n_bad = sum(1 for o, _ in results.values() if o == "failed")
                out.append(f"  {who}:已落定 {head.get('feedStatus')},"
                           f"成功 {n_ok},失败 {n_bad}")
        return done_s, still_s, skipped_s, out

    done = still = skipped = 0
    detail_lines: list[str] = []
    # 摘要按人看的店铺序排(sort_key),**不是**按完成先后:query_pending 本身
    # 没有 ORDER BY,原来那份顺序是 PG 的堆序,本来就不稳定。
    todo = sorted(by_store.items(), key=lambda kv: stores_svc.sort_key(kv[0]))
    if todo:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        per_store: dict[str, tuple] = {}
        with ThreadPoolExecutor(
                max_workers=min(stores_svc.STORE_WORKERS, len(todo))) as pool:
            futs = {pool.submit(_one_store, sn, sr): sn for sn, sr in todo}
            for f in as_completed(futs):
                per_store[futs[f]] = f.result()
        for sn, _ in todo:
            d, s, k, out = per_store[sn]
            done += d
            still += s
            skipped += k
            detail_lines.extend(out)

    if pendings:
        logger.warning("feed_log 有 %d 条 pending(提交结局不确定),"
                       "请人工核对后处理:%s", len(pendings),
                       [(p["store"], p["feed_type"], str(p["created_at"]))
                        for p in pendings[:10]])
    line = (f"feed 轮询:{len(submitted)} 个在途,落定 {done},仍处理中 {still}")
    if skipped:
        line += f",店铺凭证缺失跳过 {skipped}"
    if pendings:
        # ⚠ 只报个数**没法处理**(2026-08-16 feed 闭环审计):摘要是发去飞书的
        # 那一份,人看到"pending 3"接下来要干什么?明细只在日志里,而 pending
        # 行**永不老化**——不落定就永远挂着,数字只增不减,几轮之后这行警告就
        # 成了背景噪音。把店铺/类型/时间摊开,至少能拿去 Walmart 后台对。
        line += f";⚠ pending 待人工核对 {len(pendings)}"
        detail_lines.append(
            "  pending(提交结局不确定,**系统不会自动补交**——"
            "核对后手工处理,见 docs/feed_closure_audit.md):")
        for p in pendings[:10]:
            detail_lines.append(
                f"    {p['store']} {p['feed_type']}"
                f"({p.get('workflow') or '-'}) 提交于 {p['created_at']}")
        if len(pendings) > 10:
            detail_lines.append(f"    …另有 {len(pendings) - 10} 条,查 "
                                f"ops.feed_log WHERE status='pending'")
    return "\n".join([line] + detail_lines)


def item_results(feed_id: str) -> dict[str, tuple[str, str]]:
    """输入:feed_id → 输出:{sku: (status, error_code)}(读 ops.feed_items 台账)。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT sku, status, error_code FROM ops.feed_items "
                    "WHERE feed_id = %s", (feed_id,))
        return {sku: (status, code or "") for sku, status, code in cur.fetchall()}


def merge_error(code: str | None, desc: str | None, limit: int = 900) -> str:
    """输入:错误码 + 人话描述 → 输出:回写业务表用的「码 | 人话」。

    各业务表(停用/删除、维护记录、跟卖、上架)的报错列统一用本函数拼——
    光有 EXT_DATA_ERROR_507165… 这种数字码,运营和我们都无从下手。
    """
    code, desc = (code or "").strip(), (desc or "").strip()
    if code and desc:
        return f"{code} | {desc}"[:limit]
    return (code or desc)[:limit]


def item_codes(feed_id: str) -> dict[str, set[str]]:
    """输入:feed_id → 输出:{sku: 全部错误码集合}(读 ops.feed_item_errors)。

    ops.feed_items.error_code 只留了第一个码;而一个 SKU 可能同时返回
    合规审核 + UPC 冲突 + 字段校验多个码,**正交处置**(如 UPC 冲突要标池)
    必须看全集,不能只看第一个。
    """
    out: dict[str, set[str]] = {}
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT sku, code FROM ops.feed_item_errors "
                    "WHERE feed_id = %s AND code IS NOT NULL", (feed_id,))
        for sku, code in cur.fetchall():
            out.setdefault(sku, set()).add(code.strip())
    return out


def item_errors(feed_id: str) -> dict[str, str]:
    """输入:feed_id → 输出:{sku: 人话报错描述}(空描述的 SKU 不出现)。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT sku, error_desc FROM ops.feed_items "
                    "WHERE feed_id = %s AND error_desc IS NOT NULL", (feed_id,))
        return {sku: desc for sku, desc in cur.fetchall()}
