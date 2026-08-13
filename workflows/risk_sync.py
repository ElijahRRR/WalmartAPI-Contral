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
from services import blacklist, risk_gate

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


def _phase0_first_row(sheet) -> list[dict]:
    """输入:登记条目 → 输出:A1 行(非表头时)包装成行 dict,表头则 []。

    旧仓对这张表的表头是条件式判定(任一单元格含「黑名单」才算表头)——
    _read_sheet 从 A2 起读,若首行其实是数据,会每天漏拦同一条(评审 I-7)。
    """
    cells = (feishu.sheet_values(sheet, "A1:C1") or [[]])[0]
    vals = [(str(c).strip() if c is not None else "") for c in cells] + ["", "", ""]
    if any("黑名单" in v for v in vals[:3]):
        return []
    d = dict(zip(sheet.columns, vals))
    return [d] if any(d.values()) else []


def _sync_phase0(conn, rows: list[dict]) -> str:
    """输入:连接 + Phase0 表行 → 输出:三表重灌计数摘要(审核批次 B5)。

    「Amazon 选品黑名单」是**三条独立列**(A=卖家ID B=ASIN C=Amazon 类目),
    不是行级记录——同一行的三个格子互不相关,逐列收集非空值。
    镜像语义 = **TRUNCATE + 全量重灌**(与旧审核系统同款;risk_sync 家族的
    "只增改不删"在此不适用:飞书删行必须跟着消失,残留即幽灵拦截)。
    与本工作流其余同步共享同一事务:中途失败整体回滚,旧数据继续生效
    (fail-soft,与旧仓 sync_phase0_blacklist 单事务语义一致)。
    类目归一化与查询侧共用 audit_phase0.normalize_amazon_category,
    存的就是归一化值,审核读取端不再二次归一化。
    """
    from services.audit_phase0 import normalize_amazon_category
    sellers = {r["seller_id"] for r in rows if r.get("seller_id")}
    asins = {r["asin"] for r in rows if r.get("asin")}
    cats = {}
    for r in rows:
        raw = r.get("category")
        if raw:
            norm = normalize_amazon_category(raw)
            if norm and norm not in cats:
                cats[norm] = raw
    # 两道护栏(评审 P0-2:空读/骤缩即重灌 = 3.3 万行黑名单静默蒸发,当轮
    # 审核全放行)。空读绝不重灌;骤缩(>50%)大概率是接口/配置异常而非运营
    # 真删了半张表——删几行和腰斩是两回事,与"飞书删行必须跟着消失"不冲突。
    if not (sellers or asins or cats):
        return ("⚠ Phase0 三列表:本轮读到 0 条(疑似接口/配置异常),"
                "不重灌,库内旧数据保留生效")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM audit.phase0_blacklist_sellers),"
            "       (SELECT count(*) FROM audit.phase0_blacklist_asins),"
            "       (SELECT count(*) FROM audit.phase0_blacklist_amazon_cats)")
        old_s, old_a, old_c = cur.fetchone()
    for name, new_n, old_n in (("卖家", len(sellers), old_s),
                               ("ASIN", len(asins), old_a),
                               ("类目", len(cats), old_c)):
        if old_n >= 20 and new_n < old_n * 0.5:
            raise RuntimeError(
                f"Phase0 {name}列骤缩 {old_n}→{new_n}(超 50%),拒绝重灌"
                f"——人工核实飞书表后再跑")
    with conn.cursor() as cur:
        cur.execute("TRUNCATE audit.phase0_blacklist_sellers")
        cur.execute("TRUNCATE audit.phase0_blacklist_asins")
        cur.execute("TRUNCATE audit.phase0_blacklist_amazon_cats")
        cur.executemany("INSERT INTO audit.phase0_blacklist_sellers (seller_id) "
                        "VALUES (%s)", [(s,) for s in sorted(sellers)])
        cur.executemany("INSERT INTO audit.phase0_blacklist_asins (asin) "
                        "VALUES (%s)", [(a,) for a in sorted(asins)])
        cur.executemany("INSERT INTO audit.phase0_blacklist_amazon_cats "
                        "(category_norm, category_raw) VALUES (%s, %s)",
                        sorted(cats.items()))
    return (f"Phase0 三列表:全量重灌 卖家 {len(sellers)} / ASIN {len(asins)} "
            f"/ 类目 {len(cats)}")


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
        try:
            p0_sheet = resources.PHASE0_BLACKLIST_SHEET.require()
            p0_rows = _phase0_first_row(p0_sheet) + _read_sheet(p0_sheet)
            # 独立事务:本表是 TRUNCATE 重灌,失败绝不能连累前两表已完成的同步
            with db.pg_conn() as c2:
                lines.append(_sync_phase0(c2, p0_rows))
        except LookupError as e:
            lines.append(f"Phase0 三列表跳过:{e}")
        except Exception as e:
            logger.warning("Phase0 三列表同步失败(库内旧数据保留生效):%s", e)
            lines.append(f"⚠ Phase0 三列表同步失败(旧数据保留):{e}")
        gate = risk_gate.load_gate(conn)
        banned_asins = blacklist.load_banned_asins(conn)
    lines.append(f"闸门现状:禁售类目 {len(gate['banned_pts'])} 个,"
                 f"黑名单品牌 {len(gate['brands'])} 个,"
                 f"ASIN 黑名单 {len(banned_asins)} 个")
    # ASIN 黑名单**不由本工作流同步**(所有者问询 2026-08-12 补可见性):
    # 它是自产回路——问题产品清理每日归类(B/C/E/F/G/K 六类)+ 上架违禁
    # 回执自动入库;PG 权威,blacklist_push 反向推飞书投影。此处只报数
    return "\n".join(lines)
