"""占用台账积木(catalog.claims 的唯一读写出处)。

设计前提见 docs/allocation_plan.md §二/§五,三条纪律写在这里方便随时对照:

1. **占用是决策,不是观测**:只有分配与释放两个显式动作能改。任何"发现商品
   下架了就自动释放"的想法都不要写进来——那正是要避免的不稳定源。
2. **排他由 PG 保证**:`claims_active_uniq` 是部分唯一索引,并发时后到的
   INSERT 直接失败。`try_claim` 因此返回"谁占着"而不是抛错,调用方顺延。
3. **released 行永不删**:释放只改 status/released_at/released_reason,
   历史是唯一能回答"这品牌当初属于谁"的地方。

品牌键一律经 `services/brand_key`,产品键一律是标准 ASIN(`services/sku_asin`)
——键算错等于排他失效或大面积误锁,两处规则都只有唯一出处,这里不重复实现。
"""

import logging

BRAND = "brand"
PRODUCT = "product"
KINDS = (BRAND, PRODUCT)

logger = logging.getLogger("services.claims")

_INSERT = """
INSERT INTO catalog.claims
    (kind, claim_key, store, status, walmart_pt, pt_source, audit_version,
     source, note)
VALUES (%(kind)s, %(claim_key)s, %(store)s, 'active', %(walmart_pt)s,
        %(pt_source)s, %(audit_version)s, %(source)s, %(note)s)
ON CONFLICT (kind, claim_key) WHERE status = 'active' DO NOTHING
RETURNING id
"""

_OWNER = """
SELECT claim_key, store FROM catalog.claims
WHERE status = 'active' AND kind = %s AND claim_key = ANY(%s)
"""

_LOAD = """
SELECT claim_key, store FROM catalog.claims
WHERE status = 'active' AND kind = %s
"""

_RELEASE = """
UPDATE catalog.claims
   SET status = 'released', released_at = now(), released_reason = %(reason)s
 WHERE status = 'active'
   AND (%(store)s::text IS NULL OR store = %(store)s)
   AND (%(kind)s::text  IS NULL OR kind  = %(kind)s)
   AND (%(key)s::text   IS NULL OR claim_key = %(key)s)
RETURNING kind, claim_key, store
"""

_PREVIEW = """
SELECT kind, claim_key, store FROM catalog.claims
 WHERE status = 'active'
   AND (%(store)s::text IS NULL OR store = %(store)s)
   AND (%(kind)s::text  IS NULL OR kind  = %(kind)s)
   AND (%(key)s::text   IS NULL OR claim_key = %(key)s)
 ORDER BY kind, claim_key
"""

_BY_STORE = """
SELECT store, kind, count(*) FROM catalog.claims
WHERE status = 'active' GROUP BY store, kind ORDER BY store, kind
"""


def _row(kind, key, store, source, *, walmart_pt=None, pt_source=None,
         audit_version=None, note=None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"未知占用类型 {kind!r}(只有 {KINDS})")
    if not key or not store:
        raise ValueError(f"占用键与店铺都不能为空:key={key!r} store={store!r}")
    return {"kind": kind, "claim_key": key, "store": store, "source": source,
            "walmart_pt": walmart_pt, "pt_source": pt_source,
            "audit_version": audit_version, "note": note}


def try_claim(conn, kind: str, key: str, store: str, source: str, **snap) -> str | None:
    """输入:连接 + 类型/键/店铺/来源(+快照)→ 输出:占用成功返 None,
    已被占返回**现任占用店**。

    调用方语义:返回非 None 且不等于自己 = 这一步分配不成立,顺延次优店;
    返回值等于自己 = 之前已占(幂等重跑),按成功处理。
    """
    with conn.cursor() as cur:
        cur.execute(_INSERT, _row(kind, key, store, source, **snap))
        if cur.fetchone():
            return None
        cur.execute(_OWNER, (kind, [key]))
        got = cur.fetchall()
    return got[0][1] if got else None


def claim_many(conn, rows: list[dict]) -> tuple[int, list[tuple]]:
    """输入:连接 + [{kind,claim_key,store,source,...}] → 输出:(成功数, 冲突列表)。

    冲突列表元素 = (kind, key, 想占的店, 现任店)。**逐行插入**而不是
    executemany:要知道每一行成不成,批量拿不到这个粒度,而占用冲突恰恰是
    调用方必须逐行处置的东西。
    """
    ok, conflicts = 0, []
    for r in rows:
        owner = try_claim(conn, r["kind"], r["claim_key"], r["store"],
                          r.get("source", "unknown"),
                          walmart_pt=r.get("walmart_pt"),
                          pt_source=r.get("pt_source"),
                          audit_version=r.get("audit_version"),
                          note=r.get("note"))
        if owner is None or owner == r["store"]:
            ok += 1
        else:
            conflicts.append((r["kind"], r["claim_key"], r["store"], owner))
    return ok, conflicts


def owner_of(conn, kind: str, keys: list[str]) -> dict[str, str]:
    """输入:连接 + 类型 + 键列表 → 输出:{键: 占用店}(未被占的键不出现)。"""
    if not keys:
        return {}
    with conn.cursor() as cur:
        cur.execute(_OWNER, (kind, list(keys)))
        return dict(cur.fetchall())


def load_active(conn, kind: str) -> dict[str, str]:
    """输入:连接 + 类型 → 输出:全量 {键: 占用店}(闸门每轮加载一次,逐行零查询)。"""
    with conn.cursor() as cur:
        cur.execute(_LOAD, (kind,))
        return dict(cur.fetchall())


def preview_release(conn, *, store=None, kind=None, key=None) -> list[tuple]:
    """输入:连接 + 释放条件 → 输出:将被释放的 [(kind, key, store)](dry-run 用)。"""
    with conn.cursor() as cur:
        cur.execute(_PREVIEW, {"store": store, "kind": kind, "key": key})
        return cur.fetchall()


def release(conn, *, reason: str, store=None, kind=None, key=None) -> list[tuple]:
    """输入:连接 + 原因 + 条件 → 输出:实际释放的 [(kind, key, store)]。

    三个条件是**与**关系,至少给一个——全 None 会释放全库,那是事故不是功能。
    """
    if store is None and key is None:
        raise ValueError("释放必须限定 store 或 key:三个条件全空会清空整个台账")
    with conn.cursor() as cur:
        cur.execute(_RELEASE, {"store": store, "kind": kind, "key": key,
                               "reason": reason})
        return cur.fetchall()


def counts_by_store(conn) -> dict[str, dict[str, int]]:
    """输入:连接 → 输出:{店铺: {brand: n, product: n}}(审计与摘要用)。"""
    out: dict[str, dict[str, int]] = {}
    with conn.cursor() as cur:
        cur.execute(_BY_STORE)
        for store, kind, n in cur.fetchall():
            out.setdefault(store, {})[kind] = int(n)
    return out
