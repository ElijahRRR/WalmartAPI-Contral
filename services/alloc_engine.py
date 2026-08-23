"""alloc_engine — 分层轮转发牌(§7.4b)。**纯函数**:不碰数据库/飞书/沃尔玛。

输入两堆已经算好的东西(候选组 + 店铺配额),输出"哪一组给哪家店"。
所有取数(产品分、店铺配额、占用、类目、渠道)都在调用方 —— 这样发牌
本身可以脱库跑测试,而分配是全项目最不能"跑一次看看"的东西。

## 入参是产品,一张牌是品牌组

调用方喂进来的是**过完漏斗的产品**;分片、组队、发牌都在这里做。
2026-08-22 之前入参是"已经组好的队",于是切口只能在组这一层做 —— 见下面第 1 条。

## 一张牌 = 一个品牌组,不是一个产品

品牌排他(一个品牌只在一家店)决定了**组是原子的**:整组去同一家店,
不能拆。所以一张牌占的货位数 = 组内 ASIN 数(`size`),而配额/容量都是
按货位算的 —— 别把"发了多少张牌"当成"分了多少货"。

## 三条最容易写错的

1. **切的是产品、按排名切。** 两件事:
   · 按**排名**不按分数区间 —— 实测分数堆在 40~70,按"每 10 分一档"切会让
     中间一格装三分之一的货、顶上一格凑不出 10%,"最好的那层"根本不成层;
   · 切**产品**不切组(2026-08-22 所有者纠正)—— 切组的话,组分取最高分,
     "一个 95 分带四十个 41 分"的品牌会整组排进前排,把那四十个低分品一起
     拉进来。组队因此挪到**片内**做。
2. **配额在挑人之前判,判完就允许这一组把它顶过头。** 组是原子的,
   一个 40 件的组遇上只剩 12 个配额的店,若要求"发完仍不超配额"就永远
   发不出去。所以规则是:挑人时该店必须**还没到配额**;发完可以超,
   超出量 < 最大组的 size。真正不许越的是 `room`(物理容量),那是硬闸。
3. **片内还要有上限,光靠轮转不够。** 轮转管顺序,管不住"某张牌只有一家店
   接得了"——大类覆盖稀时那些组无视轮转顺序全落到那一两家头上,配额小的店
   于是把配额的一大截花在顶层。上限 = **片产品数 ÷ 店数**(所有者 2026-08-22
   拍 Q3),固定值:不看定向拿走多少、不随店挑完缩水,可复现优先。
   被上限压回去的组不丢,排在下一片**开头**优先重试,最后进**收尾扫描**
   (全部片发完后去掉上限再走一遍)。上限管的是"谁先拿到最好的那批",
   不是"谁最终拿多少"。

## 确定性

同样的输入必须给同样的输出(占用一旦落库就撤不回,dry-run 看到的必须
就是 --execute 会做的)。所以:排序键一律排到**唯一**(比值 → 适配分 →
店名;组分 → 组 key),不用集合迭代序,不取随机数。
"""

import logging
import math
from collections import Counter

from services import alloc_groups, store_targets

logger = logging.getLogger("services.alloc_engine")

# 片数 10(所有者 2026-08-15 晚拍层厚 10%;2026-08-22 重排后单位从"组"改成
# "产品",片数不变)。⇒ 每片 = 本轮取货量的十分之一。
SLICES = 10

# 一轮发不完就再取一个 N,最多几轮。**不是模型参数,是死循环护栏**:
# 正常情况 1~3 轮就收敛(每轮至少推进 cursor,或者 need 归零直接 break)。
MAX_ROUNDS = 20

# ⚠ `LAYER_SLACK`(层内上限松弛 1.3)2026-08-22 **删掉了**:片内上限改成
# 「片产品数 ÷ 店数」(所有者拍 Q3),是个跟配额无关的固定值,没有可松弛的
# 对象。旧值取自验收指标上界 [0.7, 1.3],现在两者不再有关系。

# `_gate` 的四道闸,**按判定顺序**排列 —— 归因取"走得最远"的那道时按这个序比大小。
_GATES = ("brand", "category", "lead", "channel")

# unplaced 的原因。分开计数不是为了好看,是因为**每一种的处置完全不同**:
#   no_category = 没有店做这个大类      → 给某店开这个大类
#   no_lead     = 有店做这个大类,货期超  → 放宽该店「配送时长限制」或换货源
#   no_channel  = 有店做这个大类,渠道不对 → 给某店配 FBM / FBA
#   no_brand    = 只有占用店能要,而它自己都过不了(定向流兜底,正常走不到)
#   no_room     = 闸全过但容量塞不下      → 等下架腾位
#   no_quota    = 闸与容量都过,只是配额满 → 等下一批,不是毛病
#   no_store    = 本批压根没有一家店有容量与配额
# ⚠ **前三种曾经是同一个 `no_gate`**,标签写死"要它出货得先给某店开这个大类"。
# 2026-08-21 实测:卡住的 6,490 件 FBA 里,**每一件的大类都有店在收**,真正
# 拦路的是货期(已分配的货配送天数最大 7 天,一件超的都没有;卡住的 17.6% 超 7 天)。
# 也就是说这个标签把所有者往"去开大类"上支了整整一批,而开大类一件也救不回来。
# 定向流那边 2026-08-16 已经按真实原因拆过(见 alloc_plan._fit_to_store 的
# docstring),自由流这边漏了 —— 这次补上。
NO_BRAND, NO_CATEGORY, NO_LEAD, NO_CHANNEL = (
    "no_brand", "no_category", "no_lead", "no_channel")
NO_ROOM, NO_QUOTA, NO_STORE = "no_room", "no_quota", "no_store"
_GATE_REASON = {"brand": NO_BRAND, "category": NO_CATEGORY,
                "lead": NO_LEAD, "channel": NO_CHANNEL}
REASON_LABEL = {
    NO_CATEGORY: "没有店的类目闸放行(给某店开这个大类才能出货)",
    NO_LEAD: "有店做这个大类但货期超出所有店的配送时长限制(放宽限制或换货源;开类目没用)",
    NO_CHANNEL: "有店做这个大类但没有店做这个渠道(给某店配上这个渠道)",
    NO_BRAND: "只有占用店能要,而占用店自己过不了闸(先释放品牌)",
    NO_ROOM: "有店放行但容量塞不下(等下架腾位)",
    NO_QUOTA: "候选店本批配额都满了(留池等下一批)",
    NO_STORE: "本批没有一家店有容量与配额(先下架腾位或补齐店铺配置)",
}
# ⚠ 归类要取**走得最远**的那一条。别拿字符串比大小 —— 按字典序
# "no_gate" < "no_quota" < "no_room",顺序正好是错的,而且不会报错:
# 一批本该记成"等下一批"的货会全被记成"配置有问题",所有者跑去改类目配置


def _blocker(group: dict, store: str, st: dict) -> str | None:
    """输入:一组 + 一店 → 输出:**第一道**拦下它的闸名(`_GATES` 之一);全过返回 None。

    容量与配额不在这里 —— 那两个是随发牌变化的量,而这里只判"静态相容"。
    合在一起写的话,"这家店永远接不了这个大类"与"这家店这批满了"会得出
    同一个结论,而这两件事的处置完全不同(前者要改配置,后者等下一批)。

    ⚠ **返回闸名而不是布尔**,是为了让 `_why` 说得出"到底哪道闸拦的"。
    压成布尔的版本上线过一批,结果所有者拿着"去给某店开这个大类"的摘要
    改了一轮飞书配置,而实际拦路的是货期 —— 一件都没救回来(见常量段注释)。
    """
    # 品牌已被占用的组只能去占用店(§7.3 定向流)。**它不是另一条流水线,
    # 就是同一副牌里"只有一家店能要"的那种牌** —— 写成两个阶段的话,两边
    # 各有一套配额与容量记账,谁也不知道对方吃了多少(2026-08-16 实测:
    # 分阶段版本让定向流一口吃光批量,而且容量闸各判各的、双双超容)
    if group.get("store") is not None and group["store"] != store:
        return "brand"
    # ⚠ 类目与配送时长的判定**一律走 store_targets 的谓词**,不在这里另写。
    # 「三列全空 = 不限制」「未填时长 = 不限」这类规则正着写反着写都像对的,
    # 各写一遍迟早分叉(本仓已在报告 vs 回填的行口径上栽过一次)。
    # 代价是 alloc_engine 不再是零依赖 —— 但 store_targets 的这两个谓词是
    # 纯函数,发牌照样脱库可测
    if not store_targets.allowed({"categories": st.get("categories") or []},
                                 group.get("category")):
        return "category"
    if not store_targets.lead_ok({"lead_limit": st.get("lead_limit")},
                                 group.get("lead")):
        return "lead"
    ch = st.get("channel")
    # 店没填配送限制 → 不接自由流(store_targets 的口径,报告点名补填);
    # 组的渠道未知 → 调用方本就不该把它送进来,这里兜一道
    if not ch or group.get("channel") != ch:
        return "channel"
    return None


def _gate(group: dict, store: str, st: dict) -> bool:
    """输入:一组 + 一店 → 输出:过不过归属/类目/货期/渠道四闸(要哪道拦的用 `_blocker`)。"""
    return _blocker(group, store, st) is None


def _order(stores: dict, got: dict) -> list:
    """输入:店表 + 已接量 → 输出:本次挑人的排队顺序(离配额最远的在前)。

    排序键排到唯一:比值 → 适配分降序 → 店名。留局部随机(比如 dict 迭代序)
    会让同样的输入给出不同的分配结果 —— dry-run 看到的就不再是 --execute
    会做的了。
    """
    return sorted(stores, key=lambda s: (got[s] / stores[s]["quota"],
                                         -float(stores[s].get("fit") or 0.0), s))


def _try_place(group: dict, stores: dict, got: dict, take: dict,
               cap: dict | None) -> tuple[str | None, bool]:
    """输入:一组 + 世界现状 → 输出:(收货店 或 None, 是否**只是**被层内上限挡下)。

    顺延不回头:队首过不了就下一家,不因为"第三家更合适"回头重排。
    层内上限踩到了也只是跳过这一家,**不是放弃这一组** —— 上限要挡的是
    "这家店在这一层吃太多",不是"这一层不发这张牌"。

    第二个返回值决定这一组还有没有下文:`got` 在一轮发牌里只增不减,所以
    **因闸/容量/配额发不出去的组,这一梯队里再也不可能发出去**(条件只会更紧),
    留着重试纯属空转;只有被上限挡下的组换个层还有戏。分不清这两者的话,
    要么白跑 O(层数×池子),要么把还有戏的组当死信丢了。
    """
    size = int(group["size"])
    capped = False
    for s in _order(stores, got):
        st = stores[s]
        if not _gate(group, s, st):
            continue
        if got[s] + size > st["room"]:          # 硬闸:物理容量,一件都不许越
            continue
        if got[s] >= st["quota"]:               # 配额:挑人之前判(见模块 docstring 2)
            continue
        if cap is not None and take[s] >= cap[s]:
            capped = True                       # 层内上限:这一层轮不到它了
            continue
        got[s] += size
        take[s] += size
        return s, False
    return None, capped


def _why(group: dict, stores: dict, got: dict) -> str:
    """输入:一组 + 最终世界 → 输出:它为什么没发出去(**只读,不改任何量**)。

    取**走得最远**的那一条,两个层次都是:

    1. 闸 → 容量 → 配额:有店放行只是配额满,比"没人放行"轻得多,两者的
       处置完全不同(前者等下一批,后者要改飞书配置);
    2. **四道闸之间同样比远近**:某店的类目放行了只是货期超,就该记"货期",
       不能记"类目" —— 这一步 2026-08-21 之前是没有的,四道闸压成一个
       `no_gate` 并写死"去开个大类"(见常量段注释里的实测)。
       跨店取 max:只要**有一家**店把这组放到了更靠后的闸,归因就归那一道。

    与 `_try_place` 分开写是有意的:合在一起的话,"重新解释一遍原因"这个
    动作会顺手把货发出去 —— 那时结果里就多了一条没人知道来路的分配。
    """
    if not stores:
        return NO_STORE
    far = -1                     # 走到第几道闸(len(_GATES) = 四闸全过)
    room_ok = False
    for s, st in stores.items():
        blocked = _blocker(group, s, st)
        if blocked is not None:
            far = max(far, _GATES.index(blocked))
            continue
        far = len(_GATES)
        if got[s] + int(group["size"]) > st["room"]:
            continue
        room_ok = True
    if far < len(_GATES):
        return _GATE_REASON[_GATES[far]]
    return NO_QUOTA if room_ok else NO_ROOM


def slice_cut(products: list, slices: int = SLICES) -> list[list]:
    """输入:**产品**(任意序)+ 片数 → 输出:按产品分降序切好的片。

    ★ **切的是产品,不是组**(所有者 2026-08-22 纠正)。此前切的是组,而组分
    取组内最高分 —— 一个品牌里有一个 95 分的爆款、四十个 41 分的平庸品,
    整组按 95 分排进切口,**四十个 41 分的跟着进来**。所有者原话:
    "如果按你所说的顺序,你会把大量排名靠后的产品拉到前面来"。

    组队挪到**片内**做(见 `deal`):进片的本来就只有 top-N 产品,组内再算
    加权分不会把好品拖下去。
    """
    if not products:
        return []
    if slices < 1:
        raise ValueError(f"片数必须 ≥ 1,给的是 {slices}")
    ordered = sorted(products, key=lambda c: (-float(c["score"]), str(c["asin"])))
    per = max(1, math.ceil(len(ordered) / slices))
    return [ordered[i:i + per] for i in range(0, len(ordered), per)]


def _fit_to_store(grp: dict, st: dict) -> tuple[dict | None, int, str | None]:
    """输入:定向流的组 + 占用店 → 输出:(该店收得了的那部分, 被剪掉的件数, 原因)。

    **只用于定向流**:去向店已被品牌占用固定死,不存在"该给谁"的竞争,所以按件
    筛是良定义的、且严格更划算。自由流不许这么做 —— 那边组的完整性参与竞争
    (组分、size 都会变),按件筛等于让同一个品牌在不同店之间被拆开,破坏排他。

    渠道整组同进退(建组时已按多数派统一过);**类目与配送时长逐件判** ——
    一个品牌横跨两个大类、或者快慢货混在一起时,占用店收得了的那部分
    **本来就能上架**,不该被组里的多数派连累。

    全被剪光时返回 `(None, 件数, 原因)`,**原因要分清是类目还是货期**:
    两者的处置完全不同(开个大类 vs 放宽货期或换货),混成一个标签会把
    所有者送去改根本没用的那一项 —— 2026-08-16 实测,货期闸上线后被它挡下的
    组曾一律被记成"缺某大类"。
    """
    bad: Counter = Counter()
    ok = []
    for it in grp["items"]:
        if not store_targets.allowed({"categories": st.get("categories") or []},
                                     it["category"]):
            bad["类目"] += 1
        elif not store_targets.lead_ok({"lead_limit": st.get("lead_limit")},
                                       it.get("lead")):
            bad["货期"] += 1
        else:
            ok.append(it)
    if not ok:
        # 并列时按名字定序 —— 报告里的归因不许随行序漂
        top = sorted(bad.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        return None, grp["size"], top
    if len(ok) == len(grp["items"]):
        return grp, 0, None
    leads = [x.get("lead") for x in ok]
    return {**grp, "items": ok, "size": len(ok),
            "score": alloc_groups.group_score(ok),
            "category": alloc_groups._major(x["category"] for x in ok),
            "lead": None if any(v is None for v in leads) else max(leads)}, \
        grp["size"] - len(ok), None


def deal(products: list, stores: dict, held_brand: dict | None = None,
         bound: dict | None = None, slices: int = SLICES,
         max_rounds: int = MAX_ROUNDS) -> dict:
    """输入:**打过分的产品** + 店铺配额表(+ 已有占用)→ 输出:发牌结果。

    ★ **2026-08-22 按所有者原述重排。**入参从"组"改成"产品",切口、分片、
    组队的先后全变了。旧顺序(全池组队 → 按组分切口 → 按组分层)会把排名靠后
    的产品跟着高分同门牌一起拉进牌堆;新顺序先在**产品**层取够数,再分片,
    片内才组队。

    ```
    按产品分降序
      └ 轮次:取 N = 各店剩余差额之和 件
          └ 分 10 片(按**产品数**)
              ├ 片内组队(alloc_groups.build,组分 = 0.7均分 + 0.3最高)
              ├ 定向组(已被占用的品牌)**直接归占用店**
              └ 自由组按轮转发:离配额最远的先挑(相对比值,所有者拍 Q2)
                 每店每片上限 = **片产品数 ÷ 店数**(所有者拍 Q3);
                 **定向与自由共用这一个上限**(所有者 2026-08-22 更正)
          └ 收尾扫描:去掉片内上限再走一遍
      └ 还有店没满、池里还有货 ⇒ 取下一个 N,再来一轮
    ```

    products: [{asin, brand, manufacturer, score, category, channel, lead, …}]
              就是 `product_pool.score_all` 的产物再过完漏斗的那批。
    stores:   {店名: {quota, room, categories, channel, lead_limit, fit?, tier?}}

    ★ **本轮新落的归属要喂回组队**:同一个品牌可能被切口切在两片里(一部分
    产品排名靠前、一部分靠后)。第一片把它发给了 A,第二片再遇到这个品牌就
    **必须还归 A** —— 否则同一个品牌在一轮之内被拆给两家店,品牌排他当场失效。
    所以 `owner` 是边发边更新的,不是一开始那份快照。

    ★ **片内上限是固定的**(片产品数 ÷ 店数),不随店挑完而缩水 —— 可复现优先。
    **定向组也吃这个上限**(所有者 2026-08-22 更正):不吃的话,一家手上老品牌
    多的店可以每片先把定向吃满、再照常参与自由流轮转,上限就管不住它了 ——
    而上限存在的理由正是"别让一家店把最好的那批拿光"。
    被上限压回去的组不丢,排到下一片开头,最后进收尾扫描。上限管的是
    "谁先拿到最好的那批",不是"谁最终拿多少"。

    返回 {assign, unplaced, queued, by_store, groups, dropped, dir_out,
          dir_trim, rounds, params}。
    """
    live = {s: v for s, v in stores.items()
            if int(v.get("quota") or 0) > 0 and int(v.get("room") or 0) > 0}
    tiers = sorted({int(v.get("tier") or 1) for v in live.values()})

    # ⚠ `held0/bound0` 是**入场时**的快照,`owner/bound` 会边发边长。
    #   两者必须分开:同一个品牌被切口切在两片里时,第二片看到的它已经"有主"
    #   了 —— 但那是本轮自己刚发的,不是所有者手上的既有占用。混成一个,
    #   报告会在你一条占用都没有的时候报出一大堆"定向流(已占品牌)"。
    held0 = dict(held_brand or {})
    bound0 = dict(bound or {})
    owner = dict(held0)
    bound = dict(bound0)
    got: dict = {s: 0 for s in live}
    assign: list = []
    unplaced: list = []
    all_groups: list = []
    dropped: Counter = Counter()
    dir_out: list = []
    dir_trim = 0
    rounds_hist: list = []
    capped_tiers: list = []          # 撞到 max_rounds 护栏而停的梯队

    ordered = sorted(products, key=lambda c: (-float(c["score"]), str(c["asin"])))
    cursor = 0

    if not live and ordered:
        # ⚠ 一家店都没有 ≠ "排队等下一批"。前者要人去下架腾位或补店铺配置,
        # 后者什么都不用做 —— 混成一个,报告会让所有者去改根本不相干的东西。
        # 所以这批走 unplaced(带 NO_STORE),不进 queued
        one = alloc_groups.build(ordered, owner, bound)
        return {"assign": [], "unplaced": [{"group": g, "reason": NO_STORE}
                                           for g in one["free"] + one["directed"]],
                "queued": [], "by_store": {}, "groups": [],
                "dropped": one["dropped"], "dir_out": [], "dir_trim": 0,
                "rounds": [],
                "params": {"slices": slices, "rounds": 0, "directed_groups": 0,
                           "stores": 0, "skipped_stores": len(stores)}}

    for tier in tiers:
        # 梯队 2(空店)只分梯队 1 消化不完的剩余池:空店缺口天然巨大,
        # 同池竞争会把最好的品全吸走(§7.5,所有者 A.4 ②)
        here = {s: v for s, v in live.items() if int(v.get("tier") or 1) == tier}
        if not here:
            continue
        for rnd in range(1, max_rounds + 1):
            need = sum(max(0, int(here[s]["quota"]) - got[s]) for s in here)
            if need <= 0 or cursor >= len(ordered):
                break                       # 正常收敛:配额填满 / 池子见底
            take = ordered[cursor:cursor + need]
            cursor += len(take)
            placed_before = len(assign)
            carry: list = []
            for si, chunk in enumerate(slice_cut(take, slices), start=1):
                # 片内上限:**片产品数 ÷ 店数**(所有者拍 Q3)。固定值 ——
                # 不看定向拿走多少、不随店挑完缩水,可复现优先
                cap = {s: max(1, math.ceil(len(chunk) / len(here))) for s in here}
                g = alloc_groups.build(chunk, owner, bound)
                dropped.update(g["dropped"])
                for grp in g["directed"]:
                    # 「已占」= 入场时就有主;「同轮续发」= 本轮前面的片刚发的
                    grp["bound_by"] = (
                        "claim" if (grp.get("brand") in held0
                                    or any(it["asin"] in bound0
                                           for it in grp["items"]))
                        else "round")
                all_groups.extend(g["free"])
                all_groups.extend(g["directed"])

                # 片内三段,顺序有意:
                #   ① 压回来的(上一片被上限挡下的)—— 它们已经排过一次队,
                #      放到后面等于"从晚一点拿变成永远拿不到";
                #   ② 定向组(只有占用店能要,直接归它);
                #   ③ 自由组轮转。
                # ★ **三段共用同一个 `take_ct` 与同一个 `cap`**(所有者
                #   2026-08-22 更正:"定向组直接归占用店,需要占用片内上限")。
                #   定向不占上限的话,一家手上老品牌多的店可以在每一片里先把
                #   定向吃满、再照常参与自由流轮转 —— 上限就管不住它了,
                #   而上限存在的理由正是"别让一家店把最好的那批拿光"。
                take_ct: Counter = Counter({s: 0 for s in here})
                pending, carry = carry, []
                ready = list(pending)
                for orig in sorted(g["directed"],
                                   key=lambda x: (-float(x["score"]), str(x["key"]))):
                    grp, trimmed, why = _prep_directed(orig, here, dir_out,
                                                       stores)
                    dir_trim += trimmed
                    if grp is not None:
                        ready.append(grp)
                ready += sorted(g["free"],
                                key=lambda x: (-float(x["score"]), str(x["key"])))

                for grp in ready:
                    store, capped = _try_place(grp, here, got, take_ct, cap)
                    if store:
                        assign.append({"group": grp, "store": store,
                                       "layer": si, "tier": tier})
                        if grp.get("brand"):
                            # ★ 喂回:同品牌下一片必须还归这家(见 docstring)
                            owner[grp["brand"]] = store
                        for it in grp["items"]:
                            bound.setdefault(it["asin"], store)
                    elif capped:
                        carry.append(grp)
                    else:
                        unplaced.append({"group": grp,
                                         "reason": _why(grp, here, got)})

            # 收尾扫描:去掉片内上限再走一遍(顺序仍是组分降序)
            swept = 0
            for grp in sorted(carry, key=lambda x: (-float(x["score"]), str(x["key"]))):
                store, _ = _try_place(grp, here, got, Counter(), None)
                if store:
                    assign.append({"group": grp, "store": store,
                                   "layer": slices + 1, "tier": tier})
                    if grp.get("brand"):
                        owner[grp["brand"]] = store
                    swept += 1
                else:
                    unplaced.append({"group": grp, "reason": _why(grp, here, got)})
            rounds_hist.append({"tier": tier, "round": rnd, "taken": len(take),
                                "groups": len(assign) - placed_before,
                                "swept": swept})
        else:
            # ⚠ for-else:循环跑满 `max_rounds` 却**没有** break ⇒ 既没填满配额、
            #   池子也没见底,是被护栏截断的。**静默截断在这里最危险** ——
            #   报告会说"未发出 N 组",读起来像"闸挡的",而真相是我们根本
            #   没往下取。所有者会去改配置,而该做的是提高护栏或缩小配额。
            if sum(max(0, int(here[s]["quota"]) - got[s]) for s in here) > 0 \
                    and cursor < len(ordered):
                capped_tiers.append(tier)

    n_dir = sum(1 for a in assign if a["group"].get("store"))
    by_store = _by_store(live, assign, got)
    return {"assign": assign, "unplaced": unplaced,
            # 排在切口之外、本轮压根没进过任何一片的产品
            "queued": ordered[cursor:],
            "by_store": by_store, "groups": all_groups, "dropped": dropped,
            "dir_out": dir_out, "dir_trim": dir_trim, "rounds": rounds_hist,
            "params": {"slices": slices, "rounds": len(rounds_hist),
                       "directed_groups": n_dir,
                       "max_rounds": max_rounds,
                       "capped_tiers": capped_tiers,
                       "stores": len(live),
                       "skipped_stores": len(stores) - len(live)}}


def _prep_directed(orig: dict, here: dict, dir_out: list, all_stores: dict):
    """输入:定向组 + 本梯队店 + **全部店** → 输出:(能发的组 或 None, 剪掉件数, 原因)。

    ⚠ `all_stores` 不是冗余参数:`here` 已经把容量 0 的店筛掉了,只看它的话
    "占用店满了"会被报成"占用店本批不接货" —— 前者下架腾位下一轮自然进来,
    后者要人去查店铺配置。两者处置完全不同。

    ⚠ **容量闸必须压在类目/货期之上**。店已经满了的时候,类目对不对根本不影响
    结果 —— 开大类一件也救不回来,得先下架腾位。不这么排的实测后果
    (2026-08-16):A154杨凯迪 剩余容量 0,报告却把它记成「缺 Arts & Crafts
    161 件」,还附一句"给该店开这个大类就能救回" —— 把所有者送去做一件买不到
    任何货位的事。
    """
    st = here.get(orig["store"])
    if not st:
        full = all_stores.get(orig["store"])
        if full is not None and int(full.get("room") or 0) <= 0:
            dir_out.append((orig, "占用店容量已满"))
        else:
            dir_out.append((orig, "占用店本批不接货"))
        return None, 0, None
    if int(st.get("room") or 0) <= 0:
        dir_out.append((orig, "占用店容量已满"))
        return None, 0, None
    grp, trimmed, blocker = _fit_to_store(orig, st)
    if grp is None:
        dir_out.append((orig, f"占用店{blocker}不符"))
        return None, trimmed, blocker
    if not _gate(grp, orig["store"], st):
        # 逐件筛过之后还过不了 = 组级的渠道闸(渠道整组同进退,剪不动)
        dir_out.append((grp, "占用店渠道不符"))
        return None, trimmed, "渠道"
    return grp, trimmed, None


def _by_store(live: dict, assign: list, got: dict) -> dict:
    """输入:店表 + 发牌结果 → 输出:逐店验收数据。

    ⚠ 按**货位**累计,不是按组数。验收指标要拿它跟「配额占比」比,而配额的
    单位是货位 —— 数组数的话,拿到大组的店比值天然偏低、拿到小组的偏高,
    指标量的就成了"组的大小"而不是"分得公不公平"(2026-08-16 实测:同一批里
    0.16 与 2.05 并存,全是这个原因)。
    ⚠ 定向流**跳过了排队**(只有占用店能要),不参与轮转,验收指标也不该算它
    —— 那等于拿一个参数管不着的量去判"参数没调对"。
    """
    out: dict = {}
    for s in live:
        rows = [a for a in assign if a["store"] == s]
        layer_items: Counter = Counter()
        free_items: Counter = Counter()
        for a in rows:
            layer_items[a["layer"]] += int(a["group"]["size"])
            if a["group"].get("store") is None:
                free_items[a["layer"]] += int(a["group"]["size"])
        out[s] = {
            "groups": len(rows),
            "items": sum(int(a["group"]["size"]) for a in rows),
            "bound_items": sum(int(a["group"]["size"]) for a in rows
                               if a["group"].get("store") is not None),
            "quota": live[s]["quota"],
            "by_layer": layer_items,
            "by_layer_free": free_items,
        }
    return out


# 出比值的两个门槛,**都要过**:绝对量 ≥ 20 件,且占该店配额 ≥ 10%。
# ⚠ 只卡绝对量不够(2026-08-16 实测):A085 自由流 36 件、A162 24 件,都过了
# 20 件这道线,但它们的量太小、恰好全落在 L1,于是"自己的顶层占比"= 1.0,
# 比值双双等于 1÷base = **7.69**(两家一模一样,一眼就知道是假象)。
# 比值要有意义,这家店得在**多个层**都拿到过货 —— 拿它自己配额的一成当界。
MIN_FREE_FOR_RATIO = 20
MIN_FREE_SHARE_OF_QUOTA = 0.10


def acceptance(result: dict, top_layers: int = 1,
               rotation_only: bool = True) -> dict:
    """输入:`deal()` 产物 → 输出:§7.4b 的验收指标(逐店)。

    **顶层比值 = 「这家店拿到的自由流货里,顶层占多大比例」÷「全体的同一比例」**
    —— 直接回答所有者那句「好货有没有堆到一家店」:1.0 = 这家店的货**质量构成**
    与平均一致;1.5 = 它拿到的好货比例是平均的一倍半。

    ⚠ **分母是"它自己拿到的自由流总量",不是配额**(2026-08-16 第三版,前两版
    都在骗人)。拿配额当分母时:
      · 定向流把一家店填满 ⇒ 自由流分子接近 0、分母仍是全配额 ⇒ 比值 0.00,
        被标"越界",而它根本没参与竞争(A142 实测 0.00,分到的 1,909 全是定向);
      · 层内上限按**总接货量**算,定向流吃掉 L1 的名额后,这家店的自由流只能
        从下面几层拿 ⇒ 比值又偏低(C017 实测 0.50)。
    这两种都不是"参数没调对",而是分母选错了。改成"自己的总量"之后,比值
    与配额、容量、定向流体量全部无关,**只量质量构成**,这才是那句话的意思。

    ⚠ 自由流接货量不够的店**不出比值**(返回 None):分母太小时比值噪声大过
    信号。两道门槛都要过 —— 绝对量 ≥ `MIN_FREE_FOR_RATIO`,且占该店配额
    ≥ `MIN_FREE_SHARE_OF_QUOTA`。只卡绝对量会漏(见常量注释:36 件与 24 件
    双双得出 7.69 这个假象)。
    `top_layers` 决定"好货"算到第几层(默认只算 L1)。
    """
    by = result["by_store"]
    key = "by_layer_free" if rotation_only else "by_layer"
    tot_q = sum(v["quota"] for v in by.values())
    tot_i = sum(v["items"] for v in by.values())
    top = {s: sum(n for li, n in (v.get(key) or {}).items() if li <= top_layers)
           for s, v in by.items()}
    # 该店参与竞争的总量(默认口径:自由流全部层)
    mine = {s: (sum((v.get(key) or {}).values()) if rotation_only else v["items"])
            for s, v in by.items()}
    tot_top, tot_mine = sum(top.values()), sum(mine.values())
    base = (tot_top / tot_mine) if tot_mine else 0.0   # 全体的顶层占比
    out = {}
    for s, v in by.items():
        qs = v["quota"] / tot_q if tot_q else 0.0
        enough = (mine[s] >= MIN_FREE_FOR_RATIO
                  and mine[s] >= v["quota"] * MIN_FREE_SHARE_OF_QUOTA)
        out[s] = {
            "quota_share": qs,
            "top_items": top[s],
            "own_items": mine[s],
            # 这家店货里顶层的占比 ÷ 全体的同一比例;量太少时不给数
            "top_ratio": ((top[s] / mine[s]) / base
                          if enough and base and mine[s] else None),
            "batch_share": (v["items"] / tot_i) if tot_i else 0.0,
            # 单店接货量不得超 1.5 × 配额占比(这条仍按配额,它量的是"总量独吞")
            "over_cap": bool(tot_i and qs and v["items"] / tot_i > 1.5 * qs),
        }
    return out
