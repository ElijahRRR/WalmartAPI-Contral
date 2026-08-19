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
"""

import logging

logger = logging.getLogger("services.upc_pool")

_SAFE_PREFIX = "016789"     # 首位白名单(旧系统实证:2/3/4/5 开头被沃尔玛拒)

# PG 状态值 → 表格「状态」列文案
STATUS_CN = {"": "", "claimed": "已领", "used": "已用",
             "conflict": "冲突", "bad_prefix": "非法前缀"}


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
    values = feishu.sheet_values(sheet, f"A2:F{total}") if total >= 2 else []
    rows = []
    for i, raw in enumerate(values):
        cells = [(str(c).strip() if c is not None else "") for c in raw] + [""] * 6
        if cells[0]:
            rows.append({"rownum": i + 2, "upc_raw": cells[0],
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
    burn_for_retire 标 conflict,复用查询摸不到它。

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


def burn_for_retire(conn, pairs: list[tuple[str, str]]) -> int:
    """输入:[(store, asin)] → 输出:标 conflict 的行数(RETIRE 成功后旧号永久弃用)。

    自愈链清列重上要的是**新号**(SKU 已绑死旧号,不退役换号必败;退役后
    旧号也不能再给任何人用)。标成 conflict 之后,claim 的同 (店,ASIN)
    复用查询摸不到它,下一轮 list_new 自然领新号——两条语义各归其位。
    """
    if not pairs:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE catalog.upc_pool SET status = 'conflict' "
            "FROM unnest(%s::text[], %s::text[]) AS t(s, a) "
            "WHERE store = t.s AND asin = t.a "
            "  AND status IN ('claimed', 'used')",
            ([p[0] for p in pairs], [p[1] for p in pairs]))
        n = cur.rowcount
    if n:
        logger.info("RETIRE 退役烧号 %d 个(标 conflict,重上必领新号)", n)
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


def mark_conflict(conn, upc: str, asin: str | None = None) -> None:
    """输入:连接 + UPC(+尝试的 asin)→ 输出:无。全站已存在,永久弃用。"""
    with conn.cursor() as cur:
        cur.execute("UPDATE catalog.upc_pool SET status = 'conflict', "
                    "asin = COALESCE(%s, asin) WHERE upc = %s", (asin, upc))


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
