"""L3 语义审核(LLM 文本判定;读官方英文政策全文 + 上游确定性证据)。

L3 只对 L2 pass 的产品跑,判 L0/L2 的确定性规则抓不到的那一类问题:
产品本身/文案是不是踩了某一条**沃尔玛官方政策**,以及上游扫出来的品牌词
是真品牌还是通用英文词。

判定不动分数(RuleHit penalty 恒 0),只决定 verdict:
`reject` → 整品拒;`pending` → 待人工复核;`pass` → 交 L4(若开)。

**输出三段**(2026-09-02 §十.7 / `docs/audit_step3_spec.md` §3.3 定稿):
判定结果 `verdict` / 类别 `policy`(官方类别名枚举,逐字抄)/ 具体内容
`detail`(中文 ≤120 字,引触发的原文片段)。**零模糊归一化**:policy 在解析层
就对表(`policy_names.resolve`),对不上 = pending,不猜、不降级。

**prompt 前缀缓存契约**:messages 恒为 `[system, user]`,system prompt 对**同一轮
的所有产品**逐字节相同(S1 指令 + S2 类别枚举 + S3 分隔段 + S4 政策全文块 ——
后三段全部由政策表实时渲染,**全部**行,条数不写死),进程内只构造一次。
任何把产品信息拼进 system 的写法都会打散前缀缓存,命中率从 ~95% 掉回 ~63%
(旧仓 l3_llm.py:193-198 / :285-289 实测)。政策表一改(新增行 / 改名 /
刷新正文),S2/S4 就跟着变,这是**设计如此**(政策表就是 L3 的判定输入):
缓存的前提是"每个产品都一样",不是"永远一样";变更后前缀缓存一次性重建
属预期成本,同批递增 `AUDIT_RULES_VERSION`。

失败语义(计划 10.2,与旧仓相反):LLM 重试尽 / 坏 JSON / verdict 取值非法 /
政策名对不上表,**一律 verdict='pending'**(旧仓是 pass —— 故障窗口漏审违规
商品的代价远大于人工复核);pending **不写 llm_cache**。

2026-09-02 B1 批删掉的三样(`docs/audit_step3_spec.md` §3.1/§3.3/§3.7):
  · **中文人工列压缩块**(`format_full_policy_block` 与 50/30/240/80 截断)——
    S4 改喂官方英文全文,人工列只是二手转述;
  · **政策路由提示**(`route_policy_hints` + 两张手工映射表)—— 它是第二张
    「类目 → 政策」映射,而 §十.7 已定「政策类别 ≠ 类目」;换全文后 LLM 面前
    有全部政策,提示只会把注意力锁在 ≤5 篇上;
  · **输出降级猜测**(政策名不在白名单就猜 `intellectual property`)——
    猜出来的类别会一路落库、进飞书、进申诉口径,而没有任何东西会红。

public:
  L3Result, judge_l3, load_policy_rows, load_reason_categories,
  system_prompt, build_system_prompt, build_user_prompt,
  summarize_evidence, parse_l3_reply, policy_enum
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from dataclasses import dataclass, field

from api import llm as _llm_api
from registry import resources
from services import llm_cache, policy_feed, policy_names
from services.audit_models import RuleHit

logger = logging.getLogger("services.audit_l3")

# 调用参数(旧仓 l3_llm.py:743-756):常规链 max_tokens=1500;温度必须显式传,
# 新仓 api/llm.py 默认 0.2 ≠ 旧仓 0.1,吃默认即改判定口径。
L3_TEMPERATURE = 0.1
L3_MAX_TOKENS = 1500
L3_PURPOSE = "audit_l3"          # registry.LLM_PURPOSE_ENV 登记项


# ── 本轮计数(惯例照 `services/audit_l1_llm.STATS`:单进程累加,run 摘要读它)──
#
# 两件事都属于"判据悄悄变窄/变形"那一类:不报错、不红,只有计数能让人看见。
#   · policy_no_full_text —— 政策表里有这一行(进了 S2 枚举),但 `full_policy`
#     是空的 ⇒ S4 里**没有它的原文**。LLM 会看到一个能选的类别名,却找不到
#     任何可引用的条款(空壳标题等于没给,故不渲染)。
#   · llm_bad_policy —— LLM 答的政策名在枚举里对不上 ⇒ 整条转 pending。
# `reset_stats()` 每轮开始由 `workflows/product_audit` 调;`workers>1` 时裸
# `+=` 会丢计数,故上锁。
STATS: Counter = Counter()
_STATS_KEYS = ("policy_no_full_text", "llm_bad_policy")   # 另有动态键 `policy_no_full_text:<名字>`
_STATS_LOCK = threading.Lock()


def bump(key: str) -> int:
    """输入:计数键 → 输出:累加后的值(线程安全;调用方拿它决定要不要打日志)。"""
    with _STATS_LOCK:
        STATS[key] += 1
        return STATS[key]


def reset_stats() -> None:
    """输入:无 → 输出:无(清零并补齐固定键,便于摘要直接取)。"""
    with _STATS_LOCK:
        STATS.clear()
        STATS.update({k: 0 for k in _STATS_KEYS})


reset_stats()


def policies_without_full_text() -> list[str]:
    """输入:无 → 输出:本进程渲染时缺全文的政策名(名字序;摘要点名用)。"""
    pre = "policy_no_full_text:"
    return sorted(k[len(pre):] for k, v in STATS.items()
                  if k.startswith(pre) and v)


# =============================================================
# system prompt(S1 指令 + S2 类别枚举 + S3 分隔段 + S4 政策全文块)
# =============================================================

# 计数占位符:S1/S3 各有一处「{N} 篇沃尔玛政策全文…」,N = **实时**篇数
# (= 下方 S4 真正渲染出来的篇数,不是政策表行数 —— 没有全文的行不渲染,
# 说 44 篇却只给 42 篇,LLM 会拿那个数当"我应该看到多少篇"的锚)。
# ⚠ 用 str.replace 而不是 str.format:S1 正文里有 JSON 示例的 `{}`。
_COUNT_SLOT = "{N}"


def _fill_count(text: str, n: int) -> str:
    """输入:带占位符的提示词段 + 实时政策篇数 → 输出:填好数的那一段。"""
    return text.replace(_COUNT_SLOT, str(n))


# S1:指令段(2026-09-02 B1 批重写,规格 §3.1)。
# ⚠ 提示词是**判定面**:改这里等于改判定,顺手改一句措辞不会报错,只会让
#   判定悄悄漂。改动请同批递增 `AUDIT_RULES_VERSION` 并写清改了哪一句。
_S1 = """你是沃尔玛 Marketplace 合规审核 AI(站在沃尔玛官方立场)。
卖家是中国搬运模式、无任何证书/认证、每日数万产品。
你只输出严格 JSON,不要任何解释文字或 markdown 前后缀。

# 判据只有一个:本提示词末尾的官方原文

- 判定**只许**依据末尾那
  {N} 篇沃尔玛政策全文(Prohibited Products Policy 各类别 + 内容标准两页)。
  你训练记忆里的沃尔玛政策**一律作废** —— 版本不同、条款不同,凭记忆判
  等于按另一套规则判。
- 原文没写的事**不判违规**:引不出条款就是没有依据。
- 默认 pass。只有「原文条款」与「产品原文证据」两样都拿得出来时才 reject。

# 判定的两类命中(别把"要证据"误读成"标题必须明示违规用途")

## A. 品类/设备/物料整体禁售(政策直接禁这个东西本身,不论用途)
政策条款列出的是**具体产品品类/设备名/物料名** → 产品本质就是这类东西即 reject。
标题写成别的用途不改变物本身(如蒸馏设备写 "for essential oil",仍是蒸馏设备)。

## B. 用途/特征/年龄段敏感(政策禁的是子类型或特征)
必须在产品原文里找到那个特征的证据(如有绳窗帘要 "corded";儿童用品要有
≤12 岁使用的证据)。找不到证据 → pass。

# 输出的三段:判定结果 / 类别 / 具体内容

- `verdict`:`pass` 或 `reject`,只有这两个值。
- `policy`:**逐字抄**下面「候选类别」清单里的一项 —— 一个字母、一个空格、
  一个逗号都不许改(它是我们跟沃尔玛对话的口径,拼错了申诉时对方查无此类)。
  verdict=pass 时填 `none`;清单里找不到能覆盖它的一项 = 这不是违规 → pass。
- `detail`:中文,≤120 字,两样缺一不可 ——
  ① **引用触发判定的原文片段**(标题/五点/描述里的那一句,**保留原语言**,
     不要翻译、不要转述);② 触犯的条款要点(哪条政策、禁什么)。
  例:标题写 "Distillation Apparatus for essential oil",Alcohol 政策禁蒸馏设备。

# 品牌证据怎么判(brand_verdicts)

「上游证据」里列出的品牌/商标词,**逐个**给判定:

- **提到 ≠ 卖的就是它**:`compatible with X` / `fits X` / `replacement for X` /
  `for X` 这类兼容、适配、对比提及,是在说配件适用范围,**不是**品牌误用,
  也不是冒充 X —— 除非文案同时自称是 X 的正品 / 授权 / OEM。
- 通用英文词(top / floor / summer / modern / classic 之类)被黑名单收进来是
  常事,按上下文判 `is_real_brand=false`。
- 真品牌的信号:品牌名后紧跟型号(如 "Dyson V6")、"100% Authentic <大牌>"、
  自称 Official / Licensed / Authorized / OEM —— 搬运卖家客观上拿不到大牌授权,
  任何"授权声明"都按虚假宣称处理。
- `evidence` 一句话说清依据的是哪段原文。

# 本 PT 的沃尔玛准入要求怎么判

user 段若出现「本 PT 的沃尔玛准入要求」一行,那是沃尔玛对这个**类目**登记的
要求(常含证书 / 检测报告 / 注册号)。顺序只有一条:

1. 先判**这个具体产品**要不要这张证 —— 要求是按整类写的,同一类目里既有要证的
   也有不要的(儿童玩具要 CPC,同类目下的成人收藏摆件不要;带电成品要 NRTL
   挂牌 UL/ETL/CSA,同类目下的纯木桌、灯泡线材这类配件不要);
2. 要、而 listing 里没有任何证据 → reject,`policy` 填**覆盖这件事的那条政策**
   (如儿童产品填 Children's Products);
3. 末尾原文里没有任何一篇覆盖它 → `policy` 填 `类目准入`;
4. 判不出"要不要" → pass(拿不准不拒 —— 这一维默认放行,宁可漏一个,
   也不要按类目名连坐整类)。

# 输出规范(严格 JSON,只输出这一个对象)

{
  "verdict": "pass" | "reject",
  "policy": "<候选类别之一,逐字;verdict=pass 时填 'none'>",
  "detail": "<中文 ≤120 字:原文片段 + 条款要点;pass 时可留空>",
  "brand_verdicts": [
    {"brand": "<上游证据里的词>", "is_real_brand": true|false, "evidence": "<依据>"}
  ],
  "confidence": "high" | "medium" | "low"
}

# 约束

- 默认 verdict=pass,只有清晰证据才 reject;
- `policy` 必须逐字取自下面的候选类别清单(pass 时 `none`),不自造类别名;
- brand_verdicts 只判「上游证据」里列出的词(最多 10 个),不凭空添加品牌;
- 不输出 JSON 之外的任何文字。

# 候选类别(verdict=reject 时必选其一)
"""

# S3:S2 候选块与 S4 全文块之间的分隔段(同样带 `{N}` 占位符)。
_S3 = """

# {N} 篇沃尔玛政策全文(Prohibited Products Policy 各类别 + 内容标准两页)

每篇以 `## 类别名` 开头(篇内还有官方自己的小标题,别把小标题当类别名);
与下面的原文逐条核对:命中哪一篇 → `policy` 填**上面候选类别清单**里对应的
那一项(逐字);没有一篇能引出条款 → verdict=pass。

"""

# S4 政策全文:**只取有全文的行**(空壳标题给 LLM 等于没给),ORDER BY id
# 固定顺序 —— 顺序即前缀缓存命中率。人工中文列一列都不读(2026-09-02 B1:
# 它们是二手转述,判据以官方英文原文为准)。
POLICY_ROWS_SQL = (
    "SELECT id, category_en, full_policy "
    "FROM audit.walmart_prohibited_policy "
    "WHERE full_policy IS NOT NULL ORDER BY id"
)
# ORDER BY id / ORDER BY category_en 都不可省:顺序即前缀缓存命中率(§11.2)
REASON_CATEGORIES_SQL = (
    "SELECT category_en FROM audit.walmart_prohibited_policy "
    "WHERE category_en IS NOT NULL ORDER BY category_en"
)


def load_policy_rows(conn) -> list[dict]:
    """输入:中心库连接 → 输出:**有全文的**政策行 dict 列表(ORDER BY id,给 S4)。"""
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
    """输入:全部 category_en(库序)→ 输出:S2 候选块(每行 `  - {c}`)。

    末尾固定追加两条**非政策类别**(`registry.resources.
    AUDIT_NONPOLICY_CATEGORIES`)与 `none`。2026-09-02 B1 删掉 `brand_misuse`:
    品牌误用归 `Intellectual Property`(由解析层的品牌翻拒规则落地),
    多一个只在提示词里存在、政策表里没有的伪类别,就多一处对不上的口径。
    """
    cats = (list(categories) + list(resources.AUDIT_NONPOLICY_CATEGORIES)
            + ["none"])
    return "\n".join(f"  - {c}" for c in cats)


def policy_parts(rows: list[dict]) -> list[str]:
    """输入:政策行(ORDER BY id,含 full_policy)→ 输出:每篇一段的 S4 文本。

    每篇渲染成 `## {category_en}` + 空行 + 喂入版全文。喂入版由
    `services/policy_feed.render_feed_text` 从 `full_policy` **渲染时派生**
    (不落库、不留第二份,`docs/policy_sync.md` §十.3):剥链接/导览/免责声明/
    页面 chrome,单行数据表转成清单。

    全文为空(或渲染后为空)的行**整条跳过并计数** —— 只剩一个 `## 标题` 的
    空壳等于没给判据,却会让 LLM 以为"这一类我已经看过了"。

    ⚠ 返回**列表**不是拼好的字符串:篇数只能这么数。官方正文自己带
      `## Overview` / `## Prohibited Products Policy: X` 这类小标题,数
      `"\\n## "` 出来的是 251 而不是 44(2026-09-02 实测),提示词自称的篇数
      会瞬间变成一个假数。
    """
    parts: list[str] = []
    for r in rows:
        cat_en = (r.get("category_en") or "").strip()
        body = policy_feed.render_feed_text(r.get("full_policy") or "").strip()
        if not cat_en or not body:
            name = cat_en or f"id={r.get('id')}"
            bump("policy_no_full_text")
            if bump(f"policy_no_full_text:{name}") == 1:
                logger.warning("政策表 %r 没有可喂的全文,S4 跳过这一篇 —— "
                               "它仍在 S2 候选里,LLM 选得到却引不出条款"
                               "(补 full_policy:policy_sync)", name)
            continue
        parts.append(f"## {cat_en}\n\n{body}")
    return parts


def format_policy_block(rows: list[dict]) -> str:
    """输入:政策行 → 输出:S4 官方全文块(各篇之间空行分隔)。"""
    return "\n\n".join(policy_parts(rows))


def build_system_prompt(categories: list[str], policy_rows: list[dict]) -> str:
    """输入:候选类别(库序)+ 政策行 → 输出:完整 system prompt(S1+S2+S3+S4)。

    零产品入参 —— 旧仓 `get_system_prompt(cat, pt, hint)` 的三个参数全被
    `_blocks_for` 吞掉(恒 (True, True)),新仓不保留死签名。

    S1/S3 里的篇数按 **S4 真正渲染出来的篇数**填(不是政策表行数):没有全文的
    行不进 S4,自称 44 篇却只给 42 篇,那个数就成了假的。
    """
    parts = policy_parts(policy_rows)          # 只渲染一次(计数也只加一轮)
    return (_fill_count(_S1, len(parts))
            + format_reason_categories(categories)
            + _fill_count(_S3, len(parts))
            + "\n\n".join(parts))


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
# 上游证据 → user prompt(2026-09-02 B1:通道从"只读 L2"泛化到三层)
# =============================================================

MAX_BRANDS = 10          # 品牌词前 10 个(system prompt 也这么要求 LLM)
MAX_BULLETS = 10         # 亚马逊五点本就 ≤5,有多的照给(B1 从 5 放宽)
MAX_DESC_CHARS = 3000    # B1 从 600 放宽:600 字砍掉的正是宣称最密的那一段
MAX_NOTES_CHARS = 200
MAX_REQ_CHARS = 500      # 本 PT 的沃尔玛准入要求(walmart_pt_meta.requirements)
MAX_EVIDENCE_CHARS = 300  # 未登记 rule_code 的 detail 摘要上限


def _line_title_desc_blacklist(h) -> tuple[str, list[str]]:
    """输入:R4 命中 → 输出:(一行文本, 品牌词)。"""
    matches = (h.detail or {}).get("matches", [])[:MAX_BRANDS]
    names = [m.get("brand", "") for m in matches if m.get("brand")]
    pairs = [f"{m.get('brand')}(原文:{m.get('matched_phrase')})" for m in matches]
    n = (h.detail or {}).get("count", len(matches))
    return (f"* 标题/描述命中黑名单(R4, 共{n}个, 前10): {', '.join(pairs)}", names)


def _line_cert(h) -> tuple[str, list[str]]:
    """输入:R3 命中 → 输出:(一行文本, 无品牌词)。

    ⚠ 键名照**规则真实写进 detail 的那些**取(meta_requirements /
    hard_cert_fields / soft_cert_fields);首版取的 `requirements` 这个键
    三种 cert hit 里一个都没有,于是前两档永远退化成一句固定套话。
    """
    d = h.detail or {}
    what = (d.get("meta_requirements") or d.get("hard_cert_fields")
            or d.get("soft_cert_fields") or d.get("note"))
    return (f"* 类目需证书({h.rule_code}): {what}", [])


def _line_trademark_live(h) -> tuple[str, list[str]]:
    """输入:R5 命中 → 输出:(一行文本, 品牌词小写)。"""
    marks = (h.detail or {}).get("matched_marks", [])[:MAX_BRANDS]
    return (f"* USPTO LIVE 商标(R5, 前10): {', '.join(marks)}",
            [m.lower() for m in marks if m])


def _line_content_promotional(h) -> tuple[str, list[str]]:
    """输入:R7 命中 → 输出:(一行文本, 无品牌词)。"""
    d = h.detail or {}
    phrases = (d.get("strong_phrases") or []) + (d.get("allcaps_runs") or [])
    tag = "仅空洞形容词" if d.get("soft_only") else "含无据宣称/全大写滥用"
    return (f"* 促销宣称(R7, {tag}): "
            f"{', '.join((phrases or d.get('soft_phrases') or [])[:MAX_BRANDS])}",
            [])


def _line_strict_sensitive(h) -> tuple[str, list[str]]:
    """输入:R8 命中 → 输出:(一行文本, 无品牌词)。"""
    d = h.detail or {}
    subtypes = ", ".join(d.get("subtypes") or [])
    phrases = (d.get("matched_phrases") or [])[:MAX_BRANDS]
    return (f"* 敏感/严格合规(R8, {subtypes}): "
            f"{', '.join(str(t) for t in phrases)}", [])


def _line_brand_mention(h) -> tuple[str, list[str]]:
    """输入:L0 品牌文案扫描命中 → 输出:(一行文本, 品牌词)。

    C 批把 R4 迁进 L0 后走这一条(rule_code `phase0_brand_mention`,detail
    形状与 R4 同款);B 批库里还没有这个码,登记在这里是为了 C 批零改动接上。
    """
    matches = (h.detail or {}).get("matches", [])[:MAX_BRANDS]
    names = [m.get("brand", "") for m in matches if m.get("brand")]
    pairs = [f"{m.get('brand')}(原文:{m.get('matched_phrase')})" for m in matches]
    n = (h.detail or {}).get("count", len(matches))
    return (f"* 文案提到黑名单品牌(共{n}个, 前10): {', '.join(pairs)}", names)


# rule_code → 渲染函数。**未登记的照样出行**(见 `_line_generic`):证据通道
# 的合同是"上游软 hit 一条都不丢",登记表只决定长得好不好看。
_EVIDENCE_LINES = {
    "title_desc_blacklist": _line_title_desc_blacklist,
    "trademark_live": _line_trademark_live,
    "content_promotional": _line_content_promotional,
    "walmart_strict_sensitive": _line_strict_sensitive,
    "phase0_brand_mention": _line_brand_mention,
}
_CERT_PREFIX = "cat_requires_cert"      # 三种 cert hit 共用一个渲染


def _line_generic(h) -> tuple[str, list[str]]:
    """输入:未登记 rule_code 的软 hit → 输出:(通用一行, 无品牌词)。"""
    bits = [f"{k}={v}" for k, v in (h.detail or {}).items()
            if v not in (None, "", [], {})]
    txt = ", ".join(bits)[:MAX_EVIDENCE_CHARS]
    return (f"* {h.rule_code}: {txt}" if txt else f"* {h.rule_code}", [])


def summarize_evidence(phase0=None, l1=None, l2=None) -> tuple[str, list[str]]:
    """输入:L0/L1/L2 三层结果 → 输出:(上游证据文本, 品牌词前 10 个)。

    只收 **penalty == 0 的软 hit**:扣了分的是硬拒,那种产品根本进不了 L3。
    顺序 = phase0 → l1 → l2(与 `AuditOutcome.all_hits` 同序,落库与提示词
    看到的是同一个顺序)。

    ⚠ 2026-09-02 B1 起通道**跨三层**(原 `summarize_l2_for_l3` 只读 L2):
    C 批把品牌文案扫描迁进 L0 之后,证据源就在 L0;通道现在按 rule_code 查
    渲染表,与它出自哪一层无关,迁层不用再改这里。
    ⚠ **未登记的 rule_code 不丢**,按通用形态打一行:漏掉一条软证据不会报错,
    只会让 L3 少看一样东西 —— 那正是"承诺了没送到"的老毛病(R7/R8 曾经
    整整两个月一个字都没进提示词)。
    """
    lines: list[str] = []
    brands: list[str] = []
    for res in (phase0, l1, l2):
        for h in (getattr(res, "hits", None) or ()):
            if h.penalty != 0:
                continue
            render = _EVIDENCE_LINES.get(h.rule_code)
            if render is None and h.rule_code.startswith(_CERT_PREFIX):
                render = _line_cert
            line, got = (render or _line_generic)(h)
            lines.append(line)
            brands.extend(got)
    return (
        ("\n".join(lines) if lines else "(上游无证据)"),
        list(dict.fromkeys(b for b in brands if b))[:MAX_BRANDS],
    )


def build_user_prompt(product, l1, l2, *, phase0=None,
                      pt_notes: str | None = None,
                      pt_requirements: str | None = None) -> str:
    """输入:产品 + L1 + L2(+ L0 / PT 备注 / 本 PT 准入要求)→ 输出:user prompt。

    2026-09-02 B1 相对旧模板的四处(规格 §3.2):长描述 600→3000 字符、
    五点全给、删「原产国」恒空行与「政策路由提示」行、「L2 规则引擎命中」段
    改为「上游证据」段并新增「本 PT 的沃尔玛准入要求」段。

    ⚠ 删「原产国」不是省字:采集契约里根本没有这个值,给 LLM 一个恒空字段
      只会诱导它把"原产国未知"当证据。
    """
    bullets_txt = "\n".join(f"  - {b}" for b in product.bullet_points[:MAX_BULLETS]) or "  (无)"
    desc = (product.long_description or "").strip()
    if len(desc) > MAX_DESC_CHARS:
        desc = desc[:MAX_DESC_CHARS] + "...(已截断)"

    ev_txt, brands = summarize_evidence(phase0, l1, l2)

    brand_list = (
        "\n".join(f"  - {b}" for b in brands)
        if brands
        else "  (上游无品牌命中, 跳过品牌真伪判定)"
    )

    # walmart_pt_meta.notes —— 飞书人工标注(如 "⚠️T&S抽查48h内");
    # 新仓零 DB 访问,由 ctx.pt_meta 带入(旧仓 _get_pt_notes 独立查询不迁)
    notes_line = (
        f"\n飞书人工标注 (pt_meta.notes): {pt_notes[:MAX_NOTES_CHARS]}"
        if pt_notes
        else ""
    )
    # 本 PT 的准入要求(walmart_pt_meta.requirements)—— R3 硬拒的替身:
    # "这个类目要什么证"是事实,"这个具体产品要不要"是判断,后者交 LLM
    req_block = (
        f"\n# 本 PT 的沃尔玛准入要求\n\n{pt_requirements[:MAX_REQ_CHARS]}\n"
        if pt_requirements
        else ""
    )

    return f"""# 产品信息

ASIN: {product.asin}
标题: {product.title}
品牌字段(商家填报): {product.brand or '(空)'}
Amazon 类目: {product.amazon_category_path or '(空)'}
沃尔玛 PT: {l1.walmart_product_type or '(空)'} (置信 {l1.pt_confidence}, 源 {l1.pt_source})
沃尔玛 Category: {l1.walmart_category or '(空)'}{notes_line}

五点描述:
{bullets_txt}

长描述:
{desc or '(空)'}
{req_block}
# 上游证据

{ev_txt}

# 待评估的品牌/商标词 (来自上游证据, 前 10 个, 综合上下文判 is_real_brand)

{brand_list}
"""


# =============================================================
# 回复解析
# =============================================================


@dataclass
class L3Result:
    """L3 语义审核结果(三段输出:verdict / policy / detail)。

    verdict:'pass' / 'reject' / 'pending';hits 至多一条且 penalty 恒 0
    (L3 不动分数,score_final 始终是 L2 的值)。
    policy 落 `audit_runs.l3_reason_category`(列名不改,语义 = 类别枚举),
    detail 落 `l3_reason_text`(列名不改,语义 = 具体内容)。
    raw 是 LLM 原始 dict,不落库。
    """

    verdict: str
    policy: str = "none"
    detail: str | None = None
    brand_verdicts: list = field(default_factory=list)
    confidence: str = "medium"
    raw: dict = field(default_factory=dict)
    hits: list[RuleHit] = field(default_factory=list)


_VALID_VERDICTS = {"pass", "reject"}
_VALID_CONFIDENCE = {"high", "medium", "low"}
_SLUG_RE = re.compile(r"[^a-z0-9]+")

NO_POLICY = "none"          # pass 的类别位(不是政策,也不落 audit_reason)
MAX_DETAIL = 500            # 提示词要求 ≤120 字,落库上限给 500(截断不改判定)
MAX_RAW_SNIPPET = 300
MAX_ERROR_SNIPPET = 500


def policy_enum(known_policies) -> frozenset:
    """输入:实时政策 category_en 集合 → 输出:`policy` 的合法取值(表内原拼写)。

    = **全部**政策类别名 + 两条非政策类别(`内部黑名单` / `类目准入`)。
    `none` 不在里面:它是 pass 的占位,不是能落库的类别。
    与 S2 候选块同源同拼写(两边吃的都是同一个 `SELECT category_en`),
    LLM 看到什么就只能答什么。
    """
    return frozenset(known_policies) | set(resources.AUDIT_NONPOLICY_CATEGORIES)


def _slug_category(cat: str) -> str:
    """输入:类别名 → 输出:rule_code 用 slug('Home Goods' → 'home_goods')。"""
    return _SLUG_RE.sub("_", (cat or "").lower()).strip("_") or "unknown"


def _pending(detail_text: str | None, rule_code: str, detail: dict,
             raw: dict) -> L3Result:
    """输入:失败要素 → 输出:pending 形态 L3Result(penalty=0 的一条告警 hit)。"""
    return L3Result(
        verdict="pending",
        policy=NO_POLICY,
        detail=detail_text,
        confidence="low",
        raw=raw,
        hits=[RuleHit(stage="L3", rule_code=rule_code, penalty=0, detail=detail)],
    )


def parse_l3_reply(raw: dict, allowed: frozenset | set) -> L3Result:
    """输入:LLM JSON dict + 类别枚举(表内原拼写)→ 输出:规范化 L3Result。

    顺序即语义(规格 §3.3):

      1. 非 JSON / verdict 取值非法 → pending `llm_bad_json`(绝不默认放行);
      2. 品牌翻拒:任一 `brand_verdicts[].is_real_brand is True` 且 LLM 自述
         pass → 改判 reject + `Intellectual Property`(确定性后处理,严格
         `is True`,字符串 "true" 不算);
      3. pass → `policy` 强制 `none`(pass 没有类别);
      4. reject → `policy` 经 `policy_names.resolve` 对枚举解析,命中回**表内
         原拼写**;**对不上 → pending `llm_bad_policy`**(不猜、不降级:猜出来
         的类别会一路落库、进飞书、进申诉口径,而没有任何东西会红);
      5. reject 落 1 条 L3 hit,rule_code = `llm_<policy slug>`,detail 五键定序
         `{policy, detail, confidence, brand_verdicts, prompt_version}`。
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

    detail = str(raw.get("detail") or "").strip()[:MAX_DETAIL] or None

    brand_verdicts = raw.get("brand_verdicts") or []
    if not isinstance(brand_verdicts, list):
        brand_verdicts = []

    confidence = str(raw.get("confidence") or "medium").strip().lower()
    if confidence not in _VALID_CONFIDENCE:
        confidence = "medium"

    policy = str(raw.get("policy") or "").strip()

    # ⭐ 品牌翻拒(合同 L3-7 照迁):任一命中词被判 is_real_brand=true → 整品
    # reject,即使 LLM 自己输出 pass。类别恒 `Intellectual Property` ——
    # 它是规则代码里唯一写死的政策名,装配时已对表(audit_rules.load_context)
    real_brand_hits = [v for v in brand_verdicts
                       if isinstance(v, dict) and v.get("is_real_brand") is True]
    if verdict == "pass" and real_brand_hits:
        first = real_brand_hits[0].get("brand") or "?"
        logger.info("L3 verdict override: pass→reject, is_real_brand=true: %s",
                    [v.get("brand") for v in real_brand_hits])
        verdict = "reject"
        policy = resources.AUDIT_IP_POLICY
        detail = f"未授权引用品牌名 {first}"

    if verdict == "pass":            # pass 没有类别(prompt 也这么要求)
        return L3Result(verdict="pass", policy=NO_POLICY, detail=detail,
                        brand_verdicts=brand_verdicts, confidence=confidence,
                        raw=raw)

    hit_policy = policy_names.resolve(policy, allowed)
    if hit_policy is None:
        # 不猜:`none`、自造类别、拼错的官方名全走这一路。计数进 run 摘要 ——
        # "LLM 答不出合法类别"是提示词/政策表出了问题的信号,不是单品的事
        bump("llm_bad_policy")
        logger.warning("L3 policy %r 不在类别枚举里 → pending(不猜类别)", policy)
        return _pending("L3 政策类别对不上枚举, 待人工复核", "llm_bad_policy",
                        {"policy": policy[:MAX_RAW_SNIPPET],
                         "detail": detail}, raw)

    return L3Result(
        verdict="reject",
        policy=hit_policy,
        detail=detail,
        brand_verdicts=brand_verdicts,
        confidence=confidence,
        raw=raw,
        hits=[RuleHit(
            stage="L3",
            rule_code=f"llm_{_slug_category(hit_policy)}",
            penalty=0,   # L3 不扣分, 直接决定 verdict
            detail={     # 五键定序是落库契约
                "policy": hit_policy,
                "detail": detail,
                "confidence": confidence,
                "brand_verdicts": brand_verdicts,
                "prompt_version": resources.AUDIT_RULES_VERSION,
            },
        )],
    )


# =============================================================
# 入口
# =============================================================


def judge_l3(product, l1, l2, ctx, conn, *, phase0=None) -> L3Result:
    """输入:产品 + L1Info + L2Result + AuditContext + 连接(+ Phase0Result)
    → 输出:L3Result。

    调用方(audit_rules.audit_one)保证只对 L2 pass 的产品调用,本函数不再检查。
    流程:进程级 system prompt → 组 user prompt(上游三层软证据 + 本 PT 准入
    要求)→ llm_cache 查(键含 model/温度/max_tokens/purpose)→ 未命中则调
    chat_json → 解析。**pending 不写缓存**(旧仓同款:失败/异常/坏 JSON 都不写)。

    失败一律 pending(合同全局节):重试尽(RuntimeError)→ `llm_chain_exhausted`;
    其他异常(4xx / 坏 JSON 抛 ValueError 等)→ `llm_error`;
    坏 JSON / verdict 非法 → `llm_bad_json`;政策类别对不上 → `llm_bad_policy`。
    四者 penalty 均 0,分数保留 L2 值。
    """
    pt = l1.walmart_product_type or ""
    meta = (ctx.pt_meta.get(pt) or {}) if pt else {}
    notes = meta.get("notes")
    pt_notes = (str(notes).strip() or None) if notes else None
    req = meta.get("requirements")
    pt_req = (str(req).strip() or None) if req else None

    messages = [
        {"role": "system", "content": system_prompt(conn)},
        {"role": "user", "content": build_user_prompt(
            product, l1, l2, phase0=phase0, pt_notes=pt_notes,
            pt_requirements=pt_req)},
    ]
    allowed = policy_enum(ctx.known_policies)

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
    "NO_POLICY",
    "judge_l3",
    "load_policy_rows",
    "load_reason_categories",
    "system_prompt",
    "build_system_prompt",
    "build_user_prompt",
    "format_policy_block",
    "format_reason_categories",
    "summarize_evidence",
    "policy_enum",
    "policies_without_full_text",
    "STATS",
    "reset_stats",
    "parse_l3_reply",
    "reset_prompt_cache",
]
