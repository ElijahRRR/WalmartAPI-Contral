"""沃尔玛 Reports 域接口。

On-request Reports(一端点一函数;轮询/等待/落台账是业务节奏,归 services/item_reports):
  create_report_request(store, type, version, body=None)  POST /v3/reports/reportRequests(1/hour/类型;body 缺省 {})
  iter_report_requests(store, type, status=, since=)      GET  /v3/reports/reportRequests(逐页生成器,轮询走它)
  get_report_request(store, request_id)                   GET  /v3/reports/reportRequests/{id}(兜底;与列表共用 18/hour 桶)
  get_download_url(store, request_id)                     GET  /v3/reports/downloadReport(20/hour)
  download_report(url, proxy)                             预签名地址 → 字节(经店铺固定代理)
  parse_report_csv / extract_item_id / report_row_sku     解析(zip 内 CSV 或裸 CSV)
  report_blob_info(blob)                                  探针体检:zip 成员 / 换行数 / 解析行数 / 首行最长字段

为什么用报表拿 itemId:GET /v3/items 与 catalog/search 的响应都没有数字 itemId
(后者 schema 声明但线上不返回,2026-08-05 实证);全站搜索按 gtin/upc 召回率极差
(实测 3/131)。ITEM 报表覆盖**整个目录**(不传过滤器不按日期筛;官方 CA 站原话
"view their entire catalog",US 站参考页"no limit on the number of rows"),有独立的
「Item ID」列,「Item Page URL」尾段是同一个数(所有者 2026-09-07 贴的后台导出实证,
表头原件 refdata/specs/item_report_header.txt)。

daily_report 用(蓝图矩阵 #25/#26/#27):
  payment_statement()     结算摘要(partnerId/sellerId/账期金额/回款计划)
  available_recon_dates() 可下载对账账期列表(MMDDYYYY)
  iter_recon_records()    对账明细逐条生成器(分页内藏)
"""

import csv
import io
import logging
import re
import zipfile

from api import _client

logger = logging.getLogger("api.reports")

_IP_URL_RE = re.compile(r"/ip/(?:[^/?\s]+/)?(\d+)")


#: 官方 On-request Reports 的请求状态枚举(RECEIVED → INPROGRESS → READY | ERROR;
#: 墨西哥站文档另提过 SUBMITTED,按「未落定」处理)。
REPORT_STATUS_TERMINAL = ("READY", "ERROR")


class ReportQuotaError(RuntimeError):
    """创建报表的额度这一小时已用完(沃尔玛 429,或本地桶已记过一枚)。

    调用方语义:**本轮放弃该店、明天再来**,不补试 —— 创建是每小时一次,
    等待不是选项(2026-09-07 生产实见:补试在桶里睡 3595 秒)。
    """


class ReportRequestError(RuntimeError):
    """报表接口回了非 2xx(401/403 之外)。`status` 是 HTTP 状态码,网络未达为 None。

    调用方按它分诊:4xx(非 429)是请求形状问题,重试无用;5xx / None 可补试。
    """

    def __init__(self, msg: str, status):
        super().__init__(msg)
        self.status = status


def _token(store: dict) -> str:
    return _client.get_token(store["client_id"], store["client_secret"], store["proxy"])


def _fail(status, store: dict, what: str, data) -> None:
    """非 2xx 的统一出口:401/403 归凭证死(与 items._guard_store_dead 同口径),其余 ReportRequestError。"""
    if status in (401, 403):
        raise _client.StoreDeadError(store["name"], status)
    raise ReportRequestError(f"{what} 返回 {status}(店铺 {store['name']}): {data}", status)


def _quota_log(what: str, store: dict, status, headers: dict) -> None:
    """输入:接口名 + 店铺 + 状态码 + 响应头 → 输出:无;把沃尔玛限速头写进日志。

    官方 Rate limiting 页:x-current-token-count = 当前可用令牌数(即该接口的额度),
    x-next-replenishment-time = 下一次加令牌的时刻。报表族的真实桶只有这两个头能证明
    (2026-09-07 生产实见列表接口连打 4 次即 429、下枚令牌 142 秒后,与官方表「200/min」
    对不上;桶到底多大、与单查是否同一个桶,靠每次调用记下来的头回答)。
    """
    tokens = (headers or {}).get("x-current-token-count")
    nxt = (headers or {}).get("x-next-replenishment-time")
    if tokens is not None or nxt is not None:
        logger.info("%s %s 限速头:令牌 %s,下枚 %s(店铺 %s)",
                    what, status, tokens, nxt, store["name"])


def create_report_request(store: dict, report_type: str, report_version: str,
                          body: dict | None = None, *, data_start: str | None = None,
                          data_end: str | None = None) -> dict:
    """输入:店铺 + reportType + reportVersion(+ 可选 body:rowFilters/excludeColumns;
    + 可选数据范围 dataStartTime/dataEndTime,`YYYY-MM-DDTHH:mm:ss.000Z`,不带毫秒回 400)
    → 输出:响应 dict(含 requestId / requestStatus / requestSubmissionDate)。

    官方 POST /v3/reports/reportRequests。reportType/reportVersion **必须走 query**
    (放 body 会 400,2026-08-05 实证);body 装过滤器与数据范围(官方 Get All 响应里
    的 payload 回显字段就是 rowFilters / excludeColumns / dataStartTime / dataEndTime,
    ITEM_PERFORMANCE 指南的示例也把日期放 body),但 **body 必须是 JSON 对象**:不带
    body 沃尔玛回 415 "Supported formats [Content-Type:application/json]"(2026-09-07
    生产实证)—— 所以缺省发 `{}`。⚠ ITEM 报表不带日期**只回 1 行**(2026-09-07 22:05
    C021 探针,在架 1490 行;后台不设时间同样只显示很少),日期由业务层决定传多长。
    ⚠ **max_retries=0**:POST 创建不是幂等的,5xx 后自动重试会重复建报表、
    重复吃每小时一次的创建额度(写操作永不自动兜底)。
    令牌走 **rate_try_acquire**(有就占、没有立刻抛 ReportQuotaError),不睡:
    创建桶一小时一枚,睡等 = 补试在桶里躺一小时。**任何结局都不还令牌**:被拒的
    4xx 沃尔玛照样计数(2026-09-07 22:52 实证:日期格式 400 之后限速头
    x-current-token-count=0,桶容量就是 1),本地还了就是比沃尔玛宽,下一枚必 429。
    """
    cid = store["client_id"]
    payload = dict(body or {})
    if data_start:
        payload["dataStartTime"] = data_start
    if data_end:
        payload["dataEndTime"] = data_end
    if not _client.rate_try_acquire("reports.create", cid):
        raise ReportQuotaError(f"{store['name']} {report_type} 报表本小时已创建过一次"
                               f"(本地限速桶),本轮不再创建")
    status, hdr, data = _client.safe_post_ex(
        f"{_client.base_url()}/v3/reports/reportRequests",
        _token(store), cid, store["proxy"],
        json_body=payload,
        params={"reportType": report_type, "reportVersion": report_version},
        max_retries=0)
    _quota_log("reportRequests 创建", store, status, hdr)
    if status == 429:
        raise ReportQuotaError(f"{store['name']} {report_type} 报表创建被限流(429):"
                               f"该类型每小时只能创建一次")
    if status != 200 or not data:
        _fail(status, store, "reportRequests 创建", data)
    if not data.get("requestId"):
        raise RuntimeError(f"reportRequests 响应无 requestId(店铺 {store['name']}): {data}")
    return data


def iter_report_requests(store: dict, report_type: str, *,
                         status: str | None = None, since: str | None = None,
                         max_pages: int = 5):
    """输入:店铺 + reportType(+ 状态 / 提交起始时间 ISO 8601)→ 输出:请求 dict 生成器,逐页产出。

    官方 GET /v3/reports/reportRequests,缺省 10 条一页,只能查最近 30 天;响应
    requests[] 每项含 requestId / requestStatus / src(SC / API / Scheduler)/
    requestSubmissionDate。
    分页是 **orders 型**(蓝图 §4 模型 2):nextCursor 是完整 query 串
    (`reportType=ITEM&page=2&limit=10`,不带 `?`),官方参考页原话「use nextCursor
    value instead of query params」—— 必须**直接拼在 URL 上**重发;当成 `nextCursor=`
    参数传,服务端忽略未知参数、原样返回第一页(2026-09-07 生产实见:同一 cursor
    连回三次,第四次 429)。同 cursor 重复 = 服务端未推进,立即停;空页即止;
    max_pages 是护栏不是常态。
    生成器:调用方找到自己那条就 break,后面的页不再请求 —— 轮询一次通常只花一枚令牌。
    限速:官方表写 200/min,生产实见 4 次即 429、下枚令牌 142 秒后 ⇒ 小时级桶,
    与单查共用 `reports.query`(18/hour 持久);真实桶大小看 _quota_log 记的头。
    """
    params: dict = {"reportType": report_type}
    if status:
        params["requestStatus"] = status
    if since:
        params["requestSubmissionStartDate"] = since
    url = f"{_client.base_url()}/v3/reports/reportRequests"
    cursor: str | None = None
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        _client.rate_acquire("reports.query", store["client_id"])
        if cursor:
            # 分页模型 2:nextCursor 就是完整 query 串,直接拼 URL,不再带 params
            st, hdr, data = _client.safe_get_ex(
                url + ("" if cursor.startswith("?") else "?") + cursor,
                _token(store), store["client_id"], store["proxy"], max_retries=3)
        else:
            st, hdr, data = _client.safe_get_ex(
                url, _token(store), store["client_id"], store["proxy"],
                params=params, max_retries=3)
        _quota_log("reportRequests 列表", store, st, hdr)
        if st != 200 or data is None:
            _fail(st, store, "reportRequests 列表", data)
        rows = data.get("requests") or []
        yield from rows
        cursor = data.get("nextCursor")
        if not cursor or not rows:
            return
        if cursor in seen:
            logger.warning("reportRequests 列表 nextCursor 重复,停止翻页(店铺 %s 第 %d 页)",
                           store["name"], page)
            return
        seen.add(cursor)
    logger.warning("reportRequests 列表触达翻页上限 %d 页,可能未看全(店铺 %s)",
                   max_pages, store["name"])


def get_report_request(store: dict, request_id: str) -> dict:
    """输入:店铺 + requestId → 输出:该请求的状态 dict(requestStatus 等)。

    官方 GET /v3/reports/reportRequests/{requestId},**20/hour** —— 只作
    列表接口找不到该 requestId 时的兜底,不用来高频轮询。与列表共用 `reports.query`
    桶(同路径前缀;2026-09-07 实证列表也是小时级桶,分开记会两边相加打超)。
    """
    _client.rate_acquire("reports.query", store["client_id"])
    st, hdr, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/reports/reportRequests/{request_id}",
        _token(store), store["client_id"], store["proxy"], max_retries=3)
    _quota_log("报表状态查询", store, st, hdr)
    if st != 200 or not data:
        _fail(st, store, "报表状态查询", data)
    return data


def get_download_url(store: dict, request_id: str) -> tuple[str, str | None]:
    """输入:店铺 + requestId → 输出:(预签名 downloadURL, downloadURLExpirationTime 或 None)。

    官方 GET /v3/reports/downloadReport?requestId=,**20/hour**;URL 有时效,
    拿到就该立刻下载(时效长度官方未公布,只给 expirationTime 字段)。
    """
    _client.rate_acquire("reports.download", store["client_id"])
    st, hdr, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/reports/downloadReport",
        _token(store), store["client_id"], store["proxy"],
        params={"requestId": request_id}, max_retries=3)
    _quota_log("downloadReport", store, st, hdr)
    if st != 200 or not data:
        _fail(st, store, "downloadReport", data)
    url = data.get("downloadURL") or (data.get("downloadURLS") or [None])[0]
    if not url:
        raise RuntimeError(f"downloadReport 响应无下载地址(店铺 {store['name']}): {data}")
    return url, data.get("downloadURLExpirationTime")


def download_report(url: str, proxy: str | None) -> bytes:
    """输入:预签名下载地址 + 店铺代理 → 输出:报表字节(zip 包或裸 CSV)。"""
    return _client.download_bytes(url, proxy)


def _report_csv_member(blob: bytes) -> tuple[str | None, bytes, list[tuple[str, int]]]:
    """输入:下载的报表字节 → 输出:(选中的 zip 成员名或 None, CSV 字节, zip 全部成员 [(名, 解压大小)])。

    zip 包取第一个 .csv 成员(没有 .csv 就取第一个成员);裸 CSV 原样返回。
    """
    if blob[:2] != b"PK":
        return None, blob, []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        members = [(i.filename, i.file_size) for i in zf.infolist()]
        names = [n for n, _ in members if n.lower().endswith(".csv")] or [n for n, _ in members]
        return names[0], zf.read(names[0]), members


def _decode_report(raw: bytes) -> str:
    return raw.decode("utf-8-sig", errors="replace")


def parse_report_csv(blob: bytes) -> list[dict]:
    """输入:下载的报表字节(zip 内含 CSV,或裸 CSV)→ 输出:行 dict 列表(表头为键)。"""
    _, raw, _ = _report_csv_member(blob)
    return list(csv.DictReader(io.StringIO(_decode_report(raw))))


def report_blob_info(blob: bytes) -> dict:
    """输入:下载的报表字节 → 输出:体检 dict(bytes / members / member / csv_bytes / lines / rows / longest_field)。

    探针用,回答「报表只有 N 行」到底是沃尔玛只给了 N 行,还是解析出了问题:
    zip 里有几个成员、取的哪个;CSV 有多少个换行、解析出多少行 —— 换行数远大于
    行数 + 1,多半是某个字段引号没闭合把后面整个文件吞进了一个字段(首行最长字段
    会大得离谱);成员不止一个则可能是分片。与 parse_report_csv 同一条取成员/解码路径。
    """
    member, raw, members = _report_csv_member(blob)
    text = _decode_report(raw)
    rows = list(csv.DictReader(io.StringIO(text)))
    longest = max((len(str(v or "")) for r in rows[:1] for v in r.values()), default=0)
    return {"bytes": len(blob), "members": members, "member": member, "csv_bytes": len(raw),
            "lines": text.count("\n"), "rows": len(rows), "longest_field": longest}


def item_id_from_column(row: dict) -> str | None:
    """输入:ITEM 报表单行 → 输出:「Item ID」列的数字串(列名大小写/下划线模糊匹配),没有或非数字给 None。"""
    for key, val in row.items():
        k = key.strip().lower().replace("_", " ")
        if k in ("item id", "walmart item id", "walmart.com item id"):
            v = str(val or "").strip()
            return v if v.isdigit() else None
    return None


def item_id_from_url(row: dict) -> str | None:
    """输入:ITEM 报表单行 → 输出:「Item Page URL」/ip/…/<数字> 尾段;没有给 None。"""
    for key, val in row.items():
        if "url" in key.lower() and val:
            m = _IP_URL_RE.search(str(val))
            if m:
                return m.group(1)
    return None


def extract_item_id(row: dict) -> str | None:
    """输入:ITEM 报表单行 → 输出:数字 itemId 或 None(显式 Item ID 列优先,其次 URL 尾段)。

    两列是否一致的判定不在这里(那是业务判据,归 services/item_reports.map_item_ids)。
    """
    return item_id_from_column(row) or item_id_from_url(row)


def report_row_sku(row: dict) -> str | None:
    """输入:报表单行 → 输出:SKU(列名大小写/空格模糊匹配)。"""
    for key, val in row.items():
        if key.strip().lower() == "sku" and val:
            return str(val).strip()
    return None


def payment_statement(store: dict) -> dict:
    """输入:店铺 → 输出:结算摘要原始 dict(含 partnerId/sellerInfo/accountSummary 等)。

    sellerId 从 storeFrontUrl 正则 /seller/(\\d+) 提取是调用方(services)的事,
    本层只负责把接口调对。官方 15/min。
    """
    _client.rate_acquire("reports.payment_statement", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/report/payment/statement",
        token, store["client_id"], store["proxy"], max_retries=3)
    if status != 200 or data is None:
        raise RuntimeError(f"payment/statement 返回 {status}(店铺 {store['name']})")
    return data


def available_recon_dates(store: dict) -> list[str]:
    """输入:店铺 → 输出:可下载对账账期列表(MMDDYYYY 字符串)。"""
    _client.rate_acquire("reports.recon", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/report/reconreport/availableReconFiles",
        token, store["client_id"], store["proxy"],
        params={"reportVersion": "v1"}, max_retries=3)
    if status != 200 or data is None:
        raise RuntimeError(f"availableReconFiles 返回 {status}(店铺 {store['name']})")
    return list(data.get("availableApReportDates") or [])


def iter_recon_records(store: dict, report_date: str):
    """输入:店铺 + 账期(MMDDYYYY)→ 输出:对账明细行 dict 生成器(全量,无截断)。

    实现走 **CSV 端点** /v3/report/reconreport/reconFile(ZIP 包裹完整报表)。
    为什么不用 JSON 端点(订单中心v1 2026-08-04 实测,修正本文件旧实现):
    reconFileJson 每账期硬截断 1000 行,offset 只接受 0(传 1/999/1000 一律
    INVALID_OFFSET),nextOffset 是字节偏移不是行号——超 1000 行的账期在 JSON
    路径下必然丢数据。CSV 无行数上限(同账期 JSON 失败/CSV 1211 行),字段名与
    JSON 完全一致("Transaction Type"/"Total Payable"/"Purchase Order #" 等)。
    必须 Accept: application/octet-stream(text/csv 返 406)。
    调用方按需 break(如只找 Transaction Type == 'PaymentSummary' 的行)。
    """
    _client.rate_acquire("reports.recon", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    status, _, body = _client.safe_get_raw(
        f"{_client.base_url()}/v3/report/reconreport/reconFile",
        token, store["client_id"], store["proxy"],
        params={"reportDate": report_date, "reportVersion": "v1"},
        timeout=120, max_retries=3, accept="application/octet-stream")
    if status in (401, 403):
        raise _client.StoreDeadError(f"{store.get('name')} reconFile", status)
    if status is None or not (200 <= status < 300) or not body:
        raise RuntimeError(f"reconFile 返回 {status}"
                           f"(店铺 {store['name']}, 账期 {report_date})")
    if body[:2] != b"PK":   # 出错时可能 HTTP 200 但返回 JSON/XML 错误体
        raise RuntimeError(f"reconFile 非 ZIP 响应(店铺 {store['name']},"
                           f" 账期 {report_date}): {body[:160]!r}")
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError(f"reconFile ZIP 内无 CSV: {zf.namelist()}")
        raw = zf.read(names[0])
    yield from csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))
