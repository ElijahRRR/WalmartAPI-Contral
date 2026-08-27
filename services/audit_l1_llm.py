"""L1 类目判定第三级:映射表候选召回 + DeepSeek rerank(移植自旧仓 pipelines/l1_category.py)。

批次 B 的 `audit_rules.resolve_pt` 已实现前两级(①沃尔玛实证 / ②映射表高置信唯一);
本模块补上第三级——两级都解不出 PT 时,用 walmart_category_map 召回候选 PT,
交 LLM 从候选里挑一个(rerank),挑不出就交回 pending。

本模块只提供积木,不接线(接线在 services/audit_rules.py 与 workflows/product_audit.py):

  candidates(conn, product)            → 七路候选(exact/prefix/leaf/title_keyword/
                                         title_literal/ancestor_N/pt_dict)
  rerank(product, cands, pt_dict)      → L1Info(判出来了) 或 None(解不出 → 调用方转 pending)
  check_publication_ban(pt)            → 出版物类 PT 硬禁 RuleHit 或 None(L1 三级共用)

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
from typing import Any

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
    "unknown", "no_candidate",
    "publication_forbidden", "conf_low",
    # 两阶段开放判定(七路候选全空时;所有者定稿 2026-08-14)
    "open_stage1_called", "open_stage1_failed", "open_stage1_empty",
    "open_stage2_candidates", "open_stage2_scored",
    # 候选都不合适时的二次机会(所有者定稿 2026-08-14:"真的都不合适,那也不行")
    "unknown_retry_called", "unknown_retry_saved",
    # ⚠ 2026-08-20 删掉 seed_excluded / seed_excluded_direct / llm_excluded 三个键:
    # seed yaml 的「3C/服饰/汽配/带电禁售」整条链已下线(所有者定稿 A1),
    # 类目能不能做只由 L2 R1 的白名单说了算。
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


def check_publication_ban(pt: str | None) -> RuleHit | None:
    """输入:PT → 输出:出版物硬禁 RuleHit 或 None。

    逐字迁自 l1_category.py:735-754,条件与旧仓一致:PT 在硬禁集合内即命中。
    ⚠ 2026-08-20 去掉 `existing_reason` 形参:它原本是"已有 excluded 理由就不叠加"
    的闸,而 excluded 那条链已整体下线(所有者定稿 A1),形参永远是 None。
    调用方按需自行去重(resolve_pt 每个产品只调一次)。

    合同 L1-6:旧仓该段对**全部三级**生效(实证/映射直出也过)。unknown/(unknown)
    无需另判——它永不在硬禁集合内。
    """
    if pt not in PUBLICATION_HARD_FORBID:
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

# 映射表哨兵值(675 行),不是真 PT。定义在此而非 audit_rules:两处都要用,
# 而 audit_rules 已 import 本模块,反向 import 会成环。
UNMAPPED_SENTINEL = "无对应Walmart PT"

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


# 第六路 祖先映射:沿 amazon_node_paths 父链上溯,取最近的已映射祖先的 PT。
# **只当候选不直出**——祖先必然比商品粗一级(Golf Cart Accessories 的父是
# Golf),粗对不对由 LLM 拿标题判;正因为不直出,不必纠结"退到第几级为止"
# (直出才需要那条线)。DAG 多父 = 多条上溯支路,递归天然覆盖。
_ANCESTOR_SQL = """
WITH RECURSIVE up AS (
    SELECT parent_node_id AS node, 1 AS dist
    FROM audit.amazon_node_paths
    WHERE node_id = %(node)s AND parent_node_id <> ''
  UNION ALL
    SELECT p.parent_node_id, up.dist + 1
    FROM audit.amazon_node_paths p
    JOIN up ON p.node_id = up.node
    WHERE p.parent_node_id <> '' AND up.dist < %(max_up)s
)
SELECT DISTINCT ON (m.walmart_product_type)
       m.amazon_category, m.walmart_product_type, m.confidence, up.dist
FROM up
JOIN audit.walmart_category_map m ON m.browse_node_id = up.node
INNER JOIN audit.walmart_pt_meta meta
        ON meta.walmart_product_type = m.walmart_product_type
WHERE m.walmart_product_type <> %(sentinel)s
ORDER BY m.walmart_product_type, up.dist
"""

# 第七路 PT 字典直搜:前六路的候选**全部来自映射表**——映射表没有的类目
# 就等于候选池空,LLM 连挑的机会都没有(所有者指出 2026-08-14)。这一路
# 直接搜 PT 字典本身(pt_meta 的 PT 名 / PTG 名),用 Amazon 叶子类目名 +
# 标题关键词。"Golf Cart Accessories" 拿 golf 去搜就有候选。零 LLM。
_PT_DICT_SQL = """
SELECT walmart_product_type, walmart_category, walmart_ptg,
       (SELECT count(*) FROM unnest(%(pats)s::text[]) AS w
         WHERE walmart_product_type ILIKE w) * 2
     + (SELECT count(*) FROM unnest(%(pats)s::text[]) AS w
         WHERE coalesce(walmart_ptg, '') ILIKE w) AS score
FROM audit.walmart_pt_meta
WHERE walmart_product_type ILIKE ANY(%(pats)s)
   OR coalesce(walmart_ptg, '') ILIKE ANY(%(pats)s)
ORDER BY score DESC, length(walmart_product_type)
LIMIT %(limit)s
"""

# 两阶段开放判定的第二阶段:在 LLM 选中的大类里按**与本产品的相关度**排序。
# ⚠ 这里必须排序不能裸取(2026-08-14 生产 bug):原实现 ORDER BY 名字母序,
# 而提示词只喂前 _PROMPT_CAND_CUT 条 ⇒ Home 大类 891 个 PT 只有 A 开头的
# 20 个能进提示词,`Cookware Sets` 这种 C 开头的永远见不到 LLM。评分口径与
# 第七路同款(PT 名命中 ×2,PTG 名命中 ×1),但**不加 WHERE 过滤**:一个词
# 都不命中的 PT 也要留在候选里(大类已经由 LLM 选定,这是兜底面不是搜索面)
_OPEN_SCORE_SQL = """
SELECT walmart_product_type, walmart_category, walmart_ptg,
       (SELECT count(*) FROM unnest(%(pats)s::text[]) AS w
         WHERE walmart_product_type ILIKE w) * 2
     + (SELECT count(*) FROM unnest(%(pats)s::text[]) AS w
         WHERE coalesce(walmart_ptg, '') ILIKE w) AS score
FROM audit.walmart_pt_meta
WHERE walmart_category = ANY(%(cats)s)
ORDER BY score DESC, length(walmart_product_type), walmart_product_type
LIMIT %(limit)s
"""

_MAX_UP = 6          # 上溯层数上限(纯防环/防深链,不是语义门槛)
_ANCESTOR_LIMIT = 6
_PT_DICT_LIMIT = 10
# 提示词实际喂给 LLM 的候选条数上限。**开放判定取多少必须与它一致**——
# 取多了是白查(超出部分构建提示词时就被切掉),取少了是白白缩窄候选面
_PROMPT_CAND_CUT = 20


def _fetch_ancestor(cur, product: ProductInfo) -> list[dict[str, Any]]:
    """输入:游标 + 产品 → 输出:第六路候选(最近已映射祖先的 PT,带层距)。"""
    if not product.browse_node_id:
        return []
    cur.execute(_ANCESTOR_SQL, {"node": product.browse_node_id,
                                "max_up": _MAX_UP,
                                "sentinel": UNMAPPED_SENTINEL})
    rows = sorted(_rows(cur), key=lambda r: r["dist"])[:_ANCESTOR_LIMIT]
    return [{"walmart_product_type": r["walmart_product_type"],
             "confidence": r["confidence"],
             "amazon_category": r["amazon_category"],
             "match_type": f"ancestor_{r['dist']}"} for r in rows]


def _dict_words(product: ProductInfo) -> list[str]:
    """输入:产品 → 输出:搜 PT 字典用的词(类目路径中下段的词 + 标题关键词)。

    **不只取叶子**(2026-08-14 修):`Home & Kitchen > Kitchen & Dining >
    Cookware > Pots` 的叶子是 `Pots`,而 PT 字典里那条叫 `Cookware Sets`
    ——最有用的词恰好在倒数第二段,只取叶子就永远搜不到它。
    从叶子往回走,近的先占名额(叶子最具体);**第一段(Amazon 一级大类)
    不取**:`Home & Kitchen` / `Tools & Home Improvement` 这类词泛到能命中
    上百个 PT,进了评分只会把信号摊平。
    """
    path = (product.amazon_category_path or "").strip()
    segs = [s.strip() for s in path.split(">")] if path else []
    out: list[str] = []
    seen: set[str] = set()
    for seg in reversed(segs[1:] if len(segs) > 1 else segs):
        for w in seg.split():
            w = w.strip(" ,&/()")
            if len(w) >= 4 and w.lower() not in _STOP and w.lower() not in seen:
                out.append(w)
                seen.add(w.lower())
    for kw in _title_keywords(product.title or ""):
        if kw.lower() not in seen:
            out.append(kw)
            seen.add(kw.lower())
    return out[:8]


def _fetch_pt_dict(cur, product: ProductInfo) -> list[dict[str, Any]]:
    """输入:游标 + 产品 → 输出:第七路候选(直接搜 PT 字典,不经映射表)。"""
    words = _dict_words(product)
    if not words:
        return []
    cur.execute(_PT_DICT_SQL, {"pats": [f"%{w}%" for w in words],
                               "limit": _PT_DICT_LIMIT})
    return [{"walmart_product_type": r["walmart_product_type"],
             "confidence": None,
             "amazon_category": r.get("walmart_ptg") or "",
             "match_type": "pt_dict"} for r in _rows(cur)]


def candidates(conn, product: ProductInfo, *, limit: int = _KW_LIMIT) -> list[dict[str, Any]]:
    """输入:中心库连接 + 产品 → 输出:候选 PT 列表(五路合并去重,顺序即优先级)。

    合并顺序逐字迁自 l1_category.py:283-313(embedding 一路按计划 10.3 裁掉,
    合并壳退化为顺序拼接,但**顺序语义原样保留**):
      1. exact / prefix / leaf(walmart_category_map 精确匹配,必须最前)
      2. title_keyword(关键词补充)
      3. title_literal(标题字面词反查,第五路)
      4. **ancestor_N**(第六路,2026-08-14 新增):沿类目树父链上溯,最近的
         已映射祖先的 PT;排在字面反查之后——祖先粗但方向对,字面词准但易串类
      5. **pt_dict**(第七路,同上):直接搜 PT 字典,不经映射表。前六路全部
         依赖映射表有这个类目,映射表没有 ⇒ 候选池空 ⇒ LLM 连挑的机会都没有
         (所有者指出:"没有直接拿到映射类目的,其实都是拿候选然后让 LLM 输出")
    每条候选:walmart_product_type / confidence / amazon_category / match_type。
    hybrid 无总量截断——截断只发生在提示词构建时的 `[:_PROMPT_CAND_CUT]`(§4.2)。
    ⚠ **本函数的返回顺序即优先级**,那个截断砍的是尾巴。任何别的候选来源
    (如 open_candidates)交给 rerank 之前必须自己排好序,否则截断退化成
    "按查询顺序发盲牌"(2026-08-14 生产 bug:开放判定按 PT 名字母序取,
    Home 大类 891 个 PT 只有 A 开头的能进提示词)。
    """
    with conn.cursor() as cur:
        kw = _fetch_candidates(cur, product, limit)
        literal = _fetch_title_literal(cur, product)
        ancestor = _fetch_ancestor(cur, product)
        pt_dict = _fetch_pt_dict(cur, product)

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in (
        [c for c in kw if c.get("match_type") in _HIGH_PRIORITY_TYPES],
        [c for c in kw if c.get("match_type") not in _HIGH_PRIORITY_TYPES],
        literal,
        ancestor,
        pt_dict,
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

**L1 只负责类目判定**, 不再判定 "需证书/中国卖家禁售/类目能不能做"。
这些全部由下游 L2 基于沃尔玛官方 PT spec 与类目准入白名单自动完成 —— 你只需要把 PT 选对。
**不要**因为觉得某个类目"我们做不了"就改选别的 PT 或拒绝作答: 选错 PT 会让准入判定查错行, 比选中一个禁售 PT 更糟。

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

# 严格 JSON 输出, 不要 markdown / 不要解释

{
  "walmart_product_type": "...",
  "pt_confidence": "高" | "中" | "低",
  "pt_source": "map_verified" | "llm_direct",
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
        for i, c in enumerate(cands[:_PROMPT_CAND_CUT], 1):
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


_MEGA_SYSTEM_PROMPT = (
    "你是沃尔玛 Marketplace 类目判定专家。给你一个 Amazon 商品和沃尔玛的"
    "**一级大类清单**,请判断它最可能属于哪 1~3 个大类。\n"
    "只依据标题、五点、描述、Amazon 类目路径推断;**只能从清单里选**,"
    "不要发明新名称。\n"
    '返回 JSON:{"categories": ["大类1", "大类2"]};完全判不了返回 '
    '{"categories": []}。'
)


def _mega_categories(cur) -> list[str]:
    """输入:游标 → 输出:PT 字典里的沃尔玛一级大类清单(去重排序)。"""
    cur.execute("SELECT DISTINCT walmart_category FROM audit.walmart_pt_meta "
                "WHERE walmart_category IS NOT NULL "
                "  AND btrim(walmart_category) <> '' ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


def open_candidates(conn, product: ProductInfo, *,
                    chat_fn=None) -> list[dict[str, Any]]:
    """输入:连接 + 产品 → 输出:两阶段 LLM 兜出来的候选(七路全空时才调)。

    所有者定稿 2026-08-14:**没有任何映射参考时也要让 LLM 判**,不能因为
    候选池空就直接 pending("达不到我让 LLM 抉择产品类目的目的")。

    做法是两阶段,始终不脱离 PT 字典:
      ① 把沃尔玛**一级大类清单**(量小,可整份进提示词)+ 标题/五点/描述/
         Amazon 路径给 LLM,让它选 1~3 个大类;
      ② 取这些大类下的**全部 PT** 当候选,交给标准 rerank 挑。
    为什么不让 LLM 直接说 PT:PT 字典几千条塞不进提示词,自由生成的 PT
    过不了 pt_meta 校验,等于白调——旧仓正是这么产 unknown 的。
    失败(异常/坏 JSON/一个大类都没选中)→ 返回空,调用方照旧 pending。
    """
    with conn.cursor() as cur:
        megas = _mega_categories(cur)
    if not megas:
        return []
    # **大类清单放 system 不放 user**(2026-08-14 生产实测:清单每次调用完全
    # 一样,放在 user 段的产品信息之后 ⇒ 一个字节都进不了 DeepSeek 前缀缓存,
    # 可缓存占比只有 188/(188+清单+产品) ≈ 9%,等于每次全价重发)。挪进
    # system 后前缀字节稳定,与 L1/L3 同享缓存红利。
    system = (_MEGA_SYSTEM_PROMPT + "\n\n# 沃尔玛一级大类清单\n"
              + "\n".join(f"  - {m}" for m in megas))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": build_user_prompt(product, [])},
    ]
    bump("open_stage1_called")
    try:
        raw = (chat_fn or _default_chat)(messages)
    except Exception as e:  # noqa: BLE001 —— 同 rerank:重试尽即转 pending
        bump("open_stage1_failed")
        logger.warning("L1 开放判定一阶段失败 asin=%s: %s", product.asin, e)
        return []
    picked = [c for c in ((raw or {}).get("categories") or [])
              if isinstance(c, str) and c in set(megas)]
    if not picked:
        bump("open_stage1_empty")
        return []
    # ⚠ 排序不是可选项(见 _OPEN_SCORE_SQL 头注):大类下动辄几百个 PT,
    # 提示词只喂前 _PROMPT_CAND_CUT 条,不按相关度排就是按字母序发盲牌
    words = _dict_words(product)
    with conn.cursor() as cur:
        cur.execute(_OPEN_SCORE_SQL, {"pats": [f"%{w}%" for w in words],
                                      "cats": picked,
                                      "limit": _PROMPT_CAND_CUT})
        rows = _rows(cur)
    bump("open_stage2_candidates")
    if any(r.get("score") for r in rows):
        bump("open_stage2_scored")   # 有词命中 = 这 20 条是选出来的,不是碰上的
    logger.info("L1 开放判定:大类 %s → 候选 %d(首条 %s score=%s,asin=%s)",
                picked, len(rows), rows[0]["walmart_product_type"] if rows
                else "-", rows[0].get("score") if rows else "-", product.asin)
    return [{"walmart_product_type": r["walmart_product_type"],
             "confidence": None,
             "amazon_category": r.get("walmart_ptg") or "",
             "match_type": "open_mega"} for r in rows]


def rerank(product: ProductInfo, cands: list[dict[str, Any]], pt_dict, *,
           chat_fn=None) -> L1Info | None:
    """输入:产品 + 候选列表 + PT 字典 → 输出:重排结果 L1Info(或 None)。

    薄壳:只要结果不要原因(旧调用点与测试沿用),全文在 rerank_ex。
    """
    return rerank_ex(product, cands, pt_dict, chat_fn=chat_fn)[0]


def rerank_ex(product: ProductInfo, cands: list[dict[str, Any]], pt_dict, *,
              chat_fn=None) -> tuple[L1Info | None, str]:
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
    if not cands:
        # 合同 L1-5:旧仓照调 LLM 且几乎必然产 unknown(自由判的 PT 过不了字典校验),
        # 终点同为 pending,省一次调用。
        bump("no_candidate")
        logger.info("L1 rerank 无候选,直接 pending:asin=%s", product.asin)
        return None, "no_candidate"

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
        return None, "llm_failed"
    if not isinstance(raw, dict) or not raw:
        bump("bad_json")
        logger.warning("L1 rerank 回复非法 asin=%s: %r → pending", product.asin, raw)
        return None, "bad_json"

    got = _coerce(product, raw, cands, pt_dict)
    return got, ("ok" if got is not None else "unknown")


def _coerce(product: ProductInfo, raw: dict[str, Any],
            cands: list[dict[str, Any]], pt_dict) -> L1Info | None:
    """输入:LLM 回复 JSON + 候选 → 输出:L1Info 或 None(解不出)。

    逐字迁自 l1_category.py:922-1008(_coerce_llm),失败分流按新系统修正:
    unknown 不再原样进 L2(旧仓 unknown 会让 L2 硬闸集体失明、分数保持 100
    → 假 pass,spec §7 F6),改返回 None。

    ⚠ 2026-08-20 删掉 `excluded_category_reason` 分支(所有者定稿 A1):
    LLM 被要求给「3C禁售/服饰禁售/汽配禁售/带电产品禁售」四选一的标签,
    命中即 -100 硬拒 —— 那是**让模型凭标题猜类目禁令**,和 R1 白名单
    (按 PT 查沃尔玛准入事实)讲同一件事,而且猜的那一份还压在事实前面。
    现在类目能不能做只有 R1 一处判据,模型只负责选 PT。
    """
    pt = str(raw.get("walmart_product_type") or "unknown").strip() or "unknown"
    conf = str(raw.get("pt_confidence") or "低").strip()
    if conf not in {"高", "中", "低"}:
        conf = "低"
    source = str(raw.get("pt_source") or "llm_direct").strip()
    if source not in {"map_verified", "llm_direct"}:
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

    if pt == "unknown":
        # F4/F5:LLM 主动输出 unknown,或字典外 PT 且候选全不可用 → pending。
        # 提示词自己写着"这种产品会后续人工处理",旧系统没兑现(§7 F5/F6)。
        bump("unknown")
        logger.info("L1 rerank 输出 unknown,转 pending:asin=%s", product.asin)
        return None

    l1 = L1Info(walmart_product_type=pt, pt_confidence=conf, pt_source=source)
    # 归因:LLM 最终选中的这个 PT 是哪一路召回来的。没有这个计数就说不清
    # 新加的第六/七路到底出没出力(no_candidate 归零可能是它们的功劳,也可能
    # 本来就是 0)——铁律:兜底/新路径触发必须可计数。
    picked = next((c for c in cands
                   if str(c.get("walmart_product_type") or "").strip() == pt),
                  None)
    bump(f"picked_{picked.get('match_type') or 'unknown'}" if picked
         else "picked_off_candidates")
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
    # 接线层只对①②直出级调它,第三级走这里,两条路互斥,不会重复挂 hit。
    ban = check_publication_ban(pt)
    if ban is not None:
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
    "check_publication_ban",
]
