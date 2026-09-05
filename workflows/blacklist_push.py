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
from services import blacklist, blacklist_sheet as sheets

DANGEROUS = False

logger = logging.getLogger("workflows.blacklist_push")

def _probe() -> str:
    """输入:无 → 输出:两张表的只读体检报告,不写任何东西。

    每张表四件事:①sheet_id 在 env 指向的文档里存不存在(不存在 = token/
    sheet_id 指错了,这正是换表格后最容易错的地方);②子表标题(人眼核对
    身份);③列 A 已填行数 + 行 2 回读(API 侧真值,不受前端刷新影响);
    ④与库侧 pushed_at 水位对账(不一致多半是崩溃重推的重复行,人眼可辨)。
    """
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sheets.ASIN_STATS)
            asin_stats = cur.fetchone()
            cur.execute(sheets.BRAND_STATS)
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
        filled = sheets.next_empty(s) - 2
        width = chr(ord("A") + len(s.columns) - 1)
        # 固定单行回读(行 2),上界不随表长走 ⇒ 小范围薄壳
        head = feishu.sheet_values_small(s, f"A2:{width}2") if filled else []
        lines.append(f"「{s.name}」→ 子表「{titles[s.sheet_id]}」已填 {filled} 行"
                     + (f",行 2 回读 {head[0]}" if head else "")
                     + f";库侧已推 {pushed} / 自产共 {total}"
                     + ("" if filled == pushed
                        else ",⚠ 表行数≠已推水位(崩溃重推的重复行或表被手动增删)"))
    return "黑名单投影探针(只读):" + ";".join(lines)


def _rebuild_brand(do_apply: bool) -> str:
    """输入:是否 apply → 输出:品牌渠道重建摘要(见模块头「一次性命令」)。"""
    sheet = resources.BRAND_ERR_SHEET
    with db.pg_conn() as conn:
        c = blacklist.channel_counts(conn)
        if not do_apply:
            filled = sheets.next_empty(sheet) - 2
            return (f"品牌渠道重建预览:①总表认领——沃尔玛来源品牌 "
                    f"{c['master']} 个(旧系统后台收集的历史,日期原样);"
                    f"②时间线推导——C/E 最新类 ASIN "
                    f"{c['with_brand'] + c['no_brand']} 个,采集库可解析品牌 "
                    f"{c['brands']} 个;beyKyi 现有 {filled} 行将整表重写为"
                    f"两腿去重后的行数(≤{c['master'] + c['brands']});"
                    f"加 -p apply=1 执行")
        st = blacklist.rebuild_brand_channel(conn)
    n = sheets.rewrite_sheet(sheet, sheets.CHANNEL_ALL,
                             sheets.CHANNEL_MARK_ALL,
                             allow_shrink=True)   # 擦净重灌,缩是预期
    return (f"品牌渠道重建:擦净 {st['wiped']} 行 → 总表认领 {st['seeded']} 个"
            f" + 时间线推导 {st['derived']} 个;beyKyi 整表重写 {n} 行")


def _opaque_note(c: dict) -> str:
    """输入:backfill_counts 结果 → 输出:不透明码键告警(零时返空串)。

    零时返空串是硬要求:摘要要与加这一档之前逐字相同(生产库里今天不该有
    不透明码)。非零 = 那批键登记簿查不到,拦不住任何东西(见 D-0b-1)。
    """
    n = c.get("opaque") or 0
    return (f";⚠ 其中 {n} 个键形如不透明码(登记簿查不到 ⇒ 拦不住任何东西),"
            f"见 D-0b-1") if n else ""


def _rebuild_asin(do_apply: bool) -> str:
    """输入:是否 apply → 输出:ASIN 黑名单重建摘要。先跑 sku_normalize
    清洗事件账本,再来重建——否则重灌出来的键还是订货号原文。"""
    sheet = resources.ASIN_BLACKLIST_SHEET
    with db.pg_conn() as conn:
        c = blacklist.backfill_counts(conn)
        if not do_apply:
            # ⚠ 报 **PG** 的数,不报飞书表格行数(原先取的是
            #   `sheets.next_empty(sheet) - 2`,而删的是 PG —— 报的不是要删的
            #   那个数,人核对不了)。
            return (f"ASIN 黑名单重建预览:时间线按标准 asin 归并后共 "
                    f"{c['total']} 个,永久禁止 {c['permanent']} 个;"
                    f"PG 表现有 {c['in_table']} 行 ⇒ **有事件背书的 "
                    f"{c['in_table'] - c['untouched']} 行会被重灌成 "
                    f"{c['permanent']} 行**,"
                    f"另外 **{c['untouched']} 行没有产品事件背书,一条都不碰**"
                    f"(历史导入,重灌不出来 —— 所有者 2026-09-04 定「需要保留」);"
                    f"键=清洗后 asin,日期=报错发生日;加 -p apply=1 执行"
                    + _opaque_note(c))
        st = blacklist.rebuild_asin_blacklist(conn)
    n = sheets.rewrite_sheet(sheet, sheets.ASIN_ALL,
                             sheets.ASIN_MARK_ALL,
                             allow_shrink=True)   # 擦净重灌,缩是预期
    return (f"ASIN 黑名单重建:删掉有事件背书的 {st['wiped']} 行 → 按标准 asin "
            f"重灌 {st['inserted']} 行;**没有事件背书的 {st['untouched']} 行"
            f"原样保留**;ASIN 表整表重写 {n} 行")


def run(params: dict) -> str:
    """输入:params(limit/probe/backfill/rebuild_asin/rebuild_brand/apply)
    → 输出:推送摘要。

    -p backfill=1:ASIN 历史回填(预览计数;加 -p apply=1 真写后顺路投影)。
    -p rebuild_asin=1 / rebuild_brand=1:两侧重建(见各自函数)。
    ⚠ `backfill` 重复跑无害(ON CONFLICT DO NOTHING);两个 `rebuild` 是
      **先删后灌**,只有「删得掉的等于灌得回的」才谈得上无害 —— `rebuild_asin`
      因此只删有产品事件背书的行(2026-09-04,见 `blacklist._ASIN_WIPE_SQL`)。
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
                top = "  ".join(
                    f"{k}×{n}" for k, n in c["fresh_codes"].most_common(8))
                return "\n".join([
                    f"历史回填预览:产品历史共 {c['total']:,} 个 ASIN,"
                    f"按**全部历史报错、够格拉黑的那条优先**判定后,"
                    f"该永久拉黑 {c['permanent']:,} 个" + _opaque_note(c),
                    f"  表里现有 {c['in_table']:,} 行 ⇒ **真跑只会新增 "
                    f"{c['fresh']:,} 条**(ON CONFLICT DO NOTHING,已在表里的不动;"
                    f"**回填只加不减**)",
                    f"  将新增,按新码:{top}" if top else "  没有要新增的行",
                    "  ⚠ **这里是产品级判定,而 `asin_blacklist.taxonomy_code` 是"
                    "行级记录** —— 前者看该 asin 的全部历史(所有者 2026-09-04:"
                    "「被拉黑的那个作为最高优先级,其他的都是作为记录」),"
                    "后者只是那一行入选时那条原文的归类。两者**本来就会不一样**,"
                    "不一样时**以产品级为准**。",
                    "  ⚠ 而 `blacklist_route` 删行用的是**行级**码 —— 判据没跟上,"
                    "在它改过来之前别拿这里的新增去对路由的结果。",
                    "  品牌渠道的历史重建走 -p rebuild_brand=1;加 -p apply=1 真写并顺路投影",
                ])
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
    lines += sheets.push_all(allow_shrink=allow_shrink)
    return "黑名单投影:" + ";".join(lines)
