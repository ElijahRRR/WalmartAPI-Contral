"""problem_product_cleanup — 问题商品处置执行件(批次 E 拆分后;危险:缺省即真跑,空跑用 --dry-run)。

用法:
  python cli.py problem_product_cleanup --dry-run      # 空跑:将执行哪些建议
  python cli.py problem_product_cleanup                # 真跑(反补/删除/停用 + 落账)
  python cli.py problem_product_cleanup -p store=A085朱丽霖

**本工作流不再做任何决策**(批次 E,批复 #8)。它只做三件事:
  ① 落定上一轮:把 executing 的建议按观测判决置 confirmed / ineffective;
  ② 领取 ops.dispositions 里 status='suggested' 的建议行;
  ③ 按 (店铺, 动作) 分桶发 feed,提交成功的转 executing 并落 feed_id。

决策(查库 → 归类 → 该反补还是该删)在 `problem_scan`。为什么拆:原来一个
文件里既做只读的定性、又做不可逆的 DELETE_ITEM,想看看该删哪些就得跑一个
DANGEROUS 工作流;而且建议不留痕,事后无从追"当初为什么删它"。

⚠ **调度顺序是硬约束**:catalog_sync → problem_scan → 本工作流(真跑)。
没跑 scan 就跑本工作流 = 消费上一轮的陈旧建议(或者什么都没有)。

去重与观测纪律(拆分后各归其位,一条没丢):
  · 决策期预筛(在途 48h 封顶 / 反补 30 天计数 / 顽固代际)→ 归 problem_scan;
  · **提交期权威防重** → api/feeds.submit_feed 的 ops.feed_log(返回
    outcome=dedup,本文件如实计数不落事件);
  · 建议行本身的防重 → ops.dispositions 的部分唯一索引(同 店铺/SKU/动作
    同时只能有一条未落定行)+ claim() 只取 suggested;
  · 生效确认 → services/dispositions.settle(),读的是 catalog_sync 经
    product_events.verify_deletions 落的 delete_verified / delete_not_effective
    ——"不信回执信观测"那套已经在跑,这里不重写第二份判定。

店铺闸(非 ACTIVE 跳过)在 problem_scan;本工作流只认凭证在不在。

容错(2026-08-07 所有者定稿,原样保留):单店提交异常只跳过该店不炸整轮;
第一轮全部店跑完后,对网络波动失败的店(token/代理阶段确定未达 retryable,
或提交块抛异常)整店**二轮重提一次**——已成功切片被在途防重拦下,不重复不漏;
二轮仍失败交下轮调度。凭证失效(StoreDeadError)与 4xx 被拒不触发重试。

⚠ 切换纪律:上调度前必须停旧 walmart-daily-cleanup cron(0/6/12/18 点)。
"""

import logging
from datetime import datetime

from api import _client, feeds
from registry import db
from services import dispositions
from services import problem_products as pp
from services import kpi, maint_sheet
from services import product_events, store_limits, stores as stores_svc

DANGEROUS = True

logger = logging.getLogger("workflows.problem_product_cleanup")

# 领取到的建议行按 (店铺, 动作) 分桶后,发 feed 要的载荷各不相同:
# relist 需要 gtin/upc 重建 MP_MAINTENANCE 条目,delete/retire 只要 sku。
# scan 把 gtin/upc 放在建议行的 detail 里带过来——执行件不再回头查库,
# 避免"决策看到的数据"与"执行用的数据"在两个时间点上不一致。
_ACTION_FEED = {
    "relist": ("MP_MAINTENANCE", product_events.MAINTENANCE_SUBMITTED, "反补"),
    "retire": ("RETIRE_ITEM", product_events.RETIRE_SUBMITTED, "顽固停用"),
    "delete": ("DELETE_ITEM", product_events.DELETE_SUBMITTED, "删除"),
}
# 发 feed 的顺序有讲究:先救活再删。同一 SKU 若同时被建议 retire 与 delete
# (顽固双击),两条都要发;但 relist 与 delete 在正常路径上互斥,不会同现。
_ACTION_ORDER = ("relist", "retire", "delete")


def group_by_store(rows: list[dict]) -> dict[str, dict]:
    """输入:建议行列表 → 输出:{店铺: {动作: [行]}}。纯函数,可测。"""
    out: dict[str, dict] = {}
    for r in rows:
        bucket = out.setdefault(r["store"], {a: [] for a in _ACTION_ORDER})
        if r["action"] in bucket:
            bucket[r["action"]].append(r)
        else:                       # 建议表里出现了不认识的动作:宁炸不吞
            raise ValueError(f"未知 action={r['action']!r}(建议行 id={r['id']})")
    return out


def _record(conn, store: str, event: str, rows: list[dict], feed_id) -> None:
    """建议行 → 产品事件。source 保持 'problem_product_cleanup' 不变:
    problem_scan 的 _SQL_ATTEMPTS 按 source 数反补次数,改名会让 30 天窗口内
    的历史计数归零,"反补满 2 次转删"重新从 0 数起 ⇒ 商品被无限反补。"""
    product_events.record_many(conn, [
        {"sku": r["sku"], "store": store, "event": event,
         "source": "problem_product_cleanup",
         "detail": {"feed_id": feed_id, "category": r.get("category"),
                    "disposition_id": r["id"],
                    "reason": (r.get("reason") or "")[:200]}}
        for r in rows])


def _retire_caps() -> dict[str, int]:
    """输入:无 → 输出:{店铺: 单轮破坏类上限}(限额表「下架限制」)。

    读不到表**不是退到不限**,是让每家店退到 `dispositions.DESTRUCTIVE_PER_STORE`
    ——由 cap_destructive 按缺省值处理。fail-closed 是这道闸唯一的方向。
    """
    try:
        return store_limits.retire_caps()
    except Exception as e:                  # noqa: BLE001 — 读表失败不炸链
        logger.warning("限额表「下架限制」读不到(%s):本轮破坏类按每店 %d 条封顶",
                       e, dispositions.DESTRUCTIVE_PER_STORE)
        return {}


def run(params: dict) -> str:
    """输入:params(execute/store)→ 输出:落定 + 领取 + 提交结果摘要。"""
    execute = bool(params.get("execute"))
    only = params.get("store")
    with db.pg_conn() as conn:
        settled = dispositions.settle(conn) if execute else {
            "confirmed": 0, "ineffective": 0}
        # ⚠ 限**动作**不限来源(2026-08-24 改):本工作流是破坏动作的唯一
        # 出口,维护链建议的删除(source='maint', action='delete')也由它执行。
        # 不限动作会领到 title/price/inventory,group_by_store 直接抛。
        rows = [r for r in dispositions.claim(conn, dispositions.PROBLEM_ACTIONS)
                if not only or r["store"] == only]
    # 单店删除上限**只在这里施加一次**(2026-08-24 归一):此前两条扫描件各按
    # 同一张限额表「下架限制」截一次,每店最多 N 条实际变成了最多 2N。
    rows, over_cap = dispositions.cap_destructive(
        rows, _retire_caps(), dispositions.DESTRUCTIVE_PER_STORE)

    mode = "" if execute else "🧪 [DRY-RUN] "
    lines = []
    if execute and (settled["confirmed"] or settled["ineffective"]):
        lines.append(f"上一轮落定:生效 {settled['confirmed']},"
                     f"**未生效 {settled['ineffective']}**"
                     f"(回执成功但观测显示没动,下轮 problem_scan 会重新建议)")
    if not rows:
        return "\n".join(lines + [
            f"{mode}没有待执行的处置建议 —— 先跑 `python cli.py problem_scan`"
            f"(catalog_sync → problem_scan → 本工作流,顺序是硬约束)"])

    plans = group_by_store(rows)
    tot = {a: sum(len(b[a]) for b in plans.values()) for a in _ACTION_ORDER}
    by_src: dict[str, int] = {}
    for r in rows:
        src = (r.get("detail") or {}).get("source") or "scan"
        by_src[src] = by_src.get(src, 0) + 1
    # ⚠ 措辞随模式走(与 maintenance 同一处理,所有者 2026-08-17 实见):
    # 这一行在**提交之前**生成,真跑完再说「待执行」就是谎话——那些行此刻
    # 已经是 executing,而通知开头写着 ✅ 成功,人会以为什么都没干。
    # 也不能说「已执行」:领取的行里有一部分会被单店上限/在途防重挡下。
    head = "待执行建议" if not execute else "本轮领取建议"
    lines.insert(0, f"{mode}{head} {len(rows)} 条:反补 {tot['relist']},"
                    f"删除 {tot['delete']},顽固停用 {tot['retire']}")
    if over_cap:
        # 截断必须见人(本仓口诀:静默截断读起来就是"全做完了")
        lines.append(f"  ⚠ 超单店「下架限制」留到下轮:"
                     + ",".join(f"{s}×{n}" for s, n in sorted(over_cap.items()))
                     + f"(共 {sum(over_cap.values())} 条,**没有丢弃**)")

    if not execute:
        # 按动作分开列样本(人眼闸门的判断依据:反补=救活方向,
        # 删除=不可逆方向,混排会误导)
        for store, b in sorted(plans.items()):
            if not any(b[a] for a in _ACTION_ORDER):
                continue
            cats: dict[str, int] = {}
            for r in b["delete"] + b["relist"]:
                cats[r.get("category") or "-"] = cats.get(r.get("category") or "-", 0) + 1
            line = (f"  {store}:反补 {len(b['relist'])},删除 {len(b['delete'])}"
                    + (f",顽固停用 {len(b['retire'])}" if b["retire"] else "")
                    + ",类别={" + ",".join(f"{c}:{v}" for c, v in sorted(cats.items()))
                    + "}")
            if b["delete"]:
                line += f",删除样本={[(r['sku'], r.get('category')) for r in b['delete'][:5]]}"
            if b["relist"]:
                line += f",反补样本={[(r['sku'], r.get('category')) for r in b['relist'][:3]]}"
            lines.append(line)
        return "\n".join(lines + ["(dry-run:未提交任何 feed;确认无误后**去掉 --dry-run** 重跑)"])

    stores_by_name = {s["name"]: s for s in stores_svc.load_stores()}
    today = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d")
    records: list[tuple] = []       # 维护记录表的行(见文件末尾一次性写出)

    def _round(names: list[str]) -> tuple[dict, list[str]]:
        """输入:要跑的店铺名 → 输出:({店铺: 该店的行}, 需二轮重试的店)。

        跨店并发(所有者定稿 2026-08-17)。每店各写各的 lines_s,主线程按店名
        排序合并 —— 往共享 list 上追加不会坏数据(GIL),但摘要行序会按完成
        先后乱序交织,同一轮跑两次输出都不一样,没法对拍。
        安全性:每店有自己的固定出口代理,配额与令牌桶都按 (store, endpoint) 计,
        跨店并发不挤同一个桶;单店失败隔离本来就在 _submit_store 里。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        out: dict[str, list[str]] = {}
        retry: list[str] = []

        def _one(store_name: str):
            lines_s: list[str] = []
            recs_s: list[tuple] = []        # 各店各写各的,主线程按店名合并
            store = stores_by_name.get(store_name)
            if store is None:
                lines_s.append(f"  {store_name}:凭证缺失,跳过")
                # 领到了却没执行的建议**也要写表**(与 maintenance 同一纪律):
                # 不写的话它在飞书完全不可见,看起来像"扫描件没建议它",
                # 而它其实每天都在建议、每天都没做成
                for action in _ACTION_ORDER:
                    label = _ACTION_FEED[action][2]
                    recs_s.extend(_sheet_row(store_name, r, label, "", "",
                                             today, "未执行(凭证缺失)")
                                  for r in plans[store_name].get(action) or [])
                return store_name, lines_s, recs_s, False
            need = _submit_store(store_name, store, plans[store_name], lines_s,
                                 recs_s, today)
            return store_name, lines_s, recs_s, need

        with ThreadPoolExecutor(
                max_workers=min(stores_svc.STORE_WORKERS, len(names))) as pool:
            for f in as_completed([pool.submit(_one, n) for n in names]):
                name, lines_s, recs_s, need = f.result()
                out[name] = lines_s
                records.extend(recs_s)
                if need:
                    retry.append(name)
        return out, retry

    first = sorted(plans)
    got, retry_stores = _round(first)
    for name in first:
        lines.extend(got.get(name, []))

    if retry_stores:
        # 二轮重试(所有者定稿 2026-08-07):第一轮全部店跑完后,对网络波动
        # 失败的店整店重提一次。衔接安全:第一轮已成功的切片仍在途,被
        # feed_log 在途防重拦下(dedup 不落账不重发);token 阶段确定未达的
        # 切片已落 failed 可重占,同载荷重提。二轮仍失败交下轮调度,不无限重试。
        # 建议行侧同样安全:已转 executing 的行不会被本轮再领(claim 只取
        # suggested),二轮重提的是同一批 rows 对象,不会重复转态。
        retry_stores.sort()
        lines.append(f"二轮重试 {len(retry_stores)} 店:{','.join(retry_stores)}")
        got2, still = _round(retry_stores)
        for name in retry_stores:
            lines.extend(got2.get(name, []))
            if name in still:
                lines.append(f"  ⚠ {name}:二轮仍失败,待下轮调度")

    # 维护记录表(2026-08-24):删除归口到本工作流之后不写表的话,所有者的
    # 这张面板就再也看不见删除流水了 —— 那是他每天看的东西。裁剪归 maintenance
    # 一处做(同一张表两处裁会各按各的水位重复读全段)
    maint_sheet.publish(records, lines, prune_after=False)
    lines.append("结果轮询走 feed_poll;生效确认在下一轮本工作流开头"
                 "(等 catalog_sync 重新观测)")
    return "\n".join(lines)


def _sheet_row(store: str, r: dict, suggestion: str, action: str,
               feed_id, today: str, result: str, err="") -> tuple:
    """输入:店铺 + 建议行 + 实际动作 + 结果 → 输出:维护记录表的一行。

    造行走 `maint_sheet.build_row`(唯一造行处,与 maintenance 同一个函数)。
    「旧值/新值」空:破坏/反补动作没有值的变化,那两列是维护三类用的。
    """
    return maint_sheet.build_row(store, r["sku"], suggestion,
                                 r.get("reason") or "", action,
                                 feed_id, today, result, err)


def _submit_store(store_name: str, store: dict, b: dict,
                  lines: list[str], records: list[tuple], today: str) -> bool:
    """输入:店铺 + 按动作分桶的建议行 → 输出:是否需二轮重试(网络类失败)。

    多切片滑窗对位记账(与 product_clear._submit_new 同款);**只有
    outcome=submitted 才落事件、才转 executing** —— dedup 携带旧 feed_id 但
    什么都没提交,记了就是幽灵事件:反补计数被灌水会导致少一次真实反补就转
    永久删除,而建议行会卡在 executing 等一个不存在的 feed 的判决。
    retryable=token/代理阶段确定未达;凭证失效(StoreDeadError)不重试。

    ⚠ records 是传进来的、不是返回的(与 maintenance._submit_kind 同款理由):
    提交到一半抛异常时,已经提交出去的那几片必须留下记录。返回局部列表的话,
    异常一抛连同"这几条其实发出去了"一起丢掉,表里看起来什么都没干、
    沃尔玛队列里却真在跑。
    """
    need_retry = False

    def _submit(action: str, entries: list, rows: list[dict]) -> None:
        nonlocal need_retry
        feed_type, event, label = _ACTION_FEED[action]
        i = 0
        n = {"submitted": 0, "dedup": 0, "failed": 0, "unknown": 0}
        for res in feeds.submit_feed(store, feed_type, entries,
                                     workflow="problem_product_cleanup"):
            rows_slice = rows[i:i + res["count"]]
            i += res["count"]
            n[res["outcome"]] = n.get(res["outcome"], 0) + len(rows_slice)
            if res["outcome"] == "submitted" and res["feed_id"]:
                with db.pg_conn() as conn:
                    _record(conn, store_name, event, rows_slice, res["feed_id"])
                    dispositions.mark_executing(
                        conn, [r["id"] for r in rows_slice], res["feed_id"],
                        by="problem_product_cleanup")
                records.extend(_sheet_row(store_name, r, label, label,
                                          res["feed_id"], today, "处理中")
                               for r in rows_slice)
            elif res["outcome"] == "dedup":
                # 动作写「跳过」而不是类型名:在途防重是**没有提交**,写成
                # "删除"会让表格看起来做了两次删除(与 maintenance 同一口径)
                records.extend(_sheet_row(store_name, r, label, "跳过",
                                          res["feed_id"] or "", today, "在途防重")
                               for r in rows_slice)
            elif res["outcome"] == "failed":
                records.extend(_sheet_row(store_name, r, label, label, "",
                                          today, "提交被拒")
                               for r in rows_slice)
            else:                       # unknown:结局不确定,留 pending 待对账
                records.extend(_sheet_row(store_name, r, label, label,
                                          res["feed_id"] or "", today, "处理中")
                               for r in rows_slice)
            if res.get("retryable"):
                need_retry = True
        line = f"  {store_name}:{label}提交 {n['submitted']}"
        if n["dedup"]:
            line += f",在途防重跳过 {n['dedup']}"
        if n["failed"]:
            line += f",⚠ 提交失败 {n['failed']}(查日志)"
        if n["unknown"]:
            line += f",⚠ 结局不确定留 pending {n['unknown']}(待对账)"
        lines.append(line)

    # 单店隔离(2026-08-07 生产实证:单店代理 TLS 断线炸掉整轮,
    # 后面的店全部没轮到):任何异常只跳过该店,其余店继续
    try:
        for action in _ACTION_ORDER:
            rows = b.get(action) or []
            if not rows:
                continue
            if action == "relist":
                # gtin/upc 由 problem_scan 放在建议行 detail 里带过来:
                # 执行件不回头查库,免得"决策看到的"与"执行用的"两个时间点不一致
                entries = [pp.build_relist_item(
                    r["sku"], (r.get("detail") or {}).get("gtin"),
                    (r.get("detail") or {}).get("upc")) for r in rows]
                keep = [(e, r) for e, r in zip(entries, rows) if e]
                if len(keep) != len(rows):
                    # 建到建议时能组出条目、现在组不出 = detail 丢了 gtin/upc。
                    # 静默少发几条比什么都不做更坏:那些行会一直挂 suggested
                    lines.append(f"  ⚠ {store_name}:{len(rows) - len(keep)} 条反补"
                                 f"建议缺 gtin/upc 组不出条目,跳过(重跑 problem_scan)")
                if not keep:
                    continue
                _submit(action, [e for e, _ in keep], [r for _, r in keep])
            else:
                _submit(action, [r["sku"] for r in rows], rows)
    except _client.StoreDeadError as e:
        logger.error("店铺 %s 凭证失效,跳过(不重试): %s", store_name, e)
        lines.append(f"  {store_name}:凭证失效跳过")
        return False
    except Exception as e:
        logger.exception("店铺 %s 提交异常: %s", store_name, e)
        lines.append(f"  ⚠ {store_name}:提交异常({e}),转入二轮重试")
        return True
    return need_retry
