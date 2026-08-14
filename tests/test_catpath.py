"""类目路径对齐测试(所有者实例 2026-08-13 为回归向量)。

Amazon 的 slug / 面包屑 / Best Sellers 导航三套名称不完全一致,中间层
节点名有别名漂移——精确等值会把已映射路径误判成缺口。
"""

import pytest

from services import catpath
from workflows.catmap_align import build_leaf_index

# 所有者提供的两条真实漂移(左=产品面包屑,右=另一套名称)
OWNER_CASES = [
    ("Home & Kitchen > Home Décor Products > Photo Albums, Frames & "
     "Accessories > Picture Frames > Wall & Tabletop Frames",
     "Home & Kitchen > Home Décor > Photo Albums, Frames & Accessories > "
     "Picture Frames > Wall & Tabletop Frames"),
    ("Patio, Lawn & Garden > Outdoor Power Tools > Replacement Parts & "
     "Accessories > Lawn Mower Parts & Accessories > Lawn Mower Replacement "
     "Parts > Belts",
     "Patio, Lawn & Garden > Mowers & Outdoor Power Tools > Replacement "
     "Parts & Accessories > Lawn Mower Parts & Accessories > Lawn Mower "
     "Replacement Parts > Belts"),
]

# 全树叫 'Belts' 的叶子实测 27 个,这几条是最容易串类的邻居
BELT_DECOYS = [
    "Clothing, Shoes & Jewelry > Men > Accessories > Belts",
    "Automotive > Replacement Parts > Belts, Hoses & Pulleys > Belts",
    "Home & Kitchen > Vacuums & Floor Care > Vacuum Parts & Accessories > "
    "Replacement Batteries & Parts > Belts",
    "Industrial & Scientific > Power Transmission Products > Belts",
]


@pytest.mark.parametrize("breadcrumb,canonical", OWNER_CASES)
def test_owner_real_drift_aligns(breadcrumb, canonical):
    """两条实例都必须对齐,且分数达标(实测 0.80 / 0.83)。"""
    best, score, status = catpath.align_path(breadcrumb, [canonical])
    assert status == "aligned" and best == canonical
    assert score >= catpath.MIN_SCORE


def test_leaf_decoys_do_not_cross_category():
    """顶级闸挡串类:同名叶子 'Belts' 的其他顶级候选一个都不许中。"""
    breadcrumb, canonical = OWNER_CASES[1]
    best, _s, status = catpath.align_path(breadcrumb, BELT_DECOYS + [canonical])
    assert status == "aligned" and best == canonical
    # 只给诱饵、不给正确答案 → 必须 no_match(宁缺勿滥)
    best2, _s2, status2 = catpath.align_path(breadcrumb, BELT_DECOYS)
    assert best2 is None and status2 == "no_match"


def test_ambiguous_not_guessed():
    """两个候选并列(分差 < margin)→ 判歧义交人工,绝不猜。"""
    path = "A > B > C > Leaf"
    cands = ["A > X > C > Leaf", "A > Y > C > Leaf"]
    best, _score, status = catpath.align_path(path, cands)
    assert best is None and status == "ambiguous"


def test_leaf_must_match_exactly():
    """叶子不同 = 不同类目,再像也不对齐。"""
    best, _s, status = catpath.align_path(
        "A > B > Picture Frames", ["A > B > Picture Frame"])
    assert best is None and status == "no_match"


def test_norm_only_whitespace_and_case():
    """归一只做压空白 + casefold:标点/&/重音一律保留(不做同义词)。"""
    assert catpath.norm_seg("  Home   &  Kitchen ") == "home & kitchen"
    assert catpath.norm_seg("Home Décor") != catpath.norm_seg("Home Decor")
    assert catpath.segments("A >  B > \n C ") == ["A", "B", "C"]
    assert catpath.segments("") == []


def test_overlap_score_penalizes_length_gap():
    """短路径全含于长路径也不给满分(对 max(len) 归一)。"""
    assert catpath.overlap_score(["A", "B"], ["A", "B"]) == 1.0
    assert catpath.overlap_score(["A", "B"], ["A", "B", "C", "D"]) == 0.5
    assert catpath.overlap_score([], ["A"]) == 0.0


def test_single_segment_path_never_aligns():
    """单段路径(顶级本身)没有叶子语义,不参与对齐。"""
    best, _s, status = catpath.align_path("Home & Kitchen", ["Home & Kitchen"])
    assert best is None and status == "no_match"


def test_decide_alias_evidence_beats_score():
    """实证 PT 优先于字符串相似度(所有者首跑实测:0.60 分把装饰牌匾折进
    '季节装饰>花环挂钩',单靠调阈值会误伤 0.67 的合法浅路径)。"""
    from workflows.catmap_align import decide_alias
    # 实证一致 → 低分也收
    assert decide_alias(0.55, "GoodPT", "GoodPT") == "verified"
    # 实证相左 → 高分也拒
    assert decide_alias(0.95, "GoodPT", "OtherPT") == "pt_conflict"
    # 无实证 → 看分数
    assert decide_alias(0.80, None, "GoodPT") == "aligned"
    assert decide_alias(0.60, None, "GoodPT") == "low_score"
    # canonical 侧 PT 不唯一(映射表分流)→ 无从背书,退回看分数
    assert decide_alias(0.60, "GoodPT", None) == "low_score"


def test_consensus_pt_requires_unanimity_and_support():
    from workflows.catmap_align import consensus_pt
    assert consensus_pt({"A": 5}) == "A"
    assert consensus_pt({"A": 1}) is None            # 单证不立
    assert consensus_pt({"A": 9, "B": 1}) is None    # 分流无共识
    assert consensus_pt({}) is None


def test_build_leaf_index_groups_by_leaf():
    idx = build_leaf_index([c for _b, c in OWNER_CASES] + BELT_DECOYS)
    assert len(idx["belts"]) == 5          # 1 正确 + 4 诱饵
    assert idx["wall & tabletop frames"] == [OWNER_CASES[0][1]]
    assert build_leaf_index([""]) == {}
