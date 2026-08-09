"""亚马逊采集服务接口(amazon-scraper-v4;增量导出契约 v1)。

契约权威:docs/scraper_migration_brief.md 第五节 + §5.1 行为补遗
(采集侧副本 docs/incremental_export_contract.md,改动需两侧同步)。

api 层只做接口适配:base_url/token 从 registry 取、超时、按状态码分流、退避。
"够不够格进 products"这类判断在 services/product_ingest,不在这里。

  export_incremental(cursor, limit) -> (records, next_cursor, has_more)

状态码分流(契约 §5.1,每一条都有来历,别简化):
  200 → 正常(**空结果也是 200**:records=[] + next_cursor 原样不推进)
  401 → ExportAuthError,修 token,不重试
  409 → RetentionGapError,**要的数据已被保留期裁掉**:告警 + 停 + 全量对账
  422 → ValueError,修请求,不重试
  404 且响应体含「批次不存在」→ 路由打歪/退化(采集侧 catch-all 前缀坑),
        按 5xx 处理告警,**绝不推进游标**——404 最容易被读成"暂无数据",
        那会让同步静默停摆且两侧都不报错
  429/5xx/超时 → 指数退避重试
"""

import logging
import os
import time

import httpx

logger = logging.getLogger("api.scraper")

CONTRACT_VERSION = 1
LIMIT_MAX = 1000            # 契约上限,超过采集侧返 422


class ExportAuthError(RuntimeError):
    """X-Export-Token 不匹配/缺失(401)——修 token,重试无用。"""


class RetentionGapError(RuntimeError):
    """游标掉出保留窗口(409)——必须告警 + 停止推进 + 转全量对账。"""


def base_url() -> str:
    """输入:无 → 输出:采集服务 base(env SCRAPER_BASE_URL,末尾斜杠已剥)。"""
    v = os.environ.get("SCRAPER_BASE_URL", "").strip().rstrip("/")
    if not v:
        raise LookupError(
            "SCRAPER_BASE_URL 未配置:写入 <DATA_ROOT>/.env"
            "(本地接线 http://127.0.0.1:8899;上服务器后改此一行即可切换)")
    return v


def _headers() -> dict:
    token = os.environ.get("SCRAPER_EXPORT_TOKEN", "").strip()
    if not token:
        # 契约:鉴权可选,采集侧没配 EXPORT_TOKEN 时放行。公网部署必须配。
        logger.warning("SCRAPER_EXPORT_TOKEN 未配置,导出请求不带鉴权头")
        return {}
    return {"X-Export-Token": token}


class BatchExistsError(RuntimeError):
    """批次名已存在(409)。**不是失败**——上一次其实推成功了。

    v4 实证语义:撞名绝不静默合并进既有批次,409 响应体带既有 batch_id,
    调用方直接拿去轮询即可。因此 POST /api/upload **可以安全重试**:
    网络超时后重发,若上次成功则拿到 409 + 那个 batch_id,不会重复建批。
    """

    def __init__(self, batch_id, batch_name: str):
        super().__init__(f"批次已存在:{batch_name}(batch_id={batch_id})")
        self.batch_id = batch_id
        self.batch_name = batch_name


def submit_batch(batch_name: str, asins: list[str], *, zip_code: str = "",
                 max_retries: int = 3) -> dict:
    """输入:批次名 + ASIN 列表(+邮编)→ 输出:{batch_id, inserted, ...}。

    txt 上传(每行一个 ASIN),不切邮编、不截图——吞吐最高的形态。
    **200 恒等于新建了批次**(v4 语义),inserted 无歧义;撞名抛
    BatchExistsError(带既有 batch_id),由调用方决定是接着轮询还是换名。
    """
    if not asins:
        raise ValueError("submit_batch:ASIN 列表为空")
    url = f"{base_url()}/api/upload"
    body = ("\n".join(str(a).strip() for a in asins if a)).encode("utf-8")
    files = {"file": (f"{batch_name}.txt", body, "text/plain")}
    data = {"batch_name": batch_name, "needs_screenshot": "false"}
    if zip_code:
        data["zip_code"] = str(zip_code)
    last: Exception | None = None

    for attempt in range(max_retries):
        try:
            resp = httpx.post(url, files=files, data=data, headers=_headers(),
                              timeout=httpx.Timeout(300, connect=10))
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 409:
                d = (resp.json() or {}).get("detail") or {}
                raise BatchExistsError(d.get("batch_id"),
                                       d.get("batch_name") or batch_name)
            if resp.status_code in (413, 422):
                raise ValueError(f"上传被拒 HTTP {resp.status_code}: "
                                 f"{resp.text[:200]}")
            raise RuntimeError(f"上传失败 HTTP {resp.status_code}: "
                               f"{resp.text[:200]}")
        except (BatchExistsError, ValueError):
            raise               # 终态:重试无意义
        except (httpx.HTTPError, RuntimeError) as e:
            last = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning("推送批次失败(%s),%ds 后重试(撞名会返 409,"
                               "不会重复建批)", e, wait)
                time.sleep(wait)
    raise RuntimeError(f"推送批次连续 {max_retries} 次失败:{last}")


def batch_status(batch_name: str) -> dict:
    """输入:批次名 → 输出:{status, stats:{total,done,failed,...}}。

    status ∈ running / completed / failed;stats.done+failed 达到 total 即采完。
    """
    resp = httpx.get(f"{base_url()}/api/batches/{batch_name}/status",
                     headers=_headers(), timeout=httpx.Timeout(60, connect=10))
    if resp.status_code == 404:
        raise LookupError(f"批次不存在:{batch_name}")
    if resp.status_code != 200:
        raise RuntimeError(f"批次状态查询失败 HTTP {resp.status_code}: "
                           f"{resp.text[:200]}")
    return resp.json()


def export_incremental(cursor: int, limit: int = 500, *, max_retries: int = 4
                       ) -> tuple[list[dict], int, bool]:
    """输入:游标(独占下界)+ 每页条数 → 输出:(records, next_cursor, has_more)。

    游标推进只认返回的 next_cursor(不要自己算 max(cursor) 或 cursor+count;
    空页不推进是唯一不丢数据的方向——契约边界语义)。
    """
    if limit < 1 or limit > LIMIT_MAX:
        raise ValueError(f"limit 须在 1..{LIMIT_MAX}(收到 {limit})")
    url = f"{base_url()}/api/export/incremental"
    params = {"cursor": int(cursor), "limit": int(limit)}
    last: Exception | None = None

    for attempt in range(max_retries):
        try:
            resp = httpx.get(url, params=params, headers=_headers(),
                             timeout=httpx.Timeout(120, connect=10))
            code = resp.status_code
            if code == 200:
                try:
                    data = resp.json()
                except ValueError as e:      # 200 但不是 JSON:当瞬时故障退避
                    raise RuntimeError(f"导出响应非 JSON: {e}") from None
                got = data.get("contract_version")
                if got not in (None, CONTRACT_VERSION):
                    logger.warning("采集侧契约版本 %s ≠ 本侧 %s:两侧需同步升版",
                                   got, CONTRACT_VERSION)
                records = data.get("records") or []
                # next_cursor 缺失时原样不推进(比瞎猜安全)
                nxt = int(data.get("next_cursor", cursor))
                return records, nxt, bool(data.get("has_more"))
            if code == 401:
                raise ExportAuthError(
                    "采集服务拒绝导出鉴权(401):核对 SCRAPER_EXPORT_TOKEN "
                    "与采集侧 EXPORT_TOKEN 是否一致")
            if code == 409:
                raise RetentionGapError(
                    f"游标 {cursor} 已掉出采集侧保留窗口(409 "
                    f"cursor_below_retention):需全量对账后重置游标,"
                    f"期间不得推进")
            if code == 422:
                raise ValueError(f"导出请求参数被拒(422):{resp.text[:200]}")
            if code == 404:
                # 契约 §4 的坑:catch-all 路由把 incremental 当批次名
                raise RuntimeError(
                    f"导出端点返回 404({resp.text[:120]})——这不是"
                    f"「暂无数据」,是请求打歪或采集侧路由退化,游标不推进")
            if code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"采集服务 HTTP {code}: {resp.text[:160]}")
            raise RuntimeError(f"采集服务未知状态 {code}: {resp.text[:160]}")
        except (ExportAuthError, RetentionGapError, ValueError):
            raise           # 终态错误(401/409/422):重试无意义
        except (httpx.HTTPError, RuntimeError) as e:
            last = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning("增量导出失败(%s),%ds 后重试", e, wait)
                time.sleep(wait)
    raise RuntimeError(f"增量导出连续 {max_retries} 次失败:{last}")
