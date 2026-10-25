"""MP_ITEM spec 一致化流水线回归(逐条对应 2026-08-09 首跑的真实拒收)。"""

from services import mp_conform as mc

_SPEC = {
    "required": ["productName", "color", "keyFeatures"],
    "properties": {
        "productName": {"type": "string"},
        "color": {"type": "string"},
        "keyFeatures": {"type": "array", "items": {"type": "string"},
                        "minItems": 3},
        "material": {"type": "array", "items": {"type": "string"}},
        "pattern": {"type": "array", "items": {"type": "string"}},
        "assembledProductWeight": {
            "type": "object",
            "properties": {"measure": {"type": "number"},
                           "unit": {"type": "string",
                                    "enum": ["lb", "oz"]}}},
        "condition": {"type": "string", "enum": ["New", "Used"]},
        "tags": {"type": "array", "items": {"type": "string",
                                            "enum": ["A", "B"]}},
        "productSecondaryImageURL": {
            "type": "array", "minItems": 5,
            "items": {"type": "string", "format": "uri"}},
        "powerType": {"type": "array", "items": {"type": "string"}},
        "displayTechnology": {"type": "string"},
        "count": {"type": "integer"},
    },
    "allOf": [
        {"if": {"properties": {"powerType": {"contains": {"enum": ["Battery"]}}}},
         "then": {"required": ["displayTechnology"]}},
    ],
}

_OSPEC = {"required": ["sku", "price"],
          "properties": {"sku": {}, "price": {}, "productIdentifiers": {},
                         "inventory": {}, "brand": {}}}


def test_scalar_wrapped_into_array():
    """EXT_DATA_ERROR_50716566635066:'Pattern' 要 JSONArray,LLM 给了字符串。"""
    v, notes = mc.fix_type_mismatches(_SPEC, {"pattern": "Solid",
                                              "material": ["Metal"]})
    assert v["pattern"] == ["Solid"]
    assert v["material"] == ["Metal"]            # 已经是数组不动
    assert any("包成 array" in n for n in notes)


def test_scalar_rebuilt_into_object():
    """同一错误码的 JSONObject 变体:标量喂给测量类字段。"""
    v, notes = mc.fix_type_mismatches(_SPEC, {"assembledProductWeight": 5.5})
    assert v["assembledProductWeight"] == {"measure": 5.5, "unit": "lb"}


def test_object_sub_enum_fixed():
    """IB.VALIDATION.DATA.001:object 子字段 unit 非法。"""
    v, notes = mc.fix_type_mismatches(
        _SPEC, {"assembledProductWeight": {"measure": 2, "unit": "ft"}})
    assert v["assembledProductWeight"]["unit"] == "lb"    # 换成 enum[0]


def test_placeholder_and_url_array_cleanup():
    """EXT_DATA_ERROR_49505365506868:URL 数组不许填 'No' 这类占位。"""
    v, _ = mc.fix_type_mismatches(_SPEC, {"productSecondaryImageURL": "No"})
    assert "productSecondaryImageURL" not in v
    v2, _ = mc.fix_type_mismatches(
        _SPEC, {"productSecondaryImageURL": ["https://a/1.jpg", "No"]})
    assert v2["productSecondaryImageURL"] == ["https://a/1.jpg"]
    # 安全默认对 URL 数组一律返回 None(绝不填占位)
    assert mc.safe_default_for(
        _SPEC["properties"]["productSecondaryImageURL"]) is None


def test_invalid_enum_required_vs_optional():
    v, notes = mc.fix_invalid_enums(_SPEC, {"condition": "Refurbished",
                                            "tags": ["A", "Z"]})
    assert v["condition"] == "New"        # 非必填但有安全默认 → 换 enum 内值
    assert v["tags"] == ["A"]             # 数组删非法元素


def test_conditional_required_fixpoint():
    """不动点迭代:powerType 兜底后才触发 displayTechnology 这条级联。"""
    v, notes = mc.fill_known_required(_SPEC, {})
    assert v["powerType"] == ["Battery"]              # 白名单兜底
    assert v["displayTechnology"] == "Not Available"  # 级联触发后补上
    assert len(notes) >= 2


def test_fill_missing_required_and_validate():
    v, _ = mc.fill_missing_required(_SPEC, {"productName": "T"})
    assert v["color"] == "Not Available"
    # 自由文本数组不拿占位灌够 minItems:validate 要如实报出来
    assert v["keyFeatures"] == ["Not Available"]
    assert mc.validate(_SPEC, _OSPEC, v, {"sku": "S1", "price": 9.9}) == [
        "visible.keyFeatures(需≥3条,现1条)"]
    v["keyFeatures"] = ["a", "b", "c"]
    assert mc.validate(_SPEC, _OSPEC, v, {"sku": "S1", "price": 9.9}) == []
    # 缺 orderable 必填 → 报出来
    assert mc.validate(_SPEC, _OSPEC, v, {"sku": "S1"}) == ["orderable.price"]


def test_strip_unknown_drops_orderable_product_name():
    """EXT_DATA_ERROR_60670554076755:productName 不是 Orderable 的合法字段。"""
    v, o, dropped = mc.strip_unknown(
        _SPEC, _OSPEC, {"productName": "T", "bogusField": 1},
        {"sku": "S1", "price": 1.0, "productName": "T"})
    assert "productName" not in o and "orderable.productName" in dropped
    assert "bogusField" not in v
    assert v["productName"] == "T"          # Visible 里它是合法的


def test_drop_empty_and_min_items_keep_required():
    v, o, dropped = mc.drop_empty_optional(
        _SPEC, _OSPEC, {"material": [], "color": ""}, {"brand": ""})
    assert "material" not in v              # 非必填空值删
    assert v["color"] == ""                 # 必填空值保留,交给 validate 报错
    assert "orderable.brand" in dropped
    v2, _o2, notes = mc.drop_min_items(
        _SPEC, _OSPEC, {"productSecondaryImageURL": ["a", "b"],
                        "keyFeatures": ["x"]}, {})
    assert "productSecondaryImageURL" not in v2      # 2 < minItems 5,非必填 → 删
    assert v2["keyFeatures"] == ["x"]                # 必填不删,留给 validate


def test_round_decimals():
    v, o, fixes = mc.round_decimals({"w": 1.23456}, {"price": 12.500001})
    assert v["w"] == 1.23 and o["price"] == 12.5
    assert len(fixes) == 2


def test_conform_pipeline_end_to_end():
    """整条流水线:LLM 原始输出 → 可提交载荷。"""
    llm_out = {"productName": "Bar Stool", "pattern": "Solid",
               # keyFeatures 由 force_amazon_copy 从亚马逊卖点填(minItems=3)
               "keyFeatures": ["轻便", "稳固", "易安装"],
               "assembledProductWeight": 5.5, "condition": "Refurbished",
               "bogus": "x"}
    orderable = {"sku": "S1", "price": 61.47, "productName": "Bar Stool"}
    v, o, notes, missing = mc.conform(_SPEC, _OSPEC, llm_out, orderable)
    assert missing == []                       # 必填全被兜底补齐
    assert v["pattern"] == ["Solid"]
    assert isinstance(v["assembledProductWeight"], dict)
    assert v["condition"] == "New"
    assert "bogus" not in v
    assert "productName" not in o              # Orderable 裁掉
    assert v["displayTechnology"]              # 条件必填级联补上
    assert notes


def test_conform_without_spec_reports_missing():
    v, o, notes, missing = mc.conform(None, None, {"a": 1}, {"sku": "S"})
    assert missing == ["spec 缺失,无法校验"] and v == {"a": 1}


def test_array_unwrapped_when_spec_wants_scalar():
    """反向类型错(2026-08-09 三轮实证):'Color' 要 String,LLM 给了数组。"""
    v, notes = mc.fix_type_mismatches(_SPEC, {"color": ["Silver", "Gray"]})
    assert v["color"] == "Silver"                 # 取首元素
    assert any("取首元素" in n for n in notes)
    # 空数组喂给标量字段 → 删(留给必填兜底重填)
    v2, _ = mc.fix_type_mismatches(_SPEC, {"color": []})
    assert "color" not in v2
    # 数字字段给了字符串数组 → 取首元素后仍转数字
    v3, _ = mc.fix_type_mismatches(_SPEC, {"count": ["12"]})
    assert v3["count"] == 12


_VSPEC = {
    "required": ["productName"],
    "properties": {
        "productName": {"type": "string"},
        "color": {"type": "string"},
        "variantGroupId": {"type": "string"},
        "variantAttributeNames": {"type": "array",
                                  "items": {"enum": ["color", "size"]}},
        "isPrimaryVariant": {"type": "string", "enum": ["Yes", "No"]},
    },
}


_VSPEC_REQ = {**_VSPEC, "required": ["productName", "variantAttributeNames"]}


def test_variant_bag_completed_when_required():
    """spec 逼着给(第 4 轮实证 PT)才补全:单品 isPrimary=Yes、SKU 占位组 ID。"""
    v, notes = mc.ensure_variant_bag(
        _VSPEC_REQ, {"variantAttributeNames": ["color"], "color": "Red"}, "B0X")
    assert v["variantGroupId"] == "B0X"          # 单品用 SKU 占位
    assert v["isPrimaryVariant"] == "Yes"
    assert v["variantAttributeNames"] == ["color"]
    assert len(notes) == 2


def test_variant_bag_half_bag_stripped_when_optional():
    """单品口径(2026-08-12 旧仓对照):非必填时旧系统从不发变体字段——
    半套(LLM 零星幻觉)整套剔除,而不是替它凑全(凑全无旧实证)。"""
    v, notes = mc.ensure_variant_bag(
        _VSPEC, {"variantAttributeNames": ["color"], "color": "Red"}, "B0X")
    assert "variantAttributeNames" not in v and "variantGroupId" not in v
    assert v["color"] == "Red" and len(notes) == 1
    # 三件俱全 = 真变体注入 → 放行不动
    full = {"variantGroupId": "G1", "variantAttributeNames": ["color"],
            "isPrimaryVariant": "Yes", "color": "Red"}
    v2, n2 = mc.ensure_variant_bag(_VSPEC, dict(full), "B0X")
    assert v2 == full and n2 == []


def test_variant_bag_untouched_when_absent():
    """三件套一个都没碰且都不必填 → 不主动引入(别给自己找麻烦)。"""
    v, notes = mc.ensure_variant_bag(_VSPEC, {"color": "Red"}, "B0X")
    assert "variantGroupId" not in v and notes == []
    # spec 里压根没这些字段的 PT:原样返回
    v2, n2 = mc.ensure_variant_bag({"properties": {"color": {}}},
                                   {"variantGroupId": "x"}, "B0X")
    assert n2 == []


def test_variant_bag_picks_attribute_we_actually_have():
    """(必填补全路径)variantAttributeNames 说有 color 就得真有 color。"""
    v, _ = mc.ensure_variant_bag(_VSPEC_REQ, {"variantGroupId": "G1",
                                              "size": "L"}, "B0X")
    assert v["variantAttributeNames"] == ["size"]     # 有值的那个优先


def test_safe_default_array_prefers_no():
    """2026-08-12 旧仓对照:array 型 enum 兜底先选 No/None/Not Applicable,
    此前直接 [enum[0]] 会把 ["Yes","No"] 类字段兜成 Yes,方向反了。"""
    assert mc.safe_default_for(
        {"type": "array", "items": {"enum": ["Yes", "No"]}}) == ["No"]
    # 原有 "0 - No warning applicable" 优选不受影响
    assert mc.safe_default_for({"type": "array", "items": {
        "enum": ["A", "0 - No warning applicable"]}}) \
        == ["0 - No warning applicable"]


def test_conform_runs_enum_and_type_fixes_on_orderable():
    """Orderable 交还 LLM 后,一致化同样要兜 Orderable 段。"""
    spec = {"required": [], "properties": {"productName": {"type": "string"}}}
    ospec = {"required": [], "properties": {
        "sku": {"type": "string"},
        "fulfillmentLagTime": {"type": "integer"},
        "shipsInOriginalPackaging": {"type": "string",
                                     "enum": ["Yes", "No"]}}}
    v, o, notes, missing = mc.conform(
        spec, ospec, {"productName": "Steel Cup"},
        {"sku": "S1", "fulfillmentLagTime": "1",
         "shipsInOriginalPackaging": "maybe"})
    assert o["fulfillmentLagTime"] == 1                 # 字符串转 integer
    assert o["shipsInOriginalPackaging"] == "No"        # 非法枚举换安全默认
    assert not missing


def test_conform_keeps_orderable_when_ospec_missing():
    """ospec 空保护(2026-08-12):此前 strip_unknown 会把整个 Orderable
    清空——_orderable.json 没就位就是必拒;现改为原样保留。"""
    spec = {"required": [], "properties": {"productName": {"type": "string"}}}
    v, o, notes, missing = mc.conform(
        spec, {}, {"productName": "Steel Cup"},
        {"sku": "S1", "price": 9.99})
    assert o == {"sku": "S1", "price": 9.99}


def test_date_fields_never_get_junk_defaults():
    """第 5 轮 EXT_DATA_ERROR_00030257670757(2026-08-12 首个生产回执):
    releaseDate 被条件必填兜底填了 'Not Available' → 沃尔玛要 YYYY-MM-DD。
    日期字段兜底必须给合法日期;垃圾日期值必填换默认、非必填删。"""
    import re as _re
    ospec = {"required": [], "properties": {
        "sku": {"type": "string"},
        "releaseDate": {"type": "string"},      # spec 没写 format,靠名字识别
    }, "allOf": [{"if": {"required": ["sku"]},
                  "then": {"required": ["releaseDate"]}}]}
    o, notes = mc.fill_known_required(ospec, {"sku": "S1"})
    assert _re.match(r"^\d{4}-\d{2}-\d{2}$", o["releaseDate"])

    # 垃圾值进日期字段:必填→合法默认,非必填→删
    spec = {"required": ["availableDate"], "properties": {
        "availableDate": {"type": "string", "format": "date"},
        "discontinueDate": {"type": "string"}}}
    v, fixes = mc.fix_type_mismatches(spec, {
        "availableDate": "Not Available", "discontinueDate": "No"})
    assert _re.match(r"^\d{4}-\d{2}-\d{2}$", v["availableDate"])
    assert "discontinueDate" not in v

    # 同码反向实证不被误伤:endDate 必须 ISO DateTime(名字推断=两种都合法)
    spec2 = {"required": ["endDate"], "properties": {
        "endDate": {"type": "string"}}}
    v2, fixes2 = mc.fix_type_mismatches(spec2, {"endDate": "2028-12-31T00:00:00Z"})
    assert v2["endDate"] == "2028-12-31T00:00:00Z" and not fixes2
    # 带 enum 的字段不算日期(isDateSensitive 这类枚举不受影响)
    spec3 = {"properties": {"promoDate": {"type": "string",
                                          "enum": ["Yes", "No"]}}}
    v3, _ = mc.fix_type_mismatches(spec3, {"promoDate": "No"})
    assert v3["promoDate"] == "No"


def test_clamp_max_length_local_gate():
    """按 spec maxLength 本地截超长(2026-08-19 生产实证 01076067496949 ×12:
    envelopeSize>12 / manufacturerPartNumber>60 / clothingSize>17 被沃尔玛
    整条拒)。字符串本体与数组元素两种形态;没标 maxLength 的字段不碰。"""
    from services import mp_conform as mc
    spec = {"properties": {
        "envelopeSize": {"type": "string", "maxLength": 12},
        "tags": {"type": "array", "items": {"type": "string", "maxLength": 5}},
        "free": {"type": "string"}}}
    out, notes = mc.clamp_max_length(spec, {
        "envelopeSize": "4.13 x 9.5 inches long",
        "tags": ["short", "waytoolongvalue"],
        "free": "x" * 500})
    assert len(out["envelopeSize"]) <= 12
    assert out["tags"] == ["short", "wayto"]
    assert out["free"] == "x" * 500                 # 没标上限的不碰
    assert any("envelopeSize" in n for n in notes)
    assert any("tags" in n for n in notes)          # 截断必须见 notes
    out2, notes2 = mc.clamp_max_length(spec, {"envelopeSize": "small"})
    assert out2["envelopeSize"] == "small" and notes2 == []


# ── SkuUpdate 必须活着穿过 strip_unknown(SKU 改造批次 3 地基,M5)────────────

def test_sku_update_is_stripped_like_any_other_unknown_field():
    """反向钉死(2026-09-06):`strip_unknown` **没有开关字段放行名单**。

    此前有一条 `ORDERABLE_SYSTEM_SWITCHES = ("SkuUpdate",)` 的放行分支,给
    「形态 B = MP_ITEM 全量 + SkuUpdate 改码」当地基。改码通道定案 MP_ITEM_MATCH
    (同 GTIN + 新 SKU + REPLACE 原地换码,载荷里没有 SkuUpdate)之后,那条分支
    连同 `mp_mapper.build_sku_update_item` / `build_orderable(sku_update=)` 一起
    删了 —— 留着就是第二条改码路径(conventions §六 双轨禁止)。
    """
    assert not hasattr(mc, "ORDERABLE_SYSTEM_SWITCHES")
    assert "SkuUpdate" not in _OSPEC["properties"]        # spec 里确实没有它
    _v, o, dropped = mc.strip_unknown(
        _SPEC, _OSPEC, {"productName": "T"},
        {"sku": "S1", "price": 1.0, "SkuUpdate": "Yes"})
    assert "SkuUpdate" not in o
    assert "orderable.SkuUpdate" in dropped


def test_strip_unknown_is_byte_identical_for_payloads_without_sku_update():
    """不带 SkuUpdate 的普通上架载荷逐字节不变(零行为变化的落脚点)。"""
    args = (_SPEC, _OSPEC, {"productName": "T", "bogusField": 1},
            {"sku": "S1", "price": 1.0, "productName": "T"})
    v, o, dropped = mc.strip_unknown(*args)
    assert o == {"sku": "S1", "price": 1.0}
    assert v == {"productName": "T"}
    assert dropped == ["visible.bogusField", "orderable.productName"]


def test_no_other_unknown_orderable_field_survives():
    """反向:随便塞一个 spec 外字段仍被剔 —— 放行名单是穷举的一元组,不是开闸。"""
    _v, o, dropped = mc.strip_unknown(
        _SPEC, _OSPEC, {}, {"sku": "S1", "SkuUpdateNow": "Yes", "whatever": 1})
    assert "SkuUpdateNow" not in o and "whatever" not in o
    assert set(dropped) == {"orderable.SkuUpdateNow", "orderable.whatever"}


# ── 数组元素是对象:元素内部也按 spec 收口(2026-10-24 装饰枕套 fabricContent)──

_FABRIC = {"type": "array", "minItems": 1, "items": {
    "type": "object", "additionalProperties": False,
    "required": ["materialPercentage", "materialName"],
    "properties": {"materialPercentage": {"type": "number"},
                   "materialName": {"type": "string"}}}}


def test_array_item_invented_sub_fields_drop_optional_field():
    """生产原样:LLM 编出 fabricMaterialName/Percentage,元素必填一个没给 →
    剔掉编出来的子字段后元素无效 → 非必填整字段删(换整条能过,不再连拒三次)。"""
    spec = {"required": [], "properties": {"fabricContent": _FABRIC}}
    vis = {"fabricContent": [{"fabricMaterialName": "Polyester",
                              "fabricMaterialPercentage": 100}]}
    out, notes = mc.fix_array_items(spec, vis)
    assert "fabricContent" not in out
    assert any("fabricMaterialName" in n for n in notes)
    assert any("非必填整字段删除" in n for n in notes)


def test_array_item_extra_keys_stripped_valid_elements_kept():
    spec = {"required": [], "properties": {"fabricContent": _FABRIC}}
    vis = {"fabricContent": [
        {"materialName": "Cotton", "materialPercentage": 60, "note": "x"},
        {"materialName": "Linen"},                     # 缺 materialPercentage
        "Polyester"]}                                  # 不是对象
    out, notes = mc.fix_array_items(spec, vis)
    assert out["fabricContent"] == [{"materialName": "Cotton",
                                     "materialPercentage": 60}]
    assert any("2 个元素" in n for n in notes)


def test_array_item_all_invalid_required_field_blocked_by_validate():
    """必填字段元素全无效:留空数组交 validate 拦下 —— 不提交,省 UPC 与配额。"""
    spec = {"required": ["fabricContent"],
            "properties": {"fabricContent": _FABRIC}}
    v, o, notes, missing = mc.conform(
        spec, _OSPEC, {"fabricContent": [{"fabricMaterialName": "Polyester"}]},
        {"sku": "S1", "price": 9.9})
    assert "visible.fabricContent" in missing


def test_array_item_valid_payload_untouched():
    spec = {"required": [], "properties": {"fabricContent": _FABRIC}}
    vis = {"fabricContent": [{"materialName": "Cotton", "materialPercentage": 100}]}
    out, notes = mc.fix_array_items(spec, vis)
    assert out == vis and notes == []


def test_array_item_open_schema_keeps_extra_keys():
    """元素 schema 没声明 additionalProperties=false 的不剔子字段(只删缺必填的)。"""
    open_items = {"type": "array", "items": {
        "type": "object", "required": ["materialName"],
        "properties": {"materialName": {"type": "string"}}}}
    spec = {"required": [], "properties": {"f": open_items}}
    vis = {"f": [{"materialName": "Cotton", "extra": 1}]}
    out, notes = mc.fix_array_items(spec, vis)
    assert out == vis and notes == []


def test_conditionally_required_object_array_emptied_blocks_submission():
    """条件必填(upholstered=Yes ⇒ fabricContent)的对象数组元素全无效:不能当可选删掉
    照发(必被拒),要进缺失清单不提交(2026-10-24 对抗审查,0829 Beds 实证)。"""
    spec = {"required": [], "properties": {
        "upholstered": {"type": "string", "enum": ["Yes", "No"]},
        "fabricContent": _FABRIC},
        "allOf": [{"if": {"properties": {"upholstered": {"enum": ["Yes"]}},
                          "required": ["upholstered"]},
                   "then": {"required": ["fabricContent"]}}]}
    v, o, notes, missing = mc.conform(
        spec, _OSPEC,
        {"upholstered": "Yes",
         "fabricContent": [{"fabricMaterialName": "Polyester"}]},
        {"sku": "S1", "price": 9.9})
    assert "visible.fabricContent" in missing
    # 条件不触发时照旧:非必填整字段删、可以提交
    v, o, notes, missing = mc.conform(
        spec, _OSPEC,
        {"upholstered": "No",
         "fabricContent": [{"fabricMaterialName": "Polyester"}]},
        {"sku": "S1", "price": 9.9})
    assert "fabricContent" not in v and missing == []


# ── 条件必填的 if 组合子(2026-10-25 换 0829):anyOf / allOf / not / contains 对象 ──
# 下面的 if 子句都是 0829 原件逐字拷贝。此前 evaluate_if 只看 required/properties,
# 条件整个写在 anyOf/allOf 里的 if 一律判「成立」。

_WRISTWATCH_IF = {"anyOf": [{"properties": {"condition": {
    "type": "string", "enum": ["Restored Premium"]}}, "required": ["condition"]}]}
_PREORDER_ANYOF = {"anyOf": [{"properties": {"IsPreorder": {
    "type": "string", "enum": ["Yes"]}}, "required": ["IsPreorder"]}]}
_GRADED_ALLOF = {"allOf": [{"properties": {"collectible_grading_type": {
    "type": "string", "enum": ["Graded"]}}, "required": ["collectible_grading_type"]}]}
_RACK_MOUNT_IF = {"anyOf": [{"properties": {"mountType": {"type": "array", "contains": {
    "type": "string", "enum": ["Rack Mount"]}}}, "required": ["mountType"]}]}
_INVENTORY_QTY_IF = {"properties": {"inventory": {"type": "array", "contains": {
    "type": "object", "required": ["quantity"]}}}, "required": ["inventory"]}


def test_if_anyof_wristwatch_restored_identifier_only_when_restored():
    """0829 新增:Wristwatches「condition=Restored Premium ⇒ restoredProductIdentifier」。
    判成恒成立 = 每块全新手表都被填一个空 ID 的 restoredProductIdentifier,必拒。"""
    spec = {"required": [], "properties": {
        "condition": {"type": "string", "enum": ["New", "Restored Premium"]},
        "restoredProductIdentifier": {"type": "object", "properties": {
            "restoredProductIdType": {"type": "string", "enum": ["GTIN", "UPC"]},
            "restoredProductId": {"type": "string", "minLength": 1}}}},
        "allOf": [{"if": _WRISTWATCH_IF,
                   "then": {"required": ["restoredProductIdentifier"]}}]}
    assert mc.evaluate_if(_WRISTWATCH_IF, {"condition": "New"}) is False
    assert mc.evaluate_if(_WRISTWATCH_IF, {"condition": "Restored Premium"}) is True
    v, notes = mc.fill_known_required(spec, {"condition": "New"})
    assert "restoredProductIdentifier" not in v and notes == []


def test_orderable_release_date_only_for_preorders():
    """Orderable「IsPreorder=Yes ⇒ releaseDate」:此前判恒成立,每个商品都带
    releaseDate=当天;现在只有预售才补。"""
    ospec = {"required": [], "properties": {
        "sku": {"type": "string"},
        "IsPreorder": {"type": "string", "enum": ["No", "Yes"]},
        "releaseDate": {"type": "string", "format": "date-time"}},
        "allOf": [{"if": _PREORDER_ANYOF, "then": {"required": ["releaseDate"]}}]}
    for o_in in ({"sku": "S1"}, {"sku": "S1", "IsPreorder": "No"}):
        o, notes = mc.fill_known_required(ospec, o_in)
        assert "releaseDate" not in o and notes == []
    o, _ = mc.fill_known_required(ospec, {"sku": "S1", "IsPreorder": "Yes"})
    assert o["releaseDate"]


def test_if_allof_requires_every_branch():
    assert mc.evaluate_if(_GRADED_ALLOF, {"collectible_grading_type": "Graded"}) is True
    assert mc.evaluate_if(_GRADED_ALLOF, {"collectible_grading_type": "Ungraded"}) is False
    assert mc.evaluate_if(_GRADED_ALLOF, {}) is False


def test_if_contains_string_enum_inside_anyof():
    """CD Recorders:mountType 含 'Rack Mount' ⇒ rackSize(0829 新增)。"""
    assert mc.evaluate_if(_RACK_MOUNT_IF, {"mountType": ["Rack Mount"]}) is True
    assert mc.evaluate_if(_RACK_MOUNT_IF, {"mountType": ["Desktop"]}) is False


def test_if_contains_object_element_needs_required_subfield():
    """Orderable 的 inventory:元素是对象且带 quantity 才算「含」。"""
    assert mc.evaluate_if(_INVENTORY_QTY_IF, {"inventory": [{"quantity": 1}]}) is True
    assert mc.evaluate_if(_INVENTORY_QTY_IF, {"inventory": [{}]}) is False
    assert mc.evaluate_if(_INVENTORY_QTY_IF, {"inventory": ["x"]}) is False


def test_if_not():
    clause = {"not": {"required": ["x"]}}
    assert mc.evaluate_if(clause, {"x": "1"}) is False
    assert mc.evaluate_if(clause, {}) is True


def test_if_unknown_keyword_logged_and_not_restricting(caplog):
    """spec 换版带来新写法:按不设限处理(与支持前一致),但要记 warning + 计数。"""
    mc.UNKNOWN_IF_KEYWORDS.pop("oneOf", None)
    with caplog.at_level("WARNING", logger="services.mp_conform"):
        assert mc.evaluate_if({"oneOf": [{"required": ["x"]}]}, {}) is True
        mc.evaluate_if({"oneOf": [{"required": ["x"]}]}, {})
    assert mc.UNKNOWN_IF_KEYWORDS["oneOf"] == 2
    assert sum("oneOf" in r.getMessage() for r in caplog.records) == 1
