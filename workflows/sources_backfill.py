"""sources_backfill — 在架商品来源登记簿补齐(**只登记不猜**;幂等可重跑)。

用法:
  python cli.py sources_backfill --dry-run     # 盲区统计:在架未登记多少/样本
  python cli.py sources_backfill               # 真跑:写 catalog.listing_sources

背景(2026-08-19 所有者实遇):product_chain 拉回了在架商品,但维护链
(services/maintenance_intents 三个 provider)按路由铁律只认
catalog.listing_sources 里 source_type='amz' 的行——没登记来源的在架商品
一律不维护(防误伤设计,行为正确;问题是该有的登记行没有)。

**2026-09-06 所有者定稿:本工作流只登记不猜。** 原话:「sources_backfill 是
系统刚建立、刚从沃尔玛拉取产品下来没有关联来源码所以需要;但现在上架时有
来源码、上架时会生成 SKU,已经对应上了,一般情况下不需要这个;就算后续再从
沃尔玛拉到新的产品,我们应该做的是手动为其设置来源码。」于是:

  · 按 SKU 长相猜 ASIN 的判型(旧 `_ASIN_RE` 与 amz/旧格式存量/新码漏登记
    三桶)**全部删除**;refdata/schema.sql 里随 db_init 每轮执行的存量回填
    INSERT 同日删除(一次性动作早已做完,留着只会把尚未登记的新码抢先
    登成 unknown,让「有新行没归类」的信号沉默)。
  · 「在架有行、登记簿无行」的 (店, SKU) 一律登记为 `source_type='unknown'`
    + `source_key=NULL` + `workflow='backfill'`:unknown 的语义就是**不参与
    任何自动破坏动作**(路由铁律),等人工归类。
  · ON CONFLICT DO NOTHING:绝不覆盖 list_new / match_listing / sku_codec.mint
    已登记的行。

**人工归类是常规路径,不是例外**:
`python cli.py sources_reclassify`(缺省预览并导出待归类 CSV → 人填「确认
来源码」「确认来源类型」→ `python cli.py sources_reclassify -p file=… -p
apply=1` 导入)。摘要每轮都报**累计** unknown 待归类行数 —— 登记一次之后
计数不会沉默,「有新行没人归类」这件事一直看得见。

⚠ 归类成 amz 的行从此被 amz 快照驱动的**改价/清库存/删除**管到 —— 盲区变
辖区。破坏面是在 `sources_reclassify` 那一步打开的,不在本工作流:纪律见
该工作流头注(真跑后先 `python cli.py maintenance_scan --dry-run` 看意图量)。
"""

import logging

from registry import db
from services import listing_sources

DANGEROUS = False   # 只写登记簿一张表(DO NOTHING 幂等),不发 feed 不动状态

logger = logging.getLogger("workflows.sources_backfill")

#: 人工归类的唯一入口(摘要尾行与模块头注同一句,别处不许再写第二条路)
_RECLASSIFY_HINT = ("  人工归类:`python cli.py sources_reclassify`(导出待归类 "
                    "CSV → 人填「确认来源码」「确认来源类型」→ "
                    "`-p file=… -p apply=1` 导入)")

_SQL_GAP = """
SELECT w.store, w.sku FROM catalog.walmart_items w
WHERE w.missing_since IS NULL
  AND NOT EXISTS (SELECT 1 FROM catalog.listing_sources ls
                  WHERE ls.store = w.store AND ls.sku = w.sku)
ORDER BY w.store, w.sku
"""

#: 累计待归类计数。判据取 `source_type='unknown'`(所有者 2026-09-06 口径),
#: 比 `listing_sources.pending_reclassify` 的入口条件
#: (`source_type='unknown' OR source_key IS NULL`)窄一档 —— 那一条还捞
#: 「类型认得出但键缺了」的行,不是本摘要要报的东西。
_SQL_UNKNOWN_TOTAL = """
SELECT count(*) FROM catalog.listing_sources WHERE source_type = %s
"""


def run(params: dict) -> str:
    """输入:params(execute)→ 输出:登记摘要(本轮新增 + 累计待归类)。"""
    # ⚠ DANGEROUS=False ⇒ cli 恒给 execute=True(缺省即真跑),`--dry-run`
    # 只走 dry_run 这一路;漏认它的话本工作流的 --dry-run 会照写不误
    execute = bool(params.get("execute")) and not params.get("dry_run")
    mode = "" if execute else "🧪 [DRY-RUN] "
    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_GAP)
            gap = cur.fetchall()
        by_store: dict[str, int] = {}
        for s, _k in gap:
            by_store[s] = by_store.get(s, 0) + 1
        if execute:
            n = listing_sources.register(conn, [
                {"store": s, "sku": k,
                 "source_type": listing_sources.SOURCE_UNKNOWN,
                 "source_key": None,
                 "workflow": "backfill"}
                for s, k in gap])
            # 写完再数:累计数含本轮新增(同一事务内看得见)
            total = _unknown_total(conn)
        else:
            n = len(gap)
            # 空跑照样报"写完会是多少":gap 行按定义不在登记簿里,
            # 现有 unknown 数 + 本轮 n 就是真跑后的累计数(两个数同一口径)
            total = _unknown_total(conn) + n
        lines = [f"{mode}本轮新增登记 {n} 行(unknown,待人工归类)|"
                 f"累计 unknown 待归类 {total} 行"]
        # ⚠ insert(1,...) 不是 insert(0,...):本工作流常驻 product_chain,
        # 链通知只取首行 —— 顶掉 🧪 抬头会让一次 dry-run 的告警以真跑的面目
        # 出现在飞书里。告警行自带 mode 前缀,单独看也认得出是不是空跑。
        if gap:
            lines.insert(1, f"{mode}⚠ 在架未登记来源 {len(gap)} 行 —— 正常上架"
                            f"「谁上架谁登记」(list_new / match_listing / "
                            f"sku_codec.mint 同事务登记),出现新行多半是从沃尔玛"
                            f"新拉到的品(按所有者定稿:手动设来源码),"
                            f"也可能是上架主链被绕过;本工作流不猜出身,一律登 "
                            f"unknown(不参与任何自动破坏动作)")
            top = sorted(by_store.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
            lines.append("  分店:" + ",".join(f"{s}×{n_}" for s, n_ in top)
                         + (" …" if len(by_store) > 8 else ""))
            lines.append(f"  样本:{[(s, k) for s, k in gap[:8]]}")
            if not execute:
                lines.append("  真跑将写 catalog.listing_sources(全部 unknown;"
                             "DO NOTHING 不覆盖已登记行)")
        lines.append(_RECLASSIFY_HINT)
    return "\n".join(lines)


def _unknown_total(conn) -> int:
    """输入:连接 → 输出:登记簿里 source_type='unknown' 的总行数(累计待归类)。"""
    with conn.cursor() as cur:
        cur.execute(_SQL_UNKNOWN_TOTAL, (listing_sources.SOURCE_UNKNOWN,))
        row = cur.fetchone()
    return int(row[0]) if row else 0
