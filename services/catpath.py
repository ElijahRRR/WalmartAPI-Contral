"""类目路径对齐积木(纯函数;零 DB 零 LLM)。

所有者发现 2026-08-13:Amazon 的 URL slug / 商品面包屑 / Best Sellers 导航
三套名称并不完全一致,**中间层节点名有别名漂移**,叶子与顶级通常一致:

  面包屑 `Home & Kitchen > Home Décor Products > … > Wall & Tabletop Frames`
  另一套 `Home & Kitchen > Home Décor         > … > Wall & Tabletop Frames`
  面包屑 `Patio, Lawn & Garden > Outdoor Power Tools          > … > Belts`
  另一套 `Patio, Lawn & Garden > Mowers & Outdoor Power Tools > … > Belts`

按完整路径精确等值匹配 ⇒ 把这些**已映射**的路径误判成缺口(catmap 缺口
7,512 条里有多少是假缺口,catmap_align 跑一遍就知道)。

对齐算法(align_path):三道闸,宁缺勿滥
  1. **叶子必须相等**(归一后)——叶子是类目身份,不同叶子绝不是同一类;
  2. **顶级必须相等**——全树叫 'Belts' 的叶子实测 27 个(男装腰带/汽车
     皮带/柔道带/砂带…),没有顶级闸必然串类;
  3. 段集重叠打分 ≥ min_score,且**最佳与次佳拉开 margin**,否则判歧义
     交人工——并列不猜。

归一(norm_seg):去首尾空白、压缩内部空白、casefold。**不动标点、不去
'&'、不做同义词**——那属于人工规则,机器只做形态归一。
"""

from __future__ import annotations

MIN_SCORE = 0.5      # 段集重叠下限(两例实测 0.80 / 0.83)
MARGIN = 0.1         # 最佳与次佳的最小差距(并列即歧义)


def norm_seg(seg: str) -> str:
    """输入:路径段 → 输出:归一形态(压空白 + casefold)。"""
    return " ".join((seg or "").split()).casefold()


def segments(path: str) -> list[str]:
    """输入:'A > B > C' → 输出:['A', 'B', 'C'](原文,仅去空白段)。

    分隔符只认 '>'(面包屑与映射表共用口径;换行/多空格容错由 split 吸收)。
    """
    return [s.strip() for s in (path or "").split(">") if s.strip()]


def overlap_score(a_segs: list[str], b_segs: list[str]) -> float:
    """输入:两条路径的段列表 → 输出:段集重叠比 0~1(对长者归一)。

    用集合而非序列:漂移多表现为"某中间层改名/增删一层",段序不变但长度
    可能差 1;对 max(len) 归一保证长度差异会拉低分数(不会因短路径全含于
    长路径就误判满分)。
    """
    if not a_segs or not b_segs:
        return 0.0
    a = {norm_seg(s) for s in a_segs}
    b = {norm_seg(s) for s in b_segs}
    return len(a & b) / max(len(a), len(b))


def diff_profile(a_segs: list[str], b_segs: list[str]) -> tuple[int, bool]:
    """输入:两条路径的段列表 → 输出:(最大未匹配段数, 父节点是否相同)。

    纯函数。比例分对**浅路径不公平**(3 段路径差 1 段就掉到 0.67,而 6 段
    差 1 段有 0.83),但浅路径的一段之差往往正是合法改名
    ('Event & Party Supplies' → 'Party Supplies')。改数"差几段"更贴事实。

    父节点(倒数第二段)是否相同是**危险信号的分水岭**(所有者首跑实测):
      `Team Sports > Soccer   > Training…` vs
      `Team Sports > Lacrosse > Training…`  只差一段却是完全不同的运动,
    差异恰在叶子的直接父节点上;而合法漂移的差异段通常在更上层
    (`Home Décor Products` vs `Home Décor`,父节点 `Picture Frames` 不变)。
    """
    a = [norm_seg(s) for s in a_segs]
    b = [norm_seg(s) for s in b_segs]
    sa, sb = set(a), set(b)
    unmatched = max(len(sa - sb), len(sb - sa))
    parent_same = (len(a) >= 2 and len(b) >= 2 and a[-2] == b[-2])
    return unmatched, parent_same


def align_tier(a_segs: list[str], b_segs: list[str]) -> str:
    """输入:两条路径的段列表 → 输出:结构信任层 strong / medium / weak。

    纯函数。strong = 只差一段**且**差异在上层(父节点未变)→ 结构上就是
    "某一层改名",可自动折;medium = 只差一段但父节点也变了(浅路径改名
    或父节点被换成同级的另一个类目)→ **必须有实证 PT 背书**才折;
    weak = 差两段以上 → 一律交人工。
    """
    unmatched, parent_same = diff_profile(a_segs, b_segs)
    if unmatched > 1:
        return "weak"
    return "strong" if parent_same else "medium"


def align_path(path: str, candidates: list[str], *,
               min_score: float = MIN_SCORE,
               margin: float = MARGIN) -> tuple[str | None, float, str]:
    """输入:待对齐路径 + 候选路径列表 → 输出:(对齐到的路径|None, 分数, 状态)。

    candidates 由调用方按**叶子**预筛(索引在库侧/内存侧建,本函数不查库)。
    状态:aligned / no_match(叶子或顶级对不上、分过低)/ ambiguous(并列)。
    """
    segs = segments(path)
    if len(segs) < 2:
        return None, 0.0, "no_match"
    leaf, top = norm_seg(segs[-1]), norm_seg(segs[0])

    scored: list[tuple[float, str]] = []
    for cand in candidates:
        c_segs = segments(cand)
        if len(c_segs) < 2:
            continue
        if norm_seg(c_segs[-1]) != leaf:      # 闸 1:叶子必须相等
            continue
        if norm_seg(c_segs[0]) != top:        # 闸 2:顶级必须相等(防串类)
            continue
        scored.append((overlap_score(segs, c_segs), cand))
    if not scored:
        return None, 0.0, "no_match"

    scored.sort(key=lambda x: (-x[0], x[1]))   # 分数降序,同分按路径稳定排序
    best_score, best = scored[0]
    if best_score < min_score:
        return None, best_score, "no_match"
    if len(scored) > 1 and best_score - scored[1][0] < margin:
        return None, best_score, "ambiguous"   # 闸 3:并列不猜,交人工
    return best, best_score, "aligned"


def leaf_key(path: str) -> str | None:
    """输入:路径 → 输出:归一后的叶子段(建索引用);无有效段 → None。"""
    segs = segments(path)
    return norm_seg(segs[-1]) if segs else None


__all__ = ["MIN_SCORE", "MARGIN", "norm_seg", "segments", "overlap_score",
           "align_path", "leaf_key"]
