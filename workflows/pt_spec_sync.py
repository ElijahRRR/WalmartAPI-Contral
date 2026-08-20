"""pt_spec_sync — 用**本地官方 spec** 重建沃尔玛类目准入明细。

用法:
  python cli.py pt_spec_sync --dry-run           # 只读 spec + 判定,打印对账,不写库
  python cli.py pt_spec_sync                     # 落 audit.walmart_pt_spec + 导出飞书粘贴表
  python cli.py pt_spec_sync -p limit=200        # 先判 200 个看看
  python cli.py pt_spec_sync -p pt="Baby Formula,Pet Bowls" --dry-run   # 单点看证据链
  python cli.py pt_spec_sync -p explain="3-in-1 Shampoo, Conditioner & Body Washes"
      # 「字段总数/必填字段数」到底该怎么数:一个 PT 在几种读法下分别是多少
  python cli.py pt_spec_sync -p sheet=~/Downloads/沃尔玛类目准入明细.csv --dry-run
      # 拿现表的「必填字段清单」与本地 spec **逐 PT 逐字段**比,看差异长什么样

数据源:`services.pt_spec` —— `<DATA_ROOT>/specs/MP_ITEM/<版本>/` 那份按 PT 拆分的
官方 spec,**上架链(listing L2c)用的就是它**。不调 `POST /v3/items/spec`:
本地这份就是权威,再拉一遍等于给同一个能力开第二条路径(铁律),而且官方那个
端点 3 TPM,七千个 PT 要跑两小时。

## 为什么要重建准入明细(所有者 2026-08-20 核查)

飞书那份 6,942 行是很久以前一次性生成的:
  · 「必需认证」**45% 是空的** —— 空到底是"不需要"还是"没填",表本身答不了;
  · 642 个 PTG 分组里 **294 组(46%)组内认证一字不差** —— 当年按 PTG 批量套的。
    实见 `Baby Foods & Formula` 整组套 CPSIA(儿童产品符合证书),于是**婴儿配方
    奶**标成了 CPSIA(它要的是 FDA 婴幼儿配方注册);`Pet Bowls`(宠物碗)被标
    「FDA 设施注册 + AAFCO」——那是宠物**食品**的要求;
  · **382 个 PT 要硬认证却标「是」**(可做且无需合规投入)—— 上架后被罚回来的正是这批。

官方 spec 的必填字段是客观的:要填 `has_nrtl_listing_certification` 就是要 NRTL,
要填 `ingredients` 就是食品。判定件在 services/pt_admission.py,每条结论都能溯源到
"哪个 spec 字段"。

## 两件它顺带回答的事

  · **类目全不全**:spec 索引里的 PT 全集 vs 准入明细,差集就是要补的类目;
  · **spec 有没有偏差**:摘要报出读的是哪个版本目录(换版 = 换 registry 里那个
    版本串,两边永远对得上上架链)。

只读本地文件 + 只写 audit.walmart_pt_spec,不碰任何店铺状态、不发任何请求。
"""

import collections
import csv
import json
import logging

from registry import db, paths, resources
from services import pt_admission, pt_spec

DANGEROUS = False       # 只读本地 spec + 只写 audit.walmart_pt_spec

logger = logging.getLogger("workflows.pt_spec_sync")

_UPSERT = """
INSERT INTO audit.walmart_pt_spec
    (walmart_product_type, real_cert_fields, has_real_cert, soft_cert_fields,
     has_soft_cert, fields, synced_at)
VALUES (%(pt)s, %(real)s, %(has_real)s, %(soft)s, %(has_soft)s, %(fields)s, now())
ON CONFLICT (walmart_product_type) DO UPDATE SET
    real_cert_fields = EXCLUDED.real_cert_fields,
    has_real_cert    = EXCLUDED.has_real_cert,
    soft_cert_fields = EXCLUDED.soft_cert_fields,
    has_soft_cert    = EXCLUDED.has_soft_cert,
    fields           = EXCLUDED.fields,
    synced_at        = now()
"""

_META_SQL = ("SELECT walmart_product_type, walmart_category, walmart_ptg, notes, "
             "zh_can_do, access_state FROM audit.walmart_pt_meta")


def _old_bucket(zh_can_do: str) -> str:
    """输入:现表「中国卖家可做」自由文本 → 输出:三档之一(便于与新判定比)。

    实测 35 种取值,但全都以 是 / 需评估 / 否 开头(「否(上架记录回测,
    BIZ-CN触发5次)」这类只是把证据写进了值里)。
    """
    z = (zh_can_do or "").strip()
    if z.startswith("是"):
        return pt_admission.OK
    if z.startswith("需评估"):
        return pt_admission.EVAL
    if z.startswith("否"):
        return pt_admission.BLOCK
    return ""


# ⚠ 曾经在这里加过"锁定":现表带政策/上架回测实证的「否」不许被 spec 翻案。
# 2026-08-20 所有者定稿撤掉 —— **「允许翻,以前生成并不可靠」**。摘要仍按现表
# 原措辞把"判否却被放松"的行拆开报数,让人看得见这批是从哪种依据翻过来的,
# 但不再替人拦。


def _age_values(spec: dict) -> list:
    """输入:PT spec → 输出:ageGroup 的枚举取值(判儿童产品用)。

    `ageGroup` 本身只是人群标签(成人服饰、老人助行器一样要填),**取值**落在
    儿童段才意味着 CPSIA —— 只看字段名会把成人拖鞋判成儿童产品。
    """
    out: list = []

    def walk(node):
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict) and "ageGroup" in props:
                ag = props["ageGroup"]
                if isinstance(ag, dict):
                    for key in ("enum", "examples"):
                        v = ag.get(key)
                        if isinstance(v, list):
                            out.extend(str(x) for x in v)
                    items = ag.get("items")
                    if isinstance(items, dict) and isinstance(items.get("enum"), list):
                        out.extend(str(x) for x in items["enum"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(spec)
    return out


def _sheet_diff(path: str, cache: dict) -> list[str]:
    """输入:现表 CSV + {PT: (spec, 顶层必填, 条件必填)} → 输出:逐 PT 逐字段差异报告。

    为什么要这个(所有者 2026-08-20:「你要辩证的看待我给你的资料,和你判断的
    如何获取必填字段」):现表与本地 spec 在三个字段上对不上 ——
    `smallPartsWarnings` +195、`has_written_warranty` +116、
    `state_chemical_disclosure` +19。至少三种解释都成立:
      ① spec 换版后确实加了必填 → 现表该更新;
      ② 现表生成时用的是更早的 spec 版本 → 两边都没错;
      ③ 当年的生成器有过滤/bug → 该怀疑现表其他列。
    **差异形状能分辨**:集中在少数类目像版本漂移,散在全表像生成器问题。
    所以不猜,把差异逐条摆出来。
    """
    import csv as _csv
    import os
    import re as _re
    fp = os.path.expanduser(path)
    if not os.path.exists(fp):
        return [f"⚠ 现表 CSV 不存在:{fp}"]
    only_sheet: collections.Counter = collections.Counter()
    only_spec: collections.Counter = collections.Counter()
    per_pt, same, n = [], 0, 0
    for row in _csv.DictReader(open(fp, encoding="utf-8-sig")):
        pt = (row.get("Walmart Product Type") or "").strip()
        got = cache.get(pt)
        if not got:
            continue
        n += 1
        sheet_set = {x.strip() for x in _re.split(r"\s*\|\s*",
                     row.get("必填字段清单") or "") if x.strip()}
        spec_set = got[1]
        a, b = sheet_set - spec_set, spec_set - sheet_set
        if not a and not b:
            same += 1
        else:
            per_pt.append((pt, sorted(a), sorted(b)))
        only_sheet.update(a)
        only_spec.update(b)
    out = [f"── 现表 vs 本地 spec 顶层必填,逐 PT 比对(能对上的 {n} 个 PT):",
           f"   完全一致 {same} 个;有差异 {len(per_pt)} 个"]
    if only_spec:
        out.append("   **spec 有、现表没有**的字段(现表可能是旧版 spec 生成的):")
        for f, c in only_spec.most_common(15):
            out.append(f"     {f:<50}{c:>6} 个 PT")
    if only_sheet:
        out.append("   **现表有、spec 没有**的字段(现表当年可能多收了):")
        for f, c in only_sheet.most_common(15):
            out.append(f"     {f:<50}{c:>6} 个 PT")
    if per_pt:
        out.append("   差异样例(前 8 个 PT):")
        for pt, a, b in per_pt[:8]:
            out.append(f"     {pt[:38]:<40} 现表多:{'、'.join(a[:3]) or '—'}"
                       f" | spec 多:{'、'.join(b[:3]) or '—'}")
    return out


def _evidence_kind(zh_can_do: str) -> str:
    """输入:现表「中国卖家可做」→ 输出:现值背后是哪一类证据(筛选用)。

    现表判否的行不是同一种东西:有的是从准入状态推的,有的是**沃尔玛政策**,
    还有 60 多个是**上架回测实证**(真上架被拒过 N 次)。所有者定「允许翻」,
    所以不拦,但复核表要能一列筛出来 —— 翻实证那批的代价和翻推断那批不是一回事。
    """
    z = (zh_can_do or "").strip()
    if not z.startswith("否"):
        return ""
    if "上架记录回测" in z or "BIZ-CN" in z:
        return "实证:上架被拒过"
    if "禁售" in z:
        return "沃尔玛政策禁售"
    if "进不去" in z:
        return "推断:准入状态需审批"
    return "否(未注明依据)"


def _explain(pt: str) -> list[str]:
    """输入:PT 名 → 输出:该 PT 在**几种字段读法**下的计数与差集(对表用)。

    起因(所有者 2026-08-20):「字段总数 / 必填字段数这两个数和我们现在的对不上,
    是否对字段总数和必填项目的理解有错误,以前上架系统遇到过相似的问题,
    搞了很多字段出来」。

    候选读法(生产上架链 services/mp_conform 读的是**顶层**,我第一版用的是递归):
      顶层        spec["required"] / spec["properties"]              ← 上架链口径
      +条件必填    ∪ allOf[*].then.required(不看取值,全量并)
      递归        每一层的 required / properties 全收              ← 我的第一版
    再叠上 Orderable 公共段与 MP_ITEM 信封段(sku/price/quantity 那些)。
    哪一种能对上现表的 113/46,哪一种就是当年的口径。
    """
    spec = pt_spec.load_pt(pt)
    if spec is None:
        return [f"── {pt}:spec 里没有这个 PT"]
    orderable = pt_spec.orderable_spec()
    top_req = set((spec.get("required") or []))
    top_props = set((spec.get("properties") or {}))
    cond_req = set()
    for cond in (spec.get("allOf") or []):
        cond_req.update((cond.get("then") or {}).get("required") or [])
    rec_req = pt_admission.extract_required(spec)
    rec_props = pt_admission.all_fields(spec)
    o_req = set((orderable.get("required") or []))
    o_props = set((orderable.get("properties") or {}))
    o_rec_req = pt_admission.extract_required(orderable)
    o_rec_props = pt_admission.all_fields(orderable)

    out = [f"── {pt} 的字段读法对表",
           "   ✅ **权威口径 = PT spec 顶层 required / 顶层 properties**(下面第一行)。"
           "与飞书现表逐字段核过一致;条件必填由 mp_conform.fill_known_required 按"
           "实际取值动态补,不算静态必填。Orderable 那些 sku/price 是信封字段,不并。"]
    for name, req, props in (
            ("✅ 顶层(仅 PT 段)← 导出列用这个", top_req, top_props),
            ("顶层 + allOf.then 条件必填", top_req | cond_req, top_props),
            ("顶层 + 条件 + Orderable 顶层", top_req | cond_req | o_req, top_props | o_props),  # 对照
            ("递归收全(PT 自己)", rec_req, rec_props),
            ("递归 + Orderable 递归(导出列口径)", rec_req | o_rec_req,
             rec_props | o_rec_props),
    ):
        out.append(f"   {name:<28} 必填 {len(req):>4} / 字段总数 {len(props):>4}")
    out.append(f"   其中 allOf 条件必填单独 {len(cond_req)} 个;"
               f"Orderable 递归必填 {len(o_rec_req)} / 字段 {len(o_rec_props)} 个")
    # 两个口径都给全(所有者 2026-08-20 自查:类目 Visible 62/14、
    # 官方完整商品 85/19、本地版本 86/19 —— 本地比官方多 1 个字段)
    out.append("   两个口径(都对,回答的是不同问题):")
    out.append(f"     类目 Visible(这个类目要准备哪些属性;导出列用它)"
               f"  字段 {len(top_props):>3} / 必填 {len(top_req):>3}")
    out.append(f"     完整商品(能不能提交,mp_conform.validate 口径)      "
               f"  字段 {len(top_props | o_props):>3} / 必填 {len(top_req | o_req):>3}")
    out.append(f"   Orderable 顶层必填 {len(o_req)} 个:{'、'.join(sorted(o_req))}")
    out.append(f"   Orderable 顶层字段 {len(o_props)} 个(官方当前 23,"
               f"多出来的那个就是本地 spec 的版本差):")
    out.append(f"     {'、'.join(sorted(o_props))}")
    # 合规文档类字段住在哪一层,决定它能不能当"要这个认证"的判据
    marks = ["certification_type", "nrtl_information", "has_nrtl_listing_certification",
             "children_product_certificate_document_reference_id",
             "children_product_test_report_document_reference_id",
             "general_certificate_of_conformity_document_reference_id",
             "ingredients", "labelImage", "prop65WarningText",
             "country_of_origin_substantial_transformation"]
    out.append("   合规相关字段各住在哪一层(**这决定它算不算判据**):")
    for f in marks:
        where = []
        if f in top_req:
            where.append("顶层必填")
        if f in cond_req:
            where.append("条件必填")
        if f in top_props:
            where.append("顶层属性")
        if f in rec_props and not where:
            where.append("仅深层")
        out.append(f"     {f:<58}{'、'.join(where) or '不存在'}")
    return out


def run(params: dict) -> str:
    """输入:params(limit/dry_run)→ 输出:对账 + 判定分布 + 导出路径。"""
    dry_run = bool(params.get("dry_run"))
    limit = int(params.get("limit", 0) or 0)

    ex = str(params.get("explain", "")).strip()
    if ex:
        return "\n".join(_explain(ex))

    n_idx, n_ok = pt_spec.coverage()
    ver = resources.FEED_SPEC_VERSIONS["MP_ITEM"]
    lines = [f"本地官方 spec:{paths.mp_item_spec_dir()}",
             f"  版本串 {ver}(与上架链同源);索引 PT {n_idx} 个,拆分文件解析到 {n_ok} 个"]
    if n_ok < n_idx:
        lines.append(f"  ⚠ 有 {n_idx - n_ok} 个 PT 在索引里但找不到拆分文件,"
                     f"这批判不了(spec 目录不完整)")

    # 「必填字段清单/字段总数」= **PT spec 的顶层 required / 顶层 properties**。
    #
    # 这不是推的,是与飞书现表逐字段核出来的(2026-08-20):
    #   `3-in-1 Shampoo, Conditioner & Body Washes` 现表 14 / 62,清单 14 项
    #   productName|brand|shortDescription|keyFeatures|mainImageUrl|
    #   isProp65WarningRequired|condition|hairProductForm|hairType|
    #   has_written_warranty|ingredients|labelImage|netContent|size
    #   —— 与本口径一字不差。全表 20+ 个判据字段的出现次数也逐个对上
    #   (ingredients 133/133、foodForm 163/163、ageGroup 919/919、
    #    labelImage 628/628、activeIngredients 19/19…),而条件必填那批
    #   (prop65WarningText、children_product_*、nrtl_information)
    #   **现表一条都没收** —— 它当年就是按顶层 required 生成的,判断是对的。
    #
    # ⚠ 不并 Orderable:sku/price/quantity/ShippingWeight 是任何商品都要的
    # 信封字段,写进"这个类目要准备哪些属性"里零信息量,现表也没收。
    # ⚠ 更不能递归:递归会混进 properties-only 与条件样板,清单一多,人照着
    # 去准备材料就会白办证(做洗发水的按那种清单要去办儿童产品符合证书)。
    lines.append("  必填口径 = PT spec 顶层 required(与飞书现表逐字段核过,一致)")

    pts = sorted(pt_spec.known_pts())
    if limit:
        pts = pts[:limit]

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_META_SQL)
            meta = {r[0]: {"cat": r[1] or "", "ptg": r[2] or "", "notes": r[3] or "",
                           "zh": r[4] or "", "access": r[5] or ""}
                    for r in cur.fetchall()}
        only_spec = [p for p in pt_spec.known_pts() if p not in meta]
        only_meta = sorted(set(meta) - pt_spec.known_pts())
        lines.append(f"对账准入明细(walmart_pt_meta {len(meta)} 个):"
                     f"**spec 有、明细没有 {len(only_spec)} 个**(要补的类目);"
                     f"明细有、spec 里没有 {len(only_meta)} 个(PT 可能已下线)")
        for tag, xs in (("待补样例", sorted(only_spec)[:5]), ("已下线样例", only_meta[:5])):
            if xs:
                lines.append(f"  {tag}:" + "、".join(xs))

        # ── 第一遍:只统计判据字段的覆盖率,定出哪些是"哪儿都有"的样板 ──
        cache: dict = {}
        top_counts: collections.Counter = collections.Counter()
        cond_counts: collections.Counter = collections.Counter()
        for pt in pts:
            spec = pt_spec.load_pt(pt)
            if spec is None:
                continue
            top_req = set(spec.get("required") or [])
            cond_req = set()
            for c in (spec.get("allOf") or []):
                cond_req.update((c.get("then") or {}).get("required") or [])
            cache[pt] = (spec, top_req, cond_req)
            top_counts.update(top_req)
            cond_counts.update(cond_req)
        bp_top, bp_cond, bp_detail = pt_admission.find_boilerplate(
            top_counts, cond_counts, len(cache) or 1)
        lines.append(f"判据字段覆盖率(共 {len(cache)} 个 PT;"
                     f"顶层>{pt_admission.BOILERPLATE_TOP:.0%} 或 "
                     f"条件>{pt_admission.BOILERPLATE_COND:.0%} 即判样板,不算判据):")
        for f, nt, nc, tag in bp_detail:
            if nt or nc:
                lines.append(f"   {f:<58}顶层 {nt:>5} / 条件 {nc:>5}"
                             + (f"   ⚠ {tag}" if tag else ""))
        if bp_top or bp_cond:
            lines.append(f"   → 剔除样板:顶层 {sorted(bp_top)} / 条件 {sorted(bp_cond)}")

        sheet_csv = str(params.get("sheet", "")).strip()
        if sheet_csv:
            lines += _sheet_diff(sheet_csv, cache)

        stat = {pt_admission.OK: 0, pt_admission.EVAL: 0, pt_admission.BLOCK: 0}
        rows, review, changed, no_spec = [], [], 0, 0
        diff: collections.Counter = collections.Counter()
        unlock_detail: collections.Counter = collections.Counter()
        want = {x.strip() for x in str(params.get("pt", "")).split(",") if x.strip()}
        for pt in pts:
            got = cache.get(pt)
            if got is None:
                no_spec += 1
                continue
            spec, top_req, cond_req = got
            req = top_req                       # 与飞书现表同口径:PT 顶层 required
            m = meta.get(pt, {})
            adm = pt_admission.judge(pt, top_req, conditional=cond_req,
                                     age_values=_age_values(spec),
                                     category=m.get("cat", ""),
                                     boilerplate_top=bp_top,
                                     boilerplate_cond=bp_cond)
            old = _old_bucket(m.get("zh", ""))
            order = {pt_admission.OK: 0, pt_admission.EVAL: 1, pt_admission.BLOCK: 2}
            move = ("新增(现表无此 PT)" if not old else
                    "不变" if old == adm.verdict else
                    "收紧" if order[adm.verdict] > order[old] else "放松")
            diff[f"{old or '(无)'} → {adm.verdict}"] += 1
            if old == pt_admission.BLOCK and adm.verdict != pt_admission.BLOCK:
                unlock_detail[(m.get("zh", "") or "(空)")[:28]] += 1
            stat[adm.verdict] += 1
            # ⚠「准入状态」**原样带出现表的值**:那是沃尔玛侧的准入事实
            # (普通商品/附条件允许/需Walmart审批/禁售),spec 里没有这个信息。
            # 留空就粘贴 = 把现表 6942 行的准入状态一次洗掉,而且不报错。
            rows.append([m.get("cat", ""), m.get("ptg", ""), pt,
                         m.get("access", ""),
                         adm.verdict, " | ".join(adm.certs), m.get("notes", ""),
                         len(spec.get("properties") or {}), len(req),
                         " | ".join(sorted(req))])
            review.append([pt, m.get("cat", ""), m.get("ptg", ""),
                           m.get("access", ""), m.get("zh", ""), adm.verdict, move,
                           _evidence_kind(m.get("zh", "")),
                           " | ".join(adm.certs),
                           " ;; ".join(adm.reasons), ""])
            if pt in want:
                lines.append(f"── {pt}:{adm.verdict}(现表「{m.get('zh','(无)')}」→ {move})")
                for r_ in adm.reasons:
                    lines.append(f"     {r_}")
                lines.append(f"     必填字段({len(req)}):{'、'.join(sorted(req))[:300]}")
            if not dry_run:
                real = adm.certs if adm.verdict == pt_admission.BLOCK else []
                with conn.cursor() as cur:
                    cur.execute(_UPSERT, {
                        "pt": pt,
                        "real": json.dumps(real, ensure_ascii=False),
                        "has_real": adm.verdict == pt_admission.BLOCK,
                        "soft": json.dumps([c for c in adm.certs if c not in real],
                                           ensure_ascii=False),
                        "has_soft": adm.verdict == pt_admission.EVAL,
                        "fields": json.dumps(sorted(req), ensure_ascii=False)})
                changed += 1
        if not dry_run:
            conn.commit()

    if unlock_detail:
        lines.append("现表判否、新判定放松的,按现表原措辞拆开(这批要逐条看):")
        for k, v in unlock_detail.most_common():
            lines.append(f"  {v:>6}  {k}")
    if diff:
        lines.append("与现表「中国卖家可做」逐条比对(现值 → 新判定):")
        for k, v in diff.most_common():
            tag = "" if k.split(" → ")[0] == k.split(" → ")[1] else "  ←要看的"
            lines.append(f"  {v:>6}  {k}{tag}")
    lines.append(f"判定 {len(rows)} 个 PT:是 {stat[pt_admission.OK]} / "
                 f"需评估 {stat[pt_admission.EVAL]} / 否 {stat[pt_admission.BLOCK]}"
                 + (f";{no_spec} 个索引里有但读不到 spec(跳过)" if no_spec else ""))
    if rows:
        out = paths.reports_dir() / "pt_admission_rebuilt.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Walmart Category", "Walmart PTG", "Walmart Product Type",
                        "准入状态", "中国卖家可做", "必需认证", "特殊备注",
                        "字段总数", "必填字段数", "必填字段清单"])
            w.writerows(rows)
        lines.append(f"飞书粘贴表(列名同现表,10 列整齐):{out}")
        rv = paths.reports_dir() / "pt_admission_review.csv"
        with open(rv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Walmart Product Type", "Walmart Category", "Walmart PTG",
                        "现准入状态", "现中国卖家可做", "新判定", "变化",
                        "现值证据类型", "必需认证", "判据(逐条溯源)", "人工"])
            w.writerows(review)
        lines.append(f"差异复核表(带判据溯源,不用于粘贴):{rv}")
        lines.append("  ⚠「准入状态」列留空 —— 那是沃尔玛侧的准入事实"
                     "(普通商品/附条件允许/需Walmart审批/禁售),spec 里没有,"
                     "沿用现表旧值或人工填,别让它被空值覆盖")
    lines.append("(dry-run:一行未写库)" if dry_run
                 else f"已写 audit.walmart_pt_spec {changed} 行")
    return "\n".join(lines)
