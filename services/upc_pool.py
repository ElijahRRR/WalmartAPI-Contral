"""UPC 池积木(catalog.upc_pool;listing L2a,所有者定稿 2026-08-07)。

PG 权威;飞书「UPC池」表(registry.UPC_SHEET)= 运营注入口 + 人看的投影:
运营填 A=UPC B=放入日期注入,脚本 sync_from_sheet 拉新号入库(首位白名单
016789 校验,2/3/4/5 开头是 GS1 特殊用途段,沃尔玛拒收,标 bad_prefix
永不分配),project_to_sheet 回写 C~F。

状态机(旧系统实证语义照搬,实现按新架构重写——旧代码的文件锁/本地
声明簿/server 集中分配三层并发补丁全部消灭,领号 = 单事务
SELECT … FOR UPDATE SKIP LOCKED,数据库层面杜绝双领):
  ''(未用)→ claimed(已领:分配给某次上架但 feed 未提交)
           → used(已用:feed 已提交,永久消耗)
  回收(release)仅三类调用路径:提交前失败 / 反查双确认未达 / 4xx 被拒;
  **Unknown(结局不确定)永不回收**——沃尔玛可能已收单,回收再分配
  = 同 UPC 双上架(旧系统生死规则)。
  conflict(全站已存在该 UPC)/ bad_prefix 永久弃用。
  burned_delete / burned_lock(2026-09-02,SKU 改造批次 0a 登记,批次 2 接线):
  **主动烧号** —— 码与 UPC 同寿命,弃码时把该 (店, ASIN) 名下的号一起烧掉
  (delete=DELETE 经观测核验;lock=SKU_LOCKED 自愈退役)。与 conflict 的分工:
  **conflict 只表示「全站已存在该 UPC」(撞库)**,主动烧号不复用这个语义,否则
  池表投影与 pool_stats 里永远分不清「这号是撞库废的」还是「我们主动烧的」。
  三个值的**唯一写入函数是 `burn(conn, pairs, status)`**,唯一调用方是
  services/sku_codec.abandon(弃码点三个原因各配一个状态);别处不许再写状态值。
"""

import logging

logger = logging.getLogger("services.upc_pool")

_SAFE_PREFIX = "016789"     # 首位白名单(旧系统实证:2/3/4/5 开头被沃尔玛拒)

# 弃码烧号的三个状态值(0a 登记两个,批次 2 接线并补 conflict 一项)。
# burned_* 不复用 conflict:那个值的语义是「撞库」,两件事混在一个值里就再也分不开;
# 反过来撞库弃码烧的号仍写 conflict,因为它本来就是撞库。
# ⚠ 2026-09-02 批次 2 删掉了 `mark_conflict(conn, upc, asin)`:它是按**单个 UPC**
# 写 conflict 的第二条写入路径,唯一调用方(listing_sheet 的撞库处置)本批已改走
# sku_codec.abandon → burn;留着就是「同一个能力两条实现路径」,而误用它的后果正是
# 本批要消灭的死循环(烧了号没弃码 ⇒ 同码换号重发 ⇒ 0101211)。
BURN_DELETE = "burned_delete"   # DELETE 经观测核验后弃码,同时烧号
BURN_LOCK = "burned_lock"       # SKU_LOCKED 自愈退役后弃码,同时烧号
#: 撞库(全站已存在该 UPC)。这个值**语义在先**:0101119 说的就是"号被占了",
#: 所以 upc_conflict 弃码烧的号继续用它,而不是再造第四个字样。
CONFLICT = "conflict"

# PG 状态值 → 表格「状态」列文案
STATUS_CN = {"": "", "claimed": "已领", "used": "已用",
             CONFLICT: "冲突", "bad_prefix": "非法前缀",
             BURN_DELETE: "删除烧号", BURN_LOCK: "锁死烧号"}


def normalize(upc) -> str:
    """输入:任意形态的 UPC → 输出:规范化 12 位(纯数字 zfill;非数字返空串)。"""
    v = "".join(ch for ch in str(upc or "").strip() if ch.isdigit())
    if not v or len(v) > 12:
        return v if len(v) <= 14 else ""
    return v.zfill(12)


def is_safe_prefix(upc: str) -> bool:
    return bool(upc) and upc[0] in _SAFE_PREFIX


def sync_rows(conn, sheet_rows: list[tuple[str, str]]) -> tuple[int, int]:
    """输入:连接 + 表格 [(UPC 原文, 放入日期)] → 输出:(新入库数, 非法前缀数)。

    幂等:已存在的 UPC 跳过(ON CONFLICT DO NOTHING,不覆盖状态)。
    """
    new_ok, new_bad = [], []
    for raw, put_date in sheet_rows:
        u = normalize(raw)
        if not u:
            continue
        if is_safe_prefix(u):
            new_ok.append((u, "", put_date))
        else:
            new_bad.append((u, "bad_prefix", put_date))
    rows = new_ok + new_bad
    if not rows:
        return 0, 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO catalog.upc_pool (upc, status, put_date) "
            "VALUES (%s, %s, %s) ON CONFLICT (upc) DO NOTHING", rows)
    if new_bad:
        logger.warning("UPC 注入含 %d 个非法前缀(首位须在 %s),已标 bad_prefix "
                       "永不分配,样本=%s", len(new_bad), _SAFE_PREFIX,
                       [u for u, _, _ in new_bad[:5]])
    return len(new_ok), len(new_bad)


def sync_from_sheet(conn) -> dict:
    """输入:连接 → 输出:{rows: 表格行, new: 新入库数, bad: 非法前缀数}。

    ⚠ **住在 services 而不是 upc_sync 工作流里**(2026-08-16,所有者定稿
    「上架运行时自动同步一次 UPC,然后再走上架流程」):`list_new` 也要用它,
    而铁律 1 禁止 workflow 互相 import。收在这里,两个调用方注入的是同一段
    代码 —— 各写一份迟早飘成"上架看到的池"与"upc_sync 看到的池"不是一个。

    只做**注入**(表格新号 → catalog.upc_pool),不回写状态列:回写是展示,
    归 `project_to_sheet` / upc_sync;上架链要的只是"运营刚贴进表格的号
    这一轮就能用"。

    表未登记抛 LookupError,由调用方决定是致命还是跳过。
    """
    from api import feishu
    from registry import resources

    sheet = resources.UPC_SHEET
    sheet.require()
    total = feishu.sheet_row_count(sheet)
    # 上界随表长增长 ⇒ 走唯一标准读通道;rownum 取通道返回值(project_to_sheet
    # 拿它拼 C{rownum}:F{rownum} 回写,手算偏移错一格就写到别人的号上)
    pairs = (feishu.sheet_values_rows(sheet, "A", "F", 2, total)
             if total >= 2 else [])
    rows = []
    for rownum, raw in pairs:
        cells = [(str(c).strip() if c is not None else "") for c in raw] + [""] * 6
        if cells[0]:
            rows.append({"rownum": rownum, "upc_raw": cells[0],
                         "put_date": cells[1], "shown": cells[2:6]})
    if not rows:
        return {"rows": [], "new": 0, "bad": 0}
    n_new, n_bad = sync_rows(conn, [(r["upc_raw"], r["put_date"]) for r in rows])
    return {"rows": rows, "new": n_new, "bad": n_bad}


def project_to_sheet(conn, rows: list[dict]) -> int:
    """输入:连接 + sync_from_sheet 的表格行 → 输出:回写行数(仅差异行)。

    PG 是权威,表格 C~F(状态/店铺/SKU/上架日期)是投影。只写值变了的行 ——
    全量重写一是慢,二是把人正在看的表整片刷掉。
    """
    from api import feishu
    from registry import resources

    info = lookup(conn, [normalize(r["upc_raw"]) for r in rows])
    updates = []
    for r in rows:
        st = info.get(normalize(r["upc_raw"]))
        if st is None:
            continue
        status, store, sku, used_at = st
        vals = [STATUS_CN.get(status, status), store or "", sku or "",
                used_at.strftime("%Y-%m-%d") if used_at else ""]
        if vals != r["shown"]:
            updates.append((f"C{r['rownum']}:F{r['rownum']}", [vals]))
    return feishu.sheet_write_ranges(resources.UPC_SHEET, updates) if updates else 0


def claim(conn, wants: list[dict]) -> list[str | None]:
    """输入:连接 + [{store, asin?}] → 输出:与 wants 等长的 UPC 列表(不足补 None)。

    **先复用后新领**(2026-08-19 生产实证 ERR_EXT_DATA_0101211):同
    (store, asin) 名下已有 claimed/used 的号必须原号复用——SKU 在沃尔玛端
    已绑死首次提交的 UPC,O=FAILED 重试换新号重发同一 SKU 必败(旧仓
    legacy_survey:1667 同款死亡路径),而且每次重试白烧一个新号。
    多个旧号时取**最早领的**(最可能是沃尔玛端建 SKU 时绑的那个)。
    自愈链要"领新号"的场景不受影响:RETIRE 成功清列时旧号已被
    `burn(..., BURN_LOCK)` 烧掉(由 sku_codec.abandon 调),复用查询只看
    claimed/used,摸不到它。

    新领仍是单事务 FOR UPDATE SKIP LOCKED:并发领号互不阻塞且绝不双领。
    调用方必须在同一事务或紧随其后提交 feed;领了不用要走 release 三类路径。
    """
    if not wants:
        return []
    reuse: dict[int, str] = {}
    with conn.cursor() as cur:
        pairs = [(w.get("store"), w.get("asin")) for w in wants]
        cur.execute(
            "SELECT DISTINCT ON (store, asin) store, asin, upc "
            "FROM catalog.upc_pool "
            "JOIN unnest(%s::text[], %s::text[]) AS t(s, a) "
            "  ON store = t.s AND asin = t.a "
            "WHERE status IN ('claimed', 'used') "
            "ORDER BY store, asin, claimed_at NULLS LAST, upc",
            ([p[0] for p in pairs], [p[1] for p in pairs]))
        by_pair = {(s, a): u for s, a, u in cur.fetchall()}
        for i, w in enumerate(wants):
            u = by_pair.get((w.get("store"), w.get("asin")))
            if u:
                reuse[i] = u
        fresh_idx = [i for i in range(len(wants)) if i not in reuse]
        got: list[str] = []
        if fresh_idx:
            cur.execute(
                "SELECT upc FROM catalog.upc_pool WHERE status = '' "
                "ORDER BY created_at, upc LIMIT %s FOR UPDATE SKIP LOCKED",
                (len(fresh_idx),))
            got = [r[0] for r in cur.fetchall()]
            cur.executemany(
                "UPDATE catalog.upc_pool SET status = 'claimed', store = %s, "
                "asin = %s, claimed_at = now() WHERE upc = %s",
                [(wants[i].get("store"), wants[i].get("asin"), u)
                 for i, u in zip(fresh_idx, got)])
    if reuse:
        logger.info("UPC 原号复用 %d 个(同店同 ASIN 领过号,重试不换号):%s",
                    len(reuse), list(reuse.values())[:5])
    if len(got) < len(fresh_idx):
        logger.warning("UPC 池余量不足:需要 %d 个,只领到 %d 个(请注入新号段)",
                       len(fresh_idx), len(got))
    out: list[str | None] = [None] * len(wants)
    for i, u in reuse.items():
        out[i] = u
    for i, u in zip(fresh_idx, got):
        out[i] = u
    return out


def burn(conn, pairs: list[tuple[str, str]], status: str) -> int:
    """输入:连接 + [(store, asin)] + 烧号状态 → 输出:烧掉的行数(永久弃用)。

    **烧号只有这一条实现路径**(批次 2 决策 D:旧 `burn_for_retire` 已删,不留
    薄封装 —— 两个名字就是两条路径,总有人调到写死 conflict 的那个)。唯一
    调用方是 `services/sku_codec.abandon`,状态由那里的弃码原因分派表给:
      弃码原因 delete_verified → BURN_DELETE   (DELETE 经观测核验)
      弃码原因 sku_locked      → BURN_LOCK     (SKU_LOCKED 自愈退役)
      弃码原因 upc_conflict    → CONFLICT      (撞库:号确实被别人占了)

    烧掉之后 `claim` 的同 (店,ASIN) 复用查询(status IN ('claimed','used'))
    摸不到它,下一轮 list_new 自然领新号 —— "清列重上领新号"靠的就是这一步,
    不烧就会被原号复用回来。

    非法状态直接抛(与 `release` 的三类原因同款 fail loud):随手传一个新字样
    进来,池表投影与 pool_stats 里就多出一支没人认识的状态,而且不报错。
    """
    if status not in (BURN_DELETE, BURN_LOCK, CONFLICT):
        raise ValueError(
            f"非法烧号状态 {status!r}:只有 {BURN_DELETE} / {BURN_LOCK} / "
            f"{CONFLICT}(取值登记在本模块顶部,新增先在那里登记)")
    if not pairs:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE catalog.upc_pool SET status = %s "
            "FROM unnest(%s::text[], %s::text[]) AS t(s, a) "
            "WHERE store = t.s AND asin = t.a "
            "  AND status IN ('claimed', 'used')",
            (status, [p[0] for p in pairs], [p[1] for p in pairs]))
        n = cur.rowcount
    if n:
        logger.info("弃码烧号 %d 个(状态=%s,重上必领新号)", n, status)
    return n


def mark_used(conn, pairs: list[tuple[str, str]]) -> int:
    """输入:连接 + [(upc, sku)] → 输出:更新数。feed 已提交,永久消耗。"""
    if not pairs:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE catalog.upc_pool SET status = 'used', sku = %s, "
            "used_at = now() WHERE upc = %s",
            [(sku, upc) for upc, sku in pairs])
    return len(pairs)


def retag_sku(conn, triples: list[tuple[str, str, str]]) -> int:
    """输入:连接 + [(店, ASIN, 新 SKU)] → 输出:改标行数(改码后号还是那个号,
    只是它现在挂在新 SKU 名下)。

    `sku` 列的语义是「这个号现在被哪个沃尔玛 SKU 占着」(schema.sql 的列注)。
    改码后不改它,列里存的就是一个**已经不存在于沃尔玛的串**;而
    listing_sheet._mark_upc_conflicts 与 UPC 池表投影都按它反查 —— 反查不到就是
    撞库标不上、运营在表上看到的归属是错的,**而且不报错**。

    **不复用 mark_used**(那是"新消耗"):mark_used 会把 status 推成 'used' 并刷
    used_at;改码不是一次新消耗,时间戳不该被改写。本函数只动 sku 一列,
    asin(领号复用键)/ status / used_at 一律不动。
    状态条件 `status IN ('claimed','used')` 与 burn 逐字一致 —— 两个改池表的出口
    口径不一致本身就是漂移源。
    """
    if not triples:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE catalog.upc_pool SET sku = t.new_sku "
            "FROM unnest(%s::text[], %s::text[], %s::text[]) AS t(s, a, new_sku) "
            "WHERE store = t.s AND asin = t.a "
            "  AND status IN ('claimed', 'used')",
            ([t[0] for t in triples], [t[1] for t in triples],
             [t[2] for t in triples]))
        n = cur.rowcount
    logger.info("UPC 改标 %d 个(改码:号不动,只换挂在它名下的 SKU)", n)
    return n


def release(conn, upcs: list[str], reason: str) -> int:
    """输入:连接 + UPC 列表 + 回收原因 → 输出:回收数(claimed → 未用)。

    仅三类合法原因:prep_failed(提交前失败)/ not_found(双确认未达)/
    rejected(4xx 被拒)。Unknown 永不回收——调用方不得为其它情形调本函数。
    """
    assert reason in ("prep_failed", "not_found", "rejected"), \
        f"非法回收原因: {reason}(Unknown 永不回收是生死规则)"
    if not upcs:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE catalog.upc_pool SET status = '', store = NULL, "
            "asin = NULL, claimed_at = NULL "
            "WHERE upc = ANY(%s) AND status = 'claimed'", (list(upcs),))
        n = cur.rowcount
    logger.info("UPC 回收 %d 个(原因=%s)", n, reason)
    return n


def pool_stats(conn) -> dict[str, int]:
    """输入:连接 → 输出:{状态: 数量}(含 ''=未用)。"""
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM catalog.upc_pool GROUP BY status")
        return {s: int(n) for s, n in cur.fetchall()}


def lookup(conn, upcs: list[str]) -> dict[str, tuple]:
    """输入:连接 + UPC 列表 → 输出:{upc: (status, store, sku, used_at)}。"""
    if not upcs:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT upc, status, store, sku, used_at "
                    "FROM catalog.upc_pool WHERE upc = ANY(%s)", (list(upcs),))
        return {u: (st, store, sku, used) for u, st, store, sku, used
                in cur.fetchall()}
