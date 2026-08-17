"""变体维度错位重映射(旧仓 Phase 0.8 的等价物;硬编码前置 + LLM 兜底)。

补迁于 2026-08-17,所有者批「都补」。旧仓出处:`auto_listing/mapper.py:1467-1629`
(`needs_variant_remap` / `try_hardcoded_remap_variant_attrs` /
`remap_variant_attrs_via_llm`)。

**要解决的是哪一类**:亚马逊给的维度名不在这个 PT 的 `variantAttributeNames`
枚举里,但它表达的东西枚举里其实有 —— 只是名字对不上。旧仓的原始案例:
文具类目的 `color_name=48 Color` 说的**不是颜色是件数**,该映到 `pieceCount`。
这一类靠 `variant_group._DIM_MAP` 的字面映射永远映不上(名字压根不同族)。

三层,越贵越靠后(与旧仓同序):
  ① `needs_remap`   有维度不在枚举内才往下走,否则一分钱不花
  ② `hardcoded`     (PT, 亚马逊维度名) 命中内置表且值能解析成数字 → 直接用
  ③ `llm_remap`     整组一次决策,过 llm_cache;输出严格校验,不合格整份丢弃

## 与旧仓的三处**有意**差异

1. **只重映"映不上的那几个",不推翻已经映上的。** 旧仓 remap 一旦触发,整组
   改用**一个** key(提示词明写"所有兄弟共用同一个 key"),于是一个 color 映得上
   而 size 映不上的组会连 color 一起丢掉、退成单维。我们保留已映上的,只给
   映不上的找归宿 —— 严格更强,且不与"多维"打架。
2. **本轮取值全同的维度不送 LLM。** 生产实见(所有者的礼品袋组):三个成员的
   `size_name` 全是 `1 Count (Pack of 100)`,那是包装数量、对分组零信息量。
   送去问只会得到一个"看着对"的 key,然后三个成员带着同一个值发出去 ——
   声明了没有差异的维度等于没声明。这一层过滤在调用方(list_new)做,
   本模块只接"值确有差异"的组。
3. **缓存键按 (PT, 维度名) 而非按样本值。** 旧仓把样本值写进提示词,于是不同
   批次的取值不同 ⇒ 缓存不命中 ⇒ 同一个 (PT, 维度) 可能被判成两个不同的
   walmart_key。而**同一个变体组的 variantAttributeNames 必须跨兄弟一致**
   (旧仓设计文档自己强调的),分批上架时前后判不一样就把组弄坏了。
   我们仍把样本值写进提示词供语义推断,但**缓存键只含 (PT, 维度名, 枚举)**
   —— 第一次判定即定案,后续批次复用。代价:早期样本不典型会把这个
   (PT, 维度) 定错;好处是绝不会出现"同组前后两套维度名"。
"""

import json
import logging
import re

from api import llm
from services import llm_cache

logger = logging.getLogger("services.variant_remap")

# (PT, 亚马逊维度名) → 候选沃尔玛属性名(按优先级)。逐字迁自旧仓
# `_HARDCODED_VARIANT_KEY_REMAP`(mapper.py:1530-1538)。
# 语义:文具类目的 color_name 通常是"套装色数",实际就是件数。
_HARDCODED: dict[tuple[str, str], tuple[str, ...]] = {
    ("Art Sets", "color_name"): ("pieceCount", "count", "multipackQuantity"),
    ("Markers", "color_name"): ("pieceCount", "count", "multipackQuantity"),
    ("Pencils", "color_name"): ("pieceCount", "count", "multipackQuantity"),
    ("Crayons", "color_name"): ("pieceCount", "count", "multipackQuantity"),
    ("Colored Pencils", "color_name"): ("pieceCount", "count",
                                        "multipackQuantity"),
    # 后续按 variant_probe 在生产上看到的错位场景持续加
}

# "48 Color" / "12 Colors" / "24 Pack" / "100 Pcs" / "8" 都能解析(旧仓同款)
_NUMERIC_RE = re.compile(
    r"^\s*(\d+)\s*(?:Color|Colors|Pack|Packs|Pcs?|Pieces?|Count|Cnt|Ct|"
    r"set|sets|x)?\s*$", re.IGNORECASE)

_PURPOSE = "variant_remap"
_TEMPERATURE = 0.1
_MAX_TOKENS = 1024


def needs_remap(dims, enum) -> bool:
    """输入:亚马逊维度名集合 + PT 的变体维度枚举 → 输出:要不要往下走。

    枚举为空 = 这个 PT 压根不支持变体分组,重映射也没归宿 → False
    (旧仓同款:不浪费一次调用)。
    """
    allowed = {str(e) for e in (enum or ())}
    if not allowed:
        return False
    return any(str(d) not in allowed for d in (dims or ()))


def hardcoded(pt: str, dim: str, values: dict, enum) -> tuple | None:
    """输入:PT + 亚马逊维度名 + {asin: 原值} + 枚举 → 输出:(属性名, {asin: 值}) 或 None。

    四条件全满足才用(旧仓 `try_hardcoded_remap_variant_attrs` 同款):
    表里有这条、候选属性名至少一个在枚举里、**每个**成员的值都能解析成数字。
    任一条不满足返回 None,交给 LLM —— 半套映射比不映射更坏。
    """
    cands = _HARDCODED.get((str(pt), str(dim)))
    if not cands:
        return None
    allowed = {str(e) for e in (enum or ())}
    key = next((c for c in cands if c in allowed), None)
    if not key:
        return None
    out = {}
    for asin, raw in (values or {}).items():
        m = _NUMERIC_RE.match(str(raw).strip())
        if not m:
            return None
        out[asin] = int(m.group(1))
    return (key, out) if out else None


def _prompt(pt: str, dim: str, values: dict, enum: list) -> list[dict]:
    samples = "\n".join(f"  {a}: {v!r}" for a, v in sorted(values.items()))
    return [{"role": "user", "content": f"""你是 Walmart Marketplace 上架变体维度判定专家。

Product Type: {pt}
该 PT 允许的变体维度(**必须从这个列表里选一个**):
{', '.join(sorted(str(e) for e in enum))}

亚马逊给的维度名是 `{dim}`,它**不在**上面的列表里。同一变体组内各成员在这个
维度上的取值:
{samples}

任务:
1. 判断 `{dim}` 在本商品语境下实际表达什么(颜色/件数/套装大小/尺寸/年龄段/…);
2. 从上面的允许列表里选出语义最贴合的一个 key(**所有成员共用这一个 key**);
3. 语义对不上任何一个就返回 null —— 硬凑一个比不映射更坏。

只返回 JSON,不要解释:
{{"walmart_key": "pieceCount", "rationale": "简短说明"}}
或 {{"walmart_key": null, "rationale": "..."}}

注意:
- walmart_key 必须严格属于上面的允许列表,否则你的输出会被丢弃;
- 只判**用哪个 key**,不要输出各成员的值(值由程序按 spec 的类型自己转)。"""}]


def llm_remap(conn, pt: str, dim: str, values: dict, enum) -> tuple | None:
    """输入:连接 + PT + 维度名 + {asin: 原值} + 枚举 → 输出:(属性名, {asin: 值}) 或 None。

    整组**一次**决策(不是逐条),过 `llm_cache`。⚠ 缓存键只含 (PT, 维度名, 枚举),
    **不含样本值** —— 见模块头注差异 3:同一个变体组的 variantAttributeNames
    必须跨兄弟/跨批次一致,按样本值缓存会让分批上架前后判出两套维度名。

    校验不过一律返回 None(降级 = 这个维度不发,不是整组退单品):
    key 不在枚举内、key 已被其它维度占用由调用方判、LLM 报错或返回空。
    """
    allowed = sorted(str(e) for e in (enum or ()))
    if not allowed:
        return None
    messages = _prompt(pt, dim, values, allowed)
    # ⚠ 缓存键刻意与提示词**脱钩**:提示词里有样本值(供语义推断),键里没有。
    # 用一份"骨架消息"算键 —— 同 (PT, 维度, 枚举) 第一次判定即定案
    skeleton = [{"role": "user",
                 "content": json.dumps({"variant_remap": 1, "pt": pt,
                                        "dim": dim, "enum": allowed},
                                       ensure_ascii=False, sort_keys=True)}]
    key = llm_cache.cache_key(skeleton, _TEMPERATURE, _MAX_TOKENS, _PURPOSE)
    raw = llm_cache.get(conn, key)
    if raw is None:
        try:
            raw = llm.chat_json(messages, temperature=_TEMPERATURE,
                                max_tokens=_MAX_TOKENS, purpose=_PURPOSE)
        except Exception as e:                                  # noqa: BLE001
            logger.warning("变体维度重映射 LLM 失败(该维度不发):%s/%s %s",
                           pt, dim, e)
            return None
        llm_cache.put(conn, key, raw, purpose=_PURPOSE)
    wm = (raw or {}).get("walmart_key")
    if not wm or str(wm) not in set(allowed):
        logger.info("变体维度重映射:%s 的 %s 未映上(LLM 给 %r,理由 %r)",
                    pt, dim, wm, (raw or {}).get("rationale"))
        return None
    logger.warning("变体维度重映射:%s 的 %s → %s(LLM,理由 %r)",
                   pt, dim, wm, (raw or {}).get("rationale"))
    return str(wm), dict(values)


def remap_dim(conn, pt: str, dim: str, values: dict, enum) -> tuple | None:
    """输入:同上 → 输出:(沃尔玛属性名, {asin: 值}) 或 None。三层的唯一入口。

    ① 硬编码表(零成本)→ ② LLM 兜底。`conn` 为 None 时只走硬编码
    (纯函数场景/测试),不静默去连库。
    """
    hit = hardcoded(pt, dim, values, enum)
    if hit:
        logger.info("变体维度重映射:%s 的 %s → %s(内置表)", pt, dim, hit[0])
        return hit
    if conn is None:
        return None
    return llm_remap(conn, pt, dim, values, enum)
