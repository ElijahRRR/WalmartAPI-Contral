"""spec_split — 把官方 450MB 单文件 MP_ITEM spec 拆成按 PT 的目录。

用法:
  python cli.py spec_split -p src=~/Downloads/5.0.2026xxxx-..._MP_ITEM_0_0_en.json --dry-run
  python cli.py spec_split -p src=<同上>                  # 真拆(目录自动按版本串命名)
  python cli.py spec_split -p src=<同上> -p out=<目录>      # 指定输出目录
  python cli.py spec_split -p src=<同上> -p diff=1          # 拆完与现用版做差集
  python cli.py spec_split -p out=<已拆好的目录> -p diff=1   # 只对账(已拆过就不重拆)

为什么要它(docs/legacy_survey.md:1535/1665):官方 MP_ITEM v5 是**一个 450MB 的
单 JSON**,`json.load` 膨胀成约 1.3GB Python 对象 —— 旧系统跑 5048 行 xlsx 时
RSS 飙到 12GB 直接 OOM。旧仓用 `tools/split_mp_item_spec.py` 拆成一目录小文件,
**加载器迁过来了(services/pt_spec),拆分工具没有**,于是换版就卡在这一步。

拆分走 `services.spec_split`:mmap + 括号配对,**整份 JSON 从不变成 Python 对象**,
峰值内存约等于最大的那个 PT 片段。

安全:
  · 目标目录默认按**源文件名里的版本串**新建,与在用的那份**并排放**,
    不覆盖(上架链此刻正读着在用的那份,拆坏了就上不了架);
  · 目录已存在且非空 → 拒绝,要覆盖得显式 `-p replace=1`;
  · 拆完自检:PT 数、Orderable 必填、`_pt_index` 能否逐个回读。
  · `-p diff=1` 与现用版做差集 —— **换版前必须看的那个数**:新版新增了哪些
    顶层必填,mapper 给不出的那些会被 mp_conform.validate 拦下,上架量
    **静悄悄地掉**(validate 不过就不提交,省 UPC 与配额,设计如此)。
"""

import json
import logging
import mmap
import os
import re

from registry import paths, resources
from services import pt_spec, spec_split as ss

DANGEROUS = False       # 只读源文件 + 只写新建的 spec 目录,不碰沃尔玛也不碰库

logger = logging.getLogger("workflows.spec_split")

# 旧仓命名:5.0.20260304-22_45_32-api_MP_ITEM_0_0_en.json
_VER_RE = re.compile(r"(\d+\.\d+\.\d{8}-\d{2}_\d{2}_\d{2}-api)")


def _version_from(src: str) -> str:
    m = _VER_RE.search(os.path.basename(src))
    return m.group(1) if m else ""


def _selfcheck_lines(out_dir: str) -> list[str]:
    """输入:新拆目录 → 输出:自检行(用**真加载器**读一遍,不是自己数文件)。"""
    pt_spec.use_spec_dir(out_dir)
    try:
        n_idx, n_ok = pt_spec.coverage()
        return [f"自检:索引 {n_idx} 个 PT,拆分文件解析到 {n_ok} 个"
                + ("" if n_idx == n_ok else " ⚠ 有对不上的,别切换")]
    finally:
        pt_spec.use_spec_dir(None)


def _diff_lines(new_dir: str, cur_dir: str) -> list[str]:
    """输入:新旧两个 spec 目录 → 输出:换版差集(**换版前唯一必看的那张表**)。

    mapper 给不出的新增必填会被 mp_conform.validate 拦下,上架量**静悄悄地掉**
    (validate 不过就不提交,省 UPC 与配额,设计如此)。所以按"影响多少个 PT"
    排序报出来,让人一眼看出改哪几个字段能覆盖大头。

    ⚠ 无论中途出什么事,最后都必须把加载器**恢复到在用版** —— 留在新版上,
    上架链就会拿新版数据去过旧版 header 的校验。
    """
    def _snapshot(d: str) -> tuple[set, dict, dict]:
        pt_spec.use_spec_dir(d)
        pts = pt_spec.known_pts()
        req = {p: set((pt_spec.load_pt(p) or {}).get("required") or []) for p in pts}
        o = pt_spec.orderable_spec()
        return pts, req, {"req": set(o.get("required") or []),
                          "props": set(o.get("properties") or {})}

    try:
        new_pts, new_req, new_o = _snapshot(new_dir)
        old_pts, old_req, old_o = _snapshot(cur_dir)
    finally:
        pt_spec.use_spec_dir(None)

    out = [f"── 换版差集:在用版 {resources.FEED_SPEC_VERSIONS['MP_ITEM']}"
           f" {len(old_pts)} 个 PT → 新版 {len(new_pts)} 个",
           f"   新增 PT {len(new_pts - old_pts)} 个;消失 PT {len(old_pts - new_pts)} 个"]
    for tag, xs in (("新增样例", sorted(new_pts - old_pts)[:5]),
                    ("消失样例", sorted(old_pts - new_pts)[:5])):
        if xs:
            out.append(f"   {tag}:" + "、".join(xs))
    o_add, o_del = new_o["props"] - old_o["props"], old_o["props"] - new_o["props"]
    out.append(f"   Orderable:字段 {len(old_o['props'])} → {len(new_o['props'])};"
               f"必填 {len(old_o['req'])} → {len(new_o['req'])}")
    if o_add or o_del:
        out.append(f"     新增字段 {sorted(o_add) or '—'};移除字段 {sorted(o_del) or '—'}")
    if new_o["req"] - old_o["req"] or old_o["req"] - new_o["req"]:
        out.append(f"     必填变化:新增 {sorted(new_o['req'] - old_o['req']) or '—'};"
                   f"不再必填 {sorted(old_o['req'] - new_o['req']) or '—'}")

    added: dict[str, int] = {}
    removed: dict[str, int] = {}
    changed = 0
    for p in new_pts & old_pts:
        a, d = new_req[p] - old_req[p], old_req[p] - new_req[p]
        if a or d:
            changed += 1
        for f_ in a:
            added[f_] = added.get(f_, 0) + 1
        for f_ in d:
            removed[f_] = removed.get(f_, 0) + 1
    out.append(f"   顶层必填有变化的 PT:{changed} 个")
    out.append("   **新版新增的顶层必填**(按影响 PT 数;mapper 给不出的会被"
               " validate 拦,上架量静悄悄掉):")
    for f_, n in sorted(added.items(), key=lambda x: -x[1])[:25] or [("(无)", 0)]:
        out.append(f"     {f_:<52}{n:>6} 个 PT")
    if removed:
        out.append("   新版不再必填的(可以少填,不影响能不能上):")
        for f_, n in sorted(removed.items(), key=lambda x: -x[1])[:15]:
            out.append(f"     {f_:<52}{n:>6} 个 PT")
    return out


def run(params: dict) -> str:
    """输入:params(src/out/replace/diff/dry_run)→ 输出:拆分与自检摘要。"""
    dry_run = bool(params.get("dry_run"))
    src = os.path.expanduser(str(params.get("src", "")).strip())
    out = str(params.get("out", "")).strip()
    want_diff = str(params.get("diff", "")).strip() == "1"
    # 已经拆好的目录 + 只想对账:不重拆(重拆要 -p replace=1,而覆盖一份已经
    # 自检通过的目录纯属多余风险)
    only_diff = bool(out and want_diff and not src
                     and os.path.exists(os.path.expanduser(os.path.join(out, "_pt_index.json"))))
    if not src and not only_diff:
        return ("没给源文件:用 -p src=<官方 MP_ITEM spec 大 json>;"
                "已经拆好了只想对账用 -p out=<目录> -p diff=1")
    lines = []
    if only_diff:
        out_dir = os.path.expanduser(out)
        cur_dir = str(paths.mp_item_spec_dir())
        lines.append(f"只对账(目录已拆好,不重拆):{out_dir}")
        return "\n".join(lines + _diff_lines(out_dir, cur_dir))

    if not os.path.exists(src):
        raise FileNotFoundError(f"源文件不存在:{src}")
    size = os.path.getsize(src)
    ver = _version_from(src)
    lines += [f"源文件:{src}({size / 1024 / 1024:.0f} MB)",
              f"版本串:{ver or '(文件名里解析不出,输出目录必须用 -p out= 指定)'}"]

    if out:
        out_dir = os.path.expanduser(out)
    elif ver:
        out_dir = str(paths.specs_dir() / "MP_ITEM" / ver)
    else:
        raise ValueError("文件名里解析不出版本串,请用 -p out=<目录> 指定输出目录")
    cur_dir = str(paths.mp_item_spec_dir())
    if os.path.abspath(out_dir) == os.path.abspath(cur_dir):
        raise ValueError(
            f"拒绝写进**上架链正在用的那份 spec 目录**({cur_dir})——"
            f"新版要与在用版并排放,验完再改 registry 的版本串切换")
    lines.append(f"输出目录:{out_dir}"
                 + ("(与在用版并排,registry 不动)" if not dry_run else ""))

    with open(src, "rb") as f:
        buf = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            pt_span = ss.walk_path(buf, ss.PT_PATH)
            members = [(k, vs, ve) for k, vs, ve in ss.iter_members(buf, pt_span[0])]
            lines.append(f"发现 PT {len(members)} 个"
                         f"(路径 {'.'.join(ss.PT_PATH)});样例:"
                         + "、".join(k for k, _, _ in members[:3]))
            ord_obj = ss.slice_json(buf, ss.walk_path(buf, ss.ORDERABLE_PATH))
            try:
                head_obj = ss.slice_json(buf, ss.walk_path(buf, ss.HEADER_PATH))
            except KeyError:
                head_obj = {}
                lines.append("  ⚠ 找不到 MPItemFeedHeader 段,_header.json 留空"
                             "(旧仓产物里有这一份,不影响加载器)")
            o_req = list(ord_obj.get("required") or [])
            o_props = list(ord_obj.get("properties") or {})
            lines.append(f"Orderable:字段 {len(o_props)} / 必填 {len(o_req)};"
                         f"必填 = {'、'.join(sorted(o_req))}")

            if dry_run:
                big = max(members, key=lambda m: m[2] - m[1])
                lines.append(f"最大的 PT 片段:{big[0]}"
                             f"({(big[2] - big[1]) / 1024 / 1024:.1f} MB)"
                             f" —— 真跑时的内存峰值约等于它,与文件总大小无关")
                lines.append("(dry-run:只做结构发现与自检,一个文件都没写)")
                return "\n".join(lines)

            if os.path.isdir(out_dir) and os.listdir(out_dir):
                if str(params.get("replace", "")).strip() != "1":
                    raise FileExistsError(
                        f"目标目录非空:{out_dir}。确认要覆盖加 -p replace=1")
            os.makedirs(out_dir, exist_ok=True)
            index: dict[str, str] = {}
            for k, vs, ve in members:
                fn = ss.safe_filename(k)
                # 同名冲突:落盘覆盖会**静默少一个 PT**,索引却指着同一个文件
                while fn in index.values():
                    fn = fn[:-5] + "_2.json"
                with open(os.path.join(out_dir, fn), "wb") as g:
                    g.write(buf[vs:ve])
                index[k] = fn
            for name, obj in (("_pt_index.json", index),
                              ("_orderable.json", ord_obj),
                              ("_header.json", head_obj)):
                with open(os.path.join(out_dir, name), "w", encoding="utf-8") as g:
                    json.dump(obj, g, ensure_ascii=False)
            lines.append(f"已写 {len(index)} 个 PT 文件 + _pt_index/_orderable/_header")
        finally:
            buf.close()

    lines += _selfcheck_lines(out_dir)
    if want_diff:
        lines += _diff_lines(out_dir, cur_dir)
    lines.append("下一步:核完差集再改 registry.FEED_SPEC_VERSIONS['MP_ITEM'] 切换;"
                 "feed header 的 version 与 spec 目录同源,改一处两边一起变")
    return "\n".join(lines)
