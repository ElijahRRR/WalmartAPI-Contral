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
