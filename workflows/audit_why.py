"""audit_why — 这个 ASIN 为什么是这个审核结论(只读排查,不写任何东西)。

用法:
  python cli.py audit_why -p asins=B00004Z4HQ,B0000DI4ZW
  python cli.py audit_why -p pt=Hammers        # 按 PT 看:那一行数据长什么样 + 谁被它拒了
  python cli.py audit_why -p asins=B0X -p runs=3   # 看最近 3 轮(判定改过之后对比用)
  python cli.py audit_why -p missing_meta=1    # 全库面:哪些 PT 让 R1/R3 两道闸静默失效

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

# R1(准入)与 R3(认证)两道闸只查 walmart_pt_meta,查不到就静默放行。
#
# ⚠ 口径必须是「**真的走到了那两道闸**、而 meta 表没有这个 PT」。
# 首版拿 `catalog.products.walmart_pt` 直接 LEFT JOIN,数出来 8394 个产品,
# 其中 7872 个 PT='unknown' —— 那是"类目没解出来"(审核停在 L1 判 pending),
# 压根没走到 R1/R3,算进"闸失效"是**冤枉的**(所有者 2026-08-17 当场指出:
# 「如果在前面任何步骤被拦截,就不会进入下一步,所以可能是输出的结果有误导性」)。
#
# 判据是 `audit_runs.stage_stopped_at`:
#   'L0' Phase0 拦下   → 没走到,不算
#   'L1' 类目解不出    → 没走到,不算
#   NULL / 'L2'/'L3'/'L4' → **走到了**,这才是闸该生效的那批
_LATEST_RUN_CTE = """
WITH latest AS (
    SELECT DISTINCT ON (asin)
           asin, walmart_product_type AS pt, stage_stopped_at, verdict
    FROM audit.audit_runs
    ORDER BY asin, created_at DESC
)
"""

_SQL_STAGE_MIX = _LATEST_RUN_CTE + """
SELECT coalesce(stage_stopped_at, '(过了闸)') AS stage, count(*)
FROM latest GROUP BY 1 ORDER BY 2 DESC
"""

_SQL_MISSING_META = """
WITH latest AS (
    SELECT DISTINCT ON (asin)
           asin, walmart_product_type AS pt, stage_stopped_at, verdict,
           pt_source, created_at
    FROM audit.audit_runs
    ORDER BY asin, created_at DESC
)
SELECT l.pt, count(*) AS n,
       count(*) FILTER (WHERE l.verdict = 'pass') AS passed,
       -- ⚠ 年代与来源是判"这是现在的洞还是搬进来的历史"的唯一凭据:
       -- 今天的引擎**产不出字典外 PT**(resolve_pt 末尾一道防御 + L1 LLM 的
       -- _in_dictionary 双闸),所以字典外 PT 只可能来自旧系统迁进来的
       -- 204 万行历史 run。是不是,看这两列,别猜
       max(l.created_at) AS newest,
       array_agg(DISTINCT coalesce(l.pt_source, '(空)')) AS sources
FROM latest l
LEFT JOIN audit.walmart_pt_meta m ON m.walmart_product_type = l.pt
WHERE (l.stage_stopped_at IS NULL
       OR l.stage_stopped_at IN ('L2', 'L3', 'L4'))      -- 真走到了闸
  AND l.pt IS NOT NULL AND l.pt <> '' AND l.pt <> 'unknown'
  AND l.pt NOT LIKE '(%%'                                 -- 桩值不是 PT
  AND m.walmart_product_type IS NULL
GROUP BY 1 ORDER BY 2 DESC
"""

# 本仓规则引擎上线日:之后跑的 run 才是"现在这套"判的。
# 早于它的一律是旧系统迁进来的历史(批次 A 搬 audit_runs 全史,见迁移计划)
_NEW_ENGINE_SINCE = "2026-08-13"

# 「表里真没有」还是「名字对不上」——两者的修法完全不同(补数据 vs 对齐命名),
# 而看 PT 名字本身分不出来。去标点去空白小写后再比一次
_SQL_META_FUZZY = """
SELECT walmart_product_type,
       lower(regexp_replace(walmart_product_type, '[^a-zA-Z0-9]', '', 'g')) AS k
FROM audit.walmart_pt_meta
WHERE lower(regexp_replace(walmart_product_type, '[^a-zA-Z0-9]', '', 'g'))
      = ANY(%s)
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
    # 2026-08-21 下线的码,只可能出现在**存量** audit_hits 里,留着让历史行说得出来路
    "cat_requires_cert_small_part": "audit.walmart_pt_spec + NRTL 词表(已下线)",
    # 判不了(不是判过了):这两条 penalty=0 但整条结论转 pending 待人工
    "cat_gate_pt_unknown":          "L2 R1:PT 没定下来,白名单查不了",
    "cat_gate_pt_not_in_meta":      "L2 R1:PT 不在 audit.walmart_pt_meta",
    # ⚠ 判据只是 amazon_category_path 的**第一段**(顶级类目名)。清单
    # 2026-08-20 起住在库里(match_type='top_name'),改类目改表不改代码;
    # 看到这条先核 detail.full_path,再核表里那行顶级名对不对
    "phase0_forbidden_category":    "catalog.amazon_cat_blacklist "
                                    "match_type='top_name'(只看路径第一段)",
    "phase0_brand_blacklist":       "catalog.brand_blacklist",
    "phase0_lark_blacklist_asin":   "catalog.asin_blacklist",
    "phase0_lark_blacklist_seller": "catalog.seller_blacklist",
    # 同一张表的另外两种匹配:node_subtree 拦整棵子树(detail.browse_node_id
    # 是被命中的子树根)、path_exact 归一化完整路径等值(飞书镜像历史行)
    "phase0_lark_blacklist_amazon_cat": "catalog.amazon_cat_blacklist "
                                    "match_type='node_subtree'/'path_exact'",
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
    # 把 detail 原样摊开(除了纯噪音的几个)—— 排查时"这一格里到底是什么"
    # 才是答案,挑着打就会漏(首版挑了 8 个键,Phase0 三表写的 asin/normalized/
    # seller_id 一个都不在里面,于是"黑名单命中"打出来是空的)
    for k, v in sorted(d.items()):
        if k in ("source", "note") or v in (None, "", [], {}):
            continue
        lines.append(f"          {k} = {v!r}")
    return lines


def _by_pt(pt: str, limit: int) -> str:
    out = [f"PT「{pt}」的原始数据(审核的四硬闸全查这两张表):"]
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_META, ([pt],))
        rows = cur.fetchall()
        if not rows:
            out.append("  ⚠ audit.walmart_pt_meta 里**没有这个 PT** —— "
                       "R1/R3 一律静默放行(旧仓原语义)")
            # 十有八九是名字对不上而不是真没有,直接把近似的那行报出来
            cur.execute(_SQL_META_FUZZY, ([_norm(pt)],))
            near = cur.fetchall()
            if near:
                out.append(f"  但表里有等价的一行:{near[0][0]!r} —— "
                           f"**是命名对不上,不是数据缺**(大小写/标点/空白)")
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


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


_STAGE_CN = {
    "L0": "停在 Phase0(黑名单/禁售大类/®™)—— 没走到 R1/R3",
    "L1": "停在 L1 类目解不出(判 pending)—— 没走到 R1/R3",
    "L2": "走到 L2 被规则拒",
    "L3": "走到 L3 被语义拒",
    "L4": "走到 L4 被图片拒",
    "(过了闸)": "过了全部闸",
    # 旧系统的 45 天 TTL 短路(本仓已废除,不再产出)——出现即说明这批 run
    # 是批次 A 从旧库搬进来的历史,不是现在这套跑的
    "SHORTCUT": "旧系统的历史短路 —— **迁进来的老 run**,不是现在这套跑的",
}


def _missing_meta(limit: int) -> str:
    """输入:列出上限 → 输出:R1/R3 两道闸真正失效的面。

    只数**真的走到那两道闸**的(见 _SQL_MISSING_META 头注),并把各层停在哪里
    的分布一并打出来 —— 分母说不清的比例没有意义。
    """
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_STAGE_MIX)
        stages = cur.fetchall()
        cur.execute(_SQL_MISSING_META)
        rows = cur.fetchall()
        fuzzy = {}
        if rows:
            cur.execute(_SQL_META_FUZZY, ([_norm(r[0]) for r in rows],))
            fuzzy = {k: name for name, k in cur.fetchall()}

    out = ["每个 ASIN 最近一轮停在哪(R1/R3 只对走到 L2 的那批生效):"]
    out += [f"    {n:>7}  {stage:<8} {_STAGE_CN.get(stage, '')}"
            for stage, n in stages]
    out.append("")
    if not rows:
        out.append("走到 R1/R3 的产品,PT 全都在 audit.walmart_pt_meta 里 ✅")
        return "\n".join(out)

    named = [r for r in rows if fuzzy.get(_norm(r[0]))]
    absent = [r for r in rows if not fuzzy.get(_norm(r[0]))]
    total = sum(r[1] for r in rows)
    fresh = [r for r in rows if str(r[3])[:10] >= _NEW_ENGINE_SINCE]

    out.append(f"⚠ 走到 R1/R3 的产品里,{total} 个的 PT 不在 walmart_pt_meta"
               f"({len(rows)} 个 PT)")
    # 先定性:是现在的洞,还是搬进来的历史?这决定了要不要动手
    if not fresh:
        out.append(f"  ✅ 但**全部是 {_NEW_ENGINE_SINCE} 之前的 run** —— "
                   f"是旧系统迁进来的历史,不是现在这套的洞。")
        out.append(f"     现在的引擎产不出字典外 PT(resolve_pt 末尾一道防御 + "
                   f"L1 的 _in_dictionary 双闸,字典就是 pt_meta 本身)。")
        out.append(f"     这些行的结论**没被现在这套复核过**;要复核就把它们的"
                   f"上架表 E 列清空,或 `-p force_rerun=<新版本号>` 整批重审。")
    else:
        out.append(f"  ⚠ 其中 {sum(r[1] for r in fresh)} 个是 "
                   f"{_NEW_ENGINE_SINCE} 之后跑的(**现在这套产出的**)——"
                   f"那说明字典闸真有漏,要查 resolve_pt / _in_dictionary")
    if named:
        out.append(f"  ① 名字对不上({len(named)} 个 PT,"
                   f"{sum(r[1] for r in named)} 个产品):表里有等价的一行,"
                   f"只是大小写/标点/空白不同 —— 修的是**命名对齐**,不是补数据")
        out += [f"      {n:>6}(过 {p:>5})  {pt!r}\n"
                f"              表里是 {fuzzy[_norm(pt)]!r}"
                for pt, n, p, _ts, _src in named[:limit]]
    if absent:
        out.append(f"  ② 表里真没有({len(absent)} 个 PT,"
                   f"{sum(r[1] for r in absent)} 个产品):")
        out += [f"      {n:>6}(过 {p:>5})  {pt:<42} 最近 {str(ts)[:10]} "
                f"来源 {','.join(src)}"
                for pt, n, p, ts, src in absent[:limit]]
    if len(rows) > limit:
        out.append(f"  (每类只列前 {limit} 个,-p limit=N 看更多)")
    return "\n".join(out)


def run(params: dict) -> str:
    pt = str(params.get("pt", "")).strip()
    limit = int(params.get("limit", 10))
    asins = [a.strip().upper() for a in str(params.get("asins", "")).split(",")
             if a.strip()]
    if params.get("missing_meta"):
        return _missing_meta(limit if limit > 10 else 30)
    if not asins and not pt:
        return ("要么给 -p asins=B0A,B0B,要么给 -p pt=Hammers,"
                "要么 -p missing_meta=1 看准入/认证闸的失效面")
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
            # ⚠ 这**不是**本次判拒的原因(下面会写清停在哪一层),但它是个
            # 独立的真问题:R1 准入闸与 R3 认证闸都只查 walmart_pt_meta,
            # 查不到就静默放行 —— 那两道闸对这个类目等于不存在。
            # 全库有多少 PT 这样,跑 `-p missing_meta=1`
            out.append(f"  ⚠ 该 PT 不在 audit.walmart_pt_meta —— **R1 准入闸与 "
                       f"R3 认证闸对它静默放行**(与本次判拒无关,是另一个问题;"
                       f"全库面看 `python cli.py audit_why -p missing_meta=1`)")
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
