"""daily_report 链条回归:业务规则(照搬清单)、insights 204、orders 分页模型 2、xlsx 解析。"""

import io
import time
from datetime import datetime, timezone

import httpx
import openpyxl
import pytest

from api import _client, insights, orders as orders_api
from services import kpi

STORE = {"name": "T1", "client_id": "cid_dr", "client_secret": "sec", "proxy": None}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setenv("WALMART_DATA_ROOT", str(tmp_path))
    _client._token_cache.clear()
    _client._rate_state.clear()
    for c in _client._client_pool.values():
        c.close()
    _client._client_pool.clear()
    monkeypatch.setattr(time, "sleep", lambda s: None)
    yield
    for c in _client._client_pool.values():
        c.close()
    _client._client_pool.clear()
    _client._token_cache.clear()
    _client._rate_state.clear()


def _use(monkeypatch, handler):
    def full_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})
        return handler(request)
    monkeypatch.setattr(_client, "_build_transport",
                        lambda proxy: httpx.MockTransport(full_handler))


# ── 业务规则(照搬清单) ────────────────────────────────────────────────────────

def test_sales_window_anchored_at_cn_0630():
    # 中国时间 08:00(UTC 00:00)跑 → 窗口 = 昨天 06:30 ~ 今天 06:30(中国时间)
    start, end = kpi.sales_window_utc(datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc))
    assert end == "2026-08-04T22:30:00Z"      # 今天 06:30 CN = 前一日 22:30 UTC
    assert start == "2026-08-03T22:30:00Z"
    # 中国时间 05:00(未过锚点)→ 锚点退回昨天 06:30
    start2, end2 = kpi.sales_window_utc(datetime(2026, 8, 4, 21, 0, tzinfo=timezone.utc))
    assert end2 == "2026-08-03T22:30:00Z"


def test_settlement_rules_non_active_payout_zero():
    st = {"paymentStatus": "HOLD", "storeFrontUrl": "https://www.walmart.com/seller/123456",
          "partnerId": "P1", "sellerInfo": {"sellerStatus": "ACTIVE"},
          "accountSummary": {"closingBalance": 500.0, "reserveToDate": -30.5,
                             "scheduledSettlementDate": "2026-08-19"},
          "transactionDetails": {"saleAggregate": {"productPrice": 800, "netComm": 80}},
          "refundDetails": {"productPrice": 20}}
    out = kpi.extract_settlement(st)
    assert out["payout"] == 0.0               # 非 ACTIVE 强制 0
    assert out["no_hold"] is False
    assert out["seller_id"] == "123456"       # storeFrontUrl 正则提取
    assert out["reserve_to_date"] == 30.5     # 取绝对值
    assert out["period_sales"] == 800 and out["commission"] == 80


def test_settlement_active_no_hold_flag():
    st = {"paymentStatus": "ACTIVE", "storeFrontUrl": "",
          "accountSummary": {"closingBalance": 100.0, "reserve": 0,
                             "scheduledSettlementDate": ""}}
    out = kpi.extract_settlement(st)
    assert out["payout"] == 100.0 and out["no_hold"] is True

    st2 = {"paymentStatus": "ACTIVE", "storeFrontUrl": "",
           "accountSummary": {"closingBalance": 100.0, "reserve": 40,
                              "scheduledSettlementDate": ""}}
    out2 = kpi.extract_settlement(st2)
    assert out2["payout"] == 60.0 and out2["no_hold"] is False


def test_prev_recon_strict_minus_14_days():
    assert kpi.prev_recon_date("2026-08-19") == "08052026"        # MMDDYYYY
    assert kpi.prev_recon_date("08/19/2026") == "08052026"
    ms = str(int(datetime(2026, 8, 19, tzinfo=timezone.utc).timestamp() * 1000))
    assert kpi.prev_recon_date(ms) == "08052026"
    assert kpi.prev_recon_date("") is None


def test_payment_summary_total_negative_to_zero():
    recs = [{"Transaction Type": "Sale", "Total Payable": 10},
            {"Transaction Type": "PaymentSummary", "Total Payable": -55.2}]
    assert kpi.payment_summary_total(iter(recs)) == 0.0
    recs2 = [{"Transaction Type": "PaymentSummary", "Total Payable": "321.5"}]
    assert kpi.payment_summary_total(iter(recs2)) == 321.5
    assert kpi.payment_summary_total(iter([])) == 0.0


def _make_xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title=title)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_problem_report_rules():
    blob = _make_xlsx({
        "Late Delivery": [
            ["Data current as of 2026-08-05", "", "", ""],
            ["Sales Order Number", "PO #", "Carrier", "Tracking Number"],
            ["108xxx1", "PO1", "USPS", "TRK1"],
            ["=SUM(A1:A2)", "", "", ""],                  # 公式行跳过
            ["", "", "", ""],                              # 空行跳过
        ],
        "Not Accountable": [
            ["Sales Order Number", "Item Name"],
            ["108xxx2", "Great Cup$$Kitchen"],             # $$ 切前半
        ],
    })
    rows = kpi.parse_problem_report("otd", blob)
    assert len(rows) == 2
    r1 = next(r for r in rows if r["sales_order_no"] == "108xxx1")
    assert r1["indicator"] == "🚚 OTD"                     # emoji 契约
    assert r1["sub_category"] == "Late Delivery"
    assert r1["accountable"] == "✅ 是"
    assert r1["carrier"] == "USPS" and r1["tracking_no"] == "TRK1"
    r2 = next(r for r in rows if r["sales_order_no"] == "108xxx2")
    assert r2["accountable"] == "⚪ 否" and r2["sub_category"] == ""
    assert r2["item"] == "Great Cup"                       # $$ 只取前半


# ── api 层 ────────────────────────────────────────────────────────────────────

def test_insights_summary_204_is_none_and_rate_priority(monkeypatch):
    def handler(request):
        if "otd" in request.url.path:
            return httpx.Response(204)
        return httpx.Response(200, json={"payload": {
            "overallRate": 9.9, "sellerAccountableRate": "1.234"}})

    _use(monkeypatch, handler)
    assert insights.performance_summary(STORE, "otd") is None       # 204=无数据
    assert insights.performance_summary(STORE, "vtr") == 1.23       # 优先 sellerAccountable


def test_orders_pagination_model2_cursor_suffix(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "nextCursor" not in str(request.url):
            return httpx.Response(200, json={"list": {
                "meta": {"totalCount": 3, "nextCursor": "?nextCursor=ABC&limit=200"},
                "elements": {"order": [
                    {"orderLines": {"orderLine": [{"charges": {"charge": [
                        {"chargeType": "PRODUCT", "chargeAmount": {"amount": 10.5}},
                        {"chargeType": "SHIPPING", "chargeAmount": {"amount": 99}}]}}]}},
                ]}}})
        return httpx.Response(200, json={"list": {
            "meta": {"totalCount": 3, "nextCursor": None},
            "elements": {"order": [{"orderLines": {"orderLine": []}}]}}})

    _use(monkeypatch, handler)
    stats = {}
    got = list(orders_api.iter_orders(STORE, created_start="2026-08-04T22:30:00Z",
                                      stats=stats))
    assert stats["total"] == 3
    assert len(got) == 2
    assert orders_api.order_product_sales(got[0]) == 10.5   # 只累加 PRODUCT 类费用
    assert "nextCursor=ABC" in calls[1]                     # cursor 即 query 串直接拼


def test_orders_404_is_empty_window(monkeypatch):
    _use(monkeypatch, lambda r: httpx.Response(404, json={}))
    stats = {}
    assert list(orders_api.iter_orders(STORE, created_start="x", stats=stats)) == []
    assert stats["total"] == 0


def test_settlement_moved_out_tells_you_where(monkeypatch):
    """对账摘出后,老命令不能静默变成"什么也没做"——要指路。

    daily_report -p phase=settlement 曾经是有效用法,拆走后若只返回"phase 只
    接受 ...",挂着旧调度的人会以为参数写错,而不是知道它搬家了(problems
    2026-08-08 摘出时就是这么处理的,这里照同一口径)。
    """
    from workflows import daily_report as dr
    out = dr.run({"phase": "settlement"})
    assert "settlement_sync" in out

    from workflows import settlement_sync as ss
    assert hasattr(ss, "run") and ss.DANGEROUS is False


def test_push_never_claims_sent_when_webhook_is_unconfigured(monkeypatch):
    """⚠ 2026-08-16 所有者实见:日志三处写着未配置,摘要却报"日报已推送"。

    sent=False 时一个字节都没发出去,说"已推送"就是假话。摘要是人眼闸门,
    不许自我美化 —— 人只看摘要就会以为发了,而飞书里什么都没有。
    """
    import contextlib

    from registry import db as _db
    from workflows import daily_report as dr

    class _Cur:
        def execute(self, sql, params=None):
            self._n = 3 if "perf_problem" in sql or "DISTINCT" in sql else 1

        def fetchone(self):
            return (41, 32, 1987.69)

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([_Conn()])))
    monkeypatch.setattr(dr.feishu, "notify", lambda text: False)
    out = dr._phase_push("2026-08-16", True)
    assert "未发出" in out and "FEISHU_WEBHOOK_URL" in out
    assert "已推送" not in out          # 一个字都不许出现
    assert "沃尔玛店铺日报" in out       # 内容仍要打出来,人能手动转发

    monkeypatch.setattr(dr.feishu, "notify", lambda text: True)
    assert dr._phase_push("2026-08-16", True) == "日报已推送"


def test_defaults_are_production_defaults(monkeypatch):
    """所有者定稿 2026-08-16 走进生产:不带参数 = kpi(含影刀)+ 看板 + 真发日报。

    ⚠ 这三个默认值改的都是同一类风险:调度里漏写一个开关,后果是**那一段每天
    空转而且报成功** —— 比误跑更难发现(误跑至少有痕迹)。
    """
    import inspect

    from workflows import daily_report as dr
    src = inspect.getsource(dr.run)
    assert 'params.get("yingdao", "1")' in src          # 影刀默认开
    assert 'params.get("push", "1")' in src             # 日报默认真发
    assert 'phase in ("all", "push")' in src            # 默认 all 会走到推送
    assert dr._yingdao_mode("1") == "full"
    assert dr._yingdao_mode("0") == ""                  # 要关得显式关


def test_cli_default_is_execute_not_dry_run():
    """所有者定稿 2026-08-16:缺省即真跑,空跑改为显式 --dry-run。

    ⚠ 与旧铁律相反,理由是进了调度之后"缺省 dry-run"只会伤到自己:launchd 里
    漏写 --execute 的后果是那条链每天空转而且报成功。--execute 保留为兼容别名。
    """
    import cli
    a = cli._parse_args(["problem_product_cleanup"])
    assert a.dry_run is False                            # 缺省真跑
    b = cli._parse_args(["problem_product_cleanup", "--dry-run"])
    assert b.dry_run is True
    c = cli._parse_args(["problem_product_cleanup", "--execute"])
    assert c.dry_run is False and c.execute is True      # 兼容别名不报错


# ── 影刀 spawn:直启主程序,不走 open(2026-08-24 生产实证)──────────────────

def test_yingdao_spawn_launches_the_app_binary_not_open(monkeypatch, tmp_path):
    """⚠ 调度沙箱里 `open <协议URL>` 要经 Launch Services 分发,被沙箱边界
    拦下退 1 —— 表现是影刀每天不跑而日报报成功(wait_fresh 超时降级用旧数据)。
    旧 walmart-kpi-daily 直启主程序是日志验证过的路,这里钉三件事:
    直启不走 open / 协议 URL 原样作 argv / Popen 非阻塞(不 wait 不 check)。
    """
    from services import yingdao

    app = tmp_path / "影刀"
    app.write_text("")
    monkeypatch.setenv("YINGDAO_APP", str(app))
    monkeypatch.setenv("YINGDAO_ROBOT_UUID", "abc-123")
    calls = []
    monkeypatch.setattr(yingdao.subprocess, "Popen",
                        lambda argv, **kw: calls.append(argv))
    assert yingdao.spawn() is True
    assert calls == [[str(app), "shadowbot:Run?robot-uuid=abc-123"]]
    assert "open" not in calls[0]


def test_yingdao_spawn_fails_closed_without_binary_or_uuid(monkeypatch, tmp_path):
    """主程序不存在 / UUID 未配置 → 返回 False 且**不发起任何进程**。
    没有 `open` 兜底:沙箱里 open 恒失败,兜它 = 每天多一次注定失败的调用,
    而且"哪条路在跑"从此变成猜谜。
    """
    from services import yingdao

    calls = []
    monkeypatch.setattr(yingdao.subprocess, "Popen",
                        lambda argv, **kw: calls.append(argv))
    monkeypatch.setenv("YINGDAO_APP", str(tmp_path / "不存在"))
    monkeypatch.setenv("YINGDAO_ROBOT_UUID", "abc-123")
    assert yingdao.spawn() is False
    monkeypatch.setenv("YINGDAO_ROBOT_UUID", "")
    assert yingdao.spawn() is False
    assert calls == []
