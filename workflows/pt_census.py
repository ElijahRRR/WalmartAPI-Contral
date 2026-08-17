"""pt_census — 沃尔玛类目(PT)四源对账:到底哪些 PT 真实存在,谁漏了谁瞎猜。

用法:
  python cli.py pt_census                 # 摘要 + 落明细 csv
  python cli.py pt_census -p export=0     # 只看摘要不落 csv
  python cli.py pt_census -p only=瞎猜    # 只看某一类(判定列的值)

所有者 2026-08-17:「我怀疑这不是沃尔玛类目(以前做映射表时瞎猜的,需要修正)
或者沃尔玛类目从 spec 里面拿出来时不全,所以我认为应该先把沃尔玛类目全部导出来,
看实际情况后再决定怎么操作」。这条就是那份"实际情况"。

**四个源,权威性从高到低**:

  1. `spec`  `<DATA_ROOT>/specs/MP_ITEM/<版本>/_pt_index.json`
             —— **沃尔玛官方 MP_ITEM schema 拆出来的**。一个 PT 在不在这里,
             就是"沃尔玛认不认这个 PT"的终审。上架 feed 也按它校验。
  2. `meta`  `audit.walmart_pt_meta`(飞书「沃尔玛类目准入明细」镜像)
             —— 审核 R1 准入闸 / R3 认证闸**只查这张**。
  3. `tmpl`  `audit.walmart_pt_spec`(飞书「PT上传模板_汇总」同源)
             —— 字段/必填清单,与 spec 同宗但是另一次导出。
  4. `map`   `audit.walmart_category_map` 里被用作映射目标的 PT
             —— 不是"存在"的凭据,只是"谁在用"。

**判定列**(csv 里那一列,照着做的依据):

  ok        四源一致,没事
  准入漏了   spec 有、meta 没有 → **真沃尔玛 PT 但准入明细漏收**,
            后果:R1/R3 两道闸对它静默放行。处置 = 补飞书准入明细
  已废弃?   meta 有、spec 没有 → 准入明细收了一个 spec 里不存在的 PT,
            多半是沃尔玛下架了该 PT。处置 = 确认后从准入明细删,或留着不管
  瞎猜      映射在用,而 spec/meta/tmpl **三处都没有** → 当年做映射时
            把亚马逊叶子名当成沃尔玛 PT 填了。处置 = 改映射(见下)
  没人用     存在但没有任何映射指向它 —— 不是问题,只是覆盖面提示

⚠ 「瞎猜」那一类是这次排查的落点:审核 `resolve_pt` 末尾有字典闸,
解出这种 PT 会被作废判 pending —— 映射填了等于没填,而表面上"映射是有的"。

本工作流**只读**,一个字都不写库、不写飞书。
"""

import csv
import logging

from registry import db, paths
from services import pt_spec

DANGEROUS = False       # 纯只读:读 spec 文件 + 三条 SELECT

logger = logging.getLogger("workflows.pt_census")

_SQL_META = """
SELECT walmart_product_type, walmart_category, walmart_ptg,
       access_state, zh_can_do
FROM audit.walmart_pt_meta
"""
_SQL_TMPL = "SELECT walmart_product_type FROM audit.walmart_pt_spec"
_SQL_MAP = """
SELECT walmart_product_type, count(*)
FROM audit.walmart_category_map
WHERE walmart_product_type NOT IN ('无对应Walmart PT', '-', '')
GROUP BY 1
"""

_COLS = ("walmart_product_type", "判定", "在spec", "在准入明细", "在上传模板",
         "映射条数", "建议PT", "相似度", "walmart_category", "walmart_ptg",
         "access_state", "zh_can_do")

# 相似度阈值:低于它的候选不给 —— 给一个八竿子打不着的建议比不给更糟,
# 人会顺手采纳(旧仓 mp_mapper 就吃过"高置信度自动采纳"的亏)
_SUGGEST_MIN = 0.62


def _key(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def _suggest(dead: list[str], spec: set) -> dict[str, tuple[str, float]]:
    """输入:死 PT 列表 + 官方 spec PT 全集 → 输出:{死PT: (建议PT, 相似度)}。

    两级:①去标点去空白小写后**完全相等**(Paperweights → Paper Weights,
    这类是纯命名差,可以直接采纳);②difflib 最近邻,低于阈值不给。

    ⚠ **只出建议不动数据**。名字像不代表语义对:'Novelty Lights' 最像的可能是
    'Novelty Lamps',而后者也不在 spec 里;沃尔玛的 PT 粒度与亚马逊叶子并不
    一一对应,得人眼定夺。旧仓 mp_mapper 自动采纳高分候选的教训见 catmap_fix 头注。
    """
    import difflib
    by_key = {}
    for p in spec:
        by_key.setdefault(_key(p), p)
    keys = list(by_key)
    out: dict[str, tuple[str, float]] = {}
    for pt in dead:
        k = _key(pt)
        if k in by_key:                      # ① 纯命名差,直接对上
            out[pt] = (by_key[k], 1.0)
            continue
        near = difflib.get_close_matches(k, keys, n=1, cutoff=_SUGGEST_MIN)
        if near:
            out[pt] = (by_key[near[0]],
                       round(difflib.SequenceMatcher(None, k, near[0]).ratio(), 3))
    return out


def _verdict(in_spec: bool, in_meta: bool, in_tmpl: bool, n_map: int) -> str:
    if not in_spec and not in_meta and not in_tmpl:
        return "瞎猜"                      # 映射在用但三处都不存在
    if in_spec and not in_meta:
        return "准入漏了"
    if in_meta and not in_spec:
        return "已废弃?"
    if not n_map:
        return "没人用"
    return "ok"


def _census() -> tuple[list[dict], dict]:
    """输入:无 → 输出:(逐 PT 明细, 各判定计数)。"""
    try:
        spec = pt_spec.known_pts()
    except FileNotFoundError as e:
        # spec 没就位就说清楚:没有它这份对账缺最权威的一列,不能假装跑过了
        raise RuntimeError(f"沃尔玛官方 spec 未就位,本对账缺最权威那一源:{e}")
    with db.pg_conn() as conn, conn.cursor() as cur:
        cur.execute(_SQL_META)
        meta = {r[0]: r for r in cur.fetchall()}
        cur.execute(_SQL_TMPL)
        tmpl = {r[0] for r in cur.fetchall()}
        cur.execute(_SQL_MAP)
        used = {r[0]: int(r[1]) for r in cur.fetchall()}

    rows, counts = [], {}
    for pt in sorted(spec | set(meta) | tmpl | set(used)):
        m = meta.get(pt)
        v = _verdict(pt in spec, pt in meta, pt in tmpl, used.get(pt, 0))
        counts[v] = counts.get(v, 0) + 1
        rows.append({
            "walmart_product_type": pt, "判定": v,
            "在spec": "Y" if pt in spec else "",
            "在准入明细": "Y" if pt in meta else "",
            "在上传模板": "Y" if pt in tmpl else "",
            "映射条数": used.get(pt, 0),
            "建议PT": "", "相似度": "",
            "walmart_category": (m[1] if m else "") or "",
            "walmart_ptg": (m[2] if m else "") or "",
            "access_state": (m[3] if m else "") or "",
            "zh_can_do": (m[4] if m else "") or "",
        })
    # 只给"瞎猜"那一类配建议:其余几类的处置与改名无关
    sug = _suggest([r["walmart_product_type"] for r in rows
                    if r["判定"] == "瞎猜"], spec)
    for r in rows:
        hit = sug.get(r["walmart_product_type"])
        if hit:
            r["建议PT"], r["相似度"] = hit[0], hit[1]
    return rows, counts


def run(params: dict) -> str:
    only = str(params.get("only", "")).strip()
    export = str(params.get("export", "1")).strip() != "0"
    rows, counts = _census()
    if only:
        rows = [r for r in rows if r["判定"] == only]

    n_spec = sum(1 for r in rows if r["在spec"])
    lines = [f"PT 四源对账:合计 {len(rows)} 个 PT"
             f"(官方 spec {n_spec} 个)"]
    for v in ("ok", "没人用", "准入漏了", "已废弃?", "瞎猜"):
        if counts.get(v):
            lines.append(f"  {v:<6} {counts[v]:>5}")

    miss = [r for r in rows if r["判定"] == "准入漏了"]
    if miss:
        # 真 PT 但准入明细没收 → R1/R3 对它静默放行,这是闸真有洞
        lines.append("")
        lines.append(f"⚠ **准入明细漏收 {len(miss)} 个真 PT**"
                     f"(官方 spec 里有)—— R1 准入闸与 R3 认证闸对它们静默放行。"
                     f"处置:补进飞书「沃尔玛类目准入明细」")
        lines += [f"    {r['walmart_product_type']}(映射 {r['映射条数']} 条)"
                  for r in sorted(miss, key=lambda r: -r["映射条数"])[:15]]

    guess = [r for r in rows if r["判定"] == "瞎猜"]
    if guess:
        lines.append("")
        lines.append(
            f"⚠ **{len(guess)} 个 PT 三处都不存在,却被 "
            f"{sum(r['映射条数'] for r in guess)} 条映射当成目标** —— "
            f"当年把亚马逊叶子名当沃尔玛 PT 填了。这些映射是死的:"
            f"审核解出它会被字典闸作废判 pending,填了等于没填")
        exact = [r for r in guess if r["相似度"] == 1.0]
        near = [r for r in guess if r["相似度"] and r["相似度"] != 1.0]
        none_ = [r for r in guess if not r["相似度"]]
        lines.append(f"  拆三档:**纯命名差 {len(exact)}**(去标点小写后与官方 PT "
                     f"完全相等,可直接改)/ 相似 {len(near)}(要人眼定夺)/ "
                     f"找不到对应 {len(none_)}")
        for tag, grp in (("纯命名差", exact), ("相似", near), ("无对应", none_)):
            for r in sorted(grp, key=lambda r: -r["映射条数"])[:8]:
                arrow = (f" → **{r['建议PT']}**({r['相似度']})"
                         if r["建议PT"] else "")
                lines.append(f"    [{tag}] {r['walmart_product_type']}"
                             f"(映射 {r['映射条数']} 条){arrow}")
        lines.append("  处置三选一:①改成 spec 里真实存在的 PT(csv 的「建议PT」"
                     "列是机器给的候选,**只是候选**——名字像不代表语义对,"
                     "沃尔玛 PT 粒度与亚马逊叶子不一一对应,得你过目);"
                     "②确认无对应就把映射的 PT 改成「无对应Walmart PT」"
                     "(审核会明确判没类目,而不是含糊 pending);③删掉那几行映射")

    if not export:
        return "\n".join(lines)
    paths.reports_dir().mkdir(parents=True, exist_ok=True)
    p = paths.reports_dir() / "pt_census.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(_COLS))
        w.writeheader()
        w.writerows(rows)
    lines += ["", f"明细 {len(rows)} 行 → {p}",
              "  (utf-8-sig,Excel/飞书直接打开;按「判定」列筛选照着做)"]
    return "\n".join(lines)
