"""视觉 LLM 域接口(火山方舟 ARK / 豆包;审核 L4 视觉专用)。

api 层只做接口适配:认证(key 从环境变量)、超时、重试、消息装配、JSON 提取、
图片下载转 base64。**零业务判断**——什么算命中、verdict 怎么算、要不要开
aggressive,全在 services/audit_l4.py。

env 三件(合同 L4-1;视觉不进 registry.LLM_PURPOSE_ENV,那张表是 DeepSeek
文本用途路由,见计划 A4):
  ARK_API_KEY       必填,无默认(fail loud,绝不静默走匿名请求)
  ARK_BASE_URL      默认 https://ark.cn-beijing.volces.com/api/v3(旧仓 settings.py:192)
  ARK_VISION_MODEL  默认 doubao-seed-1-6-flash-250615(旧仓 settings.py:197)

旧仓实证参数沿用:视觉请求超时 120s / 连接 10s(llm_router.py:556、564-566;
**不是** api/llm.py 那 180s,那是文本长思考的值)、图片本地下载并发 30 /
超时 8s(l4_vision.py:41-42)。图片以 base64 data URL 内联发送,不发原始 URL
——绕开火山服务端拉 Amazon 图超时的问题(l4_vision.py:51-53 原始理由)。

勿迁(spec §9):llm_router / llm_routes 多供应商路由全套、豆包→qwen-vl 跨供应商
failover、DashScope 相关、usage_logger 记账、llm_alerts 告警。

public:
  base_url, vision_model, fetch_images, vision_json
"""

import base64
import concurrent.futures
import logging
import os
import time

import httpx

from api.llm import _extract_json   # api 层内互相 import 允许(合同 L4-10)

logger = logging.getLogger("api.llm_vision")

_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
_DEFAULT_VISION_MODEL = "doubao-seed-1-6-flash-250615"

# 本地下图并发 / 超时(旧仓 l4_vision.py:41-42 `_IMG_DL_SEM = Semaphore(30)`、
# `_IMG_DL_TIMEOUT = 8.0`;旧仓是 asyncio,新仓同步链用线程池等价实现)
IMG_DL_CONCURRENCY = 30
IMG_DL_TIMEOUT = 8.0
# Amazon 图对默认 UA 有拒绝行为(旧仓 l4_vision.py:61)
_IMG_UA = "Mozilla/5.0"

# 视觉请求超时(旧仓 _TIMEOUT_VISION=120,连接 10s)
_TIMEOUT = httpx.Timeout(120, connect=10)


def base_url() -> str:
    """输入:无 → 输出:ARK OpenAI 兼容 base URL(env ARK_BASE_URL 覆盖)。"""
    return (os.environ.get("ARK_BASE_URL", "").strip() or _DEFAULT_BASE_URL)


def vision_model() -> str:
    """输入:无 → 输出:视觉模型名(env ARK_VISION_MODEL 覆盖)。"""
    return (os.environ.get("ARK_VISION_MODEL", "").strip()
            or _DEFAULT_VISION_MODEL)


def _api_key() -> str:
    """输入:无 → 输出:ARK API key(未配置直接抛,绝不静默降级)。"""
    v = os.environ.get("ARK_API_KEY", "").strip()
    if not v:
        raise LookupError("ARK_API_KEY 未配置:写入 <DATA_ROOT>/.env")
    return v


def _endpoint() -> str:
    """输入:无 → 输出:chat/completions 完整 URL(base 尾部 / 归一)。"""
    return base_url().rstrip("/") + "/chat/completions"


def _download_one(url: str) -> str | None:
    """输入:图片 URL → 输出:data URL(base64 内联),失败返回 None。

    逐字迁自旧仓 l4_vision.py:50-78:非 200 或空 body 即失败(**不重试**);
    mime 三级推断 = 响应头 content-type(取 ';' 前、strip、lower)→ 不以
    'image/' 开头时按 URL 后缀 .png/.webp → 兜底 image/jpeg。
    旧仓 `trust_env=False`(生产 Mac 无代理),新仓用 httpx 默认(合同 L4-5)。
    """
    try:
        with httpx.Client(timeout=IMG_DL_TIMEOUT, follow_redirects=True) as c:
            r = c.get(url, headers={"User-Agent": _IMG_UA})
            if r.status_code != 200 or not r.content:
                return None
            ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not ct.startswith("image/"):
                if url.lower().endswith(".png"):
                    ct = "image/png"
                elif url.lower().endswith(".webp"):
                    ct = "image/webp"
                else:
                    ct = "image/jpeg"
            b64 = base64.b64encode(r.content).decode("ascii")
            return f"data:{ct};base64,{b64}"
    except Exception as e:      # noqa: BLE001 —— 单张图失败不该拖垮整批
        logger.debug("图下载失败 %s: %s", url, e)
        return None


def fetch_images(urls: list[str]) -> list[tuple[str, str]]:
    """输入:图片 URL 列表 → 输出:[(原始 URL, data URL)],只保留下载成功的、保序。

    并发 30、单张超时 8s(旧仓实证值)。返回**带原始 URL** 是为了让 services
    层能给每条 issue 回填 image_url(合同 L4-3:修旧仓"下载失败静默丢弃导致
    image_index 整体前移指向错图"的错位)。
    体积不设上限(合同 L4-6,保真),只把总字节记进日志供实测后再议。
    """
    if not urls:
        return []
    workers = min(len(urls), IMG_DL_CONCURRENCY)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_download_one, urls))
    out = [(u, d) for u, d in zip(urls, results) if d]
    total_bytes = sum(len(d) for _, d in out)
    logger.info("视觉取图:%d/%d 张成功,base64 合计 %d 字节",
                len(out), len(urls), total_bytes)
    return out


def vision_json(images_b64: list[str], *, system_prompt: str, user_prompt: str,
                temperature: float = 0.0, max_tokens: int = 1024,
                max_retries: int = 3) -> dict:
    """输入:图片 data URL 列表 + 两段提示词 → 输出:模型回复里解析出的 JSON dict。

    消息装配同旧仓 llm_router.py:986-998:system 独立一条;user 是 content 数组,
    **text 在前、图在后**——这个顺序就是模型 image_index 的语义基准,不得改。
    单链无自动降级(计划 10.2):失败同链重试(超时/429/5xx 指数退避 1/2/4s),
    4xx 直抛 ValueError,重试尽抛 RuntimeError,由调用方决定后果。
    坏 JSON 由 _extract_json 抛 ValueError 且**不重试**——温度 0.0 重发同一
    请求得同一回复,重试只是白付一次钱(调用方按失败路径处理)。
    """
    content: list[dict] = [{"type": "text", "text": user_prompt}]
    for u in images_b64:
        content.append({"type": "image_url", "image_url": {"url": u}})
    body = {"model": vision_model(),
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": content}],
            "temperature": temperature, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {_api_key()}"}
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = httpx.post(_endpoint(), json=body, headers=headers,
                              timeout=_TIMEOUT)
            if resp.status_code == 200:
                payload = resp.json()
                # token 记账与文本链共用一个累加器(2026-08-21):L4 走的是另一个
                # 供应商,单价不在 registry.LLM_PRICING 里 ⇒ 摘要只报 token 并
                # 点名"该模型无计价",不会编一个金额出来
                from api.llm import record_usage
                record_usage(body.get("model", ""), "audit_l4",
                             payload.get("usage"))
                return _extract_json(
                    payload["choices"][0]["message"]["content"])
            if resp.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"视觉 LLM HTTP {resp.status_code}")
            raise ValueError(f"视觉 LLM 请求被拒 HTTP {resp.status_code}: "
                             f"{resp.text[:200]}")
        except (httpx.HTTPError, RuntimeError, KeyError) as e:
            last = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning("视觉 LLM 调用失败(%s),%ds 后重试", e, wait)
                time.sleep(wait)
    raise RuntimeError(f"视觉 LLM 调用连续 {max_retries} 次失败:{last}")
