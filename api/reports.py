"""沃尔玛 Reports 域接口。

On-request Reports(一端点一函数;轮询/等待/落台账是业务节奏,归 services/item_reports):
  create_report_request(store, type, version, body=None)  POST /v3/reports/reportRequests(1/hour/类型)
  list_report_requests(store, type, status=, since=)      GET  /v3/reports/reportRequests(200/min,轮询走它)
  get_report_request(store, request_id)                   GET  /v3/reports/reportRequests/{id}(20/hour,兜底)
  get_download_url(store, request_id)                     GET  /v3/reports/downloadReport(20/hour)
  download_report(url, proxy)                             预签名地址 → 字节(经店铺固定代理)
  parse_report_csv / extract_item_id / report_row_sku     解析(zip 内 CSV 或裸 CSV)

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
    """创建报表被 429 拒绝:该店该类型报表这一小时的额度已用完。

    调用方语义:**本轮放弃该店、明天再来**,不补试 —— 创建桶是每小时一次的
    持久桶,补试只会在 rate_acquire 里睡到下一个小时。
    """


def _token(store: dict) -> str:
    return _client.get_token(store["client_id"], store["client_secret"], store["proxy"])


def _fail(status, store: dict, what: str, data) -> None:
    """非 2xx 的统一出口:401/403 归凭证死(与 items._guard_store_dead 同口径),其余 RuntimeError。"""
    if status in (401, 403):
        raise _client.StoreDeadError(store["name"], status)
    raise RuntimeError(f"{what} 返回 {status}(店铺 {store['name']}): {data}")


def create_report_request(store: dict, report_type: str, report_version: str,
                          body: dict | None = None) -> dict:
    """输入:店铺 + reportType + reportVersion(+ 可选 body:rowFilters/excludeColumns)
    → 输出:响应 dict(含 requestId / requestStatus / requestSubmissionDate)。

    官方 POST /v3/reports/reportRequests。reportType/reportVersion **必须走 query**
    (放 body 会 400,2026-08-05 实证);body 只装过滤器,不传 = 整个目录。
    ⚠ **max_retries=0**:POST 创建不是幂等的,5xx 后自动重试会重复建报表、
    重复吃每小时一次的创建额度(写操作永不自动兜底)。429 抛 ReportQuotaError。
    """
    _client.rate_acquire("reports.create", store["client_id"])
    status, _, data = _client.safe_post_ex(
        f"{_client.base_url()}/v3/reports/reportRequests",
        _token(store), store["client_id"], store["proxy"],
        json_body=body or None,
        params={"reportType": report_type, "reportVersion": report_version},
        max_retries=0)
    if status == 429:
        raise ReportQuotaError(f"{store['name']} {report_type} 报表创建被限流(429):"
                               f"该类型每小时只能创建一次")
    if status != 200 or not data:
        _fail(status, store, "reportRequests 创建", data)
    if not data.get("requestId"):
        raise RuntimeError(f"reportRequests 响应无 requestId(店铺 {store['name']}): {data}")
    return data


def list_report_requests(store: dict, report_type: str, *,
                         status: str | None = None, since: str | None = None,
                         max_pages: int = 5) -> list[dict]:
    """输入:店铺 + reportType(+ 状态 / 提交起始时间 ISO 8601)→ 输出:请求列表(dict)。

    官方 GET /v3/reports/reportRequests,**200/min** —— 轮询报表状态走这条,
    不走 20/hour 的单查。只能查最近 30 天的请求;`since` 用
    requestSubmissionStartDate 收窄,免得翻页。响应 requests[] 每项含
    requestId / requestStatus / src(SC / API / Scheduler)/ requestSubmissionDate。
    分页:响应带 nextCursor 时原样回传,最多 max_pages 页(护栏,不是常态)。
    """
    params: dict = {"reportType": report_type}
    if status:
        params["requestStatus"] = status
    if since:
        params["requestSubmissionStartDate"] = since
    out: list[dict] = []
    for _ in range(max_pages):
        _client.rate_acquire("reports.list", store["client_id"])
        st, _, data = _client.safe_get_ex(
            f"{_client.base_url()}/v3/reports/reportRequests",
            _token(store), store["client_id"], store["proxy"],
            params=params, max_retries=3)
        if st != 200 or data is None:
            _fail(st, store, "reportRequests 列表", data)
        out.extend(data.get("requests") or [])
        cursor = data.get("nextCursor")
        if not cursor:
            break
        params = dict(params, nextCursor=cursor)
    return out


def get_report_request(store: dict, request_id: str) -> dict:
    """输入:店铺 + requestId → 输出:该请求的状态 dict(requestStatus 等)。

    官方 GET /v3/reports/reportRequests/{requestId},**20/hour** —— 只作
    列表接口找不到该 requestId 时的兜底,不用来高频轮询。
    """
    _client.rate_acquire("reports.status", store["client_id"])
    st, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/reports/reportRequests/{request_id}",
        _token(store), store["client_id"], store["proxy"], max_retries=3)
    if st != 200 or not data:
        _fail(st, store, "报表状态查询", data)
    return data


def get_download_url(store: dict, request_id: str) -> tuple[str, str | None]:
    """输入:店铺 + requestId → 输出:(预签名 downloadURL, downloadURLExpirationTime 或 None)。

    官方 GET /v3/reports/downloadReport?requestId=,**20/hour**;URL 有时效,
    拿到就该立刻下载(时效长度官方未公布,只给 expirationTime 字段)。
    """
    _client.rate_acquire("reports.download", store["client_id"])
    st, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/reports/downloadReport",
        _token(store), store["client_id"], store["proxy"],
        params={"requestId": request_id}, max_retries=3)
    if st != 200 or not data:
        _fail(st, store, "downloadReport", data)
    url = data.get("downloadURL") or (data.get("downloadURLS") or [None])[0]
    if not url:
        raise RuntimeError(f"downloadReport 响应无下载地址(店铺 {store['name']}): {data}")
    return url, data.get("downloadURLExpirationTime")


def download_report(url: str, proxy: str | None) -> bytes:
    """输入:预签名下载地址 + 店铺代理 → 输出:报表字节(zip 包或裸 CSV)。"""
    return _client.download_bytes(url, proxy)


def parse_report_csv(blob: bytes) -> list[dict]:
    """输入:下载的报表字节(zip 内含 CSV,或裸 CSV)→ 输出:行 dict 列表(表头为键)。"""
    if blob[:2] == b"PK":       # zip 包
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")] or zf.namelist()
            blob = zf.read(names[0])
    text = blob.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


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
