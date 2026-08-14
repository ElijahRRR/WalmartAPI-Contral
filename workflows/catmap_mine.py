"""catmap_mine — 从产品实证数据挖类目映射(纯 SQL 零 LLM;可反复跑)。

用法:
  python cli.py catmap_mine                    # 按类目 ID 挖掘(默认,推荐)
  python cli.py catmap_mine -p key=path        # 按类目路径挖掘(无 ID 老行)
  python cli.py catmap_mine -p min_support=8   # 收紧票数门槛(默认 5)
  python cli.py catmap_mine -p min_dominance=0.9  # 收紧优势度(默认 0.8)
  python cli.py catmap_mine -p promote=1       # 把 mined_trusted 升级进映射表

**键 = browse_node_id(所有者定稿 2026-08-14)**:类目名会漂 ID 不会,按
ID 投票天然绕开三套名称不一致;挖出来的映射自带 ID,补进映射表正好填上
ID 缺口(实测产品侧 15,538 个 node 只有 61.5% 在映射表里)。`key=path`
是无 ID 老行的兜底口径(与 2026-08-13 首版一致)。

所有者定稿 2026-08-13 的三段式映射维护顺序,本工作流是第一段:
  ① **数据挖掘(本工作流)**:products 里既有亚马逊类目路径、又有实证
    walmart_pt(pt_backfill 回填 9.5 万 + 审核结论)的产品,按路径分组投票
    ——多个产品指向同一 PT = 可信映射;
  ② 少量支持(2~4 个产品)或有分流的 → 人工核对清单;
  ③ 零数据路径 → catmap_suggest(LLM)/ 人工,从无到有。

分桶(classify_path 纯函数)与写进映射表的置信档:
  mined_trusted  不在映射表 + 单一 PT + 支持数 ≥ min_support(默认 5) → **高**
  mined_review   不在映射表 + 单一 PT + 支持数 2~(min_support-1)     → **中**
  mined_mixed    不在映射表 + 多 PT 分流(distribution 附全分布)      → **低**
  map_conflict   已在映射表但高置信映射 PT ≠ 数据共识 PT             → **中**
                 (旧行不动,实证 PT 另插一条中置信 —— 两条都留给 LLM 挑)
  map_ambiguous  映射表**自己**就挂着多条 PT 不同的高置信行 → 只报不写
  支持数 1 的路径不入桶(单证不立,留给 ③ 段)

**中/低档不是"降级收容",是喂给 LLM 的参考**(所有者定稿 2026-08-14:
"对于有争议的可以算作中等或者低等可信度,把候选交给 LLM 去判就可以了")。
消费端早就在跑:审核链两个直出闸硬筛 `confidence='高'`(audit_rules.py:162/175),
候选召回几路不筛档位、只拿它排序(audit_l1_llm._CONF_ORDER)。所以写中/低
= 给 LLM 多一份参考,**永远不会让弱证据绕过 LLM 直接定 PT**。
这把"长期维护"从人工逐条看冲突,变成机器自动降档 + LLM 兜底。

**升档也是自动的、只升不降**(同日定稿:"如果满足条件,跑一轮数据库也可以
升档"):数据攒够了重跑本工作流,低→中→高 自动往上走;目标档位不高于现档位
的一律跳过(连 match_type 都不动,重跑幂等)。**降档必须人来做**——证据可能
只是暂时变薄(pt_source 回填没跑完、本轮只挖了子集),据此自动降档会让 ②级
直出对整个类目静默失效;而高置信行也可能是人工定的。

与 `catmap_fix`(人工裁决冲突)的交界,一处**已知且有界**的交互:catmap_fix
会把被推翻的旧 PT 降为 '低'。若日后票数又倒回那个旧 PT,本工作流会把它从
低升到 **中**——部分逆转了人的降级动作。之所以不加护栏:中和低都进不了
②级直出(闸硬筛 '高'),两者只差候选排序,人真正的决定"别让这个 PT 直出"
完好无损;而 mined_trusted(唯一能到 '高' 的桶)只在该 key **没有**高置信行
时才可能出现,catmap_fix 之后那个位置被新 PT 占着,所以升不到高。

**只数实证票**(所有者定稿 2026-08-14):`products.walmart_pt` 这一列同时装
沃尔玛回执实证与审核链 LLM 推断,`pt_source` 分道后本工作流只认
`walmart_confirmed`。否则闭环会自我印证:LLM 猜一个 PT → 写进产品行 →
被当实证票挖进映射表 → 该类目全部产品按这个猜测直出,一次猜错永久固化。
⚠ 存量行 pt_source 为 NULL(无从追认来历),按保守口径**不参与投票**,
所以换上这条闸之后票数会明显变少,要等审核重跑把来源补齐才回升。

产品侧 PT 先过 pt_meta 闸(废 PT 不参与投票);写入
audit.category_map_suggestions(与 catmap_suggest 同一张复核面)。
promote=1:把上表四个可写桶按各自档位写进 walmart_category_map(带
browse_node_id,match_type='mined_<桶>');不带 promote 时先打印"将写多少、
其中新增/升档各多少"。⚠ 映射表编辑权威(飞书 vs PG)所有者尚未定稿;
若定飞书,已写入行需同步补录进「映射明细」表。
"""

import json
import logging

from registry import db

DANGEROUS = False

logger = logging.getLogger("workflows.catmap_mine")

# 键 × PT 投票(pt_meta 闸内联:废 PT 不参与;'unknown' 防御性剔除)。
# {key} = p.browse_node_id(默认)或 btrim(p.amazon_category)
_MINE_SQL = """
SELECT {key} AS k, p.walmart_pt, count(*) AS n
FROM catalog.products p
JOIN audit.walmart_pt_meta m ON m.walmart_product_type = p.walmart_pt
WHERE p.marketplace = 'US'
  AND {key} IS NOT NULL AND btrim({key}) <> ''
  AND p.walmart_pt IS NOT NULL AND p.walmart_pt <> 'unknown'
  AND p.pt_source = 'walmart_confirmed'
GROUP BY 1, 2
"""

# 每个 node 的代表路径(产品数最多的那条;升级进映射表时当 amazon_category
# 主键用——映射表 PK 是 (amazon_category, walmart_product_type))
_REP_PATH_SQL = """
SELECT DISTINCT ON (browse_node_id) browse_node_id, btrim(amazon_category)
FROM (
    SELECT browse_node_id, amazon_category, count(*) AS n
    FROM catalog.products
    WHERE marketplace = 'US' AND browse_node_id IS NOT NULL
      AND amazon_category IS NOT NULL AND btrim(amazon_category) <> ''
    GROUP BY 1, 2
) t ORDER BY browse_node_id, n DESC
"""

# ⚠ 返回**高置信 PT 个数**而不是只返回单一 PT(2026-08-14 修一处潜伏 bug):
# 原写法 `CASE WHEN count(DISTINCT pt)=1 THEN min(pt) END` 让"已有两条互相
# 矛盾的高置信行"的 key 返回 NULL,调用方 `if k` 只过滤键、值为 None 就当成
# **没映射过** ⇒ 该 key 会被当新映射挖出来、promote 时再插第三条高置信行。
# 它同时还让 ②级直出对这个 key 永久失明(直出闸要求恰好一个高置信 PT),
# 而全程无人报错。现在把"没映射"与"映射了但自相矛盾"分开:后者只报不写。
_IN_MAP_NODE_SQL = """
SELECT browse_node_id, count(DISTINCT walmart_product_type),
       min(walmart_product_type)
FROM audit.walmart_category_map
WHERE confidence = '高' AND browse_node_id IS NOT NULL
  AND btrim(browse_node_id) <> ''
GROUP BY 1
"""

_IN_MAP_PATH_SQL = """
SELECT btrim(amazon_category), count(DISTINCT walmart_product_type),
       min(walmart_product_type)
FROM audit.walmart_category_map
WHERE confidence = '高'
GROUP BY 1
"""

_UPSERT_SQL = """
INSERT INTO audit.category_map_suggestions
    (amazon_category, suggested_pt, confidence, status,
     product_count, support_count, pt_distribution, browse_node_id)
VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
ON CONFLICT (amazon_category) DO UPDATE
SET suggested_pt = EXCLUDED.suggested_pt,
    confidence   = EXCLUDED.confidence,
    status       = EXCLUDED.status,
    product_count = EXCLUDED.product_count,
    support_count = EXCLUDED.support_count,
    pt_distribution = EXCLUDED.pt_distribution,
    browse_node_id = COALESCE(EXCLUDED.browse_node_id,
                              audit.category_map_suggestions.browse_node_id),
    created_at   = now()
"""

# 升级:node 键挖出来的行**带 browse_node_id 一起写**——正是映射表 ID
# 缺口的填补物;amazon_category 用代表路径(PK 需要)。
#
# **只升不降**(所有者定稿 2026-08-14:"如果满足条件,跑一轮数据库也可以升档"):
# ON CONFLICT 从 DO NOTHING 改成"新档位更高才覆盖"。为什么不自动降档:
#   · 证据可能是**暂时**变薄的(pt_source 回填没跑完、本轮只挖了子集),
#     据此把一条高置信行降成中,②级直出立刻对整个类目失效,而没人会发现;
#   · 高置信行可能是**人工**定的,机器不该拿一轮统计推翻人的判断。
# 降档必须人来做。升档是单调的,重跑幂等,坏不了事。
_RANK_CASE = ("CASE {} WHEN '高' THEN 2 WHEN '中' THEN 1 "
              "WHEN '低' THEN 0 ELSE -1 END")
_PROMOTE_NODE_SQL = f"""
INSERT INTO audit.walmart_category_map
    (amazon_category, walmart_product_type, browse_node_id, confidence,
     match_type)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (amazon_category, walmart_product_type) DO UPDATE
SET confidence = EXCLUDED.confidence,
    match_type = EXCLUDED.match_type,
    browse_node_id = COALESCE(EXCLUDED.browse_node_id,
                              audit.walmart_category_map.browse_node_id)
WHERE {_RANK_CASE.format('EXCLUDED.confidence')}
    > {_RANK_CASE.format('audit.walmart_category_map.confidence')}
"""

# 现有映射行的档位(算"新增还是升档"用;SQL 里的 WHERE 是并发下的第二道闸)
_EXISTING_MAP_SQL = """
SELECT btrim(amazon_category), walmart_product_type, confidence
FROM audit.walmart_category_map
"""


MIN_DOMINANCE = 0.8   # 首选 PT 的占比门槛(首跑实测:要求 100% 一致 → 1321 分流)

# 分桶 → 写进映射表的置信档(所有者定稿 2026-08-14)。
# 判据是**证据的性质**,不是"我们有多想要这条映射":
#   高 = 实证票多且压倒性一致        → ②级直出,不经 LLM
#   中 = 有真实 ASIN 撑着,但票不多   → 只进候选,交 LLM 判
#   低 = 有真实 ASIN 但票是分流的,首选 PT 更像推测 → 同上,排序更靠后
# 中/低两档的消费端**早就在跑**:audit_rules 的两个直出闸硬筛 confidence='高'
# (audit_rules.py:162/175),而候选召回几路不筛档位、只拿它排序
# (audit_l1_llm._CONF_ORDER)。所以写中/低 = 给 LLM 多一份参考,永远不会
# 让一条弱证据绕过 LLM 直接定 PT。
_TIER_BY_STATUS = {
    "mined_trusted": "高",
    "mined_review": "中",     # 2~4 票但优势度达标 = "有真实 ASIN,数量不多"
    "map_conflict": "中",     # 实证与旧映射相左:两条都留,让 LLM 挑
    "mined_mixed": "低",      # 票分流,首选 PT 是多数派而非共识
}
_TIER_RANK = {"低": 0, "中": 1, "高": 2}

# 血统标记(只写不读,给日后追溯"这条映射当初怎么来的")。
# mined_trusted 保持历史值 `mined_products` —— 存量行都是这个,改名会让
# "同一来源的行分成两批"而没有任何好处
_MATCH_TYPE_BY_STATUS = {
    "mined_trusted": "mined_products",
    "mined_review": "mined_review",
    "map_conflict": "mined_conflict",
    "mined_mixed": "mined_mixed",
}


def plan_promotions(rows: list[tuple], existing: dict) -> tuple[list, dict]:
    """输入:候选写入行 + 现有映射档位 → 输出:(真正要写的行, 新增/升档/跳过计数)。

    纯函数(便于测试)。rows 每项 = (amazon_category, pt, node, tier, status);
    existing = {(amazon_category, pt): confidence}。

    只升不降:目标档位 ≤ 现档位的一律跳过,连 match_type 都不动——
    重跑本工作流必须是幂等的,不能每跑一次就把人工维护过的行刷一遍。
    """
    out, stat = [], {"新增": 0, "升档": 0, "跳过(档位未提升)": 0}
    for cat, pt, node, tier, status in rows:
        cur = existing.get((cat, pt))
        if cur is None:
            stat["新增"] += 1
        elif _TIER_RANK.get(tier, -1) > _TIER_RANK.get(cur, -1):
            stat["升档"] += 1
        else:
            stat["跳过(档位未提升)"] += 1
            continue
        out.append((cat, pt, node, tier,
                    _MATCH_TYPE_BY_STATUS.get(status, "mined_products")))
    return out, stat


def classify_path(dist: dict, in_map_pt: str | None,
                  min_support: int = 5,
                  min_dominance: float = MIN_DOMINANCE,
                  in_map_ambiguous: bool = False
                  ) -> tuple[str, str, int] | None:
    """输入:{pt: 支持数} + 该键映射表现值 → 输出:(status, pt, support) 或 None。

    纯函数。**判据是优势度不是全票**(首跑实测修正 2026-08-14):回填的 PT
    来自删除历史+报错日报,同一类目下历史被挂过几个不同 PT 很正常(不同
    店铺挂法/早期挂错),要求 100% 一致会把 1,321 个类目全打成"分流";
    真实信号是"压倒性多数指向同一 PT",少数派是噪声。
      dominance = 首选 PT 支持数 / 该键总票数
      dominance ≥ min_dominance 且 支持数 ≥ min_support → mined_trusted
      dominance 达标但支持 2~(min_support-1)            → mined_review
      dominance 不达标(真分流)                          → mined_mixed(人工)
    支持数 1 → None(单证不立);已在映射表且共识与之一致 → None(无事可报)。
    """
    if not dist:
        return None
    total = sum(dist.values())
    top_pt, top_n = max(dist.items(), key=lambda kv: kv[1])
    dominance = top_n / total if total else 0.0
    if in_map_ambiguous:
        # 映射表**自己**就有两条互相矛盾的高置信行 —— ②级直出对这个 key
        # 已经失明(直出闸要求恰好一个高置信 PT)。机器不该往这堆里再加一条,
        # 得人来裁掉多余的那条,所以只报不写。
        return ("map_ambiguous", top_pt, top_n)
    if in_map_pt is not None:
        if (top_pt != in_map_pt and top_n >= min_support
                and dominance >= min_dominance):
            return ("map_conflict", top_pt, top_n)   # 实证压倒性却与旧映射相左
        return None
    if dominance < min_dominance:
        return ("mined_mixed", top_pt, top_n)
    if top_n >= min_support:
        return ("mined_trusted", top_pt, top_n)
    if top_n >= 2:
        return ("mined_review", top_pt, top_n)
    return None


def run(params: dict) -> str:
    """输入:params(key=node|path / min_support / promote=1)→ 输出:分桶摘要。"""
    min_support = int(params.get("min_support", 5))
    min_dominance = float(params.get("min_dominance", MIN_DOMINANCE))
    promote = str(params.get("promote", "")).strip() == "1"
    by_node = str(params.get("key", "node")).strip().lower() != "path"
    key_sql = "p.browse_node_id" if by_node else "btrim(p.amazon_category)"

    with db.pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_MINE_SQL.format(key=key_sql))
            votes: dict = {}
            for k, pt, n in cur.fetchall():
                votes.setdefault(k, {})[pt] = n
            rep_path: dict = {}
            # in_map[key] = 单一高置信 PT;ambiguous 装"已有多条高置信行"的 key
            ambiguous: set = set()
            if by_node:
                cur.execute(_IN_MAP_NODE_SQL)
                in_map = {}
                for k, n_pt, pt in cur.fetchall():
                    if not k:
                        continue
                    if n_pt > 1:
                        ambiguous.add(k)
                    else:
                        in_map[k] = pt
                cur.execute(_REP_PATH_SQL)
                rep_path = dict(cur.fetchall())
            else:
                cur.execute(_IN_MAP_PATH_SQL)
                in_map = {}
                for k, n_pt, pt in cur.fetchall():
                    (ambiguous.add(k) if n_pt > 1 else in_map.__setitem__(k, pt))
                # 别名路径视同已在映射表(catmap_align:中间层名漂移的假缺口),
                # 否则会被当新映射挖出来、还可能与 canonical 行冲突刷屏
                cur.execute("SELECT path, canonical_path "
                            "FROM audit.category_path_alias")
                for alias_path, canonical in cur.fetchall():
                    if canonical in ambiguous:
                        ambiguous.add(alias_path)
                    elif canonical in in_map:
                        in_map.setdefault(alias_path, in_map[canonical])
            existing_map: dict = {}
            cur.execute(_EXISTING_MAP_SQL)
            for cat, pt, conf in cur.fetchall():
                existing_map[(cat, pt)] = conf

        counts = {"mined_trusted": 0, "mined_review": 0,
                  "mined_mixed": 0, "map_conflict": 0, "map_ambiguous": 0}
        # 分流桶按优势度分档:让所有者拿数据挑阈值,而不是拍脑袋
        dom_bands = {"0.7~0.8": 0, "0.6~0.7": 0, "0.5~0.6": 0, "<0.5": 0}
        samples: dict = {"mined_trusted": [], "map_conflict": [],
                         "mined_mixed": []}
        rows, promote_rows = [], []
        for k, dist in votes.items():
            got = classify_path(dist, in_map.get(k), min_support,
                                min_dominance, k in ambiguous)
            if got is None:
                continue
            status, pt, support = got
            counts[status] += 1
            total_votes = sum(dist.values())
            dom = support / total_votes if total_votes else 0
            if status == "mined_mixed":
                band = ("0.7~0.8" if dom >= 0.7 else "0.6~0.7" if dom >= 0.6
                        else "0.5~0.6" if dom >= 0.5 else "<0.5")
                dom_bands[band] += 1
            if status in samples and len(samples[status]) < 5:
                extra = (f" ←旧映射 {in_map.get(k)}"
                         if status == "map_conflict" else "")
                samples[status].append(
                    f"  node {k}|{pt}(票 {support}/{total_votes},"
                    f"优势 {dom:.0%}){extra}")
            # 建议表主键是 amazon_category:node 键模式下用代表路径当展示键,
            # 没有代表路径的 node 用 'node:<id>' 兜底(不会与真路径撞)
            label = (rep_path.get(k) or f"node:{k}") if by_node else k
            tier = _TIER_BY_STATUS.get(status)      # map_ambiguous → None
            rows.append((label, pt, tier,
                         status, sum(dist.values()), support,
                         json.dumps(dist, ensure_ascii=False),
                         k if by_node else None))
            # 写映射表要真路径当主键;`node:<id>` 兜底标签只能进建议表
            if by_node and tier and rep_path.get(k):
                promote_rows.append((rep_path[k], pt, k, tier, status))
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, rows)

        kind = "类目 ID" if by_node else "类目路径"
        lines = [f"catmap_mine(键={kind},票数≥{min_support},优势≥{min_dominance:.0%}):"
                 f"{kind} {len(votes)} 个(有实证 PT 投票)→ "
                 f"可信 {counts['mined_trusted']} / 待核(少量支持)"
                 f" {counts['mined_review']} / 分流 {counts['mined_mixed']} / "
                 f"与旧映射冲突 {counts['map_conflict']}"
                 + (f" / ⚠ 映射表自相矛盾 {counts['map_ambiguous']}"
                    if counts["map_ambiguous"] else ""),
                 "复核 SQL:SELECT browse_node_id, suggested_pt, "
                 "support_count, product_count, amazon_category "
                 "FROM audit.category_map_suggestions "
                 "WHERE status='map_conflict' ORDER BY support_count DESC;"]
        if counts["mined_mixed"]:
            lines.append("分流桶按优势度分档(降门槛能救回多少):"
                         + " / ".join(f"{b} {n}" for b, n in dom_bands.items()))
        if counts["map_ambiguous"]:
            lines.append(
                f"⚠ 映射表里有 {counts['map_ambiguous']} 个类目挂着**多条**高置信"
                f"且 PT 不同的行 —— ②级直出对它们已经失明(直出闸要求恰好一个"
                f"高置信 PT)。机器不往这堆里再加,须人工裁掉多余的那条:"
                f"SELECT browse_node_id, array_agg(walmart_product_type) "
                f"FROM audit.walmart_category_map WHERE confidence='高' "
                f"AND browse_node_id IS NOT NULL GROUP BY 1 "
                f"HAVING count(DISTINCT walmart_product_type) > 1;")
        for tag, title in (("mined_trusted", "可信样例(将升级)"),
                           ("map_conflict", "⚠ 与旧映射冲突(只报不改)"),
                           ("mined_mixed", "分流样例(人工看)")):
            if samples.get(tag):
                lines.append(f"{title}:")
                lines += samples[tag]

        planned, plan_stat = plan_promotions(promote_rows, existing_map)
        by_tier: dict = {}
        for _cat, _pt, _node, tier, _mt in planned:
            by_tier[tier] = by_tier.get(tier, 0) + 1
        tier_txt = " / ".join(f"{t} {n}" for t, n in
                              sorted(by_tier.items(),
                                     key=lambda kv: -_TIER_RANK.get(kv[0], -1)))
        if not promote:
            if planned:
                lines.append(f"(将写映射表 {len(planned)} 条[{tier_txt}]:"
                             f"新增 {plan_stat['新增']} / 升档 {plan_stat['升档']};"
                             f"确认后 -p promote=1 真写)")
            return "\n".join(lines)
        if not by_node:
            return "\n".join(lines + [
                "⚠ key=path 模式不支持 promote(升级行必须带 browse_node_id"
                "——那才是映射表的 ID 缺口填补物);去掉 key=path 重跑"])
        with conn.cursor() as cur:
            cur.executemany(_PROMOTE_NODE_SQL, planned)
        lines.append(
            f"写映射表完成:{len(planned)} 条[{tier_txt}]"
            f"(新增 {plan_stat['新增']} / 升档 {plan_stat['升档']} / "
            f"跳过 {plan_stat['跳过(档位未提升)']})✓")
        lines.append(
            "  高 → ②级直出即刻生效;中/低 → 只进候选交 LLM 判"
            "(直出闸硬筛 confidence='高',弱证据永远绕不过 LLM)。"
            "**只升不降**:降档须人工,机器不拿一轮统计推翻人的判断。")
    return "\n".join(lines)
