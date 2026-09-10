"""LLM 域接口(DeepSeek;listing L2 属性映射用)。

api 层只做接口适配:认证(key 从环境变量,旧系统明文写 config.py 已废止)、
超时、重试、JSON 提取。业务提示词与缓存在 services 层
(services/llm_cache 按输入哈希缓存,别在这里重复实现)。

旧系统实证参数沿用:temperature=0.2、timeout=180s(连接 10s)、
映射用 max_tokens=4096;5xx/超时指数退避重试。
"""

import datetime
import json
import threading
import logging
import os
import time

import httpx

logger = logging.getLogger("api.llm")

_BASE_URL = "https://api.deepseek.com/chat/completions"
# 全仓统一 **deepseek-flash**(= V4.1 Flash 的正式 id),审核与上架同一个。
# 模型名仍可经 .env 逐用途覆盖(registry.LLM_PURPOSE_ENV)。
#
# 名字的**唯一判据是 `GET /models` 的返回**(所有者 2026-09-10 实测):
#   HTTP 200 {"data":[{"id":"deepseek-flash"},{"id":"deepseek-v4-pro"}]}
# 只有这两个可调 —— `deepseek-v4-flash` 已不在返回里(当天上午的定价页还列着
# 它、更新日志最新一条还是 8/21:**文档站会滞后于线上**,别拿文档当判据;
# 当天 15:21 复核时页面才追上)。`deepseek-flash` 版本号被去掉了,原生多模态,
# 所以 vision-exp 一并退役。
#
# 所有者定稿 2026-09-10 的原始要求是「模型切换为 V4.1 Flash」;当天正式 id 还
# 没公布时曾打算先借道 `deepseek-v4-pro`。**幸好拿到真名就直接用了真名** ——
# 定价页注(2)后来写明:要到**北京时间 2026-09-14 12:00 之后**,对 V4 Pro 的
# 请求才路由到 V4.1 Flash 并按 Flash 计费。在那之前借道 = 按**真 Pro 价**付
# (未命中 4.5 倍、输出 3.4 倍);而 V4.1 Pro 一上线官方撤路由,又会变回去。
# ⚠ **换模型 = 换 llm_cache 键空间**:v4-pro 故意不与 v4-flash 共用键空间
#   (背后是两个模型、两套答案),所以切换当轮存量缓存全量作废、全额重付。
#   大批重审排北京时间 18:00–次日 08:00 或周末(谷价)。
# ⚠ 缺省值**不许**填 `deepseek-chat` / `deepseek-reasoner`(官方已宣布停用的
#   旧别名,停用日 2026-07-24 已过,还能用纯属宽限期):一旦切断,L1 rerank /
#   L3 / 上架属性映射 / variant_remap **同时失败**。
#   生产实见:.env 里 DEEPSEEK_MODEL 没设,一直在吃这个缺省值 —— 所以这一行
#   就是生产的实际模型,改它才是真的切换。
_DEFAULT_MODEL = "deepseek-flash"


def _default_model() -> str:
    """输入:无 → 输出:缺省模型名(**call-time 求值**)。

    不能在模块级 `os.environ.get(...)` 取:那是 import 时的快照,而 cli.py
    的约定是「.env 先于一切业务 import 加载,registry 各函数 call-time 求值
    即可拿到」。快照写法只在"import 恰好晚于 load_dotenv"时碰巧正确,
    换个入口(测试、未来的网页/MCP 入口、任何提前 import 本模块的路径)
    就会静默吃缺省值 —— 而且看不出来,只有账单和摘要里的模型名会变。
    """
    return os.environ.get("DEEPSEEK_MODEL", "").strip() or _DEFAULT_MODEL


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
    return os.environ.get(env, "").strip() or _default_model()


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


#: 已经为哪些模型抱怨过"thinking 未登记"(每个模型只吵一次,别刷满日志)
_THINKING_WARNED: set = set()

#: 哪些模型实测**拒收** thinking 字段(400 且报文提到它)。本进程内不再下发。
#: `deepseek-flash` 认不认这个字段官方无文档、无法预先实测,这道降级就是为它
#: 准备的:摘掉字段重发一次,而不是让整条 LLM 链因为一个字段全挂。
_THINKING_REJECTED: set = set()


def _request_body(messages: list[dict], temperature: float,
                  max_tokens: int, purpose: str) -> dict:
    """输入:请求要素 → 输出:DeepSeek chat 请求体(纯函数,便于测试)。

    DeepSeek 家族官方**默认开 thinking**,旧仓铁律"必须永远显式下发 disable"
    (llm_routes.py:91-93/701)——本仓全链要的是非思考的 JSON 出参。
    ⚠ 2026-09-10 从 `"flash" in model` 子串匹配改成 **registry.LLM_THINKING
    登记表**:缺省模型切成 `deepseek-v4-pro` 的那一刻,子串门控整条失效
    (名字里没有 flash),而那正是它最该生效的时候。表里没有的模型不下发该
    字段(未知模型可能拒未知字段),但**点名警告一次** —— 静默跑在思考模式下
    = 多花输出 token 且出参形状可能变,不该看不见。
    开关无条件生效、不存在两种变体并存,故 llm_cache 键不含它。
    """
    from registry import resources
    model = model_for(purpose)
    body = {"model": model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"}}
    thinking = resources.llm_thinking(model)
    if thinking is not None and model not in _THINKING_REJECTED:
        body["thinking"] = thinking
    elif thinking is not None:
        pass                      # 实测被拒过,本进程内不再下发(已告警过)
    elif model not in _THINKING_WARNED:
        _THINKING_WARNED.add(model)
        logger.warning("模型 %s 未登记 thinking 开关(registry.LLM_THINKING),"
                       "本次不下发该字段 —— 若它默认开思考模式,输出 token 会"
                       "多花且出参形状可能变;请补一行登记", model)
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
    from registry import resources
    now = at or datetime.datetime.now(datetime.timezone.utc)
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
                record_usage(body.get("model", ""), purpose,
                             payload.get("usage"))
                content = payload["choices"][0]["message"]["content"]
                return _extract_json(content)
            if resp.status_code in (429, 500, 502, 503, 504):
                _bump_retry("http_429" if resp.status_code == 429
                            else "http_5xx")
                raise RuntimeError(f"LLM HTTP {resp.status_code}")
            # 降级(仅此一种,条件明确非 catch-all):**400 且报文提到 thinking**
            # ⇒ 这个模型不认该字段。摘掉重发一次并告警,本进程内不再下发。
            # 不这么做的话,一个官方还没写文档的字段能让全链 LLM 调用一起挂。
            if (resp.status_code == 400 and "thinking" in body
                    and "thinking" in resp.text.lower()
                    and body["model"] not in _THINKING_REJECTED):
                _THINKING_REJECTED.add(body["model"])
                body.pop("thinking")
                logger.warning(
                    "模型 %s 拒收 thinking 字段(HTTP 400),已摘掉重发;"
                    "本进程内不再下发 —— 请在 registry.LLM_THINKING 删掉它那一行,"
                    "并留意它是否默认开思考模式(输出 token 会多花)", body["model"])
                continue
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
