"""services/params.flag:工作流布尔开关的黑名单语义。

钉两件事:
  ① 与全仓 33 个黑名单站点**逐字等价**(缺省关/缺省开两种形态);
  ② **不是白名单**——仓里另有 `in {'1','true','yes'}` 那一种,两者对
     `-p apply=y` 给出相反答案,合成一个开关就是把两种语义混成一个。
"""

from services import params as P


def test_blacklist_semantics_only_three_words_mean_false():
    for v in ("0", "false", "no", "FALSE", "No"):
        assert P.flag({"k": v}, "k") is False
        assert P.flag({"k": v}, "k", True) is False
    for v in ("1", "true", "yes", "y", "on", "随便什么", "", " "):
        assert P.flag({"k": v}, "k") is True, v


def test_default_decides_only_the_missing_key():
    assert P.flag({}, "k") is False
    assert P.flag({}, "k", True) is True
    # 键在就以键为准,缺省不参与
    assert P.flag({"k": "0"}, "k", True) is False
    assert P.flag({"k": "1"}, "k", False) is True


def test_equivalent_to_the_inline_sites_it_replaces():
    """逐字对拍现行写法:迁移不许顺手改语义。"""
    def inline(params, key, default):
        return str(params.get(key, "1" if default else "0")).lower() \
            not in {"0", "false", "no"}

    cases = [{}, {"k": "0"}, {"k": "1"}, {"k": "No"}, {"k": ""},
             {"k": 0}, {"k": 1}, {"k": True}, {"k": None}, {"k": "yes"}]
    for c in cases:
        for d in (False, True):
            assert P.flag(c, "k", d) == inline(c, "k", d), (c, d)


def test_is_not_the_whitelist_form():
    """白名单站点(apply / include_ties 等)**不许**迁到这里:
    `-p apply=y` 白名单判假、本函数判真 —— 对同一个输入给出相反的答案。"""
    assert P.flag({"apply": "y"}, "apply") is True
    assert (str({"apply": "y"}.get("apply", "")).lower()
            in {"1", "true", "yes"}) is False
