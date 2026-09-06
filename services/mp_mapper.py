"""MP_ITEM v5 载荷构造积木(listing L2d;实证约束逐条移植,均有旧错误码背书)。

分工:LLM 只负责"amz 属性 → PT 字段"的语义映射(api/llm + llm_cache);
本模块负责**硬约束执行**——LLM 产出必须经 finalize_visible 清洗后才能进
feed,任何字段级红线都在这里落地,不依赖提示词自觉。

实证约束清单(改动前先查 legacy_survey auto_listing 章):
  - orderable 三陷阱:productIdentifiers 必须**单对象非数组**;price 必须
    **裸 number** 非 {amount,currency};inventory[].fulfillmentCenterID 必填
    且必须是**该店的上架仓**(services/store_limits.listing_fc:配置了
    「维护仓库」的店 = 那个 shipNode,没配的店 = Partner ID/Virtual Node)
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
    """输入:图片 URL list → 输出:保序去重后的 URL list(第一张即主图)。

    保序去重(2026-08-12 旧仓对照纠正):旧系统保持亚马逊原序,
    mainImageUrl=原序第一张=亚马逊主图。此前的字典序排序会把主图换成
    URL 最小的那张;来源真被 set() 打乱时保序也不比排序差。
    """
    return list(dict.fromkeys(str(u) for u in urls or [] if u))


def secondary_min(spec: dict | None) -> int:
    """输入:PT spec → 输出:副图下限(该 PT 的 minItems;没写按 1)。

    **唯一出处**:apply_images 与素材闸(material_gap)问同一个数,
    否则又是"一处写死一处读表"。没有 minItems 时按 1:字段有值就比没有强。
    """
    return int((((spec or {}).get("properties") or {})
                .get("productSecondaryImageURL") or {}).get("minItems") or 1)


def apply_images(attrs: dict, urls: list, spec: dict | None = None) -> dict:
    """输入:Visible 属性 + 图片 URL(+该 PT 的 spec)→ 输出:写好图片字段的属性。

    ⚠ 副图下限**按该 PT 的 minItems 取,不写死**(2026-08-22 生产实证):
    在用的 62 个 PT 里副图 minItems 是 1/2/3/4,**一个 5 都没有**;写死 5
    的那版把 34/112 条**素材完全够**的行删成"必填缺失"——字段被 pop 掉,
    而它在那些 PT 里是必填,于是本地 validate 必拒。与 keyFeatures 当年
    写死 4 是同一个坑(见 force_amazon_copy 头注),这次一并按 spec 取。
    """
    imgs = sort_images(urls)
    if not imgs:
        return attrs
    attrs["mainImageUrl"] = imgs[0]
    secondary = imgs[1:9]
    if len(secondary) >= secondary_min(spec):
        attrs["productSecondaryImageURL"] = secondary
    else:
        # 不足该 PT 的 minItems:整个字段不写(必填的话由 validate 拦下,
        # 选填的话少一个字段照样能上)
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


# HTML 标签(采集侧的 description 有一部分是详情页原样 HTML)。
# ⚠ 不剥的话标签会**原样进 keyFeatures / shortDescription 发到线上**:
# 2026-08-22 实证有 `<p>…</p><ul><li>…` 整段被当成一条卖点(那次因为只凑出
# 1 条被拦下,卖点本来就有 2 条的行就直接发出去了)。
_HTML_TAG = re.compile(r"<[^<>]{0,200}>")


def _clean_copy(value, brands: list[str]) -> str:
    """输入:原始文案 → 输出:去品牌 + 去标签 + 去项目符号 + 折叠空白的单行文本。"""
    if value is None:
        return ""
    text = scrub_brand(str(value), brands).strip()
    text = _HTML_TAG.sub(" ", text)
    text = re.sub(r"[•·▪▫]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _sentences(text: str, brands: list[str], want: int = 4) -> list[str]:
    """输入:长文本(+想要几条)→ 输出:可当卖点用的句子(≥25 字符)。

    `want` 只影响**切不动时**的按长度切块:块长按 `len/want` 自适应并夹在
    60~200 字符之间。写死 120 的那版对 200 字左右的描述只切得出 2 条,
    而 keyFeatures 的门槛是 3~4 条 —— 差一条和差十条一样上不了架。
    """
    text = _clean_copy(text, brands)
    if not text:
        return []
    # 英文标点要求后随空白(防 "12.5 in" 被切开);中日文标点自带停顿,不要求
    raw = [p.strip() for p in re.split(r"(?:\n+|[.;!?]\s+|[。;!?、])", text)
           if len(p.strip()) >= 10]
    parts = [p for p in raw if len(p) >= 25]     # 够长的句子优先
    if len(parts) >= max(2, int(want or 4)):
        return parts
    if len(raw) >= max(2, int(want or 4)):
        # 长句不够条数,把 10~25 字符的短句按原序掺回来:
        # "Rust proof." 这种是**真句子**,当卖点比按长度硬切的半截强
        return raw
    if len(parts) >= 2:
        return parts
    # ⚠ 只切出**一段** = 这段文本没有句读(规格表折行 / HTML / 中文连写),
    # 必须往下走按长度切块。旧版这里写的是 `if parts: return parts` ——
    # 整段原样返回、切块兜底成了死代码,于是再长的描述也只补得出一条,
    # keyFeatures 的 3 条门槛永远过不去(2026-08-22 实证 11/112 行栽在这)。
    words = text.split()
    want = max(2, int(want or 4))
    size = min(200, max(40, len(text) // want))   # 自适应块长(下限 40 字符)
    # 闸改成"够不够切出两块"(旧版是词数 ≥24):一段 139 字符的详情页文案
    # 只有 23 个词,旧闸不放行 → 整段原样成一条 → 3 条的门槛差一条过不去。
    # 无空格文本(中文连写)split 后只有一个词,下面自然只出一块,不硬切字符。
    if len(text) >= size * 2:
        cap = max(4, want)
        chunks, cur = [], []
        for w in words:
            cur.append(w)
            if len(" ".join(cur)) >= size:
                chunks.append(" ".join(cur))
                cur = []
                if len(chunks) >= cap:
                    break
        tail = " ".join(cur)
        if tail:
            # 尾巴够长才单独成条;太短就并回上一块 —— 8 个字符的碎片当卖点
            # 发上线是内容事故,而 Walmart 不会因为"短"报错
            if len(tail) >= 25 and len(chunks) < cap:
                chunks.append(tail)
            elif chunks:
                chunks[-1] = f"{chunks[-1]} {tail}"
            else:
                chunks.append(tail)
        if chunks:
            return chunks
    return parts or ([text] if len(text) >= 10 else [])


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
            for p in _sentences(text, brands, want=min_features):
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


def material_gap(spec: dict | None, product: dict) -> str | None:
    """输入:PT spec + 产品数据 → 输出:素材凑不够必填数组的原因(够则 None)。

    只查两项:`keyFeatures` 与 `productSecondaryImageURL` —— 它们**全由系统
    从采集数据生成**(SYSTEM_OWNED_FIELDS 会把 LLM 写的这两项一律丢掉),
    所以在取数这一步就能定论,用的还是 `mp_conform.validate` 的同一组数字,
    结论不可能与它相左。

    为什么要提前判(2026-08-22 实证):这两样不够的行一路走到预备期才被
    validate 拦下,代价是**白打一次 LLM、白占一个当天配额名额**(配额切片
    在预备期之前),而素材是产品的固定属性 —— 这批行天天重来、天天白烧。
    结论完全一样(都是不上架),只是早说、说清楚、不花钱。
    """
    props, req = (spec or {}).get("properties") or {}, \
        set((spec or {}).get("required") or [])
    if "keyFeatures" in req:
        need = int((props.get("keyFeatures") or {}).get("minItems") or 4)
        got = len(force_amazon_copy({}, product, min_features=need)
                  .get("keyFeatures") or [])
        if got < need:
            return (f"卖点凑不够:{got} 条 < 该类目需 {need} 条"
                    f"(亚马逊卖点与描述都补不出来)")
    if "productSecondaryImageURL" in req:
        need = secondary_min(spec)
        got = max(0, len(sort_images(product.get("images"))) - 1)
        if got < need:
            return f"副图不够:{got} 张 < 该类目需 {need} 张(主图另算)"
    return None


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
    return apply_images(attrs, images or [], spec)


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


# ══════════════════════════════════════════════════════════════════════════════
#  发货重量(ShippingWeight,单位**磅**)
#
#  ⚠ 2026-09-06 生产事故:第二级投放的两个品把 `300.0` 与 `860.0` 当"磅"发进了
#  MP_ITEM_MATCH 的 REPLACE 载荷。原因是此前的实现**只抓字符串里第一个数字、
#  完全不看单位** —— 采集侧给的是 "300 grams" / "860 grams" 这类带单位的串。
#  沃尔玛后台那一栏是 **Shipping Weight (lbs)**,MP_ITEM / MP_ITEM_MATCH 载荷的
#  `ShippingWeight` 单位是磅(所有者观测),所以"不看单位"= 直接把克当磅发。
#
#  所有者定稿(2026-09-06 原话):「请勿猜测单位,一切以官方事实为主……如果解析
#  不出重量或者重量大于 11 磅,则把重量都写为 1 磅。」于是本模块的口径是:
#  **单位从数据里读,不猜** —— 只认显式单位记号,没有单位记号的裸数字算"解析
#  不出"(不假设它是磅),解析不出 / ≤0 / 折算后 > 11 磅 一律写 1 磅。
# ══════════════════════════════════════════════════════════════════════════════

#: 采不到 / 解析不出 / 超上限时写的那个值(单位磅,旧 test_pipeline 同值)。
DEFAULT_SHIPPING_WEIGHT = 1.0
#: 重量上限(磅)。**所有者 2026-09-06 定稿**:「如果解析不出重量或者重量大于
#: 11 磅,则把重量都写为 1 磅。」超过它不是"发大重量",是**判为不可信** ——
#: 我们搬运的品几乎不可能有 11 磅以上,而一次单位读错就是这个量级的错。
MAX_SHIPPING_WEIGHT_LBS = 11.0
#: 官方换算常量(**每个只在这里出生一次**,别在别处再写一遍数字):
#:   · 1 lb = 16 oz —— 常衡(avoirdupois)定义;
#:   · 1 lb = 453.59237 g —— 1959 年国际码磅协定(International Yard and Pound
#:     Agreement)给的**精确定义值**,不是近似;
#:   · 1 kg = 1000 / 453.59237 = 2.20462 lb(由上一条推出,不另写一个数)。
OUNCES_PER_POUND = 16.0
GRAMS_PER_POUND = 453.59237
POUNDS_PER_KILOGRAM = 1000.0 / GRAMS_PER_POUND

#: 认得的单位记号 → 折成磅的乘数。**只认这张表**:表外的记号(kgs / lb. 之外的
#: 缩写 / 中文"克"/ 空)一律判 unknown_unit 走兜底,不猜。比对前统一小写、去
#: 首尾空白与标点("Lbs." → "lbs")。
_UNIT_TO_LBS: dict[str, float] = {
    "pound": 1.0, "pounds": 1.0, "lb": 1.0, "lbs": 1.0,
    "ounce": 1.0 / OUNCES_PER_POUND, "ounces": 1.0 / OUNCES_PER_POUND,
    "oz": 1.0 / OUNCES_PER_POUND,
    "gram": 1.0 / GRAMS_PER_POUND, "grams": 1.0 / GRAMS_PER_POUND,
    "g": 1.0 / GRAMS_PER_POUND,
    "kilogram": POUNDS_PER_KILOGRAM, "kilograms": POUNDS_PER_KILOGRAM,
    "kg": POUNDS_PER_KILOGRAM,
}

#: "3.5 pounds" / "12.8 ounces" / "860grams" —— 数字 + 紧随其后的单位记号。
#: 记号段有意收得宽(**任何非数字非空白的一串**):"5 克" 要落进 unknown_unit
#: 而不是 no_unit —— 两档都写 1 磅,但摘要里"单位不认识"与"根本没有单位"是
#: 两件事,采集契约要靠这个区分去核实。
_NUM_UNIT = re.compile(r"(-?\d+(?:\.\d+)?)\s*([^\s\d]+)?")

#: dict 形态 {value|measure|amount, unit|units|unitOfMeasure} 的取值键序。
_WEIGHT_VALUE_KEYS = ("value", "measure", "amount")
_WEIGHT_UNIT_KEYS = ("unit", "units", "unitOfMeasure")

#: 兜底归因(`shipping_weight_ex` 的第二个返回值)。调用方按它分桶报数 ——
#: 光看一个 1.0 分不出"真 1 磅"与"兜底 1 磅",而这两件的处置完全不同。
WEIGHT_REASONS = ("parsed", "no_weight", "no_unit", "unknown_unit",
                  "over_cap", "nonpositive")


def _parse_weight_value(v) -> tuple[float | None, str]:
    """输入:一个重量值(数字 / 带单位串 / {value,unit})→ 输出:(磅, 归因)。

    解析出来给 `(磅, "parsed")`,解析不出给 `(None, 原因)`。
    **裸数字(没有单位记号)= 解析不出**,不假设它是磅:2026-09-06 的事故
    就是"抓到数字就当磅发"。单位只从数据里读(dict 的 unit 键,或串里数字
    后面紧跟的那个字母记号),表外记号判 unknown_unit。
    """
    unit = ""
    if isinstance(v, dict):
        raw = next((v[k] for k in _WEIGHT_VALUE_KEYS
                    if v.get(k) not in (None, "")), None)
        if raw is None:
            return None, "no_weight"
        unit = next((str(v[k]) for k in _WEIGHT_UNIT_KEYS if v.get(k)), "")
        v = raw
    if isinstance(v, bool):                      # True/False 不是重量
        return None, "no_weight"
    if isinstance(v, (int, float)):
        num = float(v)
    elif isinstance(v, str):
        m = _NUM_UNIT.search(v)
        if not m:
            return None, "no_weight"             # "N/A" / 空串:根本没有数字
        num = float(m.group(1))
        unit = unit or (m.group(2) or "")
    else:
        return None, "no_weight"
    key = unit.strip().strip(".,;:()[]").lower()
    if not key:
        return None, "no_unit"
    mult = _UNIT_TO_LBS.get(key)
    if mult is None:
        return None, "unknown_unit"
    return num * mult, "parsed"


def shipping_weight_ex(product: dict | None) -> tuple[float, str]:
    """输入:产品数据 → 输出:(发货重量**磅**, 归因)。归因见 WEIGHT_REASONS。

    采集契约:slow.weight = {package, item}(包装重与本体重,不合并)——
    发货重量取**包装重优先、本体重次之**(既有顺序)。形态不定(数字 /
    {value,unit} / 带单位串),单位一律**从数据里读**:
      · parsed      —— 读到显式单位、折算后是 (0, 11] 磅的正数,按它发;
      · no_weight   —— 两个键都没值 / 值里根本没有数字("N/A");
      · no_unit     —— 有数字但**没有单位记号**(裸数字不假设是磅);
      · unknown_unit—— 有单位记号但不在 `_UNIT_TO_LBS` 里(不猜);
      · over_cap    —— 折算后 > MAX_SHIPPING_WEIGHT_LBS(所有者定稿的不可信线);
      · nonpositive —— 折算后 ≤ 0。
    后五档一律返回 DEFAULT_SHIPPING_WEIGHT(= 1.0 磅,所有者定稿)。

    ⚠ 兜底值与"真的 1.0 磅"在数值上分不开,所以**要分清就读第二个返回值**;
    `shipping_weight()` 是本函数的薄封装(一条实现路径,不另写解析)。
    """
    weight = ((product or {}).get("attrs") or {}).get("weight")
    first = ""
    for key in ("package", "item"):
        v = weight.get(key) if isinstance(weight, dict) else None
        if v in (None, ""):
            continue
        lbs, why = _parse_weight_value(v)
        if lbs is None:
            first = first or why                 # 包装重读不出,再看本体重
            continue
        lbs = round(lbs, 2)
        if lbs <= 0:
            first = first or "nonpositive"
            continue
        if lbs > MAX_SHIPPING_WEIGHT_LBS:
            # 超上限**就地判定**,不再退到本体重:退过去等于拿另一个数替它猜。
            logger.debug("发货重量 %.2f 磅超上限 %s,按 %s 磅发(%r)",
                         lbs, MAX_SHIPPING_WEIGHT_LBS,
                         DEFAULT_SHIPPING_WEIGHT, weight)
            return DEFAULT_SHIPPING_WEIGHT, "over_cap"
        return lbs, "parsed"
    reason = first or "no_weight"
    # 逐行只落 debug(上架链一轮几百行,info 会把摘要淹了);**计数在调用方的
    # 摘要里按归因分桶**(workflows/list_new 的重量桶、sku_migrate 的兜底行数)。
    logger.debug("发货重量落兜底 %s 磅(%s):weight=%r",
                 DEFAULT_SHIPPING_WEIGHT, reason, weight)
    return DEFAULT_SHIPPING_WEIGHT, reason


def shipping_weight(product: dict | None) -> float:
    """输入:产品数据 → 输出:发货重量(磅)。`shipping_weight_ex` 的薄封装。

    只要重量,不关心是"真值"还是"兜底"时用它;要分清就用
    `shipping_weight_ex`(解析逻辑只有那一份,这里不许再写第二份)。
    """
    return shipping_weight_ex(product)[0]


# Orderable 段的**系统专属字段**(旧 mapper 的 10 项 force_overrides +
# ShippingWeight/specProductType):LLM 不该填、填了也一律被系统值覆盖。
# `specProductType` 官方 20260608 已移除,我们也不再写 —— 但**保留在这张表里**:
# 它得继续挡住 LLM 往 Orderable 里塞这个字段(spec 外字段会让整条被拒,
# EXT_DATA_ERROR_60670554076755),也继续不进 LLM 提示词。
# 这些字段也不进 LLM 提示词(旧 _orderable_fields_for_llm 同款剔除)。
# `SkuUpdate` 是沃尔玛的**系统专属开关字段**(不是内容字段):**本仓没有任何
# 工作流写它**(2026-09-06:改码通道定案 MP_ITEM_MATCH,靠「同 GTIN + 新 SKU +
# REPLACE」原地换码,载荷里根本没有这个字段;形态 A/B 两条 SkuUpdate 路线连同
# `build_sku_update_item` 与 `build_orderable(sku_update=)` 一起删了)。
# 它**留在这张表里**是为了继续挡住 LLM:LLM 填了它,后果不是报错,而是
# **沃尔玛把一次普通上架当成改码请求**,这是本仓能想到的最贵的静默失效。
ORDERABLE_SYSTEM_FIELDS = (
    "sku", "productIdentifiers", "price", "inventory", "startDate", "endDate",
    "MustShipAlone", "fulfillmentLagTime",
    "country_of_origin_substantial_transformation", "specProductType",
    "ShippingWeight", "brand", "productName", "SkuUpdate",
)


def build_orderable(sku: str, upc: str, price, qty: int, partner_id: str,
                    pt: str = "", product: dict | None = None,
                    llm_fields: dict | None = None) -> dict:
    """输入:sku/upc/沃尔玛价/库存/**上架仓 FC ID**/PT/产品数据(+LLM 填的
    Orderable 字段)→ 输出:Orderable 段。

    ⚠ 第 5 个参数是"这批货上到哪个仓",不是"Partner ID"(多仓批次 3):
    配置了「维护仓库」的店传那个仓的 shipNode,没配的店传 Partner ID
    (= Virtual Node,官方口径)。取值的唯一入口是
    `services.store_limits.listing_fc()` —— 别在调用点各自 `get_partner_id`,
    那样多仓的店会静默上到旧节点。形参名保留是为了不动三个调用点的关键字。

    结构 = 旧 auto_listing 的"LLM 按 spec 填 + force_overrides 强制覆盖"
    (2026-08-12 旧仓对照恢复:此前写死 12 键,Orderable 里的其它条件必填
    永远给不出,本地 validate 卡死或被沃尔玛拒):llm_fields 打底(剔除
    系统专属字段),强制项覆盖在上。取值实证:
      · productIdentifiers 单对象、price 裸 number、fulfillmentCenterID=上架仓
      · **inventory[].quantity 是裸 int**——写成 {unit,amount} 会被拒
        (EXT_DATA_ERROR_50716566635066 "'Inventory Quantity' … Enter a 'Number'")
      · **country_of_origin_substantial_transformation 必填**
        (EXT_DATA_ERROR_72600149546850,此前整个字段没给)
      · startDate 旧系统都写,此前漏
      · **specProductType 不再写**(2026-08-20 换 spec 到 20260608:官方把这个
        可选字段移除了)。留着也会被 mp_conform.strip_unknown 按新 spec 剔掉,
        但"写了再剔"白费一道工序,而且读代码的人会以为它还有用
      · endDate 必须 ISO DateTime(纯 yyyy-mm-dd 会被拒
        EXT_DATA_ERROR_00030257670757)
      · ShippingWeight 是 Orderable 必填:旧系统由 LLM 补,新系统从采集重量取
      · **不发 brand / countryOfOriginAssembly**(2026-08-12 旧仓对照删除:
        旧金样从未发过这两个字段,Orderable 多发字段与 productName 同一血统
        EXT_DATA_ERROR_60670554076755)

    ⚠ 本函数**永远不写 `SkuUpdate`**(2026-09-06 删除了 `sku_update=` 形参):
    改码通道定案为 MP_ITEM_MATCH,靠「同 GTIN + 新 SKU + REPLACE」原地换码
    (workflows/sku_migrate + services/match_feed.build_match_item),载荷里没有
    这个字段。留着那个形参就是第二条改码路径(§六 双轨禁止),而且它现在还是
    **会静默失效**的那一条 —— `mp_conform.strip_unknown` 的 SkuUpdate 放行分支
    已随之删除,spec 里没有它 ⇒ 传了会被裁掉 ⇒ 一次改码退化成一次普通上架
    (同店双挂),回执还全绿。`SkuUpdate` 仍留在 ORDERABLE_SYSTEM_FIELDS 里,
    职责只剩一个:挡住 LLM 往 Orderable 里塞它。
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
