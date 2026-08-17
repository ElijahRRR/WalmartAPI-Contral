"""变体分组积木回归(所有者定稿 2026-08-15 四条批复)。"""

from services import variant_group as vg

_ENUM = ["color", "size", "style", "count"]


def test_parse_attrs_splits_on_first_equals_only():
    """采集侧实证格式 "color_name=Black; size_name=L";取值里带等号不许把维度切碎。"""
    assert vg.parse_attrs("color_name=Black; size_name=L") == {
        "color_name": "Black", "size_name": "L"}
    assert vg.parse_attrs("size_name=6x6=36") == {"size_name": "6x6=36"}
    assert vg.parse_attrs("color_name=Red|size_name=M") == {
        "color_name": "Red", "size_name": "M"}       # '|' 同样认
    assert vg.parse_attrs("") == {} and vg.parse_attrs(None) == {}
    assert vg.parse_attrs("没有等号") == {}
    assert vg.parse_attrs("k=; =v") == {}            # 半边空的丢掉


def test_parse_family_adds_self():
    """采集侧的 variation_asins **不含自己**(2026-08-15 生产实证:三个兄弟互指
    对方两个)。下游算组大小、判主变体都按含自己的口径,这里补上。"""
    assert vg.parse_family("B0009GGJCI,B0009GGJDW", "B0009GGJ9G") == [
        "B0009GGJ9G", "B0009GGJCI", "B0009GGJDW"]
    assert vg.parse_family("b0001 b0002", "B0001") == ["B0001", "B0002"]  # 去重+大写
    assert vg.parse_family("", "B0X") == ["B0X"]     # 无家族 = 自己一个
    assert vg.parse_family(None, "") == []


def test_group_id_is_derived_not_looked_up():
    """同族成员各自独立算得到同一个 ID —— 这是"增量归组自动发生"的全部机制。

    不由 ID 相等来保证的话,就得回头给已在架的成员补发 MP_MAINTENANCE,
    多一次写、多一份配额,还多一个"补发失败就永远合不上"的失败态。
    """
    a = vg.group_id("B000AMXQVI", "B0009GGJ9G")
    b = vg.group_id("b000amxqvi", "B0009GGJDW")      # 不同成员、大小写不同
    assert a == b == "vg_B000AMXQVI"
    assert vg.group_id("", "B0X") == "vg_B0X"        # 无父体退回自身
    assert vg.group_id(None, "") is None


def test_pick_walmart_dim_never_guesses():
    assert vg.pick_walmart_dim(["color_name"], _ENUM) == "color"
    assert vg.pick_walmart_dim(["size_name"], _ENUM) == "size"
    assert vg.pick_walmart_dim(["set_name"], _ENUM) == "style"     # 别名映射
    assert vg.pick_walmart_dim(["number_of_items"], _ENUM) == "count"
    # 枚举里没有 → 不猜(发一个 PT 不认的属性名会让整条被拒)
    assert vg.pick_walmart_dim(["flavor_name"], _ENUM) is None
    assert vg.pick_walmart_dim(["color_name"], []) is None
    assert vg.pick_walmart_dim(["没见过的维度"], _ENUM) is None


def test_plan_variant_happy_path():
    """2026-08-15 生产实见的 GustBuster 组:三兄弟只差 color_name。"""
    p = vg.plan("B0009GGJ9G", "color_name=Black", "B0009GGJCI,B0009GGJDW",
                "B000AMXQVI", _ENUM)
    assert p["mode"] == "variant" and p["reason"] == ""
    assert p["group_id"] == "vg_B000AMXQVI"
    assert (p["attr_name"], p["attr_value"]) == ("color", "Black")
    assert p["family_size"] == 3 and p["is_primary"] is True


def test_multi_dim_family_is_grouped_by_one_dim_and_says_so():
    """⚠ 多维家族**只按一个维度分组**,这是已知限制,必须可数不可静默。

    所有者 2026-08-17 问「单属性多属性都会自动用对应方法吧」——不会:
    `pick_walmart_dim` 取第一个映得上的维度,color+size 的家族只按 color 分。
    后果不只是少发一个字段:同族里**只差 size 的两个成员**会带着同一个
    variantGroupId + 同一个 color 值发出去,沃尔玛看不出它们有什么不同。

    真支持多维要发多个 variantAttributeNames + 每个维度各写一个属性,
    是设计变更待所有者定;在那之前 `extra_dims` 让 list_new 摘要单列一栏。
    """
    p = vg.plan("B0009GGJ9G", "color_name=Black; size_name=L",
                "B0009GGJCI", "B000AMXQVI", _ENUM)
    assert p["mode"] == "variant"
    assert (p["attr_name"], p["attr_value"]) == ("color", "Black")
    assert p["extra_dims"] == ["size_name"]        # 没发的那些,点名
    # 单维不该报:否则这一栏天天有数,人就不看了
    assert vg.plan("B0009GGJ9G", "color_name=Black", "B0009GGJCI",
                   "B000AMXQVI", _ENUM)["extra_dims"] == []


def test_plan_uses_existing_group_id_when_family_already_listed():
    """③ 所有者定稿:同族已有成员在架 → 新成员沿用它的 variantGroupId。"""
    p = vg.plan("B0009GGJCI", "color_name=Navy", "B0009GGJ9G,B0009GGJDW",
                "B000AMXQVI", _ENUM, existing_group_id="OLD_GROUP_7",
                family_has_primary=True)
    assert p["group_id"] == "OLD_GROUP_7"       # 派生 ID 让位给在架的
    assert p["is_primary"] is False             # 一组只能有一个主变体


def test_oversized_family_falls_back_to_single_not_rejected():
    """④ 所有者定稿:超 20 个成员**退回单品口径照常上架**,不是拒绝上架。

    ⚠ 组大小按亚马逊真实家族算,不按库里采到几个 —— 库里只有 3 个而家族真有
    500 个的,今天当变体上了、明天采到更多就爆表。
    """
    family = ",".join(f"B{i:09d}" for i in range(30))
    p = vg.plan("B000000001", "color_name=Black", family, "P1", _ENUM)
    assert p["mode"] == "single" and "超上限 20" in p["reason"]
    assert p["family_size"] == 30


def test_every_single_fallback_states_a_reason():
    """静默降级 = 变体功能悄悄没生效而没人知道。四种退回都要给出具体原因。"""
    cases = [
        (vg.plan("A1", "", "A2", "P1", _ENUM), "无变体维度取值"),
        (vg.plan("A1", "color_name=X", ",".join(f"B{i}" for i in range(25)),
                 "P1", _ENUM), "超上限"),
        (vg.plan("A1", "flavor_name=X", "A2", "P1", _ENUM), "映不上"),
        (vg.plan("", "color_name=X", "", "", _ENUM), "凑不出稳定组 ID"),
    ]
    for got, needle in cases:
        assert got["mode"] == "single" and needle in got["reason"], got


def test_boundary_exactly_at_limit_is_still_variant():
    """20 个是上限**之内**(超了才退)——边界写反会让整档 20 成员组全掉单品。"""
    family = ",".join(f"B{i:09d}" for i in range(1, 20))     # 19 个 + 自己 = 20
    p = vg.plan("B000000000", "color_name=Black", family, "P1", _ENUM)
    assert p["family_size"] == 20 and p["mode"] == "variant"


# ── mp_conform 接线回归 ──────────────────────────────────────────────────────

# ⚠ 照抄 tests/test_mp_conform._VSPEC 的真实形态:variantAttributeNames 必须带
# "type": "array" —— 少了它 _type_of 默认 "string",_enum_of 就取不到 items.enum,
# 枚举复核那段会**空转**(测试照样绿,而生产会发出 PT 不认的属性名)。
_VSPEC = {"properties": {
    "variantGroupId": {"type": "string"},
    "isPrimaryVariant": {"type": "string", "enum": ["Yes", "No"]},
    "variantAttributeNames": {"type": "array",
                              "items": {"enum": ["color", "size"]}},
    "color": {"type": "string"}, "size": {"type": "string"}}}


def test_conform_writes_full_bag_and_the_differentiating_value():
    """三件套 + **维度取值**都要落盘:组内没有差异值,沃尔玛看不出这几个有何不同。"""
    from services import mp_conform as mc
    p = vg.plan("B0009GGJ9G", "color_name=Black", "B0009GGJCI,B0009GGJDW",
                "B000AMXQVI", ["color", "size"])
    v, notes = mc.ensure_variant_bag(_VSPEC, {}, "SKU1", plan=p)
    assert v["variantGroupId"] == "vg_B000AMXQVI"
    assert v["variantAttributeNames"] == ["color"]
    assert v["isPrimaryVariant"] == "Yes"
    assert v["color"] == "Black"                      # 差异值
    assert any("变体组 vg_B000AMXQVI" in n for n in notes)


def test_conform_rechecks_enum_and_falls_back_rather_than_guessing():
    """决策是拿枚举算的,但 spec 会换版。属性名不在本 PT 枚举内 → 整套剔除退单品。

    不复核就会发出 PT 不认的属性名,整条被拒(additionalProperties=false)。
    """
    from services import mp_conform as mc
    p = dict(vg.plan("A1", "color_name=Black", "A2", "P1", ["color"]))
    spec = {"properties": {
        "variantGroupId": {"type": "string"}, "isPrimaryVariant": {"type": "string"},
        "variantAttributeNames": {"type": "array", "items": {"enum": ["size"]}}}}
    v, notes = mc.ensure_variant_bag(spec, {}, "SKU1", plan=p)
    assert not any(k in v for k in mc._VARIANT_BAG)     # 三件套一个不留
    assert any("不在本 PT 枚举内" in n for n in notes)


def test_conform_single_mode_is_untouched():
    """mode='single' 与不给 plan 走原单品口径 —— 退回路径一个字都没改。"""
    from services import mp_conform as mc
    p = vg.plan("A1", "", "", "P1", ["color"])          # 无维度取值 → single
    assert p["mode"] == "single"
    a, _ = mc.ensure_variant_bag(_VSPEC, {"color": "Red"}, "SKU1", plan=p)
    b, _ = mc.ensure_variant_bag(_VSPEC, {"color": "Red"}, "SKU1")
    assert a == b


def test_conform_only_writes_fields_the_spec_registered():
    """spec 没登记的字段一个都不许发:additionalProperties=false,多一个整条被拒。"""
    from services import mp_conform as mc
    p = vg.plan("A1", "color_name=Black", "A2", "P1", ["color"])
    spec = {"properties": {
        "variantGroupId": {"type": "string"},
        "variantAttributeNames": {"type": "array", "items": {"enum": ["color"]}}}}
    v, notes = mc.ensure_variant_bag(spec, {}, "SKU1", plan=p)
    assert set(v) == {"variantGroupId", "variantAttributeNames"}
    assert "isPrimaryVariant" not in v and "color" not in v
    assert any("无 color 属性" in n for n in notes)


def test_code_is_stable_for_counting():
    """摘要计数按 code,不按 reason 分词 —— reason 是中文长句,改一个字计数就散了。"""
    assert vg.plan("A1", "color_name=B", "A2", "P1", _ENUM)["code"] == "variant"
    assert vg.plan("A1", "", "A2", "P1", _ENUM)["code"] == "no_attrs"
    assert vg.plan("A1", "color_name=B", ",".join(f"B{i}" for i in range(25)),
                   "P1", _ENUM)["code"] == "oversize"
    assert vg.plan("A1", "flavor_name=B", "A2", "P1", _ENUM)["code"] == "no_dim"
    assert vg.plan("", "color_name=B", "", "", _ENUM)["code"] == "no_group_id"


def test_list_new_counts_by_code_not_by_reason_words():
    """接线侧钉住:用 vplan["code"] 做计数键。"""
    import inspect

    from workflows import list_new
    src = inspect.getsource(list_new)
    assert 'n_var[vplan["code"]]' in src
    assert 'reason"].split' not in src


def test_list_new_lookup_is_store_scoped_and_failure_tolerant():
    """② 只查本店(分配侧保证一组只分一个店);查库失败不许把整行拖下水。

    变体只是锦上添花 —— 拿不到在架信息就按"本店还没有同族"处理,派生 ID 照样
    能让以后的兄弟归到一起(group_id 由 parent_asin 决定,不依赖这次查询)。
    """
    import inspect

    from workflows import list_new
    sql = list_new._FAMILY_LISTED_SQL
    assert "store = %(store)s" in sql and "missing_since IS NULL" in sql
    body = inspect.getsource(list_new._variant_plan)
    assert "except Exception" in body and "按本店无同族处理" in body
    # 查同族时把自己排掉:自己还没上架,查出来只会是噪声
    assert 'a != r["asin"]' in body
