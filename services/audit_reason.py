"""最终拒绝理由(类别)映射 + 命中规则的人话渲染。纯函数、零 DB、零 LLM。

作用两件,别混:

  · `compute_final_reason` —— 从 AuditOutcome 取**类别**(落
    `catalog.products.audit_reason`、飞书上架表 G 列)。2026-09-02 B1 起它是
    **查表**,不是推断:类别只有两种来源(`docs/audit_step3_spec.md` §二)——
    硬拒规则在自己的 `hit.detail["category"]` 里**自报**,L3 在结构化输出的
    `policy` 里给。查不到就是 None,**不许编一个**;
  · `explain_hit` / `explain_hits` —— 把命中的规则翻成给人看的一句
    (落 `catalog.products.audit_detail`、上架表 H 列的规则拒那一路,
    以及存量老行的渲染)。

**新顺序**(规格 §3.5,首个命中即出):

  0. verdict != 'reject'(含 outcome 为 None)   → None
  1. all_hits 按 phase0 → l1 → l2 → l3 顺序,第一条 detail 带 `category` 的 → 该值
  2. l3 判 reject                               → l3.policy(解析层已对表)
  3. 都没有                                     → None + `STATS['reason_missing']` + warning

⚠ **删掉的九步在这里留个墓碑**(2026-09-02 B1,规格 §3.5),免得有人照着旧
文档再写回来:步 1/1.5 的 `walmart_policy` 读取(规则改为自报 `category`)、
步 1.2 内部黑名单特判(改为自报 `内部黑名单`)、步 2 的 `_normalize_l3_cat`
归一化(L3 输出在解析层就对表了)、步 3 的 L4 关键词猜测、步 4a–4g 全部
(`_pt_to_policy` 十组裸子串、cert 分桶、以及那个把一把螺丝刀说成
`General-Use Products` 的兜底)、`known_policies_check`(枚举在解析层保证)。

**为什么零兜底**:兜底出来的类别会一路落库、进飞书 G 列、进申诉口径,
而没有任何东西会红 —— 所有者 2026-08-16 实遇「理由是 General-Use Products,
这是什么意思」。判拒而没有类别只可能是代码 bug(某条硬拒规则忘了自报),
落 NULL + 计数 + warning,让它自己现形。

⚠ **已知缺口只剩一条**(2026-09-03 C 批消化了另外两条):
  · `l4_vision_violation`(L4 视觉,penalty -100)—— **§二 的类别表没有它**:
    "图上有什么"映到哪条政策要所有者裁决,**不许在这里替它编一个**
    (L4 默认关 `-p l4=on` 才跑,面很小)。
  已消化:`cat_requires_cert_hard`(L2 R3 硬拒)整条删除,本 PT 的
  `requirements` 随产品进 L3 由 LLM 判;`made_in_usa_claim`(L2 R10)迁进
  L0 成 `phase0_made_in_usa`,自报 `Product claims`。
所以 **L4 关闭时 `reason_missing` 应恒为 0** —— 它现在是纯 bug 信号:
非 0 就是某条硬拒规则忘了自报 `detail['category']`,照旧落 NULL + 计数 +
warning,让它自己现形,不许兜底。
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from typing import Any

logger = logging.getLogger("services.audit_reason")

#: 硬拒规则自报类别的 detail 键(§二 表)。规则写它,本模块读它,别的谁都别碰。
CATEGORY_KEY = "category"


# ── 本轮计数(惯例照 `services/audit_l3.STATS`)────────────────────────────────
#
# 只收一件事:**判拒却给不出类别**。它是"某条硬拒规则忘了自报 category"的
# 唯一可见信号 —— 判定照样是 reject、落库照样成功,只是类别列空着。
STATS: Counter = Counter()
_STATS_KEYS = ("reason_missing",)      # 另有动态键 `reason_missing:<规则码组>`
_STATS_LOCK = threading.Lock()


def bump(key: str) -> int:
    """输入:计数键 → 输出:累加后的值(线程安全;判定并发 128,裸 += 会丢数)。"""
    with _STATS_LOCK:
        STATS[key] += 1
        return STATS[key]


def reset_stats() -> None:
    """输入:无 → 输出:无(清零并补齐固定键,便于摘要直接取)。"""
    with _STATS_LOCK:
        STATS.clear()
        STATS.update({k: 0 for k in _STATS_KEYS})


reset_stats()


def compute_final_reason(outcome: Any) -> str | None:
    """输入:AuditOutcome → 输出:类别(政策名枚举 / 两条非政策类别)或 None。

    仅在 verdict='reject' 时有意义;pass/pending(以及 outcome 为 None)恒 None。
    完整顺序见模块 docstring —— **全部是查表**:硬拒规则自报的 `category`
    (`services/audit_phase0` / `audit_l1_llm` / `audit_l2` 各自在 detail 里写,
    §二 表),或 L3 结构化输出的 `policy`(`services/audit_l3.parse_l3_reply`
    已对政策表解析过,回的是表内原拼写)。

    ⚠ 本函数**不做任何归一化、不查政策表**:那两件事分别在解析层与装配层
    做过了(`policy_names.resolve`),在这里再来一遍就是第二条实现路径 ——
    两处口径哪天不一致,谁也不会红。
    """
    if outcome is None or outcome.verdict != "reject":
        return None

    # (1) 规则自报(顺序 = all_hits 的 phase0 → l1 → l2 → l3,首个命中即出)。
    #     只有硬拒规则写这个键;软 hit 是证据不是判据,本来就不带。
    for h in outcome.all_hits:
        cat = (h.detail or {}).get(CATEGORY_KEY)
        if cat:
            return cat

    # (2) L3 语义判(它的类别在自己的结构化输出里,不在 hit.detail["category"])
    l3 = outcome.l3
    if l3 is not None and getattr(l3, "verdict", None) == "reject":
        policy = getattr(l3, "policy", None)
        if policy and policy != "none":
            return policy

    # (3) 没有类别 —— 不兜底。计数进 run 摘要,warning 点名 ASIN 与命中的规则码。
    # ⚠ **同一组规则码一轮只警告一次**:一条忘了自报的硬拒规则每轮能拒掉成千
    #   上万条,逐条打 warning 会把日志淹掉,而信息量只有第一条;计数照旧逐次累加
    codes = ",".join(sorted(h.rule_code for h in outcome.all_hits
                            if h.penalty < 0)) or "(无扣分规则)"
    bump("reason_missing")
    if bump(f"reason_missing:{codes}") == 1:
        logger.warning("判拒但没有类别:asin=%s 停在 %s,命中 %s —— 类别列写 NULL"
                       "(硬拒规则忘了自报 detail['category']?见 audit_reason "
                       "模块头注的已知缺口;同组规则码本轮只警告这一次)",
                       getattr(outcome, "asin", "?"),
                       getattr(outcome, "stage_stopped_at", "?"), codes)
    return None


# ── 人话理由(给人看的那一面)───────────────────────────────────────────────
#
# ⚠ 类别回答的是「按沃尔玛的哪一条政策被拒」—— 那是**平台口径**,不是原因。
# **真正的原因在命中的规则里**,而且 hit.detail 里本来就写着中文 note
# (「飞书维护的合规要求(含实验室证书/官方注册号),搬运模式做不了」)——
# 只是此前一个字都没露给人看。下面这层就是把它翻出来。
_RULE_CN = {
    "phase0_forbidden_category":      "禁售大类",
    "phase0_brand_blacklist":         "品牌黑名单",
    "phase0_trademark_symbol":        "标题含 ®/™ 商标符号",
    "phase0_patent_claim":            "文案自述专利保护",
    "phase0_made_in_usa":             "文案声明 Made in USA(无法实证)",
    "phase0_brand_mention":           "标题/描述提到黑名单品牌",
    "phase0_lark_blacklist_asin":     "ASIN 在黑名单中心",
    "phase0_lark_blacklist_seller":   "卖家在黑名单中心",
    "phase0_lark_blacklist_amazon_cat": "亚马逊类目在黑名单中心",
    "cat_zh_blocked":                 "该类目不对中国卖家开放",
    "cat_access_blocked":             "该类目未开通",
    "cat_gate_pt_unknown":            "类目没定下来,判不了(待人工)",
    "cat_gate_pt_not_in_meta":        "该类目不在准入明细里,判不了(待人工)",
    "cat_requires_cert_hard":         "**该类目要求认证**(搬运模式提供不了)",
    "cat_requires_cert_soft":         "该类目要软合规(可填披露)",
    "walmart_strict_sensitive":       "沃尔玛敏感类目",
    "publication_pt_forbidden":       "出版物类目禁售",
    "title_desc_blacklist":           "标题/描述命中黑名单词",
    "made_in_usa_claim":              "文案声明 Made in USA(无法实证)",
    "trademark_live":                 "命中在效商标",
    "content_promotional":            "标题含促销用语",
    "unmapped_amazon_path":           "亚马逊类目映射不出沃尔玛类目",
    "pt_dict_fallback":               "类目靠字典回落(不是实证)",
    "l4_vision_violation":            "图片违规",
    "l4_images_partial":              "图片没取全",
    "l4_bad_schema":                  "视觉层返回坏 JSON",
}
#: 这几条不是"被拒的原因",只是**过程留痕**:记的是我们自己链路里发生了什么
#: (类目靠字典回落、映射表曾标注无对应 PT、图没取全、视觉层返回坏 JSON),
#: 与产品违不违规无关。两个消费方共用这一张表,别各列各的:
#:   · `explain_hits` —— 单独出现时不当理由显示;
#:   · `audit_l3.summarize_evidence` —— 不送进 L3 的「上游证据」段
#:     (送了只会诱导 LLM 拿"内部没把类目定准"当拒绝理由)。
NOT_A_REASON = frozenset({"pt_dict_fallback", "unmapped_amazon_path",
                          "l4_images_partial", "l4_bad_schema"})

#: `explain_hit` 找"命中的是哪一个值"时按序试的键(照着各规则**真实写进
#: detail 的那些**取,不能想当然:首版只认 brand/category/keyword/matched 四个,
#: 而 Phase0 三表写的是 seller_id / asin / normalized —— 于是"黑名单命中"那一栏
#: 一个字都没有,人还是不知道命中的是哪一条,所有者 2026-08-16 实遇)。
#: ⚠ 2026-09-02 B1 **摘掉 `category`**:那个键现在是规则自报的**政策类别**
#: (§二),不是命中值;留着会让专利自述那条渲染成「命中:Intellectual Property」。
_HIT_VALUE_KEYS = ("brand", "matched_brand", "normalized",
                   "amazon_category_path", "seller_name", "seller_id", "asin",
                   "keyword", "matched")


def explain_hit(rule_code: str, detail: dict | None) -> str:
    """输入:命中的规则码 + detail → 输出:给人看的一句话(纯函数,零 DB)。

    优先用 detail 里现成的中文 note —— 那是规则作者当场写下的"为什么",
    比任何事后翻译都准。没有 note 才退回规则码的中文名。
    """
    d = detail or {}
    base = _RULE_CN.get(rule_code, rule_code)
    hit_val = next((d[k] for k in _HIT_VALUE_KEYS if d.get(k)), None)
    bits = [b for b in (
        d.get("note"),
        d.get("reason"),
        ("要求:" + "、".join(d["matched_hard_kws"][:3]))
        if d.get("matched_hard_kws") else None,
        ("认证字段:" + "、".join(str(x) for x in d["hard_cert_fields"][:3]))
        if d.get("hard_cert_fields") else None,
        (f"准入 {d['access_state']!r}") if d.get("access_state") else None,
        (f"中国卖家 {d['zh_can_do']!r}") if d.get("zh_can_do") else None,
        ("命中:" + str(hit_val)) if hit_val else None,
    ) if b]
    return f"{base}({';'.join(str(b) for b in bits)})" if bits else base


def explain_hits(hits: list[tuple[str, dict]]) -> str:
    """输入:[(规则码, detail)] → 输出:「人话1;人话2」(最多三条)。

    2026-09-02 B1 取代 `human_reason`:类别已经单列(products.audit_reason /
    上架表 G 列),这一句只说**规则说了什么**,不再拖一条「[政策:X]」尾巴。
    一条规则都没有(理论上不该发生)时明说"未记录命中规则",别给空白 ——
    空白让人以为"没写进来",而事实是"没有命中记录"。
    """
    said = [explain_hit(c, d) for c, d in hits if c not in NOT_A_REASON]
    return ";".join(said[:3]) if said else "未记录命中规则"


__all__ = ["CATEGORY_KEY", "NOT_A_REASON", "STATS", "bump", "reset_stats",
           "compute_final_reason", "explain_hit", "explain_hits"]
