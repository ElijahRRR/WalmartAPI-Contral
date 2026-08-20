"""risk_sync — 风控/黑名单中心四表镜像入库(只读飞书,非危险)。

用法:
  python cli.py risk_sync   # 类目表+品牌总表+黑名单卖家+黑名单亚马逊类目 → PG

来源(wiki 承载,api/feishu 自动解析节点 token):
  「沃尔玛类目表」(registry.RISK_PT_SHEET,10 列)→ catalog.risk_product_types
    (upsert,大类目定义用)**以及** audit.walmart_pt_meta(全量重灌,
    审核 R1 准入闸 / R3 认证闸唯一查的字典)。⚠ 两个消费方一份数据:
    2026-08-17 之前只同步了前者,后者是批次 A 的死快照,于是飞书增删对审核
    毫无影响(所有者实遇:「飞书表格里面的已废弃我已经删掉了,但是重新拉
    以后还是存在」)。pt_meta 必须**全量重灌**才能让"删行"生效,upsert 不行
  「黑名单品牌总表」(registry.BRAND_BAN_SHEET)→ catalog.brand_blacklist
    ——各渠道黑名单品牌由所有者人工归拢的总清单(2026-08-11 换新表,
    旧「禁止品牌收集」退役)。方向只有飞书→PG;程序自产品牌的**反向**
    投影走 blacklist_push → BRAND_ERR_SHEET(归拢的增量渠道),别混。
  「黑名单卖家店铺ID」(registry.SELLER_BLACKLIST_SHEET,sheet=B19LKn)
    → catalog.seller_blacklist(审核 Phase0 卖家闸;定稿 2026-08-13)
  「黑名单亚马逊类目」(registry.AMZCAT_BLACKLIST_SHEET,sheet=twjmql)
    → catalog.amazon_cat_blacklist(审核 Phase0 类目闸,入库即归一化)

同步语义分两族:前两表**只增改不删**(upsert,不碰 pushed_at 列);
黑名单中心两张单列表 **全量重灌**(飞书删行必须跟着消失,详见
_sync_column_blacklist);⚠ 类目表的重灌**只洗 source='feishu' 的行** ——
同一张表里还住着按 browse_node_id 拦子树的人工/清洗规则,洗掉就等于
每天把清洗成果还原一次。所有者定稿 2026-08-07:表格随时会停用,
停用后 PG 是唯一权威;上架否决闸(services/risk_gate)与审核四闸
(services/audit_rules)都只读 PG。

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


def _first_row(sheet) -> list[dict]:
    """输入:登记条目 → 输出:A1 行(非表头时)包装成行 dict,表头则 []。

    黑名单单列表的表头是条件式判定(单元格含「黑名单」才算表头)——
    _read_sheet 从 A2 起读,若首行其实是数据,会每天漏拦同一条。
    """
    cells = (feishu.sheet_values(sheet, "A1:A1") or [[]])[0]
    vals = [(str(c).strip() if c is not None else "") for c in cells] + [""]
    if "黑名单" in vals[0]:
        return []
    d = dict(zip(sheet.columns, vals))
    return [d] if any(d.values()) else []


def _sync_column_blacklist(conn, sheet, table: str, rows: list[dict],
                           normalize: bool) -> str:
    """输入:连接 + 登记条目 + 目标表 + 行 → 输出:重灌计数摘要。

    黑名单中心单列表镜像(卖家/亚马逊类目;所有者定稿 2026-08-13)。
    镜像语义 = **TRUNCATE + 全量重灌**(risk_sync 家族的"只增改不删"在此
    不适用:飞书删行必须跟着消失,残留即幽灵拦截)。两道护栏:空读绝不重灌;
    骤缩超 50% 拒绝(接口/配置异常与运营删几行是两回事)。
    类目归一化与审核查询侧共用 audit_phase0.normalize_amazon_category,
    存的就是归一化值,读取端不再二次归一化。
    """
    from services.audit_phase0 import normalize_amazon_category
    col = sheet.columns[0]
    if normalize:
        vals = {}
        for r in rows:
            raw = r.get(col)
            if raw:
                norm = normalize_amazon_category(raw)
                if norm and norm not in vals:
                    vals[norm] = raw
        payload = sorted(vals.items())
    else:
        payload = [(v,) for v in sorted({r[col] for r in rows if r.get(col)})]
    if not payload:
        return (f"⚠ 「{sheet.name}」:本轮读到 0 条(疑似接口/配置异常),"
                f"不重灌,库内旧数据保留生效")
    # ⚠ 重灌**只洗自己灌进去的那批**(source='feishu')。2026-08-20 起同一张
    # 类目表里还住着人工/清洗导入的子树规则(source<>'feishu'),TRUNCATE 会把
    # 它们一起洗掉 —— 每天同步一次就把清洗成果洗没了,而且不报错。
    own = " WHERE source = 'feishu'" if normalize else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}{own}")
        (old_n,) = cur.fetchone()
    if old_n >= 20 and len(payload) < old_n * 0.5:
        raise RuntimeError(f"「{sheet.name}」骤缩 {old_n}→{len(payload)}"
                           f"(超 50%),拒绝重灌——人工核实飞书表后再跑")
    if normalize:
        cols, ph = "(category_norm, category_raw, match_type, source)", "(%s, %s, 'path_exact', 'feishu')"
    else:
        cols, ph = "(seller_id)", "(%s)"
    with conn.cursor() as cur:
        if own:
            cur.execute(f"DELETE FROM {table}{own}")
        else:
            cur.execute(f"TRUNCATE {table}")
        cur.executemany(f"INSERT INTO {table} {cols} VALUES {ph}", payload)
    kept = ""
    if own:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table} WHERE source <> 'feishu'")
            (n_keep,) = cur.fetchone()
        if n_keep:
            kept = f";另有 {n_keep} 条非飞书来源的规则(子树/人工)未受影响"
    return f"「{sheet.name}」:全量重灌 {len(payload)} 条{kept}"


def run(params: dict) -> str:
    """输入:params(无参)→ 输出:两表同步计数与禁售/黑名单摘要。"""
    lines = []
    with db.pg_conn() as conn:
        try:
            pt_rows = _read_sheet(resources.RISK_PT_SHEET.require())
            n_pt = risk_gate.sync_product_types(conn, pt_rows)
            lines.append(f"类目表:读 {len(pt_rows)} 行,入库 {n_pt}")
            # 同一份数据的第二个消费方:审核 R1 准入闸 / R3 认证闸只查
            # audit.walmart_pt_meta。它此前是批次 A 的死快照没人同步,
            # 于是飞书增删对审核毫无影响(所有者 2026-08-17 实遇:
            # 「飞书表格里面的已废弃我已经删掉了,但是重新拉以后还是存在」)
            n_meta, dropped = risk_gate.sync_pt_meta(conn, pt_rows)
            lines.append(
                f"  → 审核准入字典 walmart_pt_meta:全量重灌 {n_meta} 行"
                + (f",**净减 {dropped} 行**(飞书删掉的已废弃 PT 同步生效)"
                   if dropped else ""))
        except LookupError as e:
            lines.append(f"类目表跳过:{e}")
        try:
            brand_rows = _read_sheet(resources.BRAND_BAN_SHEET.require())
            n_b = risk_gate.sync_brands(conn, brand_rows)
            lines.append(f"品牌表:读 {len(brand_rows)} 行,入库 {n_b}")
        except LookupError as e:
            lines.append(f"品牌表跳过:{e}")
        # 黑名单中心两张单列表(卖家/亚马逊类目;所有者定稿 2026-08-13):
        # TRUNCATE 重灌各走独立事务,失败绝不连累其他表已完成的同步
        for sheet_ref, table, norm in (
                (resources.SELLER_BLACKLIST_SHEET,
                 "catalog.seller_blacklist", False),
                (resources.AMZCAT_BLACKLIST_SHEET,
                 "catalog.amazon_cat_blacklist", True)):
            try:
                sheet = sheet_ref.require()
                rows = _first_row(sheet) + _read_sheet(sheet)
                with db.pg_conn() as c2:
                    lines.append(_sync_column_blacklist(c2, sheet, table,
                                                        rows, norm))
            except LookupError as e:
                lines.append(f"「{sheet_ref.name}」跳过:{e}")
            except Exception as e:
                logger.warning("「%s」同步失败(库内旧数据保留生效):%s",
                               sheet_ref.name, e)
                lines.append(f"⚠ 「{sheet_ref.name}」同步失败(旧数据保留):{e}")
        gate = risk_gate.load_gate(conn)
        banned_asins = blacklist.load_banned_asins(conn)
    lines.append(f"闸门现状:禁售类目 {len(gate['banned_pts'])} 个,"
                 f"黑名单品牌 {len(gate['brands'])} 个,"
                 f"ASIN 黑名单 {len(banned_asins)} 个")
    # ASIN 黑名单**不由本工作流同步**(所有者问询 2026-08-12 补可见性):
    # 它是自产回路——问题产品清理每日归类(B/C/E/F/G/K 六类)+ 上架违禁
    # 回执自动入库;PG 权威,blacklist_push 反向推飞书投影。此处只报数
    return "\n".join(lines)
