"""pt_spec_sync — 用**本地官方 spec** 重建沃尔玛类目准入明细。

用法:
  python cli.py pt_spec_sync --dry-run           # 只读 spec + 判定,打印对账,不写库
  python cli.py pt_spec_sync                     # 落 audit.walmart_pt_spec + 导出飞书粘贴表
  python cli.py pt_spec_sync -p limit=200        # 先判 200 个看看
  python cli.py pt_spec_sync -p pt="Baby Formula,Pet Bowls" --dry-run   # 单点看证据链

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
             "zh_can_do FROM audit.walmart_pt_meta")


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


def run(params: dict) -> str:
    """输入:params(limit/dry_run)→ 输出:对账 + 判定分布 + 导出路径。"""
    dry_run = bool(params.get("dry_run"))
    limit = int(params.get("limit", 0) or 0)

    n_idx, n_ok = pt_spec.coverage()
    ver = resources.FEED_SPEC_VERSIONS["MP_ITEM"]
    lines = [f"本地官方 spec:{paths.mp_item_spec_dir()}",
             f"  版本串 {ver}(与上架链同源);索引 PT {n_idx} 个,拆分文件解析到 {n_ok} 个"]
    if n_ok < n_idx:
        lines.append(f"  ⚠ 有 {n_idx - n_ok} 个 PT 在索引里但找不到拆分文件,"
                     f"这批判不了(spec 目录不完整)")

    # Orderable 公共段:6942 个 PT 一模一样,零区分度,但要并进「必填字段清单」
    common_req = pt_admission.extract_required(pt_spec.orderable_spec())
    common_all = pt_admission.all_fields(pt_spec.orderable_spec())
    lines.append(f"  Orderable 公共必填 {len(common_req)} 个(每个 PT 都有,判定时零区分度)")

    pts = sorted(pt_spec.known_pts())
    if limit:
        pts = pts[:limit]

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_META_SQL)
            meta = {r[0]: {"cat": r[1] or "", "ptg": r[2] or "", "notes": r[3] or "",
                           "zh": r[4] or ""} for r in cur.fetchall()}
        only_spec = [p for p in pt_spec.known_pts() if p not in meta]
        only_meta = sorted(set(meta) - pt_spec.known_pts())
        lines.append(f"对账准入明细(walmart_pt_meta {len(meta)} 个):"
                     f"**spec 有、明细没有 {len(only_spec)} 个**(要补的类目);"
                     f"明细有、spec 里没有 {len(only_meta)} 个(PT 可能已下线)")
        for tag, xs in (("待补样例", sorted(only_spec)[:5]), ("已下线样例", only_meta[:5])):
            if xs:
                lines.append(f"  {tag}:" + "、".join(xs))

        stat = {pt_admission.OK: 0, pt_admission.EVAL: 0, pt_admission.BLOCK: 0}
        rows, review, changed, no_spec = [], [], 0, 0
        diff: collections.Counter = collections.Counter()
        want = {x.strip() for x in str(params.get("pt", "")).split(",") if x.strip()}
        for pt in pts:
            spec = pt_spec.load_pt(pt)
            if spec is None:
                no_spec += 1
                continue
            req = common_req | pt_admission.extract_required(spec)
            adm = pt_admission.judge(pt, req, age_values=_age_values(spec))
            stat[adm.verdict] += 1
            m = meta.get(pt, {})
            old = _old_bucket(m.get("zh", ""))
            order = {pt_admission.OK: 0, pt_admission.EVAL: 1, pt_admission.BLOCK: 2}
            move = ("新增(现表无此 PT)" if not old else
                    "不变" if old == adm.verdict else
                    "收紧" if order[adm.verdict] > order[old] else "放松")
            diff[f"{old or '(无)'} → {adm.verdict}"] += 1
            rows.append([m.get("cat", ""), m.get("ptg", ""), pt, "",
                         adm.verdict, " | ".join(adm.certs), m.get("notes", ""),
                         len(common_all | pt_admission.all_fields(spec)), len(req),
                         " | ".join(sorted(req))])
            review.append([pt, m.get("cat", ""), m.get("ptg", ""),
                           m.get("zh", ""), adm.verdict, move,
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
                        "现中国卖家可做", "新判定", "变化", "必需认证",
                        "判据(逐条溯源)", "人工"])
            w.writerows(review)
        lines.append(f"差异复核表(带判据溯源,不用于粘贴):{rv}")
        lines.append("  ⚠「准入状态」列留空 —— 那是沃尔玛侧的准入事实"
                     "(普通商品/附条件允许/需Walmart审批/禁售),spec 里没有,"
                     "沿用现表旧值或人工填,别让它被空值覆盖")
    lines.append("(dry-run:一行未写库)" if dry_run
                 else f"已写 audit.walmart_pt_spec {changed} 行")
    return "\n".join(lines)
