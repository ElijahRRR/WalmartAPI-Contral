"""L2 规则引擎(R1/R3/R4/R5/R7/R8):两条硬拒 + 四条软证据,纯函数,自己不碰数据库。

移植自旧仓 `pipelines/l2_rules.py`(并入 `nrtl_classifier.py` /
`nice_class_mapper.py`)。外部数据(pt_meta / pt_spec / 黑名单自动机 / yaml 加载
结果 / USPTO 连接)一律由调用方经 `ctx` 注入(见 services/audit_rules.AuditContext);
本模块不连库、不读环境变量,yaml 路径只经 registry.paths.audit_seed_file 取(铁律 3)。

执行模型:起始 100 分 → 先叠加 l1.hits 的 penalty(**只加分**,不把 L1 hit 复制进
L2 hits)→ 六条规则**按固定顺序全跑、不短路**(R1 命中 -100 后 R3 照跑,分数可叠到
-200,这是有意的:detail 要收全证据)→ 下界保护 -1000。判定在 L2Result.verdict:
score_final < 60 → reject;分数够但 R1 报了"判不了" → pending;否则 pass。
软规则(R3b/R3c/R4/R5/R7/R8)penalty 全为 0,**不参与累积**;结论只由 R1/R3a 的
-100 决定,软 hit 是给 L3 准备的证据账本。

两条硬规则共用一道闸:上游已判死(任一 l1.hit penalty<0,如出版物硬禁)→ 直接放行,
既不重复扣分也不会把确定的拒降级成 pending。

**2026-08-20 的三件事(所有者定稿,不要往回改):**
  1. **删 R0 与 R2。** R0 是代码里 8 个 walmart_category 硬禁,R2 是 yaml 里 18 条
     禁售大类,它们和 R1 的类目白名单讲的是同一件事 —— 一个类目能不能做。
     三份清单各自维护,改一处漏两处而且**不报错**。前置条件已完成:白名单先按
     官方 spec 重建补齐(pt_spec_sync),再删这两份黑名单,中间无真空期。
     现在类目层面只有 R1 一处判据。
  2. **R1 的两条静默放行改判 pending。** PT 未知 / PT 不在 walmart_pt_meta 时
     原先 `return []`,等于"查不到 = 没问题"直接 100 分放行。删掉 R0/R2 之后
     这个洞再没有别的清单兜底,必须堵。判不了 ≠ 判过了。
  3. **修掉三条"看着在跑其实没跑"的匹配缺陷**(详见各自 docstring):
     R3 裸子串(`"ul" in "fda regulation"` → 打成 UL 认证 -100 硬拒)、
     R4 中文紧邻不算词边界(中文品牌一个都拦不住)、
     R7 只命中软短语时把证据整条丢掉(L3 收不到)。

其余已知偏差(**有意保留**,动机写在各自 docstring 里):
  - R3 的软词抑制实际只有「ansi 被 NSF/ANSI 61 抑制」一条生效(其余软词没有
    对应的硬 label,不是缺陷);
  - R5 每个 mark 最多留 2 条 goods_samples 且各截断到 80 字符(提示词预算)。

不迁(旧仓死代码,移植规格 §6):R9 `trademark_symbol_in_title`(®/™ 判定已归 Phase0)、
R6 `blacklist_compatible_for`(2026-04 删除,误伤率 90%)、`HARD_CERT_FIELDS` frozenset
(真值在 sync 侧,已固化成 walmart_pt_spec.has_real_cert 列)、旧 37000 条 regex 版
黑名单 `_compile_blacklist_patterns`。

R4/R5/R7/R8 的 penalty 一律 **0**(旧注释写 -15 / -10 / -20 / -30)。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import yaml

from registry import paths
from services.audit_models import L2Result, RuleHit
from services.audit_stopwords import is_stopword

if TYPE_CHECKING:  # 仅供阅读/类型工具;运行期不依赖,避免与 audit_models 形成硬耦合
    from services.audit_models import L1Info, ProductInfo

logger = logging.getLogger("services.audit_l2")

_AC_LOCK = threading.Lock()   # R4 自动机扫描锁(product_audit workers>1 共享 ctx)


# =============================================================
# yaml 加载(三个 refdata/audit 种子文件;结果由调用方塞进 ctx)
# =============================================================


@lru_cache(maxsize=1)
def load_nrtl_keywords() -> tuple[list[str], list[str]]:
    """输入:无 → 输出:(small_part_keywords, whole_unit_keywords),均已 strip+小写。

    yaml 缺失 → warning + 返回 ([], []) ⇒ 所有 PT 归 whole_unit(即全部硬拒),
    方向保守,照迁旧语义。
    """
    seed = paths.audit_seed_file("nrtl_small_parts.yaml")
    if not seed.exists():
        logger.warning("nrtl_small_parts yaml %s 不存在, 分类器返回全部 whole_unit", seed)
        return [], []
    with seed.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sp = [str(x).strip().lower() for x in (data.get("small_part_keywords") or []) if str(x).strip()]
    wu = [str(x).strip().lower() for x in (data.get("whole_unit_keywords") or []) if str(x).strip()]
    logger.info("loaded %d small_part / %d whole_unit keywords", len(sp), len(wu))
    return sp, wu


@lru_cache(maxsize=1)
def load_nice_mapping() -> tuple[dict[str, list[str]], list[str]]:
    """输入:无 → 输出:(walmart_category → Nice Class 列表, 默认 class 列表)。

    class 值逐项 `str(c).strip().zfill(3)` 补成三位('6' → '006')。
    yaml 缺失 → warning + 返回 ({}, []),此时 R5 的 allowed_classes 为空 → R5 整体跳过;
    yaml 在时 default_classes 非空,所以那条跳过分支实际只在 yaml 缺失时触发。
    """
    seed = paths.audit_seed_file("pt_nice_class.yaml")
    if not seed.exists():
        logger.warning("seed yaml %s 不存在, Nice Class 映射返回空", seed)
        return {}, []
    with seed.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    mapping: dict[str, list[str]] = {}
    for k, v in (data.get("category_to_nice_class") or {}).items():
        if isinstance(v, list):
            mapping[k.strip()] = [str(c).strip().zfill(3) for c in v if str(c).strip()]
    default = [str(c).strip().zfill(3) for c in (data.get("default_classes") or []) if str(c).strip()]
    logger.info("loaded %d category→nice_class mappings (default: %s)", len(mapping), default)
    return mapping, default


def _classes_for(
    walmart_category: str | None,
    mapping: dict[str, list[str]],
    default: list[str],
) -> list[str]:
    """输入:walmart_category + 映射表/默认表 → 输出:允许的 Nice Class 列表('001'~'045')。

    四级查找照迁:① 精确 → ② 大小写不敏感 → ③ **双向**前缀(cat.startswith(k)
    或 k.startswith(cat),很宽:短 cat 如 "Ga" 会命中 "Garden")→ ④ default。
    """
    if not walmart_category:
        return list(default)
    cat = walmart_category.strip()
    # 精确匹配
    if cat in mapping:
        return list(mapping[cat])
    # case-insensitive
    for k, v in mapping.items():
        if k.lower() == cat.lower():
            return list(v)
    # prefix 匹配 (e.g. walmart_category="Home Improvement - Hardware" → "Home Improvement")
    for k, v in mapping.items():
        if cat.startswith(k) or k.startswith(cat):
            return list(v)
    return list(default)


# =============================================================
# R1: 类目准入 gate — 双字段白名单 (access_state + zh_can_do)
#
# **2026-08-20 起,类目层面能不能做,只有这一条规则说了算。**
# 同期删掉的 R0(代码里 8 个 walmart_category 硬禁)与 R2(yaml 18 条禁售大类)
# 讲的是同一件事的另外两种写法:三份清单各自维护,改一处漏两处,而且漏了
# **不报错**。所有者定稿:「以后我只要维护这个沃尔玛类目白名单即可」。
# 前置条件已完成 —— 白名单先补齐(pt_spec_sync 按官方 spec 重建准入明细),
# 再删黑名单,中间没有真空期。
#
# 业务规则 (用户 2026-04-27):
#   一个 PT 必须**同时满足**两条才允许进入后续审核:
#     1. access_state ∈ {'普通商品', '附条件允许'}      ← 白名单
#     2. zh_can_do = '是' OR zh_can_do 以 '需评估' 开头  ← 白名单
#   任一不满足 → -100 reject
#
# 旧仓实测基线 (上线前用同口径 SQL 对齐, 对不上是数据搬迁问题不是规则问题):
#   通过 gate 5785/7008 PT (82.5%), 被拒 1223;
#   拒的细分: access_state='需Walmart审批' 839 / '禁售' 292 / 'v5已废弃' 66;
#   zh_can_do 含 '否' 的 37 种自由文本变体.
# =============================================================


_ACCESS_WHITELIST = {"普通商品", "附条件允许"}

# R1 判不了时写进 L2Result.pending_reason 的两个 rule_code(evaluate 认这个集合)
PENDING_RULE_CODES = frozenset({"cat_gate_pt_unknown", "cat_gate_pt_not_in_meta"})


def _blocked_upstream(l1: L1Info) -> bool:
    """输入:L1Info → 输出:上游是否已判死(任一 hit 扣了分)。

    取代原先的 `l1.excluded_category_reason` 闸(该字段 2026-08-20 已删)。
    口径与 audit_rules._blocked 一致:**按 penalty<0 判,不按"有没有 hit"** ——
    0 分留痕的 hit(如 unmapped_amazon_path 哨兵)不算判死。
    """
    return any(h.penalty < 0 for h in l1.hits)


def _rule_category_gate(l1: L1Info, ctx: Any) -> list[RuleHit]:
    """输入:L1 结果 + ctx(用 ctx.pt_meta) → 输出:R1 命中的 hit(0 或 1 条,-100)。

    四种出口:
      cat_access_blocked    -100  access_state 不在白名单(需审批/禁售/已废弃/空)
      cat_zh_blocked        -100  zh_can_do 既不等于 '是' 也不以 '需评估' 开头(含空)
      cat_gate_pt_unknown      0  PT 空 / 'unknown' / '(unknown)' → **待人工**
      cat_gate_pt_not_in_meta  0  PT 不在 walmart_pt_meta        → **待人工**
    前两个**闸一先 return**,所以两个都拒时只报更上游的 cat_access_blocked。
    后两个 penalty=0 但会让 evaluate 把整条结论置成 pending(见 PENDING_RULE_CODES)。

    ⚠ 2026-08-20 修掉两条静默放行(所有者定 P0)。此前这两种情况一律
    `return []` —— 白名单是唯一的类目判据,而"查不到这个 PT"被当成了
    "这个 PT 没问题",分数原封不动 100 分直接 pass。
    **判不了不是判过了**:改判 pending 交人工,与"L1 解不出 PT → pending"
    同一条纪律(审核宁缺勿滥)。不扣分是有意的 —— 扣分等于"证据确凿地拒",
    而事实是**没有证据**。

    ⚠ 说实话:**当前接线下这两条走不到**。`audit_rules.resolve_pt` 末尾有一道
    同款 pt_meta 闸(解出来的 PT 不在表里就丢弃),PT 解不出会先在 L1 转 pending,
    所以 evaluate 收到的 PT 必定在 pt_meta 里。这两个分支是**防御网**,不是
    在补一个正在漏货的洞。留着的理由只有一条:白名单成了唯一判据之后,
    "查不到就放行"这个默认值本身不能存在 —— 上游任何一道闸被放松、或者有人
    直接调 evaluate,后果就是**静默满分放行且不报错**。默认值要站在安全那一侧。

    上游已判死(任一 hit penalty<0,如出版物硬禁)时整条规则不参与:
    既不重复扣分,也**不会把一条已经拒掉的结论降级成 pending**。
    两条 detail 的 access_state 处理**不对称**(前者带 `or "(空)"` 后者不带),照迁。
    """
    if _blocked_upstream(l1):
        return []

    pt = (l1.walmart_product_type or "").strip()
    if not pt or pt in {"unknown", "(unknown)"}:
        return [
            RuleHit(
                stage="L2",
                rule_code="cat_gate_pt_unknown",
                penalty=0,
                detail={
                    "walmart_pt": l1.walmart_product_type,
                    "pt_source": l1.pt_source,
                    "rule": "PT 未知 ⇒ 类目白名单查不了 ⇒ 待人工",
                    "source": "L2 R1",
                },
            )
        ]

    row = ctx.pt_meta.get(pt)
    if not row:
        logger.warning("R1: PT %r 不在 walmart_pt_meta,转 pending 待人工", pt)
        return [
            RuleHit(
                stage="L2",
                rule_code="cat_gate_pt_not_in_meta",
                penalty=0,
                detail={
                    "walmart_pt": pt,
                    "pt_source": l1.pt_source,
                    "rule": "PT 不在 walmart_pt_meta ⇒ 类目白名单查不了 ⇒ 待人工",
                    "source": "walmart_pt_meta",
                },
            )
        ]

    access = (row.get("access_state") or "").strip()
    zh = (row.get("zh_can_do") or "").strip()

    # 1. access_state 白名单检查 (优先报)
    if access not in _ACCESS_WHITELIST:
        return [
            RuleHit(
                stage="L2",
                rule_code="cat_access_blocked",
                penalty=-100,
                detail={
                    "walmart_pt": pt,
                    "walmart_category": row.get("walmart_category"),
                    "walmart_policy": "Restricted/Illegal",
                    "access_state": access or "(空)",
                    "zh_can_do": zh or "(空)",
                    "rule": "access_state 不在白名单 {普通商品, 附条件允许}",
                    "source": "walmart_pt_meta.access_state",
                },
            )
        ]

    # 2. zh_can_do 白名单检查
    zh_ok = (zh == "是") or zh.startswith("需评估")
    if not zh_ok:
        return [
            RuleHit(
                stage="L2",
                rule_code="cat_zh_blocked",
                penalty=-100,
                detail={
                    "walmart_pt": pt,
                    "walmart_category": row.get("walmart_category"),
                    "walmart_policy": "Restricted/Illegal",
                    "access_state": access,   # 注意: 这里没有 or "(空)", 照迁
                    "zh_can_do": zh or "(空)",
                    "rule": "zh_can_do 不在白名单 {是, 需评估*}",
                    "source": "walmart_pt_meta.zh_can_do",
                },
            )
        ]

    return []


# =============================================================
# R3: 类目需证书 (飞书 requirements + 沃尔玛官方 PT spec) — 硬/软分层
# =============================================================


_ASCII_RE = re.compile(r"^[\x00-\x7f]+$")


@lru_cache(maxsize=256)
def _kw_re(kw: str) -> re.Pattern | None:
    """输入:一个认证关键词 → 输出:带词边界的正则,或 None(表示该退回子串匹配)。

    只有**纯 ASCII** 关键词才配词边界:中文没有分词空格,`\b` 夹在两个汉字
    之间永不成立,给「fda 食品」加边界等于让它永不命中。
    边界只加在两端本身是字母数字的那一侧 —— 「510(k)」右端是 `)`,右侧不加,
    否则 `\b` 要求 `)` 后面跟词字符,反而漏掉句末的命中。
    """
    if not kw or not _ASCII_RE.match(kw):
        return None
    left = r"\b" if kw[0].isalnum() else ""
    right = r"\b" if kw[-1].isalnum() else ""
    return re.compile(left + re.escape(kw) + right, re.IGNORECASE)


def _kw_hit(kw: str, hay_low: str) -> bool:
    """输入:关键词 + 已小写的待扫文本 → 输出:是否命中(ASCII 走词边界,其余走子串)。"""
    pat = _kw_re(kw)
    return bool(pat.search(hay_low)) if pat else (kw in hay_low)


def _classify_nrtl_pt(pt_name: str, small_kws: list[str], whole_kws: list[str]) -> str:
    """输入:PT 名 + 小件/整机词表 → 输出:'small_part' | 'whole_unit'。

    全部**裸子串 + 小写**、无词边界。三个覆盖词 replacement/parts/accessor 写死在
    代码里(不在 yaml)且**最优先**;其次整机优先;再次小件;都不中 → 保守 whole_unit。
    (yaml 头注描述的"含 small AND 不含 whole"漏了覆盖词的位置,以代码为准。)
    """
    pt_low = (pt_name or "").strip().lower()
    if not pt_low:
        return "whole_unit"

    # 覆盖性关键词: Replacement/Parts/Accessor → 强制 small_part
    if any(kw in pt_low for kw in ("replacement", "parts", "accessor")):
        return "small_part"

    # 整机优先 (如果 PT 名里同时含整机词, 以整机为准)
    for wkw in whole_kws:
        if wkw in pt_low:
            return "whole_unit"

    # 小件命中
    for skw in small_kws:
        if skw in pt_low:
            return "small_part"

    # 未命中 — 保守: whole_unit
    return "whole_unit"


# =============================================================
# walmart_policy 推导 (用 walmart_category + PT 名 + cert 关键词三重信号)
#
# 修复 2026-04-28: 旧版纯靠 cert 关键字反推 walmart_policy 太粗暴, 例如:
#   - Chicken Coop Accessories (cat=Animals, requirements 含 FDA) → 错归 Cosmetic Products
#   - Drawing Boards (cat=Arts & Crafts, requirements 含 UL) → 错归 Electronics & RF
#   - Pool Heater Parts (cat=Garden & Patio, 含 EPA ENERGY STAR 自愿能效) → 错归 Hazmat
# 新逻辑: 优先 walmart_category (飞书原生大类) → policy, cert 关键字只在 cat 没对应时 fallback.
# =============================================================

# walmart_category → walmart_prohibited_policy.category_en
# (粗映射, 仅在 cat 与 policy 强相关时直接映射, 不强相关用 cert/PT 名补)
_CATEGORY_TO_POLICY = {
    "vehicles": "Auto & Motor Vehicles",
    "automotive": "Auto & Motor Vehicles",
    "electronics": "Electronics & RF",
    "fashion": "Textiles & Apparel",
    "beauty": "Cosmetic Products",
    "baby": "Baby Products",
    "toys": "Children's Products",
    "media": "Software",
}

# walmart_category 默认是普通商品的大类 (PT 不含电子关键字时强制 General-Use, 防止 cert 字串误归 RF)
_GENERAL_USE_CATEGORIES = {
    "arts & crafts", "home improvement", "sporting goods", "office",
    "musical instruments", "business & industrial", "furniture", "household",
    "occasion & seasonal", "everything else", "safety & emergency", "photography",
}

# 真正"电子产品"PT 的关键字 (在 General-Use 大类下命中这些才升级到 Electronics & RF)
_ELECTRONIC_PT_KEYWORDS = (
    "led", "lamp", "lantern", "light", "lighting", "electric", "electronic",
    "wireless", "bluetooth", "wifi", "battery-powered", "rechargeable", "usb",
    "speaker", "amplifier", "fan", "heater", "cooler", "freezer", "refrigerator",
    "monitor ", "display ", "screen", "projector", "appliance", "flashlight",
)

# PT 名关键字 → policy (覆盖 cat_map 走不通的强信号 PT)
# 顺序敏感: 先匹配的优先
_PT_KEYWORD_TO_POLICY: list[tuple[tuple[str, ...], str]] = [
    (("vape", "e-cigar", "hookah", "tobacco", "cigarette"), "Tobacco & Vaping"),
    (("alcohol", "distillation apparatus", "still ", "moonshine", "wine kit"), "Alcohol"),
    (("vitamin", "supplement", "protein powder"), "Dietary Supplements"),
    (("medical device", "stethoscope", "blood pressure monitor", "thermometer"), "Medical Devices"),
    (("pesticide", "herbicide", "insecticide", "fertilizer", "rodenticide", "pest"), "Hazardous Items"),
    (("seed", "live plant", "live tree", "sapling"), "Plants & Seeds"),
    (("firearm", "ammunition", "bullet ", "bulletproof", "tactical vest", "body armor", "police badge"), "Military & Law Enforcement"),
    (("helmet", "bicycle helmet"), "General-Use Products"),  # ASTM/CPSC, 不归 RF
    (("chicken coop", "pet bowl", "pet food", "dog food", "cat food", "bird seed"), "Pet Products"),
    (("scooter", "e-bike", "hoverboard", "ebike", "balance bike"), "Ride-Ons & Micromobility"),
]


def _infer_walmart_policy(walmart_category: str, hard_matches: list[str], walmart_pt: str) -> str:
    """输入:walmart_category + 硬认证 label 列表 + PT 名 → 输出:walmart_policy 字符串。

    三重信号,**判定顺序即优先级,先返回先赢**:PT 名关键字 > walmart_category > cert 关键字。
    `"still "` / `"bullet "` / `"monitor "` / `"display "` / `"live "` / `"wild "` 的
    **尾随空格是有意的**(防 stainless / bulletin 误中),逐字保留。
    第 9 步作用在 hard label 拼接串上,而 label 是中文(「FDA 食品设施」不含拉丁 food),
    所以 fda 分支实际几乎总落到 Cosmetic Products —— 旧仓真实行为,照迁。
    """
    cat_low = (walmart_category or "").lower().strip()
    pt_low = (walmart_pt or "").lower().strip()
    cert_str = " ".join(hard_matches).lower()

    # 1. PT 名关键字 (最具体的信号, 优先)
    for kws, policy in _PT_KEYWORD_TO_POLICY:
        if any(k in pt_low for k in kws):
            return policy

    # 2. walmart_category 直映射 (粗大类)
    if cat_low in _CATEGORY_TO_POLICY:
        return _CATEGORY_TO_POLICY[cat_low]

    # 3. Animals 大类 - 区分 Pet Products (家禽/家宠) vs Animals (野生)
    if cat_low == "animals":
        if any(k in pt_low for k in ("live ", "wild ", "exotic")):
            return "Animals"
        return "Pet Products"

    # 4. Garden & Patio - 区分 Hazmat (化学/农药) vs General
    if cat_low == "garden & patio":
        if any(k in pt_low for k in ("fertilizer", "pesticide", "herbicide", "insecticide", "soil", "weed")):
            return "Hazardous Items"
        if any(k in pt_low for k in ("seed", "plant", "tree", "bulb")):
            return "Plants & Seeds"
        return "General-Use Products"

    # 5. Food & Beverage
    if cat_low in ("food & beverage", "food and beverage"):
        if any(k in cert_str for k in ("alcohol", "beer", "wine", "spirit")):
            return "Alcohol"
        return "Food Products"

    # 6. Health & Personal Care - 区分 cosmetic / medical / supplement
    if cat_low == "health & personal care":
        if any(k in cert_str for k in ("510(k)", "medical")):
            return "Medical Devices"
        if any(k in pt_low for k in ("supplement", "vitamin", "protein")):
            return "Dietary Supplements"
        return "Cosmetic Products"

    # 7. General-Use 大类 (Arts & Crafts / Home Improvement / Sporting Goods 等):
    #    仅当 PT 名含真"电子产品"关键字时才归 RF, 否则普通商品 (修 Drawing Boards/Pressure Regulators/Boat Brackets 误归 RF)
    if cat_low in _GENERAL_USE_CATEGORIES:
        if any(k in pt_low for k in _ELECTRONIC_PT_KEYWORDS):
            return "Electronics & RF"
        return "General-Use Products"

    # 8. Home 大类 (Lava Lamps/Floor Lamps 等带电家居 → RF, 其他 → Home Goods)
    if cat_low == "home":
        if any(k in pt_low for k in _ELECTRONIC_PT_KEYWORDS):
            return "Electronics & RF"
        return "Home Goods"

    # 9. cert 关键字 fallback (粗粒度, 不准但覆盖兜底)
    if any(k in cert_str for k in ('cpsia', 'cpc', 'gcc')):
        return "Children's Products"
    if any(k in cert_str for k in ('fda', 'mocra')):
        # ⚠ 2026-08-20 修:cert_str 是**中文 label 的拼接串**(「FDA 食品设施」
        # 「FDA 药品」「FDA 510(k)」),里面根本不会出现拉丁 'medical' ——
        # 这一支此前永不成立,医疗器械类目一律错落成 Cosmetic Products。
        # 补上 label 里真会出现的写法('510(k)' 与中文「医疗」)。
        if any(k in cert_str for k in ('medical', '510(k)', '医疗')):
            return "Medical Devices"
        if 'food' in cert_str or '食品' in cert_str:
            return "Food Products"
        if 'pet' in cert_str or '宠物' in cert_str or 'animal' in cert_str:
            return "Pet Products"
        if 'drug' in cert_str or '药品' in cert_str:
            return "Drugs & Paraphernalia"
        return "Cosmetic Products"
    if 'aafco' in cert_str:
        return "Pet Products"
    if any(k in cert_str for k in ('atf', 'dea')):
        return "Drugs & Paraphernalia"
    if any(k in cert_str for k in ('ul', 'etl', 'csa', 'nrtl', 'fcc')):
        return "Electronics & RF"
    if 'epa' in cert_str:
        # EPA ENERGY STAR 是自愿能效, 不是 Hazmat; 真 Hazmat 看 FIFRA/TSCA/化学
        if any(k in cert_str for k in ('fifra', 'tsca', 'pesticide', '危险', 'hazmat')):
            return "Hazardous Items"
        if 'energy star' in cert_str:
            return "General-Use Products"
        return "Hazardous Items"  # 默认 EPA → Hazmat (旧逻辑保留, 多数 EPA 注册都是危品)

    return "General-Use Products"


def _rule_cat_requires_cert(l1: L1Info, ctx: Any) -> list[RuleHit]:
    """输入:L1 结果 + ctx(用 ctx.pt_meta / ctx.pt_spec / ctx.nrtl_*) → 输出:0 或 1 条 hit。

    四个分支互斥、依次 return(硬优先):
      A. meta.requirements 命中硬关键词        → cat_requires_cert_hard      -100
      B. spec.has_real_cert:small_part        → cat_requires_cert_small_part   0
                            whole_unit        → cat_requires_cert_hard      -100
      C. meta.requirements 仅命中软关键词      → cat_requires_cert_soft         0
      D. spec.has_soft_cert                   → cat_requires_cert_soft         0
    pt_meta / pt_spec 查不到该 PT → 一律当"无要求"处理,不拒不标记(与 R1 同调)。

    ⚠ 2026-08-20 修掉裸子串(所有者定 P0)。此前是 `kw in req_low`,无词边界:
    `"ul" in "fda regulation"` 为真 —— **任何**含 regulation 的 requirements 都被
    打成硬认证「UL 认证」并 -100 硬拒;`"iso" in "poison control"`、
    `"atf" in "platform"`、`"dea" in "idea"`、`"cpc" in "cpcb"` 同理。
    这是纯误伤:拒得理直气壮,理由却是从别的词里抠出来的两个字母。
    现在 ASCII 关键词走词边界正则(`_kw_re`),中文/混排关键词(如「fda 食品」)
    仍走子串 —— 中文没有词边界概念,`\b` 在中文串里反而永不成立。
    软词抑制 `not _kw_re(kw).search(...)` 同步改成同一口径。

    A 与 B 两个分支的 cat_requires_cert_hard **detail 结构不同**(A 有 meta_requirements /
    matched_hard_kws / walmart_policy;B 有 hard_cert_fields / classified_as 且**没有
    walmart_policy**),C 与 D 同 rule_code 不同 detail(靠 source 区分)。下游按
    rule_code 取字段时必须两种都兼容。
    """
    pt = (l1.walmart_product_type or "").strip()
    if not pt or pt in {"unknown", "(unknown)"}:
        return []          # PT 未知已由 R1 转 pending,这里不重复表态
    if _blocked_upstream(l1):
        return []

    # ---- 1. 取 walmart_pt_meta.requirements (飞书业务维护) ----
    meta = ctx.pt_meta.get(pt)
    meta_req = (meta.get("requirements") if meta else "") or ""
    meta_notes = (meta.get("notes") if meta else "") or ""
    meta_cat = (meta.get("walmart_category") if meta else "") or ""

    # 扫描关键词 (忽略大小写)
    req_low = meta_req.lower()
    hard_matches = []
    soft_matches = []
    HARD_KWS = [
        ("UL 认证", "ul"), ("ETL Listed", "etl"), ("CSA", "csa"),
        ("NRTL", "nrtl"), ("FCC", "fcc"),
        ("FDA 食品设施", "fda 食品"), ("FDA 药品", "fda 药品"),
        ("FDA 510(k)", "510(k)"), ("MoCRA 化妆品", "mocra"),
        ("FDA", "fda"),
        ("EPA FIFRA", "fifra"), ("EPA 注册", "epa"),
        ("CPSIA / CPC", "cpsia"), ("CPC 证书", "cpc"),
        ("GCC 证书", "gcc"),
        ("AAFCO 宠物食品", "aafco"),
        ("NSF/ANSI 61", "nsf"),
        ("ATF 管控", "atf"), ("DEA 管控", "dea"),
    ]
    SOFT_KWS = [
        ("SDS/MSDS", "sds"), ("ASTM", "astm"), ("ANSI", "ansi"),
        ("ISO", "iso"), ("RoHS", "rohs"), ("Prop 65", "prop 65"), ("Prop65", "prop65"),
        ("警告标签", "警告"), ("标签", "标签"), ("测试报告", "测试报告"),
    ]
    for label, kw in HARD_KWS:
        if _kw_hit(kw, req_low):
            hard_matches.append(label)
    for label, kw in SOFT_KWS:
        # 软词抑制:该软词已经作为某条硬认证 label 的一部分出现过就不再单列
        # (实际生效的只有「ansi」被「NSF/ANSI 61」抑制这一条,保留原意)
        if _kw_hit(kw, req_low) and not any(_kw_hit(kw, hm.lower())
                                            for hm in hard_matches):
            soft_matches.append(label)

    # ---- 2. 取 walmart_pt_spec ----
    spec = ctx.pt_spec.get(pt)

    spec_has_hard = bool(spec and spec.get("has_real_cert"))
    spec_has_soft = bool(spec and spec.get("has_soft_cert"))
    spec_real = (spec and spec.get("real_cert_fields")) or []
    spec_soft = (spec and spec.get("soft_cert_fields")) or []

    # ---- 3. 综合判定 (硬优先) ----

    # A. meta 硬性认证命中 → -100
    if hard_matches:
        # 推 walmart_policy: 优先 walmart_category (飞书原生大类, 准),
        # cert 关键词只做辅助/fallback
        _policy = _infer_walmart_policy(meta_cat, hard_matches, pt)
        return [
            RuleHit(
                stage="L2",
                rule_code="cat_requires_cert_hard",
                penalty=-100,
                detail={
                    "walmart_pt": pt,
                    "walmart_policy": _policy,   # 直接映射, 不靠 L3 annotation
                    "meta_requirements": meta_req,
                    "matched_hard_kws": hard_matches,
                    "matched_soft_kws": soft_matches,
                    "ts_notes": meta_notes or None,
                    "spec_real_cert_fields": spec_real,
                    "source": "walmart_pt_meta.requirements",
                    "note": "飞书维护的合规要求 (含实验室证书/官方注册号), 搬运模式做不了",
                },
            )
        ]

    # B. walmart_pt_spec 硬 cert (NRTL 电气): 分整机/小件
    if spec_has_hard:
        part_type = _classify_nrtl_pt(pt, ctx.nrtl_small, ctx.nrtl_whole)
        if part_type == "small_part":
            return [
                RuleHit(
                    stage="L2",
                    rule_code="cat_requires_cert_small_part",
                    penalty=0,   # v3: 软证据不扣分, 交 L3 看 PT 上下文判
                    detail={
                        "walmart_pt": pt,
                        "hard_cert_fields": spec_real,
                        "classified_as": "small_part",
                        "source": "walmart_pt_spec + nrtl_classifier",
                        "note": "电气小件/配件, 部分可填 No 上架, 需下游人工审核",
                    },
                )
            ]
        return [
            RuleHit(
                stage="L2",
                rule_code="cat_requires_cert_hard",
                penalty=-100,
                detail={
                    "walmart_pt": pt,
                    "hard_cert_fields": spec_real,
                    "classified_as": part_type,
                    "source": "walmart_pt_spec.has_real_cert + nrtl_classifier",
                    "note": "整机电器, 必须 NRTL 认证, 搬运做不了",
                },
            )
        ]

    # C. meta 仅软合规命中 → 0 (软证据不扣分, 交 L3 综合判)
    if soft_matches:
        return [
            RuleHit(
                stage="L2",
                rule_code="cat_requires_cert_soft",
                penalty=0,
                detail={
                    "walmart_pt": pt,
                    "meta_requirements": meta_req,
                    "matched_soft_kws": soft_matches,
                    "source": "walmart_pt_meta.requirements (软合规)",
                    "note": "软合规 (SDS/ASTM/ISO/RoHS/Prop65/警告标签/测试报告), 可提供资料或填披露",
                },
            )
        ]

    # D. walmart_pt_spec 软合规 (smallParts/state_chemical/ingredients) → 0
    if spec_has_soft:
        return [
            RuleHit(
                stage="L2",
                rule_code="cat_requires_cert_soft",
                penalty=0,
                detail={
                    "walmart_pt": pt,
                    "soft_cert_fields": spec_soft,
                    "source": "walmart_pt_spec.has_soft_cert",
                    "note": "软合规 (Prop65/小件警告/成分披露等), 需填写披露信息",
                },
            )
        ]

    return []


# =============================================================
# R4: title/五点/描述 命中黑名单品牌 (Aho-Corasick) → penalty 0 (纯证据)
# =============================================================


R4_PENALTY = 0   # v3 用户 2026-04-27: 软证据不扣分, 只标记 detail 传 L3 判通用词/真品牌


# 中日韩 + 全角:这些文字之间不写空格,"紧邻"就是词与词的分界
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uac00-\ud7ff\uf900-\ufaff"
                     r"\ufe30-\ufe4f\uff00-\uffef]")


def _is_word_boundary_char(c: str) -> bool:
    """输入:单个字符 → 输出:它是否算词边界。

    ⚠ 2026-08-20 修:`c.isalnum()` 对中文/全角返回 True,于是"耐克运动鞋"里
    黑名单词「耐克」左右都被判成非边界 —— **中文品牌一个都拦不住**,而且不报错。
    中日韩与全角字符不写分词空格,紧邻即边界,单独判掉;拉丁带音标字母
    (café 的 é)仍按词内字符处理,免得把 "Caf" 这种前缀切出来误命中。
    """
    if not c:
        return True
    if _CJK_RE.match(c):
        return True
    return not (c.isalnum() or c == '_')


def _rule_title_desc_blacklist(product: ProductInfo, ctx: Any) -> list[RuleHit]:
    """输入:产品 + ctx(用 ctx.ac_automaton) → 输出:R4 命中的 hit(0 或 1 条,penalty 0)。

    扫 product.searchable_text(title + 全部五点 + 长描述)。自动机未构建(None)→ 返回 []。
    AC 不自带词边界,命中后手动检查前后字符;**自品牌豁免是精确等值**
    (brand strip+lower 后与命中词完全相等才跳过);同一个 brand 只报第一次。
    """
    hay = product.searchable_text
    if not hay:
        return []
    A = ctx.ac_automaton
    if A is None:   # 未装 pyahocorasick / 词表为空 → 本条规则整体跳过
        return []
    own_brand = (product.brand or "").strip().lower()
    hay_lower = hay.lower()
    n = len(hay_lower)

    matches: list[dict[str, Any]] = []
    seen: set[str] = set()

    # AC iter 返回 (end_index, value), end_index 是命中末字符的位置 (inclusive)。
    # workers>1 时共享一个自动机:pyahocorasick 只读迭代的线程安全没有官方
    # 保证,锁住扫描段(µs 级,相对秒级 LLM 调用零成本)
    with _AC_LOCK:
        ac_matches = list(A.iter(hay_lower))
    for end_idx, brand in ac_matches:
        if brand in seen or brand == own_brand:
            continue
        start_idx = end_idx - len(brand) + 1
        # 手动 word boundary 检查 (AC 不自带)
        left_ok = (start_idx == 0) or _is_word_boundary_char(hay_lower[start_idx - 1])
        right_ok = (end_idx == n - 1) or _is_word_boundary_char(hay_lower[end_idx + 1])
        if not (left_ok and right_ok):
            continue
        seen.add(brand)
        # 还原原始大小写显示给审核员
        matches.append({"brand": brand, "matched_phrase": hay[start_idx:end_idx + 1]})

    if not matches:
        return []
    return [
        RuleHit(
            stage="L2",
            rule_code="title_desc_blacklist",
            penalty=R4_PENALTY,
            detail={
                "matches": matches,
                "count": len(matches),
                "note": "L3 LLM 需判断每个词是真品牌还是通用词",
            },
        )
    ]


# =============================================================
# R5: title/desc 命中 USPTO LIVE 商标 (按 Nice Class 过滤) → penalty 0
# =============================================================


R5_PENALTY = 0   # v3: 软证据不扣分, 只标记 USPTO 命中传 L3
# 长度 >= 4: 提取 "疑似商标" 的大写开头词; 避免 3 字母缩写 "USB/DMX/FCC" 被误判
_WORD_TOKEN_RE = re.compile(r"\b([A-Z][A-Za-z0-9]{3,})\b")

_R5_SQL = """
                    SELECT DISTINCT brand_upper AS mark_identification,
                                    nice_class,
                                    goods_services
                    FROM brand_nice_class
                    WHERE brand_upper = ANY(%s)
                      AND is_live = TRUE
                      AND nice_class = ANY(%s)
                    LIMIT 200
                    """


def _extract_candidate_tokens(text: str, top_k: int = 40) -> list[str]:
    """输入:待扫文本(+ 上限)→ 输出:去重后的前 top_k 个疑似商标候选词(保持出现顺序)。

    大写开头 + 3 个以上字母数字(总长 ≥4),过滤 stopword;去重**大小写敏感**。
    """
    seen: list[str] = []
    for m in _WORD_TOKEN_RE.finditer(text or ""):
        w = m.group(1)
        if w in seen:
            continue
        # 过滤 stopword (通用英文词/产品属性词)
        if is_stopword(w):
            continue
        seen.append(w)
        if len(seen) >= top_k:
            break
    return seen


def _r5_failure(ctx: Any) -> None:
    """输入:上下文 → 输出:无(R5 连续失败计数,≥5 次自动关停本轮 R5)。

    兜底触发必须记日志计数(铁律):没有这道闸,uspto 一次报错后整轮每个
    产品都静默落 except——"R5 开着"与"R5 一条没查"在摘要里长得一样
    (评审 P2-6)。关停后由 workflow 摘要亮出失败计数。
    """
    ctx.uspto_failures = getattr(ctx, "uspto_failures", 0) + 1
    if ctx.uspto_failures >= 5 and ctx.uspto is not None:
        logger.error("R5 连续失败 %d 次,本轮自动关停(uspto 置 None)",
                     ctx.uspto_failures)
        ctx.uspto = None


def _rule_trademark_live(product: ProductInfo, l1: L1Info, ctx: Any) -> list[RuleHit]:
    """输入:产品 + L1 结果 + ctx(用 ctx.uspto / ctx.nice_*) → 输出:0 或 1 条 hit(penalty 0)。

    ctx.uspto 为 None(workflow 未开 R5 / 库不可用)→ 直接返回 []。
    连接由调用方持有并整批复用(旧仓每 ASIN 新建连接被明确记过是 CPU 灾难);
    本模块不建连接、不建连接池。
    DB 不可达或查询失败 → warning + 返回 [](fail-soft,不影响结论)。
    `3 <= len(v)` 与 is_stopword 的 min_len=4 叠加 ⇒ 实际有效下界是 4;
    variants 来自 set ⇒ 顺序不定,只影响 SQL 参数数组顺序,不影响结果。
    """
    if ctx.uspto is None:
        return []

    tokens = _extract_candidate_tokens(product.searchable_text)
    if not tokens:
        return []

    # 展开大小写形式 (brand_upper 索引通常是大写存储)
    variants: set[str] = set()
    for t in tokens:
        variants.update({t.upper(), t.lower()})
    variants_list = [v for v in variants if 3 <= len(v) <= 200 and not is_stopword(v)]
    if not variants_list:
        return []

    # Nice Class 过滤集
    allowed_classes = _classes_for(l1.walmart_category, ctx.nice_mapping, ctx.nice_default)
    if not allowed_classes:
        logger.debug("R5: walmart_category=%r 无 Nice Class 映射, 跳过", l1.walmart_category)
        return []

    import psycopg               # 惰性导入(同 registry/db.py): 不跑 R5 的环境不必装
    from psycopg.rows import dict_row

    hits_rows: list[dict] = []
    try:
        with ctx.uspto.cursor(row_factory=dict_row) as cur:
            cur.execute(_R5_SQL, ([v.upper() for v in variants_list], allowed_classes))
            hits_rows = cur.fetchall()
    except psycopg.OperationalError as e:
        logger.warning("uspto DB 不可达, R5 跳过: %s", e)
        _r5_failure(ctx)
        return []
    except Exception as e:  # noqa: BLE001
        logger.warning("R5 查询失败, 跳过: %s", e)
        _r5_failure(ctx)
        return []

    if not hits_rows:
        return []

    # 聚合结果: 同 brand 不同 class → 合并; mark 本身若是 stopword 再过
    agg: dict[str, dict] = {}
    for r in hits_rows:
        mark = (r.get("mark_identification") or "").strip()
        if not mark or is_stopword(mark):
            continue
        entry = agg.setdefault(mark, {"mark": mark, "classes": set(), "samples": []})
        cls = r.get("nice_class") or ""
        if cls:
            entry["classes"].add(cls)
        g = (r.get("goods_services") or "").strip()
        if g and len(entry["samples"]) < 2:
            entry["samples"].append(g[:80])

    if not agg:
        return []
    matches = [
        {
            "mark": v["mark"],
            "classes": sorted(v["classes"]),
            "goods_samples": v["samples"],
        }
        for v in sorted(agg.values(), key=lambda x: x["mark"].lower())
    ]
    return [
        RuleHit(
            stage="L2",
            rule_code="trademark_live",
            penalty=R5_PENALTY,
            detail={
                "matched_marks": [m["mark"] for m in matches],
                "count": len(matches),
                "token_variants": len(variants_list),
                "walmart_pt": l1.walmart_product_type,
                "walmart_category": l1.walmart_category,
                "class_filter": allowed_classes,
                "matches_with_classes": matches[:20],   # 只前 20 条带 class/samples
                "note": "L3 LLM 需判断每个商标是通用词还是真品牌词",
            },
        )
    ]


# =============================================================
# R7: 促销 / 宣称性内容 (Walmart Content Standards) → penalty 0
# =============================================================


R7_PENALTY = 0   # v3: promotional 词不扣分, 交 L3 LLM 判是否真违规

# 促销宣称短语 — Walmart Content Standards 明确禁用
# 分类:
#   tier_strong: 无证据的质量最高级 / 排名宣传 (触发 hit)
#   tier_soft  : 空洞形容词 (常和真描述混用, 仅记录; 单独命中**不产 hit**)
# 为了让 L3 有足够上下文, 一次把所有命中都扔进 detail; 不扣分 (R7_PENALTY = 0)
_PROMO_PHRASES_STRONG = [
    # 最高级 / 无证据
    r"\bpremium\s+quality\b",
    r"\bcommercial\s+grade\b",
    r"\bindustrial\s+grade\b",
    r"\bprofessional\s+grade\b",
    r"\bheavy\s+duty\b",            # "heavy duty" 独立短语, 不是 "heavy-duty"
    r"\bmilitary\s+grade\b",        # 未经认证不可声称
    r"\bmedical\s+grade\b",         # 需 FDA 支撑, 否则虚假宣传
    r"\bfood[-\s]grade\b",          # 需 FDA/NSF 支撑
    # 排名 / 比较最高级
    r"#\s*1\b",                     # "#1 Best Seller" / "#1 Rated" / "#1 Choice"
    r"\bno\.\s*1\b",                # "No.1 in..."
    r"\btop\s+rated\b",
    r"\bbest\s+seller\b",
    r"\bbest\s+selling\b",
    r"\bbest[-\s]in[-\s]class\b",
    r"\bhighest\s+rated\b",
    r"\bworld'?s\s+best\b",
    r"\bworld'?s\s+(?:leading|greatest|finest)\b",
    r"\bbest[-\s]ever\b",
    r"\b(?:fda|usda|epa|ul|etl)[-\s]approved\b",   # 声称认证但常无依据, 给 L3 检查
    # 绝对宣称
    r"\b100%\s+(?:guaranteed|pure|natural|organic|authentic|genuine)\b",
    r"\blifetime\s+(?:warranty|guarantee)\b",       # 承诺要核实
    r"\bmoney[-\s]back\s+guarantee\b",
    r"\bsatisfaction\s+guaranteed\b",
]

_PROMO_PHRASES_SOFT = [
    # 空洞形容词 (单独用时常常合法; 仅记录便于 L3 叠加判断)
    r"\bultra[-\s]premium\b",
    r"\bhigh[-\s]quality\b",
    r"\btop[-\s]quality\b",
    r"\bsuper[-\s]strong\b",
    r"\bsuper[-\s]durable\b",
    r"\bextra[-\s]strong\b",
    r"\bultra[-\s]durable\b",
    r"\bunbeatable\b",
    r"\bunmatched\b",
    r"\bsecond[-\s]to[-\s]none\b",
    r"\bas[-\s]seen[-\s]on[-\s]tv\b",
    r"\bfactory[-\s]direct\b",
    r"\bamazon'?s\s+choice\b",   # 冒用 Amazon 推荐词
]

_PROMO_STRONG_RE = re.compile("|".join(_PROMO_PHRASES_STRONG), re.IGNORECASE)
_PROMO_SOFT_RE = re.compile("|".join(_PROMO_PHRASES_SOFT), re.IGNORECASE)

# 全大写连续词 — 过多大写 Walmart 禁止
# 连续 3+ 个全大写英文词 (允许中间带数字), 每个词 >= 2 个字符, 跳过纯数字/型号
_ALLCAPS_RUN_RE = re.compile(
    r"(?:\b[A-Z][A-Z0-9]{1,}\b[\s\-]+){2,}\b[A-Z][A-Z0-9]{1,}\b"
)

# 纯数字/常见尺寸单位 token, 不计入 "全大写滥用"
# ⚠ 2026-08-20 两处修正(此前是死条目,留着只会让人以为它们在起作用):
#   · "RoHS" —— 比较用的是 `t.upper()`,大小写混写的条目永远匹配不上,
#     整表统一大写(见下方 frozenset 推导),ROHS 现在真的被当噪声了;
#   · 单字母 "L"/"M"/"S" —— 全大写连跑正则要求每个词 ≥2 字符,
#     切出来的 token 不可能是单字母,这三项**证明不可达**,删除。
_ALLCAPS_NOISE_TOKENS = frozenset(t.upper() for t in {
    "USB", "LED", "LCD", "OLED", "HDMI", "AC", "DC", "RGB", "IP",
    "GHZ", "MHZ", "HZ", "FM", "AM", "3D", "4K", "8K", "HD", "UHD",
    "PACK", "PCS", "CT", "GSM", "LBS", "OZ", "ML", "FL", "FT",
    "ISO", "ASTM", "ANSI", "NRTL", "UL", "ETL", "CE", "FCC", "RoHS",
    "USA", "US", "EU", "UK", "CA", "NBA", "NFL", "MLB", "NHL",
    "PRO", "PLUS", "MAX", "MINI", "XXL", "XL", "XS",
})


def _scan_allcaps_runs(title: str) -> list[str]:
    """输入:标题 → 输出:连续 3+ 个全大写词的原文片段列表(去噪去重后)。

    去噪后 real_tokens 不足 3 个的片段不算。
    """
    if not title:
        return []
    hits = []
    seen: set[str] = set()
    for m in _ALLCAPS_RUN_RE.finditer(title):
        span = m.group(0).strip()
        if span in seen:
            continue
        # 去噪: 如果去掉 noise token 后不足 3 词, 不算
        tokens = [t for t in re.split(r"[\s\-]+", span) if t]
        real_tokens = [t for t in tokens if t.upper() not in _ALLCAPS_NOISE_TOKENS]
        if len(real_tokens) >= 3:
            seen.add(span)
            hits.append(span)
    return hits


def _rule_content_promotional(product: ProductInfo) -> list[RuleHit]:
    """输入:产品 → 输出:R7 命中的 hit(0 或 1 条,penalty 0)。

    扫 title + bullet_points[0..2](**只前 3 条五点**,不扫 long_description,
    避免过长文本误杀);全大写连跑**只扫 title**。
    ⚠ 2026-08-20 修:此前只命中 soft 短语时**不产出 hit**,证据当场丢掉,
    L3 永远看不到 —— 而 detail 里还写着"L3 LLM 需判断",承诺了却没送到。
    现在照样产 hit(penalty 仍是 0,不影响任何判定),detail 里用
    `soft_only=True` 标出它的份量,L3 提示词按这个字段区别对待。
    """
    title = (product.title or "")
    bullets_txt = " ".join((product.bullet_points or [])[:3])
    scan_text = title + "\n" + bullets_txt
    if not scan_text.strip():
        return []

    strong_hits = sorted({m.group(0) for m in _PROMO_STRONG_RE.finditer(scan_text)})
    soft_hits = sorted({m.group(0) for m in _PROMO_SOFT_RE.finditer(scan_text)})
    allcaps_hits = _scan_allcaps_runs(title)

    if not (strong_hits or soft_hits or allcaps_hits):
        return []

    return [
        RuleHit(
            stage="L2",
            rule_code="content_promotional",
            penalty=R7_PENALTY,
            detail={
                "strong_phrases": strong_hits,      # 核心命中
                "soft_phrases": soft_hits,          # 辅助命中 (仅记录)
                "allcaps_runs": allcaps_hits,       # 连续全大写片段
                "soft_only": not (strong_hits or allcaps_hits),
                "strong_count": len(strong_hits),
                "allcaps_count": len(allcaps_hits),
                "title_preview": title[:200],
                "walmart_policy": "Content Standards",
                "note": "L3 LLM 需判断宣称词是否有事实依据 / 是否属合规描述",
            },
        )
    ]


# =============================================================
# R8: Walmart 严格合规 — 敏感文化 / 政治 / 宗教 / 武器 / 成人 / 卡通 IP → penalty 0
# =============================================================
# 目的: 迎合 Walmart 实际审核尺度 (即使 Walmart 政策文本未明列, 实际会拦).
# B 类漏放产品里很大一部分是这类 (Juneteenth/Black History/Bible/Mosque/
# Soviet/Confederate/Hunting Bullet/Pickaxe/Wine/Cigar 等文本).
# walmart_policy = "Offensive Content" (对齐 Walmart 判决).
# 注: 旧仓注释写 "-30 / 单独 -30 不够杀" 与常量矛盾, 实际 R8_PENALTY = 0 (以常量为准).

R8_PENALTY = 0   # v3: 敏感词不扣分, 交 L3 LLM 综合上下文判 (硬性禁词由 L0 phase0 处理)

# 分类词典. 按子类记录便于 detail 调试
_R8_SENSITIVE_PATTERNS = {
    "cultural_day": [
        # 美国黑人文化节日 (Walmart 实际会标 offensive content)
        r"\bjuneteenth\b",
        r"\bblack\s+history\s+month\b",
        r"\bafro[-\s]american\b",
        r"\bafrican[-\s]american\b",
        r"\bafro\s+(?:dope|pride|king|queen)\b",
        # 其他民族 / 宗教节日 (单一节日一般 OK, 但用户选择严格合规)
        r"\beid\s+mubarak\b",
        r"\bramadan\b",
        r"\bhanukkah\b",
        r"\bkwanzaa\b",
    ],
    "religious_single_faith": [
        # 单一宗教产品 (Walmart 实际偶尔标 offensive; 用户选严格合规)
        r"\bbible\s+(?:poster|verse|map|study)\b",
        r"\b12\s+tribes\s+of\s+israel\b",
        r"\bmosque\b",
        r"\bsultan\s+ahmed\b",
        r"\bislam(?:ic)?\s+(?:poster|prayer|art)\b",
        r"\btorah\b",
        r"\bhindu\s+(?:poster|deity|god|goddess)\b",
        r"\bbuddha\s+(?:statue|poster|figurine)\b",
    ],
    "political_sensitive": [
        # 政治人物 + 讽刺组合 (比 L3 hit 更早)
        r"\bmaga\b",
        r"\bdeep\s+state\b",
        r"\bpolice\s+state\b",
        r"\bcommander\s+in\s+(?:crap|chief)\b",
        r"\bliberal\s+tears\b",
        r"\btrump\s+(?:derangement|syndrome|2024|2028)\b",
        r"\btrump\s+(?:hat|sticker|flag|pin|shirt)\b",
        r"\bbiden\s+(?:sucks|fail)\b",
        r"\blet'?s\s+go\s+brandon\b",
        r"\bfjb\b",       # "F*** Joe Biden" 缩写
    ],
    "historical_intolerance": [
        # 历史敏感符号 — 严格合规侧不当装饰用
        r"\bconfederate\b",
        r"\bussr\s+(?:army|flag|emblem)\b",
        r"\bsoviet\s+(?:army|flag|emblem|red\s+star)\b",
        r"\bthird\s+reich\b",
        r"\bwehrmacht\b",
        r"\bss\s+(?:bolts|runes)\b",
        r"\bnazi\b",
        r"\bkkk\b",
        r"\bhamas\b",
        r"\btaliban\b",
    ],
    "weapons_decorative": [
        # 武器 / 弹药 / 暴力图案的装饰商品 (Walmart 大量下架案例)
        r"\bhunting\s+(?:gifts?|gear)\b",
        r"\bbullet\s+(?:tumbler|cup|mug|bottle|keychain|necklace|earring)\b",
        r"\b(?:deer|buck)\s+hunting\s+(?:gift|gear)\b",
        r"\bgun\s+(?:deer|keychain|pillow|tumbler|mug)\b",
        r"\bpickaxe\s+(?:prop|model|cosplay)\b",
        r"\bmachete\s+(?:prop|toy)\b",
        r"\btactical\s+(?:cosplay|party)\b",
    ],
    "adult_innuendo": [
        # 成人暗示 (单独 hit, 不依赖 L3)
        r"\bcouples\s+pillow\s+for\s+intimacy\b",
        r"\bintimacy\s+(?:pillow|set|toy|aid)\b",
        r"\bsensual\s+(?:massage|oil|gift)\b",
        r"\berotic\s+(?:art|gift|game)\b",
        r"\bbondage\s+(?:kit|gear|set)\b",
    ],
    "substance_decorative": [
        # 酒精 / 烟草 / 大麻图案 (装饰商品)
        r"\bwine\s+and\s+(?:whisky|cigar)\b",
        r"\bwhisky\s+(?:paint\s+by\s+numbers|on\s+wood)\b",
        r"\bmarijuana\s+(?:leaf|leaves|art|sticker)\b",
        r"\bcannabis\s+(?:leaf|art|sticker)\b",
        r"\bweed\s+(?:leaf|sticker|sign)\b",
    ],
    "cartoon_ip_character": [
        # ===== Disney / Pixar 角色与作品 =====
        r"\b(?:mickey|minnie)\s+mouse\b",
        r"\b(?:donald|daisy)\s+duck\b",
        r"\b(?:goofy|pluto|chip\s+(?:and|&|n)\s+dale)\b",
        r"\bdisney(?:\s+(?:princess|frozen|aladdin|cars))?\b",
        r"\b(?:elsa|anna|olaf|sven)\b",                       # Frozen
        r"\b(?:moana|maui)\b",                                # Moana
        r"\b(?:simba|mufasa|nala|scar|timon|pumbaa|lion\s+king)\b",
        r"\b(?:winnie\s+the\s+pooh|tigger|eeyore|piglet)\b",
        r"\b(?:lilo\s*(?:and|&)?\s*stitch|\bstitch\s+(?:plush|toy|doll|costume|sticker|backpack)|stitch\s+&\s+lilo)\b",
        r"\b(?:woody|buzz\s+lightyear|toy\s+story|jessie\s+toy)\b",
        r"\b(?:nemo|dory|finding\s+(?:nemo|dory))\b",
        r"\b(?:incredibles|wall[-\s]?e|ratatouille|up\s+pixar)\b",
        r"\b(?:lightning\s+mcqueen|cars\s+(?:movie|disney))\b",
        r"\b(?:little\s+mermaid|ariel\s+(?:disney|princess)|cinderella|snow\s+white|rapunzel|tangled|brave\s+pixar|aladdin|jasmine|tiana)\b",
        # ===== Marvel / DC =====
        r"\b(?:marvel|avengers|x[-\s]?men)\b",
        r"\b(?:iron\s+man|spider[-\s]?man|captain\s+america|thor\s+(?:marvel|hammer)|hulk\s+(?:marvel|smash)|black\s+widow|black\s+panther|ant[-\s]?man|doctor\s+strange|deadpool|wolverine|hawkeye|loki\s+marvel|scarlet\s+witch)\b",
        r"\b(?:dc\s+comics|justice\s+league)\b",
        r"\b(?:batman|superman|wonder\s+woman|aquaman|harley\s+quinn|joker\s+dc)\b",
        # ===== Pokemon / Nintendo / Sega =====
        r"\b(?:pokemon|pok[eé]mon|pikachu|charizard|squirtle|bulbasaur|eevee|mewtwo|jigglypuff)\b",
        r"\b(?:super\s+mario|mario\s+(?:bros|kart|party|odyssey)|luigi\s+nintendo|princess\s+peach|bowser|toad\s+nintendo|yoshi)\b",
        r"\b(?:donkey\s+kong|sonic\s+(?:hedgehog|the))\b",
        r"\b(?:zelda|link\s+(?:zelda|nintendo)|breath\s+of\s+the\s+wild)\b",
        # ===== Sanrio / Hello Kitty =====
        r"\b(?:hello\s+kitty|sanrio|kuromi|my\s+melody|cinnamoroll|pompompurin|gudetama|keroppi|pochacco)\b",
        # ===== Studio Ghibli / 日本动漫 =====
        r"\b(?:studio\s+ghibli|totoro|kiki'?s\s+delivery|spirited\s+away|princess\s+mononoke|howl'?s\s+moving)\b",
        r"\b(?:naruto|sasuke\s+naruto|kakashi)\b",
        r"\b(?:one\s+piece|luffy\s+pirate|zoro\s+anime)\b",
        r"\b(?:dragon\s+ball|goku\s+(?:dbz|saiyan)|vegeta\s+dbz)\b",
        r"\b(?:demon\s+slayer|kimetsu\s+no\s+yaiba|tanjiro|nezuko)\b",
        r"\b(?:attack\s+on\s+titan|jujutsu\s+kaisen|gojo\s+satoru|chainsaw\s+man)\b",
        r"\b(?:my\s+hero\s+academia|deku\s+anime)\b",
        # ===== Star Wars / Harry Potter =====
        r"\b(?:star\s+wars|mandalorian|grogu|baby\s+yoda|darth\s+vader|stormtrooper|jedi|sith\s+lord|millennium\s+falcon|kylo\s+ren|rey\s+star\s+wars)\b",
        r"\b(?:harry\s+potter|hogwarts|dumbledore|hermione|gryffindor|slytherin|hufflepuff|ravenclaw|voldemort|wizarding\s+world|fantastic\s+beasts)\b",
        # ===== 儿童动画 =====
        r"\b(?:peppa\s+pig|george\s+pig|paw\s+patrol|chase|skye\s+paw|marshall\s+pup)\b",
        r"\b(?:bluey|cocomelon|coco\s+melon|baby\s+shark|miss\s+rachel)\b",
        r"\b(?:my\s+little\s+pony|mlp\s+(?:plush|toy)|equestria)\b",
        r"\b(?:trolls?\s+(?:movie|world)|despicable\s+me|minions?\s+(?:movie|plush|toy)|gru\s+despicable|shrek\s+(?:movie|donkey))\b",
        r"\b(?:scooby[-\s]?doo|tom\s+(?:and|&)\s+jerry|smurfs)\b",
        r"\b(?:sponge\s*bob|patrick\s+star|squidward|sandy\s+cheeks|bikini\s+bottom)\b",
        r"\b(?:rugrats|teletubbies|sesame\s+street|elmo\s+sesame|big\s+bird|cookie\s+monster\s+sesame)\b",
        r"\b(?:teenage\s+mutant\s+ninja\s+turtles|tmnt|leonardo\s+turtle|donatello\s+turtle|raphael\s+turtle)\b",
        r"\b(?:power\s+rangers|transformers|optimus\s+prime|bumblebee\s+transformers|megatron)\b",
        # ===== Game / Lego =====
        r"\b(?:fortnite|minecraft|roblox|among\s+us|rainbow\s+friends)\b",
        r"\blego\s+(?:set|bricks|block|figure|minifig|kit|movie|character)\b",
        # ===== 其他流行 IP =====
        r"\b(?:five\s+nights\s+at\s+freddy'?s|fnaf|huggy\s+wuggy|poppy\s+playtime)\b",
        r"\b(?:barbie\s+(?:doll|movie|fashion)|bratz\s+doll)\b",
    ],
}

# 编译所有子类的 regex
_R8_COMPILED = {
    subtype: [re.compile(p, re.IGNORECASE) for p in pats]
    for subtype, pats in _R8_SENSITIVE_PATTERNS.items()
}


def _rule_walmart_strict_sensitive(product: ProductInfo) -> list[RuleHit]:
    """输入:产品 → 输出:R8 命中的 hit(0 或 1 条,penalty 0)。

    扫 title + bullet_points[0..2](同 R7,**只前 3 条五点**)。
    每个 pattern 用 **search 而非 finditer**:一条正则只取第一处命中。
    detail.subtypes 是 dict 插入序 = `_R8_SENSITIVE_PATTERNS` 的定义序。
    """
    title = (product.title or "")
    bullets_txt = " ".join((product.bullet_points or [])[:3])
    scan = title + "\n" + bullets_txt
    if not scan.strip():
        return []

    hits_by_type: dict[str, list[str]] = {}
    for subtype, patterns in _R8_COMPILED.items():
        matched = []
        for pat in patterns:
            m = pat.search(scan)
            if m:
                matched.append(m.group(0))
        if matched:
            hits_by_type[subtype] = sorted(set(matched))

    if not hits_by_type:
        return []

    # 扁平化所有匹配短语
    all_matched = sorted({p for lst in hits_by_type.values() for p in lst})
    return [
        RuleHit(
            stage="L2",
            rule_code="walmart_strict_sensitive",
            penalty=R8_PENALTY,
            detail={
                "subtypes": list(hits_by_type.keys()),
                "matches_by_subtype": hits_by_type,
                "matched_phrases": all_matched,
                "walmart_policy": "Offensive Content",
                "note": "Walmart 实际审核会标 offensive, 即使政策文本未明列. 严格合规侧建议 reject.",
            },
        )
    ]


# =============================================================
# public entry
# =============================================================


def evaluate(product: ProductInfo, l1: L1Info, ctx: Any) -> L2Result:
    """输入:产品 + L1 结果 + 数据上下文 ctx → 输出:L2Result(score + hits + pending)。

    顺序:
      1. 起始 100 分
      2. 叠加 l1.hits 的 penalty(**只累加分数,不把 L1 hit 复制进 L2 hits**,
         避免与 AuditOutcome.all_hits 重复入库)
      3. 六条规则按固定顺序**全部执行、不短路**(R1 命中 -100 后 R3 照跑,
         分数可叠到 -200,这是有意的:detail 要收全证据)
      4. 下界保护 -1000
      5. R1 报了"判不了"(PENDING_RULE_CODES)⇒ 置 pending_reason;
         有 -100 的确定拒时 reject 优先(见 L2Result.verdict)

    注:
      - R0 / R2 已删除(2026-08-20 所有者定稿):与 R1 类目白名单三份清单讲同
        一件事,只留 R1 一处维护。
      - R6 (blacklist_compatible_for) 已删除, 改由 L3 LLM 判 (规则硬拦误伤率 90%).
      - R9 (trademark_symbol_in_title) 已迁 Phase0 (phase0_trademark), 这里不调用.
      - 软规则 (R3b/R3c/R4/R5/R7/R8) penalty 均为 0, 不参与累积;
        结论只由 R1/R3a 的 -100 决定 (score_threshold=60 下 100-0=100 ≥ 60 → pass).
    """
    score = 100
    all_hits: list[RuleHit] = []

    # 1. 继承 L1 扣分
    for h in l1.hits:
        score += h.penalty

    # 2. 跑 L2 规则
    rules = [
        _rule_category_gate(l1, ctx),                       # R1 PT 准入双白名单(唯一类目判据)
        _rule_cat_requires_cert(l1, ctx),                   # R3 类目需证书 (硬/软四分支)
        _rule_title_desc_blacklist(product, ctx),           # R4 黑名单品牌 (证据)
        _rule_trademark_live(product, l1, ctx),             # R5 USPTO LIVE 商标 (证据)
        _rule_content_promotional(product),                 # R7 内容宣称 (证据)
        _rule_walmart_strict_sensitive(product),            # R8 严格合规敏感词 (证据)
    ]
    pending_reason = None
    for hits in rules:
        for h in hits:
            all_hits.append(h)
            score += h.penalty
            if pending_reason is None and h.rule_code in PENDING_RULE_CODES:
                pending_reason = h.detail.get("rule") or h.rule_code

    # 3. 下界保护
    if score < -1000:
        score = -1000

    return L2Result(score_final=score, hits=all_hits, pending_reason=pending_reason)


__all__ = [
    "PENDING_RULE_CODES",
    "evaluate",
    "load_nice_mapping",
    "load_nrtl_keywords",
]
