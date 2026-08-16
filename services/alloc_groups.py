"""候选 → 品牌组:一张牌的组装(纯函数,不碰数据库)。

**为什么组是原子的**:品牌排他(一个品牌只在一家店)。所以发牌发的不是产品,
是"这个品牌名下这一批产品"整体去哪家店。组一旦拆开分给两家店,排他就破了。

由此带来三个必须在这里定死的决定 —— 三个都会静默地把货分错地方:

1. **组分 = 组内最高产品分**(§7.5)。取中位数会让"一个爆款带一堆平庸品"的
   品牌整体掉下去,而那正是最该抢的品牌。
2. **组大类 = 组内件数最多的大类**。一个品牌横跨两个大类时,少数派那部分
   跟着走 —— 因为它们本来就只能跟品牌走。**件数并列时按大类名定序**,
   否则同样的输入会给出不同的组大类(占用撤不回,不许有随机)。
3. **组货期 = 组内**最长**的那个;有一件采不到就整组算未知**。店铺的
   「配送时长限制」是逐店硬闸,而组必须整组去一家店 —— 取最长才能保证
   "这家店收得了这个品牌的每一件"。采不到的当未知(不是当快),受限店会拒收:
   所有者填这一列就是明确不要慢货,拿"没采到"当"够快"是替他做了他没做的决定。
   ⚠ 定向流另有优待:去向店已固定,`alloc_plan._fit_to_store` 会**按件剪**掉
   超期的那些,不必整组等。
4. **组渠道 = 组内件数最多的渠道,少数派整体剔除**。这一条最容易写错:
   店铺是一店一渠道,而品牌必须整组去一家店 —— 混渠道的品牌里,不合该店
   渠道的那部分**上不了架**。留在组里会让 size 虚高、配额被吃掉却上不了货。
   渠道未知的产品同样剔除(**不猜**:猜错等于把 FBM 的货分给 FBA 店)。
5. **搭车闸:组内产品分低于 `RIDE_FLOOR` 的不跟车**(所有者 2026-08-16 定 50)。
   牌按组分(= 组内最高分)排队,所以一个爆款能把同品牌的平庸品一路带进第一层。
   实测那一跑里大量 30 多分的货靠 90 多分的同门上了架,而它们挤掉的是排队里
   **组分更高的整组**。见下面 `RIDE_FLOOR` 的注释,尤其是不剪的那种情况。
"""

import logging
from collections import Counter

from services import brand_key as bk

logger = logging.getLogger("services.alloc_groups")

NO_BRAND = "(无品牌)"

# 搭车地板:组里有 ≥ 这个分的品时,低于它的同门**不跟车上架**。
#
# ⚠ 它与淘汰线(`product_score.CUTOFF`,35)是**两条不同的线**,别合并:
#   · 淘汰线 = 一个产品够不够格进候选池(它自己的准入);
#   · 搭车地板 = 一个产品够不够格**沾同品牌高分品的光插队**。
#
# ⚠ **整组都低于地板时一件都不剪**。那种组没有"车"可搭 —— 组分就是它自己的
#   真实水平,在自由流里几乎永远轮不到;而定向流是店铺补齐**自己已占的品牌**,
#   剪光会让店铺连自己名下的品牌都上不了任何货。准入归淘汰线管,不归这里管。
#
# 由此有个看着别扭但站得住的结果:44 分的品,若同品牌有个 92 分的,它**不会**
# 被分配(不许搭车);若同品牌顶格就是 44,它照常排队。规则管的是"插队",
# 不是"这个品行不行"。
RIDE_FLOOR = 50.0


def _major(vals) -> str | None:
    """输入:值序列 → 输出:出现最多的值;**并列按值本身定序**。

    `Counter.most_common()` 并列时按插入序 —— 那取决于上游 SQL 的行序,
    等于给分配引入了一个看不见的随机源。这里显式排到唯一。
    """
    c = Counter(v for v in vals if v)
    if not c:
        return None
    return sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0]


def build(candidates: list, claimed_brands: dict | None = None,
          bound_asins: dict | None = None) -> dict:
    """输入:打过分的候选(见 product_pool.score_all)+ {品牌键: 占用店}
    (+ {ASIN: 占用店},仅**已占位但还没上架**的)→ 输出:{free, directed, dropped}。

    free      自由流的组 [{key, score, size, category, channel, items, brand}]
    directed  定向流:只有一家店能要(品牌被占,或组里的 ASIN 被占)[{..., store}]
    dropped   逐类计数:每一类都是"这批货为什么没进牌堆",报告要逐条报出来。
              ⚠「搭车品」那一类与其余几类**性质不同**:其余是这批货有毛病
              (渠道不明、占用打架),搭车品是货本身合格(过了淘汰线)、
              只是不许沾同门高分品的光插队 —— 别把两者当同一回事去排查

    ★ `bound_asins` 是所有者 2026-08-16 定的口径:**占位不等于上架**。上一轮
    分配定了、货还没上的 ASIN **照常进候选池**(否则方案表就成了增量,上一轮
    的上架指令从表上消失),但它只能回占用它的那家店 —— 与品牌占用同一处理,
    都是"只有一家店能要的牌"。
    """
    held = claimed_brands or {}
    bound = bound_asins or {}
    dropped: Counter = Counter()
    buckets: dict = {}
    for c in candidates:
        key = bk.brand_key(c.get("brand"), c.get("manufacturer"))
        # 无品牌:**每个 ASIN 自成一组**(§7.5)。合成一个大组的话,
        # "无品牌"这一坨会整体去一家店,而它们之间毫无关系
        gk = key or f"{NO_BRAND}:{c['asin']}"
        buckets.setdefault(gk, {"brand": key, "items": []})["items"].append(c)

    free, directed = [], []
    for gk, b in sorted(buckets.items()):
        items = b["items"]
        # 搭车闸(模块 docstring 第 5 条)。**必须剪在渠道判定之前**:
        # [92分 FBA, 40分 FBM, 39分 FBM] 先判渠道的话,两个搭车品把整组判成
        # FBM,反手把那个 92 分的当少数派剔掉 —— 车上的人把司机赶下了车。
        if any(x["score"] >= RIDE_FLOOR for x in items):
            ride = [x for x in items if x["score"] < RIDE_FLOOR]
            if ride:
                dropped[f"搭车品(产品分 < {RIDE_FLOOR:.0f})"] += len(ride)
                items = [x for x in items if x["score"] >= RIDE_FLOOR]
        ch = _major(x["channel"] for x in items)
        if ch is None:
            # 整组都没有可信渠道 —— 渠道闸是硬闸,没有渠道就无从判起
            dropped["渠道未知(整组)"] += len(items)
            continue
        keep = [x for x in items if x["channel"] == ch]
        if len(keep) < len(items):
            dropped["渠道少数派(随品牌走不了)"] += len(items) - len(keep)
        # 这一组归谁:品牌占用优先(品牌排他是更强的约束);没有品牌占用时,
        # 看组里有没有已占位的 ASIN —— 主要是无品牌的品,它们各自成组
        owner = held.get(b["brand"]) if b["brand"] else None
        if owner is None:
            owner = _major(bound.get(x["asin"]) for x in keep)
        if owner is not None:
            # ⚠ 组归 A、组里却有被 B 占着的 ASIN:那是两条占用互相矛盾
            # (落库时 claim_many 会拒掉并报冲突)。方案表不该写"把它上到 A",
            # 所以在这里剔掉并计数,让人看见有多少条占用打架
            wrong = [x for x in keep if bound.get(x["asin"], owner) != owner]
            if wrong:
                dropped["ASIN 占用与组归属矛盾"] += len(wrong)
                keep = [x for x in keep if x not in wrong]
            if not keep:
                continue
        leads = [x.get("lead") for x in keep]
        g = {"key": gk, "brand": b["brand"],
             "score": max(x["score"] for x in keep),
             "size": len(keep),
             "category": _major(x["category"] for x in keep),
             # 取**最长**;任一件采不到就整组未知 —— 见模块 docstring 第 3 条
             "lead": None if any(v is None for v in leads) else max(leads),
             "channel": ch, "items": keep}
        if owner is not None:
            directed.append({**g, "store": owner})
        else:
            free.append(g)
    return {"free": free, "directed": directed, "dropped": dropped}
