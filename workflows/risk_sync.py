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

import collections
import logging

from api import feishu
from registry import db, resources
from services import blacklist, category_blacklist, risk_gate

DANGEROUS = False

logger = logging.getLogger("workflows.risk_sync")


class _Skip(Exception):
    """dry-run 时跳过写入段的内部信号(不外泄,run() 自己吃掉)。"""


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
    # 类目表 2026-08-20 升成五列后表头首格是「类目」,不含「黑名单」——
    # 只认旧条件的话表头会被当成一条数据(它的「匹配方式」列是空的,
    # 会被按 ID 推断录成一条垃圾规则)
    if "黑名单" in vals[0] or vals[0] in ("类目", "category"):
        return []
    d = dict(zip(sheet.columns, vals))
    return [d] if any(d.values()) else []


def _sync_column_blacklist(conn, sheet, table: str, rows: list[dict],
                           *, allow_shrink: bool = False,
                           dry_run: bool = False) -> str:
    """输入:连接 + 登记条目 + 目标表 + 行 → 输出:重灌计数摘要(**单列表专用**)。

    现在只剩黑名单卖家一张(类目表 2026-08-20 升成五列,走
    `_sync_amzcat_blacklist`)。镜像语义 = TRUNCATE + 全量重灌
    (risk_sync 家族的"只增改不删"在此不适用:飞书删行必须跟着消失,
    残留即幽灵拦截)。两道护栏:空读绝不重灌;骤缩超 50% 拒绝。
    """
    col = sheet.columns[0]
    payload = [(v,) for v in sorted({r[col] for r in rows if r.get(col)})]
    if not payload:
        return (f"⚠ 「{sheet.name}」:本轮读到 0 条(疑似接口/配置异常),"
                f"不重灌,库内旧数据保留生效")
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        (old_n,) = cur.fetchone()
    _guard_shrink(sheet, old_n, len(payload), allow_shrink,
                  f"新数据 {len(payload)} 条;")
    if dry_run:
        return f"[DRY-RUN] 「{sheet.name}」:将全量重灌 {len(payload)} 条,一行未写"
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {table}")
        cur.executemany(f"INSERT INTO {table} (seller_id) VALUES (%s)", payload)
    return f"「{sheet.name}」:全量重灌 {len(payload)} 条"


def _guard_shrink(sheet, old_n: int, new_n: int, allow_shrink: bool,
                  detail: str = "") -> None:
    """输入:旧行数/新行数/放行开关(+ 新数据构成)→ 输出:无(骤缩未放行则抛错)。

    接口异常与运营删几行是两回事;但**有意的大改**也确实存在(2026-08-20
    类目表从 11,810 条精确路径换成 223 条子树规则,缩 98%)。所以护栏保留,
    只多一把要人显式敲的钥匙:`-p allow_shrink=1`。

    ⚠ detail 必须报出**将要装进去的是什么**(2026-08-20 生产实见:护栏只说
    "11810→223",人被要求"人工核实"却无从核起——他要核的恰恰是那 223 条里
    子树/顶级/路径各多少,列错位会让三个数全变样)。
    """
    if old_n >= 20 and new_n < old_n * 0.5 and not allow_shrink:
        raise RuntimeError(
            f"「{sheet.name}」骤缩 {old_n}→{new_n}(超 50%),拒绝重灌——"
            f"{detail}核对无误后加 -p allow_shrink=1 重跑")


def _sync_amzcat_blacklist(conn, sheet, table: str, rows: list[dict],
                           *, allow_shrink: bool = False,
                           dry_run: bool = False) -> str:
    """输入:连接 + 登记条目 + 目标表 + 五列行 → 输出:重灌计数摘要。

    黑名单亚马逊类目表镜像(所有者定稿 2026-08-20:「我把 233 条整个粘贴进
    飞书表格,你让黑名单中心按实际的读取」)。**飞书是这张表的唯一维护面**,
    所以语义是整表镜像(TRUNCATE + 全量重灌),不再按 source 分家 ——
    分家会让"飞书里删掉的行库里还在拦"这种幽灵长期存在,而所有者要的正是
    "表里有什么、库里就是什么"。⚠ 代价:`category_blacklist_import` 灌进去的
    行会被下一次同步覆盖,那个工作流从此只作首次灌种 / 飞书不可用时应急。

    行解析走 services.category_blacklist.make_rule,与离线 CSV 录入**同一件**:
    子树与否由「匹配方式」列说了算,不按"有 ID 就当子树根"推断(那个口径会
    让回落匹配来的 ID 整棵误拦)。该列为空的行按 ID 退回推断,并在摘要报数。
    """
    c = sheet.columns
    seen: dict[str, dict] = {}
    n = {"读入": 0, "跳过": 0, "按ID推断": 0}
    for r in rows:
        n["读入"] += 1
        rule, note = category_blacklist.make_rule(
            r.get(c[0]) or "", browse_node_id=r.get(c[1]) or "",
            category_zh=r.get(c[2]) or "", match_type=r.get(c[3]) or "",
            reason=r.get(c[4]) or "", source="feishu")
        if not rule:
            n["跳过"] += 1
            continue
        if note:
            n["按ID推断"] += 1
        seen[rule["norm"]] = rule            # 同一路径以最后一行为准
    payload = list(seen.values())
    if not payload:
        return (f"⚠ 「{sheet.name}」:本轮读到 0 条可用规则(疑似接口/配置异常"
                f"或表头列错位),不重灌,库内旧数据保留生效")
    kinds = collections.Counter(r["mt"] for r in payload)
    compo = (f"新数据构成:子树 {kinds[category_blacklist.MATCH_NODE]} / "
             f"顶级名 {kinds[category_blacklist.MATCH_TOP]} / "
             f"路径等值 {kinds[category_blacklist.MATCH_PATH]}"
             f"(读入 {n['读入']} 行,跳过 {n['跳过']}"
             + (f",{n['按ID推断']} 行匹配方式为空" if n["按ID推断"] else "") + ");")
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        (old_n,) = cur.fetchone()
    _guard_shrink(sheet, old_n, len(payload), allow_shrink, compo)
    if dry_run:
        return f"[DRY-RUN] 「{sheet.name}」:将整表重灌 {len(payload)} 条;{compo}一行未写"
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {table}")
        cur.executemany(
            f"INSERT INTO {table} (category_norm, category_raw, match_type,"
            f" match_value, browse_node_id, category_zh, reason, source)"
            f" VALUES (%(norm)s, %(raw)s, %(mt)s, %(mv)s, %(nid)s, %(zh)s,"
            f" %(reason)s, %(source)s)", payload)
    tail = (f";⚠ {n['按ID推断']} 行「匹配方式」列为空,按 ID 推断("
            f"回落匹配来的 ID 会整棵误拦,核一遍)" if n["按ID推断"] else "")
    return (f"「{sheet.name}」:整表重灌 {len(payload)} 条"
            f"(子树 {kinds[category_blacklist.MATCH_NODE]} / "
            f"顶级名 {kinds[category_blacklist.MATCH_TOP]} / "
            f"路径等值 {kinds[category_blacklist.MATCH_PATH]});"
            f"读入 {n['读入']} 行,跳过 {n['跳过']}{tail}")


def run(params: dict) -> str:
    """输入:params(allow_shrink / dry_run)→ 输出:四表同步计数与闸门摘要。

    ⚠ 本工作流 `DANGEROUS=False`,而 cli 对非危险工作流恒给 `execute=True`
    —— `--dry-run` 只体现在 `params["dry_run"]`。2026-08-20 实见:所有者按
    "先 --dry-run 看摘要"敲下去,四张表**照样真写了**(它们都是 TRUNCATE +
    全量重灌)。写的内容与日常调度一致所以没造成损失,但"敲了 dry-run 却在
    写"这件事本身不能留。现在四张表全部认这个开关。
    """
    lines = []
    dry_run = bool(params.get("dry_run"))
    if dry_run:
        lines.append("[DRY-RUN] 只读飞书 + 核对护栏,一行不写")
    with db.pg_conn() as conn:
        try:
            pt_rows = _read_sheet(resources.RISK_PT_SHEET.require())
            if dry_run:
                lines.append(f"类目表:读 {len(pt_rows)} 行,将入库(未写)")
                raise _Skip
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
        except _Skip:
            pass
        except LookupError as e:
            lines.append(f"类目表跳过:{e}")
        try:
            brand_rows = _read_sheet(resources.BRAND_BAN_SHEET.require())
            if dry_run:
                lines.append(f"品牌表:读 {len(brand_rows)} 行,将入库(未写)")
                raise _Skip
            n_b = risk_gate.sync_brands(conn, brand_rows)
            lines.append(f"品牌表:读 {len(brand_rows)} 行,入库 {n_b}")
        except _Skip:
            pass
        except LookupError as e:
            lines.append(f"品牌表跳过:{e}")
        # 黑名单中心两张镜像表(卖家单列 / 亚马逊类目五列;所有者定稿 2026-08-13,
        # 类目表 2026-08-20 升成多列)。各走独立事务,失败绝不连累其他表。
        allow_shrink = str(params.get("allow_shrink", "")).strip() == "1"
        for sheet_ref, table, fn in (
                (resources.SELLER_BLACKLIST_SHEET,
                 "catalog.seller_blacklist", _sync_column_blacklist),
                (resources.AMZCAT_BLACKLIST_SHEET,
                 "catalog.amazon_cat_blacklist", _sync_amzcat_blacklist)):
            try:
                sheet = sheet_ref.require()
                rows = _first_row(sheet) + _read_sheet(sheet)
                with db.pg_conn() as c2:
                    lines.append(fn(c2, sheet, table, rows,
                                    allow_shrink=allow_shrink,
                                    dry_run=dry_run))
            except LookupError as e:
                lines.append(f"「{sheet_ref.name}」跳过:{e}")
            except Exception as e:
                logger.warning("「%s」同步失败(库内旧数据保留生效):%s",
                               sheet_ref.name, e)
                lines.append(f"⚠ 「{sheet_ref.name}」同步失败(旧数据保留):{e}")
        gate = risk_gate.load_gate(conn)
        banned_asins = blacklist.load_banned_asins(conn)
    # ⚠ 2026-08-21 改措辞:原文写「禁售类目 N 个」,紧跟在上面那行
    # 「黑名单亚马逊类目 223 条」后面,所有者当场把两个数当成同一件事对不上。
    # 它们**单位都不同**:上面是**亚马逊**类目路径/子树条目(L0 类目闸),
    # 这里是**沃尔玛 PT**(上架前的风控闸)。名字里写清是哪一侧、查的哪张表。
    lines.append(f"闸门现状:上架禁售沃尔玛 PT {len(gate['banned_pts'])} 个"
                 f"(catalog.risk_product_types 里 准入状态=禁售 或 "
                 f"中国卖家可做以「否」开头;与上面那 223 条**亚马逊**类目黑名单"
                 f"是两回事),"
                 f"黑名单品牌 {len(gate['brands'])} 个,"
                 f"ASIN 黑名单 {len(banned_asins)} 个")
    # ASIN 黑名单**不由本工作流同步**(所有者问询 2026-08-12 补可见性):
    # 它是自产回路——问题产品清理每日归类(B/C/E/F/G/K 六类)+ 上架违禁
    # 回执自动入库;PG 权威,blacklist_push 反向推飞书投影。此处只报数
    return "\n".join(lines)
