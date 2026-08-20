"""pt_spec_sync — 从沃尔玛官方拉 PT 全集与上架 spec,重建类目准入明细。

用法:
  python cli.py pt_spec_sync --dry-run                # 只拉 taxonomy + 3 批 spec 样本
  python cli.py pt_spec_sync -p limit=200             # 先拉 200 个 PT 试水
  python cli.py pt_spec_sync                          # 全量(约 7000 PT,3/min×20 ≈ 2 小时)
  python cli.py pt_spec_sync -p store=谭总10 -p only_new=1

为什么要这条链(所有者 2026-08-20:「这个表我很久没有更新了,所以类目可能不全,
spec 的必填字段也可能有偏差了」):

飞书那张「沃尔玛类目准入明细」是很久以前一次性生成的 6,942 行,核查发现:
  · 「必需认证」45% 是空的;
  · 642 个 PTG 分组里 294 组(46%)**组内认证一字不差** —— 当年按 PTG 批量套的,
    于是婴儿配方奶标成了 CPSIA(儿童产品符合证书)、宠物碗标成了 AAFCO(宠物食品);
  · 382 个 PT 要硬认证却标「是」(可做且无需合规投入)—— 上架后被罚回来的正是这批。

修补不如重建:**官方 spec 的必填字段是客观的**,要填 `has_nrtl_listing_certification`
就是要 NRTL,要填 `ingredients` 就是食品。判定件在 services/pt_admission.py,
每条结论都能溯源到"哪个 spec 字段 / 哪条政策"。

节奏与安全:
  · `POST /v3/items/spec` 官方指南 3 TPM/seller,单次 ≤20 PT —— api 层已按 3/min
    登记桶,本工作流只管分批,不自己 sleep;
  · **边拉边落库**(每批一提交):七千个 PT 两小时,中途断了不能从头再来;
  · 已拉过且 spec 版本未变的 PT 默认跳过(`-p refresh=1` 强制重拉);
  · 只读端点 + 只写 audit.walmart_pt_spec,不碰任何店铺状态。
"""

import csv
import json
import logging

from api import items
from registry import db, paths
from services import pt_admission, stores as stores_svc

DANGEROUS = False       # 只读沃尔玛 + 只写 audit.walmart_pt_spec

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


def _taxonomy_rows(payload) -> list[dict]:
    """输入:taxonomy payload → 输出:[{category, ptg, pt}](把嵌套摊平)。

    官方结构层级名历年有变(category/categoryName、productTypeGroup/ptg…),
    所以**按值取而不是按固定键路径取**:任何一层里出现 productTypes 列表就收下,
    上两层的名字当 category/ptg。摊不出来时报数而不是静默返回空。
    """
    out: list[dict] = []

    def walk(node, cat="", ptg=""):
        if isinstance(node, dict):
            name = str(node.get("category") or node.get("categoryName")
                       or node.get("name") or node.get("productTypeGroup") or "").strip()
            pts = node.get("productTypes") or node.get("productType")
            if isinstance(pts, list) and pts and all(isinstance(x, str) for x in pts):
                for pt in pts:
                    out.append({"category": cat or name, "ptg": name or ptg,
                                "pt": pt.strip()})
            for k, v in node.items():
                if k in ("productTypes", "productType"):
                    continue
                walk(v, cat or name, name or ptg)
        elif isinstance(node, list):
            for v in node:
                walk(v, cat, ptg)

    walk(payload)
    return out


def _persist(conn, pt: str, required: set, adm) -> None:
    real = [c for c in adm.certs if adm.verdict != pt_admission.OK]
    with conn.cursor() as cur:
        cur.execute(_UPSERT, {
            "pt": pt,
            "real": json.dumps(real, ensure_ascii=False),
            "has_real": adm.verdict == pt_admission.BLOCK,
            "soft": json.dumps([c for c in adm.certs if c not in real], ensure_ascii=False),
            "has_soft": bool(adm.certs) and adm.verdict == pt_admission.OK,
            "fields": json.dumps(sorted(required), ensure_ascii=False),
        })


def run(params: dict) -> str:
    """输入:params(store/limit/refresh/only_new/dry_run)→ 输出:拉取与判定摘要。"""
    dry_run = bool(params.get("dry_run"))
    limit = int(params.get("limit", 0) or 0)
    refresh = str(params.get("refresh", "")).strip() == "1"
    names = [s.strip() for s in str(params.get("store", "")).split(",") if s.strip()]
    store = stores_svc.load_stores(names or None)[0]

    tax = items.get_taxonomy(store)
    rows = _taxonomy_rows(tax)
    if not rows:
        raise RuntimeError("taxonomy 摊不出任何 PT —— 官方结构可能变了,"
                           "先看原始 payload 再改 _taxonomy_rows(宁炸不吞)")
    seen, uniq = set(), []
    for r in rows:
        if r["pt"] and r["pt"] not in seen:
            seen.add(r["pt"])
            uniq.append(r)
    lines = [f"官方 taxonomy:{len(rows)} 行 → PT 去重 {len(uniq)} 个"]

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT walmart_product_type FROM audit.walmart_pt_spec")
            have = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT walmart_product_type FROM audit.walmart_pt_meta")
            in_meta = {r[0] for r in cur.fetchall()}
        new_pts = [r for r in uniq if r["pt"] not in in_meta]
        gone = sorted(in_meta - seen)
        lines.append(f"对账准入明细(walmart_pt_meta {len(in_meta)} 个):"
                     f"**官方有、明细没有 {len(new_pts)} 个**(要补的类目);"
                     f"明细有、官方已无 {len(gone)} 个")
        if new_pts[:5]:
            lines.append("  待补样例:" + "、".join(r["pt"] for r in new_pts[:5]))

        todo = [r for r in uniq if refresh or r["pt"] not in have]
        if str(params.get("only_new", "")).strip() == "1":
            todo = new_pts
        if limit:
            todo = todo[:limit]
        if dry_run:
            todo = todo[:items.SPEC_BATCH * 3]
        lines.append(f"本轮要拉 spec 的 PT:{len(todo)} 个"
                     f"(已有 {len(have)} 个,{'强制重拉' if refresh else '默认跳过已有'})")

        done = 0
        stat = {pt_admission.OK: 0, pt_admission.EVAL: 0, pt_admission.BLOCK: 0}
        preview: list[list] = []
        for i in range(0, len(todo), items.SPEC_BATCH):
            batch = todo[i:i + items.SPEC_BATCH]
            spec = items.get_spec(store, [r["pt"] for r in batch])
            for r in batch:
                sub = ((spec.get("payload") or {}).get(r["pt"])
                       if isinstance(spec.get("payload"), dict) else None) or spec
                required = pt_admission.extract_required(sub)
                adm = pt_admission.judge(r["pt"], required)
                stat[adm.verdict] += 1
                done += 1
                preview.append([r["category"], r["ptg"], r["pt"], "",
                                adm.verdict, " | ".join(adm.certs),
                                (adm.reasons[0] if adm.reasons else ""),
                                len(required), len(required),
                                " | ".join(sorted(required))])
                if not dry_run:
                    _persist(conn, r["pt"], required, adm)
            if not dry_run:
                conn.commit()          # 边拉边落:两小时的活,断了不能从头再来
            logger.info("spec 已拉 %d/%d(%s)", done, len(todo), r["pt"])

    lines.append(f"判定结果:是 {stat[pt_admission.OK]} / "
                 f"需评估 {stat[pt_admission.EVAL]} / 否 {stat[pt_admission.BLOCK]}")
    if preview:
        out = paths.reports_dir() / "pt_admission_preview.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Walmart Category", "Walmart PTG", "Walmart Product Type",
                        "准入状态", "中国卖家可做", "必需认证", "特殊备注",
                        "字段总数", "必填字段数", "必填字段清单"])
            w.writerows(preview)
        lines.append(f"飞书粘贴表(列名同现表):{out}")
    if dry_run:
        lines.append("(dry-run:只拉了前 3 批做结构核对,一行未写库)")
    return "\n".join(lines)
