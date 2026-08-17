"""audit_why — 这个 ASIN 为什么是这个审核结论(只读排查,不写任何东西)。

用法:
  python cli.py audit_why -p asins=B00004Z4HQ,B0000DI4ZW
  python cli.py audit_why -p pt=Hammers        # 按 PT 看:那一行数据长什么样 + 谁被它拒了
  python cli.py audit_why -p asins=B0X -p runs=3   # 看最近 3 轮(判定改过之后对比用)

为什么要有这条:审核的结论是**多层数据叠出来的**(飞书维护的 walmart_pt_meta
四列、pt_spec 的认证字段、三张黑名单、映射表、LLM),结论列里只留得下一句话。
出了可疑判定要么翻日志要么手写四表 JOIN —— 所有者 2026-08-16 实遇
(一把普通锤子被拒,理由写着 General-Use Products),而那句话既不说命中哪条
规则,也不说规则读的是哪一格数据。

本工作流把那条链**原样摊开**:命中了哪条规则 → 它读的是哪张表哪一列 →
那一格里到底写着什么。看完能直接判断是"规则对、数据错"还是"数据对、规则错"。
"""

import logging

from registry import db
from services import audit_reason

DANGEROUS = False       # 纯 SELECT

logger = logging.getLogger("workflows.audit_why")

_SQL_PRODUCT = """
SELECT asin, title, brand, walmart_pt, pt_source,
       audit_status, audit_reason, audited_at, audit_version
FROM catalog.products
WHERE marketplace = 'US' AND asin = ANY(%s)
"""

_SQL_RUNS = """
SELECT run_id, asin, walmart_product_type, pt_source, pt_confidence,
       score_final, verdict, stage_stopped_at, l3_verdict, l3_reason_text,
       created_at
FROM audit.audit_runs
WHERE asin = ANY(%s)
ORDER BY asin, created_at DESC
"""

_SQL_HITS = """
SELECT run_id, stage, rule_code, penalty, detail
FROM audit.audit_hits
WHERE run_id = ANY(%s)
ORDER BY run_id, penalty, hit_id
"""

# 飞书那张类目表(所有者手里那份「Walmart Category / PTG / Product Type /
# 准入状态 / 中国卖家可做」就是它)+ spec 表的认证字段
_SQL_META = """
SELECT walmart_product_type, walmart_category, walmart_ptg,
       access_state, zh_can_do, requirements, notes
FROM audit.walmart_pt_meta
WHERE walmart_product_type = ANY(%s)
"""

_SQL_SPEC = """
SELECT walmart_product_type, has_real_cert, has_soft_cert,
       real_cert_fields, soft_cert_fields
FROM audit.walmart_pt_spec
WHERE walmart_product_type = ANY(%s)
"""

_SQL_PT_VICTIMS = """
SELECT p.asin, p.title, p.audit_status
FROM catalog.products p
WHERE p.marketplace = 'US' AND p.walmart_pt = %s
  AND p.audit_status = 'rejected'
LIMIT %s
"""

# 规则 → 它读的是哪张表哪一列。**这一列才是排查的落点**:知道规则名没用,
# 知道"它读的是飞书某张表的某一格"才能去改那一格
_RULE_SOURCE = {
    "cat_access_blocked":           "audit.walmart_pt_meta.access_state",
    "cat_zh_blocked":               "audit.walmart_pt_meta.zh_can_do",
    "cat_requires_cert_hard":       "audit.walmart_pt_meta.requirements 或 "
                                    "audit.walmart_pt_spec.has_real_cert",
    "cat_requires_cert_soft":       "audit.walmart_pt_meta.requirements(软词)",
    "cat_requires_cert_small_part": "audit.walmart_pt_spec + NRTL 词表",
    "zh_seller_mega_cat_forbidden": "代码常量:中国卖家硬禁 8 大类",
    "forbidden_mega_cat":           "refdata 禁售大类 yaml",
    "phase0_brand_blacklist":       "catalog.brand_blacklist",
    "phase0_lark_blacklist_asin":   "catalog.asin_blacklist",
    "phase0_lark_blacklist_seller": "catalog.seller_blacklist",
    "phase0_lark_blacklist_amazon_cat": "catalog.amazon_cat_blacklist",
    "title_desc_blacklist":         "catalog.brand_blacklist(扫标题/描述)",
    "trademark_live":               "uspto 商标库",
    "phase0_trademark_symbol":      "正则扫标题/bullets/描述",
}


def _fmt_hit(stage, code, penalty, detail) -> list[str]:
    d = detail or {}
    lines = [f"    [{stage}] {code} {penalty:+d} — "
             f"{audit_reason.explain_hit(code, d)}"]
    src = _RULE_SOURCE.get(code)
    if src:
        lines.append(f"          读的是:{src}")
    for k in ("meta_requirements", "matched_hard_kws", "matched_soft_kws",
              "hard_cert_fields", "access_state", "zh_can_do",
              "walmart_category", "walmart_policy"):
        if d.get(k) not in (None, "", [], {}):
            lines.append(f"          {k} = {d[k]!r}")
    return lines


def _by_pt(pt: str, limit: int) -> str:
    out = [f"PT「{pt}」的原始数据(审核的四硬闸全查这两张表):"]
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_META, ([pt],))
        rows = cur.fetchall()
        if not rows:
            out.append("  ⚠ audit.walmart_pt_meta 里**没有这个 PT** —— "
                       "R1/R3 一律静默放行(旧仓原语义)")
        for r in rows:
            _pt, cat, ptg, access, zh, req, notes = r
            out += [f"  walmart_category = {cat!r}",
                    f"  walmart_ptg      = {ptg!r}",
                    f"  access_state     = {access!r}   ← R1 白名单 "
                    f"{{普通商品, 附条件允许}}",
                    f"  zh_can_do        = {zh!r}   ← R1 白名单 {{是, 需评估*}}",
                    f"  requirements     = {req!r}   ← **R3 在这一格里扫认证关键词**",
                    f"  notes            = {notes!r}"]
        cur.execute(_SQL_SPEC, ([pt],))
        for _pt, hard, soft, hf, sf in cur.fetchall():
            out += [f"  spec.has_real_cert = {hard}(字段 {hf}) ← R3 分支 B",
                    f"  spec.has_soft_cert = {soft}(字段 {sf})"]
        cur.execute(_SQL_PT_VICTIMS, (pt, limit))
        victims = cur.fetchall()
    if victims:
        out.append(f"  这个 PT 下已判拒的产品(前 {len(victims)} 个):")
        out += [f"    {a}  {(t or '')[:60]}" for a, t, _s in victims]
    return "\n".join(out)


def run(params: dict) -> str:
    pt = str(params.get("pt", "")).strip()
    limit = int(params.get("limit", 10))
    asins = [a.strip().upper() for a in str(params.get("asins", "")).split(",")
             if a.strip()]
    if not asins and not pt:
        return "要么给 -p asins=B0A,B0B,要么给 -p pt=Hammers"
    if pt and not asins:
        return _by_pt(pt, limit)

    want_runs = int(params.get("runs", 1))
    out: list[str] = []
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_PRODUCT, (asins,))
        prods = {r[0]: r for r in cur.fetchall()}
        cur.execute(_SQL_RUNS, (asins,))
        runs_by_asin: dict[str, list] = {}
        for r in cur.fetchall():
            runs_by_asin.setdefault(r[1], []).append(r)
        keep = [r[0] for rs in runs_by_asin.values() for r in rs[:want_runs]]
        cur.execute(_SQL_HITS, (keep,))
        hits: dict[int, list] = {}
        for run_id, stage, code, pen, detail in cur.fetchall():
            hits.setdefault(run_id, []).append((stage, code, pen, detail))
        pts = sorted({r[2] for rs in runs_by_asin.values()
                      for r in rs[:want_runs] if r[2]})
        meta = {}
        if pts:
            cur.execute(_SQL_META, (pts,))
            meta = {r[0]: r for r in cur.fetchall()}

    for asin in asins:
        p = prods.get(asin)
        out.append(f"\n══ {asin} ══")
        if not p:
            out.append("  ⚠ 不在 catalog.products —— 采集还没摄进来,"
                       "既审不了也回填不了(先跑 product_ingest)")
            continue
        _a, title, brand, wpt, psrc, status, reason, at, ver = p
        out += [f"  标题 {(title or '')[:70]}",
                f"  品牌 {brand!r}",
                f"  结论 {status}  理由 {reason!r}",
                f"  类目 {wpt!r}(来源 {psrc},审于 {at} 规则版本 {ver})"]
        m = meta.get(wpt)
        if m:
            out.append(f"  该 PT 在飞书类目表:大类 {m[1]!r} / 准入 {m[3]!r} / "
                       f"中国卖家 {m[4]!r} / 要求 {m[5]!r}")
        elif wpt:
            out.append(f"  ⚠ 该 PT 不在 audit.walmart_pt_meta —— R1/R3 静默放行")
        for r in runs_by_asin.get(asin, [])[:want_runs]:
            rid, _a2, rpt, rsrc, rconf, score, verdict, stage, l3v, l3t, ts = r
            out.append(f"  ── 第 {rid} 轮 {ts:%Y-%m-%d %H:%M} → {verdict} "
                       f"(分 {score},停在 {stage},PT={rpt!r}/{rsrc}/{rconf})")
            if l3t:
                out.append(f"    L3({l3v}):{l3t}")
            hs = hits.get(rid, [])
            if not hs:
                out.append("    (无命中记录)")
            for stage_, code, pen, detail in hs:
                out += _fmt_hit(stage_, code, pen, detail)
        if not runs_by_asin.get(asin):
            out.append("  (audit.audit_runs 里没有这个 ASIN 的记录 —— 没审过)")
    return "\n".join(out)
