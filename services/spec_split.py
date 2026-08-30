"""MP_ITEM 官方大 spec 的**流式**拆分件(纯函数,零依赖,不整份进内存)。

背景(docs/legacy_survey.md:1535/1665,旧仓 tools/split_mp_item_spec.py):
官方 MP_ITEM v5 是**一个 450MB 的单 JSON**,`json.load` 会膨胀成约 1.3GB 的
Python 对象 —— 旧系统跑 5048 行 xlsx 时 RSS 飙到 12GB 直接 OOM。解法是按 PT
拆成一目录小文件 + lru_cache,加载器(services/pt_spec)迁过来了,**拆分工具没有**。

所以这里重写拆分,原则是**从头到尾不把整份 JSON 变成 Python 对象**:
  · 用 mmap 把文件当字节看,自己做括号配对,只切出**原始字节切片**;
  · 只有切出来的小片段(单个 PT / Orderable / Header)才 json.loads;
  · 峰值内存 ≈ 最大的那个 PT 片段,和文件总大小无关。

结构发现而不是结构假定:官方层级名历年会变,所以按路径找不到就**报出每一层
实际有哪些键**,让人照着改,而不是返回空(返回空 = "官方一个 PT 都没有",
下游会当真)。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("services.spec_split")

# MP_ITEM v5 的 PT 容器路径(2026-08-20 按旧仓拆分产物反推)。
# 找不到会连每层的实际键名一起报出来,不静默。
PT_PATH = ("properties", "MPItem", "items", "properties", "Visible", "properties")
ORDERABLE_PATH = ("properties", "MPItem", "items", "properties", "Orderable")
HEADER_PATH = ("properties", "MPItemFeedHeader")

_WS = b" \t\r\n"


def _skip_ws(buf, i: int) -> int:
    n = len(buf)
    while i < n and buf[i] in _WS:
        i += 1
    return i


def _scan_string(buf, i: int) -> int:
    """输入:开引号位置 → 输出:闭引号之后的位置(处理转义)。"""
    j = i + 1
    n = len(buf)
    while j < n:
        c = buf[j]
        if c == 0x5C:          # 反斜杠:跳过被转义的那个字节
            j += 2
            continue
        if c == 0x22:          # 闭引号
            return j + 1
        j += 1
    raise ValueError(f"字符串未闭合(起始 {i})")


def scan_value(buf, i: int) -> int:
    """输入:值的首字节位置 → 输出:值的结束位置(exclusive)。

    对象/数组做**括号配对**,并且跳过字符串内部的括号 —— 不跳的话
    spec 里任何一句带 `}` 的描述文本都会让整份切错位,而且切出来的还是
    合法 JSON 片段,错得看不出来。
    """
    c = buf[i:i + 1]
    if c == b'"':
        return _scan_string(buf, i)
    if c in (b"{", b"["):
        depth, j, n = 0, i, len(buf)
        while j < n:
            ch = buf[j]
            if ch == 0x22:
                j = _scan_string(buf, j)
                continue
            if ch in (0x7B, 0x5B):        # { [
                depth += 1
            elif ch in (0x7D, 0x5D):      # } ]
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        raise ValueError(f"对象/数组未闭合(起始 {i})")
    j, n = i, len(buf)
    while j < n and buf[j] not in b",}] \t\r\n":
        j += 1
    return j


def iter_members(buf, i: int):
    """输入:`{` 的位置 → 输出:逐个 (键, 值起点, 值终点),不解析值。"""
    if buf[i:i + 1] != b"{":
        raise ValueError(f"位置 {i} 不是对象起点")
    j = _skip_ws(buf, i + 1)
    if buf[j:j + 1] == b"}":
        return
    while True:
        j = _skip_ws(buf, j)
        kend = _scan_string(buf, j)
        key = json.loads(buf[j:kend].decode("utf-8"))
        j = _skip_ws(buf, kend)
        if buf[j:j + 1] != b":":
            raise ValueError(f"键 {key!r} 后缺冒号(位置 {j})")
        j = _skip_ws(buf, j + 1)
        vend = scan_value(buf, j)
        yield key, j, vend
        j = _skip_ws(buf, vend)
        if buf[j:j + 1] == b",":
            j += 1
            continue
        return


def find_member(buf, obj_start: int, key: str):
    """输入:对象起点 + 键名 → 输出:(值起点, 值终点) 或 None。"""
    for k, vs, ve in iter_members(buf, obj_start):
        if k == key:
            return vs, ve
    return None


def walk_path(buf, path) -> tuple[int, int]:
    """输入:键路径 → 输出:(值起点, 值终点);走不通就**带着实际键名报错**。

    静默返回 None 的代价:调用方会当成"这一层没有内容",于是拆出 0 个 PT
    还一切正常。官方改层级名是常事,报出实际键名才能一眼改对。
    """
    start = _skip_ws(buf, 0)
    cur = (start, len(buf))
    for depth, key in enumerate(path):
        got = find_member(buf, cur[0], key)
        if got is None:
            keys = [k for k, _, _ in iter_members(buf, cur[0])][:20]
            raise KeyError(
                f"spec 结构对不上:走到第 {depth + 1} 层找不到键 {key!r}"
                f"(路径 {'.'.join(path[:depth + 1])});这一层实际有:{keys}")
        cur = got
    return cur


def slice_json(buf, span: tuple[int, int]):
    """输入:(起, 止) → 输出:该片段 json.loads 后的对象(只解析这一片)。"""
    return json.loads(buf[span[0]:span[1]].decode("utf-8"))


def safe_filename(pt: str) -> str:
    """输入:PT 名 → 输出:可落盘的文件名。

    加载器 `services.pt_spec` 是**按索引里的文件名**取的,取不到才回退按
    规范化名匹配,所以清洗规则怎么定都行,只要索引里记的是真名。
    这里保留字母数字与空格,其余换 `_`(与旧仓产物观感一致)。
    """
    out = "".join(ch if (ch.isalnum() or ch in " -&,.") else "_" for ch in pt)
    return out.strip().rstrip(".") + ".json"


__all__ = ["PT_PATH", "ORDERABLE_PATH", "HEADER_PATH", "iter_members",
           "find_member", "walk_path", "scan_value", "slice_json", "safe_filename"]
