"""沃尔玛 PT 准入判定:从**官方 spec 必填字段 + 37/46 条禁售政策**推出
「这个 PT 要什么认证 / 中国搬运卖家能不能做」。纯函数,零 DB、零网络。

为什么重写而不是修飞书那张表(所有者 2026-08-20 核查结论):
  · 「必需认证」列 45% 是空的,空到底是"不需要"还是"没填",表本身答不了;
  · 642 个 PTG 分组里 294 组(46%)**组内认证一字不差** —— 说明当年是按 PTG
    批量套的,不是逐 PT 判的。实见 `Baby Foods & Formula` 整组套 CPSIA,
    于是**婴儿配方奶标成了儿童产品符合证书**(它要的是 FDA 婴幼儿配方注册);
    `Pet Bowls`(宠物碗)被标「FDA 设施注册 + AAFCO」——那是宠物**食品**的要求;
  · 382 个 PT 要硬认证却标「是」(可做、无需合规投入)—— 上架后被罚回来的正是这批。

新口径:**spec 必填字段是客观的**,官方要求填 `has_nrtl_listing_certification`
就是要 NRTL 认证,要求填 `ingredients` 就是食品。由字段推认证,由认证推能不能做,
每一条都能溯源到"哪个字段 / 哪条政策"。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("services.pt_admission")

# 三档口径(所有者 2026-08-20 定):
#   BLOCK  要**主体资质**(工厂注册/器械许可/农药登记)——中国搬运卖家拿不到
#   EVAL   花钱**买得到报告**(第三方实验室测试、符合性证书)——要合规投入
#   OK     纯**标签/声明**类(填个字段、印张标签就行)
BLOCK, EVAL, OK = "否", "需评估", "是"

# spec 字段 → (认证要求, 档位, 依据)。字段名取自官方 spec,不是猜的。
#
# ⚠ **字段住在哪一层决定它算不算判据**(2026-08-20 拿本地 spec 实测定的口径):
#   顶层 required        → 硬判据。沃尔玛对这个 PT 无条件要这个东西
#   allOf.then.required  → **条件必填**,只在某个取值下才要(如 isChildProduct=Yes)。
#                          它说明"这个 PT 可能涉及",不说明"这个 PT 就是"——
#                          实见洗发水的 spec 里带着
#                          `children_product_certificate_document_reference_id`,
#                          洗发水显然不是儿童产品。所以条件必填只降到"需评估",
#                          具体要不要是**产品级**的事,归 L2/L3 判,不在 PT 级下结论
#   仅 properties        → 躺在那儿备用,**一律不算**(`certification_type` 实见
#                          只是顶层属性,几乎每个 PT 都有,当判据会全表变"否")
FIELD_CERTS: list[tuple[str, str, str, str]] = [
    # ── 食品/入口物:要 FDA 食品设施注册 + 美国代理人,中国搬运拿不到 ──
    ("food_condition", "FDA 食品设施注册", BLOCK, "食品状态字段 = 食品"),
    ("foodForm", "FDA 食品设施注册", BLOCK, "食品形态字段 = 食品"),
    ("nutritionFactsLabel", "FDA 营养标签合规", BLOCK, "营养成分表 = 食品"),
    ("ingredientListImage", "FDA 食品设施注册", BLOCK, "配料表图 = 入口物"),
    ("is_food_component_monetary_value_over_50", "FDA 食品设施注册", BLOCK, "含食品成分"),
    ("petFoodForm", "FDA 宠物食品设施注册 + AAFCO", BLOCK, "宠物食品形态字段"),
    ("pet_food_condition", "FDA 宠物食品设施注册 + AAFCO", BLOCK, "宠物食品状态字段"),
    # ── 药品/农药/消杀 ──
    ("activeIngredients", "FDA/EPA 活性成分注册", BLOCK,
     "有活性成分 = 药品/农药/消杀,要 FDA 或 EPA 主体注册"),
    ("drugFacts", "FDA OTC 专论合规", BLOCK, "药品说明字段"),
    # ── 带电/电池 ──
    ("has_nrtl_listing_certification", "NRTL 认证(UL/ETL/CSA)", EVAL,
     "官方要求申报 NRTL 挂牌;实验室测试花钱能做,但要走认证机构"),
    ("nrtl_information", "NRTL 认证(UL/ETL/CSA)", EVAL, "同上"),
    ("hasBatteries", "UN 38.3 运输测试", EVAL, "含电池,锂电要 UN 38.3"),
    ("batteriesRequired", "UN 38.3 运输测试", EVAL, "同上"),
    ("batterySize", "UN 38.3 运输测试", EVAL, "同上"),
    # ── 儿童产品:spec 自带的认证文档字段,比按小零件警告猜准得多 ──
    ("children_product_certificate_document_reference_id", "CPSIA CPC 证书", EVAL,
     "spec 直接点名要儿童产品符合证书文档"),
    ("children_product_test_report_document_reference_id", "第三方 CPSC 测试报告", EVAL,
     "spec 直接点名要儿童产品测试报告"),
    ("general_certificate_of_conformity_document_reference_id", "GCC 通用符合证书", EVAL,
     "spec 直接点名要 GCC 文档"),
    ("smallPartsWarnings", "小零件警告(≤3 岁)", EVAL, "小零件警告 = 儿童可及"),
    ("minimumRecommendedAge", "CPSIA GCC/CPC", EVAL, "有推荐年龄 = 可能儿童产品"),
    ("maximumRecommendedAge", "CPSIA GCC/CPC", EVAL, "同上"),
    # ── 纯标签/声明:填个字段、印张标签就行,不拖低档位 ──
    ("state_chemical_disclosure", "州化学品披露", OK, "填报即可,无需第三方"),
    ("isProp65WarningRequired", "Prop 65 警告标签", OK,
     "**每个 PT 都有这个字段**,零区分度,只当标签项"),
    ("prop65WarningText", "Prop 65 警告语", OK, "同上"),
    ("labelImage", "标签图", OK, "上传标签图即可"),
    ("labelImageContains", "标签内容申报", OK, "申报项"),
    ("has_written_warranty", "书面保修声明", OK, "声明项"),
]

# `ingredients` 单列:食品与化妆品/个护**都要填**,光看它分不出是哪一类,
# 而两类的合规主体完全不同(FDA 食品设施注册 vs MoCRA + 美国 Responsible Person)。
# 实见 `3-in-1 Shampoo` 顶层必填带 ingredients —— 现表把它标成了"FDA 食品设施
# 注册",其实该是 MoCRA。**结论都是"否"(都要美国主体),只是名字不能写错**。
INGREDIENTS_FIELD = "ingredients"
_FOOD_COSIGNALS = {"food_condition", "foodForm", "nutritionFactsLabel",
                   "ingredientListImage", "petFoodForm", "pet_food_condition"}

# ageGroup 单独处理:它本身只是人群标签,只有取值落在儿童段才意味着 CPSIA
AGE_FIELD = "ageGroup"
_CHILD_AGE = re.compile(r"infant|toddler|kid|child|baby|youth", re.I)


@dataclass(frozen=True)
class Admission:
    """一个 PT 的准入判定(每条都带依据,可溯源)。"""

    product_type: str
    certs: list = field(default_factory=list)      # ["NRTL 认证(UL/ETL/CSA)", ...]
    verdict: str = OK                               # 是 / 需评估 / 否
    reasons: list = field(default_factory=list)     # 逐条依据
    policy: str = ""                                # 命中的沃尔玛政策类目
    fields_seen: int = 0


def extract_required(node, out: set | None = None) -> set:
    """输入:spec JSON(任意层)→ 输出:所有 `required` 数组里的字段名。

    官方 spec 是 JSON Schema,必填字段散在多层 `required` 里(MPItem → Orderable
    → Visible → <PT>)。**递归收全**而不是只取顶层:只取顶层会把 PT 专属的合规
    字段(配料表/电池/小零件)整批漏掉,而那正是判定要用的东西。
    """
    if out is None:
        out = set()
    if isinstance(node, dict):
        req = node.get("required")
        if isinstance(req, list):
            out.update(str(x) for x in req if isinstance(x, (str, int)))
        for v in node.values():
            extract_required(v, out)
    elif isinstance(node, list):
        for v in node:
            extract_required(v, out)
    return out


def all_fields(node, out: set | None = None) -> set:
    """输入:spec JSON → 输出:出现过的全部属性名(不只必填,对应旧表「字段总数」)。"""
    if out is None:
        out = set()
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            out.update(str(k) for k in props)
        for v in node.values():
            all_fields(v, out)
    elif isinstance(node, list):
        for v in node:
            all_fields(v, out)
    return out


# 覆盖率超过这个比例的字段 = **样板块**,不算判据。
# 起因(2026-08-20 生产实跑):按"条件必填也算判据"跑出来 6951 个 PT 里
# 6494 个判「需评估」(93%),白名单直接作废。查下来是
# `children_product_certificate_document_reference_id` 那组儿童产品证书字段
# 挂在几乎每个 PT 的 allOf 里(「若声明是儿童产品则要 CPC」的通用样板),
# 和 `isProp65WarningRequired` 是同一类东西。
# **不再靠人眼一个个认**:哪个字段普遍到这个份上,它就没有区分力,实测说了算。
BOILERPLATE_TOP = 0.90       # 顶层必填命中率超过 90% ⇒ 样板
BOILERPLATE_COND = 0.50      # 条件必填命中率超过 50% ⇒ 样板


def find_boilerplate(top_counts: dict, cond_counts: dict, total: int
                     ) -> tuple[set, set, list]:
    """输入:字段→命中 PT 数(顶层/条件)+ PT 总数 → 输出:(顶层样板集, 条件样板集, 明细)。

    只对判定表里出现过的字段计算 —— 其余字段本来就不参与判定,算了也没用。
    明细给调用方**报数**用:样板被静默剔掉 = 判定悄悄少了一条依据,
    正是本仓最忌讳的那种"看起来一切正常"。
    """
    signals = {f for f, _, _, _ in FIELD_CERTS} | {INGREDIENTS_FIELD, AGE_FIELD}
    top_bp, cond_bp, detail = set(), set(), []
    for f in sorted(signals):
        nt, nc = top_counts.get(f, 0), cond_counts.get(f, 0)
        rt = nt / total if total else 0.0
        rc = nc / total if total else 0.0
        tag = ""
        if rt > BOILERPLATE_TOP:
            top_bp.add(f)
            tag = f"顶层命中 {rt:.0%} → 样板,不算判据"
        if rc > BOILERPLATE_COND:
            cond_bp.add(f)
            tag = (tag + ";" if tag else "") + f"条件命中 {rc:.0%} → 样板,不算判据"
        detail.append((f, nt, nc, tag))
    return top_bp, cond_bp, detail


def judge(product_type: str, required: set, *, conditional: set | None = None,
          age_values: list | None = None, category: str = "",
          policy: str = "", policy_status: str = "",
          boilerplate_top: set | None = None,
          boilerplate_cond: set | None = None) -> Admission:
    """输入:PT + **顶层必填**字段集(+ 条件必填 / ageGroup 取值 / 类目 / 政策)
    → 输出:Admission(认证清单 + 三档结论 + 逐条依据)。

    档位取**最严**的一条:任一 BLOCK ⇒ 否;否则任一 EVAL ⇒ 需评估;否则是。

    `conditional`(allOf.then.required)里的命中**一律只降到"需评估"**,再硬的
    认证也不升到"否" —— 条件必填只说明"这个 PT 可能涉及",不说明"这个 PT 就是"
    (洗发水的 spec 里也带着儿童产品证书字段)。具体要不要是产品级的事,归 L2/L3。

    政策优先级最高:政策判「完全禁售」直接否,不看字段(政策是沃尔玛明说不让卖,
    字段只说明要什么材料)。
    """
    order = {OK: 0, EVAL: 1, BLOCK: 2}
    certs, reasons, worst = [], [], OK
    # 样板字段直接从两个集合里剔掉:哪儿都有 = 没有区分力
    required = set(required) - (boilerplate_top or set())
    cond = (conditional or set()) - (boilerplate_cond or set())

    def _add(cert: str, tier: str, why: str, weak: bool = False) -> None:
        nonlocal worst
        if weak and order[tier] > order[EVAL]:
            tier = EVAL          # 条件必填封顶在"需评估"
        if cert not in certs:
            certs.append(cert)
            reasons.append(why)
        if order[tier] > order[worst]:
            worst = tier

    for fname, cert, tier, why in FIELD_CERTS:
        if fname in required:
            _add(cert, tier, f"spec **顶层必填** `{fname}` → {cert}({why})")
        elif fname in cond:
            _add(cert, tier, f"spec **条件必填** `{fname}` → {cert}"
                             f"(只在特定取值下才要,封顶「需评估」)", weak=True)

    # ingredients:食品与化妆品/个护都要填,靠共现字段分流;两类都要美国主体 ⇒ 都是否
    if INGREDIENTS_FIELD in required:
        if required & _FOOD_COSIGNALS:
            _add("FDA 食品设施注册", BLOCK,
                 "spec **顶层必填** `ingredients` + 食品类共现字段 → 食品,"
                 "要 FDA 食品设施注册 + 美国代理人")
        else:
            _add("FDA MoCRA + 美国 Responsible Person", BLOCK,
                 "spec **顶层必填** `ingredients` 但无食品类共现字段 → 化妆品/个护,"
                 "要 MoCRA 工厂注册 + 美国 Responsible Person(**不是**食品设施注册)")

    if AGE_FIELD in required and any(_CHILD_AGE.search(str(v) or "")
                                     for v in (age_values or [])):
        _add("CPSIA GCC/CPC", EVAL,
             f"spec 顶层必填 `{AGE_FIELD}` 且枚举含儿童段 → 儿童产品")

    if policy_status and ("完全禁售" in policy_status or policy_status.strip() == "禁售"):
        worst = BLOCK
        reasons.insert(0, f"沃尔玛政策「{policy}」:{policy_status} —— 政策禁售优先于字段")
    return Admission(product_type=product_type, certs=certs, verdict=worst,
                     reasons=reasons, policy=policy, fields_seen=len(required))


__all__ = ["Admission", "FIELD_CERTS", "INGREDIENTS_FIELD", "BLOCK",
           "EVAL", "OK", "all_fields", "extract_required", "judge"]
