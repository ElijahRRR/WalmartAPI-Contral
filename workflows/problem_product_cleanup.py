"""problem_product_cleanup — 问题商品处置执行件(批次 E 拆分后;危险:缺省即真跑,空跑用 --dry-run)。

用法:
  python cli.py problem_product_cleanup --dry-run      # 空跑:将执行哪些建议
  python cli.py problem_product_cleanup                # 真跑(删除/停用 + 落账)
  python cli.py problem_product_cleanup -p store=A085朱丽霖

**本工作流不再做任何决策**(批次 E,批复 #8)。它只做三件事:
  ① 落定上一轮:把 executing 的建议按观测判决置 confirmed / ineffective;
  ② 领取 ops.dispositions 里 status='suggested' 的建议行;
  ③ 按 (店铺, 动作) 分桶发 feed,提交成功的转 executing 并落 feed_id。

决策(查库 → 归类 → 落建议)在 `problem_scan`。为什么拆:原来一个
文件里既做只读的定性、又做不可逆的 DELETE_ITEM,想看看该删哪些就得跑一个
DANGEROUS 工作流;而且建议不留痕,事后无从追"当初为什么删它"。

⚠ **调度顺序是硬约束**:catalog_sync → problem_scan → 本工作流(真跑)。
没跑 scan 就跑本工作流 = 消费上一轮的陈旧建议(或者什么都没有)。

去重与观测纪律(拆分后各归其位,一条没丢):
  · 决策期预筛(在途 48h 封顶 / 顽固代际)→ 归 problem_scan;
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
二轮的「串行/只补一次/凭证死不补/失败店过多即止损」四条判据 2026-08-27 起
全部走公共标准件 `services.store_retry.serial_second_pass`(店级重试标准①,
docs/conventions.md §四),本文件不再自维护一份。

⚠ 缺席探测失败 → **fail-closed**(2026-08-27):本工作流是破坏动作的唯一
出口,探测挂了就当轮一条都不放行(见 run() 内注释),延后一轮无损。

⚠ 切换纪律:上调度前必须停旧 walmart-daily-cleanup cron(0/6/12/18 点)。
"""

import logging
from datetime import datetime

from api import _client, feeds
from registry import db
from services import dispositions
from services import kpi, maint_sheet, notify_fmt as nf
from services import product_events, store_absence, store_limits, \
    store_retry, stores as stores_svc

DANGEROUS = True
SUPPORTS_STORE = True   # 接受 -p store=X 单店范围(cli 链尾缺席店重赛靠它识别)

logger = logging.getLogger("workflows.problem_product_cleanup")

# ⚠ 单店「下架限制」暂停开关(所有者定稿 2026-08-28:「暂时关闭这个限制」)。
# 背景:08-28 沃尔玛档案可见性事件把万级退市死档案翻回响应集,反补退役后
# 这批全走删除 —— 按日限额(限额表/缺省 300 每店)要削几十天,清理波期间
# 不封顶,让积压当轮出清。True 期间:不读限额表、不按日记账、不截断,
# 摘要**首行**点名(静默的闸没人记得它关着);缺席避让 fail-closed、
# 在途防重、feed 速率桶(DELETE_ITEM 6/hour、单 feed ≤2500 条)**均不受
# 此开关影响** —— 出闸节奏仍被速率桶天然限住,关的只是"每天最多删多少"。
# 恢复:清理波结束后改回 False(title_mismatch 停闸同款先例:常量停闸、
# 摘要点名、恢复即改回;有用例钉住 True 现状,改回时同步改用例)。
RETIRE_CAP_PAUSED = True

# 领取到的建议行按 (店铺, 动作) 分桶后发 feed,delete/retire 载荷都只要 sku。
# ⚠ relist(反补)2026-08-28 所有者定稿退役(「非 PUBLISHED 一律删除,不再改
# End Date 救商品」):本件不再领取也不再执行它(dispositions.PROBLEM_ACTIONS
# 已收窄),存量 suggested 由扫描件 withdraw_stale 撤,executing 由 settle 收尾。
_ACTION_FEED = {
    "retire": ("RETIRE_ITEM", product_events.RETIRE_SUBMITTED, "顽固停用"),
    "delete": ("DELETE_ITEM", product_events.DELETE_SUBMITTED, "删除"),
}
# 同一 SKU 若同时被建议 retire 与 delete(顽固双击),两条都要发:
# 先停用后删除,能删的删,删不掉的至少已经停用
_ACTION_ORDER = ("retire", "delete")


class _RetryNeeded(Exception):
    """本店有网络类失败(retryable 切片 / 提交异常)→ 交串行补试。

    ⚠ 以**异常**而不是返回标志传递(2026-08-27 接线):店级补试的唯一实现是
    `services.store_retry.serial_second_pass`(标准①,conventions §四),它按
    「attempt 抛异常 = 这家店失败」判。此前本文件用 need 标志自造了一份
    `_round(workers=1)`,规模闸(2026-08-26 加进标准件的 max(3, 总数//5)
    系统性故障止损)就漏在了外面 —— 代理商区域挂掉时标准件止损,这条链仍
    逐店串行补试放大故障时长。

    随身带走本次已产生的摘要行与维护记录行:异常一抛局部列表就没了,而
    「已经提交出去的那几片必须留下记录」(见 _submit_store 头注)。
    """

    def __init__(self, store: str, lines: list[str], records: list[tuple]):
        super().__init__(f"{store}:本轮提交有网络类失败(待串行补试)")
        self.lines = lines
        self.records = records


def _record(conn, store: str, event: str, rows: list[dict], feed_id) -> None:
    """建议行 → 产品事件。source 保持 'problem_product_cleanup' 不变:
    病历(product_events)按 source 追溯是谁提交的,批次 E 拆分前后与
    反补退役(2026-08-28)前后必须连续,改名会把历史断代。"""
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
        # 缺席避让(店级重试标准③补全,2026-08-26 对抗校验):扫描件避让了
        # 缺席店、withdraw 还特意护住了它们的存量 suggested —— 执行件不避让
        # 的话,这批按**隔夜观测**建议的删除会在同一轮链里照样领走、照样发
        # DELETE_ITEM。缺席店的行原地留在 suggested,等重赛/下轮观测刷新后
        # 由扫描件重新定夺。
        absent, absence_note = store_absence.stale_or_note(conn, only=only)
        n_absent_held = 0
        if absent:
            n_absent_held = sum(1 for r in rows if r["store"] in absent)
            rows = [r for r in rows if r["store"] not in absent]
        # ⚠ 探测失败 → **fail-closed**(2026-08-27 改;此前是 fail-open「不避让
        # = 改前行为」)。本工作流是破坏动作的唯一出口:缺席探测挂了 = 不知道
        # 哪些店的观测是隔夜的,而 stale_stores → _watermarks → enabled_names
        # 走飞书,一次飞书抖动就能同时打开这里和上游 problem_scan 两道关卡。
        # conventions §六 的取舍方向:兜底是补偿外部世界的缺陷,不是补偿自己
        # 的不确定 —— 拿不准就不删,延后一轮无损(下轮 problem_scan 重新建议)。
        # 同文件 _retire_caps 的配额闸早就是这个方向,两道闸不再一个收紧一个
        # 放开。维护件 maintenance 反向维持 fail-open,理由写在那边(可逆)。
        n_fail_closed = 0
        if absence_note:
            n_fail_closed, rows = len(rows), []
        # 单店删除上限**只在这里施加一次**(2026-08-24 归一),且按**天**记账
        # (2026-08-26):当日已放行的先扣掉,链尾重赛/人工重跑不会把上限翻倍
        executed_today = ({} if RETIRE_CAP_PAUSED
                          else dispositions.destructive_executed_today(conn))
    if RETIRE_CAP_PAUSED:
        # 停闸期间不读限额表、不记账、不截断 —— 但必须在摘要**首行**点名
        # (拼进 head 那一行,见下;静默的闸没人记得它关着)
        over_cap = {}
    else:
        rows, over_cap = dispositions.cap_destructive(
            rows, _retire_caps(), dispositions.DESTRUCTIVE_PER_STORE,
            executed_today=executed_today)

    mode = "" if execute else "🧪 [DRY-RUN] "
    lines = []
    if execute and (settled["confirmed"] or settled["ineffective"]):
        lines.append(f"上一轮落定:生效 {settled['confirmed']},"
                     f"**未生效 {settled['ineffective']}**"
                     f"(回执成功但观测显示没动,下轮 problem_scan 会重新建议)")
    if n_absent_held:
        lines.append(f"⚠ 缺席避让:{n_absent_held} 条建议属于缺席店(目录未刷新),"
                     f"留在 suggested 原地 —— 隔夜观测不配开破坏 feed")
    if absence_note:
        # 点名在**首行**:链通知对成功步骤只发首行(cli.first_line_of),
        # 写在后面等于只写进日志 —— 而"今天一条都没删"必须一眼看见。
        # 归因(异常类名)由 store_absence 落日志:这里不引它那句「本轮不
        # 避让」,本件的降级方向是全停,措辞对不上
        lines.insert(0, f"{mode}⚠ 缺席探测失败,本轮破坏动作全停(fail-closed)"
                        f":{n_fail_closed} 条建议留在 suggested 原地,"
                        f"下轮重试(探测归因见日志)")
        return "\n".join(lines)
    if not rows:
        return "\n".join(lines + [
            f"{mode}没有待执行的处置建议 —— 先跑 `python cli.py problem_scan`"
            f"(catalog_sync → problem_scan → 本工作流,顺序是硬约束)"])

    # 分桶件在 services.dispositions(两个执行件共用,2026-08-27 上移):
    # 「未知动作即抛」的宁炸不吞判据原样在里面(conventions §三的安全闸)
    plans = dispositions.group_by_store(rows, key="action",
                                        order=_ACTION_ORDER, id_field="id")
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
    cap_note = (";⚠ 单店「下架限制」停用中(2026-08-28 暂时定稿,不封顶;"
                "恢复改 RETIRE_CAP_PAUSED=False)" if RETIRE_CAP_PAUSED else "")
    lines.insert(0, f"{mode}{head} {len(rows)} 条:"
                    f"删除 {tot['delete']},顽固停用 {tot['retire']}{cap_note}")
    if over_cap:
        # 截断必须见人(本仓口诀:静默截断读起来就是"全做完了")
        lines.append(f"  ⚠ 超单店「下架限制」留到下轮:"
                     + ",".join(f"{s}×{n}" for s, n in sorted(over_cap.items()))
                     + f"(共 {sum(over_cap.values())} 条,**没有丢弃**)")

    if not execute:
        # 按动作分开列样本(人眼闸门的判断依据:停用=可逆方向,
        # 删除=不可逆方向,混排会误导)
        for store, b in sorted(plans.items()):
            if not any(b[a] for a in _ACTION_ORDER):
                continue
            cats: dict[str, int] = {}
            for r in b["delete"]:
                cats[r.get("category") or "-"] = cats.get(r.get("category") or "-", 0) + 1
            line = (f"  {store}:删除 {len(b['delete'])}"
                    + (f",顽固停用 {len(b['retire'])}" if b["retire"] else "")
                    + ",类别={" + ",".join(f"{c}:{v}" for c, v in sorted(cats.items()))
                    + "}")
            if b["delete"]:
                line += f",删除样本={[(r['sku'], r.get('category')) for r in b['delete'][:5]]}"
            lines.append(line)
        return "\n".join(lines + ["(dry-run:未提交任何 feed;确认无误后**去掉 --dry-run** 重跑)"])

    stores_by_name = {s["name"]: s for s in stores_svc.load_stores()}
    today = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d")
    records: list[tuple] = []       # 维护记录表的行(见文件末尾一次性写出)

    def _one(store_name: str) -> tuple:
        """输入:店铺名 → 输出:(店铺名, 该店的摘要行, 该店的记录行)。

        网络类失败抛 `_RetryNeeded`(随身带走本次的行与记录),交
        `store_retry.serial_second_pass` 串行补试 —— 补试跑的就是本函数,
        不另写简化版(单一落地路径纪律)。
        """
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
            return store_name, lines_s, recs_s
        if _submit_store(store_name, store, plans[store_name], lines_s,
                         recs_s, today):
            raise _RetryNeeded(store_name, lines_s, recs_s)
        return store_name, lines_s, recs_s

    def _fan_out(names: list[str]) -> tuple[dict, list[tuple]]:
        """输入:要跑的店铺名 → 输出:({店铺: 该店的行}, [(店铺, 首轮异常)] 待补试)。

        跨店并发(所有者定稿 2026-08-17)。每店各写各的 lines_s,主线程按店名
        排序合并 —— 往共享 list 上追加不会坏数据(GIL),但摘要行序会按完成
        先后乱序交织,同一轮跑两次输出都不一样,没法对拍。
        安全性:每店有自己的固定出口代理,配额与令牌桶都按 (store, endpoint) 计,
        跨店并发不挤同一个桶;单店失败隔离本来就在 _submit_store 里。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        out: dict[str, list[str]] = {}
        to_retry: list[tuple] = []
        with ThreadPoolExecutor(
                max_workers=min(stores_svc.STORE_WORKERS, len(names))) as pool:
            futs = {pool.submit(_one, n): n for n in names}
            for f in as_completed(futs):
                try:
                    name, lines_s, recs_s = f.result()
                except _RetryNeeded as e:
                    # 标准①:此刻**不判生死**,跑完别人再串行补试;本轮已发
                    # 出去的那几片的记录随异常带回来,一行不丢
                    name, lines_s, recs_s = futs[f], e.lines, e.records
                    to_retry.append(({"name": name}, e))
                out[name] = lines_s
                records.extend(recs_s)
        return out, to_retry

    first = sorted(plans)
    got, to_retry = _fan_out(first)
    for name in first:
        lines.extend(got.get(name, []))

    if to_retry:
        # 二轮重试(所有者定稿 2026-08-07):第一轮全部店跑完后,对网络波动
        # 失败的店整店重提一次。衔接安全:第一轮已成功的切片仍在途,被
        # feed_log 在途防重拦下(dedup 不落账不重发);token 阶段确定未达的
        # 切片已落 failed 可重占,同载荷重提。二轮仍失败交下轮调度,不无限重试。
        # 建议行侧同样安全:已转 executing 的行不会被本轮再领(claim 只取
        # suggested),二轮重提的是同一批 rows 对象,不会重复转态。
        # 补试**串行**、只补一次、凭证死不补、失败店过多即止损:四条判据全部
        # 由 services.store_retry 一处维护(标准①,2026-08-26 定稿)。
        retry_stores = sorted(st["name"] for st, _e in to_retry)
        got2: dict[str, list[str]] = {}

        def _attempt(st: dict) -> str:
            """store_retry 的 attempt 契约:成功返回、失败抛。行与记录照收。"""
            try:
                name, lines_s, recs_s = _one(st["name"])
            except _RetryNeeded as e:
                got2[st["name"]] = e.lines
                records.extend(e.records)
                raise
            got2[name] = lines_s
            records.extend(recs_s)
            return name

        _saved, still, gate_note = store_retry.serial_second_pass(
            sorted(to_retry, key=lambda p: p[0]["name"]), _attempt,
            total_stores=len(first))
        still_names = {st["name"] for st, _e in still}
        # 规模闸拦下整批时不谎报「二轮重试」:止损原文必须进摘要(标准②)
        lines.append(gate_note or f"二轮重试 {len(retry_stores)} 店(串行):"
                                  f"{','.join(retry_stores)}")
        for name in retry_stores:
            lines.extend(got2.get(name, []))
            if name in still_names:
                lines.append(f"  ⚠ {name}:"
                             + ("未补试(疑似系统性故障)" if gate_note
                                else "二轮仍失败")
                             + ",待下轮调度")

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
    「旧值/新值」空:破坏动作没有值的变化,那两列是维护三类用的。
    """
    return maint_sheet.build_row(store, r["sku"], suggestion,
                                 r.get("reason") or "", action,
                                 feed_id, today, result, err)


def _submit_store(store_name: str, store: dict, b: dict,
                  lines: list[str], records: list[tuple], today: str) -> bool:
    """输入:店铺 + 按动作分桶的建议行 → 输出:是否需二轮重试(网络类失败)。

    多切片滑窗对位记账(与 product_clear._submit_new 同款);**只有
    outcome=submitted 才落事件、才转 executing** —— dedup 携带旧 feed_id 但
    什么都没提交,记了就是幽灵事件(病历灌水),而建议行会卡在 executing
    等一个不存在的 feed 的判决。
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
        n = {"submitted": 0, "dedup": 0, "failed": 0, "unknown": 0}
        # 多切片滑窗对位走 api/feeds 的公共游标(6 个工作流曾各写一遍
        # `rows[i:i+count]; i += count`,错一位就是整批结局落到别人行上)
        for res, rows_slice in feeds.iter_result_slices(
                feeds.submit_feed(store, feed_type, entries,
                                  workflow="problem_product_cleanup"), rows):
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
        # 四档计数的尾巴走 notify_fmt 的成品件(三个执行件逐字重复过);
        # failed 档字样由调用方给 —— 本件现行「提交失败」,maintenance 现行
        # 「提交被拒」,收口时逐字保留两处现状,不替所有者统一措辞
        lines.append(f"  {store_name}:{label}"
                     + nf.feed_outcome_tail(n["submitted"], n["dedup"],
                                            n["failed"], n["unknown"],
                                            failed_word="提交失败"))

    # 单店隔离(2026-08-07 生产实证:单店代理 TLS 断线炸掉整轮,
    # 后面的店全部没轮到):任何异常只跳过该店,其余店继续
    try:
        for action in _ACTION_ORDER:
            rows = b.get(action) or []
            if not rows:
                continue
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
