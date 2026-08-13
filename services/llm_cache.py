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


def put(conn, key: str, response: dict,
        purpose: str = "default") -> None:
    """输入:连接 + 键 + 模型回复 JSON(+用途)→ 输出:无(幂等)。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO catalog.llm_cache (input_hash, model, response) "
            "VALUES (%s, %s, %s::jsonb) ON CONFLICT (input_hash) DO NOTHING",
            (key, _llm_api.model_for(purpose),
             json.dumps(response, ensure_ascii=False)))
