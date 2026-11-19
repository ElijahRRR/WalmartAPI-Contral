"""retire_push — 把已确认的停用/删除清单追加进飞书「商品停用删除表」A~D 列。**危险:缺省即真跑,空跑用 `--dry-run`。**

用法:
  python cli.py retire_push -p from_csv=retire.csv --dry-run   # 校验 + 预览要追加的行
  python cli.py retire_push -p from_csv=retire.csv             # 真写 A~D 并逐行回读

清单 CSV(UTF-8)表头:`店铺,SKU,动作,原因`。
  · 动作只认 `停用` / `下架` / `删除`(**不许留空**:product_clear 把 C 列留空当
    DELETE_ITEM,程序写入必须把动作写明);
  · SKU 是沃尔玛上的**真实 SKU**(不是 ASIN)。

## 它补的是哪条缝(2026-11-19 标准选品流程)

删除表原先只有运营手填。标准选品流程确认删除名单后,需要一个走 cli.py
(锁 / ops.runs / 通知)的写入口,而不是脚本直接调飞书。写进表 = 交给
15:00 的 `product_clear` 执行 —— **本工作流不提交任何 feed**。

## 整批校验(任一行不过就整批拒绝,一行不写)

1. 店铺在营(`stores.enabled_names()`);
2. SKU 在该店目录里还活着(`catalog.walmart_items` 未缺席);
3. **自动删除链没有同 SKU 的未落定 delete/retire**(`ops.dispositions` suggested/
   executing)—— 两条链各删一次是 conventions §三 记过的事故;
4. 清单内同一 (店铺, SKU) 不重复。

## 去重(跳过,不报错)

删除表里已有同 (店铺, SKU) 且尚未落定的行(G 列空 / 处理中)→ 跳过:重跑自愈,
中途失败后再跑一次只补没写进去的。已落定(成功/失败/未查到)的历史行不拦,
新追加一行,历史行不改。

追加位置 = 表里最后一个有店铺或 SKU 的行 + 1;写完逐行回读 A~D,对不上首行点名。
不释放任何占用(删除不等于释放,释放只有 store_release 一条路)。
"""

import csv
import logging
from pathlib import Path

from registry import db
from services import clear_sheet, stores as stores_svc

DANGEROUS = True

logger = logging.getLogger("workflows.retire_push")

_SQL_LIVE = """
SELECT store, sku FROM catalog.walmart_items
WHERE missing_since IS NULL AND store = ANY(%(stores)s) AND sku = ANY(%(skus)s)
"""

_SQL_OPEN_DISP = """
SELECT store, sku, action, status FROM ops.dispositions
WHERE status IN ('suggested', 'executing') AND action IN ('delete', 'retire')
  AND store = ANY(%(stores)s) AND sku = ANY(%(skus)s)
"""


def read_plan(path) -> list[tuple[str, str, str, str]]:
    """输入:店铺/SKU/动作/原因 CSV → 输出:[(店铺, SKU, 动作, 原因)](按文件顺序)。

    坏行整份拒绝并报行号;Excel 留下的全空行跳过;同一 (店铺, SKU) 重复即拒。
    """
    if path is None or not str(path).strip():
        raise ValueError("-p from_csv= 给了空值(变量没展开?):宁炸不吞")
    need = ("店铺", "SKU", "动作", "原因")
    out, seen = [], set()
    try:
        with Path(str(path).strip()).expanduser().open(encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            heads = {(f or "").strip() for f in (rd.fieldnames or [])}
            if not set(need) <= heads:
                raise ValueError(f"删除清单必须有 {'/'.join(need)} 四列(读到的表头:{rd.fieldnames})")
            for row in rd:
                row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
                vals = tuple(row.get(k, "") for k in need)
                if not any(vals):
                    continue
                store, sku, action, reason = vals
                if not (store and sku and reason):
                    raise ValueError(f"删除清单第 {rd.line_num} 行店铺/SKU/原因不能为空:{vals}")
                if action not in clear_sheet.APPEND_ACTIONS:
                    raise ValueError(f"删除清单第 {rd.line_num} 行动作无效:{action!r}"
                                     f"(只认 {'/'.join(clear_sheet.APPEND_ACTIONS)};"
                                     f"留空在 product_clear 里等于删除,必须写明)")
                if (store, sku) in seen:
                    raise ValueError(f"删除清单第 {rd.line_num} 行重复:{store}/{sku}")
                seen.add((store, sku))
                out.append(vals)
    except UnicodeDecodeError as e:
        raise ValueError("删除清单不是 UTF-8:Excel 请「另存为 → CSV UTF-8(逗号分隔)」") from e
    if not out:
        raise ValueError("删除清单为空")
    return out


def run(params: dict) -> str:
    """输入:params(from_csv/execute)→ 输出:追加摘要。"""
    unknown = set(params) - {"execute", "dry_run", "from_csv"}
    if unknown:
        raise ValueError(f"retire_push 不支持参数:{sorted(unknown)}")
    if "from_csv" not in params:
        raise ValueError("retire_push 必须给 -p from_csv=<店铺,SKU,动作,原因 清单>")
    execute = bool(params.get("execute"))
    plan = read_plan(params.get("from_csv"))

    try:
        live_stores = stores_svc.enabled_names()
    except Exception as e:                          # noqa: BLE001
        return f"⛔ 凭证表读不到({e}):分不清在营店与停用店,拒绝写删除表"

    stores = sorted({r[0] for r in plan})
    skus = sorted({r[1] for r in plan})
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_LIVE, {"stores": stores, "skus": skus})
            live = {(s, k) for s, k in cur.fetchall()}
            cur.execute(_SQL_OPEN_DISP, {"stores": stores, "skus": skus})
            open_disp = {(s, k): (a, st) for s, k, a, st in cur.fetchall()}

    problems = []
    for store, sku, _action, _reason in plan:
        if store not in live_stores:
            problems.append(f"{store}/{sku}:店铺未启用")
        if (store, sku) not in live:
            problems.append(f"{store}/{sku}:目录里没有这个活 SKU(已缺席,或店名/SKU 写错)")
        if (store, sku) in open_disp:
            a, st = open_disp[(store, sku)]
            problems.append(f"{store}/{sku}:自动删除链已有未落定的 {a}({st}),不重复写")
    if problems:
        raise ValueError(f"删除清单 {len(problems)} 处不通过,整批拒绝(未写删除表):\n"
                         + "\n".join(f"  · {x}" for x in problems[:40])
                         + (f"\n  …另有 {len(problems) - 40} 处" if len(problems) > 40 else ""))

    have = clear_sheet.read_rows()
    pending = {(r["store"], r["sku"]) for r in have
               if r["result"] in clear_sheet.POLLABLE}
    todo = [r for r in plan if (r[0], r[1]) not in pending]
    skipped = len(plan) - len(todo)
    start = max((r["rownum"] for r in have), default=1) + 1
    by_action: dict[str, int] = {}
    for r in todo:
        by_action[r[2]] = by_action.get(r[2], 0) + 1
    mix = "、".join(f"{k} {v}" for k, v in sorted(by_action.items())) or "无"
    head = (f"删除清单 {len(plan)} 行 → 待追加 {len(todo)} 行({mix})"
            + (f";表里已有未落定同 SKU {skipped} 行,跳过" if skipped else ""))
    if not todo:
        return f"✓ 没有要追加的行({head})"
    if not execute:
        clear_sheet.append_rows(todo, start, execute=False)
        return (f"🧪 {head},将从第 {start} 行起写 A~D\n"
                f"  ⚠ 写进表 = 交给 15:00 的 product_clear 执行;确认后去掉 --dry-run 重跑")

    n = clear_sheet.append_rows(todo, start, execute=True)
    logger.warning("retire_push 已追加停用/删除表 %d 行,起始行 %d", n, start)
    back = {r["rownum"]: r for r in clear_sheet.read_rows()}
    bad = []
    for i, (store, sku, action, reason) in enumerate(todo):
        got = back.get(start + i)
        if not got or (got["store"], got["sku"], got["action"], got["reason"]) != (
                store, sku, action, reason):
            bad.append(start + i)
    if bad:
        return (f"⚠ 回读不一致 {len(bad)} 行(行号 {bad[:20]}):已写 {n} 行,请人工核对删除表;"
                f"{head}")
    return (f"✅ 已追加 {n} 行到停用/删除表(第 {start} 行起,只写 A~D,回读一致);{head}\n"
            "  下一步:15:00 product_clear 执行;结果由 product_clear / feed_poll 回写 E~H")
