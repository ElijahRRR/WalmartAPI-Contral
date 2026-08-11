"""风控闸积木(listing L2b;所有者定稿 2026-08-07:选 A 保留提交前否决闸)。

作用:防"审核时刻与上架执行时刻之间的时间差"——禁售类目与黑名单品牌
是活数据(品牌黑名单由产品清理报错扫描+商标库比对持续追加),审核 pass
的行到提交时可能已撞新规。命中只标"未上架理由",不影响其它行。

数据权威在 PG(catalog.risk_product_types / brand_blacklist),risk_sync
从两张飞书表(wiki 承载)镜像入库;**同步只增改不删**——表格停用后
PG 侧新增走未来黑名单流程(产品中心增量脚本),误进的人工 DELETE。

拦截条件(旧系统实证原样):准入状态 == '禁售',或 中国卖家可做 以'否'
开头;品牌 casefold 精确匹配。
"""

import logging

logger = logging.getLogger("services.risk_gate")


def sync_product_types(conn, rows: list[dict]) -> int:
    """输入:连接 + 类目表行(registry.RISK_PT_SHEET 列名键)→ 输出:upsert 数。"""
    rows = [r for r in rows if r.get("product_type")]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO catalog.risk_product_types "
            "(product_type, category, ptg, admit_status, cn_seller, "
            " cert_required, note, field_total, field_required, field_list, "
            " synced_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (product_type) DO UPDATE SET "
            "category = EXCLUDED.category, ptg = EXCLUDED.ptg, "
            "admit_status = EXCLUDED.admit_status, "
            "cn_seller = EXCLUDED.cn_seller, "
            "cert_required = EXCLUDED.cert_required, note = EXCLUDED.note, "
            "field_total = EXCLUDED.field_total, "
            "field_required = EXCLUDED.field_required, "
            "field_list = EXCLUDED.field_list, synced_at = now()",
            [(r["product_type"], r.get("category"), r.get("ptg"),
              r.get("admit_status"), r.get("cn_seller"),
              r.get("cert_required"), r.get("note"), r.get("field_total"),
              r.get("field_required"), r.get("field_list")) for r in rows])
    return len(rows)


def sync_brands(conn, rows: list[dict]) -> int:
    """输入:连接 + 品牌表行({brand, source, added_date, sku})→ 输出:upsert 数。

    D 列 ASIN 一并镜像进 src_sku(总表/旧收集表都有这列,legacy_survey:1360;
    2026-08-11 之前漏存,导致 beyKyi 认领腿 D 列全空)。表格 D 列偶有空值,
    **空不覆盖已有**(coalesce 保旧)——总表整理时清掉一格不应抹掉库里的溯源。
    """
    rows = [r for r in rows if (r.get("brand") or "").strip()]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO catalog.brand_blacklist "
            "(brand_key, brand, source, added_date, src_sku, synced_at) "
            "VALUES (%s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (brand_key) DO UPDATE SET brand = EXCLUDED.brand, "
            "source = EXCLUDED.source, added_date = EXCLUDED.added_date, "
            "src_sku = coalesce(nullif(btrim(EXCLUDED.src_sku), ''), "
            "                   brand_blacklist.src_sku), "
            "synced_at = now()",
            [(r["brand"].strip().casefold(), r["brand"].strip(),
              r.get("source"), r.get("added_date"),
              (r.get("sku") or "").strip() or None) for r in rows])
    return len(rows)


def load_gate(conn) -> dict:
    """输入:连接 → 输出:{"banned_pts": set, "brands": set(casefold)}。

    上架主链每轮加载一次,逐行 check() 零查询。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT product_type, admit_status, cn_seller "
                    "FROM catalog.risk_product_types")
        banned = {pt for pt, admit, cn in cur.fetchall()
                  if (admit or "").strip() == "禁售"
                  or (cn or "").strip().startswith("否")}
        cur.execute("SELECT brand_key FROM catalog.brand_blacklist")
        brands = {r[0] for r in cur.fetchall()}
    return {"banned_pts": banned, "brands": brands}


def check(gate: dict, product_type: str | None, brand: str | None) -> str | None:
    """输入:闸门数据 + 类目 + 品牌 → 输出:拦截原因(None=放行)。"""
    pt = (product_type or "").strip()
    if pt and pt in gate["banned_pts"]:
        return f"禁售类目:{pt}"
    b = (brand or "").strip()
    if b and b.casefold() in gate["brands"]:
        return f"黑名单品牌:{b}"
    return None
