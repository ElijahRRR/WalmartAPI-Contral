"""LLM 用量 → 成本折算(纯函数,零 DB、零网络)。

分工(2026-08-21 定稿):
  · api/llm.py    记 **token**(接口回的事实):按 (模型, 用途, 峰谷时段) 累加
  · registry      存 **单价**(会变的业务参数)与峰谷时段规则
  · 本模块        把两者折算成钱,并渲染成一行摘要
换模型 / 换供应商时,只有 registry 那张表要动。

为什么不直接问接口要价格:**DeepSeek 没有这样的端点**(2026-08-21 核过官方
文档)。`/user/balance` 只回余额,`/chat/completions` 的 usage 只回 token 数,
没有任何接口返回单价或某次调用的花费。所以单价必须落在本地。

**不认识的模型只报 token 不报钱**,并且在摘要里点名说"这个模型没有计价"。
按 0 计价会产出一个看着像钱、其实是编的数字 —— 那比不报更糟。
"""

from __future__ import annotations

from registry import resources

_M = 1_000_000


def cost_of(model: str, tier: str, row: dict) -> float | None:
    """输入:模型 + 时段 + 一行用量计数 → 输出:USD 金额,或 None(该模型无计价)。

    输入 token 分两档算:命中前缀缓存的便宜一个数量级。供应商没回
    cache_hit/cache_miss 拆分时(两者都是 0)退回按 prompt_tokens 全额
    当未命中算 —— **偏贵不偏便宜**,估出来的账不会让人以为花得比实际少。
    """
    table = resources.LLM_PRICING.get(model)
    if not table or tier not in table:
        return None
    p_hit, p_miss, p_out = table[tier]
    hit, miss = row.get("cache_hit", 0), row.get("cache_miss", 0)
    if not hit and not miss:
        miss = row.get("prompt", 0)
    return (hit * p_hit + miss * p_miss
            + row.get("completion", 0) * p_out) / _M


def summarize(usage_stats: dict) -> list[str]:
    """输入:api.llm.USAGE_STATS → 输出:摘要行列表(空用量返回空列表)。

    按**用途**汇总(L1 rerank / L3 语义 / 上架映射各花多少),这是换模型时
    真正要看的维度;单价与峰谷只在总额里体现。
    """
    if not usage_stats:
        return []
    by_purpose: dict[str, dict] = {}
    total_cost, unpriced = 0.0, set()
    for (model, purpose, tier), row in usage_stats.items():
        agg = by_purpose.setdefault(
            purpose, {"calls": 0, "prompt": 0, "completion": 0,
                      "cache_hit": 0, "cache_miss": 0, "cost": 0.0,
                      "models": set()})
        for k in ("calls", "prompt", "completion", "cache_hit", "cache_miss"):
            agg[k] += row.get(k, 0)
        agg["models"].add(model)
        c = cost_of(model, tier, row)
        if c is None:
            unpriced.add(model)
        else:
            agg["cost"] += c
            total_cost += c

    lines = []
    for purpose, a in sorted(by_purpose.items()):
        hit, miss = a["cache_hit"], a["cache_miss"]
        cache = f",缓存命中 {hit / (hit + miss):.0%}" if (hit + miss) else ""
        money = (f" ≈ ${a['cost']:.2f}"
                 if not (a["models"] & unpriced) else "(该模型无计价)")
        lines.append(
            f"  {purpose}:调用 {a['calls']} 次,"
            f"入 {a['prompt'] / _M:.2f}M / 出 {a['completion'] / _M:.2f}M token"
            f"{cache}{money}")
    head = (f"LLM 用量合计 ≈ ${total_cost:.2f}"
            if total_cost else "LLM 用量(无可计价模型)")
    if unpriced:
        # 静默按 0 计价 = 假账。点名说哪个模型没价,让人知道这个数字不全
        head += (f";⚠ 未计价模型 {sorted(unpriced)} —— 在 "
                 f"registry.LLM_PRICING 补一行(单价来源:"
                 f"{resources.LLM_PRICING_SOURCE})")
    return [head, *lines]


__all__ = ["cost_of", "summarize"]
