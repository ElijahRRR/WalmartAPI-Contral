"""MP_ITEM v5 载荷构造积木(listing L2d;实证约束逐条移植,均有旧错误码背书)。

分工:LLM 只负责"amz 属性 → PT 字段"的语义映射(api/llm + llm_cache);
本模块负责**硬约束执行**——LLM 产出必须经 finalize_visible 清洗后才能进
feed,任何字段级红线都在这里落地,不依赖提示词自觉。

实证约束清单(改动前先查 legacy_survey auto_listing 章):
  - orderable 三陷阱:productIdentifiers 必须**单对象非数组**;price 必须
    **裸 number** 非 {amount,currency};inventory[].fulfillmentCenterID 必填
    且必须是 Partner ID(api/settings.get_partner_id)
  - endDate 必须 ISO DateTime(纯日期被拒 EXT_DATA_ERROR_00030257670757)
  - 零认证强制覆盖(搬运场景拿不到 CPC/NRTL/Prop65/Warranty 文档):
    强制值 + **同时删掉**文档引用字段,填了会被判"该证书不存在"必拒;
    强制值不在该 PT enum 时按 No→Neither of these applies→Skip for now→
    删字段→enum[0] 顺序降级(⚠ 旧档记"八项",本清单为 survey 已入档的
    5 项+文档字段清理,生产对拍期若遇缺项按同模式补登)
  - 文案硬约束:productName ≤199(<10 由调用方淘汰);shortDescription
    截 3997+'…';keyFeatures ≤7 条 × 497+'…';manufacturer ≤60
    (EXT_DATA_ERROR_01076067496949)
  - 图片:防御性按 URL 字典序排(采集侧 set() 去重打乱顺序);
    mainImageUrl=urls[0];productSecondaryImageURL=urls[1:9] 且**不足 5 张
    整个字段不写**(schema minItems=5)
"""

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger("services.mp_mapper")

SITE_END_DATE = "2028-12-31T00:00:00Z"      # 旧值;必须含时间
FORCE_BRAND = "Unbranded"                    # 搬运场景统一(旧 FORCE_BRAND)
DEFAULT_FULFILLMENT_LAG_DAYS = 1
DEFAULT_MUST_SHIP_ALONE = "No"
DEFAULT_COUNTRY_OF_ORIGIN = "China"

# 零认证强制覆盖(survey 已入档 5 项;"八项"其余待生产对拍补登)
NO_CERT_FORCES = {
    "certification_type": "Neither of these applies",
    "has_nrtl_listing_certification": "No",
    "isProp65WarningRequired": "No",
    "has_written_warranty": "No",
    "isAssemblyRequired": "No",
}
# LLM 可能瞎填的文档引用字段:必须删除(填了 = 声称有证书 → 必拒)
DANGEROUS_DOC_FIELDS = ("warrantyText", "warrantyURL", "prop65WarningText",
                        "nrtl_information", "assemblyInstructions")
_DOC_SUFFIX = "_document_reference_id"
# 强制值不在 enum 时的降级顺序(None=删字段)
_FORCE_FALLBACK = ("No", "Neither of these applies", "Skip for now")


def clamp(text, limit: int, ellipsis: bool = False) -> str:
    """输入:文本 + 上限 → 输出:截断后文本(ellipsis=True 时 limit-3+'...')。"""
    s = str(text or "")
    if len(s) <= limit:
        return s
    return s[:limit - 3] + "..." if ellipsis else s[:limit]


def sort_images(urls: list) -> list[str]:
    """防御性排序:采集侧 set() 去重导致顺序随机 → 字典序保 idempotent。"""
    return sorted({str(u) for u in urls or [] if u})


def apply_images(attrs: dict, urls: list) -> dict:
    """输入:Visible 属性 + 图片 URL → 输出:写好图片字段的属性。"""
    imgs = sort_images(urls)
    if not imgs:
        return attrs
    attrs["mainImageUrl"] = imgs[0]
    secondary = imgs[1:9]
    if len(secondary) >= 5:      # schema minItems=5:不足整个字段不写
        attrs["productSecondaryImageURL"] = secondary
    else:
        attrs.pop("productSecondaryImageURL", None)
    return attrs


# LLM **不该输出**的系统后处理字段(旧提示词规则 1a):文案由 Amazon 原文强制,
# 图片/品牌/UPC/价格/库存由系统填。不靠提示词自觉——这里主动删。
# swatchImages/swatchImageUrl 尤其:LLM 瞎填会撞
# EXT_DATA_ERROR_50716566635066(要 JSONObject)。
SYSTEM_OWNED_FIELDS = (
    "brand", "productName", "shortDescription", "keyFeatures",
    "mainImageUrl", "productSecondaryImageURL", "swatchImageUrl",
    "swatchImages",
)

_BRAND_NOISE = ("unbranded", "n/a", "unknown", "generic", "")


def scrub_brand(text: str, brands: list[str]) -> str:
    """输入:文本 + 要去掉的品牌名 → 输出:去品牌后的文本(全词匹配,空格整洁)。"""
    if not text or not brands:
        return text
    out = str(text)
    for b in brands:
        if not b or str(b).strip().lower() in _BRAND_NOISE:
            continue
        out = re.sub(rf"\b{re.escape(str(b))}\b", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,;-")
    return re.sub(r"\s+([,.;:])", r"\1", out)


def _clean_copy(value, brands: list[str]) -> str:
    """输入:原始文案 → 输出:去品牌 + 去项目符号 + 折叠空白后的单行文本。"""
    if value is None:
        return ""
    text = scrub_brand(str(value), brands).strip()
    text = re.sub(r"[•·▪▫]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _sentences(text: str, brands: list[str]) -> list[str]:
    """输入:长文本 → 输出:可当卖点用的句子(≥25 字符;句号切不动就按长度切)。"""
    text = _clean_copy(text, brands)
    if not text:
        return []
    # 英文标点要求后随空白(防 "12.5 in" 被切开);中日文标点自带停顿,不要求
    parts = [p.strip() for p in re.split(r"(?:\n+|[.;!?]\s+|[。;!?、])", text)
             if len(p.strip()) >= 25]
    if parts:
        return parts
    words = text.split()
    if len(words) >= 24:
        chunks, cur = [], []
        for w in words:
            cur.append(w)
            if len(" ".join(cur)) >= 120:
                chunks.append(" ".join(cur))
                cur = []
                if len(chunks) >= 4:
                    break
        if cur and len(chunks) < 4:
            chunks.append(" ".join(cur))
        if chunks:
            return chunks
    return [text] if len(text) >= 10 else []


def force_amazon_copy(attrs: dict, product: dict) -> dict:
    """输入:Visible 属性 + 产品数据 → 输出:文案强制用亚马逊原文的属性。

    移植自旧 auto_listing/mapper.force_amazon_copy(2026-08-09 补迁):
    **LLM 不重写文案,只做结构化字段映射**——文案与亚马逊保持一致,仅去品牌名
    与截长度。品牌名来自采集数据(Unbranded/Generic 之类噪声词不参与)。

    productName ← title;keyFeatures ← bullet_points;shortDescription ← 卖点拼接。
    ⚠ keyFeatures 部分 PT 的 minItems 已从 3 提到 4~6
    (EXT_DATA_ERROR_55506974520167):不足时从描述/标题拆句补齐,
    **宁可凑短句也不能少于 minItems**。
    """
    a = (product or {}).get("attrs") or {}
    brands = [b for b in (product.get("brand"), a.get("brand"),
                          a.get("manufacturer")) if b]
    attrs = dict(attrs)

    title = _clean_copy(product.get("title") or a.get("title"), brands)
    long_text = (a.get("description") or a.get("long_description")
                 or a.get("product_description") or "")
    if title:
        attrs["productName"] = title[:199]
    else:
        for s in _sentences(long_text, brands):
            attrs["productName"] = s[:199]
            break

    bullets = a.get("bullet_points") or []
    if isinstance(bullets, str):
        bullets = [b.strip() for b in bullets.split("\n") if b.strip()]
    cleaned = []
    for b in bullets if isinstance(bullets, list) else []:
        c = _clean_copy(b, brands) if isinstance(b, str) else ""
        if c:
            cleaned.append(c[:500])
    if len(cleaned) < 4:        # 拆句补齐(卖点不够是常态,少于 minItems 会被拒)
        for text in cleaned + [long_text, title]:
            for p in _sentences(text, brands):
                if p not in cleaned:
                    cleaned.append(p[:500])
                if len(cleaned) >= 4:
                    break
            if len(cleaned) >= 4:
                break
    if cleaned:
        attrs["keyFeatures"] = cleaned[:7]      # maxItems=7

    paragraph = " ".join(c for c in cleaned) if cleaned else \
        _clean_copy(long_text, brands)
    if len(paragraph.split()) < 60 and title:
        paragraph = f"{title}. {paragraph}".strip(". ") if paragraph else title
    if paragraph:
        attrs["shortDescription"] = paragraph[:4000]
    return attrs


def _enum_of(spec: dict | None, field: str) -> list | None:
    props = (spec or {}).get("properties") or {}
    f = props.get(field)
    return f.get("enum") if isinstance(f, dict) and "enum" in f else None


def _force_value(enum: list | None, wanted: str):
    """强制值不在 enum 时按降级顺序取;全落空取 enum[0];返回 None=删字段。"""
    if enum is None or wanted in enum:
        return wanted
    for cand in _FORCE_FALLBACK:
        if cand in enum:
            return cand
    return enum[0] if enum else None


def finalize_visible(pt: str, llm_attrs: dict, spec: dict | None,
                     images: list | None = None,
                     product: dict | None = None) -> dict:
    """输入:PT + LLM 映射产出 + spec + 图片 + 产品数据 → 输出:清洗后 Visible。

    LLM 产出不可信,红线全在这里执行(见模块 docstring 清单);
    文案与图片是**系统的地盘**——LLM 写了也一律覆盖/删除。
    """
    attrs = dict(llm_attrs or {})

    # 文档引用字段清理(先删再强制,防 LLM 瞎填)
    for k in list(attrs):
        if k in DANGEROUS_DOC_FIELDS or k.endswith(_DOC_SUFFIX):
            del attrs[k]
    # 系统后处理字段:LLM 输出一律丢弃(swatchImages 这类 LLM 给标量会被拒)
    for k in SYSTEM_OWNED_FIELDS:
        attrs.pop(k, None)
    # 文案强制用亚马逊原文(去品牌名);无产品数据时保持旧行为
    if product:
        attrs = force_amazon_copy(attrs, product)
    # 零认证强制覆盖(字段在 spec 里才写,带 enum 降级)
    props = (spec or {}).get("properties") or {}
    for field, wanted in NO_CERT_FORCES.items():
        if spec is not None and field not in props:
            attrs.pop(field, None)
            continue
        v = _force_value(_enum_of(spec, field), wanted)
        if v is None:
            attrs.pop(field, None)
        else:
            attrs[field] = v
    # 文案硬约束
    if "productName" in attrs:
        attrs["productName"] = clamp(attrs["productName"], 199)
    if "shortDescription" in attrs:
        attrs["shortDescription"] = clamp(attrs["shortDescription"], 4000,
                                          ellipsis=True)
    if isinstance(attrs.get("keyFeatures"), list):
        attrs["keyFeatures"] = [clamp(x, 500, ellipsis=True)
                                for x in attrs["keyFeatures"][:7]]
    if "manufacturer" in attrs:
        attrs["manufacturer"] = clamp(attrs["manufacturer"], 60)
    attrs["brand"] = FORCE_BRAND
    return apply_images(attrs, images or [])


def build_llm_messages(pt: str, spec: dict | None, product: dict) -> list[dict]:
    """输入:PT + 该 PT spec + 产品数据契约 → 输出:LLM 映射的 messages。

    只送字段面(名称/enum/描述截断),产品侧送标题+attrs;产出要求纯 JSON
    (Visible 字段对象)。红线不靠提示词,finalize_visible 兜底执行。
    """
    import json as _json
    props = (spec or {}).get("properties") or {}
    fields = {}
    for name, meta in list(props.items())[:200]:
        if not isinstance(meta, dict):
            continue
        f: dict = {}
        if "enum" in meta:
            f["enum"] = meta["enum"][:30]
        if meta.get("description"):
            f["desc"] = str(meta["description"])[:120]
        fields[name] = f
    # 提示词规则逐条移植自旧系统(硬约束仍由 finalize_visible/mp_conform 兜底,
    # 提示词只是让 LLM 少犯错、少烧一轮修正)
    sys = (
        "你是沃尔玛商品属性映射器。根据亚马逊产品资料,填写目标 Product Type "
        "的字段,只输出一个 JSON 对象(字段名→值),不要 markdown 不要注释。\n"
        "1. 只用给定字段名,不要造字段。\n"
        "2. **不要输出系统后处理字段**:" + "/".join(SYSTEM_OWNED_FIELDS) +
        "——文案、图片、品牌由系统用亚马逊原文填,你只做结构化字段。\n"
        "3. enum 字段必须**原样**取给定枚举值之一;语义最近的也行,宁可取第一个"
        "也绝不输出枚举外的值。\n"
        "4. **spec 里 type=array 的字段必须给数组**,不能给裸字符串"
        "(pattern/color/material/shape 这类看着是单值的往往也是 array)。\n"
        "5. 数组字段宁缺勿空:没有真实数据就**不输出该字段**,不要写 []、\"\"、"
        "null、\"No\"、\"Not Available\" 之类占位。\n"
        "6. 不要输出任何认证/保修/文档类字段。")
    user = _json.dumps({
        "product_type": pt,
        "fields": fields,
        "product": {"title": product.get("title"),
                    "brand": product.get("brand"),
                    "category": product.get("category"),
                    "attrs": product.get("attrs") or {}},
    }, ensure_ascii=False)
    return [{"role": "system", "content": sys},
            {"role": "user", "content": user}]


DEFAULT_SHIPPING_WEIGHT = 1.0   # 采不到重量时的保守值(单位磅,旧 test_pipeline 同值)


def shipping_weight(product: dict | None) -> float:
    """输入:产品数据 → 输出:发货重量(磅);采不到按 DEFAULT_SHIPPING_WEIGHT。

    采集契约:slow.weight = {package, item}(包装重与本体重,不合并)——
    发货重量取包装重,退而取本体重。形态不定(数字 / {value,unit} / 带单位串),
    这里只负责取出一个正数。
    """
    weight = ((product or {}).get("attrs") or {}).get("weight")
    for key in ("package", "item"):
        v = weight.get(key) if isinstance(weight, dict) else None
        if isinstance(v, dict):
            v = v.get("value") or v.get("measure") or v.get("amount")
        if isinstance(v, (int, float)) and v > 0:
            return round(float(v), 2)
        if isinstance(v, str):
            m = re.search(r"\d+(?:\.\d+)?", v)
            if m and float(m.group()) > 0:
                return round(float(m.group()), 2)
    return DEFAULT_SHIPPING_WEIGHT


def build_orderable(sku: str, upc: str, price, qty: int, partner_id: str,
                    pt: str = "", product: dict | None = None) -> dict:
    """输入:sku/upc/沃尔玛价/库存/Partner ID/PT/产品数据 → 输出:Orderable 段。

    字段面与取值逐条对齐旧 auto_listing/mapper.force_overrides 的 Orderable 段
    (2026-08-09 首跑三条全拒后逐行比对补齐,四处差异都有实证代价):
      · productIdentifiers 单对象、price 裸 number、fulfillmentCenterID=Partner ID
      · **inventory[].quantity 是裸 int**——写成 {unit,amount} 会被拒
        (EXT_DATA_ERROR_50716566635066 "'Inventory Quantity' … Enter a 'Number'")
      · **country_of_origin_substantial_transformation 必填**
        (EXT_DATA_ERROR_72600149546850,此前整个字段没给)
      · specProductType / startDate 旧系统都写,此前漏
      · endDate 必须 ISO DateTime(纯 yyyy-mm-dd 会被拒
        EXT_DATA_ERROR_00030257670757)
      · ShippingWeight 是 Orderable 必填:旧系统由 LLM 补,新系统从采集重量取
    """
    end_date = SITE_END_DATE if "T" in SITE_END_DATE else f"{SITE_END_DATE}T00:00:00Z"
    o = {
        "sku": str(sku),
        "productIdentifiers": {"productId": str(upc), "productIdType": "UPC"},
        "brand": FORCE_BRAND,
        "price": round(float(price), 2),
        "ShippingWeight": shipping_weight(product),
        "MustShipAlone": DEFAULT_MUST_SHIP_ALONE,
        "fulfillmentLagTime": DEFAULT_FULFILLMENT_LAG_DAYS,
        "startDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDate": end_date,
        "country_of_origin_substantial_transformation": DEFAULT_COUNTRY_OF_ORIGIN,
        "countryOfOriginAssembly": DEFAULT_COUNTRY_OF_ORIGIN,
        "inventory": [{"fulfillmentCenterID": str(partner_id),
                       "quantity": int(qty)}],
    }
    if pt:
        o["specProductType"] = str(pt)
    return o


def assemble_mp_item(orderable: dict, pt: str, visible_attrs: dict) -> dict:
    """输入:Orderable + PT + 清洗后 Visible → 输出:一条完整 MPItem。

    Orderable 与 Visible 是并列顶级对象(不是 MPProduct,旧按文档猜错过);
    Visible 直接以 PT 名作命名空间(中间没有 productCategory 层)。None 值字段剔除。

    ⚠ **不往 Orderable 塞 productName**(2026-08-09 生产实证):
    EXT_DATA_ERROR_60670554076755 "'productName' is not a valid field.
    Do not add or change the field names in the specification."
    ——它只属于 Visible;Orderable 的字段面以 spec 为准(mp_conform.strip_unknown)。
    """
    o = {k: v for k, v in orderable.items() if v is not None}
    return {"Orderable": o, "Visible": {str(pt): visible_attrs}}
