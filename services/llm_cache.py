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

# 与 api/llm 实际请求的模型保持同源(DEEPSEEK_MODEL 可经 .env 切换):
# 键里的 model 与请求的 model 不一致会造成"换了模型还命中旧缓存"的静默错
_MODEL = _llm_api._MODEL


def cache_key(messages: list[dict], temperature: float, max_tokens: int) -> str:
    """输入:LLM 请求要素 → 输出:32 位哈希键(旧系统配方)。"""
    raw = json.dumps({"model": _MODEL, "messages": messages,
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


def put(conn, key: str, response: dict) -> None:
    """输入:连接 + 键 + 模型回复 JSON → 输出:无(幂等)。"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO catalog.llm_cache (input_hash, model, response) "
            "VALUES (%s, %s, %s::jsonb) ON CONFLICT (input_hash) DO NOTHING",
            (key, _MODEL, json.dumps(response, ensure_ascii=False)))
