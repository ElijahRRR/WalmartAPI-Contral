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


def _int_or_none(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


# 允许的缩量比例:飞书表删几行是常态,删掉一半必是读漏/读错
_PT_META_SHRINK_TOLERANCE = 0.2


# R1 准入闸 / R3 认证闸真正读的三列。**只比这三列**:别的列(类目名、字段数、
# 备注)改了不影响任何一条判定,记进变更台账只会让"要重判多少"这个数虚高,
# 而那个数是所有者决定跑不跑的唯一依据。
_JUDGED_COLS = (("access_state", "admit_status"),
                ("zh_can_do", "cn_seller"),
                ("requirements", "cert_required"))


def _diff_pt_meta(cur, rows: list[dict]) -> list[tuple]:
    """输入:游标 + 新行 → 输出:变更台账待插入行(**只含判据真的变了的 PT**)。

    ⚠ 必须在 TRUNCATE **之前**调 —— 之后旧值就没了。

    空值归一到 `''` 再比:飞书空单元格读出来有时是 None 有时是空串,不归一的话
    每次同步都会报一堆"变了"的假阳性,而假阳性会把"要重判 M 条"这个数撑爆,
    人看两次就不看了。
    """
    def _norm(v):
        return (v or "").strip()

    cur.execute("SELECT walmart_product_type, access_state, zh_can_do, "
                "requirements FROM audit.walmart_pt_meta")
    old = {r[0]: (_norm(r[1]), _norm(r[2]), _norm(r[3])) for r in cur.fetchall()}
    out = []
    seen = set()
    for r in rows:
        pt = r["product_type"]
        seen.add(pt)
        new = tuple(_norm(r.get(src)) for _, src in _JUDGED_COLS)
        was = old.get(pt)
        if was is None:
            out.append((pt, "added", None, new[0], None, new[1], None, new[2]))
        elif was != new:
            out.append((pt, "changed", was[0], new[0], was[1], new[1],
                        was[2], new[2]))
    for pt, was in old.items():
        if pt not in seen:
            # 删行同样是判据变更:R1 的两条 pending 分支正是"PT 不在 pt_meta"
            out.append((pt, "removed", was[0], None, was[1], None, was[2], None))
    return out


def sync_pt_meta(conn, rows: list[dict]) -> tuple[int, int, list[tuple]]:
    """输入:连接 + 类目表行(同 sync_product_types 的那份)→ 输出:(入库数, 净减数, 变更行)。

    ⚠ **全量重灌,不是 upsert** —— 这正是所有者 2026-08-17 撞上的坑:
    「飞书表格里面的已废弃我已经删掉了。但是重新拉以后还是存在」。
    upsert 只增改不删,飞书删掉的行在库里永远留着。

    `audit.walmart_pt_meta` 是审核 R1 准入闸 / R3 认证闸**唯一**查的表,
    此前它是批次 A 一次性迁入的**死快照,没有任何同步链** —— 而同一张飞书表
    早就被 risk_sync 读着灌 `catalog.risk_product_types` 了。同一份数据两个
    消费方,只同步了一个,于是飞书增删对审核毫无影响。

    列一一对应(飞书 10 列 → pt_meta):category/ptg/product_type/admit_status/
    cn_seller/cert_required/note/field_total/field_required/field_list。
    `zh_seller_forbidden` 与 `raw` 两列表里没有、全仓也无人读(只在注释里被提到),
    重灌不保。

    骤缩护栏:比库里现有行数少超 20% 就拒绝重灌 —— 飞书表删几行是常态,
    删掉一半必是读漏/读错(与 `risk_sync._guard_shrink` 同款纪律)。

    ★ **顺带落变更台账**(2026-08-21):重灌前逐 PT 比对 R1/R3 真正读的三列,
    只把变了的写进 `audit.pt_meta_change_log`。这是"飞书数据变了"这条失效信号
    的落点 —— 在此之前它根本没接上:`products.audit_version` 是仓库侧的规则
    版本号,飞书改表不会让它动,于是 `rerule` / `mode=nonpass` 那两条带版本
    谓词的通道对数据变更完全无感(所有者 2026-08-21 实遇:全量扫过一遍之后
    两条通道双双报「共 0 个」)。有了台账,`product_audit -p repts=1` 就能按
    "PT 的判据在我判过之后变过"精确取候选。
    """
    rows = [r for r in rows if r.get("product_type")]
    if not rows:
        raise ValueError("类目表读到 0 行,拒绝重灌 walmart_pt_meta"
                         "(那是审核两道闸唯一查的表,清空 = 全部静默放行)")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit.walmart_pt_meta")
        before = int(cur.fetchone()[0])
        if before and len(rows) < before * (1 - _PT_META_SHRINK_TOLERANCE):
            raise ValueError(
                f"类目表只读到 {len(rows)} 行,库里现有 {before} 行,"
                f"少了 {before - len(rows)} 行(超 "
                f"{_PT_META_SHRINK_TOLERANCE:.0%})——拒绝重灌,先核实飞书表")
        changes = _diff_pt_meta(cur, rows)      # ⚠ 必须在 TRUNCATE 之前
        cur.execute("TRUNCATE audit.walmart_pt_meta")
        cur.executemany(
            "INSERT INTO audit.walmart_pt_meta "
            "(walmart_product_type, walmart_category, walmart_ptg, "
            " access_state, zh_can_do, requirements, notes, "
            " total_fields, required_count, required_fields, synced_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
            [(r["product_type"], r.get("category"), r.get("ptg"),
              r.get("admit_status"), r.get("cn_seller"),
              r.get("cert_required"), r.get("note"),
              _int_or_none(r.get("field_total")),
              _int_or_none(r.get("field_required")),
              r.get("field_list")) for r in rows])
        if changes:
            cur.executemany(
                "INSERT INTO audit.pt_meta_change_log "
                "(walmart_product_type, change_kind, before_access, "
                " after_access, before_zh, after_zh, before_req, after_req) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", changes)
    return len(rows), max(0, before - len(rows)), changes


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


def check(gate: dict, product_type: str | None, brand: str | None,
          manufacturer: str | None = None) -> str | None:
    """输入:闸门数据 + 类目 + 品牌(+制造商)→ 输出:拦截原因(None=放行)。

    品牌与制造商**两个字段都查**(旧 check_brand 双字段实证,所有者批复
    2026-08-12):亚马逊大量商品 brand=Generic/空,真品牌在 manufacturer,
    只查 brand 黑名单必漏。
    """
    pt = (product_type or "").strip()
    if pt and pt in gate["banned_pts"]:
        return f"禁售类目:{pt}"
    for label, v in (("黑名单品牌", brand), ("黑名单制造商", manufacturer)):
        b = (v or "").strip()
        if b and b.casefold() in gate["brands"]:
            return f"{label}:{b}"
    return None
