"""订单域地基回归:行标识同构、三源归一化、returns 翻页实证语义、recon CSV、upsert。"""

import csv
import hashlib
import io
import time
import zipfile

import httpx
import json
import pytest

from api import _client, reports, returns as returns_api
from services import order_lines as ol

STORE = {"name": "T1", "client_id": "cid_ord", "client_secret": "sec", "proxy": None}


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


# ── 行标识 v3:(PO, SKU),店铺/行号不参与身份 ─────────────────────────────────

def test_order_line_id_v3_semantics():
    # v3 定稿:身份 = (PO, SKU)——同一 PO 内同一 SKU 必合并为一行(所有者实证);
    # 店铺不参与(我方标签,改名不得作废行标识);行号只存列做展示
    def reference(po, sku):
        raw = f"{po}\x1f{sku}"
        return "ol_" + hashlib.sha256(raw.encode()).hexdigest()[:24]

    assert ol.make_order_line_id("108888888888", "B0X1") == reference("108888888888", "B0X1")
    # 仅去首尾空白归一;SKU 大小写敏感,不动
    assert ol.make_order_line_id("PO1", " B0X1 ") == ol.make_order_line_id("PO1", "B0X1")
    assert ol.make_order_line_id("PO1", "b0x1") != ol.make_order_line_id("PO1", "B0X1")
    assert ol.make_order_line_id("PO1", "A") != ol.make_order_line_id("PO1", "B")
    assert ol.norm_line("2.0") == "2"          # 行号归一仍在(展示列用)


# ── 源1:订单展开 ─────────────────────────────────────────────────────────────

_ORDER = {
    "purchaseOrderId": "108000000001", "customerOrderId": "200000001",
    "orderDate": 1754300000000,
    "shippingInfo": {"phone": "555", "postalAddress": {
        "name": "N", "address1": "A1", "city": "C", "state": "TX",
        "postalCode": "75001", "country": "USA"}},
    "orderLines": {"orderLine": [{
        "lineNumber": "1", "statusDate": 1785722223000,
        "item": {"sku": "B0AAAA1111", "productName": "P"},
        "orderLineQuantity": {"amount": "2"},
        "orderLineStatuses": {"orderLineStatus": {
            "status": "Shipped", "statusSetDate": 1754310000000,
            "trackingInfo": {"carrierName": {"carrier": "USPS"},
                             "trackingURL": "https://www.walmart.com/tracking?tracking_id=9400",
                             "trackingNumber": "9400", "shipDateTime": 1754305000000}}},
        "fulfillment": {"pickUpDateTime": 1754400000000},
        "charges": {"charge": [
            {"chargeType": "PRODUCT", "chargeAmount": {"amount": 19.99}},
            {"chargeType": "SHIPPING", "chargeAmount": {"amount": 5.0}}]},
        "refunds": {"refundLines": {"refundLine": {
            "refundComment": "broken",
            "refundCharges": {"refundCharge": {
                "charge": {"chargeAmount": {"amount": -19.99}}}}}}},
    }]},
}


def test_extract_order_lines_full_parse():
    rows = ol.extract_order_lines("T1", _ORDER)
    assert len(rows) == 1
    r = rows[0]
    assert r["order_line_id"] == ol.make_order_line_id("108000000001", "B0AAAA1111")
    assert r["line_number"] == "1"             # 行号仍存列做展示
    assert r["qty"] == 2 and r["sale_status"] == "Shipped"
    assert r["product_amount"] == 19.99 and r["shipping_amount"] == 5.0
    assert r["refund_amount"] == -19.99 and r["refund_comments"] == "broken"
    assert r["carrier"] == "USPS"
    assert r["tracking_url"] == "https://www.walmart.com/tracking?tracking_id=9400"  # 官方优先
    assert r["status_date"] is not None      # 实证:statusDate 在 orderLine 层
    assert ol.tracking_url("USPS", "9400").startswith("https://tools.usps.com")  # 自拼回退仍在
    # 已发货订单无 estimated*:est_ship 回退 shipDateTime,est_delivery 回退 pickUpDateTime
    assert r["est_ship_date"] is not None and r["est_delivery_date"] is not None
    assert r["postal_code"] == "75001"


# ── 源2:售后展开(新结构 + 老格式兼容)───────────────────────────────────────

def test_flatten_return_lines_new_structure():
    order = {
        "returnOrderId": "RMA1", "customerOrderId": "200000001",
        "customerName": {"firstName": "Jo", "lastName": "Doe"},
        "refundMode": "POST_DELIVERY",
        "refundValue": {"totalAmount": 25.5},
        "returnCreationDate": "2026-08-01T00:00:00Z",
        "returnLineGroups": [{"labels": [{"carrierInfoList": [
            {"carrierName": "FedEx", "trackingNo": "777"}]}]}],
        "returnOrderLines": [{
            "purchaseOrderId": "108000000001", "purchaseOrderLineNumber": 1,
            "item": {"sku": "B0AAAA1111"}, "status": "INITIATED",
            "currentRefundStatus": "NOT_REFUNDED", "returnMethod": "KEEP_ITEM",
            "isKeepIt": True, "returnReason": "damaged",
            "quantity": {"amount": 1}}],
    }
    rows = ol.flatten_return_lines("T1", order)
    r = rows[0]
    # 身份用 item.sku(售后行必有),与销售行同 (PO,SKU) → 同一 order_line_id
    assert r["order_line_id"] == ol.make_order_line_id("108000000001", "B0AAAA1111")
    assert r["line_number"] == "1"
    assert r["return_status"] == "INITIATED" and r["is_keep_it"] is True
    assert r["carrier"] == "FedEx" and r["tracking_no"] == "777"
    assert r["refund_total"] == 25.5 and r["customer_name"] == "Jo Doe"


def test_flatten_return_lines_legacy_structure():
    order = {"returnOrderId": "RMA2",
             "returnLines": {"returnLine": [{
                 "purchaseOrderId": "PO9", "purchaseOrderLineNumber": "2",
                 "item": {"sku": "X"}, "status": "CLOSED"}]}}
    rows = ol.flatten_return_lines("T1", order)
    assert rows[0]["return_status"] == "CLOSED"
    assert rows[0]["line_number"] == "2"


# ── api/returns 翻页实证语义 ──────────────────────────────────────────────────

def test_iter_returns_paginates_by_url_appended_cursor(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "offset=200" in str(request.url):
            return httpx.Response(200, json={
                "returnOrders": [{"returnOrderId": "R2"}], "meta": {}})
        # 首页必须同时带成对时间参数
        assert "returnCreationStartDate" in str(request.url)
        assert "returnCreationEndDate" in str(request.url)
        return httpx.Response(200, json={
            "returnOrders": [{"returnOrderId": "R1"}],
            "meta": {"nextCursor": "?limit=200&offset=200"}})

    _use(monkeypatch, handler)
    got = list(returns_api.iter_returns(STORE, created_start="2026-08-01T00:00:00Z"))
    assert [g["returnOrderId"] for g in got] == ["R1", "R2"]
    assert calls[1].endswith("/v3/returns?limit=200&offset=200")   # cursor 整段拼 URL


def test_iter_returns_stops_on_duplicate_cursor_and_404(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "returnOrders": [{"returnOrderId": "R"}],
            "meta": {"nextCursor": "?offset=0"}})   # 永远同一个 cursor

    _use(monkeypatch, handler)
    got = list(returns_api.iter_returns(STORE, created_start="2026-08-01T00:00:00Z"))
    assert len(got) == 2    # 首页 + 拼接页各一条,第三次因 cursor 重复终止

    _client._close_all_clients()
    _use(monkeypatch, lambda r: httpx.Response(404, json={}))
    assert list(returns_api.iter_returns(STORE, created_start="2026-08-01T00:00:00Z")) == []


# ── recon CSV 主路径 ─────────────────────────────────────────────────────────

def _zip_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("recon.csv", buf.getvalue())
    return zbuf.getvalue()


def test_iter_recon_records_reads_zip_csv(monkeypatch):
    body = _zip_csv([
        {"Transaction Type": "PaymentSummary", "Total Payable": "123.45",
         "Purchase Order #": "", "Purchase Order line #": "", "Amount": "",
         "Amount Type": ""},
        {"Transaction Type": "Sale", "Total Payable": "",
         "Purchase Order #": "PO1", "Purchase Order line #": "1",
         "Amount": "94.43", "Amount Type": "Product Price"}])

    def handler(request):
        assert request.headers["accept"] == "application/octet-stream"
        assert request.url.params["reportVersion"] == "v1"
        return httpx.Response(200, content=body)

    _use(monkeypatch, handler)
    rows = list(reports.iter_recon_records(STORE, "07142026"))
    assert rows[0]["Transaction Type"] == "PaymentSummary"
    assert rows[1]["Purchase Order #"] == "PO1"


def test_iter_recon_records_rejects_non_zip(monkeypatch):
    _use(monkeypatch, lambda r: httpx.Response(200, content=b'{"error":"x"}'))
    with pytest.raises(RuntimeError, match="ZIP"):
        list(reports.iter_recon_records(STORE, "07142026"))


# ── 源3:对账聚合(汇总行/浮点相消/入账状态)──────────────────────────────────

def test_aggregate_settlement_skips_summary_and_rounds():
    rows = [
        {"Purchase Order #": "", "Purchase Order line #": "",       # 汇总行必须跳过
         "Amount": "9999", "Amount Type": ""},
        {"Purchase Order #": "PO1", "Purchase Order line #": "1",
         "Partner Item Id": "B0X1",                                 # CSV 自带 SKU 列
         "Amount": "52.68", "Amount Type": "Product Price"},
        {"Purchase Order #": "PO1", "Purchase Order line #": "1",   # 全额退款相消
         "Amount": "-52.68", "Amount Type": "Product Price"},
        {"Purchase Order #": "PO1", "Purchase Order line #": "1",
         "Amount": "-7.9", "Amount Type": "Commission on Product",
         "Commission Rate": "15.0"},
        {"Purchase Order #": "PO1", "Purchase Order line #": "1",
         "Amount": "7.9", "Amount Type": "Commission on Product"},
    ]
    recs, skipped = ol.aggregate_settlement_lines("T1", rows, "07142026")
    assert len(recs) == 1 and skipped == 0
    r = recs[0]
    assert r["order_line_id"] == ol.make_order_line_id("PO1", "B0X1")  # SKU 建键
    assert r["net_amount"] == 0.0          # round6 吸掉 4.44e-16 类浮点残渣
    assert r["gross_amount"] > 0
    assert r["commission_rate"] == 15.0
    assert ol.settle_status(r["net_amount"], r["gross_amount"]) == "已退款"
    assert ol.settle_status(10, 10) == "已入账"
    assert ol.settle_status(-1, 1) == "已冲销"
    assert ol.settle_status(0, 0) == "待入账"
    assert str(r["settle_date"]) == "2026-07-14"


def test_aggregate_settlement_sku_lookup_fallback_and_skip():
    # CSV 无 SKU 列:① sku_lookup 按 (po, 行号) 反查命中 → 建键;② 反查也没有 → 跳过计数
    rows = [
        {"Purchase Order #": "PO1", "Purchase Order line #": "1",
         "Amount": "10", "Amount Type": "Product Price"},
        {"Purchase Order #": "PO2", "Purchase Order line #": "3",
         "Amount": "20", "Amount Type": "Product Price"},
    ]
    recs, skipped = ol.aggregate_settlement_lines(
        "T1", rows, "07142026", sku_lookup={("PO1", "1"): "B0Y2"})
    assert len(recs) == 1 and skipped == 1
    assert recs[0]["order_line_id"] == ol.make_order_line_id("PO1", "B0Y2")
    assert recs[0]["line_number"] == "1"


# ── upsert SQL 形态 ──────────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self):
        self.calls, self.rowcount = [], 3

    def executemany(self, sql, rows):
        self.calls.append((sql, list(rows)))

    def execute(self, sql, args=None):
        self.calls.append((sql, args))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self):
        self.cur = _FakeCursor()

    def cursor(self):
        return self.cur


def test_upserts_build_conflict_sql():
    conn = _FakeConn()
    n = ol.upsert_order_lines(conn, ol.extract_order_lines("T1", _ORDER))
    assert n == 1
    sql, rows = conn.cur.calls[0]
    assert "ON CONFLICT (order_line_id)" in sql and "updated_at = now()" in sql
    assert rows[0]["po_id"] == "108000000001"

    conn2 = _FakeConn()
    ol.upsert_perf_events(conn2, [{"store": "T1", "po_id": "PO1",
                                   "metric": "otd", "period": "2026-08-01",
                                   "detail": {"k": "v"}}])
    sql2, rows2 = conn2.cur.calls[0]
    assert "ON CONFLICT (po_id, metric, period)" in sql2
    assert "first_seen_at" not in sql2.split("DO UPDATE")[1]   # 首见时间不被覆盖
    assert isinstance(rows2[0]["detail"], str)                 # dict 已序列化


def test_upsert_skips_write_when_nothing_changed():
    """内容没变就整行不写——updated_at 才能表示"这行什么时候变的"。

    原先是无条件 `DO UPDATE SET ..., updated_at = now()`,而 order_sync 每轮
    全量重拉 45 天窗口 ⇒ 窗口内每行的 updated_at 都被刷新 ⇒ order_center_push
    的指纹(含「拉取时间」)必变 ⇒ 每轮把窗口内全部行重推飞书。
    2026-08-10 生产实证:7100 行更新 3122,正是 45 天窗口的行数。
    """
    conn = _FakeConn()
    ol.upsert_order_lines(conn, ol.extract_order_lines("T1", _ORDER))
    sql, _ = conn.cur.calls[0]
    body = sql.split("DO UPDATE")[1]
    assert "IS DISTINCT FROM" in body, "没有变更检测 = 每轮全窗口重写"
    # 变更检测比的是**生效后的值**,不是 EXCLUDED——否则被 guard 挡下的
    # 假变化照样会让整行重写、updated_at 空跳
    where = body.split("WHERE")[1]
    assert "CASE WHEN" in where and "t.phone" in where


def test_upsert_phone_all_zero_never_overwrites_real_number():
    """沃尔玛常态把电话打码成全 0(实证 84%),原样覆盖会把真电话冲掉且找不回。

    旧系统的「电话全 0 保护」,legacy_survey 明列必须照搬。
    反向不设防:真电话覆盖全 0 是正常修复。
    """
    conn = _FakeConn()
    ol.upsert_order_lines(conn, ol.extract_order_lines("T1", _ORDER))
    sql, _ = conn.cur.calls[0]
    guard = [seg for seg in sql.split(",") if "phone =" in seg]
    assert guard, "phone 列必须走保护表达式而不是裸 EXCLUDED"
    assert "^0*$" in sql and "THEN t.phone" in sql
    # 其余列不受影响,仍是直接覆盖
    assert "ship_name = EXCLUDED.ship_name" in sql


def test_backfill_perf_line_ids_two_stage_sku_first():
    conn = _FakeConn()
    total = ol.backfill_perf_line_ids(conn)
    assert total == 6                       # FakeCursor rowcount=3 × 两段
    sku_sql = conn.cur.calls[0][0]
    fallback_sql = conn.cur.calls[1][0]
    # 第一段:按 (po_id, sku) 等值连接(v3 下该组合在 order_lines 唯一,无需 HAVING),
    # 店铺不参与(PO 全局唯一)
    assert "p.sku = l.sku" in sku_sql and "HAVING" not in sku_sql
    assert "p.store" not in sku_sql and "p.store" not in fallback_sql
    # 第二段:无 SKU 事件退"该 PO 仅一行"规则;两段都只回填 NULL 行
    assert "HAVING count(*) = 1" in fallback_sql
    assert "p.sku" not in fallback_sql.split("WHERE")[1]
    assert all("order_line_id IS NULL" in s for s in (sku_sql, fallback_sql))


def test_extract_warns_on_duplicate_sku_in_one_po(caplog):
    # 身份前提"同 PO 同 SKU 必合并"被打破时必须告警(兜底不许静默)
    import logging as _logging
    order = {"purchaseOrderId": "PO7", "orderLines": {"orderLine": [
        {"lineNumber": "1", "item": {"sku": "S"}, "orderLineQuantity": {"amount": "1"}},
        {"lineNumber": "2", "item": {"sku": "S"}, "orderLineQuantity": {"amount": "1"}},
    ]}}
    with caplog.at_level(_logging.WARNING, logger="services.order_lines"):
        rows = ol.extract_order_lines("T1", order)
    assert rows[0]["order_line_id"] == rows[1]["order_line_id"]
    assert any("身份撞键" in m for m in caplog.messages)


def test_perf_upsert_skips_write_when_nothing_changed():
    """绩效事件同款:内容没变就别刷 last_seen_at,否则每轮把窗口内全部行重推飞书。

    与 test_upsert_skips_write_when_nothing_changed 是**同一个病**,但当时只修了
    销售那一半。2026-08-17 所有者实见:`飞书投影:绩效订单:1634 行(全量),
    新建 0 更新 1093 跳过 541` —— 1093 正是 still_active 的那批,业务字段一个都
    没变,只因为绩效报表是滚动的、同一批单每轮再出现一次,
    `last_seen_at = now()` 就被无条件刷新,而它作为「拉取时间」参与飞书指纹。

    自我循环:因为我们跑了时间戳才变,因为时间戳变了才推。
    """
    conn = _FakeConn()
    ol.upsert_perf_events(conn, [{"store": "T1", "po_id": "PO1",
                                  "metric": "otd", "period": "2026-08-01",
                                  "detail": {"k": "v"}}])
    body = conn.cur.calls[0][0].split("DO UPDATE")[1]
    assert "IS DISTINCT FROM" in body, "没有变更检测 = 每轮全窗口刷时间戳"
    where = body.split("WHERE")[1]
    # ⚠ 比的必须是**生效后的值**(带 COALESCE),不是 EXCLUDED:
    # order_line_id/sku 的 COALESCE 会把"新值为 NULL"挡成不变,拿 EXCLUDED 比
    # 则把这种假变化当真变化,整行照写、时间戳照跳,等于闸没装
    assert "COALESCE(EXCLUDED.order_line_id" in where
    assert "COALESCE(EXCLUDED.sku" in where
    # 裸 EXCLUDED.order_line_id / EXCLUDED.sku 不许出现在比较里
    import re
    assert not re.search(r"(?<!COALESCE\()EXCLUDED\.order_line_id", where)
    assert not re.search(r"(?<!COALESCE\()EXCLUDED\.sku", where)


def test_perf_last_seen_at_has_no_judgement_reader():
    """`last_seen_at` 少刷新不影响任何结论 —— still_active 看的是 period。

    这条钉的是**改动的前提**:哪天有人拿 last_seen_at 当判据(比如"多久没见到
    就算滚出窗口"),上面那道闸就会静默改变它的语义。届时本用例转红。
    """
    import pathlib
    ddl = pathlib.Path("refdata/schema.sql").read_text(encoding="utf-8")
    span = ddl.split("CREATE OR REPLACE VIEW orders.perf_event_spans")[1]
    span = span.split(";")[0]
    assert "still_active" in span
    assert "max(e.period) = m.latest_period" in span
    assert "last_seen_at" not in span, "still_active 开始依赖 last_seen_at 了"


# ── 下单时间写一次(所有者定稿 2026-09-02)──────────────────────────────────────

class _ConflictCur:
    """带 fetchall 的假游标:模拟库里已有行。"""
    def __init__(self, existing):
        self.existing, self.calls = existing, []

    def execute(self, sql, args=None):
        self.calls.append((sql, args))

    def executemany(self, sql, rows):
        self.calls.append((sql, list(rows)))

    def fetchall(self):
        return self.existing

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ConflictConn:
    def __init__(self, existing):
        self.cur = _ConflictCur(existing)

    def cursor(self):
        return self.cur


def test_order_date_observe_then_confirm_guard_and_repair_mode():
    """下单时间观测→定稿(所有者定稿 2026-09-02):upsert 的 order_date 走状态
    守卫(定稿锁死 / 首见接受 / 连续两轮一致才改判),三列状态只在插入时写
    (skip_update),upsert 之后另发一条不碰 updated_at 的观测记账。
    repair_order_date=True 是显式修复模式,才允许 API 值直接覆盖。"""
    conn = _FakeConn()
    rows = ol.extract_order_lines("T1", _ORDER)
    ol.upsert_order_lines(conn, rows)
    sql, sent = conn.cur.calls[0]
    assert "WHEN t.order_date_confirmed THEN t.order_date" in sql
    assert "WHEN EXCLUDED.order_date = t.order_date_seen THEN EXCLUDED.order_date" in sql
    assert "order_date_seen = EXCLUDED" not in sql and "order_date_confirmed = EXCLUDED" not in sql
    assert "order_meta = EXCLUDED" not in sql          # 三列只插入不更新
    assert sent[0]["order_date_seen"] is None and sent[0]["order_date_confirmed"] is False
    assert sent[0]["order_date_streak"] == 0 and "order_date_streak = EXCLUDED" not in sql
    meta = json.loads(sent[0]["order_meta"])
    assert meta["orderDate"] == _ORDER["orderDate"]
    assert meta["lines"][0]["sku"] == rows[0]["sku"] and "seenAt" in meta
    assert "_envelope" not in sent[0]
    state_sql, state_rows = conn.cur.calls[1]
    assert state_sql.startswith("UPDATE orders.order_lines t SET order_date_confirmed")
    assert "updated_at" not in state_sql             # 观测记账不许触发飞书重推
    assert "t.order_date_seen = %(observed)s::timestamptz THEN true" in state_sql
    assert state_rows == [{"id": rows[0]["order_line_id"], "observed": rows[0]["order_date"]}]
    conn = _FakeConn()
    ol.upsert_order_lines(conn, ol.extract_order_lines("T1", _ORDER),
                          repair_order_date=True)
    sql, _ = conn.cur.calls[0]
    assert "order_date = EXCLUDED.order_date" in sql and "t.order_date_confirmed" not in sql


def test_order_date_screening_classifies_and_never_silences(caplog):
    """API 值与库值不一致必须逐条记 warning(带信封摘要)并按库里状态分三类交给
    调用方进摘要:已定稿=冲突(库值保留)/ 未定稿且等于上轮观测=改判 / 其余=待定。
    库空(首见)/相同不算冲突。"""
    from datetime import datetime, timezone
    rows = ol.extract_order_lines("T1", _ORDER)
    lid = rows[0]["order_line_id"]
    api_od = rows[0]["order_date"]
    other = datetime(2026, 9, 2, 4, 12, tzinfo=timezone.utc)
    with caplog.at_level("WARNING"):
        got = ol.screen_order_dates(
            _ConflictConn([(lid, "108000000001", "B0A", other, None, True, None, 0, None)]), rows)
    assert got == [{"kind": "冲突", "po": "108000000001", "sku": "B0A",
                    "db": other, "api": api_od}]
    assert "下单时间冲突" in caplog.text and "108000000001" in caplog.text
    assert '"customerOrderId"' in caplog.text          # 信封摘要进日志取证
    assert ol.screen_order_dates(
        _ConflictConn([(lid, "p", "s", other, api_od, False, None, 0, None)]), rows)[0]["kind"] == "改判"
    assert ol.screen_order_dates(
        _ConflictConn([(lid, "p", "s", other, None, False, None, 0, None)]), rows)[0]["kind"] == "待定"
    assert ol.screen_order_dates(
        _ConflictConn([(lid, "p", "s", other, other, False, None, 0, None)]), rows)[0]["kind"] == "待定"
    # 已定稿 + 同一异值已连续两轮(streak=2)+ 本轮又是它 ⇒ 疑错,并给修复命令
    got = ol.screen_order_dates(
        _ConflictConn([(lid, "108000000001", "B0A", other, api_od, True, None, 2, None)]), rows)
    assert got[0]["kind"] == "疑错" and "repair_order_date=108000000001" in caplog.text
    assert ol.screen_order_dates(
        _ConflictConn([(lid, "p", "s", other, api_od, True, None, 1, None)]), rows)[0]["kind"] == "冲突"
    assert ol.screen_order_dates(_ConflictConn([(lid, "p", "s", api_od, None, False, None, 0, None)]), rows) == []
    assert ol.screen_order_dates(_ConflictConn([(lid, "p", "s", None, None, False, None, 0, None)]), rows) == []
    assert ol.screen_order_dates(_ConflictConn([]), []) == []


def test_api_order_date_after_first_sight_is_rejected_not_written(caplog):
    """原文实证(2026-09-02):沃尔玛会把别的订单/加了整数天的时间当 orderDate 回来。
    订单不可能在我们第一次看见它之后才下单:晚于本行 created_at 的 API 值置空不写,
    也不算一次观测;归类 拒写 并带信封进日志。"""
    from datetime import timedelta
    rows = ol.extract_order_lines("T1", _ORDER)
    lid, api_od = rows[0]["order_line_id"], rows[0]["order_date"]
    created = api_od - timedelta(days=5)             # 五天前就见过这单
    with caplog.at_level("WARNING"):
        got = ol.screen_order_dates(
            _ConflictConn([(lid, "p", "s", created - timedelta(hours=1), None, False, created, 0, None)]), rows)
    assert got[0]["kind"] == "拒写" and rows[0]["order_date"] is None
    assert "晚于本行首次入库" in caplog.text and '"customerOrderId"' in caplog.text
    # 首次入库晚于下单时间(正常)不拦
    rows = ol.extract_order_lines("T1", _ORDER)
    assert ol.screen_order_dates(
        _ConflictConn([(lid, "p", "s", api_od, None, False, api_od + timedelta(hours=2), 0, None)]),
        rows) == [] and rows[0]["order_date"] == api_od


def test_garbage_timestamps_from_walmart_become_null(caplog):
    """原文实证:orderDate=907、estimatedDeliveryDate=-18000000/0、statusDate=0。
    早于 2020 的一律当没有:下单时间拒写并告警,状态/预计时间静默归 NULL。"""
    import copy
    o = copy.deepcopy(_ORDER)
    o["orderDate"] = 907
    o["orderLines"]["orderLine"][0]["statusDate"] = 1     # 0 会回退到 statusSetDate,那是另一条路
    o["orderLines"]["orderLine"][0]["fulfillment"] = {"estimatedDeliveryDate": -18000000,
                                                     "estimatedShipDate": 1}   # 0 会回退到 shipDateTime
    with caplog.at_level("WARNING"):
        r = ol.extract_order_lines("T1", o)[0]
    assert r["order_date"] is None and r.get("_order_date_rejected") is True
    assert r["status_date"] is None and r["est_delivery_date"] is None and r["est_ship_date"] is None
    assert "早于 2020" in caplog.text and "907" in caplog.text


def _detail(ms, **extra):
    """假详情钩子:返回带 orderDate 的订单对象。"""
    def look(po):
        look.calls.append(po)
        return {"purchaseOrderId": po, "orderDate": ms, "orderType": "REGULAR", **extra}
    look.calls = []
    return look


def test_new_order_is_committed_from_detail_and_locked(caplog):
    """所有者方案(2026-09-02):新单首见以详情接口的 orderDate 落库并直接定稿;
    列表值与详情不同记「详情补正」;定稿依据 source=detail。"""
    rows = ol.extract_order_lines("T1", _ORDER)
    list_od = rows[0]["order_date"]
    d = _detail(_ORDER["orderDate"] + 5000)
    with caplog.at_level("WARNING"):
        got = ol.screen_order_dates(_ConflictConn([]), rows, detail=d)
    assert d.calls == [rows[0]["po_id"]]
    assert got[0]["kind"] == "详情补正" and got[0]["api"] == list_od
    assert rows[0]["order_date"] != list_od and rows[0]["order_date_confirmed"] is True
    assert rows[0]["order_date_source"] == "detail"
    assert rows[0]["_envelope"]["detail"]["orderType"] == "REGULAR"
    # 详情与列表一致:静默定稿
    rows = ol.extract_order_lines("T1", _ORDER)
    assert ol.screen_order_dates(_ConflictConn([]), rows, detail=_detail(_ORDER["orderDate"])) == []
    assert rows[0]["order_date_confirmed"] is True
    # 详情不可用(404 / 异常):退回列表候选,不定稿
    rows = ol.extract_order_lines("T1", _ORDER)
    assert ol.screen_order_dates(_ConflictConn([]), rows, detail=lambda po: None) == []
    assert "order_date_confirmed" not in rows[0]

    def boom(po):
        raise RuntimeError("timeout")
    rows = ol.extract_order_lines("T1", _ORDER)
    got = ol.screen_order_dates(_ConflictConn([]), rows, detail=boom)
    assert got[0]["kind"] == "详情失败" and "order_date_confirmed" not in rows[0]
    # 凭证死店原样上抛(交店级重试标准归类)
    from api import _client

    def dead(po):
        raise _client.StoreDeadError("T1", 401)
    import pytest
    with pytest.raises(_client.StoreDeadError):
        ol.screen_order_dates(_ConflictConn([]), ol.extract_order_lines("T1", _ORDER), detail=dead)


def test_detail_verified_rows_ignore_list_noise_and_unverified_rows_get_checked(caplog):
    """所有者定稿(2026-09-02 简化):详情定过稿的行,列表再不一致只计冲突、不查详情、
    不改;没被详情核对过的行(存量/首见查失败)每轮必查详情,查到就按详情定稿。"""
    from datetime import datetime, timezone
    rows = ol.extract_order_lines("T1", _ORDER)
    lid, api_od = rows[0]["order_line_id"], rows[0]["order_date"]
    other = datetime(2026, 8, 20, 4, 12, tzinfo=timezone.utc)
    other_ms = int(other.timestamp() * 1000)
    # 详情定稿 + 列表≠库 ⇒ 冲突,详情一次都不查,行不动
    d = _detail(other_ms)
    with caplog.at_level("WARNING"):
        got = ol.screen_order_dates(
            _ConflictConn([(lid, "p", "s", other, None, True, None, 0, "detail")]), rows, detail=d)
    assert got[0]["kind"] == "冲突" and d.calls == [] and "_order_date_fix" not in rows[0]
    # 保险:同一异值已连续两轮(streak=2)再来第三轮 ⇒ 补查一次详情;详情也不认库值 ⇒ 疑错
    rows = ol.extract_order_lines("T1", _ORDER)
    d = _detail(_ORDER["orderDate"])
    got = ol.screen_order_dates(
        _ConflictConn([(lid, "p", "s", other, api_od, True, None, 2, "detail")]), rows, detail=d)
    assert got[0]["kind"] == "疑错" and d.calls == [rows[0]["po_id"]] and "_order_date_fix" not in rows[0]
    assert "repair_order_date=" in caplog.text
    # 同样三轮,但详情仍认库值 ⇒ 冲突
    rows = ol.extract_order_lines("T1", _ORDER)
    got = ol.screen_order_dates(
        _ConflictConn([(lid, "p", "s", other, api_od, True, None, 2, "detail")]), rows,
        detail=_detail(other_ms))
    assert got[0]["kind"] == "冲突"
    # 没被详情核对过(列表两轮定稿,source=list)+ 列表与库一致 ⇒ 照样查详情;详情≠库 ⇒ 改判
    rows = ol.extract_order_lines("T1", _ORDER)
    d = _detail(other_ms)
    got = ol.screen_order_dates(
        _ConflictConn([(lid, "p", "s", api_od, None, True, None, 0, "list")]), rows, detail=d)
    assert got[0]["kind"] == "改判" and rows[0]["_order_date_fix"] == other and d.calls
    # 未定稿(source 空)+ 详情=库值 ⇒ 静默升级为详情定稿,不进异常清单
    rows = ol.extract_order_lines("T1", _ORDER)
    got = ol.screen_order_dates(
        _ConflictConn([(lid, "p", "s", other, None, False, None, 0, None)]), rows,
        detail=_detail(other_ms))
    assert got == [] and rows[0]["_order_date_fix"] == other
    # 详情给的值本身不可信(未来)⇒ 当不可用,列表≠库时退回两轮逻辑(待定)
    rows = ol.extract_order_lines("T1", _ORDER)
    got = ol.screen_order_dates(
        _ConflictConn([(lid, "p", "s", other, None, False, None, 0, None)]), rows,
        detail=_detail(4102444800000))
    assert got[0]["kind"] == "待定" and "_order_date_fix" not in rows[0]
    # 详情不可用且列表=库 ⇒ 什么都不做
    rows = ol.extract_order_lines("T1", _ORDER)
    assert ol.screen_order_dates(
        _ConflictConn([(lid, "p", "s", api_od, None, False, None, 0, None)]), rows,
        detail=lambda po: None) == []

def test_order_date_later_than_status_is_flagged_not_rejected(caplog):
    """下单时间晚于本行状态时间 = 不可能的事实:标记存疑并告警,但不拒写——
    拒写会让整单从所有按下单时间取数的口子里消失。"""
    import copy
    o = copy.deepcopy(_ORDER)
    o["orderDate"] = 1754400000000                      # 晚于行 statusDate 一天以上
    o["orderLines"]["orderLine"][0]["statusDate"] = 1754300000000
    with caplog.at_level("WARNING"):
        rows = ol.extract_order_lines("T1", o)
    assert rows[0]["order_date"] is not None and rows[0].get("_order_date_suspect") is True
    assert "存疑" in caplog.text
    assert "_order_date_suspect" not in ol.extract_order_lines("T1", _ORDER)[0]
    # 沃尔玛 statusDate 的垃圾值(1970/0001/2026-01-01)不当参照,否则整批误标
    o = copy.deepcopy(_ORDER)
    o["orderLines"]["orderLine"][0]["statusDate"] = 10000          # 1970-01-01
    assert "_order_date_suspect" not in ol.extract_order_lines("T1", o)[0]


def test_future_order_date_is_rejected_with_warning(caplog):
    """晚于当前时刻的 orderDate 不是下单时间(生产实见 09/09 未来值):拒写留 NULL
    ——配合写一次守卫不会抹掉库里已有值——并告警。"""
    order = dict(_ORDER, orderDate=4102444800000)          # 2100-01-01
    with caplog.at_level("WARNING"):
        rows = ol.extract_order_lines("T1", order)
    assert rows[0]["order_date"] is None
    assert "晚于当前时刻" in caplog.text
    assert ol.extract_order_lines("T1", _ORDER)[0]["order_date"] is not None


def test_order_without_po_is_skipped_not_collapsed_to_literal_none(caplog):
    """"purchaseOrderId": null 时 dict.get 默认值不生效,str(None) 会造出字面量
    'None' 当 PO,让所有缺 PO 的订单跨店塌进同一行(对抗审查实证)。
    没有 PO 的订单没有身份:跳过 + 告警,不入库。"""
    with caplog.at_level("WARNING"):
        assert ol.extract_order_lines("T1", dict(_ORDER, purchaseOrderId=None)) == []
        assert ol.extract_order_lines("T1", dict(_ORDER, purchaseOrderId="")) == []
    assert "没有 purchaseOrderId" in caplog.text
    assert "None" not in ol.make_order_line_id("108000000001", "B0A")


def test_duplicate_order_line_id_in_one_batch_first_wins_and_warns(caplog):
    """两张信封给出同一 (PO, SKU):此前 executemany 静默后写覆盖先写、返回值还是
    塌缩前行数。现在先到者胜(与写一次守卫同向)、告警、返回真实行数。"""
    a = ol.extract_order_lines("T1", _ORDER)[0]
    b = dict(a, order_date=None, customer_order_id="OTHER")
    conn = _FakeConn()
    with caplog.at_level("WARNING"):
        n = ol.upsert_order_lines(conn, [a, b])
    assert n == 1
    _sql, rows = conn.cur.calls[0]
    assert len(rows) == 1 and rows[0]["customer_order_id"] == "200000001"
    assert "出现两个订单对象" in caplog.text
