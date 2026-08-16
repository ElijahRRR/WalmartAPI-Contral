"""分层轮转发牌回归(§7.4b)。每条都对应一个会把货分歪的实例。

发牌是全项目最不能"跑一次看看"的东西 —— 占用一旦落库就撤不回。
所以这里把每条口径都钉死:分层怎么切、轮转怎么排、上限压回去的组去哪、
配额与容量哪个是硬闸、同样输入是不是同样输出。
"""

import math

import pytest

from services import alloc_engine as ae


def _g(key, score, size=1, category="家居", channel="FBA", **kw):
    return dict(key=key, score=score, size=size, category=category,
                channel=channel, **kw)


def _s(quota, room=10_000, categories=(), channel="FBA", **kw):
    return dict(quota=quota, room=room, categories=list(categories),
                channel=channel, **kw)


# ── 分层 ──────────────────────────────────────────────────────────────

def test_layers_are_cut_by_rank_not_by_score_interval():
    """按排名切:每层货量恒等,层厚随分布自适应。

    这是所有者 2026-08-16 追问"10 层怎么切"时定死的。按分数区间切会废掉:
    实测分数堆在 40~70,"每 10 分一档"能让中间一格装三分之一的货、
    顶上一格凑不出 10% —— "最好的那层"根本不成层。
    """
    # 90 个组挤在 50~53,10 个组散在 0~100:按区间切会让一层装 90 个
    groups = ([_g(f"m{i}", 50 + i * 0.03) for i in range(90)]
              + [_g(f"w{i}", i * 10.0) for i in range(10)])
    layers = ae.layer_cut(groups, 0.10)
    assert len(layers) == 10
    assert {len(x) for x in layers} == {10}          # 每层恒 10 组
    # 顶层的分数跨度远大于中间层 —— 层厚是被分布撑出来的
    span = lambda L: L[0]["score"] - L[-1]["score"]  # noqa: E731
    assert span(layers[0]) > span(layers[5])


def test_layer_cut_is_descending_and_deterministic():
    """同分组之间也要有稳定顺序,否则两次跑的分配结果会不一样。"""
    groups = [_g("b", 50.0), _g("a", 50.0), _g("c", 90.0)]
    once = [[x["key"] for x in L] for L in ae.layer_cut(groups, 0.5)]
    twice = [[x["key"] for x in L] for L in ae.layer_cut(list(reversed(groups)), 0.5)]
    assert once == twice == [["c", "a"], ["b"]]


def test_layer_cut_rejects_bad_thickness():
    with pytest.raises(ValueError):
        ae.layer_cut([_g("a", 1.0)], 0)


# ── 轮转 ──────────────────────────────────────────────────────────────

def test_rotation_spreads_top_goods_instead_of_filling_one_store():
    """所有者的原话:"不能按顺序,这样子会导致大量评分高的品堆积到一个店铺"。

    20 组喂 2 家店(配额相同)。按分数顺序发,前 10 组会全落在同一家;
    按 已接量/配额 轮转,顶层必须一人一半。
    """
    groups = [_g(f"g{i:02d}", 100.0 - i) for i in range(20)]
    r = ae.deal(groups, {"A": _s(10), "B": _s(10)}, thickness=0.5)
    top = [a["store"] for a in r["assign"] if a["layer"] == 1]
    assert top.count("A") == top.count("B") == 5
    assert top[0] != top[1]                          # 交替,不是先喂满一家


def test_ratio_not_absolute_count_drives_the_queue():
    """排队看的是**比值**不是绝对数:配额 90 的店该拿到 9 倍于配额 10 的店。

    写成"谁接得少谁先拿"(绝对数)会让大店和小店拿一样多 —— 配额就白算了。
    """
    groups = [_g(f"g{i:03d}", 100.0 - i * 0.1) for i in range(100)]
    r = ae.deal(groups, {"BIG": _s(90), "SMALL": _s(10)}, thickness=0.1)
    got = {s: v["items"] for s, v in r["by_store"].items()}
    assert got == {"BIG": 90, "SMALL": 10}


def test_fit_breaks_ties_and_store_name_breaks_the_rest():
    """平手时按适配分降序;再平手按店名 —— 排序键必须排到唯一。

    留一处不唯一(比如靠 dict 迭代序),同样的输入就会给出不同的分配结果,
    dry-run 看到的不再是 --execute 会做的。
    """
    r = ae.deal([_g("only", 50.0)],
                {"A": _s(5, fit=0.1), "B": _s(5, fit=0.9)}, thickness=1.0)
    assert r["assign"][0]["store"] == "B"
    r2 = ae.deal([_g("only", 50.0)], {"B": _s(5), "A": _s(5)}, thickness=1.0)
    assert r2["assign"][0]["store"] == "A"


def test_same_input_same_output():
    """确定性:占用撤不回,dry-run 与 --execute 必须看到同一个世界。"""
    groups = [_g(f"g{i}", (i * 37) % 100 / 1.0, size=1 + i % 3) for i in range(60)]
    stores = {"S1": _s(20), "S2": _s(15), "S3": _s(25)}
    a = ae.deal(groups, stores)
    b = ae.deal(list(reversed(groups)), dict(reversed(list(stores.items()))))
    assert [(x["group"]["key"], x["store"], x["layer"]) for x in a["assign"]] \
        == [(x["group"]["key"], x["store"], x["layer"]) for x in b["assign"]]


# ── 层内上限 ──────────────────────────────────────────────────────────

def test_layer_cap_stops_thin_category_coverage_from_eating_the_top_layer():
    """光靠轮转不够:某个大类只有一家店收得了,那些组无视轮转顺序全落它头上。

    2026-08-16 用真实店铺数据模拟发现的(顶层验收比值 1.41 / 1.37 双双越界)。
    这里把它压成最小复现:好货全是「宠物」而只有 P 店收宠物,
    没有层内上限的话 P 在 L1 就把配额吃光了。
    """
    groups = ([_g(f"p{i}", 100.0 - i, category="宠物") for i in range(10)]
              + [_g(f"h{i}", 80.0 - i, category="家居") for i in range(20)])
    stores = {"P": _s(10, categories=["宠物", "家居"]),
              "H1": _s(10, categories=["家居"]), "H2": _s(10, categories=["家居"])}
    loose = ae.deal(groups, stores, thickness=0.5, slack=99)   # 松弛拉大 = 等于没上限
    tight = ae.deal(groups, stores, thickness=0.5, slack=1.3)
    l1 = lambda r: r["by_store"]["P"]["by_layer"][1]           # noqa: E731
    assert l1(loose) == 10 and l1(tight) < 10
    a_loose, a_tight = ae.acceptance(loose), ae.acceptance(tight)
    assert a_tight["P"]["top_ratio"] < a_loose["P"]["top_ratio"]
    # ★ 而且**一件都没少发** —— 上限只改时机不改总量,详见下一条
    assert len(loose["assign"]) == len(tight["assign"]) == 30


def test_capped_groups_are_retried_at_the_next_layer_not_after_all_of_them():
    """压回来的组必须在**下一层开头**重试,不能攒到最后。

    攒到最后的话,它想去的那家店会先被后面几层的货填满配额 —— 层内上限
    于是从"晚一点拿"变成"永远拿不到",净发牌量下降。这是本模块最容易
    写反的一处:两种写法都"看起来在均衡",但一种是倒扣。
    """
    groups = ([_g(f"p{i}", 100.0 - i, category="宠物") for i in range(10)]
              + [_g(f"h{i}", 80.0 - i, category="家居") for i in range(20)])
    stores = {"P": _s(10, categories=["宠物", "家居"]),
              "H1": _s(10, categories=["家居"]), "H2": _s(10, categories=["家居"])}
    r = ae.deal(groups, stores, thickness=0.5, slack=1.3)
    assert len(r["assign"]) == 30 and not r["unplaced"]
    # 10 个宠物组只有 P 收得了,一个都不许丢
    assert sum(1 for a in r["assign"]
               if a["group"]["category"] == "宠物" and a["store"] == "P") == 10


def test_capped_groups_are_swept_up_not_dropped():
    """层内上限压到最后一层还没发出去的,收尾扫描兜住(只剩配额与容量闸)。

    丢掉的话,层内上限就成了"给得越均匀发得越少"—— 那没人敢开它。
    """
    groups = [_g(f"g{i}", 100.0 - i) for i in range(10)]
    r = ae.deal(groups, {"ONLY": _s(10)}, thickness=0.5, slack=0.5)
    assert len(r["assign"]) == 10 and not r["unplaced"]
    # 收尾扫描的层号 = 层数+1,方案表上一眼能认出"这几组是补发的"
    assert max(a["layer"] for a in r["assign"]) == 3


# ── 硬闸 ──────────────────────────────────────────────────────────────

def test_room_is_a_hard_gate_but_quota_is_checked_before_the_pick():
    """组是原子的(品牌排他 ⇒ 整组去一家店),所以两个闸的口径必须不同。

    · room 是物理容量:发完不许越,一件都不行;
    · quota 是节奏:挑人时该店还没到配额就行,发完可以顶过头 ——
      否则一个 40 件的组遇上只剩 12 个配额的店就永远发不出去。
    """
    r = ae.deal([_g("big", 90.0, size=40)], {"A": _s(quota=12, room=100)},
                thickness=1.0)
    assert r["by_store"]["A"]["items"] == 40 > 12          # 配额可以被顶过头
    r2 = ae.deal([_g("big", 90.0, size=40)], {"A": _s(quota=99, room=30)},
                 thickness=1.0)
    assert not r2["assign"] and r2["unplaced"][0]["reason"] == ae.NO_ROOM


def test_category_and_channel_gates():
    """类目:店填了就只准入填的;没填 = 不限制(与 store_targets.allowed 同口径)。
    渠道:店没填配送限制就不接自由流分配。
    """
    stores = {"CAT": _s(9, categories=["户外"]), "ANY": _s(9),
              "NOCH": _s(9, channel=None)}
    r = ae.deal([_g("k", 50.0, category="家居")], stores, thickness=1.0)
    assert r["assign"][0]["store"] == "ANY"              # 受限店拒收,没填渠道的不参与
    r2 = ae.deal([_g("k", 50.0, category=None)], {"CAT": _s(9, categories=["户外"])},
                 thickness=1.0)
    assert not r2["assign"]                              # 归不到大类 → 受限店拒收


def test_unplaced_reasons_do_not_collapse_into_the_wrong_bucket():
    """三种原因的处置完全不同,归错类会把所有者送去改不该改的东西。

    ⚠ 这条盯的是一个真实的写法陷阱:按字典序 "no_gate" < "no_quota" <
    "no_room",顺序正好是反的 —— 拿 min()/max() 比字符串**不会报错**,
    只会把"等下一批"的货全记成"配置有问题"。
    """
    # 配额满(有店放行、也塞得下)
    r = ae.deal([_g("a", 90.0), _g("b", 80.0)], {"S": _s(quota=1, room=100)},
                thickness=1.0)
    assert r["unplaced"][0]["reason"] == ae.NO_QUOTA
    # 没人放行
    r2 = ae.deal([_g("x", 90.0, category="宠物")],
                 {"S": _s(quota=9, categories=["家居"])}, thickness=1.0)
    assert r2["unplaced"][0]["reason"] == ae.NO_GATE
    assert set(ae.REASON_LABEL) == {ae.NO_GATE, ae.NO_ROOM, ae.NO_QUOTA}


def test_stores_with_zero_quota_or_room_are_skipped_not_divided_by():
    """配额 0 的店(所有者把「单店最大在线数」填 0)不参与,也不能除以 0。"""
    r = ae.deal([_g("a", 50.0)], {"OFF": _s(0), "FULL": _s(9, room=0), "OK": _s(9)},
                thickness=1.0)
    assert r["assign"][0]["store"] == "OK"
    assert r["params"]["skipped_stores"] == 2


# ── 梯队 ──────────────────────────────────────────────────────────────

def test_tier2_only_gets_what_tier1_could_not_take():
    """空店缺口天然巨大,同池竞争会把最好的品全吸走(§7.5)。"""
    groups = [_g(f"g{i}", 100.0 - i) for i in range(6)]
    r = ae.deal(groups, {"MAIN": _s(4, tier=1), "EMPTY": _s(9, tier=2)},
                thickness=1.0)
    where = {a["group"]["key"]: a["store"] for a in r["assign"]}
    assert [where[f"g{i}"] for i in range(6)] == ["MAIN"] * 4 + ["EMPTY"] * 2


# ── 验收指标 ──────────────────────────────────────────────────────────

def test_acceptance_metrics_flag_a_hoarder():
    """验收指标必须真的会亮红 —— 恒绿的指标等于没有指标。"""
    groups = [_g(f"g{i}", 100.0 - i) for i in range(20)]
    r = ae.deal(groups, {"A": _s(10), "B": _s(10)}, thickness=0.5)
    m = ae.acceptance(r)
    assert all(0.7 <= v["top_ratio"] <= 1.3 for v in m.values())
    assert not any(v["over_cap"] for v in m.values())
    # 人为造一个独吞:B 只收宠物,货全是家居
    r2 = ae.deal([_g(f"g{i}", 100.0 - i) for i in range(20)],
                 {"A": _s(10), "B": _s(10, categories=["宠物"])}, thickness=0.5)
    assert ae.acceptance(r2)["A"]["over_cap"] is True


def test_inputs_are_not_mutated():
    """入参不许被改:调用方还要拿它们出方案表、写占用。"""
    groups = [_g("a", 50.0), _g("b", 40.0)]
    stores = {"S": _s(9)}
    snap_g = [dict(g) for g in groups]
    snap_s = {k: dict(v) for k, v in stores.items()}
    ae.deal(groups, stores)
    assert groups == snap_g and stores == snap_s


def test_layer_count_is_independent_of_store_count():
    """所有者 2026-08-16 追问:层数(10)和店数(14)没关系。

    层是把货按质量切档,店是每一档里轮流拿牌的人。
    """
    groups = [_g(f"g{i}", 100.0 - i, size=1) for i in range(100)]
    for n in (2, 14, 40):
        r = ae.deal(groups, {f"S{i:02d}": _s(100) for i in range(n)}, thickness=0.1)
        assert max(a["layer"] for a in r["assign"]) <= math.ceil(1 / 0.1) + 1
        assert len(r["assign"]) == 100


def test_acceptance_compares_slots_to_slots_not_groups_to_slots():
    """⚠ 验收指标的分子分母必须同单位(货位)。

    生产实测 2026-08-16:同一批里出现 0.16 与 2.05,不是分配不公平 —— 是分子
    数的「组数」、分母是「货位配额」。拿到大组的店比值天然偏低、小组的偏高,
    指标量的成了"组的大小"而不是"分得公不公平"。
    """
    # 两家店配额相同;A 只能收大组(每组 10 件),B 只能收小组(每组 1 件)
    groups = ([_g(f"big{i}", 100.0 - i, size=10, category="宠物") for i in range(2)]
              + [_g(f"sml{i}", 99.5 - i, size=1, category="家居") for i in range(20)])
    r = ae.deal(groups, {"A": _s(20, categories=["宠物"]),
                         "B": _s(20, categories=["家居"])}, thickness=1.0)
    m = ae.acceptance(r)
    # 组数:A 拿 2 组、B 拿 20 组(1:10);货位:各 20(1:1)。指标要看后者
    assert r["by_store"]["A"]["groups"] == 2 and r["by_store"]["B"]["groups"] == 20
    assert r["by_store"]["A"]["items"] == r["by_store"]["B"]["items"] == 20
    assert m["A"]["top_ratio"] == m["B"]["top_ratio"] == 1.0
