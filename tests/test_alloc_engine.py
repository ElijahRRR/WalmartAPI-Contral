"""分层轮转发牌回归(§7.4b)。每条都对应一个会把货分歪的实例。

发牌是全项目最不能"跑一次看看"的东西 —— 占用一旦落库就撤不回。
所以这里把每条口径都钉死:分层怎么切、轮转怎么排、上限压回去的组去哪、
配额与容量哪个是硬闸、同样输入是不是同样输出。
"""

import math

import pytest

from services import alloc_engine as ae


def _g(key, score, size=1, category="Home", channel="FBA", **kw):
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
    groups = ([_g(f"p{i:02d}", 100.0 - i, category="Animals") for i in range(50)]
              + [_g(f"h{i:03d}", 40.0 - i * 0.1, category="Home") for i in range(100)])
    stores = {"P": _s(50, categories=["Animals", "Home"]),
              "H1": _s(50, categories=["Home"]), "H2": _s(50, categories=["Home"])}
    loose = ae.deal(groups, stores, thickness=0.5, slack=99)   # 松弛拉大 = 等于没上限
    tight = ae.deal(groups, stores, thickness=0.5, slack=1.3)
    l1 = lambda r: r["by_store"]["P"]["by_layer"][1]           # noqa: E731
    assert l1(loose) == 50 and l1(tight) < 50
    a_loose, a_tight = ae.acceptance(loose), ae.acceptance(tight)
    assert a_tight["P"]["top_ratio"] < a_loose["P"]["top_ratio"]
    # ★ 而且**一件都没少发** —— 上限只改时机不改总量,详见下一条
    assert len(loose["assign"]) == len(tight["assign"]) == 150


def test_capped_groups_are_retried_at_the_next_layer_not_after_all_of_them():
    """压回来的组必须在**下一层开头**重试,不能攒到最后。

    攒到最后的话,它想去的那家店会先被后面几层的货填满配额 —— 层内上限
    于是从"晚一点拿"变成"永远拿不到",净发牌量下降。这是本模块最容易
    写反的一处:两种写法都"看起来在均衡",但一种是倒扣。
    """
    groups = ([_g(f"p{i}", 100.0 - i, category="Animals") for i in range(10)]
              + [_g(f"h{i}", 80.0 - i, category="Home") for i in range(20)])
    stores = {"P": _s(10, categories=["Animals", "Home"]),
              "H1": _s(10, categories=["Home"]), "H2": _s(10, categories=["Home"])}
    r = ae.deal(groups, stores, thickness=0.5, slack=1.3)
    assert len(r["assign"]) == 30 and not r["unplaced"]
    # 10 个宠物组只有 P 收得了,一个都不许丢
    assert sum(1 for a in r["assign"]
               if a["group"]["category"] == "Animals" and a["store"] == "P") == 10


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
    stores = {"CAT": _s(9, categories=["Sporting Goods"]), "ANY": _s(9),
              "NOCH": _s(9, channel=None)}
    r = ae.deal([_g("k", 50.0, category="Home")], stores, thickness=1.0)
    assert r["assign"][0]["store"] == "ANY"              # 受限店拒收,没填渠道的不参与
    r2 = ae.deal([_g("k", 50.0, category=None)], {"CAT": _s(9, categories=["Sporting Goods"])},
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
    r2 = ae.deal([_g("x", 90.0, category="Animals")],
                 {"S": _s(quota=9, categories=["Home"])}, thickness=1.0)
    assert r2["unplaced"][0]["reason"] == ae.NO_CATEGORY
    assert set(ae.REASON_LABEL) == {ae.NO_BRAND, ae.NO_CATEGORY, ae.NO_LEAD,
                                    ae.NO_CHANNEL, ae.NO_ROOM, ae.NO_QUOTA,
                                    ae.NO_STORE}


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
    groups = [_g(f"g{i:03d}", 100.0 - i) for i in range(100)]
    r = ae.deal(groups, {"A": _s(50), "B": _s(50)}, thickness=0.5)
    m = ae.acceptance(r)
    assert all(0.7 <= v["top_ratio"] <= 1.3 for v in m.values())
    assert not any(v["over_cap"] for v in m.values())
    # 人为造一个独吞:B 只收宠物,货全是家居
    r2 = ae.deal(groups, {"A": _s(50), "B": _s(50, categories=["Animals"])},
                 thickness=0.5)
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
    groups = ([_g(f"big{i}", 100.0 - i, size=10, category="Animals") for i in range(2)]
              + [_g(f"sml{i}", 99.5 - i, size=1, category="Home") for i in range(20)])
    r = ae.deal(groups, {"A": _s(20, categories=["Animals"]),
                         "B": _s(20, categories=["Home"])}, thickness=1.0)
    m = ae.acceptance(r)
    # 组数:A 拿 2 组、B 拿 20 组(1:10);货位:各 20(1:1)。指标要看后者
    assert r["by_store"]["A"]["groups"] == 2 and r["by_store"]["B"]["groups"] == 20
    assert r["by_store"]["A"]["items"] == r["by_store"]["B"]["items"] == 20
    assert m["A"]["top_ratio"] == m["B"]["top_ratio"] == 1.0


# ── 逐店配送时长限制(所有者建列 2026-08-16)──────────────────────────

def test_lead_limit_blocks_groups_slower_than_the_store_allows():
    """「配送时长限制」是逐店硬闸:只分 delivery_days ≤ 该值的货。"""
    fast = _g("fast", 90.0, lead=3)
    slow = _g("slow", 95.0, lead=9)
    stores = {"STRICT": _s(9, lead_limit=5), "LOOSE": _s(9)}
    r = ae.deal([fast, slow], stores, thickness=1.0)
    where = {a["group"]["key"]: a["store"] for a in r["assign"]}
    assert where["slow"] == "LOOSE"          # 9 天只有不限的店收
    assert "fast" in where                   # 3 天两家都行


def test_unknown_lead_is_refused_by_a_capped_store():
    """⚠ 采不到货期时**受限店拒收**,不是当它够快。

    所有者填这一列就是明确不要慢货;拿"没采到"当"够快"是替他做了他没做的
    决定。与类目那条「归不到大类的,受限店拒收」同一纪律。
    """
    r = ae.deal([_g("unknown", 90.0, lead=None)],
                {"STRICT": _s(9, lead_limit=5)}, thickness=1.0)
    assert not r["assign"] and r["unplaced"][0]["reason"] == ae.NO_LEAD
    # 未填限制的店照收 —— 未填 = 不限
    r2 = ae.deal([_g("unknown", 90.0, lead=None)], {"ANY": _s(9)}, thickness=1.0)
    assert r2["assign"][0]["store"] == "ANY"


def test_gate_predicates_come_from_store_targets_not_reimplemented():
    """⚠ 类目与货期的判定只留一处。

    「三列全空 = 不限制」「未填时长 = 不限」这类规则正着写反着写都像对的,
    各写一遍迟早分叉 —— 本仓已在报告 vs 回填的行口径上栽过一次。
    """
    import inspect
    src = inspect.getsource(ae._blocker)
    assert "store_targets.allowed" in src and "store_targets.lead_ok" in src
    assert "not in cats" not in src and "<= float(cap)" not in src


def test_acceptance_ignores_store_bound_groups_by_default():
    """⚠ 绑定单店的牌(定向流)**跳过排队**,验收指标不该算它。

    2026-08-16 实测:4 家"顶层越界"的店,分到的货几乎全是定向流 —— 它们不是
    被偏心了,是手上的老品牌把自己的位置占满了。拿一个参数调不动的量去判
    "参数没调对",只会让人去改根本不相干的旋钮。
    """
    # A 的配额全被绑定它的牌吃掉;B 靠自由流拿同样多
    groups = ([_g(f"bound{i:02d}", 100.0 - i, store="A") for i in range(50)]
              + [_g(f"free{i:02d}", 49.5 - i * 0.1) for i in range(50)])
    r = ae.deal(groups, {"A": _s(50), "B": _s(50)}, thickness=1.0)
    assert r["by_store"]["A"]["bound_items"] == 50
    rot = ae.acceptance(r)
    # A 一件自由流都没拿到 ⇒ **不出比值**,而不是报个 0.00 去误标"越界"
    assert rot["A"]["top_ratio"] is None and rot["A"]["own_items"] == 0
    # B 全靠自由流,质量构成就是全体本身 ⇒ 1.0
    assert rot["B"]["top_ratio"] == 1.0


def test_a_store_with_a_tiny_free_slice_gets_no_ratio():
    """⚠ 量太小的时候比值是**数学假象**,不是信号。

    2026-08-16 实测:A085 自由流 36 件、A162 24 件,都过了 20 件那道线,
    但量小到恰好全落在 L1 ⇒ "自己的顶层占比"= 1.0 ⇒ 比值双双等于 1÷base
    = **7.69**(两家一模一样,一眼就知道假)。比值要有意义,这家店得在多个层
    都拿到过货 —— 所以再加一道"占自己配额 ≥ 10%"。

    直接构造 `by_store` 而不是跑一遍发牌:这条测的是**门槛**本身,
    让发牌去凑一个刚好卡在门槛上的分布,测试会变得又脆又难读。
    """
    from collections import Counter
    r = {"by_store": {
        # 大店:自由流 500 件铺在 10 层里 —— 比值有意义
        "BIG": {"quota": 500, "items": 500, "bound_items": 0,
                "by_layer": Counter({i: 50 for i in range(1, 11)}),
                "by_layer_free": Counter({i: 50 for i in range(1, 11)})},
        # 小片:自由流 36 件、全在 L1(A085 的原样),绝对量过线但占配额仅 2.5%
        "TINY": {"quota": 1460, "items": 1477, "bound_items": 1441,
                 "by_layer": Counter({1: 1477}),
                 "by_layer_free": Counter({1: 36})}}}
    m = ae.acceptance(r)
    assert m["TINY"]["own_items"] == 36 >= ae.MIN_FREE_FOR_RATIO   # 绝对量过线
    assert 36 < 1460 * ae.MIN_FREE_SHARE_OF_QUOTA                 # 但占比不过
    assert m["TINY"]["top_ratio"] is None, "量太小的比值是假象,不该给数"
    assert m["BIG"]["top_ratio"] is not None


# ── 四道闸各自归因(2026-08-21 生产实测逼出来的)────────────────────────

def test_lead_block_is_not_reported_as_a_category_block():
    """⚠ 类目放行、货期拦下 → 记「货期」,**不许记「类目」**。

    这是一条实测教训,不是假想:2026-08-21 复盘生产方案表,「没有店的
    类目/渠道闸放行(要它出货得先给某店开这个大类)」标签下的 6,490 件 FBA,
    **每一件的大类都有店在收**。真正拦路的是货期 —— 已分配的货配送天数
    最大 7 天、一件超的都没有,而卡住的那批 17.6% 超 7 天。
    所有者照着那份摘要去开大类,一件也救不回来。
    """
    r = ae.deal([_g("slow", 90.0, category="Home", lead=12)],
                {"S": _s(9, categories=["Home"], lead_limit=7)}, thickness=1.0)
    assert r["unplaced"][0]["reason"] == ae.NO_LEAD
    assert "开类目没用" in ae.REASON_LABEL[ae.NO_LEAD]


def test_channel_block_is_not_reported_as_a_category_block():
    """类目与货期都过、只差渠道 → 记「渠道」。生产里这是最大的一块。"""
    r = ae.deal([_g("fbm", 90.0, category="Home", channel="FBM")],
                {"S": _s(9, categories=["Home"], channel="FBA")}, thickness=1.0)
    assert r["unplaced"][0]["reason"] == ae.NO_CHANNEL


def test_attribution_takes_the_farthest_gate_across_all_stores():
    """跨店取**走得最远**的那道闸:一家店类目就不符,另一家店类目过了只是货期超。

    归因该归「货期」—— 因为存在一家店,给它放宽配送时长限制就能收下这批货。
    归成「类目」的话,所有者会去给第一家店开大类,而那家店开了也还是收不了
    (它同样有货期限制,只是先被类目挡下,压根没走到那一步)。
    """
    stores = {"WRONG_CAT": _s(9, categories=["Animals"], lead_limit=7),
              "RIGHT_CAT": _s(9, categories=["Home"], lead_limit=7)}
    r = ae.deal([_g("slow", 90.0, category="Home", lead=12)], stores,
                thickness=1.0)
    assert r["unplaced"][0]["reason"] == ae.NO_LEAD


def test_no_live_store_is_its_own_reason_not_a_config_complaint():
    """一家店都没有(配额/容量全 0)→ 记 NO_STORE。

    记成「没有店的类目闸放行」会把所有者送去改类目配置,而真正要做的是
    下架腾位或补齐店铺配置 —— 类目一栏改到天亮也没用。
    """
    r = ae.deal([_g("a", 90.0)], {"FULL": _s(9, room=0)}, thickness=1.0)
    assert r["unplaced"][0]["reason"] == ae.NO_STORE


def test_every_reason_code_has_a_label():
    """新增原因码忘了配标签 → `REASON_LABEL[k]` 直接 KeyError 炸在报告里。"""
    codes = {ae.NO_BRAND, ae.NO_CATEGORY, ae.NO_LEAD, ae.NO_CHANNEL,
             ae.NO_ROOM, ae.NO_QUOTA, ae.NO_STORE}
    assert set(ae.REASON_LABEL) == codes
    assert len({ae.REASON_LABEL[c] for c in codes}) == len(codes)   # 标签不重样
