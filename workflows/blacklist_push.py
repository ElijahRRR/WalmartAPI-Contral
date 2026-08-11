"""blacklist_push — PG 黑名单自产行 → 飞书两张收集表(投影,只追加)。

用法:
  python cli.py blacklist_push              # 推全部待推行(pushed_at IS NULL)
  python cli.py blacklist_push -p limit=200 # 单轮上限(默认 500,防首推撑爆)

方向只有一个:PG → 飞书(PG 权威,表是人机界面;所有者建表时明说
"用于数据库的映射")。**人不直接编辑这两张表**,人工登记走旧「禁止品牌
收集」那张(risk_sync 镜像回 PG),两条通道不交叉。

推送范围:
  catalog.asin_blacklist   pushed_at IS NULL 的全部行(全是自产)
  catalog.brand_blacklist  pushed_at IS NULL **且 src_sku IS NOT NULL**——
                           src_sku 是自产行的指纹;risk_sync 镜像来的行
                           本来就在飞书上(人工登记的),回推一遍等于把
                           别的表的内容复制进收集表,还会随镜像反复重推

水位语义:每块写成功后当场把该块行的 pushed_at 打上。写表与打水位之间
崩掉的话,那一块下次会**重推(表里出现重复行)而不是丢行**——收集表是
展示面板,重复行人眼可辨可删;丢行则没人会发现。方向选择与 maint_sheet
反哺器同一权衡。

追加机制照抄 maint_sheet 趟过的坑:先读列 A 找真实的下一空行(飞书 +append
遇日期富文本行会误判,legacy_survey:1360 旧系统就中过),再 ensure_rows,
再分块写。
"""

import logging

from api import feishu
from registry import db, resources
from services import blacklist

DANGEROUS = False

logger = logging.getLogger("workflows.blacklist_push")

_BLOCK = 100          # 单块行数(飞书单次写入的稳妥上限,maint_sheet 同款量级)

_ASIN_PENDING = """
SELECT asin, source, created_at::date::text
FROM catalog.asin_blacklist WHERE pushed_at IS NULL
ORDER BY created_at LIMIT %s
"""

_ASIN_MARK = """
UPDATE catalog.asin_blacklist SET pushed_at = now() WHERE asin = ANY(%s)
"""

_BRAND_PENDING = """
SELECT brand_key, brand, source, added_date, src_sku
FROM catalog.brand_blacklist
WHERE pushed_at IS NULL AND src_sku IS NOT NULL
ORDER BY synced_at LIMIT %s
"""

_BRAND_MARK = """
UPDATE catalog.brand_blacklist SET pushed_at = now() WHERE brand_key = ANY(%s)
"""


def _next_empty(sheet, start: int = 2) -> int:
    """输入:登记条目 + 起扫行 → 输出:列 A 首个空行行号(1 行是表头)。"""
    grid = feishu.sheet_row_count(sheet)
    row = start
    while row <= grid:
        end = min(row + 199, grid)
        vals = feishu.sheet_values(sheet, f"A{row}:A{end}")
        got = [(str(c[0]).strip() if c and c[0] is not None else "")
               for c in (vals + [[None]] * (end - row + 1))[:end - row + 1]]
        for i, v in enumerate(got):
            if not v:
                return row + i
        row = end + 1
    return row


def _append(sheet, rows: list[list], mark, keys: list) -> tuple[int, int]:
    """输入:登记条目 + 值行 + 打水位回调(mark(块内 keys))
    → 输出:(写入行数, 块数)。每块成功即打水位(见模块头的方向权衡)。"""
    if not rows:
        return 0, 0
    s = sheet.require()
    start = _next_empty(sheet)
    feishu.sheet_ensure_rows(s, start + len(rows))
    width = chr(ord("A") + len(rows[0]) - 1)
    written = blocks = 0
    for i in range(0, len(rows), _BLOCK):
        block = rows[i:i + _BLOCK]
        row0 = start + written
        feishu.sheet_write_ranges(sheet, [
            (f"A{row0}:{width}{row0 + len(block) - 1}", block)])
        mark(keys[i:i + len(block)])
        written += len(block)
        blocks += 1
    return written, blocks


def run(params: dict) -> str:
    """输入:params(limit/backfill/apply)→ 输出:推送摘要。

    -p backfill=1:从历史时间线按「每 ASIN 最新类别」推导入选(预览计数);
    加 -p apply=1 真写,写完顺路照常投影。一次性动作,重复跑无害
    (DO NOTHING/防重台账都幂等)。
    """
    limit = int(params.get("limit", 500))
    lines = []

    if str(params.get("backfill", "")).lower() in {"1", "true", "yes"}:
        do_apply = str(params.get("apply", "")).lower() in {"1", "true", "yes"}
        with db.pg_conn() as conn:
            c = blacklist.backfill_counts(conn)
            if not do_apply:
                return (f"历史回填预览:时间线共 {c['total']} 个 ASIN,"
                        f"最新类别属永久禁止 {c['permanent']} 个(将入 ASIN 黑名单),"
                        f"C/E 品牌候选 {c['brand_cand']} 个"
                        f"(已处理/缺品牌的落库时再分流);"
                        f"加 -p apply=1 真写并顺路投影")
            st = blacklist.backfill_from_events(conn)
        lines.append(f"历史回填:ASIN +{st['asin_new']},品牌 +{st['brand_new']}"
                     f"(已知 {st['brand_known']},缺品牌 {st['no_brand']},"
                     f"已处理跳过 {st['skipped']})")

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_ASIN_PENDING, (limit,))
            asin_rows = cur.fetchall()
            cur.execute(_BRAND_PENDING, (limit,))
            brand_rows = cur.fetchall()

    if not asin_rows and not brand_rows:
        return "黑名单投影:无待推行"

    def _mark(sql):
        def do(keys):
            # 每块单独开连接提交:水位必须在写表成功后**立即**持久,
            # 攒到最后一起提交的话,中途崩 = 全部块下次重推
            with db.pg_conn() as conn:
                conn.execute(sql, (list(keys),))
        return do

    if asin_rows:
        n, blocks = _append(
            resources.ASIN_BLACKLIST_SHEET,
            [[a, src, day] for a, src, day in asin_rows],
            _mark(_ASIN_MARK), [a for a, _, _ in asin_rows])
        lines.append(f"ASIN 表 +{n} 行({blocks} 块)")
    if brand_rows:
        n, blocks = _append(
            resources.BRAND_ERR_SHEET,
            [[brand, src, day or "", sku or ""]
             for _, brand, src, day, sku in brand_rows],
            _mark(_BRAND_MARK), [k for k, *_ in brand_rows])
        lines.append(f"品牌表 +{n} 行({blocks} 块)")
    if len(asin_rows) >= limit or len(brand_rows) >= limit:
        lines.append(f"⚠ 达单轮上限 {limit},还有待推行,再跑一次即可")
    return "黑名单投影:" + ",".join(lines)
