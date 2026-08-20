"""PT 准入判定:由官方 spec 必填字段推认证与三档结论。

这套用例钉死的是**为什么要重建那张表**(所有者 2026-08-20 核查):飞书那份
「必需认证」45% 空、46% 的 PTG 组内一字不差(按组批量套的),于是婴儿配方奶
标成了 CPSIA、宠物碗标成了 AAFCO。新口径只认官方 spec 的字段。
"""

from services import pt_admission as pa
from workflows.pt_spec_sync import _age_values


# ── 必填字段要**递归**收全 ────────────────────────────────────────────────

def test_required_is_collected_from_every_level():
    """只取顶层 required 会把 PT 专属的合规字段整批漏掉 —— 而配料表/小零件/电池
    恰恰都挂在深层,漏了就等于判定件什么都看不见,而且不报错。"""
    spec = {"properties": {"MPItem": {"items": {
        "required": ["productName", "brand"],
        "properties": {"Orderable": {"required": ["ingredients"]},
                       "Visible": {"properties": {"Baby Food": {
                           "required": ["smallPartsWarnings", "ageGroup"]}}}}}}}}
    got = pa.extract_required(spec)
    assert got == {"productName", "brand", "ingredients",
                   "smallPartsWarnings", "ageGroup"}


def test_extract_required_survives_lists_and_junk():
    assert pa.extract_required({"a": [{"required": ["x"]}, 3, None],
                                "required": "不是数组"}) == {"x"}


# ── 三档口径:主体资质 → 否 / 买得到报告 → 需评估 / 纯标签 → 是 ────────────

def test_food_fields_mean_fda_facility_registration():
    """配料表 + **食品类共现字段** = 食品 = 要 FDA 食品设施注册 + 美国代理人 → 否。

    这条正是当年填错的那个:`Baby Foods & Formula` 整组被套上 CPSIA
    (儿童产品符合证书),而婴儿配方奶真正要的是 FDA 注册。
    """
    a = pa.judge("Baby Formula", {"ingredients", "nutritionFactsLabel"})
    assert a.verdict == pa.BLOCK
    assert "FDA 食品设施注册" in a.certs or "FDA 营养标签合规" in a.certs


def test_ingredients_without_food_cosignals_is_cosmetics_not_food():
    """`ingredients` 食品和化妆品都要填,光看它分不出是哪一类,而两类的合规主体
    完全不同。实见 `3-in-1 Shampoo, Conditioner & Body Washes` 顶层必填带
    ingredients —— 现表把它标成「FDA 食品设施注册」,其实该是 MoCRA。
    **结论都是否(都要美国主体),但名字写错了人就没法照着去办证。**"""
    a = pa.judge("3-in-1 Shampoo", {"ingredients", "labelImage"})
    assert a.verdict == pa.BLOCK
    assert any("MoCRA" in c for c in a.certs)
    assert not any("食品" in c for c in a.certs)


def test_conditional_required_caps_at_eval():
    """`allOf.then.required` 只在特定取值下才要 —— 它说明"这个 PT 可能涉及",
    不说明"这个 PT 就是"。实见洗发水的 spec 里带着儿童产品证书字段,洗发水
    显然不是儿童产品;不封顶的话整片个护会被判成儿童产品。"""
    a = pa.judge("3-in-1 Shampoo", {"labelImage"},
                 conditional={"children_product_certificate_document_reference_id"})
    assert a.verdict == pa.EVAL
    assert "条件必填" in a.reasons[0]


def test_properties_only_fields_are_never_evidence():
    """只躺在 properties 里的字段一律不算判据 —— `certification_type` 实见
    几乎每个 PT 都有,当判据会让全表变"否"。judge() 只收 required 与 conditional,
    压根不接受 properties,这条用例钉死这个签名。"""
    a = pa.judge("X", set(), conditional=set())
    assert a.verdict == pa.OK and a.certs == []


def test_spec_cert_document_fields_are_used_directly():
    """spec 自带认证文档字段,比按小零件警告猜准得多。"""
    a = pa.judge("Toy Blocks",
                 {"children_product_test_report_document_reference_id",
                  "general_certificate_of_conformity_document_reference_id"})
    assert a.verdict == pa.EVAL
    assert "第三方 CPSC 测试报告" in a.certs and "GCC 通用符合证书" in a.certs


def test_nrtl_is_eval_not_block():
    """NRTL(UL/ETL/CSA)花钱能做,是"要合规投入",不是"进不去"。"""
    assert pa.judge("Power Strips", {"has_nrtl_listing_certification"}).verdict == pa.EVAL


def test_label_only_fields_stay_ok():
    """纯标签/声明类不该拖低档位 —— 否则全表都是"否",白名单就没意义了。"""
    a = pa.judge("Candle Holders",
                 {"isProp65WarningRequired", "labelImage", "has_written_warranty"})
    assert a.verdict == pa.OK


def test_prop65_alone_never_blocks_anything():
    """`isProp65WarningRequired` **6942 个 PT 全都有**,零区分度。
    哪天有人把它当硬门槛,整张白名单会一次性全变成"否"。"""
    assert pa.judge("X", {"isProp65WarningRequired"}).verdict == pa.OK


def test_worst_tier_wins():
    a = pa.judge("Pet Food Bowl Set",
                 {"labelImage", "has_nrtl_listing_certification", "petFoodForm"})
    assert a.verdict == pa.BLOCK          # 三档取最严


def test_age_group_alone_is_not_a_child_product():
    """`ageGroup` 本身只是人群标签(成人用品也填),取值落在儿童段才算 CPSIA ——
    只看字段名会把成人服饰、老人助行器全判成儿童产品。"""
    assert pa.judge("Adult Slippers", {"ageGroup"}, age_values=["adult"]).verdict == pa.OK
    assert pa.judge("Toy Blocks", {"ageGroup"}, age_values=["toddler"]).verdict == pa.EVAL


def test_policy_prohibited_beats_fields():
    """政策说完全禁售就是禁售,不看字段 —— 字段只说明"要什么材料",
    政策说的是"沃尔玛压根不让卖"。"""
    a = pa.judge("Distillation Apparatus", set(), policy="Alcohol",
                 policy_status="完全禁售(含酒精食品/蒸馏设备)")
    assert a.verdict == pa.BLOCK and "Alcohol" in a.reasons[0]


def test_no_fields_no_certs_is_ok():
    a = pa.judge("Bookends", set())
    assert a.verdict == pa.OK and a.certs == [] and a.fields_seen == 0


# ── ageGroup 取值:只有落在儿童段才算儿童产品 ────────────────────────────

def test_age_values_are_read_from_enum_not_field_name():
    """`ageGroup` 字段名人人都有,**取值**才说明是不是儿童产品。
    只看字段名会把成人拖鞋、老人助行器整批判成 CPSIA。"""
    spec = {"properties": {"MPItem": {"properties": {
        "ageGroup": {"items": {"enum": ["Infant", "Toddler"]}}}}}}
    assert set(_age_values(spec)) == {"Infant", "Toddler"}
    assert pa.judge("X", {"ageGroup"}, age_values=_age_values(spec)).verdict == pa.EVAL


def test_age_values_adult_only_stays_ok():
    spec = {"properties": {"ageGroup": {"enum": ["Adult", "Senior"]}}}
    assert pa.judge("Y", {"ageGroup"}, age_values=_age_values(spec)).verdict == pa.OK


def test_age_values_missing_is_empty_not_crash():
    assert _age_values({"properties": {"brand": {"type": "string"}}}) == []
    assert _age_values([]) == []


# ── 字段总数:属性名递归收集(对应旧表「字段总数」列)────────────────────

def test_all_fields_collects_property_names():
    spec = {"properties": {"a": {"properties": {"b": {}, "c": {}}}, "d": {}}}
    assert pa.all_fields(spec) == {"a", "b", "c", "d"}


# ── 与现表比对:自由文本取值要能归到三档 ──────────────────────────────────

def test_old_bucket_reads_the_35_freetext_variants():
    """现表「中国卖家可做」实测 35 种取值,但都以 是/需评估/否 开头
    ——「否(上架记录回测,BIZ-CN触发5次)」这类只是把证据写进了值里。
    归不到档的返回空串(当作"现表没有结论"),不许猜成"是"。"""
    from workflows.pt_spec_sync import _old_bucket
    assert _old_bucket("是") == pa.OK
    assert _old_bucket("需评估 (要合规投入)") == pa.EVAL
    assert _old_bucket("否 (中国卖家进不去)") == pa.BLOCK
    assert _old_bucket("否（上架记录回测，BIZ-CN触发5次）") == pa.BLOCK
    assert _old_bucket("") == "" and _old_bucket("待定") == ""


# ── 样板字段:哪儿都有 = 没有区分力,实测覆盖率说了算 ──────────────────────

def test_boilerplate_is_measured_not_hand_listed():
    """2026-08-20 生产实跑:按"条件必填也算判据"跑出来 6951 个 PT 里 6494 个
    判「需评估」(93%),白名单直接作废。查下来是儿童产品证书那组字段挂在
    几乎每个 PT 的 allOf 里(「若声明是儿童产品则要 CPC」的通用样板)。

    这和 `isProp65WarningRequired` 是同一类东西 —— 所以不再靠人眼一个个认,
    **覆盖率超线就自动剔除**,并且要报得出是哪些(静默剔 = 判定悄悄少一条依据)。
    """
    total = 1000
    top = {"ingredients": 30, "isProp65WarningRequired": 1000}
    cond = {"children_product_certificate_document_reference_id": 940,
            "hasBatteries": 120}
    bp_top, bp_cond, detail = pa.find_boilerplate(top, cond, total)
    assert "isProp65WarningRequired" in bp_top          # 顶层 100%
    assert "ingredients" not in bp_top                  # 3%,有区分力
    assert "children_product_certificate_document_reference_id" in bp_cond   # 条件 94%
    assert "hasBatteries" not in bp_cond                # 条件 12%
    assert any(f == "ingredients" and nt == 30 for f, nt, _, _ in detail)


def test_boilerplate_fields_are_dropped_from_evidence():
    """被判样板的字段进不了判据 —— 否则整表一个颜色。"""
    a = pa.judge("X", {"ingredients"},
                 conditional={"children_product_certificate_document_reference_id"},
                 boilerplate_cond={"children_product_certificate_document_reference_id"})
    assert not any("CPC" in c for c in a.certs)
    assert a.verdict == pa.BLOCK          # ingredients 不是样板,照旧生效


def test_boilerplate_only_covers_signal_fields():
    """只对判定表里的字段算覆盖率 —— 其余字段本来就不参与判定。"""
    _, _, detail = pa.find_boilerplate({"productName": 6951}, {}, 6951)
    assert not any(f == "productName" for f, _, _, _ in detail)


def test_evidence_kind_separates_empirical_from_inferred():
    """现表判否的行不是同一种东西:60 多个是**上架回测实证**(真被拒过 N 次),
    245 个是沃尔玛政策,366 个只是从准入状态推的。所有者定「允许翻」,所以不拦,
    但复核表要能一列筛出来 —— 翻实证那批的代价和翻推断那批不是一回事。"""
    from workflows.pt_spec_sync import _evidence_kind
    assert _evidence_kind("否（上架记录回测，BIZ-CN触发26个SKU）") == "实证:上架被拒过"
    assert _evidence_kind("否 (Walmart 禁售)") == "沃尔玛政策禁售"
    assert _evidence_kind("否（Walmart 禁售）") == "沃尔玛政策禁售"
    assert _evidence_kind("否 (中国卖家进不去)") == "推断:准入状态需审批"
    assert _evidence_kind("是") == "" and _evidence_kind("需评估 (要合规投入)") == ""


# ── 「必填」以生产上架链的定义为准,不许对齐旧表的错数字 ────────────────────

def test_required_is_pt_top_level_only_matching_the_sheet():
    """「必填字段清单」= **PT spec 顶层 required**,与飞书现表逐字段核过。

    2026-08-20 实测对账(现表 6942 行 vs 本地 spec 6951 个 PT):
      ingredients 133/133、foodForm 163/163、food_condition 297/297、
      ageGroup 919/919、labelImage 628/628、activeIngredients 19/19、
      batterySize 13/13、minimumRecommendedAge 60/60 …逐个对上;
    而条件必填那批(prop65WarningText 6951、children_product_* 6941、
    nrtl_information 958)**现表一条都没收** —— 现表当年就是按顶层 required
    生成的,判断是对的。
    `3-in-1 Shampoo` 现表 14 项 / 字段总数 62,与本口径一字不差。

    两条不许再犯的:
      · 不并 Orderable(sku/price/quantity 是任何商品都要的信封字段,零信息量);
      · 更不能递归(会混进 properties-only 与条件样板,清单一多,人照着去准备
        材料就白办证 —— 做洗发水的按那种清单要去办儿童产品符合证书)。
    """
    import inspect
    from workflows import pt_spec_sync
    wf = inspect.getsource(pt_spec_sync.run)
    assert "req = top_req" in wf                      # 就是 PT 顶层
    assert "extract_required(spec)" not in wf         # 不许递归
    assert "common_req" not in wf                     # 不许并 Orderable


def test_conditional_required_is_not_static_required():
    """条件必填只在取值触发时才要 —— 把它算进"必填字段清单",人照着去准备材料
    会白准备一堆(洗发水按那张清单要去办儿童产品证书)。"""
    from services import mp_conform
    spec = {"required": ["productName"],
            "allOf": [{"if": {"properties": {"isChildProduct": {"enum": ["Yes"]}}},
                       "then": {"required": ["children_product_certificate_document_reference_id"]}}]}
    assert mp_conform._required(spec) == {"productName"}
    # 取值没触发 → 条件必填不进来
    assert mp_conform.resolve_conditional_required(spec, {"isChildProduct": "No"}) == set()


def test_sheet_diff_reports_both_directions(tmp_path):
    """现表与 spec 的差异要**双向**报:spec 多的可能是换版新增,现表多的可能是
    当年多收了。只报一边就会把"我的读法可能错"这一半藏起来。

    起因(所有者 2026-08-20):「你要辩证的看待我给你的资料,和你判断的如何
    获取必填字段」—— 上一轮我把差异一口咬定成"沃尔玛后来设成必填了",
    但至少三种解释都成立,差异形状才能分辨。
    """
    from workflows.pt_spec_sync import _sheet_diff
    csv_path = tmp_path / "sheet.csv"
    csv_path.write_text(
        "Walmart Product Type,必填字段清单\n"
        "A,productName | brand | oldOnlyField\n"
        "B,productName | brand\n", encoding="utf-8")
    cache = {"A": ({}, {"productName", "brand", "specOnlyField"}, set()),
             "B": ({}, {"productName", "brand"}, set())}
    out = "\n".join(_sheet_diff(str(csv_path), cache))
    assert "完全一致 1 个" in out and "有差异 1 个" in out
    assert "specOnlyField" in out and "oldOnlyField" in out
    assert "spec 有、现表没有" in out and "现表有、spec 没有" in out


def test_sheet_diff_missing_file_says_so():
    from workflows.pt_spec_sync import _sheet_diff
    assert "不存在" in "\n".join(_sheet_diff("/nope/nope.csv", {}))


def test_explain_reports_both_scopes():
    """explain 必须同时给「类目 Visible」和「完整商品」两个口径。

    所有者自查:类目 Visible 62/14、官方完整商品 85/19、本地版本 86/19。
    两个都对,回答的是不同问题 —— 只报一个,人就会拿类目口径去核提交口径
    (或反过来),然后以为对方错了。我自己就这么来回错了三轮。
    """
    import inspect
    from workflows import pt_spec_sync
    src = inspect.getsource(pt_spec_sync._explain)
    assert "类目 Visible" in src and "完整商品" in src
    assert "Orderable 顶层必填" in src        # 那 5 个要列名字,便于逐个核


# ── 换版对账:能指着另一份 spec 跑,但绝不能让上架链跟着漂 ──────────────────

def test_spec_dir_override_and_restore(tmp_path):
    """新版 spec 拉下来后,要先量出"新版加了哪些必填、mapper 给不出哪些",
    再决定改不改 registry 的版本串 —— 切完再发现上架量掉是**静悄悄掉的**
    (validate 不过就不提交,省 UPC 和配额,设计如此)。

    所以对账要能指着新目录跑而不动 registry;跑完必须恢复,否则同进程后续
    的上架链会拿新版 spec 去过旧版 header 的校验。
    """
    import json
    from services import pt_spec
    d = tmp_path / "newspec"
    d.mkdir()
    (d / "_pt_index.json").write_text(json.dumps({"Widgets": "w.json"}), encoding="utf-8")
    (d / "_orderable.json").write_text(json.dumps({"required": ["sku"]}), encoding="utf-8")
    (d / "w.json").write_text(json.dumps({"required": ["a"], "properties": {"a": {}}}),
                              encoding="utf-8")
    try:
        pt_spec.use_spec_dir(str(d))
        assert pt_spec.known_pts() == {"Widgets"}
        assert pt_spec.load_pt("Widgets")["required"] == ["a"]
    finally:
        pt_spec.use_spec_dir(None)
    assert pt_spec._OVERRIDE_DIR is None


def test_spec_dir_override_rejects_incomplete_dir(tmp_path):
    """目录里没有 _pt_index.json 就报错 —— 静默回落到旧版会让"对账通过"
    变成"其实根本没换过版"。"""
    import pytest as _pytest
    from services import pt_spec
    try:
        pt_spec.use_spec_dir(str(tmp_path))
        with _pytest.raises(FileNotFoundError, match="_pt_index.json"):
            pt_spec.known_pts()
    finally:
        pt_spec.use_spec_dir(None)
