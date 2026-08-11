"""blacklist_push — PG 黑名单自产行 → 飞书两张收集表(投影,只追加)。

用法:
  python cli.py blacklist_push              # 推全部待推行(pushed_at IS NULL)
  python cli.py blacklist_push -p probe=1   # 只读体检:接线/子表身份/已填行数/
                                            # 水位对账。换表格、怀疑"写了看不见"
                                            # 时先跑这个——它回读飞书 API 真值
  python cli.py blacklist_push -p limit=500 # 单轮总量上限(默认不限;
                                            # 单请求 4000 行分块自动切)

方向只有一个:PG → 飞书(PG 权威,表是人机界面)。品牌有**两张**飞书表,
角色相反,别混(所有者厘清 2026-08-11,此前混过一次):

  「黑名单品牌总表」(BRAND_BAN_SHEET,jF8dOw)——各渠道黑名单品牌由
  所有者人工归拢的总清单,方向**飞书→PG**,risk_sync 负责,与本工作流无关。

  「黑名单品牌(后台报错集成)」(BRAND_ERR_SHEET,beyKyi)——方向
  **PG→飞书**,只承接沃尔玛后台问题商品拿到的品牌,是所有者归拢总表时
  的一条**增量渠道**。

推送范围:
  catalog.asin_blacklist   pushed_at IS NULL 的全部行(ASIN 表 = 库的全量映射)
  catalog.brand_err_hits   pushed_at IS NULL 的全部行(beyKyi = 渠道表的
                           全量映射)。渠道表**不与总清单去重**——品牌已在
                           总表不挡渠道入账;总清单镜像(brand_blacklist)
                           与渠道表各管各的,永不混写。曾有两版走错:
                           ①只推 brand_blacklist 自产行(总表已有的品牌
                           永远进不了渠道);②整表全推(把总清单复制进了
                           渠道表)。现行口径为所有者终版。

一次性命令:
  -p rebuild_brand=1 [-p apply=1]  品牌渠道重建:从时间线重灌渠道表
      (每品牌取最早报错日),beyKyi **整表重写**——同时清掉 ②号错版
      复制进去的 42,064 行总清单内容。预览先行,apply 才动。

水位语义:每块写成功后当场把该块行的 pushed_at 打上。写表与打水位之间
崩掉的话,那一块下次会**重推(表里出现重复行)而不是丢行**——收集表是
展示面板,重复行人眼可辨可删;丢行则没人会发现。方向选择与 maint_sheet
反哺器同一权衡。

追加机制照抄 maint_sheet 趟过的坑:先读列 A 找真实的下一空行(飞书 +append
遇日期富文本行会误判,legacy_survey:1360 旧系统就中过),再 ensure_rows,
再分块写。
"""

import logging
import time

from api import feishu
from registry import db, resources
from services import blacklist

DANGEROUS = False

logger = logging.getLogger("workflows.blacklist_push")

_BLOCK = 4000         # 单块行数,与 api 层 sheet_overwrite 同源(13 万行在线
                      # 产品总表实证):真硬限是单请求载荷 ~4MB(20 列×5000 行
                      # 撞 90227),本表 3-4 列短字段远不到。曾误设 500——那是
                      # maint_sheet 在 90202 事故(真凶是控制字符,_scrub 已修)
                      # 当天的应急值,不是接口上限。块间 0.2s 节流

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
FROM catalog.brand_err_hits
WHERE pushed_at IS NULL
ORDER BY created_at LIMIT %s
"""

_BRAND_MARK = """
UPDATE catalog.brand_err_hits SET pushed_at = now() WHERE brand_key = ANY(%s)
"""

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
        if i + _BLOCK < len(rows):
            time.sleep(0.2)             # 5.6 万行 ≈ 15 个请求,别打成突刺
    return written, blocks


def _rebuild_brand(do_apply: bool) -> str:
    """输入:是否 apply → 输出:品牌渠道重建摘要(见模块头「一次性命令」)。

    apply 三步:①渠道表擦净重灌;②beyKyi **整表重写**(表头行回读原样
    保留,数据行全量替换,sheet_overwrite 会删掉尾部残留——错版复制进去
    的总清单内容就是这样清掉的);③全表打 pushed_at 水位。
    """
    sheet = resources.BRAND_ERR_SHEET
    with db.pg_conn() as conn:
        c = blacklist.channel_counts(conn)
        if not do_apply:
            filled = _next_empty(sheet) - 2
            return (f"品牌渠道重建预览:时间线 C/E 最新类 ASIN "
                    f"{c['with_brand'] + c['no_brand']} 个,可解析品牌 "
                    f"{c['brands']} 个(缺品牌 ASIN {c['no_brand']} 个,"
                    f"等 product_ingest 补齐后由日常链路自然入账);"
                    f"beyKyi 现有 {filled} 行将被整表重写为 {c['brands']} 行;"
                    f"加 -p apply=1 执行")
        st = blacklist.rebuild_brand_channel(conn)

    s = sheet.require()
    hdr = feishu.sheet_values(s, "A1:D1")
    header = ((hdr[0] if hdr else []) + [""] * 4)[:4]
    if not any(str(h or "").strip() for h in header):
        header = list(s.columns)        # 表头意外为空才用登记列名兜底
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_CHANNEL_ALL)
            rows = [[b, src or "", day or "", sku or ""]
                    for b, src, day, sku in cur.fetchall()]
    feishu.sheet_overwrite(s, [header] + rows)
    with db.pg_conn() as conn:
        conn.execute(_CHANNEL_MARK_ALL, ())
    return (f"品牌渠道重建:渠道表擦净 {st['wiped']} 行 → 重灌 "
            f"{st['brands']} 个品牌;beyKyi 整表重写 {len(rows)} 行"
            f"(缺品牌 ASIN {c['no_brand']} 个等补齐后自然入账)")


def run(params: dict) -> str:
    """输入:params(limit/probe/backfill/rebuild_brand/apply)→ 输出:推送摘要。

    -p backfill=1:ASIN 历史回填(预览计数;加 -p apply=1 真写后顺路投影)。
    -p rebuild_brand=1:品牌渠道重建(见 _rebuild_brand)。
    两者都是一次性动作,重复跑无害(DO NOTHING/擦净重灌都幂等)。
    """
    if str(params.get("probe", "")).lower() in {"1", "true", "yes"}:
        return _probe()

    do_apply = str(params.get("apply", "")).lower() in {"1", "true", "yes"}
    if str(params.get("rebuild_brand", "")).lower() in {"1", "true", "yes"}:
        return _rebuild_brand(do_apply)

    limit = int(params.get("limit", 1_000_000))
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
