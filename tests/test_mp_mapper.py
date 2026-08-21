"""listing L2d 回归:mapper 实证约束逐条验证(每条都有旧错误码背书)。"""

from services import mp_mapper as m


def test_orderable_three_traps():
    o = m.build_orderable("B0X", "012345678905", "19.999", 7, "10001234")
    assert isinstance(o["productIdentifiers"], dict)         # 单对象非数组
    assert o["price"] == 20.0 and isinstance(o["price"], float)  # 裸 number
    assert o["inventory"][0]["fulfillmentCenterID"] == "10001234"
    # quantity 是**裸 int**(2026-08-09 实证:写成 {unit,amount} 被拒
    # EXT_DATA_ERROR_50716566635066 "'Inventory Quantity' … Enter a 'Number'")
    assert o["inventory"][0]["quantity"] == 7
    assert "T" in o["endDate"]                               # ISO DateTime 含时间
    # 旧金样 Orderable **从不发** brand / countryOfOriginAssembly(2026-08-12
    # 旧仓对照删除:多发字段与 Orderable.productName 同血统 60670554076755)
    assert "brand" not in o and "countryOfOriginAssembly" not in o
    assert o["fulfillmentLagTime"] == 1 and o["MustShipAlone"] == "No"
    # Orderable 必填三件(旧 force_overrides 同款,首跑因缺失被拒)
    assert o["country_of_origin_substantial_transformation"] == "China"
    assert o["ShippingWeight"] == m.DEFAULT_SHIPPING_WEIGHT   # 无重量数据时
    assert "T" in o["startDate"] and "Z" in o["startDate"]
    o2 = m.build_orderable("B0X", "0123", 10, 1, "P1", pt="Cups",
                           product={"attrs": {"weight": {"package": "3.5 pounds"}}})
    assert o2["ShippingWeight"] == 3.5
    # specProductType 官方 20260608 已移除,不再写(写了也会被 strip_unknown
    # 按新 spec 剔掉,"写了再剔"白费一道工序还误导读代码的人)
    assert "specProductType" not in o2


def test_orderable_merges_llm_fields_forced_win():
    """旧结构恢复:LLM 按 spec 填 Orderable,系统强制项覆盖在上。"""
    o = m.build_orderable("B0X", "012345678905", 10, 3, "P1", llm_fields={
        "netContent": {"productNetContentMeasure": 1,
                       "productNetContentUnit": "Each"},
        "price": 999,                    # 系统专属:必须被强制值顶掉
        "sku": "HACK", "brand": "X",     # 同上/旧金样不发
        "stateRestrictions": [],         # 空值不带入
    })
    assert o["netContent"]["productNetContentUnit"] == "Each"   # LLM 字段保留
    assert o["price"] == 10.0 and o["sku"] == "B0X"             # 强制项赢
    assert "brand" not in o and "stateRestrictions" not in o


def test_visible_cert_forces_and_doc_field_cleanup():
    spec = {"properties": {
        "certification_type": {"enum": ["有证书", "Neither of these applies"]},
        "has_nrtl_listing_certification": {"enum": ["Yes", "No"]},
        "isProp65WarningRequired": {},
        "has_written_warranty": {"enum": ["Skip for now", "Yes"]},   # 无 No → 降级
        "productName": {},
    }}
    llm_out = {"productName": "A Good Cup Name",
               "certification_type": "有证书",              # LLM 瞎选 → 强制覆盖
               "warrantyText": "1 year",                    # 危险文档字段 → 删
               "cpsc_document_reference_id": "doc123",      # 后缀匹配 → 删
               "isAssemblyRequired": "Yes"}
    out = m.finalize_visible("Cups", llm_out, spec)
    assert out["certification_type"] == "Neither of these applies"
    assert out["has_nrtl_listing_certification"] == "No"
    assert out["has_written_warranty"] == "Skip for now"     # 降级链生效
    assert "warrantyText" not in out and "cpsc_document_reference_id" not in out
    assert "isAssemblyRequired" not in out                   # 字段不在 spec → 剔除
    assert out["brand"] == "Unbranded"


def test_visible_text_clamps():
    # 文案是系统的地盘:LLM 写的一律丢弃,改用亚马逊原文(force_amazon_copy)
    out = m.finalize_visible("Cups", {
        "productName": "LLM 写的会被丢掉",
        "manufacturer": "M" * 100,
    }, spec=None, product={
        "title": "N" * 300, "brand": None,
        "attrs": {"bullet_points": [f"F{i}" * 300 for i in range(9)],
                  "description": "D" * 5000}})
    assert len(out["productName"]) == 199
    assert len(out["shortDescription"]) == 4000   # 亚马逊原文硬截,不加省略号
    assert len(out["keyFeatures"]) == 7
    assert all(len(f) <= 500 for f in out["keyFeatures"])
    assert len(out["manufacturer"]) == 60


def test_images_keep_amazon_order_and_min_five_secondary():
    # 保序去重(2026-08-12 旧仓对照):主图=亚马逊原序第一张,不再字典序;
    # secondary 不足 5 张整个字段不写(schema minItems=5)
    a = m.finalize_visible("Cups", {}, None,
                           images=["u9", "u1", "u5", "u3", "u9"])
    assert a["mainImageUrl"] == "u9"                         # 原序第一张=主图
    assert "productSecondaryImageURL" not in a               # 只有 3 张 secondary
    b = m.finalize_visible("Cups", {}, None,
                           images=[f"u{i}" for i in range(7, 0, -1)])   # 7 张倒序
    assert b["mainImageUrl"] == "u7"
    assert b["productSecondaryImageURL"] == ["u6", "u5", "u4", "u3", "u2", "u1"]


def test_assemble_mp_item_shape():
    o = m.build_orderable("B0X", "012345678905", 10, 3, "P1")
    v = m.finalize_visible("Cups", {}, None,
                           product={"title": "Steel Cup 12oz", "attrs": {}})
    item = m.assemble_mp_item(o, "Cups", v)
    assert set(item.keys()) == {"Orderable", "Visible"}      # 并列顶级,非 MPProduct
    assert item["Visible"]["Cups"]["productName"] == "Steel Cup 12oz"
    # productName **不进 Orderable**(2026-08-09 生产实证 EXT_DATA_ERROR_60670554076755:
    # "'productName' is not a valid field"——此前照旧实证写的"两处同值"在 v5 spec 下是错的)
    assert "productName" not in item["Orderable"]
    assert item["Orderable"]["ShippingWeight"] > 0            # 必填,总有值


def test_force_amazon_copy_from_bullets():
    """文案强制用亚马逊原文;keyFeatures 从卖点来(minItems 靠它满足)。"""
    product = {"title": "ACME Steel Cup 12oz", "brand": "ACME",
               "attrs": {"bullet_points": ["• ACME 不锈钢材质", "保温 6 小时",
                                           "可洗碗机清洗", "附赠杯盖"],
                         "description": "ACME 出品的耐用水杯。"}}
    out = m.finalize_visible("Cups", {"swatchImages": "乱填的",
                                      "keyFeatures": ["LLM 编的"],
                                      "color": "Silver"}, None, product=product)
    assert out["productName"] == "Steel Cup 12oz"        # 品牌名被去掉
    assert len(out["keyFeatures"]) == 4                  # 来自卖点,不是 LLM 的
    assert out["keyFeatures"][0] == "不锈钢材质"          # 去品牌 + 去项目符号
    assert "swatchImages" not in out                     # 系统后处理字段丢弃
    assert out["color"] == "Silver"                      # 结构化字段留给 LLM
    assert out["brand"] == "Unbranded"


def test_force_copy_pads_key_features_from_sentences():
    """卖点不足 4 条:从描述拆句补齐(EXT_DATA_ERROR_55506974520167)。

    句子门槛 25 字符是按英文亚马逊文案调的(真实数据形态)。
    """
    product = {"title": "Garden Seeder Bulb Transplanter Tool", "brand": None,
               "attrs": {"bullet_points": ["Manual seed dispenser"],
                         "description": (
                             "Precisely sows beans peanuts and other seeds. "
                             "Stainless steel body resists rust and bending. "
                             "Ninety-one centimeters long so no bending over.")}}
    out = m.finalize_visible("Tools", {}, None, product=product)
    assert len(out["keyFeatures"]) >= 4


def test_system_owned_fields_in_prompt():
    msgs = m.build_llm_messages("Cups", {"properties": {}}, {"title": "x"})
    sys_prompt = msgs[0]["content"]
    assert "swatchImages" in sys_prompt and "keyFeatures" in sys_prompt
    assert "type=array" in sys_prompt          # 数组必须给数组


def test_force_fallback_has_none_tier():
    """2026-08-12 旧仓对照:enum 只有 None/Yes-系列时旧选 None,不掉 enum[0]
    (enum[0] 常是 'Yes - Warranty Text',一填触发 warrantyText 条件必填)。"""
    spec = {"properties": {"has_written_warranty": {
        "enum": ["Yes - Warranty Text", "Yes - Warranty URL", "None"]}}}
    out = m.finalize_visible("Cups", {"has_written_warranty": "whatever"},
                             spec)
    assert out["has_written_warranty"] == "None"


def test_assembly_people_field_is_dangerous_doc():
    """旧八项之一:留着与 isAssemblyRequired=No 自相矛盾 → 必删。"""
    out = m.finalize_visible(
        "Cups", {"suggested_number_of_people_for_assembly": 2}, None)
    assert "suggested_number_of_people_for_assembly" not in out


def test_key_features_padded_to_pt_min_items():
    """per-PT minItems(旧 enforce_copy_limits):PT 要 6 条就凑 6 条,
    写死 4 会被自家 validate 卡死永远进不了 feed。"""
    spec = {"properties": {"keyFeatures": {"type": "array", "minItems": 6}}}
    product = {"title": "Garden Seeder Bulb Transplanter Tool", "attrs": {
        "bullet_points": ["Durable steel construction for many years " * 2],
        "description": ("Great for planting bulbs seeds and seedlings. "
                        "Comfortable ergonomic handle reduces hand fatigue. "
                        "Depth markings help consistent planting depth. "
                        "Rust resistant coating protects the blade. "
                        "Suitable for garden lawn and greenhouse work. "
                        "Easy to clean and store after use.")}}
    out = m.finalize_visible("Tools", {}, spec, product=product)
    assert len(out["keyFeatures"]) >= 6


def test_llm_messages_rich_metadata_and_orderable_section():
    """2026-08-12 旧仓对照恢复:type/required/minItems 进提示词,必填全量,
    Orderable 段交还 LLM(系统专属字段剔除),条件必填翻译给模型看。"""
    import json
    spec = {"required": ["occasion"], "properties": {
        "occasion": {"type": "array", "minItems": 1,
                     "items": {"enum": ["Birthday", "Wedding"]}},
        "color": {"type": "string"},
        "brand": {"type": "string"},                # SYSTEM_OWNED:不进提示词
    }, "allOf": [{"if": {"properties": {"powered": {"enum": ["Yes"]}}},
                  "then": {"required": ["powerType"]}}]}
    ospec = {"required": ["netContent"], "properties": {
        "netContent": {"type": "object"},
        "price": {"type": "number"},                # 系统专属:不进提示词
    }}
    msgs = m.build_llm_messages("Cups", spec, {"title": "x"}, ospec=ospec)
    body = json.loads(msgs[1]["content"])
    occ = body["visible_required"]["occasion"]
    assert occ["type"] == "array" and occ["required"] and occ["minItems"] == 1
    assert occ["items"]["enum"] == ["Birthday", "Wedding"]
    assert "brand" not in body["visible_required"]
    assert "brand" not in body["visible_optional"]
    assert "netContent" in body["orderable_required"]
    assert "price" not in body["orderable_required"]
    assert body["conditional"][0]["则必填"] == ["powerType"]
    assert '"orderable"' in msgs[0]["content"]      # 两段式输出要求


def test_split_llm_output_two_part_and_legacy_flat():
    v, o = m.split_llm_output({"visible": {"color": "Red"},
                               "orderable": {"netContent": {}}})
    assert v == {"color": "Red"} and o == {"netContent": {}}
    v2, o2 = m.split_llm_output({"color": "Red"})      # 旧缓存平铺形态
    assert v2 == {"color": "Red"} and o2 == {}
