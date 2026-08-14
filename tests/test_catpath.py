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


def test_align_tier_structure():
    """结构层:差一段且父节点未变=strong;父节点也变=medium;差两段=weak。
    比例分对浅路径不公平(3 段差 1 段只有 0.67),但那正是合法改名。"""
    seg = catpath.segments
    # 所有者两条真实漂移:差异在上层,父节点未变 → strong
    for breadcrumb, canonical in OWNER_CASES:
        assert catpath.align_tier(seg(breadcrumb), seg(canonical)) == "strong"
    # 浅路径改名(Party Packs / Vases):父节点即被改名段 → medium
    assert catpath.align_tier(
        seg("Home & Kitchen > Event & Party Supplies > Party Packs"),
        seg("Home & Kitchen > Party Supplies > Party Packs")) == "medium"
    # 危险:只差一段,但差在叶子的直接父节点(足球 vs 长曲棍球)→ medium
    assert catpath.align_tier(
        seg("Sports > Team Sports > Soccer > Training Equipment"),
        seg("Sports > Team Sports > Lacrosse > Training Equipment")) == "medium"
    # 差两段以上(装饰牌匾串进季节装饰/花环挂钩)→ weak
    assert catpath.align_tier(
        seg("H & K > Home Décor Products > Home Décor Accents > "
            "Decorative Accessories > Signs"),
        seg("H & K > Seasonal Décor > Wreath Hangers > "
            "Decorative Accessories > Signs")) == "weak"


def test_taxonomy_parse_rows():
    """对账版类目树三段全收;'L1_xxx' 根级占位父存 NULL;同 node 先到先得
    (leaves 段权威);'是否叶子'=='是' 才算叶。"""
    from workflows.taxonomy_import import parse_rows
    data = {
        "leaves": [{
            "browse_node_id": "121475272011", "类目名": "Amazon Device Subscriptions",
            "完整路径": "Amazon Devices & Accessories > Amazon Device Subscriptions",
            "深度": "2", "父节点ID": "L1_amazon-devices", "是否叶子": "是",
            "L1 根类目": "Amazon Devices & Accessories", "产品样本数": "0"}],
        "verified_added_paths": [{
            "browse_node_id": "553220", "类目名": "Handsaws",
            "完整路径": "Tools & Home Improvement > … > Handsaws",
            "深度": "4", "父节点ID": "551238", "是否叶子": "否",
            "L1 根类目": "Tools & Home Improvement", "产品样本数": "12"}],
        "unverified_new_nodes": [
            {"browse_node_id": "121475272011", "类目名": "重复应被丢弃"},
            {"browse_node_id": "", "类目名": "无 ID 应跳过"},
            {"browse_node_id": "999", "类目名": "新 node", "父节点ID": "553220"}],
    }
    rows, stat = parse_rows(data)
    assert stat == {"leaves": 1, "verified_added_paths": 1,
                    "unverified_new_nodes": 1, "skipped": 1}
    by_node = {r[0]: r for r in rows}
    assert by_node["121475272011"][1] == "Amazon Device Subscriptions"  # 先到先得
    assert by_node["121475272011"][4] is None      # 'L1_xxx' 占位 → NULL
    assert by_node["121475272011"][5] is True      # 是否叶子='是'
    assert by_node["553220"][4] == "551238" and by_node["553220"][5] is False
    assert by_node["999"][8] == "unverified_new_nodes"   # source 分段留痕


def test_derive_nodes_zips_chain_with_breadcrumb():
    """ID 链 × 面包屑右对齐还原中间层:node/名/父/深度/路径/是否叶。"""
    from workflows.taxonomy_derive import derive_nodes, resolve
    acc, stat = derive_nodes([("1,2,3", "A > B > C", 7)])
    assert stat == {"链-段=+0": 7}
    assert resolve("2", acc["2"]) == ("2", "B", "A > B", 2, "1", False, "A", 7)
    assert resolve("3", acc["3"])[5] is True          # 末段=叶
    assert resolve("1", acc["1"])[4] is None          # 根级无父


def test_derive_nodes_right_anchors_on_length_gap():
    """长度不等按叶子右对齐:链多出的头段不采信,但可当最顶层的父 ID。"""
    from workflows.taxonomy_derive import derive_nodes, resolve
    # 链多一段(带了不出现在面包屑里的根 node)
    acc, stat = derive_nodes([("0,1,2", "A > B", 3)])
    assert stat == {"链-段=+1": 3}
    assert "0" not in acc                              # 没名字的段不入库
    assert resolve("1", acc["1"])[4] == "0"            # 但仍当父 ID 用
    # 面包屑多一段(促销层/多余的头) → 只取后 k 段,不错位
    acc2, stat2 = derive_nodes([("1,2", "X > A > B", 3)])
    assert stat2 == {"链-段=-1": 3}
    assert resolve("1", acc2["1"])[1] == "A"
    assert resolve("2", acc2["2"])[2] == "X > A > B"   # 路径仍用完整面包屑


def test_derive_nodes_majority_vote_on_drifting_names():
    """同 node 名称漂移 → 按产品数取多数票(catpath 的漂移在这里收敛)。"""
    from workflows.taxonomy_derive import derive_nodes, resolve
    acc, _s = derive_nodes([
        ("1,2", "H & K > Home Décor Products", 30),
        ("1,2", "H & K > Home Décor", 100),
    ])
    assert resolve("2", acc["2"])[1] == "Home Décor"
    assert resolve("2", acc["2"])[7] == 130            # 产品数累加


def test_taxonomy_survey_flags_unparsed_sections():
    """未解析的顶层段必须报警(否则'文件缺'与'解析器没读'分不清)。"""
    from workflows.taxonomy_import import survey_file
    out = "\n".join(survey_file({
        "meta": {"reconciled_tree_rows": 5},
        "leaves": [{}, {}],
        "internal_nodes": [{}],
    }))
    assert "leaves: 2 行(解析)" in out
    assert "internal_nodes: 1 行 ⚠" in out
    assert "meta.reconciled_tree_rows = 5" in out and "差 3" in out


def test_sibling_swap_distinguishes_rename_from_category_change():
    """改名 vs 换类目在字符串上分不开,在数据上可分(所有者第三轮实测:
    Soccer→Lacrosse 差一段、父节点还相同,被判 strong 却是两种运动)。"""
    from services.catpath import prefix_keys
    from workflows.catmap_align import is_sibling_swap

    soccer = ("Sports & Outdoors > Sports > Team Sports > Soccer > "
              "Training Equipment > Cones")
    lacrosse = ("Sports & Outdoors > Sports > Team Sports > Lacrosse > "
                "Training Equipment > Cones")
    # 产品语料里 Lacrosse 自成子树 ⇒ 与 Soccer 是并存的兄弟,不是改名
    prods = {k for p in [soccer, lacrosse] for k in prefix_keys(p)}
    assert is_sibling_swap(soccer, lacrosse, prods, set()) is True
    # 映射表里 Soccer 也有自己的行 ⇒ 同样判兄弟
    assert is_sibling_swap(soccer, lacrosse, set(),
                           set(prefix_keys(soccer))) is True

    # 纯改名:产品侧只有新名、映射表只有旧名 → 两边都查不到对方 → 非兄弟
    new, old = OWNER_CASES[0]
    assert is_sibling_swap(new, old, set(prefix_keys(new)),
                           set(prefix_keys(old))) is False
    # 增删层(不等长)不算替换,无兄弟可言
    assert is_sibling_swap("A > B > C > Leaf", "A > C > Leaf",
                           set(), set()) is False


def test_substitution_index():
    seg = catpath.segments
    assert catpath.substitution_index(seg("A > B > C"), seg("A > X > C")) == 1
    assert catpath.substitution_index(seg("A > B > C"), seg("A > X > Y")) is None
    assert catpath.substitution_index(seg("A > B"), seg("A > B > C")) is None
    assert catpath.prefix_keys("A > B") == [("a",), ("a", "b")]


def test_decide_alias_evidence_beats_structure():
    """实证 PT 优先于结构形态(所有者首跑实测:0.83 分的 Soccer→Lacrosse
    靠实证拦下;0.67 分的 Party Packs/Vases 靠实证救回)。"""
    from workflows.catmap_align import decide_alias
    # 实证一致 → 任何结构层都收(别名的目的是拿到正确 PT,数据已证明正确)
    assert decide_alias("medium", "GoodPT", "GoodPT") == "verified"
    assert decide_alias("weak", "GoodPT", "GoodPT") == "verified"
    # 实证相左 → 结构再像也拒
    assert decide_alias("strong", "GoodPT", "OtherPT") == "pt_conflict"
    # 无实证 → 只信 strong,且必须非同级兄弟
    assert decide_alias("strong", None, "GoodPT") == "aligned"
    assert decide_alias("strong", None, "GoodPT",
                        sibling=True) == "sibling_swap"
    assert decide_alias("medium", None, "GoodPT") == "needs_review"
    assert decide_alias("weak", None, "GoodPT") == "needs_review"
    # 实证仍压过兄弟判别(数据直接证明结果 PT 对)
    assert decide_alias("strong", "GoodPT", "GoodPT",
                        sibling=True) == "verified"
    # canonical 侧 PT 不唯一(映射表分流)→ 无从背书,退回看结构
    assert decide_alias("medium", "GoodPT", None) == "needs_review"


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
