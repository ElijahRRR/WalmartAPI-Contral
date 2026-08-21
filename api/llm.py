"""LLM 域接口(DeepSeek;listing L2 属性映射用)。

api 层只做接口适配:认证(key 从环境变量,旧系统明文写 config.py 已废止)、
超时、重试、JSON 提取。业务提示词与缓存在 services 层
(services/llm_cache 按输入哈希缓存,别在这里重复实现)。

旧系统实证参数沿用:temperature=0.2、timeout=180s(连接 10s)、
映射用 max_tokens=4096;5xx/超时指数退避重试。
"""

import json
import threading
import logging
import os
import time

import httpx

logger = logging.getLogger("api.llm")

_BASE_URL = "https://api.deepseek.com/chat/completions"
# 旧生产用 deepseek-v4-flash(thinking disabled);模型名可经 .env 切换,
# 换模型即换 llm_cache 键空间(缓存键含 model,自动失效无需清理)
_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def model_for(purpose: str) -> str:
    """输入:用途名(registry.LLM_PURPOSE_ENV 的键)→ 输出:该用途的模型名。

    批复 #1(2026-08-13):DeepSeek 分用途选模型,env 逐用途覆盖
    (如 DEEPSEEK_MODEL_AUDIT_L1),未配置回落 DEEPSEEK_MODEL 默认。
    未登记的用途直接抛错(fail loud:拼错用途名静默吃默认模型,成本/
    效果偏差没人会发现)。
    """
    from registry import resources
    env = resources.LLM_PURPOSE_ENV.get(purpose)
    if env is None:
        raise ValueError(f"未登记的 LLM 用途 {purpose!r}:先在 "
                         f"registry.LLM_PURPOSE_ENV 登记再使用")
    return os.environ.get(env, "").strip() or _MODEL


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


def _request_body(messages: list[dict], temperature: float,
                  max_tokens: int, purpose: str) -> dict:
    """输入:请求要素 → 输出:DeepSeek chat 请求体(纯函数,便于测试)。

    v4-flash 家族官方**默认开 thinking**,旧仓铁律"必须永远显式下发
    disable"(llm_routes.py:91-93/701;所有者确认生产 DEEPSEEK_MODEL=
    deepseek-v4-flash,2026-08-13)——按模型名门控,非 flash 家族不发
    该字段(未知字段可能被拒)。开关无条件生效、不存在两种变体并存,
    故 llm_cache 键不含它(既有缓存零失效)。
    """
    model = model_for(purpose)
    body = {"model": model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"}}
    if "flash" in model:
        body["thinking"] = {"type": "disabled"}
    return body


# 退避观测(线程安全):撞限流时只表现为"变慢",不计数就只能靠耗时反推。
# 纯计数不含业务判断——调用方(product_audit 摘要)读它决定要不要降并发。
_RETRY_LOCK = threading.Lock()
RETRY_STATS: dict = {"http_429": 0, "http_5xx": 0, "other": 0}


def reset_retry_stats() -> None:
    with _RETRY_LOCK:
        for k in RETRY_STATS:
            RETRY_STATS[k] = 0


def _bump_retry(key: str) -> None:
    with _RETRY_LOCK:
        RETRY_STATS[key] = RETRY_STATS.get(key, 0) + 1


# ── token 用量记账(2026-08-21)────────────────────────────────────────────
# 此前 chat_json 只取 choices[0].message.content,DeepSeek 同一个 JSON 里回的
# `usage` **整块丢掉** —— 于是"这一轮花了多少钱"全仓答不出来(旧仓的
# usage_logger 迁移时明确不迁,见 audit_l3 头注)。跑一次十几万条的重审之后
# 再想知道花了多少,数据已经没了。
#
# 记的是 **token 不是钱**:token 是接口回的事实(api 层的活),单价是会变的
# 业务参数(registry 存表、services 折算),换模型/换供应商只动后者。
# 形态照抄同文件的 RETRY_STATS:线程安全累加 + 每轮 reset + 摘要渲染。
USAGE_STATS: dict[tuple, dict] = {}
_USAGE_LOCK = threading.Lock()


def reset_usage_stats() -> None:
    with _USAGE_LOCK:
        USAGE_STATS.clear()


def record_usage(model: str, purpose: str, usage: dict | None,
                 at: "datetime.datetime | None" = None) -> None:
    """输入:模型 + 用途 + 响应里的 usage 块 → 输出:无(按 (模型,用途,时段) 累加)。

    **时段在调用当时就定死**(不是渲染时):DeepSeek 峰谷价差整整一倍,
    一轮跑几小时会跨越峰谷分界,事后按"现在是什么时段"统一折算必然算错。
    `usage` 缺失(供应商不回)只累加 calls,其余留 0 —— 少算不瞎算。
    """
    import datetime as _dt
    from registry import resources
    now = at or _dt.datetime.now(_dt.timezone.utc)
    tier = resources.llm_price_tier(now)
    u = usage or {}
    key = (model, purpose, tier)
    with _USAGE_LOCK:
        row = USAGE_STATS.setdefault(
            key, {"calls": 0, "prompt": 0, "completion": 0,
                  "cache_hit": 0, "cache_miss": 0})
        row["calls"] += 1
        row["prompt"] += int(u.get("prompt_tokens") or 0)
        row["completion"] += int(u.get("completion_tokens") or 0)
        # DeepSeek 专有:命中前缀缓存的输入 token 便宜一个数量级,不分开记
        # 就等于把 L3 那条"system prompt 逐字节相同"的缓存契约的收益抹平了
        row["cache_hit"] += int(u.get("prompt_cache_hit_tokens") or 0)
        row["cache_miss"] += int(u.get("prompt_cache_miss_tokens") or 0)


def chat_json(messages: list[dict], *, temperature: float = 0.2,
              max_tokens: int = 4096, max_retries: int = 3,
              purpose: str = "default") -> dict:
    """输入:messages(+用途)→ 输出:模型回复中解析出的 JSON dict。

    purpose 按 registry.LLM_PURPOSE_ENV 选模型(10.2 定稿);单链无自动
    降级——失败同链重试,重试尽抛异常,由调用方决定 pending,绝不默认放行。
    读操作可安全重试:超时/5xx/429 指数退避(1/2/4s);4xx 直接抛。
    """
    body = _request_body(messages, temperature, max_tokens, purpose)
    headers = {"Authorization": f"Bearer {_api_key()}"}
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = httpx.post(_BASE_URL, json=body, headers=headers,
                              timeout=httpx.Timeout(180, connect=10))
            if resp.status_code == 200:
                payload = resp.json()
                record_usage(body.get("model", _MODEL), purpose,
                             payload.get("usage"))
                content = payload["choices"][0]["message"]["content"]
                return _extract_json(content)
            if resp.status_code in (429, 500, 502, 503, 504):
                _bump_retry("http_429" if resp.status_code == 429
                            else "http_5xx")
                raise RuntimeError(f"LLM HTTP {resp.status_code}")
            raise ValueError(f"LLM 请求被拒 HTTP {resp.status_code}: "
                             f"{resp.text[:200]}")
        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError,
                KeyError) as e:
            last = e
            if not isinstance(e, RuntimeError):   # 网络/解析类,非状态码类
                _bump_retry("other")
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning("LLM 调用失败(%s),%ds 后重试", e, wait)
                time.sleep(wait)
    raise RuntimeError(f"LLM 调用连续 {max_retries} 次失败:{last}")
