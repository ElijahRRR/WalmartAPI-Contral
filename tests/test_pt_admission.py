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
    """要填配料表 = 食品 = 要 FDA 食品设施注册 + 美国代理人,中国搬运拿不到 → 否。

    这条正是当年填错的那个:`Baby Foods & Formula` 整组被套上 CPSIA
    (儿童产品符合证书),而婴儿配方奶真正要的是 FDA 注册。
    """
    a = pa.judge("Baby Formula", {"ingredients", "nutritionFactsLabel"})
    assert a.verdict == pa.BLOCK
    assert any("FDA" in c for c in a.certs)
    assert a.reasons and "ingredients" in a.reasons[0]


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


# ── spec 只许收紧,不许翻掉 spec 看不见的证据 ────────────────────────────

def test_locked_marks_policy_and_empirical_reasons():
    """现表的「否」有三类来源,**只有一类是 spec 看得见的**:

      否(中国卖家进不去)      ← 准入状态=需Walmart审批,沃尔玛侧事实
      否(Walmart 禁售)        ← **政策**,spec 里没有这个信息
      否(上架记录回测,BIZ-CN触发5次) ← **实证:真上架被拒过**,比任何推断都硬

    后两类不许被 spec 判定翻案 —— spec 只说明"要什么材料",它既不知道政策
    禁不禁,更不知道这个 PT 真上架时被拒过多少次。生产实跑里「否 → 是」有
    384 条,不锁的话这批会被一次性放行。
    """
    from workflows.pt_spec_sync import _locked
    assert _locked("否 (Walmart 禁售)") == "沃尔玛政策禁售"
    assert _locked("否（Walmart 禁售）") == "沃尔玛政策禁售"
    assert _locked("否（上架记录回测，BIZ-CN触发5次）") == "实证:上架回测被拒"
    assert _locked("否（上架记录回测，BIZ-CN触发3个SKU）") == "实证:上架回测被拒"
    # 这一类是从准入状态推的,spec 可以参与讨论 → 不锁
    assert _locked("否 (中国卖家进不去)") == ""
    # 现表说可做的行,压根不进锁定逻辑
    assert _locked("是") == "" and _locked("需评估 (要合规投入)") == ""
