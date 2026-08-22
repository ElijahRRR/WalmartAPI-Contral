"""分片轮转发牌回归。每条都对应一个会把货分歪的实例。

发牌是全项目最不能"跑一次看看"的东西 —— 占用一旦落库就撤不回。
所以这里把每条口径都钉死:切口切什么、分片怎么切、轮转怎么排、上限压回去的
组去哪、配额与容量哪个是硬闸、同样输入是不是同样输出。

★ **2026-08-22 引擎入参从「组」改成「产品」**(所有者纠正:"如果按你所说的
顺序,你会把大量排名靠后的产品拉到前面来")。切口、分片都挪到产品层,组队
挪进片内。下面的 `_g` 因此返回**一个品牌的那批产品**,不再是一个组字典。
"""

import pytest

from services import alloc_engine as ae


def _p(asin, score, brand, category="Home", channel="FBA", lead=3):
    return {"asin": asin, "brand": brand, "manufacturer": None,
            "score": float(score), "category": category, "channel": channel,
            "lead": lead}


def _g(key, score, size=1, category="Home", channel="FBA", lead=3):
    """一个品牌组的**产品**。

    组内同分 ⇒ 加权组分(0.7×均分 + 0.3×最高)恰好等于 score,所以下面每条
    测试里"组分"仍然就是传进来的那个数,读起来跟改之前一样。
    ⚠ lead 必须有值:2026-08-21 起「未填配送时长限制」回落 7 天而不是"不限",
    于是**没有任何店**会收货期未知的组(见 store_targets.lead_ok)。
    """
    return [_p(f"{key}#{i:03d}", score, key, category, channel, lead)
            for i in range(size)]


def _s(quota, room=10_000, categories=(), channel="FBA", **kw):
    return dict(quota=quota, room=room, categories=list(categories),
                channel=channel, **kw)


def _deal(groups, stores, slices=ae.SLICES, held=None, bound=None):
    """把「若干品牌的产品」摊平喂给引擎。"""
    return ae.deal([p for g in groups for p in g], stores,
                   held_brand=held, bound=bound, slices=slices)


# ── 切口切的是产品(2026-08-22 重排的核心)────────────────────────────

def test_the_cut_is_made_on_products_not_on_groups():
    """★ 这是重排要解决的那个问题本身。

    一个品牌:1 件 95 分 + 40 件 41 分。另有 10 件 70 分的散货。
    切口只取 10 件 —— 那 40 件低分品**一件都不该进来**。
    改之前:组分取组内最高分 ⇒ 整组按 95 分排第一 ⇒ 41 件货全被拉进牌堆。
    """
    fat = _p("F#hero", 95, "fat") and [_p("F#hero", 95, "fat")] + [
        _p(f"F#{i:03d}", 41, "fat") for i in range(40)]
    mid = [_p(f"M#{i:03d}", 70, f"m{i}") for i in range(10)]
    r = ae.deal(fat + mid, {"S": _s(10)}, slices=2)
    placed = [it["asin"] for a in r["assign"] for it in a["group"]["items"]]
    assert not [x for x in placed if x.startswith("F#") and x != "F#hero"], \
        "低分同门牌不该跟着高分品进池"


def test_group_score_is_weighted_so_one_hero_cannot_carry_the_rest():
    """组分 = 0.7×均分 + 0.3×最高(所有者拍 Q1)。一俊不再遮百丑。"""
    from services import alloc_groups as ag
    hero = [{"score": 95}] + [{"score": 41}] * 40
    assert round(ag.group_score(hero), 1) == 58.1
    assert round(ag.group_score([{"score": 80}, {"score": 78}]), 1) == 79.3


# ── 分片 ──────────────────────────────────────────────────────────────

def test_slices_are_cut_by_rank_not_by_score_interval():
    """按排名切:每片货量恒等,片厚随分布自适应。

    这是所有者 2026-08-16 追问"10 层怎么切"时定死的。按分数区间切会废掉:
    实测分数堆在 40~70,"每 10 分一档"能让中间一格装三分之一的货、
    顶上一格凑不出 10% —— "最好的那片"根本不成片。
    """
    prods = ([_p(f"m{i:03d}", 50 + i * 0.03, "b") for i in range(90)]
             + [_p(f"w{i}", i * 10.0, "b") for i in range(10)])
    sl = ae.slice_cut(prods, 10)
    assert len(sl) == 10
    assert {len(x) for x in sl} == {10}              # 每片恒 10 件
    span = lambda L: L[0]["score"] - L[-1]["score"]  # noqa: E731
    assert span(sl[0]) > span(sl[5])


def test_slice_cut_is_descending_and_deterministic():
    """同分之间也要有稳定顺序,否则两次跑的分配结果会不一样。"""
    prods = [_p("b", 50.0, "x"), _p("a", 50.0, "x"), _p("c", 90.0, "x")]
    once = [[x["asin"] for x in L] for L in ae.slice_cut(prods, 2)]
    twice = [[x["asin"] for x in L] for L in ae.slice_cut(list(reversed(prods)), 2)]
    assert once == twice == [["c", "a"], ["b"]]


def test_slice_cut_rejects_a_bad_slice_count():
    with pytest.raises(ValueError):
        ae.slice_cut([_p("a", 1.0, "x")], 0)


# ── 轮转 ──────────────────────────────────────────────────────────────

def test_rotation_spreads_top_goods_instead_of_filling_one_store():
    """所有者的原话:"不能按顺序,这样子会导致大量评分高的品堆积到一个店铺"。

    20 件喂 2 家店(配额相同)。按分数顺序发,前 10 件会全落在同一家;
    按 已接量/配额 轮转,第一片必须一人一半。
    """
    groups = [_g(f"g{i:02d}", 100.0 - i) for i in range(20)]
    r = _deal(groups, {"A": _s(10), "B": _s(10)}, slices=2)
    top = [a["store"] for a in r["assign"] if a["layer"] == 1]
    assert top.count("A") == top.count("B") == 5
    assert top[0] != top[1]                          # 交替,不是先喂满一家


def test_ratio_not_absolute_count_drives_the_queue():
    """★ 排队看的是**比值**不是绝对数(所有者 2026-08-22 拍 Q2 维持)。

    配额 90 的店该拿到 9 倍于配额 10 的店。写成"谁接得少谁先拿"(绝对数)
    会让大店和小店拿一样多 —— 配额就白算了。
    """
    groups = [_g(f"g{i:03d}", 100.0 - i * 0.1) for i in range(100)]
    r = _deal(groups, {"BIG": _s(90), "SMALL": _s(10)}, slices=10)
    assert {s: v["items"] for s, v in r["by_store"].items()} == {"BIG": 90,
                                                                "SMALL": 10}


def test_fit_breaks_ties_and_store_name_breaks_the_rest():
    """平手时按适配分降序;再平手按店名 —— 排序键必须排到唯一。

    留一处不唯一(比如靠 dict 迭代序),同样的输入就会给出不同的分配结果,
    dry-run 看到的不再是 --execute 会做的。
    """
    r = _deal([_g("only", 50.0)],
              {"A": _s(5, fit=0.1), "B": _s(5, fit=0.9)}, slices=1)
    assert r["assign"][0]["store"] == "B"
    r2 = _deal([_g("only", 50.0)], {"B": _s(5), "A": _s(5)}, slices=1)
    assert r2["assign"][0]["store"] == "A"


def test_same_input_same_output():
    """确定性:占用撤不回,dry-run 与 --execute 必须看到同一个世界。"""
    groups = [_g(f"g{i}", (i * 37) % 100 / 1.0, size=1 + i % 3) for i in range(60)]
    stores = {"S1": _s(20), "S2": _s(15), "S3": _s(25)}
    a = _deal(groups, stores)
    b = _deal(list(reversed(groups)), dict(reversed(list(stores.items()))))
    assert [(x["group"]["key"], x["store"], x["layer"]) for x in a["assign"]] \
        == [(x["group"]["key"], x["store"], x["layer"]) for x in b["assign"]]


# ── 片内上限 ──────────────────────────────────────────────────────────

def test_slice_cap_stops_thin_category_coverage_from_eating_the_top_slice():
    """光靠轮转不够:某个大类只有一家店收得了,那些组无视轮转顺序全落它头上。

    2026-08-16 用真实店铺数据模拟发现的(顶层验收比值 1.41 / 1.37 双双越界)。
    这里压成最小复现:好货全是「宠物」而只有 P 店收宠物 —— 片切得越细,
    P 在第一片能吃的越少(上限 = 片产品数 ÷ 店数)。
    """
    groups = ([_g(f"p{i:02d}", 100.0 - i, category="Animals") for i in range(50)]
              + [_g(f"h{i:03d}", 40.0 - i * 0.1, category="Home") for i in range(100)])
    stores = {"P": _s(50, categories=["Animals", "Home"]),
              "H1": _s(50, categories=["Home"]), "H2": _s(50, categories=["Home"])}
    loose = _deal(groups, stores, slices=1)     # 一片装完 ⇒ 上限 = 150/3 = 50
    tight = _deal(groups, stores, slices=10)    # 每片 15 件 ⇒ 上限 5
    l1 = lambda r: r["by_store"]["P"]["by_layer"][1]           # noqa: E731
    assert l1(tight) < l1(loose)
    # ★ 而且**一件都没少发** —— 上限只改时机不改总量
    assert len(loose["assign"]) == len(tight["assign"]) == 150


def test_the_slice_cap_is_slice_size_over_store_count():
    """上限 = **片产品数 ÷ 店数**(所有者拍 Q3),固定值。

    不看定向拿走多少、也不随店挑完而缩水 —— 可复现优先。
    """
    groups = [_g(f"g{i:02d}", 100.0 - i) for i in range(30)]
    r = _deal(groups, {"A": _s(30), "B": _s(30), "C": _s(30)}, slices=3)
    # 每片 10 件 / 3 家店 ⇒ 上限 ceil(10/3) = 4
    for s in ("A", "B", "C"):
        assert r["by_store"][s]["by_layer"][1] <= 4


def test_capped_groups_are_retried_at_the_next_slice_not_after_all_of_them():
    """压回来的组必须在**下一片开头**重试,不能攒到最后。

    攒到最后的话,它想去的那家店会先被后面几片的货填满配额 —— 片内上限
    于是从"晚一点拿"变成"永远拿不到",净发牌量下降。这是本模块最容易
    写反的一处:两种写法都"看起来在均衡",但一种是倒扣。
    """
    groups = ([_g(f"p{i}", 100.0 - i, category="Animals") for i in range(10)]
              + [_g(f"h{i}", 80.0 - i, category="Home") for i in range(20)])
    stores = {"P": _s(10, categories=["Animals", "Home"]),
              "H1": _s(10, categories=["Home"]), "H2": _s(10, categories=["Home"])}
    r = _deal(groups, stores, slices=2)
    assert len(r["assign"]) == 30 and not r["unplaced"]
    # 10 个宠物组只有 P 收得了,一个都不许丢
    assert sum(1 for a in r["assign"]
               if a["group"]["category"] == "Animals" and a["store"] == "P") == 10


def test_capped_groups_are_swept_up_not_dropped():
    """片内上限压到最后一片还没发出去的,收尾扫描兜住(只剩配额与容量闸)。

    丢掉的话,片内上限就成了"给得越均匀发得越少"—— 那没人敢开它。
    """
    groups = [_g(f"p{i}", 100.0 - i, category="Animals") for i in range(10)]
    stores = {"P": _s(10, categories=["Animals"]),
              "H": _s(10, categories=["Home"])}          # H 一件也收不了
    r = _deal(groups, stores, slices=2)
    assert len(r["assign"]) == 10 and not r["unplaced"]
    # 收尾扫描的层号 = 片数+1,方案表上一眼能认出"这几组是补发的"
    assert max(a["layer"] for a in r["assign"]) == 3


# ── 硬闸 ──────────────────────────────────────────────────────────────

def test_room_is_a_hard_gate_but_quota_is_checked_before_the_pick():
    """组是原子的(品牌排他 ⇒ 整组去一家店),所以两个闸的口径必须不同。

    · room 是物理容量:发完不许越,一件都不行;
    · quota 是节奏:挑人时该店还没到配额就行,发完可以顶过头 ——
      否则一个 40 件的组遇上只剩 12 个配额的店就永远发不出去。

    ⚠ 第二家店 B 是**为了把本轮取货量撑够**才加的:取货量 N = 各店剩余差额
    之和,只有 A(配额 12)的话这一轮只取 12 件,那个 40 件的品牌根本凑不齐
    (见下一条 `test_a_group_bigger_than_the_take_is_truncated`)。
    """
    pool = [_g("big", 90.0, size=40)] + [_g(f"f{i:02d}", 50.0) for i in range(80)]
    r = _deal(pool, {"A": _s(quota=12, room=100), "B": _s(quota=100, room=1000)},
              slices=1)
    assert r["by_store"]["A"]["items"] >= 40 > 12          # 配额可以被顶过头
    r2 = _deal([_g("big", 90.0, size=40)], {"A": _s(quota=99, room=30)}, slices=1)
    assert not r2["assign"] and r2["unplaced"][0]["reason"] == ae.NO_ROOM


def test_a_group_bigger_than_the_take_is_truncated_and_the_rest_waits():
    """★ 重排带来的**新语义**,不是 bug:取货量 N = 各店剩余差额之和。

    一个 40 件的品牌遇上总配额 12 ⇒ 这一轮只取它排名最高的 12 件,组就是
    12 件的组。剩下 28 件排队等下一轮 —— 而且因为品牌已经占给了这家店,
    下一轮它们只能回同一家(定向流),排他不会破。

    这正是所有者要的效果:**不让低分品跟着高分同门牌一起挤进来**。
    """
    r = _deal([_g("big", 90.0, size=40)], {"A": _s(quota=12, room=100)}, slices=1)
    assert r["by_store"]["A"]["items"] == 12
    assert len(r["queued"]) == 28


def test_category_and_channel_gates():
    """类目:店填了就只准入填的;没填 = 不限制(与 store_targets.allowed 同口径)。
    渠道:店没填配送限制就不接自由流分配。
    """
    stores = {"CAT": _s(9, categories=["Sporting Goods"]), "ANY": _s(9),
              "NOCH": _s(9, channel=None)}
    r = _deal([_g("k", 50.0, category="Home")], stores, slices=1)
    assert r["assign"][0]["store"] == "ANY"              # 受限店拒收,没填渠道的不参与
    r2 = _deal([_g("k", 50.0, category=None)],
               {"CAT": _s(9, categories=["Sporting Goods"])}, slices=1)
    assert not r2["assign"]                              # 归不到大类 → 受限店拒收


def test_unplaced_reasons_do_not_collapse_into_the_wrong_bucket():
    """三种原因的处置完全不同,归错类会把所有者送去改不该改的东西。

    ⚠ 这条盯的是一个真实的写法陷阱:按字典序 "no_gate" < "no_quota" <
    "no_room",顺序正好是反的 —— 拿 min()/max() 比字符串**不会报错**,
    只会把"等下一批"的货全记成"配置有问题"。
    """
    # ⚠ X 存在只是为了把取货量撑到 11 件(N = 各店剩余差额之和);它收不了
    #   Home,所以真正能接货的只有 S,而 S 的配额一件就满
    home = [_g(f"h{i:02d}", 90.0 - i) for i in range(11)]
    r = _deal(home, {"S": _s(quota=1, room=100),
                     "X": _s(quota=10, categories=["Animals"])}, slices=1)
    assert r["unplaced"] and {u["reason"] for u in r["unplaced"]} == {ae.NO_QUOTA}
    r2 = _deal([_g("x", 90.0, category="Animals")],
               {"S": _s(quota=9, categories=["Home"])}, slices=1)
    assert r2["unplaced"][0]["reason"] == ae.NO_CATEGORY


def test_stores_with_zero_quota_or_room_are_skipped_not_divided_by():
    """配额 0 的店(所有者把「单店最大在线数」填 0)不参与,也不能除以 0。"""
    r = _deal([_g("a", 50.0)], {"OFF": _s(0), "FULL": _s(9, room=0), "OK": _s(9)},
              slices=1)
    assert r["assign"][0]["store"] == "OK"
    assert r["params"]["skipped_stores"] == 2


# ── 梯队 ──────────────────────────────────────────────────────────────

def test_tier2_only_gets_what_tier1_could_not_take():
    """空店缺口天然巨大,同池竞争会把最好的品全吸走(§7.5)。"""
    groups = [_g(f"g{i}", 100.0 - i) for i in range(6)]
    r = _deal(groups, {"MAIN": _s(4, tier=1), "EMPTY": _s(9, tier=2)}, slices=1)
    where = {a["group"]["key"]: a["store"] for a in r["assign"]}
    assert [where[f"g{i}"] for i in range(6)] == ["MAIN"] * 4 + ["EMPTY"] * 2


# ── 轮次续取(2026-08-22 新增)─────────────────────────────────────────

def test_a_second_round_is_taken_when_the_first_cannot_fill_the_quota():
    """所有者原话:"如果10000个品全部选完了,有的店还没有拿满它需要的产品
    数量,则在候选池里继续拿…排名第10001-20000的品"。

    第一轮取 N=配额 件,但其中一批过不了闸 ⇒ 还差 ⇒ 再取一个 N。
    没有这一步的话,店永远填不满,而报告会说"排队中",看不出是闸挡的。
    """
    # 前 10 件是 P 收不了的类目,后 10 件才是它能收的
    groups = ([_g(f"x{i}", 100.0 - i, category="Animals") for i in range(10)]
              + [_g(f"h{i}", 50.0 - i, category="Home") for i in range(10)])
    r = _deal(groups, {"P": _s(10, categories=["Home"])}, slices=2)
    assert r["by_store"]["P"]["items"] == 10
    assert r["params"]["rounds"] >= 2, "第一轮全被类目闸挡下,必须再取一轮"


def test_rounds_stop_when_the_pool_runs_out():
    """池子见底就收手 —— 不许空转到 MAX_ROUNDS。"""
    r = _deal([_g("a", 50.0)], {"S": _s(100)}, slices=1)
    assert r["params"]["rounds"] == 1 and not r["queued"]


# ── 品牌排他跨片(2026-08-22 新增)─────────────────────────────────────

def test_a_brand_split_across_slices_still_lands_in_one_store():
    """★ 切口在产品层 ⇒ 同一个品牌可能被切到两片里。

    第一片把它发给了 A,第二片再遇到这个品牌就**必须还归 A** —— 否则同一个
    品牌在一轮之内被拆给两家店,品牌排他当场失效,而且两条占用会互相打架。
    """
    # 一个品牌 20 件,分数横跨整个池子;另有 20 件散货穿插其间
    wide = [_p(f"W#{i:02d}", 100.0 - i * 5, "wide") for i in range(20)]
    other = [_p(f"O#{i:02d}", 99.0 - i * 5, f"o{i}") for i in range(20)]
    r = ae.deal(wide + other, {"A": _s(40), "B": _s(40)}, slices=10)
    landed = {a["store"] for a in r["assign"] if a["group"]["brand"] == "wide"}
    assert len(landed) == 1, f"品牌 wide 被拆到了 {landed}"


def test_an_existing_claim_routes_every_slice_to_the_holder():
    """已有占用同理:不管这个品牌的产品散落在哪几片,全归占用店。"""
    wide = [_p(f"W#{i:02d}", 100.0 - i * 5, "wide") for i in range(20)]
    r = ae.deal(wide, {"A": _s(40), "B": _s(40)}, held_brand={"wide": "B"},
                slices=10)
    assert {a["store"] for a in r["assign"]} == {"B"}
    assert all(a["group"].get("store") == "B" for a in r["assign"])


# ── 验收指标 ──────────────────────────────────────────────────────────

def test_acceptance_metrics_flag_a_hoarder():
    """验收指标必须真的会亮红 —— 恒绿的指标等于没有指标。"""
    groups = [_g(f"g{i:03d}", 100.0 - i) for i in range(100)]
    r = _deal(groups, {"A": _s(50), "B": _s(50)}, slices=2)
    m = ae.acceptance(r)
    assert all(0.7 <= v["top_ratio"] <= 1.3 for v in m.values())
    assert not any(v["over_cap"] for v in m.values())
    # 人为造一个独吞:B 只收宠物,货全是家居
    r2 = _deal(groups, {"A": _s(50), "B": _s(50, categories=["Animals"])},
               slices=2)
    assert ae.acceptance(r2)["A"]["over_cap"] is True


def test_inputs_are_not_mutated():
    """入参不许被改:调用方还要拿它们出方案表、写占用。"""
    prods = _g("a", 50.0) + _g("b", 40.0)
    stores = {"S": _s(9)}
    snap_p = [dict(p) for p in prods]
    snap_s = {k: dict(v) for k, v in stores.items()}
    ae.deal(prods, stores)
    assert prods == snap_p and stores == snap_s


def test_slice_count_is_independent_of_store_count():
    """所有者 2026-08-16 追问:片数(10)和店数(14)没关系。

    片是把货按质量切档,店是每一档里轮流拿牌的人。
    """
    groups = [_g(f"g{i}", 100.0 - i) for i in range(100)]
    for n in (2, 14, 40):
        r = _deal(groups, {f"S{i:02d}": _s(100) for i in range(n)}, slices=10)
        assert max(a["layer"] for a in r["assign"]) <= ae.SLICES + 1
        assert len(r["assign"]) == 100


def test_acceptance_compares_slots_to_slots_not_groups_to_slots():
    """⚠ 验收指标的分子分母必须同单位(货位)。

    生产实测 2026-08-16:同一批里出现 0.16 与 2.05,不是分配不公平 —— 是分子
    数的「组数」、分母是「货位配额」。拿到大组的店比值天然偏低、小组的偏高,
    指标量的成了"组的大小"而不是"分得公不公平"。
    """
    groups = ([_g(f"big{i}", 100.0 - i, size=10, category="Animals") for i in range(2)]
              + [_g(f"sml{i}", 99.5 - i, size=1, category="Home") for i in range(20)])
    r = _deal(groups, {"A": _s(20, categories=["Animals"]),
                       "B": _s(20, categories=["Home"])}, slices=1)
    m = ae.acceptance(r)
    assert r["by_store"]["A"]["groups"] == 2 and r["by_store"]["B"]["groups"] == 20
    assert r["by_store"]["A"]["items"] == r["by_store"]["B"]["items"] == 20
    assert m["A"]["top_ratio"] == m["B"]["top_ratio"] == 1.0


# ── 逐店配送时长限制(所有者建列 2026-08-16)──────────────────────────

def test_lead_limit_blocks_groups_slower_than_the_store_allows():
    """「配送时长限制」是逐店硬闸:只分 delivery_days ≤ 该值的货。"""
    stores = {"STRICT": _s(9, lead_limit=5), "LOOSE": _s(9, lead_limit=10)}
    r = _deal([_g("fast", 90.0, lead=3), _g("slow", 95.0, lead=9)], stores,
              slices=1)
    where = {a["group"]["key"]: a["store"] for a in r["assign"]}
    assert where["slow"] == "LOOSE"          # 9 天只有放宽到 10 的店收
    assert "fast" in where                   # 3 天两家都行


def test_unknown_lead_is_refused_by_every_store_now():
    """⚠ 采不到货期时**一律拒收**,不是当它够快。

    所有者填这一列就是明确不要慢货;拿"没采到"当"够快"是替他做了他没做的
    决定。与类目那条「归不到大类的,受限店拒收」同一纪律。

    ★ 2026-08-21 起连**未填**的店也拒:那一列的"未填"从"不限"改成回落 7 天。
    """
    from services import amz_source
    for stores in ({"STRICT": _s(9, lead_limit=5)}, {"UNSET": _s(9)}):
        r = _deal([_g("unknown", 90.0, lead=None)], stores, slices=1)
        assert not r["assign"] and r["unplaced"][0]["reason"] == ae.NO_LEAD
    ok = _deal([_g("just", 90.0, lead=amz_source.MAX_LEAD_DAYS)],
               {"UNSET": _s(9)}, slices=1)
    assert ok["assign"][0]["store"] == "UNSET"
    no = _deal([_g("over", 90.0, lead=amz_source.MAX_LEAD_DAYS + 1)],
               {"UNSET": _s(9)}, slices=1)
    assert no["unplaced"][0]["reason"] == ae.NO_LEAD


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
    groups = ([_g(f"bound{i:02d}", 100.0 - i) for i in range(50)]
              + [_g(f"free{i:02d}", 49.5 - i * 0.1) for i in range(50)])
    held = {f"bound{i:02d}": "A" for i in range(50)}
    r = _deal(groups, {"A": _s(50), "B": _s(50)}, slices=1, held=held)
    assert r["by_store"]["A"]["bound_items"] == 50
    rot = ae.acceptance(r)
    # A 一件自由流都没拿到 ⇒ **不出比值**,而不是报个 0.00 去误标"越界"
    assert rot["A"]["top_ratio"] is None and rot["A"]["own_items"] == 0
    assert rot["B"]["top_ratio"] == 1.0


def test_a_store_with_a_tiny_free_slice_gets_no_ratio():
    """⚠ 量太小的时候比值是**数学假象**,不是信号。

    2026-08-16 实测:A085 自由流 36 件、A162 24 件,都过了 20 件那道线,
    但量小到恰好全落在 L1 ⇒ "自己的顶层占比"= 1.0 ⇒ 比值双双等于 1÷base
    = **7.69**(两家一模一样,一眼就知道假)。所以再加一道"占自己配额 ≥ 10%"。

    直接构造 `by_store` 而不是跑一遍发牌:这条测的是**门槛**本身。
    """
    from collections import Counter
    r = {"by_store": {
        "BIG": {"quota": 500, "items": 500, "bound_items": 0,
                "by_layer": Counter({i: 50 for i in range(1, 11)}),
                "by_layer_free": Counter({i: 50 for i in range(1, 11)})},
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

    2026-08-21 复盘生产方案表,「没有店的类目闸放行」标签下的 6,490 件 FBA,
    **每一件的大类都有店在收**。真正拦路的是货期。所有者照着那份摘要去开
    大类,一件也救不回来。
    """
    r = _deal([_g("slow", 90.0, category="Home", lead=12)],
              {"S": _s(9, categories=["Home"], lead_limit=7)}, slices=1)
    assert r["unplaced"][0]["reason"] == ae.NO_LEAD
    assert "开类目没用" in ae.REASON_LABEL[ae.NO_LEAD]


def test_channel_block_is_not_reported_as_a_category_block():
    """类目与货期都过、只差渠道 → 记「渠道」。生产里这是最大的一块。"""
    r = _deal([_g("fbm", 90.0, category="Home", channel="FBM")],
              {"S": _s(9, categories=["Home"], channel="FBA")}, slices=1)
    assert r["unplaced"][0]["reason"] == ae.NO_CHANNEL


def test_attribution_takes_the_farthest_gate_across_all_stores():
    """跨店取**走得最远**的那道闸:一家店类目就不符,另一家类目过了只是货期超。

    归因该归「货期」—— 因为存在一家店,给它放宽配送时长限制就能收下这批货。
    归成「类目」的话,所有者会去给第一家店开大类,而那家店开了也还是收不了。
    """
    stores = {"WRONG_CAT": _s(9, categories=["Animals"], lead_limit=7),
              "RIGHT_CAT": _s(9, categories=["Home"], lead_limit=7)}
    r = _deal([_g("slow", 90.0, category="Home", lead=12)], stores, slices=1)
    assert r["unplaced"][0]["reason"] == ae.NO_LEAD


def test_no_live_store_is_its_own_reason_not_a_config_complaint():
    """一家店都没有(配额/容量全 0)→ 记 NO_STORE,**不进 queued**。

    记成「没有店的类目闸放行」会把所有者送去改类目配置;记成「排队中」更糟
    —— 那意思是"什么都不用做,等下一批",而真正要做的是下架腾位或补齐店铺
    配置。三者处置完全不同。
    """
    r = _deal([_g("a", 90.0)], {"FULL": _s(9, room=0)}, slices=1)
    assert r["unplaced"][0]["reason"] == ae.NO_STORE
    assert not r["queued"]


def test_every_reason_code_has_a_label():
    """新增原因码忘了配标签 → `REASON_LABEL[k]` 直接 KeyError 炸在报告里。"""
    codes = {ae.NO_BRAND, ae.NO_CATEGORY, ae.NO_LEAD, ae.NO_CHANNEL,
             ae.NO_ROOM, ae.NO_QUOTA, ae.NO_STORE}
    assert set(ae.REASON_LABEL) == codes
    assert len({ae.REASON_LABEL[c] for c in codes}) == len(codes)   # 标签不重样


def test_a_same_round_regroup_is_not_reported_as_an_existing_claim():
    """⚠ 同一个品牌被切口切在两片里,第二片看到它"已经有主"了 —— 但那是
    **本轮自己刚发的**,不是所有者手上的既有占用。

    不分开的实测后果:一条占用都没有的时候,报告照样说"定向流 45 件
    (补齐已占品牌)"。所有者会以为台账里有 45 条,去查却什么都没有。
    """
    wide = [_p(f"W#{i:02d}", 100.0 - i * 5, "wide") for i in range(20)]
    r = ae.deal(wide, {"A": _s(40)}, slices=10)
    kinds = {g.get("bound_by") for g in r["groups"] if g.get("store")}
    assert kinds == {"round"}, "没有既有占用时不许出现 claim"

    r2 = ae.deal(wide, {"A": _s(40)}, held_brand={"wide": "A"}, slices=10)
    assert {g.get("bound_by") for g in r2["groups"] if g.get("store")} == {"claim"}
