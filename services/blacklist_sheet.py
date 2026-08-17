"""黑名单两张收集表的飞书投影积木(PG → 飞书,**整表重写**)。

原先这一段长在 `workflows/blacklist_push.py` 里。搬到 services 是为了让
`problem_scan` 跑完能顺手推一次(所有者定稿 2026-08-17:「让 problem_scan
完成后立马推飞书」),而工作流之间不准互相 import(铁律 1)。

这正是所有者早先那条要求的同款做法(`docs/schedule_plan.md` §四:
「已对接飞书表的,执行完就写,不要做成单独的」)—— `order_center_push` 已经
这么拆过一次,五个 pusher 进 `services/order_center.py`,各链写自己那张表。

**两个入口的失败语义相反,是有意的**(与 order_center 逐字同款纪律):

  · `push_after()`  业务链顺手写的那次 —— **永不抛错**。黑名单已经落 PG 了,
    闸门此刻已经生效(上架/审核读 PG,从不读表),飞书写挂不该把一轮扫描
    记成 failed。但**必须说出来**,否则表现成"跑成功了可表格没动"。
  · `push_all()`    你专门去补推的那次(`blacklist_push` 工作流)——
    推不成必须让异常冒泡,记 failed。

⚠ 两张表的角色相反,别混(所有者厘清 2026-08-11):本模块只写
**渠道表**「黑名单品牌(后台报错集成)」与「ASIN 黑名单」,方向 PG→飞书。
「黑名单品牌总表」是人工归拢的总清单,方向飞书→PG,归 `risk_sync`,
本模块一个格子都不碰它。
"""

import logging

from api import feishu
from registry import db, resources

logger = logging.getLogger("services.blacklist_sheet")

SHRINK_TOLERANCE = 0.02     # 与 catmap_export 同一口径

_SCAN_BLOCK = 5000      # 找空行时每个 GET 读多少行(**只读列 A**,一列 5000 个
                        # 短串约 50KB,离飞书的响应上限很远)。
                        # 原值 200:ASIN 表已填 5.7 万行 ⇒ 每轮 285 个 GET、
                        # 每个约 0.3 秒 ≈ 一分半,而真正的写只有 3 个请求
                        # (4000 行/块)。所有者 2026-08-17 实见"一次就写了
                        # 200 行"——那不是写,是这里在逐段读。
                        # ⚠ 这一段是 O(表已填行数):表越长越慢,只是常数小了
                        # 24 倍。哪天 ASIN 表涨到几十万行,该换成二分探测
                        # (但那要求"已填行是连续前缀",与现在"找首个空行"的
                        # 语义不同——中间被手工删出一个洞时两者结果不一样)。

CHANNEL_ALL = """
SELECT brand, source, added_date, src_sku
FROM catalog.brand_err_hits ORDER BY added_date, brand_key
"""

CHANNEL_MARK_ALL = "UPDATE catalog.brand_err_hits SET pushed_at = now()"

ASIN_ALL = """
SELECT asin, source, created_at::date::text
FROM catalog.asin_blacklist ORDER BY created_at, asin
"""

ASIN_MARK_ALL = "UPDATE catalog.asin_blacklist SET pushed_at = now()"

ASIN_STATS = """
SELECT count(*) FILTER (WHERE pushed_at IS NOT NULL), count(*)
FROM catalog.asin_blacklist
"""

BRAND_STATS = """
SELECT count(*) FILTER (WHERE pushed_at IS NOT NULL), count(*)
FROM catalog.brand_err_hits
"""

# 谁投影哪张表 —— **登记在这一处**,工作流里不抄。与 order_center.BY_WORKFLOW
# 同款理由:加一张表时漏改的那条链会**静默不写**,而且看不出来
TABLES = (
    ("asin", "ASIN_BLACKLIST_SHEET", ASIN_ALL, ASIN_MARK_ALL),
    ("brand", "BRAND_ERR_SHEET", CHANNEL_ALL, CHANNEL_MARK_ALL),
)


class Shrink(Exception):
    """骤缩:本次要写的行数比表里现有的少太多 —— 停手,一格不写。"""


def next_empty(sheet, start: int = 2) -> int:
    """输入:登记条目 + 起扫行 → 输出:列 A 首个空行行号(1 行是表头)。"""
    grid = feishu.sheet_row_count(sheet)
    row = start
    while row <= grid:
        end = min(row + _SCAN_BLOCK - 1, grid)
        vals = feishu.sheet_values(sheet, f"A{row}:A{end}")
        got = [(str(c[0]).strip() if c and c[0] is not None else "")
               for c in (vals + [[None]] * (end - row + 1))[:end - row + 1]]
        for i, v in enumerate(got):
            if not v:
                return row + i
        row = end + 1
    return row


def rewrite_sheet(sheet, all_sql: str, mark_sql: str,
                  *, allow_shrink: bool = False) -> int:
    """输入:登记条目 + 全量行 SQL + 打水位 SQL → 输出:重写的数据行数。

    整表重写三步:①表头行回读原样保留(读不到才用登记列名兜底);
    ②sheet_overwrite 全量替换(尾部残留自动删——错版内容就是这样清掉的);
    ③全表打 pushed_at 水位。

    ⚠ **骤缩护栏**(2026-08-17 catmap_export 的生产事故教训:整表重写把飞书
    17592 行写成 15770 行,净丢 1847 行且当时没有任何东西拦住)。本表虽然是
    纯程序投影、PG 就是权威,但"库这次只查出一小半"同样会把表清掉 ——
    查询写错、连接中断拿到半截结果、误删了库里的行,表现都是一样的。
    缩量超 SHRINK_TOLERANCE 直接抛,一格不写;确认就是要缩加 -p allow_shrink=1。
    重建类命令(rebuild_asin/rebuild_brand)本来就是"擦净重灌",显式豁免。
    """
    s = sheet.require()
    ncols = len(s.columns)
    width = chr(ord("A") + ncols - 1)
    hdr = feishu.sheet_values(s, f"A1:{width}1")
    header = ((hdr[0] if hdr else []) + [""] * ncols)[:ncols]
    if not any(str(h or "").strip() for h in header):
        header = list(s.columns)
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(all_sql)
            rows = [[c if c is not None else "" for c in r]
                    for r in cur.fetchall()]
    if not allow_shrink:
        # ⚠ 用 `next_empty`(**已填行数**)而不是 `sheet_row_count`(**网格大小**)。
        # 飞书的网格会被 ensure_rows 撑大、且只增不减,拿它当"现有行数"会把
        # 空白网格算进去 —— 6.7 万行的表若网格 7 万,每轮都算成"缩了 2365 行"
        # 从而**每轮误停手**,护栏反倒成了故障源。代价是这里要扫一遍列 A
        # (5000 行/段,6.7 万行约 14 个 GET),换护栏说的是真话。
        now = next_empty(s) - 2
        shrink = now - len(rows)
        if now > 0 and shrink > now * SHRINK_TOLERANCE:
            raise Shrink(
                f"「{s.name}」**已停手,一格未写**:表里现有 {now} 行,"
                f"库里只查出 {len(rows)} 行,要少 {shrink} 行"
                f"(超过 {SHRINK_TOLERANCE:.0%} 容忍)。"
                f"PG 是权威没错,但「库这次只查出一小半」和「本来就该少」"
                f"长得一样 —— 先确认库侧没出问题;"
                f"确认就是要缩,加 -p allow_shrink=1")
    feishu.sheet_overwrite(s, [header] + rows)
    with db.pg_conn() as conn:
        conn.execute(mark_sql, ())
    return len(rows)


def push_all(*, allow_shrink: bool = False) -> list[str]:
    """输入:是否豁免骤缩护栏 → 输出:每张表一行摘要。**非骤缩异常照常冒泡**。

    `blacklist_push` 工作流用这条 —— 人专门去补推的,推不成必须记 failed。

    ⚠ 骤缩**逐表隔离**:一张表停手不该拖垮另一张(它们各是一份独立投影),
    停手那张在返回值里以 ⛔ 开头,由调用方决定怎么报。
    """
    out = []
    for _key, res_name, all_sql, mark_sql in TABLES:
        sheet = getattr(resources, res_name)
        try:
            n = rewrite_sheet(sheet, all_sql, mark_sql,
                              allow_shrink=allow_shrink)
        except Shrink as e:
            out.append(f"⛔ {e}")
            continue
        out.append(f"「{sheet.name}」整表重写 {n} 行")
    return out


def push_after() -> str:
    """输入:无 → 输出:一行摘要。**扫描链跑完顺手投影**用,永不抛错。

    与 `push_all` 的差别只有一条:**永不抛错**(与 `order_center.push_after`
    逐字同款纪律)。黑名单已经落 PG 了,而**闸门此刻已经生效** ——
    上架与审核读的是 PG,从不读飞书表。所以飞书写挂不该让那一轮扫描被记成
    failed,但**必须说出来**,否则表现成"跑成功了可表格没动"。

    ⚠ 骤缩护栏在这条路径上**照旧生效**(不传 allow_shrink)。库侧刚出过问题的
    时候,顺手那一推更不该把表清掉;豁免要人显式去跑
    `blacklist_push -p allow_shrink=1`。
    """
    try:
        lines = push_all()
    except Exception as e:                                      # noqa: BLE001
        logger.warning("黑名单飞书投影失败(黑名单已在 PG,闸门已生效): %s", e)
        return (f"⚠ 飞书投影失败:{e}"
                f"(黑名单已在 PG,上架/审核的闸门已经生效;"
                f"补推 `python cli.py blacklist_push`)")
    return "飞书投影:" + ";".join(lines)
