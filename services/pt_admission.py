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

# spec 必填字段 → (认证要求, 档位, 依据)。字段名取自官方 spec,不是猜的。
FIELD_CERTS: list[tuple[str, str, str, str]] = [
    ("has_nrtl_listing_certification", "NRTL 认证(UL/ETL/CSA)", EVAL,
     "官方 spec 要求申报 NRTL 挂牌;实验室测试花钱能做,但要走认证机构"),
    ("activeIngredients", "FDA/EPA 活性成分注册", BLOCK,
     "有活性成分 = 药品/农药/消杀,要 FDA 或 EPA 主体注册"),
    ("ingredients", "FDA 食品设施注册", BLOCK,
     "官方要求申报配料表 = 食品/入口物,要 FDA 食品设施注册 + 美国代理人"),
    ("ingredientListImage", "FDA 食品设施注册", BLOCK, "同上(配料表图)"),
    ("nutritionFactsLabel", "FDA 营养标签合规", BLOCK, "同上(营养成分表)"),
    ("food_condition", "FDA 食品设施注册", BLOCK, "食品状态字段 = 食品"),
    ("foodForm", "FDA 食品设施注册", BLOCK, "食品形态字段 = 食品"),
    ("is_food_component_monetary_value_over_50", "FDA 食品设施注册", BLOCK, "含食品成分"),
    ("petFoodForm", "FDA 宠物食品设施注册 + AAFCO", BLOCK, "宠物食品形态字段"),
    ("pet_food_condition", "FDA 宠物食品设施注册 + AAFCO", BLOCK, "宠物食品状态字段"),
    ("smallPartsWarnings", "CPSIA GCC/CPC + 小零件警告", EVAL,
     "官方要求申报小零件警告 = 儿童产品,要第三方 CPSC 实验室测试报告"),
    ("minimumRecommendedAge", "CPSIA GCC/CPC", EVAL, "有推荐年龄 = 儿童产品"),
    ("maximumRecommendedAge", "CPSIA GCC/CPC", EVAL, "同上"),
    ("hasBatteries", "UN 38.3 运输测试", EVAL, "含电池,锂电要 UN 38.3"),
    ("batteriesRequired", "UN 38.3 运输测试", EVAL, "同上"),
    ("batterySize", "UN 38.3 运输测试", EVAL, "同上"),
    ("state_chemical_disclosure", "州化学品披露", OK, "填报即可,无需第三方"),
    ("isProp65WarningRequired", "Prop 65 警告标签", OK,
     "**6942 个 PT 全都有这个字段**,零区分度,只当标签项"),
    ("labelImage", "标签图", OK, "上传标签图即可"),
    ("has_written_warranty", "书面保修声明", OK, "声明项"),
]
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


def judge(product_type: str, required: set, *, age_values: list | None = None,
          policy: str = "", policy_status: str = "") -> Admission:
    """输入:PT + 必填字段集(+ 政策)→ 输出:Admission(认证清单 + 三档结论)。

    档位取**最严**的一条:任一 BLOCK ⇒ 否;否则任一 EVAL ⇒ 需评估;否则是。
    政策优先级更高:政策判「完全禁售」直接否,不看字段(政策是沃尔玛明说不让卖,
    字段只说明要什么材料)。
    """
    certs, reasons, worst = [], [], OK
    order = {OK: 0, EVAL: 1, BLOCK: 2}
    for fname, cert, tier, why in FIELD_CERTS:
        if fname in required:
            if cert not in certs:
                certs.append(cert)
                reasons.append(f"spec 必填 `{fname}` → {cert}({why})")
            if order[tier] > order[worst]:
                worst = tier
    if AGE_FIELD in required and any(_CHILD_AGE.search(str(v) or "")
                                     for v in (age_values or [])):
        certs.append("CPSIA GCC/CPC")
        reasons.append(f"spec 必填 `{AGE_FIELD}` 且取值含儿童段 → CPSIA")
        worst = EVAL if order[worst] < order[EVAL] else worst
    if policy_status and ("完全禁售" in policy_status or policy_status.strip() == "禁售"):
        worst = BLOCK
        reasons.insert(0, f"沃尔玛政策「{policy}」:{policy_status} —— 政策禁售优先于字段")
    return Admission(product_type=product_type, certs=certs, verdict=worst,
                     reasons=reasons, policy=policy, fields_seen=len(required))


__all__ = ["Admission", "FIELD_CERTS", "BLOCK", "EVAL", "OK",
           "extract_required", "judge"]
