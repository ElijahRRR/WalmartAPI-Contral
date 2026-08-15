"""L4 视觉审核(火山方舟豆包多图判定;移植自旧仓 pipelines/l4_vision.py +
pipelines/aggressive_offensive.py 的 L4 用途段)。

L4 是编排尾部的**增量视觉拦截**:只对当前 verdict 仍为 pass 的产品跑,且只能
把 pass 翻成 reject(永不把 reject 翻回 pass —— 它根本不对 reject 产品运行)。
**入口条件由调用方(audit_rules.audit_one)保证,本模块不查库判状态。**

不动分数:RuleHit penalty=-100 只是 audit_hits 明细里的一个数字,score_final
在 L2 阶段已定稿,此后没有任何代码重算(旧仓既有语义,不修)。故 L4 reject 的
产品落库分数通常仍 ≥60 = "分数够但被视觉拦下"。

判定确定性(旧仓 l4_vision.py:44-47 原始理由):温度必须 0.0 —— temperature>0
会让 VLM 对边缘 case(场景图里的小 logo)有 variance,同一 ASIN 一次 pass 一次
reject 无法闭环。verdict **由本地 image_issues 重算,模型自报的 verdict 字段
完全忽略**(§6.2,有意的确定性设计)。

图片来源(合同 L4-2):catalog.snapshots 最新一条**含图**快照的 `raw.slow.images[]`
(契约:首图=主图);不套 24h 新鲜度门槛(图不像价格易变)。因来源已是保序去重的
字符串数组,旧仓 main/other 分桶在新仓退化为"保持原序",顺带消掉旧仓
image_index 错位风险的一半。

失败语义(合同 L4-8,**与 L1/L3 的 pending 相反,唯一例外**):五条失败路径
(无图 / 全部下载失败 / 调用失败 / 坏 JSON / schema 违约)一律 **verdict='pass'
维持原结论 + 一条 penalty=0 的告警 hit + 日志**。理由(旧仓 l4_vision.py:347-348
成立):L4 只做增量视觉拦截、默认关、仅对 pass 产品跑,视觉端点故障不该制造假阳。
**本模块绝不产出 pending。**

不迁(spec §9):llm_router / llm_routes 多供应商路由全套、豆包→qwen-vl 跨供应商
failover、DashScope / VISION_PROVIDER 开关、usage_logger 记账、judge() 的死参数
l2(旧仓函数体零引用)、vision_json 的旧签名兼容分支、`_VISION_DEFAULT_SYSTEM`、
`_json_from_text` 三级解析(api/llm._extract_json 已有等价实现)。
L4 **不走 llm_cache**(合同 L4-4:旧仓不缓存,base64 键不可行)。

public:
  L4Result, judge_l4, load_product_images, select_l4_images,
  should_aggressive_offensive, build_l4_user_prompt,
  L4_SYSTEM_PROMPT, L4_AGGRESSIVE_ADDON
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from api import llm_vision
from services.audit_models import RuleHit

logger = logging.getLogger("services.audit_l4")

# 调用参数(旧仓 l4_vision.py:47、356;settings.py:240)
L4_TEMPERATURE = 0.0
L4_MAX_TOKENS = 1024
L4_MAX_IMAGES = 5            # 旧 env L4_MAX_IMAGES_PER_PRODUCT,生产从未覆盖 → 模块常量

MAX_ERROR_SNIPPET = 500
MAX_RAW_SNIPPET = 200


# =============================================================
# 图片取数(合同 L4-2)
# =============================================================

# 主源 = catalog.products.slow->'images'(评审 P0 修正:采集契约 v1 里 slow
# 是 record 顶层必填段,由 product_ingest 落进 products.slow;snapshots.raw
# 是"可选、裁剪后的原始载荷"**不保证内嵌 slow**——按 raw 取会让 L4 恒无图、
# 整层静默空转,listing_plan.md:162 记过同款事故)。amz_source._images 同源。
_IMAGES_SQL = """
SELECT p.slow -> 'images'
FROM catalog.products p
WHERE p.marketplace = %s AND p.asin = %s
  AND jsonb_typeof(p.slow -> 'images') = 'array'
  AND jsonb_array_length(p.slow -> 'images') > 0
"""

# 兜底 = 最新一条 raw 内嵌 slow.images 的快照(老行/契约外形态)。真兜底
# 三要件(CLAUDE.md):藏在同一函数内、触发记日志计数、条件封闭非 catch-all
_IMAGES_FALLBACK_SQL = """
SELECT s.raw -> 'slow' -> 'images'
FROM catalog.snapshots s
WHERE s.marketplace = %s AND s.asin = %s
  AND jsonb_typeof(s.raw -> 'slow' -> 'images') = 'array'
  AND jsonb_array_length(s.raw -> 'slow' -> 'images') > 0
ORDER BY s.scraped_at DESC
LIMIT 1
"""


def load_product_images(conn, asin: str, marketplace: str = "US") -> list[str]:
    """输入:连接 + ASIN → 输出:产品图片 URL 列表(原序,首图=主图)。

    主源 products.slow.images;缺席时回落最新含图快照(计数告警)。
    两处都没有 → 返回 []('无图'走 F1 路径:维持 pass + 告警 hit)。
    """
    with conn.cursor() as cur:
        cur.execute(_IMAGES_SQL, (marketplace, asin))
        row = cur.fetchone()
        if not row or not row[0]:
            cur.execute(_IMAGES_FALLBACK_SQL, (marketplace, asin))
            row = cur.fetchone()
            if row and row[0]:
                logger.warning("L4 取图走快照兜底(products.slow 无图):asin=%s",
                               asin)
    if not row or not row[0]:
        return []
    return [str(u) for u in row[0] if u]


# =============================================================
# 提示词(旧仓 l4_vision.py:87-169 / 229-251 / 172-200,逐字)
# =============================================================

L4_SYSTEM_PROMPT = """你是沃尔玛 Marketplace 视觉合规审核 AI, 站在沃尔玛官方立场审核中国搬运卖家产品图片.
只输出严格 JSON, 不要解释文字或 markdown 前后缀.

# 检测 7 类图像违规

## 1. 大牌 logo (trademark)
图片上明确显示知名品牌 logo 或完整字母 wordmark:
- 运动: Nike / Adidas / Jordan / Under Armour / Puma / Converse / Supreme
- 3C: Apple 苹果 logo / Samsung / Sony / Bose / Dyson / Beats / Rolex / Garmin
- 奢侈: LV / Gucci / Chanel / Hermès / Prada / Burberry
- 娱乐: Disney / Marvel / Star Wars / HBO
- 其他: LEGO / IKEA / Starbucks / McDonald's / Coca-Cola / Pepsi

## 2. ® 或 ™ 角标
图片右上角或文字后跟 ® / ™ / © → 说明商标声称, 若商家非注册方 = 侵权

## 3. 卡通 / 动漫 IP 形象
- 迪士尼角色 (Mickey Mouse / Donald Duck / 公主系列 / 冰雪奇缘 / Toy Story)
- 皮克斯 (超人总动员 / 玩具总动员 / Finding Nemo)
- 漫威/DC 英雄 (蜘蛛侠 / 钢铁侠 / 蝙蝠侠 / 超人)
- 日漫/宝可梦 (皮卡丘 / 哆啦A梦 / 火影忍者 / 海贼王)
- 吉卜力 (龙猫 / 千与千寻 / 魔女宅急便)
- 三丽鸥 (Hello Kitty / Kuromi / Cinnamoroll)
- SpongeBob / Peppa Pig / Paw Patrol / Sesame Street / Muppets

## 4. 运动队 / 学校 logo
- 美国四大职业体育: NBA / NFL / MLB / NHL / MLS 任一球队队徽
- NCAA Division I 大学 (Alabama Crimson Tide / Ohio State Buckeyes / Georgia Bulldogs / Duke Blue Devils 等)
- 世界杯 / 欧冠 / F1 车队 logo

## 5. 名人肖像 / 签名
可识别的公众人物面部/签名:
- 政治: Trump / Biden / Obama / Harris / 普京
- 明星: Taylor Swift / Beyoncé / BTS / Enhypen
- 运动员: LeBron / Kobe / Tom Brady / Messi / Ronaldo / Jordan
- 其他知名公众人物

## 6. 冒犯内容
- 仇恨符号 (卐字纳粹 / 南方邦联旗 / KKK)
- 血腥暴力 / 色情裸露 / 性暗示
- 宗教亵渎 / 种族歧视符号

## 7. 外观撞款 (Trade Dress / Design Patent)
产品形状/配色/包装明显仿知名产品:
- Stanley Cup tumbler (锥形大把手杯)
- YETI Rambler (双壁不锈钢保温杯典型形状)
- iPhone / AirPods / Apple Watch 外形
- Tiffany 蓝 + 白丝带礼盒
- 可口可乐经典瓶型 / 红白波浪字体
- 爱马仕橙色盒
- LV Monogram 格纹 / Gucci 红绿条纹

---

# 判定规则

- **default verdict = pass** — 证据不明确时放行 (避免过激误杀一般产品)
- 命中任一类别时必须**高置信度** (high) 才 reject; medium / low 只记录不 reject
- 必须指明是哪张图 (image_index, 从 0 开始)
- 说明 brand / ip_name 具体是什么 (不能只说"有 logo")

# 输出 JSON schema

{
  "verdict": "pass" | "reject",
  "image_issues": [
    {
      "image_index": 0,
      "issue": "big_brand_logo|registered_symbol|cartoon_ip|sports_team|celebrity|offensive|copycat_design",
      "subtype": "商标 | 版权 | 外观专利 | 商业包装 | 肖像权 | 冒犯 | 假冒",
      "brand_or_ip": "<具体品牌/IP 名, 如 Nike / Mickey Mouse / Stanley Cup>",
      "confidence": "high" | "medium" | "low",
      "detail": "<20 字内中文说明命中位置/形态>"
    }
  ],
  "reasoning": "<30 字内总体判断依据>"
}

约束:
- verdict=reject ⇔ image_issues 里至少一条 confidence=high
- image_issues 可为空数组 (pass)
- 未识别的图不要编造 issue
"""

# 拼接方式:system_prompt = L4_SYSTEM_PROMPT + L4_AGGRESSIVE_ADDON(仅 aggressive 时)。
# **本串以换行开头**(旧仓 l4_vision.py:229 的 `\"\"\"` 后直接换行),拼接处因此有
# 一个空行 + `---`,这是提示词字节契约的一部分,不得"顺手"去掉。
L4_AGGRESSIVE_ADDON = """

---

# AGGRESSIVE OFFENSIVE 模式 (此产品类目 offensive 风险升高)

对 "offensive" 类违规用**更严标准**:

1. 任何仇恨符号 (卐纳粹 / SS / 南方邦联旗 / KKK 头罩 / 白人至上主义 "OK" 手势或 "88") → confidence = "high"
2. 宗教嘲讽 / 亵渎 (耶稣 / 穆罕默德 / 佛像明显不敬, 宗教产品上的撒旦符号, 十字架上的五芒星等) → confidence = "high"
3. 种族歧视图像 (黑脸 blackface / "眯眯眼" 亚裔刻板 / 反犹钩鼻等) → confidence = "high"
4. 非成人类目产品上的色情 / 裸露 / 明显性暗示 → confidence = "high"
5. 血腥暴力 / 肢解 / 真实武器指人 → confidence = "high"
6. 政治攻击: 领导人被血 / 关笼 / 射杀 / 绞刑形象 (不分党派) → confidence = "high"
7. 非吸烟类产品上出现大麻叶 / 吸食工具图像 (如儿童产品带大麻叶) → confidence = "high"

重要:
- Cosplay/装扮 里的历史敏感人物 (纳粹军官 / KKK / 恐怖分子刻板 / 派对用 "印第安酋长头饰") = OFFENSIVE high.
- 万圣节通用骷髅 / 怪物 / 僵尸 / 南瓜 / 巫婆 / 吸血鬼 = **不** offensive, 不要标.
- 单一宗教装饰 (纯十字架 / Om / 大卫星) = **不** offensive, 不要标.

其他 issue 类型 (logos / cartoons / celebrities / copycat) 仍保持 "仅 high confidence" 的正常标准.
"""


def build_l4_user_prompt(product, l1, l3=None) -> str:
    """输入:产品 + L1Info + L3Result → 输出:视觉 user prompt(含 {img_count} 占位符)。

    逐字迁自旧仓 l4_vision.py:172-200:bullets 取**前 3 条**、每条前缀两空格 +
    "- ",空时整块 `  (无)`;标题截 200 字符;brand/类目空 → `(空)`,PT 空 →
    `(unknown)`;L3 提示块仅当 reason_text 非空时出现且截 150 字符。
    `{img_count}` 是字面占位符,由 judge_l4 用**下载成功数**替换(不是候选数)。
    """
    bullets_txt = "\n".join(f"  - {b}" for b in product.bullet_points[:3]) or "  (无)"
    l3_hint = ""
    if l3 and l3.reason_text:
        l3_hint = f"\n# L3 文本审已标注\nL3 reason: {l3.reason_text[:150]}"

    return f"""# 产品上下文 (辅助判图)

ASIN: {product.asin}
标题: {product.title[:200]}
品牌字段: {product.brand or '(空)'}
Walmart PT: {l1.walmart_product_type or '(unknown)'}
Walmart 类目: {l1.walmart_category or '(空)'}

五点 (前 3 条):
{bullets_txt}
{l3_hint}

# 任务

请逐张检查以下 {{img_count}} 张产品图, 按系统提示里的 7 类标准输出 JSON.
如果产品标题已经明确声称授权 (® / ™ / Official / Licensed / Authorized), 信任之, 不要 reject.
如果商家自填 brand 字段 = 图像上的 brand, 也信任 (是他自己的品牌, 不是冒用).
"""


# =============================================================
# 选图(旧仓 l4_vision.py:254-269、285-324)
# =============================================================


def _normalize_image_url(url: str) -> str:
    """输入:图片 URL → 输出:去 query、必要时补 .jpg 的 URL(逐字迁 l4_vision.py:256-269)。

    DMIT 采集里 Amazon 图 URL 常以 '.' 结尾缺扩展名(如 '..._AC_SL1500_.');
    归一化后的 URL 只用于本地下载(发给模型的是 base64 data URL)。
    """
    if not url:
        return url
    base = url.split("?", 1)[0]
    if base.endswith("."):
        return base + "jpg"
    lowered = base.lower()
    if not any(lowered.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")):
        return base + ".jpg"
    return base


def _explode(u: str) -> list[str]:
    """输入:可能塞了多条 URL 的字符串 → 输出:拆行 + 归一化后的 URL 列表。

    逐字迁 l4_vision.py:291-298:`\\r` 归一成 `\\n` 后按行拆,每行 strip,
    **只保留 startswith("http") 的行**。⚠ 旧仓 docstring 说"含 \\n / \\r / space
    分隔",但实现不按空格拆——已知缺陷照迁(spec §3.2-4),以实现为准。
    """
    parts: list[str] = []
    for piece in (u or "").replace("\r", "\n").split("\n"):
        piece = piece.strip()
        if piece.startswith("http"):
            parts.append(_normalize_image_url(piece))
    return parts


def select_l4_images(raw_images, limit: int = L4_MAX_IMAGES) -> list[str]:
    """输入:原始图片项列表(str 或 {url|src, is_main} dict)→ 输出:≤limit 张归一化 URL。

    逐字迁 l4_vision.py:285-324:dict 取 url 回落 src;裸 str 一律进 other 桶
    (即使是第一张也不当 main);其他类型跳过;main 桶全部排在 other 桶前;
    保序去重;截前 limit 张。
    新仓来源是 snapshots.slow.images(纯字符串数组,首图=主图),故全部落 other
    桶 → 退化为"保持原序",与 services/mp_mapper.sort_images 的顺序定义同源。
    """
    main_urls: list[str] = []
    other_urls: list[str] = []
    for item in list(raw_images or []):
        if not isinstance(item, dict):
            if isinstance(item, str):
                other_urls.extend(_explode(item))
            continue
        u_str = item.get("url") or item.get("src") or ""
        if not isinstance(u_str, str):
            continue
        urls_split = _explode(u_str)
        if not urls_split:
            continue
        if item.get("is_main"):
            main_urls.extend(urls_split)
        else:
            other_urls.extend(urls_split)

    seen: set[str] = set()
    ordered: list[str] = []
    for u in (main_urls + other_urls):
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered[:limit]


# =============================================================
# aggressive offensive 判定(旧仓 aggressive_offensive.py:27-132,逐字)
#
# 只随 L4 迁(合同 客户端-7/8):同文件的 should_use_strong_l3_model(L3 模型
# 分流用)不迁 —— 新仓 L3 单链无模型分流。
# =============================================================

_PT_KEYWORDS = {
    # 节日 / 装扮
    "costume", "costumes",
    "halloween",
    "party decoration", "party decorations", "party supplies",
    # 宗教 / 政治信仰类 (高概率踩宗教嘲讽 / 政治 figure)
    "religious", "religion",
    "flag", "flags", "banner", "banners",
    "tapestry", "tapestries",
    # 可任意印图 / 贴纸 / 墙贴 (offensive 主战场)
    "sticker", "stickers",
    "decal", "decals",
    "wall art", "wall decor",
    "poster", "posters",
    "t-shirt", "t shirts", "tee",
    "coffee mug", "mug",
    "pin", "pins",          # enamel pin / lapel pin
    "patch", "patches",
}

_CAT_KEYWORDS = {
    "seasonal", "halloween", "party",
    "religious", "religion",
    "home decor",
    "apparel accessories",
    "novelty",
}

_TITLE_KEYWORDS = (
    # 装扮 / cosplay 类 (图像高风险)
    "costume", "cosplay", "fancy dress", "fancy-dress",
    "halloween", "haunted",
    "masquerade",
    # 身体挂件 / 妆扮
    " wig", "wigs",
    " mask ", "masks", "face mask",    # "mask" 加空格避免匹配 "mascara"
    # 派对 photo booth (常含 racial/political 道具)
    "photo booth", "photo prop", "party prop",
)

_BULK_STICKER_RE = re.compile(
    r"\b(\d{2,5})\s*(?:pcs|pc|pieces?|ct|count|stickers?|decals?|pack)\b"
    r"[\s\S]{0,80}\b(?:stickers?|decals?)\b",
    re.IGNORECASE,
)
_BULK_STICKER_RE_REVERSE = re.compile(
    r"\b(?:stickers?|decals?)\b[\s\S]{0,80}\b(\d{2,5})\s*(?:pcs|pc|pieces?|ct|count|pack)\b",
    re.IGNORECASE,
)

_BULK_STICKER_MIN_COUNT = 20


def _title_has_bulk_stickers(title: str) -> tuple[bool, int]:
    """输入:小写标题 → 输出:(是否大杂烩贴纸包, 数量);逐字迁 aggressive_offensive.py:84-97。"""
    if not title:
        return False, 0
    for pat in (_BULK_STICKER_RE, _BULK_STICKER_RE_REVERSE):
        m = pat.search(title)
        if m:
            try:
                n = int(m.group(1))
                if n >= _BULK_STICKER_MIN_COUNT:
                    return True, n
            except (ValueError, IndexError):
                pass
    return False, 0


def should_aggressive_offensive(l1, product=None) -> bool:
    """输入:L1Info(+产品)→ 输出:是否开 L4 aggressive offensive 模式。

    任一命中即 True:PT 关键词子串 → 类目关键词子串 → title 关键词子串 →
    title 大杂烩贴纸(≥20 张)。逐字迁 aggressive_offensive.py:113-132。
    两个作用缺一不可:①system prompt 追加 addon;②offensive 类 medium 也算命中。
    """
    pt = (l1.walmart_product_type or "").lower()
    cat = (l1.walmart_category or "").lower()
    for kw in _PT_KEYWORDS:
        if kw in pt:
            return True
    for kw in _CAT_KEYWORDS:
        if kw in cat:
            return True

    if product is not None:
        title_lc = (product.title or "").lower()
        if title_lc:
            for kw in _TITLE_KEYWORDS:
                if kw in title_lc:
                    return True
            bulk, _ = _title_has_bulk_stickers(title_lc)
            if bulk:
                return True
    return False


# =============================================================
# 判定与结果
# =============================================================


def _vision_hit(issue: dict[str, Any], aggressive_offensive: bool = False) -> bool:
    """输入:一条 image_issue(+aggressive 开关)→ 输出:是否算命中(可触发 reject)。

    high 才算;aggressive 时 offensive 类的 medium 也算(VLM 对纳粹符号/宗教
    嘲讽/血腥常给 medium,default pass 会漏掉大量真违规)。
    ⚠ **已知缺陷照迁**(spec §6.1、合同 L4-7):confidence 是**严格大小写等值**
    比较,模型回 "High" 不算命中(静默漏拦)。保留是为双跑口径一致,已钉住测试。
    """
    if not isinstance(issue, dict):
        return False
    conf = issue.get("confidence")
    if conf == "high":
        return True
    if aggressive_offensive and conf == "medium":
        issue_type = (issue.get("issue") or "").lower()
        if "offensive" in issue_type:
            return True
    return False


@dataclass
class L4Result:
    """L4 视觉审核结果(对应 audit_runs 的 l4_verdict / l4_issues 两列)。

    verdict 只有 'pass' / 'reject'(合同 L4-8:L4 绝不产 pending)。
    image_issues 是**全量 issues**(含 low/medium),不是命中子集 —— pass 的产品
    也可能非空,上架选图侧按"有 image_index 即问题图"消费(不看 confidence)。
    """

    verdict: str
    image_issues: list[dict[str, Any]] = field(default_factory=list)
    hits: list[RuleHit] = field(default_factory=list)


def _alert(rule_code: str, detail: dict, extra_hits=()) -> L4Result:
    """输入:失败要素 → 输出:pass 形态 L4Result(penalty=0 的一条告警 hit)。

    合同 L4-8:五条失败路径全部维持原结论 pass,靠 hit + 日志计数暴露故障,
    绝不产 pending(L4 是默认关的增量拦截层,故障不该制造假阳)。
    """
    return L4Result(
        verdict="pass",
        image_issues=[],
        hits=[*extra_hits,
              RuleHit(stage="L4", rule_code=rule_code, penalty=0, detail=detail)],
    )


def _attach_image_urls(issues: list, urls: list[str]) -> list:
    """输入:模型 issues + 本次实际发送的原始 URL 列表 → 输出:每条补 image_url 的 issues。

    合同 L4-3:**新增键不删旧键** —— image_index(模型对发送顺序的编号)原样保留,
    另补 image_url 指向该下标对应的原始图片 URL。这修掉旧仓"下载失败静默丢弃 →
    索引整体前移 → 上架选图避开错图"的错位(spec §7.2-A)。
    下标越界 / 非整数 / 非 dict 的条目原样透传,不编造 image_url。
    """
    out: list = []
    for issue in issues:
        if not isinstance(issue, dict):
            out.append(issue)
            continue
        item = dict(issue)
        idx = item.get("image_index")
        try:
            i = int(idx)
        except (TypeError, ValueError):
            out.append(item)
            continue
        if 0 <= i < len(urls):
            item["image_url"] = urls[i]
        out.append(item)
    return out


def judge_l4(product, l1, l3=None, *, images=None, conn=None) -> L4Result:
    """输入:产品 + L1Info + L3Result + 图片列表(或连接自取)→ 输出:L4Result。

    调用方(audit_rules.audit_one)保证只对 verdict 仍为 pass 且开关打开的产品
    调用;本函数不查库判状态。images 省略时用 conn 从最新含图快照取。
    流程:选图(≤5)→ 并发下载转 base64 → aggressive 判定 → 拼提示词 →
    调 ARK → 本地重算 verdict(忽略模型自报 verdict)→ 组 RuleHit。
    任何失败都返回 pass + 告警 hit(合同 L4-8),**绝不返回 pending**。
    """
    if images is None:
        if conn is None:
            raise ValueError("judge_l4 需要 images 或 conn 之一(调用方决定取数来源)")
        images = load_product_images(conn, product.asin)

    urls = select_l4_images(images)
    if not urls:            # F1:无图(旧仓静默 pass,新仓补告警 hit)
        logger.warning("L4 %s 无可用图片,维持 pass", product.asin)
        return _alert("l4_no_images", {"asin": product.asin})

    fetched = llm_vision.fetch_images(urls)
    if not fetched:         # F2:候选图全部下载失败
        logger.warning("L4 %s 所有 %d 张图本地下载失败,维持 pass",
                       product.asin, len(urls))
        return _alert("l4_images_unavailable",
                      {"asin": product.asin, "images_total": len(urls)})

    orig_urls = [u for u, _ in fetched]
    data_urls = [d for _, d in fetched]
    # 合同 L4-9:部分图失败用成功子集继续判,但必须留痕(fetched/total)
    extra: list[RuleHit] = []
    if len(fetched) < len(urls):
        logger.warning("L4 %s 部分图下载失败(%d/%d),用成功子集继续判",
                       product.asin, len(fetched), len(urls))
        extra.append(RuleHit(stage="L4", rule_code="l4_images_partial", penalty=0,
                             detail={"images_fetched": len(fetched),
                                     "images_total": len(urls)}))

    aggressive_offensive = should_aggressive_offensive(l1, product)
    system_prompt = L4_SYSTEM_PROMPT
    if aggressive_offensive:
        system_prompt = system_prompt + L4_AGGRESSIVE_ADDON
    user_prompt = build_l4_user_prompt(product, l1, l3).replace(
        "{img_count}", str(len(data_urls)))

    if aggressive_offensive:
        logger.info("L4 aggressive offensive mode ON for %s (pt=%r, cat=%r)",
                    product.asin, l1.walmart_product_type, l1.walmart_category)

    try:
        raw = llm_vision.vision_json(
            data_urls, system_prompt=system_prompt, user_prompt=user_prompt,
            temperature=L4_TEMPERATURE, max_tokens=L4_MAX_TOKENS)
    except ValueError as e:     # F4:4xx / 坏 JSON(_extract_json 抛)
        logger.warning("L4 %s 视觉回复不可用(%s),维持 pass", product.asin, e)
        return _alert("l4_bad_json", {"error": str(e)[:MAX_ERROR_SNIPPET]}, extra)
    except Exception as e:      # noqa: BLE001 —— F3:重试尽 / 端点故障 / 其他
        logger.warning("L4 %s 视觉调用失败(%s),维持 pass", product.asin, e)
        return _alert("l4_llm_error", {"error": str(e)[:MAX_ERROR_SNIPPET]}, extra)

    if not raw:                 # F4:空 dict
        logger.warning("L4 %s 视觉回复为空,维持 pass", product.asin)
        return _alert("l4_bad_json", {"raw": str(raw)[:MAX_RAW_SNIPPET]}, extra)

    issues = raw.get("image_issues") or []
    if not isinstance(issues, list):    # F5:schema 违约(旧仓静默当空数组)
        logger.warning("L4 %s image_issues 不是数组(%r),按空处理,维持 pass",
                       product.asin, str(issues)[:MAX_RAW_SNIPPET])
        extra.append(RuleHit(stage="L4", rule_code="l4_bad_schema", penalty=0,
                             detail={"raw": str(issues)[:MAX_RAW_SNIPPET]}))
        issues = []

    issues = _attach_image_urls(issues, orig_urls)

    # 判定:正常 high confidence 才算;aggressive offensive 模式下 offensive medium 也算。
    # **模型自报的 raw["verdict"] 完全忽略** —— verdict 只由 image_issues 本地重算。
    high_hits = [i for i in issues if isinstance(i, dict) and _vision_hit(i, aggressive_offensive)]
    verdict = "reject" if high_hits else "pass"

    hits: list[RuleHit] = list(extra)
    if verdict == "reject":
        # reject 原因如果是 aggressive-escalated offensive, 在 detail 里标出
        aggressive_escalated = aggressive_offensive and any(
            (i.get("confidence") == "medium" and "offensive" in (i.get("issue") or "").lower())
            for i in high_hits
        )
        hits.append(RuleHit(
            stage="L4",
            rule_code="l4_vision_violation",
            penalty=-100,
            detail={
                "issues": high_hits,
                "all_issues": issues,
                "reasoning": raw.get("reasoning"),
                "images_scanned": len(data_urls),
                "aggressive_offensive": aggressive_offensive,
                "aggressive_escalated": aggressive_escalated,
                # 新增两键(合同 L4-9):候选图有几张、真发出去几张
                "images_fetched": len(data_urls),
                "images_total": len(urls),
            },
        ))

    return L4Result(verdict=verdict, image_issues=issues, hits=hits)


__all__ = [
    "L4Result",
    "L4_TEMPERATURE",
    "L4_MAX_TOKENS",
    "L4_MAX_IMAGES",
    "L4_SYSTEM_PROMPT",
    "L4_AGGRESSIVE_ADDON",
    "judge_l4",
    "load_product_images",
    "select_l4_images",
    "build_l4_user_prompt",
    "should_aggressive_offensive",
]
