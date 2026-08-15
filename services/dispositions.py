"""处置建议台账(ops.dispositions)的读写积木 —— 批次 E 的"建议/执行"分界面。

problem_scan(只读,产建议)与 problem_product_cleanup(危险,消费建议)两个
工作流共用本模块。**必须落在 services**:铁律 1 规定任何层不准 import
workflows,两个工作流之间不能互相取用。

状态机(与 refdata/schema.sql 的 ops.dispositions 头注一致):
    suggested → executing → confirmed / ineffective

四个函数各管一段,谁也不越界:
    suggest_many()   扫描件写建议(幂等:同 (店铺,SKU,动作) 已有未落定行则刷新)
    claim()          执行件领取待执行建议(只读,不改状态——提交成功才改)
    mark_executing() 提交成功后落 feed_id 并转 executing
    settle()         按观测事件把 executing 判成 confirmed / ineffective

⚠ **生效判定不在本模块实现**。settle() 读的是 catalog.product_events 里
catalog_sync 经 services/product_events.verify_deletions 落的
delete_verified / delete_not_effective ——"不信回执信观测"那套规则(含 48h
宽限期、RETIRED/缺席算 gone)已经在跑,这里再写一份判定只会产生两份会漂移的
真相。本模块只做"把已有判决登记到建议行上"。
"""

import logging

logger = logging.getLogger("services.dispositions")

# 动作取值(与 problem_product_cleanup 的三桶一一对应)
ACTIONS = ("relist", "delete", "retire")
# 来源(tro 是预留:侵权投诉链将来也走同一张建议表)
SOURCES = ("scan", "audit", "tro")
OPEN_STATUSES = ("suggested", "executing")

# 幂等写:同 (店铺,SKU,动作) 已有未落定行 → 刷新依据与时间,不新增
# (扫描件按调度反复跑,每轮堆一行会让建议表变成流水账)。
# ⚠ 只更新 suggested 行:**executing 行绝不能被覆盖**——它已经提交了 feed,
# 把它的 suggested_at 刷新会让"等观测多久了"失真,更严重的是若同轮把
# feed_id 洗掉,这条提交就永远等不到落定判决了。
_UPSERT_SQL = """
INSERT INTO ops.dispositions
    (store, sku, asin, source, action, category, reason, detail)
VALUES (%(store)s, %(sku)s, %(asin)s, %(source)s, %(action)s,
        %(category)s, %(reason)s, %(detail)s::jsonb)
ON CONFLICT (store, sku, action) WHERE status IN ('suggested', 'executing')
DO UPDATE SET category = EXCLUDED.category,
              reason = EXCLUDED.reason,
              asin = COALESCE(EXCLUDED.asin, ops.dispositions.asin),
              detail = EXCLUDED.detail,
              suggested_at = now()
WHERE ops.dispositions.status = 'suggested'
"""

_CLAIM_SQL = """
SELECT id, store, sku, asin, action, category, reason, detail
FROM ops.dispositions
WHERE status = 'suggested'
ORDER BY store, action, suggested_at
"""

_MARK_SQL = """
UPDATE ops.dispositions
SET status = 'executing', feed_id = %(feed_id)s, executed_at = now()
WHERE id = ANY(%(ids)s) AND status = 'suggested'
"""

# 观测判决登记:executing 行 × 提交之后落的核验事件。
# gone 侧(delete_verified)= 生效;still 侧(delete_not_effective)= 没生效。
# 反补(relist)的生效信号不同:商品重新 PUBLISHED —— 直接看 walmart_items
# 现状,不看事件(反补没有对应的核验事件流)。
_SETTLE_DELETE_SQL = """
UPDATE ops.dispositions d
SET status = CASE WHEN e.event = 'delete_verified'
                  THEN 'confirmed' ELSE 'ineffective' END,
    settled_at = now(),
    detail = d.detail || jsonb_build_object('settled_by', e.event)
FROM (
    SELECT DISTINCT ON (store, sku) store, sku, event, occurred_at
    FROM catalog.product_events
    WHERE event IN ('delete_verified', 'delete_not_effective')
    ORDER BY store, sku, occurred_at DESC
) e
WHERE d.status = 'executing' AND d.action IN ('delete', 'retire')
  AND d.store = e.store AND d.sku = e.sku
  AND e.occurred_at > d.executed_at
RETURNING d.status
"""

# 反补生效 = 该 SKU 已不在问题清单里(published_status 回到正常或已缺席);
# 仍在问题清单 = 没生效。**必须等 catalog_sync 重新观测过**
# (last_seen_at > executed_at),否则拿提交前的旧快照判,永远判成"没生效"。
_SETTLE_RELIST_SQL = """
UPDATE ops.dispositions d
SET status = CASE
        WHEN w.sku IS NULL OR w.missing_since IS NOT NULL
             OR w.published_status NOT IN ('UNPUBLISHED', 'SYSTEM_PROBLEM')
        THEN 'confirmed' ELSE 'ineffective' END,
    settled_at = now(),
    detail = d.detail || jsonb_build_object(
        'settled_by', coalesce(w.published_status, 'absent'))
FROM catalog.walmart_items w
WHERE d.status = 'executing' AND d.action = 'relist'
  AND w.store = d.store AND w.sku = d.sku
  AND w.last_seen_at > d.executed_at
RETURNING d.status
"""


def suggest_many(conn, rows: list[dict]) -> int:
    """输入:连接 + 建议行(store/sku/action 必填)→ 输出:写入行数。幂等。

    detail 传 dict,本函数负责序列化;asin/category/reason 可缺省。
    非法 action/source 直接抛 —— 拼错一个字符串会静默落一批永远没人领的
    建议行(执行件按 action 分桶,不认识的桶不会被消费),宁炸不吞。
    """
    import json
    if not rows:
        return 0
    payload = []
    for r in rows:
        if r["action"] not in ACTIONS:
            raise ValueError(f"未知 action={r['action']!r}(可用:{ACTIONS})")
        src = r.get("source", "scan")
        if src not in SOURCES:
            raise ValueError(f"未知 source={src!r}(可用:{SOURCES})")
        payload.append({
            "store": r["store"], "sku": r["sku"], "asin": r.get("asin"),
            "source": src, "action": r["action"],
            "category": r.get("category"), "reason": (r.get("reason") or "")[:500],
            "detail": json.dumps(r.get("detail") or {}, ensure_ascii=False),
        })
    with conn.cursor() as cur:
        # ⚠ 报**实际落库行数**,不是 len(payload)。两者会差:同 (店铺,SKU,动作)
        # 被两个来源同时命中时,部分唯一索引把它们合成一条。首版报 len(payload)
        # ——生产实测扫描件说"落账 521 条"、执行件只领到 472 条,对不上账。
        # executemany 的 rowcount 在 psycopg3 里是各次之和,正是我们要的。
        cur.executemany(_UPSERT_SQL, payload)
        return cur.rowcount if cur.rowcount is not None else len(payload)


# ⚠ 用**多参数 unnest + NOT EXISTS**,不要写成
#   `(store, sku, action) <> ALL(%(keep)s::text[][])`
# 那样是错的:左边是 record 类型,右边二维数组的元素类型是 text,PG 直接报
# 类型不匹配。(2026-08-14 首版就这么写的,靠复查发现——当时的测试只断言 SQL
# **文本**,跑不到类型检查,全绿也没用。)
# 三个平行数组 + unnest(a,b,c) 是 PG 的标准写法,三列一一对位。
#
# ⚠ **同一段 SQL 因为类型问题炸过两次**(2026-08-14),两次都是测试全绿才在
# 生产上炸的 —— 本仓的 SQL 用例只断言**文本子串**,PG 的类型推断根本跑不到。
#   第一次:`record <> ALL(text[][])` 类型不匹配;
#   第二次:`%(store)s IS NULL OR d.store = %(store)s` —— 参数只出现在
#           IS NULL 与一次比较里,PG 推不出它的类型,报
#           "could not determine data type of parameter"。**必须显式 ::text**。
# 结论不是"以后小心点",是:**这类 SQL 的唯一验证手段是连库跑一次**。
# 改动本段后别信 pytest 绿,去 dry-run。
_WITHDRAW_SQL = """
UPDATE ops.dispositions d
SET status = 'withdrawn', settled_at = now(),
    detail = d.detail || jsonb_build_object('withdrawn_reason', %(why)s)
WHERE d.status = 'suggested' AND d.source = %(source)s
  AND (%(store)s::text IS NULL OR d.store = %(store)s::text)
  AND NOT EXISTS (
      SELECT 1 FROM unnest(%(stores)s::text[], %(skus)s::text[],
                           %(actions)s::text[]) AS k(store, sku, action)
      WHERE k.store = d.store AND k.sku = d.sku AND k.action = d.action)
RETURNING d.id
"""


def withdraw_stale(conn, source: str, keep: list[tuple], why: str,
                   store: str | None = None) -> int:
    """输入:连接 + 来源 + 本轮仍建议的 (店铺,SKU,动作) + 扫描范围 → 输出:撤销行数。

    **建议是有时效的**:今天建议删 A,明天 A 自己恢复正常了、扫描件不再建议它
    —— 但昨天那条 suggested 行还挂着,执行件照样会删。这个函数把"本轮不再
    建议、但还挂着 suggested"的行置 withdrawn。

    只动**本来源**的行(source 参数):扫描件那一轮不该碰审核来源的建议,
    反之亦然 —— 两个来源各跑各的闸,互相看不见对方为什么建议。

    executing 及已落定的行一根手指都不碰:那些已经提交出去了,撤销无意义
    (feed 已经在沃尔玛队列里),它们的归宿是 settle() 按观测判决。

    ⚠ **store 参数是必需的安全边界,不是可选优化**(2026-08-14 加):
    撤销的判据是"不在本轮 keep 清单里",而扫描件支持 `-p store=X` 只扫一个店
    —— 那一轮的 keep 里只有该店的行,不限范围就会把**其余全部店铺**的待执行
    建议一次清空。调用方扫了哪个范围就必须传哪个范围;全量扫传 None。
    """
    with conn.cursor() as cur:
        if not keep:
            # 本轮一条都不建议 = 该来源(该范围内)的 suggested 全撤
            cur.execute(
                "UPDATE ops.dispositions SET status = 'withdrawn', "
                "settled_at = now() WHERE status = 'suggested' "
                "AND source = %(source)s "
                "AND (%(store)s::text IS NULL OR store = %(store)s::text)",
                {"source": source, "store": store})
            return cur.rowcount or 0
        cur.execute(_WITHDRAW_SQL, {
            "source": source, "why": why, "store": store,
            "stores": [k[0] for k in keep],
            "skus": [k[1] for k in keep],
            "actions": [k[2] for k in keep]})
        return len(cur.fetchall())


def claim(conn) -> list[dict]:
    """输入:连接 → 输出:全部 suggested 建议行(dict 列表)。**只读,不改状态**。

    领取与转态分开是有意的:提交 feed 可能失败、可能被在途防重拦下,只有
    真提交成功(拿到 feed_id)才该转 executing。先转态再提交 = 提交失败的行
    卡在 executing 永远等不到判决,而下轮扫描因部分唯一索引还建不出新建议。
    """
    with conn.cursor() as cur:
        cur.execute(_CLAIM_SQL)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def mark_executing(conn, ids: list[int], feed_id) -> int:
    """输入:连接 + 建议行 id 列表 + feed_id → 输出:转态行数。"""
    if not ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(_MARK_SQL, {"ids": list(ids), "feed_id": str(feed_id or "")})
        return cur.rowcount


def settle(conn) -> dict:
    """输入:连接 → 输出:{confirmed: n, ineffective: n}(本轮落定的建议行)。

    只登记**已有**的观测判决,不自己判生效(见模块头注)。还没等到
    catalog_sync 重新观测的行保持 executing,不落判 —— 与
    product_events.verify_deletions 的 'wait' 语义对齐。
    """
    out = {"confirmed": 0, "ineffective": 0}
    with conn.cursor() as cur:
        for sql in (_SETTLE_DELETE_SQL, _SETTLE_RELIST_SQL):
            cur.execute(sql)
            for (st,) in cur.fetchall():
                out[st] = out.get(st, 0) + 1
    if out["ineffective"]:
        logger.warning("处置建议落定:%d 条**未生效**(回执成功但观测显示没动)"
                       "——下轮扫描会重新建议", out["ineffective"])
    return out
