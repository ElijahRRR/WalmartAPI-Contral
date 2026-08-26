"""店铺「本轮缺席」判据(店级重试标准③④的数据面,所有者定稿 2026-08-26)。

⚠ **与三层店铺状态判据不是一回事**(CLAUDE.md 所有者定稿 2026-08-22):
`registered_names()` 答"在不在册"、`enabled_names()` 答"在不在营"、
`load_stores()` 答"现在能不能调 API" —— 三层答的都是"这家店是什么状态"。
本模块答的是第四个、也是**唯一一个跟着数据走**的问题:
**"这家店的目录数据这轮新鲜不新鲜"**。拿本模块判在营 = 拿错判据
(缺席 ≠ 停用);拿 enabled_names 判新鲜度 = 把缺席店的陈旧现值当今天的算
(2026-08-26 13:00 事故的下游形态:38 条 not found 的老账同款)。

判据从库里的事实派生,**不建新表、不靠进程内握手、不靠调度顺序**
(CLAUDE.md:调度顺序不许承载判据):catalog_sync 对某店失败时整店不写
(upsert 与 mark_missing 都在单店成功后才调,workflows/catalog_sync 实证),
该店的 `max(catalog.walmart_items.last_seen_at)` 于是停在上一轮 ——
「整店水位早于本轮起点」就是"缺席"的全部定义。

盲区(如实交代):首次同步前的店在 walmart_items 里没有行,查不出水位 ——
它也没有任何下游数据面,缺席概念对它不适用。
"""

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("services.store_absence")

# 下游避让的新鲜度窗口:链一天一轮(13:00),留 4 小时余量 ——
# 与 maintenance_intents.SUPPRESS_HOURS 同一个推导,不是巧合是同源。
STALE_HOURS = 20

_SQL_WATERMARK = """
SELECT store, max(last_seen_at) FROM catalog.walmart_items GROUP BY store
"""


def stale_stores(conn, since=None, hours: int = STALE_HOURS) -> list[str]:
    """输入:连接(+本轮起点 since 或小时窗 hours)→ 输出:缺席店名(排序)。

    缺席 = **在营**(enabled_names,唯一在营判据)∧ 有目录行 ∧
    整店最近观测早于起点。since 优先;不传则用 now()-hours(下游避让口径)。
    只看在营店:停用店的水位永远陈旧,不过滤的话它会天天霸占缺席名单,
    把真正的临时故障淹没掉。
    """
    from services import stores as stores_svc

    cutoff = since or (datetime.now(timezone.utc) - timedelta(hours=hours))
    enabled = set(stores_svc.enabled_names())
    with conn.cursor() as cur:
        cur.execute(_SQL_WATERMARK)
        rows = cur.fetchall()
    out = sorted(s for s, wm in rows
                 if s in enabled and wm is not None and wm < cutoff)
    if out:
        logger.warning("缺席店 %d 家(整店目录水位早于 %s):%s",
                       len(out), cutoff.isoformat(timespec="seconds"),
                       ",".join(out))
    return out
