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


def test_group_id_self_parent_falls_back_to_min_family():
    """⚠ parent_asin **等于自己**时拿它当组 ID,会把同组 N 个兄弟切成 N 组。

    旧仓 `variant_groups_design.md` §3.3 记着这个坑:它的采集侧(DMIT)每行的
    parent_asin 就是该行自己,所以旧仓一律用 min(full_set)。我们现在的采集侧
    给真族主,但它哪天改回"填自己",这里得能自愈 —— 而且不报错的分裂最难查。
    """
    fam = ["B0000000A1", "B0000000A2", "B0000000A3"]
    ids = {vg.group_id(a, a, fam) for a in fam}      # parent 全填自己
    assert ids == {"vg_B0000000A1"}                  # 三个兄弟同一个 ID
    # 真族主(生产实见形态)照旧按 parent 派生,ID 更好认
    assert vg.group_id("B000AMXQVI", "B0009GGJ9G",
                       ["B0009GGJ9G", "B0009GGJCI"]) == "vg_B000AMXQVI"
    # 孤品(家族只有自己)不受影响
    assert vg.group_id("", "B0000000A1", ["B0000000A1"]) == "vg_B0000000A1"


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
    assert p["attr_pairs"] == [("color", "Black")]
    assert p["family_size"] == 3 and p["is_primary"] is True


def test_multi_dim_family_sends_every_mapped_dimension():
    """color+size 的家族**两个维度都要发**(2026-08-17 对着旧仓核实后补齐)。

    旧仓 `auto_listing/mapper.py:1374` 是 `sorted(allowed_names &
    set(var_attrs.keys()))` —— 取交集全体;设计文档 §3.5/§4.2 同。我们首版只取
    第一个,是**迁漏**。后果不是少个字段:同族里只差 size 的两个成员会带着
    同一个 variantGroupId + 同一个 color 值发出去,沃尔玛看不出差异。

    顺序按沃尔玛属性名排序 —— 同组成员算出的顺序必须一致,否则同一个组里
    几条的 variantAttributeNames 顺序不同,排查时看着像两组。
    """
    p = vg.plan("B0009GGJ9G", "color_name=Black; size_name=L",
                "B0009GGJCI", "B000AMXQVI", _ENUM)
    assert p["mode"] == "variant"
    assert p["attr_pairs"] == [("color", "Black"), ("size", "L")]
    assert p["unmapped_dims"] == []


def test_unmapped_dimension_is_dropped_not_escalated():
    """部分维度映不上**只剔那几个**,不退单品(旧仓同款),但要点名。"""
    p = vg.plan("A1", "color_name=Black; flavor_name=Vanilla", "A2", "P1",
                _ENUM)                              # _ENUM 没有 flavor
    assert p["mode"] == "variant"
    assert p["attr_pairs"] == [("color", "Black")]
    assert p["unmapped_dims"] == ["flavor_name"]


def test_two_amazon_dims_mapping_to_one_walmart_attr_dedupe():
    """`number_of_items` 与 `item_package_quantity` 都映向 count —— 只能出现一次。

    重复列进 variantAttributeNames 会让载荷自相矛盾(同一个属性名两条)。
    """
    p = vg.plan("A1", "number_of_items=6; item_package_quantity=6", "A2",
                "P1", _ENUM)
    assert [n for n, _ in p["attr_pairs"]] == ["count"]


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
    """spec 没登记的字段一个都不许发:additionalProperties=false,多一个整条被拒。

    ⚠ 2026-08-17 改判:属性名在 variantAttributeNames 枚举里、但 spec **没登记
    这个属性**时,首版是"照发三件套 + 记一句'差异值无处可写'",现在**整套退单品**。
    理由:只发 `variantAttributeNames=[color]` 而没有 color 字段,等于告诉沃尔玛
    "我们按颜色分组"却不说自己是什么颜色 —— 组里几条长得一模一样。
    (旧仓这一步会把值原样写进去再被 strip_unknown 剔掉,留下同款矛盾载荷;
    这是我们比旧仓严的一处,不是迁漏。)
    """
    from services import mp_conform as mc
    p = vg.plan("A1", "color_name=Black", "A2", "P1", ["color"])
    spec = {"properties": {
        "variantGroupId": {"type": "string"},
        "variantAttributeNames": {"type": "array", "items": {"enum": ["color"]}}}}
    v, notes = mc.ensure_variant_bag(spec, {}, "SKU1", plan=p)
    assert not any(k in v for k in mc._VARIANT_BAG)
    assert any("差异值无处可写" in n for n in notes)


def test_conform_drops_one_bad_value_without_killing_the_group():
    """逐属性校验:一个值不合 enum 只剔它,另一个维度照常发(旧仓 Feature A)。

    整组因为一个坏值退回单品,等于一条脏数据毁掉整个家族的变体体验。
    """
    from services import mp_conform as mc
    spec = {"properties": {
        "variantGroupId": {"type": "string"},
        "variantAttributeNames": {"type": "array",
                                  "items": {"enum": ["color", "size"]}},
        "color": {"type": "string", "enum": ["Black", "Navy"]},
        "size": {"type": "string"}}}
    p = vg.plan("A1", "color_name=Chartreuse; size_name=L", "A2", "P1",
                ["color", "size"])
    v, notes = mc.ensure_variant_bag(spec, {}, "SKU1", plan=p)
    assert v["variantAttributeNames"] == ["size"] and v["size"] == "L"
    assert "color" not in v
    assert any("值不合本属性的 enum/类型" in n for n in notes)


def test_conform_coerces_integer_variant_values():
    """count / multipackQuantity 这类 spec 标 integer —— 发字符串会被类型错拒。"""
    from services import mp_conform as mc
    spec = {"properties": {
        "variantGroupId": {"type": "string"},
        "variantAttributeNames": {"type": "array", "items": {"enum": ["count"]}},
        "count": {"type": "integer"}}}
    p = vg.plan("A1", "number_of_items=6", "A2", "P1", ["count"])
    v, _ = mc.ensure_variant_bag(spec, {}, "SKU1", plan=p)
    assert v["count"] == 6 and isinstance(v["count"], int)


def test_code_is_stable_for_counting():
    """摘要计数按 code,不按 reason 分词 —— reason 是中文长句,改一个字计数就散了。"""
    assert vg.plan("A1", "color_name=B", "A2", "P1", _ENUM)["code"] == "variant"
    assert vg.plan("A1", "", "A2", "P1", _ENUM)["code"] == "no_attrs"
    assert vg.plan("A1", "color_name=B", ",".join(f"B{i}" for i in range(25)),
                   "P1", _ENUM)["code"] == "oversize"
    assert vg.plan("A1", "flavor_name=B", "A2", "P1", _ENUM)["code"] == "no_dim"
    assert vg.plan("", "color_name=B", "", "", _ENUM)["code"] == "no_group_id"


def test_list_new_counts_by_code_not_by_reason_words():
    """接线侧钉住:用 vp["code"] 做计数键(reason 是中文长句,分词计数会散)。"""
    import inspect

    from workflows import list_new
    src = inspect.getsource(list_new._plan_variants)
    assert 'n_var[vp["code"]]' in src
    assert 'reason"].split' not in src


def test_variant_counts_are_computed_before_the_summary_line():
    """⚠ 摘要里的"变体"一栏曾经**从来没出现过** —— 死代码,dry-run 与真跑都没数。

    根因:`gate_line` 是拼好的**字符串**,而 n_var 原来在它下面的提交循环里才填,
    后填的计数永远进不去;dry-run 更是在提交循环之前就 return 了。
    这条用例钉的是**顺序**:变体决策必须在 gate_line 拼出来之前算完。
    """
    import inspect

    mod = __import__("workflows.list_new", fromlist=["run"])
    src = inspect.getsource(mod.run)
    assert src.index("_plan_variants(ready") < src.index("闸门:")
    # 一致化必须**复用**这份决策,不许重算 —— 重算就是 dry-run 报一份、
    # 真跑发另一份,中间任何差异都表现为"dry-run 说没事"。
    # 2026-08-18 三段式重排后,消费点从提交循环移进预备期 _prep_rows
    prep_src = inspect.getsource(mod._prep_rows)
    assert 'variant=r.get("_vplan")' in prep_src
    # _variant_plan 只在 _plan_variants 里调,run/_prep_rows 都不许重算
    assert "_variant_plan(" not in src
    assert "_variant_plan(" not in prep_src


def _vrow(asin, pairs, gid="vg_G", store="M001"):
    return {"asin": asin, "store": store,
            "_vplan": {"mode": "variant", "group_id": gid, "code": "variant",
                       "is_primary": True, "attr_pairs": list(pairs),
                       "family_size": 3}}


def test_dimension_with_identical_values_across_the_group_is_dropped():
    """⚠ 声明一个组内取值全同的维度 = 告诉沃尔玛"按尺寸不同"再给三个一样的尺寸。

    2026-08-17 生产实见(所有者的礼品袋组):亚马逊给的 size_name 三个成员**全是**
    `1 Count (Pack of 100)` —— 那不是尺寸是包装数量,对分组毫无信息量。真正区分
    它们的是标题里的 Small/Medium/Large,亚马逊自己没放进 twister 维度。
    """
    from workflows import list_new as ln
    S = ("size", "1 Count (Pack of 100)")
    rows = [_vrow("B0BWMV956C", [("color", "Kraft brown"), S]),
            _vrow("B0BWMVQHVJ", [("color", "Brown"), S]),
            _vrow("B0BWMY43TD", [("color", "Original brown"), S])]
    ln._drop_degenerate_dims(rows)
    for r in rows:
        assert [n for n, _ in r["_vplan"]["attr_pairs"]] == ["color"]
        assert r["_vplan"]["degenerate_dims"] == ["size"]
        assert r["_vplan"]["mode"] == "variant"      # color 还有差异,仍是变体组


def test_group_with_no_differentiating_dimension_falls_back_to_single():
    """维度剔光 ⇒ 这几条压根不该是一个变体组(沃尔玛看不出区别),整组退单品。

    宁可各自独立上架,也不要发一个成员之间毫无区别的变体组 —— 后者要用
    MP_MAINTENANCE 才改得回来。
    """
    from workflows import list_new as ln
    rows = [_vrow("A1", [("size", "X")]), _vrow("A2", [("size", "X")])]
    ln._drop_degenerate_dims(rows)
    for r in rows:
        assert r["_vplan"]["mode"] == "single"
        assert r["_vplan"]["code"] == "no_diff_dim"
        assert "取值全同" in r["_vplan"]["reason"]


def test_single_visible_member_is_never_judged_degenerate():
    """本轮只看见 1 个成员时**什么都不做** —— 一条数据判不出"组内有没有差异"。

    所有者第一次验收就是只放了 2 个成员进表(家族 3 个),按"可能有差异"处理
    才是对的;据一条就剔维度,等于把增量上架的第一条永久判成单品。
    """
    from workflows import list_new as ln
    rows = [_vrow("A1", [("size", "X")])]
    ln._drop_degenerate_dims(rows)
    assert rows[0]["_vplan"]["mode"] == "variant"
    assert rows[0]["_vplan"]["attr_pairs"] == [("size", "X")]
    # 不同组的两条也各算各的,不许跨组比
    rows2 = [_vrow("A1", [("size", "X")], gid="vg_1"),
             _vrow("A2", [("size", "X")], gid="vg_2")]
    ln._drop_degenerate_dims(rows2)
    assert all(r["_vplan"]["mode"] == "variant" for r in rows2)


def test_same_round_siblings_do_not_both_claim_primary():
    """⚠ 同一轮里同族两个新成员会**各自都判自己是主变体** → 同一个 feed 两个 Yes。

    根因:`family_has_primary` 查的是 `catalog.walmart_items` 里**已在架**的同族,
    而这两个此刻都还没在架,两边都查到"本店没有主变体"。旧仓正是为了躲这个才
    干脆不发这个字段(设计文档 §3.4:"会出现两个 primary,Walmart 行为未定义")。
    我们保留字段,就得自己收口。

    取谁:**ASIN 字母序**第一个,不是行序 —— 行序会随表格增删漂,主变体跟着漂
    等于每轮都在改沃尔玛端的首图。
    """
    from workflows import list_new as ln

    def _row(asin, gid, primary=True):
        return {"asin": asin, "store": "M001",
                "_vplan": {"mode": "variant", "group_id": gid,
                           "is_primary": primary, "attr_pairs": [("color", "X")],
                           "code": "variant"}}

    rows = [_row("B0BWMV956C", "vg_P1"), _row("B0BWMVQHVJ", "vg_P1"),
            _row("B078R8W927", "vg_P2")]
    ln._dedupe_primary(rows)
    prim = {r["asin"] for r in rows if r["_vplan"]["is_primary"]}
    # 同组只剩字母序第一个;另一组的独苗不受影响
    assert prim == {"B0BWMV956C", "B078R8W927"}
    # 重跑幂等:再跑一次结果不变
    ln._dedupe_primary(rows)
    assert {r["asin"] for r in rows if r["_vplan"]["is_primary"]} == prim


def test_dedupe_primary_respects_a_listed_primary():
    """本店同族已有在架主变体时,_variant_plan 已把新成员判成非主 —— 这里不许翻回。"""
    from workflows import list_new as ln
    rows = [{"asin": "B1", "store": "M001",
             "_vplan": {"mode": "variant", "group_id": "vg_P1",
                        "is_primary": False, "code": "variant"}},
            {"asin": "B2", "store": "M001",
             "_vplan": {"mode": "variant", "group_id": "vg_P1",
                        "is_primary": False, "code": "variant"}}]
    ln._dedupe_primary(rows)
    assert not any(r["_vplan"]["is_primary"] for r in rows)


def test_list_new_lookup_is_store_scoped_and_failure_tolerant():
    """② 只查本店(分配侧保证一组只分一个店);查库失败不许把整行拖下水。

    变体只是锦上添花 —— 拿不到在架信息就按"本店还没有同族"处理,派生 ID 照样
    能让以后的兄弟归到一起(group_id 由 parent_asin 决定,不依赖这次查询)。
    """
    import inspect

    from workflows import list_new
    sql = list_new._FAMILY_LISTED_SQL
    assert "w.store = %(store)s" in sql and "missing_since IS NULL" in sql
    body = inspect.getsource(list_new._variant_plan)
    assert "except Exception" in body and "按本店无同族处理" in body
    # 查同族时把自己排掉:自己还没上架,查出来只会是噪声
    assert 'a != r["asin"]' in body


def test_family_lookup_matches_through_the_registry():
    """同族已在架按**身份键**匹配(0a-26);参数名也从 skus 正过来叫 asins。

    传进去的一直是同族 **ASIN**(variant_group.parse_family 的产物),叫 skus
    是历史笔误;切码之后这个名字会直接误导下一个人拿 SKU 去填。
    ⚠ 本处**有意不加 abandoned_at 谓词**:同族查的是「这家店此刻还挂着哪些
    同族成员」这个在架事实,与码是否已弃用无关(abandoned_at 只出现在 mint、
    list_new 去重闸、alloc_push._SQL_ONLINE 三处 —— 消费方契约)。
    """
    from workflows import list_new
    sql = list_new._FAMILY_LISTED_SQL
    assert "coalesce(ls.source_key, w.sku) = ANY(%(asins)s::text[])" in sql
    assert "ls.source_type = 'amz'" in sql
    assert "%(skus)s" not in sql
    assert "abandoned_at" not in sql
