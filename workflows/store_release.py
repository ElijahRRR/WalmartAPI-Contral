"""store_release — 释放占用(整店 / 点名品牌 / 点名 ASIN)。**危险,默认 dry-run。**

用法:
  python cli.py store_release -p store=A085                 # 预览该店全部占用
  python cli.py store_release -p store=A085 --execute       # 真释放
  python cli.py store_release -p brand=vtopmart --execute   # 点名释放一个品牌
  python cli.py store_release -p asin=B08LHF7VLT --execute  # 点名释放一个产品
  python cli.py store_release -p store=A085 -p mark_offline=0 --execute
        # 只放占用、不动在线快照(默认整店释放会同步标缺席,见下)

**这是全系统唯一的释放路径**(docs/allocation_plan.md §六):没有任何代码会
自动释放占用——店铺暂停不释放、商品下架不释放、KPI 报 TERMINATED 也不释放
(那只是"可释放资格",审计告警列清单,人来跑这条命令)。理由:KPI 是外部
观测,误报触发自动释放的话,品牌被别的店占走就再也撤不回来了。

**整店释放同时校正在线快照**(§九.4):catalog_sync 只扫在册店,店铺从凭证表
删掉后它的 walmart_items 行会永久冻结成"在架",污染在线表投影、list_new
全局去重闸与 maintenance。店铺终止 = 商品事实上已全部下架,所以整店释放时
顺手把该店在架行标 missing_since——**这是校正观测不是伪造**,dry-run 会先
把行数列出来;不想动就 `-p mark_offline=0`。
点名释放(brand/asin)不碰快照:那是个别产品的归属调整,店还在正常经营。
"""

import logging
from collections import Counter

from registry import db
from services import claims

DANGEROUS = True

logger = logging.getLogger("workflows.store_release")

_MARK_OFFLINE_SQL = """
UPDATE catalog.walmart_items
   SET missing_since = now(), published_status = NULL, lifecycle_status = NULL,
       updated_at = now()
 WHERE store = %s AND missing_since IS NULL
"""

_COUNT_ONLINE_SQL = """
SELECT count(*) FROM catalog.walmart_items
WHERE store = %s AND missing_since IS NULL
"""


def _reason(params: dict, store, brand, asin) -> str:
    if params.get("reason"):
        return str(params["reason"]).strip()
    if store:
        return "store_release:整店释放"
    return f"store_release:点名释放 {'品牌 ' + brand if brand else 'ASIN ' + asin}"


def run(params: dict) -> str:
    """输入:params(store/brand/asin/reason/mark_offline/execute)→ 输出:摘要。"""
    execute = bool(params.get("execute"))
    store = (params.get("store") or "").strip() or None
    brand = (params.get("brand") or "").strip() or None
    asin = (params.get("asin") or "").strip().upper() or None
    mark_offline = str(params.get("mark_offline", "1")).lower() not in {"0", "false", "no"}

    given = [x for x in (store, brand, asin) if x]
    if len(given) != 1:
        return ("⛔ 三选一:-p store=<店铺> / -p brand=<品牌> / -p asin=<ASIN>"
                "(一次只给一个;全空会清空整个台账,不允许)")

    kind = claims.BRAND if brand else (claims.PRODUCT if asin else None)
    key = brand or asin
    if brand:
        # 品牌键必须与占用时同一套归一算法,否则大小写/空格差一点就释放不到
        from services import brand_key as bk
        key = bk.normalize(brand)
        if bk.is_placeholder(brand):
            return (f"⛔ 「{brand}」是占位符(无品牌),按设计它从不占用品牌,"
                    f"没有可释放的行;要放产品用 -p asin=")
    reason = _reason(params, store, brand, asin)

    with db.pg_conn() as conn:
        rows = claims.preview_release(conn, store=store, kind=kind, key=key)
        online = 0
        if store and mark_offline:
            with conn.cursor() as cur:
                cur.execute(_COUNT_ONLINE_SQL, (store,))
                online = int(cur.fetchone()[0])

        by_kind = Counter(k for k, _, _ in rows)
        head = (f"{'整店 ' + store if store else ('品牌 ' + key if brand else 'ASIN ' + key)}:"
                f"active 占用 {len(rows)} 条"
                + (f"(品牌 {by_kind.get(claims.BRAND, 0)} / 产品 "
                   f"{by_kind.get(claims.PRODUCT, 0)})" if rows else ""))
        sample = "; ".join(f"{k}:{v}→{s}" for k, v, s in rows[:15])

        if not execute:
            lines = [f"🧪 将释放 {head}"]
            if sample:
                lines.append(f"   样例:{sample}" + (" …" if len(rows) > 15 else ""))
            if store and mark_offline:
                lines.append(f"   同时把该店 {online} 行在架商品标缺席"
                             f"(校正观测:店终止后商品事实上已下架;"
                             f"-p mark_offline=0 可只放占用不动快照)")
            if not rows and not online:
                lines.append("   本次无任何可释放的行——占用台账里没有它,"
                             "或已经释放过了(released 行不重复释放)")
            lines.append("   确认后加 --execute")
            return "\n".join(lines)

        freed = claims.release(conn, reason=reason, store=store, kind=kind, key=key)
        marked = 0
        if store and mark_offline:
            with conn.cursor() as cur:
                cur.execute(_MARK_OFFLINE_SQL, (store,))
                marked = cur.rowcount or 0

    logger.warning("store_release 已执行:%s,释放 %d 条,标缺席 %d 行,原因=%s",
                   store or key, len(freed), marked, reason)
    return (f"✅ 已释放 {head.replace('active 占用', '实际释放')}"
            + (f";该店 {marked} 行在架商品已标缺席" if marked else "")
            + f";原因={reason}"
            + ";released 行永久保留(回答『当初属于谁』只能靠它)")
