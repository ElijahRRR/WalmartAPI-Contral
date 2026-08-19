"""PG 连接余量护栏(2026-08-18 从 product_audit 抽出,与 list_new 共用)。

"每 worker 独占一条连接"的并发链(审核判定、上架 LLM 出参)开池之前,
按库的实际余量往下钳 worker 数。纪律(2026-08-14 所有者实遇定下):
**钳制必须说出来** —— 静默钳制 = 拿着错的数做并发决策;
**查不到余量不猜不钳** —— 护栏本身不许成为新的故障点。
"""

import logging

from registry import db

logger = logging.getLogger("services.db_guard")

# 留给别人的连接余量:本轮之外还有 launchd 的 feed_poll/订单链、人在 psql 里
# 查东西、以及 PG 自己给 superuser 保留的那几条(superuser_reserved_connections)。
# 把余量吃干净的表现不是"慢",是**别的链连不上库**。
_CONN_HEADROOM = 20


def cap_workers(workers: int) -> tuple[int, str]:
    """输入:想要的 worker 数 → 输出:(实际 worker 数, 摘要行)。

    ⚠ **每个 worker 独占一条 PG 连接**(`db.pg_conn` 是一次 `psycopg.connect`,
    没有池),而连接在整个 `audit_one` 期间都被握着(含那次几秒的 LLM 调用),
    所以池子不能小于 worker 数 —— 小了就是把并发本身按池子大小掐掉。

    于是唯一能做的是**按库的实际余量往下钳 worker 数**。默认 128 意味着 129 条
    连接,而 PostgreSQL 的 `max_connections` 缺省是 **100**:
    在缺省配置的机器上,ExitStack 建池建到第 ~100 条就抛
    `FATAL: sorry, too many clients already`,整轮审核起不来。
    炸是响的(退 1 + 飞书通知),但它每天 18:10 都会炸一次。

    钳制**必须说出来**(本仓纪律:静默钳制 = 拿着错的数做并发决策 —— 2026-08-14
    所有者用 workers=32 测吞吐、实际跑 16 而输出只字未提,就是这条的由来)。

    查不到库的余量时不猜、不钳,只在摘要里说一句 —— 这一步是护栏,
    护栏本身不该成为新的故障点。
    """
    try:
        with db.pg_conn() as conn, conn.cursor() as cur:
            cur.execute("SHOW max_connections")
            hard = int(cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM pg_stat_activity")
            used = int(cur.fetchone()[0])
    except Exception as e:                                      # noqa: BLE001
        logger.warning("查 PG 连接余量失败(不钳并发): %s", e)
        return workers, f"⚠ 未能查到 PG 连接余量({e}),并发按 {workers} 跑"
    # +1 是主线程那条事务连接;used 里已经含本次查询的那条(它此刻还开着)
    avail = hard - used - _CONN_HEADROOM
    room = max(1, avail - 1)
    if workers <= room:
        return workers, ""
    logger.warning("并发 %d 需要 %d 条 PG 连接,而 max_connections=%d、"
                   "已用 %d、留给别人 %d ⇒ 实际用 %d",
                   workers, workers + 1, hard, used, _CONN_HEADROOM, room)
    return room, (f"⚠ 并发从 {workers} 钳到 **{room}**:每个 worker 独占一条 PG "
                  f"连接,而 max_connections={hard}、已用 {used}、给别的链留 "
                  f"{_CONN_HEADROOM} 条。要跑满就调大 PG 的 max_connections "
                  f"(改 postgresql.conf 后重启),不是调 -p workers=")
