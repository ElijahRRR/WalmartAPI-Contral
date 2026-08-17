"""Phase 0 精准前置拦截:飞书黑名单 / 类目禁售 / 商标符号 / 品牌黑名单。

移植自旧仓 pipelines/phase0.py + phase0_lark_blacklist.py + phase0_category.py
+ phase0_trademark.py + phase0_brand.py 五个文件(a565d95),合并为一个模块。

设计原则(旧仓 phase0.py:3-13 原文):Phase0 只处理"100% 确定不能上"的情况,
不做文本级启发式;title 含品牌词这类模糊信号交 L2 R4 / L3 判。

四条规则**串行短路**,任一命中即整条流水线终止,每条最多产出 1 条 RuleHit,
penalty 恒 -100,stage 恒 "L0"。所以同一产品即便同时踩飞书黑名单和禁售类目,
audit_hits 也只有 1 行 —— 这是旧仓行为,照迁。

与旧仓的结构性差异(只改取数方式,不改判定):
  - 旧仓四个 check() 各自 lru_cache + psycopg.connect 直连 DB;新仓禁止 services
    自连 DB(铁律),三张飞书黑名单表与品牌黑名单一律由调用方装配进 ctx
    (services/audit_rules.AuditContext),本模块**零 DB 访问、零文件读取**。
  - 因此旧仓的 fail-soft(黑名单表加载异常 → 空 frozenset,等同该项过滤不存在)
    与 phase0_brand 的 fail-fast(无 try/except,DB 挂则整审崩)两种不对称策略
    都落在 ctx 装配侧,本模块只按 ctx 里给到的集合判。

已知缺陷一律照迁(spec_phase0 §3.2b/§4.2/§4.4/§5.3/§6.4),逐处在注释标注,
本批次不修。

public:
  check(product, ctx) -> Phase0Result
  normalize_amazon_category(s) -> str      # risk_sync 等外部复用
  FORBIDDEN_AMAZON_TOPS
"""

from __future__ import annotations

import re
from typing import Any

from services.audit_models import Phase0Result, ProductInfo, RuleHit

# =============================================================
# 规则 1 —— 飞书黑名单(seller_id / asin / amazon 类目)
# =============================================================

_WS_RE = re.compile(r"\s+")


def normalize_amazon_category(s: str | None) -> str:
    """输入:Amazon 类目路径(任意分隔符)→ 输出:归一化路径('->' 分隔、无空白)。

    逐字迁自 phase0_lark_blacklist.py:48-65。飞书表入库侧与查询侧共用本函数,
    保证两边口径对齐,所以**任何改动都会让 11,810 行存量黑名单失配**。

    examples:
      'Clothing, Shoes & Jewelry > Men > Shoes' → 'Clothing,Shoes&Jewelry->Men->Shoes'
      'VideoGames->XboxOne->Games'              → 'VideoGames->XboxOne->Games'
      '  Home & Kitchen  >  Cups  '             → 'Home&Kitchen->Cups'

    要点:'->' 必须先于 '>' 替换(否则 'a->b' 会留下孤儿 '-');'/' 也是分隔符
    (副作用:'24/7' 被拆成两级,已知误差照迁);段内空白是**全删**不是压缩;
    大小写与逗号/& 等标点原样保留 → 比较是大小写敏感的。
    """
    if not s:
        return ""
    # 1. 把 '->' 临时替换成单一分隔符 '\x00' 避免与单 '>' 冲突
    raw = s.replace("->", "\x00").replace(">", "\x00").replace("/", "\x00")
    parts: list[str] = []
    for seg in raw.split("\x00"):
        seg = _WS_RE.sub("", seg).strip()
        if seg:
            parts.append(seg)
    return "->".join(parts)


def _check_lark_blacklist(product: ProductInfo, ctx: Any) -> Phase0Result:
    """输入:产品 + 上下文 → 输出:Phase0Result(飞书三表任一等值命中即 blocked)。

    顺序 seller_id → asin → amazon_category,任一命中即返回;**某项为空只跳过
    该项**,不提前 return(phase0_lark_blacklist.py:136-191)。
    三表归一化口径互不相同:前两项仅 strip(大小写敏感,ASIN 不做 upper —— 飞书
    录了小写 asin 就永远命中不到,已知缺陷照迁),第三项走 normalize_amazon_category。
    """
    # 1. 卖家 ID
    seller = (product.seller_id or "").strip()
    if seller:
        if seller in ctx.phase0_sellers:
            return Phase0Result(
                blocked=True,
                hits=[RuleHit(
                    stage="L0",
                    rule_code="phase0_lark_blacklist_seller",
                    penalty=-100,
                    detail={
                        "seller_id": seller,
                        "seller_name": product.seller_name or None,   # 空串转 None
                        "source": "blacklist_center",
                    },
                )],
            )

    # 2. ASIN
    asin = (product.asin or "").strip()
    if asin:
        if asin in ctx.phase0_asins:
            return Phase0Result(
                blocked=True,
                hits=[RuleHit(
                    stage="L0",
                    rule_code="phase0_lark_blacklist_asin",
                    penalty=-100,
                    detail={
                        "asin": asin,
                        "source": "blacklist_center",
                    },
                )],
            )

    # 3. Amazon 类目(归一化后等值;取**完整路径**,与规则 2 只取顶级段是两个口径)
    cat_norm = normalize_amazon_category(product.amazon_category_path)
    if cat_norm:
        if cat_norm in ctx.phase0_cats:
            return Phase0Result(
                blocked=True,
                matched_category=cat_norm,
                hits=[RuleHit(
                    stage="L0",
                    rule_code="phase0_lark_blacklist_amazon_cat",
                    penalty=-100,
                    detail={
                        "amazon_category_path": product.amazon_category_path,
                        "normalized": cat_norm,
                        "source": "blacklist_center",
                    },
                )],
            )

    return Phase0Result(blocked=False)


# =============================================================
# 规则 2 —— Amazon 顶级类目精准禁售(4 大类)
# =============================================================

# Amazon 顶级类目 → (禁售原因, Walmart 政策 category_en)
# key 必须是 Amazon 官方顶级类目名 (精确到大小写+标点)
# walmart_policy 对齐 Walmart 37 条 Prohibited Product Policy category_en
# (逐字迁自 phase0_category.py:32-52,reason/policy 原文进 detail)
#
# ⚠ **只有旧仓这 4 个**(2026-08-17 所有者裁决 A,详见下方"摘掉 4 个"注释块)。
# 这条规则只看路径**第一段**,而 Amazon 顶级类目的粒度是"筐"不是"品":
# 往这张表里加大类的代价 = 把整个筐里的杂货一起拒掉,且停在 L0 连类目都不判。
# 加新 key 之前先问:这个筐里**每一件**都该拒吗?答不上来就别加,交给 L2。
FORBIDDEN_AMAZON_TOPS: dict[str, tuple[str, str]] = {
    "Books":                    ("Books 禁售: 版权/内容合规风险, 搬运模式不适合",
                                 "Intellectual Property"),
    "Kindle Store":             ("Kindle Store 禁售: 电子书版权",
                                 "Digital Goods"),
    "Clothing, Shoes & Jewelry":("服饰/鞋/珠宝禁售: 尺码/SKU管理复杂 + 珠宝 AML 审批",
                                 "Textiles & Apparel"),
    "Automotive":               ("汽配禁售: DOT/SAE 认证 + 安全件责任 + CARB/delete kit",
                                 "Auto & Motor Vehicles"),
}

# ── 摘掉 4 个大类(2026-08-17,所有者裁决 A)────────────────────────────────
#
# 批次 B 迁入时在旧仓 4 个之外**新增**了 4 个:Beauty & Personal Care /
# Health & Household / Health & Personal Care / Grocery & Gourmet Food,
# 理由写的是"Walmart 整类 restricted,中国卖家 0 可能合规"。已全部删除。
#
# 触发:所有者拿 B0BWMVQHVJ 来问——一包**牛皮纸礼品袋**被拒,理由
# 「药品/膳食补充剂 restricted: FDA 注册 + AML vetting + MoCRA」。查 detail:
#   full_path = 'Health & Household > Stationery & Gift Wrapping Supplies
#                > Gift Wrapping Supplies > Gift Bags'
# 规则没跑偏(match_type='exact'),是**判据粒度太粗**:Amazon 的
# Health & Household 本身就是杂物筐,底下混着文具礼品包装、家居清洁、纸品、
# 宠物用品;只取第一段 = 把整个筐一起拒。
#
# 为什么摘掉是安全的 —— 药品/补剂**本来就有两道更精准的闸**,都在 L2:
#   · R2  refdata/audit/forbidden_categories_zh_seller.yaml 的 drugs_supplements
#         按 **Walmart PT 名**关键词判(Vitamin/Probiotic/Dietary Supplement/
#         Melatonin/Homeopathic/Herbal Remedy…),另有 medical_devices 等 key
#   · R0  audit_l2._FORBIDDEN_WALMART_MEGA_CATEGORIES 按 **Walmart 类目**硬禁
#         (Health & Personal Care / Beauty / Food & Beverage / Baby …)
# 那两条判的是"这东西到底是什么",Phase0 这条判的是"它在亚马逊被挂在哪个筐里"。
# 前者才是判据,后者是筐 —— 所以是**去重**,不是"放开一道闸"。
#
# ⚠ 代价说清楚:PT 解不出**且** Walmart 类目也拿不到的真药品,不再有硬闸兜,
# 会往下走到 L3 语义层。这是裁决 A 明知并接受的换取(换回被误杀的杂货)。
# 要再收紧,正确做法是补 L2 的 PT 词表,**不是**把大类塞回上面那张表。

def _extract_top(amazon_category_path: str | None) -> str:
    """输入:Amazon 路径 → 输出:顶级类目段(第一个 '>' 之前,两端去空白)。

    逐字迁自 phase0_category.py:55-62。**只认 '>'**,不认 '->' 与 '/':
    输入 'VideoGames->XboxOne' 会得到 'VideoGames-'(尾横杠留着,永远匹配不上
    8 个 key)。这是与规则 1 归一化口径不一致的已知缺陷,照迁不修 —— 新仓
    amazon_category 由 product_ingest 用 ' > ' 拼接,实际不踩这个坑。
    """
    if not amazon_category_path:
        return ""
    p = amazon_category_path.strip()
    if ">" in p:
        p = p.split(">", 1)[0]
    return p.strip()


def _check_forbidden_category(product: ProductInfo) -> Phase0Result:
    """输入:产品 → 输出:Phase0Result(顶级类目命中 8 大禁售类即 blocked)。

    两级比较(phase0_category.py:65-84):先大小写+标点全敏感的精确等值,
    失败再去逗号+小写等值。净效果 = 大小写不敏感、逗号可有可无、内部空白必须
    存在(压缩成单空格),其余标点(& / -)必须一致。
    第二级命中后 top_norm 回写成规范 key,故 detail 里永远是 8 个原文之一。
    """
    top = _extract_top(product.amazon_category_path)
    if not top:
        return Phase0Result(blocked=False)

    # 归一化比较 (压缩空格, 不改大小写 — 因为 Amazon 顶级类目是固定大小写)
    top_norm = " ".join(top.split())

    # 精准等值
    entry = FORBIDDEN_AMAZON_TOPS.get(top_norm)
    if entry is None:
        # 再尝试对某些标点变体 (e.g. "Clothing Shoes & Jewelry" 缺逗号)
        for key in FORBIDDEN_AMAZON_TOPS:
            if key.replace(",", "").lower() == top_norm.replace(",", "").lower():
                entry = FORBIDDEN_AMAZON_TOPS[key]
                top_norm = key
                break
    if not entry:
        return Phase0Result(blocked=False)

    reason, walmart_policy = entry

    hit = RuleHit(
        stage="L0",
        rule_code="phase0_forbidden_category",
        penalty=-100,
        detail={
            "amazon_top_category": top_norm,
            "reason": reason,
            "walmart_policy": walmart_policy,   # 理由映射第 1 优先级读这个
            "full_path": product.amazon_category_path,
            # 走了第二级模糊匹配时这里仍写 "exact",标签不准,照迁不修
            "match_type": "exact",
        },
    )
    return Phase0Result(blocked=True, matched_category=top_norm, hits=[hit])


# =============================================================
# 规则 3 —— 商标符号 ®/™/℠/©(原 L2 R9,2026-04-25 提前到 L0)
# =============================================================

# 模式: 大写开头 + 长度 >= 3 的品牌名,紧邻 ®/™/℠/© (允许 0-2 空格)
# 逐字迁自 phase0_trademark.py:21-23,无 re.I/re.M/re.S;\s 含换行,
# 故一个 match 可以跨 title/bullets/desc 边界(hay 是 \n 拼的)。
_TRADEMARK_SYMBOL_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&\-\'\s]{2,50})\s{0,2}([®™℠©])",
)

# 句首通用词 (避免 "The Product ®" / "A New Item ®" 误触)
# 8 个原文照抄;副作用:"New Balance®" 也被过滤(已知误放,照迁)
_STOP_PREFIXES = {"the", "a", "an", "this", "our", "my", "your", "new"}

# 硬截断:bullets 只扫前 5 条、description 只扫前 1000 字符(超出部分的 ®
# 不会被发现)。旧仓是字面量,新仓提成常量,数值不变。
_TM_BULLET_LIMIT = 5
_TM_DESC_LIMIT = 1000

_TM_NOTE = (
    "title/bullets/desc 含 ®/™/℠/© 强 IP 信号, 搬运卖家无授权 (含 brand 字段同名也视作冒充)"
)


def _check_trademark(product: ProductInfo) -> Phase0Result:
    """输入:产品 → 输出:Phase0Result(title/bullets/desc 出现"品牌+®"即 blocked)。

    逐字迁自 phase0_trademark.py:29-74。**无自品牌豁免**:即使 product.brand 与
    命中短语同名也照拦(政策语义,不要好心加豁免分支)。
    有任意一个 match 即 block,不设数量阈值。
    另:旧仓 :35-36 的 `if not hay` 是死分支(hay 最小为 "\n\n" 恒真),不迁。
    hay 与 ProductInfo.searchable_text 不是同一份文本(截断规则不同),勿互换。
    """
    title = product.title or ""
    bullets_txt = " ".join((product.bullet_points or [])[:_TM_BULLET_LIMIT])
    long_desc = (product.long_description or "")[:_TM_DESC_LIMIT]
    hay = title + "\n" + bullets_txt + "\n" + long_desc

    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _TRADEMARK_SYMBOL_RE.finditer(hay):
        brand_phrase = (m.group(1) or "").strip()
        symbol = m.group(2)
        if len(brand_phrase) < 3:          # strip 后再判(如 "A  ®" → "A")
            continue
        first = brand_phrase.split()[0].lower() if brand_phrase else ""
        if first in _STOP_PREFIXES:
            continue
        key = brand_phrase.lower()         # 去重按小写全短语,保留先出现者的原大小写
        if key in seen:
            continue
        seen.add(key)
        matches.append({
            "brand_phrase": brand_phrase,
            "symbol": symbol,
            "context": hay[max(0, m.start() - 10): m.end() + 10].strip(),
        })

    if not matches:
        return Phase0Result(blocked=False)

    hit = RuleHit(
        stage="L0",
        rule_code="phase0_trademark_symbol",
        penalty=-100,
        detail={
            "matches": matches,
            "count": len(matches),
            "walmart_policy": "Intellectual Property",
            "matched_brand": matches[0]["brand_phrase"],
            # 品牌与符号之间不留空格,无论原文有没有
            "matched_phrase": f"{matches[0]['brand_phrase']}{matches[0]['symbol']}",
            "note": _TM_NOTE,
        },
    )
    return Phase0Result(blocked=True, matched_brand=matches[0]["brand_phrase"], hits=[hit])


# =============================================================
# 规则 4 —— 品牌精准黑名单(brand 字段等值,不扫 title)
# =============================================================

# 非品牌占位符 — Amazon / DMIT 常见的 "没标品牌" 字面量.
# 不把这些当品牌名做 Phase0 精准拦截 (否则 6/10 的 Amazon 产品无脑被挡).
# blacklist_brands 表里若含这些词 (如飞书错把 N/A 当品牌录入) 会被此白名单绕过.
# (20 项逐字迁自 phase0_brand.py:37-44)
_NON_BRAND_PLACEHOLDERS = {
    "n/a", "na", "n.a.", "n.a",
    "none", "null", "nil",
    "unbranded", "no brand", "no brand name", "no name",
    "generic", "oem", "various",
    "不详", "无品牌", "无",
    "-", "--", "---",
}

def _normalize_brand(raw: str) -> str:
    """输入:品牌原文 → 输出:规整键(小写 + 内部空白压单空格 + 两端去空白)。

    phase0_brand.py:67/80 的 `" ".join(raw.lower().split())`。
    **不去标点、不去 & 与连字符、不做 unicode 折叠**:"L'Oréal" → "l'oréal"。
    黑名单侧(ctx.brand_blacklist 的键)必须用同一算法规整,否则失配。
    """
    return " ".join(raw.lower().split())


def _check_brand(product: ProductInfo, ctx: Any) -> Phase0Result:
    """输入:产品 + 上下文 → 输出:Phase0Result(brand 规整后等值命中黑名单即 blocked)。

    逐字迁自 phase0_brand.py:145-177。**等值查表,不做子串/前缀/词边界扫描**
    ("IKEA Furniture" 放行);占位符白名单只作用于产品侧且在规整之后
    ("N/A" 放行,"N / A" 不在白名单会继续查表)。
    只扫 brand 字段,**不扫 title** —— 旧仓 title_fallback 已废弃(死代码不迁),
    title 里的品牌词是模糊信号,留给 L2 R4。
    黑名单含 PRS/Wen/Wilson 等通用词,误伤是现状不是 bug,双跑校准会看到。
    """
    brand_raw = (product.brand or "").strip()
    norm = _normalize_brand(brand_raw) if brand_raw else ""
    if not norm or norm in _NON_BRAND_PLACEHOLDERS:
        return Phase0Result(blocked=False)

    matched = ctx.brand_blacklist.get(norm)
    if not matched:
        return Phase0Result(blocked=False)

    hit = RuleHit(
        stage="L0",
        rule_code="phase0_brand_blacklist",
        penalty=-100,
        detail={
            "brand": product.brand,          # 原值(未 strip 未规整)
            "matched_brand": matched,        # 黑名单原文(first-wins)
            "match_type": "exact",
            "source": "blacklist_center",    # 单源:catalog.brand_blacklist
        },
    )
    return Phase0Result(blocked=True, matched_brand=matched, hits=[hit])


# =============================================================
# 主入口
# =============================================================


def check(product: ProductInfo, ctx: Any) -> Phase0Result:
    """输入:产品 + 上下文 → 输出:Phase0Result(四规则串行短路,最多 1 条 hit)。

    顺序照迁 phase0.py:30-49:飞书黑名单 → 类目禁售 → 商标符号 → 品牌黑名单。
    最后一条无 if 直接 return,所以全不命中时返回的是品牌规则那个
    Phase0Result(blocked=False)(空 hits、空 matched_*)。

    命中后上层处置(orchestrator.py:336-348,现由 audit_rules.audit_one 承接):
    verdict="reject"、score_final 硬写 0、stage_stopped_at="L0"、l1 用桩值。
    """
    # 0. 飞书黑名单 (seller_id / asin / amazon_cat) — 人工拉黑最高优先级
    r_lark = _check_lark_blacklist(product, ctx)
    if r_lark.blocked:
        return r_lark

    # 1. 类目禁售 (精准 Amazon 顶级)
    r_cat = _check_forbidden_category(product)
    if r_cat.blocked:
        return r_cat

    # 2. 商标符号硬拦 (title/bullets/desc 含 ®/™/℠/©)
    r_tm = _check_trademark(product)
    if r_tm.blocked:
        return r_tm

    # 3. 品牌黑名单 (精准 brand == blacklist, DB 全量 + yaml 手工补)
    r_brand = _check_brand(product, ctx)
    return r_brand


__all__ = ["check", "normalize_amazon_category", "FORBIDDEN_AMAZON_TOPS"]
