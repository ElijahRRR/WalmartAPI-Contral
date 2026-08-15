"""kpi_history_import 解析积木 + 影刀衔接(输入清单/新鲜度)回归。"""

import json
from datetime import datetime, timezone

from services import kpi, yingdao

# 生产真实表头(2026-08-08 预览实证:七个中文表头曾未映射,回归钉住)
_HEADER = ["日期", "店铺", "卖家名称", "partnerId", "sellerId", "店铺状态",
           "支付状态", "销售状态", "在线商品", "有库存", "无库存", "昨日出单",
           "昨日销售额($)", "准时送达(90%)", "取消率", "有效追踪(99%)",
           "卖家回复率(95%)", "退款率", "差评率", "退货率", "未收到",
           "账期销售额($)", "佣金", "退款金额", "期末余额", "迄今备用金($)",
           "回款", "回款日", "收款方", "结算周期", "无Hold", "上期回款"]


def test_header_map_full_and_traps():
    mapping, unmapped = kpi.map_history_header(_HEADER)
    assert unmapped == []
    by_field = {f: i for i, f in mapping.items()}
    # 包含关系陷阱:更具体的先占
    assert by_field["data_date"] == 0          # 「回款日」没抢走「日期」
    assert by_field["payout_date"] == 27
    assert by_field["payout"] == 26
    assert by_field["prev_payout"] == 31
    assert by_field["store"] == 1              # 「店铺状态」没抢走「店铺」
    assert by_field["store_status"] == 5
    assert by_field["refund_rate"] == 17       # 「退款金额」没抢走「退款率」
    assert by_field["refund_amount"] == 23
    assert by_field["period_sales"] == 21      # 「账期销售额」没被「销售额」抢走
    assert by_field["sales_amount"] == 12
    assert by_field["otd_rate"] == 13          # 准时送达/有效追踪/卖家回复率(中文)
    assert by_field["vtr_rate"] == 15
    assert by_field["srr_rate"] == 16          # 「卖家回复率」没抢走「卖家名称」
    assert by_field["seller_name"] == 2
    assert by_field["reserve_to_date"] == 25   # 迄今备用金
    assert by_field["payment_processor"] == 28  # 收款方
    assert by_field["no_hold"] == 30
    # 全部 32 列都有归属
    assert len(mapping) == 32


def test_parse_history_rows():
    rows = [
        ["2026/8/1", "别名店", "卖家A", "P1", "S1", "ACTIVE", "ACTIVE", "可售",
         "1,234", "1000", "234", "12", "$1,234.56", "98.5%", "0.5", "99", "100",
         "1.2", "0", "2.5", "0.8", "$5,000", "$750", "$88", "$1,000.00",
         "$0", "$900", "2026-08-05", "PAYONEER", "14", "是", "$800"],
        ["", "x"],                       # 无日期 → 跳过
        ["2026-08-02"],                  # 只有日期,其余全空 → 保留(值全 None)
    ]
    out, skipped = kpi.parse_history_rows("A085朱丽霖", _HEADER, rows)
    assert skipped == 1 and len(out) == 2
    r = out[0]
    assert r["store"] == "A085朱丽霖"          # sheet 标题权威,店铺列不采信
    assert r["data_date"] == "2026-08-01"
    assert r["items_online"] == 1234           # 千分位
    assert r["sales_amount"] == 1234.56        # $ + 千分位
    assert r["otd_rate"] == 98.5               # % 剥离
    assert r["no_hold"] is True
    assert r["payout_date"] == "2026-08-05"
    assert r["srr_rate"] == 100.0 and r["vtr_rate"] == 99.0
    assert r["reserve_to_date"] == 0.0 and r["payment_processor"] == "PAYONEER"
    assert r["prev_payout"] == 800.0
    r2 = out[1]
    assert r2["data_date"] == "2026-08-02" and r2["orders_count"] is None


def test_board_header_columns_aligned():
    from registry import resources
    from workflows import daily_report as dr
    # 看板中文表头与 registry 列序一一对应(32 列),且与导入映射对称:
    # 每个表头都能被 _HIST_HEADER_MAP 映射回同一个字段
    assert len(dr._BOARD_HEADER) == len(resources.KPI_BOARD_OVERVIEW.columns) == 32
    mapping, unmapped = kpi.map_history_header(dr._BOARD_HEADER)
    assert unmapped == []
    for i, field in mapping.items():
        assert resources.KPI_BOARD_OVERVIEW.columns[i] == field
    # 数字列写数字、日期列写序列值(所有者定稿 2026-08-08)
    from datetime import date
    from decimal import Decimal
    assert dr._board_cell("payout", None) == ""
    assert dr._board_cell("no_hold", True) == "是"
    assert dr._board_cell("payout", Decimal("12.5")) == 12.5
    assert dr._board_cell("items_online", 7) == 7
    assert dr._board_cell("data_date", date(2026, 8, 8)) == 46242   # 序列值
    assert dr._board_cell("payout_date", "2026-08-08") == 46242
    assert dr._board_cell("payout_date", "怪值") == "怪值"           # 解析不了原样
    assert dr._board_cell("store", "A085") == "A085"
    fmts = dict(dr._board_formats(10))
    # 日期格式跟着 data_date 走到 B 列(A 列现在是店铺,文本不设 formatter)
    assert "A2:A11" not in fmts
    assert fmts["B2:B11"] == "yyyy/MM/dd" and fmts["AB2:AB11"] == "yyyy/MM/dd"
    assert fmts["I2:I11"] == "#,##0" and fmts["M2:M11"] == "#,##0.00"


def test_board_first_two_columns_are_store_then_date():
    """所有者定稿 2026-08-15:看板首列店铺、次列日期。"""
    from registry import resources
    from workflows import daily_report as dr
    assert resources.KPI_BOARD_OVERVIEW.columns[:2] == ("store", "data_date")
    assert resources.KPI_BOARD_HISTORY.columns[:2] == ("store", "data_date")
    assert dr._BOARD_HEADER[:2] == ["店铺", "日期"]


def test_old_kpi_sheet_is_read_only_now():
    """旧「店铺KPI」workbook 从此只读(kpi_history_import 的导入源)。

    影刀输入投影已改文件,本仓不再按列位写它 —— columns 必须空着,
    以免下次有人看见一串列名以为"这表还在写",照着加回写入路径:
    老影刀应用可能还在读它,两个应用同时被喂数据 = 双 spawn 互抢。
    """
    import inspect

    from registry import resources
    from workflows import daily_report as dr
    assert resources.KPI_SHEET.columns == ()
    src = inspect.getsource(dr)
    assert "KPI_SHEET" not in src


def test_store_sort_key_is_not_the_db_collation():
    """2026-08-15 生产实见:PG collation 忽略中文主排序权重,把「谭总1」当「1」排,
    排出 谭总1…谭总8 → 81刘何秀 → 82杨乾良 → 谭总9 → A085。所以排序归代码管。

    所有者要的序:字母开头 → 数字开头 → 中文,组内按数字大小自然序。
    """
    from services import stores as stores_svc
    got = sorted(["谭总1", "谭总2", "谭总8", "81刘何秀", "82杨乾良", "谭总9",
                  "A085朱丽霖", "A089王译尉", "A090徐本傲", "A091张晓康",
                  "谭总10", "谭总11"], key=stores_svc.sort_key)
    assert got == ["A085朱丽霖", "A089王译尉", "A090徐本傲", "A091张晓康",
                   "81刘何秀", "82杨乾良",
                   "谭总1", "谭总2", "谭总8", "谭总9", "谭总10", "谭总11"]
    # 谭总10 排在 谭总9 之后 —— 纯字符串序会把它排到 谭总2 前面
    assert stores_svc.sort_key("谭总9") < stores_svc.sort_key("谭总10")
    # 数字按大小不按位数:A85 与 A085 同值,靠原名兜底稳定
    assert stores_svc.sort_key("A085x")[2] == 85
    # 无数字的店不炸,排在同前缀带号店之前
    assert stores_svc.sort_key("总仓") < stores_svc.sort_key("总仓2")
    assert stores_svc.sort_key("")[0] == 2 and stores_svc.sort_key(None)[3] == ""


def test_board_pages_both_sort_by_store(monkeypatch):
    """两页都按店铺排序:总览天然一店一行,历史页店内再按日期降序。

    历史页若按日期横切,同一家店会被打散在 90 个日期块里,首列提到店铺就白提了
    —— 排序和列序是同一个诉求的两半。
    ⚠ 店铺序**必须由 Python 排**(见 stores.sort_key),SQL 只负责日期降序;
    这条用例同时钉住"历史 SQL 不许再自己按 store 排"。
    """
    import contextlib

    from registry import db as _db
    from workflows import daily_report as dr

    sqls: list[str] = []
    rows = {
        "overview": [("谭总9",), ("81刘何秀",), ("A085朱丽霖",), ("谭总10",)],
        # 历史:SQL 已按日期降序返回,店内次序要被稳定排序保住
        "history": [("谭总9", "d3"), ("A085朱丽霖", "d3"),
                    ("谭总9", "d2"), ("A085朱丽霖", "d1")],
    }

    class _Cur:
        def execute(self, sql, params=None):
            sqls.append(" ".join(sql.split()))

        def fetchall(self):
            return rows["overview" if len(sqls) == 1 else "history"]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    written = {}
    monkeypatch.setattr(_db, "pg_conn",
                        contextlib.contextmanager(lambda: iter([_Conn()])))
    monkeypatch.setattr(dr, "_board_matrix", lambda rs: list(rs))
    def _capture(sheet, rs):
        written[sheet.name] = rs
        return len(rs)

    monkeypatch.setattr(dr.feishu, "sheet_overwrite", _capture)
    monkeypatch.setattr(dr.feishu, "sheet_set_formatter", lambda sheet, items: len(items))
    dr._phase_board(90)
    overview_sql, history_sql = sqls
    assert "DISTINCT ON (store)" in overview_sql
    assert "SELECT store, data_date, seller_name" in history_sql
    assert history_sql.endswith("ORDER BY data_date DESC")   # store 不进 SQL
    # 总览:按 sort_key 重排(A → 数字 → 中文,谭总9 在 谭总10 前)
    assert [r[0] for r in written["KPI看板-总览"][1:]] == [
        "A085朱丽霖", "81刘何秀", "谭总9", "谭总10"]
    # 历史:店铺归堆,店内保留 SQL 的日期降序
    assert written["KPI看板-历史"][1:] == [
        ("A085朱丽霖", "d3"), ("A085朱丽霖", "d1"),
        ("谭总9", "d3"), ("谭总9", "d2")]


def test_extract_settlement_legacy_shape():
    """旧代码实证结构(2026-08-08 定稿):payload 嵌套 + URL 编码 + 回款公式。"""
    stmt = {
        "partnerId": "P1",
        "payload": {
            "sellerInfo": {
                "storeFrontUrl": "https%3A%2F%2Fwww.walmart.com%2Fseller%2F12345",
                "sellerStatus": "ACTIVE", "paymentStatus": "ACTIVE"},
            "accountSummary": {
                "closingBalance": 100.0, "reserve": 20.0, "holdAmount": 5.0,
                "reserveToDate": -10.5, "scheduledSettlementDate": "2026-08-19",
                "paymentProcessor": "PAYONEER", "settleCycle": "14"},
            "transactionDetails": {
                "saleAggregate": {"productPrice": 500, "netComm": 50},
                "refundDetails": {"productPrice": 30}}},
    }
    out = kpi.extract_settlement(stmt)
    assert out["seller_id"] == "12345"          # URL 解码后才匹配得上
    assert out["payout"] == 75.0                # closing − reserve − hold
    assert out["no_hold"] is False              # 75 < 100
    assert out["reserve_to_date"] == 10.5 and out["payment_status"] == "ACTIVE"
    # 非 ACTIVE → 回款强制 0
    stmt["payload"]["sellerInfo"]["paymentStatus"] = "HOLD"
    assert kpi.extract_settlement(stmt)["payout"] == 0.0
    # 旧表「无Hold」列取值是 "不押款"/空
    assert kpi._hist_value("no_hold", "不押款") is True
    assert kpi._hist_value("no_hold", "") is None


def test_yingdao_freshness():
    trigger = datetime(2026, 8, 8, 0, 0, tzinfo=timezone.utc)
    fresh = {"scraped_at": "2026-08-08T08:07:05+08:00"}     # 00:07 UTC > trigger
    stale = {"scraped_at": "2026-08-07T08:07:05+08:00"}
    assert yingdao.is_fresh(fresh, trigger) is True
    assert yingdao.is_fresh(stale, trigger) is False
    assert yingdao.is_fresh({}, trigger) is False           # 缺字段不炸
    assert yingdao.is_fresh({"scraped_at": "垃圾"}, trigger) is False


def _rows_for_input():
    return [
        {"store": "Z店", "seller_id": "222", "store_status": "ACTIVE",
         "payment_status": "ACTIVE", "seller_name": "不该出现"},
        {"store": "A店", "seller_id": "111", "store_status": "active",
         "payment_status": None},                       # 大小写不敏感
        {"store": "空号店", "seller_id": "", "store_status": "ACTIVE",
         "payment_status": "ACTIVE"},
        {"store": "空号店2", "seller_id": None, "store_status": None,
         "payment_status": None},
        {"store": "停用店", "seller_id": "333", "store_status": "SUSPENDED",
         "payment_status": "ACTIVE"},
        {"store": "无状态店", "seller_id": "444", "store_status": "",
         "payment_status": "ACTIVE"},
        {"store": "重复店", "seller_id": "222", "store_status": "ACTIVE",
         "payment_status": "ACTIVE"},                   # 与 Z店 同 sellerId
    ]


def test_write_input_filters_each_counted(monkeypatch, tmp_path):
    """三道过滤各自计数 —— 丢弃静默常态化 = 少抓一半没人知道。

    ⚠ 空 sellerId 是 A147 事故防线:影刀拿它拼出 /seller//cp/shopall
    (路径中段为空)会崩掉整条 RPA 循环,后果不是少抓这一家,是它之后的店全被跳过。
    """
    monkeypatch.setenv("YINGDAO_INPUT_JSON", str(tmp_path / "input.json"))
    st = yingdao.write_input(_rows_for_input())
    assert st == {"written": 3, "no_seller_id": 2, "inactive": 1,
                  "dup_seller_id": 1, "unknown_status": 1}
    data = json.loads((tmp_path / "input.json").read_text(encoding="utf-8"))
    assert data["count"] == 3
    assert [s["store"] for s in data["stores"]] == ["A店", "Z店", "无状态店"]
    assert data["stores"][0]["seller_id"] == "111"
    assert data["stores"][0]["payment_status"] == ""               # None → 空串
    # 只公开约定的四个字段,别的列不许漏给影刀
    assert set(data["stores"][0]) == set(yingdao._INPUT_FIELDS)


def test_suspended_stores_default_to_not_selling():
    """所有者定稿 2026-08-15:SUSPENDED / TERMINATED 的店销售状态默认「不可售」。

    这些店不进影刀清单,不给默认值的话销售状态会永远空着;店都停了,
    定义上就是不可售。⚠ 不违反「销售状态不做跨日延续」—— 它推导自本轮观测到的
    store_status,不是回填昨天的旧值。
    """
    assert kpi.derived_sales_status("SUSPENDED") == "不可售"
    assert kpi.derived_sales_status("TERMINATED") == "不可售"
    assert kpi.derived_sales_status("suspended") == "不可售"      # 大小写不敏感
    assert kpi.derived_sales_status("ACTIVE") is None            # 在营的靠影刀实测
    assert kpi.derived_sales_status("") is None                  # 空≠停用,别乱标
    assert kpi.derived_sales_status(None) is None
    assert (kpi.store_activity("ACTIVE"), kpi.store_activity("SUSPENDED"),
            kpi.store_activity("")) == ("active", "inactive", "unknown")


def test_scraped_and_derived_never_contradict(monkeypatch, tmp_path):
    """「不抓它」与「标它不可售」必须永远指同一批店。

    两边各写一份 upper() != "ACTIVE" 的话,一旦飘开就会出现"抓了它却把它标成
    不可售"这种自相矛盾,而且没有任何报错。这里逐个状态值对拍。
    """
    monkeypatch.setenv("YINGDAO_INPUT_JSON", str(tmp_path / "input.json"))
    for status in ("ACTIVE", "active", "SUSPENDED", "TERMINATED", "INACTIVE",
                   "", None, "  Active  "):
        st = yingdao.write_input([{"store": "S", "seller_id": "1",
                                   "store_status": status}])
        scraped = st["written"] == 1
        derived = kpi.derived_sales_status(status)
        # 抓它 ⇔ 不给它推导不可售;不抓它 ⇔ 直接标不可售
        assert scraped is (derived is None), status


def test_empty_store_status_is_included_not_treated_as_inactive(monkeypatch,
                                                                tmp_path):
    """⚠ 空状态 ≠ 停用,是"没拿到"。

    extract_settlement 取 store_status 时**没有 _find_key 兜底**(payment_status
    有),结算响应换个形状就是空串。把空当停用 = 让解析故障静默地少抓一半店
    —— 2026-08-15 生产实见 38 家里 18 家被判 inactive,正是这个成因待查。
    """
    monkeypatch.setenv("YINGDAO_INPUT_JSON", str(tmp_path / "input.json"))
    st = yingdao.write_input([
        {"store": "空状态", "seller_id": "1", "store_status": None},
        {"store": "真停用", "seller_id": "2", "store_status": "SUSPENDED"},
    ])
    assert st["written"] == 1 and st["unknown_status"] == 1 and st["inactive"] == 1
    data = json.loads((tmp_path / "input.json").read_text(encoding="utf-8"))
    assert [s["store"] for s in data["stores"]] == ["空状态"]


def test_write_input_overwrites_never_accumulates(monkeypatch, tmp_path):
    """每轮整文件覆盖:上一轮的店不许残留(旧飞书投影就是栽在 sheet 长存上)。"""
    monkeypatch.setenv("YINGDAO_INPUT_JSON", str(tmp_path / "input.json"))
    yingdao.write_input(_rows_for_input())
    yingdao.write_input([{"store": "单店", "seller_id": "999",
                          "store_status": "ACTIVE", "payment_status": "ACTIVE"}])
    data = json.loads((tmp_path / "input.json").read_text(encoding="utf-8"))
    assert data["count"] == 1 and data["stores"][0]["seller_id"] == "999"


def test_write_input_is_atomic_and_leaves_no_tmp(monkeypatch, tmp_path):
    """原子写:影刀随时可能在读,不许让它读到写了一半的文件。"""
    monkeypatch.setenv("YINGDAO_INPUT_JSON", str(tmp_path / "sub" / "input.json"))
    yingdao.write_input(_rows_for_input())
    assert (tmp_path / "sub" / "input.json").exists()      # 目录自建
    assert list((tmp_path / "sub").glob("*.tmp")) == []    # 临时文件已 rename 掉


def test_yingdao_mode_rejects_unknown_value():
    """打错一个字不许当成"关"静默跑过去 —— 少跑半条链而摘要看不出区别。"""
    import pytest

    from workflows import daily_report as dr
    assert dr._yingdao_mode(None) == "" and dr._yingdao_mode("0") == ""
    assert dr._yingdao_mode("1") == "full" and dr._yingdao_mode("YES") == "full"
    assert dr._yingdao_mode(" Input ") == "input"
    with pytest.raises(ValueError, match="yingdao 只接受"):
        dr._yingdao_mode("inupt")


def test_yingdao_input_mode_writes_list_without_spawning(monkeypatch, tmp_path):
    """接入调试档:清单要真写出来,但一次都不许 spawn。

    旧 walmart-kpi-daily 还开着时就靠这一条 —— spawn 了就是双 spawn 互抢,
    会把旧系统那次影刀的新鲜度校验搅到超时。
    """
    from workflows import daily_report as dr
    monkeypatch.setenv("YINGDAO_INPUT_JSON", str(tmp_path / "input.json"))
    monkeypatch.setattr(dr.yingdao, "spawn",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("yingdao=input 不许 spawn")))
    out = dr._yingdao_refresh(_rows_for_input(), "2026-08-15", do_spawn=False)
    assert "输入清单 3 店" in out and "未 spawn" in out
    assert "⚠ 状态为空仍纳入 1 店" in out       # 警戒计数必须进摘要,不能只在日志
    data = json.loads((tmp_path / "input.json").read_text(encoding="utf-8"))
    assert data["count"] == 3


def test_yingdao_refresh_skips_spawn_when_no_store_left(monkeypatch, tmp_path):
    """全店 sellerId 都为空 → 清单是空的,不该再 spawn 影刀去抓个寂寞。"""
    from workflows import daily_report as dr
    monkeypatch.setenv("YINGDAO_INPUT_JSON", str(tmp_path / "input.json"))
    monkeypatch.setattr(dr.yingdao, "spawn",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("没店可抓就不该 spawn")))
    out = dr._yingdao_refresh([{"store": "空号店", "seller_id": "",
                                "store_status": None, "payment_status": None}],
                              "2026-08-15")
    assert "输入清单 0 店" in out and "no_seller_id 1" in out
