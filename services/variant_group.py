"""变体分组积木(list_new 用;所有者定稿 2026-08-15 四条批复)。

数据来源全部在库内,**不需要采集侧再改**(2026-08-15 查证):
`catalog.snapshots.raw` 原样带着采集侧的三个字段——
    parent_asin          族主 ASIN
    variant_attributes   本 ASIN 自己的维度取值,形如 "color_name=Black; size_name=L"
    variation_asins      精确同族(twister 真实家族键,**不含自己**)
(导出契约只把 variant_attributes 压成 slow.variant.theme,而那段代码按 ':' 切、
实际格式是 '=',所以 theme 只解析出 0.1% 且全是垃圾——绕开它,直接读 raw。)

所有者四条定稿:
① **增量归组,不整组同批**:有几个上几个,先按单品上,以后同族的自动并进来。
② 分配侧保证「一组变体只分配一个店」⇒ 本模块**只查本店**,不做跨店重定向
   (旧系统的 anchor 跨店逻辑不迁)。
③ 同族已有成员在架 ⇒ 新成员沿用它的 variantGroupId。
④ **组大小上限 20**,超了**退回单品口径照常上架**(不是拒绝):几百上千成员的
   家族根本放不下。

两条本模块自己的推论(所有者可推翻):
- **组大小按亚马逊真实家族算**(`variation_asins` 长度 + 1),不按库里采到几个。
  库里只有 3 个而家族真有 500 个的,今天当变体上了、明天采到更多就爆表。
- **单品也带 groupId**(由 parent_asin 派生,稳定可复现)。于是以后的兄弟各自
  独立算也得到同一个 ID,**"合并"自动发生**,不必回头给已在架的补发维护 feed
  ——那要多一次写、多一份配额,还多一个"补发失败就永远合不上"的失败态。
  依据是沃尔玛报错原文:"If you only have 1 item in a variant group, select
  'Yes' in Is Primary Variant" —— 它明确支持 1 个成员的变体组。
"""

import logging
import re

logger = logging.getLogger("services.variant_group")

MAX_FAMILY = 20         # 所有者定稿:超过按单品上架
_GROUP_PREFIX = "vg_"

# 亚马逊维度名 → 沃尔玛属性名候选(按优先级)。沃尔玛各 PT 的
# variantAttributeNames 枚举不同,调用方拿本 PT 的枚举与这里求交集取第一个。
# 旧系统这一步用 LLM remap,砍掉:维度就这二十来种,手写映射既准又不花钱。
_DIM_MAP: dict[str, tuple[str, ...]] = {
    "color_name": ("color", "colorCategory", "actualColor"),
    "size_name": ("size", "clothingSize", "shoeSize", "sizeName"),
    "style_name": ("style", "styleName"),
    "pattern_name": ("pattern", "patternStyle"),
    "material_type": ("material", "fabricMaterial"),
    "flavor_name": ("flavor",),
    "scent_name": ("scent", "fragrance"),
    "item_shape": ("shape",),
    "capacity": ("capacity", "volume"),
    "model": ("modelNumber", "model"),
    "edition": ("edition",),
    "team_name": ("team",),
    "lens_color": ("lensColor", "color"),
    "hand_orientation": ("handOrientation",),
    "number_of_items": ("count", "numberOfPieces"),
    "item_package_quantity": ("count", "numberOfPieces"),
    "unit_count": ("count",),
    "set_name": ("style", "styleName"),
    "platform_for_display": ("platform",),
    "item_display_weight": ("weight",),
}

_ASIN_SPLIT = re.compile(r"[,\s|]+")


def parse_attrs(raw) -> dict[str, str]:
    """输入:"color_name=Black; size_name=L" → 输出:{维度名: 取值}。

    分隔符按采集侧实证同时认 ';' 与 '|';**只按第一个 '=' 切**——取值里带
    等号(如 "size_name=6x6=36")不该把维度名切碎。
    """
    out: dict[str, str] = {}
    for seg in re.split(r"[;|]", str(raw or "")):
        if "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


def parse_family(raw, self_asin: str = "") -> list[str]:
    """输入:"B0001,B0002" + 本 ASIN → 输出:同族 ASIN 列表(去重、含自己、有序)。

    采集侧给的列表**不含自己**(2026-08-15 生产实证:三个兄弟互指对方两个),
    这里补上自己——下游算组大小、判主变体都按"含自己"的口径。
    """
    seen: list[str] = []
    for a in _ASIN_SPLIT.split(str(raw or "")):
        a = a.strip().upper()
        if a and a not in seen:
            seen.append(a)
    me = str(self_asin or "").strip().upper()
    if me and me not in seen:
        seen.insert(0, me)
    return seen


def group_id(parent_asin, self_asin: str = "", family=()) -> str | None:
    """输入:parent_asin(+ 本 ASIN 兜底 + 完整家族)→ 输出:变体组 ID;都空则 None。

    由 parent_asin 派生 ⇒ 同族成员各自独立计算得到同一个 ID,不必互相查
    ——这是"增量归组自动发生"的全部机制。

    ⚠ **parent_asin 等于自己时改用 min(家族)**(2026-08-17 照旧仓补的护栏)。
    旧仓设计文档 `variant_groups_design.md` §3.3 记着这个坑:它的采集侧(DMIT)
    每行的 parent_asin **就是该行自己的 ASIN**,拿它当组 ID 会把同一组 N 个兄弟
    切成 N 个组,而且不报错 —— 所以旧仓一律用 `min(full_set)`。
    我们现在的采集侧(twister 版)给的是真族主(生产实见 GustBuster 组:
    parent=B000AMXQVI,成员是另外三个),所以主路径仍按 parent 派生、ID 更好认;
    但采集侧哪天改回"填自己",这里必须还能自愈。
    min(家族) 与 parent 派生一样是**跨兄弟稳定**的:同族每个成员算出的家族集合
    相同(自己 ∪ variation_asins),min 自然相同。
    """
    p = str(parent_asin or "").strip().upper()
    fam = [str(a).strip().upper() for a in (family or ()) if str(a).strip()]
    me = str(self_asin or "").strip().upper()
    if fam and len(fam) > 1 and (not p or p == me):
        p = min(fam)
    p = p or me
    return f"{_GROUP_PREFIX}{p}" if p else None


def pick_walmart_dims(amazon_dims, enum) -> list[tuple[str, str]]:
    """输入:亚马逊维度名列表 + 本 PT 的 variantAttributeNames 枚举
    → 输出:[(亚马逊维度名, 沃尔玛属性名)],**全部映得上的都要**,映不上的丢掉。

    ⚠ **多维不是可选项,是旧系统的既有能力**(2026-08-17 对着旧仓核实):
    `auto_listing/mapper.py:1374` 是 `common = sorted(allowed_names &
    set(var_attrs.keys()))` —— 取**交集全体**,`variantAttributeNames` 发一个
    列表。设计文档 `auto_listing/docs/variant_groups_design.md` §3.5/§4.2 同。
    我们首版只取第一个,是**迁漏**,不是简化。

    枚举为空(spec 没给枚举)时返回空:映不上就走单品口径,总比发一个 PT 不认的
    属性名被整条拒强。

    同一个沃尔玛属性名只出现一次(如 `number_of_items` 与 `item_package_quantity`
    都映向 `count`,重复列进 variantAttributeNames 会让载荷自相矛盾);
    顺序按沃尔玛属性名排序,**跨兄弟稳定** —— 同组成员算出的顺序必须一致,
    否则同一个组里几条的 variantAttributeNames 顺序不同,排查时看着像两组。
    """
    allowed = {str(e) for e in (enum or [])}
    if not allowed:
        return []
    pairs: dict[str, str] = {}          # 沃尔玛属性名 → 亚马逊维度名(先到先得)
    for dim in amazon_dims or ():
        for cand in _DIM_MAP.get(str(dim), ()):
            if cand in allowed and cand not in pairs:
                pairs[cand] = str(dim)
                break                   # 一个亚马逊维度只认领一个沃尔玛属性
    return [(amz, wm) for wm, amz in sorted(pairs.items())]


def pick_walmart_dim(amazon_dims, enum) -> str | None:
    """输入:同上 → 输出:第一个映得上的沃尔玛属性名(无则 None)。

    保留给只关心"能不能映上"的调用方;分变体请用 `pick_walmart_dims`。
    """
    pairs = pick_walmart_dims(amazon_dims, enum)
    return pairs[0][1] if pairs else None


def plan(asin: str, raw_attrs, raw_family, parent_asin, enum,
         existing_group_id: str = "", family_has_primary: bool = False) -> dict:
    """输入:一个待上架 ASIN 的变体原料 → 输出:决策 dict。

    返回:
      mode      'variant' 走变体口径 / 'single' 退回单品口径(不发变体字段)
      code      稳定短码,给摘要计数用:variant / no_attrs / oversize /
                no_dim / no_group_id。**别拿 reason 分词当键**——reason 是给人
                看的中文长句,改一个字计数就散了
      reason    走 single 的原因(mode='variant' 时为 '')
      group_id  变体组 ID
      attr_pairs [(沃尔玛属性名, 本 ASIN 的取值)] —— **可能多个**(color+size)。
                属性名进 variantAttributeNames,取值各写进同名属性,
                否则组内无差异
      unmapped_dims 亚马逊有、但本 PT 枚举映不上的维度名(只剔它们,不退单品)
      family_size 亚马逊真实家族大小(含自己)
      is_primary 是否本组主变体

    退回单品的四种情形,**每种都给出具体 reason**(静默降级 = 变体功能悄悄
    没生效而没人知道):没有维度取值 / 家族超上限 / PT 枚举映不上 / 无组 ID。
    """
    attrs = parse_attrs(raw_attrs)
    family = parse_family(raw_family, asin)
    gid = str(existing_group_id or "").strip() \
        or group_id(parent_asin, asin, family)
    out = {"mode": "single", "code": "", "reason": "", "group_id": gid,
           "attr_pairs": [], "unmapped_dims": [],
           "family_size": len(family), "is_primary": False}
    if not attrs:
        out.update(code="no_attrs", reason="无变体维度取值")
        return out
    if len(family) > MAX_FAMILY:
        # ④ 所有者定稿:几百上千成员的家族放不下,按单品照常上架(不是拒绝)
        out.update(code="oversize",
                   reason=f"家族 {len(family)} 个超上限 {MAX_FAMILY}")
        return out
    pairs = pick_walmart_dims(list(attrs), enum)
    if not pairs:
        out.update(code="no_dim", reason=f"PT 枚举映不上亚马逊维度 {sorted(attrs)}")
        return out
    if not gid:
        out.update(code="no_group_id", reason="无 parent_asin,凑不出稳定组 ID")
        return out
    # 交集全体(旧仓 mapper.py:1374 同款口径),每对 = (沃尔玛属性名, 取值)。
    # 值的类型/enum 合法性由载荷层按本 PT spec 逐个校验(mp_conform)——
    # 这里只负责"选哪些维度",不认识 spec 的字段定义
    attr_pairs = [(wm, attrs[amz]) for amz, wm in pairs]
    dropped = sorted(set(attrs) - {amz for amz, _ in pairs})
    out.update(mode="variant", code="variant",
               attr_pairs=attr_pairs, is_primary=not family_has_primary,
               unmapped_dims=dropped)
    if dropped:
        # 部分维度映不上**不退单品**(旧仓同款:只剔掉映不上的那几个),
        # 但要报出来 —— 组内差异如果恰好只在被剔掉的那个维度上,发出去
        # 就是几条看不出区别的变体
        logger.warning("%s 的变体维度 %s 映不上本 PT 枚举,只按 %s 分组",
                       asin, ",".join(dropped),
                       ",".join(wm for wm, _ in attr_pairs))
    return out
