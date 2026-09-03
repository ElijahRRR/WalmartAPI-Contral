"""结算账期台账(ops.store_settlements)的读写积木。

累计回款 = 各账期 Total Payable 之和(所有者定稿 2026-08-31:「我需要累计
回款,就沃尔玛总共已经付给我的钱」)。

⚠ **为什么必须有台账,不能把每天的 `payout` 加起来**:`payout` 是
「当前待打款」的**快照**(closingBalance − reserve − hold),同一笔钱在打款
之前会天天出现在那一列;按天求和 = 同一笔重复计几十次。结算是按**账期**
发生的,一个账期只结算一次,所以累计的唯一正确单位是账期。

⚠ **台账的另一半价值:沃尔玛只保留有限期的对账文件**。
`availableReconFiles` 里的账期会随时间滚出去 —— 只在需要时现拉现算的话,
这个"累计"会随沃尔玛的保留期**缩水**。落进本表之后就永远留着,于是这份
累计随着系统运行时间**越来越完整**。这也是它值得单独一张表的理由。
"""

import logging

logger = logging.getLogger("services.settlements")

_UPSERT_SQL = """
INSERT INTO ops.store_settlements (store, report_date, total_payable)
VALUES (%(store)s, %(report_date)s, %(total_payable)s)
ON CONFLICT (store, report_date) DO UPDATE
    SET total_payable = EXCLUDED.total_payable, fetched_at = now()
"""


def known_dates(conn, store: str) -> set[str]:
    """输入:连接 + 店铺 → 输出:台账里已有的账期集合(MMDDYYYY 字符串)。

    同步件靠它做增量:只拉没见过的账期。账期一旦结算就不再变,重拉纯属浪费
    (一期一个 ZIP,几十家店首轮就是几千次下载)。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT report_date FROM ops.store_settlements"
                    " WHERE store = %s", (store,))
        return {r[0] for r in cur.fetchall()}


def record(conn, store: str, report_date: str, total_payable: float) -> None:
    """输入:连接 + 店铺 + 账期 + 该期应付总额 → 输出:无(幂等 upsert)。"""
    with conn.cursor() as cur:
        cur.execute(_UPSERT_SQL, {"store": store, "report_date": report_date,
                                  "total_payable": round(float(total_payable), 2)})


def totals(conn, stores: list[str] | None = None) -> dict[str, float]:
    """输入:连接(+店铺名单)→ 输出:{店铺: 累计回款};台账里没有的店不在字典里。

    ⚠ **不在字典里 ≠ 0**:那是"这家店还没同步过账期",与"确实一分钱没回"
    是两件事。调用方要把它写成空,不是 0 —— 写 0 会让人以为查过了。
    """
    sql = "SELECT store, sum(total_payable) FROM ops.store_settlements"
    args: tuple = ()
    if stores is not None:
        if not stores:
            return {}
        sql += " WHERE store = ANY(%s)"
        args = (list(stores),)
    sql += " GROUP BY store"
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return {r[0]: float(r[1] or 0) for r in cur.fetchall()}
