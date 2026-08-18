"""LLM 输入哈希缓存积木(catalog.llm_cache;listing L2c)。

旧系统 llm_cache.sqlite(462MB)PG 化:key = sha256(model+messages+
temperature+max_tokens)[:32](旧配方原样,换模型即全部失效——这是接受的
语义,旧数据因此不迁)。命中计数与 last_hit_at 便于后续清理低频行。
"""

import hashlib
import json
import logging

from api import llm as _llm_api

logger = logging.getLogger("services.llm_cache")


def cache_key(messages: list[dict], temperature: float, max_tokens: int,
              purpose: str = "default") -> str:
    """输入:LLM 请求要素(+用途)→ 输出:32 位哈希键(旧系统配方)。

    键里的 model 经 llm.model_for(purpose) 解析,与 chat_json 实际请求的
    模型**按构造同源**(批次 C 分用途选模型后,若键固定用默认模型,会造成
    "用途换了模型还命中旧缓存"的静默错);不同用途/模型天然分缓存键空间。
    """
    raw = json.dumps({"model": _llm_api.model_for(purpose),
                      "messages": messages,
                      "temperature": temperature, "max_tokens": max_tokens},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def get(conn, key: str) -> dict | None:
    """输入:连接 + 键 → 输出:缓存的 JSON dict 或 None(命中顺带计数)。"""
    with conn.cursor() as cur:
        cur.execute("UPDATE catalog.llm_cache SET hit_count = hit_count + 1, "
                    "last_hit_at = now() WHERE input_hash = %s "
                    "RETURNING response", (key,))
        row = cur.fetchone()
    if not row:
        return None
    v = row[0]
    return v if isinstance(v, dict) else json.loads(v)


def put(conn, key: str, response: dict, purpose: str = "default", *,
        asin: str | None = None, pt: str | None = None,
        src_title: str | None = None, reuse_sig: str | None = None) -> None:
    """输入:连接 + 键 + 模型回复 JSON(+用途/二级复用元数据)→ 输出:无(幂等)。

    四个元数据列只有 list_new 出参路径传(find_reusable 的反查依据);
    audit_l3 等其它用途不传、留 NULL——它们的复用语义不同,不参与二级。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO catalog.llm_cache (input_hash, model, response, "
            "asin, pt, src_title, reuse_sig) "
            "VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s) "
            "ON CONFLICT (input_hash) DO NOTHING",
            (key, _llm_api.model_for(purpose),
             json.dumps(response, ensure_ascii=False),
             asin, pt, src_title, reuse_sig))


def find_reusable(conn, asin: str, pt: str, sig: str
                  ) -> tuple[dict, str] | None:
    """输入:连接 + ASIN + PT + 硬条件签名 → 输出:(旧出参, 出参时标题) 或 None。

    二级反查(2026-08-18 所有者定稿):一级 input_hash miss 时,取该
    (asin, pt) 最近一次出参。reuse_sig 必须相等——签名把"不许复用"的
    硬条件(spec 字段面/brand/category/变体属性)压成一个等值判断,
    这里只管取;标题规格验证归调用方(mp_mapper.title_spec_compatible)。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT response, src_title FROM catalog.llm_cache "
            "WHERE asin = %s AND pt = %s AND reuse_sig = %s "
            "ORDER BY created_at DESC LIMIT 1", (asin, pt, sig))
        row = cur.fetchone()
    if not row:
        return None
    v = row[0]
    return (v if isinstance(v, dict) else json.loads(v)), (row[1] or "")
