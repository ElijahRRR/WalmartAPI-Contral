"""variant_probe — 变体维度为什么没发?(只读排查,不写任何东西)。

用法:
  python cli.py variant_probe -p asins=B0BWMVQHVJ,B0BWMV956C
  python cli.py variant_probe -p pt="Gift Bags"     # 只看某 PT 的变体维度枚举

为什么要有这条:`list_new --dry-run` 只能告诉你"维度 size_name 映不上未发",
说不出**映不上的是哪一头** —— 是采集侧没给这个维度、是 `_DIM_MAP` 没登记、
还是这个 PT 的 `variantAttributeNames` 枚举里压根没有 size 这一族名字。
三种成因的修法完全不同(补采 / 加映射行 / 换个维度当分组键),猜错就白改。

本工作流把三头摊开:
  ① 采集侧给了什么(`catalog.snapshots.raw` 的 variant_attributes /
     variation_asins / parent_asin,**原样**打出来);
  ② 我们的映射表怎么映(`services.variant_group._DIM_MAP`);
  ③ 这个 PT 的 spec 允许什么(`variantAttributeNames.items.enum`,
     并标出哪些枚举名在 PT 的 properties 里真有定义 —— 枚举里有、properties
     里没有的属性发出去就是"说按它分组却写不了值")。

最后按"名字里带 size/length/width/count 这类词"给**候选建议**:枚举里哪个名字
最可能承接映不上的那个亚马逊维度。⚠ 那只是**字面提示**,不是判定 —— 采纳前
人眼确认语义(旧仓踩过的坑:文具类目的 color_name=48 Color 其实是件数,
该映到 pieceCount 而不是 color,见旧仓 mapper.py 的 Phase 0.8 重映射表)。
"""

import logging

from registry import db
from services import mp_conform, pt_spec, variant_group

DANGEROUS = False       # 纯 SELECT + 读本地 spec 文件

logger = logging.getLogger("workflows.variant_probe")

_SQL = """
SELECT p.asin, p.walmart_pt, p.title,
       s.raw -> 'variant_attributes' AS attrs,
       s.raw -> 'variation_asins'    AS family,
       s.raw -> 'parent_asin'        AS parent,
       s.scraped_at
FROM catalog.products p
LEFT JOIN LATERAL (
    SELECT sn.raw, sn.scraped_at FROM catalog.snapshots sn
    WHERE sn.marketplace = p.marketplace AND sn.asin = p.asin
      AND sn.outcome = 'ok'
    ORDER BY sn.scraped_at DESC LIMIT 1
) s ON true
WHERE p.marketplace = 'US' AND p.asin = ANY(%s)
ORDER BY p.asin
"""

# 字面提示用的词族:枚举名里带这些词的,可能承接同名的亚马逊维度。
# **只用于给人提候选**,绝不参与自动判定
_HINT_WORDS = {
    "size_name": ("size", "length", "width", "height", "dimension", "capacity",
                  "volume"),
    "color_name": ("color", "colour", "finish", "shade"),
    "style_name": ("style", "pattern", "design", "theme"),
    "material_type": ("material", "fabric"),
    "number_of_items": ("count", "quantity", "pack", "piece"),
    "item_package_quantity": ("count", "quantity", "pack", "piece"),
    "flavor_name": ("flavor", "flavour", "taste"),
    "scent_name": ("scent", "fragrance", "aroma"),
}


def _enum_of_pt(pt: str) -> tuple[list[str], set[str], bool]:
    """输入:PT 名 → 输出:(变体维度枚举, PT properties 键集, spec 是否加载到)。"""
    spec = pt_spec.load_pt(pt) if pt else None
    if not spec:
        return [], set(), False
    props = spec.get("properties") or {}
    enum = mp_conform._enum_of(props.get("variantAttributeNames") or {})
    return [str(e) for e in (enum or [])], set(props), True


def _hints(dim: str, enum: list[str], props: set[str]) -> list[str]:
    """输入:映不上的亚马逊维度 + 该 PT 枚举 → 输出:字面相近的枚举名(候选)。"""
    words = _HINT_WORDS.get(dim, ())
    out = [e for e in enum
           if any(w in e.lower() for w in words)]
    # properties 里没定义的排后面并标注:那种发出去写不了值
    return sorted(out, key=lambda e: (e not in props, e))


def _probe_pt(pt: str) -> list[str]:
    enum, props, ok = _enum_of_pt(pt)
    if not ok:
        return [f"  ⚠ 取不到 PT「{pt}」的 spec(拼写?还是 specs 目录没这个 PT 的"
                f"拆分文件?先看 `cli.py catalog_health` 的 spec 覆盖面)"]
    if not enum:
        return [f"  PT「{pt}」的 spec **没有 variantAttributeNames 枚举** ——"
                f"这个类目不支持变体分组,所有成员只能按单品上"]
    lines = [f"  PT「{pt}」允许的变体维度 {len(enum)} 个:"]
    lines.append("    " + ", ".join(
        e + ("" if e in props else "(⚠ properties 里无定义,写不了值)")
        for e in sorted(enum)))
    # 我们的映射表能覆盖到哪几个
    covered = {}
    for amz, cands in variant_group._DIM_MAP.items():
        hit = next((c for c in cands if c in set(enum)), None)
        if hit:
            covered[amz] = hit
    lines.append(f"    我们的 _DIM_MAP 能映上 {len(covered)} 个:"
                 + (", ".join(f"{a}→{b}" for a, b in sorted(covered.items()))
                    or "(一个都映不上)"))
    return lines


def run(params: dict) -> str:
    """输入:params(asins= 或 pt=)→ 输出:三头对照报告。"""
    pt_only = str(params.get("pt", "")).strip()
    asins = [a.strip().upper() for a in str(params.get("asins", "")).split(",")
             if a.strip()]
    if not asins and not pt_only:
        raise ValueError("给 -p asins=B0X,B0Y 或 -p pt=<PT 名> 其中之一")

    lines = ["variant_probe(只读:采集侧给了什么 / 我们怎么映 / PT 允许什么)"]
    if pt_only:
        lines.append(f"【PT】{pt_only}")
        lines += _probe_pt(pt_only)
    if not asins:
        return "\n".join(lines)

    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL, (asins,))
        rows = cur.fetchall()
    got = {r[0] for r in rows}
    for miss in [a for a in asins if a not in got]:
        lines.append(f"【{miss}】⚠ 不在 catalog.products(先跑 product_ingest)")

    seen_pts: set[str] = set()
    for asin, pt, title, attrs, family, parent, at in rows:
        lines.append(f"【{asin}】{(title or '')[:48]}")
        lines.append(f"  PT={pt or '(未解出)'}"
                     + (f"  快照 {at:%Y-%m-%d}" if at else "  ⚠ 无 ok 快照"))
        # ① 采集侧原样
        lines.append(f"  采集侧:variant_attributes={attrs!r}")
        lines.append(f"          variation_asins={family!r} parent_asin={parent!r}")
        parsed = variant_group.parse_attrs(attrs)
        fam = variant_group.parse_family(family, asin)
        lines.append(f"  解析后:维度 {parsed or '(空)'};家族 {len(fam)} 个 {fam[:6]}")
        if not parsed:
            lines.append("  → **无变体维度取值**:这一条只能按单品上。成因在采集侧"
                         "(raw 里就没有这个字段),不是映射表的问题")
            continue
        # ② + ③ 映射与枚举
        enum, props, ok = _enum_of_pt(pt or "")
        if not ok:
            lines.append(f"  → 取不到 PT spec,映射判不了(PT={pt!r})")
            continue
        if not enum:
            lines.append("  → 这个 PT **不支持变体分组**(spec 无 "
                         "variantAttributeNames 枚举),只能按单品上")
            continue
        pairs = variant_group.pick_walmart_dims(list(parsed), enum)
        mapped = {amz for amz, _ in pairs}
        lines.append("  映上的:" + (", ".join(
            f"{amz}→{wm}={parsed[amz]!r}"
            + ("" if wm in props else "(⚠ properties 里无定义)")
            for amz, wm in pairs) or "(一个都没映上)"))
        for dim in sorted(set(parsed) - mapped):
            cands = variant_group._DIM_MAP.get(dim, ())
            hint = _hints(dim, enum, props)
            lines.append(
                f"  ⚠ {dim}={parsed[dim]!r} 没映上:_DIM_MAP 给的候选 "
                f"{list(cands) or '(该维度未登记)'} 都不在本 PT 枚举里")
            lines.append(f"      本 PT 枚举里字面相近的:{hint or '(没有)'}"
                         + ("  ← 人眼确认语义后可加进 _DIM_MAP" if hint else
                            ";这个 PT 可能真的不按这个维度分组"))
        if pt and pt not in seen_pts:
            seen_pts.add(pt)
    for pt in sorted(seen_pts):
        lines.append(f"【PT】{pt}")
        lines += _probe_pt(pt)
    lines.append("修法对照:采集侧没给 → 补采;_DIM_MAP 没登记/候选不对 → 加映射行;"
                 "枚举里压根没有这一族 → 这个类目不按该维度分组,别硬塞")
    return "\n".join(lines)
