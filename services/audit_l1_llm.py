"""L1 类目判定第三级:映射表候选召回 + DeepSeek rerank(移植自旧仓 pipelines/l1_category.py)。

批次 B 的 `audit_rules.resolve_pt` 已实现前两级(①沃尔玛实证 / ②映射表高置信唯一);
本模块补上第三级——两级都解不出 PT 时,用 walmart_category_map 召回候选 PT,
交 LLM 从候选里挑一个(rerank),挑不出就交回 pending。

本模块只提供积木,不接线(接线在 services/audit_rules.py 与 workflows/product_audit.py):

  candidates(conn, product)            → 五路候选(exact/prefix/leaf/title_keyword/title_literal)
  rerank(product, cands, pt_dict)      → L1Info(判出来了) 或 None(解不出 → 调用方转 pending)
  check_seed_excluded(product, pt)     → seed yaml 禁售品类 reason 或 None(L1 三级共用)
  check_publication_ban(pt, reason)    → 出版物类 PT 硬禁 RuleHit 或 None(L1 三级共用)

失败语义(合同全局节 / 计划 10.2:单链无自动降级):LLM 抛异常、坏 JSON、
LLM 输出 unknown、字典外 PT 且候选全不可用、无候选——一律返回 None 由调用方
产 pending。旧仓的 `_classify_db_only`(无脑取候选首条当权威 PT)是事故面,不迁。

层次:services 层,可读 DB(经 registry/db 的连接由调用方传入)与调 api/llm;
业务判断全在这里,api 层零业务判断。
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from functools import lru_cache
from typing import Any

import yaml

from registry import paths
from services.audit_models import L1Info, ProductInfo, RuleHit

logger = logging.getLogger("services.audit_l1_llm")

# 候选召回条数:旧仓 hybrid 用 kw_k=15 调 _fetch_candidates(l1_category.py:281),
# 第五路 title_literal 另加 ≤10 条;提示词侧再截 [:20](§4.2)。
_KW_LIMIT = 15

# rerank 请求参数(spec §5):temperature 0.1 是旧仓 chat_json 默认值
# (integrations/llm_client.py:64),**不是** api/llm.py 的 0.2(那是 listing
# 属性映射的实证值,与 L1 无关);max_tokens 2048 的旧注释逐字:
# "reasoning 模型的 thinking 占输出大头 / 500 太小会让 content JSON 被截断
#  (实测 9.4% 失败率) / 2048 给 thinking ~1500 + content ~500 留够空间"。
_TEMPERATURE = 0.1
_MAX_TOKENS = 2048
_PURPOSE = "audit_l1"          # → registry.LLM_PURPOSE_ENV['audit_l1']

# 兜底/失败计数(铁律:兜底触发必须记日志计数——静默常态化=主路径已坏没人知道)。
# 单进程批处理内累加,run 摘要读它(spec §11.5);reset_stats() 在每轮开始调。
STATS: Counter = Counter()

_STATS_KEYS = (
    "llm_called", "llm_failed", "bad_json", "dict_fallback",
    "unknown", "no_candidate", "seed_excluded", "llm_excluded",
    "publication_forbidden", "conf_low",
    "seed_excluded_direct",   # 接线层:①②直出级 seed 命中(与 rerank 级分开)
)


_STATS_LOCK = threading.Lock()


def bump(key: str) -> None:
    """输入:计数键 → 输出:无(线程安全 +1;workers>1 时裸 += 会丢计数)。"""
    with _STATS_LOCK:
        STATS[key] += 1


def reset_stats() -> None:
    """输入:无 → 输出:无(把 STATS 清零并把全部键补 0,便于摘要直接取)。"""
    with _STATS_LOCK:
        STATS.clear()
        STATS.update({k: 0 for k in _STATS_KEYS})


reset_stats()


# =============================================================
# seed yaml 禁售品类(3C / 服饰 / 汽配 / 带电)—— L1 三级共用
# =============================================================


@lru_cache(maxsize=1)
def _load_excluded_rules() -> tuple[dict[str, Any], ...]:
    """输入:无 → 输出:seed yaml 的 excluded_categories 条目元组(顺序即优先级)。

    逐字迁自 l1_category.py:48-54(文件不存在返回空,静默);新仓路径经
    registry.paths.audit_seed_file。audit_l2.py:105 明写"excluded_categories
    整节是 L1 消费的,这里从不读"——本函数是该节的唯一消费方。
    """
    seed = paths.audit_seed_file("forbidden_categories_zh_seller.yaml")
    if not seed.exists():
        return ()
    with seed.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return tuple(data.get("excluded_categories", []) or [])


def check_seed_excluded(product: ProductInfo, walmart_pt: str = "") -> str | None:
    """输入:产品(+可选已知 PT)→ 输出:seed yaml 命中的禁售 reason 或 None。

    逐字迁自 l1_category.py:128-144:三个 haystack 全小写、子串匹配、
    **首命中即返回**(yaml 条目顺序即优先级)、scope 缺省 'walmart_pt'。
    `title_keyword` 这一 haystack 当前 yaml 无条目使用(死路径,但保留:yaml 可加)。
    """
    haystacks = {
        "walmart_pt": (walmart_pt or "").lower(),
        "amazon_category": (product.amazon_category_path or "").lower(),
        "title_keyword": (product.title or "").lower(),
    }
    for rule in _load_excluded_rules():
        m = (rule.get("match") or "").lower()
        scope = rule.get("scope") or "walmart_pt"
        reason = rule.get("reason") or f"seed-{scope}"
        if not m:
            continue
        hay = haystacks.get(scope, "")
        if m in hay:
            return reason
    return None


# =============================================================
# 出版物类 PT 硬禁(旧仓 _apply_spec_override 第 3 段)—— L1 三级共用
# =============================================================

# 基于 walmart_error_records 实证:这 5 类 PT 在 9 天 Walmart 日报里 E 知产占比
# 96-100%,搬运模式无法合规。注意 Planners (5.8% E) / Record Books (19% E)
# 不含——误伤高(l1_category.py:735-739, 759-767)。
PUBLICATION_HARD_FORBID = frozenset({
    "Books",
    "Manuals & Guides",
    "Comics",
    "Sheet Music",
    "Autographed Collectibles",
})

# 已知缺陷照迁(spec §8):源码就是这个**无插值的 f-string**,原样保留字面量。
PUBLICATION_BAN_REASON = "出版物类 PT 搬运无法合规 (数据验证 E 知产占比 ≥96%)"


def check_publication_ban(pt: str | None,
                          existing_reason: str | None = None) -> RuleHit | None:
    """输入:PT(+已有的 excluded 理由)→ 输出:出版物硬禁 RuleHit 或 None。

    逐字迁自 l1_category.py:735-754。条件与旧仓一致:PT 在硬禁集合内**且**
    还没有别的 excluded 理由(已有理由时旧仓不叠加,故本函数返回 None)。
    命中时调用方应同时把 `hit.detail["reason"]` 写进 L1Info.excluded_category_reason。

    合同 L1-6:旧仓该段对**全部三级**生效(实证/映射直出也过),批次 B 的
    resolve_pt 漏接——本函数供三级共用;重复调用天然幂等(第二次 existing_reason
    已非空 → None)。unknown/(unknown) 由 existing_reason 之外的调用方过滤:
    旧仓 _apply_spec_override 对 unknown PT 直接原样返回(:675-677),
    而 unknown 永不在硬禁集合内,故此处无需另判。
    """
    if pt not in PUBLICATION_HARD_FORBID or existing_reason:
        return None
    bump("publication_forbidden")
    return RuleHit(
        stage="L1",
        rule_code="publication_pt_forbidden",
        penalty=-100,
        detail={
            "walmart_pt": pt,
            "reason": PUBLICATION_BAN_REASON,
            "basis": "walmart_error_records 9 天数据: Books 96.7% / Manuals 100% "
                     "/ Comics 100% / Sheet Music 100% / Autographed 100%",
        },
    )


# =============================================================
# 候选召回(五路 SQL;旧仓 _fetch_candidates + _fetch_candidates_hybrid 第五路)
# =============================================================

# 逐字照抄 l1_category.py:168-170。已知冗余照迁(spec §3.2):"pack" 重复两次、
# "pack of" 含空格永不可能命中(clean 已去掉空格)、长度 <4 的词本就被门槛滤掉
# ——集合有冗余但无害;改动即改召回口径,双跑会飘。
_STOP = {"with", "from", "your", "this", "that", "have", "will", "been", "set",
         "pack", "kit", "inch", "inches", "mm", "cm", "pcs", "pack", "pack of",
         "the", "for", "and", "or", "of", "to", "in", "on", "by", "as", "at"}

# 第五路 title_literal 的停用词表:与上表**故意不同**(10 个,含 these/their),
# 逐字照抄 l1_category.py:344-345。
_STOP_LITERAL = {"with", "from", "your", "this", "that",
                 "have", "will", "been", "these", "their"}

# 合并优先级:walmart_category_map 精确匹配必须排到其余召回之前
# (l1_category.py:283-286 实证注释:B0FDW1J3NZ "Gate Openers" 的 exact match
#  被挤到第 41 位,提示词 [:20] 截断后 LLM 看不到 → 输 unknown)。
_HIGH_PRIORITY_TYPES = {"exact", "prefix", "leaf"}

# 候选只取这三列:旧仓 SELECT 里的 requires_certificate / zh_seller_forbidden /
# requirements 是 sync_category_map.py:94-98 硬编码的 False/False/None 占位,
# 且 ON CONFLICT 不更新(:140-148)——死数据,不迁(spec §9 N8)。
_CAND_COLS = "m.amazon_category, m.walmart_product_type, m.confidence"

# 置信度排序表达式,四路 SQL 共用(逐字)。
_CONF_ORDER = ("CASE m.confidence WHEN '高' THEN 0 WHEN '中' THEN 1 "
               "WHEN '低' THEN 2 ELSE 3 END")


def _rows(cur) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _title_keywords(title: str) -> list[str]:
    """输入:标题 → 输出:≤6 个关键词(去所有非字母数字、≥4 字符、过停用词)。

    逐字迁自 l1_category.py:172-179:`"".join(ch for ch in w if ch.isalnum())`
    连词内连字符一起删(如 "Anti-Slip" → "AntiSlip"),照抄。
    """
    words: list[str] = []
    for w in title.split():
        clean = "".join(ch for ch in w if ch.isalnum())
        if len(clean) >= 4 and clean.lower() not in _STOP:
            words.append(clean)
    return words[:6]


def _literal_keywords(title: str) -> list[str]:
    """输入:标题 → 输出:第五路的字面词(strip 标点、长度 4-30、取前 8、过停用词)。

    逐字迁自 l1_category.py:341-346。与 _title_keywords 的切词口径**故意不同**
    (strip 标点而非删所有非字母数字;长度上限 30;先取前 8 再过停用词)。
    """
    words = [w for w in (title or "").split()
             if 4 <= len(w.strip("., #-&/()")) <= 30]
    clean = [w.strip("., #-&/()").lower() for w in words[:8]]
    return [w for w in clean if w and w not in _STOP_LITERAL]


def _fetch_candidates(cur, product: ProductInfo, limit: int) -> list[dict[str, Any]]:
    """输入:游标 + 产品 + 上限 → 输出:四路候选(exact/prefix/leaf/title_keyword)。

    逐字迁自 l1_category.py:152-271。四路顺序 = 优先级;按 walmart_product_type
    单键去重、**先到先得**;每路的 `len(candidates) < limit` 短路条件照抄。
    四路全部 INNER JOIN walmart_pt_meta 过废弃 PT 闸(:194-196:map 表残留
    'Office Chairs' 一类已改名的 PT,直出会让 LLM 选到上架必失败的名字)。
    """
    path = (product.amazon_category_path or "").strip()
    title = (product.title or "").strip()
    # 无 '>' 时 leaf **等于整条 path**(不是空),照迁(:168)
    leaf = path.split(">")[-1].strip() if path and ">" in path else path
    title_keywords = _title_keywords(title)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _collect(rows, match_type):
        for row in rows:
            key = row["walmart_product_type"]
            if key in seen:
                continue
            seen.add(key)
            row["match_type"] = match_type
            out.append(row)

    if path:
        cur.execute(
            f"""
            SELECT {_CAND_COLS}
            FROM audit.walmart_category_map m
            INNER JOIN audit.walmart_pt_meta meta ON meta.walmart_product_type = m.walmart_product_type
            WHERE m.amazon_category = %s
            ORDER BY {_CONF_ORDER}
            LIMIT 3
            """,
            (path,),
        )
        _collect(_rows(cur), "exact")

    if path and len(out) < limit:
        # 双向前缀(产品路径更长 or 映射路径更长都算),长者优先
        cur.execute(
            f"""
            SELECT {_CAND_COLS}
            FROM audit.walmart_category_map m
            INNER JOIN audit.walmart_pt_meta meta ON meta.walmart_product_type = m.walmart_product_type
            WHERE %s LIKE m.amazon_category || '%%' OR m.amazon_category LIKE %s || '%%'
            ORDER BY LENGTH(m.amazon_category) DESC,
                     {_CONF_ORDER}
            LIMIT 5
            """,
            (path, path),
        )
        _collect(_rows(cur), "prefix")

    if leaf and len(out) < limit:
        # 合同 L1-2:照抄 `raw->>'amazon_leaf'`(新表虽有 amazon_leaf 列,但生产
        # 实表该列非空率未实测,换写法有掉召回风险)。**无 % 包裹 = 整值大小写
        # 不敏感等值**,不是模糊匹配。
        cur.execute(
            f"""
            SELECT {_CAND_COLS}
            FROM audit.walmart_category_map m
            INNER JOIN audit.walmart_pt_meta meta ON meta.walmart_product_type = m.walmart_product_type
            WHERE m.raw->>'amazon_leaf' ILIKE %s
            ORDER BY {_CONF_ORDER}
            LIMIT 5
            """,
            (leaf,),
        )
        _collect(_rows(cur), "leaf")

    if title_keywords and len(out) < limit:
        patterns = [f"%{kw}%" for kw in title_keywords]
        cur.execute(
            f"""
            SELECT {_CAND_COLS},
                   (
                     SELECT COUNT(*) FROM unnest(%s::text[]) AS kw
                     WHERE m.walmart_product_type ILIKE kw
                   ) AS score
            FROM audit.walmart_category_map m
            INNER JOIN audit.walmart_pt_meta meta ON meta.walmart_product_type = m.walmart_product_type
            WHERE m.walmart_product_type ILIKE ANY(%s)
            ORDER BY score DESC,
                     {_CONF_ORDER}
            LIMIT 8
            """,
            (patterns, patterns),
        )
        _collect(_rows(cur), "title_keyword")

    return out[:limit]


def _fetch_title_literal(cur, product: ProductInfo) -> list[dict[str, Any]]:
    """输入:游标 + 产品 → 输出:第五路候选(标题字面词 ILIKE 反查 PT,≤10 条)。

    逐字迁自 l1_category.py:315-349(旧仓这一路另开一个连接,新实现并进同一连接)。
    """
    clean_words = _literal_keywords(product.title or "")
    if not clean_words:
        return []
    patterns = [f"%{w}%" for w in clean_words]
    cur.execute(
        """
        SELECT walmart_product_type, confidence
        FROM (
            SELECT DISTINCT ON (m.walmart_product_type)
                   m.walmart_product_type, m.confidence,
                   CASE m.confidence WHEN '高' THEN 0 WHEN '中' THEN 1 WHEN '低' THEN 2 ELSE 3 END AS _rank
            FROM audit.walmart_category_map m
            INNER JOIN audit.walmart_pt_meta meta ON meta.walmart_product_type = m.walmart_product_type
            WHERE m.walmart_product_type ILIKE ANY(%s)
            ORDER BY m.walmart_product_type, _rank
        ) t
        ORDER BY _rank
        LIMIT 10
        """,
        (patterns,),
    )
    return [{**row, "match_type": "title_literal", "amazon_category": ""}
            for row in _rows(cur)]


def candidates(conn, product: ProductInfo, *, limit: int = _KW_LIMIT) -> list[dict[str, Any]]:
    """输入:中心库连接 + 产品 → 输出:候选 PT 列表(五路合并去重,顺序即优先级)。

    合并顺序逐字迁自 l1_category.py:283-313(embedding 一路按计划 10.3 裁掉,
    合并壳退化为顺序拼接,但**顺序语义原样保留**):
      1. exact / prefix / leaf(walmart_category_map 精确匹配,必须最前)
      2. title_keyword(关键词补充)
      3. title_literal(标题字面词反查,第五路)
    每条候选:walmart_product_type / confidence / amazon_category / match_type。
    hybrid 无总量截断——截断只发生在提示词构建时的 candidates[:20](§4.2)。
    """
    with conn.cursor() as cur:
        kw = _fetch_candidates(cur, product, limit)
        literal = _fetch_title_literal(cur, product)

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in (
        [c for c in kw if c.get("match_type") in _HIGH_PRIORITY_TYPES],
        [c for c in kw if c.get("match_type") not in _HIGH_PRIORITY_TYPES],
        literal,
    ):
        for c in group:
            key = c["walmart_product_type"]
            if key in seen:
                continue
            seen.add(key)
            merged.append(c)
    return merged


# =============================================================
# rerank 提示词(逐字照抄旧仓,不得概括/改写/翻译/"顺手修错别字")
# =============================================================

# 已知缺陷照迁(spec §4.1 + 合同 L1-3):正文写"若 30 个候选里没一个合理",
# user 段标题写 "候选 PT (top 15)",实际喂 ≤20 条——三处数字互不一致,
# 但这是跑了数月的生产版提示词,改动会让判定漂移,原样保留。
# sha256(L1_SYSTEM_PROMPT) == d56b5d6e8b4621f23aa89132f9880fad9840f2215f8f32e9827fc26aaf1d5303
L1_SYSTEM_PROMPT = """你是沃尔玛 Marketplace 类目判定专家。判定 Amazon 商品对应的沃尔玛 Product Type (PT)。
输入来自中国搬运卖家, 无证书/认证, 每日数万产品。

# 核心原则 (优先级从高到低, 前者决定后者)

## (1) Amazon 类目路径是权威指引

如果产品提供了 Amazon 类目路径(如 "Automotive > Replacement Parts > Fuel System > Fittings"),
必须**坚守该路径的分类维度**。

举例:
  - Amazon 是 "Automotive > Fuel System > ..."  → 必须选 Automotive / Fuel 系列的 PT
    (不要跨去 Gas Line Connectors 这种偏家用天然气的 PT)
  - Amazon 是 "Sports & Outdoors > Outdoor > Bird..." → 优先选 Garden & Patio 系列
  - Amazon 是 "Toys & Games > ..."                → 优先选 Toys 系列

**即使标题里有跨维度的关键词, Amazon 路径的分类视角必须坚守。**
标题"Fuel Line Quick Connector for Yamaha Outboard Motor" + Amazon 路径"Automotive > Fuel System"
→ 选 Fuel Tank & Line Repairs / Fuel Injection Accessories & Parts 这种 Automotive Fuel 系列
→ **不要**因为"Line Connector"字面像"Gas Line Connectors"就选家用燃气类

## (2) 必须从候选列表中选 (不能自造 PT 名)

**walmart_product_type 必须是下面候选列表中某一条的原文**, 禁止输出字典里不存在的 PT (否则上架沃尔玛会失败)。

选择规则:
- 候选中有明显契合的 → pt_source='map_verified', **原样复制候选的 walmart_product_type 字段**
- 候选中没有完全契合但有可选的最接近 → pt_source='llm_direct', 仍然**从候选中挑最接近的原文**
- 若 30 个候选里没一个合理 → walmart_product_type='unknown', pt_confidence='低', pt_source='llm_direct'
  (这种产品会后续人工处理)

## (3) PT 名格式强约束

- ✅ "Flagpole Brackets & Mounts"、"Candle Holders"、"Automotive Filter Wrenches"
- ❌ "Sports & Outdoors > Outdoor > ..." (类目路径格式不是 PT 名, 禁止)
- ❌ "Craft Supplies & Tools" (过于宽泛的大类, 避免)
- ❌ "Craft_Cutting_Machine" (下划线命名不是沃尔玛格式, 禁止)
- 长度一般 2-6 个英文单词, 可含 "&" ","

## (4) 职责边界 (重要!)

**L1 只负责类目判定**, 不再判定 "需证书/中国卖家禁售", 这两件由下游 L2 打分引擎基于沃尔玛官方 PT spec 和映射表字段自动完成。

**但**: 若产品属于 3C / 服饰 / 汽配 / 带电产品 (严格定义见下方禁售品类), 需要在 `excluded_category_reason` 直接标明, 由 L1 阶段提前拦截。

## (5) 配件 vs 整机 / 被动 vs 主动 (关键微差别)

**必须先判产品是"整机/整品"还是"配件/替换件"**, 这两类在 Walmart 字典里通常是两个不同的 PT:

- title 含 "Replacement Part / Accessories / Cover / Case / Hood / Tires (for X) / Parts" 等 → 选**配件/替换件**类 PT, **不要**选整机
  - 例: "Pool Cleaner Tires" → 选 "Pool Cleaner Replacement Parts" / "Pool Cleaner Tires" 等配件类, **不是** "Automatic Pool Cleaners"
  - 例: "Dust Cover for Laptop Keyboard" → 选 "Keyboard Covers" / "Computer Accessories", **不是** "Electronics Port Dust Covers" (Port=端口≠Keyboard=键盘)
  - 例: "Replacement Blade for Mower" → 选 "Lawn Mower Replacement Parts", **不是** "Lawn Mowers"

- title 是 **Sensor / 传感器**, 选 Sensor 类 PT, **不要**选 Tester (测试仪) / Meter (测量仪) / Monitor (监视器)
  - 例: "80A Current Sensor" → "Current Sensors", **不是** "Electric Power Testers"

- Dust Cover / Hood / Protector: 保持"保护/覆盖"语义, 不要选测量仪器或端口接口相关 PT

- Amazon 路径与产品实际用途冲突时, **以 title 实际语义为准** (当 Amazon 明显归类错误), 同时把 pt_source 标成 "llm_direct"

# 禁售品类 (严格定义, 不要扩展)

- **3C 电子**: 手机 / 电脑 / 平板 / 耳机 / 智能手表 / 电视 / 相机 / 游戏机
- **服饰**: 衣服 / 鞋子 / 内衣 / 袜子 / 手套 / 帽子 / 围巾
- **汽配**: 汽车零件 / 汽车改装件 / 车用工具 (不含通用五金)
- **带电产品** (严格定义): 主体功能靠电池/充电/通电, 如:
    * 充电宝、移动电源、充电器、电源适配器
    * 单卖的电池 (锂电池、纽扣电池)
    * 电动工具 (主体是电的)
  **不算**带电产品 (可上架):
    * 含 Bluetooth/Wifi 模块但主功能非电的产品 (切割机 / 厨房秤 / 玩具等)
    * 插电家电 (除非本身是 3C 禁售类)
    * LED 装饰灯、LED 蜡烛
    * 内置小纽扣电池的非电子装饰品
    * **Replacement Part / 替换件 / 单卖零件** - 即使原主机是电器, 单卖的替换配件
      (如 Power Switch Replacement Part / 电源开关替换件) 自身不带电不算带电产品

**宁可不标带电产品禁售, 也不要把 "Power Switch Replacement Part" 或 "Bluetooth 切割机" 误判成带电产品。**
只有整机或主体是电池/电源的产品才算。

# 严格 JSON 输出, 不要 markdown / 不要解释

{
  "walmart_product_type": "...",
  "pt_confidence": "高" | "中" | "低",
  "pt_source": "map_verified" | "llm_direct" | "excluded",
  "excluded_category_reason": "3C禁售" | "服饰禁售" | "汽配禁售" | "带电产品禁售" | null,
  "reasoning": "30 字内中文依据"
}
"""


def build_user_prompt(product: ProductInfo, cands: list[dict[str, Any]]) -> str:
    """输入:产品 + 候选 → 输出:rerank 的 user 段文本(逐字迁 :543-582)。

    旧仓优化史(:544-553):候选 ~30→15、描述 800→400 字、五点 5→3 条、
    移除 ASIN/品牌/原产国;"预期 user prompt 从 ~3000 tokens 降到 ~1000,
    cache 命中率 37% → 65%+"。embedding 裁掉后 `sim=` 分支恒不触发,
    每行 tag 恒为 " conf=高/中/低" 或空——该分支不再保留。
    """
    bullets_txt = "; ".join((b[:120] for b in product.bullet_points[:3])) or "(无)"
    desc = (product.long_description or "")[:400]

    if cands:
        cand_lines = []
        for i, c in enumerate(cands[:20], 1):
            conf = c.get("confidence") or ""
            tag = f" conf={conf}" if conf else ""
            cand_lines.append(f"  {i}. {c['walmart_product_type']}{tag}")
        cand_txt = "\n".join(cand_lines)
    else:
        cand_txt = "  (无候选, 自由判)"

    return f"""# 产品
标题: {product.title}
Amazon 类目: {product.amazon_category_path or '(空)'}
五点: {bullets_txt}
描述: {desc or '(空)'}

# 候选 PT (top 15)
{cand_txt}
"""


# =============================================================
# rerank(请求 + 解析 + 失败分流)
# =============================================================


def _in_dictionary(pt: str | None, pt_dict) -> bool:
    """输入:PT + 字典容器 → 输出:该 PT 是否真实可上架。

    等价迁自 l1_category.py:895-919 的双表校验(walmart_pt_meta ∪ walmart_pt_spec),
    数据在新仓已装配进内存(ctx.pt_meta / ctx.pt_spec),**零 DB 往返**。
    """
    p = (pt or "").strip()
    if not p or p in {"unknown", "(unknown)"}:
        return False
    return p in pt_dict


def _default_chat(messages: list[dict]) -> dict:
    from api import llm
    return llm.chat_json(messages, temperature=_TEMPERATURE,
                         max_tokens=_MAX_TOKENS, purpose=_PURPOSE)


def rerank(product: ProductInfo, cands: list[dict[str, Any]], pt_dict, *,
           chat_fn=None) -> L1Info | None:
    """输入:产品 + 候选 + PT 字典 → 输出:L1Info(判出来了)或 None(解不出 → pending)。

    pt_dict 是任何支持 `in` 的 PT 容器,调用方传 `ctx.pt_meta.keys() | ctx.pt_spec.keys()`。
    chat_fn 只为离线测试注入,默认走 api.llm.chat_json(temperature=0.1、
    max_tokens=2048、purpose='audit_l1');**不走 llm_cache**(合同 L1-7:旧 L1
    不开缓存,rerank 结果需随映射表更新)。

    返回 None 的五种"解不出"(合同全局节:一律由调用方转 verdict='pending'):
      - 无候选(合同 L1-5:直接返回,**不调 LLM**)
      - chat_fn 抛异常(api/llm 已内建 3 次同链重试,抛出即重试尽)
      - 回复不是 dict / 空 dict(坏 JSON)
      - LLM 输出 'unknown'
      - LLM 输出字典外 PT 且候选里没有一个可用 PT
    唯一保留的兜底是 F3(字典外 PT → 回落候选首个可用 PT,记 pt_dict_fallback
    hit + 计数):条件封闭、补的是外部模型的缺陷,符合铁律的"真兜底"。
    旧仓 LLM 失败时的 `_classify_db_only`(无脑取候选首条)不迁。

    置信度**零阈值**(合同 L1-4,旧仓同):pt_confidence='低' 也照样采纳,
    只把低置信计数亮进 STATS 供 run 摘要观测。
    """
    # seed yaml 在 LLM **之前**跑且不带 PT —— 已知缺陷照迁(spec §3.1,合同 L1-1:
    # 不补跑)。后果:LLM 选出来的 PT 永远不过 seed 的 walmart_pt scope 9 条规则。
    seed_reason = check_seed_excluded(product)
    if seed_reason:
        bump("seed_excluded")

    if not cands:
        # 合同 L1-5:旧仓照调 LLM 且几乎必然产 unknown(自由判的 PT 过不了字典校验),
        # 终点同为 pending,省一次调用。
        bump("no_candidate")
        logger.info("L1 rerank 无候选,直接 pending:asin=%s", product.asin)
        return None

    messages = [
        {"role": "system", "content": L1_SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(product, cands)},
    ]
    bump("llm_called")
    try:
        raw = (chat_fn or _default_chat)(messages)
    except Exception as e:  # noqa: BLE001 —— 单链重试尽,任何异常都转 pending
        bump("llm_failed")
        logger.warning("L1 rerank LLM 失败 asin=%s: %s → pending", product.asin, e)
        return None
    if not isinstance(raw, dict) or not raw:
        bump("bad_json")
        logger.warning("L1 rerank 回复非法 asin=%s: %r → pending", product.asin, raw)
        return None

    return _coerce(product, raw, seed_reason, cands, pt_dict)


def _coerce(product: ProductInfo, raw: dict[str, Any], seed_reason: str | None,
            cands: list[dict[str, Any]], pt_dict) -> L1Info | None:
    """输入:LLM 回复 JSON + 候选 → 输出:L1Info 或 None(解不出)。

    逐字迁自 l1_category.py:922-1008(_coerce_llm),失败分流按新系统修正:
    unknown 不再原样进 L2(旧仓 unknown 会让 L2 的 R0/R1/R2/R3a 集体失明、
    分数保持 100 → 假 pass,spec §7 F6),改返回 None。
    """
    pt = str(raw.get("walmart_product_type") or "unknown").strip() or "unknown"
    conf = str(raw.get("pt_confidence") or "低").strip()
    if conf not in {"高", "中", "低"}:
        conf = "低"
    source = str(raw.get("pt_source") or "llm_direct").strip()
    if source not in {"map_verified", "llm_direct", "excluded"}:
        source = "llm_direct"

    # F3/F4:字典外 PT 回落到候选里第一个真实可用 PT(逐字 :941-961)
    fallback_from: str | None = None
    if pt != "unknown" and not _in_dictionary(pt, pt_dict):
        fallback_from = pt
        chosen = None
        for c in cands:
            cand_pt = str(c.get("walmart_product_type") or "").strip()
            if _in_dictionary(cand_pt, pt_dict):
                chosen = c
                break
        if chosen:
            pt = str(chosen["walmart_product_type"])
            source = "map_fallback"
            conf = str(chosen.get("confidence") or "低")
            bump("dict_fallback")
            logger.warning("L1 字典外 PT 回落 asin=%s: %r → %r",
                           product.asin, fallback_from, pt)
        else:
            pt, conf, source = "unknown", "低", "none"

    excluded_reason = raw.get("excluded_category_reason")
    if excluded_reason in (None, "null", "None", "", "none"):
        excluded_reason = None
    llm_excluded = bool(excluded_reason)
    # seed 仅补位:LLM 的 excluded 优先(逐字 :967-969)
    if not excluded_reason and seed_reason:
        excluded_reason = seed_reason

    if excluded_reason:
        # excluded 命中即 -100 硬拒,与 pt 是否 unknown 无关:旧仓此路走 reject,
        # 且 -100 已让 L2 直接判死,不存在 F6 的"硬规则失明→假 pass"风险,
        # 故不并入 unknown→pending(reject 比 pending 更贴近旧仓结论)。
        if llm_excluded:
            bump("llm_excluded")
        l1 = L1Info(walmart_product_type=pt, pt_confidence=conf, pt_source=source,
                    excluded_category_reason=excluded_reason)
        l1.hits.append(RuleHit(
            stage="L1", rule_code="excluded_category", penalty=-100,
            detail={
                "reason": excluded_reason,
                "pt": pt,
                "llm_reasoning": raw.get("reasoning"),
                "from_seed_yaml": bool(seed_reason) and not raw.get("excluded_category_reason"),
                "fallback_from_dict_miss": fallback_from,
            }))
        return l1

    if pt == "unknown":
        # F4/F5:LLM 主动输出 unknown,或字典外 PT 且候选全不可用 → pending。
        # 提示词自己写着"这种产品会后续人工处理",旧系统没兑现(§7 F5/F6)。
        bump("unknown")
        logger.info("L1 rerank 输出 unknown,转 pending:asin=%s", product.asin)
        return None

    l1 = L1Info(walmart_product_type=pt, pt_confidence=conf, pt_source=source)
    if fallback_from:
        # penalty=0 的记账型 hit(不影响分数),便于双跑对账 F3 回落率
        l1.hits.append(RuleHit(
            stage="L1", rule_code="pt_dict_fallback", penalty=0,
            detail={
                "llm_output_pt": fallback_from,
                "fallback_pt": pt,
                "reason": "LLM 输出 PT 不在 Walmart 字典, 回落到候选首条",
                "source": source,
            }))
    if conf == "低":
        bump("conf_low")

    # 出版物硬禁(旧仓 _apply_spec_override 第 3 段,第三级出口必经)。
    # 与接线层对①②级的调用天然幂等:此处已写 reason 后再调返回 None。
    ban = check_publication_ban(pt, l1.excluded_category_reason)
    if ban is not None:
        l1.excluded_category_reason = ban.detail["reason"]
        l1.hits.append(ban)
    return l1


__all__ = [
    "STATS",
    "reset_stats",
    "L1_SYSTEM_PROMPT",
    "PUBLICATION_HARD_FORBID",
    "PUBLICATION_BAN_REASON",
    "candidates",
    "rerank",
    "build_user_prompt",
    "check_seed_excluded",
    "check_publication_ban",
]
