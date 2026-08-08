"""kpi_history_import — 旧「店铺KPI」飞书历史 → ops.store_kpi_daily(一次性)。

用法:
  python cli.py kpi_history_import                 # 预览:表头映射/行数/重叠对比
  python cli.py kpi_history_import -p apply=1      # 真正入库
  python cli.py kpi_history_import -p store=A085朱丽霖   # 单店(sheet 标题)

数据源:旧系统「店铺KPI」workbook(registry.KPI_SHEET),每店一个 sheet
(title=店铺名)按日期一行累积历史;「总览」页跳过。表头按关键词映射
(services.kpi._HIST_HEADER_MAP),不按列位——旧表列序不可考,表头才是契约。

防重语义:INSERT ... ON CONFLICT (store, data_date) DO NOTHING——
**绝不覆盖任何已存在的行**(新系统已写的天数、重复导入,都原样保留)。
apply 收尾会用每店最近非空卖家名称回填全部 NULL 行(导入不碰已有行,
新系统先前写空的名字需要这一步才能补上;可重复执行,幂等)。
预览模式必看两处再 apply:①未映射表头列表(有列名对不上就先校准映射)
②与 PG 重叠日期的样本对比(单量/销售额口径核对)。
"""

import logging

from dataclasses import replace

from api import feishu
from registry import db, resources
from services import kpi

DANGEROUS = False

logger = logging.getLogger("workflows.kpi_history_import")

_INSERT = """
INSERT INTO ops.store_kpi_daily (
    store, data_date, seller_name, partner_id, seller_id, store_status,
    payment_status, sales_status, items_online, items_in_stock, items_out_stock,
    orders_count, sales_amount, otd_rate, cancel_rate, vtr_rate, srr_rate,
    refund_rate, negative_rate, return_rate, inr_rate, period_sales, commission,
    refund_amount, closing_balance, reserve_to_date, payout, payout_date,
    payment_processor, settle_cycle, no_hold, prev_payout)
VALUES (%(store)s, %(data_date)s, %(seller_name)s, %(partner_id)s, %(seller_id)s,
        %(store_status)s, %(payment_status)s, %(sales_status)s, %(items_online)s,
        %(items_in_stock)s, %(items_out_stock)s, %(orders_count)s, %(sales_amount)s,
        %(otd_rate)s, %(cancel_rate)s, %(vtr_rate)s, %(srr_rate)s, %(refund_rate)s,
        %(negative_rate)s, %(return_rate)s, %(inr_rate)s, %(period_sales)s,
        %(commission)s, %(refund_amount)s, %(closing_balance)s, %(reserve_to_date)s,
        %(payout)s, %(payout_date)s, %(payment_processor)s, %(settle_cycle)s,
        %(no_hold)s, %(prev_payout)s)
ON CONFLICT (store, data_date) DO NOTHING
"""

_MAX_COL = "AF"      # 旧表 32 列 A~AF(读宽一点无害,窄了丢列)

# 收尾回填:导入不覆盖已有行(DO NOTHING),新系统先前写下的空白卖家名称
# 不会被历史行填上——用每店最近一次非空名称补全部 NULL 行。卖家名称是
# 稳定资产,补历史无害;销售状态绝不做同款回填(旧事故规则)。
_BACKFILL_NAMES = """
UPDATE ops.store_kpi_daily t
SET seller_name = s.seller_name, updated_at = now()
FROM (SELECT DISTINCT ON (store) store, seller_name
      FROM ops.store_kpi_daily
      WHERE seller_name IS NOT NULL
      ORDER BY store, data_date DESC) s
WHERE t.store = s.store AND t.seller_name IS NULL
"""


def _existing_dates(store: str) -> set[str]:
    """输入:店铺 → 输出:PG 已有的 data_date 集合(ISO 字符串)。"""
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT data_date FROM ops.store_kpi_daily WHERE store=%s",
                    (store,))
        return {str(r[0]) for r in cur.fetchall()}


def run(params: dict) -> str:
    """输入:params(apply/store)→ 输出:导入/预览摘要。"""
    apply = str(params.get("apply", "")) in ("1", "true", "yes")
    only = str(params.get("store", "")).strip()

    book = resources.KPI_SHEET.require()
    sheets = [(sid, title) for sid, title in feishu.sheet_list(book)
              if title != "总览" and (not only or title == only)]
    if not sheets:
        return f"未找到店铺 sheet(store={only or '(全部)'})——确认 FEISHU_KPI_SHEET_TOKEN"

    lines = [f"{'导入' if apply else '预览(未入库,-p apply=1 真跑)'}:"
             f"共 {len(sheets)} 个店铺 sheet"]
    all_unmapped: set[str] = set()
    total_parsed = total_skipped = total_new = total_dup = 0
    overlap_samples: list[str] = []

    for sid, title in sheets:
        page = replace(book, sheet_id=sid)
        n_rows = feishu.sheet_row_count(page)
        grid = feishu.sheet_values(page, f"A1:{_MAX_COL}{max(n_rows, 2)}")
        if not grid:
            continue
        header, data_rows = grid[0], grid[1:]
        _, unmapped = kpi.map_history_header(header)
        all_unmapped.update(unmapped)
        rows, skipped = kpi.parse_history_rows(title, header, data_rows)
        have = _existing_dates(title)
        dup = sum(1 for r in rows if r["data_date"] in have)
        total_parsed += len(rows)
        total_skipped += skipped
        total_dup += dup

        # 重叠日期样本:口径核对(单量/销售额),全局最多 5 条
        if len(overlap_samples) < 5:
            for r in rows:
                if r["data_date"] in have:
                    with db.pg_conn() as conn, conn.cursor() as cur:
                        cur.execute(
                            "SELECT orders_count, sales_amount FROM ops.store_kpi_daily"
                            " WHERE store=%s AND data_date=%s",
                            (title, r["data_date"]))
                        pg = cur.fetchone()
                    overlap_samples.append(
                        f"  {title} {r['data_date']}:旧表 {r['orders_count']} 单/"
                        f"{r['sales_amount']} vs PG {pg[0]} 单/{pg[1]}")
                    break

        if apply:
            inserted = 0
            with db.pg_conn() as conn, conn.cursor() as cur:
                for r in rows:
                    cur.execute(_INSERT, r)
                    inserted += cur.rowcount
            total_new += inserted
            logger.info("%s:解析 %d 行,入库 %d(已存在跳过 %d,无日期 %d)",
                        title, len(rows), inserted, len(rows) - inserted, skipped)
        else:
            logger.info("%s:可导入 %d 行(与 PG 重叠 %d,无日期跳过 %d)",
                        title, len(rows), dup, skipped)

    lines.append(f"解析 {total_parsed} 行(无日期跳过 {total_skipped},"
                 f"与 PG 重叠 {total_dup} 行不覆盖)")
    if apply:
        lines.append(f"实际入库 {total_new} 行(ON CONFLICT DO NOTHING)")
        with db.pg_conn() as conn, conn.cursor() as cur:
            cur.execute(_BACKFILL_NAMES)
            lines.append(f"空白卖家名称回填 {cur.rowcount} 行"
                         f"(取每店最近非空值;销售状态不回填)")
    if all_unmapped:
        lines.append(f"⚠ 未映射表头 {len(all_unmapped)} 个(这些列不导入,"
                     f"需要就先校准映射):{' | '.join(sorted(all_unmapped))}")
    if overlap_samples:
        lines.append("重叠日期口径对比样本:")
        lines.extend(overlap_samples)
    return "\n".join(lines)
