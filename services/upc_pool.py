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


def claim(conn, wants: list[dict]) -> list[str | None]:
    """输入:连接 + [{store, asin?}] → 输出:与 wants 等长的 UPC 列表(不足补 None)。

    单事务 FOR UPDATE SKIP LOCKED:并发领号互不阻塞且绝不双领。
    调用方必须在同一事务或紧随其后提交 feed;领了不用要走 release 三类路径。
    """
    if not wants:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT upc FROM catalog.upc_pool WHERE status = '' "
            "ORDER BY created_at, upc LIMIT %s FOR UPDATE SKIP LOCKED",
            (len(wants),))
        got = [r[0] for r in cur.fetchall()]
        cur.executemany(
            "UPDATE catalog.upc_pool SET status = 'claimed', store = %s, "
            "asin = %s, claimed_at = now() WHERE upc = %s",
            [(w.get("store"), w.get("asin"), u)
             for w, u in zip(wants, got)])
    if len(got) < len(wants):
        logger.warning("UPC 池余量不足:需要 %d 个,只领到 %d 个(请注入新号段)",
                       len(wants), len(got))
    return got + [None] * (len(wants) - len(got))


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
