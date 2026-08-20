"""类目黑名单:按 browse_node_id 拦整棵子树(所有者定稿 2026-08-20)。

这套用例钉死的是**改造的全部理由**——旧的路径字符串等值有两个致命面:
父级不覆盖子级、名字一漂就全失效。
"""

from types import SimpleNamespace

import pytest

from services import audit_phase0, category_blacklist as cb
from services.audit_models import ProductInfo


def _rules(nodes=(), tops=(), paths=()):
    return cb.CatRules(
        nodes={str(n): {"match_value": str(n), "category_path": f"子树 {n}",
                        "category_zh": "测试子树", "reason": "整棵不做",
                        "walmart_policy": "Intellectual Property"} for n in nodes},
        tops={cb._norm_top(t): {"match_value": t, "category_path": t,
                                "category_zh": "", "reason": "顶级不做",
                                "walmart_policy": "Food Products"} for t in tops},
        paths={p: {"match_value": p, "category_path": p, "category_zh": "",
                   "reason": "", "walmart_policy": ""} for p in paths})


# ── 子树匹配:父级自动覆盖所有子级(旧行为做不到的那件事)────────────────

def test_subtree_covers_every_descendant():
    """名单里只写「拼图」这一级,3-D 拼图 / 千片拼图 全部跟着被拦。

    旧的路径等值下,这三条里只有第一条会被拦 —— 1.18 万条名单里 56% 的条目
    就是为了补这个洞逐层枚举出来的,而亚马逊每新增一个叶子类目就多一个漏洞。
    """
    rules = _rules(nodes=["166057011"])          # Toys & Games > Puzzles
    for chain in ("165793011,166057011",                      # 拼图本身
                  "165793011,166057011,166058011",            # 3-D 拼图
                  "165793011,166057011,166059011,9999999"):   # 千片拼图
        hit = cb.check(rules, "Toys & Games > Puzzles > …", browse_node_chain=chain)
        assert hit and hit.matched_by == cb.MATCH_NODE
        assert hit.matched_value == "166057011"


def test_subtree_takes_the_deepest_root():
    """ID 链自叶向根查:同时命中粗细两棵禁售树时,报**最细**的那个。
    detail 里要说清"因为属于哪一棵被禁的树",不是"因为在某个大类下"。"""
    rules = _rules(nodes=["165793011", "166057011"])
    hit = cb.check(rules, "Toys & Games > Puzzles",
                   browse_node_chain="165793011,166057011")
    assert hit.matched_value == "166057011"


def test_subtree_survives_category_rename():
    """名字漂移下仍然拦得住 —— 名单写 Wall Art,官方树改叫 Wall Décor,
    按名字一条都拦不住;按 ID 无所谓叫什么(这是真实撞到的样本)。"""
    rules = _rules(nodes=["3736081"])
    hit = cb.check(rules, "Home & Kitchen > Wall Décor > Paintings",
                   browse_node_chain="1055398,3736081,13336081")
    assert hit and hit.matched_value == "3736081"


def test_no_chain_falls_back_to_leaf_node_id():
    """老行没有 ID 链、只有叶 ID 时也能判(node_backfill 未覆盖到的存量)。"""
    rules = _rules(nodes=["166057011"])
    assert cb.check(rules, "", browse_node_id="166057011")
    assert cb.check(rules, "", browse_node_id="999") is None


def test_unrelated_subtree_passes():
    rules = _rules(nodes=["166057011"])
    assert cb.check(rules, "Home & Kitchen > Storage",
                    browse_node_chain="1055398,3736081") is None


# ── 顶级名 / 路径等值 ───────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "Grocery & Gourmet Food > Beverages",
    "Grocery & Gourmet Food",
    "grocery & gourmet food > x",
    "Grocery,& Gourmet Food > x",          # 逗号写法(旧实现在这里漏拦)
])
def test_top_name_matches_regardless_of_comma_and_case(path):
    rules = _rules(tops=["Grocery & Gourmet Food"])
    hit = cb.check(rules, path)
    assert hit and hit.matched_by == cb.MATCH_TOP


def test_path_exact_is_last_and_does_not_cover_children():
    """路径等值那份**仍然**父不覆盖子——它是飞书镜像的历史行,保留只为兼容;
    真正要拦整棵的规则必须录成 node_subtree。"""
    rules = _rules(paths=["Toys&Games->Puzzles"])
    assert cb.check(rules, "Toys & Games > Puzzles",
                    norm_path="Toys&Games->Puzzles")
    assert cb.check(rules, "Toys & Games > Puzzles > 3-D Puzzles",
                    norm_path="Toys&Games->Puzzles->3-DPuzzles") is None


def test_empty_rules_block_nothing():
    """规则为空 = 类目闸整条不生效(判定件不自带兜底清单)。"""
    assert cb.check(cb.CatRules(), "Books", browse_node_chain="283155") is None
    assert cb.check(None, "Books") is None


# ── 接进 Phase0 ────────────────────────────────────────────────────────────

def _ctx(rules):
    return SimpleNamespace(phase0_sellers=frozenset(), phase0_asins=frozenset(),
                           phase0_cats=frozenset(), cat_rules=rules,
                           brand_blacklist={})


def test_phase0_reports_subtree_hit_with_evidence():
    r = audit_phase0.check(
        ProductInfo(asin="B000TEST00", amazon_category_path="Toys & Games > Puzzles > 3-D",
                    browse_node_chain="165793011,166057011,166058011"),
        _ctx(_rules(nodes=["166057011"])))
    assert r.blocked
    h = r.hits[0]
    assert h.rule_code == "phase0_lark_blacklist_amazon_cat" and h.penalty == -100
    assert h.detail["matched_by"] == "node_subtree"
    assert h.detail["browse_node_id"] == "166057011"
    assert h.detail["source"] == "category_blacklist(DB)"
    assert h.detail["walmart_policy"] == "Intellectual Property"


def test_seed_rules_are_wellformed():
    """种子行必须能被 load 的装配逻辑吃下:node 行必须带 node_id,
    top 行必须带 match_value(否则建库后那条规则静默不生效)。"""
    for r in cb.SEED_RULES:
        assert r["match_type"] in (cb.MATCH_NODE, cb.MATCH_TOP, cb.MATCH_PATH)
        assert r.get("match_value")
        if r["match_type"] == cb.MATCH_NODE:
            assert r.get("browse_node_id", "").isdigit(), r
        assert r.get("reason")


# ── 顶级名两种写法必须归到同一个键(2026-08-20 实见的静默漏拦)──────────

def test_top_name_matches_both_spellings():
    """同一个顶级在真实数据里有两种写法,规则只写一种也得两种都拦。

    实见:飞书类目名单存的是挤掉空格的 `VideoGames->XboxOne->Games`,而规则
    按官方树写的是 `Video Games` —— 旧归一只压空白,两边差 `&` 两侧的空格就
    **整条「电子游戏整顶级不做」不触发,而且不报错**。
    """
    for rule_name, product_top in [("Video Games", "VideoGames"),
                                   ("VideoGames", "Video Games"),
                                   ("Clothing, Shoes & Jewelry", "Clothing,Shoes&Jewelry"),
                                   ("Grocery & Gourmet Food", "Grocery&GourmetFood")]:
        rules = _rules(tops=[rule_name])
        hit = cb.check(rules, f"{product_top} > 随便什么子类目")
        assert hit and hit.matched_by == cb.MATCH_TOP, f"{rule_name} 拦不住 {product_top}"


def test_distinct_tops_do_not_collide_after_strip():
    """删空白删逗号不能把两个**不同**的顶级并成一个 —— 39 官方顶级已核无碰撞,
    这里钉死几个最像的,防止哪天有人再放宽归一。"""
    assert cb._norm_top("Baby") != cb._norm_top("Baby Products")
    assert cb._norm_top("Books") != cb._norm_top("Audible Books & Originals")
    assert cb._norm_top("Camera & Photo") != cb._norm_top("Cameras & Photo Gear")
