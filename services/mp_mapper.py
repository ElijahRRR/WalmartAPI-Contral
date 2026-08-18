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
# LLM 可能瞎填的文档引用字段:必须删除(填了 = 声称有证书 → 必拒)。
# suggested_number_of_people_for_assembly 是旧八项之一(2026-08-12 旧仓对照
# 补齐):留着会与 isAssemblyRequired=No 自相矛盾,触发文档依赖必拒
DANGEROUS_DOC_FIELDS = ("warrantyText", "warrantyURL", "prop65WarningText",
                        "nrtl_information", "assemblyInstructions",
                        "suggested_number_of_people_for_assembly")
_DOC_SUFFIX = "_document_reference_id"
# 强制值不在 enum 时的降级顺序(旧 mapper 四档,"None" 2026-08-12 旧仓对照
# 补回:PT enum 只有 None 而无前三者时旧选 None,漏这档会掉到 enum[0]——
# has_written_warranty 之类 enum[0] 常是 'Yes - Warranty Text',一填就触发
# warrantyText 条件必填)
_FORCE_FALLBACK = ("No", "Neither of these applies", "Skip for now", "None")


def clamp(text, limit: int, ellipsis: bool = False) -> str:
    """输入:文本 + 上限 → 输出:截断后文本(ellipsis=True 时 limit-3+'...')。"""
    s = str(text or "")
    if len(s) <= limit:
        return s
    return s[:limit - 3] + "..." if ellipsis else s[:limit]


def sort_images(urls: list) -> list[str]:
    """保序去重(2026-08-12 旧仓对照纠正):旧系统保持亚马逊原序,
    mainImageUrl=原序第一张=亚马逊主图。此前的字典序排序会把主图换成
    URL 最小的那张;来源真被 set() 打乱时保序也不比排序差。"""
    return list(dict.fromkeys(str(u) for u in urls or [] if u))


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


def force_amazon_copy(attrs: dict, product: dict,
                      min_features: int = 4) -> dict:
    """输入:Visible 属性 + 产品数据(+该 PT 的 keyFeatures 下限)→ 输出:
    文案强制用亚马逊原文的属性。

    移植自旧 auto_listing/mapper.force_amazon_copy(2026-08-09 补迁):
    **LLM 不重写文案,只做结构化字段映射**——文案与亚马逊保持一致,仅去品牌名
    与截长度。品牌名来自采集数据(Unbranded/Generic 之类噪声词不参与)。

    productName ← title;keyFeatures ← bullet_points;shortDescription ← 卖点拼接。
    ⚠ keyFeatures 部分 PT 的 minItems 已提到 4~6(EXT_DATA_ERROR_55506974520167):
    min_features 由调用方按该 PT spec 传入(旧 enforce_copy_limits 的 per-PT
    查询,2026-08-12 旧仓对照补回——写死 4 会让 minItems=5/6 的 PT 被本地
    validate 永久卡死,永远进不了 feed),不足时从描述/标题拆句补齐,
    **宁可凑短句也不能少于 minItems**。
    """
    min_features = max(4, min(int(min_features or 4), 7))
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
    if len(cleaned) < min_features:     # 拆句补齐(少于 minItems 会被拒)
        for text in cleaned + [long_text, title]:
            for p in _sentences(text, brands):
                if p not in cleaned:
                    cleaned.append(p[:500])
                if len(cleaned) >= min_features:
                    break
            if len(cleaned) >= min_features:
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
    props = (spec or {}).get("properties") or {}
    # 文案强制用亚马逊原文(去品牌名);无产品数据时保持旧行为。
    # keyFeatures 下限按该 PT spec 的 minItems(旧 enforce_copy_limits 语义)
    if product:
        kf_min = (props.get("keyFeatures") or {}).get("minItems") or 4
        attrs = force_amazon_copy(attrs, product, min_features=kf_min)
    # 零认证强制覆盖(字段在 spec 里才写,带 enum 降级)
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


def _field_block(name: str, meta: dict, required: bool) -> dict:
    """输入:字段 schema → 输出:给 LLM 的字段元数据块。

    2026-08-12 旧仓对照恢复(旧 _format_field_block/_summarize_prop 给 12 类
    元数据,此前只送 enum+desc):**type 不送,模型就只能猜数组还是标量**——
    四轮错误账里"要 JSONArray 却给标量"与"要 String 却给数组"就是没有
    类型信息的两种猜错方向。
    """
    f: dict = {"type": meta.get("type") or "string"}
    if required:
        f["required"] = True
    if meta.get("format"):
        f["format"] = meta["format"]    # date/date-time/uri:格式错=必拒
    if "enum" in meta:
        f["enum"] = meta["enum"][:30]
    if meta.get("description"):
        f["desc"] = str(meta["description"])[:200]
    if meta.get("minItems"):
        f["minItems"] = meta["minItems"]
    items = meta.get("items")
    if isinstance(items, dict):
        it: dict = {"type": items.get("type") or "string"}
        if "enum" in items:
            it["enum"] = items["enum"][:30]
        f["items"] = it
    sub = meta.get("properties")
    if isinstance(sub, dict):
        f["object_properties"] = {
            sn: {"type": (sd or {}).get("type") or "string",
                 **({"enum": sd["enum"][:15]} if isinstance(sd, dict)
                    and "enum" in sd else {})}
            for sn, sd in list(sub.items())[:20] if isinstance(sd, dict)}
        if meta.get("required"):
            f["object_required"] = meta["required"]
    return f


def _fields_for_llm(spec: dict | None, skip: tuple,
                    optional_cap: int) -> tuple[dict, dict]:
    """输入:spec + 剔除清单 + 可选字段上限 → 输出:(必填字段块, 可选字段块)。

    必填**全量**送(旧提示词同款,不设上限——此前 [:200] 硬截断会让排在
    后面的必填字段永不出现);可选按旧口径截断(Visible 20 / Orderable 10)。
    """
    props = (spec or {}).get("properties") or {}
    required = set((spec or {}).get("required") or [])
    req_out, opt_out = {}, {}
    for name, meta in props.items():
        if name in skip or not isinstance(meta, dict):
            continue
        if name in required:
            req_out[name] = _field_block(name, meta, True)
        elif len(opt_out) < optional_cap:
            opt_out[name] = _field_block(name, meta, False)
    return req_out, opt_out


def _conditional_blocks(spec: dict | None, cap: int = 12) -> list[dict]:
    """输入:spec → 输出:allOf if-then 条件必填的简写块(给 LLM 看真实值)。

    旧 _format_conditional_block 语义:让模型知道"填了 X=Yes 就必须给 Y",
    从源头给出**真实值**;mp_conform 的占位兜底只是最后防线,占位≠真实值
    (EXT_DATA_ERROR_72600149546850 的根治在这里)。
    """
    out = []
    for cond in (spec or {}).get("allOf") or []:
        if not isinstance(cond, dict) or "if" not in cond or "then" not in cond:
            continue
        then_req = (cond.get("then") or {}).get("required") or []
        if not then_req:
            continue
        if_c = cond["if"]
        cond_desc: dict = {}
        if if_c.get("required"):
            cond_desc["若已填"] = if_c["required"]
        for fn, fc in (if_c.get("properties") or {}).items():
            if isinstance(fc, dict) and fc.get("enum") is not None:
                cond_desc.setdefault("若取值", {})[fn] = fc["enum"][:10]
        out.append({"当": cond_desc or "见 spec", "则必填": then_req})
        if len(out) >= cap:
            break
    return out


# 进提示词前从 attrs 剔掉的媒体键(2026-08-18 所有者定稿,治缓存 hash 脆):
# 图片/视频是纯 URL,系统本就禁止 LLM 输出媒体字段(SYSTEM_OWNED_FIELDS,
# 图片由 apply_images 从采集数据覆盖),进提示词纯粹是噪声——却让"慢采只
# 刷新了图片列表"也打穿 llm_cache。
# ⚠ 改这份清单 = 改 messages = 现有缓存整体失效一次,只许在接受重烧时动。
PROMPT_DROP_KEYS = ("images", "image_url", "image_urls",
                    "video", "videos", "video_url")


def _prompt_attrs(attrs) -> dict:
    """输入:采集 slow 段 attrs → 输出:进 LLM 提示词的属性(剔媒体键)。"""
    if not isinstance(attrs, dict):
        return {}
    return {k: v for k, v in attrs.items() if k not in PROMPT_DROP_KEYS}


def reuse_sig(pt: str, spec: dict | None, product: dict,
              ospec: dict | None = None) -> str:
    """输入:PT + spec + 产品数据(+Orderable spec)→ 输出:二级复用硬条件签名。

    llm_cache 二级复用(2026-08-18 所有者定稿)的"不许复用"等值判断:
    签名里任何一样变了,旧出参直接作废重打 LLM——
      · spec 字段面 + 条件必填(spec 改版后旧出参可能给不出新必填);
      · brand / category(语义地基);
      · variant_attributes(变体属性 = 规格本体)。
    **title 与 attrs 文案故意不进签名**:文案变化正是二级复用要跨过去的
    那类变化;标题里可能藏规格,那一半风险由 title_spec_compatible 单验。
    """
    import hashlib as _hashlib
    import json as _json
    v_req, v_opt = _fields_for_llm(spec, SYSTEM_OWNED_FIELDS, 20)
    o_req, o_opt = _fields_for_llm(ospec, ORDERABLE_SYSTEM_FIELDS, 10)
    raw = _json.dumps(
        {"pt": pt, "vr": v_req, "vo": v_opt, "onr": o_req, "ono": o_opt,
         "cond": _conditional_blocks(spec),
         "brand": product.get("brand"),
         "category": product.get("category"),
         "variant_attributes": product.get("variant_attributes")},
        ensure_ascii=False, sort_keys=True, default=str)
    return _hashlib.sha256(raw.encode()).hexdigest()[:32]


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _title_hit(value: str, title: str) -> bool:
    """值是否按词边界出现在标题里(纯数字不许配进更长的数字:4 ≠ 48)。"""
    v = str(value).strip()
    if not v or len(v) > 40:
        return False
    if _NUM_RE.fullmatch(v):
        return re.search(rf"(?<![\d.]){re.escape(v)}(?![\d.])",
                         title) is not None
    return re.search(rf"(?<!\w){re.escape(v)}(?!\w)", title,
                     re.IGNORECASE) is not None


def _response_scalars(node) -> list[str]:
    """出参 JSON 里的全部标量值(str/数字;bool 不算——'No' 类枚举才有意义)。"""
    out: list[str] = []
    if isinstance(node, dict):
        for v in node.values():
            out.extend(_response_scalars(v))
    elif isinstance(node, (list, tuple)):
        for v in node:
            out.extend(_response_scalars(v))
    elif isinstance(node, (str, int, float)) and not isinstance(node, bool):
        out.append(str(node))
    return out


def title_spec_compatible(old_title: str, new_title: str,
                          response: dict) -> bool:
    """输入:出参时标题 + 现标题 + 旧出参 → 输出:旧出参对新标题是否仍成立。

    所有者的验证思路(2026-08-18:「llm取出来的参数在原来的标题里会有,
    如果新的标题里这部分参数变了…再走llm重新输出」)+ 两处修正:

    ① **对称验证,不是"值∈新标题"**:出参分两类——从标题抄/提取的
      (件数/尺寸/颜色词)与推断/枚举归一的("Kraft"→material=Paper、"No")。
      后者在旧标题里本来就找不到,拿去新标题验会全军覆没、复用率归零。
      所以只验**在旧标题命中过**的值:旧有、新没了 ⇒ 规格变了,重打。
    ② **数字 token 集合必须相等**(双向护栏):新标题**新增**的规格
      (4 Pack → 48 Pack)在旧出参里没有值可验,①看不见;而规格变化
      几乎总带数字(件数/尺寸/容量)。误伤方向是安全的——顶多多烧一次
      LLM,绝不把旧规格发给新形态的产品。
    """
    old_t, new_t = str(old_title or ""), str(new_title or "")
    if set(_NUM_RE.findall(old_t)) != set(_NUM_RE.findall(new_t)):
        return False
    for v in _response_scalars(response):
        if _title_hit(v, old_t) and not _title_hit(v, new_t):
            return False
    return True


def build_llm_messages(pt: str, spec: dict | None, product: dict,
                       ospec: dict | None = None) -> list[dict]:
    """输入:PT + 该 PT spec + 产品数据契约(+Orderable spec)→ 输出:messages。

    2026-08-12 旧仓对照重写(此前砍掉的三样全部恢复):
      ① 字段元数据含 type/required/minItems/items/object 结构(旧 12 类);
      ② 必填全量 + 可选截断(Visible 20 / Orderable 10),分四区——
        此前平铺 [:200] 会把排后面的必填字段永久截掉;
      ③ **Orderable 段交还 LLM**(除系统专属字段),输出两段
        {"visible": {…}, "orderable": {…}}——Orderable 的条件必填此前
        没人填。红线仍由 finalize_visible/mp_conform 兜底执行。
    """
    import json as _json
    v_req, v_opt = _fields_for_llm(spec, SYSTEM_OWNED_FIELDS, 20)
    o_req, o_opt = _fields_for_llm(ospec, ORDERABLE_SYSTEM_FIELDS, 10)
    sys = (
        "你是沃尔玛商品属性映射器。根据亚马逊产品资料,填写目标 Product Type "
        "的字段,只输出一个 JSON 对象,形如 {\"visible\": {字段→值}, "
        "\"orderable\": {字段→值}},不要 markdown 不要注释。\n"
        "1. 只用给定字段名,不要造字段;visible 字段放 visible 段,"
        "orderable 字段放 orderable 段,不要混。\n"
        "2. **不要输出系统后处理字段**:" + "/".join(SYSTEM_OWNED_FIELDS) +
        "——文案、图片、品牌、价格、库存、UPC 由系统填,你只做结构化字段。\n"
        "3. enum 字段必须**原样**取给定枚举值之一;语义最近的也行,宁可取第一个"
        "也绝不输出枚举外的值。\n"
        "4. 每个字段都标了 type:**type=array 必须给数组,type=string/number "
        "必须给标量**,object 按 object_properties 的子字段给对象。\n"
        "5. required=true 的字段尽量都给出真实值;conditional 清单里"
        "\"当…则必填\"的字段,一旦你填了触发条件就必须一起给。\n"
        "6. 数组字段宁缺勿空:没有真实数据就**不输出该字段**,不要写 []、\"\"、"
        "null、\"No\"、\"Not Available\" 之类占位;minItems 是该数组的最少条数。\n"
        "7. 不要输出任何认证/保修/文档类字段。")
    user = _json.dumps({
        "product_type": pt,
        "visible_required": v_req,
        "visible_optional": v_opt,
        "orderable_required": o_req,
        "orderable_optional": o_opt,
        "conditional": _conditional_blocks(spec),
        "product": {"title": product.get("title"),
                    "brand": product.get("brand"),
                    "category": product.get("category"),
                    # 剔媒体键(PROMPT_DROP_KEYS):图片列表进提示词是噪声,
                    # 还让"慢采只刷新了图片"也打穿缓存 hash
                    "attrs": _prompt_attrs(product.get("attrs"))},
    }, ensure_ascii=False)
    return [{"role": "system", "content": sys},
            {"role": "user", "content": user}]


def split_llm_output(raw: dict) -> tuple[dict, dict]:
    """输入:LLM 原始 JSON → 输出:(visible 段, orderable 段)。

    新提示词产出 {"visible": …, "orderable": …};旧缓存/旧提示词是平铺
    Visible 字段对象——兼容两种形态(缓存键含 messages,新提示词自然产生
    新缓存条目,平铺形态只出现在残留旧缓存)。
    """
    if not isinstance(raw, dict):
        return {}, {}
    if isinstance(raw.get("visible"), dict) or isinstance(
            raw.get("orderable"), dict):
        return (raw.get("visible") or {}, raw.get("orderable") or {})
    return dict(raw), {}


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


# Orderable 段的**系统专属字段**(旧 mapper 的 10 项 force_overrides +
# ShippingWeight/specProductType):LLM 不该填、填了也一律被系统值覆盖。
# 这些字段也不进 LLM 提示词(旧 _orderable_fields_for_llm 同款剔除)。
ORDERABLE_SYSTEM_FIELDS = (
    "sku", "productIdentifiers", "price", "inventory", "startDate", "endDate",
    "MustShipAlone", "fulfillmentLagTime",
    "country_of_origin_substantial_transformation", "specProductType",
    "ShippingWeight", "brand", "productName",
)


def build_orderable(sku: str, upc: str, price, qty: int, partner_id: str,
                    pt: str = "", product: dict | None = None,
                    llm_fields: dict | None = None) -> dict:
    """输入:sku/upc/沃尔玛价/库存/Partner ID/PT/产品数据(+LLM 填的 Orderable
    字段)→ 输出:Orderable 段。

    结构 = 旧 auto_listing 的"LLM 按 spec 填 + force_overrides 强制覆盖"
    (2026-08-12 旧仓对照恢复:此前写死 12 键,Orderable 里的其它条件必填
    永远给不出,本地 validate 卡死或被沃尔玛拒):llm_fields 打底(剔除
    系统专属字段),强制项覆盖在上。取值实证:
      · productIdentifiers 单对象、price 裸 number、fulfillmentCenterID=Partner ID
      · **inventory[].quantity 是裸 int**——写成 {unit,amount} 会被拒
        (EXT_DATA_ERROR_50716566635066 "'Inventory Quantity' … Enter a 'Number'")
      · **country_of_origin_substantial_transformation 必填**
        (EXT_DATA_ERROR_72600149546850,此前整个字段没给)
      · specProductType / startDate 旧系统都写,此前漏
      · endDate 必须 ISO DateTime(纯 yyyy-mm-dd 会被拒
        EXT_DATA_ERROR_00030257670757)
      · ShippingWeight 是 Orderable 必填:旧系统由 LLM 补,新系统从采集重量取
      · **不发 brand / countryOfOriginAssembly**(2026-08-12 旧仓对照删除:
        旧金样从未发过这两个字段,Orderable 多发字段与 productName 同一血统
        EXT_DATA_ERROR_60670554076755)
    """
    end_date = SITE_END_DATE if "T" in SITE_END_DATE else f"{SITE_END_DATE}T00:00:00Z"
    o = {k: v for k, v in (llm_fields or {}).items()
         if k not in ORDERABLE_SYSTEM_FIELDS and v not in (None, "", [], {})}
    o.update({
        "sku": str(sku),
        "productIdentifiers": {"productId": str(upc), "productIdType": "UPC"},
        "price": round(float(price), 2),
        "ShippingWeight": shipping_weight(product),
        "MustShipAlone": DEFAULT_MUST_SHIP_ALONE,
        "fulfillmentLagTime": DEFAULT_FULFILLMENT_LAG_DAYS,
        "startDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDate": end_date,
        "country_of_origin_substantial_transformation": DEFAULT_COUNTRY_OF_ORIGIN,
        "inventory": [{"fulfillmentCenterID": str(partner_id),
                       "quantity": int(qty)}],
    })
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
