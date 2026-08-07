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
                     images: list | None = None) -> dict:
    """输入:PT + LLM 映射产出 + 该 PT spec + 图片 → 输出:清洗后的 Visible 段。

    LLM 产出不可信,红线全在这里执行(见模块 docstring 清单)。
    """
    attrs = dict(llm_attrs or {})

    # 文档引用字段清理(先删再强制,防 LLM 瞎填)
    for k in list(attrs):
        if k in DANGEROUS_DOC_FIELDS or k.endswith(_DOC_SUFFIX):
            del attrs[k]
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
    sys = ("你是沃尔玛商品属性映射器。根据亚马逊产品资料,填写目标 Product Type"
           " 的字段,输出一个 JSON 对象(字段名→值)。规则:只用给定字段名;"
           "enum 字段必须取枚举值之一;不确定的字段不要输出;"
           "productName 用英文、10~199 字符;不要输出任何认证/保修/文档类字段。")
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


def build_orderable(sku: str, upc: str, price, qty: int,
                    partner_id: str) -> dict:
    """输入:sku/upc/沃尔玛价/库存/Partner ID → 输出:Orderable 段。

    三陷阱全部照实证:productIdentifiers 单对象;price 裸 number;
    fulfillmentCenterID=Partner ID。endDate 必须 ISO DateTime。
    """
    return {
        "sku": str(sku),
        "productIdentifiers": {"productId": str(upc), "productIdType": "UPC"},
        "productName": None,        # 由调用方以 Visible.productName 同值回填
        "brand": FORCE_BRAND,
        "price": round(float(price), 2),
        "ShippingWeight": None,     # 有实测重量才写,调用方决定
        "MustShipAlone": DEFAULT_MUST_SHIP_ALONE,
        "fulfillmentLagTime": DEFAULT_FULFILLMENT_LAG_DAYS,
        "endDate": SITE_END_DATE,
        "countryOfOriginAssembly": DEFAULT_COUNTRY_OF_ORIGIN,
        "inventory": [{"fulfillmentCenterID": str(partner_id),
                       "quantity": {"unit": "EACH", "amount": int(qty)}}],
    }


def assemble_mp_item(orderable: dict, pt: str, visible_attrs: dict) -> dict:
    """输入:Orderable + PT + 清洗后 Visible → 输出:一条完整 MPItem。

    Orderable 与 Visible 是并列顶级对象(不是 MPProduct,旧按文档猜错过);
    Visible 直接以 PT 名作命名空间(中间没有 productCategory 层)。
    productName 两处同值;None 值字段剔除。
    """
    o = {k: v for k, v in orderable.items() if v is not None}
    if visible_attrs.get("productName"):
        o["productName"] = visible_attrs["productName"]
    return {"Orderable": o, "Visible": {str(pt): visible_attrs}}
