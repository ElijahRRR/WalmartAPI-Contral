"""alloc_engine — 分层轮转发牌(§7.4b)。**纯函数**:不碰数据库/飞书/沃尔玛。

输入两堆已经算好的东西(候选组 + 店铺配额),输出"哪一组给哪家店"。
所有取数(产品分、店铺配额、占用、类目、渠道)都在调用方 —— 这样发牌
本身可以脱库跑测试,而分配是全项目最不能"跑一次看看"的东西。

## 一张牌 = 一个品牌组,不是一个产品

品牌排他(一个品牌只在一家店)决定了**组是原子的**:整组去同一家店,
不能拆。所以一张牌占的货位数 = 组内 ASIN 数(`size`),而配额/容量都是
按货位算的 —— 别把"发了多少张牌"当成"分了多少货"。

## 三条最容易写错的

1. **层按排名切,不按分数区间切。** 实测分数堆在 40~70,按"每 10 分一档"
   切会让中间一格装三分之一的货、顶上一格凑不出 10% ——"最好的那层"
   根本不成层。按排名切则层厚随分布自适应,**每层货量恒等**。
2. **配额在挑人之前判,判完就允许这一组把它顶过头。** 组是原子的,
   一个 40 件的组遇上只剩 12 个配额的店,若要求"发完仍不超配额"就永远
   发不出去。所以规则是:挑人时该店必须**还没到配额**;发完可以超,
   超出量 < 最大组的 size。真正不许越的是 `room`(物理容量),那是硬闸。
3. **层内还要有上限,光靠轮转不够。** 轮转管顺序,管不住"某张牌只有一家店
   接得了"——大类覆盖稀时那些组无视轮转顺序全落到那一两家头上,配额小的店
   于是把配额的一大截花在顶层。上限 = `quota × 层厚 × 松弛`;被上限压回去的
   组不丢,进**收尾扫描**(全部层发完后再走一遍,只剩配额与容量闸)。

## 确定性

同样的输入必须给同样的输出(占用一旦落库就撤不回,dry-run 看到的必须
就是 --execute 会做的)。所以:排序键一律排到**唯一**(比值 → 适配分 →
店名;组分 → 组 key),不用集合迭代序,不取随机数。
"""

import logging
import math
from collections import Counter

from services import store_targets

logger = logging.getLogger("services.alloc_engine")

# §7.4g #7:层厚 10%(所有者拍板 2026-08-15 晚)⇒ 10 层
LAYER_THICKNESS = 0.10
# §7.4g #7a:层内上限松弛。**它不是独立参数** —— 数值取自验收指标上界
# [0.7, 1.3],调宽验收区间就得同步调它,否则上限比验收线还严,层层往
# 收尾扫描里压。
LAYER_SLACK = 1.3

# unplaced 的三种原因。分开计数是有用的:
#   no_gate  = 没有任何店的闸放行 → **可以人工干预**(给某店开这个大类)
#   no_room  = 有店放行但容量塞不下 → 等下架腾位
#   no_quota = 有店放行也塞得下,但都到配额了 → 等下一批,不是毛病
NO_GATE, NO_ROOM, NO_QUOTA = "no_gate", "no_room", "no_quota"
REASON_LABEL = {
    NO_GATE: "没有店的类目/渠道闸放行(要它出货得先给某店开这个大类)",
    NO_ROOM: "有店放行但容量塞不下(等下架腾位)",
    NO_QUOTA: "候选店本批配额都满了(留池等下一批)",
}
# ⚠ 归类要取**走得最远**的那一条。别拿字符串比大小 —— 按字典序
# "no_gate" < "no_quota" < "no_room",顺序正好是错的,而且不会报错:
# 一批本该记成"等下一批"的货会全被记成"配置有问题",所有者跑去改类目配置
_REASON_RANK = (NO_GATE, NO_ROOM, NO_QUOTA)


def layer_cut(groups: list, thickness: float = LAYER_THICKNESS) -> list[list]:
    """输入:候选组(任意序)+ 层厚 → 输出:按组分降序切好的层列表。

    **按排名切**:每层装 `ceil(总数 × 层厚)` 组,最后一层可能不满。
    层数 = ceil(1/thickness),与店铺数无关 —— 层是把货按质量切档,
    店是每一档里轮流拿牌的人,两者没有对应关系。
    """
    if not groups:
        return []
    if not 0 < thickness <= 1:
        raise ValueError(f"层厚必须落在 (0, 1],给的是 {thickness}")
    ordered = sorted(groups, key=lambda g: (-float(g["score"]), str(g["key"])))
    per = max(1, math.ceil(len(ordered) * thickness))
    return [ordered[i:i + per] for i in range(0, len(ordered), per)]


def _gate(group: dict, store: str, st: dict) -> bool:
    """输入:一组 + 一店 → 输出:过不过**归属/类目/渠道**闸。

    容量与配额不在这里 —— 那两个是随发牌变化的量,而这里只判"静态相容"。
    合在一起写的话,"这家店永远接不了这个大类"与"这家店这批满了"会得出
    同一个结论,而这两件事的处置完全不同(前者要改配置,后者等下一批)。
    """
    # 品牌已被占用的组只能去占用店(§7.3 定向流)。**它不是另一条流水线,
    # 就是同一副牌里"只有一家店能要"的那种牌** —— 写成两个阶段的话,两边
    # 各有一套配额与容量记账,谁也不知道对方吃了多少(2026-08-16 实测:
    # 分阶段版本让定向流一口吃光批量,而且容量闸各判各的、双双超容)
    if group.get("store") is not None and group["store"] != store:
        return False
    # ⚠ 类目与配送时长的判定**一律走 store_targets 的谓词**,不在这里另写。
    # 「三列全空 = 不限制」「未填时长 = 不限」这类规则正着写反着写都像对的,
    # 各写一遍迟早分叉(本仓已在报告 vs 回填的行口径上栽过一次)。
    # 代价是 alloc_engine 不再是零依赖 —— 但 store_targets 的这两个谓词是
    # 纯函数,发牌照样脱库可测
    if not store_targets.allowed({"categories": st.get("categories") or []},
                                 group.get("category")):
        return False
    if not store_targets.lead_ok({"lead_limit": st.get("lead_limit")},
                                 group.get("lead")):
        return False
    ch = st.get("channel")
    # 店没填配送限制 → 不接自由流(store_targets 的口径,报告点名补填);
    # 组的渠道未知 → 调用方本就不该把它送进来,这里兜一道
    if not ch or group.get("channel") != ch:
        return False
    return True


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

    取**走得最远**的那一条:有店放行只是配额满,比"没人放行"轻得多,
    两者的处置完全不同(前者等下一批,后者要改飞书配置)。
    与 `_try_place` 分开写是有意的:合在一起的话,"重新解释一遍原因"这个
    动作会顺手把货发出去 —— 那时结果里就多了一条没人知道来路的分配。
    """
    rank = 0
    for s, st in stores.items():
        if not _gate(group, s, st):
            continue
        rank = max(rank, 1)
        if got[s] + int(group["size"]) > st["room"]:
            continue
        rank = max(rank, 2)
    return _REASON_RANK[rank]


def deal(groups: list, stores: dict, thickness: float = LAYER_THICKNESS,
         slack: float = LAYER_SLACK) -> dict:
    """输入:候选组 + 店铺配额表 → 输出:发牌结果(不改动入参)。

    groups: [{key, score, size, category, channel, tier?}]
      key      品牌组标识(品牌名;无品牌的组用 ASIN 自成一组)
      score    组分 = 组内最高产品分(§7.5)
      size     组内 ASIN 数 = 占几个货位
      category 组主大类;None 表示归不到类(受限店会拒收)
      channel  'FBA' / 'FBM';未知的组调用方就不该送进来
    stores: {店名: {quota, room, categories, channel, fit?, tier?}}
      quota    本批配额(货位数,§7.4a)
      room     剩余容量(货位数;**预支了待下架名额的那个 room**,§7.4f)
      fit      平手时的次级排序值,越大越优先(默认 0)
      tier     梯队(§7.5:1=在营主力先跑,2=空店只分剩余池);默认全 1

    返回 {assign, unplaced, by_store, layers, params}。
    """
    live = {s: v for s, v in stores.items()
            if int(v.get("quota") or 0) > 0 and int(v.get("room") or 0) > 0}
    tiers = sorted({int(v.get("tier") or 1) for v in live.values()})

    got: dict = {s: 0 for s in live}
    assign: list = []
    pool = list(groups)
    unplaced: list = []

    layers_hist: list = []
    for tier in tiers:
        # 梯队 2(空店)只分梯队 1 消化不完的剩余池:空店缺口天然巨大,
        # 同池竞争会把最好的品全吸走(§7.5)
        here = {s: v for s, v in live.items() if int(v.get("tier") or 1) == tier}
        if not here or not pool:
            continue
        pool, hist = _deal_tier(pool, here, got, assign, thickness, slack, tier)
        layers_hist.append({"tier": tier, "layers": hist})

    for g in pool:
        # 池子跑完还没发出去的,重判一次原因:此刻的 got 是最终状态,
        # 给出的原因才是所有者看到的那个世界的原因
        unplaced.append({"group": g, "reason": _why(g, live, got)})

    by_store: dict = {}
    for s in live:
        rows = [a for a in assign if a["store"] == s]
        layer_items: Counter = Counter()
        # 只有一家店能要的牌(定向流)**跳过了排队** —— 队首过不了归属闸就
        # 顺延,一路顺延到占用店,不管它排第几。所以它不参与轮转,验收指标
        # 也不该算它:那等于拿一个参数管不着的量去判"参数没调对"
        # (2026-08-16 实测:4 家越界的店,分到的货几乎全是定向流)
        free_items: Counter = Counter()
        for a in rows:
            # ⚠ 按**货位**累计,不是按组数。验收指标要拿它跟「配额占比」比,
            # 而配额的单位是货位 —— 数组数的话,拿到大组的店比值天然偏低、
            # 拿到小组的偏高,指标量的就成了"组的大小"而不是"分得公不公平"。
            # (生产实测 2026-08-16:同一批里 0.16 与 2.05 并存,全是这个原因)
            layer_items[a["layer"]] += int(a["group"]["size"])
            if a["group"].get("store") is None:
                free_items[a["layer"]] += int(a["group"]["size"])
        by_store[s] = {
            "groups": len(rows),
            "items": sum(int(a["group"]["size"]) for a in rows),
            "bound_items": sum(int(a["group"]["size"]) for a in rows
                               if a["group"].get("store") is not None),
            "quota": live[s]["quota"],
            "by_layer": layer_items,
            "by_layer_free": free_items,
        }
    return {"assign": assign, "unplaced": unplaced, "by_store": by_store,
            "layers": layers_hist,
            "params": {"thickness": thickness, "slack": slack,
                       "stores": len(live), "skipped_stores": len(stores) - len(live)}}


def _deal_tier(pool: list, here: dict, got: dict, assign: list,
               thickness: float, slack: float, tier: int) -> tuple[list, list]:
    """输入:剩余池 + 本梯队店 → 输出:(仍未发出的组, 逐层接货明细)。

    ⚠ `got` 与 `assign` 是**被就地改写的**:发牌是顺序过程,每发一张下一次
    挑人看的必须是更新后的世界(§7.4b 第 4 条)。传引用而不是返回新副本,
    是为了让"更新后的世界"这件事在代码里也只有一份。
    """
    layers = layer_cut(pool, thickness)
    n_layer = len(layers)
    # 层内上限:配额按层均摊再放松。松弛来自验收指标上界,见模块常量注释
    cap = {s: max(1, math.ceil(here[s]["quota"] / max(1, n_layer) * slack))
           for s in here}
    carry: list = []        # 被上限压回来的,下一层开头**优先**重试
    dead: list = []         # 闸/容量/配额都不放行 —— 本梯队没戏了
    hist: list = []
    n_carried = 0
    for li, layer in enumerate(layers, start=1):
        take: Counter = Counter({s: 0 for s in here})
        pending, carry = carry, []
        # ⚠ 压回来的必须排在新一层**前面**发。放到后面的话,它想去的那家店
        # 会先被本层的货填满配额 —— 上限于是从"晚一点拿"变成"永远拿不到",
        # 净发牌量下降。这正是层内上限最容易变成倒扣的地方
        for g in pending + layer:
            store, capped = _try_place(g, here, got, take, cap)
            if store:
                assign.append({"group": g, "store": store, "layer": li,
                               "tier": tier})
            elif capped:
                carry.append(g)
                n_carried += 1
            else:
                dead.append(g)
        hist.append(dict(take))
    # 收尾扫描:去掉层内上限再走一遍(顺序仍是组分降序,只是不再分层)
    left = list(dead)
    swept = 0
    for g in sorted(carry, key=lambda x: (-float(x["score"]), str(x["key"]))):
        store, _ = _try_place(g, here, got, Counter(), None)
        if store:
            assign.append({"group": g, "store": store, "layer": n_layer + 1,
                           "tier": tier})
            swept += 1
        else:
            left.append(g)
    if n_carried:
        # 措辞要经得起读:压回次数是**事件数**(一组可能被压回多层),而绝大多数
        # 会在后续层补上 —— 写成"N 次压回,0 组补发"看起来像丢了 N 组
        logger.info("梯队 %s:层内上限触发 %d 次(松弛 %.2f);后续层内消化 %d 次,"
                    "收尾扫描补发 %d 组,最终仍未发出 %d 组",
                    tier, n_carried, slack, n_carried - len(carry),
                    swept, len(carry) - swept)
    return left, hist


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
