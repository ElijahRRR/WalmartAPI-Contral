"""维护记录工作表(registry.MAINT_SHEET)读写积木(maintenance 与 feed_poll 共用)。

列契约(A~K 共 11 列,**列序即契约,唯一权威是 registry.MAINT_SHEET.columns**;
所有者建表 2026-08-07,2026-08-16 加「建议」「原因」两列):
  A=店铺 B=SKU C=建议 D=原因 E=动作 F=旧值 G=新值 H=feedid I=日期 J=结果 K=报错
⚠ 代码里一律 `_col(名)` / `_idx(名)` 取位置,**任何地方都不许写死字母或下标** ——
  加那两列时漏改一处的表现是整表错位且不报错(prune 就漏了,读「新值」当日期)。

三段式写入(所有者定稿 2026-08-17:「执行 maintenance_scan 是为了预览实际的
逻辑有无问题,不展现出来我就只能看到总览,无法判断提交前的真实情况」):

  maintenance_scan  A~D + I   append 半行  (店铺/SKU/建议/原因/日期)
  maintenance       E~H       就地补齐     (动作/旧值/新值/feedid)
  feed_poll         J~K       反哺         (结果/报错)

  所以本表**不再是纯追加的流水账**:扫描件先把「该拿这个商品怎么办」摆到人眼
  前,执行件回来补齐它真做了什么。「建议」与「动作」分歧才是这两列的价值 ——
  建议删除而动作为空 = 领到了没执行(撞上单店上限/配额切片外),结果列说明白。
  连接键是 (店铺, SKU, 建议),与 ops.dispositions 的部分唯一索引同一口径;
  「建议」的中文标签唯一出处在 `services.maintenance_intents.KIND_LABEL`。
  PUT 同步路径(F="sync")当场就知道结果,J/K 由执行件一并落 —— 它不进 feed
  台账,反哺器永远不会来管它。
  四类动作(标题/价格/库存/删除)共用本表,不另建表(所有者问 2026-08-09
  「删除以后跑 feed 会填写到维护记录里吗」)。

水位(ops.cursors,name='maint_sheet'):{"next_row": 下一空行, "unresolved_from":
最早未落定行}。反哺器只扫 [unresolved_from, next_row) 区间,不做全表读。
⚠ 反哺器会**跨过没有 feedid 的行**(它认为那些没有待办)——而扫描件写的半行
正是这种。所以 `fill_submitted` 补完 feedid 必须把 `unresolved_from` 拉回到本次
补过的最小行号,否则反哺器再也不回头看,结果/报错永远空着且不报错。
查重与定位(`_open_index`)因此走**整表读**而非水位窗口:窗口早已跨过那些半行。

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
from services import feed_track, kpi

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


def _span() -> str:
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
    """输入:G 列日期串 → 输出:date(解析不了返 None,当作"没日期不裁不判")。"""
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


def _find_next_empty(start: int) -> int:
    """输入:候选起始行 → 输出:确认为空的首行(防水位漂移覆盖已有数据)。"""
    grid = feishu.sheet_row_count(resources.MAINT_SHEET)
    row = start
    while row <= grid:
        end = min(row + 49, grid)
        vals = feishu.sheet_values(resources.MAINT_SHEET, f"A{row}:A{end}")
        got = [(str(c[0]).strip() if c and c[0] is not None else "")
               for c in (vals + [[None]] * (end - row + 1))[:end - row + 1]]
        for i, v in enumerate(got):
            if not v:
                return row + i
        row = end + 1
    return row      # 网格已满,append_records 会先扩行


def append_records(rows: list[tuple]) -> int:
    """输入:[(店铺, sku, 动作, 旧值, 新值, feedid, 日期, 结果, 报错)]
    → 输出:写入行数。只追加;水位存 ops.cursors,起点先验空防覆盖。"""
    if not rows:
        return 0
    sheet = resources.MAINT_SHEET.require()
    with db.pg_conn() as conn:
        cur_state = _load_cursor(conn)
    start = _find_next_empty(int(cur_state.get("next_row", 2)))
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


def _all_rows() -> list[list[str]]:
    """输入:无 → 输出:表内**全部数据行**(每行补齐到 ncol),行号从 2 起。

    为什么读整表而不是水位窗口:水位 `unresolved_from` 会跨过"没有 feedid"
    的行(反哺器认为它们没有待办)——而扫描件写的半行正是没有 feedid 的。
    拿窗口去查重就会看不见它们,于是每扫一次追加一遍,飞书行数按扫描次数翻。
    prune / resync_from_ledger 一直是这么读整表的,不是新增的开销量级。
    """
    with db.pg_conn() as conn:
        hi = int(_load_cursor(conn).get("next_row", 2))
    if hi <= 2:
        return []
    ncol = len(resources.MAINT_SHEET.columns)
    out = []
    for raw in feishu.sheet_values(resources.MAINT_SHEET,
                                   f"A2:{_span()[1]}{hi - 1}"):
        out.append([(str(c).strip() if c is not None else "") for c in raw]
                   + [""] * ncol)
    return out


def _open_index() -> dict:
    """输入:无 → 输出:{(店铺, SKU, 建议): 行号} —— 只含**动作列still空**的行。

    「动作为空」= 扫描件建议了、执行件还没碰过它。只认这种行有两个理由:
      · 执行件补齐时不该改到一条已经提交过的旧流水;
      · 同一 (店铺,SKU,建议) 明天再次被建议时,应当另起一行,而不是把
        昨天那条已执行的记录覆盖掉。
    同键有多行未填时取**行号最小**的那条(先建议的先补)。
    """
    idx: dict = {}
    for i, cells in enumerate(_all_rows()):
        if cells[_idx("action")]:
            continue
        key = (cells[_idx("store")], cells[_idx("sku")],
               cells[_idx("suggestion")])
        idx.setdefault(key, 2 + i)
    return idx


def append_suggestions(rows: list[tuple]) -> tuple[int, int]:
    """输入:[(店铺, SKU, 建议, 原因, 日期)] → 输出:(写入行数, 查重跳过行数)。

    扫描件(maintenance_scan)用:先把「该拿这个商品怎么办」摆到人眼前,
    执行件再回来补齐动作/旧值/新值/feedid。**这五列里「日期」是承重的** ——
    prune 靠它把行老化掉;不写日期的话,那些建议了却一直没被执行的行
    (撞上单店删除上限、或配额切片外的)永远裁不掉,表只涨不减。

    可重复跑:同 (店铺, SKU, 建议) 已有未填行就跳过,不重复追加。
    """
    if not rows:
        return 0, 0
    resources.MAINT_SHEET.require()
    have = set(_open_index())
    fresh, skipped = [], 0
    for store, sku, suggestion, reason, op_date in rows:
        if (str(store), str(sku), str(suggestion)) in have:
            skipped += 1
            continue
        have.add((str(store), str(sku), str(suggestion)))
        vals = {"store": store, "sku": sku, "suggestion": suggestion,
                "reason": reason, "op_date": op_date, "action": "",
                "old_value": "", "new_value": "", "feed_id": "",
                "result": "", "error": ""}
        fresh.append(tuple(vals[c] for c in resources.MAINT_SHEET.columns))
    return append_records(fresh), skipped


# 执行件回填的列。⚠ **两段,不是一段**:「日期」(op_date,扫描件写的)正夹在
# feedid 与 结果 之间。当成一段连续区间写下去,6 个值会摊进 7 个格子 ——
# 整体错位,而且把扫描件写的日期覆盖成「结果」。日期一没,prune 就再也裁不掉
# 这一行(它按日期判年龄),两边都不报错。
_FILL_MAIN = ("action", "old_value", "new_value", "feed_id")   # E:H
_FILL_DONE = ("result", "error")                               # J:K,仅 PUT 同步路径


def fill_submitted(rows: list[tuple]) -> tuple[int, int]:
    """输入:执行件造的整行(11 列)→ 输出:(就地补齐行数, 找不到而追加的行数)。

    按 (店铺, SKU, 建议) 找到扫描件写的那一行,只写 `_FILL_COLS` 这几列;
    找不到就整行追加(扫描件没跑过、或那行已被 prune 裁掉——流水不能丢)。

    ⚠ **补完要把水位拉回去**:`sync_from_ledger` 会跨过没有 feedid 的行
    (扫描件写的半行正是这种),等执行件把 feedid 填进去时,`unresolved_from`
    早已越过它 —— 反哺器再也不回头看,结果/报错两列就永远空着,而且不报错。
    所以这里把水位拉到本次补过的最小行号。
    """
    if not rows:
        return 0, 0
    resources.MAINT_SHEET.require()
    idx = _open_index()
    ranges, appended, touched = [], [], []
    for row in rows:
        cells = dict(zip(resources.MAINT_SHEET.columns, row))
        key = (str(cells["store"]), str(cells["sku"]),
               str(cells["suggestion"]))
        rownum = idx.pop(key, None)
        if rownum is None:
            appended.append(row)
            continue
        touched.append(rownum)

        def _seg(cols):
            return [[str(cells[c]) if cells[c] is not None else "" for c in cols]]

        ranges.append((f"{_col(_FILL_MAIN[0])}{rownum}:"
                       f"{_col(_FILL_MAIN[-1])}{rownum}", _seg(_FILL_MAIN)))
        # 结果/报错归反哺器;只有 PUT 同步路径当场就知道结果(feedid="sync",
        # 不进 feed 台账,反哺器永远不会来管它),那一档在这里落定
        if str(cells.get("result") or ""):
            ranges.append((f"{_col(_FILL_DONE[0])}{rownum}:"
                           f"{_col(_FILL_DONE[-1])}{rownum}", _seg(_FILL_DONE)))
    for i in range(0, len(ranges), _APPEND_BLOCK):
        feishu.sheet_write_ranges(resources.MAINT_SHEET, ranges[i:i + _APPEND_BLOCK])
    if touched:
        with db.pg_conn() as conn:
            st = _load_cursor(conn)
            lo = min(touched)
            if lo < int(st.get("unresolved_from", 2)):
                st["unresolved_from"] = lo
                _save_cursor(conn, st)
    n_app = append_records(appended) if appended else 0
    return len(touched), n_app


_LABEL_BY_FEED = {"MP_MAINTENANCE": "标题", "price": "价格",
                  "inventory": "库存", "DELETE_ITEM": "删除"}
_RESULT_BY_STATUS = {"success": "成功", "failed": "失败",
                     "missing": "未查到", "submitted": "处理中"}

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
        for raw in feishu.sheet_values(resources.MAINT_SHEET, f"A2:{_span()[1]}{hi - 1}"):
            cells = ([(str(c).strip() if c is not None else "") for c in raw]
                     + [""] * len(resources.MAINT_SHEET.columns))
            # 按列名取下标(2026-08-16 加了「建议」「原因」两列;写死 5/1 的话
            # 存量识别会拿错列 ⇒ 每轮把全部行当"表里没有"重复补写)
            have.add((cells[_idx("feed_id")], cells[_idx("sku")]))
    rows = []
    for store, sku, ftype, fid, status, code, desc, submitted in ledger:
        if (str(fid or ""), str(sku)) in have:
            continue
        result = _RESULT_BY_STATUS.get(status, status)
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


def sync_from_ledger() -> str | None:
    """输入:无 → 输出:回写摘要一行(无待回填区间才返 None)。

    ⚠ 只有 append_records 写过行、水位推进过,这里才有区间可扫。
    maintenance 走 PUT 路由的行 F="sync"、H 当场落定,不参与回填。

    feed_poll 反哺器:扫 [unresolved_from, next_row) 区间内 F=真 feedid 且
    H 空/处理中的行,按 ops.feed_items 台账落 H(结果)/I(报错);
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
    values = feishu.sheet_values(resources.MAINT_SHEET,
                                 f"A{lo}:{_span()[1]}{hi - 1}")
    updates, cache, descs = [], {}, {}
    new_lo, prefix_done = lo, True
    stale_cut, n_stale = _today() - timedelta(days=STALE_DAYS), 0
    for i, raw in enumerate(values):
        cells = ([(str(c).strip() if c is not None else "") for c in raw]
                 + [""] * len(resources.MAINT_SHEET.columns))
        sku = cells[_idx("sku")]
        fid = cells[_idx("feed_id")]
        result = cells[_idx("result")]
        rownum = lo + i
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
        text = {"success": "成功", "failed": "失败",
                "missing": "未查到"}.get(st[0], "处理中")
        # 报错列写「码 | 人话」(改价/改库存/改标题/清库存共用这一列)
        err = feed_track.merge_error(
            st[1], descs.get(fid, {}).get(sku)) if text == "失败" else ""
        updates.append((f"{_col('result')}{rownum}:{_col('error')}{rownum}",
                        [[text, err]]))
        if prefix_done:
            new_lo = rownum + 1
    n = feishu.sheet_write_ranges(resources.MAINT_SHEET, updates) if updates else 0
    if new_lo != lo:
        cur_state["unresolved_from"] = new_lo
        with db.pg_conn() as conn:
            _save_cursor(conn, cur_state)
    tail = f",其中超 {STALE_DAYS} 天判未查到 {n_stale} 行" if n_stale else ""
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
    values = feishu.sheet_values(resources.MAINT_SHEET, f"A2:{_span()[1]}{hi - 1}")
    cut = _today() - timedelta(days=days)
    ncol = len(resources.MAINT_SHEET.columns)
    kept = []
    for raw in values:
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
