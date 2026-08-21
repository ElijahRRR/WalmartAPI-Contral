"""Phase 0 精准前置拦截:黑名单(卖家/ASIN/类目)/ 商标符号 / 专利声明 / 品牌。

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
"""

from __future__ import annotations

import re
from typing import Any

from services import category_blacklist
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

    # 3. Amazon 类目 —— 判据全部在库里(catalog.amazon_cat_blacklist),
    #    三种匹配一次判完:子树 ID > 顶级名 > 完整路径等值。
    #    2026-08-20 之前这里只有"完整路径等值"一种,父级不覆盖子级,
    #    名单被迫逐层枚举还照样漏(详见 services/category_blacklist 头注)。
    hit = category_blacklist.check(
        getattr(ctx, "cat_rules", None),
        product.amazon_category_path,
        browse_node_chain=getattr(product, "browse_node_chain", "") or "",
        browse_node_id=product.browse_node_id or "",
        norm_path=normalize_amazon_category(product.amazon_category_path))
    if hit:
        code = ("phase0_forbidden_category"
                if hit.matched_by == category_blacklist.MATCH_TOP
                else "phase0_lark_blacklist_amazon_cat")
        detail = {
            "amazon_category_path": product.amazon_category_path,
            "matched_by": hit.matched_by,
            "matched_value": hit.matched_value,
            "category_zh": hit.category_zh,
            "reason": hit.reason,
            "source": "category_blacklist(DB)",
        }
        if hit.matched_by == category_blacklist.MATCH_NODE:
            detail["browse_node_id"] = hit.matched_value
            detail["subtree"] = hit.category_path
        if hit.matched_by == category_blacklist.MATCH_TOP:
            detail["amazon_top_category"] = hit.matched_value
            detail["full_path"] = product.amazon_category_path
        if hit.walmart_policy:
            detail["walmart_policy"] = hit.walmart_policy   # 理由映射第 1 优先级
        return Phase0Result(
            blocked=True,
            matched_category=hit.matched_value,
            hits=[RuleHit(stage="L0", rule_code=code, penalty=-100, detail=detail)],
        )

    return Phase0Result(blocked=False)


# =============================================================
# 规则 2 —— (已并入规则 1)Amazon 类目禁售
# =============================================================
#
# 2026-08-20 所有者定稿「代码里面的类目可以拿到数据库里来」:原
# FORBIDDEN_AMAZON_TOPS 四个硬编码顶级 + 独立的 _check_forbidden_category
# 已整体迁入 `catalog.amazon_cat_blacklist`(match_type='top_name'),
# 判定并进上面规则 1 的一次 check()。名单只有一个出处:飞书「黑名单亚马逊
# 类目」表 → risk_sync 整表镜像 → 库;代码里一条类目都不留
#(曾经的 SEED_RULES 已于 2026-08-20 删除,理由见 category_blacklist 头注)。
#
# ⚠ 顶级类目的粒度是"筐"不是"品",往表里加一个顶级 = 把整个筐里的杂货
# 一起拒掉且停在 L0。加之前先问:这个筐里**每一件**都该拒吗?
# (2026-08-17 裁决 A 的教训:Health & Household 被整拦,一包牛皮纸礼品袋
#  因为"药品/膳食补充剂 restricted"被拒。)

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
# 规则 3.5 —— 专利声明硬拦(2026-08-19 所有者定稿,新增,非旧仓迁移)
# =============================================================
#
# 来历:Yociyoga 收纳架标题明写 "(Patent Protection)(Patent No. 30022416)",
# 却被类目子串误伤成"服饰禁售"——所有者:「这个写明了专利被保护,拒绝理由
# 应该是专利」。listing 自述有专利 = 与 ®/™ 同级的强 IP 信号,搬运卖家无授权,
# 与规则 3 同一política(Intellectual Property),同一扫描面(title/前 5 条
# bullets/前 1000 字符 desc)。
#
# 唯一豁免:**patent leather(漆皮)**——鞋包箱表的常见材质词,与专利无关。
# 命中词形:Patent No./Number、Patented、Patent Pending、Patent Protection、
# US/Design Patent、patents 等;"patent" 后紧跟 leather(容一个空格/连字符)
# 不算。
_PATENT_RE = re.compile(r"\bpatent(?:ed|s)?\b(?![\s\-]*leather\b)", re.I)


def _check_patent(product: ProductInfo) -> Phase0Result:
    """输入:产品 → 输出:Phase0Result(文案自述专利即 blocked,漆皮豁免)。"""
    title = product.title or ""
    bullets_txt = " ".join((product.bullet_points or [])[:_TM_BULLET_LIMIT])
    long_desc = (product.long_description or "")[:_TM_DESC_LIMIT]
    hay = title + "\n" + bullets_txt + "\n" + long_desc

    m = _PATENT_RE.search(hay)
    if not m:
        return Phase0Result(blocked=False)

    hit = RuleHit(
        stage="L0",
        rule_code="phase0_patent_claim",
        penalty=-100,
        detail={
            "matched_phrase": m.group(0),
            "context": hay[max(0, m.start() - 30): m.end() + 30].strip(),
            "walmart_policy": "Intellectual Property",
            "note": "文案自述专利保护(Patent No./Patented/Patent Pending 等),"
                    "搬运卖家无授权;patent leather(漆皮)已豁免",
        },
    )
    return Phase0Result(blocked=True, hits=[hit])


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

    顺序:类目/卖家/ASIN 黑名单(规则 1,类目三种匹配已并入)→ 商标符号 →
    专利声明 → 品牌黑名单。
    最后一条无 if 直接 return,所以全不命中时返回的是品牌规则那个
    Phase0Result(blocked=False)(空 hits、空 matched_*)。

    命中后上层处置(orchestrator.py:336-348,现由 audit_rules.audit_one 承接):
    verdict="reject"、score_final 硬写 0、stage_stopped_at="L0"、l1 用桩值。
    """
    # 0. 黑名单闸 (seller_id / asin / 类目三合一) — 人工拉黑最高优先级
    r_lark = _check_lark_blacklist(product, ctx)
    if r_lark.blocked:
        return r_lark

    # 2. 商标符号硬拦 (title/bullets/desc 含 ®/™/℠/©)
    r_tm = _check_trademark(product)
    if r_tm.blocked:
        return r_tm

    # 2.5 专利声明硬拦 (2026-08-19 新增,同一扫描面,漆皮豁免)
    r_pat = _check_patent(product)
    if r_pat.blocked:
        return r_pat

    # 3. 品牌黑名单 (精准 brand == blacklist, DB 全量 + yaml 手工补)
    r_brand = _check_brand(product, ctx)
    return r_brand


__all__ = ["check", "normalize_amazon_category"]
