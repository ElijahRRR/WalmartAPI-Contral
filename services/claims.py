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


def claim_many(conn, rows: list[dict]) -> tuple[int, list[tuple], list[dict]]:
    """输入:连接 + [{kind,claim_key,store,source,...}] → 输出:(成功数, 冲突, **真落库行**)。

    冲突列表元素 = (kind, key, 想占的店, 现任店)。**逐行插入**而不是
    executemany:要知道每一行成不成,批量拿不到这个粒度,而占用冲突恰恰是
    调用方必须逐行处置的东西。

    ⚠ 三个返回值里 `ok` 与 `landed` 是**两个数**,别互相替:
      · `ok`     = 成功数,含**幂等重跑**(这一行早就占在自己名下,本轮什么
                   都没写)。摘要一直报这个数,口径不动。
      · `landed` = 本轮 INSERT ... RETURNING 真拿到 id 的那些行。要往店铺
                   事件账本记"这一轮新占了多少"只能用它 —— 用 `ok` 的话,
                   `alloc_backfill` 每天重跑同一批(它本来就设计成幂等),
                   账本上就会天天多出一条"新占 3000 条"的假事件。
    """
    ok, conflicts, landed = 0, [], []
    for r in rows:
        owner = try_claim(conn, r["kind"], r["claim_key"], r["store"],
                          r.get("source", "unknown"),
                          walmart_pt=r.get("walmart_pt"),
                          pt_source=r.get("pt_source"),
                          audit_version=r.get("audit_version"),
                          note=r.get("note"))
        if owner is None:
            ok += 1
            landed.append(r)
        elif owner == r["store"]:
            ok += 1                     # 幂等重跑:算成功,但**本轮没落行**
        else:
            conflicts.append((r["kind"], r["claim_key"], r["store"], owner))
    return ok, conflicts, landed


# ── 事件账本用的按店汇总(ops.store_events 治理类)─────────────────────────
#
# 为什么在这里而不是在 store_events:kind 分桶是**占用台账的语义**
# (BRAND/PRODUCT 两个常量的唯一出处就在本文件);store_events 是只追加的
# 账本原语,不该知道占用有几种类型。
#
# ⚠ **detail 绝不复制 claims 表的内容**:每一条占用/释放本身在 catalog.claims
# 里都有行(released 行永不删),复制过来的那份迟早与台账漂。事件只回答
# "这一轮这家店动了多少条、为什么",`sample` 只为让人一眼认出是哪一批。

_SAMPLE = 10


def _summary(triples) -> dict[str, dict]:
    """输入:(kind, 占用键, 店铺) 三元组序列 → 输出:{店: {brand,product,sample}}。"""
    out: dict[str, dict] = {}
    for kind, key, store in triples:
        d = out.setdefault(store, {BRAND: 0, PRODUCT: 0, "sample": []})
        d[kind] = d.get(kind, 0) + 1
        if len(d["sample"]) < _SAMPLE:
            d["sample"].append(f"{kind}:{key}")
    return out


def claim_created_rows(landed: list[dict], source: str) -> list[dict]:
    """输入:`claim_many` 的真落库行 + 来源 → 输出:每店一条 claim_created 事件行。

    severity 一律 info:占用是**计划层**的正常动作,每天都有;它要回答的是
    事后"这个品牌当初什么时候归的这家店",不是要叫醒谁。
    """
    from services import store_events as se     # 局部导入:账本是叶子,不反向依赖
    summary = _summary((r["kind"], r["claim_key"], r["store"]) for r in landed)
    return [{"store": st, "event": se.CLAIM_CREATED, "severity": "info",
             "source": source, "detail": d} for st, d in sorted(summary.items())]


def released_rows(freed: list[tuple], *, source: str, scope: str, reason: str,
                  marked: dict[str, int] | None = None) -> list[dict]:
    """输入:`release` 返回的 [(kind,key,store)] + 本次范围 → 输出:每店一条事件行。

    scope 三种,severity 两档(2026-08-30 定档):
      · `store` 整店释放 = **high**:整店释放通常紧跟"这家店没了"——它是
        店铺一生里最重的一笔,而且下一轮分配就会把这些品牌发给别的店,
        那一步**不可逆**(store_release 的 dry-run 原话)。
      · `named`(点名品牌/ASIN)/ `csv`(批量归属调整)= **mid**:货还在架上,
        店还在正常经营,是个别归属调整。
    `marked` = 整店释放顺手标缺席的行数(按店)。它不是 claims 的内容,是
    **同一个动作的另一半后果**(在线快照校正),不带上的话事后看事件会以为
    只放了占用而没动商品。
    """
    from services import store_events as se
    summary = _summary(freed)
    sev = "high" if scope == "store" else "mid"
    out = []
    for st, d in sorted(summary.items()):
        d = {**d, "scope": scope, "reason": reason}
        if marked is not None and st in marked:
            d["marked_offline"] = marked[st]
        out.append({"store": st, "event": se.CLAIM_RELEASED, "severity": sev,
                    "source": source, "detail": d})
    return out


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
