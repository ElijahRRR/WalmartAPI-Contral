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
"""

import logging
from collections import Counter

from services import brand_key as bk

logger = logging.getLogger("services.alloc_groups")

NO_BRAND = "(无品牌)"


def _major(vals) -> str | None:
    """输入:值序列 → 输出:出现最多的值;**并列按值本身定序**。

    `Counter.most_common()` 并列时按插入序 —— 那取决于上游 SQL 的行序,
    等于给分配引入了一个看不见的随机源。这里显式排到唯一。
    """
    c = Counter(v for v in vals if v)
    if not c:
        return None
    return sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0]


def build(candidates: list, claimed_brands: dict | None = None) -> dict:
    """输入:打过分的候选(见 product_pool.score_all)+ {品牌键: 占用店}
    → 输出:{free, directed, dropped}。

    free      自由流的组 [{key, score, size, category, channel, items, brand}]
    directed  定向流:品牌已被占用,只能去那家店 [{..., store}]
    dropped   逐类计数:每一类都是"这批货为什么没进牌堆",报告要逐条报出来
    """
    held = claimed_brands or {}
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
        ch = _major(x["channel"] for x in items)
        if ch is None:
            # 整组都没有可信渠道 —— 渠道闸是硬闸,没有渠道就无从判起
            dropped["渠道未知(整组)"] += len(items)
            continue
        keep = [x for x in items if x["channel"] == ch]
        if len(keep) < len(items):
            dropped["渠道少数派(随品牌走不了)"] += len(items) - len(keep)
        leads = [x.get("lead") for x in keep]
        g = {"key": gk, "brand": b["brand"],
             "score": max(x["score"] for x in keep),
             "size": len(keep),
             "category": _major(x["category"] for x in keep),
             # 取**最长**;任一件采不到就整组未知 —— 见模块 docstring 第 3 条
             "lead": None if any(v is None for v in leads) else max(leads),
             "channel": ch, "items": keep}
        if b["brand"] and b["brand"] in held:
            directed.append({**g, "store": held[b["brand"]]})
        else:
            free.append(g)
    return {"free": free, "directed": directed, "dropped": dropped}
