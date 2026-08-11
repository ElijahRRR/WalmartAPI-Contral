"""risk_sync — 风控两表镜像入库(listing L2b;只读飞书,非危险)。

用法:
  python cli.py risk_sync         # 类目表 + 禁止品牌表 → PG,报禁售/黑名单计数

来源(wiki 承载,api/feishu 自动解析节点 token):
  「沃尔玛类目表」(registry.RISK_PT_SHEET,10 列)→ catalog.risk_product_types
  「黑名单品牌总表」(registry.BRAND_BAN_SHEET)→ catalog.brand_blacklist
    ——各渠道黑名单品牌由所有者人工归拢的总清单(2026-08-11 换新表,
    旧「禁止品牌收集」退役)。方向只有飞书→PG;程序自产品牌的**反向**
    投影走 blacklist_push → BRAND_ERR_SHEET(归拢的增量渠道),别混。

同步语义:**只增改不删**(upsert,不碰 pushed_at 列)。所有者定稿
2026-08-07:表格随时会停用,停用后 PG 是唯一权威;上架主链的提交前
否决闸(services/risk_gate)只读 PG。

调度建议:每日一次(上架主链跑前);表格停用后本工作流随之停用。
"""

import logging

from api import feishu
from registry import db, resources
from services import risk_gate

DANGEROUS = False

logger = logging.getLogger("workflows.risk_sync")


def _read_sheet(sheet) -> list[dict]:
    """输入:登记条目 → 输出:按 columns 命名的行 dict 列表(跳过全空行)。"""
    total = feishu.sheet_row_count(sheet)
    if total < 2:
        return []
    last_col = feishu._col_letter(len(sheet.columns))
    values = feishu.sheet_values(sheet, f"A2:{last_col}{total}")
    rows = []
    for raw in values:
        cells = [(str(c).strip() if c is not None else "") for c in raw] \
            + [""] * len(sheet.columns)
        d = dict(zip(sheet.columns, cells))
        if any(d.values()):
            rows.append(d)
    return rows


def run(params: dict) -> str:
    """输入:params(无参)→ 输出:两表同步计数与禁售/黑名单摘要。"""
    lines = []
    with db.pg_conn() as conn:
        try:
            pt_rows = _read_sheet(resources.RISK_PT_SHEET.require())
            n_pt = risk_gate.sync_product_types(conn, pt_rows)
            lines.append(f"类目表:读 {len(pt_rows)} 行,入库 {n_pt}")
        except LookupError as e:
            lines.append(f"类目表跳过:{e}")
        try:
            brand_rows = _read_sheet(resources.BRAND_BAN_SHEET.require())
            n_b = risk_gate.sync_brands(conn, brand_rows)
            lines.append(f"品牌表:读 {len(brand_rows)} 行,入库 {n_b}")
        except LookupError as e:
            lines.append(f"品牌表跳过:{e}")
        gate = risk_gate.load_gate(conn)
    lines.append(f"闸门现状:禁售类目 {len(gate['banned_pts'])} 个,"
                 f"黑名单品牌 {len(gate['brands'])} 个")
    return "\n".join(lines)
