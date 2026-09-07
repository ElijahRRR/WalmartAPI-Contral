"""维护记录工作表(registry.MAINT_SHEET)读写积木(maintenance 与 feed_poll 共用)。

列契约(A~K 共 11 列,**列序即契约,唯一权威是 registry.MAINT_SHEET.columns**;
所有者建表 2026-08-07,2026-08-16 加「建议」「原因」两列):
  A=店铺 B=SKU C=建议 D=原因 E=动作 F=旧值 G=新值 H=feedid I=日期 J=结果 K=报错
⚠ 代码里一律 `_col(名)` / `_idx(名)` 取位置,**任何地方都不许写死字母或下标** ——
  加那两列时漏改一处的表现是整表错位且不报错(prune 就漏了,读「新值」当日期)。

流水账语义:**只追加不改行**,执行件提交时一次写全 11 列(`build_row` 是
唯一造行处),程序是唯一写入方。写入方两个:`maintenance`(标题/价格/库存)与
`problem_product_cleanup`(删除/顽固停用/反补,2026-08-24 起 —— 删除归口到它
之后,不写表的话所有者的这张面板就再也看不见删除流水了)。feed 路径 H=真 feedid、J=处理中;PUT 同步路径 H="sync"、J 当场落定。
feed 路径的结果由 feed_poll 反哺器(sync_from_ledger)按 ops.feed_items 回填 J/K。
四类动作(标题/价格/库存/删除)共用本表,不另建表(所有者问 2026-08-09
「删除以后跑 feed 会填写到维护记录里吗」)。

⚠ **不要再做"扫描件先写半行、执行件回来补齐"** —— 2026-08-17 试过一轮,
所有者验完口径后撤除(「执行内容无误,不需要再向飞书写入」)。撤除前它还暴露了
一个真坑,谁要重做必须先解决:**两端算「建议」的方式不一样**。扫描件按 kind 取
`KIND_LABEL["delete"] = 删除`,而执行件 `_record` 取的是 `it.get("label")`,删除
意图在 `maintenance_intents:597` 被塞成 `删除(<原因码>)`(如 `删除(title_mismatch)`)
—— 键对不上,于是每一行都查不到、整行追加,而两边都不报错。
连接键必须两端同一个函数算出来,不能各取各的字段。

表格读取一律走 `feishu.sheet_values_rows`(唯一标准读通道:行方向分块 + 90221
对半兜底,返回 `(行号, 行值)` 对)。本文件三处读(反哺器区间 / 补写存量 / 裁剪
全表)的范围上界都随表长增长 —— 一天几千行,单发裸读在表长大后必撞飞书单响应
10MB 上限(90221 data exceeded)。**2026-08-27 生产**:反哺器读整段炸在这里,
水位一步不推 ⇒ 积压每轮重读、越读越大,靠下一轮自愈是不可能的,必须一次读完。
⚠ 行号只用通道给的那个,不许拿区间起点 + enumerate 手算:飞书会裁掉**块尾**
空行,手算会从那一块起整段错位(错位的后果是回填写到别人的行上)。

水位(ops.cursors,name='maint_sheet'):{"next_row": 下一空行, "unresolved_from":
最早未落定行}。反哺器只扫 [unresolved_from, next_row) 区间,不做全表读。
⚠ 反哺器**跨过没有 feedid 的行**(它认为那些没有待办)。当前形态下每行都是提交
时才造的、必然带 feedid 或 "sync",所以这条不成问题 —— 但它正是上面那个"先写
半行"方案的第二个坑:半行没有 feedid,水位会直接推过去,等 feedid 补进来时
反哺器再也不回头看,结果/报错永远空着且不报错。

保留期(所有者定稿 2026-08-09:「一天几千条,要不了多久飞书就很难存了」——
旧系统靠"一天一个表格"绕开):**飞书只留近 RETAIN_DAYS 天**,每轮维护提交后
自动 prune();**删的只是展示面板**,全部流水永久在 ops.feed_items/feed_log。
配套 STALE_DAYS 兜底:超 3 天仍未落定的行判「未查到」并推进水位,免得一行
悬着把水位钉死、每轮重读整段(裁剪也裁不掉它)。
"""

import json
import logging
from datetime import datetime, timedelta

from api import feishu
from registry import db, resources
from services import blacklist_sheet, feed_track, kpi

logger = logging.getLogger("services.maint_sheet")

# 列字母**从 registry.MAINT_SHEET.columns 的下标推导**,不再硬编码 A/H/I。
# 2026-08-16 所有者在飞书加了「建议」「原因」两列(9→11),硬编码的那版会把
# 「动作」写进「建议」列、把回执写进「新值」列 —— 整表错位且不报错。
# 列序即契约,契约的唯一权威是 registry;这里只按名字取。
def _col(name: str) -> str:
    return feishu._col_letter(resources.MAINT_SHEET.columns.index(name) + 1)


def _idx(name: str) -> int:
    return resources.MAINT_SHEET.columns.index(name)


_FIRST_COL = "A"


def _span() -> tuple[str, str]:
    """输入:无 → 输出:整行范围的列字母对,如 ('A', 'K')。"""
    return _FIRST_COL, feishu._col_letter(len(resources.MAINT_SHEET.columns))


_SYNC_MARK = "sync"     # PUT 同步路径的伪 feedid 标记(旧 'sync:200' 语义的收敛)
_PENDING = ("", "处理中")
_CURSOR = "maint_sheet"
_APPEND_BLOCK = 500     # 单次写飞书的行数上限(一次裹上千行会被 90202 拒)

# 未落定行的兜底与表格保留期(所有者定稿 2026-08-09)
STALE_DAYS = 3          # 超过这么多天还没终态 → 判「未查到」并推进水位
RETAIN_DAYS = 7         # 飞书只留近这么多天(一天几千行,不裁很快装不下)


def _row_date(text: str):
    """输入:I 列(日期)串 → 输出:date(解析不了返 None,当作"没日期不裁不判")。"""
    try:
        return datetime.strptime(str(text).strip()[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _today():
    return datetime.now(kpi.CN_TZ).date()


def _load_cursor(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM ops.cursors WHERE name = %s", (_CURSOR,))
        row = cur.fetchone()
    return dict(row[0]) if row else {"next_row": 2, "unresolved_from": 2}


def _save_cursor(conn, value: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.cursors (name, value) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = now()",
            (_CURSOR, json.dumps(value)))


def build_row(store: str, sku: str, suggestion: str, reason: str, action: str,
              feed_id, op_date: str, result: str, err="",
              old="", new="") -> tuple:
    """输入:一条流水的各字段 → 输出:维护记录表的一行(按 registry 列序)。

    **全项目唯一造行处**(2026-08-24 从 workflows/maintenance 提上来,因为
    problem_product_cleanup 的删除流水也要进这张表)。散在各执行件里手拼元组的
    话,加一列时漏改一处就整行错位**且不报错** —— 2026-08-16 加「建议」「原因」
    两列时已经踩过一次(prune 漏了,把「新值」当日期读)。

    「建议」是扫描件定的,「动作」是本执行件真做了什么 —— 两者分歧才是这两列
    的价值:建议删除但动作为空 = 领到了没执行,结果列写明为什么。
    """
    vals = {"store": store, "sku": sku, "suggestion": suggestion,
            "reason": reason or "", "action": action,
            "old_value": old, "new_value": new, "feed_id": feed_id,
            "op_date": op_date, "result": result,
            "error": err if err is not None else ""}
    return tuple(vals[c] for c in resources.MAINT_SHEET.columns)


def publish(rows: list[tuple], lines: list[str], *, prune_after=True) -> None:
    """输入:记录行 + 摘要行列表 → **就地** append 摘要。写表失败绝不抛。

    ⚠ **写表失败绝不能把"feed 已提交"埋进异常里**(所有者 2026-08-09 实遇:
    提交成功但表格一行没写,事后只能靠 ops.cursors 的时间戳反推)。台账在 PG,
    补写靠 `maintenance -p resync_sheet=1`。

    两个执行件共用(2026-08-24):maintenance 写维护三类,
    problem_product_cleanup 写删除/停用/反补 —— 各写各的话,"写表失败要吞掉"
    这条纪律迟早只剩一处。
    """
    if not rows:
        return
    try:
        written = append_records(rows)
        lines.append(f"维护记录追加 {written} 行;feed 结果轮询走 feed_poll")
        if prune_after:
            # 一天几千行,不裁飞书很快装不下(所有者定稿 2026-08-09:只留 7 天)。
            # 裁的只是展示面板,流水永久在 ops.feed_items
            try:
                lines.append(prune())
            except Exception as e:          # 裁剪失败不影响本轮结果
                logger.warning("维护记录裁剪失败(不影响提交): %s", e)
    except LookupError as e:
        lines.append(f"⚠ 维护记录表未登记,流水未写表(台账已在 PG):{e}")
    except Exception as e:
        logger.exception("维护记录写表失败(feed 已提交,台账在 PG): %s", e)
        lines.append(f"⚠ 维护记录写表失败:{e}"
                     f"(feed 已提交,{len(rows)} 行流水只在 PG;"
                     f"补写:python cli.py maintenance -p resync_sheet=1)")


def append_records(rows: list[tuple]) -> int:
    """输入:[(店铺, SKU, 建议, 原因, 动作, 旧值, 新值, feedid, 日期, 结果, 报错)]
    → 输出:写入行数。只追加;水位存 ops.cursors,起点先验空防覆盖。

    行必须由 `build_row` 造(全项目唯一造行处,按 registry 列序)。本行元组
    是 11 列:2026-08-16 加「建议」「原因」两列后,这条 docstring 还照旧
    9 列写着(2026-08-27 订正)—— 照它手拼元组就是整行错位且不报错。"""
    if not rows:
        return 0
    sheet = resources.MAINT_SHEET.require()
    with db.pg_conn() as conn:
        cur_state = _load_cursor(conn)
    # 找空行(防水位漂移覆盖已有数据)走 blacklist_sheet.next_empty —— **唯一
    # 实现**(2026-08-27 归一:本文件此前抄了一份逐行同形的,连那边
    # 「本算法 O(表已填行数)、涨到几十万行要换二分探测」的警告都没抄过来)。
    # ⚠ 扫描块大小(_SCAN_BLOCK)也在那边:此前 50 行/请求 —— 有游标时只扫
    #   一两块无感,但首跑/游标丢失要从第 2 行扫几千行历史 = 上百个串行请求
    #   (2026-08-19 全仓飞书逐行请求盘点)。
    # 返回值可能指到网格末尾之后:网格已满,下面 sheet_ensure_rows 会先扩行。
    start = blacklist_sheet.next_empty(resources.MAINT_SHEET,
                                       int(cur_state.get("next_row", 2)))
    feishu.sheet_ensure_rows(sheet, start + len(rows))
    cur_state.setdefault("unresolved_from", 2)

    # 分块写 + **每块成功就落水位**:中途失败时已写的部分不会被下次
    # 重复追加(所有者 2026-08-09 实遇:1000+ 行一次写被飞书拒,整轮 failed)
    written = 0
    for i in range(0, len(rows), _APPEND_BLOCK):
        block = rows[i:i + _APPEND_BLOCK]
        row0 = start + i
        try:
            feishu.sheet_write_ranges(sheet, [
                (f"A{row0}:{_span()[1]}{row0 + len(block) - 1}",
                 [[str(c) if c is not None else "" for c in r] for r in block])])
        except Exception:
            if written:
                cur_state["next_row"] = start + written
                with db.pg_conn() as conn:
                    _save_cursor(conn, cur_state)
            logger.error("维护记录写到第 %d 行时失败,已写 %d 行入账(补写用 "
                         "maintenance -p resync_sheet=1)", row0, written)
            raise
        written += len(block)
    cur_state["next_row"] = start + written
    with db.pg_conn() as conn:
        _save_cursor(conn, cur_state)
    return written


_LABEL_BY_FEED = {"MP_MAINTENANCE": "标题", "price": "价格",
                  "inventory": "库存", "DELETE_ITEM": "删除"}

_SQL_LEDGER = """
SELECT store, sku, feed_type, feed_id, status, error_code, error_desc,
       submitted_at
FROM ops.feed_items
WHERE workflow = 'maintenance'
ORDER BY submitted_at, feed_id, sku
"""


def resync_from_ledger() -> str:
    """输入:无 → 输出:补写摘要。把台账里有、表里没有的维护流水补进表格。

    用途:提交成功但写表那一步炸了(飞书超时/限流)——feed 已经发出去了,
    流水却只在 PG。本函数按 (feedid, sku) 对齐,只补缺的行,可重复跑。

    ⚠ **D/E(旧值/新值)补不回来**:PG 的 ops.feed_items 只记 SKU 级状态,
    不存当时的新旧值(那是意图层的东西,提交后就没再留)。补出来的行这两列
    为空,其余列齐全——表是展示面板,状态权威始终在 PG。
    """
    resources.MAINT_SHEET.require()
    with db.pg_conn() as conn:
        cur_state = _load_cursor(conn)
        with conn.cursor() as cur:
            cur.execute(_SQL_LEDGER)
            ledger = cur.fetchall()
    hi = int(cur_state.get("next_row", 2))
    have: set[tuple[str, str]] = set()
    if hi > 2:
        first, last = _span()
        for _rownum, raw in feishu.sheet_values_rows(
                resources.MAINT_SHEET, first, last, 2, hi - 1):
            cells = ([(str(c).strip() if c is not None else "") for c in raw]
                     + [""] * len(resources.MAINT_SHEET.columns))
            # 按列名取下标(2026-08-16 加了「建议」「原因」两列;写死 5/1 的话
            # 存量识别会拿错列 ⇒ 每轮把全部行当"表里没有"重复补写)
            have.add((cells[_idx("feed_id")], cells[_idx("sku")]))
    rows = []
    for store, sku, ftype, fid, status, code, desc, submitted in ledger:
        if (str(fid or ""), str(sku)) in have:
            continue
        # 中文面走 feed_track 的唯一出处,但**兜底仍是"原样落表"**:未登记的
        # 状态照写台账里的原文,给人看的是"库里到底写了什么"。这与
        # feed_track.text_of 的「处理中」不是等价替换,是补写口径的有意差异,
        # 不当成同一件事悄悄改掉(2026-08-27 收编时按原口径保留)。
        # ⚠ 本行与旧的 _RESULT_BY_STATUS 只在 processing/unknown 两键上不同,
        #   而这两个值**进不了 ops.feed_items**:feed_track.poll_feed 的 _STATUS
        #   (feed_track:137)落库前把它们归一成 'submitted',schema 也只登记
        #   submitted/success/failed/missing —— 真实值域上逐值等价
        result = feed_track.RESULT_TEXT.get(status, status)
        err = feed_track.merge_error(code, desc) if status == "failed" else ""
        label = _LABEL_BY_FEED.get(ftype, ftype)
        # 按列名拼(11 列):台账只有 SKU 级状态,建议/原因/旧值/新值补不回来
        vals = {"store": store, "sku": sku, "suggestion": label, "reason": "",
                "action": label, "old_value": "", "new_value": "",
                "feed_id": fid, "result": result, "error": err,
                "op_date": submitted.strftime("%Y-%m-%d") if submitted else ""}
        rows.append(tuple(vals[c] for c in resources.MAINT_SHEET.columns))
    if not rows:
        return "维护记录:表与台账已一致,无需补写"
    written = append_records(rows)
    return (f"维护记录补写 {written} 行(台账有、表里没有的流水;"
            f"旧值/新值两列补不回来,PG 里没存)")


def sync_from_ledger(execute: bool = True) -> str | None:
    """输入:是否真跑(feed_poll 透传) → 输出:回写摘要一行(无待回填区间才返 None)。

    ⚠ 只有 append_records 写过行、水位推进过,这里才有区间可扫。
    maintenance 走 PUT 路由的行 H="sync"、J 当场落定,不参与回填。

    feed_poll 反哺器:扫 [unresolved_from, next_row) 区间内 H=真 feedid 且
    J 空/处理中的行,按 ops.feed_items 台账落 J(结果)/K(报错);
    已全落定的前缀推进水位。纯读库,零沃尔玛调用。
    """
    try:
        resources.MAINT_SHEET.require()
    except LookupError as e:
        # 未登记时**说出来**:静默返 None 会让 feed_poll 什么都不打印,
        # 看起来像"回写过了但飞书没变"(所有者 2026-08-09 实遇)
        return f"维护记录:表未登记,跳过回写({e})"
    with db.pg_conn() as conn:
        cur_state = _load_cursor(conn)
    lo, hi = int(cur_state.get("unresolved_from", 2)), int(cur_state.get("next_row", 2))
    if lo >= hi:
        return None
    first, last = _span()
    values = feishu.sheet_values_rows(resources.MAINT_SHEET, first, last,
                                      lo, hi - 1)
    updates, cache, descs = [], {}, {}
    new_lo, prefix_done = lo, True
    stale_cut, n_stale = _today() - timedelta(days=STALE_DAYS), 0
    # 行号取通道给的(分块读,块尾空行会被飞书裁掉 —— 见模块头注)
    for rownum, raw in values:
        cells = ([(str(c).strip() if c is not None else "") for c in raw]
                 + [""] * len(resources.MAINT_SHEET.columns))
        sku = cells[_idx("sku")]
        fid = cells[_idx("feed_id")]
        result = cells[_idx("result")]
        # 超期兜底(所有者定稿 2026-08-09):一行永远悬着会把水位钉死,
        # 每轮 feed_poll 都要重读整段。超 STALE_DAYS 天判「未查到」放行——
        # **状态权威在 ops.feed_items,这里只是展示面板不再等它**。
        row_date = _row_date(cells[_idx("op_date")])
        if (fid and result in _PENDING and row_date
                and row_date < stale_cut):
            updates.append((f"{_col('result')}{rownum}:{_col('error')}{rownum}",
                            [["未查到", f"超 {STALE_DAYS} 天未落定,不再等"]]))
            n_stale += 1
            if prefix_done:
                new_lo = rownum + 1
            continue
        pending = fid and fid != _SYNC_MARK and result in _PENDING
        if not pending:
            if prefix_done:
                new_lo = rownum + 1
            continue
        if fid not in cache:
            cache[fid] = feed_track.item_results(fid)
            descs[fid] = feed_track.item_errors(fid)
        st = cache[fid].get(sku)
        if st is None:
            # 台账查无此 (feed, sku):不该发生(程序是唯一写入方),
            # 告警后视为已处理,不许它永久卡住水位
            logger.warning("维护记录第 %d 行 feed=%s sku=%s 台账查无,跳过",
                           rownum, fid, sku)
            if prefix_done:
                new_lo = rownum + 1
            continue
        if st[0] == "submitted":
            prefix_done = False         # feed 未落定,水位停在这里
            continue
        text = feed_track.text_of(st[0])
        # 报错列写「码 | 人话」(改价/改库存/改标题/清库存共用这一列)
        err = feed_track.merge_error(
            st[1], descs.get(fid, {}).get(sku)) if text == "失败" else ""
        updates.append((f"{_col('result')}{rownum}:{_col('error')}{rownum}",
                        [[text, err]]))
        if prefix_done:
            new_lo = rownum + 1
    tail = f",其中超 {STALE_DAYS} 天判未查到 {n_stale} 行" if n_stale else ""
    if not execute:
        return (f"[DRY-RUN] 维护记录:将回填 {len(updates)} 行(扫描区间 "
                f"{lo}~{hi - 1}),水位将推到 {new_lo}{tail}")
    n = feishu.sheet_write_ranges(resources.MAINT_SHEET, updates) if updates else 0
    if new_lo != lo:
        cur_state["unresolved_from"] = new_lo
        with db.pg_conn() as conn:
            _save_cursor(conn, cur_state)
    if not updates:
        return f"维护记录:未落定 {hi - lo} 行,台账尚无新终态" if hi > lo else None
    return f"维护记录回填 {n} 行(扫描区间 {lo}~{hi - 1}){tail}"


# 表头中文名:registry 定列序,这里只给每列的显示名。**按名字取,不按位置**
# —— 硬编码成一个九元组正是下面 prune 三处错位的根因(2026-08-16 加「建议」
# 「原因」两列时,_col/_idx 改成了从 registry 推导,这一族却被漏下)。
# registry 加列而这里没补名字 ⇒ KeyError 当场炸,不会静默写出一个短表头。
_HEADER_NAMES = {
    "store": "店铺", "sku": "SKU", "suggestion": "建议", "reason": "原因",
    "action": "动作", "old_value": "旧值", "new_value": "新值",
    "feed_id": "feedid", "op_date": "日期", "result": "结果", "error": "报错",
}


def _header() -> tuple:
    """输入:无 → 输出:与 registry 列序一一对应的中文表头。"""
    return tuple(_HEADER_NAMES[c] for c in resources.MAINT_SHEET.columns)


def prune(days: int = RETAIN_DAYS) -> str:
    """输入:保留天数 → 输出:裁剪摘要。飞书只留近 N 天,老行整表重写抹掉。

    所有者定稿 2026-08-09:「维护的 feed 一天几千条记录,要不了多久飞书就很
    难存了……只保存近 7 天」。旧系统是靠"一天一个表格"绕开这个问题的。

    **删的只是展示面板**:全部流水永久在 ops.feed_items / ops.feed_log,
    要查历史查 PG,不查飞书。裁完水位重置(行号整体上移,老水位失效)。
    没有日期的行一律保留(宁可留着也不误删)。
    """
    resources.MAINT_SHEET.require()
    with db.pg_conn() as conn:
        cur_state = _load_cursor(conn)
    hi = int(cur_state.get("next_row", 2))
    if hi <= 2:
        return "维护记录:表内无数据行,无需裁剪"
    first, last = _span()
    values = feishu.sheet_values_rows(resources.MAINT_SHEET, first, last,
                                      2, hi - 1)
    cut = _today() - timedelta(days=days)
    ncol = len(resources.MAINT_SHEET.columns)
    kept = []
    for _rownum, raw in values:
        cells = [(str(c).strip() if c is not None else "") for c in raw] + [""] * ncol
        if not any(cells[:ncol]):
            continue                    # 空行不留
        d = _row_date(cells[_idx("op_date")])
        if d is None or d >= cut:       # 没日期的保留(宁可留着也不误删)
            kept.append(cells[:ncol])
    dropped = (hi - 2) - len(kept)
    if dropped <= 0:
        return f"维护记录:{len(kept)} 行都在近 {days} 天内,无需裁剪"
    feishu.sheet_overwrite(resources.MAINT_SHEET, [list(_header())] + kept)
    # 行号整体上移:水位必须重置,否则反哺器会扫到错行
    with db.pg_conn() as conn:
        _save_cursor(conn, {"next_row": len(kept) + 2, "unresolved_from": 2})
    return (f"维护记录裁剪:删 {dropped} 行(早于 {cut}),留 {len(kept)} 行;"
            f"历史流水在 ops.feed_items 未动")
