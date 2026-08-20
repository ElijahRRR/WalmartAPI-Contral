"""spec_split — 把官方 450MB 单文件 MP_ITEM spec 拆成按 PT 的目录。

用法:
  python cli.py spec_split -p src=~/Downloads/5.0.2026xxxx-..._MP_ITEM_0_0_en.json --dry-run
  python cli.py spec_split -p src=<同上>                  # 真拆(目录自动按版本串命名)
  python cli.py spec_split -p src=<同上> -p out=<目录>      # 指定输出目录
  python cli.py spec_split -p src=<同上> -p diff=1          # 拆完与现用版做差集

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


def run(params: dict) -> str:
    """输入:params(src/out/replace/diff/dry_run)→ 输出:拆分与自检摘要。"""
    dry_run = bool(params.get("dry_run"))
    src = os.path.expanduser(str(params.get("src", "")).strip())
    if not src:
        return "没给源文件:用 -p src=<官方 MP_ITEM spec 大 json>"
    if not os.path.exists(src):
        raise FileNotFoundError(f"源文件不存在:{src}")
    size = os.path.getsize(src)
    ver = _version_from(src)
    lines = [f"源文件:{src}({size / 1024 / 1024:.0f} MB)",
             f"版本串:{ver or '(文件名里解析不出,输出目录必须用 -p out= 指定)'}"]

    out = str(params.get("out", "")).strip()
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

    # 自检:换个进程视角,用真加载器把新目录读一遍
    pt_spec.use_spec_dir(out_dir)
    try:
        n_idx, n_ok = pt_spec.coverage()
        lines.append(f"自检:索引 {n_idx} 个 PT,拆分文件解析到 {n_ok} 个"
                     + ("" if n_idx == n_ok else " ⚠ 有对不上的,别切换"))
        if str(params.get("diff", "")).strip() == "1":
            new_pts = pt_spec.known_pts()
            new_req = {p: set((pt_spec.load_pt(p) or {}).get("required") or [])
                       for p in new_pts}
            pt_spec.use_spec_dir(cur_dir)
            old_pts = pt_spec.known_pts()
            old_req = {p: set((pt_spec.load_pt(p) or {}).get("required") or [])
                       for p in old_pts}
            lines.append(f"── 换版差集:在用版 {resources.FEED_SPEC_VERSIONS['MP_ITEM']}"
                         f" {len(old_pts)} 个 PT → 新版 {len(new_pts)} 个")
            lines.append(f"   新增 PT {len(new_pts - old_pts)} 个;"
                         f"消失 PT {len(old_pts - new_pts)} 个")
            added: dict[str, int] = {}
            removed: dict[str, int] = {}
            for p in new_pts & old_pts:
                for f_ in new_req[p] - old_req[p]:
                    added[f_] = added.get(f_, 0) + 1
                for f_ in old_req[p] - new_req[p]:
                    removed[f_] = removed.get(f_, 0) + 1
            lines.append("   **新版新增的顶层必填**(按影响 PT 数;mapper 给不出的"
                         "会被 validate 拦,上架量静悄悄掉):")
            for f_, n in sorted(added.items(), key=lambda x: -x[1])[:20] or [("(无)", 0)]:
                lines.append(f"     {f_:<52}{n:>6} 个 PT")
            if removed:
                lines.append("   新版不再必填的:")
                for f_, n in sorted(removed.items(), key=lambda x: -x[1])[:10]:
                    lines.append(f"     {f_:<52}{n:>6} 个 PT")
    finally:
        pt_spec.use_spec_dir(None)      # 绝不把进程留在新版上
    lines.append("下一步:核完差集再改 registry.FEED_SPEC_VERSIONS['MP_ITEM'] 切换;"
                 "feed header 的 version 与 spec 目录同源,改一处两边一起变")
    return "\n".join(lines)
