"""Phase 0 前置拦截:黑名单(卖家/ASIN/类目)/ 商标符号 / 专利声明 /
Made in USA / 品牌,外加一条软证据(品牌黑名单文案扫描)。

移植自旧仓 pipelines/phase0.py + phase0_lark_blacklist.py + phase0_category.py
+ phase0_trademark.py + phase0_brand.py 五个文件(a565d95),合并为一个模块。

设计原则(旧仓 phase0.py:3-13 原文):Phase0 的**硬拒**只处理"100% 确定不能上"
的情况,不做文本级启发式 —— 判据要么是库里的等值查表,要么是字面声明的正则。

**五硬一软,双输出**(2026-09-03 C 批,规格 §4.1;旧形态是"四条硬规则串行短路"):

  硬拒(penalty -100,串行短路,任一命中即整条流水线终止、`blocked=True`):
    1. 黑名单三表(卖家 / ASIN / 亚马逊类目)  2. 商标符号 ®/™/℠/©
    3. 文案自述专利                          4. Made in USA 声明(原 L2 R10)
    5. 品牌字段精确等值黑名单
  软证据(penalty 0,**全部硬规则未命中才跑**,落 `Phase0Result.evidence`):
    · 品牌黑名单**文案**扫描(原 L2 R4)—— 命中不终止,随产品进 L3 由 LLM 判
      "提到 ≠ 卖的就是"(R6 误伤率 90% 的教训)。

所以一次 run 的 L0 行数是:硬拒 1 行,**或**软证据 n 行(两者互斥);
`stage_stopped_at='L0'` 的语义不变 —— **只有硬拒才停**。
黑名单能力从此只在 L0 一处实现、共用 `catalog.brand_blacklist` 一份数据
(所有者定稿 `docs/audit_pipeline.md` §10)。

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
import threading
from typing import Any

from registry import resources
from services import category_blacklist, policy_names
from services.audit_models import Phase0Result, ProductInfo, RuleHit

# 品牌文案扫描的自动机锁(product_audit workers>1 共享同一个 ctx;
# pyahocorasick 只读迭代的线程安全没有官方保证,锁住扫描段 —— µs 级,
# 相对秒级 LLM 调用零成本)。逐字迁自 audit_l2(R4 迁入时随迁)。
_AC_LOCK = threading.Lock()

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
                        # 规则自报类别(§二):内部黑名单是**我们自己的决策**,
                        # 不对应任何一条沃尔玛政策 —— 别再给它挂政策名
                        "category": resources.AUDIT_CAT_INTERNAL_BLACKLIST,
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
                        "category": resources.AUDIT_CAT_INTERNAL_BLACKLIST,
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
        # 规则自报类别(§二):黑名单行自带 `walmart_policy` 且**能对到政策表**
        # → 用那条政策;对不上(空 / 旧拼写 / 自造名)→ `内部黑名单`。
        # 两个键都留:`walmart_policy` 是黑名单行的**原值**(数据溯源),
        # `category` 是本条判定落的类别(解析后的表内拼写)——不是双轨。
        detail["category"] = resources.AUDIT_CAT_INTERNAL_BLACKLIST
        if hit.walmart_policy:
            detail["walmart_policy"] = hit.walmart_policy   # 黑名单行原值
            known = getattr(ctx, "known_policies", None) or ()
            resolved = policy_names.resolve(hit.walmart_policy, known)
            if resolved:
                detail["category"] = resolved
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
    hay 与品牌黑名单扫描那条(只扫标题)不是同一份文本,勿互换。
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
            "category": resources.AUDIT_IP_POLICY,   # 规则自报类别(§二)
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
            "category": resources.AUDIT_IP_POLICY,   # 规则自报类别(§二)
            "note": "文案自述专利保护(Patent No./Patented/Patent Pending 等),"
                    "搬运卖家无授权;patent leather(漆皮)已豁免",
        },
    )
    return Phase0Result(blocked=True, hits=[hit])


# =============================================================
# 规则 3.6 —— Made in USA 声明(原 L2 R10,2026-09-03 C 批迁入 L0)
# =============================================================
#
# 生产实证:沃尔玛按 "Prohibited Product Policy on Made in USA claims" 下架,
# 理由是 FTC 对 Made in USA 声明要求卖家能实证 —— 搬运模式下文案来自亚马逊,
# 我们**永远无法实证**,声明本身即违规,与商品真实产地无关。
# 硬拒(-100):判据是**字面声明**,不是推断 —— 与"专利自述"同类(文案自己
# 写下来的东西就是铁证),所以 2026-09-03 与它并排住进 L0;这与"儿童品不进
# L2"(误伤面大、只能语义判)方向相反且都是对的。
# 词边界防误伤:"usa" 必须独立成词("Jerusalem"/"thousand" 不命中);
# 只认肯定式声明,"not made in usa" 由否定前置词排除。
# 正则与否定式排除逐字迁自 audit_l2._R10_MADE_IN_USA / _R10_NEGATION。
_MADE_IN_USA_RE = re.compile(
    r"\b(?:made|manufactured|built|produced|crafted)\s+in\s+"
    r"(?:the\s+)?(?:usa|u\.s\.a\.?|u\.s\.|united states)(?:\s+of\s+america)?\b"
    r"|\b(?:usa|american)[- ]made\b",
    re.IGNORECASE)
_MADE_IN_USA_NEGATION = re.compile(r"\b(?:not|isn't|isnt)\s+(?:made|manufactured)\b",
                                   re.IGNORECASE)


def _check_made_in_usa(product: ProductInfo) -> Phase0Result:
    """输入:产品 → 输出:Phase0Result(文案声明 Made in USA 即 blocked)。

    扫描面 = 标题 + **全部**五点 + 长描述(声明可能只出现在长描述里)——
    与同层的商标/专利两条**有意不同**:那两条沿用旧仓的 5 条五点 / 1000 字符
    窗口(逐字迁移契约),这条是漏判反哺加的硬拒,漏了就是漏判,全文都扫。
    类别自报 `Product claims`(官方第 29 节里的 Made in the USA 专段);
    旧 R10 detail 里那个自造的 `Made in USA claims` 政策名已删 —— 政策表里
    没有那一行,写进去就是往全链唯一键上塞一个 join 不上的串。
    """
    parts = [product.title or ""]
    parts += list(product.bullet_points or [])
    parts.append(product.long_description or "")
    scan = "\n".join(x for x in parts if x)
    if not scan.strip():
        return Phase0Result(blocked=False)
    mt = _MADE_IN_USA_RE.search(scan)
    if not mt:
        return Phase0Result(blocked=False)
    # 否定式排除:命中点前 40 字符内出现 not made/manufactured 视为反声明
    ctx_before = scan[max(0, mt.start() - 40):mt.start()]
    if _MADE_IN_USA_NEGATION.search(ctx_before + mt.group(0)):
        return Phase0Result(blocked=False)

    hit = RuleHit(
        stage="L0",
        rule_code="phase0_made_in_usa",
        penalty=-100,
        detail={
            "matched": mt.group(0),
            "category": resources.AUDIT_PRODUCT_CLAIMS_POLICY,  # 规则自报类别(§二)
            "note": "FTC 要求卖家实证 Made in USA 声明;搬运文案无法实证,"
                    "声明本身即违规(生产实证下架原因)",
        },
    )
    return Phase0Result(blocked=True, hits=[hit])


# =============================================================
# 规则 4 —— 品牌精准黑名单(brand 字段等值,不扫 title)
# =============================================================

# 非品牌占位符 — Amazon / DMIT 常见的 "没标品牌" 字面量.
# 不把这些当品牌名做 Phase0 精准拦截 (否则 6/10 的 Amazon 产品无脑被挡).
# 这张表**优先于黑名单**: 表里的词就算登记进黑名单中心也照样放行 —— 立这条
# 白名单时的假设是"占位符出现在黑名单里 = 飞书录错了"(如错把 N/A 当品牌录入).
# (原 20 项逐字迁自 phase0_brand.py:37-44)
#
# ⚠ 2026-08-23 所有者定稿撤下三项: generic / oem / various.
# 起因: 飞书品牌总表里显式登记了 GENERIC, 但审核对 brand=Generic 的产品照发
# pass —— 因为 _check_brand 在查黑名单**之前**先过这张白名单就短路返回了.
# 所有者是**故意**登记的, 与上面那条"录错了"的假设正好相反, 而代码没有区分
# 这两种情况的手段, 所以按所有者口径把这三个词交还给黑名单裁决:
# 登记了就拦, 没登记照旧放行(黑名单里没有 = 查表落空 = 与在白名单里同效).
#
# ⚠ **不要顺手同步改 services/brand_key.PLACEHOLDERS** —— 那三个词在占用键
# 那边必须留着. 两张表方向相反: 这里多留一个词 = 少拦一个黑名单品牌(可控);
# 占用键那边少留一个词 = "Generic" 变成排他占用键, 把成千上万个互不相干的
# 产品锁进同一家店, 而占用**没有自动释放**(brand_key.py 模块头注 §二铁律).
_NON_BRAND_PLACEHOLDERS = {
    "n/a", "na", "n.a.", "n.a",
    "none", "null", "nil",
    "unbranded", "no brand", "no brand name", "no name",
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
    title 里的品牌词是模糊信号,留给**同层的软规则** `_scan_brand_mentions`
    (2026-09-03 C 批自 L2 R4 迁入本模块):同一份黑名单,等值即硬拒、
    文案提到只当 0 分证据送 L3。
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
            # 规则自报类别(§二):品牌拉黑多因知产风险,归 IP 是既定口径
            # (与内部黑名单三码有意不同,别顺手改)
            "category": resources.AUDIT_IP_POLICY,
        },
    )
    return Phase0Result(blocked=True, matched_brand=matched, hits=[hit])


# =============================================================
# 软规则 —— 品牌黑名单**文案**扫描(原 L2 R4,2026-09-03 C 批迁入 L0)
# =============================================================
#
# 与规则 4 是**同一份数据的两种判法**(所有者定稿:黑名单能力只在 L0 一处
# 实现、一份数据 `catalog.brand_blacklist`):
#   · brand 字段精确等值 → 硬拒(规则 4);
#   · 标题/五点/描述里扫到黑名单词 → **0 分证据不终止**,送 L3 由 LLM 判它是
#     真品牌还是通用英文词 / "兼容·适配"式提及(R6 硬拦误伤率 90% 的教训:
#     提到 ≠ 卖的就是)。
# 自动机在 ctx 装配期**只构建一处**(services/audit_rules._build_automaton)。

# 中日韩 + 全角:这些文字之间不写空格,"紧邻"就是词与词的分界
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uac00-\ud7ff\uf900-\ufaff"
                     r"\ufe30-\ufe4f\uff00-\uffef]")


def _is_word_boundary_char(c: str) -> bool:
    """输入:单个字符 → 输出:它是否算词边界。

    ⚠ 2026-08-20 修:`c.isalnum()` 对中文/全角返回 True,于是"耐克运动鞋"里
    黑名单词「耐克」左右都被判成非边界 —— **中文品牌一个都拦不住**,而且不报错。
    中日韩与全角字符不写分词空格,紧邻即边界,单独判掉;拉丁带音标字母
    (café 的 é)仍按词内字符处理,免得把 "Caf" 这种前缀切出来误命中。
    """
    if not c:
        return True
    if _CJK_RE.match(c):
        return True
    return not (c.isalnum() or c == '_')


#: 品牌命中带回的上下文半径(字符)。取 40:够装下 `Bob Smith Industries
#: BSI-151H` 这种"多词品牌 + 型号"的形态,又不至于把整条标题塞进证据行。
_BRAND_CTX = 40


def _scan_brand_mentions(product: ProductInfo, ctx: Any) -> list[RuleHit]:
    """输入:产品 + ctx(用 ctx.brand_mention_automaton) → 输出:软证据 hit(0 或 1 条)。

    **只扫 product.title**(2026-09-03 所有者定稿)。以前扫 title + 全部五点 +
    长描述,而词表 4.2 万条里混着 corner / life / wooden / better / side / time
    这类通用词 —— 扫描述等于把送进 L3 的品牌词清单**灌满噪声**,而那份清单
    `≤10 个`,真正长在品牌位上的那个词反而挤不进去(`B0GYNRCZ9F` 的 `abba` 就是
    这么丢的)。代价说清:只在描述里出现的品牌从此不进证据 —— 所有者认这笔账。
    自动机为 None → 返回 [](未装 pyahocorasick / 黑名单词表为空,那是"没有词可扫")。
    AC 不自带词边界,命中后手动检查前后字符;**自品牌豁免是精确等值**
    (brand strip+lower 后与命中词完全相等才跳过);同一个 brand 只报第一次。
    判定逻辑迁自 audit_l2._rule_title_desc_blacklist(rule_code 与层次 C 批变过,
    **扫描面 2026-09-03 收窄到标题**,词边界/自品牌豁免/同品牌只报一次都没变)。

    ⚠ **属性直取,不用 `getattr(..., None)` 兜底**(2026-09-03 复核修订):
    `AuditContext` 把这个字段声明成**无默认值**的必填项,所以生产上它一定在;
    而写成 getattr 兜底的话,哪天字段再改一次名,这条规则会**静默返回空**
    —— 表现是"品牌证据从此一条不出、TRO 命中从此不报警",而判定照样跑完、
    摘要照样漂亮,没有任何东西会红。少一个字段就该当场 AttributeError。
    """
    hay = product.title or ""
    if not hay:
        return []
    A = ctx.brand_mention_automaton
    if A is None:      # 词表为空 / 未装 pyahocorasick ⇒ 没有词可扫,不是接线坏了
        return []
    own_brand = (product.brand or "").strip().lower()
    hay_lower = hay.lower()
    n = len(hay_lower)

    matches: list[dict[str, Any]] = []
    seen: set[str] = set()

    # AC iter 返回 (end_index, value), end_index 是命中末字符的位置 (inclusive)。
    # workers>1 时共享一个自动机:pyahocorasick 只读迭代的线程安全没有官方
    # 保证,锁住扫描段(µs 级,相对秒级 LLM 调用零成本)
    with _AC_LOCK:
        ac_matches = list(A.iter(hay_lower))
    for end_idx, brand in ac_matches:
        if brand in seen or brand == own_brand:
            continue
        start_idx = end_idx - len(brand) + 1
        # 手动 word boundary 检查 (AC 不自带)
        left_ok = (start_idx == 0) or _is_word_boundary_char(hay_lower[start_idx - 1])
        right_ok = (end_idx == n - 1) or _is_word_boundary_char(hay_lower[end_idx + 1])
        if not (left_ok and right_ok):
            continue
        seen.add(brand)
        # 还原原始大小写显示给审核员
        # ⚠ **带回上下文,不只带回命中的那个词**(2026-09-03 所有者点破):
        # 黑名单收的是**单词**,而标题里的品牌往往是**多词的完整名字** ——
        # `smith` 命中,标题里其实是 `Bob Smith Industries`,那是另一家公司,
        # 只是碰巧含这个词。只把 `smith` 递给 L3,它没法判"这个词属于哪个
        # 完整品牌名、和黑名单里那个牌子是不是同一个",于是一律判成真品牌,
        # 再被确定性后处理翻成知产侵权(实测正例误伤的主因)。
        lo = max(0, start_idx - _BRAND_CTX)
        hi = min(n, end_idx + 1 + _BRAND_CTX)
        matches.append({"brand": brand,
                        "matched_phrase": hay[start_idx:end_idx + 1],
                        "context": ("…" if lo else "") + hay[lo:hi]
                                   + ("…" if hi < n else "")})

    if not matches:
        return []
    return [RuleHit(
        stage="L0",
        rule_code="phase0_brand_mention",
        penalty=0,
        detail={
            "matches": matches,
            "count": len(matches),
            "note": "L3 LLM 需判断每个词是真品牌还是通用词",
        },
    )]


# =============================================================
# 主入口
# =============================================================


def check(product: ProductInfo, ctx: Any) -> Phase0Result:
    """输入:产品 + 上下文 → 输出:Phase0Result(五硬规则短路,或软证据继续)。

    硬拒顺序(串行短路,命中即 blocked=True 返回,最多 1 条 hit):
    类目/卖家/ASIN 黑名单(规则 1,类目三种匹配已并入)→ 商标符号 →
    专利声明 → Made in USA → 品牌字段精确等值。

    **全部硬规则未命中**才跑软规则(品牌文案扫描),命中进 `evidence`、
    `blocked=False`,判定继续往 L1 走 —— 软证据**不终止流水线**
    (2026-09-03 C 批双输出,规格 §4.1)。

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

    # 2.6 Made in USA 声明硬拦 (2026-09-03 自 L2 R10 迁入,扫全文)
    r_usa = _check_made_in_usa(product)
    if r_usa.blocked:
        return r_usa

    # 3. 品牌黑名单 (精准 brand == blacklist, DB 全量 + yaml 手工补)
    r_brand = _check_brand(product, ctx)
    if r_brand.blocked:
        return r_brand

    # 4. 软证据:品牌黑名单文案扫描 (0 分,不终止;送 L3 判"提到还是卖的就是")
    return Phase0Result(blocked=False,
                        evidence=_scan_brand_mentions(product, ctx))


__all__ = ["check", "normalize_amazon_category"]
