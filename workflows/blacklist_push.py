"""blacklist_push — PG 黑名单自产行 → 飞书两张收集表(投影,**整表重写**)。

用法:
  python cli.py blacklist_push                  # 两张表各按库里全量重写一遍
  python cli.py blacklist_push -p probe=1       # 只读体检:接线/子表身份/已填
                                                # 行数/水位对账。换表格、怀疑
                                                # "写了看不见"时先跑这个
  python cli.py blacklist_push -p allow_shrink=1  # 确认就是要把表写少(默认停手)

方向只有一个:PG → 飞书(PG 权威,表是人机界面)。品牌有**两张**飞书表,
角色相反,别混(所有者厘清 2026-08-11,此前混过一次):

  「黑名单品牌总表」(BRAND_BAN_SHEET,jF8dOw)——各渠道黑名单品牌由
  所有者人工归拢的总清单,方向**飞书→PG**,risk_sync 负责,与本工作流无关。

  「黑名单品牌(后台报错集成)」(BRAND_ERR_SHEET,beyKyi)——方向
  **PG→飞书**,只承接沃尔玛后台问题商品拿到的品牌,是所有者归拢总表时
  的一条**增量渠道**。渠道表**不与总清单去重**——品牌已在总表不挡渠道入账;
  总清单镜像(brand_blacklist)与渠道表各管各的,永不混写。曾有两版走错:
  ①只推 brand_blacklist 自产行(总表已有的品牌永远进不了渠道);②整表全推
  (把总清单复制进了渠道表)。现行口径为所有者终版。

**为什么是整表重写而不是按水位追加**(所有者定稿 2026-08-17:「这个映射是从
数据库映射上去的,不许管飞书里面的内容,直接清空覆盖」):两张表都是纯程序
投影,PG 就是权威。增量看着省,实际是笔糊涂账 ——
  · 要先线性扫列 A 找空行,5.7 万行的表每轮 285 个 GET,比写还贵;
  · 表被手工删过几行,水位就永久对不上(旧摘要只能报「⚠ 表行数≠已推水位」
    然后继续错下去);
  · 崩在半路留下重复行,靠"每块立刻打水位"缓解而不是根治。
整表重写没有水位,天然幂等,表被人动过下一轮自己就正回来了。
`pushed_at` 保留,含义变成"这行投影过了",给探针与对账用。

⚠ 配套**骤缩护栏**:库里查出的行数比表里现有的少超 2% 就停手一格不写。
PG 是权威没错,但"库这次只查出一小半"和"本来就该少"长得一模一样 ——
catmap_export 2026-08-17 的事故就是没有它,把 17592 行写成 15770 行。

一次性命令(都可反复跑,预览先行,apply 才动;两条都显式豁免骤缩护栏,
因为"擦净重灌"本来就会缩):
  -p rebuild_asin=1 [-p apply=1]   ASIN 黑名单重建:SKU 清洗(sku_normalize)
      之后跑——按标准 asin 擦净重灌(多店订货号归并、日期=报错发生日)。
  -p rebuild_brand=1 [-p apply=1]  品牌渠道重建:从时间线重灌渠道表
      (每品牌取最早报错日)——同时清掉 ②号错版复制进去的 42,064 行。
"""

import logging

from api import feishu
from registry import db, resources
from services import blacklist

DANGEROUS = False

logger = logging.getLogger("workflows.blacklist_push")

_ASIN_STATS = """
SELECT count(*) FILTER (WHERE pushed_at IS NOT NULL), count(*)
FROM catalog.asin_blacklist
"""

_BRAND_STATS = """
SELECT count(*) FILTER (WHERE pushed_at IS NOT NULL), count(*)
FROM catalog.brand_err_hits
"""

_CHANNEL_ALL = """
SELECT brand, source, added_date, src_sku
FROM catalog.brand_err_hits ORDER BY added_date, brand_key
"""

_CHANNEL_MARK_ALL = "UPDATE catalog.brand_err_hits SET pushed_at = now()"

_ASIN_ALL = """
SELECT asin, source, created_at::date::text
FROM catalog.asin_blacklist ORDER BY created_at, asin
"""

_ASIN_MARK_ALL = "UPDATE catalog.asin_blacklist SET pushed_at = now()"


def _probe() -> str:
    """输入:无 → 输出:两张表的只读体检报告,不写任何东西。

    每张表四件事:①sheet_id 在 env 指向的文档里存不存在(不存在 = token/
    sheet_id 指错了,这正是换表格后最容易错的地方);②子表标题(人眼核对
    身份);③列 A 已填行数 + 行 2 回读(API 侧真值,不受前端刷新影响);
    ④与库侧 pushed_at 水位对账(不一致多半是崩溃重推的重复行,人眼可辨)。
    """
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_ASIN_STATS)
            asin_stats = cur.fetchone()
            cur.execute(_BRAND_STATS)
            brand_stats = cur.fetchone()
    lines = []
    for sheet, (pushed, total) in ((resources.ASIN_BLACKLIST_SHEET, asin_stats),
                                   (resources.BRAND_ERR_SHEET, brand_stats)):
        s = sheet.require()
        titles = dict(feishu.sheet_list(s))
        if s.sheet_id not in titles:
            lines.append(f"「{s.name}」⛔ 文档里找不到 sheet_id={s.sheet_id},"
                         f"实有子表 {[f'{t}({i})' for i, t in titles.items()]}"
                         f"——env 的 wiki token 或 sheet_id 指错了")
            continue
        filled = _next_empty(s) - 2
        width = chr(ord("A") + len(s.columns) - 1)
        head = feishu.sheet_values(s, f"A2:{width}2") if filled else []
        lines.append(f"「{s.name}」→ 子表「{titles[s.sheet_id]}」已填 {filled} 行"
                     + (f",行 2 回读 {head[0]}" if head else "")
                     + f";库侧已推 {pushed} / 自产共 {total}"
                     + ("" if filled == pushed
                        else ",⚠ 表行数≠已推水位(崩溃重推的重复行或表被手动增删)"))
    return "黑名单投影探针(只读):" + ";".join(lines)


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


def _next_empty(sheet, start: int = 2) -> int:
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


SHRINK_TOLERANCE = 0.02     # 与 catmap_export 同一口径


class _Shrink(Exception):
    """骤缩:本次要写的行数比表里现有的少太多 —— 停手,一格不写。"""


def _rewrite_sheet(sheet, all_sql: str, mark_sql: str,
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
        # ⚠ 用 `_next_empty`(**已填行数**)而不是 `sheet_row_count`(**网格大小**)。
        # 飞书的网格会被 ensure_rows 撑大、且只增不减,拿它当"现有行数"会把
        # 空白网格算进去 —— 6.7 万行的表若网格 7 万,每轮都算成"缩了 2365 行"
        # 从而**每轮误停手**,护栏反倒成了故障源。代价是这里要扫一遍列 A
        # (5000 行/段,6.7 万行约 14 个 GET),换护栏说的是真话。
        now = _next_empty(s) - 2
        shrink = now - len(rows)
        if now > 0 and shrink > now * SHRINK_TOLERANCE:
            raise _Shrink(
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


def _rebuild_brand(do_apply: bool) -> str:
    """输入:是否 apply → 输出:品牌渠道重建摘要(见模块头「一次性命令」)。"""
    sheet = resources.BRAND_ERR_SHEET
    with db.pg_conn() as conn:
        c = blacklist.channel_counts(conn)
        if not do_apply:
            filled = _next_empty(sheet) - 2
            return (f"品牌渠道重建预览:①总表认领——沃尔玛来源品牌 "
                    f"{c['master']} 个(旧系统后台收集的历史,日期原样);"
                    f"②时间线推导——C/E 最新类 ASIN "
                    f"{c['with_brand'] + c['no_brand']} 个,采集库可解析品牌 "
                    f"{c['brands']} 个;beyKyi 现有 {filled} 行将整表重写为"
                    f"两腿去重后的行数(≤{c['master'] + c['brands']});"
                    f"加 -p apply=1 执行")
        st = blacklist.rebuild_brand_channel(conn)
    n = _rewrite_sheet(sheet, _CHANNEL_ALL, _CHANNEL_MARK_ALL,
                       allow_shrink=True)   # 擦净重灌,缩是预期
    return (f"品牌渠道重建:擦净 {st['wiped']} 行 → 总表认领 {st['seeded']} 个"
            f" + 时间线推导 {st['derived']} 个;beyKyi 整表重写 {n} 行")


def _rebuild_asin(do_apply: bool) -> str:
    """输入:是否 apply → 输出:ASIN 黑名单重建摘要。先跑 sku_normalize
    清洗事件账本,再来重建——否则重灌出来的键还是订货号原文。"""
    sheet = resources.ASIN_BLACKLIST_SHEET
    with db.pg_conn() as conn:
        c = blacklist.backfill_counts(conn)
        if not do_apply:
            filled = _next_empty(sheet) - 2
            return (f"ASIN 黑名单重建预览:时间线按标准 asin 归并后共 "
                    f"{c['total']} 个,永久禁止 {c['permanent']} 个;"
                    f"ASIN 表现有 {filled} 行将被整表重写为 {c['permanent']} 行"
                    f"(键=清洗后 asin,日期=报错发生日);加 -p apply=1 执行")
        st = blacklist.rebuild_asin_blacklist(conn)
    n = _rewrite_sheet(sheet, _ASIN_ALL, _ASIN_MARK_ALL,
                       allow_shrink=True)   # 擦净重灌,缩是预期
    return (f"ASIN 黑名单重建:擦净 {st['wiped']} 行 → 按标准 asin 重灌 "
            f"{st['inserted']} 行;ASIN 表整表重写 {n} 行")


def run(params: dict) -> str:
    """输入:params(limit/probe/backfill/rebuild_asin/rebuild_brand/apply)
    → 输出:推送摘要。

    -p backfill=1:ASIN 历史回填(预览计数;加 -p apply=1 真写后顺路投影)。
    -p rebuild_asin=1 / rebuild_brand=1:两侧重建(见各自函数)。
    都是一次性动作,重复跑无害(DO NOTHING/擦净重灌都幂等)。
    """
    if str(params.get("probe", "")).lower() in {"1", "true", "yes"}:
        return _probe()

    do_apply = str(params.get("apply", "")).lower() in {"1", "true", "yes"}
    if str(params.get("rebuild_brand", "")).lower() in {"1", "true", "yes"}:
        return _rebuild_brand(do_apply)
    if str(params.get("rebuild_asin", "")).lower() in {"1", "true", "yes"}:
        return _rebuild_asin(do_apply)

    lines = []

    if str(params.get("backfill", "")).lower() in {"1", "true", "yes"}:
        with db.pg_conn() as conn:
            c = blacklist.backfill_counts(conn)
            if not do_apply:
                return (f"历史回填预览:时间线共 {c['total']} 个 ASIN,"
                        f"最新类别属永久禁止 {c['permanent']} 个(将入 ASIN 黑名单);"
                        f"品牌渠道的历史重建走 -p rebuild_brand=1;"
                        f"加 -p apply=1 真写并顺路投影")
            st = blacklist.backfill_from_events(conn)
        lines.append(f"历史回填:ASIN +{st['asin_new']}")

    # 两张表都是**纯程序投影**(人工维护的是总表 jF8dOw,走 risk_sync 反方向),
    # 所以每轮整表重写、不管飞书里原来是什么(所有者定稿 2026-08-17:
    # 「这个映射是从数据库映射上去的,不许管飞书里面的内容,直接清空覆盖」)。
    #
    # 换掉的是"按 pushed_at 水位只追加"那套。增量看着省,实际是笔糊涂账:
    #   · 要先线性扫列 A 找空行——5.7 万行的表每轮 285 个 GET,比写还贵;
    #   · 表被手工删过几行,水位就永久对不上(旧摘要只能报一句"⚠ 表行数≠水位",
    #     然后继续错下去);
    #   · 崩在半路就留下重复行,靠"每块立刻打水位"缓解而不是根治。
    # 整表重写没有水位,自然幂等,表被人动过下一轮自己就正回来了。
    # pushed_at 保留:它现在的意思是"这行投影过了",给探针和对账用。
    allow_shrink = str(params.get("allow_shrink", "")).lower() in {"1", "true", "yes"}
    for sheet, all_sql, mark_sql in (
            (resources.ASIN_BLACKLIST_SHEET, _ASIN_ALL, _ASIN_MARK_ALL),
            (resources.BRAND_ERR_SHEET, _CHANNEL_ALL, _CHANNEL_MARK_ALL)):
        try:
            n = _rewrite_sheet(sheet, all_sql, mark_sql, allow_shrink=allow_shrink)
        except _Shrink as e:
            # 一张表停手不该拖垮另一张:各写各的
            lines.append(f"⛔ {e}")
            continue
        lines.append(f"「{sheet.name}」整表重写 {n} 行")
    return "黑名单投影:" + ";".join(lines)
