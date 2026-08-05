"""沃尔玛 Reports 域接口。

当前实现 On-request Reports 的 ITEM 报表(catalog_sync 回填 item_id 用):
  fetch_item_report()  请求 → 轮询 → 下载 → 解析,返回行 dict 列表

为什么用报表拿 itemId:GET /v3/items 与 catalog/search 的响应都没有数字 itemId
(后者 schema 声明但线上不返回,2026-08-05 实证);全站搜索按 gtin/upc 召回率极差
(实测 3/131)。ITEM 报表覆盖全部商品,`Item Page URL` 列(或 Item ID 列,版本而异)
含数字 itemId——这也是市面 ERP 工具批量拿 itemId 的通用渠道。

payment_statement / recon 系列按蓝图 §7 预留,随 daily_report 迁移时实现。
"""

import csv
import io
import logging
import re
import time
import zipfile

from api import _client

logger = logging.getLogger("api.reports")

_IP_URL_RE = re.compile(r"/ip/(?:[^/?\s]+/)?(\d+)")


def _request_report(store: dict, report_type: str) -> str:
    """输入:店铺 + 报表类型 → 输出:requestId。"""
    _client.rate_acquire("reports.request", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    status, _, data = _client.safe_post_ex(
        f"{_client.base_url()}/v3/reports/reportRequests",
        token, store["client_id"], store["proxy"],
        params={"reportType": report_type},
        json_body={"reportType": report_type, "reportVersion": "v1"},
        max_retries=3)
    if status != 200 or not data:
        raise RuntimeError(f"reportRequests 提交失败 {status}(店铺 {store['name']}): {data}")
    req_id = data.get("requestId") or (data.get("requestStatus") or {}).get("requestId")
    if not req_id:
        raise RuntimeError(f"reportRequests 响应无 requestId(店铺 {store['name']}): {data}")
    return req_id


def _report_status(store: dict, request_id: str) -> str:
    _client.rate_acquire("reports.poll", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/reports/reportRequests/{request_id}",
        token, store["client_id"], store["proxy"], max_retries=3)
    if status != 200 or not data:
        raise RuntimeError(f"报表状态查询失败 {status}(店铺 {store['name']}): {data}")
    return data.get("requestStatus") or data.get("status") or ""


def _download_report(store: dict, request_id: str) -> bytes:
    _client.rate_acquire("reports.poll", store["client_id"])
    token = _client.get_token(store["client_id"], store["client_secret"], store["proxy"])
    status, _, data = _client.safe_get_ex(
        f"{_client.base_url()}/v3/reports/downloadReport",
        token, store["client_id"], store["proxy"],
        params={"requestId": request_id}, max_retries=3)
    if status != 200 or not data:
        raise RuntimeError(f"downloadReport 失败 {status}(店铺 {store['name']}): {data}")
    url = data.get("downloadURL") or (data.get("downloadURLS") or [None])[0]
    if not url:
        raise RuntimeError(f"downloadReport 响应无下载地址(店铺 {store['name']}): {data}")
    return _client.download_bytes(url, store["proxy"])


def parse_report_csv(blob: bytes) -> list[dict]:
    """输入:下载的报表字节(zip 内含 CSV,或裸 CSV)→ 输出:行 dict 列表(表头为键)。"""
    if blob[:2] == b"PK":       # zip 包
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")] or zf.namelist()
            blob = zf.read(names[0])
    text = blob.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def extract_item_id(row: dict) -> str | None:
    """输入:ITEM 报表单行 → 输出:数字 itemId 或 None。

    优先显式 Item ID 列(列名随版本变,模糊匹配);否则从 Item Page URL 提取
    /ip/.../<数字> 尾段。
    """
    for key, val in row.items():
        k = key.strip().lower().replace("_", " ")
        if k in ("item id", "walmart item id", "walmart.com item id") and val and str(val).strip().isdigit():
            return str(val).strip()
    for key, val in row.items():
        if "url" in key.lower() and val:
            m = _IP_URL_RE.search(str(val))
            if m:
                return m.group(1)
    return None


def report_row_sku(row: dict) -> str | None:
    """输入:报表单行 → 输出:SKU(列名大小写/空格模糊匹配)。"""
    for key, val in row.items():
        if key.strip().lower() == "sku" and val:
            return str(val).strip()
    return None


def fetch_item_report(store: dict, *, poll_interval: float = 20.0,
                      timeout: float = 900.0) -> list[dict]:
    """输入:店铺 → 输出:ITEM 报表全部行(dict 列表)。

    异步协议:提交请求 → 轮询至 READY(默认 20s 间隔/15 分钟超时)→ 下载解析。
    超时/生成失败抛 RuntimeError,由调用方决定跳过或重试。
    """
    req_id = _request_report(store, "ITEM")
    logger.info("店铺 %s ITEM 报表已提交 requestId=%s,轮询等待生成", store["name"], req_id)
    deadline = time.monotonic() + timeout
    while True:
        state = _report_status(store, req_id).upper()
        if state == "READY":
            break
        if state in ("ERROR", "FAILED", "CANCELLED"):
            raise RuntimeError(f"ITEM 报表生成失败 status={state}(店铺 {store['name']})")
        if time.monotonic() > deadline:
            raise RuntimeError(f"ITEM 报表 {timeout:.0f}s 未就绪(店铺 {store['name']},"
                               f"requestId={req_id})")
        time.sleep(poll_interval)
    rows = parse_report_csv(_download_report(store, req_id))
    logger.info("店铺 %s ITEM 报表就绪:%d 行", store["name"], len(rows))
    return rows
