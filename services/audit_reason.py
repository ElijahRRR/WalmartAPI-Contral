"""最终拒绝理由映射(旧仓 `pipelines/reason_mapper.py` 逐字迁入),纯函数、零 DB、零 LLM。

作用:从 AuditOutcome 的全部 hits 推一个统一的 final_reason_category,
对齐 Walmart 37 条 Prohibited Product Policy 的 category_en,用于告诉卖家
"按哪条 Walmart 政策被拒"。落 `catalog.products.audit_reason`(runs 表无此列,旧仓同)。

**真实执行顺序**(以旧仓代码为准,首个 return 即出;旧仓模块 docstring 那份
"L3 第 1 / L4 第 2"的九级清单与代码不符,**没有迁**,照抄会误导后人):

  0.  verdict != 'reject'(含 outcome 为 None)          → None(pass/pending 恒为 None)
  1.  hits 中 rule_code ∈ HARD_RULE_CODES 且 detail 有 walmart_policy → 该 policy 原样
  1.2 hits 含黑名单中心三码(lark_blacklist_asin/seller/amazon_cat)→ **None**
      (内部黑名单决策,不对应任何 Walmart 政策;2026-08-18 修,此前漏到 4g)
  2.  l3 存在且 l3.verdict=='reject' 且归一化后的 reason_category 非 None → 归一化值
  1.5 hits 中 rule_code ∉ HARD_RULE_CODES 且 detail 有 walmart_policy → 该 policy 原样
  3.  l4 存在且 l4.verdict=='reject' → 第一条 confidence=='high' 的 dict issue:
      issue 文本含 'offensive' → 'Offensive Content',否则 'Intellectual Property'
  4a. hit_codes 含 publication_pt_forbidden                  → 'Intellectual Property'
  4b. (已删)L1 excluded 按 reason 推政策 —— 2026-08-20 随 excluded 链下线
  4c. _pt_to_policy(PT, title[:80]) 非 None                  → 该值
  4d. l1 存在且 hit_codes ∩ cert 三码 → 按 walmart_category 子串四选一(必返回)
  4e. hit_codes 含 trademark_live / title_desc_blacklist     → 'Intellectual Property'
  4f. hit_codes 含 phase0_brand_blacklist                    → 'Intellectual Property'
  4g. 以上全不中                                             → 'General-Use Products'

`all_hits` 遍历顺序(决定步 1/1.5 谁先命中,**第一个命中即胜出,无优先级比较**):
phase0.hits → l1.hits → l2.hits → l3.hits → l4.hits,层内按 append 顺序。

批次 B(零 LLM)下 l3/l4 恒为 None ⇒ 步 2、步 3 恒不触发,`_L3_NORMALIZE` 也用不上;
但**整段原样迁入**,批次 C 接上 LLM 时零改动(删了批次 C 会重写出一份不一致的)。

已知缺陷(**照迁不修**):
  - `_normalize_l3_cat` 第 2 步 `cat.lower() in ('none','null','')` **未 strip**,
    `" none "` 不会被判空,会落到查表 → `.title()` 变形;
  - 查表未命中时的 `cat.strip().title()` 会把 `PFAS Chemicals` 变 `Pfas Chemicals`、
    `Children's Products` 变 `Children'S Products`(表内已收的键不受影响);
  - `_pt_to_policy` 一律是**裸子串**判断,无词边界:`'ring'` 会被 "watering can" 命中。

不迁:旧仓 orchestrator 的 `'history_shortcut'` 兜底(新仓不迁历史短路,outcome 无该形态)。
落在 37 政策集合外的已知取值:`'Restricted/Illegal'`(R1 两条 hit 的常量,经步 1.5 会直接
成为最终理由)、`'Jewelry/Precious Metals'`(本文件第 10 组)、以及 `.title()` 变形值——
用 `known_policies_check` 记日志计数,**不改判定**(理由映射不是放行/拒绝依据)。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("services.audit_reason")


# L3 reject reason_category → Walmart 37 条政策标准名
_L3_NORMALIZE = {
    'intellectual property':      'Intellectual Property',
    'offensive content':          'Offensive Content',
    'military & law enforcement': 'Military & Law Enforcement',
    'drugs & paraphernalia':      'Drugs & Paraphernalia',
    'cosmetic products':          'Cosmetic Products',
    'medical devices':            'Medical Devices',
    'baby products':              'Baby Products',
    "children's products":        "Children's Products",
    'pet products':               'Pet Products',
    'dietary supplements':        'Dietary Supplements',
    'alcohol':                    'Alcohol',
    'tobacco & vaping':           'Tobacco & Vaping',
    'food products':              'Food Products',
    'hazardous items':            'Hazardous Items',
    'electronics & rf':           'Electronics & RF',
    'textiles & apparel':         'Textiles & Apparel',
    'auto & motor vehicles':      'Auto & Motor Vehicles',
    'digital goods':              'Digital Goods',
    'general-use products':       'General-Use Products',
    'pfas chemicals':              'PFAS Chemicals',
    'brand_misuse':               'Intellectual Property',     # brand 伪装归到 IP
    'brand misuse':               'Intellectual Property',
    # Walmart Content Standards (promotional claims, all-caps abuse 等)
    'content standards':          'Content Standards',
    'content_standards':          'Content Standards',
    'promotional content':        'Content Standards',
    'promotional':                'Content Standards',
    'content policy':             'Content Standards',
}


def _normalize_l3_cat(cat: str | None) -> str | None:
    """输入:L3 给的 reason_category → 输出:归一化后的 Walmart 政策名(或 None)。

    先判空(**此处未 strip**,故 " none " 会漏过判空),再 strip+lower 等值查表;
    查不到则 `cat.strip().title()`。
    """
    if not cat or cat.lower() in ('none', 'null', ''):
        return None
    return _L3_NORMALIZE.get(cat.strip().lower(), cat.strip().title())


def _pt_to_policy(pt: str | None, title: str = '') -> str | None:
    """输入:walmart PT 名(+ 标题)→ 输出:推出的 Walmart 政策名,无明确映射返回 None。

    归一化 `((pt or '') + ' ' + (title or '')[:80]).lower()` —— 只取**标题前 80 字符**;
    判定一律是 `k in p` 裸子串包含(无词边界,已知精度缺陷),按下列顺序首组命中即返回。
    """
    p = ((pt or '') + ' ' + (title or '')[:80]).lower()
    # 优先级从具体到宽泛
    if any(k in p for k in [
        'firearm', 'ammo', 'ammunition', 'crossbow', 'gun cleaning',
        'pistol', 'rifle', 'bb gun', 'pellet', 'airsoft', 'tactical knife',
        'pocket knife', 'throwing knife', 'switchblade', 'brass knuckle',
        'pepper spray', 'taser', 'stun gun',
    ]):
        return 'Military & Law Enforcement'
    if any(k in p for k in [
        'homeopathic', 'herbal', 'supplement', 'vitamin', 'drug test',
        'melatonin', 'prescription', 'otc drug', 'pharmaceutical',
    ]):
        return 'Dietary Supplements'
    if any(k in p for k in [
        'cosmetic', 'lipstick', 'makeup', 'nail polish', 'perfume',
        'fragrance', 'deodorant', 'tampon', 'essential oil',
        'body moisturizer', 'hand sanitizer', 'dry shampoo',
        'nail care', 'hair oil', 'teeth whitening',
    ]):
        return 'Cosmetic Products'
    if any(k in p for k in [
        'medical', 'thermometer', 'catheter', 'heating pad',
        'blood pressure', 'glucose meter', 'pulse oximeter',
        'nebulizer', 'cpap', 'syringe', 'diagnostic', 'surgical',
        'eye drop', 'spirometer', 'compression glove',
    ]):
        return 'Medical Devices'
    if any(k in p for k in [
        'pet food', 'dog food', 'cat food', 'animal feed', 'bird food',
        'pet treat', 'pet vitamin', 'pet supplement', 'animal supplement',
        'pet repellent',
    ]):
        return 'Pet Products'
    if any(k in p for k in ['alcohol', 'wine', 'beer', 'spirit', 'liquor', 'distill']):
        return 'Alcohol'
    if any(k in p for k in ['tobacco', 'cigarette', 'cigar', 'vape', 'e-cigarette', 'nicotine']):
        return 'Tobacco & Vaping'
    if any(k in p for k in ['baby', 'infant', 'toddler', 'crib', 'stroller', 'diaper']):
        return 'Baby Products'
    if any(k in p for k in ['apparel', 'clothing', 'shirt', 'pants', 'jeans', 'dress',
                             'sweater', 'jacket', 'underwear', 'bra', 'sock', 'footwear', 'shoe']):
        return 'Textiles & Apparel'
    if any(k in p for k in ['coin', 'currency', 'bullion', 'precious metal', 'jewelry', 'ring',
                             'necklace', 'bracelet']):
        return 'Jewelry/Precious Metals'
    return None


def compute_final_reason(
    outcome: Any,
    product: Any | None = None,
) -> str | None:
    """输入:AuditOutcome(+ 可选 ProductInfo)→ 输出:Walmart 政策 category_en 或 None。

    仅在 verdict='reject' 时有意义;pass/pending(以及 outcome 为 None)恒返回 None。
    完整顺序见模块 docstring —— 注意实现顺序是 0→1→2→1.5→3→4a..4g,
    **L0 硬规则优先于 L3**(phase0_forbidden_category 已直接对齐 37 条政策的
    category_en;L3 常因标题里的知名品牌词把"大类禁售"误判成 IP,让对齐率退化),
    其余带 walmart_policy 的规则则排在 L3 **之后**(步 1.5)。
    `product` 可为 None(此时步 4c 只按 PT 判)。
    """
    if outcome is None or outcome.verdict != "reject":
        return None

    # 分 hard/soft hit:
    #   hard = phase0_forbidden_category — 大类禁售精确对齐 Walmart 37 条政策
    #   soft = cert / brand / trademark 等 — 只标 L2 有命中, 实际原因要靠 L3 判
    # ⚠ 2026-08-20:原集合里的 forbidden_mega_cat(L2 R2)已随 R2 删除。
    HARD_RULE_CODES = {'phase0_forbidden_category'}

    # (1) hard 规则 walmart_policy — 最优先
    for h in outcome.all_hits:
        if h.rule_code in HARD_RULE_CODES:
            policy = (h.detail or {}).get('walmart_policy')
            if policy:
                return policy

    # (1.2) 黑名单中心三码(ASIN / 卖家 / 亚马逊类目)→ **None,不挂政策**。
    # 这是我们自己的内部黑名单决策,不对应任何一条 Walmart 政策;此前无分支
    # 会一路漏到 4g 兜底,F 列写出「ASIN 在黑名单中心 [政策:General-Use
    # Products]」这种自相矛盾的话(所有者 2026-08-18 实遇,B0F2ZS3M31 一张
    # 床头柜)。拿兜底政策去申诉口径也是错的 —— 没有政策就是没有政策。
    # ⚠ 品牌黑名单(phase0_brand_blacklist)不在此列:品牌拉黑多因知产风险,
    # 4f 归 Intellectual Property 是既定口径,别顺手改。
    # 这三码都是 Phase0 短路(单 hit 即终局),不会与政策规则同轮并存。
    _INTERNAL_BLACKLIST = {'phase0_lark_blacklist_asin',
                           'phase0_lark_blacklist_seller',
                           'phase0_lark_blacklist_amazon_cat'}
    if any(h.rule_code in _INTERNAL_BLACKLIST for h in outcome.all_hits):
        return None

    # (2) L3 语义判 (硬规则没给时, L3 最准)
    if outcome.l3 and outcome.l3.verdict == 'reject':
        cat = _normalize_l3_cat(outcome.l3.reason_category)
        if cat:
            return cat

    # (1.5) 其他带 walmart_policy 的规则 (未来扩展用, 兜底放这)
    for h in outcome.all_hits:
        if h.rule_code not in HARD_RULE_CODES:
            policy = (h.detail or {}).get('walmart_policy')
            if policy:
                return policy

    # (3) L4 视觉
    if outcome.l4 and outcome.l4.verdict == 'reject':
        for issue in (outcome.l4.image_issues or []):
            if isinstance(issue, dict) and issue.get('confidence') == 'high':
                t = (issue.get('issue') or '').lower()
                if 'offensive' in t:
                    return 'Offensive Content'
                return 'Intellectual Property'

    # (4) 兜底
    hit_codes = {h.rule_code for h in outcome.all_hits}

    # 出版物 IP
    if 'publication_pt_forbidden' in hit_codes:
        return 'Intellectual Property'

    # ⚠ 2026-08-20 删掉「L1 excluded_category 按 reason 推政策」一段:
    # 那条链(seed yaml 的 3C/服饰/汽配/带电禁售)已整体下线,四组关键词
    # 再没有输入来源。现在这类产品由 L2 R1 白名单判死,政策走下面的
    # PT 关键词 / walmart_category 两条兜底。

    # PT 关键词兜底 (常用于 cert/禁售没命中大类但 PT 能推)
    pt = outcome.l1.walmart_product_type if outcome.l1 else None
    title = product.title if product else None
    by_pt = _pt_to_policy(pt, title or '')
    if by_pt:
        return by_pt

    # cat_requires_cert_* 按 walmart_category 推
    if outcome.l1:
        wc = (outcome.l1.walmart_category or '').lower()
        if {'cat_requires_cert_hard', 'cat_requires_cert_small_part',
            'cat_requires_cert_soft'} & hit_codes:
            if 'baby' in wc or 'children' in wc:
                return "Children's Products"
            if 'health' in wc or 'medical' in wc or 'personal care' in wc:
                return 'Medical Devices'
            if 'electronics' in wc:
                return 'Electronics & RF'
            return 'General-Use Products'

    # IP 兜底
    if 'trademark_live' in hit_codes or 'title_desc_blacklist' in hit_codes:
        return 'Intellectual Property'
    if 'phase0_brand_blacklist' in hit_codes:
        return 'Intellectual Property'

    return 'General-Use Products'


def known_policies_check(reason: str | None, known: frozenset) -> bool:
    """输入:理由字符串 + 37 条政策 category_en 集合 → 输出:该理由是否落在集合内。

    纯校验,**不改任何判定**:调用方拿 False 去记日志计数即可(项目铁律:兜底触发
    必须记日志计数),绝不能因为映射不到就放行——理由映射不是放行/拒绝的依据。
    reason 为 None/空串表示"没有理由可校验"(pass/pending 的正常形态),回 True。
    已知会回 False 的值:'Restricted/Illegal'、'Jewelry/Precious Metals'、
    `.title()` 变形后的自由值。
    """
    if not reason:
        return True
    return reason in known


# ── 人话理由(给人看的那一面)───────────────────────────────────────────────
#
# ⚠ `final_reason_category` 回答的是「按 Walmart 37 条政策的哪一条被拒」——
# 那是**平台口径**,不是原因。其中 `General-Use Products` 更是第 4g 步的兜底
# (以上全不中),落在一把螺丝刀、一个土豆压泥器上时,人只会看得一头雾水
# (所有者 2026-08-16:「理由是 General-Use Products,这是什么意思」)。
#
# **真正的原因在命中的规则里**,而且 hit.detail 里本来就写着中文 note
# (「飞书维护的合规要求(含实验室证书/官方注册号),搬运模式做不了」)——
# 只是此前一个字都没露给人看。下面这层就是把它翻出来。
_RULE_CN = {
    "phase0_forbidden_category":      "禁售大类",
    "phase0_brand_blacklist":         "品牌黑名单",
    "phase0_trademark_symbol":        "标题含 ®/™ 商标符号",
    "phase0_patent_claim":            "文案自述专利保护",
    "phase0_lark_blacklist_asin":     "ASIN 在黑名单中心",
    "phase0_lark_blacklist_seller":   "卖家在黑名单中心",
    "phase0_lark_blacklist_amazon_cat": "亚马逊类目在黑名单中心",
    "cat_zh_blocked":                 "该类目不对中国卖家开放",
    "cat_access_blocked":             "该类目未开通",
    "cat_gate_pt_unknown":            "类目没定下来,判不了(待人工)",
    "cat_gate_pt_not_in_meta":        "该类目不在准入明细里,判不了(待人工)",
    "cat_requires_cert_hard":         "**该类目要求认证**(搬运模式提供不了)",
    "cat_requires_cert_small_part":   "电气小件,需人工确认能否免认证",
    "cat_requires_cert_soft":         "该类目要软合规(可填披露)",
    "walmart_strict_sensitive":       "沃尔玛敏感类目",
    "publication_pt_forbidden":       "出版物类目禁售",
    "title_desc_blacklist":           "标题/描述命中黑名单词",
    "trademark_live":                 "命中在效商标",
    "content_promotional":            "标题含促销用语",
    "unmapped_amazon_path":           "亚马逊类目映射不出沃尔玛类目",
    "pt_dict_fallback":               "类目靠字典回落(不是实证)",
    "l4_vision_violation":            "图片违规",
    "l4_images_partial":              "图片没取全",
    "l4_bad_schema":                  "视觉层返回坏 JSON",
}
# 这几条不是"被拒的原因",只是过程留痕。它们单独出现时不该当理由显示
_NOT_A_REASON = {"pt_dict_fallback", "l4_images_partial", "l4_bad_schema"}


def explain_hit(rule_code: str, detail: dict | None) -> str:
    """输入:命中的规则码 + detail → 输出:给人看的一句话(纯函数,零 DB)。

    优先用 detail 里现成的中文 note —— 那是规则作者当场写下的"为什么",
    比任何事后翻译都准。没有 note 才退回规则码的中文名。
    """
    d = detail or {}
    base = _RULE_CN.get(rule_code, rule_code)
    # ⚠ 键名必须**照着各规则真实写进 detail 的那些**取,不能想当然。
    # 首版只认 brand/category/keyword/matched 四个,而 Phase0 三表写的是
    # seller_id / asin / normalized —— 于是"黑名单命中"那一栏一个字都没有,
    # 人还是不知道命中的是哪一条(所有者 2026-08-16 实遇)。
    hit_val = next((d[k] for k in (
        "brand", "matched_brand", "normalized", "amazon_category_path",
        "seller_name", "seller_id", "asin", "category", "keyword", "matched",
    ) if d.get(k)), None)
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


def human_reason(hits: list[tuple[str, dict]], policy: str | None) -> str:
    """输入:[(规则码, detail)] + 37 政策类目 → 输出:上架表 F 列写的那句话。

    形态:`人话1;人话2 [政策:General-Use Products]`。
    政策类目留在方括号里 —— 它是与沃尔玛对话时的口径(申诉/开类目要报它),
    丢掉不对;但它不该是**唯一**能看到的东西。
    一条规则都没有(理论上不该发生)时只剩政策,并明说"未记录命中规则"。
    """
    said = [explain_hit(c, d) for c, d in hits if c not in _NOT_A_REASON]
    tail = f"[政策:{policy}]" if policy else ""
    if not said:
        return (f"未记录命中规则{tail}" if tail else "未记录命中规则")
    return ";".join(said[:3]) + (f" {tail}" if tail else "")


__all__ = ["compute_final_reason", "known_policies_check",
           "explain_hit", "human_reason"]
