"""category_blacklist_import — 类目黑名单录入/重录(CSV 或内置种子)。

用法:
  python cli.py category_blacklist_import --dry-run           # 只看会写什么
  python cli.py category_blacklist_import -p seed=1           # 只灌内置种子(顶级+子树)
  python cli.py category_blacklist_import -p csv=<路径>        # 灌清洗后的名单
  python cli.py category_blacklist_import -p csv=<路径> -p replace=1
                                                # 先清掉同源旧行再灌(重录)

**判据住在库里**(所有者定稿 2026-08-20:「代码里面的类目可以拿到数据库里来,
还可以减轻代码的臃肿」)。本工作流是那张表的**唯一写入口**(飞书镜像那条
由 risk_sync 负责,两边按 `source` 列分家,互不冲刷)。

CSV 列(与清洗表同构,多余列忽略):
  类目               归一化路径或官方路径(写进 category_norm/category_raw)
  browse_node_id     子树规则的判据
  中文翻译           category_zh
  建议               只有 `留-*` / `增-*` 开头的行才录入;`删-*` 一律跳过
  原因               reason
  匹配方式           **`子树` / `顶级名` / `路径等值` / 其它值一律跳过**(见下)

⚠ **「有 ID 就当子树根」是错的**,别再退回那个口径(2026-08-20 做净增覆盖
对账时实见):名单里 582 条的 browse_node_id 是**回落匹配**来的 —— 祖先前缀、
存疑候选、或与官方路径深度对不上。旧的路径等值下它们只拦自己一行;当子树根
用就整棵拦,`Electronics->Camera&Photo->Accessories->DarkroomSupplies->Chemicals`
的 ID 回落成了 `Electronics > Camera & Photo`(深度 2),升成子树 = 整个相机
摄影大类全没了,**而且不报错**。所以子树与否由**清洗表的「匹配方式」列**说了算,
本工作流只执行不推断;没有这一列的老 CSV 才退回「有 ID ⇒ 子树」,且摘要报数。

⚠ 顶级类目(Books/Automotive/…)在亚马逊没有 browse node id,只能按名字拦,
   走 `-p seed=1` 灌种子(services.category_blacklist.SEED_RULES)。
"""

import csv
import logging

from registry import db
from services import category_blacklist as cb

DANGEROUS = False       # 只写 catalog.amazon_cat_blacklist 一张表

logger = logging.getLogger("workflows.category_blacklist_import")

_SOURCE = "cleanup"     # 本工作流写入的行统一打这个来源,risk_sync 不碰

_SQL_UPSERT = """
INSERT INTO catalog.amazon_cat_blacklist
    (category_norm, category_raw, match_type, match_value, browse_node_id,
     category_zh, reason, walmart_policy, enabled, source, synced_at)
VALUES (%(norm)s, %(raw)s, %(mt)s, %(mv)s, %(nid)s, %(zh)s, %(reason)s,
        %(policy)s, true, %(source)s, now())
ON CONFLICT (category_norm) DO UPDATE SET
    category_raw = EXCLUDED.category_raw, match_type = EXCLUDED.match_type,
    match_value = EXCLUDED.match_value, browse_node_id = EXCLUDED.browse_node_id,
    category_zh = EXCLUDED.category_zh, reason = EXCLUDED.reason,
    walmart_policy = EXCLUDED.walmart_policy, enabled = true,
    source = EXCLUDED.source, synced_at = now()
"""


def _seed_rows() -> list[dict]:
    from services.audit_phase0 import normalize_amazon_category as norm
    out = []
    for r in cb.SEED_RULES:
        mv = r["match_value"]
        out.append({"norm": norm(mv), "raw": mv, "mt": r["match_type"],
                    "mv": mv, "nid": r.get("browse_node_id") or None,
                    "zh": r.get("category_zh") or "", "reason": r.get("reason") or "",
                    "policy": r.get("walmart_policy") or "", "source": "seed"})
    return out


def _csv_rows(path: str) -> tuple[list[dict], dict]:
    from services.audit_phase0 import normalize_amazon_category as norm
    out, n = [], {"读入": 0, "跳过-删": 0, "跳过-无建议": 0, "跳过-匹配方式": 0,
                  "子树": 0, "顶级名": 0, "路径等值": 0, "无匹配方式列-按ID推断": 0}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            n["读入"] += 1
            advice = (row.get("建议") or "").strip()
            if not advice:
                n["跳过-无建议"] += 1
                continue
            if advice.startswith("删"):
                n["跳过-删"] += 1
                continue
            cat = (row.get("类目") or "").strip()
            if not cat:
                continue
            nid = (row.get("browse_node_id") or "").strip() or None
            how = (row.get("匹配方式") or "").strip()
            if not how:                       # 老 CSV 没这一列:退回按 ID 推断
                mt = cb.MATCH_NODE if nid else cb.MATCH_PATH
                n["无匹配方式列-按ID推断"] += 1
            elif how == "子树" and nid:
                mt = cb.MATCH_NODE
            elif how == "顶级名":
                # 亚马逊顶级 browse node 不发 ID,整顶级不做只能按名字拦
                mt, nid = cb.MATCH_TOP, None
            elif how == "路径等值":
                mt, nid = cb.MATCH_PATH, None
            else:
                # 「不录入」「(被覆盖,不单独录入)」「子树但没 ID」都落这里:
                # 净增覆盖那些行的建议也以「增」开头,只靠建议列会把它们录进去
                n["跳过-匹配方式"] += 1
                continue
            n[{cb.MATCH_NODE: "子树", cb.MATCH_TOP: "顶级名",
               cb.MATCH_PATH: "路径等值"}[mt]] += 1
            # match_value 一律存**人看的路径**(排查时 audit_why 直接显示它);
            # 子树规则真正的判据是 browse_node_id 列,不是这一列
            out.append({"norm": norm(cat), "raw": (row.get("官方完整路径") or cat).strip(),
                        "mt": mt, "mv": cat, "nid": nid,
                        "zh": (row.get("中文翻译") or "").strip(),
                        "reason": (row.get("原因") or row.get("分析结果") or "").strip()[:500],
                        "policy": "", "source": _SOURCE})
    return out, n


def run(params: dict) -> str:
    """输入:params(seed/csv/replace/execute)→ 输出:写入摘要。"""
    # ⚠ DANGEROUS=False ⇒ cli 恒给 execute=True(缺省即真跑),`--dry-run`
    # 只走 dry_run 这一路;只看 execute 的话本工作流的 --dry-run 完全失效
    execute = bool(params.get("execute")) and not params.get("dry_run")
    replace = str(params.get("replace", "")).strip() == "1"
    rows: list[dict] = []
    lines: list[str] = []

    if str(params.get("seed", "")).strip() == "1":
        rows += _seed_rows()
        lines.append(f"内置种子:{len(rows)} 条(顶级 + 子树根)")
    path = str(params.get("csv", "")).strip()
    if path:
        got, n = _csv_rows(path)
        rows += got
        lines.append(f"CSV {path}:读入 {n['读入']} 行 → 录入 {len(got)} "
                     f"(子树 {n['子树']} / 顶级名 {n['顶级名']} / "
                     f"路径等值 {n['路径等值']});"
                     f"跳过:建议为删 {n['跳过-删']}、无建议 {n['跳过-无建议']}、"
                     f"匹配方式非录入 {n['跳过-匹配方式']}")
        if n["无匹配方式列-按ID推断"]:
            lines.append(f"  ⚠ 有 {n['无匹配方式列-按ID推断']} 行没有「匹配方式」列,"
                         f"退回按 ID 推断子树 —— 回落匹配来的 ID 会整棵误拦,核一遍")
    if not rows:
        return "没给数据源:用 -p seed=1 或 -p csv=<路径>(两者可同时给)"

    # 同一 category_norm 只留最后一条(CSV 里父子同名极少,但要有确定行为)
    dedup = {r["norm"]: r for r in rows if r["norm"]}
    rows = list(dedup.values())
    n_node = sum(1 for r in rows if r["mt"] == cb.MATCH_NODE)
    n_top = sum(1 for r in rows if r["mt"] == cb.MATCH_TOP)
    lines.append(f"去重后 {len(rows)} 条:子树 {n_node} / 顶级 {n_top} / "
                 f"路径等值 {len(rows) - n_node - n_top}")

    if not execute:
        for r in rows[:8]:
            lines.append(f"  [DRY-RUN] {r['mt']:<12} {r['mv'][:56]}")
        if len(rows) > 8:
            lines.append(f"  …另有 {len(rows) - 8} 条")
        return "\n".join(lines + ["(dry-run:一行未写)"])

    with db.pg_conn() as conn:
        if replace:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM catalog.amazon_cat_blacklist "
                            "WHERE source = ANY(%s)", ([_SOURCE, "seed"],))
                n_del = cur.rowcount or 0
            lines.append(f"重录模式:先清掉同源旧行 {n_del} 条"
                         f"(飞书镜像的 source='feishu' 行**不动**)")
        with conn.cursor() as cur:
            cur.executemany(_SQL_UPSERT, rows)
        conn.commit()
        rules = cb.load(conn)
    lines.append(f"写入完成;当前生效规则:子树 {len(rules.nodes)} / "
                 f"顶级 {len(rules.tops)} / 路径等值 {len(rules.paths)}")
    return "\n".join(lines)
