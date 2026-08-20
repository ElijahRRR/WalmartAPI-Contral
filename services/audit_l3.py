"""L3 语义审核(DeepSeek 文本判定;移植自旧仓 pipelines/l3_llm.py + l3_policy_router.py)。

L3 只对 L2 pass(score≥60)的产品跑,补 L2 硬规则抓不到的两类问题:
  1. **品牌真伪** —— L2 R4/R5 命中词是真品牌还是通用英文词;
  2. **冒犯性 / IP 侵权 / 儿童产品 CPC** —— 从 title+bullets+description 语义判。

判定不动分数(RuleHit penalty 恒 0,旧仓 l3_llm.py:690),只决定 verdict:
`reject` → 整品拒;`pending` → 待人工复核;`pass` → 交 L4(若开)。

**prompt 前缀缓存契约**:messages 恒为 `[system, user]`,system prompt 对所有产品
**逐字节相同**(S1 字面量 + S2 候选类目 + S3 分隔段 + S4 37 条政策压缩块),
进程内只构造一次。任何把产品信息拼进 system 的写法都会打散 DeepSeek 前缀缓存,
命中率从 ~95% 掉回 ~63%(旧仓 l3_llm.py:193-198 / :285-289 实测)。

失败语义(计划 10.2 + 批次 C 合同全局节,与旧仓相反):LLM 重试尽 / 坏 JSON /
verdict 取值非法,**一律 verdict='pending'**(旧仓是 pass —— 故障窗口漏审违规
商品的代价远大于人工复核);pending **不写 llm_cache**。

不迁(旧仓 §8 勿迁清单):l3_keywords.yaml 全套与 §B.1/§B.2 动态块(`_blocks_for`
恒 (True, True) 后已死)、`get_system_prompt` 的三个入参、`judge_batch`、
政策路由的 DB 查询(结果只用 category_en,退化为内存表)、跨供应商 failover /
llm_router / reasoning_effort / usage_logger、strong 升级链(合同 L3-3:生产
yaml 两链主节点同为 claude-sonnet-5,能力事实已死)、`_summarize_l2_hits` 的
死分支 brand_blacklist(已移 L0);forbidden_mega_cat / cat_forbid_zh_seller
对应的 L2 R0/R2 已于 2026-08-20 整条删除。

public:
  L3Result, judge_l3, load_policy_rows, load_reason_categories,
  system_prompt, build_system_prompt, build_user_prompt,
  summarize_l2_for_l3, route_policy_hints, parse_l3_reply
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from api import llm as _llm_api
from services import llm_cache
from services.audit_models import RuleHit

logger = logging.getLogger("services.audit_l3")

# 调用参数(旧仓 l3_llm.py:743-756):常规链 max_tokens=1500;温度必须显式传,
# 新仓 api/llm.py 默认 0.2 ≠ 旧仓 0.1,吃默认即改判定口径。
L3_TEMPERATURE = 0.1
L3_MAX_TOKENS = 1500
L3_PURPOSE = "audit_l3"          # registry.LLM_PURPOSE_ENV 登记项


# =============================================================
# 政策路由(旧仓 l3_policy_router.py:30-103,退化为纯内存表)
#
# 旧仓 pick() 逐条 `SELECT 8 列` 只为拿回主键 category_en(政策正文一字未用,
# 全 37 条已在 system prompt 静态段),故新仓不查库。合同 L3-4:保留两张表,
# hint_line 照产(提示词字节稳定 > 省 token)。
# =============================================================

# 每个产品都扫(L3 的核心任务)——永远排在最前,截断也挤不掉
_ALWAYS_INCLUDE = ["Intellectual Property", "Offensive Content"]

# walmart_category(小写,**等值查表**不是子串)→ policy category_en[]
_CATEGORY_ROUTES: dict[str, list[str]] = {
    # 家居类
    "home": ["Home Goods", "PFAS Chemicals"],
    "home improvement": ["Home Goods", "Hazardous Items"],
    "home goods": ["Home Goods"],
    "household": ["Home Goods", "Hazardous Items"],
    "furniture": ["Home Goods"],
    "garden & patio": ["Home Goods", "Plants & Seeds", "Hazardous Items"],
    # 食品饮料 / 宠物 / 膳食 / 化妆品
    "food & beverage": ["Food Products", "Medical Foods"],
    "dietary supplements": ["Dietary Supplements", "Medical Foods", "Food Products"],
    "health & personal care": ["Cosmetic Products", "Drugs & Paraphernalia", "Medical Devices", "Dietary Supplements"],
    "beauty": ["Cosmetic Products", "PFAS Chemicals"],
    "animals": ["Animals", "Pet Products"],
    # 儿童 / 玩具
    "baby": ["Baby Products", "Children's Products", "PFAS Chemicals", "Recalled Products"],
    "toys": ["Children's Products", "General-Use Products", "PFAS Chemicals", "Recalled Products"],
    # 电子 / 汽配
    "electronics": ["Electronics & RF", "Hazardous Items", "Digital Goods"],
    "vehicles": ["Auto & Motor Vehicles", "Hazardous Items"],
    "automotive": ["Auto & Motor Vehicles", "Hazardous Items"],
    # 服饰
    "textiles & apparel": ["Textiles & Apparel", "PFAS Chemicals"],
    "fashion": ["Textiles & Apparel", "PFAS Chemicals"],
    # 运动 / 户外 / 军警
    "sporting goods": ["Military & Law Enforcement", "Ride-Ons & Micromobility", "Hazardous Items"],
    "sports & outdoors": ["Military & Law Enforcement", "Ride-Ons & Micromobility"],
    "safety & emergency": ["Military & Law Enforcement", "General-Use Products"],
    # 其他
    "arts & crafts": ["Art", "General-Use Products"],
    "office": ["General-Use Products"],
    "musical instruments": ["General-Use Products"],
    "business & industrial": ["General-Use Products", "Hazardous Items"],
    "photography": [],
    "media": ["Digital Goods", "Software"],
    "occasion & seasonal": ["Home Goods", "Children's Products"],
    "everything else": [],
    "jewelry/precious metals": ["Jewelry/Precious Metals"],
    "jewelry": ["Jewelry/Precious Metals"],
}

# PT 级关键词路由(**裸子串**,无词边界;可多组同时命中,按表顺序追加)
_PT_KEYWORD_ROUTES: list[tuple[tuple[str, ...], list[str]]] = [
    (("weapon", "gun", "rifle", "ammo", "bullet", "tactical", "body armor", "military", "knife", "blade"),
     ["Military & Law Enforcement"]),
    (("battery", "lithium", "power bank", "18650"),
     ["Hazardous Items"]),
    (("vape", "e-cigarette", "e-liquid", "tobacco", "cigar"),
     ["Tobacco & Vaping"]),
    (("alcohol", "wine", "beer", "spirit"),
     ["Alcohol"]),
    (("supplement", "vitamin", "nutri"),
     ["Dietary Supplements"]),
    (("seed", "plant", "bulb corm", "live plant"),
     ["Plants & Seeds"]),
    (("pet food", "dog food", "cat food", "pet treat"),
     ["Pet Products"]),
    (("baby", "infant", "toddler"),
     ["Baby Products", "Children's Products"]),
    (("coin", "currency", "bullion", "precious metal"),
     ["Jewelry/Precious Metals"]),
    (("medical", "surgical", "diagnostic", "therapy device"),
     ["Medical Devices"]),
    (("sticker", "decal", "patch", "flag", "banner"),
     ["Offensive Content"]),  # 这类最容易误印种族/政治/宗教
    (("costume", "cosplay", "halloween"),
     ["Offensive Content", "Children's Products"]),
    (("couples", "adult", "intimacy", "sensual", "erotic"),
     ["Offensive Content"]),
]

ROUTE_MAX_POLICIES = 5   # 旧仓 pick 默认 6,唯一调用点传 5(l3_llm.py:724-728)


def route_policy_hints(walmart_category: str | None, walmart_pt: str | None,
                       *, max_policies: int = ROUTE_MAX_POLICIES,
                       known: frozenset | set | None = None) -> list[str]:
    """输入:walmart_category + PT → 输出:最相关政策 category_en 列表(≤max_policies)。

    顺序 = always_include(IP + Offensive)→ category 等值路由 → PT 子串路由;
    去重保序,**先截断再过滤**(旧仓 l3_policy_router.py:193 截断发生在读 DB 之前,
    always_include 永不被挤掉)。`known` 传入时,表里写了但库里没有的 category_en
    静默跳过(旧仓 `_load_policy` 返回 None 即跳过,l3_policy_router.py:196-198)。
    """
    cat_norm = (walmart_category or "").strip().lower()
    pt_norm = (walmart_pt or "").strip().lower()

    seen: set[str] = set()
    ordered: list[str] = []

    def _add(c_en: str) -> None:
        if c_en and c_en not in seen:
            seen.add(c_en)
            ordered.append(c_en)

    for c_en in _ALWAYS_INCLUDE:
        _add(c_en)
    for c_en in _CATEGORY_ROUTES.get(cat_norm, []):
        _add(c_en)
    for kws, policies in _PT_KEYWORD_ROUTES:
        if any(k in pt_norm for k in kws):
            for c_en in policies:
                _add(c_en)

    ordered = ordered[:max_policies]
    if known is None:
        return ordered
    out = [c for c in ordered if c in known]
    if len(out) != len(ordered):
        logger.debug("L3 路由跳过库中不存在的政策:%s",
                     [c for c in ordered if c not in known])
    return out


# =============================================================
# system prompt(S1 字面量 + S2 候选类目 + S3 分隔段 + S4 政策块)
# =============================================================

# S1:旧仓 l3_llm.py:295-431 `base` 字面量,逐字节移植(脚本切片,勿手改)。
_S1 = """你是沃尔玛 Marketplace 合规审核 AI (站在沃尔玛官方立场)。
卖家是中国搬运模式、无任何证书/认证、每日数万产品。
你只输出严格 JSON, 不要任何解释文字或 markdown 前后缀。

# 业务关键约束
- 卖家 = 中国搬运模式, 客观上不可能拥有 Nike/Dyson/Disney 等大牌的
  Official / Licensed / Authorized / OEM 授权.
- 任何"授权声明"都视为虚假宣称, 一律按侵权判 (无豁免).
- 默认 pass — 只有清晰证据才 reject.

# 政策匹配的两类 (重要)

判 reject 时, 与下方"沃尔玛 37 条 Prohibited Products Policy 全清单"做语义匹配,
按下面两类来判 — **不要把"严格证据"误读为"标题必须明示违规用途"**:

## A. 品类/设备/物料整体禁售 (政策直接禁该物本身, 不论用途)
当政策 prohibited_items 列出**具体产品品类/设备名/物料名** → 产品本质就是这类东西即 reject:
  - 例: Policy 1 Alcohol 禁"蒸馏设备" → 产品 = Distillation Apparatus 直接 reject
        (即使标题写 "for essential oil / water purifier", 设备本身就是禁的)
  - 例: Policy 17 Hazmat 禁"烟花/灭火器/18650 锂电" → 产品 = 这些即 reject
  - 例: Policy 37 Tobacco 禁"vape pen/hookah pen" → 产品 = 这些即 reject
  - 例: Policy 5 Auto 禁"DPF/SCR/EGR delete kit" → 产品 = delete kit 即 reject
  - 例: Policy 23 Military 禁"防弹衣/战术头盔/手铐/警徽" → 产品 = 这些即 reject

## B. 用途/特征敏感 (政策禁的是子类型/特征, 需文本佐证)
当政策禁的是产品的**特定属性/用途/年龄段** (而非整个品类), 必须文本里有该属性证据:
  - 例: Home Goods 禁"有绳窗帘 (corded window blinds)" → 必须含 "corded";
        "cordless" 反而合规 (CPSC 推荐)
  - 例: Children's Products 需 CPC → 必须 ≤12 岁儿童用品 (标 "for adult" 不算)
  - 例: Hazardous 含"EPA-regulated" → 必须真化学品/农药; 命名 "clean" 不算

## 证据要求
- 类型 A: reason_text 写"产品本质是 [禁售品类], 政策 [Policy N] 直接禁此类"
- 类型 B: reason_text 必须引用产品原文证据 (title/bullets 原句)
- 不命中: verdict=pass (默认保守)

# 你的判定职责 (4 个维度)

## 1. 品牌真伪 (针对 user prompt 列出的"L2 命中词")
用完整 title/bullets/description 上下文综合判断每个命中词:
- 是真品牌 → is_real_brand=true
- 是通用英文词 (如 top/floor/summer/modern/classic 等) → is_real_brand=false

任一命中词 is_real_brand=true → 整品 reject, reason_category='Intellectual Property'.

特殊语法铁证 (X 必为品牌, 必判 true):
  "compatible for X" / "fits X" / "replacement for X" / "works with X" /
  "designed for X" / "OEM for X" / 品牌名后紧跟型号 (如 "Dyson V6")

## 2. 冒犯性内容 (Walmart Offensive Content 政策)
从 title/bullets/description 语义判, 命中以下任一 → reject:
- 色情 / 成人主题 / 性化未成年 / 捆绑 / 性玩具
- 种族辱骂 / 仇恨象征 (Confederate / Nazi / Swastika / KKK)
- 恶搞冒犯 (funny + offensive 混合 / 嘲讽政治人物)
- 仿真武器 / 玩具枪 / replica firearm (例外: 水枪 / 泡泡枪 /
  bright orange tip 合规标识 / NERF foam dart 品牌豁免)

## 3. 知识产权 (Intellectual Property)
任一 → reject, reason_category='Intellectual Property':
- 商标仿冒: 已通过维度 1 处理
- 版权 IP 角色: 卡通 / 电影 / 游戏 / 动漫 IP 名 + 周边商品
  (贴纸 / T恤 / 毛绒 / 玩具 / 海报 / 杯子 / 钥匙扣)
- 外观/发明专利: 文本明示仿造 ("Stanley Cup style" / "AirPods case for Apple")
- 商业包装 (Trade Dress): 仿知名整体视觉 (可口可乐瓶形 / Tiffany 蓝盒)
- 肖像权 (Publicity Rights): 名人姓名 + 周边商品
  (政治人物 / 演员 / 运动员 / 音乐人 / Kpop 艺人)
- 假冒: "100% Authentic <大牌>" + 显著低价

## 4. 品牌字段伪装 (brand_misuse)
brand 填 Unbranded/Generic/小品牌, 但 title/描述暗示某大牌
→ reject, reason_category='brand_misuse'

## 5. 儿童产品 / CPC 兜底 (重要 — 即便 L1 PT 没标 CPC 也要拦)
背景: 美国 CPSIA 法规要求所有 ≤12 岁儿童产品必须有 CPC (Children's Product
Certificate) + ASTM/CPSC 实验室测试报告. 沃尔玛会强制要求卖家提供, 无 CPC
→ 上架被警告 / 下架 / 罚款.

L1 类目映射经常因为亚马逊源头分类错误而漏判 (例如 "Baby Bodysuit Extender"
被亚马逊放在 Sewing Fasteners 类目, L1 选了 Sewing Fasteners 这个非儿童 PT
→ requirements 字段没 CPC → L2 不会触发软合规警告).

判定步骤:
1. 看 title / bullets / description 是否暗示 **≤12 岁儿童使用**:
   - 显式年龄: "for kids", "for children", "ages 3+", "infant", "toddler",
              "baby", "newborn", "婴儿", "儿童", "小孩"
   - 儿童专用形态: "onesie", "bodysuit", "bib", "diaper", "stroller",
              "crib", "high chair", "baby gate", "car seat", "pacifier",
              "sippy cup", "training pants", "swaddle", "burp cloth"
   - 玩具/教具: "squishy", "plush toy", "stuffed animal", "fidget toy",
              "slime kit", "sticker book for kids", "kid's craft kit",
              "play set", "playmat", "play tent", "kid's drawing"
   - 儿童规格描述: "BPA-free baby bottle", "non-toxic for toddlers" 等
2. 排除明确成人/兽用情形:
   - "for adult", "adult onesie", "for pets", "dog onesie" → 不算儿童产品
   - "Baby Pillow for Mom Comfort" (产品是给妈妈用的, 不是给婴儿) → 不算
   - "Baby's Breath Flower" (婴儿之吻花卉) → 文字含 baby 但是植物 → 不算
   - 修车工具 "infant car seat protector" 给汽车用 → 不算 (但接近边界, 谨慎)

   **特别注意 — 这些情况打着 "DIY/craft/adult" 旗号但仍算儿童产品**:
   - "DIY Squishy Kit" / "DIY Slime Kit" — 成品 squishy/slime 是 ≤12 岁儿童玩具,
     即使 kit 是大人帮做, 沃尔玛仍判 needs_cpc.
   - "Adult Putty Toy" — 沃尔玛把所有 putty/slime 类减压玩具判 children product,
     因为流行儿童市场.
   - "Plush Animal for Home Decor" — 即使描述是装饰品, 毛绒玩偶仍要 CPC.
   - 规则: 成品形态是儿童典型玩具的 (squishy/slime/plush/fidget/play dough/stuffed
     animal), **不管文案怎么写**, 一律判 reject + "Children's Products".
3. 命中且无明确成人/兽用排除 → verdict='reject',
   reason_category="Children's Products" (或 "Baby Products" 如果 < 3 岁专用),
   reason_text 必须明确指出: "<产品类别>属于儿童用品, 需 CPC + ASTM 认证, 卖家
   未提供 → 上架会被警告". 必须引用 title 原文证据.

# 输出规范 (严格 JSON)

{
  "verdict": "pass" | "reject",
  "reason_category": "<候选之一; verdict=pass 时必须是 'none'>",
  "reason_text": "<=50字中文简短原因 (verdict=pass 时可留空)",
  "signals": {
    "has_trademark_symbol": true|false,
    "has_authorization_claim": true|false,
    "offensive_signals": ["<具体信号>",...]
  },
  "blacklist_brand_verdict": [
    {"brand": "<命中词>", "is_real_brand": true|false, "evidence": "<简短理由>"}
  ],
  "llm_confidence": "high" | "medium" | "low"
}

# 约束
- 默认 verdict=pass, 只有清晰证据才 reject
- reason_category 必须从下方"候选 reason_category 列表"里选一项
- verdict=pass 时 reason_category 必须是 "none"
- blacklist_brand_verdict: 对 L2 R4/R5 命中词每个都要 verdict (最多 10 个)
- 不凭空添加未列出的品牌

# 候选 reason_category (verdict=reject 时必选其一)
"""

# S3:旧仓 l3_llm.py:432-438 分隔字面量(首行是空行),逐字节移植。
_S3 = """

# 沃尔玛 37 条 Prohibited Products Policy 全清单 (LLM 训练数据外的内部规则)

判产品是否违规时与下面清单做语义匹配, 命中任一 → reason_category 填对应 category_en.
不命中 → 默认 verdict=pass.

"""

POLICY_ROWS_SQL = (
    "SELECT id, category_en, category_zh, overall_status, "
    "prohibited_items, zh_seller_risk, zh_seller_notes "
    "FROM audit.walmart_prohibited_policy ORDER BY id"
)
# ORDER BY id / ORDER BY category_en 都不可省:顺序即前缀缓存命中率(§11.2)
REASON_CATEGORIES_SQL = (
    "SELECT category_en FROM audit.walmart_prohibited_policy "
    "WHERE category_en IS NOT NULL ORDER BY category_en"
)


def load_policy_rows(conn) -> list[dict]:
    """输入:中心库连接 → 输出:37 条政策行 dict 列表(ORDER BY id,给 S4)。"""
    with conn.cursor() as cur:
        cur.execute(POLICY_ROWS_SQL)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def load_reason_categories(conn) -> list[str]:
    """输入:中心库连接 → 输出:category_en 列表(库序 ORDER BY category_en,给 S2)。

    不用 `sorted(ctx.known_policies)`:PG 排序规则与 Python 码点序不同
    (如 'Pet Products' / 'PFAS Chemicals' 两序相反),用库序才与旧仓同字节。
    """
    with conn.cursor() as cur:
        cur.execute(REASON_CATEGORIES_SQL)
        return [r[0] for r in cur.fetchall() if r[0]]


def format_reason_categories(categories: list[str]) -> str:
    """输入:37 条 category_en(库序)→ 输出:S2 候选块(每行 `  - {c}`)。

    末尾固定追加 brand_misuse / none 两项(旧仓 l3_llm.py:276)。
    """
    cats = list(categories) + ["brand_misuse", "none"]
    return "\n".join(f"  - {c}" for c in cats)


def format_full_policy_block(rows: list[dict]) -> str:
    """输入:政策行(ORDER BY id)→ 输出:S4 压缩块文本(逐条截断长度是移植契约)。

    截断常量 50/30/240/80 逐字迁自旧仓 l3_llm.py:239-242。⚠ 旧仓注释自称
    "每条 < 150 字"与实际不符(prohib 单项就允许 240),以截断常量为准。
    """
    parts: list[str] = []
    for r in rows:
        cat_en = (r.get("category_en") or "").strip()
        cat_zh = (r.get("category_zh") or "").strip()
        status = (r.get("overall_status") or "").strip().replace("\n", " ")[:50]
        risk = (r.get("zh_seller_risk") or "").strip().replace("\n", " ")[:30]
        prohib = (r.get("prohibited_items") or "").replace("\n", " ").replace("•", "/").strip()[:240]
        notes = (r.get("zh_seller_notes") or "").replace("\n", " ").strip()[:80]

        line = f"## {r['id']}. {cat_en} ({cat_zh})"
        if status:
            line += f"\n状态: {status}"
            if risk:
                line += f" | 中国卖家:{risk}"
        if prohib:
            line += f"\n禁: {prohib}"
        if notes and "红" in risk:  # 仅高风险政策保留卖家备注 (节省 token)
            line += f"\n备注: {notes}"
        parts.append(line)
    return "\n\n".join(parts)


def build_system_prompt(categories: list[str], policy_rows: list[dict]) -> str:
    """输入:候选类目(库序)+ 政策行 → 输出:完整 system prompt(S1+S2+S3+S4)。

    零产品入参 —— 旧仓 `get_system_prompt(cat, pt, hint)` 的三个参数全被
    `_blocks_for` 吞掉(恒 (True, True)),新仓不保留死签名。
    """
    return _S1 + format_reason_categories(categories) + _S3 + format_full_policy_block(policy_rows)


_SYSTEM_PROMPT: str | None = None


def system_prompt(conn) -> str:
    """输入:中心库连接 → 输出:进程级缓存的 system prompt(只查一次 DB)。"""
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = build_system_prompt(load_reason_categories(conn),
                                             load_policy_rows(conn))
        logger.info("L3 system prompt 构造完成:%d 字符", len(_SYSTEM_PROMPT))
    return _SYSTEM_PROMPT


def reset_prompt_cache() -> None:
    """输入:无 → 输出:无(清空 system prompt 进程缓存,仅测试/长驻进程刷新用)。"""
    global _SYSTEM_PROMPT
    _SYSTEM_PROMPT = None


# =============================================================
# L2 软证据 → user prompt(旧仓 _summarize_l2_hits,l3_llm.py:486-518)
# =============================================================

MAX_BRANDS = 10          # R4/R5 命中词前 10 个(system prompt 也这么要求 LLM)
MAX_BULLETS = 5
MAX_DESC_CHARS = 600
MAX_NOTES_CHARS = 200


def summarize_l2_for_l3(l2) -> tuple[str, list[str]]:
    """输入:L2Result → 输出:(L2 命中摘要文本, R4/R5 品牌词前 10 个)。

    死分支不迁(旧仓 §8-15):brand_blacklist(R1 已移 Phase0,且 phase0 命中根本
    不进 L3);forbidden_mega_cat / cat_forbid_zh_seller 对应的 R0/R2 已于
    2026-08-20 删除。

    ⚠ 2026-08-20 补上两处丢证据(此前是"照迁旧缺陷"):
      · R7 `content_promotional` 与 R8 `walmart_strict_sensitive` **原先完全不进
        prompt** —— L2 在 detail 里写着"L3 LLM 需判断宣称词是否有事实依据",
        而 L3 根本没收到,只能自己从原文重看一遍。承诺了没送到,比不承诺更糟。
      · cert 分支取的是 `detail['requirements']`,这个键在 L2 三种 cert hit 里
        **一个都不存在**(真实键是 meta_requirements / hard_cert_fields /
        soft_cert_fields),于是前两档永远退化成一句固定套话 note。
    """
    lines: list[str] = []
    r4_brands: list[str] = []
    for h in l2.hits:
        if h.rule_code == "title_desc_blacklist":
            matches = h.detail.get("matches", [])[:MAX_BRANDS]
            names = [m.get("brand", "") for m in matches if m.get("brand")]
            r4_brands.extend(names)
            pairs = [f"{m.get('brand')}(原文:{m.get('matched_phrase')})" for m in matches]
            lines.append(f"* 标题/描述命中黑名单(R4, 共{h.detail.get('count', len(matches))}个, 前10): {', '.join(pairs)}")
        elif h.rule_code.startswith("cat_requires_cert"):
            d = h.detail
            what = (d.get("meta_requirements") or d.get("hard_cert_fields")
                    or d.get("soft_cert_fields") or d.get("note"))
            lines.append(f"* 类目需证书({h.rule_code}): {what}")
        elif h.rule_code == "trademark_live":
            marks = h.detail.get("matched_marks", [])[:MAX_BRANDS]
            lines.append(f"* USPTO LIVE 商标(R5, 前10): {', '.join(marks)}")
            r4_brands.extend(m.lower() for m in marks if m)
        elif h.rule_code == "content_promotional":
            d = h.detail
            phrases = (d.get("strong_phrases") or []) + (d.get("allcaps_runs") or [])
            tag = "仅空洞形容词" if d.get("soft_only") else "含无据宣称/全大写滥用"
            lines.append(
                f"* 促销宣称(R7, {tag}): "
                f"{', '.join((phrases or d.get('soft_phrases') or [])[:MAX_BRANDS])}")
        elif h.rule_code == "walmart_strict_sensitive":
            d = h.detail
            subtypes = ", ".join(d.get("subtypes") or [])
            phrases = (d.get("matched_phrases") or [])[:MAX_BRANDS]
            lines.append(
                f"* 敏感/严格合规(R8, {subtypes}): {', '.join(str(t) for t in phrases)}")
    return (
        ("\n".join(lines) if lines else "(L2 无命中)"),
        list(dict.fromkeys(r4_brands))[:MAX_BRANDS],
    )


def build_user_prompt(product, l1, l2, *, pt_notes: str | None = None,
                      routed_cats: list[str] | None = None) -> str:
    """输入:产品 + L1 + L2(+ PT 备注/路由提示)→ 输出:user prompt 文本。

    模板逐字迁自旧仓 l3_llm.py:559-582。⚠ 已知缺陷照迁(spec §3.4 / OPEN-9,
    合同 L3-9):`原产国` 行保留但值恒 `(空)` —— 新仓 ProductInfo 无
    country_of_origin 字段,采集契约也无此值;删行会改 user prompt 模板口径。
    """
    bullets_txt = "\n".join(f"  - {b}" for b in product.bullet_points[:MAX_BULLETS]) or "  (无)"
    desc = (product.long_description or "").strip()
    if len(desc) > MAX_DESC_CHARS:
        desc = desc[:MAX_DESC_CHARS] + "...(已截断)"

    l2_txt, r4_brands = summarize_l2_for_l3(l2)

    brand_list = (
        "\n".join(f"  - {b}" for b in r4_brands)
        if r4_brands
        else "  (无 R4/R5 命中, 跳过品牌真伪判定)"
    )

    # walmart_pt_meta.notes —— 飞书人工标注(如 "⚠️T&S抽查48h内");
    # 新仓零 DB 访问,由 ctx.pt_meta 带入(旧仓 _get_pt_notes 独立查询不迁)
    notes_line = (
        f"\n飞书人工标注 (pt_meta.notes): {pt_notes[:MAX_NOTES_CHARS]}"
        if pt_notes
        else ""
    )

    cats = routed_cats or []
    hint_line = (
        f"# 政策路由提示 (本产品 PT/category 最相关的政策, 优先核对): {', '.join(cats[:5])}\n\n"
        if cats
        else ""
    )

    country_of_origin = ""   # 已知缺陷照迁:采集契约无此值,恒 '(空)'
    return f"""{hint_line}# 产品信息

ASIN: {product.asin}
标题: {product.title}
品牌字段(商家填报): {product.brand or '(空)'}
原产国: {country_of_origin or '(空)'}
Amazon 类目: {product.amazon_category_path or '(空)'}
沃尔玛 PT: {l1.walmart_product_type or '(空)'} (置信 {l1.pt_confidence}, 源 {l1.pt_source})
沃尔玛 Category: {l1.walmart_category or '(空)'}{notes_line}

五点描述:
{bullets_txt}

长描述:
{desc or '(空)'}

# L2 规则引擎命中

{l2_txt}

# 待评估的品牌/商标词 (来自 L2 R4+R5, 前 10 个, 综合上下文判 is_real_brand)

{brand_list}
"""


# =============================================================
# 回复解析
# =============================================================


@dataclass
class L3Result:
    """L3 语义审核结果(对应 audit_runs 的 l3_verdict / l3_reason_* 三列)。

    verdict:'pass' / 'reject' / 'pending';hits 至多一条且 penalty 恒 0
    (L3 不动分数,score_final 始终是 L2 的值)。raw 是 LLM 原始 dict,不落库。
    """

    verdict: str
    reason_category: str = "none"
    reason_text: str | None = None
    blacklist_brand_verdict: list = field(default_factory=list)
    llm_confidence: str = "medium"
    raw: dict = field(default_factory=dict)
    hits: list[RuleHit] = field(default_factory=list)


_VALID_VERDICTS = {"pass", "reject"}
_VALID_CONFIDENCE = {"high", "medium", "low"}
_SLUG_RE = re.compile(r"[^a-z0-9]+")

MAX_REASON_TEXT = 500
MAX_RAW_SNIPPET = 300
MAX_ERROR_SNIPPET = 500

# 旧标签兼容(旧仓 l3_llm.py:636-641)
_LEGACY_CATEGORY_MAP = {
    "ip_infringement": "intellectual property",
    "offensive":       "offensive content",
    "forbidden_cat":   "none",   # forbidden_cat 无对应 Walmart 政策, 降级
    "needs_cert":      "none",   # 证书由 L2 R3 处理
}


def valid_reason_categories(known_policies) -> set[str]:
    """输入:37 条政策 category_en 集合 → 输出:reason_category 白名单(小写)。

    全 37 条 + brand_misuse + none,**与路由结果无关**(旧仓
    `_valid_reason_categories(routed_policies)` 的入参 docstring 自述已不使用)。
    """
    return {str(c).lower() for c in known_policies} | {"brand_misuse", "none"}


def _slug_category(cat: str) -> str:
    """输入:category_en → 输出:rule_code 用 slug('Home Goods' → 'home_goods')。"""
    return _SLUG_RE.sub("_", (cat or "").lower()).strip("_") or "unknown"


def _pending(reason_text: str | None, rule_code: str, detail: dict,
             raw: dict) -> L3Result:
    """输入:失败要素 → 输出:pending 形态 L3Result(penalty=0 的一条告警 hit)。"""
    return L3Result(
        verdict="pending",
        reason_category="none",
        reason_text=reason_text,
        llm_confidence="low",
        raw=raw,
        hits=[RuleHit(stage="L3", rule_code=rule_code, penalty=0, detail=detail)],
    )


def parse_l3_reply(raw: dict, valid_categories: set[str]) -> L3Result:
    """输入:LLM JSON dict + 类目白名单 → 输出:规范化 L3Result(坏输出→pending)。

    执行顺序逐字迁自旧仓 `_coerce_result`(l3_llm.py:615-709),两处按计划 10.2
    /10.6 修正:verdict 取值非法与坏 JSON 从"默认 pass"改判 **pending**
    (绝不默认放行)。
    """
    if not raw or "_raw" in raw:
        logger.warning("L3 LLM 返回非法 JSON:%r", str(raw)[:200])
        return _pending("L3 LLM 返回格式异常", "llm_bad_json",
                        {"raw": str(raw)[:MAX_RAW_SNIPPET]}, raw or {})

    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in _VALID_VERDICTS:
        # 【新系统修正】旧仓此处默认 pass(l3_llm.py:625-628)——事故面
        logger.warning("L3 verdict 非法:%r → pending(绝不默认放行)", verdict)
        return _pending("L3 verdict 取值非法", "llm_bad_json",
                        {"verdict": verdict, "raw": str(raw)[:MAX_RAW_SNIPPET]},
                        raw)

    category_raw = str(raw.get("reason_category") or "").strip()
    category_lower = category_raw.lower()
    if category_lower not in valid_categories:
        mapped = _LEGACY_CATEGORY_MAP.get(category_lower)
        if mapped and mapped in valid_categories:
            category_lower = mapped
        else:
            logger.warning("L3 reason_category %r 不在候选列表, 降级 (verdict=%s)",
                           category_raw, verdict)
            category_lower = "none" if verdict == "pass" else "intellectual property"

    if verdict == "pass":            # pass 时强制 category=none(prompt 约束)
        category_lower = "none"

    reason_text = str(raw.get("reason_text") or "").strip()[:MAX_REASON_TEXT] or None

    brand_verdict = raw.get("blacklist_brand_verdict") or []
    if not isinstance(brand_verdict, list):
        brand_verdict = []

    # ⭐ 强制覆盖(合同 L3-7 照迁):任一命中词被判 is_real_brand=true → 整品 reject,
    # 即使 LLM 自己输出 pass。严格 `is True`,字符串 "true" 不算(旧生产口径)。
    real_brand_hits = [v for v in brand_verdict
                       if isinstance(v, dict) and v.get("is_real_brand") is True]
    if verdict == "pass" and real_brand_hits:
        first = real_brand_hits[0].get("brand") or "?"
        logger.info("L3 verdict override: pass→reject, is_real_brand=true: %s",
                    [v.get("brand") for v in real_brand_hits])
        verdict = "reject"
        category_lower = "intellectual property"
        if not reason_text:
            reason_text = f"[Trademark] 未授权引用品牌名 {first}"

    signals = raw.get("signals") or {}
    if not isinstance(signals, dict):
        signals = {}

    confidence = str(raw.get("llm_confidence") or "medium").strip().lower()
    if confidence not in _VALID_CONFIDENCE:
        confidence = "medium"

    hits: list[RuleHit] = []
    if verdict == "reject":
        rule_code = (f"llm_{_slug_category(category_lower)}"
                     if category_lower != "none" else "llm_reject")
        hits.append(RuleHit(
            stage="L3",
            rule_code=rule_code,
            penalty=0,   # L3 不扣分, 直接决定 verdict
            detail={     # 五键定序是落库契约
                "reason_category": category_lower,
                "reason_text": reason_text,
                "llm_confidence": confidence,
                "signals": signals,
                "blacklist_brand_verdict": brand_verdict,
            },
        ))

    return L3Result(
        verdict=verdict,
        reason_category=category_lower,
        reason_text=reason_text,
        blacklist_brand_verdict=brand_verdict,
        llm_confidence=confidence,
        raw=raw,
        hits=hits,
    )


# =============================================================
# 入口
# =============================================================


def judge_l3(product, l1, l2, ctx, conn) -> L3Result:
    """输入:产品 + L1Info + L2Result + AuditContext + 连接 → 输出:L3Result。

    调用方(audit_rules.audit_one)保证只对 L2 pass 的产品调用,本函数不再检查。
    流程:内存路由取 hint → 进程级 system prompt → 组 user prompt →
    llm_cache 查(键含 model/温度/max_tokens/purpose)→ 未命中则调 chat_json →
    解析。**pending 不写缓存**(旧仓同款:失败/异常/坏 JSON 都不写)。

    失败一律 pending(合同全局节):重试尽(RuntimeError)→ `llm_chain_exhausted`;
    其他异常(4xx / 坏 JSON 抛 ValueError 等)→ `llm_error`;
    坏 JSON / verdict 非法 → `llm_bad_json`。三者 penalty 均 0,分数保留 L2 值。
    """
    routed = route_policy_hints(l1.walmart_category, l1.walmart_product_type,
                                known=ctx.known_policies or None)
    pt = l1.walmart_product_type or ""
    notes = (ctx.pt_meta.get(pt) or {}).get("notes") if pt else None
    pt_notes = (str(notes).strip() or None) if notes else None

    messages = [
        {"role": "system", "content": system_prompt(conn)},
        {"role": "user", "content": build_user_prompt(
            product, l1, l2, pt_notes=pt_notes, routed_cats=routed)},
    ]
    allowed = valid_reason_categories(ctx.known_policies)

    key = llm_cache.cache_key(messages, L3_TEMPERATURE, L3_MAX_TOKENS,
                              purpose=L3_PURPOSE)
    cached = llm_cache.get(conn, key)
    if cached is not None:
        return parse_l3_reply(cached, allowed)

    try:
        raw = _llm_api.chat_json(messages, temperature=L3_TEMPERATURE,
                                 max_tokens=L3_MAX_TOKENS, purpose=L3_PURPOSE)
    except RuntimeError as e:      # api/llm.py 重试尽 → 旧仓 F1 语义
        logger.error("L3 LLM 重试尽 for %s: %s —— verdict=pending 待人工复核",
                     product.asin, e)
        return _pending("LLM 全链路故障, 待人工复核", "llm_chain_exhausted",
                        {"error": str(e)[:MAX_ERROR_SNIPPET]},
                        {"error": str(e)[:MAX_ERROR_SNIPPET]})
    except Exception as e:         # noqa: BLE001 —— 4xx / 坏 JSON / 其他
        # 【新系统修正】旧仓此处 pass(l3_llm.py:776-793)——故障不该放行
        logger.error("L3 LLM 调用失败 for %s: %s —— verdict=pending", product.asin, e)
        return _pending(None, "llm_error",
                        {"error": str(e)[:MAX_ERROR_SNIPPET]},
                        {"error": str(e)[:MAX_ERROR_SNIPPET]})

    result = parse_l3_reply(raw, allowed)
    if result.verdict != "pending":
        llm_cache.put(conn, key, raw, purpose=L3_PURPOSE)
    return result


__all__ = [
    "L3Result",
    "L3_TEMPERATURE",
    "L3_MAX_TOKENS",
    "L3_PURPOSE",
    "judge_l3",
    "load_policy_rows",
    "load_reason_categories",
    "system_prompt",
    "build_system_prompt",
    "build_user_prompt",
    "format_full_policy_block",
    "format_reason_categories",
    "summarize_l2_for_l3",
    "route_policy_hints",
    "valid_reason_categories",
    "parse_l3_reply",
    "reset_prompt_cache",
]
