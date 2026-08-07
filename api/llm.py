"""LLM 域接口(DeepSeek;listing L2 属性映射用)。

api 层只做接口适配:认证(key 从环境变量,旧系统明文写 config.py 已废止)、
超时、重试、JSON 提取。业务提示词与缓存在 services 层
(services/llm_cache 按输入哈希缓存,别在这里重复实现)。

旧系统实证参数沿用:temperature=0.2、timeout=180s(连接 10s)、
映射用 max_tokens=4096;5xx/超时指数退避重试。
"""

import json
import logging
import os
import time

import httpx

logger = logging.getLogger("api.llm")

_BASE_URL = "https://api.deepseek.com/chat/completions"
_MODEL = "deepseek-chat"


def _api_key() -> str:
    v = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not v:
        raise LookupError("DEEPSEEK_API_KEY 未配置:写入 <DATA_ROOT>/.env")
    return v


def _extract_json(text: str) -> dict:
    """输入:模型回复文本 → 输出:其中的 JSON 对象(容忍代码围栏/前后缀)。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.startswith("json"):
            s = s[4:]
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"LLM 回复中未找到 JSON 对象:{s[:200]!r}")
    return json.loads(s[start:end + 1])


def chat_json(messages: list[dict], *, temperature: float = 0.2,
              max_tokens: int = 4096, max_retries: int = 3) -> dict:
    """输入:messages → 输出:模型回复中解析出的 JSON dict。

    读操作可安全重试:超时/5xx/429 指数退避(1/2/4s);4xx 直接抛。
    """
    body = {"model": _MODEL, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {_api_key()}"}
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = httpx.post(_BASE_URL, json=body, headers=headers,
                              timeout=httpx.Timeout(180, connect=10))
            if resp.status_code == 200:
                content = (resp.json()["choices"][0]["message"]["content"])
                return _extract_json(content)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"LLM HTTP {resp.status_code}")
            raise ValueError(f"LLM 请求被拒 HTTP {resp.status_code}: "
                             f"{resp.text[:200]}")
        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError,
                KeyError) as e:
            last = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning("LLM 调用失败(%s),%ds 后重试", e, wait)
                time.sleep(wait)
    raise RuntimeError(f"LLM 调用连续 {max_retries} 次失败:{last}")
