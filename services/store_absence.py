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
from datetime import timedelta

logger = logging.getLogger("services.store_absence")

# 下游避让的判据是**船队相对滞后**,不是绝对小时窗:
# 「这家店的水位比全船队最新水位落后超过 LAG_HOURS」才算缺席。
# 为什么不能用绝对窗(如 now()-20h):链一天一轮,昨天 13:05 同步、
# 今天 09:05 起**所有店**都会超过任何 <24h 的绝对窗 —— 早晨手动
# `maintenance_scan --dry-run`(改码后必做的纪律)会把全部店误判缺席、
# 产出恒零;而窗放到 >24h 又接不住"今天刚失败"的事故店(24.4h < 26h)。
# 相对滞后两头都对:同步刚跑完,健康店水位=今天、缺席店=昨天,滞后 24h
# 一眼判出;全船队都停在昨天(没人同步过)则彼此滞后 0,判据自然退场
# ——那是"该不该扫"的问题(调度顺序纪律管),不是"谁缺席"的问题。
LAG_HOURS = 4   # 同一轮链内各店完成时刻的最大合理散布(补试串行在尾部,留余量)

# 「长期缺席」分界:落后船队超过 3 天 = 不是临时故障(代理抖动当天就该
# 恢复;三天不恢复的是凭证死/代理没配/店被停权,重试只会天天再死一次)。
# 链尾重赛对长期缺席店**不再逐日空跑**(2026-08-26 对抗校验:一家凭证死但
# 凭证表仍勾「启用」的店会每天两条链各刷一行 ❌,三天后没人再看通知 ——
# 不看的通知等于没有通知);扫描件的**避让照旧覆盖它们**(陈旧就是陈旧)。
CHRONIC_LAG_HOURS = 72

# ⚠ 只看**在架行**(missing_since IS NULL)的水位:一家「同步成功但在线
# 0 商品」的店,upsert 一行都不写、mark_missing 把存量全标缺席 —— 按全行
# 水位它会永久像缺席店(天天进名单、天天被"救回"),而它在架为零,下游
# 本来就没有任何数据面,缺席概念对它不适用。
_SQL_WATERMARK = """
SELECT store, max(last_seen_at) FROM catalog.walmart_items
WHERE missing_since IS NULL
GROUP BY store
"""


def _watermarks(conn) -> dict:
    """输入:连接 → 输出:{在营店: 整店在架行水位}(无行/不在营的不出现)。"""
    from services import stores as stores_svc

    enabled = set(stores_svc.enabled_names())
    with conn.cursor() as cur:
        cur.execute(_SQL_WATERMARK)
        rows = cur.fetchall()
    return {s: wm for s, wm in rows if s in enabled and wm is not None}


def stale_stores(conn, since=None, lag_hours: int = LAG_HOURS) -> list[str]:
    """输入:连接(+绝对锚点 since 或相对滞后 lag_hours)→ 输出:缺席店名(排序)。

    缺席 = **在营**(enabled_names,唯一在营判据)∧ 有在架目录行 ∧ 整店水位
    早于判据线。判据线二选一:
      · since(cli 链尾重赛用):绝对锚点 = 本轮链起点,水位没跨过它就是
        本轮没同步成;
      · 缺省(下游避让用):船队最新水位 − lag_hours(见模块头注,绝对窗
        两头都错)。
    只看在营店:停用店的水位永远陈旧,不过滤会天天霸占缺席名单,
    把真正的临时故障淹没掉。长期缺席(凭证死/缺代理但没人去停用)**照样
    在列** —— 避让方拿它没错;要区分"今天抖了"还是"死了三天"用 split_stale。
    """
    marks = _watermarks(conn)
    if not marks:
        return []
    cutoff = since if since is not None \
        else max(marks.values()) - timedelta(hours=lag_hours)
    out = sorted(s for s, wm in marks.items() if wm < cutoff)
    if out:
        logger.warning("缺席店 %d 家(整店目录水位早于 %s):%s",
                       len(out), cutoff.isoformat(timespec="seconds"),
                       ",".join(out))
    return out


def split_stale(conn, since) -> tuple[list[str], list[str]]:
    """输入:连接 + 本轮链起点 → 输出:(今日缺席, 长期缺席)(各自排序)。

    给 cli 链尾重赛用的分诊:今日缺席(水位没跨过链起点、但落后船队不足
    CHRONIC_LAG_HOURS)值得逐店重赛一次;长期缺席重试是空跑,只点名不重赛
    —— 它们的归宿是修凭证/代理,或者去凭证表取消「启用」。
    """
    marks = _watermarks(conn)
    if not marks:
        return [], []
    fleet_max = max(marks.values())
    chronic_cut = fleet_max - timedelta(hours=CHRONIC_LAG_HOURS)
    stale = {s: wm for s, wm in marks.items() if wm < since}
    chronic = sorted(s for s, wm in stale.items() if wm < chronic_cut)
    recent = sorted(s for s in stale if s not in set(chronic))
    return recent, chronic
