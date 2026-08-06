"""daily_retire — 飞书停用/删除表驱动的商品清理(plan #7,危险,默认 dry-run)。

用法:
  python cli.py daily_retire                 # dry-run:打印将提交什么、将回写什么
  python cli.py daily_retire --execute       # 真跑(提交 feed + 回写表格)
  python cli.py daily_retire -p limit=100    # 单店单日上限覆盖(默认 300,限额表接入前)
  python cli.py daily_retire -p store=A085朱丽霖

驱动表(registry.RETIRE_SHEET,电子表格,列序即契约):
  A=store  B=sku  C=停用/删除  D=操作原因 | E=feedid  F=操作日期  G=结果
  A~D 运营填,E~G 程序写。

行状态机:
  E 空 + C 合法        → 待提交(受单店单日上限约束,超限行留到下一轮)
  E 有 + G 空/处理中    → 轮询 feed 终态,逐 SKU 回写 成功 / 失败:错误码 / 未查到
  G=失败/未查到 的行    → **不自动重试**(高危写操作;运营核对原因后清空 E 列
                          即重新排队——feeds 层 failed 记录允许同载荷重占)

动作映射(2026-08-06 所有者定稿):停用/下架 → RETIRE_ITEM(可恢复);
删除 → DELETE_ITEM(永久,仅自发货)。提交走 api/feeds 唯一通道
(切片/三层防重/反查三态),状态权威在 ops.feed_log,飞书 E~G 只是展示
——旧系统"状态只存飞书三列"的结构性风险(2026-05-07 事故根源)就此消除。

⚠ 切换纪律:本工作流上生产调度前,必须先停旧系统 walmart-daily-retire
cron(每天 15:00),新旧并跑 = 重复删除。
"""

import logging
from datetime import datetime

from api import feeds, feishu
from registry import resources
from services import kpi, stores as stores_svc

DANGEROUS = True

logger = logging.getLogger("workflows.daily_retire")

_ACTIONS = {"停用": "RETIRE_ITEM", "下架": "RETIRE_ITEM", "删除": "DELETE_ITEM"}
_POLLABLE = ("", "处理中")          # G 列这些值才轮询;失败/未查到不自动重试
_COL_WRITE_START = "E"             # 程序回写区 E:G


def _read_rows() -> list[dict]:
    """输入:无 → 输出:表内全部数据行(含 1 基行号,表头在第 1 行)。"""
    sheet = resources.RETIRE_SHEET
    total = feishu.sheet_row_count(sheet)
    if total < 2:
        return []
    values = feishu.sheet_values(sheet, f"A2:G{total}")
    rows = []
    for i, raw in enumerate(values):
        cells = [(str(c).strip() if c is not None else "") for c in raw] + [""] * 7
        store, sku, action, reason, feed_id, op_date, result = cells[:7]
        if not (store or sku):
            continue
        rows.append({"rownum": i + 2, "store": store, "sku": sku,
                     "action": action, "reason": reason,
                     "feed_id": feed_id, "op_date": op_date, "result": result})
    return rows


def _writeback(updates: list[tuple[int, str, str, str]], execute: bool) -> int:
    """输入:[(行号, feedid, 日期, 结果)] → 输出:写入行数(dry-run 只打印)。"""
    if not updates:
        return 0
    if not execute:
        for rownum, fid, dt, res in updates[:20]:
            logger.info("[DRY-RUN] 将回写 第%d行 E=%s F=%s G=%s", rownum, fid, dt, res)
        if len(updates) > 20:
            logger.info("[DRY-RUN] …另有 %d 行回写省略", len(updates) - 20)
        return 0
    return feishu.sheet_write_ranges(
        resources.RETIRE_SHEET,
        [(f"{_COL_WRITE_START}{r}:G{r}", [[fid, dt, res]])
         for r, fid, dt, res in updates])


def _poll_feeds(rows: list[dict], stores_by_name: dict,
                execute: bool) -> tuple[list, list[str]]:
    """轮询已提交行的 feed 终态 → 逐 SKU 回写结果。"""
    updates, lines = [], []
    pollable = [r for r in rows if r["feed_id"] and r["result"] in _POLLABLE]
    by_feed: dict[str, list[dict]] = {}
    for r in pollable:
        by_feed.setdefault(r["feed_id"], []).append(r)

    done = still = 0
    for feed_id, frows in by_feed.items():
        store = stores_by_name.get(frows[0]["store"])
        if store is None:
            continue
        try:
            head = feeds.get_feed_status(store, feed_id)
        except Exception as e:
            logger.warning("feed %s 状态查询失败,本轮跳过: %s", feed_id, e)
            continue
        if head.get("feedStatus") not in feeds.FEED_TERMINAL:
            still += len(frows)
            continue
        sku_status: dict[str, tuple[str, str]] = {}
        for item in feeds.iter_feed_items(store, feed_id):
            code = ""
            errs = item.get("ingestionErrors") or {}
            errlist = errs.get("ingestionError") or []
            if errlist:
                code = str(errlist[0].get("code") or errlist[0].get("type") or "")
            sku_status[str(item.get("sku") or "")] = (
                feeds.sku_outcome(item.get("ingestionStatus")), code)
        ok_all = head.get("feedStatus") == "PROCESSED"
        if execute:
            feeds.mark_feed_done(feed_id, ok_all)
        for r in frows:
            outcome, code = sku_status.get(r["sku"], ("missing", ""))
            result = {"success": "成功", "failed": f"失败:{code}" if code else "失败",
                      "processing": "处理中", "unknown": "处理中",
                      "missing": "未查到"}[outcome]
            if result != r["result"]:
                updates.append((r["rownum"], r["feed_id"], r["op_date"], result))
            done += 1
    if by_feed:
        lines.append(f"轮询:{len(by_feed)} 个 feed,{done} 行落定,{still} 行仍处理中")
    return updates, lines


def _submit_new(rows: list[dict], stores_by_name: dict, limit: int,
                execute: bool) -> tuple[list, list[str]]:
    """待提交行 → 按 店铺×动作 分组提交(受单店单日上限),回写 E~G。"""
    updates, lines = [], []
    today = datetime.now(kpi.CN_TZ).strftime("%Y-%m-%d")
    fresh = [r for r in rows if not r["feed_id"]]

    bad = [r for r in fresh if r["action"] not in _ACTIONS or
           r["store"] not in stores_by_name]
    for r in bad:
        why = "动作不识别" if r["action"] not in _ACTIONS else "店铺不识别"
        if r["result"] != why:
            updates.append((r["rownum"], "", "", why))
    good = [r for r in fresh if r not in bad]

    by_store: dict[str, list[dict]] = {}
    for r in good:
        by_store.setdefault(r["store"], []).append(r)

    submitted = deferred = 0
    for store_name, srows in by_store.items():
        take, defer = srows[:limit], srows[limit:]
        deferred += len(defer)
        by_action: dict[str, list[dict]] = {}
        for r in take:
            by_action.setdefault(_ACTIONS[r["action"]], []).append(r)
        for feed_type, arows in by_action.items():
            skus = [r["sku"] for r in arows]
            if not execute:
                logger.info("[DRY-RUN] %s %s 将提交 %d 个 SKU:%s%s",
                            store_name, feed_type, len(skus), skus[:8],
                            " …" if len(skus) > 8 else "")
                lines.append(f"[DRY-RUN] {store_name} {feed_type} 待提交 {len(skus)}")
                continue
            results = feeds.submit_feed(stores_by_name[store_name], feed_type,
                                        skus, workflow="daily_retire")
            i = 0
            for res in results:
                slice_rows = arows[i:i + res["count"]]
                i += res["count"]
                if res["outcome"] in ("submitted", "dedup") and res["feed_id"]:
                    submitted += len(slice_rows)
                    for r in slice_rows:
                        updates.append((r["rownum"], res["feed_id"], today, "处理中"))
                elif res["outcome"] == "failed":
                    for r in slice_rows:
                        updates.append((r["rownum"], "", "", "提交被拒"))
                else:   # unknown:保持 pending 待启动对账,行不动
                    lines.append(f"⚠ {store_name} {feed_type} 一批 {res['count']} 条"
                                 f"提交结果不确定,已留 pending 待对账")
    if good or bad:
        lines.append(f"提交:有效待提交 {len(good)} 行,本轮"
                     f"{'提交' if execute else '将提交'} {submitted if execute else min(len(good), sum(min(len(v), limit) for v in by_store.values()))} 行,"
                     f"超限延后 {deferred} 行,无效行 {len(bad)}")
    return updates, lines


def run(params: dict) -> str:
    """输入:params(execute/limit/store)→ 输出:轮询+提交结果摘要。"""
    execute = bool(params.get("execute"))
    limit = int(params.get("limit", 300))

    rows = _read_rows()
    if not rows:
        return "停用/删除表无数据行"
    only = params.get("store")
    if only:
        rows = [r for r in rows if r["store"] == only]

    names = sorted({r["store"] for r in rows})
    stores_by_name = {s["name"]: s for s in stores_svc.load_stores(names)}

    updates_a, lines_a = _poll_feeds(rows, stores_by_name, execute)
    updates_b, lines_b = _submit_new(rows, stores_by_name, limit, execute)
    written = _writeback(updates_a + updates_b, execute)

    mode = "" if execute else "🧪 [DRY-RUN] "
    lines = [f"{mode}daily_retire:表内 {len(rows)} 行"] + lines_a + lines_b
    if execute:
        lines.append(f"回写 {written} 行")
    return "\n".join(lines)
