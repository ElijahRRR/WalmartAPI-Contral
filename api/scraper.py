"""亚马逊采集服务接口(amazon-scraper-v4;增量导出契约 v1)。

契约权威:docs/scraper_migration_brief.md 第五节 + §5.1 行为补遗
(采集侧副本 docs/incremental_export_contract.md,改动需两侧同步)。

api 层只做接口适配:base_url/token 从 registry 取、超时、按状态码分流、退避。
"够不够格进 products"这类判断在 services/product_ingest,不在这里。

  submit_batch(batch_name, asins)       -> {batch_id, inserted, ...}
  batch_status(batch_name)              -> {status, stats:{...}}
  batch_failures(batch_id)              -> [{asin, error_type, error_detail, ...}]
  export_incremental(cursor, limit)     -> (records, next_cursor, has_more)

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


_warned_no_token = False


def _headers() -> dict:
    global _warned_no_token
    token = os.environ.get("SCRAPER_EXPORT_TOKEN", "").strip()
    if not token:
        # 契约:鉴权可选,采集侧没配 EXPORT_TOKEN 时放行。公网部署必须配。
        # **每进程只喊一次**:订单审核一轮要推上百个按邮编的批次,每请求一行
        # 告警会把真正该看的日志(推了哪些批次、哪些失败)整个淹掉。
        if not _warned_no_token:
            logger.warning("SCRAPER_EXPORT_TOKEN 未配置,本进程所有采集请求"
                           "都不带鉴权头(公网部署必须配)")
            _warned_no_token = True
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
                 needs_screenshot: bool = False, max_retries: int = 3) -> dict:
    """输入:批次名 + ASIN 列表(+邮编/是否要截图)→ 输出:{batch_id, inserted, ...}。

    txt 上传(每行一个 ASIN)。两个可选开关都会拖慢吞吐,默认都关——
    维护链全量重推走最快形态(不切邮编不截图,2000~3000/分钟),
    订单审核按收件邮编采、且要截图做佐证,两项都开。

    ⚠ 调用方约束(采集侧语义,api 层不替你做):**同一 ASIN 的不同邮编
    不可以放进同一批次**,采集侧按 ASIN 唯一存结果,同批会互相覆盖丢数据。
    编排见 services.order_audit.plan_round。

    **200 恒等于新建了批次**(v4 语义),inserted 无歧义;撞名抛
    BatchExistsError(带既有 batch_id),由调用方决定是接着轮询还是换名。
    """
    if not asins:
        raise ValueError("submit_batch:ASIN 列表为空")
    url = f"{base_url()}/api/upload"
    body = ("\n".join(str(a).strip() for a in asins if a)).encode("utf-8")
    files = {"file": (f"{batch_name}.txt", body, "text/plain")}
    data = {"batch_name": batch_name,
            "needs_screenshot": "true" if needs_screenshot else "false"}
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


def submit_json(batch_name: str, items: list, *, needs_screenshot: bool = False,
                max_retries: int = 3) -> dict:
    """输入:批次名 + [(asin, 邮编)] → 输出:{batch_id, inserted, per_asin_zip_count}。

    走 `POST /api/batches`(JSON),与 submit_batch 的 `/api/upload` 共用同一个
    核心函数:撞名 409、回调注册、回显读回值逐字一致。用它的理由有两个——
    不必把 ASIN 列表拼成 txt 再 multipart 上传,以及**逐 ASIN 带邮编**
    (`items[].zip_code`,采集侧一等能力,邮编三档:逐 ASIN > 批次级 > 服务端默认)。

    ⚠ 调用方约束(api 层不替你做):**同一批里同一个 ASIN 不能出现两个不同
    邮编** —— 采集侧 `tasks` 是 `UNIQUE(batch_id, asin)`,会回
    `400 conflicting_zip_for_asin`(明确拒绝,不静默取第一个)。
    不同 ASIN 的不同邮编同批则完全正常。编排见
    services.order_audit.plan_waves。

    响应里的 `per_asin_zip_count` 应等于带了邮编的 ASIN 数:对不上说明邮编
    没被采纳(比如格式不合法被退回批次邮编),**那会按错地区采回价格**。
    """
    pairs = [(str(a).strip(), str(z).strip()) for a, z in items if a]
    if not pairs:
        raise ValueError("submit_json:ASIN 列表为空")
    dup = {a for a, _ in pairs if len({z for b, z in pairs if b == a}) > 1}
    if dup:
        # 本地先拦一道:采集侧会 400,但那时批次已经建出来了(撞名占位),
        # 下一轮同名重推还得再撞一次 409
        raise ValueError(f"submit_json:同批内这些 ASIN 有多个邮编,必须拆批"
                         f"(见 plan_waves):{sorted(dup)[:5]}")
    body = {"batch_name": batch_name,
            "items": [{"asin": a, "zip_code": z} for a, z in pairs],
            "needs_screenshot": bool(needs_screenshot)}
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = httpx.post(f"{base_url()}/api/batches", json=body,
                              headers=_headers(),
                              timeout=httpx.Timeout(300, connect=10))
            if resp.status_code == 200:
                return resp.json()
            detail = {}
            try:
                detail = (resp.json() or {}).get("detail") or {}
            except ValueError:
                pass
            if resp.status_code == 409:
                d = detail if isinstance(detail, dict) else {}
                raise BatchExistsError(d.get("batch_id"),
                                       d.get("batch_name") or batch_name)
            if resp.status_code in (400, 413, 422):
                # 400 conflicting_zip_for_asin 是调用方编排错了,重试一万次也一样
                raise ValueError(f"推送被拒 HTTP {resp.status_code}: "
                                 f"{resp.text[:300]}")
            raise RuntimeError(f"推送失败 HTTP {resp.status_code}: "
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


def screenshot_list(batch_name: str) -> list[dict]:
    """输入:批次名 → 输出:该批次逐 ASIN 的截图状态 [{asin, status, url, ...}]。

    `GET /api/screenshots?batch_name=`(采集侧 2026-08-10 新增)。
    **`url` 仅在 `status == "done"` 时非 null** —— 别的状态那张图不存在,
    拿 URL 去取只会撞 404。

    一次拿全比逐 ASIN 试探省得多:一批 50 个 ASIN、只有 10 张图好了,
    逐个试要发 50 次(40 次收 409),这里 1 次拿清单 + 10 次取图。
    自动翻页(next_cursor 为 null 即到底)。
    """
    out: list[dict] = []
    cursor = None
    while True:
        params = {"batch_name": batch_name, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        resp = httpx.get(f"{base_url()}/api/screenshots", params=params,
                         headers=_headers(),
                         timeout=httpx.Timeout(60, connect=10))
        if resp.status_code == 404:
            raise LookupError(f"批次不存在:{batch_name}")
        if resp.status_code != 200:
            raise RuntimeError(f"截图清单查询失败 HTTP {resp.status_code}: "
                               f"{resp.text[:200]}")
        data = resp.json() or {}
        out.extend(data.get("items") or [])
        cursor = data.get("next_cursor")
        if not cursor:
            return out


class ScreenshotPending(RuntimeError):
    """截图还没截好(409)——**稍后再来**,不是失败。响应头带 Retry-After。"""


class ScreenshotGone(RuntimeError):
    """截图不会再有了(404 没记录/已清理 或 410 截图失败)——**别重试**。

    采集侧把这两种与"还没好"分成三个状态码,正是为了让调用方有"该不该重试"
    的判据(旧的 /static/screenshots 路径上后三种全是同一个 404,分不出来)。
    """


def fetch_screenshot(batch_name: str, asin: str) -> bytes:
    """输入:批次名 + ASIN → 输出:PNG 字节;未就绪抛 ScreenshotPending,
    不会再有抛 ScreenshotGone。

    走 `GET /api/screenshots/{batch_name}/{asin}`(采集侧 2026-08-10 新增)。
    批次名是隔离键——截图落盘就是 `<批次名>/<asin>.png`,所以同一 ASIN 的
    不同邮编批次各有各的图,不会互相覆盖。
    """
    resp = httpx.get(f"{base_url()}/api/screenshots/{batch_name}/{asin}",
                     headers=_headers(), timeout=httpx.Timeout(60, connect=10))
    if resp.status_code == 200:
        return resp.content
    detail = {}
    try:
        detail = (resp.json() or {}).get("detail") or {}
    except ValueError:
        pass
    if not isinstance(detail, dict):
        detail = {"message": str(detail)}
    if resp.status_code == 409:
        raise ScreenshotPending(f"{asin}@{batch_name} 截图未就绪"
                                f"(status={detail.get('status')})")
    if resp.status_code in (404, 410):
        raise ScreenshotGone(f"{asin}@{batch_name} 截图不会再有"
                             f"(HTTP {resp.status_code} "
                             f"{detail.get('error') or detail.get('message') or ''}"
                             f"{' ' + str(detail.get('error_detail')) if detail.get('error_detail') else ''})")
    raise RuntimeError(f"取截图失败 HTTP {resp.status_code}: {resp.text[:200]}")


def prioritize(batch_id) -> bool:
    """输入:batch_id(**不是批次名**)→ 输出:插队成功与否(不抛异常)。

    `POST /api/batches/{batch_id}/prioritize` —— 把该批次**还没开始的**任务
    提到 `priority=10`。采集侧 worker 每次只拉 `MAX(priority)` 的 pending 任务、
    默认 0,所以提到 10 就是排到所有常规任务之前。
    逐字沿用旧仓语义(`scraper_client.aprioritize`,旧仓 `V3客户端.提交批次`
    在每次提交后调它)。

    **best-effort:失败返回 False,绝不抛**。插不了队只是慢,而把已经提交成功
    的批次因为"插队请求失败"判成失败,会让调用方去重推 —— 那才是真损失
    (撞名 409 → 换名重建 → 同一批 ASIN 采两遍)。所以这里连重试都不做:
    这一轮插不进去,下一轮的批次自己会插。

    ⚠ 只对 **pending** 生效:已经被 worker 领走的任务改不了优先级,
    所以"提交后立刻调"才有意义,隔几分钟再调等于只对剩下的尾巴生效。
    """
    if not batch_id:
        return False
    try:
        # ⚠ `base_url()` / `_headers()` 也要在 try 里:它们在配置缺失时抛
        # LookupError,漏在外面就等于"承诺不抛却抛了"。2026-08-17 用例当场
        # 抓到:那个异常一路冒到 order_audit 的泛化 except,把**已经推成功**
        # 的批次记成 failed —— 正是"做成了报失败"那个形状
        url = f"{base_url()}/api/batches/{batch_id}/prioritize"
        resp = httpx.post(url, headers=_headers(),
                          timeout=httpx.Timeout(30, connect=10))
    except (httpx.HTTPError, LookupError, ValueError) as e:
        logger.warning("批次 %s 插队请求失败(不影响已提交的采集):%s",
                       batch_id, e)
        return False
    if resp.status_code != 200:
        logger.warning("批次 %s 插队被拒 HTTP %s(不影响已提交的采集):%s",
                       batch_id, resp.status_code, resp.text[:200])
        return False
    return True


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


FAILURES_LIMIT_MAX = 100000     # 采集侧 Query(ge=1, le=100000),默认即"全要"


def batch_failures(batch_id, *, error_type: str = "",
                   limit: int = FAILURES_LIMIT_MAX) -> list[dict]:
    """输入:batch_id(整数)→ 输出:失败任务明细行列表。

    每行:{asin, status, error_type, error_detail, retry_count, worker_id,
    updated_at}。**按 batch_id 而不是批次名**——采集侧另有一条按名字的旧接口
    只返回最近 200 条(旧仓库踩过:失败超 200 就静默看不全),v4 已不提供,
    本函数是唯一入口。

    这里拿到的是「根本没采到」的原因(captcha/timeout/blocked/…);
    「采到了但不完整」在 snapshots.outcome / completeness_ok,两者互补。
    """
    if batch_id in (None, ""):
        raise ValueError("batch_failures:batch_id 为空(该批次没记下 id)")
    params = {"limit": max(1, min(int(limit), FAILURES_LIMIT_MAX))}
    if error_type:
        params["error_type"] = error_type
    resp = httpx.get(f"{base_url()}/api/batches/{int(batch_id)}/failures",
                     params=params, headers=_headers(),
                     timeout=httpx.Timeout(120, connect=10))
    if resp.status_code == 404:
        raise LookupError(f"批次不存在:batch_id={batch_id}")
    if resp.status_code != 200:
        raise RuntimeError(f"失败明细查询失败 HTTP {resp.status_code}: "
                           f"{resp.text[:200]}")
    return (resp.json() or {}).get("failed_tasks") or []


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
